"""scripts/preset_weight_walkforward.py のユニットテスト（Issue #625・ADR-0059）。

主眼は3つ。

1. **最適化しているスコアが、評価しているスコアと同じもの**であること。推定器が独自の
   標準化を持つと、学習で高めた値と `preset_ic_gate` が測る値が別物になる（#529 と同型）。
2. **学習がテスト月より後（embargo 12か月を含む）を1行も見ない**こと。壊しても重みが
   1ビットも変わらないことで縛る。
3. **プリセットの性格（静的重みの大小順）が推定後も保たれる**こと。

パネル構築（`build_period_panel`）は DB フルロードが要るのでここでは触らない（合成パネル）。
"""
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.macro_snapshots import LABEL_HORIZON_MONTHS, _avg_ranks
from plugins.recommend import PRESETS
from scripts import preset_weight_walkforward as wf
from scripts.preset_ic_gate import _panel_rows, build_view_stats, collect_weights, score_period

# パネルの列（build_period_panel(with_gap_ratio=True) と同じ並び）
FACTORS = ["z_roe", "z_op_margin", "z_revenue", "z_cf_ratio",
           "z_equity_ratio", "z_eps", "z_de_ratio", "z_momentum", "gap_ratio"]

# 大小順の仕組みを確かめる検体。**本番の値ではない**（#546 以前の成長重視）。段が4つあり、
# 同値の組（z_op_margin と z_cf_ratio）も持つので、隣り合う段だけを結ぶこと・同値は自由なこと・
# 最下位が上の段で止まることを1つで試せる。本番の成長重視は #546 で「主軸＋同格4指標」の
# 2段になったので、これらのテストを PRESETS に追随させると検証が空振りする。
LAYERED = {"z_revenue": 2.0, "z_roe": 1.0, "z_op_margin": 0.5, "z_cf_ratio": 0.5, "gap_ratio": 0.3}


def _months(start: str, n: int) -> list:
    year, month = map(int, start.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _cross_section(rng, n=80, signal=None):
    """1期分。列ごとに尺度を変え（gap_ratio は％・momentum は生の log return 相当）、
    外れ値も入れる——標準化の経路が効かない合成データでは一致テストが空振りする。"""
    X = rng.normal(size=(n, len(FACTORS)))
    X[:, FACTORS.index("gap_ratio")] = X[:, FACTORS.index("gap_ratio")] * 25 + 40
    X[:, FACTORS.index("z_momentum")] *= 0.3
    X[0, FACTORS.index("z_op_margin")] = -60.0      # 1社の極端値（#469 の形）
    y = rng.normal(size=n)
    for metric, beta in (signal or {}).items():
        col = X[:, FACTORS.index(metric)]
        y = y + beta * (col - col.mean()) / col.std()
    return X, y


def _panel(seed=0, months=None, **kw) -> dict:
    rng = np.random.default_rng(seed)
    months = months or _months("2019-01", 40)
    return {ym: _cross_section(rng, **kw) for ym in months}


def _moments_for(panel: dict, static: dict) -> dict:
    metrics = list(static)
    union = wf.panel_moments(panel, FACTORS, metrics)
    return {ym: wf.sub_moments(m, list(range(len(metrics)))) for ym, m in union.items()}


# ── 1. 最適化するスコア＝評価するスコア ──────────────────────────────────────

class TestScoreMatchesConsumer:
    @pytest.mark.parametrize("name", sorted(PRESETS))
    def test_linear_score_equals_score_period(self, name):
        """任意の非負の重みで `Z w / Σ|w|` が `preset_ic_gate.score_period` と一致する。

        列の統計が他の列の重みに依存しないことが前提。崩れると、代理 IC を高めても
        評価側の rank-IC は別のスコアを測る。
        """
        rng = np.random.default_rng(1)
        X, _y = _cross_section(rng)
        metrics = list(PRESETS[name])
        Z = wf.standardized_matrix(X, FACTORS, metrics)
        for _ in range(5):
            w = dict(zip(metrics, rng.uniform(0.0, 2.0, size=len(metrics))))
            records = _panel_rows(X, FACTORS)
            expected = score_period(records, w, build_view_stats(records, w, FACTORS))
            vec = np.asarray([w[m] for m in metrics])
            got = Z @ vec / np.abs(vec).sum()
            assert got == pytest.approx(expected, abs=1e-12)

    def test_momentum_is_standardized_like_production(self):
        """z_momentum も標準化される（生の log return のまま入れると単位が違う・ADR-0041）。"""
        rng = np.random.default_rng(2)
        X, _y = _cross_section(rng)
        Z = wf.standardized_matrix(X, FACTORS, ["z_momentum"])
        raw = X[:, FACTORS.index("z_momentum")]
        assert not np.allclose(Z[:, 0], raw)
        assert abs(Z[:, 0].mean()) < 0.2

    def test_missing_column_fails_loudly(self):
        rng = np.random.default_rng(3)
        X, _y = _cross_section(rng)
        with pytest.raises(ValueError, match="gap_ratio"):
            wf.standardized_matrix(X[:, :-1], FACTORS[:-1], ["z_roe", "gap_ratio"])

    def test_surrogate_is_pearson_with_ranks(self):
        rng = np.random.default_rng(4)
        X, y = _cross_section(rng)
        metrics = list(PRESETS["バランス型"])
        Z = wf.standardized_matrix(X, FACTORS, metrics)
        mom = wf.period_moments(Z, y)
        ranks = np.asarray(_avg_ranks(list(y)))
        for _ in range(5):
            w = rng.uniform(0.1, 2.0, size=len(metrics))
            expected = np.corrcoef(Z @ w, ranks)[0, 1]
            assert wf.surrogate_ic(w, [mom])[0] == pytest.approx(expected, abs=1e-12)

    def test_gradient_matches_finite_difference(self):
        rng = np.random.default_rng(5)
        panel = _panel(seed=5, months=_months("2020-01", 4))
        moments = [m for m in _moments_for(panel, PRESETS["成長重視"]).values()]
        w = rng.uniform(0.1, 2.0, size=len(PRESETS["成長重視"]))
        _f, grad = wf.surrogate_ic(w, moments)
        eps = 1e-6
        for i in range(len(w)):
            e = np.zeros_like(w)
            e[i] = eps
            num = (wf.surrogate_ic(w + e, moments)[0] - wf.surrogate_ic(w - e, moments)[0]) / (2 * eps)
            assert grad[i] == pytest.approx(num, abs=1e-7)

    def test_constant_ranks_are_not_usable(self):
        Z = np.random.default_rng(6).normal(size=(10, 2))
        assert wf.period_moments(Z, [1.0] * 10) is None


# ── 2. 性格（大小順）──────────────────────────────────────────────────────────

class TestCharacterConstraints:
    def test_order_pairs_link_adjacent_levels_only(self):
        pairs = set(wf.order_pairs(LAYERED))
        assert pairs == {
            ("z_revenue", "z_roe"),
            ("z_roe", "z_op_margin"), ("z_roe", "z_cf_ratio"),
            ("z_op_margin", "gap_ratio"), ("z_cf_ratio", "gap_ratio"),
        }

    def test_tied_weights_get_no_pair(self):
        """同じ重みの指標同士は入れ替わってよい（z_op_margin と z_cf_ratio は 0.5 で同値）。"""
        pairs = set(wf.order_pairs(LAYERED))
        assert ("z_op_margin", "z_cf_ratio") not in pairs
        assert ("z_cf_ratio", "z_op_margin") not in pairs

    @pytest.mark.parametrize("name", sorted(PRESETS))
    def test_fit_keeps_the_character(self, name):
        static = PRESETS[name]
        panel = _panel(seed=7, months=_months("2019-01", 24),
                       signal={"z_eps": 0.3, "gap_ratio": 0.2, "z_de_ratio": -0.2})
        fit = wf.fit_weights(static, list(_moments_for(panel, static).values()))
        assert fit.success, fit.message
        assert set(fit.weights) == set(static)
        assert wf.constraint_violation(fit.weights, static) <= wf.CONSTRAINT_TOL
        assert min(fit.weights.values()) >= 0.0
        assert sum(fit.weights.values()) == pytest.approx(sum(static.values()), abs=1e-9)
        for hi, lo in wf.order_pairs(static):
            assert fit.weights[hi] >= fit.weights[lo] - 1e-9

    def test_every_static_preset_is_estimable(self):
        for name, static in PRESETS.items():
            wf.validate_static(name, static)

    def test_non_positive_static_weight_is_rejected(self):
        with pytest.raises(ValueError, match="正でない"):
            wf.validate_static("x", {"z_roe": 1.0, "z_de_ratio": -0.5})

    def test_constraint_violation_sees_each_kind(self):
        static = {"z_roe": 2.0, "z_op_margin": 1.0}
        assert wf.constraint_violation({"z_roe": 2.0, "z_op_margin": 1.0}, static) == 0.0
        assert wf.constraint_violation({"z_roe": 1.0, "z_op_margin": 2.0}, static) == pytest.approx(1.0)
        assert wf.constraint_violation({"z_roe": 3.5, "z_op_margin": -0.5}, static) == pytest.approx(0.5)
        assert wf.constraint_violation({"z_roe": 2.0, "z_op_margin": 2.0}, static) == pytest.approx(1.0)
        assert wf.constraint_violation({"z_roe": 2.0, "z_op_margin": 1.0, "z_eps": 0.3},
                                       static) == pytest.approx(0.3)


# ── 3. 最適化が効く ─────────────────────────────────────────────────────────

class TestOptimizationMovesTowardTheSignal:
    def test_informative_low_rank_metric_rises_to_its_ceiling(self):
        """最下位の gap_ratio だけが信号を持つとき、gap は上位の水準まで上がり、
        大小順の制約で止まる（上回らない）。代理 IC は静的重みより上がる。"""
        static = LAYERED
        panel = _panel(seed=8, months=_months("2019-01", 30), n=300, signal={"gap_ratio": 1.0})
        fit = wf.fit_weights(static, list(_moments_for(panel, static).values()))
        assert fit.success, fit.message
        w = fit.weights
        ceiling = min(w["z_op_margin"], w["z_cf_ratio"])
        assert w["gap_ratio"] > static["gap_ratio"] * 2
        assert w["gap_ratio"] == pytest.approx(ceiling, abs=1e-5)
        assert fit.objective > fit.static_objective + 0.05

    def test_uninformative_top_metric_cannot_fall_below_the_next(self):
        """主軸（z_revenue）に信号が無くても、z_roe を下回らない（性格が残る）。"""
        static = LAYERED
        panel = _panel(seed=9, months=_months("2019-01", 30), n=300, signal={"z_roe": 1.0})
        fit = wf.fit_weights(static, list(_moments_for(panel, static).values()))
        assert fit.success, fit.message
        assert fit.weights["z_revenue"] >= fit.weights["z_roe"] - 1e-9


# ── 4. 先読みが無い ─────────────────────────────────────────────────────────

class TestNoLookahead:
    def test_embargo_is_the_label_horizon(self):
        assert wf.EMBARGO_MONTHS == LABEL_HORIZON_MONTHS == 12

    def test_train_months_stop_thirteen_months_before_the_test(self):
        yms = _months("2019-01", 48)
        train = wf.train_months(yms, "2022-01")
        assert train[-1] == "2020-12"            # 13か月前は使う
        assert "2021-01" not in train            # 12か月前はまだラベルが確定しない
        assert all(ym < "2021-01" for ym in train)

    def test_boundary_matches_walk_forward_cv_monthly(self):
        """`walk_forward_cv_monthly` の `all_yms[:i - embargo]` と同じ集合（月の抜けが無いとき）。"""
        yms = _months("2019-01", 48)
        for i in range(wf.EMBARGO_MONTHS + 1, len(yms)):
            assert wf.train_months(yms, yms[i]) == yms[:i - wf.EMBARGO_MONTHS]

    def test_missing_months_do_not_shift_the_boundary(self):
        """月が抜けていても暦で切る。並びの位置で12個戻ると、抜けた分だけ境界が過去へずれ、
        確定済みのラベルを捨てる（2021-08..10 が無いと 2021-02 で止まってしまう）。"""
        yms = [ym for ym in _months("2019-01", 48) if not ("2021-08" <= ym <= "2021-10")]
        train = wf.train_months(yms, "2022-06")
        assert train[-1] == "2021-05"
        assert yms[:yms.index("2022-06") - wf.EMBARGO_MONTHS][-1] == "2021-02"

    def test_rolling_window_has_the_requested_length(self):
        yms = _months("2019-01", 48)
        train = wf.train_months(yms, "2022-01", window=6)
        assert train == _months("2020-07", 6)

    def test_poisoning_the_embargo_and_the_future_leaves_the_fold_unchanged(self):
        """テスト月から 12か月以内とそれより後の月を壊しても、その月の重みは1ビットも変わらない。"""
        static = PRESETS["バランス型"]
        months = _months("2019-01", 40)
        panel = _panel(seed=10, months=months, signal={"z_eps": 0.5, "gap_ratio": 0.3})
        test_ym = months[33]
        base = wf.walk_forward(_moments_for(panel, static), static)

        rng = np.random.default_rng(99)
        poisoned = dict(panel)
        for ym in months:
            if wf._month_index(test_ym) - wf._month_index(ym) <= wf.EMBARGO_MONTHS:
                X, y = panel[ym]
                poisoned[ym] = (X * rng.uniform(-50, 50, size=X.shape), -y * 100)
        after = wf.walk_forward(_moments_for(poisoned, static), static)

        assert after[test_ym]["weights"] == base[test_ym]["weights"]
        assert after[test_ym]["train_last"] == months[33 - wf.EMBARGO_MONTHS - 1]
        # 空振りしていない: 壊した月を学習に含む後の fold は変わる
        assert after[months[-1]]["weights"] != base[months[-1]]["weights"]

    def test_first_fold_waits_for_min_train_plus_embargo(self):
        static = PRESETS["高収益重視"]
        months = _months("2019-01", 40)
        folds = wf.walk_forward(_moments_for(_panel(seed=11, months=months), static), static)
        first = min(folds)
        assert first == months[wf.DEFAULT_MIN_TRAIN_MONTHS + wf.EMBARGO_MONTHS]
        assert folds[first]["n_train"] == wf.DEFAULT_MIN_TRAIN_MONTHS


# ── 5. 評価の委譲・決定性・書き出し ──────────────────────────────────────────

def _evaluate(name, seed=12, n_boot=200):
    months = _months("2019-01", 40)
    panel = _panel(seed=seed, months=months, signal={"z_eps": 0.4, "gap_ratio": 0.4})
    union = list(dict.fromkeys(m for s in PRESETS.values() for m in s))
    moments = wf.panel_moments(panel, FACTORS, union)
    return wf.evaluate_preset(name, PRESETS[name], panel, FACTORS, moments, union,
                              min_train=wf.DEFAULT_MIN_TRAIN_MONTHS, window=None, n_boot=n_boot)


class TestEvaluation:
    def test_static_is_measured_on_the_same_months(self):
        res = _evaluate("成長重視")
        assert res["oof_ic_wf"]
        assert set(res["oof_ic_static"]) == set(res["oof_ic_wf"])
        assert res["significance"]["n_common"] == len(res["oof_ic_wf"])

    def test_union_columns_give_the_same_moments_as_the_preset_alone(self):
        """和集合で前計算してから部分を取っても、プリセットの列だけで作ったのと同じ。"""
        panel = _panel(seed=13, months=_months("2019-01", 3))
        static = PRESETS["割安重視"]
        union = list(dict.fromkeys(m for s in PRESETS.values() for m in s))
        idx = [union.index(m) for m in static]
        from_union = {ym: wf.sub_moments(m, idx)
                      for ym, m in wf.panel_moments(panel, FACTORS, union).items()}
        direct = _moments_for(panel, static)
        for ym in panel:
            assert np.allclose(from_union[ym][0], direct[ym][0], atol=1e-12)
            assert np.allclose(from_union[ym][1], direct[ym][1], atol=1e-12)

    def test_failed_folds_are_left_out_not_filled(self):
        panel = _panel(seed=14, months=_months("2019-01", 3))
        yms = sorted(panel)
        folds = {ym: {"weights": dict(PRESETS["バランス型"]), "success": ym != yms[1]}
                 for ym in yms}
        ics = wf.oof_ic(panel, FACTORS, folds)
        assert set(ics) == {yms[0], yms[2]}

    def test_same_input_same_output(self):
        a = json.dumps(_evaluate("割安重視"), sort_keys=True, ensure_ascii=False)
        b = json.dumps(_evaluate("割安重視"), sort_keys=True, ensure_ascii=False)
        assert a == b


class TestOutputs:
    def test_weights_out_is_readable_by_preset_ic_gate(self, tmp_path):
        res = _evaluate("高収益重視")
        path = tmp_path / "w.json"
        path.write_text(json.dumps(wf.weights_out_payload({"高収益重視": res}),
                                   ensure_ascii=False), encoding="utf-8")
        args = SimpleNamespace(preset=None, premia_run_id=None, weights_json=str(path))
        got = collect_weights(None, args)
        assert list(got) == ["高収益重視(wf)"]
        assert got["高収益重視(wf)"] == pytest.approx(res["final"]["weights"])

    def test_failed_final_fit_is_not_written(self):
        res = {"final": {"success": False, "weights": {"z_roe": 1.0}}}
        assert wf.weights_out_payload({"x": res}) == {}

    def test_verdict_uses_corrected_alpha_not_sign(self):
        def r(mean, p):
            return {"significance": {"mean": mean, "p_value": p}}
        alpha = 0.0125
        v = wf.verdict_of({"a": r(+0.02, 0.001), "b": r(-0.03, 0.002), "c": r(+0.05, 0.02),
                           "d": {"significance": None}}, alpha)
        assert v == ("walk-forward significantly BETTER than static: a | "
                     "walk-forward significantly WORSE than static: b (Bonferroni alpha=0.0125)")
        assert "no preset differs" in wf.verdict_of({"c": r(+0.05, 0.02)}, alpha)


class TestDaytimeJob:
    def test_job_runs_this_script_and_writes_its_result(self):
        from scripts import run_daytime as rd
        job = rd.JOBS["wf:preset-weights"]
        assert job.argv[1:3] == ("-m", "scripts.preset_weight_walkforward")
        assert "--json" in job.argv     # 完走してから書き出す成果物
        assert "--cache-panel" not in job.argv   # キャッシュは世代を持たない（#454/#456）
