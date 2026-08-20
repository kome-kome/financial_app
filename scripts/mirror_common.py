"""ローカルミラー3本の共有基盤（Issue #481 B-2〜B-4）。

## なぜ必要か

Supabase が Egress 超過で restricted になるとアプリも分析も一切動かせない（2026-07・2026-08 に
2回発生し、2回目は8日間まるごと停止した）。#481 はローカル PostgreSQL に読取レプリカを置いて
「障害時の継続」と「検証反復の Egress ゼロ化」を同時に解決する。B-0（器の作成）と
B-1（接続先スイッチ `FINAPP_DB_TARGET`）は済んでおり、残るのがデータの投入と同期＝
`mirror_pull` / `mirror_sync` / `mirror_verify` の3本である。本モジュールはその共有基盤。

## 現在の位置づけ（2026-08-20・#503 / ADR-0038）

**このレプリカは正本へ昇格した。** 2026-08-20 の最終フル pull（17表・152.8MB・checksum
16表一致）を引き渡し点として、収集も分析もローカルで完結する。したがって:

- `mirror_pull` / `mirror_sync` は **定常運転では使わない**（Supabase から引く理由が無い）。
  正本を Supabase へ戻す決定をしたときのために残してある。
- `mirror_verify` は**引き続き使う**。エンドポイントを引数で受けるので、バックアップの
  復元先とローカル正本の突合に転用できる。
- `guard_dest_local()` の向きは**変わらない**（理由は入れ替わったが結論は同じ）。
- バックアップは `scripts/backup_push.py` / `backup_restore.py` が担い、置き場は Supabase の
  Postgres ではなく **Storage**。だからこのモジュール群は今も本番へ書く経路を持たない。

## 設計の中心: エンドポイントを引数化する

3本とも source / dest を引数で受け、**両方をローカルへ向ければ予行演習になる**。

| 実行 | source | dest | Egress |
|---|---|---|---|
| 予行 | `financial_db_rehearsal_src` | `financial_db_rehearsal_dst` | 0 |
| 本番 | Supabase | `financial_db` | 実測 |

こうしておくと **Supabase へ触れる箇所が「URL 文字列1つ」に閉じる**ので、
Egress 枠が復旧する 2026-08-18 を待たずに、本番と同一のコードパスを今日検証できる。
`--source-url` / `--dest-url` は運用上も正当な引数であり、テスト専用のスキーマ引数のような
「本番では絶対に通らない分岐」をコードへ持ち込まずに済む。

## 3つの安全装置

1. **dest はローカル限定**（`guard_dest_local`）。本スクリプト群は**本番へ書く経路を持たない**。
   `scripts/setup_local_db.py::guard_local()` と同型だが、あちらが「自分の接続先」を見るのに対し
   こちらは「書き込み先エンドポイント」を見る。
2. **パスワードは argv に載せない**。`PGPASSWORD` 環境変数で渡す（argv はプロセス一覧から
   他ユーザーに見える）。例外メッセージも `database.mask_url` でマスクする。
3. **転送量は事前に見積もる**。`pg_dump` は SQLAlchemy を通らないため
   `db_egress` のサーキットブレーカが**途中で止められない**。歯止めは
   `estimate_bytes()` による事前見積り＋ `--allow-full-pull` ゲートで、
   `LEDGER.record_external()` は事後の記帳にすぎない。

## テーブル順序と FK

ミラー範囲は全18表から `xbrl_raw_documents`（BLOB・0行）を除いた17表。FK は4本ともに
`companies.edinet_code`
向きなので、`Base.metadata.sorted_tables`（依存順）に従えば `companies` が依存側より先に来る。
**`edinet` は superuser でないため `pg_restore --disable-triggers` が使えず**、順序で満たすしかない。
並列 `--jobs` は順序が崩れるので使わない。

実行: 本モジュールは直接実行しない（`mirror_pull` / `mirror_sync` / `mirror_verify` が import する）。
出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional
from urllib.parse import unquote, urlsplit

from sqlalchemy import text

import database
from database import (
    DAILY_WINDOW_DAYS, WEEKLY_OVERLAP_DAYS, WEEKLY_OVERLAP_WEEKS, Base, mask_url,
)

# ── ミラー範囲 ────────────────────────────────────────────────────────────────
# xbrl_raw_documents: gzip BLOB。分析経路は一切読まない（財務は financial_records 側）。
#   #219 で raw_xbrl_json を落として以降 0 行のままで、再取得元は EDINET にある。
#
# stock_price_daily は 2026-08-20 に**除外から外した**。「183日ローリングで再構成できるから
# 300MB 枠を割く価値がない」は **Supabase が正本だったとき**の理屈であって、正本がローカルへ
# 移った後は daily はローカルにしか無い正本データになる（ADR-0038）。しかも
# `_recompute_weeks_from_daily` の入力かつ gap-fill の基準なので、空のまま収集を始めると
# 全社が183日窓を Yahoo から引き直すことになる。
MIRROR_EXCLUDED = ("xbrl_raw_documents",)

# ── 同期モード ────────────────────────────────────────────────────────────────
MODE_FULL = "FULL"            # 全置換（DELETE -> INSERT）。総入替か行数極小の表
MODE_WATERMARK = "WATERMARK"  # 高水位 - overlap 以降だけ取得して upsert

# 週次のオーバーラップ（27週＝189日）は **`database.py` で `DAILY_WINDOW_DAYS` から導出**する。
# 根拠（`_recompute_weeks_from_daily` が保持窓ぶん遡って上書きする）が database 側にあるため、
# 定義もそこに置き、ここは再エクスポートに留める。#480 の週次価格キャッシュも同じものを使う
# ＝導出は1箇所・消費が2箇所。ここで再計算すると「片方だけスラックを足す」変更で黙って乖離する。
# 旧: WEEKLY_OVERLAP_WEEKS = math.ceil(DAILY_WINDOW_DAYS / 7)  →  database.py へ移動（#480）

# `macro_data` / `statement_disclosure` の `created_at` は upsert の set_ に含まれず、
# **値だけが訂正されたときに進まない**。日付列の高水位に頼るしかないので窓を広めに取る。
DATED_OVERLAP_DAYS = 90


@dataclass(frozen=True)
class TableSync:
    """1テーブルぶんの同期方針。`note` に根拠を持たせ、出所不明の設定を作らない。

    `db_egress.EgressCost` が `measured_on` / `source_issue` を必須にしているのと同じ作法。
    """
    mode: str
    key: Optional[str] = None       # WATERMARK のときの単調増加列
    overlap_days: int = 0           # 高水位から遡って無条件に取り直す日数
    extra_where: Optional[str] = None   # overlap とは別に必ず取り直す条件（OR で足す）
    note: str = ""


# 増分キーの信頼度は3段階に割れている（2026-08-16 に database.py の ORM 定義で確認）。
#   ◎ onupdate 付き  = 値の訂正まで検出できる（companies / financial_records / regression_results）
#   ○ 追記型 created_at = run 単位で積むだけなので追記は拾えるが訂正は拾えない
#   × 時刻列が無い / upsert で進まない = 日付列の高水位＋オーバーラップで代替する
SYNC_PLAN: dict[str, TableSync] = {
    # ◎ onupdate 付き
    "companies": TableSync(
        MODE_WATERMARK, "updated_at", 1,
        note="updated_at は onupdate 付き＝上場廃止・業種変更などの訂正も高水位で拾える"),
    "financial_records": TableSync(
        MODE_WATERMARK, "updated_at", 1,
        note="updated_at は onupdate 付き。period_end は決算期であって更新時刻ではなく、"
             "過去期の訂正提出を取り落とすので使わない（Issue #481 当初案の誤り）"),
    "regression_results": TableSync(
        MODE_WATERMARK, "computed_at", 1,
        note="computed_at は onupdate 付き"),

    # ○ 追記型 created_at（run_id 単位で積む）
    "macro_beta_loadings": TableSync(
        MODE_WATERMARK, "created_at", 1, note="run 単位の追記のみ。既存行の更新は無い"),
    "macro_beta_meta": TableSync(
        MODE_WATERMARK, "created_at", 1, note="同上"),
    "recommend_factor_premia": TableSync(
        MODE_WATERMARK, "computed_at", 1,
        note="run_id 単位の追記。computed_at は onupdate 無しだが既存行を更新しない"),
    "collection_logs": TableSync(
        MODE_WATERMARK, "started_at", 1, extra_where="finished_at IS NULL",
        note="running -> done の後追い UPDATE があるため、未完了行は高水位に関わらず毎回取り直す"),

    # × 時刻列が無い / upsert で進まない
    "stock_price_daily": TableSync(
        MODE_WATERMARK, "trade_date", DAILY_WINDOW_DAYS,
        note=f"時刻列を持たない。表そのものが直近 {DAILY_WINDOW_DAYS} 日のローリング窓で、"
             f"Yahoo の遡及調整（分割）は窓全体を書き換えうるので overlap も窓と同じ"
             f"{DAILY_WINDOW_DAYS}日＝実質全件になる。取り落とすより取り直す方を選ぶ"),
    "stock_price_weekly": TableSync(
        MODE_WATERMARK, "week_start", WEEKLY_OVERLAP_DAYS,
        note=f"時刻列を持たない。_recompute_weeks_from_daily が最大 DAILY_WINDOW_DAYS"
             f"={DAILY_WINDOW_DAYS} 日遡って上書きするため overlap は "
             f"{WEEKLY_OVERLAP_WEEKS} 週={WEEKLY_OVERLAP_DAYS} 日"),
    "macro_data": TableSync(
        MODE_WATERMARK, "trade_date", DATED_OVERLAP_DAYS,
        note="created_at は upsert_macro_batch の set_ に含まれず値の訂正で進まない"),
    "statement_disclosure": TableSync(
        MODE_WATERMARK, "disc_date", DATED_OVERLAP_DAYS,
        note="created_at は upsert_statement_disclosures の set_ から除外されている"),

    # 全置換（edinet_code 単独 PK の総入替。replace_macro_*_scores が DELETE -> INSERT する）
    "macro_dlm_scores": TableSync(MODE_FULL, note="全置換方式。増分キーが意味を持たない"),
    "macro_enet_scores": TableSync(MODE_FULL, note="同上"),
    "macro_ensemble_scores": TableSync(MODE_FULL, note="同上"),
    "macro_gbdt_scores": TableSync(MODE_FULL, note="同上"),

    # 行数極小
    "plugin_tuned_params": TableSync(MODE_FULL, note="plugin ごとに1行。全件でも数十行"),
    "app_settings": TableSync(MODE_FULL, note="key ごとに1行"),
}


def mirror_tables() -> tuple[str, ...]:
    """ミラー対象を **FK 依存順** で返す（親が先）。

    手書きリストにしない。テーブルを足したときに黙って漏れるのを防ぐため、
    `Base.metadata.sorted_tables`（SQLAlchemy が FK から算出する位相順）を唯一の源にする。
    `companies` は4本の FK の参照先なので必ず依存側より前に来る＝
    `pg_restore --disable-triggers` が使えない（`edinet` が非 superuser）環境でも
    この順に単一スレッドで流せば FK を満たせる。
    """
    return tuple(t.name for t in Base.metadata.sorted_tables
                 if t.name not in MIRROR_EXCLUDED)


def truncate_targets(tables: Iterable[str]) -> tuple[str, ...]:
    """TRUNCATE に含める必要がある表を**明示列挙**して返す（`CASCADE` は使わない）。

    `companies` を空にするには、それを参照する表もすべて同時に TRUNCATE される必要がある。
    `CASCADE` で済ませると **「ミラー範囲外の表が黙って消える」ことがコードから読めなくなる**
    ので、メタデータから機械的に洗い出して名前で並べる。

    `stock_price_daily` をミラー範囲へ入れた（2026-08-20）ことで、現在この関数が足す
    範囲外の表は**無い**（残る除外 `xbrl_raw_documents` は FK を持たない）。それでも関数を
    残すのは、FK 子を持つ表を将来また範囲外にしたときに黙って TRUNCATE が失敗するのを
    防ぐため＝ミラー範囲が FK 閉包であることの検査点でもある。
    """
    picked = list(tables)
    targets = list(picked)
    for name, tbl in Base.metadata.tables.items():
        if name in targets:
            continue
        for col in tbl.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name in picked:
                    targets.append(name)
                    break
            else:
                continue
            break
    return tuple(targets)


def primary_key_columns(table: str) -> tuple[str, ...]:
    return tuple(c.name for c in Base.metadata.tables[table].primary_key)


def table_columns(table: str) -> tuple[str, ...]:
    return tuple(c.name for c in Base.metadata.tables[table].columns)


def key_is_datetime(table: str, key: str) -> bool:
    """高水位列が DateTime か（True）、ISO 日付文字列 / Date か（False）。

    ORM の型定義から導出する。`week_start` / `trade_date` / `disc_date` は String(10) だが
    ISO 形式なので**辞書順＝時系列順**が成り立ち、文字列のまま比較してよい。
    """
    col = Base.metadata.tables[table].columns[key]
    return col.type.__class__.__name__ == "DateTime"


def since_value(table: str, key: str, watermark, overlap_days: int):
    """高水位から overlap_days 遡った「ここ以降を取り直す」境界値を返す。

    watermark が None（dest が空）なら None を返し、呼び出し側は全件取得へ倒す。
    """
    if watermark is None:
        return None
    if key_is_datetime(table, key):
        base = watermark if isinstance(watermark, datetime) else datetime.fromisoformat(str(watermark))
        return base - timedelta(days=overlap_days)
    # ISO 日付文字列 / Date
    if isinstance(watermark, (date, datetime)):
        base_d = watermark.date() if isinstance(watermark, datetime) else watermark
    else:
        base_d = date.fromisoformat(str(watermark)[:10])
    shifted = base_d - timedelta(days=overlap_days)
    col = Base.metadata.tables[table].columns[key]
    return shifted if col.type.__class__.__name__ == "Date" else shifted.isoformat()


# ── エンドポイント解決 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Endpoint:
    """接続先1つぶん。`url` は生の接続文字列、`label` は表示用（パスワードを含まない）。"""
    spec: str
    url: str

    @property
    def label(self) -> str:
        return f"{self.spec} [{mask_url(self.url)}]"

    @property
    def dbname(self) -> str:
        return urlsplit(self.url).path.lstrip("/") or "?"

    @property
    def is_local(self) -> bool:
        return database._looks_local(self.url)


def resolve_endpoint(spec: str, env: Optional[Mapping[str, str]] = None) -> Endpoint:
    """`prod` / `local` / 生の接続 URL を Endpoint へ解決する。

    `prod` / `local` は `database.resolve_database_url()`（副作用のない純関数）へ委譲する。
    解決ロジックを二重に持つと、片方だけ直したときに「ローカルのつもりで本番」が復活する。
    """
    env = dict(os.environ if env is None else env)
    spec = (spec or "").strip()
    if spec.startswith("postgres://") or spec.startswith("postgresql://"):
        url = spec.replace("postgres://", "postgresql://", 1)
        return Endpoint("url", url)
    env["FINAPP_DB_TARGET"] = spec.lower()
    _, url = database.resolve_database_url(env)
    return Endpoint(spec.lower(), url)


def make_engine(ep: Endpoint):
    """エンドポイント専用の engine。**`database.engine`（プロセス既定）とは別物**。

    ミラーは2つの接続先を同時に持つので、モジュールグローバルの engine は使えない。
    `db_egress.install()` を必ず掛ける——source が Supabase のときの SELECT を台帳へ載せる
    ためで、これを忘れると「誰が Egress を食ったか」がまた答えられなくなる（ADR-0034）。

    **`_SESSION_FIXES` は接続時フックで自動適用する。** 以前は `table_stats()` の中でしか
    呼んでおらず、`mirror_sync` の `fetch_rows()` は既定のまま source を読んでいた。その結果
    **float8 が有効数字15桁へ丸められ、sync した行だけ値が劣化していた**（2026-08-19 実測:
    `mean_b` が 0.014484329376308225 → 0.0144843293763082）。float8 の完全往復には17桁＝
    `extra_float_digits >= 1` が要る。`pull` が無事だったのは `pg_dump` が自前で設定するため
    で、**経路ごとに正しさが違う状態だった**。セッション設定は「読む人が思い出す」ものでは
    なく「接続に付いてくる」ものにする。
    """
    from sqlalchemy import create_engine, event
    import db_egress

    connect_args = {} if ep.is_local else {"sslmode": "require"}
    eng = create_engine(ep.url, pool_pre_ping=True, pool_recycle=180,
                        connect_args=connect_args, echo=False)

    @event.listens_for(eng, "connect")
    def _apply_session_fixes(dbapi_conn, _record):      # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            for sql in _SESSION_FIXES:
                cur.execute(sql)
        finally:
            cur.close()

    db_egress.install(eng)
    return eng


def guard_dest_local(dest: Endpoint) -> None:
    """書き込み先がローカルでなければ止める。**このモジュール群唯一の不可逆性の歯止め**。

    **#503 で正本がローカルへ反転した後も、このガードの向きは変わらない。** 反転前は
    「ミラーは Supabase を正本とする読取レプリカだから逆向きに書く用途が無い」が理由で、
    反転後は「Supabase は 2026-08-07 の断面で凍結してあり、書けば正本と分岐する」が理由。
    **理由が入れ替わっても結論は同じ**——このコード群は本番 DB へ書く経路を持たない
    （バックアップは Postgres ではなく Storage へ置く・ADR-0038）。
    """
    if not dest.is_local:
        raise SystemExit(
            "中止: 書き込み先がローカルではありません: " + mask_url(dest.url) + "\n"
            "  正本はローカル PostgreSQL です（#503）。Supabase は凍結した閲覧用の断面で、\n"
            "  そこへ書き戻す経路はコードとして持ちません。\n"
            "  --dest local か、localhost / 127.0.0.1 の --dest-url を指定してください。"
        )


# ── PostgreSQL クライアント（pg_dump / pg_restore / psql）─────────────────────

DEFAULT_PG_BIN = r"C:\Program Files\PostgreSQL\18\bin"


def pg_bin(name: str) -> str:
    """`pg_dump` 等のフルパス。**PATH には無い**（2026-08-15 実測）ので明示的に解決する。"""
    root = os.environ.get("FINAPP_PG_BIN") or DEFAULT_PG_BIN
    exe = name + (".exe" if os.name == "nt" else "")
    path = Path(root) / exe
    if path.exists():
        return str(path)
    return name          # PATH 上にあればそれを使う（Linux/CI 想定）


@dataclass(frozen=True)
class PgConn:
    """接続 URL を pg_* コマンドの引数へ分解したもの。**パスワードは argv に載せない**。"""
    host: str
    port: str
    user: str
    password: str
    dbname: str
    sslmode: Optional[str]

    @classmethod
    def from_url(cls, url: str) -> "PgConn":
        p = urlsplit(url)
        q = dict(kv.split("=", 1) for kv in p.query.split("&") if "=" in kv)
        host = p.hostname or "localhost"
        sslmode = q.get("sslmode") or (None if database._looks_local(url) else "require")
        return cls(
            host=host,
            port=str(p.port or 5432),
            user=unquote(p.username or ""),
            password=unquote(p.password or ""),
            dbname=p.path.lstrip("/"),
            sslmode=sslmode,
        )

    def conn_argv(self) -> list[str]:
        """接続系の argv。**`-w` でプロンプトを禁じる**（無いと認証失敗時に無言でハングする）。

        接続文字列を `--dbname=postgresql://user:pw@...` として1本で渡すこともできるが、
        **argv はプロセス一覧から他ユーザーに見える**。分解して渡し、パスワードは env へ。
        """
        return ["--host", self.host, "--port", self.port,
                "--username", self.user, "--dbname", self.dbname, "-w"]

    def env(self) -> dict[str, str]:
        env = {**os.environ, "PGPASSWORD": self.password}
        if self.sslmode:
            env["PGSSLMODE"] = self.sslmode
        return env


def pg_dump_argv(conn: PgConn, tables: Iterable[str], out_path: str,
                 compress: int = 0) -> list[str]:
    """データのみの custom 形式ダンプ。**純関数**（テストで argv をそのまま検証する）。

    フラグの根拠（いずれも 2026-08-16 にローカル PG18.6 で実測）:

    - **`--strict-names` は必須**。無いと「16 表のうち 1 本だけ綴りを間違えた」ケースが
      **exit 0 で通り、その表だけ入っていないダンプができる**（実測: 正しい表1本＋typo 1本で
      exit=0・1,433 バイトのダンプが生成された）。8/18 の一回きりの実行で無言で通る事故になる。
    - **`-w`**（パスワードプロンプト禁止）。無いと認証失敗時に子プロセスが標準入力待ちで
      黙ってハングする。
    - **`compress` の既定 0 は意図的**（ミラーの pull 用）。custom 形式は既定でローカル側
      zlib 圧縮するが、**ワイヤ上を流れるのは非圧縮の COPY ストリーム**である（libpq の
      通信圧縮は既定 OFF）。圧縮したままだと出来上がったファイルのサイズが実 Egress を
      大幅に過小申告し、`LEDGER.record_external()` に嘘の数字が載る。
      **逆にバックアップ（#503 Phase 3）では圧縮する**——測りたいのが転送量ではなく
      保管サイズであり、Supabase Storage は 50MB/ファイル・1GB/プロジェクトだから。
      用途で正解が逆になるので、既定を持たせつつ引数で選べる形にしてある。
    - `--no-owner` / `--no-acl`: Supabase の `postgres` ロールがローカルに存在しない。
    - `--lock-wait-timeout=30s`: Supabase 側の DDL とかち合ったとき無限に待たない。
    """
    if not 0 <= compress <= 9:
        raise ValueError(f"compress は 0-9: {compress}")
    argv = [pg_bin("pg_dump"), *conn.conn_argv(),
            "--strict-names", "--format=custom", "--data-only", f"--compress={compress}",
            "--no-owner", "--no-acl", "--no-large-objects",
            "--lock-wait-timeout=30s", "--file", out_path]
    argv += [f"--table=public.{t}" for t in tables]
    return argv


def pg_restore_argv(conn: PgConn, dump_path: str, table: str) -> list[str]:
    """**1 表ぶん**の restore。`--single-transaction` で全か無か。`--jobs` は使わない。

    表を1本ずつ流すのは、**ダンプの TOC が `--table` の指定順ではなくアルファベット順**
    だからである（2026-08-16 実測: `stock_price_weekly, financial_records, companies,
    statement_disclosure` の順で指定しても TOC は companies -> financial_records ->
    statement_disclosure -> stock_price_weekly）。今たまたま FK を満たせているのは
    `companies` の頭文字が c で子表より先に来るという**偶然**にすぎず、表名が変われば黙って壊れる。

    `edinet` は superuser でないため `pg_restore --disable-triggers` で FK を一時停止できず、
    順序だけが FK を満たす手段になっている。**偶然に依存させない。**
    """
    return [pg_bin("pg_restore"), *conn.conn_argv(),
            "--data-only", "--single-transaction", "--no-owner", "--no-acl",
            f"--table={table}", dump_path]


def decode_pg_output(raw: bytes) -> str:
    """pg_* の出力を decode する。**utf-8 -> cp932 の順に試す**。

    Windows では出力に2種類のエンコーディングが混ざりうる（2026-08-16 実測）:

    - pg_dump 自身のメッセージ … ASCII 英語
    - **サーバから返る FATAL** … サーバの `lc_messages`（`Japanese_Japan.932`）そのままの
      cp932 バイト列。**認証失敗は client_encoding のネゴシエーション前に起きるので
      `PGCLIENTENCODING=UTF8` を立てても直らない**

    そのため `text=True, encoding="utf-8", errors="replace"` では日本語 FATAL が
    U+FFFD の羅列に潰れ、「パスワード認証に失敗しました」が読めなくなる。
    バイトのまま受けて順に試すと両方そのまま読める。
    """
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_pg(argv: list[str], conn: PgConn, *, what: str) -> str:
    """pg_* を実行し stderr を返す。失敗したら末尾を添えて SystemExit。"""
    proc = subprocess.run(argv, env=conn.env(), capture_output=True)
    err = decode_pg_output(proc.stderr or b"").strip()
    if proc.returncode != 0:
        tail = err.splitlines()[-8:]
        raise SystemExit(
            f"{what} に失敗しました (exit={proc.returncode})\n"
            + "\n".join("  " + mask_url(line) for line in tail)
        )
    return err


# ── サーバ側の計測（Egress ほぼ 0）───────────────────────────────────────────

# 行を text へ落とす表現は**セッション設定に依存する**。両端で揃えないと、値が同一でも
# チェックサムが恒常的に食い違い「常に赤い＝誰も見なくなる」という最悪の劣化になる。
# 実測（2026-08-16）: ローカルは TimeZone=Asia/Tokyo / extra_float_digits=1 / DateStyle='ISO, YMD'、
# Supabase はほぼ確実に TimeZone=UTC。timestamptz を持つのは app_settings.updated_at。
_SESSION_FIXES = (
    "SET TimeZone = 'UTC'",
    "SET DateStyle = 'ISO, YMD'",
    "SET extra_float_digits = 1",
)

# 行順にも**列順にも**依存しないチェックサム。
#
# 行順: `string_agg(... ORDER BY pk)` は 1.28M 行でソートと巨大な文字列連結が要るうえ、
# **照合順の違い**（ローカルは Japanese_Japan.932）で両端がずれる。md5 の先頭 32bit を
# 整数化して足し込めばストリームで済み順序も無関係。
#
# 列順: **`x::text`（行全体のテキスト化）を使ってはいけない**——これは attnum 順なので、
# 両端でテーブルの物理列順が違うと値が完全に一致していてもずれる。2026-08-19 の初回 pull で
# 実際に踏んだ: source は `ALTER TABLE ADD COLUMN` の積み重ね、mirror は
# `Base.metadata.create_all` 由来で、**16 表中 7 表が「行数 +0 なのに ck が NG」**になった
# （`companies` は created_at/updated_at と issued_shares 以降が入れ替わっていた）。
# 列名でソートして明示連結すれば定義順に依存しない。
#
# **この失敗の形は下の `_SESSION_FIXES` が警戒していたものと同じ**（値が同一なのに恒常的に
# 食い違い「常に赤い＝誰も見なくなる」）。TimeZone / DateStyle / float 桁は揃えたのに、
# 列順という軸だけ見落としていた。表現に依存する軸は1つ残らず潰すこと。
#
# NULL: `concat_ws` は NULL 引数を**黙って飛ばす**ので `(a, NULL, c)` と `(a, c)` が同じ
# 文字列になる。`\N`（COPY の NULL 表現）へ落としてから連結する。
def checksum_expr(columns: Iterable[str]) -> str:
    """列の物理順に依存しない行チェックサム式。列名は呼び出し側が渡す。"""
    names = sorted(columns)
    if not names:
        raise ValueError(
            "チェックサムの対象列が空です（schema_columns の取得に失敗した可能性）")
    parts = ", ".join(f"""coalesce("{c}"::text, '\\N')""" for c in names)
    return (f"coalesce(sum(('x' || substr(md5(concat_ws('|', {parts})), 1, 8))"
            "::bit(32)::bigint), 0)")


def fix_session(conn) -> None:
    """両端で text 表現を揃える。engine はミラー専用で最後に dispose するので session 単位でよい。"""
    for sql in _SESSION_FIXES:
        conn.execute(text(sql))


def table_stats(conn, tables: Iterable[str], *, with_bytes: bool = False,
                with_checksum: bool = False) -> dict[str, dict]:
    """テーブルごとの件数（と任意でバイト数・チェックサム）。**返るのは表ごとに1行だけ**。

    バイト数はサーバ側 `sum(octet_length(t::text))` で測る。psycopg2 はテキストプロトコルなので
    これが実転送量の近似になる（#446 が確立した測り方）。行そのものは1行も転送しない。

    チェックサムは **件数と最新キーだけでは検出できない「過去行の値だけの訂正」** を拾うため
    のもの（#465 の分割段差修復がまさにこの形）。どちらもサーバ側の全表スキャンになるので、
    Supabase では `statement_timeout=2min` に注意（ADR-0032）。
    """
    fix_session(conn)
    tables = list(tables)
    # チェックサムは列名が要る（列順に依存させないため）。メタデータ照会1回でまとめて引く
    colmap = schema_columns(conn, tables) if with_checksum else {}
    out: dict[str, dict] = {}
    for t in tables:
        sync = SYNC_PLAN.get(t)
        cols = ["count(*) AS n"]
        if with_bytes:
            cols.append("coalesce(sum(octet_length(x::text)), 0) AS nbytes")
        if with_checksum:
            cols.append(f"{checksum_expr(colmap.get(t, {}))} AS ck")
        if sync and sync.key:
            cols.append(f'max("{sync.key}") AS hi')
            cols.append(f'min("{sync.key}") AS lo')
        row = conn.execute(text(
            f'SELECT {", ".join(cols)} FROM public."{t}" AS x'
        )).mappings().first()
        out[t] = dict(row) if row else {}
    return out


def schema_columns(conn, tables: Iterable[str]) -> dict[str, dict[str, str]]:
    """表ごとの {列名: データ型}。**pull の事前確認に使う**（数百行＝Egress ほぼ 0）。

    `pg_dump --data-only` は source の列リストで `COPY t (a,b,c)` を吐く。
    - dest に無い列が source にある -> restore がエラーで落ちる（気づける）
    - **dest にしか無い列は黙って NULL のまま残る**（気づけない・こちらが危険）
    したがって転送する前に列集合を突き合わせる。
    """
    rows = conn.execute(text(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name = ANY(:names)"
    ), {"names": list(tables)}).fetchall()
    out: dict[str, dict[str, str]] = {t: {} for t in tables}
    for tname, cname, dtype in rows:
        out.setdefault(tname, {})[cname] = dtype
    return out


def watermark(conn, table: str) -> Optional[object]:
    """dest 側の高水位。ローカルを読むので Egress は発生しない。"""
    sync = SYNC_PLAN[table]
    if not sync.key:
        return None
    return conn.execute(text(
        f'SELECT max("{sync.key}") FROM public."{table}"'
    )).scalar()


def resync_sequences(conn, tables: Iterable[str]) -> list[str]:
    """serial 列のシーケンスを max(id) へ合わせる。

    ミラーは `id` ごと複製するため、dest のシーケンスは 1 のまま取り残される。
    ローカル接続中の書き込みは許す設計（#481 B-1）なので、
    **これをやらないと最初のローカル INSERT が主キー重複で落ちる**。
    """
    fixed = []
    for t in tables:
        for col in Base.metadata.tables[t].primary_key:
            # serial かどうかは ORM の autoincrement（既定は文字列 "auto"）では判定できない。
            # サーバに聞く。serial でなければ NULL が返るので、その列は黙って飛ばす。
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                               {"t": f"public.{t}", "c": col.name}).scalar()
            if not seq:
                continue
            conn.execute(text(
                f'SELECT setval(:seq, coalesce((SELECT max("{col.name}") '
                f'FROM public."{t}"), 1), true)'
            ), {"seq": seq})
            fixed.append(f"{t}.{col.name}")
    return fixed


# ── 表示ヘルパ ────────────────────────────────────────────────────────────────

def mb(n_bytes: float) -> str:
    return f"{n_bytes / (1024 * 1024):.1f}MB"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def print_endpoints(source: Endpoint, dest: Endpoint) -> None:
    print(f"source: {source.label}")
    print(f"dest  : {dest.label}")
