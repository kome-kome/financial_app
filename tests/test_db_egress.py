"""db_egress（クライアント側 Egress 台帳とサーキットブレーカ）のユニットテスト — Issue #478。

本番でしか実数が出ない仕組み（psycopg2 の rowcount は SELECT でも有効／SQLite は -1）
なので、ここで担保するのは次の4点に絞る:

  - 帰属: 文からテーブル名を取り出せること・結果セットを返さない文を数えないこと
  - 誠実さ: rowcount が取れない呼び出しを 0 件に化けさせず unknown へ隔離すること
  - 歯止め: 予算超過で例外を送出し、局所上書きが必ず元へ戻ること
  - 出所: 較正値に measured_on / source_issue が必ず付いていること

DB へは繋がない（in-memory SQLite のみ）。
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_egress
from db_egress import (
    EGRESS_BYTES_PER_COLUMN,
    EGRESS_COST_TABLE,
    DEFAULT_BYTES_PER_COLUMN,
    EgressBudgetExceeded,
    LEDGER,
    bytes_per_row,
    egress_budget,
    extract_table,
    summary_line,
)

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def clean_ledger(monkeypatch):
    """台帳・上書き予算・関連環境変数を毎回まっさらにする。

    `FINAPP_EGRESS_LEDGER` だけは delenv ではなく**明示的に無効化**する。既定オン
    （#478 の穴3）にしたので、単に消すとテストがリポジトリ直下の
    `.egress/ledger.jsonl` へ実データを書き込む。既定の解決そのものを見るテストは
    自分で delenv する（`TestLedgerPathDefault`）。

    サイクル累計も既定で切る。有効時の挙動は `TestCycleBudget` が
    `cycle_tracking_enabled` を差し替えて検証する（DB へは繋がない）。
    """
    for var in ("FINAPP_EGRESS_ROW_LIMIT", "FINAPP_EGRESS_MB_LIMIT",
                "FINAPP_EGRESS_ENFORCE", "FINAPP_JOB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINAPP_EGRESS_LEDGER", "0")
    monkeypatch.setenv("FINAPP_EGRESS_CYCLE", "0")
    db_egress._reset_for_tests()
    yield
    db_egress._reset_for_tests()


def _seed_cycle(base_bytes: float, start: str = "2026-08-18") -> None:
    """DB を触らずにサイクル累計の初期値を置く（読み込み済みの状態にする）。"""
    with db_egress._cycle_lock:
        db_egress._cycle_state.update(
            {"loaded": True, "start": start, "base_bytes": float(base_bytes)})


class TestExtractTable:
    @pytest.mark.parametrize("stmt,expected", [
        ("SELECT a.x FROM stock_price_weekly a WHERE a.x = 1", "stock_price_weekly"),
        ("SELECT * FROM companies", "companies"),
        ("SELECT * FROM public.macro_data", "macro_data"),
        ('SELECT * FROM "financial_metrics"', "financial_metrics"),
        ("SELECT * FROM ONLY financial_records", "financial_records"),
        ("SELECT c.x FROM companies c JOIN financial_records f ON f.i = c.i", "companies"),
        ("INSERT INTO macro_data (a) VALUES (1) RETURNING id", "macro_data"),
        ("UPDATE financial_records SET x = 1 RETURNING id", "financial_records"),
        ("SELECT 1", db_egress.UNKNOWN_TABLE),
        ("", db_egress.UNKNOWN_TABLE),
    ])
    def test_extracts_primary_table(self, stmt, expected):
        assert extract_table(stmt) == expected

    def test_subquery_falls_through_to_real_identifier(self):
        """`FROM (` は識別子にマッチしないので、内側の実テーブルが主テーブルになる。"""
        stmt = "SELECT count(*) AS count_1 FROM (SELECT x FROM stock_price_weekly) AS anon_1"
        assert extract_table(stmt) == "stock_price_weekly"


class TestBytesPerRow:
    def test_exact_measurement_wins(self):
        # 3列は #482 で 32.1 → 20.2 へ是正（ローカル正本と Supabase の両側で
        # avg(octet_length) が 20.19 B/行 で一致し、旧値は再現できなかった）
        assert bytes_per_row("stock_price_weekly", 3) == 20.2
        assert bytes_per_row("stock_price_weekly", 4) == 42.0

    def test_falls_back_to_per_column_rate_of_same_table(self):
        """実測に無い列数でも、同じテーブルの列単価（実測由来）を使う。

        列数は**較正表に無いもの**を選ぶこと。ここは以前 `financial_metrics` の 36 列を
        使っていたが、#482 でその組の実測が入りフォールバック経路を通らなくなった。
        """
        n_cols = 42
        assert ("financial_metrics", n_cols) not in EGRESS_COST_TABLE
        rate = EGRESS_BYTES_PER_COLUMN["financial_metrics"].bytes_per_row
        assert bytes_per_row("financial_metrics", n_cols) == pytest.approx(rate * n_cols)

    def test_unknown_table_uses_conservative_default(self):
        """較正表に載っていない表（＝これから足される表）は保守側の既定へ倒す。

        実在の表名を書かないこと。#493 で mirror 16 表すべてに実測が入ったため、
        当時未較正だった plugin_tuned_params を使っていたこのテストは意味を失った。
        """
        assert bytes_per_row("table_added_after_the_last_calibration", 5) == \
            DEFAULT_BYTES_PER_COLUMN * 5

    def test_zero_columns_does_not_zero_out_the_estimate(self):
        """列数が取れなくても 0 バイト扱いにしない（過小評価はブレーカを無力化する）。"""
        assert bytes_per_row("whatever", 0) == DEFAULT_BYTES_PER_COLUMN


class TestCalibrationProvenance:
    """出所の無い数字を混ぜられないようにする（#446 実測表が正本）。"""

    @pytest.mark.parametrize("cost", list(EGRESS_COST_TABLE.values()) +
                                      list(EGRESS_BYTES_PER_COLUMN.values()))
    def test_every_entry_cites_a_measurement(self, cost):
        assert cost.measured_on, "measured_on が空"
        assert cost.source_issue.startswith("#"), "source_issue は Issue 番号で示すこと"
        assert cost.note, "note に実測の内訳を書くこと"
        assert cost.bytes_per_row > 0


class TestRecord:
    def test_accumulates_rows_and_bytes(self):
        LEDGER.record("SELECT x FROM companies", 4437, 14)
        snap = LEDGER.snapshot()
        assert snap["rows"] == 4437
        # 較正値そのものは #446 / #493 のように測り直される。ここの主題は積み上げなので
        # 数字を直書きせず較正表から引く（直書きすると再較正のたびに無関係に落ちる）。
        assert snap["tables"]["companies"]["est_bytes"] == \
            pytest.approx(4437 * bytes_per_row("companies", 14))
        assert snap["unknown_calls"] == 0

    def test_negative_rowcount_goes_to_unknown_not_zero(self):
        """SQLite の SELECT は rowcount=-1。0 件と区別できないと「引いていない」と誤読する。"""
        LEDGER.record("SELECT x FROM companies", -1, 14)
        snap = LEDGER.snapshot()
        assert snap["rows"] == 0
        assert snap["est_bytes"] == 0
        assert snap["unknown_calls"] == 1
        assert snap["tables"]["companies"]["unknown_calls"] == 1
        assert snap["tables"]["companies"]["calls"] == 1     # 呼ばれたこと自体は残る

    def test_external_transfer_counts_toward_total(self):
        """pg_dump など SQLAlchemy を通らない転送も手で積める。"""
        LEDGER.record_external("mirror_pull", 10 * MB, "コアテーブル dump")
        assert LEDGER.snapshot()["est_bytes"] == pytest.approx(10 * MB)
        assert LEDGER.snapshot()["external"][0]["label"] == "mirror_pull"


class TestCircuitBreaker:
    def test_raises_when_row_limit_exceeded(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "1000")
        with pytest.raises(EgressBudgetExceeded, match="rows"):
            LEDGER.record("SELECT x FROM stock_price_weekly", 1001, 3)

    def test_raises_when_mb_limit_exceeded(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "0")      # 行数側は無効化
        monkeypatch.setenv("FINAPP_EGRESS_MB_LIMIT", "1")
        with pytest.raises(EgressBudgetExceeded, match="mb"):
            LEDGER.record("SELECT x FROM stock_price_weekly", 100_000, 3)

    def test_stays_silent_below_the_limit(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "1000")
        LEDGER.record("SELECT x FROM stock_price_weekly", 999, 3)   # 例外なく通ること

    def test_enforce_zero_records_but_does_not_raise(self, monkeypatch):
        """8/18 直後の逃げ道。計測は続くので台帳は埋まる。"""
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "10")
        monkeypatch.setenv("FINAPP_EGRESS_ENFORCE", "0")
        LEDGER.record("SELECT x FROM stock_price_weekly", 1000, 3)
        assert LEDGER.snapshot()["rows"] == 1000

    def test_message_names_the_escape_hatches(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "10")
        with pytest.raises(EgressBudgetExceeded) as exc:
            LEDGER.record("SELECT x FROM stock_price_weekly", 11, 3)
        assert "FINAPP_EGRESS_ENFORCE" in str(exc.value)
        assert "egress_budget" in str(exc.value)


class TestEgressBudgetContextManager:
    def test_scoped_raise_of_the_limit(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "100")
        with egress_budget(rows=10_000):
            LEDGER.record("SELECT x FROM stock_price_weekly", 5_000, 3)
        assert LEDGER.snapshot()["rows"] == 5_000

    def test_restores_previous_limits_on_exit(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_ROW_LIMIT", "100")
        with egress_budget(rows=10_000):
            assert db_egress.limits()[0] == 10_000
        assert db_egress.limits()[0] == 100

    def test_restores_after_exception(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_MB_LIMIT", "7")
        with pytest.raises(ValueError):
            with egress_budget(mb=999):
                raise ValueError("boom")
        assert db_egress.limits()[1] == 7

    def test_nesting_unwinds_one_level_at_a_time(self):
        with egress_budget(rows=100):
            with egress_budget(rows=200):
                assert db_egress.limits()[0] == 200
            assert db_egress.limits()[0] == 100

    def test_only_the_named_knob_is_overridden(self, monkeypatch):
        """db_timeouts と同じ約束: 指定しなかった側の既定は動かさない。"""
        monkeypatch.setenv("FINAPP_EGRESS_MB_LIMIT", "7")
        with egress_budget(rows=10_000):
            assert db_egress.limits()[1] == 7


class TestSummaryLine:
    def test_is_ascii_even_with_non_ascii_job_name(self, monkeypatch):
        """cp932 コンソールへリダイレクトすると非 ASCII は出力ごとクラッシュする（既知の罠）。"""
        monkeypatch.setenv("FINAPP_JOB", "夜間スコア更新")
        LEDGER.record("SELECT x FROM companies", 10, 14)
        line = summary_line()
        line.encode("ascii")            # 例外が出ないこと
        assert "summary" in line

    def test_reports_unknown_rowcount_when_present(self):
        LEDGER.record("SELECT x FROM companies", -1, 14)
        assert "unknown_rowcount=1" in summary_line()

    def test_omits_unknown_when_all_counts_are_known(self):
        LEDGER.record("SELECT x FROM companies", 5, 14)
        assert "unknown_rowcount" not in summary_line()


class TestLedgerFile:
    def test_appends_one_json_line_per_process(self, tmp_path, monkeypatch):
        path = tmp_path / "nested" / "ledger.jsonl"
        monkeypatch.setenv("FINAPP_EGRESS_LEDGER", str(path))
        monkeypatch.setenv("FINAPP_JOB", "nightly-scores")
        LEDGER.record("SELECT x FROM stock_price_weekly", 1_000, 3)
        db_egress.emit_summary()

        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["job"] == "nightly-scores"
        assert rows[0]["rows"] == 1_000
        assert rows[0]["tables"]["stock_price_weekly"]["rows"] == 1_000

    def test_second_emit_is_a_no_op(self, tmp_path, monkeypatch):
        """明示呼び出しの後に atexit が発火しても台帳へ2行入らない（実行数の二重計上を防ぐ）。"""
        path = tmp_path / "ledger.jsonl"
        monkeypatch.setenv("FINAPP_EGRESS_LEDGER", str(path))
        LEDGER.record("SELECT x FROM companies", 1, 14)
        db_egress.emit_summary()
        db_egress.emit_summary()
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    def test_write_failure_does_not_propagate(self, tmp_path, monkeypatch):
        """台帳が書けないことで本処理を落とさない（計測は本業の邪魔をしない）。"""
        blocked = tmp_path / "file.txt"
        blocked.write_text("x", encoding="utf-8")
        monkeypatch.setenv("FINAPP_EGRESS_LEDGER", str(blocked / "ledger.jsonl"))
        LEDGER.record("SELECT x FROM companies", 1, 14)
        db_egress.emit_summary()        # 例外が出ないこと

    def test_silent_when_nothing_was_queried(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.jsonl"
        monkeypatch.setenv("FINAPP_EGRESS_LEDGER", str(path))
        db_egress.emit_summary()
        assert not path.exists()


class TestEngineIntegration:
    """実 engine に張った状態での挙動（SQLite なので rowcount は unknown 側へ積まれる）。"""

    @pytest.fixture
    def engine(self):
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
        assert db_egress.install(eng) is True
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE companies (edinet_code TEXT, name TEXT)"))
        db_egress._reset_for_tests()        # DDL ぶんを落としてから本番の計測に入る
        yield eng
        eng.dispose()

    def test_select_is_recorded(self, engine):
        with engine.connect() as conn:
            conn.execute(text("SELECT edinet_code FROM companies")).fetchall()
        snap = LEDGER.snapshot()
        assert snap["tables"]["companies"]["calls"] == 1
        # SQLite の SELECT は rowcount=-1 なので実数ではなく unknown に積まれるのが正しい
        assert snap["unknown_calls"] == 1

    def test_write_statements_are_not_counted(self, engine):
        """INSERT/UPDATE/DELETE は ingress（無料）。結果セットを返さないので description が None。"""
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO companies VALUES ('E0', 'a')"))
            conn.execute(text("UPDATE companies SET name = 'b'"))
            conn.execute(text("DELETE FROM companies"))
        assert LEDGER.snapshot()["calls"] == 0

    def test_install_is_idempotent(self, engine):
        assert db_egress.install(engine) is False
        with engine.connect() as conn:
            conn.execute(text("SELECT edinet_code FROM companies")).fetchall()
        assert LEDGER.snapshot()["tables"]["companies"]["calls"] == 1   # 二重計上しない


class TestProductionEngineIsWired:
    def test_database_engine_has_the_listener(self):
        """database.py の import 時に張られていること（張り忘れは黙って無計測になる）。"""
        import database
        assert database.engine in db_egress._installed


class TestWorkflowLedgerCollection:
    """#478 の穴1: 台帳が集約されない。

    `FINAPP_JOB`（帰属ラベル）は 17 本すべてに入っていたのに、台帳ファイルを artifact と
    して回収していたのは `nightly-scores.yml` の1本だけだった。残り16本は run ログを手で
    落として `egress_report --log` に食わせない限り月次のロールアップに乗らない。
    **回収漏れは failure を出さないので `notify-failure.yml` では原理的に拾えない**
    （ADR-0031 の `HEAVY_AUTOMATION` と同型＝だから CI のメタ検査で落とす）。
    """

    WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

    def _workflows(self):
        for path in sorted(self.WORKFLOW_DIR.glob("*.yml")):
            yield path.name, yaml.safe_load(path.read_text(encoding="utf-8"))

    def _artifact_steps(self):
        for name, wf in self._workflows():
            for job in (wf.get("jobs") or {}).values():
                for step in (job.get("steps") or []):
                    if str(step.get("uses") or "").startswith("actions/upload-artifact"):
                        yield name, step

    def test_every_artifact_upload_collects_the_ledger(self):
        missing = [
            f"{name}({(step.get('with') or {}).get('name')})"
            for name, step in self._artifact_steps()
            if ".egress" not in str((step.get("with") or {}).get("path", ""))
        ]
        assert not missing, (
            "artifact に Egress 台帳 (.egress/*.jsonl) が含まれていない: " + ", ".join(missing))

    def test_the_check_is_not_vacuous(self):
        """glob やキー名が壊れて 0 件になると、この検査は黙って全部 pass する。"""
        assert len(list(self._artifact_steps())) >= 10

    def test_db_touching_steps_carry_a_job_label(self):
        """`FINAPP_JOB` が無いと台帳の帰属が argv 由来になり、ジョブ別集計が濁る。"""
        missing = []
        for name, wf in self._workflows():
            for job_name, job in (wf.get("jobs") or {}).items():
                for step in (job.get("steps") or []):
                    env = step.get("env") or {}
                    if "DATABASE_URL" in env and "FINAPP_JOB" not in env:
                        missing.append(f"{name}:{job_name}:{step.get('name')}")
        assert not missing, "DB を触るのに帰属ラベルが無いステップ: " + ", ".join(missing)


class TestLedgerPathDefault:
    """#478 の穴3: ローカル実行が既定で無計測だった。

    過去2回の Egress 超過はどちらもローカル検証の反復が主因なのに、環境変数を
    人が立てる運用だったため記録が1バイトも残っていなかった。
    """

    def test_defaults_to_repo_local_path_when_unset(self, monkeypatch):
        monkeypatch.delenv("FINAPP_EGRESS_LEDGER", raising=False)
        assert db_egress.ledger_path() == db_egress.DEFAULT_LEDGER_PATH

    def test_default_path_is_gitignored(self):
        """既定の書き先がコミット対象だと、計測を入れた途端にリポジトリが汚れる。"""
        ignored = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8")
        assert ".egress/" in ignored
        assert db_egress.DEFAULT_LEDGER_PATH.startswith(".egress/")

    @pytest.mark.parametrize("value", ["0", "", "   "])
    def test_explicit_disable(self, monkeypatch, value):
        monkeypatch.setenv("FINAPP_EGRESS_LEDGER", value)
        assert db_egress.ledger_path() is None

    def test_explicit_path_wins(self, monkeypatch):
        monkeypatch.setenv("FINAPP_EGRESS_LEDGER", "custom/x.jsonl")
        assert db_egress.ledger_path() == "custom/x.jsonl"

    def test_writes_without_any_env_var(self, tmp_path, monkeypatch):
        """環境変数を一切立てずに書かれること（既定オンの実挙動）。"""
        monkeypatch.delenv("FINAPP_EGRESS_LEDGER", raising=False)
        monkeypatch.chdir(tmp_path)
        LEDGER.record("SELECT x FROM companies", 5, 14)
        db_egress.emit_summary()

        written = tmp_path / db_egress.DEFAULT_LEDGER_PATH
        assert written.exists()
        assert json.loads(written.read_text(encoding="utf-8").strip())["rows"] == 5


class TestCycleWindow:
    """請求サイクルの境界（Supabase は毎月 18 日にリセット）。"""

    @pytest.mark.parametrize("today,expected", [
        ("2026-08-19", "2026-08-18"),      # サイクル開始の翌日
        ("2026-08-18", "2026-08-18"),      # 開始日ちょうど
        ("2026-08-17", "2026-07-18"),      # 開始日の前日は前サイクル
        ("2026-09-17", "2026-08-18"),
        ("2026-01-05", "2025-12-18"),      # 年跨ぎ
        ("2026-03-01", "2026-02-18"),      # 短い月を跨ぐ
    ])
    def test_boundary(self, today, expected):
        d = date.fromisoformat(today)
        assert db_egress.current_cycle_start(d).isoformat() == expected

    def test_cycle_day_matches_observed_billing_period(self):
        """2026-08-19 実測のダッシュボード表記は "18 Aug 2026 - 18 Sep 2026"。"""
        assert db_egress.EGRESS_CYCLE_DAY == 18


class TestCycleBudget:
    """#478 の穴2: プロセス予算では「じわじわ型」の超過を止められない。

    2026-08 の 7.312GB はスパイク約2GB＋平常運転 約5GB で、400MB/プロセスの
    ブレーカは一度も踏まれなかった。ここではサイクル累計側の歯止めを検証する。
    DB へは繋がず、`cycle_tracking_enabled` を差し替える。
    """

    @pytest.fixture(autouse=True)
    def enable_cycle(self, monkeypatch):
        monkeypatch.setattr(db_egress, "cycle_tracking_enabled", lambda: True)

    def test_process_total_is_added_to_the_carried_over_base(self):
        _seed_cycle(1.0 * 1024 ** 3)
        LEDGER.record("SELECT x FROM companies", 1_000, 14)
        snap = db_egress.cycle_snapshot()

        assert snap["base_bytes"] == pytest.approx(1.0 * 1024 ** 3)
        assert snap["total_bytes"] == pytest.approx(
            1.0 * 1024 ** 3 + snap["process_bytes"])
        assert 0.20 < snap["ratio"] < 0.21

    def test_warns_at_the_warn_ratio(self, monkeypatch, capsys):
        _seed_cycle(db_egress.QUOTA_BYTES * db_egress.CYCLE_WARN_RATIO)
        LEDGER.record("SELECT x FROM companies", 1, 14)
        assert "WARN cycle" in capsys.readouterr().err

    def test_stays_silent_below_the_warn_ratio(self, capsys):
        _seed_cycle(db_egress.QUOTA_BYTES * 0.5)
        LEDGER.record("SELECT x FROM companies", 1, 14)
        assert "cycle" not in capsys.readouterr().err

    def test_raises_at_the_block_ratio(self):
        """**100% ではなく 95% で止める。** 使い切ってから止めると復旧の余地が無い。"""
        _seed_cycle(db_egress.QUOTA_BYTES * db_egress.CYCLE_BLOCK_RATIO)
        with pytest.raises(EgressBudgetExceeded) as ei:
            LEDGER.record("SELECT x FROM companies", 1, 14)
        assert "cycle" in str(ei.value)

    def test_block_ratio_leaves_headroom(self):
        assert db_egress.CYCLE_WARN_RATIO < db_egress.CYCLE_BLOCK_RATIO < 1.0

    def test_message_names_the_escape_hatches(self):
        _seed_cycle(db_egress.QUOTA_BYTES)
        with pytest.raises(EgressBudgetExceeded) as ei:
            LEDGER.record("SELECT x FROM companies", 1, 14)
        assert "FINAPP_EGRESS_ENFORCE=0" in str(ei.value)
        assert "FINAPP_EGRESS_CYCLE=0" in str(ei.value)

    def test_enforce_zero_warns_but_does_not_raise(self, monkeypatch, capsys):
        monkeypatch.setenv("FINAPP_EGRESS_ENFORCE", "0")
        _seed_cycle(db_egress.QUOTA_BYTES)
        LEDGER.record("SELECT x FROM companies", 1, 14)      # 例外が出ないこと
        assert "OVER (enforce=0, continuing)" in capsys.readouterr().err

    def test_summary_line_reports_the_cycle(self):
        _seed_cycle(1.0 * 1024 ** 3)
        LEDGER.record("SELECT x FROM companies", 1, 14)
        line = summary_line()
        assert "cycle=" in line and "/5GB" in line
        assert line == line.encode("ascii", "replace").decode("ascii")   # cp932 対策


class TestCycleSelfAccounting:
    """サイクル台帳自身の読み書きを計上しない（無限再帰と自己汚染の両方を防ぐ）。"""

    def test_queries_inside_the_cycle_scope_are_not_recorded(self):
        with db_egress._cycle_io_scope():
            LEDGER.record("SELECT value FROM app_settings", 1, 3)
        assert LEDGER.snapshot()["calls"] == 0

    def test_scope_restores_the_previous_flag(self):
        assert db_egress._in_cycle_io() is False
        with db_egress._cycle_io_scope():
            assert db_egress._in_cycle_io() is True
            with db_egress._cycle_io_scope():
                assert db_egress._in_cycle_io() is True
            assert db_egress._in_cycle_io() is True      # 内側を抜けても戻らない
        assert db_egress._in_cycle_io() is False

    def test_recording_resumes_after_the_scope(self):
        with db_egress._cycle_io_scope():
            LEDGER.record("SELECT value FROM app_settings", 1, 3)
        LEDGER.record("SELECT x FROM companies", 7, 14)
        assert LEDGER.snapshot()["rows"] == 7


class TestCycleTrackingScope:
    """ミラー（ローカル）読取は Egress を払わないので累計に積まない（#481）。"""

    @pytest.fixture
    def outside_pytest(self, monkeypatch):
        """本番プロセスと同じ条件にする（pytest ガードを外す）。"""
        monkeypatch.setattr(db_egress, "_running_under_pytest", lambda: False)
        monkeypatch.delenv("FINAPP_EGRESS_CYCLE", raising=False)

    def test_disabled_for_local_connections(self, monkeypatch, outside_pytest):
        import database
        monkeypatch.setattr(database, "_is_local", True)
        db_egress._reset_for_tests()
        assert db_egress.cycle_tracking_enabled() is False

    def test_enabled_for_remote_connections(self, monkeypatch, outside_pytest):
        import database
        monkeypatch.setattr(database, "_is_local", False)
        db_egress._reset_for_tests()
        assert db_egress.cycle_tracking_enabled() is True

    def test_kill_switch_wins_over_connection_target(self, monkeypatch, outside_pytest):
        monkeypatch.setenv("FINAPP_EGRESS_CYCLE", "0")
        import database
        monkeypatch.setattr(database, "_is_local", False)
        db_egress._reset_for_tests()
        assert db_egress.cycle_tracking_enabled() is False

    def test_pytest_guard_wins_over_everything(self, monkeypatch):
        """**テストが本番 Supabase へ接続しないための最後の砦**（10分ハングの実例あり）。

        ローカルの pytest は `.env` の DATABASE_URL を読んだ本番向け engine を
        import する。ここが False を返さないと、`record()` を呼ぶ全テストが
        本番へ繋ぎ、atexit の `emit_summary` が本番へ書き込む。
        """
        monkeypatch.delenv("FINAPP_EGRESS_CYCLE", raising=False)
        import database
        monkeypatch.setattr(database, "_is_local", False)
        db_egress._reset_for_tests()
        assert db_egress._running_under_pytest() is True
        assert db_egress.cycle_tracking_enabled() is False

    def test_flush_is_a_no_op_when_disabled(self, monkeypatch):
        """無効時は DB を一切触らない（触ると SQLite の CI が落ちる）。"""
        monkeypatch.setattr(db_egress, "cycle_tracking_enabled", lambda: False)
        called = []
        monkeypatch.setattr(db_egress, "_cycle_io_scope", lambda: called.append(1))
        LEDGER.record("SELECT x FROM companies", 10, 14)
        db_egress._flush_cycle()
        assert called == []

    def test_flush_advances_the_marker_even_with_zero_bytes(self, monkeypatch):
        """**消費ゼロでも印は進める。**

        進めないと「消費ゼロ」と「計測が止まっている」を区別できず、`egress-health`
        の「記録がまだ無い」注記が毎日出て本当の異常が埋もれる（#438 と同型）。
        `check_egress_health` 自身が 1 行しか引かないので、これは実際に踏んだ。
        """
        monkeypatch.setattr(db_egress, "cycle_tracking_enabled", lambda: True)
        executed: list[str] = []

        class _FakeDB:
            def execute(self, stmt, params=None):
                executed.append(str(stmt))
                return type("R", (), {"fetchone": lambda self: None})()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        import database
        monkeypatch.setattr(database, "SessionLocal", lambda: _FakeDB())
        db_egress._flush_cycle()            # 1文も引いていない状態
        assert len(executed) == 2, "印の更新と累計の加算で2文のはず"
        assert all("app_settings" in sql for sql in executed)
