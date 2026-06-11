"""update_industry_from_jpx のユニットテスト (#78)。

HTTP 呼び出しをモックし、Company/FinancialRecord の業種が
バルク UPDATE で正しく更新されることを検証する。
"""
import asyncio
import io
import os
import sys

import httpx
import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import update_industry_from_jpx


def _make_jpx_xlsx(rows: list) -> bytes:
    """テスト用 JPX 業種マスタ xlsx を生成（col1=sec_code, col5=industry）"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["col0", "sec_code", "col2", "col3", "col4", "industry_name"])
    for sec, ind in rows:
        ws.append(["", sec, "", "", "", ind])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _mock_client(content: bytes) -> httpx.AsyncClient:
    def handler(request):
        return httpx.Response(200, content=content)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestUpdateIndustryFromJpx:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_updates_company_industry(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=""))
        db.commit()
        content = _make_jpx_xlsx([("1001", "情報・通信業")])
        client = _mock_client(content)
        co_updated, fr_updated = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 1
        from database import Company
        co = db.query(Company).filter_by(edinet_code="E00001").first()
        assert co.industry == "情報・通信業"

    def test_updates_financial_record_industry(self, db, make_company, make_fin):
        db.add(make_company(edinet_code="E00001", sec_code="2001"))
        db.add(make_fin(edinet_code="E00001", sec_code="2001", industry="旧業種"))
        db.commit()
        content = _make_jpx_xlsx([("2001", "小売業")])
        client = _mock_client(content)
        _, fr_updated = self._run(update_industry_from_jpx(client, db))
        assert fr_updated == 1
        from database import FinancialRecord
        fr = db.query(FinancialRecord).filter_by(edinet_code="E00001").first()
        assert fr.industry == "小売業"

    def test_no_change_when_industry_already_correct(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.query(type(make_company())).filter_by(edinet_code="E00001").update({"industry": "情報・通信業"})
        db.commit()
        content = _make_jpx_xlsx([("1001", "情報・通信業")])
        client = _mock_client(content)
        co_updated, _ = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 0

    def test_zero_padded_sec_code_matches(self, db, make_company):
        # DB に "0101"、JPX マップは "0101" → ゼロ埋め形式での一致
        db.add(make_company(edinet_code="E00001", sec_code="0101"))
        db.commit()
        content = _make_jpx_xlsx([("0101", "建設業")])
        client = _mock_client(content)
        co_updated, _ = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 1

    def test_http_error_returns_zeros(self, db):
        def handler(request):
            return httpx.Response(500)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        co_updated, fr_updated = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 0
        assert fr_updated == 0
