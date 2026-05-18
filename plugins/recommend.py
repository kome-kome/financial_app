from typing import Any
from sqlalchemy import func
from .base import AnalysisPlugin


METRICS = [
    "z_roe", "z_op_margin", "z_revenue", "z_cf_ratio",
    "z_equity_ratio", "z_eps", "gap_ratio", "z_de_ratio",
]

PRESETS = {
    "バランス型":  {"z_roe": 1.0, "z_op_margin": 1.0, "z_revenue": 0.8, "z_cf_ratio": 0.8, "z_equity_ratio": 0.5, "gap_ratio": 0.5},
    "成長重視":    {"z_revenue": 2.0, "z_roe": 1.0, "z_op_margin": 0.5, "z_cf_ratio": 0.5, "gap_ratio": 0.3},
    "割安重視":    {"gap_ratio": 2.0, "z_roe": 1.0, "z_op_margin": 1.0, "z_equity_ratio": 0.5},
    "高収益重視":  {"z_roe": 2.0, "z_op_margin": 2.0, "z_cf_ratio": 1.0, "z_equity_ratio": 0.5},
}


class RecommendPlugin(AnalysisPlugin):
    name = "recommend"
    label = "おすすめ銘柄"
    description = "Zスコア指標を重み付けスコアリングしてランキング表示します"
    depends_on = []

    def params_schema(self) -> dict:
        return {
            "preset": {
                "type": "select",
                "label": "プリセット",
                "options": [{"value": k, "label": k} for k in PRESETS],
                "default": "バランス型",
                "description": "カスタムウェイトを使う場合は「カスタム」を選択",
            },
            "weights": {
                "type": "weights",
                "label": "カスタムウェイト",
                "metrics": METRICS,
                "default": PRESETS["バランス型"],
                "description": "各指標の重要度（-2〜3）。z_de_ratioは負ウェイト推奨",
            },
            "top_n": {
                "type": "slider",
                "label": "表示件数",
                "min": 10, "max": 100, "step": 10,
                "default": 30,
            },
            "year": {
                "type": "number",
                "label": "対象年度（空=最新）",
                "default": None,
                "optional": True,
            },
            "industry": {
                "type": "text",
                "label": "業種フィルタ（空=全業種）",
                "default": None,
                "optional": True,
            },
            "min_market_cap": {
                "type": "number",
                "label": "最低時価総額（百万円）",
                "default": None,
                "optional": True,
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        from database import FinancialRecord

        preset       = params.get("preset", "バランス型")
        weights      = params.get("weights") or PRESETS.get(preset, PRESETS["バランス型"])
        top_n        = int(params.get("top_n", 30))
        year         = params.get("year")
        industry     = params.get("industry")
        min_market_cap = params.get("min_market_cap")

        subq = (db.query(FinancialRecord.edinet_code,
                         func.max(FinancialRecord.year).label("max_year"))
                  .group_by(FinancialRecord.edinet_code).subquery())
        query = (db.query(FinancialRecord)
                   .join(subq, (FinancialRecord.edinet_code == subq.c.edinet_code) &
                               (FinancialRecord.year == subq.c.max_year)))
        if year:
            query = query.filter(FinancialRecord.year == int(year))
        if industry:
            query = query.filter(FinancialRecord.industry == industry)
        if min_market_cap is not None:
            query = query.filter(FinancialRecord.market_cap >= float(min_market_cap))

        records = query.all()
        scored = []
        for r in records:
            score, detail, has_any = 0.0, {}, False
            for metric, weight in weights.items():
                val = getattr(r, metric, None)
                if val is not None:
                    score += weight * val
                    has_any = True
                detail[metric] = round(val, 4) if val is not None else None
            if has_any:
                scored.append((score, r, detail))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for rank, (score, r, detail) in enumerate(scored[:top_n], 1):
            results.append({
                "rank":         rank,
                "edinet_code":  r.edinet_code,
                "sec_code":     r.sec_code,
                "company_name": r.company_name,
                "industry":     r.industry,
                "year":         r.year,
                "score":        round(score, 4),
                "market_cap":   r.market_cap,
                "per":          r.per,
                "pbr":          r.pbr,
                "roe":          r.roe,
                "op_margin":    r.op_margin,
                "rev_growth":   r.rev_growth,
                "gap_ratio":    r.gap_ratio,
                "detail":       detail,
            })

        return {
            "count":            len(results),
            "total_candidates": len(scored),
            "presets":          PRESETS,
            "metrics":          METRICS,
            "results":          results,
        }


plugin = RecommendPlugin()
