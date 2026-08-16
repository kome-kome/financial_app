"""ミラー3本を**実 PostgreSQL** で検証する（Issue #481 B-2〜B-4）。

`tests/test_db_egress_postgres.py` と同じく、環境変数が無ければモジュールごと skip する。
**CI は本番 DB にも外部にも触れない**という契約を崩さないため。

証明する2点（どちらも Supabase 無しで確かめられる。8/18 に初めて試して落ちるのを避ける）:

1. **FK 依存順の restore が非 superuser で成立する。** しかも「順序が本当に効いている」ことを
   逆順で流して FK 違反が起きることで示す。順序が実は無関係だった（＝ FK が効いていない）
   ケースと取り違えない。
2. **27週オーバーラップが 20週前の遡及訂正を拾う。** 件数も最大キーも動かない訂正なので、
   検出できるのは値レベルのチェックサムだけ。当初案の「末尾8週」では覆えない位置に置いてある。

実行（PowerShell）:
    $env:FINAPP_TEST_PG_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
    pytest tests/test_mirror_postgres.py -v -s

**注意**: `edinet` に CREATEDB 権限が要る（専用の2 DB を作って実 `financial_db` を汚さないため）。
無ければ skip する。付与は superuser で1回:
    psql -U postgres -h localhost -c "ALTER ROLE edinet CREATEDB;"
"""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PG_URL = os.environ.get("FINAPP_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="FINAPP_TEST_PG_URL 未設定（ローカル PostgreSQL がある環境でのみ実行）",
)

if PG_URL:
    from scripts import mirror_common as mc
    from scripts import mirror_rehearse as reh


@pytest.fixture(scope="module")
def rehearsal():
    """予行用の2 DB を作り、src へ合成シードを入れる。teardown で **DB ごと削除**。

    「TRUNCATE で掃除する」ではなく器ごと捨てるので、合成データが実ミラーへ残る経路が無い。
    """
    adm = reh.admin_engine()
    try:
        with adm.connect() as conn:
            can = conn.execute(text(
                "SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user")).scalar()
            if not can:
                pytest.skip("CREATEDB 権限が無い（ALTER ROLE ... CREATEDB が未実行）")
            reh.drop_dbs(conn)          # 前回の残骸があれば消してから始める
            reh.create_dbs(conn)
    finally:
        adm.dispose()

    src = reh._swap_db(reh.base_url(), reh.SRC_DB)
    dst = reh._swap_db(reh.base_url(), reh.DST_DB)
    reh.init_schema(src, reh.SRC_DB)
    reh.init_schema(dst, reh.DST_DB)
    reh.seed(src)

    yield {"src": src, "dst": dst}

    adm = reh.admin_engine()
    try:
        with adm.connect() as conn:
            reh.drop_dbs(conn)
    finally:
        adm.dispose()


def _stats(url, *, checksum=False):
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            return mc.table_stats(conn, mc.mirror_tables(), with_checksum=checksum)
    finally:
        eng.dispose()


class TestForeignKeyOrdering:
    """FK は順序でしか満たせない（非 superuser は --disable-triggers を使えない）。"""

    def test_reverse_order_restore_violates_fk(self, rehearsal, tmp_path):
        """**逆順に流すと必ず失敗すること。** これが「順序が効いている」ことの証明。

        失敗しないなら FK が効いていないか、順序が無関係だったということで、
        正順で通ったこと自体の意味が消える。
        """
        src = mc.PgConn.from_url(rehearsal["src"])
        dst = mc.PgConn.from_url(rehearsal["dst"])
        dump = tmp_path / "rev.dump"
        mc.run_pg(mc.pg_dump_argv(src, mc.mirror_tables(), str(dump)), src, what="pg_dump")

        eng = create_engine(rehearsal["dst"])
        try:
            with eng.begin() as conn:
                conn.execute(text("TRUNCATE TABLE " + ", ".join(
                    f'public."{t}"' for t in mc.truncate_targets(mc.mirror_tables()))))
        finally:
            eng.dispose()

        with pytest.raises(SystemExit) as ei:
            for t in reversed(mc.mirror_tables()):
                mc.run_pg(mc.pg_restore_argv(dst, str(dump), t), dst, what=f"pg_restore({t})")
        assert "foreign key" in str(ei.value).lower()

    def test_dependency_order_restore_succeeds(self, rehearsal, tmp_path):
        """正順なら非 superuser でも通る（`--disable-triggers` 無しで FK を満たせる）。"""
        src = mc.PgConn.from_url(rehearsal["src"])
        dst = mc.PgConn.from_url(rehearsal["dst"])
        dump = tmp_path / "fwd.dump"
        mc.run_pg(mc.pg_dump_argv(src, mc.mirror_tables(), str(dump)), src, what="pg_dump")

        eng = create_engine(rehearsal["dst"])
        try:
            with eng.begin() as conn:
                conn.execute(text("TRUNCATE TABLE " + ", ".join(
                    f'public."{t}"' for t in mc.truncate_targets(mc.mirror_tables()))))
        finally:
            eng.dispose()

        for t in mc.mirror_tables():
            mc.run_pg(mc.pg_restore_argv(dst, str(dump), t), dst, what=f"pg_restore({t})")

        s, d = _stats(rehearsal["src"], checksum=True), _stats(rehearsal["dst"], checksum=True)
        for t in mc.mirror_tables():
            assert s[t]["n"] == d[t]["n"], f"{t} の件数が一致しない"
            assert s[t]["ck"] == d[t]["ck"], f"{t} の内容が一致しない"


class TestStrictNames:
    def test_typo_in_one_table_fails_loudly(self, rehearsal, tmp_path):
        """`--strict-names` が無いと綴り誤りが exit 0 で通り、その表だけ欠けたダンプができる。"""
        src = mc.PgConn.from_url(rehearsal["src"])
        dump = tmp_path / "typo.dump"
        with pytest.raises(SystemExit):
            mc.run_pg(mc.pg_dump_argv(src, ("companies", "no_such_table"), str(dump)),
                      src, what="pg_dump")


class TestRetroactiveCorrection:
    """件数も最大キーも動かない訂正を、27週オーバーラップと checksum が拾えるか。"""

    def test_overlap_catches_a_twenty_week_old_rewrite(self, rehearsal):
        # まず両者を揃える
        src, dst = rehearsal["src"], rehearsal["dst"]
        base = _stats(src, checksum=True)
        assert base["stock_price_weekly"]["ck"] == _stats(dst, checksum=True)[
            "stock_price_weekly"]["ck"], "前提: 同期済みであること"

        info = reh.mutate(src)
        corrected = info["corrected_week"]

        after = _stats(src, checksum=True)
        assert after["stock_price_weekly"]["ck"] != base["stock_price_weekly"]["ck"]

        # 訂正位置が「8週では覆えず 27週なら覆う」ところに在ることを明示的に確認する
        hi = after["stock_price_weekly"]["hi"]
        eight = mc.since_value("stock_price_weekly", "week_start", hi, 8 * 7)
        twentyseven = mc.since_value("stock_price_weekly", "week_start", hi,
                                     mc.WEEKLY_OVERLAP_DAYS)
        assert corrected < eight, "訂正が 8週窓の内側にあるとテストの意味が無い"
        assert corrected >= twentyseven, "訂正が 27週窓からも外れている"

        # 同期して一致まで戻ること
        from scripts import mirror_sync as ms
        src_eng, dst_eng = create_engine(src), create_engine(dst)
        try:
            with src_eng.connect() as sc, dst_eng.connect() as dc:
                plans = [ms.plan_table(sc, dc, t) for t in mc.mirror_tables()]
            with src_eng.connect() as sc:
                for p in plans:
                    if not p["n_fetch"]:
                        continue
                    rows = ms.fetch_rows(sc, p["table"], p["since"])
                    with dst_eng.begin() as dc:
                        ms.apply_rows(dc, p["table"], rows, mode=p["mode"])
        finally:
            src_eng.dispose()
            dst_eng.dispose()

        final_src, final_dst = _stats(src, checksum=True), _stats(dst, checksum=True)
        for t in mc.mirror_tables():
            assert final_src[t]["n"] == final_dst[t]["n"], f"{t} の件数が一致しない"
            assert final_src[t]["ck"] == final_dst[t]["ck"], \
                f"{t} の内容が一致しない（20週前の訂正を取り落とした可能性）"
