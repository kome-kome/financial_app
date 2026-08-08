"""collector パッケージ共通の設定定数・ロガー。

収集系モジュール（collector_prices / collector_financials / collector_master）が
共有する設定値とロガーを集約する。ドメイン固有の定数は各モジュール側に置く。
"""
import os
import logging
import re
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx は "HTTP Request: GET <url> ..." を INFO で出力し、クエリパラメータに載る
# APIキー（FRED_API_KEY/ESTAT_API_KEY 等）がそのままログへ残ってしまうため抑制する。
for _noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)
log = logging.getLogger("collector")

EDINET_BASE   = "https://disclosure.edinet-fsa.go.jp/api/v2"
JPX_EXCEL_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
API_KEY       = os.environ.get("EDINET_API_KEY", "")
RATE_SLEEP             = 0.6   # EDINET API のリクエスト間隔（秒）
BATCH_PAUSE            = 3.0   # 100件ごとの追加ポーズ（秒）
STOOQ_CONCURRENCY      = 30    # stooq 現在株価の同時接続数
STOOQ_HIST_CONCURRENCY = 20    # stooq 履歴の同時接続数（1リクエストが重いため控えめ）
JQUANTS_ENDPOINT             = "https://api.jquants.com/v2/equities/bars/daily"
# 上場銘柄一覧。v1 の `/markets/listed/info` は **v2 に存在しない**（403
# `The requested endpoint does not exist`）。後継は `/equities/master`（#462・2026-08-08 実測で
# 4,446銘柄）。ただし master は発行済株式数を持たない（Code/CoName/Mkt/Mrgn/S17/S33/ScaleCat/Date のみ）
# ため、issued_shares は `/fins/summary` の ShOutFY 経由で取る（collector_disclosures）。
JQUANTS_MASTER_ENDPOINT      = "https://api.jquants.com/v2/equities/master"
JQUANTS_SUMMARY_ENDPOINT     = "https://api.jquants.com/v2/fins/summary"  # 会社予想・決算短信サマリー（Issue #322）
JQUANTS_RATE_SLEEP           = 20.0  # リクエスト開始間隔の最低値（秒）。
JQUANTS_BACKFILL_DAYS        = 730   # J-Quants 無料プランの最大取得可能期間（2年分）
JQUANTS_DISCLOSURE_DELAY_DAYS = 84   # /fins/summary 無料プランの配信遅延（実測・12週固定。Issue #322 調査コメント参照）

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


# --- J-Quants のステータスコード解釈（2026-08-08 実測・#462）-------------------
# **403 はカバレッジ境界を意味しない**。契約が有効な状態で窓の外側を叩くと 400 が返り、
# ボディに `Your subscription covers the following dates: A ~ B` が載る（過去側・エンバーゴ
# 側の両端で同一）。403 は下記3種のいずれかで、**どれも日付を変えても直らない**。
# #412 / #425 が「403＝カバー範囲外」と読んだ観測は、実際には契約失効（#461）だった。
JQUANTS_NO_SUBSCRIPTION_MARK = "no active subscription"          # 契約が有効でない
JQUANTS_PLAN_RESTRICTED_MARK = "not available on your subscription"  # プラン対象外の API
JQUANTS_ENDPOINT_MISSING_MARK = "requested endpoint does not exist"  # v2 に存在しない URL
# 400 のボディに載るカバレッジ窓の文言（`Your subscription covers the following dates: ...`）
JQUANTS_COVERAGE_MARK = "subscription covers the following dates"

# 連続でこの日数だけ 403 が続いたら以降の日付を叩かない（#461）。403 は上記3種のいずれかで
# 日付非依存＝続けても全日 403 になり、窓の長さ × JQUANTS_RATE_SLEEP をまるごと捨てる
# （実測: 730日窓＝523営業日 × 20秒 = 174分を空振りに使い full-pipeline finalize が
# timeout した・run 31126473273）。
JQUANTS_MAX_CONSECUTIVE_FORBIDDEN = 10

_JQUANTS_COVERAGE_RE = re.compile(
    r"covers the following dates:\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})"
)


def classify_jquants_forbidden(body: str) -> str:
    """403 のボディから理由を分類する（#462）。

    戻り値: "no_subscription" / "plan_restricted" / "endpoint_missing" / "unknown"。
    いずれも「同じリクエストを別の日付で投げても直らない」点が共通するため、呼び出し側は
    理由によらず打ち切ってよい。分類はログの文言と対処（契約を見るのか URL を直すのか）を
    分けるために持つ——**ステータスコードだけ見て理由を推測すると、プラン制約と URL 誤りが
    同じ記述に潰れる**（#425 の `listed/info` がまさにそれだった）。
    """
    low = (body or "").lower()
    if JQUANTS_NO_SUBSCRIPTION_MARK in low:
        return "no_subscription"
    if JQUANTS_PLAN_RESTRICTED_MARK in low:
        return "plan_restricted"
    if JQUANTS_ENDPOINT_MISSING_MARK in low:
        return "endpoint_missing"
    return "unknown"


def parse_jquants_coverage(body: str) -> tuple:
    """400 のボディからカバレッジ窓 (from, to) を ISO 文字列で返す（#462）。

    窓の文言が無ければ (None, None)＝非営業日など通常の空レスポンス。
    """
    m = _JQUANTS_COVERAGE_RE.search(body or "")
    return (m.group(1), m.group(2)) if m else (None, None)


class JQuantsAccessError(Exception):
    """J-Quants が 403 を返した＝そのリクエスト自体が許可されていない（#412 → #461 → #462）。

    `reason` は `classify_jquants_forbidden` の3分類＋"unknown"。**カバレッジ境界はここに
    来ない**（境界は 400・`JQuantsOutOfCoverage`）。旧名 `JQuantsCoverageError` は
    「境界の欠測」を含意していたが、その集合は実測上存在しなかった。

    `no_subscription`: 契約失効のときだけ True（#461）。失効を他の 403 と同じ警告へ潰すと
    平常運転と区別がつかない——実際 2026-08-07 まで、全日 403 のログは「エンバーゴ内の窓なら
    正常」と読める文言のまま毎晩流れていた。
    """

    def __init__(self, date_str: str, reason: str = "unknown", message: str = ""):
        super().__init__(f"{date_str} ({reason})")
        self.date_str = date_str
        self.reason = reason
        self.message = message

    @property
    def no_subscription(self) -> bool:
        return self.reason == "no_subscription"


class JQuantsOutOfCoverage(Exception):
    """400 かつボディにカバレッジ窓の文言があった＝契約窓の外側の日付（#462）。

    非営業日の 400 と**必ず区別する**。同一視すると、無料プランのエンバーゴ（直近約12週）に
    かかる約60営業日が「祝日」と同じログで消え、1日あたり JQUANTS_RATE_SLEEP を払い続けて
    いることに気づけない。`cover_from`/`cover_to` は以降の日付ループを窓でクリップするのに使う。
    """

    def __init__(self, date_str: str, cover_from: Optional[str] = None,
                 cover_to: Optional[str] = None):
        super().__init__(f"{date_str} (covers {cover_from} ~ {cover_to})")
        self.date_str = date_str
        self.cover_from = cover_from
        self.cover_to = cover_to
