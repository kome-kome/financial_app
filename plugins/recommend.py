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

# 週次株価ロードの遡及上限日数（Issue #418）。get_momentum_return は long_months=12＝
# as_of - 360日 までしか参照しないため、余裕1ヶ月を足した 400 日で情報損失はゼロ。
# 下限なしの全期間ロード（本番 stock_price_weekly は実測 1,271,282 行）は Supabase pooler の
# statement_timeout(2min) を踏んで接続を壊す（docs/GOTCHAS.md・Issue #311 と同型）。
_MOMENTUM_LOOKBACK_DAYS = 400

# edinet_code の IN 句チャンクサイズ（Issue #311/#418）。候補全社（実測 3,677）を単一クエリへ
# 一括バインドするとプランナ負荷が上がるため、macro_snapshots.load_weekly_prices_chunked と
# 同じ 500 社ずつに分割する。
_MOMENTUM_CODE_BATCH = 500

# データ駆動プリセット名（Issue #271）。PRESETS には含めず、recommend_factor_premia.py が
# 永続化した最新のFama-MacBethファクタープレミアムから resolve_weights() が動的に組み立てる。
STATISTICAL_PRESET_NAME = "統計的最適化"


def get_dynamic_preset(db: Any) -> dict | None:
    """DB永続化済みのFama-MacBethファクタープレミアムから統計的最適化プリセットの重みを
    組み立てる（recommend_factor_premia.py --persist が書き込む・未実行ならNone）。"""
    from database import get_latest_factor_premia
    premia = get_latest_factor_premia(db)
    if not premia:
        return None
    return {factor: vals["mean_b"] for factor, vals in premia.items()}


def get_all_presets(db: Any) -> dict:
    """UI表示用: 静的PRESETSに動的プリセット（算出済みなら）をマージして返す。

    recommend.execute() と GET /api/recommend/presets が共用する（両者とも
    フロントエンドのプリセット切替UIが参照する presets 辞書を返す必要があるため）。
    """
    dynamic_preset = get_dynamic_preset(db)
    if dynamic_preset is not None:
        return {**PRESETS, STATISTICAL_PRESET_NAME: dynamic_preset}
    return PRESETS


def resolve_weights(db: Any, preset_name: str) -> dict:
    """プリセット名から重みdictを解決する。

    静的PRESETSに一致すればそれを返す。STATISTICAL_PRESET_NAMEはDBの動的プリセットから
    解決し、未実行（データなし）ならバランス型へフォールバックする（recommend/backtest共用）。
    """
    if preset_name in PRESETS:
        return PRESETS[preset_name]
    if preset_name == STATISTICAL_PRESET_NAME:
        dynamic = get_dynamic_preset(db)
        if dynamic is not None:
            return dynamic
    return PRESETS["バランス型"]


def compute_momentum_z(db: Any, edinet_codes: list, as_of_date: str) -> dict:
    """12-1モメンタム（get_momentum_return）を候補集団横断でZスコア化する。

    z_momentum は financial_metrics VIEW の列ではなく実行時計算（sell_ranking の
    _resolve_metric と同じ方式）。モメンタムは週次で更新される価格由来データで、
    VIEW の年度別Zスコアとは cadence が異なるため。

    as_of_date 以前の StockPriceWeekly のみ参照するため、backtest の as-of 検証でも
    リークしない（get_momentum_return 自体も ref_date 以前でフィルタする二重の安全策）。
    有効サンプルが4件未満の場合は winsorize が機能しないため空 dict を返す
    （呼び出し側では他の欠損指標と同様 None 扱いになる）。

    ロードは as_of - _MOMENTUM_LOOKBACK_DAYS の下限付き＋_MOMENTUM_CODE_BATCH 社ずつの
    チャンクで行う（Issue #418）。下限は week_start（PK の第2列）へ掛けることで
    (edinet_code, week_start) 複合インデックスの範囲スキャンになる。trade_date は
    nullable かつ非インデックス列なので、上限側（リークガード）だけに使う。

    **転送するのは各社2行だけ**（各 cutoff 以下の最終バー・Issue #423 子3）。全期間を
    引いていた頃は約4,000社 × 約57週 ≈ 23万行を毎回転送し、本番実測で `/api/recommend`
    が 37.9秒＝**Render の 30秒リクエスト上限を超えていた**（z_momentum を外すと 3.65秒
    だったため犯人はここと特定）。`get_momentum_return` は「cutoff 以下の最終バー」しか
    見ないので、行を絞っても結果は変わらない（両脚が同一バーなら None を返す #430 の
    ガードもそのまま効く）。Supabase の Egress 節約にもなる。
    """
    if not edinet_codes:
        return {}
    from datetime import date as _date, timedelta as _td
    from sqlalchemy import func as _sqla_func
    from database import StockPriceWeekly, iso_week_start
    from .utils import (get_momentum_return, momentum_cutoffs, winsorize,
                        normalize_transform)

    week_from = iso_week_start(
        (_date.fromisoformat(as_of_date) - _td(days=_MOMENTUM_LOOKBACK_DAYS)).isoformat())
    short_cutoff, long_cutoff = momentum_cutoffs(as_of_date)
    codes = list(edinet_codes)
    price_rows_by_ec = defaultdict(list)

    def _latest_before(chunk: list, cutoff: str) -> list:
        """chunk 各社について trade_date <= cutoff の最終バー1本を返す。"""
        rn = _sqla_func.row_number().over(
            partition_by=StockPriceWeekly.edinet_code,
            order_by=StockPriceWeekly.trade_date.desc(),
        ).label("rn")
        sub = (
            db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                     StockPriceWeekly.close_last, rn)
              .filter(StockPriceWeekly.edinet_code.in_(chunk),
                      StockPriceWeekly.week_start >= week_from,
                      StockPriceWeekly.trade_date <= cutoff,
                      StockPriceWeekly.close_last.isnot(None),
                      StockPriceWeekly.close_last > 0)
              .subquery()
        )
        return db.query(sub.c.edinet_code, sub.c.trade_date, sub.c.close_last) \
                 .filter(sub.c.rn == 1).all()

    for i in range(0, len(codes), _MOMENTUM_CODE_BATCH):
        chunk = codes[i:i + _MOMENTUM_CODE_BATCH]
        seen = set()
        # long → short の順に積む（get_momentum_return は昇順を前提にしないが、
        # 同一バーが両脚に解決されるケースを重複させないため集合で弾く）
        for cutoff in (long_cutoff, short_cutoff):
            for ec, td, cl in _latest_before(chunk, cutoff):
                if (ec, td) in seen:
                    continue
                seen.add((ec, td))
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
                "options": [{"value": k, "label": k} for k in PRESETS]
                           + [{"value": STATISTICAL_PRESET_NAME, "label": STATISTICAL_PRESET_NAME}],
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

    def execute(self, params: dict, db: Any) -> dict:
        """重み付き指標スコアでランキング。

        スコア計算: weighted mean を用いる。
          score = Σ(w_i × z_i) / Σ|w_i|   (i は値が存在する指標のみ)
        これにより指標カバレッジが異なる銘柄を公平に比較できる。
        min_coverage は重み付き指標のうち値が存在する比率（重み総和ベース）の下限。
        """
        # Zスコア・gap_ratio・派生指標は financial_metrics VIEW が都度算出/合成する。
        # z_momentum のみ VIEW 外の実行時計算（compute_momentum_z）。
        from database import (FinancialMetric, latest_year_subq,
                              price_asof_by_code, price_freshness)
        from datetime import date

        # params はパラメータ契約に従い coerce 済み。weights 未指定時は preset の重みへ。
        preset       = params["preset"]
        weights      = params["weights"] or resolve_weights(db, preset)
        top_n        = params["top_n"]
        min_coverage = params["min_coverage"]
        year         = params["year"]
        industry     = params["industry"]
        min_market_cap = params["min_market_cap"]

        # UI表示用: 動的プリセット（統計的最適化）が算出済みならPRESETSへマージして返す
        # （フロントエンドのプリセット切替は presets[name] を直接参照するため無改修で動く）。
        all_presets = get_all_presets(db)

        # 重み総和（絶対値ベース）。カバレッジ計算と正規化に使う
        total_weight = sum(abs(w) for w in weights.values())
        if total_weight == 0:
            return {"count": 0, "total_candidates": 0, "presets": all_presets,
                    "metrics": METRICS, "results": []}

        subq = latest_year_subq(db, FinancialMetric)
        query = (db.query(FinancialMetric)
                   .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                               (FinancialMetric.year == subq.c.max_year))
                   # 上場廃止銘柄は買えないため対象外（Issue #315）。is_active 未設定（旧データ）は
                   # 対象に含める（isnot(False) で NULL を許容）。
                   .filter(FinancialMetric.is_active.isnot(False)))
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

        # 株価 as-of（Issue #416）。z_momentum も PER/PBR も株価由来なので、株価が
        # 止まっていればスコア全体が静かに古くなる。行ごとの齢を返して UI で見せる。
        asof_by_code = price_asof_by_code(db)
        price = price_freshness(db, asof_by_code)

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for rank, (score, coverage, r, detail) in enumerate(scored[:top_n], 1):
            results.append({
                "rank":         rank,
                "price_asof":   asof_by_code.get(r.edinet_code),
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
            "presets":          all_presets,
            "metrics":          METRICS,
            "price_freshness":  price,       # as-of 分位・鮮度レベル（Issue #416）
            "results":          results,
        }


plugin = RecommendPlugin()
