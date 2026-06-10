"""plugins/sector_ols.py のユニットテスト。

純粋: 特徴量オプション定数の次元整合性（CLAUDE.md 制約の回帰防止）。
execute(): 空DB・サンプル不足→ValueError / 予測値とランクの書き込み / カンマ文字列パース。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
from plugins.sector_ols import (
    DB_PER_SHARE_KEYS,
    DEFAULT_FEATURES_PRICE,
    FEATURE_OPTIONS,
    FEATURE_OPTIONS_PER_SHARE,
    PER_SHARE_DERIVED,
    plugin,
)

_PER_SHARE_VALUES = {o["value"] for o in FEATURE_OPTIONS_PER_SHARE}


# ── 純粋: 特徴量定数の次元整合性 ─────────────────────────────────────────────

class TestConstants:
    def test_price_defaults_are_per_share(self):
        # stock_price[円/株] ターゲットの説明変数は per-share [円/株] のみ
        assert set(DEFAULT_FEATURES_PRICE) <= _PER_SHARE_VALUES

    def test_default_features_count(self):
        # デフォルト10項目（PL/BS/CF網羅）
        assert len(DEFAULT_FEATURES_PRICE) == 10

    def test_feature_options_all_per_share(self):
        # FEATURE_OPTIONS は per-share 統一後の互換参照
        assert FEATURE_OPTIONS == FEATURE_OPTIONS_PER_SHARE

    def test_per_share_derived_keys_in_options(self):
        # PER_SHARE_DERIVED の全キーが FEATURE_OPTIONS_PER_SHARE に登録されている
        for key in PER_SHARE_DERIVED:
            assert key in _PER_SHARE_VALUES, f"{key} not in FEATURE_OPTIONS_PER_SHARE"

    def test_db_per_share_keys_in_options(self):
        # DB永続 per-share (pl_eps/bs_bps/dps) も FEATURE_OPTIONS に登録されている
        for key in DB_PER_SHARE_KEYS:
            assert key in _PER_SHARE_VALUES, f"{key} not in FEATURE_OPTIONS_PER_SHARE"

    def test_derived_and_db_disjoint(self):
        # 派生キーと DB永続キーは重複しない（命名規則の整合性）
        assert set(PER_SHARE_DERIVED.keys()).isdisjoint(DB_PER_SHARE_KEYS)

    def test_c2_features_wired(self):
        # C2 収集列が per-share 派生として結線されていること（収集済み→分析未活用 の回帰防止）
        c2_map = {
            "ps_rd_expenses":              "pl_rd_expenses",
            "ps_depreciation":             "pl_depreciation",
            "ps_extraordinary_income":     "pl_extraordinary_income",
            "ps_extraordinary_loss":       "pl_extraordinary_loss",
            "ps_pretax_profit":            "pl_pretax_profit",
            "ps_ppe_total":                "bs_ppe_total",
            "ps_investments_other_assets": "bs_investments_other_assets",
        }
        for ps_key, src_col in c2_map.items():
            assert PER_SHARE_DERIVED.get(ps_key) == src_col, f"{ps_key} 未結線"
            assert ps_key in _PER_SHARE_VALUES, f"{ps_key} not in FEATURE_OPTIONS"

    def test_c2_features_not_in_defaults(self):
        # 欠損縮小（全特徴量 non-null 要件）と VIF 回避のため、C2 列はデフォルト非選択
        c2_keys = {
            "ps_rd_expenses", "ps_depreciation", "ps_extraordinary_income",
            "ps_extraordinary_loss", "ps_pretax_profit", "ps_ppe_total",
            "ps_investments_other_assets",
        }
        assert c2_keys.isdisjoint(DEFAULT_FEATURES_PRICE)

    def test_plugin_is_heavy(self):
        # 重い回帰は Render 軽量モードでブロックするため heavy フラグを持つ
        assert plugin.heavy is True
        assert plugin.to_meta()["heavy"] is True


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

def _seed_sector(db, make_fin, n=12, industry="情報・通信業"):
    """OLS が回る程度に分散のある（共線でない）n 社を 1 業種に投入。

    stock_price[円/株] target 固定方式のため bs_bps と stock_price を必須 seed。
    デフォルト10項目（pl_eps, bs_bps, dps, ps_revenue, ps_gross_profit,
    ps_operating_profit, ps_total_assets, ps_total_liabilities,
    ps_operating_cf, ps_free_cf）の元となる絶対額カラムを全て埋める。
    """
    recs = []
    for i in range(1, n + 1):
        bps_val = 800.0 + 50.0 * i + (i % 4) * 10.0  # 円/株
        equity_val = bps_val * (1.0e6 + 5.0e4 * i)   # 株数 1〜2 百万株想定
        recs.append(make_fin(
            edinet_code=f"E{i:05d}", industry=industry,
            pl_revenue=1.0e9 + 1.0e8 * i,
            pl_gross_profit=4.0e8 + 5.0e7 * i + (i % 3) * 1.0e7,
            pl_operating_profit=1.0e8 + 2.0e7 * i + (i % 3) * 5.0e6,
            pl_net_income=5.0e7 + 1.0e7 * i - (i % 2) * 3.0e6,
            bs_total_assets=2.0e9 + 1.0e8 * i + (i % 4) * 5.0e7,
            bs_total_liabilities=1.0e9 + 5.0e7 * i + (i % 3) * 2.0e7,
            bs_total_equity=equity_val,
            bs_bps=bps_val,
            pl_eps=80.0 + 5.0 * i + (i % 3) * 3.0,
            dps=20.0 + 2.0 * i,
            cf_operating_cf=1.2e8 + 1.5e7 * i + (i % 5) * 2.0e6,
            cf_free_cf=8.0e7 + 1.0e7 * i + (i % 4) * 1.5e6,
            stock_price=1500.0 + 100.0 * i + (i % 3) * 50.0,
            market_cap=1000.0 + 200.0 * i + (i % 3) * 50.0,
        ))
    db.add_all(recs)
    db.commit()


class TestExecute:
    def test_empty_db_raises(self, db):
        with pytest.raises(ValueError):
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_insufficient_samples_raises(self, db, make_fin):
        _seed_sector(db, make_fin, n=3)  # default min_samples=10 未満
        with pytest.raises(ValueError):
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_writes_predictions_and_ranks(self, db, make_fin):
        from database import RegressionResult
        _seed_sector(db, make_fin, n=12)
        res = asyncio.run(execute_plugin(plugin, {}, db))
        assert res["n_sectors"] >= 1
        assert res["sector_stats"][0]["r2"] is not None
        # 予測値は financial_records ではなく regression_results に保存される（計算結果の分離）
        rrs = db.query(RegressionResult).all()
        assert len(rrs) == 12
        # 乖離率は全件付与されること（市場データ揃いの seed）
        assert all(r.gap_ratio is not None for r in rrs)
        # market_cap & stock_price 揃いの場合 predicted_market_cap も書き込まれる
        assert all(r.predicted_market_cap is not None for r in rrs)
        # 業種内ランクが全件付与されていること
        assert all(p["sector_rank"] is not None for p in res["results"])

    def test_features_comma_string(self, db, make_fin):
        _seed_sector(db, make_fin, n=12)
        # per-share キーのみで指定可能（カンマ区切り）
        res = asyncio.run(execute_plugin(
            plugin, {"features": "pl_eps,bs_bps,ps_revenue,ps_operating_cf"}, db))
        assert res["n_sectors"] >= 1

    def test_resolve_c2_per_share_value(self, make_fin):
        # C2 派生 per-share が「絶対額 ÷ 発行株数」で正しく算出されること
        from plugins.sector_ols import _resolve_per_share_value
        from plugins.utils import shares_outstanding

        rec = make_fin(bs_total_equity=1.0e9, bs_bps=500.0, pl_rd_expenses=2.0e8)
        shares = shares_outstanding(rec)         # 1.0e9 / 500 = 2.0e6 株
        assert shares == pytest.approx(2.0e6)
        v = _resolve_per_share_value(rec, "ps_rd_expenses", shares)
        assert v == pytest.approx(2.0e8 / 2.0e6)  # 100 円/株

    def test_execute_with_c2_features(self, db, make_fin):
        # C2 per-share 特徴量を選択して coerce→execute が通り、予測が書かれること（結線の通し確認）
        from database import RegressionResult
        recs = []
        for i in range(1, 13):
            bps_val = 800.0 + 50.0 * i + (i % 4) * 10.0
            equity_val = bps_val * (1.0e6 + 5.0e4 * i)
            recs.append(make_fin(
                edinet_code=f"E{i:05d}", industry="情報・通信業",
                bs_total_equity=equity_val, bs_bps=bps_val,
                pl_rd_expenses=3.0e7 + 4.0e6 * i + (i % 3) * 1.0e6,
                pl_depreciation=5.0e7 + 6.0e6 * i + (i % 4) * 2.0e6,
                bs_ppe_total=8.0e8 + 7.0e7 * i + (i % 5) * 1.0e7,
                stock_price=1500.0 + 100.0 * i + (i % 3) * 50.0,
                market_cap=1000.0 + 200.0 * i,
            ))
        db.add_all(recs)
        db.commit()

        res = asyncio.run(execute_plugin(
            plugin,
            {"features": ["bs_bps", "ps_rd_expenses", "ps_depreciation", "ps_ppe_total"]},
            db,
        ))
        assert res["n_total"] == 12
        assert res["sector_stats"][0]["r2"] is not None
        assert len(db.query(RegressionResult).all()) == 12

    def test_skip_records_without_bps(self, db, make_fin):
        """bs_bps が NULL の銘柄は株数推計不能のため対象外になることを検証。"""
        # 11社は正常、3社は bs_bps=None（→ 計14社中11社のみ集計対象）
        recs_ok = []
        recs_no_bps = []
        for i in range(1, 12):
            bps_val = 800.0 + 50.0 * i
            equity_val = bps_val * (1.0e6 + 5.0e4 * i)
            recs_ok.append(make_fin(
                edinet_code=f"E{i:05d}", industry="情報・通信業",
                pl_revenue=1.0e9 + 1.0e8 * i,
                pl_operating_profit=1.0e8 + 2.0e7 * i,
                pl_net_income=5.0e7 + 1.0e7 * i,
                bs_total_equity=equity_val,
                bs_bps=bps_val,
                pl_eps=80.0 + 5.0 * i,
                dps=20.0 + 2.0 * i,
                cf_operating_cf=1.2e8 + 1.5e7 * i,
                cf_free_cf=8.0e7 + 1.0e7 * i,
                bs_total_assets=2.0e9 + 1.0e8 * i,
                bs_total_liabilities=1.0e9 + 5.0e7 * i,
                pl_gross_profit=4.0e8 + 5.0e7 * i,
                stock_price=1500.0 + 100.0 * i,
                market_cap=1000.0 + 200.0 * i,
            ))
        for i in range(20, 23):
            # bs_bps を None にしておく → shares_outstanding が None → スキップ
            recs_no_bps.append(make_fin(
                edinet_code=f"E{i:05d}", industry="情報・通信業",
                pl_revenue=1.0e9, pl_operating_profit=1.0e8,
                bs_total_equity=1.0e9, bs_bps=None,
                pl_eps=80.0, dps=20.0,
                cf_operating_cf=1.2e8, cf_free_cf=8.0e7,
                bs_total_assets=2.0e9, bs_total_liabilities=1.0e9,
                pl_gross_profit=4.0e8,
                stock_price=1500.0, market_cap=1500.0,
            ))
        db.add_all(recs_ok + recs_no_bps)
        db.commit()

        res = asyncio.run(execute_plugin(plugin, {}, db))
        # 業種に集計されたサンプル数は bs_bps 有効の 11 社のみ
        assert res["n_total"] == 11

    def test_target_default_is_stock_price(self):
        """params_schema の target デフォルトが stock_price であること（market_cap 削除済み）。"""
        schema = plugin.params_schema()
        assert schema["target"]["default"] == "stock_price"
        options = schema["target"]["options"]
        assert len(options) == 1
        assert options[0]["value"] == "stock_price"
