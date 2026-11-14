"""売買の記録からの保有・損益・期間リターン（#1222）。

値はすべて作り物（リポジトリは public＝実際の売買を書かない）。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import portfolio_ledger as pl
from database import (
    Company, JQuantsAdjFactorEvent, MacroData, StockPriceWeekly, TradeLedgerEntry,
    insert_trade_ledger_entry,
)

TODAY = date(2026, 6, 30)


def _row(i, side, code="1111", day="2026-01-05", price=None, qty=None, ratio=None, fee=0.0,
         voids=None):
    return {"id": i, "side": side, "sec_code": code, "trade_date": day, "price": price,
            "quantity": qty, "split_ratio": ratio, "fee": fee, "voids_id": voids, "memo": None}


def _buy(i, day, price, qty, code="1111", fee=0.0):
    return _row(i, pl.SIDE_BUY, code, day, price, qty, fee=fee)


def _sell(i, day, price, qty, code="1111", fee=0.0):
    return _row(i, pl.SIDE_SELL, code, day, price, qty, fee=fee)


def _split(i, day, ratio, code="1111"):
    return _row(i, pl.SIDE_SPLIT, code, day, ratio=ratio)


def _void(i, target):
    return _row(i, pl.SIDE_VOID, code=None, day=None, voids=target)


def _book(rows):
    return pl.positions(pl.to_current_scale(pl.effective_entries(rows)))


class TestValidateEntry:
    def test_buy_keeps_only_its_fields(self):
        e = pl.validate_entry({"side": "BUY", "sec_code": " 130a ", "trade_date": "2026-01-05",
                               "price": 1000, "quantity": 100, "split_ratio": 2, "memo": " M-6 "},
                              today=TODAY)
        assert e == {"side": "buy", "sec_code": "130A", "trade_date": "2026-01-05", "price": 1000.0,
                     "quantity": 100.0, "split_ratio": None, "fee": 0.0, "voids_id": None,
                     "memo": "M-6"}

    def test_three_digit_code_is_padded(self):
        assert pl.normalize_sec_code("123") == "0123"

    def test_split_needs_a_ratio_other_than_one(self):
        base = {"side": "split", "sec_code": "1111", "trade_date": "2026-01-05"}
        assert pl.validate_entry(dict(base, split_ratio=2), today=TODAY)["split_ratio"] == 2.0
        with pytest.raises(ValueError, match="比率が 1"):
            pl.validate_entry(dict(base, split_ratio=1), today=TODAY)

    @pytest.mark.parametrize("patch, msg", [
        ({"side": "hold"}, "売買の別"),
        ({"sec_code": "12345"}, "証券コード"),
        ({"sec_code": "abcd"}, "証券コード"),
        ({"trade_date": "2026/01/05"}, "YYYY-MM-DD"),
        ({"trade_date": "2026-07-01"}, "未来"),
        ({"price": 0}, "単価"),
        ({"price": float("nan")}, "単価"),
        ({"quantity": -1}, "株数"),
        ({"fee": -1}, "手数料"),
        ({"memo": "x" * 201}, "メモ"),
    ])
    def test_rejects(self, patch, msg):
        raw = {"side": "buy", "sec_code": "1111", "trade_date": "2026-01-05", "price": 1000,
               "quantity": 100}
        with pytest.raises(ValueError, match=msg):
            pl.validate_entry(dict(raw, **patch), today=TODAY)

    def test_void_needs_a_target(self):
        assert pl.validate_entry({"side": "void", "voids_id": "3"})["voids_id"] == 3
        with pytest.raises(ValueError, match="voids_id"):
            pl.validate_entry({"side": "void"})


class TestPositions:
    def test_moving_average_cost_includes_buy_fee(self):
        book, problems = _book([_buy(1, "2026-01-05", 1000, 100, fee=500),
                                _buy(2, "2026-01-12", 1200, 100, fee=500)])
        assert problems == []
        assert book["1111"]["quantity"] == 200
        assert book["1111"]["avg_cost"] == pytest.approx((100_500 + 120_500) / 200)
        assert book["1111"]["buy_date"] == "2026-01-05"

    def test_sell_realizes_against_average_cost_and_subtracts_fee(self):
        book, _ = _book([_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-02-02", 1300, 40, fee=200)])
        assert book["1111"]["quantity"] == 60
        assert book["1111"]["avg_cost"] == pytest.approx(1000)
        assert book["1111"]["realized_pnl"] == pytest.approx(300 * 40 - 200)

    def test_rebuy_after_full_exit_resets_buy_date(self):
        book, _ = _book([_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-02-02", 900, 100),
                         _buy(3, "2026-03-02", 800, 50)])
        assert book["1111"]["buy_date"] == "2026-03-02"
        assert book["1111"]["avg_cost"] == pytest.approx(800)
        assert book["1111"]["realized_pnl"] == pytest.approx(-10_000)

    def test_oversell_is_reported_not_dropped(self):
        book, problems = _book([_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-02-02", 1100, 150)])
        assert book["1111"]["quantity"] == 0
        assert book["1111"]["realized_pnl"] == pytest.approx(100 * 100)
        assert len(problems) == 1 and "超えています" in problems[0]

    def test_as_of_ignores_later_trades(self):
        entries = pl.to_current_scale(pl.effective_entries(
            [_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-02-02", 1300, 100)]))
        book, _ = pl.positions(entries, as_of="2026-01-31")
        assert book["1111"]["quantity"] == 100

    def test_void_removes_both_rows(self):
        book, _ = _book([_buy(1, "2026-01-05", 1000, 100), _buy(2, "2026-01-06", 5000, 100),
                         _void(3, 2)])
        assert book["1111"]["quantity"] == 100
        assert book["1111"]["avg_cost"] == pytest.approx(1000)


class TestSplit:
    def test_split_rescales_earlier_trades_only(self):
        """1,000 円 × 100 株 → 1 対 2 の分割 → 今の水準では 500 円 × 200 株。金額は変わらない。"""
        scaled = pl.to_current_scale(pl.effective_entries(
            [_buy(1, "2026-01-05", 1000, 100), _split(2, "2026-02-02", 2.0),
             _buy(3, "2026-03-02", 600, 100)]))
        assert [(e["price"], e["quantity"]) for e in scaled] == [(500.0, 200.0), (600.0, 100.0)]
        assert all(e["side"] != pl.SIDE_SPLIT for e in scaled)

    def test_without_the_split_row_the_same_trades_look_like_a_loss(self):
        """分割の行が無いと、今の水準の株価（500 円）に対して 50% の損に見える＝行が要る理由。"""
        with_split, _ = _book([_buy(1, "2026-01-05", 1000, 100), _split(2, "2026-02-02", 2.0)])
        without, _ = _book([_buy(1, "2026-01-05", 1000, 100)])
        assert with_split["1111"]["avg_cost"] == pytest.approx(500)
        assert without["1111"]["avg_cost"] == pytest.approx(1000)

    def test_trade_on_the_split_day_is_already_post_split(self):
        scaled = pl.to_current_scale(pl.effective_entries(
            [_split(1, "2026-02-02", 2.0), _buy(2, "2026-02-02", 500, 100)]))
        assert (scaled[0]["price"], scaled[0]["quantity"]) == (500, 100)

    def test_split_of_another_code_does_not_apply(self):
        scaled = pl.to_current_scale(pl.effective_entries(
            [_buy(1, "2026-01-05", 1000, 100, code="2222"), _split(2, "2026-02-02", 2.0)]))
        assert (scaled[0]["price"], scaled[0]["quantity"]) == (1000, 100)

    def test_sell_after_split_uses_post_split_shares(self):
        book, problems = _book([_buy(1, "2026-01-05", 1000, 100), _split(2, "2026-02-02", 2.0),
                                _sell(3, "2026-03-02", 600, 200)])
        assert problems == []
        assert book["1111"]["quantity"] == 0
        assert book["1111"]["realized_pnl"] == pytest.approx((600 - 500) * 200)


class TestCheckAgainstLedger:
    ROWS = [_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-02-02", 1100, 60)]

    def _entry(self, **kw):
        return pl.validate_entry(dict({"sec_code": "1111"}, **kw), today=TODAY)

    def test_sell_within_holdings_passes(self):
        pl.check_against_ledger(self._entry(side="sell", trade_date="2026-03-02", price=1200,
                                            quantity=40), self.ROWS)

    def test_sell_beyond_holdings_is_rejected(self):
        with pytest.raises(ValueError, match="超えています"):
            pl.check_against_ledger(self._entry(side="sell", trade_date="2026-03-02", price=1200,
                                                quantity=41), self.ROWS)

    def test_backdated_sell_that_strands_a_later_sell_is_rejected(self):
        with pytest.raises(ValueError, match="超えています"):
            pl.check_against_ledger(self._entry(side="sell", trade_date="2026-01-20", price=1200,
                                                quantity=50), self.ROWS)

    def test_voiding_a_buy_that_was_sold_is_rejected(self):
        with pytest.raises(ValueError, match="超えています"):
            pl.check_against_ledger(pl.validate_entry({"side": "void", "voids_id": 1}), self.ROWS)

    def test_void_target_rules(self):
        rows = self.ROWS + [_void(3, 2)]
        for target, msg in [(9, "見つかりません"), (3, "取消の行は"), (2, "すでに")]:
            with pytest.raises(ValueError, match=msg):
                pl.check_against_ledger(pl.validate_entry({"side": "void", "voids_id": target}), rows)

    def test_voiding_a_sell_passes(self):
        pl.check_against_ledger(pl.validate_entry({"side": "void", "voids_id": 2}), self.ROWS)


class TestPerformance:
    # 月曜: 2026-01-05 / 01-12 / 01-19 / 01-26 / 02-02
    BENCH = {"2025-12-29": 100.0, "2026-01-05": 100.0, "2026-01-12": 110.0, "2026-01-19": 110.0,
             "2026-01-26": 99.0, "2026-02-02": 99.0}

    def _perf(self, rows, prices, bench=None):
        return pl.performance(pl.to_current_scale(pl.effective_entries(rows)), prices,
                              self.BENCH if bench is None else bench)

    def test_first_week_counts_from_the_trade_price(self):
        """1,000 円で買って週末 1,100 円 → 最初の週は +10%（買値からの値動きを落とさない）。"""
        perf = self._perf([_buy(1, "2026-01-06", 1000, 100)], {"1111": {"2026-01-05": 1100.0}},
                          bench={"2025-12-29": 100.0, "2026-01-05": 100.0})
        assert perf["weeks"][0]["ret"] == pytest.approx(0.10)
        assert perf["weeks"][0]["bench_ret"] == pytest.approx(0.0)
        assert perf["bench_missing_weeks"] == 0

    def test_buying_more_does_not_count_as_a_gain(self):
        """株価が動かない週に買い増しても、リターンは 0（入金を増減と読まない）。"""
        prices = {"1111": {"2026-01-05": 1000.0, "2026-01-12": 1000.0}}
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100), _buy(2, "2026-01-13", 1000, 300)], prices)
        assert [w["ret"] for w in perf["weeks"][:2]] == pytest.approx([0.0, 0.0])
        assert perf["weeks"][1]["value"] == pytest.approx(400_000)

    def test_full_exit_counts_up_to_the_sale_price(self):
        prices = {"1111": {"2026-01-05": 1000.0, "2026-01-12": 1500.0}}
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-01-14", 1200, 100)], prices)
        assert perf["weeks"][1]["ret"] == pytest.approx(0.20)   # 週末の 1,500 円ではなく売値の 1,200 円
        assert perf["weeks"][1]["value"] == 0

    def test_weeks_without_a_position_are_skipped_on_both_sides(self):
        """全部売った後の週は、こちらにも相手にも入れない（相手だけ下落を拾わない）。"""
        prices = {"1111": {w: 1000.0 for w in self.BENCH}}
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100), _sell(2, "2026-01-13", 1000, 100)], prices)
        assert [w["week"] for w in perf["weeks"]] == ["2026-01-05", "2026-01-12"]
        assert perf["bench_return"] == pytest.approx(0.10)
        assert perf["excess"] == pytest.approx(-0.10)

    def test_total_excess_and_drawdown(self):
        prices = {"1111": {"2026-01-05": 1000.0, "2026-01-12": 1200.0, "2026-01-19": 900.0,
                           "2026-01-26": 900.0, "2026-02-02": 1080.0}}
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100)], prices)
        assert perf["total_return"] == pytest.approx(0.08)
        assert perf["bench_return"] == pytest.approx(-0.01)
        assert perf["excess"] == pytest.approx(0.09)
        assert perf["max_drawdown"] == pytest.approx(900 / 1200 - 1)
        assert perf["bench_max_drawdown"] == pytest.approx(99 / 110 - 1)
        assert perf["n_weeks"] == 5

    def test_months_chain_to_the_total(self):
        prices = {"1111": {"2026-01-05": 1000.0, "2026-01-26": 1100.0, "2026-02-02": 1210.0}}
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100)], prices)
        assert [m["month"] for m in perf["months"]] == ["2026-01", "2026-02"]
        assert [m["ret"] for m in perf["months"]] == pytest.approx([0.10, 0.10])
        assert perf["months"][0]["excess"] == pytest.approx(0.10 - (-0.01))

    def test_missing_price_is_forward_filled_then_falls_back_to_cost(self):
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100), _buy(2, "2026-01-05", 500, 100, code="2222")],
                          {"1111": {"2026-01-05": 1100.0}})
        assert perf["weeks"][1]["value"] == pytest.approx(1100 * 100 + 500 * 100)
        assert perf["problems"] == ["2222 の株価が無いため、約定単価で評価しています"]

    def test_missing_benchmark_weeks_are_counted(self):
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100)],
                          {"1111": {"2026-01-05": 1000.0, "2026-01-12": 1000.0}}, bench={})
        assert perf["bench_missing_weeks"] == perf["n_weeks"] == 2
        assert perf["bench_return"] == pytest.approx(0.0)

    def test_split_does_not_move_the_return(self):
        """分割の行があれば、分割をまたいでも株価が今の水準で横ばいならリターンは 0。"""
        prices = {"1111": {w: 500.0 for w in self.BENCH}}
        perf = self._perf([_buy(1, "2026-01-05", 1000, 100), _split(2, "2026-01-19", 2.0)], prices)
        assert perf["total_return"] == pytest.approx(0.0)

    def test_no_trades(self):
        perf = pl.performance([], {}, {})
        assert perf["total_return"] is None and perf["weeks"] == []


class TestSplitWarnings:
    LIVE = [_buy(1, "2026-01-05", 1000, 100)]

    def test_official_split_without_a_row_warns(self):
        out = pl.split_warnings(self.LIVE, {"1111": [("2026-03-30", 0.5)]})
        assert len(out) == 1 and "2026-03-30" in out[0] and "2 株" in out[0]

    def test_recorded_split_near_the_date_is_quiet(self):
        live = self.LIVE + [_split(2, "2026-04-01", 2.0)]
        assert pl.split_warnings(live, {"1111": [("2026-03-30", 0.5)]}) == []

    @pytest.mark.parametrize("official", [
        {"1111": [("2025-12-29", 0.5)]},     # 持ち始める前
        {"1111": [("2026-03-30", 0.999)]},   # 分割ではない微小な調整
        {"2222": [("2026-03-30", 0.5)]},     # 別の銘柄
    ])
    def test_quiet_cases(self, official):
        assert pl.split_warnings(self.LIVE, official) == []

    def test_sold_out_position_is_quiet(self):
        live = self.LIVE + [_sell(2, "2026-02-02", 1100, 100)]
        assert pl.split_warnings(live, {"1111": [("2026-03-30", 0.5)]}) == []


def test_week_start_matches_database():
    """純関数ブロックに置いた写しが、正本（database.iso_week_start）と食い違わない。"""
    for day in ("2026-01-04", "2026-01-05", "2026-01-11", "2026-12-31", "2024-02-29"):
        assert pl.week_start(day) == database.iso_week_start(day)


def test_sides_cover_what_validate_accepts():
    assert set(pl.SIDES) == {"buy", "sell", "split", "void"}


class TestSummary:
    @pytest.fixture
    def seeded(self, db):
        db.add_all([
            Company(edinet_code="E11111", sec_code="1111", name="作り物A"),
            Company(edinet_code="E22222", sec_code="2222", name="作り物B"),
        ])
        for w, a, b, t in [("2026-01-05", 1000.0, 2000.0, 100.0), ("2026-01-12", 1100.0, 1900.0, 102.0)]:
            db.add_all([
                StockPriceWeekly(edinet_code="E11111", week_start=w, trade_date=w, close_last=a),
                StockPriceWeekly(edinet_code="E22222", week_start=w, trade_date=w, close_last=b),
                MacroData(series_code="TOPIX", trade_date=w, close=t),
            ])
        db.add(MacroData(series_code="TOPIX", trade_date="2025-12-30", close=100.0))
        db.add(JQuantsAdjFactorEvent(edinet_code="E22222", event_date="2026-01-09", adj_factor=0.5))
        db.commit()
        for raw in [
            {"side": "buy", "sec_code": "1111", "trade_date": "2026-01-05", "price": 1000, "quantity": 100},
            {"side": "buy", "sec_code": "2222", "trade_date": "2026-01-05", "price": 2000, "quantity": 50},
            {"side": "buy", "sec_code": "9999", "trade_date": "2026-01-05", "price": 300, "quantity": 10},
        ]:
            insert_trade_ledger_entry(db, pl.validate_entry(raw, today=TODAY))
        return db

    def test_summary_end_to_end(self, seeded):
        s = pl.summary(seeded)
        by_code = {h["sec_code"]: h for h in s["holdings"]}
        assert by_code["1111"]["name"] == "作り物A"
        assert by_code["1111"]["last_close"] == 1100.0
        assert by_code["1111"]["unrealized_pnl"] == pytest.approx(10_000)
        assert by_code["2222"]["unrealized_pct"] == pytest.approx(-5.0)
        assert by_code["9999"]["last_close"] is None and by_code["9999"]["unrealized_pnl"] is None
        assert s["unrealized_pnl"] == pytest.approx(10_000 - 5_000)
        assert s["realized_pnl"] == 0
        assert s["n_entries"] == 3 and s["benchmark"] == "TOPIX"
        # 2 週目: (110,000 + 95,000 + 3,000) / (100,000 + 100,000 + 3,000) − 1
        assert s["performance"]["total_return"] == pytest.approx(208_000 / 203_000 - 1)
        assert s["performance"]["bench_return"] == pytest.approx(0.02)
        assert s["performance"]["bench_missing_weeks"] == 0
        assert len(s["warnings"]) == 1 and s["warnings"][0].startswith("2222")
        assert any("9999" in p and "企業の表に無い" in p for p in s["problems"])
        assert any("9999" in p and "約定単価" in p for p in s["problems"])

    def test_empty_ledger(self, db):
        s = pl.summary(db)
        assert s["holdings"] == [] and s["n_entries"] == 0
        assert s["performance"]["total_return"] is None

    def test_rows_are_append_only(self, seeded):
        """取消は行を足すだけで、元の行を消さない・書き換えない。"""
        before = seeded.query(TradeLedgerEntry).count()
        target = seeded.query(TradeLedgerEntry).filter_by(sec_code="9999").one()
        entry = pl.prepare_entry({"side": "void", "voids_id": target.id}, pl.load_rows(seeded))
        assert (entry["sec_code"], entry["trade_date"]) == ("9999", "2026-01-05")   # 相手から写す
        insert_trade_ledger_entry(seeded, entry)
        assert seeded.query(TradeLedgerEntry).count() == before + 1
        assert seeded.get(TradeLedgerEntry, target.id).price == 300
        assert "9999" not in {h["sec_code"] for h in pl.summary(seeded)["holdings"]}


class TestCurrentHoldings:
    def test_only_open_positions_in_code_order(self, db):
        for raw in [
            {"side": "buy", "sec_code": "2222", "trade_date": "2026-01-05", "price": 2000, "quantity": 50},
            {"side": "buy", "sec_code": "1111", "trade_date": "2026-01-06", "price": 1000, "quantity": 100, "fee": 500},
            {"side": "buy", "sec_code": "3333", "trade_date": "2026-01-05", "price": 300, "quantity": 10},
            {"side": "sell", "sec_code": "3333", "trade_date": "2026-02-02", "price": 310, "quantity": 10},
        ]:
            insert_trade_ledger_entry(db, pl.prepare_entry(raw, pl.load_rows(db), today=TODAY))
        got = pl.current_holdings(db)
        assert [(h["sec_code"], h["quantity"], h["buy_date"]) for h in got] == [
            ("1111", 100, "2026-01-06"), ("2222", 50, "2026-01-05")]
        assert got[0]["avg_cost"] == pytest.approx(1005)

    def test_text_is_what_sell_ranking_parses(self):
        """売り候補ランキングの入力の書式で、取得単価と持ち始めた日が落ちずに読める。"""
        from plugins.sell_ranking import parse_holdings
        text = pl.holdings_text([
            {"sec_code": "1111", "avg_cost": 12345.678, "buy_date": "2026-01-06"},
            {"sec_code": "2222", "avg_cost": 2000.0, "buy_date": None},
        ])
        assert text == "1111, @12345.68, 2026-01-06\n2222, @2000.0"
        parsed, invalid = parse_holdings(text)
        assert invalid == []
        assert parsed == [{"sec_code": "1111", "avg_cost": 12345.68, "buy_date": "2026-01-06"},
                          {"sec_code": "2222", "avg_cost": 2000.0, "buy_date": None}]
