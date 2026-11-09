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


class TestProductionRow:
    """本番の設定の行（M-2・#1115）: λ の参考には印付きで並べ、事前登録の規則（K の選び方）には入れない。"""

    @staticmethod
    def _production(name="macro_gbdt", passing=True):
        by_k = {k: (_steady(0.08, seed=k + 20) if passing else _noisy()) for k in X.CANDIDATE_K}
        e = _entry(name, by_k, display=True)
        e.update(variant=model_comparison.VARIANT_PRODUCTION, selection_optimism=True)
        return e

    def test_the_rule_reads_only_the_default_rows(self):
        """同じ名前の本番の行で既定の行を上書きしない（M-2 の既定は不合格・本番は合格でも K は動かない）。"""
        base = {"macro_risk_return": {k: _steady(0.05, seed=k) for k in X.CANDIDATE_K},
                "macro_gbdt": {k: _noisy() for k in X.CANDIDATE_K},
                "macro_enet": {k: _steady(0.05, seed=k + 3) for k in X.CANDIDATE_K}}
        models = _models(base)
        table_default, _ = X.build_table(models)
        table_mixed, _ = X.build_table(models[:2] + [self._production()] + models[2:])
        assert table_mixed == table_default
        assert X.choose_k(table_mixed) is None

    def test_lambda_reference_lists_the_production_row_under_its_own_key(self, capsys):
        models = TestLambdaReference()._models(u_mean=0.01, mu_mean=0.05) + [self._production()]
        ref = X.lambda_reference(models)
        assert set(ref) == {"macro_risk_return", "macro_gbdt", "macro_gbdt:production"}
        X._print_lambda(ref)
        out = capsys.readouterr().out
        assert "M-2*" in out and "selection optimism" in out

    def test_the_json_marks_selection_optimism(self, tmp_path):
        models = TestLambdaReference()._models(u_mean=0.01, mu_mean=0.05) + [self._production()]
        p = tmp_path / "comparison.json"
        p.write_text(json.dumps({"models": models}), encoding="utf-8")
        out = tmp_path / "out.json"
        assert X.main(["--prereg", str(PREREG), "--prereg-sha", FROZEN, "--from-json", str(p), "--out", str(out)]) == 0
        lam = json.loads(out.read_text(encoding="utf-8"))["lambda_reference"]
        assert lam["selection_optimism"] == ["macro_gbdt:production"]
        assert "macro_gbdt:production" in lam["by_model"]


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


# ── 上位K社の実現順位の平均で測り直す（#1156・docs/prereg/top-k-veto-pct-rank.md）──────────────

PREREG_PCT = ROOT / "docs" / "prereg" / "top-k-veto-pct-rank.md"
FROZEN_PCT = prereg.recorded_sha(PREREG_PCT.read_text(encoding="utf-8"))
PCT_KEY = "top_k_pct_rank_by_period"


def _pct_entry(name, by_k):
    """予想の採点（λ=0）に実現順位の系列を持つ 1 モデル。超過リターンの系列はぶれの大きい別物にしておく。"""
    oof = {PCT_KEY: {str(k): s for k, s in by_k.items()},
           "top_k_excess_return_by_period": {str(k): _noisy() for k in by_k}}
    return {"name": name, "available": True, "oof_backtest": oof, "oof_backtest_display": None}


def _pct_models(per_model):
    return [_pct_entry(name, by_k) for name, by_k in per_model.items()]


def _oof_pct_series(skill, n=80, seed=0):
    """合成の OOF 予測を `oof_backtest` に通して、本物と同じ作り方の実現順位の期別系列を得る。

    skill=1 は完全に当てた並び、skill=0 はでたらめな並び（yhat は y と独立）。
    """
    from plugins.macro_snapshots import oof_backtest
    rng = random.Random(seed)
    residuals = {}
    for ym in YMS:
        rows = []
        for _ in range(n):
            y = rng.gauss(0.0, 0.2)
            rows.append((skill * y + (1 - skill) * rng.gauss(0.0, 0.2), y))
        residuals[ym] = rows
    return oof_backtest(residuals)[PCT_KEY]


class TestPctRankSeries:
    def test_the_center_is_one_half(self):
        """同じ系列でも、中心 0 では平均 0.5 が大きな信号に見え、中心 0.5 では信号が無い。"""
        flat_half = list(_steady(0.5, noise=0.01).values())
        assert X.judge_series(flat_half)["passed"] is True
        assert X.judge_series(flat_half, X.SERIES["pct_rank"].center)["passed"] is False
        assert X.judge_series(list(_steady(0.7, noise=0.01).values()), 0.5)["passed"] is True

    def test_the_rule_reads_the_series_that_oof_backtest_writes(self):
        """`oof_backtest` が出す系列のまま: 当てた並びは合格し、でたらめな並びは中心 0.5 の近くで不合格。"""
        perfect, noise = _oof_pct_series(skill=1.0), _oof_pct_series(skill=0.0, seed=1)
        assert set(perfect) == {str(k) for k in TOP_K_REFERENCE}
        # 上位 5 社は実現でも上から 5 社＝順位 79..75 / 79 の平均
        assert all(v == pytest.approx(77 / 79, abs=1e-6) for v in perfect["5"].values())
        assert statistics.mean(noise["30"].values()) == pytest.approx(0.5, abs=0.05)
        good = {k: perfect[str(k)] for k in X.CANDIDATE_K}
        bad = {k: noise[str(k)] for k in X.CANDIDATE_K}
        table, _ = X.build_table(_pct_models({n: good for n in X.MODELS}), "pct_rank")
        assert X.choose_k(table) == 5
        table, _ = X.build_table(_pct_models({**{n: good for n in X.MODELS}, "macro_gbdt": bad}), "pct_rank")
        assert X.choose_k(table) is None

    def test_reads_the_pct_rank_key_not_the_excess_key(self):
        source, by_k = X.series_of(_pct_entry("macro_enet", {5: _steady(0.7)}), "pct_rank")
        assert source == "forecast"
        assert by_k["5"] == _steady(0.7)
        # 既定（excess）は今までどおり超過リターンのキーを読む
        assert X.series_of(_pct_entry("macro_enet", {5: _steady(0.7)}))[1]["5"] == _noisy()

    def test_result_from_before_the_series_is_rejected(self):
        old = _entry("macro_enet", {5: _steady(0.05)}, display=False)    # 超過リターンだけを持つ
        with pytest.raises(ValueError, match="#1159 より前"):
            X.series_of(old, "pct_rank")

    def test_each_series_has_its_own_output(self):
        assert X.SERIES["excess"].out != X.SERIES["pct_rank"].out
        assert X.SERIES["excess"].out.name == "top_k_veto.json"          # #1044 の出力先は変えない


class TestPctRankCli:
    def _json(self, tmp_path, models):
        p = tmp_path / "comparison.json"
        p.write_text(json.dumps({"models": models}), encoding="utf-8")
        return p

    def _per(self):
        return {n: {k: _steady(0.7, noise=0.01, seed=k) for k in X.CANDIDATE_K} for n in X.MODELS}

    def test_writes_the_rule_with_the_series_and_center(self, tmp_path):
        src, out = self._json(tmp_path, _pct_models(self._per())), tmp_path / "out.json"
        code = X.main(["--series", "pct_rank", "--prereg", str(PREREG_PCT), "--prereg-sha", FROZEN_PCT,
                       "--from-json", str(src), "--out", str(out)])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["chosen_k"] == 5
        assert payload["rule"]["series"] == "pct_rank"
        assert payload["rule"]["series_key"] == PCT_KEY
        assert payload["rule"]["center"] == 0.5
        assert payload["prereg_issue"].endswith("/issues/1156")
        assert payload["sources"] == {n: "forecast" for n in X.MODELS}

    @pytest.mark.parametrize("series, doc, sha", [
        ("pct_rank", PREREG, FROZEN),          # 古い文書の凍結値で新しい系列を測る
        ("excess", PREREG_PCT, FROZEN_PCT),    # 新しい文書で超過リターンを測る
    ])
    def test_series_and_prereg_must_match(self, tmp_path, series, doc, sha):
        src, out = self._json(tmp_path, _pct_models(self._per())), tmp_path / "out.json"
        code = X.main(["--series", series, "--prereg", str(doc), "--prereg-sha", sha,
                       "--from-json", str(src), "--out", str(out)])
        assert code == prereg.EXIT_MISMATCH
        assert not out.exists()


class TestPctRankDaytimeJob:
    KEY = "measure:top-k-veto-pct-rank"

    def test_frozen_value_matches_the_prereg_appendix(self):
        argv = rd.JOBS[self.KEY].argv
        doc = ROOT / argv[argv.index("--prereg") + 1]
        sha = argv[argv.index("--prereg-sha") + 1]
        text = doc.read_text(encoding="utf-8")
        assert doc == PREREG_PCT
        assert sha == prereg.recorded_sha(text) == prereg.body_sha256(text)

    def test_runs_the_pct_rank_series_alone(self):
        job = rd.JOBS[self.KEY]
        assert job.argv[1:5] == ("-m", "scripts.measure_top_k_veto", "--series", "pct_rank")
        assert job.parallel_sensitive is True
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN

    def test_the_prereg_keeps_the_rule_of_1044(self):
        """系列と中心のほかは #1044 と同じ（対象・K の候補・閾値を変えるなら新しい ADR）。"""
        text = PREREG_PCT.read_text(encoding="utf-8")
        assert "M-1（`macro_risk_return`）・M-2（`macro_gbdt`）・M-6（`macro_enet`）" in text
        assert "K の候補: 5・10・30" in text
        assert "MDE ≤ 0.5 × |平均 − 0.5|" in text
        assert X.MODELS == ("macro_risk_return", "macro_gbdt", "macro_enet")
        assert X.CANDIDATE_K == (5, 10, 30)
