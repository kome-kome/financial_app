"""tests/test_hyperparameter_search.py — run_search() 共有ロジック（Issue #278）。

CLI（hyperparameter_search.py の _run）と GUI ジョブ（routers/analysis.py）の
両方から呼ばれる run_search() が、persist/persist_scores/on_progress/cancel_check を
正しく plugins.tuning.search() へ橋渡しすることを検証する。
"""
import asyncio

import pytest

import hyperparameter_search as hs


class _FakePlugin:
    """x=5 が最適解の1軸探索プラグイン。execute 呼び出し回数を記録する。"""
    name = "fake_model"
    depends_on: list = []
    execute_calls = 0

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10}}

    def tuning_search_space(self):
        from plugins.tuning import SearchDim
        return {}, [SearchDim("x", [0, 3, 5, 7])]

    async def execute(self, params: dict, db) -> dict:
        type(self).execute_calls += 1
        score = -((params["x"] - 5) ** 2)
        return {"oof_backtest": {"rank_ic": {"mean": float(score), "std": 1.0, "n": 3}}}


class _NoSpacePlugin:
    """tuning_search_space() 未実装プラグイン。"""
    name = "no_space"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {}

    async def execute(self, params: dict, db) -> dict:
        return {}


@pytest.fixture(autouse=True)
def _reset_execute_calls():
    _FakePlugin.execute_calls = 0
    yield


class TestRunSearch:

    def test_plugin_not_found_raises_value_error(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: None)
        with pytest.raises(ValueError, match="見つかりません"):
            asyncio.run(hs.run_search("macro_gbdt", "grid", 50, "rank_ic", 0, db))

    def test_missing_tuning_search_space_raises_value_error(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _NoSpacePlugin())
        with pytest.raises(ValueError, match="tuning_search_space"):
            asyncio.run(hs.run_search("no_space", "grid", 50, "rank_ic", 0, db))

    def test_default_does_not_persist(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search("fake_model", "grid", 50, "rank_ic", 0, db))
        assert result["persisted"] is False
        assert result["best_params"]["x"] == 5
        from database import get_tuned_params
        assert get_tuned_params(db, "fake_model") is None

    def test_persist_writes_plugin_tuned_params(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))
        assert result["persisted"] is True
        from database import get_tuned_params
        tuned = get_tuned_params(db, "fake_model")
        assert tuned is not None
        assert tuned["params"]["x"] == 5
        # persist_scores=False（既定）なので execute は探索中の候補評価分のみ（追加の1回は無い）
        assert _FakePlugin.execute_calls == 4

    def test_persist_scores_runs_extra_execute_with_best_params(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db,
            persist=True, persist_scores=True,
        ))
        # 探索4候補 + best params での最終 execute 1回 = 5回
        assert _FakePlugin.execute_calls == 5

    def test_persist_scores_without_persist_is_ignored(self, db, monkeypatch):
        """persist=False のとき persist_scores=True を渡しても無視される（永続化されない）。"""
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db,
            persist=False, persist_scores=True,
        ))
        assert result["persisted"] is False
        assert _FakePlugin.execute_calls == 4  # 追加 execute は起きない

    def test_on_progress_and_cancel_check_propagate_to_search(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        calls = []
        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db,
            on_progress=lambda cur, tot, msg: calls.append((cur, tot)),
            cancel_check=lambda: False,
        ))
        assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]
        assert result["config"]["cancelled"] is False

    def test_cancel_mid_search_skips_persist_when_no_scored_candidate(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db,
            persist=True, cancel_check=lambda: True,
        ))
        assert result["config"]["cancelled"] is True
        assert result["persisted"] is False
        from database import get_tuned_params
        assert get_tuned_params(db, "fake_model") is None
