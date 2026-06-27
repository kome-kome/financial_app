"""マクロ系エンドポイント（routers/market.py）の最小テスト。

/api/macro/series（系列カバレッジ一覧）と /api/macro/data/{series_code}
（系列日次データ・未知コードで 404）を検証。conftest の fixture DB を再利用。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
from database import MacroData  # noqa: E402
from collector import MACRO_SERIES, FRED_SERIES, BOJ_SERIES, ESTAT_SERIES  # noqa: E402

client = TestClient(api.app)

_ALL_SERIES = MACRO_SERIES + FRED_SERIES + BOJ_SERIES + ESTAT_SERIES
_VALID_CODE = MACRO_SERIES[0]["code"]  # 例: "USDJPY"


@pytest.fixture(autouse=True)
def _override_db(db):
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield
    api.app.dependency_overrides.clear()


# ── /api/macro/series ───────────────────────────────────────────────────────

class TestMacroSeries:
    def test_lists_all_series_with_zero_rows_when_empty(self):
        r = client.get("/api/macro/series")
        assert r.status_code == 200
        items = r.json()["series"]
        assert len(items) == len(_ALL_SERIES)
        assert all(i["rows"] == 0 for i in items)

    def test_reflects_row_counts(self, db):
        db.add(MacroData(series_code=_VALID_CODE, trade_date="2023-01-04", close=140.0))
        db.add(MacroData(series_code=_VALID_CODE, trade_date="2023-01-05", close=141.0))
        db.commit()
        r = client.get("/api/macro/series")
        assert r.status_code == 200
        target = next(i for i in r.json()["series"] if i["code"] == _VALID_CODE)
        assert target["rows"] == 2
        assert target["newest"] == "2023-01-05"


# ── /api/macro/data/{series_code} ───────────────────────────────────────────

class TestMacroData:
    def test_valid_code_returns_rows_ascending(self, db):
        db.add(MacroData(series_code=_VALID_CODE, trade_date="2023-01-05", close=141.0))
        db.add(MacroData(series_code=_VALID_CODE, trade_date="2023-01-04", close=140.0))
        db.commit()
        r = client.get(f"/api/macro/data/{_VALID_CODE}")
        assert r.status_code == 200
        body = r.json()
        assert body["series_code"] == _VALID_CODE
        dates = [row["trade_date"] for row in body["rows"]]
        assert dates == ["2023-01-04", "2023-01-05"]

    def test_unknown_code_returns_404(self):
        assert client.get("/api/macro/data/NOPE").status_code == 404

    def test_invalid_days_returns_400(self):
        assert client.get(f"/api/macro/data/{_VALID_CODE}?days=0").status_code == 400
