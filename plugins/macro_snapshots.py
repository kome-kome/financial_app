"""
plugins/macro_snapshots.py — M-1/M-2 共有スナップショット構築モジュール（ADR-0003 §3）

M-1（macro_risk_return）と M-2（macro_gbdt）の共通面を集約し、M-2→M-1 の
直接結合をゼロにする。utils.py の _MACRO_MAP 遅延 import 循環ハックも解消。

正本として保有するもの:
  - FINANCIAL_LAG_DAYS / HORIZON_WEEKS
  - FIN_BASE_OPTIONS / DEFAULT_FIN_FEATURES
  - _MACRO_MAP / MACRO_FEATURE_NAMES / MACRO_FEATURE_OPTIONS / DEFAULT_MACRO_FEATURES
  - スナップショット構築（build_snapshots / preload_macro / load_data）
  - リーク感応 helpers（_find_applicable_fin / _macro_from_cache / _realized_vol）
  - producer スコア（producer_scores / get_producer_scores）
"""
import contextvars
import math
import statistics
from collections import OrderedDict, defaultdict, namedtuple
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .utils import macro_risk_exposure, normalize, winsorize

# ── 定数 ──────────────────────────────────────────────────────────────────

FINANCIAL_LAG_DAYS = 45
HORIZON_WEEKS = 52

# 財務ベース特徴量の選択肢（全て financial_metrics VIEW の実列）。
FIN_BASE_OPTIONS = [
    {"value": "per",            "label": "PER"},
    {"value": "pbr",            "label": "PBR"},
    {"value": "div_yield",      "label": "配当利回り（%）"},
    {"value": "roe",            "label": "ROE（%）"},
    {"value": "roa",            "label": "ROA（%）"},
    {"value": "op_margin",      "label": "営業利益率（%）"},
    {"value": "net_margin",     "label": "純利益率（%）"},
    {"value": "asset_turnover", "label": "総資産回転率（回）"},
    {"value": "equity_ratio",   "label": "自己資本比率（%）"},
    {"value": "de_ratio",       "label": "D/Eレシオ"},
    {"value": "nc_ratio",       "label": "ネットキャッシュ比率"},
    {"value": "cf_ratio",       "label": "営業CF/売上（%）"},
    {"value": "eps_growth",     "label": "EPS成長率（%）"},
    {"value": "op_growth",      "label": "営業利益成長率（%）"},
    {"value": "rev_growth",     "label": "売上成長率（%）"},
    {"value": "rd_intensity",   "label": "R&D集約度"},
    {"value": "da_intensity",   "label": "D&A集約度"},
    {"value": "z_op_margin",    "label": "営業利益率Zスコア"},
    {"value": "z_roe",          "label": "ROE Zスコア"},
    {"value": "z_cf_ratio",     "label": "CF比率Zスコア"},
]
DEFAULT_FIN_FEATURES = ["per", "pbr", "roe", "equity_ratio", "roa", "eps_growth"]

# feature_name → (series_code, transform: "yoy" | "zscore") の正本。
# utils.py の _macro_feature_map() はここから import する（循環依存ハック解消）。
_MACRO_MAP = {
    "macro_usdjpy_yoy":    ("USDJPY",    "yoy"),
    "macro_eurjpy_yoy":    ("EURJPY",    "yoy"),
    "macro_dxy_yoy":       ("DXY",       "yoy"),
    "macro_sp500_yoy":     ("SP500",     "yoy"),
    "macro_us5y_zscore":   ("US5Y",      "zscore"),
    "macro_us10y_zscore":  ("US10Y",     "zscore"),
    "macro_us30y_zscore":  ("US30Y",     "zscore"),
    "macro_nikkei225_yoy": ("NIKKEI225", "yoy"),
    "macro_topix_yoy":     ("TOPIX",     "yoy"),
    "macro_vix_zscore":    ("VIX",       "zscore"),
    "macro_wti_yoy":       ("WTI",       "yoy"),
    "macro_gold_yoy":      ("GOLD",      "yoy"),
    # ── FRED チャネル（#221・2026-06-24 本番蓄積確認済み） ──────────────────────
    "macro_hy_oas_zscore":       ("HY_OAS",      "zscore"),
    "macro_ig_oas_zscore":       ("IG_OAS",       "zscore"),
    "macro_breakeven10y_zscore": ("BREAKEVEN10Y", "zscore"),
    "macro_jp10y_fred_zscore":   ("JP10Y_FRED",   "zscore"),
    "macro_t10y2y_zscore":       ("T10Y2Y",       "zscore"),
    # ── 日本 実体経済指標（#250・米国偏重の是正）─────────────────────────────────
    # 水準系（GDP・生産指数）は常に正なので yoy。失業率は「率」なので既存金利と同じ
    # zscore 規約。貿易収支は符号がプラス/マイナス両方を取り yoy（除算）が発散するため
    # zscore。低頻度＋公表ラグは収集側（FRED_SERIES.lag_days）で trade_date を補正済み。
    "macro_jp_real_gdp_yoy":     ("JP_REAL_GDP",  "yoy"),
    "macro_jp_unemp_zscore":     ("JP_UNEMP",     "zscore"),
    "macro_jp_trade_bal_zscore": ("JP_TRADE_BAL", "zscore"),
    # ── 鉱工業指数（e-Stat・#253 の FRED 凍結代替・#281）────────────────────────
    # JPNPROINDMISMEI（旧 FRED 系列）は2024-04-30凍結。e-Stat「鉱工業指数」2020年基準・
    # 鉱工業総合を直接取得する代替に切替。生産・在庫とも水準系（常に正）なので yoy。
    "macro_jp_iip_yoy":           ("JP_IIP",           "yoy"),
    "macro_jp_iip_inventory_yoy": ("JP_IIP_INVENTORY", "yoy"),
    # ── 日銀/e-Stat チャネル（ADR-0006・#251 第2弾）───────────────────────────
    # 日銀コア CPI は BOJ が金融政策判断基準に使う指標で M-1 の金利文脈と整合。
    # 短観 DI は既に拡散指数（水準値）なため yoy は解釈が歪む → zscore。
    # M2 は名目水準なので yoy。公表ラグは収集側（BOJ_SERIES/ESTAT_SERIES.lag_days）で補正済み。
    "macro_jp_cpi_core_yoy":          ("JP_CPI_CORE",         "yoy"),
    "macro_jp_tankan_mfg_large_zscore": ("JP_TANKAN_MFG_LARGE", "zscore"),
    "macro_jp_m2_yoy":                ("JP_M2",               "yoy"),
    # ── 日銀追加系列（#282・#280サブイシュー）─────────────────────────────────
    # CGPI は CPI と川上/川下が異なる独立した物価チャネル。指数値（常に正）なので yoy。
    # マネタリーベースは M2（マネーストック）とは異なる金融政策スタンスの直接指標。水準系なので yoy。
    # 短観の残り3バリアント（従来 JP_TANKAN_MFG_LARGE のみ公開）は収集コストゼロのため公開し、
    # pooled BIC による特徴選定に委ねる（多重共線性の判定はモデル側の責務）。
    "macro_jp_cgpi_yoy":                  ("JP_CGPI",              "yoy"),
    "macro_jp_monetary_base_yoy":         ("JP_MONETARY_BASE",     "yoy"),
    "macro_jp_tankan_nonmfg_large_zscore": ("JP_TANKAN_NONMFG_LARGE", "zscore"),
    "macro_jp_tankan_mfg_small_zscore":    ("JP_TANKAN_MFG_SMALL",    "zscore"),
    "macro_jp_tankan_nonmfg_small_zscore": ("JP_TANKAN_NONMFG_SMALL", "zscore"),
    # ── OECD 先行指標（ADR-0009・Issue #283）─────────────────────────────────
    # CLI（振幅調整済み・100を中心とした指数）は水準（100からの乖離）自体がトレンド
    # 転換点シグナルであり yoy（前年比）を取ると意味が薄れるため zscore を採用。
    "macro_jp_cli_zscore": ("JP_CLI", "zscore"),
    # ── IMF WEO 見通し（forward-looking・#284）─────────────────────────────
    # GDP成長率・インフレ率とも符号が反転しうる（マイナス成長・デフレ見通し）ため
    # 既存の実績GDP（yoy）とは区別し、率指標として zscore を採用（貿易収支と同じ規約）。
    "macro_jp_weo_gdp_fcast_zscore": ("JP_WEO_GDP_FCAST", "zscore"),
    "macro_jp_weo_cpi_fcast_zscore": ("JP_WEO_CPI_FCAST", "zscore"),
}
MACRO_FEATURE_NAMES = list(_MACRO_MAP.keys())

MACRO_FEATURE_OPTIONS = [
    {"value": "macro_usdjpy_yoy",    "label": "USD/JPY 前年比（YoY）"},
    {"value": "macro_eurjpy_yoy",    "label": "EUR/JPY 前年比（YoY）"},
    {"value": "macro_dxy_yoy",       "label": "ドル指数（DXY）前年比（YoY）"},
    {"value": "macro_sp500_yoy",     "label": "S&P500 前年比（YoY）"},
    {"value": "macro_us5y_zscore",   "label": "米5年金利 Zスコア"},
    {"value": "macro_us10y_zscore",  "label": "米10年金利 Zスコア"},
    {"value": "macro_us30y_zscore",  "label": "米30年金利 Zスコア"},
    {"value": "macro_nikkei225_yoy", "label": "日経225 前年比（YoY）"},
    {"value": "macro_topix_yoy",     "label": "TOPIX 前年比（YoY）"},
    {"value": "macro_vix_zscore",    "label": "VIX恐怖指数 Zスコア"},
    {"value": "macro_wti_yoy",       "label": "WTI原油 前年比（YoY）"},
    {"value": "macro_gold_yoy",      "label": "金（ゴールド）前年比（YoY）"},
    {"value": "macro_hy_oas_zscore",       "label": "米HYスプレッド（OAS）Zスコア"},
    {"value": "macro_ig_oas_zscore",       "label": "米IGスプレッド（OAS）Zスコア"},
    {"value": "macro_breakeven10y_zscore", "label": "米10年BEI（インフレ期待）Zスコア"},
    {"value": "macro_jp10y_fred_zscore",   "label": "日10年金利（FRED）Zスコア"},
    {"value": "macro_t10y2y_zscore",       "label": "米10y−2yスプレッド Zスコア"},
    {"value": "macro_jp_real_gdp_yoy",     "label": "日本 実質GDP 前年比（YoY）"},
    {"value": "macro_jp_unemp_zscore",     "label": "日本 失業率 Zスコア"},
    {"value": "macro_jp_trade_bal_zscore", "label": "日本 貿易収支 Zスコア"},
    {"value": "macro_jp_iip_yoy",           "label": "日本 鉱工業生産指数 前年比（YoY）"},
    {"value": "macro_jp_iip_inventory_yoy", "label": "日本 鉱工業在庫指数 前年比（YoY）"},
    # macro_jp_cpi_core_yoy: #262 で解決済み。cdTab（表章項目=指数）と lvTime（時間軸レベル=月次）
    # の両方を fetch_estat_series に追加し、実APIで月次125行（2016-01〜2026-05）の取得を確認済み。
    {"value": "macro_jp_cpi_core_yoy",             "label": "日本 CPI コア（生鮮除く）前年比（YoY）"},
    {"value": "macro_jp_tankan_mfg_large_zscore", "label": "日銀短観 製造業大企業 業況DI Zスコア"},
    {"value": "macro_jp_m2_yoy",                 "label": "日本 M2（マネーストック）前年比（YoY）"},
    {"value": "macro_jp_cgpi_yoy",                  "label": "日本 企業物価指数（CGPI）前年比（YoY）"},
    {"value": "macro_jp_monetary_base_yoy",         "label": "日本 マネタリーベース 前年比（YoY）"},
    {"value": "macro_jp_tankan_nonmfg_large_zscore", "label": "日銀短観 非製造業大企業 業況DI Zスコア"},
    {"value": "macro_jp_tankan_mfg_small_zscore",    "label": "日銀短観 製造業中小企業 業況DI Zスコア"},
    {"value": "macro_jp_tankan_nonmfg_small_zscore", "label": "日銀短観 非製造業中小企業 業況DI Zスコア"},
    {"value": "macro_jp_cli_zscore", "label": "OECD景気先行指数（CLI）Zスコア"},
    {"value": "macro_jp_weo_gdp_fcast_zscore", "label": "IMF WEO 実質GDP成長率見通し（翌年）Zスコア"},
    {"value": "macro_jp_weo_cpi_fcast_zscore", "label": "IMF WEO インフレ率見通し（翌年）Zスコア"},
]
DEFAULT_MACRO_FEATURES = ["macro_usdjpy_yoy", "macro_sp500_yoy", "macro_us10y_zscore"]


# ── 日付 / 財務 helpers ────────────────────────────────────────────────────

def _add_days(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _find_applicable_fin(fin_recs: list, snap_date: str):
    """snap_date より FINANCIAL_LAG_DAYS 前以前に period_end がある最新の財務レコードを返す。"""
    result = None
    for fr in fin_recs:
        if not fr.period_end:
            continue
        pe = fr.period_end
        pe_str = pe.isoformat() if hasattr(pe, "isoformat") else str(pe)[:10]
        if _add_days(pe_str, FINANCIAL_LAG_DAYS) <= snap_date:
            result = fr
    return result


# ── マクロ helpers ─────────────────────────────────────────────────────────

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
            past = sorted((d, v) for d, v in date_close.items() if d <= ref_date)
            if not past:
                result[fname] = None
                continue
            current_vals = [past[-1][1]]
        current_avg = statistics.mean(current_vals)

        if ttype == "yoy":
            from datetime import date as _d2, timedelta as _td2
            ref_1y = ref - _td2(days=365)
            p_s = (ref_1y - _td2(days=window_days)).isoformat()
            p_e = (ref_1y + _td2(days=window_days)).isoformat()
            prev_vals = [v for d, v in date_close.items() if p_s <= d <= p_e]
            if not prev_vals:
                result[fname] = None
                continue
            prev_avg = statistics.mean(prev_vals)
            result[fname] = (current_avg - prev_avg) / prev_avg if prev_avg else None

        elif ttype == "zscore":
            from datetime import date as _d3, timedelta as _td3
            hist_start = (ref - _td3(days=zscore_years * 366)).isoformat()
            all_vals = [v for d, v in date_close.items() if hist_start <= d <= ref_date]
            if len(all_vals) < 20:
                result[fname] = None
                continue
            mu = statistics.mean(all_vals)
            sigma = statistics.stdev(all_vals) if len(all_vals) > 1 else 0.0
            result[fname] = (current_avg - mu) / sigma if sigma else None

    return result


# ── 実現ボラ ───────────────────────────────────────────────────────────────

def _realized_vol(price_rows: list, ref_date: str, weeks: int = 52) -> float | None:
    """ref_date 直前 weeks 週の実現ボラティリティ（年率）を返す。リークなし。"""
    eligible = [(r.trade_date, r.close_last)
                for r in price_rows
                if r.trade_date <= ref_date and r.close_last and r.close_last > 0]
    if len(eligible) < 4:
        return None
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


# ── ハイパーパラメータ探索中のスナップショットキャッシュ（Issue #298）──────────────
# plugins/tuning.py の search() は各候補を execute_plugin() でフル実行するが、
# load_data/preload_macro/build_snapshots はいずれも探索軸（モデルのハイパーパラメータ）
# に依存しない重い処理（DB全件ロード・特徴量スナップショット構築）で、構造パラメータ
# （fin_features/macro_features/use_momentum/min_coverage 等）が同じ候補間では結果が
# 完全に一致する（実測: 1候補あたり load_data 約23〜33秒 + build_snapshots 約25秒）。
# database.tuning_dry_run() と対になる contextvars.ContextVar パターンで、探索中だけ
# 有効なプロセス内メモリキャッシュ（DB永続化なし）を提供する。
#
# 通常の API 実行（/api/plugins/{name}/run）はこのコンテキストが未設定（None）のため
# 常に従来通りフル計算する（キャッシュは探索専用・副作用が漏れ出さない）。

_CACHE_MAXSIZE = 8

_tuning_cache: contextvars.ContextVar = contextvars.ContextVar("_tuning_cache", default=None)


class _BoundedCache:
    """探索専用の簡易 LRU（辞書/リスト引数を含むキーは hashable 化済み前提）。

    maxsize 超過時は最も長く使われていないエントリを追い出す。標準ライブラリの
    functools.lru_cache は引数が辞書/リスト（hashable でない）だと使えないため、
    呼び出し側でキーを正規化（id()・tuple 化）した上でこのクラスに渡す設計。
    """

    def __init__(self, maxsize: int = _CACHE_MAXSIZE):
        self._maxsize = maxsize
        self._data: OrderedDict = OrderedDict()

    def get_or_compute(self, key: Any, compute) -> Any:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        value = compute()
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        return value


# キャッシュ名前空間の一覧。load_data/preload_macro/build_snapshots は本モジュール内の
# 専用ラッパー（下記）が直接 _tuning_cache から引く。load_prices/load_macro_levels
# （M-3・plugins/macro_dlm.py）と cv_by_selected_features（M-1・plugins/macro_risk_return.py）
# は `tuning_cache_get_or_compute` 経由で他モジュールから使う汎用名前空間（Issue #304）。
_CACHE_NAMESPACES = (
    "load_data", "preload_macro", "build_snapshots",
    "load_prices", "load_macro_levels",
    "cv_by_selected_features",
)


@contextmanager
def tuning_snapshot_cache():
    """このブロック内では探索軸に依存しない重い処理の結果をキャッシュする。

    ハイパーパラメータ探索（plugins/tuning.py の search()）専用。load_data/preload_macro/
    build_snapshots（Issue #298・M-1/M-2 共有）に加え、M-3 の load_prices/load_macro_levels
    （plugins/macro_dlm.py）・M-1 の BIC選択結果に紐づく CV 結果（cv_by_selected_features・
    plugins/macro_risk_return.py）も同じコンテキストで管理する（Issue #304・モジュールを
    またぐ利用は `tuning_cache_get_or_compute` 経由）。ブロックを抜けるとキャッシュは破棄され、
    以後の呼び出し（次回の探索・通常の API 実行）には一切影響しない。
    """
    token = _tuning_cache.set({ns: _BoundedCache() for ns in _CACHE_NAMESPACES})
    try:
        yield
    finally:
        _tuning_cache.reset(token)


def tuning_cache_get_or_compute(namespace: str, key: Any, compute) -> Any:
    """`tuning_snapshot_cache()` の名前空間付きキャッシュへ汎用アクセスする（Issue #304）。

    macro_snapshots.py 外のモジュール（plugins/macro_dlm.py の load_prices/load_macro_levels・
    plugins/macro_risk_return.py の BIC選択結果に紐づく CV 結果）が、load_data 等と同じ
    contextvars パターンを再利用するための公開ヘルパー。`tuning_snapshot_cache()` コンテキスト
    外（通常の API 実行）では常に `compute()` を実行しキャッシュしない。namespace は
    `_CACHE_NAMESPACES` に定義済みのもののみ有効（未定義は KeyError）。
    """
    cache = _tuning_cache.get()
    if cache is None:
        return compute()
    return cache[namespace].get_or_compute(key, compute)


# ── データロード ───────────────────────────────────────────────────────────

# 週次株価ロードのチャンクサイズ（edinet_code を N 社ずつ IN 句で分割・Issue #311）。
# stock_price_weekly は本番で ~95万行あり、単一クエリ（全件 .all()）で取ると Supabase
# pooler 経由で statement_timeout(2min) 超過・大結果セットで接続破損（lost synchronization）を
# 起こす。500社/チャンクなら各クエリが PK インデックス（edinet_code, week_start）で数秒完了し、
# 全件でも実測 ~30秒で安定完走する（単発は失敗）。
_WEEKLY_LOAD_BATCH = 500
_WEEKLY_PX = namedtuple("_WeeklyPX", "trade_date close_last volume_sum")


def load_weekly_prices_chunked(db, batch: int = _WEEKLY_LOAD_BATCH) -> dict:
    """stock_price_weekly を企業単位のチャンクに分割ロードし {edinet_code: [_WeeklyPX,...]}
    を返す（各社リストは trade_date 昇順）。Issue #311。

    edinet_code の一覧は companies から取る（週次株価の edinet_code は companies の部分集合＝
    孤立コード無しを実測確認済み。行数が全件 count と一致）。各チャンクは
    `WHERE edinet_code IN (...) ORDER BY edinet_code, trade_date` で PK インデックスを使う。
    M-1/M-2（load_data）と M-3（macro_dlm.load_prices）が共用する唯一の週次ローダー。
    volume_sum は M-3 の価格行動系特徴量（出来高z-score・Issue #317）専用で、M-1/M-2 は未参照。
    """
    from database import Company, StockPriceWeekly
    codes = [c[0] for c in db.query(Company.edinet_code).all()]
    # plain dict + setdefault（defaultdict にしない）: 消費側は .get()/.items()/.values() か
    # 存在キー前提の [] アクセスのみで、欠損キーの暗黙生成に依存しない（従来の M-3 は plain dict）。
    prices_by_co: dict[str, list] = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        rows = (
            db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                     StockPriceWeekly.close_last, StockPriceWeekly.volume_sum)
            .filter(StockPriceWeekly.edinet_code.in_(chunk))
            .order_by(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date)
            .all()
        )
        for ec, td, cl, vs in rows:
            prices_by_co.setdefault(ec, []).append(_WEEKLY_PX(td, cl, vs))
    return prices_by_co


def load_data(db) -> tuple:
    """Company / FinancialMetric / StockPriceWeekly を一括ロード。

    tuning_snapshot_cache() コンテキスト内では db セッション単位（id(db)）で結果を
    キャッシュし、2回目以降の呼び出しは DB へ再クエリしない（Issue #298）。探索中は
    同一 db セッションに対して結果は不変という前提。コンテキスト外では常にフル計算する。
    """
    cache = _tuning_cache.get()
    if cache is None:
        return _load_data_impl(db)
    return cache["load_data"].get_or_compute(id(db), lambda: _load_data_impl(db))


def _load_data_impl(db) -> tuple:
    """load_data の実体（キャッシュなし・毎回フル計算）。"""
    from database import Company, FinancialMetric
    # 週次株価は単一クエリだと本番 pooler で timeout/接続破損するため分割ロード（Issue #311）。
    prices_by_co = load_weekly_prices_chunked(db)

    fin_by_co: dict[str, list] = defaultdict(list)
    for r in (db.query(FinancialMetric)
              .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end)
              .all()):
        fin_by_co[r.edinet_code].append(r)

    companies = {c.edinet_code: c for c in db.query(Company).all()}
    return prices_by_co, fin_by_co, companies


def preload_macro(db, prices_by_co: dict, macro_names: list[str] | None = None) -> dict:
    """MacroData を一括プリロードしキャッシュ dict を返す。

    tuning_snapshot_cache() コンテキスト内では (id(prices_by_co), macro_names) の
    組み合わせで結果をキャッシュする（Issue #298）。コンテキスト外では常にフル計算する。
    """
    cache = _tuning_cache.get()
    if cache is None:
        return _preload_macro_impl(db, prices_by_co, macro_names)
    key = (id(prices_by_co), tuple(sorted(macro_names)) if macro_names else ())
    return cache["preload_macro"].get_or_compute(
        key, lambda: _preload_macro_impl(db, prices_by_co, macro_names)
    )


def _preload_macro_impl(db, prices_by_co: dict, macro_names: list[str] | None = None) -> dict:
    """preload_macro の実体（キャッシュなし・毎回フル計算）。"""
    from database import MacroData
    from datetime import date as _date, timedelta as _td
    all_dates = [row.trade_date for rows in prices_by_co.values() for row in rows]
    if not all_dates:
        return {}
    min_d = min(all_dates)
    since = (_date.fromisoformat(min_d) - _td(days=5 * 366)).isoformat()
    max_d = max(all_dates)
    series_codes = sorted({_MACRO_MAP[n][0] for n in (macro_names or MACRO_FEATURE_NAMES) if n in _MACRO_MAP})
    q = (
        db.query(MacroData)
        .filter(
            MacroData.trade_date >= since,
            MacroData.trade_date <= max_d,
            MacroData.close.isnot(None),
        )
    )
    if series_codes:
        q = q.filter(MacroData.series_code.in_(series_codes))
    rows = q.order_by(MacroData.series_code, MacroData.trade_date).all()
    by_series: dict[str, dict[str, float]] = {}
    for r in rows:
        by_series.setdefault(r.series_code, {})[r.trade_date] = r.close
    return by_series


# ── モメンタム helper ──────────────────────────────────────────────────────

def _momentum(closes: list, dates: list, snap_idx: int, long_months: int) -> float | None:
    """12-1 モメンタム（log リターン）。データ不足は None。"""
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


# ── スナップショット構築（M-1/M-2 共通。build_interactions=False で M-2 用）──

def build_snapshots(
    prices_by_co: dict,
    fin_by_co: dict,
    companies: dict,
    macro_cache: dict,
    fin_features: list[str],
    macro_names: list[str],
    use_momentum: bool,
    mom_window: int,
    min_coverage: float,
    build_interactions: bool = True,
    macro_nan_ok: bool = False,
    return_stock_ids: bool = False,
) -> tuple:
    """M-1/M-2 共有スナップショット構築。

    build_interactions=True（M-1 既定）: fin×macro 交差項を生成。
    build_interactions=False（M-2）: 交差項なし＝同一母集団を保証しつつ特徴量を削減。

    macro_nan_ok=False（既定・M-1）: マクロ特徴量が1つでも欠損したらスナップショットを破棄。
    macro_nan_ok=True（M-2 専用）: マクロ欠損を float('nan') として保持し企業を残す。
      XGBoost は NaN をネイティブ処理するため、表示母集団を財務＋株価で決められる
      （薄いマクロ系列を足しても企業が激減しない）。表示可否は min_coverage が制御する。
      **build_interactions=False と併用すること**（交差項に NaN が混入すると OLS が壊れる）。

    return_stock_ids=True（ADR-0002 M-1 per-stock 階層ベイズ専用）: 5番目の戻り値として
      `stock_ids_by_ym: dict[str, list[str]]`（各サンプルの edinet_code・samples_by_ym と同じ並び順）
      を追加する。既定 False では戻り値の形（4-tuple）は従来どおり変わらない。

    tuning_snapshot_cache() コンテキスト内では、大きいオブジェクト引数（prices_by_co/
    fin_by_co/companies/macro_cache）は id()、それ以外の引数は値そのものからキーを
    構築してキャッシュする（Issue #298）。コンテキスト外では常にフル計算する。
    """
    cache = _tuning_cache.get()
    if cache is None:
        return _build_snapshots_impl(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions, macro_nan_ok, return_stock_ids,
        )
    key = (
        id(prices_by_co), id(fin_by_co), id(companies), id(macro_cache),
        tuple(fin_features), tuple(macro_names), use_momentum, mom_window,
        min_coverage, build_interactions, macro_nan_ok, return_stock_ids,
    )
    return cache["build_snapshots"].get_or_compute(
        key,
        lambda: _build_snapshots_impl(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions, macro_nan_ok, return_stock_ids,
        ),
    )


def _build_snapshots_impl(
    prices_by_co: dict,
    fin_by_co: dict,
    companies: dict,
    macro_cache: dict,
    fin_features: list[str],
    macro_names: list[str],
    use_momentum: bool,
    mom_window: int,
    min_coverage: float,
    build_interactions: bool,
    macro_nan_ok: bool,
    return_stock_ids: bool,
) -> tuple:
    """build_snapshots の実体（キャッシュなし・毎回フル計算）。"""
    use_macro = bool(macro_names)
    momentum_name = ["momentum_12m1"] if use_momentum else []

    interaction_names: list[str] = []
    if build_interactions and use_macro:
        for fn in fin_features:
            for mn in macro_names:
                interaction_names.append(f"{fn}_x_{mn}")

    all_feat_names = fin_features + macro_names + momentum_name + interaction_names
    n_feat = len(all_feat_names)

    samples_by_ym: dict[str, list] = defaultdict(list)
    sample_meta_by_ym: dict[str, list] = defaultdict(list)
    stock_ids_by_ym: dict[str, list] = defaultdict(list)
    current_snaps: dict[str, tuple] = {}
    min_rows = HORIZON_WEEKS + 4
    macro_memo: dict[str, dict] = {}

    for edinet_code, price_rows in prices_by_co.items():
        n = len(price_rows)
        if n < min_rows:
            continue
        fin_recs = fin_by_co.get(edinet_code, [])
        if not fin_recs:
            continue

        dates  = [r.trade_date for r in price_rows]
        closes = [r.close_last  for r in price_rows]

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

            macro_row: list[float] = []
            macro_dict: dict[str, float] = {}
            if use_macro:
                m_feats = macro_memo.get(snap_date)
                if m_feats is None:
                    m_feats = _macro_from_cache(macro_cache, snap_date, macro_names)
                    macro_memo[snap_date] = m_feats
                if any(v is None for v in m_feats.values()):
                    if not macro_nan_ok:
                        continue
                    # M-2: 欠損は NaN として保持（XGBoost が処理）。企業は落とさない。
                for mn in macro_names:
                    val = m_feats[mn]
                    fval = float("nan") if val is None else float(val)
                    macro_row.append(fval)
                    macro_dict[mn] = fval

            mom_row: list[float] = []
            if use_momentum:
                mom = _momentum(closes, dates, snap_idx, mom_window)
                if mom is None:
                    continue
                mom_row = [mom]

            industry = (
                fin_rec.industry
                or (companies.get(edinet_code) and companies[edinet_code].industry)
                or "不明"
            )

            size_val = getattr(fin_rec, "bs_total_assets", None)
            size_val = float(size_val) if (size_val is not None and size_val > 0) else None

            inter_row: list[float] = []
            if build_interactions and use_macro:
                for fn, fv in zip(fin_features, fin_row):
                    for mn in macro_names:
                        inter_row.append(fv * macro_dict[mn])

            feat_row = fin_row + macro_row + mom_row + inter_row

            non_null = sum(1 for v in feat_row if v == v)
            if non_null / n_feat < min_coverage:
                continue

            if has_future:
                c_snap, c_fut = closes[snap_idx], closes[snap_idx + HORIZON_WEEKS]
                if c_snap and c_fut and c_snap > 0 and c_fut > 0:
                    samples_by_ym[snap_ym].append((feat_row, math.log(c_fut / c_snap)))
                    sample_meta_by_ym[snap_ym].append((industry, size_val))
                    if return_stock_ids:
                        stock_ids_by_ym[snap_ym].append(edinet_code)

            if is_current:
                comp = companies.get(edinet_code)
                current_snaps[edinet_code] = (feat_row, {
                    "sec_code":     fin_rec.sec_code or (comp.sec_code if comp else ""),
                    "company_name": fin_rec.company_name or (comp.name if comp else edinet_code),
                    "industry":     industry,
                    "size":         size_val,
                    "price_rows":   price_rows,
                    "snap_date":    snap_date,
                })

    if return_stock_ids:
        return (dict(samples_by_ym), dict(sample_meta_by_ym), current_snaps,
                all_feat_names, dict(stock_ids_by_ym))
    return dict(samples_by_ym), dict(sample_meta_by_ym), current_snaps, all_feat_names


# ── 特徴量選択（BIC） ──────────────────────────────────────────────────────

def select_features_bic(X: np.ndarray, y: np.ndarray, max_features: int) -> list[int]:
    """winsorize+zscore 正規化した上で LassoLarsIC(BIC) により特徴量を選択し、列インデックスを返す。

    M-1（macro_risk_return._select_macro_features）と M-1' per-stock 階層ベイズ
    （macro_beta_inference.select_shared_factors）が共有する「pooled BIC 選択」の実体
    （ADR-0002 §1・Considered Options）。L1 正則化が共線性をネイティブに処理する。
    選択は LASSO で行い、最終係数は呼び出し側の OLS/階層モデル再フィットで不偏化する。
    BIC 最小解が max_features を超える場合は |係数| 降順の上位 max_features に切り詰める。

    コスト計測（Issue #304・本番DB読取専用・M-1 max_features 6候補グリッド）: フルパイプライン
    199.08秒（本Issueの他3案適用後）のうち本関数の累計実行時間は31.49秒（15.82%）。
    無視できない割合だが単体では支配的ボトルネックではなく（残り84.18%は Walk-Forward CV・
    最終 OLS・全社スコアリング）、本 Issue のスコープでは高速化を見送る（`LassoLarsIC` 自体の
    アルゴリズム変更は統計的妥当性に影響し得るため4案の対象外）。max_features 候補間で
    選択結果（selected_names）が偶然一致するケースが実測でも観測された（30/40 候補で
    score 完全一致）ため、BIC 自体の高速化ではなく後続 CV の重複排除（`macro_risk_return.py`
    の `cv_by_selected_features` キャッシュ）で対処した。
    """
    from sklearn.linear_model import LassoLarsIC

    n_samples, n_cand = X.shape
    if n_samples < 5:
        return []

    X_norm = np.empty_like(X, dtype=float)
    for ci in range(n_cand):
        col_w, _, _ = winsorize(X[:, ci].tolist())
        col_n, _, _ = normalize(col_w, "zscore")
        X_norm[:, ci] = col_n
    y_w, _, _ = winsorize(list(y))
    y_n, _, _ = normalize(y_w, "zscore")
    y_np = np.asarray(y_n, dtype=float)

    try:
        model = LassoLarsIC(criterion="bic")
        model.fit(X_norm, y_np)
    except Exception:
        return []

    coef = model.coef_
    nz = [i for i in range(n_cand) if abs(coef[i]) > 1e-12]
    if not nz:
        return []
    nz.sort(key=lambda i: abs(coef[i]), reverse=True)
    return sorted(nz[:max_features])


# ── producer スコア ────────────────────────────────────────────────────────

def producer_scores(meta: dict, loadings: dict, macro_snapshot: dict | None = None) -> dict:
    """macro_beta 推論結果から per-stock μ・R_macro・R1' を算出（ADR-0002 §5）。"""
    factors = list(meta.get("selected_factors") or [])
    cov = meta.get("factor_cov") or []
    out: dict = {}
    for code, fmap in loadings.items():
        beta = [float(fmap.get(f, (0.0, None))[0]) for f in factors]
        rec: dict = {"r_macro": macro_risk_exposure(beta, cov) if (cov and beta) else 0.0}
        if macro_snapshot is not None:
            m = [float(macro_snapshot.get(f) or 0.0) for f in factors]
            se = [float(fmap.get(f, (0.0, 0.0))[1] or 0.0) for f in factors]
            a_mean, a_se = fmap.get("_intercept", (0.0, 0.0))
            a_se = float(a_se or 0.0)
            rec["mu"] = float(a_mean) + sum(b * mm for b, mm in zip(beta, m))
            rec["r1_prime"] = math.sqrt(a_se ** 2 + sum((s * mm) ** 2 for s, mm in zip(se, m)))
        out[code] = rec
    return out


def get_producer_scores(db: Any, macro_snapshot: dict | None = None) -> dict:
    """DB から macro_beta を読み producer_scores を返す。未蓄積なら {}（graceful degrade）。"""
    try:
        from database import get_macro_beta
        meta, loadings = get_macro_beta(db)
        if not meta or not loadings:
            return {}
        return producer_scores(meta, loadings, macro_snapshot)
    except Exception:
        return {}


# ── アウトオブサンプル検証（OOF）: 無リーク walk-forward 予測のモデル評価（ADR-0004）──
# 既存「バックテスト」(/api/backtest・preset/as-of ポートフォリオ模擬) とは別概念。
# こちらは「μ̂ が将来リターンを順序付けるか」を OOF 予測のみで評価する（再学習・追加価格取得なし）。
# M-2（macro_gbdt）が使用。M-1 も同じ residuals を持つため後付け可能（共有ヘルパ・ADR-0004 §6）。

def _avg_ranks(vals: list) -> list:
    """同順位を平均順位に割り当てた順位列（1始まり）を返す。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0   # i..j の平均順位（1始まり）
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list, ys: list) -> float | None:
    """Spearman 順位相関（= 順位の Pearson）。n<3 または無分散なら None。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def oof_backtest(residuals_by_ym: dict, n_quantiles: int = 5, cost_bps: float = 0.0) -> dict:
    """無リーク OOF 予測から「アウトオブサンプル検証（OOF）」指標を算出する（ADR-0004）。

    residuals_by_ym = {test_ym: [(yhat, y_true), ...]}（walk_forward_cv_monthly の
    return_residuals=True 出力・テストサンプル順）。再学習・追加の価格取得は不要。

    手法（ADR-0004「分位の作り方」）:
      - 各 test_ym 内で yhat を横断ランク→ n_quantiles 分位→分位平均 y_true（期内）
        →期間平均（per-period cross-sectional・μ̂ 水準の時系列ドリフトに頑健）。
      - rank-IC = Spearman(yhat, y_true) を fold 毎→ mean/std/n。
      - long_short_spread = top分位平均 − bottom分位平均（期間平均）。
      - hit_rate = top分位 > bottom分位 だった期の割合。
      - 期内サンプルが n_quantiles*2 未満の期は分位計算から自動除外（IC には使用）。
    quantile_returns[0]=最低 μ̂ バケット, [-1]=最高 μ̂ バケットの実現リターン。

    cost_bps（Issue #316）: 片道売買コスト（bp、1bp=0.01%）。long_short_spread は毎期
    ロング・ショートを1回転する前提のため、往復（買い+売り）で2倍控除した
    long_short_spread_net を併記する（デフォルト0＝控除なし・既存キーは不変）。
    このコストは「期」1回あたりの控除であり、期の頻度（週次/月次）はホライズン
    ごとに異なる（ADR-0012）ため、複数モデルの spread を跨いで cost_bps ベースで
    直接比較する場合は呼び出し側で頻度差を考慮すること。
    """
    yms = sorted(residuals_by_ym.keys())
    n_oof = sum(len(residuals_by_ym[y]) for y in yms)

    # rank-IC（fold 毎・サンプル<3 の期は除外）
    ics: list[float] = []
    for ym in yms:
        pairs = residuals_by_ym[ym]
        ic = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append(ic)
    ic_mean = statistics.mean(ics) if ics else None
    ic_std = statistics.pstdev(ics) if len(ics) > 1 else (0.0 if ics else None)

    # 期内横断分位リターン（各期で yhat 昇順→ n_quantiles 等分→分位平均 y_true）
    q_sums = [0.0] * n_quantiles
    q_periods = 0
    ls_spreads: list[float] = []
    hits = 0
    for ym in yms:
        pairs = residuals_by_ym[ym]
        if len(pairs) < n_quantiles * 2:
            continue
        ordered = sorted(pairs, key=lambda p: p[0])   # yhat 昇順
        m = len(ordered)
        q_means = []
        for q in range(n_quantiles):
            lo = q * m // n_quantiles
            hi = (q + 1) * m // n_quantiles
            seg = ordered[lo:hi]
            q_means.append(sum(p[1] for p in seg) / len(seg))
        for q in range(n_quantiles):
            q_sums[q] += q_means[q]
        q_periods += 1
        ls = q_means[-1] - q_means[0]   # top（高 yhat）− bottom（低 yhat）
        ls_spreads.append(ls)
        if ls > 0:
            hits += 1

    quantile_returns = [round(s / q_periods, 6) for s in q_sums] if q_periods else []
    long_short_spread = round(statistics.mean(ls_spreads), 6) if ls_spreads else None
    hit_rate = round(hits / q_periods, 4) if q_periods else None

    round_trip_cost_pct = cost_bps / 100.0 * 2
    long_short_spread_net = (
        round(long_short_spread - round_trip_cost_pct, 6) if long_short_spread is not None else None
    )

    return {
        "n_quantiles":        n_quantiles,
        "n_periods":          len(yms),
        "n_periods_quantile": q_periods,
        "n_oof_samples":      n_oof,
        "quantile_returns":   quantile_returns,
        "rank_ic": {
            "mean": round(ic_mean, 4) if ic_mean is not None else None,
            "std":  round(ic_std, 4) if ic_std is not None else None,
            "n":    len(ics),
        },
        "long_short_spread":     long_short_spread,
        "hit_rate":              hit_rate,
        "cost_bps":              cost_bps,
        "long_short_spread_net": long_short_spread_net,
    }
