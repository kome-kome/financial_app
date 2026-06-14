"""分析系エンドポイント（routers/analysis.py）の最小テスト。

/api/recommend（プラグイン入口・不正 params で ValueError→400）・
/api/recommend/presets・/api/backtest/multi の正常系を検証。
conftest の fixture DB を再利用。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _override_db(db):
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield
    api.app.dependency_overrides.clear()


# ── /api/recommend/presets ──────────────────────────────────────────────────

class TestRecommendPresets:
    def test_returns_presets_and_metrics(self):
        r = client.get("/api/recommend/presets")
        assert r.status_code == 200
        body = r.json()
        assert "バランス型" in body["presets"]
        assert isinstance(body["metrics"], list) and body["metrics"]


# ── /api/recommend ──────────────────────────────────────────────────────────

class TestRecommend:
    def test_empty_db_returns_empty_ranking(self):
        r = client.post("/api/recommend", json={"preset": "バランス型"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["results"] == []

    def test_ranks_records(self, db, make_metric):
        db.add(make_metric(edinet_code="E00001", year=2023, roe=0.2, z_roe=2.0))
        db.add(make_metric(edinet_code="E00002", sec_code="1002", year=2023, roe=0.1, z_roe=-1.0))
        db.commit()
        r = client.post("/api/recommend", json={"preset": "バランス型", "min_coverage": 0.0})
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 2
        # z_roe が高い E00001 が上位
        assert results[0]["edinet_code"] == "E00001"

    def test_invalid_preset_returns_400(self):
        r = client.post("/api/recommend", json={"preset": "存在しないプリセット"})
        assert r.status_code == 400

    def test_out_of_range_top_n_returns_400(self):
        r = client.post("/api/recommend", json={"preset": "バランス型", "top_n": 9999})
        assert r.status_code == 400


# ── /api/backtest/multi ─────────────────────────────────────────────────────

class TestBacktestMulti:
    def test_empty_db_returns_periods(self):
        r = client.get("/api/backtest/multi")
        assert r.status_code == 200
        body = r.json()
        assert body["preset"] == "バランス型"
        assert isinstance(body["periods"], list) and body["periods"]

    def test_invalid_top_n_returns_400(self):
        assert client.get("/api/backtest/multi?top_n=1").status_code == 400
