"""売買の記録の API（#1222）。値はすべて作り物。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import database
from database import TradeLedgerEntry

client = TestClient(api.app)

BUY = {"side": "buy", "sec_code": "1111", "trade_date": "2026-01-05", "price": 1000, "quantity": 100}


@pytest.fixture(autouse=True)
def _local(db, monkeypatch):
    # 書き込みガード（#733）の判定を開発機の .env に依存させない。DB は必ず in-memory へ向ける。
    monkeypatch.setattr(api, "RENDER_LIGHT_MODE", False)
    monkeypatch.setattr(database, "DB_TARGET", "local")
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield
    api.app.dependency_overrides.clear()


def _count(db):
    return db.query(TradeLedgerEntry).count()


class TestWrite:
    def test_add_then_list_and_summary(self, db):
        r = client.post("/api/portfolio/ledger", json=BUY)
        assert r.status_code == 200, r.text
        entries = client.get("/api/portfolio/ledger").json()["entries"]
        assert [(e["id"], e["side"], e["voided"]) for e in entries] == [(r.json()["id"], "buy", False)]
        s = client.get("/api/portfolio/summary").json()
        assert [(h["sec_code"], h["quantity"]) for h in s["holdings"]] == [("1111", 100.0)]

    @pytest.mark.parametrize("patch", [
        {"price": 0}, {"sec_code": "12345"}, {"trade_date": "2999-01-01"}, {"side": "void"},
    ])
    def test_bad_input_is_400_and_writes_nothing(self, db, patch):
        r = client.post("/api/portfolio/ledger", json=dict(BUY, **patch))
        assert r.status_code == 400, r.text
        assert _count(db) == 0

    def test_oversell_is_400(self, db):
        client.post("/api/portfolio/ledger", json=BUY)
        r = client.post("/api/portfolio/ledger",
                        json=dict(BUY, side="sell", trade_date="2026-02-02", quantity=101))
        assert r.status_code == 400 and "超えています" in r.json()["detail"]
        assert _count(db) == 1

    def test_void_appends_and_marks_the_target(self, db):
        target = client.post("/api/portfolio/ledger", json=BUY).json()["id"]
        assert client.post(f"/api/portfolio/ledger/{target}/void").status_code == 200
        assert _count(db) == 2
        entries = {e["id"]: e for e in client.get("/api/portfolio/ledger").json()["entries"]}
        assert entries[target]["voided"] is True
        assert client.get("/api/portfolio/summary").json()["holdings"] == []
        # 二重の取消・存在しない行は 400
        assert client.post(f"/api/portfolio/ledger/{target}/void").status_code == 400
        assert client.post("/api/portfolio/ledger/999/void").status_code == 400
        assert _count(db) == 2


class TestReadOnlyGuard:
    """閲覧専用の環境（Render・接続先が prod）では書けない（#733）。読む口は通る。"""

    @pytest.mark.parametrize("how", ["light", "prod"])
    def test_writes_are_403_and_nothing_is_written(self, db, monkeypatch, how):
        if how == "light":
            monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        else:
            monkeypatch.setattr(database, "DB_TARGET", "prod")
        assert client.post("/api/portfolio/ledger", json=BUY).status_code == 403
        assert client.post("/api/portfolio/ledger/1/void").status_code == 403
        assert _count(db) == 0

    def test_reads_still_pass(self, monkeypatch):
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        assert client.get("/api/portfolio/ledger").json() == {"entries": []}
        assert client.get("/api/portfolio/summary").status_code == 200
