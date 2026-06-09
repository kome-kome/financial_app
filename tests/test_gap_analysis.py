"""plugins/gap_analysis.py のユニットテスト。

純粋: _estimate_ar1_half_life_years（AR(1) MLE 半減期推定）。
execute(): gap_ratio 皆無→空結果（前提条件の弾きは ensure_dependencies 側）/ 回帰メタ（staleness・model）
/ AR(1)・ヒューリスティックの分岐 / ソート。
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
from plugins.utils import coerce_params


def _typed(raw):
    """パラメータ契約で coerce のみ行う（依存ゲートは通さず execute 単体を検証する）。"""
    return coerce_params(plugin.params_schema(), raw)


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
    def test_no_gap_records_returns_empty(self, db):
        # 前提条件（sector_ols 未実行）の弾きは ensure_dependencies が担うため、execute 自体は
        # 空結果を返す（「該当年度に無い」= エラーではない）。回帰メタは null/空。
        res = asyncio.run(plugin.execute(_typed({}), db))
        assert res["count"] == 0
        assert res["results"] == []
        assert res["regression"]["computed_at"] is None
        assert res["regression"]["is_stale"] is False
        assert res["regression"]["models"] == []

    def test_regression_meta_reports_models_and_staleness(self, db, make_metric, make_fin):
        from datetime import datetime
        from database import RegressionResult
        old, new = datetime(2020, 1, 1), datetime(2026, 1, 1)
        db.add(make_fin(updated_at=new))                       # 財務データは新しい
        db.add(make_metric(edinet_code="E00001", gap_ratio=10.0))
        db.add(RegressionResult(edinet_code="E00001", year=2023, period_end="2023-03-31",
                                predicted_market_cap=12000.0, gap_ratio=10.0,
                                model="ols", sector="情報・通信業", computed_at=old))  # 回帰は古い
        db.commit()
        reg = asyncio.run(plugin.execute(_typed({}), db))["regression"]
        assert reg["models"] == ["ols"]
        assert reg["is_stale"] is True                         # 回帰 < データ更新 → stale
        assert reg["computed_at"] is not None and reg["data_updated_at"] is not None

    def test_short_history_uses_heuristic(self, db, make_metric):
        db.add_all([make_metric(edinet_code="E00001", gap_ratio=20.0)])
        db.commit()
        res = asyncio.run(plugin.execute(_typed({}), db))
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
        res = asyncio.run(plugin.execute(_typed({}), db))
        assert res["n_ar1_estimated"] >= 1
        assert any(r["method"] == "ar1" for r in res["results"])

    def test_sort_direction(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", gap_ratio=5.0),
            make_metric(edinet_code="E00002", gap_ratio=25.0),
        ])
        db.commit()
        asc = asyncio.run(plugin.execute(_typed({"sort": "asc"}), db))
        assert asc["results"][0]["gap_ratio"] == 5.0
        desc = asyncio.run(plugin.execute(_typed({"sort": "desc"}), db))
        assert desc["results"][0]["gap_ratio"] == 25.0
