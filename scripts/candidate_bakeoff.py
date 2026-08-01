"""兄弟モデル候補の OOF 横並び実測（Issue #372・探索枠の bake-off ランナー）。

`plugins/model_candidates.py` の候補（ElasticNet / ExtraTrees / Fama-MacBeth /
regime-switch 閾値線形 / LightGBM / CatBoost）と基準線（M-2 の XGBoost・素 OLS）を、
**同一スナップショット・同一 fold・同一指標**で走らせて OOF rank-IC を並べる。

    build_snapshots（M-2 既定 config を1回だけ構築）
      → walk_forward_cv_monthly(embargo_months=12, fit_predict=候補)
      → oof_backtest（rank-IC / 業種中立IC / ロングショート spread / hit-rate / breakeven bps）

M-2 既定 config は `MacroGbdtPlugin.params_schema()` を `coerce_params({})` して取り出すため、
`model_comparison`（POST /api/backtest/model-comparison）が M-2 を走らせるときと同じ設定になる
＝ここで出る xgb_m2 の値は本番 M-2 の OOF と直接比較できる。

**低 Egress 設計**（[[feedback_verification_fullloads_exhaust_egress]]）:
重い stock_price_weekly（約97万行）は `scripts/.cache/weekly_prices_close.pkl`（Issue #355）を
再利用し pull しない。小さい financial_metrics / companies / macro_data のみ本番から1回 pull し、
その1回のロードで全候補を評価する（候補を増やしても Egress は増えない）。

実行例（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）:
    python -m scripts.candidate_bakeoff --list
    python -m scripts.candidate_bakeoff --smoke                     # 5社に1社へ間引いた短時間確認
    python -m scripts.candidate_bakeoff                             # 全候補フル実測
    python -m scripts.candidate_bakeoff --candidates elasticnet --pca 5
    python -m scripts.candidate_bakeoff --json scripts/.cache/bakeoff.json

出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import defaultdict, namedtuple

import database
from database import Company, FinancialMetric, SessionLocal
from plugins import get_plugin
from plugins.model_candidates import (
    CANDIDATES,
    build_candidate,
    candidate_available,
    pca_feature_names,
    summarize_diag,
)
from plugins.macro_snapshots import (
    FIN_BASE_OPTIONS,
    LABEL_HORIZON_MONTHS,
    build_oof_meta,
    build_snapshots,
    oof_backtest,
    preload_macro,
)
from plugins.utils import coerce_params, walk_forward_cv_monthly
import plugins.macro_snapshots as ms
from scripts._cache import cached, set_refresh

# M-2 と同一の walk-forward 設定（macro_gbdt.execute と同じ値・比較の公平性のため固定）
MIN_TRAIN_MONTHS = 6
STEP_MONTHS = 3

# 既定で回す候補（+ 素 OLS 基準線は常に付く）
DEFAULT_ORDER = ["xgb_m2", "elasticnet", "extratrees", "fama_macbeth",
                 "fama_macbeth_ridge", "regime_linear", "lightgbm", "catboost"]

# ── 小テーブルのローカルキャッシュ（Issue #355 の Egress 恒久対策を本スクリプトにも適用）──
# build_snapshots が財務レコードに要求する属性だけを軽量 namedtuple で持つ（ORM を pickle
# しない）。fin_features は M-2 既定＝FIN_BASE_OPTIONS 全選択なので、全候補列を収録する。
#
# **pickle には素の tuple だけを載せる**（namedtuple クラス自体は載せない）。本スクリプトは
# `python -m scripts.candidate_bakeoff` で走るためモジュール名が `__main__` になり、namedtuple を
# そのまま pickle すると `__main__._FinRow` として記録されて、他モジュールから import した
# ときに `AttributeError: Can't get attribute '_FinRow'` で読めなくなる（実際に踏んだ）。
# 読み出し後に `_FinRow(*vals)` へ復元することで、キャッシュを誰からでも共有できる。
_FIN_FIELDS = tuple(o["value"] for o in FIN_BASE_OPTIONS) + (
    "industry", "sec_code", "company_name", "bs_total_assets", "period_end")
_FinRow = namedtuple("_FinRow", _FIN_FIELDS)
_CompanyRow = namedtuple("_CompanyRow", ("edinet_code", "sec_code", "name", "industry"))
# キャッシュ形式を素 tuple へ変えたためキーを更新する（旧 pickle は読めないので作り直す）。
_FIN_CACHE_KEY = "bakeoff_fin_metrics_v2"
_CO_CACHE_KEY = "bakeoff_companies_v2"


def _load_prices(allow_full_pull: bool) -> dict:
    """weekly_prices_close キャッシュを build_snapshots が食える形へ変換して返す。"""
    def _pull_full():
        if not allow_full_pull:
            raise SystemExit(
                "weekly_prices_close キャッシュがありません。97万行の pull はストールしやすいため"
                "既定で拒否します。--allow-full-pull を付けるか、先に "
                "python -m scripts.measure_embargo_impact 等でキャッシュを作ってください。")
        from database import StockPriceWeekly
        db = SessionLocal()
        try:
            out: dict[str, list] = defaultdict(list)
            for ec, ws, cl in (db.query(StockPriceWeekly.edinet_code,
                                        StockPriceWeekly.week_start,
                                        StockPriceWeekly.close_last).all()):
                out[ec].append((ws, cl))
            # SELECT だけでもトランザクションは開く。pooler(Supavisor) 経由では close() 後も
            # セッションが再利用待ちで残り、"idle in transaction" のまま読取ロックを掴み続けて
            # `ALTER TABLE companies ...`（init_db の冪等マイグレーション）を
            # AccessExclusiveLock 待ちで statement_timeout(2min) まで殺す（#411 で実害・
            # Render 起動と収集ワークフローが同時に詰まった）。読み終えたら必ず閉じる。
            db.commit()
            import pandas as pd
            return {ec: pd.DataFrame(rows, columns=["week_start", "close_last"])
                          .sort_values("week_start")
                    for ec, rows in out.items()}
        finally:
            db.close()

    px_df = cached("weekly_prices_close", _pull_full)
    prices_by_co: dict[str, list] = {}
    for ec, df in px_df.items():
        rows = [ms._WEEKLY_PX(str(ws), float(cl), None)
                for ws, cl in zip(df["week_start"].tolist(), df["close_last"].tolist())]
        rows.sort(key=lambda r: r.trade_date)
        prices_by_co[ec] = rows
    return prices_by_co


def _thin(samples_by_ym: dict, meta_by_ym: dict, ids_by_ym: dict, stride: int) -> tuple:
    """--smoke 用に各月のサンプルを stride 件おきへ間引く（3系列の並び順対応を保つ）。"""
    if stride <= 1:
        return samples_by_ym, meta_by_ym, ids_by_ym
    s2, m2, i2 = {}, {}, {}
    for ym, rows in samples_by_ym.items():
        idxs = list(range(0, len(rows), stride))
        s2[ym] = [rows[i] for i in idxs]
        m2[ym] = [meta_by_ym.get(ym, [])[i] for i in idxs if i < len(meta_by_ym.get(ym, []))]
        i2[ym] = [ids_by_ym.get(ym, [])[i] for i in idxs if i < len(ids_by_ym.get(ym, []))]
    return s2, m2, i2


def _load_financials(db) -> tuple[dict, dict]:
    """financial_metrics / companies を軽量 namedtuple でキャッシュして返す。

    2回目以降の実行は本番 Supabase を一切叩かない（`--refresh-cache` で再取得）。
    """
    def _pull_fin():
        out: dict[str, list] = defaultdict(list)
        for r in (db.query(FinancialMetric)
                  .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end).all()):
            out[r.edinet_code].append(tuple(getattr(r, f, None) for f in _FIN_FIELDS))
        return dict(out)

    def _pull_companies():
        return {c.edinet_code: (c.edinet_code, c.sec_code, c.name, c.industry)
                for c in db.query(Company).all()}

    fin_raw = cached(_FIN_CACHE_KEY, _pull_fin)
    co_raw = cached(_CO_CACHE_KEY, _pull_companies)
    fin_by_co = {ec: [_FinRow(*vals) for vals in rows] for ec, rows in fin_raw.items()}
    companies = {ec: _CompanyRow(*vals) for ec, vals in co_raw.items()}
    return fin_by_co, companies


def build_panel(db, args) -> tuple:
    """M-2 既定 config でスナップショットを1回だけ構築して返す。"""
    p = get_plugin("macro_gbdt")
    params = coerce_params(p.params_schema(), {})
    macro_names = list(params["macro_features"]) if params["use_macro"] else []
    missing = [f for f in params["fin_features"] if f not in _FIN_FIELDS]
    if missing:
        raise SystemExit(f"_FIN_FIELDS に無い財務特徴量: {missing}（キャッシュ定義を更新してください）")

    prices_by_co = _load_prices(args.allow_full_pull)
    print(f"cached companies (prices)={len(prices_by_co)}", flush=True)

    fin_by_co, companies = _load_financials(db)
    print(f"fin_cos={len(fin_by_co)} companies={len(companies)}", flush=True)

    mkey = hashlib.md5(",".join(sorted(macro_names)).encode()).hexdigest()[:10]
    macro_cache = (cached(f"bakeoff_macro_{mkey}",
                          lambda: preload_macro(db, prices_by_co, macro_names))
                   if macro_names else {})
    db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない

    samples_by_ym, meta_by_ym, _current, feat_names, ids_by_ym = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache,
        params["fin_features"], macro_names,
        params["use_momentum"], params["momentum_window"], params["min_coverage"],
        build_interactions=False, macro_nan_ok=True,
        price_features=list(params.get("price_features") or []),
        return_stock_ids=True,
    )
    samples_by_ym, meta_by_ym, ids_by_ym = _thin(
        samples_by_ym, meta_by_ym, ids_by_ym, args.stride)
    n_total = sum(len(v) for v in samples_by_ym.values())
    print(f"panel: months={len(samples_by_ym)} samples={n_total} features={len(feat_names)}",
          flush=True)
    return samples_by_ym, meta_by_ym, ids_by_ym, feat_names


def run_one(name: str, samples_by_ym: dict, meta_by_ym: dict, ids_by_ym: dict,
            feat_names: list, pca: int) -> dict:
    """候補1件を walk-forward → oof_backtest まで回して指標 dict を返す。"""
    t0 = time.time()
    eff_names = pca_feature_names(feat_names, pca) if pca > 0 else feat_names
    errors: list[str] = []
    if name == "ols":
        fit_predict, wf_extra, diag = None, {}, {}
    else:
        raw_fp, wf_extra, diag = build_candidate(name, feat_names, pca_components=pca)

        def fit_predict(*a, _inner=raw_fp, **k):
            """例外理由を握り潰さずに記録する薄いラッパー。

            walk_forward_cv_monthly は fit_predict の例外を fold スキップとして**静かに**
            飲み込む（`except Exception: continue`）。全 fold が同じ理由で落ちても
            「fold が 0 件」としか見えず原因が消えるため、ここで理由を控えてから再送出する。
            """
            try:
                return _inner(*a, **k)
            except Exception as e:  # noqa: BLE001 — 記録して再送出（挙動は変えない）
                errors.append(f"{type(e).__name__}: {e}")
                raise

    folds, residuals = walk_forward_cv_monthly(
        samples_by_ym, eff_names,
        min_train_months=MIN_TRAIN_MONTHS, step_months=STEP_MONTHS,
        return_residuals=True, fit_predict=fit_predict,
        embargo_months=LABEL_HORIZON_MONTHS, **wf_extra,
    )
    meta = build_oof_meta(ids_by_ym, meta_by_ym, residuals.keys())
    bt = oof_backtest(residuals, n_quantiles=5, meta_by_ym=meta, rebalance_per_year=4)
    out = {
        "name": name,
        "pca": pca,
        "n_folds": len(folds),
        "elapsed_sec": round(time.time() - t0, 1),
        "oof": bt,
        "diag": summarize_diag(diag) if diag else {},
    }
    if errors:
        # 全 fold が落ちても walk_forward は静かに [] を返すため、握った理由をここで表に出す
        # （n_folds が減っただけの「静かな縮退」を見逃さない）。
        out["fold_errors"] = {"n": len(errors), "first": errors[0]}
        if not folds:
            out["error"] = f"全 fold 失敗: {errors[0]}"
    return out


def _fmt(v, nd: int = 4) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:+.{nd}f}"
    return str(v)


def significance_vs_baseline(rows: list[dict], baseline: str = "xgb_m2") -> dict:
    """各候補と基準線（M-2 相当）の rank-IC 差を定常ブートストラップで検定する。

    `model_stats.paired_ic_significance`（Issue #369・ADR-0018）をそのまま再利用する。
    walk-forward の per-fold IC は学習窓が重なり系列相関を持つため、素朴な paired-t では
    有意差を過大に主張する。共通 test 期でペアリングし、ブロック・ブートストラップで
    差の平均の CI を出す（CI が 0 を跨がなければ有意）。昇格判断はこの検定で行う。
    """
    from model_stats import paired_ic_significance

    base = next((r for r in rows if r["name"] == baseline and not r.get("error")), None)
    if base is None:
        return {}
    base_ic = (base.get("oof") or {}).get("rank_ic_by_period") or {}
    out: dict = {}
    for r in rows:
        if r["name"] == baseline or r.get("error"):
            continue
        ic = (r.get("oof") or {}).get("rank_ic_by_period") or {}
        res = paired_ic_significance(ic, base_ic)
        if res:
            res["better"] = (r["name"] if res["mean"] > 0 else baseline) if res["significant"] else None
            out[r["name"]] = res
    return out


def report(rows: list[dict], sig: dict | None = None) -> None:
    print("\n================ #372 CANDIDATE BAKE-OFF (honest OOF, embargo=12) ================")
    hdr = (f"{'candidate':16} {'IC mean':>9} {'IC std':>8} {'IC n':>5} {'neutIC':>9} "
           f"{'LS spread':>10} {'hit':>6} {'bkeven bp':>10} {'turnover':>9} "
           f"{'n_oof':>7} {'sec':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        bt = r["oof"]
        if r.get("error"):
            print(f"{r['name'][:16]:16} ERROR {r['error']}")
            continue
        ric = bt.get("rank_ic", {})
        nic = bt.get("rank_ic_industry_neutral", {})
        label = r["name"] + (f"+pca{r['pca']}" if r["pca"] else "")
        print(f"{label[:16]:16} {_fmt(ric.get('mean')):>9} {_fmt(ric.get('std')):>8} "
              f"{str(ric.get('n')):>5} {_fmt(nic.get('mean')):>9} "
              f"{_fmt(bt.get('long_short_spread')):>10} {_fmt(bt.get('hit_rate'), 2):>6} "
              f"{_fmt(bt.get('breakeven_cost_bps'), 1):>10} "
              f"{_fmt(bt.get('effective_turnover'), 3):>9} "
              f"{str(bt.get('n_oof_samples')):>7} {r['elapsed_sec']:>7}")

    if sig:
        print("\n---- rank-IC diff vs xgb_m2 (stationary bootstrap, ADR-0018) ----")
        h2 = (f"{'candidate':20} {'diff':>9} {'ci_lo':>9} {'ci_hi':>9} {'p':>7} "
              f"{'signif':>7} {'n_common':>9}")
        print(h2)
        print("-" * len(h2))
        for name, s in sig.items():
            print(f"{name[:20]:20} {_fmt(s['mean']):>9} {_fmt(s['ci_lo']):>9} "
                  f"{_fmt(s['ci_hi']):>9} {s['p_value']:>7} "
                  f"{str(s['significant']):>7} {s['n_common']:>9}")

    print("\n---- diagnostics ----")
    for r in rows:
        if r.get("diag"):
            compact = {k: v for k, v in r["diag"].items()
                       if not isinstance(v, list) or len(v) <= 8}
            print(f"[{r['name']}] {json.dumps(compact, ensure_ascii=False, default=str)[:600]}")
    print("\n---- interval diagnostics (nominal vs empirical) ----")
    for r in rows:
        d = r.get("diag") or {}
        if "interval_coverage_leaf" in d:
            print(f"[{r['name']}] nominal={d.get('interval_nominal')} "
                  f"leaf_cov={d.get('interval_coverage_leaf')} "
                  f"leaf_hw={d.get('interval_halfwidth_leaf')} "
                  f"tree_cov={d.get('interval_coverage_tree')} "
                  f"tree_hw={d.get('interval_halfwidth_tree')}")
        bt = r.get("oof") or {}
        if bt.get("interval_coverage") is not None:
            print(f"[{r['name']}] split-conformal(family-wide): tau={bt.get('interval_tau')} "
                  f"cov={bt.get('interval_coverage')} hw={bt.get('interval_halfwidth')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Issue #372 兄弟モデル候補の OOF 横並び実測")
    ap.add_argument("--candidates", default=",".join(DEFAULT_ORDER),
                    help="カンマ区切りの候補名（既定=全候補）。--list で一覧表示")
    ap.add_argument("--pca", type=int, default=0,
                    help="マクロ列を fold 内 PCA で圧縮する主成分数（0=圧縮しない）")
    ap.add_argument("--with-ols", action="store_true",
                    help="素 OLS 基準線（fit_predict=None）も併走する")
    ap.add_argument("--stride", type=int, default=1,
                    help="各月のサンプルを stride 件おきへ間引く（動作確認用）")
    ap.add_argument("--smoke", action="store_true", help="--stride 5 の短縮実行")
    ap.add_argument("--list", action="store_true", help="候補一覧を表示して終了")
    ap.add_argument("--json", dest="json_out", default=None, help="結果 JSON の出力先")
    ap.add_argument("--refresh-cache", action="store_true", help="価格キャッシュを再取得")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="価格キャッシュ不在時に97万行 pull を許可")
    args = ap.parse_args()

    if args.list:
        print(f"{'name':16} {'available':10} note")
        for n, c in CANDIDATES.items():
            print(f"{n:16} {str(candidate_available(n)):10} {c.label} / {c.note}")
        return

    if args.smoke:
        args.stride = max(args.stride, 5)
    if args.refresh_cache:
        set_refresh(True)

    names = [n.strip() for n in args.candidates.split(",") if n.strip()]
    skipped = [n for n in names if n in CANDIDATES and not candidate_available(n)]
    names = [n for n in names if n not in skipped]
    if skipped:
        print(f"SKIP (optional package missing): {', '.join(skipped)}"
              " -- pip install -r requirements-optional.txt", flush=True)
    if args.with_ols:
        names = ["ols"] + names

    print(f"is_local={database._is_local} stride={args.stride} pca={args.pca}", flush=True)
    db = SessionLocal()
    rows: list[dict] = []
    try:
        samples_by_ym, meta_by_ym, ids_by_ym, feat_names = build_panel(db, args)
        for name in names:
            try:
                r = run_one(name, samples_by_ym, meta_by_ym, ids_by_ym, feat_names, args.pca)
            except Exception as e:  # noqa: BLE001 — 1候補の失敗で全体を落とさない
                r = {"name": name, "pca": args.pca, "oof": {}, "diag": {},
                     "elapsed_sec": 0.0, "error": f"{type(e).__name__}: {e}"}
            ric = (r.get("oof") or {}).get("rank_ic", {})
            fe = r.get("fold_errors")
            note = r.get("error") or ""
            if fe:
                note += " fold例外 {n}件: {first}".format(**fe)
            print(f"[{name}] ic_mean={ric.get('mean')} n_folds={r.get('n_folds')} "
                  f"{note} ({r['elapsed_sec']}s)", flush=True)
            rows.append(r)
    finally:
        db.close()

    sig = significance_vs_baseline(rows)
    report(rows, sig)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "significance_vs_xgb_m2": sig},
                      f, ensure_ascii=False, indent=2, default=str)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
