"""scripts/check_batch_freshness.py のテスト — Issue #515 手順3。

この watchdog が担うのは1つ:**「走らなかったこと」を Issue へ翻訳する**こと。
2026-08-21 の夜間バッチは `0xC000013A` で即死し、ログも足跡も残さなかった。足跡を書く
仕組みはあったが読む側が無く、別件の調査でたまたま気づくまで丸1日誰も知らなかった。

ここで担保するのは:

  - 閾値が**導出**であること（`cadence + 窓`）。窓を広げたのに閾値が古いまま、を不可能にする
  - 判定が watchdog 自身の起動時刻に依存しないこと（依存したら「20:00 に合わせて詰めた」＝逆算）
  - 実行中に鳴らないこと・1日飛べば必ず鳴ること（#515 の受け入れ条件）
  - 「一度も走っていない」と「止まった」を同じ顔にしないこと
  - 毎日走っても Issue が積み上がらないこと
  - 通知の失敗が判定を握り潰さないこと
  - 新しいバッチを足して監視表へ載せ忘れる経路が塞がっていること

DB へは繋がない（`_get_setting` / `_open_session` の継ぎ目を差し替える）。
"""
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import check_batch_freshness as cbf
from scripts import run_monthly, run_nightly

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 26, 11, 0, 0, tzinfo=timezone.utc)      # JST 20:00 = watchdog の起動時刻

NIGHTLY = next(w for w in cbf.WATCHED if w.key_run == run_nightly.KEY_LAST_RUN)
MONTHLY = next(w for w in cbf.WATCHED if w.key_run == run_monthly.KEY_LAST_RUN)
SELF = next(w for w in cbf.WATCHED if w.key_run == cbf.KEY_LAST_RUN)


def _iso(hours_ago: float, base: datetime = NOW) -> str:
    return (base - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


class _FakeDB:
    def close(self):
        pass


@pytest.fixture
def settings(monkeypatch):
    """app_settings の中身を差し替える。既定は「全部健全」。"""
    store = {
        run_nightly.KEY_LAST_RUN: _iso(1.0),
        run_nightly.KEY_LAST_SUCCESS: _iso(1.0),
        run_monthly.KEY_LAST_RUN: _iso(25 * 24),
        run_monthly.KEY_LAST_SUCCESS: _iso(90 * 24),   # #512 で成功はずっと古い（正常）
        cbf.KEY_LAST_RUN: _iso(24.0),
    }
    monkeypatch.setattr(cbf, "_get_setting", lambda db, key: store.get(key))
    monkeypatch.setattr(cbf, "_upsert_setting",
                        lambda db, key, value: store.__setitem__(key, value))
    monkeypatch.setattr(cbf, "_open_session", lambda: _FakeDB())
    monkeypatch.setattr(cbf, "_db_label", lambda: "ローカル（financial_app）")
    return store


def _snap(store, now=NOW):
    return cbf.collect(_FakeDB(), now, get=lambda db, key: store.get(key))


class _FakeRun:
    """gh の代役。呼ばれた argv を全部残す。"""

    def __init__(self, issue_list="[]", returncode=0, stderr=""):
        self.calls: list[list[str]] = []
        self._issue_list = issue_list
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        stdout = self._issue_list if argv[:3] == ["gh", "issue", "list"] else ""
        return subprocess.CompletedProcess(argv, self._returncode,
                                           stdout=stdout, stderr=self._stderr)


class TestThresholdIsDerivedNotGuessed:
    """閾値は約束（cadence + 窓）から導出する。実測から逆算しない。"""

    def test_threshold_is_cadence_plus_window(self):
        assert NIGHTLY.stale_h == 24 + run_nightly.WINDOW_MIN / 60.0        # 30h
        assert MONTHLY.stale_h == 31 * 24 + run_monthly.WINDOW_MIN / 60.0   # 760h
        assert SELF.stale_h == 24 + cbf.SELF_WINDOW_MIN / 60.0              # 24.25h

    def test_widening_the_window_widens_the_threshold(self, monkeypatch):
        """窓を広げたのに閾値が古いまま、を構造的に不可能にする（ADR-0040 の1段外側）。"""
        before = NIGHTLY.stale_h
        widened = cbf.Watched(**{**NIGHTLY.__dict__,
                                 "window_min": run_nightly.WINDOW_MIN + 60})
        assert widened.stale_h == before + 1.0

    def test_keys_and_windows_come_from_the_batch_modules(self):
        """書き写すと typo が『永久に警告が出ない』形でしか現れない。"""
        assert NIGHTLY.key_run == run_nightly.KEY_LAST_RUN
        assert NIGHTLY.key_success == run_nightly.KEY_LAST_SUCCESS
        assert NIGHTLY.window_min == run_nightly.WINDOW_MIN
        assert MONTHLY.key_run == run_monthly.KEY_LAST_RUN
        assert MONTHLY.window_min == run_monthly.WINDOW_MIN

    def test_settings_helpers_actually_exist(self):
        """`database` 側の関数名を直接縛る（`set_setting` と書き間違えた事故が根拠）。"""
        import database
        assert callable(database.get_setting)
        assert callable(database.upsert_setting)
        assert callable(database.db_target_info)


class TestOneMissedNight:
    """#515 の受け入れ条件『わざと1日飛ばして警告が出る』。"""

    def test_a_normal_night_is_silent(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = _iso(1.0)
        assert cbf.problems(_snap(settings)) == []

    def test_a_run_still_in_flight_is_silent(self, settings):
        """夜間バッチが実行中でも鳴らない。窓の項が『まだ走っていてよい時間』を吸収する。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(24.0)     # 前日ぶんしか無い＝今夜は実行中
        assert cbf.problems(_snap(settings)) == []

    def test_a_single_skipped_night_is_detected(self, settings):
        """実測の欠落間隔は 31.3h（2026-08-20 22:45 -> 08-22 06:06）。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(31.3)
        found = cbf.problems(_snap(settings))
        assert [p["title"] for p in found] == [NIGHTLY.issue_title]
        assert found[0]["status"] == "stale"

    def test_a_window_kill_leaves_no_footprint_and_is_detected(self, settings):
        """窓で打ち切られると record_footprint に到達しない＝ADR-0040 が名指しした穴。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        assert [p["title"] for p in cbf.problems(_snap(settings))] == [NIGHTLY.issue_title]

    @pytest.mark.parametrize("observe_hour", range(0, 24, 3))
    @pytest.mark.parametrize("delay_h", [0.0, 0.5, 1.7])
    def test_healthy_history_never_fires_at_any_observation_time(
            self, settings, observe_hour, delay_h):
        """実測の起動遅延（+31分〜+1h41m）込みの健全な履歴が、どの観測時刻でも鳴らない。

        ここが落ちるなら『20:00 に合わせて閾値を詰めた』＝逆算をやっている。
        """
        observe = NOW.replace(hour=observe_hour)
        # 直近の実行は「前回の名目時刻 + 遅延 + 所要70分」。最悪でも 24h + 遅延 + 1.2h 前。
        settings[run_nightly.KEY_LAST_RUN] = _iso(24.0 + delay_h + 1.2, base=observe)
        settings[cbf.KEY_LAST_RUN] = _iso(24.0, base=observe)
        settings[run_monthly.KEY_LAST_RUN] = _iso(30 * 24, base=observe)
        assert cbf.problems(_snap(settings, now=observe)) == []


class TestSuccessIsShownButNotJudged:
    """`monthly_last_success` は #512 が解けるまで設計上ずっと古い（run_monthly.py:111）。"""

    def test_a_stale_success_alone_does_not_fire(self, settings):
        settings[run_monthly.KEY_LAST_RUN] = _iso(10 * 24)
        settings[run_monthly.KEY_LAST_SUCCESS] = _iso(365 * 24)
        assert cbf.problems(_snap(settings)) == []

    def test_a_stale_success_is_still_reported(self, settings):
        settings[run_monthly.KEY_LAST_SUCCESS] = _iso(365 * 24)
        report = "\n".join(cbf.format_report(_snap(settings)))
        assert "成功" in report and "日前" in report

    def test_monthly_is_judged_on_its_own_clock(self, settings):
        settings[run_monthly.KEY_LAST_RUN] = _iso(30 * 24)
        assert cbf.problems(_snap(settings)) == []
        settings[run_monthly.KEY_LAST_RUN] = _iso(40 * 24)
        assert [p["title"] for p in cbf.problems(_snap(settings))] == [MONTHLY.issue_title]


class TestNeverRanIsNotTheSameAsStopped:
    def test_missing_footprint_is_reported_as_missing(self, settings):
        del settings[run_nightly.KEY_LAST_RUN]
        found = cbf.problems(_snap(settings))
        assert found[0]["status"] == "missing"
        assert "一度も走っていない" in found[0]["message"]

    def test_unreadable_footprint_does_not_crash_the_gate(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = "きのう"
        found = cbf.problems(_snap(settings))
        assert found[0]["status"] == "unreadable"

    def test_naive_timestamp_is_read_as_utc(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = NOW.replace(tzinfo=None).isoformat()
        assert cbf.problems(_snap(settings)) == []

    def test_first_ever_watchdog_run_is_not_an_alarm(self, settings):
        """自分の行を書くのは自分だけ＝missing は『まだ1回目』を意味する。"""
        del settings[cbf.KEY_LAST_RUN]
        assert cbf.problems(_snap(settings)) == []

    def test_a_silent_watchdog_is_detected_afterwards(self, settings):
        """リアルタイムに自分の死は検知できないが、次に走ったとき隠しはしない。"""
        settings[cbf.KEY_LAST_RUN] = _iso(72.0)
        assert [p["title"] for p in cbf.problems(_snap(settings))] == [SELF.issue_title]


class TestUnreachableDatabase:
    def test_a_dead_database_is_a_problem_not_a_traceback(self, settings, monkeypatch):
        def boom():
            raise RuntimeError("could not connect to server")

        monkeypatch.setattr(cbf, "_open_session", boom)
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        assert cbf.main(["--now", NOW.isoformat()]) == cbf.EXIT_UNHEALTHY

    def test_the_db_problem_has_its_own_title(self, settings):
        snap = {"now": NOW, "rows": [], "db_error": "connection refused",
                "db_label": "ローカル（financial_app）"}
        assert cbf.problems(snap)[0]["title"] == cbf.DB_ERROR_TITLE


class TestIssueIsNotDuplicatedDaily:
    """毎日走るので、タイトルが1文字でも動けば Issue が毎日積み上がる。"""

    @pytest.mark.parametrize("watched", cbf.WATCHED, ids=lambda w: w.log_prefix)
    def test_title_has_no_date_or_count(self, watched):
        assert not re.search(r"\d", watched.issue_title), watched.issue_title
        assert "{" not in watched.issue_title

    def test_titles_differ_between_targets(self):
        titles = [w.issue_title for w in cbf.WATCHED] + [cbf.DB_ERROR_TITLE]
        assert len(set(titles)) == len(titles)

    def test_status_is_not_part_of_the_title(self, settings):
        """stale -> missing の遷移で2本目が開かないこと。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        stale = cbf.problems(_snap(settings))[0]["title"]
        del settings[run_nightly.KEY_LAST_RUN]
        assert cbf.problems(_snap(settings))[0]["title"] == stale

    def test_existing_open_issue_gets_a_comment(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        run = _FakeRun(issue_list=f'[{{"number": 42, "title": "{NIGHTLY.issue_title}"}}]')
        assert cbf.notify(cbf.problems(snap), snap, say=lambda _: None, run=run) == []
        assert run.calls[-1][:4] == ["gh", "issue", "comment", "42"]

    def test_a_new_issue_is_created_when_none_is_open(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        run = _FakeRun(issue_list='[{"number": 7, "title": "無関係な Issue"}]')
        assert cbf.notify(cbf.problems(snap), snap, say=lambda _: None, run=run) == []
        assert run.calls[-1][:3] == ["gh", "issue", "create"]
        assert "--label" in run.calls[-1]

    def test_the_listing_is_not_filtered_by_label(self, settings):
        """ラベルで絞ると誰かが ops を外した瞬間に重複起票が始まる。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        run = _FakeRun()
        cbf.notify(cbf.problems(snap), snap, say=lambda _: None, run=run)
        listing = next(c for c in run.calls if c[:3] == ["gh", "issue", "list"])
        assert "--label" not in listing

    def test_a_failed_listing_falls_back_to_create(self, settings):
        """重複より沈黙の方が悪い。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)

        class _ListFails(_FakeRun):
            def __call__(self, argv, **kwargs):
                self.calls.append(list(argv))
                if argv[:3] == ["gh", "issue", "list"]:
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        run = _ListFails()
        cbf.notify(cbf.problems(snap), snap, say=lambda _: None, run=run)
        assert run.calls[-1][:3] == ["gh", "issue", "create"]


class TestNotifyFailureDoesNotSwallowTheVerdict:
    def test_missing_gh_is_returned_not_raised(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)

        def boom(*_a, **_k):
            raise OSError("gh が無い")

        errors = cbf.notify(cbf.problems(snap), snap, say=lambda _: None, run=boom)
        assert errors and "gh を起動できない" in errors[0]

    def test_a_failed_notification_gets_its_own_exit_code(self, settings, monkeypatch):
        """『問題を見つけたのに誰にも伝えられていない』が他のどこにも現れないため。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: ["gh を起動できない: なし"])
        assert cbf.main(["--now", NOW.isoformat()]) == cbf.EXIT_NOTIFY_FAILED


class TestExitCodes:
    def test_healthy_is_zero(self, settings, monkeypatch):
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        assert cbf.main(["--now", NOW.isoformat()]) == 0

    def test_a_detected_stop_is_two(self, settings, monkeypatch):
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        assert cbf.main(["--now", NOW.isoformat()]) == cbf.EXIT_UNHEALTHY

    def test_warn_only_is_always_zero(self, settings, monkeypatch):
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        assert cbf.main(["--now", NOW.isoformat(), "--warn-only"]) == 0

    def test_dry_run_keeps_the_verdict_but_touches_nothing(self, settings, monkeypatch):
        """--dry-run は gh を抑止するだけで判定は変えない。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        touched = []
        monkeypatch.setattr(cbf, "_upsert_setting", lambda *a, **k: touched.append("footprint"))
        monkeypatch.setattr(cbf.subprocess, "run", lambda *a, **k: touched.append("gh"))
        assert cbf.main(["--now", NOW.isoformat(), "--dry-run"]) == cbf.EXIT_UNHEALTHY
        assert touched == []

    def test_unreadable_now_is_rejected(self, settings):
        assert cbf.main(["--now", "きのう"]) == 1

    def test_the_watchdog_leaves_its_own_footprint(self, settings, monkeypatch):
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        settings[cbf.KEY_LAST_RUN] = _iso(24.0)
        cbf.main(["--now", NOW.isoformat()])
        assert settings[cbf.KEY_LAST_RUN] != _iso(24.0)

    def test_no_footprint_leaves_it_alone(self, settings, monkeypatch):
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        before = settings[cbf.KEY_LAST_RUN]
        cbf.main(["--now", NOW.isoformat(), "--no-footprint"])
        assert settings[cbf.KEY_LAST_RUN] == before


class TestOutputSafety:
    def test_report_and_body_encode_as_cp932(self, settings):
        """非対応文字は**出力済みの内容ごと**クラッシュさせる（リダイレクト時）。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        text = "\n".join(cbf.format_report(snap))
        for problem in cbf.problems(snap):
            text += problem["message"] + cbf.issue_body(problem, snap)
        text.encode("cp932")

    def test_no_connection_string_reaches_the_output(self, settings, monkeypatch):
        """Issue は公開されうる。生 URL を1文字も出さない。"""
        monkeypatch.setattr(cbf, "_db_label", lambda: "ローカル（financial_app）")
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        text = "\n".join(cbf.format_report(snap))
        for problem in cbf.problems(snap):
            text += cbf.issue_body(problem, snap)
        assert "postgresql://" not in text and "@" not in text

    def test_body_points_at_the_registered_task_name(self, settings):
        """本文が案内するタスク名が実在しなければ、受け取った人は最初の一手で詰まる。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        body = cbf.issue_body(cbf.problems(snap)[0], snap)
        ps1 = (ROOT / "scripts" / "install_nightly_task.ps1").read_text(encoding="utf-8-sig")
        m = re.search(r'\$TaskName\s*=\s*"([^"]+)"', ps1)
        assert m, "install_nightly_task.ps1 から既定 TaskName を読めない（書式が変わった）"
        assert m.group(1) in body


class TestEveryLocalBatchIsWatched:
    """新しいバッチを足して監視表へ載せ忘れても**失敗としては現れない**（#515 の穴そのもの）。"""

    def test_every_batch_spec_key_is_in_the_table(self):
        watched_keys = {w.key_run for w in cbf.WATCHED}
        for module in (run_nightly, run_monthly):
            assert module.KEY_LAST_RUN in watched_keys, (
                f"{module.__name__} の足跡が監視表に無い（WATCHED へ1行足すこと）")

    def test_the_table_covers_the_run_scripts_on_disk(self):
        """`scripts/run_*.py` が増えたら監視表も増える（ADR-0031 型の穴を塞ぐ）。"""
        found = {p.stem for p in (ROOT / "scripts").glob("run_*.py")}
        assert found == {"run_nightly", "run_monthly"}, (
            f"ローカル駆動バッチが増減した: {found}。cbf.WATCHED を見直すこと")


class TestWatchdogInstaller:
    INSTALLER = ROOT / "scripts" / "install_watchdog_task.ps1"
    LAUNCHER = ROOT / "run_watchdog.ps1"

    def test_installer_exists_with_bom(self):
        """BOM が無いと cp932 扱いで日本語が化ける（`test_run_monthly.py` と同型）。"""
        for path in (self.INSTALLER, self.LAUNCHER):
            assert path.exists(), f"{path.name} が無い"
            assert path.read_bytes()[:3] == b"\xef\xbb\xbf", f"{path.name} は BOM 付き UTF-8 で"

    def test_execution_time_limit_matches_the_self_window(self):
        """ここが乖離すると自己監視の閾値（24h + 窓）が実物とずれる。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Minutes\s*=\s*(\d+)", text)
        assert m, "install_watchdog_task.ps1 から -Minutes の既定を読めない（書式が変わった）"
        assert int(m.group(1)) == cbf.SELF_WINDOW_MIN, (
            f"ps1 の {m.group(1)}分 と SELF_WINDOW_MIN {cbf.SELF_WINDOW_MIN}分 が食い違う")
        assert "New-TimeSpan -Minutes $Minutes" in text

    def test_installer_registers_a_daily_trigger(self):
        """週次に変えられると自己監視の 24h 閾値が毎週鳴る。"""
        assert "-Daily" in self.INSTALLER.read_text(encoding="utf-8-sig")

    def test_installer_registers_an_s4u_task(self):
        """見張りが #515 と同じ理由で消えては話にならない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "-LogonType S4U" in text
        assert "Principals.Principal.LogonType" in text, "登録後に実物の LogonType を読み戻していない"
        assert '-ne "S4U"' in text, "読み戻した LogonType を検証していない"

    def test_installer_verifies_what_it_registered(self):
        """cmdlet は失敗しても非終了エラーで返す＝確認しないと『登録しました』と嘘をつく。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "Export-ScheduledTask" in text
        assert "NextRunTime" in text

    def test_installer_checks_for_elevation(self):
        """S4U 登録は管理者権限を要求する（#515 で実測）。生の CIM エラーで放り出さない。"""
        assert "IsInRole" in self.INSTALLER.read_text(encoding="utf-8-sig")

    def test_installer_runs_this_module(self):
        """入口とモジュール名がずれたら、登録しても何も見ない。"""
        assert "run_watchdog.ps1" in self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "scripts.check_batch_freshness" in self.LAUNCHER.read_text(encoding="utf-8-sig")

    def test_launcher_pins_the_local_target(self):
        """正本はローカル。別の DB を読むと足跡が古く見えて毎日誤警報になる。"""
        assert 'FINAPP_DB_TARGET = "local"' in self.LAUNCHER.read_text(encoding="utf-8-sig")
