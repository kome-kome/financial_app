"""ローカル夜間バッチ `scripts/run_nightly.py` の不変条件（Issue #503 Phase 2）。

正本がローカルへ移り GHA の cron を全て止めた以上、**このバッチが唯一の自動更新経路**に
なる。止まれば鮮度も止まり、しかも GitHub 上には何の失敗も現れない。守るのは4点:

1. **ステップ順**（鮮度 → スコア）。逆にすると前日データでスコアが更新される（#423）
2. **ステップ間で止めない**。片方の失敗で全部落とすと、翌朝分かるのが最初の失敗だけになる
3. **記録・通知の失敗がバッチを落とさない**。付帯処理の失敗が本業を殺すのは本末転倒
4. **接続先を local に固定**。親シェルが prod を持っていても引きずらない
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import run_nightly as rn


class _FakeProc:
    """`subprocess.Popen` の差し替え用。`wait()` は即返る＝heartbeat は刻まれない（#522）。"""

    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def wait(self, timeout=None):
        return self.returncode


class TestStepOrder:
    def test_freshness_runs_before_scores(self):
        """収集がスコアより先。逆順だと前日データでスコアが確定する（#423 の依存順）。"""
        names = [s.name for s in rn.steps_for(sys.executable)]
        assert names.index("pipeline") < names.index("scores")

    def test_collection_uses_the_pipeline_not_collector_cli(self):
        """**株価を更新する入口を選ぶ。**

        `collector.py --incremental` が回すのは `run_full_collection`（企業マスタ・書類
        スキャン・XBRL・業種補完）だけで、株価を1バイトも触らない。鮮度の担い手は
        `_pipeline_incremental.py` の Phase 4（Yahoo gap-fill → J-Quants 置換）にある。
        2026-08-20 に前者で12日ぶんの欠測を埋めようとして、財務だけ通り株価が動かないのを
        実測した。ここを取り違えても**エラーは出ない**（収集は成功する）ので CI で縛る。
        """
        argv = next(s.argv for s in rn.steps_for("py") if s.name == "pipeline")
        assert argv[1] == "_pipeline_incremental.py", (
            "収集ステップが GHA と同じ入口を使っていない＝株価鮮度が止まる"
        )

    def test_every_step_states_why(self):
        """`why` 必須＝何が止まったかをログの言葉で残す（mirror の note と同じ作法）。"""
        for s in rn.steps_for(sys.executable):
            assert s.why.strip(), f"{s.name} に理由が書かれていない"

    def test_steps_use_the_running_interpreter(self):
        """venv の python を使う。素の `python` だと依存の無い環境で動きうる。"""
        for s in rn.steps_for("/x/py.exe"):
            assert s.argv[0] == "/x/py.exe"


class TestExecutionEnvIsRecorded:
    """**どこで走ったか**をログの先頭に残す（#550）。

    S4U 化（#515）でバッチはセッション0で走るようになり、`.env` の解決も PATH も対話
    セッションと変わりうる。取り落としても**書き込み自体は成功する**ので、別の DB を見て
    いても静かに正常終了する（#508 と同型で、接続先の食い違いは沈黙する）。
    """

    def test_the_log_opens_with_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn.subprocess, "Popen", lambda argv, **kw: _FakeProc(0))
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)
        rn.main([])
        text = (tmp_path / "n.log").read_text(encoding="utf-8")
        for key in ("[env] python", "[env] cwd", "[env] DB", "[env] session", "[env] FINAPP"):
            assert key in text, f"{key} がログに無い＝どこで走ったか読めない"

    def test_no_connection_string_leaks_into_the_log(self, monkeypatch):
        """`.logs` は Issue へ貼られうるし、リポジトリは public。生 URL を出さない。"""
        import scripts.batch_common as bc
        monkeypatch.setenv("FINAPP_DB_TARGET", "local")
        text = "\n".join(bc.env_lines())
        assert "postgresql://" not in text and "@" not in text

    def test_secret_looking_env_vars_are_masked(self, monkeypatch):
        import scripts.batch_common as bc
        monkeypatch.setenv("FINAPP_SOME_API_KEY", "s3cr3t-value")
        monkeypatch.setenv("FINAPP_DB_TARGET", "local")
        text = "\n".join(bc.env_lines())
        assert "s3cr3t-value" not in text, "機微な値がそのまま出ている"
        assert "FINAPP_SOME_API_KEY=***" in text
        assert "FINAPP_DB_TARGET=local" in text, "普通のフラグまで伏せている"

    def test_an_unresolvable_database_does_not_stop_the_batch(self, monkeypatch):
        """ここで落ちるとバッチが**始まらない**＝診断行のために本業を壊す。"""
        import builtins
        import scripts.batch_common as bc
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if name == "database":
                raise RuntimeError("解決先がリモート")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", boom)
        text = "\n".join(bc.env_lines())
        assert "解決できない" in text and "RuntimeError" in text

    def test_dry_run_shows_the_environment_before_running(self, capsys):
        """本番へ向いていないかを、叩く前に確認できる。"""
        rn.main(["--dry-run"])
        assert "[env] DB" in capsys.readouterr().out


class TestKeepsGoing:
    def test_a_failing_step_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        seen = []

        def fake_run(argv, **kw):
            seen.append(Path(argv[1]).name if len(argv) > 1 else argv[0])
            return _FakeProc(returncode=1 if "_pipeline_incremental.py" in argv[1] else 0)

        monkeypatch.setattr(rn.subprocess, "Popen", fake_run)
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)

        code = rn.main([])
        assert seen == ["_pipeline_incremental.py", "nightly_scores.py"], (
            "失敗したステップの後ろが実行されていない＝途中で止まっている"
        )
        assert code == 1, "失敗数が exit code に出ていない"

    def test_all_success_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn.subprocess, "Popen", lambda argv, **kw: _FakeProc(0))
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)
        assert rn.main([]) == 0

    def test_unlaunchable_step_is_recorded_not_raised(self, tmp_path):
        """venv が壊れて起動すらできない場合も、例外ではなく exit code で返す。"""
        def boom(*a, **kw):
            raise OSError("no such file")

        with rn.Runner(tmp_path / "n.log", echo=lambda _: None) as r:
            original = rn.subprocess.Popen
            rn.subprocess.Popen = boom
            try:
                code = r.run(rn.Step("x", ("python", "-c", "pass"), why="test"))
            finally:
                rn.subprocess.Popen = original
        assert code == 127


class TestSideEffectsNeverKillTheBatch:
    def test_footprint_helpers_actually_exist(self):
        """`record_footprint` が呼ぶ名前が database に実在すること。

        名前を間違えても import 失敗として握られ、**毎晩「足跡だけ残らない」状態が
        警告1行で続く**（実際に `set_setting` と書き間違えて踏んだ）。付帯処理を落とさない
        設計は、裏返すと「壊れても動き続ける」ので、実在はここで直接縛る。
        """
        import database

        assert callable(database.SessionLocal)
        assert callable(database.upsert_setting)

    def test_footprint_failure_is_reported_not_raised(self, monkeypatch):
        """DB が落ちていても「収集は通ったのに全体が異常終了」にしない。"""
        import database

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(database, "SessionLocal", boom)
        note = rn.record_footprint({"incremental": 0})
        assert note and "app_settings" in note

    def test_notify_is_skipped_when_nothing_failed(self):
        called = []
        assert rn.notify({"a": 0, "b": 0}, Path("x.log"), run=lambda *a, **k: called.append(a)) is None
        assert not called, "全部成功なのに通知しようとしている"

    def test_notify_failure_is_reported_not_raised(self):
        note = rn.notify({"a": 1}, Path("x.log"),
                         run=lambda *a, **k: _FakeProc(returncode=1, stderr="gh: not logged in"))
        assert note and "gh issue create" in note

    def test_missing_gh_is_reported_not_raised(self):
        def boom(*a, **kw):
            raise OSError("gh not found")

        assert "gh を起動できない" in rn.notify({"a": 1}, Path("x.log"), run=boom)

    def test_issue_body_lists_failed_steps(self):
        body = rn.issue_body({"incremental": 0, "scores": 1}, Path("n.log"))
        assert "scores" in body and "#503" in body


class TestTargetPinning:
    def test_local_is_forced_even_if_parent_says_prod(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FINAPP_DB_TARGET", "prod")
        monkeypatch.setattr(rn.subprocess, "Popen", lambda argv, **kw: _FakeProc(0))
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)
        rn.main([])
        assert os.environ["FINAPP_DB_TARGET"] == "local", (
            "親シェルの prod を引きずっている＝正本の外へ書きにいく"
        )


class TestCli:
    def test_dry_run_executes_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(rn.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("ドライランなのに実行された"))
        assert rn.main(["--dry-run"]) == 0
        assert "ドライラン" in capsys.readouterr().out

    def test_unknown_step_is_rejected(self):
        with pytest.raises(SystemExit, match="未知のステップ"):
            rn.main(["--steps", "nope", "--dry-run"])

    def test_steps_can_be_limited(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(rn.subprocess, "Popen",
                            lambda argv, **kw: (seen.append(argv), _FakeProc(0))[1])
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)
        rn.main(["--steps", "scores"])
        assert len(seen) == 1 and "nightly_scores.py" in seen[0][1]


class TestLog:
    def test_log_is_daily_rotated_under_dot_logs(self):
        p = rn.log_path()
        assert p.parent.name == ".logs"
        assert p.name.startswith("nightly_") and p.suffix == ".log"

    def test_log_dir_is_git_ignored(self):
        """ログを誤ってコミットしない（収集ログは大きく、内容も日々変わる）。"""
        ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        assert any(line.strip().rstrip("/") == ".logs" for line in ignore.splitlines()), \
            ".logs が .gitignore に無い"


# ── 子の出力はログへ直結する（2026-08-21 の消えた7時間・#504）────────────────
#
# `capture_output=True` で完了まで溜め込んでいた頃は、途中で親ごと落ちると **START 行だけが
# 残って出力は全部消えた**。macro_beta が7時間走った形跡がどこにも残らず、「走っているのか
# 死んでいるのか」を12時間区別できなかった。直結なら kill されてもそこまではディスクに残る。

class TestChildOutputReachesTheLog:
    def test_output_is_written_even_though_we_never_read_stdout(self, tmp_path):
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None) as r:
            code = r.run(rn.Step(
                "x", (sys.executable, "-c", "print('hello-from-child')"), why="test"))
        assert code == 0
        text = log.read_text(encoding="utf-8")
        assert "hello-from-child" in text
        assert "END   x: exit=0" in text

    def test_partial_output_survives_a_crash(self, tmp_path):
        """途中で落ちても、落ちるまでに書いた行はログに残る（今回の核心）。"""
        import sys
        log = tmp_path / "n.log"
        code = (
            "import sys; print('phase-1'); sys.stdout.flush(); "
            "raise SystemExit(3)"
        )
        with rn.Runner(log, echo=lambda _: None) as r:
            rc = r.run(rn.Step("x", (sys.executable, "-c", code), why="test"))
        assert rc == 3
        assert "phase-1" in log.read_text(encoding="utf-8")

    def test_child_stderr_is_merged_in_order(self, tmp_path):
        import sys
        log = tmp_path / "n.log"
        code = "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr)"
        with rn.Runner(log, echo=lambda _: None) as r:
            r.run(rn.Step("x", (sys.executable, "-c", code), why="test"))
        text = log.read_text(encoding="utf-8")
        assert "to-stdout" in text and "to-stderr" in text

    def test_non_ascii_child_output_is_not_mojibake(self, tmp_path):
        """Windows の子は放っておくと cp932 で書く＝utf-8 で開いたログが化ける。"""
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None) as r:
            r.run(rn.Step("x", (sys.executable, "-c", "print('収束診断')"), why="test"))
        assert "収束診断" in log.read_text(encoding="utf-8")

    def test_end_line_summarises_the_last_child_line(self, tmp_path):
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None) as r:
            r.run(rn.Step("x", (sys.executable, "-c",
                                "print('first'); print('last-line')"), why="test"))
        end = [ln for ln in log.read_text(encoding="utf-8").splitlines()
               if "END   x" in ln][0]
        assert "last-line" in end


def _end_line(log) -> str:
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if "END   x" in ln][0]


# ── heartbeat を Runner へ（Issue #522）────────────────────────────────────────
# PR #511 が入れた heartbeat は macro_beta_inference.py の private な口で、pm.sample 1呼び出しを
# 包んでいるだけだった。同じ月次バッチが回す tune:* や日次の pipeline は無音のまま＝次に
# 「死んだのか走っているのか分からない」が起きるのは別ステップで、そのとき同じ調査をやり直す。

class TestHeartbeat:
    def test_heartbeat_is_written_while_the_child_runs(self, tmp_path):
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=0.05) as r:
            code = r.run(rn.Step("x", (sys.executable, "-c",
                                       "import time; time.sleep(0.4)"), why="test"))
        text = log.read_text(encoding="utf-8")
        assert code == 0
        assert "[heartbeat] x 継続中" in text, "長時間ステップが無音のまま＝生死が判定できない"
        assert "END   x: exit=0" in text, "heartbeat の後で END 行が出ていない"

    def test_no_heartbeat_for_a_quick_child(self, tmp_path):
        """すぐ終わるステップに heartbeat は出ない（ログを無意味に太らせない）。"""
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=30.0) as r:
            r.run(rn.Step("x", (sys.executable, "-c", "print('done')"), why="test"))
        assert "[heartbeat]" not in log.read_text(encoding="utf-8")

    def test_end_summary_ignores_heartbeat_lines(self, tmp_path):
        """END 行の要約に heartbeat を選ばない（heartbeat は進捗ではない）。"""
        import sys
        log = tmp_path / "n.log"
        child = "print('real-progress', flush=True); import time; time.sleep(0.4)"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=0.05) as r:
            r.run(rn.Step("x", (sys.executable, "-c", child), why="test"))
        assert "[heartbeat] x 継続中" in log.read_text(encoding="utf-8")
        assert "real-progress" in _end_line(log)


# ── Runner の堅牢化（Issue #521）──────────────────────────────────────────────

class TestRunnerHardening:
    def test_run_without_a_log_handle_refuses_instead_of_running_blind(self, tmp_path):
        """`with` の外で呼ばれたら**走らせない**（Issue #521-3）。

        以前は `stdout=self._fh or subprocess.DEVNULL` で、子の出力が丸ごと捨てられたうえ
        END 行も診断ゼロになった。数時間のステップでこれが起きると何も残らない。
        """
        import sys
        sentinel = tmp_path / "ran.txt"
        r = rn.Runner(tmp_path / "n.log", echo=lambda _: None)      # with を使わない
        child = f"open({str(sentinel)!r}, 'w').close()"
        code = r.run(rn.Step("x", (sys.executable, "-c", child), why="test"))
        assert code == 126
        assert not sentinel.exists(), "ログハンドルが無いのに子を起動している"

    def test_closed_log_handle_is_recorded_not_raised(self, tmp_path):
        """閉じたハンドルでも例外を漏らさない（Issue #521-1）。

        `subprocess` は閉じたファイルの `fileno()` で **ValueError** を投げる（OSError では
        ない）。取り逃がすと run_batch のステップループごと落ち、`record_footprint` も
        `notify` も走らない＝「黙って走らなかった」を検知するためのモジュールが、それ自体を起こす。
        """
        import sys
        log = tmp_path / "n.log"
        r = rn.Runner(log, echo=lambda _: None)
        with r:
            pass                                     # ここで _fh が閉じる（None にはならない）
        code = r.run(rn.Step("x", (sys.executable, "-c", "print('hi')"), why="test"))
        assert code == 127, "閉じたハンドルで例外が漏れている（exit code に翻訳されていない）"

    def test_end_summary_is_correct_for_output_larger_than_the_tail_window(self, tmp_path):
        """末尾窓（TAIL_BYTES）より大きい出力でも、要約は本当の最終行（Issue #521-2）。

        以前は `pos` から EOF まで丸ごと str へ読み全行の list を作っていた＝ログ直結にした
        理由（数時間ぶんをディスクへ流す）が END 行の瞬間に消えていた。
        """
        import sys
        from scripts.batch_common import TAIL_BYTES
        log = tmp_path / "n.log"
        n_lines = (TAIL_BYTES // 20) + 500           # 窓を確実に超える量
        child = (f"[print('filler-%06d' % i) for i in range({n_lines})]; "
                 "print('the-true-last-line')")
        with rn.Runner(log, echo=lambda _: None) as r:
            r.run(rn.Step("x", (sys.executable, "-c", child), why="test"))
        assert log.stat().st_size > TAIL_BYTES, "テストが窓を超えていない＝何も検証していない"
        assert "the-true-last-line" in _end_line(log)


# ── ステップの時間予算（Issue #530）──────────────────────────────────────────
#
# バッチ全体の窓（タスクスケジューラの ExecutionTimeLimit）は **1ステップに食い尽くされうる**。
# 窓の終わりの打ち切りは「失敗」として現れない——タスクスケジューラがプロセスを止めるだけで
# `record_footprint` も `notify` も走らず、後続ステップは走った形跡すら残さずに消える。
# 2026-09-01 の月次で macro_beta が16時間を食い、tune×3 が一度も起動しない直前だった。

class TestStepBudget:
    def test_over_budget_step_is_killed_and_reported_as_timeout(self, tmp_path):
        """予算を超えたら打ち切り、`TIMEOUT_EXIT` を返す。**子は本当に死んでいる**。

        sentinel は sleep の**後**に書く。予算で切れているなら永遠に現れない——
        「exit code だけ 124 になって子は走り続けている」を弾くための観測点で、
        venv の python.exe がランチャースタブである以上（#512）ここは実物で見るしかない。
        """
        import sys
        from scripts.batch_common import TIMEOUT_EXIT
        log = tmp_path / "n.log"
        sentinel = tmp_path / "finished.txt"
        child = f"import time; time.sleep(30); open({str(sentinel)!r}, 'w').close()"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=0.05) as r:
            code = r.run(rn.Step("x", (sys.executable, "-c", child),
                                 why="test", budget_min=0.02))
        assert code == TIMEOUT_EXIT
        text = log.read_text(encoding="utf-8")
        assert "TIMEOUT x" in text, "打ち切ったことがログに残っていない"
        assert f"END   x: exit={TIMEOUT_EXIT}" in text
        assert not sentinel.exists(), "予算で切ったはずの子が生き残って走り切っている"

    def test_end_summary_keeps_the_childs_last_progress_not_our_timeout_line(self, tmp_path):
        """END 行の要約は**子がどこまで進んだか**を残す（打ち切った旨は別行にある）。

        打ち切りの行を先に書くと `_tail_since` がそれを拾い、一番知りたい「子の最終進捗」が
        END 行から消える。#521 で heartbeat 行を要約に選ばないようにしたのと同じ理由。
        """
        import sys
        log = tmp_path / "n.log"
        child = "print('reached-phase-3', flush=True); import time; time.sleep(30)"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=0.05) as r:
            r.run(rn.Step("x", (sys.executable, "-c", child), why="test", budget_min=0.02))
        assert "reached-phase-3" in _end_line(log), "END 行の要約が子の最終進捗になっていない"
        assert "TIMEOUT x" in log.read_text(encoding="utf-8"), "打ち切った旨の行が消えている"

    def test_a_step_inside_its_budget_is_untouched(self, tmp_path):
        """予算内で終わるステップに副作用を出さない（打ち切りログも出さない）。"""
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=30.0) as r:
            code = r.run(rn.Step("x", (sys.executable, "-c", "print('done')"),
                                 why="test", budget_min=10))
        text = log.read_text(encoding="utf-8")
        assert code == 0
        assert "TIMEOUT" not in text
        assert "END   x: exit=0" in text

    def test_no_budget_means_no_deadline(self, tmp_path):
        """`budget_min=None` は従来どおり無期限（既存の呼び出しを壊さない）。"""
        import sys
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=0.05) as r:
            code = r.run(rn.Step("x", (sys.executable, "-c",
                                       "import time; time.sleep(0.3)"), why="test"))
        assert code == 0
        assert "TIMEOUT" not in log.read_text(encoding="utf-8")

    def test_zero_budget_is_not_treated_as_unlimited(self, tmp_path):
        """0 を falsy 判定で「無期限」へ落とさない（`if step.budget_min` の罠）。"""
        import sys
        from scripts.batch_common import TIMEOUT_EXIT
        log = tmp_path / "n.log"
        with rn.Runner(log, echo=lambda _: None, heartbeat_sec=0.05) as r:
            code = r.run(rn.Step("x", (sys.executable, "-c", "import time; time.sleep(30)"),
                                 why="test", budget_min=0))
        assert code == TIMEOUT_EXIT

    def test_dry_run_shows_the_budget(self, capsys):
        """実行計画に予算が出る＝窓の使い方を実行前に確認できる。"""
        rn.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "予算" in out and "pipeline" in out


class TestBudgetFitsTheWindow:
    """**窓と予算はセットでしか意味を持たない。** 片方だけ動かすと静かに壊れる。"""

    def test_every_step_has_a_budget(self):
        """1本でも無期限があれば窓の保証はその時点で消える。"""
        missing = [s.name for s in rn.steps_for(sys.executable) if s.budget_min is None]
        assert not missing, f"予算の無いステップ: {missing}（BUDGET_MIN への追加漏れ）"

    def test_budgets_fit_inside_the_scheduler_window(self):
        import scripts.batch_common as bc
        problem = bc.window_problem(rn.steps_for(sys.executable), rn.WINDOW_MIN)
        assert problem is None, problem

    def test_window_matches_the_installer(self):
        """`install_nightly_task.ps1` の `ExecutionTimeLimit` と `WINDOW_MIN` を照合する。

        ここを縛らないと、窓を広げたのに予算が古いまま（窓を使い切れない）／予算を足したのに
        窓が足りない（最後のステップが黙って打ち切られる）が起きる。**どちらも失敗として
        現れない**ので CI で見るしかない。
        """
        import re
        ps1 = (Path(__file__).resolve().parents[1]
               / "scripts" / "install_nightly_task.ps1").read_text(encoding="utf-8-sig")
        m = re.search(r"-ExecutionTimeLimit\s*\(New-TimeSpan\s+-Hours\s+(\d+)\)", ps1)
        assert m, "install_nightly_task.ps1 から ExecutionTimeLimit を読めない（書式が変わった）"
        assert int(m.group(1)) * 60 == rn.WINDOW_MIN, (
            f"ps1 の窓 {m.group(1)}時間 と run_nightly.WINDOW_MIN {rn.WINDOW_MIN}分 が食い違う"
        )
