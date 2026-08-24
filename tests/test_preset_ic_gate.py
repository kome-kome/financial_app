"""scripts/preset_ic_gate.py のユニットテスト（Issue #529）。

主眼は **本番の消費側と同じ単位でスコアが合成されるか**。ハーネスが独自の標準化経路を
持つと、測っているものが本番と別物になる（#529 が指摘した乖離そのもの）。そのため
「同じ入力に対し `RecommendPlugin.execute` と同じスコアが出る」ことを最初のテストに置く。

パネル構築（`build_period_panel`）は DB フルロードが要るのでここでは触らない。
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
from plugins.recommend import PRESETS, plugin
from scripts.preset_ic_gate import (
    _panel_rows, build_view_stats, collect_weights, fmt_sig, ic_series,
    missing_weight_ratio, panel_info, score_period,
)

# パネルの列（build_period_panel が返す8列と同じ並び）
PANEL_FACTORS = ["z_roe", "z_op_margin", "z_revenue", "z_cf_ratio",
                 "z_equity_ratio", "z_eps", "z_de_ratio", "z_momentum"]

# 3列だけの小さな断面（fit_zscore_stats は有効4件未満で None を返すので6社置く）
FACTORS3 = ["z_roe", "z_op_margin", "z_revenue"]
VALUES3 = [
    (3.0, 1.0, -2.0),
    (1.5, -1.0, 0.5),
    (0.5, 2.0, 1.0),
    (-0.5, 0.0, 2.0),
    (-1.5, -2.0, -0.5),
    (-3.0, 0.5, 0.0),
]


def _records3():
    return _panel_rows(np.asarray(VALUES3, dtype=float), FACTORS3)


# ── 消費側との一致（このテストが #529 の本体）──────────────────────────────

class TestScoreMatchesProduction:
    def test_matches_execute_for_the_same_cross_section(self, db, make_metric):
        """同じ断面・同じ重みなら `execute` と同じスコアが出ること。

        ここが崩れると、測った rank-IC は本番の並びを説明しない。
        """
        weights = {"z_roe": 1.0, "z_op_margin": 0.5, "z_revenue": -0.8}
        db.add_all([
            make_metric(edinet_code=f"E{i:05d}", z_roe=v[0], z_op_margin=v[1], z_revenue=v[2])
            for i, v in enumerate(VALUES3)
        ])
        db.commit()

        res = asyncio.run(execute_plugin(
            plugin, {"weights": weights, "min_coverage": 0.0, "top_n": 100}, db))
        assert res["count"] == len(VALUES3)
        by_code = {r["edinet_code"]: r["score"] for r in res["results"]}

        records = _records3()
        stats = build_view_stats(records, weights, FACTORS3)
        scores = score_period(records, weights, stats)
        for i, s in enumerate(scores):
            assert by_code[f"E{i:05d}"] == pytest.approx(s, abs=1e-4)

    def test_denominator_is_weight_present_not_total(self):
        """分母は存在指標の |w| 和。パネルに無い列は分母にも入らない。"""
        records = _records3()
        weights = {"z_roe": 1.0, "gap_ratio": 3.0}   # gap_ratio はパネルに無い
        stats = build_view_stats(records, weights, FACTORS3)
        scores = score_period(records, weights, stats)
        only_roe = score_period(records, {"z_roe": 1.0},
                                build_view_stats(records, {"z_roe": 1.0}, FACTORS3))
        # total_weight(4.0) で割っていたら 1/4 に縮む。weight_present(1.0) なら一致する。
        assert scores == pytest.approx(only_roe)

    def test_row_without_any_weighted_value_is_none(self):
        records = _panel_rows(np.asarray(VALUES3, dtype=float), FACTORS3)
        scores = score_period(records, {"gap_ratio": 1.0}, {})
        assert scores == [None] * len(VALUES3)


# ── 標準化経路の切り替え ──────────────────────────────────────────────────

class TestStandardizationPath:
    def test_cross_section_standardize_is_applied_by_default(self):
        records = _records3()
        stats = build_view_stats(records, {"z_roe": 1.0, "z_op_margin": 1.0}, FACTORS3)
        assert set(stats) == {"z_roe", "z_op_margin"}

    def test_no_standardize_reproduces_pre_509_behaviour(self):
        """`--no-cross-section-standardize` は生値をそのまま線形結合する（#509 是正前）。"""
        records = _records3()
        weights = {"z_roe": 1.0}
        stats = build_view_stats(records, weights, FACTORS3, standardize=False)
        assert stats == {}
        scores = score_period(records, weights, stats)
        assert scores == pytest.approx([v[0] for v in VALUES3])

    def test_momentum_is_standardized_even_when_cross_section_is_off(self):
        """パネルの momentum は生 log return（#519）。本番は Z なので常に揃える。"""
        factors = FACTORS3 + ["z_momentum"]
        X = np.asarray([list(v) + [0.1 * i] for i, v in enumerate(VALUES3)], dtype=float)
        records = _panel_rows(X, factors)
        stats = build_view_stats(records, {"z_momentum": 1.0}, factors, standardize=False)
        assert "z_momentum" in stats

    def test_raw_momentum_opts_out(self):
        factors = FACTORS3 + ["z_momentum"]
        X = np.asarray([list(v) + [0.1 * i] for i, v in enumerate(VALUES3)], dtype=float)
        records = _panel_rows(X, factors)
        stats = build_view_stats(records, {"z_momentum": 1.0}, factors, raw_momentum=True)
        assert "z_momentum" not in stats


# ── 測れない重みの明示（gap_ratio・ADR-0008 Decision 1）──────────────────────

class TestMissingWeight:
    def test_value_preset_loses_gap_ratio(self):
        """割安重視は重み 4.5 のうち gap_ratio 2.0 が測れない＝44.4%。"""
        assert missing_weight_ratio(PRESETS["割安重視"], PANEL_FACTORS) == pytest.approx(2 / 4.5)

    def test_balanced_preset_loses_less(self):
        assert missing_weight_ratio(PRESETS["バランス型"], PANEL_FACTORS) == pytest.approx(0.5 / 5.1)

    def test_high_profit_preset_is_fully_measurable(self):
        assert missing_weight_ratio(PRESETS["高収益重視"], PANEL_FACTORS) == 0.0

    def test_zero_weights_do_not_divide_by_zero(self):
        assert missing_weight_ratio({}, PANEL_FACTORS) == 0.0


# ── 重みの収集と reject ───────────────────────────────────────────────────

def _args(**over):
    base = dict(preset=None, premia_run_id=None, weights_json=None)
    base.update(over)
    return SimpleNamespace(**base)


class TestCollectWeights:
    def test_defaults_to_the_four_static_presets(self, db):
        got = collect_weights(db, _args())
        assert set(got) == set(PRESETS)

    def test_named_preset_only(self, db):
        got = collect_weights(db, _args(preset=["バランス型"]))
        assert list(got) == ["バランス型"]
        assert got["バランス型"] == PRESETS["バランス型"]

    def test_unknown_preset_rejected(self, db):
        with pytest.raises(SystemExit):
            collect_weights(db, _args(preset=["存在しない"]))

    def test_mu_weight_rejected(self, db, tmp_path):
        p = tmp_path / "w.json"
        p.write_text(json.dumps({"x": {"z_roe": 1.0, "mu": 0.5}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            collect_weights(db, _args(weights_json=str(p)))

    def test_metric_outside_METRICS_rejected(self, db, tmp_path):
        p = tmp_path / "w.json"
        p.write_text(json.dumps({"x": {"z_nonexistent": 1.0}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            collect_weights(db, _args(weights_json=str(p)))

    def test_all_zero_weights_rejected(self, db, tmp_path):
        p = tmp_path / "w.json"
        p.write_text(json.dumps({"x": {"z_roe": 0.0}}), encoding="utf-8")
        with pytest.raises(SystemExit):
            collect_weights(db, _args(weights_json=str(p)))

    def test_missing_premia_run_rejected(self, db):
        with pytest.raises(SystemExit):
            collect_weights(db, _args(premia_run_id=["rfp_does_not_exist"]))


# ── IC 系列 ───────────────────────────────────────────────────────────────

class TestIcSeries:
    def test_perfect_ranking_gives_ic_one(self):
        X = np.asarray([[3.0], [2.0], [1.0], [0.0], [-1.0], [-2.0]])
        y = np.asarray([0.3, 0.2, 0.1, 0.0, -0.1, -0.2])
        ics = ic_series({"2024-01": (X, y)}, ["z_roe"], {"z_roe": 1.0})
        assert ics["2024-01"] == pytest.approx(1.0)

    def test_sign_flips_with_the_weight(self):
        X = np.asarray([[3.0], [2.0], [1.0], [0.0], [-1.0], [-2.0]])
        y = np.asarray([0.3, 0.2, 0.1, 0.0, -0.1, -0.2])
        ics = ic_series({"2024-01": (X, y)}, ["z_roe"], {"z_roe": -1.0})
        assert ics["2024-01"] == pytest.approx(-1.0)

    def test_keys_are_the_periods(self):
        X = np.asarray([[3.0], [2.0], [1.0], [0.0], [-1.0], [-2.0]])
        y = np.asarray([0.3, 0.2, 0.1, 0.0, -0.1, -0.2])
        panel = {"2024-01": (X, y), "2024-02": (X, y)}
        assert set(ic_series(panel, ["z_roe"], {"z_roe": 1.0})) == {"2024-01", "2024-02"}


# ── パネルの世代表示（ADR-0028 規則3）────────────────────────────────────

class TestPanelInfo:
    def test_reports_span_and_company_counts(self):
        panel = {
            "2024-02": (np.zeros((5, 1)), np.zeros(5)),
            "2024-01": (np.zeros((9, 1)), np.zeros(9)),
            "2024-03": (np.zeros((7, 1)), np.zeros(7)),
        }
        info = panel_info(panel, ["z_roe"])
        assert info["n_periods"] == 3
        assert info["first_ym"] == "2024-01"
        assert info["last_ym"] == "2024-03"
        assert info["companies_median"] == 7
        assert info["companies_min"] == 5
        assert info["companies_max"] == 9

    def test_empty_panel_does_not_raise(self):
        info = panel_info({}, [])
        assert info["n_periods"] == 0
        assert info["first_ym"] is None


# ── 判定表示 ─────────────────────────────────────────────────────────────

class TestFmtSig:
    def test_none_is_reported_as_na(self):
        assert "n/a" in fmt_sig(None, 0.05, 0.001)

    def test_p_at_bootstrap_floor_is_marked(self):
        """p の下限は 2/(n_boot+1)。「p=0.001 だから強い」と読まないための印。"""
        sig = {"mean": 0.18, "ci_lo": 0.11, "ci_hi": 0.26, "p_value": 0.001, "n_common": 61}
        out = fmt_sig(sig, 0.0125, 2 / 2001)
        assert "p at floor" in out
        assert "SIG" in out

    def test_not_significant_after_correction(self):
        sig = {"mean": 0.01, "ci_lo": -0.02, "ci_hi": 0.04, "p_value": 0.478, "n_common": 61}
        out = fmt_sig(sig, 0.0125, 2 / 2001)
        assert "ns" in out
        assert "p at floor" not in out
