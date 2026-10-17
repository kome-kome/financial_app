"""M-2 の early stopping の検証集合の取り方（`valid_split`・#976・#969）。

`rows`（本番・既定）は学習行の末尾 2 割を行の番号で取るので、区切りが月の途中に落ちる。`months` は末尾の月を
丸ごと取る。危ないのは次の形で、どれも例外が出ない:

  - `rows` の手順が #976 の前と変わる（本番の M-2 が黙って別物になる）
  - `months` の検証集合が月の途中で切れる／月内の並びで変わる（直したつもりの問題が残る）
  - `train_groups` を渡し忘れる（`walk_forward_cv_monthly` は fit_predict の例外を握って fold を捨てる）
  - 候補 `xgb_m2` の opts の `valid_split` が XGBoost の設定へ混ざる

行は特徴量の1列目で識別する（月の番号 × 100 ＋ 月内の番号）。fit は差し替えて受け取った行を記録するだけで、
本物の学習は走らせない（`test_macro_gbdt.py::test_eval_set_is_subset_of_train` と同じやり方）。
"""
import os
import random
import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_gbdt as mg  # noqa: E402
from plugins.macro_gbdt import (  # noqa: E402
    _MIN_FIT_N, _VALID_FRAC, _make_xgb_fit_predict, _tail_month_valid_rows,
)

_XGB = {"max_depth": 3, "learning_rate": 0.1, "subsample": 0.8, "colsample_bytree": 0.8,
        "min_child_weight": 1, "reg_lambda": 1.0, "reg_alpha": 0.0,
        "n_estimators": 100, "early_stopping_rounds": 10,
        "tree_method": "hist", "objective": "reg:squarederror", "random_state": 42}

# 月ごとの行数。末尾の月（番号 4）は 7 行で、行で取る検証（25 × 0.2 = 5 行）は月の途中で切れる。
GROUPS = [3, 4, 5, 6, 7]


def _train(groups=GROUPS, shuffle_within=None):
    rows = []
    for m, g in enumerate(groups):
        seg = [([m * 100.0 + i, 1.0, 2.0], 0.01 * (m * 100 + i)) for i in range(g)]
        if shuffle_within is not None:
            random.Random(shuffle_within).shuffle(seg)
        rows.extend(seg)
    return rows


_TEST = [([9000.0 + j, 0.0, 0.0], 0.05) for j in range(3)]


def _fit_rows(valid_split, train, groups=GROUPS, **kw):
    """fit_predict を差し替えた XGBoost で1回呼び、(fit 側の行 id, 検証側の行 id, best_iterations)。"""
    import xgboost as xgb

    calls = []

    def spy_fit(self, X, y, eval_set=None, verbose=False, **kwargs):
        calls.append((np.asarray(X)[:, 0].tolist(),
                      None if eval_set is None else np.asarray(eval_set[0][0])[:, 0].tolist()))
        return self

    best: list = []
    fp = _make_xgb_fit_predict(_XGB, best, valid_split=valid_split, **kw)
    with patch.object(xgb.XGBRegressor, "fit", spy_fit), \
         patch.object(xgb.XGBRegressor, "predict", lambda self, X: np.zeros(len(X))):
        if valid_split == "rows" and groups is None:
            fp(train, _TEST)
        else:
            fp(train, _TEST, groups)
    assert len(calls) == 1
    return calls[0][0], calls[0][1], best


class TestProductionDefault:
    def test_production_default_is_months(self):
        """#969 が `momentum_gate --valid-split`（2026-10-17）の結果で `months` へ切り替えた。"""
        assert mg._VALID_SPLIT == "months"
        assert mg._VALID_SPLIT_DEFAULT == "rows"
        assert mg._VALID_SPLIT_CHOICES == ("rows", "months")

    def test_cv_callback_follows_the_constant(self, monkeypatch):
        """本番の CV は `_VALID_SPLIT` の1行で切り替わり、`months` のときだけ各月の行数を受け取る。"""
        plugin = mg.MacroGbdtPlugin()
        _fp, extra_m = plugin._make_cv_callback(_XGB, [])
        assert extra_m == {"pass_train_groups": True}   # 本番（months）は各学習月の行数を受け取る
        monkeypatch.setattr(mg, "_VALID_SPLIT", "rows")
        fp, extra = plugin._make_cv_callback(_XGB, [])
        assert extra == {}                       # 定数1つを戻せば #976 の前と同じ 2 引数呼び出し


class TestRowsIsUnchanged:
    def test_rows_takes_the_tail_rows_as_before(self):
        """`rows` は末尾 `_VALID_FRAC` の行（#976 の前の手順）。この検体では区切りが月の途中に落ちる。"""
        train = _train()
        fit_ids, valid_ids, _ = _fit_rows("rows", train, groups=None)
        n_valid = max(1, int(len(train) * _VALID_FRAC))
        assert valid_ids == [r[0][0] for r in train[-n_valid:]]
        assert fit_ids == [r[0][0] for r in train[:-n_valid]]
        # 空振り防止: 末尾の月（400 番台）が fit 側と検証側に割れている
        assert any(400 <= i < 500 for i in fit_ids) and all(400 <= i < 500 for i in valid_ids)

    def test_rows_ignores_train_groups(self):
        """3 引数で呼ばれても `rows` は行数を見ない（呼び出し側の経路によらず同じ検証集合）。"""
        train = _train()
        assert _fit_rows("rows", train, groups=None)[:2] == _fit_rows("rows", train, groups=GROUPS)[:2]

    def test_rows_reaches_real_xgboost_bit_identically(self):
        """本物の XGBoost で、既定の呼び出しと `valid_split="rows"` の予測・best_iteration が1ビットも同じ。"""
        rng = np.random.default_rng(0)
        train = [([float(v) for v in rng.normal(size=3)], float(rng.normal())) for _ in range(120)]
        test = [([float(v) for v in rng.normal(size=3)], 0.0) for _ in range(10)]
        b1, b2 = [], []
        y1, _ = _make_xgb_fit_predict(_XGB, b1)(train, test)
        y2, _ = _make_xgb_fit_predict(_XGB, b2, valid_split="rows")(train, test)
        assert y1 == y2 and b1 == b2


class TestMonthsTakesWholeTailMonths:
    def test_valid_set_is_whole_tail_months(self):
        fit_ids, valid_ids, _ = _fit_rows("months", _train())
        assert sorted(valid_ids) == [400.0 + i for i in range(7)]        # 末尾の月を丸ごと
        assert not any(400 <= i < 500 for i in fit_ids)                 # 月の途中で切れない

    def test_shuffling_within_months_does_not_change_the_split(self):
        """月内の行の並びを入れ替えても、検証に入る行の集合は同じ（#969 の問題そのもの）。"""
        base = _fit_rows("months", _train())
        for seed in (1, 2, 3):
            got = _fit_rows("months", _train(shuffle_within=seed))
            assert sorted(got[0]) == sorted(base[0]) and sorted(got[1]) == sorted(base[1])
        # 空振り防止: rows は同じ入れ替えで検証に入る行が変わる
        r0 = _fit_rows("rows", _train(), groups=None)[1]
        r1 = _fit_rows("rows", _train(shuffle_within=1), groups=None)[1]
        assert sorted(r0) != sorted(r1)

    def test_needs_the_month_sizes(self):
        fp = _make_xgb_fit_predict(_XGB, [], valid_split="months")
        with pytest.raises(ValueError, match="train_groups"):
            fp(_train(), _TEST)

    def test_marks_each_fold_that_took_months(self):
        """切り替えが CV まで届いたかを呼び出し側が確かめる印（`momentum_gate --valid-split`）。"""
        marks: list = []
        _fit_rows("months", _train(), split_marks=marks)
        assert marks == ["months"]
        marks_rows: list = []
        _fit_rows("rows", _train(), groups=None, split_marks=marks_rows)
        assert marks_rows == []

    def test_unknown_scheme_is_rejected(self):
        with pytest.raises(ValueError, match="valid_split"):
            _make_xgb_fit_predict(_XGB, [], valid_split="weeks")

    def test_too_few_rows_falls_back_like_rows(self):
        """fit 側が `_MIN_FIT_N` 未満なら、今と同じく early stopping を諦めて固定本数で学習する。"""
        groups = [2, 3]
        fit_ids, valid_ids, best = _fit_rows("months", _train(groups), groups=groups)
        assert valid_ids is None and len(fit_ids) == 5
        assert best == [_XGB["n_estimators"]]
        assert 2 < _MIN_FIT_N


class TestTailMonthValidRows:
    @pytest.mark.parametrize("groups, want", [
        ([10, 10, 10, 10, 10], 10),      # 目標 10 行 → 末尾 1 か月
        ([30, 5, 5], 10),                # 目標 8 行 → 5（差 3）より 10（差 2）
        ([3, 4, 5, 6, 7], 7),            # 目標 5 行 → 7（差 2）より 13（差 8）
        ([10, 1, 1, 1, 1], 3),           # 目標 2.8 行 → 3（差 0.2）。4（差 1.2）まで足さない
    ])
    def test_closest_to_the_target(self, groups, want):
        assert _tail_month_valid_rows(groups, sum(groups)) == want

    def test_keeps_at_least_one_month_for_fit(self):
        assert _tail_month_valid_rows([1, 100], 101) == 100
        assert _tail_month_valid_rows([100, 1], 101) == 1

    def test_tie_goes_to_fewer_months(self):
        # 目標 4 行: 末尾 1 か月 = 2（差 2）・2 か月 = 6（差 2）→ 少ない側
        assert _tail_month_valid_rows([14, 4, 2], 20) == 2

    def test_single_month_leaves_nothing_to_fit(self):
        assert _tail_month_valid_rows([12], 12) == 12

    def test_sizes_must_add_up(self):
        with pytest.raises(ValueError, match="合わない"):
            _tail_month_valid_rows([3, 4], 8)


class TestCandidateKeepsTheOptionOutOfXgboost:
    """候補 `xgb_m2` は opts の `valid_split` を XGBoost の設定へ混ぜない（混ぜると未知の引数として届く）。"""

    def test_default_is_unchanged(self):
        from plugins.model_candidates import CANDIDATES

        diag: dict = {"best_iteration": []}
        _fp, extra = CANDIDATES["xgb_m2"].build([], {}, diag)
        assert extra == {}

    def test_months_asks_for_the_month_sizes_and_marks(self):
        import xgboost as xgb
        from collections import defaultdict
        from plugins.model_candidates import CANDIDATES

        seen_params: list = []
        real_init = xgb.XGBRegressor.__init__

        def spy_init(self, *a, **kw):
            seen_params.append(dict(kw))
            real_init(self, *a, **kw)

        diag: dict = defaultdict(list)
        fp, extra = CANDIDATES["xgb_m2"].build([], {"valid_split": "months", "random_state": 7}, diag)
        assert extra == {"pass_train_groups": True}
        with patch.object(xgb.XGBRegressor, "__init__", spy_init), \
             patch.object(xgb.XGBRegressor, "fit", lambda self, *a, **k: self), \
             patch.object(xgb.XGBRegressor, "predict", lambda self, X: np.zeros(len(X))):
            fp(_train(), _TEST, GROUPS)
        assert seen_params and all("valid_split" not in p for p in seen_params)
        assert seen_params[0]["random_state"] == 7          # 他の opts はそのまま届く
        assert diag["valid_split"] == ["months"]
