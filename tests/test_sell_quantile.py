"""売り専用の候補 S2（#1158）: 丸めたピンボール損失の L1+L2 分位回帰と、`sell_mu_source_bakeoff --sell-set s2`。

DB と M-4 は回さず、損失と勾配・厳密な分位回帰との一致・合成データでの効き方・リークが無いこと・診断値・
事前登録の照合・日中キューの登録を確かめる。合成パネルは S1 のテスト（下側だけで効く信号）を流用する。
"""
import argparse

import numpy as np
import pytest

import scripts.run_daytime as rd
import scripts.sell_mu_source_bakeoff as B
from plugins.model_candidates import (
    _EN_INNER_CV_PURGED, fit_quantile_enet_path, make_diag, make_elasticnet_fit_predict,
    make_quantile_enet_fit_predict, pinball_loss, quantile_alpha_grid, smoothed_pinball,
)
from scripts import prereg
from tests.test_sell_dedicated import _panel, _short_side

S2_SET = B.SELL_SETS["s2"]
SHA = prereg.body_sha256(prereg.read_document(S2_SET.prereg))


class TestSmoothedPinball:
    def test_gradient_matches_finite_difference(self):
        r = np.random.default_rng(0).standard_normal(200)
        eps = 1e-6
        num = (smoothed_pinball(r + eps)[0] - smoothed_pinball(r - eps)[0]) / (2 * eps)
        assert np.max(np.abs(num - smoothed_pinball(r)[1])) < 1e-7

    def test_shrinks_to_the_pinball_loss(self):
        r = np.random.default_rng(1).standard_normal(500)
        assert np.mean(smoothed_pinball(r, h=1e-6)[0]) == pytest.approx(pinball_loss(r), abs=1e-6)

    def test_slopes_outside_the_band(self):
        _, psi = smoothed_pinball(np.array([-5.0, 5.0]), tau=0.2, h=0.1)
        assert psi.tolist() == pytest.approx([-0.8, 0.2])


class TestPath:
    def test_matches_the_exact_quantile_regression(self):
        """h と α が小さければ、線形計画で解いた sklearn の分位回帰とほぼ同じ係数になる。"""
        from sklearn.linear_model import QuantileRegressor

        rng = np.random.default_rng(2)
        X = rng.standard_normal((800, 4))
        y = X @ np.array([1.0, -0.5, 0.0, 0.3]) + rng.standard_normal(800) * (1 + 0.5 * (X[:, 0] > 0))
        a = 1e-4
        qr = QuantileRegressor(quantile=0.2, alpha=a, solver="highs").fit(X, y)
        c, b, capped = fit_quantile_enet_path(X, y, [a], 1.0, h=0.01, max_iter=20000, tol=1e-9)
        assert capped == 0
        assert np.max(np.abs(c[0] - qr.coef_)) < 0.02
        assert b[0] == pytest.approx(qr.intercept_, abs=0.02)

    def test_top_of_the_grid_keeps_all_coefficients_zero(self):
        rng = np.random.default_rng(3)
        X = rng.standard_normal((400, 5))
        y = X[:, 0] + rng.standard_normal(400)
        grid = quantile_alpha_grid(X, y, 0.5)
        assert len(grid) == 20 and grid[0] > grid[-1]
        assert grid[-1] == pytest.approx(grid[0] * 1e-3)
        c, _, _ = fit_quantile_enet_path(X, y, grid[:2], 0.5)
        assert np.count_nonzero(c[0]) == 0
        assert np.count_nonzero(c[1]) > 0

    def test_counts_alphas_that_hit_the_iteration_cap(self):
        rng = np.random.default_rng(4)
        X = rng.standard_normal((300, 3))
        y = X[:, 0] + rng.standard_normal(300)
        # 上端の α は係数 0 が厳密な解で、1 回目の更新が 0 なので収束に数える。上端を除いた 4 点は上限に当たる
        grid = quantile_alpha_grid(X, y, 0.5, n_alphas=5)[1:]
        assert fit_quantile_enet_path(X, y, grid, 0.5, max_iter=1, tol=0.0)[2] == 4


class TestS2:
    def _fit(self, fp, seed=0):
        train, groups = _panel(seed)
        test, _ = _panel(seed + 100, n_months=1)
        return fp(train, test, groups)

    def test_picks_up_a_downside_only_signal_better_than_squared_error(self):
        s2 = make_quantile_enet_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        en = make_elasticnet_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        p_s2, y = self._fit(s2)
        p_en, _ = self._fit(en)
        assert _short_side(p_s2, y) > _short_side(p_en, y)
        assert _short_side(p_s2, y) > 0.2

    def test_prediction_sits_in_the_lower_tail(self):
        """下側 20% 点の予測なので、実現値のおよそ 2 割だけが予測を下回る。"""
        p, y = self._fit(make_quantile_enet_fit_predict(inner_cv=_EN_INNER_CV_PURGED))
        below = float(np.mean(np.asarray(y) < np.asarray(p)))
        assert 0.1 < below < 0.3

    def test_test_labels_do_not_leak(self):
        """テストのラベルだけ差し替えても予測はビット一致する。"""
        fp = make_quantile_enet_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        train, groups = _panel(1)
        test, _ = _panel(2, n_months=1)
        flipped = [(x, -t * 7.0) for x, t in test]
        assert fp(train, test, groups)[0] == fp(train, flipped, groups)[0]

    def test_deterministic(self):
        fp = make_quantile_enet_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        assert self._fit(fp)[0] == self._fit(fp)[0]

    def test_needs_train_groups(self):
        fp = make_quantile_enet_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        train, _ = _panel(0, n_months=2)
        with pytest.raises(ValueError, match="train_groups"):
            fp(train, train[:5])

    def test_diagnostics(self):
        diag = make_diag()
        self._fit(make_quantile_enet_fit_predict(inner_cv=_EN_INNER_CV_PURGED, diag=diag))
        assert set(diag) == {"alpha", "l1_ratio", "n_nonzero", "alpha_at_path_min", "alpha_at_path_max",
                             "n_iter_capped"}
        assert diag["l1_ratio"][0] in (0.1, 0.5, 0.9)

    def test_uses_the_production_m6_settings(self, monkeypatch):
        from plugins import macro_enet

        seen = {}
        monkeypatch.setattr(B, "make_quantile_enet_fit_predict", lambda **kw: seen.update(kw) or "fp")
        m6p = {"l1_ratio": macro_enet.MacroEnetPlugin().params_schema()["l1_ratio"]["default"]}
        assert B.sell_candidate_fit_predict(B.SELL_S2, m6p, {}) == "fp"
        assert seen["inner_cv"] == macro_enet._INNER_CV
        assert seen["cv_splits"] == macro_enet._CV_SPLITS
        assert seen["l1_ratios"] == macro_enet.MacroEnetPlugin._l1_ratios(m6p["l1_ratio"])


def _args(**kw):
    base = {"sell_candidates": True, "sell_set": "s2", "prereg": S2_SET.prereg, "prereg_sha": SHA}
    return argparse.Namespace(**{**base, **kw})


class TestSellSet:
    def test_s1_stays_the_default(self):
        """--sell-set を付けない今の日中ジョブ（#1152）は S1 の組のまま。"""
        assert B.SELL_SET_DEFAULT == "s1"
        assert B.SELL_SETS["s1"].candidates == (B.SELL_S1,)
        assert B.SELL_SETS["s1"].prereg == B.SELL_PREREG
        assert B.build_parser().parse_args(["--sell-candidates"]).sell_set == "s1"

    def test_s2_set_is_s2_only_with_its_own_output(self):
        assert S2_SET.candidates == (B.SELL_S2,)
        assert S2_SET.out != B.SELL_SETS["s1"].out
        assert B.sell_alpha(len(S2_SET.candidates)) == pytest.approx(0.025)

    def test_s1_document_does_not_pass_for_s2(self):
        s1_sha = prereg.body_sha256(prereg.read_document(B.SELL_PREREG))
        with pytest.raises(SystemExit, match=S2_SET.prereg):
            B.check_prereg_args(_args(prereg=B.SELL_PREREG, prereg_sha=s1_sha))

    def test_sha_mismatch_exits_2(self):
        with pytest.raises(SystemExit) as e:
            B.check_prereg_args(_args(prereg_sha="0" * 64))
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_match_returns_the_stamp(self):
        stamp = B.check_prereg_args(_args())
        assert stamp["prereg_sha256"] == SHA
        assert stamp["prereg_issue"].endswith("/issues/1158")

    def test_document_records_its_frozen_value(self):
        assert prereg.recorded_sha(prereg.read_document(S2_SET.prereg)) == SHA


class TestDaytimeJob:
    def test_job_passes_the_frozen_document(self):
        job = rd.JOBS["measure:sell-quantile"]
        argv = list(job.argv)
        assert "--sell-candidates" in argv and job.parallel_sensitive is True
        assert argv[argv.index("--sell-set") + 1] == "s2"
        assert argv[argv.index("--prereg") + 1] == S2_SET.prereg
        assert argv[argv.index("--prereg-sha") + 1] == SHA
        assert "--smoke" not in argv

    def test_result_section_records_the_decision(self):
        # 2026-11-12 の実走で adopt（採用）。書き写した結果を消さない（#1158）。本番の切り替えは #1201
        section = prereg.appendix_section(prereg.read_document(S2_SET.prereg), "### 結果")
        assert not section.startswith(prereg.PLACEHOLDER)
        assert "判定: adopt（採用）" in section
