"""本番予測の月次の写し（prediction_snapshots.py・#1005）のテスト。

共通の `db` fixture（in-memory SQLite）だけを使い、本物の DB・ネットには触れない。
写しの元は全置換の表（`replace_macro_enet_scores`）と `financial_metrics`（SQLite では表）へ直接入れる。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

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

    def test_all_empty_values_are_an_error(self, db):
        """社の行があるのに μ̂ が全部空＝読み方の誤り。書かずに失敗にする（#1005 の初回の M-1）。"""
        rows = [dict(r, mu=None) for r in ROWS["macro_enet"]]
        result = run(db, sources=("macro_enet",), reader=lambda db, s: rows)
        assert result["macro_enet"][0] == PS.STATUS_ERROR and "全部空" in result["macro_enet"][1]
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

    def test_mu_sources_get_the_current_macro_snapshot(self, db, monkeypatch):
        """M-1 は当日のマクロ値が無いと μ̂ を返さない。売りランキングと同じ値を渡す。"""
        import plugins
        import plugins.utils as U

        seen = []

        class FakeM1:
            def read_producer_scores(self, db, macro_snapshot=None):
                seen.append(macro_snapshot)
                if macro_snapshot is None:
                    return {"E0001": {"r_macro": 0.2}}
                return {"E0001": {"mu": 0.07, "r_macro": 0.2, "r1_prime": 0.01}}

        monkeypatch.setattr(plugins, "get_plugin", lambda name: FakeM1())
        monkeypatch.setattr(U, "current_macro_snapshot", lambda db: {"FACTOR": 1.5})
        rows = PS.read_source(db, "macro_risk_return")
        assert seen == [{"FACTOR": 1.5}]
        assert [(r["edinet_code"], r["mu"], r["r1_prime"]) for r in rows] == [("E0001", 0.07, 0.01)]

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


class TestReadS2:
    """売り専用の候補 S2（#1208）。売りの並びと同じ入口で読み、M-6 と断面の日付がそろう夜だけ写す。"""

    def _m6(self, db, snapshot_date="2026-10-20"):
        database.replace_macro_enet_scores(
            db, [{"edinet_code": "E0001", "mu": 0.12, "r1_prime": 0.3},
                 {"edinet_code": "E0002", "mu": -0.05, "r1_prime": None}],
            snapshot_date=snapshot_date, snapshot_date_min="2026-10-13", n_stale=1)

    def _s2(self, db, rows=None, snapshot_date="2026-10-20"):
        rows = [{"edinet_code": "E0001", "q20": -0.21}, {"edinet_code": "E0002", "q20": -0.35}] \
            if rows is None else rows
        database.replace_sell_quantile_scores(db, rows, snapshot_date=snapshot_date,
                                              snapshot_date_min="2026-10-13", n_stale=1)

    def test_q20_is_written_to_mu_with_the_s2_asof(self, db):
        self._m6(db)
        self._s2(db)
        result = run(db, sources=(PS.S2_SOURCE,), reader=None)
        assert result[PS.S2_SOURCE][0] == PS.STATUS_WROTE
        got = [(r.edinet_code, r.mu, r.r1_prime, r.gap_ratio, r.data_asof) for r in stored(db, PS.S2_SOURCE)]
        assert got == [("E0001", -0.21, None, None, "2026-10-20"), ("E0002", -0.35, None, None, "2026-10-20")]
        assert database.get_sell_quantile_scores(db)["q20"] == {"E0001": -0.21, "E0002": -0.35}

    def test_empty_table_is_skipped_not_failed(self, db):
        """予測が全社同値の夜は S2 の表が空（#1204）。失敗にせず見送り、夜間を非ゼロ終了にしない。"""
        self._m6(db)
        self._s2(db, rows=[])
        result = run(db, sources=(PS.S2_SOURCE,), reader=None)
        assert result[PS.S2_SOURCE][0] == PS.STATUS_SKIPPED
        assert PS.failed_sources(result) == []
        assert nightly_scores.snapshot_failed_labels(result) == []
        assert stored(db, PS.S2_SOURCE) == []

    def test_skipped_month_is_retried_the_next_night(self, db):
        """見送った月は当月分が無いままなので、S2 が出た夜に写る。"""
        self._m6(db)
        self._s2(db, rows=[])
        assert run(db, sources=(PS.S2_SOURCE,), reader=None)[PS.S2_SOURCE][0] == PS.STATUS_SKIPPED
        self._s2(db)
        assert run(db, sources=(PS.S2_SOURCE,), reader=None)[PS.S2_SOURCE][0] == PS.STATUS_WROTE
        assert len(stored(db, PS.S2_SOURCE)) == 2

    def test_other_empty_sources_still_fail(self, db):
        """空を見送るのは S2 だけ。μ̂ の源が空なのは読み出しの失敗かもしれないので、失敗のまま。"""
        result = run(db, sources=("macro_enet",), reader=None)
        assert result["macro_enet"][0] == PS.STATUS_EMPTY
        assert PS.EMPTY_IS_NORMAL == (PS.S2_SOURCE,)

    def test_stale_s2_is_not_written_and_other_sources_are(self, db):
        """S2 だけ古い断面は売りの並びが M-6 へ倒すので写さない。M-6 は写す。"""
        self._m6(db, "2026-10-20")
        self._s2(db, snapshot_date="2026-10-19")
        result = run(db, sources=("macro_enet", PS.S2_SOURCE), reader=None)
        assert result["macro_enet"][0] == PS.STATUS_WROTE
        assert result[PS.S2_SOURCE][0] == PS.STATUS_ERROR
        assert "2026-10-19" in result[PS.S2_SOURCE][1] and "2026-10-20" in result[PS.S2_SOURCE][1]
        assert stored(db, PS.S2_SOURCE) == [] and len(stored(db, "macro_enet")) == 2

    def test_missing_m6_asof_is_a_failure(self, db):
        self._s2(db)
        result = run(db, sources=(PS.S2_SOURCE,), reader=None)
        assert result[PS.S2_SOURCE][0] == PS.STATUS_ERROR
        assert stored(db, PS.S2_SOURCE) == []

    def test_name_matches_the_sell_ranking_key(self):
        from plugins.sell_ranking import MU_SOURCE, SELL_KEY_S2

        assert PS.S2_SOURCE == SELL_KEY_S2
        assert PS.S2_BASE == MU_SOURCE
        assert PS.value_column(PS.S2_SOURCE) == "mu"


def test_sources_cover_every_live_mu_source():
    """写す源は永続化している全ての mu_source と sector_ols と S2。退役した M-4 は入れない。"""
    assert set(PS.MU_SOURCES) == set(database._PRODUCER_SCORE_MODELS) - {"macro_ensemble"} | {"macro_risk_return"}
    assert PS.GAP_SOURCE in PS.SOURCES
    assert PS.S2_SOURCE in PS.SOURCES and PS.S2_SOURCE not in PS.MU_SOURCES


class TestCurrentMacroSnapshot:
    """`plugins.utils.current_macro_snapshot`（売りランキング・買い推奨・写しが共有する）。"""

    def test_selected_factors_only_and_drops_missing(self, monkeypatch):
        import plugins.utils as U

        calls = []
        monkeypatch.setattr(database, "get_macro_beta",
                            lambda db, with_loadings=True: ({"selected_factors": ["A", "B"]}, None))

        def features(db, ref_date, names):
            calls.append((ref_date, names))
            return {"A": 1.0, "B": None}

        monkeypatch.setattr(U, "get_macro_features", features)
        assert U.current_macro_snapshot(None, "2026-10-21") == {"A": 1.0}
        assert calls == [("2026-10-21", ["A", "B"])]

    def test_none_without_factors_or_values(self, monkeypatch):
        import plugins.utils as U

        monkeypatch.setattr(database, "get_macro_beta", lambda db, with_loadings=True: (None, None))
        assert U.current_macro_snapshot(None) is None
        monkeypatch.setattr(database, "get_macro_beta",
                            lambda db, with_loadings=True: ({"selected_factors": ["A"]}, None))
        monkeypatch.setattr(U, "get_macro_features", lambda db, d, names: {"A": None})
        assert U.current_macro_snapshot(None) is None


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

    def test_s2_is_not_watched(self, db):
        """S2 は仕様どおり空の月がある（#1204）。写しが無くても古くても、監視は鳴らさない（#1208）。"""
        assert PS.S2_SOURCE not in PS.WATCHED_SOURCES
        assert set(PS.WATCHED_SOURCES) == set(PS.SOURCES) - {PS.S2_SOURCE}
        for s in PS.WATCHED_SOURCES:
            self._add(db, s, datetime(2026, 10, 21))
        assert bf._prediction_snapshots_at(db) == datetime(2026, 10, 21)      # S2 の写しが無い
        self._add(db, PS.S2_SOURCE, datetime(2026, 7, 1))
        assert bf._prediction_snapshots_at(db) == datetime(2026, 10, 21)      # S2 だけ古い

    def test_registered_as_a_monthly_producer(self):
        p = next(p for p in bf.PRODUCERS if p.read is bf._prediction_snapshots_at)
        assert p.cadence_h == 31 * 24.0
