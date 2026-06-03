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
            "min_coverage": {
                "type": "slider",
                "label": "必須指標カバレッジ（0-1）",
                "min": 0.0, "max": 1.0, "step": 0.1,
                "default": 0.5,
                "description": "重み付き指標のうち、値が揃っている比率の下限。1.0=全指標必須。",
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
        """重み付き指標スコアでランキング。

        スコア計算: weighted mean を用いる。
          score = Σ(w_i × z_i) / Σ|w_i|   (i は値が存在する指標のみ)
        これにより指標カバレッジが異なる銘柄を公平に比較できる。
        min_coverage は重み付き指標のうち値が存在する比率（重み総和ベース）の下限。
        """
        # Zスコア・gap_ratio・派生指標は financial_metrics VIEW が都度算出/合成する。
        from database import FinancialMetric

        preset       = params.get("preset", "バランス型")
        weights      = params.get("weights") or PRESETS.get(preset, PRESETS["バランス型"])
        top_n        = int(params.get("top_n", 30))
        min_coverage = float(params.get("min_coverage", 0.5))
        year         = params.get("year")
        industry     = params.get("industry")
        min_market_cap = params.get("min_market_cap")

        # 重み総和（絶対値ベース）。カバレッジ計算と正規化に使う
        total_weight = sum(abs(w) for w in weights.values())
        if total_weight == 0:
            return {"count": 0, "total_candidates": 0, "presets": PRESETS,
                    "metrics": METRICS, "results": []}

        subq = (db.query(FinancialMetric.edinet_code,
                         func.max(FinancialMetric.year).label("max_year"))
                  .group_by(FinancialMetric.edinet_code).subquery())
        query = (db.query(FinancialMetric)
                   .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                               (FinancialMetric.year == subq.c.max_year)))
        if year:
            query = query.filter(FinancialMetric.year == int(year))
        if industry:
            query = query.filter(FinancialMetric.industry == industry)
        if min_market_cap is not None:
            query = query.filter(FinancialMetric.market_cap >= float(min_market_cap))

        records = query.all()
        scored = []
        skipped_low_coverage = 0
        for r in records:
            weighted_sum = 0.0
            weight_present = 0.0
            detail = {}
            for metric, weight in weights.items():
                val = getattr(r, metric, None)
                if val is not None:
                    weighted_sum += weight * val
                    weight_present += abs(weight)
                detail[metric] = round(val, 4) if val is not None else None
            coverage = weight_present / total_weight if total_weight > 0 else 0.0
            if coverage < min_coverage:
                skipped_low_coverage += 1
                continue
            if weight_present == 0:
                continue
            score = weighted_sum / weight_present
            scored.append((score, coverage, r, detail))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for rank, (score, coverage, r, detail) in enumerate(scored[:top_n], 1):
            results.append({
                "rank":         rank,
                "edinet_code":  r.edinet_code,
                "sec_code":     r.sec_code,
                "company_name": r.company_name,
                "industry":     r.industry,
                "year":         r.year,
                "score":        round(score, 4),
                "coverage":     round(coverage, 2),
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
            "skipped_low_coverage": skipped_low_coverage,
            "min_coverage":     min_coverage,
            "presets":          PRESETS,
            "metrics":          METRICS,
            "results":          results,
        }


plugin = RecommendPlugin()
