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

# 比較対象は COMPARISON_MODELS から導出する（兄弟モデル追加時にテスト側の
# ハードコード一覧が取り残されないように・Issue #372 で M-6 追加時に実際に取り残された）。
_MODELS = tuple(n for n, _ in model_comparison.COMPARISON_MODELS)
_SHORTS = [s for _, s in model_comparison.COMPARISON_MODELS]


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
        assert [m["short"] for m in res["models"]] == _SHORTS
        assert all(m["available"] for m in res["models"])
        assert res["models"][0]["oof_backtest"]["rank_ic"]["mean"] == 0.05
        assert res["models"][0]["label"] == "macro_risk_returnラベル"
        assert "computed_at" in res

    def test_display_ordering_scoring_is_requested_and_kept(self, monkeypatch):
        """各モデルを探索と同じ早期 return で回しつつ、画面の並びの採点（#1042）だけは出させて並べる。

        ▲▼（significance_matrix）は予想の採点（oof_backtest の rank_ic_by_period）から作る。
        """
        import database

        pmap = {n: _fake_plugin(n) for n in _MODELS}
        seen = []

        async def _exec(p, params, db):
            seen.append((database.is_tuning_objective_only(), database.wants_display_scoring()))
            display = (None if p.name == "macro_enet" else
                       {"rank_ic": {"mean": 0.5}, "rank_ic_by_period": {"2020-01": 0.9},
                        "ordering": {"lambda": 1.0}})
            return {"oof_backtest": {"rank_ic": {"mean": 0.05},
                                     "rank_ic_by_period": {"2020-01": 0.1, "2020-04": 0.2}},
                    "oof_backtest_display": display}

        _patch(monkeypatch, pmap, _exec)
        res = _run()
        assert seen and all(s == (True, True) for s in seen)
        by = {m["name"]: m for m in res["models"]}
        assert by["macro_risk_return"]["oof_backtest_display"]["ordering"] == {"lambda": 1.0}
        assert by["macro_enet"]["oof_backtest_display"] is None
        pair = next(iter(res["significance_matrix"]["pairs"].values()))
        assert pair["n_common"] == 2, "▲▼ が画面の並びの採点（1期）から作られている"

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


class TestSignificanceCorrection:
    """画面の有意性マトリクスは全ペアで Bonferroni を掛ける（#741・ADR-0063）。

    以前は `significance_matrix` の alpha が判定に届かず、15 組を補正なしの 95% で ▲/▼ 判定していた。
    """

    @staticmethod
    def _exec_with_ic(levels):
        yms = [f"2020-{m:02d}" for m in range(1, 9)]

        async def _exec(p, params, db):
            return {"oof_backtest": {
                "rank_ic": {"mean": levels[p.name], "std": 0.0, "n": len(yms)},
                "rank_ic_by_period": {ym: levels[p.name] for ym in yms},
            }}
        return _exec

    def test_all_pairs_are_bonferroni_corrected(self, monkeypatch):
        levels = {n: 0.02 * (i + 1) for i, n in enumerate(_MODELS)}
        _patch(monkeypatch, {n: _fake_plugin(n) for n in _MODELS}, self._exec_with_ic(levels))
        sm = _run()["significance_matrix"]
        n_pairs = len(_MODELS) * (len(_MODELS) - 1) // 2
        assert sm["correction"] == "bonferroni"
        assert sm["n_tests"] == n_pairs
        assert sm["family_alpha"] == 0.05
        assert sm["alpha"] == pytest.approx(0.05 / n_pairs)
        assert len(sm["pairs"]) == n_pairs

    def test_subset_of_two_models_keeps_alpha(self, monkeypatch):
        # CLI の `--models a,b`（退役判断の実測・#570）は1組＝補正しても α 0.05 のまま。
        two = _MODELS[:2]
        levels = {n: 0.02 * (i + 1) for i, n in enumerate(_MODELS)}
        _patch(monkeypatch, {n: _fake_plugin(n) for n in _MODELS}, self._exec_with_ic(levels))
        sm = _run(only_models=list(two))["significance_matrix"]
        assert sm["n_tests"] == 1
        assert sm["alpha"] == pytest.approx(0.05)


# ── 本番の設定の行（#1115・ADR-0070 決定 6）────────────────────────────────────

class TestProductionVariant:
    """M-2 は本番で探索の 1 位の設定が動いている。既定の行の直後に本番の設定の行を足す。

    - 本番の行は選択の楽観を含むので ▲▼ と Bonferroni の組数には入れない（判定は既定の行だけ）
    - 本番の設定が無ければ行は理由だけを持つ（数値は出さない）
    - データが探索した日から変わっていても行は出し、`panel_changed` で注記する（#1115 でユーザー判断）
    """

    TUNED = {"params": {"learning_rate": 0.01, "use_momentum": True, "gone_axis": 3},
             "data_fingerprint": "fp-at-tuning", "tuned_at": "2026-11-01T05:00:00+00:00"}

    @staticmethod
    def _gbdt():
        from plugins.tuning import SearchDim

        def space():
            return {"use_momentum": False}, [SearchDim("learning_rate", [0.01, 0.05])]
        return SimpleNamespace(name="macro_gbdt", label="M-2", heavy=True, tuning_search_space=space)

    def _setup(self, monkeypatch, tuned=TUNED, current_fp="fp-now"):
        import database
        import hyperparameter_search

        pmap = {n: _fake_plugin(n) for n in _MODELS}
        pmap["macro_gbdt"] = self._gbdt()
        self.calls = []

        async def _exec(p, params, db):
            self.calls.append((p.name, dict(params), database.is_tuning_objective_only(),
                               database._tuning_dry_run.get()))
            return {"oof_backtest": {"rank_ic": {"mean": 0.1},
                                     "rank_ic_by_period": {"2020-01": 0.1, "2020-04": 0.2, "2020-07": 0.15}}}

        _patch(monkeypatch, pmap, _exec)
        monkeypatch.setattr(database, "get_tuned_params",
                            lambda db, name: tuned if name == "macro_gbdt" else None)
        monkeypatch.setattr(hyperparameter_search, "_data_fingerprint", lambda db: current_fp)

    @staticmethod
    def _prod(res):
        return next(m for m in res["models"] if model_comparison.is_production(m))

    def test_the_production_row_follows_the_default_row(self, monkeypatch):
        self._setup(monkeypatch)
        res = _run(include_production=True)
        assert [m["short"] for m in res["models"]] == ["M-1", "M-2", "M-2（本番）", "M-3", "M-4", "M-5", "M-6"]
        prod = res["models"][2]
        assert prod["name"] == "macro_gbdt" and prod["selection_optimism"] is True
        assert prod["available"] is True
        # 画面と同じ射影: base は上書き・軸は保存値・空間から消えたキーは落とす
        assert prod["params"] == {"learning_rate": 0.01, "use_momentum": False}
        assert prod["stale_params"] == ["gone_axis", "use_momentum"]
        assert prod["tuned_at"] == self.TUNED["tuned_at"]
        gbdt = [c for c in self.calls if c[0] == "macro_gbdt"]
        assert [c[1] for c in gbdt] == [{}, prod["params"]], "既定の行は {}・本番の行は射影した設定で回す"
        assert all(c[2] and c[3] for c in gbdt), "本番の行も objective_only と dry_run の内側（本番の μ̂ を上書きしない）"

    def test_the_production_row_is_kept_out_of_the_verdicts(self, monkeypatch):
        """▲▼ の組にも Bonferroni の組数にも入れない（入れると既定の行どうしの基準まで変わる）。"""
        self._setup(monkeypatch)
        with_prod = _run(include_production=True)["significance_matrix"]
        without = _run()["significance_matrix"]
        assert with_prod["models"] == without["models"] == _SHORTS
        assert with_prod["n_tests"] == without["n_tests"] == 15

    def test_without_a_tuned_row_only_the_reason_is_shown(self, monkeypatch):
        self._setup(monkeypatch, tuned=None)
        prod = self._prod(_run(include_production=True))
        assert prod["available"] is False and prod["reason"] == "no_production_config"
        assert "oof_backtest" not in prod
        assert [c[1] for c in self.calls if c[0] == "macro_gbdt"] == [{}], "本番の設定が無いのに回した"

    def test_a_changed_panel_is_noted_not_hidden(self, monkeypatch):
        self._setup(monkeypatch, current_fp="fp-now")
        prod = self._prod(_run(include_production=True))
        assert prod["available"] is True and prod["panel_changed"] is True

    def test_the_same_panel_and_an_unreadable_panel(self, monkeypatch):
        import hyperparameter_search
        self._setup(monkeypatch, current_fp="fp-at-tuning")
        assert self._prod(_run(include_production=True))["panel_changed"] is False

        def _boom(db):
            raise RuntimeError("count(*) に失敗")
        monkeypatch.setattr(hyperparameter_search, "_data_fingerprint", _boom)
        prod = self._prod(_run(include_production=True))
        assert prod["available"] is True and prod["panel_changed"] is None, "指紋が取れなくても行は出す"

    def test_render_skips_the_production_row_too(self, monkeypatch):
        self._setup(monkeypatch)
        prod = self._prod(_run(include_production=True, render_light_mode=True))
        assert prod["reason"] == "heavy_render"
        assert self.calls == []

    def test_the_default_call_has_no_production_row(self, monkeypatch):
        """既定は False＝既存の呼び出し（CLI・テスト）の行の集合を変えない。"""
        self._setup(monkeypatch)
        assert not any(model_comparison.is_production(m) for m in _run()["models"])

    def test_a_failing_production_run_does_not_stop_the_comparison(self, monkeypatch):
        self._setup(monkeypatch)
        real = plugin_registry.execute_plugin

        async def _exec(p, params, db):
            if p.name == "macro_gbdt" and params:
                raise ValueError("本番の設定が契約を満たさない")
            return await real(p, params, db)

        monkeypatch.setattr(plugin_registry, "execute_plugin", _exec)
        res = _run(include_production=True)
        prod = self._prod(res)
        assert prod["available"] is False and prod["reason"] == "value_error"
        assert res["models"][-1]["available"] is True, "後続のモデルまで止まった"
