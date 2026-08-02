"""tests/test_asof_freshness.py — producer as-of 代表値（#417）と株価鮮度（#416）。

#417: μ̂ は銘柄ごとに「その銘柄の最終週次バー」時点で計算されるため、代表値を max に
潰すと最新の 1〜2 銘柄が全体の as-of を名乗る（実測 2026-08-02: max=07-31 は 2 銘柄・
3,677 銘柄は 07-13）。代表値が median であること・最古と古い銘柄数を持つことを検証。

#416: 株価鮮度は p50 で判定する（max だけ新しいケースで警告が消えないこと）。
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (  # noqa: E402
    MacroGbdtScore, MacroEnetScore, StockPriceDaily,
    business_days_between, get_producer_asof, price_asof_by_code, price_freshness,
    replace_macro_gbdt_scores, replace_macro_enet_scores,
    PRICE_STALE_WARN_BDAYS, PRICE_STALE_ALERT_BDAYS,
)
from plugins.macro_snapshots import representative_snapshot_date, to_date_str  # noqa: E402


class TestRepresentativeSnapshotDate:
    def test_median_not_max(self):
        """実測形（大多数が古く 2 銘柄だけ新しい）で代表値が max にならない。"""
        snaps = ["2026-07-13"] * 8 + ["2026-07-31"] * 2
        out = representative_snapshot_date(snaps)
        assert out["snapshot_date"] == "2026-07-13"      # max（07-31）ではない
        assert out["snapshot_date_min"] == "2026-07-13"
        assert out["snapshot_date_max"] == "2026-07-31"
        assert out["n_stale"] == 0                       # 代表値より古い銘柄は無い

    def test_n_stale_counts_older_than_representative(self):
        snaps = ["2026-06-01", "2026-07-01", "2026-07-13", "2026-07-13", "2026-07-31"]
        out = representative_snapshot_date(snaps)
        assert out["snapshot_date"] == "2026-07-13"
        assert out["snapshot_date_min"] == "2026-06-01"
        assert out["n_stale"] == 2                        # 06-01 と 07-01

    def test_even_count_takes_older_side(self):
        """偶数個は古い側（lower median）＝保守側へ寄せる。"""
        out = representative_snapshot_date(["2026-07-01", "2026-07-31"])
        assert out["snapshot_date"] == "2026-07-01"

    def test_accepts_date_objects_and_generators(self):
        out = representative_snapshot_date(d for d in [date(2026, 7, 13), date(2026, 7, 31)])
        assert out["snapshot_date"] == "2026-07-13"

    def test_empty_and_all_none(self):
        for src in ([], [None, None, ""]):
            out = representative_snapshot_date(src)
            assert out == {"snapshot_date": None, "snapshot_date_min": None,
                           "snapshot_date_max": None, "n_stale": 0}

    def test_to_date_str(self):
        assert to_date_str(date(2026, 7, 13)) == "2026-07-13"
        assert to_date_str("2026-07-13T00:00:00") == "2026-07-13"
        assert to_date_str(None) is None


class TestProducerAsofPersistence:
    def test_gbdt_scores_persist_asof_triplet(self, db):
        asof = representative_snapshot_date(["2026-07-13"] * 3 + ["2026-07-31"])
        replace_macro_gbdt_scores(
            db, [{"edinet_code": "E001", "mu": 0.1, "r1_prime": 0.2}],
            asof["snapshot_date"], snapshot_date_min=asof["snapshot_date_min"],
            n_stale=asof["n_stale"])
        row = db.query(MacroGbdtScore).one()
        assert row.snapshot_date == "2026-07-13"
        assert row.snapshot_date_min == "2026-07-13"
        assert row.n_stale == 0

    def test_get_producer_asof_roundtrip(self, db):
        replace_macro_enet_scores(
            db, [{"edinet_code": "E001", "mu": 0.1, "r1_prime": None}],
            "2026-07-13", snapshot_date_min="2026-06-01", n_stale=7)
        assert get_producer_asof(db, "macro_enet") == {
            "snapshot_date": "2026-07-13", "snapshot_date_min": "2026-06-01", "n_stale": 7}

    def test_get_producer_asof_graceful_empty(self, db):
        assert get_producer_asof(db, "macro_gbdt") is None
        assert get_producer_asof(db, "unknown_plugin") is None

    def test_m1_returns_none_because_meta_date_is_run_date(self, db):
        """M-1 の macro_beta_meta.snapshot_date は推論バッチの実行日でデータ as-of ではない。

        そのまま返すと株価が止まっていても「今日」と表示され #417 と同型の嘘になるため、
        意図的に None を返す。
        """
        from database import upsert_macro_beta
        upsert_macro_beta(db, {"run_id": "r1", "snapshot_date": date.today().isoformat(),
                               "selected_factors": ["f1"], "factor_cov": [[1.0]],
                               "hyperparams": {}},
                          [{"run_id": "r1", "edinet_code": "E001", "factor_name": "f1",
                            "loading_mean": 1.0, "loading_se": 0.1}])
        assert get_producer_asof(db, "macro_risk_return") is None


class TestBusinessDaysBetween:
    @pytest.mark.parametrize("d0,d1,expected", [
        (date(2026, 7, 31), date(2026, 8, 3), 1),     # 金 → 月
        (date(2026, 7, 27), date(2026, 8, 3), 5),     # 月 → 翌月曜
        (date(2026, 7, 13), date(2026, 8, 3), 15),    # 実測の 19 日停止ケース
        (date(2026, 8, 3), date(2026, 8, 3), 0),
        (date(2026, 8, 4), date(2026, 8, 3), 0),      # 未来日は 0
    ])
    def test_counts_weekdays_only(self, d0, d1, expected):
        assert business_days_between(d0, d1) == expected


class TestPriceFreshness:
    @staticmethod
    def _seed(db, per_code_last: dict):
        for ec, last in per_code_last.items():
            db.add(StockPriceDaily(edinet_code=ec, trade_date=last, close=100.0))
        db.commit()

    def test_empty(self, db):
        out = price_freshness(db)
        assert out["level"] == "empty" and out["price_asof_p50"] is None

    def test_p50_drives_level_not_max(self, db):
        """max だけ新しい実測形で、判定が p50 基準になる（バッジが赤のまま）。"""
        today = date.today()
        old = (today - timedelta(days=25)).isoformat()
        new = today.isoformat()
        self._seed(db, {f"E{i:03d}": old for i in range(20)} | {"E900": new, "E901": new})
        out = price_freshness(db)
        assert out["price_asof_p50"] == old
        assert out["price_asof_max"] == new            # max は参考値として返る
        assert out["level"] == "alert"                  # p50 基準なので赤のまま
        assert out["stale_bdays"] > PRICE_STALE_ALERT_BDAYS
        assert out["n_stale_over_5d"] == 20             # 古い 20 社だけ

    def test_fresh_when_all_recent(self, db):
        self._seed(db, {"E001": date.today().isoformat(), "E002": date.today().isoformat()})
        out = price_freshness(db)
        assert out["level"] == "fresh" and out["n_stale_over_5d"] == 0

    def test_warn_between_thresholds(self, db):
        """5営業日超〜10営業日以下は黄（warn）。"""
        d = date.today()
        n = 0
        while business_days_between(d, date.today()) <= PRICE_STALE_WARN_BDAYS:
            n += 1
            d = date.today() - timedelta(days=n)
        assert business_days_between(d, date.today()) <= PRICE_STALE_ALERT_BDAYS
        self._seed(db, {"E001": d.isoformat()})
        assert price_freshness(db)["level"] == "warn"

    def test_aggregated_and_python_paths_agree(self, db):
        """DB 側集約（/api/stats 用）と dict 集計（recommend 用）が一致する。

        経路が 2 本あるのは Egress 削減のため（集約側は全銘柄行を転送しない）。
        片方だけ直すと表示が食い違うので、一致を回帰テストで固定する。
        """
        today = date.today()
        per_code = {f"E{i:03d}": (today - timedelta(days=i * 3)).isoformat()
                    for i in range(25)}
        self._seed(db, per_code)
        assert price_freshness(db) == price_freshness(db, price_asof_by_code(db))

    def test_stale_cutoff_boundary(self, db):
        """cutoff は「WARN 営業日以内に収まる最古の日」＝これより古い日だけ stale。"""
        from database import stale_cutoff_date
        today = date.today()
        cutoff = date.fromisoformat(stale_cutoff_date(today, PRICE_STALE_WARN_BDAYS))
        assert business_days_between(cutoff, today) <= PRICE_STALE_WARN_BDAYS
        assert business_days_between(cutoff - timedelta(days=1), today) > PRICE_STALE_WARN_BDAYS

    def test_price_asof_by_code_takes_max_per_code(self, db):
        db.add_all([
            StockPriceDaily(edinet_code="E001", trade_date="2026-07-13", close=1.0),
            StockPriceDaily(edinet_code="E001", trade_date="2026-07-31", close=2.0),
            StockPriceDaily(edinet_code="E002", trade_date="2026-07-13", close=3.0),
        ])
        db.commit()
        assert price_asof_by_code(db) == {"E001": "2026-07-31", "E002": "2026-07-13"}
