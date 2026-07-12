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
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from database import engine
import _pipeline_utils

LOG_FILE = "logs/pipeline_vacuum.log"
log = _pipeline_utils.make_logger(LOG_FILE)

TARGET_TABLE = "stock_price_daily"


def _table_size(conn) -> str:
    return conn.execute(
        text("SELECT pg_size_pretty(pg_total_relation_size(:t))"), {"t": TARGET_TABLE}
    ).scalar()


def _db_size(conn) -> str:
    return conn.execute(
        text("SELECT pg_size_pretty(pg_database_size(current_database()))")
    ).scalar()


def main():
    log("=" * 60)
    log("DBメンテナンス（VACUUM FULL）パイプライン 開始")
    log("=" * 60)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        before_table, before_db = _table_size(conn), _db_size(conn)
        log(f"[before] {TARGET_TABLE}: {before_table} / DB全体: {before_db}")

        log(f"VACUUM FULL {TARGET_TABLE} 実行中...")
        conn.execute(text(f"VACUUM FULL {TARGET_TABLE}"))

        after_table, after_db = _table_size(conn), _db_size(conn)
        log(f"[after]  {TARGET_TABLE}: {after_table} / DB全体: {after_db}")

    log("=" * 60)
    log("DBメンテナンス完了")
    log("=" * 60)


if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"DBメンテナンスパイプライン開始: {datetime.now()}\n")
    main()
