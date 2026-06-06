"""depends_on の実行時強制（candidate4）のテスト。

producer の produced_output と registry の ensure_dependencies が、宣言だけだった
depends_on を load-bearing にする（gap_analysis は sector_ols 未実行で DependencyError）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import ensure_dependencies, get_plugin
from plugins.base import AnalysisPlugin, DependencyError
from database import RegressionResult


def _regression_row(**over):
    data = dict(edinet_code="E00001", year=2023, period_end="2023-03-31",
                predicted_market_cap=1.0, gap_ratio=5.0, model="ols", sector="x")
    data.update(over)
    return RegressionResult(**data)


class TestProducedOutput:
    def test_default_is_true(self):
        # 依存を持たない一般 plugin の produced_output はデフォルト True（前提条件なし）
        class _P(AnalysisPlugin):
            name = "p"
            label = "P"
            def params_schema(self):
                return {}
            async def execute(self, params, db):
                return {}
        assert _P().produced_output(db=None) is True

    def test_sector_ols_false_when_empty(self, db):
        assert get_plugin("sector_ols").produced_output(db) is False

    def test_sector_ols_true_when_results_exist(self, db):
        db.add(_regression_row())
        db.commit()
        assert get_plugin("sector_ols").produced_output(db) is True

    def test_sector_ols_false_when_gap_ratio_null(self, db):
        # 予測値はあるが gap_ratio が NULL のみ → 乖離分析の前提は未充足
        db.add(_regression_row(gap_ratio=None))
        db.commit()
        assert get_plugin("sector_ols").produced_output(db) is False


class TestEnsureDependencies:
    def test_raises_when_dependency_unsatisfied(self, db):
        gap = get_plugin("gap_analysis")            # depends_on = ["sector_ols"]
        with pytest.raises(DependencyError) as ei:
            ensure_dependencies(gap, db)
        assert ei.value.plugin_name == "gap_analysis"
        assert ei.value.unsatisfied == ["sector_ols"]
        assert "業種別OLS" in str(ei.value)          # producer の label で案内

    def test_passes_when_dependency_satisfied(self, db):
        db.add(_regression_row())
        db.commit()
        ensure_dependencies(get_plugin("gap_analysis"), db)   # raise しなければ充足

    def test_no_deps_is_noop(self, db):
        ensure_dependencies(get_plugin("sector_ols"), db)     # depends_on=[] → 何もしない
