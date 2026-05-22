"""ローカル PostgreSQL と Supabase の件数を比較するレポートスクリプト。

docs/REFACTORING.md §3.2.3 Step 1 用。手元 PC で実行する想定。

使い方:
    .\\venv\\Scripts\\python.exe scripts\\compare_db_counts.py

.env に以下の 2 つを設定しておくこと:
    DATABASE_URL        ... Supabase の接続 URL
    LOCAL_DATABASE_URL  ... ローカル PostgreSQL の接続 URL

Web 版 Claude Code（claude.ai/code のリモートコンテナ）からは
ローカル PG にも Supabase にもネットワーク到達できないため、
このスクリプトは必ず手元 PC で実行すること。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()

TABLES = [
    "companies",
    "financial_records",
    "stock_price_history",
    "collection_logs",
    "macro_data",
]

EXTRA_CHECKS = [
    (
        "financial_records",
        "SELECT MIN(period_end) AS min_pe, MAX(period_end) AS max_pe, "
        "COUNT(DISTINCT edinet_code) AS n_companies FROM financial_records",
    ),
    (
        "stock_price_history",
        "SELECT MIN(trade_date) AS min_d, MAX(trade_date) AS max_d, "
        "COUNT(DISTINCT edinet_code) AS n_companies FROM stock_price_history",
    ),
    (
        "macro_data",
        "SELECT MIN(trade_date) AS min_d, MAX(trade_date) AS max_d, "
        "COUNT(DISTINCT series_code) AS n_series FROM macro_data",
    ),
]


def make_engine(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    is_local = "localhost" in url or "127.0.0.1" in url
    connect_args = {} if is_local else {"sslmode": "require"}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def count_rows(engine: Engine, table: str) -> Optional[int]:
    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            return int(row) if row is not None else 0
    except Exception as e:
        print(f"  [warn] {table}: {e}", file=sys.stderr)
        return None


def run_extra(engine: Engine, sql: str) -> Optional[dict]:
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql)).mappings().first()
            return dict(row) if row else None
    except Exception as e:
        print(f"  [warn] extra check failed: {e}", file=sys.stderr)
        return None


def fmt(n: Optional[int]) -> str:
    if n is None:
        return "    error"
    return f"{n:>10,}"


def decide(local: Optional[int], remote: Optional[int]) -> str:
    if local is None or remote is None:
        return "?"
    if local == remote:
        return "= 一致"
    if local > remote:
        return "→ ローカル優位"
    return "← Supabase 優位"


def main() -> int:
    local_url = os.environ.get("LOCAL_DATABASE_URL")
    remote_url = os.environ.get("DATABASE_URL")

    if not local_url:
        print("ERROR: LOCAL_DATABASE_URL が未設定です（.env を確認）", file=sys.stderr)
        return 1
    if not remote_url:
        print("ERROR: DATABASE_URL が未設定です（.env を確認）", file=sys.stderr)
        return 1
    if local_url == remote_url:
        print("ERROR: LOCAL_DATABASE_URL と DATABASE_URL が同一です", file=sys.stderr)
        return 1

    print("=" * 72)
    print("ローカル PostgreSQL と Supabase の件数比較レポート")
    print("=" * 72)
    print(f"ローカル: {_redact(local_url)}")
    print(f"Supabase: {_redact(remote_url)}")
    print()

    local_engine = make_engine(local_url)
    remote_engine = make_engine(remote_url)

    print(f"{'テーブル':<22} {'ローカル':>12} {'Supabase':>12}  判定")
    print("-" * 72)

    counts: dict[str, tuple[Optional[int], Optional[int]]] = {}
    for tbl in TABLES:
        local_n = count_rows(local_engine, tbl)
        remote_n = count_rows(remote_engine, tbl)
        counts[tbl] = (local_n, remote_n)
        print(f"{tbl:<22} {fmt(local_n)} {fmt(remote_n)}  {decide(local_n, remote_n)}")

    print()
    print("追加サニティチェック")
    print("-" * 72)
    for tbl, sql in EXTRA_CHECKS:
        print(f"[{tbl}]")
        for label, engine in [("local   ", local_engine), ("supabase", remote_engine)]:
            r = run_extra(engine, sql)
            if r is None:
                print(f"  {label}: (取得失敗)")
            else:
                print(f"  {label}: " + "  ".join(f"{k}={v}" for k, v in r.items()))
        print()

    print("=" * 72)
    print("方針判断の目安:")
    print("  ・ローカル優位の項目が多い → 戦略 A (全置換) を推奨")
    print("  ・Supabase 優位がある     → 戦略 B (upsert) を検討、要事前退避")
    print("  ・差分が僅か              → どちらでも安全")
    print()
    print("次のステップ:")
    print("  python scripts/migrate_local_to_supabase.py --dry-run")
    print("=" * 72)
    return 0


def _redact(url: str) -> str:
    """接続 URL のパスワード部分を伏字にする"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{host}"
    return url


if __name__ == "__main__":
    sys.exit(main())
