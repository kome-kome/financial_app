"""平日日中バッチ `scripts/run_daytime.py` の不変条件（Issue #618）。

2026-09-07 に `macro_beta` を手動で回したところ、**同一パネル・同一設定・同一コードなのに
発散が 0 → 344 回に増え**、収束ゲートに落ちて隔離された。9/6 の run との差は、その7時間の
裏で重いテストを並走させたことしか見当たらない。本番の推論経路にはスレッド固定が無く、
XLA が使うコア数は実行時の混み具合で変わる。つまり**「重い計算の裏で作業をしない」という
運用条件が結果の再現性に直結している**。人が PC を触らない平日日中を専用の枠にした。

守るのは11点:

1. **キューは先頭を取り除いてから返す**（失敗しても戻さない＝同じ計算を繰り返さない）
2. **窓に入らない仕事は積ませない**（走ってから打ち切られると何も残らない）
3. **予算が窓に収まり、窓は installer の既定と一致する**
4. **監視表に載っている**（走らなかったことに気づけるのはここだけ）
5. **重い計算の引数が既存バッチと同一**（片方だけ動かすと同じ名前の別物を測る）
6. **並走で結果が変わる仕事に印が付いている**（`-Now` の手動キックはここで分岐する）
7. **手動キックはタスク経由で走る**（直に走らせると端末を閉じた瞬間に死ぬ・#515 と同型）
8. **仕事は手で作ったキャッシュに依存しない**（待っている間に退避されると即死する・#674）
9. **日付で決まる仕事は暦が積む**（積むのが人だと、積み忘れが失敗として現れない・#681）
10. **月次系のバッチと時間が重なる日は、並走に敏感な仕事を取り出さない**（#681）
11. **祝日・年末年始も取り出さない。`-Now -Force` だけが今日の祝日の見送りを外す**（#684）
"""
import json
import os
import re
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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

# 暦と月次の重なりは「今日」で結果が変わる。**テストの結果を実行日に依存させない**ため、
# 既定では暦を空にし、月次と重ならず暦の期限も来ない日へ固定する。暦のテストは本物を戻す。
REAL_SCHEDULE = rd.SCHEDULE
QUIET_DAY = date(2026, 9, 10)      # 木曜・10日（月次系は 1〜3日、暦は 1日と16日）


@pytest.fixture(autouse=True)
def _quiet_calendar(monkeypatch):
    monkeypatch.setattr(rd, "_today", lambda: QUIET_DAY)
    monkeypatch.setattr(rd, "SCHEDULE", ())


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


class TestRhatScaleSamplesLikeProduction:
    """規模依存の格子（#664）は**本番の `beta` と同じサンプリング設定**で回す。

    測りたいのは本番の収束ゲートの余裕が規模で縮むか。tune・draws・軌道長・target_accept の
    どれかが違えば `r_hat` の分布そのものが変わり、本番のゲートの話ではなくなる——しかも
    例外は出ず、もっともらしい p99 が並ぶ。旗の名前はドライバ側で違うので値で突き合わせる。
    """

    # (beta の旗, 格子の旗)
    PAIRS = (("--draws", "--draws"), ("--tune", "--tune"), ("--chains", "--chains"),
             ("--target-accept", "--target-accepts"), ("--max-tree-depth", "--depths"),
             ("--nuts-sampler", "--nuts-sampler"), ("--init", "--init"))

    def _value(self, argv, flag):
        return argv[argv.index(flag) + 1]

    @pytest.mark.parametrize("beta_flag,grid_flag", PAIRS)
    def test_sampling_config_matches_beta(self, beta_flag, grid_flag):
        beta = rd.JOBS["beta"].argv
        grid = rd.JOBS["bench:rhat-scale"].argv
        assert self._value(grid, grid_flag) == self._value(beta, beta_flag)

    def test_grid_takes_single_values_where_beta_does(self):
        """格子の旗は複数値を取れる。本番と揃えるなら1値だけ（2値目があれば別の条件を足している）。"""
        grid = list(rd.JOBS["bench:rhat-scale"].argv)
        for _, flag in self.PAIRS:
            nxt = grid[grid.index(flag) + 2]
            assert nxt.startswith("--"), f"{flag} に2つ目の値がある: {nxt}"

    def test_measures_only_the_sampler_seed(self):
        """反復は chain の乱数だけを振る（パネル固定）＝本番の run 間差と同じ種類の揺れ。"""
        argv = rd.JOBS["bench:rhat-scale"].argv
        assert "--panel-seed" in argv
        assert "--resume" in argv          # 積み直しで続きから回る前提
        assert argv[argv.index("--mode") + 1] == "synth"

    def test_scale_job_gets_the_dependency_smoke_first(self):
        names = [s.name for s in rd.steps_for("py", "bench:rhat-scale")]
        assert names == ["deps_smoke", "bench_rhat_scale"]


class TestJobsBuildTheirOwnInputs:
    """**積んだ時点のキャッシュを当てにしない**（#674）。

    2026-09-07 に `gate:interactions` を積んだ時点では `scripts/.cache/weekly_prices_close.pkl`
    があったが、順番を待つ間の 9/8 に #620 の株価修復で `_stale_pre620/` へ退避された。
    9/14 に順番が来たジョブは `--allow-full-pull` を持たず、`candidate_bakeoff._load_prices` が
    読み込みを拒否して 0.1分で exit=1——平日1日ぶんの枠が消え、「結論を出した失敗」なので
    キューにも戻らなかった。

    `--allow-full-pull` だけでも足りない。キャッシュは**世代の印を持たず古い世代を黙って返す**
    ので、財務（8/31）とマクロ（9/3）は #655 の分割補正より前のまま、株価だけ新しい世代という
    混ざったパネルを測ることになる。だから `--refresh-cache` も要る。

    判定はスクリプトのソースから取る＝同じ系統のスクリプトを呼ぶ仕事を後から足しても
    自動で対象になる（書き忘れは失敗として現れない）。
    """

    FLAGS = ("--allow-full-pull", "--refresh-cache")

    @staticmethod
    def _script_of(argv):
        """`-m scripts.X` なら `scripts/X.py` を返す（それ以外の起動形は対象外）。"""
        if "-m" not in argv:
            return None
        module = argv[argv.index("-m") + 1]
        path = ROOT.joinpath(*module.split(".")).with_suffix(".py")
        return path if path.is_file() else None

    @pytest.mark.parametrize("key", sorted(rd.JOBS))
    def test_cache_flags_the_script_accepts_are_passed(self, key):
        argv = rd.JOBS[key].argv
        script = self._script_of(argv)
        if script is None:
            pytest.skip("scripts/ 配下のモジュール起動ではない")
        source = script.read_text(encoding="utf-8")
        missing = [f for f in self.FLAGS if f'"{f}"' in source and f not in argv]
        assert not missing, (
            f"{key}: {script.name} は {missing} を受け付けるのに argv に無い。"
            "積んだ時点のキャッシュは待っている間に退避されうる（即死する）し、"
            "世代の印が無いので古い世代を黙って返す（#674）")

    def test_the_rule_actually_covers_the_gate(self):
        """照合が空振りしていないこと（対象0件でも上のテストは全部通ってしまう）。"""
        script = self._script_of(rd.JOBS["gate:interactions"].argv)
        assert script is not None and script.name == "momentum_gate.py"
        source = script.read_text(encoding="utf-8")
        assert all(f'"{f}"' in source for f in self.FLAGS)


class TestMaxFeaturesGateComparesAgainstProduction:
    """**列数ジョブの値に本番の既定を含める**（#615）。

    列数モードの分母は「本番値」（`params_schema()` の `max_features` 既定）で、条件集合に
    無いと `base_of` は最小値へ黙って倒れる。本番の既定を 20 から動かしたのにジョブの値を
    直し忘れると、**本番との比較のつもりで `mf5` との比較を測る**——例外は出ず、数値も
    もっともらしい。
    """

    def test_production_default_is_among_the_values(self):
        from plugins import get_plugin
        from plugins.utils import coerce_params

        argv = rd.JOBS["gate:max-features"].argv
        values = [int(v) for v in argv[argv.index("--max-features") + 1].split(",")]
        prod = coerce_params(get_plugin("macro_risk_return").params_schema(), {})["max_features"]
        assert prod in values, f"本番の既定 {prod} がジョブの値 {values} に無い"

    def test_the_job_measures_at_full_resolution(self):
        """`--smoke` の共通域は間引きで壊れるので読まない（ADR-0050 Decision 3）。"""
        argv = rd.JOBS["gate:max-features"].argv
        assert "--smoke" not in argv
        assert argv[argv.index("--stride") + 1] == "1"


class TestDemeanGateMeasuresTheTargetAxis:
    """目的変数のジョブ（#615）は目的変数モードで、間引かずに測る。

    フラグを落とすと既定モード（モメンタムの昇格ゲート・M-2/M-6）が走り、exit 0 のまま
    別の軸の結果が `momentum_gate.json` に残る。
    """

    def test_the_job_runs_the_target_mode(self):
        assert "--demean-target" in rd.JOBS["gate:demean"].argv

    def test_the_job_measures_at_full_resolution(self):
        """`--smoke` の共通域は間引きで壊れるので読まない（ADR-0050 Decision 3）。"""
        argv = rd.JOBS["gate:demean"].argv
        assert "--smoke" not in argv
        assert argv[argv.index("--stride") + 1] == "1"


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
                                     "gate:interactions", "gate:max-features", "gate:ttm",
                                     "gate:demean", "bench:rhat-scale"])
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

    def test_now_stops_on_a_blocked_day_before_starting(self, text):
        """月次と重なる日は、キューに仕事があっても起動しない（-Force でも同じ・#681）。

        `key` が空なので、見ないと「キューが空です」と誤った理由を出してしまう。
        """
        blocked = text.find("$peek.blocked")
        assert blocked != -1, "-Now が --peek の blocked を見ていない"
        assert blocked < text.find("-not $peek.key"), "空の判定より後ろで見ている"
        assert blocked < text.find("Start-ScheduledTask -TaskName"), "起動の後ろで見ている"

    def test_force_lifts_only_the_holiday_before_blocked_is_read(self, text):
        """祝日の見送りは -Force で今日だけ外す（#684）。月次の重なりは外さない。

        解除印を書くのが blocked の判定より後ろだと、祝日に -Force を付けても止まる。
        """
        lift = text.find("--allow-holiday")
        assert lift != -1, "-Now -Force が祝日の解除印を書いていない"
        assert re.search(r"\$peek\.holiday_skip\s+-and\s+\$Force", text), (
            "解除印を -Force 無しでも書く形になっている")
        assert lift < text.find("$peek.blocked"), "blocked を見た後で解除している"
        assert lift < text.find("Start-ScheduledTask -TaskName"), "起動の後ろで解除している"
        assert 'blocked_by -eq "holiday"' in text, "祝日と月次を同じ文言で止めている"

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


# ── 暦（#681・ADR-0056）──────────────────────────────────────────────────────
# H1（半期）と会社予想の収集は、人が積んだときにしか走らなかった。積み忘れは失敗として
# 現れず、次の提出の波（3月期の H1・提出期限 11/14）を逃しても誰も気づけない。


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _const(value):
    """`Scheduled.produced` の代役。例外を渡すとそれを送出する。"""
    def read(_db):
        if isinstance(value, Exception):
            raise value
        return value
    return read


def _with_produced(**last):
    """本物の暦の読み手だけを差し替えたもの（日・順序は本物のまま）。"""
    return tuple(replace(s, produced=_const(last.get(s.job))) for s in REAL_SCHEDULE)


@pytest.fixture
def real_schedule(monkeypatch):
    monkeypatch.setattr(rd, "SCHEDULE", REAL_SCHEDULE)
    return REAL_SCHEDULE


class TestScheduleDefinition:
    def test_scheduled_jobs_are_offerable(self):
        for s in REAL_SCHEDULE:
            assert s.job in rd.JOBS, f"暦の {s.job} が JOBS に無い"
            assert rd.JOBS[s.job].measured_min <= rd.JOB_BUDGET_MIN, s.job

    def test_scheduled_jobs_are_not_parallel_sensitive(self):
        """暦は先頭へ割り込ませる。重い計算を割り込ませると、並走の条件を暦が作る。

        会社予想を月次と重なる1日に置いた根拠（その日でも走れる）もこれに依存する。
        """
        for s in REAL_SCHEDULE:
            assert rd.JOBS[s.job].parallel_sensitive is False, s.job

    def test_days_exist_in_every_month(self):
        for s in REAL_SCHEDULE:
            assert 1 <= s.day <= 28, s.job

    def test_jobs_are_unique(self):
        jobs = [s.job for s in REAL_SCHEDULE]
        assert len(jobs) == len(set(jobs))

    def test_interim_comes_after_the_filing_deadlines(self):
        """半期報告書の期限は期末+45日。月末期末の12通りのうち11通りが16日より前に来る。

        残る1通り（1月末期末 -> 3/17）は翌月の実走で取る。
        """
        s = next(s for s in REAL_SCHEDULE if s.job == "interim")
        month_ends = [date(2026, m + 1, 1) - timedelta(days=1) for m in range(1, 12)]
        month_ends.append(date(2026, 12, 31))
        before = [e for e in month_ends if (e + timedelta(days=45)).day < s.day]
        assert len(before) == 11


class TestProducedReaders:
    """暦の「今月ぶんは入ったか」と watchdog の「前進したか」が共有する読み手。"""

    def test_h1_reads_only_h1_rows(self, db, make_fin):
        import collector_interim

        db.add(make_fin(edinet_code="E00001", year=2026, period_end="2026-03-31",
                        created_at=datetime(2026, 9, 20)))
        db.add(make_fin(edinet_code="E00001", year=2026, period_end="2025-09-30",
                        period_type=collector_interim.INTERIM_PERIOD_TYPE,
                        created_at=datetime(2026, 9, 16, 0, 4)))
        db.commit()
        assert rd.h1_created_at(db) == _utc(2026, 9, 16, 0, 4)

    def test_h1_ignores_updates_to_existing_rows(self, db, make_fin):
        """株価の補完などで `updated_at` が進んでも、収集が前進したことにはならない。"""
        db.add(make_fin(edinet_code="E00001", year=2026, period_end="2025-09-30",
                        period_type="H1", created_at=datetime(2026, 8, 1),
                        updated_at=datetime(2026, 9, 20)))
        db.commit()
        assert rd.h1_created_at(db) == _utc(2026, 8, 1)

    def test_empty_tables_read_as_none(self, db):
        assert rd.h1_created_at(db) is None
        assert rd.disclosure_created_at(db) is None

    def test_disclosure_created_at_survives_a_re_fetch(self, db):
        """同じ日を取り直しても進まない（upsert が `created_at` を上書きしない）。"""
        from database import upsert_statement_disclosures

        row = {"disc_no": "1", "edinet_code": "E00001", "disc_date": "2026-06-16"}
        upsert_statement_disclosures(db, [row])
        db.commit()
        first = rd.disclosure_created_at(db)
        upsert_statement_disclosures(db, [{**row, "sales": 1.0}])
        db.commit()
        assert first is not None
        assert rd.disclosure_created_at(db) == first


class TestPlanSchedule:
    @staticmethod
    def _plan(today, queue=(), marks=None, **last):
        calls = []

        def produced_at(s):
            calls.append(s.job)
            return _const(last.get(s.job))(None)

        items, new_marks, notes = rd.plan_schedule(list(queue), marks or {}, today, produced_at)
        return items, new_marks, notes, calls

    def test_not_due_before_the_day(self, real_schedule):
        items, marks, notes, _ = self._plan(date(2026, 10, 15), ["beta"], {"disclosures": "2026-10"})
        assert items == ["beta"]
        assert "interim" not in marks
        assert notes == []

    def test_due_on_the_day_goes_to_the_head(self, real_schedule):
        """先頭へ積む。末尾だと待ちに上限が無く、watchdog の閾値を約束から導けない。"""
        items, marks, _, _ = self._plan(date(2026, 10, 16), ["beta", "gate:max-features"],
                                        {"disclosures": "2026-10"}, interim=_utc(2026, 9, 16))
        assert items == ["interim", "beta", "gate:max-features"]
        assert marks["interim"] == "2026-10"

    def test_handled_once_a_month(self, real_schedule):
        marks = {"disclosures": "2026-10", "interim": "2026-10"}
        items, _, notes, calls = self._plan(date(2026, 10, 19), ["beta"], marks)
        assert items == ["beta"] and notes == [] and calls == []

    def test_a_late_start_still_catches_up(self, real_schedule):
        """「16日ちょうど」ではなく「16日以降の最初の実走」。土日や休暇を挟んでも取りこぼさない。"""
        items, *_ = self._plan(date(2026, 11, 30), [], {"disclosures": "2026-11"},
                               interim=_utc(2026, 10, 16))
        assert items == ["interim"]

    def test_already_produced_this_month_is_not_queued(self, real_schedule):
        """手で回した月に二重に回さない（2026-09-16 の手動実走の直後にデプロイする形）。"""
        items, marks, notes, _ = self._plan(date(2026, 9, 17), ["oof:split-bias"], {},
                                            disclosures=_utc(2026, 9, 7, 23, 14),
                                            interim=_utc(2026, 9, 16, 0, 4))
        assert items == ["oof:split-bias"]
        assert marks == {"disclosures": "2026-09", "interim": "2026-09"}
        assert len(notes) == 2 and all("積まない" in n for n in notes)

    def test_the_anchor_is_midnight_jst(self, real_schedule):
        """16日 00:30 JST（＝15日 15:30 UTC）に入った行は今月ぶん。UTC の日付で比べると取り違える。"""
        marks = {"disclosures": "2026-10"}
        items, *_ = self._plan(date(2026, 10, 16), [], marks, interim=_utc(2026, 10, 15, 15, 30))
        assert items == []
        items, *_ = self._plan(date(2026, 10, 16), [], marks, interim=_utc(2026, 10, 15, 14, 30))
        assert items == ["interim"]

    def test_already_queued_is_moved_not_duplicated(self, real_schedule):
        items, *_ = self._plan(date(2026, 10, 16), ["beta", "interim"], {"disclosures": "2026-10"},
                               interim=_utc(2026, 9, 16))
        assert items == ["interim", "beta"]

    def test_two_due_jobs_keep_the_schedule_order(self, real_schedule):
        items, *_ = self._plan(date(2026, 10, 16), ["beta"], {})
        assert items == [s.job for s in REAL_SCHEDULE] + ["beta"]

    def test_never_produced_is_queued(self, real_schedule):
        items, *_ = self._plan(date(2026, 10, 1), [], {})
        assert items == ["disclosures"]

    def test_unmeasurable_falls_to_the_enqueue_side(self, real_schedule):
        """積まない側へ倒すと、次に気づくのは1か月以上先の watchdog になる。収集は冪等。"""
        items, marks, notes, _ = self._plan(date(2026, 10, 16), [], {"disclosures": "2026-10"},
                                            interim=RuntimeError("接続できない"))
        assert items == ["interim"]
        assert marks["interim"] == "2026-10"
        assert any("測れない" in n for n in notes)

    def test_notes_survive_cp932(self, real_schedule):
        """暦の行は `Runner.write` を通る＝print が先に走る。1文字で暦ごと落とさない。"""
        _, _, notes, _ = self._plan(date(2026, 10, 16), [], {},
                                    disclosures=RuntimeError("壊れた — 値"),
                                    interim=_utc(2026, 10, 16))
        assert notes
        for n in notes:
            n.encode("cp932")


class TestApplySchedule:
    @pytest.fixture
    def db(self, fake_db, monkeypatch, real_schedule):
        monkeypatch.setattr(rd, "_session", lambda: fake_db)
        return fake_db

    def test_writes_queue_and_marks(self, db, monkeypatch):
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced())
        rd.write_queue(["beta"], db=db)

        items, notes = rd.apply_schedule(date(2026, 10, 16), db=db)

        assert rd.read_queue(db=db) == items == ["disclosures", "interim", "beta"]
        assert rd.read_schedule_marks(db=db) == {"disclosures": "2026-10", "interim": "2026-10"}
        assert len(notes) == 2

    def test_read_only_writes_nothing(self, db, monkeypatch):
        """`--peek` / `--queue` / ドライランは見せるだけ。書くと見ただけで今月ぶんが消費される。"""
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced())
        rd.write_queue(["beta"], db=db)

        items, _ = rd.apply_schedule(date(2026, 10, 16), db=db, write=False)

        assert items[:2] == ["disclosures", "interim"]
        assert rd.read_queue(db=db) == ["beta"]
        assert rd.KEY_SCHEDULE not in db.store

    def test_a_failed_read_is_rolled_back_before_writing(self, db, monkeypatch):
        """後始末しないと、続くキュー書き込みが失敗した文に巻き込まれる（PostgreSQL）。"""
        rolled = []
        db.rollback = lambda: rolled.append(True)
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced(
            disclosures=RuntimeError("boom"), interim=RuntimeError("boom")))

        rd.apply_schedule(date(2026, 10, 16), db=db)

        assert len(rolled) == 2
        assert rd.read_queue(db=db) == ["disclosures", "interim"]

    def test_corrupt_marks_read_as_empty(self, db):
        """例外にすると、値が1つ壊れただけで日中バッチが起動不能になる。"""
        db.store[rd.KEY_SCHEDULE] = "{壊れている"
        assert rd.read_schedule_marks(db=db) == {}
        db.store[rd.KEY_SCHEDULE] = json.dumps(["not", "a", "dict"])
        assert rd.read_schedule_marks(db=db) == {}


class TestMonthlyOverlap:
    """月次系のバッチ（01:00 起動・16時間の窓）と日中枠（8:00〜）は時間が重なる。

    並走は所要ではなく結論を変える（#618・macro_beta の発散 0 -> 344）。
    """

    @pytest.mark.parametrize("mod,ps1", [
        (rm, "install_monthly_task.ps1"),
        (rmb, "install_monthly_beta_task.ps1"),
        (rm1, "install_monthly_m1_task.ps1"),
    ])
    def test_trigger_matches_the_installer(self, mod, ps1):
        """書き写した起動日がずれると、重なる日を見送らず重ならない日を見送る。"""
        text = (ROOT / "scripts" / ps1).read_text(encoding="utf-8-sig")
        day = re.search(r"\[int\]\$Day\s*=\s*(\d+)", text)
        time = re.search(r'\[string\]\$Time\s*=\s*"([^"]+)"', text)
        assert day and time, f"{ps1} から既定の -Day / -Time を読めない（書式が変わった）"
        assert mod.TRIGGER_DAY == int(day.group(1))
        assert mod.TRIGGER_TIME == time.group(1)

    def test_daytime_trigger_matches_the_installer(self):
        text = (ROOT / "scripts" / "install_daytime_task.ps1").read_text(encoding="utf-8-sig")
        m = re.search(r'\[string\]\$Time\s*=\s*"([^"]+)"', text)
        assert m, "install_daytime_task.ps1 から既定の -Time を読めない（書式が変わった）"
        assert m.group(1) == rd.TRIGGER_TIME

    def test_every_monthly_batch_is_considered(self):
        """月次系のバッチを足したらここへ。忘れると、その日に重い計算が並走する。"""
        monthly = {w.key_run for w in batch_freshness.WATCHED if w.cadence_h >= 28 * 24}
        assert {m.KEY_LAST_RUN for m in rd.MONTHLY_BATCHES} == monthly

    def test_the_days_are_derived_from_the_promises(self):
        assert rd.monthly_overlap_days() == frozenset({1, 2, 3})

    def test_a_batch_that_ends_before_the_slot_does_not_block(self, monkeypatch):
        monkeypatch.setattr(rmb, "WINDOW_MIN", 6 * 60)       # 01:00〜07:00
        assert 2 not in rd.monthly_overlap_days()

    def test_a_window_crossing_midnight_blocks_the_next_day(self, monkeypatch):
        monkeypatch.setattr(rm1, "TRIGGER_TIME", "20:00")    # 翌日 12:00 まで
        days = rd.monthly_overlap_days()
        assert 4 in days and 3 not in days

    def test_other_days_take_the_head(self):
        assert rd.select_job(["beta", "interim"], QUIET_DAY) == ("beta", None)

    @pytest.mark.parametrize("day", [1, 2, 3])
    def test_monthly_days_skip_sensitive_jobs(self, day):
        key, why = rd.select_job(["beta", "tune:macro_dlm", "interim", "disclosures"],
                                 date(2026, 10, day))
        assert key == "interim"
        assert why and "interim" in why
        why.encode("cp932")

    def test_a_collection_head_needs_no_note(self):
        assert rd.select_job(["interim", "beta"], date(2026, 10, 1)) == ("interim", None)

    def test_nothing_runnable_is_none_with_a_reason(self):
        key, why = rd.select_job(["beta"], date(2026, 10, 2))
        assert key is None and why
        why.encode("cp932")

    def test_unknown_jobs_are_not_taken_on_monthly_days(self):
        """判断材料が無い名前は敏感側に倒す（`--peek` と同じ方針）。"""
        assert rd.select_job(["vanished", "interim"], date(2026, 10, 1))[0] == "interim"
        assert rd.select_job(["vanished"], date(2026, 10, 1))[0] is None

    def test_empty_queue(self):
        assert rd.select_job([], date(2026, 10, 1)) == (None, None)


class TestHolidays:
    """祝日・年末年始は並走に敏感な仕事を取り出さない（#684）。

    トリガは月〜金の固定で祝日を知らない。見送りを忘れても値はもっともらしいまま出るので、
    失敗としては現れない。
    """

    SILVER_WEEK = (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23))

    @pytest.mark.parametrize("day", SILVER_WEEK)
    def test_the_2026_silver_week_is_a_holiday(self, day):
        assert rd.is_holiday(day) is True

    @pytest.mark.parametrize("day", [date(2026, 9, 24), date(2026, 12, 28), date(2027, 1, 4)])
    def test_ordinary_weekdays_are_not(self, day):
        assert rd.is_holiday(day) is False

    @pytest.mark.parametrize("day", [date(2026, 12, 29), date(2026, 12, 31),
                                     date(2027, 1, 2), date(2027, 1, 3)])
    def test_the_year_end_break_counts(self, day):
        assert rd.is_holiday(day) is True

    def test_a_year_outside_the_table_is_unknown(self):
        assert rd.is_holiday(date(2030, 5, 6)) is None

    def test_the_quiet_day_used_by_other_tests_is_not_a_holiday(self):
        """既定の「今日」が祝日だと、他のテストが黙って見送りの経路を通る。"""
        assert rd.is_holiday(QUIET_DAY) is False

    def test_the_table_is_well_formed(self):
        for year, days in rd.HOLIDAYS.items():
            assert all(d.year == year for d in days), f"{year} 年の表に別の年が混ざっている"
            assert list(days) == sorted(set(days)), f"{year} 年の表が昇順・重複なしでない"

    def test_the_table_covers_next_year_from_october(self):
        """**実行日に依存する（意図して）。** 内閣府は翌年分を毎年2月に公表するので、
        10月になっても翌年が無いのは足し忘れ。表が切れると祝日の見送りが黙って外れる。"""
        today = date.today()
        need = today.year + (1 if today.month >= 10 else 0)
        assert need in rd.HOLIDAYS, (
            f"run_daytime.HOLIDAYS に {need} 年が無い。"
            "https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html から足すこと")

    def test_a_holiday_skips_sensitive_jobs_but_takes_collection(self):
        key, why = rd.select_job(["beta", "gate:macro", "interim"], date(2026, 9, 21))
        assert key == "interim"
        assert why and "祝日" in why and "interim" in why
        why.encode("cp932")

    def test_nothing_runnable_on_a_holiday_waits_for_a_weekday(self):
        key, why = rd.select_job(["beta"], date(2026, 9, 22))
        assert key is None
        assert why and "次の平日" in why
        why.encode("cp932")

    def test_the_override_lifts_the_holiday(self):
        assert rd.select_job(["beta", "interim"], date(2026, 9, 21), True) == ("beta", None)

    def test_the_override_does_not_lift_a_monthly_day(self):
        """2027-01-01 は元日で、かつ月次の起動日。月次は人の有無と関係が無い。"""
        day = date(2027, 1, 1)
        assert rd.blocked_by(day, holiday_override=True) == "monthly"
        assert rd.select_job(["beta"], day, True)[0] is None

    def test_a_weekday_is_unchanged(self):
        assert rd.blocked_by(date(2026, 9, 24)) is None
        assert rd.select_job(["beta", "interim"], date(2026, 9, 24)) == ("beta", None)

    def test_outside_the_table_does_not_block_but_says_so(self):
        day = date(2030, 5, 6)
        assert rd.blocked_by(day) is None
        note = rd.holiday_table_note(day)
        assert note and "2030" in note
        note.encode("cp932")
        assert rd.holiday_table_note(QUIET_DAY) is None

    def test_the_override_is_only_valid_on_its_day(self, fake_db):
        today = date(2026, 9, 21)
        assert rd.read_holiday_override(today, db=fake_db) is False
        rd.write_holiday_override(today, db=fake_db)
        assert rd.read_holiday_override(today, db=fake_db) is True
        assert rd.read_holiday_override(date(2026, 9, 22), db=fake_db) is False

    @pytest.mark.parametrize("raw", ["", "garbage", "2026-09-21T00:00:00", None])
    def test_a_broken_override_reads_as_absent(self, fake_db, raw):
        fake_db.store[rd.KEY_HOLIDAY_OVERRIDE] = raw
        assert rd.read_holiday_override(date(2026, 9, 21), db=fake_db) is False


class TestCalendarInMain:
    """実走・ドライラン・`--peek`・`--queue` が同じ暦と同じ選び方を使う。"""

    @pytest.fixture
    def db(self, fake_db, monkeypatch, tmp_path):
        monkeypatch.setattr(rd, "_session", lambda: fake_db)
        monkeypatch.setattr(rd, "log_path", lambda *a, **k: tmp_path / "daytime.log")
        self.log = tmp_path / "daytime.log"
        self.footprints = []
        monkeypatch.setattr(rd, "record_footprint", lambda results: self.footprints.append(results))
        self.ran = []

        def _run_batch(spec, steps, hooks, argv):
            self.ran.append([s.name for s in steps])
            return 0

        monkeypatch.setattr(rd.bc, "run_batch", _run_batch)
        return fake_db

    @staticmethod
    def _on(monkeypatch, day, **last):
        monkeypatch.setattr(rd, "_today", lambda: day)
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced(**last))

    @staticmethod
    def _settled(day):
        """暦が何もしない月（両方とも今月ぶんが入っている）。"""
        return {"disclosures": _utc(day.year, day.month, 1, 0, 0),
                "interim": _utc(day.year, day.month, 16, 0, 0)}

    def test_a_monthly_day_leaves_sensitive_jobs_queued(self, db, monkeypatch):
        day = date(2026, 10, 2)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["beta"], db=db)

        assert rd.main([]) == 0

        assert self.ran == []
        assert rd.read_queue(db=db) == ["beta"], "見送った仕事がキューから消えた"
        assert self.footprints == [{}], "見送った日にも足跡は残す（起動しなかった、と区別する）"
        assert rd.read_inflight(db=db) is None
        assert "[calendar]" in self.log.read_text(encoding="utf-8")

    def test_a_monthly_day_runs_collection_from_the_middle(self, db, monkeypatch):
        day = date(2026, 10, 2)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["beta", "interim"], db=db)

        rd.main([])

        assert self.ran == [["collect_interim"]]
        assert rd.read_queue(db=db) == ["beta"]

    def test_a_holiday_leaves_sensitive_jobs_queued(self, db, monkeypatch):
        day = date(2026, 9, 21)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["gate:max-features", "gate:macro"], db=db)

        assert rd.main([]) == 0

        assert self.ran == []
        assert rd.read_queue(db=db) == ["gate:max-features", "gate:macro"]
        assert self.footprints == [{}]
        assert "祝日" in self.log.read_text(encoding="utf-8")

    def test_allow_holiday_lets_todays_run_take_the_head(self, db, monkeypatch, capsys):
        day = date(2026, 9, 21)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["gate:macro"], db=db)

        assert rd.main(["--allow-holiday"]) == 0
        assert db.store[rd.KEY_HOLIDAY_OVERRIDE] == "2026-09-21"
        capsys.readouterr().out.encode("cp932")
        rd.main([])

        assert self.ran == [["gate_macro"]]
        assert rd.read_queue(db=db) == []

    def test_yesterdays_override_does_not_carry_over(self, db, monkeypatch):
        day = date(2026, 9, 22)
        self._on(monkeypatch, day, **self._settled(day))
        db.store[rd.KEY_HOLIDAY_OVERRIDE] = "2026-09-21"
        rd.write_queue(["beta"], db=db)

        rd.main([])

        assert self.ran == []
        assert rd.read_queue(db=db) == ["beta"]

    def test_a_year_outside_the_table_is_logged(self, db, monkeypatch):
        day = date(2030, 5, 7)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["gate:macro"], db=db)

        rd.main([])

        assert self.ran == [["gate_macro"]], "表が切れた年に見送る側へ倒すとキューが止まる"
        assert "2030" in self.log.read_text(encoding="utf-8")

    def test_the_scheduled_job_is_todays_run(self, db, monkeypatch):
        self._on(monkeypatch, date(2026, 10, 16),
                 disclosures=_utc(2026, 10, 1, 0, 0), interim=_utc(2026, 9, 16, 0, 4))
        rd.write_queue(["oof:split-bias"], db=db)

        rd.main([])

        assert self.ran == [["collect_interim"]]
        assert rd.read_queue(db=db) == ["oof:split-bias"]
        assert rd.read_schedule_marks(db=db) == {"disclosures": "2026-10", "interim": "2026-10"}
        assert "[schedule] interim" in self.log.read_text(encoding="utf-8")

    def test_the_scheduled_job_goes_ahead_of_a_reclaimed_job(self, db, monkeypatch):
        self._on(monkeypatch, date(2026, 10, 16),
                 disclosures=_utc(2026, 10, 1, 0, 0), interim=_utc(2026, 9, 16, 0, 4))
        rd.write_queue(["gate:max-features"], db=db)
        db.store[rd.KEY_INFLIGHT] = json.dumps(
            {"job": "beta", "state": rd._STATE_RUNNING, "requeued": 0, "at": "x"})

        rd.main([])

        assert self.ran == [["collect_interim"]]
        assert rd.read_queue(db=db) == ["beta", "gate:max-features"]

    def test_the_first_run_after_deploy_does_not_rerun_september(self, db, monkeypatch):
        """2026-09-16 に手で回した直後の形。暦は印だけ付け、キューの先頭がそのまま走る。"""
        self._on(monkeypatch, date(2026, 9, 17),
                 disclosures=_utc(2026, 9, 7, 23, 14), interim=_utc(2026, 9, 16, 0, 4))
        rd.write_queue(["oof:split-bias", "gate:max-features"], db=db)

        rd.main([])

        assert self.ran == [["oof_split_bias"]]
        assert rd.read_queue(db=db) == ["gate:max-features"]
        assert rd.read_schedule_marks(db=db) == {"disclosures": "2026-09", "interim": "2026-09"}

    def test_dry_run_writes_neither_queue_marks_nor_footprint(self, db, monkeypatch, capsys):
        self._on(monkeypatch, date(2026, 10, 16))
        rd.write_queue(["beta"], db=db)

        rd.main(["--dry-run"])

        assert self.ran == [["collect_disclosures"]], "ドライランが実走と違う1件を見せている"
        assert rd.read_queue(db=db) == ["beta"]
        assert rd.KEY_SCHEDULE not in db.store
        assert self.footprints == []
        assert "[schedule]" in capsys.readouterr().out

    def test_dry_run_on_an_empty_queue_does_not_crash(self, db, monkeypatch):
        day = date(2026, 10, 20)
        self._on(monkeypatch, day, **self._settled(day))

        assert rd.main(["--dry-run"]) == 0
        assert self.footprints == [], "ドライランが足跡を書いた"

    def _peek(self, capsys):
        assert rd.main(["--peek"]) == 0
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if ln.lstrip().startswith("{"))
        return json.loads(line)

    def test_peek_shows_the_scheduled_job(self, db, monkeypatch, capsys):
        self._on(monkeypatch, date(2026, 10, 16),
                 disclosures=_utc(2026, 10, 1, 0, 0), interim=_utc(2026, 9, 16, 0, 4))
        rd.write_queue(["beta"], db=db)

        got = self._peek(capsys)

        assert got["key"] == "interim" and got["sensitive"] is False
        assert got["remaining"] == 2 and got["blocked"] is False
        assert rd.read_queue(db=db) == ["beta"], "peek がキューを書き換えた"
        assert rd.KEY_SCHEDULE not in db.store, "peek が暦の印を書いた"

    def test_peek_reports_a_blocked_day(self, db, monkeypatch, capsys):
        day = date(2026, 10, 2)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["beta"], db=db)

        got = self._peek(capsys)

        assert got["key"] is None and got["blocked"] is True and got["remaining"] == 1
        assert got["blocked_by"] == "monthly" and got["holiday_skip"] is False

    def test_peek_reports_a_holiday(self, db, monkeypatch, capsys):
        """`-Now -Force` はこれを見て解除印を書く。月次と区別できないと外してはいけない方を外す。"""
        day = date(2026, 9, 23)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["beta"], db=db)

        got = self._peek(capsys)

        assert got["key"] is None and got["blocked"] is True
        assert got["blocked_by"] == "holiday" and got["holiday_skip"] is True

        db.store[rd.KEY_HOLIDAY_OVERRIDE] = day.isoformat()
        got = self._peek(capsys)
        assert got["key"] == "beta" and got["blocked"] is False
        assert got["blocked_by"] is None and got["holiday_skip"] is False

    def test_peek_on_a_holiday_with_collection_still_flags_the_skip(self, db, monkeypatch, capsys):
        day = date(2026, 9, 21)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["beta", "interim"], db=db)

        got = self._peek(capsys)

        assert got["key"] == "interim" and got["blocked"] is False
        assert got["holiday_skip"] is True, "-Force でも先頭の beta に届かなくなる"

    def test_queue_listing_shows_a_holiday(self, db, monkeypatch, capsys):
        day = date(2026, 9, 21)
        self._on(monkeypatch, day, **self._settled(day))
        rd.write_queue(["beta"], db=db)

        assert rd.main(["--queue"]) == 0

        out = capsys.readouterr().out
        assert "祝日" in out
        out.encode("cp932")

    def test_queue_listing_shows_the_calendar(self, db, monkeypatch, capsys):
        """セッション開始時に必ず見る画面。次の実走で何が積まれ、何が見送られるかを出す。"""
        self._on(monkeypatch, date(2026, 10, 2))
        rd.write_queue(["beta"], db=db)

        assert rd.main(["--queue"]) == 0

        out = capsys.readouterr().out
        assert "[schedule]" in out and "[calendar]" in out
        out.encode("cp932")
        assert rd.read_queue(db=db) == ["beta"]
        assert rd.KEY_SCHEDULE not in db.store


# ── 引数はキューに触る前に解析する（#692）──────────────────────────────────
# 以前は `"--x" in args` の手書き判定のあと、実走経路の最後（`bc.run_batch` の中）で
# 初めて解析していた。`--help` は `take` の後で `SystemExit(0)` になり、キュー先頭の仕事が
# 黙って消えた（ヘルプが出て exit 0。in-flight マーカーも `finally` で消えるので #639 の
# 回収にも引っかからない）。2026-09-18 に実際に踏んだ。


class TestArgumentsAreParsedBeforeTheQueue:
    LAUNCHER = ROOT / "run_daytime.ps1"

    @pytest.fixture
    def db(self, fake_db, monkeypatch, tmp_path):
        monkeypatch.setattr(rd, "_session", lambda: fake_db)
        self.log = tmp_path / "daytime.log"
        monkeypatch.setattr(rd, "log_path", lambda *a, **k: self.log)
        self.footprints = []
        monkeypatch.setattr(rd, "record_footprint", lambda results: self.footprints.append(results))
        self.ran = []

        def _run_batch(spec, steps, hooks, argv):
            self.ran.append([s.name for s in steps])
            return 0

        monkeypatch.setattr(rd.bc, "run_batch", _run_batch)
        return fake_db

    def _loaded(self, db, monkeypatch):
        """触られたら必ず跡が残る状態: 暦が積む日・回収すべきマーカー・キューに仕事。"""
        monkeypatch.setattr(rd, "_today", lambda: date(2026, 10, 16))
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced())      # 成果物なし＝両方積む日
        rd.write_queue(["gate:macro"], db=db)
        db.store[rd.KEY_INFLIGHT] = json.dumps(
            {"job": "beta", "state": rd._STATE_RUNNING, "requeued": 0, "at": "x"})
        return dict(db.store)

    def _untouched(self, db, before):
        assert db.store == before, "キュー・暦の印・in-flight マーカーのどれかが書き換わった"
        assert self.ran == [], "バッチが起動した"
        assert self.footprints == [], "足跡を書いた"
        assert not self.log.exists(), "ログへ書いた"

    @pytest.mark.parametrize("argv,code", [
        (["--help"], 0),
        (["-h"], 0),
        (["--no-such-flag"], 2),
        (["--dry-run", "--typo"], 2),
        (["--steps"], 2),                      # 値の欠落
        (["--enqueue"], 2),
    ])
    def test_help_and_bad_arguments_touch_nothing(self, db, monkeypatch, capsys, argv, code):
        before = self._loaded(db, monkeypatch)

        with pytest.raises(SystemExit) as e:
            rd.main(argv)

        assert e.value.code == code
        self._untouched(db, before)
        captured = capsys.readouterr()
        (captured.out + captured.err).encode("cp932")

    def test_help_lists_the_queue_operations(self, db, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            rd.main(["--help"])
        out = capsys.readouterr().out
        for flag in ("--queue", "--peek", "--allow-holiday", "--clear-queue", "--enqueue",
                     "--steps", "--dry-run", "--no-issue"):
            assert flag in out, f"ヘルプに {flag} が無い"

    def test_two_queue_operations_are_rejected_not_resolved_silently(self, db, monkeypatch):
        """以前は先に判定した `--queue` が黙って勝った。`--clear-queue` を含むので止める側へ倒す。"""
        before = self._loaded(db, monkeypatch)

        with pytest.raises(SystemExit) as e:
            rd.main(["--queue", "--clear-queue"])

        assert e.value.code == 2
        self._untouched(db, before)

    def test_an_unknown_step_is_rejected_before_the_job_is_taken(self, db, monkeypatch):
        """`--steps` の検証は取り出す1件が決まってからでないとできないが、`take` の前に置く。"""
        monkeypatch.setattr(rd, "_today", lambda: QUIET_DAY)
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced(
            disclosures=_utc(QUIET_DAY.year, QUIET_DAY.month, 1, 0, 0)))
        rd.write_queue(["gate:macro"], db=db)

        with pytest.raises(SystemExit):
            rd.main(["--steps", "no_such_step"])

        assert rd.read_queue(db=db) == ["gate:macro"], "打ち間違いの --steps で仕事が消えた"
        assert rd.read_inflight(db=db) is None
        assert self.ran == []

    def test_a_known_step_still_runs(self, db, monkeypatch):
        monkeypatch.setattr(rd, "_today", lambda: QUIET_DAY)
        monkeypatch.setattr(rd, "SCHEDULE", _with_produced(
            disclosures=_utc(QUIET_DAY.year, QUIET_DAY.month, 1, 0, 0)))
        rd.write_queue(["gate:macro"], db=db)

        assert rd.main(["--steps", "gate_macro", "--no-issue"]) == 0

        assert self.ran == [["gate_macro"]]
        assert rd.read_queue(db=db) == []

    def test_enqueue_splits_on_commas(self, db):
        assert rd.main(["--enqueue", "beta,gate:ttm"]) == 0
        assert rd.read_queue(db=db) == ["beta", "gate:ttm"]

    def test_enqueue_with_no_names_is_refused(self, db):
        with pytest.raises(SystemExit) as e:
            rd.main(["--enqueue", ","])
        assert "--enqueue" in str(e.value.code)
        assert rd.read_queue(db=db) == []

    def test_every_flag_the_launcher_passes_is_known(self):
        """ps1 が渡す引数をパーサが知らないと、今後は exit 2 で止まる（黙って実走へは進まない）。"""
        text = self.LAUNCHER.read_text(encoding="utf-8-sig")
        passed = set(re.findall(r'"(--[a-z][a-z-]*)"', text))
        assert passed, "run_daytime.ps1 から引数を読めない（書式が変わった）"
        known = {s for a in rd.build_parser()._actions for s in a.option_strings}
        assert passed <= known, f"パーサが知らない引数: {sorted(passed - known)}"
