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
        period_end=None, equity=None) -> M.AnnualRow:
    return M.AnnualRow(
        edinet_code=ec, year=year,
        period_end=period_end or ("%d-03-31" % year),
        issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=dps,
        stock_price=price, per=per, pbr=pbr, div_yield=div_yield, market_cap=market_cap,
        bs_total_equity=equity)


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

    @pytest.mark.parametrize("ec,y0,prev,y1,cur,expected", [
        # E03717 unbanked の 3:1 併合（2024-09-27・公式 AdjFactor 1/3・Yahoo 1:3）。値は DB の実値
        ("E03717", 2024, (30070543.0, 185.21, 13.17, 5569522000.0),
         2025, (10023514.0, 552.41, 23.63, 5752092000.0), 1 / 3),
        # E37831 INEST の 15:1 併合（2025-09-29・公式 1/15・Yahoo 1:15）
        ("E37831", 2025, (109596485.0, 30.42, 0.39, 4944000000.0),
         2026, (7306432.0, 458.87, 24.74, 5103000000.0), 1 / 15),
    ])
    def test_confirmed_reverse_splits_snap_to_added_ratios(self, ec, y0, prev, y1, cur, expected):
        """#669 で足した比。足す前は `unsnapped` で F に入らず、併合前の断面が補正されなかった。"""
        rows = [row(y0, prev[0], prev[1], eps=prev[2], equity=prev[3], ec=ec),
                row(y1, cur[0], cur[1], eps=cur[2], equity=cur[3], ec=ec)]
        events, _ = M.detect_events(rows)
        assert [(e.source, e.kind, e.canonical) for e in events] == [
            ("shares", "reverse", pytest.approx(expected))]
        assert M.cumulative_factors(rows, events)[(ec, y0)] == pytest.approx(expected)

    @pytest.mark.parametrize("ec,y0,prev,y1,cur", [
        # E05698 UT グループの 1:15（2025-12-29・公式/Yahoo とも一致）。**本物だが補正されない**
        ("E05698", 2025, (39860383.0, 741.37, 225.32, 36323000000.0),
         2026, (601193745.0, 44.26, 12.37, 32141000000.0)),
        # E05714 ソニーフィナンシャルグループ。2020 年に上場廃止し 2026 年に別の株数で再上場した
        # 行が隣り合っただけで、分割ではない（公式・Yahoo ともイベント無し）
        ("E05714", 2020, (435087405.0, 1584.9, 171.09, 691978000000.0),
         2026, (6770358214.0, 93.74, 7.96, 629284000000.0)),
    ])
    def test_fifteen_is_not_in_the_table(self, ec, y0, prev, y1, cur):
        """**15 は定番比の表に足さない**（#669・ADR-0055 決定4-6）。

        足すと E05698 の本物の 1:15 は split で入るが、E05714 の株数比 15.56 も composite で
        15 へ寄り、分割の無い 2019〜2020 年の断面へ F=15 が掛かる。比を足すのは、前後で倍率が
        変わるイベントが全部確かめられたときだけ、という規則でここは落とした。
        """
        rows = [row(y0, prev[0], prev[1], eps=prev[2], equity=prev[3], ec=ec),
                row(y1, cur[0], cur[1], eps=cur[2], equity=cur[3], ec=ec)]
        events, _ = M.detect_events(rows)
        assert [(e.kind, e.canonical) for e in events] == [("unsnapped", None)]
        assert M.cumulative_factors(rows, events)[(ec, y0)] == 1.0
        assert 15.0 not in M.CANONICAL_RATIOS

    def test_share_count_ratio_near_six_stays_composite_five(self):
        """E05426 みずほリースの翌年株数比 5.7682 は 1:5 分割（Yahoo 2024-03-28）＋増資。

        #669 は 1:6 を疑ったが本物の比は 5 で、今の composite 5.0 が正しい。6 は足さない。
        """
        c, residual, kind = M.snap_to_canonical(282666300.0 / 49004000.0)
        assert (c, kind) == (pytest.approx(5.0), "composite")
        assert residual == pytest.approx(5.7682 / 5.0, abs=1e-4)   # 増資の分（純資産 +21.7%）

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


class TestOfficialMagnitude:
    """翌年の行が無い第2経路の倍率を公式 `AdjFactor` から取る — Issue #661。

    公式の値は DB に残したもの（`jquants_adj_factor_events`）を呼び出し側が渡す。検出器は
    純関数のままで、**渡されなければ #659 と1件も違わない**。
    """

    # 最新年（2021）に bps / eps が半分・株数は据え置き・翌年の行は無い。窓は
    # (2020-03-31 - 45日, 2021-03-31 + 45日] = (2020-02-15, 2021-05-15]。
    ROWS = (
        (2020, 1000.0, 200.0, 20.0),
        (2021, 1000.0, 100.0, 10.0),
    )

    def _rows(self, ec="E00001"):
        return [row(y, sh, bps, eps=eps, ec=ec) for y, sh, bps, eps in self.ROWS]

    def test_official_event_in_the_window_fills_the_magnitude(self):
        events, stats = M.detect_events(self._rows(), bps_path=True,
                                        official_events={"E00001": [("2020-10-01", 0.5)]})
        assert [(e.year, e.canonical, e.kind, e.source) for e in events] == [
            (2021, pytest.approx(2.0), "split", "bps")]
        e = events[0]
        assert e.official_ratio == pytest.approx(2.0)
        assert e.lagged_sh_ratio is None
        # 観測値は捏造せずそのまま残す
        assert e.sh_ratio == pytest.approx(1.0) and e.bps_ratio == pytest.approx(2.0)
        assert stats["bps_path"]["magnitude_source"] == {"lagged_shares": 0, "official": 1}
        assert stats["bps_path"]["awaiting_magnitude"] == []
        assert "no_lagged_row" not in stats["bps_path"]["rejected"]

    @pytest.mark.parametrize("official", [
        {},                                         # 取り込み前・その晩に取れなかった
        {"E00001": []},
        {"E09999": [("2020-10-01", 0.5)]},          # 別の社のイベント
        {"E00001": [("2020-02-15", 0.5)]},          # 窓の左端は開区間
        {"E00001": [("2021-05-16", 0.5)]},          # 窓の右端より後
    ])
    def test_no_official_event_in_the_window_is_not_taken(self, official):
        """**行が無いことを「分割は無かった」とも「倍率は1」とも読まない**。今日までどおり採らず、
        倍率待ちとして残す＝公式が落ちた晩も補正が誤るのではなく採らない側へ倒れる。"""
        events, stats = M.detect_events(self._rows(), bps_path=True, official_events=official)
        assert events == []
        assert stats["bps_path"]["rejected"]["no_lagged_row"] == 1
        assert stats["bps_path"]["awaiting_magnitude"] == [{
            "edinet_code": "E00001", "year": 2021, "prev_period_end": "2020-03-31",
            "period_end": "2021-03-31", "bps_ratio": pytest.approx(2.0)}]

    def test_none_and_empty_mapping_reproduce_the_previous_behaviour(self):
        """`official_events=None`（測定器の CLI）と `{}`（表が空の夜）は #659 と完全に一致する。"""
        rows = TestBpsPath()._rows()
        base, base_stats = M.detect_events(rows, bps_path=True)
        for official in (None, {}):
            events, stats = M.detect_events(rows, bps_path=True, official_events=official)
            assert events == base
            assert stats["bps_path"]["rejected"] == base_stats["bps_path"]["rejected"]
        assert base_stats["bps_path"]["official"]["enabled"] is False

    def test_flat_official_ratio_is_rejected(self):
        """公式は 1:1.1 しか動いていない。bps の半減を説明しないので採らない。"""
        events, stats = M.detect_events(
            self._rows(), bps_path=True,
            official_events={"E00001": [("2020-10-01", 1.0 / 1.1)]})
        assert events == []
        assert stats["bps_path"]["rejected"]["official_flat"] == 1
        assert stats["bps_path"]["awaiting_magnitude"] == []

    def test_opposite_direction_official_ratio_is_rejected(self):
        """bps は分割方向・公式は併合方向（別の社の値が付いた等）。同じ事象ではない。"""
        events, stats = M.detect_events(
            self._rows(), bps_path=True, official_events={"E00001": [("2020-10-01", 2.0)]})
        assert events == []
        assert stats["bps_path"]["rejected"]["official_direction"] == 1

    @pytest.mark.parametrize("prev_pe,cur_pe,adj_factors,expected", [
        # E38205 の実値（1:6 が1件）。定番比の表に 6 は無く、丸めると 5（composite）へ寄る
        ("2024-06-30", "2025-06-30", [("2025-06-27", 1.0 / 6.0)], 6.0),
        # E38979 の実値（1:2 と 1:3 が同じ窓 (2024-05-16, 2025-08-14] に2件・積 6）
        ("2024-06-30", "2025-06-30", [("2024-12-27", 0.5), ("2025-06-27", 1.0 / 3.0)], 6.0),
        # E02128 の実値（1:7）。丸めると残差 1.4 で unsnapped になり採られない
        ("2025-03-31", "2026-03-31", [("2025-09-29", 1.0 / 7.0)], 7.0),
    ])
    def test_official_ratio_is_used_as_is_without_snapping(self, prev_pe, cur_pe,
                                                           adj_factors, expected):
        """**公式の比は定番比へ丸めない**（#661・2026-09-13 実測で判明）。丸めは株数比に増資の分が
        混ざるのを切り離す仕組みで、`AdjFactor` は株価の遡及調整に使われた係数そのものである。"""
        # 日付の実値は取り込んだ `jquants_adj_factor_events`。株数・bps は形だけ（bps 比 = 0.9F）
        y0, y1 = int(prev_pe[:4]), int(cur_pe[:4])
        rows = [row(y0, 1000.0, 100.0 * expected * 0.9, eps=10.0 * expected * 0.9,
                    period_end=prev_pe),
                row(y1, 1000.0, 100.0, eps=10.0, period_end=cur_pe)]
        assert M.snap_to_canonical(expected)[0] != expected   # 丸めると真の比から外れる、を先に押さえる
        events, _ = M.detect_events(rows, bps_path=True, official_events={"E00001": adj_factors})
        assert [(e.canonical, e.kind, e.residual) for e in events] == [
            (pytest.approx(expected), "split", 1.0)]
        assert M.cumulative_factors(rows, events)[("E00001", y0)] == pytest.approx(expected)

    def test_several_official_events_in_the_window_multiply(self):
        """同じ窓に 1:2 が2回ある。株数比は積の 4.0（`match_event` と同じ定義）。"""
        rows = [row(2020, 1000.0, 400.0, eps=40.0), row(2021, 1000.0, 100.0, eps=10.0)]
        events, _ = M.detect_events(
            rows, bps_path=True,
            official_events={"E00001": [("2020-06-01", 0.5), ("2020-12-01", 0.5)]})
        assert [(e.canonical, e.official_ratio) for e in events] == [
            (pytest.approx(4.0), pytest.approx(4.0))]

    def test_lagged_shares_win_and_disagreement_is_only_counted(self):
        """翌年の株数があるペアは公式を倍率に使わない。**食い違っても同じイベントのまま**、
        交差検証の件数と中身だけが変わる（既存の係数表を公式で動かさない）。"""
        rows = TestBpsPath()._rows()
        base, _ = M.detect_events(rows, bps_path=True)
        # 2024 のイベント（翌年の株数 x2）の窓 (2023-02-14, 2024-05-15] に公式 1:3 を置く
        events, stats = M.detect_events(rows, bps_path=True,
                                        official_events={"E03137": [("2023-10-01", 1.0 / 3.0)]})
        assert events == base
        cc = stats["bps_path"]["official"]["crosscheck"]
        assert (cc["agree"], cc["disagree"]) == (0, 1)
        assert cc["disagreements"][0]["edinet_code"] == "E03137"
        assert cc["disagreements"][0]["year"] == 2024
        assert cc["disagreements"][0]["official"] == pytest.approx(3.0)

    def test_agreeing_official_event_is_counted_as_agree(self):
        rows = TestBpsPath()._rows()
        _, stats = M.detect_events(rows, bps_path=True,
                                   official_events={"E03137": [("2023-10-01", 0.5)]})
        cc = stats["bps_path"]["official"]["crosscheck"]
        assert (cc["agree"], cc["disagree"], cc["disagreements"]) == (1, 0, [])

    def test_latest_year_official_event_reaches_the_cumulative_factor(self):
        """しまむら型の 2026（翌年の行が無い 1:3）が公式で埋まると、2025 以前の F に掛かる。

        公式イベントは E03137 の `jquants_adj_factor_events` 実値（2026-09-13 取り込み・
        `2026-02-19` の `AdjFactor` 1/3）。2024 のイベントは翌年の株数で決まったままで、
        公式を渡しても変わらない。
        """
        rows = TestBpsPath()._rows()
        events, stats = M.detect_events(rows, bps_path=True,
                                        official_events={"E03137": [("2026-02-19", 1.0 / 3.0)]})
        assert [(e.year, e.canonical) for e in events] == [
            (2024, pytest.approx(2.0)), (2026, pytest.approx(3.0))]
        assert stats["bps_path"]["magnitude_source"] == {"lagged_shares": 1, "official": 1}
        f = M.cumulative_factors(rows, events)
        assert f[("E03137", 2023)] == pytest.approx(6.0)
        assert f[("E03137", 2024)] == pytest.approx(3.0)
        assert f[("E03137", 2025)] == pytest.approx(3.0)
        assert f[("E03137", 2026)] == pytest.approx(1.0)

    def test_shares_path_is_untouched_by_official_events(self):
        """第1経路（当年の株数比）は公式を見ない。公式が食い違っても同じイベントのまま。"""
        rows = [row(2020, 1000.0, 200.0), row(2021, 2000.0, 100.0)]
        base, _ = M.detect_events(rows)
        events, _ = M.detect_events(rows, official_events={"E00001": [("2020-10-01", 0.2)]})
        assert events == base


class TestOfficialRatioInWindow:
    def test_reciprocal_of_the_product_inside_the_half_open_window(self):
        official = [("2020-01-01", 0.5), ("2020-06-01", 0.5), ("2021-01-01", 0.1)]
        assert M.official_ratio_in_window(official, ("2020-01-01", "2020-12-31")) == (
            pytest.approx(2.0), 1)
        assert M.official_ratio_in_window(official, ("2019-12-31", "2020-12-31")) == (
            pytest.approx(4.0), 2)

    def test_nothing_inside_or_no_window(self):
        assert M.official_ratio_in_window([("2020-06-01", 0.5)], ("2021-01-01", "2021-12-31")) == (
            None, 0)
        assert M.official_ratio_in_window([("2020-06-01", 0.5)], None) == (None, 0)
        # 0 以下の係数は壊れた値として数えない
        assert M.official_ratio_in_window([("2020-06-01", 0.0)], ("2020-01-01", "2020-12-31")) == (
            None, 0)


class TestEquityCheck:
    """第1経路の純資産総額チェック — Issue #657。

    検体は `financial_records` の実値をそのまま写した（2026-09-13・接続先 local）。

        E01121 日本板硝子 | year | issued_shares | bs_bps  | pl_eps  | bs_total_equity
                          | 2025 |    91,568,599 | 3182.04 | -173.20 | 142,411,000,000
                          | 2026 |   142,341,906 | 2230.45 |   44.51 | 185,519,000,000
        E05716 地域新聞社 | 2024 |     2,670,276 |  113.20 |    1.55 |     302,271,000
                          | 2025 |     3,741,914 |   87.22 |    5.52 |     653,233,000
        E01777 ソニーG    | 2024 | 1,261,231,889 | 2661.69 |  788.29 | 7,756,105,000,000
                          | 2025 | 6,149,810,645 |  540.61 |  188.71 | 8,510,151,000,000

    **この信号で分離できるのは `bs_bps` と純資産総額が食い違う社だけ**である。
    `bps逆比 / 株数比 = 1 / 純資産比` なので、`bs_bps ≈ 純資産 / 株数` が成り立つ社では
    bps の交差検証（許容 15%）が既に「純資産の伸びが ±15% 程度」を見ている。E01121 は
    `(bps逆比/株数比) x 純資産比 = 1.196`、E05716 は 2.002 で、ここが食い違っている。
    """

    NSG = ((2025, 91568599.0, 3182.04, -173.2, 142411000000.0),
           (2026, 142341906.0, 2230.45, 44.51, 185519000000.0))
    CHIIKI = ((2024, 2670276.0, 113.2, 1.55, 302271000.0),
              (2025, 3741914.0, 87.22, 5.52, 653233000.0))
    SONY = ((2024, 1261231889.0, 2661.69, 788.29, 7756105000000.0),
            (2025, 6149810645.0, 540.61, 188.71, 8510151000000.0))

    def _rows(self, table, ec):
        return [row(y, sh, bps, eps=eps, equity=eq, ec=ec) for y, sh, bps, eps, eq in table]

    @pytest.mark.parametrize("table,ec,eq_ratio", [
        (NSG, "E01121", 1.3027), (CHIIKI, "E05716", 2.1611)])
    def test_equity_jump_rejects_the_known_false_positives(self, table, ec, eq_ratio):
        """株数と同じ向きに純資産が許容を超えて動いた。増資と読んで採らない。"""
        rows = self._rows(table, ec)
        off, _ = M.detect_events(rows, bps_path=False, equity_tol=None)
        assert len(off) == 1 and off[0].kind == "composite"          # 今日までは採っていた
        assert off[0].equity_ratio == pytest.approx(eq_ratio, abs=1e-4)
        on, stats = M.detect_events(rows, bps_path=False, equity_tol=0.25)
        assert on == []
        eq = stats["equity"]
        assert eq["enabled"] is True and eq["tol"] == 0.25
        assert eq["n_rejected"] == 1 and eq["rejected_by_kind"] == {"composite": 1}
        assert [(r["edinet_code"], r["year"]) for r in eq["rejected"]] == [(ec, table[1][0])]
        assert eq["rejected"][0]["equity_ratio"] == pytest.approx(eq_ratio, abs=1e-4)

    def test_real_split_with_organic_growth_is_kept(self):
        """ソニーG の 1:5。純資産は業績で +9.7% 伸びているが、許容の内側なので残る。"""
        events, stats = M.detect_events(self._rows(self.SONY, "E01777"),
                                        bps_path=False, equity_tol=0.15)
        assert [(e.year, e.canonical, e.kind) for e in events] == [(2025, 5.0, "composite")]
        assert events[0].equity_ratio == pytest.approx(1.0972, abs=1e-4)
        assert stats["equity"]["n_rejected"] == 0

    def test_equity_falling_while_shares_rise_is_not_evidence_of_issuance(self):
        """赤字・減損で純資産が減るのは増資の証拠にならない。片側でしか判定しない。"""
        rows = [row(2020, 1000.0, 200.0, equity=200000.0),
                row(2021, 2000.0, 100.0, equity=100000.0)]
        events, stats = M.detect_events(rows, bps_path=False, equity_tol=0.25)
        assert len(events) == 1
        assert events[0].equity_ratio == pytest.approx(0.5)
        assert stats["equity"]["n_rejected"] == 0

    def test_reverse_direction_rejects_a_same_direction_drop(self):
        """併合側は株数と純資産が一緒に減ったときに落ちる（減資・自己株式の取得）。"""
        rows = [row(2020, 2000.0, 100.0, equity=200000.0),
                row(2021, 1000.0, 200.0, equity=100000.0)]
        assert M.detect_events(rows, bps_path=False, equity_tol=0.25)[0] == []
        flat = [row(2020, 2000.0, 100.0, equity=200000.0),
                row(2021, 1000.0, 200.0, equity=200000.0)]
        events, _ = M.detect_events(flat, bps_path=False, equity_tol=0.25)
        assert len(events) == 1 and events[0].kind == "reverse"

    @pytest.mark.parametrize("eq_prev,eq_cur", [(None, 100000.0), (200000.0, None),
                                                (0.0, 100000.0), (-5.0, 100000.0)])
    def test_unknown_equity_keeps_the_event_and_is_counted(self, eq_prev, eq_cur):
        """判定できない社を「増資でない」とも「増資だ」とも読まない。今日までどおり採って数える。"""
        rows = [row(2020, 1000.0, 200.0, equity=eq_prev), row(2021, 2000.0, 100.0, equity=eq_cur)]
        events, stats = M.detect_events(rows, bps_path=False, equity_tol=0.25)
        assert len(events) == 1 and events[0].equity_ratio is None
        assert stats["equity"]["n_unknown"] == 1
        assert stats["equity"]["n_rejected"] == 0

    def test_unknown_is_not_counted_when_the_check_is_off(self):
        rows = [row(2020, 1000.0, 200.0), row(2021, 2000.0, 100.0)]
        _, stats = M.detect_events(rows, bps_path=False, equity_tol=None)
        assert stats["equity"] == {"enabled": False, "tol": None, "n_rejected": 0,
                                   "rejected_by_kind": {}, "n_unknown": 0, "rejected": []}

    def test_disabled_check_reproduces_the_previous_behaviour(self):
        """`equity_tol=None` は #659 までの検出と 1 件も違わないこと（倍率・種別・経路）。"""
        rows = (self._rows(self.NSG, "E01121") + self._rows(self.CHIIKI, "E05716")
                + self._rows(self.SONY, "E01777") + TestBpsPath()._rows())
        plain = [r._replace(bs_total_equity=None) for r in rows]
        a, _ = M.detect_events(rows, equity_tol=None)
        b, _ = M.detect_events(plain, equity_tol=None)
        strip = lambda evs: [e._replace(equity_ratio=None) for e in evs]  # noqa: E731
        assert strip(a) == strip(b)
        assert len(a) == 4

    def test_bps_path_is_not_gated(self):
        """第2経路は倍率を翌年の株数から取る別の主張なので、純資産比では落とさない。"""
        rows = [row(y, sh, bps, eps=eps, ec="E03137", equity=eq) for (y, sh, bps, eps), eq
                in zip(TestBpsPath.SHIMAMURA, (1.0e11, 1.0e11, 3.0e11, 3.0e11))]
        a, _ = M.detect_events(rows, bps_path=True, equity_tol=None)
        b, _ = M.detect_events(rows, bps_path=True, equity_tol=0.15)
        assert a == b and [(e.year, e.source) for e in b] == [(2024, "bps")]

    def test_rejected_pair_can_still_be_taken_by_the_bps_path(self):
        """第1経路が落としたペアを第2経路が独立に拾うことは妨げない（畳む規則と揃える）。

        2021 は株数 x2・bps 半減・純資産 x2（増資型）。2022 に株数がもう一度 x2 になり、
        bps 経路は「2021 に 1 株指標が動き、翌年に株数が追随した」と主張する。
        """
        rows = [row(2020, 1000.0, 200.0, eps=20.0, equity=200000.0),
                row(2021, 2000.0, 100.0, eps=10.0, equity=400000.0),
                row(2022, 4000.0, 100.0, eps=10.0, equity=400000.0)]
        events, stats = M.detect_events(rows, bps_path=True, equity_tol=0.25)
        assert [(e.year, e.source) for e in events] == [(2021, "bps")]
        assert stats["equity"]["n_rejected"] == 1

    # E36173 の実値（2026-09-13・接続先 local）。純資産 x1.9848 だが Yahoo に 2023-05-16 の
    # 1:2 分割がある本物。**既定の許容を 1.0 より下げられない理由そのもの**（決定4-4）。
    E36173 = ((2022, 4939380.0, 175.99, -53.7, 879146000.0),
              (2023, 10072890.0, 94.1, -20.47, 1744943000.0))

    def test_default_drops_only_what_the_census_allowed(self):
        """既定（1.0）で落ちるのは E05716 だけ。E01121 は偽陽性と確定しているが残る。

        E01121 を落とせる許容（< 0.30）では、本物の E36173 も一緒に落ちる。
        """
        assert M.DEFAULT_EQUITY_TOL == 1.0
        rows = (self._rows(self.NSG, "E01121") + self._rows(self.CHIIKI, "E05716")
                + self._rows(self.E36173, "E36173") + self._rows(self.SONY, "E01777"))
        events, stats = M.detect_events(rows)
        assert sorted(e.edinet_code for e in events if e.source == "shares") == [
            "E01121", "E01777", "E36173"]
        assert [r["edinet_code"] for r in stats["equity"]["rejected"]] == ["E05716"]
        # 0.25 まで下げると本物の E36173 を巻き込む
        _, low = M.detect_events(rows, equity_tol=0.25)
        assert {r["edinet_code"] for r in low["equity"]["rejected"]} >= {"E01121", "E36173"}

    def test_default_is_declared_in_one_place(self):
        """既定は定数1つ。`rebuild_split_adjustment_factors` は書き写さずこれに従う。"""
        import inspect
        sig = inspect.signature(M.detect_events)
        assert sig.parameters["equity_tol"].default == M.DEFAULT_EQUITY_TOL
        assert M.DEFAULT_EQUITY_TOL is None or M.DEFAULT_EQUITY_TOL in M.EQUITY_TOL_GRID

    def test_annual_row_still_accepts_twelve_positional_fields(self):
        """`collector_prices` とテストの位置指定生成（12 列）を壊さない。"""
        r = M.AnnualRow("E1", 2020, "2020-03-31", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
        assert r.bs_total_equity is None


class TestEquityGateCrosstab:
    """チェックが落とすイベントを突合ステータスで数える — Issue #657 の既定判定の材料。"""

    def _ungated(self):
        return [ev(2025, ec="E1"), ev(2025, ec="E2"), ev(2025, ec="E3"),
                ev(2025, ec="E4"), ev(2025, ec="E5")]

    def _results(self):
        return [M.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                M.MatchResult("E2", 2025, 2.0, 2.0, None, 0, "no_official_event"),
                M.MatchResult("E3", 2025, 2.0, 2.0, 3.0, 1, "disagree_magnitude"),
                M.MatchResult("E4", 2025, 2.0, 2.0, 2.0, 1, "agree")]

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
        results = [M.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree_raw_only")]
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
        results = [M.MatchResult("E1", 2025, 2.0, 2.0, 2.0, 1, "agree"),
                   M.MatchResult("E2", 2025, 2.0, 2.0, None, 0, "no_official_event"),
                   M.MatchResult("E3", 2025, 2.0, 2.0, 2.0, 1, "agree")]
        got = M.tally_by_group(results, events)
        assert got == {"shares:composite": {"agree": 1, "no_official_event": 1},
                       "shares:split": {"agree": 1}}


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

    def test_bps_event_without_a_lagged_row_is_matched_on_the_official_ratio(self):
        """翌年の行が無く公式で倍率を決めたイベント（#661）の生比は公式の比。bps 比へ落ちない。"""
        e = ev(2024, canonical=2.0, sh_ratio=1.0, **self.E)._replace(
            source="bps", bps_ratio=1.7036, official_ratio=2.0)
        assert M.match_event(e, [("2023-10-02", 0.5)]).raw_detected == pytest.approx(2.0)


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
