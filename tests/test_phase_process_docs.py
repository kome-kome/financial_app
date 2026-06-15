"""_phase_process_docs のフェイルソフト挙動テスト (#140)。

XBRL 取得→パース→DB 保存ループで、1社の失敗（fetch/parse/DB）が起きても
収集全体を止めず個社スキップで継続すること、rollback の二次例外でも
ループが止まらないことを検証する。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import _phase_process_docs


def _doc(doc_id, edinet, sec="1001", period="2023-03-31", name="テスト"):
    return {"docID": doc_id, "edinetCode": edinet, "secCode": sec,
            "periodEnd": period, "filerName": name}


def _run(docs, **patches):
    """known_edinet を事前投入（master フェーズの DB 書き込みを回避）して実行する。"""
    db = MagicMock()
    known = {"E00001", "E00002"}
    with patch("collector_financials.RATE_SLEEP", 0), patch("collector_financials.BATCH_PAUSE", 0):
        return asyncio.run(_phase_process_docs(
            db, client=MagicMock(), all_docs=docs,
            company_info={}, known_edinet=known,
            existing_doc_ids=set(), skip_existing=False,
            on_progress=None, cancel_check=None,
        )), db


def _good_parsed():
    return {"bs": {"assets": 1.0}, "pl": {}, "cf": {}}


class TestPhaseProcessDocsFailSoft:
    def test_parse_failure_skips_company_and_continues(self):
        docs = [_doc("S1", "E00001"), _doc("S2", "E00002")]

        def parse_side(df, ec, pe):
            if ec == "E00001":
                raise ValueError("broken XBRL")
            return _good_parsed()

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value="df")),
            patch("collector_financials.parse_xbrl_csv", side_effect=parse_side),
            patch("collector_financials.calc_derived", return_value={"meta": {}}),
            patch("collector_financials.upsert_financial") as upsert,
        ):
            result, _db = _run(docs)

        skipped, cancelled = result
        assert cancelled is False
        # 失敗した E00001 はスキップ、E00002 のみ保存される
        assert upsert.call_count == 1

    def test_db_failure_does_not_stop_loop(self):
        docs = [_doc("S1", "E00001"), _doc("S2", "E00002")]

        def upsert_side(db, rec):
            if rec["edinet_code"] == "E00001":
                raise OperationalError("stmt", {}, Exception("db down"))

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value="df")),
            patch("collector_financials.parse_xbrl_csv", return_value=_good_parsed()),
            patch("collector_financials.calc_derived", return_value={"meta": {}}),
            patch("collector_financials.upsert_financial", side_effect=upsert_side) as upsert,
        ):
            result, db = _run(docs)

        _, cancelled = result
        assert cancelled is False
        assert upsert.call_count == 2          # 両社とも試行（1社失敗でも継続）
        assert db.rollback.called               # 失敗時に rollback

    def test_rollback_secondary_exception_is_swallowed(self):
        docs = [_doc("S1", "E00001"), _doc("S2", "E00002")]

        def parse_side(df, ec, pe):
            raise ValueError("always fails")

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value="df")),
            patch("collector_financials.parse_xbrl_csv", side_effect=parse_side),
            patch("collector_financials.upsert_financial"),
        ):
            db = MagicMock()
            db.rollback.side_effect = Exception("rollback boom")
            known = {"E00001", "E00002"}
            with patch("collector_financials.RATE_SLEEP", 0), patch("collector_financials.BATCH_PAUSE", 0):
                # 二次例外が送出されずに完走すれば成功
                result = asyncio.run(_phase_process_docs(
                    db, client=MagicMock(), all_docs=docs,
                    company_info={}, known_edinet=known,
                    existing_doc_ids=set(), skip_existing=False,
                    on_progress=None, cancel_check=None,
                ))

        assert result == (0, False)
        assert db.rollback.call_count == 2      # 各社で rollback 試行
