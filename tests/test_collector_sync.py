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

    def test_market_cap_prefers_issued_shares(self, db, make_fin, make_price):
        # issued_shares 直接利用: 1.0e6株 × 2000円 / 1e6 = 2000 百万円
        # 旧実装（bs_total_equity/bs_bps）なら: 1.5e9/500 = 3.0e6株 → 6000 百万円
        rec = make_fin(issued_shares=1.0e6, bs_total_equity=1.5e9, bs_bps=500.0)
        db.add(rec)
        db.add(make_price(close=2000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.market_cap == pytest.approx(2000.0)  # issued_shares 優先

    def test_market_cap_falls_back_to_derived_when_no_issued_shares(self, db, make_fin, make_price):
        # issued_shares なし: bs_total_equity/bs_bps フォールバック
        # 1.0e9/500 = 2.0e6株 × 1000円 / 1e6 = 2000 百万円
        rec = make_fin(bs_total_equity=1.0e9, bs_bps=500.0)
        db.add(rec)
        db.add(make_price(close=1000.0))
        db.commit()
        update_market_data_from_history(db)
        db.refresh(rec)
        assert rec.market_cap == pytest.approx(2000.0)  # フォールバック

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


class TestUpdateMarketDataPointInTimeNearest:
    """point_in_time=True の最近傍探索・日付範囲フィルタ・latest_by_ec 整合の深掘り。

    最新レコードは最終ステップで latest_prices に上書きされるため、近傍探索の挙動は
    「最新でない（year が小さい）レコード」で観測する。
    """

    def test_bisect_selects_nearest_weekly(self, db, make_fin, make_weekly):
        # old_rec(2022) は最新でないため近傍探索の結果がそのまま残る
        old_rec = make_fin(year=2022, period_end="2022-03-31")
        new_rec = make_fin(year=2023, period_end="2023-03-31")  # latest
        db.add_all([old_rec, new_rec])
        # 2022-03-31 近傍: 03-28(差3日) を 03-07(差24日) より優先（bisect で前後2候補比較）
        db.add(make_weekly(trade_date="2022-03-07", close_last=2100.0))
        db.add(make_weekly(trade_date="2022-03-28", close_last=2200.0))
        db.add(make_weekly(trade_date="2023-03-27", close_last=5000.0))  # 最新側
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(old_rec); db.refresh(new_rec)
        assert old_rec.stock_price == 2200.0   # period_end 最近傍
        assert new_rec.stock_price == 5000.0   # latest 上書き

    def test_weekly_beyond_max_gap_not_matched(self, db, make_fin, make_weekly):
        # MAX_GAP_DAYS=30。old_rec の period_end から 30日超離れた weekly は不採用
        old_rec = make_fin(year=2022, period_end="2022-03-31")
        new_rec = make_fin(year=2023, period_end="2023-03-31")  # latest
        db.add_all([old_rec, new_rec])
        db.add(make_weekly(trade_date="2022-05-09", close_last=2200.0))  # 39日差 → 範囲外
        db.add(make_weekly(trade_date="2023-03-27", close_last=5000.0))  # 最新側
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(old_rec); db.refresh(new_rec)
        assert old_rec.stock_price is None     # gap > MAX_GAP_DAYS で不採用・既存値保持
        assert new_rec.stock_price == 5000.0

    def test_latest_record_overwritten_with_latest_price(self, db, make_fin, make_weekly):
        # 単独=最新レコード。近傍(3000)で一旦更新後、最終ステップで最新株価(4000)に上書き。
        # 最新週次(2023-06-26)は period_end±MAX_GAP の weekly 取得範囲外だが、
        # latest_prices は別クエリのため最新終値として引かれる（latest_by_ec 整合）。
        rec = make_fin(year=2023, period_end="2023-03-31", bs_bps=1000.0)
        db.add(rec)
        db.add(make_weekly(trade_date="2023-03-27", close_last=3000.0))
        db.add(make_weekly(trade_date="2023-06-26", close_last=4000.0))
        db.commit()
        update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec)
        assert rec.stock_price == 4000.0       # 近傍3000ではなく最新4000で上書き
        assert rec.pbr == pytest.approx(4.0)

    def test_two_companies_matched_by_own_period_end(self, db, make_fin, make_weekly):
        rec1 = make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31")
        rec2 = make_fin(edinet_code="E00002", sec_code="1002",
                        year=2023, period_end="2023-09-30")
        db.add_all([rec1, rec2])
        db.add(make_weekly(edinet_code="E00001", trade_date="2023-03-27", close_last=1500.0))
        db.add(make_weekly(edinet_code="E00002", trade_date="2023-09-25", close_last=2500.0))
        db.commit()
        n = update_market_data_from_history(db, point_in_time=True)
        db.refresh(rec1); db.refresh(rec2)
        assert rec1.stock_price == 1500.0      # 各社が自社 period_end 近傍で独立にマッチ
        assert rec2.stock_price == 2500.0
        assert n == 2
