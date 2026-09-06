"""週次バックアップバッチ `scripts/run_backup.py` の不変条件（Issue #606・親 #503）。

#503 で復元経路は通した（Storage から17表を落として使い捨てクラスタへ戻し、行数一致と
画面9本の表示まで確認済み）。しかし取る側は手で叩く CLI のままで、どのバッチにも
入っていなかった＝**戻せるのは最後に人が思い出した日まで**だった。

守るのは5点:

1. **ステップの argv が `--apply` と `--dest storage` を両方持つ**（どちらが欠けても
   exit 0 で返るため、失敗としては現れない）
2. **予算が窓に収まり、窓は installer の既定と一致する**
3. **監視表に載っている**（取り忘れを現す手段が他に無い）
4. **ローカル世代の掃除が dest によらず走り、復元予行の産物を消さない**
5. **起動口と登録スクリプトが実在し、登録後の検証が夜間バッチと同じ4項目を見る**
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import backup_push as bp  # noqa: E402
from scripts import batch_common as bc  # noqa: E402
from scripts import run_backup as rb  # noqa: E402
from scripts import run_monthly as rm  # noqa: E402
from scripts import run_monthly_beta as rmb  # noqa: E402
from scripts import run_monthly_m1 as rm1  # noqa: E402
from scripts import run_nightly as rn  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _argv(name: str) -> tuple[str, ...]:
    return next(s.argv for s in rb.steps_for("py") if s.name == name)


class TestPushArgs:
    """**両方のフラグが要る。** どちらが欠けても backup_push は exit 0 を返す。"""

    def test_apply_is_present(self):
        """`--apply` が無いとドライラン＝計画を出して何も取らずに成功する。"""
        assert "--apply" in _argv("push")

    def test_destination_is_storage(self):
        """`--dest storage` が無いとローカルの `.backups/` に作るだけになる。

        それは正本と同じディスク上にあり、ディスクが飛べば一緒に失われる＝
        **バックアップとして意味を成さない**。しかもログには「完了」と出る。
        """
        argv = _argv("push")
        assert argv[argv.index("--dest") + 1] == "storage"

    def test_every_step_states_why(self):
        for s in rb.steps_for(sys.executable):
            assert s.why, f"{s.name} に why が無い（ログだけ見て意図が分からなくなる）"

    def test_steps_use_the_running_interpreter(self):
        for s in rb.steps_for(sys.executable):
            assert s.argv[0] == sys.executable

    def test_heavy_models_come_from_the_steps_not_a_copy(self):
        """バックアップは分析ではないので `--model` を持たず、ここは空になる。"""
        assert rb.heavy_models() == ()


class TestBudgetFitsTheWindow:
    INSTALLER = ROOT / "scripts" / "install_backup_task.ps1"

    def test_every_step_has_a_budget(self):
        missing = [s.name for s in rb.steps_for(sys.executable) if s.budget_min is None]
        assert not missing, f"予算の無いステップ: {missing}（BUDGET_MIN への追加漏れ）"

    def test_budget_fits_the_window(self):
        problem = bc.window_problem(rb.steps_for(sys.executable), rb.WINDOW_MIN)
        assert problem is None, problem

    def test_window_matches_the_installer_default(self):
        """`-Hours` と `WINDOW_MIN` はセットでしか意味を持たない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Hours\s*=\s*(\d+)", text)
        assert m, "install_backup_task.ps1 から既定の -Hours を読めない（書式が変わった）"
        assert int(m.group(1)) * 60 == rb.WINDOW_MIN


class TestWatched:

    def test_footprint_keys_do_not_collide(self):
        """足跡キーが他バッチと衝突すると、片方の実行がもう片方の鮮度を偽装する。"""
        keys = {rb.KEY_LAST_RUN, rb.KEY_LAST_SUCCESS}
        for mod in (rn, rm, rmb, rm1):
            assert not keys & {mod.KEY_LAST_RUN, mod.KEY_LAST_SUCCESS}

    def test_watched_by_the_freshness_check(self):
        """監視表に無いと「取り忘れたのに誰も気づかない」（#515 の穴そのもの）。"""
        import batch_freshness as bf

        assert rb.KEY_LAST_RUN in {w.key_run for w in bf.WATCHED}

    def test_threshold_is_derived_from_the_cadence_and_window(self):
        """閾値は `cadence + 窓` の導出であって、直接置く定数ではない（ADR-0042）。"""
        import batch_freshness as bf

        w = next(w for w in bf.WATCHED if w.key_run == rb.KEY_LAST_RUN)
        assert w.window_min == rb.WINDOW_MIN, "窓を書き写している（import で受けること）"
        assert w.cadence_h == 7 * 24.0
        assert w.stale_h == 7 * 24.0 + rb.WINDOW_MIN / 60.0


class TestLocalRetention:
    """`--dest storage` でもローカルを掃除する（#606）。

    掃除を storage 側だけにかけると、週次で 37.5MB/週＝年 1.9GB が正本と同じディスクへ
    積み上がる。
    """

    def _make(self, tmp_path: Path, stamps: list) -> None:
        for s in stamps:
            d = tmp_path / s
            d.mkdir()
            (d / bp.MANIFEST_NAME).write_text("{}", encoding="utf-8")
            (d / "companies.dump").write_bytes(b"x")

    def test_prune_local_applies_the_same_policy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bp, "LOCAL_STORE", tmp_path)
        stamps = ["20260601T000000Z", "20260602T000000Z", "20260701T000000Z",
                  "20260702T000000Z", "20260703T000000Z", "20260704T000000Z"]
        self._make(tmp_path, stamps)

        dropped = bp.prune_local(keep_recent=2, keep_monthly=2, echo=lambda *_: None)

        # 直近2世代（0703/0704）＋各月の最初（0601/0701）が残る
        assert set(bp.local_generations()) == {"20260601T000000Z", "20260701T000000Z",
                                               "20260703T000000Z", "20260704T000000Z"}
        assert set(dropped) == {"20260602T000000Z", "20260702T000000Z"}
        for s in dropped:
            assert not (tmp_path / s).exists(), f"{s} のディレクトリが残っている"

    def test_restore_drill_directory_is_not_a_generation(self, tmp_path, monkeypatch):
        """`.backups/_from_storage/` は `backup_restore --source storage` の落とし先。

        直下に manifest.json を持たないので世代として数えない＝**掃除で消さない**。
        ここを取り違えると、復元予行の途中で足元のダンプが消える。
        """
        monkeypatch.setattr(bp, "LOCAL_STORE", tmp_path)
        self._make(tmp_path, ["20260701T000000Z"])
        nested = tmp_path / "_from_storage" / "20260101T000000Z"
        nested.mkdir(parents=True)
        (nested / bp.MANIFEST_NAME).write_text("{}", encoding="utf-8")

        assert bp.local_generations() == ["20260701T000000Z"]
        assert bp.prune_local(keep_recent=1, keep_monthly=1, echo=lambda *_: None) == []
        assert nested.is_dir(), "復元予行の落とし先が消えている"

    def test_main_prunes_local_outside_the_dest_branch(self):
        """`main` の掃除呼び出しが `if args.dest == "storage":` の外にあること。

        中に入れると storage 経路でローカルが溜まり続ける——これが #606 で直した形で、
        **ディスクが埋まるまで失敗として現れない**ので実行では検知できない。
        """
        src = (ROOT / "scripts" / "backup_push.py").read_text(encoding="utf-8")
        body = src[src.index("def main("):]
        head = body[:body.index("prune_local(args.keep_recent")]
        indent = head.rsplit("\n", 1)[-1]
        assert indent == "    ", f"prune_local がネストされている（インデント {len(indent)}）"


class TestTaskInstaller:
    INSTALLER = ROOT / "scripts" / "install_backup_task.ps1"
    NIGHTLY_INSTALLER = ROOT / "scripts" / "install_nightly_task.ps1"
    LAUNCHER = ROOT / "run_backup.ps1"

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

    def test_task_name_differs_from_the_others(self):
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "financial_app-backup" in text
        assert "run_backup.ps1" in text

    def test_trigger_is_weekly(self):
        """週次でないと WATCHED の cadence（7日）と食い違い、閾値が意味を失う。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "-Weekly -DaysOfWeek" in text, "週次トリガになっていない"

    def test_verification_matches_the_nightly_installer(self):
        """**登録できたことを確かめてから成功を出す。**

        ScheduledTasks の cmdlet は失敗しても非終了エラーで返すため、確認しないと
        「登録しました」と嘘をつく（install_monthly_task.ps1 で実際に起きた）。
        夜間バッチ側と同じ4項目を見ていること＝片方だけ緩むのを防ぐ。
        """
        mine = self.INSTALLER.read_text(encoding="utf-8-sig")
        theirs = self.NIGHTLY_INSTALLER.read_text(encoding="utf-8-sig")
        probes = ("$info.NextRunTime", '$logon -ne "S4U"', '$swa -ne "true"',
                  '$runlevel -eq "HighestAvailable"')
        for probe in probes:
            assert probe in theirs, f"夜間バッチ側から {probe} が消えた（この照合が無意味になる）"
            assert probe in mine, f"登録後の検証に {probe} が無い"

    def test_launcher_pins_the_local_target(self):
        """バックアップ元は正本＝ローカル。

        リモートを引いたものは Supabase の自己複製で、正本が失われたときに役に立たない
        （`backup_push.guard_source_is_primary` が実際に弾く）。
        """
        text = self.LAUNCHER.read_text(encoding="utf-8-sig")
        assert 'FINAPP_DB_TARGET = "local"' in text
