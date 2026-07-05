from typing import Any
from collections import defaultdict, namedtuple
from sqlalchemy import func
from .base import AnalysisPlugin


METRICS = [
    "z_roe", "z_op_margin", "z_revenue", "z_cf_ratio",
    "z_equity_ratio", "z_eps", "gap_ratio", "z_de_ratio", "z_momentum",
]

PRESETS = {
    "バランス型":  {"z_roe": 1.0, "z_op_margin": 1.0, "z_revenue": 0.8, "z_cf_ratio": 0.8, "z_equity_ratio": 0.5, "gap_ratio": 0.5, "z_momentum": 0.5},
    "成長重視":    {"z_revenue": 2.0, "z_roe": 1.0, "z_op_margin": 0.5, "z_cf_ratio": 0.5, "gap_ratio": 0.3},
    "割安重視":    {"gap_ratio": 2.0, "z_roe": 1.0, "z_op_margin": 1.0, "z_equity_ratio": 0.5},
    "高収益重視":  {"z_roe": 2.0, "z_op_margin": 2.0, "z_cf_ratio": 1.0, "z_equity_ratio": 0.5},
}

_MomentumPX = namedtuple("_MomentumPX", "trade_date close")


def compute_momentum_z(db: Any, edinet_codes: list, as_of_date: str) -> dict:
    """12-1モメンタム（get_momentum_return）を候補集団横断でZスコア化する。

    z_momentum は financial_metrics VIEW の列ではなく実行時計算（sell_ranking の
    _resolve_metric と同じ方式）。モメンタムは週次で更新される価格由来データで、
    VIEW の年度別Zスコアとは cadence が異なるため。

    as_of_date 以前の StockPriceWeekly のみ参照するため、backtest の as-of 検証でも
    リークしない（get_momentum_return 自体も ref_date 以前でフィルタする二重の安全策）。
    有効サンプルが4件未満の場合は winsorize が機能しないため空 dict を返す
    （呼び出し側では他の欠損指標と同様 None 扱いになる）。
    """
    if not edinet_codes:
        return {}
    from database import StockPriceWeekly
    from .utils import get_momentum_return, winsorize, normalize_transform

    rows = (
        db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                  StockPriceWeekly.close_last)
          .filter(StockPriceWeekly.edinet_code.in_(edinet_codes),
                  StockPriceWeekly.trade_date <= as_of_date)
          .order_by(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date)
          .all()
    )
    price_rows_by_ec = defaultdict(list)
    for ec, td, cl in rows:
        price_rows_by_ec[ec].append(_MomentumPX(td, cl))

    raw = {}
    for ec, price_rows in price_rows_by_ec.items():
        m = get_momentum_return(price_rows, as_of_date)
        if m is not None:
            raw[ec] = m
    if len(raw) < 4:
        return {}

    vals = list(raw.values())
    wv, _, _ = winsorize(vals)
    mean_ = sum(wv) / len(wv)
    var = sum((v - mean_) ** 2 for v in wv) / (len(wv) - 1)
    sd = var ** 0.5 or 1.0
    return {ec: normalize_transform(v, mean_, sd, "zscore") for ec, v in raw.items()}


class RecommendPlugin(AnalysisPlugin):
    name = "recommend"
    label = "おすすめ銘柄"
    description = "Zスコア指標を重み付けスコアリングしてランキング表示します"
    depends_on = []
    category = "① 銘柄を探す"
    ui_order = 110

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
                "default": None,
                "optional": True,   # 未指定なら execute が preset の重みにフォールバック
                "description": "各指標の重要度（-2〜3）。z_de_ratioは負ウェイト推奨",
            },
            "top_n": {
                "type": "slider",
                "dtype": "int",
                "label": "表示件数",
                "min": 10, "max": 100, "step": 10,
                "default": 30,
            },
            "min_coverage": {
                "type": "slider",
                "dtype": "float",
                "label": "必須指標カバレッジ（0-1）",
                "min": 0.0, "max": 1.0, "step": 0.1,
                "default": 0.5,
                "description": "重み付き指標のうち、値が揃っている比率の下限。1.0=全指標必須。",
            },
            "year": {
                "type": "number",
                "dtype": "int",
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
                "dtype": "float",
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
        # z_momentum のみ VIEW 外の実行時計算（compute_momentum_z）。
        from database import FinancialMetric, latest_year_subq
        from datetime import date

        # params はパラメータ契約に従い coerce 済み。weights 未指定時は preset の重みへ。
        preset       = params["preset"]
        weights      = params["weights"] or PRESETS.get(preset, PRESETS["バランス型"])
        top_n        = params["top_n"]
        min_coverage = params["min_coverage"]
        year         = params["year"]
        industry     = params["industry"]
        min_market_cap = params["min_market_cap"]

        # 重み総和（絶対値ベース）。カバレッジ計算と正規化に使う
        total_weight = sum(abs(w) for w in weights.values())
        if total_weight == 0:
            return {"count": 0, "total_candidates": 0, "presets": PRESETS,
                    "metrics": METRICS, "results": []}

        subq = latest_year_subq(db, FinancialMetric)
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

        momentum_z = {}
        if "z_momentum" in weights:
            momentum_z = compute_momentum_z(
                db, [r.edinet_code for r in records if r.edinet_code],
                date.today().isoformat())

        scored = []
        skipped_low_coverage = 0
        for r in records:
            weighted_sum = 0.0
            weight_present = 0.0
            detail = {}
            for metric, weight in weights.items():
                val = momentum_z.get(r.edinet_code) if metric == "z_momentum" else getattr(r, metric, None)
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
