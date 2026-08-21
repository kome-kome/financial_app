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
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


class TestKeepsGoing:
    def test_a_failing_step_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        seen = []

        def fake_run(argv, **kw):
            seen.append(Path(argv[1]).name if len(argv) > 1 else argv[0])
            return _FakeProc(returncode=1 if "_pipeline_incremental.py" in argv[1] else 0)

        monkeypatch.setattr(rn.subprocess, "run", fake_run)
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)

        code = rn.main([])
        assert seen == ["_pipeline_incremental.py", "nightly_scores.py"], (
            "失敗したステップの後ろが実行されていない＝途中で止まっている"
        )
        assert code == 1, "失敗数が exit code に出ていない"

    def test_all_success_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn.subprocess, "run", lambda argv, **kw: _FakeProc(0))
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)
        assert rn.main([]) == 0

    def test_unlaunchable_step_is_recorded_not_raised(self, tmp_path):
        """venv が壊れて起動すらできない場合も、例外ではなく exit code で返す。"""
        def boom(*a, **kw):
            raise OSError("no such file")

        with rn.Runner(tmp_path / "n.log", echo=lambda _: None) as r:
            r.run.__globals__  # noqa: B018 - 参照のみ（差し替えは下の monkeypatch 相当）
            original = rn.subprocess.run
            rn.subprocess.run = boom
            try:
                code = r.run(rn.Step("x", ("python", "-c", "pass"), why="test"))
            finally:
                rn.subprocess.run = original
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
        monkeypatch.setattr(rn.subprocess, "run", lambda argv, **kw: _FakeProc(0))
        monkeypatch.setattr(rn, "log_path", lambda now=None: tmp_path / "n.log")
        monkeypatch.setattr(rn, "record_footprint", lambda results: None)
        monkeypatch.setattr(rn, "notify", lambda results, log, run=None: None)
        rn.main([])
        assert os.environ["FINAPP_DB_TARGET"] == "local", (
            "親シェルの prod を引きずっている＝正本の外へ書きにいく"
        )


class TestCli:
    def test_dry_run_executes_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(rn.subprocess, "run",
                            lambda *a, **k: pytest.fail("ドライランなのに実行された"))
        assert rn.main(["--dry-run"]) == 0
        assert "ドライラン" in capsys.readouterr().out

    def test_unknown_step_is_rejected(self):
        with pytest.raises(SystemExit, match="未知のステップ"):
            rn.main(["--steps", "nope", "--dry-run"])

    def test_steps_can_be_limited(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(rn.subprocess, "run",
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
