"""引き直しスクリプト（#565）のガードと SQL 形の検査。**実 DB へは触れない。**

守る不変条件:

1. **対象列は metadata から導出する** — 一覧を書き写すと「表を足したのに直っていない」が
   静かに起きる（ADR-0031 と同型）。`timezone=True` の列は仕様どおり正しいので除く。
2. **更新は生 SQL** — ORM 経由だと `financial_records.updated_at` の `onupdate` が発火して
   現在時刻で潰れる＝直したつもりで全行を壊す。
3. **ローカル正本以外へは書かない** — Supabase の Postgres へ書き戻す経路は作らない（ADR-0038）。
4. **冪等スタンプ** — 2度掛けると 18 時間ずれる。取り返しがつかないので必ず止まる。

実行: pytest tests/test_fix_naive_jst_timestamps.py -q
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from scripts import fix_naive_jst_timestamps as fx  # noqa: E402


class _FakeResult:
    rowcount = 7


class _FakeSession:
    """execute された SQL 文字列を溜めるだけの Session もどき。"""

    def __init__(self):
        self.statements: list[str] = []
        self.params: list[dict] = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        self.params.append(params or {})
        return _FakeResult()


# ── 1. 対象列の導出 ─────────────────────────────────────────────────────────

class TestTargetColumnsComeFromMetadata:
    def test_includes_known_contaminated_columns(self):
        cols = fx.naive_datetime_columns()
        for pair in [("financial_records", "updated_at"),
                     ("financial_records", "created_at"),
                     ("macro_data", "created_at"),
                     ("regression_results", "computed_at"),
                     ("macro_enet_scores", "created_at")]:
            assert pair in cols, f"{pair} が対象から漏れている"

    def test_excludes_timezone_aware_column(self):
        """`app_settings.updated_at` は唯一の timestamptz＝最初から正しいので触らない。"""
        assert ("app_settings", "updated_at") not in fx.naive_datetime_columns()

    def test_matches_metadata_exactly(self):
        """書き写しでなく metadata から導出していること（表を足したら自動で載る）。"""
        expected = {
            (t.name, c.name)
            for t in database.Base.metadata.sorted_tables
            for c in t.columns
            if isinstance(c.type, database.DateTime) and not c.type.timezone
        }
        assert set(fx.naive_datetime_columns()) == expected


# ── 2. 変換式 ───────────────────────────────────────────────────────────────

class TestShiftExpression:
    def test_uses_at_time_zone_not_a_bare_interval(self):
        """セッション TZ に依存しない形にする（`- interval '9 hours'` と値は同値だが、
        意図が読めず、将来 TZ を触ったときに黙って壊れる）。"""
        expr = fx.SHIFT_EXPR.format(col='"created_at"')
        assert "AT TIME ZONE 'Asia/Tokyo'" in expr
        assert "AT TIME ZONE 'UTC'" in expr

    def test_update_is_raw_sql_with_bound_cutoff(self):
        db = _FakeSession()
        n = fx.shift_column(db, "financial_records", "updated_at",
                            datetime(2026, 8, 20, 19, 0, 0))
        assert n == 7
        sql = db.statements[0]
        assert sql.startswith('UPDATE public."financial_records"')
        assert "AT TIME ZONE 'Asia/Tokyo'" in sql
        assert ":cut" in sql, "cutoff はバインドで渡す（文字列連結しない）"
        assert db.params[0]["cut"] == datetime(2026, 8, 20, 19, 0, 0)


# ── 3. 接続先ガード ─────────────────────────────────────────────────────────

class TestLocalOnlyGuard:
    def test_rejects_prod_target(self, monkeypatch):
        monkeypatch.setattr(database, "DB_TARGET", "prod")
        monkeypatch.setattr(database, "_is_local", False)
        with pytest.raises(SystemExit):
            fx.guard_local_target()

    def test_rejects_local_target_pointing_at_remote(self, monkeypatch):
        """target が local でも解決先がリモートなら止める（二重の網）。"""
        monkeypatch.setattr(database, "DB_TARGET", "local")
        monkeypatch.setattr(database, "_is_local", False)
        with pytest.raises(SystemExit):
            fx.guard_local_target()

    def test_accepts_local(self, monkeypatch):
        monkeypatch.setattr(database, "DB_TARGET", "local")
        monkeypatch.setattr(database, "_is_local", True)
        fx.guard_local_target()


# ── 4. 冪等スタンプ ─────────────────────────────────────────────────────────

class TestIdempotencyStamp:
    def test_second_run_is_refused(self, monkeypatch):
        stamp = json.dumps({"applied_at": "2026-08-29T00:00:00+00:00", "rows": {}})
        monkeypatch.setattr(database, "get_setting", lambda db, key: stamp)
        with pytest.raises(SystemExit):
            fx.guard_not_applied(object(), force=False)

    def test_force_overrides(self, monkeypatch):
        stamp = json.dumps({"applied_at": "2026-08-29T00:00:00+00:00", "rows": {}})
        monkeypatch.setattr(database, "get_setting", lambda db, key: stamp)
        assert fx.guard_not_applied(object(), force=True) == stamp

    def test_first_run_passes(self, monkeypatch):
        monkeypatch.setattr(database, "get_setting", lambda db, key: None)
        assert fx.guard_not_applied(object(), force=False) is None

    def test_stamp_key_is_stable(self):
        """キーを変えると過去の適用が見えなくなり、2度掛けが通る。"""
        assert fx.STAMP_KEY == "tz_jst_backfill_applied"


# ── 5. 既定値 ───────────────────────────────────────────────────────────────

class TestDefaults:
    def test_cutoff_sits_in_the_measured_gap(self):
        """実測の空白（2026-08-18 21:10 の UTC 値 〜 2026-08-20 19:43 の JST 値）の中。"""
        cut = datetime.fromisoformat(fx.DEFAULT_CUTOFF)
        assert datetime(2026, 8, 18, 21, 11) < cut < datetime(2026, 8, 20, 19, 43)

    def test_guard_band_is_wide_enough_to_catch_a_nearby_utc_row(self):
        assert fx.GUARD_BAND_HOURS >= 12
