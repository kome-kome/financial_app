import math
from typing import Any
from .base import AnalysisPlugin


class GapAnalysisPlugin(AnalysisPlugin):
    name = "gap_analysis"
    label = "乖離分析"
    description = "業種別OLS分析で算出した理論時価総額との乖離率を表示します（先に業種別OLS分析を実行してください）"
    depends_on = ["sector_ols"]

    def params_schema(self) -> dict:
        return {
            "year": {
                "type": "number",
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
        from database import FinancialRecord

        year = params.get("year")
        sort = params.get("sort", "asc")

        query = db.query(FinancialRecord).filter(FinancialRecord.gap_ratio.isnot(None))
        if year:
            query = query.filter(FinancialRecord.year == int(year))
        records = query.all()

        if not records:
            raise ValueError("先に業種別OLS分析を実行してください")

        results = []
        for r in records:
            gap = r.gap_ratio or 0
            # 収束予測はOU過程の簡易ヒューリスティック（統計モデルではなく参考値）
            half_life_months = max(6, min(24, abs(gap) / 2))
            exp_6m  = round(gap * math.exp(-0.693 / half_life_months * 6), 2)
            exp_12m = round(gap * math.exp(-0.693 / half_life_months * 12), 2)
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
            })

        results.sort(key=lambda x: x["gap_ratio"], reverse=(sort == "desc"))
        return {"count": len(results), "results": results}


plugin = GapAnalysisPlugin()
