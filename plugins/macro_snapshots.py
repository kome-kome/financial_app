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
import sys
from collections import OrderedDict, defaultdict, namedtuple
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .utils import macro_risk_exposure, normalize, winsorize

# ── 定数 ──────────────────────────────────────────────────────────────────

FINANCIAL_LAG_DAYS = 45
HORIZON_WEEKS = 52
# walk-forward CV の purge ギャップ（月数）: 52週先ラベルの窓重複を学習集合から除外する
# （ADR-0014・Issue #363）。math.ceil(52*12/52) = 12。walk_forward_cv_monthly(embargo_months=)
# へ M-1/M-2 双方が渡す（HORIZON_WEEKS の月換算の単一情報源）。
LABEL_HORIZON_MONTHS = math.ceil(HORIZON_WEEKS * 12 / 52)  # = 12

# ── 低頻度マクロ系列の変換パラメータ（Issue #379）─────────────────────────
# `_macro_from_cache` の既定窓は日次/月次（median gap 1〜31日）前提で、四半期(92日)や
# 年2回(183日)の系列では変換が構造的に None になり、strict（macro_nan_ok=False）の M-1 が
# 全スナップショットを破棄して学習0件になっていた（#358 の全マクロ既定 ON 以降）。
# 高頻度系列の値は不変のまま、低頻度系列だけを救うためのフォールバック定数。
_YOY_PREV_FLOOR_DAYS  = 366   # yoy の前年同期 ffill 下限（ref-1y からこれ以上遡らない）
_ZSCORE_MIN_PTS       = 20    # zscore の必要点数（従来のハードコード値）
_ZSCORE_MIN_PTS_SPARSE = 8    # 全履歴でも足りない超低頻度系列の最低許容点数

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
    # ── 質・トレンド・業種内相対の追加因子（#373・追加収集ゼロ・成長/バリューと直交）──
    {"value": "accruals",        "label": "アクルーアル（(純利益−営業CF)/総資産・Sloan質因子）"},
    {"value": "delta_roe",       "label": "ROE前年差（%pt・改善/悪化トレンド）"},
    {"value": "delta_op_margin", "label": "営業利益率前年差（%pt・改善/悪化トレンド）"},
    {"value": "z_roe_sec",       "label": "ROE 業種内Zスコア"},
    {"value": "z_op_margin_sec", "label": "営業利益率 業種内Zスコア"},
]
DEFAULT_FIN_FEATURES = ["per", "pbr", "roe", "equity_ratio", "roa", "eps_growth"]

# `load_data` が financial_metrics VIEW から実際に引く列（Issue #459）。VIEW は 97 列あるが
# 消費側（`_build_snapshots_impl` が唯一）が読むのはここだけで、ORM の全列ロードは本番実測
# 22.5MB/回＝夜間バッチ 67.7MB の 33% を占めていた。`load_weekly_prices_chunked`（3列）・
# `_preload_macro_impl`（3列）と同じ「消費列だけ引く」方針を財務側へ広げる（#446 の続き）。
#
# 突合・メタ列: edinet_code（グルーピング）/ period_end（`_find_applicable_fin`）/
# industry・sec_code・company_name・bs_total_assets（スナップショットのメタ）。
_FIN_META_FIELDS = ("edinet_code", "period_end", "industry", "sec_code",
                    "company_name", "bs_total_assets")
# `recommend.METRICS` の非 RUNTIME 列のうち FIN_BASE_OPTIONS に無いもの。
# `recommend_factor_premia.build_period_panel` が fin_features として渡す（ADR-0008）。
# recommend 側との対応は `tests/test_macro_snapshots_loaders.py` のメタテストが CI で照合する
# （ここで `plugins.recommend` を import すると plugins の自動検出と循環しうるため）。
_FIN_RECOMMEND_FIELDS = ("z_revenue", "z_equity_ratio", "z_eps", "z_de_ratio", "gap_ratio")
FIN_LOAD_FIELDS = tuple(dict.fromkeys(
    tuple(o["value"] for o in FIN_BASE_OPTIONS) + _FIN_RECOMMEND_FIELDS + _FIN_META_FIELDS))
_FinRow = namedtuple("_FinRow", FIN_LOAD_FIELDS)

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
    # 非ICE代替の信用スプレッド（#381）。HY_OAS/IG_OAS は FRED の ICE BofA ライセンス制約で
    # 2023-06 以降しか取得できず strict の学習窓を律速する。BAA10Y（Moody's Baa−10Y）は
    # truncate されず 2016 以前まで遡れるため、既定の信用ファクターをこちらへ移す（下の
    # DEFAULT_MACRO_FEATURES 参照）。
    "macro_baa_spread_zscore":   ("BAA_SPREAD",   "zscore"),
    "macro_breakeven10y_zscore": ("BREAKEVEN10Y", "zscore"),
    "macro_jp10y_fred_zscore":   ("JP10Y_FRED",   "zscore"),
    "macro_t10y2y_zscore":       ("T10Y2Y",       "zscore"),
    # ── 政策不確実性チャネル（#404）──────────────────────────────────────────
    # Baker-Bloom-Davis EPU（新聞記事ベースの指数・常に正の水準系）。VIX が市場の織り込む
    # 変動を測るのに対し EPU は政策・制度側の不確実性を測る別チャネル。水準そのもの（平時比
    # で高いか低いか）がレジーム情報なので、既存の指数系（VIX/CLI/短観DI）と同じ zscore 規約。
    "macro_us_epu_zscore":        ("US_EPU",        "zscore"),
    "macro_us_equity_epu_zscore": ("US_EQUITY_EPU", "zscore"),
    # ── 日本 実体経済指標（#250・米国偏重の是正）─────────────────────────────────
    # 水準系（GDP・生産指数）は常に正なので yoy。失業率は「率」なので既存金利と同じ
    # zscore 規約。貿易収支は符号がプラス/マイナス両方を取り yoy（除算）が発散するため
    # zscore。低頻度＋公表ラグは収集側（FRED_SERIES.lag_days）で trade_date を補正済み。
    "macro_jp_real_gdp_yoy":     ("JP_REAL_GDP",  "yoy"),
    "macro_jp_unemp_zscore":     ("JP_UNEMP",     "zscore"),
    "macro_jp_trade_bal_zscore": ("JP_TRADE_BAL", "zscore"),
    # ── ESRI 四半期別GDP速報 需要項目（#373・追加収集ゼロの死蔵解消）─────
    # collector_prices.py ESRI_SERIES で既収集の実質季節調整4系列（十億円・水準系→yoy）。
    # 総需要 JP_REAL_GDP の内訳を民間消費/住宅/設備/公共の需要項目へ分解し業種別露出を捉える。
    # M-1/M-2 向け（四半期→月次ffill で週次変化は疎・ADR-0012 により M-3=DLM には不適・
    # 既存 JP_REAL_GDP と同じ扱い）。公表ラグは ESRI_SERIES.lag_days=60 で trade_date 補正済。
    "macro_jp_gdp_consumption_yoy": ("JP_GDP_PRIVATE_CONSUMPTION", "yoy"),
    "macro_jp_gdp_residential_yoy": ("JP_GDP_RESIDENTIAL_INV",     "yoy"),
    "macro_jp_gdp_capex_yoy":       ("JP_GDP_CAPEX",               "yoy"),
    "macro_jp_gdp_public_inv_yoy":  ("JP_GDP_PUBLIC_INV",          "yoy"),
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
    # ── コモディティ・チャネル拡張（ADR-0013・#358）──────────────────────────
    # 日本株の業種別コモディティ感応度（銅=非鉄/電線/機械・天然ガス=電力ガス/化学・
    # 貴金属=商社/触媒/電子材料・穀物=食品/飼料）を捕捉。既存 WTI/GOLD と同じく商品価格は
    # 常に正の水準系なので yoy（前年比）を採用（変換規約 L79 準拠。docs/MODELS.md §9.2 が
    # DXY/WTI/金を「Zスコア」と記すのは陳腐化した誤記で、コード側 yoy が正本）。
    "macro_bcom_yoy":     ("BCOM",     "yoy"),
    "macro_copper_yoy":   ("COPPER",   "yoy"),
    "macro_natgas_yoy":   ("NATGAS",   "yoy"),
    "macro_silver_yoy":   ("SILVER",   "yoy"),
    "macro_wheat_yoy":    ("WHEAT",    "yoy"),
    "macro_corn_yoy":     ("CORN",     "yoy"),
    "macro_soybean_yoy":  ("SOYBEAN",  "yoy"),
    "macro_platinum_yoy": ("PLATINUM", "yoy"),
    # ── ニューストーン／関心度チャネル（#406・GDELT / Wikimedia Pageviews）────────
    # EPU（#404）が新聞記事「量」で政策不確実性を測るのに対し、GDELT のトーンは記事の
    # **極性**（悲観/楽観）を、報道量・Wikipedia 閲覧数は**注目度**を測る別チャネル。
    # トーンは正負を跨ぐため yoy（除算）が発散する → zscore。報道量(%)・閲覧数は水準系だが、
    # 「平時比でどれだけ注目されているか」がレジーム情報なので EPU/VIX と同じ zscore 規約
    # （yoy にすると水準の高低が消える）。いずれも日次＝低頻度変換窓（#379/#382）・
    # strict 律速（#381）に触れない。
    "macro_jp_news_tone_zscore":       ("JP_NEWS_TONE",       "zscore"),
    "macro_jp_news_econ_tone_zscore":  ("JP_NEWS_ECON_TONE",  "zscore"),
    "macro_jp_news_econ_vol_zscore":   ("JP_NEWS_ECON_VOL",   "zscore"),
    "macro_jp_wiki_market_attn_zscore": ("JP_WIKI_MARKET_ATTN", "zscore"),
    "macro_jp_wiki_macro_attn_zscore":  ("JP_WIKI_MACRO_ATTN",  "zscore"),
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
    {"value": "macro_baa_spread_zscore",   "label": "米Baa社債スプレッド（Baa−10Y）Zスコア"},
    {"value": "macro_breakeven10y_zscore", "label": "米10年BEI（インフレ期待）Zスコア"},
    {"value": "macro_jp10y_fred_zscore",   "label": "日10年金利（FRED）Zスコア"},
    {"value": "macro_t10y2y_zscore",       "label": "米10y−2yスプレッド Zスコア"},
    # 政策不確実性チャネル（#404）
    {"value": "macro_us_epu_zscore",        "label": "米 経済政策不確実性指数（EPU）Zスコア"},
    {"value": "macro_us_equity_epu_zscore", "label": "米 株式市場関連 経済不確実性指数 Zスコア"},
    {"value": "macro_jp_real_gdp_yoy",     "label": "日本 実質GDP 前年比（YoY）"},
    {"value": "macro_jp_gdp_consumption_yoy", "label": "日本 GDP 民間最終消費支出 前年比（YoY）"},
    {"value": "macro_jp_gdp_residential_yoy", "label": "日本 GDP 民間住宅投資 前年比（YoY）"},
    {"value": "macro_jp_gdp_capex_yoy",       "label": "日本 GDP 民間企業設備投資 前年比（YoY）"},
    {"value": "macro_jp_gdp_public_inv_yoy",  "label": "日本 GDP 公的固定資本形成（公共投資）前年比（YoY）"},
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
    # コモディティ・チャネル拡張（ADR-0013・#358）
    {"value": "macro_bcom_yoy",     "label": "ブルームバーグ商品指数 前年比（YoY）"},
    {"value": "macro_copper_yoy",   "label": "銅先物 前年比（YoY）"},
    {"value": "macro_natgas_yoy",   "label": "天然ガス先物 前年比（YoY）"},
    {"value": "macro_silver_yoy",   "label": "銀先物 前年比（YoY）"},
    {"value": "macro_wheat_yoy",    "label": "小麦先物 前年比（YoY）"},
    {"value": "macro_corn_yoy",     "label": "トウモロコシ先物 前年比（YoY）"},
    {"value": "macro_soybean_yoy",  "label": "大豆先物 前年比（YoY）"},
    {"value": "macro_platinum_yoy", "label": "プラチナ先物 前年比（YoY）"},
    # ニューストーン／関心度チャネル（#406）
    {"value": "macro_jp_news_tone_zscore",       "label": "日本ニュース 平均トーン（GDELT）Zスコア"},
    {"value": "macro_jp_news_econ_tone_zscore",  "label": "日本 株式市場ニュース 平均トーン（GDELT）Zスコア"},
    {"value": "macro_jp_news_econ_vol_zscore",   "label": "日本 株式市場ニュース 報道量（GDELT）Zスコア"},
    {"value": "macro_jp_wiki_market_attn_zscore", "label": "日本 株式市場 関心度（Wikipedia 閲覧数）Zスコア"},
    {"value": "macro_jp_wiki_macro_attn_zscore",  "label": "日本 景気・金融政策 関心度（Wikipedia 閲覧数）Zスコア"},
]
# 既定は全選択肢（#358・ユーザー方針変更）。従来は米国寄り3本（USDJPY/SP500/US10Y）のみ
# だったが、コモディティを含む全マクロ系列を既定 ON にし M-2/M-3 と揃える。過剰選択は
# LassoLarsIC(BIC) の pooled 選択が抑えるため、既定を広げても最終モデルは自動的に絞られる。
#
# 例外（#381）: HY_OAS/IG_OAS は FRED の ICE BofA ライセンス制約で 2026-04 以降ローリング
# 3年窓に制限され 2023-06 以前を配信しない。strict（macro_nan_ok=False・同一母集団保証・
# ADR-0003）の M-1 は「選択中の全マクロ特徴が同時に非None」の行しか使わないため、学習窓が
# この2系列に律速され 24ヶ月＝honest OOF の fold が2期しか立たない。よって両系列を既定から
# 除外し（選択肢としては残す＝直近3年窓で使いたいユーザーは手動 ON 可能）、信用スプレッドの
# 経済的情報は非ICE代替 macro_baa_spread_zscore（Baa−10Y・日次・truncate されず 2016 以前まで
# 遡れる）が既定で担う。
#
# **現況（2026-08-01 実測・`python -m scripts.measure_strict_binding`）**: 除外後の既定マクロ
# 46本は全て 2019-07 から非 None で、strict / nan_ok / マクロ無しの3条件でスナップショット
# 母集団が完全一致する（**71ヶ月・2019-08〜2025-06・173,836サンプル**）。
# ＝**strict 制約は1行も落としておらず、学習窓を律速していない**。窓を決めているのは
# stock_price_weekly と financial_records のデータ履歴長で、マクロ既定の増減では伸びない。
# 実際 #411 で株価を 2019-08 まで・財務を 2018 まで延伸し窓は 47→71ヶ月（fold 10→18 期）に
# 広がった（ADR-0025）。現在の律速は週次株価（price-only cap = 実測月数）。かつてここに
# 書かれていた「次の律速はコモディティ8系列（2020-07 開始）」は、その後の再収集で 8系列とも
# 2016-07 まで遡ったため解消済み。
_STRICT_TRUNCATED_FEATURES = {"macro_hy_oas_zscore", "macro_ig_oas_zscore"}
# 追加直後で昇格ゲート（#372 基準＝有意差＋多重比較補正）を**まだ実測していない**特徴量の枠。ADR-0016
# の順序制約と同じく、本番 macro_data へ蓄積し rank-IC / short_side_spread を実測して有意と
# 判定されるまでは既定へ入れない（strict は「選択中の全マクロが同時に非None」の行しか使わない
# ため、未蓄積の系列を既定に混ぜると学習母集団が消える）。選択肢としては即日使える。
#
# #404 の EPU 2系列は `scripts/macro_feature_bakeoff.py --preset epu` の実測（3,979社・
# 43ヶ月・57,955サンプル・9 fold）で M-6 の売り側 spread +0.0652→+0.0684（diff +0.0032・
# p=0.001・Bonferroni α=0.0125 通過）を示し既定へ昇格した（ADR-0023）。
#
# 現在は空＝実測待ちの系列なし。
_PENDING_EVAL_FEATURES: set[str] = set()
# 昇格ゲートを実測した結果 **有意な改善が無かった**特徴量の枠（未実測の保留枠とは区別する）。
# 選択肢としては残し、既定からは外す。再判定するなら
# `python -m scripts.macro_feature_bakeoff --features <カンマ区切り>`。
#
# #406（ADR-0024）の GDELT / Wikimedia 5系列: `--preset attention` の実測（3,979社・43ヶ月・
# 57,955サンプル・9 fold・honest embargo=12）で4検定すべて非有意——M-2 rank-IC −0.0060
# (p=0.140) / M-2 売り側 spread −0.0021 (p=0.495) / M-6 rank-IC +0.0010 (p=0.214) / M-6 売り側
# spread −0.0002 (p=0.623)。strict 母集団は 43ヶ月・57,955サンプルで不変（律速はしない）。
#
# #451（ADR-0028）の鉱工業指数2系列: `--features macro_jp_iip_yoy,macro_jp_iip_inventory_yoy`
# の実測（3,981社・67ヶ月・91,482サンプル・17 fold）で4検定すべて非有意——M-2 rank-IC
# −0.0027 (p=0.527) / M-2 売り側 spread −0.0008 (p=0.731) / M-6 rank-IC +0.0001 (p=0.942) /
# M-6 売り側 spread −0.0017 (p=0.472)。strict 母集団は 67ヶ月・91,482サンプルで不変
# （外しても 1 サンプルも減らない）。
# 併せて e-Stat 側が 2026年3月分（trade_date=2026-04-30）で配信を止めており、代替ソース
# （経産省直接CSV・ESRI 級の工数）を実装する根拠も無いと判断した。収集自体は継続する
# （e-Stat が年単位で更新されるため・`macro_health.EXCLUDED_SERIES` で鮮度判定からは退避）。
#
# **#454 の再判定（2026-08-05・lag_days 是正後データ）**: 上の2群はどちらも #447 以前の
# データで判定しており、`trade_date` が動いた以上は前提が変わっている（asof が拾う値が別物に
# なるため）。同一パネル（3,981社・67ヶ月・91,482サンプル・17 fold）で測り直し、**両群とも
# 棄却が維持**された:
#   attention 5系列 … M-2 rank-IC +0.0054 (p=0.626) / M-2 売り側 +0.0054 (p=0.155) /
#                     M-6 rank-IC −0.0013 (p=0.146) / M-6 売り側 +0.0012 (p=0.112)
#   鉱工業指数2系列 … M-2 rank-IC +0.0033 (p=0.229) / M-2 売り側 +0.0055 (p=0.027) /
#                     M-6 rank-IC −0.0025 (p=0.233) / M-6 売り側 −0.0012 (p=0.653)
# いずれも Bonferroni α=0.0125 を通らず strict 母集団も不変。ただし**鉱工業指数の符号構成は
# 是正前後で入れ替わった**（是正前 3負1正 → 是正後 2正2負・最接近は M-2 売り側 p=0.027）。
# 棄却の根拠は「符号が負」ではなく「補正後 α を通らないこと」＋ e-Stat の配信停止に置く
# （符号の向きだけを棄却理由に書くと、データ世代が変わるたびに理由が揺れる）。
_GATE_REJECTED_FEATURES: set[str] = {
    "macro_jp_news_tone_zscore",
    "macro_jp_news_econ_tone_zscore",
    "macro_jp_news_econ_vol_zscore",
    "macro_jp_wiki_market_attn_zscore",
    "macro_jp_wiki_macro_attn_zscore",
    "macro_jp_iip_yoy",
    "macro_jp_iip_inventory_yoy",
}
# **既定入りの再判定（#454・2026-08-05）**: #447 で `lag_days` を引き直した月次6本
# （`macro_jp_cpi_core_yoy` / `macro_jp_m2_yoy` / `macro_jp_cgpi_yoy` /
# `macro_jp_monetary_base_yoy` / `macro_jp_unemp_zscore` / `macro_jp10y_fred_zscore`）を
# leave-out で測り直した（base=既定−6本 vs 既定）。**既定は変更していない**——4検定とも
# Bonferroni α=0.0125 を通らず、除外を正当化する有意な悪化も無かったため:
#   M-2 rank-IC +0.0041 (p=0.562) / M-2 売り側 −0.0068 (p=0.270) /
#   M-6 rank-IC +0.0009 (p=0.714) / M-6 売り側 +0.0023 (p=0.070)
# strict 母集団は 67ヶ月・91,482サンプルで不変（外してもサンプルは1行も増えない）。
#
# **判定規則（既定入りに対する非対称性）**: 昇格ゲートは「候補を足すか否か」の検定であり、
# 帰無仮説は「base のまま」に置かれる。これを既定入りの特徴量へそのまま当てると帰無が反転し
# 「残す根拠」を要求する形になるため、スクリプトの `keep as option only` という verdict 文字列を
# 既定からの除外と読み替えてはならない。**既定を減らす向きの変更も、増やす向きと同じく
# 補正後 α を通る実測を要する**（#358 の「全選択肢を既定 ON・過剰選択は BIC/正則化が抑える」
# 方針を、検出力の低い非有意結果で崩さないため）。
#
# **M-2 の点差に関する注意（Issue #457）**: leave-out 用法では base と with_cand で特徴量の
# **列順**が変わる（抜いた6本が末尾へ回る）。XGBoost は `random_state=42` 固定でも列順に依存
# するため、同一集合・順序違いだけで rank-IC が 0.0014 動く実測がある。M-6（ElasticNet）は
# 順序不変（同一集合の再実行で差 0.0001）なので、leave-out の解釈は M-6 側を主に読む。
# 候補追加という本来の用法では共通部分の順序が保たれるため、この交絡は起きない。
DEFAULT_MACRO_FEATURES = [o["value"] for o in MACRO_FEATURE_OPTIONS
                          if o["value"] not in _STRICT_TRUNCATED_FEATURES
                          and o["value"] not in _PENDING_EVAL_FEATURES
                          and o["value"] not in _GATE_REJECTED_FEATURES]


# ── 価格行動系特徴量定義（Issue #317・#364 で M-2/M-3 共有化）────────────────────
# stock_price_weekly（close_last/volume_sum・全履歴保持・分割調整済み）のみで構築する
# 銘柄固有の特徴量。マクロ列（week t の「同時点」変化＝ファクター・エクスポージャー）とは
# 意味が異なる、銘柄固有の遅行特徴量（academic factor model のモメンタム/リバーサル/ボラ
# 特徴量と同じ扱い）。M-3（DLM・macro_dlm）と M-2（GBDT・macro_gbdt）が本定義を共有する。
_PX_RVOL_WINDOW   = 12   # 週次実現ボラ（対数リターン標準偏差）の窓（週）
_PX_VOLZ_WINDOW   = 12   # 出来高z-score の窓（週）
_PX_HIGH52_WINDOW = 52   # 52週高値判定の窓（週）
_PX_REV_WINDOW    = 4    # N週リバーサルの窓（週）

PRICE_FEATURE_OPTIONS = [
    {"value": "px_rvol",      "label": f"週次実現ボラティリティ（直近{_PX_RVOL_WINDOW}週の対数リターン標準偏差）"},
    {"value": "px_volz",      "label": f"出来高z-score（直近{_PX_VOLZ_WINDOW}週の自己相対）"},
    {"value": "px_high52dev", "label": f"{_PX_HIGH52_WINDOW}週高値からの乖離（対数）"},
    {"value": "px_rev4w",     "label": f"{_PX_REV_WINDOW}週リバーサル（過去{_PX_REV_WINDOW}週リターン）"},
]
# 既定は全選択（Issue #317・実データ検証済み）: 本番DB（3,600社規模）でのOOF比較で
# rank_ic mean +35%（0.0097→0.0131）・rank_ic std 半減（0.0572→0.0304・IC情報比率で約2.5倍）・
# long_short_spread 負→正転換（-0.0012→+0.0008）・hit_rate 0.45→0.59（コイン投げ以下→明確に上回る）
# の一貫した改善を確認した上でユーザー承認を得て全選択化。トレードオフ: px_high52dev の52週
# warmup により対象企業数は微減（実測 3,641→3,546社）。
DEFAULT_PRICE_FEATURES: list[str] = [o["value"] for o in PRICE_FEATURE_OPTIONS]


def build_price_features(px_rows: list, selected: list[str]) -> dict[str, list]:
    """1銘柄分の価格行動系特徴量を週インデックス整列で事前計算する（Issue #317・#364）。

    px_rows: StockPriceWeekly 由来の (trade_date, close_last, volume_sum) 行（trade_date昇順）。
    戻り値: {feature_name: [値|nan, ...]}（長さ=len(px_rows)）。各インデックス i の値は
    「i 番目の週までの情報で計算可能な既知値」（rolling の窓は i を含む過去 w 週）。M-3 は
    week (t-1) のインデックスを参照する遅行特徴量として、M-2 は snap_idx（スナップショット週）の
    既知値として利用する（いずれも未来を覗かない）。窓に満たない先頭や NaN の混入は nan
    （pandas rolling の min_periods=window により窓内が完全に揃わないと nan）。
    """
    if not selected:
        return {}
    closes = pd.Series(
        [r.close_last if r.close_last and r.close_last > 0 else np.nan for r in px_rows],
        dtype=float,
    )
    out: dict[str, list] = {}

    if "px_rvol" in selected or "px_rev4w" in selected:
        logret = np.log(closes / closes.shift(1))

    if "px_rvol" in selected:
        w = _PX_RVOL_WINDOW
        out["px_rvol"] = logret.rolling(window=w, min_periods=w).std(ddof=1).tolist()

    if "px_volz" in selected:
        w = _PX_VOLZ_WINDOW
        # 出来高列を落としてロードした行で px_volz を求めようとしたら即落とす（Issue #446）。
        # 番兵を欠測として扱うと全 nan の px_volz が黙って出来上がる＝壊れたのか薄いのかを
        # 後から区別できない。
        if px_rows and getattr(px_rows[0], "volume_sum", None) is _VOLUME_NOT_LOADED:
            raise ValueError(
                "px_volz には volume_sum が要る。load_data/load_weekly_prices_chunked を "
                "with_volume=True でロードすること（Issue #446）"
            )
        vols = pd.Series(
            [r.volume_sum if getattr(r, "volume_sum", None) and r.volume_sum > 0 else np.nan
             for r in px_rows],
            dtype=float,
        )
        roll_mean = vols.rolling(window=w, min_periods=w).mean()
        roll_std  = vols.rolling(window=w, min_periods=w).std(ddof=1).replace(0, np.nan)
        out["px_volz"] = ((vols - roll_mean) / roll_std).tolist()

    if "px_high52dev" in selected:
        w = _PX_HIGH52_WINDOW
        roll_max = closes.rolling(window=w, min_periods=w).max()
        out["px_high52dev"] = np.log(closes / roll_max).tolist()

    if "px_rev4w" in selected:
        w = _PX_REV_WINDOW
        out["px_rev4w"] = np.log(closes / closes.shift(w)).tolist()

    return out


# ── 日付 / 財務 helpers ────────────────────────────────────────────────────

def _add_days(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def to_date_str(d) -> str | None:
    """date / datetime / str を "YYYY-MM-DD" へ正規化する（None・空は None）。"""
    if not d:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def representative_snapshot_date(snap_dates) -> dict:
    """銘柄別スナップ日から producer の as-of 代表値を作る（Issue #417）。

    μ̂ は銘柄ごとに「その銘柄の最終週次バー」時点で計算される（`build_snapshots` の
    `is_current = (snap_idx == n - 1)`）。したがって as-of は本来 1 個の値ではない。
    ここを `max` で潰すと「最新の 1〜2 銘柄」が全体の as-of を名乗る
    （実測 2026-08-02: 2 銘柄 2026-07-31 / 3,677 銘柄 2026-07-13 → max は 19 日新しい嘘）。

    代表値は中央値（p50）を採る。偶数個は古い側（lower median）＝保守側へ寄せる。

    戻り値 `{"snapshot_date": p50, "snapshot_date_min": 最古, "snapshot_date_max": 最新,
    "n_stale": 代表値より古い銘柄数}`。入力が空なら日付 3 つとも None・n_stale=0。
    """
    ds = sorted(s for s in (to_date_str(d) for d in snap_dates) if s)
    if not ds:
        return {"snapshot_date": None, "snapshot_date_min": None,
                "snapshot_date_max": None, "n_stale": 0}
    p50 = ds[(len(ds) - 1) // 2]
    return {
        "snapshot_date":     p50,
        "snapshot_date_min": ds[0],
        "snapshot_date_max": ds[-1],
        "n_stale":           sum(1 for s in ds if s < p50),
    }


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
                # 低頻度系列（四半期=92日間隔等）は前年同期 ±window に点が無い（Issue #379）。
                # current 側（L233-239）と対称に「p_e 以前の直近点」へフォールバックする。
                # 古すぎる点で yoy が無意味化しないよう _YOY_PREV_FLOOR_DAYS で下限を切る。
                floor = (ref_1y - _td2(days=_YOY_PREV_FLOOR_DAYS)).isoformat()
                past = sorted((d, v) for d, v in date_close.items() if floor <= d <= p_e)
                if not past:
                    result[fname] = None
                    continue
                prev_vals = [past[-1][1]]
            prev_avg = statistics.mean(prev_vals)
            result[fname] = (current_avg - prev_avg) / prev_avg if prev_avg else None

        elif ttype == "zscore":
            from datetime import date as _d3, timedelta as _td3
            hist_start = (ref - _td3(days=zscore_years * 366)).isoformat()
            all_vals = [v for d, v in date_close.items() if hist_start <= d <= ref_date]
            if len(all_vals) < _ZSCORE_MIN_PTS:
                # 低頻度系列（IMF WEO=年2回）は5年窓に _ZSCORE_MIN_PTS 点が構造的に入らない
                # （実測5点・Issue #379）。窓を「直近 _ZSCORE_MIN_PTS 点」へ切り替えて点数を
                # 確保する（＝低頻度ほど実効窓が伸びる）。全履歴でも足りなければ
                # _ZSCORE_MIN_PTS_SPARSE 点まで許容し、それ未満のみ None。
                past = sorted((d, v) for d, v in date_close.items() if d <= ref_date)
                if len(past) >= _ZSCORE_MIN_PTS:
                    all_vals = [v for _, v in past[-_ZSCORE_MIN_PTS:]]
                elif len(past) >= _ZSCORE_MIN_PTS_SPARSE:
                    all_vals = [v for _, v in past]
                else:
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


# ── 1プロセス内で execute を複数回まわすときの共有スナップショットキャッシュ（Issue #298）──
# load_data/preload_macro/build_snapshots はいずれもモデルのハイパーパラメータに依存しない
# 重い処理（DB全件ロード・特徴量スナップショット構築）で、構造パラメータ
# （fin_features/macro_features/use_momentum/min_coverage 等）が同じなら結果が完全に一致する
# （実測: 1回あたり load_data 約23〜33秒 + build_snapshots 約25秒）。
# database.tuning_dry_run() と対になる contextvars.ContextVar パターンで、明示的に包んだ
# ブロックの中だけ有効なプロセス内メモリキャッシュ（DB永続化なし）を提供する。
#
# 利用者は2つ:
#   - ハイパーパラメータ探索（plugins/tuning.py の search()・Issue #298/#304）。各候補を
#     execute_plugin() でフル実行するため、候補数だけ同じロードが繰り返される。
#   - 夜間スコア更新バッチ（nightly_scores.py・Issue #443）。1プロセスで複数の producer を
#     順に回すため、モデルを増やすと週次127万行の再ロードが線形に増え、Supabase の
#     Egress 上限（5GB/月）を圧迫する。
#
# 通常の API 実行（/api/plugins/{name}/run）はこのコンテキストが未設定（None）のため
# 常に従来通りフル計算する（キャッシュは明示的に包んだ場合のみ・副作用が漏れ出さない）。

_CACHE_MAXSIZE = 8

_shared_cache: contextvars.ContextVar = contextvars.ContextVar("_shared_cache", default=None)


class _BoundedCache:
    """コンテキスト内専用の簡易 LRU（辞書/リスト引数を含むキーは hashable 化済み前提）。

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
# 専用ラッパー（下記）が直接 _shared_cache から引く。load_prices/load_macro_levels
# （M-3・plugins/macro_dlm.py）と cv_by_selected_features（M-1・plugins/macro_risk_return.py）
# は `shared_cache_get_or_compute` 経由で他モジュールから使う汎用名前空間（Issue #304）。
_CACHE_NAMESPACES = (
    "load_data", "preload_macro", "build_snapshots",
    "load_prices", "load_macro_levels",
    "cv_by_selected_features",
)


@contextmanager
def shared_snapshot_cache():
    """このブロック内ではハイパーパラメータに依存しない重い処理の結果をキャッシュする。

    利用者はハイパーパラメータ探索（plugins/tuning.py の search()）と夜間スコア更新バッチ
    （nightly_scores.py・Issue #443）の2つ。load_data/preload_macro/build_snapshots
    （Issue #298・M-1/M-2/M-6 共有）に加え、M-3 の load_prices/load_macro_levels
    （plugins/macro_dlm.py）・M-1 の BIC選択結果に紐づく CV 結果（cv_by_selected_features・
    plugins/macro_risk_return.py）も同じコンテキストで管理する（Issue #304・モジュールを
    またぐ利用は `shared_cache_get_or_compute` 経由）。ブロックを抜けるとキャッシュは破棄され、
    以後の呼び出し（次回の探索・通常の API 実行）には一切影響しない。

    **キャッシュが正しいのは「ブロック中に DB の中身が変わらない」ときだけ**。同じ db
    セッションに対して load_data の結果は不変という前提を置いている（探索は読むだけ、
    夜間バッチは producer が自分の出力テーブルへ書くだけで、キャッシュ対象の入力
    （株価・財務・マクロ）は書き換えない）。収集と同じプロセスで包んではいけない。
    """
    token = _shared_cache.set({ns: _BoundedCache() for ns in _CACHE_NAMESPACES})
    try:
        yield
    finally:
        _shared_cache.reset(token)


def shared_cache_get_or_compute(namespace: str, key: Any, compute) -> Any:
    """`shared_snapshot_cache()` の名前空間付きキャッシュへ汎用アクセスする（Issue #304）。

    macro_snapshots.py 外のモジュール（plugins/macro_dlm.py の load_prices/load_macro_levels・
    plugins/macro_risk_return.py の BIC選択結果に紐づく CV 結果）が、load_data 等と同じ
    contextvars パターンを再利用するための公開ヘルパー。`shared_snapshot_cache()` コンテキスト
    外（通常の API 実行）では常に `compute()` を実行しキャッシュしない。namespace は
    `_CACHE_NAMESPACES` に定義済みのもののみ有効（未定義は KeyError）。
    """
    cache = _shared_cache.get()
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

# `with_volume=False` でロードした行の volume_sum に入れる番兵（Issue #446）。
# **None にしない**——`build_price_features` の欠測扱い（nan）と区別できず、出来高z-score が
# 全 nan になっただけの状態を「データが薄い」と誤読する。#438 の「静かな固定」と同型の罠なので、
# 未ロードは値ではなく状態として持ち、参照されたら例外にする。
_VOLUME_NOT_LOADED = object()


def load_weekly_prices_chunked(db, batch: int = _WEEKLY_LOAD_BATCH,
                               with_volume: bool = True) -> dict:
    """stock_price_weekly を企業単位のチャンクに分割ロードし {edinet_code: [_WeeklyPX,...]}
    を返す（各社リストは trade_date 昇順）。Issue #311。

    edinet_code の一覧は companies から取る（週次株価の edinet_code は companies の部分集合＝
    孤立コード無しを実測確認済み。行数が全件 count と一致）。各チャンクは
    `WHERE edinet_code IN (...) ORDER BY edinet_code, trade_date` で PK インデックスを使う。
    M-1/M-2（load_data）と M-3（macro_dlm.load_prices）が共用する唯一の週次ローダー。

    volume_sum は価格行動系特徴量の `px_volz`（出来高z-score・Issue #317）**だけ**が読む。
    本番実測で 1,282,436 行 × 12.1MB/回の Egress を占めるため、`px_volz` を選んでいない
    呼び出し（M-1 は price_features 非対応・M-2/M-6 は既定 OFF）は `with_volume=False` で
    列ごと落とす（Issue #446・無料枠 5GB/月に対し夜間バッチ1回 86MB のうち 14%）。

    **全件ではなく差分で引く（#480・ADR-0036）。** 毎晩 1,282,436 行を引き直していたが増分は
    約4,400行＝転送の 99.7% が不変データの再送だった。`weekly_price_cache` が run 間の永続
    キャッシュを持ち、ここは「DB から何を引くか」だけを担う。戻り値の契約（各社 trade_date
    昇順・`_WeeklyPX`・番兵）はキャッシュ経由でも素通しでも同一。`FINAPP_WEEKLY_CACHE=0` で
    従来のフルロードへ戻せる（そのときは指紋クエリすら発行しない＝1文も変わらない）。
    """
    import weekly_price_cache as wpc
    from database import Company, StockPriceWeekly

    def _fetch(since):
        """since 以降（None なら全件）を {edinet_code: [_WeeklyPX,...]} で返す。"""
        codes = [c[0] for c in db.query(Company.edinet_code).all()]
        # plain dict + setdefault（defaultdict にしない）: 消費側は .get()/.items()/.values() か
        # 存在キー前提の [] アクセスのみで、欠損キーの暗黙生成に依存しない（従来の M-3 は plain dict）。
        prices_by_co: dict[str, list] = {}
        cols = [StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                StockPriceWeekly.close_last]
        if with_volume:
            cols.append(StockPriceWeekly.volume_sum)
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            q = db.query(*cols).filter(StockPriceWeekly.edinet_code.in_(chunk))
            if since is not None:
                # 高水位は **week_start**（PK 第2列）。`trade_date` は週内最終営業日で PK に
                # 含まれず nullable ＝範囲スキャンの索引条件に入らない。かつ、この条件は
                # `edinet_code IN (...)` の**中に**足すこと——week_start 単独では PK の先頭列に
                # ならず seq scan になり、チャンク分割の意味が消える。
                q = q.filter(StockPriceWeekly.week_start >= since)
            rows = q.order_by(StockPriceWeekly.edinet_code,
                              StockPriceWeekly.trade_date).all()
            # 行の幅で分岐せず **要求した列で** 決める。DB をモックするテストは cols に関わらず
            # 4要素の行を返すため、`for ec, td, cl in rows` と書くと本物だけ通ってモックが落ちる。
            for row in rows:
                vs = row[3] if with_volume else _VOLUME_NOT_LOADED
                prices_by_co.setdefault(row[0], []).append(_WEEKLY_PX(row[1], row[2], vs))
        return prices_by_co

    # ワイヤ形式は **素のタプル**。namedtuple ごと pickle すると `_VOLUME_NOT_LOADED`（object()）
    # の同一性が round-trip で壊れ、`is` 判定が False になる＝px_volz が ValueError を投げずに
    # 全 nan を返す「静かな故障」（#438/#446 が番兵で潰した罠）がキャッシュ経由で復活する。
    # 番兵の再付与はここで行い、キャッシュ層には行の型を一切知らせない。
    # trade_date は intern する: 1,282,436 行に対し distinct な日付は約1,500個しかなく、
    # 同一オブジェクトにしておくと pickle が memo 参照にまとめてファイルが大幅に縮む。
    def _to_wire(r):
        td = sys.intern(r.trade_date) if isinstance(r.trade_date, str) else r.trade_date
        return (td, r.close_last, r.volume_sum) if with_volume else (td, r.close_last)

    def _from_wire(t):
        return _WEEKLY_PX(t[0], t[1], t[2] if with_volume else _VOLUME_NOT_LOADED)

    return wpc.load_incremental(
        db, with_volume=with_volume, fetch=_fetch, to_wire=_to_wire,
        from_wire=_from_wire, trade_date_of=lambda r: r.trade_date,
    )


def load_data(db, with_volume: bool = True) -> tuple:
    """Company / FinancialMetric / StockPriceWeekly を一括ロード。

    shared_snapshot_cache() コンテキスト内では (id(db), with_volume) 単位で結果を
    キャッシュし、2回目以降の呼び出しは DB へ再クエリしない（Issue #298）。探索中は
    同一 db セッションに対して結果は不変という前提。コンテキスト外では常にフル計算する。

    with_volume: 週次の `volume_sum` を引くか。`px_volz` を選んでいる呼び出しだけ True に
    する（Issue #446）。**キャッシュキーに含める**——False でロードした結果を True の要求へ
    再利用すると `px_volz` が壊れる（番兵に当たって ValueError）。
    """
    cache = _shared_cache.get()
    if cache is None:
        return _load_data_impl(db, with_volume)
    return cache["load_data"].get_or_compute(
        (id(db), with_volume), lambda: _load_data_impl(db, with_volume)
    )


def _load_data_impl(db, with_volume: bool = True) -> tuple:
    """load_data の実体（キャッシュなし・毎回フル計算）。

    financial_metrics は ORM 全列（97列）ではなく `FIN_LOAD_FIELDS` だけを引き、軽量な
    `_FinRow`（namedtuple）で返す（Issue #459）。**行の幅で分岐せず要求した列で決める**——
    DB をモックするテストは要求列に関わらず固定幅の行を返しうるため、位置展開にしておくと
    本物とモックのズレがテストで露見する（`load_weekly_prices_chunked` と同じ考え方）。

    `companies` は全列でも実測 0.5MB と小さいので絞らない（#446 の実測表）。
    """
    from database import Company, FinancialMetric
    # 週次株価は単一クエリだと本番 pooler で timeout/接続破損するため分割ロード（Issue #311）。
    prices_by_co = load_weekly_prices_chunked(db, with_volume=with_volume)

    fin_cols = [getattr(FinancialMetric, f) for f in FIN_LOAD_FIELDS]
    fin_by_co: dict[str, list] = defaultdict(list)
    for row in (db.query(*fin_cols)
                .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end)
                .all()):
        rec = _FinRow(*row)
        fin_by_co[rec.edinet_code].append(rec)

    companies = {c.edinet_code: c for c in db.query(Company).all()}
    return prices_by_co, fin_by_co, companies


def preload_macro(db, prices_by_co: dict, macro_names: list[str] | None = None) -> dict:
    """MacroData を一括プリロードしキャッシュ dict を返す。

    shared_snapshot_cache() コンテキスト内では (id(prices_by_co), macro_names) の
    組み合わせで結果をキャッシュする（Issue #298）。コンテキスト外では常にフル計算する。
    """
    cache = _shared_cache.get()
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
    # 戻り値は {series_code: {trade_date: close}} だけなので3列で足りる。ORM 行（open/high/low/
    # volume/series_name/category/created_at 込み）を引くと本番実測で 8.7MB/回、3列なら 2.6MB
    # ＝夜間バッチ1回 86MB のうち 7%（Issue #446）。
    q = (
        db.query(MacroData.series_code, MacroData.trade_date, MacroData.close)
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
    # 属性で読む（tuple 展開にしない）: SQLAlchemy の Row は列名で属性アクセスでき、
    # DB をモックして ORM 風オブジェクトを返すテストとも両立する。
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
    price_features: list[str] | None = None,
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

    price_features（既定 None＝空・Issue #364）: 価格行動系特徴量（px_rvol/px_volz/
      px_high52dev/px_rev4w）を指定すると、各スナップショット snap_idx 時点の既知値を
      momentum の直後・交差項の手前に追加する（feature 名は all_feat_names 末尾側へ）。
      M-2（GBDT・NaN ネイティブ処理）専用で `use_momentum` と同様に呼び出し側でゲートする。
      M-1（OLS）は既定の None のままにして OLS 特徴を汚さない。px_* は全て無次元→木で
      単調不変・次元整合 OK。px_high52dev の52週 warmup 分は nan となり coverage で制御される。

    shared_snapshot_cache() コンテキスト内では、大きいオブジェクト引数（prices_by_co/
    fin_by_co/companies/macro_cache）は id()、それ以外の引数は値そのものからキーを
    構築してキャッシュする（Issue #298）。コンテキスト外では常にフル計算する。
    """
    price_features = list(price_features) if price_features else []
    cache = _shared_cache.get()
    if cache is None:
        return _build_snapshots_impl(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions, macro_nan_ok, return_stock_ids, price_features,
        )
    key = (
        id(prices_by_co), id(fin_by_co), id(companies), id(macro_cache),
        tuple(fin_features), tuple(macro_names), use_momentum, mom_window,
        min_coverage, build_interactions, macro_nan_ok, return_stock_ids,
        tuple(price_features),
    )
    return cache["build_snapshots"].get_or_compute(
        key,
        lambda: _build_snapshots_impl(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions, macro_nan_ok, return_stock_ids, price_features,
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
    price_features: list[str] | None = None,
) -> tuple:
    """build_snapshots の実体（キャッシュなし・毎回フル計算）。"""
    use_macro = bool(macro_names)
    price_features = price_features or []
    momentum_name = ["momentum_12m1"] if use_momentum else []

    interaction_names: list[str] = []
    if build_interactions and use_macro:
        for fn in fin_features:
            for mn in macro_names:
                interaction_names.append(f"{fn}_x_{mn}")

    all_feat_names = (
        fin_features + macro_names + momentum_name + list(price_features) + interaction_names
    )
    n_feat = len(all_feat_names)

    # ロードされていない財務列を早期に落とす（Issue #459）。`load_data` は FIN_LOAD_FIELDS
    # だけを引くため、範囲外の列は `getattr(fin_rec, fn, None)` が None を返し、**欠測と同じ
    # 経路で全社が静かに捨てられて「データが薄い」に化ける**。属性が無い＝設定ミス、属性が
    # あって値が None＝欠測、として分ける（#446 の番兵と同じ考え方）。
    probe = next((recs[0] for recs in fin_by_co.values() if recs), None)
    if probe is not None:
        unknown = [fn for fn in fin_features if not hasattr(probe, fn)]
        if unknown:
            raise ValueError(
                f"build_snapshots: ロードされていない財務列が fin_features にあります: {unknown}"
                "（plugins/macro_snapshots.py の FIN_LOAD_FIELDS へ追加してください・Issue #459）")

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

        # 価格行動系特徴量（Issue #364）: 銘柄単位で1回だけ週インデックス整列で事前計算。
        # 各 snap_idx 時点の既知値（未来を覗かない）を feat_row 末尾側へ追加する。
        px_feats = build_price_features(price_rows, price_features) if price_features else {}

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

            # 価格行動系特徴量（snap_idx 時点の既知値・warmup 未満は nan → XGBoost 処理）
            px_row: list[float] = [
                float(px_feats[name][snap_idx]) for name in price_features
            ] if price_features else []

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

            feat_row = fin_row + macro_row + mom_row + px_row + inter_row

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


def _industry_neutral_spearman(yhats: list, ytrues: list, industries: list) -> float | None:
    """業種中立 rank-IC（Issue #368・Grinold & Kahn 流の順位版）。

    各業種内で yhat/y_true をそれぞれ平均順位化→業種平均順位を引く（順位デミーン）→
    全業種をプールして Spearman を取る。これにより「素材>ハイテクを WTI で一括に並べる」
    ような**業種ベット（セクター傾斜）で稼いだ IC** を除去し、**業種内の真の銘柄選択力**
    だけを測る。単独銘柄の業種はデミーンで消える（情報量ゼロ）ため除外する。
    有効ペア<3 または無分散なら None。
    """
    groups: dict = defaultdict(list)
    for i, ind in enumerate(industries):
        groups[ind].append(i)
    dyh: list[float] = []
    dyt: list[float] = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        ry = _avg_ranks([yhats[i] for i in idxs])
        rt = _avg_ranks([ytrues[i] for i in idxs])
        my = sum(ry) / len(ry)
        mt = sum(rt) / len(rt)
        dyh.extend(r - my for r in ry)
        dyt.extend(r - mt for r in rt)
    return _spearman(dyh, dyt)


def _jaccard_nonoverlap(a: set, b: set) -> float | None:
    """1 − |a∩b|/|a∪b|（両集合とも空なら None）。分位メンバーシップの入替割合＝実効ターンオーバー。"""
    union = a | b
    if not union:
        return None
    return 1.0 - len(a & b) / len(union)


def build_oof_meta(stock_ids_by_ym: dict | None, sample_meta_by_ym: dict, yms) -> dict:
    """oof_backtest 用の {ym: [(stock_id, industry), ...]} を残差と同順で組む（Issue #368）。

    build_snapshots は samples_by_ym / sample_meta_by_ym / stock_ids_by_ym を同一サンプル順で
    返し、walk_forward_cv_monthly の残差もその順序を保存する（_compute_r3_buckets や
    macro_ensemble._align が依拠する既存契約）。よって index で 1:1 突合できる。
      sample_meta_by_ym[ym][j] = (industry, size)（build_snapshots 出力）。
      stock_ids_by_ym[ym][j]  = edinet_code（return_stock_ids=True 時・None 可＝ターンオーバー無効）。
    """
    meta: dict = {}
    for ym in yms:
        metas = sample_meta_by_ym.get(ym, [])
        sids = (stock_ids_by_ym or {}).get(ym, [])
        meta[ym] = [
            (sids[j] if j < len(sids) else None,
             metas[j][0] if j < len(metas) else None)
            for j in range(len(metas))
        ]
    return meta


# ── コンフォーマル予測区間（Issue #365・分割コンフォーマル）──────────────────
# XGBoost（M-2）は OLS のような閉形式の予測 SE を持たない。代わりに無リーク
# walk-forward OOF 残差の絶対値の τ 分位を区間半幅（r1_prime）とする分割コンフォーマル
# （Lei et al. 2018, JASA DOI:10.1080/01621459.2017.1307116）で確実性軸を与える。
# marginal 版（全銘柄一定半幅）は sell_ranking の R3 足切りゲートを全通過/全遮断の二択に
# 退化させるため、既存 R3 バケット（業種×サイズ三分位）条件付きで per-stock 化する
# （Koenker & Bassett 1978 の分位のノンパラ近似）。バケット→業種→global の順に最小標本数を
# 満たす最も細かい粒度を採用（_compute_r3_buckets / _r3_for と同一フォールバック規約）。
CONFORMAL_TAU = 0.9          # 既定被覆水準（|resid| の 0.9 分位＝片側 90% を半幅とする）
CONFORMAL_MIN_BUCKET = 20    # τ=0.9 分位が安定する最小 |resid| 標本数（未満は下位粒度へ）


def _conformal_size_thresholds(meta_by_ym: dict) -> tuple | None:
    """meta_by_ym の (sector, size) から size 三分位閾値 (t1,t2) を返す。

    ADR-0003 の階層（macro_snapshots が基盤・M-1/M-2 がその上）を保つため、
    macro_risk_return._compute_r3_buckets と同一の三分位ロジックを本モジュール内に複製する
    （M-1 への上向き依存を作らない）。"""
    sizes = [size for ym in meta_by_ym for (_sec, size) in meta_by_ym.get(ym, [])
             if size is not None and size > 0]
    if len(sizes) < 3:
        return None
    ss = sorted(sizes)
    return (ss[len(ss) // 3], ss[2 * len(ss) // 3])


def _conformal_size_bucket(size, thresholds) -> str | None:
    """総資産を S/M/L の三分位バケットへ（_M1._size_bucket と同一・欠損は None）。"""
    if size is None or size <= 0 or thresholds is None:
        return None
    t1, t2 = thresholds
    return "S" if size < t1 else ("M" if size < t2 else "L")


def conformal_bucket_halfwidths(residuals_by_ym: dict, meta_by_ym: dict,
                                tau: float = CONFORMAL_TAU,
                                min_bucket: int = CONFORMAL_MIN_BUCKET) -> dict:
    """OOF 残差 |resid| の τ 分位を (業種×サイズ)/業種/global 粒度で集計し半幅マップを返す。

    residuals_by_ym = {ym: [(yhat, ytrue), ...]}（walk_forward_cv_monthly の return_residuals=True）。
    meta_by_ym[ym][k] = (sector, size)（build_snapshots の sample_meta_by_ym・残差と同順）。
    返り値: {"bucket":{(sec,bkt):hw}, "sector":{sec:hw}, "global":hw|None,
             "thresholds":(t1,t2)|None, "tau":tau}。半幅は該当粒度の標本数 >= min_bucket の
    ときのみ格納（未満は conformal_halfwidth_for が下位粒度へフォールバック）。"""
    thresholds = _conformal_size_thresholds(meta_by_ym)
    bkt_abs: dict = defaultdict(list)
    sec_abs: dict = defaultdict(list)
    glob_abs: list = []
    for ym, resids in residuals_by_ym.items():
        metas = meta_by_ym.get(ym, [])
        for k, (yhat, ytrue) in enumerate(resids):
            if k >= len(metas):
                break  # 添字対応が崩れた場合の安全策（通常は同長）
            sec, size = metas[k]
            ae = abs(ytrue - yhat)
            glob_abs.append(ae)
            if sec:
                sec_abs[sec].append(ae)
                b = _conformal_size_bucket(size, thresholds)
                if b is not None:
                    bkt_abs[(sec, b)].append(ae)

    def _q(vals):
        return float(np.quantile(vals, tau)) if len(vals) >= min_bucket else None

    return {
        "bucket":     {k: q for k, v in bkt_abs.items() if (q := _q(v)) is not None},
        "sector":     {k: q for k, v in sec_abs.items() if (q := _q(v)) is not None},
        "global":     (float(np.quantile(glob_abs, tau)) if glob_abs else None),
        "thresholds": thresholds,
        "tau":        tau,
    }


def conformal_halfwidth_for(sector, size, data: dict) -> float | None:
    """企業の (sector, size) から r1_prime（区間半幅）を返す。bucket→sector→global フォールバック。"""
    if not data:
        return None
    bkt = _conformal_size_bucket(size, data.get("thresholds"))
    if sector and bkt is not None:
        hw = data["bucket"].get((sector, bkt))
        if hw is not None:
            return hw
    if sector:
        hw = data["sector"].get(sector)
        if hw is not None:
            return hw
    return data.get("global")


def oof_backtest(residuals_by_ym: dict, n_quantiles: int = 5, cost_bps: float = 0.0,
                 meta_by_ym: dict | None = None, rebalance_per_year: float | None = None,
                 tau: float = CONFORMAL_TAU) -> dict:
    """無リーク OOF 予測から「アウトオブサンプル検証（OOF）」指標を算出する（ADR-0004）。

    residuals_by_ym = {test_ym: [(yhat, y_true), ...]}（walk_forward_cv_monthly の
    return_residuals=True 出力・テストサンプル順）。再学習・追加の価格取得は不要。

    手法（ADR-0004「分位の作り方」）:
      - 各 test_ym 内で yhat を横断ランク→ n_quantiles 分位→分位平均 y_true（期内）
        →期間平均（per-period cross-sectional・μ̂ 水準の時系列ドリフトに頑健）。
      - rank-IC = Spearman(yhat, y_true) を fold 毎→ mean/std/n。
      - long_short_spread = top分位平均 − bottom分位平均（期間平均）。
      - hit_rate = top分位 > bottom分位 だった期の割合。
      - short_side_spread = 期内全体平均 − bottom分位平均（期間平均・Issue #402）。売り判定
        （sell_ranking は μ̂ 下位を売る）専用の識別力。正なら売り候補が市場平均を下回った。
        short_side_hit_rate はそれが成立した期の割合、short_side_spread_by_period は per-fold 系列。
      - 期内サンプルが n_quantiles*2 未満の期は分位計算から自動除外（IC には使用）。
    quantile_returns[0]=最低 μ̂ バケット, [-1]=最高 μ̂ バケットの実現リターン。

    cost_bps（Issue #316）: 片道売買コスト（bp、1bp=0.01%）。long_short_spread は毎期
    ロング・ショートを1回転する前提のため、往復（買い+売り）で2倍控除した
    long_short_spread_net を併記する（デフォルト0＝控除なし・既存キーは不変）。
    このコストは「期」1回あたりの控除であり、期の頻度（週次/月次）はホライズン
    ごとに異なる（ADR-0012）ため、複数モデルの spread を跨いで cost_bps ベースで
    直接比較する場合は呼び出し側で頻度差を考慮すること。

    meta_by_ym（Issue #368・任意）: {test_ym: [(stock_id, industry), ...]}。residuals_by_ym と
    同順（build_oof_meta が組む）。渡すと以下を追加算出する（無印キーは不変・後方互換）:
      - rank_ic_industry_neutral: 業種内で順位デミーンしてから Spearman（業種ベットを除去した
        真の銘柄選択力）。期毎に算出しサンプル数で加重平均。industry が None の行は除外。
      - effective_turnover: 隣接期の top/bottom 分位メンバーシップ（stock_id）の Jaccard 非重複
        （入替割合）の平均。安定・低回転モデルほど小さい。stock_id が無ければ None。
      - breakeven_cost_bps: エッジ（gross spread）を実効ターンオーバーで割り戻し、片道コスト何 bp で
        long_short_spread が消えるか＝ gross·50/turnover（cost_bps 換算と同一規約）。頻度依存の
        gross/turnover が比で相殺されるため、リバランス頻度に依らずモデル横断で直接比較できる
        単一スカラー（Grinold & Kahn "Active Portfolio Management" の turnover 調整）。
      - long_short_spread_net_turnover: gross − (cost_bps/100)·2·turnover（実効回転で控除・#316 の
        100%回転固定版 long_short_spread_net の一般化）。
    rebalance_per_year（任意）: 年間リバランス回数。渡すと annual_turnover（＝実効回転×頻度）を
    参考値として併記する（breakeven_cost_bps 自体は頻度不変）。
    """
    yms = sorted(residuals_by_ym.keys())
    n_oof = sum(len(residuals_by_ym[y]) for y in yms)

    # rank-IC（fold 毎・サンプル<3 の期は除外）。ym→ic を保持し、モデル間の
    # 「共通 test 期ペアリング」（model_stats.paired_ic_significance・Issue #369）へ供する。
    # 業種中立 rank-IC（Issue #368・meta_by_ym 有時のみ）: 同じ第1ループで期毎に算出し
    # (ic, 有効n) を蓄積してサンプル数加重平均する。
    ic_by_period: dict[str, float] = {}
    in_ic_pairs: list[tuple[float, int]] = []
    for ym in yms:
        pairs = residuals_by_ym[ym]
        ic = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ic_by_period[ym] = ic
        if meta_by_ym is not None:
            metas = meta_by_ym.get(ym, [])
            idxs = [i for i in range(len(pairs)) if i < len(metas) and metas[i][1] is not None]
            if len(idxs) >= 3:
                nic = _industry_neutral_spearman(
                    [pairs[i][0] for i in idxs],
                    [pairs[i][1] for i in idxs],
                    [metas[i][1] for i in idxs],
                )
                if nic is not None:
                    in_ic_pairs.append((nic, len(idxs)))
    ics = list(ic_by_period.values())
    ic_mean = statistics.mean(ics) if ics else None
    ic_std = statistics.pstdev(ics) if len(ics) > 1 else (0.0 if ics else None)
    in_ic_n = len(in_ic_pairs)
    _in_w = sum(n for _, n in in_ic_pairs)
    in_ic_mean = (sum(ic * n for ic, n in in_ic_pairs) / _in_w) if _in_w else None

    # 期内横断分位リターン（各期で yhat 昇順→ n_quantiles 等分→分位平均 y_true）
    q_sums = [0.0] * n_quantiles
    q_periods = 0
    ls_spreads: list[float] = []
    hits = 0
    # 単調性（Issue #369）: top-bottom spread へ畳むと中間分位の U 字/非単調が隠れる。
    # 期毎に Spearman(分位idx, 分位平均) と隣接分位の正順数を蓄積し model_stats で畳む。
    q_idx = list(range(n_quantiles))
    mono_spearmans: list[float] = []
    adj_increasing = 0
    adj_total = 0
    # 実効ターンオーバー（Issue #368・meta_by_ym 有時）: 分位計算対象の各期で top/bottom
    # 分位の stock_id 集合を保持し、後段で隣接期の Jaccard 非重複を平均する。yms は昇順の
    # ため append 順＝時系列順。
    membership: list[tuple[set, set]] = []
    # 売り側（ショート側）識別力（Issue #402）: sell_ranking（ADR-0004 の下流）は μ̂ **下位**を
    # 売る。long_short_spread は top 分位の強さに引っ張られるため、μ̂ 出所を売り判定基準で
    # 選ぶ材料にならない（top が強いだけのモデルでも spread は大きくなる）。ロングオンリーの
    # 保有者にとっての売りの価値は「市場平均 − 売り候補平均」なので、期内全体平均を
    # ベンチマークに bottom 分位の劣後幅を測る。正なら売り候補が市場平均を下回った＝有効。
    ss_spread_by_period: dict[str, float] = {}
    ss_hits = 0
    for ym in yms:
        pairs = residuals_by_ym[ym]
        if len(pairs) < n_quantiles * 2:
            continue
        metas = meta_by_ym.get(ym, []) if meta_by_ym is not None else []
        order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])   # yhat 昇順（index）
        m = len(order)
        q_means = []
        top_ids: set = set()
        bot_ids: set = set()
        for q in range(n_quantiles):
            lo = q * m // n_quantiles
            hi = (q + 1) * m // n_quantiles
            seg = order[lo:hi]
            q_means.append(sum(pairs[i][1] for i in seg) / len(seg))
            if meta_by_ym is not None:
                sid_set = {metas[i][0] for i in seg if i < len(metas) and metas[i][0] is not None}
                if q == n_quantiles - 1:
                    top_ids = sid_set
                elif q == 0:
                    bot_ids = sid_set
        for q in range(n_quantiles):
            q_sums[q] += q_means[q]
        q_periods += 1
        if meta_by_ym is not None:
            membership.append((top_ids, bot_ids))
        ls = q_means[-1] - q_means[0]   # top（高 yhat）− bottom（低 yhat）
        ls_spreads.append(ls)
        if ls > 0:
            hits += 1
        # 売り側 spread（Issue #402）: 期内全体平均 − bottom 分位平均。分位平均は
        # 等分セグメントの単純平均だが端数（m % n_quantiles）で分位サイズが不均一に
        # なりうるため、ベンチマークは q_means の平均ではなく全サンプル平均で取る。
        ss = sum(p[1] for p in pairs) / m - q_means[0]
        ss_spread_by_period[ym] = ss
        if ss > 0:
            ss_hits += 1
        sp = _spearman(q_idx, q_means)   # 分位idx と 分位平均の順位相関（+1=完全単調）
        if sp is not None:
            mono_spearmans.append(sp)
        for i in range(n_quantiles - 1):
            adj_total += 1
            if q_means[i + 1] > q_means[i]:
                adj_increasing += 1

    quantile_returns = [round(s / q_periods, 6) for s in q_sums] if q_periods else []
    long_short_spread = round(statistics.mean(ls_spreads), 6) if ls_spreads else None
    hit_rate = round(hits / q_periods, 4) if q_periods else None
    short_side_spread = (round(statistics.mean(ss_spread_by_period.values()), 6)
                         if ss_spread_by_period else None)
    short_side_hit_rate = (round(ss_hits / len(ss_spread_by_period), 4)
                           if ss_spread_by_period else None)

    round_trip_cost_pct = cost_bps / 100.0 * 2
    long_short_spread_net = (
        round(long_short_spread - round_trip_cost_pct, 6) if long_short_spread is not None else None
    )

    # ── 実効ターンオーバー → ネット / ブレークイーブンbps（Issue #368）─────────────
    # 隣接期の top・bottom 分位メンバーシップの Jaccard 非重複を平均。t=0 は完全据置、
    # t=1 は毎期総入替。gross・turnover はともにリバランス頻度に比例するため breakeven は
    # 比で頻度不変（モデル横断で直接比較可能な単一スカラー）。
    effective_turnover = None
    if len(membership) >= 2:
        ts: list[float] = []
        for (t0, b0), (t1, b1) in zip(membership, membership[1:]):
            parts = [x for x in (_jaccard_nonoverlap(t0, t1), _jaccard_nonoverlap(b0, b1))
                     if x is not None]
            if parts:
                ts.append(sum(parts) / len(parts))
        effective_turnover = round(statistics.mean(ts), 6) if ts else None

    long_short_spread_net_turnover = (
        round(long_short_spread - round_trip_cost_pct * effective_turnover, 6)
        if (long_short_spread is not None and effective_turnover is not None) else None
    )
    # gross·50/turnover: net = gross − (cost_bps/100)·2·turnover = 0 を解いた片道コスト[bp]。
    breakeven_cost_bps = (
        round(long_short_spread * 50.0 / effective_turnover, 2)
        if (long_short_spread is not None and long_short_spread > 0
            and effective_turnover is not None and effective_turnover > 0) else None
    )
    annual_turnover = (
        round(effective_turnover * rebalance_per_year, 4)
        if (effective_turnover is not None and rebalance_per_year) else None
    )

    # ── コンフォーマル区間の被覆診断（Issue #365）─────────────────────────────
    # honest split-conformal 被覆率: 各 test 期を、それより前の全 test 期の |resid| で較正した
    # marginal 半幅（τ 分位）で被覆判定し、標本加重平均する（Lei et al. 2018 の妥当性検査）。
    # 追加学習・Egress ゼロ。marginal（全銘柄一定半幅）で、per-stock バケット化は producer 側
    # （conformal_bucket_halfwidths）が別途担う。yms は昇順のため calib は過去のみで無リーク。
    cov_covered = 0
    cov_total = 0
    calib_abs: list = []
    for ym in yms:
        if len(calib_abs) >= CONFORMAL_MIN_BUCKET:
            hw = float(np.quantile(calib_abs, tau))
            for yhat, ytrue in residuals_by_ym[ym]:
                cov_total += 1
                if abs(ytrue - yhat) <= hw:
                    cov_covered += 1
        calib_abs.extend(abs(ytrue - yhat) for yhat, ytrue in residuals_by_ym[ym])
    interval_coverage = round(cov_covered / cov_total, 4) if cov_total else None
    interval_halfwidth = round(float(np.quantile(calib_abs, tau)), 6) if calib_abs else None

    # 分位単調性の畳み込み（純後処理・Egress ゼロ）。model_stats はブートストラップに
    # stdlib random のみ使用（seed 固定で決定的）。
    from model_stats import monotonicity_summary
    monotonicity = monotonicity_summary(mono_spearmans, adj_increasing, adj_total)

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
        # per-fold IC（ym→ic）: モデル間の共通 test 期ペアリング用（Issue #369）。
        "rank_ic_by_period":     {ym: round(v, 6) for ym, v in ic_by_period.items()},
        # 業種中立 rank-IC（Issue #368・meta_by_ym 有時のみ非 None）。
        "rank_ic_industry_neutral": {
            "mean": round(in_ic_mean, 4) if in_ic_mean is not None else None,
            "n":    in_ic_n,
        },
        "monotonicity":          monotonicity,
        "long_short_spread":     long_short_spread,
        "hit_rate":              hit_rate,
        # 売り側識別力（Issue #402・sell_ranking の μ 出所選定用）。期内全体平均 − bottom
        # 分位平均の期間平均／勝率／per-fold 系列（model_stats.paired_ic_significance で
        # モデル間の共通 test 期ペアリング検定に渡せる形）。
        "short_side_spread":           short_side_spread,
        "short_side_hit_rate":         short_side_hit_rate,
        "short_side_spread_by_period": {ym: round(v, 6) for ym, v in ss_spread_by_period.items()},
        "cost_bps":              cost_bps,
        "long_short_spread_net": long_short_spread_net,
        # ターンオーバー調整（Issue #368・meta_by_ym 有時のみ非 None）。
        "effective_turnover":             effective_turnover,
        "annual_turnover":                annual_turnover,
        "long_short_spread_net_turnover": long_short_spread_net_turnover,
        "breakeven_cost_bps":             breakeven_cost_bps,
        # コンフォーマル区間の被覆診断（Issue #365・全モデル family-wide）。honest
        # walk-forward split-conformal の実測被覆率。理想は ≈ interval_tau。
        "interval_coverage":   interval_coverage,
        "interval_tau":        tau,
        "interval_halfwidth":  interval_halfwidth,
        "n_interval_calib":    cov_total,
    }
