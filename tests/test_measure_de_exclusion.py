"""`scripts/measure_de_exclusion.py`（#941・#927）: factor premia の D/E を業種除外の VIEW で測る道具。

DB を相手にする部分（トランザクションの中で VIEW を差し替えて取り消す）は実 PG が要るので、ここでは
**黙って別物を測る**形を純関数で縛る:

  - VIEW の SQL の差し替えが `de_ratio` の業種の条件以外まで書き換える／該当が無いのに通る
  - 共通域へ絞るときに、ID と行の並びがずれて別の社の行を残す
  - 安全装置を書き写して、元（`measure_industry_fill_impact.simulated`）の修正が届かなくなる
"""
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from scripts import measure_de_exclusion as mde  # noqa: E402
from scripts import measure_industry_fill_impact as mifi  # noqa: E402

EXTRA = database.DE_RATIO_EXCLUSION_CANDIDATES


class TestExclusionViewSql:
    def test_keep_is_the_main_sql_unchanged(self):
        assert mde.exclusion_view_sql(database.FINANCIAL_METRICS_VIEW_SQL, ()) == \
            database.FINANCIAL_METRICS_VIEW_SQL

    def test_only_the_de_ratio_industry_list_grows(self):
        base = database.FINANCIAL_METRICS_VIEW_SQL
        out = mde.exclusion_view_sql(base, EXTRA)
        old = mde._not_in(database.DE_RATIO_EXCLUDED_INDUSTRIES)
        new = mde._not_in(database.DE_RATIO_EXCLUDED_INDUSTRIES + EXTRA)
        assert out.count(new) == 1 and out.count(old) == 0
        # 差は足した業種名の分だけ（他の式は1文字も変えない）
        assert out.replace(new, old) == base

    @pytest.mark.parametrize("sql", ["SELECT 1", "x NOT IN ('銀行業', '保険業') y NOT IN ('銀行業', '保険業')"])
    def test_refuses_unless_exactly_one_place(self, sql):
        """0 か所なら式が変わった、2 か所以上なら別の式まで書き換える。どちらも黙って別物を測る。"""
        with pytest.raises(ValueError):
            mde.exclusion_view_sql(sql, EXTRA)

    def test_conditions(self):
        assert list(mde.CONDS) == ["keep", "exclude"] and mde.BASE_COND == "keep"
        assert mde.CONDS["keep"] == () and mde.CONDS["exclude"] == EXTRA


class TestSafetyIsShared:
    def test_uses_the_precedent_safety_net_not_a_copy(self):
        """commit/rollback の封じ込めは前例の1か所だけ（書き写すと片方だけ直る）。"""
        assert mde.simulated is mifi.simulated
        assert mde.SimulationBroken is mifi.SimulationBroken


class TestCommonDomain:
    def test_restrict_keeps_the_rows_of_the_given_keys(self):
        panel = {"2024-01": (np.array([[1.0], [2.0], [3.0]]), np.array([0.1, 0.2, 0.3]))}
        ids = {"2024-01": ["E1", "E2", "E3"]}
        out = mde.restrict_panel(panel, ids, {("2024-01", "E1"), ("2024-01", "E3")})
        X, y = out["2024-01"]
        assert X.tolist() == [[1.0], [3.0]] and y.tolist() == [0.1, 0.3]

    def test_months_with_fewer_than_two_rows_are_dropped(self):
        panel = {"2024-01": (np.array([[1.0], [2.0]]), np.array([0.1, 0.2]))}
        out = mde.restrict_panel(panel, {"2024-01": ["E1", "E2"]}, {("2024-01", "E1")})
        assert out == {}

    def test_weights_skip_missing_premia(self):
        r = SimpleNamespace(mean_b={"z_de_ratio": -0.01, "z_roe": 0.02, "z_eps": None})
        assert mde.weights_from_result(r) == {"z_de_ratio": -0.01, "z_roe": 0.02}


def _measured(n_cos: int, drop: set, seed: int):
    """factor premia の結果とパネルの擬似（2因子・6か月）。`drop` の社は持たない。"""
    rng = np.random.default_rng(seed)
    panel, ids = {}, {}
    for m in range(1, 7):
        ym = f"2024-{m:02d}"
        ecs = [f"E{i}" for i in range(n_cos) if f"E{i}" not in drop]
        X = rng.normal(size=(len(ecs), 2))
        y = X[:, 1] * 0.1 + rng.normal(scale=0.1, size=len(ecs))
        panel[ym], ids[ym] = (X, y), ecs
    names = ["z_de_ratio", "z_roe"]
    result = SimpleNamespace(mean_b={"z_de_ratio": -0.01, "z_roe": 0.05},
                             t_stat={"z_de_ratio": -1.0, "z_roe": 2.0},
                             p_value={"z_de_ratio": 0.3, "z_roe": 0.04},
                             n_periods=6, factor_names=names)
    return {"result": result, "panel": panel, "ids": ids, "names": names}


class TestSummarize:
    def test_reports_population_coefficients_and_the_paired_test(self):
        measured = {"keep": _measured(40, set(), 1), "exclude": _measured(40, {"E0", "E1"}, 2)}
        s = mde.summarize(measured)
        assert s["conditions"]["keep"]["companies"] == 40
        assert s["conditions"]["exclude"]["companies"] == 38
        assert s["conditions"]["exclude"]["companies_dropped_vs_keep"] == 2
        assert s["common_rows"] == 38 * 6
        assert s["conditions"]["exclude"]["z_de_ratio"]["mean_b"] == -0.01
        assert set(s["paired_common"]) == {"exclude|keep"}
        assert "in-sample" in s["note"]
