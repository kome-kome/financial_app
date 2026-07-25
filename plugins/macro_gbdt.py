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
  - SHAP: グローバル mean|SHAP|（feature_coefs スロット）＋署名付き重要度＋学習方向 corr
    ＋特徴量ペアの交互作用強度（Issue #371）＋全社 per-stock SHAP
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
    PRICE_FEATURE_OPTIONS,
    DEFAULT_PRICE_FEATURES,
    _realized_vol,
    load_data,
    preload_macro,
    build_snapshots,
    get_producer_scores,
    oof_backtest,
    build_oof_meta,
    conformal_bucket_halfwidths,
    conformal_halfwidth_for,
)
from .macro_risk_return import (
    MacroRiskReturnPlugin as _M1,
)

# ── 経済符号の事前知識（monotone_constraints・Issue #366）───────────────────────
# XGBoost の単調性制約で、符号が経済理論から明確な財務比率のみ「特徴量↑→52週先
# リターン↑（+1）/↓（−1）」を木の分岐に強制する。低 S/N な日本株リターン予測で
# 符号が経済理論と逆の過学習分岐を抑止する正則化（Chen & Guestrin 2016 KDD）。
#   +1: 高いほど将来リターンが高い（クオリティ・インカム）
#   −1: 高いほど将来リターンが低い（割高・高レバレッジ）
# マクロ系（符号がレジーム依存）・業種内Zスコア（z_*）・曖昧な成長/流動性指標・
# モメンタム・px_* は収載せず 0（制約なし）＝木の自由分岐に委ねる。本制約は符号の
# 「事前知識」の唯一の注入点。signed SHAP（#371・feature_shap_dir）は学習後に木が
# 実際に付けた方向の「事後」診断であり、本制約の事前符号とのクロスチェックに使える。
_MONOTONE_SIGN: dict[str, int] = {
    "pbr":       -1,   # 割高（高 PBR）→ バリュープレミアムの逆 → 将来リターン低
    "per":       -1,   # 割高（高 PER）→ 同上
    "de_ratio":  -1,   # 高レバレッジ → 財務リスク高 → 将来リターン低
    "roe":       +1,   # 高収益性（クオリティ）→ 将来リターン高
    "roa":       +1,   # 高収益性（クオリティ）→ 将来リターン高
    "op_margin": +1,   # 高営業利益率（クオリティ）→ 将来リターン高
    "div_yield": +1,   # 高配当利回り（インカム）→ 将来リターン高
}


def _build_monotone_constraints(feat_names: list) -> tuple:
    """all_feat_names の並び順に沿った monotone_constraints タプルを返す（Issue #366）。

    _MONOTONE_SIGN に載る財務比率のみ ±1、未収載（マクロ・z_*・曖昧な成長/流動性・
    モメンタム・px_*・交差項）は 0。XGBoost へ列位置で渡す（numpy 入力は列名を持た
    ないため位置整合が必須）。全要素 0 でも無害（制約なしと等価）。
    """
    return tuple(_MONOTONE_SIGN.get(name, 0) for name in feat_names)


# ── XGBoost fit_predict コールバックファクトリ ─────────────────────────────────

_VALID_FRAC = 0.2          # 学習データから時系列末尾の何割を early_stopping 検証用に使うか
_MIN_FIT_N  = 5            # この未満なら early_stopping を諦め固定 n_estimators にフォールバック

# ── SHAP 解釈性強化（signed SHAP＋交互作用・Issue #371）────────────────────────
_INTERACT_TOP_K   = 15     # グローバル交互作用の上位ペア表示件数
_INTERACT_MAX_ROWS = 800   # shap_interaction_values は O(n·F²) メモリ＋TreeSHAP O(n·T·D²) で
                           # 全社（数千）だと重い。断面を等間隔サブサンプルして上限を課す
                           # （グローバル平均 |交互作用| の順位付けには十分。決定的スライス）。


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


# ── SHAP 解釈性ヘルパー（Issue #371）──────────────────────────────────────────

def _signed_global_shap(shap_matrix: np.ndarray, X_current: np.ndarray,
                        feat_names: list) -> tuple[dict, dict]:
    """署名付きグローバル SHAP（Issue #371）。

    mean|SHAP| は大きさのみで方向を持たない。各特徴量について
      - 学習された単調方向 = corr(特徴量値, その SHAP 寄与) ∈ [-1, 1]
      - 署名付き重要度      = mean|SHAP| × 方向符号
    を返す。方向符号は monotone_constraints（#366）の事前符号とのクロスチェックにも使える。
    特徴量値・SHAP に NaN/定数が混じる列は方向 0（符号なし＝正扱い）へフォールバック。
    """
    signed: dict = {}
    direction: dict = {}
    for i, name in enumerate(feat_names):
        col = shap_matrix[:, i].astype(float)
        mag = float(np.abs(col).mean())
        xv = X_current[:, i].astype(float)
        mask = np.isfinite(xv) & np.isfinite(col)
        corr = 0.0
        if mask.sum() >= 2:
            xm, cm = xv[mask], col[mask]
            if xm.std() > 0 and cm.std() > 0:
                corr = float(np.corrcoef(xm, cm)[0, 1])
        sign = 1.0 if corr >= 0 else -1.0
        signed[name] = round(mag * sign, 6)
        direction[name] = round(corr, 4)
    return signed, direction


def _global_interactions(explainer, X_int: np.ndarray, feat_names: list) -> list:
    """グローバル SHAP 交互作用の上位ペア（Issue #371）。

    shap_interaction_values → (m, F, F)。対称行列の off-diagonal |交互作用| の断面平均で
    ペア強度を測り（ペア総効果 = [i,j]+[j,i]）、上位 _INTERACT_TOP_K を返す。対角（主効果）
    は除外。TreeSHAP が交互作用を出せない場合は握って空リストへ degrade。
    """
    try:
        inter = np.asarray(explainer.shap_interaction_values(X_int), dtype=float)
    except Exception:
        return []
    if inter.ndim != 3 or inter.shape[1] != len(feat_names):
        return []
    abs_inter = np.abs(inter).mean(axis=0)  # (F, F)
    F = len(feat_names)
    pairs = []
    for i in range(F):
        for j in range(i + 1, F):
            strength = float(abs_inter[i, j] + abs_inter[j, i])
            if strength > 0:
                pairs.append((feat_names[i], feat_names[j], strength))
    pairs.sort(key=lambda t: t[2], reverse=True)
    return [
        {"a": a, "b": b, "strength": round(s, 6)}
        for a, b, s in pairs[:_INTERACT_TOP_K]
    ]


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
            "price_features": {
                "type": "multiselect",
                "label": "価格行動系特徴量（px_*）",
                "description": (
                    "週次実現ボラ・出来高z・52週高値乖離・4週リバーサル（M-3 と共有・追加収集ゼロ）。"
                    "非線形/閾値効果が強く GBDT の得意領域。既定 OFF（use_momentum と同じ保守ゲート）。"
                    "OOF 前後比較で有効性を確認してから全選択を既定化する（検証→全選択化・Issue #364）。"
                ),
                "options": PRICE_FEATURE_OPTIONS,
                "default": [],
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
            "use_monotone_constraints": {
                "type": "checkbox",
                "label": "経済符号の単調性制約を使う",
                "description": (
                    "符号が経済理論から明確な財務比率（PBR/PER/D-E→負、ROE/ROA/"
                    "営業利益率/配当利回り→正）に単調性制約を課し、符号が理論と逆の"
                    "過学習分岐を抑止する（Issue #366）。マクロ・業種内Zスコア・"
                    "モメンタム・px_* は制約なし。既定 OFF：OOF rank-IC の ON/OFF 比較で"
                    "有効性（特に fold 間 std の低下）を確認してから既定化する"
                    "（use_momentum/px_* と同じ保守ゲート）。"
                ),
                "default": False,
            },
            "shap_interactions": {
                "type": "checkbox",
                "label": "SHAP 交互作用を算出",
                "description": (
                    "TreeSHAP の交互作用値で、どの特徴量ペアが非線形に効いているかを"
                    "可視化する（M-2 が自動学習する fin×macro 交互作用の中身・Issue #371）。"
                    f"計算コストが O(n·F²) のため断面を最大 {_INTERACT_MAX_ROWS} 社へ"
                    "等間隔サブサンプルして上限を課す。OFF で交互作用計算をスキップ。"
                ),
                "default": True,
            },
        }

    def tuning_search_space(self) -> tuple:
        """ハイパーパラメータ自動探索の探索空間（Issue #266）。

        XGBoost 7軸（木構造・正則化）＋モメンタム2軸（use_momentum/momentum_window・
        M-1 と同一候補・ADR-0007 §5 のチャネル単位トグル）＋符号事前知識1軸
        （use_monotone_constraints・#366）の10軸。use_monotone_constraints は build_snapshots
        のキャッシュキーに影響しない純 xgb_param のため再構築を誘発せず LRU も圧迫しない。
        momentum を探索できる
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
            SearchDim("use_monotone_constraints", [False, True]),
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

        mu / r1_prime は永続化済み macro_gbdt_scores、r_macro は共有 macro_beta producer
        からマージ。r1_prime はコンフォーマル区間半幅（Issue #365）＝ sell_ranking の R3 足切り
        ゲートが読む確実性軸。列未 migration / 旧スナップショットでは None（ゲート素通り）。"""
        from database import get_macro_gbdt_producer
        prods = get_macro_gbdt_producer(db)
        if not prods:
            return {}
        r_macro_src = get_producer_scores(db, macro_snapshot)
        out: dict = {}
        for ec, rec in prods.items():
            prod = r_macro_src.get(ec) or {}
            out[ec] = {
                "mu":       float(rec["mu"]),
                "r_macro":  prod.get("r_macro"),
                "r1_prime": rec.get("r1_prime"),
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
                [{"edinet_code": it["edinet_code"], "mu": it["mu_raw"],
                  "r1_prime": it.get("r1")} for it in raw_items],
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
        price_features = list(params.get("price_features") or [])
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
        samples_by_ym, sample_meta_by_ym, current_snaps, all_feat_names, stock_ids_by_ym = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, use_momentum, mom_window, min_coverage,
            build_interactions=False,
            macro_nan_ok=True,
            price_features=price_features,
            return_stock_ids=True,   # Issue #368: OOF ターンオーバー用に stock_id を回収
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

        # ── 経済符号の単調性制約（Issue #366）─────────────────────────────────
        # all_feat_names の列位置に整合したタプルを注入。xgb_params 経由のため CV の
        # fit_predict（_make_cv_callback→base_params）と最終モデル（final_params）双方へ
        # 自動伝播する。M-5（XGBRanker）も execute を継承し、XGBRanker は monotone_constraints
        # を受け付けるため同一符号表がランク学習にもそのまま効く。
        if params.get("use_monotone_constraints", False):
            xgb_params["monotone_constraints"] = _build_monotone_constraints(all_feat_names)

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
        # Issue #368: 業種中立rank-IC / 実効ターンオーバー / ブレークイーブンbps も算出（M-5 も
        # 本 execute を継承するため同時に得る）。step_months=3 の四半期リバランス（=4/年）。
        oof_meta = build_oof_meta(stock_ids_by_ym, sample_meta_by_ym, cv_residuals_xgb.keys())
        oof_bt = oof_backtest(cv_residuals_xgb, n_quantiles=5,
                              meta_by_ym=oof_meta, rebalance_per_year=4)

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
                "feature_coefs_signed": {},
                "feature_shap_dir":     {},
                "feature_interactions": [],
                "shap_interactions_available": False,
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

        # ── コンフォーマル区間半幅 r1_prime（確実性軸・Issue #365）────────────────
        # XGBoost は OLS 予測SE を持たないため、無リーク OOF 残差 |resid| の τ 分位を
        # (業種×サイズ)/業種/global 粒度で集計し per-stock の区間半幅とする（分割コンフォーマル・
        # Lei et al. 2018）。sell_ranking の R3 足切りゲートが読む。r3_data と同一の残差・
        # メタから算出（R3=√平均二乗残差=リスク軸／r1_prime=|resid| τ分位=確実性軸で役割は別）。
        conformal_data = conformal_bucket_halfwidths(cv_residuals_xgb, sample_meta_by_ym)

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
        shap_matrix = np.asarray(
            explainer.shap_values(X_current), dtype=float
        )  # (n_companies, n_features)

        global_shap = {
            name: round(float(np.abs(shap_matrix[:, i]).mean()), 6)
            for i, name in enumerate(all_feat_names)
        }

        # ── signed SHAP＋交互作用（解釈性強化・Issue #371）──────────────────
        # global_shap は大きさのみ。方向（学習された単調符号）と特徴量ペアの
        # 交互作用強度を追加し、M-2 が自動学習する非線形構造を可視化する。
        global_shap_signed, shap_direction = _signed_global_shap(
            shap_matrix, X_current, all_feat_names
        )
        if params.get("shap_interactions", True) and len(all_feat_names) >= 2:
            n_rows = X_current.shape[0]
            if n_rows > _INTERACT_MAX_ROWS:
                idx = np.linspace(0, n_rows - 1, _INTERACT_MAX_ROWS).astype(int)
                X_int = X_current[idx]
            else:
                X_int = X_current
            feature_interactions = _global_interactions(explainer, X_int, all_feat_names)
        else:
            feature_interactions = []
        shap_interactions_available = bool(feature_interactions)

        # ── 全社 raw items 構築 ──────────────────────────────────────────────
        raw_items: list[dict] = []
        for j, edinet_code in enumerate(codes_ordered):
            _, info = current_snaps[edinet_code]
            mu_raw = float(mu_preds[j])
            price_rows = prices_by_co.get(edinet_code, [])
            snap_date  = info["snap_date"]
            r2 = _realized_vol(price_rows, snap_date, weeks=52)
            r3 = m1_inst._r3_for(info.get("industry"), info.get("size"), r3_data)
            # r1_prime = コンフォーマル区間半幅（確実性軸・Issue #365）。XGBoost は OLS 予測SE を
            # 持たないため OOF 残差の τ 分位で代替。sell_ranking の R3 足切りゲートが読む。
            r1p = conformal_halfwidth_for(info.get("industry"), info.get("size"), conformal_data)
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
                "r1":           round(r1p, 6) if r1p is not None else None,  # コンフォーマル区間半幅（Issue #365）
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
            "feature_coefs":       global_shap,   # mean|SHAP|（大きさのみ・方向なし・後方互換）
            "feature_coefs_signed": global_shap_signed,      # 署名付き重要度（Issue #371）
            "feature_shap_dir":     shap_direction,          # 学習された単調方向 corr∈[-1,1]
            "feature_interactions": feature_interactions,    # 上位特徴量ペアの交互作用強度
            "shap_interactions_available": shap_interactions_available,
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
