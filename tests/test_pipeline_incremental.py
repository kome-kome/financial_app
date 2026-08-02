"""_pipeline_incremental.main() の差分収集固有の制御フローを検証する。

外部 I/O（DB・ネットワーク）は全てモックし、Phase 4 の
「J-Quants 直近取得 → catchup（Yahoo 暫定値を J-Quants 公式値で上書き）→
Yahoo gap-fill → financial_records 反映」の呼び出し制御と、
Phase 1 キャンセル時の早期終了を検証する。test_pipeline_utils.py の方針に倣う。
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _pipeline_incremental as pinc


def _run_main_with_mocks(*, cancelled=False):
    """main() の外部依存をモックし、各モックを返す。実 DB / 通信なし。"""
    mocks = {
        "log": MagicMock(),                      # ファイル書き込みを抑止
        "init_db": MagicMock(),
        "SessionLocal": MagicMock(return_value=MagicMock()),
        "run_full_collection": AsyncMock(return_value=cancelled),  # 戻り値=cancelled
        "collect_macro_data": AsyncMock(return_value=0),
        "collect_stock_price_history_jquants": AsyncMock(return_value={"upserted": 0}),
        "fill_recent_stock_price_gap_yahoo": AsyncMock(
            return_value={"skipped": False, "upserted": 0, "from": "a", "to": "b"}),
        "update_market_data_from_history": MagicMock(return_value=0),
    }
    with patch.multiple(pinc, **mocks):
        asyncio.run(pinc.main())
    return mocks


class TestPhase4Control:
    def test_jquants_catchup_and_yahoo_gapfill_sequence(self):
        mocks = _run_main_with_mocks(cancelled=False)

        jq = mocks["collect_stock_price_history_jquants"]
        # J-Quants は catchup の1回のみ。直近 days_back=14 の取得は 84日エンバーゴ内で
        # 構造的に常に0件かつ全日403で中断ガードを誤発火させたため撤去した（#419 / #425）
        assert jq.await_count == 1
        catchup = jq.await_args_list[0]
        # catchup は today-90 〜 today-80（J-Quants 公式値で Yahoo 暫定値を上書きする経路）
        assert catchup.kwargs["date_from"] == date.today() - timedelta(days=90)
        assert catchup.kwargs["date_to"] == date.today() - timedelta(days=80)
        assert "days_back" not in catchup.kwargs   # catchup は明示日付指定

        # Yahoo gap-fill が gap_days=0 で必ず走る（steady-state の直近補完フォールバック）
        gap = mocks["fill_recent_stock_price_gap_yahoo"]
        assert gap.await_count == 1
        assert gap.await_args.kwargs["gap_days"] == 0

        # 最後に financial_records.stock_price へ反映（point_in_time=False のデフォルト）
        assert mocks["update_market_data_from_history"].call_count == 1

    def test_macro_collection_runs_before_market_phase(self):
        mocks = _run_main_with_mocks(cancelled=False)
        assert mocks["collect_macro_data"].await_count == 1

    def test_yahoo_runs_before_jquants(self):
        """鮮度を担う Yahoo を J-Quants より先に走らせる（#425）。

        片方の収集元の障害がもう片方を巻き添えにしないための順序。
        """
        order = []

        async def _yahoo(*a, **kw):
            order.append("yahoo")
            return {"skipped": False, "upserted": 0, "from": "a", "to": "b"}

        async def _jq(*a, **kw):
            order.append("jquants")
            return {"upserted": 0}

        with patch.multiple(
            pinc,
            log=MagicMock(), init_db=MagicMock(),
            SessionLocal=MagicMock(return_value=MagicMock()),
            run_full_collection=AsyncMock(return_value=False),
            collect_macro_data=AsyncMock(return_value=0),
            collect_stock_price_history_jquants=AsyncMock(side_effect=_jq),
            fill_recent_stock_price_gap_yahoo=AsyncMock(side_effect=_yahoo),
            update_market_data_from_history=MagicMock(return_value=0),
        ):
            asyncio.run(pinc.main())

        assert order == ["yahoo", "jquants"]

    def test_jquants_catchup_failure_does_not_block_market_data_update(self):
        """J-Quants catchup が例外を出しても Yahoo 補完と PER/PBR 反映は完了する（#425）。"""
        mocks = {
            "log": MagicMock(),
            "init_db": MagicMock(),
            "SessionLocal": MagicMock(return_value=MagicMock()),
            "run_full_collection": AsyncMock(return_value=False),
            "collect_macro_data": AsyncMock(return_value=0),
            "collect_stock_price_history_jquants": AsyncMock(
                side_effect=ValueError("J-Quants 側の障害")),
            "fill_recent_stock_price_gap_yahoo": AsyncMock(
                return_value={"skipped": False, "upserted": 5, "from": "a", "to": "b"}),
            "update_market_data_from_history": MagicMock(return_value=3),
        }
        with patch.multiple(pinc, **mocks):
            asyncio.run(pinc.main())   # 例外が伝播しないこと自体が検証対象

        assert mocks["fill_recent_stock_price_gap_yahoo"].await_count == 1
        assert mocks["update_market_data_from_history"].call_count == 1


class TestPhase1Cancellation:
    def test_cancellation_skips_all_later_phases(self):
        # Phase 1（XBRL 差分収集）がキャンセルを返したら後続フェーズは実行されない
        mocks = _run_main_with_mocks(cancelled=True)
        assert mocks["collect_macro_data"].await_count == 0
        assert mocks["collect_stock_price_history_jquants"].await_count == 0
        assert mocks["fill_recent_stock_price_gap_yahoo"].await_count == 0
        assert mocks["update_market_data_from_history"].call_count == 0
