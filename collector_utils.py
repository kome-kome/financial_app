"""collector パッケージ共通の設定定数・ロガー。

収集系モジュール（collector_prices / collector_financials / collector_master）が
共有する設定値とロガーを集約する。ドメイン固有の定数は各モジュール側に置く。
"""
import os
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

EDINET_BASE   = "https://disclosure.edinet-fsa.go.jp/api/v2"
JPX_EXCEL_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
API_KEY       = os.environ.get("EDINET_API_KEY", "")
RATE_SLEEP             = 0.6   # EDINET API のリクエスト間隔（秒）
BATCH_PAUSE            = 3.0   # 100件ごとの追加ポーズ（秒）
STOOQ_CONCURRENCY      = 30    # stooq 現在株価の同時接続数
STOOQ_HIST_CONCURRENCY = 20    # stooq 履歴の同時接続数（1リクエストが重いため控えめ）
JQUANTS_ENDPOINT             = "https://api.jquants.com/v2/equities/bars/daily"
JQUANTS_LISTED_INFO_ENDPOINT = "https://api.jquants.com/v2/markets/listed/info"
JQUANTS_RATE_SLEEP           = 20.0  # リクエスト開始間隔の最低値（秒）。
JQUANTS_BACKFILL_DAYS        = 730   # J-Quants 無料プランの最大取得可能期間（2年分）
YAHOO_STOCK_RATE_SLEEP = 0.5   # Yahoo Finance 銘柄別取得のリクエスト間隔（秒）
                               # 銘柄ごとに1リクエスト。3800社×0.5s ≈ 32分
MAX_GAP_DAYS           = 30    # period_end から±30日以内の株価のみ採用（point_in_time マッチ）
                               # 実測：データ日は約8s、非営業日は約3s で応答。
                               # 無料プランの上限が約5リクエスト/60秒のため20s を確保して安全マージンを持たせる。
                               # データ日はダウンロードに~8秒かかるため追加待機ほぼゼロ。
                               # 祝日（即時400）の後は残り~7秒を補完スリープ。

# --- バッチ処理間隔（コミット/スリープ/進捗）。N 件ごとに処理を区切る。値は現状維持。---
PRICE_COMMIT_BATCH            = 200  # 株価レコード更新のコミット間隔
MASTER_COMMIT_BATCH          = 200  # 企業マスタ保存のコミット間隔
REPARSE_COMMIT_BATCH         = 100  # XBRL 再解析・CF 補完のコミット間隔
MARKET_COMMIT_BATCH          = 50   # 市場データ（株価）更新のコミット間隔
COLLECT_COMMIT_BATCH         = 50   # 全件収集（財務）のコミット間隔
COLLECT_SLEEP_BATCH          = 100  # 全件収集で BATCH_PAUSE を挟む間隔
PROGRESS_LOG_BATCH           = 100  # 進捗ログ出力の間隔
PROGRESS_REPORT_BATCH        = 500  # 進捗コールバック報告の間隔
YAHOO_BACKFILL_PROGRESS_BATCH = 200 # Yahoo backfill の進捗報告間隔

# Supabase Free プランの DB 容量制約(500MB)で xbrl_raw_documents (TOAST 880MB)
# を持てないため、デフォルトで保存をスキップ。再解析が必要な場合のみ
# SKIP_XBRL_RAW=false にすると保存される
SKIP_XBRL_RAW = os.environ.get("SKIP_XBRL_RAW", "true").lower() == "true"
