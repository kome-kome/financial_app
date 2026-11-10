"""
兄弟モデル候補メニュー（Issue #372・探索枠）

`plugins/utils.py::walk_forward_cv_monthly(fit_predict=...)` の注入点は、**同一 fold・同一
特徴量・同一指標**で任意の学習器を評価できる共有ハーネスになっている（ADR-0003 §3）。本
モジュールはそこへ差し込む候補モデルの `fit_predict` ファクトリを1箇所に集約し、正式兄弟
（M-1/M-2/M-3/M-4/M-5）へ昇格する前の**安価な実証比較**を可能にする。

    候補 fit_predict → walk_forward_cv_monthly(embargo_months=12) → oof_backtest
    → rank-IC / ロングショート spread / hit-rate を M-2 と横並び（scripts/candidate_bakeoff.py）

昇格の判断基準（Issue #372「検証」）: OOF rank-IC が M-2 を**有意に**上回る候補のみを正式兄弟
（Mn）プラグイン化し、ADR に実測根拠を残す（VISION 核心の「並置してどちらが有効か」を低コストで
回す枠）。**2026-07-26 の実測（ADR-0021）では ElasticNet のみがゲートを通過し M-6
（`plugins/macro_enet.py`）へ昇格した**。他候補の結果と確定知見は ADR-0021 の「実測」節を参照。

収録候補（Issue #372 の改善案メニュー・末尾は 2026-07-26 の本番実測 rank-IC）:
  1. elasticnet    ElasticNet（sklearn）: M-1(OLS) と M-2(非線形) の中間。grouped collinearity
                   （us5y/10y/30y・ig/hy_oas）に L2 で頑健な符号付き係数。**0.1713 → M-6 へ昇格**
  2. extratrees    ExtraTrees（sklearn）: バギング非線形＋木の葉分散からの予測区間（R1' 相当）。0.1649
  3. fama_macbeth  Fama-MacBeth 期待リターン: 各月断面回帰の λ_t → 時系列平均 λ̄ で ŷ=Σβ·λ̄
                   （ADR-0008 の `recommend_factor_premia` を予測ヘッドへ転用）。断面OLS −0.0131 /
                   断面Ridge 0.1653（第1段階の正則化が必須と判明）
  4. regime_linear regime-switch 閾値線形: VIX/信用スプレッドでレジーム分割し regime 別係数。0.1627
  5. lightgbm      代替 GBDT（LightGBM・MIT）: XGBoost 採用の実証比較。**任意依存**。0.1474
  6. catboost      代替 GBDT（CatBoost・Apache-2.0）: 同上（ordered boosting）。**任意依存**。0.1523
     （基準線: xgb_m2 = M-2 既定 0.1419 / 素 OLS 0.1554）

  ＋ `wrap_macro_pca`: マクロ列を **fold 内 PCA** で直交少数因子へ圧縮する合成可能ラッパー
     （上記いずれの候補にも被せられる。改善案「マクロPCA圧縮」）。

リーク防止の共通契約（本モジュール全体の不変条件）:
  - 前処理パラメータ（NaN 補完平均・winsorize 境界・正規化統計・PCA 主成分・レジーム閾値・
    λ̄）は**すべて学習 fold のみ**で fit し、テストは同一パラメータで transform する。
  - 目的変数 y はテスト側では一切参照しない（返り値の第2要素は評価用の実測列）。
  - 例外は Fama-MacBeth のテスト期断面標準化のみ（後述・ラベル非参照＝ look-ahead なし）。

任意依存（lightgbm / catboost）は `requirements-optional.txt` に pin し、本番 requirements.txt
には入れない（Render 無料プランのビルド footprint を増やさない）。未導入環境では
`candidate_available()` が False を返し、bakeoff 側が自動スキップする。

参考:
  Zou & Hastie (2005) "Regularization and variable selection via the elastic net" DOI:10.1111/j.1467-9868.2005.00503.x
  Geurts et al. (2006) "Extremely randomized trees" DOI:10.1007/s10994-006-6226-1
  Meinshausen (2006) "Quantile Regression Forests" JMLR 7:983-999
  Fama & MacBeth (1973) "Risk, Return, and Equilibrium: Empirical Tests" DOI:10.1086/260061
  Hamilton (1989) "A New Approach to the Economic Analysis of Nonstationary Time Series" DOI:10.2307/1912559
  Ke et al. (2017) "LightGBM: A Highly Efficient Gradient Boosting Decision Tree" (NIPS)
  Prokhorenkova et al. (2018) "CatBoost: unbiased boosting with categorical features" (NeurIPS)
"""
from __future__ import annotations

import importlib.util
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .utils import fit_feature_columns, normalize, transform_feature_row, winsorize

# ── 共通定数 ──────────────────────────────────────────────────────────────
_VALID_FRAC = 0.2      # GBDT 候補の early_stopping 検証割合（M-2 `_make_xgb_fit_predict` と同一）
_MIN_FIT_N  = 5        # 同上（これ未満なら early_stopping を諦める）
_MACRO_PREFIX = "macro_"


def make_diag() -> dict:
    """候補が fold ごとの診断値を積む収集器（`best_iterations` パターンの一般化）。"""
    return defaultdict(list)


def summarize_diag(diag: dict) -> dict:
    """診断収集器を人が読める要約へ畳む（数値列は平均、系列は最終 fold 値）。"""
    out: dict = {}
    for k, vals in (diag or {}).items():
        if not vals:
            continue
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            finite = [float(v) for v in vals if math.isfinite(float(v))]
            out[k] = round(sum(finite) / len(finite), 6) if finite else None
        else:
            out[k] = vals[-1]      # coef ベクトル・除外特徴リスト等は最終 fold の値
    return out


# ── 共通前処理 ────────────────────────────────────────────────────────────

def _n_feat(train_samples: list) -> int:
    """サンプル行から特徴量数を取る（PCA 等のラッパーで列数が変わるため行から測る）。"""
    return len(train_samples[0][0]) if train_samples else 0


def _prep_matrices(train_samples: list, test_samples: list) -> tuple:
    """学習 fold のみで fit した winsorize/正規化/NaN補完を train・test に適用する。

    `fit_feature_columns`（列平均で NaN 補完 → p1-p99 winsorize → zscore・切片列付き）と
    `transform_feature_row`（同一パラメータで1行変換）を流用する（Issue #372 の実装指示）。
    y は M-2 と同じく p1-p99 winsorize してから zscore（外れリターンへの過適合を抑える）。

    返り値 (X_train, y_train_z, X_test, y_mu, y_sd, y_test_orig)。X は**切片列を除いた**
    (n, F) 配列（sklearn 側の fit_intercept に任せる）。予測は `y*y_sd + y_mu` で元スケールへ戻す。
    """
    n_feat = _n_feat(train_samples)
    X_tr_raw = [s[0] for s in train_samples]
    y_raw = [s[1] for s in train_samples]

    X_tr, win_p, norm_p = fit_feature_columns(X_tr_raw, n_feat)
    X_te = [transform_feature_row(s[0], win_p, norm_p) for s in test_samples]

    y_w, _, _ = winsorize(y_raw)
    y_z, y_mu, y_sd = normalize(y_w, "zscore")

    return (
        np.asarray(X_tr, dtype=float)[:, 1:],
        np.asarray(y_z, dtype=float),
        np.asarray(X_te, dtype=float)[:, 1:],
        y_mu, y_sd,
        [s[1] for s in test_samples],
    )


def _raw_matrices(train_samples: list, test_samples: list) -> tuple:
    """NaN をそのまま保持する生の行列（NaN ネイティブ処理の GBDT 候補用・M-2 と同一契約）。

    返り値 (X_train, y_train_winsorized, X_test, y_test_orig)。X は無変換（木は単調不変）、
    y のみ p1-p99 winsorize する（`_make_xgb_fit_predict` と同じ）。
    """
    X_tr = np.array([s[0] for s in train_samples], dtype=float)
    y_w, _, _ = winsorize([s[1] for s in train_samples])
    X_te = np.array([s[0] for s in test_samples], dtype=float)
    return X_tr, np.array(y_w, dtype=float), X_te, [s[1] for s in test_samples]


def _tail_split(n: int) -> int:
    """時系列末尾 _VALID_FRAC を early_stopping 検証に回すときの学習側件数（0=分割しない）。"""
    n_valid = max(1, int(n * _VALID_FRAC))
    n_fit = n - n_valid
    return n_fit if n_fit >= _MIN_FIT_N else 0


# ── 1. ElasticNet（sklearn・改善案①）────────────────────────────────────────

_EN_L1_RATIOS = (0.1, 0.5, 0.9)
_EN_N_ALPHAS = 20
_EN_CV_SPLITS = 3
# 座標降下の反復上限。**M-6（macro_enet）と M-4 の探索設定はここが単一ソース**（Issue #452）。
# 5000 では α パス末端が収束せず `ConvergenceWarning` が出ていた（duality gap が tolerance の
# 9〜21 倍）。#452（2026-08-04）時点の本番パネル（91,482 サンプル・67ヶ月・78特徴量）の実測 = walk-forward 17 fold で
# 10 件 + 最終学習で 1 件。50000 では**警告 0 件で、指標も所要も変わらない**:
#   rank-IC 0.1663 / short_side_spread 0.070221 / ターンオーバー 0.340073 が完全一致
#   （per-fold rank-IC の最大差 9.7e-5）、最終学習の μ̂・係数はビット一致、
#   所要は CV 288.5→286.7秒・最終学習 36.2→36.9秒。
# 追加反復が安いのは、未収束が起きるのが CV の選ばない極小 α に限られ、そこでは係数がほぼ
# 飽和しているため（最大 fold の実測: α パスは [0.00035, 3.186] で選択 α=0.05）。`eps`
# （α パス下限）を切り上げる案は選択 α・l1_ratio 自体が変わりモデルが別物になるので採らない。
_EN_MAX_ITER = 50000
# α・l1_ratio を選ぶ内側 CV の区切り方（#971）。`rows` は行の番号で区切る今の方式、`months` は
# 月の境界でだけ区切る方式。`rows` は区切りが月の途中に落ち、月内の並び（社の順）だけで最終モデルの
# α が 165 倍動く（#964）。**候補の既定は `rows` のまま**で、M-6 本体は `macro_enet._INNER_CV`（`months_purged`）を
# 明示して渡す。`_EN_INNER_CV_DEFAULT` は「opts に `inner_cv` が無いときに回る区切り方」で、本番の M-6 の
# 区切り方ではない（呼び出し側が opts を省くか・診断値が無いときに何で回ったかを判断するのに使う）。
# `months_purged`（#979）は月の塊で区切ったうえで、学習の最後の月と検証の最初の月の間を空ける方式。目的変数は
# 52 週先リターンなので、隣り合う月のラベル期間はほぼ丸ごと重なる。外側の walk-forward は
# `embargo_months=LABEL_HORIZON_MONTHS`（ADR-0014）で重なりを外しているが、内側には無かった（#968）。
# M-6 本体の既定はこれ（#968・2026-10-17 の `momentum_gate --inner-cv-purge` で採った）。候補の既定は `rows` のまま。
_EN_INNER_CV_PURGED = "months_purged"
_EN_INNER_CV_CHOICES = ("rows", "months", _EN_INNER_CV_PURGED)
_EN_INNER_CV_DEFAULT = "rows"
# 月の境界で区切る方式＝各学習月の行数（`train_groups`）が要る方式。
_EN_INNER_CV_BY_MONTH = ("months", _EN_INNER_CV_PURGED)
# α・l1_ratio を内側 CV で選ぶ**基準**（#1116・ADR-0070 決定 7）。`mse` は `ElasticNetCV` の二乗誤差（今の版）、
# `rank_ic` は内側の検証月ごとの Spearman（予測 × 生のラベル）の平均が最大の組。評価と探索の基準は rank-IC なのに、
# 選ぶ基準だけが二乗誤差になっている——その差を測るための版で、**既定は `mse` のまま**（M-6 本体の基準は
# `macro_enet._INNER_SCORE`。切り替えは事前登録 `docs/prereg/m6-inner-score.md` の計測と判定の後・#1112）。
# `rank_ic` は検証の行を月へ割り戻すので、月で区切る `_EN_INNER_CV_BY_MONTH` でしか使えない。
_EN_INNER_SCORE_DEFAULT = "mse"
_EN_INNER_SCORE_RANK_IC = "rank_ic"
_EN_INNER_SCORE_CHOICES = (_EN_INNER_SCORE_DEFAULT, _EN_INNER_SCORE_RANK_IC)
# α パスの下限の比（`ElasticNetCV` の既定 `eps`）。`rank_ic` も同じ並びを試す＝2つの版で違うのは選ぶ基準だけ。
_EN_ALPHA_EPS = 1e-3
# 期内の Spearman を取る最小の社数（`macro_snapshots._spearman`・M-4 の `rank_ic_grid` と同じ）。
_EN_RANK_IC_MIN_N = 3


def _purge_gap_months() -> int:
    """`months_purged` が空ける月数の上限。書き写さず、外側の embargo と同じ `LABEL_HORIZON_MONTHS` から取る。"""
    from .macro_snapshots import LABEL_HORIZON_MONTHS
    return LABEL_HORIZON_MONTHS


def purged_month_plan(n_months: int, cv_splits: int, gap_months: int) -> tuple[int, int, int]:
    """空白付きの月の塊の (分割数, 検証の塊の月数, 実際に空ける月数) を返す（#979）。

    空白は `gap_months` から始める。数え方は外側の walk-forward の `embargo_months` と同じで、学習の最後の
    月と検証の最初の月の**間に挟まる月の数**（学習に入った月の並びで数える）。分割数は `months` と同じ式
    `max(2, min(cv_splits, 月数 // 2 - 1))`、検証の塊の大きさは**空白を引いた残りの月数**から
    `(月数 − 空白) // (分割数 + 1)` で決める（sklearn の既定 `月数 // (分割数 + 1)` のまま空白を足すと、
    学習 48 か月以下では 12 か月が入らない）。この大きさが 1 以上なら、どの split も学習月が 1 つ以上残る。

    両立しないときは**空白を最優先で保ち**、検証の塊の大きさ（1 か月まで）→ 分割数（最小 2）→ 空白 の順に
    縮める。空白は両立する最大の値まで縮め、最小は 0。0 まで縮んだら `months` と同じ (分割数, 大きさ) を返す。
    """
    splits0 = max(2, min(cv_splits, n_months // 2 - 1))
    for gap in range(max(0, int(gap_months)), 0, -1):
        test_size = (n_months - gap) // (splits0 + 1)
        if test_size >= 1:
            return splits0, test_size, gap
        # 検証の塊を 1 か月に縮めたうえで、学習月が 1 つ以上残るところまで分割数を縮める
        splits = min(splits0, n_months - gap - 1)
        if splits >= 2:
            return splits, 1, gap
    return splits0, n_months // (splits0 + 1), 0


def month_block_splits(train_groups, cv_splits: int,
                       gap_months: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """月の塊で区切る内側 CV の (学習行, 検証行) の列を返す（#971）。

    `train_groups` は月の昇順に連結した学習行の、各月の行数（`walk_forward_cv_monthly` の
    `pass_train_groups=True` と同じ契約）。月の番号の列に `TimeSeriesSplit` をそのまま当てて、
    月の番号を行の番号の範囲へ写す——「過去の月で学習 → 直後の月の塊で検証」という行ベースと
    同じ形のまま、**どの月も学習側と検証側に分かれない**。分割数は行ベースの式
    `max(2, min(cv_splits, 行数 // 2 - 1))` の行数を月数へ置き換えたもの。

    `gap_months`（#979）は学習の最後の月と検証の最初の月の間に空ける月数の上限。既定 0 は今のまま。1 以上なら
    `purged_month_plan` が決めた空白・分割数・検証の塊の大きさで切り、空白が 0 まで縮んだら既定と同じ分割になる。
    """
    from sklearn.model_selection import TimeSeriesSplit

    sizes = [int(g) for g in train_groups]
    if not sizes or any(g <= 0 for g in sizes):
        raise ValueError(f"各月の行数は 1 以上で、1 か月以上が要る（受け取った値: {sizes}）")
    n_months = len(sizes)
    bounds = np.concatenate([[0], np.cumsum(sizes)])
    gap = 0
    if gap_months > 0:
        splits, test_size, gap = purged_month_plan(n_months, cv_splits, gap_months)
    if gap > 0:
        tss = TimeSeriesSplit(n_splits=splits, test_size=test_size, gap=gap)
    else:
        tss = TimeSeriesSplit(n_splits=max(2, min(cv_splits, n_months // 2 - 1)))
    out = []
    for tr_m, te_m in tss.split(np.arange(n_months)):
        out.append((np.arange(bounds[tr_m[0]], bounds[tr_m[-1] + 1]),
                    np.arange(bounds[te_m[0]], bounds[te_m[-1] + 1])))
    return out


def elasticnet_inner_cv(n_rows: int, cv_splits: int, inner_cv: str = _EN_INNER_CV_DEFAULT,
                        train_groups=None):
    """`ElasticNetCV(cv=...)` へ渡す内側 CV を返す（#971）。CV の fit_predict と最終モデルが共有する。

    `rows` は修正前と同じ `TimeSeriesSplit(n_splits=max(2, min(cv_splits, n_rows // 2 - 1)))`。
    `months` は `month_block_splits` の分割で、各月の行数 `train_groups`（合計 = n_rows）が要る。
    `months_purged`（#979）は `months` に `LABEL_HORIZON_MONTHS` か月を上限とする空白を足したもの。
    """
    from sklearn.model_selection import TimeSeriesSplit

    if inner_cv == "rows":
        return TimeSeriesSplit(n_splits=max(2, min(cv_splits, n_rows // 2 - 1)))
    if inner_cv in _EN_INNER_CV_BY_MONTH:
        if train_groups is None:
            raise ValueError(f"inner_cv='{inner_cv}' には各学習月の行数（train_groups）が要る")
        if sum(int(g) for g in train_groups) != n_rows:
            raise ValueError(f"train_groups の合計 {sum(int(g) for g in train_groups)} が"
                             f"学習行数 {n_rows} と合わない")
        if inner_cv == _EN_INNER_CV_PURGED:
            return month_block_splits(train_groups, cv_splits, gap_months=_purge_gap_months())
        return month_block_splits(train_groups, cv_splits)
    raise ValueError(f"未知の inner_cv: {inner_cv}（{', '.join(_EN_INNER_CV_CHOICES)} のみ）")


def alpha_path_bounds(model, l1_ratios) -> tuple[float, float]:
    """`ElasticNetCV` が選んだ l1_ratio の α パスの (最小, 最大) を返す（#726）。

    α パスは l1_ratio ごとに別に自動生成される（`alphas_` は l1_ratio が複数なら
    (n_l1_ratio, n_alphas)、1つなら (n_alphas,)）。選ばれた α がこの端に一致したら
    「パスの外にもっと良い α がある」可能性がある——上端は全係数ゼロ（定数予測）、
    下端は `eps` で切った正則化の最弱側。
    """
    path = np.asarray(model.alphas_, dtype=float)
    if path.ndim == 2:
        ratios = [float(r) for r in l1_ratios]
        idx = int(np.argmin([abs(r - float(model.l1_ratio_)) for r in ratios]))
        path = path[idx]
    return float(path.min()), float(path.max())


def enet_alpha_grid(X: np.ndarray, y: np.ndarray, l1_ratio: float,
                    n_alphas: int = _EN_N_ALPHAS, eps: float = _EN_ALPHA_EPS) -> np.ndarray:
    """`ElasticNetCV(fit_intercept=True)` が l1_ratio ごとに作る α パスと同じ並び（降順）を返す（#1116）。

    sklearn は学習データ全体で1回だけ作り、全内側 fold で共有する（非公開の `_alpha_grid`）。式は
    α_max = max_j |Σ_i (x_ij − x̄_j)(y_i − ȳ)| / (n · l1_ratio) から α_max·eps まで等比で n_alphas 点。
    非公開関数には頼らず同じ手順で書き、`ElasticNetCV().alphas_` との一致をテストで縛る。退化
    （α_max が分解能以下）も sklearn と同じく分解能の値で埋める。
    """
    X = np.asarray(X, dtype=float)
    yc = np.asarray(y, dtype=float) - np.average(y, axis=0)
    Xyw = (X.T @ yc - np.average(X, axis=0) * np.sum(yc, axis=0))[:, np.newaxis]
    alpha_max = np.sqrt(np.max(np.abs(np.sum(Xyw ** 2, axis=1)))) / (X.shape[0] * l1_ratio)
    if alpha_max <= np.finfo(np.float64).resolution:
        return np.full(n_alphas, np.finfo(np.float64).resolution)
    return np.geomspace(alpha_max, alpha_max * eps, num=n_alphas)


def month_rank_ics(preds: np.ndarray, labels, month_of_row) -> list[np.ndarray]:
    """月ごとの Spearman（予測の各列 × ラベル）を、月の数だけの (列数,) 配列で返す（#1116）。

    `preds` は (行数, 候補数)。各月で順位（同順位は平均順位・`macro_snapshots._avg_ranks` と同じ）の Pearson を
    取る＝`_spearman` を候補ごとに回したものと同じ値。`_EN_RANK_IC_MIN_N` 社未満の月は返さず、予測が月内で
    すべて同じ値の列（係数が全て 0 等）とラベルが無分散の月は NaN（`_spearman` の None）にする。
    """
    from scipy.stats import rankdata

    preds = np.asarray(preds, dtype=float)
    labels = np.asarray(labels, dtype=float)
    month_of_row = np.asarray(month_of_row)
    out = []
    for m in np.unique(month_of_row):
        rows = np.flatnonzero(month_of_row == m)
        if len(rows) < _EN_RANK_IC_MIN_N:
            continue
        rp = rankdata(preds[rows], axis=0)
        ry = rankdata(labels[rows])
        dp = rp - rp.mean(axis=0)
        dy = ry - ry.mean()
        vp = np.sum(dp ** 2, axis=0)
        vy = float(np.sum(dy ** 2))
        with np.errstate(invalid="ignore", divide="ignore"):
            ic = (dp.T @ dy) / np.sqrt(vp * vy)
        ic[(vp <= 0) | (vy <= 0)] = np.nan
        out.append(ic)
    return out


def _select_by_rank_ic(X: np.ndarray, y: np.ndarray, y_rank, splits, month_of_row, l1_ratios: tuple,
                       n_alphas: int, max_iter: int) -> tuple[float, float, np.ndarray, float | None]:
    """内側の検証月の rank-IC の平均が最大の (α, l1_ratio) を選ぶ（#1116）。

    α の並びは `enet_alpha_grid`（学習全体で l1_ratio ごとに1回・`ElasticNetCV` と同じ）。各 split では学習行を
    中心化して `enet_path` で並び全体の係数を出し（`ElasticNetCV` の `_path_residuals` と同じ手順。切片は月内の
    順位に効かないので足さない）、検証行の予測と**生のラベル** `y_rank` の月ごとの Spearman を取る。点数は全 split の
    検証月を1つに並べた単純平均（NaN の月は除く）。規約は M-4 の `rank_ic_grid` と同じ: 先に来た候補が勝つ
    （`>` で比べる＝α は大きい側・l1_ratio は与えた順）。全候補の点数が出なければ最初の l1_ratio の最大の α
    （係数が全て 0＝定数の予測）にする。返り値は (α, l1_ratio, その l1_ratio の並び, 点数 or None)。
    """
    from sklearn.linear_model import enet_path

    grids = [enet_alpha_grid(X, y, r, n_alphas) for r in l1_ratios]
    ics_by_ratio = [[[] for _ in range(n_alphas)] for _ in l1_ratios]
    for tr, te in splits:
        X_tr, y_tr = X[tr], y[tr]
        X_c = X_tr - X_tr.mean(axis=0)
        y_c = y_tr - y_tr.mean()
        for k, r in enumerate(l1_ratios):
            _alphas, coefs, _gaps = enet_path(X_c, y_c, l1_ratio=r, alphas=grids[k],
                                              max_iter=max_iter, selection="cyclic")
            for ic in month_rank_ics(X[te] @ coefs, np.asarray(y_rank)[te], month_of_row[te]):
                for j in range(n_alphas):
                    if not math.isnan(ic[j]):
                        ics_by_ratio[k][j].append(float(ic[j]))
    best = None
    for k, r in enumerate(l1_ratios):
        for j in range(n_alphas):
            ics = ics_by_ratio[k][j]
            if ics:
                score = sum(ics) / len(ics)
                if best is None or score > best[0]:
                    best = (score, k, j)
    if best is None:
        return float(grids[0][0]), float(l1_ratios[0]), grids[0], None
    score, k, j = best
    return float(grids[k][j]), float(l1_ratios[k]), grids[k], score


def fit_elasticnet(X: np.ndarray, y: np.ndarray, *, l1_ratios: tuple, n_alphas: int, cv_splits: int,
                   max_iter: int, inner_cv: str = _EN_INNER_CV_DEFAULT, train_groups=None,
                   inner_score: str = _EN_INNER_SCORE_DEFAULT, y_rank=None) -> tuple:
    """α・l1_ratio を内側 CV で選んで学習した `(model, chosen)` を返す（#1116）。CV の fit_predict と最終モデルが共有する。

    `model` は `predict`・`coef_`・`intercept_` を持つ。`chosen` は `{"alpha", "l1_ratio", "path_min", "path_max"}`
    （path_* は選んだ l1_ratio の α パスの端・#726）で、`rank_ic` のときだけ内側の点数 `inner_rank_ic` を足す。
    `mse`（既定）は修正前と同じ引数で組んだ `ElasticNetCV` そのもの（予測はビット一致）。`rank_ic` は
    `_select_by_rank_ic` で選び、全学習行で `ElasticNet` を学習し直す（`ElasticNetCV` の最後の refit と同じ）。
    `y_rank` は月内の順位を取る生のラベル（winsorize 前）、`train_groups` は各学習月の行数（月の昇順）。
    """
    from sklearn.linear_model import ElasticNet, ElasticNetCV

    if inner_score not in _EN_INNER_SCORE_CHOICES:
        raise ValueError(f"未知の inner_score: {inner_score}（{', '.join(_EN_INNER_SCORE_CHOICES)} のみ）")
    cv = elasticnet_inner_cv(len(X), cv_splits, inner_cv, train_groups)
    if inner_score == _EN_INNER_SCORE_DEFAULT:
        model = ElasticNetCV(
            l1_ratio=list(l1_ratios),
            alphas=n_alphas,        # sklearn>=1.7: 整数を渡すと自動生成する α パス本数
            cv=cv,
            max_iter=max_iter,
            random_state=42,
            selection="cyclic",     # 決定的（random は seed 依存の座標選択順）
        )
        model.fit(X, y)
        lo, hi = alpha_path_bounds(model, l1_ratios)
        return model, {"alpha": float(model.alpha_), "l1_ratio": float(model.l1_ratio_),
                       "path_min": lo, "path_max": hi}
    if inner_cv not in _EN_INNER_CV_BY_MONTH:
        raise ValueError(f"inner_score='{inner_score}' は月で区切る内側 CV（{', '.join(_EN_INNER_CV_BY_MONTH)}）"
                         f"でしか使えない（受け取った inner_cv: {inner_cv}）")
    if y_rank is None or len(y_rank) != len(X):
        raise ValueError(f"inner_score='{inner_score}' には学習行と同じ長さの生のラベル（y_rank）が要る")
    month_of_row = np.repeat(np.arange(len(train_groups)), [int(g) for g in train_groups])
    alpha, l1_ratio, grid, score = _select_by_rank_ic(
        X, y, y_rank, cv, month_of_row, l1_ratios, n_alphas, max_iter)
    model = ElasticNet(alpha=alpha, l1_ratio=l1_ratio, max_iter=max_iter, random_state=42, selection="cyclic")
    model.fit(X, y)
    return model, {"alpha": alpha, "l1_ratio": l1_ratio, "path_min": float(grid.min()),
                   "path_max": float(grid.max()), "inner_rank_ic": score}


def make_elasticnet_fit_predict(l1_ratios: tuple = _EN_L1_RATIOS,
                                n_alphas: int = _EN_N_ALPHAS,
                                cv_splits: int = _EN_CV_SPLITS,
                                max_iter: int = _EN_MAX_ITER,
                                diag: dict | None = None,
                                inner_cv: str = _EN_INNER_CV_DEFAULT,
                                inner_score: str = _EN_INNER_SCORE_DEFAULT) -> Callable:
    """ElasticNet（L1+L2）の fit_predict を返す（Zou & Hastie 2005）。

    M-1(OLS・BIC 特徴選択) と M-2(XGBoost 非線形) の中間に位置づく「頑健な正則化線形」。
    金利カーブ（us5y/10y/30y）や信用スプレッド（ig/hy/baa）のようなグループ共線性に対し、
    L1 単独（LASSO）が group 内から1本だけ恣意的に選ぶのに対し、L2 成分が係数を分け合って
    安定させる。「非線形が本当に効くのか、頑健な正則化線形で足りるのか」の切り分けに使う。

    α・l1_ratio の選択は **学習 fold 内の時系列分割 CV**（`TimeSeriesSplit`）で行う。
    walk_forward_cv_monthly は train_samples を月昇順に連結して渡すため、TimeSeriesSplit は
    「過去で学習→直近で検証」の向きになり、ランダム K-fold のような期間シャッフルによる
    楽観バイアスを避けられる。

    `inner_cv`（#971）は内側 CV の区切り方（`elasticnet_inner_cv`）。既定 `rows` は行の番号で
    区切る（修正前と同一）。`months` は月の境界でだけ区切り、3 番目の引数 `train_groups`（各学習月の
    行数）を使う——`walk_forward_cv_monthly(pass_train_groups=True)` で呼ぶこと（候補メニューの
    `elasticnet` は opts に `inner_cv="months"` があれば `pass_train_groups=True` を返す）。`months` のときだけ診断値に
    `inner_cv` を積む（`rows` では何も足さない＝夜間の診断値の形は変わらない）。
    `months_purged`（#979）も `train_groups` を使い、診断値には `inner_cv` に加えて、その fold で実際に空けた月数
    `inner_cv_gap` を積む（学習月の少ない fold では縮む・`purged_month_plan`）。`months` では積まない。
    `inner_score`（#1116）は α・l1_ratio を選ぶ基準（`fit_elasticnet`）。既定 `mse` は修正前と同一で、`rank_ic`
    のときだけ診断値に `inner_score` と選んだ組の内側の点数 `inner_rank_ic` を積む。`rank_ic` は月で区切る `inner_cv` が要る。
    """
    if inner_cv not in _EN_INNER_CV_CHOICES:
        raise ValueError(f"未知の inner_cv: {inner_cv}（{', '.join(_EN_INNER_CV_CHOICES)} のみ）")
    if inner_score not in _EN_INNER_SCORE_CHOICES:
        raise ValueError(f"未知の inner_score: {inner_score}（{', '.join(_EN_INNER_SCORE_CHOICES)} のみ）")
    if inner_score != _EN_INNER_SCORE_DEFAULT and inner_cv not in _EN_INNER_CV_BY_MONTH:
        raise ValueError(f"inner_score='{inner_score}' は月で区切る内側 CV（{', '.join(_EN_INNER_CV_BY_MONTH)}）"
                         f"でしか使えない（受け取った inner_cv: {inner_cv}）")

    def fit_predict(train_samples, test_samples, train_groups=None):
        X_tr, y_tr, X_te, y_mu, y_sd, y_te = _prep_matrices(train_samples, test_samples)
        model, chosen = fit_elasticnet(
            X_tr, y_tr, l1_ratios=l1_ratios, n_alphas=n_alphas, cv_splits=cv_splits, max_iter=max_iter,
            inner_cv=inner_cv, train_groups=train_groups, inner_score=inner_score,
            y_rank=[s[1] for s in train_samples])
        yhat = (model.predict(X_te) * y_sd + y_mu).tolist()

        if diag is not None:
            diag["alpha"].append(chosen["alpha"])
            diag["l1_ratio"].append(chosen["l1_ratio"])
            diag["n_nonzero"].append(int(np.count_nonzero(model.coef_)))
            diag["coef"].append([float(c) for c in model.coef_])
            # 0/1 で積む＝`summarize_diag` の平均が「α パスの端に張り付いた fold の割合」になる（#726）
            diag["alpha_at_path_min"].append(int(math.isclose(chosen["alpha"], chosen["path_min"], rel_tol=1e-9)))
            diag["alpha_at_path_max"].append(int(math.isclose(chosen["alpha"], chosen["path_max"], rel_tol=1e-9)))
            if inner_cv != _EN_INNER_CV_DEFAULT:
                # 切り替えが CV まで届いたかを呼び出し側が確かめる印（`momentum_gate --inner-cv`）
                diag["inner_cv"].append(inner_cv)
            if inner_cv == _EN_INNER_CV_PURGED:
                diag["inner_cv_gap"].append(
                    purged_month_plan(len(train_groups), cv_splits, _purge_gap_months())[2])
            if inner_score != _EN_INNER_SCORE_DEFAULT:
                # 選ぶ基準の切り替えが CV まで届いたかの印（`momentum_gate --inner-cv-score`・#1116）
                diag["inner_score"].append(inner_score)
                if chosen["inner_rank_ic"] is not None:
                    diag["inner_rank_ic"].append(chosen["inner_rank_ic"])
        return yhat, y_te

    return fit_predict


# ── 1b. 売り専用の候補 S1（下位に入る確率・#1152）────────────────────────────────
# 当てる答えを「52週先リターンが、その月の学習行の下位 1/SELL_BOTTOM_QUANTILES に入るか」に替えた L1+L2 の
# ロジスティック回帰。特徴量・前処理（`_prep_matrices`）・内側 CV の区切り方（`elasticnet_inner_cv`）・l1_ratio は
# M-6 と同じものを渡す＝違いは「当てる答え」と学習器だけ。予測は −P(下位)（高いほど売る理由が小さい＝μ̂ と同じ向き）。
# 下位の切り方は `oof_backtest` の最下位分位と同じ（月内の昇順の先頭 m // n 行）。候補メニュー（`_CANDIDATES`）には
# 載せない（回帰の bakeoff の土俵ではない）。使い手は `scripts/sell_mu_source_bakeoff --sell-candidates`。
SELL_BOTTOM_QUANTILES = 5
# 正則化の強さ C の候補数（`LogisticRegressionCV` の既定・1e-4〜1e4 の対数等間隔）。
_LR_N_CS = 10
# saga の反復（エポック）の上限。合成 13 万行×72 列で 1 fold 約 45 秒（2026-11-09）。
_LR_MAX_ITER = 1000
# 内側 CV で C・l1_ratio を選ぶ基準。二乗誤差の ElasticNetCV に対応する、確率の対数損失。
_LR_SCORING = "neg_log_loss"


def bottom_labels(y, train_groups, n_quantiles: int = SELL_BOTTOM_QUANTILES) -> np.ndarray:
    """月ごとに、y の昇順の先頭 m // n_quantiles 行を 1、残りを 0 にした配列（#1152）。

    `train_groups` は月の昇順に連結した各月の行数（`walk_forward_cv_monthly(pass_train_groups=True)`）。
    同じ値は元の並びの順に先頭から取る（安定ソート）。m // n_quantiles が 0 の月は全て 0。
    """
    y = np.asarray(y, dtype=float)
    sizes = [int(g) for g in train_groups]
    if sum(sizes) != len(y):
        raise ValueError(f"train_groups の合計 {sum(sizes)} が行数 {len(y)} と合わない")
    out = np.zeros(len(y), dtype=int)
    start = 0
    for m in sizes:
        k = m // n_quantiles
        if k > 0:
            order = np.argsort(y[start:start + m], kind="stable")
            out[start + order[:k]] = 1
        start += m
    return out


def make_logistic_bottom_fit_predict(l1_ratios: tuple = _EN_L1_RATIOS,
                                     n_cs: int = _LR_N_CS,
                                     cv_splits: int = _EN_CV_SPLITS,
                                     max_iter: int = _LR_MAX_ITER,
                                     diag: dict | None = None,
                                     inner_cv: str = _EN_INNER_CV_PURGED,
                                     n_quantiles: int = SELL_BOTTOM_QUANTILES) -> Callable:
    """売り専用の候補 S1 の fit_predict（#1152）。返す予測は −P(その月の下位に入る)。

    月ごとの下位のラベルを作るので、各学習月の行数 `train_groups` が要る——
    `walk_forward_cv_monthly(pass_train_groups=True)` で呼ぶこと。C・l1_ratio は M-6 と同じ区切り方の内側 CV
    （`elasticnet_inner_cv`）で、対数損失が最小の組を選ぶ（`LogisticRegressionCV`・saga・決定的な seed）。
    診断値には C・l1_ratio・非ゼロ係数の数・収束しなかった警告の数を積む。
    """
    if inner_cv not in _EN_INNER_CV_CHOICES:
        raise ValueError(f"未知の inner_cv: {inner_cv}（{', '.join(_EN_INNER_CV_CHOICES)} のみ）")

    def fit_predict(train_samples, test_samples, train_groups=None):
        import warnings

        from sklearn.exceptions import ConvergenceWarning
        from sklearn.linear_model import LogisticRegressionCV

        if train_groups is None:
            raise ValueError("S1 は月ごとの下位のラベルを作るので train_groups が要る（pass_train_groups=True で呼ぶ）")
        X_tr, _y_z, X_te, _mu, _sd, y_te = _prep_matrices(train_samples, test_samples)
        labels = bottom_labels([s[1] for s in train_samples], train_groups, n_quantiles)
        cv = elasticnet_inner_cv(len(X_tr), cv_splits, inner_cv, train_groups)
        model = LogisticRegressionCV(
            Cs=n_cs, l1_ratios=tuple(l1_ratios), solver="saga", cv=cv, scoring=_LR_SCORING,
            max_iter=max_iter, random_state=42, use_legacy_attributes=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ConvergenceWarning)
            model.fit(X_tr, labels)
        n_conv = sum(1 for w in caught if issubclass(w.category, ConvergenceWarning))
        pos = list(model.classes_).index(1)
        yhat = (-model.predict_proba(X_te)[:, pos]).tolist()
        if diag is not None:
            diag["C"].append(float(model.C_))
            diag["l1_ratio"].append(float(model.l1_ratio_))
            diag["n_nonzero"].append(int(np.count_nonzero(model.coef_)))
            diag["n_convergence_warnings"].append(n_conv)
        return yhat, y_te

    return fit_predict


# ── 1c. 売り専用の候補 S2（下側の分位回帰・#1158）──────────────────────────────────
# 当てる答えを「52週先リターンの下側 SELL_QUANTILE_TAU 点」に替えた、L1+L2 の線形分位回帰。特徴量・前処理
# （`_prep_matrices`＝y も M-6 と同じく winsorize → zscore）・内側 CV の区切り方・l1_ratio・α の点数と比は M-6 と同じ
# ものを渡す＝違いは「当てる答え（損失）」だけ。予測は分位の予測値そのもの（高いほど売る理由が小さい＝μ̂ と同じ向き）。
# sklearn の `QuantileRegressor`（線形計画）は行数の 2 乗に近く伸び、本番の大きさ（約 20 万行）では日中枠に
# 収まらない（#1152）。そこでピンボール損失の角を幅 h の二次関数で丸め（Huber 型）、FISTA（Beck & Teboulle 2009）で
# 解く。反復に上限を置くので、所要は「反復回数 × 行列とベクトルの積」で決まり、データの性質に左右されない
# （S1 は saga の収束待ちで、間引きからの見積りの約 41 倍かかった・#1152）。候補メニュー（`_CANDIDATES`）には
# 載せない。使い手は `scripts/sell_mu_source_bakeoff --sell-candidates --sell-set s2`。
SELL_QUANTILE_TAU = 0.2
# ピンボール損失を丸める幅（標準化した y の単位）。|残差| < h の範囲だけ二次関数でつなぐ。
_QR_SMOOTH_H = 0.1
# α 1 点あたりの FISTA の反復の上限（α の経路は上端から温めて解く）。上限に当たった回数は診断値に積む。
_QR_MAX_ITER = 300
# 収束の判定: 係数と切片の更新の最大の絶対値 ≤ _QR_TOL × max(1, 係数の最大の絶対値)。
_QR_TOL = 1e-5
# 切片だけの最適値（丸めた損失の勾配が 0 になる点）を二分法で探す回数。
_QR_BISECT = 60


def smoothed_pinball(r: np.ndarray, tau: float = SELL_QUANTILE_TAU,
                     h: float = _QR_SMOOTH_H) -> tuple[np.ndarray, np.ndarray]:
    """丸めたピンボール損失の各行の値と、残差 r についての導関数を返す（#1158）。

    r ≤ −h: (τ−1)r − h/4、r ≥ h: τr − h/4、その間は (τ−1)r + (r+h)²/(4h) − h/4。導関数は τ−1 から τ へ
    傾き 1/(2h) で線形につながる（リプシッツ定数 1/(2h)）。h → 0 で元のピンボール損失に一致する。
    """
    r = np.asarray(r, dtype=float)
    psi = (tau - 1.0) + np.clip((r + h) / (2.0 * h), 0.0, 1.0)
    loss = np.where(r <= -h, (tau - 1.0) * r - h / 4.0,
                    np.where(r >= h, tau * r - h / 4.0,
                             (tau - 1.0) * r + (r + h) ** 2 / (4.0 * h) - h / 4.0))
    return loss, psi


def pinball_loss(r: np.ndarray, tau: float = SELL_QUANTILE_TAU) -> float:
    """丸めないピンボール損失の平均（内側 CV で α・l1_ratio を選ぶ基準・#1158）。"""
    r = np.asarray(r, dtype=float)
    return float(np.mean(np.where(r >= 0, tau * r, (tau - 1.0) * r)))


def _intercept_only(y: np.ndarray, tau: float, h: float) -> float:
    """係数が全て 0 のときの最適な切片（丸めた損失の勾配の平均が 0 になる点）を二分法で返す。"""
    lo, hi = float(np.min(y)) - h, float(np.max(y)) + h
    for _ in range(_QR_BISECT):
        mid = 0.5 * (lo + hi)
        # 平均の ψ(y − q) は q について単調に減る
        if np.mean(smoothed_pinball(y - mid, tau, h)[1]) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def quantile_alpha_grid(X: np.ndarray, y: np.ndarray, l1_ratio: float, tau: float = SELL_QUANTILE_TAU,
                        h: float = _QR_SMOOTH_H, n_alphas: int = _EN_N_ALPHAS,
                        eps: float = _EN_ALPHA_EPS) -> np.ndarray:
    """丸めた分位回帰の α の経路（降順）。作り方は `enet_alpha_grid`（ElasticNetCV）と同じ（#1158）。

    上端は係数が全て 0 のままになる最小の α＝ max_j |Σ_i x_ij ψ_i| / (n · l1_ratio)（ψ は切片だけの最適値での
    損失の導関数）。そこから α_max·eps まで等比で n_alphas 点。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    psi = smoothed_pinball(y - _intercept_only(y, tau, h), tau, h)[1]
    alpha_max = float(np.max(np.abs(X.T @ psi))) / (X.shape[0] * l1_ratio)
    if alpha_max <= np.finfo(np.float64).resolution:
        return np.full(n_alphas, np.finfo(np.float64).resolution)
    return np.geomspace(alpha_max, alpha_max * eps, num=n_alphas)


def _lipschitz(X: np.ndarray, h: float) -> float:
    """平均の丸めた損失の勾配（係数と切片）のリプシッツ定数 σ_max([X, 1])² / (2h·n)。"""
    n = X.shape[0]
    xm = X.sum(axis=0)
    gram = np.empty((X.shape[1] + 1, X.shape[1] + 1))
    gram[:-1, :-1] = X.T @ X
    gram[:-1, -1] = xm
    gram[-1, :-1] = xm
    gram[-1, -1] = n
    return float(np.linalg.eigvalsh(gram)[-1]) / (2.0 * h * n)


def fit_quantile_enet_path(X: np.ndarray, y: np.ndarray, alphas, l1_ratio: float,
                           tau: float = SELL_QUANTILE_TAU, h: float = _QR_SMOOTH_H,
                           max_iter: int = _QR_MAX_ITER, tol: float = _QR_TOL) -> tuple:
    """α の経路（降順）に沿って丸めた L1+L2 分位回帰を FISTA で解く（#1158）。

    目的は mean(ρ_h(y − Xw − b)) + α(l1_ratio·|w|₁ + (1−l1_ratio)/2·|w|²)（罰則の形は sklearn の ElasticNet と
    同じ・切片は罰しない）。各 α は前の α の解から始める（温める）。勾配のステップは 1 / (L + α(1−l1_ratio))、
    L は `_lipschitz`。運動量は勾配の向きで再始動する（O'Donoghue & Candès 2015）。
    返り値は (係数 (n_alphas, p), 切片 (n_alphas,), 反復の上限に当たった α の数)。
    """
    X = np.ascontiguousarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, p = X.shape
    lip = _lipschitz(X, h)
    w = np.zeros(p)
    b = _intercept_only(y, tau, h)
    coefs = np.empty((len(alphas), p))
    intercepts = np.empty(len(alphas))
    n_capped = 0
    for k, alpha in enumerate(alphas):
        l2 = alpha * (1.0 - l1_ratio)
        step = 1.0 / (lip + l2)
        thresh = step * alpha * l1_ratio
        zw, zb, t = w.copy(), b, 1.0
        converged = False
        for _ in range(max_iter):
            psi = smoothed_pinball(y - X @ zw - zb, tau, h)[1]
            gw = -(X.T @ psi) / n + l2 * zw
            gb = -float(psi.mean())
            vw = zw - step * gw
            w_new = np.sign(vw) * np.maximum(np.abs(vw) - thresh, 0.0)
            b_new = zb - step * gb
            dw, db = w_new - w, b_new - b
            # 勾配の向きで再始動: 進んだ向きが運動量と逆なら運動量を捨てる
            if float((zw - w_new) @ dw + (zb - b_new) * db) > 0:
                t = 1.0
            t_new = 0.5 * (1.0 + math.sqrt(1.0 + 4.0 * t * t))
            mom = (t - 1.0) / t_new
            zw, zb = w_new + mom * dw, b_new + mom * db
            w, b, t = w_new, b_new, t_new
            if max(float(np.max(np.abs(dw))) if p else 0.0, abs(db)) <= tol * max(1.0, float(np.max(np.abs(w))) if p else 0.0):
                converged = True
                break
        if not converged:
            n_capped += 1
        coefs[k] = w
        intercepts[k] = b
    return coefs, intercepts, n_capped


def fit_quantile_enet(X: np.ndarray, y: np.ndarray, splits, *, l1_ratios: tuple, tau: float = SELL_QUANTILE_TAU,
                      h: float = _QR_SMOOTH_H, n_alphas: int = _EN_N_ALPHAS, max_iter: int = _QR_MAX_ITER) -> dict:
    """内側 CV で (α, l1_ratio) を選び、全学習行で解き直した結果を返す（#1158）。

    α の経路は l1_ratio ごとに学習全体で 1 回作り（`quantile_alpha_grid`）、全 split で共有する（ElasticNetCV と同じ）。
    点数は検証行の**丸めない**ピンボール損失の平均を split で平均したもの（ElasticNetCV の検証の二乗誤差に当たる）で、
    最小の組を選ぶ。同点は先に来た組（α は大きい側・l1_ratio は与えた順）。選んだ組は経路を上端から選んだ α まで
    温めて解き直す。返り値は {coef, intercept, alpha, l1_ratio, path_min, path_max, n_capped}。
    """
    grids = [quantile_alpha_grid(X, y, r, tau, h, n_alphas) for r in l1_ratios]
    scores = np.zeros((len(l1_ratios), n_alphas))
    n_capped = 0
    for tr, te in splits:
        X_tr, y_tr = np.ascontiguousarray(X[tr]), y[tr]
        X_te, y_te = X[te], y[te]
        for k, r in enumerate(l1_ratios):
            coefs, ints, capped = fit_quantile_enet_path(X_tr, y_tr, grids[k], r, tau, h, max_iter)
            n_capped += capped
            for j in range(n_alphas):
                scores[k, j] += pinball_loss(y_te - X_te @ coefs[j] - ints[j], tau) / len(splits)
    best = None
    for k in range(len(l1_ratios)):
        for j in range(n_alphas):
            if best is None or scores[k, j] < best[0]:
                best = (scores[k, j], k, j)
    _score, k, j = best
    coefs, ints, capped = fit_quantile_enet_path(X, y, grids[k][: j + 1], l1_ratios[k], tau, h, max_iter)
    n_capped += capped
    return {"coef": coefs[-1], "intercept": float(ints[-1]), "alpha": float(grids[k][j]),
            "l1_ratio": float(l1_ratios[k]), "path_min": float(grids[k].min()), "path_max": float(grids[k].max()),
            "n_capped": n_capped}


def make_quantile_enet_fit_predict(l1_ratios: tuple = _EN_L1_RATIOS,
                                   n_alphas: int = _EN_N_ALPHAS,
                                   cv_splits: int = _EN_CV_SPLITS,
                                   max_iter: int = _QR_MAX_ITER,
                                   diag: dict | None = None,
                                   inner_cv: str = _EN_INNER_CV_PURGED,
                                   tau: float = SELL_QUANTILE_TAU) -> Callable:
    """売り専用の候補 S2 の fit_predict（#1158）。返す予測は 52 週先リターンの下側 τ 点の予測値（元の尺度）。

    `_prep_matrices`（y は winsorize → zscore）の上で `fit_quantile_enet` を回し、`y*y_sd + y_mu` で戻す。
    月で区切る内側 CV には各学習月の行数 `train_groups` が要る——`walk_forward_cv_monthly(pass_train_groups=True)`
    で呼ぶこと。診断値には α・l1_ratio・非ゼロ係数の数・α パスの端への張り付き・反復の上限に当たった α の数を積む。
    """
    if inner_cv not in _EN_INNER_CV_CHOICES:
        raise ValueError(f"未知の inner_cv: {inner_cv}（{', '.join(_EN_INNER_CV_CHOICES)} のみ）")

    def fit_predict(train_samples, test_samples, train_groups=None):
        if inner_cv in _EN_INNER_CV_BY_MONTH and train_groups is None:
            raise ValueError(f"S2 の内側 CV（{inner_cv}）は train_groups が要る（pass_train_groups=True で呼ぶ）")
        X_tr, y_tr, X_te, y_mu, y_sd, y_te = _prep_matrices(train_samples, test_samples)
        cv = elasticnet_inner_cv(len(X_tr), cv_splits, inner_cv, train_groups)
        splits = list(cv.split(X_tr)) if hasattr(cv, "split") else list(cv)
        chosen = fit_quantile_enet(X_tr, y_tr, splits, l1_ratios=tuple(l1_ratios), tau=tau,
                                   n_alphas=n_alphas, max_iter=max_iter)
        yhat = ((X_te @ chosen["coef"] + chosen["intercept"]) * y_sd + y_mu).tolist()
        if diag is not None:
            diag["alpha"].append(chosen["alpha"])
            diag["l1_ratio"].append(chosen["l1_ratio"])
            diag["n_nonzero"].append(int(np.count_nonzero(chosen["coef"])))
            diag["alpha_at_path_min"].append(int(math.isclose(chosen["alpha"], chosen["path_min"], rel_tol=1e-9)))
            diag["alpha_at_path_max"].append(int(math.isclose(chosen["alpha"], chosen["path_max"], rel_tol=1e-9)))
            diag["n_iter_capped"].append(chosen["n_capped"])
        return yhat, y_te

    return fit_predict


# ── 2. ExtraTrees / QRF 相当（sklearn・改善案②）──────────────────────────────

_ET_N_ESTIMATORS = 300
_ET_MIN_LEAF = 20
_ET_MAX_FEATURES = 0.5
_ET_INTERVAL_ALPHA = 0.2       # 中央 80% 区間（conformal の tau=0.9 片側と整合する両側水準）
_Z_HALFWIDTH = {0.2: 1.2815515655446004, 0.1: 1.6448536269514722, 0.05: 1.959963984540054}


def make_extratrees_fit_predict(n_estimators: int = _ET_N_ESTIMATORS,
                                min_samples_leaf: int = _ET_MIN_LEAF,
                                max_features: float = _ET_MAX_FEATURES,
                                interval_alpha: float = _ET_INTERVAL_ALPHA,
                                n_jobs: int = -1,
                                diag: dict | None = None) -> Callable:
    """ExtraTrees（極端ランダム木のバギング）の fit_predict を返す（Geurts et al. 2006）。

    ブースティング（M-2）と異なり**バギング**のため、木ごとの予測とその葉に落ちた学習
    サンプルの分散から予測の不確実性を直接読める。区間は2通り算出して `diag` に積む:

      - `interval_leaf`（QRF 相当・既定の R1' 候補）: 全分散の法則
        Var[y|x] ≈ E_t[Var_leaf(t,x)] + Var_t[mean_leaf(t,x)] を葉統計から集計し、
        正規近似で半幅 = z_{1-α/2}·sd。Meinshausen (2006) の QRF が葉の**経験分位**を
        使うのに対し、本実装は葉の1次・2次モーメントのみを `np.bincount` で集計する軽量版
        （O(n·T) で walk-forward の各 fold に耐える。真の QRF は葉ごとの学習ラベル集合を
        テスト行ごとに再集約するため O(n_test·T·leaf) でこの規模では現実的でない）。
      - `interval_tree`（木予測の経験分布・Issue #372 の記述どおり）: 木ごとの予測値の
        α/2〜1-α/2 分位から半幅を取る。これは**アンサンブル平均の epistemic なばらつき**で
        あり残差分布ではないため、被覆率は名目を大きく下回るのが理論的な期待値。

    どちらの被覆率も `interval_coverage_*` として実測し、既存の分割コンフォーマル
    （Issue #365・ADR-0020）と比較できる形で残す。**本番実測の結論（ADR-0021）**: 名目 80% に対し
    `interval_tree` の被覆は 40.6%（半幅 0.139）、`interval_leaf` でも 71.4%（半幅 0.292）にとどまり、
    family-wide の分割コンフォーマル（τ=0.9 に対し実測 87〜89%）に及ばない。R1' の実装としては
    #365 のコンフォーマル区間が正解であり、木固有の区間へ差し替える理由はない。

    NaN 補完は `fit_feature_columns` / `transform_feature_row`（sklearn の木は XGBoost と
    違い NaN をネイティブ処理しないため必須・Issue #372 の実装指示どおり）。
    """
    from sklearn.ensemble import ExtraTreesRegressor

    z = _Z_HALFWIDTH.get(round(interval_alpha, 3), 1.2815515655446004)

    def fit_predict(train_samples, test_samples):
        X_tr, y_tr, X_te, y_mu, y_sd, y_te = _prep_matrices(train_samples, test_samples)
        model = ExtraTreesRegressor(
            n_estimators=n_estimators,
            min_samples_leaf=min_samples_leaf,
            max_features=max_features,
            random_state=42,
            n_jobs=n_jobs,
        )
        model.fit(X_tr, y_tr)
        # 予測は木の順序を固定した逐次平均で作る（`model.predict` は n_jobs 並列で
        # 共有配列へ加算するため合計順序がスレッド完了順に依存し、末尾ビットが実行ごとに
        # 揺れる＝ADR に載せる実測値が再現しなくなる。木単位の predict は安いので逐次で足りる）。
        per_tree = np.stack([est.predict(X_te) for est in model.estimators_])
        yhat = (per_tree.mean(axis=0) * y_sd + y_mu).tolist()

        if diag is not None:
            y_true = np.asarray(y_te, dtype=float)
            err = np.abs(y_true - np.asarray(yhat, dtype=float))

            # (a) 葉モーメント版（QRF 相当）: 全分散の法則で条件付き予測分散を近似
            leaves_tr = model.apply(X_tr)        # (n_train, T)
            leaves_te = model.apply(X_te)        # (n_test,  T)
            var_acc = np.zeros(len(X_te), dtype=float)
            mean_acc = np.zeros(len(X_te), dtype=float)
            mean_sq_acc = np.zeros(len(X_te), dtype=float)
            n_trees = leaves_tr.shape[1]
            for t in range(n_trees):
                lt = leaves_tr[:, t]
                size = int(lt.max()) + 1
                cnt = np.bincount(lt, minlength=size).astype(float)
                s1 = np.bincount(lt, weights=y_tr, minlength=size)
                s2 = np.bincount(lt, weights=y_tr ** 2, minlength=size)
                safe = np.where(cnt > 0, cnt, 1.0)
                leaf_mean = s1 / safe
                leaf_var = np.maximum(s2 / safe - leaf_mean ** 2, 0.0)
                idx = leaves_te[:, t]
                var_acc += leaf_var[idx]
                mean_acc += leaf_mean[idx]
                mean_sq_acc += leaf_mean[idx] ** 2
            e_var = var_acc / n_trees
            var_of_mean = np.maximum(mean_sq_acc / n_trees - (mean_acc / n_trees) ** 2, 0.0)
            hw_leaf = z * np.sqrt(e_var + var_of_mean) * y_sd
            diag["interval_halfwidth_leaf"].append(float(hw_leaf.mean()))
            diag["interval_coverage_leaf"].append(float((err <= hw_leaf).mean()))

            # (b) 木予測の経験分布版（yhat と同じ per_tree を使い回す）
            lo = np.quantile(per_tree, interval_alpha / 2, axis=0)
            hi = np.quantile(per_tree, 1 - interval_alpha / 2, axis=0)
            hw_tree = (hi - lo) / 2 * y_sd
            diag["interval_halfwidth_tree"].append(float(hw_tree.mean()))
            diag["interval_coverage_tree"].append(float((err <= hw_tree).mean()))

            diag["interval_nominal"].append(1.0 - interval_alpha)
            diag["feature_importance"].append([float(v) for v in model.feature_importances_])
        return yhat, y_te

    return fit_predict


# ── 3. Fama-MacBeth 期待リターン（改善案③）──────────────────────────────────

_FM_MIN_COMPANIES = 30         # 断面回帰に必要な最小社数（recommend_factor_premia と同一既定）
_FM_MAXLAGS = 11               # Newey-West ラグ（52週オーバーラップ・同上）
_FM_MIN_VAR_PERIODS = 0.5      # 断面分散を持つ期の割合がこれ未満の列は推定不能として除外
# 断面定数の判定閾値（相対）。同一値が並ぶ列でも平均の丸め誤差で標準偏差が 1e-16 程度に
# なるため、厳密な `sd > 0` では定数列を取りこぼす（実測で確認）。列スケール相対で見る。
_FM_CONST_TOL = 1e-8


def _cross_section_variability(arr: np.ndarray) -> np.ndarray:
    """列ごとの「断面でどれだけ動くか」を NaN 安全・警告なしで返す（sd / max(平均|値|,1)）。

    `np.nanstd` / `np.nanmean` は全 NaN 列で RuntimeWarning を出すため、有効数を数えて
    自前で集計する（全 NaN 列は変動 0＝定数扱い）。同一値が並ぶ列でも平均の丸め誤差で
    sd が 1e-16 程度になるため、列スケールで割った相対量で判定できるようにする。
    """
    mask = np.isfinite(arr)
    cnt = mask.sum(axis=0).astype(float)
    safe = np.where(cnt > 0, cnt, 1.0)
    vals = np.where(mask, arr, 0.0)
    mean = vals.sum(axis=0) / safe
    var = np.where(mask, (arr - mean) ** 2, 0.0).sum(axis=0) / safe
    scale = np.maximum(np.abs(vals).sum(axis=0) / safe, 1.0)
    return np.where(cnt > 0, np.sqrt(var) / scale, 0.0)


def make_fama_macbeth_fit_predict(min_companies: int = _FM_MIN_COMPANIES,
                                  maxlags: int = _FM_MAXLAGS,
                                  estimator: str = "ols",
                                  diag: dict | None = None) -> Callable:
    """Fama-MacBeth 期待リターンヘッドの fit_predict を返す（3引数・pass_train_groups=True）。

    各学習月 t で断面 OLS  r_{i,t+52w} = λ_{0,t} + Σ_k λ_{k,t}·z_{k,i,t} を解き、
    λ̄_k = mean_t(λ_{k,t})（Newey-West HAC 補正付き）を推定 → テスト月の断面標準化特徴に
    掛けて ŷ_i = ȳ + Σ_k λ̄_k·z_{k,i} とする（Fama & MacBeth 1973）。

    **ADR-0008 資産の再利用範囲**（[[feedback_verify_before_trusting_issue_claims]] に従い着手時に
    実コードを確認した結果）:
      - 再利用する: `recommend_factor_premia.fama_macbeth_regression(period_panel, factor_names,
        maxlags)`。{期→(X, y)} を渡すと期別 OLS → λ̄・NW SE・t 値まで返す純関数で、DB にも
        recommend の指標セットにも依存しない。本ヘッドはここへ M-1/M-2 と同一の特徴量パネルを
        渡すだけで済む。
      - 再利用しない: `build_period_panel()`（recommend の7指標に固定・独自 db ロード・
        momentum の Z 化を内包）と `compute_factor_premia()` / `persist()`（DB 依存）。本候補は
        walk_forward_cv_monthly から渡される train_samples を期別に切り直して panel を作る。

    **断面定数列の除外**: マクロ特徴量は同一月の全銘柄で同じ値を取るため、断面回帰では
    切片と完全共線になり λ_k が識別できない（lstsq の最小ノルム解が 0 を返すだけで発散は
    しないが情報を持たない）。学習期のうち断面が実質的に変動する期の割合が
    `_FM_MIN_VAR_PERIODS` 未満の列を落とす（判定は `_cross_section_variability` の相対量。
    同一値が並ぶ列でも平均の丸め誤差で標準偏差が 1e-16 程度になるため厳密な `sd>0` は使えない）。
    結果として本候補は「マクロ非条件付きの characteristics モデル」となり、マクロ条件付けを
    持つ M-1/M-2 との対比が明確になる（除外列は `diag["dropped_idx"]` に記録）。

    **断面標準化**: 各期で `fit_feature_columns`（列平均 NaN 補完 → p1-p99 winsorize → zscore・
    切片列付き）を**その期の断面統計のみ**で適用する。テスト期も自期の断面統計を使うが、
    使うのは説明変数だけでラベルは一切参照しない（= look-ahead なし。同時点の他銘柄の
    特徴量は運用時にも既知）。期ごとに平均 0・分散 1 へ揃えることで λ_t の単位が期間を
    通じて比較可能になり、時系列平均が意味を持つ（Fama-MacBeth の標準手続き）。

    `estimator`:
      - `"ols"`（既定・Fama-MacBeth 1973 に忠実）: 第1段階は `fama_macbeth_regression` へ丸ごと委譲。
      - `"ridge"`: 第1段階だけ `plugins.utils.ridge_regression`（RidgeCV・L2）へ差し替え、第2段階の
        HAC 平均は同じ `average_premia` を共有する。財務特徴量は roe/roa/op_margin/net_margin や
        per/pbr のように強く相関する群を含み、素の断面 OLS は打ち消し合う巨大係数を出す
        （本番実測: λ̄ の最大絶対値 5.37・予測残差の 0.9 分位が対数リターンで 5.56 ＝数値不安定。
        rank-IC は −0.013 と負になった）。Ridge へ替えると λ̄ 最大絶対値 0.027・rank-IC 0.165 へ
        回復する（ADR-0021）。
    """
    if estimator not in ("ols", "ridge"):
        raise ValueError(f"estimator は 'ols' か 'ridge': {estimator}")

    def fit_predict(train_samples, test_samples, train_groups):
        from recommend_factor_premia import average_premia, fama_macbeth_regression

        n_feat = _n_feat(train_samples)
        # ── 学習サンプルを月境界（train_groups）で切り直す ──────────────────
        periods: list[tuple[list, list]] = []
        start = 0
        for g in train_groups:
            seg = train_samples[start:start + g]
            start += g
            if len(seg) >= min_companies:
                periods.append(([s[0] for s in seg], [s[1] for s in seg]))
        if len(periods) < 2:
            return None

        # ── 断面分散を持つ列（推定可能な factor）を学習期から決める ───────────
        var_counts = np.zeros(n_feat, dtype=float)
        for X_rows, _ in periods:
            var_counts += _cross_section_variability(
                np.asarray(X_rows, dtype=float)) > _FM_CONST_TOL
        est_mask = var_counts / len(periods) >= _FM_MIN_VAR_PERIODS
        est_idx = np.flatnonzero(est_mask).tolist()
        if not est_idx:
            return None

        # ── 期別パネル（断面標準化済み・切片列は ols 側で使うため保持）────────
        period_panel: dict[str, tuple] = {}
        y_means: list[float] = []
        for pi, (X_rows, y_rows) in enumerate(periods):
            sub = [[row[j] for j in est_idx] for row in X_rows]
            Xn, _, _ = fit_feature_columns(sub, len(est_idx))
            period_panel[f"p{pi:04d}"] = (np.asarray(Xn, dtype=float)[:, 1:],
                                          np.asarray(y_rows, dtype=float))
            y_means.append(float(np.mean(y_rows)))

        factor_names = [f"f{j}" for j in est_idx]
        if estimator == "ols":
            result = fama_macbeth_regression(period_panel, factor_names, maxlags=maxlags)
        else:
            from .utils import ridge_regression
            betas: dict[str, list[float]] = {f: [] for f in factor_names}
            n_used = 0
            for key in sorted(period_panel):
                X, y = period_panel[key]
                # `ridge_regression` は fit_intercept=False で切片列も**罰則対象**にするため、
                # 切片列を渡すと切片が 0 方向へ縮み、その分を傾きが吸収して λ_t が歪む。
                # 特徴量は断面標準化で平均 0 なので、y を期内平均で中心化すれば切片は
                # 構造的に 0 となり、切片列なしで傾きだけを正しく縮小推定できる。
                res = ridge_regression(X.tolist(), (y - y.mean()).tolist())
                if res is None or len(res["beta"]) != len(factor_names):
                    continue
                for i, f in enumerate(factor_names):
                    betas[f].append(res["beta"][i])
                n_used += 1
            if n_used == 0:
                return None
            result = average_premia(betas, factor_names, n_used, maxlags=maxlags)
        lam = np.array([result.mean_b[f] for f in factor_names], dtype=float)
        y_bar = float(np.mean(y_means))     # 断面標準化下では期別回帰の切片 = その期の平均 y

        # ── テスト期を自期の断面統計で標準化して λ̄ を掛ける ────────────────
        sub_te = [[row[j] for j in est_idx] for row in (s[0] for s in test_samples)]
        Xte, _, _ = fit_feature_columns(sub_te, len(est_idx))
        yhat = (np.asarray(Xte, dtype=float)[:, 1:] @ lam + y_bar).tolist()

        if diag is not None:
            diag["estimator"].append(estimator)
            diag["n_periods_fm"].append(len(periods))
            diag["n_factors"].append(len(est_idx))
            diag["n_dropped"].append(n_feat - len(est_idx))
            diag["max_abs_lambda"].append(float(np.max(np.abs(lam))) if len(lam) else 0.0)
            diag["dropped_idx"].append([j for j in range(n_feat) if j not in set(est_idx)])
            sig = [f for f in factor_names
                   if result.t_stat.get(f) is not None and abs(result.t_stat[f]) >= 2.0]
            diag["n_significant_t2"].append(len(sig))
            diag["lambda_bar"].append([float(v) for v in lam])
        return yhat, [s[1] for s in test_samples]

    return fit_predict


# ── 4. regime-switch 閾値線形（改善案⑤）─────────────────────────────────────

_REGIME_CANDIDATES = ("macro_vix_zscore", "macro_baa_spread_zscore", "macro_hy_oas_zscore")
_REGIME_MIN_N = 200            # regime 別モデルを立てる最小学習件数（未満は pooled へ退避）


def make_regime_linear_fit_predict(feature_names: list[str],
                                   regime_feature: str | None = None,
                                   threshold: float | None = None,
                                   estimator: str = "ridge",
                                   min_regime_n: int = _REGIME_MIN_N,
                                   diag: dict | None = None) -> Callable:
    """レジーム別（ストレス/平穏）に符号付き係数を持つ閾値線形モデルの fit_predict を返す。

    M-2 の木も「VIX が高い時だけ効く特徴量」を暗黙に学習しうるが、本候補は**離散状態 ×
    符号解釈**を明示的に持つ点が差別化（Hamilton 1989 のレジームスイッチを、状態推定では
    なく観測可能なストレス指標の閾値で置き換えた簡易版）。

    レジーム変数はマクロ列（同一月の全銘柄で同値＝実質的に月次の状態変数）から選ぶ:
    `regime_feature` 未指定時は `_REGIME_CANDIDATES` の先頭から `feature_names` に在るものを
    採用する。Issue #372 は `sample_meta` へ regime 列を足す案だったが、**レジームは既に
    特徴行内のマクロ列から一意に決まる**ため build_snapshots の改修は不要（同じ状態変数を
    より安く得る等価な実装）。

    閾値は既定で**学習 fold 内の中央値**（データ駆動・両レジームの標本を確保・リークなし）。
    `threshold` を明示すると固定閾値（例: VIX z=+1.0 をストレス）になる。

    各レジームで独立に `fit_feature_columns`（そのレジーム内統計）→ Ridge/OLS を学習し、
    テスト行は自分のレジーム側の係数で予測する。標本が `min_regime_n` に満たないレジーム、
    およびレジーム変数が NaN の行は pooled（全学習データ）モデルへフォールバックする。
    """
    from .utils import ols, ridge_regression

    ridx = None
    if regime_feature and regime_feature in feature_names:
        ridx = feature_names.index(regime_feature)
    else:
        for cand in _REGIME_CANDIDATES:
            if cand in feature_names:
                ridx = feature_names.index(cand)
                regime_feature = cand
                break

    def _fit(rows: list, y_rows: list, n_feat: int) -> tuple | None:
        Xn, win_p, norm_p = fit_feature_columns(rows, n_feat)
        y_w, _, _ = winsorize(y_rows)
        y_z, y_mu, y_sd = normalize(y_w, "zscore")
        res = (ridge_regression(Xn, y_z) if estimator == "ridge"
               else ols(Xn, y_z))
        if not res:
            return None
        return res["beta"], win_p, norm_p, y_mu, y_sd

    def _predict(model: tuple, feat_row: list) -> float:
        beta, win_p, norm_p, y_mu, y_sd = model
        row = transform_feature_row(feat_row, win_p, norm_p)
        return float(np.dot(row, beta)) * y_sd + y_mu

    def fit_predict(train_samples, test_samples):
        if ridx is None:
            return None
        n_feat = _n_feat(train_samples)
        rvals = np.array([s[0][ridx] for s in train_samples], dtype=float)
        finite = rvals[np.isfinite(rvals)]
        if len(finite) < min_regime_n:
            return None
        thr = float(np.median(finite)) if threshold is None else float(threshold)

        pooled = _fit([s[0] for s in train_samples], [s[1] for s in train_samples], n_feat)
        if pooled is None:
            return None

        stress_mask = np.isfinite(rvals) & (rvals >= thr)
        calm_mask = np.isfinite(rvals) & (rvals < thr)
        models: dict[str, tuple] = {}
        for tag, mask in (("stress", stress_mask), ("calm", calm_mask)):
            idxs = np.flatnonzero(mask).tolist()
            if len(idxs) < min_regime_n:
                continue
            m = _fit([train_samples[i][0] for i in idxs],
                     [train_samples[i][1] for i in idxs], n_feat)
            if m is not None:
                models[tag] = m

        yhat: list[float] = []
        n_by_tag = defaultdict(int)
        for s in test_samples:
            v = s[0][ridx]
            tag = "pooled"
            if v == v:      # NaN でない
                tag = "stress" if v >= thr else "calm"
                if tag not in models:
                    tag = "pooled"
            n_by_tag[tag] += 1
            yhat.append(_predict(models.get(tag, pooled), s[0]))

        if diag is not None:
            diag["regime_feature"].append(regime_feature)
            diag["threshold"].append(thr)
            diag["n_train_stress"].append(int(stress_mask.sum()))
            diag["n_train_calm"].append(int(calm_mask.sum()))
            diag["n_test_pooled_fallback"].append(int(n_by_tag["pooled"]))
            for tag in ("stress", "calm"):
                if tag in models:
                    diag[f"coef_{tag}"].append([float(b) for b in models[tag][0]])
        return yhat, [s[1] for s in test_samples]

    return fit_predict


# ── 5/6. 代替 GBDT（LightGBM / CatBoost・改善案⑥・任意依存）───────────────────
# M-2（XGBoost）の既定ハイパーパラメータへ意図的に揃える（実装差だけを比較する）:
#   max_depth=4 / learning_rate=0.05 / subsample=0.8 / colsample=0.8 /
#   min_child_weight=5 / reg_lambda=1.0 / n_estimators<=500 / early_stopping=40
_GBDT_DEFAULTS = {
    "max_depth": 4,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_samples": 5,
    "reg_lambda": 1.0,
    "n_estimators": 500,
    "early_stopping_rounds": 40,
}


def make_lightgbm_fit_predict(params: dict | None = None, diag: dict | None = None) -> Callable:
    """LightGBM（leaf-wise 成長・MIT）の fit_predict を返す（Ke et al. 2017）。

    XGBoost（M-2）との差は木の成長戦略（leaf-wise vs depth-wise）とヒストグラム実装。
    NaN をネイティブ処理するため `_raw_matrices`（無変換 X・winsorize 済み y）を使い、
    M-2 と完全に同じ入力・同じ early_stopping 手順（時系列末尾 20% を検証）で比較する。
    `num_leaves = 2^max_depth - 1` として depth-wise 相当の複雑度に揃える。
    """
    import inspect

    import lightgbm as lgb

    p = {**_GBDT_DEFAULTS, **(params or {})}
    # LightGBM 4.7 で fit(eval_set=...) は非推奨（eval_X / eval_y へ移行）。pin 版へ追随しつつ
    # 旧版でも動くようシグネチャで分岐する。
    _new_eval_api = "eval_X" in inspect.signature(lgb.LGBMRegressor.fit).parameters

    def fit_predict(train_samples, test_samples):
        X_tr, y_tr, X_te, y_te = _raw_matrices(train_samples, test_samples)
        kwargs = dict(
            objective="regression",
            n_estimators=p["n_estimators"],
            learning_rate=p["learning_rate"],
            max_depth=p["max_depth"],
            num_leaves=max(2, 2 ** p["max_depth"] - 1),
            subsample=p["subsample"],
            subsample_freq=1,
            colsample_bytree=p["colsample_bytree"],
            min_child_samples=p["min_child_samples"],
            reg_lambda=p["reg_lambda"],
            random_state=42,
            n_jobs=-1,
            verbose=-1,
            deterministic=True,     # スレッド分割に依存しないヒストグラム構築（再現性）
            force_row_wise=True,    # deterministic=True と併用しないと警告＋非決定な分割
        )
        n_fit = _tail_split(len(X_tr))
        model = lgb.LGBMRegressor(**kwargs)
        if n_fit and p["early_stopping_rounds"]:
            eval_kw = ({"eval_X": X_tr[n_fit:], "eval_y": y_tr[n_fit:]} if _new_eval_api
                       else {"eval_set": [(X_tr[n_fit:], y_tr[n_fit:])]})
            model.fit(
                X_tr[:n_fit], y_tr[:n_fit],
                callbacks=[lgb.early_stopping(p["early_stopping_rounds"], verbose=False),
                           lgb.log_evaluation(0)],
                **eval_kw,
            )
            best = getattr(model, "best_iteration_", None) or p["n_estimators"]
        else:
            model.fit(X_tr, y_tr)
            best = p["n_estimators"]
        if diag is not None:
            diag["best_iteration"].append(int(best))
        return model.predict(X_te).tolist(), y_te

    return fit_predict


def make_catboost_fit_predict(params: dict | None = None, diag: dict | None = None) -> Callable:
    """CatBoost（ordered boosting・対称木・Apache-2.0）の fit_predict を返す（Prokhorenkova 2018）。

    ordered boosting は勾配推定のターゲットリークを構造的に抑える設計で、低 S/N な日本株
    リターン予測では XGBoost/LightGBM より過学習に強い可能性がある（その検証が本候補の目的）。
    NaN ネイティブ処理・M-2 と同一の early_stopping 手順。`allow_writing_files=False` で
    catboost_info/ の副作用ファイルを作らない。
    """
    from catboost import CatBoostRegressor

    p = {**_GBDT_DEFAULTS, **(params or {})}

    def fit_predict(train_samples, test_samples):
        X_tr, y_tr, X_te, y_te = _raw_matrices(train_samples, test_samples)
        kwargs = dict(
            loss_function="RMSE",
            iterations=p["n_estimators"],
            learning_rate=p["learning_rate"],
            depth=p["max_depth"],
            l2_leaf_reg=p["reg_lambda"],
            subsample=p["subsample"],
            bootstrap_type="Bernoulli",
            rsm=p["colsample_bytree"],
            min_data_in_leaf=p["min_child_samples"],
            random_seed=42,
            verbose=False,
            allow_writing_files=False,
        )
        n_fit = _tail_split(len(X_tr))
        model = CatBoostRegressor(**kwargs)
        if n_fit and p["early_stopping_rounds"]:
            model.fit(X_tr[:n_fit], y_tr[:n_fit],
                      eval_set=(X_tr[n_fit:], y_tr[n_fit:]),
                      early_stopping_rounds=p["early_stopping_rounds"], verbose=False)
            best = model.get_best_iteration() or p["n_estimators"]
        else:
            model.fit(X_tr, y_tr, verbose=False)
            best = p["n_estimators"]
        if diag is not None:
            diag["best_iteration"].append(int(best))
        return model.predict(X_te).tolist(), y_te

    return fit_predict


# ── マクロ fold 内 PCA 圧縮ラッパー（改善案④）───────────────────────────────

_PCA_DEFAULT_K = 5


def macro_column_indices(feature_names: list[str]) -> list[int]:
    """`macro_` 接頭辞の列インデックス（PCA 圧縮対象）。"""
    return [i for i, n in enumerate(feature_names) if n.startswith(_MACRO_PREFIX)]


def wrap_macro_pca(inner_fit_predict: Callable, macro_idx: list[int],
                   n_components: int = _PCA_DEFAULT_K,
                   diag: dict | None = None) -> Callable:
    """マクロ列を **fold 内 PCA** で直交少数因子へ圧縮するラッパー（任意の候補に合成可能）。

    40 本超のマクロ系列は相互に強く相関する（金利カーブ・信用スプレッド・株価指数・
    コモディティが各々ブロックを成す）。線形モデルでは多重共線性、木モデルでは分割の
    希釈という形で過学習を招く。主成分で少数の直交因子へ落として両方を緩和する。

    **必ず学習 fold 内で fit する**（平均・分散・主成分ベクトルすべて）。テスト側は同じ
    変換を適用するだけで、テスト期のマクロ値が主成分の向きを決めることはない（リーク防止）。
    欠損は学習列平均で補完してから標準化する（マクロは M-2 経路で NaN を保持しうる）。

    非マクロ列（財務・モメンタム・px_*・セクター列）は無変換で温存し、圧縮後の特徴量は
    `[非マクロ列..., PC1..PCk]` の順に連結する。`*rest`（M-5 の train_groups など3引数
    呼び出し）は素通しするため `pass_train_groups=True` の候補にも被せられる。

    **本番実測の結論（ADR-0021）**: k=5 でマクロ分散の 90.9% を説明しても rank-IC はほぼ不変
    （elasticnet 0.1713→0.1714 / xgb_m2 0.1419→0.1409）で、木モデルではむしろ悪化した
    （lightgbm 0.1474→0.1379）。ターンオーバー・ブレークイーブンも横ばい。**昇格ゲートを
    通らなかったため本番モデル（M-6）には載せていない**が、別の特徴量構成で再検証できるよう
    探索枠には残す。
    """
    from sklearn.decomposition import PCA

    macro_set = set(macro_idx)

    def wrapped(train_samples, test_samples, *rest):
        if not macro_idx:
            return inner_fit_predict(train_samples, test_samples, *rest)
        n_feat = _n_feat(train_samples)
        keep_idx = [i for i in range(n_feat) if i not in macro_set]

        Xtr = np.array([s[0] for s in train_samples], dtype=float)
        Xte = np.array([s[0] for s in test_samples], dtype=float)
        Mtr = Xtr[:, macro_idx]
        Mte = Xte[:, macro_idx]

        # 列平均は有効値のみで算出する（`np.nanmean` は全 NaN 列で RuntimeWarning を出すため
        # 自前集計。全 NaN のマクロ列は平均 0＝定数列となり主成分に寄与しない）。
        finite = np.isfinite(Mtr)
        cnt = finite.sum(axis=0).astype(float)
        col_mean = np.where(finite, Mtr, 0.0).sum(axis=0) / np.where(cnt > 0, cnt, 1.0)
        Mtr = np.where(finite, Mtr, col_mean)
        Mte = np.where(np.isfinite(Mte), Mte, col_mean)
        sd = Mtr.std(axis=0)
        sd = np.where(sd > 0, sd, 1.0)
        Mtr_s = (Mtr - col_mean) / sd
        Mte_s = (Mte - col_mean) / sd

        k = max(1, min(n_components, Mtr_s.shape[1], Mtr_s.shape[0]))
        pca = PCA(n_components=k, random_state=42)
        Ztr = pca.fit_transform(Mtr_s)
        Zte = pca.transform(Mte_s)

        if diag is not None:
            diag["pca_k"].append(k)
            diag["pca_explained"].append(float(pca.explained_variance_ratio_.sum()))

        # 行ごとの fancy indexing は 7 万行規模で重いため、行列単位で連結してから list 化する。
        rows_tr = np.hstack([Xtr[:, keep_idx], Ztr]).tolist()
        rows_te = np.hstack([Xte[:, keep_idx], Zte]).tolist()
        new_train = [(rows_tr[i], s[1]) for i, s in enumerate(train_samples)]
        new_test = [(rows_te[i], s[1]) for i, s in enumerate(test_samples)]
        return inner_fit_predict(new_train, new_test, *rest)

    return wrapped


def pca_feature_names(feature_names: list[str], n_components: int) -> list[str]:
    """`wrap_macro_pca` 適用後の特徴量名（非マクロ列 + macro_pc1..k）。"""
    macro_set = set(macro_column_indices(feature_names))
    kept = [n for i, n in enumerate(feature_names) if i not in macro_set]
    k = min(n_components, len(macro_set)) if macro_set else 0
    return kept + [f"macro_pc{i + 1}" for i in range(k)]


# ── 候補レジストリ ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """候補1件の定義。`build` は (feature_names, opts, diag) → (fit_predict, wf_extra)。"""
    name: str
    label: str
    build: Callable[[list, dict, dict], tuple]
    requires: tuple[str, ...] = ()     # 追加パッケージ（未導入なら候補をスキップ）
    note: str = ""


def _b_elasticnet(feature_names, opts, diag):
    # 月で区切る内側 CV（#971・空白付きは #979）だけ各学習月の行数が要る。既定（rows）は従来どおり 2 引数で呼ぶ。
    extra = {"pass_train_groups": True} if opts.get("inner_cv") in _EN_INNER_CV_BY_MONTH else {}
    return make_elasticnet_fit_predict(diag=diag, **opts), extra


def _b_extratrees(feature_names, opts, diag):
    return make_extratrees_fit_predict(diag=diag, **opts), {}


def _b_fama_macbeth(feature_names, opts, diag):
    return make_fama_macbeth_fit_predict(diag=diag, **opts), {"pass_train_groups": True}


def _b_fama_macbeth_ridge(feature_names, opts, diag):
    return (make_fama_macbeth_fit_predict(diag=diag, **{"estimator": "ridge", **opts}),
            {"pass_train_groups": True})


def _b_regime_linear(feature_names, opts, diag):
    return make_regime_linear_fit_predict(feature_names, diag=diag, **opts), {}


def _b_lightgbm(feature_names, opts, diag):
    return make_lightgbm_fit_predict(params=opts or None, diag=diag), {}


def _b_catboost(feature_names, opts, diag):
    return make_catboost_fit_predict(params=opts or None, diag=diag), {}


def _b_xgb_baseline(feature_names, opts, diag):
    """M-2 と同一の XGBoost コールバック（基準線・同一 fold で候補と直接比較する）。

    opts の `valid_split`（#976）は early stopping の検証集合の取り方で、XGBoost の設定へ混ぜずに取り出して
    渡す（混ぜると `XGBRegressor` に未知の引数として届く）。`months` のときだけ各学習月の行数が要るので
    `pass_train_groups=True` を返し、回った fold ごとに診断値 `valid_split` へ印を積む。
    `subsample`（#989）は XGBoost の設定のまま渡し、`_SUBSAMPLE_DEFAULT` 以外のときだけ回った fold ごとに
    診断値 `subsample` へ値を積む（切り替えが CV まで届いたかを `momentum_gate --subsample` が確かめる）。
    """
    from .macro_gbdt import _SUBSAMPLE_DEFAULT, _VALID_SPLIT_DEFAULT, _make_xgb_fit_predict
    rest = dict(opts or {})
    valid_split = rest.pop("valid_split", _VALID_SPLIT_DEFAULT)
    xgb_params = {
        "max_depth": 4, "learning_rate": 0.05, "subsample": _SUBSAMPLE_DEFAULT,
        "colsample_bytree": 0.8, "min_child_weight": 5, "reg_lambda": 1.0,
        "reg_alpha": 0.0, "n_estimators": 500, "early_stopping_rounds": 40,
        "tree_method": "hist", "objective": "reg:squarederror", "random_state": 42,
        **rest,
    }
    if valid_split == _VALID_SPLIT_DEFAULT:
        fp, extra = _make_xgb_fit_predict(xgb_params, diag["best_iteration"]), {}
    else:
        fp = _make_xgb_fit_predict(xgb_params, diag["best_iteration"],
                                   valid_split=valid_split, split_marks=diag["valid_split"])
        extra = {"pass_train_groups": True}
    subsample = xgb_params["subsample"]
    if subsample == _SUBSAMPLE_DEFAULT:
        return fp, extra

    def marked(*args, **kwargs):
        out = fp(*args, **kwargs)
        diag["subsample"].append(float(subsample))   # fold が回り切ったときだけ積む（例外の fold は捨てられる）
        return out

    return marked, extra


CANDIDATES: dict[str, Candidate] = {
    "elasticnet": Candidate(
        "elasticnet", "ElasticNet（正則化線形・L1+L2）", _b_elasticnet,
        note="M-1(OLS) と M-2(非線形) の中間。grouped collinearity に頑健な符号付き係数。"),
    "extratrees": Candidate(
        "extratrees", "ExtraTrees（バギング非線形＋予測区間）", _b_extratrees,
        note="木の葉モーメントから条件付き予測区間（QRF 相当）を同時に得る。"),
    "fama_macbeth": Candidate(
        "fama_macbeth", "Fama-MacBeth 期待リターン（断面OLS）", _b_fama_macbeth,
        note="期別断面回帰 λ_t → NW 補正付き時系列平均 λ̄。マクロ列は断面定数のため自動除外。"),
    "fama_macbeth_ridge": Candidate(
        "fama_macbeth_ridge", "Fama-MacBeth 期待リターン（断面Ridge）", _b_fama_macbeth_ridge,
        note="第1段階の断面回帰を L2 正則化。相関の強い財務特徴群による係数発散の切り分け。"),
    "regime_linear": Candidate(
        "regime_linear", "regime-switch 閾値線形", _b_regime_linear,
        note="VIX/信用スプレッドの中央値でストレス/平穏に分割し regime 別の符号付き係数。"),
    "lightgbm": Candidate(
        "lightgbm", "LightGBM（代替GBDT・leaf-wise）", _b_lightgbm, requires=("lightgbm",),
        note="M-2 と同一ハイパラでの実装比較。任意依存（requirements-optional.txt）。"),
    "catboost": Candidate(
        "catboost", "CatBoost（代替GBDT・ordered boosting）", _b_catboost, requires=("catboost",),
        note="ordered boosting による勾配リーク抑制の検証。任意依存。"),
    "xgb_m2": Candidate(
        "xgb_m2", "XGBoost（M-2 基準線）", _b_xgb_baseline,
        note="比較の基準線。M-2 既定ハイパラと同一。"),
}


def candidate_available(name: str) -> bool:
    """候補が実行可能か（任意依存パッケージが導入済みか）。"""
    c = CANDIDATES.get(name)
    if c is None:
        return False
    return all(importlib.util.find_spec(pkg) is not None for pkg in c.requires)


def build_candidate(name: str, feature_names: list[str], opts: dict | None = None,
                    pca_components: int = 0) -> tuple:
    """候補名から (fit_predict, walk_forward追加kwargs, diag) を組み立てる。

    `pca_components > 0` のときはマクロ列を fold 内 PCA で圧縮するラッパーを被せる
    （候補側は圧縮後の特徴行を受け取るだけで、候補の実装は一切変わらない）。
    """
    c = CANDIDATES.get(name)
    if c is None:
        raise ValueError(f"未知の候補: {name}（有効: {', '.join(CANDIDATES)}）")
    if not candidate_available(name):
        raise ValueError(
            f"候補 {name} には {', '.join(c.requires)} が必要です。"
            "`pip install -r requirements-optional.txt` で導入してください。")
    if pca_components > 0 and name == "regime_linear":
        # regime_linear はレジーム変数（マクロ列）の**列位置**を feature_names から引くが、
        # PCA ラッパーはマクロ列を主成分へ畳んで列順も変えるため、両者は併用できない
        # （レジーム変数そのものが消える）。誤った列で分割するより明示的に拒否する。
        raise ValueError(
            "regime_linear はマクロ列をレジーム変数として直接参照するため "
            "--pca とは併用できません（PCA がレジーム変数を主成分へ畳んでしまうため）。")
    diag = make_diag()
    fit_predict, wf_extra = c.build(feature_names, dict(opts or {}), diag)
    if pca_components > 0:
        fit_predict = wrap_macro_pca(
            fit_predict, macro_column_indices(feature_names), pca_components, diag)
    return fit_predict, wf_extra, diag
