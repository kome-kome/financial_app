"""実行中マーカー（`batch_activity`）と、それを使う重い分析の関所のテスト。

守るのは4点:
  1. `run_batch` が走っている間だけマーカーがある（失敗するステップでも、例外で抜けても消える）
  2. マーカーが書けなくてもバッチは走り切る（安全柵でバッチを落とさない）
  3. 期限切れのマーカー（OS ごと落ちた残骸）を「実行中」と見なさない
  4. バッチ実行中・同じ分析が実行中なら、heavy は 409（関所はサーバ側にある）

マーカーの置き場所は conftest の `batch_activity_sandbox` が tmp へ向けている。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import batch_activity  # noqa: E402
from collection_jobs import jobs  # noqa: E402
from scripts import batch_common as bc  # noqa: E402

client = TestClient(api.app)

SPEC = bc.BatchSpec(name="テストバッチ", log_prefix="nightly", key_run="k_run",
                    key_success="k_ok", job_label="test", issue_title="t {failed}",
                    headline="h")


class _FakeProc:
    def __init__(self, returncode=0, on_wait=None):
        self.returncode = returncode
        self.on_wait = on_wait
        self.pid = 0

    def wait(self, timeout=None):
        if self.on_wait:
            self.on_wait()
        return self.returncode


def _hooks(tmp_path, **kw):
    return bc.Hooks(log_path=lambda: tmp_path / "b.log",
                    record_footprint=lambda results: None,
                    notify=lambda results, log: None, **kw)


def _marker():
    return batch_activity.marker_path(SPEC.log_prefix)


class TestTheBatchWritesAndClearsTheMarker:
    def test_marker_exists_only_while_running(self, tmp_path, monkeypatch):
        seen = {}

        def look():
            seen["data"] = json.loads(_marker().read_text(encoding="utf-8"))
            seen["running"] = batch_activity.read_activity()["running"]

        monkeypatch.setattr(bc.subprocess, "Popen", lambda argv, **kw: _FakeProc(0, look))
        bc.run_batch(SPEC, [bc.Step("s1", ("x",), why="t")], _hooks(tmp_path), argv=["--no-issue"])

        assert seen["data"]["step"] == "s1"            # ステップ開始でステップ名が入る
        assert seen["data"]["pid"] == os.getpid()
        assert [r["log_prefix"] for r in seen["running"]] == ["nightly"]
        assert not _marker().exists()                  # 終わったら消える
        assert batch_activity.read_activity()["running"] == []

    def test_marker_is_cleared_even_when_a_step_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bc.subprocess, "Popen", lambda argv, **kw: _FakeProc(1))
        failed = bc.run_batch(SPEC, [bc.Step("s1", ("x",), why="t")], _hooks(tmp_path),
                              argv=["--no-issue"])
        assert failed == 1
        assert not _marker().exists()

    def test_marker_is_cleared_when_the_batch_raises(self, tmp_path, monkeypatch):
        """`on_step_done` は例外を握らない（#707）。抜けた後に残ると画面の heavy を止め続ける。"""
        monkeypatch.setattr(bc.subprocess, "Popen", lambda argv, **kw: _FakeProc(0))

        def boom(step, code):
            raise RuntimeError("marker update failed")

        with pytest.raises(RuntimeError):
            bc.run_batch(SPEC, [bc.Step("s1", ("x",), why="t")],
                         _hooks(tmp_path, on_step_done=boom), argv=["--no-issue"])
        assert not _marker().exists()

    def test_dry_run_writes_no_marker(self, tmp_path, monkeypatch, capsys):
        bc.run_batch(SPEC, [bc.Step("s1", ("x",), why="t")], _hooks(tmp_path), argv=["--dry-run"])
        assert not _marker().exists()

    def test_an_unwritable_marker_does_not_stop_the_batch(self, tmp_path, monkeypatch):
        """マーカーは安全柵で、書けなくてもバッチ本体は走り切る（警告を1回だけログへ出す）。"""
        monkeypatch.setattr(bc.subprocess, "Popen", lambda argv, **kw: _FakeProc(0))

        def deny(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr(batch_activity.os, "replace", deny)
        failed = bc.run_batch(SPEC, [bc.Step("s1", ("x",), why="t"), bc.Step("s2", ("x",), why="t")],
                              _hooks(tmp_path), argv=["--no-issue"])
        assert failed == 0
        log = (tmp_path / "b.log").read_text(encoding="utf-8")
        assert log.count("実行中マーカーを書けない") == 1

    def test_the_writer_and_the_reader_share_one_directory(self, monkeypatch):
        """書き手（batch_common）と読み手（API）が別の場所を見ると、関所は黙って開きっぱなしになる。"""
        monkeypatch.undo()                              # conftest の差し替えを外して実体を比べる
        assert batch_activity.LOG_DIR == bc.LOG_DIR


class TestStaleMarkers:
    def _write(self, *, age_sec: float, heartbeat_sec: float = 300.0):
        beat = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        path = _marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "started_at": beat.isoformat(),
                                    "heartbeat_at": beat.isoformat(),
                                    "heartbeat_sec": heartbeat_sec, "step": "scores"}),
                        encoding="utf-8")

    def test_a_recent_heartbeat_is_running(self):
        self._write(age_sec=60)
        act = batch_activity.read_activity()
        assert [r["step"] for r in act["running"]] == ["scores"]
        assert act["stale"] == []

    def test_a_marker_past_its_deadline_is_stale_not_running(self):
        """OS ごと落ちると finally が走らず残る。残骸で heavy を永久に止めない。"""
        self._write(age_sec=2 * 300 + batch_activity.STALE_GRACE_SEC + 60)
        act = batch_activity.read_activity()
        assert act["running"] == []
        assert [r["log_prefix"] for r in act["stale"]] == ["nightly"]

    def test_the_deadline_follows_the_heartbeat_the_writer_promised(self):
        """期限は書き写さず、マーカーに載った heartbeat_sec から導出する。"""
        self._write(age_sec=1000, heartbeat_sec=600)   # 2×600+300=1500 秒以内
        assert batch_activity.read_activity()["running"]

    def test_an_unreadable_marker_is_reported_as_stale(self):
        path = _marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        act = batch_activity.read_activity()
        assert act["running"] == []
        assert act["stale"][0]["note"] == "マーカーを読めない"


@pytest.fixture
def local_db(db, monkeypatch):
    import database
    monkeypatch.setattr(api, "RENDER_LIGHT_MODE", False)
    monkeypatch.setattr(database, "DB_TARGET", "local")
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield db
    api.app.dependency_overrides.clear()


def _pretend_nightly_is_running():
    TestStaleMarkers()._write(age_sec=30)


class TestHeavyRefusesToRunAlongsideABatch:
    def test_heavy_plugin_is_409_while_a_batch_runs(self, local_db):
        _pretend_nightly_is_running()
        r = client.post("/api/plugins/sector_ols/run", json={})
        assert r.status_code == 409
        assert "夜間バッチ" in r.json()["detail"]
        assert "ステップ scores" in r.json()["detail"]

    def test_model_comparison_is_409_while_a_batch_runs(self, local_db):
        _pretend_nightly_is_running()
        assert client.post("/api/backtest/model-comparison").status_code == 409

    def test_light_plugin_is_not_refused(self, local_db):
        """関所は heavy だけ。読むだけの分析まで止めると、バッチ中に画面が使えなくなる。"""
        _pretend_nightly_is_running()
        assert client.post("/api/plugins/net_cash_analysis/run", json={}).status_code != 409

    def test_a_stale_marker_does_not_refuse(self, local_db):
        TestStaleMarkers()._write(age_sec=10_000)
        assert client.post("/api/plugins/sector_ols/run", json={}).status_code != 409

    def test_the_same_heavy_already_running_is_409(self, local_db):
        st = jobs.state("plugin:sector_ols")
        st.running = True
        try:
            r = client.post("/api/plugins/sector_ols/run", json={})
        finally:
            st.running = False
        assert r.status_code == 409
        assert "既に実行中" in r.json()["detail"]


class TestActivityAndPreflight:
    def test_activity_lists_the_running_batch_in_jst(self, local_db):
        _pretend_nightly_is_running()
        body = client.get("/api/batch/activity").json()
        assert [r["label"] for r in body["running"]] == ["夜間バッチ"]
        assert body["running"][0]["started_at_jst"].endswith("JST")
        assert "夜間バッチ" in body["message"]

    def test_activity_is_empty_when_nothing_runs(self, local_db):
        body = client.get("/api/batch/activity").json()
        assert body["running"] == [] and "message" not in body

    def test_preflight_shows_what_will_be_replaced(self, local_db):
        body = client.get("/api/plugins/sector_ols/preflight").json()
        assert body["writes"] == ["regression_results"]
        assert body["blocked_reason"] is None
        assert body["last_run_min"] is None

    def test_preflight_reports_the_block_reason(self, local_db):
        _pretend_nightly_is_running()
        body = client.get("/api/plugins/macro_enet/preflight").json()
        assert "夜間バッチ" in body["blocked_reason"]

    def test_preflight_covers_the_special_entry(self, local_db):
        body = client.get("/api/plugins/model_comparison/preflight").json()
        assert body["writes"] == []

    def test_preflight_rejects_light_analyses(self, local_db):
        assert client.get("/api/plugins/net_cash_analysis/preflight").status_code == 404

    def test_preflight_reads_the_last_run(self, local_db):
        from database import upsert_setting
        from routers.analysis import LAST_RUN_KEY_PREFIX
        upsert_setting(local_db, LAST_RUN_KEY_PREFIX + "macro_enet", "4.4")
        assert client.get("/api/plugins/macro_enet/preflight").json()["last_run_min"] == 4.4


class TestHeavyDeclaresWrites:
    """heavy を画面から回すと、夜間・月次が書いた結果を黙って置き換える。押す前に何が変わるかを
    見せるため、heavy は `writes` を宣言必須にする（書かないなら `()` を明示）。"""

    def test_every_heavy_plugin_declares_writes(self):
        from plugins import list_plugins
        missing = [p.name for p in list_plugins() if p.heavy and p.writes is None]
        assert not missing, f"writes を宣言していない heavy: {missing}"

    def test_every_heavy_special_entry_declares_writes(self):
        from routers.analysis import SPECIAL_ANALYSES
        missing = [e["name"] for e in SPECIAL_ANALYSES if e.get("heavy") and "writes" not in e]
        assert not missing, f"writes を宣言していない heavy の特例: {missing}"
