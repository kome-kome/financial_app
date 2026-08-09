"""refill_cf_from_xbrl (#95) / fill_recent_stock_price_gap_yahoo (#96) のユニットテスト。

外部 API（EDINET / Yahoo Finance）をモックし、DB 更新動作を検証する。
"""
import asyncio
import os
import sys
from datetime import date, datetime, time as dtime, timedelta
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import fill_recent_stock_price_gap_yahoo, refill_cf_from_xbrl
from collector_prices import JST, PRICE_REFRESH_TAIL_DAYS


def _monday_anchor():
    """「直近の月曜 03:00 JST」相当の (now_jst, session) を返す。

    実行日に依存せず基準セッションを金曜に固定するためのアンカー。実 `date.today()` の
    2週間ほど手前に置き、`floor_d`（DAILY_WINDOW_DAYS のクリップ）に触れないようにする。
    """
    base = date.today() - timedelta(days=14)
    monday = base + timedelta(days=(7 - base.weekday()) % 7)
    return datetime.combine(monday, dtime(3, 0), JST), monday - timedelta(days=3)


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

    def test_start_date_is_per_company_not_global_max(self, db, make_company, make_price):
        """起点は銘柄別の最終日（Issue #415）。

        全社横断の max を1つ選んで全社に適用すると、先行して復旧した1銘柄の日付が
        遅延銘柄にも使われ、遅延銘柄の欠測期間が永久に埋まらない（2026-07 の実障害）。
        """
        now_jst, session = _monday_anchor()
        fresh = (session - timedelta(days=1)).isoformat()   # 1営業日ぶん遅れている社
        stale = (session - timedelta(days=20)).isoformat()  # 大きく遅れている社
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_company(edinet_code="E00002", sec_code="1002"))
        db.add(make_price(edinet_code="E00001", trade_date=fresh))
        db.add(make_price(edinet_code="E00002", trade_date=stale))
        db.commit()

        seen = {}

        async def _fake_fetch(http, ticker, d_from, d_to):
            seen[ticker] = d_from
            return []

        tail = timedelta(days=PRICE_REFRESH_TAIL_DAYS - 1)
        with patch("collector_prices.fetch_yahoo_history", new=_fake_fetch):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        # 遅延銘柄は自分の最終日を起点にする（全社 max ではない）。#474 以降は
        # 暫定終値を潰すため tail ぶん手前へ倒す。
        assert seen["1002.T"] == (date.fromisoformat(stale) - tail).strftime("%Y%m%d")
        assert seen["1001.T"] == (date.fromisoformat(fresh) - tail).strftime("%Y%m%d")

    def test_skips_companies_already_at_latest_session(self, db, make_company, make_price):
        """基準は「閉場済みの最新 JST 営業日」（#474）。

        旧実装は UTC の `date.today()` と比べていたため、JST 日曜 03:47 起動の
        run 31272807314 は全社が既に持つ金曜バーを 4,437社ぶん取り直して 2h11m を使った。
        """
        now_jst, session = _monday_anchor()          # 月曜 03:00 JST → session=金曜
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_company(edinet_code="E00002", sec_code="1002"))
        db.add(make_price(edinet_code="E00001", trade_date=session.isoformat()))
        db.add(make_price(edinet_code="E00002", trade_date=session.isoformat()))
        db.commit()

        seen = []

        async def _fake_fetch(http, ticker, d_from, d_to):
            seen.append(ticker)
            return []

        with (
            patch("collector_prices.fetch_yahoo_history", new=_fake_fetch),
            patch("collector_prices.trim_daily", return_value=0) as trim,
        ):
            result = self._run(
                fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        assert seen == []                            # Yahoo を1回も叩かない
        assert result["skipped"] is True
        assert result["reason"] == "no_gap"
        assert result["session"] == session.isoformat()
        # 取得なしでも保持窓の trim は回す（skip した夜だけ daily が伸びない）
        assert trim.call_count == 1

    def test_drops_in_progress_session_bar(self, db, make_company, make_price):
        """場中に走った run が「進行中バー」を終値として書かないこと（#474）。

        Yahoo の interval=1d は場中でもその日の途中経過を1本返す。J-Quants 無料は
        直近12週を配信しないため暫定値は訂正されず、対象社を絞ると上書きの機会も来ない。
        """
        anchor, _ = _monday_anchor()
        now_jst = anchor + timedelta(days=1, hours=8)     # 火曜 11:00 JST ＝ 場中
        session = now_jst.date() - timedelta(days=1)      # 引け済みは前日（月曜）
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_price(edinet_code="E00001",
                          trade_date=(session - timedelta(days=1)).isoformat()))
        db.commit()

        async def _fake_fetch(http, ticker, d_from, d_to):
            return [
                {"trade_date": session.isoformat(), "close": 1500.0, "volume": 10},
                # 進行中セッション（当日）の途中経過。これが入ってはいけない。
                {"trade_date": now_jst.date().isoformat(), "close": 1499.0, "volume": 1},
            ]

        saved = []
        # record_prices_batch は Postgres 専用（pg_insert）のため保存側はモックする。
        with (
            patch("collector_prices.fetch_yahoo_history", new=_fake_fetch),
            patch("collector_prices.record_prices_batch",
                  side_effect=lambda _db, batch, **kw: saved.extend(batch) or len(batch)),
            patch("collector_prices.trim_daily", return_value=0),
        ):
            result = self._run(
                fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        assert [r["trade_date"] for r in saved] == [session.isoformat()]
        assert result["new_rows"] == 1

    def test_falls_back_to_all_companies_when_session_too_old(
            self, db, make_company, make_price, caplog):
        """基準セッションが異常に古いときは絞らず全社取得へ倒す（#474 の安全弁）。

        判定側の異常が「誰も取りに行かない」＝ #415 の静かな鮮度死へ倒れないこと。
        """
        now_jst, session = _monday_anchor()
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_price(edinet_code="E00001", trade_date=session.isoformat()))
        db.commit()

        seen = []

        async def _fake_fetch(http, ticker, d_from, d_to):
            seen.append(ticker)
            return []

        stale_session = session - timedelta(days=30)
        with (
            patch("collector_prices.last_closed_session", return_value=stale_session),
            patch("collector_prices.fetch_yahoo_history", new=_fake_fetch),
            caplog.at_level("WARNING", logger="collector"),
        ):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        assert seen == ["1001.T"]                    # skip 側へ倒れていない
        assert "フォールバック" in caplog.text

    def test_skips_only_companies_without_gap(self, db, make_company, make_price):
        """gap_days の判定も銘柄別。ギャップのある社だけが取得対象になる。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_company(edinet_code="E00002", sec_code="1002"))
        db.add(make_price(edinet_code="E00001", trade_date=date.today().isoformat()))
        db.add(make_price(edinet_code="E00002",
                          trade_date=(date.today() - timedelta(days=15)).isoformat()))
        db.commit()

        seen = []

        async def _fake_fetch(session, ticker, d_from, d_to):
            seen.append(ticker)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=_fake_fetch):
            result = self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=7))

        assert seen == ["1002.T"]          # today の株価を持つ 1001 は対象外
        assert result["companies"] == 1

    def test_start_date_clipped_to_daily_window(self, db, make_company, make_price):
        """起点は daily 保持窓（DAILY_WINDOW_DAYS）でクリップする。

        それより過去への遡及は backfill_weekly_history_yahoo の管轄。毎日の
        ギャップ補完が数年分を取りに行かないための暴走ガード。
        """
        from database import DAILY_WINDOW_DAYS

        very_old = (date.today() - timedelta(days=DAILY_WINDOW_DAYS + 400)).isoformat()
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_price(edinet_code="E00001", trade_date=very_old))
        db.commit()

        seen = {}

        async def _fake_fetch(session, ticker, d_from, d_to):
            seen[ticker] = d_from
            return []

        with patch("collector_prices.fetch_yahoo_history", new=_fake_fetch):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0))

        floor_str = (date.today() - timedelta(days=DAILY_WINDOW_DAYS)).strftime("%Y%m%d")
        assert seen["1001.T"] == floor_str
