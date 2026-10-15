"""tests/test_momentum_gate_seeds.py — M-2 を種を変えて平均する（Issue #955）

M-2（XGBoost）は `random_state` を固定していても、種が違えば subsample の抜き方が変わり、同じ
パネルで rank-IC が ±0.005〜0.01 動く。条件の差と同じ大きさなので、`momentum_gate` は既定で
5 個の種で回して行ごとに予測を平均してから測る（`candidate_bakeoff.run_seeds`）。

ここで固定するのは:
  - 平均は行ごと（同じ月・同じ行の予測どうし）で、実現値は変えない。別のパネルは混ぜない
  - 種の効く候補だけを回し直し、`seeds=1` は従来の `run_one` と同じ値になる
  - `momentum_gate` の 2 つの CV 段（raw と共通月）がどちらも種の数を渡す
"""
import datetime
import inspect
import math
from types import SimpleNamespace

import numpy as np
import pytest

import scripts.candidate_bakeoff as cb
from scripts import momentum_gate


# ── 行ごとの平均 ─────────────────────────────────────────────────────────────

class TestAverageResiduals:
    def test_averages_predictions_and_keeps_realized(self):
        a = {"2021-01": [(0.1, 1.0), (0.3, 2.0)], "2021-04": [(0.0, 3.0)]}
        b = {"2021-01": [(0.3, 1.0), (0.1, 2.0)], "2021-04": [(0.4, 3.0)]}
        out = cb.average_residuals([a, b])
        assert out == {"2021-01": [(pytest.approx(0.2), 1.0), (pytest.approx(0.2), 2.0)],
                       "2021-04": [(pytest.approx(0.2), 3.0)]}

    def test_refuses_different_months(self):
        with pytest.raises(ValueError, match="test 月"):
            cb.average_residuals([{"2021-01": [(0.1, 1.0)]}, {"2021-04": [(0.1, 1.0)]}])

    def test_refuses_different_rows(self):
        """実現値の並びが違う＝別のパネル。黙って平均すると別の社の予測を足し合わせる。"""
        with pytest.raises(ValueError, match="2021-01"):
            cb.average_residuals([{"2021-01": [(0.1, 1.0), (0.2, 2.0)]},
                                  {"2021-01": [(0.1, 2.0), (0.2, 1.0)]}])


# ── run_seeds の呼び方 ───────────────────────────────────────────────────────

def _fake_run_one(calls):
    """呼ばれた opts を記録し、種ごとに違う予測を返す偽の run_one。"""
    def run_one(name, s, m, i, feats, pca, *, opts=None, return_parts=False):
        calls.append(opts)
        rs = (opts or {}).get("random_state", 42)
        resid = {"2021-01": [(float(rs), 1.0), (float(rs) + 1.0, 2.0)]}
        out = {"name": name, "n_folds": 1, "elapsed_sec": 0.0,
               "oof": {"rank_ic": {"mean": rs / 100.0}, "short_side_spread": rs / 1000.0}}
        if return_parts:
            out["_parts"] = (resid, {"meta": rs})
        return out
    return run_one


class TestRunSeeds:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(cb, "run_one", _fake_run_one(self.calls))
        monkeypatch.setattr(cb, "_backtest", lambda resid, meta: {"averaged": resid, "meta": meta})

    def test_one_seed_is_the_plain_run(self):
        out = cb.run_seeds("xgb_m2", {}, {}, {}, [], 0, seeds=1)
        assert self.calls == [None]
        assert "seeds" not in out and "_parts" not in out

    def test_unseeded_candidate_runs_once(self):
        """M-6（elasticnet）は種で結果が変わらないので、何個指定しても 1 回だけ。"""
        cb.run_seeds("elasticnet", {}, {}, {}, [], 0, seeds=5)
        assert self.calls == [None]

    def test_seeds_are_consecutive_from_the_default(self):
        out = cb.run_seeds("xgb_m2", {}, {}, {}, [], 0, seeds=3, return_parts=True)
        assert [c["random_state"] for c in self.calls] == [42, 43, 44]
        resid, meta = out["_parts"]
        assert resid == {"2021-01": [(43.0, 1.0), (44.0, 2.0)]}   # 42/43/44 の平均
        assert meta == {"meta": 42}                                # パネルは同じなので 1 個目
        assert out["oof"]["averaged"] == resid                     # 指標は平均した残差から
        assert out["seeds"]["n"] == 3
        assert out["seeds"]["random_states"] == [42, 43, 44]
        assert out["seeds"]["rank_ic_by_seed"] == [0.42, 0.43, 0.44]

    def test_parts_are_left_out_unless_asked(self):
        out = cb.run_seeds("xgb_m2", {}, {}, {}, [], 0, seeds=2)
        assert "_parts" not in out

    def test_failed_seed_is_reported_not_averaged(self, monkeypatch):
        def failing(name, s, m, i, feats, pca, *, opts=None, return_parts=False):
            return {"name": name, "error": "全 fold 失敗: x", "_parts": ({}, {})}
        monkeypatch.setattr(cb, "run_one", failing)
        out = cb.run_seeds("xgb_m2", {}, {}, {}, [], 0, seeds=3)
        assert out["error"].startswith("全 fold 失敗")
        assert "_parts" not in out


# ── 本物の XGBoost で ───────────────────────────────────────────────────────

def _panel(n_companies=6, n_weeks=200):
    """社ごとに財務と株価の動きが違う小さなパネル（M-2 と同じ build_snapshots で作る）。"""
    from plugins.macro_snapshots import build_snapshots

    rng = np.random.default_rng(955)
    base = datetime.date(2019, 1, 4)
    pe = datetime.date(2018, 9, 1)
    prices, fins, cos = {}, {}, {}
    for ci in range(n_companies):
        ec = f"E{ci:05d}"
        close, rows = 1000.0, []
        for w in range(n_weeks):
            close *= math.exp(rng.normal(0.0, 0.03))
            d = (base + datetime.timedelta(weeks=w)).isoformat()
            rows.append(SimpleNamespace(trade_date=d, close_last=close))
        prices[ec] = rows
        fins[ec] = [SimpleNamespace(
            edinet_code=ec, sec_code=str(1000 + ci), company_name=f"会社{ci}",
            industry="テスト業", period_end=pe, bs_total_assets=1e10,
            per=5.0 + 3.0 * ci, pbr=0.5 + 0.4 * ((ci * 3) % n_companies))]
        cos[ec] = SimpleNamespace(edinet_code=ec, name=f"会社{ci}", sec_code=str(1000 + ci),
                                  industry="テスト業")
    s, m, _cur, feats, ids = build_snapshots(prices, fins, cos, {}, ["per", "pbr"], [],
                                             False, 12, 0.5, build_interactions=False,
                                             return_stock_ids=True)
    return s, m, ids, feats


class TestRealXgb:
    def test_one_seed_matches_run_one_and_more_seeds_average(self):
        s, m, ids, feats = _panel()
        plain = cb.run_one("xgb_m2", s, m, ids, feats, 0, return_parts=True)
        assert plain["n_folds"] > 0, "fold が回っていない（パネルの作り方を見直す）"
        one = cb.run_seeds("xgb_m2", s, m, ids, feats, 0, seeds=1, return_parts=True)
        assert one["_parts"][0] == plain["_parts"][0]
        assert one["oof"]["rank_ic"] == plain["oof"]["rank_ic"]

        two = cb.run_seeds("xgb_m2", s, m, ids, feats, 0, seeds=2, return_parts=True)
        assert two["seeds"]["rank_ic_by_seed"][0] == plain["oof"]["rank_ic"]["mean"]
        other = cb.run_one("xgb_m2", s, m, ids, feats, 0, opts={"random_state": 43},
                           return_parts=True)
        expected = cb.average_residuals([plain["_parts"][0], other["_parts"][0]])
        assert two["_parts"][0] == expected


# ── momentum_gate の配線 ─────────────────────────────────────────────────────

class TestGateWiring:
    def test_default_is_five_seeds(self):
        assert momentum_gate.SEEDS_DEFAULT == 5

    def test_both_cv_stages_pass_the_seed_count(self):
        """raw の段と共通月の段（主判定）の両方で種を平均する。片方だけだと判定と水準がずれる。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("run_seeds(") == 2
        assert src.count("seeds=args.seeds") == 2
        assert "run_one(" not in src

    def test_seed_spread_line(self):
        line = momentum_gate.seed_spread_line({
            "n": 3, "rank_ic_by_seed": [0.10, 0.12, 0.11],
            "short_side_spread_by_seed": [0.05, None, 0.04]})
        assert line.startswith("[seeds] n=3 ")
        assert "min=+0.1000 max=+0.1200 range=+0.0200" in line
        assert "short_side: min=+0.0400 max=+0.0500" in line
