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

    def test_oof_backtest_keys_present(self):
        """execute 結果に oof_backtest キーと必須サブキーが含まれる。"""
        res = self._run(n_companies=5, n_weeks=90)
        assert "oof_backtest" in res, "oof_backtest キーがない"
        oof = res["oof_backtest"]
        for k in ("n_quantiles", "n_periods", "n_periods_quantile", "n_oof_samples",
                  "quantile_returns", "rank_ic", "long_short_spread", "hit_rate"):
            assert k in oof, f"oof_backtest に '{k}' がない"
        assert isinstance(oof["rank_ic"], dict)
        for k in ("mean", "std", "n"):
            assert k in oof["rank_ic"], f"rank_ic に '{k}' がない"

    def test_oof_n_oof_samples_positive(self):
        """OOF サンプルが1件以上集まる（バーンイン後に週次ペアが存在する）。"""
        res = self._run(n_companies=5, n_weeks=90, burn_in_weeks=5, min_weeks=40)
        assert res["oof_backtest"]["n_oof_samples"] > 0

    def test_oof_rank_ic_n_positive(self):
        """rank-IC の fold 数が正（月次グルーピングで複数の fold が生成される）。"""
        res = self._run(n_companies=5, n_weeks=90, burn_in_weeks=5, min_weeks=40)
        assert res["oof_backtest"]["rank_ic"]["n"] > 0


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


# ── 8. OOF: アウトオブサンプル検証（ADR-0004）────────────────────────────────

class TestOofBacktest:
    """Issue #240: M-3 が oof_backtest を呼び、α の順序付け能力を評価できること。"""

    def _run_ordered(self, n_companies: int = 20, n_weeks: int = 260):
        """α_true[i] = i * 0.001 の完全順序合成データで execute を実行し結果を返す。

        ノイズを極小（0.0005）にすることで rank-IC が正（α 推定が順序を回収）になることを確認。
        マクロ不使用（F = [1]・定数項のみ）になるよう 'dlm_nikkei225' のみ選択し、
        マクロ水準を全期間一定（→ Δmacro ≈ 0）にしてαに集中させる。
        """
        import datetime
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch as _patch

        base = datetime.date(2018, 1, 5)
        dates = [(base + datetime.timedelta(weeks=w)).isoformat() for w in range(n_weeks)]

        # α_true の階段: 銘柄 i は日次ドリフト = i * 0.001 / 52
        # 意図的に全銘柄の週次リターンが α_true に比例するよう設計
        rng = np.random.default_rng(999)
        prices_by_co, companies = {}, {}
        for ci in range(n_companies):
            alpha_true = ci * 0.001          # 週次アルファ
            ec = f"EO{ci:04d}"
            close = 1000.0
            rows = []
            for d in dates:
                ret = alpha_true + float(rng.normal(0, 0.0005))   # ノイズ極小
                close *= math.exp(ret)
                rows.append(SimpleNamespace(trade_date=d, close_last=close))
            prices_by_co[ec] = rows
            companies[ec] = SimpleNamespace(
                edinet_code=ec, name=f"会社{ci}", sec_code=str(2000 + ci), industry="テスト業",
            )

        # マクロ: NIKKEI225 を定数（→ Δlogret ≈ 0）にして β の影響を消す
        nikkei_level = 28000.0
        macro_levels = {
            "NIKKEI225": (list(dates), [nikkei_level] * n_weeks),
        }

        base_params = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        from plugins.utils import coerce_params as _coerce
        params = _coerce(plugin.params_schema(), base_params)
        params.update({
            "macro_features": ["dlm_nikkei225"],
            "min_weeks": 52, "burn_in_weeks": 26, "top_n": n_companies,
            "state_discount": 0.98,
        })

        db = MagicMock()
        with _patch("plugins.macro_dlm.load_prices", return_value=(prices_by_co, companies)), \
             _patch("plugins.macro_dlm.load_macro_levels", return_value=macro_levels):
            import asyncio
            return asyncio.run(plugin.execute(params, db))

    def test_oof_rank_ic_positive_with_ordered_data(self):
        """α_true が完全昇順の合成データで rank-IC の mean が正になる（予測力あり）。"""
        res = self._run_ordered()
        oof = res["oof_backtest"]
        assert oof["rank_ic"]["n"] > 0, "fold が0件: OOF ペアが収集されていない"
        ic_mean = oof["rank_ic"]["mean"]
        assert ic_mean is not None, "rank_ic.mean が None"
        assert ic_mean > 0, f"α が完全順序なのに rank-IC が非正: {ic_mean}"

    def test_oof_quantile_returns_monotone_with_ordered_data(self):
        """α 完全順序データで分位リターンが Q1 < Q5 の傾向を持つ（単調増加）。"""
        res = self._run_ordered(n_companies=20)
        oof = res["oof_backtest"]
        qr = oof["quantile_returns"]
        if not qr:
            pytest.skip("n_quantiles * 2 銘柄未満で分位計算スキップ（データ不足）")
        assert qr[-1] > qr[0], f"高 α 分位が低 α 分位を上回らない: {qr}"


# ── 9. producer: write→read round-trip + sell_ranking 連携 ───────────────────

class TestProducer:
    """Issue #238: M-3 producer 化（produced_output / read_producer_scores / execute 永続化）"""

    def _make_db(self, stored_mus: dict | None = None):
        """macro_dlm_scores の read/write をシミュレートする軽量 DB モック。"""
        store: dict = {}
        if stored_mus:
            store.update(stored_mus)

        db = MagicMock()

        # replace_macro_dlm_scores が呼ばれたら store を更新
        def _replace(rows, snapshot_date=None):
            store.clear()
            for r in rows:
                if r.get("edinet_code") and r.get("mu") is not None:
                    store[r["edinet_code"]] = r["mu"]
            return len(store)

        # get_macro_dlm_scores が呼ばれたら store を返す
        def _get():
            return dict(store)

        return db, store, _replace, _get

    def test_produced_output_false_when_empty(self):
        with patch("database.get_macro_dlm_scores", return_value={}):
            assert plugin.produced_output(MagicMock()) is False

    def test_produced_output_true_when_data_exists(self):
        with patch("database.get_macro_dlm_scores", return_value={"E00001": 0.05}):
            assert plugin.produced_output(MagicMock()) is True

    def test_read_producer_scores_empty_when_no_data(self):
        with patch("database.get_macro_dlm_scores", return_value={}):
            result = plugin.read_producer_scores(MagicMock())
        assert result == {}

    def test_read_producer_scores_shape(self):
        stored = {"E00001": 0.12, "E00002": -0.05}
        with patch("database.get_macro_dlm_scores", return_value=stored), \
             patch("plugins.macro_dlm.MacroDlmPlugin.read_producer_scores.__func__",
                   side_effect=None, create=True):
            # macro_snapshots の get_producer_scores は r_macro を返すがここでは空でよい
            with patch("plugins.macro_snapshots.get_producer_scores", return_value={}):
                result = plugin.read_producer_scores(MagicMock())
        assert set(result.keys()) == {"E00001", "E00002"}
        for ec, v in result.items():
            assert set(v.keys()) == {"mu", "r_macro", "r1_prime"}
            assert v["r1_prime"] is None
            assert isinstance(v["mu"], float)

    def test_read_producer_scores_merges_r_macro(self):
        stored = {"E00001": 0.10}
        r_macro_src = {"E00001": {"mu": 0.0, "r_macro": 0.25, "r1_prime": 0.03}}
        with patch("database.get_macro_dlm_scores", return_value=stored), \
             patch("plugins.macro_snapshots.get_producer_scores", return_value=r_macro_src):
            result = plugin.read_producer_scores(MagicMock())
        assert result["E00001"]["r_macro"] == pytest.approx(0.25)
        assert result["E00001"]["mu"] == pytest.approx(0.10)
        assert result["E00001"]["r1_prime"] is None

    def test_execute_persists_scores(self):
        """execute が replace_macro_dlm_scores を呼び、全銘柄の mu を永続化する。"""
        dates = _weekly_dates(90)
        n_co = 5
        prices_by_co, companies = _make_prices_companies(n_co, dates)
        macro_levels = _make_macro_levels(dates)
        params = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        from plugins.utils import coerce_params
        params = coerce_params(plugin.params_schema(), params)
        params.update({"min_weeks": 40, "burn_in_weeks": 5, "top_n": 3})

        persisted: list[dict] = []

        def fake_replace(db, rows, snapshot_date=None):
            persisted.extend(rows)
            return len(rows)

        db = MagicMock()
        with patch("plugins.macro_dlm.load_prices", return_value=(prices_by_co, companies)), \
             patch("plugins.macro_dlm.load_macro_levels", return_value=macro_levels), \
             patch("database.replace_macro_dlm_scores", side_effect=fake_replace):
            asyncio.run(plugin.execute(params, db))

        assert len(persisted) == n_co, f"全 {n_co} 銘柄を保存すべきが {len(persisted)} 件"
        for row in persisted:
            assert "edinet_code" in row
            assert "mu" in row and row["mu"] is not None

    def test_execute_persists_all_not_just_top_n(self):
        """top_n=2 でも全銘柄が永続化される（ランキング外銘柄の除外なし）。"""
        dates = _weekly_dates(90)
        n_co = 6
        prices_by_co, companies = _make_prices_companies(n_co, dates)
        macro_levels = _make_macro_levels(dates)
        params = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        from plugins.utils import coerce_params
        params = coerce_params(plugin.params_schema(), params)
        params.update({"min_weeks": 40, "burn_in_weeks": 5, "top_n": 2})

        persisted: list[dict] = []

        def fake_replace(db, rows, snapshot_date=None):
            persisted.extend(rows)

        db = MagicMock()
        with patch("plugins.macro_dlm.load_prices", return_value=(prices_by_co, companies)), \
             patch("plugins.macro_dlm.load_macro_levels", return_value=macro_levels), \
             patch("database.replace_macro_dlm_scores", side_effect=fake_replace):
            asyncio.run(plugin.execute(params, db))

        assert len(persisted) == n_co, f"全 {n_co} 銘柄を保存すべきが {len(persisted)} 件（top_n=2 でも全件）"

    def test_sell_ranking_mu_source_schema_has_macro_dlm(self):
        """sell_ranking の mu_source に macro_dlm が含まれる。"""
        from plugins.sell_ranking import SellRankingPlugin
        sell = SellRankingPlugin()
        schema = sell.params_schema()
        opts = {o["value"] for o in schema["mu_source"]["options"]}
        assert "macro_dlm" in opts

    def test_sell_ranking_reads_macro_dlm_scores(self):
        """sell_ranking が mu_source=macro_dlm のとき M-3 の read_producer_scores を呼ぶ。"""
        from plugins.sell_ranking import SellRankingPlugin
        import asyncio as _asyncio
        from unittest.mock import patch as _patch, MagicMock as _MM, AsyncMock

        sell = SellRankingPlugin()
        # 最低限の execute 引数
        params_raw = {k: v.get("default") for k, v in sell.params_schema().items()}
        params_raw["holdings"] = "7203"
        params_raw["mu_source"] = "macro_dlm"
        from plugins.utils import coerce_params
        params = coerce_params(sell.params_schema(), params_raw)

        read_called = []

        class FakeDlmPlugin:
            def produced_output(self, db):
                return True

            def read_producer_scores(self, db, macro_snapshot=None):
                read_called.append(True)
                return {}

        db = _MM()
        # FinancialMetric / latest_year_subq / StockPriceWeekly のモック
        db.query.return_value.join.return_value.filter.return_value.all.return_value = []
        db.query.return_value.join.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []

        with _patch("plugins._registry", {"macro_dlm": FakeDlmPlugin()}), \
             _patch("plugins.sell_ranking.SellRankingPlugin.execute",
                    wraps=sell.execute):
            # get_plugin がモックを返すように
            with _patch("plugins.sell_ranking.__import__",
                        side_effect=ImportError, create=True):
                pass  # import は通常通り走る

        # 実際に execute を呼んで macro_dlm plugin の read_producer_scores が呼ばれるか確認
        with _patch("plugins.get_plugin", return_value=FakeDlmPlugin()), \
             _patch("database.FinancialMetric", create=True), \
             _patch("database.StockPriceWeekly", create=True), \
             _patch("database.latest_year_subq", return_value=_MM()):
            try:
                _asyncio.run(sell.execute(params, db))
            except Exception:
                pass  # DB モックが不完全でも read_called が確認できればよい

        assert read_called, "mu_source=macro_dlm のとき read_producer_scores が呼ばれなかった"
