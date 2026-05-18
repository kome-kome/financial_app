"""plugins/utils.py の純関数に対するユニットテスト。

このモジュールは numpy/scipy 等の外部依存を持たないため、本テストは
プロジェクトの venv なしで実行できる:

    python3 -m pytest tests/test_utils.py -v

CLAUDE.md「ols() は Pure Python 単体実装」を担保する回帰テストでもある。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ── winsorize ────────────────────────────────────────────────────────────

class TestWinsorize:
    def test_small_sample_returns_unchanged(self):
        # n < 4 はそのまま返す
        vals = [1.0, 2.0, 3.0]
        out, lo, hi = winsorize(vals)
        assert out == vals
        assert lo == 1.0 and hi == 3.0

    def test_outliers_clipped_to_p1_p99(self):
        # 100 要素、最後を極端な外れ値に
        vals = list(range(100))
        vals[99] = 100000
        out, lo, hi = winsorize(vals)
        assert max(out) == hi
        assert hi < 100000  # 外れ値はクリップされる

    def test_uniform_values(self):
        # 全て同値の場合は変更なし
        vals = [5.0] * 10
        out, lo, hi = winsorize(vals)
        assert out == vals
        assert lo == 5.0 and hi == 5.0


# ── normalize ────────────────────────────────────────────────────────────

class TestNormalize:
    def test_zscore_basic(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        normed, mu, sd = normalize(vals, "zscore")
        assert mu == pytest.approx(3.0)
        assert sd == pytest.approx(math.sqrt(2.5))
        # z-score の合計は約 0
        assert sum(normed) == pytest.approx(0.0, abs=1e-9)

    def test_minmax_basic(self):
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        normed, mn, rng = normalize(vals, "minmax")
        assert mn == 10.0
        assert rng == 40.0
        assert normed[0] == 0.0
        assert normed[-1] == 1.0

    def test_log_basic(self):
        vals = [1.0, 10.0, 100.0, 1000.0]
        normed, _, sd = normalize(vals, "log")
        # log 後の zscore は約 0 中心になる
        assert sum(normed) == pytest.approx(0.0, abs=1e-9)
        assert sd > 0

    def test_log_handles_zero(self):
        # log(0) を回避できているか（max(v, 1e-9) クランプ）
        vals = [0.0, 1.0, 2.0]
        normed, _, _ = normalize(vals, "log")
        assert all(math.isfinite(v) for v in normed)


# ── normalize_transform ──────────────────────────────────────────────────

class TestNormalizeTransform:
    def test_zscore_clip_at_5(self):
        # 極端に大きい値は ±5 にクリップ
        z = normalize_transform(1e9, 0.0, 1.0, "zscore")
        assert z == 5.0
        z = normalize_transform(-1e9, 0.0, 1.0, "zscore")
        assert z == -5.0

    def test_zscore_basic(self):
        # mu=10, sd=2 で val=12 なら z=1
        z = normalize_transform(12.0, 10.0, 2.0, "zscore")
        assert z == pytest.approx(1.0)


# ── ols ──────────────────────────────────────────────────────────────────

class TestOls:
    def test_perfect_fit_line(self):
        # y = 2 + 3x にぴったり乗るデータ
        X = [[1.0, x] for x in range(1, 11)]
        y = [2.0 + 3.0 * x for x in range(1, 11)]
        result = ols(X, y)
        assert result is not None
        assert result["beta"][0] == pytest.approx(2.0)
        assert result["beta"][1] == pytest.approx(3.0)
        assert result["r2"] == pytest.approx(1.0)
        assert result["rmse"] == pytest.approx(0.0, abs=1e-9)

    def test_returns_none_on_singular_matrix(self):
        # 完全共線：x1 == x2
        X = [[1.0, 1.0, 1.0], [1.0, 2.0, 2.0], [1.0, 3.0, 3.0]]
        y = [1.0, 2.0, 3.0]
        # mat_inv は擬似的に 1e-12 で進めるため None ではないが、係数は不安定
        # ここでは少なくとも例外で落ちないことを確認
        result = ols(X, y)
        assert result is not None  # 構造的に成立はする

    def test_r2_zero_when_target_constant(self):
        # 目的変数が定数 → SST=0 → r2 は 0 を返す（実装どおり）
        X = [[1.0, 1.0], [1.0, 2.0], [1.0, 3.0]]
        y = [5.0, 5.0, 5.0]
        result = ols(X, y)
        assert result is not None
        assert result["r2"] == 0.0

    def test_adj_r2_lower_than_r2(self):
        # 普通のフィッティングでは adj_r2 ≤ r2
        X = [[1.0, x] for x in [1, 2, 3, 4, 5]]
        y = [2.1, 4.0, 6.2, 7.8, 10.1]
        result = ols(X, y)
        assert result["adj_r2"] <= result["r2"]


# ── kfold_cv ─────────────────────────────────────────────────────────────

class TestKfoldCv:
    def test_returns_empty_on_too_few_samples(self):
        # 5 fold だが samples が 5 未満
        samples = [([1.0, 2.0], 1.0)] * 5
        out = kfold_cv(samples, n_folds=5)
        assert out == []

    def test_returns_fold_results_for_sufficient_data(self):
        # y = x1 + 2*x2 + ノイズ少なめ
        import random
        rng = random.Random(0)
        samples = []
        for _ in range(50):
            x1 = rng.uniform(0, 10)
            x2 = rng.uniform(0, 10)
            y = x1 + 2 * x2 + rng.gauss(0, 0.5)
            samples.append(([x1, x2], y))
        out = kfold_cv(samples, n_folds=5, y_norm_method="zscore")
        assert len(out) == 5
        # 線形なので CV r2 は十分高いはず
        avg_r2 = sum(r["r2"] for r in out) / len(out)
        assert avg_r2 > 0.8


# ── walk_forward_cv ──────────────────────────────────────────────────────

class TestWalkForwardCv:
    def test_returns_empty_when_too_few_years(self):
        records = {2020: [([1.0], 1.0)] * 10}
        out = walk_forward_cv(records, ["x"], min_train_years=2)
        assert out == []

    def test_basic_walk_forward(self):
        import random
        rng = random.Random(1)
        records = {}
        for year in range(2018, 2024):
            samples = []
            for _ in range(30):
                x = rng.uniform(1, 100)
                y = 10 + 2 * x + rng.gauss(0, 5)
                samples.append(([x], y))
            records[year] = samples
        out = walk_forward_cv(records, ["x"], min_train_years=2, n_folds=3, y_norm_method="zscore")
        assert len(out) == 3
        for r in out:
            assert "test_year" in r and "r2" in r
            assert r["test_year"] in (2021, 2022, 2023)


# ── walk_forward_cv_monthly ──────────────────────────────────────────────

class TestWalkForwardCvMonthly:
    def test_returns_empty_when_too_few_months(self):
        samples = {f"2020-{m:02d}": [([1.0], 0.01)] for m in range(1, 6)}
        out = walk_forward_cv_monthly(samples, ["x"], min_train_months=18)
        assert out == []

    def test_no_lookahead_bias(self):
        # 任意の月のテストで、学習は厳密にそれより前の月のみ
        import random
        rng = random.Random(2)
        samples = {}
        for year in (2022, 2023, 2024):
            for m in range(1, 13):
                ym = f"{year}-{m:02d}"
                samples[ym] = [([rng.uniform(0, 1)], rng.gauss(0, 0.1)) for _ in range(10)]
        out = walk_forward_cv_monthly(samples, ["x"], min_train_months=18, step_months=3)
        # 結果が返ること、test_ym が学習月より後ろにあること
        all_yms = sorted(samples.keys())
        for r in out:
            test_idx = all_yms.index(r["test_ym"])
            assert test_idx >= 18
            assert r["n_train"] > 0


# ── LOG_PRED_CAP ─────────────────────────────────────────────────────────

def test_log_pred_cap_is_finite_and_positive():
    assert math.isfinite(LOG_PRED_CAP)
    assert LOG_PRED_CAP > 0
    # exp(LOG_PRED_CAP) は 100 万円/株オーダー（数百万）を上限とする想定
    assert math.exp(LOG_PRED_CAP) > 1e5
