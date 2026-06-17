"""
M-1 マクロ・リスク-リターン推奨プラグイン（Phase B）

財務比率 × マクロ要因の交差項を前進 BIC で選択し、
リスク-リターン平面に各銘柄をプロットして推奨集合を選ぶ。

次元整合性（CLAUDE.md）:
  目的変数 = 52週先対数リターン（無次元）
  説明変数 = 財務比率・Zスコア・マクロ変化率/Zスコア・それらの交差項（全て無次元）
"""
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .base import AnalysisPlugin
from .utils import (
    check_collinearity,
    normalize,
    normalize_transform,
    ols,
    walk_forward_cv_monthly,
    winsorize,
)

FINANCIAL_LAG_DAYS = 45
HORIZON_WEEKS = 52

FIN_BASE_OPTIONS = [
    {"value": "per",          "label": "PER"},
    {"value": "pbr",          "label": "PBR"},
    {"value": "roe",          "label": "ROE（%）"},
    {"value": "equity_ratio", "label": "自己資本比率（%）"},
    {"value": "rd_intensity", "label": "R&D集約度"},
    {"value": "da_intensity", "label": "D&A集約度"},
    {"value": "z_op_margin",  "label": "営業利益率Zスコア"},
    {"value": "z_roe",        "label": "ROE Zスコア"},
    {"value": "z_cf_ratio",   "label": "CF比率Zスコア"},
]
DEFAULT_FIN_FEATURES = ["per", "pbr", "roe", "equity_ratio"]

# series_code → transform ("yoy" | "zscore")
_MACRO_MAP = {
    "macro_usdjpy_yoy":   ("USDJPY", "yoy"),
    "macro_sp500_yoy":    ("SP500",  "yoy"),
    "macro_us10y_zscore": ("US10Y",  "zscore"),
    "macro_jp10y_zscore": ("JP10Y",  "zscore"),
}
MACRO_FEATURE_NAMES = list(_MACRO_MAP.keys())


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _add_days(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _find_applicable_fin(fin_recs: list, snap_date: str):
    result = None
    for fr in fin_recs:
        if not fr.period_end:
            continue
        pe = fr.period_end
        pe_str = pe.isoformat() if hasattr(pe, "isoformat") else str(pe)[:10]
        if _add_days(pe_str, FINANCIAL_LAG_DAYS) <= snap_date:
            result = fr
    return result


def _macro_from_cache(
    by_series: dict[str, dict[str, float]],
    ref_date: str,
    feature_names: list[str],
    window_days: int = 30,
    zscore_years: int = 5,
) -> dict[str, float | None]:
    """プリロード済みマクロデータから特徴量を計算（DB クエリなし）。"""
    from datetime import date as _date, timedelta as _td
    ref = _date.fromisoformat(ref_date)
    win_start = (ref - _td(days=window_days)).isoformat()
    result: dict[str, float | None] = {}

    for fname in feature_names:
        if fname not in _MACRO_MAP:
            result[fname] = None
            continue
        scode, ttype = _MACRO_MAP[fname]
        date_close = by_series.get(scode, {})
        if not date_close:
            result[fname] = None
            continue

        current_vals = [v for d, v in date_close.items() if win_start <= d <= ref_date]
        if not current_vals:
            result[fname] = None
            continue
        current_avg = statistics.mean(current_vals)

        if ttype == "yoy":
            ref_1y = ref - _td(days=365)
            p_s = (ref_1y - _td(days=window_days)).isoformat()
            p_e = (ref_1y + _td(days=window_days)).isoformat()
            prev_vals = [v for d, v in date_close.items() if p_s <= d <= p_e]
            if not prev_vals:
                result[fname] = None
                continue
            prev_avg = statistics.mean(prev_vals)
            result[fname] = (current_avg - prev_avg) / prev_avg if prev_avg else None

        elif ttype == "zscore":
            hist_start = (ref - _td(days=zscore_years * 366)).isoformat()
            all_vals = [v for d, v in date_close.items() if hist_start <= d <= ref_date]
            if len(all_vals) < 20:
                result[fname] = None
                continue
            mu = statistics.mean(all_vals)
            sigma = statistics.stdev(all_vals) if len(all_vals) > 1 else 0.0
            result[fname] = (current_avg - mu) / sigma if sigma else None

    return result


def _realized_vol(price_rows: list, ref_date: str, weeks: int = 52) -> float | None:
    """ref_date 直前 weeks 週の実現ボラティリティ（年率）を返す。リークなし。"""
    eligible = [(r.trade_date, r.close_last)
                for r in price_rows
                if r.trade_date <= ref_date and r.close_last and r.close_last > 0]
    if len(eligible) < 4:
        return None
    # 直近 weeks+1 件
    recent = eligible[max(0, len(eligible) - weeks - 1):]
    if len(recent) < 4:
        return None
    log_rets = [
        math.log(recent[i][1] / recent[i - 1][1])
        for i in range(1, len(recent))
        if recent[i - 1][1] > 0
    ]
    if len(log_rets) < 3:
        return None
    return statistics.stdev(log_rets) * math.sqrt(52)


def _pareto_frontier(
    items: list[dict],
    x_key: str = "r2",
    y_key: str = "mu_shrunk",
) -> set[str]:
    """Y が大きく X が小さい意味での非劣解（Pareto 最適）の edinet_code 集合を返す。"""
    dominated: set[str] = set()
    codes = [it["edinet_code"] for it in items]
    vals = {it["edinet_code"]: (it.get(x_key) or 0.0, it.get(y_key) or 0.0) for it in items}
    for code_a in codes:
        xa, ya = vals[code_a]
        for code_b in codes:
            if code_a == code_b:
                continue
            xb, yb = vals[code_b]
            # B が A を支配: B のリスク <= A のリスク AND B のリターン >= A のリターン（少なくとも一方は strict）
            if xb <= xa and yb >= ya and (xb < xa or yb > ya):
                dominated.add(code_a)
                break
    return set(codes) - dominated


# ── プラグイン本体 ────────────────────────────────────────────────────────────

class MacroRiskReturnPlugin(AnalysisPlugin):
    name = "macro_risk_return"
    label = "マクロ×リスク-リターン推奨"
    description = (
        "財務比率×マクロ要因の交差項を前進BICで選択し、"
        "各銘柄を期待リターン（縦軸）×リスク（横軸）の散布図に配置して推奨集合を選びます。"
        "【注意】株価週次履歴とマクロデータ5年分の蓄積が必要です。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    category = "③ 将来リターンを予測"
    ui_order = 330

    def params_schema(self) -> dict:
        return {
            "lambda_risk": {
                "type": "slider",
                "dtype": "float",
                "label": "リスク回避度 λ",
                "description": "U = μ − λ × R2。λ=0 でリターン最大化、λ大でリスク重視。",
                "default": 1.0,
                "min": 0.0,
                "max": 3.0,
            },
            "risk_axis": {
                "type": "select",
                "label": "横軸リスク",
                "description": "R2=実現ボラ（既定）/ R1=予測不確実性",
                "options": [
                    {"value": "r2", "label": "R2 実現ボラティリティ（既定）"},
                    {"value": "r1", "label": "R1 予測不確実性"},
                ],
                "default": "r2",
            },
            "fin_features": {
                "type": "multiselect",
                "label": "財務ベース特徴量",
                "options": FIN_BASE_OPTIONS,
                "default": DEFAULT_FIN_FEATURES,
            },
            "use_macro": {
                "type": "checkbox",
                "label": "マクロ特徴量・交差項を使用",
                "default": True,
            },
            "momentum_window": {
                "type": "number",
                "dtype": "int",
                "label": "モメンタム算出月数",
                "default": 12,
                "min": 3,
                "max": 24,
            },
            "max_features": {
                "type": "slider",
                "dtype": "int",
                "label": "BIC 最大採用特徴量数",
                "default": 20,
                "min": 5,
                "max": 40,
            },
            "min_coverage": {
                "type": "slider",
                "dtype": "float",
                "label": "特徴量充足率下限",
                "description": "全特徴量が揃っているサンプルの最低割合。",
                "default": 0.5,
                "min": 0.1,
                "max": 1.0,
            },
            "top_n": {
                "type": "number",
                "dtype": "int",
                "label": "上位表示件数",
                "default": 30,
                "min": 5,
                "max": 200,
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        lambda_risk    = params["lambda_risk"]
        risk_axis      = params["risk_axis"]
        fin_features   = params["fin_features"]
        use_macro      = params["use_macro"]
        mom_window     = params["momentum_window"]
        max_features   = params["max_features"]
        min_coverage   = params["min_coverage"]
        top_n          = params["top_n"]

        if not fin_features:
            raise ValueError("財務特徴量を1つ以上選択してください。")

        prices_by_co, fin_by_co, companies = self._load_data(db)
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に収集を実行してください。")

        macro_cache = self._preload_macro(db, prices_by_co) if use_macro else {}
        sectors = self._collect_sectors(fin_by_co, companies)

        samples_by_ym, current_snaps, all_feat_names = self._build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache, sectors,
            fin_features, use_macro, mom_window, min_coverage,
        )

        total_samples = sum(len(v) for v in samples_by_ym.values())
        if total_samples < 20:
            raise ValueError(f"学習サンプルが不足（{total_samples}件）。データを収集してから再実行してください。")

        # --- 前進 BIC 特徴量選択 ---
        selected_names = self._forward_bic(
            samples_by_ym, all_feat_names, max_features=max_features
        )
        n_sel = len(selected_names)
        if n_sel == 0:
            raise ValueError("BIC 選択で有効な特徴量が選ばれませんでした。")

        # 選択済み特徴量の列インデックス
        sel_idx = [all_feat_names.index(n) for n in selected_names]
        samples_sel: dict[str, list] = {
            ym: [([row[i] for i in sel_idx], tgt) for row, tgt in pairs]
            for ym, pairs in samples_by_ym.items()
        }

        # --- Walk-Forward CV ---
        cv_folds = walk_forward_cv_monthly(
            dict(samples_sel), selected_names, min_train_months=6, step_months=3
        )
        cv_metrics = {
            "folds":     cv_folds,
            "mean_r2":   round(statistics.mean(f["r2"] for f in cv_folds), 4) if cv_folds else None,
            "mean_rmse": round(statistics.mean(f["rmse"] for f in cv_folds), 4) if cv_folds else None,
            "n_folds":   len(cv_folds),
        }

        # --- 最終モデル学習 ---
        beta, win_params, norm_params, y_mu, y_sd, XtX_inv, sigma2 = self._fit_final(
            samples_sel, n_sel
        )

        # --- スコアリング ---
        results = self._score_companies(
            current_snaps, sel_idx, n_sel,
            beta, win_params, norm_params, y_mu, y_sd,
            XtX_inv, sigma2, prices_by_co,
            lambda_risk, risk_axis, top_n,
        )

        return {
            "cv_metrics":       cv_metrics,
            "selected_features": selected_names,
            "n_train_samples":  total_samples,
            "n_companies":      len(current_snaps),
            "results":          results,
        }

    # ── データロード ────────────────────────────────────────────────────────

    def _load_data(self, db) -> tuple:
        from collections import namedtuple as _nt
        from database import Company, FinancialMetric, StockPriceWeekly

        raw = (
            db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                     StockPriceWeekly.close_last)
            .order_by(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date)
            .all()
        )
        _PX = _nt("_PX", "trade_date close_last")
        prices_by_co: dict[str, list] = defaultdict(list)
        for ec, td, cl in raw:
            prices_by_co[ec].append(_PX(td, cl))

        fin_by_co: dict[str, list] = defaultdict(list)
        for r in (db.query(FinancialMetric)
                  .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end)
                  .all()):
            fin_by_co[r.edinet_code].append(r)

        companies = {c.edinet_code: c for c in db.query(Company).all()}
        return prices_by_co, fin_by_co, companies

    def _preload_macro(self, db, prices_by_co: dict) -> dict:
        from database import MacroData
        all_dates = [row.trade_date for rows in prices_by_co.values() for row in rows]
        if not all_dates:
            return {}
        from datetime import date as _date, timedelta as _td
        min_d = min(all_dates)
        # zscore 用に 5年前まで遡る
        since = (_date.fromisoformat(min_d) - _td(days=5 * 366)).isoformat()
        max_d = max(all_dates)
        rows = (
            db.query(MacroData)
            .filter(
                MacroData.trade_date >= since,
                MacroData.trade_date <= max_d,
                MacroData.close.isnot(None),
            )
            .order_by(MacroData.series_code, MacroData.trade_date)
            .all()
        )
        by_series: dict[str, dict[str, float]] = {}
        for r in rows:
            by_series.setdefault(r.series_code, {})[r.trade_date] = r.close
        return by_series

    def _collect_sectors(self, fin_by_co: dict, companies: dict) -> list[str]:
        seen: set[str] = set()
        for recs in fin_by_co.values():
            if recs:
                seen.add(recs[-1].industry or "不明")
        for c in companies.values():
            seen.add(c.industry or "不明")
        return sorted(seen - {"不明", None, ""})

    # ── スナップショット構築 ─────────────────────────────────────────────────

    def _build_snapshots(
        self,
        prices_by_co, fin_by_co, companies, macro_cache,
        sectors, fin_features, use_macro, mom_window, min_coverage,
    ) -> tuple:
        macro_names = MACRO_FEATURE_NAMES if use_macro else []
        momentum_name = ["momentum_12m1"] if use_macro else []

        # 交差項名を生成（fin × macro + sector_dummy × macro）
        interaction_names: list[str] = []
        if use_macro:
            for fn in fin_features:
                for mn in macro_names:
                    interaction_names.append(f"{fn}_x_{mn}")
            for s in sectors[:10]:  # 最大10セクター
                safe = s.replace(" ", "_").replace("・", "_")[:12]
                for mn in macro_names:
                    interaction_names.append(f"sec_{safe}_x_{mn}")

        all_feat_names = (
            fin_features + macro_names + momentum_name + interaction_names
        )
        n_feat = len(all_feat_names)

        samples_by_ym: dict[str, list] = defaultdict(list)
        current_snaps: dict[str, tuple] = {}
        min_rows = HORIZON_WEEKS + 4

        for edinet_code, price_rows in prices_by_co.items():
            n = len(price_rows)
            if n < min_rows:
                continue
            fin_recs = fin_by_co.get(edinet_code, [])
            if not fin_recs:
                continue

            dates  = [r.trade_date for r in price_rows]
            closes = [r.close_last  for r in price_rows]

            # 月末インデックス
            month_ends = [
                i for i in range(n - 1) if dates[i][:7] != dates[i + 1][:7]
            ] + [n - 1]

            for snap_idx in month_ends:
                if snap_idx < 4:
                    continue
                snap_date = dates[snap_idx]
                snap_ym   = snap_date[:7]
                is_current = (snap_idx == n - 1)
                has_future = (snap_idx + HORIZON_WEEKS < n)

                fin_rec = _find_applicable_fin(fin_recs, snap_date)
                if fin_rec is None:
                    continue

                # 財務特徴量
                fin_row: list[float] = []
                ok = True
                for fn in fin_features:
                    v = getattr(fin_rec, fn, None)
                    if v is None:
                        ok = False
                        break
                    fin_row.append(float(v))
                if not ok:
                    continue

                # マクロ特徴量
                macro_row: list[float] = []
                macro_dict: dict[str, float] = {}
                if use_macro:
                    m_feats = _macro_from_cache(macro_cache, snap_date, macro_names)
                    if any(v is None for v in m_feats.values()):
                        continue  # マクロ未蓄積はスキップ
                    for mn in macro_names:
                        val = m_feats[mn]
                        macro_row.append(float(val))  # type: ignore[arg-type]
                        macro_dict[mn] = float(val)   # type: ignore[arg-type]

                # モメンタム
                mom_row: list[float] = []
                if use_macro:
                    mom = self._momentum(closes, dates, snap_idx, mom_window)
                    if mom is None:
                        continue
                    mom_row = [mom]

                # セクター取得
                industry = fin_rec.industry or (companies.get(edinet_code, None) and companies[edinet_code].industry) or "不明"

                # 交差項
                inter_row: list[float] = []
                if use_macro:
                    for fn, fv in zip(fin_features, fin_row):
                        for mn in macro_names:
                            inter_row.append(fv * macro_dict[mn])
                    for s in sectors[:10]:
                        d_val = 1.0 if industry == s else 0.0
                        for mn in macro_names:
                            inter_row.append(d_val * macro_dict[mn])

                feat_row = fin_row + macro_row + mom_row + inter_row

                # 充足率チェック
                non_null = sum(1 for v in feat_row if v == v)  # NaN チェック
                if non_null / n_feat < min_coverage:
                    continue

                if has_future:
                    c_snap, c_fut = closes[snap_idx], closes[snap_idx + HORIZON_WEEKS]
                    if c_snap and c_fut and c_snap > 0 and c_fut > 0:
                        samples_by_ym[snap_ym].append((feat_row, math.log(c_fut / c_snap)))

                if is_current:
                    comp = companies.get(edinet_code)
                    current_snaps[edinet_code] = (feat_row, {
                        "sec_code":     fin_rec.sec_code or (comp.sec_code if comp else ""),
                        "company_name": fin_rec.company_name or (comp.name if comp else edinet_code),
                        "industry":     industry,
                        "price_rows":   price_rows,
                        "snap_date":    snap_date,
                    })

        return dict(samples_by_ym), current_snaps, all_feat_names

    @staticmethod
    def _momentum(closes: list, dates: list, snap_idx: int, long_months: int) -> float | None:
        short_months = 1
        snap_date = dates[snap_idx]
        from datetime import date as _date, timedelta as _td
        ref = _date.fromisoformat(snap_date)
        short_cutoff = (ref - _td(days=short_months * 30)).isoformat()
        long_cutoff  = (ref - _td(days=long_months  * 30)).isoformat()
        eligible = [(dates[i], closes[i]) for i in range(snap_idx + 1)
                    if closes[i] and closes[i] > 0]
        if not eligible:
            return None
        short_cands = [(d, c) for d, c in eligible if d <= short_cutoff]
        long_cands  = [(d, c) for d, c in eligible if d <= long_cutoff]
        if not short_cands or not long_cands:
            return None
        return math.log(short_cands[-1][1] / long_cands[-1][1])

    # ── 前進 BIC 特徴量選択 ─────────────────────────────────────────────────

    def _forward_bic(
        self,
        samples_by_ym: dict,
        all_feat_names: list[str],
        max_features: int = 20,
        vif_threshold: float = 10.0,
    ) -> list[str]:
        all_samples = [s for ym_s in samples_by_ym.values() for s in ym_s]
        if len(all_samples) < 5:
            return []
        X_raw = [s[0] for s in all_samples]
        y_raw = [s[1] for s in all_samples]
        n = len(y_raw)
        n_cand = len(all_feat_names)

        # winsorize + zscore 正規化（全候補特徴量に適用）
        X_norm_cols: list[list[float]] = []
        for ci in range(n_cand):
            col = [X_raw[ri][ci] for ri in range(n)]
            col_w, _, _ = winsorize(col)
            col_n, _, _ = normalize(col_w, "zscore")
            X_norm_cols.append(col_n)
        y_w, _, _ = winsorize(y_raw)
        y_n, _, _ = normalize(y_w, "zscore")

        def _bic(selected_cols: list[int]) -> float:
            if not selected_cols:
                sse = sum(v ** 2 for v in y_n) - len(y_n) * (statistics.mean(y_n) ** 2)
                return n * math.log(max(sse, 1e-12) / n) + math.log(n)
            X_sub = [[1.0] + [X_norm_cols[ci][ri] for ci in selected_cols]
                     for ri in range(n)]
            res = ols(X_sub, y_n)
            if res is None:
                return float("inf")
            sse = sum((y_n[i] - res["yhat"][i]) ** 2 for i in range(n))
            k = len(selected_cols) + 1  # +1 for intercept
            return n * math.log(max(sse, 1e-12) / n) + k * math.log(n)

        selected: list[int] = []
        remaining = list(range(n_cand))
        current_bic = _bic([])

        while remaining and len(selected) < max_features:
            best_bic = current_bic
            best_ci  = -1

            for ci in remaining:
                test = selected + [ci]
                # VIF チェック（2個以上になったとき）
                if len(test) >= 2:
                    X_cols_test = [X_norm_cols[i] for i in test]
                    names_test  = [all_feat_names[i] for i in test]
                    vif_info = check_collinearity(X_cols_test, names_test)
                    if vif_info.get("max_vif", 0) > vif_threshold:
                        continue
                b = _bic(test)
                if b < best_bic:
                    best_bic = b
                    best_ci  = ci

            if best_ci == -1:
                break
            selected.append(best_ci)
            remaining.remove(best_ci)
            current_bic = best_bic

        return [all_feat_names[i] for i in selected]

    # ── 最終モデル学習 ───────────────────────────────────────────────────────

    def _fit_final(self, samples_sel: dict, n_sel: int) -> tuple:
        all_s = [s for ym_s in samples_sel.values() for s in ym_s]
        X_raw = [s[0] for s in all_s]
        y_raw = [s[1] for s in all_s]
        n = len(y_raw)

        win_params:  list[tuple] = []
        norm_params: list[tuple] = []
        X_n = [[1.0] + [0.0] * n_sel for _ in range(n)]
        for fi in range(n_sel):
            col = [X_raw[ri][fi] for ri in range(n)]
            col_w, wlo, whi = winsorize(col)
            win_params.append((wlo, whi))
            col_norm, p1, p2 = normalize(col_w, "zscore")
            norm_params.append((p1, p2))
            for ri, v in enumerate(col_norm):
                X_n[ri][fi + 1] = v

        y_w, _, _ = winsorize(y_raw)
        y_n, y_mu, y_sd = normalize(y_w, "zscore")

        res = ols(X_n, y_n)
        if res is None:
            raise ValueError("最終 OLS の計算に失敗しました。多重共線性の可能性があります。")

        beta = res["beta"]
        sse = sum((y_n[i] - res["yhat"][i]) ** 2 for i in range(n))
        df  = n - (n_sel + 1)
        sigma2 = sse / df if df > 0 else 1.0

        # (X^T X)^{-1} の対角（R1 計算用）
        X_np = np.array(X_n)
        try:
            XtX_inv = np.linalg.inv(X_np.T @ X_np)
        except np.linalg.LinAlgError:
            XtX_inv = None

        return beta, win_params, norm_params, y_mu, y_sd, XtX_inv, sigma2

    # ── スコアリング ─────────────────────────────────────────────────────────

    def _score_companies(
        self,
        current_snaps, sel_idx, n_sel,
        beta, win_params, norm_params, y_mu, y_sd,
        XtX_inv, sigma2, prices_by_co,
        lambda_risk, risk_axis, top_n,
    ) -> list[dict]:
        raw_items: list[dict] = []

        for edinet_code, (feat_row, info) in current_snaps.items():
            if sel_idx and len(feat_row) <= max(sel_idx):
                continue

            # 選択済み列だけ抽出・正規化
            x_norm = [1.0]
            ok = True
            for fi, orig_ci in enumerate(sel_idx):
                v = feat_row[orig_ci]
                wlo, whi = win_params[fi]
                v_w = max(wlo, min(whi, v))
                x_norm.append(normalize_transform(v_w, *norm_params[fi]))

            # 予測リターン（log space → 年率）
            pred_log = sum(x_norm[j] * beta[j] for j in range(len(beta))) * y_sd + y_mu
            mu_raw = pred_log  # 無次元対数リターン ≈ 年率

            # R1: 予測標準誤差 se_obs = sqrt(sigma2 * (1 + x^T (X^TX)^{-1} x))
            r1: float | None = None
            if XtX_inv is not None:
                xv = np.array(x_norm)
                leverage = float(xv @ XtX_inv @ xv)
                r1 = math.sqrt(max(0.0, sigma2 * (1.0 + leverage)))

            # R2: 実現ボラ（直前52週）
            price_rows = prices_by_co.get(edinet_code, [])
            snap_date  = info["snap_date"]
            r2 = _realized_vol(price_rows, snap_date, weeks=52)

            raw_items.append({
                "edinet_code":  edinet_code,
                "sec_code":     info["sec_code"],
                "company_name": info["company_name"],
                "industry":     info["industry"],
                "mu_raw":       round(mu_raw, 6),
                "r1":           round(r1, 6) if r1 is not None else None,
                "r2":           round(r2, 6) if r2 is not None else None,
            })

        if not raw_items:
            return []

        # μ 収縮: セクター平均へ R1 の正規化ウェイトで引き寄せる（Black-Litterman 型）
        r1_vals = [it["r1"] for it in raw_items if it["r1"] is not None]
        r1_max  = max(r1_vals) if r1_vals else 1.0

        sector_means: dict[str, float] = defaultdict(list)  # type: ignore[assignment]
        for it in raw_items:
            sector_means[it["industry"]].append(it["mu_raw"])  # type: ignore[index]
        sector_means = {s: statistics.mean(vs) for s, vs in sector_means.items()}  # type: ignore[assignment]

        for it in raw_items:
            w = (it["r1"] / r1_max) if (it["r1"] is not None and r1_max > 0) else 0.5
            sec_mu = sector_means.get(it["industry"], it["mu_raw"])
            it["mu_shrunk"] = round((1 - w) * it["mu_raw"] + w * sec_mu, 6)

        # Pareto フロンティア
        pareto_codes = _pareto_frontier(
            [it for it in raw_items if it.get("r2") is not None and it.get("mu_shrunk") is not None],
            x_key="r2",
            y_key="mu_shrunk",
        )

        # 効用 U = μ_shrunk - λ × リスク（選択軸）
        for it in raw_items:
            risk_val = it.get(risk_axis) or it.get("r2")
            if it.get("mu_shrunk") is not None and risk_val is not None:
                it["utility"] = round(it["mu_shrunk"] - lambda_risk * risk_val, 6)
            else:
                it["utility"] = None
            it["is_pareto"] = edinet_code in pareto_codes  # Will be overwritten below

        # is_pareto を正しく設定
        for it in raw_items:
            it["is_pareto"] = it["edinet_code"] in pareto_codes

        # U でソートして top_n
        scored = [it for it in raw_items if it.get("utility") is not None]
        scored.sort(key=lambda x: x["utility"], reverse=True)

        return scored[:top_n]


plugin = MacroRiskReturnPlugin()
