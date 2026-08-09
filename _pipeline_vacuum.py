"""
GitHub Actions 用・DB メンテナンスパイプライン（月次自動実行向け）。

`stock_price_daily` は日次収集のたびに古い行を DELETE する
ローリング trim（database.py:record_prices_batch）を行っており、
btree インデックス（pk_stock_price_daily / ix_spd_trade_date）が
bloat し続ける（Issue #290）。autovacuum は死領域をテーブル内で
再利用するだけでファイルサイズは縮まないため、VACUUM FULL で
定期的に物理サイズを頭打ちにする。

VACUUM はトランザクションブロック外でのみ実行可能なため、
AUTOCOMMIT の接続で実行する。対象は stock_price_daily のみ
（bloat の主要因であることは実測で確認済み・Issue #290 コメント参照）。
"""
import time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from database import engine, db_timeouts
import _pipeline_utils

LOG_FILE = "logs/pipeline_vacuum.log"
log = _pipeline_utils.make_logger(LOG_FILE)

TARGET_TABLE = "stock_price_daily"

# ⏱ タイムアウト設計（Issue #471）
# Supabase の postgres ロール既定は statement_timeout=2min / lock_timeout=0（実測）。
# VACUUM FULL 本体は実測 7.6〜10.4秒（43〜92MB）で 2min に遠く及ばないが、
# 2026-08-08 の失敗は **きっかり 2分01秒**で打ち切られた＝待っていたのはロックである。
# lock_timeout=0 のままだと「取れるまで待つ→statement_timeout で殺される」ので、
#   - lock_timeout を有限にしてロック待ちだけを先に諦めさせ（原因がログで確定する）
#   - statement_timeout は外して VACUUM 本体を時間で殺さない
#     （暴走時の歯止めはワークフローの timeout-minutes: 30）
# ロック保持者は一過性（夜間チェーンの残り・autovacuum）なので、間を空けて数回粘る。
LOCK_TIMEOUT      = "90s"
STATEMENT_TIMEOUT = "0"     # 無制限。上限はワークフローの timeout-minutes が持つ
MAX_ATTEMPTS      = 3
RETRY_SLEEP_SEC   = 120


def _table_size(conn) -> str:
    return conn.execute(
        text("SELECT pg_size_pretty(pg_total_relation_size(:t))"), {"t": TARGET_TABLE}
    ).scalar()


def _db_size(conn) -> str:
    return conn.execute(
        text("SELECT pg_size_pretty(pg_database_size(current_database()))")
    ).scalar()


def _is_lock_timeout(exc) -> bool:
    """lock_timeout での打ち切り（Postgres 55P03 lock_not_available）か。"""
    return getattr(getattr(exc, "orig", None), "pgcode", None) == "55P03"


def _log_lock_holders(conn) -> None:
    """対象テーブルのロックを掴んでいるセッションを残す。

    「誰に待たされたか」が run ログに無いと、次に同じ失敗をしたときも
    ロック待ちなのか処理が重いのかを事後に確定できない（#471 の初回がそれ）。
    """
    rows = conn.execute(text("""
        SELECT a.pid, a.state, a.application_name, l.mode,
               COALESCE(now() - a.xact_start, interval '0') AS xact_age,
               left(COALESCE(a.query, ''), 120) AS q
        FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE l.relation = CAST(:t AS regclass) AND l.pid <> pg_backend_pid()
    """), {"t": TARGET_TABLE}).fetchall()
    if not rows:
        log("  ロック保持者は見つからず（取得までの間に解放された可能性）")
        return
    for pid, state, app, mode, age, q in rows:
        log(f"  ロック保持: pid={pid} state={state} app={app} mode={mode} xact_age={age} query={q}")


def main():
    log("=" * 60)
    log("DBメンテナンス（VACUUM FULL）パイプライン 開始")
    log("=" * 60)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        before_table, before_db = _table_size(conn), _db_size(conn)
        log(f"[before] {TARGET_TABLE}: {before_table} / DB全体: {before_db}")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            log(f"VACUUM FULL {TARGET_TABLE} 実行中...（試行 {attempt}/{MAX_ATTEMPTS}・"
                f"lock_timeout={LOCK_TIMEOUT} / statement_timeout={STATEMENT_TIMEOUT}）")
            t_begin = time.time()
            try:
                with db_timeouts(conn, statement=STATEMENT_TIMEOUT, lock=LOCK_TIMEOUT):
                    conn.execute(text(f"VACUUM FULL {TARGET_TABLE}"))
                log(f"  VACUUM 完了（{time.time() - t_begin:.1f}秒）")
                break
            except Exception as e:
                if not _is_lock_timeout(e):
                    raise
                log(f"  ロック取得に失敗（{LOCK_TIMEOUT} 待ちで打ち切り）")
                _log_lock_holders(conn)
                if attempt == MAX_ATTEMPTS:
                    raise
                log(f"  {RETRY_SLEEP_SEC}秒待って再試行する")
                time.sleep(RETRY_SLEEP_SEC)

        after_table, after_db = _table_size(conn), _db_size(conn)
        log(f"[after]  {TARGET_TABLE}: {after_table} / DB全体: {after_db}")

    log("=" * 60)
    log("DBメンテナンス完了")
    log("=" * 60)


if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"DBメンテナンスパイプライン開始: {datetime.now()}\n")
    main()
