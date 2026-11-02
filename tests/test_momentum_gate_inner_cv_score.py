"""`scripts/momentum_gate.py` の M-6 の選ぶ基準のモード（`--inner-cv-score`・#1116・親 #1112）。

M-6 の α・l1_ratio を内側 CV の二乗誤差で選ぶ（本番・`mse`）か、内側の検証月の rank-IC の平均で選ぶ（`rank_ic`）かを
共通域で比べる道具。判定の規則は測る前に `docs/prereg/m6-inner-score.md` へ凍結した。危ないのは**黙って同じもの・
別のものを比べる**形:
  - opts が候補まで届かず、両条件とも同じ基準で回る（`walk_forward_cv_monthly` は fit_predict の例外を握る）
  - `inner_score` だけ渡して `inner_cv` を落とし、候補が行で区切る既定に戻る（`rank_ic` は月で区切る CV が要る）
  - 本番が `mse` から変わったのに、分母を `mse` のまま比べる
  - 凍結した文書と違う文書・違う凍結値で回し、規則を見てから選んだ結果に見えなくなる
このファイルは条件・分母・検定数・opts・届いたかの確認・事前登録の照合・判定・`main` の配線を縛る。
"""
import argparse
import inspect
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.macro_enet as me  # noqa: E402
from plugins.model_candidates import _EN_INNER_SCORE_CHOICES, _EN_INNER_SCORE_DEFAULT  # noqa: E402
from scripts import momentum_gate, prereg  # noqa: E402
from scripts.momentum_gate import (  # noqa: E402
    INNER_CV_PURGE_CONDS, INNER_CV_SCORE_BASE_COND, INNER_CV_SCORE_CONDS, INNER_CV_SCORE_MODELS,
    INNER_CV_SCORE_PREREG, MODE_SUFFIX, MOM_WINDOW, VALID_SPLIT_CONDS, base_of, bonferroni_alpha,
    build_conditions, cv_opts, inner_cv_score_decision, inner_score_reach_problems, mode_of, panel_cond,
    prereg_stamp_for, top_k_paired_reference, verdict_text,
)

ROOT = Path(__file__).resolve().parent.parent
PREREG_TEXT = (ROOT / INNER_CV_SCORE_PREREG).read_text(encoding="utf-8")
FROZEN = prereg.recorded_sha(PREREG_TEXT)


class TestConditions:
    def test_compares_the_production_mse_with_rank_ic(self):
        conds = build_conditions(inner_cv_score=True)
        assert list(conds) == list(INNER_CV_SCORE_CONDS) == ["mse", "rank_ic"]
        assert {n: c.inner_score for n, c in conds.items()} == {"mse": "mse", "rank_ic": "rank_ic"}
        assert set(INNER_CV_SCORE_CONDS.values()) == set(_EN_INNER_SCORE_CHOICES)

    def test_other_axes_stay_at_the_production_shape(self):
        """差し替えるのは選ぶ基準だけ。内側の区切り方は両側とも本番（`months_purged`）。"""
        conds = build_conditions(inner_cv_score=True)
        assert conds["mse"] == momentum_gate.Cond(False, MOM_WINDOW)
        assert conds["rank_ic"]._replace(inner_score=me._INNER_SCORE) == conds["mse"]
        assert {c.inner_cv for c in conds.values()} == {me._INNER_CV}

    def test_both_sides_share_one_panel(self):
        conds = build_conditions(inner_cv_score=True)
        assert len({panel_cond(c) for c in conds.values()}) == 1
        assert len(set(conds.values())) == 2          # CV は条件ごとに回る

    def test_baseline_is_the_production_criterion_of_m6(self):
        conds = build_conditions(inner_cv_score=True)
        assert base_of(conds) == INNER_CV_SCORE_BASE_COND == "mse"
        assert INNER_CV_SCORE_CONDS[INNER_CV_SCORE_BASE_COND] == me._INNER_SCORE

    def test_stops_when_production_is_no_longer_the_baseline(self, monkeypatch):
        """本番が `mse` でなくなったら止める——黙って `mse` を分母にすると、本番と比べているつもりで別物を比べる。"""
        monkeypatch.setattr(momentum_gate, "_M6_INNER_SCORE", "rank_ic")
        with pytest.raises(ValueError, match="分母"):
            build_conditions(inner_cv_score=True)

    def test_names_do_not_collide_with_the_other_modes(self):
        assert not set(INNER_CV_SCORE_CONDS) & set(INNER_CV_PURGE_CONDS)
        assert not set(INNER_CV_SCORE_CONDS) & set(VALID_SPLIT_CONDS)

    def test_measures_m6_only(self):
        assert INNER_CV_SCORE_MODELS == ["elasticnet"]
        build_conditions(inner_cv_score=True, models=["elasticnet"])
        for models in (["xgb_m2"], ["xgb_m2", "elasticnet"], ["risk_return"]):
            with pytest.raises(ValueError, match="M-6"):
                build_conditions(inner_cv_score=True, models=models)

    def test_alpha_is_two_tests(self):
        """事前登録 §3: 1 モデル × 2 指標 ＝ 2 検定・α 0.025。"""
        conds = build_conditions(inner_cv_score=True)
        assert bonferroni_alpha(len(INNER_CV_SCORE_MODELS), len(conds)) == pytest.approx(0.025)


class TestOptions:
    def test_rank_ic_side_carries_the_month_blocks_too(self):
        """`rank_ic` は月で区切る内側 CV が要る。`inner_score` だけ渡すと候補は既定の `rows` に戻って止まる。"""
        conds = build_conditions(inner_cv_score=True)
        assert cv_opts(conds["rank_ic"], "elasticnet") == {"inner_cv": me._INNER_CV, "inner_score": "rank_ic"}
        assert cv_opts(conds["mse"], "elasticnet") == {"inner_cv": me._INNER_CV}
        assert "inner_score" not in (cv_opts(conds["rank_ic"], "xgb_m2") or {})   # M-2 に漏れない

    @pytest.mark.parametrize("kwargs", [
        dict(), dict(windows=[3, 12]), dict(macro=True), dict(fin_nan=True), dict(fin_fill=True),
        dict(size_neutral=True), dict(valid_split=True), dict(inner_cv_purge=True), dict(subsample=True),
        dict(earnings_yield=True),
    ])
    def test_existing_modes_run_the_production_criterion(self, kwargs):
        """既存モードの M-6 は本番の基準（`mse`）で回り、opts に選ぶ基準を足さない（形を変えない）。"""
        for c in build_conditions(**kwargs).values():
            assert c.inner_score == me._INNER_SCORE == _EN_INNER_SCORE_DEFAULT
            assert "inner_score" not in (cv_opts(c, "elasticnet") or {})


class TestReachCheck:
    def test_reach_check(self):
        conds = build_conditions(inner_cv_score=True)
        healthy = {"mse|elasticnet": {"inner_cv": "months_purged"},
                   "rank_ic|elasticnet": {"inner_cv": "months_purged", "inner_score": "rank_ic"}}
        assert inner_score_reach_problems(healthy, conds) == []
        lost = {"mse|elasticnet": {"inner_cv": "months_purged"},
                "rank_ic|elasticnet": {"inner_cv": "months_purged"}}
        assert inner_score_reach_problems(lost, conds) == [
            "rank_ic|elasticnet: 選ぶ基準が mse で回った（指定は rank_ic）"]
        # 全 fold が落ちて診断値が空でも、届いたとはみなさない
        assert inner_score_reach_problems({"rank_ic|elasticnet": None}, conds)


def _args(**kw):
    base = dict(inner_cv_score=True, prereg=INNER_CV_SCORE_PREREG, prereg_sha=FROZEN)
    return argparse.Namespace(**{**base, **kw})


class TestPrereg:
    """事前登録 §5: 本文が凍結値と一致しなければ exit 2 で、重い計算の前に止まる。"""

    @pytest.fixture(autouse=True)
    def _repo_root(self, monkeypatch):
        monkeypatch.chdir(ROOT)
        monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)

    def test_document_is_frozen(self):
        assert FROZEN == prereg.body_sha256(PREREG_TEXT)
        assert prereg.issue_url(PREREG_TEXT).endswith("/issues/1116")

    def test_matching_document_returns_the_stamp(self):
        stamp = prereg_stamp_for(_args())
        assert stamp["prereg_path"] == INNER_CV_SCORE_PREREG
        assert stamp["prereg_sha256"] == FROZEN

    @pytest.mark.parametrize("kw", [dict(prereg=None), dict(prereg_sha=None), dict(prereg_sha="0" * 64)])
    def test_missing_or_wrong_values_stop_with_exit_2(self, kw, capsys):
        with pytest.raises(SystemExit) as e:
            prereg_stamp_for(_args(**kw))
        assert e.value.code == prereg.EXIT_MISMATCH
        assert "[prereg] NG" in capsys.readouterr().err

    def test_another_frozen_document_is_rejected(self, capsys):
        """凍結値が合っていても、このモードの文書でなければ止める（別の規則で測った結果に見えないように）。"""
        other = "docs/prereg/top-k-veto.md"
        sha = prereg.recorded_sha((ROOT / other).read_text(encoding="utf-8"))
        with pytest.raises(SystemExit) as e:
            prereg_stamp_for(_args(prereg=other, prereg_sha=sha))
        assert e.value.code == prereg.EXIT_MISMATCH
        assert INNER_CV_SCORE_PREREG in capsys.readouterr().err

    def test_other_modes_do_not_take_a_prereg(self):
        assert prereg_stamp_for(_args(inner_cv_score=False, prereg=None, prereg_sha=None)) is None
        with pytest.raises(SystemExit, match="--inner-cv-score"):
            prereg_stamp_for(_args(inner_cv_score=False))


def _sig(mean, p):
    return {"mean": mean, "p_value": p, "ci_lo": mean - 0.01, "ci_hi": mean + 0.01, "n_common": 18}


def _sigs(ic=None, short=None):
    out = {}
    if ic is not None:
        out["common|rank_ic|elasticnet|rank_ic"] = _sig(*ic)
    if short is not None:
        out["common|rank_ic|elasticnet|short_side_spread"] = _sig(*short)
    return out


class TestDecision:
    """事前登録 §4: rank-IC が有意に正で、売り側spread が有意に負でなければ採用。それ以外は据え置き。"""

    @pytest.mark.parametrize("ic, short, want", [
        ((0.004, 0.001), (0.001, 0.40), "adopt"),
        ((0.004, 0.001), (0.003, 0.001), "adopt"),      # 売り側が有意に良くても採用は rank-IC で決まる
        ((0.004, 0.001), (-0.002, 0.30), "adopt"),      # 売り側の悪化が有意でなければ拒否権は発動しない
        ((0.004, 0.001), (-0.002, 0.010), "keep"),      # 売り側spread の拒否権
        ((0.004, 0.030), (0.001, 0.40), "keep"),        # 0.025 を超える p は有意でない
        ((-0.004, 0.001), (0.001, 0.40), "keep"),       # rank-IC が有意に負
        ((0.0, 0.025), (0.001, 0.40), "keep"),          # 境界: p < alpha の厳密な不等号
        (None, (0.003, 0.001), "keep"),                 # rank-IC の検定が出ない
        ((0.004, 0.001), None, "adopt"),                # 売り側の検定が出なければ拒否権は発動しない
    ])
    def test_rule(self, ic, short, want):
        got = inner_cv_score_decision(_sigs(ic, short), 0.025)
        assert got["decision"] == want
        assert got["alpha"] == 0.025


class TestTopKReference:
    def test_mean_of_paired_differences_on_common_periods(self):
        a = {"top_k_excess_return_by_period": {"10": {"2024-01": 0.03, "2024-04": 0.01, "2024-07": 0.02}}}
        b = {"top_k_excess_return_by_period": {"10": {"2024-01": 0.01, "2024-04": 0.02, "2024-10": 0.05}}}
        got = top_k_paired_reference(a, b, 10)
        assert got == {"k": 10, "n_common": 2, "mean_diff": pytest.approx(0.005)}

    def test_missing_series_gives_no_value(self):
        assert top_k_paired_reference({}, {}, 10) == {"k": 10, "n_common": 0, "mean_diff": None}


class TestOutput:
    def test_mode_and_suffix(self):
        assert mode_of(inner_cv_score=True) == "inner_cv_score"
        assert MODE_SUFFIX["inner_cv_score"] == "_inner_cv_score"

    def test_verdict_names_the_axis_and_the_prereg(self):
        v = verdict_text("inner_cv_score", 2, [], [])
        assert "INNER CV SCORE AXIS" in v and "M-6 only" in v and INNER_CV_SCORE_PREREG in v
        v.encode("cp932")

    def test_main_wiring(self):
        """**渡し忘れても例外は出ない**（両条件が同じ基準になり、差なしだけが残る）。"""
        src = inspect.getsource(momentum_gate.main)
        assert src.count("inner_cv_score=args.inner_cv_score") == 2      # build_conditions / mode_of
        assert src.count("inner_score_reach_problems(") == 2             # 全月と共通月の確認
        assert "prereg_stamp = prereg_stamp_for(args)" in src            # 重い計算の前に照合する
        assert "INNER_CV_SCORE_MODELS" in src
        assert "inner_cv_score_decision(sigs, alpha)" in src
        assert '"inner_score": c.inner_score' in src
        # 照合はパネルを読む前（DB に触る前）
        assert src.index("prereg_stamp_for(args)") < src.index("build_conditions(")
