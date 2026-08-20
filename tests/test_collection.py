"""run_full_collection / reparse_from_raw のユニットテスト (#75)。

外部 API（EDINET）と asyncio.sleep をモックし DB 更新動作を検証する。
"""
import asyncio
import os
import sys
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import reparse_from_raw, run_full_collection
from database import FinancialRecord, XbrlRawDocument, _parse_period_end
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
        assert rec.period_end == date(2023, 3, 31)
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
                      doc_id="S100TEST", period_end=date(2023, 3, 31)):
        """XbrlRawDocument を DB に挿入して返す"""
        raw_rows = [{"element": "Assets", "context": "Prior2YearInstant_NonConsolidatedMember", "value": "1000000000"}]
        doc = XbrlRawDocument(
            doc_id=doc_id,
            edinet_code=edinet_code,
            period_end=_parse_period_end(period_end),
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
        assert rec.period_end == date(2023, 3, 31)
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

    # ── #507: BLOB を件数ぶん一度に載せない ────────────────────────────────

    @staticmethod
    def _selects_by_table(db):
        """発行された SELECT を対象テーブルごとに数えるリスナを張る。"""
        from sqlalchemy import event

        counts = {}

        def before(conn, cursor, statement, params, context, executemany):
            s = " ".join(statement.split())
            if not s.upper().startswith("SELECT"):
                return
            for table in ("xbrl_raw_documents", "companies", "financial_records"):
                if f"FROM {table}" in s or f"from {table}" in s:
                    counts[table] = counts.get(table, 0) + 1

        event.listen(db.bind, "before_cursor_execute", before)
        return counts, lambda: event.remove(db.bind, "before_cursor_execute", before)

    def test_blobs_are_fetched_in_chunks_not_all_at_once(self, db, make_company):
        """**#507 の本旨**。5書類・チャンク2なら BLOB の取得は 1(id引き)+3(チャンク)。

        旧実装は `db.query(XbrlRawDocument).all()` の1文で全件を materialize しており、
        文の数だけ見ると「1回」で最も少ない。ここで縛りたいのは往復回数ではなく
        **1文が運ぶ BLOB の件数**なので、少ないほど良いのではなくチャンク数と一致することを見る。
        """
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        for k in range(5):
            # 年をずらす。同一 (edinet_code, year, period_end) だと FinancialRecord が
            # 1行へ upsert され、チャンク境界の取りこぼしを検知できない。
            self._make_raw_doc(db, doc_id=f"S100_{k}", period_end=date(2023 - k, 3, 31))

        proxy = _NoCloseSession(db)
        counts, detach = self._selects_by_table(db)
        try:
            with (
                patch("collector_financials.SessionLocal", return_value=proxy),
                patch("collector_financials.REPARSE_FETCH_BATCH", 2),
                patch("collector_financials.parse_raw_rows", return_value=_parsed_financial()),
                patch("collector.asyncio.sleep", new=AsyncMock()),
            ):
                self._run(reparse_from_raw())
        finally:
            detach()

        assert counts.get("xbrl_raw_documents") == 4, (
            f"id引き1 + チャンク3 を期待したが {counts.get('xbrl_raw_documents')} 文"
        )
        assert db.query(FinancialRecord).count() == 5, "チャンク境界で取りこぼしている"

    def test_company_lookup_does_not_scale_with_documents(self, db, make_company):
        """会社属性の SELECT が書類数に比例しないこと（旧実装は1書類1文・#506 と同型）。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        for k in range(5):
            # 年をずらす。同一 (edinet_code, year, period_end) だと FinancialRecord が
            # 1行へ upsert され、チャンク境界の取りこぼしを検知できない。
            self._make_raw_doc(db, doc_id=f"S100_{k}", period_end=date(2023 - k, 3, 31))

        proxy = _NoCloseSession(db)
        counts, detach = self._selects_by_table(db)
        try:
            with (
                patch("collector_financials.SessionLocal", return_value=proxy),
                patch("collector_financials.REPARSE_FETCH_BATCH", 2),
                patch("collector_financials.parse_raw_rows", return_value=_parsed_financial()),
                patch("collector.asyncio.sleep", new=AsyncMock()),
            ):
                self._run(reparse_from_raw())
        finally:
            detach()

        assert counts.get("companies") == 1, (
            f"5書類に対して companies を {counts.get('companies')} 回引いている"
        )

    def test_identity_map_does_not_hold_processed_blobs(self, db, make_company):
        """チャンクを進めたら前のチャンクの BLOB を Session が掴んでいないこと。

        **このテストは `expunge_all()` の有無では落ちない**（実測で確認した）。identity map は
        weak ref で、doc を書き換えていない限り `by_id` の再代入だけで解放されるため。
        ここで縛っているのは「処理済みの BLOB を strong ref で抱える経路が生えていないこと」で、
        `expunge_all()` はその保険。doc を dirty にする変更が入ると Session の `_dirty` が
        strong ref を持ち、**結果は正しいままメモリだけ件数比例へ戻る**——それを捕まえる。
        """
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        for k in range(6):
            # 年をずらす。同一 (edinet_code, year, period_end) だと FinancialRecord が
            # 1行へ upsert され、チャンク境界の取りこぼしを検知できない。
            self._make_raw_doc(db, doc_id=f"S100_{k}", period_end=date(2023 - k, 3, 31))

        proxy = _NoCloseSession(db)
        with (
            patch("collector_financials.SessionLocal", return_value=proxy),
            patch("collector_financials.REPARSE_FETCH_BATCH", 2),
            patch("collector_financials.parse_raw_rows", return_value=_parsed_financial()),
            patch("collector.asyncio.sleep", new=AsyncMock()),
        ):
            self._run(reparse_from_raw())

        held = [o for o in db.identity_map.values() if isinstance(o, XbrlRawDocument)]
        assert held == [], f"処理済みの BLOB が {len(held)} 件 identity map に残っている"
