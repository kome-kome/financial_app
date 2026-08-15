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

import httpx
import pytest


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _const(response: httpx.Response):
    def handler(request):
        return response
    return handler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import (
    JQUANTS_MAX_CONSECUTIVE_FORBIDDEN,
    JQuantsAccessError, JQuantsOutOfCoverage,
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

    def test_syncs_is_active_from_equity_master(self, db, make_company):
        """J-Quants /equities/master の現在の上場銘柄集合と companies.is_active を同期する（#315・#462）。"""
        from database import Company
        self._add_company(db, make_company)   # E00001 / sec_code=1001
        db.add(make_company(edinet_code="E00002", sec_code="1002", name="廃止予定"))
        db.commit()

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_equity_master",
                               new_callable=AsyncMock, return_value=({"1001"}, None)):
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

    def test_equity_master_fetch_failure_skips_sync(self, db, make_company):
        """/equities/master 取得失敗（active_codes 空）時は同期をスキップし、既存 is_active を保つ。"""
        from database import Company
        self._add_company(db, make_company)   # E00001 / sec_code=1001
        db.commit()

        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector_prices.record_prices_batch", return_value=0):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_equity_master",
                               new_callable=AsyncMock, return_value=(set(), None)):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                asyncio.run(
                                    collect_stock_price_history_jquants(
                                        db, date_from=self._MON, date_to=self._MON,
                                    )
                                )

        co1 = db.query(Company).filter_by(edinet_code="E00001").one()
        assert co1.is_active is True   # 誤って全件 delisted 化しない

    # ── 403（契約失効／プラン対象外／URL 不在）の扱い・#412 → #462 ───────────
    # **カバレッジ境界はここに来ない**（境界は 400・下の TestJquantsCoverageWindow）。
    _JQ_ROW = {
        "Code": "10010", "Date": "2024-01-09",
        "AdjO": 1000.0, "AdjH": 1010.0, "AdjL": 990.0, "AdjC": 1005.0, "AdjVo": 10000.0,
    }

    def _run_jq(self, db, fetch_mock, active_codes):
        with patch("collector_prices._jquants_fetch_date", new=fetch_mock):
            with patch("collector_prices.record_prices_batch", return_value=1):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_equity_master",
                               new_callable=AsyncMock, return_value=(active_codes, None)):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                return asyncio.run(
                                    collect_stock_price_history_jquants(
                                        db, date_from=self._MON, date_to=self._TUE,
                                    )
                                )

    def test_403_day_is_skipped_and_collection_continues(self, db, make_company):
        """403 の日は欠測扱いでスキップし、後続日の収集を継続する（Issue #412）。"""
        self._add_company(db, make_company)

        async def fetch(session, api_key, date_str):
            if date_str == self._MON.strftime("%Y-%m-%d"):
                raise JQuantsAccessError(date_str)
            return [self._JQ_ROW]

        result = self._run_jq(db, fetch, {"1001"})

        assert result["cancelled"] is False
        assert result["forbidden"] == 1
        assert result["upserted"] == 1     # 403 の翌営業日は正常に upsert される

    def test_all_403_with_valid_key_does_not_raise(self, db, make_company):
        """全日程 403 でも例外は投げず警告のみで完走する（#425 の構造を維持）。"""
        self._add_company(db, make_company)

        async def fetch(session, api_key, date_str):
            raise JQuantsAccessError(date_str)

        result = self._run_jq(db, fetch, {"1001"})

        assert result["cancelled"] is False
        assert result["forbidden"] == result["days"] == 2
        assert result["upserted"] == 0
        assert result["all_forbidden"] is True

    def test_all_403_and_master_failure_does_not_raise(self, db, make_company):
        """全日程 403 かつ上場銘柄一覧も失敗しても例外を投げない（Issue #425 の構造を維持）。

        J-Quants（収集元A）の失敗が、同じキーに依存しない Yahoo ギャップ補完（収集元B・
        株価鮮度の実質唯一の担い手）まで巻き添えで止めるのは誤り。継続可否は結果を見て
        呼び出し側が決める。
        """
        self._add_company(db, make_company)

        async def fetch(session, api_key, date_str):
            raise JQuantsAccessError(date_str)

        result = self._run_jq(db, fetch, set())   # listed/info 失敗＝active_codes 空

        assert result["cancelled"] is False
        assert result["forbidden"] == result["days"] == 2
        assert result["all_forbidden"] is True

    # ── 契約失効の分離と連続403の早期打ち切り・Issue #461 ──────────────────
    def _run_jq_window(self, db, fetch_mock, days: int):
        """days 営業日ぶんの窓で収集する（早期打ち切りの検証用）。"""
        with patch("collector_prices._jquants_fetch_date", new=fetch_mock):
            with patch("collector_prices.record_prices_batch", return_value=1):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_equity_master",
                               new_callable=AsyncMock, return_value=(set(), None)):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                return asyncio.run(
                                    collect_stock_price_history_jquants(
                                        db, date_from=date(2024, 1, 1),
                                        date_to=date(2024, 1, 1) + timedelta(days=days),
                                    )
                                )

    def test_consecutive_403_aborts_remaining_days(self, db, make_company):
        """連続 403 が閾値に達したら残り日数を叩かない（#461）。

        契約失効・権限喪失は全日 403 になるため、打ち切らないと窓の長さ ×
        JQUANTS_RATE_SLEEP(20秒) を丸ごと捨てる（本番実測: 523営業日で174分を空振りに使い
        full-pipeline finalize が timeout・run 31126473273）。
        """
        self._add_company(db, make_company)
        attempted: list[str] = []

        async def fetch(session, api_key, date_str):
            attempted.append(date_str)
            raise JQuantsAccessError(date_str, reason="no_subscription")

        result = self._run_jq_window(db, fetch, days=40)

        assert len(attempted) == JQUANTS_MAX_CONSECUTIVE_FORBIDDEN
        assert result["aborted_days"] > 0
        assert result["forbidden"] + result["aborted_days"] == result["days"]
        assert result["all_forbidden"] is True     # 1日も取れていない事実は保たれる
        assert result["cancelled"] is False        # 例外は投げない（#425 の構造を維持）

    def test_success_resets_consecutive_counter(self, db, make_company):
        """間に成功日が挟まれば打ち切らない（403 が飛び飛びの窓を捨てない）。"""
        self._add_company(db, make_company)
        attempted: list[str] = []

        async def fetch(session, api_key, date_str):
            attempted.append(date_str)
            # 5日ごとに1日成功させる＝連続403が閾値へ届かない
            if len(attempted) % 5 == 0:
                return [{**self._JQ_ROW, "Date": date_str}]
            raise JQuantsAccessError(date_str)

        result = self._run_jq_window(db, fetch, days=40)

        assert result["aborted_days"] == 0
        assert len(attempted) == result["days"]
        assert result["all_forbidden"] is False

    def test_no_subscription_is_counted_separately(self, db, make_company):
        """契約失効は他の403と区別して数える（平常運転と読み違えない・#461）。"""
        self._add_company(db, make_company)

        async def fetch(session, api_key, date_str):
            raise JQuantsAccessError(date_str, reason="no_subscription")

        result = self._run_jq(db, fetch, set())
        assert result["no_subscription"] == result["forbidden"] == 2

    def test_other_403_is_not_counted_as_subscription_lapse(self, db, make_company):
        """プラン対象外・URL 不在の403を失効として数えない（対処が別物・#462）。"""
        self._add_company(db, make_company)

        async def fetch(session, api_key, date_str):
            raise JQuantsAccessError(date_str, reason="endpoint_missing")

        result = self._run_jq(db, fetch, set())
        assert result["no_subscription"] == 0
        assert result["forbidden"] == 2

    def test_partial_403_is_not_all_forbidden(self, db, make_company):
        """一部日だけ 403 なら all_forbidden は False（キー失効の誤検知を避ける）。"""
        self._add_company(db, make_company)

        async def fetch(session, api_key, date_str):
            if date_str == self._MON.strftime("%Y-%m-%d"):
                raise JQuantsAccessError(date_str)
            return [self._JQ_ROW]

        result = self._run_jq(db, fetch, set())

        assert result["forbidden"] == 1
        assert result["all_forbidden"] is False


class TestJquantsCoverageWindow:
    """契約カバレッジ窓（400）の扱い・#462。

    **403 はカバレッジ境界を意味しない**。2026-08-08 に契約有効な状態で実 API を叩いた結果、
    窓の外側（過去側・エンバーゴ側の両端）は 400 で
    `Your subscription covers the following dates: 2024-05-16 ~ 2026-05-16` を返した。
    #412 / #425 が「403＝カバー範囲外」と読んだ観測は、実際には契約失効だった（#461）。
    """

    _JQ_ROW = {
        "Code": "10010", "Date": "2024-01-09",
        "AdjO": 1000.0, "AdjH": 1010.0, "AdjL": 990.0, "AdjC": 1005.0, "AdjVo": 10000.0,
    }

    def _add_company(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト社"))
        db.commit()

    def _run(self, db, fetch_mock, date_from, date_to):
        with patch("collector_prices._jquants_fetch_date", new=fetch_mock):
            with patch("collector_prices.record_prices_batch", return_value=1):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_equity_master",
                               new_callable=AsyncMock, return_value=(set(), None)):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                return asyncio.run(collect_stock_price_history_jquants(
                                    db, date_from=date_from, date_to=date_to))

    def test_400_with_coverage_message_raises_out_of_coverage(self):
        """400＋covers 文言は非営業日の 400 と区別して送出する（#462）。"""
        from collector_prices import _jquants_fetch_date

        body = ('{"message": "Your subscription covers the following dates: '
                '2024-05-16 ~ 2026-05-16. If you want more data..."}')
        client = _client(_const(httpx.Response(400, text=body)))
        with pytest.raises(JQuantsOutOfCoverage) as ei:
            asyncio.run(_jquants_fetch_date(client, "key", "2026-05-18"))
        assert ei.value.cover_from == "2024-05-16"
        assert ei.value.cover_to == "2026-05-16"

    def test_400_without_coverage_message_is_holiday(self):
        """covers 文言の無い 400 は非営業日＝空リストで正常終了（従来どおり）。"""
        from collector_prices import _jquants_fetch_date

        client = _client(_const(httpx.Response(400, text='{"message": "no data"}')))
        assert asyncio.run(_jquants_fetch_date(client, "key", "2024-01-01")) == []

    def test_learned_window_skips_without_http_call(self, db, make_company):
        """窓を1日学習したら、窓外の残り日は**リクエストせず**に飛ばす（#462）。

        叩いてから 400 を受ける実装だと 1日あたり JQUANTS_RATE_SLEEP=20秒 を捨てる。
        730日窓では上限側の約60営業日が該当し、約20分の空振りになる。
        """
        self._add_company(db, make_company)
        attempted: list = []

        async def fetch(session, api_key, date_str):
            attempted.append(date_str)
            # 窓は 2024-01-01 〜 2024-01-05。以降の日付はすべて窓外
            raise JQuantsOutOfCoverage(date_str, "2024-01-01", "2024-01-05")

        result = self._run(db, fetch, date(2024, 1, 8), date(2024, 1, 19))

        assert len(attempted) == 1                      # 学習は1回で足りる
        assert result["out_of_coverage"] == result["days"] == 10
        assert result["forbidden"] == 0                 # 403 とは別集計（#462）
        assert result["all_forbidden"] is False         # 窓外は異常ではない

    def test_in_window_days_are_still_fetched(self, db, make_company):
        """窓内の日付は学習後も従来どおり取得する（窓の下限側で1日踏んだだけで止めない）。"""
        self._add_company(db, make_company)
        attempted: list = []

        async def fetch(session, api_key, date_str):
            attempted.append(date_str)
            if date_str < "2024-01-10":
                raise JQuantsOutOfCoverage(date_str, "2024-01-10", "2024-01-31")
            return [{**self._JQ_ROW, "Date": date_str}]

        result = self._run(db, fetch, date(2024, 1, 8), date(2024, 1, 12))

        # 01-08 が窓外 → 学習。01-09 は窓外なので叩かない。01-10〜01-12 は窓内で取得。
        assert attempted == ["2024-01-08", "2024-01-10", "2024-01-11", "2024-01-12"]
        assert result["out_of_coverage"] == 2
        assert result["upserted"] == 3

    def test_embargo_days_are_learned_not_hardcoded(self, db, make_company):
        """エンバーゴ境界は**API から学習**し、日数をコードに決め打ちしない（#462）。

        `today − 84日` のような固定値で窓を切ると、プランやエンバーゴが変わったときに
        取れるはずのデータを黙って捨てる（ログは平常時と区別がつかない）＝#438/#461 と
        同型の静かな劣化。学習型なら境界を1日踏むコスト（20秒）だけで自動追随する。

        **窓の下限は平日へ寄せる（#484）**: `collect_stock_price_history_jquants` は
        `weekday() < 5` で土日をリクエスト対象から外すため、`today − 60日` が週末に
        当たる週は最古の試行日が翌営業日へずれる。暦日をそのまま期待すると曜日次第で
        落ちる（2026-08-13 は `today − 60日` が日曜で main が赤になった）。
        """
        self._add_company(db, make_company)
        attempted: list = []
        today = date.today()
        date_from = today - timedelta(days=60)
        while date_from.weekday() >= 5:
            date_from += timedelta(days=1)
        # API 側のエンバーゴが 40日へ短縮された想定。決め打ち 84日なら取り逃す領域。
        cutoff = today - timedelta(days=40)

        async def fetch(session, api_key, date_str):
            attempted.append(date_str)
            if date_str > cutoff.strftime("%Y-%m-%d"):
                raise JQuantsOutOfCoverage(
                    date_str, "2024-01-01", cutoff.strftime("%Y-%m-%d"))
            return [{**self._JQ_ROW, "Date": date_str}]

        result = self._run(db, fetch, date_from, today - timedelta(days=30))

        # 短縮後に取得可能になった 60〜40日前は取り逃さない
        assert min(attempted) == date_from.strftime("%Y-%m-%d")
        assert result["upserted"] > 0
        # 学習後、窓外の残りは叩かない（境界を踏むのは1日だけ）
        assert sum(1 for d in attempted if d > cutoff.strftime("%Y-%m-%d")) == 1

    def test_fully_embargoed_window_costs_one_request(self, db, make_company):
        """窓が丸ごとエンバーゴ内なら、1日だけ叩いて残りは学習でスキップする（#462）。"""
        self._add_company(db, make_company)
        attempted: list = []
        today = date.today()

        async def fetch(session, api_key, date_str):
            attempted.append(date_str)
            raise JQuantsOutOfCoverage(
                date_str, "2024-01-01", (today - timedelta(days=84)).strftime("%Y-%m-%d"))

        result = self._run(db, fetch, today - timedelta(days=10), today)

        assert len(attempted) == 1
        assert result["upserted"] == 0
        assert result["out_of_coverage"] == result["days"]


class TestMasterAsOfProtectsNewListings:
    """マスタの as-of より後に上場した銘柄を delisted にしない（#463）。

    J-Quants 無料プランは `/equities/master` もエンバーゴし、実測の as-of は「今日−84日」
    だった。この日より後に新規上場した銘柄はマスタに載らないため、「載っていない＝廃止」と
    読むと IPO 直後の銘柄を誤って `is_active=False` にする。2026-08-08 の本番実走で
    589A / 607A の2社が実際にこれで落ちた（前日まで値がついていた）。
    """

    def test_recently_listed_company_is_not_delisted(self, db, make_company, make_price):
        """価格履歴が as-of より後に始まる＝ as-of 時点で未上場 → 保護（589A 実例）。"""
        from database import Company, sync_active_status

        db.add(make_company(edinet_code="E00001", sec_code="1001", name="既存上場"))
        db.add(make_company(edinet_code="E00002", sec_code="589A", name="新規上場"))
        db.commit()
        db.add(make_price(edinet_code="E00002", trade_date="2026-06-30", close=1500.0))
        db.add(make_price(edinet_code="E00002", trade_date="2026-08-07", close=1600.0))
        db.commit()

        # master は 1001 のみ・as-of は 2026-05-15（エンバーゴ境界）
        result = sync_active_status(db, {"1001"}, master_as_of="2026-05-15")

        assert result["delisted"] == 0
        assert result["protected"] == 1
        assert db.query(Company).filter_by(sec_code="589A").one().is_active is True

    def test_delisted_just_after_as_of_is_still_flagged(self, db, make_company, make_price):
        """as-of の直後に廃止した銘柄は保護しない（3593/4917/6901 実例・#463 の要注意ケース）。

        「as-of より後にも取引がある」を保護条件にするとこの3社まで救ってしまう。
        as-of 前から履歴がある＝ as-of 時点で上場していた＝マスタに載るはずだった、が正しい読み。
        """
        from database import Company, sync_active_status

        db.add(make_company(edinet_code="E00001", sec_code="1001", name="既存上場"))
        db.add(make_company(edinet_code="E00003", sec_code="3593", name="as-of直後に廃止"))
        db.commit()
        db.add(make_price(edinet_code="E00003", trade_date="2026-02-06", close=800.0))
        db.add(make_price(edinet_code="E00003", trade_date="2026-05-19", close=810.0))
        db.commit()

        result = sync_active_status(db, {"1001"}, master_as_of="2026-05-15")

        assert result["delisted"] == 1
        assert result["protected"] == 0
        assert db.query(Company).filter_by(sec_code="3593").one().is_active is False

    def test_company_without_prices_is_still_flagged(self, db, make_company):
        """株価が1行も無い社は保護対象外（本番 699件中 648件がこれ）。"""
        from database import Company, sync_active_status

        db.add(make_company(edinet_code="E00004", sec_code="8888", name="株価なし"))
        db.commit()

        result = sync_active_status(db, {"1001"}, master_as_of="2026-05-15")

        assert result["protected"] == 0
        assert db.query(Company).filter_by(sec_code="8888").one().is_active is False

    def test_without_as_of_behaviour_is_unchanged(self, db, make_company, make_price):
        """as-of 未指定なら従来どおり（呼び出し側が渡し忘れても壊れないこと）。"""
        from database import Company, sync_active_status

        db.add(make_company(edinet_code="E00002", sec_code="589A", name="新規上場"))
        db.commit()
        db.add(make_price(edinet_code="E00002", trade_date="2026-08-07", close=1500.0))
        db.commit()

        result = sync_active_status(db, set())

        assert result["delisted"] == 1
        assert result["protected"] == 0
        assert db.query(Company).filter_by(sec_code="589A").one().is_active is False
