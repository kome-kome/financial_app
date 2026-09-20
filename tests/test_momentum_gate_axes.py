"""`scripts/momentum_gate.py` の交互作用モード・列数モード（Issue #615）。

#604 の実測で、M-1 のマクロ特徴量は共通 (ym,ec) 域で rank-IC を **−0.0920**
（95%CI [−0.1448, −0.0448]）下げていた。母集団は完全同一なので特徴量そのものの効果だが、
`use_macro` は**主効果と交差項を同時に動かす**ため、悪化しているのがどちらかは分からない。
M-1 は `build_interactions=True` で財務 × マクロの交差項を作り、実測では
`max_features=20` の上限いっぱいまで選ばれていた（`nomacro` は4列）。

このファイルが縛るのは**既存モードを壊さずに軸を2つ足した**部分:

  - 6モード（既定を除く）が互いに排他であること（母集団を動かしうる軸を2つ振ると分離できない）
  - 目的変数の月平均除去（`--demean-target`・#615 の 9/18 の見立て）の変換と、判定指標が
    月ごとの平行移動で変わらないという前提
  - 各モードの分母が正しいこと（縮む側を分母にすると母集団効果が改善に化ける）
  - `_build` が Cond の値を実際に使い、**M-2 では交互作用が入らない**こと
  - 既定モードの条件が1ビットも動いていないこと（ADR-0045 の実測条件）
  - 出力（判定行・JSON の `mode`）が**測ったモードの名前**を出すこと

パネル構築の本体は DB フルロードが要るのでここでは触らない（既存の窓モードテストと同じ）。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    BASE_COND, CONDS, DEMEAN_BASE_COND, DEMEAN_CONDS, DEMEAN_MODELS, INTERACTION_BASE_COND,
    INTERACTION_CONDS, INTERACTION_MODELS, MACRO_BASE_COND, MAXFEAT_MODELS, METRICS,
    MODE_SUFFIX, MOM_WINDOW, Cond, base_of, bonferroni_alpha, build_conditions,
    demean_reach_problems, demean_target_by_month, max_abs_month_mean, maxfeat_cond_name,
    mode_of, verdict_text,
)


class TestModesAreMutuallyExclusive:
    """**母集団を動かしうる軸を2つ同時に振ると、共通域へ制限しても分離できない。**

    これは #592/#604 が指摘している当のもので、測る側で再現しては意味がない。
    `max_features` は ADR-0050 の実測で母集団を動かさないと確定しているが、それでも
    併用させない——動かさないのは現行パネルでの実測であって、データが伸びれば変わりうる。
    """

    PAIRS = [
        (dict(windows=[3], macro=True)),
        (dict(windows=[3], interactions=True)),
        (dict(windows=[3], max_features=[5])),
        (dict(macro=True, interactions=True)),
        (dict(macro=True, max_features=[5])),
        (dict(interactions=True, max_features=[5])),
        # 行の基準モード（#424 子3）も同じ扱い。TTM は as-of で母集団を動かしうる。
        (dict(windows=[3], fin_rows=True)),
        (dict(macro=True, fin_rows=True)),
        (dict(interactions=True, fin_rows=True)),
        (dict(max_features=[5], fin_rows=True)),
        # 目的変数モード（#615）も同じ扱い。行は落とさないが、軸を2つ振ると分離できない点は同じ。
        (dict(windows=[3], demean_target=True)),
        (dict(macro=True, demean_target=True)),
        (dict(interactions=True, demean_target=True)),
        (dict(max_features=[5], demean_target=True)),
        (dict(fin_rows=True, demean_target=True)),
    ]

    @pytest.mark.parametrize("kwargs", PAIRS)
    def test_two_modes_at_once_are_rejected(self, kwargs):
        with pytest.raises(ValueError):
            build_conditions(**kwargs)

    def test_the_error_names_both_modes(self):
        """どちらを外せばよいか分かる形にする（片方だけ書くと外し方が伝わらない）。"""
        with pytest.raises(ValueError) as e:
            build_conditions(interactions=True, max_features=[5])
        assert "--interactions" in str(e.value) and "--max-features" in str(e.value)


class TestInteractionMode:
    def test_builds_both_sides(self):
        conds = build_conditions(interactions=True)
        assert set(conds) == set(INTERACTION_CONDS)
        assert conds["nointer"].build_interactions is False
        assert conds["inter"].build_interactions is True

    def test_momentum_is_off_and_macro_is_on(self):
        """マクロを切ると交互作用の相手が消えて軸そのものが無くなる。"""
        for c in build_conditions(interactions=True).values():
            assert c.use_momentum is False
            assert c.use_macro is True
            assert c.momentum_window == MOM_WINDOW

    def test_baseline_is_the_side_without_interactions(self):
        """列が少ない側が母集団の広い側（strict は列が増えるほど破棄に当たりやすい）。"""
        conds = build_conditions(interactions=True)
        assert base_of(conds) == INTERACTION_BASE_COND
        assert conds[base_of(conds)].build_interactions is False

    def test_measures_m1_only(self):
        """M-2/M-6 は `build_interactions=False` が本番構成＝振る意味が無い。"""
        assert INTERACTION_MODELS == ["risk_return"]

    def test_alpha_matches_a_two_condition_single_model_gate(self):
        conds = build_conditions(interactions=True)
        assert bonferroni_alpha(1, len(conds)) == pytest.approx(0.05 / (1 * len(METRICS) * 1))


class TestMaxFeaturesMode:
    def test_conditions_are_sorted_and_deduplicated(self):
        conds = build_conditions(max_features=[20, 5, 20, 10])
        assert list(conds) == [maxfeat_cond_name(n) for n in (5, 10, 20)]

    def test_each_condition_carries_its_own_limit(self):
        conds = build_conditions(max_features=[5, 40])
        assert conds["mf5"].max_features == 5
        assert conds["mf40"].max_features == 40

    def test_other_axes_stay_at_the_production_shape(self):
        for c in build_conditions(max_features=[5, 20]).values():
            assert c.use_momentum is False
            assert c.use_macro is True
            assert c.build_interactions is True

    def test_baseline_is_the_production_value(self):
        """**母集団が動かない軸なので「広い側」の原則が使えない。**

        代わりに本番値を分母に置き、「本番から動かすとどうなるか」を見る形にする。
        """
        conds = build_conditions(max_features=[5, 10, 20, 30, 40])
        assert base_of(conds, 20) == "mf20"

    def test_baseline_falls_back_to_the_smallest_when_production_is_absent(self):
        """本番値を条件に入れ忘れても落ちない（母集団は同じなのでどれでも比較は成立する）。"""
        conds = build_conditions(max_features=[10, 30])
        assert base_of(conds, 20) == "mf10"

    @pytest.mark.parametrize("bad", [[0], [-5], [3, 0]])
    def test_non_positive_limit_is_rejected(self, bad):
        with pytest.raises(ValueError):
            build_conditions(max_features=bad)

    def test_alpha_tightens_with_more_conditions(self):
        """条件を増やしたぶんの多重比較を補正する（5値なら基準以外が4つ）。"""
        conds = build_conditions(max_features=[5, 10, 20, 30, 40])
        assert bonferroni_alpha(1, len(conds)) == pytest.approx(0.05 / (1 * len(METRICS) * 4))

    def test_measures_m1_only(self):
        """BIC の列数上限を持つのは M-1 だけ。"""
        assert MAXFEAT_MODELS == ["risk_return"]


class TestDemeanTargetMode:
    """目的変数モード（`--demean-target`・#615）。

    9/18 の列数上限の本測定で「上限が財務の列を締め出している」は否定され、残った見立ては
    「BIC（二乗誤差）は相場全体の変動を説明するマクロ列を選ぶが、評価は月内の順位」だった。
    目的変数だけを差し替えた2条件で、その見立てを測る。
    """

    def test_builds_both_sides(self):
        conds = build_conditions(demean_target=True)
        assert set(conds) == set(DEMEAN_CONDS) == {"raw", "demean"}
        assert conds["raw"].demean_target is False
        assert conds["demean"].demean_target is True

    def test_other_axes_stay_at_the_production_shape(self):
        """差し替えるのは目的変数だけ。他の軸が動くと2軸ぶんの差が混ざる。"""
        for c in build_conditions(demean_target=True).values():
            assert c.use_momentum is False
            assert c.momentum_window == MOM_WINDOW
            assert c.use_macro is True
            assert c.build_interactions is True
            assert c.max_features is None
            assert c.fin_rows == "annual"

    def test_baseline_is_the_production_target(self):
        conds = build_conditions(demean_target=True)
        assert base_of(conds) == DEMEAN_BASE_COND == "raw"
        assert conds[base_of(conds)].demean_target is False

    def test_measures_m1_only_by_default(self):
        """見立ては BIC 選択を持つ M-1 のもの。"""
        assert DEMEAN_MODELS == ["risk_return"]

    def test_alpha_matches_a_two_condition_single_model_gate(self):
        conds = build_conditions(demean_target=True)
        assert bonferroni_alpha(1, len(conds)) == pytest.approx(0.05 / (1 * len(METRICS) * 1))


def _panel():
    """{ym: [(特徴量の行, 目的変数)]}。月ごとに平均が違う（相場全体の上げ下げがある）形。"""
    return {
        "2024-01": [([1.0, 2.0], 0.30), ([3.0, 4.0], 0.10), ([5.0, 6.0], 0.20)],
        "2024-02": [([7.0, 8.0], -0.40), ([9.0, 1.0], -0.20)],
        "2024-03": [([2.0, 2.0], 0.05)],
        "2024-04": [],
    }


class TestDemeanTargetByMonth:
    def test_every_month_has_zero_mean(self):
        out = demean_target_by_month(_panel())
        for ym, pairs in out.items():
            if pairs:
                assert sum(t for _r, t in pairs) / len(pairs) == pytest.approx(0.0, abs=1e-15)

    def test_values_are_the_monthly_mean_subtracted(self):
        out = demean_target_by_month(_panel())
        assert [t for _r, t in out["2024-01"]] == pytest.approx([0.10, -0.10, 0.0])
        assert [t for _r, t in out["2024-02"]] == pytest.approx([-0.10, 0.10])

    def test_order_and_feature_rows_are_kept(self):
        """`ids_by_ym` との index 1:1 対応が崩れると共通域の突合が静かに壊れる。"""
        src = _panel()
        out = demean_target_by_month(src)
        assert list(out) == list(src)
        for ym in src:
            assert [r for r, _t in out[ym]] == [r for r, _t in src[ym]]

    def test_input_is_not_mutated(self):
        """raw の条件と同じオブジェクトを持っていても壊さない。"""
        src = _panel()
        before = {ym: [(list(r), t) for r, t in pairs] for ym, pairs in src.items()}
        demean_target_by_month(src)
        assert src == before

    def test_single_sample_month_becomes_zero_and_empty_month_stays_empty(self):
        out = demean_target_by_month(_panel())
        assert [t for _r, t in out["2024-03"]] == [0.0]
        assert out["2024-04"] == []

    def test_max_abs_month_mean(self):
        assert max_abs_month_mean(_panel()) == pytest.approx(0.30)   # 2024-02 の平均 −0.30
        assert max_abs_month_mean(demean_target_by_month(_panel())) == pytest.approx(0.0, abs=1e-15)
        assert max_abs_month_mean({}) == 0.0
        assert max_abs_month_mean({"2024-01": []}) == 0.0


class TestDemeanReachIsChecked:
    """**変換が断面に届いていなければ CV の前に止める。**

    両条件が同じパネルのまま走っても例外は出ず、「差なし」だけが残る。`count_changed_rows` は
    特徴量しか見ないので、目的変数の差し替えは拾えない。
    """

    CONDS = build_conditions(demean_target=True)

    def test_healthy_panels_pass(self):
        assert demean_reach_problems({"raw|m1": 0.12, "demean|m1": 1e-17}, self.CONDS) == []

    def test_demean_side_that_was_not_demeaned_is_caught(self):
        problems = demean_reach_problems({"raw|m1": 0.12, "demean|m1": 0.12}, self.CONDS)
        assert len(problems) == 1 and problems[0].startswith("demean|m1")

    def test_raw_side_with_nothing_to_remove_is_caught(self):
        """素の月平均が全部0なら、変換しても何も変わらない＝同じものを比べている。"""
        problems = demean_reach_problems({"raw|m1": 0.0, "demean|m1": 0.0}, self.CONDS)
        assert len(problems) == 1 and problems[0].startswith("raw|m1")


class TestDemeanMainWiring:
    def test_main_passes_the_flag_and_checks_the_reach(self):
        """**渡し忘れても例外は出ない**（両条件が素の目的変数で組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("demean_target=args.demean_target") == 2   # build_conditions / mode_of
        assert "c.demean_target)" in src                              # _build へ渡す
        assert "demean_reach_problems(" in src
        assert "DEMEAN_MODELS" in src


class TestEvaluationIsInvariantToMonthlyShift:
    """**評価側の y を素へ戻さなくてよい、という前提を縛る。**

    demean 条件では CV の残差が持つ y_true も月平均を引いた値になる。判定の3指標は月の中で
    完結する（rank-IC＝月ごとの Spearman・long_short＝top−bottom・short_side＝期内全体平均−bottom）
    ので、月ごとの平行移動で変わらない。`oof_backtest` がこの性質を失えば（例: 期をまたいで
    プールした指標へ変わる）、demean 条件の判定は raw と別の物差しで測ることになる。
    """

    @staticmethod
    def _residuals(shift_by_ym: dict[str, float]) -> dict:
        import random
        rng = random.Random(0)
        out = {}
        for ym, shift in shift_by_ym.items():
            pairs = []
            for _ in range(25):
                yhat = rng.gauss(0.0, 1.0)
                pairs.append((yhat, 0.3 * yhat + rng.gauss(0.0, 1.0) + shift))
            out[ym] = pairs
        return out

    def test_decision_metrics_do_not_move(self):
        from plugins.macro_snapshots import oof_backtest
        yms = ["2023-01", "2023-04", "2023-07", "2023-10"]
        raw = oof_backtest(self._residuals({ym: 0.0 for ym in yms}), n_quantiles=5)
        shifted = oof_backtest(
            self._residuals(dict(zip(yms, [0.8, -1.5, 0.05, 3.0]))), n_quantiles=5)
        assert shifted["rank_ic"]["mean"] == pytest.approx(raw["rank_ic"]["mean"], abs=2e-6)
        for key in ("short_side_spread", "long_short_spread"):
            assert shifted[key] == pytest.approx(raw[key], abs=2e-6), key
        for key in ("rank_ic_by_period", "short_side_spread_by_period"):
            assert shifted[key].keys() == raw[key].keys()
            for ym in raw[key]:
                assert shifted[key][ym] == pytest.approx(raw[key][ym], abs=2e-6), (key, ym)


class TestExistingModesAreUnchanged:
    """**ADR-0045 / ADR-0050 の実測条件を1ビットも動かさない。**

    新しい軸の既定値（`build_interactions=True` / `max_features=None`）は本番構成なので、
    既存モードの条件は以前と同じ意味でなければならない。
    """

    def test_default_gate_still_has_two_conditions(self):
        conds = build_conditions()
        assert set(conds) == set(CONDS)
        assert base_of(conds) == BASE_COND

    def test_default_conditions_carry_the_production_shape_on_the_new_axes(self):
        for c in build_conditions().values():
            assert c.build_interactions is True
            assert c.max_features is None

    def test_macro_mode_conditions_are_unchanged(self):
        conds = build_conditions(macro=True)
        assert base_of(conds) == MACRO_BASE_COND
        for c in conds.values():
            assert c.build_interactions is True
            assert c.max_features is None

    def test_windows_mode_conditions_are_unchanged(self):
        conds = build_conditions(windows=[3, 12])
        assert list(conds) == [BASE_COND, "mw3", "mw12"]
        for c in conds.values():
            assert c.build_interactions is True
            assert c.max_features is None

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(interactions=True),
        dict(max_features=[5, 20]),
    ])
    def test_existing_modes_read_annual_rows_only(self, kwargs):
        """行の基準（#424 子3）の既定は本番の構成＝通期のみ。既存モードは TTM を読まない。"""
        for c in build_conditions(**kwargs).values():
            assert c.fin_rows == "annual"

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(interactions=True),
        dict(max_features=[5, 20]), dict(fin_rows=True),
    ])
    def test_existing_modes_keep_the_raw_target(self, kwargs):
        """目的変数（#615）の既定は本番の構成＝素の52週先リターン。既存モードは変換しない。"""
        for c in build_conditions(**kwargs).values():
            assert c.demean_target is False


class TestBuildUsesTheConditionAxes:
    """`_build` が Cond の値を実際に使うこと。

    **渡し忘れても例外は出ず、それらしい rank-IC が返る**（本番構成のまま測ってしまい、
    条件間に差が出ないので「効かなかった」という誤った結論になる）。だから引数の受け渡しを
    シグネチャと呼び出しの両方で縛る。
    """

    def test_build_accepts_both_new_axes(self):
        sig = inspect.signature(momentum_gate._build).parameters
        assert "build_interactions" in sig
        assert "max_features" in sig

    def test_m2_never_gets_interactions_even_when_the_condition_asks(self, monkeypatch):
        """M-2/M-6 は交互作用なしが本番構成。条件が True でも入れてはいけない。"""
        seen = {}

        def fake_build_snapshots(*a, **k):
            seen.update(k)
            return {}, {}, None, [], {}

        monkeypatch.setattr(momentum_gate, "build_snapshots", fake_build_snapshots)
        monkeypatch.setattr(momentum_gate, "_thin", lambda s, m, i, stride: (s, m, i))
        momentum_gate._build("gbdt", _Args(), {}, {}, [], {},
                             use_momentum=False, mom_window=12, use_macro=True,
                             build_interactions=True, max_features=None)
        assert seen["build_interactions"] is False

    def test_m1_passes_the_condition_through(self, monkeypatch):
        seen = {}

        def fake_build_snapshots(*a, **k):
            seen.update(k)
            return {}, {}, None, [], {}

        monkeypatch.setattr(momentum_gate, "build_snapshots", fake_build_snapshots)
        monkeypatch.setattr(momentum_gate, "_thin", lambda s, m, i, stride: (s, m, i))
        monkeypatch.setattr(momentum_gate, "_select_bic",
                            lambda s, f, max_features: (s, [f"sel{max_features}"]))
        _, _, _, feats = momentum_gate._build(
            "m1", _Args(), {}, {}, [], {},
            use_momentum=False, mom_window=12, use_macro=True,
            build_interactions=False, max_features=7)
        assert seen["build_interactions"] is False
        assert feats == ["sel7"], "max_features が _select_bic へ渡っていない"

    def test_m1_falls_back_to_the_plugin_default_limit(self, monkeypatch):
        """`max_features=None` は「プラグインの既定に従う」＝数値を書き写さない。"""
        seen = {}
        monkeypatch.setattr(momentum_gate, "build_snapshots",
                            lambda *a, **k: ({}, {}, None, [], {}))
        monkeypatch.setattr(momentum_gate, "_thin", lambda s, m, i, stride: (s, m, i))
        monkeypatch.setattr(momentum_gate, "_select_bic",
                            lambda s, f, max_features: (seen.setdefault("mf", max_features), []))
        momentum_gate._build("m1", _Args(), {}, {}, [], {},
                             use_momentum=False, mom_window=12, use_macro=True,
                             build_interactions=True, max_features=None)
        from plugins.utils import coerce_params
        from plugins import get_plugin
        want = coerce_params(get_plugin("macro_risk_return").params_schema(), {})["max_features"]
        assert seen["mf"] == want

    def test_macro_axis_does_not_follow_the_production_default(self, monkeypatch):
        """`use_macro` の ON/OFF を持つのは条件側だけで、本番の既定は見ない（#615）。

        `macro_names_for` が `params_schema()` の `use_macro` を読んでいた間は、本番の既定が
        OFF になった瞬間に `--macro` ゲートの **`macro` 側まで空**になり、両側が同一条件に
        なって「差なし」だけが残る形だった（ADR-0050 の「黙って同じものを比べる」）。
        数値はもっともらしく出るので**失敗として現れない**。構造ごと縛る。

        `max_features` には既定追従を縛るテストが上にあるが、`use_macro` には無かった。
        """
        from plugins import get_plugin
        from plugins.utils import coerce_params

        seen = {}

        def fake_build_snapshots(prices, fin, cos, cache, fin_features, macro_names,
                                 *a, **k):
            seen.setdefault("calls", []).append(list(macro_names))
            return ({}, {}, None, [], {})

        monkeypatch.setattr(momentum_gate, "build_snapshots", fake_build_snapshots)
        monkeypatch.setattr(momentum_gate, "_thin", lambda s, m, i, stride: (s, m, i))
        monkeypatch.setattr(momentum_gate, "_select_bic", lambda s, f, max_features: (s, []))

        for on in (True, False):
            momentum_gate._build("m1", _Args(), {}, {}, [], {},
                                 use_momentum=False, mom_window=12, use_macro=on,
                                 build_interactions=True, max_features=None)

        on_names, off_names = seen["calls"]
        assert on_names, (
            "use_macro=True の条件でマクロ系列が空になった。`macro_names_for` が本番の"
            "既定に追従していないか確認すること（#615）"
        )
        assert off_names == [], "use_macro=False の条件にマクロ系列が漏れている"
        assert on_names != off_names, "両側が同じ条件になっている（測っても差は出ない）"

        # この縛りが要る理由そのもの: 本番の既定はいま OFF である。
        prod = coerce_params(get_plugin("macro_risk_return").params_schema(), {})
        assert prod["use_macro"] is False

    @pytest.mark.parametrize("demean, want_mean_zero", [(True, True), (False, False)])
    def test_demean_reaches_the_bic_selection(self, monkeypatch, demean, want_mean_zero):
        """**変換は BIC 選択の前に掛かる**（#615）。選択に届かないと「選ばれる列が変わるか」を
        測れず、学習だけに掛かっても見立ての半分しか測れない。"""
        panel = _panel()
        seen = {}
        monkeypatch.setattr(momentum_gate, "build_snapshots",
                            lambda *a, **k: (panel, {}, None, ["a", "b"], {}))
        monkeypatch.setattr(momentum_gate, "_thin", lambda s, m, i, stride: (s, m, i))

        def fake_select(s, f, max_features):
            seen["s"] = s
            return s, f

        monkeypatch.setattr(momentum_gate, "_select_bic", fake_select)
        s, _, _, _ = momentum_gate._build(
            "m1", _Args(), {}, {}, [], {},
            use_momentum=False, mom_window=12, use_macro=True,
            build_interactions=True, max_features=None, demean_target=demean)
        assert seen["s"] is s
        assert (max_abs_month_mean(seen["s"]) < 1e-12) is want_mean_zero
        if not demean:
            assert seen["s"] is panel, "raw 条件でパネルを作り替えている"

    def test_build_accepts_the_target_axis(self):
        sig = inspect.signature(momentum_gate._build).parameters
        assert "demean_target" in sig
        assert sig["demean_target"].default is False


class TestOutputNamesTheModeThatWasMeasured:
    """**出力は測ったモードの名前を出す。**

    2026-09-15 の `--interactions` 本測定は、JSON の `mode` が `"default"`、判定行が
    `REJECT (keep default use_momentum=False)` だった——測ったのは交互作用なのに、
    モメンタムの昇格ゲートの結果に見える。列数モード（5条件）なら `WINDOW SCAN: no window
    beat the no-momentum baseline` と出ていた。**どちらも例外は出ず、数値は正しい**ので、
    読み違いは ADR や Issue へ書き写すときにしか起きない。
    """

    @pytest.mark.parametrize("kwargs, want", [
        (dict(), "default"),
        (dict(windows=[3, 12]), "windows"),
        (dict(macro=True), "macro"),
        (dict(interactions=True), "interactions"),
        (dict(max_features=[5, 20]), "max_features"),
        (dict(fin_rows=True), "fin_rows"),
        (dict(demean_target=True), "demean_target"),
    ])
    def test_mode_follows_the_flag(self, kwargs, want):
        assert mode_of(**kwargs) == want

    def test_every_mode_has_a_file_suffix(self):
        assert set(MODE_SUFFIX) == {"default", "windows", "macro", "interactions", "max_features",
                                    "fin_rows", "demean_target"}

    def test_demean_mode_writes_its_own_file(self):
        """既定の `momentum_gate.json` を上書きしない（どの軸の結果かがファイル名で分かる）。"""
        suffixes = [v for k, v in MODE_SUFFIX.items() if k != "windows"]
        assert MODE_SUFFIX["demean_target"] == "_demean"
        assert len(suffixes) == len(set(suffixes))

    def test_existing_output_files_do_not_move(self):
        """過去の結果の置き場所を変えない（窓モードは #592 以来、既定と同じファイル）。"""
        assert MODE_SUFFIX["default"] == MODE_SUFFIX["windows"] == ""
        assert MODE_SUFFIX["macro"] == "_macro"

    @pytest.mark.parametrize("mode, n_conds, head", [
        ("interactions", 2, "INTERACTIONS AXIS"),
        ("max_features", 5, "MAX_FEATURES SCAN"),
        ("max_features", 2, "MAX_FEATURES SCAN"),
        ("fin_rows", 2, "FIN ROWS AXIS"),
        ("demean_target", 2, "DEMEAN TARGET AXIS"),
    ])
    def test_new_modes_do_not_borrow_the_momentum_wording(self, mode, n_conds, head):
        for passed, regressed in (([], []), (["x"], []), ([], ["y"])):
            v = verdict_text(mode, n_conds, passed, regressed)
            assert v.startswith(head + ":"), v
            assert "use_momentum" not in v and "window" not in v, v

    def test_regressions_are_reported_on_axis_modes(self):
        v = verdict_text("max_features", 5, [], ["M-1(RiskReturn)/mf40/rank_ic"])
        assert v.endswith("| significantly WORSE: M-1(RiskReturn)/mf40/rank_ic")

    @pytest.mark.parametrize("mode, n_conds, passed, regressed, want", [
        ("default", 2, [], [],
         "REJECT (keep default use_momentum=False): no metric passed corrected alpha"),
        ("default", 2, ["a"], [], "PROMOTE (default use_momentum=True): a"),
        ("default", 2, [], ["b"],
         "REJECT (keep default use_momentum=False): no improvement passed corrected alpha; "
         "significantly WORSE on b"),
        # `--windows 12` は2条件＝既定ゲートと同じ文言（切り出す前からの挙動）
        ("windows", 2, [], [],
         "REJECT (keep default use_momentum=False): no metric passed corrected alpha"),
        ("windows", 3, [], [],
         "WINDOW SCAN: no window beat the no-momentum baseline on the common (ym,ec) domain "
         "at the corrected alpha"),
        ("windows", 3, ["a"], ["b"],
         "WINDOW SCAN: effects that survive the common-domain restriction: a"
         " | significantly WORSE: b"),
        ("macro", 2, [], [],
         "MACRO AXIS: use_macro did not beat the no-macro baseline on the common (ym,ec) "
         "domain at the corrected alpha"),
    ])
    def test_existing_wording_is_unchanged(self, mode, n_conds, passed, regressed, want):
        """既存3モードの文言は1文字も変えない（過去のログ・JSON と突き合わせるため）。"""
        assert verdict_text(mode, n_conds, passed, regressed) == want

    @pytest.mark.parametrize("mode", sorted(MODE_SUFFIX))
    def test_verdicts_survive_cp932(self, mode):
        """日中枠のログはリダイレクト先へ書かれる（cp932 で落ちる記号を使わない）。"""
        verdict_text(mode, 5, ["a"], ["b"]).encode("cp932")


class _Args:
    """`_build` が読むのは `stride` だけ（argparse.Namespace の代わり）。"""
    stride = 1
