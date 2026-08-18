"""db_egress を**実 PostgreSQL** で検証する（Issue #481 B-0・ADR-0034 の未検証項目）。

## なぜ別ファイルなのか

`tests/test_db_egress.py` は in-memory SQLite で完結するが、**SQLite は SELECT の
`cursor.rowcount` が常に -1** なので、台帳の中核である「転送行数の実計上」を
**CI では原理的に一度も検証できない**。ADR-0034 はこれを「限界」として明記していた。

ローカル PostgreSQL（#481 B-0）が用意できたのでその穴を塞ぐ。`FINAPP_TEST_PG_URL` が
設定されているときだけ走り、**CI では skip される**（`ci.yml` は本番 DB にも外部にも触れない
という契約を崩さない）。

実行:
    $env:FINAPP_TEST_PG_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
    pytest tests/test_db_egress_postgres.py -v -s

`-s` を付けると較正残差のレポートが読める。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_egress
from db_egress import LEDGER, EgressBudgetExceeded

PG_URL = os.environ.get("FINAPP_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="FINAPP_TEST_PG_URL 未設定（ローカル PostgreSQL がある環境でのみ実行）",
)

# アプリの実テーブルには触れない。3列の型は stock_price_weekly の
# `load_weekly_prices_chunked(with_volume=False)` が引く列（#446 実測の対象）に合わせる。
SCRATCH = "_test_db_egress_scratch"
N_ROWS = 500


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(PG_URL, pool_pre_ping=True)
    assert db_egress.install(eng) is True, "リスナが張れていない"
    with eng.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{SCRATCH}"'))
        conn.execute(text(
            f'CREATE TABLE public."{SCRATCH}" '
            "(id serial PRIMARY KEY, edinet_code varchar(10), "
            " trade_date varchar(10), close_last double precision)"
        ))
        conn.execute(text(
            f'INSERT INTO public."{SCRATCH}" (edinet_code, trade_date, close_last) '
            "SELECT 'E' || lpad(i::text, 5, '0'), "
            "       to_char(DATE '2020-01-06' + (i % 300) * 7, 'YYYY-MM-DD'), "
            "       1000 + (i % 5000)::float / 10 "
            f"FROM generate_series(1, {N_ROWS}) AS i"
        ))
    yield eng
    with eng.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{SCRATCH}"'))
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_ledger(monkeypatch):
    for var in ("FINAPP_EGRESS_ROW_LIMIT", "FINAPP_EGRESS_MB_LIMIT",
                "FINAPP_EGRESS_ENFORCE", "FINAPP_EGRESS_LEDGER", "FINAPP_JOB"):
        monkeypatch.delenv(var, raising=False)
    db_egress._reset_for_tests()
    yield
    db_egress._reset_for_tests()


class TestRealRowCounts:
    """SQLite では unknown にしか積めなかった経路が、実 PostgreSQL では実数で積まれる。"""

    def test_select_records_the_actual_row_count(self, engine):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f'SELECT edinet_code, trade_date, close_last FROM public."{SCRATCH}"'
            )).fetchall()
        assert len(rows) == N_ROWS
        snap = LEDGER.snapshot()
        assert snap["rows"] == N_ROWS, "psycopg2 の既定カーソルはバッファ式＝rowcount が確定する"
        assert snap["unknown_calls"] == 0, "PostgreSQL では unknown へ落ちてはいけない"
        assert snap["tables"][SCRATCH]["rows"] == N_ROWS

    def test_limit_is_reflected_in_the_ledger(self, engine):
        with engine.connect() as conn:
            conn.execute(text(f'SELECT id FROM public."{SCRATCH}" LIMIT 37')).fetchall()
        assert LEDGER.snapshot()["rows"] == 37

    def test_aggregate_transfers_one_row(self, engine):
        """サーバ側集約は1行しか返さない＝Egress ほぼゼロであることが台帳に出る。"""
        with engine.connect() as conn:
            conn.execute(text(f'SELECT count(*) FROM public."{SCRATCH}"')).fetchall()
        assert LEDGER.snapshot()["rows"] == 1


class TestWhatCountsAsEgress:
    def test_write_statements_are_not_counted(self, engine):
        """INSERT/UPDATE/DELETE は ingress（無料）＝結果セットを返さないので description が None。"""
        with engine.begin() as conn:
            conn.execute(text(
                f'INSERT INTO public."{SCRATCH}" (edinet_code) VALUES (\'E99999\')'))
            conn.execute(text(
                f'UPDATE public."{SCRATCH}" SET close_last = 1 WHERE edinet_code = \'E99999\''))
            conn.execute(text(
                f'DELETE FROM public."{SCRATCH}" WHERE edinet_code = \'E99999\''))
        assert LEDGER.snapshot()["calls"] == 0

    def test_insert_returning_is_counted(self, engine):
        """**RETURNING は行が返る＝Egress**。「文が SELECT で始まるか」で判定していたら落ちる。

        SQLite でも description は付くが rowcount が -1 なので、実数で確認できるのはここだけ。
        """
        with engine.begin() as conn:
            out = conn.execute(text(
                f'INSERT INTO public."{SCRATCH}" (edinet_code) '
                "VALUES ('E88888'), ('E88889') RETURNING id"
            )).fetchall()
            conn.execute(text(
                f"DELETE FROM public.\"{SCRATCH}\" WHERE edinet_code LIKE 'E8888%'"))
        assert len(out) == 2
        snap = LEDGER.snapshot()
        assert snap["rows"] == 2, "RETURNING の行が計上されていない"
        assert snap["tables"][SCRATCH]["rows"] == 2


class TestCircuitBreakerOnRealQueries:
    def test_row_limit_trips_on_a_real_query(self, engine, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", str(N_ROWS // 2))
        with pytest.raises(EgressBudgetExceeded, match="rows"):
            with engine.connect() as conn:
                conn.execute(text(f'SELECT id FROM public."{SCRATCH}"')).fetchall()

    def test_under_the_limit_passes(self, engine, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", str(N_ROWS * 10))
        with engine.connect() as conn:
            conn.execute(text(f'SELECT id FROM public."{SCRATCH}"')).fetchall()   # 例外が出ないこと

    def test_enforce_zero_lets_it_through(self, engine, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "1")
        monkeypatch.setenv("FINAPP_EGRESS_ENFORCE", "0")
        with engine.connect() as conn:
            conn.execute(text(f'SELECT id FROM public."{SCRATCH}"')).fetchall()
        assert LEDGER.snapshot()["rows"] == N_ROWS, "送出は止めても計測は続く"


class TestCalibrationResidual:
    """推定 B/行 と正本（サーバ側 `sum(octet_length(列::text))`）の残差を出す。

    **アサートしない。** 本番データでの較正は #493 のランブック手順4 で 2026-08-19 に
    取り直し済み（mirror 16 表を全列で実測 → `EGRESS_COST_TABLE`）。ここは
    「その手順が実際に走ること」と「桁が合っていること」を確かめる足場に留める。
    scratch の値は本番の分布と違うため、残差の絶対値そのものに意味は無い。
    """

    def test_report_residual_against_octet_length(self, engine, capsys):
        cols = ("edinet_code", "trade_date", "close_last")
        expr = " + ".join(f"octet_length({c}::text)" for c in cols)
        with engine.connect() as conn:
            measured = conn.execute(text(
                f'SELECT sum({expr}) FROM public."{SCRATCH}"')).scalar()
            db_egress._reset_for_tests()
            conn.execute(text(
                f'SELECT {", ".join(cols)} FROM public."{SCRATCH}"')).fetchall()
        estimated = LEDGER.snapshot()["est_bytes"]
        coef = db_egress.bytes_per_row("stock_price_weekly", 3)

        with capsys.disabled():
            print(f"\n  --- 較正残差（scratch {N_ROWS} 行 × 3列）---")
            print(f"  正本 octet_length 合計 : {measured:>10,} B  "
                  f"({measured / N_ROWS:.1f} B/行)")
            print(f"  台帳の推定             : {estimated:>10,.0f} B  "
                  f"({estimated / N_ROWS:.1f} B/行・未較正テーブルなので既定 "
                  f"{db_egress.DEFAULT_BYTES_PER_COLUMN} B/列/行)")
            print(f"  参考: stock_price_weekly 3列の較正値 = {coef} B/行（#446 実測）")
            print(f"  残差（推定/正本）      : {estimated / measured:.2f}x")

        assert measured > 0 and estimated > 0
