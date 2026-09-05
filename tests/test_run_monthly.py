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
        """受け皿は**ローカル駆動バッチ全体**で見る（1本のファイルに限らない）。

        `macro_beta` は #579 で `run_monthly_beta.py` へ出た。ここを本体だけで見ていると、
        別タスクへ切り出した瞬間に「GHA を止めたぶんの穴」と区別が付かなくなる。
        """
        from scripts import run_monthly_beta as rmb
        from scripts import run_monthly_m1 as rm1

        entrypoints = {Path(s.argv[1]).name
                       for mod in (rm, rmb, rm1)
                       for s in mod.steps_for("py")}
        assert script in entrypoints, (
            f"{script} を回すステップが無い＝GHA を止めたぶんの穴が空いたまま"
        )

    def test_tune_covers_the_same_three_models(self):
        """tune-hyperparameters.yml の matrix と同じ3モデルを回すこと。

        **M-1 は別タスク**（`scripts/run_monthly_m1.py`・#584）なので、網羅は2本の合併で見る。
        片方だけを見ると「M-1 が消えた」ことに気づけない——移設の穴は失敗として現れない。
        """
        from scripts import run_monthly_m1 as rm1

        assert set(rm.heavy_models()) | set(rm1.heavy_models()) == {
            "macro_risk_return", "macro_gbdt", "macro_dlm"}

    def test_m1_is_not_in_the_monthly_body(self):
        """M-1 は月次本体に**居ない**（#584）。

        実測 2.61分/件 × 288件 ＝ 約752分で窓（960分）に入らず、`hyperparameter_search` は
        完走してからしか永続化しないため、月次に置くと 250分を使って何も残さない。
        """
        assert "macro_risk_return" not in rm.heavy_models()

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
        """`macro_beta_loadings` は M-1 の入力。推論が後だと当月の tune が前月の β を使う。

        **推論も探索も別タスクへ出た**（#584 / #579）ので、この依存は日をまたぐ形で成立する:
        macro_beta は毎月2日・M-1 探索は毎月3日。順序の根拠は `install_*_task.ps1` の既定 Day で、
        **ここを読まずに「本体にあるか」で代用すると、切り出したときに黙って壊れる**
        （実際 #579 で本体から出したときこのテストが落ちた）。
        """
        import re

        from scripts import run_monthly_beta as rmb
        from scripts import run_monthly_m1 as rm1

        owners = {name: [s.name for s in mod.steps_for("py")]
                  for name, mod in (("beta", rmb), ("m1", rm1), ("monthly", rm))}
        assert "macro_beta" in owners["beta"]
        assert "macro_beta" not in owners["m1"] and "macro_beta" not in owners["monthly"], (
            "macro_beta を複数のバッチで回すと同じ月に2回推論することになる"
        )

        def _day(ps1_name: str) -> int:
            text = (ROOT / "scripts" / ps1_name).read_text(encoding="utf-8-sig")
            m = re.search(r"\[int\]\$Day\s*=\s*(\d+)", text)
            assert m, f"{ps1_name} から既定 Day を読めない（書式が変わった）"
            return int(m.group(1))

        assert _day("install_monthly_beta_task.ps1") < _day("install_monthly_m1_task.ps1"), (
            "macro_beta が M-1 探索より後の日に起動する＝探索が常に前月の loadings を見る"
        )

    def test_vacuum_runs_first(self):
        """VACUUM FULL は ACCESS EXCLUSIVE ロックを取るので先頭（#290）。

        後ろに置くと tune が長引いたぶん実行機会が減り、上限で打ち切られると
        一度も走らない。**打ち切りは失敗として現れない**ので気づけない。
        """
        assert _names()[0] == "vacuum"

    def test_lightest_runs_first(self):
        """打ち切られても前方は当月分が揃う（nightly_scores の NIGHTLY_MODELS と同じ思想）。

        実測 price_suffix は約5秒・factor_premia は約2分。tune は GHA で 300〜355分の
        timeout を積んでいた。

        **名前で固定しない**——ステップを足すたびにここを書き換える形にすると、
        「軽い順」という不変条件ではなく「そのときの並び」を検査することになる
        （実際 #560 で price_suffix を足したとき、意図は満たしているのに落ちた）。
        """
        steps = [s for s in rm.steps_for(sys.executable) if s.name != "vacuum"]
        assert steps[0].budget_min == min(s.budget_min for s in steps), (
            f"先頭が最軽量でない: {steps[0].name}({steps[0].budget_min}分) / "
            f"最小 {min(s.budget_min for s in steps)}分"
        )

    def test_deps_smoke_runs_before_the_steps_that_need_those_imports(self):
        """重い依存の import 確認は、それを実際に使うステップより前（2026-09-01）。

        後ろに置くと役目が消える——Smart App Control が未評価の jaxlib DLL を初回ロードで
        ブロックしたとき、`macro_beta` は 180分の予算ではなく **1.4分の exit=1** で落ち、
        その月の `macro_beta_loadings` が丸ごと固着した。先に消化しておけば本番ステップは
        通り、消化できなければ予算を待たずに失敗として現れる。
        """
        names = _names()
        for name in (n for n in names if n.startswith("tune:")):
            assert names.index("deps_smoke") < names.index(name)
        # macro_beta を持つバッチ側でも同じ不変条件が要る（#579 で出ていった先）。
        from scripts import run_monthly_beta as rmb

        beta_names = [s.name for s in rmb.steps_for("py")]
        assert beta_names.index("deps_smoke") < beta_names.index("macro_beta")

    def test_remaining_tunes_are_ordered_lightest_first(self):
        """M-1 が別タスクへ出た後、本体に残る tune は M-3 → M-2（#584）。

        打ち切られても前方は当月分が揃う、という「軽い順」の思想は変わらない。
        M-1 の「止まって最も困る」優先は、専用タスクを持たせたことで満たされている。
        """
        names = [n for n in _names() if n.startswith("tune:")]
        assert names == ["tune:macro_dlm", "tune:macro_gbdt"]

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
            # **本体に実在するステップで失敗させる**（#579 で macro_beta が出ていったので、
            # 存在しない名前を書くと「全部成功」になってこのテスト自体が無意味になる）。
            return _FakeProc(returncode=1 if "recommend_factor_premia.py" in argv[1] else 0)

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


# ── ステップの時間予算（Issue #530）──────────────────────────────────────────
#
# ステップ順は「打ち切られても軽い順に並べてあるので前方は当月分が揃う」という設計だが、
# **前方の1本が窓を食い尽くすとその設計が成立しない**。2026-09-01 がまさにそれで、
# macro_beta（ローカル実測 741.5分でも未完走・#512）が16時間を使い切り、tune×3 は
# 一度も起動しないはずだった。μ̂ の最終更新は 2026-07-10。

class TestBudgetFitsTheWindow:
    INSTALLER = ROOT / "scripts" / "install_monthly_task.ps1"

    def test_every_step_has_a_budget(self):
        """1本でも無期限があれば窓の保証はその時点で消える。"""
        missing = [s.name for s in rm.steps_for(sys.executable) if s.budget_min is None]
        assert not missing, f"予算の無いステップ: {missing}（BUDGET_MIN への追加漏れ）"

    def test_budgets_fit_inside_the_scheduler_window(self):
        import scripts.batch_common as bc
        problem = bc.window_problem(rm.steps_for(sys.executable), rm.WINDOW_MIN)
        assert problem is None, problem

    def test_window_matches_the_installer(self):
        """`install_monthly_task.ps1` の既定 `-Hours` と `WINDOW_MIN` を照合する。

        窓と予算はセットでしか意味を持たない。片方だけ動かしても**失敗としては現れない**
        （窓が足りなければ最後のステップが黙って打ち切られ、窓が余れば使われないだけ）ので
        CI で見るしかない。`tests/test_db_target.py` が `launch.py` の既定を `database` 側と
        照合しているのと同型。
        """
        import re
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Hours\s*=\s*(\d+)", text)
        assert m, "install_monthly_task.ps1 から既定の -Hours を読めない（書式が変わった）"
        assert int(m.group(1)) * 60 == rm.WINDOW_MIN, (
            f"ps1 の既定 {m.group(1)}時間 と run_monthly.WINDOW_MIN {rm.WINDOW_MIN}分 が食い違う"
        )

    def test_the_tune_steps_can_still_start_after_everything_before_them(self):
        """**tune×3 が窓に届く**こと＝本 Issue（#530）の目的そのもの。

        前方（vacuum / factor_premia / macro_beta）の予算を全部使い切っても、後ろの tune が
        開始でき、かつ最後の1本が窓の中で終われることを見る。ここが崩れたら、並びを
        変えたか予算を膨らませたかのどちらか。
        """
        steps = rm.steps_for(sys.executable)
        elapsed = 0.0
        for s in steps:
            assert elapsed < rm.WINDOW_MIN, f"{s.name} が窓の外で開始することになる"
            elapsed += s.budget_min
        assert elapsed <= rm.WINDOW_MIN, "最後のステップが窓から溢れる"

    def test_macro_beta_is_no_longer_budgeted_here(self):
        """`macro_beta` は #579 で `run_monthly_beta.py` へ出た。

        本体に予算だけ残っていると「Σ が窓に収まる」判定が実態とずれる（走らないものに
        180分を積んだまま tune の余地を狭める）。**出したら予算も消す**ことをここで縛る。
        """
        assert "macro_beta" not in rm.BUDGET_MIN
        assert "macro_beta" not in [s.name for s in rm.steps_for(sys.executable)]

    def test_dry_run_shows_the_budget(self, capsys):
        rm.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "予算" in out and "factor_premia" in out
