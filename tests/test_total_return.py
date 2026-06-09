"""plugins/total_return.py のユニットテスト。

純粋: MECE 特徴量グループの整合性。
execute(): サンプル不足・空DB→ValueError / ランキング構造 / 配当利回りフィルタ。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
from plugins.total_return import (
    ALL_MECE_FEATURES,
    BS_FEATURES,
    CF_FEATURES,
    DIV_FEATURES,
    FEATURE_LABELS,
    NULLABLE_AS_ZERO,
    PL_FEATURES,
    plugin,
)


# ── 純粋: MECE 定数 ──────────────────────────────────────────────────────────

class TestConstants:
    def test_mece_union(self):
        assert ALL_MECE_FEATURES == PL_FEATURES + CF_FEATURES + BS_FEATURES + DIV_FEATURES

    def test_nullable_subset_of_features(self):
        assert NULLABLE_AS_ZERO <= set(ALL_MECE_FEATURES)

    def test_labels_cover_all_features(self):
        assert set(FEATURE_LABELS) >= set(ALL_MECE_FEATURES)


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

def _seed(db, make_fin, n=24):
    """stock_price と per-share 特徴量が揃った n 社（3 業種）を投入。"""
    inds = ["情報・通信業", "電気機器", "小売業"]
    recs = []
    for i in range(n):
        eps = 50.0 + 3.0 * i
        bps = 800.0 + 20.0 * i
        recs.append(make_fin(
            edinet_code=f"E{i:05d}", industry=inds[i % 3],
            pl_eps=eps, bs_bps=bps,
            bs_total_equity=1.0e9 + 5.0e7 * i,
            cf_operating_cf=1.0e8 + 3.0e6 * i,
            dps=10.0 + 0.5 * i,
            div_yield=4.0 if i % 2 == 0 else 1.0,
            stock_price=10.0 * eps + 1.0 * bps + (i % 5) * 10.0,
        ))
    db.add_all(recs)
    db.commit()


class TestExecute:
    def test_empty_db_raises(self, db):
        with pytest.raises(ValueError):
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_insufficient_records_raises(self, db, make_fin):
        _seed(db, make_fin, n=5)  # 20 件未満
        with pytest.raises(ValueError):
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_ranking_and_structure(self, db, make_fin):
        _seed(db, make_fin, n=24)
        res = asyncio.run(execute_plugin(plugin, {}, db))
        # 構造
        assert "cv_metrics" in res and "mean_r2" in res["cv_metrics"]
        assert "pl_eps" in res["feature_weights"]
        assert "bs_bps" in res["feature_weights"]
        assert set(res["sector_fixed_effects"]) >= {"enabled", "baseline", "effects", "n_dummies"}
        assert res["n_total_samples"] == 24
        # ランキングは total_return_pct 降順・rank 連番
        ranking = res["ranking"]
        assert len(ranking) > 0
        trs = [r["total_return_pct"] for r in ranking]
        assert trs == sorted(trs, reverse=True)
        assert [r["rank"] for r in ranking] == list(range(1, len(ranking) + 1))

    def test_min_div_yield_filter(self, db, make_fin):
        _seed(db, make_fin, n=24)
        res = asyncio.run(execute_plugin(plugin, {"min_div_yield": 3.0}, db))
        # 返ってきた銘柄はすべて配当利回り >= 3.0
        assert all(r["div_yield_pct"] >= 3.0 for r in res["ranking"])
