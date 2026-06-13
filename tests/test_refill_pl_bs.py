"""refill_pl_bs_from_xbrl のユニットテスト。

EDINET 再取得（fetch_xbrl_csv / parse_xbrl_csv）をモックし、pl_pretax_profit を駆動
マーカーに NULL の PL/BS 列のみ補完する挙動（既存値は上書きしない）を検証する。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import refill_pl_bs_from_xbrl


def _df():
    """fetch_xbrl_csv モックの戻り（非空 DataFrame）。"""
    return pd.DataFrame([{"element": "dummy"}])


def _parsed(pl=None, bs=None):
    return {"pl": pl or {}, "bs": bs or {}}


class TestRefillPlBsFromXbrl:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_fills_pretax_and_other_null_pl_bs(self, db, make_fin):
        rec = make_fin(
            edinet_code="E00001", doc_id="S100A",
            pl_pretax_profit=None,   # 駆動マーカー（NULL）
            pl_revenue=None,         # NULL → 補完対象
            bs_inventory=None,       # NULL → 補完対象
        )
        db.add(rec)
        db.commit()

        with (
            patch("collector.fetch_xbrl_csv", new=AsyncMock(return_value=_df())),
            patch("collector.parse_xbrl_csv", return_value=_parsed(
                pl={"pretax_profit": 126.0, "revenue": 500.0},
                bs={"inventory": 77.0},
            )),
        ):
            result = self._run(refill_pl_bs_from_xbrl(db, sleep_sec=0))

        assert result["updated"] == 1
        assert result["remaining"] == 0
        db.refresh(rec)
        assert rec.pl_pretax_profit == 126.0
        assert rec.pl_revenue == 500.0
        assert rec.bs_inventory == 77.0

    def test_does_not_overwrite_existing_values(self, db, make_fin):
        rec = make_fin(
            edinet_code="E00002", doc_id="S100B",
            pl_pretax_profit=None,
            pl_revenue=999.0,        # 既存の非 NULL → 上書きされない
        )
        db.add(rec)
        db.commit()

        with (
            patch("collector.fetch_xbrl_csv", new=AsyncMock(return_value=_df())),
            patch("collector.parse_xbrl_csv", return_value=_parsed(
                pl={"pretax_profit": 100.0, "revenue": 555.0},
            )),
        ):
            result = self._run(refill_pl_bs_from_xbrl(db, sleep_sec=0))

        assert result["updated"] == 1
        db.refresh(rec)
        assert rec.pl_pretax_profit == 100.0   # NULL だったので補完
        assert rec.pl_revenue == 999.0          # 既存値は保護

    def test_skips_records_with_pretax_present(self, db, make_fin):
        # pretax が既にあるレコードは対象外＝再取得しない（自然終了の検証）
        rec = make_fin(
            edinet_code="E00003", doc_id="S100C",
            pl_pretax_profit=123.0,
        )
        db.add(rec)
        db.commit()

        fetch = AsyncMock(return_value=_df())
        with (
            patch("collector.fetch_xbrl_csv", new=fetch),
            patch("collector.parse_xbrl_csv", return_value=_parsed(pl={"pretax_profit": 999.0})),
        ):
            result = self._run(refill_pl_bs_from_xbrl(db, sleep_sec=0))

        assert result == {"updated": 0, "skipped": 0, "failed": 0, "remaining": 0}
        fetch.assert_not_called()
        db.refresh(rec)
        assert rec.pl_pretax_profit == 123.0

    def test_skips_when_xbrl_empty(self, db, make_fin):
        db.add(make_fin(edinet_code="E00004", doc_id="S100D", pl_pretax_profit=None))
        db.commit()
        with patch("collector.fetch_xbrl_csv", new=AsyncMock(return_value=None)):
            result = self._run(refill_pl_bs_from_xbrl(db, sleep_sec=0))
        assert result["skipped"] == 1
        assert result["updated"] == 0
        assert result["remaining"] == 1   # 補完できず対象に残る
