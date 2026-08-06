"""tests/test_collect_mof_jgb.py — 財務省「国債金利情報」CSV コネクタ（Issue #458）

M-3 の `dlm_jp10y` は月次 FRED を週次差分へ落としていたため **76.89% がゼロ**だった
（#456 実測）。財務省が日次で直配信している CSV へ差し替えて ADR-0012 の唯一の例外を消す。

ここで固定するのは主にパースの前提（実 CSV で確認済み）:
  - 和暦（`R8.8.4` → 2026-08-04）
  - ヘッダ2行・末尾の空行と注意書き行・欠測 `-`
  - 10年物は列 index 10
実 HTTP を叩くテストは置かない（CI をネットワークに依存させない）。
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collector_prices import (
    MOF_ERA_BASE,
    MOF_SERIES,
    fetch_mof_jgb_csv,
    parse_mof_jgb_csv,
)

# 実物と同じ構造（タイトル行＋列名行＋データ＋空行＋注意書き行）。
_CSV = (
    "国債金利情報 (令和8年8月),,,\r\n"
    "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年\r\n"
    "S61.7.5,-,-,-,-,-,-,-,-,-,-,-\r\n"          # 10年物が欠測の古い行
    "H31.4.26,-0.153,-0.160,-0.170,-0.165,-0.160,-0.140,-0.120,-0.090,-0.060,-0.040,0.230\r\n"
    "R8.8.4,0.700,0.900,1.100,1.300,1.500,1.700,1.900,2.100,2.400,2.848,3.500\r\n"
    "R8.8.5,0.700,0.900,1.100,1.300,1.500,1.700,1.900,2.100,2.400,2.813,3.500\r\n"
    "\r\n"
    "最新の csv データがダウンロードできない場合は、ブラウザのキャッシュを削除してください。\r\n"
)


class TestParse:
    def test_parses_wareki_and_skips_non_data_rows(self):
        rows = parse_mof_jgb_csv(_CSV, column=10)
        # S61.7.5 は 10年物が "-" で落ちる。タイトル/列名/空行/注意書きも落ちる。
        assert [r["trade_date"] for r in rows] == ["2019-04-26", "2026-08-04", "2026-08-05"]
        assert [r["close"] for r in rows] == [-0.040, 2.848, 2.813]

    def test_era_base_covers_all_four_eras(self):
        """元号の起点は「元年 = base + 1」。H31.4.26 → 1988+31 = 2019 で照合済み。"""
        assert MOF_ERA_BASE == {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}
        rows = parse_mof_jgb_csv(_CSV, column=10)
        assert date.fromisoformat(rows[0]["trade_date"]) == date(2019, 4, 26)

    def test_other_columns_are_selectable(self):
        """年限は列 index で選ぶ（10年物=10）。他年限も同じ関数で取れる。"""
        rows5 = parse_mof_jgb_csv(_CSV, column=5)     # 5年
        assert [r["close"] for r in rows5] == [-0.160, 1.500, 1.500]

    def test_negative_and_malformed_values(self):
        """マイナス金利は正常値。数値でないセルは警告してスキップ（全体は止めない）。"""
        broken = _CSV.replace("2.848", "N/A")
        rows = parse_mof_jgb_csv(broken, column=10)
        assert [r["trade_date"] for r in rows] == ["2019-04-26", "2026-08-05"]

    def test_ohlcv_shape_matches_upsert_contract(self):
        """upsert_macro_batch が要求するキーを満たす（close 以外は None 許容）。"""
        r = parse_mof_jgb_csv(_CSV, column=10)[0]
        assert set(r) == {"trade_date", "open", "high", "low", "close", "volume"}
        assert r["open"] is r["high"] is r["low"] is r["volume"] is None

    def test_empty_or_headers_only_returns_empty(self):
        assert parse_mof_jgb_csv("", column=10) == []
        assert parse_mof_jgb_csv("国債金利情報,,,\r\n基準日,1年\r\n", column=10) == []


class TestSeriesDefinition:
    def test_jp10y_mof_is_daily_with_lag(self):
        s = {x["code"]: x for x in MOF_SERIES}["JP10Y_MOF"]
        assert s["mof_column"] == 10
        assert s["category"] == "rate"
        assert s["freq"] == "daily"
        # 基準日 T の値は T+1 公表。日次系列は lag_days を実配信ラグより大きく取ると
        # trade_date が未来日になり、鮮度ゲートが ahead_days > 観測周期(1日) で CRITICAL に
        # するため、**安全側に大きく取れない**（#458 で lag_days=4 が today+2 を作った実測）。
        assert s["lag_days"] == 2

    def test_stale_days_is_not_declared(self):
        """`lag_days` が観測周期を超えない系列に個別 stale_days は置かない（ADR-0028）。"""
        s = {x["code"]: x for x in MOF_SERIES}["JP10Y_MOF"]
        assert "stale_days" not in s


class TestFetch:
    @staticmethod
    def _session(status=200, body=_CSV.encode("cp932")):
        resp = MagicMock()
        resp.status_code = status
        resp.content = body
        resp.raise_for_status = MagicMock(
            side_effect=None if status == 200 else Exception("boom"))
        session = MagicMock()
        session.get = AsyncMock(return_value=resp)
        return session

    def test_full_and_current_use_different_urls(self):
        from collector_prices import MOF_JGB_ALL_URL, MOF_JGB_CURRENT_URL
        s = self._session()
        asyncio.run(fetch_mof_jgb_csv(s, column=10, full=True))
        assert s.get.call_args[0][0] == MOF_JGB_ALL_URL
        asyncio.run(fetch_mof_jgb_csv(s, column=10, full=False))
        assert s.get.call_args[0][0] == MOF_JGB_CURRENT_URL

    def test_http_error_returns_empty_without_raising(self):
        """1系列の失敗で収集全体を落とさない（他コネクタと同じフェイルセーフ）。"""
        assert asyncio.run(fetch_mof_jgb_csv(self._session(status=503), 10, full=False)) == []

    def test_decodes_cp932(self):
        rows = asyncio.run(fetch_mof_jgb_csv(self._session(), column=10, full=True))
        assert [r["close"] for r in rows] == [-0.040, 2.848, 2.813]


class TestCollectIntegration:
    """collect_macro_data への組み込み（全期間版⇔当月版の切替・lag シフト）。"""

    def test_full_csv_when_db_has_no_rows_then_current_csv(self, db):
        from collector_prices import collect_macro_data
        from database import MacroData

        calls = []

        async def _fake_fetch(_session, column, full):
            calls.append(full)
            return parse_mof_jgb_csv(_CSV, column)

        with patch("collector_prices.fetch_mof_jgb_csv", side_effect=_fake_fetch):
            asyncio.run(collect_macro_data(db, years_back=1, only=["JP10Y_MOF"]))
            # 1回目: DB が空 → 全期間版。当月分は全期間版に無いので当月版も続けて取る。
            assert calls == [True, False]
            asyncio.run(collect_macro_data(db, years_back=1, only=["JP10Y_MOF"]))
            assert calls == [True, False, False]   # 2回目以降は当月版のみ

        rows = (db.query(MacroData)
                .filter(MacroData.series_code == "JP10Y_MOF")
                .order_by(MacroData.trade_date).all())
        # lag_days=2 で基準日から後ろへ寄る（2026-08-04 → 2026-08-06）
        assert [r.trade_date for r in rows] == ["2019-04-28", "2026-08-06", "2026-08-07"]
        assert rows[-1].close == 2.813
        assert rows[-1].category == "rate"
