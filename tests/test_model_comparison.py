"""model_comparison.run_comparison のユニットテスト（HTTP 非依存）。

実モデル（M-1/M-2/M-3）は heavy かつ実 DB・重い CV が要るため、plugins.get_plugin /
execute_plugin を monkeypatch して「oof 集約」と「per-model graceful-degrade」だけを検証する。
run_comparison は tuning_objective_only()/tuning_dry_run()（database.py の contextvars）を
実際に使うが、これらは contextvar を立てるだけなので DB なしで動く。
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_comparison
import plugins as plugin_registry
from plugins.base import DependencyError

_MODELS = ("macro_risk_return", "macro_gbdt", "macro_dlm")


def _fake_plugin(name, label="ラベル", heavy=True):
    return SimpleNamespace(name=name, label=label, heavy=heavy)


def _patch(monkeypatch, plugins_map, execute_impl):
    monkeypatch.setattr(plugin_registry, "get_plugin", lambda n: plugins_map.get(n))
    monkeypatch.setattr(plugin_registry, "execute_plugin", execute_impl)


def _run(**kw):
    return asyncio.run(model_comparison.run_comparison(MagicMock(), **kw))


class TestRunComparison:
    def test_all_models_available(self, monkeypatch):
        pmap = {n: _fake_plugin(n, f"{n}ラベル") for n in _MODELS}

        async def _exec(p, params, db):
            assert params == {}          # 既定パラメータ（coerce 補完）で呼ぶ契約
            return {"oof_backtest": {
                "rank_ic": {"mean": 0.05, "std": 0.10, "n": 6},
                "quantile_returns": [-0.02, 0.0, 0.03],
                "long_short_spread": 0.05, "hit_rate": 0.6,
            }, "macro_features": ["dlm_usdjpy"]}

        _patch(monkeypatch, pmap, _exec)
        res = _run()
        assert [m["short"] for m in res["models"]] == ["M-1", "M-2", "M-3"]
        assert all(m["available"] for m in res["models"])
        assert res["models"][0]["oof_backtest"]["rank_ic"]["mean"] == 0.05
        assert res["models"][0]["label"] == "macro_risk_returnラベル"
        assert "computed_at" in res

    def test_render_light_mode_skips_all_heavy(self, monkeypatch):
        pmap = {n: _fake_plugin(n, heavy=True) for n in _MODELS}

        async def _exec(p, params, db):
            raise AssertionError("heavy モデルは Render 軽量モードで実行してはいけない")

        _patch(monkeypatch, pmap, _exec)
        res = asyncio.run(
            model_comparison.run_comparison(MagicMock(), render_light_mode=True))
        assert all(m["available"] is False and m["reason"] == "heavy_render"
                   for m in res["models"])

    def test_non_heavy_runs_even_on_render(self, monkeypatch):
        # heavy=False のモデルは Render でも実行される（heavy ゲートは heavy のみ対象）
        pmap = {n: _fake_plugin(n, heavy=False) for n in _MODELS}

        async def _exec(p, params, db):
            return {"oof_backtest": {"rank_ic": {"mean": 0.0}, "quantile_returns": []}}

        _patch(monkeypatch, pmap, _exec)
        res = asyncio.run(
            model_comparison.run_comparison(MagicMock(), render_light_mode=True))
        assert all(m["available"] for m in res["models"])

    def test_value_error_degrades_single_model(self, monkeypatch):
        pmap = {n: _fake_plugin(n) for n in _MODELS}

        async def _exec(p, params, db):
            if p.name == "macro_gbdt":
                raise ValueError("推定可能な銘柄がありません")
            return {"oof_backtest": {"rank_ic": {"mean": 0.0}, "quantile_returns": []}}

        _patch(monkeypatch, pmap, _exec)
        by = {m["name"]: m for m in _run()["models"]}
        assert by["macro_gbdt"]["available"] is False
        assert by["macro_gbdt"]["reason"] == "value_error"
        assert "推定可能" in by["macro_gbdt"]["error"]
        assert by["macro_risk_return"]["available"] is True   # 他モデルは継続
        assert by["macro_dlm"]["available"] is True

    def test_dependency_error_reason(self, monkeypatch):
        pmap = {n: _fake_plugin(n) for n in _MODELS}

        async def _exec(p, params, db):
            # DependencyError(plugin_name, unsatisfied, message) の3引数契約（plugins/base.py）
            raise DependencyError(p.name, ["sector_ols"], "先に producer を実行してください")

        _patch(monkeypatch, pmap, _exec)
        assert all(m["reason"] == "dependency" for m in _run()["models"])

    def test_generic_exception_reason(self, monkeypatch):
        pmap = {n: _fake_plugin(n) for n in _MODELS}

        async def _exec(p, params, db):
            raise RuntimeError("想定外")

        _patch(monkeypatch, pmap, _exec)
        assert all(m["reason"] == "error" for m in _run()["models"])

    def test_rollback_on_failure_prevents_cascade(self, monkeypatch):
        """1モデルの DB エラーで session が失敗状態になっても、失敗モデルの rollback で
        後続モデルが連鎖失敗しない（実DB smoke で M-1 失敗→M-2/M-3 が invalid transaction
        で巻き添えになった回帰の固定）。rollback は失敗時のみ＝成功モデルはキャッシュ維持のため
        rollback しない（M-1 失敗の1回だけ）。"""
        pmap = {n: _fake_plugin(n) for n in _MODELS}

        async def _exec(p, params, db):
            if p.name == "macro_risk_return":
                raise RuntimeError("lost synchronization with server")
            return {"oof_backtest": {"rank_ic": {"mean": 0.02}, "quantile_returns": []}}

        db = MagicMock()
        monkeypatch.setattr(plugin_registry, "get_plugin", lambda n: pmap.get(n))
        monkeypatch.setattr(plugin_registry, "execute_plugin", _exec)
        res = asyncio.run(model_comparison.run_comparison(db))
        by = {m["name"]: m for m in res["models"]}
        assert by["macro_risk_return"]["available"] is False   # 先頭が落ちても
        assert by["macro_gbdt"]["available"] is True            # 後続は独立に成功
        assert by["macro_dlm"]["available"] is True
        assert db.rollback.call_count == 1                      # 失敗した M-1 の1回だけ

    def test_no_rollback_when_all_succeed(self, monkeypatch):
        """全モデル成功時は rollback しない（snapshot_cache の ORM オブジェクトを
        expire させないため）。"""
        pmap = {n: _fake_plugin(n) for n in _MODELS}

        async def _exec(p, params, db):
            return {"oof_backtest": {"rank_ic": {"mean": 0.01}, "quantile_returns": []}}

        db = MagicMock()
        monkeypatch.setattr(plugin_registry, "get_plugin", lambda n: pmap.get(n))
        monkeypatch.setattr(plugin_registry, "execute_plugin", _exec)
        asyncio.run(model_comparison.run_comparison(db))
        assert db.rollback.call_count == 0

    def test_missing_plugin_not_registered(self, monkeypatch):
        async def _exec(p, params, db):
            return {"oof_backtest": {}}

        _patch(monkeypatch, {}, _exec)   # get_plugin は常に None
        res = _run()
        assert all(m["available"] is False and m["reason"] == "not_registered"
                   for m in res["models"])
