"""`scripts/momentum_gate.py` の交互作用モード・列数モード（Issue #615）。

#604 の実測で、M-1 のマクロ特徴量は共通 (ym,ec) 域で rank-IC を **−0.0920**
（95%CI [−0.1448, −0.0448]）下げていた。母集団は完全同一なので特徴量そのものの効果だが、
`use_macro` は**主効果と交差項を同時に動かす**ため、悪化しているのがどちらかは分からない。
M-1 は `build_interactions=True` で財務 × マクロの交差項を作り、実測では
`max_features=20` の上限いっぱいまで選ばれていた（`nomacro` は4列）。

このファイルが縛るのは**既存モードを壊さずに軸を2つ足した**部分:

  - 4モードが互いに排他であること（母集団を動かしうる軸を2つ振ると分離できない）
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
    BASE_COND, CONDS, INTERACTION_BASE_COND, INTERACTION_CONDS, INTERACTION_MODELS,
    MACRO_BASE_COND, MAXFEAT_MODELS, METRICS, MODE_SUFFIX, MOM_WINDOW, Cond,
    base_of, bonferroni_alpha, build_conditions, maxfeat_cond_name, mode_of, verdict_text,
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
    ])
    def test_mode_follows_the_flag(self, kwargs, want):
        assert mode_of(**kwargs) == want

    def test_every_mode_has_a_file_suffix(self):
        assert set(MODE_SUFFIX) == {"default", "windows", "macro", "interactions", "max_features",
                                    "fin_rows"}

    def test_existing_output_files_do_not_move(self):
        """過去の結果の置き場所を変えない（窓モードは #592 以来、既定と同じファイル）。"""
        assert MODE_SUFFIX["default"] == MODE_SUFFIX["windows"] == ""
        assert MODE_SUFFIX["macro"] == "_macro"

    @pytest.mark.parametrize("mode, n_conds, head", [
        ("interactions", 2, "INTERACTIONS AXIS"),
        ("max_features", 5, "MAX_FEATURES SCAN"),
        ("max_features", 2, "MAX_FEATURES SCAN"),
        ("fin_rows", 2, "FIN ROWS AXIS"),
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
