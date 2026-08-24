"""計測ハーネス `scripts/bench_macro_beta.py` の不変条件（Issue #512）。

このハーネスの目的は「ローカルが GHA の 6.4倍以上遅い」の原因を**測って**切り分けること。
測定器そのものが壊れていると、出てきた数字で原因を誤断定する（#500 で症状から原因を
推測して遠回りした型）。CI で NUTS は回せない（数分〜数時間）ので、**サンプリングを
含まない純粋部分だけ**をここで縛る。

守るのは4点:

1. **合成パネルが決定的**。ローカルと GHA で同一データでなければ A/B の比が意味を持たない
2. **固定費と限界費の分離**（2点回帰）が既知入力で正しい。ここがずれると us/step が全部ずれる
3. **leapfrog 歩数の抽出が実装差に耐える**。`n_steps` / `num_steps` / `tree_depth` の
   どれで来ても拾う＝名前が変わったときに**黙って欠測**しない
4. **環境指紋に必須キーが揃う**。比較の前提（jax 版・デバイス数・スレッド）を後から検算できる
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import bench_macro_beta as bmb  # noqa: E402


class TestSynthPanel:
    """合成パネル: 決定的・形が本番と同型・モデルの入力契約を満たす。"""

    def test_deterministic_for_same_seed(self):
        a = bmb.synth_panel(n_stock=12, n_factor=3, obs_per_stock=4, n_sector=4, seed=0)
        b = bmb.synth_panel(n_stock=12, n_factor=3, obs_per_stock=4, n_sector=4, seed=0)
        # ローカル(Windows)と GHA(Linux)で同一データになることが A/B の前提。
        np.testing.assert_array_equal(a["returns"], b["returns"])
        np.testing.assert_array_equal(a["macro"], b["macro"])

    def test_different_seed_changes_data(self):
        a = bmb.synth_panel(n_stock=12, n_factor=3, obs_per_stock=4, n_sector=4, seed=0)
        b = bmb.synth_panel(n_stock=12, n_factor=3, obs_per_stock=4, n_sector=4, seed=1)
        assert not np.array_equal(a["returns"], b["returns"])

    def test_shapes_match_model_contract(self):
        p = bmb.synth_panel(n_stock=10, n_factor=3, obs_per_stock=5, n_sector=4, seed=0)
        # stock_idx は観測粒度・sector_idx は銘柄粒度（build_hierarchical_model の前提）。
        assert p["n_obs"] == 50
        assert p["macro"].shape == (50, 3)
        assert p["returns"].shape == (50,)
        assert p["stock_idx"].shape == (50,)
        assert p["sector_idx"].shape == (10,)
        assert p["stock_idx"].max() == 9
        assert p["n_sector"] == 4

    def test_sector_count_clamped_to_stock_count(self):
        # 銘柄より多いセクターは作れない（間引きで潰れたセクターも詰める subsample_panel と同じ思想）。
        p = bmb.synth_panel(n_stock=3, n_factor=2, obs_per_stock=2, n_sector=34, seed=0)
        assert p["n_sector"] == 3
        assert p["sector_idx"].max() == 2


class TestTwoPointFit:
    """固定費（コンパイル＋warmup）と 1 draw の限界費の分離。"""

    def test_exact_two_points(self):
        # t = 30 + 0.5 * draws
        fit = bmb.two_point_fit([(50, 55.0), (200, 130.0)])
        assert fit["per_draw_sec"] == pytest.approx(0.5)
        assert fit["fixed_sec"] == pytest.approx(30.0)

    def test_least_squares_with_three_points(self):
        fit = bmb.two_point_fit([(50, 55.0), (100, 80.0), (200, 130.0)])
        assert fit["per_draw_sec"] == pytest.approx(0.5, abs=1e-6)

    def test_returns_none_when_draws_do_not_vary(self):
        # draws が同じ2点では分離できない。**0 や適当な値を返さない**（測れなかったと言う）。
        fit = bmb.two_point_fit([(100, 60.0), (100, 61.0)])
        assert fit["per_draw_sec"] is None
        assert fit["fixed_sec"] is None

    def test_returns_none_for_single_point(self):
        assert bmb.two_point_fit([(100, 60.0)])["per_draw_sec"] is None


class TestExtractSteps:
    """leapfrog 歩数の抽出（実装差に耐えること）。"""

    def test_prefers_n_steps(self):
        got = bmb.extract_steps({"n_steps": np.array([[3.0, 7.0]]), "tree_depth": np.array([[1.0, 1.0]])})
        assert got["source"] == "n_steps"
        np.testing.assert_array_equal(got["steps"], np.array([3.0, 7.0]))

    def test_falls_back_to_num_steps(self):
        got = bmb.extract_steps({"num_steps": np.array([[15.0]])})
        assert got["source"] == "num_steps"
        assert got["steps"][0] == 15.0

    def test_reconstructs_steps_from_tree_depth(self):
        # 深さ d の木は 2**d - 1 歩（歩数が無い実装でも比較可能な量にそろえる）。
        got = bmb.extract_steps({"tree_depth": np.array([[3.0, 10.0]])})
        assert got["source"] == "tree_depth"
        np.testing.assert_array_equal(got["steps"], np.array([7.0, 1023.0]))

    def test_missing_stats_reported_as_none_not_zero(self):
        got = bmb.extract_steps({"lp": np.array([[1.0]])})
        assert got["source"] == "none"
        assert got["steps"].size == 0
        # 「測れなかった」を 0 と区別する＝空なら要約は全部 None。
        assert bmb.summarize_steps(got["steps"])["mean"] is None


class TestPickBest:
    """反復から最速を採る（共有デスクトップの外乱は必ず遅い側へ出る）。"""

    def test_takes_the_fastest_and_keeps_the_spread(self):
        best = bmb.pick_best([
            {"seconds": 3.235, "draws": 75},
            {"seconds": 0.838, "draws": 75},
            {"seconds": 1.431, "draws": 75},
        ])
        assert best["seconds"] == 0.838
        assert best["repeats"] == 3
        # ばらつきを捨てない＝大きければ「その測定を信用しない」判断材料になる。
        assert best["seconds_spread"] == pytest.approx(3.235 / 0.838)
        assert best["seconds_all"] == [3.235, 0.838, 1.431]

    def test_single_repeat_has_spread_one(self):
        best = bmb.pick_best([{"seconds": 2.0, "draws": 25}])
        assert best["seconds"] == 2.0
        assert best["repeats"] == 1
        assert best["seconds_spread"] == pytest.approx(1.0)


class TestSummarizeSteps:
    def test_max_treedepth_rate(self):
        steps = np.array([1023.0, 1023.0, 7.0, 15.0])
        s = bmb.summarize_steps(steps)
        assert s["max_treedepth_rate"] == pytest.approx(0.5)
        assert s["max"] == 1023.0
        assert s["mean"] == pytest.approx(np.mean(steps))


class TestPredictFullScale:
    """フル規模への外挿（仮定を必ず添えること）。"""

    def test_scales_by_n_obs_and_total_iters(self):
        pred = bmb.predict_full_minutes(per_draw_sec=0.1, n_obs_bench=9045)
        scale = bmb.FULL_SCALE["n_obs"] / 9045.0
        expected = 0.1 * scale * (bmb.FULL_SCALE["draws"] + bmb.FULL_SCALE["tune"]) / 60.0
        assert pred["minutes"] == pytest.approx(expected)
        # 突き合わせ先（既知の実測）を必ず持ち歩く。
        assert pred["gha_full_minutes"] == 116.0
        assert pred["local_full_minutes_incomplete"] == 741.5
        assert pred["assumptions"]

    def test_none_when_marginal_cost_unknown(self):
        assert bmb.predict_full_minutes(None, 1000)["minutes"] is None
        assert bmb.predict_full_minutes(0.1, 0)["minutes"] is None


class TestEnvFingerprint:
    def test_required_keys_present(self):
        fp = bmb.env_fingerprint()
        # 比較の前提を後から検算するための最低限（#512 の突合でこれが無くて往復した）。
        for key in ("platform", "python", "cpu_count", "xla_flags"):
            assert key in fp


class TestThreadLimits:
    def test_noop_for_non_positive(self, monkeypatch):
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        monkeypatch.delenv("XLA_FLAGS", raising=False)
        bmb.apply_thread_limits(0)
        assert "OMP_NUM_THREADS" not in os.environ
        assert "XLA_FLAGS" not in os.environ

    def test_sets_thread_env_and_appends_xla_flags(self, monkeypatch):
        monkeypatch.setenv("XLA_FLAGS", "--xla_force_host_platform_device_count=2")
        bmb.apply_thread_limits(1)
        assert os.environ["OMP_NUM_THREADS"] == "1"
        # 既存フラグを消さずに足す（デバイス強制と併用して切り分けたい）。
        assert "--xla_force_host_platform_device_count=2" in os.environ["XLA_FLAGS"]
        assert "intra_op_parallelism_threads=1" in os.environ["XLA_FLAGS"]


class TestFormatReport:
    def test_renders_ascii_only(self):
        record = {
            "label": "A-local", "mode": "synth",
            "panel": {"n_stock": 250, "n_sector": 34, "n_factor": 12, "n_obs": 6000},
            "config": {"chains": 2, "tune": 200, "draws_list": [50, 200], "target_accept": 0.95,
                       "nuts_sampler": "numpyro", "init": "adapt_diag", "chain_method": None,
                       "force_devices": True, "threads": 0, "seed": 0},
            "runs": [{"draws": 50, "seconds": 55.0,
                      "steps": bmb.summarize_steps(np.array([7.0, 15.0])),
                      "n_divergences": 0}],
            "fit": {"fixed_sec": 30.0, "per_draw_sec": 0.5},
            "per_step_us": 12.3, "per_step_us_per_obs": 0.002,
            "predicted_full": bmb.predict_full_minutes(0.5, 6000),
            "stage_sec": {"panel": 0.2, "model_build": 1.0, "sample_total": 55.0},
            "env": {"jax": "0.10.2", "jax_local_device_count": 2, "jax_enable_x64": True,
                    "cpu_count": 6, "pytensor_cxx": "", "pytensor_floatX": "float64",
                    "xla_flags": "--xla_force_host_platform_device_count=2"},
        }
        text = bmb.format_report(record)
        # cp932 コンソールへリダイレクトしても落ちないこと（feedback_windows_cp932_stdout_symbols）。
        text.encode("cp932")
        assert "A-local" in text
        assert "us/leapfrog-step" in text
