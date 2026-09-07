"""平日日中バッチ `scripts/run_daytime.py` の不変条件（Issue #618）。

2026-09-07 に `macro_beta` を手動で回したところ、**同一パネル・同一設定・同一コードなのに
発散が 0 → 344 回に増え**、収束ゲートに落ちて隔離された。9/6 の run との差は、その7時間の
裏で重いテストを並走させたことしか見当たらない。本番の推論経路にはスレッド固定が無く、
XLA が使うコア数は実行時の混み具合で変わる。つまり**「重い計算の裏で作業をしない」という
運用条件が結果の再現性に直結している**。人が PC を触らない平日日中を専用の枠にした。

守るのは5点:

1. **キューは先頭を取り除いてから返す**（失敗しても戻さない＝同じ計算を繰り返さない）
2. **窓に入らない仕事は積ませない**（走ってから打ち切られると何も残らない）
3. **予算が窓に収まり、窓は installer の既定と一致する**
4. **監視表に載っている**（走らなかったことに気づけるのはここだけ）
5. **重い計算の引数が既存バッチと同一**（片方だけ動かすと同じ名前の別物を測る）
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch_freshness  # noqa: E402
from scripts import batch_common as bc  # noqa: E402
from scripts import run_daytime as rd  # noqa: E402
from scripts import run_monthly as rm  # noqa: E402
from scripts import run_monthly_beta as rmb  # noqa: E402
from scripts import run_monthly_m1 as rm1  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class _FakeDB:
    """`get_setting` / `upsert_setting` だけを持つ最小の器（実 DB を触らない）。"""

    def __init__(self, initial=None):
        self.store = dict(initial or {})


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()

    def _get(session, key):
        return session.store.get(key)

    def _upsert(session, key, value):
        session.store[key] = value

    import database

    monkeypatch.setattr(database, "get_setting", _get)
    monkeypatch.setattr(database, "upsert_setting", _upsert)
    return db


class TestQueue:
    def test_enqueue_then_read_round_trips(self, fake_db):
        rd.enqueue(["beta", "tune:macro_gbdt"], db=fake_db)
        assert rd.read_queue(db=fake_db) == ["beta", "tune:macro_gbdt"]

    def test_pop_removes_the_head_before_returning(self, fake_db):
        """**取り出したら戻さない。**

        失敗した仕事を戻すと毎日同じ計算を繰り返し、先へ進まなくなる——このバッチを
        作った動機がまさにそれ。再試行は積み直しで行う。
        """
        rd.enqueue(["beta", "tune:macro_dlm"], db=fake_db)
        assert rd.pop_queue(db=fake_db) == "beta"
        assert rd.read_queue(db=fake_db) == ["tune:macro_dlm"], "先頭が残っている"

    def test_pop_on_empty_queue_is_none_not_an_error(self, fake_db):
        assert rd.pop_queue(db=fake_db) is None

    def test_unknown_job_is_rejected_at_enqueue(self, fake_db):
        """走ってから「そんな仕事は無い」と気づく形にしない。"""
        with pytest.raises(SystemExit):
            rd.enqueue(["tune:macro_risk_return"], db=fake_db)
        assert rd.read_queue(db=fake_db) == [], "弾いたのに積まれている"

    def test_job_that_cannot_fit_the_window_is_rejected(self, fake_db, monkeypatch):
        """窓に入らない仕事は積ませない（打ち切られると何も残らない）。"""
        big = rd.Job(name="huge", argv=("{python}", "-c", "pass"), why="test",
                     measured_min=rd.JOB_BUDGET_MIN + 1)
        monkeypatch.setitem(rd.JOBS, "huge", big)
        with pytest.raises(SystemExit):
            rd.enqueue(["huge"], db=fake_db)

    def test_corrupt_queue_value_reads_as_empty(self, fake_db):
        """壊れた値で**バッチが起動不能にならない**こと（例外にしない）。"""
        fake_db.store[rd.KEY_QUEUE] = "{ not json"
        assert rd.read_queue(db=fake_db) == []
        fake_db.store[rd.KEY_QUEUE] = json.dumps({"a": 1})
        assert rd.read_queue(db=fake_db) == []

    def test_m1_search_is_not_offerable(self):
        """M-1 探索（752分）は日中枠に存在しない＝専用タスクのまま（ADR-0046）。"""
        assert "tune:macro_risk_return" not in rd.JOBS
        assert "m1" not in rd.JOBS


class TestSteps:
    def test_empty_queue_produces_no_steps(self):
        assert rd.steps_for("py", None) == ()

    def test_beta_gets_the_dependency_smoke_first(self):
        names = [s.name for s in rd.steps_for("py", "beta")]
        assert names == ["deps_smoke", "macro_beta"]

    def test_search_jobs_skip_the_smoke_step(self):
        """探索は pymc を使わないので、20秒の import 確認を毎回払わない。"""
        assert [s.name for s in rd.steps_for("py", "tune:macro_gbdt")] == ["tune:macro_gbdt"]

    def test_a_queued_job_that_lost_its_definition_fails_loudly(self):
        """定義が消えた仕事は**黙って何もしない**のではなく失敗にする。"""
        steps = rd.steps_for("py", "gone")
        assert len(steps) == 1 and steps[0].name == "unknown:gone"

    def test_python_placeholder_is_substituted(self):
        argv = rd.steps_for("/tmp/py.exe", "tune:macro_gbdt")[0].argv
        assert argv[0] == "/tmp/py.exe"
        assert "{python}" not in argv


class TestArgumentsMatchTheExistingBatches:
    """**同じ名前の別物を測らない。** 片方の引数だけ動かすと比較が成立しなくなる。"""

    def _argv_of(self, steps, name):
        return next(s.argv for s in steps if s.name == name)

    def test_beta_argv_matches_run_monthly_beta(self):
        here = self._argv_of(rd.steps_for("py", "beta"), "macro_beta")
        there = self._argv_of(rmb.steps_for("py"), "macro_beta")
        assert here == there

    def test_beta_does_not_pass_force(self):
        """`--force` はゲートを迂回する。日中枠は通常判定でなければ検証にならない。"""
        assert "--force" not in self._argv_of(rd.steps_for("py", "beta"), "macro_beta")

    @pytest.mark.parametrize("key", ["tune:macro_gbdt", "tune:macro_dlm"])
    def test_tune_argv_matches_run_monthly(self, key):
        here = self._argv_of(rd.steps_for("py", key), key)
        there = self._argv_of(rm.steps_for("py"), key)
        assert here == there


class TestBudgetFitsTheWindow:
    INSTALLER = ROOT / "scripts" / "install_daytime_task.ps1"

    @pytest.mark.parametrize("key", sorted(rd.JOBS))
    def test_every_step_has_a_budget(self, key):
        missing = [s.name for s in rd.steps_for(sys.executable, key) if s.budget_min is None]
        assert not missing, f"予算の無いステップ: {missing}"

    @pytest.mark.parametrize("key", sorted(rd.JOBS))
    def test_budget_fits_the_window(self, key):
        problem = bc.window_problem(rd.steps_for(sys.executable, key), rd.WINDOW_MIN)
        assert problem is None, problem

    def test_window_matches_the_installer_default(self):
        """`-Hours` と `WINDOW_MIN` はセットでしか意味を持たない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r"\[int\]\$Hours\s*=\s*(\d+)", text)
        assert m, "install_daytime_task.ps1 から既定の -Hours を読めない（書式が変わった）"
        assert int(m.group(1)) * 60 == rd.WINDOW_MIN

    def test_every_offered_job_actually_fits(self):
        """`JOBS` に置いた時点で窓に入ることを縛る（積むときの検査より前に落とす）。"""
        too_big = {k: j.measured_min for k, j in rd.JOBS.items()
                   if j.measured_min > rd.JOB_BUDGET_MIN}
        assert not too_big, f"窓に入らない仕事が JOBS にある: {too_big}"

    def test_window_leaves_room_before_the_nightly_batch(self):
        """夜間バッチ（17:20）との間隔。窓を広げてメモリを取り合わせない。"""
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        m = re.search(r'\[string\]\$Time\s*=\s*"(\d+):(\d+)"', text)
        assert m, "installer から既定の -Time を読めない"
        start_min = int(m.group(1)) * 60 + int(m.group(2))
        assert start_min + rd.WINDOW_MIN <= 17 * 60 + 20, (
            "日中枠の窓が夜間バッチ（17:20）へ食い込む＝メモリを取り合う")


class TestWatchedByTheFreshnessCheck:
    def test_registered_in_the_watch_table(self):
        """監視表に無いと「走らなかったのに誰も気づかない」（#515 の穴そのもの）。"""
        keys = {w.key_run for w in batch_freshness.WATCHED}
        assert rd.KEY_LAST_RUN in keys

    def test_footprint_keys_do_not_collide(self):
        """足跡キーが他バッチと衝突すると、片方の実行がもう片方の鮮度を偽装する。"""
        keys = {rd.KEY_LAST_RUN, rd.KEY_LAST_SUCCESS}
        for mod in (rm, rmb, rm1):
            assert not keys & {mod.KEY_LAST_RUN, mod.KEY_LAST_SUCCESS}

    def test_cadence_spans_the_weekend(self):
        """平日トリガなので金→月の 72時間が正常な最長間隔。

        24 にすると毎週土曜に「走っていない」と鳴り、鳴りっぱなしの警告は読まれなくなる。
        """
        w = next(w for w in batch_freshness.WATCHED if w.key_run == rd.KEY_LAST_RUN)
        assert w.cadence_h >= 72.0
        assert w.window_min == rd.WINDOW_MIN


class TestTaskInstaller:
    INSTALLER = ROOT / "scripts" / "install_daytime_task.ps1"
    LAUNCHER = ROOT / "run_daytime.ps1"

    def test_installer_and_launcher_exist(self):
        """**起動手順を人の記憶に置かない**——PC を入れ替えた時点で黙って消える。"""
        assert self.INSTALLER.is_file()
        assert self.LAUNCHER.is_file()

    def test_ps1_files_have_a_bom(self):
        """BOM が無いと PowerShell が cp932 として読み、日本語が化ける。"""
        for path in (self.INSTALLER, self.LAUNCHER):
            assert path.read_bytes()[:3] == b"\xef\xbb\xbf", f"{path.name}: BOM が無い"

    def test_trigger_covers_weekdays_only(self):
        text = self.INSTALLER.read_text(encoding="utf-8-sig")
        assert "-Weekly" in text
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            assert day in text, f"{day} がトリガに無い"
        assert "Saturday" not in text and "Sunday" not in text, "休日にも起動する形になっている"

    def test_runs_in_session_zero(self):
        """S4U でないと対話コンソールの終了に巻き込まれて即死する（#515）。"""
        assert "S4U" in self.INSTALLER.read_text(encoding="utf-8-sig")
