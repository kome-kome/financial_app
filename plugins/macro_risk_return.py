"""
M-1 マクロ・リスク-リターン推奨プラグイン（Phase B）

財務比率 × マクロ要因の交差項を LassoLarsIC(BIC) で選択し、
リスク-リターン平面に各銘柄をプロットして推奨集合を選ぶ。

次元整合性（CLAUDE.md）:
  目的変数 = 52週先対数リターン（無次元）
  説明変数 = 財務比率・Zスコア・マクロ変化率/Zスコア・それらの交差項（全て無次元）

共有ロジックは macro_snapshots.py に集約（ADR-0003 §3）。
本モジュールは: BIC 特徴量選択・最終 OLS フィット・R3・スコアリング・プラグイン本体を保有。
"""
import math
import statistics
from collections import defaultdict
from typing import Any

import numpy as np

from .base import AnalysisPlugin
from .utils import (
    normalize,
    normalize_transform,
    ols,
    walk_forward_cv_monthly,
    winsorize,
)
from .macro_snapshots import (
    FINANCIAL_LAG_DAYS,
    HORIZON_WEEKS,
    FIN_BASE_OPTIONS,
    DEFAULT_FIN_FEATURES,
    _MACRO_MAP,
    MACRO_FEATURE_NAMES,
    MACRO_FEATURE_OPTIONS,
    DEFAULT_MACRO_FEATURES,
    _realized_vol,
    _find_applicable_fin,  # テスト後方互換の再エクスポート
    _macro_from_cache,     # テスト後方互換の再エクスポート
    load_data,
    preload_macro,
    build_snapshots,
    select_features_bic,
    producer_scores,
    get_producer_scores,
)
# R3（セクター×サイズ別 CV-RMSE）でバケットを採用する最小残差数。
R3_MIN_BUCKET_N = 5


def _pareto_frontier(
    items: list[dict],
    x_key: str = "r2",
    y_key: str = "mu_raw",
) -> set[str]:
    """Y が大きく X が小さい意味での非劣解（Pareto 最適）の edinet_code 集合を返す。"""
    dominated: set[str] = set()
    codes = [it["edinet_code"] for it in items]
    vals = {it["edinet_code"]: (it.get(x_key) or 0.0, it.get(y_key) or 0.0) for it in items}
    for code_a in codes:
        xa, ya = vals[code_a]
        for code_b in codes:
            if code_a == code_b:
                continue
            xb, yb = vals[code_b]
            # B が A を支配: B のリスク <= A のリスク AND B のリターン >= A のリターン（少なくとも一方は strict）
            if xb <= xa and yb >= ya and (xb < xa or yb > ya):
                dominated.add(code_a)
                break
    return set(codes) - dominated


# ── プラグイン本体 ────────────────────────────────────────────────────────────

class MacroRiskReturnPlugin(AnalysisPlugin):
    name = "macro_risk_return"
    label = "マクロ×リスク-リターン推奨"
    description = (
        "財務比率×マクロ要因の交差項を LassoLarsIC(BIC) で選択し、"
        "各銘柄を期待リターン（縦軸）×リスク（横軸）の散布図に配置して推奨集合を選びます。"
        "【注意】株価週次履歴とマクロデータ5年分の蓄積が必要です。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    category = "③ 将来リターンを予測"
    ui_order = 330

    def params_schema(self) -> dict:
        return {
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
                    "R2=実現ボラ（既定）/ R_macro=マクロ起因リスク √(βᵀΣ_macroβ)（per-stock β推論＝macro_beta 蓄積が必要）。"
                    "両者リターン単位で λ の次元整合 U=μ−λR が保たれる。"
                ),
                "options": [
                    {"value": "r2",      "label": "R2 実現ボラティリティ（既定）"},
                    {"value": "r_macro", "label": "R_macro マクロ起因リスク（β推論要）"},
                ],
                "default": "r2",
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
            "fin_features": {
                "type": "multiselect",
                "label": "財務ベース特徴量",
                "options": FIN_BASE_OPTIONS,
                "default": DEFAULT_FIN_FEATURES,
            },
            "use_macro": {
                "type": "checkbox",
                "label": "マクロ特徴量・交差項を使用",
                "default": True,
            },
            "macro_features": {
                "type": "multiselect",
                "label": "マクロ特徴量",
                "description": "use_macro=ON のとき、ここで選んだマクロ系列のみを特徴量・交差項に使う。",
                "options": MACRO_FEATURE_OPTIONS,
                "default": DEFAULT_MACRO_FEATURES,
            },
            "use_momentum": {
                "type": "checkbox",
                "label": "モメンタム特徴量を使用",
                "description": (
                    "12-1ヶ月モメンタムを特徴量に加える。ON は各スナップショットに過去履歴を要求"
                    "するため、週次株価の蓄積が浅い環境では walk-forward CV のフォールド数が減る"
                    "（既定 OFF＝マクロ ON のままでも CV が成立する）。マクロとは独立に切替可能。"
                ),
                "default": False,
            },
            "momentum_window": {
                "type": "number",
                "dtype": "int",
                "label": "モメンタム算出月数",
                "description": "use_momentum=ON のとき、何ヶ月前を起点に 12-1 モメンタムを測るか。",
                "default": 12,
                "min": 3,
                "max": 24,
            },
            "max_features": {
                "type": "slider",
                "dtype": "int",
                "label": "BIC 最大採用特徴量数",
                "default": 20,
                "min": 5,
                "max": 40,
                "step": 1,
            },
            "min_coverage": {
                "type": "slider",
                "dtype": "float",
                "label": "特徴量充足率下限",
                "description": "全特徴量が揃っているサンプルの最低割合。",
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
        }

    def produced_output(self, db: Any) -> bool:
        """macro_beta（per-stock 階層ベイズ推論結果）を共有DBに持つか（#217 の depends_on 充足判定）。

        推論バッチ（macro_beta_inference.py / Actions）が未実行なら False を返し、consumer は
        graceful-degrade する。"""
        try:
            from database import get_macro_beta
            meta, _ = get_macro_beta(db)
            return meta is not None
        except Exception:
            return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        """macro_beta producer スコアを返す。macro_snapshots.get_producer_scores の thin wrapper。"""
        return get_producer_scores(db, macro_snapshot)

    # ── テスト後方互換ラッパー（macro_snapshots への thin delegation）────────────

    def _load_data(self, db) -> tuple:
        return load_data(db)

    def _build_snapshots(self, prices_by_co, fin_by_co, companies, macro_cache,
                         fin_features, macro_names, use_momentum, mom_window, min_coverage) -> tuple:
        return build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions=True,
        )

    async def execute(self, params: dict, db: Any) -> dict:
        lambda_risk    = params["lambda_risk"]
        risk_axis      = params["risk_axis"]
        if risk_axis not in ("r2", "r_macro"):
            risk_axis = "r2"
        r3_gate        = params.get("r3_gate", 0.0)
        fin_features   = params["fin_features"]
        use_macro      = params["use_macro"]
        macro_features = params["macro_features"]
        use_momentum   = params["use_momentum"]
        mom_window     = params["momentum_window"]
        max_features   = params["max_features"]
        min_coverage   = params["min_coverage"]
        top_n          = params["top_n"]

        if not fin_features:
            raise ValueError("財務特徴量を1つ以上選択してください。")

        macro_names = list(macro_features) if use_macro else []

        prices_by_co, fin_by_co, companies = load_data(db)
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に収集を実行してください。")

        macro_cache = preload_macro(db, prices_by_co, macro_names) if macro_names else {}

        samples_by_ym, sample_meta_by_ym, current_snaps, all_feat_names = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions=True,
        )

        total_samples = sum(len(v) for v in samples_by_ym.values())
        if total_samples < 20:
            raise ValueError(f"学習サンプルが不足（{total_samples}件）。データを収集してから再実行してください。")

        # --- LassoLarsIC(BIC) 特徴量選択 ---
        selected_names = self._select_macro_features(
            samples_by_ym, all_feat_names, max_features=max_features
        )
        n_sel = len(selected_names)
        if n_sel == 0:
            raise ValueError("BIC 選択で有効な特徴量が選ばれませんでした。")

        # 選択済み特徴量の列インデックス
        sel_idx = [all_feat_names.index(n) for n in selected_names]
        samples_sel: dict[str, list] = {
            ym: [([row[i] for i in sel_idx], tgt) for row, tgt in pairs]
            for ym, pairs in samples_by_ym.items()
        }

        # --- Walk-Forward CV（残差も回収し R3 バケット CV-RMSE を算出）---
        cv_folds, cv_residuals_by_ym = walk_forward_cv_monthly(
            dict(samples_sel), selected_names, min_train_months=6, step_months=3,
            return_residuals=True,
        )
        cv_metrics = {
            "folds":     cv_folds,
            "mean_r2":   round(statistics.mean(f["r2"] for f in cv_folds), 4) if cv_folds else None,
            "mean_rmse": round(statistics.mean(f["rmse"] for f in cv_folds), 4) if cv_folds else None,
            "n_folds":   len(cv_folds),
        }

        # --- R3: セクター×サイズ・バケット別の CV 残差 RMSE（モデル信頼性）---
        r3_data = self._compute_r3_buckets(cv_residuals_by_ym, sample_meta_by_ym)

        # --- 最終モデル学習 ---
        beta, win_params, norm_params, y_mu, y_sd, XtX_inv, sigma2 = self._fit_final(
            samples_sel, n_sel
        )

        # --- スコアリング（全社の raw 値を返す。効用 U・パレート・並べ替え・top_n は
        #     λ/リスク軸に依存する後処理のためクライアント側で算出する）---
        results = self._score_companies(
            current_snaps, sel_idx, n_sel,
            beta, win_params, norm_params, y_mu, y_sd,
            XtX_inv, sigma2, prices_by_co, r3_data,
        )

        # 標準化係数（X・y とも z-score 正規化済のため特徴量間で大小比較可能）。
        # beta[0]=切片、beta[1:] が selected_names と整列。UI の係数バー表示に使う。
        feature_coefs = {
            name: round(float(beta[i + 1]), 4) for i, name in enumerate(selected_names)
        }

        # --- #214 producer: macro_beta があれば per-stock μ・R_macro・R1' を添付（無ければ {}・
        #     graceful degrade で従来 OLS 経路に影響しない）---
        try:
            macro_beta_producer = self.read_producer_scores(db)
        except Exception:
            macro_beta_producer = {}

        # per-stock R_macro を全社 raw 値に追加（#215 リスク軸 r_macro のデータソース）。
        # macro_beta 未蓄積なら None（クライアントは r_macro 軸選択時に null をフィルタ）。
        for item in results:
            prod = macro_beta_producer.get(item["edinet_code"])
            item["r_macro"] = (
                round(float(prod["r_macro"]), 6)
                if (prod and prod.get("r_macro") is not None)
                else None
            )
        # #273: r_macro が全社 None（macro_beta 未蓄積）かをクライアントへ明示。
        # UI は risk_axis の r_macro 選択肢を無効化し理由を表示する（サイレント空表示の防止）。
        r_macro_available = any(item["r_macro"] is not None for item in results)

        return {
            "cv_metrics":       cv_metrics,
            "selected_features": selected_names,
            "feature_coefs":    feature_coefs,
            "n_train_samples":  total_samples,
            "n_companies":      len(results),
            # クライアントの初期表示シード（λ・リスク軸・表示件数・ゲートは再計算なしで切替可能）
            "risk_axis":        risk_axis,
            "lambda_risk":      lambda_risk,
            "r3_gate":          r3_gate,
            "top_n":            top_n,
            "results":          results,
            "r_macro_available": r_macro_available,
        }

    # ── LassoLarsIC(BIC) 特徴量選択 ────────────────────────────────────────────

    def _select_macro_features(
        self,
        samples_by_ym: dict,
        all_feat_names: list[str],
        max_features: int = 20,
    ) -> list[str]:
        """LASSO-LARS パスを BIC 最小で切る特徴量選択（sklearn）。

        共有ロジックは `macro_snapshots.select_features_bic` に集約（ADR-0002 §1・
        `macro_beta_inference.select_shared_factors` と同一の pooled BIC 選択手続き）。
        """
        all_samples = [s for ym_s in samples_by_ym.values() for s in ym_s]
        if len(all_samples) < 5:
            return []
        X_raw = np.asarray([s[0] for s in all_samples], dtype=float)
        y_raw = np.asarray([s[1] for s in all_samples], dtype=float)
        idx = select_features_bic(X_raw, y_raw, max_features)
        return [all_feat_names[i] for i in idx]

    # ── 最終モデル学習 ───────────────────────────────────────────────────────

    def _fit_final(self, samples_sel: dict, n_sel: int) -> tuple:
        all_s = [s for ym_s in samples_sel.values() for s in ym_s]
        X_raw = [s[0] for s in all_s]
        y_raw = [s[1] for s in all_s]
        n = len(y_raw)

        win_params:  list[tuple] = []
        norm_params: list[tuple] = []
        X_n = [[1.0] + [0.0] * n_sel for _ in range(n)]
        for fi in range(n_sel):
            col = [X_raw[ri][fi] for ri in range(n)]
            col_w, wlo, whi = winsorize(col)
            win_params.append((wlo, whi))
            col_norm, p1, p2 = normalize(col_w, "zscore")
            norm_params.append((p1, p2))
            for ri, v in enumerate(col_norm):
                X_n[ri][fi + 1] = v

        y_w, _, _ = winsorize(y_raw)
        y_n, y_mu, y_sd = normalize(y_w, "zscore")

        res = ols(X_n, y_n)
        if res is None:
            raise ValueError("最終 OLS の計算に失敗しました。多重共線性の可能性があります。")

        beta = res["beta"]
        sse = sum((y_n[i] - res["yhat"][i]) ** 2 for i in range(n))
        df  = n - (n_sel + 1)
        sigma2 = sse / df if df > 0 else 1.0

        # (X^T X)^{-1} の対角（R1 計算用）
        X_np = np.array(X_n)
        try:
            XtX_inv = np.linalg.inv(X_np.T @ X_np)
        except np.linalg.LinAlgError:
            XtX_inv = None

        return beta, win_params, norm_params, y_mu, y_sd, XtX_inv, sigma2

    # ── R3: セクター×サイズ・バケット別 CV-RMSE（モデル信頼性）──────────────────

    @staticmethod
    def _size_bucket(size: float | None, thresholds: tuple | None) -> str | None:
        """総資産を S/M/L の三分位バケットへ割当てる。サイズ/閾値欠損は None。"""
        if size is None or size <= 0 or thresholds is None:
            return None
        t1, t2 = thresholds
        if size < t1:
            return "S"
        if size < t2:
            return "M"
        return "L"

    def _compute_r3_buckets(self, residuals_by_ym: dict, meta_by_ym: dict) -> dict:
        """walk-forward CV 残差を (sector, size三分位) バケットへ集計する。

        返り値は二乗残差の累積 {(sector,bucket):[sse,n]} / {sector:[sse,n]} / [sse,n] と
        三分位閾値。実 RMSE と粒度フォールバックは `_r3_for` が担う。
        サイズ閾値は「残差を持つサンプル」の母集団から決め、現企業へも同閾値を適用する。
        """
        sizes = [
            size
            for ym in residuals_by_ym
            for (_sector, size) in meta_by_ym.get(ym, [])
            if size is not None and size > 0
        ]
        thresholds: tuple | None = None
        if len(sizes) >= 3:
            ss = sorted(sizes)
            thresholds = (ss[len(ss) // 3], ss[2 * len(ss) // 3])

        bucket: dict = defaultdict(lambda: [0.0, 0])
        sector: dict = defaultdict(lambda: [0.0, 0])
        glob = [0.0, 0]
        for ym, resids in residuals_by_ym.items():
            metas = meta_by_ym.get(ym, [])
            for k, (yhat, ytrue) in enumerate(resids):
                if k >= len(metas):
                    break  # 添字対応が崩れた場合の安全策（通常は同長）
                sec, size = metas[k]
                e2 = (ytrue - yhat) ** 2
                glob[0] += e2; glob[1] += 1
                if sec:
                    s = sector[sec]; s[0] += e2; s[1] += 1
                    bkt = self._size_bucket(size, thresholds)
                    if bkt is not None:
                        b = bucket[(sec, bkt)]; b[0] += e2; b[1] += 1
        return {
            "bucket":     dict(bucket),
            "sector":     dict(sector),
            "global":     glob,
            "thresholds": thresholds,
        }

    def _r3_for(self, sector: str | None, size: float | None, r3_data: dict) -> float | None:
        """企業の (sector, size) から R3 = √(平均二乗残差) を返す。
        (sector,bucket) → sector → global の順に、最小残差数を満たす最も細かい粒度を採用。"""
        def _rmse(acc) -> float | None:
            return math.sqrt(acc[0] / acc[1]) if (acc and acc[1] > 0) else None

        bkt = self._size_bucket(size, r3_data.get("thresholds"))
        if sector and bkt is not None:
            acc = r3_data["bucket"].get((sector, bkt))
            if acc and acc[1] >= R3_MIN_BUCKET_N:
                return _rmse(acc)
        if sector:
            acc = r3_data["sector"].get(sector)
            if acc and acc[1] >= R3_MIN_BUCKET_N:
                return _rmse(acc)
        return _rmse(r3_data.get("global"))

    # ── スコアリング ─────────────────────────────────────────────────────────

    def _score_companies(
        self,
        current_snaps, sel_idx, n_sel,
        beta, win_params, norm_params, y_mu, y_sd,
        XtX_inv, sigma2, prices_by_co, r3_data,
    ) -> list[dict]:
        """全社の raw リスク-リターン指標を返す。

        効用 U・パレート判定・並べ替え・top_n は λ／リスク軸に依存する後処理であり、
        クライアント側で再計算なしに切替できるよう、ここでは算出しない（JS が担う）。
        μ 収縮（R1 依存・λ/軸に非依存）はモデル確定値のためサーバー側で行う。
        """
        raw_items: list[dict] = []

        for edinet_code, (feat_row, info) in current_snaps.items():
            if sel_idx and len(feat_row) <= max(sel_idx):
                continue

            # 選択済み列だけ抽出・正規化
            x_norm = [1.0]
            for fi, orig_ci in enumerate(sel_idx):
                v = feat_row[orig_ci]
                wlo, whi = win_params[fi]
                v_w = max(wlo, min(whi, v))
                x_norm.append(normalize_transform(v_w, *norm_params[fi]))

            # 予測リターン（log space → 年率）
            pred_log = sum(x_norm[j] * beta[j] for j in range(len(beta))) * y_sd + y_mu
            mu_raw = pred_log  # 無次元対数リターン ≈ 年率

            # R1: 予測標準誤差 se_obs = sqrt(sigma2 * (1 + x^T (X^TX)^{-1} x))
            r1: float | None = None
            if XtX_inv is not None:
                xv = np.array(x_norm)
                leverage = float(xv @ XtX_inv @ xv)
                r1 = math.sqrt(max(0.0, sigma2 * (1.0 + leverage)))

            # R2: 実現ボラ（直前52週）
            price_rows = prices_by_co.get(edinet_code, [])
            snap_date  = info["snap_date"]
            r2 = _realized_vol(price_rows, snap_date, weeks=52)

            # R3: セクター×サイズ・バケットの CV-RMSE（モデル信頼性・バケット解像度）
            r3 = self._r3_for(info.get("industry"), info.get("size"), r3_data)

            raw_items.append({
                "edinet_code":  edinet_code,
                "sec_code":     info["sec_code"],
                "company_name": info["company_name"],
                "industry":     info["industry"],
                "mu_raw":       round(mu_raw, 6),
                "r1":           round(r1, 6) if r1 is not None else None,
                "r2":           round(r2, 6) if r2 is not None else None,
                "r3":           round(r3, 6) if r3 is not None else None,
            })

        if not raw_items:
            return []

        # μ_raw 降順の安定既定順（クライアントは選択 λ/軸で U 並べ替えする）
        raw_items.sort(key=lambda x: x.get("mu_raw") or -1e18, reverse=True)
        return raw_items


plugin = MacroRiskReturnPlugin()
