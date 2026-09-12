"""scripts/measure_split_valuation_bias.py のテスト — Issue #653。

純関数だけを対象にし、**DB・ネットワーク・環境変数に一切触れない**
（`tests/test_repair_splits_from_jquants.py` と同じ構え）。`database` を import すると
接続先解決が走るので、モジュールのトップでは重い import をしていないこと自体もここで守られる。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import measure_split_valuation_bias as M  # noqa: E402


def row(year: int, shares, bps, *, eps=100.0, dps=10.0, price=1000.0,
        per=10.0, pbr=1.0, div_yield=1.0, market_cap=500.0, ec="E00001",
        period_end=None) -> M.AnnualRow:
    return M.AnnualRow(
        edinet_code=ec, year=year,
        period_end=period_end or ("%d-03-31" % year),
        issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=dps,
        stock_price=price, per=per, pbr=pbr, div_yield=div_yield, market_cap=market_cap)


def ev(year, *, canonical=2.0, sh_ratio=2.0, kind="split", ec="E00001",
       prev_year=None, period_end=None, prev_period_end=None) -> M.ShareEvent:
    prev_year = prev_year if prev_year is not None else year - 1
    return M.ShareEvent(
        edinet_code=ec, year=year, prev_year=prev_year, gap_years=year - prev_year,
        period_end=period_end or ("%d-03-31" % year),
        prev_period_end=prev_period_end or ("%d-03-31" % prev_year),
        sh_ratio=sh_ratio, bps_ratio=sh_ratio, canonical=canonical,
        residual=1.0, kind=kind)


class TestDetectEvents:
    def test_clean_split_is_detected(self):
        """1:2 分割＝株数×2・bps÷2。これが検出の基本形。"""
        rows = [row(2020, 1000.0, 200.0), row(2021, 2000.0, 100.0)]
        events, stats = M.detect_events(rows)
        assert len(events) == 1
        e = events[0]
        assert e.kind == "split"
        assert e.canonical == pytest.approx(2.0)
        assert e.year == 2021 and e.prev_year == 2020 and e.gap_years == 1
        assert stats["n_events"] == 1 and stats["n_event_companies"] == 1

    def test_bps_only_move_is_not_an_event(self):
        """資産再評価で bps だけ動く。株数が動かないので候補ゲートに掛からない。"""
        rows = [row(2020, 1000.0, 200.0), row(2021, 1000.0, 100.0)]
        events, _ = M.detect_events(rows)
        assert events == []

    def test_share_issuance_is_rejected_by_cross_check(self):
        """時価発行増資: 株数 1.5 倍だが bps はほぼ動かない。

        偽陽性を止めているのは閾値ではなく bps 逆比の交差検証である、という中心の主張。
        """
        rows = [row(2020, 1000.0, 200.0), row(2021, 1500.0, 198.0)]
        events, stats = M.detect_events(rows)
        assert events == []
        assert stats["n_candidate_pairs"] == 1        # 候補にはなるが交差検証で落ちる

    def test_reverse_split(self):
        """1:20 併合。F < 1 になり per は過大＝割高に見える向きへ反転する。"""
        rows = [row(2020, 20000.0, 10.0), row(2021, 1000.0, 200.0)]
        events, _ = M.detect_events(rows)
        assert len(events) == 1
        assert events[0].kind == "reverse"
        assert events[0].canonical == pytest.approx(0.05)

    def test_composite_snaps_to_five_not_four(self):
        """実測の合成例 4.7891。5.0 へ寄り、4.0 は選ばれない。"""
        c, residual, kind = M.snap_to_canonical(4.7891)
        assert kind == "composite"
        assert c == pytest.approx(5.0)
        assert c != pytest.approx(4.0)
        assert residual == pytest.approx(4.7891 / 5.0)

    def test_unsnappable_ratio_is_not_rounded(self):
        """7.0 は最近傍 5.0 に対し残差 1.4＝合成としても寄らない。丸めずに別枠へ出す。"""
        c, residual, kind = M.snap_to_canonical(7.0)
        assert kind == "unsnapped"
        assert c is None
        assert residual == pytest.approx(7.0)

    def test_gate_boundary_is_log_symmetric(self):
        """閾値は対数対称。予備クエリの `< 0.72` との差がここに出る。"""
        # 上側 1.4 ちょうどは通り、1.399 は通らない
        assert M.detect_events([row(2020, 1000.0, 140.0), row(2021, 1400.0, 100.0)])[0]
        assert M.detect_events([row(2020, 1000.0, 139.9), row(2021, 1399.0, 100.0)])[0] == []
        # 下側 0.716 は 0.72 より小さいが 1/1.4 = 0.7143 より大きい＝対称なら通らない
        assert M.detect_events([row(2020, 1000.0, 71.6), row(2021, 716.0, 100.0)])[0] == []
        # 0.71 は 1/1.4 を超えるので通る
        assert M.detect_events([row(2020, 1000.0, 71.0), row(2021, 710.0, 100.0)])[0]

    def test_missing_year_pairs_with_previous_available_row(self):
        rows = [row(2019, 1000.0, 200.0), row(2021, 2000.0, 100.0)]
        events, stats = M.detect_events(rows)
        assert len(events) == 1
        assert events[0].gap_years == 2
        assert stats["n_gap_years_ge2"] == 1

    def test_unusable_rows_are_skipped_and_counted(self):
        """欠損・ゼロ・負の bps。ゼロ除算も符号反転による幽霊イベントも起こさない。"""
        rows = [row(2019, 1000.0, 200.0), row(2020, None, 100.0),
                row(2021, 2000.0, 0.0), row(2022, 3000.0, -50.0)]
        events, stats = M.detect_events(rows)
        assert events == []
        assert stats["skipped"]["missing_shares"] == 1
        assert stats["skipped"]["missing_bps"] == 1
        assert stats["skipped"]["negative_bps"] == 1


class TestBpsPath:
    """第2経路（`bs_bps` 候補ゲート x `pl_eps` 交差検証）— Issue #656 / #659。

    検体は E03137 しまむらの `financial_records` 実値をそのまま写した（2026-09-12・
    接続先 local）。**推測で書いた検体は「本物を読めないこと」を検出できない。**

        year | issued_shares | bs_bps   | pl_eps
        2023 |    36,913,299 | 11973.98 | 1034.57
        2024 |    36,913,299 |  6413.61 |  545.35
        2025 |    73,826,598 |  6815.66 |  569.83
        2026 |    73,826,598 |  2353.09 |  202.36

    株数は 2025 に x2 されるが、1 株指標は 2024（→1:2）と 2026（→1:3）に動く。
    つまり**株数と 1 株指標が 1 年ずれて報告されている**ので、第1経路は 2025 の候補を
    交差検証で落とし（bps は下がるどころか上がっている）、2024 と 2026 は候補にすら上がらない。

    **倍率は翌年の株数比から取る**（#659）。2024 のイベントは 2025 の株数 x2.0000 が倍率になる。
    2026 のイベントは 2027 の行がまだ無いので**採らない**——bps 比 2.8965 をスナップすれば
    3.0 が出るが、bps は内部留保でも動くため「何倍か」の根拠にならない。
    """

    SHIMAMURA = (
        (2023, 36913299.0, 11973.98, 1034.57),
        (2024, 36913299.0, 6413.61, 545.35),
        (2025, 73826598.0, 6815.66, 569.83),
        (2026, 73826598.0, 2353.09, 202.36),
    )

    def _rows(self):
        return [row(y, sh, bps, eps=eps, ec="E03137") for y, sh, bps, eps in self.SHIMAMURA]

    def test_shares_path_alone_finds_nothing(self):
        """#656 が報告した取りこぼしそのもの。第2経路が無ければ 1 件も立たない。"""
        assert M.detect_events(self._rows(), bps_path=False)[0] == []

    def test_bps_path_finds_the_split_with_a_third_signal(self):
        """翌年の株数が裏を取れる 2024 だけが立つ。2026 は第3信号が無いので採らない。"""
        events, stats = M.detect_events(self._rows(), bps_path=True)
        assert [(e.year, e.canonical) for e in events] == [(2024, pytest.approx(2.0))]
        assert events[0].source == "bps"
        assert stats["n_events_by_source"] == {"bps": 1}
        assert stats["bps_path"]["n_events"] == 1
        assert stats["bps_path"]["rejected"]["no_lagged_row"] == 1

    def test_magnitude_comes_from_the_lagged_shares_not_from_bps(self):
        """**倍率の出どころが `bs_bps` から翌年の `issued_shares` へ移ったこと**（#659）。

        bps 比 1.8670 をスナップすると残差 0.9335 の `composite` になり、値としては 2.0 に
        寄るが、これは「たまたま同じ定番比へ落ちた」だけである（実測では 1.7032 が 1.5 へ
        落ちて公式 2.0 を外す）。ここでは倍率が翌年の株数比 2.0000 ちょうどから決まるので
        `kind` は `split`・残差は 1.0 になる。bps 比と株数比は観測値のまま残す。
        """
        e = [e for e in M.detect_events(self._rows(), bps_path=True)[0] if e.year == 2024][0]
        assert e.lagged_sh_ratio == pytest.approx(2.0)
        assert e.kind == "split"
        assert e.residual == pytest.approx(1.0)
        assert e.bps_ratio == pytest.approx(1.8670, abs=1e-4)
        # 株数比は捏造せず観測値のまま残す（あとから「当年は株数が動いていない」が読める）
        assert e.sh_ratio == pytest.approx(1.0)

    def test_a_half_ratio_snaps_to_the_lagged_shares_answer(self):
        """#659 の表そのもの。bps 比 1.7036 だけ見ると 1.5 へ落ちるが、公式は 2.0。

        翌年の株数比を倍率にすれば 2.0 が出る。**bps 比は「分割があった」は言えるが
        「何倍か」を決められない**、という本 issue の主張を固定する。
        """
        rows = [row(2020, 1000.0, 100.0, eps=10.0),
                row(2021, 1000.0, 58.7, eps=5.87),
                row(2022, 2000.0, 58.7, eps=5.87)]
        # bps 比だけを見たときのスナップ先（＝#659 が直した誤り）を先に押さえておく
        assert M.snap_to_canonical(100.0 / 58.7)[0] == pytest.approx(1.5)
        events, _ = M.detect_events(rows, bps_path=True)
        assert [(e.year, e.canonical, e.source) for e in events] == [(2021, 2.0, "bps")]

    def test_cumulative_factor_is_the_product_of_later_events(self):
        """F は**その行より後**のイベントの積。立つのは 2024 の 1 件だけ。

        2026 の 1:3 は第3信号が無いので今回は入らない。**係数表は毎晩全置換**なので、
        2027 の決算が提出されて株数が x3 になった時点で自動的に F へ入る（#659）。
        """
        rows = self._rows()
        f = M.cumulative_factors(rows, M.detect_events(rows, bps_path=True)[0])
        assert f[("E03137", 2023)] == pytest.approx(2.0)
        assert f[("E03137", 2024)] == pytest.approx(1.0)
        assert f[("E03137", 2025)] == pytest.approx(1.0)
        assert f[("E03137", 2026)] == pytest.approx(1.0)

    def test_impairment_is_rejected_by_the_eps_cross_check(self):
        """bps だけが半分になる（減損・資産再評価）。eps が追随しないので立たない。"""
        rows = [row(2020, 1000.0, 200.0, eps=100.0), row(2021, 1000.0, 100.0, eps=100.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert events == []
        assert stats["bps_path"]["rejected"]["eps_mismatch"] == 1

    @pytest.mark.parametrize("eps_prev,eps_cur", [(100.0, -50.0), (-100.0, -50.0),
                                                  (100.0, 0.0), (None, 50.0), (100.0, None)])
    def test_non_positive_eps_is_rejected(self, eps_prev, eps_cur):
        """赤字・ゼロ・欠損の年は比の意味が壊れる。赤字幅の増減が分割比に化ける。"""
        rows = [row(2020, 1000.0, 200.0, eps=eps_prev), row(2021, 1000.0, 100.0, eps=eps_cur)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert events == []
        assert stats["bps_path"]["rejected"]["eps_sign"] == 1

    def test_latest_year_event_is_not_taken(self):
        """翌年の決算がまだ提出されていない年は倍率の出どころが無い。採らずに次回へ送る。"""
        rows = [row(2020, 1000.0, 200.0, eps=20.0), row(2021, 1000.0, 100.0, eps=10.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert events == []
        assert stats["bps_path"]["rejected"]["no_lagged_row"] == 1

    def test_flat_lagged_shares_is_rejected(self):
        """翌年も株数が動かない。1 株指標だけが動いた理由を分割と言い切れない。"""
        rows = [row(2020, 1000.0, 200.0, eps=20.0), row(2021, 1000.0, 100.0, eps=10.0),
                row(2022, 1000.0, 100.0, eps=10.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert events == []
        assert stats["bps_path"]["rejected"]["lagged_flat"] == 1

    def test_opposite_direction_lagged_shares_is_rejected(self):
        """bps は分割方向・翌年の株数は併合方向。同じ事象ではないので採らない。"""
        rows = [row(2020, 1000.0, 200.0, eps=20.0), row(2021, 1000.0, 100.0, eps=10.0),
                row(2022, 100.0, 100.0, eps=10.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert events == []
        assert stats["bps_path"]["rejected"]["lagged_direction"] == 1

    def test_unsnappable_lagged_ratio_is_not_rounded(self):
        """どの定番比にも合成としても寄らない比は捨てる（第1経路と同じ扱い）。"""
        rows = [row(2020, 1000.0, 700.0, eps=70.0), row(2021, 1000.0, 100.0, eps=10.0),
                row(2022, 7000.0, 100.0, eps=10.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert events == []
        assert stats["bps_path"]["rejected"]["lagged_unsnapped"] == 1

    def test_clean_split_is_not_counted_twice(self):
        """株数も bps も同じ年に動く普通の分割。両経路が拾うが 1 件へ畳む。"""
        rows = [row(2020, 1000.0, 200.0, eps=20.0), row(2021, 2000.0, 100.0, eps=10.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert len(events) == 1
        assert events[0].source == "shares"       # 株数の方が基準として素直
        assert stats["bps_path"]["rejected"]["dup_with_shares"] == 1
        # 畳み損ねると F が比の二乗（4.0）になる。そこが実害。
        assert M.cumulative_factors(rows, events)[("E00001", 2020)] == pytest.approx(2.0)

    def test_lagged_move_already_taken_by_the_shares_path_is_folded(self):
        """**同じ株数の動きを2回数えない**（#659）。

        2021 で bps だけが半分になり、2022 で株数 x2 と bps 半減が揃う。株数の動きは
        1 回しか起きていないのに、bps 経路が 2021 の倍率として同じ動きを使い、第1経路が
        2022 のイベントとして採ると、F が比の二乗（4.0）になる。第1経路を残す側へ畳む。
        """
        rows = [row(2020, 1000.0, 200.0, eps=20.0), row(2021, 1000.0, 100.0, eps=10.0),
                row(2022, 2000.0, 50.0, eps=5.0)]
        events, stats = M.detect_events(rows, bps_path=True)
        assert [(e.year, e.source) for e in events] == [(2022, "shares")]
        assert stats["bps_path"]["rejected"]["dup_lagged_with_shares"] == 1
        assert M.cumulative_factors(rows, events)[("E00001", 2020)] == pytest.approx(2.0)

    def test_disabled_path_reproduces_the_previous_behaviour(self):
        """`bps_path=False` は #655 までの検出と 1 件も違わないこと。"""
        rows = [row(2020, 1000.0, 200.0, eps=20.0), row(2021, 2000.0, 100.0, eps=10.0),
                row(2022, 2000.0, 50.0, eps=5.0), row(2023, 4000.0, 50.0, eps=5.0)]
        off, off_stats = M.detect_events(rows, bps_path=False)
        assert [(e.year, e.canonical, e.source) for e in off] == [(2021, 2.0, "shares")]
        assert off_stats["bps_path"]["enabled"] is False
        assert off_stats["bps_path"]["n_events"] == 0
        # 第2経路を入れると 2022 の分割（株数は 2023 に追随）が増える
        on, _ = M.detect_events(rows, bps_path=True)
        assert [(e.year, e.source) for e in on] == [(2021, "shares"), (2022, "bps")]

    def test_reverse_split_via_bps(self):
        """1:10 併合（株数は翌年に追随）。bps と eps が 10 倍になり canonical は 0.1。"""
        rows = [row(2020, 1000.0, 100.0, eps=10.0), row(2021, 1000.0, 1000.0, eps=100.0),
                row(2022, 100.0, 1000.0, eps=100.0)]
        events, _ = M.detect_events(rows, bps_path=True)
        assert len(events) == 1
        assert events[0].canonical == pytest.approx(0.1)
        assert events[0].kind == "reverse" and events[0].source == "bps"
        assert events[0].lagged_sh_ratio == pytest.approx(0.1)


class TestCumulativeFactors:
    def test_only_later_events_count(self):
        """`e.year > y` であって `>=` ではない。分割当年の行は既に新基準で歪んでいない。"""
        rows = [row(y, 1000.0, 100.0) for y in (2019, 2021, 2022, 2023, 2024)]
        events = [ev(2021, canonical=2.0), ev(2023, canonical=3.0)]
        f = M.cumulative_factors(rows, events)
        assert f[("E00001", 2019)] == pytest.approx(6.0)
        assert f[("E00001", 2021)] == pytest.approx(3.0)
        assert f[("E00001", 2022)] == pytest.approx(3.0)
        assert f[("E00001", 2023)] == pytest.approx(1.0)   # 当年は歪まない
        assert f[("E00001", 2024)] == pytest.approx(1.0)

    def test_unsnapped_events_are_excluded_from_main_aggregate(self):
        rows = [row(2019, 1000.0, 100.0), row(2021, 1000.0, 100.0)]
        events = [ev(2021, canonical=None, sh_ratio=7.0, kind="unsnapped")]
        assert M.cumulative_factors(rows, events)[("E00001", 2019)] == pytest.approx(1.0)
        # 上限見積りが要るときだけ観測比を使う
        raw = M.cumulative_factors(rows, events, use_canonical=False)
        assert raw[("E00001", 2019)] == pytest.approx(7.0)


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
        assert M.COLUMN_DIRECTION["nc_ratio"] == -1

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


class TestMatchEvent:
    E = dict(prev_period_end="2023-03-31", period_end="2024-03-31")

    def test_official_factor_is_inverted(self):
        """公式の AdjFactor は過去株価に掛ける係数。1:2 分割は 0.5 で返る。"""
        e = ev(2024, canonical=2.0, sh_ratio=2.0, **self.E)
        r = M.match_event(e, [("2023-10-02", 0.5)])
        assert r.status == "agree"
        assert r.official == pytest.approx(2.0)

    def test_two_events_in_one_window_multiply(self):
        e = ev(2024, canonical=4.0, sh_ratio=4.0, **self.E)
        r = M.match_event(e, [("2023-10-02", 0.5), ("2023-11-01", 0.5)])
        assert r.status == "agree" and r.n_official_events == 2

    def test_no_official_event(self):
        e = ev(2024, canonical=2.0, sh_ratio=2.0, **self.E)
        assert M.match_event(e, []).status == "no_official_event"

    def test_window_slack_controls_inclusion(self):
        e = ev(2024, canonical=2.0, sh_ratio=2.0, **self.E)
        assert M.match_event(e, [("2024-08-01", 0.5)]).status == "no_official_event"
        assert M.match_event(e, [("2024-08-01", 0.5)],
                             slack_days=180).status == "agree"

    def test_event_window_absorbs_the_period_shift(self):
        e = ev(2024, **self.E)
        assert M.event_window(e, slack_days=45) == ("2023-02-14", "2024-05-15")

    def test_in_coverage_requires_the_whole_window(self):
        """窓が契約期間へ**完全に**収まるときだけ判定できる。

        抽出をこの条件で絞らないと、大半が out_of_coverage に落ちて分母が消える
        （実測 2026-09-12: 30件中 21件が窓外で分母 9 まで縮んだ）。
        """
        e = ev(2024, **self.E)
        assert M.in_coverage(e, ("2023-01-01", "2026-06-20")) is True
        assert M.in_coverage(e, ("2024-06-20", "2026-06-20")) is False   # 窓の頭が外
        assert M.in_coverage(e, ("2023-01-01", "2024-03-31")) is False   # 窓の尻が外
        assert M.in_coverage(e, None) is True                            # 窓を知らなければ通す

    def test_out_of_coverage_is_excluded(self):
        """契約窓の外は分母から外す。混ぜると一致率が理由なく下がる。"""
        e = ev(2019, canonical=2.0, sh_ratio=2.0,
               prev_period_end="2018-03-31", period_end="2019-03-31")
        r = M.match_event(e, [("2018-10-01", 0.5)],
                          coverage=("2020-01-01", "2026-09-01"))
        assert r.status == "out_of_coverage"

    def test_snap_is_credited_separately_from_raw(self):
        """生比では外れ、スナップ後で合う＝スナップが効いた証拠。別ラベルで数える。"""
        # 実測の 4.7891 は 5.0 へ寄り、公式 0.2（= 1:5）と一致する
        e = ev(2024, canonical=5.0, sh_ratio=4.7891, **self.E)
        assert M.match_event(e, [("2023-10-02", 0.2)]).status == "agree"
        # 残差が突合許容（5%）より大きい合成では、生比だけが合う。スナップの当否が数字に出る
        e2 = ev(2024, canonical=5.0, sh_ratio=4.5, **self.E)
        r = M.match_event(e2, [("2023-10-02", 1 / 4.5)])
        assert r.status == "agree_raw_only"

    def test_magnitude_disagreement(self):
        e = ev(2024, canonical=2.0, sh_ratio=2.0, **self.E)
        assert M.match_event(e, [("2023-10-02", 0.2)]).status == "disagree_magnitude"

    def test_bps_event_is_matched_on_the_lagged_shares_ratio(self):
        """第2経路の生比は倍率を決めた量＝翌年の株数比（#659）。

        `bps_ratio` を生比に使っていた頃の意味のままにすると、`agree_raw_only` が
        「もう倍率に使っていない量では合う」を数えることになり、指標が黙って壊れる。
        """
        e = ev(2024, canonical=2.0, sh_ratio=1.0, **self.E)._replace(
            source="bps", bps_ratio=1.7036, lagged_sh_ratio=2.0)
        r = M.match_event(e, [("2023-10-02", 0.5)])
        assert r.status == "agree"
        assert r.raw_detected == pytest.approx(2.0)


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
        assert M.in_coverage(e, self.COVER) is False
        assert M.in_coverage(e, self.COVER, mode="partial") is True

    def test_official_event_inside_the_overlap_is_compared(self):
        e = self._ev()
        r = M.match_event(e, [("2024-10-01", 0.5)], coverage=self.COVER,
                          coverage_mode="partial")
        assert r.status == "agree" and r.official == pytest.approx(2.0)

    def test_official_event_before_the_overlap_is_not_used(self):
        """重なりの外にある公式イベントは、そもそも API が返さない。拾いにいかない。"""
        e = self._ev()
        r = M.match_event(e, [("2024-04-01", 0.5)], coverage=self.COVER,
                          coverage_mode="partial")
        assert r.status == "no_official_event_partial"

    def test_partial_miss_is_excluded_from_the_denominator(self):
        """**ここが partial の肝**。「分割が無かった」と「窓の外で起きた」を区別できない。

        full の `no_official_event` は分母に入る（公式が返さない＝分割が無かったと読めるので
        検出が間違いだと言える）。partial の同じ状況は分母から外す。
        """
        rows = [M.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                M.MatchResult("E2", 2025, 2.0, 2.0, 3.0, 1, "disagree_magnitude"),
                M.MatchResult("E3", 2025, 2.0, 2.0, None, 0, "no_official_event_partial"),
                M.MatchResult("E4", 2025, 2.0, 2.0, None, 0, "out_of_coverage")]
        tally, denom, rate, rate_raw = M.tally_rates(rows)
        assert denom == 2
        assert rate == pytest.approx(0.5)
        assert rate_raw == pytest.approx(0.5)
        assert tally["no_official_event_partial"] == 1

    def test_full_miss_stays_in_the_denominator(self):
        rows = [M.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                M.MatchResult("E2", 2025, 2.0, 2.0, None, 0, "no_official_event")]
        _, denom, rate, _ = M.tally_rates(rows)
        assert denom == 2 and rate == pytest.approx(0.5)

    def test_empty_denominator_does_not_divide_by_zero(self):
        rows = [M.MatchResult("E1", 2025, 2.0, 2.0, None, 0, "out_of_coverage")]
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
        factors = M.cumulative_factors(rows, events)
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


def test_module_does_not_import_database_at_top_level():
    """トップで重い import をしない（テストを DB 無しで回すため・#653）。"""
    src = (ROOT / "scripts" / "measure_split_valuation_bias.py").read_text(encoding="utf-8")
    head = src.split("# ── I/O ──")[0]
    for bad in ("\nimport database", "\nfrom database import",
                "\nimport collector_prices", "\nimport httpx"):
        assert bad not in head


def test_canonical_ratios_are_log_sorted_and_unique():
    assert len(set(M.CANONICAL_RATIOS)) == len(M.CANONICAL_RATIOS)
    assert all(r > 0 for r in M.CANONICAL_RATIOS)
    assert math.isclose(min(M.CANONICAL_RATIOS), 1 / 20)
