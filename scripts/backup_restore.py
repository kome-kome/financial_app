"""バックアップ世代からローカルへ復元する（Issue #503 Phase 3・ADR-0038）。

## なぜ復元側を先に書くのか

**復元したことのないバックアップはバックアップではない。** `backup_push` だけを用意して
「いざとなれば戻せる」と考えるのは、`#493` 手順7 が「実装済み前提の確認手順」だったのに
実装が無かったのと同じ形の思い込みになる。取る側と戻す側は同じ回で作り、四半期に1度は
空の DB へ実際に流す（`--dest-url` を使い捨てクラスタへ向ければ本番のミラーに触れない）。

## 復元順序

**FK 依存順（親が先）で1表ずつ。** `mirror_common.mirror_tables()` がその順を返す。
ダンプは逆順（子が先）で取ってある——表ごとに別プロセスなのでスナップショット時点が
揃わず、親を後に取ることで「子が参照する親は必ず含まれる」状態にしている
（`backup_push.dump_order` の docstring）。

`pg_restore --disable-triggers` は superuser でないと使えない（`edinet` は非 superuser）ので、
FK は順序だけで満たす。**ダンプの TOC はアルファベット順であって `--table` の指定順ではない**
ため、1回の pg_restore にまとめると「`companies` の頭文字が c だから偶然通る」状態になる。

## 書き込み先はローカル限定

`mirror_common.guard_dest_local()` をそのまま使う。#503 で正本はローカルへ移ったが、
**この方向の制約は変わらない**——本番 Postgres へ書き込む経路はコードとして持たない。

実行:
    python -m scripts.backup_restore                       # 最新世代の内容を表示（ドライラン）
    python -m scripts.backup_restore --apply               # 既定のローカル DB へ復元
    python -m scripts.backup_restore --apply --dest-url postgresql://u:p@localhost:5433/scratch
    python -m scripts.backup_restore --generation 20260820T111003Z --apply

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

import database
from scripts import backup_push as bp
from scripts import mirror_common as mc


def latest_generation(store: Path = bp.LOCAL_STORE) -> Optional[str]:
    gens = sorted(p.name for p in store.iterdir()
                  if p.is_dir() and (p / bp.MANIFEST_NAME).is_file()) if store.is_dir() else []
    return gens[-1] if gens else None


def load_manifest(stamp: str, store: Path = bp.LOCAL_STORE) -> dict:
    path = store / stamp / bp.MANIFEST_NAME
    if not path.is_file():
        raise SystemExit(f"中止: マニフェストが無い: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def restore_order(manifest: dict) -> list[str]:
    """復元順＝FK 依存順。**マニフェストの並びをそのまま信用しない**。

    マニフェストは `backup_push` が依存順で書いているが、手で編集された世代や、将来の
    形式変更で並びが崩れることはありうる。復元は不可逆なので、順序は毎回コード側の
    `mirror_tables()` から取り直し、マニフェストとは集合として突き合わせる。
    """
    in_manifest = {t["table"] for t in manifest["tables"]}
    ordered = [t for t in mc.mirror_tables() if t in in_manifest]
    unknown = in_manifest - set(ordered)
    if unknown:
        raise SystemExit(
            f"中止: マニフェストに未知の表がある: {sorted(unknown)}\n"
            "  スキーマ側で改名・削除された可能性がある。復元前に器を確認すること。"
        )
    return ordered


def verify_counts(engine, manifest: dict) -> list[str]:
    """復元後の行数をマニフェストと突合し、**食い違いを文字列で返す**（空なら一致）。

    「復元が成功した」を pg_restore の exit code だけで判断しない。ダンプが空でも
    restore は成功するし、FK 順序を誤れば一部だけ入った状態でも 0 で返りうる。
    """
    expected = {t["table"]: t["rows"] for t in manifest["tables"]}
    problems = []
    with engine.connect() as c:
        for table, want in expected.items():
            got = c.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar() or 0
            if got != want:
                problems.append(f"{table}: 期待 {want:,} 行 / 実際 {got:,} 行")
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="バックアップ世代からローカルへ復元する（#503）")
    ap.add_argument("--generation", help="世代スタンプ（既定は最新）")
    ap.add_argument("--dest", default="local", help="復元先のエンドポイント（既定 local）")
    ap.add_argument("--dest-url", help="--dest の代わりに接続URLを直接指定（使い捨てクラスタ用）")
    ap.add_argument("--apply", action="store_true", help="実際に復元する（既定はドライラン）")
    ap.add_argument("--create-schema", action="store_true",
                    help="復元先に器（テーブル）が無ければ作る。ダンプは data-only なので空の DB には必須")
    args = ap.parse_args(argv)

    stamp = args.generation or latest_generation()
    if not stamp:
        raise SystemExit(f"中止: 世代が1つも無い（{bp.LOCAL_STORE}）。"
                         " まず python -m scripts.backup_push --apply を実行してください。")
    manifest = load_manifest(stamp)
    tables = restore_order(manifest)

    dest = (mc.Endpoint("url", args.dest_url) if args.dest_url
            else mc.resolve_endpoint(args.dest))
    mc.guard_dest_local(dest)

    total_mb = manifest["total_bytes"] / 1024 / 1024
    print(f"世代   : {stamp}（作成 {manifest['created_at']}）")
    print(f"元     : {manifest['source']}")
    print(f"復元先 : {database.mask_url(dest.url)}")
    print(f"内容   : {len(tables)} 表 / {total_mb:.1f}MB")

    if not args.apply:
        print("\n復元順（FK 依存順・1表ずつ）:")
        for t in tables:
            rows = next(x["rows"] for x in manifest["tables"] if x["table"] == t)
            print(f"  {t:<28} {rows:>10,} 行")
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")
        return 0

    engine = mc.make_engine(dest)
    if args.create_schema:
        print("[schema] 器を作成しています（存在するものはそのまま）...")
        database.Base.metadata.create_all(engine)

    conn = mc.PgConn.from_url(dest.url)
    print("\n[restore] FK 依存順に1表ずつ流しています...")
    for i, table in enumerate(tables, 1):
        dump = bp.LOCAL_STORE / stamp / f"{table}.dump"
        if not dump.is_file():
            raise SystemExit(f"中止: ダンプが無い: {dump}")
        mc.run_pg(mc.pg_restore_argv(conn, str(dump), table), conn,
                  what=f"pg_restore {table}")
        print(f"  {i:>2}/{len(tables)} {table}")

    print("[sequence] シーケンスを再同期しています...")
    with engine.begin() as c:
        mc.resync_sequences(c, tables)

    print("\n[検証] マニフェストの行数と突合しています...")
    problems = verify_counts(engine, manifest)
    if problems:
        print("NG  行数が一致しません:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"OK  {len(tables)} 表すべてで行数が一致しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
