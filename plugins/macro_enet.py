"""
M-6 マクロ×財務 正則化線形（ElasticNet）推奨プラグイン（Issue #372 / ADR-0021）

M-2（macro_gbdt・XGBoost）の**線形兄弟**。Issue #372 の候補メニューで実測した結果、
本番パネル（57,955 サンプル・43ヶ月・71 特徴量・honest OOF・embargo=12）で

    OOF rank-IC  M-6 ElasticNet 0.1713  >  M-2 XGBoost 0.1419
    差 +0.0294（定常ブートストラップ 95%CI [+0.0116, +0.0469]・p=0.002・ADR-0018）

と **M-2 を有意に上回った**（8 候補中で唯一、多重比較補正 α/8=0.00625 を通過）。ADR-0021 の
昇格ゲートを満たしたため正式兄弟へ昇格した。低 S/N な日本株リターン予測では、木の非線形性より
**グループ共線性に対する縮小推定**（金利カーブ us5y/10y/30y・信用スプレッド・株価指数・
コモディティが各々ブロックを成す）のほうが効く、というのが本モデルが示した知見である。

設計:
  - **候補実装をそのまま使う**: CV の fit_predict は `model_candidates.make_elasticnet_fit_predict`
    を呼ぶ。ADR-0021 の実測値と本プラグインの OOF が**同一コードパス**であることを保証する
    （数字だけドキュメントに残って実装が乖離するのを防ぐ）。
  - **同一スナップショット・同一 fold**: `build_snapshots(build_interactions=False, macro_nan_ok=True)`
    と `walk_forward_cv_monthly(min_train_months=6, step_months=3, embargo_months=12)` は M-2 と同値。
    `model_comparison` で M-2 と apples-to-apples に並ぶ。
  - **解釈性**: 最終モデルの符号付き係数を `feature_coefs`（M-2 の mean|SHAP| と同じスロット）へ
    載せる。ElasticNet は L1 成分でゼロ係数を作るため「使われた特徴量」がそのまま読める。
  - **マクロ fold 内 PCA は載せない**: ADR-0021 の改善案④（マクロ 46 列を fold 内 PCA で圧縮）は
    同じ bake-off で実測したが、5 主成分で分散の 90.9% を説明しても rank-IC は 0.1713→0.1714 と
    不変、ターンオーバー・ブレークイーブンも横ばいで、木モデルではむしろ悪化した（LightGBM
    0.1474→0.1379）。**昇格ゲートを通らなかったため本プラグインには入れない**（ラッパー
    `model_candidates.wrap_macro_pca` は探索枠に残す）。
  - **producer なし（初版）**: M-5 と同様に OOF 比較専用とし、`macro_enet_scores` テーブル追加・
    `sell_ranking` の `mu_source` 統合は行わない（DB スキーマ変更は init_db が本番へ無条件反映される
    ため別 Issue で扱う）。予測は M-1/M-2 と同じ 52 週先対数リターン単位なので、統合自体は将来可能。
"""
import statistics
from typing import Any

import numpy as np

from .base import AnalysisPlugin
from .model_candidates import make_diag, make_elasticnet_fit_predict, summarize_diag
from .macro_snapshots import (
    FIN_BASE_OPTIONS,
    LABEL_HORIZON_MONTHS,
    MACRO_FEATURE_OPTIONS,
    PRICE_FEATURE_OPTIONS,
    _realized_vol,
    build_oof_meta,
    build_snapshots,
    conformal_bucket_halfwidths,
    conformal_halfwidth_for,
    get_producer_scores,
    load_data,
    oof_backtest,
    preload_macro,
)
from .macro_risk_return import MacroRiskReturnPlugin as _M1
from .utils import fit_feature_columns, normalize, transform_feature_row, walk_forward_cv_monthly, winsorize

# CV と最終学習で共有する ElasticNet の探索設定（ADR-0021 の実測と同一値）。
_L1_RATIOS = (0.1, 0.5, 0.9)
_N_ALPHAS = 20
_CV_SPLITS = 3
_MAX_ITER = 5000


class MacroEnetPlugin(AnalysisPlugin):
    name = "macro_enet"
    label = "M-6: マクロ×財務 正則化線形（ElasticNet）"
    description = (
        "M-2 と同一データ・同一 fold で、L1+L2 正則化した線形モデル（ElasticNet）を学習します。"
        "金利カーブや信用スプレッドのようなグループ共線性に強く、符号付き係数がそのまま読めます。"
        "本番パネルの honest OOF rank-IC で M-2（XGBoost）を有意に上回った兄弟です（ADR-0021）。"
        "【注意】株価週次履歴とマクロデータ5年分の蓄積が必要です。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    category = "③ 将来リターンを予測"
    ui_order = 390                       # M-5=380 の後（M-1→M-2→M-3→M-4→M-5→M-6 順）

    def params_schema(self) -> dict:
        return {
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
            "price_features": {
                "type": "multiselect",
                "label": "価格行動系特徴量（px_*）",
                "description": "週次実現ボラ・出来高z・52週高値乖離・4週リバーサル（M-2/M-3 と共有）。既定 OFF。",
                "options": PRICE_FEATURE_OPTIONS,
                "default": [],
            },
            "min_coverage": {
                "type": "slider",
                "dtype": "float",
                "label": "特徴量充足率下限",
                "description": (
                    "サンプル採用に必要な非欠損特徴量の最低割合。マクロ欠損は学習 fold 内の"
                    "列平均で補完する（線形モデルは NaN をネイティブ処理できないため）。"
                ),
                "default": 0.5,
                "min": 0.1,
                "max": 1.0,
                "step": 0.05,
            },
            "l1_ratio": {
                "type": "select",
                "label": "L1 比率の探索範囲",
                "description": (
                    "0 に近いほど Ridge 寄り（共線性グループで係数を分け合う）、1 に近いほど Lasso 寄り"
                    "（スパース）。既定は 0.1/0.5/0.9 を学習 fold 内 CV で自動選択。"
                ),
                "options": [
                    {"value": "auto",  "label": "自動選択（0.1 / 0.5 / 0.9・既定）"},
                    {"value": "ridge", "label": "Ridge 寄り固定（0.1）"},
                    {"value": "even",  "label": "均等固定（0.5）"},
                    {"value": "lasso", "label": "Lasso 寄り固定（0.9）"},
                ],
                "default": "auto",
            },
            "top_n": {
                "type": "number",
                "dtype": "int",
                "label": "上位表示件数",
                "default": 30,
                "min": 5,
                "max": 200,
            },
        }

    # ── producer を持たない（初版・M-5 と同じ扱い）──────────────────────────
    def produced_output(self, db: Any) -> bool:
        return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        return {}

    def tuning_search_space(self) -> tuple:
        """探索軸は構造パラメータのみ（α・l1_ratio は学習 fold 内 CV が自動決定するため対象外）。"""
        from .tuning import SearchDim

        return {}, [
            SearchDim("use_momentum", [True, False]),
            SearchDim("momentum_window", [3, 6, 12, 18, 24],
                      only_if=lambda c: c.get("use_momentum") is True),
        ]

    @staticmethod
    def _l1_ratios(param: str) -> tuple:
        return {"auto": _L1_RATIOS, "ridge": (0.1,), "even": (0.5,), "lasso": (0.9,)}[param]

    def execute(self, params: dict, db: Any) -> dict:
        fin_features = params["fin_features"]
        use_macro = params["use_macro"]
        macro_names = list(params["macro_features"]) if use_macro else []
        use_momentum = params["use_momentum"]
        mom_window = params["momentum_window"]
        price_features = list(params.get("price_features") or [])
        min_coverage = params["min_coverage"]
        l1_ratios = self._l1_ratios(params["l1_ratio"])
        top_n = params["top_n"]

        if not fin_features:
            raise ValueError("財務特徴量を1つ以上選択してください。")

        # ── データロード・スナップショット構築（M-2 と同一契約）────────────────
        prices_by_co, fin_by_co, companies = load_data(db)
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に収集を実行してください。")
        macro_cache = preload_macro(db, prices_by_co, macro_names) if macro_names else {}

        samples_by_ym, sample_meta_by_ym, current_snaps, all_feat_names, stock_ids_by_ym = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions=False,
            macro_nan_ok=True,
            price_features=price_features,
            return_stock_ids=True,
        )
        total_samples = sum(len(v) for v in samples_by_ym.values())
        if total_samples < 20:
            raise ValueError(
                f"学習サンプルが不足（{total_samples}件）。データを収集してから再実行してください。"
            )

        model_feat_names = all_feat_names

        # ── walk-forward CV（候補実装をそのまま注入＝ADR-0021 と同一コードパス）──
        diag = make_diag()
        fit_predict = make_elasticnet_fit_predict(
            l1_ratios=l1_ratios, n_alphas=_N_ALPHAS, cv_splits=_CV_SPLITS,
            max_iter=_MAX_ITER, diag=diag,
        )

        cv_folds, cv_residuals = walk_forward_cv_monthly(
            samples_by_ym, model_feat_names,
            min_train_months=6, step_months=3,
            return_residuals=True,
            fit_predict=fit_predict,
            embargo_months=LABEL_HORIZON_MONTHS,   # 52週先ラベルの窓重複を purge（ADR-0014）
        )

        oof_meta = build_oof_meta(stock_ids_by_ym, sample_meta_by_ym, cv_residuals.keys())
        oof_bt = oof_backtest(cv_residuals, n_quantiles=5,
                              meta_by_ym=oof_meta, rebalance_per_year=4)

        cv_diag = summarize_diag(diag)

        def _cv_summary(folds):
            if not folds:
                return {"folds": [], "mean_r2": None, "mean_rmse": None, "n_folds": 0}
            return {
                "folds":     folds,
                "mean_r2":   round(statistics.mean(f["r2"] for f in folds), 4),
                "mean_rmse": round(statistics.mean(f["rmse"] for f in folds), 4),
                "n_folds":   len(folds),
            }

        # ── ハイパラ探索／モデル比較中は OOF 算出後に早期 return（Issue #299）────
        from database import is_tuning_objective_only
        if is_tuning_objective_only():
            return {
                "cv_metrics":        {"enet": _cv_summary(cv_folds)},
                "selected_features": model_feat_names,
                "feature_coefs":     {},
                "cv_diagnostics":    cv_diag,
                "n_train_samples":   total_samples,
                "n_companies":       0,
                "top_n":             top_n,
                "results":           [],
                "model_type":        "elasticnet",
                "oof_backtest":      oof_bt,
                "r_macro_available": False,
            }

        # ── 最終モデル（全データ）: CV と同一前処理で fit し現在断面をスコアリング ──
        all_samples = [s for ym_s in samples_by_ym.values() for s in ym_s]
        codes_ordered = list(current_snaps.keys())
        current_rows = [current_snaps[c][0] for c in codes_ordered]
        coefs, mu_preds, final_meta = self._fit_final_and_score(
            all_samples, current_rows, l1_ratios)
        feature_coefs = {name: round(float(c), 6)
                         for name, c in zip(model_feat_names, coefs)}

        # ── リスク軸（M-2 と同一ヘルパーを共有）────────────────────────────
        m1_inst = _M1()
        r3_data = m1_inst._compute_r3_buckets(cv_residuals, sample_meta_by_ym)
        conformal_data = conformal_bucket_halfwidths(cv_residuals, sample_meta_by_ym)
        try:
            macro_beta_producer = get_producer_scores(db)
        except Exception:
            macro_beta_producer = {}

        raw_items: list[dict] = []
        for j, edinet_code in enumerate(codes_ordered):
            _, info = current_snaps[edinet_code]
            r2 = _realized_vol(prices_by_co.get(edinet_code, []), info["snap_date"], weeks=52)
            r3 = m1_inst._r3_for(info.get("industry"), info.get("size"), r3_data)
            r1p = conformal_halfwidth_for(info.get("industry"), info.get("size"), conformal_data)
            prod = macro_beta_producer.get(edinet_code)
            raw_items.append({
                "edinet_code":  edinet_code,
                "sec_code":     info["sec_code"],
                "company_name": info["company_name"],
                "industry":     info["industry"],
                "mu_raw":       round(float(mu_preds[j]), 6),
                "r1":           round(r1p, 6) if r1p is not None else None,
                "r2":           round(r2, 6) if r2 is not None else None,
                "r3":           round(r3, 6) if r3 is not None else None,
                "r_macro":      (round(float(prod["r_macro"]), 6)
                                 if (prod and prod.get("r_macro") is not None) else None),
            })
        raw_items.sort(key=lambda x: x.get("mu_raw") or -1e18, reverse=True)
        r_macro_available = any(it["r_macro"] is not None for it in raw_items)

        return {
            "cv_metrics":        {"enet": _cv_summary(cv_folds)},
            "selected_features": model_feat_names,
            # 符号付き係数（L1 でゼロになった特徴量はそのまま 0＝「使われなかった」が読める）。
            "feature_coefs":     feature_coefs,
            "cv_diagnostics":    cv_diag,
            "final_model":       final_meta,
            "n_train_samples":   total_samples,
            "n_companies":       len(raw_items),
            "top_n":             top_n,
            # 全社ではなく上位 top_n のみ返す（汎用レンダラが数千行の DOM を吐かないように）。
            "results":           raw_items[:top_n],
            "model_type":        "elasticnet",
            "oof_backtest":      oof_bt,
            "r_macro_available": r_macro_available,
        }

    @staticmethod
    def _fit_final_and_score(all_samples: list, current_rows: list, l1_ratios: tuple) -> tuple:
        """全学習データで ElasticNet を再学習し、現在断面 μ̂ と係数・メタを返す。

        CV の fit_predict（`make_elasticnet_fit_predict`）と同じ前処理・同じ探索設定を使う。
        現在断面にはラベルが無いため `fit_predict` をそのまま使い回せない（2 番目の返り値が
        テストラベルである契約のため）ので、同じ手順を「学習＝全サンプル／テスト＝現在断面」
        として明示的に組み立てる。
        """
        from sklearn.linear_model import ElasticNetCV
        from sklearn.model_selection import TimeSeriesSplit

        train = list(all_samples)
        test = [(row, 0.0) for row in current_rows]   # ラベルは未知（予測には一切使わない）

        n_feat = len(train[0][0])
        X_tr, win_p, norm_p = fit_feature_columns([s[0] for s in train], n_feat)
        y_w, _, _ = winsorize([s[1] for s in train])
        y_z, y_mu, y_sd = normalize(y_w, "zscore")
        X_te = [transform_feature_row(s[0], win_p, norm_p) for s in test]

        splits = max(2, min(_CV_SPLITS, len(X_tr) // 2 - 1))
        model = ElasticNetCV(
            l1_ratio=list(l1_ratios), alphas=_N_ALPHAS,
            cv=TimeSeriesSplit(n_splits=splits), max_iter=_MAX_ITER,
            random_state=42, selection="cyclic",
        )
        model.fit(np.asarray(X_tr, dtype=float)[:, 1:], np.asarray(y_z, dtype=float))
        mu = model.predict(np.asarray(X_te, dtype=float)[:, 1:]) * y_sd + y_mu
        meta = {
            "alpha":     round(float(model.alpha_), 6),
            "l1_ratio":  round(float(model.l1_ratio_), 4),
            "n_nonzero": int(np.count_nonzero(model.coef_)),
            "n_features": n_feat,
        }
        return model.coef_.tolist(), mu.tolist(), meta


plugin = MacroEnetPlugin()
