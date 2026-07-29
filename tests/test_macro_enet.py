"""tests/test_macro_enet.py — M-6 MacroEnetPlugin テスト（Issue #372 / ADR-0021）

候補メニュー（`plugins/model_candidates.py`）で M-2 を有意に上回ったため正式兄弟へ昇格した
ElasticNet 線形モデル。

テスト観点:
  1. meta   : 登録・ui_order=390・未実行時は producer 無し（graceful-degrade）
  2. coerce : l1_ratio の membership 検証・macro_pca_components の bounds 検証
  3. fold   : M-2 と同一の walk-forward 設定（min_train_months=6 / step=3 / embargo=12）で回す
  4. smoke  : execute の出力契約（model_type=elasticnet・係数と特徴量名の対応・results は top_n 以内）
  5. tuning : tuning_objective_only で OOF 算出後に早期 return する（model_comparison の高速化）
  6. compare: model_comparison.COMPARISON_MODELS に M-6 として登録されている

マクロ fold 内 PCA（ADR-0021 改善案④）は bake-off の実測で昇格ゲートを通らなかったため
**本プラグインには載せない**（探索枠 `model_candidates.wrap_macro_pca` に残す）。その契約も
`test_no_pca_knob_promoted` で固定する。
"""
from unittest.mock import MagicMock, patch

import pytest

from plugins.macro_enet import MacroEnetPlugin
from plugins.macro_snapshots import LABEL_HORIZON_MONTHS
from plugins.utils import coerce_params
# M-2 スモーク用 DB ビルダーを流用（同一母集団で純比較する前提を共有）。
# エイリアスは Test* を避ける（pytest による二重 collection 防止）。
from tests.test_macro_gbdt import TestExecuteSmoke as _M2Smoke

plugin = MacroEnetPlugin()


def _params(**overrides):
    base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
    base.update(overrides)
    return coerce_params(plugin.params_schema(), base)


def _run(params, **patches):
    db, prices_by_co, fin_by_co, companies = _M2Smoke()._make_db()
    with patch("plugins.macro_enet.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
         patch("plugins.macro_enet.preload_macro", return_value={}), \
         patch("plugins.macro_enet.get_producer_scores", return_value={}):
        return plugin.execute(params, db)


# ── 1. meta ───────────────────────────────────────────────────────────────────

class TestPluginMeta:
    def test_registered_in_registry(self):
        import plugins as reg
        assert isinstance(reg.get_plugin("macro_enet"), MacroEnetPlugin)

    def test_name_label(self):
        assert plugin.name == "macro_enet"
        assert plugin.label.startswith("M-6")

    def test_ui_order_after_m5(self):
        assert plugin.ui_order == 390

    def test_category_and_heavy(self):
        assert plugin.category == "③ 将来リターンを予測"
        assert plugin.heavy is True

    def test_producer_absent_before_run(self, db):
        """未実行（macro_enet_scores 空）なら producer 無し＝consumer は graceful-degrade。"""
        assert plugin.produced_output(db) is False
        assert plugin.read_producer_scores(db) == {}

    def test_registered_in_model_comparison(self):
        from model_comparison import COMPARISON_MODELS
        assert ("macro_enet", "M-6") in COMPARISON_MODELS


# ── 2. パラメータ契約 ─────────────────────────────────────────────────────────

class TestParamsContract:
    def test_l1_ratio_membership_enforced(self):
        with pytest.raises(ValueError):
            coerce_params(plugin.params_schema(), {"l1_ratio": "elastic"})

    def test_l1_ratio_presets_map_to_tuples(self):
        assert plugin._l1_ratios("auto") == (0.1, 0.5, 0.9)
        assert plugin._l1_ratios("ridge") == (0.1,)
        assert plugin._l1_ratios("lasso") == (0.9,)

    def test_no_pca_knob_promoted(self):
        """PCA 圧縮は実測で昇格ゲートを通らなかったため本モデルには載せない（ADR-0021）。"""
        assert "macro_pca_components" not in plugin.params_schema()

    def test_empty_fin_features_rejected(self):
        with pytest.raises(ValueError, match="財務特徴量"):
            _run(_params(fin_features=[], use_macro=False))

    def test_tuning_search_space_is_structural_only(self):
        """α/l1_ratio は学習 fold 内 CV が決めるため探索軸に含めない。"""
        _base, dims = plugin.tuning_search_space()
        names = {d.name for d in dims}
        assert names == {"use_momentum", "momentum_window"}


# ── 3. fold 設定（M-2 と同一）─────────────────────────────────────────────────

class TestFoldConfigMatchesM2:
    def test_walk_forward_called_with_m2_settings(self):
        captured: dict = {}

        def _spy(samples, names, **kw):
            captured.update(kw)
            return [], {}

        db, prices_by_co, fin_by_co, companies = _M2Smoke()._make_db()
        with patch("plugins.macro_enet.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_enet.preload_macro", return_value={}), \
             patch("plugins.macro_enet.get_producer_scores", return_value={}), \
             patch("plugins.macro_enet.walk_forward_cv_monthly", side_effect=_spy):
            plugin.execute(_params(use_macro=False), db)

        assert captured["min_train_months"] == 6
        assert captured["step_months"] == 3
        assert captured["embargo_months"] == LABEL_HORIZON_MONTHS == 12
        assert captured["return_residuals"] is True
        assert callable(captured["fit_predict"])


# ── 4. execute スモーク ───────────────────────────────────────────────────────

class TestExecuteSmoke:
    def test_required_keys(self):
        res = _run(_params(use_macro=False))
        required = {"cv_metrics", "selected_features", "feature_coefs", "cv_diagnostics",
                    "n_train_samples", "n_companies", "results", "model_type", "oof_backtest"}
        assert required <= set(res)

    def test_model_type(self):
        assert _run(_params(use_macro=False))["model_type"] == "elasticnet"

    def test_coefficients_align_with_feature_names(self):
        res = _run(_params(use_macro=False))
        assert set(res["feature_coefs"]) == set(res["selected_features"])
        assert all(isinstance(v, float) for v in res["feature_coefs"].values())

    def test_final_model_meta_reported(self):
        meta = _run(_params(use_macro=False))["final_model"]
        # 合成スモークデータは価格が完全な線形列で目的変数がほぼ定数のため、α パスの上端
        # （alpha_max = max|Xᵀy|/(n·l1_ratio)）自体が 0 に潰れうる。ここでは値域と型の契約だけ
        # 固定する（実データで α>0 が選ばれることは ADR-0021 の実測 α=0.062 が示す）。
        assert meta["alpha"] >= 0
        assert meta["l1_ratio"] in (0.1, 0.5, 0.9)
        assert 0 <= meta["n_nonzero"] <= meta["n_features"]

    def test_results_capped_at_top_n_and_sorted(self):
        res = _run(_params(use_macro=False, top_n=5))
        assert len(res["results"]) <= 5
        mus = [r["mu_raw"] for r in res["results"]]
        assert mus == sorted(mus, reverse=True)

    def test_result_rows_have_risk_axes(self):
        rows = _run(_params(use_macro=False))["results"]
        assert rows, "スモークDBで結果が空になった"
        for key in ("edinet_code", "company_name", "industry", "mu_raw", "r1", "r2", "r3", "r_macro"):
            assert key in rows[0]

    def test_oof_backtest_present(self):
        oof = _run(_params(use_macro=False))["oof_backtest"]
        assert "rank_ic" in oof and "n_periods" in oof


# ── 5. tuning 早期 return（model_comparison 高速化）───────────────────────────

class TestTuningObjectiveOnly:
    def test_early_return_skips_scoring(self):
        from database import tuning_objective_only
        with tuning_objective_only():
            res = _run(_params(use_macro=False))
        assert res["results"] == []
        assert res["n_companies"] == 0
        assert res["feature_coefs"] == {}
        assert "oof_backtest" in res           # 比較ビューが読む値は返る


# ── 6. 昇格しなかった軸を持ち込んでいないこと ────────────────────────────────

class TestPromotionScope:
    def test_feature_names_are_raw_not_compressed(self):
        """マクロ列は主成分へ畳まず生のまま使う（PCA は昇格対象外・ADR-0021）。"""
        res = _run(_params(use_macro=False))
        assert not any(n.startswith("macro_pc") for n in res["selected_features"])


# ── 7. producer 往復（in-memory SQLite・conftest の db fixture・Issue #396）───────

class TestProducer:
    def test_replace_and_read_round_trip(self, db):
        from database import get_macro_enet_scores, replace_macro_enet_scores
        n = replace_macro_enet_scores(
            db,
            [{"edinet_code": "E1", "mu": 0.12, "r1_prime": 0.30},
             {"edinet_code": "E2", "mu": None, "r1_prime": 0.10}],
            "2026-07-01")
        assert n == 1                      # mu=None はスキップ
        assert get_macro_enet_scores(db) == {"E1": 0.12}
        assert plugin.produced_output(db) is True

        scores = plugin.read_producer_scores(db)
        assert scores["E1"]["mu"] == pytest.approx(0.12)
        assert scores["E1"]["r1_prime"] == pytest.approx(0.30)   # R3 ゲートが読む確実性軸
        assert "r_macro" in scores["E1"]                          # macro_beta 未蓄積なら None

    def test_r1_prime_optional(self, db):
        """r1_prime 省略時は None（R3 足切りゲート素通り・graceful）。"""
        from database import replace_macro_enet_scores
        replace_macro_enet_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "2026-07-01")
        assert plugin.read_producer_scores(db)["E1"]["r1_prime"] is None

    def test_replace_is_snapshot_overwrite(self, db):
        from database import get_macro_enet_scores, replace_macro_enet_scores
        replace_macro_enet_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "2026-07-01")
        replace_macro_enet_scores(db, [{"edinet_code": "E2", "mu": 0.2}], "2026-07-02")
        assert get_macro_enet_scores(db) == {"E2": 0.2}

    def test_dry_run_noop(self, db):
        """探索中（tuning_dry_run）は中間候補で producer を上書きしない（Issue #264）。"""
        import database
        from database import get_macro_enet_scores, replace_macro_enet_scores
        with database.tuning_dry_run():
            n = replace_macro_enet_scores(db, [{"edinet_code": "E1", "mu": 0.1}], None)
        assert n == 0
        assert get_macro_enet_scores(db) == {}

    def test_execute_persists_producer(self):
        """execute() 末尾で μ̂ と r1_prime が macro_enet_scores へ渡る（sell_ranking の入力）。"""
        with patch("database.replace_macro_enet_scores") as m:
            res = _run(_params(use_macro=False))
        assert m.call_count == 1
        rows, snap = m.call_args[0][1], m.call_args[0][2]
        assert len(rows) == res["n_companies"]
        assert {"edinet_code", "mu", "r1_prime"} == set(rows[0])
        assert all(r["mu"] is not None for r in rows)
        assert snap is None or len(snap) == 10          # "YYYY-MM-DD"

    def test_execute_skips_persist_in_objective_only(self):
        """探索の早期 return 経路では永続化しない（全社スコアリング前に return するため）。"""
        from database import tuning_objective_only
        with patch("database.replace_macro_enet_scores") as m:
            with tuning_objective_only():
                _run(_params(use_macro=False))
        assert m.call_count == 0
