"""`companies.market` を data_j の断面から埋める（#1015）のテスト。

共通の `db` fixture（in-memory SQLite）だけを使う。断面は `jpx_listing_snapshots` へ直接入れる
（市場・商品区分の表記は 2026-10-20 版の実物の写し）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector_master import (plan_company_market, update_company_market,  # noqa: E402
                              update_industry_from_jpx)
from database import Company, JpxListingSnapshot  # noqa: E402

SEEN = datetime(2026, 10, 21, 8, 0)


def add_snapshot(db, segments: dict, as_of=date(2026, 9, 30), seen=SEEN):
    for sec, seg in segments.items():
        db.add(JpxListingSnapshot(source="data_j", as_of_date=as_of, sec_code=sec, market_segment=seg,
                                  size_class="-", file_sha256="x", first_seen_at=seen, last_seen_at=seen))
    db.commit()


def market_of(db, ec):
    return db.query(Company.market).filter(Company.edinet_code == ec).scalar()


class TestPlan:
    SNAPSHOT = {"1001": "プライム（内国株式）", "1002": "スタンダード（外国株式）", "1003": "グロース（内国株式）",
                "1004": "PRO Market", "1005": "REIT・ベンチャーファンド・カントリーファンド・インフラファンド",
                "1006": "謎の区分"}

    def test_aliases_and_non_markets(self):
        companies = [("E1", "1001", None), ("E2", "1002", None), ("E3", "1003", "グロース"),
                     ("E4", "1004", None), ("E5", "1005", "プライム"), ("E6", "1006", None),
                     ("E7", "9999", "スタンダード"), ("E8", None, None)]
        updates, unknown = plan_company_market(self.SNAPSHOT, companies)
        # E3 は変わらないので出さない・E5 は市場ではない区分なので空へ・E6 は表に無いので書かない・
        # E7 は断面に載っていないので触らない・E8 はコードが無い
        assert updates == {"E1": "プライム", "E2": "スタンダード", "E4": "PRO Market", "E5": None}
        assert unknown == Counter({"謎の区分": 1})


class TestUpdate:
    def test_updates_only_changed_rows(self, db, make_company):
        db.add_all([make_company(edinet_code="E1", sec_code="1001"),
                    make_company(edinet_code="E2", sec_code="1002"),
                    make_company(edinet_code="E3", sec_code="9999")])
        db.commit()
        db.query(Company).filter(Company.edinet_code == "E3").update({"market": "グロース"})
        db.commit()
        add_snapshot(db, {"1001": "プライム（内国株式）", "1002": "スタンダード（内国株式）"})
        out = update_company_market(db, known_at=datetime(2026, 10, 22, tzinfo=timezone.utc))
        assert out["status"] == "updated" and out["updated"] == 2 and out["as_of"] == "2026-09-30"
        assert (market_of(db, "E1"), market_of(db, "E2"), market_of(db, "E3")) == ("プライム", "スタンダード", "グロース")
        # もう一度回しても変わる社は無い
        assert update_company_market(db, known_at=datetime(2026, 10, 22, tzinfo=timezone.utc))["updated"] == 0

    def test_unknown_segment_warns_and_is_not_written(self, db, make_company, caplog):
        db.add(make_company(edinet_code="E1", sec_code="1001"))
        db.commit()
        add_snapshot(db, {"1001": "新しい区分"})
        with caplog.at_level(logging.WARNING, logger="collector"):
            out = update_company_market(db, known_at=datetime(2026, 10, 22, tzinfo=timezone.utc))
        assert out["updated"] == 0 and out["unknown"] == {"新しい区分": 1}
        assert market_of(db, "E1") is None and "別名表に無い" in caplog.text

    def test_snapshot_not_yet_seen_does_nothing(self, db, make_company):
        db.add(make_company(edinet_code="E1", sec_code="1001"))
        db.commit()
        add_snapshot(db, {"1001": "プライム（内国株式）"})
        out = update_company_market(db, known_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
        assert out == {"status": "no_snapshot"} and market_of(db, "E1") is None


class TestNightly:
    def test_phase5_fills_market_from_the_same_download(self, db, make_company):
        """業種と同じダウンロードで断面を積み、その断面から市場を埋める。"""
        from tests.test_jpx_listing_snapshots import listing_rows, make_xlsx
        from tests.test_update_industry import _mock_client

        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=""))
        db.commit()
        asyncio.run(update_industry_from_jpx(_mock_client(make_xlsx(listing_rows())), db))
        assert market_of(db, "E00001") == "プライム"

    def test_market_failure_does_not_stop_the_industry_update(self, db, make_company, monkeypatch):
        import collector_master
        from tests.test_jpx_listing_snapshots import listing_rows, make_xlsx
        from tests.test_update_industry import _mock_client

        def boom(db, **kw):
            raise RuntimeError("市場の更新が壊れた")

        monkeypatch.setattr(collector_master, "update_company_market", boom)
        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=""))
        db.commit()
        asyncio.run(update_industry_from_jpx(_mock_client(make_xlsx(listing_rows())), db))
        assert db.query(Company.industry).filter(Company.edinet_code == "E00001").scalar() == "水産・農林業"
