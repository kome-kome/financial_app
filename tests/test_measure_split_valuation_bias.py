"""scripts/measure_split_valuation_bias.py（測定 CLI）のテスト — Issue #653。

検出器そのもののテストは `tests/test_corporate_actions.py`（台帳が唯一の源・#746）。

純関数だけを対象にし、**DB・ネットワーク・環境変数に一切触れない**
（`tests/test_repair_splits_from_jquants.py` と同じ構え）。`database` を import すると
接続先解決が走るので、モジュールのトップでは重い import をしていないこと自体もここで守られる。
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import corporate_actions as C  # noqa: E402
from scripts import measure_split_valuation_bias as M  # noqa: E402


def row(year: int, shares, bps, *, eps=100.0, dps=10.0, price=1000.0,
        per=10.0, pbr=1.0, div_yield=1.0, market_cap=500.0, ec="E00001",
        period_end=None, equity=None) -> C.AnnualRow:
    return C.AnnualRow(
        edinet_code=ec, year=year,
        period_end=period_end or ("%d-03-31" % year),
        issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=dps,
        stock_price=price, per=per, pbr=pbr, div_yield=div_yield, market_cap=market_cap,
        bs_total_equity=equity)


def ev(year, *, canonical=2.0, sh_ratio=2.0, kind="split", ec="E00001",
       prev_year=None, period_end=None, prev_period_end=None) -> C.ShareEvent:
    prev_year = prev_year if prev_year is not None else year - 1
    return C.ShareEvent(
        edinet_code=ec, year=year, prev_year=prev_year, gap_years=year - prev_year,
        period_end=period_end or ("%d-03-31" % year),
        prev_period_end=prev_period_end or ("%d-03-31" % prev_year),
        sh_ratio=sh_ratio, bps_ratio=sh_ratio, canonical=canonical,
        residual=1.0, kind=kind)


class TestEquityGateCrosstab:
    """チェックが落とすイベントを突合ステータスで数える — Issue #657 の既定判定の材料。"""

    def _ungated(self):
        return [ev(2025, ec="E1"), ev(2025, ec="E2"), ev(2025, ec="E3"),
                ev(2025, ec="E4"), ev(2025, ec="E5")]

    def _results(self):
        return [C.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                C.MatchResult("E2", 2025, 2.0, 2.0, None, 0, "no_official_event"),
                C.MatchResult("E3", 2025, 2.0, 2.0, 3.0, 1, "disagree_magnitude"),
                C.MatchResult("E4", 2025, 2.0, 2.0, 2.0, 1, "agree")]

    def test_harm_and_benefit_are_counted_from_the_dropped_events(self):
        gated = [ev(2025, ec="E4")]
        ct = M.equity_gate_crosstab(self._results(), self._ungated(), gated)
        assert ct["dropped"] == [("E1", 2025), ("E2", 2025), ("E3", 2025), ("E5", 2025)]
        assert ct["dropped_status"] == {"agree": 1, "no_official_event": 1,
                                        "disagree_magnitude": 1, "not_in_census": 1}
        assert ct["harm"] == 1               # 公式と一致する本物を落とした
        assert ct["benefit"] == 1            # 公式に無いイベントを落とした
        assert ct["not_in_census"] == [("E5", 2025)]

    def test_raw_only_agreement_counts_as_harm(self):
        results = [C.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree_raw_only")]
        ct = M.equity_gate_crosstab(results, [ev(2025, ec="E1")], [])
        assert ct["harm"] == 1 and ct["benefit"] == 0

    def test_readmission_by_another_path_is_not_a_drop(self):
        """同じ (ec, year) を第2経路が拾い直したら、そのイベントは消えていない。"""
        gated = [ev(2025, ec=ec) for ec in ("E1", "E3", "E4", "E5")]
        gated.append(ev(2025, ec="E2")._replace(source="bps"))
        ct = M.equity_gate_crosstab(self._results(), self._ungated(), gated)
        assert ct["dropped"] == []
        assert ct["readmitted"] == [("E2", 2025)]
        assert ct["benefit"] == 0

    def test_events_that_only_appear_after_gating_are_listed(self):
        """落としたことで畳まれなくなった第2経路のイベントは、黙って増やさない。"""
        gated = self._ungated() + [ev(2024, ec="E1")._replace(source="bps")]
        ct = M.equity_gate_crosstab(self._results(), self._ungated(), gated)
        assert ct["added"] == [("E1", 2024)]


class TestTallyByGroup:
    def test_groups_by_source_and_kind(self):
        events = [ev(2025, ec="E1", kind="split"), ev(2025, ec="E2", kind="composite"),
                  ev(2025, ec="E3", kind="composite")]
        results = [C.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                   C.MatchResult("E2", 2025, 2.0, 2.0, None, 0, "no_official_event"),
                   C.MatchResult("E3", 2025, 2.0, 2.0, 2.0, 1, "agree")]
        got = M.tally_by_group(results, events)
        assert got == {"shares:composite": {"agree": 1, "no_official_event": 1},
                       "shares:split": {"agree": 1}}


class TestCorrectedValues:
    def test_direction_differs_by_column(self):
        """per/pbr/market_cap は x F、div_yield と nc_ratio は / F。取り違えると全部逆になる。"""
        r = row(2020, 1000.0, 100.0, per=3.29, pbr=0.97, div_yield=2.0, market_cap=500.0)
        got = M.corrected_values(r, 2.0)
        assert got["per"] == pytest.approx(6.58)
        assert got["pbr"] == pytest.approx(1.94)
        assert got["market_cap"] == pytest.approx(1000.0)
        assert got["div_yield"] == pytest.approx(1.0)
        assert "nc_ratio" not in got               # AnnualRow は持たない（VIEW 側の列）
        assert C.COLUMN_DIRECTION["nc_ratio"] == -1

    def test_reverse_split_flips_the_direction(self):
        r = row(2020, 1000.0, 100.0, per=100.0)
        assert M.corrected_values(r, 0.05)["per"] == pytest.approx(5.0)


class TestPriceBasis:
    def test_classify(self):
        weekly = [("2024-03-29", 1000.0)]
        assert M.classify_price_basis(1000.0, weekly, 2.0) == "adjusted"
        assert M.classify_price_basis(2000.0, weekly, 2.0) == "raw"
        assert M.classify_price_basis(1500.0, weekly, 2.0) == "unknown"
        assert M.classify_price_basis(1000.0, [], 2.0) == "unknown"
        assert M.classify_price_basis(None, weekly, 2.0) == "unknown"

    def test_factor_near_one_cannot_separate(self):
        """F が 1 に近いと両仮説が分離できない。分離できないものを断定しない。"""
        assert M.classify_price_basis(1000.0, [("2024-03-29", 1000.0)], 1.02) == "unknown"


class TestPartialCoverage:
    """`coverage_mode="partial"` — 倍率が合っているかだけを測るための緩め方（#659）。

    第2経路の倍率は翌年の `issued_shares` から取るので、突合には「公式が判定できる」と
    「翌年の行が提出済み」の両方が要る。契約窓は直近2年しかないため、full のままでは
    この2条件が排他になり分母が 0 になる。
    """

    def _ev(self):
        # 窓 = (2024-03-31 - 45日, 2025-03-31 + 45日] = 2024-02-15 〜 2025-05-15
        return ev(2025, canonical=2.0, sh_ratio=2.0,
                  prev_period_end="2024-03-31", period_end="2025-03-31")

    COVER = ("2024-06-20", "2026-06-20")

    def test_full_rejects_what_partial_accepts(self):
        """窓の左端が契約窓より前。full は判定できないと見なし、partial は重なりを見る。"""
        e = self._ev()
        assert C.in_coverage(e, self.COVER) is False
        assert C.in_coverage(e, self.COVER, mode="partial") is True

    def test_official_event_inside_the_overlap_is_compared(self):
        e = self._ev()
        r = C.match_event(e, [("2024-10-01", 0.5)], coverage=self.COVER,
                          coverage_mode="partial")
        assert r.status == "agree" and r.official == pytest.approx(2.0)

    def test_official_event_before_the_overlap_is_not_used(self):
        """重なりの外にある公式イベントは、そもそも API が返さない。拾いにいかない。"""
        e = self._ev()
        r = C.match_event(e, [("2024-04-01", 0.5)], coverage=self.COVER,
                          coverage_mode="partial")
        assert r.status == "no_official_event_partial"

    def test_partial_miss_is_excluded_from_the_denominator(self):
        """**ここが partial の肝**。「分割が無かった」と「窓の外で起きた」を区別できない。

        full の `no_official_event` は分母に入る（公式が返さない＝分割が無かったと読めるので
        検出が間違いだと言える）。partial の同じ状況は分母から外す。
        """
        rows = [C.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                C.MatchResult("E2", 2025, 2.0, 2.0, 3.0, 1, "disagree_magnitude"),
                C.MatchResult("E3", 2025, 2.0, 2.0, None, 0, "no_official_event_partial"),
                C.MatchResult("E4", 2025, 2.0, 2.0, None, 0, "out_of_coverage")]
        tally, denom, rate, rate_raw = M.tally_rates(rows)
        assert denom == 2
        assert rate == pytest.approx(0.5)
        assert rate_raw == pytest.approx(0.5)
        assert tally["no_official_event_partial"] == 1

    def test_full_miss_stays_in_the_denominator(self):
        rows = [C.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                C.MatchResult("E2", 2025, 2.0, 2.0, None, 0, "no_official_event")]
        _, denom, rate, _ = M.tally_rates(rows)
        assert denom == 2 and rate == pytest.approx(0.5)

    def test_empty_denominator_does_not_divide_by_zero(self):
        rows = [C.MatchResult("E1", 2025, 2.0, 2.0, None, 0, "out_of_coverage")]
        _, denom, rate, rate_raw = M.tally_rates(rows)
        assert denom == 0 and rate == 0.0 and rate_raw == 0.0


class TestChooseSample:
    def _events(self):
        out = []
        for i in range(20):
            out.append(ev(2021, ec="E%05d" % i, kind="split"))
        for i in range(20, 26):
            out.append(ev(2021, ec="E%05d" % i, kind="reverse", canonical=0.5))
        for i in range(26, 30):
            out.append(ev(2023, ec="E%05d" % i, kind="composite", prev_year=2021))
        return out

    def test_deterministic_and_stratified(self):
        evs = self._events()
        flat = ["F%05d" % i for i in range(50)]
        pos1, ctrl1 = M.choose_sample(evs, flat, n=10, controls=5, seed=0)
        pos2, ctrl2 = M.choose_sample(evs, flat, n=10, controls=5, seed=0)
        assert (pos1, ctrl1) == (pos2, ctrl2)
        assert len(pos1) == 10 and len(ctrl1) == 5
        kinds = {e.kind for e in evs if e.edinet_code in set(pos1)}
        assert kinds == {"split", "reverse", "composite"}   # 各層から最低1社
        assert not (set(pos1) & set(ctrl1))                 # 対照は陽性と重複しない

    def test_seed_changes_the_draw(self):
        evs = self._events()
        flat = ["F%05d" % i for i in range(50)]
        assert M.choose_sample(evs, flat, n=10, seed=0) != \
            M.choose_sample(evs, flat, n=10, seed=7)


class TestReport:
    def _report(self):
        rows = [row(y, 1000.0, 100.0, ec="E%05d" % i)
                for i in range(40) for y in (2020, 2021)]
        events = [ev(2021, canonical=2.0, ec="E00000")]
        factors = C.cumulative_factors(rows, events)
        rep = M.build_report(rows, events, factors, extras={
            "input": {"db_target": "local", "n_rows": len(rows), "n_companies": 40,
                      "year_from": 2020, "year_to": 2021, "non_null": {"per": len(rows)}},
            "detect": {"n_candidate_pairs": 1, "n_candidate_companies": 1, "n_events": 1,
                       "n_event_companies": 1, "skipped": {}, "by_kind": {"split": 1},
                       "n_gap_years_ge2": 0, "canonical_hist": {"2": 1}},
            "nc_ratio": {}, "price_basis": {"adjusted": 1}, "top": [],
            "min_cross_n": 5, "settings": {},
        })
        rep["verdict"] = M.build_verdict(rep)
        return rep

    def test_render_text_is_ascii_only_for_symbols(self):
        """cp932 リダイレクトで落ちる装飾記号を機械的に締め出す（日本語ラベルは可）。"""
        out = M.render_text(self._report())
        forbidden = set("─│┌┐└┘├┤┬┴┼✓✗★☆→←↑↓≥≤±·—–‘’“”")
        assert not (set(out) & forbidden)

    def test_json_keys_are_fixed(self):
        """指標を黙って落とす変更をここで落とす。"""
        assert tuple(M.to_json_dict(self._report()).keys()) == M.JSON_KEYS

    def test_verdict_mentions_damage(self):
        rep = self._report()
        assert "rows distorted" in rep["verdict"]

    def test_percentile_shift_flags_thin_cross_sections(self):
        vals = [("E%05d" % i, float(i)) for i in range(5)]
        got = M.percentile_shift(vals, {}, "per", min_n=30)
        assert got["thin_cross_section"] is True

    def test_percentile_shift_measures_rank_movement(self):
        vals = [("E%05d" % i, float(i + 1)) for i in range(40)]
        factors = {"E00000": 50.0}          # 最下位が最上位へ飛ぶ
        got = M.percentile_shift(vals, factors, "per", min_n=10)
        assert got["n_affected"] == 1
        assert got["max_shift_pt"] > 90.0
        assert got["n_gt_25pt"] == 1


class TestSeverityBands:
    def test_bands_exclude_unaffected_rows(self):
        bands = M.severity_bands([1.0, 1.0, 2.0, 5.0, 0.05])
        assert bands[">=2"] == 2 and bands[">=5"] == 1
        assert bands["<=0.05"] == 1
        assert bands[">=1.2"] == 2          # F == 1.0 の行は数えない


class TestNoOfficialBars:
    """全数突合が「公式のバーが無い」を偽陽性に数えない — Issue #668。

    #657 の census は E03474 を `no_official_event`（偽陽性）に数えたが、J-Quants はこの社のバーを
    契約窓で 1 本も返していなかった（2026-09-14 実測）。判定できないものは分母から外す。
    """

    E = dict(prev_period_end="2025-03-31", period_end="2026-03-31")
    COVER = ("2024-06-22", "2026-06-22")

    def test_zero_bars_is_not_a_false_positive(self):
        e = ev(2026, **self.E)
        r = C.match_event(e, [], coverage=self.COVER, official_spans=[])
        assert r.status == "no_official_bars"
        assert "no_official_bars" not in M.MATCH_DENOMINATOR
        assert "no_official_bars" not in M.GATE_BENEFIT_STATUSES

    def test_bars_covering_the_window_keep_the_false_positive(self):
        e = ev(2026, **self.E)
        r = C.match_event(e, [], coverage=self.COVER,
                          official_spans=[("2024-06-24", "2026-06-22", 486)])
        assert r.status == "no_official_event"

    def test_bars_not_covering_the_window_are_excluded_in_full_mode(self):
        """途中から上場した社・長い空白のある社。窓の一部しかバーが無ければ不在と読まない。"""
        e = ev(2026, **self.E)
        r = C.match_event(e, [], coverage=self.COVER,
                          official_spans=[("2025-09-01", "2026-06-22", 200)])
        assert r.status == "no_official_bars"

    def test_official_event_wins_over_missing_spans(self):
        e = ev(2026, canonical=2.0, sh_ratio=2.0, **self.E)
        assert C.match_event(e, [("2025-09-29", 0.5)], coverage=self.COVER,
                             official_spans=[]).status == "agree"

    def test_partial_mode_only_excludes_zero_bars(self):
        """partial は重なりの中だけを見る緩め方（#659）。区間が窓を覆わないのは前提なので、
        0 本のときだけ `no_official_bars` にし、それ以外は従来の `no_official_event_partial`。"""
        e = ev(2025, prev_period_end="2024-03-31", period_end="2025-03-31")
        cover = ("2024-06-20", "2026-06-20")
        assert C.match_event(e, [], coverage=cover, coverage_mode="partial",
                             official_spans=[]).status == "no_official_bars"
        assert C.match_event(e, [], coverage=cover, coverage_mode="partial",
                             official_spans=[("2024-06-21", "2026-06-19", 480)]
                             ).status == "no_official_event_partial"

    def test_none_keeps_the_previous_behaviour(self):
        e = ev(2026, **self.E)
        assert C.match_event(e, [], coverage=self.COVER).status == "no_official_event"

    def test_tally_excludes_it_from_the_denominator(self):
        rows = [C.MatchResult("E1", 2026, 2.0, 2.0, 2.0, 1, "agree"),
                C.MatchResult("E2", 2026, 2.0, 2.0, None, 0, "no_official_event"),
                C.MatchResult("E3", 2026, 2.0, 2.0, None, 0, "no_official_bars")]
        tally, denom, rate, _ = M.tally_rates(rows)
        assert denom == 2 and rate == pytest.approx(0.5)
        assert tally["no_official_bars"] == 1


def test_equity_tol_grid_contains_the_ledger_default():
    """感度表の格子は台帳の既定（`corporate_actions.DEFAULT_EQUITY_TOL`）を含む。

    既定を動かしたのに格子へ入れ忘れると、`detect --sweep` の表に本番の値の行が無くなる。
    """
    assert C.DEFAULT_EQUITY_TOL is None or C.DEFAULT_EQUITY_TOL in M.EQUITY_TOL_GRID
