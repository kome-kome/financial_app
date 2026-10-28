"""イベントスタディの部品（index_event_study.py・#1031・親 #1008）のテスト。

DB・ネット・環境変数に触れない。暦は事前登録文書の表と 1 行ずつ照合し、マッチング・SMD・first stage・
Newey-West・MDE・placebo は合成データで手計算と突き合わせる。盲検は「公表週以降の終値を書き換えても
stage A の出力が変わらない」で固定する。
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_event_study as S  # noqa: E402
import index_events as IE  # noqa: E402

PREREG = ROOT / "docs" / "prereg" / "index-event-2026.md"
W = timedelta(days=7)


# ── 暦 ↔ 事前登録文書 §3.1・§3.2 ─────────────────────────────────────────────────

_CAL_ROW = re.compile(
    r"^\| (\d+) \| (\d{4}-\d{2}-\d{2})（.） \| (\d{4}-\d{2}-\d{2})（.） \| (\d{4}-\d{2}-\d{2}) \|", re.M)


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def _rows(section: str) -> list:
    return [(int(m), date.fromisoformat(d), date.fromisoformat(p), date.fromisoformat(w))
            for m, d, p, w in _CAL_ROW.findall(section)]


class TestCalendarMatchesPrereg:
    text = PREREG.read_text(encoding="utf-8")

    def test_2022_table_row_by_row(self):
        rows = _rows(_section(self.text, "### 3.1", "### 3.2"))
        assert len(rows) == 10
        got = [(pw.m, pw.effective_on, pw.pressure_day, pw.week)
               for pw in S.pressure_calendar(IE.STEPS_2022)]
        assert got == rows

    def test_2026_table_row_by_row(self):
        # 2026 の表でも同じ関数が合う＝休業日の表（4/29 の扱い）を別の 8 行で確かめる
        rows = _rows(_section(self.text, "### 3.2", "### 3.3"))
        assert len(rows) == 8
        got = [(pw.m, pw.effective_on, pw.pressure_day, pw.week)
               for pw in S.pressure_calendar(IE.STEPS_2026)]
        assert got == rows

    def test_match_dates_match_section_3_3(self):
        sec = _section(self.text, "### 3.3", "## 4.")
        assert f"| 2022 前例 | {S.MATCH_DATES_2022['main']} | {S.MATCH_DATES_2022['robust']} |" in sec
        assert f"| 2026 群 | {S.MATCH_DATES_2026['main']} | {S.MATCH_DATES_2026['robust']} |" in sec


class TestCalendar:
    def test_showa_day_moves_the_pressure_day_back(self):
        # 2024-04-30（火）の前営業日は 4/29（月・昭和の日）を飛ばして 4/26（金）
        assert S.previous_business_day(date(2024, 4, 30)) == date(2024, 4, 26)

    def test_golden_week_2019(self):
        # 2019-04-29（昭和の日）と 4/30（国民の休日）が続けて休み＝4 月の最終営業日は 4/26（金）
        assert S.last_business_day(2019, 4) == date(2019, 4, 26)

    def test_year_end_is_closed(self):
        # 東証は 12/31 が休み＝2019 年 12 月の最終営業日は 12/30（月）、前営業日は 12/27（金）
        assert S.last_business_day(2019, 12) == date(2019, 12, 30)
        assert S.previous_business_day(date(2019, 12, 30)) == date(2019, 12, 27)

    def test_placebo_weeks(self):
        """事前期間の各月の月末の週（#1033）。四半期末だけでは 6 個で下限の 15 個に届かない。"""
        weeks = S.placebo_pressure_weeks()
        assert len(weeks) == 18 >= S.PLACEBO_MIN_WEEKS
        assert weeks[0] == date(2019, 8, 26)        # 8/30（金）の前営業日 8/29（木）の週
        assert date(2019, 12, 23) in weeks          # 年末: 12/30（月）の前営業日 12/27（金）の週
        assert date(2020, 3, 30) in weeks           # 3/31（火）の前営業日 3/30（月）の週
        assert weeks[-1] == date(2021, 1, 25)       # 1/29（金）の前営業日 1/28（木）の週
        assert all(w.weekday() == 0 for w in weeks)
        quarterly = S.placebo_pressure_weeks(months=S.REVIEW_MONTHS)
        assert len(quarterly) == 6 < S.PLACEBO_MIN_WEEKS

    def test_placebo_windows_sit_inside_the_pre_period(self):
        lo, hi = S.PRE_PERIOD
        assert all(lo < w - W and w <= hi for w in S.placebo_pressure_weeks())

    def test_pre_period_ends_before_the_matching_window(self):
        """事前期間はマッチ日の 52 週リターンの窓（基準の足は最大 56 週前）に入らない（#1033）。

        窓の中では処置と対照のリターンをそろえているので、AR̄ のばらつきが作りとして小さくなる。
        マッチ日の足はその週か前の週（週の最後の取引日がマッチ日より後なら前の週の足）。
        """
        bar_week = S.iso_monday(S.MATCH_DATES_2022["main"]) - W
        first_matched_return = bar_week - W * (52 + S.RET_BASE_SLACK_WEEKS) + W
        assert S.PRE_PERIOD[1] < first_matched_return
        assert S.PRE_PERIOD[0] == date(2019, 7, 29)          # 週次株価の最古の週

    @pytest.mark.parametrize("d", [date(2018, 4, 30), date(2022, 3, 31), date(2022, 4, 21), date(2029, 1, 31)])
    def test_outside_the_table_raises(self, d):
        with pytest.raises(S.CalendarError):
            S.is_business_day(d)

    def test_blind_from_is_the_announcement_week(self):
        assert S.blind_from(date(2022, 10, 7)) == date(2022, 10, 3)


def test_sector17_is_a_33_to_17_map():
    assert len(S.SECTOR17) == 33
    assert len(set(S.SECTOR17.values())) == 17


# ── 合成の表 ─────────────────────────────────────────────────────────────────────

def _vars(rows: dict) -> pd.DataFrame:
    """{社: (logcap, log_turnover_ratio, log_pbr, ret_52w, sector17)} → 変数の表。"""
    return pd.DataFrame.from_dict(rows, orient="index", columns=[*S.MATCH_VARS, "sector17"])


class TestMatching:
    # 処置 T1・T2 と対照 C1〜C8。標準偏差は 8+2 社のプールで決まる
    BASE = {
        "T1": (10.0, -3.0, 0.0, 0.0, "A"),
        "T2": (10.1, -3.0, 0.0, 0.0, "A"),
        "C1": (10.0, -3.0, 0.0, 0.01, "A"),
        "C2": (10.05, -3.0, 0.0, 0.0, "A"),
        "C3": (10.0, -3.0, 0.0, 0.0, "B"),          # 業種違い（同じ値でも使わない）
        "C4": (12.0, -1.0, 1.0, 0.5, "A"),          # caliper の外
        "C5": (8.0, -5.0, -1.0, -0.5, "A"),
        "C6": (11.0, -2.5, 0.5, 0.2, "B"),
        "C7": (9.0, -4.0, -0.5, -0.2, "B"),
        "C8": (10.0, -3.0, 0.0, 0.0, "A"),
    }

    def test_sector_exact_caliper_and_replacement(self):
        v = _vars(self.BASE)
        m = S.match_controls(v, ["T1", "T2"], [f"C{i}" for i in range(1, 9)])
        assert set(m.pairs["T1"]) == {"C1", "C2", "C8"}
        assert set(m.pairs["T2"]) == {"C1", "C2", "C8"}     # 復元あり: 同じ対照を両方が使う
        sd = m.sd
        for t, cs in m.pairs.items():
            for c in cs:
                assert v.at[c, "sector17"] == v.at[t, "sector17"]
                for var in S.MATCH_VARS:
                    assert abs(v.at[c, var] - v.at[t, var]) <= S.CALIPER_SD * sd[var]

    def test_ordered_by_distance_and_ties_by_code(self):
        v = _vars(self.BASE)
        m = S.match_controls(v, ["T1"], [f"C{i}" for i in range(1, 9)])
        # C8 は T1 と同じ値＝距離 0。C2 は logcap だけ、C1 は ret_52w だけ離れる
        assert m.pairs["T1"][0] == "C8"
        twin = {**self.BASE, "C9": self.BASE["C8"]}
        m2 = S.match_controls(_vars(twin), ["T1"], [f"C{i}" for i in range(1, 10)])
        assert m2.pairs["T1"][:2] == ("C8", "C9")

    def test_at_most_k(self):
        rows = {"T1": (10.0, -3.0, 0.0, 0.0, "A")}
        rows.update({f"C{i:02d}": (10.0 + i * 1e-4, -3.0, 0.0, 0.0, "A") for i in range(1, 9)})
        rows.update({"D1": (11.0, -4.0, 0.5, 0.1, "A"), "D2": (12.0, -3.5, -0.5, 0.3, "A"),
                     "D3": (9.0, -5.0, 1.0, -0.2, "A"), "D4": (13.0, -2.0, 0.2, -0.4, "A"),
                     "D5": (8.0, -6.0, -1.0, 0.5, "A")})
        m = S.match_controls(_vars(rows), ["T1"], [k for k in rows if k != "T1"])
        assert m.pairs["T1"] == ("C01", "C02", "C03", "C04", "C05")

    def test_unmatched_reasons(self):
        rows = {**self.BASE,
                "T3": (math.nan, -3.0, 0.0, 0.0, "A"),
                "T4": (10.0, -3.0, 0.0, 0.0, None),
                "T5": (10.0, -3.0, 0.0, 0.0, "Z"),
                "T6": (20.0, 5.0, 3.0, 2.0, "A")}
        m = S.match_controls(_vars(rows), ["T1", "T3", "T4", "T5", "T6", "T7"],
                             [f"C{i}" for i in range(1, 9)])
        assert m.unmatched == {"T3": "missing_variables", "T4": "unknown_sector",
                               "T5": "no_control_in_caliper", "T6": "no_control_in_caliper",
                               "T7": "no_data"}
        assert "T1" in m.pairs

    def test_treated_are_never_controls(self):
        v = _vars(self.BASE)
        # T2 は T1 に近いが、処置社なので対照の候補に渡しても使わない
        m = S.match_controls(v, ["T1", "T2"], ["T2"] + [f"C{i}" for i in range(1, 9)])
        assert all("T2" not in cs for cs in m.pairs.values())
        assert m.n_pool_controls == 8

    def test_sd_and_covariance_come_from_the_pool(self):
        v = _vars(self.BASE)
        m = S.match_controls(v, ["T1", "T2"], [f"C{i}" for i in range(1, 9)])
        pool = v.loc[["T1", "T2"] + [f"C{i}" for i in range(1, 9)], list(S.MATCH_VARS)]
        for var in S.MATCH_VARS:
            assert m.sd[var] == pytest.approx(pool[var].std(ddof=1))
        assert (m.n_pool_treated, m.n_pool_controls) == (2, 8)


class TestBalance:
    def test_smd_matches_hand_calculation(self):
        v = _vars({"T1": (1.0, 0.0, 0.0, 0.0, "A"), "T2": (3.0, 0.0, 0.0, 0.0, "A"),
                   "C1": (0.0, 0.0, 0.0, 0.0, "A"), "C2": (2.0, 0.0, 0.0, 0.0, "A"),
                   "C3": (4.0, 0.0, 0.0, 0.0, "A")})
        m = S.Matching(pairs={"T1": ("C1", "C2"), "T2": ("C3",)}, unmatched={},
                       sd={k: 2.0 for k in S.MATCH_VARS}, n_pool_treated=2, n_pool_controls=3)
        smd = S.balance(v, m)
        # 処置の平均 2.0、対照の平均 = mean(mean(0,2), 4) = 2.5 → (2.0 − 2.5) / 2.0
        assert smd["logcap"] == pytest.approx(-0.25)
        assert smd["log_pbr"] == pytest.approx(0.0)
        assert S.balance_ok(smd) is False
        assert S.balance_ok({k: 0.05 for k in S.MATCH_VARS}) is True
        assert S.balance_ok({**{k: 0.0 for k in S.MATCH_VARS}, "logcap": None}) is None


# ── first stage ───────────────────────────────────────────────────────────────

def _calendar(weeks):
    return [S.PressureWeek(i + 1, w, w, w, "rule") for i, w in enumerate(weeks)]


class TestFirstStage:
    WEEKS = [date(2022, 1, 3) + W * k for k in range(40)]
    PW = date(2022, 1, 3) + W * 30

    def _turnover(self, spikes: dict) -> pd.DataFrame:
        data = {ec: {w: 100.0 for w in self.WEEKS} for ec in ("T1", "T2", "T3", "C1", "C2")}
        for ec, mult in spikes.items():
            data[ec][self.PW] = 100.0 * mult
        return S.weekly_frame(data)

    def test_t_matches_hand_calculation(self):
        to = self._turnover({"T1": math.e ** 2, "T2": math.e ** 3, "T3": math.e ** 1, "C1": math.e})
        pairs = {"T1": ("C1", "C2"), "T2": ("C2",), "T3": ("C1",)}
        rows = S.first_stage(to, pairs, _calendar([self.PW]))
        # x = 2 − mean(1, 0), 3 − 0, 1 − 1 = 1.5, 3, 0
        x = [1.5, 3.0, 0.0]
        mean = sum(x) / 3
        sd = float(np.std(x, ddof=1))
        assert rows[0]["n"] == 3
        assert rows[0]["mean"] == pytest.approx(mean)
        assert rows[0]["t"] == pytest.approx(mean / (sd / math.sqrt(3)))

    def test_below_three_fails(self):
        to = self._turnover({"T1": 1.1, "T2": 1.2, "T3": 0.9})
        rows = S.first_stage(to, {"T1": ("C1",), "T2": ("C2",), "T3": ("C1",)}, _calendar([self.PW]))
        assert rows[0]["t"] < S.FIRST_STAGE_T_MIN
        assert S.first_stage_ok(rows) is False

    def test_no_t_is_a_failure(self):
        to = self._turnover({})
        rows = S.first_stage(to, {"T1": ("C1",)}, _calendar([self.PW]))
        assert rows[0]["t"] is None
        assert S.first_stage_ok(rows) is False

    def test_short_base_is_left_out(self):
        data = {"T1": {w: 100.0 for w in self.WEEKS[-10:]}, "C1": {w: 100.0 for w in self.WEEKS}}
        q = S.abnormal_turnover(S.weekly_frame(data), self.PW)
        assert "T1" not in q.index and "C1" in q.index


# ── 異常リターン・Newey-West・MDE ───────────────────────────────────────────────────

def test_abnormal_return_series_hand_calculation():
    idx = [date(2015, 1, 5) + W * k for k in range(3)]
    r = pd.DataFrame({"T1": [0.1, 0.2, np.nan], "T2": [0.0, np.nan, 0.3],
                      "C1": [0.05, 0.1, 0.1], "C2": [np.nan, 0.3, 0.1]}, index=idx)
    ar = S.abnormal_return_series(r, {"T1": ("C1", "C2"), "T2": ("C2",)})
    # 週0: T1 0.1−0.05=0.05、T2 は対照が無い → 0.05 / 週1: T1 0.2−0.2=0 / 週2: T2 0.3−0.1=0.2
    assert ar.tolist() == pytest.approx([0.05, 0.0, 0.2])


class TestNeweyWest:
    def test_autocovariances_do_not_demean(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert S.nw_autocovariances(s, lag=2) == pytest.approx([55 / 5, 40 / 5, 26 / 5])

    def test_missing_weeks_are_left_out_of_the_products(self):
        s = pd.Series([1.0, np.nan, 3.0])
        assert S.nw_autocovariances(s, lag=1) == pytest.approx([5.0, 0.0])

    def test_quadratic_form_matches_the_bartlett_sum(self):
        g = [2.0, 1.0, 0.5, 0.25, 0.125]
        w0 = date(2015, 1, 5)
        assert S.nw_variance({w0: 1.0}, g) == pytest.approx(2.0)
        # 隣り合う 2 週: 2γ0 + 2·(1 − 1/5)·γ1
        assert S.nw_variance({w0: 1.0, w0 + W: 1.0}, g) == pytest.approx(4.0 + 1.6)
        # lag を超えて離れた週は掛け合わない
        assert S.nw_variance({w0: 1.0, w0 + W * 5: 1.0}, g) == pytest.approx(4.0)

    def test_full_window_equals_the_long_run_variance_formula(self):
        rng = np.random.default_rng(3)
        x = pd.Series(rng.normal(size=60))
        g = S.nw_autocovariances(x)
        n = len(x)
        weeks = {date(2015, 1, 5) + W * k: 1.0 for k in range(n)}
        expect = n * g[0] + 2 * sum((n - k) * (1 - k / 5) * g[k] for k in range(1, 5))
        assert S.nw_variance(weeks, g) == pytest.approx(expect)

    def test_constant_series_is_not_demeaned(self):
        # 平均を引くと 0 になる系列でも、平均 0 の帰無の下の分散は c² になる（§5.2）
        g = S.nw_autocovariances(pd.Series([0.5] * 10))
        assert S.nw_variance({date(2015, 1, 5): 1.0}, g) == pytest.approx(0.25)


class TestPower:
    def test_mde_formula(self):
        rng = np.random.default_rng(1)
        idx = [date(2014, 1, 6) + W * k for k in range(200)]
        ar = pd.Series(rng.normal(0, 0.01, size=200), index=idx)
        weeks = [pw.week for pw in S.pressure_calendar(IE.STEPS_2022)]
        p = S.power_summary(ar, weeks)
        g = S.nw_autocovariances(ar)
        se = math.sqrt(S.nw_variance(S.delta_hat_weights(weeks), g))
        assert p["se_delta_hat"] == pytest.approx(se)
        assert p["mde"] == pytest.approx((1.6448536 + 0.8416212) * se, rel=1e-6)
        assert p["sigma_weekly"] == pytest.approx(math.sqrt(g[0]))
        assert p["n_weeks"] == 200

    def test_delta_hat_weights(self):
        weeks = [pw.week for pw in S.pressure_calendar(IE.STEPS_2022)]
        c = S.delta_hat_weights(weeks)
        assert len(c) == 60 and all(v == pytest.approx(0.4) for v in c.values())
        assert sum(S.impact_weights(weeks).values()) == pytest.approx(2.0)

    def test_no_pre_period_data(self):
        p = S.power_summary(pd.Series(dtype=float), [date(2022, 10, 24)])
        assert p["mde"] is None and p["n_weeks"] == 0 and "reason" in p


# ── placebo ───────────────────────────────────────────────────────────────────

def _pre_series(rng, drift: float = 0.0) -> pd.Series:
    lo, hi = S.PRE_PERIOD
    idx = [S.iso_monday(lo) + W * k for k in range(((hi - S.iso_monday(lo)).days // 7) + 1)]
    return pd.Series(rng.normal(drift, 0.01, size=len(idx)), index=idx)


class TestPlacebo:
    def test_null_false_positive_rate_is_near_nominal(self):
        # 系列ごとの誤検出率は 18 個の擬似 CAR の偶然に引きずられるので、多数の系列で平均する
        rng = np.random.default_rng(0)
        weeks = S.placebo_pressure_weeks()
        rates = []
        for _ in range(300):
            ar = _pre_series(rng)
            res = S.placebo_test(ar, weeks, S.nw_autocovariances(ar), draws=100,
                                 seed=int(rng.integers(1 << 30)))
            rates.append(res["fpr"])
        assert abs(np.mean(rates) - 0.05) <= 0.05
        assert np.mean(rates) > 0.01                # 常に 0 を返す壊れ方を通さない

    def test_drift_switches_to_the_empirical_p(self):
        rng = np.random.default_rng(5)
        ar = _pre_series(rng, drift=-0.01)
        res = S.placebo_test(ar, S.placebo_pressure_weeks(), S.nw_autocovariances(ar), draws=500)
        assert res["n_usable"] == 18
        assert res["fpr"] > 0.10 and res["switch_to_empirical_p"] is True

    def test_too_few_usable_weeks(self):
        rng = np.random.default_rng(2)
        ar = _pre_series(rng)
        ar[[w >= date(2020, 7, 1) for w in ar.index]] = np.nan     # 使えるのは 2019-08〜2020-06 の 11 個
        res = S.placebo_test(ar, S.placebo_pressure_weeks(), S.nw_autocovariances(ar))
        assert res["n_usable"] == 11
        assert res["fpr"] is None and res["switch_to_empirical_p"] is False

    def test_deterministic_with_the_seed(self):
        rng = np.random.default_rng(4)
        ar = _pre_series(rng)
        g = S.nw_autocovariances(ar)
        a = S.placebo_test(ar, S.placebo_pressure_weeks(), g, draws=200)
        b = S.placebo_test(ar, S.placebo_pressure_weeks(), g, draws=200)
        assert a == b


# ── R0 ───────────────────────────────────────────────────────────────────────

class TestR0:
    KW = dict(counts={"transition": 493, "released": 43, "continuing": 439}, d1=11,
              resolution={"topix-2022-10": 492 / 493}, min_resolution=0.98)

    def test_pending_while_d2_is_unregistered(self):
        r = S.r0_summary(d2=None, first_stage=True, balance=True, **self.KW)
        assert r["status"] == "pending"
        assert r["items"]["counts_2022"]["pass"] is True
        assert r["items"]["excluded_2025_01"]["value"] is None

    def test_pass(self):
        r = S.r0_summary(d2=16, first_stage=True, balance=True, **self.KW)
        assert r["status"] == "pass"
        assert r["items"]["excluded_2025_01"]["value"] == 423

    @pytest.mark.parametrize("fs,bal", [(False, True), (True, False)])
    def test_fail(self, fs, bal):
        assert S.r0_summary(d2=None, first_stage=fs, balance=bal, **self.KW)["status"] == "fail"

    def test_low_resolution_fails(self):
        kw = {**self.KW, "resolution": {"topix-2022-10": 0.97}}
        assert S.r0_summary(d2=0, first_stage=True, balance=True, **kw)["status"] == "fail"


# ── 対照 ─────────────────────────────────────────────────────────────────────

def test_c1_and_c2_controls():
    from collections import namedtuple

    Row = namedtuple("Row", "edinet_code market_segment size_class")
    rows = [Row("E1", "スタンダード（内国株式）", "-"),
            Row("E2", "グロース（内国株式）", "-"),
            Row("E3", "プライム（内国株式）", "TOPIX Small 2"),    # TOPIX
            Row("E4", "ETF・ETN", "-"),                          # 内国株式でない
            Row("E5", "スタンダード（内国株式）", "-"),             # 台帳に載っている
            Row(None, "スタンダード（内国株式）", "-")]
    assert S.c1_controls(rows, {"E5": ()}) == {"E1", "E2"}
    assert S.c2_controls({"E3", "E6"}, {"E6"}) == {"E3"}


# ── stage A（合成）──────────────────────────────────────────────────────────────

def _synthetic(seed: int = 0, spike: float = 4.0):
    """2019-07〜2025 の週次（本物と同じ始まり）。処置 6 社・対照 18 社（処置ごとに値の近い 3 社）。
    圧力週に処置の売買代金の山。"""
    rng = np.random.default_rng(seed)
    weeks = [date(2019, 7, 29) + W * k for k in range(((date(2025, 2, 24) - date(2019, 7, 29)).days // 7) + 1)]
    calendar = S.pressure_calendar(IE.STEPS_2022)
    pressure = {pw.week for pw in calendar}
    closes, turnover, firms = {}, {}, {"main": {}, "robust": {}}
    treated = [f"T{i}" for i in range(6)]
    controls = []
    for i, t in enumerate(treated):
        path = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, size=len(weeks))))
        group = [t] + [f"C{i}{j}" for j in range(3)]
        controls += group[1:]
        for j, ec in enumerate(group):
            p = path * np.exp(np.cumsum(rng.normal(0, 0.001, size=len(weeks))))
            closes[ec] = dict(zip(weeks, p.tolist()))
            base = 1e6 * (1 + 0.1 * i) * (1 + 0.001 * j)
            turnover[ec] = {w: base * (spike if (ec == t and w in pressure) else 1.0)
                            * float(np.exp(rng.normal(0, 0.05))) for w in weeks}
            for role, d in S.MATCH_DATES_2022.items():
                bar = max(w for w in weeks if w + timedelta(days=4) <= d) + timedelta(days=4)
                firms[role][ec] = S.Firm(bar=bar, price=float(closes[ec][S.iso_monday(bar)]),
                                         shares=1e6 * (1 + 0.1 * i) * (1 + 0.0005 * j),
                                         bps=50.0 * (1 + 0.1 * i), industry33="情報・通信業")
    return dict(
        closes=S.weekly_frame(closes), turnover=S.weekly_frame(turnover), firms_by_role=firms,
        treated=set(treated), subgroups={"released": {"T0"}, "continuing": set(treated[1:])},
        controls={"C1": set(controls), "C2": set(controls[:9])}, calendar=calendar,
        cutoff=S.blind_from(date(2022, 10, 7)),
        r0_inputs=dict(counts={"transition": 493, "released": 43, "continuing": 439}, d1=11, d2=None,
                       resolution={"topix-2022-10": 0.998}, min_resolution=0.98))


class TestStageA:
    @pytest.fixture(scope="class")
    def result(self):
        return S.stage_a(**_synthetic())

    def test_summary_shape(self, result):
        summary, pairs = result
        main = summary["matching"]["C1@2022-03-31"]
        assert main["main"] is True and main["n_matched"] == 6 and main["balance_ok"] is True
        assert summary["first_stage"]["pass"] is True
        assert summary["power"]["mde"] is not None
        assert summary["power"]["period"] == ["2019-07-29", "2021-02-21"]
        assert summary["placebo"]["n_usable"] == 18
        assert summary["r0"]["status"] == "pending"               # d2 が未登録
        assert set(pairs) == {"C1@2022-03-31", "C1@2022-09-30", "C2@2022-03-31", "C2@2022-09-30"}

    def test_pairs_stay_within_each_treated_firms_twins(self, result):
        _, pairs = result
        for t, cs in pairs["C1@2022-03-31"].items():
            assert all(c.startswith("C" + t[1:]) for c in cs)

    def test_summary_carries_no_codes(self, result):
        summary, _ = result
        text = json.dumps(summary, ensure_ascii=False, default=str)
        assert not re.search(r'"(T\d|C\d\d)"', text)

    def test_blind_to_event_window_returns(self, result):
        """公表週以降の終値をでたらめに書き換えても、stage A の出力は 1 文字も変わらない。"""
        inputs = _synthetic()
        closes = inputs["closes"].copy()
        after = [w >= inputs["cutoff"] for w in closes.index]
        closes.loc[after] = np.random.default_rng(99).uniform(1, 1e4, size=closes.loc[after].shape)
        inputs["closes"] = closes
        poisoned = S.stage_a(**inputs)
        assert json.dumps(poisoned, default=str) == json.dumps(result, default=str)

    def test_first_stage_fails_without_the_spike(self):
        summary, _ = S.stage_a(**_synthetic(spike=1.0))
        assert summary["first_stage"]["pass"] is False
        assert summary["r0"]["status"] == "fail"

    def test_match_specs_is_what_stage_a_runs(self, result):
        """前向き監視は stage A と同じ `match_specs` を呼ぶ。マッチ日を 2026 にしても変わるのは仕様の名前だけ。"""
        inputs = _synthetic()
        closes = S.blind(inputs["closes"], inputs["cutoff"])
        variables = {role: S.match_variables(f, closes, inputs["turnover"])
                     for role, f in inputs["firms_by_role"].items()}
        args = (variables, inputs["treated"], inputs["controls"], inputs["subgroups"])
        specs, pairs, _ = S.match_specs(*args, S.MATCH_DATES_2022)
        summary, stage_pairs = result
        assert pairs == stage_pairs and specs == summary["matching"]
        specs26, pairs26, _ = S.match_specs(*args, S.MATCH_DATES_2026)
        assert list(pairs26) == ["C1@2026-03-31", "C1@2026-09-30", "C2@2026-03-31", "C2@2026-09-30"]
        assert list(pairs26.values()) == list(pairs.values())
        assert specs26["C1@2026-03-31"]["match_date"] == "2026-03-31" and specs26["C1@2026-03-31"]["main"] is True


# ── 前向き監視（2026 群・§6.3・#1061）──────────────────────────────────────────────

CAL26 = S.pressure_calendar(IE.STEPS_2026)


def _monitor_synthetic(effect: float = 0.0, end: date = date(2027, 5, 31), seed: int = 0) -> dict:
    """2026-03-30〜`end` の週次。処置 8 社（対照 2 社ずつの双子）。処置の圧力窓 [w_m − 1, w_m] に `effect` を仕込む。

    既定の `end` では 1〜3 回目が到来し（w_3 + 3 = 2027-05-17）、4 回目以降は未到来。
    """
    rng = np.random.default_rng(seed)
    start = date(2026, 3, 30)
    weeks = [start + W * k for k in range((end - start).days // 7 + 1)]
    pressure = {w for pw in CAL26 for w in (pw.week - W, pw.week)}
    closes, pairs, segments = {}, {}, {}
    for i in range(8):
        market = rng.normal(0, 0.02, size=len(weeks))
        group = [f"T{i}", f"C{i}a", f"C{i}b"]
        pairs[group[0]] = group[1:]
        segments[group[0]] = "プライム（内国株式）" if i < 4 else "グロース（内国株式）"
        for ec in group:
            r = market + rng.normal(0, 0.001, size=len(weeks))
            if ec == group[0]:
                r = r + np.array([effect if w in pressure else 0.0 for w in weeks])
            closes[ec] = dict(zip(weeks, (100 * np.exp(np.cumsum(r))).tolist()))
    return dict(closes=S.weekly_frame(closes), pairs=pairs, calendar=CAL26,
                announce_week=S.announcement_week(date(2026, 10, 7)), segments=segments, confirmatory=True)


class TestMonitor:
    def test_announcement_week_matches_section_3_2(self):
        assert S.announcement_week(date(2026, 10, 7)) == date(2026, 10, 5)

    def test_planted_pressure_is_rejected(self):
        seq = S.monitor(**_monitor_synthetic(effect=-0.01))["sequential"]
        looks = seq["looks"]
        assert [r["status"] for r in looks] == ["tested"] * 3 + ["not_yet"] * 5
        assert looks[0]["cum_car"] == pytest.approx(-0.02, abs=0.002)
        assert looks[2]["cum_car"] == pytest.approx(-0.06, abs=0.004)
        # 1 回目は境界 5.421 にほぼ届かない: 平均を引かない Ω は事象週の値そのものを含み、解析期間が 32 週しか
        # ないので、雑音の無い極限でも |Z_1| = 2 * sqrt(32 / 5.6) = 4.78。2 回目（45 週）からは届く
        assert looks[0]["rejected"] is False and abs(looks[0]["z"]) < looks[0]["z_bound"]
        assert looks[1]["rejected"] is True and looks[1]["z"] <= -looks[1]["z_bound"]
        assert seq["first_rejection"] == 2

    def test_null_is_not_rejected(self):
        seq = S.monitor(**_monitor_synthetic(effect=0.0))["sequential"]
        assert [r["rejected"] for r in seq["looks"][:3]] == [False] * 3
        assert seq["first_rejection"] is None

    def test_information_fraction_and_bounds_ignore_the_dates(self):
        """実施が遅れても t_k = k/8 と z_k は回の番号だけで決まる（§6.3）。"""
        from model_stats import ld_obf_boundaries, obf_spending

        delayed = tuple(pw._replace(effective_on=pw.effective_on + 3 * W, pressure_day=pw.pressure_day + 3 * W,
                                    week=pw.week + 3 * W) for pw in CAL26)
        inputs = _monitor_synthetic()
        got = {}
        for name, cal in (("planned", CAL26), ("delayed", delayed)):
            looks = S.monitor(**{**inputs, "calendar": cal})["sequential"]["looks"]
            got[name] = [(r["k"], r["t"], r["z_bound"], r["cum_alpha"]) for r in looks]
        want = [(k, k / 8, z, obf_spending(k / 8, 0.05)) for k, z in enumerate(ld_obf_boundaries(8, 0.05), 1)]
        assert got["planned"] == got["delayed"] == want

    def test_looks_without_the_w_plus_2_close_are_not_yet(self):
        """データが w_2 + 2 の週で終わると、その週の終値はまだそろっていない＝2 回目は未到来。"""
        inputs = _monitor_synthetic(effect=-0.01)
        w2 = CAL26[1].week
        short = inputs["closes"][[w <= w2 + 2 * W for w in inputs["closes"].index]]
        summary = S.monitor(**{**inputs, "closes": short})
        looks = summary["sequential"]["looks"]
        assert [r["status"] for r in looks] == ["tested"] + ["not_yet"] * 7
        assert all(r["z"] is None and r["car"] is None for r in looks[1:])
        assert list(summary["descriptive"]["pressure_car"]) == [1]
        # 後ろの週が増えても、1 回目の Z は変わらない（解析期間の終わりは w_1 + 2 に固定）
        full = S.monitor(**inputs)["sequential"]["looks"][0]
        assert looks[0] == full

    def test_wrong_number_of_looks_raises(self):
        with pytest.raises(ValueError):
            S.monitor(**{**_monitor_synthetic(), "calendar": CAL26[:7]})

    def test_descriptive_by_segment_and_no_codes(self):
        summary = S.monitor(**_monitor_synthetic(effect=-0.01))
        seg = summary["descriptive"]["by_segment"]
        assert {k: v["n_treated"] for k, v in seg.items()} == {"growth": 4, "prime": 4}
        assert list(seg["prime"]["pressure_car"]) == [1, 2, 3]
        assert summary["n_treated_matched"] == 8 and summary["n_treated_with_ar"] == 8
        assert not re.search(r'"(T\d|C\d[ab])"', json.dumps(summary, default=str))

    def test_new_additions_are_descriptive_only(self):
        add_cal = S.pressure_calendar(IE.SCHEDULES[("topix-2026-10", "new_add")].steps)
        cohort = S.MONITOR_COHORTS["topix-2026-10-add"]
        summary = S.monitor(**{**_monitor_synthetic(), "calendar": add_cal, "confirmatory": cohort.confirmatory,
                               "anticipation_from": cohort.anticipation_from})
        assert summary["sequential"] is None
        a = summary["descriptive"]["anticipation"]
        assert a["window"] == ["2026-08-31", "2026-10-12"] and a["car"] is not None
        assert list(summary["descriptive"]["pressure_car"]) == [1]


def _looks(*rows):
    """(status, car) の並びから sequential_looks の行の形を作る。"""
    return [{"k": k, "status": st, "car": c, "pressure_week": f"2026-1{k}-01"} for k, (st, c) in enumerate(rows, 1)]


class TestBandsAndPressure:
    """予測帯・帯の外の連続・起票文・売り圧力（§6.3・#1062）。"""
    CI = [-0.02, -0.004]

    def test_s_equal_to_one_reproduces_the_2022_ci(self):
        assert S.prediction_bands(self.CI, {"one": 1.0})["one"] == {"s": 1.0, "lo": -0.02, "hi": -0.004}

    def test_main_band_scales_by_the_step_ratio(self):
        bands = S.prediction_bands(self.CI, S.band_scales(0.3, 0.2))
        assert bands["step"]["lo"] == pytest.approx(-0.025) and bands["step"]["hi"] == pytest.approx(-0.005)
        assert bands["sqrt_step"]["s"] == pytest.approx(1.25 ** 0.5)
        assert bands["pressure"]["s"] == pytest.approx(1.5)

    def test_missing_ci_or_pressure_leaves_the_band_unavailable(self):
        assert S.prediction_bands(None, {"step": 1.25})["step"]["lo"] is None
        assert S.prediction_bands(self.CI, S.band_scales(0.3, None))["pressure"]["lo"] is None

    def _alert(self, *rows):
        looks = _looks(*rows)
        S.mark_band(looks, {"lo": -0.025, "hi": -0.005})
        return looks, S.band_alert(looks)

    def test_two_consecutive_outside_raise_the_alert_on_either_side(self):
        looks, alert = self._alert(("tested", -0.01), ("tested", -0.05), ("tested", 0.01), ("not_yet", None))
        assert alert == {"k": [2, 3], "side": ["below", "above"]}
        draft = S.issue_draft(alert, looks, {"step": {"s": 1.25, "lo": -0.025, "hi": -0.005}}, "topix-2026-10")
        assert "R8" in draft and "| 2 |" in draft and "| 3 |" in draft

    def test_unavailable_neither_counts_nor_breaks_the_run(self):
        _, alert = self._alert(("tested", -0.05), ("unavailable", None), ("tested", -0.06))
        assert alert["k"] == [1, 3]

    def test_a_look_inside_the_band_breaks_the_run(self):
        _, alert = self._alert(("tested", -0.05), ("tested", -0.01), ("tested", -0.06), ("not_yet", None))
        assert alert is None
        assert S.issue_draft(None, [], {}, "x") is None

    def test_no_alert_without_a_band(self):
        looks = _looks(("tested", -0.05), ("tested", -0.06))
        S.mark_band(looks, {"lo": None, "hi": None})
        assert [r["outside"] for r in looks] == [None, None] and S.band_alert(looks) is None

    def test_pressure_is_market_cap_times_step_over_median_turnover(self):
        base_day = date(2026, 3, 31)
        week = S.iso_monday(base_day)
        weeks = {week - W * k: 100.0 + k for k in range(1, 27)}          # 中央値 113.5
        short = {week - W * k: 50.0 for k in range(1, 10)}               # 足 9 本＜13 本
        turnover = S.weekly_frame({"A": weeks, "B": short, "C": weeks})
        firms = {"A": S.Firm(base_day, 10.0, 1000.0, None, None), "B": S.Firm(base_day, 10.0, 1000.0, None, None),
                 "C": S.Firm(base_day, 10.0, None, None, None)}
        got = S.pressure_scores(firms, turnover, base_day, 0.125)
        assert got == {"A": pytest.approx(10.0 * 1000.0 * 0.125 / 113.5)}

    def test_terciles_split_by_rank(self):
        scores = {f"E{i}": float(i) for i in range(9)}
        t = S.pressure_terciles(scores)
        assert [t[f"E{i}"] for i in range(9)] == [1, 1, 1, 2, 2, 2, 3, 3, 3]

    def test_monitor_reports_bands_terciles_and_draft(self):
        inputs = _monitor_synthetic(effect=-0.01)
        terciles = {f"T{i}": 1 + i % 3 for i in range(8)}
        # 処置の圧力窓 CAR は約 −0.02。主の帯をそれより上に置くと、到来した 3 回とも帯の下に出る
        summary = S.monitor(**inputs, i_ci=[-0.004, 0.0], terciles=terciles,
                            pressure_medians={"2026": 0.3, "2022": 0.2}, cohort="topix-2026-10")
        d = summary["descriptive"]
        assert [r["outside"] for r in summary["sequential"]["looks"][:4]] == [True, True, True, None]
        assert d["band_alert"] == {"k": [1, 2], "side": ["below", "below"]}
        assert d["issue_draft"].startswith("## [topix-2026-10]")
        assert set(d["pressure_tercile_car"]) == {"t1", "t2", "t3"}
        assert d["pressure"] == {"median_2026": 0.3, "median_2022": 0.2, "n_scored": 8}
        assert not re.search(r'"(T\d|C\d[ab])"', json.dumps(summary, default=str))


# ── M 族（§6.2・#1009）────────────────────────────────────────────────────────

CAL = S.pressure_calendar(IE.STEPS_2022)


class TestExposure:
    @pytest.mark.parametrize("ym,e,p", [
        ("2022-09", 1.0, 0.0),                       # 2022-10〜2023-07 の 4 回が窓に入る
        ("2024-12", 0.25, 333 / 364),                # 最後の 1 回（2025-01-30）と除外後 333 日
        ("2025-06", 0.0, 1.0),                       # 窓のすべてが除外の後
        ("2021-06", 0.0, 0.0),
    ])
    def test_hand_values(self, ym, e, p):
        assert S.label_window_exposure(ym, CAL) == (pytest.approx(e), pytest.approx(p))

    def test_month_end(self):
        assert S.month_end("2024-02") == date(2024, 2, 29)
        assert S.month_end("2024-12") == date(2024, 12, 31)


def _random_aligned(seed: int = 0, n: int = 30, yms=("2024-01", "2024-02", "2024-03")) -> dict:
    rng = np.random.default_rng(seed)
    out = {}
    for ym in yms:
        for i in range(n):
            y = float(rng.normal())
            out[(ym, f"E{i:02d}")] = (float(np.round(y + rng.normal(), 1)), y)   # 予測に同点を混ぜる
    return out


class TestPlacement:
    def test_group_bias_equals_blank_group_breakdown(self):
        """M 族の偏りは momentum_gate の群の内訳と同じ定義（期ごとの値まで一致する）。"""
        from scripts import momentum_gate

        aligned = _random_aligned()
        group = {f"E{i:02d}" for i in range(0, 30, 3)}
        labels = {k: ["g"] for k in aligned if k[1] in group}
        want = momentum_gate.blank_group_breakdown(aligned, labels)["g"]["bias_by_period"]
        got = S.group_bias(S.placements(aligned), {ym: group for ym in ("2024-01", "2024-02", "2024-03")})
        assert got == pytest.approx(want)

    def test_tiny_period_is_left_out(self):
        df = S.placements({("2024-01", "E1"): (0.1, 0.2), ("2024-01", "E2"): (0.2, 0.1)})
        assert df.empty

    def test_paired_domain(self):
        pairs = {"T1": ("C1", "C2"), "T2": ("C3",), "T3": ("C1",)}
        got = S.paired_domain({"2024-01": {"T1", "T2", "C2", "T3"}}, pairs)
        # T2 は対照がいない・T3 の対照 C1 もいない → T は T1 だけ、C はそのそろった対照 C2 だけ
        assert got == {"2024-01": ({"T1"}, {"C2"})}


class TestRegression:
    def test_lead_year_is_left_out(self):
        exposure = {f"{y}-01": (float(y - 2019), 0.0) for y in range(2019, 2026)}
        exposure.update({f"{y}-07": (0.0, float(y > 2023)) for y in range(2019, 2026)})
        d = {ym: 0.1 + 0.02 * e + 0.05 * p for ym, (e, p) in exposure.items()}
        d["2021-01"] = 99.0                                  # 2021 年の期は回帰に入らない
        fit = S.fit_m_regression(d, exposure)
        assert (fit["a"], fit["b"], fit["c"]) == (pytest.approx(0.1), pytest.approx(0.02), pytest.approx(0.05))
        assert fit["n_periods"] == 12

    def test_unidentified_coefficients_are_none(self):
        exposure = {f"2019-{m:02d}": (0.0, 0.0) for m in range(1, 13)}
        fit = S.fit_m_regression({ym: 0.1 for ym in exposure}, exposure)
        assert fit["b"] is None and fit["c"] is None


class TestStrataAndPseudo:
    def test_strata_bins(self):
        v = pd.DataFrame({"logcap": [float(i) for i in range(20)],
                          "log_turnover_ratio": [float(-i) for i in range(20)],
                          "sector17": ["A"] * 19 + [None]},
                         index=[f"E{i:02d}" for i in range(20)])
        st = S.strata(v)
        assert "E19" not in st                                # 業種の無い社は層に入らない
        assert st["E00"][:2] == (0, 2) and st["E18"][:2] == (9, 0)

    def test_pseudo_groups_keep_the_strata_counts(self):
        st = {**{f"T{i}": (0, 0, "A") for i in range(3)}, "T9": (1, 0, "A"),
              **{f"P{i}": (0, 0, "A") for i in range(10)}, "Q0": (2, 0, "A")}
        groups, shortfall = S.pseudo_groups(st, ["T0", "T1", "T2", "T9"],
                                            [f"P{i}" for i in range(10)] + ["Q0"], n=50, seed=0)
        assert all(len(g) == 3 and g <= {f"P{i}" for i in range(10)} for g in groups)
        assert shortfall == {"size1|liq0|A": 1}                # 層 (1,0,A) に引ける社が無い
        again, _ = S.pseudo_groups(st, ["T0", "T1", "T2", "T9"],
                                   [f"P{i}" for i in range(10)] + ["Q0"], n=50, seed=0)
        assert groups == again and len({frozenset(g) for g in groups}) > 1


def _m_panel(shift: float, seed: int = 0) -> tuple:
    """2019-01〜2025-10 の月次 OOF。処置 20 社（対照 2 社ずつ）＋他 200 社。処置の予測だけ shift × E_t 上げる。"""
    rng = np.random.default_rng(seed)
    yms = [f"{y}-{m:02d}" for y in range(2019, 2026) for m in range(1, 13)][:82]
    pairs = {f"T{i:02d}": (f"C{i:02d}a", f"C{i:02d}b") for i in range(20)}
    others = [f"O{i:03d}" for i in range(200)]
    firms = list(pairs) + [c for cs in pairs.values() for c in cs] + others
    aligned = {}
    for ym in yms:
        e, _p = S.label_window_exposure(ym, CAL)
        for ec in firms:
            y = float(rng.normal())
            aligned[(ym, ec)] = (y + float(rng.normal()) + (shift * e if ec in pairs else 0.0), y)
    return aligned, pairs, {ec: (0, 0, "A") for ec in firms}, set(others)


class TestMFamily:
    def test_planted_effect_is_recovered(self):
        aligned, pairs, st, eligible = _m_panel(shift=1.5)
        r = S.m_family_test(aligned, pairs, CAL, st, eligible, permutations=200)
        assert r["fit"]["b"] > 0.1
        assert r["permutation"]["p"]["b"] < S.M_ALPHA
        assert r["permutation"]["n_valid"] == 200 and r["permutation"]["pseudo_size"] == 20
        assert set(r["lead_periods"]) == {f"2021-{m:02d}" for m in range(1, 13)}
        assert r["fit"]["n_periods"] == 82 - 12
        verdict = S.m_family_verdict({"xgb_m2": r})
        assert verdict["xgb_m2"]["b"]["significant"] is True

    def test_null_is_not_significant(self):
        aligned, pairs, st, eligible = _m_panel(shift=0.0, seed=3)
        r = S.m_family_test(aligned, pairs, CAL, st, eligible, permutations=200)
        assert r["permutation"]["p"]["b"] > S.M_ALPHA and r["permutation"]["p"]["c"] > S.M_ALPHA

    def test_result_carries_no_codes(self):
        aligned, pairs, st, eligible = _m_panel(shift=0.5)
        r = S.m_family_test(aligned, pairs, CAL, st, eligible, permutations=20)
        assert not re.search(r'"(T\d\d|C\d\d[ab]|O\d{3})"', json.dumps(r, default=str))

    def test_alpha_is_bonferroni_over_four(self):
        assert S.M_ALPHA == pytest.approx(0.0125)
        assert S.M_MODELS == {"xgb_m2": "raw", "elasticnet": "size_neutral"}


# ── stage B（開封・#1032）─────────────────────────────────────────────────────────

def _weekly(lo: date, hi: date, value: float = 0.0) -> pd.Series:
    idx = [lo + W * k for k in range(((hi - lo).days // 7) + 1)]
    return pd.Series(value, index=idx, dtype=float)


def _planted(series: pd.Series, pressure: float, reversal: float, post: float) -> pd.Series:
    """圧力窓の各週に pressure、戻り窓の各週に reversal、除外後 [+1, +52] の各週に post を足す。"""
    s = series.copy()
    for pw in CAL:
        for w in (pw.week - W, pw.week):
            s[w] += pressure
        for k in range(1, 5):
            s[pw.week + W * k] += reversal
    for k in range(1, S.POST_WEEKS + 1):
        s[CAL[-1].week + W * k] += post
    return s


class TestStageBQuantities:
    def test_announcement_week(self):
        # 2022-10-07（金）引け後 → 翌平日 10-10（月・祝日だが同じ週の 10-11 が最初の取引日）
        assert S.announcement_week(date(2022, 10, 7)) == date(2022, 10, 10)

    def test_hand_calculation(self):
        ar = _planted(_weekly(date(2022, 4, 4), date(2026, 3, 30)), pressure=-0.01, reversal=0.0025, post=0.001)
        q = S.event_quantities(ar, CAL)
        # m = 10 の戻り窓 [w_10 + 1, w_10 + 4] は除外後の窓 [+1, +52] と重なる（§5.1）＝R に 4 週 × 0.001 / 10 が乗る
        r = 0.01 + 4 * 0.001 / 10
        assert q["I"] == pytest.approx(-0.02) and q["R"] == pytest.approx(r)
        assert q["kappa"] == pytest.approx(1 + r / -0.02) and q["delta_hat"] == pytest.approx(4 * (-0.02 + r))
        post = 0.052 + 4 * 0.0025                     # 同じ重なりで、除外後の 52 週にも m = 10 の戻りが入る
        assert q["car_post"] == pytest.approx(post)
        assert q["rho"] == pytest.approx(post / abs(10 * (-0.02 + r)))

    def test_rho_is_left_out_when_nothing_is_lost(self):
        ar = _planted(_weekly(date(2022, 4, 4), date(2026, 3, 30)), pressure=-0.01, reversal=0.006, post=0.001)
        assert S.event_quantities(ar, CAL)["rho"] is None        # I + R ≥ 0

    def test_missing_week_makes_the_window_unknown(self):
        ar = _weekly(date(2022, 4, 4), date(2026, 3, 30))
        ar[CAL[3].week] = np.nan
        assert S.event_quantities(ar, CAL)["I"] is None
        assert S.car(ar, [CAL[0].week]) == 0.0

    def test_holm(self):
        assert S.holm({"E1": 0.01, "E2": 0.04, "E4": 0.03}) == {"E1": True, "E4": False, "E2": False}
        assert S.holm({"E1": 0.01, "E2": 0.02, "E4": 0.04}) == {"E1": True, "E2": True, "E4": True}
        # p の無い検定（切り替えで外した E4）は非有意として最後に並ぶ
        assert S.holm({"E1": 0.001, "E2": 0.2, "E4": None}) == {"E1": True, "E2": False, "E4": False}

    def test_e2_is_not_significant_when_there_is_no_pressure(self):
        assert S.e2_p(0.01, [{"I": -0.01, "kappa": 0.5}]) == 1.0
        boot = [{"I": -0.02, "kappa": 0.4}, {"I": 0.001, "kappa": None}, {"I": -0.02, "kappa": -0.1}]
        assert S.e2_p(-0.02, boot) == pytest.approx((2 + 1) / (3 + 1))

    def test_empirical_p_and_nw_direction(self):
        assert S.empirical_impact_p(-0.02, [-0.03, -0.01, 0.0, 0.01]) == pytest.approx(2 / 5)
        g = [1e-4, 0.0, 0.0, 0.0, 0.0]
        weights = {date(2022, 10, 24): 1.0}
        assert S.nw_one_sided_p(-0.02, weights, g, lower=True) == pytest.approx(NormalDist().cdf(-2.0))
        assert S.nw_one_sided_p(0.02, weights, g, lower=False) == pytest.approx(1 - NormalDist().cdf(2.0))

    def test_lee_bounds_and_retention(self):
        assert S.lee_bounds([float(i) for i in range(1, 11)], 0.2) == [pytest.approx(4.5), pytest.approx(6.5)]
        idx = [date(2023, 1, 2) + W * k for k in range(10)]
        closes = pd.DataFrame({"A": 1.0, "B": 1.0, "C": [1.0] * 5 + [np.nan] * 5, "D": np.nan}, index=idx)
        # 最初の 4 週に足がある A・B・C のうち、最後の 4 週にも足があるのは A・B
        assert S.retention(closes, ["A", "B", "C", "D"], idx[0], idx[-1]) == pytest.approx(2 / 3)


class TestStageBBootstrap:
    def test_planted_delta_is_recovered_and_covered(self):
        rng = np.random.default_rng(11)
        base = _weekly(date(2022, 4, 11), date(2026, 1, 26))
        noise = pd.Series(rng.normal(0, 0.002, size=len(base)), index=base.index)
        ar = _planted(noise, pressure=-0.0125, reversal=0.00125, post=0.0)
        truth = 4 * (-0.025 + 0.005)
        q = S.event_quantities(ar, CAL)
        assert q["delta_hat"] == pytest.approx(truth, abs=0.012)
        boot = S.residual_bootstrap(ar, CAL, reps=400)
        lo, hi = S.percentile_ci([b["delta_hat"] for b in boot])
        assert lo < truth < hi and lo < q["delta_hat"] < hi
        assert S.e2_p(q["I"], boot) < 0.05                       # 戻りは一部だけ＝持続率 κ > 0

    def test_deterministic_with_the_seed(self):
        ar = _planted(_weekly(date(2022, 4, 11), date(2026, 1, 26), 0.0001), -0.01, 0.001, 0.0)
        a = S.residual_bootstrap(ar, CAL, reps=20)
        assert a == S.residual_bootstrap(ar, CAL, reps=20)


def test_label_did_recovers_b():
    rng = np.random.default_rng(3)
    pairs = {f"T{i}": (f"C{i}",) for i in range(20)}
    labels = {}
    for ym in S.ym_range("2019-07", "2024-01"):
        shift = -0.05 if "2022-10" <= ym <= "2024-01" else 0.0
        for i in range(20):
            y = float(rng.normal(0, 0.1))
            labels[(ym, f"C{i}")] = y
            labels[(ym, f"T{i}")] = y + shift + float(rng.normal(0, 0.005))
    r = S.label_did(labels, pairs)
    assert (r["n_event"], r["n_base"]) == (16, 27)
    assert r["b"] == pytest.approx(-0.05, abs=0.005) and r["p_one_sided"] < 0.01


def _stage_b_synthetic(pressure: float = -0.01, reversal: float = 0.0025, seed: int = 0) -> dict:
    """2019-07〜2026-03 の週次。処置 6 社（対照 2 社ずつ・値の近い双子）。処置の圧力窓・戻り窓に効果を仕込む。"""
    rng = np.random.default_rng(seed)
    weeks = [date(2019, 7, 29) + W * k for k in range(((date(2026, 3, 30) - date(2019, 7, 29)).days // 7) + 1)]
    pressure_weeks = {w for pw in CAL for w in (pw.week - W, pw.week)}
    reversal_weeks = {pw.week + W * k for pw in CAL for k in range(1, 5)}
    closes, pairs = {}, {}
    for i in range(6):
        market = rng.normal(0, 0.02, size=len(weeks))
        group = [f"T{i}", f"C{i}a", f"C{i}b"]
        pairs[group[0]] = group[1:]
        for ec in group:
            r = market + rng.normal(0, 0.001, size=len(weeks))
            if ec == group[0]:
                r = r + np.array([pressure if w in pressure_weeks else reversal if w in reversal_weeks else 0.0
                                  for w in weeks])
            closes[ec] = dict(zip(weeks, (100 * np.exp(np.cumsum(r))).tolist()))
    return dict(closes=S.weekly_frame(closes), pairs=pairs, calendar=CAL,
                announce_week=S.announcement_week(date(2022, 10, 7)), released={"T0"}, labels={}, gaps={},
                gap_yms=[])


class TestStageB:
    @pytest.fixture(scope="class")
    def result(self):
        return S.stage_b(**_stage_b_synthetic())

    def test_recovers_the_planted_pressure(self, result):
        q = result["quantities"]
        assert q["I"] == pytest.approx(-0.02, abs=0.002) and q["R"] == pytest.approx(0.01, abs=0.002)
        e = result["e_family"]
        assert e["tests"] == ["E1", "E2", "E4"] and e["significant"]["E1"] is True
        if e["switch_to_empirical_p"]:
            assert e["p"]["E4"] is None                          # 経験 p を持たない E4 は確証から外す
        lo, hi = result["ci"]["delta_hat"]
        assert lo < q["delta_hat"] < hi
        assert result["materials"]["delta_hat_ci90"] == result["ci"]["delta_hat"]
        lo, hi = result["ci"]["I"]                                 # 2026 群の予測帯の材料（#1062）
        assert lo < q["I"] < hi

    def test_ci_of_I_is_the_percentile_of_the_bootstrap_draws(self, monkeypatch):
        boot = [{"I": -0.001 * k, "R": 0.0005 * k, "kappa": 0.5, "delta_hat": -0.002 * k, "car_post": None,
                 "rho": None} for k in range(1, 41)]
        monkeypatch.setattr(S, "residual_bootstrap", lambda ar, calendar, **kw: boot)
        result = S.stage_b(**_stage_b_synthetic())
        assert result["ci"]["I"] == S.percentile_ci([b["I"] for b in boot])
        assert list(result["ci"]) == ["I", "delta_hat", "kappa", "rho"]

    def test_no_attrition_means_no_lee_bounds(self, result):
        for row in result["survival_bias"].values():
            assert row["retention_treated"] == 1.0 and row["retention_control"] == 1.0
            assert row["lee_bounds"] is None

    def test_summary_carries_no_codes(self, result):
        assert not re.search(r'"(T\d|C\d[ab])"', json.dumps(result, default=str))

    def test_descriptive_windows(self, result):
        d = result["descriptive"]
        assert set(d["drift"]) == set(range(1, 10))
        assert d["announcement"] is not None and d["post_short"] is not None
        assert d["released_after_review"] is not None


def test_pure_module_does_not_import_database_or_scripts():
    src = (ROOT / "index_event_study.py").read_text(encoding="utf-8")
    for bad in ("\nimport database", "\nfrom database", "\nfrom scripts", "\nimport scripts",
                "\n    import database", "\n    from database"):
        assert bad not in src
