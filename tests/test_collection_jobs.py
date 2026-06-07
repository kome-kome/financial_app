"""collection_jobs.py（収集ジョブ registry）の単体テスト。

候補2「収集ジョブを1モジュールへ集約」の状態遷移を HTTP なしで固定する:
  - JobState の log_seq 累積／切り捨て
  - start の running ガード・force・progress/cancel 注入・例外時の error append と running 解除
  - request_cancel / snapshot / SSE 差分 / スロット独立性
"""
import asyncio
import os
import sys

import pytest
from fastapi import BackgroundTasks, HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collection_jobs import CollectionJobs, JobState, _sse_stream, _LOG_MAX


def _run_bg(bg: BackgroundTasks) -> None:
    """BackgroundTasks に積まれた _runner を同期テストから実行する。"""
    asyncio.run(bg())


class TestJobState:
    def test_append_log_increments_seq(self):
        st = JobState()
        st.append_log("a")
        st.append_log("b")
        assert st.log == ["a", "b"]
        assert st.log_seq == 2

    def test_append_log_truncates_but_seq_keeps_counting(self):
        st = JobState()
        for i in range(_LOG_MAX + 50):
            st.append_log(str(i))
        assert len(st.log) == _LOG_MAX
        assert st.log_seq == _LOG_MAX + 50          # 切り捨て後も seq は単調増加
        assert st.log[-1] == str(_LOG_MAX + 49)

    def test_reset_for_run(self):
        st = JobState(running=False, progress=5, total=9, cancel_requested=True)
        st.log = ["x"]
        st.log_seq = 1
        st.reset_for_run()
        assert st.running is True
        assert st.progress == 0 and st.total == 0
        assert st.log == [] and st.log_seq == 0 and st.cancel_requested is False


class TestStart:
    def test_busy_guard_raises_400(self):
        jobs = CollectionJobs()
        jobs.state("j").running = True
        bg = BackgroundTasks()

        async def body(progress, cancel):
            ...

        with pytest.raises(HTTPException) as ei:
            jobs.start("j", bg, body, busy_message="busy")
        assert ei.value.status_code == 400

    def test_force_bypasses_guard(self):
        jobs = CollectionJobs()
        jobs.state("j").running = True
        bg = BackgroundTasks()
        ran = []

        async def body(progress, cancel):
            ran.append(True)

        jobs.start("j", bg, body, busy_message="busy", force=True)
        _run_bg(bg)
        assert ran == [True]
        assert jobs.is_running("j") is False

    def test_body_runs_with_injected_progress_and_cancel(self):
        jobs = CollectionJobs()
        bg = BackgroundTasks()
        seen = {}

        async def body(progress, cancel):
            seen["cancel0"] = cancel()
            progress(3, 10, "進捗")
            seen["running_mid"] = jobs.is_running("j")

        jobs.start("j", bg, body, busy_message="busy")
        assert jobs.is_running("j") is True          # reset_for_run で同期的に True
        _run_bg(bg)
        st = jobs.state("j")
        assert seen["cancel0"] is False
        assert seen["running_mid"] is True
        assert st.progress == 3 and st.total == 10
        assert st.log == ["進捗"]
        assert st.running is False                    # finally で解除

    def test_body_exception_appends_error_and_clears(self):
        jobs = CollectionJobs()
        bg = BackgroundTasks()

        async def body(progress, cancel):
            raise RuntimeError("boom")

        jobs.start("j", bg, body, busy_message="busy", error_message="[エラー] X")
        _run_bg(bg)
        st = jobs.state("j")
        assert st.running is False
        assert st.log[-1] == "[エラー] X"

    def test_cancel_visible_to_body(self):
        jobs = CollectionJobs()
        bg = BackgroundTasks()
        seen = {}

        async def body(progress, cancel):
            jobs.request_cancel("j")
            seen["after"] = cancel()

        jobs.start("j", bg, body, busy_message="busy")
        _run_bg(bg)
        assert seen["after"] is True


class TestRequestCancel:
    def test_sets_flag_and_returns_prior_running(self):
        jobs = CollectionJobs()
        jobs.state("j").running = True
        assert jobs.request_cancel("j") is True
        assert jobs.state("j").cancel_requested is True

    def test_not_running_returns_false(self):
        jobs = CollectionJobs()
        assert jobs.request_cancel("j") is False


class TestSnapshot:
    def test_shape_and_recent_slice(self):
        jobs = CollectionJobs()
        st = jobs.state("j")
        st.running = True
        st.progress = 2
        st.total = 7
        for i in range(30):
            st.append_log(str(i))
        snap = jobs.snapshot("j", recent=20)
        assert snap == {
            "running": True,
            "progress": 2,
            "total": 7,
            "recent_logs": [str(i) for i in range(10, 30)],
        }


class TestSseStream:
    def test_breaks_when_not_running_and_emits_logs(self):
        st = JobState()
        st.running = False
        st.append_log("done")
        frames = []

        async def consume():
            async for chunk in _sse_stream(st):
                frames.append(chunk)

        asyncio.run(consume())
        assert len(frames) == 1                  # not running → 1フレームで break
        assert '"running": false' in frames[0]
        assert "done" in frames[0]


class TestIndependentSlots:
    def test_slots_do_not_interfere(self):
        jobs = CollectionJobs()
        jobs.state("a").running = True
        assert jobs.is_running("a") is True
        assert jobs.is_running("b") is False
