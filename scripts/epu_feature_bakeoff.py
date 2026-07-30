"""Issue #404: EPU 2系列を `DEFAULT_MACRO_FEATURES` へ昇格すべきかの OOF 実測。

判定対象は「マクロ特徴量セットに `macro_us_epu_zscore` / `macro_us_equity_epu_zscore`
（Baker-Bloom-Davis EPU・#404 で収集追加）を**足すか否か**」であって、モデルの優劣ではない。
よって同一モデル・同一 fold・同一スナップショット設定のまま、`build_snapshots` へ渡す
`macro_names` だけを差し替えた2条件を比較する。

    base     … 現行 DEFAULT_MACRO_FEATURES（EPU なし）
    with_epu … base + EPU 2本

比較モデルは M-2（`xgb_m2`＝非線形）と M-6（`elasticnet`＝正則化線形・ADR-0021 で昇格）の
2本。**両方**を見るのは、EPU が「木の分岐として効く」のか「縮小推定下の線形項として効く」の
かで結論が変わりうるため（#372 の確定知見＝グループ共線性への縮小推定が効く）。

判定指標は買い側 rank-IC と売り側 `short_side_spread` の**両方**。ADR-0022 の確定知見
（買い側 rank-IC の順位と売り側 spread の順位は一致しない）に従い、下流既定の判断に買い側
指標だけを流用しない。有意差は共通 test 期でペアリングした定常ブートストラップ
（`model_stats.paired_ic_significance`・ADR-0018）で見る。

昇格ゲート（#372 と同じ作法）:
  検定数 = 2モデル × 2指標 = 4 → Bonferroni 補正 α = 0.05 / 4 = 0.0125。
  どれか1つでも補正後 α を下回る改善があれば既定採用、なければ選択肢のみに留める。

strict 母集団への影響（#381 の教訓）:
  M-1 は `macro_nan_ok=False`＝「選択中の全マクロが同時に非None」の行しか使わないため、
  既定へ入れる系列のカバレッジが短いと学習窓が律速される。EPU は日次1985〜のはずだが、
  **実際に本番 macro_data へ何年分入ったか**で決まるので、strict パネルのサンプル数と
  月数を base / with_epu で実測して比較する（縮んだら昇格しない）。

データは `scripts/_cache.py` 経由のローカル pickle を使う（Issue #355・本番 Egress ゼロ）。
ただし EPU は新規収集系列のためマクロだけは条件ごとに pull し直す（キーは系列名の md5）。

実行例（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）:
    python -m scripts.epu_feature_bakeoff --smoke      # 5社に1社へ間引いた短時間確認
    python -m scripts.epu_feature_bakeoff              # フル実測
    python -m scripts.epu_feature_bakeoff --refresh-cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    DEFAULT_MACRO_FEATURES,
    build_snapshots,
    preload_macro,
)
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import (  # noqa: E402
    _load_financials,
    _load_prices,
    _thin,
    run_one,
)

_OUT = Path(__file__).resolve().parent / ".cache" / "epu_feature_bakeoff.json"

EPU_FEATURES = ["macro_us_epu_zscore", "macro_us_equity_epu_zscore"]
MODELS = ["xgb_m2", "elasticnet"]
MODEL_LABELS = {"xgb_m2": "M-2(XGBoost)", "elasticnet": "M-6(ElasticNet)"}
# 昇格ゲート: 2モデル × 2指標（rank-IC / short_side_spread）の 4 検定を Bonferroni 補正。
N_TESTS = 4
ALPHA = 0.05 / N_TESTS


def _macro_cache(db, prices_by_co, macro_names: list) -> dict:
    key = hashlib.md5(",".join(sorted(macro_names)).encode()).hexdigest()[:10]
    return cached(f"bakeoff_macro_{key}", lambda: preload_macro(db, prices_by_co, macro_names))


def _build(db, args, prices_by_co, fin_by_co, companies, macro_names: list,
           macro_nan_ok: bool) -> tuple:
    """M-2 既定 config のまま macro_names だけ差し替えてスナップショットを構築する。"""
    params = coerce_params(get_plugin("macro_gbdt").params_schema(), {})
    macro_cache = _macro_cache(db, prices_by_co, macro_names)
    samples_by_ym, meta_by_ym, _current, feat_names, ids_by_ym = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache,
        params["fin_features"], macro_names,
        params["use_momentum"], params["momentum_window"], params["min_coverage"],
        build_interactions=False, macro_nan_ok=macro_nan_ok,
        price_features=list(params.get("price_features") or []),
        return_stock_ids=True,
    )
    return _thin(samples_by_ym, meta_by_ym, ids_by_ym, args.stride) + (feat_names,)


def _row(o: dict) -> dict:
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":           o["rank_ic"]["mean"],
        "rank_ic_std":       o["rank_ic"].get("std"),
        "short_side_spread": o.get("short_side_spread"),
        "short_side_hit":    o.get("short_side_hit_rate"),
        "bottom_q_return":   q[0] if q else None,
        "long_short_spread": o.get("long_short_spread"),
        "n_periods":         o.get("n_periods"),
    }


def _fmt_sig(sig: dict | None) -> str:
    if not sig:
        return "n/a (common test periods < 2)"
    star = "SIG" if (sig.get("p_value") is not None and sig["p_value"] < ALPHA) else "ns"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={sig.get('p_value', float('nan')):.3f} n={sig['n_common']} "
            f"{star}(alpha={ALPHA:.4f})")


def main() -> None:
    # Windows cp932 では非ASCII記号でクラッシュするため UTF-8 に固定
    # （[[feedback_windows_cp932_stdout_symbols]]）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Issue #404 EPU 特徴量の昇格判定")
    ap.add_argument("--smoke", action="store_true", help="サンプルを間引いた短時間確認")
    ap.add_argument("--stride", type=int, default=1, help="各月のサンプル間引き幅")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    args = ap.parse_args()
    if args.smoke and args.stride <= 1:
        args.stride = 5
    set_refresh(args.refresh_cache)

    base_names = [f for f in DEFAULT_MACRO_FEATURES if f not in EPU_FEATURES]
    conds = {"base": base_names, "with_epu": base_names + EPU_FEATURES}

    db = SessionLocal()
    try:
        prices_by_co = _load_prices(args.allow_full_pull)
        fin_by_co, companies = _load_financials(db)
        print(f"panel src: price_cos={len(prices_by_co)} fin_cos={len(fin_by_co)} "
              f"companies={len(companies)}", flush=True)

        # ── 1. strict 母集団への影響（#381 の律速チェック）───────────────────────
        strict = {}
        for cond, names in conds.items():
            s, _m, _i, feats = _build(db, args, prices_by_co, fin_by_co, companies,
                                      names, macro_nan_ok=False)
            strict[cond] = {"months": len(s), "samples": sum(len(v) for v in s.values()),
                            "n_features": len(feats)}
            print(f"[strict/{cond}] months={strict[cond]['months']} "
                  f"samples={strict[cond]['samples']} features={strict[cond]['n_features']}",
                  flush=True)

        # ── 2. 非strict パネルで M-2 / M-6 を2条件走らせる ───────────────────────
        results: dict[str, dict] = {}
        for cond, names in conds.items():
            s, m, i, feats = _build(db, args, prices_by_co, fin_by_co, companies,
                                    names, macro_nan_ok=True)
            print(f"[{cond}] months={len(s)} samples={sum(len(v) for v in s.values())} "
                  f"features={len(feats)}", flush=True)
            for model in MODELS:
                out = run_one(model, s, m, i, feats, pca=0)
                if out.get("error"):
                    print(f"  {model}: ERROR {out['error']}", flush=True)
                results[f"{cond}|{model}"] = out
                o = out["oof"]
                print(f"  {MODEL_LABELS[model]:<18} rank-IC={o['rank_ic']['mean']:+.4f} "
                      f"short_side={o.get('short_side_spread', float('nan')):+.4f} "
                      f"folds={out['n_folds']} ({out['elapsed_sec']}s)", flush=True)
        db.commit()
    finally:
        db.close()

    # ── 3. 昇格ゲート判定 ────────────────────────────────────────────────────
    print(f"\n=== with_epu - base (Bonferroni alpha={ALPHA:.4f}, {N_TESTS} tests) ===",
          flush=True)
    sigs: dict[str, dict | None] = {}
    passed: list[str] = []
    for model in MODELS:
        a = results[f"with_epu|{model}"]["oof"]
        b = results[f"base|{model}"]["oof"]
        for metric, key in (("rank_ic", "rank_ic_by_period"),
                            ("short_side_spread", "short_side_spread_by_period")):
            sig = paired_ic_significance(a.get(key) or {}, b.get(key) or {})
            sigs[f"{model}|{metric}"] = sig
            improved = sig and sig["mean"] > 0
            ok = bool(sig and sig.get("p_value") is not None
                      and sig["p_value"] < ALPHA and improved)
            if ok:
                passed.append(f"{MODEL_LABELS[model]}/{metric}")
            print(f"  {MODEL_LABELS[model]:<18} {metric:<18} {_fmt_sig(sig)}", flush=True)

    strict_ok = strict["with_epu"]["months"] >= strict["base"]["months"]
    print(f"\nstrict population: base months={strict['base']['months']} -> "
          f"with_epu={strict['with_epu']['months']} "
          f"({'OK' if strict_ok else 'SHRUNK -> do not promote'})", flush=True)

    if passed and strict_ok:
        verdict = f"promote to DEFAULT_MACRO_FEATURES (passed: {', '.join(passed)})"
    elif passed and not strict_ok:
        verdict = "keep as option only (improved but strict window shrinks)"
    else:
        verdict = "keep as option only (no significant improvement after correction)"
    print(f"\n=== VERDICT: {verdict} ===", flush=True)

    payload = {
        "alpha": ALPHA,
        "n_tests": N_TESTS,
        "strict_population": strict,
        "rows": {k: _row(v["oof"]) for k, v in results.items() if not v.get("error")},
        "significance": sigs,
        "verdict": verdict,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUT.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"wrote {_OUT}", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
