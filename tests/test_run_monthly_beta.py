"""macro_beta 専用バッチ `scripts/run_monthly_beta.py` の不変条件（Issue #579・#609）。

`macro_beta`（M-1 の入力 `macro_beta_loadings` を作る PyMC/NUTS 推論）は本番規模の実測が
**360分**（2026-09-05・3,837銘柄・n_obs 95,010・draws=800）で、月次本体の予算 180分に
収まらない。本体の窓（960分）にも増やす空きが無かった（Σ863 + マージン30）ため、#584 が
M-1 探索を切り出したのと同じ形でここへ出した。

**所要が縮む見込みが無いことは #600 で確定している**——`target_accept` を下げる案も
`max_tree_depth` を上げる案も実測で棄却され、そもそも「`steps/draw` が 1023 に張り付いて
いる」という前提自体が誤りだった（上限を倍にしても歩数も ESS もビット単位で同一）。

守るのは5点:

1. **移設が完了している**（macro_beta がここに居て、月次本体にも M-1 探索にも居ない）
2. **M-1 探索より前の日に起動する**（loadings は探索の入力なので、後ろだと常に前月の値を見る）
3. **予算が窓に収まり、窓は installer の既定と一致する**
4. **推論の引数が GHA 時代の設定と同じ**（numpyro / adapt_diag / 緩和ゲート / warmup キャップ）
5. **`--force` はタスクからは渡らない**（人手で精査したときだけの経路・#609）
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import batch_common as bc  # noqa: E402
from scripts import run_monthly as rm  # noqa: E402
from scripts import run_monthly_beta as rmb  # noqa: E402
from scripts import run_monthly_m1 as rm1  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _names() -> list[str]:
    return [s.name for s in rmb.steps_for(sys.executable)]


def _argv(name: str, force: bool = False) -> tuple[str, ...]:
    return next(s.argv for s in rmb.steps_for("py", force=force) if s.name == name)


class TestMigrationIsComplete:

    def test_macro_beta_lives_here_only(self):
        """**両方に置くと同じ月に2回推論し、片方だけ消すと黙って更新が止まる。**"""
        assert "macro_beta" in _names()
        for mod, label in ((rm, "月次本体"), (rm1, "M-1 探索")):
            assert "macro_beta" not in [s.name for s in mod.steps_for("py")], (
                f"{label} にも macro_beta が残っている"
            )

    def test_footprint_keys_do_not_collide(self):
        """足跡キーが本体と衝突すると、片方の実行がもう片方の鮮度を偽装する。"""
        keys = {rmb.KEY_LAST_RUN, rmb.KEY_LAST_SUCCESS}
        for mod in (rm, rm1):
            assert not keys & {mod.KEY_LAST_RUN, mod.KEY_LAST_SUCCESS}

    def test_watched_by_the_freshness_check(self):
        """監視表に無いと「走らなかったのに誰も気づかない」（#515 の穴そのもの）。"""
        import batch_freshness as bf

        assert rmb.KEY_LAST_RUN in {w.key_run for w in bf.WATCHED}

    def test_heavy_models_come_from_the_steps_not_a_copy(self):
        """`macro_beta` は分析プラグインではないので `--model` を持たず、ここは空になる。

        列挙を二重に持たない形（argv から抜く）を本体・M-1 と揃えていることの確認で、
        「空だからテストも要らない」ではない——形が崩れると `HEAVY_AUTOMATION` の照合が
        静かにずれる。
        """
        assert rmb.heavy_models() == ()


class TestStepOrder:

    def test_deps_smoke_runs_before_the_inference(self):
        """2026-09-01 は Smart App Control が未評価の jaxlib DLL を初回ロードで弾き、
        `macro_beta` が 180分の予算ではなく 1.4分の exit=1 で落ちて1か月ぶん固着した。
        先に消化しておけば本番ステップは通り、消化できなければ予算を待たずに失敗が現れる。
        """
        names = _names()
        assert names.index("deps_smoke") < names.index("macro_beta")

    def test_every_step_states_why(self):
        for s in rmb.steps_for(sys.executable):
            assert s.why, f"{s.name} に why が無い（ログだけ見て意図が分からなくなる）"

    def test_steps_use_the_running_interpreter(self):
        for s in rmb.steps_for(sys.executable):
            assert s.argv[0] == sys.executable


class TestInferenceArgs:
    """GHA（macro-beta-inference.yml）で回していた設定と同じであること。"""

    def test_uses_numpyro(self):
        """純 Python バックエンドは実測 75秒/draw＝現実的な時間で終わらない。"""
        argv = _argv("macro_beta")
        assert argv[argv.index("--nuts-sampler") + 1] == "numpyro"
        assert argv[argv.index("--init") + 1] == "adapt_diag", (
            "numpyro の既定初期化は発散多発（1/100〜91/100 divergence の実測）"
        )

    def test_keeps_the_relaxed_rhat_gate(self):
        """無人実行の閾値は 1.05（strict 1.01 は chains=2 では構造的に届かない・#341）。"""
        argv = _argv("macro_beta")
        assert argv[argv.index("--r-hat-threshold") + 1] == "1.05"

    def test_keeps_the_warmup_only_treedepth_cap(self):
        """`8,10` は **warmup だけ** 2**8-1 歩に切る（ADR-0002）。

        一律キャップ（`--max-tree-depth 8`）は ESS/歩 では最良に見えるのに `ess_bulk_min` が
        3.55 まで落ち `r_hat` が 1.63 になる＝**採ってはいけない**。値が `8` へ縮むと
        黙って崩壊するので、ここで文字列ごと固定する。
        """
        argv = _argv("macro_beta")
        assert argv[argv.index("--max-tree-depth") + 1] == "8,10"

    def test_force_is_opt_in_only(self):
        """`--force` は人手で精査したときの経路。**タスクからは渡らない**（#609）。

        既定で付いていると、収束ゲートに落ちた run が毎月そのままライブへ出る。
        """
        assert "--force" not in _argv("macro_beta")
        assert "--force" in _argv("macro_beta", force=True)

    def test_force_flag_is_stripped_before_the_common_parser(self, monkeypatch):
        """共通パーサ（`batch_common.build_parser`）は `--force` を知らない。

        外し忘れると `--force` 付きの実行が argparse のエラーで即死する——**そして
        それは「ゲートに落ちた」と区別が付かない形でログに出る**。
        """
        seen = {}

        def fake_run_batch(spec, steps, hooks, argv):
            seen["argv"] = list(argv)
            seen["steps"] = [s.argv for s in steps]
            return 0

        monkeypatch.setattr(bc, "run_batch", fake_run_batch)
        assert rmb.main(["--force", "--dry-run"]) == 0
        assert "--force" not in seen["argv"], "共通パーサへ --force が漏れている"
        assert "--dry-run" in seen["argv"]
        assert any("--force" in argv for argv in seen["steps"]), "ステップ側へ届いていない"


class TestBudgetFitsTheWindow:
    INSTALLER = ROOT / "scripts" / "install_monthly_beta_task.ps1"

    def test_every_step_has_a_budget(self):
        missing = [s.name for s in rmb.steps_for(sys.executable) if s.budget_min is None]
        assert not missing, f"予算の無いステップ: {missing}（BUDGET_MIN への追加漏れ）"

    def test_budget_fits_the_window(self):
        problem = bc.window_problem(rmb.steps_for(sys.executable), rmb.WINDOW_MIN)
        assert problem is None, problem

    def test_window_matches_the_installer_default(self):
        """`-Hours` と `WINDOW_MIN` はセットでしか意味を持たない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Hours\s*=\s*(\d+)", text)
        assert m, "install_monthly_beta_task.ps1 から既定の -Hours を読めない（書式が変わった）"
        assert int(m.group(1)) * 60 == rmb.WINDOW_MIN

    def test_budget_covers_the_measured_duration(self):
        """実測 360分を**下回らない**こと。

        下回ると毎月 exit=124 で打ち切られ、persist に到達しないので何も残らない。
        上限は窓 − マージン − deps_smoke（ADR-0040 に従い実測へ寄せず窓から導出する）。
        """
        budget = rmb.BUDGET_MIN["macro_beta"]
        assert budget >= 360, f"実測 360分を下回る予算 {budget}分＝毎月打ち切られて何も残らない"
        assert budget <= rmb.WINDOW_MIN - bc.WINDOW_MARGIN_MIN - rmb.BUDGET_MIN["deps_smoke"]


class TestTaskInstaller:
    INSTALLER = ROOT / "scripts" / "install_monthly_beta_task.ps1"
    SHARED_INSTALLER = ROOT / "scripts" / "install_monthly_task.ps1"
    M1_INSTALLER = ROOT / "scripts" / "install_monthly_m1_task.ps1"
    LAUNCHER = ROOT / "run_monthly_beta.ps1"

    def test_installer_and_launcher_exist(self):
        """**起動手順を人の記憶に置かない**——PC を入れ替えた時点で黙って消える。"""
        assert self.INSTALLER.is_file()
        assert self.LAUNCHER.is_file()

    def test_ps1_files_have_a_bom(self):
        for path in (self.INSTALLER, self.LAUNCHER):
            assert path.read_bytes()[:3] == b"\xef\xbb\xbf", f"{path.name}: BOM が無い"

    def test_no_stray_cr_in_ps1(self):
        for path in (self.INSTALLER, self.LAUNCHER):
            body = path.read_bytes()[3:]
            assert b"\r" not in body.replace(b"\r\n", b""), f"{path.name}: 孤立した CR"

    def test_runs_before_the_m1_search_and_after_the_body(self):
        """**依存順は起動日でしか担保されない**（別タスクなので実行順の保証が他に無い）。

        `macro_beta_loadings` は `tune:macro_risk_return` の入力。探索が先に走ると、
        毎月「前月の loadings で探索する」ことになり、しかもそれは失敗として現れない。
        """
        def _day(path: Path) -> int:
            m = re.search(r"\[int\]\$Day\s*=\s*(\d+)", path.read_text(encoding="utf-8-sig"))
            assert m, f"{path.name} から既定の -Day を読めない（書式が変わった）"
            return int(m.group(1))

        beta_day = _day(self.INSTALLER)
        assert beta_day != 1, "月次本体（1日）と同じ日に走る設定になっている"
        assert beta_day < _day(self.M1_INSTALLER), (
            "macro_beta が M-1 探索より後の日に起動する＝探索が常に前月の loadings を見る"
        )

    def test_task_name_differs_from_the_others(self):
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "financial_app-monthly-beta" in text

    def test_installer_delegates_instead_of_duplicating(self):
        """登録ロジックは1本（月次本体の installer へ委譲）＝片方だけ直す事故を防ぐ。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "install_monthly_task.ps1" in text
        assert "run_monthly_beta.ps1" in text

    def test_launcher_exposes_force(self):
        """`-Force` を起動口が持たないと、止血の1回を回す経路が無くなる（#609）。"""
        text = self.LAUNCHER.read_text(encoding="utf-8-sig")
        assert re.search(r"\[switch\]\$Force", text), "-Force パラメータが無い"
        assert '"--force"' in text
