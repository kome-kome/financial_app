"""scripts/batch_common.py の WSL2 で起動するステップ（`Step.runner`）— #1316・ADR-0080。

Smart App Control が Windows の jaxlib を止め続けたので、jax を使う仕事は WSL2 の venv で回す。守る境界は3つ:

1. **argv**: リポジトリ直下を cwd にし、`$HOME` の venv の python へ引数をそのまま渡す（ユーザー名を書かない）
2. **環境変数**: 締切（`FINAPP_STEP_DEADLINE_UTC`）などは `WSLENV` に載せないと Linux 側へ届かない。時刻帯は JST
3. **打ち切り**: `wsl.exe` を落としても Linux 側の計算は残りうるので、ディストリビューションごと止める

`wsl.exe` は CI（ubuntu）に無いので、subprocess はすべて差し替える。本物の WSL での確認は PR の検証で行う。
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import batch_common as bc  # noqa: E402


class TestWslPath:
    def test_drive_path_becomes_mnt(self):
        assert bc.to_wsl_path(r"C:\Users\u\.local\bin\financial_app") == "/mnt/c/Users/u/.local/bin/financial_app"

    def test_forward_slashes_and_trailing_separator(self):
        assert bc.to_wsl_path("D:/data/x/") == "/mnt/d/data/x"

    def test_drive_root(self):
        assert bc.to_wsl_path("C:\\") == "/mnt/c"

    def test_posix_path_is_left_alone(self):
        """CI（ubuntu）の ROOT は POSIX のパス。壊さずに返す。"""
        assert bc.to_wsl_path("/home/runner/work/repo") == "/home/runner/work/repo"


class TestWslArgv:
    ARGS = ("-m", "scripts.measure_transformer", "--prereg", "docs/prereg/m7 transformer.md")

    def test_runs_the_venv_python_with_the_arguments_untouched(self):
        argv = bc.wsl_argv(self.ARGS, root=r"C:\repo")
        assert argv[:6] == ("wsl.exe", "-d", bc.WSL_DISTRO, "--cd", "/mnt/c/repo", "--exec")
        # 引数はシェルに解釈させない（空白を含んでも 1 つのまま）
        assert argv[-len(self.ARGS):] == self.ARGS

    def test_python_is_resolved_from_home_not_a_hardcoded_user(self):
        """公開リポジトリなので Linux のユーザー名を書かない。`--exec` は `~` を展開しないので `$HOME` を sh で解く。"""
        argv = bc.wsl_argv(self.ARGS, root=r"C:\repo")
        sh = argv.index("sh")
        assert argv[sh + 1] == "-c"
        script = argv[sh + 2]
        assert f'"$HOME/{bc.WSL_VENV}/bin/python" "$@"' in script and script.startswith("exec ")
        assert "~" not in script and "/home/" not in script

    def test_default_root_is_the_repository(self):
        assert bc.wsl_argv(())[4] == bc.to_wsl_path(bc.ROOT)


class TestWslEnv:
    BASE = {
        bc.ENV_DEADLINE: "2026-11-30T05:23:00+00:00",
        "FINAPP_DB_TARGET": "local",
        "FINAPP_JOB": "daytime-local",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PATH": r"C:\Windows",
        "DATABASE_URL_LOCAL": "secret",
    }

    def _names(self, env):
        return {x.split("/")[0] for x in env["WSLENV"].split(":") if x}

    def test_deadline_and_job_labels_reach_linux(self):
        names = self._names(bc.wsl_env(self.BASE))
        assert {bc.ENV_DEADLINE, "FINAPP_DB_TARGET", "FINAPP_JOB",
                "PYTHONUNBUFFERED", "PYTHONIOENCODING", "TZ"} <= names

    def test_timezone_is_jst(self):
        """Linux の既定は UTC。08:00 JST は前日の 23:00 UTC で、「今日」を使うコードが期を取り違える。"""
        assert bc.wsl_env(self.BASE)["TZ"] == "Asia/Tokyo"

    def test_windows_only_and_secret_variables_are_not_forwarded(self):
        """PATH を渡すと Linux の PATH が壊れる。接続文字列は Linux 側で .env から読む（環境変数で運ばない）。"""
        names = self._names(bc.wsl_env(self.BASE))
        assert "PATH" not in names and "DATABASE_URL_LOCAL" not in names

    def test_existing_wslenv_is_kept_and_not_duplicated(self):
        env = dict(self.BASE, WSLENV="USERPROFILE/p:FINAPP_JOB/u")
        parts = bc.wsl_env(env)["WSLENV"].split(":")
        assert parts[:2] == ["USERPROFILE/p", "FINAPP_JOB/u"]
        assert [p.split("/")[0] for p in parts].count("FINAPP_JOB") == 1

    def test_input_is_not_mutated(self):
        env = dict(self.BASE)
        bc.wsl_env(env)
        assert env == self.BASE


class TestTerminateWsl:
    def test_terminates_the_whole_distribution(self):
        calls = []
        bc.terminate_wsl(run=lambda argv, **kw: calls.append(argv))
        assert calls == [["wsl.exe", "--terminate", bc.WSL_DISTRO]]

    def test_a_failure_is_reported_not_raised(self):
        said = []

        def boom(argv, **kw):
            raise FileNotFoundError("wsl.exe")

        bc.terminate_wsl(echo=said.append, run=boom)
        assert said and "wsl --terminate" in said[0]


# ── Runner を通したときの振る舞い ─────────────────────────────────────────────

class _Proc:
    """`wait` が `hang` 回だけ TimeoutExpired を投げ、その後は `returncode` を返す偽プロセス。"""

    pid = 4242
    stdout = None

    def __init__(self, hang=0, returncode=0):
        self.hang, self.returncode, self.killed = hang, returncode, False

    def wait(self, timeout=None):
        if self.killed:
            return -9
        if self.hang:
            self.hang -= 1
            raise subprocess.TimeoutExpired("x", timeout)
        return self.returncode


def _run(tmp_path, monkeypatch, step, proc, heartbeat_sec=30.0):
    seen = {}

    def popen(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return proc

    terminated = []
    monkeypatch.setattr(bc.subprocess, "Popen", popen)
    monkeypatch.setattr(bc, "kill_tree", lambda p, echo=None: setattr(p, "killed", True))
    monkeypatch.setattr(bc, "terminate_wsl", lambda echo=None, run=None: terminated.append(True))
    log = tmp_path / "d.log"
    with bc.Runner(log, echo=lambda _: None, heartbeat_sec=heartbeat_sec) as r:
        code = r.run(step)
    return code, seen, terminated, log.read_text(encoding="utf-8")


def _wsl_step(**kw):
    return bc.Step("m7_transformer", bc.wsl_argv(("-m", "scripts.measure_transformer")),
                   why="test", runner=bc.RUNNER_WSL, **kw)


class TestRunnerWithWslSteps:
    def test_the_deadline_is_forwarded_through_wslenv(self, tmp_path, monkeypatch):
        _, seen, _, _ = _run(tmp_path, monkeypatch, _wsl_step(budget_min=30), _Proc())
        env = seen["env"]
        assert bc.ENV_DEADLINE in env
        assert f"{bc.ENV_DEADLINE}/u" in env["WSLENV"].split(":")
        assert env["TZ"] == bc.WSL_TZ

    def test_native_steps_get_no_wsl_variables(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WSLENV", raising=False)
        monkeypatch.delenv("TZ", raising=False)
        step = bc.Step("x", ("py", "-m", "x"), why="test", budget_min=30)
        _, seen, _, _ = _run(tmp_path, monkeypatch, step, _Proc())
        assert "WSLENV" not in seen["env"] and "TZ" not in seen["env"]

    def test_timeout_terminates_the_linux_side_too(self, tmp_path, monkeypatch):
        """`wsl.exe` を落としても Linux 側の計算は残りうる。残ると翌日の仕事と並走する。"""
        code, _, terminated, text = _run(tmp_path, monkeypatch, _wsl_step(budget_min=0), _Proc(hang=10**6))
        assert code == bc.TIMEOUT_EXIT
        assert terminated == [True]
        assert "TIMEOUT m7_transformer" in text

    def test_native_timeout_does_not_touch_wsl(self, tmp_path, monkeypatch):
        step = bc.Step("x", ("py", "-m", "x"), why="test", budget_min=0)
        code, _, terminated, _ = _run(tmp_path, monkeypatch, step, _Proc(hang=10**6))
        assert code == bc.TIMEOUT_EXIT and terminated == []

    def test_heartbeat_does_not_claim_a_windows_side_memory_number(self, tmp_path, monkeypatch):
        """Windows 側のツリーには中継の wsl.exe しか見えない。その小さい値を実測として出さない。"""
        monkeypatch.setattr(bc, "_mem_line", lambda pid=None: "mem SHOULD-NOT-APPEAR")
        _, _, _, text = _run(tmp_path, monkeypatch, _wsl_step(), _Proc(hang=1), heartbeat_sec=0.01)
        beat = [x for x in text.splitlines() if x.startswith(bc.HEARTBEAT_MARK)]
        assert beat and "WSL 内" in beat[0] and "SHOULD-NOT-APPEAR" not in beat[0]


def test_steps_default_to_native():
    """既存のステップ（夜間・月次・バックアップ）は何も変えずに Windows で走る。"""
    assert bc.Step("x", ("py",), why="t").runner == bc.RUNNER_NATIVE


def test_root_resolves_to_a_wsl_path_on_windows():
    if os.name != "nt":
        return
    assert bc.to_wsl_path(bc.ROOT).startswith("/mnt/") and "\\" not in bc.to_wsl_path(Path(bc.ROOT))
