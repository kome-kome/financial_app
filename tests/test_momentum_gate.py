"""scripts/momentum_gate.py のユニットテスト（モメンタム既定 ON/OFF の昇格ゲート）。

主眼は **2条件を比較可能な形へ揃える手続きが壊れていないこと**。このゲートは母集団が動く
条件比較（モメンタム ON は履歴不足の社・月を落とす）なので、揃え方を1段でも取り落とすと
「特徴量の効果」ではなく「母集団が変わった効果」を測ってしまう。しかも**その失敗は例外に
ならず、それらしい数値が出る**（初回実測では共通域が 0 件になり、その手前まで数値は健全に
見えていた）。よって:

  - fold の位相ずれ（`TestFoldPhase`）… なぜ共通月制限が要るのかを実挙動で縛る
  - (ym,ec) の突合契約（`TestRestrict`）… `_align` / `build_oof_meta` と同じ index 1:1 前提

パネル構築（`build_snapshots`）と実測本体は DB フルロードが要るのでここでは触らない。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.macro_ensemble import _align
from plugins.utils import walk_forward_cv_monthly
from scripts.momentum_gate import (
    ALPHA, CONDS, METRICS, MODELS, MOM_WINDOW, N_TESTS,
    _num, _panel_stats, _restrict, _restrict_months,
)


def _months(start_ym: str, n: int) -> list[str]:
    y, m = int(start_ym[:4]), int(start_ym[5:])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _panel(yms: list[str], n_per_month: int = 8, seed: int = 0) -> dict:
    """OLS が解ける程度の小さな月次パネル（特徴量2本）。"""
    rng = np.random.default_rng(seed)
    out = {}
    for ym in yms:
        rows = []
        for _ in range(n_per_month):
            x = rng.normal(size=2)
            rows.append(([float(x[0]), float(x[1])], float(x[0] * 0.5 + rng.normal() * 0.1)))
        out[ym] = rows
    return out


# ── ゲートの契約（定数）────────────────────────────────────────────────────

class TestGateContract:
    def test_two_conditions_only(self):
        """条件は off/on の2つ。増やすなら検定数と alpha を見直すこと。"""
        assert CONDS == {"off": False, "on": True}

    def test_bonferroni_matches_the_test_count(self):
        """alpha は 2モデル x 2指標 = 4 検定の Bonferroni 補正であること。"""
        assert N_TESTS == len(MODELS) * len(METRICS) == 4
        assert ALPHA == pytest.approx(0.05 / 4)

    def test_window_is_the_12_1_standard(self):
        """窓は 12-1 モメンタムの標準形に固定（窓の探索はゲートの対象外）。"""
        assert MOM_WINDOW == 12


# ── fold の位相（このテストが共通月制限の存在理由）──────────────────────────

class TestFoldPhase:
    """`walk_forward_cv_monthly` の test 月は月リストの**インデックス位置**で決まる
    （`for i in range(min_train_months + embargo_months, len(all_yms), step_months)`）。
    よってパネルの開始月が1ヶ月違うだけで test 月が step_months 周期の別位相になり、
    `paired_ic_significance` がペアリングできる共通 test 期が 0 になる。
    """

    def _test_yms(self, yms: list[str]) -> set:
        _folds, resid = walk_forward_cv_monthly(
            _panel(yms), ["f0", "f1"],
            min_train_months=6, step_months=3, return_residuals=True, embargo_months=12,
        )
        return set(resid)

    def test_shifted_start_month_yields_disjoint_test_months(self):
        """開始月が1ヶ月ずれると test 月が一度も一致しない（実測で踏んだ現象）。"""
        a = self._test_yms(_months("2020-01", 24))
        b = self._test_yms(_months("2020-02", 23))
        assert a and b
        assert a.isdisjoint(b)

    def test_restricting_to_common_months_realigns_the_phase(self):
        """共通月へ制限すれば test 月が完全に一致する（ゲートが踏む手続き）。"""
        long_yms, short_yms = _months("2020-01", 24), _months("2020-02", 23)
        common = set(long_yms) & set(short_yms)
        a = self._test_yms(sorted(common))
        b = self._test_yms(sorted(common))
        assert a == b and len(a) >= 2


# ── (ym,ec) 制限 ───────────────────────────────────────────────────────────

class TestRestrict:
    RESID = {"2024-01": [(0.1, 0.2), (0.3, 0.4), (0.5, 0.6)],
             "2024-04": [(0.7, 0.8)]}
    META = {"2024-01": [("E1", "電気"), ("E2", "銀行"), ("E3", "化学")],
            "2024-04": [("E9", "機械")]}
    IDS = {"2024-01": ["E1", "E2", "E3"], "2024-04": ["E9"]}

    def test_keeps_only_the_requested_pairs_in_order(self):
        keys = {("2024-01", "E1"), ("2024-01", "E3")}
        r, m = _restrict(self.RESID, self.META, self.IDS, keys)
        assert r == {"2024-01": [(0.1, 0.2), (0.5, 0.6)]}
        assert m == {"2024-01": [("E1", "電気"), ("E3", "化学")]}

    def test_residuals_and_meta_stay_index_aligned(self):
        """oof_backtest は residuals[j] と meta[j] の index 1:1 に依拠する。"""
        keys = {("2024-01", "E2"), ("2024-04", "E9")}
        r, m = _restrict(self.RESID, self.META, self.IDS, keys)
        for ym in r:
            assert len(r[ym]) == len(m[ym])
        assert m["2024-01"] == [("E2", "銀行")]
        assert r["2024-01"] == [(0.3, 0.4)]

    def test_month_emptied_by_the_restriction_is_dropped(self):
        r, m = _restrict(self.RESID, self.META, self.IDS, {("2024-04", "E9")})
        assert set(r) == {"2024-04"} and set(m) == {"2024-04"}

    def test_empty_key_set_yields_empty_panel(self):
        assert _restrict(self.RESID, self.META, self.IDS, set()) == ({}, {})

    def test_ids_shorter_than_residuals_does_not_raise(self):
        """ids が欠けた行は突合できないので落とす（IndexError にしない）。"""
        r, _m = _restrict(self.RESID, self.META, {"2024-01": ["E1"]},
                          {("2024-01", "E1")})
        assert r == {"2024-01": [(0.1, 0.2)]}


class TestRestrictMatchesAlign:
    """共通キーは `_align` が作る。両者が同じ突合契約に乗っていることを縛る。"""

    def test_align_keys_round_trip_through_restrict(self):
        resid = {"2024-01": [(0.1, 0.2), (0.3, 0.4)]}
        ids = {"2024-01": ["E1", "E2"]}
        meta = {"2024-01": [("E1", "電気"), ("E2", "銀行")]}
        keys = set(_align(resid, ids))
        r, m = _restrict(resid, meta, ids, keys)
        assert r == resid and m == meta

    def test_nan_rows_are_dropped_by_both(self):
        """`_align` は NaN を弾く。制限側も同じ行を落とし、ズレを作らない。"""
        nan = float("nan")
        resid = {"2024-01": [(0.1, 0.2), (nan, 0.4), (0.5, 0.6)]}
        ids = {"2024-01": ["E1", "E2", "E3"]}
        meta = {"2024-01": [("E1", "電気"), ("E2", "銀行"), ("E3", "化学")]}
        keys = set(_align(resid, ids))
        assert ("2024-01", "E2") not in keys
        r, m = _restrict(resid, meta, ids, keys)
        assert r == {"2024-01": [(0.1, 0.2), (0.5, 0.6)]}
        assert m == {"2024-01": [("E1", "電気"), ("E3", "化学")]}


class TestRestrictMonths:
    PANEL = ({"2024-01": [1], "2024-02": [2], "2024-03": [3]},
             {"2024-01": ["m1"], "2024-02": ["m2"], "2024-03": ["m3"]},
             {"2024-01": ["E1"], "2024-02": ["E2"], "2024-03": ["E3"]},
             ["f0", "f1"])

    def test_keeps_only_the_given_months(self):
        s, m, i, feats = _restrict_months(self.PANEL, {"2024-01", "2024-03"})
        assert set(s) == set(m) == set(i) == {"2024-01", "2024-03"}

    def test_feature_names_are_untouched(self):
        """列は条件ごとに違う（on は momentum が1本増える）。制限で触ってはいけない。"""
        assert _restrict_months(self.PANEL, {"2024-01"})[3] == ["f0", "f1"]

    def test_unknown_month_is_ignored(self):
        s, *_ = _restrict_months(self.PANEL, {"2024-01", "1999-12"})
        assert set(s) == {"2024-01"}


class TestPanelStats:
    def test_counts_months_samples_and_distinct_companies(self):
        st = _panel_stats(
            {"2024-01": [1, 2], "2024-02": [3]},
            {"2024-01": ["E1", "E2"], "2024-02": ["E1"]},
            ["f0", "f1", "f2"],
        )
        assert st["months"] == 2 and st["samples"] == 3
        assert st["companies"] == 2 and st["n_features"] == 3
        assert st["first_ym"] == "2024-01" and st["last_ym"] == "2024-02"

    def test_empty_panel_does_not_raise(self):
        st = _panel_stats({}, {}, [])
        assert st["months"] == 0 and st["first_ym"] is None


class TestNum:
    def test_none_renders_as_dash(self):
        assert _num(None) == "-"

    def test_float_keeps_the_sign(self):
        assert _num(0.1234) == "+0.1234"
        assert _num(-0.1234) == "-0.1234"

    def test_int_passes_through(self):
        assert _num(15) == "15"
