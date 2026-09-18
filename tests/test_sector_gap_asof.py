"""sector_gap_asof.py のユニットテスト（#626・ADR-0057）。

主眼は**先読みが入らないこと**。月末 D の gap は「D に見えていた財務 × D の株価（×F）」だけで
決まり、D より後に公表される行を足しても1件も変わらないこと。値はもっともらしいまま狂うので、
例外では検出できない（ADR-0055 決定7 と同じ種類の壊れ方）。
"""
import os
import sys
from collections import namedtuple
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.sector_ols import DEFAULT_FEATURES_PRICE, plugin, sector_load_fields
from sector_gap_asof import (
    asof_gap_ratios,
    asof_records,
    attach_gap_ratio,
    load_inputs,
    month_end_closes,
    nightly_params,
)

P = namedtuple("P", "trade_date close_last")
Rec = namedtuple("Rec", sector_load_fields(DEFAULT_FEATURES_PRICE))


def _rec(ec, year, period_end, i, *, scale=1.0, industry="情報・通信業"):
    """回帰が回る程度に分散のある1行（`_seed_sector` と同じ形・scale で値を歪められる）。"""
    bps = (800.0 + 50.0 * i + (i % 4) * 10.0) * scale
    vals = dict.fromkeys(Rec._fields)
    vals.update(
        edinet_code=ec, sec_code=ec[-4:], company_name=ec, industry=industry,
        year=year, period_end=period_end,
        stock_price=1.0, market_cap=1000.0,          # stock_price は asof_records が差し替える
        issued_shares=1.0e6 + 5.0e4 * i,
        bs_total_equity=bps * (1.0e6 + 5.0e4 * i), bs_bps=bps,
        pl_eps=(80.0 + 5.0 * i + (i % 3) * 3.0) * scale,
        dps=20.0 + 2.0 * i,
        pl_revenue=(1.0e9 + 1.0e8 * i) * scale,
        pl_gross_profit=(4.0e8 + 5.0e7 * i + (i % 3) * 1.0e7) * scale,
        pl_operating_profit=(1.0e8 + 2.0e7 * i + (i % 3) * 5.0e6) * scale,
        bs_total_assets=(2.0e9 + 1.0e8 * i + (i % 4) * 5.0e7) * scale,
        bs_total_liabilities=(1.0e9 + 5.0e7 * i + (i % 3) * 2.0e7) * scale,
        cf_operating_cf=(1.2e8 + 1.5e7 * i + (i % 5) * 2.0e6) * scale,
        cf_free_cf=(8.0e7 + 1.0e7 * i + (i % 4) * 1.5e6) * scale,
    )
    return Rec(**vals)


def _panel(n=12, with_future=False):
    """n 社・2022年3月期の行（2023年3月期の行は with_future のときだけ）と、2023-04/05 の週次足。"""
    codes = [f"E{i:05d}" for i in range(1, n + 1)]
    fin = {}
    for i, ec in enumerate(codes, 1):
        rows = [_rec(ec, 2022, date(2022, 3, 31), i)]
        if with_future:
            # 2023-03-31 + 45日 = 2023-05-15。2023-04 の月末からは見えない行に極端な値を入れる。
            rows.append(_rec(ec, 2023, date(2023, 3, 31), n + 1 - i, scale=50.0))
        fin[ec] = rows
    prices = {
        ec: [P("2023-04-21", 1000.0 + 90 * i), P("2023-04-28", 1500.0 + 100 * i + (i % 3) * 50),
             P("2023-05-19", 1600.0 + 95 * i), P("2023-05-26", 1550.0 + 105 * i)]
        for i, ec in enumerate(codes, 1)
    }
    closes = {ec: month_end_closes(rows) for ec, rows in prices.items()}
    return fin, closes


class TestMonthEndCloses:
    def test_last_bar_of_each_month(self):
        rows = [P("2023-04-21", 10.0), P("2023-04-28", 11.0), P("2023-05-05", 12.0)]
        assert month_end_closes(rows) == {"2023-04": ("2023-04-28", 11.0),
                                          "2023-05": ("2023-05-05", 12.0)}

    def test_non_positive_close_is_skipped(self):
        rows = [P("2023-04-28", 0.0), P("2023-05-26", None)]
        assert month_end_closes(rows) == {}

    def test_empty(self):
        assert month_end_closes([]) == {}


class TestAsofRecords:
    def test_row_is_visible_only_after_period_end_plus_45_days(self):
        fin, closes = _panel(n=3, with_future=True)
        april = {r.edinet_code: r.year for r in asof_records(fin, closes, {}, "2023-04")}
        may = {r.edinet_code: r.year for r in asof_records(fin, closes, {}, "2023-05")}
        assert set(april.values()) == {2022}        # 2023-04-28 < 2023-05-15
        assert set(may.values()) == {2023}          # 2023-05-26 >= 2023-05-15

    def test_target_is_month_end_close_times_split_factor(self):
        fin, closes = _panel(n=3)
        recs = {r.edinet_code: r for r in
                asof_records(fin, closes, {("E00002", 2022): 2.0}, "2023-04")}
        assert recs["E00001"].stock_price == pytest.approx(closes["E00001"]["2023-04"][1])
        assert recs["E00002"].stock_price == pytest.approx(2.0 * closes["E00002"]["2023-04"][1])

    def test_factor_of_another_year_does_not_apply(self):
        fin, closes = _panel(n=3)
        recs = {r.edinet_code: r for r in
                asof_records(fin, closes, {("E00002", 2023): 2.0}, "2023-04")}
        assert recs["E00002"].stock_price == pytest.approx(closes["E00002"]["2023-04"][1])

    def test_order_is_fixed_whatever_the_input_order(self):
        """ridge の fold は行の並びで決まるので、入力の辞書順に依らず edinet_code 順で返す。"""
        fin, closes = _panel(n=5)
        reordered = {ec: closes[ec] for ec in reversed(list(closes))}
        codes = [r.edinet_code for r in asof_records(fin, reordered, {}, "2023-04")]
        assert codes == sorted(codes)

    def test_company_without_price_that_month_is_absent(self):
        fin, closes = _panel(n=3)
        closes["E00003"] = {}
        assert {r.edinet_code for r in asof_records(fin, closes, {}, "2023-04")} == \
            {"E00001", "E00002"}


class TestAsofGapRatios:
    def test_rows_published_later_do_not_change_earlier_months(self):
        """先読み防止の本丸: 2023-04 の gap は、まだ見えない 2023年3月期の行に左右されない。"""
        params = nightly_params()
        fin_now, closes = _panel(with_future=False)
        fin_later, _ = _panel(with_future=True)
        gaps_now, _ = asof_gap_ratios(fin_now, closes, {}, ["2023-04"], params, plugin.predict_gaps)
        gaps_later, _ = asof_gap_ratios(fin_later, closes, {}, ["2023-04"], params,
                                        plugin.predict_gaps)
        assert len(gaps_now) == 12
        assert gaps_now == gaps_later

    def test_later_month_uses_the_newly_visible_rows(self):
        params = nightly_params()
        fin, closes = _panel(with_future=True)
        gaps, stats = asof_gap_ratios(fin, closes, {}, ["2023-04", "2023-05"], params,
                                      plugin.predict_gaps)
        april = {ec: g for (ec, ym), g in gaps.items() if ym == "2023-04"}
        may = {ec: g for (ec, ym), g in gaps.items() if ym == "2023-05"}
        assert len(april) == len(may) == 12
        assert april != may
        assert stats["2023-05"] == {"n_universe": 12, "n_gap": 12}

    def test_month_where_regression_fails_is_recorded_not_filled(self):
        def _boom(_recs, _params):
            raise ValueError("分析可能な業種がありません")
        fin, closes = _panel(n=3)
        gaps, stats = asof_gap_ratios(fin, closes, {}, ["2023-04"], nightly_params(), _boom)
        assert gaps == {}
        assert stats["2023-04"]["n_gap"] == 0
        assert "分析可能な業種" in stats["2023-04"]["skipped"]

    def test_sector_below_min_samples_has_no_gap(self):
        fin, closes = _panel(n=3)                   # min_samples=5 未満
        gaps, stats = asof_gap_ratios(fin, closes, {}, ["2023-04"], nightly_params(),
                                      plugin.predict_gaps)
        assert gaps == {}
        assert stats["2023-04"]["n_universe"] == 3

    def test_split_factor_changes_only_through_the_target(self):
        """F を掛けた社は目的変数が大きくなる＝同じ財務なら割高側（gap が小さい）へ動く。"""
        params = nightly_params()
        fin, closes = _panel()
        base, _ = asof_gap_ratios(fin, closes, {}, ["2023-04"], params, plugin.predict_gaps)
        split, _ = asof_gap_ratios(fin, closes, {("E00005", 2022): 2.0}, ["2023-04"], params,
                                   plugin.predict_gaps)
        assert split[("E00005", "2023-04")] < base[("E00005", "2023-04")]


class TestAttachGapRatio:
    def test_appends_and_drops_rows_without_gap(self):
        samples = {"2023-04": [([1.0, 2.0], 0.1), ([3.0, 4.0], 0.2), ([5.0, 6.0], 0.3)]}
        ids = {"2023-04": ["A", "B", "C"]}
        coverage: dict = {}
        out = attach_gap_ratio(samples, ids, {("A", "2023-04"): 5.0, ("C", "2023-04"): -2.0},
                               coverage)
        assert out == {"2023-04": [([1.0, 2.0, 5.0], 0.1), ([5.0, 6.0, -2.0], 0.3)]}
        assert coverage == {"2023-04": (3, 2)}

    def test_does_not_mutate_the_input_rows(self):
        row = [1.0]
        attach_gap_ratio({"2023-04": [(row, 0.1)]}, {"2023-04": ["A"]}, {("A", "2023-04"): 1.0})
        assert row == [1.0]

    def test_gap_of_another_month_is_not_used(self):
        out = attach_gap_ratio({"2023-04": [([1.0], 0.1)]}, {"2023-04": ["A"]},
                               {("A", "2023-05"): 1.0})
        assert out == {}

    def test_misaligned_ids_raise(self):
        with pytest.raises(ValueError, match="一致しません"):
            attach_gap_ratio({"2023-04": [([1.0], 0.1)]}, {"2023-04": []}, {})


class TestNightlyParams:
    def test_follows_the_nightly_producer_settings(self):
        from nightly_scores import NIGHTLY_PARAMS
        params = nightly_params()
        for k, v in NIGHTLY_PARAMS["sector_ols"].items():
            assert params[k] == v
        assert params["features"] == DEFAULT_FEATURES_PRICE


class TestLoadInputs:
    def test_all_annual_years_sorted_and_split_factors(self, db, make_fin):
        from database import SplitAdjustmentFactor
        db.add_all([
            make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31",
                     bs_bps=900.0, pl_eps=50.0),
            make_fin(edinet_code="E00001", year=2022, period_end="2022-03-31",
                     bs_bps=800.0, pl_eps=40.0),
            make_fin(edinet_code="E00001", year=2023, period_end="2023-09-30",
                     period_type="H1", bs_bps=950.0),
            SplitAdjustmentFactor(edinet_code="E00001", year=2022, factor=2.0,
                                  n_events=1, kinds="split"),
        ])
        db.commit()
        fin_by_co, factors = load_inputs(db, DEFAULT_FEATURES_PRICE)
        assert [r.year for r in fin_by_co["E00001"]] == [2022, 2023]
        assert factors == {("E00001", 2022): 2.0}
