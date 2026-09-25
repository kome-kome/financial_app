"""api.py 収集エンドポイントのテスト。

/api/collect/start の3パス（400/403/200）と CollectionLog 作成を検証する。
BackgroundTask が実際に走るのを防ぐため _run_collection_bg をノープロコルーチンに差し替える。
閲覧専用の環境での書き込みガード（#733）は TestReadOnlyGuard。
"""
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import database
from database import Base, CollectionLog
from fastapi.routing import APIRoute
from routers import collect as collect_router
from starlette.background import BackgroundTasks

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
def _cleanup(monkeypatch):
    # 書き込みガード（#733）の判定を開発機の .env に依存させない。
    monkeypatch.setattr(api, "RENDER_LIGHT_MODE", False)
    monkeypatch.setattr(database, "DB_TARGET", "local")
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
        # ガードが壊れたときに本物の全件収集が走らないように。
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start",
                        json={"years_back": 1, "skip_existing": False})
        assert r.status_code == 403

    def test_render_light_mode_blocks_incremental(self, db_session, monkeypatch):
        # #733 以前は差分なら通していた＝Supabase の断面だけが前進していた。
        api.app.dependency_overrides[api.get_db] = lambda: db_session
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        monkeypatch.setattr(api, "_run_collection_bg", _noop_bg)
        r = client.post("/api/collect/start",
                        json={"years_back": 1, "skip_existing": True})
        assert r.status_code == 403
        assert db_session.query(CollectionLog).count() == 0

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


class TestMarketDataUpdate:
    """/api/collect/market-data は株価テーブル由来の一括反映を呼ぶ（#428）。

    旧実装は stooq へ全社ぶん逐次リクエストしていたが、stooq はクラウド IP から
    ブロックされ実質動作しない経路だった。収集ワークフローは既に
    update_market_data_from_history へ一本化されており、GUI もそこへ揃える。
    """

    def _run_job_body(self, monkeypatch):
        """jobs.start に渡された body を捕まえて、その場で実行し進捗を返す。"""
        captured = {}

        def _fake_start(name, background_tasks, body, **kwargs):
            captured["body"] = body

        monkeypatch.setattr(api.jobs, "start", _fake_start)
        return captured

    def test_calls_history_based_update(self, monkeypatch):
        import asyncio
        import routers.collect as collect_router

        calls = []
        monkeypatch.setattr(collect_router, "update_market_data_from_history",
                            lambda db: (calls.append(db) or 42))
        captured = self._run_job_body(monkeypatch)

        r = client.post("/api/collect/market-data", json={"force": False})
        assert r.status_code == 200

        progress = []
        asyncio.run(captured["body"](
            lambda c, t, m: progress.append(m), lambda: False))
        assert len(calls) == 1                     # DB 由来の一括更新が1回
        assert "42社" in progress[-1]

    def test_stooq_path_is_gone(self):
        """stooq へ現在株価を取りに行く旧関数が復活していないこと。"""
        import collector
        import collector_prices
        assert not hasattr(collector_prices, "update_market_data")
        assert not hasattr(collector_prices, "fetch_stock_price_stooq")
        assert not hasattr(collector, "update_market_data")

    @pytest.mark.parametrize("name", [
        "collect_stock_price_history", "fetch_stock_history_stooq", "fetch_stooq_history",
        "_fetch_stooq_ohlcv", "_parse_stooq_csv",
    ])
    def test_stooq_fetchers_are_gone(self, name):
        """stooq の個別銘柄経路とマクロのフォールバックが復活していないこと（#736）。

        stooq はどの実行環境からも CSV が取れず、ボット検証の HTML を 0 行として返して
        「取れなかった」を「データが無かった」に化けさせていた。"""
        import collector
        import collector_prices
        assert not hasattr(collector_prices, name)
        assert not hasattr(collector, name)

    @pytest.mark.parametrize("method,path", [
        ("POST", "/api/collect/history/start"),
        ("POST", "/api/collect/history/stop"),
        ("GET", "/api/collect/history/status"),
        ("GET", "/api/collect/history/stream"),
    ])
    def test_stooq_history_routes_are_gone(self, method, path):
        """株価履歴収集（stooq）の API は撤去済み。`/history/coverage`（DB 読み取り）だけ残す。"""
        routes = {(m, r.path) for r in api.app.routes if isinstance(r, APIRoute)
                  for m in r.methods}
        assert (method, path) not in routes
        assert ("GET", "/api/collect/history/coverage") in routes

    def test_legacy_max_companies_is_accepted_and_ignored(self, monkeypatch):
        """旧クライアントが max_companies を送っても 422 にしない（受理して無視）。"""
        self._run_job_body(monkeypatch)
        r = client.post("/api/collect/market-data",
                        json={"max_companies": 100, "force": False})
        assert r.status_code == 200


# ── 閲覧専用の環境での書き込みガード（#733）─────────────────────────────────

def _write_routes() -> list[tuple[str, str]]:
    """収集ルーターの GET 以外のルートを実体から列挙する（足したルートは自動で検査対象に入る）。"""
    out = []
    for route in collect_router.router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
            out.append((method, route.path))
    return out


_WRITE_ROUTES = _write_routes()

# Issue #733 の本文で「Render でも通る」と列挙された8本。列挙の空振り検出に使う。
_ISSUE_733_ROUTES = {
    "/api/collect/start",
    "/api/collect/smart-start",
    "/api/scheduler/run-now",
    "/api/collect/refresh/{edinet_code}",
    "/api/collect/market-data",
    "/api/collect/reparse/start",
    "/api/collect/industry",
    "/api/collect/macro/start",
}


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "E00001", path)


@pytest.fixture
def no_side_effects(db_session, monkeypatch):
    """ガードが壊れていても本物の DB・外部 API・バックグラウンド処理へ届かないようにする。"""
    api.app.dependency_overrides[api.get_db] = lambda: db_session
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, *a, **k: None)
    monkeypatch.setattr(api.jobs, "start", lambda *a, **k: None)

    async def _no_jpx(*args, **kwargs):
        return 0, 0
    monkeypatch.setattr(collect_router, "update_industry_from_jpx", _no_jpx)
    return db_session


class TestReadOnlyGuard:
    """Render（閲覧専用）では収集ルーターの GET 以外が一律 403（#733）。

    Render は Supabase の断面を読むだけの窓で、書き込みはどちらの DB でも成功するので
    黙って断面だけが前進する。ルーター単位の依存で止めているので、ここではルートを
    実体から列挙して全部叩く＝ガードの外に置かれたルートは失敗として現れる。
    """

    def test_enumeration_covers_the_routes_in_the_issue(self):
        assert _ISSUE_733_ROUTES <= {path for _, path in _WRITE_ROUTES}

    @pytest.mark.parametrize("method, path", _WRITE_ROUTES)
    def test_blocked_in_light_mode(self, no_side_effects, monkeypatch, method, path):
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        r = client.request(method, _url(path), json={})
        assert r.status_code == 403, r.text

    @pytest.mark.parametrize("method, path", _WRITE_ROUTES)
    def test_blocked_when_target_is_prod(self, no_side_effects, monkeypatch, method, path):
        # render.yaml の旗が反映されていない Render（`RENDER` 検知で prod）も止める。
        monkeypatch.setattr(database, "DB_TARGET", "prod")
        r = client.request(method, _url(path), json={})
        assert r.status_code == 403, r.text

    def test_nothing_is_written_when_blocked(self, no_side_effects, monkeypatch):
        db = no_side_effects
        db.add(CollectionLog(job_type="incremental", status="running")); db.commit()
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        client.post("/api/scheduler/run-now")
        client.post("/api/collect/reset-stuck")
        db.expire_all()
        logs = db.query(CollectionLog).all()
        assert [(l.job_type, l.status) for l in logs] == [("incremental", "running")]

    def test_invalid_body_gets_403_not_422(self, no_side_effects, monkeypatch):
        """ルーター依存は body の検証より先に解かれる。"""
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        r = client.post("/api/collect/start", json={"years_back": 99})
        assert r.status_code == 403

    @pytest.mark.parametrize("path", [
        "/api/collect/status",
        "/api/collect/edinet-coverage",
        "/api/collect/market-data/status",
        "/api/collect/history/coverage",
    ])
    def test_reads_still_pass(self, no_side_effects, monkeypatch, path):
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        assert client.get(path).status_code == 200

    def test_writes_pass_when_not_read_only(self, no_side_effects):
        # ガードが常時 403 に倒れていないこと（autouse で light=False・local）。
        r = client.post("/api/scheduler/run-now")
        assert r.status_code == 200


class TestWriteRouteRegistry:
    """収集ルーターの外にある GET 以外のルートは api.WRITE_ROUTE_EXEMPTIONS に登録する（#733）。

    ガードはルーター依存なので収集ルーターの外には掛からない。別のルーターへ書き込み系を
    足しても**テストもアプリも通る**＝失敗として現れないので、実体と登録表を照合する。
    """

    @staticmethod
    def _app_write_routes() -> set[tuple[str, str]]:
        guarded = {r.endpoint for r in collect_router.router.routes if isinstance(r, APIRoute)}
        out = set()
        for route in api.app.routes:
            if not isinstance(route, APIRoute) or route.endpoint in guarded:
                continue
            for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
                out.add((method, route.path))
        return out

    def test_every_unguarded_write_route_is_registered(self):
        missing = self._app_write_routes() - set(api.WRITE_ROUTE_EXEMPTIONS)
        assert not missing, (
            f"収集ルーターの外に GET 以外のルートがある: {sorted(missing)}。断面へ書くなら "
            "routers/collect.py へ置き、書かないなら api.WRITE_ROUTE_EXEMPTIONS へ理由付きで足す")

    def test_no_stale_entries(self):
        stale = set(api.WRITE_ROUTE_EXEMPTIONS) - self._app_write_routes()
        assert not stale, f"実体の無い登録: {sorted(stale)}"

    def test_every_entry_has_a_reason(self):
        for key, reason in api.WRITE_ROUTE_EXEMPTIONS.items():
            assert reason.startswith("exempt:") and reason[len("exempt:"):].strip(), key

    def test_enumeration_is_not_empty(self):
        # 列挙が壊れて0件になると、上の2本が静かに通る。
        assert len(self._app_write_routes()) >= 5
