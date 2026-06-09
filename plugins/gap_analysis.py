import math
from collections import defaultdict
from typing import Any

from .base import AnalysisPlugin

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
    except Exception:
        return None

    # statsmodels ARIMA の AR(1) 係数は res.arparams[0] = φ
    try:
        phi = float(res.arparams[0])
        intercept = float(res.params[0])  # 定数項
    except Exception:
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
    name = "gap_analysis"
    label = "乖離分析"
    description = (
        "業種別OLS分析で算出した理論時価総額との乖離率を表示します"
        "（先に業種別OLS分析を実行してください）。"
        "履歴データが十分にある銘柄は AR(1) MLE で平均回帰速度を推定し、"
        "不足する銘柄はヒューリスティック（参考値）にフォールバックします。"
    )
    depends_on = ["sector_ols"]

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
                    {"value": "asc",  "label": "乖離率 低い順（割安）"},
                    {"value": "desc", "label": "乖離率 高い順（割高）"},
                ],
                "default": "asc",
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        # gap_ratio / predicted_market_cap は regression_results 由来。
        # financial_metrics VIEW が LEFT JOIN して合成するため VIEW を読む。
        from database import FinancialMetric

        # params はパラメータ契約に従い coerce 済み（year:int|None・sort:str）。
        year = params["year"]
        sort = params["sort"]

        # 当該フィルタの最新スナップショット
        query = db.query(FinancialMetric).filter(FinancialMetric.gap_ratio.isnot(None))
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
                "edinet_code":           r.edinet_code,
                "sec_code":              r.sec_code,
                "company_name":          r.company_name,
                "industry":              r.industry,
                "actual_market_cap":     r.market_cap,
                "predicted_market_cap":  r.predicted_market_cap,
                "gap_ratio":             gap,
                "expected_gap_6m":       exp_6m,
                "expected_gap_12m":      exp_12m,
                "conv_score_12m":        conv_score_12m,
                "half_life_months":      round(half_life_months, 2),
                "method":                method,
                "ar1_phi":               round(phi, 4) if phi is not None else None,
                "n_history":             len(series),
            })

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
