"""`scripts/momentum_gate.py` の財務の空欄モード（`--fin-nan`・#938/#916）。

M-2/M-6 で、財務特徴量が空欄の行を捨てずに NaN で学習へ通すかを共通域で測る道具。危ないのは
**黙って同じものを比べる**形で、どれも例外が出ずに「差なし」という結論だけが残る:

  - `fin_nan_ok` がパネル構築まで渡らない（`_build` の呼び出しで落とす）
  - キャッシュのキーに入らず、drop と nan が同じパネルを共有する（`tests/test_tuning.py` が縛る）
  - 空欄の財務を持つ行が1つも無く、両条件が同じパネルになる

このファイルは条件・分母・検定数・出力と、3つ目を止める純関数を縛る。1つ目は
`tests/test_momentum_gate_axes.py::TestBuildUsesTheConditionAxes` が縛る。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    FIN_NAN_BASE_COND, FIN_NAN_CONDS, FIN_NAN_MODELS, MODE_SUFFIX, MODEL_SPECS, MOM_WINDOW, N_TESTS,
    base_of, bonferroni_alpha, build_conditions, fin_nan_reach_problems, mode_of, verdict_text,
)


class TestConditions:
    def test_conditions_are_drop_and_nan(self):
        conds = build_conditions(fin_nan=True)
        assert list(conds) == list(FIN_NAN_CONDS) == ["drop", "nan"]
        assert conds["drop"].fin_nan_ok is False
        assert conds["nan"].fin_nan_ok is True

    def test_other_axes_stay_at_the_production_shape(self):
        """問うのは「本番の M-2/M-6 で空欄の社を外さずに通したら」。他の軸は本番構成に固定する。"""
        for c in build_conditions(fin_nan=True).values():
            assert c.use_momentum is False
            assert c.momentum_window == MOM_WINDOW
            assert c.use_macro is True
            assert c.build_interactions is True
            assert c.max_features is None
            assert c.fin_rows == "annual"
            assert c.demean_target is False

    def test_baseline_is_the_production_shape(self):
        conds = build_conditions(fin_nan=True)
        assert base_of(conds) == FIN_NAN_BASE_COND == "drop"

    def test_measures_m2_and_m6_on_one_shared_panel(self):
        """M-2 と M-6 は同じ gbdt のパネルを共有する＝パネルに効く軸は両方を測る。"""
        assert FIN_NAN_MODELS == ["xgb_m2", "elasticnet"]
        assert {MODEL_SPECS[m][2] for m in FIN_NAN_MODELS} == {"gbdt"}

    def test_alpha_is_the_promotion_gate_alpha(self):
        """2モデル × 2指標 × 1条件 = 4検定＝既定の昇格ゲート（ADR-0045）と同じ alpha。"""
        conds = build_conditions(fin_nan=True)
        assert bonferroni_alpha(len(FIN_NAN_MODELS), len(conds)) == pytest.approx(0.05 / N_TESTS)


class TestOutput:
    def test_mode_and_file(self):
        assert mode_of(fin_nan=True) == "fin_nan"
        assert MODE_SUFFIX["fin_nan"] == "_fin_nan"

    def test_verdict_is_neutral_and_says_what_the_common_domain_covers(self):
        """本番を変えるかは人が決める（#916）。共通域が財務の揃った行だけであることを見出しに書く。"""
        none = verdict_text("fin_nan", 2, [], [])
        assert none.startswith("FIN NAN AXIS [common domain = complete-financials rows only; "
                               "read raw for blank rows]: ")
        assert "passing blank financials as NaN did not beat dropping them" in none
        hit = verdict_text("fin_nan", 2, ["M-2(XGBoost)/nan/rank_ic"], [])
        assert "PROMOTE" not in hit and "use_momentum" not in hit

    def test_output_is_ascii(self):
        """出力は ASCII のみ（Windows cp932 リダイレクト対策）。"""
        verdict_text("fin_nan", 2, ["x"], ["y"]).encode("ascii")


class TestMainWiring:
    def test_each_condition_builds_with_its_own_flag(self):
        """**渡し忘れても例外は出ない**（両条件が同じパネルで組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        assert "fin_nan_ok=c.fin_nan_ok" in src
        assert "fin_nan=args.fin_nan" in src
        assert "fin_nan_reach_problems(" in src

    def test_json_records_the_axis(self):
        src = inspect.getsource(momentum_gate.main)
        assert '"fin_nan_ok": c.fin_nan_ok' in src
        assert '"fin_nan_samples": fin_nan_samples' in src


class TestReachProblems:
    def test_more_rows_on_the_nan_side_is_healthy(self):
        samples = {"drop|gbdt": 100, "nan|gbdt": 130}
        assert fin_nan_reach_problems(samples, ["gbdt"]) == []

    def test_equal_rows_mean_the_switch_did_not_reach(self):
        """空欄の財務を持つ行が無い（または引数が渡っていない）＝同じパネルを比べることになる。"""
        problems = fin_nan_reach_problems({"drop|gbdt": 100, "nan|gbdt": 100}, ["gbdt"])
        assert len(problems) == 1 and "nan|gbdt" in problems[0]

    def test_fewer_rows_on_the_nan_side_is_also_a_problem(self):
        """nan 側は drop 側を含む。減っていたら包含関係そのものが崩れている。"""
        assert fin_nan_reach_problems({"drop|gbdt": 100, "nan|gbdt": 90}, ["gbdt"])

    def test_a_missing_panel_counts_as_zero(self):
        assert fin_nan_reach_problems({"drop|gbdt": 100}, ["gbdt"])
