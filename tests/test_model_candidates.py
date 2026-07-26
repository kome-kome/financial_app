"""兄弟モデル候補メニュー（Issue #372・plugins/model_candidates.py）のテスト。

DB を使わない合成パネルで、各候補が
  1. walk_forward_cv_monthly の注入契約（(yhat, y_true) をテスト件数分返す）を満たすこと
  2. **テストラベルを一切使わないこと**（＝リークしないこと）
  3. 候補固有の性質（PCA 圧縮幅・レジーム分割・断面定数列の除外 等）
を満たすことを検証する。

リーク検査の方法: 同じ train / 同じテスト特徴量のまま **テストラベルだけ差し替えて**
fit_predict を2回呼び、予測値がビット単位で一致することを確かめる。テストラベルが
予測に1ビットでも影響していれば不一致になるため、実装の中で y_test を覗いていないことの
直接的な証拠になる（[[feedback_verify_edits_landed_on_disk]] と同じ「受領証ではなく
独立検証で確かめる」方針）。
"""
import math

import numpy as np
import pytest

from plugins.model_candidates import (
    CANDIDATES,
    build_candidate,
    candidate_available,
    macro_column_indices,
    make_diag,
    pca_feature_names,
    summarize_diag,
    wrap_macro_pca,
)
from plugins.utils import walk_forward_cv_monthly

FIN_FEATURES = ["per", "pbr", "roe", "de_ratio"]
MACRO_FEATURES = ["macro_vix_zscore", "macro_baa_spread_zscore",
                  "macro_us10y_zscore", "macro_sp500_yoy"]
FEATURE_NAMES = FIN_FEATURES + MACRO_FEATURES


def _panel(n_months: int = 30, n_stocks: int = 40, seed: int = 7,
           with_nan: bool = False) -> dict:
    """合成パネル {ym: [(feat_row, y), ...]}。

    マクロ列は**その月の全銘柄で同値**（実データと同じ性質＝断面回帰では定数列になる）。
    y は財務特徴とマクロ状態の交互作用＋ノイズで作り、線形/非線形いずれの候補でも
    ある程度の信号が拾える構造にする。
    """
    rng = np.random.default_rng(seed)
    out: dict = {}
    for m in range(n_months):
        ym = f"20{20 + m // 12:02d}-{m % 12 + 1:02d}"
        macro = rng.normal(size=len(MACRO_FEATURES))
        rows = []
        for s in range(n_stocks):
            fin = rng.normal(size=len(FIN_FEATURES))
            feat = list(fin) + list(macro)
            if with_nan and s % 7 == 0:
                feat[len(FIN_FEATURES)] = float("nan")      # マクロ欠損（M-2 経路の再現）
            y = float(0.6 * fin[0] - 0.4 * fin[1] + 0.3 * fin[2] * macro[0]
                      + 0.2 * macro[1] + rng.normal(scale=0.5))
            rows.append((feat, y))
        out[ym] = rows
    return out


def _split(panel: dict, n_train_months: int = 20) -> tuple:
    """パネルを (train_samples, test_samples, train_groups) へ平坦化する。"""
    yms = sorted(panel)
    train_samples: list = []
    train_groups: list = []
    for ym in yms[:n_train_months]:
        train_samples.extend(panel[ym])
        train_groups.append(len(panel[ym]))
    test_samples = panel[yms[n_train_months]]
    return train_samples, test_samples, train_groups


def _call(fit_predict, wf_extra, train, test, groups):
    if wf_extra.get("pass_train_groups"):
        return fit_predict(train, test, groups)
    return fit_predict(train, test)


def _perturb_labels(test_samples: list) -> list:
    """テスト行の特徴量はそのままにラベルだけ差し替える（リーク検査用）。"""
    return [(row, y * -3.0 + 1.234) for row, y in test_samples]


def _assert_same(a: list, b: list, msg: str) -> None:
    """予測列の一致判定（許容 1e-12）。

    厳密な == ではなく極小許容にするのは、並列実装（LightGBM/CatBoost のスレッド分割等）で
    末尾ビットが揺れうるため。リークがあればラベルを -3 倍したぶん予測は桁で動くので、
    1e-12 の許容でも検出力は落ちない。
    """
    assert np.allclose(np.asarray(a), np.asarray(b), rtol=0.0, atol=1e-12), msg


ALL_NAMES = list(CANDIDATES)
LOCAL_NAMES = [n for n in ALL_NAMES if candidate_available(n)]


# ── 契約とリーク ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ALL_NAMES)
def test_candidate_contract_and_no_label_leak(name):
    """全候補: 返り値の形が契約どおりで、テストラベルを予測に使っていない。"""
    if not candidate_available(name):
        pytest.skip(f"任意依存が未導入のためスキップ: {name}")
    panel = _panel()
    train, test, groups = _split(panel)

    fp1, extra1, _ = build_candidate(name, FEATURE_NAMES)
    yhat1, ytrue1 = _call(fp1, extra1, train, test, groups)

    assert len(yhat1) == len(test)
    assert len(ytrue1) == len(test)
    assert all(math.isfinite(v) for v in yhat1)
    assert ytrue1 == [y for _, y in test]

    fp2, extra2, _ = build_candidate(name, FEATURE_NAMES)
    yhat2, ytrue2 = _call(fp2, extra2, train, _perturb_labels(test), groups)

    _assert_same(yhat1, yhat2, f"{name}: テストラベルが予測に影響している（リーク）")
    assert ytrue2 != ytrue1   # 差し替えたラベルはそのまま返る（評価用の実測列）


@pytest.mark.parametrize("name", ALL_NAMES)
def test_candidate_is_deterministic(name):
    """同一入力で同一出力（seed 固定・ADR 記録した実測を再現できること）。"""
    if not candidate_available(name):
        pytest.skip(f"任意依存が未導入のためスキップ: {name}")
    panel = _panel()
    train, test, groups = _split(panel)
    fp1, extra1, _ = build_candidate(name, FEATURE_NAMES)
    fp2, extra2, _ = build_candidate(name, FEATURE_NAMES)
    _assert_same(_call(fp1, extra1, train, test, groups)[0],
                 _call(fp2, extra2, train, test, groups)[0],
                 f"{name}: 同一入力で予測が再現しない")


@pytest.mark.parametrize("name", ["elasticnet", "extratrees", "fama_macbeth",
                                  "fama_macbeth_ridge", "regime_linear"])
def test_candidate_tolerates_nan_features(name):
    """マクロ欠損（M-2 経路の NaN 保持）が混ざっても有限の予測を返す。"""
    panel = _panel(with_nan=True)
    train, test, groups = _split(panel)
    fp, extra, _ = build_candidate(name, FEATURE_NAMES)
    yhat, _ = _call(fp, extra, train, test, groups)
    assert all(math.isfinite(v) for v in yhat)


@pytest.mark.parametrize("name", ["elasticnet", "extratrees", "fama_macbeth",
                                  "fama_macbeth_ridge", "regime_linear"])
def test_candidate_has_predictive_signal(name):
    """合成パネルの既知シグナルを拾えている（rank 相関が正）。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, _ = build_candidate(name, FEATURE_NAMES)
    yhat, ytrue = _call(fp, extra, train, test, groups)
    from plugins.macro_snapshots import _spearman
    assert (_spearman(yhat, ytrue) or 0.0) > 0.1


# ── walk_forward_cv_monthly との統合 ──────────────────────────────────────

@pytest.mark.parametrize("name", ["elasticnet", "extratrees", "fama_macbeth",
                                  "fama_macbeth_ridge", "regime_linear"])
def test_walk_forward_integration(name):
    """共有ハーネスへ注入して fold が立ち、残差が test 件数分返る。"""
    panel = _panel(n_months=36, n_stocks=30)
    fit_predict, wf_extra, diag = build_candidate(name, FEATURE_NAMES)
    folds, residuals = walk_forward_cv_monthly(
        panel, FEATURE_NAMES, min_train_months=6, step_months=3,
        return_residuals=True, fit_predict=fit_predict,
        embargo_months=12, **wf_extra,
    )
    assert folds, f"{name}: fold が1つも立たなかった"
    assert len(residuals) == len(folds)
    for ym, pairs in residuals.items():
        assert len(pairs) == len(panel[ym])
    assert summarize_diag(diag)     # 診断が空でない


def test_embargo_shortens_folds_equally():
    """候補注入でも embargo（ADR-0014）が効く＝purge 前後で fold 数が変わる。"""
    panel = _panel(n_months=36, n_stocks=20)
    fp, extra, _ = build_candidate("elasticnet", FEATURE_NAMES)
    honest = walk_forward_cv_monthly(panel, FEATURE_NAMES, min_train_months=6,
                                     step_months=3, fit_predict=fp, embargo_months=12, **extra)
    fp2, extra2, _ = build_candidate("elasticnet", FEATURE_NAMES)
    leaky = walk_forward_cv_monthly(panel, FEATURE_NAMES, min_train_months=6,
                                    step_months=3, fit_predict=fp2, embargo_months=0, **extra2)
    assert len(honest) < len(leaky)


# ── マクロ fold 内 PCA 圧縮ラッパー ────────────────────────────────────────

def test_macro_pca_reduces_width_and_keeps_non_macro():
    """PCA ラッパーは非マクロ列を温存し、マクロ列だけを k 本の主成分へ畳む。"""
    seen: dict = {}

    def spy(train, test):
        seen["train_width"] = len(train[0][0])
        seen["test_width"] = len(test[0][0])
        seen["train_head"] = list(train[0][0][:len(FIN_FEATURES)])
        return [0.0] * len(test), [y for _, y in test]

    panel = _panel()
    train, test, _ = _split(panel)
    wrapped = wrap_macro_pca(spy, macro_column_indices(FEATURE_NAMES), n_components=2)
    wrapped(train, test)

    assert seen["train_width"] == len(FIN_FEATURES) + 2
    assert seen["test_width"] == len(FIN_FEATURES) + 2
    # 非マクロ列は無変換で先頭に残る
    assert seen["train_head"] == pytest.approx(list(train[0][0][:len(FIN_FEATURES)]))
    assert pca_feature_names(FEATURE_NAMES, 2) == FIN_FEATURES + ["macro_pc1", "macro_pc2"]


def test_macro_pca_is_fit_on_train_only():
    """テスト行のマクロ値を変えても学習側の主成分・学習行の変換結果は変わらない。"""
    panel = _panel()
    train, test, _ = _split(panel)
    captured: list = []

    def spy(tr, te):
        captured.append([row for row, _ in tr])
        return [0.0] * len(te), [y for _, y in te]

    macro_idx = macro_column_indices(FEATURE_NAMES)
    wrap_macro_pca(spy, macro_idx, 2)(train, test)
    shifted = [([v + 10.0 if i in set(macro_idx) else v for i, v in enumerate(row)], y)
               for row, y in test]
    wrap_macro_pca(spy, macro_idx, 2)(train, shifted)

    assert np.allclose(np.asarray(captured[0]), np.asarray(captured[1]))


def test_macro_pca_composes_with_candidate():
    """build_candidate(pca_components=k) で候補に合成でき、診断へ寄与率が載る。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate("elasticnet", FEATURE_NAMES, pca_components=3)
    yhat, _ = _call(fp, extra, train, test, groups)
    assert len(yhat) == len(test)
    s = summarize_diag(diag)
    assert s["pca_k"] == 3
    assert 0.0 < s["pca_explained"] <= 1.0


def test_macro_pca_rejected_for_regime_linear():
    """regime_linear はレジーム変数が畳まれるため PCA 併用を明示的に拒否する。"""
    with pytest.raises(ValueError, match="regime_linear"):
        build_candidate("regime_linear", FEATURE_NAMES, pca_components=3)


# ── 候補固有の性質 ────────────────────────────────────────────────────────

def test_fama_macbeth_drops_cross_sectionally_constant_macro():
    """マクロ列（断面定数）は推定不能として自動除外され、財務列だけが factor になる。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate("fama_macbeth", FEATURE_NAMES)
    _call(fp, extra, train, test, groups)
    s = summarize_diag(diag)
    assert s["n_factors"] == len(FIN_FEATURES)
    assert s["n_dropped"] == len(MACRO_FEATURES)
    assert set(s["dropped_idx"]) == set(macro_column_indices(FEATURE_NAMES))


def test_fama_macbeth_needs_month_boundaries():
    """train_groups（月境界）を要求する 3 引数コールバックである。"""
    fp, extra, _ = build_candidate("fama_macbeth", FEATURE_NAMES)
    assert extra == {"pass_train_groups": True}
    panel = _panel()
    train, test, _ = _split(panel)
    with pytest.raises(TypeError):
        fp(train, test)      # 2 引数呼び出しは契約違反


def test_fama_macbeth_ridge_variant_shares_second_stage():
    """Ridge 版は第1段階だけ差し替え、第2段階（HAC 平均）は同じ average_premia を通る。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate("fama_macbeth_ridge", FEATURE_NAMES)
    yhat, _ = _call(fp, extra, train, test, groups)
    s = summarize_diag(diag)
    assert s["estimator"] == "ridge"
    assert s["n_factors"] == len(FIN_FEATURES)
    assert all(math.isfinite(v) for v in yhat)
    # OLS 版より係数が縮む（L2 収縮の定義どおり）
    fp_o, extra_o, diag_o = build_candidate("fama_macbeth", FEATURE_NAMES)
    _call(fp_o, extra_o, train, test, groups)
    assert summarize_diag(diag_o)["estimator"] == "ols"
    assert s["max_abs_lambda"] <= summarize_diag(diag_o)["max_abs_lambda"] + 1e-9


def test_fama_macbeth_rejects_unknown_estimator():
    from plugins.model_candidates import make_fama_macbeth_fit_predict
    with pytest.raises(ValueError, match="estimator"):
        make_fama_macbeth_fit_predict(estimator="lasso")


def test_cross_section_variability_is_nan_safe():
    """全 NaN 列・定数列を警告なしで 0 と判定する（RuntimeWarning を出さない）。"""
    import warnings
    from plugins.model_candidates import _cross_section_variability
    arr = np.array([[1.0, 5.0, np.nan, 2.0],
                    [2.0, 5.0, np.nan, np.nan],
                    [3.0, 5.0, np.nan, 2.0]])
    with warnings.catch_warnings():
        warnings.simplefilter("error")     # 警告が出たら失敗させる
        v = _cross_section_variability(arr)
    assert v[0] > 0          # 変動する列
    assert v[1] < 1e-12      # 定数列
    assert v[2] == 0.0       # 全 NaN 列
    assert v[3] < 1e-12      # NaN 混じりの定数列


def test_regime_linear_splits_at_train_median():
    """閾値は学習 fold 内の中央値＝両レジームがほぼ半々に分かれる（リークなし）。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate("regime_linear", FEATURE_NAMES)
    _call(fp, extra, train, test, groups)
    s = summarize_diag(diag)
    assert s["regime_feature"] == "macro_vix_zscore"
    n_stress, n_calm = s["n_train_stress"], s["n_train_calm"]
    assert n_stress + n_calm == len(train)
    assert abs(n_stress - n_calm) <= len(train) * 0.1
    # レジーム別に別々の係数が立っている
    assert s["coef_stress"] != s["coef_calm"]


def test_regime_linear_explicit_threshold_and_fallback():
    """閾値を極端に振ると片側レジームが最小件数を割り、pooled へフォールバックする。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate(
        "regime_linear", FEATURE_NAMES, opts={"threshold": 99.0})
    yhat, _ = _call(fp, extra, train, test, groups)
    s = summarize_diag(diag)
    assert s["threshold"] == 99.0
    assert s["n_train_stress"] == 0
    assert all(math.isfinite(v) for v in yhat)


def test_regime_linear_returns_none_without_regime_feature():
    """レジーム変数が特徴量に無ければ None を返す（walk_forward が fold をスキップ）。"""
    fp, extra, _ = build_candidate("regime_linear", FIN_FEATURES)
    panel = _panel()
    train, test, _ = _split(panel)
    train = [([r[i] for i in range(len(FIN_FEATURES))], y) for r, y in train]
    test = [([r[i] for i in range(len(FIN_FEATURES))], y) for r, y in test]
    assert fp(train, test) is None


def test_extratrees_reports_interval_coverage():
    """予測区間（葉モーメント版・木予測分布版）の被覆率が診断に載る。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate("extratrees", FEATURE_NAMES)
    _call(fp, extra, train, test, groups)
    s = summarize_diag(diag)
    for key in ("interval_coverage_leaf", "interval_halfwidth_leaf",
                "interval_coverage_tree", "interval_halfwidth_tree"):
        assert key in s
    assert 0.0 <= s["interval_coverage_leaf"] <= 1.0
    # アンサンブル平均のばらつき（tree）は残差分布（leaf）より必ず狭い＝被覆も低い
    assert s["interval_halfwidth_tree"] < s["interval_halfwidth_leaf"]
    assert s["interval_coverage_tree"] <= s["interval_coverage_leaf"]


def test_elasticnet_reports_sparse_signed_coefficients():
    """符号付き係数と非ゼロ本数が診断に載る（解釈性の担保）。"""
    panel = _panel()
    train, test, groups = _split(panel)
    fp, extra, diag = build_candidate("elasticnet", FEATURE_NAMES)
    _call(fp, extra, train, test, groups)
    s = summarize_diag(diag)
    assert s["alpha"] > 0
    assert 0 < s["n_nonzero"] <= len(FEATURE_NAMES)
    coef = s["coef"]
    assert len(coef) == len(FEATURE_NAMES)
    assert coef[0] > 0 and coef[1] < 0      # 合成データの真の符号（+per / -pbr）を再現


# ── レジストリ ────────────────────────────────────────────────────────────

def test_registry_metadata_complete():
    for name, c in CANDIDATES.items():
        assert c.name == name
        assert c.label and c.note
        assert isinstance(c.requires, tuple)


def test_unknown_candidate_rejected():
    with pytest.raises(ValueError, match="未知の候補"):
        build_candidate("no_such_model", FEATURE_NAMES)


def test_optional_dependency_gate(monkeypatch):
    """任意依存が未導入なら候補は available=False になり build_candidate が拒否する。"""
    import plugins.model_candidates as mc
    monkeypatch.setattr(mc.importlib.util, "find_spec", lambda pkg: None)
    assert not mc.candidate_available("lightgbm")
    assert not mc.candidate_available("catboost")
    with pytest.raises(ValueError, match="requirements-optional"):
        mc.build_candidate("lightgbm", FEATURE_NAMES)


def test_summarize_diag_mixes_numeric_and_series():
    d = make_diag()
    d["alpha"].extend([1.0, 3.0])
    d["coef"].append([0.1, 0.2])
    d["coef"].append([0.3, 0.4])
    d["regime_feature"].append("macro_vix_zscore")
    s = summarize_diag(d)
    assert s["alpha"] == 2.0                 # 数値列は平均
    assert s["coef"] == [0.3, 0.4]           # 系列は最終 fold
    assert s["regime_feature"] == "macro_vix_zscore"
