"""update_market_data_from_history のユニットテスト (#97)。

point_in_time=False（最新株価で最新レコードを更新）と
point_in_time=True（全レコードを period_end 近傍の週次株価で更新）の
両分岐を SQLite インメモリ DB で検証する。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import update_market_data_from_history


class TestPointInTimeFalse:
    """point_in_time=False: 最新日次株価で最新レコードだけを更新。"""

    def test_updates_stock_price_from_latest_daily(self, db, make_fin, make_price):
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        db.add(make_price(edinet_code="E00001", trade_date="2023-12-01", close=1500.0))
        db.commit()

        updated = update_market_data_from_history(db, point_in_time=False)
        assert updated == 1

        from database import FinancialRecord
        rec = db.query(FinancialRecord).filter_by(edinet_code="E00001").first()
        assert rec.stock_price == 1500.0

    def test_skips_zero_price(self, db, make_fin, make_price):
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        db.add(make_price(edinet_code="E00001", trade_date="2023-12-01", close=0.0))
        db.commit()

        updated = update_market_data_from_history(db, point_in_time=False)
        assert updated == 0

    def test_no_price_data_returns_zero(self, db, make_fin):
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        db.commit()

        updated = update_market_data_from_history(db, point_in_time=False)
        assert updated == 0


class TestPointInTimeTrue:
    """point_in_time=True: 全レコードを period_end 近傍の週次株価で更新。"""

    def test_updates_each_record_with_nearest_weekly_price(self, db, make_fin, make_weekly):
        # 同一社の2期分レコードを登録し、各 period_end に近い週次株価が当たること
        db.add(make_fin(edinet_code="E00001", year=2022, period_end="2022-03-31"))
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        # 各 period_end から7日後の週次株価（MAX_GAP_DAYS=30 以内）
        db.add(make_weekly(edinet_code="E00001", trade_date="2022-04-07", close_last=1000.0))
        db.add(make_weekly(edinet_code="E00001", trade_date="2023-04-07", close_last=2000.0))
        db.commit()

        update_market_data_from_history(db, point_in_time=True)

        from database import FinancialRecord
        rec_2022 = db.query(FinancialRecord).filter_by(
            edinet_code="E00001", year=2022
        ).first()
        rec_2023 = db.query(FinancialRecord).filter_by(
            edinet_code="E00001", year=2023
        ).first()
        # 2022年レコードは2022-04-07の株価（最寄り）
        assert rec_2022.stock_price == 1000.0
        # 2023年レコード（最新）は最新株価=2000.0 で上書き
        assert rec_2023.stock_price == 2000.0

    def test_skips_when_no_weekly_data(self, db, make_fin):
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        db.commit()

        updated = update_market_data_from_history(db, point_in_time=True)
        assert updated == 0

    def test_skips_record_outside_gap_window(self, db, make_fin, make_weekly):
        # period_end="2023-03-31" から60日超の週次データ → MAX_GAP_DAYS=30 超えでスキップ
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2023-06-01", close_last=9999.0))
        db.commit()

        updated = update_market_data_from_history(db, point_in_time=True)
        # 近傍レコードが存在しないため更新数は0
        assert updated == 0
