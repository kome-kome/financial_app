"""`financial_metrics_with_ttm` を**実 PostgreSQL** で作り、通期の行が変わらないことを確かめる。

## なぜ別ファイルなのか

この VIEW の壊れ方は SQLite では原理的に再現できない。CI（`conftest.py`）は ORM の列から
実テーブルを作るだけで、VIEW の SQL は 1 文字も実行されない。窓関数・UNION の型解決・
`::double precision` の往復は Postgres でしか確かめられず、`tests/test_financial_metrics_with_ttm.py`
は「2 つの SQL の式が同じ文字列である」までしか言えない。

`FINAPP_TEST_PG_URL` が設定されているときだけ走り、**CI では skip される**
（`ci.yml` は本番 DB にも外部にも触れないという契約を崩さない）。規約は
`tests/test_tz_postgres.py` と同じ。

**書き込みはすべてトランザクションの中で行い、最後に必ず取り消す。** VIEW を落として作り直す
間はその VIEW に ACCESS EXCLUSIVE を取るので、夜間バッチ（JST 17:20）や日中枠と重ねて
走らせないこと。

実行:
    $env:FINAPP_TEST_PG_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
    pytest tests/test_financial_metrics_with_ttm_postgres.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

PG_URL = os.environ.get("FINAPP_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="FINAPP_TEST_PG_URL 未設定（ローカル PostgreSQL がある環境でのみ実行）",
)

VIEW = "financial_metrics_with_ttm"


@pytest.fixture
def conn():
    """**取り消されるトランザクション**。本番の表・VIEW に痕跡を残さない。"""
    with database.engine.connect() as c:
        trans = c.begin()
        try:
            database.TtmFinancialRecord.__table__.create(bind=c, checkfirst=True)
            # 基準の遅れの列（#753）。移行前の DB でも2本の VIEW を作れるよう、ここで足す（本番の移行は
            # `database._ensure_tables`）。取り消すトランザクションの中なので DB には残らない。
            for col in ("shares_lag", "dps_lag"):
                c.execute(text(
                    f"ALTER TABLE split_adjustment_factors ADD COLUMN IF NOT EXISTS {col} "
                    "DOUBLE PRECISION NOT NULL DEFAULT 1.0"))
            # 比べる相手の `financial_metrics` も今の SQL から作り直す（DB に残っている古い定義と
            # 比べると、列の足し引きがそのまま「差」に出る）。
            c.execute(text("DROP VIEW IF EXISTS financial_metrics"))
            c.execute(text(database.FINANCIAL_METRICS_VIEW_SQL))
            c.execute(text(f"DROP VIEW IF EXISTS {VIEW}"))
            c.execute(text(database.FINANCIAL_METRICS_WITH_TTM_VIEW_SQL))
            yield c
        finally:
            trans.rollback()


def _sample_company(conn) -> str:
    ec = conn.execute(text(
        "SELECT edinet_code FROM financial_records WHERE period_type='annual'"
        " AND pl_revenue IS NOT NULL ORDER BY edinet_code LIMIT 1")).scalar()
    assert ec, "annual 行が1つも無い DB では検証できない"
    return ec


class TestViewShape:
    def test_columns_match_the_orm(self, conn):
        actual = [r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = :v ORDER BY ordinal_position"), {"v": VIEW})]
        assert set(actual) == {c.name for c in
                               database.FinancialMetricWithTTM.__table__.columns}

    def test_security_invoker_can_be_set(self, conn):
        conn.execute(text(f"ALTER VIEW {VIEW} SET (security_invoker = true)"))


class TestAnnualRowsAreUnchanged:
    """**通期の行は `financial_metrics` と 1 行も違わない。**

    ここが崩れると、子3 のゲートが「TTM の効果」として測るものに式の差が混ざる。
    どちらも妥当な数字なので、測ってみるまで気づけない。
    """

    def _cols(self) -> str:
        skip = {"basis", "filing_date"}
        return ", ".join(c.name for c in database.FinancialMetric.__table__.columns
                         if c.name not in skip)

    def test_no_row_differs_in_either_direction(self, conn):
        cols = self._cols()
        for a, b in ((f"SELECT {cols} FROM financial_metrics",
                      f"SELECT {cols} FROM {VIEW} WHERE basis = 'annual'"),
                     (f"SELECT {cols} FROM {VIEW} WHERE basis = 'annual'",
                      f"SELECT {cols} FROM financial_metrics")):
            n = conn.execute(text(f"SELECT count(*) FROM (({a}) EXCEPT ({b})) t")).scalar()
            assert n == 0

    def test_row_counts_agree(self, conn):
        a = conn.execute(text("SELECT count(*) FROM financial_metrics")).scalar()
        b = conn.execute(text(
            f"SELECT count(*) FROM {VIEW} WHERE basis = 'annual'")).scalar()
        assert a == b


class TestTtmRows:
    def _insert(self, conn, ec: str, **over):
        vals = dict(edinet_code=ec, year=2026, period_end=date(2025, 9, 30),
                    filing_date=date(2025, 11, 14), industry="小売業",
                    pl_revenue=1000.0, pl_operating_profit=100.0, pl_net_income=60.0,
                    pl_eps=60.0, cf_operating_cf=90.0, bs_total_assets=5000.0,
                    bs_total_equity=2000.0, bs_bps=200.0, bs_current_assets=3000.0,
                    bs_total_liabilities=1000.0, issued_shares=10.0, dps=10.0,
                    stock_price=1200.0, per=20.0, pbr=6.0, market_cap=12000.0,
                    div_yield=0.83, split_factor=2.0, source="TTM_COMPOSITE")
        vals.update(over)
        cols = ", ".join(vals)
        conn.execute(text(f"INSERT INTO ttm_financial_records ({cols}) "
                          f"VALUES ({', '.join(':' + k for k in vals)})"), vals)

    def test_ttm_row_appears_with_its_basis_and_correction(self, conn):
        ec = _sample_company(conn)
        self._insert(conn, ec)
        row = conn.execute(text(
            f"SELECT basis, per, pbr, market_cap, div_yield, split_factor, filing_date,"
            f" stock_price, nc_ratio FROM {VIEW}"
            f" WHERE edinet_code = :ec AND basis = 'ttm'"), {"ec": ec}).one()
        assert row.basis == "ttm" and row.split_factor == 2.0
        # 向きは通期と同じ（per/pbr/market_cap は ×F、div_yield は ÷F・ADR-0055）。
        assert (row.per, row.pbr, row.market_cap) == (40.0, 12.0, 24000.0)
        assert row.div_yield == pytest.approx(0.42, abs=0.01)
        assert row.stock_price == 1200.0        # 株価そのものは補正しない
        assert row.filing_date == date(2025, 11, 14)

    def test_growth_needs_the_previous_year_on_the_same_basis(self, conn):
        """間の空いた年度で「2年前との比」を成長率として出さない。"""
        ec = _sample_company(conn)
        self._insert(conn, ec, year=2024, period_end=date(2023, 9, 30),
                     filing_date=date(2023, 11, 14), pl_revenue=800.0)
        self._insert(conn, ec, year=2026, pl_revenue=1000.0)
        rows = {r.year: r.rev_growth for r in conn.execute(text(
            f"SELECT year, rev_growth FROM {VIEW}"
            f" WHERE edinet_code = :ec AND basis = 'ttm'"), {"ec": ec})}
        assert rows[2026] is None and rows[2024] is None

    def test_growth_is_computed_for_consecutive_years(self, conn):
        ec = _sample_company(conn)
        self._insert(conn, ec, year=2025, period_end=date(2024, 9, 30),
                     filing_date=date(2024, 11, 14), pl_revenue=800.0)
        self._insert(conn, ec, year=2026, pl_revenue=1000.0)
        rows = {r.year: r.rev_growth for r in conn.execute(text(
            f"SELECT year, rev_growth FROM {VIEW}"
            f" WHERE edinet_code = :ec AND basis = 'ttm'"), {"ec": ec})}
        assert rows[2026] == pytest.approx(25.0)

    def test_zscore_is_taken_within_the_same_basis(self, conn):
        """TTM 行の Zスコアは TTM 行どうしで測る（通期の分布を借りない）。"""
        ec = _sample_company(conn)
        self._insert(conn, ec, year=2026, pl_revenue=1000.0)
        z = conn.execute(text(
            f"SELECT z_revenue FROM {VIEW} WHERE edinet_code = :ec AND basis = 'ttm'"),
            {"ec": ec}).scalar()
        assert z is None      # 同じ年度・同じ基準の行が1本だけ＝COUNT >= 2 を満たさない

    def test_gap_ratio_is_null_for_ttm_rows(self, conn):
        ec = _sample_company(conn)
        self._insert(conn, ec)
        gap = conn.execute(text(
            f"SELECT gap_ratio FROM {VIEW} WHERE edinet_code = :ec AND basis = 'ttm'"),
            {"ec": ec}).scalar()
        assert gap is None
