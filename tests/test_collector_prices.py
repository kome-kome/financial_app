"""tests/test_collector_prices.py — collector_prices の純粋関数テスト（DB/ネットワーク不要）。

対象:
  - fetch_fred_series: FRED レスポンスのパース・欠損スキップ・エラー処理
  - _price_collection_driver: 保存失敗の再試行とタイムアウト引き上げ（#470）
  - last_closed_session: Yahoo ギャップ補完の基準となる JST 営業日（#474）
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector_prices as cp
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


# ── 株価保存ドライバの再試行（#470）──────────────────────────────────────────
# 2026-08-08 の夜間差分収集は、この経路で pooler 枯渇（ECHECKOUTTIMEOUT）2回と
# statement_timeout 1回を踏み、いずれも warning 1行だけ残してバッチを捨てていた。
# 落ちた株価は「収集は成功扱い・データだけ穴」という形で残るため気づけない。

class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeDB:
    bind = _FakeBind()

    def __init__(self):
        self.executed: list[str] = []
        self.rollbacks = 0
        self.commits = 0

    def execute(self, stmt, params=None):
        self.executed.append(str(stmt))

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


async def _gen(batches):
    for b in batches:
        yield b


def _drive(monkeypatch, db, batches, save_side_effects):
    """record_prices_batch / trim_daily / sleep を差し替えて driver を回す。"""
    calls = {"save": 0, "trim": 0, "slept": []}

    def _save(_db, batch, trim=False):
        calls["save"] += 1
        effect = save_side_effects[min(calls["save"] - 1, len(save_side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(cp, "record_prices_batch", _save)
    monkeypatch.setattr(cp, "trim_daily", lambda _db: calls.__setitem__("trim", calls["trim"] + 1))

    async def _sleep(sec):
        calls["slept"].append(sec)

    monkeypatch.setattr(cp.asyncio, "sleep", _sleep)
    cancelled, total = asyncio.run(cp._price_collection_driver(db, _gen(batches)))
    return cancelled, total, calls


ROW = [{"edinet_code": "E00001", "trade_date": "2026-08-07", "close": 100.0}]


class TestPriceBatchRetry:

    def test_transient_failure_is_retried_and_recovered(self, monkeypatch):
        """1回目が落ちても再試行で保存され、件数が失われない。"""
        db = _FakeDB()
        err = RuntimeError("FATAL: (ECHECKOUTTIMEOUT) unable to check out connection")
        _, total, calls = _drive(monkeypatch, db, [ROW], [err, 1])

        assert total == 1
        assert calls["save"] == 2
        assert db.rollbacks == 1                       # 失敗のたびに aborted を解消
        assert calls["slept"] == [cp.PRICE_BATCH_RETRY_SLEEP]

    def test_backoff_grows_per_attempt(self, monkeypatch):
        db = _FakeDB()
        err = RuntimeError("statement timeout")
        _, total, calls = _drive(monkeypatch, db, [ROW], [err, err, 3])

        assert total == 3
        assert calls["slept"] == [cp.PRICE_BATCH_RETRY_SLEEP, cp.PRICE_BATCH_RETRY_SLEEP * 2]

    def test_gives_up_after_max_attempts_without_killing_collection(self, monkeypatch):
        """使い切ったらそのバッチは捨てるが、後続バッチと trim は続ける（従来方針）。"""
        db = _FakeDB()
        err = RuntimeError("boom")
        _, total, calls = _drive(monkeypatch, db, [ROW, ROW], [err] * 10)

        assert total == 0
        assert calls["save"] == cp.PRICE_BATCH_MAX_ATTEMPTS * 2
        assert calls["trim"] == 1

    def test_success_path_does_not_sleep(self, monkeypatch):
        db = _FakeDB()
        _, total, calls = _drive(monkeypatch, db, [ROW, ROW], [1, 1])

        assert (total, calls["save"], calls["slept"]) == (2, 2, [])
        assert db.rollbacks == 0

    def test_statement_timeout_is_raised_for_save_and_trim(self, monkeypatch):
        """保存と trim（全社横断 DELETE）はどちらも 2min を超えうる。"""
        db = _FakeDB()
        _drive(monkeypatch, db, [ROW], [1])

        sets = [s for s in db.executed if s.startswith("SET statement_timeout")]
        assert sets == [f"SET statement_timeout = '{cp.HEAVY_STATEMENT_TIMEOUT}'"] * 2
        assert db.executed.count("RESET statement_timeout") == 2

    def test_cancellation_sentinel_still_commits_and_stops(self, monkeypatch):
        """None は中断合図。従来どおり commit して打ち切る。"""
        db = _FakeDB()
        cancelled, total, calls = _drive(monkeypatch, db, [ROW, None, ROW], [1, 1])

        assert (cancelled, total) == (True, 1)
        assert calls["trim"] == 0
        assert db.commits == 1


# ── last_closed_session（#474）──────────────────────────────────────────────

class TestLastClosedSession:
    """閉場済みの最新 JST 営業日。

    毎晩の Yahoo ギャップ補完はこの日付を基準に対象社を絞る。旧実装は
    ランナーの **UTC 日付**と比べていたため、その日のセッションがまだ無い時間帯・
    非営業日には全社が対象になっていた（run 31272807314 で 4,437社 / 2h11m）。
    """

    def _at(self, y, m, d, hh, mm=0):
        return cp.last_closed_session(datetime(y, m, d, hh, mm, tzinfo=cp.JST))

    def test_weekday_before_close_uses_previous_day(self):
        """定時実行（JST 03:00）はまだ当日が引けていない → 前営業日。"""
        assert self._at(2026, 8, 6, 3) == date(2026, 8, 5)      # 木03:00 → 水

    def test_weekday_after_close_uses_same_day(self):
        assert self._at(2026, 8, 6, 16) == date(2026, 8, 6)     # 木16:00 → 木

    def test_exactly_at_market_close_boundary(self):
        """15:10 ちょうどは「引けた」側。大引け15:00＋Yahoo 反映の余裕。"""
        assert self._at(2026, 8, 6, 15, 10) == date(2026, 8, 6)
        assert self._at(2026, 8, 6, 15, 9)  == date(2026, 8, 5)

    def test_saturday_and_sunday_fall_back_to_friday(self):
        assert self._at(2026, 8, 8, 3)  == date(2026, 8, 7)     # 土03:00 → 金
        assert self._at(2026, 8, 8, 20) == date(2026, 8, 7)     # 土20:00 → 金（土は非営業）
        assert self._at(2026, 8, 9, 3)  == date(2026, 8, 7)     # 日03:00 → 金

    def test_monday_early_morning_falls_back_to_friday(self):
        """実害が出ていた回。JST 月曜 03:00 の基準は金曜であって日曜ではない。"""
        assert self._at(2026, 8, 10, 3) == date(2026, 8, 7)

    def test_never_returns_a_weekend(self):
        base = datetime(2026, 8, 3, 0, 0, tzinfo=cp.JST)
        for h in range(24 * 14):
            assert cp.last_closed_session(base + timedelta(hours=h)).weekday() < 5
