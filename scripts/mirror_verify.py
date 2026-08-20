"""ミラーと正本を突合する（Issue #481 B-4）。

## なぜ必要か

**2つの DB の内容が食い違っていないかを、行数・値レベルで答える**のがこのスクリプト。
`/api/stats` の `freshness` も `/api/system/info` のバッジも「どちらの DB か」は見せても
「中身がどれだけ食い違うか」は答えない。

当初（#481）の用途は「同期を忘れたミラーが古いスコアを最新の顔で見せ続ける」ことの検出
だった——夜間バッチが Supabase 側に書き、ローカルは pull / sync した時点で止まるため
（#438 の静かな配信停止と同型）。**#503 で正本がローカルへ移り、その向きは無くなった**。

いまの用途は2つ:

1. **バックアップの検証**。復元先とローカル正本を突合する（`--source local --dest-url <復元先>`）。
   エンドポイントを引数で受ける設計（ADR-0035）がそのまま効く。
2. **Supabase の断面がいつのものかを確認する**。凍結した 2026-08-07 断面と正本の差分を見る。

## 3段の検査（--level・累積ではなく択一）

| level | 見るもの | source への負荷 | 何を検出できるか |
|---|---|---|---|
| `schema` | `information_schema.columns` | 数百行・スキャン無し | 列集合と型の乖離。**pull の事前確認**（後述） |
| `counts`（既定） | `count(*)` / `max(キー)` / `min(キー)` | 表あたり1行 | 件数ずれ・同期の遅れ |
| `checksum` | 全行の md5 集約 | 表あたり1行だが**全表スキャン** | **過去行の値だけの訂正**（#465 の分割段差修復がこの形） |

`schema` を pull の前に走らせる理由: `pg_dump --data-only` は source の列リストで
`COPY t (a,b,c)` を吐く。dest に無い列が source にあれば restore は落ちて気づけるが、
**dest にしか無い列は黙って NULL のまま残る**。転送してから気づくのでは Egress を無駄に払う。

## Egress

`schema` と `counts` は行を1行も転送しない（集約とメタデータのみ）ので、
Supabase が restricted でない限り毎日回せる。`checksum` はサーバ側の全表スキャンになるため
`statement_timeout=2min`（ADR-0032）に注意すること。

## 終了コード

  0  一致（または --warn-only）
  1  乖離あり
  2  接続できない・テーブルが無い等

`scripts/check_macro_health.py` と同じ「判定して非ゼロ終了、調査用に --warn-only」の形。

実行:
    python -m scripts.mirror_verify                       # 件数で突合（既定）
    python -m scripts.mirror_verify --level schema        # pull 前の列差分チェック
    python -m scripts.mirror_verify --level checksum      # 値レベルまで突合
    python -m scripts.mirror_verify --bytes               # 転送量の見積りも出す
    python -m scripts.mirror_verify --source-url ... --dest-url ...   # 予行演習

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import sys

from scripts import mirror_common as mc

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

LEVELS = ("schema", "counts", "checksum")


def add_endpoint_args(ap: argparse.ArgumentParser) -> None:
    """3本で共通のエンドポイント引数。source/dest を差し替えれば予行演習になる。"""
    ap.add_argument("--source", default="prod",
                    help="正本のエンドポイント（prod / local / 接続URL）。既定 prod")
    ap.add_argument("--dest", default="local",
                    help="ミラーのエンドポイント（local / 接続URL）。既定 local")
    ap.add_argument("--source-url", default=None, help="--source の代わりに接続URLを直接指定")
    ap.add_argument("--dest-url", default=None, help="--dest の代わりに接続URLを直接指定")
    ap.add_argument("--tables", default=None,
                    help="対象テーブルをカンマ区切りで限定（既定は全16表）")


def endpoints(args) -> tuple[mc.Endpoint, mc.Endpoint]:
    source = mc.resolve_endpoint(args.source_url or args.source)
    dest = mc.resolve_endpoint(args.dest_url or args.dest)
    if source.url == dest.url:
        raise SystemExit(
            "中止: source と dest が同じ接続先です: " + mc.mask_url(source.url) + "\n"
            "  ミラーは2つのエンドポイント間の操作です。予行演習でも別の DB を使ってください。")
    return source, dest


def selected_tables(args) -> tuple[str, ...]:
    allowed = mc.mirror_tables()
    if not args.tables:
        return allowed
    picked = tuple(t.strip() for t in args.tables.split(",") if t.strip())
    unknown = [t for t in picked if t not in allowed]
    if unknown:
        raise SystemExit(f"中止: ミラー対象外のテーブルが指定されました: {unknown}\n"
                         f"  対象は {allowed}")
    # 指定順ではなく FK 依存順へ並べ直す（restore 順序を壊さないため）
    return tuple(t for t in allowed if t in picked)


# ── level=schema ──────────────────────────────────────────────────────────────

def compare_schema(src_cols: dict, dst_cols: dict, tables) -> list[dict]:
    rows = []
    for t in tables:
        s, d = src_cols.get(t, {}), dst_cols.get(t, {})
        only_src = sorted(set(s) - set(d))
        only_dst = sorted(set(d) - set(s))
        type_diff = sorted(c for c in set(s) & set(d) if s[c] != d[c])
        rows.append({"table": t, "only_src": only_src, "only_dst": only_dst,
                     "type_diff": type_diff,
                     "ok": not (only_src or only_dst or type_diff)})
    return rows


def report_schema(rows: list[dict]) -> bool:
    all_ok = True
    for r in rows:
        if r["ok"]:
            continue
        all_ok = False
        print(f"NG {r['table']}")
        if r["only_src"]:
            print(f"     source にしか無い列（restore がエラーで落ちる）: {r['only_src']}")
        if r["only_dst"]:
            print(f"     mirror にしか無い列（黙って NULL のまま残る）  : {r['only_dst']}")
        if r["type_diff"]:
            print(f"     型が違う列: {r['type_diff']}")
    if all_ok:
        print(f"OK  {len(rows)} 表すべてで列集合と型が一致しています")
    return all_ok


# ── level=counts / checksum ───────────────────────────────────────────────────

def compare(src_stats: dict, dst_stats: dict, tables, *, with_checksum: bool = False) -> list[dict]:
    """表ごとの突合結果。`ok` が全て True なら一致。"""
    rows = []
    for t in tables:
        s, d = src_stats.get(t, {}), dst_stats.get(t, {})
        ok = (s.get("n") == d.get("n")) and (str(s.get("hi")) == str(d.get("hi")))
        ck_ok = None
        if with_checksum:
            ck_ok = str(s.get("ck")) == str(d.get("ck"))
            ok = ok and ck_ok
        rows.append({"table": t, "src_n": s.get("n"), "dst_n": d.get("n"),
                     "src_hi": s.get("hi"), "dst_hi": d.get("hi"),
                     "src_bytes": s.get("nbytes"), "ck_ok": ck_ok, "ok": ok})
    return rows


def report(rows: list[dict], *, with_bytes: bool = False, with_checksum: bool = False) -> bool:
    head = f"{'table':<26}{'source':>12}{'mirror':>12}{'diff':>10}  latest"
    if with_checksum:
        head += "   ck"
    if with_bytes:
        head += "   est"
    print(head)
    print("-" * (len(head) + 4))
    all_ok = True
    for r in rows:
        s_n = r["src_n"] if r["src_n"] is not None else -1
        d_n = r["dst_n"] if r["dst_n"] is not None else -1
        mark = "OK " if r["ok"] else "NG "
        latest = "" if r["src_hi"] is None else str(r["src_hi"])[:19]
        if str(r["src_hi"]) != str(r["dst_hi"]):
            latest += f" != {str(r['dst_hi'])[:19]}"
        line = f"{mark}{r['table']:<23}{s_n:>12,}{d_n:>12,}{d_n - s_n:>+10,}  {latest}"
        if with_checksum:
            line += "   " + ("OK" if r["ck_ok"] else "NG")
        if with_bytes and r["src_bytes"] is not None:
            line += f"   {mc.mb(r['src_bytes'])}"
        print(line)
        all_ok = all_ok and r["ok"]
    if with_bytes:
        total = sum(r["src_bytes"] or 0 for r in rows)
        print(f"\n正本側の推定転送量（全件を引いた場合）: {mc.mb(total)}")
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ミラー（ローカル）と正本（Supabase）を突合する")
    add_endpoint_args(ap)
    ap.add_argument("--level", choices=LEVELS, default="counts",
                    help="schema=列差分 / counts=件数と最新キー（既定） / checksum=値レベル")
    ap.add_argument("--bytes", action="store_true", dest="with_bytes",
                    help="サーバ側 octet_length で転送量も見積もる（全表スキャンが走る）")
    ap.add_argument("--warn-only", action="store_true",
                    help="乖離があっても終了コード 0 で返す（調査用）")
    args = ap.parse_args()

    source, dest = endpoints(args)
    tables = selected_tables(args)
    mc.print_endpoints(source, dest)
    print(f"level : {args.level}\n")

    want_ck = args.level == "checksum"
    src_eng = mc.make_engine(source)
    dst_eng = mc.make_engine(dest)
    try:
        with src_eng.connect() as sc, dst_eng.connect() as dc:
            if args.level == "schema":
                rows = compare_schema(mc.schema_columns(sc, tables),
                                      mc.schema_columns(dc, tables), tables)
                all_ok = report_schema(rows)
            else:
                src_stats = mc.table_stats(sc, tables, with_bytes=args.with_bytes,
                                           with_checksum=want_ck)
                dst_stats = mc.table_stats(dc, tables, with_checksum=want_ck)
                rows = compare(src_stats, dst_stats, tables, with_checksum=want_ck)
                all_ok = report(rows, with_bytes=args.with_bytes, with_checksum=want_ck)
    except Exception as e:
        print(f"接続または集計に失敗しました: {type(e).__name__}: "
              f"{mc.mask_url(str(e)).splitlines()[0]}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        src_eng.dispose()
        dst_eng.dispose()

    print()
    if all_ok:
        print("一致: 乖離はありません")
        return EXIT_OK
    drifted = [r["table"] for r in rows if not r["ok"]]
    print(f"乖離: {len(drifted)} 表 -> {drifted}")
    if args.level == "schema":
        print("  スキーマを揃えるには: python -m scripts.setup_local_db --apply")
    else:
        print("  同期するには: python -m scripts.mirror_sync --apply")
    return EXIT_OK if args.warn_only else EXIT_DRIFT


if __name__ == "__main__":
    sys.exit(main())
