"""
M-2 マクロ×財務 勾配ブースティング推奨プラグイン（ADR-0003 / Issue #234）

M-1（macro_risk_return）の非線形兄弟。同一スナップショット母集団・同一リスク-リターン幾何を
共有しつつ、XGBoost が fin×macro の交互作用を自動学習する（交差項の手動生成なし）。

次元整合性（CLAUDE.md）:
  目的変数 = 52週先対数リターン（無次元）
  説明変数 = 財務比率・マクロ変化率/Zスコア・モメンタム（全て無次元）
  特徴量 X の winsorize は撤去（木は単調不変）、y のみ p1-p99 winsorize を維持

設計決定（ADR-0003）:
  - 同期 in-execute・heavy=True（macro_beta バッチに倣わない・XGBoost は MCMC と速度域が違う）
  - 共有ビルダー build_snapshots(..., build_interactions=False)
  - fit_predict コールバックを walk_forward_cv_monthly に注入
  - 内蔵比較: 同一特徴量・同一 fold の素 OLS ベースライン（交差項/BIC なし）
  - SHAP: グローバル mean|SHAP|（feature_coefs スロット）＋全社 per-stock SHAP
  - R1 なし（効用軸でない）、R_macro は既存 macro_beta producer から流用
"""
import math
import statistics
from typing import Any

import numpy as np

from .base import AnalysisPlugin
from .utils import walk_forward_cv_monthly, winsorize
from .macro_snapshots import (
    FIN_BASE_OPTIONS,
    LABEL_HORIZON_MONTHS,
    MACRO_FEATURE_OPTIONS,
    _realized_vol,
    load_data,
    preload_macro,
    build_snapshots,
    get_producer_scores,
    oof_backtest,
)
from .macro_risk_return import (
    MacroRiskReturnPlugin as _M1,
)

# ── XGBoost fit_predict コールバックファクトリ ─────────────────────────────────

_VALID_FRAC = 0.2          # 学習データから時系列末尾の何割を early_stopping 検証用に使うか
_MIN_FIT_N  = 5            # この未満なら early_stopping を諦め固定 n_estimators にフォールバック


def _make_xgb_fit_predict(xgb_params: dict, best_iterations: list) -> callable:
    """walk_forward_cv_monthly 注入用 XGBoost fit_predict コールバックを返す。

    各フォールドで:
      1. y を p1-p99 winsorize（X は winsorize しない）
      2. 学習データの時系列末尾 _VALID_FRAC を early_stopping の eval_set に
      3. best_iteration を best_iterations リストに記録（最終モデルの n_estimators 決定用）
      4. (yhat_orig, y_test_orig) を返す（y はオリジナルスケール・winsorize なし）
    """
    import xgboost as xgb

    n_estimators_max = xgb_params.get("n_estimators", 500)
    early_stopping_rounds = xgb_params.get("early_stopping_rounds", 40)
    base_params = {k: v for k, v in xgb_params.items() if k not in ("n_estimators", "early_stopping_rounds")}

    def fit_predict(train_samples, test_samples):
        X_train_all = np.array([s[0] for s in train_samples], dtype=float)
        y_train_raw = [s[1] for s in train_samples]
        X_test = np.array([s[0] for s in test_samples], dtype=float)
        y_test_orig = [s[1] for s in test_samples]

        y_w, _, _ = winsorize(y_train_raw)
        y_train_all = np.array(y_w, dtype=float)

        n = len(train_samples)
        n_valid = max(1, int(n * _VALID_FRAC))
        n_fit = n - n_valid

        if n_fit < _MIN_FIT_N or early_stopping_rounds is None:
            model = xgb.XGBRegressor(**base_params, n_estimators=n_estimators_max)
            model.fit(X_train_all, y_train_all, verbose=False)
            best_iterations.append(n_estimators_max)
        else:
            X_fit, X_valid = X_train_all[:n_fit], X_train_all[n_fit:]
            y_fit, y_valid = y_train_all[:n_fit], y_train_all[n_fit:]
            model = xgb.XGBRegressor(
                **base_params,
                n_estimators=n_estimators_max,
                early_stopping_rounds=early_stopping_rounds,
            )
            model.fit(X_fit, y_fit, eval_set=[(X_valid, y_valid)], verbose=False)
            bi = getattr(model, "best_iteration", None)
            best_iterations.append(bi if (bi and bi > 0) else n_estimators_max)

        yhat = model.predict(X_test).tolist()
        return yhat, y_test_orig

    return fit_predict


# ── プラグイン本体 ────────────────────────────────────────────────────────────

class MacroGbdtPlugin(AnalysisPlugin):
    name = "macro_gbdt"
    label = "M-2: マクロ×財務 勾配ブースティング"
    description = (
        "財務比率×マクロ要因を XGBoost（勾配ブースティング決定木）で学習し、"
        "非線形・高次の交互作用を自動捕捉します。M-1（OLS 線形）と同一データで比較可能。"
        "SHAP で特徴量寄与を可視化。【注意】株価週次履歴とマクロデータ5年分の蓄積が必要です。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    category = "③ 将来リターンを予測"
    ui_order = 340

    def params_schema(self) -> dict:
        return {
            # ── リスク-リターン幾何（M-1 と同一契約）──
            "lambda_risk": {
                "type": "slider",
                "dtype": "float",
                "label": "リスク回避度 λ",
                "description": "U = μ − λ × R。λ=0 でリターン最大化、λ大でリスク重視。",
                "default": 1.0,
                "min": 0.0,
                "max": 5.0,
                "step": 0.1,
            },
            "risk_axis": {
                "type": "select",
                "label": "横軸リスク",
                "description": (
                    "R2=実現ボラ / R_macro=マクロ起因リスク（既定・macro_beta 蓄積が必要）。"
                ),
                "options": [
                    {"value": "r2",      "label": "R2 実現ボラティリティ"},
                    {"value": "r_macro", "label": "R_macro マクロ起因リスク（β推論要・既定）"},
                ],
                "default": "r_macro",
            },
            "r3_gate": {
                "type": "slider",
                "dtype": "float",
                "label": "R3 信頼度ゲート（足切り）",
                "description": "CV-RMSE がこの値を超える銘柄を上位表示から除外（0=ゲートなし）。",
                "default": 0.0,
                "min": 0.0,
                "max": 0.5,
                "step": 0.01,
            },
            # ── 特徴量（M-1 から継承）──
            "fin_features": {
                "type": "multiselect",
                "label": "財務ベース特徴量",
                "options": FIN_BASE_OPTIONS,
                "default": [o["value"] for o in FIN_BASE_OPTIONS],
            },
            "use_macro": {
                "type": "checkbox",
                "label": "マクロ特徴量を使用",
                "default": True,
            },
            "macro_features": {
                "type": "multiselect",
                "label": "マクロ特徴量",
                "options": MACRO_FEATURE_OPTIONS,
                "default": [o["value"] for o in MACRO_FEATURE_OPTIONS],
            },
            "use_momentum": {
                "type": "checkbox",
                "label": "モメンタム特徴量を使用",
                "default": False,
            },
            "momentum_window": {
                "type": "number",
                "dtype": "int",
                "label": "モメンタム算出月数",
                "default": 12,
                "min": 3,
                "max": 24,
            },
            "min_coverage": {
                "type": "slider",
                "dtype": "float",
                "label": "特徴量充足率下限",
                "description": (
                    "サンプル採用に必要な非欠損特徴量の最低割合。マクロ欠損は NaN として保持し"
                    "（XGBoost が処理）、本下限が表示可否を制御する。薄いマクロ系列を足しても"
                    "下限を割らなければ企業は脱落しない。財務特徴量は欠損時に常に除外。"
                ),
                "default": 0.5,
                "min": 0.1,
                "max": 1.0,
                "step": 0.05,
            },
            "top_n": {
                "type": "number",
                "dtype": "int",
                "label": "上位表示件数",
                "default": 30,
                "min": 5,
                "max": 200,
            },
            # ── XGBoost ハイパーパラメータ（強正則化デフォルト・ADR-0003 §7）──
            "max_depth": {
                "type": "slider",
                "dtype": "int",
                "label": "XGB 最大深さ",
                "description": "小さいほど正則化が強い。低 S/N な日本株リターン予測では浅め推奨。",
                "default": 4,
                "min": 2,
                "max": 10,
                "step": 1,
            },
            "learning_rate": {
                "type": "slider",
                "dtype": "float",
                "label": "学習率",
                "default": 0.05,
                "min": 0.01,
                "max": 0.3,
                "step": 0.01,
            },
            "subsample": {
                "type": "slider",
                "dtype": "float",
                "label": "サブサンプル率",
                "description": "各ツリーで使うサンプルの割合。",
                "default": 0.8,
                "min": 0.4,
                "max": 1.0,
                "step": 0.05,
            },
            "colsample_bytree": {
                "type": "slider",
                "dtype": "float",
                "label": "列サブサンプル率",
                "description": "各ツリーで使う特徴量の割合。",
                "default": 0.8,
                "min": 0.4,
                "max": 1.0,
                "step": 0.05,
            },
            "min_child_weight": {
                "type": "slider",
                "dtype": "int",
                "label": "最小葉重み",
                "description": "葉ノードに必要な最小サンプル重み。大きいほど正則化が強い。",
                "default": 5,
                "min": 1,
                "max": 30,
                "step": 1,
            },
            "reg_lambda": {
                "type": "slider",
                "dtype": "float",
                "label": "L2 正則化（reg_lambda）",
                "default": 1.0,
                "min": 0.0,
                "max": 10.0,
                "step": 0.5,
            },
            "reg_alpha": {
                "type": "slider",
                "dtype": "float",
                "label": "L1 正則化（reg_alpha）",
                "default": 0.0,
                "min": 0.0,
                "max": 5.0,
                "step": 0.5,
            },
            "n_estimators_max": {
                "type": "slider",
                "dtype": "int",
                "label": "最大木数（early_stopping 上限）",
                "description": "early_stopping で実際の木数を自動決定。この値が上限。",
                "default": 500,
                "min": 100,
                "max": 2000,
                "step": 100,
            },
            "early_stopping_rounds": {
                "type": "slider",
                "dtype": "int",
                "label": "早期終了ラウンド数",
                "description": "検証誤差がこのラウンド数改善しなければ学習を停止。",
                "default": 40,
                "min": 10,
                "max": 100,
                "step": 10,
            },
        }

    def tuning_search_space(self) -> tuple:
        """ハイパーパラメータ自動探索の探索空間（Issue #266）。

        XGBoost 7軸（木構造・正則化）＋モメンタム2軸（use_momentum/momentum_window・
        M-1 と同一候補・ADR-0007 §5 のチャネル単位トグル）の9軸。momentum を探索できる
        のは build_snapshots のキャッシュキーが use_momentum/mom_window を含む（#298）ため
        ＝再構築は momentum 構成6種（off＋窓5種）ごとに1回だけで `_CACHE_MAXSIZE=8` 内に
        収まる。他の構造パラメータ（fin_features/macro_features/use_macro/min_coverage）は
        引き続き既定値に固定（min_coverage 併用はキー数 6×4=24 > 8 で LRU スラッシュ）。
        `n_estimators_max`/`early_stopping_rounds` は early_stopping が自動決定するため
        対象外（#264 設計方針）。全グリッドは組合せ爆発するため、呼び出し側
        （hyperparameter_search.py）は既定 strategy="random" を推奨。
        """
        from .tuning import SearchDim

        base_params: dict = {}
        dims = [
            # only_if は自分より前の軸しか参照できない → use_momentum を先に置く
            SearchDim("use_momentum",    [True, False]),
            SearchDim("momentum_window", [3, 6, 12, 18, 24],
                      only_if=lambda c: c.get("use_momentum") is True),
            SearchDim("max_depth",          [2, 4, 6, 8, 10]),
            SearchDim("learning_rate",      [0.01, 0.03, 0.05, 0.1, 0.2, 0.3]),
            SearchDim("subsample",          [0.4, 0.6, 0.8, 1.0]),
            SearchDim("colsample_bytree",   [0.4, 0.6, 0.8, 1.0]),
            SearchDim("min_child_weight",   [1, 5, 10, 20, 30]),
            SearchDim("reg_lambda",         [0.0, 0.5, 1.0, 2.0, 5.0, 10.0]),
            SearchDim("reg_alpha",          [0.0, 0.5, 1.0, 2.0, 5.0]),
        ]
        return base_params, dims

    def produced_output(self, db: Any) -> bool:
        """M-2 producer μ̂（macro_gbdt_scores）を共有DBに持つか（sell_ranking の graceful 判定用）。

        M-2 を一度ローカル実行すると execute() が μ̂ を永続化する。未実行なら False を返し、
        consumer（sell_ranking, mu_source=macro_gbdt）は graceful-degrade する（ADR-0004）。"""
        try:
            from database import get_macro_gbdt_scores
            return bool(get_macro_gbdt_scores(db))
        except Exception:
            return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        """M-1 と同一形 {edinet_code: {mu, r_macro, r1_prime}} を返す（sell_ranking 共用）。

        mu は永続化済み macro_gbdt_scores、r_macro は共有 macro_beta producer から
        マージ、r1_prime は常に None（XGBoost は OLS 予測SE を持たない・ADR-0003 §5）。"""
        from database import get_macro_gbdt_scores
        mus = get_macro_gbdt_scores(db)
        if not mus:
            return {}
        r_macro_src = get_producer_scores(db, macro_snapshot)
        out: dict = {}
        for ec, mu in mus.items():
            prod = r_macro_src.get(ec) or {}
            out[ec] = {
                "mu":       float(mu),
                "r_macro":  prod.get("r_macro"),
                "r1_prime": None,
            }
        return out

    # ── 兄弟モデル拡張フック（M-5 macro_gbdt_rank が override・Issue #362）──────────
    # 既定実装は M-2（MSE 回帰）の従来挙動そのもの。execute() 本体を共有しつつ、
    # 学習目的・fit_predict・最終モデル・producer 永続化の4点だけを差し替え可能にする。
    def _objective(self, params: dict) -> str:
        """XGBoost の学習目的（objective）。M-2 は MSE 固定。"""
        return "reg:squarederror"

    def _model_type(self) -> str:
        """結果メタの model_type ラベル。M-2 は "xgboost"。"""
        return "xgboost"

    def _make_cv_callback(self, xgb_params: dict, best_iterations: list) -> tuple:
        """walk-forward CV へ注入する (fit_predict, walk_forward追加kwargs) を返す。

        M-2 は XGBRegressor の early-stopping コールバック＋追加kwargsなし。M-5 は
        XGBRanker コールバック＋`pass_train_groups=True`（月クエリグループ境界の受渡）。"""
        return _make_xgb_fit_predict(xgb_params, best_iterations), {}

    def _fit_final_model(self, final_params: dict, n_est_final: int,
                         X_all, y_all, samples_by_ym: dict, feat_names: list):
        """全データ再学習の最終モデルを返す。M-2 は XGBRegressor。"""
        import xgboost as xgb
        model = xgb.XGBRegressor(**final_params, n_estimators=n_est_final)
        model.fit(X_all, y_all, verbose=False)
        return model

    def _persist_producer(self, db: Any, raw_items: list, rep_str: str | None) -> None:
        """producer μ̂ を macro_gbdt_scores へ永続化（sell_ranking が mu_source で読む）。

        M-2 のみ。M-5 のスコアは順位（リターン単位でない）ため producer を持たず no-op
        で override する（Issue #362・下流統合は「順位→分位期待リターン写像」を別途定義
        するまで見送り）。"""
        from database import replace_macro_gbdt_scores
        try:
            replace_macro_gbdt_scores(
                db,
                [{"edinet_code": it["edinet_code"], "mu": it["mu_raw"]} for it in raw_items],
                rep_str,
            )
        except Exception:
            pass   # 永続化失敗（読取専用DB等）は分析表示を妨げない・producer は次回実行で再生成

    def execute(self, params: dict, db: Any) -> dict:
        import xgboost as xgb
        import shap

        lambda_risk  = params["lambda_risk"]
        risk_axis    = params["risk_axis"]
        if risk_axis not in ("r2", "r_macro"):
            risk_axis = "r2"
        r3_gate      = params.get("r3_gate", 0.0)
        fin_features = params["fin_features"]
        use_macro    = params["use_macro"]
        macro_names  = list(params["macro_features"]) if use_macro else []
        use_momentum = params["use_momentum"]
        mom_window   = params["momentum_window"]
        min_coverage = params["min_coverage"]
        top_n        = params["top_n"]

        if not fin_features:
            raise ValueError("財務特徴量を1つ以上選択してください。")

        # ── データロード ─────────────────────────────────────────────────────
        prices_by_co, fin_by_co, companies = load_data(db)
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に収集を実行してください。")

        macro_cache = preload_macro(db, prices_by_co, macro_names) if macro_names else {}

        # ── スナップショット構築（交差項なし・M-2）───────────────────────────
        samples_by_ym, sample_meta_by_ym, current_snaps, all_feat_names = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions=False,
            macro_nan_ok=True,
        )

        total_samples = sum(len(v) for v in samples_by_ym.values())
        if total_samples < 20:
            raise ValueError(
                f"学習サンプルが不足（{total_samples}件）。データを収集してから再実行してください。"
            )

        # ── XGBoost パラメータ ────────────────────────────────────────────────
        xgb_params = {
            "max_depth":          params["max_depth"],
            "learning_rate":      params["learning_rate"],
            "subsample":          params["subsample"],
            "colsample_bytree":   params["colsample_bytree"],
            "min_child_weight":   params["min_child_weight"],
            "reg_lambda":         params["reg_lambda"],
            "reg_alpha":          params["reg_alpha"],
            "n_estimators":       params["n_estimators_max"],
            "early_stopping_rounds": params["early_stopping_rounds"],
            "tree_method":        "hist",
            "objective":          self._objective(params),
            "random_state":       42,
        }

        # ── XGBoost walk-forward CV ───────────────────────────────────────────
        best_iterations: list[int] = []
        xgb_callback, wf_extra = self._make_cv_callback(xgb_params, best_iterations)

        cv_folds_xgb, cv_residuals_xgb = walk_forward_cv_monthly(
            samples_by_ym, all_feat_names,
            min_train_months=6, step_months=3,
            return_residuals=True,
            fit_predict=xgb_callback,
            embargo_months=LABEL_HORIZON_MONTHS,  # 52週先ラベルの窓重複を purge（ADR-0014）
            **wf_extra,
        )

        # ── アウトオブサンプル検証（OOF）: 無リーク walk-forward 予測のモデル評価（ADR-0004）─
        # 既存「バックテスト」(/api/backtest) とは別概念。cv_residuals_xgb が揃った時点で
        # 算出可能（このあとの OLS ベースライン CV・全社スコアリング・SHAP 計算には非依存）。
        oof_bt = oof_backtest(cv_residuals_xgb, n_quantiles=5)

        # ── ハイパーパラメータ探索中は oof_backtest 算出後に早期return（Issue #299）───
        # plugins/tuning.py::search() が読むのは oof_backtest のみで、以降の OLS
        # ベースライン CV・最終モデル再学習・全社 raw_items 構築（SHAP 計算含む）は
        # 探索候補の評価には不要（かつ oof_backtest の値には一切影響しない）。通常の
        # API 実行（/api/plugins/{name}/run）ではこのモードは無効のため、常に従来通り
        # フル実行する。
        from database import is_tuning_objective_only
        if is_tuning_objective_only():
            return {
                "cv_metrics":        {"xgb": None, "ols_baseline": None},
                "selected_features": all_feat_names,
                "feature_coefs":     {},
                "n_train_samples":   total_samples,
                "n_companies":       0,
                "risk_axis":         risk_axis,
                "lambda_risk":       lambda_risk,
                "r3_gate":           r3_gate,
                "top_n":             top_n,
                "results":           [],
                "model_type":        self._model_type(),
                "best_iteration":    None,
                "oof_backtest":      oof_bt,
                "r_macro_available": False,
            }

        # ── OLS ベースライン CV（同一特徴量・交差項なし・BIC なし）────────────
        cv_folds_ols = walk_forward_cv_monthly(
            samples_by_ym, all_feat_names,
            min_train_months=6, step_months=3,
            return_residuals=False,
            fit_predict=None,  # 既定 OLS
            embargo_months=LABEL_HORIZON_MONTHS,  # XGB と同一 fold を保つ（比較の公平性）
        )

        def _cv_summary(folds):
            if not folds:
                return {"folds": [], "mean_r2": None, "mean_rmse": None, "n_folds": 0}
            return {
                "folds":     folds,
                "mean_r2":   round(statistics.mean(f["r2"]   for f in folds), 4),
                "mean_rmse": round(statistics.mean(f["rmse"] for f in folds), 4),
                "n_folds":   len(folds),
            }

        cv_metrics = {
            "xgb":          _cv_summary(cv_folds_xgb),
            "ols_baseline": _cv_summary(cv_folds_ols),
        }

        # ── R3 バケット CV-RMSE（M-1 と同一ロジック・_M1 の staticmethod を共有）─
        m1_inst = _M1()
        r3_data = m1_inst._compute_r3_buckets(cv_residuals_xgb, sample_meta_by_ym)

        # ── 最終モデル（全データで学習）─────────────────────────────────────
        n_est_final = (
            int(statistics.median(best_iterations))
            if best_iterations
            else params["n_estimators_max"] // 2
        )
        all_samples = [s for ym_s in samples_by_ym.values() for s in ym_s]
        X_all = np.array([s[0] for s in all_samples], dtype=float)
        y_all_raw = [s[1] for s in all_samples]
        y_all_w, _, _ = winsorize(y_all_raw)
        y_all = np.array(y_all_w, dtype=float)

        final_params = {k: v for k, v in xgb_params.items()
                        if k not in ("n_estimators", "early_stopping_rounds")}
        final_model = self._fit_final_model(
            final_params, n_est_final, X_all, y_all, samples_by_ym, all_feat_names
        )

        # ── スコアリング ─────────────────────────────────────────────────────
        codes_ordered = list(current_snaps.keys())
        X_current = np.array([current_snaps[c][0] for c in codes_ordered], dtype=float)
        mu_preds = final_model.predict(X_current).tolist()

        # ── SHAP（グローバル＋per-stock）────────────────────────────────────
        explainer = shap.TreeExplainer(final_model)
        shap_matrix = explainer.shap_values(X_current)  # (n_companies, n_features)

        global_shap = {
            name: round(float(np.abs(shap_matrix[:, i]).mean()), 6)
            for i, name in enumerate(all_feat_names)
        }

        # ── 全社 raw items 構築 ──────────────────────────────────────────────
        raw_items: list[dict] = []
        for j, edinet_code in enumerate(codes_ordered):
            _, info = current_snaps[edinet_code]
            mu_raw = float(mu_preds[j])
            price_rows = prices_by_co.get(edinet_code, [])
            snap_date  = info["snap_date"]
            r2 = _realized_vol(price_rows, snap_date, weeks=52)
            r3 = m1_inst._r3_for(info.get("industry"), info.get("size"), r3_data)
            stock_shap = {
                name: round(float(shap_matrix[j, i]), 4)
                for i, name in enumerate(all_feat_names)
            }
            raw_items.append({
                "edinet_code":  edinet_code,
                "sec_code":     info["sec_code"],
                "company_name": info["company_name"],
                "industry":     info["industry"],
                "mu_raw":       round(mu_raw, 6),
                "r1":           None,  # XGBoost は OLS 予測 SE を持たない（ADR-0003 §5）
                "r2":           round(r2, 6) if r2 is not None else None,
                "r3":           round(r3, 6) if r3 is not None else None,
                "shap":         stock_shap,
            })

        raw_items.sort(key=lambda x: x.get("mu_raw") or -1e18, reverse=True)

        # ── R_macro（macro_beta producer から流用・graceful degrade）────────────
        try:
            macro_beta_producer = get_producer_scores(db)
        except Exception:
            macro_beta_producer = {}

        for item in raw_items:
            prod = macro_beta_producer.get(item["edinet_code"])
            item["r_macro"] = (
                round(float(prod["r_macro"]), 6)
                if (prod and prod.get("r_macro") is not None)
                else None
            )
        # #273: r_macro が全社 None（macro_beta 未蓄積）かをクライアントへ明示。
        r_macro_available = any(item["r_macro"] is not None for item in raw_items)

        # oof_bt は cv_residuals_xgb が揃った時点（Issue #299 の早期return判定の直前）で
        # 算出済み（この後の全社スコアリングとは非依存のため、SHAP計算等より前に算出）。

        # ── producer μ̂ を永続化（sell_ranking が mu_source=macro_gbdt で読む・ADR-0004）─
        # M-2 は macro_gbdt_scores へ書く。M-5 は producer を持たず no-op（_persist_producer override）。
        _snap_dates = [current_snaps[c][1].get("snap_date") for c in codes_ordered]
        _snap_dates = [d for d in _snap_dates if d]
        _rep = max(_snap_dates) if _snap_dates else None
        _rep_str = (_rep.isoformat() if hasattr(_rep, "isoformat")
                    else (str(_rep)[:10] if _rep else None))
        self._persist_producer(db, raw_items, _rep_str)

        return {
            "cv_metrics":        cv_metrics,
            "selected_features": all_feat_names,
            "feature_coefs":     global_shap,   # mean|SHAP|（大きさのみ・方向なし）
            "n_train_samples":   total_samples,
            "n_companies":       len(raw_items),
            "risk_axis":         risk_axis,
            "lambda_risk":       lambda_risk,
            "r3_gate":           r3_gate,
            "top_n":             top_n,
            "results":           raw_items,
            "model_type":        self._model_type(),
            "best_iteration":    n_est_final,
            "oof_backtest":      oof_bt,
            "r_macro_available": r_macro_available,
        }


plugin = MacroGbdtPlugin()
