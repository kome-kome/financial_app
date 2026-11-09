"""OOF 運用再現（Issue #1045・ADR-0069 の参考）のユニットテスト。

`scripts/oof_portfolio_replay.py` の入れ替えの再現・期間リターン・指標と、引数・日中キューの登録を
合成の検体で確かめる。M-4 は回さない（共通域の取り出しは tests/test_sell_mu_source_bakeoff.py）。
"""
import math
import random
from collections import namedtuple

import pytest

import database
import plugins.macro_gbdt as m2mod
import scripts.oof_portfolio_replay as op
import scripts.run_daytime as rd

PX = namedtuple("PX", "trade_date close_last volume_sum")
P1, P2, P3 = "2020-01", "2020-04", "2020-07"


def _months(year: int, month: int, n: int) -> list[str]:
    """year-month から n か月の "YYYY-MM"。"""
    return [f"{year + (month - 1 + k) // 12:04d}-{(month - 1 + k) % 12 + 1:02d}" for k in range(n)]


def _table_ret(table):
    """{(社, ym0, ym1): リターン or (リターン, 途切れたか)} を ret の形へ。"""
    def ret(ec, ym0, ym1):
        v = table.get((ec, ym0, ym1))
        if v is None:
            return None, False
        return v if isinstance(v, tuple) else (v, False)
    return ret


class TestReplayByHand:
    """K=3・k=1・6 社・3 期の手計算の検体。

    P1: 買いの並び A>B>C>… → 保有 {A, B, C}（初回の買い 3 社）
    P2: C は予測が無い（強制で外す）。残り {A, B} のうち売りのキーが低い A を売る。買いの並び D>E>A>F>B
        から、売った A と残した B を除いて D・E を補充 → 保有 {B, D, E}
    P3: 最後の期なので入れ替えない
    """
    BUY = {**{(P1, ec): s for ec, s in zip("ABCDEF", [6, 5, 4, 3, 2, 1])},
           **{(P2, ec): s for ec, s in zip("ABDEF", [7, 5, 9, 8, 1])}}
    SELL = {**{(P1, ec): 0.0 for ec in "ABCDEF"},
            **{(P2, ec): s for ec, s in zip("ABDEF", [0.1, 0.5, 0.3, 0.2, 0.4])}}
    RET = {
        ("A", P1, P2): 0.10, ("B", P1, P2): 0.20, ("C", P1, P2): (-0.30, True),
        ("D", P1, P2): 0.00, ("E", P1, P2): 0.05, ("F", P1, P2): -0.05,
        ("A", P2, P3): 0.00, ("B", P2, P3): 0.10, ("D", P2, P3): 0.30,
        ("E", P2, P3): -0.10, ("F", P2, P3): 0.20,
    }

    def _run(self, cost_bps=100.0):
        return op.replay([P1, P2, P3], self.BUY, self.SELL, _table_ret(self.RET), 3, 1, cost_bps)

    def test_holdings_follow_the_rule(self):
        iv1, iv2 = self._run()
        assert (iv1["holdings"], iv1["sold"], iv1["forced"], iv1["bought"]) == (
            ["A", "B", "C"], [], [], ["A", "B", "C"])
        assert (iv2["holdings"], iv2["sold"], iv2["forced"], iv2["bought"]) == (
            ["B", "D", "E"], ["A"], ["C"], ["D", "E"])

    def test_period_returns_and_costs_match_hand_calculation(self):
        iv1, iv2 = self._run(cost_bps=100.0)
        assert iv1["gross"] == pytest.approx((0.10 + 0.20 - 0.30) / 3)
        assert iv1["cost"] == pytest.approx(0.01 * 3 / 3)          # 初回の買い 3 社
        assert iv1["net"] == pytest.approx(iv1["gross"] - 0.01)
        assert iv1["bench"] == pytest.approx((0.10 + 0.20 - 0.30 + 0.0 + 0.05 - 0.05) / 6)
        assert iv1["n_stopped"] == 1 and iv1["months"] == 3
        assert iv2["gross"] == pytest.approx((0.10 + 0.30 - 0.10) / 3)
        assert iv2["cost"] == pytest.approx(0.01 * (1 + 1 + 2) / 3)   # 売り A・強制 C・買い D,E
        assert iv2["bench"] == pytest.approx((0.0 + 0.10 + 0.30 - 0.10 + 0.20) / 5)

    def test_summary_matches_hand_calculation(self):
        ivs = self._run(cost_bps=100.0)
        s = op.summarize(ivs, 3)
        years = 6 / 12
        nets = [iv["net"] for iv in ivs]
        assert s["annual_net"] == pytest.approx(math.prod(1 + r for r in nets) ** (1 / years) - 1)
        assert s["turnover"] == pytest.approx((1 + 1) / 3)           # 2 回目の入れ替えだけ
        assert s["annual_excess"] == pytest.approx(s["annual_net"] - s["annual_bench"])
        assert s["n_stopped"] == 1


class TestSign:
    """予測が完全に当たる検体で超過が正、逆順で負。"""

    @staticmethod
    def _world(seed=0, n=40, periods=("2020-01", "2020-04", "2020-07", "2020-10", "2021-01", "2021-04")):
        rng = random.Random(seed)
        future = {(ym, f"E{i:02d}"): rng.gauss(0.02, 0.10)
                  for ym in periods[:-1] for i in range(n)}
        table = {(ec, ym, nxt): future[(ym, ec)]
                 for ym, nxt in zip(periods, periods[1:]) for (y, ec) in future if y == ym}
        last = {(periods[-1], f"E{i:02d}"): 0.0 for i in range(n)}
        return list(periods), future, last, _table_ret(table)

    def test_perfect_foresight_beats_the_average_and_reverse_loses(self):
        periods, future, last, ret = self._world()
        good = op.summarize(op.replay(periods, {**future, **last}, {**future, **last}, ret, 5, 1, 0.0), 5)
        bad_scores = {k: -v for k, v in {**future, **last}.items()}
        bad = op.summarize(op.replay(periods, bad_scores, bad_scores, ret, 5, 1, 0.0), 5)
        assert good["annual_excess"] > 0
        assert bad["annual_excess"] < 0

    def test_higher_cost_lowers_net_monotonically(self):
        periods, future, last, ret = self._world(seed=1)
        scores = {**future, **last}
        nets = [op.summarize(op.replay(periods, scores, scores, ret, 5, 1, c), 5)["annual_net"]
                for c in (0.0, 10.0, 50.0, 200.0)]
        assert nets == sorted(nets, reverse=True) and len(set(nets)) == 4
        gross = {op.summarize(op.replay(periods, scores, scores, ret, 5, 1, c), 5)["annual_gross"]
                 for c in (0.0, 200.0)}
        assert len(gross) == 1                                       # コスト前は動かない


class TestMetrics:
    def test_max_drawdown(self):
        assert op.max_drawdown([0.1, -0.2, 0.05]) == pytest.approx(1 - 0.88 / 1.1)
        assert op.max_drawdown([0.1, 0.1]) == 0.0

    def test_annualized_uses_elapsed_months(self):
        """fold が抜けて 6 か月空いた区間も、実際の経過月数で年率にする。"""
        ivs = [{"months": 3, "gross": 0.1, "net": 0.1, "bench": 0.0, "sold": [], "forced": [],
                "n_stopped": 0, "n_missing": 0},
               {"months": 6, "gross": 0.1, "net": 0.1, "bench": 0.0, "sold": ["X"], "forced": [],
                "n_stopped": 0, "n_missing": 0}]
        s = op.summarize(ivs, 2)
        assert s["years"] == pytest.approx(0.75)
        assert s["annual_net"] == pytest.approx(1.21 ** (1 / 0.75) - 1)
        assert s["turnover"] == pytest.approx(0.5)

    def test_months_between(self):
        assert op.months_between("2020-11", "2021-02") == 3


class TestPriceBook:
    ROWS = {"A": [PX("2020-01-10", 100.0, None), PX("2020-01-31", 110.0, None),
                  PX("2020-03-27", 121.0, None)],
            "B": [PX("2020-01-31", 0.0, None), PX("2020-02-28", 50.0, None)]}

    def test_month_end_close_and_stopped_fallback(self):
        book = op.PriceBook(self.ROWS)
        assert book.close_at("A", "2020-01") == (110.0, False)     # 月末の週の終値
        assert book.close_at("A", "2020-02") == (110.0, True)      # 2 月は足が無い＝途切れた
        assert book.close_at("A", "2020-03") == (121.0, False)
        assert book.close_at("A", "2019-12") == (None, False)

    def test_period_return(self):
        book = op.PriceBook(self.ROWS)
        assert book.period_return("A", "2020-01", "2020-03") == (pytest.approx(0.1), False)
        assert book.period_return("A", "2020-01", "2020-02") == (pytest.approx(0.0), True)
        # 始めの月末の終値が 0（無効）なら測らない
        assert book.period_return("B", "2020-01", "2020-02") == (None, False)


class TestAlignedMinTrain:
    """単体の M-2 は共通域より早く始まる。最初の学習期間を延ばすと test 月が共通域とそろう（#1123）。"""

    MT, EMB, STEP = 6, 12, 3

    def test_shifted_test_months_match_the_common_grid(self):
        own = _months(2019, 11, 72)                 # 単体の M-2 の月
        common = own[1:]                            # M-1 が 1 か月遅く始まる共通域
        common_tests = common[self.MT + self.EMB::self.STEP]
        assert not set(common_tests) & set(own[self.MT + self.EMB::self.STEP]), \
            "検体の前提: そろえないと 1 期も重ならない"
        m = op.aligned_min_train(own, common_tests[0], self.MT, self.EMB)
        assert m == self.MT + 1
        assert own[m + self.EMB::self.STEP] == common_tests

    def test_cannot_align(self):
        own = _months(2020, 1, 40)
        assert op.aligned_min_train(own, "2030-01", self.MT, self.EMB) is None    # 共通域の月が無い
        assert op.aligned_min_train(own, own[10], self.MT, self.EMB) is None      # 本番の最小より短くなる
        assert op.aligned_min_train(own, own[18], self.MT, self.EMB) == self.MT   # ずれていなければそのまま


class TestProductionRow:
    """本番の設定の M-2 の行（#1123）: 既定の M-2 の直後に印付きで足し、共通域の外の鍵では落ちない。"""

    BASES = ["macro_risk_return", "macro_gbdt", "macro_enet"]
    YMS = ["2021-06", "2021-09", "2021-12"]

    def _common(self):
        rng = random.Random(3)
        return {ym: [(f"E{i:02d}", (rng.random(), rng.random(), rng.random()), rng.gauss(0, 0.1))
                     for i in range(8)] for ym in self.YMS}

    def _patch(self, monkeypatch):
        def buy(name, pred, y, prices_by_co, db):
            # 本物の buy_scores は y[(ym, 社)] を引く＝共通域の外の鍵が来ると KeyError で止まる
            assert set(pred) <= set(y), "共通域の外の鍵が買いの並びへ渡った"
            return dict(pred), {"lambda": 0.0, "risk_axis": None, "time_reproducible": True,
                                "n_dropped_rows": 0}

        class Book:
            def __init__(self, _prices):
                pass

            def period_return(self, ec, ym0, ym1):
                return 0.01, False

        monkeypatch.setattr(op, "buy_scores", buy)
        monkeypatch.setattr(op, "PriceBook", Book)

    def _run(self, production=None):
        return op.run_replays(self._common(), self.BASES, {}, None, list(self.BASES), "macro_enet",
                              3, 1, 10.0, production=production)

    def test_the_production_row_follows_the_default_m2(self, monkeypatch):
        self._patch(monkeypatch)
        prod = {(ym, f"E{i:02d}"): float(i) for ym in self.YMS for i in range(6)}
        prod[("2021-03", "E00")] = 1.0              # 単体の M-2 にだけある月
        prod[("2021-06", "ZZ")] = 1.0               # 共通域に無い社
        res = self._run(prod)
        assert list(res) == ["macro_risk_return", "macro_gbdt", op.PRODUCTION_KEY, "macro_enet"]
        row = res[op.PRODUCTION_KEY]
        assert row["selection_optimism"] is True
        assert row["coverage"] == {"n_rows": 18, "n_outside_common": 2, "n_common_without_prediction": 6}
        assert row["intervals"][0]["holdings"] == ["E03", "E04", "E05"], "本番の予測の上位 3 社を買う"
        assert "selection_optimism" not in res["macro_gbdt"]
        assert op._label(op.PRODUCTION_KEY) == "M-2*"

    def test_the_default_rows_do_not_change(self, monkeypatch):
        self._patch(monkeypatch)
        prod = {(ym, f"E{i:02d}"): float(i) for ym in self.YMS for i in range(8)}
        with_prod, without = self._run(prod), self._run()
        assert op.PRODUCTION_KEY not in without
        assert all(with_prod[name] == without[name] for name in self.BASES)


class TestProductionPredictions:
    """本番の設定の M-2 を回して予測を取り出す（#1123）。M-2 の中身は偽物で、横取りとそろえの経路を確かめる。"""

    OWN = _months(2019, 11, 40)                     # 単体の M-2 の月
    TARGET = OWN[1:][18::3]                         # 1 か月遅く始まる共通域の test 月
    CFG = ({"learning_rate": 0.01},
           {"params": {"learning_rate": 0.01}, "stale_params": [], "tuned_at": "2026-11-01T05:00:00+00:00",
            "panel_changed": None})

    def _setup(self, monkeypatch, tuned=True):
        calls = self.calls = []
        own = self.OWN

        class FakeM2:
            def params_schema(self):
                return {}

            def execute(self, params, db):
                calls.append((dict(params), database.is_tuning_objective_only(), database._tuning_dry_run.get()))
                rng = random.Random(0)
                samples = {ym: [([rng.random()], rng.gauss(0, 0.1)) for _ in range(20)] for ym in own}
                # 本番の M-2 と同じ呼び方（キーワード）。差し替えられた関数が最初の学習期間をずらす
                _folds, resid = m2mod.walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                                              return_residuals=True, embargo_months=12)
                meta = {ym: [(f"E{j:02d}", "ind") for j in range(len(rows))] for ym, rows in resid.items()}
                m2mod.oof_backtest(resid, n_quantiles=5, meta_by_ym=meta, rebalance_per_year=4)
                return {}

        none = (None, {"reason": "no_production_config", "error": "本番の設定が無い"})
        monkeypatch.setattr(op, "_production_config", lambda db, plugin, name, cache: self.CFG if tuned else none)
        monkeypatch.setattr(op, "get_plugin", lambda name: FakeM2())
        monkeypatch.setattr(op, "coerce_params", lambda schema, params: dict(params))

    def _run(self, test_yms):
        return op.production_predictions(({}, {}, {}, {}), None, test_yms)

    def test_predictions_land_on_the_common_test_months(self, monkeypatch):
        self._setup(monkeypatch)
        preds, info = self._run(self.TARGET)
        assert info["available"] is True and info["tuned_at"] == self.CFG[1]["tuned_at"]
        assert sorted({ym for ym, _ec in preds}) == self.TARGET
        assert info["n_periods_matched"] == info["n_periods_target"] == len(self.TARGET)
        assert len(preds) == 20 * len(self.TARGET)
        params, objective_only, dry_run = self.calls[0]
        assert params == self.CFG[0]
        assert objective_only and dry_run, "本番の μ̂ を上書きしない（objective_only・dry_run の内側で回す）"

    def test_without_a_tuned_row_only_the_reason_is_returned(self, monkeypatch):
        self._setup(monkeypatch, tuned=False)
        preds, info = self._run(self.TARGET)
        assert preds is None and info["available"] is False and info["reason"] == "no_production_config"
        assert self.calls == [], "本番の設定が無いのに回した"

    def test_months_that_cannot_be_aligned(self, monkeypatch):
        self._setup(monkeypatch)
        preds, info = self._run(["2030-03"])
        assert preds is None and info["available"] is False and info["reason"] == "not_aligned"


class TestCli:
    KEY = "measure:oof-replay"

    def test_job_argv_is_accepted_by_the_script(self):
        argv = rd.JOBS[self.KEY].argv
        assert argv[1:3] == ("-m", "scripts.oof_portfolio_replay")
        args = op.build_parser().parse_args(list(argv[3:]))
        assert args.allow_full_pull and args.refresh_cache and not args.smoke
        assert (args.top_k, args.sell_k, args.cost_bps) == (op.DEFAULT_TOP_K, op.DEFAULT_SELL_K,
                                                            op.DEFAULT_COST_BPS)

    def test_job_is_parallel_sensitive(self):
        assert rd.JOBS[self.KEY].parallel_sensitive is True

    def test_bad_k_stops_before_loading(self, monkeypatch):
        monkeypatch.setattr(op, "_load_panel", lambda *_a, **_k: pytest.fail("読み込む前に止まる"))
        assert op.main(["--top-k", "2", "--sell-k", "3"]) == 2

    def test_defaults_keep_one_path_and_no_bands(self):
        args = op.build_parser().parse_args([])
        assert args.start_shifts == (0,) and args.cost_bands_bps is None


# ── #1167: 入替の利得・信頼区間・帯ごとのコスト・開始月をずらした経路（どれも参考）──────────────

class TestSwapGain:
    def test_hand_calculation_excludes_forced_and_the_first_purchase(self):
        """TestReplayByHand の P2: 売った A（P2→P3 は 0.00）、買った D・E（+0.30・−0.10）。外した C は売った社に入れない。"""
        iv1, iv2 = TestReplayByHand()._run()
        assert iv1["swap_gain"] is None                              # 初回は買うだけ
        assert iv2["swap_gain"] == pytest.approx((0.30 - 0.10) / 2 - 0.00)
        s = op.summarize([iv1, iv2], 3)
        assert s["swap_gain_mean"] == pytest.approx(0.10) and s["n_swaps"] == 1
        assert s["swap_gain_ci"] is None                             # 1 回だけでは CI が出ない

    def test_sign_follows_the_foresight(self):
        periods, future, last, ret = TestSign._world(seed=2)
        good = {**future, **last}
        bad = {k: -v for k, v in good.items()}
        assert op.summarize(op.replay(periods, good, good, ret, 5, 1, 0.0), 5)["swap_gain_mean"] > 0
        assert op.summarize(op.replay(periods, bad, bad, ret, 5, 1, 0.0), 5)["swap_gain_mean"] < 0

    def test_existing_keys_keep_their_values(self):
        """足すのは新しいキーだけ。既定（帯なし）では帯のキーも出ない。"""
        iv1, iv2 = TestReplayByHand()._run()
        old = {"from", "to", "months", "holdings", "sold", "forced", "bought", "gross", "cost", "net", "bench",
               "n_domain", "n_stopped", "n_missing"}
        assert set(iv2) == old | {"swap_gain"}
        assert iv2["net"] == pytest.approx((0.10 + 0.30 - 0.10) / 3 - 0.01 * 4 / 3)


class TestConfidenceIntervals:
    def test_excess_ci_is_the_bootstrap_of_net_minus_bench(self):
        periods, future, last, ret = TestSign._world(seed=4)
        scores = {**future, **last}
        ivs = op.replay(periods, scores, scores, ret, 5, 1, 10.0)
        s = op.summarize(ivs, 5)
        assert s["excess_ci"] == op.bootstrap_mean_ci([iv["net"] - iv["bench"] for iv in ivs])
        assert s["excess_ci"]["n"] == len(ivs)

    def test_annual_ci_collapses_on_identical_intervals(self):
        """どの区間も同じなら、どう抜き直しても年率の超過は同じ＝区間の幅は 0 で点推定に一致する。"""
        iv = {"months": 3, "net": 0.03, "bench": 0.01}
        ivs = [dict(iv) for _ in range(6)]
        ci = op.annual_excess_ci(ivs)
        point = (1.03 ** 4 - 1) - (1.01 ** 4 - 1)
        assert ci["ci_lo"] == pytest.approx(point) and ci["ci_hi"] == pytest.approx(point)
        assert op.annual_excess_ci(ivs[:1]) is None

    def test_annual_ci_is_reproducible_and_ordered(self):
        periods, future, last, ret = TestSign._world(seed=5)
        scores = {**future, **last}
        ivs = op.replay(periods, scores, scores, ret, 5, 1, 10.0)
        a, b = op.annual_excess_ci(ivs), op.annual_excess_ci(ivs)
        assert a == b and a["ci_lo"] <= a["ci_hi"]


def _weeks(end: str, n: int) -> list:
    """end（月曜）から n 週さかのぼった月曜の列。"""
    from datetime import date, timedelta
    d = date.fromisoformat(end)
    return [d - timedelta(weeks=i) for i in range(n)]


class TestCostBands:
    def test_bands_are_tertiles_of_the_trailing_52_weeks(self):
        weeks = _weeks("2020-03-30", 60)                 # 2020-03 の月末以前の 60 週
        turnover = {f"E{i}": {w: float(i + 1) for w in weeks} for i in range(6)}
        turnover["SHORT"] = {w: 1000.0 for w in weeks[:5]}   # 13 週に満たない社は帯を付けない
        bands = op.turnover_bands(turnover, ["2020-03"])
        assert [bands[("2020-03", f"E{i}")] for i in range(6)] == [0, 0, 1, 1, 2, 2]
        assert ("2020-03", "SHORT") not in bands

    def test_the_window_does_not_look_ahead_or_too_far_back(self):
        """月末より後の週と、52 週より前の週は平均に入らない。"""
        inside = _weeks("2020-03-30", 52)
        future = _weeks("2020-05-25", 4)                 # 2020-03 の月末より後
        old = _weeks("2019-01-28", 20)                   # 52 週より前
        turnover = {
            "A": {**{w: 1.0 for w in inside}, **{w: 1e9 for w in future + old}},
            "B": {w: 2.0 for w in inside},
            "C": {w: 3.0 for w in inside},
        }
        bands = op.turnover_bands(turnover, ["2020-03"])
        assert bands[("2020-03", "A")] == 0              # 未来と古い週の大きな値に引っ張られない

    def test_flat_band_costs_reproduce_the_flat_net(self):
        ivs_flat = TestReplayByHand()._run(cost_bps=100.0)
        cost = op.make_band_cost({}, (999.0, 999.0, 999.0), 100.0)   # 帯の無い社は一律
        ivs = op.replay([P1, P2, P3], TestReplayByHand.BUY, TestReplayByHand.SELL,
                        _table_ret(TestReplayByHand.RET), 3, 1, 100.0, cost)
        for a, b in zip(ivs, ivs_flat):
            assert a["cost_banded"] == pytest.approx(b["cost"]) and a["net_banded"] == pytest.approx(b["net"])
            assert a["net"] == b["net"]                  # 既定の net は帯に左右されない
        s = op.summarize(ivs, 3)
        assert s["annual_net_banded"] == pytest.approx(s["annual_net"])

    def test_costlier_band_lowers_only_the_banded_net(self):
        bands = {(P2, "D"): 0, (P2, "E"): 2}             # P2 に買う D は売買代金の小さい帯
        cheap = op.make_band_cost(bands, (10.0, 10.0, 10.0), 10.0)
        dear = op.make_band_cost(bands, (500.0, 10.0, 10.0), 10.0)
        args = ([P1, P2, P3], TestReplayByHand.BUY, TestReplayByHand.SELL, _table_ret(TestReplayByHand.RET), 3, 1, 10.0)
        a, b = op.replay(*args, cheap), op.replay(*args, dear)
        assert b[1]["net_banded"] == pytest.approx(a[1]["net_banded"] - (500.0 - 10.0) / 10_000 / 3)
        assert b[0]["net_banded"] == a[0]["net_banded"]  # P1 の買いは帯の外＝一律
        assert [iv["net"] for iv in a] == [iv["net"] for iv in b]


class TestShiftedMinTrain:
    YMS = _months(2015, 1, 40)

    def _resid(self):
        import plugins.macro_ensemble as m4
        rng = random.Random(0)
        samples = {ym: [([rng.random()], rng.gauss(0, 0.1)) for _ in range(10)] for ym in self.YMS}
        _folds, resid = m4.walk_forward_cv_monthly(samples, ["x"], min_train_months=6, step_months=3,
                                                   return_residuals=True, embargo_months=12)
        return sorted(resid)

    def test_the_grid_moves_by_the_shift(self):
        base = self._resid()
        assert base[0] == self.YMS[18]                    # min_train 6 + embargo 12
        for shift in (1, 2):
            with op.shifted_min_train(shift):
                moved = self._resid()
            assert moved[0] == self.YMS[18 + shift]
            assert not set(moved) & set(base)             # 3 か月おきの格子が重ならない
        assert self._resid() == base                      # 抜けたら元に戻る

    def test_zero_shift_does_not_patch(self):
        import plugins.macro_ensemble as m4
        before = m4.walk_forward_cv_monthly
        with op.shifted_min_train(0):
            assert m4.walk_forward_cv_monthly is before

    def test_a_call_without_the_keyword_stops(self):
        import plugins.macro_ensemble as m4
        with op.shifted_min_train(1), pytest.raises(RuntimeError, match="min_train_months"):
            m4.walk_forward_cv_monthly({}, [])

    def test_overlapping_paths_stop(self):
        op.check_paths_disjoint({0: {"2020-01", "2020-04"}, 1: {"2020-02", "2020-05"}})
        with pytest.raises(RuntimeError, match="重なる"):
            op.check_paths_disjoint({0: {"2020-01", "2020-04"}, 2: {"2020-04"}})


class TestPathsCli:
    KEY = "measure:oof-replay-paths"
    BASES = ["macro_risk_return", "macro_gbdt", "macro_enet"]

    @pytest.mark.parametrize("bad", ["0,3", "1,1", "", "a", "-1"])
    def test_bad_shifts_are_rejected(self, bad):
        with pytest.raises(SystemExit):
            op.build_parser().parse_args(["--start-shifts", bad])

    @pytest.mark.parametrize("bad", ["30,15", "30,15,8,4", "30,-1,8", "x,1,2"])
    def test_bad_band_costs_are_rejected(self, bad):
        with pytest.raises(SystemExit):
            op.build_parser().parse_args(["--cost-bands-bps", bad])

    def test_job_runs_three_paths_alone_into_its_own_file(self):
        job = rd.JOBS[self.KEY]
        assert job.argv[1:3] == ("-m", "scripts.oof_portfolio_replay")
        args = op.build_parser().parse_args(list(job.argv[3:]))
        assert args.start_shifts == (0, 1, 2) and args.cost_bands_bps is None
        assert args.out != str(op._OUT)                   # 1 本の運用再現の結果を上書きしない
        assert job.parallel_sensitive is True
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN

    def test_main_writes_every_path_and_keeps_the_head_as_results(self, monkeypatch, tmp_path):
        """M-4 は回さず、経路ごとに格子のずれた共通域を返す偽物で配線を確かめる（M-2 は選ばない＝本番の行なし）。"""
        import plugins.macro_ensemble as m4
        rng = random.Random(7)
        grids = {0: ["2021-01", "2021-04", "2021-07"], 1: ["2021-02", "2021-05", "2021-08"],
                 2: ["2021-03", "2021-06", "2021-09"]}

        def fake_run_config(label, bases, panel, db, weight_method, keep_common):
            shift = next((s for s in (1, 2) if label.endswith(f"+{s}か月")), 0)
            patched = m4.walk_forward_cv_monthly is not op.walk_forward_cv_monthly
            assert patched == (shift > 0), "ずらしの経路の M-4 だけが、包まれた walk-forward で回る"
            common = {ym: [(f"E{i:02d}", (rng.random(), rng.random(), rng.random()), rng.gauss(0, 0.1))
                           for i in range(8)] for ym in grids[shift]}
            return {"common": common, "bases": bases}

        class Book:
            def __init__(self, _prices):
                pass

            def period_return(self, ec, ym0, ym1):
                return 0.01, False

        monkeypatch.setattr(op, "_load_panel", lambda args: ({}, {}, {}, {}))
        monkeypatch.setattr(op, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())
        monkeypatch.setattr(op, "_run_config", fake_run_config)
        monkeypatch.setattr(op, "buy_scores", lambda name, pred, y, prices_by_co, db: (
            dict(pred), {"lambda": 0.0, "risk_axis": None, "time_reproducible": True, "n_dropped_rows": 0}))
        monkeypatch.setattr(op, "PriceBook", Book)
        out = tmp_path / "paths.json"
        code = op.main(["--start-shifts", "0,1,2", "--models", "macro_risk_return", "macro_enet",
                        "--top-k", "3", "--sell-k", "1", "--out", str(out)])
        assert code == 0
        import json
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert set(payload["paths"]) == {"0", "1", "2"}
        assert payload["results"] == payload["paths"]["0"]["results"]
        assert payload["paths"]["1"]["test_months"] == {"first": "2021-02", "last": "2021-08", "n": 3}
        assert set(payload["path_summary"]) == {"macro_risk_return", "macro_enet"}
        assert payload["path_summary"]["macro_enet"]["n_paths"] == 3
        assert op.PATH_NOTE in payload["caveats"] and op.BAND_NOTE not in payload["caveats"]
        assert payload["params"]["start_shifts"] == [0, 1, 2]
