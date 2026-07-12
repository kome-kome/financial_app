"""バリュエーション分析プラグイン（旧 乖離分析 / gap_analysis）。

業種内OLS（sector_ols）の `gap_ratio` seam を起点に、バリュエーション系の出力を
一括で出すハブ:
  - 割安度（gap_ratio）          … 業種内 OLS 理論株価との乖離率 [%]
  - 平均回帰タイミング（半減期）  … gap_ratio 時系列の AR(1) MLE
  - 期待総リターン               … gap_ratio + 配当利回り [%]（旧 total_return 吸収）
  - implied P/E・P/B             … 予測株価 ÷ EPS・BPS

旧 total_return プラグイン（独自プール回帰 OLS）はここへ統合・廃止した。理論株価は
sector_ols の per-share 業種内回帰から得る（OLSエンジンは sector_ols 1本に統一）。
内部 plugin name / `/api/gap-analysis` エンドポイントは後方互換のため "gap_analysis" を維持し、
表示ラベルのみ「バリュエーション分析」へ改名している。
"""
import logging
import math
from collections import defaultdict
from typing import Any

from .base import AnalysisPlugin

logger = logging.getLogger(__name__)

# 配当利回りの異常値ガード（％）。VIEW 由来の極端値を 0 とみなす（旧 total_return 踏襲）。
_DIV_YIELD_CAP = 30.0

# AR(1) MLE で半減期を推定するための最低観測数
_AR1_MIN_OBS = 8
# 推定値の妥当性チェック範囲（年単位の半減期）
_HL_MIN_YEARS = 0.25
_HL_MAX_YEARS = 20.0


def _estimate_ar1_half_life_years(series: list[float]) -> dict | None:
    """gap_ratio 時系列 (年次) から AR(1) MLE で半減期（年）を推定する。

    モデル: x_t = c + φ x_{t-1} + ε_t,  ε_t ~ N(0, σ²)
    平均回帰条件: 0 < φ < 1
    半減期: HL = -log(2) / log(φ)  (年単位)

    Returns: {phi, half_life_years, intercept, n_obs} または推定失敗時 None。
    """
    if len(series) < _AR1_MIN_OBS:
        return None
    try:
        from statsmodels.tsa.arima.model import ARIMA
        model = ARIMA(series, order=(1, 0, 0))
        res = model.fit()
    except Exception as e:
        logger.warning("AR(1) ARIMA推定失敗 (%s: %s)", type(e).__name__, e)
        return None

    # statsmodels ARIMA の AR(1) 係数は res.arparams[0] = φ
    try:
        phi = float(res.arparams[0])
        intercept = float(res.params[0])  # 定数項
    except Exception as e:
        logger.warning("AR(1) 係数取得失敗 (%s: %s)", type(e).__name__, e)
        return None

    # 平均回帰条件: 0 < φ < 1 でないと半減期が定義できない
    if not (0 < phi < 1):
        return None

    half_life = -math.log(2) / math.log(phi)
    if not (_HL_MIN_YEARS <= half_life <= _HL_MAX_YEARS):
        return None

    return {
        "phi": phi,
        "half_life_years": half_life,
        "intercept": intercept,
        "n_obs": len(series),
    }


class GapAnalysisPlugin(AnalysisPlugin):
    name = "gap_analysis"   # 内部 slug は後方互換（/api/gap-analysis）。表示ラベルは下記。
    label = "バリュエーション分析"
    description = (
        "業種別OLS分析の理論株価との乖離率（割安度）を起点に、平均回帰タイミング（半減期）と"
        "期待総リターン（乖離＋配当利回り）を一括表示します（先に業種別OLS分析を実行してください）。"
        "履歴データが十分にある銘柄は AR(1) MLE で平均回帰速度を推定し、"
        "不足する銘柄はヒューリスティック（参考値）にフォールバックします。"
    )
    depends_on = ["sector_ols"]
    category = "② 割安度を測る"
    ui_order = 220

    def params_schema(self) -> dict:
        return {
            "year": {
                "type": "number",
                "dtype": "int",
                "label": "対象年度（空=全年度）",
                "default": None,
                "optional": True,
            },
            "sort": {
                "type": "select",
                "label": "ソート順",
                "options": [
                    {"value": "desc",         "label": "乖離率 高い順（割安）"},
                    {"value": "asc",          "label": "乖離率 低い順（割高）"},
                    {"value": "total_return", "label": "期待総リターン 高い順（乖離＋配当）"},
                ],
                "default": "desc",
            },
            "min_div_yield": {
                "type": "number",
                "dtype": "float",
                "label": "最低配当利回り（%、0=フィルタなし）",
                "default": 0.0,
                "optional": True,
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        # gap_ratio / predicted_market_cap は regression_results 由来。
        # financial_metrics VIEW が LEFT JOIN して合成するため VIEW を読む。
        from database import FinancialMetric

        # params はパラメータ契約に従い coerce 済み（year:int|None・sort:str・min_div_yield:float）。
        year = params["year"]
        sort = params["sort"]
        min_div_yield = params["min_div_yield"]

        # 当該フィルタの最新スナップショット。上場廃止銘柄は買えないため対象外（Issue #315）。
        query = (db.query(FinancialMetric)
                   .filter(FinancialMetric.gap_ratio.isnot(None))
                   .filter(FinancialMetric.is_active.isnot(False)))
        if year:
            query = query.filter(FinancialMetric.year == year)
        records = query.all()

        # 回帰の鮮度・モデルメタ（staleness / ols-ridge 混在の可視化）。
        regression = self._regression_meta(db, year)

        # 依存（sector_ols）が全く未実行のケースは前段の ensure_dependencies が 404/400 で弾く。
        # ここで空 = 回帰はあるが当該フィルタ（年度等）に該当が無い → エラーでなく空結果を返す。
        if not records:
            return {
                "count": 0,
                "n_ar1_estimated": 0,
                "n_heuristic_fallback": 0,
                "results": [],
                "regression": regression,
            }

        # AR(1) 推定用に、各企業の全年度 gap_ratio 履歴を取得
        all_history = (
            db.query(FinancialMetric.edinet_code,
                     FinancialMetric.year,
                     FinancialMetric.gap_ratio)
              .filter(FinancialMetric.gap_ratio.isnot(None))
              .order_by(FinancialMetric.edinet_code, FinancialMetric.year)
              .all()
        )
        history_by_co: dict[str, list[float]] = defaultdict(list)
        for ec, yr, gap in all_history:
            if gap is not None:
                history_by_co[ec].append(float(gap))

        results = []
        n_ar1, n_heuristic = 0, 0
        for r in records:
            gap = r.gap_ratio or 0.0

            # 期待総リターン（旧 total_return 吸収）: gap[%] と同次元の配当利回り[%] を加算。
            # 理論株価は sector_ols の gap_ratio から復元: pred = actual × (1 + gap/100)。
            div_yield = float(r.div_yield) if r.div_yield is not None else 0.0
            if div_yield > _DIV_YIELD_CAP:
                div_yield = 0.0
            if min_div_yield > 0 and div_yield < min_div_yield:
                continue   # 配当利回りフィルタ（0=フィルタなし）

            sp  = r.stock_price
            eps = float(r.pl_eps) if r.pl_eps else None
            bps = float(r.bs_bps) if r.bs_bps else None
            if sp and sp > 0:
                pred_price = round(sp * (1 + gap / 100.0), 1)
                implied_per = round(pred_price / eps, 1) if eps and eps > 0 else None
                implied_pbr = round(pred_price / bps, 2) if bps and bps > 0 else None
            else:
                pred_price = implied_per = implied_pbr = None
            expected_total_return = round(gap + div_yield, 2)

            series = history_by_co.get(r.edinet_code, [])
            ar1 = _estimate_ar1_half_life_years(series)

            if ar1 is not None:
                half_life_months = ar1["half_life_years"] * 12
                method = "ar1"
                phi = ar1["phi"]
                n_ar1 += 1
            else:
                # フォールバック: 旧ヒューリスティック（統計的根拠なし、参考値）
                half_life_months = max(6, min(24, abs(gap) / 2))
                method = "heuristic"
                phi = None
                n_heuristic += 1

            decay = math.exp(-math.log(2) / half_life_months * 6)
            exp_6m  = round(gap * decay, 2)
            decay12 = math.exp(-math.log(2) / half_life_months * 12)
            exp_12m = round(gap * decay12, 2)
            conv_score_12m = round(min(95, max(5, 50 + gap * 0.8)), 1)

            results.append({
                "edinet_code":               r.edinet_code,
                "sec_code":                  r.sec_code,
                "company_name":              r.company_name,
                "industry":                  r.industry,
                "actual_market_cap":         r.market_cap,
                "predicted_market_cap":      r.predicted_market_cap,
                "gap_ratio":                 gap,
                # 期待総リターン系（旧 total_return 由来）
                "div_yield_pct":             round(div_yield, 2),
                "expected_total_return_pct": expected_total_return,
                "pred_price":                pred_price,
                "actual_price":              round(sp, 1) if sp else None,
                "implied_per":               implied_per,
                "implied_pbr":               implied_pbr,
                # 平均回帰タイミング系
                "expected_gap_6m":           exp_6m,
                "expected_gap_12m":          exp_12m,
                "conv_score_12m":            conv_score_12m,
                "half_life_months":          round(half_life_months, 2),
                "method":                    method,
                "ar1_phi":                   round(phi, 4) if phi is not None else None,
                "n_history":                 len(series),
            })

        if sort == "total_return":
            results.sort(key=lambda x: x["expected_total_return_pct"], reverse=True)
        else:
            results.sort(key=lambda x: x["gap_ratio"], reverse=(sort == "desc"))
        return {
            "count": len(results),
            "n_ar1_estimated": n_ar1,
            "n_heuristic_fallback": n_heuristic,
            "results": results,
            "regression": regression,
        }

    def _regression_meta(self, db, year) -> dict:
        """回帰結果（regression_results）の鮮度・使用モデルを要約する。

        - computed_at      : 回帰の最終計算時刻（最大）
        - data_updated_at  : 財務データの最終更新時刻（最大）
        - is_stale         : 回帰がデータ更新より古い（再実行が望ましい）
        - models           : 当該フィルタで使われたモデル（ols/ridge 混在の検出）
        """
        from database import RegressionResult, FinancialRecord
        from sqlalchemy import func

        rq = db.query(RegressionResult).filter(RegressionResult.gap_ratio.isnot(None))
        if year:
            rq = rq.filter(RegressionResult.year == year)
        computed_at = rq.with_entities(func.max(RegressionResult.computed_at)).scalar()
        models = sorted(
            m for (m,) in rq.with_entities(RegressionResult.model).distinct().all() if m
        )
        data_updated_at = db.query(func.max(FinancialRecord.updated_at)).scalar()
        is_stale = bool(computed_at and data_updated_at and computed_at < data_updated_at)
        return {
            "computed_at":     computed_at.isoformat() if computed_at else None,
            "data_updated_at": data_updated_at.isoformat() if data_updated_at else None,
            "is_stale":        is_stale,
            "models":          models,
        }


plugin = GapAnalysisPlugin()
