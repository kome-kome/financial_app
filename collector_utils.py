"""collector パッケージ共通の設定定数・ロガー。

収集系モジュール（collector_prices / collector_financials / collector_master）が
共有する設定値とロガーを集約する。ドメイン固有の定数は各モジュール側に置く。
"""
import os
import logging
import re
from datetime import time as dtime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx は "HTTP Request: GET <url> ..." を INFO で出力し、クエリパラメータに載る
# APIキー（FRED_API_KEY/ESTAT_API_KEY 等）がそのままログへ残ってしまうため抑制する。
for _noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)
log = logging.getLogger("collector")

# --- 例外・ログからの機微値の除去（#577）--------------------------------------
# 値に機微が入りうる環境変数名の手掛かり。`scripts/batch_common._SECRET_HINTS` と**同じ語彙**だが
# 機構は別物で、あちらは変数名を出すときに**名前**で伏せ、こちらは任意の文字列から**値**を消す。
# 一致は `tests/test_collector.py::TestRedactSecrets` が CI で照合する（写しが黙って割れるのを防ぐ）。
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CRED")


def redact_secrets(text: str) -> str:
    """環境変数の機微な**値**を伏せる（`database.mask_url` と同じ役どころ）。

    上の httpx ロガー抑制は「クエリ付き URL がログへ出ない」ことを狙ったものだが、
    **例外メッセージは素通りする**。httpx の `HTTPStatusError` は `for url '...'` として
    クエリごと URL を持つため、`log.warning(f"...{e}")` の1行で
    `Subscription-Key` が平文で `.logs/` へ落ちる。実際 2026-08-29〜30 の夜間ログ2本に
    EDINET のキーが 3,568 箇所残った（`.logs` は Issue へ貼られうる・リポジトリは public）。

    短い値は誤爆する（`PASS=1` で "1" が全部消える）ので 8 文字未満は対象外にする。
    """
    if not text:
        return text
    for name, value in os.environ.items():
        if len(value) < 8 or not any(h in name.upper() for h in SECRET_HINTS):
            continue
        text = text.replace(value, f"<{name}:redacted>")
    return text

# EDINET API のホスト。2026-08-29 に `disclosure.edinet-fsa.go.jp` が **301** を返すように
# なり、全収集経路が同日から無言で 0 件になった（#577）。
# **`follow_redirects=True` では直らない**——リダイレクト先の `disclosure2.edinet-fsa.go.jp` は
# API ではなく**人間用画面**へ 302 で送る（`wzek0130.aspx`）。追従すると HTML を JSON として
# パースして「その日は提出ゼロ」に化けるので、いまより悪い失敗の仕方になる。
# 移設先は `api.edinet-fsa.go.jp`（2026-08-30 実測: documents.json 200・documents/{id} 200）。
EDINET_BASE   = "https://api.edinet-fsa.go.jp/api/v2"
JPX_EXCEL_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
API_KEY       = os.environ.get("EDINET_API_KEY", "")
RATE_SLEEP             = 0.6   # EDINET API のリクエスト間隔（秒）
# 連続でこの回数だけ書類一覧の取得に失敗したら以降の日付を叩かない（#577）。
# `JQUANTS_MAX_CONSECUTIVE_FORBIDDEN` と**同じ理屈**で置いた値＝連続失敗は構造的（ホスト移設・
# キー失効・ネットワーク断）で、日付を変えても直らない。実測から逆算した数字ではない。
# **単発の失敗は従来どおり握って続行する**（EDINET は個別日でしばしば失敗し、差分収集は翌日
# 同じ日付を再スキャンするので取りこぼしにならない）。この寛容さは意図的なので壊さない。
# 効果: 2026-08-29 は 892日ぶんを 0.6秒間隔で叩き切って約9分を捨てたうえ exit=0 で通った。
EDINET_MAX_CONSECUTIVE_FAILURES = 10
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

# --- Yahoo ティッカーのサフィックス（#555）------------------------------------
# 生成箇所を1つに寄せるための定数群。かつては collector_prices.py の4箇所が
# それぞれ `f"{sec_code}.T"` を直書きしており、**東証以外の単独上場は原理的に
# 取得できない**という欠測が「例外を出さずに落ちる」形で常設化していた
# （札証/福証の単独上場38社が全モデルの母集団から静かに脱落・#555）。
YAHOO_SUFFIX_TSE = ".T"

# サフィックス → Yahoo `meta.exchangeName` の期待値（2026-08-27 実測）。
# **採用ガードの正本**。`.F` は Frankfurt と名前空間が衝突し、`377A.F` / `6461.F` は
# HTTP200 で61バーを返すが `exchangeName=FRA`＝同記号の欧州銘柄（454社中2社が誤爆）。
# 「取れた」ように見えるので件数だけ見ていると**別会社の株価を書き込む**。
# `.T`（東証）は #555 で exchangeName を実測していないので**ここには載せない**
# （未実測の定数を置かない＝db_egress.EgressCost が measured_on を必須にするのと同じ作法）。
# 名証（`.NG`）は HTTP200・timestamp 0行・`exchangeName=YHD` で取得手段が無く、
# 英数字コード8社は Yahoo 未収録。いずれも #555 のスコープ外（docs/GOTCHAS.md に記録）。
YAHOO_LOCAL_EXCHANGES = {".S": "SAP", ".F": "FKA"}

YAHOO_EXPECT_CURRENCY = "JPY"   # 誤爆は通貨でも弾ける（FRA 銘柄は EUR 建て）


def yahoo_ticker(sec_code: str, suffix: Optional[str] = None) -> str:
    """Yahoo Finance のティッカー文字列。suffix が None/空なら東証（.T）。

    `suffix` は `companies.yahoo_suffix` の値をそのまま渡す想定で、**NULL は
    「未解決＝.T で試す」の1つの意味しか持たない**（3状態目を作らない）。
    DB は引かない純関数——gap-fill の内側ループは4,000社を逐次回るため、
    ここで1クエリでも投げると4,000回の往復になる。
    """
    return f"{sec_code}{suffix or YAHOO_SUFFIX_TSE}"


def yahoo_expect_exchanges(suffix: Optional[str]) -> Optional[frozenset]:
    """そのサフィックスで受け入れてよい `meta.exchangeName` の集合。

    `.T`（＝未解決を含む）は None＝**検証しない**。従来どおりの挙動を保つための
    既定であり、地方取引所として解決済みの社にだけ突合を課す。
    """
    ex = YAHOO_LOCAL_EXCHANGES.get(suffix or YAHOO_SUFFIX_TSE)
    return frozenset({ex}) if ex else None


def yahoo_guard_kwargs(suffix: Optional[str]) -> dict:
    """`fetch_yahoo_history` へ渡す誤爆ガードの kwargs。

    **`.T`（未解決を含む）では空 dict を返す**——キーワードを1つも渡さないので、
    東証経路の呼び出しはシグネチャの見た目まで従来と同一になる。これは審査上の
    都合ではなく、「検証は解決済みの社にだけ課す」という意図を呼び出し側の形で
    表しておくためのもの（新しい引数が既定で全社に効き始める事故を構造的に防ぐ）。
    """
    ex = yahoo_expect_exchanges(suffix)
    if ex is None:
        return {}
    return {"expect_exchanges": ex, "expect_currency": YAHOO_EXPECT_CURRENCY}

# --- 日本時間の基準（#474 / #476）----------------------------------------------
# GitHub Actions のランナーは UTC。日本市場・EDINET の「日付」は JST なので、
# 収集の日付境界は必ずこの tz で判定する（`date.today()` を直接使わない）。
JST = timezone(timedelta(hours=9))

# 判定を「ランナーの UTC 日付」から「閉場済みの最新 JST 営業日」へ移すための定数。
# 旧判定 `(today_utc - last_d).days <= 0` は、**その日のセッションがまだ無い時間帯・
# 非営業日には全社が対象**になる。JST 日曜 03:47 起動の run 31272807314 は、全社が
# 既に持つ金曜バーを 4,437社ぶん取り直して 2時間11分を使った。
# **大引けは 15:30**（2024-11-05 のクロージング・オークション導入で 15:00 から延伸）。
# ここに Yahoo が終値を確定させるまでの余裕を足す。定時実行が JST 17:17 に移り
# （#476）、この境界は「当日ぶんを取りに行くか」を実際に左右するようになった。
MARKET_CLOSE_JST         = dtime(16, 0)
# EDINET の提出受付終了（平日 9:00〜17:15）。これを過ぎた JST 日付は書類一覧が確定
# しているとみなしてスキャン範囲へ含める（#476）。締切間際の提出が一覧へ載るまでの
# ラグで取りこぼしても、差分収集が翌日同じ日付を再スキャンするので欠落にはならない。
EDINET_CUTOFF_JST        = dtime(17, 15)
# 取得する社は起点をこの日数ぶん手前へ倒し、直近セッションを取り直す。
# `record_prices_batch` は ON CONFLICT DO UPDATE なので、場中実行が書いた暫定終値が
# あっても確定値で上書きされる。**Yahoo は1社1リクエストのため追加コストはゼロ**
# （窓が広がるだけ・`fill_recent_stock_price_gap_yahoo` の docstring と同じ理屈）。
PRICE_REFRESH_TAIL_DAYS  = 3
# 基準セッションがこれ以上古く出たら判定を信用せず全社取得へ倒す。判定側の異常が
# 「誰も取りに行かない」（＝#415 の静かな鮮度死）へ倒れるのを防ぐ安全弁。
SESSION_SANITY_DAYS      = 5
# 上場廃止済み（`is_active=False`）で株価を1件も持たない社の再試行間隔（日・Issue #475）。
# 毎晩の gap-fill は「価格が無い社」を保持窓の先頭から取りに行くが、2026-08-21 の実測では
# 該当 454社が**全て `is_active=False`**＝Yahoo も返さない。毎晩 454リクエスト ≒ 13分を
# 捨てていることになる。**恒久除外にはしない**——`/equities/master` のエンバーゴで新規上場が
# 誤って delisted 判定された前例があり（#463）、除外すると復活も新規も永久に拾えなくなる。
# 曜日は edinet_code から決定的に散らす（同じ日に 454社が集中しないように）。
DELISTED_RETRY_INTERVAL_DAYS = 7
                               # 実測：データ日は約8s、非営業日は約3s で応答。
                               # 無料プランの上限が約5リクエスト/60秒のため20s を確保して安全マージンを持たせる。
                               # データ日はダウンロードに~8秒かかるため追加待機ほぼゼロ。
                               # 祝日（即時400）の後は残り~7秒を補完スリープ。

# --- バッチ処理間隔（コミット/スリープ/進捗）。N 件ごとに処理を区切る。値は現状維持。---
PRICE_COMMIT_BATCH            = 200  # 株価レコード更新のコミット間隔
# financial_records の株価・バリュエーション列を一括更新する単位（#464）。
# 1件1 UPDATE だと往復レイテンシに比例し、GHA↔Supabase では 42,289件で143分かかっていた。
# 大きくするほど往復は減るが、1文の失敗で巻き戻る範囲も広がるためこの程度に留める。
BULK_UPDATE_CHUNK             = 2000

# --- 重い DB 文のタイムアウトと再試行（#470）------------------------------------
# Supabase の postgres ロール既定 statement_timeout=2min は、GHA↔Supabase から走る
# 株価 upsert（daily+weekly 再集約+trim の3文）と financial_records の一括 UPDATE には
# 足りない。2026-08-08 の夜間差分収集は catchup の upsert とこの一括 UPDATE の両方で
# 2min を踏み、後者で非ゼロ終了して nightly-scores チェーンごと止めた。
# `db_timeouts`（database.py）で**その文の実行中だけ**引き上げる。
HEAVY_STATEMENT_TIMEOUT       = "10min"
# 保存の再試行。同日の run では Supabase pooler の枯渇
# （FATAL ECHECKOUTTIMEOUT: unable to check out connection from the pool）も2回起きており、
# どちらも1バッチぶんの株価が**警告だけ出して静かに落ちていた**。プール枯渇は一過性なので、
# 間を空けて数回粘れば埋まる。
PRICE_BATCH_MAX_ATTEMPTS      = 3
PRICE_BATCH_RETRY_SLEEP       = 20   # 秒（試行ごとに倍化）
MASTER_COMMIT_BATCH          = 200  # 企業マスタ保存のコミット間隔
REPARSE_COMMIT_BATCH         = 100  # XBRL 再解析・CF 補完のコミット間隔
# XBRL 再解析で一度にメモリへ載せる gzip BLOB の件数（#507）。
# ピークメモリを書類数から切り離すための単位で、コミット間隔もこの境界に揃える
# （commit → expunge_all をチャンクの区切りで行う）。
REPARSE_FETCH_BATCH          = 100
MARKET_COMMIT_BATCH          = 50   # 市場データ（株価）更新のコミット間隔
COLLECT_COMMIT_BATCH         = 50   # 全件収集（財務）のコミット間隔
COLLECT_SLEEP_BATCH          = 100  # 全件収集で BATCH_PAUSE を挟む間隔
PROGRESS_LOG_BATCH           = 100  # 進捗ログ出力の間隔
PROGRESS_REPORT_BATCH        = 500  # 進捗コールバック報告の間隔
YAHOO_BACKFILL_PROGRESS_BATCH = 200 # Yahoo backfill の進捗報告間隔

# --- 週次株価の段差（分割の遡及調整もれ）検出・修復（#465）---
# 乖離は「ほぼ0」と「1%以上」に二分し、0.1〜1% の帯は実測でほぼ空。したがって閾値は
# 1% 前後ならどこに置いても同じ集合を拾う（＝「検知したい値」から逆算した数字ではない）。
PRICE_BREAK_THRESHOLD    = 0.01
PRICE_BREAK_PROBE_MONTHS = 24    # 契約窓内で突合する月数（月あたり1営業日 × JQUANTS_RATE_SLEEP）
# 検出がこれを超えたら書かずに中止する安全弁。想定は全体の1〜2%（40〜70社）で、大きく
# 超えるのは「段差が広がった」ではなく突合側の前提（コード対応・窓・API仕様）が壊れた
# 疑いが濃い。その状態で全銘柄を上書きするほうが危ない。
PRICE_BREAK_MAX_REPAIR   = 300

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


def is_common_stock_code(code: str) -> bool:
    """J-Quants の5桁 `Code` が普通株か（#465）。

    5桁コードは「証券コード4桁＋種類1桁」で、末尾 `0` が普通株、それ以外は優先株・
    優先出資証券などの別クラス。収集側は `code[:4]` で `sec_code` に落とすため、
    同一企業に複数クラスがあると 1 つの `edinet_code` に複数行が対応する。
    **到着順の先着勝ちで潰してはいけない**——レスポンス順が変われば別クラスの終値が
    普通株の枠に入る（実測で先頭4桁が重複するのは 9434 / 5076 / 2593）。
    """
    s = str(code)
    return len(s) == 5 and s.endswith("0")


class EdinetAccessError(Exception):
    """EDINET が構造的に応答しない＝日付を変えても直らない状態（#577）。

    **「その日の提出がゼロ」と「取得に失敗した」を型で分ける**ために要る。旧実装は失敗を
    `except Exception` で握って `[]` を返しており、呼び出し側から両者を区別する手段が無かった。
    その結果 2026-08-29 のホスト移設（301）では 892/892 の失敗が「提出ゼロの日が892日続いた」に
    化け、`exit=0` ／ `OK pipeline` ／ watchdog `[鮮度] OK` のまま2晩気づかれなかった。

    「走らなかったこと」の検知（#515・ADR-0042）はこれを拾わない——**走ってはいる**からである。
    """

    def __init__(self, reason: str, consecutive: int = 0):
        super().__init__(f"{reason}（連続 {consecutive} 回）" if consecutive else reason)
        self.reason = reason
        self.consecutive = consecutive


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
