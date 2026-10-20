"""本番予測の月次の写し（prediction_snapshots.py・#1005）のテスト。

共通の `db` fixture（in-memory SQLite）だけを使い、本物の DB・ネットには触れない。
写しの元は全置換の表（`replace_macro_enet_scores`）と `financial_metrics`（SQLite では表）へ直接入れる。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import batch_freshness as bf  # noqa: E402
import database  # noqa: E402
import nightly_scores  # noqa: E402
import prediction_snapshots as PS  # noqa: E402
from database import FinancialMetric, PredictionSnapshot  # noqa: E402

TODAY = date(2026, 10, 21)
ROWS = {
    "macro_enet": [{"edinet_code": "E0001", "mu": 0.12, "r1_prime": 0.3, "gap_ratio": None,
                    "data_asof": "2026-10-20"},
                   {"edinet_code": "E0002", "mu": -0.05, "r1_prime": None, "gap_ratio": None,
                    "data_asof": "2026-10-20"}],
    "sector_ols": [{"edinet_code": "E0001", "mu": None, "r1_prime": None, "gap_ratio": 12.5,
                    "data_asof": "2026-03-31"}],
}


def fake_reader(db, source):
    return ROWS.get(source, [])


def run(db, today=TODAY, sources=("macro_enet", "sector_ols"), reader=fake_reader, **kw):
    kw.setdefault("is_local", True)
    return PS.snapshot_month(db, today=today, code_version="abc123", preprocess_version="v9",
                             sources=sources, reader=reader, **kw)


def stored(db, source=None):
    q = db.query(PredictionSnapshot)
    if source:
        q = q.filter(PredictionSnapshot.source == source)
    return q.order_by(PredictionSnapshot.source, PredictionSnapshot.edinet_code).all()


class TestSnapshotMonth:
    def test_writes_what_the_reader_returns(self, db):
        result = run(db)
        assert result["macro_enet"][0] == PS.STATUS_WROTE and result["sector_ols"][0] == PS.STATUS_WROTE
        got = [(r.source, r.edinet_code, r.mu, r.r1_prime, r.gap_ratio, r.data_asof, r.snapshot_date,
                r.code_version, r.preprocess_version) for r in stored(db)]
        assert got == [
            ("macro_enet", "E0001", 0.12, 0.3, None, "2026-10-20", "2026-10-21", "abc123", "v9"),
            ("macro_enet", "E0002", -0.05, None, None, "2026-10-20", "2026-10-21", "abc123", "v9"),
            ("sector_ols", "E0001", None, None, 12.5, "2026-03-31", "2026-10-21", "abc123", "v9"),
        ]
        assert PS.failed_sources(result) == []

    def test_second_run_in_the_same_month_does_not_write(self, db):
        run(db)
        result = run(db, today=date(2026, 10, 31))
        assert {s: st for s, (st, _) in result.items()} == {
            "macro_enet": PS.STATUS_EXISTS, "sector_ols": PS.STATUS_EXISTS}
        assert len(stored(db)) == 3

    def test_next_month_writes_again(self, db):
        run(db)
        run(db, today=date(2026, 11, 1))
        dates = sorted({r.snapshot_date for r in stored(db, "macro_enet")})
        assert dates == ["2026-10-21", "2026-11-01"]

    def test_missing_source_is_retried_while_others_exist(self, db):
        """1 晩目に空だった源は、翌晩に当月分として取り直される。"""
        run(db, reader=lambda db, s: ROWS["macro_enet"] if s == "macro_enet" else [])
        result = run(db, today=date(2026, 10, 22))
        assert result["macro_enet"][0] == PS.STATUS_EXISTS
        assert result["sector_ols"][0] == PS.STATUS_WROTE
        assert stored(db, "sector_ols")[0].snapshot_date == "2026-10-22"

    def test_empty_source_is_a_failure_and_writes_nothing(self, db):
        result = run(db, sources=("macro_dlm",))
        assert result["macro_dlm"][0] == PS.STATUS_EMPTY
        assert PS.failed_sources(result) == ["macro_dlm"]
        assert stored(db) == []

    def test_one_broken_source_does_not_stop_the_others(self, db):
        def reader(db, source):
            if source == "macro_enet":
                raise RuntimeError("壊れた")
            return fake_reader(db, source)

        result = run(db, reader=reader)
        assert result["macro_enet"][0] == PS.STATUS_ERROR and "壊れた" in result["macro_enet"][1]
        assert result["sector_ols"][0] == PS.STATUS_WROTE
        assert PS.failed_sources(result) == ["macro_enet"]

    def test_not_local_writes_nothing(self, db):
        result = run(db, is_local=False)
        assert {st for st, _ in result.values()} == {PS.STATUS_NOT_LOCAL}
        assert PS.failed_sources(result) == []
        assert stored(db) == []

    def test_dry_run_context_writes_nothing_and_fails_loudly(self, db):
        """`tuning_dry_run()` の内側では書かない＝行数の照合が合わず失敗として返る（黙って成功にしない）。"""
        with database.tuning_dry_run():
            result = run(db, sources=("macro_enet",))
        assert result["macro_enet"][0] == PS.STATUS_ERROR
        assert stored(db) == []


class TestReadSource:
    def test_mu_matches_the_replaced_table(self, db):
        """写した μ̂ が全置換の表（＝売りランキングが読む値）と一致する。"""
        database.replace_macro_enet_scores(
            db, [{"edinet_code": "E0001", "mu": 0.12, "r1_prime": 0.3},
                 {"edinet_code": "E0002", "mu": -0.05, "r1_prime": None}],
            snapshot_date="2026-10-20", snapshot_date_min="2026-10-13", n_stale=1)
        result = run(db, sources=("macro_enet",), reader=None)
        assert result["macro_enet"][0] == PS.STATUS_WROTE
        table = database.get_macro_enet_producer(db)
        assert len(table) == 2
        got = {r.edinet_code: (r.mu, r.r1_prime, r.data_asof) for r in stored(db, "macro_enet")}
        assert got == {ec: (v["mu"], v["r1_prime"], "2026-10-20") for ec, v in table.items()}

    def test_gap_takes_each_companys_latest_year(self, db):
        db.add_all([
            FinancialMetric(edinet_code="E0001", year=2024, period_end=date(2025, 3, 31), gap_ratio=5.0),
            FinancialMetric(edinet_code="E0001", year=2025, period_end=date(2026, 3, 31), gap_ratio=12.5),
            # 決算期変更で同じ年度に 2 本: 期末の新しい方
            FinancialMetric(edinet_code="E0002", year=2025, period_end=date(2025, 6, 30), gap_ratio=-1.0),
            FinancialMetric(edinet_code="E0002", year=2025, period_end=date(2025, 12, 31), gap_ratio=-3.0),
            # 最新年度の gap が無い社は写さない（古い年度の gap へ戻らない）
            FinancialMetric(edinet_code="E0003", year=2024, period_end=date(2025, 3, 31), gap_ratio=7.0),
            FinancialMetric(edinet_code="E0003", year=2025, period_end=date(2026, 3, 31), gap_ratio=None),
        ])
        db.commit()
        rows = PS.read_source(db, "sector_ols")
        assert [(r["edinet_code"], r["gap_ratio"], r["data_asof"], r["mu"]) for r in rows] == [
            ("E0001", 12.5, "2026-03-31", None), ("E0002", -3.0, "2025-12-31", None)]


def test_sources_cover_every_live_mu_source():
    """写す源は永続化している全ての mu_source と sector_ols。退役した M-4 は入れない。"""
    assert set(PS.MU_SOURCES) == set(database._PRODUCER_SCORE_MODELS) - {"macro_ensemble"} | {"macro_risk_return"}
    assert PS.GAP_SOURCE in PS.SOURCES


def test_jst_today_uses_the_japanese_date():
    assert PS.jst_today(datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)) == date(2026, 11, 1)


class TestNightlyHook:
    def test_failed_labels(self):
        snap = {"macro_enet": (PS.STATUS_WROTE, ""), "macro_dlm": (PS.STATUS_EMPTY, ""),
                "sector_ols": (PS.STATUS_EXISTS, "")}
        assert nightly_scores.snapshot_failed_labels(snap) == ["prediction_snapshots:macro_dlm"]

    def test_unexpected_error_is_counted(self, db, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("表を作れない")

        monkeypatch.setattr(PS, "snapshot_month", boom)
        snap = nightly_scores.snapshot_predictions(db, code_version="x")
        assert nightly_scores.snapshot_failed_labels(snap) == ["prediction_snapshots:*"]


class TestFreshness:
    def _add(self, db, source, created_at):
        db.add(PredictionSnapshot(snapshot_date="2026-10-21", source=source, edinet_code="E0001",
                                  mu=0.1, created_at=created_at))
        db.commit()

    def test_missing_source_reads_as_none(self, db):
        self._add(db, "macro_enet", datetime(2026, 10, 21))
        assert bf._prediction_snapshots_at(db) is None

    def test_oldest_of_the_latest_per_source(self, db):
        for i, s in enumerate(PS.SOURCES):
            self._add(db, s, datetime(2026, 10, 1 + i))
        assert bf._prediction_snapshots_at(db) == datetime(2026, 10, 1)

    def test_registered_as_a_monthly_producer(self):
        p = next(p for p in bf.PRODUCERS if p.read is bf._prediction_snapshots_at)
        assert p.cadence_h == 31 * 24.0
