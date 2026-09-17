"""TTM 表の作り直し（#424 子2・ADR-0051）の I/O 側を SQLite で確かめる。

純関数の検証は `tests/test_ttm_composite.py`。ここで縛るのは**表に触る/触らないの規約**である。
全置換は「消してから書く」ので、途中で失敗したときに何が残るかが挙動を決める。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ttm_composite as T  # noqa: E402
from database import (FinancialRecord, StockPriceWeekly, TtmFinancialRecord,  # noqa: E402
                      replace_ttm_financial_records, ttm_financial_columns)


def seed(db, *, ec="E00001", split=False):
    """3 材料（3月期・H1 は 9月末）と、期末近傍の週次株価を入れる。"""
    rows = [
        FinancialRecord(edinet_code=ec, year=2025, period_type="annual",
                        period_end=date(2025, 3, 31), industry="小売業",
                        pl_revenue=1000.0, pl_operating_profit=100.0, pl_net_income_attr=60.0,
                        pl_eps=60.0, cf_operating_cf=90.0, bs_total_assets=5000.0,
                        bs_total_equity=2000.0, bs_bps=200.0, issued_shares=10.0, dps=10.0),
        FinancialRecord(edinet_code=ec, year=2025, period_type="H1",
                        period_end=date(2024, 9, 30), filing_date=date(2024, 11, 14),
                        industry="小売業",
                        pl_revenue=500.0, pl_operating_profit=50.0, pl_net_income_attr=30.0,
                        pl_eps=30.0, cf_operating_cf=45.0, bs_total_assets=4900.0,
                        bs_total_equity=1950.0, issued_shares=10.0),
        FinancialRecord(edinet_code=ec, year=2026, period_type="H1",
                        period_end=date(2025, 9, 30), filing_date=date(2025, 11, 14),
                        industry="小売業",
                        pl_revenue=550.0, pl_operating_profit=60.0, pl_net_income_attr=35.0,
                        pl_eps=35.0, cf_operating_cf=50.0, bs_total_assets=5200.0,
                        bs_total_equity=2100.0,
                        issued_shares=20.0 if split else 10.0),
    ]
    db.add_all(rows)
    db.add(StockPriceWeekly(edinet_code=ec, week_start="2025-09-29", trade_date="2025-10-03",
                            close_last=1500.0, volume_sum=1.0, turnover_sum=1.0, n_days=5))
    db.commit()


class TestRebuild:
    def test_writes_one_row_per_company_year(self, db):
        seed(db)
        n = T.rebuild_ttm_financial_records(db)
        assert n == 1
        (r,) = db.query(TtmFinancialRecord).all()
        assert (r.edinet_code, r.year, r.period_end) == ("E00001", 2026, date(2025, 9, 30))
        assert r.pl_revenue == pytest.approx(1000.0 - 500.0 + 550.0)
        assert r.source == "TTM_COMPOSITE" and r.split_factor == 1.0
        assert r.filing_date == date(2025, 11, 14)
        assert r.prev_annual_period_end == date(2025, 3, 31)

    def test_market_values_are_computed_from_the_price(self, db):
        seed(db)
        T.rebuild_ttm_financial_records(db)
        (r,) = db.query(TtmFinancialRecord).all()
        # その社の最新の行なので現在株価（週次にしか値が無いので 1500）。
        assert r.stock_price == 1500.0
        assert r.per == pytest.approx(1500.0 / r.pl_eps, rel=1e-3)

    def test_split_between_materials_is_not_composed(self, db):
        """株数が倍になった社は作らない（1株指標の基準が混ざるため）。"""
        seed(db, split=True)
        with pytest.raises(RuntimeError):
            T.rebuild_ttm_financial_records(db)
        assert db.query(TtmFinancialRecord).count() == 0

    def test_full_replace_drops_previous_rows(self, db):
        seed(db)
        db.add(TtmFinancialRecord(edinet_code="E99999", year=1999))
        db.commit()
        T.rebuild_ttm_financial_records(db)
        assert {r.edinet_code for r in db.query(TtmFinancialRecord).all()} == {"E00001"}

    def test_no_h1_rows_keeps_the_table(self, db):
        """H1 が 0 件＝入力そのものが無い。既存の表に触らず 0 を返す。"""
        db.add(FinancialRecord(edinet_code="E00001", year=2025, period_type="annual",
                               period_end=date(2025, 3, 31)))
        db.add(TtmFinancialRecord(edinet_code="E00001", year=2024))
        db.commit()
        assert T.rebuild_ttm_financial_records(db) == 0
        assert db.query(TtmFinancialRecord).count() == 1

    def test_nothing_composable_raises_and_keeps_the_table(self, db):
        """入力はあるのに 1 件も作れない＝合成か判定が壊れた側。表は温存する。"""
        seed(db, split=True)
        db.add(TtmFinancialRecord(edinet_code="E00001", year=2024))
        db.commit()
        with pytest.raises(RuntimeError):
            T.rebuild_ttm_financial_records(db)
        db.rollback()
        assert db.query(TtmFinancialRecord).count() == 1


class TestTable:
    def test_financial_columns_mirror_financial_records(self):
        """財務列は `FinancialRecord` の複製＝再分類項目を増やす場所は1箇所のまま。"""
        src = {c.name for c in FinancialRecord.__table__.columns}
        ttm = {c.name for c in TtmFinancialRecord.__table__.columns}
        assert set(ttm_financial_columns()) <= src
        assert set(ttm_financial_columns()) <= ttm
        # 期種は持たない（基準は VIEW の `basis` が表す）。提出日は H1 のものを持つ。
        assert "period_type" not in ttm and "filing_date" in ttm

    def test_no_xbrl_tags_are_duplicated(self):
        """複製した列が `info["xbrl"]` を持つと `build_xbrl_map()` が重複で落ちる。"""
        assert all(not c.info.get("xbrl") for c in TtmFinancialRecord.__table__.columns)

    def test_unknown_key_raises(self, db):
        with pytest.raises(ValueError):
            replace_ttm_financial_records(db, [{"edinet_code": "E1", "year": 2026,
                                                "mystery": 1.0}])
