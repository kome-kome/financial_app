"""正本からローカルミラーへ増分だけ取り込む（Issue #481 B-3）。

## なぜ必要か

初回 pull（B-2）はコア約 300MB を1回で引く。以後これを毎回やると枠（5GB/月）の 6% を
1回ごとに食う。実際の1日の増分は週次株価で約 4,400 行しかないので、差分だけ取れば
桁が変わる。#480（夜間バッチの週次株価を差分ロードにする）と同じ設計思想。

## オーバーラップが要る理由（ここが設計の肝）

「高水位より新しい行だけ取る」では**取り落とす**。増分キーの信頼度が3段階に割れているため:

  ◎ `onupdate` 付き     companies / financial_records / regression_results
      -> 値の訂正でも時刻が進むので、高水位＋1日で十分
  ○ 追記型 `created_at`  macro_beta_* / recommend_factor_premia / collection_logs
      -> 既存行を更新しないので追記だけ拾えばよい。ただし collection_logs は
         running -> done の後追い UPDATE があるため未完了行を毎回取り直す
  x 時刻列が無い / upsert で進まない
      stock_price_weekly  -> 時刻列そのものが無い。しかも `_recompute_weeks_from_daily` が
                             最大 DAILY_WINDOW_DAYS=183 日遡って過去週を上書きする
      macro_data / statement_disclosure
                          -> `created_at` が upsert の set_ に含まれず、**値だけの訂正で進まない**

したがって x 群は「日付列の高水位から overlap 日ぶん遡って無条件に取り直す」しかない。
`stock_price_weekly` の overlap は `DAILY_WINDOW_DAYS` から導出する（27週=189日）。
Issue #481 / #480 の当初案「末尾8週」は 56 日ぶんしか覆わず、**183 日の遡及上書きを取り落とす**。

## Egress

source からの SELECT は `mirror_common.make_engine()` が `db_egress.install()` を掛けるので
**自動的に台帳へ載る**（ADR-0034）。実行後の `[egress] summary` 行で1回ぶんの転送量が分かる。

実行:
    python -m scripts.mirror_sync                    # ドライラン: 高水位と取得予定行数だけ出す
    python -m scripts.mirror_sync --apply
    python -m scripts.mirror_sync --apply --tables stock_price_weekly,macro_data
    python -m scripts.mirror_sync --source-url ... --dest-url ... --apply   # 予行演習

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database import Base
from scripts import mirror_common as mc
from scripts.mirror_verify import add_endpoint_args, compare, endpoints, report, selected_tables

# 1 INSERT あたりの行数。大きすぎると psycopg2 のパラメータ上限（65535）に当たる。
# 列数最大は financial_records の 69 列なので 900 行 * 69 = 62,100 で収まる。
CHUNK_ROWS = 900

# 取得予定がこれを超えたら中止する安全弁。増分同期が想定するのは1日ぶんの差分
# （週次で約12万行）であって、ミラーが空のときの初期ロードではない。
# `fetch_rows` は結果を全部メモリへ載せるので、1.28M 行の週次を素で引くと数 GB になる。
# その用途は `mirror_pull`（pg_dump 経由でストリームする）が担当する。
MAX_SYNC_ROWS = 500_000


def delta_where(table: str) -> tuple[str, dict]:
    """WATERMARK 表の「ここ以降を取り直す」WHERE 句テンプレート（値は後で束ねる）。"""
    sync = mc.SYNC_PLAN[table]
    clause = f'"{sync.key}" >= :since'
    if sync.extra_where:
        clause = f"({clause} OR {sync.extra_where})"
    return clause, {}


def plan_table(src_conn, dst_conn, table: str) -> dict:
    """1表ぶんの取得計画。**source へは count(*) しか投げない**（Egress ほぼ 0）。"""
    sync = mc.SYNC_PLAN[table]
    if sync.mode == mc.MODE_FULL:
        n = src_conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar()
        n_dst = dst_conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar()
        # source が空でミラーに残骸がある場合も**掃除が要る**。n_fetch=0 で飛ばすと
        # 「正本から消えた行がミラーに残り続ける」形で静かに乖離する。
        return {"table": table, "mode": sync.mode, "since": None, "n_fetch": n,
                "needs_delete": n == 0 and n_dst > 0}

    hi = mc.watermark(dst_conn, table)
    since = mc.since_value(table, sync.key, hi, sync.overlap_days)
    if since is None:
        n = src_conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar()
        return {"table": table, "mode": sync.mode, "since": None, "n_fetch": n,
                "note": "ミラーが空のため全件"}
    clause, _ = delta_where(table)
    n = src_conn.execute(
        text(f'SELECT count(*) FROM public."{table}" WHERE {clause}'), {"since": since}
    ).scalar()
    return {"table": table, "mode": sync.mode, "since": since, "n_fetch": n,
            "watermark": hi, "overlap_days": sync.overlap_days}


def fetch_rows(src_conn, table: str, since) -> list[dict]:
    cols = mc.table_columns(table)
    col_sql = ", ".join(f'"{c}"' for c in cols)
    if since is None:
        stmt = text(f'SELECT {col_sql} FROM public."{table}"')
        params = {}
    else:
        clause, _ = delta_where(table)
        stmt = text(f'SELECT {col_sql} FROM public."{table}" WHERE {clause}')
        params = {"since": since}
    return [dict(r) for r in src_conn.execute(stmt, params).mappings()]


def apply_rows(dst_conn, table: str, rows: list[dict], *, mode: str) -> int:
    """ミラー側へ反映する。FULL は全置換、WATERMARK は PK で upsert。

    upsert の `set_` を `__table__.columns` から自動生成するのは
    `database.upsert_statement_disclosures` のイディオム。列を足したときに
    同期対象から黙って漏れるのを防ぐ。
    """
    tbl = Base.metadata.tables[table]
    if mode == mc.MODE_FULL:
        dst_conn.execute(text(f'DELETE FROM public."{table}"'))
        for i in range(0, len(rows), CHUNK_ROWS):
            dst_conn.execute(tbl.insert(), rows[i:i + CHUNK_ROWS])
        return len(rows)

    pk = list(mc.primary_key_columns(table))
    update_cols = [c.name for c in tbl.columns if c.name not in pk]
    for i in range(0, len(rows), CHUNK_ROWS):
        chunk = rows[i:i + CHUNK_ROWS]
        stmt = pg_insert(tbl).values(chunk)
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=pk,
                set_={c: getattr(stmt.excluded, c) for c in update_cols})
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk)
        dst_conn.execute(stmt)
    return len(rows)


def print_plan(plans: list[dict]) -> int:
    head = f"{'table':<26}{'mode':<11}{'fetch':>10}  since"
    print(head)
    print("-" * (len(head) + 6))
    total = 0
    for p in plans:
        total += p["n_fetch"] or 0
        since = "-" if p["since"] is None else str(p["since"])[:19]
        extra = ""
        if p.get("overlap_days"):
            extra = f"  (高水位 {str(p.get('watermark'))[:10]} から {p['overlap_days']}日遡及)"
        if p.get("note"):
            extra = "  " + p["note"]
        print(f"{p['table']:<26}{p['mode']:<11}{p['n_fetch']:>10,}  {since}{extra}")
    print(f"\n取得予定 合計 {total:,} 行")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="正本からローカルミラーへ増分同期")
    add_endpoint_args(ap)
    ap.add_argument("--apply", action="store_true",
                    help="実際に取得・投入する（既定はドライラン）")
    ap.add_argument("--verify", action="store_true",
                    help="同期後に mirror_verify 相当の突合を行う")
    ap.add_argument("--max-rows", type=int, default=MAX_SYNC_ROWS,
                    help=f"取得予定がこの行数を超えたら中止する（既定 {MAX_SYNC_ROWS:,}）")
    args = ap.parse_args()

    source, dest = endpoints(args)
    mc.guard_dest_local(dest)
    tables = selected_tables(args)
    mc.print_endpoints(source, dest)
    print()

    src_eng = mc.make_engine(source)
    dst_eng = mc.make_engine(dest)
    try:
        with src_eng.connect() as sc, dst_eng.connect() as dc:
            plans = [plan_table(sc, dc, t) for t in tables]
    except Exception as e:
        print(f"接続または集計に失敗しました: {type(e).__name__}: "
              f"{mc.mask_url(str(e)).splitlines()[0]}", file=sys.stderr)
        src_eng.dispose(); dst_eng.dispose()
        return 2

    total = print_plan(plans)

    if not args.apply:
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")
        src_eng.dispose(); dst_eng.dispose()
        return 0

    if total > args.max_rows:
        src_eng.dispose(); dst_eng.dispose()
        raise SystemExit(
            f"\n中止: 取得予定 {total:,} 行が上限 {args.max_rows:,} 行を超えています。\n"
            "  増分同期が想定するのは1日ぶんの差分です。この規模はミラーが空か、\n"
            "  長く同期していないかのどちらかなので、一括で入れ直すほうが速くて安全です:\n"
            "    python -m scripts.mirror_pull --apply --allow-full-pull\n"
            "  意図した増分なら --max-rows で上限を上げてください。")

    print()
    applied = 0
    with src_eng.connect() as sc:
        for p in plans:
            if not p["n_fetch"]:
                if p.get("needs_delete"):
                    # 正本が空になった全置換表。ミラーの残骸を掃除する
                    with dst_eng.begin() as dc:
                        dc.execute(text(f'DELETE FROM public."{p["table"]}"'))
                    print(f"  {p['table']:<26} 正本が空のため掃除")
                else:
                    print(f"  {p['table']:<26} 変化なし")
                continue
            rows = fetch_rows(sc, p["table"], p["since"])
            # 表ごとに1トランザクション。途中で落ちても既に入った表は残る（冪等なので再実行可）。
            with dst_eng.begin() as dc:
                n = apply_rows(dc, p["table"], rows, mode=p["mode"])
            applied += n
            print(f"  {p['table']:<26} {n:>10,} 行を反映")

    with dst_eng.begin() as dc:
        fixed = mc.resync_sequences(dc, tables)
    print(f"\n[sequence] {len(fixed)} 本のシーケンスを再同期しました")
    print(f"同期完了: {applied:,} 行（計画 {total:,} 行）")

    rc = 0
    if args.verify:
        print("\n[検証] 突合しています...")
        with src_eng.connect() as sc, dst_eng.connect() as dc:
            rows = compare(mc.table_stats(sc, tables), mc.table_stats(dc, tables), tables)
        ok = report(rows, with_bytes=False)
        rc = 0 if ok else 1

    src_eng.dispose(); dst_eng.dispose()
    return rc


if __name__ == "__main__":
    sys.exit(main())
