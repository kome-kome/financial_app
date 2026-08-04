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
# 9〜21 倍）。本番パネル（91,482 サンプル・67ヶ月・78特徴量）の実測 = walk-forward 17 fold で
# 10 件 + 最終学習で 1 件。50000 では**警告 0 件で、指標も所要も変わらない**:
#   rank-IC 0.1663 / short_side_spread 0.070221 / ターンオーバー 0.340073 が完全一致
#   （per-fold rank-IC の最大差 9.7e-5）、最終学習の μ̂・係数はビット一致、
#   所要は CV 288.5→286.7秒・最終学習 36.2→36.9秒。
# 追加反復が安いのは、未収束が起きるのが CV の選ばない極小 α に限られ、そこでは係数がほぼ
# 飽和しているため（最大 fold の実測: α パスは [0.00035, 3.186] で選択 α=0.05）。`eps`
# （α パス下限）を切り上げる案は選択 α・l1_ratio 自体が変わりモデルが別物になるので採らない。
_EN_MAX_ITER = 50000


def make_elasticnet_fit_predict(l1_ratios: tuple = _EN_L1_RATIOS,
                                n_alphas: int = _EN_N_ALPHAS,
                                cv_splits: int = _EN_CV_SPLITS,
                                max_iter: int = _EN_MAX_ITER,
                                diag: dict | None = None) -> Callable:
    """ElasticNet（L1+L2）の fit_predict を返す（Zou & Hastie 2005）。

    M-1(OLS・BIC 特徴選択) と M-2(XGBoost 非線形) の中間に位置づく「頑健な正則化線形」。
    金利カーブ（us5y/10y/30y）や信用スプレッド（ig/hy/baa）のようなグループ共線性に対し、
    L1 単独（LASSO）が group 内から1本だけ恣意的に選ぶのに対し、L2 成分が係数を分け合って
    安定させる。「非線形が本当に効くのか、頑健な正則化線形で足りるのか」の切り分けに使う。

    α・l1_ratio の選択は **学習 fold 内の時系列分割 CV**（`TimeSeriesSplit`）で行う。
    walk_forward_cv_monthly は train_samples を月昇順に連結して渡すため、TimeSeriesSplit は
    「過去で学習→直近で検証」の向きになり、ランダム K-fold のような期間シャッフルによる
    楽観バイアスを避けられる。
    """
    from sklearn.linear_model import ElasticNetCV
    from sklearn.model_selection import TimeSeriesSplit

    def fit_predict(train_samples, test_samples):
        X_tr, y_tr, X_te, y_mu, y_sd, y_te = _prep_matrices(train_samples, test_samples)
        splits = max(2, min(cv_splits, len(X_tr) // 2 - 1))
        model = ElasticNetCV(
            l1_ratio=list(l1_ratios),
            alphas=n_alphas,        # sklearn>=1.7: 整数を渡すと自動生成する α パス本数
            cv=TimeSeriesSplit(n_splits=splits),
            max_iter=max_iter,
            random_state=42,
            selection="cyclic",     # 決定的（random は seed 依存の座標選択順）
        )
        model.fit(X_tr, y_tr)
        yhat = (model.predict(X_te) * y_sd + y_mu).tolist()

        if diag is not None:
            diag["alpha"].append(float(model.alpha_))
            diag["l1_ratio"].append(float(model.l1_ratio_))
            diag["n_nonzero"].append(int(np.count_nonzero(model.coef_)))
            diag["coef"].append([float(c) for c in model.coef_])
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
                res = ridge_regression(X.tolist(), (y - y.mean()).tolist(), cv_folds=3)
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
        res = (ridge_regression(Xn, y_z, cv_folds=3) if estimator == "ridge"
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
    return make_elasticnet_fit_predict(diag=diag, **opts), {}


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
    """M-2 と同一の XGBoost コールバック（基準線・同一 fold で候補と直接比較する）。"""
    from .macro_gbdt import _make_xgb_fit_predict
    xgb_params = {
        "max_depth": 4, "learning_rate": 0.05, "subsample": 0.8,
        "colsample_bytree": 0.8, "min_child_weight": 5, "reg_lambda": 1.0,
        "reg_alpha": 0.0, "n_estimators": 500, "early_stopping_rounds": 40,
        "tree_method": "hist", "objective": "reg:squarederror", "random_state": 42,
        **(opts or {}),
    }
    return _make_xgb_fit_predict(xgb_params, diag["best_iteration"]), {}


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
