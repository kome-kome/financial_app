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
    ALPHA, CONDS, METRICS, MODEL_SPECS, MODELS, MOM_WINDOW, N_TESTS,
    RISK_BASE_COND, RISK_CONDS, RISK_LAMBDA_PLUGIN, RISK_MODELS, RISK_NO_AXIS,
    _month_end_bound, _num, _panel_stats, _restrict, _restrict_months,
    apply_risk_axis, base_of, bonferroni_alpha, build_conditions, mode_of,
    normalize_lambdas, production_risk, quantile_risk_profile, resolve_risk_lambda,
    resolve_risk_base, risk_base_cond, risk_by_ym, risk_common_keys, risk_cond_name, risk_specs,
    verdict_text,
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


# ── リスク軸モード（`--risk-axis`・#709）──────────────────────────────────────

class _Row:
    """`_realized_vol` が読む週次行の最小形（trade_date / close_last）。"""

    def __init__(self, trade_date: str, close_last: float):
        self.trade_date = trade_date
        self.close_last = close_last


class TestRiskAxisContract:
    def test_three_conditions_with_production_default_as_base(self):
        """条件は mu_only / r2 / r_macro の3つで、分母は本番の既定（r2）。

        `mu_only` を残すのは「そもそもリスクを引くことが効いているのか」が一度も
        測られていないから——既存6モードの rank-IC はすべて μ̂ だけの順位だった。
        """
        assert RISK_CONDS == {"mu_only": None, "r2": "r2", "r_macro": "r_macro"}
        assert RISK_BASE_COND == "r2"
        conds = build_conditions(risk_axis=True)
        assert set(conds) == set(RISK_CONDS)
        assert base_of(conds) == "r2"

    def test_all_conditions_share_one_production_cond(self):
        """**3条件のパネルは同一**。ここが崩れると μ̂ が条件ごとにずれ、軸の差と混ざる。"""
        conds = build_conditions(risk_axis=True)
        assert len(set(conds.values())) == 1
        c = conds["r2"]
        assert c.use_momentum is False and c.use_macro is True
        assert c.demean_target is False and c.fin_rows == "annual"

    def test_mode_name_and_models(self):
        assert mode_of(risk_axis=True) == "risk_axis"
        # 既定は M-1 だけ。M-6 も U を持つが分母（mu_only）が違うので別に回す（#808）。
        assert RISK_MODELS == ["risk_return"]

    def test_bonferroni_matches_the_default_gate(self):
        """1モデル x 2指標 x 2条件 = 4 検定＝既定ゲートと同じ検定数。"""
        conds = build_conditions(risk_axis=True)
        assert len(RISK_MODELS) * len(METRICS) * (len(conds) - 1) == N_TESTS

    @pytest.mark.parametrize("kwargs", [
        {"windows": [12]}, {"macro": True}, {"interactions": True},
        {"max_features": [20]}, {"fin_rows": True}, {"demean_target": True},
    ])
    def test_cannot_be_combined_with_other_modes(self, kwargs):
        """他の軸と同時に振ると、どちらの効果か分離できない（既存6モードと同じ規則）。"""
        with pytest.raises(ValueError, match="--risk-axis"):
            build_conditions(risk_axis=True, **kwargs)


class TestRiskAxisExplicitLambdas:
    """`--lambdas`（#808）: λ を画面と同じ値で明示し、`mu_only` ＋ 軸 × λ を測る。"""

    LAMS = [0.1, 0.3, 1.0]

    def test_conditions_are_mu_only_plus_axis_times_lambda(self):
        conds = build_conditions(risk_axis=True, lambdas=self.LAMS)
        assert list(conds) == ["mu_only", "r2@0.1", "r2@0.3", "r2@1.0",
                               "r_macro@0.1", "r_macro@0.3", "r_macro@1.0"]

    def test_all_conditions_still_share_one_cond(self):
        """λ を増やしてもパネルと CV は1回＝μ̂ はビット単位で一致する。"""
        conds = build_conditions(risk_axis=True, lambdas=self.LAMS)
        assert len(set(conds.values())) == 1

    def test_specs_carry_axis_and_lambda(self):
        specs = risk_specs(self.LAMS)
        assert specs["mu_only"] == (None, 0.0)
        assert specs["r2@0.3"] == ("r2", 0.3)
        assert specs["r_macro@1.0"] == ("r_macro", 1.0)

    def test_default_specs_keep_the_m1_registration(self):
        """`--lambdas` 無しは M-1 の事前登録の3条件のまま（ADR-0050 の 2026-09-21 追記）。"""
        assert risk_specs(None, 1.0) == {"mu_only": (None, 0.0), "r2": ("r2", 1.0),
                                         "r_macro": ("r_macro", 1.0)}
        assert set(build_conditions(risk_axis=True)) == set(RISK_CONDS)

    @pytest.mark.parametrize("bad", [[0.0], [0.1, 0.0], [-0.1],
                                     [float("nan")], [float("inf")]])
    def test_zero_negative_or_non_finite_lambda_stops(self, bad):
        """λ=0 は `mu_only` と同一＝比べても差が構造的にゼロ（黙って「差なし」が出る形）。"""
        with pytest.raises(ValueError, match="λ"):
            build_conditions(risk_axis=True, lambdas=bad)

    def test_duplicates_are_dropped_and_sorted(self):
        """同じ λ を2回測ると検定数だけが増えて α が不当に締まる。"""
        assert normalize_lambdas([1.0, 0.1, 0.1]) == [0.1, 1.0]

    def test_close_lambdas_do_not_collapse_into_one_name(self):
        specs = risk_specs([0.1234561, 0.1234562])
        assert len(specs) == 1 + 2 * 2

    def test_lambdas_without_risk_axis_stop(self):
        with pytest.raises(ValueError, match="--lambdas"):
            build_conditions(lambdas=[0.1])

    def test_m6_gate_has_twelve_tests_above_the_p_floor(self):
        """1モデル x 2指標 x 6条件 = 12 検定。α は p の下限 2/(n_boot+1) より上（ADR-0063 §5）。"""
        conds = build_conditions(risk_axis=True, lambdas=self.LAMS)
        assert 1 * len(METRICS) * (len(conds) - 1) == 12
        alpha = bonferroni_alpha(1, len(conds))
        assert alpha == pytest.approx(0.05 / 12)
        assert alpha > 2 / (2000 + 1)


class TestRiskLambdaSource:
    """λ は**画面に U を出すプラグイン**から取る（#808）。

    `MODEL_SPECS` の設定元から取っていた間は、M-6 を M-2（`macro_gbdt`）の λ=1.0 で、
    M-1 と並べた M-6 を M-1 の λ で黙って測る形だった。
    """

    def test_every_model_has_a_screen_plugin(self):
        assert set(RISK_LAMBDA_PLUGIN) == set(MODEL_SPECS)

    def test_m6_lambda_does_not_come_from_the_panel_source(self):
        assert MODEL_SPECS["elasticnet"][0] == "macro_gbdt"     # パネル設定は M-2 から借りる
        assert RISK_LAMBDA_PLUGIN["elasticnet"] == "macro_enet"  # λ は M-6 の画面から
        assert production_risk("elasticnet")[0] == 0.0           # #807 の既定
        assert production_risk("xgb_m2")[0] > 0                  # 取り違えると M-2 の λ で測る

    def test_m6_without_lambdas_stops(self):
        with pytest.raises(ValueError, match="--lambdas"):
            resolve_risk_lambda(["elasticnet"])

    def test_m1_resolves_to_its_screen_default(self):
        lam = resolve_risk_lambda(["risk_return"])
        assert lam > 0 and lam == production_risk("risk_return")[0]

    def test_models_with_different_defaults_stop(self):
        with pytest.raises(ValueError, match="--lambdas"):
            resolve_risk_lambda(["risk_return", "elasticnet"])


class TestRiskBaseCond:
    """分母は各モデルの画面が既定で出す並び（#808）。M-1 は r2、M-6 は mu_only。"""

    def test_m1_base_is_the_preregistered_r2(self):
        specs = risk_specs(None, resolve_risk_lambda(["risk_return"]))
        assert risk_base_cond(["risk_return"], specs) == RISK_BASE_COND == "r2"

    def test_m1_with_explicit_lambdas_uses_its_default_lambda(self):
        assert risk_base_cond(["risk_return"], risk_specs([0.3, 1.0])) == "r2@1.0"

    def test_m1_without_its_default_lambda_stops(self):
        """分母の無い比較になる。"""
        with pytest.raises(ValueError, match="条件に無い"):
            risk_base_cond(["risk_return"], risk_specs([0.3]))

    def test_m6_base_is_mu_only(self):
        assert risk_base_cond(["elasticnet"], risk_specs([0.1, 0.3, 1.0])) == RISK_NO_AXIS

    def test_m2_base_is_its_screen_default_ordering(self):
        """M-2 の分母は画面の既定の並び（#814 の run は `r_macro@1.0`・#816 で既定を R2 へ変えて `r2@1.0`）。

        期待値を書き写さず既定から作る——既定を変えたら分母も追随するのが正しい挙動で、
        書き写すと「既定を変えたのに旧既定を分母に測る」形をテストが固定してしまう。
        """
        lam, axis = production_risk("xgb_m2")
        want = RISK_NO_AXIS if lam <= 0 else risk_cond_name(axis, lam)
        assert risk_base_cond(["xgb_m2"], risk_specs([0.1, 0.3, 1.0])) == want

    def test_models_with_different_bases_stop(self):
        """判定の向きが1本の表で混ざる。別々に回す。"""
        with pytest.raises(ValueError, match="別々に"):
            risk_base_cond(["risk_return", "elasticnet"], risk_specs([0.1, 1.0]))

    def test_verdict_names_the_actual_base(self):
        assert "(mu_only)" in verdict_text("risk_axis", 7, [], [], base="mu_only")
        assert "(r2)" in verdict_text("risk_axis", 3, [], [])


class TestRiskBaseOverride:
    """`--risk-base` で分母を上書きする（#816）。既定ではない並びを基準に測るための口。"""

    SPECS = risk_specs([1.0])     # mu_only / r2@1.0 / r_macro@1.0

    def test_without_override_the_base_is_the_screen_default(self):
        base, production = resolve_risk_base(["xgb_m2"], self.SPECS)
        assert base == production == risk_base_cond(["xgb_m2"], self.SPECS)

    def test_override_returns_both_names(self):
        base, production = resolve_risk_base(["xgb_m2"], self.SPECS, RISK_NO_AXIS)
        assert base == RISK_NO_AXIS
        assert production == risk_base_cond(["xgb_m2"], self.SPECS)

    def test_override_not_in_conditions_stops(self):
        """分母の無い比較になる。"""
        with pytest.raises(ValueError, match="条件に無い"):
            resolve_risk_base(["xgb_m2"], self.SPECS, "r2@0.3")

    def test_override_tolerates_a_missing_screen_default(self):
        """上書きした run は既定を測らなくてよい（M-1 の既定 r2@1.0 が条件に無い）。"""
        base, production = resolve_risk_base(["risk_return"], risk_specs([0.3]), RISK_NO_AXIS)
        assert (base, production) == (RISK_NO_AXIS, None)
        with pytest.raises(ValueError, match="条件に無い"):
            resolve_risk_base(["risk_return"], risk_specs([0.3]))   # 上書きが無ければ止まる

    def test_verdict_says_base_not_production_when_overridden(self):
        """「production」と書くと画面の既定と取り違える。分母と画面の既定の両方を出す。"""
        v = verdict_text("risk_axis", 3, [], [], base="mu_only",
                         production="r_macro@1.0", overridden=True)
        assert "base ordering (mu_only)" in v
        assert "production" not in v
        assert "screen default r_macro@1.0" in v
        hit = verdict_text("risk_axis", 3, ["M-2(XGBoost)/r2@1.0/rank_ic"], [],
                           base="mu_only", production=None, overridden=True)
        assert "overridden" in hit and "screen default -" in hit

    def test_verdict_without_override_is_unchanged(self):
        v = verdict_text("risk_axis", 7, [], [], base="r_macro@1.0", production="r_macro@1.0")
        assert "production ordering (r_macro@1.0)" in v
        assert "overridden" not in v


class TestMonthEndBound:
    def test_bound_covers_every_day_of_the_month(self):
        """ISO 文字列の上限として使う（実日数を知らなくてもその月の全行が入る）。"""
        assert _month_end_bound("2019-02") == "2019-02-31"
        assert "2019-02-28" <= _month_end_bound("2019-02")
        assert "2019-03-01" > _month_end_bound("2019-02")


class TestRiskByYm:
    def test_r2_is_as_of_and_ignores_later_prices(self):
        """`r2` はその月までの行だけを見る＝未来を見ない。"""
        rows = [_Row(f"2020-{m:02d}-01", 100.0 + m) for m in range(1, 13)]
        rows_with_shock = rows + [_Row("2020-12-28", 1000.0)]
        ids = {"2020-06": ["E1"]}
        a = risk_by_ym("r2", {"E1": rows}, ids, {})
        b = risk_by_ym("r2", {"E1": rows_with_shock}, ids, {})
        assert a[("2020-06", "E1")] == pytest.approx(b[("2020-06", "E1")])

    def test_r_macro_is_time_invariant_per_company(self):
        """**既知の限界**: β も Σ も単一スナップショットなので月で変わらない（#709）。"""
        ids = {"2020-01": ["E1", "E2"], "2020-02": ["E1", "E2"]}
        producer = {"E1": {"r_macro": 0.3}, "E2": {"r_macro": 0.7}}
        got = risk_by_ym("r_macro", {}, ids, producer)
        assert got[("2020-01", "E1")] == got[("2020-02", "E1")] == 0.3
        assert got[("2020-01", "E2")] == got[("2020-02", "E2")] == 0.7

    def test_missing_producer_entries_are_absent_not_zero(self):
        """β が無い社は 0 で埋めない——0 は「マクロリスクが無い」という強い主張になる。"""
        ids = {"2020-01": ["E1", "E2"]}
        got = risk_by_ym("r_macro", {}, ids, {"E1": {"r_macro": 0.3}})
        assert ("2020-01", "E2") not in got

    def test_no_axis_returns_empty(self):
        assert risk_by_ym(None, {}, {"2020-01": ["E1"]}, {}) == {}

    def test_unknown_axis_raises(self):
        with pytest.raises(ValueError, match="未知のリスク軸"):
            risk_by_ym("r99", {}, {}, {})


class TestRiskCommonKeys:
    def test_rows_missing_from_any_axis_are_dropped_before_the_transform(self):
        """全軸で R が取れる行だけへ揃える（ADR-0045: 縮む側が有利に見えるのを防ぐ）。"""
        keys = {("2020-01", "E1"), ("2020-01", "E2")}
        maps = {"mu_only": {}, "r2": {k: 1.0 for k in keys},
                "r_macro": {("2020-01", "E1"): 0.5}}
        assert risk_common_keys(maps, keys) == {("2020-01", "E1")}

    def test_axis_without_risk_does_not_constrain(self):
        keys = {("2020-01", "E1")}
        assert risk_common_keys({"mu_only": {}}, keys) == keys


class TestApplyRiskAxis:
    def _panel(self):
        resid = {"2020-01": [(1.0, 0.1), (2.0, 0.2), (3.0, 0.3)]}
        meta = {"2020-01": [("E1", "a"), ("E2", "b"), ("E3", "c")]}
        return resid, meta

    def test_empty_map_is_the_identity(self):
        """`mu_only` は変換をかけない＝既存6モードが測ってきたものと同じ。"""
        resid, meta = self._panel()
        got_r, got_m, dropped = apply_risk_axis(resid, meta, {}, 1.0)
        assert got_r is resid and got_m is meta and dropped == 0

    def test_risk_is_standardized_within_the_month(self):
        """R は月内 z-score。スケールを10倍しても U は変わらない（軸の差だけを測る）。"""
        resid, meta = self._panel()
        small = {("2020-01", f"E{i}"): float(i) for i in (1, 2, 3)}
        large = {k: v * 10.0 for k, v in small.items()}
        a, _m, _d = apply_risk_axis(resid, meta, small, 1.0)
        b, _m2, _d2 = apply_risk_axis(resid, meta, large, 1.0)
        assert [yh for yh, _y in a["2020-01"]] == pytest.approx(
            [yh for yh, _y in b["2020-01"]])

    def test_u_subtracts_lambda_times_the_zscore(self):
        resid, meta = self._panel()
        rmap = {("2020-01", f"E{i}"): float(i) for i in (1, 2, 3)}
        got, _m, _d = apply_risk_axis(resid, meta, rmap, 2.0)
        # R = 1,2,3 → 標本標準偏差 1.0 → z = -1, 0, +1
        assert [yh for yh, _y in got["2020-01"]] == pytest.approx([3.0, 2.0, 1.0])

    def test_raw_scale_subtracts_lambda_times_the_raw_risk(self):
        """`standardize=False` は画面と同じ U = μ̂ − λR（`--risk-scale raw`・#808）。"""
        resid, meta = self._panel()
        rmap = {("2020-01", f"E{i}"): 0.1 * i for i in (1, 2, 3)}
        got, _m, _d = apply_risk_axis(resid, meta, rmap, 2.0, standardize=False)
        assert [yh for yh, _y in got["2020-01"]] == pytest.approx([0.8, 1.6, 2.4])

    def test_raw_scale_depends_on_the_scale_of_r(self):
        """生の R ではスケールが効く（λ がスライダーと同じ意味を持つ代わりに）。"""
        resid, meta = self._panel()
        small = {("2020-01", f"E{i}"): 0.1 * i for i in (1, 2, 3)}
        large = {k: v * 10.0 for k, v in small.items()}
        a, _m, _d = apply_risk_axis(resid, meta, small, 1.0, standardize=False)
        b, _m2, _d2 = apply_risk_axis(resid, meta, large, 1.0, standardize=False)
        assert [yh for yh, _y in a["2020-01"]] != pytest.approx([yh for yh, _y in b["2020-01"]])

    def test_targets_are_untouched(self):
        """y_true は変換しない（変えたら測っている対象そのものが変わる）。"""
        resid, meta = self._panel()
        rmap = {("2020-01", f"E{i}"): float(i) for i in (1, 2, 3)}
        got, _m, _d = apply_risk_axis(resid, meta, rmap, 1.0)
        assert [y for _yh, y in got["2020-01"]] == pytest.approx([0.1, 0.2, 0.3])

    def test_constant_risk_does_not_tilt(self):
        """月内の R が一定なら傾けない（`r_macro` は定数なので1銘柄の月で起きる）。"""
        resid, meta = self._panel()
        rmap = {("2020-01", f"E{i}"): 5.0 for i in (1, 2, 3)}
        got, _m, _d = apply_risk_axis(resid, meta, rmap, 1.0)
        assert [yh for yh, _y in got["2020-01"]] == pytest.approx([1.0, 2.0, 3.0])

    def test_rows_without_risk_are_counted_and_dropped(self):
        resid, meta = self._panel()
        rmap = {("2020-01", "E1"): 1.0, ("2020-01", "E2"): 2.0}
        got, got_m, dropped = apply_risk_axis(resid, meta, rmap, 1.0)
        assert dropped == 1
        assert len(got["2020-01"]) == len(got_m["2020-01"]) == 2

    def test_meta_stays_aligned_with_residuals(self):
        """`oof_backtest` は index 1:1 で meta を読む（業種中立 IC が壊れる）。"""
        resid, meta = self._panel()
        rmap = {("2020-01", "E1"): 1.0, ("2020-01", "E3"): 3.0}
        _got, got_m, _d = apply_risk_axis(resid, meta, rmap, 1.0)
        assert [m[0] for m in got_m["2020-01"]] == ["E1", "E3"]


class TestQuantileRiskProfile:
    def test_quantile_zero_is_the_lowest_u(self):
        """index 0 = 最低 U（`oof_backtest.quantile_returns` と同じ向き）。"""
        resid = {f"2020-{m:02d}": [(float(i), float(i)) for i in range(10)]
                 for m in range(1, 7)}
        prof = quantile_risk_profile(resid, n_quantiles=5)
        means = [row["mean_return"] for row in prof]
        assert means == sorted(means)

    def test_sharpe_is_mean_over_vol(self):
        resid = {"2020-01": [(float(i), 0.1) for i in range(10)],
                 "2020-02": [(float(i), 0.3) for i in range(10)]}
        prof = quantile_risk_profile(resid, n_quantiles=5)
        row = prof[0]
        assert row["n_periods"] == 2
        assert row["mean_return"] == pytest.approx(0.2)
        assert row["sharpe"] == pytest.approx(row["mean_return"] / row["vol"])

    def test_thin_periods_are_skipped_like_oof_backtest(self):
        """期内サンプルが n_quantiles*2 未満の月は分位を作らない。"""
        resid = {"2020-01": [(float(i), 0.1) for i in range(4)]}
        prof = quantile_risk_profile(resid, n_quantiles=5)
        assert all(row["n_periods"] == 0 for row in prof)

    def test_single_period_has_no_vol_and_no_sharpe(self):
        """1期しか無ければボラは測れない（0 で割らずに None を返す）。"""
        resid = {"2020-01": [(float(i), 0.1) for i in range(10)]}
        prof = quantile_risk_profile(resid, n_quantiles=5)
        assert prof[0]["vol"] is None and prof[0]["sharpe"] is None
