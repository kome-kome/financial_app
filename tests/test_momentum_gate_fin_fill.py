"""`scripts/momentum_gate.py` の空欄を 0 で埋めるモード（`--fin-fill`・#950）。

書類の実物で「無い」と確かめた空欄（無配の配当利回り・R&D の無い社の R&D 集約度）を 0 で埋めて、
M-2/M-6 の対象を広げるかを共通域で測る道具。危ないのは**黙って同じものを比べる**形と、**埋めては
いけない空欄まで 0 にする**形で、どちらも例外が出ない:

  - 判定の生の値が突き合わず、埋めた行が0になる
  - 埋めた財務がパネル構築に渡らない（元の `fins` を渡してしまう）
  - 埋めた写しが元の財務を書き換え、`drop` 側まで埋まる
  - 株価が空欄の行（赤字・無配と関係の無い欠け）や売上の無い行まで 0 にする

このファイルは条件・分母・検定数・出力と、上を止める純関数を縛る。

#983 で埋める処理が本番（`plugins.macro_snapshots`）へ移り、M-2 / M-6 の本番が埋める側になった。分母と
`Cond.fin_fill` の既定は本番の値から取る（書き写さない）。既定が埋める側になったので、**他のモードの財務の
写し（行の基準・D/E の空欄化）を保ったまま重ねる**こと（`build_fin_sources`）と、**M-1 には埋めた財務を
渡さない**こと（`fin_source_key`）もここで縛る。
"""
import datetime
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_snapshots as ms  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.candidate_bakeoff import _FinRow, _FIN_FIELDS  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    DE_EXCLUDE_CONDS, FIN_FILL_BASE_COND, FIN_FILL_CONDS, FIN_FILL_MODELS, MODE_SUFFIX, MODEL_SPECS,
    MOM_WINDOW, N_TESTS, Cond, base_of, bonferroni_alpha, build_conditions, build_fin_sources,
    fill_fin_blanks, fin_fill_reach_problems, fin_source_key, mode_of, verdict_text,
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
        """分母は本番の M-2 / M-6 の値（`macro_snapshots.FIN_FILL`）の側。書き写すと、本番を切り替えた日に
        「本番と比べているつもりで別物を比べる」（#964 の教訓）。"""
        base = base_of(build_conditions(fin_fill=True))
        assert base == FIN_FILL_BASE_COND
        assert FIN_FILL_CONDS[base] is ms.FIN_FILL

    def test_production_now_fills(self):
        """#983 で本番が埋める側になった。分母も `fill`。"""
        assert ms.FIN_FILL is True
        assert FIN_FILL_BASE_COND == "fill"

    def test_baseline_is_not_taken_from_the_fin_nan_name(self):
        """`drop` は `--fin-nan` の分母と同じ名前。`base_of` が `FIN_NAN_BASE_COND` を先に引くと、
        `--fin-fill` の分母が黙って `drop`（本番でない側）になる。"""
        assert base_of(build_conditions(fin_fill=True)) != momentum_gate.FIN_NAN_BASE_COND
        assert base_of(build_conditions(fin_nan=True)) == momentum_gate.FIN_NAN_BASE_COND

    def test_measures_m2_and_m6_on_one_shared_panel(self):
        assert FIN_FILL_MODELS == ["xgb_m2", "elasticnet"]
        assert {MODEL_SPECS[m][2] for m in FIN_FILL_MODELS} == {"gbdt"}

    def test_alpha_is_the_promotion_gate_alpha(self):
        conds = build_conditions(fin_fill=True)
        assert bonferroni_alpha(len(FIN_FILL_MODELS), len(conds)) == pytest.approx(0.05 / N_TESTS)

    def test_cannot_be_combined_with_another_population_axis(self):
        with pytest.raises(ValueError, match="--fin-nan と --fin-fill"):
            build_conditions(fin_nan=True, fin_fill=True)

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(interactions=True),
        dict(max_features=[5, 20]), dict(fin_rows=True), dict(demean_target=True),
        dict(risk_axis=True, models=["xgb_m2"]), dict(fin_nan=True), dict(de_exclude=True),
        dict(size_neutral=True), dict(valid_split=True), dict(inner_cv_purge=True), dict(subsample=True),
    ])
    def test_other_modes_use_the_production_value(self, kwargs):
        """他のモードの `Cond` は本番の M-2 / M-6 の値（#983 で埋める）。ゲートが本番と別の財務を測らない。"""
        assert all(c.fin_fill is ms.FIN_FILL for c in build_conditions(**kwargs).values())


class TestOutput:
    def test_mode_and_file(self):
        assert mode_of(fin_fill=True) == "fin_fill"
        assert MODE_SUFFIX["fin_fill"] == "_fin_fill"

    def test_verdict_is_neutral_and_ascii(self):
        none = verdict_text("fin_fill", 2, [], [])
        assert none.startswith("FIN FILL AXIS [common domain = rows complete before filling")
        # 分母が本番（#983 で fill）に入れ替わったので、どちらの向きでも読める文にした
        assert "did not beat the production one" in none
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

    def test_the_check_does_not_depend_on_which_side_is_the_baseline(self):
        """#983 で分母が `fill` になった。分母の名前で向きを決めると、縮んだ側を「増えていない」と誤読する。"""
        assert FIN_FILL_BASE_COND == "fill"
        assert fin_fill_reach_problems({"rd_intensity": 5, "div_yield": 3},
                                       {"drop|gbdt": 100, "fill|gbdt": 130}, ["gbdt"]) == []


class TestFillIsTheProductionCode:
    """埋める処理は本番（`plugins.macro_snapshots`）から import する（#983・書き写さない）。"""

    def test_the_functions_are_the_production_ones(self):
        assert momentum_gate.fill_fin_blanks is ms.fill_fin_blanks
        assert momentum_gate.load_fill_sources is ms.load_fill_sources
        assert ms.fill_fin_blanks.__module__ == "plugins.macro_snapshots"

    def test_no_copy_is_left_in_the_gate(self):
        src = inspect.getsource(momentum_gate)
        assert "def fill_fin_blanks" not in src
        assert "def load_fill_sources" not in src
        assert "FIN_FILL_SOURCE_FIELDS = " not in src

    def test_the_production_function_fills_the_same_rows(self):
        """計測の道具の行（`candidate_bakeoff._FinRow`）と本番の行（`macro_snapshots._FinRow`）で同じ行を同じ値で
        埋める（移す前の計測と同じ結果）。"""
        prod_vals = dict.fromkeys(ms.FIN_LOAD_FIELDS)
        prod_vals.update(period_end=PE, per=12.0)
        prod_row = ms._FinRow(**prod_vals)
        gate_out, gate_n = fill_fin_blanks({"E1": [_row()]}, {KEY: _src()})
        prod_out, prod_n = ms.fill_fin_blanks({"E1": [prod_row]}, {KEY: _src()})
        assert gate_n == prod_n == {"rd_intensity": 1, "div_yield": 1}
        for col in ms.FIN_FILL_COLUMNS:
            assert getattr(gate_out["E1"][0], col) == getattr(prod_out["E1"][0], col) == 0.0


def _row_ind(industry="証券、商品先物取引業", de_ratio=2.0, period_end=PE):
    vals = dict.fromkeys(_FIN_FIELDS)
    vals.update(period_end=period_end, per=12.0, industry=industry, de_ratio=de_ratio)
    return _FinRow(**vals)


class TestFinSourceKey:
    def test_m1_never_gets_the_filled_financials(self):
        """M-1 の本番は埋めない（`macro_risk_return` は `apply_fin_fill` を通らない）。"""
        c = Cond(False, MOM_WINDOW, fin_fill=True)
        assert fin_source_key(c, "m1")[2] is False
        assert fin_source_key(c, "gbdt")[2] is True

    def test_the_key_keeps_the_other_axes(self):
        c = Cond(False, MOM_WINDOW, fin_rows="with_ttm", de_null_industries=("銀行業",), fin_fill=True)
        assert fin_source_key(c, "gbdt") == ("with_ttm", ("銀行業",), True, False)


class TestBuildFinSources:
    """財務の写しの重ね方（#983）。**取り違えても例外は出ず、両条件が同じ財務で組まれて差なしになる**。"""

    def test_fill_keeps_the_row_basis(self):
        """`--fin-rows` の TTM 側が埋めた `annual` の財務に置き換わらない。"""
        fins = {"annual": {"E1": [_row()]}, "with_ttm": {"E1": [_row()], "E2": [_row()]}}
        conds = build_conditions(fin_rows=True)
        srcs, _n_null, filled = build_fin_sources(conds, ["gbdt"], fins, {KEY: _src()})
        ttm = srcs[fin_source_key(conds["with_ttm"], "gbdt")]
        assert set(ttm) == {"E1", "E2"}, "TTM 側が annual の財務で組まれている"
        assert ttm["E1"][0].rd_intensity == 0.0, "TTM 側が埋まっていない"
        assert set(filled) == {"annual", "with_ttm"}

    def test_fill_keeps_the_de_blanking(self):
        """`--de-exclude` の空欄化が埋めた写しで消えない。"""
        inds = DE_EXCLUDE_CONDS["exclude"]
        fins = {"annual": {"E1": [_row_ind(industry=inds[0])]}}
        conds = build_conditions(de_exclude=True)
        srcs, n_null, _filled = build_fin_sources(conds, ["gbdt"], fins, {KEY: _src()})
        ex = srcs[fin_source_key(conds["exclude"], "gbdt")]["E1"][0]
        keep = srcs[fin_source_key(conds["keep"], "gbdt")]["E1"][0]
        assert ex.de_ratio is None and keep.de_ratio == 2.0
        assert ex.rd_intensity == 0.0 and keep.rd_intensity == 0.0
        assert n_null == 1

    def test_m1_gets_the_original_financials(self):
        fins = {"annual": {"E1": [_row()]}}
        conds = build_conditions()
        srcs, _n, _f = build_fin_sources(conds, ["gbdt", "m1"], fins, {KEY: _src()})
        cond = next(iter(conds.values()))
        assert srcs[fin_source_key(cond, "m1")] is fins["annual"]
        assert srcs[fin_source_key(cond, "gbdt")]["E1"][0].rd_intensity == 0.0

    def test_the_drop_side_stays_blank(self):
        fins = {"annual": {"E1": [_row()]}}
        conds = build_conditions(fin_fill=True)
        srcs, _n, filled = build_fin_sources(conds, ["gbdt"], fins, {KEY: _src()})
        assert srcs[fin_source_key(conds["drop"], "gbdt")] is fins["annual"]
        assert srcs[fin_source_key(conds["fill"], "gbdt")]["E1"][0].rd_intensity == 0.0
        assert fins["annual"]["E1"][0].rd_intensity is None, "元の財務を書き換えた"
        assert filled == {"annual": {"rd_intensity": 1, "div_yield": 1}}


class TestMainWiring:
    def test_the_conditions_build_from_their_own_financials(self):
        """**渡し忘れても例外は出ない**（両条件が同じ財務で組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        assert "build_fin_sources(conds, kinds, fins, fill_sources," in src
        assert "fin_src = fin_srcs[fin_source_key(c, kind)]" in src
        assert "kind, args, prices_by_co, fin_src, companies," in src
        assert "load_fill_sources(db)" in src
        assert "fin_fill=args.fin_fill" in src
        assert "fin_fill_reach_problems(" in src

    def test_the_sources_are_read_before_the_cpu_work(self):
        """判定の生の値は `db.commit()` の前に読む（CPU 計算中に読取トランザクションを残さない・#411）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.index("load_fill_sources(db)") < src.index("db.commit()")

    def test_json_records_the_axis(self):
        src = inspect.getsource(momentum_gate.main)
        assert '"fin_fill": c.fin_fill' in src
        assert '"fin_fill": {"filled_rows": fin_filled_by_rows' in src
