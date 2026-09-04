"""正本（ローカル PostgreSQL）のバックアップを Supabase Storage へ置く（Issue #503 Phase 3・ADR-0038）。

## 役割の反転

2026-08-20 以前、Supabase は正本でローカルはその読取レプリカだった。#503 でこれが反転し、
**Supabase はバックアップの置き場**になった。ただし置くのは Postgres ではなく Storage である。
この区別が設計の要:

- Postgres へ書き戻す経路は作らない。`mirror_common.guard_dest_local()` の「dest はローカル
  限定」（ADR-0035）を**そのまま維持できる**＝ローカルから本番 DB へ書くコードは今も存在しない。
- Storage はオブジェクトストアなので、DB のサイズ（395MB / NANO 実効メモリ 408MB）にも
  swap にも触らない。#500 の再発要因を増やさずにバックアップだけ置ける。

## 制約と実測（2026-08-20）

Free プランは **50MB/ファイル・1GB/プロジェクト**。ローカル PG18 から `--compress=9` で
表ごとにダンプした実測は次のとおり:

    stock_price_weekly  17.4MB   financial_records 10.8MB   stock_price_daily 3.2MB
    macro_beta_loadings  2.5MB   macro_data         1.8MB   ... 合計 37.5MB / 17表

**1ファイルにまとめても 50MB は割る**が、表ごとに分ける。最大表と上限の距離が構造的に
大きくなる（17.4MB vs 50MB）／復元が FK 依存順の1表ずつになり `mirror_common` の既存設計と
一致する／1表だけ壊れたときに全部を落とさずに済む、の3点が理由。

1世代 37.5MB なので 1GB 枠には27世代入る。保持は「直近 N 世代＋各月の最初の1世代」。

## Supabase 抜きで検証できるようにする

`--dest local` を既定にしてある。ダンプ生成・マニフェスト・保持ポリシーの判定までは
**認証情報が無くても今日確かめられる**（ミラー3本で `--source-url` / `--dest-url` を引数化した
のと同じ発想・ADR-0035）。Storage へ実際に置くのは `--dest storage` を明示したときだけ。

必要な環境変数（`--dest storage` のときのみ）:
    SUPABASE_URL                例 https://ndebkuazchtzkxiutiqn.supabase.co
    SUPABASE_SERVICE_ROLE_KEY   ダッシュボード > Project Settings > API

実行:
    python -m scripts.backup_push                          # ドライラン（計画のみ）
    python -m scripts.backup_push --apply                  # ローカルへ世代を作る
    python -m scripts.backup_push --apply --dest storage   # Storage へ push
    python -m scripts.backup_push --list --dest storage    # 置いてある世代を見る

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from scripts import mirror_common as mc

ROOT = Path(__file__).resolve().parents[1]
LOCAL_STORE = ROOT / ".backups"

BUCKET = "db-backups"
MANIFEST_NAME = "manifest.json"

# Free プランの上限。**超えたら止める**（アップロードが 413 で落ちるより前に気づきたい）。
MAX_OBJECT_MB = 50.0
MAX_TOTAL_MB = 1024.0

# 保持ポリシー。1世代 37.5MB の実測に対し、4 + 6 = 10 世代 = 約 375MB で 1GB 枠に収まる。
KEEP_RECENT = 4        # 直近この世代数は無条件で残す
KEEP_MONTHLY = 6       # 加えて、各月の最初の世代をこのか月ぶん残す

# 圧縮レベル。9 は実測で weekly 17.4MB（0 だと 100MB 超）。保管サイズを最小化したいので最大。
COMPRESS = 9


@dataclass
class TableDump:
    table: str
    path: Path
    bytes: int
    rows: int


@dataclass
class Generation:
    """1回ぶんのバックアップ。`stamp` が世代の識別子（UTC 日付＋時刻）。"""
    stamp: str
    dumps: list[TableDump] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(d.bytes for d in self.dumps)

    def manifest(self) -> dict:
        return {
            "stamp": self.stamp,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": database.mask_url(database.DATABASE_URL),
            "compress": COMPRESS,
            "tables": [{"table": d.table, "bytes": d.bytes, "rows": d.rows} for d in self.dumps],
            "total_bytes": self.total_bytes,
            "note": "restore は FK 依存順に1表ずつ（pg_restore --disable-triggers は非 superuser では使えない）",
        }


def new_stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def stamp_date(stamp: str) -> date:
    return datetime.strptime(stamp[:8], "%Y%m%d").date()


def guard_source_is_primary() -> None:
    """バックアップ元は**正本＝ローカル**であること。

    リモートを引いてバックアップにすると、それは「バックアップ」ではなく「Supabase の
    自己複製」になり、正本が失われたときに何の役にも立たない。`mirror_common` の
    `guard_dest_local` と対になるガードで、あちらは書き先、こちらは読み元を見る。
    """
    if not database._is_local:
        raise SystemExit(
            "中止: バックアップ元が正本（ローカル）ではありません: "
            f"{database.mask_url(database.DATABASE_URL)}\n"
            "  FINAPP_DB_TARGET=local で実行してください（#503・正本はローカル）"
        )


def row_counts(tables: Iterable[str]) -> dict[str, int]:
    from sqlalchemy import text

    out = {}
    with database.engine.connect() as c:
        for t in tables:
            out[t] = c.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar() or 0
    return out


def dump_order(tables: Iterable[str]) -> tuple[str, ...]:
    """ダンプする順序＝**FK 依存の逆順（子が先・親が後）**。

    表ごとに別の `pg_dump` プロセスを起こすので、**各表のスナップショット時点は揃わない**。
    親（`companies`）を先に取ると、その後に追加された会社を参照する子の行が「FK 先の無い行」
    としてバックアップに入り、復元時に落ちる。子を先に取れば、後から取る親は子より新しい
    ＝子が参照する親は必ず含まれる（余分な親が入るのは無害）。

    根本解は `pg_export_snapshot` で全表に同一スナップショットを渡すことだが、別接続で
    スナップショットを保持し続ける必要があり、得られる整合性に対して仕掛けが重い。
    バックアップは夜間バッチと別の週次タスクで回す（同時に書き込みが走らない）ことと
    この順序で足りる。**restore は逆に依存順（親が先）**——`mirror_common.mirror_tables()`
    がその順を返す。
    """
    return tuple(reversed(tuple(tables)))


def dump_tables(tables: Iterable[str], out_dir: Path, echo=print) -> Generation:
    """表ごとに custom 形式でダンプする。**1表でも失敗したら世代ごと捨てる**。

    半端な世代を残すと、復元時に「入っていない表がある」ことに気づけない。ダンプの
    `--strict-names` が typo を落とすのと同じ理由で、ここでも全か無かに倒す。

    `gen.dumps` は **restore 順（FK 依存順）** で並べる。ダンプ順とは逆になる（`dump_order`）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = mc.PgConn.from_url(database.DATABASE_URL)
    tables = tuple(tables)
    counts = row_counts(tables)
    gen = Generation(stamp=out_dir.name)
    by_table: dict[str, TableDump] = {}
    for t in dump_order(tables):
        path = out_dir / f"{t}.dump"
        argv = mc.pg_dump_argv(conn, [t], str(path), compress=COMPRESS)
        proc = subprocess.run(argv, env=conn.env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise SystemExit(f"中止: {t} のダンプに失敗: {(proc.stderr or '').strip()[:200]}")
        size = path.stat().st_size
        mb = size / 1024 / 1024
        if mb > MAX_OBJECT_MB:
            raise SystemExit(
                f"中止: {t} が {mb:.1f}MB で Free プランの上限 {MAX_OBJECT_MB}MB を超えた。\n"
                "  表を期間で分割するか、プランを上げること（#503 Phase 3 の前提が崩れている）"
            )
        by_table[t] = TableDump(t, path, size, counts[t])
        echo(f"  {t:<28} {mb:>8.1f}MB  {counts[t]:>10,} 行")
    gen.dumps = [by_table[t] for t in tables]      # restore 順（FK 依存順）で持つ
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(gen.manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    return gen


def local_generations() -> list[str]:
    if not LOCAL_STORE.is_dir():
        return []
    return sorted(p.name for p in LOCAL_STORE.iterdir()
                  if p.is_dir() and (p / MANIFEST_NAME).is_file())


def generations_to_delete(stamps: list[str], keep_recent: int = KEEP_RECENT,
                          keep_monthly: int = KEEP_MONTHLY) -> list[str]:
    """保持ポリシーを適用し、**消す世代**を返す（純関数＝テストで直接検証する）。

    残すのは2種類:
      - 直近 `keep_recent` 世代（新しい順）
      - 各月の**最初**の世代を、新しい月から `keep_monthly` か月ぶん

    「各月の最初」を選ぶのは、月末に近いものより月初のほうが「その月の状態」として
    後から指定しやすいため。どちらでも良いが、**規則を決めておかないと消す順が実行ごとに
    変わる**（ORDER BY 無しの結果を最大値走査に使わない、と同じ原則）。
    """
    if not stamps:
        return []
    ordered = sorted(stamps, reverse=True)
    keep = set(ordered[:keep_recent])

    first_of_month: dict[str, str] = {}
    for s in sorted(stamps):                       # 昇順＝各月で最初に当たったものが月初
        first_of_month.setdefault(s[:6], s)
    for month in sorted(first_of_month, reverse=True)[:keep_monthly]:
        keep.add(first_of_month[month])

    return [s for s in ordered if s not in keep]


# ── Supabase Storage（REST・新規依存なし）──────────────────────────────────

class Storage:
    """Storage REST の薄いラッパ。`httpx` は既に requirements にある（supabase-py は入れない）。"""

    def __init__(self, url: str, key: str, bucket: str = BUCKET):
        import httpx

        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.client = httpx.Client(
            timeout=300.0,
            headers={"Authorization": f"Bearer {key}", "apikey": key},
        )

    @classmethod
    def from_env(cls, env=None) -> "Storage":
        # `env or os.environ` にしない——空 dict は falsy なので**本物の環境変数へ落ちる**。
        # 「認証情報が無い」を渡すテストが `.env` を読んでしまい、ローカルだけ落ちて CI では
        # 通る（`feedback_local_green_is_not_ci_green` の逆向き）。
        env = os.environ if env is None else env
        url = env.get("SUPABASE_URL")
        key = env.get("SUPABASE_SERVICE_ROLE_KEY")
        missing = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_SERVICE_ROLE_KEY", key)) if not v]
        if missing:
            raise SystemExit(
                f"中止: {', '.join(missing)} が未設定です。\n"
                "  Supabase ダッシュボード > Project Settings > API から取得し .env へ追記してください。\n"
                "  （認証情報が無くても --dest local までは検証できます）"
            )
        return cls(url, key)

    def _fail(self, what: str, r) -> None:
        """失敗を**手が動く形**で伝える。初回は必ずバケット未作成を踏むので、そこを名指しする。"""
        if r.status_code == 404 or "Bucket not found" in r.text:
            raise SystemExit(
                f"中止: バケット '{self.bucket}' が見つかりません。\n"
                "  Supabase ダッシュボード > Storage > New bucket で作成してください:\n"
                f"    名前   : {self.bucket}\n"
                "    Public : オフ（**private**。バックアップを公開しない）\n"
                "  作成後にもう一度実行してください。"
            )
        if r.status_code in (401, 403):
            raise SystemExit(
                f"中止: {what} が認証で拒否されました（{r.status_code}）。\n"
                "  SUPABASE_SERVICE_ROLE_KEY が anon key になっていないか確認してください"
                "（private バケットへの書き込みには service_role が要ります）。"
            )
        raise SystemExit(f"中止: {what}: {r.status_code} {r.text[:200]}")

    def upload(self, remote_path: str, data: bytes) -> None:
        r = self.client.post(
            f"{self.base}/object/{self.bucket}/{remote_path}",
            content=data,
            headers={"content-type": "application/octet-stream", "x-upsert": "true"},
        )
        if r.status_code >= 400:
            self._fail(f"アップロード失敗 {remote_path}", r)

    def list_prefixes(self) -> list[str]:
        """世代フォルダ名の一覧。Storage は擬似ディレクトリなので prefix を列挙する。"""
        r = self.client.post(f"{self.base}/object/list/{self.bucket}",
                             json={"prefix": "", "limit": 1000})
        if r.status_code >= 400:
            self._fail("一覧取得に失敗", r)
        return sorted({item["name"].split("/")[0] for item in r.json()})

    def download(self, remote_path: str) -> bytes:
        """1オブジェクトを取ってくる（`backup_restore --source storage` が使う）。

        **上げる側と同じクラスに置く。** 取る経路だけ別実装にすると、バケット名・認証・
        エラーの読み方が2箇所に分かれ、`_fail` が名指ししてくれる「バケット未作成」
        「anon key を渡している」がダウンロード側では出なくなる。
        """
        r = self.client.get(f"{self.base}/object/{self.bucket}/{remote_path}")
        if r.status_code >= 400:
            self._fail(f"ダウンロード失敗 {remote_path}", r)
        return r.content

    def remove_prefix(self, prefix: str, names: Iterable[str]) -> None:
        paths = [f"{prefix}/{n}" for n in names]
        r = self.client.request("DELETE", f"{self.base}/object/{self.bucket}",
                                json={"prefixes": paths})
        if r.status_code >= 400:
            self._fail(f"削除に失敗 {prefix}", r)


def push_generation(store: Storage, gen: Generation, echo=print) -> None:
    for d in gen.dumps:
        echo(f"  upload {gen.stamp}/{d.table}.dump ({d.bytes / 1024 / 1024:.1f}MB)")
        store.upload(f"{gen.stamp}/{d.table}.dump", d.path.read_bytes())
    store.upload(f"{gen.stamp}/{MANIFEST_NAME}",
                 json.dumps(gen.manifest(), ensure_ascii=False, indent=2).encode("utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="正本のバックアップを取り、Storage へ置く（#503）")
    ap.add_argument("--dest", choices=("local", "storage"), default="local",
                    help="local=ローカルに世代を作るだけ（既定） / storage=Supabase Storage へ push")
    ap.add_argument("--apply", action="store_true", help="実際に実行する（既定はドライラン）")
    ap.add_argument("--list", action="store_true", help="置いてある世代を一覧して終了")
    ap.add_argument("--tables", help="対象表をカンマ区切りで限定（既定は全17表）")
    ap.add_argument("--keep-recent", type=int, default=KEEP_RECENT)
    ap.add_argument("--keep-monthly", type=int, default=KEEP_MONTHLY)
    args = ap.parse_args(argv)

    if args.list:
        stamps = Storage.from_env().list_prefixes() if args.dest == "storage" else local_generations()
        print(f"世代（{args.dest}）: {len(stamps)} 件")
        for s in stamps:
            print(f"  {s}")
        drop = generations_to_delete(stamps, args.keep_recent, args.keep_monthly)
        print(f"保持ポリシー適用で消える世代: {drop or 'なし'}")
        return 0

    guard_source_is_primary()

    tables = mc.mirror_tables()
    if args.tables:
        picked = {t.strip() for t in args.tables.split(",") if t.strip()}
        unknown = picked - set(tables)
        if unknown:
            raise SystemExit(f"中止: 対象外のテーブル {sorted(unknown)}（対象は {tables}）")
        tables = tuple(t for t in tables if t in picked)

    stamp = new_stamp()
    print(f"source : {database.mask_url(database.DATABASE_URL)}")
    print(f"dest   : {args.dest}")
    print(f"世代   : {stamp}（{len(tables)} 表・compress={COMPRESS}）")

    if not args.apply:
        existing = local_generations()
        print("\n実行する操作:")
        print(f"  1. {len(tables)} 表を .backups/{stamp}/ へ custom 形式でダンプ")
        if args.dest == "storage":
            print(f"  2. Storage バケット '{BUCKET}' の {stamp}/ へアップロード")
        print(f"  3. 保持ポリシー（直近 {args.keep_recent} ＋ 月次 {args.keep_monthly}）で古い世代を削除")
        print(f"\n既存のローカル世代: {existing or 'なし'}")
        print("ドライラン（何も変更していない）。実行するには --apply を付けてください。")
        return 0

    out_dir = LOCAL_STORE / stamp
    print("\n[dump] 表ごとにダンプしています...")
    gen = dump_tables(tables, out_dir)
    total_mb = gen.total_bytes / 1024 / 1024
    print(f"[dump] 完了: 合計 {total_mb:.1f}MB")

    if args.dest == "storage":
        store = Storage.from_env()
        remote = store.list_prefixes()
        projected = (len(remote) + 1) * total_mb
        if projected > MAX_TOTAL_MB:
            print(f"[warn] 世代を足すと約 {projected:.0f}MB で 1GB 枠に近づきます"
                  f"（保持ポリシーの削除は下で実行されます）")
        print("[push] Storage へアップロードしています...")
        push_generation(store, gen)
        drop = generations_to_delete(store.list_prefixes(), args.keep_recent, args.keep_monthly)
        for s in drop:
            names = [f"{t}.dump" for t in mc.mirror_tables()] + [MANIFEST_NAME]
            store.remove_prefix(s, names)
            print(f"[retain] 削除: {s}")
    else:
        drop = generations_to_delete(local_generations(), args.keep_recent, args.keep_monthly)
        for s in drop:
            for p in sorted((LOCAL_STORE / s).iterdir()):
                p.unlink()
            (LOCAL_STORE / s).rmdir()
            print(f"[retain] 削除: {s}")

    print(f"\n完了: 世代 {stamp}（{total_mb:.1f}MB / {len(tables)} 表）")
    print("  復元手順は docs/DEPLOYMENT.md の「バックアップからの復元」を参照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
