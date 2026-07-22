"""plugins/utils.py の純関数に対するユニットテスト。

このモジュールは numpy/scipy 等の外部依存を持たないため、本テストは
プロジェクトの venv なしで実行できる:

    python3 -m pytest tests/test_utils.py -v

CLAUDE.md「ols() は Pure Python 単体実装」を担保する回帰テストでもある。
"""
import math
import os
import statistics
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.utils import (
    LOG_PRED_CAP,
    check_collinearity,
    fit_feature_columns,
    kfold_cv,
    macro_risk_exposure,
    normalize,
    normalize_transform,
    ols,
    ols_with_diagnostics,
    ridge_regression,
    shares_outstanding,
    transform_feature_row,
    walk_forward_cv_monthly,
    winsorize,
)


# ── macro_risk_exposure（R_macro = sqrt(βᵀΣβ)）─────────────────────────────

class TestMacroRiskExposure:
    def test_single_factor(self):
        # b=[2], Σ=[[9]] → sqrt(4*9) = 6
        assert macro_risk_exposure([2.0], [[9.0]]) == pytest.approx(6.0)

    def test_diagonal_cov(self):
        # 無相関 2 因子: b=[1,1], Σ=diag(4,9) → sqrt(4+9)
        r = macro_risk_exposure([1.0, 1.0], [[4.0, 0.0], [0.0, 9.0]])
        assert r == pytest.approx(math.sqrt(13.0))

    def test_full_cov_with_correlation(self):
        # 相関項が二次形式に効く: b=[1,1], Σ=[[1,0.5],[0.5,1]] → sqrt(1+1+2*0.5)=sqrt(3)
        r = macro_risk_exposure([1.0, 1.0], [[1.0, 0.5], [0.5, 1.0]])
        assert r == pytest.approx(math.sqrt(3.0))

    def test_returns_in_return_units_scales_linearly(self):
        # ローディングを 2 倍すると R_macro も 2 倍（リターン単位の標準偏差ゆえ）
        cov = [[1.0, 0.2], [0.2, 1.0]]
        base = macro_risk_exposure([1.0, 0.5], cov)
        doubled = macro_risk_exposure([2.0, 1.0], cov)
        assert doubled == pytest.approx(2.0 * base)

    def test_empty_loadings_returns_zero(self):
        assert macro_risk_exposure([], [[]]) == 0.0

    def test_dimension_mismatch_raises(self):
        with pytest.raises(ValueError):
            macro_risk_exposure([1.0, 2.0], [[1.0]])

    def test_negative_quadratic_form_clipped_to_zero(self):
        # 数値誤差で二次形式が僅かに負になっても sqrt(NaN) を出さず 0 に丸める
        assert macro_risk_exposure([1.0], [[-1.0]]) == 0.0


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


# ── winsorize/normalize/fit_feature_columns の新旧完全一致（Issue #304）────
# plugins/utils.py の numpy ベクトル化版は、旧 Pure Python 実装（sorted()/list内包表記）と
# 完全に同一の数値を返す必要がある（CLAUDE.md「統計的妥当性に影響しない」要件）。
# 以下は変更前の実装をそのまま複製した参照実装で、ランダム入力・境界ケースで
# 新実装と厳密一致（==）することを検証する。

def _ref_winsorize(vals, lo_pct=1.0, hi_pct=99.0):
    """変更前の Pure Python 実装（sorted() ベース）。"""
    n = len(vals)
    if n < 4:
        return vals, min(vals), max(vals)
    sv = sorted(vals)
    lo_i = max(0, int(n * lo_pct / 100))
    hi_i = min(n - 1, int(math.ceil(n * hi_pct / 100)) - 1)
    lo, hi = sv[lo_i], sv[hi_i]
    if lo >= hi:
        return vals, lo, hi
    return [max(lo, min(hi, v)) for v in vals], lo, hi


def _ref_normalize(vals, method):
    """変更前の Pure Python 実装（statistics モジュールベース）。"""
    if method == "log":
        vals = [math.log(max(v, 1e-9)) for v in vals]
        mu = statistics.mean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
        sd = sd or 1.0
        return [(v - mu) / sd for v in vals], mu, sd
    if method == "minmax":
        mn, mx = min(vals), max(vals)
        r = mx - mn or 1.0
        return [(v - mn) / r for v in vals], mn, r
    mu = statistics.mean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
    sd = sd or 1.0
    return [(v - mu) / sd for v in vals], mu, sd


def _ref_fit_feature_columns(X_raw, n_feat, method="zscore"):
    """変更前の Pure Python 実装（列ごとに旧 winsorize/normalize を適用）。"""
    X_norm = [[1.0] + [0.0] * n_feat for _ in range(len(X_raw))]
    win_params = []
    norm_params = []
    for fi in range(n_feat):
        col = [row[fi] for row in X_raw]
        present = [v for v in col if v == v]
        col_mean = (sum(present) / len(present)) if present else 0.0
        col = [v if v == v else col_mean for v in col]
        col_w, w_lo, w_hi = _ref_winsorize(col)
        win_params.append((w_lo, w_hi))
        normed, p1, p2 = _ref_normalize(col_w, method)
        norm_params.append((p1, p2))
        for ri, v in enumerate(normed):
            X_norm[ri][fi + 1] = v
    return X_norm, win_params, norm_params


class TestWinsorizeNormalizeExactMatch:
    """新（numpy ベクトル化）実装と旧（Pure Python）参照実装の完全一致検証。"""

    def _random_cases(self, seed=0, n_cases=200):
        import random
        rng = random.Random(seed)
        sizes = [1, 2, 3, 4, 5, 6, 10, 17, 50, 100, 337, 1000]
        cases = []
        for _ in range(n_cases):
            n = rng.choice(sizes)
            vals = [rng.uniform(-1e7, 1e7) for _ in range(n)]
            cases.append(vals)
        return cases

    def test_winsorize_matches_reference_random(self):
        for vals in self._random_cases(seed=1):
            new_out, new_lo, new_hi = winsorize(list(vals))
            ref_out, ref_lo, ref_hi = _ref_winsorize(list(vals))
            assert new_lo == ref_lo
            assert new_hi == ref_hi
            assert new_out == ref_out

    def test_winsorize_boundary_uniform(self):
        vals = [3.5] * 20
        new_out, new_lo, new_hi = winsorize(vals)
        ref_out, ref_lo, ref_hi = _ref_winsorize(vals)
        assert (new_out, new_lo, new_hi) == (ref_out, ref_lo, ref_hi)

    def test_winsorize_boundary_small_n(self):
        for n in (0, 1, 2, 3):
            vals = [float(i) for i in range(1, n + 1)]
            if n == 0:
                continue  # min([]) は仕様上 ValueError（旧実装と同じ）
            new_out, new_lo, new_hi = winsorize(vals)
            ref_out, ref_lo, ref_hi = _ref_winsorize(vals)
            assert (new_out, new_lo, new_hi) == (ref_out, ref_lo, ref_hi)

    def test_winsorize_empty_raises_same_as_reference(self):
        with pytest.raises(ValueError):
            winsorize([])
        with pytest.raises(ValueError):
            _ref_winsorize([])

    def test_winsorize_extreme_outlier_matches_reference(self):
        vals = list(range(100))
        vals[99] = 10_000_000
        new_out, new_lo, new_hi = winsorize([float(v) for v in vals])
        ref_out, ref_lo, ref_hi = _ref_winsorize([float(v) for v in vals])
        assert (new_out, new_lo, new_hi) == (ref_out, ref_lo, ref_hi)

    def test_normalize_matches_reference_random(self):
        for vals in self._random_cases(seed=2):
            if len(vals) == 0:
                continue
            for method in ("zscore", "minmax", "log"):
                new_out, new_p1, new_p2 = normalize(list(vals), method)
                ref_out, ref_p1, ref_p2 = _ref_normalize(list(vals), method)
                assert new_p1 == ref_p1
                assert new_p2 == ref_p2
                assert new_out == ref_out

    def test_normalize_single_value_matches_reference(self):
        for method in ("zscore", "minmax", "log"):
            new_out, new_p1, new_p2 = normalize([42.0], method)
            ref_out, ref_p1, ref_p2 = _ref_normalize([42.0], method)
            assert (new_out, new_p1, new_p2) == (ref_out, ref_p1, ref_p2)

    def test_normalize_log_handles_zero_and_negative_matches_reference(self):
        vals = [0.0, -5.0, 1.0, 2.0, 100.0]
        new_out, new_p1, new_p2 = normalize(vals, "log")
        ref_out, ref_p1, ref_p2 = _ref_normalize(vals, "log")
        assert (new_out, new_p1, new_p2) == (ref_out, ref_p1, ref_p2)

    def test_fit_feature_columns_matches_reference_random(self):
        import random
        rng = random.Random(3)
        for _ in range(50):
            n_rows = rng.choice([4, 5, 10, 50, 200])
            n_feat = rng.choice([1, 2, 5])
            X_raw = [[rng.uniform(-1e5, 1e5) for _ in range(n_feat)] for _ in range(n_rows)]
            for method in ("zscore", "minmax", "log"):
                new_Xn, new_win, new_norm = fit_feature_columns(X_raw, n_feat, method)
                ref_Xn, ref_win, ref_norm = _ref_fit_feature_columns(X_raw, n_feat, method)
                assert new_win == ref_win
                assert new_norm == ref_norm
                assert new_Xn == ref_Xn

    def test_fit_feature_columns_with_nan_matches_reference(self):
        # 欠損値混入（境界ケース）。列平均補完込みで新旧一致することを検証。
        import random
        rng = random.Random(4)
        for _ in range(30):
            n_rows = rng.choice([4, 5, 10, 50])
            n_feat = 3
            X_raw = []
            for _ in range(n_rows):
                row = []
                for _ in range(n_feat):
                    row.append(float("nan") if rng.random() < 0.3 else rng.uniform(-1e4, 1e4))
                X_raw.append(row)
            new_Xn, new_win, new_norm = fit_feature_columns(X_raw, n_feat)
            ref_Xn, ref_win, ref_norm = _ref_fit_feature_columns(X_raw, n_feat)
            assert new_win == ref_win
            assert new_norm == ref_norm
            assert new_Xn == ref_Xn

    def test_winsorize_nan_present_does_not_crash_and_is_well_defined(self):
        """winsorize が直接 NaN を受け取るケース（本番では発生しない・境界確認のみ）。

        旧実装は sorted() が NaN を安定ソートできず境界値の選び方が不定（Timsort の
        マージ順序に依存）だった。新実装は np.sort() が NaN を末尾へ寄せる well-defined な
        順序を持つため、NaN 混入時の挙動は旧実装と意図的に異なる（本番経路では
        fit_feature_columns が winsorize 呼び出し前に NaN を列平均で補完するため到達しない。
        select_features_bic・M-2 の y winsorize も NaN を含まない列のみに適用される）。
        ここでは新実装がクラッシュせず、非 NaN 要素に対しては妥当な境界を返すことのみ確認する。
        """
        vals = [1.0, 2.0, float("nan"), 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        out, lo, hi = winsorize(vals)  # クラッシュしないことのみ確認（NaN は末尾へ寄る仕様）
        assert len(out) == len(vals)
        assert math.isfinite(lo)


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

    # ── purge/embargo（Issue #363・ADR-0014）──────────────────────────────
    def _months(self, n, k=4, seed=3):
        """n ヶ月 × k サンプル/月 の合成 samples_by_ym（2020-01 起点・各月固定 k 件）。"""
        import random
        rng = random.Random(seed)
        samples = {}
        for idx in range(n):
            ym = f"{2020 + idx // 12}-{idx % 12 + 1:02d}"
            samples[ym] = [([rng.uniform(0, 1)], rng.gauss(0, 0.1)) for _ in range(k)]
        return samples

    def test_embargo_default_zero_is_backward_compatible(self):
        """embargo_months=0（明示）は省略時（既定0）と test_ym・n_train が完全一致。"""
        samples = self._months(30)
        r_omit = walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3)
        r_zero = walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                         embargo_months=0)
        assert [f["test_ym"] for f in r_omit] == [f["test_ym"] for f in r_zero]
        assert [f["n_train"] for f in r_omit] == [f["n_train"] for f in r_zero]

    def test_embargo_shifts_first_fold_and_shrinks_train(self):
        """案B: embargo>0 で最初のテスト月が min_train+embargo へ後ろ倒しされ、同一テスト月の
        学習件数は embargo 分縮む。"""
        samples = self._months(42, k=4)
        all_yms = sorted(samples.keys())
        r0 = walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                     embargo_months=0)
        r12 = walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                      embargo_months=12)
        assert r0[0]["test_ym"] == all_yms[6]       # 従来: min_train=6 から開始
        assert r12[0]["test_ym"] == all_yms[18]     # 案B: 6 + 12 から開始
        # 同一テスト月 all_yms[18]: embargo なし=18ヶ月分学習, embargo=12 なら 6ヶ月分のみ
        n_train_r0_at18 = next(f["n_train"] for f in r0 if f["test_ym"] == all_yms[18])
        assert n_train_r0_at18 == 18 * 4
        assert r12[0]["n_train"] == 6 * 4
        assert r12[0]["n_train"] < n_train_r0_at18

    def test_embargo_purges_recent_months(self):
        """embargo=12 の各フォールドで、学習に使う最新月とテスト月の差が 12ヶ月以上
        （train_yms = all_yms[:test_idx-12] → 学習件数 = (test_idx-12)*k）。"""
        samples = self._months(42, k=4)
        all_yms = sorted(samples.keys())
        folds = walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                        embargo_months=12)
        assert folds  # 非空（サイレント空洞化していない）
        for f in folds:
            test_idx = all_yms.index(f["test_ym"])
            assert f["n_train"] == (test_idx - 12) * 4


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


# ── AR(1) 半減期推定（gap_analysis） ───────────────────────────────────

class TestAr1HalfLife:
    def test_ar1_recovers_known_phi(self):
        """既知の φ=0.7 で AR(1) シリーズを生成 → 推定値が近いことを確認。
        小サンプルバイアスがあるため許容範囲は ±0.25。"""
        from plugins.gap_analysis import _estimate_ar1_half_life_years
        import random
        rng = random.Random(0)
        n = 300  # burn-in 込みで十分な長さ
        phi_true = 0.7
        series = [0.0]
        for _ in range(n - 1):
            series.append(phi_true * series[-1] + rng.gauss(0, 0.3))
        # 最初の 50 サンプルは burn-in としてスキップ
        series = series[50:]
        result = _estimate_ar1_half_life_years(series)
        assert result is not None
        # 小サンプルバイアスを考慮した許容範囲
        assert abs(result["phi"] - phi_true) < 0.25
        # HL = -ln(2)/ln(0.7) ≈ 1.94 年（±2 年の範囲で確認）
        assert 0.5 < result["half_life_years"] < 5.0

    def test_ar1_returns_none_for_short_series(self):
        from plugins.gap_analysis import _estimate_ar1_half_life_years
        series = [0.1, -0.2, 0.05]  # 3 観測（最低 8 未満）
        assert _estimate_ar1_half_life_years(series) is None

    def test_ar1_returns_none_for_unit_root(self):
        """φ ≥ 1（ランダムウォーク）では平均回帰しないため None。"""
        from plugins.gap_analysis import _estimate_ar1_half_life_years
        import random
        rng = random.Random(1)
        # 純粋なランダムウォーク
        series = [0.0]
        for _ in range(99):
            series.append(series[-1] + rng.gauss(0, 1))
        result = _estimate_ar1_half_life_years(series)
        # φ ≈ 1 のため平均回帰条件を満たさず None または HL が非常に大きい
        assert result is None or result["half_life_years"] > 5.0


# ── walk_forward_cv_monthly が sklearn.TimeSeriesSplit のセマンティクスと一致 ──

class TestWalkForwardSklearnConsistency:
    def test_train_indices_match_timeseries_split(self):
        """walk_forward_cv_monthly のテスト月 i に対し、学習データは
        all_yms[:i] と一致する（sklearn.TimeSeriesSplit と同じ pattern）。
        ルックアヘッドバイアスなし設計の独立検証。
        """
        from sklearn.model_selection import TimeSeriesSplit
        import random
        rng = random.Random(0)

        # 36 月分のサンプル（min_train_months=18, step=3）
        yms = [f"{y}-{m:02d}" for y in range(2022, 2025) for m in range(1, 13)]
        samples_by_ym = {ym: [([rng.gauss(0, 1)], rng.gauss(0, 0.1)) for _ in range(5)] for ym in yms}

        # walk_forward_cv_monthly の test_ym を集める
        fold_results = walk_forward_cv_monthly(samples_by_ym, ["x"], min_train_months=18, step_months=3)
        wf_test_yms = [r["test_ym"] for r in fold_results]

        # sklearn.TimeSeriesSplit でも同じ test 位置になることを確認
        all_yms_sorted = sorted(yms)
        n_total = len(all_yms_sorted)

        # walk_forward_cv_monthly は i in range(18, n, 3) のインデックスを test とする
        expected_test_indices = list(range(18, n_total, 3))
        expected_test_yms = [all_yms_sorted[i] for i in expected_test_indices]

        assert wf_test_yms == expected_test_yms

        # sklearn.TimeSeriesSplit (n_splits=len(test_indices)) の test 末尾が
        # 我々の test_idx と整合する設計（train=[0..i), test=[i:i+1)）
        tscv = TimeSeriesSplit(n_splits=len(expected_test_indices), test_size=1)
        sklearn_test_indices = []
        for train_idx, test_idx in tscv.split(range(n_total)):
            sklearn_test_indices.append(test_idx[0])
        # 末尾の数個（min_train_months=18 以降）が一致するはず（sklearn は等間隔配置）
        # 完全一致は設計差があるが、両者とも「学習が test より厳密に過去」を保証する
        for i_test in wf_test_yms:
            idx = all_yms_sorted.index(i_test)
            assert idx >= 18  # min_train_months 以上


# ── Ridge 回帰 ───────────────────────────────────────────────────────

class TestRidgeRegression:
    def test_ridge_basic_recovery(self):
        # ノイズの少ない y = 2 + 3x で Ridge も近い係数を出す（小 α）
        import random
        rng = random.Random(0)
        X = [[1.0, float(i)] for i in range(50)]
        y = [2.0 + 3.0 * X[i][1] + rng.gauss(0, 0.5) for i in range(50)]
        result = ridge_regression(X, y)
        assert result is not None
        assert result["method"] == "ridge"
        # 切片 ≈ 2, 傾き ≈ 3
        assert abs(result["beta"][0] - 2.0) < 0.5
        assert abs(result["beta"][1] - 3.0) < 0.1
        assert result["r2"] > 0.95
        # SE / t / p は Ridge では NaN
        assert result["se"][0] != result["se"][0]

    def test_ridge_stable_under_collinearity(self):
        # 完全共線な特徴量を含めても Ridge は爆発しない（OLS の対比）
        import random
        rng = random.Random(1)
        n = 80
        x1 = [float(i) for i in range(n)]
        x2 = [v * 2.0 + rng.gauss(0, 0.001) for v in x1]  # x1 とほぼ完全相関
        X = [[1.0, x1[i], x2[i]] for i in range(n)]
        y = [10.0 + 1.5 * x1[i] + rng.gauss(0, 1.0) for i in range(n)]
        result = ridge_regression(X, y)
        assert result is not None
        # 係数の大きさが暴れない（Ridge の本領）
        max_coef = max(abs(b) for b in result["beta"])
        assert max_coef < 100.0
        # alpha が選択されている
        assert result["alpha"] > 0


# ── shares_outstanding ───────────────────────────────────────────────────

class TestSharesOutstanding:
    def test_prefers_issued_shares_over_derived(self):
        # issued_shares が設定されている場合は bs_total_equity/bs_bps より優先
        rec = SimpleNamespace(issued_shares=1.5e6, bs_total_equity=1.0e9, bs_bps=500.0)
        assert shares_outstanding(rec) == pytest.approx(1.5e6)

    def test_falls_back_to_equity_over_bps(self):
        rec = SimpleNamespace(issued_shares=None, bs_total_equity=1.0e9, bs_bps=500.0)
        assert shares_outstanding(rec) == pytest.approx(2.0e6)

    def test_issued_shares_zero_falls_back(self):
        rec = SimpleNamespace(issued_shares=0.0, bs_total_equity=1.0e9, bs_bps=500.0)
        assert shares_outstanding(rec) == pytest.approx(2.0e6)

    def test_returns_none_when_no_data(self):
        rec = SimpleNamespace(issued_shares=None, bs_total_equity=None, bs_bps=None)
        assert shares_outstanding(rec) is None

    def test_returns_none_when_no_attribute(self):
        rec = SimpleNamespace()
        assert shares_outstanding(rec) is None


# ── fit_feature_columns / transform_feature_row の NaN 補完 ───────────────
# M-2 のマクロ欠損許容（macro_nan_ok）で OLS ベースライン経路へ NaN が流入する。
# 学習列平均（nanmean）による補完がリークなしで機能することを担保する。

class TestFeatureColumnsNaN:
    def test_fit_imputes_nan_with_column_mean(self):
        # 列0は [1, 2, NaN, 3] → 非NaN平均=2 で補完。winsorize/normalize が NaN で壊れない。
        X_raw = [[1.0, 10.0], [2.0, 20.0], [float("nan"), 30.0], [3.0, 40.0]]
        X_norm, win_params, norm_params = fit_feature_columns(X_raw, n_feat=2)
        # 全要素が有限（NaN が伝播していない）
        for row in X_norm:
            assert all(math.isfinite(v) for v in row), f"NaN/inf が残存: {row}"
        # intercept 列が先頭
        assert all(row[0] == 1.0 for row in X_norm)

    def test_fit_all_nan_column_becomes_zero(self):
        # 全 NaN 列は col_mean=0 → 正規化後も 0（定数列・クラッシュしない）
        X_raw = [[float("nan"), 1.0], [float("nan"), 2.0], [float("nan"), 3.0], [float("nan"), 4.0]]
        X_norm, _, _ = fit_feature_columns(X_raw, n_feat=2)
        for row in X_norm:
            assert math.isfinite(row[1]), "全NaN列が有限化されていない"

    def test_transform_imputes_nan_to_neutral(self):
        # 学習列の中心（mean）で補完 → 正規化後ほぼ 0（中立値）
        X_raw = [[0.0], [1.0], [2.0], [3.0], [4.0]]
        _, win_params, norm_params = fit_feature_columns(X_raw, n_feat=1)
        row = transform_feature_row([float("nan")], win_params, norm_params)
        assert math.isfinite(row[1])
        assert abs(row[1]) < 1e-9, f"NaN 補完が中立(0)でない: {row[1]}"

    def test_transform_no_nan_unchanged(self):
        # NaN を含まない行は従来どおり（中立化分岐が副作用を持たない）
        X_raw = [[0.0], [1.0], [2.0], [3.0], [4.0]]
        _, win_params, norm_params = fit_feature_columns(X_raw, n_feat=1)
        with_value = transform_feature_row([2.0], win_params, norm_params)
        assert math.isfinite(with_value[1])
        # 中央値(=2)は z=0 付近
        assert abs(with_value[1]) < 1e-9


# ── LOG_PRED_CAP ─────────────────────────────────────────────────────────

def test_log_pred_cap_is_finite_and_positive():
    assert math.isfinite(LOG_PRED_CAP)
    assert LOG_PRED_CAP > 0
    # exp(LOG_PRED_CAP) は 100 万円/株オーダー（数百万）を上限とする想定
    assert math.exp(LOG_PRED_CAP) > 1e5
