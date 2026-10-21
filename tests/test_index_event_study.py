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


class TestCalendar:
    def test_showa_day_moves_the_pressure_day_back(self):
        # 2024-04-30（火）の前営業日は 4/29（月・昭和の日）を飛ばして 4/26（金）
        assert S.previous_business_day(date(2024, 4, 30)) == date(2024, 4, 26)

    def test_substitute_holiday_2018(self):
        # 2018-04-29 は日曜なので 4/30（月）が振替休日＝4 月の最終営業日は 4/27（金）
        assert S.last_business_day(2018, 4) == date(2018, 4, 27)

    def test_placebo_weeks(self):
        weeks = S.placebo_pressure_weeks()
        assert len(weeks) == 20
        assert weeks[0] == date(2014, 1, 27)        # 1/31（金）の前営業日 1/30（木）の週
        assert weeks[1] == date(2014, 4, 28)        # 4/30（水）の前営業日は 4/28（月・4/29 は祝日）
        assert date(2018, 4, 23) in weeks           # 最終営業日 4/27（金）の前営業日 4/26（木）の週
        assert weeks[-1] == date(2018, 10, 29)      # 10/31（水）の前営業日 10/30（火）の週
        assert all(w.weekday() == 0 for w in weeks)

    @pytest.mark.parametrize("d", [date(2014, 3, 31), date(2020, 4, 30), date(2022, 4, 21)])
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
        # 系列ごとの誤検出率は 20 個の擬似 CAR の偶然に引きずられるので、多数の系列で平均する
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
        assert res["n_usable"] == 20
        assert res["fpr"] > 0.10 and res["switch_to_empirical_p"] is True

    def test_too_few_usable_weeks(self):
        rng = np.random.default_rng(2)
        ar = _pre_series(rng)
        ar[[w >= date(2017, 1, 1) for w in ar.index]] = np.nan     # 使えるのは 2014〜2016 の 12 個
        res = S.placebo_test(ar, S.placebo_pressure_weeks(), S.nw_autocovariances(ar))
        assert res["n_usable"] == 12
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
    """2014〜2025 の週次。処置 6 社・対照 18 社（処置ごとに値の近い 3 社）。圧力週に処置の売買代金の山。"""
    rng = np.random.default_rng(seed)
    weeks = [date(2013, 12, 30) + W * k for k in range(((date(2025, 2, 24) - date(2013, 12, 30)).days // 7) + 1)]
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
        assert summary["placebo"]["n_usable"] == 20
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


def test_pure_module_does_not_import_database_or_scripts():
    src = (ROOT / "index_event_study.py").read_text(encoding="utf-8")
    for bad in ("\nimport database", "\nfrom database", "\nfrom scripts", "\nimport scripts",
                "\n    import database", "\n    from database"):
        assert bad not in src
