"""`scripts/momentum_gate.py` の M-6 の内側 CV の空白のモード（`--inner-cv-purge`・#979・#968）。

M-6 の内側 CV で学習の最後の月と検証の最初の月の間に `LABEL_HORIZON_MONTHS` か月を上限とする空白を空ける
（本番・`gap`＝`months_purged`・#968 で切り替え）か、空けずに月の塊で区切る（`nogap`＝`months`）かを共通域で
比べる道具。危ないのは**黙って同じものを比べる**形:
  - opts が候補まで届かず、両条件とも同じ区切り方で回る（`walk_forward_cv_monthly` は fit_predict の例外を握るので、
    `train_groups` の渡し忘れも失敗として現れない）
  - 本番が `months_purged` から変わったのに、分母を `gap` のまま比べる（本番と比べているつもりで別物を比べる）
  - 条件名が他のモードと同じで、分母を名前で引く `base_of` が他方の本番を返す
このファイルは条件・分母・検定数・opts・届いたかの確認・`main` の配線と、`--inner-cv`（#971）の退役を縛る。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_enet as me  # noqa: E402
from plugins.model_candidates import (  # noqa: E402
    _EN_INNER_CV_CHOICES, _EN_INNER_CV_DEFAULT, _EN_INNER_CV_PURGED,
)
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    INNER_CV_PURGE_BASE_COND, INNER_CV_PURGE_CONDS, INNER_CV_PURGE_MODELS,
    MODE_SUFFIX, MOM_WINDOW, VALID_SPLIT_CONDS, base_of, bonferroni_alpha, build_conditions,
    cv_opts, inner_cv_reach_problems, mode_of, panel_cond, verdict_text,
)


class TestConditions:
    def test_compares_the_production_purged_blocks_with_the_plain_ones(self):
        conds = build_conditions(inner_cv_purge=True)
        assert list(conds) == list(INNER_CV_PURGE_CONDS) == ["nogap", "gap"]
        assert {n: c.inner_cv for n, c in conds.items()} == {"nogap": "months", "gap": "months_purged"}
        assert set(INNER_CV_PURGE_CONDS.values()) <= set(_EN_INNER_CV_CHOICES)
        assert INNER_CV_PURGE_CONDS["gap"] == _EN_INNER_CV_PURGED

    def test_other_axes_stay_at_the_production_shape(self):
        """差し替えるのは内側 CV の区切り方だけ。本番（#968 で `months_purged`）の側が `Cond` の既定そのもの。"""
        conds = build_conditions(inner_cv_purge=True)
        assert conds["gap"] == momentum_gate.Cond(False, MOM_WINDOW)
        assert conds["nogap"]._replace(inner_cv=me._INNER_CV) == conds["gap"]

    def test_both_sides_share_one_panel(self):
        conds = build_conditions(inner_cv_purge=True)
        assert len({panel_cond(c) for c in conds.values()}) == 1
        assert len(set(conds.values())) == 2          # CV は条件ごとに回る

    def test_baseline_is_the_production_scheme_of_m6(self):
        """分母は本番（#968 で `gap`）。#968 の判定の時点では `nogap`（当時の本番 `months`）だった。"""
        conds = build_conditions(inner_cv_purge=True)
        assert base_of(conds) == INNER_CV_PURGE_BASE_COND == "gap"
        assert INNER_CV_PURGE_CONDS[INNER_CV_PURGE_BASE_COND] == me._INNER_CV

    def test_stops_when_production_is_no_longer_the_baseline(self, monkeypatch):
        """本番が `months_purged` でなくなったら止める——黙って `gap` を分母にすると別物を比べる。"""
        for prod in ("rows", "months"):
            monkeypatch.setattr(momentum_gate, "_M6_INNER_CV", prod)
            with pytest.raises(ValueError, match="分母"):
                build_conditions(inner_cv_purge=True)

    def test_names_do_not_collide_with_the_other_split_modes(self):
        """条件名を区切り方そのものにすると、分母を名前で引く `base_of` が他のモードの条件名と取り違えうる。"""
        assert not set(INNER_CV_PURGE_CONDS) & set(VALID_SPLIT_CONDS)
        assert not set(INNER_CV_PURGE_CONDS) & set(_EN_INNER_CV_CHOICES)

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
    def test_existing_modes_run_the_production_split(self, kwargs):
        """既存モードの M-6 は本番の区切り方（#968 で `months_purged`）で回る。"""
        for c in build_conditions(**kwargs).values():
            assert c.inner_cv == me._INNER_CV == "months_purged"
            assert cv_opts(c, "elasticnet") == {"inner_cv": me._INNER_CV}

    @pytest.mark.parametrize("kwargs", [dict(inner_cv_purge=True), dict(), dict(fin_nan=True),
                                        dict(fin_fill=True)])
    def test_every_condition_reaches_the_candidate_with_its_own_scheme(self, kwargs):
        """各条件の opts を候補へ渡したとき、候補が回る区切り方＝条件の `inner_cv`（#964）。

        **opts を省くかを本番の値と比べると落ちる**: 本番側の opts が None になり、候補は既定の `rows` で回る
        ——本番の構成のつもりで旧い区切り方を測り、エラーは出ない。
        """
        for c in build_conditions(**kwargs).values():
            opts = cv_opts(c, "elasticnet") or {}
            assert opts.get("inner_cv", _EN_INNER_CV_DEFAULT) == c.inner_cv


class TestReachCheck:
    def test_reach_check(self):
        conds = build_conditions(inner_cv_purge=True)
        healthy = {"nogap|elasticnet": {"inner_cv": "months"},
                   "gap|elasticnet": {"inner_cv": "months_purged", "inner_cv_gap": 10.5}}
        assert inner_cv_reach_problems(healthy, conds) == []
        lost = {"nogap|elasticnet": {"inner_cv": "months"},
                "gap|elasticnet": {"inner_cv": "months"}}
        assert inner_cv_reach_problems(lost, conds) == [
            "gap|elasticnet: 内側 CV が months で回った（指定は months_purged）"]
        # 全 fold が落ちて診断値が空でも、届いたとはみなさない
        assert inner_cv_reach_problems({"gap|elasticnet": None}, conds)


class TestOutput:
    def test_mode_and_suffix(self):
        assert mode_of(inner_cv_purge=True) == "inner_cv_purge"
        assert MODE_SUFFIX["inner_cv_purge"] == "_inner_cv_purge"

    def test_verdict_names_the_axis_in_the_production_direction(self):
        """分母は本番の `gap` なので、判定文は「空白を外しても上回らない」の向き（#968）。"""
        v = verdict_text("inner_cv_purge", 2, [], [])
        assert "INNER CV PURGE AXIS" in v and "M-6 only" in v
        assert "removing the label-horizon gap" in v
        v.encode("cp932")

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ区切り方になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("inner_cv_purge=args.inner_cv_purge") == 2      # build_conditions / mode_of
        assert src.count("inner_cv_reach_problems(") == 2               # 全月と共通月の確認
        assert 'mode in ("inner_cv", "inner_cv_purge")' not in src      # 退役したモードの分岐を残さない
        assert src.count("opts=cv_opts(conds[cond], model)") == 2       # 全月と共通月の CV
        assert "pc = panel_cond(c)" in src
        assert "INNER_CV_PURGE_MODELS" in src
        assert "inner_cv_gap" in src


class TestRetiredInnerCvMode:
    """`--inner-cv`（`rows` 対 `months`・#971）は #968 で両側とも本番でなくなったので退役した。"""

    def test_flag_is_gone(self):
        with pytest.raises(TypeError):
            build_conditions(inner_cv=True)
        with pytest.raises(TypeError):
            mode_of(inner_cv=True)
        assert "inner_cv" not in MODE_SUFFIX
        assert not hasattr(momentum_gate, "INNER_CV_CONDS")
        assert not hasattr(momentum_gate, "INNER_CV_BASE_COND")

    def test_cli_rejects_the_flag(self):
        src = inspect.getsource(momentum_gate.main)
        assert '"--inner-cv",' not in src
