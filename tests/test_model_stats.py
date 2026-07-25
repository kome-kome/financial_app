"""model_stats のユニットテスト（Issue #369: rank-IC 差の有意性検定＋分位単調性）。

すべて stdlib 純関数・seed 固定で決定的（フレーク無し）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_stats as ms


class TestPercentile:
    def test_endpoints_and_interp(self):
        v = [0.0, 1.0, 2.0, 3.0, 4.0]
        assert ms._percentile(v, 0) == 0.0
        assert ms._percentile(v, 100) == 4.0
        assert ms._percentile(v, 50) == 2.0
        assert ms._percentile(v, 25) == 1.0

    def test_single_and_empty(self):
        assert ms._percentile([7.0], 33) == 7.0
        assert ms._percentile([], 50) != ms._percentile([], 50)  # nan != nan


class TestStationaryBootstrapSample:
    def test_length_and_membership(self):
        import random
        rng = random.Random(0)
        s = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = ms._stationary_bootstrap_sample(s, rng, 3)
        assert len(out) == len(s)
        assert set(out) <= set(s)

    def test_deterministic_same_seed(self):
        import random
        s = [1.0, 2.0, 3.0, 4.0, 5.0]
        a = ms._stationary_bootstrap_sample(s, random.Random(42), 3)
        b = ms._stationary_bootstrap_sample(s, random.Random(42), 3)
        assert a == b


class TestBootstrapMeanCI:
    def test_too_short(self):
        assert ms.bootstrap_mean_ci([0.1]) is None
        assert ms.bootstrap_mean_ci([]) is None

    def test_clearly_positive_series_excludes_zero(self):
        # 全て正で分散小 → CI が 0 を跨がず有意（p 小）
        series = [0.05, 0.06, 0.04, 0.05, 0.05, 0.06, 0.04, 0.05]
        r = ms.bootstrap_mean_ci(series, seed=0)
        assert r["ci_lo"] > 0
        assert r["p_value"] < 0.05
        # Davison-Hinkley フロア: 全リサンプル同符号でも p は厳密 0 にならない
        assert r["p_value"] >= 2 / (r["n_boot"] + 1) - 1e-9
        assert r["mean"] == round(sum(series) / len(series), 6)

    def test_centered_series_not_significant(self):
        # 0 中心で符号混在 → CI が 0 を跨ぎ p 大
        series = [0.05, -0.05, 0.04, -0.04, 0.03, -0.03, 0.02, -0.02]
        r = ms.bootstrap_mean_ci(series, seed=0)
        assert r["ci_lo"] < 0 < r["ci_hi"]
        assert r["p_value"] > 0.05

    def test_deterministic(self):
        s = [0.01, 0.03, -0.01, 0.02, 0.04]
        assert ms.bootstrap_mean_ci(s, seed=7) == ms.bootstrap_mean_ci(s, seed=7)


class TestPairedICSignificance:
    def test_no_common_periods_returns_none(self):
        a = {"2020-01": 0.1, "2020-02": 0.2}
        b = {"2021-01": 0.1}
        assert ms.paired_ic_significance(a, b) is None

    def test_single_common_returns_none(self):
        a = {"2020-01": 0.1, "2020-02": 0.2}
        b = {"2020-01": 0.0, "2021-02": 0.1}
        assert ms.paired_ic_significance(a, b) is None

    def test_a_clearly_better(self):
        yms = [f"2020-{i:02d}" for i in range(1, 9)]
        a = {ym: 0.20 for ym in yms}
        b = {ym: 0.05 for ym in yms}
        r = ms.paired_ic_significance(a, b, seed=0)
        assert r["n_common"] == 8
        assert r["mean"] > 0
        assert r["significant"] is True

    def test_tie_not_significant(self):
        yms = [f"2020-{i:02d}" for i in range(1, 9)]
        a = {ym: 0.10 + (0.01 if i % 2 else -0.01) for i, ym in enumerate(yms)}
        b = {ym: 0.10 for ym in yms}
        r = ms.paired_ic_significance(a, b, seed=0)
        assert r["significant"] is False


class TestSignificanceMatrix:
    def test_structure_and_better(self):
        yms = [f"2020-{i:02d}" for i in range(1, 9)]
        models = {
            "M-1": {ym: 0.05 for ym in yms},
            "M-2": {ym: 0.20 for ym in yms},
        }
        m = ms.significance_matrix(models, seed=0)
        assert m["models"] == ["M-1", "M-2"]
        pair = m["pairs"]["M-1|M-2"]
        assert pair["significant"] is True
        assert pair["better"] == "M-2"      # M-2 の IC が高い
        assert pair["mean_diff"] < 0        # M-1 − M-2 < 0

    def test_no_common_pair_marked(self):
        models = {
            "M-1": {"2020-01": 0.1, "2020-02": 0.1},
            "M-2": {"2099-01": 0.1, "2099-02": 0.1},
        }
        m = ms.significance_matrix(models)
        pair = m["pairs"]["M-1|M-2"]
        assert pair["n_common"] == 0
        assert pair["significant"] is False
        assert pair["better"] is None


class TestMonotonicitySummary:
    def test_perfectly_monotonic(self):
        # 各期で完全単調増加 → Spearman=1, 隣接正順率=1
        r = ms.monotonicity_summary([1.0, 1.0, 1.0, 1.0], adj_increasing=16, adj_total=16)
        assert r["spearman_mean"] == 1.0
        assert r["adjacent_increasing_rate"] == 1.0
        # 全ブートストラップ平均が >0。Davison-Hinkley フロアで厳密 0 でなく ~1/(n_boot+1)。
        assert 0.0 < r["p_value"] < 0.001
        assert r["n_periods"] == 4

    def test_non_monotonic_low_confidence(self):
        # U 字/ノイズで Spearman 平均が 0 付近 → p 大
        r = ms.monotonicity_summary([0.5, -0.5, 0.3, -0.3, 0.1, -0.1],
                                    adj_increasing=6, adj_total=12)
        assert r["adjacent_increasing_rate"] == 0.5
        assert r["p_value"] > 0.05

    def test_empty(self):
        r = ms.monotonicity_summary([], adj_increasing=0, adj_total=0)
        assert r["spearman_mean"] is None
        assert r["adjacent_increasing_rate"] is None
        assert r["p_value"] is None
        assert r["n_periods"] == 0
