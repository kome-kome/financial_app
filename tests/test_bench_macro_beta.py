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


class TestRegimeCheck:
    """run 間で NUTS のレジームが揃っているか（違えば比較が成立しない）。"""

    def _run(self, steps_mean, n_div=0):
        return {"steps": {"mean": steps_mean}, "n_divergences": n_div}

    def test_same_regime_passes(self):
        got = bmb.regime_check([self._run(1023.0), self._run(1023.0)])
        assert got["ok"] is True
        assert got["steps_ratio"] == pytest.approx(1.0)

    def test_regime_shift_is_flagged(self):
        # tune=25 の実測: 1023 歩の run と 63 歩の run が混ざり、回帰の傾きが負になった。
        got = bmb.regime_check([self._run(1023.0), self._run(63.1, n_div=78)])
        assert got["ok"] is False
        assert got["steps_ratio"] > 1.2
        assert got["n_divergences_max"] == 78

    def test_divergences_alone_fail_the_check(self):
        # 本番（tune=800）は発散0。出ているなら別の状態を測っている。
        got = bmb.regime_check([self._run(1000.0), self._run(1010.0, n_div=12)])
        assert got["ok"] is False

    def test_single_run_is_undetermined_not_ok(self):
        got = bmb.regime_check([self._run(1023.0)])
        assert got["ok"] is None


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


class TestParseMaxTreeDepth:
    """`--max-tree-depth` のパース（#540）。不正値は silent-drop せず raise する。"""

    def test_single_value(self):
        assert bmb.parse_max_tree_depth("8") == 8

    def test_tuple_is_warmup_then_sampling(self):
        # numpyro は (warmup, sampling) として解釈する＝warmup だけ切り詰める案が測れる。
        assert bmb.parse_max_tree_depth("8,10") == (8, 10)

    def test_none_and_empty_keep_current_behaviour(self):
        # 既定は「現状維持」＝None。0 や 10 を勝手に埋めない（埋めると既定値の変更になる）。
        assert bmb.parse_max_tree_depth(None) is None
        assert bmb.parse_max_tree_depth("") is None
        assert bmb.parse_max_tree_depth("  ") is None

    @pytest.mark.parametrize("bad", ["abc", "8,x", "0", "21", "7,8,9", "8.5"])
    def test_invalid_values_raise(self, bad):
        with pytest.raises(ValueError):
            bmb.parse_max_tree_depth(bad)


class TestSamplerKwargs:
    """軌道長の設定が**届く場所**（サンプラーで違う）。

    `nuts={...}` は外部サンプラー経路では捨てられる（pymc/sampling/mcmc.py は target_accept
    しか見ない）＝取り違えるとエラーも警告も出ずに設定だけが効かない。ここで縛る。
    """

    def test_numpyro_goes_through_nuts_sampler_kwargs(self):
        got = bmb.sampler_kwargs("numpyro", max_tree_depth=8)
        assert got["nuts_sampler_kwargs"]["nuts_kwargs"] == {"max_tree_depth": 8}
        assert "nuts" not in got          # numpyro 経路で nuts={} は黙って無視される

    def test_numpyro_passes_tuple_through_unchanged(self):
        got = bmb.sampler_kwargs("numpyro", max_tree_depth=(8, 10))
        assert got["nuts_sampler_kwargs"]["nuts_kwargs"] == {"max_tree_depth": (8, 10)}

    def test_pymc_path_uses_nuts_step_kwarg(self):
        for sampler in (None, "", "pymc"):
            got = bmb.sampler_kwargs(sampler, max_tree_depth=8)
            assert got["nuts"] == {"max_treedepth": 8}
            assert "nuts_sampler_kwargs" not in got

    def test_pymc_tuple_maps_to_early_max_treedepth(self):
        got = bmb.sampler_kwargs("pymc", max_tree_depth=(8, 10))
        assert got["nuts"] == {"early_max_treedepth": 8, "max_treedepth": 10}

    def test_chain_method_is_preserved_alongside(self):
        got = bmb.sampler_kwargs("numpyro", max_tree_depth=9, chain_method="vectorized")
        assert got["nuts_sampler_kwargs"]["chain_method"] == "vectorized"
        assert got["nuts_sampler_kwargs"]["nuts_kwargs"] == {"max_tree_depth": 9}

    def test_no_max_tree_depth_changes_nothing(self):
        # 既定（未指定）では pm.sample へ渡る引数が現状と1バイトも変わらないこと。
        assert bmb.sampler_kwargs("numpyro") == {}
        assert bmb.sampler_kwargs("numpyro", chain_method="parallel") == {
            "nuts_sampler_kwargs": {"chain_method": "parallel"}}

    def test_unknown_sampler_raises_instead_of_dropping(self):
        with pytest.raises(ValueError):
            bmb.sampler_kwargs("blackjax", max_tree_depth=8)


class TestTreedepthCap:
    """到達率の基準は設定した上限に追従すること（#540）。"""

    def test_default_is_2_pow_10_minus_1(self):
        assert bmb.treedepth_cap_steps(None) == bmb.MAX_TREEDEPTH_STEPS == 1023

    def test_scalar_depth(self):
        assert bmb.treedepth_cap_steps(8) == 255
        assert bmb.treedepth_cap_steps(7) == 127

    def test_tuple_uses_sampling_side(self):
        # sample_stats は warmup を含まない＝比較すべき上限は draws 側。
        assert bmb.treedepth_cap_steps((8, 10)) == 1023

    def test_rate_follows_the_cap_not_the_default(self):
        steps = np.array([255.0, 255.0, 127.0, 63.0])
        # 1023 を基準にすると「張り付いていない」と誤読する（観測対象が見えなくなる）。
        assert bmb.summarize_steps(steps)["max_treedepth_rate"] == pytest.approx(0.0)
        assert bmb.summarize_steps(steps, cap_steps=255)["max_treedepth_rate"] == pytest.approx(0.5)
        assert bmb.summarize_steps(steps, cap_steps=255)["cap_steps"] == 255


class TestEssEfficiency:
    """ESS の正規化（主指標＝ESS/歩・従指標＝ESS/秒）。"""

    def test_derives_per_step_and_per_sec(self):
        ess = {"ess_bulk_median": 500.0, "ess_bulk_min": 100.0}
        got = bmb.ess_efficiency(ess, total_steps=2_000_000, seconds=250.0)
        assert got["ess_bulk_median_per_1e6step"] == pytest.approx(250.0)
        assert got["ess_bulk_min_per_1e6step"] == pytest.approx(50.0)
        assert got["ess_bulk_median_per_sec"] == pytest.approx(2.0)
        assert got["ess_bulk_min_per_sec"] == pytest.approx(0.4)

    def test_missing_inputs_are_none_not_zero(self):
        # 「測れなかった」を 0 と区別する（0 は「効率がゼロ」という別の主張になる）。
        assert bmb.ess_efficiency(None, 1000, 10.0)["ess_bulk_median_per_1e6step"] is None
        ess = {"ess_bulk_median": 500.0, "ess_bulk_min": 100.0}
        assert bmb.ess_efficiency(ess, None, 10.0)["ess_bulk_median_per_1e6step"] is None
        assert bmb.ess_efficiency(ess, 1000, 0.0)["ess_bulk_median_per_sec"] is None


class TestDiagnoseMatchesProductionGate:
    """bench が測る ESS / r_hat が **本番ゲートと同じ量**であること（#540）。

    別物を測っていたら格子の結論が本番へ移らない。`persist_allowed` の閾値は `beta` の r_hat に
    対して較正された値なので、`beta_raw` で代用すると黙って緩くなる（ADR-0002 #541 節）。
    突き合わせ相手は `macro_beta_inference.summarize_diagnostics` そのもの。

    合成 idata は `tests.test_macro_beta_inference._raw_idata` を**共有する**（同じ材料で比べる
    ことが検証の要件。こちらで作り直すと「両方が同じように間違っている」を見逃しうる）。
    """

    def _idata(self, az):
        from tests.test_macro_beta_inference import _raw_idata
        return _raw_idata(az)

    def test_r_hat_max_and_ess_bulk_min_equal_production(self, monkeypatch):
        az = pytest.importorskip("arviz")
        import macro_beta_inference as mbi

        monkeypatch.setattr(mbi, "BETA_CHUNK_STOCKS", 3)   # 境界跨ぎを強制
        idata, sector_idx = self._idata(az)

        prod = mbi.summarize_diagnostics(idata, sector_idx)
        got = bmb.diagnose(idata, sector_idx)

        assert got["r_hat_max"] == pytest.approx(prod["r_hat_max"], rel=1e-12)
        assert got["ess_bulk_min"] == pytest.approx(prod["ess_bulk_min"], rel=1e-12)
        assert got["ess_tail_min"] == pytest.approx(prod["ess_tail_min"], rel=1e-12)

    def test_reports_the_distribution_not_only_the_min(self):
        az = pytest.importorskip("arviz")
        idata, sector_idx = self._idata(az)
        got = bmb.diagnose(idata, sector_idx)

        # min は最小順序統計でノイズが大きい＝順位付けは median で見る。両方残す。
        assert got["ess_bulk_min"] <= got["ess_bulk_p10"] <= got["ess_bulk_median"]
        # beta(7x2) + alpha(7) + mu_universe(2) = 23 パラメータ。
        assert got["n_params"] == 23

    def test_argmin_points_at_the_same_parameter_as_production(self, monkeypatch):
        """#600: 極値の**位置**も本番と同じであること。

        位置の解決は `macro_beta_inference` 側の1実装（`locate_extreme`）を共有している。
        bench 側へ書き写すと、格子で見た母数と本番で起きている母数が別物になりうる。
        """
        az = pytest.importorskip("arviz")
        import macro_beta_inference as mbi

        monkeypatch.setattr(mbi, "BETA_CHUNK_STOCKS", 3)   # 境界跨ぎを強制
        idata, sector_idx = self._idata(az)

        prod = mbi.summarize_diagnostics(idata, sector_idx)
        got = bmb.diagnose(idata, sector_idx)

        for key in ("ess_bulk_argmin", "r_hat_argmax"):
            assert got[key]["label"] == prod[key]["label"]
            assert got[key]["value"] == pytest.approx(prod[key]["value"], rel=1e-12)
        # 値そのものとも整合していること（別の母数を指していたらここで落ちる）。
        assert got["ess_bulk_argmin"]["value"] == pytest.approx(got["ess_bulk_min"], rel=1e-12)
        assert got["r_hat_argmax"]["value"] == pytest.approx(got["r_hat_max"], rel=1e-12)

    def test_values_are_raw_not_arviz_rounded(self):
        """#356 の丸め回帰検知。整数/小数2桁へ量子化されていたら格子の差が消える。"""
        az = pytest.importorskip("arviz")
        idata, sector_idx = self._idata(az)
        got = bmb.diagnose(idata, sector_idx)
        assert got["ess_bulk_min"] != float(int(got["ess_bulk_min"]))
        assert round(got["r_hat_max"], 2) != got["r_hat_max"]


class TestFormatReport:
    def test_renders_ascii_only(self):
        record = {
            "label": "A-local", "mode": "synth",
            "panel": {"n_stock": 250, "n_sector": 34, "n_factor": 12, "n_obs": 6000},
            "config": {"chains": 2, "tune": 200, "draws_list": [50, 200], "target_accept": 0.95,
                       "nuts_sampler": "numpyro", "init": "adapt_diag", "chain_method": None,
                       "force_devices": True, "threads": 0, "seed": 0, "max_tree_depth": None},
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

    def _record_with_ess(self):
        run = {"draws": 400, "seconds": 250.0, "cpu_per_wall": 1.8,
               "steps": bmb.summarize_steps(np.array([255.0, 255.0]), cap_steps=255),
               "n_divergences": 0, "total_steps": 2_000_000, "diag_sec": 12.0,
               "ess": {"r_hat_max": 1.0231, "ess_bulk_min": 101.7, "ess_bulk_p10": 203.4,
                       "ess_bulk_median": 512.9, "ess_tail_min": 190.2, "n_params": 3012}}
        run.update(bmb.ess_efficiency(run["ess"], run["total_steps"], run["seconds"]))
        return {
            "label": "md8", "mode": "real",
            "panel": {"n_stock": 250, "n_sector": 33, "n_factor": 12, "n_obs": 6190},
            "config": {"chains": 2, "tune": 800, "draws_list": [400], "target_accept": 0.95,
                       "nuts_sampler": "numpyro", "init": "adapt_diag", "chain_method": None,
                       "force_devices": True, "threads": 0, "seed": 0, "max_tree_depth": 8},
            "runs": [run], "fit": {"fixed_sec": None, "per_draw_sec": None},
            "stage_sec": {"panel": 3.0, "model_build": 1.0, "sample_total": 250.0},
            "env": {"jax": "0.10.2", "cpu_count": 6},
        }

    def test_ess_table_is_rendered_with_raw_values(self):
        text = bmb.format_report(self._record_with_ess())
        text.encode("cp932")
        assert "ESS/1e6step" in text
        assert "max_tree_depth=8" in text
        # 生値のまま出す（整形で桁を落とすと格子の差が消える・#466）。
        assert "101.7" in text and "512.9" in text

    def test_ess_table_is_absent_when_not_measured(self):
        record = self._record_with_ess()
        record["runs"][0]["ess"] = None
        assert "ESS/1e6step" not in bmb.format_report(record)
