"""collect_macro_data のモックテスト (#152)。

MACRO_SERIES 全系列について、Yahoo Finance 取得→stooq フォールバックと、
既存レコードの更新／新規挿入の分岐を検証する（件数アサートは len(MACRO_SERIES)
でパラメタライズ済みのため系列追加に自動追従する）。外部 HTTP は httpx 組み込みの
MockTransport で擬似（新規依存なし）。DB は conftest.py の in-memory SQLite fixture。
"""
import io
import os
import sys
from datetime import date, timedelta

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


# ── 1. Yahoo 正常系：全系列が保存される ─────────────────────────────────────
def test_yahoo_success_saves_all_series(db):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "query1.finance.yahoo.com":
            return httpx.Response(200, json=_yahoo_json([TS1, TS2], [1.0, 2.0]))
        # stooq は呼ばれないはず（呼ばれたら 500 でフォールバック失敗→検知しやすく）
        return httpx.Response(500)

    saved = _run(db, handler)

    # 全系列 × 2 日 挿入（件数は MACRO_SERIES の長さに追従）
    assert saved == len(MACRO_SERIES) * 2
    codes = {c for (c,) in db.query(MacroData.series_code).distinct().all()}
    assert codes == {s["code"] for s in MACRO_SERIES}
    # Yahoo の close 値が保存されている
    row = db.query(MacroData).filter_by(series_code="USDJPY", trade_date=DATE1).one()
    assert row.close == 1.0


# ── 1b. コモディティ・チャネル拡張が MACRO_SERIES に登録されている（#358）──────
def test_commodity_series_defined():
    by_code = {s["code"]: s for s in MACRO_SERIES}
    # 既存2本＋拡張8本 = コモディティ計10系列
    expected = {"WTI", "GOLD", "BCOM", "COPPER", "NATGAS", "SILVER",
                "WHEAT", "CORN", "SOYBEAN", "PLATINUM"}
    assert expected <= set(by_code), f"未登録: {expected - set(by_code)}"
    for code in expected:
        assert by_code[code]["category"] == "commodity", f"{code} の category が commodity でない"
        assert by_code[code]["yf_ticker"], f"{code} に yf_ticker が無い"
    # 拡張8本の Yahoo ティッカー（Phase 0 疎通検証済み）
    assert by_code["BCOM"]["yf_ticker"]     == "^BCOM"
    assert by_code["COPPER"]["yf_ticker"]   == "HG=F"
    assert by_code["NATGAS"]["yf_ticker"]   == "NG=F"
    assert by_code["PLATINUM"]["yf_ticker"] == "PL=F"


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
    # JP_IP (JPNPROINDMISMEI) は 2024-04-30 で凍結のため除外中 (#253)
    assert "JP_IP" not in by_code
    assert by_code["JP_TRADE_BAL"]["lag_days"] == 135
    # 既存系列は lag_days 未設定（= 0 既定で後方互換）
    assert "lag_days" not in by_code["JP10Y_FRED"]
    # #381 非ICE代替の信用スプレッド（BAA10Y=Moody's Baa−10Y・日次・truncate されない）
    assert by_code["BAA_SPREAD"]["fred_id"] == "BAA10Y"
    assert by_code["BAA_SPREAD"]["category"] == "credit"
    assert "lag_days" not in by_code["BAA_SPREAD"]  # 日次系列なのでラグ補正不要


# ── 5. 日銀 API フェッチャー（ADR-0006 §着手点1）───────────────────────────
from collector import fetch_boj_series, BOJ_SERIES, ESTAT_SERIES, fetch_estat_series


def _boj_json(series_code: str, freq: str, survey_dates: list, values: list) -> dict:
    """fetch_boj_series が解析する BOJ API getDataCode レスポンス。"""
    return {
        "STATUS": 200,
        "RESULTSET": [{
            "SERIES_CODE": series_code,
            "FREQUENCY": freq,
            "VALUES": {"SURVEY_DATES": survey_dates, "VALUES": values},
        }],
    }


def _fetch_boj(series_code, survey_dates, values, freq="monthly", lag_days=0, db_name="MD02"):
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if "stat-search.boj.or.jp" in request.url.host:
                return httpx.Response(200, json=_boj_json(series_code, freq, survey_dates, values))
            return httpx.Response(404)
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_boj_series(s, db_name, series_code, "202001", "202601",
                                          lag_days=lag_days, freq=freq)
    return asyncio.run(run())


def test_boj_monthly_m2_date_conversion_and_lag():
    # M2 月次: YYYYMM=202501 → 2025-01-01 + 21days → 2025-01-22
    rows = _fetch_boj("MAM1NAM2M2MO", [202501, 202502], [12000000, 12100000],
                      freq="monthly", lag_days=21)
    assert len(rows) == 2
    assert rows[0]["trade_date"] == "2025-01-22"
    assert rows[1]["trade_date"] == "2025-02-22"
    assert rows[0]["close"] == 12000000.0


def test_boj_quarterly_tankan_date_conversion_and_lag():
    # 短観 四半期: Q1(01)→4月1日+14=4月15, Q2(02)→7月1日+14=7月15,
    #              Q3(03)→10月1日+14=10月15, Q4(04)→翌年1月1日+14=1月15
    rows = _fetch_boj("TK99F1000601GCQ01000", [202401, 202402, 202403, 202404],
                      [11, 13, 14, 14], freq="quarterly", lag_days=14, db_name="CO")
    assert len(rows) == 4
    assert rows[0]["trade_date"] == "2024-04-15"   # Q1
    assert rows[1]["trade_date"] == "2024-07-15"   # Q2
    assert rows[2]["trade_date"] == "2024-10-15"   # Q3
    assert rows[3]["trade_date"] == "2025-01-15"   # Q4 → 翌年1月


def test_boj_series_registered():
    by_code = {s["code"]: s for s in BOJ_SERIES}
    # M2
    assert "JP_M2" in by_code
    assert by_code["JP_M2"]["db"] == "MD02"
    assert by_code["JP_M2"]["freq"] == "monthly"
    assert by_code["JP_M2"]["lag_days"] == 21
    # 短観 4バリアント登録済み
    for code in ("JP_TANKAN_MFG_LARGE", "JP_TANKAN_NONMFG_LARGE",
                 "JP_TANKAN_MFG_SMALL", "JP_TANKAN_NONMFG_SMALL"):
        assert code in by_code, f"{code} が BOJ_SERIES に未登録"
        assert by_code[code]["db"] == "CO"
        assert by_code[code]["freq"] == "quarterly"
        assert by_code[code]["lag_days"] == 14
    # CGPI（#282）
    assert "JP_CGPI" in by_code
    assert by_code["JP_CGPI"]["db"] == "PR01"
    assert by_code["JP_CGPI"]["boj_code"] == "PRCG20_2200000000"
    assert by_code["JP_CGPI"]["freq"] == "monthly"
    # マネタリーベース（#282）
    assert "JP_MONETARY_BASE" in by_code
    assert by_code["JP_MONETARY_BASE"]["db"] == "MD01"
    assert by_code["JP_MONETARY_BASE"]["boj_code"] == "MABS1AN11"
    assert by_code["JP_MONETARY_BASE"]["freq"] == "monthly"


def test_estat_series_registered():
    by_code = {s["code"]: s for s in ESTAT_SERIES}
    # CPI 3系列
    assert "JP_CPI_TOTAL" in by_code
    assert "JP_CPI_CORE"  in by_code
    assert "JP_CPI_TOKYO" in by_code
    assert by_code["JP_CPI_CORE"]["cd_cat01"]  == "0161"   # 生鮮食品を除く総合
    # 表示名は "13100 東京都区部" だが実際の cdArea コードは 13A01（#262 で実API確認）
    assert by_code["JP_CPI_TOKYO"]["cd_area"]  == "13A01"  # 東京都区部
    assert by_code["JP_CPI_CORE"]["lag_days"]  == 30
    # cdTab（表章項目=指数）未指定が年次データのみ返却される原因だった（#262）
    for code in ("JP_CPI_TOTAL", "JP_CPI_CORE", "JP_CPI_TOKYO"):
        assert by_code[code]["cd_tab"] == "1", f"{code} に cd_tab 未設定"


def test_boj_api_error_returns_empty():
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_boj_series(s, "MD02", "MAM1NAM2M2MO", "202501", "202503")
    rows = asyncio.run(run())
    assert rows == []


def test_boj_status_non_200_returns_empty():
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"STATUS": 400, "MESSAGE": "Invalid params"})
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_boj_series(s, "MD02", "MAM1NAM2M2MO", "202501", "202503")
    rows = asyncio.run(run())
    assert rows == []


def test_boj_quarterly_uses_yyyyqq_date_format():
    """quarterly 系列は startDate/endDate を YYYYQQ 形式で送信する（YYYYMM だと BOJ API が 400）。"""
    import asyncio
    captured = {}

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_boj_json("TK_CODE", "quarterly", [], []))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            # date_from="202006"(June2020)→Q1="202001", date_to="202606"(June2026)→Q1="202601"
            return await fetch_boj_series(s, "CO", "TK_CODE", "202006", "202606",
                                          freq="quarterly")
    asyncio.run(run())
    assert "startDate=202001" in captured["url"], captured["url"]
    assert "endDate=202601"   in captured["url"], captured["url"]


# ── 6. e-Stat API フェッチャー（cdTab/lvTime 未指定バグの回帰テスト・#262）─────────
# 実 API 検証済みの @time 実測フォーマット: 月次="YYYY"+"00"+"MM"+"MM"（月を2回繰り返す。
# 例 2024年12月="2024001212"）、年度（会計年度集計）="YYYY"+"10"+"0000"。

def _estat_json(values: list) -> dict:
    """fetch_estat_series が解析する e-Stat API getStatsData レスポンス形式。"""
    return {
        "GET_STATS_DATA": {
            "STATISTICAL_DATA": {
                "DATA_INF": {"VALUE": values},
            },
        },
    }


def _fetch_estat(cd_tab, cd_cat01, cd_area, values, lag_days=0):
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_estat_json(values))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_estat_series(s, "0003427113", cd_tab, cd_cat01, cd_area,
                                            "2020000101", "2026000606", lag_days=lag_days)
    return asyncio.run(run())


def test_estat_monthly_values_parsed_and_deduped():
    """@time の月次コード（"YYYY"+"00"+"MM"+"MM"）から年・月を復元し、lag_days 分シフトする。
    月は先頭6文字ではなく末尾2文字から取り出す点が回帰しやすい（#262）。"""
    values = [
        {"@time": "2024000101", "@cat01": "0161", "@area": "00000", "$": "105.2"},  # 2024年1月
        {"@time": "2024000202", "@cat01": "0161", "@area": "00000", "$": "105.6"},  # 2024年2月
    ]
    rows = _fetch_estat("1", "0161", "00000", values, lag_days=30)
    assert len(rows) == 2
    assert rows[0]["trade_date"] == "2024-01-31"  # 2024-01-01 + 30日
    assert rows[0]["close"] == 105.2
    assert rows[1]["trade_date"] == "2024-03-02"  # 2024-02-01 + 30日（うるう年）


def test_estat_ignores_fiscal_year_rows_if_present():
    """年度集計行（@time="YYYY"+"10"+"0000"）が万一混入しても、月として誤読しない
    （[8:10]="00" は有効な月ではないため ValueError で当該行のみスキップされる）。"""
    values = [
        {"@time": "2024000101", "@cat01": "0161", "@area": "00000", "$": "105.2"},  # 2024年1月（正常）
        {"@time": "2024100000", "@cat01": "0161", "@area": "00000", "$": "999.9"},  # 2024年度（除外されるべき）
    ]
    rows = _fetch_estat("1", "0161", "00000", values)
    assert len(rows) == 1
    assert rows[0]["close"] == 105.2


def test_estat_filters_by_cat01_when_multiple_categories_present():
    """cdCat01 を指定していても API が複数カテゴリを混在返却する場合に備え、
    後段の @cat01 一致フィルタで意図しないカテゴリの値を除外できることを確認する。"""
    values = [
        {"@time": "2024000101", "@cat01": "0001", "@area": "00000", "$": "106.0"},  # 総合（除外対象）
        {"@time": "2024000101", "@cat01": "0161", "@area": "00000", "$": "105.2"},  # コア（一致）
    ]
    rows = _fetch_estat("1", "0161", "00000", values)
    assert len(rows) == 1
    assert rows[0]["close"] == 105.2


def test_estat_series_request_includes_cdtab_and_lvtime():
    """cdTab（表章項目）・lvTime（時間軸レベル=月次）のどちらか片方でも欠けると
    年次データのみ返却される（#262 で実API確認済み）ため、両方が送信パラメータに
    含まれることを確認する。"""
    import asyncio
    captured = {}

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_estat_json([]))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_estat_series(s, "0003427113", "1", "0161", "00000",
                                            "2020000101", "2026000606")
    asyncio.run(run())
    assert "cdTab=1" in captured["url"], captured["url"]
    assert "lvTime=4" in captured["url"], captured["url"]


def test_estat_response_missing_value_path_returns_empty():
    """lvTime=2 で確認された「HTTP 200 だが DATA_INF.VALUE が存在しない」形状でも
    例外を送出せず空リストへフォールバックする（#262 症状の回帰防止）。"""
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "GET_STATS_DATA": {"RESULT": {"STATUS": 1, "ERROR_MSG": "該当データなし"}},
            })
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_estat_series(s, "0003427113", "1", "0161", "00000",
                                            "2020000101", "2026000606")
    rows = asyncio.run(run())
    assert rows == []


# ── 7. e-Stat 鉱工業指数フェッチャー（time軸が連番コード・#253/#281）─────────────
# CPI と異なり @time が "0500100" のような連番コードで年月を直接表現しない。
# metaGetFlg="Y" で同梱される CLASS_INF（time クラス code→"YYYYMM"）を使って変換する。
# 実API検証値: 鉱工業指数2020年基準・code="0519700"→name="202603"（2026年3月・最新）。

from collector import fetch_estat_index_series, ESTAT_INDEX_SERIES


def _estat_index_json(values: list, time_classes: list) -> dict:
    """fetch_estat_index_series が解析する metaGetFlg=Y 付き getStatsData レスポンス形式。"""
    return {
        "GET_STATS_DATA": {
            "STATISTICAL_DATA": {
                "DATA_INF": {"VALUE": values},
                "CLASS_INF": {"CLASS_OBJ": [
                    {"@id": "cat01", "@name": "業種別", "CLASS": []},
                    {"@id": "time", "@name": "時間軸", "CLASS": time_classes},
                ]},
            },
        },
    }


def _fetch_estat_index(values, time_classes, lag_days=0):
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_estat_index_json(values, time_classes))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_estat_index_series(s, "0004052177", "0001000", lag_days=lag_days)
    return asyncio.run(run())


def test_estat_index_series_registered():
    by_code = {s["code"]: s for s in ESTAT_INDEX_SERIES}
    assert "JP_IIP" in by_code
    assert "JP_IIP_INVENTORY" in by_code
    assert by_code["JP_IIP"]["cd_cat01"] == "0001000"  # 鉱工業総合
    assert by_code["JP_IIP"]["lag_days"] == 60


def test_estat_index_time_code_resolved_via_meta():
    """@time の連番コード（"0500100" 等）はメタ情報（code→"YYYYMM"）経由で解決する。"""
    time_classes = [
        {"@code": "0100100", "@name": "付加生産ウエイト"},
        {"@code": "0500100", "@name": "201801"},
        {"@code": "0519700", "@name": "202603"},
    ]
    values = [
        {"@time": "0100100", "$": "10000"},
        {"@time": "0500100", "$": "112.3"},
        {"@time": "0519700", "$": "102.0"},
    ]
    rows = _fetch_estat_index(values, time_classes)
    # ウエイト行(0100100)はスキップされ、2件のみ残る
    assert len(rows) == 2
    by_date = {r["trade_date"]: r["close"] for r in rows}
    assert by_date["2018-01-01"] == 112.3
    assert by_date["2026-03-01"] == 102.0


# ── 8. OECD SDMX API フェッチャー（先行指標CLI・ADR-0009・#283）─────────────────
# 実API検証済み（2026-07-09）: CSV形式（csvfilewithlabels）で TIME_PERIOD/OBS_VALUE 列を
# 返す。存在しない series_key は HTTP 404 + プレーンテキスト "NoRecordsFound"。

from collector import fetch_oecd_series, OECD_SERIES


def _oecd_csv(rows: list) -> str:
    """fetch_oecd_series が解析する OECD SDMX csvfilewithlabels 形式（簡略版・
    実レスポンスは列が多いが TIME_PERIOD/OBS_VALUE のみパースに使うため他は省略）。"""
    lines = ["TIME_PERIOD,OBS_VALUE"]
    for t, v in rows:
        lines.append(f"{t},{v}")
    return "\n".join(lines)


def _fetch_oecd(rows, lag_days=0, status=200, body=None):
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if status != 200:
                return httpx.Response(status, text=body or "")
            return httpx.Response(200, text=_oecd_csv(rows))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_oecd_series(
                s, "OECD.SDD.STES,DSD_STES@DF_CLI,4.1", "JPN.M.LI.IX._Z.AA.IX._Z.H",
                "2020-01", lag_days=lag_days,
            )
    return asyncio.run(run())


def test_oecd_series_registered():
    by_code = {s["code"]: s for s in OECD_SERIES}
    assert "JP_CLI" in by_code
    assert by_code["JP_CLI"]["dataflow"]   == "OECD.SDD.STES,DSD_STES@DF_CLI,4.1"
    assert by_code["JP_CLI"]["series_key"] == "JPN.M.LI.IX._Z.AA.IX._Z.H"
    assert by_code["JP_CLI"]["lag_days"]   == 60


def test_oecd_monthly_values_parsed_and_lag_shifted():
    """TIME_PERIOD（"YYYY-MM"）から年月を復元し、lag_days 分シフトする。"""
    rows = _fetch_oecd([("2024-06", "99.88074"), ("2024-07", "99.83847")], lag_days=60)
    assert len(rows) == 2
    assert rows[0]["trade_date"] == "2024-07-31"  # 2024-06-01 + 60日
    assert rows[0]["close"] == 99.88074
    assert rows[1]["trade_date"] == "2024-08-30"  # 2024-07-01 + 60日


def test_oecd_missing_series_returns_empty():
    """存在しない series_key は HTTP 404 + "NoRecordsFound"（JSONではない）を返す。
    raise_for_status() で例外化されログ経由で空リストへフォールバックする。"""
    rows = _fetch_oecd([], status=404, body="NoRecordsFound")
    assert rows == []


def test_oecd_api_error_returns_empty():
    rows = _fetch_oecd([], status=500, body="Internal Server Error")
    assert rows == []


def test_estat_index_skips_weight_row():
    """ウエイト行（time_map の名前が "付加生産ウエイト" 等・YYYYMM形式でない）は
    誤って日付として解釈されず除外される。"""
    time_classes = [{"@code": "0100100", "@name": "付加生産ウエイト"}]
    values = [{"@time": "0100100", "$": "10000"}]
    rows = _fetch_estat_index(values, time_classes)
    assert rows == []


def test_estat_index_lag_days_shifts_trade_date():
    time_classes = [{"@code": "0500100", "@name": "202401"}]
    values = [{"@time": "0500100", "$": "105.0"}]
    rows = _fetch_estat_index(values, time_classes, lag_days=60)
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2024-03-01"  # 2024-01-01 + 60日（うるう年）


def test_estat_index_request_includes_metagetflg_and_cdcat01():
    """metaGetFlg=Y（メタ情報同梱・追加リクエスト不要）と cdCat01 が送信されることを確認。"""
    import asyncio
    captured = {}

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_estat_index_json([], []))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_estat_index_series(s, "0004052177", "0001000")
    asyncio.run(run())
    assert "metaGetFlg=Y" in captured["url"], captured["url"]
    assert "cdCat01=0001000" in captured["url"], captured["url"]


def test_estat_index_response_missing_path_returns_empty():
    """レスポンスに DATA_INF/CLASS_INF が無い異常系でも例外を送出せず空リストへ。"""
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "GET_STATS_DATA": {"RESULT": {"STATUS": 1, "ERROR_MSG": "該当データなし"}},
            })
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_estat_index_series(s, "0004052177", "0001000")
    rows = asyncio.run(run())
    assert rows == []


# ── 6. 内閣府ESRI GDP需要項目 直接CSV配布（#286）───────────────────────────
from collector import (
    fetch_esri_gdp_csv, ESRI_SERIES, _esri_candidate_urls, _parse_esri_gdp_csv,
    _esri_apply_lag,
)


# 実データの列順を再現した簡略ヘッダー行（実際は日本語見出し行等も混在するが
# _parse_esri_gdp_csv は "PrivateConsumption" セルを含む行のみを探すため無関係）。
ESRI_HEADER_ROW = (
    ",GDP(Expenditure Approach),PrivateConsumption,Consumption ofHouseholds,"
    "ExcludingImputed Rent,PrivateResidentialInvestment,Private Non-Resi.Investment,"
    "Changein PrivateInventories,GovernmentConsumption,PublicInvestment,"
    "Changein PublicInventories,Net Exports"
)


def _esri_csv(data_rows: list) -> str:
    """fetch_esri_gdp_csv/_parse_esri_gdp_csv が解析するESRI実額系列CSVの簡略版。"""
    lines = ["実質季節調整系列,,,", ESRI_HEADER_ROW, *data_rows]
    return "\n".join(lines)


_ESRI_ROW_1994Q1 = (
    '1994/ 1- 3.,"465,065.0 ","252,337.9 ","248,579.4 ","212,801.4 ","33,377.1 ",'
    '"75,393.7 ","3,963.4 ","70,669.0 ","47,225.3 ",-489.4 ,"-5,582.8 "'
)
_ESRI_ROW_1994Q2 = (
    '4- 6.,"461,497.8 ","253,547.0 ","249,701.3 ","213,721.0 ","34,755.7 ",'
    '"74,417.6 ","-2,837.6 ","71,469.4 ","48,073.0 ",559.7 ,"-6,148.3 "'
)


def test_esri_series_registered():
    by_code = {s["code"]: s for s in ESRI_SERIES}
    assert by_code["JP_GDP_PRIVATE_CONSUMPTION"]["esri_column"] == "PrivateConsumption"
    assert by_code["JP_GDP_RESIDENTIAL_INV"]["esri_column"]     == "PrivateResidentialInvestment"
    assert by_code["JP_GDP_CAPEX"]["esri_column"]                == "Private Non-Resi.Investment"
    assert by_code["JP_GDP_PUBLIC_INV"]["esri_column"]           == "PublicInvestment"
    for s in ESRI_SERIES:
        assert s["category"]  == "real_economy"
        assert s["lag_days"]  == 60


def test_esri_candidate_urls_newest_quarter_and_report_first():
    urls = _esri_candidate_urls(date(2026, 7, 10))  # 2026Q3中
    assert len(urls) == 8  # 直近4四半期 × 速報2種
    assert urls[0] == ("https://www.esri.cao.go.jp/jp/sna/data/data_list/sokuhou/files/"
                        "2026/qe263_2/tables/gaku-jk2632.csv")
    assert urls[1] == ("https://www.esri.cao.go.jp/jp/sna/data/data_list/sokuhou/files/"
                        "2026/qe263_1/tables/gaku-jk2631.csv")
    assert urls[-1] == ("https://www.esri.cao.go.jp/jp/sna/data/data_list/sokuhou/files/"
                         "2025/qe254_1/tables/gaku-jk2541.csv")


def test_esri_candidate_urls_year_rollover():
    urls = _esri_candidate_urls(date(2026, 1, 15))  # 2026Q1中 → 遡ると前年へまたぐ
    assert "qe261_2" in urls[0]
    assert "qe254_2" in urls[2]  # 2025Q4


def test_esri_parse_extracts_four_columns_and_carries_year():
    text = _esri_csv([_ESRI_ROW_1994Q1, _ESRI_ROW_1994Q2])
    result = _parse_esri_gdp_csv(text)
    assert set(result.keys()) == {
        "PrivateConsumption", "PrivateResidentialInvestment",
        "Private Non-Resi.Investment", "PublicInvestment",
    }

    consumption = result["PrivateConsumption"]
    assert len(consumption) == 2
    # Q1（年明示）→ 翌四半期初日 1994-04-01 が基準日。Q2（年省略）は前行の年を引き継ぐ。
    assert consumption[0]["trade_date"] == "1994-04-01"
    assert consumption[0]["close"] == 252337.9
    assert consumption[1]["trade_date"] == "1994-07-01"
    assert consumption[1]["close"] == 253547.0

    assert result["PrivateResidentialInvestment"][0]["close"] == 33377.1
    assert result["Private Non-Resi.Investment"][0]["close"]  == 75393.7
    assert result["PublicInvestment"][0]["close"]              == 47225.3


def test_esri_parse_skips_footnote_rows():
    text = _esri_csv([_ESRI_ROW_1994Q1, "＊年率で表示している。,,,,,,,,,,,"])
    result = _parse_esri_gdp_csv(text)
    assert len(result["PrivateConsumption"]) == 1


def test_esri_parse_missing_header_returns_empty():
    assert _parse_esri_gdp_csv("no header here\n1,2,3\n") == {}


def test_esri_apply_lag_shifts_trade_date():
    rows = [{"trade_date": "1994-04-01", "open": None, "high": None, "low": None,
             "close": 100.0, "volume": None}]
    shifted = _esri_apply_lag(rows, 60)
    assert shifted[0]["trade_date"] == "1994-05-31"
    assert _esri_apply_lag(rows, 0) == rows


def _fetch_esri_probe(url_status_map: dict) -> dict:
    """URL→ステータスコードのマップに基づき fetch_esri_gdp_csv のプロービングを検証する。
    200 を返すURLには常に _ESRI_ROW_1994Q1 一行のみを含むCSVを返す。"""
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            status = url_status_map.get(str(request.url), 404)
            if status == 200:
                body = _esri_csv([_ESRI_ROW_1994Q1]).encode("cp932")
                return httpx.Response(200, content=body)
            return httpx.Response(status)
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_esri_gdp_csv(s)
    return asyncio.run(run())


def test_esri_probing_falls_back_to_first_200():
    """先頭2候補が404、3番目が200になるケース→3番目の内容が採用される。"""
    urls = _esri_candidate_urls(date.today())
    status_map = {urls[0]: 404, urls[1]: 404, urls[2]: 200}
    result = _fetch_esri_probe(status_map)
    assert result["PrivateConsumption"][0]["close"] == 252337.9


def test_esri_probing_all_fail_returns_empty():
    """全候補404の場合はログ警告のみで空dictを返し、例外を送出しない。"""
    result = _fetch_esri_probe({})
    assert result == {}


# ── 9. IMF WEO 見通しフェッチャー（forward-looking・#284）───────────────────
# 実API検証済み（2026-07-11）: api.imf.org/external/sdmx/3.0 は匿名アクセス可・APIキー
# 不要。①バックフィルは IMF 公式 WEOhistorical.xlsx（point-in-time パネル）から翌年
# 予測値を vintage ごとに抽出。②継続収集は現行dataflowから「収集日時点で分かっている
# 翌年予測値」を trade_date=収集日 で1点取得（先読みバイアスなし・現行dataflowは公式
# vintage境界と無関係に随時改定されるため過去日付には割り当てない）。
import openpyxl

from collector import (
    fetch_imf_weo_historical, fetch_imf_weo_current, _parse_imf_weo_sheet,
    IMF_SERIES,
)


def _build_weo_workbook() -> bytes:
    """WEOhistorical.xlsx を模した最小ワークブック（ngdp_rpch シートのみ）。
    vintage列は S2020/F2020（対象年=2021行から翌年予測を抽出）と S2021（対象年=2022行が
    "."＝欠測のため0件になることを確認する用）のみを持たせる。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ngdp_rpch"
    ws.append(["country", "WEO_Country_Code", "ISOAlpha_3Code", "year",
               "S2020ngdp_rpch", "F2020ngdp_rpch", "S2021ngdp_rpch"])
    ws.append(["Japan", "158", "JPN", "2020", "0.5", "0.6", "."])
    ws.append(["Japan", "158", "JPN", "2021", "1.234", "1.345", "2.222"])
    ws.append(["Japan", "158", "JPN", "2022", "0.1", "0.2", "."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_imf_weo_series_registered():
    by_code = {s["code"]: s for s in IMF_SERIES}
    assert by_code["JP_WEO_GDP_FCAST"]["indicator"]    == "NGDP_RPCH"
    assert by_code["JP_WEO_GDP_FCAST"]["excel_column"] == "ngdp_rpch"
    assert by_code["JP_WEO_CPI_FCAST"]["indicator"]    == "PCPIPCH"
    assert by_code["JP_WEO_CPI_FCAST"]["excel_column"] == "pcpi_pch"
    for s in IMF_SERIES:
        assert s["category"]  == "forecast"
        assert s["lag_days"]  == 45


def test_parse_imf_weo_sheet_extracts_next_year_forecast():
    """vintage S2020/F2020 の値は「対象年=2021行」から読み取る（当年ではなく翌年）。
    vintage S2021 は対象年=2022行の値が "."（欠測）のため0件（F2021列は存在しないため
    そもそも対象外）。"""
    wb = openpyxl.load_workbook(io.BytesIO(_build_weo_workbook()), read_only=True)
    rows = _parse_imf_weo_sheet(wb, "ngdp_rpch", lag_days=45)
    wb.close()
    assert len(rows) == 2
    by_date = {r["trade_date"]: r["close"] for r in rows}
    assert by_date[(date(2020, 4, 1) + timedelta(days=45)).isoformat()] == 1.234
    assert by_date[(date(2020, 10, 1) + timedelta(days=45)).isoformat()] == 1.345


def test_fetch_imf_weo_historical_range_header_workaround():
    """IMFサーバーは素のGETを403で拒否するがRangeヘッダー付きなら200/206で応答する
    （bot対策の実装差・2026-07-11実API検証済み）。fetch側がRangeヘッダーを付けて
    リクエストすることを確認する。pcpi_pch シートは存在しないワークブックのため
    CPI 側は空リストで返る。"""
    import asyncio
    captured = {}
    xlsx_bytes = _build_weo_workbook()

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            captured["range"] = request.headers.get("range")
            return httpx.Response(200, content=xlsx_bytes)
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_imf_weo_historical(s)
    result = asyncio.run(run())
    assert captured["range"] is not None
    assert len(result["JP_WEO_GDP_FCAST"]) == 2
    assert result.get("JP_WEO_CPI_FCAST", []) == []


def test_fetch_imf_weo_historical_http_error_returns_empty():
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="Forbidden")
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_imf_weo_historical(s)
    result = asyncio.run(run())
    assert result == {}


def _sdmx_current_json(time_periods: list, values: list) -> dict:
    """fetch_imf_weo_current が解析する IMF SDMX 3.0 data-json 形式（簡略版）。"""
    return {
        "data": {
            "structures": [{
                "dimensions": {"observation": [
                    {"id": "TIME_PERIOD", "values": [{"value": t} for t in time_periods]},
                ]},
            }],
            "dataSets": [{
                "series": {
                    "0:0:0": {"observations": {str(i): [v] for i, v in enumerate(values)}},
                },
            }],
        },
    }


def test_fetch_imf_weo_current_extracts_next_calendar_year():
    """trade_date は収集日そのもの（先読みバイアスなし）。対象は今年+1年の値。"""
    import asyncio
    today = date.today()
    time_periods = [str(today.year - 1), str(today.year), str(today.year + 1)]
    values = ["1.0", "2.0", "3.456"]

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_sdmx_current_json(time_periods, values))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_imf_weo_current(s, "NGDP_RPCH")
    rows = asyncio.run(run())
    assert len(rows) == 1
    assert rows[0]["trade_date"] == today.isoformat()
    assert rows[0]["close"] == 3.456


def test_fetch_imf_weo_current_missing_target_year_returns_empty():
    import asyncio
    today = date.today()

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_sdmx_current_json([str(today.year)], ["2.0"]))
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_imf_weo_current(s, "NGDP_RPCH")
    rows = asyncio.run(run())
    assert rows == []


def test_fetch_imf_weo_current_api_error_returns_empty():
    import asyncio

    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")
        async with _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)) as s:
            return await fetch_imf_weo_current(s, "NGDP_RPCH")
    rows = asyncio.run(run())
    assert rows == []
