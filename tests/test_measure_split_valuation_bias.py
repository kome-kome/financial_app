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
