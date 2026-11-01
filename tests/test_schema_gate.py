"""スキーマ指紋ゲートのユニットテスト（#597・ADR-0048）。

`init_db()` は呼ばれるたび無条件に DDL を打っていた。冪等ではあるが
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` は**列が既に存在しても ACCESS EXCLUSIVE を取る**
ため、長時間バッチが AccessShareLock を握っている間に呼ぶと無言で待ち続ける
（2026-09-02 実測・#411 で実害の前例あり）。

ここで縛るのは3点:
  - 指紋が**移行を決めている入力すべて**から作られること（片方を変えても変わる）
  - 判定が純関数で、実体の欠落（テーブル/VIEW/security_invoker）を指紋一致でも見落とさないこと
  - `init_db()` が「一致なら DDL を1本も打たない／不一致なら打って指紋を記録する」こと

DB を読む層（`_schema_state`）は薄く保ち、判定（`_schema_is_current`）だけを単体で測る。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from database import _SchemaState, _schema_fingerprint, _schema_is_current


def _state(**kw) -> _SchemaState:
    """すべて健全な状態を既定にし、壊したいところだけ上書きする。"""
    base = dict(
        stored_fingerprint="FP",
        missing_tables=(),
        missing_views=(),
        views_without_security_invoker=(),
    )
    base.update(kw)
    return _SchemaState(**base)


class TestSchemaFingerprint:
    def test_is_stable_across_calls(self):
        assert _schema_fingerprint() == _schema_fingerprint()

    def test_changes_when_new_cols_change(self, monkeypatch):
        """`_NEW_COLS` は関数の**外**にある定数。ソースだけを見ていると拾えない。"""
        before = _schema_fingerprint()
        monkeypatch.setattr(database, "_NEW_COLS", tuple(database._NEW_COLS) + ("zzz_probe",))
        assert _schema_fingerprint() != before

    def test_changes_when_legacy_drop_list_changes(self, monkeypatch):
        """DROP COLUMN の対象が変われば移行内容が変わる。"""
        before = _schema_fingerprint()
        monkeypatch.setattr(database, "_LEGACY_COMPUTED_COLS",
                            tuple(database._LEGACY_COMPUTED_COLS) + ("zzz_legacy",))
        assert _schema_fingerprint() != before

    def test_changes_when_view_sql_changes(self, monkeypatch):
        before = _schema_fingerprint()
        monkeypatch.setattr(database, "FINANCIAL_METRICS_VIEW_SQL",
                            database.FINANCIAL_METRICS_VIEW_SQL + " -- probe")
        assert _schema_fingerprint() != before

    def test_changes_when_orm_columns_change(self, monkeypatch):
        """CLAUDE.md が定める列追加の経路（`FinancialRecord` へ列を足す）を拾うこと。

        ORM の列は DDL 文字列にも関数ソースにも現れない（`create_all` が作る）ので、
        ここを見ていないと「列を足したのに移行が走らない」が静かに起きる。
        """
        from sqlalchemy import Column, Float
        tbl = database.Base.metadata.tables["financial_records"]
        before = _schema_fingerprint()
        col = Column("zzz_probe_col", Float())
        tbl.append_column(col)
        try:
            assert _schema_fingerprint() != before
        finally:
            tbl._columns.remove(col)

    def test_survives_getsource_failure(self, monkeypatch):
        """ソースが取れない環境でも例外にせず、必ず「不一致」側へ倒すこと。"""
        import inspect
        monkeypatch.setattr(inspect, "getsource",
                            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no source")))
        fp = _schema_fingerprint()
        assert fp is None or fp != ""


class TestSchemaIsCurrent:
    def test_all_good(self):
        assert _schema_is_current(_state(), "FP") is True

    def test_fingerprint_mismatch(self):
        assert _schema_is_current(_state(stored_fingerprint="OLD"), "FP") is False

    def test_fingerprint_absent(self):
        """まっさらな DB（app_settings に行が無い）は必ず移行を走らせる。"""
        assert _schema_is_current(_state(stored_fingerprint=None), "FP") is False

    def test_missing_table_beats_matching_fingerprint(self):
        assert _schema_is_current(_state(missing_tables=("companies",)), "FP") is False

    def test_missing_view_beats_matching_fingerprint(self):
        """`_ensure_tables` の period_end 移行は条件付きで VIEW を DROP する。

        指紋が一致していても VIEW が存在しないことがありうるので、実在確認が要る。
        """
        assert _schema_is_current(_state(missing_views=("financial_metrics",)), "FP") is False

    def test_view_without_security_invoker_beats_matching_fingerprint(self):
        """security_invoker は RLS の前提（#344）。`ALTER VIEW` は再作成経路でしか打たない。"""
        assert _schema_is_current(
            _state(views_without_security_invoker=("financial_metrics",)), "FP") is False


class TestInitDbGate:
    def test_skips_all_ddl_when_current(self, monkeypatch):
        """定常状態では DDL を1本も発行しない＝ロックを一切取らない（本修正の本体）。"""
        calls = []
        monkeypatch.setattr(database, "_ensure_tables", lambda bind=None: calls.append("tables"))
        monkeypatch.setattr(database, "_ensure_view", lambda bind=None: calls.append("view"))
        monkeypatch.setattr(database, "_read_schema_state", lambda bind=None: _state())
        monkeypatch.setattr(database, "_schema_fingerprint", lambda: "FP")
        monkeypatch.setattr(database, "_record_schema_fingerprint",
                            lambda fp, bind=None: calls.append("record"))
        database.init_db()
        assert calls == []

    def test_runs_and_records_when_stale(self, monkeypatch):
        calls = []
        monkeypatch.setattr(database, "_ensure_tables", lambda bind=None: calls.append("tables"))
        monkeypatch.setattr(database, "_ensure_view", lambda bind=None: calls.append("view"))
        monkeypatch.setattr(database, "_read_schema_state",
                            lambda bind=None: _state(stored_fingerprint="OLD"))
        monkeypatch.setattr(database, "_schema_fingerprint", lambda: "FP")
        monkeypatch.setattr(database, "_record_schema_fingerprint",
                            lambda fp, bind=None: calls.append(f"record:{fp}"))
        database.init_db()
        assert calls == ["tables", "view", "record:FP"]

    def test_does_not_record_when_migration_raises(self, monkeypatch):
        """移行が途中で落ちたのに指紋を書くと、次回スキップして壊れたまま固定される。"""
        calls = []

        def boom(bind=None):
            raise RuntimeError("lock timeout")

        monkeypatch.setattr(database, "_ensure_tables", boom)
        monkeypatch.setattr(database, "_ensure_view", lambda bind=None: calls.append("view"))
        monkeypatch.setattr(database, "_read_schema_state",
                            lambda bind=None: _state(stored_fingerprint=None))
        monkeypatch.setattr(database, "_schema_fingerprint", lambda: "FP")
        monkeypatch.setattr(database, "_record_schema_fingerprint",
                            lambda fp, bind=None: calls.append("record"))
        with pytest.raises(RuntimeError):
            database.init_db()
        assert "record" not in calls

    def test_read_failure_falls_back_to_running(self, monkeypatch):
        """app_settings が無い/読めない場合は「一致しない」に倒して移行を走らせる。"""
        calls = []
        monkeypatch.setattr(database, "_ensure_tables", lambda bind=None: calls.append("tables"))
        monkeypatch.setattr(database, "_ensure_view", lambda bind=None: calls.append("view"))
        monkeypatch.setattr(database, "_record_schema_fingerprint", lambda fp, bind=None: None)

        def boom(bind=None):
            raise RuntimeError("relation \"app_settings\" does not exist")

        monkeypatch.setattr(database, "_read_schema_state", boom)
        database.init_db()
        assert calls == ["tables", "view"]

    def test_bind_reaches_every_step(self, monkeypatch):
        """移行用の engine（#1100）は、判定の読み取りから指紋の記録まで全段へ届く。
        どこか1段でも常用の engine へ戻ると、権限の狭い常用ロールで DDL を打って起動が落ちる。"""
        seen = []
        mig = object()
        monkeypatch.setattr(database, "_ensure_tables", lambda bind=None: seen.append(("tables", bind)))
        monkeypatch.setattr(database, "_ensure_view", lambda bind=None: seen.append(("view", bind)))
        monkeypatch.setattr(database, "_read_schema_state",
                            lambda bind=None: seen.append(("read", bind)) or _state(stored_fingerprint="OLD"))
        monkeypatch.setattr(database, "_schema_fingerprint", lambda: "FP")
        monkeypatch.setattr(database, "_record_schema_fingerprint",
                            lambda fp, bind=None: seen.append(("record", bind)))
        database.init_db(bind=mig)
        assert seen == [("read", mig), ("tables", mig), ("view", mig), ("record", mig)]


class TestMigrationEngine:
    """`migration_engine()`（#1100）: 未設定なら常用の engine、設定時は別の engine を作って閉じる。"""

    def test_unset_yields_the_app_engine(self, monkeypatch):
        monkeypatch.setattr(database, "DATABASE_MIGRATION_URL", None)
        with database.migration_engine() as eng:
            assert eng is database.engine

    def test_set_yields_a_separate_engine_with_session_fixes_and_ledger(self, monkeypatch):
        """所有者ロールの engine は常用と別物で、セッション設定（ADR-0043）と Egress 台帳を
        常用と同じものが付き、抜けると閉じられる（起動の後まで強いロールの接続を持たない）。"""
        from sqlalchemy import event
        mig_url = "postgresql://postgres.abc:secret@pooler.example:5432/postgres"
        monkeypatch.setattr(database, "DATABASE_MIGRATION_URL", mig_url)
        # 実 Postgres へは繋がない。作り方（URL・小さいプール）だけ控えて SQLite の engine を返す
        real_create = database.create_engine
        captured = {}

        def fake_create(url, **kw):
            captured.update(kw, url=url)
            return real_create("sqlite://")

        monkeypatch.setattr(database, "create_engine", fake_create)
        disposed = []
        with database.migration_engine() as eng:
            assert captured["url"] == mig_url
            assert captured["max_overflow"] == 0
            assert eng is not database.engine
            assert event.contains(eng, "connect", database._apply_session_fixes)
            assert eng in database.db_egress._installed
            monkeypatch.setattr(eng, "dispose", lambda *a, **k: disposed.append(True))
        assert disposed == [True]
