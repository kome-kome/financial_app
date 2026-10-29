"""`momentum_gate --earnings-yield`（#1082・親 #950）: 特徴量の per を益利回り E/P に差し替えるモード。

本番（per）を分母に、`financial_metrics.earnings_yield`（per と同じ F・赤字の行にも値が入る）へ差し替えた
条件と共通域で比べる。DB と CV は回さず、条件・財務の差し替え・配線・日中キューの登録を確かめる。
"""
import inspect
from collections import namedtuple

import pytest

import scripts.momentum_gate as G
import scripts.run_daytime as rd

Row = namedtuple("Row", ["period_end", "per", "roe"])


class TestConditions:
    def test_two_conditions_with_production_per_as_base(self):
        conds = G.build_conditions(earnings_yield=True)
        assert list(conds) == ["per", "ep"]
        assert conds["per"].earnings_yield is False and conds["ep"].earnings_yield is True
        assert G.base_of(conds) == G.EARNINGS_YIELD_BASE_COND == "per"
        # 他の軸は本番構成のまま（差は per の列だけ）
        assert conds["per"]._replace(earnings_yield=True) == conds["ep"]

    def test_mode_name_and_suffix(self):
        assert G.mode_of(earnings_yield=True) == "earnings_yield"
        assert G.MODE_SUFFIX["earnings_yield"] == "_earnings_yield"
        assert "earnings_yield" in G._AXIS_VERDICTS

    def test_cannot_be_combined_with_other_modes(self):
        with pytest.raises(ValueError, match="--earnings-yield"):
            G.build_conditions(earnings_yield=True, fin_fill=True)

    def test_m1_never_swaps(self):
        c = G.build_conditions(earnings_yield=True)["ep"]
        assert G.fin_source_key(c, "m1")[3] is False
        assert G.fin_source_key(c, "gbdt")[3] is True


class TestSwap:
    FINS = {"E1": [Row("2024-03-31", 12.5, 0.1), Row("2025-03-31", None, -0.2)],
            "E2": [Row("2025-03-31", 20.0, 0.05)]}
    EY = {("E1", "2024-03-31"): 0.08, ("E1", "2025-03-31"): -0.04}

    def test_per_is_replaced_and_loss_rows_appear(self):
        out, n_swap, n_new = G.swap_per_for_earnings_yield(self.FINS, self.EY)
        assert [r.per for r in out["E1"]] == [0.08, -0.04]
        assert out["E2"][0].per is None          # E/P の無い行は per も空欄（2 つの量を混ぜない）
        assert [r.roe for r in out["E1"]] == [0.1, -0.2]
        assert (n_swap, n_new) == (3, 1)

    def test_original_rows_are_not_touched(self):
        G.swap_per_for_earnings_yield(self.FINS, self.EY)
        assert self.FINS["E1"][0].per == 12.5

    def test_no_new_rows_stops(self):
        conds = G.build_conditions(earnings_yield=True)
        with pytest.raises(SystemExit):
            G.build_fin_sources(conds, ["gbdt"], {"annual": self.FINS}, {}, {})


class TestWiring:
    def test_main_reads_earnings_yields_only_when_needed(self):
        src = inspect.getsource(G.main)
        assert "load_earnings_yields(db)" in src
        assert "fin_source_key(c, MODEL_SPECS[m][2])[3]" in src
        assert "ey_sources)" in src

    def test_daytime_job_runs_the_mode_alone(self):
        job = rd.JOBS["gate:earnings-yield"]
        assert "--earnings-yield" in job.argv and job.parallel_sensitive is True
        assert "--smoke" not in job.argv and job.argv[job.argv.index("--stride") + 1] == "1"
