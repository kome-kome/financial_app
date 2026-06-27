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
        _seed_sector(db, make_fin, n=3)  # default min_samples=5 未満
        with pytest.raises(ValueError):
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_auto_drops_high_missing_feature(self, db, make_fin):
        # 欠損率の高い説明変数を足しても AND 全除外で 0 業種に潰れず、自動ドロップして
        # 実行できること（_select_features の根本対策）。pl_pretax_profit は seed しない
        # ＝ ps_pretax_profit は 100% NULL になる。
        _seed_sector(db, make_fin, n=12)
        params = {"features": DEFAULT_FEATURES_PRICE + ["ps_pretax_profit"], "min_samples": 5}
        res = asyncio.run(execute_plugin(plugin, params, db))
        assert res["n_sectors"] >= 1
        dropped = {d["feature"] for d in res["dropped_features"]}
        assert "ps_pretax_profit" in dropped          # 100% NULL は自動ドロップ
        assert "ps_pretax_profit" not in res["features_used"]
        assert set(res["features_used"]) <= set(DEFAULT_FEATURES_PRICE)

    def test_all_features_high_missing_raises_clearly(self, db, make_fin):
        # 選択列が全て高欠損なら採用列ゼロで明確に reject（データ収集ではなく項目選択の問題）
        _seed_sector(db, make_fin, n=12)
        params = {"features": ["ps_pretax_profit", "ps_machinery"], "min_samples": 5}
        with pytest.raises(ValueError, match="自動除外"):
            asyncio.run(execute_plugin(plugin, params, db))

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


# ── サブメソッド単体テスト（#169：回帰検出粒度の向上）──────────────────────────
import math
import statistics

from plugins.utils import normalize, winsorize


class TestPreprocessSector:
    """_preprocess_sector: winsorize(p1-p99) → z-score 正規化 + 切片列付与。"""

    def test_winsorize_then_zscore_and_intercept(self):
        features = ["f0", "f1"]
        # 100 サンプル。f0 と y に巨大外れ値を1つ仕込み、winsorize でクリップされることを見る
        rows = [[float(i), float(i) * 2.0] for i in range(100)]
        ys   = [float(i) for i in range(100)]
        rows[0][0] = 1e9
        ys[0]      = 1e9
        samples = [(rows[i], ys[i], None) for i in range(100)]

        X_norm, y_normed, y_mu, y_sd, X_win_cols, raw_y_win = \
            plugin._preprocess_sector(samples, features)

        # 切片列（1.0）＋特徴量列。各行は len(features)+1 要素
        assert all(row[0] == 1.0 for row in X_norm)
        assert all(len(row) == len(features) + 1 for row in X_norm)
        assert len(X_norm) == len(samples)

        # winsorize 適用: f0 列の外れ値（1e9）が p99 でクリップされ最大値が激減
        assert max(X_win_cols[0]) < 1e9
        assert X_win_cols[0] == winsorize([r[0] for r in rows])[0]

        # z-score 正規化: 第1特徴量列の平均≈0・標準偏差≈1
        feat0_norm = [row[1] for row in X_norm]
        assert abs(statistics.mean(feat0_norm)) < 1e-9
        assert abs(statistics.stdev(feat0_norm) - 1.0) < 1e-6

        # y も winsorize → zscore（mu/sd は winsorize 済み y から算出）
        assert raw_y_win == winsorize(ys)[0]
        _, exp_mu, exp_sd = normalize(raw_y_win, "zscore")
        assert y_mu == exp_mu and y_sd == exp_sd


class TestFitAndPredict:
    """_fit_and_predict: regularization による OLS / Ridge 分岐 + 逆正規化予測。"""

    def _xy(self):
        # 切片付き設計行列（線形 y=2x）。正規化済み入力という前提
        X_norm   = [[1.0, float(i)] for i in range(10)]
        y_normed = [0.5 * i for i in range(10)]
        return X_norm, y_normed

    def test_ols_branch(self):
        X_norm, y_normed = self._xy()
        result, all_yhat = plugin._fit_and_predict(
            X_norm, y_normed, y_mu=100.0, y_sd=2.0, regularization="ols")
        assert result is not None
        assert "alpha" not in result  # OLS は正則化パラメータを返さない
        # 逆正規化: yhat_norm * y_sd + y_mu と一致
        beta = result["beta"]
        expected = [sum(x * b for x, b in zip(row, beta)) * 2.0 + 100.0 for row in X_norm]
        assert all(abs(a - e) < 1e-9 for a, e in zip(all_yhat, expected))

    def test_ridge_branch(self):
        X_norm, y_normed = self._xy()
        result, all_yhat = plugin._fit_and_predict(
            X_norm, y_normed, y_mu=0.0, y_sd=1.0, regularization="ridge")
        assert result is not None
        assert "alpha" in result  # Ridge は選択された α を返す（OLS との分岐確認）
        beta = result["beta"]
        expected = [sum(x * b for x, b in zip(row, beta)) for row in X_norm]
        assert all(abs(a - e) < 1e-9 for a, e in zip(all_yhat, expected))

    def test_returns_none_on_empty(self):
        # フィット不能（空行列）→ (None, None)
        result, all_yhat = plugin._fit_and_predict([], [], 0.0, 1.0, "ols")
        assert result is None and all_yhat is None


class TestBuildStatEntry:
    """_build_stat_entry: 診断統計の NaN/欠損を安全に詰める（境界値）。"""

    def test_nan_values_packed_as_none(self):
        result = {
            "r2": 0.5, "adj_r2": 0.4, "rmse": 1.0,
            "df": 3, "rank": 3, "method": "ridge", "alpha": 1.0,
            "condition_number": float("nan"),               # NaN → None
            "p_value": [float("nan"), float("nan"), float("nan")],
            "t_stat":  [float("nan"), 2.0, float("nan")],   # 一部のみ有限
        }
        # 2特徴量（+切片）で check_collinearity が単列スカラー化を踏まない構成
        X_win_cols = [
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [2.0, 1.0, 4.0, 3.0, 5.0],
        ]
        entry = plugin._build_stat_entry(
            sector="X", samples=[(None, 1.0, None)] * 5, result=result,
            y_sd=1.0, X_norm=[[1.0, 0.0, 0.0]] * 5, y_normed=[0.0] * 5,
            features=["f0", "f1"], regularization="ridge", X_win_cols=X_win_cols,
        )
        assert entry["condition_number"] is None           # NaN → None
        assert entry["p_values"] == [None, None, None]      # NaN → None
        assert entry["t_stats"] == [None, 2.0, None]        # 有限値のみ残る
        assert entry["n_significant_features"] is None      # ridge は算出しない
        assert "diagnostics" not in entry                   # ridge は HC3 診断をスキップ
        assert entry["n"] == 5 and entry["industry"] == "X"

    def test_finite_condition_number_rounded(self):
        result = {
            "r2": 0.5, "adj_r2": 0.4, "rmse": 1.0,
            "df": 3, "rank": 3, "method": "ridge", "alpha": 1.0,
            "condition_number": 12.3456,
            "p_value": [], "t_stat": [],
        }
        entry = plugin._build_stat_entry(
            sector="Y", samples=[(None, 1.0, None)] * 5, result=result,
            y_sd=1.0, X_norm=[[1.0, 0.0, 0.0]] * 5, y_normed=[0.0] * 5,
            features=["f0", "f1"], regularization="ridge",
            X_win_cols=[[1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 1.0, 4.0, 3.0, 5.0]],
        )
        assert entry["condition_number"] == round(12.3456, 2)


# ── 部分プーリング（薄業種縮約）テスト ────────────────────────────────────────────

class TestShrinkage:
    """shrink_threshold による薄業種の gap_ratio 安定化を検証。"""

    FEATURES = ["pl_eps", "bs_bps"]  # 2特徴量: 薄業種でも OLS が回る最小構成

    def _seed_two_sectors(self, db, make_fin, n_thin=7, n_thick=20):
        """薄業種（n_thin 社・外れ値1社含む）＋厚業種（n_thick 社）を投入。"""
        recs = []
        # 厚業種: 正常データ
        for i in range(1, n_thick + 1):
            bps_val = 500.0 + 30.0 * i
            recs.append(make_fin(
                edinet_code=f"T{i:05d}", industry="厚業種",
                bs_total_equity=bps_val * 1.0e6,
                bs_bps=bps_val,
                pl_net_income=5.0e7 + 1.0e7 * i,
                pl_eps=50.0 + 5.0 * i,
                dps=10.0 + i,
                stock_price=800.0 + 50.0 * i,
                market_cap=1000.0 + 100.0 * i,
            ))
        # 薄業種: 正常 (n_thin-1) 社 ＋ 外れ値 1 社（stock_price が 50倍）
        for i in range(1, n_thin):
            bps_val = 600.0 + 40.0 * i
            recs.append(make_fin(
                edinet_code=f"S{i:05d}", industry="薄業種",
                bs_total_equity=bps_val * 1.0e6,
                bs_bps=bps_val,
                pl_net_income=4.0e7 + 8.0e6 * i,
                pl_eps=40.0 + 4.0 * i,
                dps=8.0 + i,
                stock_price=700.0 + 40.0 * i,
                market_cap=900.0 + 80.0 * i,
            ))
        # 外れ値: EPS・BPS は普通だが stock_price が極端
        recs.append(make_fin(
            edinet_code="SOUT1", industry="薄業種",
            bs_total_equity=650.0 * 1.0e6,
            bs_bps=650.0,
            pl_net_income=4.5e7,
            pl_eps=45.0,
            dps=9.0,
            stock_price=50000.0,  # 50倍の外れ値
            market_cap=60000.0,
        ))
        db.add_all(recs)
        db.commit()

    def test_shrink_threshold_in_return(self, db, make_fin):
        """shrink_threshold と n_shrunk_sectors が戻り値に含まれること。"""
        self._seed_two_sectors(db, make_fin)
        res = asyncio.run(execute_plugin(
            plugin,
            {"features": self.FEATURES, "min_samples": 5, "shrink_threshold": 15},
            db,
        ))
        assert "shrink_threshold" in res
        assert res["shrink_threshold"] == 15
        assert "n_shrunk_sectors" in res
        assert res["n_shrunk_sectors"] >= 0

    def test_no_shrinkage_when_threshold_zero(self, db, make_fin):
        """shrink_threshold=0 のとき n_shrunk_sectors=0（従来 separate OLS と同等）。"""
        self._seed_two_sectors(db, make_fin)
        res = asyncio.run(execute_plugin(
            plugin,
            {"features": self.FEATURES, "min_samples": 5, "shrink_threshold": 0},
            db,
        ))
        assert res["shrink_threshold"] == 0
        assert res["n_shrunk_sectors"] == 0

    def test_thin_sector_counted_as_shrunk(self, db, make_fin):
        """薄業種（n < threshold）が n_shrunk_sectors に計上されること。"""
        self._seed_two_sectors(db, make_fin, n_thin=7, n_thick=20)
        res = asyncio.run(execute_plugin(
            plugin,
            {"features": self.FEATURES, "min_samples": 5, "shrink_threshold": 15},
            db,
        ))
        # 薄業種 n=7 < threshold=15 → shrunk にカウントされる
        assert res["n_shrunk_sectors"] >= 1

    def test_shrinkage_reduces_gap_ratio_variance(self, db, make_fin):
        """薄業種に外れ値がある場合、縮約あり/なしで gap_ratio 標準偏差が低下すること。"""
        from database import RegressionResult
        self._seed_two_sectors(db, make_fin, n_thin=7, n_thick=20)

        # 縮約なし
        asyncio.run(execute_plugin(
            plugin,
            {"features": self.FEATURES, "min_samples": 5, "shrink_threshold": 0},
            db,
        ))
        thin_gaps_no_shrink = [
            r.gap_ratio for r in db.query(RegressionResult).all()
            if r.sector == "薄業種" and r.gap_ratio is not None
        ]
        db.query(RegressionResult).delete()
        db.commit()

        # 縮約あり
        asyncio.run(execute_plugin(
            plugin,
            {"features": self.FEATURES, "min_samples": 5, "shrink_threshold": 15},
            db,
        ))
        thin_gaps_shrunk = [
            r.gap_ratio for r in db.query(RegressionResult).all()
            if r.sector == "薄業種" and r.gap_ratio is not None
        ]

        assert len(thin_gaps_no_shrink) > 0 and len(thin_gaps_shrunk) > 0
        std_no_shrink = statistics.stdev(thin_gaps_no_shrink) if len(thin_gaps_no_shrink) > 1 else 0
        std_shrunk    = statistics.stdev(thin_gaps_shrunk)    if len(thin_gaps_shrunk) > 1 else 0
        # 縮約後の std は縮約前以下（外れ値がグローバル予測に引き寄せられる）
        assert std_shrunk <= std_no_shrink, (
            f"shrunk std={std_shrunk:.2f} should be <= no-shrink std={std_no_shrink:.2f}"
        )
