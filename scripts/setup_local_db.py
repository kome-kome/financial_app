"""ローカル PostgreSQL を本アプリのスキーマで初期化する（Issue #481 B-0）。

## なぜ必要か

Supabase が Egress 超過で restricted になると**アプリも分析も一切動かせない**（2026-07・2026-08 に
2回発生し、2回目は8日間まるごと停止した）。ローカルに読取レプリカを置けば、障害時の継続と
検証反復の Egress ゼロ化が同時に片付く。本スクリプトはその第一歩＝**器を作る**ところまでを担う。

データの取り込み（Supabase からの pull）は別スクリプト（#481 B-2）で、本スクリプトは
**Supabase へ一切接続しない**。

## 3つの安全装置

1. **接続先ガード**: 解決した `DATABASE_URL` がローカルでなければ即 `SystemExit`。
   `init_db()` は起動のたび無条件に DDL を打ち、`_DEBUG_ONLY_COLS` / `_LEGACY_COMPUTED_COLS` の
   `DROP COLUMN` 移行を含む——本番へ誤射すると不可逆。`database._is_local` をガードとして
   使う初の箇所。
2. **既定はドライラン**。実際の DDL は `--apply` を明示したときだけ（`--persist` と同じ作法）。
3. **旧スキーマの掃除は1回だけ**。「素の `stock_price_history` が在る かつ `stock_price_weekly`
   が無い」を旧世代のマーカーにする。2回目以降はマーカーが消えているので掃除は丸ごとスキップ
   され、`init_db()` だけが走る（＝ミラー投入後に誤って走らせても中身を消さない）。

## 旧ローカル DB について

このマシンの `financial_db` には Supabase 移行前（2026-02〜05 で凍結）の開発 DB が残っている。
`stock_price_history`（1,636,505行・2024-05-17〜2026-02-20・3,960社の**日次 OHLCV**）は、
現行 Supabase の `stock_price_daily` が `DAILY_WINDOW_DAYS=183` でローリング削除されるため
**もう本番には存在しない**。週次は `close_last` と集約しか持たないので O/H/L は復元できず、
Yahoo から取り直すと 3,960社ぶんで数十時間かかる。よって **DROP せず改名して温存**する。

実行:
    python -m scripts.setup_local_db            # ドライラン（何も変更しない）
    python -m scripts.setup_local_db --apply    # 実行
"""
from __future__ import annotations

import argparse
import re
import sys

from sqlalchemy import text

# 旧世代の日次 OHLCV。DROP せず改名して残す（上記の理由）。
LEGACY_TABLE = "stock_price_history"
LEGACY_RENAMED = "legacy_stock_price_history_2026_02"

# 旧世代のうち、現行スキーマと同名で中身が別物のもの。改名では逃げられないので DROP する。
# `companies` は最後（他3本の FK 先）。CASCADE で改名済み legacy 表に残る FK 制約だけが外れる。
LEGACY_DROP_TABLES = ("financial_records", "macro_data", "collection_logs", "companies")

# 「現行スキーマが既に入っている」ことの目印。これがあれば旧世代の掃除は一切行わない。
CURRENT_SCHEMA_MARKER = "stock_price_weekly"


def mask(url: str) -> str:
    """接続文字列のパスワードを伏せる（ログ・標準出力へ出すため）。"""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def _tables(conn) -> set[str]:
    rows = conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    )).fetchall()
    return {r[0] for r in rows}


def _views(conn) -> set[str]:
    rows = conn.execute(text(
        "SELECT table_name FROM information_schema.views WHERE table_schema='public'"
    )).fetchall()
    return {r[0] for r in rows}


def _count(conn, table: str) -> int:
    return conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar()


def guard_local() -> str:
    """ローカル以外を指していたら止める。**このスクリプト唯一の不可逆性の歯止め**。"""
    import database

    url = database.DATABASE_URL
    print(f"接続先: {mask(url)}")
    if not database._is_local:
        raise SystemExit(
            "中止: DATABASE_URL がローカルを指していません。\n"
            "  init_db() は無条件に DDL（DROP COLUMN 移行を含む）を打つため、本番へ向けたまま\n"
            "  実行すると不可逆です。先に環境変数を立ててから実行してください:\n"
            '    $env:DATABASE_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"\n'
            "  （load_dotenv() は override=False なので、先に立てた環境変数が .env に勝ちます）"
        )
    return url


def report(conn, *, expected_tables: set[str], expected_views: set[str]) -> bool:
    """作成後の検証レポート。全項目 OK なら True。"""
    ok = True
    tables, views = _tables(conn), _views(conn)

    missing_t = sorted(expected_tables - tables)
    print(f"\n[検証] テーブル {len(expected_tables) - len(missing_t)}/{len(expected_tables)} 本")
    if missing_t:
        ok = False
        print(f"  NG 未作成: {missing_t}")
    else:
        print("  OK すべて存在")

    missing_v = sorted(expected_views - views)
    print(f"[検証] VIEW {len(expected_views) - len(missing_v)}/{len(expected_views)} 本")
    if missing_v:
        ok = False
        print(f"  NG 未作成: {missing_v}")
    else:
        for v in sorted(expected_views):
            # 空でも SELECT が通ること＝STDDEV_SAMP / ::numeric / 名前付き WINDOW 句が
            # このサーバで解釈できることの実証（VIEW 作成だけでは本体が評価されない）
            try:
                conn.execute(text(f'SELECT * FROM public."{v}" LIMIT 1')).fetchall()
                print(f"  OK {v}: SELECT 可")
            except Exception as e:
                ok = False
                print(f"  NG {v}: SELECT 不可 {type(e).__name__}: {str(e).splitlines()[0]}")

    # security_invoker は _ensure_one_view が失敗しても debug ログに握るだけ＝黙って消える。
    # 非 superuser で通ったかを明示的に確かめる（RLS を効かせる前提の設定・#344）。
    print("[検証] VIEW の security_invoker")
    rows = conn.execute(text(
        "SELECT c.relname, c.reloptions FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='v' ORDER BY c.relname"
    )).fetchall()
    for name, opts in rows:
        has = any("security_invoker=true" in o for o in (opts or []))
        print(f"  {'OK' if has else '警告'} {name}: {opts}")
        if not has:
            ok = False

    if LEGACY_RENAMED in tables:
        print(f"[検証] 温存した旧日次 OHLCV: {LEGACY_RENAMED} = {_count(conn, LEGACY_RENAMED):,} 行")
    elif LEGACY_TABLE in tables:
        print(f"[検証] 旧日次 OHLCV は未改名のまま: {LEGACY_TABLE}")
    else:
        print("[検証] 旧日次 OHLCV は存在しない（このマシンでは初回セットアップ）")

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ローカル PostgreSQL を本アプリのスキーマで初期化する（Supabase へは接続しない）")
    ap.add_argument("--apply", action="store_true",
                    help="実際に DDL を実行する（既定はドライラン）")
    args = ap.parse_args()

    guard_local()
    import database

    expected_tables = set(database.Base.metadata.tables)
    expected_views = set(database.ViewBase.metadata.tables)

    with database.engine.connect() as conn:
        tables = _tables(conn)
        legacy_present = LEGACY_TABLE in tables and CURRENT_SCHEMA_MARKER not in tables
        print(f"現在の public テーブル: {len(tables)} 本")
        if legacy_present:
            print("  → 旧世代（Supabase 移行前）のスキーマを検出")
            for t in (LEGACY_TABLE,) + LEGACY_DROP_TABLES:
                if t in tables:
                    print(f"     {t:22s} {_count(conn, t):>10,} 行")
        elif CURRENT_SCHEMA_MARKER in tables:
            print("  → 現行スキーマ済み。旧世代の掃除は行わない（init_db のみ）")
        else:
            print("  → 空 DB。旧世代の掃除は不要（init_db のみ）")

    print("\n実行する操作:")
    steps = []
    if legacy_present:
        steps.append(f'ALTER TABLE {LEGACY_TABLE} RENAME TO {LEGACY_RENAMED}   （日次 OHLCV を温存）')
        steps.append("DROP TABLE " + ", ".join(LEGACY_DROP_TABLES) + " CASCADE   （旧世代・退避済み）")
    steps.append("init_db()   （全テーブル ＋ VIEW 2本を冪等生成）")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")

    if not args.apply:
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")
        return 0

    if legacy_present:
        with database.engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE public."{LEGACY_TABLE}" RENAME TO "{LEGACY_RENAMED}"'))
            print(f"\n改名: {LEGACY_TABLE} -> {LEGACY_RENAMED}")
            for t in LEGACY_DROP_TABLES:
                conn.execute(text(f'DROP TABLE IF EXISTS public."{t}" CASCADE'))
                print(f"DROP: {t}")

    print("\ninit_db() 実行中...")
    database.init_db()
    print("init_db() 完了")

    with database.engine.connect() as conn:
        ok = report(conn, expected_tables=expected_tables, expected_views=expected_views)

    print("\n" + ("すべて OK" if ok else "警告あり（上記 NG/警告 を確認すること）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
