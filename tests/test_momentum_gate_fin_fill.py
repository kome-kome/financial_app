"""`scripts/momentum_gate.py` の空欄を 0 で埋めるモード（`--fin-fill`・#950）。

書類の実物で「無い」と確かめた空欄（無配の配当利回り・R&D の無い社の R&D 集約度）を 0 で埋めて、
M-2/M-6 の対象を広げるかを共通域で測る道具。危ないのは**黙って同じものを比べる**形と、**埋めては
いけない空欄まで 0 にする**形で、どちらも例外が出ない:

  - 判定の生の値が突き合わず、埋めた行が0になる
  - 埋めた財務がパネル構築に渡らない（元の `fins` を渡してしまう）
  - 埋めた写しが元の財務を書き換え、`drop` 側まで埋まる
  - 株価が空欄の行（赤字・無配と関係の無い欠け）や売上の無い行まで 0 にする

このファイルは条件・分母・検定数・出力と、上を止める純関数を縛る。
"""
import datetime
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import momentum_gate  # noqa: E402
from scripts.candidate_bakeoff import _FinRow, _FIN_FIELDS  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    FIN_FILL_BASE_COND, FIN_FILL_CONDS, FIN_FILL_MODELS, MODE_SUFFIX, MODEL_SPECS, MOM_WINDOW,
    N_TESTS, base_of, bonferroni_alpha, build_conditions, fill_fin_blanks, fin_fill_reach_problems,
    mode_of, verdict_text,
)

PE = datetime.date(2025, 3, 31)
KEY = ("E1", "2025-03-31")


def _row(rd_intensity=None, div_yield=None, period_end=PE):
    vals = dict.fromkeys(_FIN_FIELDS)
    vals.update(rd_intensity=rd_intensity, div_yield=div_yield, period_end=period_end, per=12.0)
    return _FinRow(**vals)


def _src(dps=None, price=1000.0, rd=None, revenue=1.0e9):
    """FIN_FILL_SOURCE_FIELDS の順（dps, stock_price, pl_rd_expenses, pl_revenue）。"""
    return (dps, price, rd, revenue)


class TestConditions:
    def test_conditions_are_drop_and_fill(self):
        conds = build_conditions(fin_fill=True)
        assert list(conds) == list(FIN_FILL_CONDS) == ["drop", "fill"]
        assert conds["drop"].fin_fill is False
        assert conds["fill"].fin_fill is True

    def test_other_axes_stay_at_the_production_shape(self):
        for c in build_conditions(fin_fill=True).values():
            assert c.use_momentum is False
            assert c.momentum_window == MOM_WINDOW
            assert c.use_macro is True
            assert c.fin_rows == "annual"
            assert c.demean_target is False
            assert c.fin_nan_ok is False
            assert c.de_null_industries == ()

    def test_baseline_is_the_production_shape(self):
        assert base_of(build_conditions(fin_fill=True)) == FIN_FILL_BASE_COND == "drop"

    def test_measures_m2_and_m6_on_one_shared_panel(self):
        assert FIN_FILL_MODELS == ["xgb_m2", "elasticnet"]
        assert {MODEL_SPECS[m][2] for m in FIN_FILL_MODELS} == {"gbdt"}

    def test_alpha_is_the_promotion_gate_alpha(self):
        conds = build_conditions(fin_fill=True)
        assert bonferroni_alpha(len(FIN_FILL_MODELS), len(conds)) == pytest.approx(0.05 / N_TESTS)

    def test_cannot_be_combined_with_another_population_axis(self):
        with pytest.raises(ValueError, match="--fin-nan と --fin-fill"):
            build_conditions(fin_nan=True, fin_fill=True)

    def test_existing_modes_are_untouched(self):
        """既存モードの `Cond` は `fin_fill=False`（本番）のまま。"""
        for kwargs in (dict(), dict(fin_nan=True), dict(de_exclude=True), dict(fin_rows=True)):
            assert all(c.fin_fill is False for c in build_conditions(**kwargs).values())


class TestOutput:
    def test_mode_and_file(self):
        assert mode_of(fin_fill=True) == "fin_fill"
        assert MODE_SUFFIX["fin_fill"] == "_fin_fill"

    def test_verdict_is_neutral_and_ascii(self):
        none = verdict_text("fin_fill", 2, [], [])
        assert none.startswith("FIN FILL AXIS [common domain = rows complete before filling")
        assert "did not beat dropping those rows" in none
        hit = verdict_text("fin_fill", 2, ["M-6(ElasticNet)/fill/rank_ic"], ["y"])
        assert "PROMOTE" not in hit and "use_momentum" not in hit
        hit.encode("ascii")


class TestFillFinBlanks:
    def test_no_rd_company_gets_zero_intensity(self):
        out, n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src(rd=None, revenue=5.0e9)})
        assert out["E1"][0].rd_intensity == 0.0
        assert n["rd_intensity"] == 1

    def test_rd_is_not_filled_without_revenue(self):
        """売上が空欄か 0 で割れない行は R&D の有無と関係が無い。"""
        for revenue in (None, 0.0):
            out, n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src(revenue=revenue)})
            assert out["E1"][0].rd_intensity is None
            assert n["rd_intensity"] == 0

    def test_rd_is_not_filled_when_expenses_exist(self):
        """R&D 費があるのに集約度が空欄なら別の理由。0 にしない。"""
        out, _n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src(rd=1.0e8)})
        assert out["E1"][0].rd_intensity is None

    def test_no_dividend_company_gets_zero_yield(self):
        for dps in (None, 0.0):
            out, n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src(dps=dps, price=800.0)})
            assert out["E1"][0].div_yield == 0.0
            assert n["div_yield"] == 1

    def test_yield_is_not_filled_without_a_price(self):
        """株価の欠けは無配とは関係が無い（#950 の数えでは PER・配当利回りの空欄の 3〜4 割）。"""
        for price in (None, 0.0):
            out, n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src(price=price)})
            assert out["E1"][0].div_yield is None
            assert n["div_yield"] == 0

    def test_yield_is_not_filled_when_a_dividend_exists(self):
        out, _n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src(dps=30.0)})
        assert out["E1"][0].div_yield is None

    def test_existing_values_are_kept(self):
        out, n = fill_fin_blanks({"E1": [_row(rd_intensity=3.2, div_yield=1.5)]}, {KEY: _src()})
        assert (out["E1"][0].rd_intensity, out["E1"][0].div_yield) == (3.2, 1.5)
        assert n == {"rd_intensity": 0, "div_yield": 0}

    def test_rows_without_a_source_are_left_blank(self):
        out, n = fill_fin_blanks({"E1": [_row(period_end=datetime.date(2024, 3, 31))]},
                                 {KEY: _src()})
        assert out["E1"][0].rd_intensity is None and out["E1"][0].div_yield is None
        assert sum(n.values()) == 0

    def test_does_not_touch_the_original(self):
        """drop 側は同じ `fins` を読む。元を書き換えると drop 側まで埋まる。"""
        fins = {"E1": [_row()]}
        fill_fin_blanks(fins, {KEY: _src()})
        assert fins["E1"][0].rd_intensity is None and fins["E1"][0].div_yield is None

    def test_other_columns_are_kept(self):
        out, _n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src()})
        assert out["E1"][0].per == 12.0

    def test_period_end_may_come_as_a_string(self):
        """キャッシュ由来の財務は period_end が文字列のことがある（`_iso` で揃える）。"""
        out, _n = fill_fin_blanks({"E1": [_row(period_end="2025-03-31")]}, {KEY: _src()})
        assert out["E1"][0].rd_intensity == 0.0


class TestReachProblems:
    def test_healthy_when_rows_were_filled_and_the_panel_grew(self):
        assert fin_fill_reach_problems({"rd_intensity": 5, "div_yield": 3},
                                       {"drop|gbdt": 100, "fill|gbdt": 130}, ["gbdt"]) == []

    def test_no_filled_rows_is_a_problem(self):
        problems = fin_fill_reach_problems({"rd_intensity": 0, "div_yield": 0},
                                           {"drop|gbdt": 100, "fill|gbdt": 130}, ["gbdt"])
        assert any("0" in p for p in problems)

    def test_a_panel_that_did_not_grow_is_a_problem(self):
        problems = fin_fill_reach_problems({"rd_intensity": 5, "div_yield": 3},
                                           {"drop|gbdt": 100, "fill|gbdt": 100}, ["gbdt"])
        assert any("fill|gbdt" in p for p in problems)


class TestMainWiring:
    def test_the_fill_condition_builds_from_the_filled_financials(self):
        """**渡し忘れても例外は出ない**（両条件が同じ財務で組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        assert "fill_fin_blanks(fins[FIN_ROWS_BASE_COND], load_fill_sources(db))" in src
        assert "if c.fin_fill:\n                        fin_src = filled_fins" in src
        assert "kind, args, prices_by_co, fin_src, companies," in src
        assert "fin_fill=args.fin_fill" in src
        assert "fin_fill_reach_problems(" in src

    def test_json_records_the_axis(self):
        src = inspect.getsource(momentum_gate.main)
        assert '"fin_fill": c.fin_fill' in src
        assert '"fin_fill": {"filled_rows": fin_filled' in src
