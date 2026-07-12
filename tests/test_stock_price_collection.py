"""株価収集関数のユニットテスト（stooq版 / JQuants版）。

collector.py の HTTP ヘルパー（fetch_stock_history_stooq / _jquants_fetch_date）と
DB 書き込み（record_prices_batch / trim_daily）をモックすることで
ネットワーク通信・PostgreSQL なしで各収集ブランチを検証する。
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import (
    backfill_historical_stock_prices_yahoo,
    backfill_weekly_history_yahoo,
    collect_stock_price_history,
    collect_stock_price_history_jquants,
)
from database import FinancialRecord


def _collect_with_capture(db, **kwargs):
    """collect_stock_price_history を実行し、stooq へ渡された (sec_code, d_from, d_to)
    のフェッチ呼び出しを順序どおり捕捉して返すヘルパー。"""
    fetch_calls: list = []

    async def mock_fetch(session, sec_code, d_from, d_to):
        fetch_calls.append((sec_code, d_from, d_to))
        return []

    with patch("collector_prices.fetch_stock_history_stooq", new=mock_fetch):
        with patch("collector_prices.record_prices_batch", return_value=0):
            with patch("collector_prices.trim_daily", return_value=0):
                asyncio.run(collect_stock_price_history(db, **kwargs))
    return fetch_calls


# ── stooq版：collect_stock_price_history ─────────────────────────────────────

class TestCollectStooqHistory:
    """stooq 経由の株価差分収集ロジックのテスト（HTTP 通信・DB 書き込みはモック）。"""

    def test_skip_existing_skips_up_to_date_companies(self, db, make_company, make_weekly):
        """skip_existing=True: 最新日付が昨日以降の企業はスキップされ skipped カウントが正しく返る。"""
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add(make_weekly(edinet_code="E00001", trade_date=yesterday, close_last=1000.0))
        db.commit()

        with patch("collector_prices.fetch_stock_history_stooq",
                   new_callable=AsyncMock, return_value=[]) as mock_fetch:
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    result = asyncio.run(
                        collect_stock_price_history(db, skip_existing=True)
                    )

        assert result["skipped"] == 1
        assert result["cancelled"] is False
        # 最新済みのため HTTP フェッチは発生しない
        mock_fetch.assert_not_called()

    def test_backfill_adds_both_forward_and_backward_gaps(self, db, make_company, make_weekly):
        """backfill=True: 前方差分と後方欠損の両方が to_fetch に積まれ 2 回フェッチされる。"""
        today = date.today()
        # 3 か月前の週次レコード 1 件（前後に gap が生まれる位置）
        three_months_ago = (today - timedelta(days=90)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add(make_weekly(edinet_code="E00001", trade_date=three_months_ago, close_last=1000.0))
        db.commit()

        fetch_calls: list = []

        async def mock_fetch(session, sec_code, d_from, d_to):
            fetch_calls.append((sec_code, d_from, d_to))
            return []

        with patch("collector_prices.fetch_stock_history_stooq", new=mock_fetch):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    result = asyncio.run(
                        collect_stock_price_history(
                            db, years_back=1, skip_existing=True, backfill=True
                        )
                    )

        # 前方（3ヶ月前の翌日 → 今日）と後方（1年前 → 3ヶ月前の前日）の計 2 件
        assert len(fetch_calls) == 2
        sec_codes = [c[0] for c in fetch_calls]
        assert all(sc == "1001" for sc in sec_codes)
        assert result["cancelled"] is False

    def test_backfill_forward_only_when_history_starts_at_range_start(
        self, db, make_company, make_weekly
    ):
        """backfill=True: 最古レコードが years_back 起点ちょうど（後方欠損なし）かつ
        最新が古い場合、前方差分のみが to_fetch に積まれる。"""
        today = date.today()
        date_from = date(today.year - 1, today.month, today.day)
        date_from_str = date_from.strftime("%Y-%m-%d")
        # 最古=最新が years_back 起点ちょうど → 後方欠損は発生せず、前方のみ
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add(make_weekly(edinet_code="E00001", trade_date=date_from_str, close_last=1000.0))
        db.commit()

        fetch_calls = _collect_with_capture(db, years_back=1, skip_existing=True, backfill=True)

        expected_d1_fwd = (date_from + timedelta(days=1)).strftime("%Y%m%d")
        expected_d2 = today.strftime("%Y%m%d")
        assert fetch_calls == [("1001", expected_d1_fwd, expected_d2)]

    def test_backfill_backward_only_when_history_is_current(
        self, db, make_company, make_weekly
    ):
        """backfill=True: 最新が昨日（前方差分なし）かつ最古が years_back 起点より後の
        場合、後方欠損のみが to_fetch に積まれる。"""
        today = date.today()
        date_from = date(today.year - 1, today.month, today.day)
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add(make_weekly(edinet_code="E00001", trade_date=yesterday, close_last=1000.0))
        db.commit()

        fetch_calls = _collect_with_capture(db, years_back=1, skip_existing=True, backfill=True)

        expected_d1 = date_from.strftime("%Y%m%d")
        expected_d2_bwd = (today - timedelta(days=2)).strftime("%Y%m%d")
        assert fetch_calls == [("1001", expected_d1, expected_d2_bwd)]

    def test_backfill_skips_when_no_gaps(self, db, make_company, make_weekly):
        """backfill=True: 履歴が years_back 起点〜昨日を完全カバーしている場合、
        前方・後方とも欠損なしでスキップされフェッチは発生しない。"""
        today = date.today()
        date_from = date(today.year - 1, today.month, today.day)
        date_from_str = date_from.strftime("%Y-%m-%d")
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add(make_weekly(edinet_code="E00001", trade_date=date_from_str, close_last=900.0))
        db.add(make_weekly(edinet_code="E00001", trade_date=yesterday, close_last=1000.0))
        db.commit()

        with patch("collector_prices.fetch_stock_history_stooq",
                   new_callable=AsyncMock, return_value=[]) as mock_fetch:
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    result = asyncio.run(
                        collect_stock_price_history(
                            db, years_back=1, skip_existing=True, backfill=True
                        )
                    )

        mock_fetch.assert_not_called()
        assert result["skipped"] == 1

    def test_backfill_full_range_when_no_history(self, db, make_company):
        """backfill=True: 週次レコードが1件も無い企業は years_back 起点→今日の
        全範囲が1件 to_fetch に積まれる。"""
        today = date.today()
        date_from = date(today.year - 1, today.month, today.day)
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.commit()

        fetch_calls = _collect_with_capture(db, years_back=1, skip_existing=True, backfill=True)

        assert fetch_calls == [
            ("1001", date_from.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
        ]

    def test_cancel_check_stops_collection(self, db, make_company):
        """cancel_check が True を返すと処理が中断され cancelled: True が返る。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.commit()

        with patch("collector_prices.fetch_stock_history_stooq",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    result = asyncio.run(
                        collect_stock_price_history(
                            db, skip_existing=False, cancel_check=lambda: True
                        )
                    )

        assert result["cancelled"] is True


# ── Yahoo backfill：backfill_historical_stock_prices_yahoo ───────────────────

class TestBackfillYahooNearestMatch:
    """Yahoo backfill の period_end 近傍マッチングの境界値テスト（fetch_yahoo_history
    をモックし、_nearest_price 経由の最近傍選択を統合レベルで検証する）。"""

    # default period_end="2023-03-31" は cutoff（today-730日）より前で backfill 対象。
    _PERIOD_END = "2023-03-31"

    def _run(self, db, rows):
        async def mock_fetch(session, ticker, d_from, d_to):
            return rows

        with patch("collector_prices.fetch_yahoo_history", new=mock_fetch):
            with patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0):
                return asyncio.run(backfill_historical_stock_prices_yahoo(db))

    def test_picks_nearest_when_both_sides_present(self, db, make_company, make_fin):
        """period_end の前後どちらにも候補があるとき、より近い日付の終値を採用する。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_fin(period_end=self._PERIOD_END))
        db.commit()

        # 2023-03-31 に対し前(03-20:11日)より後(04-01:1日)が近い
        updated = self._run(db, [
            {"trade_date": "2023-03-20", "close": 900.0},
            {"trade_date": "2023-04-01", "close": 1100.0},
        ])

        assert updated == 1
        rec = db.query(FinancialRecord).first()
        assert rec.stock_price == 1100.0

    def test_no_update_when_gap_exceeded(self, db, make_company, make_fin):
        """最近傍でも MAX_GAP_DAYS(30日)を超える場合は更新しない。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_fin(period_end=self._PERIOD_END))
        db.commit()

        # 2023-03-31 から最も近い候補でも 60 日以上離れている
        updated = self._run(db, [
            {"trade_date": "2023-01-15", "close": 900.0},
            {"trade_date": "2023-06-15", "close": 1100.0},
        ])

        assert updated == 0
        rec = db.query(FinancialRecord).first()
        assert rec.stock_price is None

    def test_no_target_records_returns_zero(self, db, make_company, make_fin):
        """stock_price が既に埋まっているレコードは対象外（NULL のみ補完）。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_fin(period_end=self._PERIOD_END, stock_price=500.0))
        db.commit()

        updated = self._run(db, [{"trade_date": "2023-03-30", "close": 1000.0}])
        assert updated == 0


# ── 週次バックフィル：backfill_weekly_history_yahoo（#198）────────────────────

class TestBackfillWeeklyHistoryYahoo:
    """stock_price_weekly 過去延伸の対象選定ロジックを検証する。

    record_prices_batch は Postgres 専用（pg_insert）のためモックし、fetch_yahoo_history へ
    渡される (ticker, d_from, d_to) を捕捉して「どの社をどの範囲で取得するか」を確認する。
    """

    def _run(self, db, years_back=5):
        fetch_calls: list = []

        async def mock_fetch(session, ticker, d_from, d_to):
            fetch_calls.append((ticker, d_from, d_to))
            return []  # 取得 0 件（保存経路は別途モック）

        with patch("collector_prices.fetch_yahoo_history", new=mock_fetch):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0):
                    result = asyncio.run(
                        backfill_weekly_history_yahoo(db, years_back=years_back))
        return result, fetch_calls

    def test_skips_already_covered_company(self, db, make_company, make_weekly):
        """週次の最古日が floor(today-years_back) 以前の社は取得対象外。"""
        old = (date.today() - timedelta(days=365 * 6)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_weekly(edinet_code="E00001", trade_date=old, close_last=1000.0))
        db.commit()

        result, fetch_calls = self._run(db, years_back=5)
        assert fetch_calls == []
        assert result.get("skipped") is True
        assert result.get("companies") == 0

    def test_extends_company_with_recent_only_weekly(self, db, make_company, make_weekly):
        """週次が直近2年分しかない社は floor〜(最古日-1) で取得される。"""
        oldest = (date.today() - timedelta(days=365 * 2)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_weekly(edinet_code="E00001", trade_date=oldest, close_last=1000.0))
        db.commit()

        result, fetch_calls = self._run(db, years_back=5)
        assert len(fetch_calls) == 1
        ticker, d_from, d_to = fetch_calls[0]
        assert ticker == "1001.T"
        floor_d = date(date.today().year - 5, date.today().month, date.today().day)
        expected_to = (date.fromisoformat(oldest) - timedelta(days=1)).strftime("%Y%m%d")
        assert d_from == floor_d.strftime("%Y%m%d")
        assert d_to == expected_to
        assert result.get("companies") == 1

    def test_fetches_full_range_when_no_weekly(self, db, make_company):
        """週次が未収集の社は floor〜today の全範囲で取得される。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.commit()

        _, fetch_calls = self._run(db, years_back=5)
        assert len(fetch_calls) == 1
        ticker, d_from, d_to = fetch_calls[0]
        assert ticker == "1001.T"
        assert d_to == date.today().strftime("%Y%m%d")


# ── JQuants版：collect_stock_price_history_jquants ───────────────────────────

class TestCollectJQuantsHistory:
    """JQuants 経由の日次株価収集ロジックのテスト（HTTP 通信・DB 書き込みはモック）。"""

    # 祝日の影響を受けない固定月曜日
    _MON = date(2024, 1, 8)
    _TUE = date(2024, 1, 9)

    def _add_company(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.commit()

    def test_normal_upsert_reflects_in_result(self, db, make_company):
        """正常系: 取得した日次 OHLCV が DB に upsert され件数が返り値に反映される。"""
        self._add_company(db, make_company)
        jquants_row = {
            "Code": "10010", "Date": "2024-01-08",
            "O": 1000.0, "H": 1010.0, "L": 990.0, "C": 1005.0, "Vo": 10000.0,
            "AdjO": 1000.0, "AdjH": 1010.0, "AdjL": 990.0, "AdjC": 1005.0, "AdjVo": 10000.0,
            "AdjFactor": 1.0,
        }

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[jquants_row]):
            with patch("collector_prices.record_prices_batch", return_value=1) as mock_batch:
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                            result = asyncio.run(
                                collect_stock_price_history_jquants(
                                    db, date_from=self._MON, date_to=self._MON,
                                )
                            )

        assert result["cancelled"] is False
        assert result["upserted"] == 1
        mock_batch.assert_called_once()

    def test_uses_adjusted_close_not_unadjusted(self, db, make_company):
        """株式分割で AdjC と C が乖離するケース: 保存される close は調整後 AdjC を使う（Issue #314）。"""
        self._add_company(db, make_company)
        jquants_row = {
            "Code": "10010", "Date": "2024-01-08",
            "O": 2000.0, "H": 2020.0, "L": 1980.0, "C": 2010.0, "Vo": 5000.0,
            "AdjO": 1000.0, "AdjH": 1010.0, "AdjL": 990.0, "AdjC": 1005.0, "AdjVo": 10000.0,
            "AdjFactor": 0.5,
        }

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[jquants_row]):
            with patch("collector_prices.record_prices_batch", return_value=1) as mock_batch:
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                            asyncio.run(
                                collect_stock_price_history_jquants(
                                    db, date_from=self._MON, date_to=self._MON,
                                )
                            )

        saved_batch = mock_batch.call_args[0][1]
        assert saved_batch[0]["close"] == 1005.0
        assert saved_batch[0]["volume"] == 10000.0

    def test_cancel_check_stops_jquants(self, db, make_company):
        """cancel_check が True を返すと処理が中断され cancelled: True が返る。"""
        self._add_company(db, make_company)

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                            result = asyncio.run(
                                collect_stock_price_history_jquants(
                                    db,
                                    date_from=self._MON,
                                    date_to=self._TUE,
                                    cancel_check=lambda: True,
                                )
                            )

        assert result["cancelled"] is True

    def test_empty_api_response_yields_zero_upserts(self, db, make_company):
        """API が空レスポンスを返す日付（非営業日等）は upsert されない。"""
        self._add_company(db, make_company)

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0) as mock_batch:
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                            result = asyncio.run(
                                collect_stock_price_history_jquants(
                                    db,
                                    date_from=self._MON,
                                    date_to=self._TUE,
                                )
                            )

        assert result["cancelled"] is False
        assert result["upserted"] == 0
        # 空レスポンスのため record_prices_batch は呼ばれない
        mock_batch.assert_not_called()

    def test_syncs_is_active_from_listed_info(self, db, make_company):
        """J-Quants listed/info の現在の上場銘柄集合と companies.is_active を同期する（Issue #315）。"""
        from database import Company
        self._add_company(db, make_company)   # E00001 / sec_code=1001
        db.add(make_company(edinet_code="E00002", sec_code="1002", name="廃止予定"))
        db.commit()

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_listed_info",
                               new_callable=AsyncMock,
                               return_value={"issued_shares": {}, "active_codes": {"1001"}}):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                asyncio.run(
                                    collect_stock_price_history_jquants(
                                        db, date_from=self._MON, date_to=self._MON,
                                    )
                                )

        co1 = db.query(Company).filter_by(edinet_code="E00001").one()
        co2 = db.query(Company).filter_by(edinet_code="E00002").one()
        assert co1.is_active is True
        assert co2.is_active is False
        assert co2.delisted_date is not None

    def test_listed_info_fetch_failure_skips_sync(self, db, make_company):
        """listed/info 取得失敗（active_codes 空）時は同期をスキップし、既存 is_active を保つ。"""
        from database import Company
        self._add_company(db, make_company)   # E00001 / sec_code=1001
        db.commit()

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_listed_info",
                               new_callable=AsyncMock,
                               return_value={"issued_shares": {}, "active_codes": set()}):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                asyncio.run(
                                    collect_stock_price_history_jquants(
                                        db, date_from=self._MON, date_to=self._MON,
                                    )
                                )

        co1 = db.query(Company).filter_by(edinet_code="E00001").one()
        assert co1.is_active is True   # 誤って全件 delisted 化しない
