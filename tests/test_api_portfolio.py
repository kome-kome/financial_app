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


class TestSellCandidates:
    """記録の保有を売り候補ランキングへ渡す口（朝の画面用・#1228）。判定の規則はプラグインのまま。"""

    @pytest.fixture
    def captured(self, monkeypatch):
        import plugins as plugin_registry
        calls = []

        async def fake_execute(plugin, raw, db):
            calls.append((plugin.name, raw))
            return {"count": 2, "n_sell": 0, "n_hold": 2, "results": []}

        monkeypatch.setattr(plugin_registry, "execute_plugin", fake_execute)
        return calls

    def test_no_holdings_does_not_run_the_plugin(self, captured):
        r = client.get("/api/portfolio/sell-candidates")
        assert r.status_code == 200
        assert r.json() == {"n_holdings": 0, "ranking": None, "reason": None}
        assert captured == []

    def test_holdings_come_from_the_ledger(self, captured):
        client.post("/api/portfolio/ledger", json=BUY)
        client.post("/api/portfolio/ledger", json=dict(BUY, sec_code="2222", price=2000.5, quantity=10))
        sold = dict(BUY, sec_code="3333")
        client.post("/api/portfolio/ledger", json=sold)
        client.post("/api/portfolio/ledger", json=dict(sold, side="sell", trade_date="2026-02-02"))
        d = client.get("/api/portfolio/sell-candidates").json()
        assert d["n_holdings"] == 2 and d["ranking"]["n_hold"] == 2 and d["reason"] is None
        # 売り切った 3333 は渡さない。書式は分析画面の「売買の記録の保有を入れる」と同じ
        assert captured == [("sell_ranking", {
            "holdings": "1111, @1000.0, 2026-01-05\n2222, @2000.5, 2026-01-05"})]

    def test_a_missing_dependency_is_a_reason_not_an_error(self, monkeypatch):
        """M-6 の μ̂ が未保存でも朝の画面を 4xx で壊さない（理由を返す）。"""
        import plugins as plugin_registry

        async def refuse(plugin, raw, db):
            raise plugin_registry.DependencyError(
                "sell_ranking", ["macro_enet"], "先に M-6 を実行してください")

        monkeypatch.setattr(plugin_registry, "execute_plugin", refuse)
        client.post("/api/portfolio/ledger", json=BUY)
        d = client.get("/api/portfolio/sell-candidates").json()
        assert d == {"n_holdings": 1, "ranking": None, "reason": "先に M-6 を実行してください"}

    def test_it_is_a_read_and_passes_when_read_only(self, monkeypatch, captured):
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        assert client.get("/api/portfolio/sell-candidates").status_code == 200
