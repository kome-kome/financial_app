"""tests/test_macro_dlm.py — M-3 MacroDlmPlugin（ベイズ状態空間・割引 DLM）フルテスト

テスト観点:
  1. meta    : プラグイン登録メタ（name/label/heavy/category/ui_order/depends_on）
  2. coerce  : params_schema の bounds/membership が reject する・既定因子4本
  3. filter  : dlm_filter が既知の α/β を合成データから回収・出力が有限
  4. calib   : 正しく特定された合成データで標準化予測誤差の分散 ≈ 1（校正）
  5. smoke   : execute が必要キー・µ̂ 降順・top_n 経路付与・診断を満たす（DBモック）
  6. guard   : 空株価/空マクロ/週数不足で ValueError
"""
import asyncio
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from plugins.macro_dlm import (
    MacroDlmPlugin, dlm_filter, load_prices, load_macro_levels,
    DEFAULT_MACRO_FEATURES, MACRO_FEATURE_OPTIONS, _DLM_MACRO_MAP,
    _downsample_idx,
)
from plugins.utils import coerce_params

plugin = MacroDlmPlugin()


# ── 1. meta ──────────────────────────────────────────────────────────────────

class TestPluginMeta:
    def test_name(self):
        assert plugin.name == "macro_dlm"

    def test_is_heavy(self):
        assert plugin.heavy is True

    def test_category(self):
        assert plugin.category == "③ 将来リターンを予測"

    def test_ui_order(self):
        assert plugin.ui_order == 360

    def test_depends_on_empty(self):
        assert plugin.depends_on == []

    def test_default_factors_are_four(self):
        assert len(DEFAULT_MACRO_FEATURES) == 4
        assert "dlm_nikkei225" in DEFAULT_MACRO_FEATURES   # 市場ファクターを含む

    def test_all_defaults_are_valid_options(self):
        valid = {o["value"] for o in MACRO_FEATURE_OPTIONS}
        assert set(DEFAULT_MACRO_FEATURES) <= valid

    def test_to_meta_has_required_keys(self):
        meta = plugin.to_meta()
        for k in ("name", "label", "heavy", "category", "ui_order", "params_schema"):
            assert k in meta

    def test_registered_in_registry(self):
        import plugins as plugin_registry
        assert plugin_registry.get_plugin("macro_dlm") is not None


# ── 2. coerce ────────────────────────────────────────────────────────────────

class TestCoerce:
    schema = plugin.params_schema()

    def _coerce(self, raw):
        return coerce_params(self.schema, raw)

    def _defaults(self):
        return {k: v["default"] for k, v in self.schema.items() if "default" in v}

    def test_defaults_valid(self):
        result = self._coerce(self._defaults())
        assert result["macro_features"] == DEFAULT_MACRO_FEATURES
        assert result["state_discount"] == 0.98

    def test_state_discount_out_of_bounds_rejected(self):
        raw = self._defaults()
        raw["state_discount"] = 1.5
        with pytest.raises(ValueError, match="state_discount"):
            self._coerce(raw)

    def test_state_discount_below_min_rejected(self):
        raw = self._defaults()
        raw["state_discount"] = 0.5
        with pytest.raises(ValueError, match="state_discount"):
            self._coerce(raw)

    def test_min_weeks_out_of_bounds_rejected(self):
        raw = self._defaults()
        raw["min_weeks"] = 5
        with pytest.raises(ValueError, match="min_weeks"):
            self._coerce(raw)

    def test_top_n_out_of_bounds_rejected(self):
        raw = self._defaults()
        raw["top_n"] = 9999
        with pytest.raises(ValueError, match="top_n"):
            self._coerce(raw)

    def test_invalid_macro_feature_rejected(self):
        raw = self._defaults()
        raw["macro_features"] = ["dlm_usdjpy", "dlm_unknown_xyz"]
        with pytest.raises(ValueError, match="macro_features"):
            self._coerce(raw)

    def test_min_weeks_is_int(self):
        result = self._coerce(self._defaults())
        assert isinstance(result["min_weeks"], int)


# ── 3. filter: 合成データからの係数回収 ───────────────────────────────────────

class TestDlmFilterRecovery:
    def test_recovers_known_static_coefficients(self):
        """δ≈1（ほぼ静的）で生成した既知 α/β を最終フィルタ推定が回収する。"""
        rng = np.random.default_rng(42)
        T = 800
        true_alpha, true_b1, true_b2 = 0.0005, 0.7, -1.2
        X = []
        y = []
        for _ in range(T):
            m1 = float(rng.normal(0, 0.05))
            m2 = float(rng.normal(0, 0.05))
            noise = float(rng.normal(0, 0.01))
            X.append([1.0, m1, m2])
            y.append(true_alpha + true_b1 * m1 + true_b2 * m2 + noise)

        res = dlm_filter(y, X, delta=0.999, beta_v=1.0)
        m = res["m"]
        assert abs(m[0] - true_alpha) < 0.005, f"α 回収失敗: {m[0]}"
        assert abs(m[1] - true_b1) < 0.2, f"β1 回収失敗: {m[1]}"
        assert abs(m[2] - true_b2) < 0.2, f"β2 回収失敗: {m[2]}"

    def test_output_shapes_and_finite(self):
        rng = np.random.default_rng(1)
        T, p = 120, 3
        X = [[1.0] + list(rng.normal(0, 0.04, p - 1)) for _ in range(T)]
        y = list(rng.normal(0, 0.02, T))
        res = dlm_filter(y, X, delta=0.97, beta_v=0.98)
        assert res["m_path"].shape == (T, p)
        assert res["sd_path"].shape == (T, p)
        assert res["std_errs"].shape == (T,)
        assert np.all(np.isfinite(res["m_path"]))
        assert np.all(np.isfinite(res["sd_path"]))
        assert np.all(res["sd_path"] >= 0)

    def test_tracks_changing_beta(self):
        """β が途中で変化する系列で、最終推定が後半の真値側に寄る（時変追従）。"""
        rng = np.random.default_rng(7)
        T = 600
        X, y = [], []
        for t in range(T):
            m1 = float(rng.normal(0, 0.06))
            b = 0.5 if t < T // 2 else 2.0          # 後半で感応度が上昇
            X.append([1.0, m1])
            y.append(b * m1 + float(rng.normal(0, 0.008)))
        res = dlm_filter(y, X, delta=0.95, beta_v=0.98)
        assert res["m"][1] > 1.2, f"後半の高い β に追従していない: {res['m'][1]}"


# ── 4. calib: 標準化予測誤差の校正 ────────────────────────────────────────────

class TestCalibration:
    def test_standardized_error_variance_near_one(self):
        """正しく特定された合成データで mean(std_err²) ≈ 1（予測分散が妥当）。"""
        rng = np.random.default_rng(123)
        T = 1000
        X, y = [], []
        for _ in range(T):
            m1 = float(rng.normal(0, 0.05))
            X.append([1.0, m1])
            y.append(0.001 + 0.6 * m1 + float(rng.normal(0, 0.012)))
        res = dlm_filter(y, X, delta=0.999, beta_v=1.0)
        se = res["std_errs"][100:]               # バーンイン除外
        v = float(np.mean(se ** 2))
        assert 0.6 < v < 1.6, f"標準化予測誤差の分散が校正範囲外: {v}"


# ── 5. smoke: execute（DBモック）─────────────────────────────────────────────

def _weekly_dates(n_weeks: int, start=(2018, 1, 5)):
    import datetime
    base = datetime.date(*start)
    return [(base + datetime.timedelta(weeks=w)).isoformat() for w in range(n_weeks)]


def _make_macro_levels(dates, seed=0):
    """既定4因子の系列コードに対し forward-fill 用 (dates, vals) を生成。"""
    rng = np.random.default_rng(seed)
    n = len(dates)
    levels = {
        "USDJPY":    140.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n))),
        "NIKKEI225": 28000.0 * np.exp(np.cumsum(rng.normal(0, 0.015, n))),
        "WTI":       75.0 * np.exp(np.cumsum(rng.normal(0, 0.03, n))),
        "US10Y":     4.0 + np.cumsum(rng.normal(0, 0.05, n)),
    }
    return {sc: (list(dates), list(map(float, v))) for sc, v in levels.items()}


def _make_prices_companies(n_companies, dates, seed=10):
    rng = np.random.default_rng(seed)
    prices_by_co, companies = {}, {}
    for ci in range(n_companies):
        ec = f"E{ci:05d}"
        rets = rng.normal(0.001 + ci * 0.0003, 0.02, len(dates))
        close = 1000.0
        rows = []
        for d, r in zip(dates, rets):
            close *= math.exp(float(r))
            rows.append(SimpleNamespace(trade_date=d, close_last=close))
        prices_by_co[ec] = rows
        companies[ec] = SimpleNamespace(
            edinet_code=ec, name=f"会社{ci}", sec_code=str(1000 + ci), industry="テスト業",
        )
    return prices_by_co, companies


class TestExecuteSmoke:
    def _params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        params = coerce_params(plugin.params_schema(), base)
        params.update(overrides)
        return params

    def _run(self, n_companies=5, n_weeks=90, **pover):
        dates = _weekly_dates(n_weeks)
        prices_by_co, companies = _make_prices_companies(n_companies, dates)
        macro_levels = _make_macro_levels(dates)
        opts = {"min_weeks": 40, "burn_in_weeks": 5, "top_n": 3}
        opts.update(pover)
        params = self._params(**opts)
        db = MagicMock()
        with patch("plugins.macro_dlm.load_prices", return_value=(prices_by_co, companies)), \
             patch("plugins.macro_dlm.load_macro_levels", return_value=macro_levels):
            return asyncio.run(plugin.execute(params, db))

    def test_required_keys(self):
        res = self._run()
        for k in ("model_type", "macro_features", "factor_labels", "params",
                  "n_companies", "diagnostics", "results"):
            assert k in res, f"出力に '{k}' がない"
        assert res["model_type"] == "bayesian_dlm"

    def test_results_capped_at_top_n(self):
        res = self._run(n_companies=5, top_n=3)
        assert len(res["results"]) == 3
        assert res["n_companies"] == 5

    def test_results_sorted_by_mu_desc(self):
        res = self._run(n_companies=6, top_n=6)
        mus = [r["mu"] for r in res["results"]]
        assert mus == sorted(mus, reverse=True), "µ̂ 降順でない"

    def test_result_row_structure(self):
        res = self._run()
        r0 = res["results"][0]
        for k in ("edinet_code", "company_name", "mu", "mu_ci", "alpha_weekly",
                  "n_weeks", "beta_latest", "path"):
            assert k in r0, f"results 行に '{k}' がない"
        # beta_latest は4因子ぶん
        assert set(r0["beta_latest"].keys()) == set(DEFAULT_MACRO_FEATURES)
        for f, b in r0["beta_latest"].items():
            assert set(b.keys()) == {"mean", "lo", "hi"}

    def test_path_structure(self):
        res = self._run()
        path = res["results"][0]["path"]
        assert "dates" in path and "alpha" in path and "beta" in path
        n = len(path["dates"])
        assert n > 0
        for key in ("mean", "lo", "hi"):
            assert len(path["alpha"][key]) == n
        for f in DEFAULT_MACRO_FEATURES:
            assert len(path["beta"][f]["mean"]) == n

    def test_diagnostics_present(self):
        res = self._run()
        diag = res["diagnostics"]
        for k in ("calibration", "pred_rmse", "coverage95", "n_companies_scored"):
            assert k in diag

    def test_factor_labels_match(self):
        res = self._run()
        assert set(res["factor_labels"].keys()) == set(DEFAULT_MACRO_FEATURES)


# ── 6. guard: 異常系 ─────────────────────────────────────────────────────────

class TestGuards:
    def _params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        params = coerce_params(plugin.params_schema(), base)
        params.update(overrides)
        return params

    def test_empty_prices_raises(self):
        db = MagicMock()
        with patch("plugins.macro_dlm.load_prices", return_value=({}, {})), \
             patch("plugins.macro_dlm.load_macro_levels", return_value={}):
            with pytest.raises(ValueError, match="株価週次履歴"):
                asyncio.run(plugin.execute(self._params(), db))

    def test_empty_macro_raises(self):
        dates = _weekly_dates(90)
        prices_by_co, companies = _make_prices_companies(3, dates)
        db = MagicMock()
        with patch("plugins.macro_dlm.load_prices", return_value=(prices_by_co, companies)), \
             patch("plugins.macro_dlm.load_macro_levels", return_value={}):
            with pytest.raises(ValueError, match="マクロデータ"):
                asyncio.run(plugin.execute(self._params(), db))

    def test_insufficient_weeks_raises(self):
        dates = _weekly_dates(50)
        prices_by_co, companies = _make_prices_companies(3, dates)
        macro_levels = _make_macro_levels(dates)
        db = MagicMock()
        params = self._params(min_weeks=104)   # 50 週 < 104
        with patch("plugins.macro_dlm.load_prices", return_value=(prices_by_co, companies)), \
             patch("plugins.macro_dlm.load_macro_levels", return_value=macro_levels):
            with pytest.raises(ValueError, match="推定可能な銘柄がありません"):
                asyncio.run(plugin.execute(params, db))

    def test_no_macro_features_raises(self):
        db = MagicMock()
        params = self._params()
        params["macro_features"] = []
        with pytest.raises(ValueError, match="マクロ・ファクター"):
            asyncio.run(plugin.execute(params, db))


# ── 7. ヘルパ ────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_downsample_includes_endpoints(self):
        idx = _downsample_idx(1000, 120)
        assert idx[0] == 0
        assert idx[-1] == 999
        assert len(idx) <= 120

    def test_downsample_small_returns_all(self):
        assert _downsample_idx(10, 120) == list(range(10))

    def test_macro_map_kinds_valid(self):
        for scode, kind, label in _DLM_MACRO_MAP.values():
            assert kind in ("logret", "diff")
            assert isinstance(label, str) and label
