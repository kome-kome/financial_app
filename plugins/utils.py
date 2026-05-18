"""プラグイン共通ユーティリティ（Pure Python、numpy/scipy不使用）"""
import math
import statistics
from typing import Any

# 予測値の log-space 上限（exp(LOG_PRED_CAP) ≈ 3.3 百万円/株）
LOG_PRED_CAP = 15.0


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


def _normal_cdf(x: float) -> float:
    """標準正規分布の累積分布関数 Φ(x)。Pure Python の math.erf を利用。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _two_sided_pvalue(t: float, df: int) -> float:
    """両側 p 値。df ≥ 30 なら正規近似、それ未満は t 分布の係数で補正。

    完全な t 分布 CDF は無依存実装が複雑なため、有限自由度では Cornish-Fisher 風の
    粗い補正のみを行う（小サンプル時は「参考値」扱いとする想定）。
    """
    if df <= 0:
        return float("nan")
    z = abs(t)
    if df >= 30:
        return 2.0 * (1.0 - _normal_cdf(z))
    # 小自由度: t 分布は正規より裾が厚いため p 値が大きくなる方向に補正
    # 近似式: 正規 p × (1 + (z^2 + 1) / (4 df))（Hill 1970 系統の簡易補正）
    p_norm = 2.0 * (1.0 - _normal_cdf(z))
    correction = 1.0 + (z * z + 1.0) / (4.0 * df)
    return min(1.0, p_norm * correction)


def ols(X: list, y: list) -> dict | None:
    """Pure-Python OLS。beta, yhat, r2, adj_r2, rmse, mae, se, t_stat, p_value を返す。

    se / t_stat / p_value は各係数（切片含む）に対する配列。p_value は df ≥ 30 で
    正規近似、それ未満は簡易補正（小サンプルでは「参考値」扱い）。
    """
    n, p = len(X), len(X[0])

    def dot(a, b):
        return sum(ai * bi for ai, bi in zip(a, b))

    def mat_mul(A, B):
        r, c, k = len(A), len(B[0]), len(B)
        return [[sum(A[i][l] * B[l][j] for l in range(k)) for j in range(c)] for i in range(r)]

    def transpose(M):
        return [[M[i][j] for i in range(len(M))] for j in range(len(M[0]))]

    def mat_inv(M):
        n2 = len(M)
        aug = [row[:] + [1 if i == j else 0 for j in range(n2)] for i, row in enumerate(M)]
        for i in range(n2):
            pivot_row = max(range(i, n2), key=lambda r: abs(aug[r][i]))
            aug[i], aug[pivot_row] = aug[pivot_row], aug[i]
            piv = aug[i][i] or 1e-12
            aug[i] = [v / piv for v in aug[i]]
            for k in range(n2):
                if k != i:
                    f = aug[k][i]
                    aug[k] = [aug[k][j] - f * aug[i][j] for j in range(2 * n2)]
        return [row[n2:] for row in aug]

    Xt = transpose(X)
    XtX = mat_mul(Xt, X)
    try:
        inv = mat_inv(XtX)
    except Exception:
        return None

    Xty = [dot(row, y) for row in Xt]
    beta = [sum(inv[i][j] * Xty[j] for j in range(p)) for i in range(p)]
    yhat = [dot(row, beta) for row in X]
    ymean = statistics.mean(y)
    sst = sum((v - ymean) ** 2 for v in y)
    sse = sum((v - yhat[i]) ** 2 for i, v in enumerate(y))
    r2 = 1 - sse / sst if sst else 0
    adj_r2 = 1 - (1 - r2) * (n - 1) / (n - p - 1) if n > p + 1 else r2
    rmse = math.sqrt(sse / n)
    mae = sum(abs(v - yhat[i]) for i, v in enumerate(y)) / n

    # 標準誤差・t統計量・p値（df = n - p）。
    # df ≤ 0 は推定不能（過剰フィット）として全て NaN を返す。
    df = n - p
    se = [float("nan")] * p
    t_stat = [float("nan")] * p
    p_value = [float("nan")] * p
    if df > 0:
        sigma2 = sse / df
        for i in range(p):
            var_i = sigma2 * inv[i][i]
            if var_i >= 0:
                se[i] = math.sqrt(var_i)
                if se[i] > 0:
                    t_stat[i] = beta[i] / se[i]
                    p_value[i] = _two_sided_pvalue(t_stat[i], df)

    return {
        "beta": beta, "yhat": yhat, "r2": r2, "adj_r2": adj_r2,
        "rmse": rmse, "mae": mae,
        "se": se, "t_stat": t_stat, "p_value": p_value, "df": df,
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
    means = [statistics.mean(c) for c in X_cols]
    sds = [statistics.stdev(c) if len(c) > 1 else 0.0 for c in X_cols]

    # ── Pearson 相関行列 ───────────────────────────────────────────────
    corr = [[1.0 if i == j else 0.0 for j in range(k)] for i in range(k)]
    high_corr_pairs: list[tuple] = []
    for i in range(k):
        for j in range(i + 1, k):
            if sds[i] == 0 or sds[j] == 0:
                corr[i][j] = corr[j][i] = float("nan")
                continue
            cov = sum(
                (X_cols[i][r] - means[i]) * (X_cols[j][r] - means[j])
                for r in range(n)
            ) / (n - 1)
            r = cov / (sds[i] * sds[j])
            r = max(-1.0, min(1.0, r))
            corr[i][j] = corr[j][i] = r
            if abs(r) > corr_threshold:
                high_corr_pairs.append((i, j, round(r, 4)))

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
            continue
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
        y_test_orig = [s[1] for s in test_samples]

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


def walk_forward_cv(records_by_year: dict, feature_names: list,
                    min_train_years: int = 2, n_folds: int = 3,
                    y_norm_method: str = "log") -> list[dict]:
    """
    ウォークフォワード CV。
    records_by_year: {year(int): [(feature_row: list[float], target: float), ...]}
    y_norm_method: 目的変数の正規化方法。市場データには "log" を推奨。
    Returns: [{train_years, test_year, r2, rmse_pct, n_train, n_test}, ...]
    R² と RMSE% は原空間（逆変換後）で計算する。
    """
    years = sorted(records_by_year.keys())
    if len(years) < min_train_years + 1:
        return []

    actual_folds = min(n_folds, len(years) - min_train_years)
    test_years = years[len(years) - actual_folds:]
    n_feat = len(feature_names)

    fold_results = []
    for test_year in test_years:
        train_years = [y for y in years if y < test_year]

        train_samples: list[tuple] = []
        for ty in train_years:
            train_samples.extend(records_by_year[ty])
        test_samples = records_by_year[test_year]

        if len(train_samples) < 5 or not test_samples:
            continue

        X_train_raw = [s[0] for s in train_samples]
        y_train_raw = [s[1] for s in train_samples]

        norm_params: list[tuple[float, float]] = []
        X_train_norm = [[1.0] + [0.0] * n_feat for _ in range(len(X_train_raw))]
        for fi in range(n_feat):
            col = [row[fi] for row in X_train_raw]
            normed, p1, p2 = normalize(col, "zscore")
            norm_params.append((p1, p2))
            for ri, v in enumerate(normed):
                X_train_norm[ri][fi + 1] = v

        y_train, y_mu, y_sd = normalize(y_train_raw, y_norm_method)
        result = ols(X_train_norm, y_train)
        if not result:
            continue
        beta = result["beta"]

        X_test_norm = []
        for s in test_samples:
            row = [1.0]
            for fi, v in enumerate(s[0]):
                p1, p2 = norm_params[fi]
                row.append(normalize_transform(v, p1, p2))
            X_test_norm.append(row)

        yhat_norm = [sum(row[j] * beta[j] for j in range(len(beta))) for row in X_test_norm]

        # 原空間に逆変換（R²・RMSE% を人間が解釈しやすい単位で算出）
        if y_norm_method == "log":
            yhat_orig = [math.exp(min(v * y_sd + y_mu, LOG_PRED_CAP)) for v in yhat_norm]
        else:
            yhat_orig = [v * y_sd + y_mu for v in yhat_norm]
        y_test_orig = [s[1] for s in test_samples]

        ymean = statistics.mean(y_test_orig)
        sst = sum((v - ymean) ** 2 for v in y_test_orig)
        sse = sum((yhat_orig[i] - v) ** 2 for i, v in enumerate(y_test_orig))
        r2 = 1 - sse / sst if sst > 0 else 0.0
        rmse = math.sqrt(sse / len(y_test_orig))
        rmse_pct = rmse / abs(ymean) * 100 if ymean != 0 else 0.0

        fold_results.append({
            "train_years": train_years,
            "test_year":   test_year,
            "r2":          round(r2, 4),
            "rmse_pct":    round(abs(rmse_pct), 2),
            "n_train":     len(train_samples),
            "n_test":      len(test_samples),
        })

    return fold_results


def walk_forward_cv_monthly(
    samples_by_ym: dict,
    feature_names: list,
    min_train_months: int = 18,
    step_months: int = 3,
) -> list[dict]:
    """月次ウォークフォワードCV（FUTURE_TASKS.md 仕様）。
    samples_by_ym: {"YYYY-MM": [(feature_row: list[float], target: float), ...]}
    学習: index < i の全月、テスト: index = i の1ヶ月、step_months ずつスライド。
    y正規化: zscore（対数リターンは無次元のため log 変換不要）。
    ルックアヘッドバイアスなし: テスト月データは学習に使わない。
    Returns: [{test_ym, n_train, n_test, r2, rmse}, ...]
    """
    all_yms = sorted(samples_by_ym.keys())
    if len(all_yms) < min_train_months + 1:
        return []

    n_feat = len(feature_names)

    fold_results = []
    for i in range(min_train_months, len(all_yms), step_months):
        test_ym = all_yms[i]
        train_yms = all_yms[:i]

        train_samples: list[tuple] = []
        for ym in train_yms:
            train_samples.extend(samples_by_ym[ym])
        test_samples = samples_by_ym.get(test_ym, [])

        if len(train_samples) < 5 or not test_samples:
            continue

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

        y_train, y_mu, y_sd = normalize(y_train_raw, "zscore")
        result = ols(X_train_norm, y_train)
        if not result:
            continue
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
        yhat_orig = [v * y_sd + y_mu for v in yhat_norm]
        y_test_orig = [s[1] for s in test_samples]

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

    return fold_results
