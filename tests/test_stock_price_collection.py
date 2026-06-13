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

from collector import collect_stock_price_history, collect_stock_price_history_jquants


# ── stooq版：collect_stock_price_history ─────────────────────────────────────

class TestCollectStooqHistory:
    """stooq 経由の株価差分収集ロジックのテスト（HTTP 通信・DB 書き込みはモック）。"""

    def test_skip_existing_skips_up_to_date_companies(self, db, make_company, make_weekly):
        """skip_existing=True: 最新日付が昨日以降の企業はスキップされ skipped カウントが正しく返る。"""
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.add(make_weekly(edinet_code="E00001", trade_date=yesterday, close_last=1000.0))
        db.commit()

        with patch("collector.fetch_stock_history_stooq",
                   new_callable=AsyncMock, return_value=[]) as mock_fetch:
            with patch("collector.record_prices_batch", return_value=0):
                with patch("collector.trim_daily", return_value=0):
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

        with patch("collector.fetch_stock_history_stooq", new=mock_fetch):
            with patch("collector.record_prices_batch", return_value=0):
                with patch("collector.trim_daily", return_value=0):
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

    def test_cancel_check_stops_collection(self, db, make_company):
        """cancel_check が True を返すと処理が中断され cancelled: True が返る。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.commit()

        with patch("collector.fetch_stock_history_stooq",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector.record_prices_batch", return_value=0):
                with patch("collector.trim_daily", return_value=0):
                    result = asyncio.run(
                        collect_stock_price_history(
                            db, skip_existing=False, cancel_check=lambda: True
                        )
                    )

        assert result["cancelled"] is True


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
        }

        with patch("collector._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[jquants_row]):
            with patch("collector.record_prices_batch", return_value=1) as mock_batch:
                with patch("collector.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector.JQUANTS_RATE_SLEEP", 0):
                            result = asyncio.run(
                                collect_stock_price_history_jquants(
                                    db, date_from=self._MON, date_to=self._MON,
                                )
                            )

        assert result["cancelled"] is False
        assert result["upserted"] == 1
        mock_batch.assert_called_once()

    def test_cancel_check_stops_jquants(self, db, make_company):
        """cancel_check が True を返すと処理が中断され cancelled: True が返る。"""
        self._add_company(db, make_company)

        with patch("collector._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector.record_prices_batch", return_value=0):
                with patch("collector.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector.JQUANTS_RATE_SLEEP", 0):
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

        with patch("collector._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[]):
            with patch("collector.record_prices_batch", return_value=0) as mock_batch:
                with patch("collector.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector.JQUANTS_RATE_SLEEP", 0):
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
