"""collect_doc_ids_for_period の max_companies「先着N社」不変条件の回帰テスト (#153)。

CLAUDE.md の設計制約「max_companies は全期間スキャン後に先着 N 社へ絞る（早期終了禁止）」を、
fetch_doc_list（書類一覧取得）だけをモックし、collect_doc_ids_for_period を実体で呼んで検証する。
"""
import asyncio
import os
import sys
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import collect_doc_ids_for_period


def _doc(ec: str, doc_id: str) -> dict:
    """fetch_doc_list が返す書類1件の形式（必要キーのみ）。"""
    return {"docID": doc_id, "edinetCode": ec, "secCode": "1234", "periodEnd": "2023-03-31"}


# 3 日間の書類一覧。発見順は E001, E002 (1日目) → E003, E004 (2日目) → E005 (3日目)。
# E002 は 3 日目に再出現させ、「先着 N 社に入った企業の後日書類も拾われる」ことを検証する。
START = date(2023, 1, 1)
END = date(2023, 1, 3)
DAILY = {
    date(2023, 1, 1): [_doc("E001", "D1"), _doc("E002", "D2")],
    date(2023, 1, 2): [_doc("E003", "D3"), _doc("E004", "D4")],
    date(2023, 1, 3): [_doc("E005", "D5"), _doc("E002", "D6")],
}
TOTAL_DAYS = (END - START).days + 1  # 3


def _run(**kwargs):
    """fetch_doc_list と asyncio.sleep をモックし collect_doc_ids_for_period を実体で実行する。"""
    async def fake_fetch(client, target_date):
        return list(DAILY.get(target_date, []))

    fetch_mock = AsyncMock(side_effect=fake_fetch)
    with patch("collector_financials.fetch_doc_list", new=fetch_mock), \
         patch("collector.asyncio.sleep", new=AsyncMock()):
        docs = asyncio.run(
            collect_doc_ids_for_period(client=None, start=START, end=END, **kwargs)
        )
    return docs, fetch_mock


def test_no_limit_returns_all_docs_and_scans_full_period():
    """max_companies 未指定なら全書類を返し、全期間（3日）をスキャンする。"""
    docs, fetch_mock = _run()
    assert fetch_mock.call_count == TOTAL_DAYS
    assert {d["docID"] for d in docs} == {"D1", "D2", "D3", "D4", "D5", "D6"}


def test_max_companies_limits_to_first_n():
    """max_companies=3 指定時、結果が先着 3 社（E001/E002/E003）の書類に絞られる。"""
    docs, _ = _run(max_companies=3)
    companies = {d["edinetCode"] for d in docs}
    assert companies == {"E001", "E002", "E003"}
    # 4 社目以降（E004/E005）は除外される。
    assert "E004" not in companies
    assert "E005" not in companies


def test_max_companies_does_not_early_terminate_scan():
    """3 社目が 2 日目で揃っても、3 日目まで全期間スキャンする（早期終了禁止）。"""
    docs, fetch_mock = _run(max_companies=3)
    # 全期間ぶんの一覧取得が呼ばれている。
    assert fetch_mock.call_count == TOTAL_DAYS
    # 3 日目に再出現した先着内企業（E002, docID=D6）の書類も結果に含まれる。
    # → N 社到達時点で打ち切らず全期間スキャンした証拠。
    assert "D6" in {d["docID"] for d in docs}


def test_max_companies_larger_than_seen_returns_all():
    """max_companies が発見社数以上なら絞り込みは発生しない。"""
    docs, fetch_mock = _run(max_companies=100)
    assert fetch_mock.call_count == TOTAL_DAYS
    assert {d["docID"] for d in docs} == {"D1", "D2", "D3", "D4", "D5", "D6"}
