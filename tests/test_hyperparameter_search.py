"""tests/test_hyperparameter_search.py — run_search() 共有ロジック（Issue #264）。

CLI（hyperparameter_search.py の _run）・GitHub Actions（Issue #292）から呼ばれる
run_search() が、persist/persist_scores を正しく plugins.tuning.search() へ
橋渡しすることを検証する。
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

class TestQualityGate:
    """persist=True 時の劣化防止ゲート（Issue #291）。"""

    def test_improved_score_persists(self, db, monkeypatch):
        """前回 objective_value より今回 best_score が高ければ従来通り persist される。"""
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 3}, "rank_ic", -100.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["persisted"] is True
        assert result["skipped_reason"] is None
        tuned = get_tuned_params(db, "fake_model")
        assert tuned["params"]["x"] == 5
        assert tuned["objective_value"] == result["best_score"]

    def test_degraded_score_skips_persist(self, db, monkeypatch):
        """前回 objective_value より今回 best_score が低ければ persist せずスキップする。"""
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        # x=5 が最適解（score=0）なので、前回スコアをそれより高い値にしておくと必ず劣化扱いになる。
        upsert_tuned_params(db, "fake_model", {"x": 5}, "rank_ic", 100.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True, persist_scores=True,
        ))

        assert result["persisted"] is False
        assert result["skipped_reason"] is not None
        # persist_scores 分岐（producer スコア永続化のための追加 execute）にも入らない
        assert _FakePlugin.execute_calls == 4
        tuned = get_tuned_params(db, "fake_model")
        assert tuned["params"]["x"] == 5
        assert tuned["objective_value"] == 100.0  # 上書きされていない

    def test_first_time_no_existing_row_always_persists(self, db, monkeypatch):
        """plugin_tuned_params に該当行がない初回は、ゲート対象外で常に persist される。"""
        import plugins
        from database import get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        assert get_tuned_params(db, "fake_model") is None

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["persisted"] is True
        assert result["skipped_reason"] is None
        tuned = get_tuned_params(db, "fake_model")
        assert tuned["params"]["x"] == 5

    def test_cli_run_exits_nonzero_on_skip(self, db, monkeypatch):
        """CLI の _run() は品質ゲートでスキップされた場合、非ゼロ終了（SystemExit）する。"""
        import argparse
        import plugins
        from database import upsert_tuned_params, SessionLocal
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 5}, "rank_ic", 100.0, [], 4, "fp")
        monkeypatch.setattr("database.SessionLocal", lambda: db)
        monkeypatch.setattr(db, "close", lambda: None)

        args = argparse.Namespace(
            model="fake_model", strategy="grid", n_iter=50, objective="rank_ic",
            seed=0, persist=True, persist_scores=False,
        )
        with pytest.raises(SystemExit):
            asyncio.run(hs._run(args))
