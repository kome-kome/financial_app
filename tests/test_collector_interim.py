"""collector_interim.py（Issue #219② フェーズB・半期H1収集）のユニットテスト。

ネットワークを使わない純関数・DB選別ロジックを検証する。実 EDINET 収集の E2E は
scripts/investigate_*_edinet*.py の実データ de-risk で別途確認済み。
"""
import os
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector_interim import (  # noqa: E402
    _extract_dei, _h1_month, build_fy_end_month_map, prefilter_interim_docs,
    INTERIM_DOC_TYPES,
)


class TestH1Month:
    @pytest.mark.parametrize("fy_end,expected", [
        (3, 9),    # 3月決算 → H1末9月
        (12, 6),   # 12月決算 → H1末6月
        (9, 3),    # 9月決算 → H1末3月
        (6, 12),   # 6月決算 → H1末12月
        (1, 7),    # 1月決算 → H1末7月
    ])
    def test_h1_month(self, fy_end, expected):
        assert _h1_month(fy_end) == expected


class TestPrefilter:
    def _doc(self, **o):
        d = dict(edinetCode="E00001", docTypeCode="140", periodEnd="2022-09-30", docID="S1")
        d.update(o)
        return d

    def test_doctype160_always_kept(self):
        # 半期報告書(160)は年1回=常にH1 → 年度末不明でもそのまま候補。
        docs = [self._doc(docTypeCode="160", periodEnd="2025-03-31")]
        assert prefilter_interim_docs(docs, {}) == docs

    def test_doctype140_kept_when_periodend_matches_h1(self):
        # 3月決算(fy_end=3)→H1末9月。periodEnd=09-30 は残す。
        docs = [self._doc(docTypeCode="140", periodEnd="2022-09-30")]
        assert prefilter_interim_docs(docs, {"E00001": 3}) == docs

    def test_doctype140_dropped_when_periodend_is_q1_or_q3(self):
        # Q1(06-30)・Q3(12-31)は H1末(09)と不一致 → 除外。
        q1 = self._doc(docTypeCode="140", periodEnd="2022-06-30")
        q3 = self._doc(docTypeCode="140", periodEnd="2022-12-31")
        assert prefilter_interim_docs([q1, q3], {"E00001": 3}) == []

    def test_doctype140_unknown_fyend_kept_for_dei_judgement(self):
        # 年度末不明企業は事前選別せず候補に残し、DEI 判定に委ねる（取りこぼし防止）。
        docs = [self._doc(docTypeCode="140", periodEnd="2022-06-30")]
        assert prefilter_interim_docs(docs, {}) == docs

    def test_doc_types_constant(self):
        assert INTERIM_DOC_TYPES == {"140", "160"}


class TestExtractDei:
    def _df(self, rows):
        # 実 EDINET CSV の日本語列名を模す（_detect_xbrl_columns が拾う）。
        return pd.DataFrame(rows, columns=["要素ID", "コンテキストID", "値"])

    def test_extracts_period_meta(self):
        df = self._df([
            ["jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "Q2"],
            ["jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2024-09-30"],
            ["jpdei_cor:CurrentFiscalYearEndDateDEI", "FilingDateInstant", "2025-03-31"],
            ["jppfs_cor:NetSales", "CurrentYTDDuration", "12345"],
        ])
        dei = _extract_dei(df)
        assert dei["TypeOfCurrentPeriodDEI"] == "Q2"
        assert dei["CurrentPeriodEndDateDEI"] == "2024-09-30"
        assert dei["CurrentFiscalYearEndDateDEI"] == "2025-03-31"

    def test_missing_dei_returns_partial(self):
        df = self._df([["jppfs_cor:Assets", "CurrentQuarterInstant", "999"]])
        assert _extract_dei(df) == {}


class TestBuildFyEndMonthMap:
    def test_uses_most_common_annual_period_end_month(self, db, make_fin):
        # 通期行のみから会計年度末「月」を最頻で推定する。H1 行は無視される。
        db.add(make_fin(edinet_code="E00001", year=2023,
                        period_end=date(2023, 3, 31), period_type="annual"))
        db.add(make_fin(edinet_code="E00001", year=2024,
                        period_end=date(2024, 3, 31), period_type="annual"))
        db.add(make_fin(edinet_code="E00001", year=2024,
                        period_end=date(2023, 9, 30), period_type="H1"))  # 無視される
        db.add(make_fin(edinet_code="E00002", year=2024,
                        period_end=date(2024, 12, 31), period_type="annual"))
        db.commit()
        m = build_fy_end_month_map(db)
        assert m["E00001"] == 3
        assert m["E00002"] == 12
