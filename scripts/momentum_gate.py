"""モメンタム特徴量（`use_momentum`）を既定 ON にすべきかの OOF 実測（昇格ゲート）。

`use_momentum` は 2026-06-20 に既定 OFF で導入された。理由は2つある:

  (a) **データ制約**: 52週先リターン（未来が必要）と 12ヶ月モメンタム（過去が必要）を同時に
      要求すると、週次株価が約2年分しかない環境では両条件を満たす月が約1ヶ月の薄帯へ収縮し、
      walk-forward CV が 0 fold になった（MODELS.md §9.8-4）。
  (b) **保守ゲート**: `px_*` / `monotone` / `sector` と同じく「既定 OFF で入れ、OOF の ON/OFF
      実測で有効性を示してから既定化」する慣行（ADR-0019）。

(a) は #198 の Yahoo バックフィルで解けている（2026-08-31 実測: `stock_price_weekly` は
2019-07-29〜・1,306,610 行・4,024 社。104週以上の履歴を持つ社が 3,686＝92%）。一方 (b) は
**未消化**で、ON/OFF の実測は一度も取られていない——ADR-0021（M-6 昇格）も ADR-0022（既定
mu_source を M-6 へ）も、実測条件はすべて `use_momentum=False` だった。本スクリプトはその
欠けている実測を埋める。

判定対象は「特徴量セットにモメンタムを**足すか否か**」であってモデルの優劣ではない。よって
同一モデル・同一 fold・同一パネル入力のまま `build_snapshots` へ渡す `use_momentum` だけを
差し替えた2条件を比較する（`scripts/macro_feature_bakeoff.py` と同型。あちらが差し替えるのは
`macro_names`）。

    off … use_momentum=False（現行既定・M-2/M-6 の本番構成）
    on  … use_momentum=True, momentum_window=12（12-1 モメンタム・Jegadeesh-Titman 1993）

**母集団が動く点が macro_feature_bakeoff との決定的な違い**（本スクリプト固有の設計）:
マクロ系列の追加は M-2/M-6 が `macro_nan_ok=True` なので母集団を動かさないが、モメンタムは
`macro_snapshots._build_snapshots_impl` の `if mom is None: continue` が**行ごと落とす**ため、
ON では履歴不足の社・月が母集団から消える。さらに `min_coverage` の充足率が `c/n → (c+1)/(n+1)`
へ変わって ON 側がわずかに緩くなるので、**厳密な部分集合ですらない**。母集団差を交絡させたまま
測ると「モメンタムの効果」と「母集団が変わった効果」が分離できない（#454 の列順交絡と同型）。
そこで:

  1. 両条件のパネル規模（月数 / サンプル数 / 社数 / 特徴量数）を必ず出す
  2. **主判定は共通 (ym, ec) 域へ制限した OOF**（ADR-0015 の base-on-common と同じ発想）
  3. 生母集団のままの値も併記する（本番の運用形に対応するため）

測る手続きは書き直さない（ADR-0041 の教訓＝書き直すと本番と別物を測る）。walk-forward →
`oof_backtest` は `scripts/candidate_bakeoff.run_one`、有意差は `model_stats.
paired_ic_significance`（ADR-0018 の定常ブートストラップ）、(ym,ec) 突合は
`plugins.macro_ensemble._align` をそのまま使う。

昇格ゲート（#372 / ADR-0023 と同じ作法）:
  検定数 = 2モデル（M-2 / M-6） × 2指標（買い側 rank-IC / 売り側 short_side_spread）= 4
  → Bonferroni 補正 α = 0.05 / 4 = 0.0125。共通域の判定でどれか1つでも補正後 α を下回る
  **改善**があれば既定 ON、なければ選択肢のまま（棄却を ADR へ記録する）。
  買い側だけで決めない理由は ADR-0022 の確定知見（買い側 rank-IC の順位と売り側 spread の
  順位は一致しない）。

データは `scripts/_cache.py` 経由のローカル pickle を使う。**モメンタムは過去履歴を読む特徴量
なので、週次株価キャッシュが旧世代だと ON 条件だけが不当に不利になり判定そのものが壊れる**
（#456 と同型。mtime は当てにならない）。起点日・行数・社数を実行時に必ず印字する。

実行例（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）:
    python -m scripts.momentum_gate --smoke                    # 5社に1社へ間引いた経路確認
    python -m scripts.momentum_gate                            # フル実測
    python -m scripts.momentum_gate --models elasticnet        # M-6 だけ
    python -m scripts.momentum_gate --json scripts/.cache/momentum_gate.json

出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_ensemble import _align  # noqa: E402
from plugins.macro_snapshots import build_snapshots, oof_backtest, preload_macro  # noqa: E402
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import (  # noqa: E402
    _load_financials,
    _load_prices,
    _thin,
    run_one,
)

_OUT_DIR = Path(__file__).resolve().parent / ".cache"

# 12-1 モメンタムの標準形（Jegadeesh-Titman 1993）。窓の最適化は本ゲートの対象外＝
# まず「入れるか否か」を clean に判定する（窓も同時に振ると検定数が5倍になり、
# 「窓を選んだこと」自体が過剰適合になりうる）。通過後に別途 tuning_search_space で探索する。
MOM_WINDOW = 12

CONDS: dict[str, bool] = {"off": False, "on": True}
MODELS = ["xgb_m2", "elasticnet"]
MODEL_LABELS = {"xgb_m2": "M-2(XGBoost)", "elasticnet": "M-6(ElasticNet)"}
METRICS = (("rank_ic", "rank_ic_by_period"),
           ("short_side_spread", "short_side_spread_by_period"))
# 昇格ゲート: 2モデル × 2指標 = 4 検定を Bonferroni 補正。
N_TESTS = 4
ALPHA = 0.05 / N_TESTS


def _num(v, nd: int = 4) -> str:
    """None 安全な数値整形（欠測は '-'）。cp932 で落ちる記号は使わない。"""
    if v is None:
        return "-"
    return f"{v:+.{nd}f}" if isinstance(v, float) else str(v)


def _restrict_months(panel: tuple, yms: set) -> tuple:
    """パネル（samples / meta / ids / feats）を指定した月集合へ制限する。

    2条件で fold の位相を揃えるために使う（`walk_forward_cv_monthly` の test 月は月リストの
    先頭からの相対位置で決まるため、開始月が違うと 3ヶ月周期の別位相になる）。
    """
    s, m, i, feats = panel
    return ({ym: v for ym, v in s.items() if ym in yms},
            {ym: v for ym, v in m.items() if ym in yms},
            {ym: v for ym, v in i.items() if ym in yms},
            feats)


def _panel_stats(samples_by_ym: dict, ids_by_ym: dict, feats: list) -> dict:
    cos = {ec for ids in ids_by_ym.values() for ec in ids}
    return {
        "months": len(samples_by_ym),
        "samples": sum(len(v) for v in samples_by_ym.values()),
        "companies": len(cos),
        "n_features": len(feats),
        "first_ym": min(samples_by_ym) if samples_by_ym else None,
        "last_ym": max(samples_by_ym) if samples_by_ym else None,
    }


def _restrict(resid_by_ym: dict, oof_meta: dict, ids_by_ym: dict, keys: set) -> tuple:
    """residuals / meta を共通 (ym, ec) 集合へ制限して同順で組み直す。

    `build_snapshots(return_stock_ids=True)` と `walk_forward_cv_monthly(return_residuals=True)`
    は samples_by_ym[ym] のサンプル順を保存する（`_align` / `build_oof_meta` が依拠する既存
    契約）ため index で 1:1 突合できる。keys は `_align` が作った集合で NaN 行を含まないので、
    NaN は自動的に落ちる。
    """
    r2: dict[str, list] = {}
    m2: dict[str, list] = {}
    for ym, pairs in resid_by_ym.items():
        ids = ids_by_ym.get(ym, [])
        metas = oof_meta.get(ym, [])
        rr, mm = [], []
        for j, (yh, y) in enumerate(pairs):
            if j >= len(ids) or (ym, ids[j]) not in keys:
                continue
            rr.append((yh, y))
            mm.append(metas[j] if j < len(metas) else (ids[j], None))
        if rr:
            r2[ym] = rr
            m2[ym] = mm
    return r2, m2


def _row(o: dict) -> dict:
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":           o["rank_ic"]["mean"],
        "rank_ic_std":       o["rank_ic"].get("std"),
        "rank_ic_neutral":   o.get("rank_ic_industry_neutral"),
        "short_side_spread": o.get("short_side_spread"),
        "short_side_hit":    o.get("short_side_hit_rate"),
        "bottom_q_return":   q[0] if q else None,
        "long_short_spread": o.get("long_short_spread"),
        "turnover":          o.get("effective_turnover"),
        "breakeven_bps":     o.get("breakeven_cost_bps"),
        "n_periods":         o.get("n_periods"),
    }


def _fmt_sig(sig: dict | None) -> str:
    if not sig:
        return "n/a (common test periods < 2)"
    p = sig.get("p_value")
    star = "SIG" if (p is not None and p < ALPHA) else "ns"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={p if p is not None else float('nan'):.3f} n={sig['n_common']} "
            f"{star}(alpha={ALPHA:.4f})")


def _build(db, args, prices_by_co, fin_by_co, companies, macro_cache,
           macro_names: list, use_momentum: bool) -> tuple:
    """M-2 既定 config のまま use_momentum だけ差し替えてスナップショットを構築する。"""
    params = coerce_params(get_plugin("macro_gbdt").params_schema(), {})
    samples_by_ym, meta_by_ym, _current, feats, ids_by_ym = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache,
        params["fin_features"], macro_names,
        use_momentum, MOM_WINDOW, params["min_coverage"],
        build_interactions=False, macro_nan_ok=True,
        price_features=list(params.get("price_features") or []),
        return_stock_ids=True,
    )
    return _thin(samples_by_ym, meta_by_ym, ids_by_ym, args.stride) + (feats,)


def main() -> None:
    # Windows cp932 では非ASCII記号でクラッシュするため UTF-8 に固定
    # （[[feedback_windows_cp932_stdout_symbols]]）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="モメンタム特徴量（use_momentum）の既定 ON/OFF 昇格ゲート実測")
    ap.add_argument("--models", help="測るモデルをカンマ区切りで指定（既定: xgb_m2,elasticnet）")
    ap.add_argument("--smoke", action="store_true", help="サンプルを間引いた短時間確認")
    ap.add_argument("--stride", type=int, default=1, help="各月のサンプル間引き幅")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に DB からのフルロードを許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--json", dest="json_path", help="結果 JSON の出力先")
    args = ap.parse_args()
    if args.smoke and args.stride <= 1:
        args.stride = 5
    set_refresh(args.refresh_cache)

    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else list(MODELS))
    unknown = [m for m in models if m not in MODEL_LABELS]
    if unknown:
        raise SystemExit(f"未知のモデル: {', '.join(unknown)}（{', '.join(MODELS)} のみ）")
    n_tests = len(models) * len(METRICS)
    if n_tests != N_TESTS:
        print(f"[warn] 検定数が {n_tests}（既定 {N_TESTS}）です。表示している alpha は "
              f"{N_TESTS} 検定前提の {ALPHA:.4f} のままなので、判定はこの値で読むこと。",
              flush=True)
    out_path = Path(args.json_path) if args.json_path else _OUT_DIR / "momentum_gate.json"

    db = SessionLocal()
    try:
        prices_by_co = _load_prices(args.allow_full_pull)
        # モメンタムは過去履歴を読む。キャッシュが旧世代だと ON 側だけが不当に不利になり
        # 判定が壊れるため（#456 と同型）、起点・終端・規模をここで必ず現す。
        first = min((r.trade_date for rows in prices_by_co.values() for r in rows[:1]),
                    default=None)
        last = max((rows[-1].trade_date for rows in prices_by_co.values() if rows),
                   default=None)
        n_rows = sum(len(r) for r in prices_by_co.values())
        print(f"weekly px cache: cos={len(prices_by_co)} rows={n_rows} "
              f"range={first}..{last}", flush=True)

        fin_by_co, companies = _load_financials(db)
        print(f"panel src: fin_cos={len(fin_by_co)} companies={len(companies)}", flush=True)

        params = coerce_params(get_plugin("macro_gbdt").params_schema(), {})
        macro_names = list(params["macro_features"]) if params["use_macro"] else []
        mkey = hashlib.md5(",".join(sorted(macro_names)).encode()).hexdigest()[:10]
        macro_cache = (cached(f"bakeoff_macro_{mkey}",
                              lambda: preload_macro(db, prices_by_co, macro_names))
                       if macro_names else {})
        db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない（#411）

        # ── 1. 2条件でパネルを構築し、母集団の差をそのまま現す ──────────────────
        panels: dict[str, tuple] = {}
        stats: dict[str, dict] = {}
        for cond, use_mom in CONDS.items():
            s, m, i, feats = _build(db, args, prices_by_co, fin_by_co, companies,
                                    macro_cache, macro_names, use_mom)
            panels[cond] = (s, m, i, feats)
            stats[cond] = _panel_stats(s, i, feats)
            st = stats[cond]
            print(f"[{cond}] months={st['months']} ({st['first_ym']}..{st['last_ym']}) "
                  f"samples={st['samples']} companies={st['companies']} "
                  f"features={st['n_features']}", flush=True)

        # ── 2. 各条件 × 各モデルを走らせる（残差も受け取る）────────────────────
        results: dict[str, dict] = {}
        parts: dict[str, tuple] = {}
        for cond in CONDS:
            s, m, i, feats = panels[cond]
            for model in models:
                out = run_one(model, s, m, i, feats, pca=0, return_parts=True)
                parts[f"{cond}|{model}"] = out.pop("_parts")
                if out.get("error"):
                    print(f"  {model}: ERROR {out['error']}", flush=True)
                results[f"{cond}|{model}"] = out
                o = out["oof"]
                print(f"  [{cond}] {MODEL_LABELS[model]:<18} "
                      f"rank-IC={_num(o['rank_ic']['mean'])} "
                      f"(std={_num(o['rank_ic'].get('std'))}) "
                      f"short_side={_num(o.get('short_side_spread'))} "
                      f"folds={out['n_folds']} ({out['elapsed_sec']}s)", flush=True)
    finally:
        db.close()

    # ── 3. 共通月で走らせ直し、さらに共通 (ym,ec) 域へ制限する（主判定）────────
    #
    # **月を揃えないと fold の位相がずれて共通域が空になる**（初回実測で実際に踏んだ）。
    # `walk_forward_cv_monthly` は月リストの先頭から min_train_months+embargo_months を空けて
    # step_months=3 刻みで test 月を選ぶため、パネルの開始月が1ヶ月でも違うと test 月が
    # 3ヶ月周期の別位相になり **一度も一致しない**（実測: off=2019-12 起点/69ヶ月 と
    # on=2020-07 起点/62ヶ月 で共通 (ym,ec) が 0 件）。よって共通月へ制限したパネルで
    # 走らせ直す。ここまでが「fold を揃える」段で、そのあと同一 fold の中で銘柄集合を
    # 揃えるのが (ym,ec) 制限の段になる。
    common_yms = set(panels["off"][0]) & set(panels["on"][0])
    print(f"\n=== common months: {len(common_yms)} "
          f"({min(common_yms, default='-')}..{max(common_yms, default='-')}) ===", flush=True)
    cpanels = {cond: _restrict_months(panels[cond], common_yms) for cond in CONDS}
    cparts: dict[str, tuple] = {}
    cruns: dict[str, dict] = {}
    for cond in CONDS:
        s, m, i, feats = cpanels[cond]
        st = _panel_stats(s, i, feats)
        print(f"[{cond}/common-months] months={st['months']} samples={st['samples']} "
              f"companies={st['companies']} features={st['n_features']}", flush=True)
        for model in models:
            out = run_one(model, s, m, i, feats, pca=0, return_parts=True)
            cparts[f"{cond}|{model}"] = out.pop("_parts")
            cruns[f"{cond}|{model}"] = out
            if out.get("error"):
                print(f"  {model}: ERROR {out['error']}", flush=True)

    print("\n=== common (ym,ec) restriction ===", flush=True)
    common_results: dict[str, dict] = {}
    common_info: dict[str, dict] = {}
    for model in models:
        aligned = {cond: _align(cparts[f"{cond}|{model}"][0], cpanels[cond][2])
                   for cond in CONDS}
        keys = set(aligned["off"]) & set(aligned["on"])
        folds = {c: cruns[f"{c}|{model}"]["n_folds"] for c in CONDS}
        common_info[model] = {
            "n_common": len(keys),
            "n_off": len(aligned["off"]),
            "n_on": len(aligned["on"]),
            "n_folds": folds,
        }
        print(f"  {MODEL_LABELS[model]:<18} common={len(keys)} "
              f"(off={len(aligned['off'])} on={len(aligned['on'])}) folds={folds}", flush=True)
        if folds["off"] != folds["on"]:
            print("    [warn] fold 数が一致していません（位相が揃っていない可能性）", flush=True)
        for cond in CONDS:
            resid, meta = cparts[f"{cond}|{model}"]
            r2, m2 = _restrict(resid, meta, cpanels[cond][2], keys)
            bt = oof_backtest(r2, n_quantiles=5, meta_by_ym=m2, rebalance_per_year=4)
            common_results[f"{cond}|{model}"] = bt
            print(f"    [{cond}] rank-IC={_num(bt['rank_ic']['mean'])} "
                  f"(std={_num(bt['rank_ic'].get('std'))}) "
                  f"short_side={_num(bt.get('short_side_spread'))} "
                  f"periods={bt.get('n_periods')}", flush=True)

    # ── 4. 昇格ゲート判定 ──────────────────────────────────────────────────
    # 検定は common スコープだけで行う。**raw では差を検定できない**——条件ごとに
    # パネルの開始月が違うと `walk_forward_cv_monthly` の test 月が3ヶ月周期の別位相になり、
    # `paired_ic_significance` がペアリングできる共通 test 期が 0 になる（実測で 4検定すべて
    # "common test periods < 2"）。raw は各条件の**水準**としてだけ意味を持つので併記する。
    sigs: dict[str, dict] = {}
    passed: list[str] = []
    regressed: list[str] = []
    print(f"\n=== on - off / common [PRIMARY] "
          f"(Bonferroni alpha={ALPHA:.4f}, {N_TESTS} tests) ===", flush=True)
    for model in models:
        a = common_results[f"on|{model}"]
        b = common_results[f"off|{model}"]
        for metric, key in METRICS:
            sig = paired_ic_significance(a.get(key) or {}, b.get(key) or {})
            sigs[f"common|{model}|{metric}"] = sig
            p = sig.get("p_value") if sig else None
            hit = bool(sig and p is not None and p < ALPHA)
            if hit and sig["mean"] > 0:
                passed.append(f"{MODEL_LABELS[model]}/{metric}")
            elif hit:
                regressed.append(f"{MODEL_LABELS[model]}/{metric}")
            print(f"  {MODEL_LABELS[model]:<18} {metric:<18} {_fmt_sig(sig)}", flush=True)

    print("\n=== raw levels (each condition's own population; NOT testable across "
          "conditions: fold phases differ) ===", flush=True)
    print(f"  {'cond':<5} {'model':<18} {'rank-IC':>9} {'IC std':>9} {'short':>9} "
          f"{'LS spread':>10} {'folds':>6}", flush=True)
    for cond in CONDS:
        for model in models:
            r = results[f"{cond}|{model}"]
            o = r["oof"]
            print(f"  {cond:<5} {MODEL_LABELS[model]:<18} {_num(o['rank_ic']['mean']):>9} "
                  f"{_num(o['rank_ic'].get('std')):>9} "
                  f"{_num(o.get('short_side_spread')):>9} "
                  f"{_num(o.get('long_short_spread')):>10} {r['n_folds']:>6}", flush=True)

    if passed:
        verdict = "PROMOTE (default use_momentum=True): " + ", ".join(passed)
    elif regressed:
        verdict = ("REJECT (keep default use_momentum=False): no improvement passed "
                   "corrected alpha; significantly WORSE on " + ", ".join(regressed))
    else:
        verdict = "REJECT (keep default use_momentum=False): no metric passed corrected alpha"
    print(f"\n=== verdict === {verdict}", flush=True)
    print("判定は common スコープで読む（同一 fold・同一 (ym,ec) 域）。raw は水準のみ。",
          flush=True)

    payload = {
        "momentum_window": MOM_WINDOW,
        "alpha": ALPHA,
        "n_tests": N_TESTS,
        "models": models,
        "stride": args.stride,
        "panel": stats,
        "common_months": {"n": len(common_yms),
                          "first": min(common_yms, default=None),
                          "last": max(common_yms, default=None)},
        "common_info": common_info,
        "raw": {k: _row(v["oof"]) for k, v in results.items()},
        "common": {k: _row(v) for k, v in common_results.items()},
        "n_folds": {k: v["n_folds"] for k, v in results.items()},
        "n_folds_common_months": {k: v["n_folds"] for k, v in cruns.items()},
        "significance": sigs,
        "passed": passed,
        "regressed": regressed,
        "verdict": verdict,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
