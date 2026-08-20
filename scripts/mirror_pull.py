"""正本（Supabase）からローカルミラーへ一括で取り込む（Issue #481 B-2）。

## なぜ必要か

Supabase が Egress 超過で restricted になるとアプリも分析も一切動かせない（2026-07・2026-08 に
2回発生し、2回目は8日間まるごと停止した）。ローカルに読取レプリカがあれば障害時も動く。
器（B-0）と接続先スイッチ（B-1）は済んでいるので、あとはデータを入れるだけ——それが本スクリプト。

**2026-08-20 の実行をもって役目を終えた**（#503・ADR-0038）。この pull（17表・152.8MB・
checksum 16表一致）が正本の**引き渡し点**になり、以降 Supabase から引く理由が無くなった。
正本を Supabase へ戻す決定をしたときのために残してある。

## サーキットブレーカが効かない唯一の経路

`pg_dump` は SQLAlchemy を通らないため、**`db_egress` のブレーカが転送を途中で止められない**
（ADR-0034 のブレーカは `after_cursor_execute` に張ってある）。したがって歯止めは事前に置く:

1. `--allow-full-pull` を明示しない限り、見積りが `FULL_PULL_MB_THRESHOLD` を超えたら中止する
   （`scripts/candidate_bakeoff.py` の同名イディオムを踏襲）
2. 見積りはサーバ側 `sum(octet_length(...))`＝**表ごとに1行しか返らない**（#446 の測り方）
3. 実行後に `LEDGER.record_external()` で台帳へ記帳する。これは**事後**であり歯止めではない

`--compress=0` で dump するのも計測のため。custom 形式は既定でローカル圧縮するが、
ワイヤ上は非圧縮の COPY ストリームが流れるので、圧縮したままだとファイルサイズが
実 Egress を大幅に過小申告する。

## restore が単一スレッドである理由

`edinet` は superuser でないため `pg_restore --disable-triggers` が使えず、FK は順序で満たすしかない
（FK は4本ともに `companies.edinet_code` 向き）。並列 `--jobs` は順序が崩れるので使わない。

## TRUNCATE の連鎖について

`companies` を空にするには、それを参照する表も同時に空になる必要がある。範囲外の表を
巻き込むときは **ドライランで必ず件数付きで予告する**（`collateral_rows`）。

`stock_price_daily` をミラー範囲へ入れた 2026-08-20 以降、巻き込まれる範囲外の表は無い
（残る除外 `xbrl_raw_documents` は FK を持たない）。予告の仕組みは、FK 子を持つ表を将来
また範囲外にしたときのために残してある。

実行:
    python -m scripts.mirror_pull                            # ドライラン（既定・何も変更しない）
    python -m scripts.mirror_pull --apply --allow-full-pull  # 本番 pull
    python -m scripts.mirror_pull --source-url ... --dest-url ... --apply   # 予行演習

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from db_egress import LEDGER, egress_budget
from scripts import mirror_common as mc
from scripts.mirror_verify import (add_endpoint_args, compare, compare_schema, endpoints,
                                   report, report_schema, selected_tables)

# これを超える見積りは --allow-full-pull を要求する。ミラー初回は 300MB 級なので
# 必ず明示させる＝「気づいたら枠を食っていた」を作らない。
FULL_PULL_MB_THRESHOLD = 50.0

DUMP_DIR = Path(__file__).resolve().parent.parent / "migration_dumps"


def server_major(conn) -> int:
    raw = conn.execute(text("SHOW server_version")).scalar() or ""
    m = re.match(r"(\d+)", str(raw))
    return int(m.group(1)) if m else 0


def client_major() -> int:
    proc = subprocess.run([mc.pg_bin("pg_dump"), "--version"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    m = re.search(r"(\d+)", proc.stdout or "")
    return int(m.group(1)) if m else 0


def check_versions(conn) -> None:
    """pg_dump クライアントはサーバ以上である必要がある（逆方向は不可）。"""
    s, c = server_major(conn), client_major()
    print(f"[版] source サーバ PG{s} / pg_dump クライアント PG{c}")
    if c and s and c < s:
        raise SystemExit(
            f"中止: pg_dump クライアント (PG{c}) が source サーバ (PG{s}) より古いため dump できません。\n"
            f"  PG{s} 以上のクライアントを FINAPP_PG_BIN で指定してください。")


def collateral_rows(conn, tables) -> dict[str, int]:
    """TRUNCATE に巻き込まれる「ミラー範囲外」の表とその件数。

    `companies` を空にするには、それを参照する表も同時に TRUNCATE される必要がある。
    `CASCADE` で済ませると **消える表がコードから読めなくなる**ので
    `mirror_common.truncate_targets()` がメタデータから明示列挙し、ここで件数を数えて
    ドライランに出す。2026-08-20 現在、該当する範囲外の表は無い（空 dict が返る）。
    """
    extra = [t for t in mc.truncate_targets(tables) if t not in tables]
    return {t: conn.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar()
            for t in extra}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="正本からローカルミラーへ一括取り込み（pg_dump -> pg_restore）")
    add_endpoint_args(ap)
    ap.add_argument("--apply", action="store_true",
                    help="実際に転送・投入する（既定はドライラン）")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help=f"見積りが {FULL_PULL_MB_THRESHOLD:.0f}MB を超えても実行する")
    ap.add_argument("--keep-dump", action="store_true",
                    help="restore 後もダンプファイルを残す（既定は削除）")
    args = ap.parse_args()

    source, dest = endpoints(args)
    mc.guard_dest_local(dest)
    tables = selected_tables(args)
    mc.print_endpoints(source, dest)
    print(f"対象 {len(tables)} 表（FK 依存順）: {', '.join(tables)}")
    print(f"除外: {', '.join(mc.MIRROR_EXCLUDED)}\n")

    src_eng = mc.make_engine(source)
    dst_eng = mc.make_engine(dest)
    try:
        with src_eng.connect() as sc, dst_eng.connect() as dc:
            check_versions(sc)
            # スキーマ乖離のプリフライト。**転送する前に**確認する——source にしか無い列は
            # restore が落ちて気づけるが、dest にしか無い列は黙って NULL のまま残る。
            print("[事前] 列集合を突合しています（メタデータのみ）...")
            schema_rows = compare_schema(mc.schema_columns(sc, tables),
                                         mc.schema_columns(dc, tables), tables)
            schema_ok = report_schema(schema_rows)
            print("\n[見積] サーバ側 octet_length で転送量を測っています（行は転送しません）...")
            src_stats = mc.table_stats(sc, tables, with_bytes=True)
            dst_stats = mc.table_stats(dc, tables)
            collateral = collateral_rows(dc, tables)
    except Exception as e:
        print(f"接続または集計に失敗しました: {type(e).__name__}: "
              f"{mc.mask_url(str(e)).splitlines()[0]}", file=sys.stderr)
        src_eng.dispose(); dst_eng.dispose()
        return 2

    est_bytes = sum(v.get("nbytes") or 0 for v in src_stats.values())
    est_mb = est_bytes / (1024 * 1024)
    print()
    report(compare(src_stats, dst_stats, tables), with_bytes=True)

    dump_path = DUMP_DIR / f"mirror_{source.dbname}_{mc.now_stamp()}.dump"
    truncate_list = mc.truncate_targets(tables)
    print("\n実行する操作:")
    steps = [
        f'pg_dump --strict-names --data-only --compress=0 ({len(tables)} 表) -> {dump_path.name}'
        f'   [推定 {mc.mb(est_bytes)} の Egress]',
        f"TRUNCATE {len(truncate_list)} 表（明示列挙・CASCADE は使わない）",
        f"pg_restore を FK 依存順に {len(tables)} 回（1表ずつ・--single-transaction）",
        "serial シーケンスを max(id) へ再同期 -> ANALYZE",
        "LEDGER.record_external() で台帳へ記帳 -> 突合",
    ]
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")
    if collateral:
        print("\n  注意: FK のためミラー範囲外も TRUNCATE されます（再収集可能なため許容）:")
        for t, n in collateral.items():
            print(f"    - {t}: {n:,} 行")

    if not schema_ok:
        src_eng.dispose(); dst_eng.dispose()
        raise SystemExit(
            "\n中止: スキーマが乖離しています。転送する前に揃えてください。\n"
            "  ミラー側: python -m scripts.setup_local_db --apply")

    if not args.apply:
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")
        src_eng.dispose(); dst_eng.dispose()
        return 0

    if est_mb > FULL_PULL_MB_THRESHOLD and not args.allow_full_pull:
        src_eng.dispose(); dst_eng.dispose()
        raise SystemExit(
            f"中止: 推定転送量 {est_mb:.1f}MB が上限 {FULL_PULL_MB_THRESHOLD:.0f}MB を超えています。\n"
            "  初回ミラー投入など、意図した一回性の転送なら --allow-full-pull を付けてください。\n"
            "  pg_dump は SQLAlchemy を通らずサーキットブレーカが途中で止められないため、\n"
            "  この事前ゲートが唯一の歯止めです。")

    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    src_conn = mc.PgConn.from_url(source.url)
    dst_conn = mc.PgConn.from_url(dest.url)

    # 予算の局所引き上げ。`record_external()` は予算チェックを走らせるので、
    # これが無いと**正当な pull そのものがブレーカに引っかかって落ちる**。
    # 「すでに使った分 ＋ 見積りの 1.5 倍 ＋ 余裕」を上限にする（既定は書き換えない＝ADR-0032）。
    spent_mb = LEDGER.snapshot()["est_bytes"] / (1024 * 1024)
    with egress_budget(mb=spent_mb + max(est_mb * 1.5, 50.0) + 50.0):
        print(f"\n[dump] {dump_path.name} を作成中...")
        mc.run_pg(mc.pg_dump_argv(src_conn, tables, str(dump_path)), src_conn, what="pg_dump")
        actual = dump_path.stat().st_size
        print(f"[dump] 完了 {mc.mb(actual)}（見積り {mc.mb(est_bytes)}）")
        LEDGER.record_external("mirror_pull", actual,
                               f"pg_dump {len(tables)} tables from {source.spec}")

        print(f"[truncate] ミラー側 {len(truncate_list)} 表を空にしています...")
        with dst_eng.begin() as dc:
            dc.execute(text("TRUNCATE TABLE "
                            + ", ".join(f'public."{t}"' for t in truncate_list)))

        # **1表ずつ FK 依存順に流す。** ダンプの TOC はアルファベット順であって
        # --table の指定順ではない（2026-08-16 実測）ので、1回の pg_restore に任せると
        # 「companies の頭文字が c だから偶然 FK を満たしている」状態になる。
        print("[restore] FK 依存順に1表ずつ投入中...")
        for i, t in enumerate(tables, 1):
            mc.run_pg(mc.pg_restore_argv(dst_conn, str(dump_path), t), dst_conn,
                      what=f"pg_restore({t})")
            print(f"  {i:>2}/{len(tables)} {t}")

        with dst_eng.begin() as dc:
            fixed = mc.resync_sequences(dc, tables)
        print(f"[sequence] {len(fixed)} 本のシーケンスを再同期しました")

        # 統計はダンプに含めていないので取り直す。無いと VIEW（financial_metrics）の
        # 実行計画が全表スキャンに倒れ、ローカルで開いた画面が本番より遅くなる。
        print("[analyze] 統計を再取得しています...")
        with dst_eng.connect().execution_options(isolation_level="AUTOCOMMIT") as dc:
            for t in tables:
                dc.execute(text(f'ANALYZE public."{t}"'))

    if not args.keep_dump:
        try:
            dump_path.unlink()
        except OSError:
            pass
    else:
        print(f"[dump] 保持: {dump_path}")

    print("\n[検証] 突合しています...")
    with src_eng.connect() as sc, dst_eng.connect() as dc:
        rows = compare(mc.table_stats(sc, tables), mc.table_stats(dc, tables), tables)
    src_eng.dispose(); dst_eng.dispose()
    ok = report(rows, with_bytes=False)
    print()
    if ok:
        print("pull 完了: 全テーブルで件数と最新キーが一致しました")
        return 0
    print("pull 後も乖離が残っています。上記 NG 行を確認してください。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
