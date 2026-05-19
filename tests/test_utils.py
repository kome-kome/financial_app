"""plugins/utils.py の純粋関数テスト（DB・ネットワーク不要）"""
import math
import statistics

import pytest

from plugins.utils import (
    LOG_PRED_CAP,
    kfold_cv,
    normalize,
    normalize_transform,
    ols,
    walk_forward_cv,
    walk_forward_cv_monthly,
    winsorize,
)


# ─── winsorize ────────────────────────────────────────────────────────

class TestWinsorize:
    def test_clips_extreme_high(self):
        # 0..98 の 99 個 + 外れ値 10000 で n=100。
        # p99 ≈ 97 となるため 10000 は確実にクリップされる。
        vals = [float(i) for i in range(99)] + [10000.0]
        clipped, lo, hi = winsorize(vals)
        assert hi < 10000.0
        assert max(clipped) == hi

    def test_clips_extreme_low(self):
        # 外れ値 -10000 + 1..99 の 99 個 で n=100、p1 ≈ 1 で -10000 はクリップされる。
        vals = [-10000.0] + [float(i) for i in range(1, 100)]
        clipped, lo, hi = winsorize(vals)
        assert lo > -10000.0
        assert min(clipped) == lo

    def test_small_sample_returns_unchanged(self):
        vals = [1.0, 2.0, 3.0]  # n < 4
        clipped, lo, hi = winsorize(vals)
        assert clipped == vals
        assert lo == 1.0
        assert hi == 3.0

    def test_preserves_length(self):
        vals = list(range(100))
        clipped, _, _ = winsorize(vals)
        assert len(clipped) == len(vals)

    def test_lo_eq_hi_returns_unchanged(self):
        vals = [5.0] * 100  # 全部同じ値 → lo == hi
        clipped, lo, hi = winsorize(vals)
        assert clipped == vals


# ─── normalize ────────────────────────────────────────────────────────

class TestNormalize:
    def test_zscore_mean_zero(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        normed, mu, sd = normalize(vals, "zscore")
        assert abs(statistics.mean(normed)) < 1e-9
        assert mu == pytest.approx(3.0)
        assert sd == pytest.approx(statistics.stdev(vals))

    def test_log_method(self):
        vals = [1.0, 10.0, 100.0]
        normed, mu, sd = normalize(vals, "log")
        # log で線形になる → z-score 後の平均は 0
        assert abs(statistics.mean(normed)) < 1e-9

    def test_minmax_range(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        normed, mn, r = normalize(vals, "minmax")
        assert min(normed) == 0.0
        assert max(normed) == 1.0
        assert mn == 1.0
        assert r == 4.0

    def test_zero_variance_does_not_crash(self):
        # 全部同じ値 → sd=0 だが 1.0 にフォールバック
        vals = [5.0, 5.0, 5.0]
        normed, mu, sd = normalize(vals, "zscore")
        assert sd == 1.0
        assert all(v == 0.0 for v in normed)


# ─── normalize_transform ──────────────────────────────────────────────

class TestNormalizeTransform:
    def test_zscore_clipping(self):
        # mu=0, sd=1 で巨大値を渡すと ±5 にクリップ
        assert normalize_transform(1000.0, 0.0, 1.0, "zscore") == 5.0
        assert normalize_transform(-1000.0, 0.0, 1.0, "zscore") == -5.0

    def test_zscore_normal_range(self):
        assert normalize_transform(2.0, 0.0, 1.0, "zscore") == 2.0

    def test_log_method(self):
        # log(100) ≈ 4.605
        val = normalize_transform(100.0, 0.0, 1.0, "log")
        assert val == pytest.approx(math.log(100.0), abs=1e-6)


# ─── ols ──────────────────────────────────────────────────────────────

class TestOls:
    def test_perfect_linear_fit(self):
        # y = 2*x + 1（切片付き）
        X = [[1.0, 1.0], [1.0, 2.0], [1.0, 3.0], [1.0, 4.0]]
        y = [3.0, 5.0, 7.0, 9.0]
        result = ols(X, y)
        assert result is not None
        assert result["beta"][0] == pytest.approx(1.0, abs=1e-6)
        assert result["beta"][1] == pytest.approx(2.0, abs=1e-6)
        assert result["r2"] == pytest.approx(1.0, abs=1e-6)
        assert result["rmse"] < 1e-6

    def test_returns_metrics(self):
        X = [[1.0, 1.0], [1.0, 2.0], [1.0, 3.0]]
        y = [2.0, 4.0, 5.0]
        result = ols(X, y)
        assert "beta" in result
        assert "yhat" in result
        assert "r2" in result
        assert "adj_r2" in result
        assert "rmse" in result
        assert "mae" in result

    def test_singular_matrix_returns_none(self):
        # 重複した列で X^T X が特異行列
        X = [[1.0, 1.0, 2.0], [1.0, 2.0, 4.0], [1.0, 3.0, 6.0]]
        y = [1.0, 2.0, 3.0]
        # 逆行列の計算中にゼロ除算が発生 → None または巨大値が返る
        # mat_inv はゼロピボットをハンドリングするが結果は信頼できない
        result = ols(X, y)
        # 例外を投げないことだけ確認
        assert result is None or "beta" in result

    def test_multiple_features(self):
        # y = x1 + x2 + 1
        X = [[1.0, 1.0, 1.0], [1.0, 2.0, 0.0],
             [1.0, 0.0, 3.0], [1.0, 1.0, 2.0],
             [1.0, 3.0, 1.0]]
        y = [3.0, 3.0, 4.0, 4.0, 5.0]
        result = ols(X, y)
        assert result["r2"] == pytest.approx(1.0, abs=1e-6)
        assert result["beta"][0] == pytest.approx(1.0, abs=1e-6)
        assert result["beta"][1] == pytest.approx(1.0, abs=1e-6)
        assert result["beta"][2] == pytest.approx(1.0, abs=1e-6)


# ─── kfold_cv ─────────────────────────────────────────────────────────

class TestKfoldCV:
    def test_basic_run(self):
        # 80サンプル、y ≈ 2*x で 5-fold（log 正規化が効くように正値）
        samples = [([float(i)], 2.0 * i + 10.0) for i in range(1, 81)]
        results = kfold_cv(samples, n_folds=5, y_norm_method="log")
        assert len(results) == 5
        for r in results:
            assert "fold" in r
            assert "n_train" in r
            assert "n_test" in r
            assert "r2" in r
            assert "rmse_pct" in r

    def test_insufficient_data_returns_empty(self):
        # n_folds=5 に対し samples<10 は不足
        samples = [([float(i)], float(i)) for i in range(5)]
        results = kfold_cv(samples, n_folds=5)
        assert results == []

    def test_no_features_returns_empty(self):
        samples = [([], 1.0) for _ in range(20)]
        results = kfold_cv(samples, n_folds=5)
        assert results == []


# ─── walk_forward_cv ──────────────────────────────────────────────────

class TestWalkForwardCV:
    def test_basic_run(self):
        records_by_year = {
            2020: [([float(i)], 2.0 * i + 10.0) for i in range(1, 11)],
            2021: [([float(i)], 2.0 * i + 10.0) for i in range(1, 11)],
            2022: [([float(i)], 2.0 * i + 10.0) for i in range(1, 11)],
            2023: [([float(i)], 2.0 * i + 10.0) for i in range(1, 11)],
        }
        results = walk_forward_cv(records_by_year, feature_names=["x"],
                                  min_train_years=2, n_folds=2,
                                  y_norm_method="log")
        assert len(results) >= 1
        for r in results:
            # ウォークフォワード: 学習年は全てテスト年より過去
            assert all(ty < r["test_year"] for ty in r["train_years"])
            assert r["n_train"] >= 5
            assert r["n_test"] > 0

    def test_insufficient_years_returns_empty(self):
        # 2年しかない場合、min_train_years=2 なら test_year が取れない
        records_by_year = {
            2022: [([1.0], 1.0) for _ in range(10)],
            2023: [([1.0], 1.0) for _ in range(10)],
        }
        results = walk_forward_cv(records_by_year, feature_names=["x"],
                                  min_train_years=2)
        assert results == []


# ─── walk_forward_cv_monthly ──────────────────────────────────────────

class TestWalkForwardCVMonthly:
    def test_basic_run(self):
        # 24ヶ月分のデータ
        samples_by_ym = {}
        for month in range(1, 25):
            ym = f"2023-{month:02d}" if month <= 12 else f"2024-{month-12:02d}"
            samples_by_ym[ym] = [([float(i)], 0.01 * i) for i in range(1, 11)]

        results = walk_forward_cv_monthly(samples_by_ym, feature_names=["x"],
                                          min_train_months=18, step_months=3)
        # min_train_months=18, step=3 → fold が複数走る
        assert len(results) >= 1
        for r in results:
            assert "test_ym" in r
            assert "r2" in r
            assert r["n_train"] >= 5

    def test_insufficient_months_returns_empty(self):
        samples_by_ym = {
            f"2023-{m:02d}": [([1.0], 0.01)] for m in range(1, 13)
        }
        results = walk_forward_cv_monthly(samples_by_ym, feature_names=["x"],
                                          min_train_months=18)
        assert results == []


# ─── LOG_PRED_CAP ─────────────────────────────────────────────────────

def test_log_pred_cap_is_sane():
    """LOG_PRED_CAP の物理的妥当性。exp(15) ≈ 3.27M 円/株（百万円/株）"""
    assert math.exp(LOG_PRED_CAP) < 1e7  # 1000万円/株未満
    assert math.exp(LOG_PRED_CAP) > 1e5  # 10万円/株超
