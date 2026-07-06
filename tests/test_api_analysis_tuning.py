"""routers/analysis.py のハイパーパラメータ探索GUI統合エンドポイント（Issue #278）。

/api/plugins/{plugin_name}/tune-start・tune-stop・tune-status・tune-stream を検証する。
BackgroundTask が実際に重い探索を走らせないよう hyperparameter_search.run_search を
ノープロ関数に差し替え、SessionLocal も in-memory SQLite に差し替える
（本番DBへ誤接続しないため・collector_prices系テストと同じ思想）。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import hyperparameter_search
import routers.analysis as analysis_router
from database import Base

client = TestClient(api.app)


@pytest.fixture
def sqlite_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


async def _noop_run_search(*args, **kwargs):
    return {"best_params": {"x": 1}, "best_score": 1.0, "objective": "rank_ic",
            "leaderboard": [], "config": {"cancelled": False}, "persisted": True}


def _job_name(plugin_name: str) -> str:
    return f"tuning_{plugin_name}"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for name in ("macro_gbdt", "macro_risk_return", "macro_dlm"):
        st = api.jobs.state(_job_name(name))
        st.running = False
        st.cancel_requested = False


class TestTuneStart:
    def test_unknown_plugin_returns_404(self):
        r = client.post("/api/plugins/not_a_model/tune-start", json={})
        assert r.status_code == 404

    def test_render_light_mode_blocks(self, monkeypatch):
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        r = client.post("/api/plugins/macro_gbdt/tune-start", json={})
        assert r.status_code == 403

    def test_invalid_strategy_returns_400(self):
        r = client.post("/api/plugins/macro_gbdt/tune-start", json={"strategy": "bogus"})
        assert r.status_code == 400

    def test_invalid_objective_returns_400(self):
        r = client.post("/api/plugins/macro_gbdt/tune-start", json={"objective": "bogus"})
        assert r.status_code == 400

    def test_success_starts_job(self, monkeypatch, sqlite_session_factory):
        monkeypatch.setattr(hyperparameter_search, "run_search", _noop_run_search)
        monkeypatch.setattr(analysis_router, "SessionLocal", sqlite_session_factory)
        r = client.post("/api/plugins/macro_gbdt/tune-start",
                        json={"strategy": "random", "n_iter": 5})
        assert r.status_code == 200

    def test_already_running_returns_400(self, monkeypatch, sqlite_session_factory):
        monkeypatch.setattr(hyperparameter_search, "run_search", _noop_run_search)
        monkeypatch.setattr(analysis_router, "SessionLocal", sqlite_session_factory)
        api.jobs.state(_job_name("macro_gbdt")).running = True
        r = client.post("/api/plugins/macro_gbdt/tune-start", json={})
        assert r.status_code == 400


class TestTuneStop:
    def test_not_running_returns_400(self):
        r = client.post("/api/plugins/macro_gbdt/tune-stop")
        assert r.status_code == 400

    def test_running_requests_cancel(self):
        api.jobs.state(_job_name("macro_gbdt")).running = True
        r = client.post("/api/plugins/macro_gbdt/tune-stop")
        assert r.status_code == 200
        assert api.jobs.state(_job_name("macro_gbdt")).cancel_requested is True


class TestTuneStatus:
    def test_returns_snapshot_shape(self):
        r = client.get("/api/plugins/macro_dlm/tune-status")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"running", "progress", "total", "recent_logs"}
