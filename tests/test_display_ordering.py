"""画面の並びの採点（Issue #1042・ADR-0069）のユニットテスト。

`plugins/macro_snapshots.py` の display_ordering_defaults / display_ordering_oof / risk_by_ym の
速い経路と、`database.wants_display_scoring` を合成の検体で確かめる。各プラグインへの組み込みは
tests/test_macro_risk_return.py・test_macro_gbdt.py・test_macro_dlm.py・test_model_comparison.py。
"""
import random
from collections import namedtuple
from datetime import date, timedelta

import pytest

import database
from plugins import get_plugin
from plugins.macro_snapshots import (
    _month_end_bound,
    _realized_vol,
    apply_risk_axis,
    display_ordering_defaults,
    display_ordering_oof,
    oof_backtest,
    risk_by_ym,
)

PX = namedtuple("PX", "trade_date close_last volume_sum")


def _meta(ym_rows):
    return {ym: [(f"E{i:02d}", "A") for i in range(len(rows))] for ym, rows in ym_rows.items()}


def _rmap(ym_rows, r_of):
    return {(ym, f"E{i:02d}"): r_of(i) for ym, rows in ym_rows.items() for i in range(len(rows))}


class TestDefaults:
    @pytest.mark.parametrize("name", ["macro_risk_return", "macro_gbdt", "macro_enet"])
    def test_reads_the_schema_defaults(self, name):
        schema = get_plugin(name).params_schema()
        assert display_ordering_defaults(schema) == (
            float(schema["lambda_risk"]["default"]), schema["risk_axis"]["default"])

    def test_plugin_without_axis_choice_gets_the_given_axis(self):
        """M-3 は横軸の選択肢を持たず R_macro 固定。"""
        schema = get_plugin("macro_dlm").params_schema()
        assert "risk_axis" not in schema
        lam, axis = display_ordering_defaults(schema, default_axis="r_macro")
        assert (lam, axis) == (float(schema["lambda_risk"]["default"]), "r_macro")

    def test_momentum_gate_reads_the_same_source(self):
        """λ の源を2つにしない（#808）。ゲートの分母と画面の並びの採点が同じ (λ, 横軸) を読む。"""
        from scripts.momentum_gate import RISK_LAMBDA_PLUGIN, production_risk
        for model, plugin_name in RISK_LAMBDA_PLUGIN.items():
            assert production_risk(model) == display_ordering_defaults(
                get_plugin(plugin_name).params_schema())


class TestDisplayOrderingOof:
    # 20 社。yhat=0.01i。i≥10 の社だけ R=0.02i を持つので、λ=1 の U は i<10 で 0.01i、i≥10 で −0.01i。
    # 実現リターンを U と同じにして、U の順なら完全に当たり μ̂ の順なら上位が外れる検体にする。
    @staticmethod
    def _case():
        rows = {"2020-01": [(0.01 * i, (0.01 * i if i < 10 else -0.01 * i)) for i in range(20)]}
        rmap = _rmap(rows, lambda i: 0.0 if i < 10 else 0.02 * i)
        return rows, _meta(rows), rmap

    def test_lambda_zero_returns_nothing(self):
        rows, meta, rmap = self._case()
        assert display_ordering_oof(rows, meta, rmap, 0.0, "r2", True) is None

    def test_lambda_zero_transform_is_the_forecast_scoring(self):
        """λ=0 の U は μ̂ そのもの＝予想の採点と1ビットも変わらない（恒等）。"""
        rows, meta, rmap = self._case()
        u_res, u_meta, dropped = apply_risk_axis(rows, meta, rmap, 0.0, standardize=False)
        assert dropped == 0
        assert oof_backtest(u_res, n_quantiles=5, meta_by_ym=u_meta) == \
            oof_backtest(rows, n_quantiles=5, meta_by_ym=meta)

    def test_scores_the_u_ordering(self):
        rows, meta, rmap = self._case()
        forecast = oof_backtest(rows, n_quantiles=5, meta_by_ym=meta)
        display = display_ordering_oof(rows, meta, rmap, 1.0, "r2", True)
        assert display["rank_ic"]["mean"] == 1.0          # U の順は実現と完全に一致
        assert forecast["rank_ic"]["mean"] < 0.5
        # 上位 5 社: U の順は i=5..9（y 平均 0.07）、μ̂ の順は i=15..19（y 平均 −0.17）。期内平均は −0.05
        assert display["top_k"]["5"]["excess_return"] == pytest.approx(0.12, abs=1e-6)
        assert forecast["top_k"]["5"]["excess_return"] == pytest.approx(-0.12, abs=1e-6)
        assert display["ordering"] == {"lambda": 1.0, "risk_axis": "r2",
                                       "time_reproducible": True, "n_dropped_rows": 0}

    def test_rows_without_risk_are_dropped_and_counted(self):
        rows, meta, rmap = self._case()
        del rmap[("2020-01", "E03")], rmap[("2020-01", "E17")]
        display = display_ordering_oof(rows, meta, rmap, 1.0, "r2", True)
        assert display["ordering"]["n_dropped_rows"] == 2
        assert display["n_oof_samples"] == 18

    def test_no_risk_at_all_drops_every_row(self):
        """R が1社も無いのに予想の採点と同じ値を出さない（apply_risk_axis の空は恒等変換）。"""
        rows, meta, _rmap_ = self._case()
        display = display_ordering_oof(rows, meta, {}, 1.0, "r_macro", False)
        assert display["ordering"]["n_dropped_rows"] == 20
        assert display["n_oof_samples"] == 0
        assert display["rank_ic"]["n"] == 0

    def test_future_informed_axis_is_marked(self):
        rows, meta, rmap = self._case()
        display = display_ordering_oof(rows, meta, rmap, 1.0, "r_macro", False)
        assert display["ordering"]["time_reproducible"] is False
        assert display["ordering"]["risk_axis"] == "r_macro"


class TestRiskByYmFastPath:
    """R2 の速い経路（`_vol_index`）が、全期間を毎回走査する素の `_realized_vol` と同じ値を返す。"""

    YMS = [f"{y}-{m:02d}" for y in range(2019, 2023) for m in (1, 4, 7, 10)]

    @staticmethod
    def _prices(seed, n_weeks=230, with_gaps=False):
        rng = random.Random(seed)
        p, rows = 1000.0, []
        start = date(2018, 1, 5)
        for w in range(n_weeks):
            p *= 1.0 + rng.gauss(0, 0.04)
            close = p
            if with_gaps and rng.random() < 0.1:
                close = rng.choice([0.0, None, -1.0])     # 株価の欠け・不正値は両経路とも外す
            rows.append(PX((start + timedelta(weeks=w)).isoformat(), close, 1))
        return rows

    def _naive(self, prices, ids_by_ym):
        out = {}
        for ym, ids in ids_by_ym.items():
            for ec in ids:
                v = _realized_vol(prices.get(ec) or [], _month_end_bound(ym))
                if v is not None:
                    out[(ym, ec)] = float(v)
        return out

    def test_matches_the_full_scan(self):
        prices = {f"E{c}": self._prices(c, with_gaps=(c % 2 == 0)) for c in range(6)}
        prices["E9"] = self._prices(9, n_weeks=20)          # 履歴が短い社（早い月は None）
        ids = {ym: list(prices) + ["E_missing"] for ym in self.YMS}
        got = risk_by_ym("r2", prices, ids, {})
        assert got == self._naive(prices, ids)
        assert len(got) > len(self.YMS) * 5

    def test_unsorted_rows_fall_back_to_the_full_scan(self):
        """日付順でない社は素の `_realized_vol` へ倒す（並べ直すと直近の行が変わりうる）。"""
        rows = self._prices(3)
        random.Random(1).shuffle(rows)
        prices = {"E1": rows}
        ids = {ym: ["E1"] for ym in self.YMS}
        assert risk_by_ym("r2", prices, ids, {}) == self._naive(prices, ids)

    def test_r_macro_and_unknown_axis_are_unchanged(self):
        ids = {"2020-01": ["E1", "E2"]}
        assert risk_by_ym("r_macro", {}, ids, {"E1": {"r_macro": 0.3}}) == {("2020-01", "E1"): 0.3}
        assert risk_by_ym(None, {}, ids, {}) == {}
        with pytest.raises(ValueError, match="未知のリスク軸"):
            risk_by_ym("r99", {}, ids, {})


class TestWantsDisplayScoring:
    def test_normal_run_scores_the_display_ordering(self):
        assert database.wants_display_scoring() is True

    def test_search_does_not(self):
        with database.tuning_objective_only():
            assert database.wants_display_scoring() is False

    def test_model_comparison_switch_turns_it_back_on(self):
        with database.tuning_objective_only(), database.display_scoring():
            assert database.wants_display_scoring() is True
        assert database.wants_display_scoring() is True
