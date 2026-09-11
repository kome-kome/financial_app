"""collector_interim.py（Issue #219② フェーズB・半期H1収集）のユニットテスト。

ネットワークを使わない純関数・DB選別ロジックを検証する。実 EDINET 収集の E2E は
scripts/investigate_*_edinet*.py の実データ de-risk で別途確認済み。
"""
import asyncio
import os
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector_interim  # noqa: E402
from collector_utils import EDINET_MAX_CONSECUTIVE_FAILURES, EdinetAccessError  # noqa: E402
from database import FinancialRecord  # noqa: E402
from collector_interim import (  # noqa: E402
    _extract_dei, _h1_month, build_fy_end_month_map, prefilter_interim_docs,
    process_interim_docs, split_by_csv_availability,
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


class TestSplitByCsvAvailability:
    """CSV 形式を持たない書類を候補の手前で外す（#630）。

    実測（2026-09-08・16件）はいずれも csvFlag='0' / xbrlFlag='0' の外国会社等の
    HTML のみ提出で、EDINET は type=5 に HTTP 200 + JSON を返す＝恒久的失敗。
    """

    def _doc(self, doc_id, **o):
        d = dict(docID=doc_id, edinetCode="E00001", docTypeCode="160", csvFlag="1")
        d.update(o)
        return d

    def test_drops_only_explicit_zero(self):
        with_csv, without = split_by_csv_availability([
            self._doc("S1", csvFlag="1"),
            self._doc("S2", csvFlag="0"),
        ])
        assert [d["docID"] for d in with_csv] == ["S1"]
        assert [d["docID"] for d in without] == ["S2"]

    def test_missing_flag_is_kept(self):
        # 欠損・None は除外しない（取りこぼし防止。prefilter_interim_docs と同じ向き）。
        docs = [self._doc("S1", csvFlag=None), {"docID": "S2", "edinetCode": "E1"}]
        with_csv, without = split_by_csv_availability(docs)
        assert [d["docID"] for d in with_csv] == ["S1", "S2"]
        assert without == []

    def test_integer_zero_is_dropped(self):
        # JSON が数値で返ってきても落とせる（str 比較で正規化している）。
        with_csv, without = split_by_csv_availability([self._doc("S1", csvFlag=0)])
        assert with_csv == []
        assert [d["docID"] for d in without] == ["S1"]


# ── 失敗の理由別カウントと連続失敗の送出（#630）──────────────────────────────

def _interim_doc(n: int) -> dict:
    return {"docID": f"S{n:06d}", "edinetCode": "E00001", "secCode": "1234",
            "filerName": "テスト社", "submitDateTime": "2024-11-14 10:00"}


class _StubDb:
    """process_interim_docs が触る最小の DB。**本物のセッションへは触れない。**"""
    def commit(self): pass
    def rollback(self): pass


class TestFailureBreakdown:
    def _run(self, monkeypatch, fetch_side_effect, n_docs=1):
        async def fake_fetch(client, doc_id):
            return fetch_side_effect(doc_id)
        monkeypatch.setattr(collector_interim, "fetch_xbrl_csv", fake_fetch)
        monkeypatch.setattr(collector_interim, "RATE_SLEEP", 0)
        docs = [_interim_doc(i) for i in range(n_docs)]
        return asyncio.run(process_interim_docs(
            _StubDb(), None, docs, known_edinet={"E00001"}))

    def test_fetch_failure_is_counted_by_reason(self, monkeypatch):
        stat = self._run(monkeypatch, lambda doc_id: None)
        assert stat["attempted"] == 1
        assert stat["failed"] == 1
        assert stat["failed_fetch"] == 1
        # 0 の理由も鍵として必ず出る（内訳が読めないと恒久/一時を分けられない）。
        for reason in ("dei", "dei_type", "parse", "http", "other"):
            assert stat[f"failed_{reason}"] == 0

    def test_missing_dei_is_counted_separately(self, monkeypatch):
        df = pd.DataFrame([["jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "Q2"]],
                          columns=["要素ID", "コンテキストID", "値"])
        stat = self._run(monkeypatch, lambda doc_id: df)
        assert stat["failed_dei"] == 1
        assert stat["failed_fetch"] == 0

    def test_not_q2_is_not_a_failure(self, monkeypatch):
        df = pd.DataFrame([["jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "Q3"]],
                          columns=["要素ID", "コンテキストID", "値"])
        stat = self._run(monkeypatch, lambda doc_id: df)
        assert stat["skipped_notq2"] == 1
        assert stat["failed"] == 0

    def test_consecutive_failures_raise(self, monkeypatch):
        # 単発は握って続行・**連続は構造的なので送出する**（#577 の約束を再利用）。
        with pytest.raises(EdinetAccessError):
            self._run(monkeypatch, lambda doc_id: None,
                      n_docs=EDINET_MAX_CONSECUTIVE_FAILURES + 1)

    def test_below_threshold_does_not_raise(self, monkeypatch):
        stat = self._run(monkeypatch, lambda doc_id: None,
                         n_docs=EDINET_MAX_CONSECUTIVE_FAILURES - 1)
        assert stat["failed"] == EDINET_MAX_CONSECUTIVE_FAILURES - 1

    def test_success_resets_the_streak(self, monkeypatch):
        # 「連続」であることが送出の条件。間に成功が挟まれば構造的ではない。
        ok = pd.DataFrame([["jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "Q3"]],
                          columns=["要素ID", "コンテキストID", "値"])
        n = EDINET_MAX_CONSECUTIVE_FAILURES
        seq = {f"S{i:06d}": (ok if i == n - 1 else None) for i in range(2 * n - 1)}
        stat = self._run(monkeypatch, lambda doc_id: seq[doc_id], n_docs=2 * n - 1)
        assert stat["skipped_notq2"] == 1
        assert stat["failed"] == 2 * n - 2


# ── 当期種別 HY の受理と、半期報告書の種別不一致を失敗として数える（#647）────────────

def _dei_df(period_kind: str, extra_rows=()) -> pd.DataFrame:
    """DEI 3要素＋任意の財務行。行の形は実物 S100WXIT（2025-10-30 提出の半期報告書）から写した。"""
    rows = [
        ["jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", period_kind],
        ["jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2025-09-30"],
        ["jpdei_cor:CurrentFiscalYearEndDateDEI", "FilingDateInstant", "2026-03-31"],
        *extra_rows,
    ]
    return pd.DataFrame(rows, columns=["要素ID", "コンテキストID", "値"])


class TestHalfYearPeriodKind:
    """2025年提出の新式半期報告書は DEI 当期種別を `HY` と名乗る（#647）。

    `Q2` だけを H1 とみなしていたため、2026-09-08 の日中枠は 3,921件を取得して
    3,905件を「Q2 ではない」として捨て、保存0件のまま exit=0 で終わった。
    """

    def _run(self, monkeypatch, db, df, doc_type="160", n_docs=1):
        async def fake_fetch(client, doc_id):
            return df
        monkeypatch.setattr(collector_interim, "fetch_xbrl_csv", fake_fetch)
        monkeypatch.setattr(collector_interim, "RATE_SLEEP", 0)
        docs = [{**_interim_doc(i), "docTypeCode": doc_type} for i in range(n_docs)]
        return asyncio.run(process_interim_docs(db, None, docs, known_edinet={"E00001"}))

    def test_hy_is_saved_as_h1(self, monkeypatch, db):
        # 新式の context は InterimDuration / Prior1InterimDuration。前中間期の値を採らないこと。
        df = _dei_df("HY", [
            ["jppfs_cor:NetSales", "Prior1InterimDuration_NonConsolidatedMember", "3312000000"],
            ["jppfs_cor:NetSales", "InterimDuration_NonConsolidatedMember", "3758000000"],
        ])
        stat = self._run(monkeypatch, db, df)
        assert stat["saved"] == 1
        assert stat["failed"] == 0
        assert stat["skipped_notq2"] == 0

        rec = db.query(FinancialRecord).one()
        assert rec.period_type == "H1"
        assert rec.period_end == date(2025, 9, 30)
        assert rec.year == 2026           # 同一会計年度の通期行（2026-03-31）と同じ year
        assert rec.pl_revenue == 3758000000

    def test_q2_is_still_saved(self, monkeypatch, db):
        df = _dei_df("Q2", [["jppfs_cor:NetSales", "CurrentYTDDuration", "100"]])
        assert self._run(monkeypatch, db, df)["saved"] == 1

    def test_half_year_report_with_unknown_kind_is_a_failure(self, monkeypatch, db):
        # 半期報告書は定義上 H1。H1 と名乗らないのは判定側の想定が古い＝スキップではなく失敗。
        stat = self._run(monkeypatch, db, _dei_df("XX"))
        assert stat["failed_dei_type"] == 1
        assert stat["failed"] == 1
        assert stat["skipped_notq2"] == 0
        assert stat["saved"] == 0

    def test_old_quarterly_q3_is_still_skipped(self, monkeypatch, db):
        # 旧四半期（140）の Q1/Q3 は正当な除外。失敗として数えない。
        stat = self._run(monkeypatch, db, _dei_df("Q3"), doc_type="140")
        assert stat["skipped_notq2"] == 1
        assert stat["failed"] == 0

    def test_consecutive_kind_mismatch_raises(self, monkeypatch, db):
        # 9/8 の状況（半期報告書が軒並み不一致）は、62分走り切る前に止まる。
        with pytest.raises(EdinetAccessError, match="dei_type"):
            self._run(monkeypatch, db, _dei_df("XX"),
                      n_docs=EDINET_MAX_CONSECUTIVE_FAILURES + 1)

    def test_accepted_kinds(self):
        assert collector_interim.H1_PERIOD_TYPES_DEI == {"Q2", "HY"}
