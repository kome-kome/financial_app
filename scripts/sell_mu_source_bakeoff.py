"""Issue #402: sell_ranking の既定 μ 出所（mu_source）を M-2 / M-6 / M-4 のどれにするかの実測。

**Issue #402 の前提の訂正**:
  Issue の改善案は「`/api/backtest`（`source=sell`）で `mu_source` を振る」だったが、
  `backtest.py` の `source="sell"` は **recommend プリセット加重和の符号反転**
  （`score_record`）であり、sell_ranking の μ / −R_macro 観点を一切使わない
  （`mu_source` パラメータも持たない・`/api/backtest` のクエリにも無い）。
  加えて producer スコアテーブル（`macro_gbdt_scores` 等）は「現在時点」のスナップショット
  しか持たないため、過去日付の as-of バックテストへ持ち込むと look-ahead になる。
  よって μ 出所の売り性能を honest に測れるのは OOF 側（purge/embargo 付き walk-forward・
  ADR-0014）だけであり、本スクリプトは `oof_backtest` の **売り側指標**
  （`short_side_spread`・Issue #402 で追加）で比較する。

手法:
  M-4（3基底＝M-1+M-2+M-6）を1回実行すると `base_oof_backtest` に**同一共通 (ym,ec) 域**へ
  制限した各基底の OOF が揃う（ADR-0015 の base-on-common）。母集団差の交絡なしに
  M-1 / M-2 / M-6 / M-4 の4通りを横並び比較できるため、実行は1回で足りる。

  読み方（売り判定として）:
    - short_side_spread = 期内全体平均 − bottom 分位平均。**大きいほど**「売り候補（μ̂ 下位）が
      市場平均を下回った」＝売りシグナルとして有効。
    - bottom 分位リターン（quantile_returns[0]）は**小さいほど**良い（売って正解だった）。
    - long_short_spread / rank-IC は買い側（top 分位）の強さに引っ張られるため、売り判定の
      優劣判定には使わない（参考値として併記する）。
  有意差は per-fold 系列を共通 test 期でペアリングした定常ブートストラップ
  （`model_stats.paired_ic_significance`・ADR-0018）で見る。

  μ と −R_macro の合成について: sell_ranking の既定プリセット「マクロ予測型」は
  mu:1.0 + neg_r_macro:0.5 の加重和だが、`r_macro` は共有 macro_beta 由来で **mu_source
  非依存**（sell_ranking.execute のコメント参照）。したがって mu_source 間の差分は μ 成分に
  帰着し、μ 単独ランキングでの比較で優劣の向きは保たれる。

  R3 ゲート（Issue の第2項）: OOF の `interval_halfwidth` は honest split-conformal の
  **marginal**（全銘柄一定）半幅なので、M-2 と M-6 の水準比を見れば「同じ `r3_gate` 閾値を
  使ったときにどちらが厳しく足切りされるか」の目安になる。per-stock バケット半幅
  （producer 側 `conformal_bucket_halfwidths`）の分布までは OOF では測れない。

データは `scripts/_cache.py` 経由でローカル pickle を読む（Issue #355・本番 Egress ゼロ）。
producer 永続化は `tuning_dry_run()` で no-op、現在μ̂スコアリングは `tuning_objective_only()`
で省略する（本番 `macro_ensemble_scores` を実測で汚さない）。

実行:
    python -m scripts.sell_mu_source_bakeoff                 # 実測（キャッシュ利用）
    python -m scripts.sell_mu_source_bakeoff --smoke         # 間引きパネルで動作確認
    python -m scripts.sell_mu_source_bakeoff --refresh-cache # 本番から取り直す
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from scripts.ensemble_base_bakeoff import _load_panel, _run_config  # noqa: E402
from scripts._cache import set_refresh  # noqa: E402

_OUT = Path(__file__).resolve().parent / ".cache" / "sell_mu_source_bakeoff.json"

# M-4 の基底＝比較対象の μ 出所（sell_ranking.params_schema の mu_source 候補のうち
# per-(ym,銘柄) OOF を出せるもの）。M-3 は水準を持つが M-4 の基底ではないため対象外
# （M-5 は順位スコアのみ・ADR-0017）。
BASES = ["macro_risk_return", "macro_gbdt", "macro_enet"]
LABELS = {
    "macro_risk_return": "M-1",
    "macro_gbdt":        "M-2（現既定）",
    "macro_enet":        "M-6",
}
# 現既定と切替候補（結論の自動判定に使う）。
CURRENT = "macro_gbdt"
CANDIDATE = "macro_enet"


def _row(o: dict) -> dict:
    """oof_backtest から売り判定の評価に使う指標だけ抜く。"""
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":            o["rank_ic"]["mean"],
        "rank_ic_std":        o["rank_ic"].get("std"),
        "short_side_spread":  o.get("short_side_spread"),
        "short_side_hit":     o.get("short_side_hit_rate"),
        "bottom_q_return":    q[0] if q else None,
        "top_q_return":       q[-1] if q else None,
        "long_short_spread":  o.get("long_short_spread"),
        "interval_halfwidth": o.get("interval_halfwidth"),
        "n_periods":          o.get("n_periods"),
    }


def _fmt_sig(sig: dict | None) -> str:
    if not sig:
        return "n/a（共通 test 期 < 2）"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={sig.get('p_value', float('nan')):.3f} n={sig['n_common']} "
            f"{'有意' if sig['significant'] else '非有意'}")


def _print_table(rows: dict) -> None:
    hdr = (f"{'モデル':<16}{'売り側spread':>13}{'売り勝率':>10}{'bottom分位':>12}"
           f"{'rank-IC':>10}{'LS spread':>11}{'区間半幅':>10}")
    print(hdr, flush=True)
    print("-" * 82, flush=True)
    for label, r in rows.items():
        def _f(v, w, p=4):
            return f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else f"{'n/a':>{w}}"
        print(f"{label:<16}{_f(r['short_side_spread'], 13)}{_f(r['short_side_hit'], 10)}"
              f"{_f(r['bottom_q_return'], 12)}{_f(r['rank_ic'], 10)}"
              f"{_f(r['long_short_spread'], 11)}{_f(r['interval_halfwidth'], 10)}", flush=True)


def main() -> None:
    # Windows の既定 cp932 では μ̂（結合文字 U+0302）等が出力できずクラッシュするため、
    # リダイレクト時も含めて UTF-8 に固定する（ensemble_base_bakeoff.py と同流儀）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Issue #402 売り側 μ 出所の OOF 実測")
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    args = ap.parse_args()
    set_refresh(args.refresh_cache)

    panel = _load_panel(args)
    db = SessionLocal()
    try:
        run = _run_config("M-4(3基底・rank_ic_grid)", BASES, panel, db,
                          weight_method="rank_ic_grid")
    finally:
        db.close()

    rows = {LABELS[name]: _row(bo) for name, bo in run["base_oof"].items()}
    rows["M-4（3基底統合）"] = _row(run["oof"])

    print(f"\n=== 売り側 OOF 指標（共通域 {run['n_common']} ペア・"
          f"weights={run['weights']}）===", flush=True)
    print("売り側spread・売り勝率は大きいほど良い / bottom分位は小さいほど良い", flush=True)
    _print_table(rows)

    # per-fold 系列（売り側 spread）でペア検定。M-4 は base_oof と同一共通域・同一 test 期。
    ss_by_period = {name: (bo.get("short_side_spread_by_period") or {})
                    for name, bo in run["base_oof"].items()}
    ss_by_period["macro_ensemble"] = run["oof"].get("short_side_spread_by_period") or {}
    ic_by_period = {name: (bo.get("rank_ic_by_period") or {})
                    for name, bo in run["base_oof"].items()}
    ic_by_period["macro_ensemble"] = run["oof"].get("rank_ic_by_period") or {}

    pairs = [
        (CANDIDATE, CURRENT),            # 主判定: M-6 − M-2（既定切替の根拠）
        ("macro_ensemble", CANDIDATE),   # M-4 − M-6（統合が単体を超えるか・ADR-0015 の作法）
        (CANDIDATE, "macro_risk_return"),
    ]
    disp = {**LABELS, "macro_ensemble": "M-4"}
    print("\n=== 売り側 spread の差の有意性（定常ブートストラップ・共通 test 期ペアリング）===",
          flush=True)
    sigs: dict[str, dict | None] = {}
    for a, b in pairs:
        sig = paired_ic_significance(ss_by_period[a], ss_by_period[b])
        sigs[f"{a}|{b}"] = sig
        print(f"  {disp[a]} − {disp[b]:<12} {_fmt_sig(sig)}", flush=True)

    print("\n=== 参考: rank-IC 差（買い側込みの総合予測力・#397 の再掲）===", flush=True)
    ic_sigs: dict[str, dict | None] = {}
    for a, b in pairs:
        sig = paired_ic_significance(ic_by_period[a], ic_by_period[b])
        ic_sigs[f"{a}|{b}"] = sig
        print(f"  {disp[a]} − {disp[b]:<12} {_fmt_sig(sig)}", flush=True)

    # ── 結論（既定切替の可否）───────────────────────────────────────────────
    main_sig = sigs[f"{CANDIDATE}|{CURRENT}"]
    cand = _row(run["base_oof"][CANDIDATE])
    curr = _row(run["base_oof"][CURRENT])
    better = (cand["short_side_spread"] or 0) > (curr["short_side_spread"] or 0)
    signif = bool(main_sig and main_sig["significant"])
    print("\n=== 結論 ===", flush=True)
    print(f"  M-6 売り側spread={cand['short_side_spread']:+.4f} vs "
          f"M-2={curr['short_side_spread']:+.4f} / bottom分位 "
          f"{cand['bottom_q_return']:+.4f} vs {curr['bottom_q_return']:+.4f}", flush=True)
    if better and signif:
        verdict = "切替推奨（M-6 が売り側でも有意に優位）"
    elif better:
        verdict = "切替は任意（M-6 が優位だが売り側では有意差なし）"
    else:
        verdict = "切替非推奨（売り側では M-6 が優位でない）"
    print(f"  → {verdict}", flush=True)
    hw_ratio = ((cand["interval_halfwidth"] / curr["interval_halfwidth"])
                if (cand["interval_halfwidth"] and curr["interval_halfwidth"]) else None)
    if hw_ratio:
        print(f"  R3 ゲート: marginal 区間半幅は M-6/M-2 = {hw_ratio:.3f} 倍"
              f"（既定 r3_gate=0.0 は無効のため挙動差なし。閾値を設ける場合の目安）", flush=True)

    payload = {
        "n_common":       run["n_common"],
        "weights":        run["weights"],
        "rows":           rows,
        "sig_short_side": sigs,
        "sig_rank_ic":    ic_sigs,
        "verdict":        verdict,
        "halfwidth_ratio_cand_over_current": hw_ratio,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUT.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {_OUT}", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
