"""model_stats のユニットテスト（Issue #369: rank-IC 差の有意性検定＋分位単調性）。

すべて stdlib 純関数・seed 固定で決定的（フレーク無し）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

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


# ── 多重比較の補正と判定規則（Issue #741・ADR-0063）───────────────────────────
# p≈0.019 の差の系列（0.005 < p < 0.05）。α しだいで有意かどうかが切り替わる境目の素材。
MID_DIFFS = [-0.0788, 0.0023, 0.0252, 0.0406, -0.0235, 0.0748, 0.016, -0.075, 0.0093, -0.0796,
             0.0762, 0.0639, 0.0302, -0.0333, -0.017, 0.0779, 0.0228, 0.0645, 0.0407, 0.151,
             0.1023, -0.0088, 0.0743, 0.0698]


def _pair(diffs, offset=0):
    """差が diffs になる2系列（b は 0）。offset で test 期をずらすと共通期の無い組が作れる。"""
    yms = [f"{2018 + offset + i // 4}-{(i % 4) * 3 + 1:02d}" for i in range(len(diffs))]
    return {ym: d for ym, d in zip(yms, diffs)}, {ym: 0.0 for ym in yms}


def _const_pair(level=0.15, n=8):
    """差が一定の組。全リサンプルが同符号＝p は下限 2/(n_boot+1)。"""
    return _pair([level] * n)


class TestAlphaThreading:
    def test_default_alpha_output_unchanged(self):
        # 変更前（2026-09-26 の main）で記録した値。既定 α=0.05 の呼び出し元は数字が1桁も動かない。
        r = ms.paired_ic_significance(*_pair(MID_DIFFS))
        assert (r["mean"], r["ci_lo"], r["ci_hi"], r["p_value"]) == (
            0.026075, 0.003413, 0.050349, 0.019)
        assert r["alpha"] == 0.05
        assert r["significant"] is True

    def test_alpha_switches_significance(self):
        a, b = _pair(MID_DIFFS)
        loose = ms.paired_ic_significance(a, b, alpha=0.05)
        strict = ms.paired_ic_significance(a, b, alpha=0.005)
        assert 0.005 < loose["p_value"] < 0.05
        assert loose["p_value"] == strict["p_value"]        # p は α に依存しない
        assert loose["significant"] is True
        assert strict["significant"] is False
        # CI は判定と同じ α の区間＝α を小さくすると広がる。
        assert strict["ci_lo"] < 0 < loose["ci_lo"]
        assert strict["ci_hi"] > loose["ci_hi"]
        assert strict["alpha"] == 0.005


class TestSignificanceRule:
    """有意＝p < α かつ 同じ α の CI が 0 を跨がない。片方だけでは有意にしない。"""

    @staticmethod
    def _judge(monkeypatch, *, ci_lo, ci_hi, p_value, alpha):
        monkeypatch.setattr(ms, "bootstrap_mean_ci", lambda diffs, **kw: {
            "mean": 0.01, "ci_lo": ci_lo, "ci_hi": ci_hi, "p_value": p_value,
            "n": len(diffs), "n_boot": 2000})
        return ms.paired_ic_significance(*_const_pair(), alpha=alpha)["significant"]

    def test_ci_excludes_zero_but_p_not_below_alpha(self, monkeypatch):
        # 15組・α=0.0033 の境目: 0 以下の標本が3個＝CI は 0 を跨がないが p=0.0040。
        assert self._judge(monkeypatch, ci_lo=0.001, ci_hi=0.02, p_value=0.004,
                           alpha=0.05 / 15) is False

    def test_p_below_alpha_but_ci_includes_zero(self, monkeypatch):
        # p の丸めの端で起きうる形。表示の CI が 0 を含むのに ▲ を出さない（安全装置）。
        assert self._judge(monkeypatch, ci_lo=-0.001, ci_hi=0.02, p_value=0.003,
                           alpha=0.05 / 15) is False

    def test_p_equal_to_alpha_is_not_significant(self, monkeypatch):
        # 4桁丸めの p が α と同値なら有意にしない（スクリプト群の `p < alpha` と同じ向き）。
        assert self._judge(monkeypatch, ci_lo=0.001, ci_hi=0.02, p_value=0.05,
                           alpha=0.05) is False

    def test_both_hold(self, monkeypatch):
        assert self._judge(monkeypatch, ci_lo=0.001, ci_hi=0.02, p_value=0.003,
                           alpha=0.05 / 15) is True
        assert self._judge(monkeypatch, ci_lo=-0.02, ci_hi=-0.001, p_value=0.003,
                           alpha=0.05 / 15) is True


class TestFamilySignificance:
    def test_bonferroni_divides_by_tested_pairs(self):
        fam = ms.paired_family_significance(
            {"x": _const_pair(), "y": _const_pair(0.1), "z": _pair(MID_DIFFS)})
        assert fam["correction"] == "bonferroni"
        assert fam["n_tests"] == 3
        assert fam["family_alpha"] == 0.05
        assert fam["alpha"] == pytest.approx(0.05 / 3)
        assert all(r["alpha"] == fam["alpha"] for r in fam["results"].values())

    def test_correction_turns_a_lone_significant_pair_insignificant(self):
        # #741 の本体: 1組だけなら有意（p≈0.019 < 0.05）でも、10組を同時に検定すると
        # α=0.005 に下がって有意でなくなる。補正が判定に届いていることを直接見る。
        alone = ms.paired_family_significance({"mid": _pair(MID_DIFFS)})
        assert alone["results"]["mid"]["significant"] is True
        pairs = {"mid": _pair(MID_DIFFS)}
        pairs.update({f"c{i}": _const_pair(0.1 + 0.01 * i) for i in range(9)})
        fam = ms.paired_family_significance(pairs)
        assert fam["alpha"] == pytest.approx(0.005)
        assert fam["results"]["mid"]["significant"] is False
        # 差が一定の組は p=0.001（下限）< 0.005 なので補正後も有意のまま。
        assert all(fam["results"][f"c{i}"]["significant"] for i in range(9))

    def test_untestable_pairs_are_not_counted(self):
        a, _ = _pair(MID_DIFFS)
        _, far = _pair(MID_DIFFS, offset=50)     # 共通期ゼロ
        fam = ms.paired_family_significance({"ok": _pair(MID_DIFFS), "na": (a, far)})
        assert fam["n_tests"] == 1
        assert fam["alpha"] == 0.05
        assert fam["results"]["na"] is None

    def test_correction_none_keeps_alpha(self):
        fam = ms.paired_family_significance(
            {"x": _const_pair(), "y": _const_pair(0.1)}, alpha=0.01, correction="none")
        assert fam["alpha"] == 0.01
        assert fam["n_tests"] == 2

    def test_unknown_correction_raises(self):
        with pytest.raises(ValueError):
            ms.paired_family_significance({"x": _const_pair()}, correction="holm")

    def test_alpha_below_p_floor_is_flagged(self):
        # n_boot=99 → p の下限 0.02。3組で α=0.0167 は下限以下＝差が一定でも有意になりえない。
        fam = ms.paired_family_significance(
            {"x": _const_pair(), "y": _const_pair(0.1), "z": _const_pair(0.2)}, n_boot=99)
        assert fam["p_floor"] == 0.02
        assert fam["alpha_below_p_floor"] is True
        assert not any(r["significant"] for r in fam["results"].values())
        default = ms.paired_family_significance({"x": _const_pair(), "y": _const_pair(0.1)})
        assert default["alpha_below_p_floor"] is False

    def test_ci_level_label(self):
        assert ms.ci_level_label(0.05) == "95%"
        assert ms.ci_level_label(0.05 / 15) == "99.67%"
        assert ms.ci_level_label(0.025) == "97.5%"


class TestSignificanceMatrixCorrection:
    def test_matrix_reports_correction(self):
        yms = [f"2020-{i:02d}" for i in range(1, 9)]
        models = {k: {ym: v for ym in yms} for k, v in
                  (("M-1", 0.05), ("M-2", 0.20), ("M-3", 0.10))}
        m = ms.significance_matrix(models)
        assert m["correction"] == "bonferroni"
        assert m["n_tests"] == 3
        assert m["alpha"] == pytest.approx(0.05 / 3)
        assert m["alpha_below_p_floor"] is False
        assert m["pairs"]["M-1|M-2"]["better"] == "M-2"

    def test_matrix_without_correction(self):
        # preset_ic_gate の all-pairs は自前で p < 補正後 α を判定し、CI は 95% で出す。
        yms = [f"2020-{i:02d}" for i in range(1, 9)]
        models = {k: {ym: v for ym in yms} for k, v in (("A", 0.05), ("B", 0.2), ("C", 0.1))}
        m = ms.significance_matrix(models, correction="none")
        assert m["alpha"] == 0.05
        assert m["n_tests"] == 3

    def test_alpha_reaches_the_judgement(self):
        # #741 の症状そのもの: 以前は alpha を渡しても判定は常に 95% 基準だった。
        a, b = _pair(MID_DIFFS)
        strict = ms.significance_matrix({"A": a, "B": b}, alpha=0.005, correction="none")
        assert strict["pairs"]["A|B"]["significant"] is False
        assert strict["pairs"]["A|B"]["better"] is None
        loose = ms.significance_matrix({"A": a, "B": b}, alpha=0.05, correction="none")
        assert loose["pairs"]["A|B"]["significant"] is True
