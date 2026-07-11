"""GitHub Actions 用パイプライン（`_pipeline_gh.py` / `_pipeline_incremental.py`）の
共通ユーティリティ。

両パイプラインに逐語重複していた `log` / `_is_readonly_error` / `_run_with_retry` を
1実装に集約する。バックオフ係数だけが微差（gh=定数待機 / incremental=指数バックオフ）
だったため `backoff_base` 引数で吸収し、各パイプラインは既定挙動を保つ値を束ねて使う。
"""
import asyncio
import os
from datetime import datetime

from sqlalchemy.exc import InternalError, OperationalError


def make_logger(log_file: str):
    """`log_file` に追記しつつ標準出力にも流すロガー関数を生成する。"""
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return log


def _is_readonly_error(exc: BaseException) -> bool:
    """Supabase が一時的に read-only へ切り替わった際の例外メッセージを判定する。"""
    msg = str(exc).lower()
    return "read-only" in msg or "readonlysqltransaction" in msg


async def _run_with_retry(coro_factory, label: str, *, log_fn,
                          max_retry: int = 2, wait_sec: int = 90,
                          backoff_base: int = 2):
    """Supabase が一時的に read-only に切り替わる事象に対する単純なリトライ。

    `backoff_base=1` で定数待機（待機=`wait_sec`）、`backoff_base=2` で
    指数バックオフ（待機=`wait_sec * backoff_base ** attempt`）になる。
    read-only 以外の `InternalError`/`OperationalError` は即座に送出する。
    """
    for attempt in range(max_retry + 1):
        try:
            return await coro_factory()
        except (InternalError, OperationalError) as e:
            if not _is_readonly_error(e):
                raise
            if attempt >= max_retry:
                raise
            wait = wait_sec * (backoff_base ** attempt)
            log_fn(f"[{label}] ReadOnly エラー検出 (attempt {attempt+1}/{max_retry+1}) "
                   f"— {wait}秒待機して再試行: {e.__class__.__name__}")
            await asyncio.sleep(wait)
