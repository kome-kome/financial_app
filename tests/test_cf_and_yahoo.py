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


class TestDelistedPricelessBackoff:
    """上場廃止済みで価格ゼロの社を毎晩叩かない（Issue #475）。

    2026-08-21 の実測で、価格を1件も持たない 454社は**全て `is_active=False`** だった。
    毎晩 454リクエスト ≒ 13分を捨てている。ただし**恒久除外にはしない**——`is_active` は
    `/equities/master` 由来で、無料プランのエンバーゴにより「as-of より後に上場した銘柄」が
    誤って delisted に見えることがある（#463）。除外すると新規上場も再開も永久に拾えず、
    しかも**失敗が出ない**。
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def test_fires_exactly_once_per_interval(self):
        from collector_prices import should_retry_priceless_delisted as retry
        from collector_utils import DELISTED_RETRY_INTERVAL_DAYS as N

        base = date(2026, 8, 21)
        hits = [k for k in range(N * 3) if retry("E00001", base + timedelta(days=k))]
        assert len(hits) == 3
        assert all(b - a == N for a, b in zip(hits, hits[1:]))

    def test_load_is_spread_across_days(self):
        """同じ日に全社が固まらないこと（曜日を edinet_code から決定的に散らす）。"""
        from collector_prices import should_retry_priceless_delisted as retry
        from collector_utils import DELISTED_RETRY_INTERVAL_DAYS as N

        codes = [f"E{i:05d}" for i in range(454)]
        base = date(2026, 8, 21)
        per_day = [sum(1 for c in codes if retry(c, base + timedelta(days=k)))
                   for k in range(N)]
        assert sum(per_day) == len(codes), "1周期で全社ちょうど1回"
        assert max(per_day) < len(codes) / N * 1.5, f"偏りが大きい: {per_day}"

    def test_delisted_priceless_company_is_skipped_on_off_days(
            self, db, make_company, make_price, monkeypatch):
        """当たらない日は fetch を呼ばない。生きている社の補完は続ける。"""
        from collector_prices import should_retry_priceless_delisted

        now_jst, session = _monday_anchor()
        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=True))
        db.add(make_company(edinet_code="E00002", sec_code="1002", is_active=False))
        db.add(make_price(edinet_code="E00001",
                          trade_date=(session - timedelta(days=3)).isoformat()))
        db.commit()

        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: False)
        seen = []

        async def fake(http, sec_code, d_from, d_to, **kw):
            seen.append(sec_code)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=fake):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))
        assert seen == ["1001.T"], f"廃止済み価格ゼロの社を叩いている: {seen}"
        assert callable(should_retry_priceless_delisted)

    def test_delisted_priceless_company_is_retried_on_its_day(
            self, db, make_company, make_price, monkeypatch):
        """当たる日には試す＝恒久除外ではない（#463 の誤 delisted 判定を拾い直せる）。"""
        now_jst, session = _monday_anchor()
        db.add(make_company(edinet_code="E00002", sec_code="1002", is_active=False))
        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=True))
        db.add(make_price(edinet_code="E00001",
                          trade_date=(session - timedelta(days=3)).isoformat()))
        db.commit()

        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: True)
        seen = []

        async def fake(http, sec_code, d_from, d_to, **kw):
            seen.append(sec_code)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=fake):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))
        assert sorted(seen) == ["1001.T", "1002.T"]

    def test_active_company_without_prices_is_never_skipped(
            self, db, make_company, make_price, monkeypatch):
        """`is_active` が True / 未設定の社は毎晩どおり取りに行く。"""
        now_jst, session = _monday_anchor()
        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=True))
        db.add(make_company(edinet_code="E00003", sec_code="1003"))       # is_active 未設定
        db.add(make_price(edinet_code="E00001",
                          trade_date=(session - timedelta(days=3)).isoformat()))
        db.commit()

        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: False)
        seen = []

        async def fake(http, sec_code, d_from, d_to, **kw):
            seen.append(sec_code)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=fake):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))
        assert sorted(seen) == ["1001.T", "1003.T"]

    def test_backoff_does_not_apply_to_explicit_gap_days(
            self, db, make_company, monkeypatch):
        """`gap_days > 0`（バックフィル用の明示呼び出し）では絞らない。

        毎晩の鮮度確保と違い、こちらは「取りに行くこと」自体が目的の呼び出し。
        """
        db.add(make_company(edinet_code="E00002", sec_code="1002", is_active=False))
        db.add(make_company(edinet_code="E00001", sec_code="1001", is_active=True))
        from database import StockPriceDaily
        db.add(StockPriceDaily(edinet_code="E00001",
                               trade_date=(date.today() - timedelta(days=30)).isoformat(),
                               close=100.0))
        db.commit()

        monkeypatch.setattr("collector_prices.should_retry_priceless_delisted",
                            lambda ec, today: False)
        seen = []

        async def fake(http, sec_code, d_from, d_to, **kw):
            seen.append(sec_code)
            return []

        with patch("collector_prices.fetch_yahoo_history", new=fake):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=7))
        assert sorted(seen) == ["1001.T", "1002.T"], "gap_days>0 でも絞ってしまっている"


# ── Yahoo gap-fill の並行フェッチ (#556) ────────────────────────────────────

class TestYahooGapFillConcurrency:
    """並行フェッチ（#556）の不変条件。

    速くすること自体はテストできない（実測は夜間バッチのログが持つ）。ここで縛るのは
    **速くしても壊れていないこと**の3点:

    1. `YAHOO_STOCK_CONCURRENCY=1` で従来の逐次と同じ順序になる（緊急停止が本当に効く）
    2. 並行度を上げると実際に複数リクエストが同時に飛ぶ（設定が素通りしていない）
    3. **完了順が入れ替わってもカウンタと投入行が変わらない**——`n_new` の加算を
       タスク側へ動かすと並行度に依存して壊れるので、消費側に残してあることを縛る
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def _seed(self, db, make_company, make_price, n=6):
        now_jst, session = _monday_anchor()
        stale = (session - timedelta(days=20)).isoformat()
        for i in range(n):
            db.add(make_company(edinet_code=f"E{i:05d}", sec_code=f"{1001 + i}"))
            db.add(make_price(edinet_code=f"E{i:05d}", trade_date=stale))
        db.commit()
        return now_jst

    def test_concurrency_one_fetches_in_ticker_order(self, db, make_company, make_price):
        now_jst = self._seed(db, make_company, make_price)
        seen = []

        async def _fake(http, ticker, d_from, d_to):
            seen.append(ticker)
            return []

        with (
            patch("collector_prices.fetch_yahoo_history", new=_fake),
            patch("collector_prices.YAHOO_STOCK_CONCURRENCY", 1),
            patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0),
        ):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        assert seen == sorted(seen), "並行度1では従来どおり銘柄順に叩く"
        assert len(seen) == 6

    def test_requests_actually_overlap_when_concurrency_raised(
            self, db, make_company, make_price):
        now_jst = self._seed(db, make_company, make_price)
        inflight, peak = 0, 0

        async def _fake(http, ticker, d_from, d_to):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)          # 他のタスクへ制御を渡す
            inflight -= 1
            return []

        with (
            patch("collector_prices.fetch_yahoo_history", new=_fake),
            patch("collector_prices.YAHOO_STOCK_CONCURRENCY", 4),
            patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0),
        ):
            self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        assert peak > 1, "並行度を上げても1本ずつしか飛んでいない（設定が効いていない）"
        assert peak <= 4, "Semaphore の上限を超えている"

    def test_counters_survive_out_of_order_completion(self, db, make_company, make_price):
        """完了順を逆にしても `new_rows` は同じ。

        `n_new` は「その社の従来の最終日より後の日付」を数えるので、**どの社の
        `last_iso` と突き合わせるか**を取り違えると壊れる。並行化で完了順が変わっても
        社と `last_iso` の対応が保たれることを、遅延を逆順に入れて確かめる。
        """
        now_jst, session = _monday_anchor()
        for i in range(4):
            db.add(make_company(edinet_code=f"E{i:05d}", sec_code=f"{2001 + i}"))
            db.add(make_price(edinet_code=f"E{i:05d}",
                              trade_date=(session - timedelta(days=20)).isoformat()))
        db.commit()

        order = {"2001.T": 0.004, "2002.T": 0.003, "2003.T": 0.002, "2004.T": 0.001}

        async def _fake(http, ticker, d_from, d_to):
            await asyncio.sleep(order[ticker])       # 銘柄順と完了順を逆にする
            return [{"trade_date": session.isoformat(), "close": 100.0, "volume": 1}]

        with (
            patch("collector_prices.fetch_yahoo_history", new=_fake),
            patch("collector_prices.YAHOO_STOCK_CONCURRENCY", 4),
            patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0),
        ):
            res = self._run(fill_recent_stock_price_gap_yahoo(db, gap_days=0, now_jst=now_jst))

        assert res["new_rows"] == 4          # 4社 × 1日ぶんの新規日付
        assert res["companies"] == 4
        assert res["concurrency"] == 4


class TestYahooHttpErrorStats:
    """`fetch_yahoo_chart` の HTTP 失敗を数える器（#556）。

    この関数は**全エラー経路を `([], {})` へ畳む**ため、429（レート制限）と
    「その銘柄のデータが無い」が呼び出し側から区別できない。並行度を上げる前に、
    絞られたことがログに出る状態を作る必要がある。
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def _session_raising(self, exc):
        class _S:
            async def get(self, *a, **kw):
                raise exc
        return _S()

    def test_counts_429_separately(self):
        import httpx as _httpx
        from collector_prices import fetch_yahoo_chart, yahoo_http_stats

        resp = _httpx.Response(429, request=_httpx.Request("GET", "https://x"))
        exc = _httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        with yahoo_http_stats() as stats:
            rows, meta = self._run(fetch_yahoo_chart(
                self._session_raising(exc), "1001.T", "20260101", "20260131"))
        assert (rows, meta) == ([], {})      # 戻り値の契約は変えない
        assert stats == {"429": 1, "5xx": 0, "4xx": 0, "other": 0}

    def test_unclassifiable_failures_are_still_counted(self):
        """分類できない失敗も必ず1つ数える（0 を「起きなかった」の意味に保つ）。"""
        from collector_prices import fetch_yahoo_chart, yahoo_http_stats

        with yahoo_http_stats() as stats:
            self._run(fetch_yahoo_chart(
                self._session_raising(OSError("connection reset")),
                "1001.T", "20260101", "20260131"))
        assert stats == {"429": 0, "5xx": 0, "4xx": 0, "other": 1}

    def test_counting_is_off_outside_the_context(self):
        """with の外では数えない＝他の呼び出し元（macro / backfill）に副作用を持たせない。"""
        from collector_prices import fetch_yahoo_chart

        # 例外を出さずに完走すること（カウンタが None でも落ちない）
        rows, meta = self._run(fetch_yahoo_chart(
            self._session_raising(OSError("boom")), "1001.T", "20260101", "20260131"))
        assert (rows, meta) == ([], {})
