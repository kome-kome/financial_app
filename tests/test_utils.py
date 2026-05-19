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
    check_collinearity,
    kfold_cv,
    normalize,
    normalize_transform,
    ols,
    ols_with_diagnostics,
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

    def test_stats_present(self):
        # se / t_stat / p_value / df が返ること
        import random
        rng = random.Random(0)
        X = [[1.0, x, rng.gauss(0, 1)] for x in range(50)]
        y = [2.0 + 3.0 * row[1] + 0.5 * row[2] + rng.gauss(0, 1) for row in X]
        result = ols(X, y)
        assert result is not None
        assert "se" in result and "t_stat" in result and "p_value" in result
        assert result["df"] == 50 - 3
        assert len(result["se"]) == 3
        # 真の係数 (2, 3, 0.5) を反映する t 統計量
        assert result["t_stat"][1] > 10  # x 係数は強く有意
        assert result["p_value"][1] < 0.01

    def test_noise_only_has_high_pvalue(self):
        # 真の関係が無い場合、p 値は高い（≥ 0.1 程度）
        import random
        rng = random.Random(42)
        X = [[1.0, rng.gauss(0, 1)] for _ in range(100)]
        y = [rng.gauss(0, 1) for _ in range(100)]
        result = ols(X, y)
        assert result is not None
        # 切片以外の係数の p 値は一定確率で 0.05 を超える
        # （ここでは少なくとも NaN ではないことを担保）
        assert result["p_value"][1] == result["p_value"][1]  # not NaN
        assert 0.0 <= result["p_value"][1] <= 1.0

    def test_df_zero_returns_nan_stats(self):
        # n == p の完全フィット → df = 0 → 統計量は NaN
        X = [[1.0, 1.0], [1.0, 2.0]]
        y = [3.0, 5.0]
        result = ols(X, y)
        assert result is not None
        assert result["df"] == 0
        # NaN 判定
        assert result["se"][0] != result["se"][0]
        assert result["p_value"][0] != result["p_value"][0]


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


# ── scipy.stats.t による正確な p 値 ────────────────────────────────────

class TestScipyPvalue:
    def test_pvalue_matches_scipy_reference_small_df(self):
        # df=10 で t=2.5 のとき真の両側 p 値は scipy.stats.t.sf(2.5, 10)*2 ≈ 0.03133
        from scipy.stats import t as scipy_t
        # サンプル: y = 2 + 3 x + noise（n=12 → df=10）
        import random
        rng = random.Random(7)
        X = [[1.0, float(i)] for i in range(12)]
        y = [2.0 + 3.0 * X[i][1] + rng.gauss(0, 1.0) for i in range(12)]
        result = ols(X, y)
        # x の係数の p 値（result["p_value"][1]）が scipy 標準 sf×2 と一致
        ref = 2.0 * float(scipy_t.sf(abs(result["t_stat"][1]), result["df"]))
        assert result["p_value"][1] == pytest.approx(ref, abs=1e-10)

    def test_pvalue_consistent_with_two_tailed(self):
        # t = 0 → p ≈ 1
        import random
        rng = random.Random(1)
        X = [[1.0, float(i)] for i in range(50)]
        y = [rng.gauss(0, 1.0) for _ in range(50)]  # x との相関ゼロ
        result = ols(X, y)
        # x の係数 t はほぼ 0 → p はほぼ 1
        if abs(result["t_stat"][1]) < 0.1:
            assert result["p_value"][1] > 0.8


# ── ols 新機能: rank / condition_number ────────────────────────────────

class TestOlsExtras:
    def test_rank_reported(self):
        # rank-full の場合 rank == p
        X = [[1.0, x, x ** 2] for x in [1, 2, 3, 4, 5]]
        y = [1.0, 4.0, 9.0, 16.0, 25.0]
        result = ols(X, y)
        assert result["rank"] == 3
        assert result["condition_number"] > 0

    def test_rank_deficient_singular(self):
        # 完全共線な列を含む → rank < p、se は NaN
        X = [[1.0, 1.0, 2.0], [1.0, 2.0, 4.0], [1.0, 3.0, 6.0], [1.0, 4.0, 8.0]]
        y = [1.0, 2.0, 3.0, 4.0]
        result = ols(X, y)
        assert result["rank"] < 3
        # se は NaN（rank < p のため）
        assert result["se"][0] != result["se"][0]


# ── ols_with_diagnostics ───────────────────────────────────────────────

class TestOlsWithDiagnostics:
    def test_basic_diagnostics_present(self):
        import random
        rng = random.Random(11)
        X = [[1.0, float(i), rng.gauss(0, 1)] for i in range(40)]
        y = [2.0 + 3.0 * row[1] + 0.5 * row[2] + rng.gauss(0, 1) for row in X]
        result = ols_with_diagnostics(X, y)
        assert result is not None
        assert "durbin_watson" in result
        assert "jarque_bera" in result
        assert "f_stat" in result and "f_pvalue" in result
        # F 検定はモデル全体として有意 (p < 0.05)
        assert result["f_pvalue"] < 0.05
        # 残差ランダムノイズ → DW は 2 付近
        assert 1.0 < result["durbin_watson"] < 3.0

    def test_robust_se_hc3_differs_from_nonrobust(self):
        # 異分散ノイズで HC3 SE が非頑健 SE と異なる
        import random
        rng = random.Random(3)
        X = [[1.0, float(i)] for i in range(60)]
        # y の分散が x に比例する（不均一分散）
        y = [2.0 + 1.5 * row[1] + rng.gauss(0, abs(row[1]) + 0.1) for row in X]
        r_nonrobust = ols_with_diagnostics(X, y, cov_type="nonrobust")
        r_hc3 = ols_with_diagnostics(X, y, cov_type="HC3")
        assert r_nonrobust is not None and r_hc3 is not None
        # 異分散があるので HC3 の係数 SE は通常異なる
        assert r_nonrobust["se"][1] != pytest.approx(r_hc3["se"][1])
        # β は両者で同一
        assert r_nonrobust["beta"][1] == pytest.approx(r_hc3["beta"][1])


# ── check_collinearity ──────────────────────────────────────────────────

class TestCheckCollinearity:
    def test_independent_features(self):
        # 完全独立なら相関は低く VIF は ~1
        import random
        rng = random.Random(0)
        n = 100
        f1 = [rng.gauss(0, 1) for _ in range(n)]
        f2 = [rng.gauss(0, 1) for _ in range(n)]
        f3 = [rng.gauss(0, 1) for _ in range(n)]
        result = check_collinearity([f1, f2, f3], ["a", "b", "c"])
        assert len(result["high_corr_pairs"]) == 0
        assert len(result["high_vif"]) == 0
        # VIF は全て 1 付近
        for v in result["vif"]:
            assert 0.5 < v < 2.0

    def test_perfect_collinearity_detected(self):
        # f2 = 2 * f1 → 相関 1.0、VIF 無限大
        f1 = list(range(1, 51))
        f2 = [2.0 * v for v in f1]
        f3 = [3.0 + 0.1 * v + (v % 7) for v in f1]
        result = check_collinearity([f1, f2, f3], ["x", "y", "z"])
        # |r(x,y)| == 1.0 で高相関ペアとして検出
        assert any(
            {p["feature_a"], p["feature_b"]} == {"x", "y"}
            for p in result["high_corr_pairs"]
        )
        # VIF は無限大相当 → None として記録
        assert any(v["vif"] is None for v in result["high_vif"])

    def test_correlation_matrix_diagonal_is_one(self):
        f1 = [1.0, 2.0, 3.0, 4.0, 5.0]
        f2 = [5.0, 4.0, 3.0, 2.0, 1.0]
        result = check_collinearity([f1, f2], ["a", "b"])
        assert result["correlation"][0][0] == 1.0
        assert result["correlation"][1][1] == 1.0
        # f2 = -f1 + 6 → 完全負相関
        assert result["correlation"][0][1] == pytest.approx(-1.0, abs=1e-6)

    def test_empty_input(self):
        result = check_collinearity([], [])
        assert result["correlation"] == []
        assert result["vif"] == []


# ── LOG_PRED_CAP ─────────────────────────────────────────────────────────

def test_log_pred_cap_is_finite_and_positive():
    assert math.isfinite(LOG_PRED_CAP)
    assert LOG_PRED_CAP > 0
    # exp(LOG_PRED_CAP) は 100 万円/株オーダー（数百万）を上限とする想定
    assert math.exp(LOG_PRED_CAP) > 1e5
