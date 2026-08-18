"""週次差分ロード（#480・ADR-0036）を**実 PostgreSQL** で検証する。

## なぜ別ファイルなのか

`tests/test_weekly_price_cache.py` は in-memory SQLite で完結するが、SQLite では
**#480 の受け入れ基準そのものが測れない**。SELECT の `cursor.rowcount` が常に -1 なので、
`db_egress` の台帳には `unknown_calls` にしか積まれず「転送行数が実際に減った」を数値で
言えない（ADR-0034 決定4）。加えて次の3つも SQLite では確かめられない。

  - `WHERE edinet_code IN (...) AND week_start >= :since` が **PK の範囲スキャン**になること
    （`week_start` は PK 第2列。単独条件だと seq scan で、チャンク分割の意味が消える）
  - 文字列日付の比較が PostgreSQL のコレーションでも辞書順＝時系列順であること
  - 差分とフルが**実 DB のプランナ経由でもビット一致**すること

`FINAPP_TEST_PG_URL` が設定されているときだけ走り、**CI では skip される**
（`ci.yml` の「本番 DB にも外部にも触れない」契約を崩さない）。

アプリの実テーブルには触れない。**専用スキーマを作って `search_path` をそこへ固定**し、
終了時に `DROP SCHEMA CASCADE` する——`financial_db` は #481 のミラーの器で、8/18 に本番から
pull する予定のもの。合成データが残る経路を構造的に持たせない。

実行:
    $env:FINAPP_TEST_PG_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
    pytest tests/test_weekly_price_cache_postgres.py -v -s

`-s` を付けると転送行数・所要のレポートが読める。
"""
import os
import sys
import time
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_egress
import weekly_price_cache as wpc
from database import AppSetting, Company, StockPriceWeekly
from db_egress import LEDGER
from plugins.macro_snapshots import load_weekly_prices_chunked

PG_URL = os.environ.get("FINAPP_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="FINAPP_TEST_PG_URL 未設定（ローカル PostgreSQL がある環境でのみ実行）",
)

SCHEMA = "_test_weekly_cache"
N_COMPANIES = 60
N_WEEKS = 300          # 約5.7年。27週オーバーラップが全体の 9% になり本番の比率に近い
LAST_MONDAY = "2026-08-10"


@pytest.fixture(scope="module")
def engine():
    boot = create_engine(PG_URL, pool_pre_ping=True)
    with boot.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    boot.dispose()

    # search_path をテスト用スキーマへ固定した engine で ORM を動かす。
    # ORM のテーブル名は変えずに、実体だけを隔離する。
    eng = create_engine(PG_URL, pool_pre_ping=True,
                        connect_args={"options": f"-csearch_path={SCHEMA}"})
    db_egress.install(eng)
    for table in (Company.__table__, StockPriceWeekly.__table__, AppSetting.__table__):
        table.create(bind=eng)

    mondays = [(date.fromisoformat(LAST_MONDAY) - timedelta(days=7 * i)).isoformat()
               for i in range(N_WEEKS - 1, -1, -1)]
    with eng.begin() as conn:
        # is_active は ORM 側のデフォルトなので raw INSERT では効かない（NOT NULL 違反になる）
        conn.execute(text(
            "INSERT INTO companies (edinet_code, name, sec_code, is_active) "
            "SELECT 'E' || lpad(i::text, 5, '0'), 'T' || i, lpad(i::text, 4, '0'), true "
            f"FROM generate_series(1, {N_COMPANIES}) AS i"))
        # week_start は月曜、trade_date は同じ週の金曜。**両者をずらしておくことが重要**——
        # 差分ロードは DB を week_start で、キャッシュを trade_date で切るので、同じ値だと
        # ISO 週の不変条件が効いているかを検証できない。
        conn.execute(text(
            "INSERT INTO stock_price_weekly "
            "  (edinet_code, week_start, trade_date, close_last, volume_sum, n_days) "
            "SELECT 'E' || lpad(i::text, 5, '0'), w, "
            "       to_char(w::date + 4, 'YYYY-MM-DD'), "
            "       1000 + (i * 7 + row_number() OVER ())::float / 100, 1e6, 5 "
            f"FROM generate_series(1, {N_COMPANIES}) AS i, unnest(:weeks) AS w"),
            {"weeks": mondays})
        conn.execute(text(f"ANALYZE {SCHEMA}.stock_price_weekly"))
    yield eng

    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    eng.dispose()


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def cache_on(tmp_path, monkeypatch):
    monkeypatch.setenv("FINAPP_WEEKLY_CACHE", "1")
    monkeypatch.setenv("FINAPP_WEEKLY_CACHE_DIR", str(tmp_path / "wc"))
    for var in ("FINAPP_EGRESS_ROW_LIMIT", "FINAPP_EGRESS_MB_LIMIT",
                "FINAPP_EGRESS_ENFORCE", "FINAPP_EGRESS_LEDGER", "FINAPP_JOB"):
        monkeypatch.delenv(var, raising=False)
    wpc._stats.update(hits=0, misses=0, fetched_rows=0)
    db_egress._reset_for_tests()
    yield
    db_egress._reset_for_tests()


def _weekly_rows_transferred() -> int:
    snap = LEDGER.snapshot()
    return snap["tables"].get("stock_price_weekly", {}).get("rows", 0)


class TestEgressActuallyDrops:
    """#480 の受け入れ基準。SQLite では rowcount=-1 で原理的に測れない。"""

    def test_second_load_transfers_far_fewer_rows(self, db, capsys):
        total = N_COMPANIES * N_WEEKS

        load_weekly_prices_chunked(db, with_volume=False)
        cold = _weekly_rows_transferred()

        db_egress._reset_for_tests()
        load_weekly_prices_chunked(db, with_volume=False)
        warm = _weekly_rows_transferred()

        with capsys.disabled():
            print(f"\n[#480] weekly rows: cold={cold} warm={warm} "
                  f"({100.0 * warm / cold:.1f}% / total={total})")

        assert cold >= total, "コールドで全行を引いていない"
        # 27週 + 指紋の1行。理論値は 27/300 = 9.0%。プランの揺れを見て 15% を上限に置く。
        assert warm < cold * 0.15, f"差分ロードで転送が落ちていない（cold={cold} warm={warm}）"

    def test_disabled_switch_restores_the_full_transfer(self, db, monkeypatch):
        """緊急停止したら本当に元へ戻る（＝スイッチが飾りでない）。"""
        monkeypatch.setenv("FINAPP_WEEKLY_CACHE", "0")
        load_weekly_prices_chunked(db, with_volume=False)
        assert _weekly_rows_transferred() >= N_COMPANIES * N_WEEKS


class TestPlanUsesThePrimaryKey:
    def test_delta_predicate_is_an_index_condition(self, db, capsys):
        """`week_start >= :since` が **PK の Index Cond に入る**こと。

        `edinet_code IN (...)` の外に出すと先頭列にならず seq scan になり、
        500社チャンクに分けている意味が消える（それでも結果は正しいので気づけない）。
        """
        since = wpc.refresh_boundary(wpc.fingerprint(db).max_week_start)
        codes = [f"E{str(i).zfill(5)}" for i in range(1, 21)]
        plan = "\n".join(r[0] for r in db.execute(text(
            "EXPLAIN SELECT edinet_code, trade_date, close_last FROM stock_price_weekly "
            "WHERE edinet_code = ANY(:codes) AND week_start >= :since "
            "ORDER BY edinet_code, trade_date"), {"codes": codes, "since": since}).all())
        with capsys.disabled():
            print(f"\n[#480] plan for since={since}:\n{plan}")
        assert "Seq Scan" not in plan, f"seq scan になっている:\n{plan}"
        assert "week_start" in plan, f"week_start が索引条件に入っていない:\n{plan}"


class TestBitIdentityOnRealPostgres:
    def test_incremental_equals_full_through_the_real_planner(self, db, monkeypatch):
        """文字列日付の比較・ORDER BY のコレーション込みで一致すること。"""
        monkeypatch.setenv("FINAPP_WEEKLY_CACHE", "0")
        full = load_weekly_prices_chunked(db, with_volume=False)

        monkeypatch.setenv("FINAPP_WEEKLY_CACHE", "1")
        load_weekly_prices_chunked(db, with_volume=False)          # cold
        warm = load_weekly_prices_chunked(db, with_volume=False)   # delta + merge
        assert warm == full

    def test_correction_inside_the_window_propagates(self, db):
        load_weekly_prices_chunked(db, with_volume=False)
        since = wpc.refresh_boundary(wpc.fingerprint(db).max_week_start)
        db.execute(text("UPDATE stock_price_weekly SET close_last = 424242.0 "
                        "WHERE edinet_code = 'E00001' AND week_start = :ws"), {"ws": since})
        db.commit()
        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        assert 424242.0 in [r.close_last for r in rows]


class TestColumnCountUnchanged:
    def test_select_still_returns_three_or_four_columns(self, db):
        """`week_start` を SELECT に足していないこと（ADR-0036 決定6）。

        足すと `db_egress.EGRESS_COST_TABLE` の `("stock_price_weekly", 4)`（volume 込みの
        較正値 42.0 B/行）に誤って当たり、台帳の推定が静かにずれる。
        """
        seen = []
        bind = db.get_bind()

        def _spy(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
            if "stock_price_weekly" in statement and cursor.description:
                seen.append(len(cursor.description))

        event.listen(bind, "after_cursor_execute", _spy)
        try:
            load_weekly_prices_chunked(db, with_volume=False)
            load_weekly_prices_chunked(db, with_volume=False)
        finally:
            event.remove(bind, "after_cursor_execute", _spy)

        data_queries = [n for n in seen if n > 2]
        assert data_queries, "週次のデータクエリが観測できていない"
        assert set(data_queries) == {3}, f"列数が変わっている: {sorted(set(seen))}"


class TestFingerprintCost:
    def test_fingerprint_is_fast_enough_for_the_statement_timeout(self, db, capsys):
        """`max(week_start)+count(*)` の所要（参考出力・アサートは緩く）。

        PK は `(edinet_code, week_start)` なので `max(week_start)` は先頭列にならず
        index-only scan / seq scan になる。ADR-0032 の `statement_timeout=2min` に対して
        余裕があるはずだが、本番 1.28M 行での実測は 8/18 以降。ここは合成データでの目安。
        """
        t0 = time.monotonic()
        fp = wpc.fingerprint(db)
        dt = time.monotonic() - t0
        with capsys.disabled():
            print(f"\n[#480] fingerprint: {dt * 1000:.0f}ms "
                  f"rows={fp.n_rows} max_week_start={fp.max_week_start}")
        assert fp.n_rows == N_COMPANIES * N_WEEKS
        assert fp.max_week_start == LAST_MONDAY
        assert dt < 10.0, "指紋の取得が遅すぎる（本番規模では statement_timeout に当たりうる）"
