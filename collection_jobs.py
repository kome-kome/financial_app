"""収集ジョブの実行時状態を1箇所に集約する registry。

api.py に散在していた6本の並列グローバル status dict（_job_status / _market_status /
_history_status / _jquants_status / _macro_status / _reparse_status）と、各ジョブ種別が
手書きしていた start/stop/status/stream の反復スケルトンを、job 名キーの1つの registry へ畳む。

- `jobs.state(name)`  : 種別ごとの JobState（独立スロット。種別をまたいだ並行実行は可）
- `jobs.start(...)`   : 単純種別の定型ライフサイクル（running ガード→状態リセット→
                        progress/cancel 注入→try/finally で running 解除）を一括提供
- `jobs.request_cancel(name)` / `jobs.snapshot(name)` / `jobs.stream(name)`

SSE 配信ジェネレータ（_sse_stream）と状態遷移は HTTP なしで単体テストできる
（tests/test_collection_jobs.py）。full/smart 収集のように CollectionLog へ書き込む
bespoke なジョブは、`jobs.state("collection")` を直接操作する body を自前で組む。
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)

_LOG_MAX = 500
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# body は registry が注入する progress(current, total, msg) と cancel() -> bool を使って収集を実行する。
ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]
JobBody = Callable[[ProgressFn, CancelFn], Awaitable[object]]


@dataclass
class JobState:
    """1つの収集ジョブ種別の実行時状態。SSE 配信と進捗表示が読む単一の真実。"""
    running: bool = False
    progress: int = 0
    total: int = 0
    log: list = field(default_factory=list)
    log_seq: int = 0            # 累積カウンタ（単調増加）。log 切り捨て後も SSE 差分計算を可能にする
    cancel_requested: bool = False

    def append_log(self, msg: str) -> None:
        self.log.append(msg)
        self.log_seq += 1
        if len(self.log) > _LOG_MAX:
            self.log = self.log[-_LOG_MAX:]

    def reset_for_run(self) -> None:
        self.running = True
        self.progress = 0
        self.total = 0
        self.log = []
        self.log_seq = 0
        self.cancel_requested = False


async def _sse_stream(state: JobState):
    """JobState を監視してリアルタイム配信する SSE 共通ジェネレータ。

    log_seq（累積カウンタ）で差分計算するため、内部リストが切り捨てされても
    ログの取りこぼしを最小化できる（切り捨て分は復元不可だが以降は連続配信可能）。
    """
    last_seq = 0
    while True:
        log_list = state.log
        cur_seq = state.log_seq
        # log_list[-1] の seq == cur_seq、log_list[0] の seq == cur_seq - len(log_list) + 1
        oldest_seq = cur_seq - len(log_list) + 1
        start = max(0, last_seq - oldest_seq + 1) if log_list else 0
        new_logs = log_list[start:]
        last_seq = cur_seq
        data = {
            "running": state.running,
            "progress": state.progress,
            "total": state.total,
            "new_logs": new_logs,
        }
        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        if not state.running:
            break
        await asyncio.sleep(1)


class CollectionJobs:
    """job 名キーの収集ジョブ registry。種別ごとに独立した JobState を1箇所で保持する。"""

    def __init__(self) -> None:
        self._states: dict[str, JobState] = {}

    def state(self, name: str) -> JobState:
        return self._states.setdefault(name, JobState())

    def is_running(self, name: str) -> bool:
        return self.state(name).running

    def start(
        self,
        name: str,
        background_tasks: BackgroundTasks,
        body: JobBody,
        *,
        busy_message: str,
        force: bool = False,
        error_message: str = "[エラー] 処理中に問題が発生しました（詳細はサーバーログを確認）",
    ) -> None:
        """単純種別の定型ライフサイクルでジョブを開始する。

        running 中で force でなければ HTTPException(400)。body には registry が注入する
        progress/cancel を渡し、未捕捉例外は error_message をログへ append して running を解除する。
        body 側の db ライフサイクル・特殊例外処理（例: ValueError）は body 内で完結させる。
        """
        st = self.state(name)
        if st.running and not force:
            raise HTTPException(400, busy_message)
        st.reset_for_run()

        def progress(current: int, total: int, msg: str) -> None:
            st.progress = current
            st.total = total
            st.append_log(msg)

        def cancel() -> bool:
            return st.cancel_requested

        async def _runner() -> None:
            try:
                await body(progress, cancel)
            except Exception as e:
                log.error("収集ジョブ '%s' エラー: %s", name, e, exc_info=True)
                st.append_log(error_message)
            finally:
                st.running = False
                st.cancel_requested = False

        background_tasks.add_task(_runner)

    def request_cancel(self, name: str) -> bool:
        """停止フラグを立てる。戻り値=立てた時点で実行中だったか。"""
        st = self.state(name)
        was_running = st.running
        st.cancel_requested = True
        return was_running

    def snapshot(self, name: str, recent: int = 20) -> dict:
        st = self.state(name)
        return {
            "running": st.running,
            "progress": st.progress,
            "total": st.total,
            "recent_logs": st.log[-recent:],
        }

    def stream(self, name: str) -> StreamingResponse:
        return StreamingResponse(
            _sse_stream(self.state(name)),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )


# アプリ全体で共有する単一の registry。
jobs = CollectionJobs()
