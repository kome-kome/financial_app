"""tests/test_collector_prices.py — collector_prices の純粋関数テスト（DB/ネットワーク不要）。

対象:
  - fetch_fred_series: FRED レスポンスのパース・欠損スキップ・エラー処理
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector_prices import fetch_fred_series


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fred_json(observations: list, status_code: int = 200) -> httpx.Response:
    body = json.dumps({"observations": observations}).encode()
    return httpx.Response(status_code, content=body)


SAMPLE_OBS = [
    {"date": "2025-01-01", "value": "4.5"},
    {"date": "2025-02-01", "value": "4.6"},
    {"date": "2025-03-01", "value": "4.7"},
]


class TestFetchFredSeries:

    def test_normal_parse(self):
        """正常系: 全観測を close にパースして返す。"""
        def handler(req):
            return _fred_json(SAMPLE_OBS)

        rows = asyncio.run(self._fetch(handler))
        assert len(rows) == 3
        assert rows[0]["trade_date"] == "2025-01-01"
        assert rows[0]["close"] == pytest.approx(4.5)
        assert rows[2]["close"] == pytest.approx(4.7)
        assert rows[0]["open"] is None
        assert rows[0]["volume"] is None

    def test_missing_dot_skipped(self):
        """欠損値 "." はスキップされ、有効値だけ返る。"""
        obs = [
            {"date": "2025-01-01", "value": "."},
            {"date": "2025-02-01", "value": "4.6"},
        ]

        def handler(req):
            return _fred_json(obs)

        rows = asyncio.run(self._fetch(handler))
        assert len(rows) == 1
        assert rows[0]["trade_date"] == "2025-02-01"

    def test_none_value_skipped(self):
        """value=None はスキップされる。"""
        obs = [
            {"date": "2025-01-01", "value": None},
            {"date": "2025-02-01", "value": "3.0"},
        ]

        def handler(req):
            return _fred_json(obs)

        rows = asyncio.run(self._fetch(handler))
        assert len(rows) == 1

    def test_invalid_float_skipped(self):
        """float() 変換不能な値はスキップされ、他の行は返る。"""
        obs = [
            {"date": "2025-01-01", "value": "N/A"},
            {"date": "2025-02-01", "value": "4.6"},
        ]

        def handler(req):
            return _fred_json(obs)

        rows = asyncio.run(self._fetch(handler))
        assert len(rows) == 1

    def test_empty_observations(self):
        """observations が空なら空リストを返す。"""
        def handler(req):
            return _fred_json([])

        rows = asyncio.run(self._fetch(handler))
        assert rows == []

    def test_http_error_returns_empty(self):
        """HTTP 4xx は [] を返す（例外を上に伝播させない）。"""
        def handler(req):
            return httpx.Response(400, content=b'{"error_message":"Bad Request"}')

        rows = asyncio.run(self._fetch(handler))
        assert rows == []

    def test_http_500_returns_empty(self):
        """HTTP 5xx も [] を返す。"""
        def handler(req):
            return httpx.Response(500, content=b"Internal Server Error")

        rows = asyncio.run(self._fetch(handler))
        assert rows == []

    def test_network_error_returns_empty(self):
        """ネットワーク接続エラーは [] を返す。"""
        def handler(req):
            raise httpx.ConnectError("connection refused")

        rows = asyncio.run(self._fetch(handler))
        assert rows == []

    # ── ヘルパー ─────────────────────────────────────────────────────────────

    @staticmethod
    async def _fetch_async(handler):
        async with _client(handler) as session:
            return await fetch_fred_series(session, "T10Y2Y", "2025-01-01", "2025-03-31")

    @classmethod
    def _fetch(cls, handler):
        return cls._fetch_async(handler)
