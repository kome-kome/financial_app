"""run_full_collection / reparse_from_raw のユニットテスト (#75)。

外部 API（EDINET）と asyncio.sleep をモックし DB 更新動作を検証する。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import reparse_from_raw, run_full_collection
from database import FinancialRecord, XbrlRawDocument
from database import pack_elements


# ── ヘルパー ──────────────────────────────────────────────────────────────

def _company_df():
    """fetch_edinet_code_list が返す形式の DataFrame"""
    return pd.DataFrame([{
        "edinet_code":  "E00001",
        "sec_code":     "1001",
        "company_name": "テスト株式会社",
        "industry":     "情報・通信業",
        "fiscal_month": "3",
    }])


def _doc_list():
    """collect_doc_ids_for_period が返す形式のリスト（書類1件）"""
    return [{
        "docID":      "S100TEST",
        "edinetCode": "E00001",
        "secCode":    "1001",
        "periodEnd":  "2023-03-31",
        "filerName":  "テスト株式会社",
    }]


def _xbrl_df():
    return pd.DataFrame([{"element": "dummy"}])


def _parsed_financial():
    """parse_xbrl_csv が返す財務データ（bs に値あり → スキップされない）"""
    return {
        "bs": {"total_assets": 1_000_000_000.0},
        "pl": {},
        "cf": {},
        "val": {},
        "nonfin": {},
        "meta": {},
    }


# ── run_full_collection (#75) ─────────────────────────────────────────────

class TestRunFullCollection:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_normal_case_inserts_financial_record(self, db):
        """正常系: 企業1社・書類1件でFinancialRecordが作成される"""
        with (
            patch("collector_financials.fetch_edinet_code_list",
                  new=AsyncMock(return_value=_company_df())),
            patch("collector_financials.collect_doc_ids_for_period",
                  new=AsyncMock(return_value=_doc_list())),
            patch("collector_financials.fetch_xbrl_csv",
                  new=AsyncMock(return_value=_xbrl_df())),
            patch("collector_financials.parse_xbrl_csv", return_value=_parsed_financial()),
            patch("collector_financials.update_industry_from_jpx",
                  new=AsyncMock(return_value=(0, 0))),
            patch("collector.asyncio.sleep", new=AsyncMock()),
        ):
            cancelled = self._run(run_full_collection(db, years_back=1))

        assert cancelled is False
        rec = db.query(FinancialRecord).filter_by(
            edinet_code="E00001", doc_id="S100TEST"
        ).first()
        assert rec is not None
        assert rec.period_end == "2023-03-31"
        assert rec.bs_total_assets == 1_000_000_000.0

    def test_error_during_processing_skips_and_rolls_back(self, db):
        """書類処理中に例外が発生した場合、ロールバックしてスキップする"""
        with (
            patch("collector_financials.fetch_edinet_code_list",
                  new=AsyncMock(return_value=_company_df())),
            patch("collector_financials.collect_doc_ids_for_period",
                  new=AsyncMock(return_value=_doc_list())),
            patch("collector_financials.fetch_xbrl_csv",
                  new=AsyncMock(side_effect=RuntimeError("EDINET 障害テスト"))),
            patch("collector_financials.update_industry_from_jpx",
                  new=AsyncMock(return_value=(0, 0))),
            patch("collector.asyncio.sleep", new=AsyncMock()),
        ):
            cancelled = self._run(run_full_collection(db, years_back=1))

        assert cancelled is False
        # 書類処理は失敗したが関数全体は続行する
        count = db.query(FinancialRecord).count()
        assert count == 0

    def test_skip_existing_skips_already_collected_doc(self, db, make_fin):
        """skip_existing=True: 収集済み doc_id の書類をスキップする"""
        rec = make_fin(doc_id="S100TEST", edinet_code="E00001",
                       year=2023, period_end="2023-03-31",
                       bs_total_assets=999.0)
        db.add(rec)
        db.commit()

        with (
            patch("collector_financials.fetch_edinet_code_list",
                  new=AsyncMock(return_value=_company_df())),
            patch("collector_financials.collect_doc_ids_for_period",
                  new=AsyncMock(return_value=_doc_list())),
            patch("collector_financials.fetch_xbrl_csv",
                  new=AsyncMock(return_value=_xbrl_df())) as mock_fetch,
            patch("collector_financials.parse_xbrl_csv", return_value=_parsed_financial()),
            patch("collector_financials.update_industry_from_jpx",
                  new=AsyncMock(return_value=(0, 0))),
            patch("collector.asyncio.sleep", new=AsyncMock()),
        ):
            self._run(run_full_collection(db, years_back=1, skip_existing=True))

        # fetch_xbrl_csv は呼ばれない（スキップ）
        mock_fetch.assert_not_called()
        # 既存レコードは変更されない
        db.refresh(rec)
        assert rec.bs_total_assets == 999.0


# ── reparse_from_raw (#75) ────────────────────────────────────────────────

class _NoCloseSession:
    """テスト用: close() を無効化して同一セッションを使い回す"""
    def __init__(self, real_db):
        self._db = real_db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


class TestReparseFromRaw:
    def _run(self, coro):
        return asyncio.run(coro)

    def _make_raw_doc(self, db, edinet_code="E00001",
                      doc_id="S100TEST", period_end="2023-03-31"):
        """XbrlRawDocument を DB に挿入して返す"""
        raw_rows = [{"element": "Assets", "context": "Prior2YearInstant_NonConsolidatedMember", "value": "1000000000"}]
        doc = XbrlRawDocument(
            doc_id=doc_id,
            edinet_code=edinet_code,
            period_end=period_end,
            elements_gz=pack_elements(raw_rows),
            n_rows=len(raw_rows),
        )
        db.add(doc)
        db.commit()
        return doc

    def test_recovers_financial_record_from_raw(self, db, make_company):
        """正常系: XbrlRawDocument から FinancialRecord を再構築できる"""
        company = make_company(edinet_code="E00001", sec_code="1001")
        db.add(company)
        self._make_raw_doc(db)

        proxy = _NoCloseSession(db)

        with (
            patch("collector_financials.SessionLocal", return_value=proxy),
            patch("collector_financials.parse_raw_rows", return_value=_parsed_financial()),
            patch("collector.asyncio.sleep", new=AsyncMock()),
        ):
            cancelled = self._run(reparse_from_raw(edinet_code="E00001"))

        assert cancelled is False
        rec = db.query(FinancialRecord).filter_by(
            edinet_code="E00001", doc_id="S100TEST"
        ).first()
        assert rec is not None
        assert rec.period_end == "2023-03-31"
        assert rec.bs_total_assets == 1_000_000_000.0

    def test_cancel_check_stops_processing(self, db, make_company):
        """cancel_check=True: 最初のドキュメントでキャンセルして True を返す"""
        company = make_company(edinet_code="E00001", sec_code="1001")
        db.add(company)
        self._make_raw_doc(db)

        proxy = _NoCloseSession(db)

        with (
            patch("collector_financials.SessionLocal", return_value=proxy),
            patch("collector_financials.parse_raw_rows", return_value=_parsed_financial()),
        ):
            cancelled = self._run(
                reparse_from_raw(edinet_code="E00001",
                                 cancel_check=lambda: True)
            )

        assert cancelled is True
        # キャンセル前に処理されていないため FinancialRecord は作成されない
        count = db.query(FinancialRecord).filter_by(edinet_code="E00001").count()
        assert count == 0

    def test_year_filter_limits_scope(self, db, make_company):
        """year フィルタ: 対象年の書類のみ処理される"""
        company = make_company(edinet_code="E00001", sec_code="1001")
        db.add(company)
        self._make_raw_doc(db, doc_id="S100_2023", period_end="2023-03-31")
        self._make_raw_doc(db, doc_id="S100_2022", period_end="2022-03-31")

        proxy = _NoCloseSession(db)

        with (
            patch("collector_financials.SessionLocal", return_value=proxy),
            patch("collector_financials.parse_raw_rows", return_value=_parsed_financial()),
            patch("collector.asyncio.sleep", new=AsyncMock()),
        ):
            self._run(reparse_from_raw(year=2023))

        records = db.query(FinancialRecord).filter_by(edinet_code="E00001").all()
        assert len(records) == 1
        assert records[0].doc_id == "S100_2023"
