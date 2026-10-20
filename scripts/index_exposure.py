"""露出τの道具 — 本番予測の上位・下位に TOPIX 移行措置・新規採用の銘柄がどれだけ並ぶか（#1006・親 #1001）。

ラベル（52週後の実現値）を待たずに今すぐ見える指標。M-2 の本番は規模を中立化しておらず（#957）、M-6 の
中立化も浮動株ではなく総時価総額が基準なので、移行措置の銘柄（小型・低流動が多い）が推奨の上位や下位に
偏って並んでも、そのままでは見えない。モデルごとに、4 群（transition / new_add / other / unknown・
`index_events.group_labels`）の件数、上位・下位 5 分位の占有率と母集団比率の対比、平均順位分位を出す。
事前登録（`docs/prereg/index-event-2026.md` §6.3 前向き監視）では**記述**であって採否の根拠にしない。

## 何を読むか（DB へは書かない）

- 群: 指数イベント台帳（`index_events.load_ledger`）。`on` と `known_at` に同じ日（`--asof`・既定は今日の JST）を
  使う＝先読みしない。台帳が使えなければ失敗する（黙って全社 unknown にしない）
- モデルの予測（M-1 / M-2 / M-3 / M-6 / gap）: 現行値は写しと同じ入口（`prediction_snapshots.read_source`）。
  `--snapshot-date` を付けると、積んだ写し（`prediction_snapshots` の表）から読む——反映前の 2026-10-21 の
  予測はここにある（バックアップの世代を一時スキーマへ復元する経路は作らない＝DB へ書かない）
- 推奨プリセット: `plugins.recommend.score_candidates` で全社を採点する（画面と同じ候補の条件と採点）。
  財務の断面から計算するので写しが無く、`--snapshot-date` のときは出さない

## 守ること

- **最初に重なりの件数を出す**（移行措置 683・新規採用 35 のうち、各モデルの予測が付いた社の母集団・
  売買可能な社＝`tradable_filters` に入る社数）。移行措置が全ての母集団で 0 社なら終了コード 2
  （黙って空の表を出さない）
- 上位は「点数が高い側」（μ̂・乖離率・プリセットの点数）。上位と下位の両方を出すので、モデルごとの向き
  （M-3 は M-2 / M-6 と逆向き）は読み手が判断する
- 夜間には入れない（ADR-0061 決定1）。手で回す。軽い（DB の現行値を読むだけ）

実行: python -m scripts.index_exposure [--asof YYYY-MM-DD] [--snapshot-date YYYY-MM-DD] [--out <json>]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 失敗したときの終了コード（argparse の引数欠落と同じ 2）
EXIT_FAILED = 2
REVIEW = "topix-2026-10"
GROUPS = ("transition", "new_add", "other", "unknown")
WATCHED_KINDS = ("transition", "new_add")
QUINTILE = 0.2
# 表の名前 → `prediction_snapshots` の源
MODEL_SOURCES = {"M-1": "macro_risk_return", "M-2": "macro_gbdt", "M-3": "macro_dlm",
                 "M-6": "macro_enet", "gap": "sector_ols"}


def _finite(v) -> bool:
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


def exposure_table(scores: Mapping[str, float], groups: Mapping[str, str]) -> dict:
    """露出τ（純関数）。`scores` は {edinet_code: 点数}（高いほど上位）、`groups` は {edinet_code: 群}。

    群の無い社は unknown。点数が None / NaN の社は母集団に入れない。5 分位の大きさ `k` は
    `max(1, N // 5)`。平均順位分位は (N − 順位 + 0.5) / N の平均（1 に近いほど上位・一様なら 0.5）。
    占有率の対比 `top_lift` は「上位 5 分位に占める割合 ÷ 母集団に占める割合」（母集団に居なければ None）。
    """
    items = sorted(((float(s), ec) for ec, s in scores.items() if _finite(s)), key=lambda x: (-x[0], x[1]))
    n_all = len(items)
    k = max(1, n_all // 5) if n_all else 0
    top = {ec for _, ec in items[:k]}
    bottom = {ec for _, ec in items[n_all - k:]} if k else set()
    pct = {ec: (n_all - i - 0.5) / n_all for i, (_, ec) in enumerate(items)}
    out: dict = {"n": n_all, "k": k, "groups": {}}
    for g in GROUPS:
        members = [ec for _, ec in items if groups.get(ec, "unknown") == g]
        n = len(members)
        base = n / n_all if n_all else None
        n_top = sum(1 for ec in members if ec in top)
        n_bottom = sum(1 for ec in members if ec in bottom)
        top_share = n_top / k if k else None
        bottom_share = n_bottom / k if k else None
        out["groups"][g] = {
            "n": n, "base_rate": base,
            "top_n": n_top, "top_share": top_share,
            "top_lift": top_share / base if base else None,
            "bottom_n": n_bottom, "bottom_share": bottom_share,
            "bottom_lift": bottom_share / base if base else None,
            "mean_rank_pct": sum(pct[ec] for ec in members) / n if n else None,
        }
    return out


def overlap_counts(members: Mapping[str, frozenset], populations: Mapping[str, set]) -> dict:
    """{群: {"total": 件数, 母集団: 重なりの件数, ...}}（純関数）。"""
    return {kind: {"total": len(m), **{name: len(m & set(pop)) for name, pop in populations.items()}}
            for kind, m in members.items()}


# ── DB から読む（テストでは差し替える）────────────────────────────────────────

def model_scores(db, snapshot_date: Optional[str] = None) -> dict:
    """{表の名前: {edinet_code: 値}}。`snapshot_date` があれば `prediction_snapshots` の表から読む。"""
    import prediction_snapshots as PS

    out: dict = {}
    for name, source in MODEL_SOURCES.items():
        col = PS.value_column(source)
        if snapshot_date is None:
            rows = PS.read_source(db, source)
            out[name] = {r["edinet_code"]: r[col] for r in rows if _finite(r[col])}
        else:
            from database import PredictionSnapshot as S

            q = db.query(S.edinet_code, getattr(S, col)).filter(
                S.snapshot_date == snapshot_date, S.source == source)
            out[name] = {ec: v for ec, v in q if _finite(v)}
    return out


def preset_scores(db) -> dict:
    """{"preset:<名前>": {edinet_code: 点数}}（画面と同じ候補の条件と採点・全社）。"""
    from plugins.recommend import DEFAULT_MIN_COVERAGE, get_all_presets, score_candidates

    out: dict = {}
    for name, weights in get_all_presets(db).items():
        cand = score_candidates(db, weights, min_coverage=DEFAULT_MIN_COVERAGE)
        out[f"preset:{name}"] = {r.edinet_code: s for s, _, r, _ in cand.scored if r.edinet_code}
    return out


def tradable_codes(db) -> set:
    """`tradable_filters` を満たす社（companies 基準）。"""
    from database import Company, tradable_filters

    return {ec for (ec,) in db.query(Company.edinet_code).filter(
        *tradable_filters(db, is_active=Company.is_active, edinet_code=Company.edinet_code))}


def _fmt(v, pct=True) -> str:
    if v is None:
        return "   -  "
    return f"{v * 100:5.1f}%" if pct else f"{v:6.3f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="露出τ: 本番予測の上位・下位に指数イベントの銘柄がどれだけ並ぶか")
    ap.add_argument("--asof", help="群を決める日（YYYY-MM-DD・既定は今日の JST。--snapshot-date があればその日）")
    ap.add_argument("--snapshot-date", help="prediction_snapshots の写した日から読む（YYYY-MM-DD）")
    ap.add_argument("--out", help="結果の JSON の書き出し先")
    ap.add_argument("--lists-dir", help="指数の一覧の置き場（既定はリポジトリの .index_lists）")
    args = ap.parse_args(argv)

    import index_events as IE
    import prediction_snapshots as PS
    from database import SessionLocal

    asof = date.fromisoformat(args.asof or args.snapshot_date or PS.jst_today().isoformat())
    with SessionLocal() as db:
        ledger = IE.load_ledger(db, root=Path(args.lists_dir) if args.lists_dir else None)
        if not ledger.ok:
            print(f"[index_exposure] NG: 指数イベント台帳が使えない（{ledger.reason}）", file=sys.stderr)
            return EXIT_FAILED
        members = {k: IE.members(ledger, REVIEW, k) for k in WATCHED_KINDS}
        tables = model_scores(db, args.snapshot_date)
        if args.snapshot_date is None:
            tables.update(preset_scores(db))
        populations = {name: set(s) for name, s in tables.items()}
        populations["tradable"] = tradable_codes(db)
        overlap = overlap_counts(members, populations)
        exposure = {name: exposure_table(s, IE.group_labels(ledger, s.keys(), on=asof, known_at=asof))
                    for name, s in tables.items()}

    print(f"露出τ（{REVIEW}・群の日 {asof}・予測 {'写し ' + args.snapshot_date if args.snapshot_date else '現行値'}）")
    print("重なり（台帳の社のうち各母集団に入る社数）:")
    for kind, row in overlap.items():
        print(f"  {kind:10s} 台帳 {row['total']:4d} | " + " ".join(
            f"{name}={row[name]}" for name in populations))
    if not any(overlap["transition"][name] for name in populations):
        print("[index_exposure] NG: 移行措置の社がどの母集団にも入っていない（台帳か予測の読み違い）",
              file=sys.stderr)
        return EXIT_FAILED
    print("\n表         N     k | 群          件数  母集団比  上位5分位  下位5分位  平均順位分位")
    for name, t in exposure.items():
        for g in WATCHED_KINDS:
            r = t["groups"][g]
            print(f"{name:14s} {t['n']:5d} {t['k']:4d} | {g:10s} {r['n']:5d}  {_fmt(r['base_rate'])}"
                  f"    {_fmt(r['top_share'])}     {_fmt(r['bottom_share'])}     {_fmt(r['mean_rank_pct'], False)}")
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"review": REVIEW, "asof": asof.isoformat(), "snapshot_date": args.snapshot_date,
             "overlap": overlap, "exposure": exposure}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n書き出し: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
