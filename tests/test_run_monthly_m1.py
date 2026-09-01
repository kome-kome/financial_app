"""M-1 専用バッチ `scripts/run_monthly_m1.py` の不変条件（Issue #584・親 #532 / #579）。

2026-09-01 の月次バッチ初実走で、`tune:macro_risk_return` は 250分の予算を使い切って
15/288 しか進まなかった。#588 のキャッシュ修正でメモリ枯渇による停止は解消したが、所要は
変わらない——実測 2.61分/件 × 288件 ＝ 約752分で、月次の窓（960分）のほぼ全部を1本で食う。

`hyperparameter_search` は `search()` が完走してからしか永続化しないので、**予算内に
終わらないステップは時間を使い切って何も残さない**。9/1 は M-1 に 250分・M-3 に 250分を
与えて両方とも成果ゼロだった。そこで「完走見込みのあるステップにだけ予算を与える」を
原則に、M-1 をこの専用タスク（毎月2日 JST 01:00）へ出した。

守るのは4点:

1. **移設が完了している**（M-1 がここに居て、月次本体には居ない）
2. **予算が窓に収まる**（Σ + マージン ≤ 窓・`install_*_task.ps1` の既定と一致）
3. **`HEAVY_AUTOMATION` がこのバッチを指す**（ADR-0031。登録漏れは失敗として現れない）
4. **起動手順が再現できる**（登録スクリプトが実在し、本体と別の日・別のタスク名を持つ）
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import batch_common as bc  # noqa: E402
from scripts import run_monthly as rm  # noqa: E402
from scripts import run_monthly_m1 as rm1  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _names() -> list[str]:
    return [s.name for s in rm1.steps_for(sys.executable)]


class TestMigrationIsComplete:

    def test_m1_lives_here_and_not_in_the_monthly_body(self):
        """**両方に置くと同じ月に2回回り、片方だけ消すと黙って更新が止まる。**"""
        assert rm1.heavy_models() == ("macro_risk_return",)
        assert "macro_risk_return" not in rm.heavy_models()

    def test_heavy_models_come_from_the_steps_not_a_copy(self):
        """`heavy_models()` は argv から導く＝列挙を二重に持たない（ADR-0031 の照合先）。"""
        from_argv = tuple(
            argv[argv.index("--model") + 1]
            for argv in (list(s.argv) for s in rm1.steps_for("py")) if "--model" in argv
        )
        assert rm1.heavy_models() == from_argv

    def test_registered_in_heavy_automation(self):
        from nightly_scores import HEAVY_AUTOMATION

        for model in rm1.heavy_models():
            assert HEAVY_AUTOMATION.get(model) == "local:scripts/run_monthly_m1.py", (
                f"{model} の HEAVY_AUTOMATION 登録がこのバッチを指していない"
            )

    def test_persists_scores(self):
        """`--persist-scores` が無いと μ̂ が更新されない＝走っても鮮度が出ない。"""
        argv = [s.argv for s in rm1.steps_for("py") if s.name.startswith("tune:")][0]
        assert "--persist" in argv and "--persist-scores" in argv

    def test_footprint_keys_do_not_collide_with_the_monthly_body(self):
        """足跡が混ざると「M-1 が走らなかった月」が本体の成功で隠れる。"""
        assert rm1.KEY_LAST_RUN != rm.KEY_LAST_RUN
        assert rm1.KEY_LAST_SUCCESS != rm.KEY_LAST_SUCCESS
        assert rm1.SPEC.log_prefix != rm.SPEC.log_prefix


class TestStepOrder:

    def test_deps_smoke_runs_before_the_tuning(self):
        """重い依存を import できないなら探索も同じ理由で落ちる。900分待たずに現れる方がよい。"""
        names = _names()
        assert names.index("deps_smoke") < names.index("tune:macro_risk_return")

    def test_every_step_states_why(self):
        for s in rm1.steps_for(sys.executable):
            assert s.why.strip(), f"{s.name} に理由が書かれていない"

    def test_steps_use_the_running_interpreter(self):
        for s in rm1.steps_for("/x/py.exe"):
            assert s.argv[0] == "/x/py.exe"


class TestBudgetFitsTheWindow:
    INSTALLER = ROOT / "scripts" / "install_monthly_m1_task.ps1"

    def test_every_step_has_a_budget(self):
        missing = [s.name for s in rm1.steps_for(sys.executable) if s.budget_min is None]
        assert not missing, f"予算の無いステップ: {missing}（BUDGET_MIN への追加漏れ）"

    def test_budget_fits_the_window(self):
        problem = bc.window_problem(rm1.steps_for(sys.executable), rm1.WINDOW_MIN)
        assert problem is None, problem

    def test_window_matches_the_installer_default(self):
        """`-Hours` と `WINDOW_MIN` はセットでしか意味を持たない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Hours\s*=\s*(\d+)", text)
        assert m, "install_monthly_m1_task.ps1 から既定の -Hours を読めない（書式が変わった）"
        assert int(m.group(1)) * 60 == rm1.WINDOW_MIN, (
            f"ps1 の既定 {m.group(1)}時間 と run_monthly_m1.WINDOW_MIN {rm1.WINDOW_MIN}分 が食い違う"
        )

    def test_budget_covers_the_measured_duration(self):
        """実測 752分（2.61分/件 × 288件）を**下回らない**こと。

        下回ると毎月 exit=124 で打ち切られ、永続化まで到達しないので何も残らない
        （9/1 に M-1・M-3 の両方で実際に起きた）。上限は窓 − マージン − deps_smoke。
        """
        budget = rm1.BUDGET_MIN["tune:macro_risk_return"]
        assert budget >= 752, f"実測 752分を下回る予算 {budget}分＝毎月打ち切られて何も残らない"
        assert budget <= rm1.WINDOW_MIN - bc.WINDOW_MARGIN_MIN - rm1.BUDGET_MIN["deps_smoke"]


class TestTaskInstaller:
    INSTALLER = ROOT / "scripts" / "install_monthly_m1_task.ps1"
    SHARED_INSTALLER = ROOT / "scripts" / "install_monthly_task.ps1"
    LAUNCHER = ROOT / "run_monthly_m1.ps1"

    def test_installer_and_launcher_exist(self):
        """**起動手順を人の記憶に置かない**——PC を入れ替えた時点で黙って消える。"""
        assert self.INSTALLER.is_file()
        assert self.LAUNCHER.is_file()

    def test_ps1_files_have_a_bom(self):
        """BOM 無しだと PowerShell が cp932 扱いして日本語が化ける。"""
        for path in (self.INSTALLER, self.LAUNCHER):
            assert path.read_bytes()[:3] == b"\xef\xbb\xbf", f"{path.name}: BOM が無い"

    def test_no_stray_cr_in_ps1(self):
        for path in (self.INSTALLER, self.LAUNCHER):
            body = path.read_bytes()[3:]
            assert b"\r" not in body.replace(b"\r\n", b""), f"{path.name}: 孤立した CR"

    def test_runs_on_a_different_day_than_the_monthly_body(self):
        """同じ日に両方走らせると 2つの重いバッチが窓を奪い合う。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Day\s*=\s*(\d+)", text)
        assert m, "既定の -Day を読めない（書式が変わった）"
        assert int(m.group(1)) != 1, "月次本体（1日）と同じ日に走る設定になっている"

    def test_task_name_differs_from_the_monthly_body(self):
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "financial_app-monthly-m1" in text

    def test_installer_delegates_instead_of_duplicating(self):
        """登録ロジックは1本（月次本体の installer へ委譲）＝片方だけ直す事故を防ぐ。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "install_monthly_task.ps1" in text
        assert "run_monthly_m1.ps1" in text

    def test_shared_installer_accepts_the_script_parameter(self):
        """委譲先が `-Script` を受け取れること（受け取れないと既定の本体を登録してしまう）。"""
        text = self.SHARED_INSTALLER.read_text(encoding="utf-8-sig")
        assert re.search(r"\[string\]\$Script\s*=", text), "-Script パラメータが無い"
