"""`scripts/measure_transformer.py`（事前登録 docs/prereg/m7-transformer.md の計測・#1316）のテスト。

重い計算は回さない。判定（§4）・入力の照合・凍結値の照合・日中キューの引数を縛る。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import measure_transformer as mt
from scripts import prereg

ROOT = Path(__file__).resolve().parents[1]


def _sig(m6_m7=None, m2_m7=None):
    """significance_matrix の形。引数は (mean_diff=相手−M7, significant)。"""
    pairs = {}
    for key, v in (("M-6|M-7", m6_m7), ("M-2|M-7", m2_m7)):
        if v is None:
            continue
        diff, sig = v
        better = None if not sig else (key.split("|")[0] if diff > 0 else "M-7")
        pairs[key] = {"mean_diff": diff, "significant": sig, "better": better, "n_common": 18,
                      "p_value": 0.001 if sig else 0.4, "ci_lo": None, "ci_hi": None}
    return {"models": ["M-2", "M-6", "M-7"], "pairs": pairs}


class TestDecision:
    @pytest.mark.parametrize("m6, m2, expected", [
        ((-0.03, True), (-0.05, True), mt.DECISION_PROMOTE),     # M-6 に勝ち M-2 にも勝つ
        ((-0.03, True), (0.01, False), mt.DECISION_PROMOTE),     # M-6 に勝ち M-2 とは差なし
        ((-0.03, True), (0.02, True), mt.DECISION_KEEP),         # M-6 に勝つが M-2 に負ける
        ((0.04, True), (0.05, True), mt.DECISION_RETIRE),        # 両方に負ける
        ((0.04, True), (0.01, False), mt.DECISION_KEEP),         # M-6 にだけ負ける
        ((0.01, False), (0.05, True), mt.DECISION_KEEP),         # M-2 にだけ負ける
        ((0.01, False), (0.01, False), mt.DECISION_KEEP),        # 差なし
    ])
    def test_table(self, m6, m2, expected):
        assert mt.prereg_decision(_sig(m6, m2))["decision"] == expected

    def test_missing_pairs_count_as_not_significant(self):
        assert mt.prereg_decision(_sig())["decision"] == mt.DECISION_KEEP
        assert mt.prereg_decision(None)["decision"] == mt.DECISION_KEEP

    def test_reversed_pair_order_is_read_correctly(self):
        sig = {"pairs": {"M-7|M-6": {"mean_diff": 0.03, "significant": True, "better": "M-7",
                                    "n_common": 18, "p_value": 0.001, "ci_lo": 0.01, "ci_hi": 0.05}}}
        d = mt.prereg_decision(sig)
        assert d["m7_beats_m6"] is True and d["vs_m6"]["mean_diff"] == -0.03


def _entry(short, n_periods=18, n_rows=50000, ic=None, topk=None, available=True):
    return {"short": short, "available": available, "reason": None if available else "error",
            "error": None if available else "boom",
            "oof_backtest": {"n_periods": n_periods, "n_oof_samples": n_rows,
                             "rank_ic": {"mean": 0.1, "n": n_periods},
                             "rank_ic_by_period": ic or {f"2024-{m:02d}": 0.1 for m in range(1, 7)},
                             "top_k": {"10": {"excess_return": 0.2}},
                             "top_k_excess_return_by_period": {"10": topk or {"2024-01": 0.2}}}}


class TestAlignment:
    def test_matching_rows_pass(self):
        rows = {k: _entry(k) for k in ("M-2", "M-6", "M-7", "flat")}
        assert mt.check_alignment(rows) == {"n_periods": 18, "n_oof_samples": 50000}

    def test_row_count_mismatch_stops(self):
        rows = {k: _entry(k) for k in ("M-2", "M-6", "M-7")}
        rows["flat"] = _entry("flat", n_rows=49000)
        with pytest.raises(mt.InputMissing, match="そろわない"):
            mt.check_alignment(rows)

    def test_failed_model_stops(self):
        rows = {k: _entry(k) for k in ("M-2", "M-6")}
        rows["M-7"] = _entry("M-7", available=False)
        with pytest.raises(mt.InputMissing, match="採点できなかった"):
            mt.check_alignment(rows)


class TestBuildReport:
    def _comparison(self, sig):
        return {"models": [_entry("M-2"), _entry("M-6"), _entry("M-7")], "significance_matrix": sig}

    def test_report_contains_decision_ablation_and_reference(self):
        m7_ic = {f"2024-{m:02d}": 0.20 + 0.01 * m for m in range(1, 13)}
        flat_ic = {f"2024-{m:02d}": 0.05 + 0.005 * (m % 3) for m in range(1, 13)}
        comp = self._comparison(_sig((-0.05, True), (-0.06, True)))
        comp["models"][2] = _entry("M-7", ic=m7_ic)
        rep = mt.build_report(comp, _entry("flat", ic=flat_ic), {43: _entry("M-7"), 44: _entry("M-7")})
        assert rep["prereg_decision"]["decision"] == mt.DECISION_PROMOTE
        assert rep["ablation"]["sequence_adds"] is True
        assert "note" not in rep["prereg_decision"]
        assert set(rep["reference"]["summary"]) == {"M-2", "M-6", "M-7", "M-7（系列なし）"}
        assert rep["reference"]["seeds"] == {"43": 0.1, "44": 0.1}
        assert rep["reference"]["top10_excess_m7_minus_m6"] == {"mean_diff": 0.0, "n_common": 1}

    def test_promotion_without_sequence_gain_is_noted(self):
        same = {f"2024-{m:02d}": 0.1 for m in range(1, 13)}
        comp = self._comparison(_sig((-0.05, True), (0.0, False)))
        comp["models"][2] = _entry("M-7", ic=same)
        rep = mt.build_report(comp, _entry("flat", ic=same), {})
        assert rep["prereg_decision"]["decision"] == mt.DECISION_PROMOTE
        assert rep["ablation"]["sequence_adds"] is False
        assert "系列" in rep["prereg_decision"]["note"]

    def test_missing_model_row_stops(self):
        comp = {"models": [_entry("M-2"), _entry("M-6")], "significance_matrix": None}
        with pytest.raises(mt.InputMissing, match="行が無い"):
            mt.build_report(comp, _entry("flat"), {})


def test_mismatched_frozen_value_exits_2_before_any_work(monkeypatch):
    import collector_utils

    def boom(*a, **k):
        raise AssertionError("計測を始めてはいけない")
    monkeypatch.setattr(mt, "_measure", boom)
    # 標準出力を差し替えると pytest の捕捉が壊れ、後続のテストが setup で落ちる
    monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)
    with pytest.raises(SystemExit) as e:
        mt.main(["--prereg", mt.PREREG, "--prereg-sha", "0" * 64])
    assert e.value.code == prereg.EXIT_MISMATCH


def test_daytime_job_carries_the_frozen_value():
    from scripts import run_daytime as rd
    job = rd.JOBS["measure:m7-transformer"]
    argv = job.argv
    assert argv[1:3] == ("-m", "scripts.measure_transformer")
    assert argv[argv.index("--prereg") + 1] == mt.PREREG
    text = (ROOT / mt.PREREG).read_text(encoding="utf-8")
    assert argv[argv.index("--prereg-sha") + 1] == prereg.recorded_sha(text) == prereg.body_sha256(text)
    assert job.parallel_sensitive is True and job.needs_deps_smoke is True
    assert job.measured_min <= rd.JOB_BUDGET_MIN


def test_rule_matches_the_prereg():
    """事前登録 §3 の量: 3 モデル・系列なしの対照（α 0.05）・種 43/44 は参考。"""
    assert mt.MODELS == ("macro_gbdt", "macro_enet", "macro_transformer")
    assert mt.ABLATION_PARAMS == {"use_sequence": False}
    assert mt.ABLATION_ALPHA == 0.05
    assert mt.REFERENCE_SEEDS == (43, 44)
