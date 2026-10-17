"""`scripts/momentum_gate.py` の M-2 の検証集合のモード（`--valid-split`・#976・#969）。

M-2 の early stopping の検証集合を、末尾の月を丸ごと取る（本番・`valid_months`・#969）か末尾の行で取る（`valid_rows`）かを
共通域で比べる道具。`--inner-cv` と同じ形で、危ないのは**黙って同じものを比べる**形:

  - opts が候補まで届かず、両条件とも行で取る（`walk_forward_cv_monthly` は fit_predict の例外を握るので、
    `train_groups` の渡し忘れも失敗として現れない）
  - 分母を本番から取らず書き写す（本番を切り替えたときに旧い方式を本番として測る・#964）
  - 条件名が `--inner-cv` と同じで、分母を名前で引く `base_of` が他方の本番を返す

このファイルは条件・分母・検定数・opts・届いたかの確認・`main` の配線を縛る。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_gbdt as mg  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    INNER_CV_PURGE_CONDS, MODE_SUFFIX, MOM_WINDOW, VALID_SPLIT_BASE_COND, VALID_SPLIT_CONDS,
    VALID_SPLIT_MODELS, base_of, bonferroni_alpha, build_conditions, cv_opts, mode_of, panel_cond,
    valid_split_reach_problems, verdict_text,
)


class TestConditions:
    def test_builds_both_sides_from_the_function_choices(self):
        conds = build_conditions(valid_split=True)
        assert list(conds) == list(VALID_SPLIT_CONDS) == ["valid_rows", "valid_months"]
        assert {n: c.valid_split for n, c in conds.items()} == {"valid_rows": "rows", "valid_months": "months"}
        assert tuple(VALID_SPLIT_CONDS.values()) == mg._VALID_SPLIT_CHOICES

    def test_other_axes_stay_at_the_production_shape(self):
        """差し替えるのは検証集合の取り方だけ。本番（#969 で `months`）の側が `Cond` の既定そのもの。"""
        conds = build_conditions(valid_split=True)
        assert conds["valid_months"] == momentum_gate.Cond(False, MOM_WINDOW)
        assert conds["valid_rows"]._replace(valid_split="months") == conds["valid_months"]

    def test_both_sides_share_one_panel(self):
        conds = build_conditions(valid_split=True)
        assert len({panel_cond(c) for c in conds.values()}) == 1
        assert len(set(conds.values())) == 2          # CV は条件ごとに回る

    def test_baseline_is_the_production_scheme_of_m2(self):
        """分母は M-2 本体の `_VALID_SPLIT` から取る（書き写さない）。"""
        conds = build_conditions(valid_split=True)
        assert base_of(conds) == VALID_SPLIT_BASE_COND == f"valid_{mg._VALID_SPLIT}"

    def test_names_do_not_collide_with_the_inner_cv_purge_mode(self):
        """区切り方の名前（`rows` / `months`）をそのまま条件名にすると、分母を名前で引く `base_of` が他のモードの
        条件名と取り違えうる（足した当時は `--inner-cv` と衝突した・#968 で退役）。"""
        assert not set(VALID_SPLIT_CONDS) & set(INNER_CV_PURGE_CONDS)
        assert not set(VALID_SPLIT_CONDS) & {"rows", "months"}

    def test_measures_m2_only(self):
        assert VALID_SPLIT_MODELS == ["xgb_m2"]
        build_conditions(valid_split=True, models=["xgb_m2"])
        for models in (["elasticnet"], ["xgb_m2", "elasticnet"], ["risk_return"]):
            with pytest.raises(ValueError, match="M-2"):
                build_conditions(valid_split=True, models=models)

    def test_alpha_is_two_tests(self):
        conds = build_conditions(valid_split=True)
        assert bonferroni_alpha(len(VALID_SPLIT_MODELS), len(conds)) == pytest.approx(0.025)


class TestOptions:
    @pytest.mark.parametrize("kwargs", [dict(valid_split=True), dict(), dict(inner_cv_purge=True), dict(fin_fill=True)])
    def test_every_condition_reaches_the_candidate_with_its_own_scheme(self, kwargs):
        """各条件の opts を候補へ渡したとき、候補が取る検証集合＝条件の `valid_split`。

        **opts を省くかを本番の値と比べると、本番を切り替えた日に落ちる**（#964 と同じ理由）。
        """
        for c in build_conditions(**kwargs).values():
            opts = cv_opts(c, "xgb_m2") or {}
            assert opts.get("valid_split", mg._VALID_SPLIT_DEFAULT) == c.valid_split

    def test_only_the_m2_months_side_gets_options(self):
        conds = build_conditions(valid_split=True)
        assert cv_opts(conds["valid_rows"], "xgb_m2") is None
        assert cv_opts(conds["valid_months"], "xgb_m2") == {"valid_split": "months"}
        assert cv_opts(conds["valid_months"], "risk_return") is None

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(interactions=True),
        dict(max_features=[5, 20]), dict(fin_rows=True), dict(demean_target=True),
        dict(risk_axis=True, models=["xgb_m2"]), dict(fin_nan=True), dict(de_exclude=True),
        dict(fin_fill=True), dict(inner_cv_purge=True), dict(size_neutral=True),
    ])
    def test_existing_modes_keep_the_production_valid_split(self, kwargs):
        """既存モードの M-2 は本番の取り方（#969 で months）で回る。パネルの鍵も変わらない。"""
        for c in build_conditions(**kwargs).values():
            assert c.valid_split == mg._VALID_SPLIT
            assert cv_opts(c, "xgb_m2") == {"valid_split": mg._VALID_SPLIT}
            assert panel_cond(c)._replace(inner_cv=c.inner_cv) == c


class TestReachCheck:
    def test_reach_check(self):
        conds = build_conditions(valid_split=True)
        healthy = {"valid_rows|xgb_m2": {"best_iteration": 80},
                   "valid_months|xgb_m2": {"valid_split": "months"}}
        assert valid_split_reach_problems(healthy, conds) == []
        lost = {"valid_rows|xgb_m2": {"best_iteration": 80},
                "valid_months|xgb_m2": {"best_iteration": 80}}
        assert valid_split_reach_problems(lost, conds) == [
            "valid_months|xgb_m2: 検証集合が rows で取られた（指定は months）"]
        # 全 fold が落ちて診断値が空でも、届いたとはみなさない
        assert valid_split_reach_problems({"valid_months|xgb_m2": None}, conds)


class TestOutput:
    def test_mode_and_suffix(self):
        assert mode_of(valid_split=True) == "valid_split"
        assert MODE_SUFFIX["valid_split"] == "_valid_split"

    def test_verdict_names_the_axis(self):
        v = verdict_text("valid_split", 2, [], [])
        assert "VALID SPLIT AXIS" in v and "M-2 only" in v
        v.encode("cp932")

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ取り方になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("valid_split=args.valid_split") == 2            # build_conditions / mode_of
        assert src.count("opts=cv_opts(conds[cond], model)") == 2         # 全月と共通月の CV
        assert src.count("valid_split_reach_problems(") == 2
        assert "VALID_SPLIT_MODELS" in src
        assert "valid_split={c.valid_split}" in src
