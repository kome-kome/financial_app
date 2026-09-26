"""期末後分割の年の行の基準の遅れ（#753・ADR-0055 決定4-11）を**実 PostgreSQL** の VIEW で確かめる。

## なぜ別ファイルなのか

CI（`conftest.py`）は ORM の列から実テーブルを作るだけで、VIEW の SQL は 1 文字も実行されない。
`tests/test_split_adjustment_factors.py` は補正の向きと遅れの列をソースで縛るところまでしか言えないので、
ここで実在の通期行に遅れ入りの係数行を仕込み、2 本の VIEW（`financial_metrics` /
`financial_metrics_with_ttm`）が同じ値を出すことを確かめる。

`FINAPP_TEST_PG_URL` が設定されているときだけ走り、**CI では skip される**（規約は
`tests/test_financial_metrics_with_ttm_postgres.py` と同じ）。

**書き込みはすべてトランザクションの中で行い、最後に必ず取り消す。** 係数表と VIEW に
ACCESS EXCLUSIVE を取るので、夜間バッチ（JST 17:20）や日中枠と重ねて走らせないこと。

実行:
    $env:FINAPP_TEST_PG_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
    pytest tests/test_split_basis_lag_postgres.py -v
"""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

PG_URL = os.environ.get("FINAPP_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="FINAPP_TEST_PG_URL 未設定（ローカル PostgreSQL がある環境でのみ実行）",
)

ANNUAL = "financial_metrics"
WITH_TTM = "financial_metrics_with_ttm"
VIEWS = (ANNUAL, WITH_TTM)


@pytest.fixture
def conn():
    """**取り消されるトランザクション**。本番の表・VIEW に痕跡を残さない。"""
    with database.engine.connect() as c:
        trans = c.begin()
        try:
            # 移行前の DB でも VIEW を作れるよう、遅れの列をここで足す（本番の移行は `_ensure_tables`）。
            for col in ("shares_lag", "dps_lag"):
                c.execute(text(
                    f"ALTER TABLE split_adjustment_factors ADD COLUMN IF NOT EXISTS {col} "
                    "DOUBLE PRECISION NOT NULL DEFAULT 1.0"))
            database.TtmFinancialRecord.__table__.create(bind=c, checkfirst=True)
            for name, sql in ((ANNUAL, database.FINANCIAL_METRICS_VIEW_SQL),
                              (WITH_TTM, database.FINANCIAL_METRICS_WITH_TTM_VIEW_SQL)):
                c.execute(text(f"DROP VIEW IF EXISTS {name}"))
                c.execute(text(sql))
            yield c
        finally:
            trans.rollback()


def _sample(conn):
    """market_cap・div_yield・per・pbr・予測時価総額・ネットキャッシュがすべて出る通期行を1つ選ぶ。

    同じ year に通期行が2本ある社（会計期間変更）は避ける（係数表のキーは (edinet_code, year)）。
    """
    row = conn.execute(text(
        "SELECT fr.edinet_code, fr.year, fr.period_end, fr.market_cap, fr.div_yield, fr.per, fr.pbr,"
        "       rr.predicted_market_cap"
        "  FROM financial_records fr"
        "  JOIN regression_results rr ON rr.edinet_code = fr.edinet_code AND rr.year = fr.year"
        "   AND rr.period_end = fr.period_end"
        " WHERE fr.period_type = 'annual' AND fr.market_cap > 0 AND fr.div_yield > 0"
        "   AND fr.per > 0 AND fr.pbr > 0 AND rr.predicted_market_cap > 0"
        "   AND fr.bs_current_assets IS NOT NULL AND fr.bs_total_liabilities IS NOT NULL"
        "   AND NOT EXISTS (SELECT 1 FROM financial_records d WHERE d.edinet_code = fr.edinet_code"
        "                   AND d.year = fr.year AND d.period_type = 'annual' AND d.id <> fr.id)"
        " ORDER BY fr.edinet_code, fr.year LIMIT 1")).first()
    assert row is not None, "条件を満たす通期行が無い DB では検証できない"
    return row


def _set_factor(conn, s, *, factor: float, shares_lag: float, dps_lag: float) -> None:
    conn.execute(text("DELETE FROM split_adjustment_factors WHERE edinet_code = :ec AND year = :y"),
                 {"ec": s.edinet_code, "y": s.year})
    conn.execute(text(
        "INSERT INTO split_adjustment_factors"
        " (edinet_code, year, factor, shares_lag, dps_lag, n_events, kinds, computed_at)"
        " VALUES (:ec, :y, :f, :sl, :dl, 0, NULL, now())"),
        {"ec": s.edinet_code, "y": s.year, "f": factor, "sl": shares_lag, "dl": dps_lag})


def _drop_factor(conn, s) -> None:
    conn.execute(text("DELETE FROM split_adjustment_factors WHERE edinet_code = :ec AND year = :y"),
                 {"ec": s.edinet_code, "y": s.year})


def _read(conn, view: str, s):
    basis = " AND basis = 'annual'" if view == WITH_TTM else ""
    return conn.execute(text(
        "SELECT market_cap, div_yield, per, pbr, net_cash, nc_ratio, predicted_market_cap,"
        f"      split_factor, split_shares_lag, split_dps_lag FROM {view}"
        f" WHERE edinet_code = :ec AND year = :y AND period_end = :pe{basis}"),
        {"ec": s.edinet_code, "y": s.year, "pe": s.period_end}).one()


@pytest.mark.parametrize("view", VIEWS)
class TestLagArithmetic:
    def test_split_year_row_lags_only_the_shares_and_dividend_columns(self, conn, view):
        """F=1.0・遅れ 2.0 の行（期末後分割の年の行）: market_cap ×2・div_yield ÷2・予測時価総額 ×2。

        per / pbr は分母が分割後の基準の1株指標なので動かない（動いたら正しい値を壊している）。
        """
        s = _sample(conn)
        _set_factor(conn, s, factor=1.0, shares_lag=2.0, dps_lag=2.0)
        r = _read(conn, view, s)
        assert r.market_cap == pytest.approx(s.market_cap * 2, abs=0.011)
        assert r.div_yield == pytest.approx(s.div_yield / 2, abs=0.011)
        assert (r.per, r.pbr) == (pytest.approx(s.per, abs=0.011), pytest.approx(s.pbr, abs=0.011))
        assert r.predicted_market_cap == s.predicted_market_cap * 2
        # nc_ratio は補正後の market_cap から計算される（遅れを別に当てない）。net_cash / nc_ratio は
        # VIEW の中で numeric のまま（Decimal で返る）なので float へ直して比べる。
        assert float(r.nc_ratio) == pytest.approx(
            float(r.net_cash) / (r.market_cap * 1_000_000), abs=1e-4)
        assert (r.split_factor, r.split_shares_lag, r.split_dps_lag) == (1.0, 2.0, 2.0)

    def test_factor_and_lags_multiply_but_the_prediction_takes_only_the_lag(self, conn, view):
        """F と遅れは掛け合わせる。予測時価総額は保存時点で F=1 なので、遅れだけを掛ける。"""
        s = _sample(conn)
        _set_factor(conn, s, factor=3.0, shares_lag=2.0, dps_lag=1.0)
        r = _read(conn, view, s)
        assert r.market_cap == pytest.approx(s.market_cap * 6, abs=0.011)
        assert r.div_yield == pytest.approx(s.div_yield / 3, abs=0.011)
        assert r.per == pytest.approx(s.per * 3, abs=0.011)
        assert r.predicted_market_cap == s.predicted_market_cap * 2

    def test_row_without_a_factor_row_is_left_alone(self, conn, view):
        s = _sample(conn)
        _drop_factor(conn, s)
        r = _read(conn, view, s)
        assert r.market_cap == pytest.approx(s.market_cap, abs=0.011)
        assert r.div_yield == pytest.approx(s.div_yield, abs=0.011)
        assert r.predicted_market_cap == s.predicted_market_cap
        assert (r.split_factor, r.split_shares_lag, r.split_dps_lag) == (1.0, 1.0, 1.0)


def test_both_views_agree_on_the_lagged_row(conn):
    """遅れを持つ通期の行でも、2本の VIEW は1列も違わない（with_ttm の契約）。"""
    s = _sample(conn)
    _set_factor(conn, s, factor=1.5, shares_lag=2.0, dps_lag=2.0)
    assert tuple(_read(conn, ANNUAL, s)) == tuple(_read(conn, WITH_TTM, s))
