"""売り専用の候補 S1（#1152）: 下位に入る確率を当てるロジスティック回帰と、`sell_mu_source_bakeoff --sell-candidates`。

DB と M-4 は回さず、ラベルの作り方・合成データでの効き方・リークが無いこと・判定の規則・事前登録の照合・
日中キューの登録を確かめる。
"""
import argparse

import numpy as np
import pytest

import scripts.run_daytime as rd
import scripts.sell_mu_source_bakeoff as B
from plugins.model_candidates import (
    _EN_INNER_CV_PURGED, bottom_labels, make_diag, make_elasticnet_fit_predict, make_logistic_bottom_fit_predict,
)
from scripts import prereg

SHA = prereg.body_sha256(prereg.read_document(B.SELL_PREREG))


class TestBottomLabels:
    def test_bottom_fifth_of_each_month(self):
        y = [5, 4, 3, 2, 1, 0, 9, 8, 7, 6,   # 月 1（10 行 → 下位 2 行）
             1, 2, 3, 4, 5]                  # 月 2（5 行 → 下位 1 行）
        lab = bottom_labels(y, [10, 5])
        assert lab.tolist() == [0, 0, 0, 0, 1, 1, 0, 0, 0, 0,
                                1, 0, 0, 0, 0]

    def test_ties_take_the_earlier_rows(self):
        assert bottom_labels([1, 1, 1, 1, 1], [5]).tolist() == [1, 0, 0, 0, 0]

    def test_small_month_has_no_bottom(self):
        assert bottom_labels([3, 1, 2], [3]).tolist() == [0, 0, 0]

    def test_groups_must_cover_the_rows(self):
        with pytest.raises(ValueError, match="合わない"):
            bottom_labels([1, 2, 3], [2])


def _panel(seed, n_months=12, n_per=200):
    """下落の側だけで効く信号 x1（大きいと急落する）と、上側の大きな外れを作る x2 を持つ合成パネル。"""
    rng = np.random.default_rng(seed)
    samples, groups = [], []
    for _ in range(n_months):
        X = rng.standard_normal((n_per, 4))
        y = 0.05 * rng.standard_normal(n_per)
        y = y - 0.4 * (X[:, 1] > 1.0)                    # 下側だけで効く
        y = y + 3.0 * (X[:, 2] > 1.6) * rng.random(n_per)  # 上側の大きな外れ（二乗誤差を引っ張る）
        samples += [(list(map(float, x)), float(t)) for x, t in zip(X, y)]
        groups.append(n_per)
    return samples, groups


def _short_side(pred, y):
    pred, y = np.asarray(pred), np.asarray(y)
    k = len(y) // 5
    return float(y.mean() - y[np.argsort(pred, kind="stable")[:k]].mean())


class TestS1:
    def _fit(self, fp, seed=0):
        train, groups = _panel(seed)
        test, _ = _panel(seed + 100, n_months=1)
        pred, y_te = fp(train, test, groups)
        return pred, y_te

    def test_picks_up_a_downside_only_signal_better_than_squared_error(self):
        s1 = make_logistic_bottom_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        en = make_elasticnet_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        p_s1, y = self._fit(s1)
        p_en, _ = self._fit(en)
        assert _short_side(p_s1, y) > _short_side(p_en, y)
        assert _short_side(p_s1, y) > 0.2

    def test_prediction_is_minus_probability(self):
        p, _ = self._fit(make_logistic_bottom_fit_predict(inner_cv=_EN_INNER_CV_PURGED))
        assert all(-1.0 <= v <= 0.0 for v in p)

    def test_test_labels_do_not_leak(self):
        """テストのラベルだけ差し替えても予測はビット一致する。"""
        fp = make_logistic_bottom_fit_predict(inner_cv=_EN_INNER_CV_PURGED)
        train, groups = _panel(1)
        test, _ = _panel(2, n_months=1)
        flipped = [(x, -t * 7.0) for x, t in test]
        a, _ = fp(train, test, groups)
        b, _ = fp(train, flipped, groups)
        assert a == b

    def test_needs_train_groups(self):
        fp = make_logistic_bottom_fit_predict()
        train, _ = _panel(0, n_months=2)
        with pytest.raises(ValueError, match="train_groups"):
            fp(train, train[:5])

    def test_diagnostics(self):
        diag = make_diag()
        fp = make_logistic_bottom_fit_predict(inner_cv=_EN_INNER_CV_PURGED, diag=diag)
        self._fit(fp)
        assert set(diag) == {"C", "l1_ratio", "n_nonzero", "n_convergence_warnings"}
        assert diag["l1_ratio"][0] in (0.1, 0.5, 0.9)

    def test_uses_the_production_m6_settings(self, monkeypatch):
        from plugins import macro_enet

        seen = {}
        monkeypatch.setattr(B, "make_logistic_bottom_fit_predict", lambda **kw: seen.update(kw) or "fp")
        m6p = {"l1_ratio": macro_enet.MacroEnetPlugin().params_schema()["l1_ratio"]["default"]}
        assert B.sell_candidate_fit_predict(B.SELL_S1, m6p, {}) == "fp"
        assert seen["inner_cv"] == macro_enet._INNER_CV
        assert seen["cv_splits"] == macro_enet._CV_SPLITS
        assert seen["l1_ratios"] == macro_enet.MacroEnetPlugin._l1_ratios(m6p["l1_ratio"])


class TestCommonDomain:
    Y = {("2024-01", "E1"): 0.1, ("2024-01", "E2"): -0.2}

    def test_aligns(self):
        aligned = {("2024-01", "E1"): (-0.3, 0.1), ("2024-01", "E2"): (-0.6, -0.2), ("2024-01", "E3"): (0.0, 0.5)}
        assert B.candidate_on_common(aligned, self.Y, "s1") == {("2024-01", "E1"): -0.3, ("2024-01", "E2"): -0.6}

    def test_missing_row_stops(self):
        with pytest.raises(RuntimeError, match="予測が無い"):
            B.candidate_on_common({("2024-01", "E1"): (-0.3, 0.1)}, self.Y, "s1")

    def test_label_mismatch_stops(self):
        aligned = {("2024-01", "E1"): (-0.3, 0.1), ("2024-01", "E2"): (-0.6, -0.25)}
        with pytest.raises(RuntimeError, match="食い違う"):
            B.candidate_on_common(aligned, self.Y, "s1")


def _sig(mean, p):
    return {"mean": mean, "p_value": p}


class TestDecision:
    A = 0.025

    def test_alpha(self):
        assert B.sell_alpha() == pytest.approx(0.025)

    def test_adopt(self):
        d = B.sell_dedicated_decision(_sig(0.01, 0.01), _sig(-0.01, 0.2), self.A)
        assert d["decision"] == "adopt"

    def test_layer_not_significant_keeps(self):
        assert B.sell_dedicated_decision(_sig(0.01, 0.03), None, self.A)["decision"] == "keep"

    def test_layer_significantly_worse_keeps(self):
        assert B.sell_dedicated_decision(_sig(-0.01, 0.001), None, self.A)["decision"] == "keep"

    def test_whole_universe_veto(self):
        d = B.sell_dedicated_decision(_sig(0.01, 0.01), _sig(-0.01, 0.01), self.A)
        assert d["decision"] == "keep" and d["whole_universe_worse"] is True

    def test_whole_universe_better_does_not_veto(self):
        assert B.sell_dedicated_decision(_sig(0.01, 0.01), _sig(0.02, 0.001), self.A)["decision"] == "adopt"

    def test_missing_tests(self):
        assert B.sell_dedicated_decision(None, None, self.A)["decision"] == "keep"

    def test_overall_picks_larger_layer_gain(self):
        decisions = {"a": {"decision": "adopt"}, "b": {"decision": "adopt"}, "c": {"decision": "keep"}}
        out = B.overall_sell_decision(decisions, {"a": _sig(0.01, 0), "b": _sig(0.02, 0), "c": _sig(0.5, 0)})
        assert out["decision"] == "adopt" and out["chosen"] == "b"
        assert B.overall_sell_decision({"a": {"decision": "keep"}}, {})["chosen"] is None


def _args(**kw):
    base = {"sell_candidates": False, "prereg": None, "prereg_sha": None}
    return argparse.Namespace(**{**base, **kw})


class TestPrereg:
    def test_not_used_without_the_mode(self):
        assert B.check_prereg_args(_args()) is None

    def test_prereg_without_the_mode_stops(self):
        with pytest.raises(SystemExit, match="--sell-candidates"):
            B.check_prereg_args(_args(prereg=B.SELL_PREREG))

    def test_mode_requires_prereg(self):
        with pytest.raises(SystemExit, match="--prereg"):
            B.check_prereg_args(_args(sell_candidates=True))

    def test_other_document_stops(self):
        with pytest.raises(SystemExit, match=B.SELL_PREREG):
            B.check_prereg_args(_args(sell_candidates=True, prereg="docs/prereg/m6-inner-score.md", prereg_sha=SHA))

    def test_sha_mismatch_exits_2(self):
        with pytest.raises(SystemExit) as e:
            B.check_prereg_args(_args(sell_candidates=True, prereg=B.SELL_PREREG, prereg_sha="0" * 64))
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_match_returns_the_stamp(self):
        stamp = B.check_prereg_args(_args(sell_candidates=True, prereg=B.SELL_PREREG, prereg_sha=SHA))
        assert stamp["prereg_sha256"] == SHA
        assert stamp["prereg_issue"].endswith("/issues/1152")

    def test_document_records_its_frozen_value(self):
        assert prereg.recorded_sha(prereg.read_document(B.SELL_PREREG)) == SHA


class TestDaytimeJob:
    def test_job_passes_the_frozen_document(self):
        job = rd.JOBS["measure:sell-dedicated"]
        argv = list(job.argv)
        assert "--sell-candidates" in argv and job.parallel_sensitive is True
        assert argv[argv.index("--prereg") + 1] == B.SELL_PREREG
        assert argv[argv.index("--prereg-sha") + 1] == SHA
        assert "--smoke" not in argv

    def test_result_section_records_the_decision(self):
        # 2026-11-10 の実走で keep（据え置き）。書き写した結果を消さない（#1152）
        section = prereg.appendix_section(prereg.read_document(B.SELL_PREREG), "### 結果")
        assert not section.startswith(prereg.PLACEHOLDER)
        assert "判定: keep（据え置き）" in section


def test_sell_ranking_is_unchanged():
    """本番の売りの並びは判定の後でしか変えない（#1152 の判定は keep＝M-6 の μ̂ のまま）。"""
    from plugins import sell_ranking

    assert sell_ranking.MU_SOURCE == B.SELL_BASE == "macro_enet"
