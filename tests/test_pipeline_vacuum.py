"""_pipeline_vacuum.py のユニットテスト — Issue #471。

週次 VACUUM FULL は 2026-08-08 に**きっかり 2分01秒**で打ち切られた。VACUUM 本体は
実測 7.6〜10.4秒（43〜92MB）なので、待っていたのは ACCESS EXCLUSIVE ロックである。
既定は lock_timeout=0（無制限待ち）＋ statement_timeout=2min のため、
「ロック待ち超過」が「文が重い」と区別できない形で落ちていた。

ここで担保するのは
  - VACUUM を lock_timeout 有限 / statement_timeout 無制限で実行すること
  - ロック待ちで落ちたら保持者を記録して再試行し、使い切ったら送出すること
  - ロック待ち以外（本当のエラー）はリトライせず即送出すること
の3点。DB へは一切繋がず、接続をフェイクに差し替えて検証する。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _pipeline_vacuum as pv


class _Orig(Exception):
    def __init__(self, pgcode):
        self.pgcode = pgcode


class _DBError(Exception):
    """SQLAlchemy が psycopg2 例外を包んだ形（`.orig.pgcode` を持つ）。"""

    def __init__(self, pgcode):
        super().__init__(f"pgcode={pgcode}")
        self.orig = _Orig(pgcode)


class _FakeConn:
    """VACUUM だけが指定回数ロック待ちで落ち、以降は成功する接続。"""

    def __init__(self, lock_failures=0, fatal=False):
        self.executed: list[str] = []
        self._lock_failures = lock_failures
        self._fatal = fatal
        self.dialect = type("D", (), {"name": "postgresql"})()

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        if "VACUUM FULL" in sql:
            if self._fatal:
                raise _DBError("53100")           # disk full — リトライしてはいけない
            if self._lock_failures > 0:
                self._lock_failures -= 1
                raise _DBError("55P03")           # lock_not_available
        return _FakeResult(sql)

    # engine.connect().execution_options(...) as conn: の形に合わせる
    def execution_options(self, **_):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeResult:
    def __init__(self, sql):
        self._sql = sql

    def scalar(self):
        return "42 MB"

    def fetchall(self):
        # ロック保持者の照会は「1件見つかった」体で返す
        return [(999, "idle in transaction", "collector", "AccessExclusiveLock",
                 "00:03:00", "UPDATE stock_price_daily ...")]


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(pv.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(pv, "log", lines.append)
    return lines


def _run(monkeypatch, conn):
    monkeypatch.setattr(pv, "engine", type("E", (), {"connect": lambda self: conn})())


class TestTimeoutKnobs:
    def test_vacuum_runs_with_bounded_lock_and_unbounded_statement(
            self, monkeypatch, no_sleep, logged):
        conn = _FakeConn()
        _run(monkeypatch, conn)
        pv.main()

        assert f"SET lock_timeout = '{pv.LOCK_TIMEOUT}'" in conn.executed
        assert f"SET statement_timeout = '{pv.STATEMENT_TIMEOUT}'" in conn.executed
        # 引き上げは VACUUM の実行中だけ。接続はプールへ返るので必ず戻す
        assert "RESET lock_timeout" in conn.executed
        assert "RESET statement_timeout" in conn.executed

    def test_statement_timeout_is_unlimited(self):
        """VACUUM を時間で殺さない（歯止めはワークフローの timeout-minutes）。"""
        assert pv.STATEMENT_TIMEOUT == "0"

    def test_lock_timeout_is_finite(self):
        assert pv.LOCK_TIMEOUT != "0"


class TestLockRetry:
    def test_retries_after_lock_timeout_then_succeeds(self, monkeypatch, no_sleep, logged):
        conn = _FakeConn(lock_failures=1)
        _run(monkeypatch, conn)
        pv.main()

        assert conn.executed.count(f"VACUUM FULL {pv.TARGET_TABLE}") == 2
        assert no_sleep == [pv.RETRY_SLEEP_SEC]

    def test_logs_lock_holder_on_failure(self, monkeypatch, no_sleep, logged):
        """次に同じ失敗をしたとき「誰に待たされたか」を run ログから辿れること。"""
        conn = _FakeConn(lock_failures=1)
        _run(monkeypatch, conn)
        pv.main()

        assert any("pg_locks" in s for s in conn.executed)
        assert any("ロック保持: pid=999" in line for line in logged)

    def test_raises_after_exhausting_attempts(self, monkeypatch, no_sleep, logged):
        conn = _FakeConn(lock_failures=pv.MAX_ATTEMPTS)
        _run(monkeypatch, conn)
        with pytest.raises(_DBError):
            pv.main()

        assert conn.executed.count(f"VACUUM FULL {pv.TARGET_TABLE}") == pv.MAX_ATTEMPTS
        # 最終試行のあとは待たない
        assert len(no_sleep) == pv.MAX_ATTEMPTS - 1

    def test_non_lock_error_is_not_retried(self, monkeypatch, no_sleep, logged):
        """ロック待ち以外は粘っても直らない。即送出してワークフローを失敗させる。"""
        conn = _FakeConn(fatal=True)
        _run(monkeypatch, conn)
        with pytest.raises(_DBError):
            pv.main()

        assert conn.executed.count(f"VACUUM FULL {pv.TARGET_TABLE}") == 1
        assert no_sleep == []


class TestIsLockTimeout:
    def test_detects_55p03(self):
        assert pv._is_lock_timeout(_DBError("55P03")) is True

    def test_rejects_other_pgcode(self):
        assert pv._is_lock_timeout(_DBError("57014")) is False   # statement_timeout

    def test_rejects_plain_exception(self):
        assert pv._is_lock_timeout(RuntimeError("boom")) is False
