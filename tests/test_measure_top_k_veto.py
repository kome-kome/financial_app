"""拒否権に使う K を決める計測 CLI（scripts/measure_top_k_veto.py・#1044）のテスト。

モデル比較は回さず、合成の `run_comparison` 形の結果（`--from-json` で読む形）で規則を確かめる。
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import model_comparison  # noqa: E402
from model_stats import bootstrap_mean_se  # noqa: E402
from plugins.macro_snapshots import TOP_K_REFERENCE  # noqa: E402
from scripts import measure_top_k_veto as X  # noqa: E402
from scripts import prereg  # noqa: E402
from scripts import run_daytime as rd  # noqa: E402

PREREG = ROOT / "docs" / "prereg" / "top-k-veto.md"
FROZEN = prereg.recorded_sha(PREREG.read_text(encoding="utf-8"))
YMS = [f"{2020 + i // 4}-{(i % 4) * 3 + 1:02d}" for i in range(19)]


@pytest.fixture(autouse=True)
def _fixed_head_sha(monkeypatch):
    import collector_utils
    monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)
    # 標準出力の差し替えは pytest のキャプチャを壊す（force_utf8_stdout の docstring）
    monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)


def _steady(mean, noise=0.001, seed=0):
    """平均が大きくぶれが小さい期別系列（合格する側）。"""
    rng = random.Random(seed)
    return {ym: mean + rng.uniform(-noise, noise) for ym in YMS}


def _noisy(mean=0.01, swing=0.1):
    """平均に比べてぶれが大きい期別系列（不合格の側）。"""
    return {ym: mean + (swing if i % 2 else -swing) for i, ym in enumerate(YMS)}


def _entry(name, by_k, display=True):
    """`run_comparison` の 1 モデル。display=True なら画面の並びの採点に系列を持つ。"""
    series = {"top_k_excess_return_by_period": {str(k): s for k, s in by_k.items()}}
    other = {"top_k_excess_return_by_period": {str(k): _noisy() for k in by_k}}
    if display:
        return {"name": name, "available": True, "oof_backtest": other, "oof_backtest_display": series}
    return {"name": name, "available": True, "oof_backtest": series, "oof_backtest_display": None}


def _models(per_model):
    """{model: {K: 系列}} → run_comparison の models。M-6 は画面の並びの採点を持たない（λ=0）。"""
    return [_entry(name, by_k, display=(name != "macro_enet")) for name, by_k in per_model.items()]


class TestRuleQuantities:
    def test_mde_formula(self):
        z = statistics.NormalDist()
        expected = (z.inv_cdf(0.95) + z.inv_cdf(0.80)) * math.sqrt(2.0) * 0.01
        assert X.mde(0.01) == pytest.approx(expected, rel=1e-12)
        assert X.mde(0.01) == pytest.approx(0.035164, abs=1e-6)       # 2.4865 × √2 × 0.01

    def test_judge_uses_the_bootstrap_se(self):
        values = list(_steady(0.05).values())
        r = X.judge_series(values)
        assert r["n"] == 19
        assert r["mean"] == pytest.approx(statistics.mean(values))
        assert r["se"] == bootstrap_mean_se(values)
        assert r["mde"] == pytest.approx(X.mde(r["se"]))
        assert r["passed"] is True

    def test_noisy_short_and_zero_series_fail(self):
        assert X.judge_series(list(_noisy().values()))["passed"] is False
        assert X.judge_series([0.05])["passed"] is False                 # 1 期では SE が出ない
        assert X.judge_series([])["passed"] is False
        flat_zero = [0.0, 0.001, -0.001, 0.0] * 5
        assert X.judge_series(flat_zero)["passed"] is False               # 平均 0 は 0.5×|平均|=0


class TestChooseK:
    def test_smallest_k_where_every_model_passes(self):
        models = _models({n: {k: _steady(0.05, seed=k) for k in X.CANDIDATE_K} for n in X.MODELS})
        table, _src = X.build_table(models)
        assert X.choose_k(table) == 5

    def test_one_model_failing_at_five_moves_to_ten(self):
        per = {n: {k: _steady(0.05, seed=k) for k in X.CANDIDATE_K} for n in X.MODELS}
        per["macro_gbdt"][5] = _noisy()
        table, _src = X.build_table(_models(per))
        assert X.choose_k(table) == 10

    def test_no_k_means_no_veto(self):
        per = {n: {k: _noisy() for k in X.CANDIDATE_K} for n in X.MODELS}
        per["macro_risk_return"] = {k: _steady(0.05) for k in X.CANDIDATE_K}
        table, _src = X.build_table(_models(per))
        assert X.choose_k(table) is None

    def test_swapping_series_between_models_does_not_move_k(self):
        """規則の入力は各モデル自身のぶれだけ＝どのモデルがどの系列を持つかを入れ替えても K は同じ。"""
        series = [{5: _noisy(), 10: _steady(0.04, seed=1), 30: _steady(0.03, seed=2)},
                  {5: _steady(0.05, seed=3), 10: _steady(0.05, seed=4), 30: _noisy()},
                  {5: _steady(0.02, seed=5), 10: _steady(0.02, seed=6), 30: _steady(0.02, seed=7)}]
        ks = set()
        for perm in ([0, 1, 2], [2, 0, 1], [1, 2, 0], [2, 1, 0]):
            per = {name: series[i] for name, i in zip(X.MODELS, perm)}
            ks.add(X.choose_k(X.build_table(_models(per))[0]))
        assert ks == {10}


class TestInputs:
    def test_display_ordering_is_used_when_present(self):
        source, by_k = X.series_of(_entry("macro_gbdt", {5: _steady(0.05)}, display=True))
        assert source == "display"
        assert by_k["5"] == _steady(0.05)

    def test_forecast_ordering_when_there_is_no_display(self):
        source, by_k = X.series_of(_entry("macro_enet", {5: _steady(0.05)}, display=False))
        assert source == "forecast"
        assert by_k["5"] == _steady(0.05)

    def test_result_from_before_top_k_is_rejected(self):
        """#1041 より前の結果を空の系列として読むと、黙って「該当なし」になる。"""
        with pytest.raises(ValueError, match="期別系列が無い"):
            X.series_of({"name": "macro_enet", "available": True,
                         "oof_backtest": {"rank_ic": {}}, "oof_backtest_display": None})

    def test_a_missing_model_stops_the_rule(self):
        per = {n: {k: _steady(0.05) for k in X.CANDIDATE_K} for n in X.MODELS}
        models = _models(per)
        models[1] = {"name": "macro_gbdt", "available": False, "reason": "error"}
        with pytest.raises(ValueError, match="macro_gbdt"):
            X.build_table(models)
        with pytest.raises(ValueError, match="macro_enet"):
            X.build_table(models[:2])

    def test_series_are_read_in_time_order(self):
        """定常ブートストラップは期の並びの相関を保つので、期の順に並べてから渡す。"""
        by = _steady(0.05)
        shuffled = dict(sorted(by.items(), key=lambda kv: kv[1]))
        per = {n: {k: shuffled for k in X.CANDIDATE_K} for n in X.MODELS}
        table, _src = X.build_table(_models(per))
        assert table["macro_enet"][5]["se"] == bootstrap_mean_se([by[ym] for ym in YMS])

    def test_rule_constants_fit_the_code(self):
        assert set(X.CANDIDATE_K) <= set(TOP_K_REFERENCE)
        assert set(X.MODELS) <= {n for n, _ in model_comparison.COMPARISON_MODELS}


class TestCli:
    def _json(self, tmp_path, models):
        p = tmp_path / "comparison.json"
        p.write_text(json.dumps({"models": models}), encoding="utf-8")
        return p

    def test_writes_the_table_and_the_chosen_k(self, tmp_path):
        per = {n: {k: _steady(0.05, seed=k) for k in X.CANDIDATE_K} for n in X.MODELS}
        src, out = self._json(tmp_path, _models(per)), tmp_path / "out.json"
        code = X.main(["--prereg", str(PREREG), "--prereg-sha", FROZEN,
                       "--from-json", str(src), "--out", str(out)])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["chosen_k"] == 5
        assert payload["prereg_sha256"] == FROZEN
        assert payload["prereg_issue"].endswith("/issues/1044")
        assert payload["sources"] == {"macro_risk_return": "display", "macro_gbdt": "display",
                                      "macro_enet": "forecast"}
        assert set(payload["table"]["macro_gbdt"]) == {str(k) for k in X.CANDIDATE_K}

    def test_mismatched_prereg_stops_with_exit_code_2(self, tmp_path):
        src = self._json(tmp_path, _models({n: {k: _steady(0.05) for k in X.CANDIDATE_K}
                                            for n in X.MODELS}))
        with pytest.raises(SystemExit) as e:
            X.main(["--prereg", str(PREREG), "--prereg-sha", "0" * 64, "--from-json", str(src),
                    "--out", str(tmp_path / "out.json")])
        assert e.value.code == 2
        assert not (tmp_path / "out.json").exists()

    def test_incomplete_input_stops_with_exit_code_3(self, tmp_path):
        src = self._json(tmp_path, [])
        out = tmp_path / "out.json"
        assert X.main(["--prereg", str(PREREG), "--prereg-sha", FROZEN,
                       "--from-json", str(src), "--out", str(out)]) == X.EXIT_INPUT
        assert not out.exists()


class TestLambdaReference:
    """λ の参考（#1040）: 画面の並び U − 予想 μ̂ の上位K社超過リターン。K の規則には入らない。"""

    def _models(self, u_mean, mu_mean):
        def entry(name, display):
            mu = {"top_k_excess_return_by_period": {str(k): _steady(mu_mean, seed=k) for k in X.CANDIDATE_K}}
            u = {"top_k_excess_return_by_period": {str(k): _steady(u_mean, seed=k + 7) for k in X.CANDIDATE_K}}
            return {"name": name, "available": True, "oof_backtest": mu, "oof_backtest_display": u if display else None}
        return [entry(n, n != "macro_enet") for n in X.MODELS]

    def test_difference_is_display_minus_forecast_and_skips_lambda_zero(self):
        ref = X.lambda_reference(self._models(u_mean=0.01, mu_mean=0.05))
        assert set(ref) == {"macro_risk_return", "macro_gbdt"}
        sig = ref["macro_gbdt"][str(X.LAMBDA_MAIN_K)]
        assert sig["mean"] == pytest.approx(-0.04, abs=0.002) and sig["significant"] is True

    def test_missing_series_gives_none(self):
        models = self._models(0.01, 0.05)
        models[0]["oof_backtest"]["top_k_excess_return_by_period"].pop("30")
        assert X.lambda_reference(models)["macro_risk_return"]["30"] is None

    def test_written_to_the_json_without_moving_the_rule(self, tmp_path):
        p = tmp_path / "comparison.json"
        models = self._models(0.01, 0.05)
        p.write_text(json.dumps({"models": models}), encoding="utf-8")
        out = tmp_path / "out.json"
        assert X.main(["--prereg", str(PREREG), "--prereg-sha", FROZEN, "--from-json", str(p), "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["lambda_reference"]["main_k"] == 10
        assert set(payload["lambda_reference"]["by_model"]) == {"macro_risk_return", "macro_gbdt"}
        table, _src = X.build_table(models)
        assert payload["chosen_k"] == X.choose_k(table)


class TestDaytimeJob:
    KEY = "measure:top-k-veto"

    def test_frozen_value_matches_the_prereg_appendix(self):
        argv = rd.JOBS[self.KEY].argv
        doc = ROOT / argv[argv.index("--prereg") + 1]
        sha = argv[argv.index("--prereg-sha") + 1]
        text = doc.read_text(encoding="utf-8")
        # 凍結し直したら（本文と追記節の凍結値を変えたら）日中キューの引数も直す
        assert sha == prereg.recorded_sha(text) == prereg.body_sha256(text)

    def test_runs_alone(self):
        job = rd.JOBS[self.KEY]
        assert job.argv[1:4] == ("-m", "scripts.measure_top_k_veto", "--prereg")
        assert job.parallel_sensitive is True

    def test_result_section_records_the_chosen_k(self):
        # 2026-10-28 の実走で K は該当なし（ADR-0069 accepted）。書き写した結果を消さない
        text = PREREG.read_text(encoding="utf-8")
        section = prereg.appendix_section(text, "### 結果")
        assert not section.startswith(prereg.PLACEHOLDER)
        assert "選ばれた K: 該当なし" in section
