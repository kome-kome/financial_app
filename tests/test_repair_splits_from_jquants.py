"""#466 の分割修復スクリプトの判定ロジック。**ネットワークにも DB にも触れない。**

守る不変条件は「何を直すか」ではなく **「何を直さないか」**:

1. **直近で公式と一致しない銘柄は直さない。** 過去の分割の未反映なら直近は必ず一致する。
   一致しないなら原因が別にある（E32779: 公式イベント none なのに 5/6 ずれ）。
2. **公式イベントで説明できない段差は直さない。** 直すと**分割の無い銘柄にニセの分割を
   作る**行為になり、週次リターンを入力に持つ M-1 / M-2 / M-6 へ誤った企業イベントを
   伝播させる。ただし公式が持たないスピンオフは測る前に換算する（E02086・#568）。
3. **公式イベントがあるのに段差が無いのは正常。** Yahoo が既に知っている分割は DB 側で
   調整済みなので比が動かない（E02978 の 2024-07-30）。ここを「イベントには段差が対応
   するはず」と書くと、正しく調整済みの銘柄を弾く。
4. **低位株の丸めを段差と読まない。** 株価 21円の銘柄では 1円の丸めが 4.8% に化ける。

実行: pytest tests/test_repair_splits_from_jquants.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import repair_splits_from_jquants as R  # noqa: E402


def bars(*items) -> list:
    """(date, AdjC, AdjFactor) から J-Quants 風の行を作る。"""
    return [{"Date": d, "AdjC": c, "AdjFactor": f} for d, c, f in items]


# ── 公式イベントの抽出 ──────────────────────────────────────────────────────

class TestExtractEvents:
    def test_picks_only_non_unit_factors(self):
        rows = bars(("2025-03-27", 100.0, 1.0),
                    ("2025-03-28", 100.0, 0.909091),
                    ("2025-03-31", 100.0, 1.0))
        assert R.extract_events(rows) == [("2025-03-28", 0.909091)]

    def test_multiple_events_are_sorted(self):
        rows = bars(("2025-03-28", 1.0, 0.1),
                    ("2024-07-30", 1.0, 10.0),
                    ("2024-09-04", 1.0, 0.75))
        assert [d for d, _ in R.extract_events(rows)] == [
            "2024-07-30", "2024-09-04", "2025-03-28"]

    def test_missing_factor_is_not_an_event(self):
        assert R.extract_events([{"Date": "2025-01-01", "AdjC": 10.0}]) == []


# ── 丸め許容 ────────────────────────────────────────────────────────────────

class TestRoundingTolerance:
    def test_low_priced_stock_gets_a_wide_tolerance(self):
        """株価 21円（E01300）では 1円の丸めが約 4.8% に化ける。"""
        assert R.rounding_tolerance(21.0, 24.7) == pytest.approx(1.0 / 21.0, rel=1e-9)

    def test_high_priced_stock_falls_back_to_the_floor(self):
        assert R.rounding_tolerance(7870.0, 2623.3) == R.REL_TOL_FLOOR

    def test_zero_is_not_a_division_error(self):
        assert R.rounding_tolerance(0.0, 100.0) == R.REL_TOL_FLOOR


# ── 段差検出 ────────────────────────────────────────────────────────────────

class TestFindSteps:
    def test_constant_ratio_has_no_step(self):
        ratios = [("2025-01-06", 0.909091, 1000.0, 909.1),
                  ("2025-01-14", 0.909100, 1010.0, 918.2)]
        assert R.find_steps(ratios) == []

    def test_real_step_is_detected(self):
        ratios = [("2025-03-24", 0.909091, 1000.0, 909.1),
                  ("2025-03-31", 1.000000, 1000.0, 1000.0)]
        steps = R.find_steps(ratios)
        assert len(steps) == 1
        assert steps[0][0] == "2025-03-24" and steps[0][1] == "2025-03-31"

    def test_rounding_noise_on_a_penny_stock_is_not_a_step(self):
        """#466 の「丸めた表示で定数と誤結論した」の逆向き＝生値の揺れを段差と読まない。"""
        ratios = [("2025-01-06", 1.172727, 21.0, 24.6),
                  ("2025-01-14", 1.176190, 21.0, 24.7)]
        assert R.find_steps(ratios) == []


# ── 判定（このファイルの中心）───────────────────────────────────────────────

class TestValidate:
    def test_accepts_a_step_backed_by_an_official_event(self):
        ratios = [("2025-03-24", 0.909091, 1000.0, 909.1),
                  ("2025-03-31", 1.000000, 1000.0, 1000.0)]
        ok, reason = R.validate(ratios, [("2025-03-28", 0.909091)])
        assert ok, reason

    def test_rejects_when_latest_ratio_is_not_one(self):
        """E32779 型: 公式イベントが無いのに直近まで 5/6 ずれている。"""
        ratios = [("2025-03-24", 0.833333, 3000.0, 2500.0),
                  ("2026-05-01", 0.833333, 3600.0, 3000.0)]
        ok, reason = R.validate(ratios, [])
        assert not ok
        assert "直近で公式と一致しない" in reason

    def test_rejects_a_step_with_no_official_event(self):
        """段差の時期に公式イベントが無い（#466 当時の E02086 の見え方。#568 でスピンオフと
        判明し換算で解消したが、登録の無い社ではこの型のまま棄却する）。"""
        ratios = [("2024-09-27", 1.854000, 1000.0, 1854.0),
                  ("2024-10-04", 1.000000, 1000.0, 1000.0)]
        ok, reason = R.validate(ratios, [("2026-03-30", 0.5)])
        assert not ok
        assert "公式イベントが無い" in reason

    def test_event_without_a_step_is_fine(self):
        """E02978 型: Yahoo が既に知っている分割は DB 側で調整済み＝段差が出ないのが正しい。

        ここを「イベントには段差が対応するはず」と書くと、正しく調整済みの銘柄を弾く。
        """
        ratios = [("2024-07-29", 1.0, 100.0, 100.0),
                  ("2024-08-05", 1.0, 110.0, 110.0)]
        ok, reason = R.validate(ratios, [("2024-07-30", 10.0)])
        assert ok, reason

    def test_rejects_empty_measurement(self):
        ok, reason = R.validate([], [("2025-03-28", 0.9)])
        assert not ok


# ── 公式が持たないスピンオフの換算（#568）──────────────────────────────────

class TestSpinoffConversion:
    """**換算しないと、補正計画が DB からスピンオフ調整を剥がす向きの係数を出す。**

    E02086 は Yahoo（= DB）だけが権利落ち日 2024-09-27 より前へ (3820 − 1760) / 3820 を掛けており、
    DB 側が正しい。公式値を DB のスケールへ換算すれば比は平らになり、何も補正しない。
    """

    F = (3820.0 - 1760.0) / 3820.0
    ROWS = bars(("2024-09-20", 1890.0, 1.0),
                ("2024-09-27", 1140.0, 1.0),
                ("2026-06-12", 1500.0, 1.0))
    WEEKLY = {"2024-09-20": 1890.0 * F, "2024-09-27": 1140.0, "2026-06-12": 1500.0}

    def test_without_ec_the_spinoff_looks_like_an_unexplained_step(self):
        ratios = R.measured_ratios(self.ROWS, self.WEEKLY)
        assert len(R.find_steps(ratios)) == 1
        ok, reason = R.validate(ratios, [])
        assert not ok
        assert "公式イベントが無い" in reason

    def test_with_ec_the_ratios_are_flat_and_nothing_is_corrected(self):
        ratios = R.measured_ratios(self.ROWS, self.WEEKLY, "E02086")
        assert [r for _, r, _, _ in ratios] == pytest.approx([1.0, 1.0, 1.0])
        assert R.find_steps(ratios) == []
        assert R.validate(ratios, []) == (True, "")
        factors = R.plan_corrections(ratios, list(self.WEEKLY))
        assert all(abs(f - 1.0) <= R.REL_TOL_FLOOR for f in factors.values())

    def test_unregistered_company_is_measured_as_is(self):
        ratios = R.measured_ratios(self.ROWS, self.WEEKLY, "E00001")
        assert ratios == R.measured_ratios(self.ROWS, self.WEEKLY)


# ── エンバーゴ群の判別 ──────────────────────────────────────────────────────

class TestPostWindowAdjustment:
    """**「直せない」を1つの箱に入れると打ち手を取り違える。**

    公式が窓内の `AdjC` を遡及調整済みで返すのに `AdjFactor` の行が無いのは、分割が
    J-Quants 無料プランのエンバーゴ（直近12週）の中で起きているため。普通の分割なら
    Yahoo も遡及調整するので **#466 の現象ではなく既存の Yahoo 経路の担当**であり、
    「公式イベントが無いから説明できない」群（E32779）と混ぜてはいけない。
    """

    def test_detects_a_post_window_split(self):
        """E03178 型: 窓全体で AdjC = C/2 なのに AdjFactor は全日 1.0。"""
        rows = [{"Date": "2026-06-05", "C": 1529.0, "AdjC": 764.5, "AdjFactor": 1.0}]
        assert R.post_window_adjustment(rows) == pytest.approx(0.5)

    def test_returns_none_when_official_needs_no_adjustment(self):
        rows = [{"Date": "2026-06-05", "C": 1529.0, "AdjC": 1529.0, "AdjFactor": 1.0}]
        assert R.post_window_adjustment(rows) is None

    def test_returns_none_on_missing_fields(self):
        assert R.post_window_adjustment([{"Date": "2026-06-05", "C": 100.0}]) is None
        assert R.post_window_adjustment([]) is None

    def test_penny_stock_rounding_is_not_read_as_an_adjustment(self):
        rows = [{"Date": "2026-06-05", "C": 21.0, "AdjC": 21.4, "AdjFactor": 1.0}]
        assert R.post_window_adjustment(rows) is None


# ── 1社ぶんの群分け（書いてよいのは fixed だけ）───────────────────────────────

def jq_bars(*items) -> list:
    """(date, C, AdjC, AdjFactor) から J-Quants 風の行を作る（`post_window_adjustment` は C を読む）。"""
    return [{"Date": d, "C": c, "AdjC": a, "AdjFactor": f} for d, c, a, f in items]


def weekly(*items) -> list:
    """(trade_date, close_last) から `weekly_rows` の形 (trade_date, week_start, close_last) を作る。"""
    return [(d, d, c) for d, c in items]


RIGHTS = 1.0 / (1.0 + 0.5)   # 1株につき1個・目的株式 0.5株の全員行使時の理論係数（#652）


def rights_allotment_case(ex_date: str, dates: tuple) -> tuple:
    """公式だけが権利落ち日より前へ 2/3 を掛け、DB（実約定値）は調整しない社のデータ。

    `dates` は ex_date を挟む4週（前2つ・後2つ）。AdjC は公式らしく小数1桁に丸める。
    """
    closes = (370.0, 365.0, 357.0, 340.0)
    bars_ = [(d, c, round(c * RIGHTS, 1) if d < ex_date else c, 1.0) for d, c in zip(dates, closes)]
    bars_.append((ex_date, 360.0, 360.0, round(RIGHTS, 6)))
    return weekly(*zip(dates, closes)), jq_bars(*sorted(bars_))


class TestWithheldOfficialAdjustments:
    """**公式イベントで説明できる段差でも、DB が正しい側の企業イベントは直さない**（#652）。

    E34165（1447）の新株予約権無償割当は、公式が権利落ち日より前へ理論係数 2/3 を掛けるが、
    DB（= Yahoo = 実約定値）は調整しない。イベントが契約窓に入ると `validate` は**綺麗に通る**ので、
    登録表で止めないと `--apply` が実際の取引に無い +45% の跳ねを週次リターンへ入れる。

    仕組みのテストは架空の社（E99999）を登録して行う。実登録（E34165）は根拠の Issue で
    外されうるので、実登録そのものの確認は1本に閉じ込めてある。
    """

    EC = "E99999"
    DATES = ("2026-08-28", "2026-09-04", "2026-09-18", "2026-12-04")

    @pytest.fixture
    def registered(self, monkeypatch):
        import collector_utils
        monkeypatch.setitem(collector_utils.WITHHELD_OFFICIAL_ADJUSTMENTS, self.EC,
                            (("2026-09-01", "2026-12-31", RIGHTS, "テスト用の登録・#652"),))

    def test_validate_alone_would_accept_the_official_adjustment(self):
        """前提の確認: このデータは validate を通り、登録が無ければ書き込みを誘発する形である。"""
        rows, jq = rights_allotment_case("2026-09-11", self.DATES)
        ratios = R.measured_ratios(jq, {d: c for d, _, c in rows})
        assert R.validate(ratios, R.extract_events(jq)) == (True, "")

    def test_unregistered_company_is_fixed(self):
        """対照: 同じデータの未登録社は fixed になり、権利落ち前の週へ 2/3 を掛ける計画が出る。"""
        rows, jq = rights_allotment_case("2026-09-11", self.DATES)
        bucket, entry, factors = R.judge_company("E00001", "0000", rows, jq)
        assert bucket == "fixed"
        assert entry["would_change"] == 2
        assert factors["2026-08-28"] == pytest.approx(RIGHTS, rel=5e-3)

    def test_registered_event_is_withheld_and_nothing_is_planned(self, registered):
        rows, jq = rights_allotment_case("2026-09-11", self.DATES)
        bucket, entry, factors = R.judge_company(self.EC, "9999", rows, jq)
        assert bucket == "withheld"
        assert factors == {}
        assert [w["date"] for w in entry["withheld"]] == ["2026-09-11"]
        assert entry["withheld"][0]["expected_factor"] == pytest.approx(RIGHTS)
        assert "#652" in entry["withheld"][0]["reason"]

    def test_event_outside_the_window_is_judged_as_before(self, registered):
        """鍵は社ではなく社＋窓（ADR-0053 の案C「社の名簿」にしない）。窓の外の分割は今までどおり直す。"""
        dates = ("2027-02-19", "2027-02-26", "2027-03-05", "2027-03-12")
        rows, jq = rights_allotment_case("2027-03-01", dates)
        bucket, entry, _ = R.judge_company(self.EC, "9999", rows, jq)
        assert bucket == "fixed"
        assert "withheld" not in entry

    def test_window_bounds_are_inclusive(self, registered):
        got = R.withheld_official_events(self.EC, [("2026-08-31", 0.5), ("2026-09-01", 0.5),
                                                   ("2026-12-31", 0.5), ("2027-01-01", 0.5)])
        assert [d for d, *_ in got] == ["2026-09-01", "2026-12-31"]

    def test_unregistered_company_has_no_withheld_events(self):
        assert R.withheld_official_events("E00001", [("2026-09-11", RIGHTS)]) == []

    def test_embargo_phase_stays_embargoed_with_the_registration_shown(self, registered):
        """イベントがまだ契約窓に入っていない時期（`AdjFactor` 行なし・`AdjC/C = 2/3`）は従来どおり
        書かない群のまま。案内を取り違えないよう、登録の理由を entry に付ける。"""
        rows = weekly(("2026-06-12", 400.0), ("2026-06-19", 410.0))
        jq = jq_bars(("2026-06-12", 400.0, 266.7, 1.0), ("2026-06-19", 410.0, 273.3, 1.0))
        bucket, entry, factors = R.judge_company(self.EC, "9999", rows, jq)
        assert bucket == "embargoed"
        assert factors == {}
        assert entry["post_window_factor"] == pytest.approx(RIGHTS, rel=5e-3)
        assert entry["registered"][0]["window"] == ["2026-09-01", "2026-12-31"]

    def test_e34165_is_registered_over_the_observed_schedule(self):
        """実登録の確認（#652）。公式の段差は 9/8〜9/12 の間に現れ、基準日には 9/14 説と 9/24 説がある。
        どの候補日の公式イベントも止まること。登録を外すときはこのテストも外す。"""
        for d in ("2026-09-09", "2026-09-11", "2026-09-18", "2026-09-22"):
            assert R.withheld_official_events("E34165", [(d, RIGHTS)]), d


class TestJudgeCompanyBuckets:
    """切り出し前の `_run` と同じ群分けになること（withheld 以外の回帰）。"""

    def test_flat_ratio_is_clean(self):
        rows = weekly(("2025-01-10", 100.0), ("2025-01-17", 101.0))
        jq = jq_bars(("2025-01-10", 100.0, 100.0, 1.0), ("2025-01-17", 101.0, 101.0, 1.0))
        bucket, entry, _ = R.judge_company("E00001", "0000", rows, jq)
        assert bucket == "clean"
        assert entry["would_change"] == 0

    def test_latest_mismatch_without_post_window_adjustment_is_skipped(self):
        """E32779 型: 公式イベントも窓外調整も無いのに DB が 5/6 ずれている。"""
        rows = weekly(("2025-01-10", 120.0), ("2025-01-17", 120.0))
        jq = jq_bars(("2025-01-10", 100.0, 100.0, 1.0), ("2025-01-17", 100.0, 100.0, 1.0))
        bucket, entry, factors = R.judge_company("E00001", "0000", rows, jq)
        assert bucket == "skipped"
        assert factors == {}
        assert "公式と一致しない" in entry["reason"]

    def test_post_window_adjustment_is_embargoed(self):
        """E03178 型: 窓全体で AdjC = C/2 なのに AdjFactor は全日 1.0。"""
        rows = weekly(("2026-06-05", 1529.0))
        jq = jq_bars(("2026-06-05", 1529.0, 764.5, 1.0))
        bucket, entry, _ = R.judge_company("E00001", "0000", rows, jq)
        assert bucket == "embargoed"
        assert "registered" not in entry


# ── 補正計画 ────────────────────────────────────────────────────────────────

class TestPlanCorrections:
    def test_uses_the_measured_ratio_on_measured_dates(self):
        ratios = [("2025-03-24", 0.909091, 1000.0, 909.1),
                  ("2025-03-31", 1.0, 1000.0, 1000.0)]
        got = R.plan_corrections(ratios, ["2025-03-24", "2025-03-31"])
        assert got["2025-03-24"] == pytest.approx(0.909091)
        assert got["2025-03-31"] == pytest.approx(1.0)

    def test_extends_the_oldest_ratio_backwards(self):
        """窓外（＝公式値が存在しない 68%）は最古の実測比を延長する。"""
        ratios = [("2024-06-07", 0.333333, 3000.0, 1000.0)]
        got = R.plan_corrections(ratios, ["2019-08-02", "2021-01-08", "2024-06-07"])
        assert got["2019-08-02"] == pytest.approx(0.333333)
        assert got["2021-01-08"] == pytest.approx(0.333333)

    def test_unmeasured_day_takes_the_next_measured_ratio(self):
        """比は事象日でのみ変わる階段関数なので、次の測定日の比と等しい。"""
        ratios = [("2025-03-24", 0.9, 100.0, 90.0), ("2025-03-31", 1.0, 100.0, 100.0)]
        got = R.plan_corrections(ratios, ["2025-03-17", "2025-03-28"])
        assert got["2025-03-17"] == pytest.approx(0.9)
        assert got["2025-03-28"] == pytest.approx(1.0)

    def test_no_measurement_means_no_correction(self):
        assert R.plan_corrections([], ["2020-01-06"]) == {}


class TestExtensionSpan:
    def test_counts_weeks_before_the_oldest_measurement(self):
        ratios = [("2024-06-07", 0.5, 100.0, 50.0)]
        assert R.extension_span(ratios, ["2019-08-02", "2024-06-07", "2025-01-06"]) == 1

    def test_zero_when_nothing_measured(self):
        assert R.extension_span([], ["2019-08-02"]) == 0


# ── 実測比の計算 ────────────────────────────────────────────────────────────

class TestMeasuredRatios:
    def test_matches_on_trade_date(self):
        rows = bars(("2024-11-01", 2623.3, 1.0), ("2024-11-08", 2600.0, 1.0))
        got = R.measured_ratios(rows, {"2024-11-01": 7870.0})
        assert len(got) == 1
        assert got[0][0] == "2024-11-01"
        assert got[0][1] == pytest.approx(1.0 / 3.0, rel=1e-4)

    def test_skips_non_positive_and_missing(self):
        rows = bars(("2024-11-01", 100.0, 1.0), ("2024-11-08", None, 1.0))
        assert R.measured_ratios(rows, {"2024-11-01": 0.0, "2024-11-08": 10.0}) == []


# ── 安全側の既定 ────────────────────────────────────────────────────────────

class TestSafetyDefaults:
    def test_stamp_key_is_stable(self):
        """キーを変えると過去の適用が見えなくなり、2度掛け（係数の二乗）が通る。"""
        assert R.STAMP_KEY == "splits_repaired_from_jquants"

    def test_apply_corrections_skips_unit_factors(self):
        """係数 1.0 の週へ UPDATE を撃たない（無駄な書き込みを増やさない）。"""
        executed = []

        class _Db:
            def execute(self, *a, **k):
                executed.append(a)

        n = R.apply_corrections(_Db(), "E00001",
                                [("2025-01-06", "2025-01-06", 100.0)],
                                {"2025-01-06": 1.0})
        assert n == 0 and executed == []
