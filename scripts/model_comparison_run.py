"""モデル比較（OOF）を CLI から回す。UI の「④ 戦略を検証 → モデル比較（OOF）」と同一手続き。

`model_comparison.run_comparison()` をそのまま呼ぶだけの薄い入口で、**測る手続きを持たない**。
rank-IC・分位リターン・ロングショート spread・売り側 spread・ターンオーバー・区間被覆は
各モデルの `oof_backtest`（`plugins/macro_snapshots.py`・ADR-0004/0014/0018/0020）が、
モデル間の差の有意性は `model_stats.significance_matrix`（ADR-0018 の定常ブートストラップ）が
既に持っている。ここでそれらを再実装すると、**測ったものが本番と別物になる**（ADR-0041 で
preset の rank-IC ゲートが3回書き直されかけたのと同型）。

`--models` で部分集合を指定できる。2モデルだけ測るときも fold（`min_train_months=6` /
`step_months=3` / `embargo_months=12`）・特徴量・significance_matrix の手続きは全件時と同一。

副作用なし: `run_comparison` が `tuning_objective_only()`（全社スコアリングを省く）と
`tuning_dry_run()`（producer 永続化を no-op）の中で各モデルを回すため、本番の
`macro_*_scores` を実測で上書きしない。

実行:
    python -m scripts.model_comparison_run                                    # 全モデル
    python -m scripts.model_comparison_run --models macro_gbdt,macro_gbdt_rank
    python -m scripts.model_comparison_run --models macro_gbdt --json out.json

接続先は `FINAPP_DB_TARGET`（既定 local＝ローカル正本・#503/ADR-0038）に従う。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_comparison import COMPARISON_MODELS, run_comparison  # noqa: E402


def _num(v, digits: int = 4) -> str:
    """None 安全な数値整形（欠測は '-'）。cp932 で落ちる記号は使わない。"""
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}"
    return str(v)


def _print_models(models: list[dict]) -> None:
    print("")
    print("=== モデル別 OOF ===")
    header = ("model  rank-IC   IC std   IC(業種中立)  LS spread  short spread  "
              "turnover  breakeven(bp)  n_periods  n_oof")
    print(header)
    print("-" * len(header))
    for m in models:
        short = m.get("short", "?")
        if not m.get("available"):
            print(f"{short:<6} 実行不可: reason={m.get('reason')} {m.get('error') or ''}")
            continue
        oof = m.get("oof_backtest") or {}
        ic = oof.get("rank_ic") or {}
        inic = oof.get("rank_ic_industry_neutral") or {}
        print(
            f"{short:<6} "
            f"{_num(ic.get('mean')):>8} "
            f"{_num(ic.get('std')):>8} "
            f"{_num(inic.get('mean')):>13} "
            f"{_num(oof.get('long_short_spread')):>10} "
            f"{_num(oof.get('short_side_spread')):>13} "
            f"{_num(oof.get('effective_turnover')):>9} "
            f"{_num(oof.get('breakeven_cost_bps'), 1):>14} "
            f"{str(oof.get('n_periods')):>10} "
            f"{str(oof.get('n_oof_samples')):>6}"
        )


def _print_significance(sig: dict | None) -> None:
    print("")
    print("=== rank-IC 差の有意性（定常ブートストラップ・ADR-0018）===")
    if not sig:
        print("（IC 系列を持つモデルが2本未満のため未算出）")
        return
    print("pair       mean_diff   95%CI                p_value  significant  better  n_common")
    for key, r in sorted((sig.get("pairs") or {}).items()):
        ci = f"[{_num(r.get('ci_lo'))}, {_num(r.get('ci_hi'))}]"
        print(
            f"{key:<10} {_num(r.get('mean_diff')):>9}   {ci:<20} "
            f"{_num(r.get('p_value'), 3):>7}  {str(r.get('significant')):<11}  "
            f"{str(r.get('better')):<6}  {r.get('n_common')}"
        )
    print("")
    print("注: p は差の検定であって強さではない。点推定の大小ではなく CI が 0 を跨ぐかで読む")
    print("    （walk-forward の per-fold IC は学習窓が重なり系列相関を持つ・fold 数も10前後）。")


def main() -> int:
    names = [n for n, _ in COMPARISON_MODELS]
    ap = argparse.ArgumentParser(description="モデル比較（OOF）を CLI から実行する")
    ap.add_argument("--models", default=None,
                    help=f"カンマ区切りのプラグイン名（既定=全件）。選択肢: {','.join(names)}")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="生レスポンスを書き出す JSON パス")
    args = ap.parse_args()

    only = [s.strip() for s in args.models.split(",") if s.strip()] if args.models else None
    if only:
        print(f"対象: {only}")
    else:
        print(f"対象: 全 {len(names)} モデル")

    t0 = time.time()
    db = SessionLocal()
    try:
        res = asyncio.run(run_comparison(db, render_light_mode=False, only_models=only))
    finally:
        db.close()
    elapsed = time.time() - t0

    _print_models(res.get("models") or [])
    _print_significance(res.get("significance_matrix"))
    print("")
    print(f"所要 {elapsed:.1f} 秒 / computed_at={res.get('computed_at')}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"JSON を書き出しました: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
