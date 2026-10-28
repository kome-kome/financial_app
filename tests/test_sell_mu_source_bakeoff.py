"""`scripts/sell_mu_source_bakeoff.py --in-layer`（Issue #1043）の配線のテスト。

M-4 は回さない。共通域の取り出し（`_run_config(keep_common=True)`）・形の変換・買いの並び（U）・
上限版の売りスコア・引数と日中キューの登録を、合成の検体で確かめる。計算の本体は
tests/test_in_layer_short_side.py。
"""
import inspect

import pytest

import scripts.run_daytime as rd
import scripts.sell_mu_source_bakeoff as sb
from plugins import macro_ensemble
from plugins.sell_ranking import sell_score
from scripts.ensemble_base_bakeoff import _run_config

BASES = ["macro_risk_return", "macro_gbdt", "macro_enet"]
COMMON = {
    "2020-01": [("A", (0.1, 0.2, 0.3), 0.05), ("B", (0.4, 0.5, 0.6), -0.02)],
    "2020-04": [("A", (0.7, 0.8, 0.9), 0.01)],
}


class TestKeepCommon:
    def _fake_execute(self, calls_stack: bool):
        def execute(params, db):
            if calls_stack:
                macro_ensemble._stack_walk_forward(COMMON, "rank_ic_grid", 0.1, 3, n_base=3)
            return {"oof_backtest": {"rank_ic": {"mean": 0.0}, "n_periods": 0},
                    "base_oof_backtest": {}, "weights": {}, "n_common_pairs": 3}
        return execute

    def test_captures_the_first_argument_of_the_stacker(self, monkeypatch):
        monkeypatch.setattr(macro_ensemble, "_stack_walk_forward", lambda *a, **k: ({}, {}))
        monkeypatch.setattr(macro_ensemble.plugin, "execute", self._fake_execute(True))
        run = _run_config("t", BASES, ({}, {}, {}, {}), db=None, keep_common=True)
        assert run["common"] is COMMON

    def test_without_keep_common_nothing_is_added(self, monkeypatch):
        monkeypatch.setattr(macro_ensemble, "_stack_walk_forward", lambda *a, **k: ({}, {}))
        monkeypatch.setattr(macro_ensemble.plugin, "execute", self._fake_execute(True))
        assert "common" not in _run_config("t", BASES, ({}, {}, {}, {}), db=None)

    def test_missing_capture_fails_loudly(self, monkeypatch):
        """横取りが空振りしたら空の共通域で測らずに止まる。"""
        monkeypatch.setattr(macro_ensemble.plugin, "execute", self._fake_execute(False))
        with pytest.raises(RuntimeError):
            _run_config("t", BASES, ({}, {}, {}, {}), db=None, keep_common=True)

    def test_stacker_still_takes_common_first(self):
        """横取りは1番目の引数を読む。M-4 の側で引数を入れ替えたら、ここで気づく。"""
        params = list(inspect.signature(macro_ensemble._stack_walk_forward).parameters)
        assert params[0] == "common"
        src = inspect.getsource(macro_ensemble.MacroEnsemblePlugin.execute)
        assert "_stack_walk_forward(\n            common," in src or "_stack_walk_forward(common," in src


class TestShapes:
    def test_common_to_dicts(self):
        preds, y = sb.common_to_dicts(COMMON, BASES)
        assert preds["macro_gbdt"] == {("2020-01", "A"): 0.2, ("2020-01", "B"): 0.5,
                                       ("2020-04", "A"): 0.8}
        assert preds["macro_enet"][("2020-04", "A")] == 0.9
        assert y == {("2020-01", "A"): 0.05, ("2020-01", "B"): -0.02, ("2020-04", "A"): 0.01}

    def test_buy_scores_keeps_mu_when_lambda_is_zero(self):
        preds, y = sb.common_to_dicts(COMMON, BASES)
        buy, info = sb.buy_scores("macro_enet", preds["macro_enet"], y, prices_by_co={}, db=None)
        assert buy == preds["macro_enet"]
        assert info == {"lambda": 0.0, "risk_axis": None, "time_reproducible": True, "n_dropped_rows": 0}

    def test_buy_scores_uses_the_screen_u(self, monkeypatch):
        """λ>0 なら U = μ̂ − λR（生の R）。R が無い社は落とす。"""
        preds, y = sb.common_to_dicts(COMMON, BASES)
        rmap = {("2020-01", "A"): 0.05, ("2020-04", "A"): 0.5}
        monkeypatch.setattr(sb, "display_risk_map", lambda *a, **k: (2.0, "r2", rmap))
        buy, info = sb.buy_scores("macro_risk_return", preds["macro_risk_return"], y, {}, None)
        assert buy == pytest.approx({("2020-01", "A"): 0.1 - 2.0 * 0.05, ("2020-04", "A"): 0.7 - 2.0 * 0.5})
        assert info == {"lambda": 2.0, "risk_axis": "r2", "time_reproducible": True, "n_dropped_rows": 1}


class TestUpperBound:
    MU = {("2020-01", f"E{i}"): 0.01 * (i % 3) for i in range(12)}

    def test_higher_r_macro_is_sold_first_at_equal_mu(self):
        producer = {f"E{i}": {"r_macro": 0.1 + 0.01 * i} for i in range(12)}
        keys = sb.upper_bound_keys(self.MU, producer)
        # E0・E3・E6・E9 は μ̂ が同じ。R_macro が高い社ほど売りスコアが高い＝キーが低い。
        same_mu = [keys[("2020-01", f"E{i}")] for i in (0, 3, 6, 9)]
        assert same_mu == sorted(same_mu, reverse=True)

    def test_missing_r_macro_is_scored_by_mu_alone(self):
        """R_macro が無い社は μ だけで採点される（本番の `sell_score` と同じ・被覆率 2/3 ≥ 下限 0.4）。"""
        keys = sb.upper_bound_keys(self.MU, {})
        assert set(keys) == set(self.MU)
        order = sorted(self.MU, key=lambda k: (keys[k], k))
        assert [self.MU[k] for k in order] == sorted(self.MU.values())

    def test_preset_with_other_metrics_stops(self, monkeypatch):
        monkeypatch.setitem(sb.SELL_PRESETS, "マクロ予測型", {"mu": 1.0, "neg_r_macro": 0.5, "roe": 1.0})
        with pytest.raises(ValueError):
            sb.upper_bound_keys(self.MU, {})

    def test_same_formula_as_the_sell_ranking(self):
        """上限版のスコアは本番の `sell_score` と同じ値（式を書き写していない）。"""
        producer = {f"E{i}": {"r_macro": 0.1 + 0.02 * i} for i in range(12)}
        keys = sb.upper_bound_keys(self.MU, producer)
        from plugins.utils import fit_zscore_stats
        mus = list(self.MU.values())
        negs = [-producer[f"E{i}"]["r_macro"] for i in range(12)]
        stats = {"mu": fit_zscore_stats(mus), "neg_r_macro": fit_zscore_stats(negs)}
        score, _cov = sell_score({"mu": self.MU[("2020-01", "E5")], "neg_r_macro": negs[5]},
                                 {"mu": 1.0, "neg_r_macro": 0.5}, stats, 0.4)
        assert keys[("2020-01", "E5")] == pytest.approx(-score)


class TestDaytimeJob:
    KEY = "measure:in-layer-sell"

    def test_job_argv_is_accepted_by_the_script(self):
        argv = rd.JOBS[self.KEY].argv
        assert argv[1:4] == ("-m", "scripts.sell_mu_source_bakeoff", "--in-layer")
        args = sb.build_parser().parse_args(list(argv[3:]))
        assert args.in_layer and not args.smoke

    def test_job_is_parallel_sensitive(self):
        assert rd.JOBS[self.KEY].parallel_sensitive is True


class TestWholeUniverse:
    """全社の売り側spread（#1073）。M-4 の base_oof と同じ定義・同じ域で測る。"""
    YMS = ("2020-01", "2020-04")

    def _common(self):
        common = {}
        for k, ym in enumerate(self.YMS):
            common[ym] = [(f"E{i}", (0.01 * i, 0.02 * ((i * 7) % 12), 0.03 * ((i * 5 + k) % 12)),
                           0.01 * ((i * 3 + k) % 12) - 0.05) for i in range(12)]
        return common

    def _base_oof(self, common):
        """macro_ensemble と同じ組み方の base_oof。"""
        return {name: sb.oof_backtest({ym: [(yh[j], y) for _ec, yh, y in v] for ym, v in common.items()})
                for j, name in enumerate(BASES)}

    def test_value_is_period_mean_minus_bottom_fifth(self):
        common = self._common()
        preds, y = sb.common_to_dicts(common, BASES)
        other = {k: -v for k, v in preds["macro_enet"].items()}
        res = sb.whole_universe_short_side(common, {**preds, sb.UPPER: other}, y, self._base_oof(common))
        for ym in self.YMS:
            rows = sorted((other[(ym, ec)], y[(ym, ec)]) for ec, _yh, _y in common[ym])
            bottom = rows[:len(rows) // 5]
            want = sum(r[1] for r in rows) / len(rows) - sum(r[1] for r in bottom) / len(bottom)
            assert res["by_condition"][sb.UPPER]["by_period"][ym] == pytest.approx(want, abs=1e-6)
        assert res["n_common_rows"] == 24

    def test_mu_conditions_match_the_m4_base_oof(self):
        common = self._common()
        preds, y = sb.common_to_dicts(common, BASES)
        base = self._base_oof(common)
        res = sb.whole_universe_short_side(common, dict(preds), y, base)
        for name in BASES:
            assert res["by_condition"][name]["by_period"] == base[name]["short_side_spread_by_period"]

    def test_mismatch_with_base_oof_stops(self):
        common = self._common()
        preds, y = sb.common_to_dicts(common, BASES)
        base = self._base_oof(common)
        base["macro_enet"]["short_side_spread_by_period"]["2020-01"] += 0.01
        with pytest.raises(RuntimeError):
            sb.whole_universe_short_side(common, dict(preds), y, base)

    def test_all_conditions_share_one_domain(self):
        """どれかの条件にキーが無い社は全条件から外す（縮む側が有利に見える罠を避ける・ADR-0045）。
        域が縮めば base_oof と合わなくなるので、照合も止まる。"""
        common = self._common()
        preds, y = sb.common_to_dicts(common, BASES)
        upper = {k: v for k, v in preds["macro_enet"].items() if k != ("2020-01", "E3")}
        sells = {**preds, sb.UPPER: upper}
        res = sb.whole_universe_short_side(common, sells, y, base_oof={})
        assert res["n_common_rows"] == 23
        assert res["by_condition"]["macro_enet"]["by_period"] == res["by_condition"][sb.UPPER]["by_period"]
        with pytest.raises(RuntimeError):
            sb.whole_universe_short_side(common, sells, y, self._base_oof(common))
