"""ミラー3本（Issue #481 B-2〜B-4）の純関数とメタ検査。**DB もバイナリも要らない**。

守る不変条件:

1. **ミラー範囲がメタデータと一致する** — テーブルを追加した人が同期方針を書き忘れたら CI で落ちる。
   「登録し忘れは失敗として現れないので通知では拾えない」型の乖離
   （ADR-0031 の `HEAVY_AUTOMATION` と同型）を、ここで失敗に変える。
2. **FK 依存順が偶然でなく保証されている** — `edinet` は superuser でないため
   `pg_restore --disable-triggers` が使えず、順序だけが FK を満たす手段になっている。
3. **argv に危険な組み合わせが混ざらない** — `--strict-names` が抜けると綴り誤りが無言で通り、
   `--jobs` が入ると restore 順序が崩れる。
4. **パスワードが argv に載らない** — argv はプロセス一覧から他ユーザーに見える。
5. **書き込み先がローカル以外なら止まる** — ミラーが本番へ書く経路を持たないことの担保。

実行: pytest tests/test_mirror_common.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import DAILY_WINDOW_DAYS, Base  # noqa: E402
from scripts import mirror_common as mc  # noqa: E402


# ── 1. ミラー範囲とメタデータの一致 ──────────────────────────────────────────

class TestMirrorScope:
    def test_covers_every_table_exactly_once(self):
        """全テーブル = ミラー対象 + 除外。**新テーブルを足したらここで落ちる。**"""
        assert set(mc.mirror_tables()) | set(mc.MIRROR_EXCLUDED) == set(Base.metadata.tables)
        assert not set(mc.mirror_tables()) & set(mc.MIRROR_EXCLUDED)

    def test_excluded_tables_exist(self):
        for t in mc.MIRROR_EXCLUDED:
            assert t in Base.metadata.tables, f"除外指定 {t} が実在しない（改名？）"

    def test_sync_plan_covers_all_mirror_tables(self):
        """同期方針の登録漏れ・余分をどちらも落とす。"""
        assert set(mc.SYNC_PLAN) == set(mc.mirror_tables())

    def test_every_plan_has_a_reason(self):
        """`note` 必須（db_egress.EgressCost が measured_on を必須にするのと同じ作法）。"""
        for name, spec in mc.SYNC_PLAN.items():
            assert spec.note.strip(), f"{name} の同期方針に根拠が書かれていない"

    def test_watermark_keys_exist_on_the_table(self):
        """高水位列が実在すること。列名を変えたらここで落ちる。"""
        for name, spec in mc.SYNC_PLAN.items():
            if spec.mode == mc.MODE_WATERMARK:
                assert spec.key, f"{name} は WATERMARK なのに key が無い"
                assert spec.key in mc.table_columns(name), \
                    f"{name}.{spec.key} が実在しない"
            else:
                assert spec.mode == mc.MODE_FULL

    def test_full_mode_tables_are_not_fk_parents(self):
        """FULL は DELETE 全消しなので、FK に参照される表には使えない。"""
        parents = {fk.column.table.name
                   for t in Base.metadata.tables.values()
                   for c in t.columns for fk in c.foreign_keys}
        for name, spec in mc.SYNC_PLAN.items():
            if spec.mode == mc.MODE_FULL:
                assert name not in parents, f"{name} は FK 参照先なので FULL にできない"


# ── 2. FK 依存順 ──────────────────────────────────────────────────────────────

class TestOrdering:
    def test_parents_precede_children(self):
        """親テーブルが必ず子より前に来る（アルファベット順の偶然に依存しない）。"""
        order = {t: i for i, t in enumerate(mc.mirror_tables())}
        for name in mc.mirror_tables():
            for col in Base.metadata.tables[name].columns:
                for fk in col.foreign_keys:
                    parent = fk.column.table.name
                    if parent in order:
                        assert order[parent] < order[name], \
                            f"{parent} が {name} より後ろにある（restore で FK 違反）"

    def test_truncate_targets_include_out_of_scope_children(self):
        """ミラー範囲の表を参照する FK 子は、範囲外でも TRUNCATE 対象に入る必要がある。

        `CASCADE` に頼らず明示列挙しているので、対象が漏れると TRUNCATE 自体が失敗する。
        **表名を直書きせずメタデータから導出する**——旧版は `stock_price_daily` を
        直書きしていたが、2026-08-20 にその表をミラー範囲へ入れた時点で検査の意図
        （範囲が FK 閉包であること）と文面が食い違った。
        """
        tables = set(mc.mirror_tables())
        targets = set(mc.truncate_targets(mc.mirror_tables()))
        assert tables <= targets
        children = {t.name for t in Base.metadata.tables.values()
                    for c in t.columns for fk in c.foreign_keys
                    if fk.column.table.name in tables}
        assert children <= targets, f"FK 子が TRUNCATE 対象から漏れている: {children - targets}"


# ── 3. argv（純関数）──────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    return mc.PgConn.from_url("postgresql://someone:s3cr3t@db.example.com:5432/appdb")


class TestDumpArgv:
    def test_has_strict_names(self, conn):
        """無いと「16表のうち1本だけ綴り違い」が exit 0 で通り、その表だけ欠けたダンプができる。"""
        argv = mc.pg_dump_argv(conn, ("companies",), "out.dump")
        assert "--strict-names" in argv

    def test_is_data_only_and_uncompressed(self, conn):
        argv = mc.pg_dump_argv(conn, ("companies",), "out.dump")
        assert "--data-only" in argv
        # ワイヤ上は非圧縮の COPY が流れる。圧縮するとファイルサイズが実 Egress を過小申告する
        assert "--compress=0" in argv

    def test_no_password_prompt(self, conn):
        assert "-w" in mc.pg_dump_argv(conn, ("companies",), "out.dump")

    def test_tables_are_schema_qualified(self, conn):
        argv = mc.pg_dump_argv(conn, ("companies", "macro_data"), "out.dump")
        assert "--table=public.companies" in argv
        assert "--table=public.macro_data" in argv

    def test_password_never_appears_in_argv(self, conn):
        argv = mc.pg_dump_argv(conn, mc.mirror_tables(), "out.dump")
        assert not any("s3cr3t" in a for a in argv)
        assert conn.env()["PGPASSWORD"] == "s3cr3t"

    def test_sslmode_defaults_to_require_for_remote(self, conn):
        assert conn.env()["PGSSLMODE"] == "require"

    def test_sslmode_absent_for_local(self):
        local = mc.PgConn.from_url("postgresql://edinet:edinet@localhost:5432/financial_db")
        assert "PGSSLMODE" not in local.env()


class TestRestoreArgv:
    def test_single_table_per_invocation(self, conn):
        """TOC はアルファベット順なので、1回の restore に順序を任せられない。"""
        argv = mc.pg_restore_argv(conn, "in.dump", "companies")
        assert "--table=companies" in argv

    def test_single_transaction(self, conn):
        assert "--single-transaction" in mc.pg_restore_argv(conn, "in.dump", "companies")

    def test_no_parallel_jobs(self, conn):
        """--jobs は順序を崩す。FK を順序でしか満たせないので使えない。"""
        argv = mc.pg_restore_argv(conn, "in.dump", "companies")
        assert not any(a.startswith("--jobs") or a == "-j" for a in argv)

    def test_no_disable_triggers(self, conn):
        """`edinet` は非 superuser なので --disable-triggers は使えない。"""
        assert "--disable-triggers" not in mc.pg_restore_argv(conn, "in.dump", "companies")


# ── 4. 出力の decode ──────────────────────────────────────────────────────────

class TestDecodePgOutput:
    def test_utf8_roundtrip(self):
        assert mc.decode_pg_output("pg_dump: error: nope".encode("utf-8")) \
            == "pg_dump: error: nope"

    def test_cp932_server_message(self):
        """サーバの FATAL は lc_messages の cp932 で返る。**認証失敗は client_encoding 交渉前**
        なので PGCLIENTENCODING では直らない（2026-08-16 実測）。"""
        msg = 'FATAL:  ユーザー"x"のパスワード認証に失敗しました'
        assert mc.decode_pg_output(msg.encode("cp932")) == msg

    def test_never_raises(self):
        assert mc.decode_pg_output(b"\xff\xfe\x00 broken") is not None


# ── 5. エンドポイント解決とガード ────────────────────────────────────────────

class TestEndpoints:
    def test_raw_url_is_taken_as_is(self):
        ep = mc.resolve_endpoint("postgresql://u:p@localhost:5432/x")
        assert ep.url == "postgresql://u:p@localhost:5432/x"
        assert ep.is_local
        assert ep.dbname == "x"

    def test_postgres_scheme_is_normalized(self):
        assert mc.resolve_endpoint("postgres://u:p@localhost/x").url.startswith("postgresql://")

    def test_local_uses_database_url_local(self):
        ep = mc.resolve_endpoint("local", {"DATABASE_URL_LOCAL":
                                           "postgresql://a:b@127.0.0.1:5432/mirror"})
        assert ep.url.endswith("/mirror")

    def test_prod_uses_database_url(self):
        ep = mc.resolve_endpoint("prod", {"DATABASE_URL":
                                          "postgresql://a:b@db.example.com:5432/prod"})
        assert ep.dbname == "prod"
        assert not ep.is_local

    def test_local_pointing_at_remote_raises(self):
        """`database.resolve_database_url` のガードへ委譲していること（二重実装しない）。"""
        with pytest.raises(RuntimeError):
            mc.resolve_endpoint("local", {"DATABASE_URL_LOCAL":
                                          "postgresql://a:b@db.example.com:5432/x"})

    def test_unknown_target_raises(self):
        with pytest.raises(ValueError):
            mc.resolve_endpoint("localhost", {})

    def test_label_masks_password(self):
        ep = mc.resolve_endpoint("postgresql://u:s3cr3t@localhost:5432/x")
        assert "s3cr3t" not in ep.label


class TestDestGuard:
    def test_remote_dest_is_rejected(self):
        """ミラーが本番へ書く経路を持たないことの担保。"""
        remote = mc.Endpoint("url", "postgresql://u:p@db.example.com:5432/prod")
        with pytest.raises(SystemExit):
            mc.guard_dest_local(remote)

    def test_local_dest_passes(self):
        mc.guard_dest_local(mc.Endpoint("url", "postgresql://u:p@localhost:5432/x"))


# ── 6. オーバーラップと高水位 ────────────────────────────────────────────────

class TestOverlap:
    def test_weekly_overlap_is_derived_not_hardcoded(self):
        """`DAILY_WINDOW_DAYS` から導出すること。定数を直書きすると窓を広げたとき黙って壊れる。"""
        assert mc.WEEKLY_OVERLAP_DAYS >= DAILY_WINDOW_DAYS
        assert mc.WEEKLY_OVERLAP_DAYS % 7 == 0
        assert mc.WEEKLY_OVERLAP_WEEKS * 7 == mc.WEEKLY_OVERLAP_DAYS

    def test_weekly_overlap_beats_the_original_eight_week_proposal(self):
        """Issue #481 / #480 の当初案「末尾8週」では 183 日の遡及上書きを覆えない。"""
        assert mc.WEEKLY_OVERLAP_DAYS > 8 * 7
        assert mc.SYNC_PLAN["stock_price_weekly"].overlap_days == mc.WEEKLY_OVERLAP_DAYS

    def test_since_value_for_datetime_key(self):
        got = mc.since_value("companies", "updated_at", datetime(2026, 8, 16, 12, 0), 1)
        assert got == datetime(2026, 8, 15, 12, 0)

    def test_since_value_for_iso_string_key(self):
        got = mc.since_value("stock_price_weekly", "week_start", "2026-08-10",
                             mc.WEEKLY_OVERLAP_DAYS)
        assert got == "2026-02-02"          # 2026-08-10 から 189 日前
        assert isinstance(got, str)

    def test_since_value_for_date_key(self):
        got = mc.since_value("regression_results", "period_end", date(2026, 8, 16), 1)
        assert got == date(2026, 8, 15)

    def test_empty_mirror_falls_back_to_full_fetch(self):
        assert mc.since_value("companies", "updated_at", None, 1) is None


# ── 7. テーブル選択 ──────────────────────────────────────────────────────────

class TestSelectedTables:
    def _args(self, tables):
        class A:
            pass
        a = A()
        a.tables = tables
        return a

    def test_default_is_every_mirror_table(self):
        from scripts.mirror_verify import selected_tables
        assert selected_tables(self._args(None)) == mc.mirror_tables()

    def test_subset_is_reordered_to_dependency_order(self):
        from scripts.mirror_verify import selected_tables
        got = selected_tables(self._args("financial_records,companies"))
        assert got.index("companies") < got.index("financial_records")

    def test_out_of_scope_table_is_rejected(self):
        """除外表を `--tables` で指定したら弾く（typo が exit 0 で通ると黙って欠ける）。

        対象は `MIRROR_EXCLUDED` から取る。直書きすると、その表を範囲へ入れた日に
        検査が「範囲内の表を弾け」という別物へ化ける（2026-08-20 の `stock_price_daily`）。
        """
        from scripts.mirror_verify import selected_tables
        assert mc.MIRROR_EXCLUDED, "除外が空なら、この検査は成立しない"
        with pytest.raises(SystemExit):
            selected_tables(self._args(mc.MIRROR_EXCLUDED[0]))


class TestChecksumExpr:
    """チェックサム式は**行順にも列順にも**依存してはいけない（2026-08-19 の実障害）。

    `x::text`（行全体のテキスト化）は attnum 順なので、source が
    `ALTER TABLE ADD COLUMN` の積み重ね、mirror が `Base.metadata.create_all` 由来だと
    値が完全に一致していてもずれる。初回 pull で **16 表中 7 表が「行数 +0 なのに NG」**
    になり、`companies` は created_at/updated_at と issued_shares 以降が入れ替わっていた。

    既存の実 PG テスト（test_mirror_postgres.py）は両端を同じ DDL で作るため列順が揃い、
    **この罠を構造的に検出できなかった**。だから純関数側で式そのものを固定する。
    """

    def test_column_order_does_not_change_the_expression(self):
        a = mc.checksum_expr(["id", "name", "created_at"])
        b = mc.checksum_expr(["created_at", "id", "name"])
        assert a == b

    def test_does_not_use_whole_row_text(self):
        """`x::text` を使うと attnum 順に依存する。二度と戻さない。"""
        expr = mc.checksum_expr(["id", "name"])
        assert "x::text" not in expr
        assert "concat_ws" in expr

    def test_every_column_is_quoted_and_present(self):
        expr = mc.checksum_expr(["id", "trade_date", "close"])
        for col in ("id", "trade_date", "close"):
            assert f'"{col}"::text' in expr

    def test_null_is_distinguishable(self):
        r"""`concat_ws` は NULL を黙って飛ばすので (a,NULL,c) と (a,c) が同じになる。

        `\N` へ落としてから連結していることを固定する。
        """
        expr = mc.checksum_expr(["a", "b"])
        assert expr.count("coalesce(") >= 2
        assert r"'\N'" in expr

    def test_empty_columns_is_rejected(self):
        """列が空のまま式を組むと、全行が同じ定数ハッシュになり常に一致してしまう。"""
        with pytest.raises(ValueError):
            mc.checksum_expr([])

    def test_aggregate_is_order_independent(self):
        """行順に依存する `string_agg(... ORDER BY)` へ戻していないこと。"""
        expr = mc.checksum_expr(["id"])
        assert "string_agg" not in expr
        assert expr.startswith("coalesce(sum(")


class TestSessionFixesAreAttachedToEngines:
    """セッション設定は「呼ぶ人が思い出す」ものではなく「接続に付いてくる」ものにする。

    以前は `table_stats()` の中でしか `fix_session()` を呼んでおらず、`mirror_sync` の
    `fetch_rows()` は既定のまま source を読んでいた。float8 は
    `extra_float_digits >= 1` でないと有効数字15桁へ丸められる（完全往復には17桁が要る）。
    `pull` が無事だったのは `pg_dump` が自前で設定するためで、**経路ごとに正しさが違った**。
    """

    def test_float_precision_is_pinned(self):
        assert any("extra_float_digits" in s for s in mc._SESSION_FIXES)

    def test_timezone_and_datestyle_are_pinned(self):
        joined = " ".join(mc._SESSION_FIXES)
        assert "TimeZone" in joined and "DateStyle" in joined

    def test_make_engine_registers_a_connect_hook(self):
        """engine を作った時点でフックが張られていること（呼び忘れを構造的に消す）。"""
        import inspect
        src = inspect.getsource(mc.make_engine)
        assert 'listens_for' in src and '"connect"' in src
        assert "_SESSION_FIXES" in src
