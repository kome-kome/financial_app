"""上場廃止した行を採点に足す計測（#1197・親 #1195）: `build_snapshots(return_no_future_rows=True)` と
`scripts.measure_delisting_redesign`。

DB と M-4 は回さず、合成の株価・財務・予想で確かめる。Issue の「検証」の項目に対応する:
消えた行が 0 件なら採点は既存の値とビット一致する / 消えた行にも予想が出て、学習には入らない /
件数が合わなければ止まる / 凍結値が合わなければ終了コード 2 / 既定の `build_snapshots` の戻り値は変わらない。
"""
import math
import random
from collections import namedtuple
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

import plugins.macro_snapshots as ms
import scripts.measure_delisting_redesign as X
import scripts.run_daytime as rd
from plugins.macro_gbdt import _make_xgb_fit_predict
from plugins.model_candidates import make_elasticnet_fit_predict
from plugins.utils import walk_forward_cv_monthly
from scripts import prereg

SHA = prereg.body_sha256(prereg.read_document(X.PREREG))

Fin = namedtuple("Fin", "period_end industry sec_code company_name bs_total_assets per")
START = date(2020, 1, 6)   # 月曜


def _weeks(n: int, start: date = START) -> list[str]:
    return [(start + timedelta(weeks=i)).isoformat() for i in range(n)]


def _series(dates: list[str], closes: list[float]) -> list:
    return [ms._WEEKLY_PX(d, float(c), None) for d, c in zip(dates, closes)]


def _rising(n: int, base: float = 100.0, step: float = 0.5) -> list[float]:
    return [base + step * i for i in range(n)]


def _fin(per: float = 10.0) -> list:
    return [Fin(date(2019, 12, 31), "製造業", "1000", "合成", 1e9, per)]


def _build(prices: dict, **kw):
    fin = {ec: _fin(10.0 + i) for i, ec in enumerate(sorted(prices))}
    return ms._build_snapshots_impl(
        prices, fin, {}, {}, ["per"], [], False, 12, 0.0,
        build_interactions=False, macro_nan_ok=False, return_stock_ids=True, price_features=[], **kw)


def _panel() -> dict:
    """E_A: 全期間（150 週）続く。E_B: 90 週で終わる（上場廃止）。"""
    return {
        "E_A": _series(_weeks(150), _rising(150)),
        "E_B": _series(_weeks(90), _rising(90, base=50.0)),
    }


# ── build_snapshots の return_no_future_rows ────────────────────────────────────

class TestReturnNoFutureRows:
    def test_default_and_no_future_shapes_are_unchanged(self):
        prices = _panel()
        base = _build(prices)
        nf = _build(prices, return_no_future=True)
        rows = _build(prices, return_no_future=True, return_no_future_rows=True)
        assert (len(base), len(nf), len(rows)) == (5, 6, 7)
        assert rows[:6] == nf and nf[:5] == base

    def test_rows_match_the_listed_codes_and_carry_the_features(self):
        prices = _panel()
        *_head, no_future, rows = _build(prices, return_no_future=True, return_no_future_rows=True)
        assert set(rows) == set(no_future)
        for ym, ecs in no_future.items():
            assert [r[0] for r in rows[ym]] == ecs
            for ec, feat, industry, size in rows[ym]:
                # 答えのある行と同じ特徴量の並び（ここでは per だけ）・業種・規模
                assert feat == [10.0 + sorted(prices).index(ec)]
                assert industry == "製造業" and size == 1e9

    def test_rows_require_no_future(self):
        with pytest.raises(ValueError, match="return_no_future"):
            ms.build_snapshots({}, {}, {}, {}, ["per"], [], False, 12, 0.0,
                               build_interactions=False, return_no_future_rows=True)

    def test_cache_key_distinguishes_the_flag(self, monkeypatch):
        mock_impl = MagicMock(return_value=({}, {}, {}, []))
        monkeypatch.setattr(ms, "_build_snapshots_impl", mock_impl)
        with ms.shared_snapshot_cache():
            ms.build_snapshots({}, {}, {}, {}, ["per"], [], False, 12, 1.0,
                               build_interactions=False, return_no_future=True)
            ms.build_snapshots({}, {}, {}, {}, ["per"], [], False, 12, 1.0,
                               build_interactions=False, return_no_future=True, return_no_future_rows=True)
        assert mock_impl.call_count == 2
        assert mock_impl.call_args.kwargs["return_no_future_rows"] is True


# ── 消えた行の選び方 ────────────────────────────────────────────────────────────

class TestSelectEnded:
    def test_only_rows_whose_series_ended_are_kept(self):
        prices = _panel()
        rows = _build(prices, return_no_future=True, return_no_future_rows=True)[-1]
        keep, info, counts = X.select_ended(rows, prices, X.global_last_week(prices))
        assert keep and all(ec == "E_B" for ecs in keep.values() for ec, _f in ecs)
        assert counts.get("recent", 0) > 0          # 全期間続く E_A の直近の行は足さない
        for (ym, ec), c in info.items():
            assert c["kind"] == "ended"
            dates = [r.trade_date for r in prices[ec]]
            closes = [r.close_last for r in prices[ec]]
            snap = next(i for i in ms.month_end_indices(dates) if dates[i][:7] == ym)
            assert c["r_last"] == closes[-1] / closes[snap] - 1.0     # 最後の終値で手仕舞い


# ── 消えた行にも予想を出す ──────────────────────────────────────────────────────

def _synthetic_samples(n_months: int = 30, n_rows: int = 14, n_feat: int = 3, seed: int = 7):
    rng = random.Random(seed)
    samples, extras = {}, {}
    for m in range(n_months):
        ym = f"{2018 + m // 12}-{m % 12 + 1:02d}"
        rows = []
        for _ in range(n_rows):
            x = [rng.gauss(0, 1) for _ in range(n_feat)]
            rows.append((x, 0.3 * x[0] - 0.2 * x[1] + rng.gauss(0, 0.5)))
        samples[ym] = rows
        if m % 2 == 0:
            extras[ym] = [(f"X{m}_{j}", [rng.gauss(0, 1) for _ in range(n_feat)]) for j in range(2)]
    return samples, extras, [f"f{i}" for i in range(n_feat)]


def _xgb_factory(best: list | None = None):
    params = {"n_estimators": 30, "early_stopping_rounds": 5, "max_depth": 2, "learning_rate": 0.1,
              "tree_method": "hist", "objective": "reg:squarederror", "random_state": 42}
    return _make_xgb_fit_predict(params, best if best is not None else [])


def _enet_factory():
    return make_elasticnet_fit_predict(n_alphas=5, cv_splits=3, max_iter=2000)


def _walk(samples, feats, fp):
    return walk_forward_cv_monthly(samples, feats, min_train_months=6, step_months=3,
                                   return_residuals=True, fit_predict=fp, embargo_months=2)


class TestExtraPredictor:
    @pytest.mark.parametrize("make", [_xgb_factory, _enet_factory], ids=["xgb", "enet"])
    def test_labelled_predictions_are_bit_identical(self, make):
        """消えた行を test の後ろに連結しても、答えのある行の予想は 1 ビットも変わらない。"""
        samples, extras, feats = _synthetic_samples()
        _f0, resid0 = _walk(samples, feats, make())
        ex = X.ExtraPredictor()
        ex.set_build(samples, extras)
        _f1, resid1 = _walk(samples, feats, ex.wrap("macro_gbdt", lambda: make())())
        assert resid1 == resid0 and resid0
        assert not ex.errors and not ex.unmapped
        for ym in resid0:
            assert ex.raw["macro_gbdt"][ym] == [yh for yh, _y in resid0[ym]]
            for ec, _feat in extras.get(ym, []):
                assert isinstance(ex.extra["macro_gbdt"][(ym, ec)], float)
        # 予想が出るのは fold の月だけ
        assert {ym for ym, _ec in ex.extra["macro_gbdt"]} == set(resid0) & set(extras)

    def test_extras_are_predicted_but_not_trained(self):
        samples, extras, feats = _synthetic_samples()
        seen = []

        def spy_factory():
            inner = _enet_factory()

            def fp(train, test, *rest):
                seen.append((len(train), len(test), list(test[len(test) - 2:])))
                return inner(train, test, *rest)
            return fp

        ex = X.ExtraPredictor()
        ex.set_build(samples, extras)
        _walk(samples, feats, ex.wrap("macro_enet", spy_factory)())
        extra_feats = {tuple(f) for rows in extras.values() for _ec, f in rows}
        for n_train, n_test, _tail in seen:
            assert n_train % 14 == 0                # 学習は答えのある行（各月 14 行）だけ
            assert n_test in (14, 16)               # test は答えのある行＋その月の消えた行
        assert any(tuple(s[0]) in extra_feats for _a, n, tail in seen if n == 16 for s in tail)

    def test_count_mismatch_is_recorded_and_the_fold_is_dropped(self):
        samples, extras, feats = _synthetic_samples()

        def bad_factory():
            inner = _enet_factory()

            def fp(train, test, *rest):
                yhat, y = inner(train, test, *rest)
                return yhat[:-1], y[:-1]
            return fp

        ex = X.ExtraPredictor()
        ex.set_build(samples, extras)
        _walk(samples, feats, ex.wrap("macro_enet", bad_factory)())
        assert ex.errors and "件数" in ex.errors[0]

    def test_unmapped_test_list_is_counted(self):
        samples, extras, feats = _synthetic_samples()
        ex = X.ExtraPredictor()
        ex.set_build({ym: list(rows) for ym, rows in samples.items()}, extras)   # 別の list の実体
        _walk(samples, feats, ex.wrap("macro_enet", _enet_factory)())
        assert ex.unmapped["macro_enet"] > 0 and not ex.extra["macro_enet"]


# ── M-6 の規模の中立化 ──────────────────────────────────────────────────────────

class TestNeutralizeExtras:
    RAW = {"2024-02": [0.10, 0.25, 0.30, 0.42]}
    IDS = {"2024-02": ["A", "B", "C", "D"]}
    LAB = {("2024-02", "A"): 1.0, ("2024-02", "B"): 2.0, ("2024-02", "C"): 3.5}   # D は時価総額なし

    def _want(self):
        xs = [self.LAB.get(("2024-02", ec)) for ec in self.IDS["2024-02"]]
        adj, _slope, _n = ms.neutralize_size(self.RAW["2024-02"], xs)
        return dict(zip((("2024-02", ec) for ec in self.IDS["2024-02"]), adj))

    def test_extras_get_the_same_slope_as_the_labelled_rows(self):
        extra = {("2024-02", "E"): 0.5, ("2024-02", "F"): 0.6}
        ext_x = {("2024-02", "E"): 4.0}                                           # F は時価総額なし
        out, meta = X.neutralize_extras(self.RAW, extra, self.IDS, self.LAB, ext_x, self._want())
        slope, mean_x, _my = ms.size_slope([0.10, 0.25, 0.30], [1.0, 2.0, 3.5])
        assert meta["2024-02"]["n_checked"] == 4
        assert out[("2024-02", "E")] == 0.5 - slope * (4.0 - mean_x)
        assert out[("2024-02", "F")] == 0.6                                       # x が無ければ補正しない

    def test_unreproducible_neutralization_stops(self):
        want = self._want()
        want[("2024-02", "B")] += 1e-6
        with pytest.raises(X.InputMismatch, match="再現できない"):
            X.neutralize_extras(self.RAW, {}, self.IDS, self.LAB, {}, want)

    def test_misaligned_rows_stop(self):
        with pytest.raises(X.InputMismatch, match="合わない"):
            X.neutralize_extras(self.RAW, {}, {"2024-02": ["A", "B"]}, self.LAB, {}, {})


# ── 判定 ────────────────────────────────────────────────────────────────────────

def _scores(diff: float, top_diff: float, seed: int = 3) -> dict:
    """M-6 と M-2 の期別系列（M-6 − M-2 がおおむね diff・上位10社超過の差が top_diff）。"""
    rng = random.Random(seed)
    yms = [f"{2021 + i // 4}-{(i % 4) * 3 + 2:02d}" for i in range(19)]
    a, b = {}, {}
    for name, shift, tk in (("macro_enet", diff, top_diff), ("macro_gbdt", 0.0, 0.0)):
        ric = {ym: 0.15 + shift + rng.gauss(0, 0.005) for ym in yms}
        ssp = {ym: 0.08 + shift + rng.gauss(0, 0.005) for ym in yms}
        top = {ym: 0.05 + tk + rng.gauss(0, 0.005) for ym in yms}
        (a if name == "macro_enet" else b).update(
            {"rank_ic_by_period": ric, "short_side_spread_by_period": ssp,
             "top_k_excess_return_by_period": {str(X.TOP_K): top}})
    return {"macro_enet": a, "macro_gbdt": b}


class TestIncludeDecision:
    def test_same_verdicts_keep(self):
        d = X.include_decision(_scores(0.05, 0.02), _scores(0.05, 0.02, seed=4))
        assert d["decision"] == "keep" and not d["flip_primary"] and not d["flip_top_k"]

    def test_flipped_primary_verdict_proposes_inclusion(self):
        d = X.include_decision(_scores(0.05, 0.02), _scores(0.0, 0.02))
        assert d["flip_primary"] and d["decision"] == "propose_include"

    def test_flipped_top_k_sign_proposes_inclusion(self):
        d = X.include_decision(_scores(0.05, 0.02), _scores(0.05, -0.02))
        assert d["flip_top_k"] and d["decision"] == "propose_include"


# ── 本体（M-4 を回さずに、捕まえた形を渡す）──────────────────────────────────────

def _fake_run(prices: dict):
    """答えのある行を共通域に並べた M-4 の戻り値と、それに合う ExtraPredictor・captured を作る。"""
    s, _meta, _cur, _feats, ids, _nf, rows = _build(prices, return_no_future=True, return_no_future_rows=True)
    keep, info, counts = X.select_ended(rows, prices, X.global_last_week(prices))
    common, raw = {}, {"macro_gbdt": {}, "macro_enet": {}}
    for k, ym in enumerate(sorted(s)):
        for (feat, yt), ec in zip(s[ym], ids[ym]):
            yh = 0.01 * feat[0] + 0.001 * k
            common.setdefault(ym, []).append((ec, (yh, yh * 1.1, yh * 0.9), yt))
        raw["macro_gbdt"][ym] = [r[1][1] for r in common[ym]]
        raw["macro_enet"][ym] = [r[1][2] for r in common[ym]]
    ex = X.ExtraPredictor()
    ex.raw.update(raw)
    for ym, ecs in keep.items():
        if ym in common:
            for ec, feat in ecs:
                ex.extra["macro_gbdt"][(ym, ec)] = 0.02 * feat[0]
                ex.extra["macro_enet"][(ym, ec)] = 0.03 * feat[0]
    from scripts.sell_mu_source_bakeoff import common_to_dicts
    preds, y = common_to_dicts(common, X.BASES)
    order = {ym: [ec for ec, _yh, _y in rows_] for ym, rows_ in common.items()}
    base_oof = X.score_all(preds, order, y)
    run = {"bases": list(X.BASES), "common": common, "base_oof": base_oof}
    captured = {"m6_build": {"ids": ids, "fin_by_co": {}, "market_caps": {}},
                "ended_info": info, "kind_counts": counts}
    return run, ex, captured


class TestBuildReport:
    def test_report_adds_only_ended_rows_and_decides(self):
        prices = {f"E_{i}": _series(_weeks(150), _rising(150, base=100.0 + 7 * i, step=0.3 + 0.05 * i))
                  for i in range(6)}
        prices["E_X"] = _series(_weeks(90), _rising(90, base=40.0, step=-0.2))
        run, ex, captured = _fake_run(prices)
        report = X.build_report(run, ex, captured, prices)
        assert report["added"]["n"] > 0
        assert set(report["added"]["by_reason"]) == set(X.REASONS)
        assert report["prereg_decision"]["decision"] in ("keep", "propose_include")
        assert set(report["scores"]) == {"base", "with_ended", "with_ended_bankrupt_minus_99",
                                         "covered_base", "covered_with_ended"}

    def test_no_ended_rows_scores_bit_identical(self):
        prices = {f"E_{i}": _series(_weeks(150), _rising(150, base=100.0 + 7 * i, step=0.3 + 0.05 * i))
                  for i in range(6)}
        run, ex, captured = _fake_run(prices)
        report = X.build_report(run, ex, captured, prices)
        assert report["added"]["n"] == 0
        assert report["scores"]["with_ended"] == report["scores"]["base"]
        assert report["prereg_decision"]["decision"] == "keep"

    def test_prediction_errors_stop(self):
        prices = _panel()
        run, ex, captured = _fake_run(prices)
        ex.errors.append("M-6 2021-02: ValueError: x")
        with pytest.raises(X.InputMismatch, match="失敗"):
            X.build_report(run, ex, captured, prices)

    def test_differing_extras_between_models_stop(self):
        prices = {f"E_{i}": _series(_weeks(150), _rising(150, base=100.0 + 7 * i)) for i in range(4)}
        prices["E_X"] = _series(_weeks(90), _rising(90, base=40.0))
        run, ex, captured = _fake_run(prices)
        key = next(iter(ex.extra["macro_gbdt"]))
        del ex.extra["macro_gbdt"][key]
        with pytest.raises(X.InputMismatch, match="違う"):
            X.build_report(run, ex, captured, prices)


# ── 事前登録と日中キュー ────────────────────────────────────────────────────────

class TestPrereg:
    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        import collector_utils
        monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)
        # 標準出力の差し替えは pytest のキャプチャを壊す（force_utf8_stdout の docstring・test_label_audit と同じ）
        monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)

    def test_wrong_sha_exits_2(self):
        with pytest.raises(SystemExit) as e:
            X.main(["--prereg", X.PREREG, "--prereg-sha", "f" * 64])
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_other_document_exits_2(self):
        other = "docs/prereg/label-audit.md"
        sha = prereg.body_sha256(prereg.read_document(other))
        assert X.main(["--prereg", other, "--prereg-sha", sha]) == prereg.EXIT_MISMATCH

    def test_issue_is_1197(self):
        assert prereg.issue_url(prereg.read_document(X.PREREG)).endswith("/issues/1197")

    def test_recorded_sha_matches_the_body(self):
        text = prereg.read_document(X.PREREG)
        assert prereg.recorded_sha(text) == SHA


class TestDaytimeJob:
    def test_job_passes_the_frozen_document(self):
        job = rd.JOBS["measure:delisting-redesign"]
        argv = list(job.argv)
        assert job.name == "delisting_redesign" and job.parallel_sensitive is True
        assert argv[1:3] == ["-m", "scripts.measure_delisting_redesign"]
        assert argv[argv.index("--prereg") + 1] == X.PREREG
        assert argv[argv.index("--prereg-sha") + 1] == SHA
        assert "--smoke" not in argv
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN


def test_exit_label_is_the_log_of_the_last_close():
    """答えは log(最後の終値 / スナップ時の終値)＝ log(1 + r_last)。"""
    assert math.isclose(math.log(1.0 + (40.0 / 50.0 - 1.0)), math.log(40.0 / 50.0))
