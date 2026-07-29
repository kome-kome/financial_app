"""Issue #397: M-4（兄弟μ̂スタッキング）の基底 2 vs 3 の OOF 横並び実測。

`BASE_MODELS` を差し替えて M-4 を2回走らせ、**同一の honest 前提**（embargo=12・
walk-forward・ADR-0014）で以下を比較する:

  - M-4(M-1+M-2)     … #367/ADR-0015 の従来構成
  - M-4(M-1+M-2+M-6) … #397 の提案構成（M-6 = ElasticNet・ADR-0021 で M-2 を有意に上回った）

判定は ADR-0015 の **base-on-common**（同一共通 (ym,ec) 域に制限した各基底の OOF）で行う。
基底が増えると intersection は狭まりうるため、2基底構成と3基底構成では母集団が異なる。
そのため「3基底 M-4 vs 3基底共通域での各基底」を主判定に、「2基底 M-4 vs 3基底 M-4」は
参考値（母集団差を含む）として併記する。有意差は ADR-0018 の定常ブートストラップ
（`model_stats.paired_ic_significance`・共通 test 期でペアリング）で見る。

データは `scripts/_cache.py` 経由でローカル pickle を読む（Issue #355・本番 Egress ゼロ）。
producer 永続化は `tuning_dry_run()` で no-op、現在μ̂スコアリングは `tuning_objective_only()`
で省略する（本番 `macro_ensemble_scores` を実測で汚さない）。

実行:
    python -m scripts.ensemble_base_bakeoff                 # 実測（キャッシュ利用）
    python -m scripts.ensemble_base_bakeoff --smoke         # 間引きパネルで動作確認
    python -m scripts.ensemble_base_bakeoff --refresh-cache # 本番から取り直す
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, tuning_dry_run, tuning_objective_only  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin, macro_ensemble  # noqa: E402
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import _load_financials, _load_prices  # noqa: E402

_OUT = Path(__file__).resolve().parent / ".cache" / "ensemble_base_bakeoff.json"

# label -> (BASE_MODELS, weight_method)。weight_method も比較軸に入れるのは、二段目の NNLS が
# MSE 最小化で評価指標（rank-IC）と目的が食い違うため（#397 実測で M-6 の重みが 0 に落ちた）。
CONFIGS = {
    "M-4(M-1+M-2)":          (["macro_risk_return", "macro_gbdt"], "nnls"),
    "M-4(M-1+M-2+M-6)":      (["macro_risk_return", "macro_gbdt", "macro_enet"], "nnls"),
    "M-4(3基底・rank_ic)":   (["macro_risk_return", "macro_gbdt", "macro_enet"], "rank_ic_grid"),
}


def _thin_prices(prices_by_co: dict, keep: int) -> dict:
    """--smoke 用に対象企業を先頭 keep 社へ絞る（パネル全体が軽くなる）。"""
    return {ec: rows for ec, rows in list(prices_by_co.items())[:keep]}


def _load_panel(args) -> tuple[dict, dict, dict, dict]:
    """prices/fin/companies/macro をキャッシュから組み立てて返す（本番 pull なし）。"""
    import hashlib

    from plugins.macro_snapshots import preload_macro

    db = SessionLocal()
    try:
        prices_by_co = _load_prices(args.allow_full_pull)
        if args.smoke:
            prices_by_co = _thin_prices(prices_by_co, args.smoke_companies)
        fin_by_co, companies = _load_financials(db)
        params = coerce_params(get_plugin("macro_gbdt").params_schema(), {})
        macro_names = list(params["macro_features"]) if params["use_macro"] else []
        mkey = hashlib.md5(",".join(sorted(macro_names)).encode()).hexdigest()[:10]
        macro_cache = (cached(f"bakeoff_macro_{mkey}",
                              lambda: preload_macro(db, prices_by_co, macro_names))
                       if macro_names else {})
        db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない
    finally:
        db.close()
    print(f"panel: price_cos={len(prices_by_co)} fin_cos={len(fin_by_co)} "
          f"companies={len(companies)} macro={len(macro_cache)}", flush=True)
    return prices_by_co, fin_by_co, companies, macro_cache


def _run_config(label: str, bases: list, panel: tuple, db,
                weight_method: str = "nnls") -> dict:
    """BASE_MODELS を bases に差し替えて M-4 を1回実行し、OOF 指標を返す。"""
    prices_by_co, fin_by_co, companies, macro_cache = panel
    plugin = macro_ensemble.plugin
    params = coerce_params(plugin.params_schema(), {"weight_method": weight_method})
    saved = list(macro_ensemble.BASE_MODELS)
    macro_ensemble.BASE_MODELS[:] = bases
    t0 = time.time()
    try:
        with patch("plugins.macro_ensemble.load_data",
                   return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_ensemble.preload_macro", return_value=macro_cache), \
             patch("plugins.macro_ensemble.get_producer_scores", return_value={}), \
             tuning_dry_run(), tuning_objective_only():
            res = plugin.execute(params, db)
    finally:
        macro_ensemble.BASE_MODELS[:] = saved
    secs = round(time.time() - t0, 1)
    oof = res["oof_backtest"]
    print(f"[{label}] rank-IC={oof['rank_ic']['mean']:+.4f} "
          f"periods={oof['n_periods']} pairs={res['n_common_pairs']} "
          f"weights={res['weights']} ({secs}s)", flush=True)
    return {
        "label":        label,
        "bases":        list(bases),
        "seconds":      secs,
        "oof":          oof,
        "base_oof":     res["base_oof_backtest"],
        "weights":      res["weights"],
        "n_common":     res["n_common_pairs"],
    }


def _ic_by_period(oof: dict) -> dict:
    """oof_backtest の per-fold IC（{test_ym: ic}）。paired_ic_significance の入力形。"""
    return oof.get("rank_ic_by_period") or {}


def _fmt_sig(sig: dict | None) -> str:
    if not sig:
        return "n/a（共通 test 期 < 2）"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={sig.get('p_value', float('nan')):.3f} n={sig['n_common']} "
            f"{'有意' if sig['significant'] else '非有意'}")


def main() -> None:
    # Windows の既定 cp932 では μ̂（結合文字 U+0302）等が出力できずクラッシュするため、
    # リダイレクト時も含めて UTF-8 に固定する（event_study_multivariate_xgboost.py と同流儀）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Issue #397 M-4 基底 2 vs 3 の OOF 実測")
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--only", nargs="*", default=None,
                    help=f"実行する構成ラベルを絞る（既定=全構成）。候補: {list(CONFIGS)}")
    args = ap.parse_args()
    set_refresh(args.refresh_cache)

    panel = _load_panel(args)
    db = SessionLocal()
    runs = {}
    try:
        for label, (bases, wm) in CONFIGS.items():
            if args.only and label not in args.only:
                continue
            runs[label] = _run_config(label, bases, panel, db, weight_method=wm)
    finally:
        db.close()

    if len(runs) < len(CONFIGS):
        print(f"\n（--only 指定のため {len(runs)}/{len(CONFIGS)} 構成のみ実行）", flush=True)
        _dump(runs, {}, None)
        return

    three = runs["M-4(M-1+M-2+M-6)"]
    two = runs["M-4(M-1+M-2)"]

    print("\n=== 主判定: 3基底 M-4 vs 同一共通域の各基底（ADR-0015 base-on-common）===",
          flush=True)
    rows = [("M-4(3基底・統合)", three["oof"])]
    rows += [(f"  基底 {name}", bo) for name, bo in three["base_oof"].items()]
    for name, o in rows:
        print(f"{name:34} rank-IC={o['rank_ic']['mean']:+.4f} "
              f"IC std={o['rank_ic'].get('std', float('nan')):.4f} "
              f"LS={o.get('long_short_spread', float('nan')):+.4f} "
              f"periods={o['n_periods']}", flush=True)
    sigs = {name: paired_ic_significance(_ic_by_period(three["oof"]), _ic_by_period(bo))
            for name, bo in three["base_oof"].items()}
    for name, sig in sigs.items():
        print(f"  M-4(3基底) − {name:20} {_fmt_sig(sig)}", flush=True)

    print("\n=== 参考: 2基底 M-4 vs 3基底 M-4（母集団差を含む）===", flush=True)
    for r in (two, three):
        print(f"{r['label']:20} rank-IC={r['oof']['rank_ic']['mean']:+.4f} "
              f"pairs={r['n_common']} weights={r['weights']}", flush=True)
    sig23 = paired_ic_significance(_ic_by_period(three["oof"]), _ic_by_period(two["oof"]))
    print(f"  3基底 − 2基底: {_fmt_sig(sig23)}", flush=True)

    _dump(runs, sigs, sig23)


def _dump(runs: dict, sigs: dict, sig23: dict | None) -> None:
    """実測結果を JSON へ保存する。

    `--only` の部分実行では既存結果とマージする（先に走らせた構成の実測を消さない）。
    検定結果は全構成を回したときだけ更新する。
    """
    prev: dict = {}
    if _OUT.exists():
        try:
            prev = json.loads(_OUT.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    payload = {
        "runs": {**(prev.get("runs") or {}), **runs},
        "sig_vs_bases": sigs or prev.get("sig_vs_bases") or {},
        "sig_three_minus_two": sig23 if sig23 is not None else prev.get("sig_three_minus_two"),
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUT.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {_OUT}", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
