"""株価2本立て（daily + weekly）の集約・派生・解像度自動切替・trim のテスト。

純粋関数 aggregate_weeks と、読み取りヘルパ prices_on_or_after / latest_prices /
trim_daily を in-memory SQLite で検証する（record_prices_batch は pg_insert 依存の
ため SQLite では走らせず、集約ロジック＝aggregate_weeks と読み取り側で担保する）。
"""
from datetime import date, timedelta

from database import (
    StockPriceDaily, StockPriceWeekly,
    aggregate_weeks, iso_week_start, trim_daily,
    prices_on_or_after, latest_prices, DAILY_WINDOW_DAYS,
)


def _d(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


# ── iso_week_start ──────────────────────────────────────────────────────────

class TestIsoWeekStart:
    def test_monday_is_its_own_week_start(self):
        assert iso_week_start("2026-06-01") == "2026-06-01"   # 月曜

    def test_friday_maps_back_to_monday(self):
        assert iso_week_start("2026-06-05") == "2026-06-01"   # 金 → 同週月曜

    def test_sunday_maps_to_that_weeks_monday(self):
        assert iso_week_start("2026-06-07") == "2026-06-01"   # 日 → 同週月曜


# ── aggregate_weeks（純粋関数）─────────────────────────────────────────────

class TestAggregateWeeks:
    def test_close_last_is_last_trading_day(self):
        rows = [
            ("E1", "2026-06-01", 100.0, 10.0),
            ("E1", "2026-06-05", 110.0, 20.0),   # 週内最終営業日
            ("E1", "2026-06-03", 105.0, 30.0),
        ]
        out = aggregate_weeks(rows)
        assert len(out) == 1
        w = out[0]
        assert w["week_start"] == "2026-06-01"
        assert w["trade_date"] == "2026-06-05"
        assert w["close_last"] == 110.0      # 順不同入力でも最終営業日の終値
        assert w["n_days"] == 3

    def test_volume_and_turnover_sums(self):
        rows = [
            ("E1", "2026-06-01", 100.0, 10.0),
            ("E1", "2026-06-02", 200.0, 30.0),
        ]
        w = aggregate_weeks(rows)[0]
        assert w["volume_sum"] == 40.0
        assert w["turnover_sum"] == 100.0 * 10.0 + 200.0 * 30.0   # 7000
        # VWAP（派生）= turnover/volume = 7000/40 = 175（単純平均150ではない＝出来高加重）
        assert round(w["turnover_sum"] / w["volume_sum"], 4) == 175.0

    def test_volume_missing_yields_none_sums(self):
        rows = [
            ("E1", "2026-06-01", 100.0, None),
            ("E1", "2026-06-02", 110.0, None),
        ]
        w = aggregate_weeks(rows)[0]
        assert w["volume_sum"] is None
        assert w["turnover_sum"] is None
        assert w["close_last"] == 110.0      # VWAP 派生不可の週は close_last にフォールバック

    def test_partial_volume_counts_only_present_days(self):
        rows = [
            ("E1", "2026-06-01", 100.0, 10.0),
            ("E1", "2026-06-02", 110.0, None),   # volume 欠落日は加重から除外
        ]
        w = aggregate_weeks(rows)[0]
        assert w["volume_sum"] == 10.0
        assert w["turnover_sum"] == 1000.0
        assert w["n_days"] == 2               # 営業日数は両方数える

    def test_groups_by_week_and_company(self):
        rows = [
            ("E1", "2026-06-01", 100.0, 1.0),
            ("E1", "2026-06-08", 120.0, 1.0),   # 翌週
            ("E2", "2026-06-01", 500.0, 1.0),   # 別会社・同週
        ]
        out = {(w["edinet_code"], w["week_start"]): w for w in aggregate_weeks(rows)}
        assert set(out.keys()) == {
            ("E1", "2026-06-01"), ("E1", "2026-06-08"), ("E2", "2026-06-01")
        }

    def test_skips_rows_without_close(self):
        rows = [("E1", "2026-06-01", None, 10.0), ("E1", "2026-06-02", 110.0, 10.0)]
        w = aggregate_weeks(rows)[0]
        assert w["n_days"] == 1
        assert w["close_last"] == 110.0


# ── prices_on_or_after（エントリー価格・解像度自動切替）──────────────────────

class TestPricesOnOrAfter:
    def test_recent_entry_uses_daily(self, db):
        db.add(StockPriceDaily(edinet_code="E1", trade_date=_d(-10), close=111.0))
        db.add(StockPriceWeekly(edinet_code="E1", week_start=iso_week_start(_d(-12)),
                                trade_date=_d(-12), close_last=999.0))
        db.commit()
        out = prices_on_or_after(db, ["E1"], _d(-20))   # after は窓内 → daily
        assert out["E1"]["price"] == 111.0

    def test_old_entry_uses_weekly(self, db):
        old = _d(-400)
        db.add(StockPriceWeekly(edinet_code="E1", week_start=iso_week_start(old),
                                trade_date=old, close_last=80.0))
        db.commit()
        out = prices_on_or_after(db, ["E1"], _d(-410))   # after が窓外 → weekly
        assert out["E1"]["price"] == 80.0

    def test_falls_back_to_weekly_when_daily_missing(self, db):
        # after は窓内だが daily に該当行が無い → weekly にフォールバック
        db.add(StockPriceWeekly(edinet_code="E1", week_start=iso_week_start(_d(-5)),
                                trade_date=_d(-5), close_last=77.0))
        db.commit()
        out = prices_on_or_after(db, ["E1"], _d(-20))
        assert out["E1"]["price"] == 77.0

    def test_empty_codes(self, db):
        assert prices_on_or_after(db, [], _d(-10)) == {}


# ── latest_prices（イグジット="now"・daily 優先）────────────────────────────

class TestLatestPrices:
    def test_prefers_daily_over_weekly(self, db):
        db.add(StockPriceDaily(edinet_code="E1", trade_date=_d(-2), close=150.0))
        db.add(StockPriceWeekly(edinet_code="E1", week_start=iso_week_start(_d(-9)),
                                trade_date=_d(-9), close_last=140.0))
        db.commit()
        out = latest_prices(db, ["E1"])
        assert out["E1"]["price"] == 150.0

    def test_falls_back_to_weekly_when_no_daily(self, db):
        db.add(StockPriceWeekly(edinet_code="E1", week_start=iso_week_start(_d(-9)),
                                trade_date=_d(-9), close_last=140.0))
        db.commit()
        out = latest_prices(db, ["E1"])
        assert out["E1"]["price"] == 140.0


# ── trim_daily（ローリング削除）─────────────────────────────────────────────

class TestTrimDaily:
    def test_removes_rows_older_than_window(self, db):
        db.add(StockPriceDaily(edinet_code="E1", trade_date=_d(-10), close=100.0))
        db.add(StockPriceDaily(edinet_code="E1",
                               trade_date=_d(-(DAILY_WINDOW_DAYS + 30)), close=90.0))
        db.commit()
        removed = trim_daily(db)
        assert removed == 1
        remaining = [r.trade_date for r in db.query(StockPriceDaily).all()]
        assert remaining == [_d(-10)]
