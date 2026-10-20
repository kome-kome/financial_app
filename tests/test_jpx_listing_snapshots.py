"""上場銘柄一覧（data_j）の月次断面（#1004）のテスト。

共通の `db` fixture（in-memory SQLite）だけを使い、ネットには触れない。検体の見出しは実物の写し
（`tests/test_update_industry.py::JPX_HEADER`・2026-10-21 に data_j_20261020.xlsx の1行目から写した）で、
行は架空。健全性の閾値（行数 3,000 以上・'-' 以外 900〜2,300 件）を満たす大きさで作る。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch_freshness as bf  # noqa: E402
from collector_master import (_read_jpx_excel, parse_jpx_snapshot,  # noqa: E402
                              record_jpx_listing_snapshot, update_industry_from_jpx)
from collector_utils import JpxListingSnapshotError  # noqa: E402
from database import JpxListingSnapshot, jpx_listing_snapshot_at  # noqa: E402
from tests.test_update_industry import JPX_HEADER, _FakeXlsBook, _mock_client  # noqa: E402

N_ROWS = 3200
N_SIZED = 1200
SIZES = ("TOPIX Core30", "TOPIX Large70", "TOPIX Mid400", "TOPIX Small 1", "TOPIX Small 2")
T0 = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


def listing_rows(n=N_ROWS, sized=N_SIZED, as_of=20260930, size_of=None):
    """実物と同じ並びの行。コードは 1001 から（整数）、最後の1社だけ英字入り（`130A`）。"""
    rows = []
    for i in range(n):
        code = "130A" if i == n - 1 else 1001 + i
        size = (size_of(i) if size_of else (SIZES[i % 5] if i < sized else "-"))
        size_code = "-" if size == "-" else (i % 5) + 1
        rows.append([as_of, code, f"架空{i}", "プライム（内国株式）", 50, "水産・農林業", 1, "食品",
                     size_code, size])
    return rows


def make_xlsx(rows, header=JPX_HEADER) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def content():
    return make_xlsx(listing_rows())


def stored(db):
    return {r.sec_code: r for r in db.query(JpxListingSnapshot).all()}


class TestParse:
    def test_reads_the_snapshot_by_name(self, content):
        as_of, rows = parse_jpx_snapshot(content)
        assert as_of == date(2026, 9, 30)
        assert len(rows) == N_ROWS
        assert rows[0] == {"sec_code": "1001", "market_segment": "プライム（内国株式）",
                           "industry33": "水産・農林業", "size_code": "1", "size_class": "TOPIX Core30"}
        assert rows[-1]["sec_code"] == "130A" and rows[-1]["size_class"] == "-"

    def test_xls_floats_become_plain_strings(self, monkeypatch):
        import xlrd

        rows = [JPX_HEADER] + [[float(r[0]), float(r[1]) if isinstance(r[1], int) else r[1]] + r[2:8]
                               + [float(r[8]) if r[8] != "-" else "-", r[9]] for r in listing_rows()]
        monkeypatch.setattr(xlrd, "open_workbook", lambda *a, **k: _FakeXlsBook(rows))
        as_of, parsed = parse_jpx_snapshot(b"dummy-xls-bytes")
        assert as_of == date(2026, 9, 30)
        assert parsed[0]["sec_code"] == "1001" and parsed[0]["size_code"] == "1"

    def test_missing_snapshot_column_fails_but_industry_still_reads(self):
        """断面の列（規模区分）が欠けても業種の読み出しは止めない。"""
        header = JPX_HEADER[:-1]
        content = make_xlsx([r[:-1] for r in listing_rows()], header=header)
        with pytest.raises(JpxListingSnapshotError, match="規模区分"):
            parse_jpx_snapshot(content)
        assert len(_read_jpx_excel(content)) == N_ROWS

    @pytest.mark.parametrize("rows, match", [
        (listing_rows(n=2900, sized=1200), "行数"),
        (listing_rows()[:-1] + [listing_rows(as_of=20261031)[-1]], "日付"),
        (listing_rows(size_of=lambda i: "TOPIX Small 3" if i < 100 else ("TOPIX Small 1" if i < 1200 else "-")),
         "語彙外"),
        (listing_rows(sized=800), "'-' 以外"),
        (listing_rows(sized=2400), "'-' 以外"),
        (listing_rows() + [listing_rows()[0]], "重複"),
    ], ids=["few_rows", "two_dates", "unknown_size", "too_few_sized", "too_many_sized", "duplicate"])
    def test_health_gate(self, rows, match):
        with pytest.raises(JpxListingSnapshotError, match=match):
            parse_jpx_snapshot(make_xlsx(rows))


class TestRecord:
    def test_writes_rows_with_edinet_code(self, db, make_company, content):
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_company(edinet_code="E00002", sec_code="130A"))
        db.commit()
        out = record_jpx_listing_snapshot(db, content, now=T0)
        assert out["status"] == "wrote" and out["rows"] == N_ROWS and out["resolved"] == 2
        rows = stored(db)
        assert rows["1001"].edinet_code == "E00001" and rows["130A"].edinet_code == "E00002"
        assert rows["1002"].edinet_code is None          # 企業表に無い社も断面には残す
        assert rows["1001"].as_of_date == date(2026, 9, 30)
        assert rows["1001"].first_seen_at == rows["1001"].last_seen_at == T0.replace(tzinfo=None)
        assert len(rows["1001"].file_sha256) == 64

    def test_same_as_of_is_not_overwritten(self, db, content, caplog):
        record_jpx_listing_snapshot(db, content, now=T0)
        later = T0 + timedelta(days=1)
        assert record_jpx_listing_snapshot(db, content, now=later)["status"] == "seen"
        # 中身が違う同じ時点: 上書きせず WARNING
        changed = make_xlsx(listing_rows(size_of=lambda i: "TOPIX Mid400" if i < N_SIZED else "-"))
        with caplog.at_level(logging.WARNING, logger="collector"):
            assert record_jpx_listing_snapshot(db, changed, now=later + timedelta(days=1))["status"] == "seen"
        assert "上書きしない" in caplog.text and "値が違う" in caplog.text
        rows = stored(db)
        assert len(rows) == N_ROWS
        assert rows["1001"].size_class == "TOPIX Core30"            # 値は最初の版のまま
        assert rows["1001"].first_seen_at == T0.replace(tzinfo=None)
        assert rows["1001"].last_seen_at == (later + timedelta(days=1)).replace(tzinfo=None)

    def test_failed_gate_leaves_existing_rows(self, db, content):
        record_jpx_listing_snapshot(db, content, now=T0)
        with pytest.raises(JpxListingSnapshotError):
            record_jpx_listing_snapshot(db, make_xlsx(listing_rows(n=10, sized=5)), now=T0 + timedelta(days=1))
        rows = stored(db)
        assert len(rows) == N_ROWS and rows["1001"].last_seen_at == T0.replace(tzinfo=None)

    def test_next_month_adds_a_new_snapshot(self, db, content):
        record_jpx_listing_snapshot(db, content, now=T0)
        record_jpx_listing_snapshot(db, make_xlsx(listing_rows(as_of=20261031)), now=T0 + timedelta(days=15))
        dates = {d for (d,) in db.query(JpxListingSnapshot.as_of_date).distinct()}
        assert dates == {date(2026, 9, 30), date(2026, 10, 31)}


class TestUpdateIndustryWritesTheSnapshot:
    def test_same_download_updates_industry_and_snapshot(self, db, make_company, content):
        from database import Company

        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=""))
        db.commit()
        asyncio.run(update_industry_from_jpx(_mock_client(content), db))
        assert db.query(Company).filter_by(edinet_code="E00001").one().industry == "水産・農林業"
        assert len(stored(db)) == N_ROWS

    def test_bad_snapshot_does_not_stop_the_industry_update(self, db, make_company, caplog):
        from database import Company

        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=""))
        db.commit()
        bad = make_xlsx(listing_rows()[:-1] + [listing_rows(as_of=20261031)[-1]])   # 日付が2種類
        with caplog.at_level(logging.WARNING, logger="collector"):
            asyncio.run(update_industry_from_jpx(_mock_client(bad), db))
        assert db.query(Company).filter_by(edinet_code="E00001").one().industry == "水産・農林業"
        assert stored(db) == {}
        assert "断面を記録できなかった" in caplog.text


class TestVisibility:
    def test_by_first_seen_at_not_as_of(self, db, content):
        """月末時点の版でも、こちらが取得する前の時刻には見えない（公表遅れぶんの先読みをしない）。"""
        seen = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
        record_jpx_listing_snapshot(db, content, now=seen)
        assert jpx_listing_snapshot_at(db, datetime(2026, 10, 1, tzinfo=timezone.utc)) == (None, {})
        as_of, rows = jpx_listing_snapshot_at(db, seen)
        assert as_of == date(2026, 9, 30) and len(rows) == N_ROWS
        assert jpx_listing_snapshot_at(db, datetime(2026, 10, 7))[0] == date(2026, 9, 30)   # naive は UTC


class TestFreshness:
    def test_empty_table_reads_as_none(self, db):
        assert bf._jpx_listing_seen_at(db) is None and bf._jpx_listing_as_of(db) is None

    def test_reads_the_latest(self, db, content):
        record_jpx_listing_snapshot(db, content, now=T0)
        assert bf._jpx_listing_seen_at(db) == T0.replace(tzinfo=None)
        assert bf._jpx_listing_as_of(db) == datetime(2026, 9, 30, tzinfo=timezone.utc)

    def test_registered_with_the_publication_lag(self):
        by_read = {p.read: p for p in bf.PRODUCERS}
        assert by_read[bf._jpx_listing_seen_at].cadence_h == 24.0
        assert by_read[bf._jpx_listing_as_of].cadence_h == (31 + bf.JPX_LISTING_PUBLICATION_LAG_DAYS) * 24.0
