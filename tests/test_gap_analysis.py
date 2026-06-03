"""plugins/gap_analysis.py のユニットテスト。

純粋: _estimate_ar1_half_life_years（AR(1) MLE 半減期推定）。
execute(): gap_ratio 皆無→ValueError / AR(1)・ヒューリスティックの分岐 / ソート。
"""
import asyncio
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.gap_analysis import (
    _AR1_MIN_OBS,
    _HL_MAX_YEARS,
    _HL_MIN_YEARS,
    _estimate_ar1_half_life_years,
    plugin,
)


# ── 純粋: AR(1) 半減期推定 ───────────────────────────────────────────────────

class TestEstimateAr1:
    def test_min_obs_constant(self):
        assert _AR1_MIN_OBS == 8

    def test_too_few_observations_returns_none(self):
        assert _estimate_ar1_half_life_years([1.0, 2.0, 1.5, 2.5, 1.0, 2.0, 1.5]) is None

    def test_mean_reverting_series_estimates_valid_phi(self):
        # phi=0.6 の定常 AR(1) 実現値を生成（再現性のため seed 固定）
        rng = np.random.default_rng(42)
        phi, n = 0.6, 60
        x = [0.0]
        for _ in range(n - 1):
            x.append(phi * x[-1] + float(rng.normal(0, 1)))
        res = _estimate_ar1_half_life_years(x)
        assert res is not None
        assert 0 < res["phi"] < 1
        assert _HL_MIN_YEARS <= res["half_life_years"] <= _HL_MAX_YEARS
        assert res["n_obs"] == n

    def test_zero_variance_series_returns_none(self):
        # 定数列は平均回帰条件（0<phi<1）を満たさない → None
        assert _estimate_ar1_half_life_years([5.0] * 20) is None


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

class TestExecute:
    def test_no_gap_records_raises(self, db):
        with pytest.raises(ValueError):
            asyncio.run(plugin.execute({}, db))

    def test_short_history_uses_heuristic(self, db, make_metric):
        db.add_all([make_metric(edinet_code="E00001", gap_ratio=20.0)])
        db.commit()
        res = asyncio.run(plugin.execute({}, db))
        assert res["n_heuristic_fallback"] == 1
        assert res["n_ar1_estimated"] == 0
        row = res["results"][0]
        assert row["method"] == "heuristic"
        # half_life = max(6, min(24, |20|/2)) = 10
        assert row["half_life_months"] == 10.0

    def test_long_history_uses_ar1(self, db, make_metric):
        gaps = [40.0, 28.0, 20.0, 14.0, 10.0, 7.0, 5.0, 4.0, 3.0, 2.0]  # 平均回帰的に減衰
        db.add_all([
            make_metric(edinet_code="E00001", year=2014 + i,
                     period_end=f"{2014 + i}-03-31", gap_ratio=g)
            for i, g in enumerate(gaps)
        ])
        db.commit()
        res = asyncio.run(plugin.execute({}, db))
        assert res["n_ar1_estimated"] >= 1
        assert any(r["method"] == "ar1" for r in res["results"])

    def test_sort_direction(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", gap_ratio=5.0),
            make_metric(edinet_code="E00002", gap_ratio=25.0),
        ])
        db.commit()
        asc = asyncio.run(plugin.execute({"sort": "asc"}, db))
        assert asc["results"][0]["gap_ratio"] == 5.0
        desc = asyncio.run(plugin.execute({"sort": "desc"}, db))
        assert desc["results"][0]["gap_ratio"] == 25.0
