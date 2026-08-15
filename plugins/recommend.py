from typing import Any
from collections import defaultdict, namedtuple
from sqlalchemy import func
from .base import AnalysisPlugin


METRICS = [
    "z_roe", "z_op_margin", "z_revenue", "z_cf_ratio",
    "z_equity_ratio", "z_eps", "gap_ratio", "z_de_ratio", "z_momentum", "mu",
]

# financial_metrics VIEW の列ではなく実行時計算で埋める指標（SELECT_COLS から除く）。
# z_momentum = 週次株価から都度計算（compute_momentum_z）
# mu         = μ producer（M-1/M-2/M-3/M-4/M-6）の永続化スコア（compute_mu_z・Issue #423 子4）
RUNTIME_METRICS = ("z_momentum", "mu")

# μ̂ の出所（sell_ranking の mu_source と同じ producer 集合・ADR-0004）。
# 買い側の既定は **None＝μ̂ を使わない**（Issue #423 子4）。売り側は既定 macro_enet だが、
# 買い側 rank-IC と売り側 spread は順位が逆転しうる（ADR-0022）ため、買いの既定重みへ
# 無検証で持ち込まない。既定を動かすときは ADR-0028 の昇格ゲート（増減どちらも補正後αを
# 通す実測）を必ず通すこと。
MU_SOURCE_OPTIONS = [
    {"value": "",                  "label": "使わない（既定）"},
    {"value": "macro_risk_return", "label": "M-1: マクロ×リスク-リターン（OLS）"},
    {"value": "macro_gbdt",        "label": "M-2: マクロ×財務 勾配ブースティング（XGBoost）"},
    {"value": "macro_dlm",         "label": "M-3: ベイズ状態空間（時変マクロβ DLM）"},
    {"value": "macro_ensemble",    "label": "M-4: 兄弟μ̂スタッキング（M-1+M-2 統合）"},
    {"value": "macro_enet",        "label": "M-6: マクロ×財務 正則化線形（ElasticNet）"},
]

PRESETS = {
    "バランス型":  {"z_roe": 1.0, "z_op_margin": 1.0, "z_revenue": 0.8, "z_cf_ratio": 0.8, "z_equity_ratio": 0.5, "gap_ratio": 0.5, "z_momentum": 0.5},
    "成長重視":    {"z_revenue": 2.0, "z_roe": 1.0, "z_op_margin": 0.5, "z_cf_ratio": 0.5, "gap_ratio": 0.3},
    "割安重視":    {"gap_ratio": 2.0, "z_roe": 1.0, "z_op_margin": 1.0, "z_equity_ratio": 0.5},
    "高収益重視":  {"z_roe": 2.0, "z_op_margin": 2.0, "z_cf_ratio": 1.0, "z_equity_ratio": 0.5},
}

# 表示・フィルタで使う financial_metrics 列（Issue #441）。VIEW は97列あるが recommend が
# 読むのはここと METRICS だけで、全列×全社（実測 4,430 行）を転送すると本番 Supabase 実測
# 3.00s、必要列だけなら 1.16s。Render の 30秒リクエスト上限は有料プランでも変わらないため、
# 余裕は列を絞って確保する。
_DISPLAY_COLS = (
    "edinet_code", "sec_code", "company_name", "industry", "year",
    "market_cap", "per", "pbr", "roe", "op_margin", "rev_growth",
)

# 実際に SELECT する列。METRICS から導出するので、指標を METRICS へ足せば転送列も自動で
# 追従する（列リストを二重管理しない）。RUNTIME_METRICS は VIEW 列ではなく実行時計算なので除く。
# weights のキーは coerce_params が METRICS へ制限するため、ここに無い列は読まれない。
SELECT_COLS = tuple(dict.fromkeys(
    _DISPLAY_COLS + tuple(m for m in METRICS if m not in RUNTIME_METRICS)))

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
    組み立てる（recommend_factor_premia.py --persist が書き込む・未実行ならNone）。

    METRICS 外の factor は採らない（Issue #441）。この重みは coerce_params を通らない
    経路（execute 内の resolve_weights）で使われる一方、読み取り列は METRICS から導出
    するため、範囲外のキーが混ざると値が取れず黙って None 扱い＝カバレッジが下がる。
    全て範囲外なら None を返してバランス型へフォールバックさせる。

    mu は対象外（Issue #423 子4）。Fama-MacBeth の断面回帰は財務・株価由来の factor だけを
    推定しており（`recommend_factor_premia.build_period_panel` が RUNTIME_METRICS を除外）
    μ̂ の premium は存在しない。仮に混ざると mu_source 未指定の実行を reject させてしまい、
    「統計的最適化」プリセットが選べなくなるため、ここで構造的に落とす。
    """
    from database import get_latest_factor_premia
    premia = get_latest_factor_premia(db)
    if not premia:
        return None
    weights = {factor: vals["mean_b"] for factor, vals in premia.items()
               if factor in METRICS and factor != "mu"}
    return weights or None


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


def load_producer_mu(db: Any, mu_source: str) -> dict:
    """mu_source の producer から {edinet_code: μ̂} を読む（未実行・失敗なら空 dict）。

    sell_ranking.execute の μ 読み出しと同じ契約（ADR-0004）: producer は
    `read_producer_scores(db, macro_snapshot) -> {edinet_code: {mu, r_macro, r1_prime}}`
    を返す。買い側が使うのは mu のみ（r_macro は売り側の −Rᴹ 観点専用、r1_prime は
    売り側 R3 足切り専用）。

    macro_snapshot は共有 macro_beta の selected_factors 分だけを当日値で渡す
    （M-1 の μ̂ 再構成に必要・他モデルは無視する）。producer 未実行・DB 未 migration・
    マクロ欠測はいずれも graceful-degrade（空 dict → μ̂ 抜きでスコアリング継続）。
    """
    import datetime as _dt
    from plugins import get_plugin as _get_plugin

    producer = _get_plugin(mu_source)
    if producer is None or not producer.produced_output(db):
        return {}
    try:
        from database import get_macro_beta
        from .utils import get_macro_features
        _meta_m1, _ = get_macro_beta(db, with_loadings=False)   # selected_factors だけ（#482）
        sel_factors = (_meta_m1 or {}).get("selected_factors") or []
        macro_snap: dict | None = None
        if sel_factors:
            raw_snap = get_macro_features(db, _dt.date.today().isoformat(), sel_factors)
            macro_snap = {k: v for k, v in raw_snap.items() if v is not None} or None
        scores = producer.read_producer_scores(db, macro_snap)
    except Exception:
        return {}
    return {ec: float(ps["mu"]) for ec, ps in (scores or {}).items()
            if ps.get("mu") is not None}


def compute_mu_z(db: Any, mu_source: str, edinet_codes: list) -> dict:
    """producer μ̂ を候補集団横断でZスコア化する（Issue #423 子4）。

    μ̂ は週次リターンの期待値[小数]で、他指標（z_*）とスケールが2桁違う。加重和
    Σ(w·z)/Σ|w| は指標が同一スケールであることを前提にしているため、compute_momentum_z
    と同じ winsorize(p1-p99)→Zスコアで揃えてから重みを掛ける。

    標準化の母集団は**フィルタ適用後の候補集団**（compute_momentum_z と同一基準）。
    業種・時価総額で絞ったときは絞った集団内での相対順位になる＝同一画面内の他指標と
    基準が揃う。売り側（sell_ranking）は保有銘柄がユニバースの一部でしかないため
    producer 全体を母集団に取っており、そちらとは基準が異なる（意図的）。

    有効サンプルが4件未満なら空 dict（winsorize が機能しない・compute_momentum_z と同じ）。
    """
    if not mu_source or not edinet_codes:
        return {}
    from .utils import normalize_transform, winsorize

    raw_all = load_producer_mu(db, mu_source)
    if not raw_all:
        return {}
    wanted = set(edinet_codes)
    raw = {ec: v for ec, v in raw_all.items() if ec in wanted}
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
                "description": "各指標の重要度（-2〜3）。z_de_ratioは負ウェイト推奨。muはmu_source必須",
            },
            "mu_source": {
                "type": "select",
                "label": "μ̂（期待リターン）の出所",
                "options": MU_SOURCE_OPTIONS,
                "default": None,           # 既定は μ̂ 不使用（既存プリセットは mu 重み 0）
                "optional": True,
                "description": "指標ウェイトの mu に重みを付けたときだけ使う推奨モデル。"
                               "未指定のまま mu へ重みを付けると reject（黙って無視しない）。",
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
        mu_source    = params["mu_source"]

        # μ̂ を使う意思表示（mu 重み≠0）があるのに出所未指定なら reject（Issue #423 子4）。
        # 黙って None 扱いにすると「重みを付けたのに効いていない」状態が画面から見分けられず、
        # カバレッジだけが静かに下がる（設計制約の fail fast と同じ思想）。
        if weights.get("mu") and not mu_source:
            raise ValueError(
                "'mu'（μ̂）に重みを付けるときは mu_source を指定してください"
                "（指定可能: " + ", ".join(o["value"] for o in MU_SOURCE_OPTIONS if o["value"]) + "）")

        # UI表示用: 動的プリセット（統計的最適化）が算出済みならPRESETSへマージして返す
        # （フロントエンドのプリセット切替は presets[name] を直接参照するため無改修で動く）。
        all_presets = get_all_presets(db)

        # 重み総和（絶対値ベース）。カバレッジ計算と正規化に使う
        total_weight = sum(abs(w) for w in weights.values())
        if total_weight == 0:
            return {"count": 0, "total_candidates": 0, "presets": all_presets,
                    "metrics": METRICS, "results": [],
                    "mu_source": mu_source, "mu_available": False, "mu_asof": None}

        subq = latest_year_subq(db, FinancialMetric)
        # SELECT は SELECT_COLS だけ（Issue #441）。is_active は WHERE でしか使わないので
        # 転送しない。戻りは ORM インスタンスではなく Row タプル＝絞っていない列を後から
        # 読むことが構造的に起きない。
        query = (db.query(*[getattr(FinancialMetric, c) for c in SELECT_COLS])
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

        candidate_codes = [r.edinet_code for r in records if r.edinet_code]

        momentum_z = {}
        if "z_momentum" in weights:
            momentum_z = compute_momentum_z(db, candidate_codes, date.today().isoformat())

        # μ̂（producer スコア）。mu へ重みが無ければ producer を読まない＝既定経路のコストは 0。
        mu_z = {}
        mu_asof = None
        if weights.get("mu") and mu_source:
            mu_z = compute_mu_z(db, mu_source, candidate_codes)
            if mu_z:
                # μ̂ の as-of（銘柄ごとの最終週次バー基準・#417）。UI の鮮度表示と /api/morning が読む。
                try:
                    from database import get_producer_asof
                    mu_asof = get_producer_asof(db, mu_source)
                except Exception:
                    mu_asof = None

        scored = []
        skipped_low_coverage = 0
        for r in records:
            weighted_sum = 0.0
            weight_present = 0.0
            detail = {}
            for metric, weight in weights.items():
                if metric == "z_momentum":
                    val = momentum_z.get(r.edinet_code)
                elif metric == "mu":
                    val = mu_z.get(r.edinet_code)
                else:
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

        # 株価 as-of（Issue #416）。z_momentum も PER/PBR も株価由来なので、株価が
        # 止まっていればスコア全体が静かに古くなる。行ごとの齢を返して UI で見せる。
        # 母集団の分位・鮮度レベルは DB 側集約だけで出し（全銘柄の MAX を転送しない）、
        # 行ごとの as-of は画面に出る上位 top_n 社だけ引く（Issue #441）。
        price = price_freshness(db)

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_n]
        asof_by_code = price_asof_by_code(
            db, [r.edinet_code for _, _, r, _ in top if r.edinet_code])

        results = []
        for rank, (score, coverage, r, detail) in enumerate(top, 1):
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
            # μ̂ の結線状態（Issue #423 子4）。mu_available=False は「重みを付けたが producer
            # 未実行 or 候補集団に4件未満」＝ graceful-degrade したことを画面へ明示するための旗。
            "mu_source":        mu_source,
            "mu_available":     bool(mu_z),
            "mu_asof":          mu_asof,
            "results":          results,
        }


plugin = RecommendPlugin()
