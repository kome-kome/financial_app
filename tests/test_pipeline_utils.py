"""_pipeline_utils（GitHub Actions パイプライン共通ユーティリティ）の単体テスト。

リトライ成功 / 枯渇、read-only 判定、定数待機 vs 指数バックオフの待機計算を検証する。
asyncio.sleep はモックして実待機ゼロで回す。
"""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy.exc import InternalError, OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _pipeline_utils
from _pipeline_utils import _is_readonly_error, _run_with_retry


def _readonly_exc():
    # InternalError(statement, params, orig) — メッセージに read-only を含める
    return InternalError("stmt", {}, Exception("cannot execute UPDATE in a read-only transaction"))


def _other_db_exc():
    return OperationalError("stmt", {}, Exception("connection timed out"))


# ── _is_readonly_error ───────────────────────────────────────────────────────

class TestIsReadonlyError:
    def test_detects_read_only_phrase(self):
        assert _is_readonly_error(Exception("cannot execute in a read-only transaction"))

    def test_detects_readonlysqltransaction(self):
        assert _is_readonly_error(Exception("ReadOnlySqlTransaction error code 25006"))

    def test_non_readonly_is_false(self):
        assert not _is_readonly_error(Exception("connection refused"))


# ── _run_with_retry ──────────────────────────────────────────────────────────

class TestRunWithRetry:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_returns_value_on_first_success(self):
        async def factory():
            return 42
        result = self._run(_run_with_retry(factory, "ok", log_fn=lambda m: None))
        assert result == 42

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _readonly_exc()
            return "done"

        with patch("_pipeline_utils.asyncio.sleep") as sleep:
            result = self._run(_run_with_retry(
                factory, "retry", log_fn=lambda m: None, wait_sec=10, backoff_base=1))

        assert result == "done"
        assert calls["n"] == 3
        assert sleep.await_count == 2  # 2 回リトライ → 2 回 sleep

    def test_raises_after_exhausting_retries(self):
        async def factory():
            raise _readonly_exc()

        with patch("_pipeline_utils.asyncio.sleep"):
            with pytest.raises(InternalError):
                self._run(_run_with_retry(
                    factory, "exhaust", log_fn=lambda m: None, max_retry=2))

    def test_non_readonly_error_raised_immediately(self):
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise _other_db_exc()

        with patch("_pipeline_utils.asyncio.sleep") as sleep:
            with pytest.raises(OperationalError):
                self._run(_run_with_retry(factory, "other", log_fn=lambda m: None))

        assert calls["n"] == 1          # リトライせず即送出
        assert sleep.await_count == 0

    def test_constant_wait_when_backoff_base_1(self):
        async def factory():
            raise _readonly_exc()

        waits = []
        with patch("_pipeline_utils.asyncio.sleep", side_effect=lambda s: waits.append(s)):
            with pytest.raises(InternalError):
                self._run(_run_with_retry(
                    factory, "const", log_fn=lambda m: None,
                    max_retry=2, wait_sec=90, backoff_base=1))
        assert waits == [90, 90]        # 定数待機

    def test_exponential_backoff_when_base_2(self):
        async def factory():
            raise _readonly_exc()

        waits = []
        with patch("_pipeline_utils.asyncio.sleep", side_effect=lambda s: waits.append(s)):
            with pytest.raises(InternalError):
                self._run(_run_with_retry(
                    factory, "expo", log_fn=lambda m: None,
                    max_retry=2, wait_sec=90, backoff_base=2))
        assert waits == [90, 180]       # 90*2^0, 90*2^1


# ── make_logger ──────────────────────────────────────────────────────────────

def test_make_logger_writes_to_file(tmp_path):
    log_file = tmp_path / "x.log"
    log = _pipeline_utils.make_logger(str(log_file))
    log("hello-line")
    content = log_file.read_text(encoding="utf-8")
    assert "hello-line" in content
