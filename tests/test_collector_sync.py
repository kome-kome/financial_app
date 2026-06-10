"""collector.update_market_data_from_history のユニットテスト。

同期関数で db Session を直接受け取るため、conftest の in-memory SQLite で完結する。
point_in_time=False（最新株価で最新レコードを更新）と
point_in_time=True（全レコードを period_end 近傍の週次株価で更新）の2分岐をカバー。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import update_market_data_from_history


class TestUpdateMarketDataDefault:
    """point_in_time=False（デフォルト）: 最新株価で最新レコードを更新"""

    def test_empty_db_returns_zero(self, db):
        assert update_market_data_from_history(db) == 0

    def test_financial_records_without_prices_returns_zero(self, db, make_fin):
        db.add(make_fin())
        db.commit()
        assert update_market_data_from_history(db) == 0

    def test_prices_without_financial_records_returns_zero(self, db, make_price):
        db.add(make_price(close=1000.0))
        db.commit()
        assert update_market_data_from_history(db) == 0

    def test_updates_stock_price(self, db, make_fin, make_price):
        rec = make_fin()
        db.add(rec)
        db.add(make_price(close=1500.0))
        db.commit()
        count = update_market_data_from_history(db)
        assert count == 1
        db.refresh(rec)
        assert rec.stock_price == 1500.0

    def test_calculates_pbr_from_bps(self, db, make_fin, make_price):
        rec = make_fin(bs_bps=500.0)
        db.add(rec)
        db.add(make_price(close=1000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.pbr == pytest.approx(2.0)

    def test_calculates_per_from_eps(self, db, make_fin, make_price):
        rec = make_fin(pl_eps=50.0)
        db.add(rec)
        db.add(make_price(close=1000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.per == pytest.approx(20.0)

    def test_skips_zero_price(self, db, make_fin, make_price):
        rec = make_fin()
        db.add(rec)
        db.add(make_price(close=0.0))
        db.commit()
        assert update_market_data_from_history(db) == 0
        db.refresh(rec)
        assert rec.stock_price is None

    def test_skips_negative_price(self, db, make_fin, make_price):
        rec = make_fin()
        db.add(rec)
        db.add(make_price(close=-10.0))
        db.commit()
        assert update_market_data_from_history(db) == 0

    def test_updates_only_latest_record_per_company(self, db, make_fin, make_price):
        old_rec = make_fin(year=2021, period_end="2021-03-31")
        new_rec = make_fin(year=2023, period_end="2023-03-31")
        db.add(old_rec); db.add(new_rec)
        db.add(make_price(close=2000.0))
        db.commit()
        count = update_market_data_from_history(db)
        assert count == 1
        db.refresh(old_rec)
        db.refresh(new_rec)
        # 最新レコード（2023年）が更新される
        assert new_rec.stock_price == 2000.0
        assert old_rec.stock_price is None

    def test_two_companies_updated_independently(self, db, make_fin, make_price):
        rec1 = make_fin(edinet_code="E00001")
        rec2 = make_fin(edinet_code="E00002", year=2022, period_end="2022-03-31")
        db.add(rec1); db.add(rec2)
        db.add(make_price(edinet_code="E00001", close=1000.0))
        db.add(make_price(edinet_code="E00002", close=2000.0, trade_date="2023-01-05"))
        db.commit()
        count = update_market_data_from_history(db)
        assert count == 2
        db.refresh(rec1); db.refresh(rec2)
        assert rec1.stock_price == 1000.0
        assert rec2.stock_price == 2000.0


class TestUpdateMarketDataPointInTime:
    """point_in_time=True: 全財務レコードを period_end 近傍の週次株価で更新"""

    def test_empty_weekly_returns_zero(self, db, make_fin):
        db.add(make_fin())
        db.commit()
        assert update_market_data_from_history(db, point_in_time=True) == 0

    def test_empty_financial_records_returns_zero(self, db, make_weekly):
        db.add(make_weekly(close_last=1000.0))
        db.commit()
        assert update_market_data_from_history(db, point_in_time=True) == 0

    def test_updates_by_nearest_period_end(self, db, make_fin, make_weekly):
        rec = make_fin(period_end="2023-03-31", bs_bps=1000.0)
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-31", close_last=3000.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price == 3000.0
        assert rec.pbr == pytest.approx(3.0)

    def test_period_end_none_skips_bisect_but_gets_latest_price(self, db, make_fin, make_weekly):
        # period_end なしは二分探索をスキップするが、最終ステップ（latest_prices）で
        # 最新週次株価が適用される（スクリーニング用の最新株価上書き）。
        rec = make_fin(period_end=None)
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-31", close_last=1000.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price == 1000.0

    def test_skips_zero_weekly_price(self, db, make_fin, make_weekly):
        rec = make_fin(period_end="2023-03-31")
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-31", close_last=0.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price is None
