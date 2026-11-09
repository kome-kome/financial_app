"""M-6 と本番の設定の M-2 を上位10社で公平に比べる計測（#1155）: `scripts.measure_selection_optimism`。

DB と M-4・M-2 は回さず、合成の期別系列で確かめる。issue の「検証」の 4 項目に対応する:
期 t の選択に「t の 12 か月前より後」の成績を使わない / 全候補の期別系列を持つ（上位 20 件に限らない）/
事前登録の凍結値が合わなければ非 0 で止まる / 本番の探索の結果・周回の状態・画面は変わらない（どの実行も
`tuning_dry_run`・`tuning_objective_only` の内側）。
"""
import random

import numpy as np
import pytest

import database
import scripts.measure_selection_optimism as X
import scripts.oof_portfolio_replay as op
import scripts.run_daytime as rd
from scripts import prereg

SHA = prereg.body_sha256(prereg.read_document(X.PREREG))
PERIODS = [f"{2021 + (1 + 3 * k) // 12}-{(1 + 3 * k) % 12 + 1:02d}" for k in range(19)]   # 2021-02〜2025-08


def _series(level: float, wiggle: float, seed: int, periods=PERIODS) -> dict:
    rng = random.Random(seed)
    return {p: level + rng.uniform(-wiggle, wiggle) for p in periods}


def _cand(ic: float, top: float, seed: int, wiggle: float = 0.02) -> dict:
    return {"ic": _series(ic, wiggle, seed), "top": _series(top, wiggle, seed + 1000)}


def _base(n: int = 6) -> list[dict]:
    return [_cand(0.10 + 0.01 * i, 0.05 + 0.02 * ((i * 7) % 5), i) for i in range(n)]


def _seeds(base: list[dict], n_seeds: int = 2) -> dict:
    """候補ごとの種の系列（先頭が種 42）。種で少しだけ揺らす。"""
    out = {}
    for i, c in enumerate(base):
        runs = [c]
        for s in range(1, n_seeds):
            runs.append({"ic": {p: v + 0.001 * s for p, v in c["ic"].items()},
                         "top": {p: v - 0.002 * s for p, v in c["top"].items()}})
        out[i] = runs
    return out


# ── 時間順の選び直し ────────────────────────────────────────────────────────────

class TestAvailablePeriods:
    def test_only_periods_whose_answer_is_complete(self):
        assert X.available_periods("2022-02", PERIODS) == ["2021-02"]
        assert X.available_periods("2022-05", PERIODS) == ["2021-02", "2021-05"]
        assert X.available_periods("2021-11", PERIODS) == []

    def test_gap_is_the_label_horizon(self):
        assert X.GAP_MONTHS == 12


class TestTimeOrderedChoices:
    def test_early_periods_without_two_available_are_dropped(self):
        base = _base()
        choices = X.time_ordered_choices(PERIODS, base, _seeds(base), top=3)
        first = min(choices)
        assert first == "2022-05"                       # 使える期が 2 期になる最初の期
        assert all(len(X.available_periods(t, PERIODS)) >= 2 for t in choices)
        assert set(PERIODS) - set(choices) == set(PERIODS[:5])

    def test_choice_at_t_ignores_results_after_t_minus_12_months(self):
        """期 t の 12 か月前より後の成績を差し替えても、t の選択は変わらない（検証 1）。"""
        base = _base(8)
        seeds = _seeds(base, 3)
        t = "2023-11"
        before = X.time_ordered_choices(PERIODS, base, seeds, top=3)[t]
        hidden = set(PERIODS) - set(X.available_periods(t, PERIODS))
        rng = random.Random(9)
        tampered = [{k: {p: (rng.uniform(-1, 1) if p in hidden else v) for p, v in c[k].items()}
                     for k in ("ic", "top")} for c in base]
        tampered_seeds = {i: [{k: {p: (rng.uniform(-1, 1) if p in hidden else v) for p, v in r[k].items()}
                               for k in ("ic", "top")} for r in runs] for i, runs in seeds.items()}
        after = X.time_ordered_choices(PERIODS, tampered, tampered_seeds, top=3)[t]
        assert after["chosen"] == before["chosen"]
        assert after["finalists"] == before["finalists"]

    def test_choice_does_move_with_available_results(self):
        """使える期の成績は選択を動かす（上のテストが空振りしていないことの裏取り）。"""
        base = _base(8)
        t = "2023-11"
        before = X.time_ordered_choices(PERIODS, base, _seeds(base), top=3)[t]["chosen"]
        loser = next(i for i in range(8) if i != before)
        avail = set(X.available_periods(t, PERIODS))
        base[loser] = {"ic": {p: (v + 1.0 if p in avail else v) for p, v in base[loser]["ic"].items()},
                       "top": {p: (v + 1.0 if p in avail else v) for p, v in base[loser]["top"].items()}}
        after = X.time_ordered_choices(PERIODS, base, _seeds(base), top=3)[t]["chosen"]
        assert after == loser

    def test_previous_choice_joins_the_finalists(self):
        base = _base(8)
        avail = set(PERIODS[:4])
        top3 = X.finalists_at(avail, base, 3, None)
        outsider = next(i for i in range(8) if i not in top3)
        assert X.finalists_at(avail, base, 3, outsider) == top3 + [outsider]
        assert X.finalists_at(avail, base, 3, top3[0]) == top3

    def test_union_covers_every_period_finalist(self):
        base = _base(8)
        union = set(X.finalist_union(PERIODS, base, 3))
        choices = X.time_ordered_choices(PERIODS, base, _seeds(base), top=3)
        assert all(set(c["finalists"]) <= union for c in choices.values())


class TestSelectAt:
    """本番の 2 段の選び方（`plugins.tuning._two_stage_select`）を使える期に限って当てる。"""

    def _cands(self):
        # A: rank-IC の 1 位・上位10社は低い / B: rank-IC は同点（差は揺れの中）で上位10社が高い / C: rank-IC が有意に悪い
        wig = [0.02 if k % 2 else -0.02 for k in range(len(PERIODS))]
        a = {"ic": {p: 0.200 for p in PERIODS}, "top": {p: 0.05 for p in PERIODS}}
        b = {"ic": {p: 0.199 + w for p, w in zip(PERIODS, wig)}, "top": {p: 0.15 for p in PERIODS}}
        c = {"ic": {p: 0.100 + w for p, w in zip(PERIODS, wig)}, "top": {p: 0.30 for p in PERIODS}}
        return [a, b, c]

    def test_two_stage_picks_the_tie_member_with_the_higher_top_k(self):
        cands = self._cands()
        sel = X.select_at(set(PERIODS), [0, 1, 2], {i: [c] for i, c in enumerate(cands)})
        assert sel["selection"] == "two_stage"
        assert sel["leader"] == 0 and sel["chosen"] == 1
        assert sorted(sel["tie_set"]) == [0, 1]

    def test_failed_seed_run_drops_the_candidate(self):
        cands = self._cands()
        per_seed = {0: [cands[0], cands[0]], 1: [cands[1], {"ic": None, "top": None}], 2: [cands[2], cands[2]]}
        sel = X.select_at(set(PERIODS), [0, 1, 2], per_seed)
        assert 1 not in sel["seed_mean"] and sel["chosen"] == 0

    def test_without_series_falls_back_to_rank_ic(self):
        cands = self._cands()
        cands[1] = {"ic": cands[1]["ic"], "top": None}
        sel = X.select_at(set(PERIODS), [0, 1, 2], {i: [c] for i, c in enumerate(cands)})
        assert sel["selection"] == "rank_ic_only" and sel["chosen"] == 0

    def test_nothing_eligible(self):
        sel = X.select_at(set(PERIODS), [0], {0: [{"ic": None, "top": None}]})
        assert sel["chosen"] is None


class TestSpliceAndCompare:
    def test_splice_takes_the_chosen_candidate_of_each_period(self):
        rows = [("2022-05", "E1"), ("2022-05", "E2"), ("2022-08", "E1"), ("2021-02", "E1")]
        preds = np.array([[1.0, 2.0, 3.0, 4.0], [10.0, np.nan, 30.0, 40.0]])
        choices = {"2022-05": {"chosen": 1}, "2022-08": {"chosen": 0}}
        assert X.splice(choices, preds, rows) == {("2022-05", "E1"): 10.0, ("2022-08", "E1"): 3.0}

    def test_compare_uses_rows_every_model_has(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(X, "oof_backtest", lambda resid: seen.setdefault(len(seen), resid) or {})
        order = {"2022-05": ["E1", "E2", "E3"]}
        y = {("2022-05", e): 0.1 for e in ("E1", "E2", "E3")}
        preds = {"a": {("2022-05", "E1"): 1.0, ("2022-05", "E2"): 2.0, ("2022-05", "E3"): 3.0},
                 "b": {("2022-05", "E1"): 1.0, ("2022-05", "E3"): 3.0}}
        _scores, n_rows = X.compare(preds, y, order, ["2022-05"])
        assert n_rows == {"2022-05": 2}
        assert [yh for yh, _ in seen[0]["2022-05"]] == [1.0, 3.0]


def _oof(top: dict) -> dict:
    return {"top_k_excess_return_by_period": {str(X.TOP_K): top}}


class TestDecide:
    def _scores(self, m6: float, prod: float, time_: float) -> dict:
        wig = {p: (0.01 if k % 2 else -0.01) for k, p in enumerate(PERIODS)}
        return {X.M6: _oof({p: m6 + w for p, w in wig.items()}),
                X.PRODUCTION: _oof({p: prod + w for p, w in wig.items()}),
                X.TIME_ORDERED: _oof({p: time_ + w for p, w in wig.items()})}

    def test_m6_confirmed(self):
        assert X.decide(self._scores(0.30, 0.20, 0.25))["decision"] == "m6_confirmed"

    def test_consider_m2(self):
        assert X.decide(self._scores(0.20, 0.20, 0.30))["decision"] == "consider_m2"

    def test_conflict(self):
        assert X.decide(self._scores(0.30, 0.20, 0.40))["decision"] == "conflict"

    def test_no_difference(self):
        d = X.decide(self._scores(0.20, 0.20, 0.20))
        assert d["decision"] == "no_difference"
        assert X.ALPHA == pytest.approx(0.025) and d["alpha"] == X.ALPHA


class TestCandidateTable:
    def test_every_candidate_is_kept(self):
        """全候補の期別系列を持つ（上位 20 件に限らない・検証 2）。"""
        n = 150
        combos = [{"x": i} for i in range(n)]
        base = [{"ic": {"2021-02": 0.1}, "top": {"2021-02": 0.2}} for _ in range(n)]
        infos = [{"elapsed_sec": 1.0} for _ in range(n)]
        table = X.candidate_table(combos, base, infos)
        assert len(table) == n and table[-1]["params"] == {"x": n - 1} and table[-1]["ic"] == {"2021-02": 0.1}


class TestSearchArgs:
    def test_read_from_the_production_search_job(self):
        assert X.search_args(rd.JOBS[X.SEARCH_JOB].argv) == {
            "strategy": "random", "n_iter": 150, "seed": 0, "confirm_top": 5, "confirm_seeds": 5}

    def test_missing_flag_raises(self):
        with pytest.raises(ValueError, match="--confirm-seeds"):
            X.search_args(["--strategy", "random", "--n-iter", "1", "--seed", "0", "--confirm-top", "5"])


# ── 本番を変えない ──────────────────────────────────────────────────────────────

class TestPredictionsWithParams:
    """任意の設定の M-2 を、本番の μ̂ と探索の結果を上書きしない内側で回す（検証 4）。"""

    TARGET = ["2022-09", "2022-12", "2023-03"]

    def test_runs_inside_dry_run_and_returns_its_own_oof(self, monkeypatch):
        import plugins.macro_gbdt as m2mod
        calls = []
        # 2021-03〜2023-05 の 27 か月。最初の学習期間を延ばすと test 月は 2022-09・2022-12・2023-03 になる
        own = [f"{2021 + (k + 2) // 12}-{(k + 2) % 12 + 1:02d}" for k in range(27)]

        class FakeM2:
            def params_schema(self):
                return {}

            def execute(self, params, db):
                calls.append((dict(params), database.is_tuning_objective_only(), database._tuning_dry_run.get()))
                rng = random.Random(0)
                samples = {ym: [([rng.random()], rng.gauss(0, 0.1)) for _ in range(20)] for ym in own}
                _f, resid = m2mod.walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                                          return_residuals=True, embargo_months=12)
                meta = {ym: [(f"E{j:02d}", "ind") for j in range(len(rows))] for ym, rows in resid.items()}
                m2mod.oof_backtest(resid, n_quantiles=5, meta_by_ym=meta, rebalance_per_year=4)
                return {"oof_backtest": {"rank_ic_by_period": {"2022-09": 0.1}}}

        monkeypatch.setattr(op, "get_plugin", lambda name: FakeM2())
        monkeypatch.setattr(op, "coerce_params", lambda schema, params: dict(params))
        preds, oof, info = op.predictions_with_params(({}, {}, {}, {}), None, self.TARGET, {"max_depth": 4})
        assert sorted({ym for ym, _ in preds}) == self.TARGET
        assert oof == {"rank_ic_by_period": {"2022-09": 0.1}}
        assert info["n_periods_matched"] == 3
        params, objective_only, dry_run = calls[0]
        assert params == {"max_depth": 4} and objective_only and dry_run

    def test_extra_m2_rows_in_the_replay(self, monkeypatch):
        names = []

        def buy(name, pred, y, prices_by_co, db):
            names.append(name)
            assert set(pred) <= set(y), "共通域の外の鍵が買いの並びへ渡った"
            return dict(pred), {"lambda": 0.0}

        class Book:
            def __init__(self, _p):
                pass

            def period_return(self, ec, ym0, ym1):
                return 0.01, False

        monkeypatch.setattr(op, "buy_scores", buy)
        monkeypatch.setattr(op, "PriceBook", Book)
        yms = ["2021-06", "2021-09", "2021-12"]
        rng = random.Random(3)
        common = {ym: [(f"E{i:02d}", (rng.random(), rng.random(), rng.random()), 0.0) for i in range(8)]
                  for ym in yms}
        extra = {(ym, f"E{i:02d}"): float(i) for ym in yms for i in range(8)}
        extra[("2021-06", "ZZ")] = 9.0
        res = op.run_replays(common, ["macro_risk_return", "macro_gbdt", "macro_enet"], {}, None, ["macro_enet"],
                             "macro_enet", 3, 1, 10.0, extra_m2={"time_ordered": extra})
        assert list(res) == ["macro_enet", "time_ordered"]
        assert names == ["macro_enet", op.PRODUCTION_MODEL], "追加の行は M-2 の並び方で買う"
        assert res["time_ordered"]["coverage"] == {"n_rows": 24, "n_outside_common": 1,
                                                   "n_common_without_prediction": 0}
        assert res["time_ordered"]["intervals"][0]["holdings"] == ["E05", "E06", "E07"]


# ── 事前登録と日中キュー ────────────────────────────────────────────────────────

class TestPrereg:
    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        import collector_utils
        monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)
        monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)

    def test_document_records_its_frozen_value(self):
        assert prereg.recorded_sha(prereg.read_document(X.PREREG)) == SHA

    def test_sha_mismatch_exits_2_before_loading(self):
        with pytest.raises(SystemExit) as e:
            X.main(["--prereg", X.PREREG, "--prereg-sha", "f" * 64])
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_other_document_exits_2(self):
        other = "docs/prereg/label-audit.md"
        sha = prereg.body_sha256(prereg.read_document(other))
        assert X.main(["--prereg", other, "--prereg-sha", sha]) == prereg.EXIT_MISMATCH

    def test_issue_is_1155(self):
        assert prereg.issue_url(prereg.read_document(X.PREREG)).endswith("/issues/1155")


class TestDaytimeJob:
    def test_job_passes_the_frozen_document(self):
        job = rd.JOBS["measure:selection-optimism"]
        argv = list(job.argv)
        assert job.name == "selection_optimism" and job.parallel_sensitive is True
        assert argv[argv.index("--prereg") + 1] == X.PREREG
        assert argv[argv.index("--prereg-sha") + 1] == SHA
        assert "--smoke" not in argv
        assert job.measured_min <= rd.JOB_BUDGET_MIN

    def test_job_argv_is_accepted_by_the_script(self):
        argv = rd.JOBS["measure:selection-optimism"].argv
        assert argv[1:3] == ("-m", "scripts.measure_selection_optimism")
        args = X.build_parser().parse_args(list(argv[3:]))
        assert args.allow_full_pull and args.refresh_cache and not args.smoke
