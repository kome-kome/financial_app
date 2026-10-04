"""tests/test_macro_enet.py — M-6 MacroEnetPlugin テスト（Issue #372 / ADR-0021）

候補メニュー（`plugins/model_candidates.py`）で M-2 を有意に上回ったため正式兄弟へ昇格した
ElasticNet 線形モデル。

テスト観点:
  1. meta   : 登録・ui_order=390・未実行時は producer 無し（graceful-degrade）
  2. coerce : l1_ratio の membership 検証・macro_pca_components の bounds 検証
  3. fold   : M-2 と同一の walk-forward 設定（min_train_months=6 / step=3 / embargo=12）で回す
  3.5 config: 探索設定（l1_ratios / n_alphas / cv_splits / max_iter）が候補実装の単一ソース（#452）
  4. smoke  : execute の出力契約（model_type=elasticnet・係数と特徴量名の対応・results は今買える社の全件）
  4.5 view  : 散布図の入力（#807）＝表示だけのパラメータ・相対 μ̂・予測の内訳（合計＝μ̂）
  5. tuning : tuning_objective_only で OOF 算出後に早期 return する（model_comparison の高速化）
  6. compare: model_comparison.COMPARISON_MODELS に M-6 として登録されている

マクロ fold 内 PCA（ADR-0021 改善案④）は bake-off の実測で昇格ゲートを通らなかったため
**本プラグインには載せない**（探索枠 `model_candidates.wrap_macro_pca` に残す）。その契約も
`test_no_pca_knob_promoted` で固定する。
"""
import statistics
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from plugins.macro_enet import MacroEnetPlugin
from plugins.macro_snapshots import LABEL_HORIZON_MONTHS
from plugins.utils import coerce_params
# M-2 スモーク用 DB ビルダーを流用（同一母集団で純比較する前提を共有）。
# エイリアスは Test* を避ける（pytest による二重 collection 防止）。
from tests.test_macro_gbdt import TestExecuteSmoke as _M2Smoke

plugin = MacroEnetPlugin()


@pytest.fixture(autouse=True)
def _no_tradable_lookup(monkeypatch):
    """producer の as-of の母集団判定（#780）を素通しにする。

    このファイルの execute は MagicMock の db で回すことが多く、`stale_price_codes` の
    比較式（`sub.c.d < cutoff`）が組めず TypeError になる。母集団の判定そのものはここの
    主題ではなく、tests/test_tradable_universe.py が実 DB（SQLite）で縛る。空集合なら
    as-of は全行の代表値＝変更前と同じ値になるので、既存の assert はそのまま読める。
    """
    monkeypatch.setattr("database.non_tradable_codes", lambda db: set())


def _params(**overrides):
    base = {k: v["default"] for k, v in plugin.params_schema().items() if "default" in v}
    base.update(overrides)
    return coerce_params(plugin.params_schema(), base)


def _run(params, n_companies=4, **patches):
    db, prices_by_co, fin_by_co, companies = _M2Smoke()._make_db(n_companies=n_companies)
    # as-of の母集団判定（#780）もここで素通しにする。autouse fixture はこのモジュールの
    # テストにしか効かないが、`_run` は tests/test_nightly_scores.py からも借りられる。
    with patch("plugins.macro_enet.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
         patch("plugins.macro_enet.preload_macro", return_value={}), \
         patch("plugins.macro_enet.get_producer_scores", return_value={}), \
         patch("database.non_tradable_codes", return_value=set()):
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

    def test_use_momentum_default_off(self):
        """モメンタムの既定 OFF は実測の結論（ADR-0045）＝惰性ではない。

        ON/OFF を同一 fold・同一 (ym,ec) 域で比較して4検定すべて補正後 α を通らず符号も負
        （M-6 rank-IC −0.0104 p=0.100 / 売り側 −0.0043 p=0.269）。**この既定は UI の初期値
        であると同時に本番 producer の設定**でもあり（`nightly_scores.NIGHTLY_PARAMS` は M-6 の
        default をそのまま使う）、変えると `macro_enet_scores` が「評価していない構成」で
        生成される。再検討するなら `python -m scripts.momentum_gate` を回してから。
        """
        assert coerce_params(plugin.params_schema(), {})["use_momentum"] is False

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


# ── 3.5 探索設定の単一ソース（Issue #452）─────────────────────────────────────

class TestSearchConfigSingleSource:
    """CV（候補実装）・最終学習・M-4 が同じ ElasticNet 探索設定を見ることを固定する。

    以前は同じ値を `macro_enet` と `model_candidates` の双方で宣言していたため、片方だけ
    書き換えると「CV は収束するのに最終学習だけ未収束」のような割れ方をしうる状態だった。
    """

    def test_constants_are_reexported_from_candidate_impl(self):
        import plugins.macro_enet as me
        import plugins.model_candidates as mc
        assert (me._L1_RATIOS, me._N_ALPHAS, me._CV_SPLITS, me._MAX_ITER) == (
            mc._EN_L1_RATIOS, mc._EN_N_ALPHAS, mc._EN_CV_SPLITS, mc._EN_MAX_ITER)

    def test_max_iter_calibrated_for_convergence(self):
        """max_iter は本番規模で収束する水準を保つ（#452 実測: 5000 は 17 fold 中 10 件が未収束）。"""
        import plugins.macro_enet as me
        assert me._MAX_ITER >= 50_000


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

    def test_final_model_reports_alpha_path_edges(self):
        """夜間の診断（#726）が「α がパスの端か」を読めるよう、パスの両端と判定を返す。"""
        meta = _run(_params(use_macro=False))["final_model"]
        assert meta["l1_ratio_grid"] == [0.1, 0.5, 0.9]
        assert meta["alpha_path_min"] <= meta["alpha_path_max"]
        # 丸める前の α で判定する（表示用の alpha は 6桁に丸めてある）
        assert meta["alpha_path_min"] - 1e-6 <= meta["alpha"] <= meta["alpha_path_max"] + 1e-6
        assert isinstance(meta["alpha_at_path_min"], bool)
        assert isinstance(meta["alpha_at_path_max"], bool)

    def test_results_are_all_tradable_and_sorted(self):
        """今買える社を全件返す（top_n で切らない＝表の件数・λ・横軸は画面が切る・#807）。"""
        res = _run(_params(use_macro=False, top_n=5), n_companies=8)
        assert len(res["results"]) == res["n_companies"] == 8
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


# ── 4.5 散布図の入力（#807）────────────────────────────────────────────────────

class TestViewParams:
    """λ・横軸・R3 ゲートは表示だけのパラメータ（サーバーは受け取って返すだけ・M-2 と同じ）。"""

    def test_defaults_keep_mu_order_and_r2_axis(self):
        """既定は λ=0（表は μ̂ の順＝OOF で検証済みの並び）・横軸 R2（macro_beta に依存しない）。"""
        p = coerce_params(plugin.params_schema(), {})
        assert p["lambda_risk"] == 0.0
        assert p["risk_axis"] == "r2"
        assert p["r3_gate"] == 0.0

    def test_risk_axis_membership_enforced(self):
        opts = {o["value"] for o in plugin.params_schema()["risk_axis"]["options"]}
        assert opts == {"r2", "r_macro"}
        with pytest.raises(ValueError):
            coerce_params(plugin.params_schema(), {"risk_axis": "r1"})

    def test_view_params_echoed_and_unused_by_model(self):
        """値は返るだけで μ̂ を変えない（変えると画面の再計算と食い違う）。"""
        a = _run(_params(use_macro=False))
        b = _run(_params(use_macro=False, lambda_risk=2.5, risk_axis="r_macro", r3_gate=0.2))
        assert (b["lambda_risk"], b["risk_axis"], b["r3_gate"]) == (2.5, "r_macro", 0.2)
        assert [r["mu_raw"] for r in a["results"]] == [r["mu_raw"] for r in b["results"]]


class TestRelativeMu:
    """相対 μ̂ = μ̂ − 今買える社の μ̂ 中央値（CONTEXT.md「相対 μ̂」）。"""

    def test_is_a_uniform_shift_centered_at_median(self):
        res = _run(_params(use_macro=False), n_companies=8)
        rows = res["results"]
        shifts = {round(r["mu_raw"] - r["mu_rel"], 6) for r in rows}
        # 全社に同じ量＝順位・パレート集合・U=μ−λR の並びは μ̂ と同じ（変わるのは目盛りだけ）
        assert len(shifts) == 1
        assert shifts.pop() == pytest.approx(res["mu_rel_center"], abs=1e-6)
        assert res["mu_rel_center"] == pytest.approx(
            statistics.median(r["mu_raw"] for r in rows), abs=1e-6)
        assert statistics.median(r["mu_rel"] for r in rows) == pytest.approx(0.0, abs=1e-6)

    def test_center_uses_tradable_companies_only(self):
        """中央値は表示する（今買える）社だけで取る＝廃止社の μ̂ に引っ張られない（#806）。"""
        db, prices_by_co, fin_by_co, companies = _M2Smoke()._make_db(n_companies=6)
        with patch("plugins.macro_enet.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_enet.preload_macro", return_value={}), \
             patch("plugins.macro_enet.get_producer_scores", return_value={}), \
             patch("database.non_tradable_breakdown",
                   return_value={"delisted": {"E00000", "E00001"}, "stale": set()}):
            res = plugin.execute(_params(use_macro=False), db)
        shown = res["results"]
        assert {r["edinet_code"] for r in shown} == {"E00002", "E00003", "E00004", "E00005"}
        assert res["mu_rel_center"] == pytest.approx(
            statistics.median(r["mu_raw"] for r in shown), abs=1e-6)


def _noisy_panel(seed=807, n_train=300, n_cur=40, n_feat=6):
    """ノイズ入りの線形パネル。完全線形の検体だと全社の μ̂ が揃い、合計の一致が空振りする。"""
    rng = np.random.default_rng(seed)
    beta = np.array([0.8, -0.5, 0.3, 0.0, 0.0, 0.2])[:n_feat]
    X = rng.normal(size=(n_train, n_feat))
    y = X @ beta + rng.normal(scale=0.5, size=n_train)
    samples = [(X[i].tolist(), float(y[i])) for i in range(n_train)]
    current = rng.normal(size=(n_cur, n_feat)).tolist()
    return samples, current


class TestBreakdown:
    """予測の内訳（#807）: 基準 + Σ寄与 = μ̂ を近似なしで満たす（線形モデルなので）。"""

    def test_sums_to_mu_on_noisy_panel(self):
        samples, current = _noisy_panel()
        coefs, mu, _meta, bd = MacroEnetPlugin._fit_final_and_score(
            samples, current, (0.5,), return_breakdown=True)
        mu = np.asarray(mu)
        assert bd["contrib"].shape == (len(current), len(current[0]))
        np.testing.assert_allclose(bd["base"] + bd["contrib"].sum(axis=1), mu, atol=1e-9)
        # 空振り防止: 内訳が「全社同じ」「基準だけ」だと一致は自明に通る
        assert np.std(mu) > 0.1
        assert sum(1 for c in coefs if c != 0.0) >= 2
        assert np.abs(bd["contrib"].sum(axis=1)).max() > 0.1
        assert np.ptp(bd["contrib"], axis=0).max() > 0.1

    def test_default_keeps_three_tuple_for_m4(self):
        """M-4（macro_ensemble）は3つ組で呼ぶ＝既定の戻り値の形を変えない。"""
        samples, current = _noisy_panel(n_cur=5)
        out = MacroEnetPlugin._fit_final_and_score(samples, current, (0.5,))
        assert len(out) == 3

    def test_rows_sum_to_mu_raw(self):
        res = _run(_params(use_macro=False), n_companies=8)
        base = res["breakdown_base"]
        for r in res["results"]:
            total = base + r["contrib_macro"] + sum(r["contrib"].values())
            assert total == pytest.approx(r["mu_raw"], abs=1e-4)

    def test_macro_columns_fold_into_one_row_and_zero_coefs_dropped(self):
        """マクロ列は「マクロ環境（全社共通）」1行へ合算し、係数 0 の列は内訳に並べない。"""
        def _fake_fit(all_samples, current_rows, l1_ratios, return_breakdown=False):
            n, k = len(current_rows), len(current_rows[0])
            contrib = np.random.default_rng(1).normal(size=(n, k))
            coefs = [1.0] * k
            coefs[0] = 0.0                    # L1 で落ちた列
            contrib[:, 0] = 0.0
            base = 0.05
            mu = (base + contrib.sum(axis=1)).tolist()
            return coefs, mu, {}, {"base": base, "contrib": contrib}

        # 検体はマクロ値を持たない（全欠測）。系列を絞らないと充足率下限で全サンプルが落ちる。
        two = [o["value"] for o in plugin.params_schema()["macro_features"]["options"]][:2]
        params = _params(use_macro=True, macro_features=two)
        with patch.object(MacroEnetPlugin, "_fit_final_and_score", staticmethod(_fake_fit)):
            res = _run(params)
        macro = set(params["macro_features"])
        feats = res["selected_features"]
        assert macro & set(feats), "マクロ列が特徴量に入っていない（検体が主題を踏んでいない）"
        for r in res["results"]:
            assert not (macro & set(r["contrib"]))
            assert feats[0] not in r["contrib"]
            assert r["contrib_macro"] != 0.0
            total = res["breakdown_base"] + r["contrib_macro"] + sum(r["contrib"].values())
            assert total == pytest.approx(r["mu_raw"], abs=1e-4)

    def test_no_macro_means_zero_macro_row(self):
        for r in _run(_params(use_macro=False))["results"]:
            assert r["contrib_macro"] == 0.0


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


class TestUntradableHidden:
    """廃止・価格停止の社は results から外れ、保存する μ̂ には残る（#806）。

    M-6 では株価が止まった日のマクロ値で μ̂ が押し上がり、上位30のうち23社が廃止社だった。
    """

    def test_hidden_from_results_but_persisted(self):
        db, prices_by_co, fin_by_co, companies = _M2Smoke()._make_db()
        persisted: list[dict] = []
        with patch("plugins.macro_enet.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_enet.preload_macro", return_value={}), \
             patch("plugins.macro_enet.get_producer_scores", return_value={}), \
             patch("database.non_tradable_breakdown",
                   return_value={"delisted": {"E00001"}, "stale": {"E00002"}}), \
             patch("database.replace_macro_enet_scores",
                   side_effect=lambda db, rows, *a, **k: persisted.extend(rows)):
            result = plugin.execute(_params(use_macro=False), db)

        assert {it["edinet_code"] for it in result["results"]} == {"E00000", "E00003"}
        assert {r["edinet_code"] for r in persisted} == set(companies)
        assert result["untradable_excluded"] == {"delisted": 1, "stale": 1, "fallback": False}
        assert result["n_companies"] == len(companies)


class TestScreenWiring:
    """画面側（static/js/analysis.js）が応答のキーを読んでいること（#807）。

    JS の実行環境は CI に無いので文字列で縛る。キー名がずれても失敗としては現れず、
    散布図が空になる・内訳が開かないだけになる。
    """

    @staticmethod
    def _js() -> str:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return open(os.path.join(root, "static", "js", "analysis.js"), encoding="utf-8").read()

    def test_renderer_registered(self):
        assert "'macro_enet':        renderMacroEnet," in self._js()

    def test_reads_view_keys(self):
        js = self._js()
        for key in ("mu_rel", "mu_rel_center", "breakdown_base", "contrib_macro", "item.contrib"):
            assert key in js, key

    def test_rows_use_delegation_not_inline_onclick(self):
        """インラインの onclick は CSP（script-src-attr）で遮断され、行クリックが黙って効かない。"""
        js = self._js()
        assert 'data-click="_rrSelectRow"' in js
        assert "CustomEvent('mg-shap'" not in js
