"""scripts/check_egress_health.py と egress-health.yml のテスト — Issue #478 / #483。

このゲートが担うのは1つ:**枠の消費を failure へ翻訳する**こと。Egress 超過は
2026-07（61.2GB）・2026-08（7.312GB）とも restricted になるまで誰も気づかなかった。
`notify-failure.yml` は failure しか拾わず、枠の消費は failure を出さないためである。

ここで担保するのは:

  - 誠実さ: 前サイクルの累計を今サイクルへ繰り越さない（リセット直後の誤警報を作らない）
  - 歯止め: 閾値超過で exit 2・`--warn-only` で exit 0
  - 静かな停止への備え: 台帳の印が現サイクルでないことを報告に明記する
  - 配線: cron で回り、帰属ラベルを持ち、`notify-failure` の除外に当たらないこと

DB へは繋がない（接続をフェイクに差し替える）。
"""
import os
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_egress
from scripts import check_egress_health as ceh

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "egress-health.yml"
GB = 1024 ** 3
MB = 1024 ** 2


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDB:
    """`pg_database_size` だけを返す接続。app_settings は get_setting 側で差し替える。"""

    def __init__(self, db_bytes=395 * MB):
        self._db_bytes = db_bytes
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append(str(stmt))
        return _FakeResult(self._db_bytes)

    def close(self):
        pass


@pytest.fixture
def settings(monkeypatch):
    """app_settings の中身を差し替える。"""
    store: dict = {}
    monkeypatch.setattr(ceh, "get_setting", lambda db, key: store.get(key))
    return store


@pytest.fixture
def today(monkeypatch):
    """サイクル境界を固定する（実日付に依存させない）。"""
    monkeypatch.setattr(db_egress, "current_cycle_start",
                        lambda t=None: date(2026, 8, 18))
    return date(2026, 8, 18)


class TestCollect:
    def test_reads_the_accumulated_bytes_of_the_current_cycle(self, settings, today):
        settings[db_egress.CYCLE_START_KEY] = "2026-08-18"
        settings[db_egress.CYCLE_BYTES_KEY] = str(int(1.5 * GB))

        snap = ceh.collect(_FakeDB())
        assert snap["egress_bytes"] == pytest.approx(1.5 * GB)
        assert snap["egress_ratio"] == pytest.approx(0.30)
        assert snap["ledger_is_current"] is True

    def test_does_not_carry_over_a_previous_cycle(self, settings, today):
        """**リセット直後に前サイクルの値を読むと必ず誤警報が出る**（通知が信用を失う）。"""
        settings[db_egress.CYCLE_START_KEY] = "2026-07-18"
        settings[db_egress.CYCLE_BYTES_KEY] = str(int(4.9 * GB))

        snap = ceh.collect(_FakeDB())
        assert snap["egress_bytes"] == 0.0
        assert snap["ledger_is_current"] is False

    def test_missing_marker_is_treated_as_zero(self, settings, today):
        snap = ceh.collect(_FakeDB())
        assert snap["egress_bytes"] == 0.0
        assert snap["saved_start"] is None

    def test_corrupt_value_does_not_crash_the_gate(self, settings, today):
        """壊れた値で落ちると、以後この通知そのものが来なくなる。"""
        settings[db_egress.CYCLE_START_KEY] = "2026-08-18"
        settings[db_egress.CYCLE_BYTES_KEY] = "not-a-number"
        assert ceh.collect(_FakeDB())["egress_bytes"] == 0.0

    def test_reads_database_size(self, settings, today):
        snap = ceh.collect(_FakeDB(db_bytes=430 * MB))
        assert snap["db_bytes"] == pytest.approx(430 * MB)
        assert snap["db_ratio"] == pytest.approx(0.86)


class TestProblems:
    def _snap(self, egress_ratio=0.0, db_ratio=0.0):
        return {
            "egress_bytes": db_egress.QUOTA_BYTES * egress_ratio,
            "egress_ratio": egress_ratio,
            "db_bytes": ceh.DB_QUOTA_BYTES * db_ratio,
            "db_ratio": db_ratio,
        }

    def test_silent_when_both_are_low(self):
        assert ceh.problems(self._snap(0.1, 0.5)) == []

    def test_flags_egress_at_the_warn_ratio(self):
        found = ceh.problems(self._snap(egress_ratio=db_egress.CYCLE_WARN_RATIO))
        assert len(found) == 1 and "Egress" in found[0]

    def test_flags_database_size_at_its_own_ratio(self):
        found = ceh.problems(self._snap(db_ratio=ceh.DB_WARN_RATIO))
        assert len(found) == 1 and "Database Size" in found[0]

    def test_reports_both_when_both_exceed(self):
        assert len(ceh.problems(self._snap(0.9, 0.95))) == 2

    def test_current_86_percent_does_not_fire_yet(self):
        """2026-08-19 実測は 430MB/500MB (86%)。ここで鳴ると初日から常時 failure になる。"""
        assert ceh.problems(self._snap(db_ratio=0.86)) == []

    def test_db_threshold_is_stricter_than_egress(self):
        """Egress は翌サイクルで戻るが、Database Size 超過は read-only で収集が止まる。"""
        assert ceh.DB_WARN_RATIO > db_egress.CYCLE_WARN_RATIO


class TestReport:
    def test_notes_a_stale_ledger_marker(self, settings, today):
        """「消費ゼロ」と「計測が止まっている」は台帳の上では同じ顔をする（#438 と同型）。"""
        settings[db_egress.CYCLE_START_KEY] = "2026-07-18"
        lines = ceh.format_report(ceh.collect(_FakeDB()))
        assert any("記録がまだ無い" in line for line in lines)

    def test_no_note_when_the_marker_is_current(self, settings, today):
        settings[db_egress.CYCLE_START_KEY] = "2026-08-18"
        settings[db_egress.CYCLE_BYTES_KEY] = "1024"
        lines = ceh.format_report(ceh.collect(_FakeDB()))
        assert not any("記録がまだ無い" in line for line in lines)

    def test_report_is_ascii_safe(self, settings, today):
        """cp932 コンソールへリダイレクトしても出力済みごと落ちないこと。"""
        settings[db_egress.CYCLE_START_KEY] = "2026-08-18"
        for line in ceh.format_report(ceh.collect(_FakeDB())):
            line.encode("cp932")        # 例外が出ないこと


class TestExitCode:
    @pytest.fixture(autouse=True)
    def stub_session(self, monkeypatch):
        monkeypatch.setattr(ceh, "SessionLocal", lambda: _FakeDB(db_bytes=495 * MB))

    def test_exits_two_when_over_threshold(self, settings, today, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["check_egress_health"])
        assert ceh.main() == ceh.EXIT_UNHEALTHY
        assert "notify-failure" in capsys.readouterr().out

    def test_warn_only_exits_zero(self, settings, today, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["check_egress_health", "--warn-only"])
        assert ceh.main() == 0

    def test_exits_zero_when_healthy(self, settings, today, monkeypatch):
        monkeypatch.setattr(ceh, "SessionLocal", lambda: _FakeDB(db_bytes=395 * MB))
        monkeypatch.setattr(sys, "argv", ["check_egress_health"])
        assert ceh.main() == 0

    def test_exit_code_is_nonzero_so_the_run_fails(self):
        """0 だと workflow が success になり notify-failure が発火しない。"""
        assert ceh.EXIT_UNHEALTHY != 0


class TestWorkflowWiring:
    @pytest.fixture(scope="class")
    def wf(self):
        return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_runs_on_a_daily_cron(self, wf):
        """`workflow_run` チェーンにしない——ローカル CLI 由来の消費が見落とされる。"""
        # YAML の `on:` は真偽値 True として読まれる（Norway problem の親戚）
        triggers = wf.get("on") or wf.get(True)
        crons = [s["cron"] for s in triggers["schedule"]]
        assert crons == ["0 21 * * *"]

    def test_has_manual_dispatch_for_investigation(self, wf):
        triggers = wf.get("on") or wf.get(True)
        assert "workflow_dispatch" in triggers

    def test_step_carries_the_job_label(self, wf):
        """FINAPP_JOB が無いと台帳の帰属が argv 由来になり集計が濁る。"""
        envs = [s.get("env", {}) for s in wf["jobs"]["check"]["steps"]]
        assert any(e.get("FINAPP_JOB") == "egress-health" for e in envs)

    def test_invokes_the_script_as_a_module(self, wf):
        """`python scripts/foo.py` は ModuleNotFoundError（-m 必須）。"""
        runs = " ".join(s.get("run", "") for s in wf["jobs"]["check"]["steps"])
        assert "python -m scripts.check_egress_health" in runs

    def test_dispatch_input_has_a_schedule_safe_default(self, wf):
        """schedule 起動では `github.event.inputs.*` が空になる（|| を外さないこと）。"""
        runs = " ".join(s.get("run", "") for s in wf["jobs"]["check"]["steps"])
        assert "github.event.inputs.warn_only || 'false'" in runs

    def test_is_not_excluded_from_failure_notification(self, wf):
        """notify-failure は ci.yml だけを名前で除外する。ここが一致すると通知が消える。"""
        notify = yaml.safe_load(
            (WORKFLOW.parent / "notify-failure.yml").read_text(encoding="utf-8"))
        condition = notify["jobs"]["notify"]["if"]
        assert wf["name"] not in condition

    def test_requests_only_read_permission(self, wf):
        assert wf["permissions"] == {"contents": "read"}
