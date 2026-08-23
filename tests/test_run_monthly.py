"""ローカル月次バッチ `scripts/run_monthly.py` の不変条件（Issue #504・親 #503）。

#503 で GHA の cron を全部止めたとき、日次だけをローカルへ移して月次3本は止まったまま
残った。**無実行は failure を出さない**ので、notify-failure でも macro-health でも
拾えない——気づけるのは誰かが「この値いつのだ？」と思ったときだけになる。

守るのは4点:

1. **移設漏れが無い**。GHA で回していた3本（tune / macro-beta / factor-premia）が
   すべてステップとして載っていること
2. **ステップ順**（依存順 ∧ 軽い順）。`macro_beta_loadings` は M-1 の入力なので推論が先。
   打ち切られても前方が揃うよう軽い順に並べる
3. **GHA と同じ引数**で回すこと。探索規模を移設のついでに変えると `objective_value` の
   品質ゲート（#291）が別条件の値と比較される
4. **足跡・ログが日次と混ざらない**。月次は1か月に1度しか機会が無く、混ざると
   「走らなかった月」が日次の成功で隠れる

骨格（ステップ間で止めない・記録と通知が本業を殺さない）は `scripts/batch_common.py` に
あり、`tests/test_run_nightly.py` が同じ経路を通して守っている。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import run_monthly as rm  # noqa: E402
from scripts import run_nightly as rn  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


class _FakeProc:
    """`subprocess.Popen` の差し替え用。`wait()` は即返る＝heartbeat は刻まれない（#522）。"""

    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def wait(self, timeout=None):
        return self.returncode


def _names() -> list[str]:
    return [s.name for s in rm.steps_for(sys.executable)]


def _argv(name: str) -> tuple[str, ...]:
    return next(s.argv for s in rm.steps_for("py") if s.name == name)


class TestMigrationIsComplete:
    """GHA で回していたものが1本も落ちていないこと。"""

    @pytest.mark.parametrize("script", [
        "recommend_factor_premia.py",   # recommend-factor-premia.yml
        "macro_beta_inference.py",      # macro-beta-inference.yml
        "hyperparameter_search.py",     # tune-hyperparameters.yml
        "_pipeline_vacuum.py",          # vacuum-maintenance.yml（正本側の受け皿・#290）
    ])
    def test_every_stopped_workflow_has_a_local_step(self, script):
        entrypoints = {Path(s.argv[1]).name for s in rm.steps_for("py")}
        assert script in entrypoints, (
            f"{script} を回すステップが無い＝GHA を止めたぶんの穴が空いたまま"
        )

    def test_tune_covers_the_same_three_models(self):
        """tune-hyperparameters.yml の matrix と同じ3モデルを回すこと。"""
        assert set(rm.heavy_models()) == {"macro_risk_return", "macro_gbdt", "macro_dlm"}

    def test_heavy_models_come_from_the_steps_not_a_copy(self):
        """`heavy_models()` は argv から導く＝列挙を二重に持たない（ADR-0031 の照合先）。"""
        from_argv = tuple(
            argv[argv.index("--model") + 1]
            for argv in (list(s.argv) for s in rm.steps_for("py")) if "--model" in argv
        )
        assert rm.heavy_models() == from_argv

    def test_registered_in_heavy_automation(self):
        """レジストリ側がこのバッチを指していること（逆向きは test_nightly_scores.py）。"""
        from nightly_scores import HEAVY_AUTOMATION

        for model in rm.heavy_models():
            assert HEAVY_AUTOMATION.get(model) == "local:scripts/run_monthly.py", (
                f"{model} の HEAVY_AUTOMATION 登録が月次バッチを指していない"
            )


class TestStepOrder:
    def test_inference_runs_before_tuning(self):
        """`macro_beta_loadings` は M-1 の入力。推論が後だと当月の tune が前月の β を使う。"""
        names = _names()
        assert names.index("macro_beta") < names.index("tune:macro_risk_return")

    def test_vacuum_runs_first(self):
        """VACUUM FULL は ACCESS EXCLUSIVE ロックを取るので先頭（#290）。

        後ろに置くと tune が長引いたぶん実行機会が減り、上限で打ち切られると
        一度も走らない。**打ち切りは失敗として現れない**ので気づけない。
        """
        assert _names()[0] == "vacuum"

    def test_lightest_runs_first(self):
        """打ち切られても前方は当月分が揃う（nightly_scores の NIGHTLY_MODELS と同じ思想）。

        実測 factor_premia は約2分。tune は GHA で 300〜355分の timeout を積んでいた。
        """
        names = [n for n in _names() if n != "vacuum"]   # vacuum はロック都合で別枠
        assert names[0] == "factor_premia"

    def test_m1_is_tuned_before_the_other_models(self):
        """tune の中では M-1 が先。止まって最も困るのが M-1 の μ̂ だから。"""
        names = [n for n in _names() if n.startswith("tune:")]
        assert names[0] == "tune:macro_risk_return"

    def test_every_step_states_why(self):
        for s in rm.steps_for(sys.executable):
            assert s.why.strip(), f"{s.name} に理由が書かれていない"

    def test_steps_use_the_running_interpreter(self):
        for s in rm.steps_for("/x/py.exe"):
            assert s.argv[0] == "/x/py.exe"


class TestArgsMatchTheWorkflowsTheyReplace:
    """移設のついでに探索条件を変えない（#291 の品質ゲートが別条件の値と比較される）。"""

    def test_tune_persists_both_params_and_scores(self):
        """`--persist-scores` が μ̂ の唯一の更新経路。落とすと鮮度が止まる。"""
        for model in rm.heavy_models():
            argv = _argv(f"tune:{model}")
            assert "--persist" in argv and "--persist-scores" in argv

    def test_only_gbdt_uses_random_search(self):
        """grid で張れないのは M-2 だけ（GHA で n_iter=200 相当が4〜8時間だった）。"""
        for model in rm.heavy_models():
            argv = _argv(f"tune:{model}")
            strategy = argv[argv.index("--strategy") + 1]
            assert strategy == ("random" if model == "macro_gbdt" else "grid")

    def test_gbdt_keeps_the_workflow_n_iter(self):
        argv = _argv("tune:macro_gbdt")
        assert argv[argv.index("--n-iter") + 1] == "150"

    def test_inference_uses_numpyro(self):
        """純 Python バックエンドは実測 75秒/draw＝現実的な時間で終わらない。"""
        argv = _argv("macro_beta")
        assert argv[argv.index("--nuts-sampler") + 1] == "numpyro"
        assert argv[argv.index("--init") + 1] == "adapt_diag", (
            "numpyro の既定初期化は発散多発（1/100〜91/100 divergence の実測）"
        )

    def test_inference_keeps_the_relaxed_rhat_gate(self):
        """chains=2 では r_hat が構造的に ~1.02 で頭打ちする（#341）。閾値も据え置く。"""
        argv = _argv("macro_beta")
        assert argv[argv.index("--chains") + 1] == "2"
        assert argv[argv.index("--r-hat-threshold") + 1] == "1.05"


class TestFootprintIsSeparateFromNightly:
    def test_keys_do_not_collide_with_the_nightly_batch(self):
        """月次の停止が日次の成功で隠れないこと。"""
        assert rm.KEY_LAST_RUN != rn.KEY_LAST_RUN
        assert rm.KEY_LAST_SUCCESS != rn.KEY_LAST_SUCCESS

    def test_log_is_daily_rotated_under_dot_logs(self):
        p = rm.log_path()
        assert p.parent.name == ".logs"
        assert p.name.startswith("monthly_") and p.suffix == ".log"

    def test_issue_title_names_the_monthly_batch(self):
        """日次と同じ件名だと、Issue 一覧でどちらが止まったか読めない。"""
        assert "月次" in rm.SPEC.issue_title and "{failed}" in rm.SPEC.issue_title


class TestKeepsGoing:
    def test_a_failing_step_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        """月次は1か月に1度。1本の失敗で残りを落とすと、その月ぶんが丸ごと空く。"""
        seen = []

        def fake_run(argv, **kw):
            seen.append(Path(argv[1]).name)
            return _FakeProc(returncode=1 if "macro_beta_inference.py" in argv[1] else 0)

        monkeypatch.setattr(rm.subprocess, "Popen", fake_run)
        monkeypatch.setattr(rm, "log_path", lambda now=None: tmp_path / "m.log")
        monkeypatch.setattr(rm, "record_footprint", lambda results: None)
        monkeypatch.setattr(rm, "notify", lambda results, log, run=None: None)

        code = rm.main([])
        assert len(seen) == len(_names()), "失敗の後ろが実行されていない＝途中で止まっている"
        assert code == 1


class TestTaskInstaller:
    """起動手順が再現可能な形で存在すること（人の記憶に置かない）。"""

    INSTALLER = ROOT / "scripts" / "install_monthly_task.ps1"

    def test_installer_exists_with_bom(self):
        """BOM 無しは cp932 扱いで日本語が化ける（#503 で踏んだ）。"""
        assert self.INSTALLER.is_file()
        assert self.INSTALLER.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_launcher_exists_with_bom(self):
        launcher = ROOT / "run_monthly.ps1"
        assert launcher.is_file()
        assert launcher.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_no_stray_cr_in_generated_ps1(self):
        """`\\r` がリテラルのまま混ざると parse は通るのに中身が壊れる（#503）。

        `.\\run_monthly.ps1` の `\\r` が実際の CR へ化けると `.` + CR + `un_monthly.ps1` になり、
        CRLF を除いた残りに CR として現れる。実際に `docs/DEPLOYMENT.md` で同じ壊れ方をしていた。

        **改行そのもの（LF か CRLF か）は縛らない。** このリポジトリは `.gitattributes` が無く
        `core.autocrlf=true` なので、**作業ツリーは CRLF・リポジトリと Linux の CI は LF** になる。
        当初ここに「孤立した LF が無いこと」も入れていたが、それは
        **Windows ローカルでしか通らない条件**で、ローカル 2,121 passed の直後に CI だけが落ちた
        （[[feedback_local_green_is_not_ci_green]] と同型）。改行コードは git の正規化対象なので
        テストの対象にしない。
        """
        for path in (self.INSTALLER, ROOT / "run_monthly.ps1"):
            body = path.read_bytes()[3:]
            assert b"\r" not in body.replace(b"\r\n", b""), f"{path.name}: 孤立した CR"

    def test_installer_refuses_days_that_some_months_lack(self):
        """29-31 日を許すと、その月だけ黙って走らない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "-gt 28" in text, "Day の上限チェックが無い"
