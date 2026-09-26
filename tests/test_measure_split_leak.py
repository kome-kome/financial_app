"""`scripts/measure_split_leak.py` の純関数（DB・ネットワークに触れない）。

ADR-0055 決定7 の直接証拠を測る道具。層の割り当てやラベルの定義が本番の学習パネルと
ずれると、測ったものが別物になる。
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

import corporate_actions as C
from plugins import macro_snapshots as ms
from scripts import measure_split_leak as L


def ev(ec, year, canonical, prev_period_end=None, period_end=None):
    return SimpleNamespace(edinet_code=ec, year=year, canonical=canonical,
                           prev_period_end=prev_period_end, period_end=period_end)


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

    def test_registered_spinoff_is_neither_a_stratum_nor_in_the_sample_f(self, monkeypatch):
        """登録表のスピンオフ（#740）は層にも標本の F にも入れない（分割の読みの対象ではない）。

        係数表との突合は台帳の F 全体で行う（表には登録分も入る）ので、台帳の F はそのまま残る。
        """
        monkeypatch.setitem(C.SPINOFF_ADJUSTMENTS, "E99998",
                            (("2021-09-27", 0.5, "テスト用の登録・#740"),))

        def annual(ec, year, shares, bps):
            return C.AnnualRow(ec, year, "%d-03-31" % year, shares, bps, 100.0, 10.0,
                               1000.0, 10.0, 1.0, 1.0, 500.0)

        rows = [annual("E99998", y, 1000.0, 500.0) for y in (2020, 2021, 2022)]
        rows += [annual("E00001", 2020, 1000.0, 210.0), annual("E00001", 2021, 2000.0, 105.0)]
        ledger = C.compute_ledger(rows, official={}, coverage={}, series={})

        events, factors = L.split_events_and_factors(ledger)
        assert [(e.edinet_code, e.kind) for e in events] == [("E00001", "split")]
        assert "E99998" not in L.events_by_company(events)
        assert factors[("E99998", 2020)] == 1.0
        assert factors[("E00001", 2020)] == pytest.approx(2.0)
        assert ledger.factors[("E99998", 2020)] == pytest.approx(2.0)


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


class TestSeparability:
    """#687: 年差1の層は形成日が必ずイベント窓の内側に入る＝前後を分けられない。"""

    def test_only_the_one_year_strata_are_ambiguous(self):
        assert L.AMBIGUOUS_STRATA == {"split_1", "reverse_1"}

    def test_derived_from_stratum_of_not_hand_written(self):
        """層の刻み方を変えても追随する（書き写した名前を持たない）。"""
        assert all(name in L.STRATA_ORDER for name in L.AMBIGUOUS_STRATA)
        assert all(L.stratum_of(L._GapEvent(1, r), 0) in L.AMBIGUOUS_STRATA for r in (2.0, 0.5))

    @pytest.mark.parametrize("stratum,expected", [
        ("split_1", False), ("reverse_1", False),
        ("split_2", True), ("split_3", True), ("split_4+", True),
        ("reverse_2+", True), ("none", True),
    ])
    def test_is_separable(self, stratum, expected):
        assert L.is_separable(stratum) is expected

    def test_the_window_contains_every_one_year_formation_date(self):
        """包含関係そのものを実データの定義で確かめる（#687 の証明の実行版）。

        年 2020 の行が効く形成日の範囲は [2020-12-31+45日, 2021-12-31+45日)。
        年 2021 のイベントの窓は (2020-12-31-45日, 2021-12-31+45日]。
        """
        from scripts.measure_split_valuation_bias import event_window

        w0, w1 = event_window(ev("A", 2021, 2.0, "2020-12-31", "2021-12-31"))
        prices = {"A": weekly(2020, 200, growth=0.001)}
        rows = {"A": [fin(2020, "2020-12-31"), fin(2021, "2021-12-31")]}
        points = [d for d, _c0, _c1, f in
                  L.usable_formation_points(prices["A"], rows["A"], ms._find_applicable_fin)
                  if f.year == 2020]
        assert points, "年 2020 の行が効く形成日が1つも無い"
        assert all(w0 < d <= w1 for d in points), "窓の外に出た形成日がある"


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

    def test_every_stratum_carries_separability(self):
        """空の層でも `separable` が落ちない（#687・report が読む）。"""
        got = L.summarize([L.Sample("m", "A", "split_1", 0.7, 0.2)], n_boot=200)
        assert all("separable" in got[k] for k in L.STRATA_ORDER)
        assert got["split_1"]["separable"] is False and got["split_2"]["separable"] is True

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

    def test_every_path_says_it_is_retired(self):
        """#687: 再実行した人が新しい判断の根拠として読まないための印。"""
        for means in ([0.3, 0.2, 0.1, 0.05], [0.3, 0.1, 0.2, 0.05], [0.3, None, 0.1, 0.05]):
            got = L.verdict(_summary(means))
            assert got["retired"] is True and got["superseded_by"] == 687
            assert got["retired_reason"] == L.RETIRED_REASON

    def test_retired_reason_survives_cp932(self):
        L.RETIRED_REASON.encode("cp932")


class TestCompareFactors:
    def test_match(self):
        got = L.compare_factors({("A", 1): 2.0, ("A", 2): 1.0}, {("A", 1): 2.0})
        assert got["match"] is True and got["n_computed"] == 1

    def test_each_kind_of_mismatch_is_counted(self):
        got = L.compare_factors({("A", 1): 2.0, ("B", 1): 3.0, ("C", 1): 4.0},
                                {("A", 1): 2.5, ("D", 1): 2.0, ("C", 1): 4.0})
        assert (got["differ"], got["only_computed"], got["only_table"]) == (1, 1, 1)
        assert got["match"] is False


class TestRetryTrigger:
    """#687 で測る前に登録した着手条件を数える（層別の平均は出さない）。"""

    def _window(self):
        from scripts.measure_split_valuation_bias import event_window
        return event_window

    def _fixture(self, official):
        prices = {"A": weekly(2020, 200, growth=0.001)}
        rows = {"A": [fin(2020, "2020-12-31"), fin(2021, "2021-12-31")]}
        evs = {"A": [ev("A", 2021, 2.0, "2020-12-31", "2021-12-31")]}
        return L.count_datable_future_companies(
            prices, rows, evs, official, ms._find_applicable_fin, self._window())

    def test_counts_only_dates_after_the_formation_day(self):
        """窓の中の公式イベントが形成日より後のサンプルだけ数える。"""
        got = self._fixture({"A": [("2021-06-30", 0.5)]})
        assert got["companies"] == 1
        # 2021-02-14（=2020-12-31+45日）以降 2021-06-30 までの形成日だけが数えられる
        assert 0 < got["samples"] < 12
        assert got["threshold"] == L.RETRY_MIN_COMPANIES and got["ready"] is False

    def test_separable_strata_are_not_counted(self):
        """2年以上の層は元から綺麗＝公式の日付が情報を足さないので数えない（#687）。

        混ぜて数えると閾値を即座に満たしたように見える（実測 全層 177 社 / 11,057 件）。
        """
        prices = {"A": weekly(2019, 260, growth=0.001)}
        rows = {"A": [fin(2019, "2019-12-31"), fin(2020, "2020-12-31"),
                      fin(2021, "2021-12-31")]}
        evs = {"A": [ev("A", 2021, 2.0, "2020-12-31", "2021-12-31")]}
        points = list(L.usable_formation_points(prices["A"], rows["A"], ms._find_applicable_fin))
        assert any(f.year == 2019 for _d, _c0, _c1, f in points), "2年離れた行のサンプルが無い"
        got = L.count_datable_future_companies(
            prices, rows, evs, {"A": [("2021-06-30", 0.5)]},
            ms._find_applicable_fin, self._window())
        # 2019 年の行（split_2）は数えず、2020 年の行（split_1）だけを数える
        counted = [d for d, _c0, _c1, f in points
                   if f.year == 2020 and d < "2021-06-30"]
        assert got["samples"] == len(counted)

    def test_two_events_in_the_window_are_not_countable(self):
        """どちらが層を決めたイベントか分けられないので採らない。"""
        got = self._fixture({"A": [("2021-06-30", 0.5), ("2021-09-30", 0.5)]})
        assert (got["companies"], got["samples"], got["months"], got["month_range"],
                got["ready"]) == (0, 0, 0, None, False)

    def test_date_outside_the_window_is_not_countable(self):
        assert self._fixture({"A": [("2023-06-30", 0.5)]})["companies"] == 0

    def test_no_official_rows_means_zero(self):
        assert self._fixture({})["companies"] == 0

    def test_ready_flips_at_the_threshold(self, monkeypatch):
        monkeypatch.setattr(L, "RETRY_MIN_COMPANIES", 1)
        assert self._fixture({"A": [("2021-06-30", 0.5)]})["ready"] is True

    def test_reverse_events_are_not_counted(self):
        """事前登録した判定式は分割側だけを対象にしている。"""
        prices = {"A": weekly(2020, 200, growth=0.001)}
        rows = {"A": [fin(2020, "2020-12-31"), fin(2021, "2021-12-31")]}
        evs = {"A": [ev("A", 2021, 0.5, "2020-12-31", "2021-12-31")]}
        got = L.count_datable_future_companies(
            prices, rows, evs, {"A": [("2021-06-30", 2.0)]},
            ms._find_applicable_fin, self._window())
        assert got["companies"] == 0

    def test_shares_the_sample_definition_with_build_samples(self):
        """母集団がずれない（同じ `usable_formation_points` を通る）。"""
        prices = {"A": weekly(2020, 200, growth=0.001)}
        rows = {"A": [fin(2020, "2020-12-31"), fin(2021, "2021-12-31")]}
        points = list(L.usable_formation_points(prices["A"], rows["A"], ms._find_applicable_fin))
        built = L.build_samples(prices, rows, {}, {}, ms._find_applicable_fin)
        assert len(points) == len(built)


class TestDatablePointsAreShared:
    """再測定（#690）の層は、着手条件で数えたのと同じ述語から作る。"""

    def _args(self, official):
        from scripts.measure_split_valuation_bias import event_window
        prices = {"A": weekly(2020, 200, growth=0.001)}
        rows = {"A": [fin(2020, "2020-12-31"), fin(2021, "2021-12-31")]}
        evs = {"A": [ev("A", 2021, 2.0, "2020-12-31", "2021-12-31")]}
        return prices, rows, evs, official, ms._find_applicable_fin, event_window

    def test_counter_counts_exactly_the_points(self):
        args = self._args({"A": [("2021-06-30", 0.5)]})
        points = list(L.datable_future_points(*args))
        assert L.count_datable_future_companies(*args)["samples"] == len(points) > 0
        assert all(ec == "A" and d < "2021-06-30" for ec, d in points)

    def test_no_points_when_the_official_date_is_ambiguous(self):
        args = self._args({"A": [("2021-06-30", 0.5), ("2021-09-30", 0.5)]})
        assert list(L.datable_future_points(*args)) == []

    def test_dated_layer_is_exactly_the_counted_samples(self):
        """数えた `(社, 形成日)` と、測る層の `(社, 形成月)` が一致する。"""
        args = self._args({"A": [("2021-06-30", 0.5)]})
        prices, rows, evs, _official, find, _window = args
        samples = L.demean_by_month(L.build_samples(prices, rows, evs, {}, find))
        points = list(L.datable_future_points(*args))
        dated = L.dated_split_samples(samples, points)
        assert len(dated) == len(points)
        assert {s.stratum for s in dated} == {L.DATED_STRATUM}
        assert {(s.edinet_code, s.ym) for s in dated} == {(ec, d[:7]) for ec, d in points}


class TestDatedSplitSamples:
    SAMPLES = [L.Sample("2024-01", "A", "split_1", 0.7, 0.3),
               L.Sample("2024-02", "A", "split_1", 0.7, -0.1),
               L.Sample("2024-01", "B", "none", 0.0, 0.0)]

    def test_takes_only_the_keyed_samples_and_keeps_the_demeaned_label(self):
        got = L.dated_split_samples(self.SAMPLES, [("A", "2024-01-26")])
        assert got == [L.Sample("2024-01", "A", L.DATED_STRATUM, 0.7, 0.3)]

    def test_a_key_outside_split_1_is_a_definition_drift(self):
        with pytest.raises(RuntimeError, match="split_1"):
            L.dated_split_samples(self.SAMPLES, [("B", "2024-01-26")])

    def test_a_missing_key_is_a_definition_drift(self):
        with pytest.raises(RuntimeError, match="見つからない"):
            L.dated_split_samples(self.SAMPLES, [("C", "2024-01-26")])

    def test_the_dated_stratum_is_not_in_the_685_table(self):
        """#685 の表の再現性を壊さない（別の節に出す）。"""
        assert L.DATED_STRATUM not in L.STRATA_ORDER


def _stats(mean, lo=None, hi=None):
    return {"mean": mean, "ci": [lo, hi]}


class TestRetryVerdict:
    """#687 で測る前に登録した判定式（#690）。"""

    def test_both_conditions_keep_the_reading(self):
        got = L.retry_verdict(_stats(0.08, 0.02, 0.14), _stats(-0.01))
        assert got["keep_leak_reading"] is True
        assert got["ci_lo_above_zero"] is True and got["beats_none"] is True
        assert got["preregistered"] == 690

    def test_ci_touching_zero_drops_it(self):
        got = L.retry_verdict(_stats(0.08, 0.0, 0.16), _stats(-0.01))
        assert got["keep_leak_reading"] is False and got["ci_lo_above_zero"] is False

    def test_not_beating_none_drops_it_even_with_a_positive_ci(self):
        """平均が `none` と同値でも上回ったことにはしない（厳密な不等号）。"""
        got = L.retry_verdict(_stats(0.05, 0.01, 0.09), _stats(0.05))
        assert got["keep_leak_reading"] is False
        assert got["ci_lo_above_zero"] is True and got["beats_none"] is False

    def test_both_failing_says_so(self):
        got = L.retry_verdict(_stats(-0.02, -0.05, 0.01), _stats(0.0))
        assert got["keep_leak_reading"] is False
        assert "含み" in got["reason"] and "上回らなかった" in got["reason"]

    def test_an_empty_stratum_cannot_be_judged(self):
        got = L.retry_verdict(_stats(None), _stats(-0.01))
        assert got["keep_leak_reading"] is False and got["ci_lo_above_zero"] is None
        assert got["ci_half_width"] is None and got["powered"] is False

    @pytest.mark.parametrize("lo,hi,powered", [(0.01, 0.09, True), (0.00, 0.12, False)])
    def test_half_width_is_reported_but_does_not_decide(self, lo, hi, powered):
        got = L.retry_verdict(_stats(0.05, lo, hi), _stats(-0.01))
        assert got["ci_half_width"] == pytest.approx((hi - lo) / 2)
        assert got["powered"] is powered
        assert got["min_detectable_effect"] == L.MIN_DETECTABLE_EFFECT

    def test_reasons_survive_cp932(self):
        for dated, none in [(_stats(0.08, 0.02, 0.14), _stats(-0.01)),
                            (_stats(0.08, 0.0, 0.16), _stats(-0.01)),
                            (_stats(0.05, 0.01, 0.09), _stats(0.05)),
                            (_stats(-0.02, -0.05, 0.01), _stats(0.0)),
                            (_stats(None), _stats(-0.01))]:
            L.retry_verdict(dated, none)["reason"].encode("cp932")


def _retry_samples():
    """日付で確定した層（A・B の 2024-01/02）と、3か月にまたがる `none`。"""
    return [L.Sample("2024-01", "A", "split_1", 0.7, 0.10),
            L.Sample("2024-02", "A", "split_1", 0.7, 0.20),
            L.Sample("2024-01", "B", "split_1", 0.7, 0.30),
            L.Sample("2024-01", "C", "none", 0.0, -0.01),
            L.Sample("2024-02", "D", "none", 0.0, -0.02),
            L.Sample("2023-06", "E", "none", 0.0, 0.05)]


_RETRY_KEYS = [("A", "2024-01-26"), ("A", "2024-02-23"), ("B", "2024-01-26")]


class TestMeasureRetry:
    def test_not_ready_computes_no_means(self):
        """READY まで層別の平均は出さない（#687 の事前登録）。"""
        assert L.measure_retry(_retry_samples(), _RETRY_KEYS, False, n_boot=50) == {
            "ready": False}

    def test_ready_compares_against_the_whole_none_stratum(self):
        got = L.measure_retry(_retry_samples(), _RETRY_KEYS, True, n_boot=50)
        assert got["ready"] is True
        assert got[L.DATED_STRATUM]["n"] == 3 and got[L.DATED_STRATUM]["n_companies"] == 2
        assert got["none"]["n"] == 3
        # 参考値は同じ形成月（2024-01 / 2024-02）に限る。2023-06 の E は入らない
        assert got["none_same_months"]["n"] == 2
        assert got["none_same_months"]["reference_only"] is True
        assert (got["months"], got["month_range"]) == (2, ["2024-01", "2024-02"])
        assert got["verdict"] == L.retry_verdict(got[L.DATED_STRATUM], got["none"])


class TestReportRetry:
    def _report(self, retry):
        summary = L.summarize([], n_boot=10)
        trigger = {"companies": 0, "samples": 0, "threshold": L.RETRY_MIN_COMPANIES,
                   "ready": bool(retry.get("ready"))}
        L.report(summary, L.compare_factors({}, {}), L.verdict(summary), trigger, retry)

    def test_ready_prints_the_preregistered_verdict(self, capsys):
        self._report(L.measure_retry(_retry_samples(), _RETRY_KEYS, True, n_boot=50))
        out = capsys.readouterr().out
        out.encode("cp932")
        assert "#690" in out and L.DATED_STRATUM in out and "none(same months,ref)" in out
        assert "verdict (#690 preregistered)" in out

    def test_not_ready_prints_no_dated_row(self, capsys):
        self._report({"ready": False})
        out = capsys.readouterr().out
        out.encode("cp932")
        assert "層別の平均は出さない" in out and L.DATED_STRATUM not in out


def test_report_survives_cp932(capsys):
    summary = L.summarize([], n_boot=10)
    trigger = {"companies": 0, "samples": 0, "threshold": L.RETRY_MIN_COMPANIES, "ready": False}
    L.report(summary, L.compare_factors({}, {}), L.verdict(summary), trigger)
    out = capsys.readouterr().out
    out.encode("cp932")
    assert "RETIRED" in out and "separable" in out and "NOT YET" in out
