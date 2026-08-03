"""_pipeline_gh.main(finalize_only=True) の Phase 5（市場データ）制御フローを検証する。

外部 I/O（DB・ネットワーク）は全てモックし、
「Yahoo ギャップ補完 → J-Quants → financial_records 反映」の順序と、
J-Quants 失敗時に鮮度確保・反映が巻き添えにならないことを検証する（#425 / #426）。
tests/test_pipeline_incremental.py の方針に倣う。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _pipeline_gh as pgh


def _run_finalize_with_mocks(**overrides):
    """finalize-only の外部依存をモックし、各モックを返す。実 DB / 通信なし。"""
    mocks = {
        "log": MagicMock(),                      # ファイル書き込みを抑止
        "init_db": MagicMock(),
        "SessionLocal": MagicMock(return_value=MagicMock()),
        "collect_macro_data": AsyncMock(return_value=0),
        "collect_stock_price_history_jquants": AsyncMock(return_value={"upserted": 0}),
        "fill_recent_stock_price_gap_yahoo": AsyncMock(
            return_value={"skipped": False, "upserted": 0,
                          "from": "a", "to": "b", "companies": 0}),
        "update_market_data_from_history": MagicMock(return_value=0),
    }
    mocks.update(overrides)
    with patch.multiple(pgh, **mocks):
        asyncio.run(pgh.main(5, finalize_only=True))
    return mocks


class TestPhase5Market:
    def test_yahoo_gap_fill_runs_in_finalize(self):
        """finalize でも Yahoo ギャップ補完が gap_days=0 で必ず走る（#426）。

        J-Quants 無料プランは直近84日を配信しないため、これが無いと全件収集を
        何度回しても直近12週の株価が1日も前進しない。
        """
        mocks = _run_finalize_with_mocks()
        gap = mocks["fill_recent_stock_price_gap_yahoo"]
        assert gap.await_count == 1
        assert gap.await_args.kwargs["gap_days"] == 0

    def test_yahoo_runs_before_jquants(self):
        """鮮度を担う Yahoo を J-Quants より先に走らせる（差分パイプラインと同順序・#425）。"""
        order = []

        async def _yahoo(*a, **kw):
            order.append("yahoo")
            return {"skipped": False, "upserted": 0, "from": "a", "to": "b", "companies": 0}

        async def _jq(*a, **kw):
            order.append("jquants")
            return {"upserted": 0}

        _run_finalize_with_mocks(
            fill_recent_stock_price_gap_yahoo=AsyncMock(side_effect=_yahoo),
            collect_stock_price_history_jquants=AsyncMock(side_effect=_jq),
        )
        assert order == ["yahoo", "jquants"]

    def test_jquants_failure_does_not_block_freshness_or_reflection(self):
        """J-Quants が例外を出しても Yahoo 補完と point_in_time 反映は完了する（#425）。"""
        mocks = _run_finalize_with_mocks(
            collect_stock_price_history_jquants=AsyncMock(
                side_effect=ValueError("J-Quants 側の障害")),
            fill_recent_stock_price_gap_yahoo=AsyncMock(
                return_value={"skipped": False, "upserted": 5,
                              "from": "a", "to": "b", "companies": 2}),
            update_market_data_from_history=MagicMock(return_value=3),
        )
        # 例外が伝播しないこと自体が検証対象
        assert mocks["fill_recent_stock_price_gap_yahoo"].await_count == 1
        assert mocks["update_market_data_from_history"].call_count == 1

    def test_point_in_time_reflection_kept(self):
        """finalize は全財務レコードの period_end 近傍株価を設定する（point_in_time=True）。"""
        mocks = _run_finalize_with_mocks()
        upd = mocks["update_market_data_from_history"]
        assert upd.call_count == 1
        assert upd.call_args.kwargs.get("point_in_time") is True

    def test_yahoo_skipped_result_is_logged_not_raised(self):
        """株価テーブルが空で skipped が返っても後続フェーズは継続する。"""
        mocks = _run_finalize_with_mocks(
            fill_recent_stock_price_gap_yahoo=AsyncMock(
                return_value={"skipped": True, "reason": "empty"}),
        )
        assert mocks["collect_stock_price_history_jquants"].await_count == 1
        assert mocks["update_market_data_from_history"].call_count == 1

    def test_finalize_skips_collection_phases(self):
        """finalize-only は Phase 1（XBRL 収集）を実行しない。"""
        collect = AsyncMock(return_value=False)
        _run_finalize_with_mocks(run_full_collection=collect)
        assert collect.await_count == 0
