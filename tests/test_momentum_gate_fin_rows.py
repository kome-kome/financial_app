"""`scripts/momentum_gate.py` の行の基準モード（`--fin-rows`・#424 子3・ADR-0051 決定9）。

TTM 行を学習パネルへ入れるかの昇格ゲート。危ないのは**黙って同じものを比べる**形で、どれも
例外が出ずに「差なし」という結論だけが残る:

  - 検証スクリプトが切替を素通りする（`_load_financials` が `FinancialMetric` を直に読んでいた）
  - TTM の表が空（夜間の再構築の失敗）でも、VIEW の通期側は同じ行を返す
  - TTM 行があっても as-of で1行も選ばれない

このファイルは条件・分母・検定数と、上の2つ目・3つ目を止める純関数を縛る。1つ目は
`tests/test_fin_rows_switch.py` が `_load_financials` 側で縛る。
"""
import inspect
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.macro_snapshots import FIN_ROW_SOURCES  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    FIN_ROWS_BASE_COND, FIN_ROWS_MODELS, MODE_SUFFIX, MODEL_SPECS, MOM_WINDOW, N_TESTS,
    base_of, bonferroni_alpha, build_conditions, count_changed_rows, mode_of, ttm_row_count,
    verdict_text,
)


class TestConditions:
    def test_conditions_are_the_switch_values(self):
        """条件名は切替の値そのもの（書き写すと、切替に値が増えたとき黙って片方だけ測る）。"""
        conds = build_conditions(fin_rows=True)
        assert list(conds) == list(FIN_ROW_SOURCES)
        for name, c in conds.items():
            assert c.fin_rows == name

    def test_other_axes_stay_at_the_production_shape(self):
        """決定9 が問うのは「本番の断面に TTM を足すか」。他の軸は本番構成に固定する。"""
        for c in build_conditions(fin_rows=True).values():
            assert c.use_momentum is False
            assert c.momentum_window == MOM_WINDOW
            assert c.use_macro is True
            assert c.build_interactions is True
            assert c.max_features is None

    def test_baseline_is_the_production_source(self):
        conds = build_conditions(fin_rows=True)
        assert base_of(conds) == FIN_ROWS_BASE_COND == "annual"

    def test_measures_m2_and_m6_on_one_shared_panel(self):
        """決定9: 対象は M-2 / M-6（M-1 は strict でパネルを共有できないので別に参考値）。"""
        assert FIN_ROWS_MODELS == ["xgb_m2", "elasticnet"]
        assert {MODEL_SPECS[m][2] for m in FIN_ROWS_MODELS} == {"gbdt"}

    def test_alpha_is_the_promotion_gate_alpha(self):
        """2モデル × 2指標 × 1条件 = 4検定＝既定の昇格ゲート（ADR-0045）と同じ alpha。"""
        conds = build_conditions(fin_rows=True)
        assert bonferroni_alpha(len(FIN_ROWS_MODELS), len(conds)) == pytest.approx(0.05 / N_TESTS)


class TestOutput:
    def test_mode_and_file(self):
        assert mode_of(fin_rows=True) == "fin_rows"
        assert MODE_SUFFIX["fin_rows"] == "_fin_rows"

    def test_verdict_is_neutral(self):
        """既定を切り替えるかは人が決める（決定9）。判定文は「変えよ」と言わない。"""
        none = verdict_text("fin_rows", 2, [], [])
        assert none == ("FIN ROWS AXIS: with_ttm did not beat the annual-only baseline on the "
                        "common (ym,ec) domain at the corrected alpha")
        hit = verdict_text("fin_rows", 2, ["M-2(XGBoost)/with_ttm/rank_ic"], [])
        assert "PROMOTE" not in hit and "use_momentum" not in hit


class TestMainWiring:
    def test_each_condition_builds_from_its_own_rows(self):
        """**渡し忘れても例外は出ない**（両条件が同じ財務で組まれ、差なしになる）。"""
        src = inspect.getsource(momentum_gate.main)
        assert "fins[c.fin_rows]" in src
        assert "use_fin_rows(src)" in src
        # 行の基準モードでは両方をキャッシュせずに読む（片方だけ古い世代になる事故を構造で防ぐ）
        assert 'use_cache=(mode != "fin_rows")' in src


class TestTtmRowCount:
    def test_counts_the_extra_rows(self):
        fins = {"annual": {"E1": [1, 2], "E2": [1]}, "with_ttm": {"E1": [1, 2, 3], "E2": [1, 2]}}
        assert ttm_row_count(fins) == 2

    def test_zero_when_the_ttm_table_is_empty(self):
        """表が空でも VIEW の通期側は同じ行を返す＝差は 0（ここで止める）。"""
        fins = {"annual": {"E1": [1]}, "with_ttm": {"E1": [1]}}
        assert ttm_row_count(fins) == 0

    def test_none_when_only_one_side_was_loaded(self):
        assert ttm_row_count({"annual": {"E1": [1]}}) is None


def _panel(rows: dict, feats=("a", "b")):
    """{ym: [(ec, [値...]), ...]} から `_build` と同じ形のパネルを作る。"""
    samples = {ym: [(list(vals), 0.0) for _ec, vals in pairs] for ym, pairs in rows.items()}
    ids = {ym: [ec for ec, _vals in pairs] for ym, pairs in rows.items()}
    meta = {ym: [(ec, None) for ec, _vals in pairs] for ym, pairs in rows.items()}
    return samples, meta, ids, list(feats)


class TestCountChangedRows:
    def test_identical_panels_have_no_changes(self):
        p = _panel({"2024-01": [("E1", [1.0, 2.0]), ("E2", [3.0, 4.0])]})
        assert count_changed_rows(p, p) == 0

    def test_counts_rows_whose_features_differ(self):
        a = _panel({"2024-01": [("E1", [1.0, 2.0]), ("E2", [3.0, 4.0])]})
        b = _panel({"2024-01": [("E1", [1.0, 2.0]), ("E2", [3.0, 9.0])]})
        assert count_changed_rows(a, b) == 1

    def test_nan_equals_nan(self):
        """M-2/M-6 は欠損を nan で保持する。nan != nan を差と数えると全行が変わって見える。"""
        a = _panel({"2024-01": [("E1", [math.nan, 2.0])]})
        b = _panel({"2024-01": [("E1", [float("nan"), 2.0])]})
        assert count_changed_rows(a, b) == 0

    def test_nan_versus_a_value_is_a_change(self):
        """TTM で欠損が埋まるのは、まさに数えたい変化。"""
        a = _panel({"2024-01": [("E1", [math.nan, 2.0])]})
        b = _panel({"2024-01": [("E1", [1.5, 2.0])]})
        assert count_changed_rows(a, b) == 1

    def test_rows_present_on_one_side_only_are_not_counted(self):
        """母集団の差は共通域が扱う。ここで見るのは同じ (ym, ec) の中身が変わったかだけ。"""
        a = _panel({"2024-01": [("E1", [1.0, 2.0])], "2024-02": [("E1", [1.0, 2.0])]})
        b = _panel({"2024-01": [("E1", [1.0, 2.0]), ("E9", [7.0, 7.0])]})
        assert count_changed_rows(a, b) == 0

    def test_order_within_a_month_does_not_matter(self):
        a = _panel({"2024-01": [("E1", [1.0, 2.0]), ("E2", [3.0, 4.0])]})
        b = _panel({"2024-01": [("E2", [3.0, 4.0]), ("E1", [1.0, 2.0])]})
        assert count_changed_rows(a, b) == 0

    def test_features_are_matched_by_name(self):
        """M-1 のように条件ごとに選ばれる列が違えば、列の集合が違う行は変わったと数える。"""
        a = _panel({"2024-01": [("E1", [1.0, 2.0])]}, feats=("a", "b"))
        b = _panel({"2024-01": [("E1", [2.0, 1.0])]}, feats=("b", "a"))
        assert count_changed_rows(a, b) == 0
        c = _panel({"2024-01": [("E1", [1.0, 2.0])]}, feats=("a", "c"))
        assert count_changed_rows(a, c) == 1
