"""bakeoff スクリプトの有意判定が多重比較の補正を掛けることのテスト（#741・ADR-0063）。

`candidate_bakeoff`（候補 vs 基準線）と `ensemble_base_bakeoff`（3基底 M-4 vs 各基底）は、
複数の組を同時に検定しているのに補正なしの `significant` をそのまま「有意」と出していた。
どちらも `model_stats.paired_family_significance` を通し、検定した組数で Bonferroni を掛ける。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import candidate_bakeoff, ensemble_base_bakeoff
from tests.test_model_stats import MID_DIFFS

_YMS = [f"{2018 + i // 4}-{(i % 4) * 3 + 1:02d}" for i in range(len(MID_DIFFS))]


def _oof(ic_by_period):
    return {"rank_ic_by_period": ic_by_period}


def _level(v):
    return {ym: v for ym in _YMS}


class TestCandidateBakeoff:
    def _rows(self, n_const):
        rows = [{"name": "xgb_m2", "oof": _oof(_level(0.0))},
                {"name": "mid", "oof": _oof(dict(zip(_YMS, MID_DIFFS)))}]
        rows += [{"name": f"c{i}", "oof": _oof(_level(0.1 + 0.01 * i))} for i in range(n_const)]
        return rows

    def test_alone_the_mid_candidate_is_significant(self):
        sig = candidate_bakeoff.significance_vs_baseline(self._rows(0))
        assert sig["mid"]["n_tests"] == 1
        assert sig["mid"]["alpha"] == pytest.approx(0.05)
        assert sig["mid"]["better"] == "mid"

    def test_bonferroni_over_candidates(self):
        # 候補10本（mid + 定数9本）→ α=0.005。p≈0.019 の mid は補正後に有意でなくなる。
        sig = candidate_bakeoff.significance_vs_baseline(self._rows(9))
        assert sig["mid"]["n_tests"] == 10
        assert sig["mid"]["family_alpha"] == 0.05
        assert sig["mid"]["alpha"] == pytest.approx(0.005)
        assert sig["mid"]["significant"] is False
        assert sig["mid"]["better"] is None
        assert sig["c0"]["better"] == "c0"

    def test_errored_rows_are_not_tested(self):
        rows = self._rows(1) + [{"name": "broken", "error": "boom", "oof": {}}]
        sig = candidate_bakeoff.significance_vs_baseline(rows)
        assert "broken" not in sig
        assert sig["mid"]["n_tests"] == 2


class TestEnsembleBaseBakeoff:
    def test_bonferroni_over_bases(self):
        three = _oof(_level(0.2))
        bases = {"M-1": _oof(_level(0.1)), "M-2": _oof(_level(0.05)), "M-6": _oof(_level(0.15))}
        sigs = ensemble_base_bakeoff.significance_vs_bases(three, bases)
        assert set(sigs) == {"M-1", "M-2", "M-6"}
        assert all(s["alpha"] == pytest.approx(0.05 / 3) for s in sigs.values())

    def test_fmt_sig_prints_the_ci_level_it_used(self):
        three = _oof(_level(0.2))
        bases = {name: _oof(_level(0.1)) for name in ("M-1", "M-2", "M-6")}
        line = ensemble_base_bakeoff._fmt_sig(
            ensemble_base_bakeoff.significance_vs_bases(three, bases)["M-1"])
        assert "98.33%CI" in line          # 3検定 → α=0.0167 の区間（95% ではない）
        assert "alpha=0.0167" in line
