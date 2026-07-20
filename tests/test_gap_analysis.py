"""plugins/gap_analysis.py のユニットテスト。

純粋: _estimate_ar1_half_life_years（AR(1) MLE 半減期推定）。
execute(): gap_ratio 皆無→空結果（前提条件の弾きは ensure_dependencies 側）/ 回帰メタ（staleness・model）
/ AR(1)・ヒューリスティックの分岐 / ソート。
"""
import math
import os
import sys
from datetime import date

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

    def test_unit_root_phi_gte_1_returns_none_no_exception(self):
        """φ >= 1（単位根）のシリーズ：例外を送出せず None を返す。"""
        # 単調増加列は AR(1) 係数 φ ≈ 1 を与える
        series = [float(i) for i in range(30)]
        result = _estimate_ar1_half_life_years(series)
        assert result is None  # 例外ではなく None

    def test_negative_phi_returns_none_no_exception(self):
        """φ <= 0（負の自己相関）のシリーズ：例外を送出せず None を返す。"""
        # 交互符号列は AR(1) 係数 φ < 0 を与える
        series = [(-1.0) ** i * 10.0 for i in range(20)]
        result = _estimate_ar1_half_life_years(series)
        assert result is None  # 例外ではなく None

    def test_phi_half_known_analytical_half_life(self):
        """φ ≈ 0.5 の正常系：半減期が解析値 1.0 年と数値的に一致する（回帰テスト）。"""
        rng = np.random.default_rng(123)
        phi_true, n = 0.5, 100
        x = [0.0]
        for _ in range(n - 1):
            x.append(phi_true * x[-1] + float(rng.normal(0, 1)))
        result = _estimate_ar1_half_life_years(x)
        assert result is not None
        # φ = 0.5 の解析的半減期 = -log(2)/log(0.5) = 1.0 年
        expected_hl = -math.log(2) / math.log(0.5)
        assert result["half_life_years"] == pytest.approx(expected_hl, abs=0.5)
        assert 0 < result["phi"] < 1


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

class TestExecute:
    def test_no_gap_records_returns_empty(self, db):
        # 前提条件（sector_ols 未実行）の弾きは ensure_dependencies が担うため、execute 自体は
        # 空結果を返す（「該当年度に無い」= エラーではない）。回帰メタは null/空。
        res = plugin.execute(_typed({}), db)
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
        db.add(RegressionResult(edinet_code="E00001", year=2023, period_end=date(2023, 3, 31),
                                predicted_market_cap=12000.0, gap_ratio=10.0,
                                model="ols", sector="情報・通信業", computed_at=old))  # 回帰は古い
        db.commit()
        reg = plugin.execute(_typed({}), db)["regression"]
        assert reg["models"] == ["ols"]
        assert reg["is_stale"] is True                         # 回帰 < データ更新 → stale
        assert reg["computed_at"] is not None and reg["data_updated_at"] is not None

    def test_short_history_uses_heuristic(self, db, make_metric):
        db.add_all([make_metric(edinet_code="E00001", gap_ratio=20.0)])
        db.commit()
        res = plugin.execute(_typed({}), db)
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
        res = plugin.execute(_typed({}), db)
        assert res["n_ar1_estimated"] >= 1
        assert any(r["method"] == "ar1" for r in res["results"])

    def test_delisted_company_excluded(self, db, make_metric):
        # 上場廃止銘柄（is_active=False）は買えないため対象外（Issue #315）。
        db.add_all([
            make_metric(edinet_code="E00001", gap_ratio=50.0, is_active=False),
            make_metric(edinet_code="E00002", gap_ratio=5.0),
        ])
        db.commit()
        res = plugin.execute(_typed({}), db)
        assert res["count"] == 1
        assert res["results"][0]["edinet_code"] == "E00002"

    def test_sort_direction(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", gap_ratio=5.0),
            make_metric(edinet_code="E00002", gap_ratio=25.0),
        ])
        db.commit()
        asc = plugin.execute(_typed({"sort": "asc"}), db)
        assert asc["results"][0]["gap_ratio"] == 5.0
        desc = plugin.execute(_typed({"sort": "desc"}), db)
        assert desc["results"][0]["gap_ratio"] == 25.0


# ── execute(): 期待総リターン系（旧 total_return 吸収）─────────────────────────

class TestTotalReturnColumns:
    def test_total_return_columns_present_and_computed(self, db, make_metric):
        # gap=20% / 株価1000 / EPS50 / BPS800 / 配当利回り3% の単一銘柄。
        db.add(make_metric(edinet_code="E00001", gap_ratio=20.0,
                           stock_price=1000.0, pl_eps=50.0, bs_bps=800.0, div_yield=3.0))
        db.commit()
        row = plugin.execute(_typed({}), db)["results"][0]
        # 期待総リターン = gap + 配当利回り（同次元 %）
        assert row["div_yield_pct"] == 3.0
        assert row["expected_total_return_pct"] == pytest.approx(23.0)
        # 予測株価 = 実株価 × (1 + gap/100) = 1000 × 1.20 = 1200
        assert row["pred_price"] == pytest.approx(1200.0)
        assert row["actual_price"] == pytest.approx(1000.0)
        # implied PER = 予測株価 / EPS = 1200 / 50 = 24.0、PBR = 1200 / 800 = 1.5
        assert row["implied_per"] == pytest.approx(24.0)
        assert row["implied_pbr"] == pytest.approx(1.5)

    def test_div_yield_cap_treats_outlier_as_zero(self, db, make_metric):
        # 異常な配当利回り（>30%）は 0 とみなす
        db.add(make_metric(edinet_code="E00001", gap_ratio=10.0,
                           stock_price=500.0, div_yield=99.0))
        db.commit()
        row = plugin.execute(_typed({}), db)["results"][0]
        assert row["div_yield_pct"] == 0.0
        assert row["expected_total_return_pct"] == pytest.approx(10.0)

    def test_missing_price_yields_null_implied(self, db, make_metric):
        # 株価欠損なら予測株価・implied 倍率は None（期待総リターンは gap+配当で算出可）
        db.add(make_metric(edinet_code="E00001", gap_ratio=15.0,
                           stock_price=None, pl_eps=50.0, div_yield=2.0))
        db.commit()
        row = plugin.execute(_typed({}), db)["results"][0]
        assert row["pred_price"] is None
        assert row["implied_per"] is None
        assert row["expected_total_return_pct"] == pytest.approx(17.0)

    def test_sort_by_total_return(self, db, make_metric):
        # A: gap=20, 配当0 → 総リターン20 / B: gap=5, 配当25→capで0 … 別ケースで明快に。
        # A: gap=10, 配当8 → 18 / B: gap=20, 配当0 → 20。総リターン順なら B が先。
        db.add_all([
            make_metric(edinet_code="E00001", gap_ratio=10.0, stock_price=100.0, div_yield=8.0),
            make_metric(edinet_code="E00002", gap_ratio=20.0, stock_price=100.0, div_yield=0.0),
        ])
        db.commit()
        res = plugin.execute(_typed({"sort": "total_return"}), db)
        trs = [r["expected_total_return_pct"] for r in res["results"]]
        assert trs == sorted(trs, reverse=True)
        assert res["results"][0]["edinet_code"] == "E00002"   # 総リターン20 > 18

    def test_min_div_yield_filter(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", gap_ratio=10.0, stock_price=100.0, div_yield=4.0),
            make_metric(edinet_code="E00002", gap_ratio=10.0, stock_price=100.0, div_yield=1.0),
        ])
        db.commit()
        res = plugin.execute(_typed({"min_div_yield": 3.0}), db)
        assert all(r["div_yield_pct"] >= 3.0 for r in res["results"])
        assert {r["edinet_code"] for r in res["results"]} == {"E00001"}
