"""api.py 収集エンドポイントのテスト。

/api/collect/start の3パス（400/403/200）と CollectionLog 作成を検証する。
BackgroundTask が実際に走るのを防ぐため _run_collection_bg をノープロコルーチンに差し替える。
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
from database import Base, CollectionLog

client = TestClient(api.app)


async def _noop_bg(*args, **kwargs):
    """background_tasks に渡される _run_collection_bg の代替（何もしない）。"""


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    api.app.dependency_overrides.clear()
    api.jobs.state(api._COLLECTION).running = False
    api.jobs.state(api._COLLECTION).cancel_requested = False


class TestStartCollection:
    def test_already_running_returns_400(self, db_session):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        api.jobs.state(api._COLLECTION).running = True
        r = client.post("/api/collect/start", json={"years_back": 1})
        assert r.status_code == 400

    def test_render_light_mode_blocks_full_collection(self, db_session, monkeypatch):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        r = client.post("/api/collect/start",
                        json={"years_back": 1, "skip_existing": False})
        assert r.status_code == 403

    def test_render_light_mode_allows_incremental(self, db_session, monkeypatch):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start",
                        json={"years_back": 1, "skip_existing": True})
        assert r.status_code == 200

    def test_success_returns_log_id(self, db_session, monkeypatch):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start", json={"years_back": 1})
        assert r.status_code == 200
        body = r.json()
        assert "log_id" in body
        assert isinstance(body["log_id"], int)

    def test_creates_collection_log_with_running_status(self, db_session, monkeypatch):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start", json={"years_back": 1})
        log_id = r.json()["log_id"]
        log = db_session.get(CollectionLog, log_id)
        assert log is not None
        assert log.status == "running"

    def test_full_job_type_by_default(self, db_session, monkeypatch):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start", json={"years_back": 1})
        log = db_session.get(CollectionLog, r.json()["log_id"])
        assert log.job_type == "full"

    def test_incremental_job_type_when_skip_existing(self, db_session, monkeypatch):
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start",
                        json={"years_back": 1, "skip_existing": True})
        log = db_session.get(CollectionLog, r.json()["log_id"])
        assert log.job_type == "incremental"
