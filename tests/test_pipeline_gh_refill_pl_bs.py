"""_pipeline_gh.py の refill-pl-bs 単独モード結線のスモークテスト。

init_db / SessionLocal / refill_pl_bs_from_xbrl をモックし、main(refill_pl_bs=True) が
単独モード分岐に入り、全件（limit=None）で refill_pl_bs_from_xbrl を呼ぶことを検証する。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _pipeline_gh


def test_main_refill_pl_bs_invokes_collector_with_full_limit():
    refill = AsyncMock(return_value={"updated": 5, "skipped": 1, "failed": 0, "remaining": 0})
    with (
        patch.object(_pipeline_gh, "init_db", MagicMock()),
        patch.object(_pipeline_gh, "SessionLocal", MagicMock(return_value=MagicMock())),
        patch.object(_pipeline_gh, "refill_pl_bs_from_xbrl", new=refill),
        patch.object(_pipeline_gh, "log", MagicMock()),
    ):
        asyncio.run(_pipeline_gh.main(0, refill_pl_bs=True))

    refill.assert_awaited_once()
    # 既定では limit=None（全件）で呼ばれる
    assert refill.await_args.kwargs["limit"] is None


def test_main_refill_pl_bs_passes_explicit_limit():
    refill = AsyncMock(return_value={"updated": 0, "skipped": 0, "failed": 0, "remaining": 100})
    with (
        patch.object(_pipeline_gh, "init_db", MagicMock()),
        patch.object(_pipeline_gh, "SessionLocal", MagicMock(return_value=MagicMock())),
        patch.object(_pipeline_gh, "refill_pl_bs_from_xbrl", new=refill),
        patch.object(_pipeline_gh, "log", MagicMock()),
    ):
        asyncio.run(_pipeline_gh.main(0, refill_pl_bs=True, refill_pl_bs_limit=100))

    refill.assert_awaited_once()
    assert refill.await_args.kwargs["limit"] == 100
