"""
DB メンテナンスパイプライン。**定常の起動元はローカル月次バッチ**
（`scripts/run_monthly.py` の先頭ステップ `vacuum`・毎月1日 JST 01:00・#504/ADR-0040）。

GHA の `vacuum-maintenance.yml`（週次）は **2026-08-25 に schedule 停止**した（#290 / #505・
ADR-0038 の追補）。正本がローカルへ移り Supabase 断面は 2026-08-07 で凍結された＝書き込みが
無いので bloat も増えず、毎週 `VACUUM FULL` を打っても初回以降は何も回収しない。yml は断面を
手で保守するときの口としてだけ残っている。**このファイルの中身は起動元に依存しない**
（接続先を決めるのは `FINAPP_DB_TARGET`・既定 local）。

`stock_price_daily` は日次収集のたびに古い行を DELETE する
ローリング trim（database.py:record_prices_batch）を行っており、
btree インデックス（pk_stock_price_daily / ix_spd_trade_date）が
bloat し続ける（Issue #290）。autovacuum は死領域をテーブル内で
再利用するだけでファイルサイズは縮まないため、VACUUM FULL で
定期的に物理サイズを頭打ちにする。

VACUUM はトランザクションブロック外でのみ実行可能なため、
AUTOCOMMIT の接続で実行する。

## 対象を2表へ広げた理由（2026-08-19・#290 再オープン）

Database Size が 430MB / 500MB (86%) に達した時点で内訳を測ると、最大の消費者は
`stock_price_weekly`（195MB）で、うち **dead tuple が 200,498 行**溜まっていた
（autovacuum の最終実行は 2026-07-31）。

**これは autovacuum の故障ではない。** per-table 設定が無く（`reloptions = null`）、
クラスタ既定の `autovacuum_vacuum_scale_factor = 0.2` が効くため、発火閾値は

    50 + 0.2 * 1,284,465 = 256,943 行

現在の 200,498 行は**その 78%** で、まだ一度も届いていない。
**128万行の表に既定のスケール係数（20%）が粗すぎる**というだけである。
止まって見えるものが壊れているとは限らない——先に発火閾値を計算してから直す。

そこで2つを同時に行う:

  1. `_tune_autovacuum()` — per-table の scale_factor を 0.02 へ下げ、以後 dead が 2%
     溜まった時点で通常 VACUUM が回るようにする（**これから溜まるのを止める**）
  2. `VACUUM FULL` の対象を `stock_price_weekly` へも広げる（**既に溜まった分を回収する**）

1 だけでは既存の 200,498 行は物理サイズを返さない（通常 VACUUM は死領域をテーブル内で
再利用するだけ）。2 だけでは次の回までにまた溜まる。**両方要る。**

> per-table 設定を `init_db()` / `_ensure_tables()` へ入れてはいけない。api.py の lifespan が
> 無条件に実行するため、ローカル API を起動しただけで本番へ不可逆に反映される
> （`raw_xbrl_json` 削除の前例）。定期バッチで走りログに残るこのファイルが正しい置き場所。
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

# VACUUM FULL の対象。**先頭から順に処理する**（1表目で落ちたら2表目は走らない）。
# bloat の大きい順ではなく「所要の短い順」に並べてある＝Disk IO Budget を食い潰す前に
# 軽い方を確実に終わらせる（NANO インスタンスの IO バーストは有限・2026-08-19）。
TARGET_TABLES = ("stock_price_daily", "stock_price_weekly")

# per-table autovacuum（#290・2026-08-19）。クラスタ既定 0.2 は 128万行の表には粗すぎ、
# 発火閾値 256,943 行に一度も届かないまま bloat し続けていた。0.02 で 25,739 行相当。
# analyze 側も併せて下げる（プランナ統計だけ古いまま残るのを避ける）。
AUTOVACUUM_SCALE_FACTOR = 0.02
AUTOVACUUM_ANALYZE_SCALE_FACTOR = 0.02

# ⏱ タイムアウト設計（Issue #471）
# Supabase の postgres ロール既定は statement_timeout=2min / lock_timeout=0（実測）。
# VACUUM FULL 本体は実測 7.6〜10.4秒（43〜92MB）で 2min に遠く及ばないが、
# 2026-08-08 の失敗は **きっかり 2分01秒**で打ち切られた＝待っていたのはロックである。
# lock_timeout=0 のままだと「取れるまで待つ→statement_timeout で殺される」ので、
#   - lock_timeout を有限にしてロック待ちだけを先に諦めさせ（原因がログで確定する）
#   - statement_timeout は外して VACUUM 本体を時間で殺さない
#     （暴走時の歯止めはワークフローの timeout-minutes）
# ロック保持者は一過性（夜間チェーンの残り・autovacuum）なので、間を空けて数回粘る。
LOCK_TIMEOUT      = "90s"
STATEMENT_TIMEOUT = "0"     # 無制限。上限はワークフローの timeout-minutes が持つ
MAX_ATTEMPTS      = 3
RETRY_SLEEP_SEC   = 120


def _table_size(conn, table: str) -> str:
    return conn.execute(
        text("SELECT pg_size_pretty(pg_total_relation_size(:t))"), {"t": table}
    ).scalar()


def _db_size(conn) -> str:
    return conn.execute(
        text("SELECT pg_size_pretty(pg_database_size(current_database()))")
    ).scalar()


def _is_lock_timeout(exc) -> bool:
    """lock_timeout での打ち切り（Postgres 55P03 lock_not_available）か。"""
    return getattr(getattr(exc, "orig", None), "pgcode", None) == "55P03"


def _log_lock_holders(conn, table: str) -> None:
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
    """), {"t": table}).fetchall()
    if not rows:
        log("  ロック保持者は見つからず（取得までの間に解放された可能性）")
        return
    for pid, state, app, mode, age, q in rows:
        log(f"  ロック保持: pid={pid} state={state} app={app} mode={mode} xact_age={age} query={q}")


# ── autovacuum の per-table 設定（#290）────────────────────────────────────────

def _reloptions(conn, table: str) -> list[str]:
    """pg_class.reloptions を list[str] で返す（未設定なら空リスト）。"""
    raw = conn.execute(
        text("SELECT reloptions FROM pg_class WHERE relname = :t"), {"t": table}
    ).scalar()
    return list(raw) if raw else []


def _wanted_reloptions() -> set[str]:
    return {
        f"autovacuum_vacuum_scale_factor={AUTOVACUUM_SCALE_FACTOR}",
        f"autovacuum_analyze_scale_factor={AUTOVACUUM_ANALYZE_SCALE_FACTOR}",
    }


def _tune_autovacuum(conn) -> None:
    """per-table の autovacuum 発火閾値を下げる（冪等）。

    **VACUUM FULL より先に実行する。** ここが効き始めれば次回以降の VACUUM FULL は
    軽くなる（＝Disk IO を毎週食う構造そのものを畳みにいく）。

    既に望みの値なら ALTER を投げない。ALTER TABLE ... SET は同じ値でも成功するが、
    ログに毎回「変更した」と出ると、次に見た人が「どこかで設定が戻されている」と
    誤読する。**冪等な操作ほど、何もしなかったことをログに残す。**
    """
    want = _wanted_reloptions()
    for table in TARGET_TABLES:
        current = _reloptions(conn, table)
        if want.issubset(set(current)):
            log(f"[autovacuum] {table}: 設定済み（{', '.join(current)}）")
            continue
        log(f"[autovacuum] {table}: 現在 {current or '未設定（クラスタ既定 0.2）'} "
            f"-> scale_factor={AUTOVACUUM_SCALE_FACTOR}")
        # ALTER TABLE ... SET はカタログ更新のみだが ACCESS EXCLUSIVE を一瞬取る。
        # 無制限待ちにすると VACUUM 本体の前で詰まるので lock_timeout を掛ける。
        with db_timeouts(conn, lock=LOCK_TIMEOUT):
            conn.execute(text(
                f"ALTER TABLE {table} SET ("
                f"autovacuum_vacuum_scale_factor = {AUTOVACUUM_SCALE_FACTOR}, "
                f"autovacuum_analyze_scale_factor = {AUTOVACUUM_ANALYZE_SCALE_FACTOR})"
            ))
        log(f"[autovacuum] {table}: 適用完了 -> {_reloptions(conn, table)}")


# ── VACUUM FULL ───────────────────────────────────────────────────────────────

def _vacuum_full(conn, table: str) -> None:
    """1表を VACUUM FULL する。ロック待ちだけ再試行し、それ以外は即送出する。"""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"VACUUM FULL {table} 実行中...（試行 {attempt}/{MAX_ATTEMPTS}・"
            f"lock_timeout={LOCK_TIMEOUT} / statement_timeout={STATEMENT_TIMEOUT}）")
        t_begin = time.time()
        try:
            with db_timeouts(conn, statement=STATEMENT_TIMEOUT, lock=LOCK_TIMEOUT):
                conn.execute(text(f"VACUUM FULL {table}"))
            log(f"  VACUUM 完了（{time.time() - t_begin:.1f}秒）")
            return
        except Exception as e:
            if not _is_lock_timeout(e):
                raise
            log(f"  ロック取得に失敗（{LOCK_TIMEOUT} 待ちで打ち切り）")
            _log_lock_holders(conn, table)
            if attempt == MAX_ATTEMPTS:
                raise
            log(f"  {RETRY_SLEEP_SEC}秒待って再試行する")
            time.sleep(RETRY_SLEEP_SEC)


def main():
    log("=" * 60)
    log("DBメンテナンス（VACUUM FULL）パイプライン 開始")
    log("=" * 60)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        _tune_autovacuum(conn)

        for table in TARGET_TABLES:
            log(f"[before] {table}: {_table_size(conn, table)}")
        log(f"[before] DB全体: {_db_size(conn)}")

        for table in TARGET_TABLES:
            _vacuum_full(conn, table)

        for table in TARGET_TABLES:
            log(f"[after]  {table}: {_table_size(conn, table)}")
        log(f"[after]  DB全体: {_db_size(conn)}")

    log("=" * 60)
    log("DBメンテナンス完了")
    log("=" * 60)


if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"DBメンテナンスパイプライン開始: {datetime.now()}\n")
    main()
