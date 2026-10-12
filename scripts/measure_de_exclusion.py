"""証券業・その他金融業の D/E を空欄にした VIEW で factor premia を測る（#941・#927）。正本には書かない。

## 何を測るのか

#926 で D/E の分子を有利子負債へ広げると、借入が事業の資金そのものである証券業・その他金融業（リース・
信販など）の D/E が大きく出る。この 2 業種を D/E の比較から外すかを、factor premia（推奨の「統計的最適化」の
重みの元）について測る。M-2 / M-6 は `scripts/momentum_gate --de-exclude`（#940）が測る。

    keep    … 2業種の D/E を残す（本番の構成）＝基準
    exclude … 2業種の D/E を空欄にする（業種名の正本は `database.DE_RATIO_EXCLUSION_CANDIDATES`）

出すもの（JSON・`scripts/.cache/measure_de_exclusion.json`）:

1. `z_de_ratio` の係数（`compute_factor_premia` の `mean_b` / `t_stat` / `p_value`）と期間数
2. 母集団（月数・行数・社数）と、`exclude` で外れた社数
3. 各条件で推定した重み（`mean_b`）で作った合成スコアの rank-IC（`preset_ic_gate.ic_series`）を、各条件の
   母集団と共通 (ym, ec) 域の両方で。共通域の差は `paired_ic_significance` で検定する。**重みを推定したのと
   同じパネルで測る in-sample の値**なので、採否の根拠ではなく記録として読む（ADR-0059 と同じ注意）

## なぜ VIEW をトランザクションの中で差し替えるのか

`z_de_ratio` は VIEW が**同じ年度の全社で標準化**する。2 業種の D/E を Python 側で空欄にするだけでは、
他社の Z スコアが標準化し直されず近似になる。Python で Z を作り直すと VIEW の式の写しになり、ADR-0041 の
「書き直すと別物を測る」に当たる。そこで、同じセッションの中で `financial_metrics` を作り直し、本番の関数
（`compute_factor_premia` / `build_period_panel`）を呼んでから必ず取り消す。安全装置は
`scripts/measure_industry_fill_impact.simulated`（commit を flush に、rollback を例外に差し替える）を使う。

- **VIEW は main の SQL から作り直す**（`keep` も）。DB 上の VIEW は古いことがあり、それと比べると定義の
  差が「業種を外した差」に混ざる
- 差し替えるのは `de_ratio` の除外業種の文字列だけで、該当が1か所でなければ止まる（式が変わったのに
  気づかず別物を測らない）
- 単一因子（`z_de_ratio` だけ）の rank-IC は**測らない**: 同じ社の集合の上では、年度ごとの標準化が変わっても
  順位は変わらない（Spearman は正の一次変換で不変）ので、共通域で差が出ない
- 実行の前後で `pg_get_viewdef` と行数を比べ、違えば exit=1（取り消しが効いていない）

実行:
    python -m scripts.measure_de_exclusion
    python -m scripts.measure_de_exclusion --json out.json

接続先は `FINAPP_DB_TARGET`（既定 local）。計測中は `financial_metrics` に ACCESS EXCLUSIVE が掛かり、
他の読み手（夜間バッチ・ローカルの API）を待たせるので、日中枠（`measure:de-exclusion`）で何も並走させずに
回す。**#926 のマージ後に測る**（新しい定義の D/E でないと測る意味がない）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

import database as D  # noqa: E402
from database import (  # noqa: E402
    DDL_LOCK_TIMEOUT, DE_RATIO_EXCLUDED_INDUSTRIES, DE_RATIO_EXCLUSION_CANDIDATES, SessionLocal,
    db_timeouts, guard_local_target,
)
from model_stats import paired_ic_significance  # noqa: E402
from scripts.measure_industry_fill_impact import SimulationBroken, simulated  # noqa: E402
from scripts.preset_ic_gate import ic_series  # noqa: E402

VIEW = "financial_metrics"
CONDS: dict[str, tuple[str, ...]] = {"keep": (), "exclude": DE_RATIO_EXCLUSION_CANDIDATES}
BASE_COND = "keep"
FACTOR = "z_de_ratio"
DEFAULT_JSON = Path(__file__).resolve().parent / ".cache" / "measure_de_exclusion.json"
EXIT_BROKEN = 1


def _not_in(names: tuple[str, ...]) -> str:
    return "NOT IN (" + ", ".join(f"'{n}'" for n in names) + ")"


def exclusion_view_sql(base_sql: str, extra: tuple[str, ...]) -> str:
    """`de_ratio` の除外業種に `extra` を足した VIEW の SQL を返す（`extra` が空なら元のまま）。

    除外業種の文字列は `DE_RATIO_EXCLUDED_INDUSTRIES` から組み立てる（VIEW と同じ並び・照合は
    `tests/test_financial_metrics_with_ttm.py`）。**該当が1か所でなければ止める**——0 か所なら式が
    変わっていて、2 か所以上なら別の式まで書き換えてしまう。どちらも黙って別物を測る。
    """
    if not extra:
        return base_sql
    old = _not_in(DE_RATIO_EXCLUDED_INDUSTRIES)
    n = base_sql.count(old)
    if n != 1:
        raise ValueError(f"VIEW の SQL で {old} が {n} か所（1か所のはず）。de_ratio の式が変わった可能性がある")
    return base_sql.replace(old, _not_in(DE_RATIO_EXCLUDED_INDUSTRIES + tuple(extra)))


def weights_from_result(result) -> dict:
    """factor premia の結果を合成スコアの重みへ（`preset_ic_gate.premia_weights` と同じく mean_b をそのまま）。"""
    return {f: float(b) for f, b in result.mean_b.items() if b is not None}


def panel_keys(ids: dict) -> set:
    return {(ym, ec) for ym, ecs in ids.items() for ec in ecs}


def restrict_panel(panel: dict, ids: dict, keys: set) -> dict:
    """パネルを (ym, ec) の集合へ絞る（共通域）。期内の行の並びは保つ。"""
    import numpy as np

    out = {}
    for ym, (X, y) in panel.items():
        rows = [i for i, ec in enumerate(ids.get(ym, [])) if (ym, ec) in keys]
        if len(rows) >= 2:
            out[ym] = (np.asarray(X)[rows], np.asarray(y)[rows])
    return out


def view_fingerprint(db) -> tuple[str, int]:
    """VIEW の定義（Postgres が再構成した文字列）と行数。取り消しが効いたかの照合に使う。"""
    viewdef = db.execute(text("SELECT pg_get_viewdef(CAST(:v AS regclass), true)"), {"v": VIEW}).scalar()
    n = db.execute(text(f"SELECT count(*) FROM {VIEW}")).scalar()
    return viewdef, int(n)


def measure_condition(db, sql: str) -> dict:
    """トランザクションの中で VIEW を `sql` に差し替え、本番の関数で測って必ず取り消す。"""
    from plugins.macro_snapshots import shared_snapshot_cache
    from recommend_factor_premia import build_period_panel, compute_factor_premia

    with simulated(db) as state:
        with db_timeouts(db, lock=DDL_LOCK_TIMEOUT):
            db.execute(text(f"DROP VIEW IF EXISTS {VIEW}"))
            db.execute(text(sql))
        # 同じ条件の中だけ load_data / build_snapshots を共有する（VIEW が違う条件へ持ち越さない）
        with shared_snapshot_cache():
            result = compute_factor_premia(db)
            ids: dict = {}
            panel, names = build_period_panel(db, stock_ids_out=ids)
    if state["rollback_calls"] or state["commit_blocked"]:
        raise SimulationBroken(f"模擬実行が壊れた: {state}")
    if names != result.factor_names or len(panel) != result.n_periods:
        raise SimulationBroken("factor premia とパネルの形が食い違う（別の財務を読んだ可能性）")
    return {"result": result, "panel": panel, "ids": ids, "names": names}


def summarize(measured: dict) -> dict:
    """条件ごとの係数・母集団・rank-IC と、共通域の対比較をまとめる（DB に触らない純関数）。"""
    keys = {c: panel_keys(m["ids"]) for c, m in measured.items()}
    common = set.intersection(*keys.values())
    base_cos = {ec for _ym, ec in keys[BASE_COND]}
    out: dict = {"conditions": {}, "common_rows": len(common)}
    ic_common: dict = {}
    for cond, m in measured.items():
        r = m["result"]
        w = weights_from_result(r)
        own = ic_series(m["panel"], m["names"], w)
        com = ic_series(restrict_panel(m["panel"], m["ids"], common), m["names"], w)
        ic_common[cond] = com
        cos = {ec for _ym, ec in keys[cond]}
        out["conditions"][cond] = {
            "industries_blanked": list(CONDS[cond]),
            FACTOR: {"mean_b": r.mean_b.get(FACTOR), "t_stat": r.t_stat.get(FACTOR),
                     "p_value": r.p_value.get(FACTOR)},
            "n_periods": r.n_periods,
            "rows": len(keys[cond]),
            "companies": len(cos),
            "companies_dropped_vs_keep": len(base_cos - cos),
            "rank_ic_own_mean": (sum(own.values()) / len(own)) if own else None,
            "rank_ic_common_mean": (sum(com.values()) / len(com)) if com else None,
            "weights": weights_from_result(r),
        }
    other = [c for c in measured if c != BASE_COND]
    out["paired_common"] = {
        f"{c}|{BASE_COND}": paired_ic_significance(ic_common[c], ic_common[BASE_COND])
        for c in other}
    out["note"] = ("rank-IC uses weights estimated on the same panel (in-sample); "
                   "record only, not a basis for adoption")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="証券業・その他金融業の D/E を空欄にした VIEW で factor premia を測る")
    ap.add_argument("--json", dest="json_path", default=str(DEFAULT_JSON), help="結果 JSON の出力先")
    args = ap.parse_args(argv)
    guard_local_target()

    db = SessionLocal()
    try:
        before = view_fingerprint(db)
        db.rollback()   # 読取トランザクションを持ち越さない（以降は simulated が1条件ずつ張る）
        measured = {}
        for cond, extra in CONDS.items():
            sql = exclusion_view_sql(D.FINANCIAL_METRICS_VIEW_SQL, extra)
            measured[cond] = measure_condition(db, sql)
            after = view_fingerprint(db)
            db.rollback()
            if after != before:
                print(f"[broken] {cond}: VIEW の定義か行数が実行前と違う（取り消しが効いていない）", flush=True)
                return EXIT_BROKEN
            print(f"[{cond}] periods={measured[cond]['result'].n_periods} "
                  f"rows={sum(len(v) for v in measured[cond]['ids'].values())}", flush=True)
    except SimulationBroken as e:
        print(f"[broken] {e}", flush=True)
        return EXIT_BROKEN
    finally:
        db.close()

    summary = summarize(measured)
    for cond, s in summary["conditions"].items():
        z = s[FACTOR]
        print(f"[{cond}] {FACTOR} mean_b={z['mean_b']} t={z['t_stat']} p={z['p_value']} "
              f"companies={s['companies']} dropped={s['companies_dropped_vs_keep']} "
              f"ic_own={s['rank_ic_own_mean']} ic_common={s['rank_ic_common_mean']}", flush=True)
    for key, sig in summary["paired_common"].items():
        print(f"[paired] {key}: {sig}", flush=True)
    print(f"[note] {summary['note']}", flush=True)
    out = Path(args.json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
