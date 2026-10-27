"""`scripts/momentum_gate.py` の学習 fold の除外のモード（`--train-exclude-index`・#1068・#1010 の (e)）。

2022 前例の G22 のうちラベル窓に圧力日が入る (ym, 社) を**学習 fold からだけ**落とし、全行で学習する本番と比べる
道具。危ないのは**黙って同じものを比べる**形と、**評価域まで動かす**形:

  - `train_exclude` が CV まで届かず、両条件が同じ CV になる（`walk_forward_cv_monthly` は例外を出さない）
  - 落とした行を `train_groups` に数えたまま渡し、M-2 / M-6 の月の塊の切り出しがずれる
  - テスト月の行まで落とし、評価域が2条件で変わる（共通域の比較が別物になる）
  - 台帳の社コードとパネルの社コードが食い違い、1行も落ちない

このファイルは CV の核の除外・既定で何も変えないこと・素通し・除外行の作り方・条件・到達検査・`main` の配線を縛る。
モデルの学習は差し替えるか OLS の合成データで回し、DB と台帳の本物は読まない。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_event_study as S  # noqa: E402
import index_events as IE  # noqa: E402
from plugins.utils import walk_forward_cv_monthly  # noqa: E402
from scripts import candidate_bakeoff  # noqa: E402
from scripts import momentum_gate  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    DE_EXCLUDE_CONDS, INNER_CV_PURGE_CONDS, MODE_SUFFIX, MOM_WINDOW, SUBSAMPLE_CONDS, TRAIN_EXCLUDE_INDEX_BASE_COND,
    TRAIN_EXCLUDE_INDEX_CONDS, TRAIN_EXCLUDE_INDEX_GROUP, TRAIN_EXCLUDE_INDEX_MODELS, VALID_SPLIT_CONDS, base_of,
    bonferroni_alpha, build_conditions, cv_opts, index_exclusion_rows, mode_of, panel_cond, train_exclude_of,
    train_exclude_reach_problems, verdict_text,
)

CAL22 = S.pressure_calendar(IE.STEPS_2022)


def _months(first_year: int, n: int) -> list[str]:
    return [f"{first_year + (k // 12)}-{k % 12 + 1:02d}" for k in range(n)]


def _panel(yms, per_month=4):
    """行 j の特徴量に (月の番号, j) を埋めた合成パネル。どの行が学習に入ったかを特徴量から読み戻せる。"""
    return {ym: [([float(k), float(j), 1.0], 0.01 * (k + j)) for j in range(per_month)]
            for k, ym in enumerate(yms)}


class _Spy:
    """walk_forward_cv_monthly へ注入する fit_predict。fold ごとに学習行・月の行数・テスト行を控える。"""

    def __init__(self):
        self.calls = []

    def __call__(self, train, test, groups):
        self.calls.append((list(train), list(groups), list(test)))
        return [0.0] * len(test), [y for _x, y in test]


def _run(samples, train_exclude=None, **kw):
    spy = _Spy()
    folds, resid = walk_forward_cv_monthly(
        samples, ["a", "b", "c"], min_train_months=3, step_months=2, return_residuals=True,
        fit_predict=spy, pass_train_groups=True, embargo_months=1, train_exclude=train_exclude, **kw)
    return folds, resid, spy


class TestWalkForwardExclusion:
    YMS = _months(2020, 10)

    def test_none_is_identical_to_not_passing_it(self):
        """既定 None は引数を足す前と同じ（fold の結果・残差・学習行・`train_groups`）。記録の列も増やさない。"""
        samples = _panel(self.YMS)
        spy_old = _Spy()
        folds_old, resid_old = walk_forward_cv_monthly(
            samples, ["a", "b", "c"], min_train_months=3, step_months=2, return_residuals=True,
            fit_predict=spy_old, pass_train_groups=True, embargo_months=1)
        folds_new, resid_new, spy_new = _run(samples)
        assert folds_new == folds_old
        assert resid_new == resid_old
        assert spy_new.calls == spy_old.calls
        assert all("n_train_excluded" not in f for f in folds_new)

    def test_drops_only_training_rows_and_recounts_groups(self):
        samples = _panel(self.YMS)
        drop = {self.YMS[0]: {1, 3}, self.YMS[2]: {0}, self.YMS[-1]: {0, 1, 2, 3}}   # 最後の月は test にしかならない
        folds_keep, resid_keep, spy_keep = _run(samples, train_exclude={})
        folds_ex, resid_ex, spy_ex = _run(samples, train_exclude=drop)
        assert [f["test_ym"] for f in folds_ex] == [f["test_ym"] for f in folds_keep]
        for (tr_k, g_k, te_k), (tr_e, g_e, te_e), f in zip(spy_keep.calls, spy_ex.calls, folds_ex):
            assert te_e == te_k                                         # 評価域は同じ
            assert sum(g_e) == len(tr_e)                                # 月の行数は落とした後で数える
            dropped = {(int(x[0]), int(x[1])) for x, _y in tr_k} - {(int(x[0]), int(x[1])) for x, _y in tr_e}
            want = {(k, j) for k, ym in enumerate(self.YMS) for j in drop.get(ym, ())
                    if (k, j) in {(int(x[0]), int(x[1])) for x, _y in tr_k}}
            assert dropped == want
            assert f["n_train_excluded"] == len(want) == len(tr_k) - len(tr_e)
        assert resid_ex.keys() == resid_keep.keys()
        assert all(len(resid_ex[ym]) == len(resid_keep[ym]) for ym in resid_keep)   # テスト行は落ちない

    def test_a_fully_dropped_month_leaves_no_empty_group(self):
        samples = _panel(self.YMS)
        _folds, _r, spy = _run(samples, train_exclude={self.YMS[1]: {0, 1, 2, 3}})
        assert all(0 not in groups for _tr, groups, _te in spy.calls)

    def test_empty_map_records_zero_per_fold(self):
        folds, _r, _spy = _run(_panel(self.YMS), train_exclude={})
        assert folds and all(f["n_train_excluded"] == 0 for f in folds)


class TestBakeoffPassThrough:
    def test_run_one_passes_it_and_records_per_fold(self, monkeypatch):
        seen = {}

        def fake_wf(*a, **kw):
            seen.update(kw)
            return [{"test_ym": "2024-01", "n_train_excluded": 0},
                    {"test_ym": "2024-03", "n_train_excluded": 7}], {}

        monkeypatch.setattr(candidate_bakeoff, "walk_forward_cv_monthly", fake_wf)
        monkeypatch.setattr(candidate_bakeoff, "_backtest", lambda r, m: {})
        drop = {"2022-01": {0}}
        out = candidate_bakeoff.run_one("ols", {}, {}, {}, ["a"], 0, train_exclude=drop)
        assert seen["train_exclude"] is drop
        assert out["train_excluded"] == [0, 7]

    def test_run_one_default_adds_nothing(self, monkeypatch):
        seen = {}

        def fake_wf(*a, **kw):
            seen.update(kw)
            return [{"test_ym": "2024-01"}], {}

        monkeypatch.setattr(candidate_bakeoff, "walk_forward_cv_monthly", fake_wf)
        monkeypatch.setattr(candidate_bakeoff, "_backtest", lambda r, m: {})
        out = candidate_bakeoff.run_one("ols", {}, {}, {}, ["a"], 0)
        assert seen["train_exclude"] is None
        assert "train_excluded" not in out

    @pytest.mark.parametrize("name,seeds", [("elasticnet", 5), ("xgb_m2", 1), ("xgb_m2", 3)])
    def test_run_seeds_passes_it_to_every_run(self, monkeypatch, name, seeds):
        calls = []

        def fake_run_one(*a, **kw):
            calls.append(kw.get("train_exclude"))
            return {"oof": {"rank_ic": {"mean": 0.0}, "short_side_spread": 0.0},
                    "_parts": ({"2024-01": [(0.0, 0.1)]}, {"2024-01": [("E1", "x")]})}

        monkeypatch.setattr(candidate_bakeoff, "run_one", fake_run_one)
        monkeypatch.setattr(candidate_bakeoff, "_backtest", lambda r, m: {"rank_ic": {"mean": 0.0}})
        drop = {"2022-01": {0}}
        candidate_bakeoff.run_seeds(name, {}, {}, {}, ["a"], 0, seeds=seeds, return_parts=True,
                                    train_exclude=drop)
        assert calls and all(c is drop for c in calls)


class TestExclusionRows:
    MEMBERS = frozenset({"E1", "E3"})

    def test_only_members_in_months_whose_label_window_holds_a_pressure_day(self):
        ids = {"2021-01": ["E1", "E2", "E3"],      # 窓 (2021-01-31, 2022-01-30] に圧力日なし
               "2021-12": ["E3", "E1", "E2"],      # 2022-10 の圧力日が窓に入る
               "2024-06": ["E2", "E3"],
               "2025-06": ["E1", "E3"]}            # 最後の圧力日（2025-01）より後
        got = index_exclusion_rows(ids, self.MEMBERS, CAL22)
        assert got == {"2021-12": {0, 1}, "2024-06": {1}}

    def test_uses_the_event_study_exposure(self):
        """重なりは E_t > 0 と同じ判定（窓を書き写さない）。"""
        for ym in _months(2020, 72):
            hit = S.label_window_exposure(ym, CAL22)[0] > 0
            assert (ym in index_exclusion_rows({ym: ["E1"]}, self.MEMBERS, CAL22)) == hit

    def test_condition_without_the_flag_passes_nothing(self):
        conds = build_conditions(train_exclude_index=True)
        ids = {"2022-06": ["E1", "E2"]}
        assert train_exclude_of(conds["keep_index"], ids, self.MEMBERS, CAL22) is None
        assert train_exclude_of(conds["exclude_index"], ids, self.MEMBERS, CAL22) == {"2022-06": {0}}

    def test_group_is_the_2022_transition_designation(self):
        assert TRAIN_EXCLUDE_INDEX_GROUP == ("topix-2022-10", "transition")


class TestConditions:
    def test_two_conditions_on_the_production_shape(self):
        conds = build_conditions(train_exclude_index=True)
        assert list(conds) == list(TRAIN_EXCLUDE_INDEX_CONDS) == ["keep_index", "exclude_index"]
        assert conds["keep_index"] == momentum_gate.Cond(False, MOM_WINDOW)
        assert conds["exclude_index"] == momentum_gate.Cond(False, MOM_WINDOW, train_exclude_index=True)

    def test_both_sides_share_one_panel_but_run_their_own_cv(self):
        conds = build_conditions(train_exclude_index=True)
        assert len({panel_cond(c) for c in conds.values()}) == 1
        assert len(set(conds.values())) == 2

    def test_no_cv_options_change(self):
        """候補へ渡す opts は2条件で同じ（差は学習側の除外だけ）。"""
        conds = build_conditions(train_exclude_index=True)
        for model in TRAIN_EXCLUDE_INDEX_MODELS:
            assert cv_opts(conds["keep_index"], model) == cv_opts(conds["exclude_index"], model)

    def test_baseline_is_production(self):
        conds = build_conditions(train_exclude_index=True)
        assert base_of(conds) == TRAIN_EXCLUDE_INDEX_BASE_COND == "keep_index"

    def test_names_do_not_collide(self):
        for other in (DE_EXCLUDE_CONDS, SUBSAMPLE_CONDS, VALID_SPLIT_CONDS, INNER_CV_PURGE_CONDS):
            assert not set(TRAIN_EXCLUDE_INDEX_CONDS) & set(other)

    def test_measures_m2_and_m6_only(self):
        assert TRAIN_EXCLUDE_INDEX_MODELS == ["xgb_m2", "elasticnet"]
        build_conditions(train_exclude_index=True, models=["elasticnet"])
        with pytest.raises(ValueError, match="M-2 / M-6"):
            build_conditions(train_exclude_index=True, models=["risk_return"])

    @pytest.mark.parametrize("other", [dict(subsample=True), dict(de_exclude=True), dict(size_neutral=True),
                                       dict(windows=[3])])
    def test_cannot_combine_with_another_mode(self, other):
        with pytest.raises(ValueError, match="同時に指定できません"):
            build_conditions(train_exclude_index=True, **other)

    def test_existing_modes_never_exclude(self):
        for kwargs in (dict(), dict(de_exclude=True), dict(subsample=True), dict(size_neutral=True)):
            assert not any(c.train_exclude_index for c in build_conditions(**kwargs).values())

    def test_alpha_is_four_tests(self):
        conds = build_conditions(train_exclude_index=True)
        assert bonferroni_alpha(len(TRAIN_EXCLUDE_INDEX_MODELS), len(conds)) == pytest.approx(0.0125)


class TestReachCheck:
    CONDS = build_conditions(train_exclude_index=True)

    def test_healthy(self):
        runs = {"keep_index|xgb_m2": {}, "exclude_index|xgb_m2": {"train_excluded": [0, 0, 12, 40]}}
        assert train_exclude_reach_problems(runs, self.CONDS) == []

    def test_early_folds_with_zero_are_fine(self):
        """最初のほうの fold は学習期間が重なる月より前で終わる。fold ごとの 0 では止めない。"""
        runs = {"exclude_index|elasticnet": {"train_excluded": [0, 0, 0, 5]}}
        assert train_exclude_reach_problems(runs, self.CONDS) == []

    @pytest.mark.parametrize("got,needle", [(None, "届いていない"), ([], "0"), ([0, 0], "0"),
                                            ([3, 0], "最後の fold")])
    def test_not_reached(self, got, needle):
        out = {} if got is None else {"train_excluded": got}
        problems = train_exclude_reach_problems({"exclude_index|xgb_m2": out}, self.CONDS)
        assert len(problems) == 1 and needle in problems[0]

    def test_baseline_must_not_drop(self):
        problems = train_exclude_reach_problems({"keep_index|xgb_m2": {"train_excluded": [0, 4]}}, self.CONDS)
        assert problems and "基準側" in problems[0]


class TestOutput:
    def test_mode_and_suffix(self):
        assert mode_of(train_exclude_index=True) == "train_exclude_index"
        assert MODE_SUFFIX["train_exclude_index"] == "_train_exclude_index"

    def test_verdict_names_the_axis(self):
        v = verdict_text("train_exclude_index", 2, [], [])
        assert "TRAIN EXCLUDE INDEX AXIS" in v and "training folds only" in v
        v.encode("cp932")

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ CV になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("train_exclude_index=args.train_exclude_index") == 2      # build_conditions / mode_of
        assert src.count("train_exclude=train_exclude_of(conds[cond], i, index_members") == 2   # 全月と共通月の CV
        assert src.count("train_exclude_reach_problems(") == 2                      # 全月と共通月の確認
        assert "IE.members(IE.load_ledger(db), *TRAIN_EXCLUDE_INDEX_GROUP)" in src
        assert "S.pressure_calendar(IE.STEPS_2022)" in src
        assert "TRAIN_EXCLUDE_INDEX_MODELS" in src
        assert '"train_exclude_index": c.train_exclude_index' in src               # JSON の conditions
