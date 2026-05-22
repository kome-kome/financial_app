"""ローカル PostgreSQL → Supabase へのワンショット移行スクリプト。

docs/REFACTORING.md §3.2.3 Step 2〜4 用。手元 PC で実行する想定。

使い方:
    # 事前確認（DB は触らない）
    .\\venv\\Scripts\\python.exe scripts\\migrate_local_to_supabase.py --dry-run

    # 全置換（推奨。Supabase の既存データを上書き）
    .\\venv\\Scripts\\python.exe scripts\\migrate_local_to_supabase.py --strategy=replace

    # upsert マージ（既存重複は更新、新規のみ insert）
    .\\venv\\Scripts\\python.exe scripts\\migrate_local_to_supabase.py --strategy=upsert

前提:
    - .env に DATABASE_URL（Supabase）と LOCAL_DATABASE_URL（ローカル）を併記
    - pg_dump / psql コマンドが PATH 上にあること（PostgreSQL クライアントツール）
    - 実行前に必ず scripts/compare_db_counts.py でデータ差分を確認すること

Web 版 Claude Code（claude.ai/code）からは到達不可。
必ず手元 PC のターミナルまたは VS Code 版 Claude Code から実行する。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

TABLES = [
    "companies",
    "financial_records",
    "stock_price_history",
    "collection_logs",
    "macro_data",
]

DUMP_DIR = Path("migration_dumps")


def need_cli(name: str) -> str:
    p = shutil.which(name)
    if not p:
        sys.exit(
            f"ERROR: '{name}' コマンドが見つかりません。"
            "PostgreSQL クライアントツールをインストールしてください。"
        )
    return p


def redact(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{host}"
    return url


def confirm(msg: str) -> bool:
    ans = input(f"{msg} [yes/no]: ").strip().lower()
    return ans in ("yes", "y")


def dump_local(local_url: str, out_path: Path) -> None:
    pg_dump = need_cli("pg_dump")
    args = [pg_dump, "--no-owner", "--no-acl", "--data-only", "--column-inserts"]
    for tbl in TABLES:
        args.extend(["--table", tbl])
    args.extend(["--file", str(out_path), local_url])
    print(f"  → pg_dump 実行: {out_path}")
    subprocess.run(args, check=True)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  → ダンプ完了: {size_mb:.1f} MB")


def truncate_supabase(remote_url: str) -> None:
    psql = need_cli("psql")
    sql = (
        "TRUNCATE "
        + ", ".join(TABLES)
        + " RESTART IDENTITY CASCADE;"
    )
    print("  → Supabase の対象テーブルを TRUNCATE")
    subprocess.run([psql, remote_url, "-c", sql], check=True)


def restore_to_supabase(remote_url: str, dump_path: Path) -> None:
    psql = need_cli("psql")
    print(f"  → Supabase へ restore: {dump_path}")
    # ON_ERROR_STOP=1: 途中エラーで停止（部分投入を防ぐ）
    subprocess.run(
        [psql, remote_url, "-v", "ON_ERROR_STOP=1", "-f", str(dump_path)],
        check=True,
    )


def upsert_dump_to_temp(remote_url: str, dump_path: Path) -> None:
    """upsert 戦略: dump を一時 schema に投入してから INSERT ... ON CONFLICT を使う。

    現状は未実装（complex）。戦略 A (replace) を推奨するため、
    必要になったタイミングで段階的に実装する。
    """
    raise NotImplementedError(
        "upsert 戦略は未実装です。--strategy=replace を使うか、"
        "手動で psql から COPY + INSERT ... ON CONFLICT を実行してください。"
    )


def run_count(url: str) -> dict[str, int]:
    """各テーブルの件数を辞書で返す（軽量サニティ）"""
    from sqlalchemy import create_engine, text

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    is_local = "localhost" in url or "127.0.0.1" in url
    connect_args = {} if is_local else {"sslmode": "require"}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for tbl in TABLES:
            try:
                out[tbl] = int(conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar() or 0)
            except Exception:
                out[tbl] = -1
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ローカル PostgreSQL → Supabase 移行スクリプト"
    )
    p.add_argument(
        "--strategy",
        choices=["replace", "upsert"],
        default="replace",
        help="マージ戦略（既定: replace = Supabase 全置換）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="実行内容を表示するだけで DB は触らない",
    )
    p.add_argument(
        "--keep-dump",
        action="store_true",
        help="restore 成功後もダンプファイルを残す（既定では残す）",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="確認プロンプトをスキップ（自動化用、慎重に）",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    local_url = os.environ.get("LOCAL_DATABASE_URL")
    remote_url = os.environ.get("DATABASE_URL")

    if not local_url or not remote_url:
        print("ERROR: .env に LOCAL_DATABASE_URL と DATABASE_URL の両方が必要です",
              file=sys.stderr)
        return 1
    if local_url == remote_url:
        print("ERROR: 2 つの URL が同一です（移行先と移行元が同じ）",
              file=sys.stderr)
        return 1

    print("=" * 72)
    print(f"ローカル PG → Supabase 移行  (strategy={args.strategy}"
          f"{' / DRY-RUN' if args.dry_run else ''})")
    print("=" * 72)
    print(f"  source: {redact(local_url)}")
    print(f"  target: {redact(remote_url)}")
    print()

    # Step 1: 件数事前確認
    print("[1/4] 件数事前確認")
    before_local = run_count(local_url)
    before_remote = run_count(remote_url)
    print(f"  {'table':<22} {'local':>10} {'supabase':>12}")
    for tbl in TABLES:
        print(f"  {tbl:<22} {before_local.get(tbl, -1):>10,} "
              f"{before_remote.get(tbl, -1):>12,}")
    print()

    if args.dry_run:
        print("[dry-run] ここで停止します。実投入は --dry-run を外して再実行してください。")
        return 0

    # 確認プロンプト
    if not args.yes:
        warn = ("⚠️  戦略 'replace' は Supabase の上記テーブルを TRUNCATE してから "
                "ローカルのデータで全置換します。") if args.strategy == "replace" else \
               ("戦略 'upsert' で進めます。")
        print(warn)
        if not confirm("続行しますか？"):
            print("中断しました。")
            return 0

    # Step 2: ダンプ
    print("\n[2/4] ローカル DB をダンプ")
    DUMP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_path = DUMP_DIR / f"local_dump_{ts}.sql"
    try:
        dump_local(local_url, dump_path)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: pg_dump に失敗しました: {e}", file=sys.stderr)
        return 1

    # Step 3: Supabase へ投入
    print(f"\n[3/4] Supabase へ投入 (戦略: {args.strategy})")
    try:
        if args.strategy == "replace":
            truncate_supabase(remote_url)
            restore_to_supabase(remote_url, dump_path)
        else:
            upsert_dump_to_temp(remote_url, dump_path)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: 投入に失敗しました: {e}", file=sys.stderr)
        print(f"  ダンプファイルは保持されています: {dump_path}", file=sys.stderr)
        return 1
    except NotImplementedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Step 4: 件数照合
    print("\n[4/4] 移行後の件数照合")
    after_remote = run_count(remote_url)
    print(f"  {'table':<22} {'local (元)':>12} {'supabase (新)':>15}  判定")
    all_ok = True
    for tbl in TABLES:
        loc = before_local.get(tbl, -1)
        rem = after_remote.get(tbl, -1)
        ok = (loc == rem)
        if not ok:
            all_ok = False
        mark = "✓" if ok else "✗ 不一致"
        print(f"  {tbl:<22} {loc:>12,} {rem:>15,}  {mark}")

    print()
    if all_ok:
        print("✅ 移行成功（全テーブルで件数一致）")
        print()
        print("次のステップ:")
        print("  1. アプリ起動 → /api/regression で Zスコア・成長率を再計算")
        print("  2. Render の /health を叩いて本番動作確認")
        print("  3. .env の DATABASE_URL を Supabase に切替（LOCAL_DATABASE_URL は残す）")
        return 0
    else:
        print("⚠️  件数不一致あり。手動でログ・データを確認してください。")
        print(f"     ダンプファイル: {dump_path}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
