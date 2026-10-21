"""指数イベント台帳の見直しの登録を watchdog が見張る（#1014）のテスト。

DB・gh には触れない。架空の登録表と判定時刻で状態を作り、起票・自動クローズ・本文を確かめる。
本物の登録表では、次の期限より前のどの日付でも鳴らないことも確かめる。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import batch_freshness as bf  # noqa: E402
import index_events as IE  # noqa: E402
from scripts import check_batch_freshness as W  # noqa: E402

EXPECTED = {"topix-2099-10": IE.ExpectedReview(announce_expected=date(2099, 10, 7),
                                               effective=date(2099, 10, 30), basis="架空")}
REVIEW = IE.REVIEWS["topix-2026-10"]


def at(d: date, hour=12) -> datetime:
    """JST の日付の昼（UTC で表す）。"""
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone(timedelta(hours=9))).astimezone(timezone.utc)


def statuses(rows) -> dict:
    return {(r["key"], r["check"]): r["status"] for r in rows}


def snap_of(rows, error=None) -> dict:
    snap = {"now": at(date(2099, 10, 20)), "db_label": "local", "db_error": None, "rows": [],
            "producers": [], "index_reviews": rows}
    if error:
        snap["index_reviews_error"] = error
    return snap


class TestCollect:
    def test_due_is_announcement_plus_grace(self):
        due = date(2099, 10, 7) + timedelta(days=bf.INDEX_REVIEW_GRACE_DAYS)
        ok = bf.collect_index_reviews(at(due), reviews={}, expected=EXPECTED)
        late = bf.collect_index_reviews(at(due + timedelta(days=1)), reviews={}, expected=EXPECTED)
        assert statuses(ok) == {("topix-2099-10", "registration"): "ok"}
        assert statuses(late) == {("topix-2099-10", "registration"): "overdue"}
        assert late[0]["due"] == due and late[0]["title"] == bf.index_registration_title("topix-2099-10")

    def test_registered_review_is_ok_even_after_it_leaves_expected(self):
        """登録すると EXPECTED_REVIEWS から消える。それでも登録の検査は ok と言える（閉じる相手を見失わない）。"""
        rows = bf.collect_index_reviews(at(date(2099, 12, 1)), reviews={"topix-2099-10": REVIEW}, expected={})
        assert statuses(rows)[("topix-2099-10", "registration")] == "ok"

    def test_unconfirmed_on_effective_day(self):
        unconfirmed = REVIEW._replace(confirmed=False, effective=date(2099, 10, 30))
        before = bf.collect_index_reviews(at(date(2099, 10, 29)), reviews={"k": unconfirmed}, expected={})
        on = bf.collect_index_reviews(at(date(2099, 10, 30)), reviews={"k": unconfirmed}, expected={})
        assert statuses(before)[("k", "confirmation")] == "ok"
        assert statuses(on)[("k", "confirmation")] == "unconfirmed"

    def test_judged_by_the_japanese_date(self):
        """UTC では前日でも JST で期限の翌日なら overdue（JST 0:30 = 前日 UTC 15:30）。"""
        due = date(2099, 10, 7) + timedelta(days=bf.INDEX_REVIEW_GRACE_DAYS)
        jst_after_midnight = at(due + timedelta(days=1), hour=0)
        assert jst_after_midnight.date() == due      # UTC ではまだ期限の日
        rows = bf.collect_index_reviews(jst_after_midnight, reviews={}, expected=EXPECTED)
        assert rows[0]["status"] == "overdue"

    def test_real_registry_is_quiet_until_the_first_due_date(self):
        """健全な履歴では鳴らない: 本物の登録表で、次の期限（2027-10 の再評価）まではどの日も ok。"""
        key, exp = IE.next_review_expected()
        due = exp.announce_expected + timedelta(days=bf.INDEX_REVIEW_GRACE_DAYS)
        d = date(2026, 10, 21)
        while d <= due:
            rows = bf.collect_index_reviews(at(d))
            assert all(r["status"] == "ok" for r in rows), (d, [r for r in rows if r["status"] != "ok"])
            d += timedelta(days=7)
        late = bf.collect_index_reviews(at(due + timedelta(days=1)))
        assert statuses(late)[(key, "registration")] == "overdue"


class TestWatchdog:
    def test_overdue_files_and_registration_recovers(self):
        overdue = bf.collect_index_reviews(at(date(2099, 10, 20)), reviews={}, expected=EXPECTED)
        found = W.problems(snap_of(overdue))
        assert [p["title"] for p in found] == [bf.index_registration_title("topix-2099-10")]
        assert "期限" in found[0]["message"]
        body = W.issue_body(found[0], snap_of(overdue))
        assert "topix-2099-10" in body and "import_index_list" in body

        registered = bf.collect_index_reviews(at(date(2099, 10, 20)), reviews={"topix-2099-10": REVIEW},
                                              expected=EXPECTED)
        assert W.problems(snap_of(registered)) == []
        titles = {t["title"] for t in W.recoveries(snap_of(registered))}
        assert bf.index_registration_title("topix-2099-10") in titles
        target = next(t for t in W.recoveries(snap_of(registered))
                      if t["title"] == bf.index_registration_title("topix-2099-10"))
        assert "ok に戻った" in W.recovery_body(target, snap_of(registered), human_touched=False)

    def test_problems_are_reported_even_when_the_db_is_down(self):
        overdue = bf.collect_index_reviews(at(date(2099, 10, 20)), reviews={}, expected=EXPECTED)
        snap = {**snap_of(overdue), "db_error": "connection refused"}
        titles = [p["title"] for p in W.problems(snap)]
        assert W.DB_ERROR_TITLE in titles and bf.index_registration_title("topix-2099-10") in titles

    def test_unreadable_registry_files_and_recovers(self):
        found = W.problems(snap_of([], error="ImportError: x"))
        assert [p["title"] for p in found] == [W.INDEX_REVIEWS_ERROR_TITLE]
        assert "登録表を読めなかった" in W.issue_body(found[0], snap_of([], error="ImportError: x"))
        rec = W.recoveries(snap_of([]))
        target = next(t for t in rec if t["title"] == W.INDEX_REVIEWS_ERROR_TITLE)
        assert "読めた" in W.recovery_body(target, snap_of([]), human_touched=False)
        # 読めない回は「読めた」と言わない（開いた起票を閉じない）
        assert W.INDEX_REVIEWS_ERROR_TITLE not in {t["title"] for t in W.recoveries(snap_of([], error="x"))}

    def test_report_section(self):
        rows = bf.collect_index_reviews(at(date(2099, 10, 1)), reviews={}, expected=EXPECTED)
        lines = W.format_report(snap_of(rows))
        assert "-- 指数イベント台帳の見直し --" in lines
        assert any(line.startswith("topix-2099-10: ok") for line in lines)
