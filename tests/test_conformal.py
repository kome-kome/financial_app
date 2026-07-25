"""コンフォーマル予測区間（Issue #365）のユニットテスト。

M-2（macro_gbdt）に確実性軸 r1_prime を与える分割コンフォーマルの中核ロジック:
  - conformal_bucket_halfwidths: OOF 残差 |resid| の τ 分位をバケット粒度で集計
  - conformal_halfwidth_for: bucket→sector→global フォールバック解決
  - oof_backtest の honest walk-forward 被覆診断（interval_coverage）
"""
from unittest.mock import patch

import numpy as np
import pytest

from plugins.macro_snapshots import (
    conformal_bucket_halfwidths,
    conformal_halfwidth_for,
    oof_backtest,
    CONFORMAL_TAU,
)


def _resid(vals):
    """|resid| のリスト vals を (yhat, ytrue) ペアへ（yhat=0, ytrue=val≥0 → |resid|=val）。"""
    return [(0.0, v) for v in vals]


class TestBucketHalfwidths:
    def test_bucket_quantile_matches_numpy(self):
        res = {"2020-01": _resid([0.1, 0.2, 0.3, 0.4])}
        meta = {"2020-01": [("X", 10), ("X", 11), ("X", 12), ("X", 13)]}
        d = conformal_bucket_halfwidths(res, meta, tau=0.9, min_bucket=3)
        hw = conformal_halfwidth_for("X", 10, d)
        assert hw == pytest.approx(float(np.quantile([0.1, 0.2, 0.3, 0.4], 0.9)))

    def test_fallback_bucket_to_sector_to_global(self):
        res = {"2020-01": _resid([0.1, 0.2, 0.3, 0.9, 0.8, 0.7])}
        meta = {"2020-01": [("X", 5), ("X", 100), ("X", 100),
                            ("Y", 5), ("Y", 100), ("Y", 100)]}
        d = conformal_bucket_halfwidths(res, meta, tau=0.9, min_bucket=2)
        # X の小型（size=5）は S バケット1標本（<min_bucket）→ X sector へフォールバック
        assert conformal_halfwidth_for("X", 5, d) is not None
        # 未知業種 → global へフォールバック
        assert conformal_halfwidth_for("Z", 5, d) == pytest.approx(d["global"])

    def test_global_always_available_with_residuals(self):
        res = {"2020-01": _resid([0.1, 0.2, 0.3])}
        meta = {"2020-01": [(None, None), (None, None), (None, None)]}
        d = conformal_bucket_halfwidths(res, meta, tau=0.9, min_bucket=100)
        assert d["global"] is not None
        assert conformal_halfwidth_for(None, None, d) == pytest.approx(d["global"])

    def test_empty_data_returns_none(self):
        assert conformal_halfwidth_for("X", 10, {}) is None
        d = conformal_bucket_halfwidths({}, {}, tau=0.9)
        assert d["global"] is None
        assert conformal_halfwidth_for("X", 10, d) is None

    def test_default_tau(self):
        res = {"2020-01": _resid([0.1] * 30)}
        meta = {"2020-01": [("X", 10)] * 30}
        d = conformal_bucket_halfwidths(res, meta)
        assert d["tau"] == CONFORMAL_TAU


class TestOofCoverage:
    def test_coverage_keys_present(self):
        res = {
            "2020-01": _resid(list(np.linspace(0.0, 0.4, 30))),
            "2020-02": _resid(list(np.linspace(0.0, 0.4, 30))),
            "2020-03": _resid(list(np.linspace(0.0, 0.4, 30))),
        }
        bt = oof_backtest(res, n_quantiles=3)
        assert bt["interval_tau"] == CONFORMAL_TAU
        assert bt["interval_halfwidth"] is not None
        assert bt["n_interval_calib"] > 0
        assert 0.0 <= bt["interval_coverage"] <= 1.0

    def test_coverage_near_tau_for_stationary_residuals(self):
        rng = np.random.RandomState(0)
        res = {f"2020-{m:02d}": _resid(list(np.abs(rng.normal(0, 0.1, 200))))
               for m in range(1, 8)}
        bt = oof_backtest(res, n_quantiles=5, tau=0.9)
        assert bt["interval_coverage"] == pytest.approx(0.9, abs=0.05)

    def test_coverage_none_when_insufficient(self):
        res = {"2020-01": _resid([0.1, 0.2])}
        bt = oof_backtest(res, n_quantiles=2)
        assert bt["interval_coverage"] is None
        assert bt["n_interval_calib"] == 0


class TestReadProducerFillsR1Prime:
    def test_read_producer_scores_returns_r1_prime(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database import Base, replace_macro_gbdt_scores
        from plugins.macro_gbdt import plugin as m2

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        replace_macro_gbdt_scores(db, [
            {"edinet_code": "E1", "mu": 0.12, "r1_prime": 0.3},
            {"edinet_code": "E2", "mu": -0.05},
        ], "2026-06-26")

        with patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            out = m2.read_producer_scores(db, macro_snapshot=None)
        assert out["E1"]["mu"] == pytest.approx(0.12)
        assert out["E1"]["r1_prime"] == pytest.approx(0.3)
        assert out["E2"]["r1_prime"] is None
