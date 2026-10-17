"""`scripts/momentum_gate.py` の M-2 の行の抽出の割合のモード（`--subsample`・#989・#987）。

M-2 の XGBoost が木ごとに学習行の 0.8 を抽出する（本番・`subsample_0.8`）か、抽出を止める（`subsample_1.0`）かを
共通域で比べる道具。`--valid-split` と同じ形で、危ないのは**黙って同じものを比べる**形:

  - opts が候補まで届かず、両条件とも 0.8 で回る（`walk_forward_cv_monthly` は fit_predict の例外を握る）
  - 分母を本番から取らず書き写す（本番を切り替えたときに旧い割合を本番として測る・#964）
  - 候補 `xgb_m2` と M-2 本体の既定が別々に書かれていて、片方だけ動く

このファイルは本番と候補の既定・候補の印・条件・分母・検定数・opts・届いたかの確認・`main` の配線を縛る。
XGBoost の学習は差し替えて、本物は走らせない（`test_macro_gbdt_valid_split.py` と同じやり方）。
"""
import inspect
import os
import sys
from collections import defaultdict
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_gbdt as mg  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    INNER_CV_PURGE_CONDS, MODE_SUFFIX, MOM_WINDOW, SUBSAMPLE_BASE_COND, SUBSAMPLE_CONDS, SUBSAMPLE_MODELS,
    VALID_SPLIT_CONDS, base_of, bonferroni_alpha, build_conditions, cv_opts, mode_of, panel_cond,
    subsample_reach_problems, verdict_text,
)

GROUPS = [3, 4, 5, 6, 7]


def _train(groups=GROUPS):
    return [([m * 100.0 + i, 1.0, 2.0], 0.01 * (m * 100 + i))
            for m, g in enumerate(groups) for i in range(g)]


_TEST = [([9000.0 + j, 0.0, 0.0], 0.05) for j in range(3)]


def _run_candidate(opts):
    """候補 `xgb_m2` を1 fold だけ回し、(XGBRegressor へ渡った設定の列, 診断値, extra)。学習は差し替える。"""
    import xgboost as xgb
    from plugins.model_candidates import CANDIDATES

    seen: list = []
    real_init = xgb.XGBRegressor.__init__

    def spy_init(self, *a, **kw):
        seen.append(dict(kw))
        real_init(self, *a, **kw)

    diag: dict = defaultdict(list)
    fp, extra = CANDIDATES["xgb_m2"].build([], opts, diag)
    with patch.object(xgb.XGBRegressor, "__init__", spy_init), \
         patch.object(xgb.XGBRegressor, "fit", lambda self, *a, **k: self), \
         patch.object(xgb.XGBRegressor, "predict", lambda self, X: np.zeros(len(X))):
        if extra.get("pass_train_groups"):
            fp(_train(), _TEST, GROUPS)
        else:
            fp(_train(), _TEST)
    return seen, diag, extra


class TestProductionDefaults:
    def test_both_defaults_are_point_eight(self):
        """1.0 へ切り替えるかは #987 が `momentum_gate --subsample` の結果で決める（ここで変えない）。"""
        assert mg._SUBSAMPLE == 0.8
        assert mg._SUBSAMPLE_DEFAULT == 0.8

    def test_screen_default_comes_from_the_production_constant(self, monkeypatch):
        """M-2 本体の `params_schema()` の既定は `_SUBSAMPLE` から取る（書き写すと片方だけ動く）。"""
        assert mg.MacroGbdtPlugin().params_schema()["subsample"]["default"] == mg._SUBSAMPLE
        monkeypatch.setattr(mg, "_SUBSAMPLE", 1.0)
        assert mg.MacroGbdtPlugin().params_schema()["subsample"]["default"] == 1.0

    def test_candidate_default_comes_from_its_constant(self, monkeypatch):
        seen, _diag, _extra = _run_candidate({})
        assert seen[0]["subsample"] == mg._SUBSAMPLE_DEFAULT
        monkeypatch.setattr(mg, "_SUBSAMPLE_DEFAULT", 0.6)
        seen, _diag, _extra = _run_candidate({})
        assert seen[0]["subsample"] == 0.6


class TestCandidateMarks:
    def test_default_rate_leaves_no_mark(self):
        seen, diag, extra = _run_candidate({})
        assert extra == {}
        assert "subsample" not in diag

    def test_other_rate_reaches_xgboost_and_marks_each_fold(self):
        seen, diag, extra = _run_candidate({"subsample": 1.0, "random_state": 7})
        assert extra == {}
        assert seen[0]["subsample"] == 1.0
        assert seen[0]["random_state"] == 7               # 他の opts はそのまま届く
        assert diag["subsample"] == [1.0]

    def test_combines_with_the_valid_split_option(self):
        seen, diag, extra = _run_candidate({"subsample": 1.0, "valid_split": "months"})
        assert extra == {"pass_train_groups": True}
        assert all("valid_split" not in p for p in seen)
        assert diag["subsample"] == [1.0]
        assert diag["valid_split"] == ["months"]

    def test_a_failed_fold_leaves_no_mark(self):
        """例外の fold は `walk_forward_cv_monthly` が捨てる。印が残ると、届いていない fold を届いたと読む。"""
        import xgboost as xgb
        from plugins.model_candidates import CANDIDATES

        def boom(self, *a, **k):
            raise RuntimeError("fit failed")

        diag: dict = defaultdict(list)
        fp, _extra = CANDIDATES["xgb_m2"].build([], {"subsample": 1.0}, diag)
        with patch.object(xgb.XGBRegressor, "fit", boom):
            with pytest.raises(RuntimeError):
                fp(_train(), _TEST)
        assert diag["subsample"] == []


class TestConditions:
    def test_compares_the_two_preregistered_rates(self):
        conds = build_conditions(subsample=True)
        assert list(conds) == list(SUBSAMPLE_CONDS) == ["subsample_0.8", "subsample_1.0"]
        assert {n: c.subsample for n, c in conds.items()} == {"subsample_0.8": 0.8, "subsample_1.0": 1.0}

    def test_other_axes_stay_at_the_production_shape(self):
        """差し替えるのは行の抽出の割合だけ（検証集合も本番の取り方）。本番の側が `Cond` の既定そのもの。"""
        conds = build_conditions(subsample=True)
        assert conds[SUBSAMPLE_BASE_COND] == momentum_gate.Cond(False, MOM_WINDOW)
        for c in conds.values():
            assert c._replace(subsample=mg._SUBSAMPLE) == conds[SUBSAMPLE_BASE_COND]
            assert c.valid_split == mg._VALID_SPLIT

    def test_both_sides_share_one_panel(self):
        conds = build_conditions(subsample=True)
        assert len({panel_cond(c) for c in conds.values()}) == 1
        assert len(set(conds.values())) == 2          # CV は条件ごとに回る

    def test_baseline_is_the_production_rate_of_m2(self):
        conds = build_conditions(subsample=True)
        assert base_of(conds) == SUBSAMPLE_BASE_COND == "subsample_0.8"
        assert SUBSAMPLE_CONDS[SUBSAMPLE_BASE_COND] == mg._SUBSAMPLE

    def test_stops_when_production_is_not_a_registered_rate(self, monkeypatch):
        """本番が 0.8 / 1.0 のどちらでもなくなったら止める——黙って別の割合を分母にすると別物を比べる。"""
        monkeypatch.setattr(momentum_gate, "_M2_SUBSAMPLE", 0.6)
        monkeypatch.setattr(momentum_gate, "SUBSAMPLE_BASE_COND", "subsample_0.6")
        with pytest.raises(ValueError, match="分母"):
            build_conditions(subsample=True)

    def test_names_do_not_collide_with_the_other_cv_modes(self):
        assert not set(SUBSAMPLE_CONDS) & set(VALID_SPLIT_CONDS)
        assert not set(SUBSAMPLE_CONDS) & set(INNER_CV_PURGE_CONDS)

    def test_measures_m2_only(self):
        assert SUBSAMPLE_MODELS == ["xgb_m2"]
        build_conditions(subsample=True, models=["xgb_m2"])
        for models in (["elasticnet"], ["xgb_m2", "elasticnet"], ["risk_return"]):
            with pytest.raises(ValueError, match="M-2"):
                build_conditions(subsample=True, models=models)

    def test_alpha_is_two_tests(self):
        conds = build_conditions(subsample=True)
        assert bonferroni_alpha(len(SUBSAMPLE_MODELS), len(conds)) == pytest.approx(0.025)


class TestOptions:
    def test_only_the_m2_non_default_side_gets_options(self):
        conds = build_conditions(subsample=True)
        # 行の抽出の opts は既定以外の側だけ。M-2 には本番の検証集合（#969 で months）も渡るので、
        # opts 全体ではなく `subsample` の鍵で見る
        assert "subsample" not in (cv_opts(conds["subsample_0.8"], "xgb_m2") or {})
        assert (cv_opts(conds["subsample_1.0"], "xgb_m2") or {}).get("subsample") == 1.0
        assert "subsample" not in (cv_opts(conds["subsample_1.0"], "elasticnet") or {})
        assert cv_opts(conds["subsample_1.0"], "risk_return") is None

    def test_both_cv_options_are_passed_together(self):
        c = momentum_gate.Cond(False, MOM_WINDOW, valid_split="months", subsample=1.0)
        assert cv_opts(c, "xgb_m2") == {"valid_split": "months", "subsample": 1.0}

    @pytest.mark.parametrize("kwargs", [dict(subsample=True), dict(), dict(valid_split=True),
                                        dict(fin_fill=True)])
    def test_every_condition_reaches_the_candidate_with_its_own_rate(self, kwargs):
        """各条件の opts を候補へ渡したとき、候補が回る割合＝条件の `subsample`。

        **opts を省くかを本番の値と比べると、本番を切り替えた日に落ちる**（#964 と同じ理由）。
        """
        for c in build_conditions(**kwargs).values():
            opts = cv_opts(c, "xgb_m2") or {}
            assert opts.get("subsample", mg._SUBSAMPLE_DEFAULT) == c.subsample

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(interactions=True),
        dict(max_features=[5, 20]), dict(fin_rows=True), dict(demean_target=True),
        dict(risk_axis=True, models=["xgb_m2"]), dict(fin_nan=True), dict(de_exclude=True),
        dict(fin_fill=True), dict(inner_cv_purge=True), dict(size_neutral=True), dict(valid_split=True),
    ])
    def test_existing_modes_keep_the_production_rate(self, kwargs):
        """既存モードの M-2 は本番の割合のまま（`subsample` の opts を渡さない）。"""
        for c in build_conditions(**kwargs).values():
            assert c.subsample == mg._SUBSAMPLE
            assert "subsample" not in (cv_opts(c, "xgb_m2") or {})


class TestReachCheck:
    def test_reach_check(self):
        conds = build_conditions(subsample=True)
        healthy = {"subsample_0.8|xgb_m2": {"best_iteration": 80},
                   "subsample_1.0|xgb_m2": {"subsample": 1.0}}
        assert subsample_reach_problems(healthy, conds) == []
        lost = {"subsample_0.8|xgb_m2": {"best_iteration": 80},
                "subsample_1.0|xgb_m2": {"best_iteration": 80}}
        assert subsample_reach_problems(lost, conds) == [
            "subsample_1.0|xgb_m2: 行の抽出が 0.8 で回った（指定は 1.0）"]
        # 全 fold が落ちて診断値が空でも、届いたとはみなさない
        assert subsample_reach_problems({"subsample_1.0|xgb_m2": None}, conds)


class TestOutput:
    def test_mode_and_suffix(self):
        assert mode_of(subsample=True) == "subsample"
        assert MODE_SUFFIX["subsample"] == "_subsample"

    def test_verdict_names_the_axis(self):
        v = verdict_text("subsample", 2, [], [])
        assert "SUBSAMPLE AXIS" in v and "M-2 only" in v
        v.encode("cp932")

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ割合になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("subsample=args.subsample") == 2               # build_conditions / mode_of
        assert src.count("opts=cv_opts(conds[cond], model)") == 2       # 全月と共通月の CV
        assert src.count("subsample_reach_problems(") == 2              # 全月と共通月の確認
        assert "SUBSAMPLE_MODELS" in src
        assert "subsample={c.subsample}" in src
        assert '"subsample": c.subsample' in src                         # JSON の conditions
