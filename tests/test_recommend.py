"""plugins/recommend.py のユニットテスト。

純粋: METRICS / PRESETS の整合性。
execute(): 重み付きスコアのランキング・カバレッジフィルタ・空DB・top_n（in-memory SQLite）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.recommend import METRICS, PRESETS, plugin


# ── 純粋: 定数の整合性 ───────────────────────────────────────────────────────

class TestConstants:
    def test_metrics_unique_and_nonempty(self):
        assert len(METRICS) == 8
        assert len(set(METRICS)) == len(METRICS)

    def test_presets_reference_valid_metrics(self):
        # 各プリセットのウェイトキーは必ず METRICS に存在すること
        for name, weights in PRESETS.items():
            for metric in weights:
                assert metric in METRICS, f"{name} の {metric} が METRICS に無い"

    def test_presets_weights_are_numeric(self):
        for weights in PRESETS.values():
            for w in weights.values():
                assert isinstance(w, (int, float))


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

class TestExecute:
    def test_ranking_orders_by_weighted_score(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", z_roe=3.0),
            make_metric(edinet_code="E00002", z_roe=1.0),
            make_metric(edinet_code="E00003", z_roe=-1.0),
        ])
        db.commit()
        res = asyncio.run(plugin.execute(
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["count"] == 3
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00001", "E00002", "E00003"]
        scores = [r["score"] for r in res["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_min_coverage_skips_low_coverage(self, db, make_metric):
        db.add_all([
            make_metric(edinet_code="E00001", z_roe=2.0, z_op_margin=1.0),  # coverage 1.0
            make_metric(edinet_code="E00002", z_roe=2.0, z_op_margin=None),  # coverage 0.5
        ])
        db.commit()
        res = asyncio.run(plugin.execute(
            {"weights": {"z_roe": 1.0, "z_op_margin": 1.0}, "min_coverage": 0.75}, db))
        assert res["count"] == 1
        assert res["skipped_low_coverage"] == 1
        assert res["results"][0]["edinet_code"] == "E00001"

    def test_zero_weights_returns_empty(self, db):
        res = asyncio.run(plugin.execute({"weights": {"z_roe": 0.0}}, db))
        assert res["count"] == 0
        assert res["total_candidates"] == 0

    def test_empty_db_returns_empty(self, db):
        res = asyncio.run(plugin.execute({"weights": {"z_roe": 1.0}}, db))
        assert res["count"] == 0
        assert res["total_candidates"] == 0

    def test_top_n_limits_results(self, db, make_metric):
        db.add_all([make_metric(edinet_code=f"E{i:05d}", z_roe=float(i)) for i in range(1, 6)])
        db.commit()
        res = asyncio.run(plugin.execute(
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0, "top_n": 2}, db))
        assert len(res["results"]) == 2
        assert res["total_candidates"] == 5

    def test_only_latest_year_per_company(self, db, make_metric):
        # 同一企業の複数年は最新年のみ対象（max-year subquery）
        db.add_all([
            make_metric(edinet_code="E00001", year=2021, period_end="2021-03-31", z_roe=9.0),
            make_metric(edinet_code="E00001", year=2023, period_end="2023-03-31", z_roe=1.0),
        ])
        db.commit()
        res = asyncio.run(plugin.execute(
            {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))
        assert res["count"] == 1
        assert res["results"][0]["year"] == 2023
