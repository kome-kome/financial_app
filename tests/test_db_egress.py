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

import pytest
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
    """台帳・上書き予算・関連環境変数を毎回まっさらにする。"""
    for var in ("FINAPP_EGRESS_ROW_LIMIT", "FINAPP_EGRESS_MB_LIMIT",
                "FINAPP_EGRESS_ENFORCE", "FINAPP_EGRESS_LEDGER", "FINAPP_JOB"):
        monkeypatch.delenv(var, raising=False)
    db_egress._reset_for_tests()
    yield
    db_egress._reset_for_tests()


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
        assert bytes_per_row("stock_price_weekly", 3) == 32.1
        assert bytes_per_row("stock_price_weekly", 4) == 42.0

    def test_falls_back_to_per_column_rate_of_same_table(self):
        """実測に無い列数でも、同じテーブルの列単価（実測由来）を使う。"""
        rate = EGRESS_BYTES_PER_COLUMN["financial_metrics"].bytes_per_row
        assert bytes_per_row("financial_metrics", 36) == pytest.approx(rate * 36)

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
