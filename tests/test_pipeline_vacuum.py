"""_pipeline_vacuum.py のユニットテスト — Issue #471 / #290。

週次 VACUUM FULL は 2026-08-08 に**きっかり 2分01秒**で打ち切られた。VACUUM 本体は
実測 7.6〜10.4秒（43〜92MB）なので、待っていたのは ACCESS EXCLUSIVE ロックである。
既定は lock_timeout=0（無制限待ち）＋ statement_timeout=2min のため、
「ロック待ち超過」が「文が重い」と区別できない形で落ちていた。

2026-08-19（#290 再オープン）に対象を 2 表へ広げ、per-table の autovacuum
チューニングを前段に足した。ここで担保するのは

  - VACUUM を lock_timeout 有限 / statement_timeout 無制限で実行すること
  - ロック待ちで落ちたら保持者を記録して再試行し、使い切ったら送出すること
  - ロック待ち以外（本当のエラー）はリトライせず即送出すること
  - **TARGET_TABLES の全表**が VACUUM されること（1表増やしたのに回っていない、を防ぐ）
  - autovacuum チューニングが **VACUUM より先**に走り、かつ**冪等**であること

の5点。DB へは一切繋がず、接続をフェイクに差し替えて検証する。
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
    """VACUUM だけが指定回数ロック待ちで落ち、以降は成功する接続。

    `reloptions` は {テーブル名: [オプション文字列, ...]}。未指定の表は「未設定」
    （pg_class.reloptions が NULL）を返す＝本番の初回と同じ状態。
    """

    def __init__(self, lock_failures=0, fatal=False, reloptions=None):
        self.executed: list[str] = []
        self._lock_failures = lock_failures
        self._fatal = fatal
        self._reloptions = dict(reloptions or {})
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
            return _FakeResult(None)
        if "ALTER TABLE" in sql:
            # 適用後は照会が新しい値を返すようにする（冪等性の検証で効く）
            for table in pv.TARGET_TABLES:
                if f"ALTER TABLE {table} " in sql:
                    self._reloptions[table] = sorted(pv._wanted_reloptions())
            return _FakeResult(None)
        if "reloptions" in sql:
            return _FakeResult(self._reloptions.get((params or {}).get("t")))
        return _FakeResult("42 MB")

    # engine.connect().execution_options(...) as conn: の形に合わせる
    def execution_options(self, **_):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- 検証用ヘルパ --------------------------------------------------------

    def index_of(self, needle: str) -> int:
        for i, sql in enumerate(self.executed):
            if needle in sql:
                return i
        return -1


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

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


def _first_table() -> str:
    """1表目。ロック再試行の検証はここで完結する（2表目まで到達しない）。"""
    return pv.TARGET_TABLES[0]


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


class TestTargetTables:
    def test_all_target_tables_are_vacuumed(self, monkeypatch, no_sleep, logged):
        """表を足したのに回っていない、を防ぐ（#290 で daily のみだった実例）。"""
        conn = _FakeConn()
        _run(monkeypatch, conn)
        pv.main()

        for table in pv.TARGET_TABLES:
            assert f"VACUUM FULL {table}" in conn.executed

    def test_weekly_is_covered(self):
        """dead tuple 200,498 を抱えていた実表が対象に入っていること（#290）。"""
        assert "stock_price_weekly" in pv.TARGET_TABLES
        assert "stock_price_daily" in pv.TARGET_TABLES

    def test_sizes_are_reported_per_table(self, monkeypatch, no_sleep, logged):
        """before/after が表ごとに出ること（どの表が縮んだか事後に分かるように）。"""
        conn = _FakeConn()
        _run(monkeypatch, conn)
        pv.main()

        for table in pv.TARGET_TABLES:
            assert any(f"[before] {table}" in line for line in logged)
            assert any(f"[after]  {table}" in line for line in logged)


class TestAutovacuumTuning:
    def test_applies_when_unset(self, monkeypatch, no_sleep, logged):
        """reloptions が NULL（クラスタ既定 0.2）の表には ALTER を投げる。"""
        conn = _FakeConn()
        _run(monkeypatch, conn)
        pv.main()

        for table in pv.TARGET_TABLES:
            assert any(f"ALTER TABLE {table} SET (" in sql for sql in conn.executed)
        assert any(f"autovacuum_vacuum_scale_factor = {pv.AUTOVACUUM_SCALE_FACTOR}" in sql
                   for sql in conn.executed)

    def test_skipped_when_already_tuned(self, monkeypatch, no_sleep, logged):
        """冪等。既に望みの値なら ALTER を投げない（毎週「変更した」と誤読させない）。"""
        tuned = {t: sorted(pv._wanted_reloptions()) for t in pv.TARGET_TABLES}
        conn = _FakeConn(reloptions=tuned)
        _run(monkeypatch, conn)
        pv.main()

        assert not any("ALTER TABLE" in sql for sql in conn.executed)
        for table in pv.TARGET_TABLES:
            assert any(f"[autovacuum] {table}: 設定済み" in line for line in logged)

    def test_runs_before_vacuum(self, monkeypatch, no_sleep, logged):
        """チューニングが先。後に回すと、その週の VACUUM FULL は重いままになる。"""
        conn = _FakeConn()
        _run(monkeypatch, conn)
        pv.main()

        assert conn.index_of("ALTER TABLE") < conn.index_of("VACUUM FULL")

    def test_scale_factor_is_below_cluster_default(self):
        """0.2 のままでは 128万行の表で発火閾値 256,943 行に届かない（#290）。"""
        assert pv.AUTOVACUUM_SCALE_FACTOR < 0.2
        assert pv.AUTOVACUUM_ANALYZE_SCALE_FACTOR < 0.2

    def test_alter_is_lock_bounded(self, monkeypatch, no_sleep, logged):
        """ALTER も ACCESS EXCLUSIVE を取る。無制限待ちだと VACUUM の前で詰まる。"""
        conn = _FakeConn()
        _run(monkeypatch, conn)
        pv.main()

        assert conn.index_of(f"SET lock_timeout = '{pv.LOCK_TIMEOUT}'") < conn.index_of("ALTER TABLE")


class TestLockRetry:
    def test_retries_after_lock_timeout_then_succeeds(self, monkeypatch, no_sleep, logged):
        conn = _FakeConn(lock_failures=1)
        _run(monkeypatch, conn)
        pv.main()

        assert conn.executed.count(f"VACUUM FULL {_first_table()}") == 2
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

        assert conn.executed.count(f"VACUUM FULL {_first_table()}") == pv.MAX_ATTEMPTS
        # 最終試行のあとは待たない
        assert len(no_sleep) == pv.MAX_ATTEMPTS - 1

    def test_later_tables_are_skipped_when_earlier_one_fails(
            self, monkeypatch, no_sleep, logged):
        """1表目が落ちたら送出して止まる（残りを黙って飛ばして成功扱いにしない）。"""
        conn = _FakeConn(lock_failures=pv.MAX_ATTEMPTS)
        _run(monkeypatch, conn)
        with pytest.raises(_DBError):
            pv.main()

        for table in pv.TARGET_TABLES[1:]:
            assert f"VACUUM FULL {table}" not in conn.executed

    def test_non_lock_error_is_not_retried(self, monkeypatch, no_sleep, logged):
        """ロック待ち以外は粘っても直らない。即送出してワークフローを失敗させる。"""
        conn = _FakeConn(fatal=True)
        _run(monkeypatch, conn)
        with pytest.raises(_DBError):
            pv.main()

        assert conn.executed.count(f"VACUUM FULL {_first_table()}") == 1
        assert no_sleep == []


class TestIsLockTimeout:
    def test_detects_55p03(self):
        assert pv._is_lock_timeout(_DBError("55P03")) is True

    def test_rejects_other_pgcode(self):
        assert pv._is_lock_timeout(_DBError("57014")) is False   # statement_timeout

    def test_rejects_plain_exception(self):
        assert pv._is_lock_timeout(RuntimeError("boom")) is False
