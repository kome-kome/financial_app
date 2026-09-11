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
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch_freshness as bf
from scripts import check_batch_freshness as cbf
from scripts import (run_backup, run_daytime, run_monthly, run_monthly_beta,
                     run_monthly_m1, run_nightly)

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
def settings(monkeypatch, tmp_path):
    """app_settings の中身を差し替える。既定は「全部健全」。

    **ログと gh も必ず隔離する。** ここを差し替えないと `main()` を通るテストが本番の
    `.logs/watchdog_YYYYMMDD.log` へ書き込み、実際に 2026-08-26 に汚染した。
    """
    store = {
        run_nightly.KEY_LAST_RUN: _iso(1.0),
        run_nightly.KEY_LAST_SUCCESS: _iso(1.0),
        run_monthly.KEY_LAST_RUN: _iso(25 * 24),
        run_monthly.KEY_LAST_SUCCESS: _iso(90 * 24),   # #512 で成功はずっと古い（正常）
        run_monthly_beta.KEY_LAST_RUN: _iso(25 * 24),
        run_monthly_beta.KEY_LAST_SUCCESS: _iso(25 * 24),
        run_monthly_m1.KEY_LAST_RUN: _iso(25 * 24),
        run_monthly_m1.KEY_LAST_SUCCESS: _iso(25 * 24),
        run_backup.KEY_LAST_RUN: _iso(24.0),          # 週次（閾値 170時間）
        run_backup.KEY_LAST_SUCCESS: _iso(24.0),
        run_daytime.KEY_LAST_RUN: _iso(24.0),         # 平日（閾値 72 + 8 = 80時間）
        run_daytime.KEY_LAST_SUCCESS: _iso(24.0),
        cbf.KEY_LAST_RUN: _iso(24.0),
    }
    monkeypatch.setattr(bf, "_get_setting", lambda db, key: store.get(key))
    monkeypatch.setattr(cbf, "_upsert_setting",
                        lambda db, key, value: store.__setitem__(key, value))
    monkeypatch.setattr(cbf, "_open_session", lambda: _FakeDB())
    monkeypatch.setattr(bf, "db_label", lambda: "ローカル（financial_app）")
    monkeypatch.setattr(cbf, "_log_path", lambda: tmp_path / "watchdog.log")
    monkeypatch.setattr(cbf, "check_gh", lambda **_k: None)
    # `--now` の回は自動クローズしない（#635）ので、クローズの配線は `--now` 無しで試す。
    # そのとき判定時刻が実時計へ流れないよう、既定値の継ぎ目も固定する。
    monkeypatch.setattr(cbf, "_utcnow", lambda: NOW)
    # **実プロセスを構造的に遮断する。** ここを個々のテストの monkeypatch に任せていたため、
    # notify を潰し忘れた1本が本物の `gh` を起動し、**GitHub へ Issue を立てた**
    # （2026-08-26・#552 を誤起票）。書き忘れうる場所に依存させない。
    def _no_subprocess(argv, **_k):
        raise AssertionError(f"テストから実プロセスを起動しようとした: {argv}")

    monkeypatch.setattr(cbf.subprocess, "run", _no_subprocess)
    # producer の読み取りは実 DB クエリなので**ここでも構造的に遮断する**（#504）。
    # `_FakeDB` は `execute` を持たないので素通しにすると全件 unreadable になるが、それは
    # 「テストが本物の DB へ届きかけた」印でしかなく、判定の検証にはならない。既定は
    # 「全部健全」で、固着を試すテストは snap["producers"] を自分で組む。
    monkeypatch.setattr(cbf, "collect_producers",
                        lambda db, now: _producers(FRESH_PRODUCERS, now))
    return store


def _snap(store, now=NOW, gh_error=None):
    snap = cbf.collect(_FakeDB(), now, get=lambda db, key: store.get(key))
    snap["gh_error"] = gh_error
    return snap


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


class TestTheNotificationPathIsCheckedWhileHealthy:
    """**実行の成功 != 通知が届く。** 異常時にしか gh を呼ばないと、通知の死は
    一番届いてほしい回に判明する（#515 の「登録 != 実行」の一段先）。"""

    def test_a_healthy_run_still_reports_the_notification_path(self, settings):
        report = "\n".join(cbf.format_report(_snap(settings)))
        assert "通知経路" in report and "gh 到達可" in report

    def test_a_dead_gh_is_reported_even_when_batches_are_fine(self, settings):
        snap = _snap(settings, gh_error="PATH から gh が見つからない")
        found = cbf.problems(snap)
        assert [p["title"] for p in found] == [cbf.GH_ERROR_TITLE]
        assert "gh 到達可" not in "\n".join(cbf.format_report(snap))

    def test_missing_gh_binary_is_detected(self):
        assert "PATH" in cbf.check_gh(which=lambda _n: None)

    def test_failed_auth_is_detected(self):
        def unauth(argv, **_k):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")

        msg = cbf.check_gh(run=unauth, which=lambda _n: "C:/gh.exe")
        assert msg and "認証" in msg

    def test_a_reachable_gh_returns_none(self):
        def ok(argv, **_k):
            return subprocess.CompletedProcess(argv, 0, stdout="Logged in", stderr="")

        assert cbf.check_gh(run=ok, which=lambda _n: "C:/gh.exe") is None

    def test_a_hanging_gh_does_not_eat_the_window(self):
        """15分の窓を gh の待ちで食い潰さない。"""
        def hang(argv, **kwargs):
            assert kwargs.get("timeout") == cbf.GH_TIMEOUT_SEC
            raise subprocess.TimeoutExpired(argv, cbf.GH_TIMEOUT_SEC)

        assert "返らない" in cbf.check_gh(run=hang, which=lambda _n: "C:/gh.exe")
        assert cbf.GH_TIMEOUT_SEC * 2 < cbf.SELF_WINDOW_MIN * 60

    def test_the_gh_problem_cannot_be_filed_but_still_exits_nonzero(self, settings, monkeypatch):
        """起票の手段が死んでいるので Issue は立たない。痕跡は exit code とログだけ。"""
        monkeypatch.setattr(cbf, "check_gh", lambda **_k: "PATH から gh が見つからない")
        assert cbf.main(["--now", NOW.isoformat()]) == cbf.EXIT_NOTIFY_FAILED

    def test_dry_run_does_not_probe_gh(self, settings, monkeypatch):
        monkeypatch.setattr(cbf, "check_gh",
                            lambda **_k: pytest.fail("--dry-run で gh を叩いてはいけない"))
        cbf.main(["--now", NOW.isoformat(), "--dry-run"])


class TestTestsMustNotTouchTheRealLog:
    """テストが本番ログを汚した実害があった（2026-08-26）。運用中の記録が読めなくなる。"""

    def test_main_takes_its_log_path_from_the_seam(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        cbf.main(["--now", NOW.isoformat()])
        assert (tmp_path / "watchdog.log").exists(), "差し替えた場所へ書いていない"

    def test_the_seam_points_at_the_batch_log_dir_by_default(self):
        import scripts.batch_common as bc
        assert cbf._log_path() == bc.log_path("watchdog")


class TestIssueIsNotDuplicatedDaily:
    """毎日走るので、タイトルが1文字でも動けば Issue が毎日積み上がる。"""

    @pytest.mark.parametrize("watched", cbf.WATCHED, ids=lambda w: w.log_prefix)
    def test_title_has_no_date_or_count(self, watched):
        """**固定の数字も許さない。** 「日付か件数か固定値か」を機械的に区別できない以上、
        一律で禁じる方が安全側（1文字でも動けば Issue が毎日積み上がる）。モデル名に数字が
        要るときは別名で表す（M-1 → 「マクロ×リスク-リターン」）。
        """
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
        monkeypatch.setattr(bf, "db_label", lambda: "ローカル（financial_app）")
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
        assert found == {"run_nightly", "run_monthly", "run_monthly_beta", "run_monthly_m1",
                         "run_backup", "run_daytime"}, (
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


# ── producer の鮮度（#504）──────────────────────────────────────────────────
# 「走ったか」と「走った結果として値が前進したか」は別の事実。2026-09-01 の月次は前者が
# 健全・後者が固着という状態を作り、`plugin_tuned_params` が 50〜59日 古いまま
# 誰にも気づかれなかった。失敗（#587）は起票・クローズされたのに穴だけが残った。

M2_LABEL = "マクロ勾配ブースティング探索の結果"
FRESH_PRODUCERS = {p.label: 1.0 for p in bf.PRODUCERS}


def _producers(ages_days: dict, now=NOW):
    """成果物の最終更新を差し替えて測る。値は「何日前か」・`None` は行が無い。"""
    def read(db, produced):
        days = ages_days[produced.label]
        return None if days is None else now - timedelta(days=days)
    return bf.collect_producers(_FakeDB(), now, read=read)


def _by_label(label):
    return next(p for p in bf.PRODUCERS if p.label == label)


class TestProducerThresholdIsDerived:
    """`Watched` と同じく閾値は `cadence + 窓` の導出。実測から逆算しない。"""

    def test_threshold_is_cadence_plus_window(self):
        for p in bf.PRODUCERS:
            assert p.stale_h == p.cadence_h + p.window_min / 60.0

    def test_windows_come_from_the_batch_modules(self):
        """書き写すと、窓を広げたときに閾値だけが古いまま残る。"""
        assert _by_label(M2_LABEL).window_min == run_monthly.WINDOW_MIN
        assert _by_label("マクロ・ベータの推論結果").window_min == run_monthly_beta.WINDOW_MIN
        assert _by_label("マクロ×リスク-リターン探索の結果").window_min == run_monthly_m1.WINDOW_MIN
        # 唯一、月次ではなく夜間バッチが更新する producer（#632）
        assert _by_label("JPX 業種マスタ").window_min == run_nightly.WINDOW_MIN

    def test_widening_the_window_widens_the_threshold(self):
        p = _by_label(M2_LABEL)
        widened = bf.Produced(**{**p.__dict__, "window_min": p.window_min + 60})
        assert widened.stale_h == p.stale_h + 1.0


class TestRunningIsNotTheSameAsProducing:
    """足跡が健全でも成果物は固着しうる（#504 で実際に起きた形）。"""

    def test_a_fresh_producer_is_silent(self, settings):
        snap = _snap(settings)
        snap["producers"] = _producers(FRESH_PRODUCERS)
        assert cbf.problems(snap) == []

    def test_a_stale_producer_fires_even_when_the_batch_ran(self, settings):
        snap = _snap(settings)
        snap["producers"] = _producers({**FRESH_PRODUCERS, M2_LABEL: 50.0})
        found = cbf.problems(snap)
        assert [f["title"] for f in found] == [_by_label(M2_LABEL).issue_title]
        assert "前進していない" in found[0]["message"]

    def test_a_producer_just_inside_the_threshold_is_silent(self):
        """cadence + 窓 の内側では鳴らない（実行中に鳴らないのと同じ理屈）。"""
        p = _by_label(M2_LABEL)
        rows = _producers({**FRESH_PRODUCERS, M2_LABEL: p.stale_h / 24.0 - 0.01})
        assert all(r["status"] == "ok" for r in rows)

    def test_an_empty_table_is_missing_not_stale(self, settings):
        """「一度も永続化されていない」と「止まった」を同じ顔にしない。"""
        snap = _snap(settings)
        snap["producers"] = _producers({**FRESH_PRODUCERS, M2_LABEL: None})
        assert [f["status"] for f in cbf.problems(snap)] == ["missing"]

    def test_an_unreadable_producer_is_a_problem_not_a_traceback(self, settings):
        def boom(db, produced):
            raise RuntimeError("列が無い")

        snap = _snap(settings)
        snap["producers"] = bf.collect_producers(_FakeDB(), NOW, read=boom)
        found = cbf.problems(snap)
        assert found and all(f["status"] == "unreadable" for f in found)

    def test_a_naive_timestamp_is_read_as_utc(self):
        """接続は `SESSION_FIXES` で UTC 固定（ADR-0043）。JST とみなすと9時間若く見える。"""
        naive = NOW.replace(tzinfo=None) - timedelta(days=50)
        rows = bf.collect_producers(_FakeDB(), NOW, read=lambda db, p: naive)
        assert all(abs(r["age_h"] - 50 * 24) < 1e-6 for r in rows)

    def test_the_morning_payload_is_untouched(self, settings):
        """producer は watchdog だけに出す。`/api/morning` の行が増えると画面契約が変わる。"""
        summary = bf.summarize(_snap(settings))
        assert len(summary["rows"]) == len(bf.WATCHED)


class TestMacroBetaCountsOnlyLiveRuns:
    """隔離（quarantined）は producer が読まない＝残っていても鮮度としては固着（#609）。"""

    class _CaptureDB:
        def __init__(self):
            self.sql = []

        def execute(self, stmt):
            self.sql.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return self

        def scalar(self):
            return None

    def test_the_query_filters_on_live_status(self):
        import database

        db = self._CaptureDB()
        bf._macro_beta_live_at(db)
        sql = " ".join(db.sql)
        assert "status" in sql, "status で絞っていない＝隔離された run も新しさとして数える"
        assert database.MACRO_BETA_STATUS_LIVE in sql
        assert database.MACRO_BETA_STATUS_QUARANTINED not in sql


class TestEveryHeavyProducerIsCovered:
    """増やしたら登録表へ1行。忘れても失敗として現れないので CI が実体と照合する。"""

    def test_every_heavy_plugin_is_in_the_coverage_table(self):
        from nightly_scores import HEAVY_AUTOMATION

        missing = set(HEAVY_AUTOMATION) - set(bf.PRODUCER_COVERAGE)
        assert not missing, (
            f"heavy なのに producer 鮮度の扱いが未登録: {sorted(missing)}。"
            "batch_freshness.PRODUCER_COVERAGE へ 'watched' か 'exempt: <理由>' を足すこと")

    def test_the_table_has_no_stale_entries(self):
        from nightly_scores import HEAVY_AUTOMATION

        stale = set(bf.PRODUCER_COVERAGE) - set(HEAVY_AUTOMATION)
        assert not stale, f"heavy でないのに登録されている: {sorted(stale)}"

    def test_watched_entries_have_a_real_producer(self):
        """`watched` と書いたら実体があること（飾りの登録を作らない）。"""
        sources = " ".join(p.source for p in bf.PRODUCERS)
        for name, how in bf.PRODUCER_COVERAGE.items():
            if how != "watched":
                continue
            assert name in sources, f"{name} は watched だが PRODUCERS が読んでいない"

    def test_exempt_entries_carry_a_reason(self):
        for name, how in bf.PRODUCER_COVERAGE.items():
            if how == "watched":
                continue
            assert how.startswith(bf.PRODUCER_EXEMPT_PREFIX), f"{name}: {how!r}"
            assert how[len(bf.PRODUCER_EXEMPT_PREFIX):].strip(), f"{name}: 理由が空"


class TestProducerIssuesAreNotDuplicated:
    @pytest.mark.parametrize("produced", bf.PRODUCERS, ids=lambda p: p.source)
    def test_title_has_no_date_or_count(self, produced):
        assert not re.search(r"\d", produced.issue_title), produced.issue_title
        assert "{" not in produced.issue_title

    def test_titles_differ_from_the_batch_titles(self):
        """「走っていない」と「値が前進していない」で別の Issue が開くこと。"""
        titles = ([w.issue_title for w in cbf.WATCHED]
                  + [p.issue_title for p in bf.PRODUCERS] + [cbf.DB_ERROR_TITLE])
        assert len(set(titles)) == len(titles)

    def test_the_body_points_at_the_batch_that_updates_it(self, settings):
        snap = _snap(settings)
        snap["producers"] = _producers({**FRESH_PRODUCERS, M2_LABEL: 50.0})
        body = cbf.issue_body(cbf.problems(snap)[0], snap)
        assert "financial_app-monthly" in body
        assert "plugin_tuned_params" in body
        assert "直接クエリ" in body, "ログの表示で判定させない誘導が本文に無い"


class TestJpxIndustryProducer:
    """JPX 業種マスタは夜間バッチが更新する producer（#632）。

    取得が止まっても既存の業種は残るので、画面にも `nightly_last_run` にも現れない
    ——2026-09-03 の拡張子変更（`data_j.xls` → `.xlsx`）は6晩連続の 404 になりながら
    `exit=0` で通った。ここが唯一の現れ方になる。
    """

    LABEL = "JPX 業種マスタ"

    def test_it_reads_the_footprint_not_the_industry_column(self, monkeypatch):
        """既存値は取得が止まっても残る＝`companies.industry` は証拠にならない。"""
        from database import KEY_JPX_INDUSTRY_LAST_SUCCESS
        seen = {}

        def fake_get(db, key):
            seen["key"] = key
            return NOW.isoformat()

        monkeypatch.setattr(bf, "_get_setting", fake_get)
        assert bf._jpx_industry_at(_FakeDB()) == NOW
        assert seen["key"] == KEY_JPX_INDUSTRY_LAST_SUCCESS

    def test_a_stale_footprint_fires(self, settings):
        """夜間バッチ自体は毎晩走っている（足跡は健全）のに、業種だけ止まっている形。"""
        snap = _snap(settings)
        snap["producers"] = _producers({**FRESH_PRODUCERS, self.LABEL: 3.0})
        found = cbf.problems(snap)
        assert [f["title"] for f in found] == [_by_label(self.LABEL).issue_title]

    def test_a_single_missed_night_is_silent(self):
        """cadence(24h) + 窓 の内側では鳴らない（実行中に鳴らないのと同じ理屈）。"""
        p = _by_label(self.LABEL)
        rows = _producers({**FRESH_PRODUCERS, self.LABEL: p.stale_h / 24.0 - 0.01})
        assert all(r["status"] == "ok" for r in rows)


# ── 復旧したら閉じる（#635）────────────────────────────────────────────────
# #634 は起票の16分後に解消したが、閉じるのが人の手だったので翌日まで open のまま残った。
# 閉じ忘れた Issue へ次の欠落が追記されると、直った話と今の話が同じスレッドに混ざって埋もれる。


def _by_watchdog(text="自動起票"):
    return f"{text}\n{cbf.WATCHDOG_MARKER}"


RECOVERY_NOTE = f"復旧\n{cbf.WATCHDOG_MARKER}\n{cbf.RECOVERY_MARKER}"


class _GhIssues:
    """gh の代役。open な Issue ごとに本文とコメントを持ち、呼ばれた argv を全部残す。

    `issues` は `{番号: (タイトル, 本文, [コメント本文, ...])}`。`fail` に入れたサブコマンド
    （`list` / `view` / `close` / `comment`）は returncode=1 で返す。
    """

    def __init__(self, issues=None, fail=()):
        self.issues = issues or {}
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        sub = argv[2]
        if sub in self.fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"{sub} boom")
        stdout = ""
        if sub == "list":
            stdout = json.dumps([{"number": n, "title": t}
                                 for n, (t, _b, _c) in self.issues.items()])
        elif sub == "view":
            _t, body, comments = self.issues[int(argv[3])]
            stdout = json.dumps({"body": body, "comments": [{"body": c} for c in comments]})
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def writes(self):
        return [c for c in self.calls if c[2] in ("close", "comment", "create")]


def _arg(call, flag):
    return call[call.index(flag) + 1]


class TestRecoveredIssuesAreClosed:
    """ok へ戻った対象の起票は、復旧の根拠を添えて watchdog が閉じる。"""

    @staticmethod
    def _close(gh, snap):
        return cbf.close_recovered(cbf.recoveries(snap), snap, say=lambda _: None, run=gh)

    def test_a_recovered_target_is_closed_with_its_evidence(self, settings):
        """1回の ok で閉じる。何を読んで ok と言ったかを本文に残す。"""
        gh = _GhIssues({42: (NIGHTLY.issue_title, _by_watchdog(), [_by_watchdog("追記")])})
        assert self._close(gh, _snap(settings)) == []
        (call,) = gh.writes()
        assert call[:4] == ["gh", "issue", "close", "42"]
        assert _arg(call, "--reason") == "completed"
        body = _arg(call, "--comment")
        assert NOW.isoformat(timespec="seconds") in body         # 判定時刻
        assert NIGHTLY.key_run in body                           # 読んだ場所
        assert settings[run_nightly.KEY_LAST_RUN] in body        # 読んだ値
        assert cbf.RECOVERY_MARKER in body

    def test_nothing_open_means_nothing_but_the_listing(self, settings):
        gh = _GhIssues({7: ("無関係な Issue", "人が書いた", [])})
        assert self._close(gh, _snap(settings)) == []
        assert [c[2] for c in gh.calls] == ["list"]

    def test_a_still_stale_target_stays_open(self, settings):
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        gh = _GhIssues({42: (NIGHTLY.issue_title, _by_watchdog(), [])})
        assert self._close(gh, _snap(settings)) == []
        assert gh.writes() == []

    def test_a_human_comment_stops_the_close_but_gets_a_note(self, settings):
        """投稿者は同じアカウントなので見分けられない。目印の無い本文＝人の手。"""
        gh = _GhIssues({42: (NIGHTLY.issue_title, _by_watchdog(), ["原因を調べている"])})
        assert self._close(gh, _snap(settings)) == []
        (call,) = gh.writes()
        assert call[:4] == ["gh", "issue", "comment", "42"]
        body = _arg(call, "--body")
        assert cbf.RECOVERY_MARKER in body
        assert "自動ではクローズしない" in body

    def test_the_note_is_left_only_once(self, settings):
        """毎日走るので、最新コメントが復旧コメントなら何もしない（積み上げない）。"""
        gh = _GhIssues({42: (NIGHTLY.issue_title, _by_watchdog(),
                             ["原因を調べている", RECOVERY_NOTE])})
        assert self._close(gh, _snap(settings)) == []
        assert gh.writes() == []

    def test_a_relapse_after_the_note_is_noted_again(self, settings):
        """復旧コメントの後に再検出の追記があれば、次の復旧はまた伝える。"""
        gh = _GhIssues({42: (NIGHTLY.issue_title, _by_watchdog(),
                             ["原因を調べている", RECOVERY_NOTE, _by_watchdog("再検出")])})
        assert self._close(gh, _snap(settings)) == []
        assert [c[2] for c in gh.writes()] == ["comment"]

    def test_a_hand_filed_issue_is_not_closed(self, settings):
        """同じタイトルで人が手で立てた Issue（本文に目印が無い）を閉じない。"""
        gh = _GhIssues({42: (NIGHTLY.issue_title, "人が立てた", [])})
        assert self._close(gh, _snap(settings)) == []
        assert [c[2] for c in gh.writes()] == ["comment"]

    def test_a_recovered_producer_is_closed(self, settings):
        snap = _snap(settings)
        snap["producers"] = _producers(FRESH_PRODUCERS)
        prod = _by_label(M2_LABEL)
        gh = _GhIssues({9: (prod.issue_title, _by_watchdog(), [])})
        assert self._close(gh, snap) == []
        (call,) = gh.writes()
        assert call[:4] == ["gh", "issue", "close", "9"]
        assert prod.source in _arg(call, "--comment")

    def test_a_stale_producer_stays_open(self, settings):
        snap = _snap(settings)
        snap["producers"] = _producers({**FRESH_PRODUCERS, M2_LABEL: 50.0})
        assert _by_label(M2_LABEL).issue_title not in [t["title"] for t in cbf.recoveries(snap)]

    def test_a_dead_database_recovers_nothing(self):
        """補集合で作ると、行が空の回に全対象が「問題なし」に見えて全部閉じる。"""
        snap = {"now": NOW, "rows": [], "producers": [], "db_error": "boom",
                "db_label": "x", "gh_error": None}
        assert cbf.recoveries(snap) == []

    def test_a_readable_database_recovers_the_db_issue(self, settings):
        titles = [t["title"] for t in cbf.recoveries(_snap(settings))]
        assert cbf.DB_ERROR_TITLE in titles
        assert cbf.GH_ERROR_TITLE not in titles     # 原理的に自動起票されない

    def test_a_first_ever_watchdog_run_counts_as_recovered(self, settings):
        """自分の初回 missing は正常（problems() と同じ扱い）。"""
        del settings[cbf.KEY_LAST_RUN]
        assert SELF.issue_title in [t["title"] for t in cbf.recoveries(_snap(settings))]


class TestAFailedCloseIsNotSilent:
    """閉じ損ねは起票の失敗と同じ扱い（exit 3）。閉じ損ねた Issue は次の欠落を埋もれさせる。"""

    @pytest.mark.parametrize("step", ["list", "view", "close"])
    def test_each_failed_step_is_returned(self, settings, step):
        gh = _GhIssues({42: (NIGHTLY.issue_title, _by_watchdog(), [])}, fail=[step])
        snap = _snap(settings)
        errors = cbf.close_recovered(cbf.recoveries(snap), snap, say=lambda _: None, run=gh)
        assert errors and f"{step} boom" in errors[0]

    def test_missing_gh_is_returned_not_raised(self, settings):
        def boom(*_a, **_k):
            raise OSError("gh が無い")

        snap = _snap(settings)
        errors = cbf.close_recovered(cbf.recoveries(snap), snap, say=lambda _: None, run=boom)
        assert errors and "gh を起動できない" in errors[0]

    def test_a_failed_close_exits_three_even_when_healthy(self, settings, monkeypatch):
        monkeypatch.setattr(cbf, "close_recovered", lambda *a, **k: ["閉じ損ねた"])
        assert cbf.main([]) == cbf.EXIT_NOTIFY_FAILED
        assert cbf.main(["--warn-only"]) == 0


class TestCloseWiring:
    def test_a_healthy_run_closes(self, settings, monkeypatch):
        seen = []
        monkeypatch.setattr(cbf, "close_recovered",
                            lambda targets, snap, **k: seen.append((targets, k)) or [])
        assert cbf.main([]) == 0
        (targets, kwargs), = seen
        assert NIGHTLY.issue_title in [t["title"] for t in targets]
        assert not kwargs.get("dry_run")

    def test_an_unhealthy_run_still_closes_the_others(self, settings, monkeypatch):
        """1本が止まっていても、戻った別の対象は閉じる。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        seen = []
        monkeypatch.setattr(cbf, "notify", lambda *a, **k: [])
        monkeypatch.setattr(cbf, "close_recovered",
                            lambda targets, snap, **k: seen.append(targets) or [])
        assert cbf.main([]) == cbf.EXIT_UNHEALTHY
        titles = [t["title"] for t in seen[0]]
        assert NIGHTLY.issue_title not in titles
        assert MONTHLY.issue_title in titles

    def test_now_never_closes(self, settings, monkeypatch):
        """過去の時刻を渡すと、いま stale の対象が ok に見えて本物の Issue を閉じてしまう。"""
        def must_not_close(*_a, **_k):
            raise AssertionError("--now の回にクローズしようとした")

        monkeypatch.setattr(cbf, "close_recovered", must_not_close)
        assert cbf.main(["--now", NOW.isoformat()]) == 0

    def test_a_dead_gh_never_closes(self, settings, monkeypatch):
        def must_not_close(*_a, **_k):
            raise AssertionError("gh が死んでいる回にクローズしようとした")

        monkeypatch.setattr(cbf, "check_gh", lambda **_k: "gh が無い")
        monkeypatch.setattr(cbf, "close_recovered", must_not_close)
        assert cbf.main([]) == cbf.EXIT_NOTIFY_FAILED

    def test_dry_run_lists_the_targets_without_gh(self, settings, capsys):
        """subprocess は fixture が遮断しているので、gh を叩けばここで落ちる。"""
        assert cbf.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert f"[dry-run] 復旧（open な Issue があれば復旧コメント付きでクローズ）: " \
               f"{NIGHTLY.issue_title}" in out


class TestMarkers:
    def test_filed_bodies_carry_the_marker(self, settings):
        """目印が無いと、次の復旧で自分の起票を「人の手」とみなして閉じられない。"""
        settings[run_nightly.KEY_LAST_RUN] = _iso(48.0)
        snap = _snap(settings)
        body = cbf.issue_body(cbf.problems(snap)[0], snap)
        assert cbf.WATCHDOG_MARKER in body
        assert cbf.RECOVERY_MARKER not in body

    def test_the_markers_are_distinct(self):
        assert cbf.WATCHDOG_MARKER not in cbf.RECOVERY_MARKER
        assert cbf.RECOVERY_MARKER not in cbf.WATCHDOG_MARKER

    @pytest.mark.parametrize("human_touched", [True, False])
    def test_recovery_bodies_encode_as_cp932(self, settings, human_touched):
        snap = _snap(settings)
        snap["producers"] = _producers(FRESH_PRODUCERS)
        for target in cbf.recoveries(snap):
            body = cbf.recovery_body(target, snap, human_touched)
            body.encode("cp932")
            assert cbf.WATCHDOG_MARKER in body and cbf.RECOVERY_MARKER in body
