"""refill_cf_from_xbrl (#95) / fill_recent_stock_price_gap_yahoo (#96) のユニットテスト。

外部 API（EDINET / Yahoo Finance）をモックし、DB 更新動作を検証する。
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import fill_recent_stock_price_gap_yahoo, refill_cf_from_xbrl


# ── refill_cf_from_xbrl (#95) ─────────────────────────────────────────────

def _make_cf_df(op_cf=100.0, net_cash=50.0, capex=-30.0):
    """parse_xbrl_csv が返す形式のダミー DataFrame（実際は parse 内で使用）"""
    return pd.DataFrame([{"element": "dummy"}])


def _parsed_cf(op_cf=None, net_cash=None, capex=None, inv_cf=None, fin_cf=None):
    cf = {}
    if op_cf    is not None: cf["operating_cf"]    = op_cf
    if net_cash is not None: cf["net_change_cash"] = net_cash
    if capex    is not None: cf["capex"]            = capex
    if inv_cf   is not None: cf["investing_cf"]     = inv_cf
    if fin_cf   is not None: cf["financing_cf"]     = fin_cf
    return {"cf": cf}


class TestRefillCfFromXbrl:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_normal_mode_fills_net_change_cash(self, db, make_fin):
        rec = make_fin(
            edinet_code="E00001", year=2023, period_end="2023-03-31",
            doc_id="S100TEST",
            cf_operating_cf=100.0,   # 非 NULL（対象になる条件）
            cf_net_change_cash=None,  # NULL → 補完対象
        )
        db.add(rec)
        db.commit()

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value=_make_cf_df())),
            patch("collector_financials.parse_xbrl_csv", return_value=_parsed_cf(
                op_cf=100.0, net_cash=50.0, capex=-30.0, inv_cf=-80.0, fin_cf=20.0
            )),
        ):
            result = self._run(refill_cf_from_xbrl(db, limit=10, sleep_sec=0))

        assert result["updated"] == 1
        db.refresh(rec)
        assert rec.cf_net_change_cash == 50.0

    def test_capex_only_mode_fills_capex(self, db, make_fin):
        rec = make_fin(
            doc_id="S100TEST",
            cf_operating_cf=100.0,
            cf_net_change_cash=50.0,  # 非 NULL
            cf_capex=None,             # NULL → capex_only 対象
        )
        db.add(rec)
        db.commit()

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value=_make_cf_df())),
            patch("collector_financials.parse_xbrl_csv", return_value=_parsed_cf(capex=-30.0)),
        ):
            result = self._run(refill_cf_from_xbrl(db, limit=10, capex_only=True, sleep_sec=0))

        assert result["updated"] == 1
        db.refresh(rec)
        assert rec.cf_capex == -30.0

    def test_missing_cf_mode_fills_operating_cf(self, db, make_fin):
        rec = make_fin(
            doc_id="S100TEST",
            cf_operating_cf=None,  # NULL → missing_cf 対象
        )
        db.add(rec)
        db.commit()

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value=_make_cf_df())),
            patch("collector_financials.parse_xbrl_csv", return_value=_parsed_cf(op_cf=200.0)),
        ):
            result = self._run(refill_cf_from_xbrl(db, limit=10, missing_cf=True, sleep_sec=0))

        assert result["updated"] == 1
        db.refresh(rec)
        assert rec.cf_operating_cf == 200.0

    def test_skips_when_xbrl_returns_empty(self, db, make_fin):
        db.add(make_fin(doc_id="S100TEST", cf_operating_cf=100.0, cf_net_change_cash=None))
        db.commit()

        with (
            patch("collector_financials.fetch_xbrl_csv", new=AsyncMock(return_value=None)),
        ):
            result = self._run(refill_cf_from_xbrl(db, limit=10, sleep_sec=0))

        assert result["skipped"] == 1
        assert result["updated"] == 0


# ── fill_recent_stock_price_gap_yahoo (#96) ────────────────────────────────

class TestFillRecentStockPriceGapYahoo:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_skips_when_no_price_data(self, db):
        result = self._run(fill_recent_stock_price_gap_yahoo(db))
        assert result["skipped"] is True
        assert result["reason"] == "empty"

    def test_skips_when_no_gap(self, db, make_price):
        # today の株価があれば gap=0 ≤ gap_days=7 → スキップ
        db.add(make_price(trade_date=date.today().isoformat()))
        db.commit()
        result = self._run(fill_recent_stock_price_gap_yahoo(db))
        assert result["skipped"] is True
        assert result["reason"] == "no_gap"

    def test_fetches_yahoo_when_gap_exceeds_threshold(self, db, make_company, make_price):
        # 15日前の株価 → gap=15 > gap_days=7 → Yahoo 補完が走る
        old_date = (date.today() - timedelta(days=15)).isoformat()
        db.add(make_company(sec_code="1001"))
        db.add(make_price(trade_date=old_date))
        db.commit()

        with patch("collector_prices.fetch_yahoo_history", new=AsyncMock(return_value=[
            {"trade_date": date.today().isoformat(), "close": 1500.0, "volume": 10000},
        ])):
            result = self._run(fill_recent_stock_price_gap_yahoo(db))

        assert result["skipped"] is False
        assert result["upserted"] >= 1
