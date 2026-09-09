"""平日日中バッチ `scripts/run_daytime.py` の不変条件（Issue #618）。

2026-09-07 に `macro_beta` を手動で回したところ、**同一パネル・同一設定・同一コードなのに
発散が 0 → 344 回に増え**、収束ゲートに落ちて隔離された。9/6 の run との差は、その7時間の
裏で重いテストを並走させたことしか見当たらない。本番の推論経路にはスレッド固定が無く、
XLA が使うコア数は実行時の混み具合で変わる。つまり**「重い計算の裏で作業をしない」という
運用条件が結果の再現性に直結している**。人が PC を触らない平日日中を専用の枠にした。

守るのは7点:

1. **キューは先頭を取り除いてから返す**（失敗しても戻さない＝同じ計算を繰り返さない）
2. **窓に入らない仕事は積ませない**（走ってから打ち切られると何も残らない）
3. **予算が窓に収まり、窓は installer の既定と一致する**
4. **監視表に載っている**（走らなかったことに気づけるのはここだけ）
5. **重い計算の引数が既存バッチと同一**（片方だけ動かすと同じ名前の別物を測る）
6. **並走で結果が変わる仕事に印が付いている**（`-Now` の手動キックはここで分岐する）
7. **手動キックはタスク経由で走る**（直に走らせると端末を閉じた瞬間に死ぬ・#515 と同型）
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

    def close(self):
        """`_session()` 経由（db 引数なし）の呼び出しが閉じにくるので受ける。"""


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
                     measured_min=rd.JOB_BUDGET_MIN + 1, parallel_sensitive=True)
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


class TestParallelSensitivity:
    """**並走で結果が変わる仕事と、遅くなるだけの仕事を混ぜない**（#618）。

    `-Now` の手動キックは人が PC を触っている時間帯に叩かれる前提なので、この区別が
    そのまま「確認を挟むか素通しか」の分岐になる。
    """

    def test_the_flag_has_no_default(self):
        """既定値があると、新しい仕事を足したとき黙って非敏感側へ倒れる。

        忘れたことが失敗として現れないので、**必須フィールドにして TypeError で落とす**
        （CLAUDE.md「増やしたら登録表へ1行足す」と同じ狙い）。
        """
        import dataclasses

        field = next(f for f in dataclasses.fields(rd.Job) if f.name == "parallel_sensitive")
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING

    @pytest.mark.parametrize("key", ["beta", "tune:macro_gbdt", "tune:macro_dlm",
                                     "gate:interactions"])
    def test_computations_are_sensitive(self, key):
        """MCMC も探索も昇格ゲートも、数値の揺れが**採否や重みそのもの**を変える。"""
        assert rd.JOBS[key].parallel_sensitive is True

    @pytest.mark.parametrize("key", ["interim", "disclosures"])
    def test_collection_is_not_sensitive(self, key):
        """収集は外部 API の応答待ちが所要の大半で、取れる中身は裏で何が動いても同じ。"""
        assert rd.JOBS[key].parallel_sensitive is False


class TestPeek:
    """`-Now` が判断に使う機械可読口。**キューを減らさない**ことが不変条件。"""

    @pytest.fixture
    def db(self, fake_db, monkeypatch):
        monkeypatch.setattr(rd, "_session", lambda: fake_db)
        return fake_db

    def _peek(self, capsys):
        assert rd.main(["--peek"]) == 0
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if ln.lstrip().startswith("{"))
        return json.loads(line)

    def test_empty_queue_reports_no_key(self, db, capsys):
        got = self._peek(capsys)
        assert got["key"] is None and got["remaining"] == 0
        assert got["sensitive"] is False, "空を敏感扱いすると空振りのたびに止まる"

    def test_reports_the_head_without_consuming_it(self, db, capsys):
        rd.enqueue(["interim", "beta"], db=db)
        got = self._peek(capsys)
        assert got["key"] == "interim"
        assert got["sensitive"] is False and got["known"] is True
        assert got["remaining"] == 2
        assert rd.read_queue(db=db) == ["interim", "beta"], "peek がキューを減らしている"

    def test_sensitive_head_is_reported_as_such(self, db, capsys):
        rd.enqueue(["beta"], db=db)
        assert self._peek(capsys)["sensitive"] is True

    def test_unknown_job_falls_to_the_sensitive_side(self, db, capsys):
        """判断材料が無いときに**黙って走らせない**（積んだ後で定義が消えた場合）。"""
        rd.write_queue(["vanished"], db=db)
        got = self._peek(capsys)
        assert got["known"] is False and got["sensitive"] is True

    def test_output_is_ascii(self, db, capsys):
        """cp932 へリダイレクトされても落ちない（このモジュールの出力規約）。"""
        rd.enqueue(["beta"], db=db)
        assert rd.main(["--peek"]) == 0
        capsys.readouterr().out.encode("ascii")


class TestManualKick:
    """`-Now` は**タスクを叩く**（自分で走らない）。ここが崩れると長時間ジョブが端末と心中する。"""

    LAUNCHER = ROOT / "run_daytime.ps1"
    INSTALLER = ROOT / "scripts" / "install_daytime_task.ps1"

    @pytest.fixture(scope="class")
    def text(self):
        return self.LAUNCHER.read_text(encoding="utf-8-sig")

    def test_now_and_force_are_parameters(self, text):
        assert re.search(r"\[switch\]\$Now", text)
        assert re.search(r"\[switch\]\$Force", text)

    def test_now_starts_the_scheduled_task(self, text):
        """対話ターミナルで直に走らせると、画面を閉じた瞬間に死ぬ（#515 と同型）。"""
        assert "Start-ScheduledTask" in text

    def test_now_verifies_the_task_actually_started(self, text):
        """Start-ScheduledTask は起動しなくても例外を投げない＝確認しないと嘘をつく。"""
        assert 'State -ne "Running"' in text

    def test_now_refuses_to_stack_on_a_running_task(self, text):
        """走っている最中に叩くと重い計算が2本並ぶ（IgnoreNew は理由を返さない）。"""
        assert 'State -eq "Running"' in text

    def test_now_consults_peek_and_gates_on_sensitivity(self, text):
        assert "--peek" in text
        assert re.search(r"\$peek\.sensitive\s+-and\s+-not\s+\$Force", text), (
            "敏感な仕事を -Force 無しで素通しする形になっている")

    def test_task_name_default_matches_the_installer(self, text):
        """既定がずれると -Now が『登録されていないタスク』を叩き続ける。"""
        here = re.search(r'\[string\]\$TaskName\s*=\s*"([^"]+)"', text)
        there = re.search(r'\[string\]\$TaskName\s*=\s*"([^"]+)"',
                          self.INSTALLER.read_text(encoding="utf-8-sig"))
        assert here and there, "TaskName の既定を読めない（書式が変わった）"
        assert here.group(1) == there.group(1)


class TestInflightReclaim:
    """中断（結論を出す前にプロセスごと消えた）だけをキューへ戻す（#639）。

    2026-09-09 に Windows Update の再起動が `tune:macro_dlm` を 285分（255/294件）で殺し、
    285分の計算とキューの1件が同時に消えた。`daytime_last_run` は閾値の内側だったので
    watchdog も起票せず、**失敗としてはどこにも現れなかった**。
    """

    @pytest.fixture
    def db(self, fake_db, monkeypatch):
        monkeypatch.setattr(rd, "_session", lambda: fake_db)
        return fake_db

    @staticmethod
    def _mark(db, job, state, requeued=0):
        db.store[rd.KEY_INFLIGHT] = json.dumps(
            {"job": job, "state": state, "requeued": requeued, "at": "2026-09-09T08:00:04+00:00"})

    def test_no_marker_reclaims_nothing(self, db):
        rd.write_queue(["beta"], db=db)
        assert rd.reclaim_inflight(db=db) == []
        assert rd.read_queue(db=db) == ["beta"]

    def test_interrupted_job_goes_back_to_the_head(self, db):
        """先頭へ戻す。末尾だと、消えた仕事が数日後まで進まない。"""
        rd.write_queue(["beta", "interim"], db=db)
        self._mark(db, "tune:macro_dlm", rd._STATE_RUNNING, requeued=0)

        lines = rd.reclaim_inflight(db=db)

        assert rd.read_queue(db=db) == ["tune:macro_dlm", "beta", "interim"]
        assert any("tune:macro_dlm" in ln for ln in lines)

    def test_requeue_count_is_carried_so_the_cap_can_be_counted(self, db):
        """引き継がないと毎回 0 から数え直して無限に戻り続ける。"""
        self._mark(db, "beta", rd._STATE_RUNNING, requeued=0)
        rd.reclaim_inflight(db=db)

        mark = rd.read_inflight(db=db)
        assert mark["state"] == rd._STATE_QUEUED
        assert mark["requeued"] == 1
        assert rd.carried_requeue("beta", db=db) == 1
        assert rd.carried_requeue("interim", db=db) == 0, "無関係な仕事に回数が漏れている"

    def test_a_queued_marker_is_left_alone(self, db):
        """戻したがまだ pop されていないだけ。触ると毎回キューの先頭へ積み直してしまう。"""
        rd.write_queue(["beta"], db=db)
        self._mark(db, "beta", rd._STATE_QUEUED, requeued=1)

        assert rd.reclaim_inflight(db=db) == []
        assert rd.read_queue(db=db) == ["beta"]

    def test_second_interruption_is_dropped_and_filed(self, db):
        """2回続けて消えるのは環境側の問題。戻し続けると先へ進まない。"""
        calls = []

        def _run(argv, **kw):
            calls.append(argv)
            return type("P", (), {"returncode": 0, "stderr": ""})()

        rd.write_queue(["interim"], db=db)
        self._mark(db, "beta", rd._STATE_RUNNING, requeued=rd.MAX_REQUEUE)

        lines = rd.reclaim_inflight(db=db, run=_run)

        assert rd.read_queue(db=db) == ["interim"], "上限を超えても戻している"
        assert rd.read_inflight(db=db) is None
        assert calls and calls[0][:3] == ["gh", "issue", "create"]
        assert any("戻さず捨てる" in ln for ln in lines)

    def test_filing_failure_does_not_raise(self, db):
        """gh が無くてもバッチは走り続ける（`bc.notify` と同じ方針）。"""
        def _run(argv, **kw):
            raise OSError("gh not found")

        self._mark(db, "beta", rd._STATE_RUNNING, requeued=rd.MAX_REQUEUE)
        lines = rd.reclaim_inflight(db=db, run=_run)
        assert any("通知できなかった" in ln for ln in lines)

    def test_corrupt_marker_reads_as_absent(self, db):
        """例外にすると、値が1つ壊れただけでバッチが起動不能になる。"""
        db.store[rd.KEY_INFLIGHT] = "{壊れている"
        assert rd.read_inflight(db=db) is None
        assert rd.reclaim_inflight(db=db) == []

    def test_marker_for_a_job_that_lost_its_definition_is_dropped(self, db):
        rd.write_queue(["beta"], db=db)
        self._mark(db, "gone", rd._STATE_RUNNING, requeued=0)

        lines = rd.reclaim_inflight(db=db)

        assert rd.read_queue(db=db) == ["beta"]
        assert rd.read_inflight(db=db) is None
        assert any("JOBS に無い" in ln for ln in lines)

    def test_requeue_does_not_duplicate_an_already_queued_job(self, db):
        rd.write_queue(["beta", "interim"], db=db)
        self._mark(db, "beta", rd._STATE_RUNNING, requeued=0)

        rd.reclaim_inflight(db=db)

        assert rd.read_queue(db=db) == ["beta", "interim"]

    @pytest.mark.parametrize("job,requeued,gh", [
        ("beta", 0, "ok"),                     # 戻す
        ("beta", rd.MAX_REQUEUE, "ok"),        # 上限で捨てる
        ("beta", rd.MAX_REQUEUE, "missing"),   # 捨てるが gh が無い
        ("gone", 0, "ok"),                     # 定義が消えた
    ])
    def test_every_message_survives_cp932(self, db, job, requeued, gh):
        """回収の出力は cp932 で書ける文字だけで組む（このモジュールの出力規約）。

        `bc.Runner.write` は**ログへ書く前に print を呼び**、その例外は try の外にある。
        em dash 1文字で `UnicodeEncodeError` が漏れ、**回収そのものが走らなくなる**——
        「走らなかったことを検知する」ための仕組みが、それ自体を起こす形になる。
        """
        def _run(argv, **kw):
            if gh == "missing":
                raise OSError("gh not found")
            return type("P", (), {"returncode": 0, "stderr": ""})()

        self._mark(db, job, rd._STATE_RUNNING, requeued)
        for line in rd.reclaim_inflight(db=db, run=_run):
            line.encode("cp932")


class TestInflightLifecycleInMain:
    """マーカーは Python が生きていれば必ず消える。**残ること自体が中断の証拠**（#639）。"""

    @pytest.fixture
    def db(self, fake_db, monkeypatch, tmp_path):
        monkeypatch.setattr(rd, "_session", lambda: fake_db)
        monkeypatch.setattr(rd, "log_path", lambda *a, **k: tmp_path / "daytime.log")
        monkeypatch.setattr(rd, "record_footprint", lambda results: None)
        return fake_db

    def test_marker_is_written_while_the_job_runs(self, db, monkeypatch):
        seen = {}

        def _run_batch(spec, steps, hooks, argv):
            seen["mark"] = rd.read_inflight(db=db)
            return 0

        monkeypatch.setattr(rd.bc, "run_batch", _run_batch)
        rd.write_queue(["interim"], db=db)
        rd.main([])

        assert seen["mark"]["job"] == "interim"
        assert seen["mark"]["state"] == rd._STATE_RUNNING

    def test_marker_is_cleared_after_a_failing_run(self, db, monkeypatch):
        """結論を出した失敗は戻さない（`pop_queue` の設計判断をそのまま残す）。"""
        monkeypatch.setattr(rd.bc, "run_batch", lambda *a, **k: 1)
        rd.write_queue(["interim"], db=db)

        assert rd.main([]) == 1
        assert rd.read_inflight(db=db) is None
        assert rd.read_queue(db=db) == []

    def test_marker_is_cleared_when_the_run_raises(self, db, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(rd.bc, "run_batch", _boom)
        rd.write_queue(["interim"], db=db)

        with pytest.raises(RuntimeError):
            rd.main([])
        assert rd.read_inflight(db=db) is None

    def test_dry_run_touches_neither_queue_nor_marker(self, db, monkeypatch):
        monkeypatch.setattr(rd.bc, "run_batch", lambda *a, **k: 0)
        rd.write_queue(["interim"], db=db)

        rd.main(["--dry-run"])

        assert rd.read_queue(db=db) == ["interim"], "ドライランがキューを減らした"
        assert rd.read_inflight(db=db) is None

    def test_a_reclaimed_job_becomes_todays_run(self, db, monkeypatch):
        """回収はキューを読む前。戻した仕事がその場で今日の1件になる。"""
        seen = {}

        def _run_batch(spec, steps, hooks, argv):
            seen["steps"] = [s.name for s in steps]
            return 0

        monkeypatch.setattr(rd.bc, "run_batch", _run_batch)
        rd.write_queue(["interim"], db=db)
        db.store[rd.KEY_INFLIGHT] = json.dumps(
            {"job": "tune:macro_dlm", "state": rd._STATE_RUNNING, "requeued": 0, "at": "x"})

        rd.main([])

        assert "tune:macro_dlm" in seen["steps"]
        assert rd.read_queue(db=db) == ["interim"]
        assert rd.read_inflight(db=db) is None

    @pytest.mark.parametrize("state", [rd._STATE_RUNNING, rd._STATE_QUEUED])
    def test_queue_listing_with_a_marker_survives_cp932(self, db, capsys, state):
        """`--queue` は毎セッション叩く。マーカーの行で落ちるとキューが読めなくなる。"""
        rd.write_queue(["interim"], db=db)
        db.store[rd.KEY_INFLIGHT] = json.dumps(
            {"job": "interim", "state": state, "requeued": 1, "at": "x"})

        assert rd.main(["--queue"]) == 0
        capsys.readouterr().out.encode("cp932")

    def test_clear_queue_also_clears_the_marker(self, db, capsys):
        """残すと、消したはずの仕事を次の実走が黙って積み直す。"""
        rd.write_queue(["interim"], db=db)
        db.store[rd.KEY_INFLIGHT] = json.dumps(
            {"job": "interim", "state": rd._STATE_QUEUED, "requeued": 1, "at": "x"})

        rd.main(["--clear-queue"])

        assert rd.read_queue(db=db) == []
        assert rd.read_inflight(db=db) is None
