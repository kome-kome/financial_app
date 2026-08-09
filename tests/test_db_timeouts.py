"""db_timeouts（database.py）のユニットテスト — #470 / #471。

Supabase 既定の statement_timeout=2min / lock_timeout=0 を「重い文の実行中だけ」
差し替えるコンテキストマネージャ。本番でしか効かない仕組みなので、
  - Postgres では SET → 本体 → RESET の順で必ず戻ること
  - SQLite（テスト経路）では一切 SQL を投げないこと
  - 例外時も RESET を試み、RESET 自体が失敗しても本体の例外を潰さないこと
をダイアレクト差し替えで検証する。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db_timeouts


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeBind:
    def __init__(self, name):
        self.dialect = _FakeDialect(name)


class _FakeDB:
    """Session/Connection の代わり。実行された SQL を文字列で記録するだけ。"""

    def __init__(self, dialect="postgresql", fail_on: str | None = None):
        self.bind = _FakeBind(dialect)
        self.executed: list[str] = []
        self._fail_on = fail_on

    def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("current transaction is aborted")


class TestPostgres:
    def test_sets_and_resets_both(self):
        db = _FakeDB()
        with db_timeouts(db, statement="10min", lock="90s"):
            db.execute("VACUUM FULL x")
        assert db.executed == [
            "SET statement_timeout = '10min'",
            "SET lock_timeout = '90s'",
            "VACUUM FULL x",
            "RESET statement_timeout",
            "RESET lock_timeout",
        ]

    def test_only_requested_knob_is_touched(self):
        """指定しなかった側は SET も RESET もしない（既定値を勝手に動かさない）。"""
        db = _FakeDB()
        with db_timeouts(db, statement="10min"):
            pass
        assert db.executed == ["SET statement_timeout = '10min'", "RESET statement_timeout"]

    def test_zero_means_unlimited(self):
        """VACUUM 用の '0'（無制限）も書式として通ること。"""
        db = _FakeDB()
        with db_timeouts(db, statement="0"):
            pass
        assert db.executed[0] == "SET statement_timeout = '0'"

    def test_resets_after_exception(self):
        """本体が落ちても RESET を試み、例外はそのまま伝播する。"""
        db = _FakeDB()
        with pytest.raises(ValueError):
            with db_timeouts(db, statement="10min"):
                raise ValueError("boom")
        assert db.executed[-1] == "RESET statement_timeout"

    def test_reset_failure_does_not_mask_body_error(self):
        """aborted transaction では RESET も失敗するが、本体の例外を上書きしない
        （呼び出し側の rollback が SET ごと巻き戻す）。"""
        db = _FakeDB(fail_on="RESET")
        with pytest.raises(ValueError, match="boom"):
            with db_timeouts(db, statement="10min"):
                raise ValueError("boom")

    def test_reset_failure_alone_is_swallowed(self):
        db = _FakeDB(fail_on="RESET")
        with db_timeouts(db, statement="10min"):
            pass    # 例外なく抜けること


class TestNonPostgres:
    def test_sqlite_is_noop(self):
        """SQLite は SET を解さない。テスト全件がここを通るので no-op でなければ全滅する。"""
        db = _FakeDB(dialect="sqlite")
        with db_timeouts(db, statement="10min", lock="90s"):
            db.execute("SELECT 1")
        assert db.executed == ["SELECT 1"]


class TestValueValidation:
    @pytest.mark.parametrize("bad", ["10 min", "10m", "; DROP TABLE x", "abc", ""])
    def test_rejects_malformed_value(self, bad):
        """SET は値をバインドできず文字列連結になるため、書式を検証してから流す。"""
        db = _FakeDB()
        with pytest.raises(ValueError):
            with db_timeouts(db, statement=bad):
                pass
        assert db.executed == []

    @pytest.mark.parametrize("ok", ["0", "500ms", "90s", "10min"])
    def test_accepts_supported_units(self, ok):
        db = _FakeDB()
        with db_timeouts(db, lock=ok):
            pass
        assert db.executed[0] == f"SET lock_timeout = '{ok}'"
