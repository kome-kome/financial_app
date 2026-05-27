"""plugins/sector_ols.py のユニットテスト。

純粋: 特徴量オプション定数の次元整合性（CLAUDE.md 制約の回帰防止）。
execute(): 空DB・サンプル不足→ValueError / 予測値とランクの書き込み / カンマ文字列パース。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.sector_ols import (
    DEFAULT_FEATURES_MKTCAP,
    DEFAULT_FEATURES_PRICE,
    FEATURE_OPTIONS,
    FEATURE_OPTIONS_ABS,
    FEATURE_OPTIONS_PER_SHARE,
    plugin,
)

_ABS_VALUES = {o["value"] for o in FEATURE_OPTIONS_ABS}
_PER_SHARE_VALUES = {o["value"] for o in FEATURE_OPTIONS_PER_SHARE}


# ── 純粋: 特徴量定数の次元整合性 ─────────────────────────────────────────────

class TestConstants:
    def test_mktcap_defaults_are_absolute(self):
        # market_cap[百万円] ターゲットの説明変数は絶対額 [円] のみ
        assert set(DEFAULT_FEATURES_MKTCAP) <= _ABS_VALUES

    def test_price_defaults_are_per_share(self):
        # stock_price[円/株] ターゲットの説明変数は per-share [円/株] のみ
        assert set(DEFAULT_FEATURES_PRICE) <= _PER_SHARE_VALUES

    def test_feature_options_is_union(self):
        assert FEATURE_OPTIONS == FEATURE_OPTIONS_ABS + FEATURE_OPTIONS_PER_SHARE

    def test_abs_and_per_share_disjoint(self):
        assert _ABS_VALUES.isdisjoint(_PER_SHARE_VALUES)


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

def _seed_sector(db, make_fin, n=12, industry="情報・通信業"):
    """OLS が回る程度に分散のある（共線でない）n 社を 1 業種に投入。"""
    recs = []
    for i in range(1, n + 1):
        recs.append(make_fin(
            edinet_code=f"E{i:05d}", industry=industry,
            pl_revenue=1.0e9 + 1.0e8 * i,
            pl_operating_profit=1.0e8 + 2.0e7 * i + (i % 3) * 5.0e6,
            pl_net_income=5.0e7 + 1.0e7 * i - (i % 2) * 3.0e6,
            bs_total_equity=8.0e8 + 5.0e7 * i + (i % 4) * 1.0e7,
            cf_operating_cf=1.2e8 + 1.5e7 * i + (i % 5) * 2.0e6,
            market_cap=1000.0 + 200.0 * i + (i % 3) * 50.0,
        ))
    db.add_all(recs)
    db.commit()


class TestExecute:
    def test_empty_db_raises(self, db):
        with pytest.raises(ValueError):
            asyncio.run(plugin.execute({}, db))

    def test_insufficient_samples_raises(self, db, make_fin):
        _seed_sector(db, make_fin, n=3)  # default min_samples=10 未満
        with pytest.raises(ValueError):
            asyncio.run(plugin.execute({}, db))

    def test_writes_predictions_and_ranks(self, db, make_fin):
        from database import FinancialRecord
        _seed_sector(db, make_fin, n=12)
        res = asyncio.run(plugin.execute({}, db))
        assert res["n_sectors"] >= 1
        assert res["sector_stats"][0]["r2"] is not None
        # 予測値・乖離率が各レコードに書き込まれていること
        rows = db.query(FinancialRecord).all()
        assert all(r.predicted_market_cap is not None for r in rows)
        assert all(r.gap_ratio is not None for r in rows)
        # 業種内ランクが全件付与されていること
        assert all(p["sector_rank"] is not None for p in res["results"])

    def test_features_comma_string(self, db, make_fin):
        _seed_sector(db, make_fin, n=12)
        res = asyncio.run(plugin.execute(
            {"features": "pl_revenue,bs_total_equity,cf_operating_cf"}, db))
        assert res["n_sectors"] >= 1
