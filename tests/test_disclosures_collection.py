"""会社予想開示収集（collector_disclosures.py）のユニットテスト。

HTTP 通信（_jquants_fetch_summary_date）はモックし、DB 書き込みは in-memory
SQLite（conftest.py の db fixture）に対して実際に行う（upsert_statement_disclosures
のロジックそのものを検証するため）。
"""
import asyncio
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from collector_disclosures import (
    _num, _row_to_record, collect_statement_disclosures,
)
from database import StatementDisclosure


def _sample_row(**overrides):
    row = {
        "DiscNo": "20240424575411",
        "Code": "72030",
        "DiscDate": "2024-05-08",
        "DiscTime": "13:55:00",
        "DocType": "FYFinancialStatements_Consolidated_IFRS",
        "CurPerType": "FY",
        "CurPerSt": "2023-04-01",
        "CurPerEn": "2024-03-31",
        "CurFYSt": "2023-04-01",
        "CurFYEn": "2024-03-31",
        "NxtFYSt": "2024-04-01",
        "NxtFYEn": "2025-03-31",
        "Sales": "45095325000000",
        "OP": "5352934000000",
        "OdP": "",
        "NP": "4944933000000",
        "EPS": "365.94",
        "DEPS": "365.94",
        "DivAnn": "75.0",
        "FSales": "",
        "FOP": "",
        "FOdP": "",
        "FNP": "",
        "FEPS": "",
        "FDivAnn": "",
        "NxFSales": "46000000000000",
        "NxFOP": "4300000000000",
        "NxFOdP": "",
        "NxFNp": "3570000000000",
        "NxFEPS": "264.95",
    }
    row.update(overrides)
    return row


class TestNum:
    def test_parses_numeric_string(self):
        assert _num("365.94") == 365.94

    def test_empty_string_is_none(self):
        assert _num("") is None

    def test_none_is_none(self):
        assert _num(None) is None

    def test_non_numeric_is_none(self):
        assert _num("N/A") is None


class TestRowToRecord:
    def test_maps_fields_and_derives_edinet_code(self):
        sec_to_edinet = {"7203": "E00001"}
        rec = _row_to_record(_sample_row(), sec_to_edinet)
        assert rec["edinet_code"] == "E00001"
        assert rec["sec_code"] == "7203"
        assert rec["disc_no"] == "20240424575411"
        assert rec["disc_date"] == "2024-05-08"
        assert rec["sales"] == 45095325000000.0
        assert rec["odp"] is None   # IFRS採用企業は経常利益概念なし
        assert rec["nxf_np"] == 3570000000000.0   # J-Quants側の実フィールド名 "NxFNp"

    def test_unknown_code_returns_none(self):
        rec = _row_to_record(_sample_row(), sec_to_edinet={})
        assert rec is None

    def test_missing_disc_date_returns_none(self):
        rec = _row_to_record(_sample_row(DiscDate=""), sec_to_edinet={"7203": "E00001"})
        assert rec is None


def _collect_with_mock(db, fetch_return, **kwargs):
    with patch("collector_disclosures._jquants_fetch_summary_date",
               new_callable=AsyncMock, return_value=fetch_return):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
            with patch("collector_disclosures.JQUANTS_RATE_SLEEP", 0):
                return asyncio.run(collect_statement_disclosures(db, **kwargs))


class TestCollectStatementDisclosures:
    def test_missing_api_key_raises(self, db):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": ""}):
            with pytest.raises(ValueError):
                asyncio.run(collect_statement_disclosures(db))

    def test_backfills_from_scratch_and_persists_row(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="7203", name="トヨタ自動車"))
        db.commit()

        d_from = date(2024, 5, 6)
        d_to = date(2024, 5, 8)
        result = _collect_with_mock(
            db, fetch_return=[_sample_row()],
            date_from=d_from, date_to=d_to,
        )

        assert result["cancelled"] is False
        assert result["upserted"] > 0
        saved = db.query(StatementDisclosure).filter_by(disc_no="20240424575411").one()
        assert saved.edinet_code == "E00001"
        assert saved.sales == 45095325000000.0

    def test_upsert_is_idempotent_on_disc_no(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="7203", name="トヨタ自動車"))
        db.commit()

        d_from = d_to = date(2024, 5, 8)
        _collect_with_mock(db, fetch_return=[_sample_row()], date_from=d_from, date_to=d_to)
        _collect_with_mock(db, fetch_return=[_sample_row(EPS="999.99")], date_from=d_from, date_to=d_to)

        rows = db.query(StatementDisclosure).filter_by(disc_no="20240424575411").all()
        assert len(rows) == 1
        assert rows[0].eps == 999.99   # 2回目のフェッチ内容で上書きされている

    def test_no_op_when_date_range_already_covers_delay_boundary(self, db):
        """date_from > 有効な date_to（配信遅延境界でクランプ後）なら収集をスキップする。"""
        today = date.today()
        result = _collect_with_mock(
            db, fetch_return=[],
            date_from=today, date_to=today,
        )
        assert result == {"cancelled": False, "upserted": 0, "days": 0}

    def test_cancel_check_stops_collection(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="7203", name="トヨタ自動車"))
        db.commit()
        result = _collect_with_mock(
            db, fetch_return=[_sample_row()],
            date_from=date(2024, 5, 6), date_to=date(2024, 5, 8),
            cancel_check=lambda: True,
        )
        assert result["cancelled"] is True
        assert result["upserted"] == 0

    def test_incremental_resumes_from_latest_disc_date(self, db, make_company):
        """date_from 省略時は DB 内の最新 disc_date から再開する。"""
        db.add(make_company(edinet_code="E00001", sec_code="7203", name="トヨタ自動車"))
        db.commit()
        # 既存の最新開示日を 2024-05-08 にしておく
        _collect_with_mock(db, fetch_return=[_sample_row()],
                            date_from=date(2024, 5, 8), date_to=date(2024, 5, 8))

        fetch_calls = []

        async def _mock_fetch(session, api_key, date_str):
            fetch_calls.append(date_str)
            return []

        with patch("collector_disclosures._jquants_fetch_summary_date", new=_mock_fetch):
            with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                with patch("collector_disclosures.JQUANTS_RATE_SLEEP", 0):
                    asyncio.run(collect_statement_disclosures(
                        db, date_to=date(2024, 5, 10)
                    ))

        assert fetch_calls[0] == "2024-05-08"
        assert fetch_calls[-1] == "2024-05-10"
