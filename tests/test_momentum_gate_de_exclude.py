"""`scripts/momentum_gate.py` の D/E の業種除外モード（`--de-exclude`・#940/#927）。

証券業・その他金融業の D/E を M-2/M-6 の比較から外すかを共通域で測る道具。危ないのは
**黙って同じものを比べる**形で、どれも例外が出ずに「差なし」という結論だけが残る:

  - 業種名の表記ずれで、空欄にした行が0になる
  - 差し替えた財務がパネル構築に渡らない（元の `fins` を渡してしまう）
  - 差し替えが元の財務を書き換え、`keep` 側まで空欄になる

このファイルは条件・分母・検定数・出力と、上の3つを止める純関数を縛る。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from plugins.macro_snapshots import FIN_LOAD_FIELDS, _FinRow  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    DE_EXCLUDE_BASE_COND, DE_EXCLUDE_CONDS, DE_EXCLUDE_MODELS, MODE_SUFFIX, MODEL_SPECS, MOM_WINDOW,
    N_TESTS, base_of, bonferroni_alpha, build_conditions, de_exclude_reach_problems, mode_of,
    null_de_ratio, verdict_text,
)

SECURITIES = "証券、商品先物取引業"
OTHER_FIN = "その他金融業"


def _row(industry: str, de_ratio):
    vals = dict.fromkeys(FIN_LOAD_FIELDS)
    vals.update(industry=industry, de_ratio=de_ratio)
    return _FinRow(**vals)


class TestConditions:
    def test_conditions_are_keep_and_exclude(self):
        conds = build_conditions(de_exclude=True)
        assert list(conds) == list(DE_EXCLUDE_CONDS) == ["keep", "exclude"]
        assert conds["keep"].de_null_industries == ()
        assert conds["exclude"].de_null_industries == database.DE_RATIO_EXCLUSION_CANDIDATES

    def test_candidates_are_the_two_industries(self):
        """業種名は JPX 33 業種の表記（`tests/test_collector.py` の業種名と同じ）。"""
        assert database.DE_RATIO_EXCLUSION_CANDIDATES == (SECURITIES, OTHER_FIN)
        assert not set(database.DE_RATIO_EXCLUSION_CANDIDATES) & set(
            database.DE_RATIO_EXCLUDED_INDUSTRIES)

    def test_other_axes_stay_at_the_production_shape(self):
        for c in build_conditions(de_exclude=True).values():
            assert c.use_momentum is False
            assert c.momentum_window == MOM_WINDOW
            assert c.use_macro is True
            assert c.fin_rows == "annual"
            assert c.demean_target is False
            assert c.fin_nan_ok is False

    def test_baseline_is_the_production_shape(self):
        assert base_of(build_conditions(de_exclude=True)) == DE_EXCLUDE_BASE_COND == "keep"

    def test_measures_m2_and_m6_on_one_shared_panel(self):
        assert DE_EXCLUDE_MODELS == ["xgb_m2", "elasticnet"]
        assert {MODEL_SPECS[m][2] for m in DE_EXCLUDE_MODELS} == {"gbdt"}

    def test_alpha_is_the_promotion_gate_alpha(self):
        conds = build_conditions(de_exclude=True)
        assert bonferroni_alpha(len(DE_EXCLUDE_MODELS), len(conds)) == pytest.approx(0.05 / N_TESTS)

    def test_conditions_are_hashable(self):
        """パネルは `(Cond, 種別)` をキーに1回だけ作る（`built`）。tuple の業種なのでハッシュできる。"""
        assert len({c for c in build_conditions(de_exclude=True).values()}) == 2


class TestOutput:
    def test_mode_and_file(self):
        assert mode_of(de_exclude=True) == "de_exclude"
        assert MODE_SUFFIX["de_exclude"] == "_de_exclude"

    def test_verdict_is_neutral_and_ascii(self):
        none = verdict_text("de_exclude", 2, [], [])
        assert none.startswith("DE EXCLUDE AXIS: ")
        assert "blanking D/E for the candidate industries did not beat keeping it" in none
        hit = verdict_text("de_exclude", 2, ["M-2(XGBoost)/exclude/rank_ic"], ["y"])
        assert "PROMOTE" not in hit and "use_momentum" not in hit
        hit.encode("ascii")


class TestMainWiring:
    def test_each_condition_builds_from_its_own_financials(self):
        """**渡し忘れても例外は出ない**（両条件が同じ財務で組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        # 財務の写しは `build_fin_sources` が (行の基準, 業種, 0 で埋めるか) ごとに作る（#983。空欄化を埋めた写しで
        # 消さない形は tests/test_momentum_gate_fin_fill.py::TestBuildFinSources が縛る）
        assert "null_de_ratio(src, inds)" in inspect.getsource(momentum_gate.build_fin_sources)
        assert "build_fin_sources(conds, kinds, fins, fill_sources)" in src
        assert "fin_src = fin_srcs[fin_source_key(c, kind)]" in src
        assert "kind, args, prices_by_co, fin_src, companies," in src
        assert "de_exclude=args.de_exclude" in src
        assert "de_exclude_reach_problems(" in src

    def test_json_records_the_axis(self):
        src = inspect.getsource(momentum_gate.main)
        assert '"de_null_industries": list(c.de_null_industries)' in src
        assert '"de_exclude": {"nulled_rows": de_nulled_rows' in src


class TestNullDeRatio:
    def test_blanks_only_the_listed_industries(self):
        fins = {"E1": [_row(SECURITIES, 5.0)], "E2": [_row("小売業", 0.4)],
                "E3": [_row(OTHER_FIN, 9.0), _row(OTHER_FIN, 8.0)]}
        out, n = null_de_ratio(fins, (SECURITIES, OTHER_FIN))
        assert n == 3
        assert out["E1"][0].de_ratio is None
        assert out["E2"][0].de_ratio == 0.4
        assert [r.de_ratio for r in out["E3"]] == [None, None]

    def test_does_not_touch_the_original(self):
        """keep 側は同じ `fins` を読む。元を書き換えると keep 側まで空欄になる。"""
        fins = {"E1": [_row(SECURITIES, 5.0)]}
        null_de_ratio(fins, (SECURITIES,))
        assert fins["E1"][0].de_ratio == 5.0

    def test_already_blank_rows_are_not_counted(self):
        """届いたかの検査に使うので、実際に値を消した行だけを数える。"""
        _out, n = null_de_ratio({"E1": [_row(SECURITIES, None)]}, (SECURITIES,))
        assert n == 0

    def test_other_columns_are_kept(self):
        rec = _row(SECURITIES, 5.0)._replace(per=12.0, roe=8.0)
        out, _n = null_de_ratio({"E1": [rec]}, (SECURITIES,))
        assert out["E1"][0].per == 12.0 and out["E1"][0].roe == 8.0


class TestReachProblems:
    def test_healthy_when_rows_were_blanked_and_the_panel_shrank(self):
        assert de_exclude_reach_problems(40, {"keep|gbdt": 100, "exclude|gbdt": 90}, ["gbdt"]) == []

    def test_no_blanked_rows_is_a_problem(self):
        """業種名の表記ずれ・元から空欄＝両条件が同じ財務で組まれる。"""
        problems = de_exclude_reach_problems(0, {"keep|gbdt": 100, "exclude|gbdt": 90}, ["gbdt"])
        assert any("0" in p for p in problems)

    def test_unchanged_sample_count_is_a_problem(self):
        """空欄が行の除外まで届いていない（既定の特徴量に de_ratio が無い等）。"""
        problems = de_exclude_reach_problems(40, {"keep|gbdt": 100, "exclude|gbdt": 100}, ["gbdt"])
        assert len(problems) == 1 and "exclude|gbdt" in problems[0]
