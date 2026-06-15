"""collect_doc_ids_for_period の max_companies「先着N社」不変条件の回帰テスト (#153)。

CLAUDE.md の設計制約:
  「collect_doc_ids_for_period の max_companies は全期間スキャン後に先着 N 社へ絞る
   （早期終了禁止）」

test_collection.py ではこの関数自体を patch でモック化しているため、不変条件そのもの
（先着 N 社への絞り込み・早期終了しないこと）は未検証だった。本テストは関数を実体のまま
呼び、fetch_doc_list と asyncio.sleep のみモックして不変条件を直接検証する。
"""
import asyncio
import os
import sys
from datetime import date
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
from collector import collect_doc_ids_for_period


def _doc(ec: str, doc_id: str) -> dict:
    """fetch_doc_list が返す書類1件の形式。"""
    return {
        "docID":      doc_id,
        "edinetCode": ec,
        "secCode":    "0000",
        "periodEnd":  "2024-03-31",
        "filerName":  ec,
    }


class TestCollectDocIdsMaxCompanies:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_narrows_to_first_n_after_full_scan(self, monkeypatch):
        """max_companies=N 指定時、全期間スキャン後に先着 N 社へ絞り込まれる。

        - 初日に E001/E002 を発見、後日に E003/E004 を発見
        - N=2 のため最終結果は E001/E002 の書類のみ（E003/E004 は除外）
        - 初日発見の E001 が後日に再登場した書類も保持される
        """
        start = date(2024, 1, 1)
        end = date(2024, 1, 3)  # 3 日間

        by_date = {
            date(2024, 1, 1): [_doc("E001", "D1"), _doc("E002", "D2")],
            date(2024, 1, 2): [_doc("E003", "D3")],
            date(2024, 1, 3): [_doc("E004", "D4"), _doc("E001", "D5")],
        }
        calls: list = []

        async def fake_fetch(client, target_date):
            calls.append(target_date)
            return by_date[target_date]

        monkeypatch.setattr(collector, "fetch_doc_list", fake_fetch)
        # RATE_SLEEP の実待機を無効化
        monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock())

        docs = self._run(collect_doc_ids_for_period(
            client=None, start=start, end=end, max_companies=2))

        ecs = {d["edinetCode"] for d in docs}
        assert ecs == {"E001", "E002"}              # 先着2社のみ残る
        assert "E003" not in ecs and "E004" not in ecs  # N社目以降は除外

        # 先着社の書類は全期間ぶん保持される（初日 D1/D2 + 後日の再登場 D5）
        assert {d["docID"] for d in docs} == {"D1", "D2", "D5"}

    def test_full_period_scanned_no_early_exit(self, monkeypatch):
        """N 社に達してもスキャンを早期終了せず、全期間の一覧取得が呼ばれる。

        初日で N=1 社に到達するが、3 日ぶんすべて fetch_doc_list が呼ばれることを
        呼び出し回数・対象日で担保する。
        """
        start = date(2024, 1, 1)
        end = date(2024, 1, 3)  # 3 日間

        by_date = {
            date(2024, 1, 1): [_doc("E001", "D1")],   # 初日で先着上限(N=1)に到達
            date(2024, 1, 2): [_doc("E002", "D2")],
            date(2024, 1, 3): [_doc("E003", "D3")],
        }
        calls: list = []

        async def fake_fetch(client, target_date):
            calls.append(target_date)
            return by_date[target_date]

        monkeypatch.setattr(collector, "fetch_doc_list", fake_fetch)
        monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock())

        docs = self._run(collect_doc_ids_for_period(
            client=None, start=start, end=end, max_companies=1))

        # 全 3 日が順にスキャンされている（早期終了していない）
        assert calls == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
        # 結果は先着1社のみ
        assert {d["edinetCode"] for d in docs} == {"E001"}

    def test_no_limit_returns_all(self, monkeypatch):
        """max_companies 未指定時は全社の書類がそのまま返る。"""
        start = date(2024, 1, 1)
        end = date(2024, 1, 2)

        by_date = {
            date(2024, 1, 1): [_doc("E001", "D1")],
            date(2024, 1, 2): [_doc("E002", "D2"), _doc("E003", "D3")],
        }

        async def fake_fetch(client, target_date):
            return by_date[target_date]

        monkeypatch.setattr(collector, "fetch_doc_list", fake_fetch)
        monkeypatch.setattr(collector.asyncio, "sleep", AsyncMock())

        docs = self._run(collect_doc_ids_for_period(
            client=None, start=start, end=end))

        assert {d["edinetCode"] for d in docs} == {"E001", "E002", "E003"}
        assert {d["docID"] for d in docs} == {"D1", "D2", "D3"}
