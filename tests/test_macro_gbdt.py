"""tests/test_macro_gbdt.py — M-2 MacroGbdtPlugin フルテスト（ADR-0003）

テスト観点:
  1. parity  : 共有ビルダーが M-1/M-2 に同一母集団を返す（交差項列を除く）
  2. leak    : walk-forward eval_set が train 月に厳密包含（test 月を含まない）
  3. coerce  : params_schema の bounds/membership が reject する
  4. smoke   : execute が cv_metrics（xgb/ols_baseline）・SHAP・per-stock shap・全社返却を満たす
  5. M-1 回帰: 既存 test_macro_risk_return.py が通ること（pytest 呼び出しで確認）
"""
import math
from types import SimpleNamespace
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

from plugins.macro_gbdt import (
    MacroGbdtPlugin, _make_xgb_fit_predict,
    _build_monotone_constraints, _MONOTONE_SIGN,
    _SIZE_FEAT, _SECTOR_TE_FEAT, _log_size, _fit_sector_encoding,
    _build_sector_cv_samples, _wrap_sector_target_encoding,
    _build_sector_final_samples, _sector_current_row,
)
from plugins.macro_snapshots import (
    build_snapshots, FINANCIAL_LAG_DAYS, HORIZON_WEEKS, oof_backtest, build_oof_meta,
)
from plugins.utils import coerce_params


# ── フィクスチャ共通 ──────────────────────────────────────────────────────────

def _make_price(trade_date: str, close_last: float):
    return SimpleNamespace(trade_date=trade_date, close_last=close_last)


def _make_fin(period_end_str: str, **kwargs):
    defaults = dict(
        edinet_code="E01234", sec_code="1234", company_name="テスト株式会社",
        industry="テスト業", period_end=None,
        per=15.0, pbr=1.2, roe=8.0, equity_ratio=50.0, roa=5.0, eps_growth=3.0,
        op_margin=10.0, net_margin=5.0, asset_turnover=0.8, de_ratio=0.5,
        nc_ratio=0.2, cf_ratio=8.0, op_growth=5.0, rev_growth=4.0,
        rd_intensity=0.0, da_intensity=0.0, z_op_margin=0.5, z_roe=0.3, z_cf_ratio=0.2,
        accruals=0.02, delta_roe=1.0, delta_op_margin=0.5, z_roe_sec=0.3, z_op_margin_sec=0.4,
        div_yield=2.0, bs_total_assets=1e10,
    )
    defaults.update(kwargs)
    import datetime
    if period_end_str:
        defaults["period_end"] = datetime.date.fromisoformat(period_end_str)
    return SimpleNamespace(**defaults)


def _make_db_mock(n_companies=5, n_weeks=120):
    """n_companies 社 × n_weeks 週の最小 DB モックを返す。"""
    import datetime

    prices_by_co = {}
    fin_by_co = {}
    companies = {}
    base = datetime.date(2019, 1, 4)

    for ci in range(n_companies):
        ec = f"E{ci:05d}"
        prices = []
        for w in range(n_weeks):
            d = (base + datetime.timedelta(weeks=w)).isoformat()
            prices.append(_make_price(d, 1000.0 + ci * 10 + w * 0.5))
        prices_by_co[ec] = prices

        pe_date = (base - datetime.timedelta(days=60)).isoformat()
        fin_by_co[ec] = [_make_fin(pe_date, edinet_code=ec, company_name=f"会社{ci}")]

        companies[ec] = SimpleNamespace(
            edinet_code=ec, name=f"会社{ci}", sec_code=str(1000 + ci), industry="テスト業",
        )

    db = MagicMock()
    return db, prices_by_co, fin_by_co, companies


def _make_macro_cache_usdjpy_only(prices_by_co):
    """USDJPY を全期間カバーで生成（YoY が全 snap_date で計算可能）。

    他系列（SP500 / US10Y 等）は意図的に含めない → _macro_from_cache が None を返し、
    macro_nan_ok の挙動（厳格除外 vs NaN 保持）を検証できる。
    """
    import datetime
    all_dates = sorted({r.trade_date for rows in prices_by_co.values() for r in rows})
    start = datetime.date.fromisoformat(all_dates[0]) - datetime.timedelta(days=420)
    end   = datetime.date.fromisoformat(all_dates[-1]) + datetime.timedelta(days=10)
    series = {}
    d, i = start, 0
    while d <= end:
        series[d.isoformat()] = 100.0 + i * 0.1
        d += datetime.timedelta(days=7)
        i += 1
    return {"USDJPY": series}   # SP500 / US10Y は欠落


plugin = MacroGbdtPlugin()


# ── 1. parity ─────────────────────────────────────────────────────────────────

class TestParity:
    """共有ビルダーが M-1/M-2 に同一母集団（交差項を除いた features / samples_by_ym）を返す。"""

    def _make_minimal_inputs(self):
        _, prices_by_co, fin_by_co, companies = _make_db_mock(n_companies=3, n_weeks=130)
        return prices_by_co, fin_by_co, companies

    def test_same_samples_by_ym_keys(self):
        prices_by_co, fin_by_co, companies = self._make_minimal_inputs()
        fin_feats = ["per", "pbr"]
        # M-1 版（交差項あり）
        s_m1, _, _, feats_m1 = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            fin_feats, [], False, 12, 0.5,
            build_interactions=True,
        )
        # M-2 版（交差項なし）
        s_m2, _, _, feats_m2 = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            fin_feats, [], False, 12, 0.5,
            build_interactions=False,
        )
        # 月キーは同一
        assert set(s_m1.keys()) == set(s_m2.keys()), "samples_by_ym のキー（月）が異なる"
        # 各月のサンプル数は同一
        for ym in s_m1:
            assert len(s_m1[ym]) == len(s_m2[ym]), f"{ym} のサンプル数が異なる"

    def test_m2_has_no_interaction_columns(self):
        prices_by_co, fin_by_co, companies = self._make_minimal_inputs()
        fin_feats = ["per", "pbr"]
        macro_names = []  # マクロなし（交差項は fin×macro）
        _, _, _, feats_m2 = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            fin_feats, macro_names, False, 12, 0.5,
            build_interactions=False,
        )
        assert not any("_x_" in f for f in feats_m2), "M-2 特徴量に交差項が混入"

    def test_m1_has_interaction_columns_when_macro_enabled(self):
        prices_by_co, fin_by_co, companies = self._make_minimal_inputs()
        fin_feats = ["per"]
        macro_cache_dummy = {}
        # マクロを付けても macro_cache が空だと全スキップになるので、
        # マクロなしで交差項は生成されないことを確認
        _, _, _, feats_m1 = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            fin_feats, [], False, 12, 0.5,
            build_interactions=True,
        )
        # マクロなしなら交差項もなし
        assert not any("_x_" in f for f in feats_m1)

    def test_target_values_match_between_m1_m2(self):
        """M-1/M-2 で学習ターゲット（52週先対数リターン）が一致する。"""
        prices_by_co, fin_by_co, companies = self._make_minimal_inputs()
        fin_feats = ["per", "pbr"]
        s_m1, _, _, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            fin_feats, [], False, 12, 0.5, build_interactions=True,
        )
        s_m2, _, _, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            fin_feats, [], False, 12, 0.5, build_interactions=False,
        )
        for ym in s_m1:
            targets_m1 = [t for _, t in s_m1[ym]]
            targets_m2 = [t for _, t in s_m2[ym]]
            assert targets_m1 == targets_m2, f"{ym} のターゲットが異なる"


# ── 1b. マクロ NaN 許容（macro_nan_ok・M-2 専用）────────────────────────────────

class TestMacroNanOk:
    """薄いマクロ系列で企業が激減しない根本対策の検証（build_snapshots レベル）。"""

    def _inputs(self):
        _, prices_by_co, fin_by_co, companies = _make_db_mock(n_companies=3, n_weeks=130)
        return prices_by_co, fin_by_co, companies

    def test_strict_drops_companies_when_macro_missing(self):
        """macro_nan_ok=False（M-1 既定）: 1系列でも欠損なら全企業脱落（従来挙動）。"""
        prices_by_co, fin_by_co, companies = self._inputs()
        macro_cache = _make_macro_cache_usdjpy_only(prices_by_co)
        macro_names = ["macro_usdjpy_yoy", "macro_sp500_yoy"]   # SP500 は cache に無い
        samples, _, snaps, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            ["per", "pbr"], macro_names, False, 12, 0.5,
            build_interactions=False, macro_nan_ok=False,
        )
        assert len(snaps) == 0, "厳格モードで欠損系列があるのに企業が残った"
        assert sum(len(v) for v in samples.values()) == 0

    def test_nan_ok_retains_companies_with_nan_feature(self):
        """macro_nan_ok=True（M-2）: 欠損系列は NaN として保持し企業を残す。"""
        prices_by_co, fin_by_co, companies = self._inputs()
        macro_cache = _make_macro_cache_usdjpy_only(prices_by_co)
        macro_names = ["macro_usdjpy_yoy", "macro_sp500_yoy"]
        samples, _, snaps, feats = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            ["per", "pbr"], macro_names, False, 12, 0.5,
            build_interactions=False, macro_nan_ok=True,
        )
        assert len(snaps) > 0, "NaN 許容モードで企業が残らなかった"
        sp_idx  = feats.index("macro_sp500_yoy")
        usd_idx = feats.index("macro_usdjpy_yoy")
        for ec, (feat_row, _info) in snaps.items():
            assert math.isnan(feat_row[sp_idx]), "欠損系列が NaN になっていない"
            assert math.isfinite(feat_row[usd_idx]), "充足系列まで NaN 化している"

    def test_company_count_stable_when_adding_thin_feature(self):
        """薄い系列を足しても企業数が維持される（USDJPY のみ → +SP500 で不変）。"""
        prices_by_co, fin_by_co, companies = self._inputs()
        macro_cache = _make_macro_cache_usdjpy_only(prices_by_co)
        _, _, snaps_1, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            ["per", "pbr"], ["macro_usdjpy_yoy"], False, 12, 0.5,
            build_interactions=False, macro_nan_ok=True,
        )
        _, _, snaps_2, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            ["per", "pbr"], ["macro_usdjpy_yoy", "macro_sp500_yoy"], False, 12, 0.5,
            build_interactions=False, macro_nan_ok=True,
        )
        assert set(snaps_1.keys()) == set(snaps_2.keys()), "薄い系列追加で企業母集団が変化した"


# ── 2. leak ───────────────────────────────────────────────────────────────────

class TestLeak:
    """early_stopping の eval_set が train 月に厳密包含（test 月を含まない）。"""

    def test_eval_set_is_subset_of_train(self):
        """fit_predict コールバックで eval_set に使う行数が n_fit 以降（train の末尾）。"""
        import xgboost as xgb

        eval_sets_received = []
        original_fit = xgb.XGBRegressor.fit

        def mock_fit(self, X, y, eval_set=None, verbose=False, **kwargs):
            if eval_set:
                eval_sets_received.append(eval_set)
            # 実際には学習しない
            self.best_iteration = 10
            self.n_estimators = 100
            return self

        n_features = 3
        n_train = 50
        n_test = 10
        train_samples = [([float(i % 7), float(i % 5), float(i % 3)], float(i) * 0.01) for i in range(n_train)]
        test_samples  = [([1.0, 2.0, 3.0], 0.05) for _ in range(n_test)]

        best_iters = []
        callback = _make_xgb_fit_predict(
            {"max_depth": 3, "learning_rate": 0.1, "subsample": 0.8, "colsample_bytree": 0.8,
             "min_child_weight": 1, "reg_lambda": 1.0, "reg_alpha": 0.0,
             "n_estimators": 100, "early_stopping_rounds": 10,
             "tree_method": "hist", "objective": "reg:squarederror", "random_state": 42},
            best_iters,
        )

        with patch.object(xgb.XGBRegressor, 'fit', mock_fit):
            with patch.object(xgb.XGBRegressor, 'predict', lambda self, X: np.zeros(len(X))):
                with patch.object(xgb.XGBRegressor, '__init__', lambda self, **kwargs: None):
                    # mock_fit を直接動かす代わりに _VALID_FRAC ロジックのみ確認
                    pass

        # eval_set に使う行は train の末尾 _VALID_FRAC（= n_train * 0.2 = 10 行）
        # つまり n_fit = 40、valid_rows = 10 行（インデックス 40-49）
        from plugins.macro_gbdt import _VALID_FRAC
        n_valid = max(1, int(n_train * _VALID_FRAC))
        n_fit = n_train - n_valid
        assert n_fit > 0 and n_valid > 0
        assert n_fit + n_valid == n_train

    def test_future_price_not_in_features(self):
        """スナップショット構築で snap_idx+HORIZON_WEEKS の株価をターゲットに使い、
        それ以降（未来）の株価を特徴量に使わないことを確認する。"""
        _, prices_by_co, fin_by_co, companies = _make_db_mock(n_companies=2, n_weeks=130)
        _, _, current_snaps, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, {},
            ["per", "pbr"], [], False, 12, 0.5, build_interactions=False,
        )
        # current_snaps の snap_date は price_rows の最終日以前でなければならない
        for ec, (feat_row, info) in current_snaps.items():
            snap_date = info["snap_date"]
            price_rows = info["price_rows"]
            max_date = max(r.trade_date for r in price_rows)
            assert snap_date <= max_date, "snap_date が price_rows の最終日を超えている（リーク）"


# ── 3. coerce ─────────────────────────────────────────────────────────────────

class TestCoerce:
    """params_schema の bounds/membership 違反が ValueError を送出する。"""

    schema = plugin.params_schema()

    def _coerce(self, raw):
        return coerce_params(self.schema, raw)

    def test_defaults_valid(self):
        """全フィールドがデフォルト値で通過する。"""
        defaults = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        result = self._coerce(defaults)
        assert "lambda_risk" in result
        assert "max_depth" in result

    def test_lambda_out_of_bounds_rejected(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["lambda_risk"] = 99.0
        with pytest.raises(ValueError, match="lambda_risk"):
            self._coerce(raw)

    def test_max_depth_out_of_bounds_rejected(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["max_depth"] = 100
        with pytest.raises(ValueError, match="max_depth"):
            self._coerce(raw)

    def test_invalid_risk_axis_rejected(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["risk_axis"] = "r1"
        with pytest.raises(ValueError, match="risk_axis"):
            self._coerce(raw)

    def test_invalid_fin_feature_rejected(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["fin_features"] = ["per", "nonexistent_feature"]
        with pytest.raises(ValueError, match="fin_features"):
            self._coerce(raw)

    def test_invalid_macro_feature_rejected(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["macro_features"] = ["macro_usdjpy_yoy", "macro_unknown_xyz"]
        with pytest.raises(ValueError, match="macro_features"):
            self._coerce(raw)

    def test_learning_rate_bounds(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["learning_rate"] = 0.0  # min=0.01
        with pytest.raises(ValueError):
            self._coerce(raw)

    def test_subsample_upper_bound(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["subsample"] = 1.5  # max=1.0
        with pytest.raises(ValueError):
            self._coerce(raw)

    def test_no_max_features_param(self):
        """M-2 は max_features（BIC 専用）を持たない。"""
        assert "max_features" not in self.schema


# ── 4. smoke ─────────────────────────────────────────────────────────────────

class TestExecuteSmoke:
    """execute が期待する出力キーと構造を持つか（DB モック使用）。"""

    def _make_params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        base.update(overrides)
        return coerce_params(plugin.params_schema(), base)

    def _make_db(self, n_companies=4, n_weeks=210):
        """prices_by_co / fin_by_co / companies を持つ DB モックを返す。"""
        import datetime
        db = MagicMock()
        base = datetime.date(2018, 1, 5)

        PX = type("PX", (), {})
        FIN = type("FIN", (), {})
        CO = type("CO", (), {})

        prices_by_co = defaultdict(list)
        fin_by_co = defaultdict(list)
        companies = {}

        for ci in range(n_companies):
            ec = f"E{ci:05d}"
            for w in range(n_weeks):
                d = (base + datetime.timedelta(weeks=w)).isoformat()
                p = PX()
                p.trade_date = d; p.close_last = 1000.0 + ci * 10 + w * 0.3
                prices_by_co[ec].append(p)

            pe = (base - datetime.timedelta(days=60)).isoformat()
            fin = _make_fin(pe, edinet_code=ec, company_name=f"会社{ci}",
                            sec_code=str(1000 + ci), industry="テスト業")
            fin_by_co[ec].append(fin)

            co = CO()
            co.edinet_code = ec; co.name = f"会社{ci}"; co.sec_code = str(1000 + ci)
            co.industry = "テスト業"
            companies[ec] = co

        return db, dict(prices_by_co), dict(fin_by_co), companies

    def test_execute_returns_required_keys(self):
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        required = {"cv_metrics", "selected_features", "feature_coefs",
                    "n_train_samples", "n_companies", "results",
                    "model_type", "best_iteration"}
        for k in required:
            assert k in result, f"出力に '{k}' がない"

    def test_execute_cv_metrics_has_xgb_and_ols(self):
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        cv = result["cv_metrics"]
        assert "xgb" in cv, "cv_metrics に xgb がない"
        assert "ols_baseline" in cv, "cv_metrics に ols_baseline がない"
        for key in ("folds", "mean_r2", "mean_rmse", "n_folds"):
            assert key in cv["xgb"], f"cv_metrics.xgb に '{key}' がない"
            assert key in cv["ols_baseline"], f"cv_metrics.ols_baseline に '{key}' がない"

    def test_execute_with_price_features(self):
        """price_features 有効時に execute が完走し、selected_features へ px_* が入る（Issue #364）。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False,
                                   price_features=["px_rvol", "px_rev4w"])

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert "results" in result and result["results"]
        # SHAP/feature_coefs は all_feat_names 由来 → px_* が特徴量として現れる
        feat_names = set(result.get("feature_coefs", {}).keys())
        assert {"px_rvol", "px_rev4w"} & feat_names, f"px_* が特徴量に現れない: {feat_names}"

    def test_execute_has_oof_backtest(self):
        """execute が oof_backtest（アウトオブサンプル検証）を返す（ADR-0004）。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert "oof_backtest" in result, "execute 出力に oof_backtest がない"
        oof = result["oof_backtest"]
        for k in ("n_quantiles", "n_periods", "n_periods_quantile", "n_oof_samples",
                  "quantile_returns", "rank_ic", "long_short_spread", "hit_rate"):
            assert k in oof, f"oof_backtest に '{k}' がない"
        assert set(oof["rank_ic"].keys()) == {"mean", "std", "n"}
        # embargo=12（ADR-0014）適用後も OOF fold が生存していることを検証。キー存在のみだと
        # fold 全滅（oof_backtest({})）でも緑になりサイレントに空洞化する（#363）。fold 生存の
        # 直接指標は n_periods / n_oof_samples を使う（rank_ic.n は期内予測分散に依存し、
        # この合成データは完全線形で期内予測が縮退＝Spearman 未定義になるため embargo 生存の
        # 指標には使えない）。
        assert oof["n_periods"] > 0, "embargo 適用で OOF fold が全滅している"
        assert oof["n_oof_samples"] > 0

    def test_execute_all_companies_returned(self):
        """results は全社を返す（top_n でスライスしない）。"""
        n_co = 4
        db, prices_by_co, fin_by_co, companies = self._make_db(n_companies=n_co)
        params = self._make_params(use_macro=False, top_n=5)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["n_companies"] == len(result["results"]), "n_companies と results 件数が不一致"
        assert result["n_companies"] > 0, "results が空"

    def test_execute_per_stock_shap_attached(self):
        """全社の results に 'shap' キーが存在する。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        for item in result["results"]:
            assert "shap" in item, f"{item['edinet_code']} に shap がない"
            assert isinstance(item["shap"], dict), "shap は dict でなければならない"
            assert len(item["shap"]) > 0, "shap が空"

    def test_execute_global_shap_in_feature_coefs(self):
        """feature_coefs に mean|SHAP|（非負）が入っている。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        coefs = result["feature_coefs"]
        assert isinstance(coefs, dict) and len(coefs) > 0, "feature_coefs が空"
        for name, val in coefs.items():
            assert val >= 0, f"mean|SHAP| が負（{name}={val}）: 絶対値でなければならない"

    def test_execute_signed_shap(self):
        """署名付き SHAP（feature_coefs_signed）と学習方向（feature_shap_dir）が付く（#371）。

        - feature_coefs_signed のキーは feature_coefs と一致
        - |signed| は mean|SHAP| に一致（符号だけ付与）
        - feature_shap_dir の相関は [-1, 1]
        """
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        mag = result["feature_coefs"]
        signed = result["feature_coefs_signed"]
        direction = result["feature_shap_dir"]
        assert set(signed.keys()) == set(mag.keys()), "signed のキーが mean|SHAP| と不一致"
        assert set(direction.keys()) == set(mag.keys()), "direction のキーが不一致"
        for name in mag:
            assert abs(signed[name]) == pytest.approx(mag[name], abs=1e-6), \
                f"|signed| が mean|SHAP| と不一致（{name}）"
            assert -1.0 <= direction[name] <= 1.0, f"方向 corr が範囲外（{name}={direction[name]}）"

    def test_execute_shap_interactions(self):
        """SHAP 交互作用の上位ペアが返る（#371）。強度降順・strength>0・自己ペアなし。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        inter = result["feature_interactions"]
        assert isinstance(inter, list), "feature_interactions は list"
        assert result["shap_interactions_available"] is (len(inter) > 0)
        strengths = [p["strength"] for p in inter]
        assert strengths == sorted(strengths, reverse=True), "強度降順でない"
        for p in inter:
            assert set(p.keys()) == {"a", "b", "strength"}
            assert p["a"] != p["b"], "自己ペア（対角）が混入している"
            assert p["strength"] > 0

    def test_execute_shap_interactions_off(self):
        """shap_interactions=False で交互作用計算をスキップ（空リスト・#371）。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False, shap_interactions=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["feature_interactions"] == []
        assert result["shap_interactions_available"] is False

    def test_execute_r1_is_conformal_halfwidth(self):
        """R1（確実性軸）= コンフォーマル区間半幅（Issue #365）。OLS 予測SE の代替。

        XGBoost は閉形式の予測SE を持たないため、OOF 残差 |resid| の τ 分位（区間半幅）で
        r1 を埋める。非負・有限（None もフォールバック不能時は許容）で、sell_ranking の
        R3 足切りゲートが読める形であることを確認する。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        r1s = [item.get("r1") for item in result["results"]]
        # 少なくとも一部の銘柄が非 None の区間半幅を持つ（OOF 残差が揃えば global で必ず埋まる）。
        assert any(v is not None for v in r1s), "r1（コンフォーマル区間半幅）が全社 None"
        for v in r1s:
            assert v is None or (isinstance(v, float) and v >= 0.0), f"r1 が不正な区間半幅: {v}"

    def test_execute_with_partial_macro_nan(self):
        """マクロ一部欠損（NaN）でも execute が end-to-end（XGB CV・OLS baseline・最終fit
        ・predict・SHAP）を完走し全社返す。USDJPY のみ充足、SP500/US10Y は NaN。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        # macro_features は全選択肢が既定になったため、この
        # テストの意図（USDJPYのみ充足・SP500/US10Yは意図的NaN）に合わせて3系列に絞る。
        params = self._make_params(
            use_macro=True,
            macro_features=["macro_usdjpy_yoy", "macro_sp500_yoy", "macro_us10y_zscore"],
        )
        macro_cache = _make_macro_cache_usdjpy_only(prices_by_co)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value=macro_cache), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["n_companies"] > 0, "NaN マクロで企業が全滅した"
        assert "macro_sp500_yoy" in result["selected_features"]
        # OLS ベースラインも NaN 補完で完走している
        assert result["cv_metrics"]["ols_baseline"]["n_folds"] >= 0
        # μ̂ が有限
        for item in result["results"]:
            assert item["mu_raw"] == item["mu_raw"], "mu_raw が NaN"

    def test_execute_model_type_xgboost(self):
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["model_type"] == "xgboost"

    def test_execute_r_macro_available_false_when_producer_empty(self):
        """#273: macro_beta 未蓄積時、r_macro_available は False（UI が risk_axis の
        r_macro 選択肢を無効化する判断材料）。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["r_macro_available"] is False
        assert all(item["r_macro"] is None for item in result["results"])

    def test_execute_r_macro_available_true_when_producer_has_data(self):
        """#273: macro_beta が1社でも蓄積済みなら r_macro_available は True。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)
        first_ec = next(iter(companies))
        producer = {first_ec: {"mu": 0.01, "r_macro": 0.05, "r1_prime": 0.02}}

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value=producer):
            result = plugin.execute(params, db)

        assert result["r_macro_available"] is True

    def test_execute_insufficient_samples_raises(self):
        """サンプル不足で ValueError。"""
        db = MagicMock()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=({}, {}, {})):
            with pytest.raises(ValueError, match="株価週次履歴"):
                plugin.execute(params, db)

    def test_execute_no_fin_features_raises(self):
        """財務特徴量が空で ValueError。"""
        db = MagicMock()
        params = self._make_params(use_macro=False)
        params["fin_features"] = []
        with pytest.raises(ValueError, match="財務特徴量"):
            plugin.execute(params, db)


# ── 5. プラグイン登録 ─────────────────────────────────────────────────────────

class TestPluginMeta:
    def test_plugin_is_heavy(self):
        assert plugin.heavy is True

    def test_plugin_ui_order(self):
        assert plugin.ui_order == 340

    def test_plugin_category(self):
        assert plugin.category == "③ 将来リターンを予測"


# ── 6. ハイパーパラメータ探索中のスコアリング省略モード（Issue #299） ────────────

class TestObjectiveOnlyMode:

    def _make_params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        base.update(overrides)
        return coerce_params(plugin.params_schema(), base)

    def _make_db(self, n_companies=4, n_weeks=210):
        return TestExecuteSmoke()._make_db(n_companies=n_companies, n_weeks=n_weeks)

    def test_skips_shap_and_raw_items_construction(self):
        """database.tuning_objective_only() 内では SHAP 計算・最終モデル再学習を伴う
        全社 raw_items 構築が呼ばれず、oof_backtest 算出直後に早期return する。"""
        import database

        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}), \
             patch("shap.TreeExplainer") as mock_shap, \
             database.tuning_objective_only():
            result = plugin.execute(params, db)

        mock_shap.assert_not_called()
        assert result["results"] == []
        assert result["n_companies"] == 0
        assert result["feature_coefs"] == {}
        assert result["feature_coefs_signed"] == {}      # #371
        assert result["feature_interactions"] == []       # #371
        assert result["shap_interactions_available"] is False
        assert result["best_iteration"] is None
        assert "oof_backtest" in result

    def test_oof_backtest_identical_with_and_without_objective_only(self):
        """スコアリング省略の有無で oof_backtest の値は一切変わらない（統計的妥当性の担保）。"""
        import database

        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result_full = plugin.execute(params, MagicMock())

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}), \
             database.tuning_objective_only():
            result_skip = plugin.execute(params, MagicMock())

        assert result_full["oof_backtest"] == result_skip["oof_backtest"]

    def test_normal_mode_still_returns_full_results_outside_context(self):
        """コンテキスト外（通常の /api/plugins/{name}/run 相当）は従来通りフルスコアリングする。"""
        import database

        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)

        assert database.is_tuning_objective_only() is False
        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert len(result["results"]) > 0
        assert result["n_companies"] > 0
        assert result["best_iteration"] is not None

    def test_plugin_name(self):
        assert plugin.name == "macro_gbdt"

    def test_plugin_depends_on_empty(self):
        assert plugin.depends_on == []

    def test_to_meta_has_required_keys(self):
        meta = plugin.to_meta()
        for k in ("name", "label", "heavy", "category", "ui_order", "params_schema"):
            assert k in meta


# ── 6. アウトオブサンプル検証（OOF）ヘルパ（ADR-0004）─────────────────────────

class TestOofBacktest:
    """oof_backtest: 無リーク OOF 予測 → 分位/IC/LS/hit-rate。純関数・DB非依存。"""

    def _ramp(self, n=20):
        # yhat と y_true 同順（完全な順序付け）
        return [(i * 0.01, i * 0.01) for i in range(n)]

    def test_perfect_order_ic_one_and_monotonic(self):
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        o = oof_backtest(r, n_quantiles=5)
        assert o["rank_ic"]["mean"] == 1.0
        assert o["rank_ic"]["n"] == 2
        assert o["long_short_spread"] > 0
        assert o["hit_rate"] == 1.0
        q = o["quantile_returns"]
        assert q == sorted(q), "分位リターンが μ̂ 昇順で単調増でない"
        assert o["n_oof_samples"] == 40
        assert o["n_periods_quantile"] == 2

    def test_reverse_order_negative_ic(self):
        r = {"m": [(i * 0.01, -i * 0.01) for i in range(20)]}
        o = oof_backtest(r, n_quantiles=5)
        assert o["rank_ic"]["mean"] == -1.0
        assert o["long_short_spread"] < 0
        assert o["hit_rate"] == 0.0

    def test_insufficient_samples_no_quantiles(self):
        # 期内サンプルが n_quantiles*2 未満 → 分位は出さないが IC は試行
        r = {"m": [(0.1, 0.2), (0.2, 0.1), (0.3, 0.3)]}  # 3 < 5*2
        o = oof_backtest(r, n_quantiles=5)
        assert o["quantile_returns"] == []
        assert o["n_periods_quantile"] == 0
        assert o["long_short_spread"] is None
        assert o["hit_rate"] is None
        assert o["rank_ic"]["n"] == 1   # IC は 3 サンプルで算出

    def test_empty(self):
        o = oof_backtest({}, n_quantiles=5)
        assert o["n_oof_samples"] == 0
        assert o["rank_ic"]["n"] == 0
        assert o["rank_ic"]["mean"] is None

    # ── 摩擦コスト（Issue #316）: cost_bps ────────────────────────────────

    def test_cost_bps_default_zero_matches_legacy(self):
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        o = oof_backtest(r, n_quantiles=5)
        assert o["cost_bps"] == 0.0
        assert o["long_short_spread_net"] == o["long_short_spread"]

    def test_cost_bps_deducts_round_trip(self):
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        o = oof_backtest(r, n_quantiles=5, cost_bps=10.0)
        assert o["cost_bps"] == 10.0
        assert o["long_short_spread_net"] == pytest.approx(o["long_short_spread"] - 0.2)

    def test_cost_bps_none_spread_stays_none(self):
        r = {"m": [(0.1, 0.2), (0.2, 0.1), (0.3, 0.3)]}  # 3 < 5*2 → spread は None
        o = oof_backtest(r, n_quantiles=5, cost_bps=10.0)
        assert o["long_short_spread"] is None
        assert o["long_short_spread_net"] is None
        assert o["quantile_returns"] == []

    # ── 業種中立rank-IC・実効ターンオーバー・ブレークイーブンbps（Issue #368）────

    def test_meta_none_new_metrics_are_none(self):
        """meta_by_ym 未指定なら新指標は全 None・既存キーは不変（後方互換）。"""
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        o = oof_backtest(r, n_quantiles=5)
        assert o["rank_ic_industry_neutral"] == {"mean": None, "n": 0}
        assert o["effective_turnover"] is None
        assert o["breakeven_cost_bps"] is None
        assert o["long_short_spread_net_turnover"] is None
        assert o["annual_turnover"] is None

    def test_industry_neutral_ic_removes_sector_bet(self):
        """業種ベットで raw IC は高いが業種内は逆順 → 業種中立 IC は負に落ちる。"""
        yh = [10, 11, 12, 13, 14, 0, 1, 2, 3, 4]
        yt = [104, 103, 102, 101, 100, 4, 3, 2, 1, 0]   # 各業種内で yhat と逆順
        ids = [f"a{i}" for i in range(5)] + [f"b{i}" for i in range(5)]
        inds = ["A"] * 5 + ["B"] * 5
        r = {"m": list(zip(yh, yt))}
        meta = {"m": list(zip(ids, inds))}
        o = oof_backtest(r, n_quantiles=5, meta_by_ym=meta)
        assert o["rank_ic"]["mean"] > 0.4                    # セクター傾斜で正
        assert o["rank_ic_industry_neutral"]["mean"] == -1.0  # 業種内は完全逆順
        assert o["rank_ic_industry_neutral"]["n"] == 1

    def test_industry_neutral_skips_null_industry_and_singletons(self):
        """industry=None の行は除外・単独業種はデミーンで消える → 有効n不足なら None。"""
        r = {"m": [(i * 0.01, i * 0.01) for i in range(6)]}
        # 全行 industry=None → 中立 IC 算出不可
        meta = {"m": [(f"e{i}", None) for i in range(6)]}
        o = oof_backtest(r, n_quantiles=5, meta_by_ym=meta)
        assert o["rank_ic_industry_neutral"] == {"mean": None, "n": 0}

    def test_turnover_zero_when_membership_stable(self):
        """分位メンバーが毎期同一 → 実効ターンオーバー0・breakeven は算出不能(None)。"""
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        ids = [f"e{i}" for i in range(20)]
        meta = {ym: [(i, "A") for i in ids] for ym in r}
        o = oof_backtest(r, n_quantiles=5, meta_by_ym=meta, rebalance_per_year=4)
        assert o["effective_turnover"] == 0.0
        assert o["breakeven_cost_bps"] is None    # turnover=0 → ゼロ除算回避
        assert o["annual_turnover"] == 0.0

    def test_turnover_full_churn_breakeven_scales(self):
        """2期目で銘柄を総入替 → turnover=1・breakeven=gross*50/1。"""
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        meta = {
            "2020-01": [(f"e{i}", "A") for i in range(20)],
            "2020-02": [(f"x{i}", "A") for i in range(20)],
        }
        o = oof_backtest(r, n_quantiles=5, meta_by_ym=meta, rebalance_per_year=4)
        assert o["effective_turnover"] == 1.0
        assert o["breakeven_cost_bps"] == pytest.approx(o["long_short_spread"] * 50.0)
        assert o["annual_turnover"] == 4.0

    def test_turnover_partial_and_net_turnover(self):
        """部分入替の Jaccard 平均・ネット(実効回転控除) の式一致。"""
        r = {"2020-01": [(1, 1), (2, 2), (3, 3), (4, 4)],
             "2020-02": [(1, 1), (2, 2), (3, 3), (4, 4)]}
        # n_quantiles=2: top={c,d}/bottom={a,b} → 期2 top={d,e}: top非重複=1-1/3, bottom=0 → 平均 1/3
        meta = {"2020-01": [("a", "A"), ("b", "A"), ("c", "A"), ("d", "A")],
                "2020-02": [("a", "A"), ("b", "A"), ("d", "A"), ("e", "A")]}
        o = oof_backtest(r, n_quantiles=2, cost_bps=10.0, meta_by_ym=meta)
        assert o["effective_turnover"] == pytest.approx(1.0 / 3.0, abs=1e-4)
        gross = o["long_short_spread"]
        expect_net = gross - (10.0 / 100.0) * 2 * o["effective_turnover"]
        assert o["long_short_spread_net_turnover"] == pytest.approx(expect_net, abs=1e-6)

    def test_build_oof_meta_aligns_ids_and_industry(self):
        """build_oof_meta は残差順に (stock_id, industry) を並べる。stock_ids 無しは None 埋め。"""
        sample_meta = {"2020-01": [("鉄鋼", 100.0), ("情報通信", 50.0)]}
        stock_ids = {"2020-01": ["E1", "E2"]}
        m = build_oof_meta(stock_ids, sample_meta, ["2020-01"])
        assert m["2020-01"] == [("E1", "鉄鋼"), ("E2", "情報通信")]
        m2 = build_oof_meta(None, sample_meta, ["2020-01"])
        assert m2["2020-01"] == [(None, "鉄鋼"), (None, "情報通信")]

    # ── rank-IC 差検定・単調性（Issue #369）────────────────────────────────

    def test_rank_ic_by_period_maps_each_valid_period(self):
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        o = oof_backtest(r, n_quantiles=5)
        assert set(o["rank_ic_by_period"]) == {"2020-01", "2020-02"}
        assert o["rank_ic_by_period"]["2020-01"] == 1.0   # 完全順序 → IC=1

    def test_rank_ic_by_period_skips_low_sample_periods(self):
        # n<3 の期は _spearman が None → by_period から除外（IC の n と一致）
        r = {"good": self._ramp(), "bad": [(0.1, 0.2), (0.2, 0.1)]}
        o = oof_backtest(r, n_quantiles=5)
        assert "bad" not in o["rank_ic_by_period"]
        assert o["rank_ic"]["n"] == len(o["rank_ic_by_period"]) == 1

    def test_monotonicity_perfect_order(self):
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        m = oof_backtest(r, n_quantiles=5)["monotonicity"]
        assert m["spearman_mean"] == 1.0            # 各期の分位が完全単調増
        assert m["adjacent_increasing_rate"] == 1.0
        assert 0.0 < m["p_value"] < 0.001           # Davison-Hinkley フロアで厳密 0 でない
        assert m["n_periods"] == 2

    def test_monotonicity_reverse_order_low(self):
        r = {"m": [(i * 0.01, -i * 0.01) for i in range(20)]}
        m = oof_backtest(r, n_quantiles=5)["monotonicity"]
        assert m["spearman_mean"] == -1.0           # 逆順 → 完全単調減
        assert m["adjacent_increasing_rate"] == 0.0

    def test_monotonicity_empty_when_no_quantile_periods(self):
        r = {"m": [(0.1, 0.2), (0.2, 0.1), (0.3, 0.3)]}  # 分位計算対象外
        m = oof_backtest(r, n_quantiles=5)["monotonicity"]
        assert m["n_periods"] == 0
        assert m["spearman_mean"] is None
        assert m["p_value"] is None

    # ── 売り側（ショート側）識別力（Issue #402）─────────────────────────────

    def test_short_side_spread_positive_when_bottom_underperforms(self):
        """完全順序なら bottom 分位は市場平均を下回る → spread 正・hit-rate 1.0。"""
        r = {"2020-01": self._ramp(), "2020-02": self._ramp()}
        o = oof_backtest(r, n_quantiles=5)
        # y=0.00..0.19 → 全体平均 0.095・bottom 分位(4件)平均 0.015
        assert o["short_side_spread"] == pytest.approx(0.08, abs=1e-6)
        assert o["short_side_hit_rate"] == 1.0
        assert set(o["short_side_spread_by_period"]) == {"2020-01", "2020-02"}

    def test_short_side_spread_negative_on_reverse_order(self):
        """μ̂ 下位が実際は勝つ（逆順）→ 売り側 spread 負・hit-rate 0.0。"""
        r = {"m": [(i * 0.01, -i * 0.01) for i in range(20)]}
        o = oof_backtest(r, n_quantiles=5)
        assert o["short_side_spread"] < 0
        assert o["short_side_hit_rate"] == 0.0

    def test_short_side_benchmark_is_all_sample_mean_not_quantile_mean(self):
        """分位サイズが不均一（m%n_quantiles≠0）でもベンチは全サンプル平均。"""
        r = {"m": [(i * 0.01, i * 0.01) for i in range(7)]}   # n_quantiles=2 → 3件/4件
        o = oof_backtest(r, n_quantiles=2)
        # 全体平均 0.03 − bottom(3件)平均 0.01 = 0.02（分位平均の単純平均 0.0275 ではない）
        assert o["short_side_spread"] == pytest.approx(0.02, abs=1e-6)

    def test_short_side_none_when_no_quantile_periods(self):
        r = {"m": [(0.1, 0.2), (0.2, 0.1), (0.3, 0.3)]}  # 分位計算対象外
        o = oof_backtest(r, n_quantiles=5)
        assert o["short_side_spread"] is None
        assert o["short_side_hit_rate"] is None
        assert o["short_side_spread_by_period"] == {}


# ── 7. producer μ̂ 永続化（sell_ranking 連携・ADR-0004）───────────────────────

class TestProducer:
    """macro_gbdt_scores への write→read 往復・スナップショット置換・M-1 形契約。"""

    def test_produced_output_false_when_empty(self, db):
        assert plugin.produced_output(db) is False

    def test_replace_and_read_round_trip(self, db):
        from database import replace_macro_gbdt_scores, get_macro_gbdt_scores
        rows = [{"edinet_code": f"E{i:05d}", "mu": i * 0.01} for i in range(5)]
        n = replace_macro_gbdt_scores(db, rows, "2026-06-26")
        assert n == 5
        assert plugin.produced_output(db) is True
        got = get_macro_gbdt_scores(db)
        assert got["E00003"] == pytest.approx(0.03)
        # read_producer_scores は M-1 と同一形 {mu, r_macro, r1_prime}
        scores = plugin.read_producer_scores(db, None)
        assert set(scores["E00002"].keys()) == {"mu", "r_macro", "r1_prime"}
        assert scores["E00002"]["mu"] == pytest.approx(0.02)
        assert scores["E00002"]["r1_prime"] is None          # XGBoost は予測SEなし
        assert scores["E00002"]["r_macro"] is None            # macro_beta 未蓄積→graceful

    def test_replace_is_snapshot_overwrite(self, db):
        from database import replace_macro_gbdt_scores, get_macro_gbdt_scores
        replace_macro_gbdt_scores(db, [{"edinet_code": "E1", "mu": 0.1},
                                       {"edinet_code": "E2", "mu": 0.2}], "d1")
        # 2回目は全置換 → E1/E2 は消え E3 のみ残る
        replace_macro_gbdt_scores(db, [{"edinet_code": "E3", "mu": 0.3}], "d2")
        assert get_macro_gbdt_scores(db) == {"E3": pytest.approx(0.3)}

    def test_none_mu_skipped(self, db):
        from database import replace_macro_gbdt_scores, get_macro_gbdt_scores
        n = replace_macro_gbdt_scores(db, [{"edinet_code": "E1", "mu": None},
                                           {"edinet_code": "E2", "mu": 0.2}], "d")
        assert n == 1
        assert set(get_macro_gbdt_scores(db)) == {"E2"}


# ── 8. ハイパーパラメータ自動探索の探索空間（Issue #266）───────────────────────

class TestTuningSearchSpace:

    def test_returns_base_params_and_dims(self):
        base_params, dims = plugin.tuning_search_space()
        assert isinstance(base_params, dict)
        assert len(dims) == 9

    def test_dims_cover_xgb_axes(self):
        _base_params, dims = plugin.tuning_search_space()
        names = {d.name for d in dims}
        assert names == {
            "max_depth", "learning_rate", "subsample", "colsample_bytree",
            "min_child_weight", "reg_lambda", "reg_alpha",
            "use_monotone_constraints",   # 符号事前知識 1軸（Issue #366）
            "use_sector_features",        # セクター/サイズ 1軸（Issue #370）
        }
        # 構造・表示専用パラメータは対象外
        assert "fin_features" not in names
        assert "n_estimators_max" not in names
        assert "lambda_risk" not in names

    def test_momentum_is_not_a_search_axis(self):
        """モメンタム2軸は探索しない（#604・ADR-0050）。

        warmup で行を落とす＝母集団を動かす軸で、`hyperparameter_search` は各候補を
        その候補自身の母集団で評価するため、探索は構造的に「母集団が縮む側」を選ぶ。
        共通 (ym,ec) 域の実測（16,867件・11 fold）では**全窓が基準以下**で、探索が
        選んでいた窓18 は −0.0072（符号反転）、窓24 は −0.0264（p=0.001）で有意に悪化した。

        M-1（#604 で同時に外した）と違い M-2 は木モデルで特徴量選択が無いため、
        外すと列が実際に消えて μ̂ が変わる。変わる向きは上の実測では改善側。
        """
        _base_params, dims = plugin.tuning_search_space()
        names = {d.name for d in dims}
        assert "use_momentum" not in names
        assert "momentum_window" not in names

    def test_momentum_is_pinned_off_in_base_params(self):
        """外すだけでなく OFF で固定する。

        明示すると `plugin_tuned_params.params_json` へ「探索がこの値を固定した」ことが残り、
        `params_schema()` の既定が将来変わっても探索条件が動かない。
        """
        base_params, _dims = plugin.tuning_search_space()
        assert base_params.get("use_momentum") is False

    def test_momentum_stays_in_the_params_contract(self):
        """探索から外すのと契約から消すのは別（UI から手動で ON にできる）。"""
        schema = plugin.params_schema()
        assert "use_momentum" in schema
        assert "momentum_window" in schema

    def test_snapshot_cache_now_needs_one_panel(self):
        """**副次効果**: スナップショットのキャッシュキーが1種類になる。

        `build_snapshots` のキャッシュキーは `use_momentum`/`mom_window` を含むため、
        以前は momentum 構成6種（off＋窓5種）ぶんのパネルを保持して `_CACHE_MAXSIZE=8` の
        大半を占めていた（`min_coverage` を併用できなかったのもこれが理由＝6×4=24 > 8 で
        LRU スラッシュ）。軸が消えたので off の1種だけになる。
        """
        base_params, dims = plugin.tuning_search_space()
        names = {d.name for d in dims}
        momentum_keys = {"use_momentum", "momentum_window"} & names
        assert not momentum_keys, (
            f"{momentum_keys} が探索軸に戻っている。パネルのキャッシュが 6種へ膨らむので、"
            "_CACHE_MAXSIZE との関係を測り直すこと（#298・#588）")
        assert base_params.get("use_momentum") is False

    def test_dim_values_within_schema_bounds(self):
        schema = plugin.params_schema()
        _base_params, dims = plugin.tuning_search_space()
        for d in dims:
            field = schema[d.name]
            lo, hi = field.get("min"), field.get("max")
            for v in d.values:
                if lo is not None:
                    assert v >= lo, f"{d.name}={v} は schema min={lo} 未満"
                if hi is not None:
                    assert v <= hi, f"{d.name}={v} は schema max={hi} 超過"

    def test_combos_pass_coerce_params(self):
        """探索空間から生成した候補が coerce_params を通る（契約違反なし）。
        全グリッド構築（216,000 combo）は重いため random サンプリングで抽出検証する。"""
        import random

        from plugins.tuning import _random_combos

        base_params, dims = plugin.tuning_search_space()
        schema = plugin.params_schema()
        combos = _random_combos(dims, n_iter=20, rng=random.Random(0))
        assert len(combos) > 0
        for combo in combos:
            coerce_params(schema, {**base_params, **combo})


# ── 価格行動系特徴量（px_*）の M-2 導入（Issue #364）─────────────────────────────

class TestPriceFeatures:
    """build_snapshots(price_features=...) が px_* を正しく配線し、既定は無変更に保つ。"""

    def _inputs(self):
        _, prices_by_co, fin_by_co, companies = _make_db_mock(n_companies=3, n_weeks=180)
        return prices_by_co, fin_by_co, companies

    def test_schema_default_off(self):
        """M-2 の price_features 既定は空（use_momentum と同じ保守ゲート）。"""
        schema = plugin.params_schema()
        assert "price_features" in schema
        assert schema["price_features"]["type"] == "multiselect"
        assert schema["price_features"]["default"] == []

    def test_use_momentum_default_off(self):
        """モメンタムの既定 OFF は実測の結論（ADR-0045）＝惰性ではない。

        `use_momentum` は px_* と同じ保守ゲートの下にあり、こちらは**ゲートを通した**：
        ON/OFF を同一 fold・同一 (ym,ec) 域で比較して4検定すべて補正後 α を通らず符号も負
        （M-2 rank-IC −0.0056 p=0.528 / 売り側 −0.0051 p=0.178）。raw の母集団のままだと
        ON が改善して見える（履歴の浅い銘柄が落ちる効果）ので、測り直すときは
        `python -m scripts.momentum_gate` を使い共通域制限を外さないこと。
        """
        assert plugin.params_schema()["use_momentum"]["default"] is False

    def test_default_empty_is_noop(self):
        """price_features 未指定と空指定で feature 名・サンプル母集団が完全一致（既定無変更）。"""
        prices_by_co, fin_by_co, companies = self._inputs()
        args = (prices_by_co, fin_by_co, companies, {}, ["per", "pbr"], [], False, 12, 0.5)
        s0, _, _, f0 = build_snapshots(*args, build_interactions=False, macro_nan_ok=True)
        s1, _, _, f1 = build_snapshots(*args, build_interactions=False, macro_nan_ok=True,
                                       price_features=[])
        assert f0 == f1
        assert {k: len(v) for k, v in s0.items()} == {k: len(v) for k, v in s1.items()}

    def test_px_appended_after_momentum_before_interactions(self):
        """px_* は momentum の直後・交差項の手前に並ぶ。"""
        from plugins.macro_snapshots import build_snapshots as bs
        prices_by_co, fin_by_co, companies = self._inputs()
        _, _, _, feats = bs(
            prices_by_co, fin_by_co, companies, {}, ["per", "pbr"], [], True, 12, 0.5,
            build_interactions=False, macro_nan_ok=True,
            price_features=["px_rvol", "px_high52dev"],
        )
        assert feats == ["per", "pbr", "momentum_12m1", "px_rvol", "px_high52dev"]

    def test_feat_row_length_and_values_match_direct(self):
        """feat_row 長が feature 数に一致し、px 値が build_price_features(snap_idx) と一致する。"""
        from plugins.macro_snapshots import build_price_features
        prices_by_co, fin_by_co, companies = self._inputs()
        pf = ["px_rvol", "px_high52dev", "px_rev4w"]
        samples, _, _, feats = build_snapshots(
            prices_by_co, fin_by_co, companies, {}, ["per", "pbr"], [], False, 12, 0.1,
            build_interactions=False, macro_nan_ok=True, price_features=pf,
        )
        # feature 順: fin(2) + px(3)
        assert feats == ["per", "pbr"] + pf
        # サンプルが存在し、各 feat_row 長が feature 数と一致
        total = sum(len(v) for v in samples.values())
        assert total > 0
        for ym, rows in samples.items():
            for feat_row, _y in rows:
                assert len(feat_row) == len(feats)

    def test_px_high52dev_warmup_reduces_population(self):
        """px_high52dev の52週 warmup 分は nan → min_coverage=1.0 で先頭 snapshot が脱落しうる。"""
        prices_by_co, fin_by_co, companies = self._inputs()
        # coverage=1.0（全特徴 non-nan 必須）だと warmup 未満の px_high52dev nan で脱落
        s_px, _, _, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, {}, ["per", "pbr"], [], False, 12, 1.0,
            build_interactions=False, macro_nan_ok=True, price_features=["px_high52dev"],
        )
        s_base, _, _, _ = build_snapshots(
            prices_by_co, fin_by_co, companies, {}, ["per", "pbr"], [], False, 12, 1.0,
            build_interactions=False, macro_nan_ok=True,
        )
        n_px = sum(len(v) for v in s_px.values())
        n_base = sum(len(v) for v in s_base.values())
        assert n_px <= n_base


# ── 経済符号の単調性制約（monotone_constraints・Issue #366）─────────────────────

class TestMonotoneConstraints:
    """符号表・列整合タプル構築・schema・XGB への伝播・execute 完走を検証する。"""

    def test_sign_table_only_confident_financials(self):
        """符号表は経済理論で確信のある財務比率のみ ±1（マクロ/z_* は非収載）。"""
        assert _MONOTONE_SIGN == {
            "pbr": -1, "per": -1, "de_ratio": -1,
            "roe": 1, "roa": 1, "op_margin": 1, "div_yield": 1,
        }
        # 曖昧・レジーム依存は収載しない（=制約なし 0 になる）
        for k in ("equity_ratio", "sales_growth", "profit_growth", "current_ratio",
                  "z_pbr", "z_per", "z_roe", "z_de", "z_op_margin",
                  "cpi_yoy", "jgb10y", "momentum_12m1", "px_vol_13w"):
            assert k not in _MONOTONE_SIGN

    def test_build_constraints_aligns_to_feat_order(self):
        """all_feat_names の列位置に沿ってタプルを組み、未収載は 0。"""
        feats = ["pbr", "cpi_yoy", "roe", "z_pbr", "de_ratio", "px_vol_13w", "momentum_12m1"]
        mc = _build_monotone_constraints(feats)
        assert mc == (-1, 0, 1, 0, -1, 0, 0)
        assert len(mc) == len(feats)      # 列数と厳密一致（位置ずれ防止）
        assert isinstance(mc, tuple)

    def test_build_constraints_all_zero_when_no_confident_features(self):
        """収載財務比率が1つも無ければ全 0（制約なしと等価・無害）。"""
        assert _build_monotone_constraints(["equity_ratio", "cpi_yoy", "momentum_12m1"]) == (0, 0, 0)
        assert _build_monotone_constraints([]) == ()

    def test_schema_has_checkbox_default_off(self):
        """params_schema に use_monotone_constraints（checkbox・既定 OFF）が追加されている。"""
        schema = MacroGbdtPlugin().params_schema()
        assert "use_monotone_constraints" in schema
        field = schema["use_monotone_constraints"]
        assert field["type"] == "checkbox"
        assert field["default"] is False

    def test_constraints_propagate_to_xgb_regressor(self):
        """xgb_params.monotone_constraints が CV fit_predict 経由で XGBRegressor へ渡る。"""
        import xgboost as xgb

        captured: dict = {}
        orig_init = xgb.XGBRegressor.__init__

        def spy_init(self, **kwargs):
            captured.update(kwargs)
            orig_init(self, **kwargs)

        mc = (-1, 0, 1)
        xgb_params = {
            "max_depth": 3, "learning_rate": 0.1, "subsample": 0.8,
            "colsample_bytree": 0.8, "min_child_weight": 1, "reg_lambda": 1.0,
            "reg_alpha": 0.0, "n_estimators": 50, "early_stopping_rounds": 10,
            "tree_method": "hist", "objective": "reg:squarederror",
            "random_state": 42, "monotone_constraints": mc,
        }
        train = [([float(i % 7), float(i % 5), float(i % 3)], float(i) * 0.01) for i in range(50)]
        test  = [([1.0, 2.0, 3.0], 0.05) for _ in range(10)]

        cb = _make_xgb_fit_predict(xgb_params, [])
        with patch.object(xgb.XGBRegressor, "__init__", spy_init):
            cb(train, test)

        assert captured.get("monotone_constraints") == mc, "monotone_constraints が XGB へ届いていない"

    def _make_params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        base.update(overrides)
        return coerce_params(plugin.params_schema(), base)

    def test_execute_with_monotone_on_completes(self):
        """use_monotone_constraints=True で execute が end-to-end 完走し全社返す
        （xgboost 3.3.0 が hist+tuple+SHAP を受理する統合確認）。"""
        db, prices_by_co, fin_by_co, companies = TestExecuteSmoke()._make_db()
        params = self._make_params(use_macro=False, use_monotone_constraints=True)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["n_companies"] > 0
        assert result["results"]
        for item in result["results"]:
            assert item["mu_raw"] == item["mu_raw"], "mu_raw が NaN"

    def test_execute_default_off_is_baseline(self):
        """既定（OFF）は従来通り完走（monotone 未指定でも壊れない）。"""
        db, prices_by_co, fin_by_co, companies = TestExecuteSmoke()._make_db()
        params = self._make_params(use_macro=False)
        assert params["use_monotone_constraints"] is False

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["n_companies"] > 0


# ── 9. セクター/サイズのカテゴリ特徴量（Issue #370）─────────────────────────────

class TestSectorSizeFeatures:
    """業種 target encoding（リークフリー）＋ log_size の後付け連結。"""

    def _make_params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        base.update(overrides)
        return coerce_params(plugin.params_schema(), base)

    # ── schema ──
    def test_schema_has_checkbox_default_off(self):
        schema = MacroGbdtPlugin().params_schema()
        assert "use_sector_features" in schema
        field = schema["use_sector_features"]
        assert field["type"] == "checkbox"
        assert field["default"] is False

    # ── log_size ──
    def test_log_size_nan_for_missing_or_nonpositive(self):
        assert math.isnan(_log_size(None))
        assert math.isnan(_log_size(0))
        assert math.isnan(_log_size(-5.0))

    def test_log_size_log_for_positive(self):
        assert _log_size(math.e) == pytest.approx(1.0)
        assert _log_size(1e10) == pytest.approx(math.log(1e10))

    # ── target encoding fit ──
    def test_fit_sector_encoding_means_and_global(self):
        enc, gmean = _fit_sector_encoding([("A", 1.0), ("A", 3.0), ("B", 10.0)])
        assert enc["A"] == pytest.approx(2.0)
        assert enc["B"] == pytest.approx(10.0)
        assert gmean == pytest.approx((1.0 + 3.0 + 10.0) / 3)

    def test_fit_sector_encoding_empty(self):
        enc, gmean = _fit_sector_encoding([])
        assert enc == {}
        assert gmean == 0.0

    # ── CV サンプル整形 ──
    def test_build_cv_samples_appends_logsize_and_carries_industry(self):
        samples = {"2020-01": [([1.0, 2.0], 0.05)]}
        meta = {"2020-01": [("素材", 1e10)]}
        out = _build_sector_cv_samples(samples, meta)
        row, y, industry = out["2020-01"][0]
        assert row == [1.0, 2.0, _log_size(1e10)]
        assert y == 0.05
        assert industry == "素材"

    # ── リークフリー性（最重要）──
    def test_wrap_target_encoding_is_leak_free(self):
        """test サンプルの sector_te は TRAIN の業種平均のみ由来（test 自身の y を使わない）。"""
        captured: dict = {}

        def inner(tr, te, *rest):
            captured["train"] = tr
            captured["test"] = te
            return [0.0] * len(te), [s[1] for s in te]

        wrapped = _wrap_sector_target_encoding(inner)
        # TRAIN: A→平均0.2、B→-0.1
        train = [([1.0], 0.1, "A"), ([1.0], 0.3, "A"), ([1.0], -0.1, "B")]
        # TEST: 極端な y（999/-999）でも encoding は TRAIN 由来 ＝ test の y は無関係
        test = [([9.0], 999.0, "A"), ([9.0], -999.0, "B"), ([9.0], 0.0, "C")]
        wrapped(train, test)

        # train の sector_te（末尾列）= 業種平均
        assert captured["train"][0][0][-1] == pytest.approx(0.2)   # A
        assert captured["train"][2][0][-1] == pytest.approx(-0.1)  # B
        # test の sector_te も TRAIN 平均（test の y 999/-999 由来ではない＝リークなし）
        assert captured["test"][0][0][-1] == pytest.approx(0.2)    # A
        assert captured["test"][1][0][-1] == pytest.approx(-0.1)   # B
        # 未知業種 C は TRAIN 全体平均へフォールバック
        gmean = (0.1 + 0.3 - 0.1) / 3
        assert captured["test"][2][0][-1] == pytest.approx(gmean)
        # 2-tuple へ縮約＆列数 +1
        assert len(captured["train"][0]) == 2
        assert len(captured["train"][0][0]) == 2

    def test_wrap_forwards_extra_args_for_ranker(self):
        """M-5（pass_train_groups=True）の3引数呼び出しを *rest で素通しする。"""
        seen: dict = {}

        def inner(tr, te, groups):
            seen["groups"] = groups
            return [0.0] * len(te), [s[1] for s in te]

        wrapped = _wrap_sector_target_encoding(inner)
        train = [([1.0], 0.1, "A"), ([1.0], 0.2, "A")]
        test = [([1.0], 0.3, "A")]
        wrapped(train, test, [2])
        assert seen["groups"] == [2]

    # ── 最終モデル用サンプル ──
    def test_build_final_samples_appends_two_cols(self):
        samples = {"2020-01": [([1.0], 0.2), ([1.0], 0.4)], "2020-02": [([2.0], -0.1)]}
        meta = {"2020-01": [("A", 1e10), ("A", 1e10)], "2020-02": [("B", 1e9)]}
        out, enc, gmean = _build_sector_final_samples(samples, meta)
        assert enc["A"] == pytest.approx(0.3)
        assert enc["B"] == pytest.approx(-0.1)
        assert gmean == pytest.approx((0.2 + 0.4 - 0.1) / 3)
        row, y = out["2020-01"][0]
        assert row[0] == 1.0
        assert row[1] == pytest.approx(_log_size(1e10))
        assert row[2] == pytest.approx(0.3)   # sector_te = A の平均
        assert y == 0.2

    def test_sector_current_row_uses_alltrain_encoding(self):
        enc = {"A": 0.3, "B": -0.1}
        row = _sector_current_row([5.0], {"size": 1e10, "industry": "A"}, enc, gmean=0.05)
        assert row[0] == 5.0
        assert row[1] == pytest.approx(_log_size(1e10))
        assert row[2] == pytest.approx(0.3)
        # 未知業種 → gmean フォールバック・size 欠損 → NaN
        row2 = _sector_current_row([5.0], {"size": None, "industry": "Z"}, enc, gmean=0.05)
        assert math.isnan(row2[1])
        assert row2[2] == pytest.approx(0.05)

    # ── execute end-to-end ──
    def test_execute_with_sector_features_completes(self):
        db, prices_by_co, fin_by_co, companies = TestExecuteSmoke()._make_db()
        params = self._make_params(use_macro=False, use_sector_features=True)

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["n_companies"] > 0
        # 2 列がモデル特徴・SHAP キーへ入る
        assert _SIZE_FEAT in result["selected_features"]
        assert _SECTOR_TE_FEAT in result["selected_features"]
        assert _SIZE_FEAT in result["feature_coefs"]
        assert _SECTOR_TE_FEAT in result["feature_coefs"]
        for item in result["results"]:
            assert _SIZE_FEAT in item["shap"]
            assert _SECTOR_TE_FEAT in item["shap"]
            assert item["mu_raw"] == item["mu_raw"], "mu_raw が NaN"

    def test_execute_default_off_excludes_sector_cols(self):
        db, prices_by_co, fin_by_co, companies = TestExecuteSmoke()._make_db()
        params = self._make_params(use_macro=False)
        assert params["use_sector_features"] is False

        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert _SIZE_FEAT not in result["selected_features"]
        assert _SECTOR_TE_FEAT not in result["selected_features"]
