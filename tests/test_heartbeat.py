"""ブラウザ連動自動停止（/heartbeat・_shutdown_due・any_running）のテスト。

watchdog スレッド自体（os._exit）は起動しない。判定関数 _shutdown_due と
エンドポイント・ジョブ実行中ガードのロジックのみ検証する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
from collection_jobs import jobs  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


def test_heartbeat_endpoint_updates_timestamp():
    api._hb["last"] = None
    r = client.post("/heartbeat")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["auto_shutdown"] == api.AUTO_SHUTDOWN
    assert api._hb["last"] is not None


def test_shutdown_due_startup_grace():
    api._hb["boot"] = 1000.0
    api._hb["last"] = None
    assert not api._shutdown_due(1000.0 + api.STARTUP_GRACE - 1)
    assert api._shutdown_due(1000.0 + api.STARTUP_GRACE + 1)


def test_shutdown_due_heartbeat_timeout():
    api._hb["last"] = 2000.0
    assert not api._shutdown_due(2000.0 + api.HEARTBEAT_TIMEOUT - 1)
    assert api._shutdown_due(2000.0 + api.HEARTBEAT_TIMEOUT + 1)


def test_shutdown_deferred_while_job_running():
    api._hb["last"] = 0.0
    st = jobs.state("_test_hb")
    st.running = True
    try:
        assert not api._shutdown_due(1e9)
    finally:
        st.running = False
    # ジョブ終了後は停止対象に戻る
    assert api._shutdown_due(1e9)


def test_any_running_reflects_registry_state():
    assert jobs.any_running() is False
    st = jobs.state("_test_hb2")
    st.running = True
    try:
        assert jobs.any_running() is True
    finally:
        st.running = False
    assert jobs.any_running() is False
