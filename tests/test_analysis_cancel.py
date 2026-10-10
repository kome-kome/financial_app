"""画面から回した heavy の取消（協調型・#849）の単体テスト。

縛るのは4つ:
  1. **取消は進捗の区切りで止まり、保存を始めた後は受け付けない**（`progress.Cancellation` /
     `persisting`）。sector_ols は業種ごとに commit するので、途中で止めると
     `regression_results` に新旧が混ざる。
  2. **バッチ経路（取消状態が無い）では何も変わらない**。
  3. **取消を握る `except Exception` が無い**——`model_comparison` のモデル単位の except で
     握ると、止まらずに次のモデルへ進む。
  4. **保存先（`writes`）を持つ heavy は保存を `progress.persisting(` で包む**。包み忘れても
     失敗としては現れない（保存の途中で止まりうるだけ）ので、表と実体を照合する。
"""
import asyncio
import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402,F401  （routers.analysis を単独 import すると循環になる）
import model_comparison  # noqa: E402
import plugins as plugin_registry  # noqa: E402
import routers.analysis as analysis  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from plugins import progress  # noqa: E402
from plugins.sector_ols import SectorOLSPlugin  # noqa: E402


def _collect():
    """sink が受けたステップ名を貯める。"""
    steps: list[str] = []
    return steps, (lambda step, current, total: steps.append(step))


class TestCancellation:
    def test_emit_stops_after_a_request(self):
        steps, sink = _collect()
        cancel = progress.Cancellation()
        with progress.progress_sink(sink, cancel=cancel):
            progress.emit("前段")
            assert cancel.request() is True
            with pytest.raises(progress.AnalysisCancelled):
                progress.emit("後段")
        # 止まった場所が画面のログの最後に残る（送ってから見る）
        assert steps == ["前段", "後段"]

    def test_thinned_points_do_not_check(self):
        """間引きで sink まで届かない点では止まらない（止まれるのは送った点だけ）。"""
        steps, sink = _collect()
        cancel = progress.Cancellation()
        with progress.progress_sink(sink, cancel=cancel):
            cancel.request()
            progress.emit("ループ", 3, 10, every=5)      # 間引かれる＝見ない
            with pytest.raises(progress.AnalysisCancelled):
                progress.emit("ループ", 5, 10, every=5)

    def test_saving_refuses_requests_and_runs_to_the_end(self):
        steps, sink = _collect()
        cancel = progress.Cancellation()
        saved = []
        with progress.progress_sink(sink, cancel=cancel):
            with progress.persisting():
                assert cancel.request() is False         # 保存中は受け付けない
                progress.emit("業種 A を保存")             # 保存中の進捗では止まらない
                saved.append("A")
            progress.emit("後処理")                       # 断った取消は後から効かない
        assert saved == ["A"]
        assert cancel.requested is False
        assert steps[0] == progress.PERSIST_STEP

    def test_a_request_before_saving_stops_at_the_entrance(self):
        steps, sink = _collect()
        cancel = progress.Cancellation()
        body_ran = []
        with progress.progress_sink(sink, cancel=cancel):
            cancel.request()
            with pytest.raises(progress.AnalysisCancelled):
                with progress.persisting():
                    body_ran.append(True)
        assert body_ran == []                             # 保存は1行も走らない
        assert cancel.saving == 0

    def test_saving_depth_is_restored_when_the_body_raises(self):
        cancel = progress.Cancellation()
        with progress.progress_sink(lambda *a: None, cancel=cancel):
            with pytest.raises(RuntimeError):
                with progress.persisting():
                    raise RuntimeError("保存に失敗")
        assert cancel.saving == 0
        assert cancel.request() is True                   # 保存を抜けたら再び受け付ける

    def test_batch_path_is_untouched(self):
        """sink も取消状態も無い経路（バッチ・/api/recommend）では emit も persisting も素通り。"""
        ran = []
        progress.emit("バッチ")
        with progress.persisting():
            ran.append(True)
        assert ran == [True]

    def test_sink_without_cancellation_never_stops(self):
        steps, sink = _collect()
        with progress.progress_sink(sink):
            progress.emit("a")
            with progress.persisting():
                progress.emit("b")
        assert steps == ["a", progress.PERSIST_STEP, "b"]


def _fake_plugin(execute, name="fake_cancel"):
    class _P:
        label = "偽の重い分析"
        heavy = True
        depends_on: list = []

        def params_schema(self):
            return {}

    p = _P()
    p.name = name
    p.execute = execute
    return p


class TestCancelThroughTheRunner:
    def test_cancel_stops_execute_and_leaves_the_reason_last(self):
        job = analysis._progress_job("fake_cancel")

        def execute(params, db):
            progress.emit("構築", 1, 3)
            # 画面の取消ボタン（cancel_plugin）が実行中に旗を立てたのと同じ
            assert analysis._cancellations[job].request() is True
            progress.emit("構築", 2, 3)
            raise AssertionError("取消の後も進んだ")

        p = _fake_plugin(execute)
        with pytest.raises(progress.AnalysisCancelled):
            asyncio.run(analysis._execute_with_progress(p, p.name, {}, None))

        st = analysis.jobs.state(job)
        assert st.running is False
        assert st.log[-1] == analysis.CANCELLED_MESSAGE
        assert job not in analysis._cancellations          # 実行の間だけ置く

    def test_a_finished_run_leaves_no_cancellation_behind(self):
        p = _fake_plugin(lambda params, db: {"ok": True})
        assert asyncio.run(analysis._execute_with_progress(p, p.name, {}, None)) == {"ok": True}
        assert analysis._progress_job(p.name) not in analysis._cancellations


class TestCancelEndpoint:
    NAME = "sector_ols"

    def _run(self):
        return asyncio.run(analysis.cancel_plugin(self.NAME))

    def test_unknown_name_is_404(self):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(analysis.cancel_plugin("no_such_analysis"))
        assert ei.value.status_code == 404

    def test_idle_is_not_running(self):
        body = self._run()
        assert body["accepted"] is False and body["running"] is False

    def _running(self, cancel):
        job = analysis._progress_job(self.NAME)
        st = analysis.jobs.state(job)
        st.reset_for_run()
        analysis._cancellations[job] = cancel
        return job, st

    def _stop(self, job, st):
        analysis._cancellations.pop(job, None)
        st.running = False

    def test_running_is_accepted(self):
        cancel = progress.Cancellation()
        job, st = self._running(cancel)
        try:
            body = self._run()
        finally:
            self._stop(job, st)
        assert body["accepted"] is True and cancel.requested is True
        assert "受け付けました" in st.log[-1]

    def test_saving_is_refused_and_says_why(self):
        cancel = progress.Cancellation()
        cancel.saving = 1
        job, st = self._running(cancel)
        try:
            body = self._run()
        finally:
            self._stop(job, st)
        assert body["accepted"] is False and body["saving"] is True
        assert cancel.requested is False
        assert "保存を始めた後" in body["message"]

    def test_the_route_is_registered_as_a_non_writing_route(self):
        assert ("POST", "/api/plugins/{plugin_name}/cancel") in api.WRITE_ROUTE_EXEMPTIONS


@pytest.fixture
def local_db(db, monkeypatch):
    import database
    monkeypatch.setattr(api, "RENDER_LIGHT_MODE", False)
    monkeypatch.setattr(database, "DB_TARGET", "local")
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield db
    api.app.dependency_overrides.clear()


class TestCancelledRunIsA409:
    """取消は失敗ではない＝500 に化けさせない。画面は detail の書き出しで取消と見分ける。"""

    def test_plugin_run(self, local_db, monkeypatch):
        async def cancelled(plugin, params, db):
            raise progress.AnalysisCancelled("取消しました")

        monkeypatch.setattr(plugin_registry, "execute_plugin", cancelled)
        r = TestClient(api.app).post("/api/plugins/sector_ols/run", json={})
        assert r.status_code == 409
        assert r.json()["detail"] == analysis.CANCELLED_MESSAGE

    def test_model_comparison(self, local_db, monkeypatch):
        async def cancelled(db, render_light_mode=False, only_models=None):
            raise progress.AnalysisCancelled("取消しました")

        monkeypatch.setattr(model_comparison, "run_comparison", cancelled)
        r = TestClient(api.app).post("/api/backtest/model-comparison")
        assert r.status_code == 409
        assert r.json()["detail"].startswith("取消しました")


class TestModelComparisonStopsOnCancel:
    def test_does_not_move_on_to_the_next_model(self, monkeypatch):
        calls = []

        async def execute_plugin(plugin, params, db):
            calls.append(plugin.name)
            raise progress.AnalysisCancelled("取消しました")

        monkeypatch.setattr(plugin_registry, "get_plugin",
                            lambda name: SimpleNamespace(name=name, label=name, heavy=False))
        monkeypatch.setattr(plugin_registry, "execute_plugin", execute_plugin)
        db = MagicMock()
        with pytest.raises(progress.AnalysisCancelled):
            asyncio.run(model_comparison.run_comparison(db))
        assert calls == [model_comparison.COMPARISON_MODELS[0][0]]
        db.rollback.assert_not_called()                  # 失敗扱いの後始末にも入らない


class TestSectorOlsStopsOnlyBeforeSaving:
    """業種ごとに commit する sector_ols は、最初の業種から先は止めない。"""

    PARAMS = {"features": ["x"], "min_samples": 1, "regularization": "ols", "year": None,
              "shrink_threshold": 0, "zero_fill_no_dividend": False}

    def _plugin(self, persisted, on_persist=None):
        p = SectorOLSPlugin()

        def sample(ec):   # (行ベクトル, 目的変数, レコード)。削除の基準のキー（#905）を引ける形にする
            return ([1.0], 1.0, SimpleNamespace(edinet_code=ec, year=2023, period_end=None))

        sectors = {"A業種": [sample("a")], "B業種": [sample("b")], "C業種": [sample("c")]}
        p._load_records = lambda db, year, features, **kw: []
        p._prepare_fit = lambda records, params: SimpleNamespace(
            features=["x"], dropped_features=[], dropped_by_sector={}, by_sector=sectors,
            n_excluded_missing=0, excluded_by_feature=[])
        p._fit_sector = lambda prep, sector, samples, params: SimpleNamespace(
            samples=samples, all_yhat=[1.0], result={}, y_sd=1.0, X_norm=[], y_normed=[],
            features=["x"], X_win_cols=[])

        def persist(db, sector, samples, all_yhat, regularization):
            persisted.append(sector)
            if on_persist:
                on_persist()
            return []

        p._persist_and_rank = persist

        def prune(db, records, fitted_keys):   # 保存を差し替えるなら削除も差し替える（#905）
            persisted.append(("prune", frozenset(fitted_keys)))
            return 0

        p._prune_unwritten = prune
        p._build_stat_entry = lambda sector, *a: {"r2": 0.5, "n": 10, "industry": sector}
        return p

    def test_a_request_after_the_first_commit_runs_to_the_end(self):
        persisted, answers = [], []
        cancel = progress.Cancellation()
        p = self._plugin(persisted, on_persist=lambda: answers.append(cancel.request()))
        with progress.progress_sink(lambda *a: None, cancel=cancel):
            result = p.execute(dict(self.PARAMS), db=None)
        # 古い行の削除（#905）は全業種の保存の後に1回、保存の区間の中で走る＝取消で途中に残らない
        assert persisted == ["A業種", "B業種", "C業種",
                             ("prune", frozenset({("a", 2023, None), ("b", 2023, None),
                                                  ("c", 2023, None)}))]
        assert answers == [False, False, False]
        assert result["n_sectors"] == 3

    def test_a_request_during_loading_stops_before_any_commit(self):
        persisted = []
        cancel = progress.Cancellation()
        p = self._plugin(persisted)
        load = p._load_records

        def load_then_click(*a, **kw):
            cancel.request()                              # ロード中に取消が押された
            return load(*a, **kw)

        p._load_records = load_then_click
        with progress.progress_sink(lambda *a: None, cancel=cancel):
            with pytest.raises(progress.AnalysisCancelled):
                p.execute(dict(self.PARAMS), db=None)
        assert persisted == []


def _sources_of(plugin) -> str:
    """MRO を辿って定義モジュールのソースを連結する（継承で execute を借りる M-5 向け）。"""
    texts = []
    for klass in type(plugin).__mro__:
        module = sys.modules.get(klass.__module__)
        if module is not None and getattr(module, "__file__", None):
            texts.append(inspect.getsource(module))
    return "\n".join(texts)


class TestEverySavingHeavyGuardsItsSave:
    def test_plugins_with_writes_use_persisting(self):
        savers = [p for p in plugin_registry.list_plugins()
                  if getattr(p, "heavy", False) and getattr(p, "writes", None)]
        assert savers, "保存先を持つ heavy が1つも無い（前提が変わった）"
        missing = [p.name for p in savers if "progress.persisting(" not in _sources_of(p)]
        assert not missing, (
            f"保存を progress.persisting() で包んでいない heavy: {missing}。"
            "包まないと、画面の取消が保存の途中で効いて新旧が混ざりうる（#849）"
        )


class TestTheScreenHasACancelButton:
    def test_progress_box_gets_a_cancel_button(self):
        js = (analysis.api.BASE_DIR / "static" / "js" / "analysis.js").read_text(encoding="utf-8")
        start = js.index("function _startPluginProgress(")
        body = js[start:js.index("\nfunction ", start + 1)]
        assert "_cancelButtonFor(" in body, "進捗ボックスに取消ボタンを置かなくなっている"
        assert "/cancel`" in js and "function cancelHeavyRun(" in js
