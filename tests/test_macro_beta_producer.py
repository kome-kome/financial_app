"""tests/test_macro_beta_producer.py — #214 producer 化（graceful-degrade）。

macro_beta（推論バッチ出力）から per-stock μ・R_macro・R1' を算出する producer_scores、
プラグインの read_producer_scores / produced_output（DB 有無での graceful degrade）を検証。
MCMC（PyMC）は不要＝ローカルで実行可能。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.macro_risk_return import producer_scores, MacroRiskReturnPlugin
from database import upsert_macro_beta


_META = {
    "run_id": "r1", "snapshot_date": "2026-06-01",
    "selected_factors": ["f1", "f2"],
    "factor_cov": [[4.0, 0.0], [0.0, 9.0]],   # 対角 → R_macro = sqrt(β1²·4 + β2²·9)
    "hyperparams": {},
}
_LOADINGS = {
    "E001": {"f1": (1.0, 0.1), "f2": (1.0, 0.2), "_intercept": (0.05, 0.02)},
}


class TestProducerScores:
    def test_r_macro_only_without_snapshot(self):
        out = producer_scores(_META, _LOADINGS)
        assert set(out["E001"].keys()) == {"r_macro"}            # snapshot 無し → R_macro のみ
        assert out["E001"]["r_macro"] == pytest.approx(math.sqrt(4.0 + 9.0))

    def test_full_with_snapshot(self):
        snap = {"f1": 0.5, "f2": 2.0}
        rec = producer_scores(_META, _LOADINGS, snap)["E001"]
        assert rec["mu"] == pytest.approx(0.05 + 0.5 + 2.0)       # α + β·m
        assert rec["r1_prime"] == pytest.approx(
            math.sqrt(0.02 ** 2 + (0.1 * 0.5) ** 2 + (0.2 * 2.0) ** 2))
        assert rec["r_macro"] == pytest.approx(math.sqrt(13.0))

    def test_missing_factor_treated_zero(self):
        loadings = {"E002": {"f1": (2.0, 0.0), "_intercept": (0.0, 0.0)}}  # f2 欠落
        rec = producer_scores(_META, loadings, {"f1": 1.0, "f2": 1.0})["E002"]
        assert rec["mu"] == pytest.approx(2.0)                    # f2 の β=0 で寄与なし
        assert rec["r_macro"] == pytest.approx(4.0)              # sqrt(2²·4)

    def test_empty_loadings(self):
        assert producer_scores(_META, {}) == {}


class TestPluginProducer:
    def test_produced_output_false_when_empty(self, db):
        assert MacroRiskReturnPlugin().produced_output(db) is False

    def test_read_producer_scores_graceful_empty(self, db):
        assert MacroRiskReturnPlugin().read_producer_scores(db) == {}

    def test_producer_after_upsert(self, db):
        upsert_macro_beta(db, _META, [
            {"run_id": "r1", "edinet_code": "E001", "factor_name": "f1",
             "loading_mean": 1.0, "loading_se": 0.1},
            {"run_id": "r1", "edinet_code": "E001", "factor_name": "f2",
             "loading_mean": 1.0, "loading_se": 0.2},
            {"run_id": "r1", "edinet_code": "E001", "factor_name": "_intercept",
             "loading_mean": 0.05, "loading_se": 0.02},
        ])
        plugin = MacroRiskReturnPlugin()
        assert plugin.produced_output(db) is True                # 蓄積後は producer 充足
        out = plugin.read_producer_scores(db, {"f1": 0.5, "f2": 2.0})
        assert out["E001"]["mu"] == pytest.approx(0.05 + 0.5 + 2.0)
        assert out["E001"]["r_macro"] == pytest.approx(math.sqrt(13.0))
