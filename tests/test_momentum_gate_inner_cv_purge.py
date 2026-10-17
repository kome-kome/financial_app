"""`scripts/momentum_gate.py` の M-6 の内側 CV の空白のモード（`--inner-cv-purge`・#979・#968）。

M-6 の内側 CV を月の塊で区切る（本番・`nogap`＝`months`）か、学習の最後の月と検証の最初の月の間に
`LABEL_HORIZON_MONTHS` か月を上限とする空白を空ける（`gap`＝`months_purged`）かを共通域で比べる道具。
`--inner-cv` と同じ形で、危ないのは**黙って同じものを比べる**形:
  - opts が候補まで届かず、両条件とも `months` で回る（`walk_forward_cv_monthly` は fit_predict の例外を握るので、
    `train_groups` の渡し忘れも失敗として現れない）
  - 本番が `months` から変わったのに、分母を `months` のまま比べる（本番と比べているつもりで別物を比べる）
  - 条件名が `--inner-cv` と同じで、分母を名前で引く `base_of` が他方の本番を返す
このファイルは条件・分母・検定数・opts・届いたかの確認・`main` の配線を縛る。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_enet as me  # noqa: E402
from plugins.model_candidates import _EN_INNER_CV_CHOICES, _EN_INNER_CV_PURGED  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    INNER_CV_BASE_COND, INNER_CV_PURGE_BASE_COND, INNER_CV_PURGE_CONDS, INNER_CV_PURGE_MODELS,
    MODE_SUFFIX, MOM_WINDOW, VALID_SPLIT_CONDS, base_of, bonferroni_alpha, build_conditions,
    cv_opts, inner_cv_reach_problems, mode_of, panel_cond, verdict_text,
)


class TestConditions:
    def test_compares_the_production_month_blocks_with_the_purged_ones(self):
        conds = build_conditions(inner_cv_purge=True)
        assert list(conds) == list(INNER_CV_PURGE_CONDS) == ["nogap", "gap"]
        assert {n: c.inner_cv for n, c in conds.items()} == {"nogap": "months", "gap": "months_purged"}
        assert set(INNER_CV_PURGE_CONDS.values()) <= set(_EN_INNER_CV_CHOICES)
        assert INNER_CV_PURGE_CONDS["gap"] == _EN_INNER_CV_PURGED

    def test_other_axes_stay_at_the_production_shape(self):
        """差し替えるのは内側 CV の区切り方だけ。本番（`months`）の側が `Cond` の既定そのもの。"""
        conds = build_conditions(inner_cv_purge=True)
        assert conds["nogap"] == momentum_gate.Cond(False, MOM_WINDOW)
        assert conds["gap"]._replace(inner_cv=me._INNER_CV) == conds["nogap"]

    def test_both_sides_share_one_panel(self):
        conds = build_conditions(inner_cv_purge=True)
        assert len({panel_cond(c) for c in conds.values()}) == 1
        assert len(set(conds.values())) == 2          # CV は条件ごとに回る

    def test_baseline_is_the_production_scheme_of_m6(self):
        conds = build_conditions(inner_cv_purge=True)
        assert base_of(conds) == INNER_CV_PURGE_BASE_COND == "nogap"
        assert INNER_CV_PURGE_CONDS[INNER_CV_PURGE_BASE_COND] == me._INNER_CV

    def test_stops_when_production_is_no_longer_the_baseline(self, monkeypatch):
        """本番が `months` でなくなったら止める——黙って `months` を分母にすると別物を比べる。"""
        for prod in ("rows", "months_purged"):
            monkeypatch.setattr(momentum_gate, "_M6_INNER_CV", prod)
            with pytest.raises(ValueError, match="分母"):
                build_conditions(inner_cv_purge=True)

    def test_names_do_not_collide_with_the_other_split_modes(self):
        """`--inner-cv` の `months` と同じ名前だと、`base_of` が先に並ぶ `INNER_CV_BASE_COND` を返す。"""
        assert not set(INNER_CV_PURGE_CONDS) & set(build_conditions(inner_cv=True))
        assert not set(INNER_CV_PURGE_CONDS) & set(VALID_SPLIT_CONDS)
        assert INNER_CV_BASE_COND not in INNER_CV_PURGE_CONDS

    def test_measures_m6_only(self):
        assert INNER_CV_PURGE_MODELS == ["elasticnet"]
        build_conditions(inner_cv_purge=True, models=["elasticnet"])
        for models in (["xgb_m2"], ["xgb_m2", "elasticnet"], ["risk_return"]):
            with pytest.raises(ValueError, match="M-6"):
                build_conditions(inner_cv_purge=True, models=models)

    def test_alpha_is_two_tests(self):
        conds = build_conditions(inner_cv_purge=True)
        assert bonferroni_alpha(len(INNER_CV_PURGE_MODELS), len(conds)) == pytest.approx(0.025)


class TestOptions:
    def test_each_side_reaches_the_candidate_with_its_own_split(self):
        conds = build_conditions(inner_cv_purge=True)
        assert cv_opts(conds["nogap"], "elasticnet") == {"inner_cv": "months"}
        assert cv_opts(conds["gap"], "elasticnet") == {"inner_cv": "months_purged"}
        # M-2 には内側 CV の軸が漏れない（M-2 に渡るのは M-2 自身の本番の検証集合だけ・#969）
        assert "inner_cv" not in (cv_opts(conds["gap"], "xgb_m2") or {})

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(interactions=True),
        dict(max_features=[5, 20]), dict(fin_rows=True), dict(demean_target=True),
        dict(risk_axis=True, models=["elasticnet"]), dict(fin_nan=True), dict(de_exclude=True),
        dict(fin_fill=True), dict(size_neutral=True), dict(valid_split=True),
    ])
    def test_existing_modes_never_run_the_purged_split(self, kwargs):
        """既存モードの M-6 は本番の区切り方のまま（空白付きは新しいモードでしか回らない）。"""
        for c in build_conditions(**kwargs).values():
            assert c.inner_cv == me._INNER_CV
            assert cv_opts(c, "elasticnet") == {"inner_cv": me._INNER_CV}


class TestReachCheck:
    def test_reach_check(self):
        conds = build_conditions(inner_cv_purge=True)
        healthy = {"nogap|elasticnet": {"inner_cv": "months"},
                   "gap|elasticnet": {"inner_cv": "months_purged", "inner_cv_gap": 10.5}}
        assert inner_cv_reach_problems(healthy, conds) == []
        lost = {"nogap|elasticnet": {"inner_cv": "months"},
                "gap|elasticnet": {"inner_cv": "months"}}
        assert len(inner_cv_reach_problems(lost, conds)) == 1
        # 全 fold が落ちて診断値が空でも、届いたとはみなさない
        assert inner_cv_reach_problems({"gap|elasticnet": None}, conds)


class TestOutput:
    def test_mode_and_suffix(self):
        assert mode_of(inner_cv_purge=True) == "inner_cv_purge"
        assert MODE_SUFFIX["inner_cv_purge"] == "_inner_cv_purge"

    def test_verdict_names_the_axis(self):
        v = verdict_text("inner_cv_purge", 2, [], [])
        assert "INNER CV PURGE AXIS" in v and "M-6 only" in v
        v.encode("cp932")

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ区切り方になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("inner_cv_purge=args.inner_cv_purge") == 2      # build_conditions / mode_of
        assert src.count('if mode in ("inner_cv", "inner_cv_purge"):') == 2   # 全月と共通月の確認
        assert "INNER_CV_PURGE_MODELS" in src
        assert "inner_cv_gap" in src
