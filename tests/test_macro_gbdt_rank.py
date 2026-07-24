"""tests/test_macro_gbdt_rank.py — M-5 MacroGbdtRankPlugin テスト（Issue #362 / ADR-0017）

M-2（macro_gbdt）の rank-IC 整合版。学習目的を MSE→learning-to-rank（rank:pairwise 既定）へ
差し替えた兄弟モデル。execute() 本体は M-2 から継承し、4フックのみ override する。

テスト観点:
  1. meta   : 登録・ui_order=380・producer を持たない（produced_output=False / read=空）
  2. coerce : objective select の membership 検証・M-2 スキーマ継承
  3. labels : _prep_rank_labels（pairwise 素通し / ndcg 非負グレード・上限クリップ）
  4. cv     : walk_forward_cv_monthly(pass_train_groups=True) が 3 引数コールバックで XGBRanker を
              月クエリグループ学習し、oof_backtest で rank-IC を出す（DB 非依存・合成データ）
  5. compat : pass_train_groups=False（既定）は従来の 2 引数呼び出しで不変（M-2 回帰保護）
  6. smoke  : execute が model_type=xgboost_ranker を返し producer を永続化しない
"""
import datetime
from collections import defaultdict
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from plugins.macro_gbdt_rank import (
    MacroGbdtRankPlugin,
    _make_xgb_rank_fit_predict,
    _prep_rank_labels,
    _NDCG_GRADES,
)
from plugins.utils import coerce_params, walk_forward_cv_monthly
from plugins.macro_snapshots import oof_backtest
# M-2 スモーク用 DB ビルダーを流用（同一母集団で純比較の前提を共有）。
# エイリアスは Test* を避ける（pytest による二重 collection 防止）。
from tests.test_macro_gbdt import TestExecuteSmoke as _M2Smoke

plugin = MacroGbdtRankPlugin()


# ── 1. meta ───────────────────────────────────────────────────────────────────

class TestPluginMeta:
    def test_registered_in_registry(self):
        import plugins as reg
        assert isinstance(reg.get_plugin("macro_gbdt_rank"), MacroGbdtRankPlugin)

    def test_name_label(self):
        assert plugin.name == "macro_gbdt_rank"
        assert plugin.label.startswith("M-5")

    def test_ui_order_after_m4(self):
        assert plugin.ui_order == 380

    def test_category(self):
        assert plugin.category == "③ 将来リターンを予測"

    def test_heavy(self):
        assert plugin.heavy is True

    def test_no_producer_output(self):
        """順位スコアはリターン単位でないため producer を持たない（sell_ranking へ流さない）。"""
        assert plugin.produced_output(MagicMock()) is False

    def test_read_producer_scores_empty(self):
        assert plugin.read_producer_scores(MagicMock(), None) == {}

    def test_in_comparison_models(self):
        import model_comparison
        assert ("macro_gbdt_rank", "M-5") in model_comparison.COMPARISON_MODELS


# ── 2. coerce ─────────────────────────────────────────────────────────────────

class TestCoerce:
    schema = plugin.params_schema()

    def _coerce(self, raw):
        return coerce_params(self.schema, raw)

    def test_objective_default_pairwise(self):
        c = self._coerce({})
        assert c["objective"] == "rank:pairwise"

    def test_objective_ndcg_accepted(self):
        c = self._coerce({"objective": "rank:ndcg"})
        assert c["objective"] == "rank:ndcg"

    def test_invalid_objective_rejected(self):
        with pytest.raises(ValueError, match="objective"):
            self._coerce({"objective": "reg:squarederror"})

    def test_inherits_m2_schema_fields(self):
        """M-2 の主要フィールド（max_depth / fin_features / lambda_risk）を継承している。"""
        for k in ("max_depth", "fin_features", "lambda_risk", "learning_rate"):
            assert k in self.schema

    def test_m2_bounds_still_enforced(self):
        raw = {k: v["default"] for k, v in self.schema.items() if "default" in v}
        raw["max_depth"] = 100
        with pytest.raises(ValueError, match="max_depth"):
            self._coerce(raw)


# ── 3. labels ─────────────────────────────────────────────────────────────────

class TestPrepRankLabels:
    def test_pairwise_passthrough(self):
        y = np.array([0.1, -0.2, 0.3, -0.5])
        out = _prep_rank_labels(y, [4], "rank:pairwise")
        assert np.array_equal(out, y)

    def test_ndcg_non_negative_and_bounded(self):
        y = np.array([0.1, -0.2, 0.3, -0.5, 0.05, 0.4, -0.1, 0.2])
        out = _prep_rank_labels(y, [4, 4], "rank:ndcg")
        assert out.min() >= 0.0
        assert out.max() <= _NDCG_GRADES - 1

    def test_ndcg_preserves_within_group_order(self):
        """グレード化後も期内順序が保たれる（最大リターンが最大グレード）。"""
        y = np.array([0.1, 0.9, 0.5, 0.3])
        out = _prep_rank_labels(y, [4], "rank:ndcg")
        assert out.argmax() == y.argmax()
        assert out.argmin() == y.argmin()

    def test_ndcg_large_group_grades_capped(self):
        """500 銘柄でもグレードは _NDCG_GRADES 未満（2^rel オーバーフロー回避）。"""
        y = np.arange(500, dtype=float)
        out = _prep_rank_labels(y, [500], "rank:ndcg")
        assert out.max() <= _NDCG_GRADES - 1
        assert out.min() >= 0.0

    def test_singleton_group(self):
        out = _prep_rank_labels(np.array([0.5]), [1], "rank:ndcg")
        assert out[0] == 0.0


# ── 4. cv（DB 非依存・合成データ）─────────────────────────────────────────────

def _synthetic_samples(n_months=15, n_stocks=50, n_feat=4, seed=0):
    rng = np.random.default_rng(seed)
    w = np.array([1.0, -0.7, 0.4, 0.0])[:n_feat]
    samples_by_ym = {}
    for mi in range(n_months):
        ym = f"2020-{mi+1:02d}" if mi < 12 else f"2021-{mi-11:02d}"
        rows = []
        for _ in range(n_stocks):
            x = rng.normal(size=n_feat)
            yv = float(x @ w + rng.normal(scale=0.5))
            rows.append((x.tolist(), yv))
        samples_by_ym[ym] = rows
    return samples_by_ym


_XGB_PARAMS = {
    "max_depth": 3, "learning_rate": 0.1, "subsample": 0.8, "colsample_bytree": 0.8,
    "min_child_weight": 5, "reg_lambda": 1.0, "reg_alpha": 0.0,
    "n_estimators": 60, "early_stopping_rounds": 40,
    "tree_method": "hist", "objective": "rank:pairwise", "random_state": 42,
}


class TestRankCv:
    def test_pass_train_groups_invokes_3arg_callback(self):
        """pass_train_groups=True で fit_predict が 3 引数（train_groups 付き）で呼ばれ、
        train_groups の合計が train_samples 件数と一致する。"""
        received = {}

        def spy(train_samples, test_samples, train_groups):
            received["groups"] = train_groups
            received["n_train"] = len(train_samples)
            return [0.0] * len(test_samples), [t for _, t in test_samples]

        s = _synthetic_samples()
        walk_forward_cv_monthly(
            s, ["f0", "f1", "f2", "f3"],
            min_train_months=6, step_months=3, return_residuals=False,
            fit_predict=spy, embargo_months=0, pass_train_groups=True,
        )
        assert sum(received["groups"]) == received["n_train"]
        assert all(g > 0 for g in received["groups"])

    def test_ranker_produces_positive_ic_on_signal(self):
        """rank:pairwise が強シグナル合成データで正の rank-IC を出す。"""
        s = _synthetic_samples()
        params = dict(_XGB_PARAMS, objective="rank:pairwise")
        best_iters = []
        cb = _make_xgb_rank_fit_predict(params, best_iters)
        _folds, resid = walk_forward_cv_monthly(
            s, ["f0", "f1", "f2", "f3"],
            min_train_months=6, step_months=3, return_residuals=True,
            fit_predict=cb, embargo_months=0, pass_train_groups=True,
        )
        bt = oof_backtest(resid, n_quantiles=5)
        assert bt["rank_ic"]["n"] >= 1
        assert bt["rank_ic"]["mean"] > 0.3, "強シグナルで正の rank-IC が出ていない"
        assert best_iters and all(b == params["n_estimators"] for b in best_iters)

    def test_ndcg_objective_runs(self):
        s = _synthetic_samples(seed=1)
        params = dict(_XGB_PARAMS, objective="rank:ndcg")
        cb = _make_xgb_rank_fit_predict(params, [])
        _folds, resid = walk_forward_cv_monthly(
            s, ["f0", "f1", "f2", "f3"],
            min_train_months=6, step_months=3, return_residuals=True,
            fit_predict=cb, embargo_months=0, pass_train_groups=True,
        )
        bt = oof_backtest(resid, n_quantiles=5)
        assert bt["rank_ic"]["n"] >= 1


# ── 5. compat（既定 pass_train_groups=False は 2 引数呼び出し・M-2 回帰保護）──────

class TestBackwardCompat:
    def test_default_calls_2arg_callback(self):
        """pass_train_groups 未指定（既定 False）では従来の 2 引数コールバックが呼ばれる。"""
        arities = []

        def cb2(train_samples, test_samples):
            arities.append(2)
            return [0.0] * len(test_samples), [t for _, t in test_samples]

        s = _synthetic_samples()
        walk_forward_cv_monthly(
            s, ["f0", "f1", "f2", "f3"],
            min_train_months=6, step_months=3, return_residuals=False,
            fit_predict=cb2, embargo_months=0,
        )
        assert arities and all(a == 2 for a in arities)


# ── 6. smoke（execute end-to-end・DB モック）───────────────────────────────────

class TestExecuteSmokeRank:
    def _make_params(self, **overrides):
        base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
        base.update(overrides)
        return coerce_params(plugin.params_schema(), base)

    def _make_db(self, n_companies=4, n_weeks=210):
        return _M2Smoke()._make_db(n_companies=n_companies, n_weeks=n_weeks)

    def test_execute_returns_ranker_model_type(self):
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)
        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        assert result["model_type"] == "xgboost_ranker"
        assert result["n_companies"] == len(result["results"]) > 0
        assert "oof_backtest" in result
        assert result["best_iteration"] is not None
        for item in result["results"]:
            assert item["mu_raw"] == item["mu_raw"], "スコアが NaN"

    def test_execute_does_not_persist_producer(self):
        """M-5 は producer を持たない → macro_gbdt_scores を書かない（_persist_producer=no-op）。"""
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)
        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}), \
             patch("database.replace_macro_gbdt_scores") as mock_replace:
            plugin.execute(params, db)

        mock_replace.assert_not_called()

    def test_execute_oof_has_rank_ic(self):
        db, prices_by_co, fin_by_co, companies = self._make_db()
        params = self._make_params(use_macro=False)
        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value={}), \
             patch("plugins.macro_gbdt.get_producer_scores", return_value={}):
            result = plugin.execute(params, db)

        oof = result["oof_backtest"]
        assert set(oof["rank_ic"].keys()) == {"mean", "std", "n"}
        assert oof["n_oof_samples"] > 0
