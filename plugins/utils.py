"""プラグイン共通ユーティリティ。

VISION.md「サードパーティーライブラリ採用基準」に従い、統計推論および数値計算は
numpy / scipy / statsmodels を利用する（旧 Pure Python 実装からの移行済み）。
- OLS は numpy.linalg.lstsq（SVD ベース、数値安定）
- p 値は scipy.stats.t.sf（df < 30 でも正確）
- 詳細診断は statsmodels.OLS（Durbin-Watson・Jarque-Bera・F検定）
"""
import logging
import math
import statistics
from typing import Any

log = logging.getLogger(__name__)

import numpy as np
from scipy import stats as scipy_stats

# 予測値の log-space 上限（exp(LOG_PRED_CAP) ≈ 3.3 百万円/株）
LOG_PRED_CAP = 15.0


# ── パラメータ契約（param contract）の coerce seam ──────────────────────────
# CONTEXT.md「パラメータ契約」を参照。プラグインの params_schema() を型契約として読み、
# raw params を型付け・default 補完・bounds/membership 検証する単一の純粋関数。
# 各 execute 冒頭に散在していた int()/float()/isinstance().split()/clamp/or-default を
# ここへ集約する。違反は ValueError（runner が 400 へマップ）。
#
# 2軸: type = ウィジェット（select/multiselect/slider/number/checkbox/text/weights）、
#      dtype = データ型（int/float/str/list[str]/bool/dict）。
#      dtype は数値ウィジェット（number/slider）にのみ明示必須、他は type から推論する。

# widget type → 推論 dtype（数値は曖昧なため推論せず dtype 明示を要求する）
_WIDGET_DTYPE = {
    "select":      "str",
    "text":        "str",
    "multiselect": "list[str]",
    "checkbox":    "bool",
    "weights":     "dict",
}
_TRUE_STRINGS = {"true", "1", "on", "yes"}
_MISSING = object()


def _is_blank(v) -> bool:
    """欠損とみなす値（未指定・None・空文字・NaN）。NaN はフロントの空数値入力由来。"""
    if v is _MISSING or v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return False


def _resolve_dtype(key: str, field: dict) -> str:
    """フィールドの dtype を決める。number/slider は dtype 明示必須、他は widget から推論。"""
    if "dtype" in field:
        return field["dtype"]
    wtype = field.get("type")
    if wtype in ("number", "slider"):
        raise ValueError(
            f"schema バグ: '{key}' は {wtype} のため dtype（int/float）の明示が必須です"
        )
    inferred = _WIDGET_DTYPE.get(wtype)
    if inferred is None:
        raise ValueError(f"schema バグ: '{key}' の type='{wtype}' は未知のウィジェットです")
    return inferred


def _coerce_scalar(key: str, dtype: str, value):
    """単一スカラーを dtype に従って変換する。変換不能は ValueError。"""
    if dtype == "int":
        if isinstance(value, bool):          # bool は int サブクラスのため明示的に弾く
            raise ValueError(f"'{key}' は整数である必要があります")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise ValueError(f"'{key}' は整数である必要があります（{value}）")
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            raise ValueError(f"'{key}' を整数に変換できません: {value!r}")
    if dtype == "float":
        if isinstance(value, bool):
            raise ValueError(f"'{key}' は数値である必要があります")
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (ValueError, TypeError):
            raise ValueError(f"'{key}' を数値に変換できません: {value!r}")
    if dtype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in _TRUE_STRINGS
    if dtype == "str":
        return str(value)
    raise ValueError(f"内部エラー: 未対応 dtype '{dtype}'")


def coerce_params(schema: dict, raw: dict) -> dict:
    """パラメータ契約（schema）に従い raw params を型付け・default 補完・検証する。

    処理（フィールドごと）:
      1. dtype 解決（number/slider は int/float 明示、他は type から推論）
      2. 欠損（_is_blank）→ default があれば適用（None 可）／optional なら None／
         どちらも無ければ ValueError（必須欠落）
      3. dtype 別 coerce（int/float/str/bool/list[str]/dict）。変換不能は ValueError
      4. bounds（min/max、数値のみ）違反は ValueError（reject）
      5. membership（select/multiselect の options）違反は ValueError（reject）

    出力は schema 宣言キーのみ（raw の未知キーは無視）。意味的 validation
    （features 非空・weights 合計≠0 等）は execute 側に残す。
    """
    out: dict = {}
    for key, field in schema.items():
        if not isinstance(field, dict):
            continue
        dtype = _resolve_dtype(key, field)
        raw_val = raw.get(key, _MISSING)

        # ── 欠損処理 ──────────────────────────────────────────────
        if _is_blank(raw_val):
            if "default" in field:
                out[key] = field["default"]
            elif field.get("optional"):
                out[key] = None
            else:
                raise ValueError(f"'{key}' は必須です")
            continue

        # ── dtype 別 coerce ───────────────────────────────────────
        if dtype == "list[str]":
            if isinstance(raw_val, str):
                val = [s.strip() for s in raw_val.split(",") if s.strip()]
            elif isinstance(raw_val, (list, tuple)):
                val = [str(x).strip() for x in raw_val if str(x).strip()]
            else:
                raise ValueError(f"'{key}' はリストである必要があります: {raw_val!r}")
        elif dtype == "dict":
            if not isinstance(raw_val, dict):
                raise ValueError(f"'{key}' は辞書である必要があります: {raw_val!r}")
            val = {str(k): _coerce_scalar(key, "float", v) for k, v in raw_val.items()}
        else:
            val = _coerce_scalar(key, dtype, raw_val)

        # ── bounds（数値のみ・reject）─────────────────────────────
        if dtype in ("int", "float"):
            lo, hi = field.get("min"), field.get("max")
            if lo is not None and val < lo:
                raise ValueError(f"'{key}' は {lo} 以上である必要があります（{val}）")
            if hi is not None and val > hi:
                raise ValueError(f"'{key}' は {hi} 以下である必要があります（{val}）")

        # ── membership（select/multiselect・reject）───────────────
        if field.get("type") in ("select", "multiselect"):
            allowed = {
                o["value"] for o in field.get("options", [])
                if isinstance(o, dict) and "value" in o
            }
            if allowed:
                check = val if isinstance(val, list) else [val]
                bad = [v for v in check if v not in allowed]
                if bad:
                    raise ValueError(f"'{key}' に無効な値があります: {bad}")

        out[key] = val
    return out


def shares_outstanding(record) -> float | None:
    """発行済株式数を返す（優先順位: XBRL期末値 → J-Quantsマスタ値 → BS推計）。

    1. record.issued_shares   — XBRL 直接収集列（期末発行済株式数タグ）
    2. record.company.issued_shares — J-Quants /markets/listed/info で取得した最新値
    3. bs_total_equity / bs_bps     — 純資産÷BPS 推計（外れ値リスクあり）

    bs_bps が誤った XBRL タグを捕捉すると推計値が実際の数十万倍になる企業が
    散見される（実DB比較で中央乖離 3.2%、p95 で 44%）。
    """
    issued = getattr(record, "issued_shares", None)
    if issued and issued > 0:
        return float(issued)
    # J-Quants マスタ値（company リレーション経由・遅延ロード）
    company = getattr(record, "company", None)
    co_issued = getattr(company, "issued_shares", None) if company is not None else None
    if co_issued and co_issued > 0:
        return float(co_issued)
    eq = getattr(record, "bs_total_equity", None)
    bps = getattr(record, "bs_bps", None)
    if eq and bps and bps > 0:
        return float(eq) / float(bps)
    return None


def winsorize(vals: list[float], lo_pct: float = 1.0, hi_pct: float = 99.0) -> tuple[list[float], float, float]:
    """外れ値を lo/hi パーセンタイルでクリップする。(clipped_vals, lo_bound, hi_bound) を返す。"""
    n = len(vals)
    if n < 4:
        return vals, min(vals), max(vals)
    sv = sorted(vals)
    lo_i = max(0, int(n * lo_pct / 100))
    hi_i = min(n - 1, int(math.ceil(n * hi_pct / 100)) - 1)
    lo, hi = sv[lo_i], sv[hi_i]
    if lo >= hi:
        return vals, lo, hi
    return [max(lo, min(hi, v)) for v in vals], lo, hi


def normalize(vals: list, method: str) -> tuple[list, float, float]:
    """正規化。(normed, param1, param2) を返す。"""
    if method == "log":
        vals = [math.log(max(v, 1e-9)) for v in vals]
        mu = statistics.mean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
        sd = sd or 1.0
        return [(v - mu) / sd for v in vals], mu, sd
    if method == "minmax":
        mn, mx = min(vals), max(vals)
        r = mx - mn or 1.0
        return [(v - mn) / r for v in vals], mn, r
    # zscore (default)
    mu = statistics.mean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
    sd = sd or 1.0
    return [(v - mu) / sd for v in vals], mu, sd


def _two_sided_pvalue(t: float, df: int) -> float:
    """両側 p 値。scipy.stats.t.sf による正確な計算（df < 30 でも正しい裾確率）。

    sf(x, df) = 1 - cdf(x, df) は |t| が大きい時に数値的に安定（cdf ≈ 1 の桁落ち回避）。
    """
    if df <= 0 or not math.isfinite(t):
        return float("nan")
    return float(2.0 * scipy_stats.t.sf(abs(t), df))


def ols(X: list, y: list) -> dict | None:
    """OLS 回帰（numpy.linalg.lstsq + scipy.stats.t）。

    SVD ベースの最小二乗法を用いるため、条件数の悪い行列でも数値的に安定。
    返り値: {beta, yhat, r2, adj_r2, rmse, mae, se, t_stat, p_value, df, rank,
            condition_number}。rank < p の場合は se / t_stat / p_value が NaN。
    詳細統計診断（Durbin-Watson・Jarque-Bera・F検定）が必要な場合は
    `ols_with_diagnostics()` を使用すること。
    """
    X_np = np.asarray(X, dtype=float)
    y_np = np.asarray(y, dtype=float)
    if X_np.ndim != 2 or len(X_np) == 0:
        return None
    n, p = X_np.shape

    # SVD ベースの最小二乗解（rcond=None で機械イプシロン × max(n,p) × max(sv)）
    try:
        beta_np, _residuals, rank, sv = np.linalg.lstsq(X_np, y_np, rcond=None)
    except np.linalg.LinAlgError:
        return None
    rank = int(rank)

    yhat_np = X_np @ beta_np
    resid = y_np - yhat_np
    sse = float((resid ** 2).sum())
    ymean = float(y_np.mean())
    sst = float(((y_np - ymean) ** 2).sum())
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / (n - p - 1) if n > p + 1 else r2
    rmse = math.sqrt(sse / n)
    mae = float(np.abs(resid).mean())

    # 標準誤差・t統計量・p値（df = n - p）。
    # df ≤ 0 または rank < p は推定不能として NaN。
    df = n - p
    se: list[float] = [float("nan")] * p
    t_stat: list[float] = [float("nan")] * p
    p_value: list[float] = [float("nan")] * p
    if df > 0 and rank == p:
        sigma2 = sse / df
        try:
            XtX_inv = np.linalg.inv(X_np.T @ X_np)
            for i in range(p):
                var_i = float(sigma2 * XtX_inv[i, i])
                if var_i >= 0:
                    se[i] = math.sqrt(var_i)
                    if se[i] > 0:
                        t_stat[i] = float(beta_np[i]) / se[i]
                        p_value[i] = _two_sided_pvalue(t_stat[i], df)
        except np.linalg.LinAlgError:
            pass  # 数値特異 — NaN のまま

    # 条件数（特異値の最大 / 最小）。大きいほど数値的に不安定
    cond_number = float(sv[0] / sv[-1]) if len(sv) > 0 and sv[-1] > 0 else float("inf")

    return {
        "beta": [float(b) for b in beta_np],
        "yhat": [float(v) for v in yhat_np],
        "r2": r2, "adj_r2": adj_r2,
        "rmse": rmse, "mae": mae,
        "se": se, "t_stat": t_stat, "p_value": p_value, "df": df,
        "rank": rank,
        "condition_number": cond_number,
    }


def ols_with_diagnostics(X: list, y: list, cov_type: str = "nonrobust") -> dict | None:
    """statsmodels.OLS を用いた詳細統計診断付き OLS。

    cov_type:
        - "nonrobust": 標準 (homoskedastic) 標準誤差
        - "HC3": White の不均一分散頑健 SE（推奨：金融データは異分散が多い）
        - "HC0", "HC1", "HC2": 他の HC 系
    返り値は ols() に以下を追加:
        - durbin_watson: 残差自己相関の検定統計量（1.5〜2.5 なら問題なし）
        - jarque_bera: {stat, pvalue, skew, kurtosis} — 残差正規性
        - f_stat: モデル全体の F 統計量
        - f_pvalue: F 検定の p 値（モデルが意味あるかどうか）
        - cov_type: 使用した共分散行列タイプ
    """
    import statsmodels.api as sm
    from statsmodels.stats.stattools import durbin_watson, jarque_bera

    X_np = np.asarray(X, dtype=float)
    y_np = np.asarray(y, dtype=float)
    if X_np.ndim != 2 or len(X_np) == 0:
        return None
    n, p = X_np.shape

    try:
        model = sm.OLS(y_np, X_np)
        if cov_type == "nonrobust":
            res = model.fit()
        else:
            res = model.fit(cov_type=cov_type)
    except Exception as e:
        log.debug(f"OLS失敗: {e}")
        return None

    df = int(res.df_resid)
    yhat_np = np.asarray(res.fittedvalues)
    resid = np.asarray(res.resid)

    # JB は残差サンプル数が少なすぎると意味がない（n < 8 はスキップ）
    if n >= 8:
        try:
            jb_stat, jb_p, skew, kurt = jarque_bera(resid)
            jb = {
                "stat": float(jb_stat), "pvalue": float(jb_p),
                "skew": float(skew), "kurtosis": float(kurt),
            }
        except Exception as e:
            log.debug(f"Jarque-Bera 計算失敗（jb=None でフォールバック）: {e}")
            jb = None
    else:
        jb = None

    try:
        dw = float(durbin_watson(resid))
    except Exception as e:
        log.debug(f"Durbin-Watson 計算失敗（dw=NaN でフォールバック）: {e}")
        dw = float("nan")

    return {
        "beta": [float(b) for b in res.params],
        "yhat": [float(v) for v in yhat_np],
        "r2": float(res.rsquared),
        "adj_r2": float(res.rsquared_adj),
        "rmse": math.sqrt(float((resid ** 2).mean())),
        "mae": float(np.abs(resid).mean()),
        "se": [float(s) for s in res.bse],
        "t_stat": [float(t) for t in res.tvalues],
        "p_value": [float(pv) for pv in res.pvalues],
        "df": df,
        "rank": int(res.df_model) + 1,  # +1 for intercept-or-not (statsmodels 流儀)
        "condition_number": float(res.condition_number) if hasattr(res, "condition_number") else float("nan"),
        # 追加診断
        "durbin_watson": dw,
        "jarque_bera": jb,
        "f_stat": float(res.fvalue) if res.fvalue is not None else float("nan"),
        "f_pvalue": float(res.f_pvalue) if res.f_pvalue is not None else float("nan"),
        "cov_type": cov_type,
    }


def ridge_regression(X: list, y: list,
                     alphas: list[float] | None = None,
                     cv_folds: int = 5) -> dict | None:
    """Ridge 回帰（L2 正則化）。多重共線性が高い特徴量で OLS が不安定な場合の代替。

    `sklearn.linear_model.RidgeCV` で `alphas` から GCV 経由で最適 α を選択する。
    返り値は `ols()` と同じスキーマ（beta, yhat, r2, adj_r2, rmse, mae, df,
    rank, condition_number）。SE / t / p 値は Ridge では伝統的に定義されないため
    NaN を返す（推論より予測精度を重視する場合に適切）。
    追加で `alpha`（選択された正則化パラメータ）を返す。

    Args:
        X: 設計行列（切片列を含む）
        y: 目的変数
        alphas: 探索する α 候補（None なら `[1e-3, 1e-2, 0.1, 1, 10, 100, 1000]`）
        cv_folds: alpha 選択時の CV fold 数（≥ 2）
    """
    from sklearn.linear_model import RidgeCV

    X_np = np.asarray(X, dtype=float)
    y_np = np.asarray(y, dtype=float)
    if X_np.ndim != 2 or len(X_np) == 0:
        return None
    n, p = X_np.shape

    if alphas is None:
        alphas = [1e-3, 1e-2, 0.1, 1.0, 10.0, 100.0, 1000.0]

    # 切片は X に含めて RidgeCV(fit_intercept=False) とする（既存呼び出しと一貫）
    try:
        model = RidgeCV(
            alphas=alphas,
            fit_intercept=False,
            cv=min(cv_folds, max(2, n // 5)) if n >= 10 else None,
            scoring="neg_mean_squared_error",
        )
        model.fit(X_np, y_np)
    except Exception as e:
        log.debug(f"Ridge 回帰の fitting 失敗（None を返却）: {e}")
        return None

    beta_np = np.asarray(model.coef_)
    yhat_np = X_np @ beta_np
    resid = y_np - yhat_np
    sse = float((resid ** 2).sum())
    ymean = float(y_np.mean())
    sst = float(((y_np - ymean) ** 2).sum())
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / (n - p - 1) if n > p + 1 else r2
    rmse = math.sqrt(sse / n)
    mae = float(np.abs(resid).mean())

    return {
        "beta":  [float(b) for b in beta_np],
        "yhat":  [float(v) for v in yhat_np],
        "r2": r2, "adj_r2": adj_r2,
        "rmse": rmse, "mae": mae,
        "se":      [float("nan")] * p,
        "t_stat":  [float("nan")] * p,
        "p_value": [float("nan")] * p,
        "df": n - p,
        "rank": p,
        "condition_number": float("nan"),
        "alpha": float(model.alpha_),
        "method": "ridge",
    }


def normalize_transform(val: float, param1: float, param2: float, method: str = "zscore") -> float:
    """学習データから求めたパラメータでスカラー値を変換する。z-score は ±5 にクリップ。"""
    if method == "log":
        z = (math.log(max(val, 1e-9)) - param1) / (param2 or 1.0)
    elif method == "minmax":
        z = (val - param1) / (param2 or 1.0)
    else:
        z = (val - param1) / (param2 or 1.0)
    return max(-5.0, min(5.0, z))


def check_collinearity(X_cols: list[list[float]], feature_names: list[str],
                       corr_threshold: float = 0.9,
                       vif_threshold: float = 10.0) -> dict:
    """多重共線性チェック。Pearson 相関行列と VIF を計算する。

    X_cols: 列指向の特徴量行列 [feature1_vals, feature2_vals, ...]
    feature_names: 各列の名前。len(X_cols) と一致する必要がある。

    Returns:
        {
            "correlation": [[float]],          # k×k 行列（対称、対角=1）
            "vif": [float],                    # 各特徴量の VIF（計算不能なら NaN）
            "high_corr_pairs": [(i, j, r)],    # |r| > corr_threshold の組
            "high_vif": [(i, vif)],            # vif > vif_threshold の特徴量
        }

    VIF (Variance Inflation Factor) は特徴量 i を他の特徴量で回帰した R² から
    VIF_i = 1 / (1 - R²_i) で求める。VIF > 10 で多重共線性ありと判断するのが
    慣例（Kutner et al. 2005）。
    """
    k = len(X_cols)
    if k == 0 or len(X_cols[0]) < 3 or len(feature_names) != k:
        return {"correlation": [], "vif": [], "high_corr_pairs": [], "high_vif": []}

    n = len(X_cols[0])

    # ── Pearson 相関行列（np.corrcoef でベクトル化）──────────────────
    X_arr = np.array(X_cols, dtype=float)  # shape: (k, n)
    corr_matrix = np.corrcoef(X_arr)       # shape: (k, k)、ゼロ分散列は NaN
    corr = corr_matrix.tolist()
    high_corr_pairs: list[tuple] = []
    for i in range(k):
        for j in range(i + 1, k):
            r = corr_matrix[i, j]
            if not np.isnan(r) and abs(r) > corr_threshold:
                high_corr_pairs.append((i, j, round(float(r), 4)))

    # ── VIF（各特徴量を他の特徴量で OLS 回帰）───────────────────────
    vif: list[float] = []
    high_vif: list[tuple] = []
    if k < 2:
        # 特徴量が 1 つしかない場合、VIF は定義できない（常に 1）
        vif = [1.0]
    else:
        for i in range(k):
            # 特徴量 i を目的変数、その他を説明変数として OLS
            y_i = X_cols[i]
            X_others = [
                [1.0] + [X_cols[j][r] for j in range(k) if j != i]
                for r in range(n)
            ]
            res = ols(X_others, y_i)
            if res is None or res["r2"] >= 0.9999:
                # 完全共線（行列特異 or R²≈1）→ VIF は無限大相当
                vif_i = float("inf")
            else:
                vif_i = 1.0 / max(1.0 - res["r2"], 1e-12)
            vif.append(vif_i)
            if vif_i > vif_threshold:
                high_vif.append((i, round(vif_i, 2) if math.isfinite(vif_i) else None))

    return {
        "correlation": [[round(v, 4) if v == v else None for v in row] for row in corr],
        "vif": [round(v, 2) if math.isfinite(v) else None for v in vif],
        "high_corr_pairs": [
            {"feature_a": feature_names[i], "feature_b": feature_names[j], "r": r}
            for i, j, r in high_corr_pairs
        ],
        "high_vif": [
            {"feature": feature_names[i], "vif": v}
            for i, v in high_vif
        ],
    }


def _fit_predict_fold(
    train_samples: list[tuple],
    test_samples: list[tuple],
    n_feat: int,
    y_norm_method: str = "zscore",
) -> tuple[list[float], list[float]] | None:
    """フォールドの学習・予測を実行し (yhat_orig, y_test_orig) を返す。失敗時は None。

    kfold_cv / walk_forward_cv_monthly の共通フレーム:
    winsorize → normalize → OLS → 予測 → 逆変換。
    """
    X_train_raw = [s[0] for s in train_samples]
    y_train_raw = [s[1] for s in train_samples]

    norm_params: list[tuple[float, float]] = []
    win_params:  list[tuple[float, float]] = []
    X_train_norm = [[1.0] + [0.0] * n_feat for _ in range(len(X_train_raw))]
    for fi in range(n_feat):
        col = [row[fi] for row in X_train_raw]
        col_w, w_lo, w_hi = winsorize(col)
        win_params.append((w_lo, w_hi))
        normed, p1, p2 = normalize(col_w, "zscore")
        norm_params.append((p1, p2))
        for ri, v in enumerate(normed):
            X_train_norm[ri][fi + 1] = v

    y_train, y_mu, y_sd = normalize(y_train_raw, y_norm_method)
    result = ols(X_train_norm, y_train)
    if not result:
        return None
    beta = result["beta"]

    X_test_norm = []
    for s in test_samples:
        row = [1.0]
        for fi, v in enumerate(s[0]):
            w_lo, w_hi = win_params[fi]
            v_w = max(w_lo, min(w_hi, v))
            p1, p2 = norm_params[fi]
            row.append(normalize_transform(v_w, p1, p2))
        X_test_norm.append(row)

    yhat_norm = [sum(row[j] * beta[j] for j in range(len(beta))) for row in X_test_norm]
    if y_norm_method == "log":
        yhat_orig = [math.exp(min(v * y_sd + y_mu, LOG_PRED_CAP)) for v in yhat_norm]
    else:
        yhat_orig = [v * y_sd + y_mu for v in yhat_norm]
    return yhat_orig, [s[1] for s in test_samples]


def kfold_cv(samples: list, n_folds: int = 5,
             y_norm_method: str = "log") -> list[dict]:
    """
    横断的 k-fold CV。
    samples: [(feature_row: list[float], target: float), ...]
    Returns: [{fold, n_train, n_test, r2, rmse_pct}, ...]
    各 fold で学習データの正規化パラメータのみを使いテストを評価する（リーク防止）。
    """
    import random
    n = len(samples)
    n_feat = len(samples[0][0]) if n > 0 else 0
    if n < n_folds * 2 or n_feat == 0:
        return []

    rng = random.Random(42)
    indices = list(range(n))
    rng.shuffle(indices)
    fold_size = n // n_folds

    fold_results = []
    for fold in range(n_folds):
        test_idx  = set(indices[fold * fold_size: (fold + 1) * fold_size])
        train_idx = [i for i in indices if i not in test_idx]

        train_samples = [samples[i] for i in train_idx]
        test_samples  = [samples[i] for i in list(test_idx)]

        fold_res = _fit_predict_fold(train_samples, test_samples, n_feat, y_norm_method)
        if fold_res is None:
            continue
        yhat_orig, y_test_orig = fold_res

        ymean = statistics.mean(y_test_orig)
        sst = sum((v - ymean) ** 2 for v in y_test_orig)
        sse = sum((yhat_orig[i] - v) ** 2 for i, v in enumerate(y_test_orig))
        r2 = 1 - sse / sst if sst > 0 else 0.0
        rmse = math.sqrt(sse / len(y_test_orig))
        rmse_pct = rmse / abs(ymean) * 100 if ymean != 0 else 0.0

        fold_results.append({
            "fold":    fold + 1,
            "n_train": len(train_samples),
            "n_test":  len(test_samples),
            "r2":      round(r2, 4),
            "rmse_pct": round(abs(rmse_pct), 2),
        })

    return fold_results


def walk_forward_cv_monthly(
    samples_by_ym: dict,
    feature_names: list,
    min_train_months: int = 18,
    step_months: int = 3,
    return_residuals: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    """月次ウォークフォワードCV（FUTURE_TASKS.md 仕様）。
    samples_by_ym: {"YYYY-MM": [(feature_row: list[float], target: float), ...]}
    学習: index < i の全月、テスト: index = i の1ヶ月、step_months ずつスライド。
    y正規化: zscore（対数リターンは無次元のため log 変換不要）。
    ルックアヘッドバイアスなし: テスト月データは学習に使わない。
    Returns: [{test_ym, n_train, n_test, r2, rmse}, ...]
    return_residuals=True のとき (fold_results, residuals_by_ym) を返す。
      residuals_by_ym = {test_ym: [(yhat, y_true), ...]}（テストサンプル順）。
      M-1 の R3（セクター×サイズ別バケットの CV-RMSE）のように、個別残差を
      外部メタデータでグループ集計したい用途向け。テストサンプルの並び順は
      samples_by_ym[test_ym] と一致するため、呼び出し側で同順のメタ列と突合できる。
    """
    all_yms = sorted(samples_by_ym.keys())
    if len(all_yms) < min_train_months + 1:
        return []

    n_feat = len(feature_names)

    fold_results = []
    residuals_by_ym: dict[str, list[tuple[float, float]]] = {}
    for i in range(min_train_months, len(all_yms), step_months):
        test_ym = all_yms[i]
        train_yms = all_yms[:i]

        train_samples: list[tuple] = []
        for ym in train_yms:
            train_samples.extend(samples_by_ym[ym])
        test_samples = samples_by_ym.get(test_ym, [])

        if len(train_samples) < 5 or not test_samples:
            continue

        fold_res = _fit_predict_fold(train_samples, test_samples, n_feat)
        if fold_res is None:
            continue
        yhat_orig, y_test_orig = fold_res

        ymean = statistics.mean(y_test_orig)
        sst = sum((v - ymean) ** 2 for v in y_test_orig)
        sse = sum((yhat_orig[i2] - v) ** 2 for i2, v in enumerate(y_test_orig))
        r2 = 1 - sse / sst if sst > 0 else 0.0
        rmse = math.sqrt(sse / len(y_test_orig))

        fold_results.append({
            "test_ym": test_ym,
            "n_train": len(train_samples),
            "n_test":  len(test_samples),
            "r2":      round(r2, 4),
            "rmse":    round(rmse, 4),
        })
        if return_residuals:
            residuals_by_ym[test_ym] = list(zip(yhat_orig, y_test_orig))

    if return_residuals:
        return fold_results, residuals_by_ym
    return fold_results


# ── M-1 Phase A: マクロ特徴量・モメンタム ──────────────────────────────────────

# feature_name → (series_code, transform: "yoy" | "zscore")
# series_code は collector_prices.py の MACRO_SERIES["code"] と一致させる
_MACRO_FEATURE_MAP: dict[str, tuple[str, str]] = {
    "macro_usdjpy_yoy":   ("USDJPY", "yoy"),
    "macro_sp500_yoy":    ("SP500",  "yoy"),
    "macro_us10y_zscore": ("US10Y",  "zscore"),
    "macro_jp10y_zscore": ("JP10Y",  "zscore"),
}


def get_macro_features(
    db,
    ref_date: str,
    feature_names: list[str],
    window_days: int = 30,
    zscore_years: int = 5,
) -> dict[str, float | None]:
    """MacroData テーブルから ref_date 時点のマクロ特徴量 dict を返す。

    yoy  : (直近 window_days 平均 − 1年前 window_days 平均) / 1年前平均
    zscore: (直近 window_days 平均 − zscore_years 全体平均) / 全体標準偏差
    DB 未蓄積（データなし/不足）は None を返す。呼び出し側でサンプルを除外すること。
    """
    from datetime import date as _date, timedelta as _td
    from database import MacroData  # lazy import to avoid circular dependency

    ref = _date.fromisoformat(ref_date)
    result: dict[str, float | None] = {}

    # 必要な series_code を集約し、1 クエリで取得（N+1 回避）
    series_needed: dict[str, str] = {}  # series_code → transform
    valid_fnames: list[str] = []
    for fname in feature_names:
        if fname not in _MACRO_FEATURE_MAP:
            result[fname] = None
            continue
        scode, ttype = _MACRO_FEATURE_MAP[fname]
        series_needed[scode] = ttype
        valid_fnames.append(fname)

    if not series_needed:
        return result

    has_zscore = any(t == "zscore" for t in series_needed.values())
    lookback_days = zscore_years * 366 if has_zscore else 365 + window_days + 30
    since = (ref - _td(days=lookback_days)).isoformat()

    rows = (
        db.query(MacroData)
        .filter(
            MacroData.series_code.in_(list(series_needed.keys())),
            MacroData.trade_date >= since,
            MacroData.trade_date <= ref_date,
            MacroData.close.isnot(None),
        )
        .order_by(MacroData.series_code, MacroData.trade_date)
        .all()
    )

    by_series: dict[str, dict[str, float]] = {}
    for r in rows:
        by_series.setdefault(r.series_code, {})[r.trade_date] = r.close

    win_start = (ref - _td(days=window_days)).isoformat()

    for fname in valid_fnames:
        scode, ttype = _MACRO_FEATURE_MAP[fname]
        date_close = by_series.get(scode, {})
        if not date_close:
            result[fname] = None
            continue

        current_vals = [v for d, v in date_close.items() if d >= win_start]
        if not current_vals:
            result[fname] = None
            continue
        current_avg = statistics.mean(current_vals)

        if ttype == "yoy":
            ref_1y  = ref - _td(days=365)
            p_start = (ref_1y - _td(days=window_days)).isoformat()
            p_end   = (ref_1y + _td(days=window_days)).isoformat()
            prev_vals = [v for d, v in date_close.items() if p_start <= d <= p_end]
            if not prev_vals:
                result[fname] = None
                continue
            prev_avg = statistics.mean(prev_vals)
            result[fname] = (current_avg - prev_avg) / prev_avg if prev_avg else None

        elif ttype == "zscore":
            all_vals = list(date_close.values())
            if len(all_vals) < 20:
                result[fname] = None
                continue
            mu = statistics.mean(all_vals)
            sigma = statistics.stdev(all_vals) if len(all_vals) > 1 else 0.0
            result[fname] = (current_avg - mu) / sigma if sigma else None

    return result


def get_momentum_return(
    price_rows: list,
    ref_date: str,
    long_months: int = 12,
    short_months: int = 1,
) -> float | None:
    """Jegadeesh-Titman 型モメンタムを算出する。

    log(P_{ref - short_months} / P_{ref - long_months}) を返す。
    price_rows: .trade_date (str "YYYY-MM-DD") と .close (float) を持つオブジェクト列。
    リークなし — ref_date より未来の行は無視する。
    """
    from datetime import date as _date, timedelta as _td

    if not price_rows:
        return None

    ref = _date.fromisoformat(ref_date)
    short_cutoff = (ref - _td(days=short_months * 30)).isoformat()
    long_cutoff  = (ref - _td(days=long_months  * 30)).isoformat()

    eligible = [
        (r.trade_date, r.close)
        for r in price_rows
        if r.trade_date <= ref_date and r.close and r.close > 0
    ]
    if not eligible:
        return None

    short_cands = [(d, c) for d, c in eligible if d <= short_cutoff]
    long_cands  = [(d, c) for d, c in eligible if d <= long_cutoff]

    if not short_cands or not long_cands:
        return None

    short_price = short_cands[-1][1]
    long_price  = long_cands[-1][1]

    return math.log(short_price / long_price)
