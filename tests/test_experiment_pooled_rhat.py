"""tests/test_experiment_pooled_rhat.py — Issue #341 プール検証スクリプトの高速テスト。

subsample_panel は純 numpy で常時実行。pool_and_diagnose は arviz を使うため
（summarize_diagnostics 経由）CI（requirements.txt のみ）では importorskip でスキップされる。
実 MCMC は回さず、az.from_dict で手組みした微小 idata でプール連結ロジックだけを検証する。
"""
import numpy as np
import pytest

from scripts.experiment_pooled_rhat import STRICT_GATE, pool_and_diagnose, subsample_panel


# ── subsample_panel（純 numpy・常時実行）──────────────────────────────────────────

def _fake_panel(n_stock=10, n_obs=200, n_factor=2, n_sector=3, seed=0):
    """build_panel と同形の返り値を合成する（stock_idx=観測粒度・sector_idx=銘柄粒度）。"""
    rng = np.random.default_rng(seed)
    returns = rng.normal(size=n_obs)
    macro = rng.normal(size=(n_obs, n_factor))
    stock_idx = rng.integers(0, n_stock, size=n_obs)
    sector_idx = np.array([i % n_sector for i in range(n_stock)], dtype=int)
    factor_names = [f"f{i}" for i in range(n_factor)]
    edinet_codes = [f"E{i:05d}" for i in range(n_stock)]
    sector_names = [f"sec{i}" for i in range(n_sector)]
    return returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names


class TestSubsamplePanel:

    def test_reduces_stock_count(self):
        panel = _fake_panel(n_stock=10)
        out = subsample_panel(panel, n_stocks=4, seed=1)
        _r, _m, _si, sector_idx, _fn, edinet_codes, _sn = out
        assert len(edinet_codes) == 4
        assert len(sector_idx) == 4          # sector_idx は銘柄粒度

    def test_remaps_indices_contiguous(self):
        panel = _fake_panel(n_stock=12, n_obs=400)
        out = subsample_panel(panel, n_stocks=5, seed=2)
        returns, macro, stock_idx, sector_idx, _fn, edinet_codes, sector_names = out
        # stock_idx は 0..4 の連番へ remap されている
        assert set(stock_idx.tolist()) <= set(range(5))
        assert stock_idx.max() < 5
        # sector_idx も 0..(n_sector-1) の連番（消えたセクターは詰める）
        assert set(sector_idx.tolist()) == set(range(len(sector_names)))
        # 観測数の整合（returns と macro が同じ長さ・stock_idx と一致）
        assert len(returns) == len(stock_idx) == macro.shape[0]

    def test_zero_or_large_returns_all_unchanged(self):
        panel = _fake_panel(n_stock=8)
        assert subsample_panel(panel, n_stocks=0) is panel          # 0=全件
        assert subsample_panel(panel, n_stocks=8) is panel          # ちょうど全件
        assert subsample_panel(panel, n_stocks=99) is panel         # 超過


# ── pool_and_diagnose（arviz 必須・CI では自動スキップ）────────────────────────────

def _fake_idata(az, seed, n_chains=2, n_draws=60, n_stock=3, n_factor=2):
    """summarize_diagnostics が読む beta/alpha/mu_universe + sample_stats.diverging を持つ idata。"""
    rng = np.random.default_rng(seed)
    posterior = {
        "beta": rng.normal(size=(n_chains, n_draws, n_stock, n_factor)),
        "alpha": rng.normal(size=(n_chains, n_draws, n_stock)),
        "mu_universe": rng.normal(size=(n_chains, n_draws, n_factor)),
    }
    sample_stats = {"diverging": np.zeros((n_chains, n_draws), dtype=bool)}
    return az.from_dict(posterior=posterior, sample_stats=sample_stats)


class TestPoolAndDiagnose:

    def test_trend_rows_stack_chains(self):
        az = pytest.importorskip("arviz")
        idatas = [_fake_idata(az, seed=s) for s in range(3)]     # 各 run 2チェーン
        rows = pool_and_diagnose(idatas)
        assert [r["n_runs"] for r in rows] == [1, 2, 3]
        assert [r["n_chains"] for r in rows] == [2, 4, 6]        # プールで 2→4→6 チェーン
        for r in rows:
            assert r["r_hat_max"] is not None and r["r_hat_max"] > 0
            assert r["ess_bulk_min"] is not None
            assert r["n_divergences"] == 0                       # 手組みは発散ゼロ

    def test_single_run_returns_one_row(self):
        az = pytest.importorskip("arviz")
        rows = pool_and_diagnose([_fake_idata(az, seed=0)])
        assert len(rows) == 1
        assert rows[0]["n_runs"] == 1 and rows[0]["n_chains"] == 2


def test_strict_gate_constant():
    # 実験の合否基準は ADR-0002 の strict 収束基準に一致
    assert STRICT_GATE == 1.01
