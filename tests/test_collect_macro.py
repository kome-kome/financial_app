"""collect_macro_data のモックテスト (#152)。

MACRO_SERIES（9 系列）について、Yahoo Finance 取得→stooq フォールバックと、
既存レコードの更新／新規挿入の分岐を検証する。外部 HTTP は httpx 組み込みの
MockTransport で擬似（新規依存なし）。DB は conftest.py の in-memory SQLite fixture。
"""
import os
import sys
from datetime import date

import httpx
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
from collector import collect_macro_data, MACRO_SERIES
from database import MacroData


# ── 共通フィクスチャ：固定タイムスタンプと、そこから導く trade_date 文字列 ──────
TS1 = 1672617600  # 2023-01-02 00:00 UTC 付近
TS2 = 1672704000  # 翌日
DATE1 = date.fromtimestamp(TS1).strftime("%Y-%m-%d")
DATE2 = date.fromtimestamp(TS2).strftime("%Y-%m-%d")


def _yahoo_json(timestamps: list, closes: list) -> dict:
    """fetch_yahoo_history が解析する Yahoo Finance v8 chart レスポンス形式。"""
    n = len(timestamps)
    return {
        "chart": {"result": [{
            "timestamp": timestamps,
            "indicators": {"quote": [{
                "open":   closes,
                "high":   closes,
                "low":    closes,
                "close":  closes,
                "volume": [100] * n,
            }]},
        }]},
    }


def _stooq_csv(rows: list) -> str:
    """fetch_stooq_history が解析する CSV（[(date, close), ...]）。"""
    lines = ["Date,Open,High,Low,Close,Volume"]
    for d, c in rows:
        lines.append(f"{d},{c},{c},{c},{c},10")
    return "\n".join(lines)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_client(handler):
    """collector が内部生成する httpx.AsyncClient を MockTransport 付きに差し替える。"""
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
    return patch("collector.httpx.AsyncClient", new=factory)


def _run(db, handler, **kwargs):
    import asyncio
    with _patch_client(handler):
        return asyncio.run(collect_macro_data(db, years_back=1, **kwargs))


# ── 1. Yahoo 正常系：9 系列が保存される ─────────────────────────────────────
def test_yahoo_success_saves_all_series(db):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "query1.finance.yahoo.com":
            return httpx.Response(200, json=_yahoo_json([TS1, TS2], [1.0, 2.0]))
        # stooq は呼ばれないはず（呼ばれたら 500 でフォールバック失敗→検知しやすく）
        return httpx.Response(500)

    saved = _run(db, handler)

    # 9 系列 × 2 日 = 18 件挿入
    assert saved == len(MACRO_SERIES) * 2
    codes = {c for (c,) in db.query(MacroData.series_code).distinct().all()}
    assert codes == {s["code"] for s in MACRO_SERIES}
    # Yahoo の close 値が保存されている
    row = db.query(MacroData).filter_by(series_code="USDJPY", trade_date=DATE1).one()
    assert row.close == 1.0


# ── 2. Yahoo 失敗時：stooq へフォールバックする ─────────────────────────────
def test_falls_back_to_stooq_when_yahoo_fails(db):
    stooq_hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "query1.finance.yahoo.com":
            return httpx.Response(500)  # Yahoo 失敗 → fetch_yahoo_history は [] を返す
        if host == "stooq.com":
            stooq_hits.append(str(request.url))
            return httpx.Response(200, text=_stooq_csv([(DATE1, 1.5), (DATE2, 2.5)]))
        return httpx.Response(404)

    saved = _run(db, handler)

    assert saved == len(MACRO_SERIES) * 2
    assert stooq_hits, "stooq フォールバックが発火していない"
    # stooq の close 値（1.5）が保存されている＝フォールバック経路で書き込まれた
    row = db.query(MacroData).filter_by(series_code="USDJPY", trade_date=DATE1).one()
    assert row.close == 1.5


# ── 3. 既存系列は更新・無い系列は新規挿入 ───────────────────────────────────
def test_updates_existing_and_inserts_new(db):
    # DATE1 の USDJPY を既存レコードとして事前投入（close=999.0）
    db.add(MacroData(series_code="USDJPY", series_name="USD/JPY", category="fx",
                     trade_date=DATE1, open=999.0, high=999.0, low=999.0,
                     close=999.0, volume=1.0))
    db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "query1.finance.yahoo.com":
            # 既存（DATE1）＋新規（DATE2）の 2 行
            return httpx.Response(200, json=_yahoo_json([TS1, TS2], [1.0, 2.0]))
        return httpx.Response(500)

    saved = _run(db, handler)

    assert saved == len(MACRO_SERIES) * 2

    usdjpy = {r.trade_date: r for r in
              db.query(MacroData).filter_by(series_code="USDJPY").all()}
    # 既存 DATE1 行は更新（999.0 → 1.0）
    assert usdjpy[DATE1].close == 1.0
    # 新規 DATE2 行は挿入
    assert DATE2 in usdjpy
    assert usdjpy[DATE2].close == 2.0
    # USDJPY は 2 行のまま（既存行を重複挿入していない）
    assert len(usdjpy) == 2


# ── 4. FRED 公表ラグ補正（#250）：lag_days 分だけ trade_date が後ろへシフトする ──
from collector import fetch_fred_series, FRED_SERIES


def _fred_handler(observations: list):
    """FRED observations API のモック（[(date, value), ...]）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.stlouisfed.org":
            return httpx.Response(200, json={
                "observations": [{"date": d, "value": v} for d, v in observations]
            })
        return httpx.Response(404)
    return handler


def _fetch_fred(observations, **kwargs):
    import asyncio

    async def run():
        async with _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(_fred_handler(observations))
        ) as s:
            return await fetch_fred_series(s, "DUMMY", "2016-01-01", "2026-06-27", **kwargs)
    return asyncio.run(run())


def test_fred_lag_days_shifts_trade_date():
    # 四半期GDP：obs_date=期首 2026-01-01 を lag_days=135 で公表時点へシフト → 2026-05-16
    rows = _fetch_fred([("2026-01-01", "550.0")], lag_days=135)
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2026-05-16"
    assert rows[0]["close"] == 550.0


def test_fred_lag_days_zero_keeps_obs_date():
    # lag_days 既定=0 → 既存系列は完全後方互換（シフトなし）
    rows = _fetch_fred([("2026-05-01", "2.6")])
    assert rows[0]["trade_date"] == "2026-05-01"


def test_japan_macro_fred_series_registered():
    by_code = {s["code"]: s for s in FRED_SERIES}
    # 第1弾 日本実体経済指標が lag_days 付きで登録されている
    assert by_code["JP_REAL_GDP"]["fred_id"] == "JPNRGDPEXP"
    assert by_code["JP_REAL_GDP"]["lag_days"] == 135
    assert by_code["JP_UNEMP"]["lag_days"] == 60
    assert by_code["JP_IP"]["lag_days"] == 60
    assert by_code["JP_TRADE_BAL"]["lag_days"] == 135
    # 既存系列は lag_days 未設定（= 0 既定で後方互換）
    assert "lag_days" not in by_code["JP10Y_FRED"]
