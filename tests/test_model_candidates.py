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
    _EN_CV_SPLITS,
    _EN_L1_RATIOS,
    _EN_MAX_ITER,
    _EN_N_ALPHAS,
    CANDIDATES,
    _prep_matrices,
    alpha_path_bounds,
    build_candidate,
    candidate_available,
    elasticnet_inner_cv,
    macro_column_indices,
    make_diag,
    make_elasticnet_fit_predict,
    month_block_splits,
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


def test_elasticnet_diag_records_alpha_path_edges():
    """fold ごとに「α がパスの端か」を 0/1 で積み、要約では端に張り付いた fold の割合になる（#726）。"""
    diag = make_diag()
    fp = make_elasticnet_fit_predict(diag=diag)
    panel = _panel()
    for n_train in (18, 20, 22):
        train, test, _ = _split(panel, n_train)
        fp(train, test)

    assert len(diag["alpha_at_path_min"]) == 3
    assert set(diag["alpha_at_path_min"]) <= {0, 1}
    assert set(diag["alpha_at_path_max"]) <= {0, 1}
    s = summarize_diag(diag)
    assert 0.0 <= s["alpha_at_path_min"] <= 1.0
    assert 0.0 <= s["alpha_at_path_max"] <= 1.0


@pytest.mark.parametrize("l1_ratio", [[0.1, 0.5, 0.9], 0.5])
def test_alpha_path_bounds_bracket_the_chosen_alpha(l1_ratio):
    """l1_ratio が複数でも1つでも、選ばれた α は返した (最小, 最大) の内側にある。"""
    from sklearn.linear_model import ElasticNetCV
    from sklearn.model_selection import TimeSeriesSplit

    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 5))
    y = X[:, 0] + rng.normal(size=200)
    model = ElasticNetCV(l1_ratio=l1_ratio, alphas=20, cv=TimeSeriesSplit(3),
                         random_state=42).fit(X, y)
    lo, hi = alpha_path_bounds(model, l1_ratio if isinstance(l1_ratio, list) else [l1_ratio])
    assert lo < hi
    assert lo <= model.alpha_ <= hi


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


# ── 内側 CV の区切り方（#971）──────────────────────────────────────────────
#
# M-6 は α・l1_ratio を学習 fold の中の時系列分割 CV で選ぶ。`rows`（本番）は行の番号で区切るので、
# 区切りが月の途中に落ち、月内の並びだけで α が動く（#964）。`months` は月の境界でだけ区切る。

# 月ごとの行数をわざと不揃いにする。揃っていると行の区切りも偶然すべて月の境界に落ち、
# rows と months が同じ分割になって比べる意味が無くなる（空振り）。
_UNEVEN_GROUPS = [7, 3, 9, 5, 8, 4, 6, 10, 3, 7, 5, 9]


def _month_of_row(groups) -> np.ndarray:
    return np.repeat(np.arange(len(groups)), groups)


def _months_cut(splits, groups) -> set:
    """学習側と検証側の両方に行を持つ月（＝月の途中で区切られた月）の番号。"""
    month = _month_of_row(groups)
    cut: set = set()
    for tr, te in splits:
        cut |= set(month[tr].tolist()) & set(month[te].tolist())
    return cut


def _row_splits(groups, cv_splits: int = 3) -> list:
    n = sum(groups)
    return list(elasticnet_inner_cv(n, cv_splits, "rows").split(np.zeros((n, 1))))


def _uneven_panel(sizes, seed: int = 971) -> dict:
    """月ごとの社数が `sizes` の合成パネル（線形の信号＋ノイズ・マクロは月内で同値）。

    `_panel` を切り詰めた検体では、どちらの区切り方でも同じ α が選ばれて差が出なかった
    （区切り方を変えても予測が動かない検体では、rows のビット一致が自明に通ってしまう）。
    """
    rng = np.random.default_rng(seed)
    out: dict = {}
    for m, g in enumerate(sizes):
        ym = f"20{20 + m // 12:02d}-{m % 12 + 1:02d}"
        macro = rng.normal(size=len(MACRO_FEATURES))
        rows = []
        for _ in range(g):
            fin = rng.normal(size=len(FIN_FEATURES))
            y = float(0.6 * fin[0] - 0.3 * fin[1] + 0.3 * macro[1] + rng.normal(scale=0.5))
            rows.append((list(fin) + list(macro), y))
        out[ym] = rows
    return out


def _reference_enet(train_samples, test_samples):
    """修正前（#971 より前）の `make_elasticnet_fit_predict` の手順をそのまま書き写した参照実装。"""
    from sklearn.linear_model import ElasticNetCV
    from sklearn.model_selection import TimeSeriesSplit

    X_tr, y_tr, X_te, y_mu, y_sd, _y_te = _prep_matrices(train_samples, test_samples)
    splits = max(2, min(_EN_CV_SPLITS, len(X_tr) // 2 - 1))
    model = ElasticNetCV(l1_ratio=list(_EN_L1_RATIOS), alphas=_EN_N_ALPHAS,
                         cv=TimeSeriesSplit(n_splits=splits), max_iter=_EN_MAX_ITER,
                         random_state=42, selection="cyclic")
    model.fit(X_tr, y_tr)
    return (model.predict(X_te) * y_sd + y_mu).tolist(), model


def test_month_splits_never_cut_a_month():
    """months: どの分割でも、触れた月は丸ごと学習側か検証側に入る（月が両側に分かれない）。"""
    month = _month_of_row(_UNEVEN_GROUPS)
    splits = month_block_splits(_UNEVEN_GROUPS, cv_splits=3)
    assert len(splits) == 3
    for tr, te in splits:
        for side in (tr, te):
            for m in set(month[side].tolist()):
                assert set(np.flatnonzero(month == m).tolist()) <= set(side.tolist())
        assert tr.max() < te.min()     # 過去の月で学習 → 直後の月で検証（行ベースと同じ向き）
    assert _months_cut(splits, _UNEVEN_GROUPS) == set()
    # 空振り防止: 同じ行数を rows で区切ると月の途中に落ちる（#964 の原因そのもの）
    assert _months_cut(_row_splits(_UNEVEN_GROUPS), _UNEVEN_GROUPS)


def test_month_splits_hold_the_same_rows_when_rows_are_shuffled_within_months():
    """months: 月内の行の並びを入れ替えても、各分割の学習側・検証側に入る行（社）の集合は変わらない。"""
    rng = np.random.default_rng(971)
    ids = [(m, k) for m, g in enumerate(_UNEVEN_GROUPS) for k in range(g)]
    shuffled: list = []
    start = 0
    for g in _UNEVEN_GROUPS:
        block = ids[start:start + g]
        shuffled.extend(block[i] for i in rng.permutation(g))
        start += g
    assert shuffled != ids                                      # 並びは実際に変わっている

    def sides(splits, order):
        return [({order[i] for i in tr}, {order[i] for i in te}) for tr, te in splits]

    months = month_block_splits(_UNEVEN_GROUPS, cv_splits=3)
    assert sides(months, ids) == sides(months, shuffled)
    # 空振り防止: rows では同じ入れ替えで検証側に入る社が変わる
    rows = _row_splits(_UNEVEN_GROUPS)
    assert sides(rows, ids) != sides(rows, shuffled)


@pytest.mark.parametrize("n_months, want", [(6, 2), (7, 2), (8, 3), (40, 3)])
def test_month_split_count_maps_the_row_rule_onto_months(n_months, want):
    """分割数は行ベースの `max(2, min(cv_splits, n // 2 - 1))` の n を月数へ置き換えたもの。"""
    assert len(month_block_splits([5] * n_months, cv_splits=_EN_CV_SPLITS)) == want


def test_inner_cv_rejects_bad_inputs():
    with pytest.raises(ValueError, match="train_groups"):
        elasticnet_inner_cv(10, 3, "months")
    with pytest.raises(ValueError, match="合わない"):
        elasticnet_inner_cv(10, 3, "months", [4, 5])
    with pytest.raises(ValueError, match="未知の inner_cv"):
        elasticnet_inner_cv(10, 3, "weeks")
    with pytest.raises(ValueError, match="未知の inner_cv"):
        make_elasticnet_fit_predict(inner_cv="weeks")


def test_rows_is_bit_identical_to_the_pre_971_procedure():
    """既定（rows）の fit_predict は修正前と同じ予測・係数を返す（2 引数でも 3 引数でも）。"""
    panel = _uneven_panel([40, 17, 33, 25, 38, 21, 29, 15, 36, 23, 31, 19, 27, 40, 22, 34,
                           18, 30, 26, 39, 24])
    train, test, groups = _split(panel, n_train_months=20)
    want, ref = _reference_enet(train, test)

    diag = make_diag()
    got, _ = make_elasticnet_fit_predict(diag=diag)(train, test)
    got3, _ = make_elasticnet_fit_predict(inner_cv="rows")(train, test, groups)
    assert got == want and got3 == want
    assert diag["alpha"] == [float(ref.alpha_)]
    assert diag["coef"] == [[float(c) for c in ref.coef_]]
    assert "inner_cv" not in diag          # rows は診断値に何も足さない（夜間の診断値の形は不変）

    # 空振り防止: この検体では区切り方を変えると予測が動く（一致が自明に通る検体ではない）
    got_m, _ = make_elasticnet_fit_predict(inner_cv="months")(train, test, groups)
    assert got_m != want


def test_months_fit_ignores_the_order_of_rows_within_a_month():
    """months: 月内の行を入れ替えても、選ぶ α・l1_ratio と予測は変わらない。

    α は行の並びで足し算の順が変わるぶん末尾の桁だけ動く（α パスは Xᵀy から作る）ので、
    同じ候補を選んだかは相対 1e-12 で比べる。
    """
    sizes = [40, 17, 33, 25, 38, 21, 29, 15, 36, 23, 31, 19, 27, 40, 22, 34, 18, 30, 26, 39, 24]
    panel = _uneven_panel(sizes)
    rng = np.random.default_rng(964)
    shuffled = {ym: [rows[i] for i in rng.permutation(len(rows))] for ym, rows in panel.items()}
    tr_a, te, groups = _split(panel, n_train_months=20)
    tr_b, _te, _g = _split(shuffled, n_train_months=20)
    assert tr_a != tr_b                                         # 並びは実際に変わっている

    def fit(train, inner_cv):
        d = make_diag()
        yhat, _ = make_elasticnet_fit_predict(diag=d, inner_cv=inner_cv)(train, te, groups)
        return d, yhat

    (a, yhat_a), (b, yhat_b) = fit(tr_a, "months"), fit(tr_b, "months")
    assert math.isclose(a["alpha"][0], b["alpha"][0], rel_tol=1e-12)
    assert a["l1_ratio"] == b["l1_ratio"]
    assert np.allclose(yhat_a, yhat_b, rtol=0.0, atol=1e-9)
    assert a["inner_cv"] == ["months"]
    # 空振り防止: rows では同じ入れ替えで選ぶ α が動く（#964 の症状そのもの）
    (ra, _), (rb, _) = fit(tr_a, "rows"), fit(tr_b, "rows")
    assert not math.isclose(ra["alpha"][0], rb["alpha"][0], rel_tol=1e-6)


def test_months_needs_the_month_sizes():
    """months は各学習月の行数が要る（2 引数で呼ぶと分割を作れない）。"""
    train, test, _groups = _split(_panel())
    with pytest.raises(ValueError, match="train_groups"):
        make_elasticnet_fit_predict(inner_cv="months")(train, test)


def test_candidate_menu_asks_for_month_sizes_only_for_months():
    """候補メニューの elasticnet: 既定は従来どおり 2 引数、months のときだけ train_groups を受け取る。"""
    _fp, extra, _ = build_candidate("elasticnet", FEATURE_NAMES)
    assert extra == {}
    fp, extra_m, diag = build_candidate("elasticnet", FEATURE_NAMES, {"inner_cv": "months"})
    assert extra_m == {"pass_train_groups": True}
    panel = _uneven_panel([30, 12, 25, 18, 28, 14, 22, 11, 27, 16, 24, 13, 20, 30, 15, 26,
                           12, 23, 19, 29, 17, 21, 25, 14])
    folds = walk_forward_cv_monthly(panel, FEATURE_NAMES, min_train_months=6, step_months=3,
                                    fit_predict=fp, embargo_months=12, **extra_m)
    assert folds, "months の fold が1つも立たなかった（全 fold が例外で飛ばされた可能性）"
    assert summarize_diag(diag)["inner_cv"] == "months"


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
