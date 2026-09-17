"""`scripts/measure_split_leak.py` の純関数（DB・ネットワークに触れない）。

ADR-0055 決定7 の直接証拠を測る道具。層の割り当てやラベルの定義が本番の学習パネルと
ずれると、測ったものが別物になる。
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from plugins import macro_snapshots as ms
from scripts import measure_split_leak as L


def ev(ec, year, canonical):
    return SimpleNamespace(edinet_code=ec, year=year, canonical=canonical)


def fin(year, period_end):
    return SimpleNamespace(year=year, period_end=period_end)


def weekly(start_year: int, n: int, growth: float = 0.0):
    """月曜始まりの週次バー n 本（close は週 growth の複利）。"""
    from datetime import date, timedelta
    d0 = date(start_year, 1, 5)
    return [ms._WEEKLY_PX((d0 + timedelta(weeks=i)).isoformat(),
                          100.0 * math.exp(growth * i), None) for i in range(n)]


class TestDefinitionsMatchTheTrainingPanel:
    def test_horizon_is_the_same(self):
        assert L.HORIZON_WEEKS == ms.HORIZON_WEEKS


class TestEvents:
    def test_unsnapped_and_unit_ratios_are_dropped(self):
        got = L.events_by_company([ev("A", 2022, None), ev("A", 2021, 2.0), ev("A", 2023, 1.0)])
        assert [e.year for e in got["A"]] == [2021]

    def test_sorted_by_year(self):
        got = L.events_by_company([ev("A", 2024, 3.0), ev("A", 2021, 2.0)])
        assert [e.year for e in got["A"]] == [2021, 2024]

    def test_nearest_future_uses_the_strict_predicate(self):
        """当年のイベントは当年の行を歪めない（`cumulative_factors` と同じ `>`）。"""
        evs = [ev("A", 2021, 2.0), ev("A", 2024, 3.0)]
        assert L.nearest_future_event(evs, 2020).year == 2021
        assert L.nearest_future_event(evs, 2021).year == 2024
        assert L.nearest_future_event(evs, 2024) is None


class TestStratum:
    @pytest.mark.parametrize("event_year,expected", [
        (2021, "split_1"), (2022, "split_2"), (2023, "split_3"),
        (2024, "split_4+"), (2030, "split_4+"),
    ])
    def test_split_buckets(self, event_year, expected):
        assert L.stratum_of(ev("A", event_year, 2.0), 2020) == expected

    def test_reverse_buckets(self):
        assert L.stratum_of(ev("A", 2021, 0.1), 2020) == "reverse_1"
        assert L.stratum_of(ev("A", 2023, 0.1), 2020) == "reverse_2+"

    def test_no_event_is_none(self):
        assert L.stratum_of(None, 2020) == "none"

    def test_every_stratum_is_reported(self):
        names = {L.stratum_of(ev("A", 2020 + k, r), 2020) for k in (1, 2, 3, 4) for r in (2.0, 0.5)}
        names.add(L.stratum_of(None, 2020))
        assert names <= set(L.STRATA_ORDER)


class TestBuildSamples:
    def test_label_and_applicable_row_follow_the_panel(self):
        prices = {"A": weekly(2020, 160, growth=0.01)}
        rows = {"A": [fin(2019, "2019-12-31"), fin(2020, "2020-12-31")]}
        evs = {"A": [ev("A", 2021, 2.0)]}
        factors = {("A", 2019): 2.0, ("A", 2020): 2.0}

        got = L.build_samples(prices, rows, evs, factors, ms._find_applicable_fin)

        assert got, "サンプルが1件も作られない"
        assert all(s.label == pytest.approx(0.52) for s in got), "52週先の log リターンでない"
        assert all(s.log_f == pytest.approx(math.log(2.0)) for s in got)
        # 2020 年の行が効くのは 2020-12-31 + 45日 以降。それまでは 2019 年の行（分割まで2年）。
        by_ym = {s.ym: s.stratum for s in got}
        assert by_ym["2020-06"] == "split_2"
        assert by_ym["2021-06"] == "split_1"

    def test_no_future_bar_no_sample(self):
        prices = {"A": weekly(2020, 50)}
        rows = {"A": [fin(2019, "2019-12-31")]}
        assert L.build_samples(prices, rows, {}, {}, ms._find_applicable_fin) == []

    def test_company_without_financials_is_skipped(self):
        prices = {"A": weekly(2020, 120)}
        assert L.build_samples(prices, {}, {}, {}, ms._find_applicable_fin) == []

    def test_missing_factor_means_one(self):
        prices = {"A": weekly(2020, 120)}
        rows = {"A": [fin(2019, "2019-12-31")]}
        got = L.build_samples(prices, rows, {}, {}, ms._find_applicable_fin)
        assert got and all(s.log_f == 0.0 and s.stratum == "none" for s in got)


class TestDemeanAndSummary:
    def test_demean_removes_the_month_mean(self):
        s = [L.Sample("2020-01", "A", "none", 0.0, 0.3),
             L.Sample("2020-01", "B", "split_1", 0.7, 0.5),
             L.Sample("2020-02", "A", "none", 0.0, -0.2)]
        got = L.demean_by_month(s)
        assert [round(x.label, 10) for x in got] == [-0.1, 0.1, 0.0]

    def test_summary_counts_companies_and_samples(self):
        s = [L.Sample("m", "A", "split_1", 0.7, 0.2), L.Sample("n", "A", "split_1", 0.7, 0.4),
             L.Sample("m", "B", "split_1", 0.7, 0.0)]
        got = L.summarize(s, n_boot=200)
        assert got["split_1"]["n"] == 3 and got["split_1"]["n_companies"] == 2
        assert got["split_1"]["mean"] == pytest.approx(0.2)
        assert got["none"]["n"] == 0 and got["none"]["mean"] is None

    def test_bootstrap_resamples_companies_not_rows(self):
        """1社だけの層は社単位では CI が作れない（行で引くと偽の精度が出る）。"""
        assert L.cluster_bootstrap_ci({"A": [0.1, 0.2, 0.3]}) == (None, None)

    def test_bootstrap_is_deterministic(self):
        g = {"A": [0.1, 0.2], "B": [0.3], "C": [-0.1, 0.0, 0.4]}
        assert L.cluster_bootstrap_ci(g, n_boot=300) == L.cluster_bootstrap_ci(g, n_boot=300)


def _summary(means, lo=0.01):
    out = {k: {"mean": m, "ci": [lo if k == "split_1" else None, None]}
           for k, m in zip(L.SPLIT_ORDER, means)}
    return out


class TestVerdict:
    def test_monotone_and_positive_keeps_the_reading(self):
        assert L.verdict(_summary([0.3, 0.2, 0.1, 0.05]))["keep_leak_reading"] is True

    def test_not_monotone_drops_it(self):
        got = L.verdict(_summary([0.3, 0.1, 0.2, 0.05]))
        assert got["keep_leak_reading"] is False and got["monotone"] is False

    def test_ties_are_not_monotone(self):
        assert L.verdict(_summary([0.3, 0.3, 0.2, 0.1]))["monotone"] is False

    def test_ci_touching_zero_drops_it(self):
        got = L.verdict(_summary([0.3, 0.2, 0.1, 0.05], lo=-0.001))
        assert got["keep_leak_reading"] is False and got["split_1_ci_above_zero"] is False

    def test_an_empty_stratum_cannot_be_judged(self):
        got = L.verdict(_summary([0.3, None, 0.1, 0.05]))
        assert got["keep_leak_reading"] is False

    def test_reasons_survive_cp932(self):
        for means in ([0.3, 0.2, 0.1, 0.05], [0.1, 0.2, 0.3, 0.4], [0.3, None, 0.1, 0.0]):
            L.verdict(_summary(means))["reason"].encode("cp932")


class TestCompareFactors:
    def test_match(self):
        got = L.compare_factors({("A", 1): 2.0, ("A", 2): 1.0}, {("A", 1): 2.0})
        assert got["match"] is True and got["n_computed"] == 1

    def test_each_kind_of_mismatch_is_counted(self):
        got = L.compare_factors({("A", 1): 2.0, ("B", 1): 3.0, ("C", 1): 4.0},
                                {("A", 1): 2.5, ("D", 1): 2.0, ("C", 1): 4.0})
        assert (got["differ"], got["only_computed"], got["only_table"]) == (1, 1, 1)
        assert got["match"] is False


def test_report_survives_cp932(capsys):
    summary = {k: {"n": 0, "n_companies": 0, "mean": None, "ci": [None, None],
                   "mean_log_f": None} for k in L.STRATA_ORDER}
    L.report(summary, L.compare_factors({}, {}), L.verdict(summary))
    capsys.readouterr().out.encode("cp932")
