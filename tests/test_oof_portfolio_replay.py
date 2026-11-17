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
        monkeypatch.setattr(op, "load_index_daily", lambda db, since: {})   # 指数（#1224）は DB から読む＝偽物の DB では読めない
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


# ── #1224: TOPIX に対する超過と、保有の売買代金の帯の内訳（どちらも参考）──────────────────────

def _index_table(table):
    """{(ym0, ym1): リターン or None} を index_ret の形へ（月末の週の値は代用していない）。"""
    def index_ret(ym0, ym1):
        return table.get((ym0, ym1)), False
    return index_ret


def _fake_book(index_return=0.01):
    """どの社もどの区間も +1%、指数だけ `index_return` を返す偽物の `PriceBook`。"""
    class Book:
        def __init__(self, _prices):
            pass

        def period_return(self, ec, ym0, ym1):
            return (index_return if ec == op.INDEX_SERIES else 0.01), False
    return Book


def _fake_main(monkeypatch, common, bases, book):
    """`main` の M-4・パネル・DB・買いの並び・株価を偽物にする（配線だけを確かめる）。"""
    monkeypatch.setattr(op, "_load_panel", lambda args: ({}, {}, {}, {}))
    monkeypatch.setattr(op, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(op, "_run_config", lambda *a, **k: {"common": common, "bases": bases})
    monkeypatch.setattr(op, "buy_scores", lambda name, pred, y, prices_by_co, db: (
        dict(pred), {"lambda": 0.0, "risk_axis": None, "time_reproducible": True, "n_dropped_rows": 0}))
    monkeypatch.setattr(op, "PriceBook", book)


def _fake_common(seed, n_companies):
    rng = random.Random(seed)
    return {ym: [(f"E{i:02d}", (rng.random(), rng.random(), rng.random()), rng.gauss(0, 0.1))
                 for i in range(n_companies)] for ym in ["2021-01", "2021-04", "2021-07"]}


class TestIndexExcess:
    """TestReplayByHand の検体に、区間ごとの指数のリターン（P1→P2 +2%・P2→P3 −1%）を並べる。"""

    INDEX = {(P1, P2): 0.02, (P2, P3): -0.01}

    def _run(self, index=None):
        hand = TestReplayByHand
        return op.replay([P1, P2, P3], hand.BUY, hand.SELL, _table_ret(hand.RET), 3, 1, 100.0,
                         None, _index_table(self.INDEX if index is None else index))

    def test_annual_excess_matches_hand_calculation(self):
        ivs = self._run()
        assert [iv["index"] for iv in ivs] == [0.02, -0.01]
        s = op.summarize(ivs, 3)
        assert s["annual_index"] == pytest.approx((1.02 * 0.99) ** (1 / 0.5) - 1)
        assert s["annual_excess_index"] == pytest.approx(s["annual_net"] - s["annual_index"])
        assert (s["n_index_missing"], s["n_index_stale"]) == (0, 0)
        # 全社平均に対する超過とは別の値＝比べる相手を取り違えていない
        assert s["annual_excess_index"] != pytest.approx(s["annual_excess"])

    def test_the_default_call_keeps_its_keys_and_values(self):
        """足すのは新しいキーだけ。指数を渡さない呼び出し（#1155 の計測など）は、行も要約も今のまま。"""
        plain, with_index = TestReplayByHand()._run(), self._run()
        for a, b in zip(plain, with_index):
            assert set(b) == set(a) | {"index", "index_stale"}
            assert {k: b[k] for k in a} == a
        s_plain, s_index = op.summarize(plain, 3), op.summarize(with_index, 3)
        added = {"annual_index", "annual_excess_index", "annual_excess_index_ci", "n_index_missing", "n_index_stale"}
        assert set(s_index) == set(s_plain) | added and not added & set(s_plain)
        assert {k: s_index[k] for k in s_plain} == s_plain

    def test_a_missing_interval_is_not_chained_as_zero(self):
        """指数の値が 1 区間でも無ければ年率を出さない（0 として繋ぐと、もっともらしい年率が黙って出る）。"""
        s = op.summarize(self._run({(P1, P2): 0.02}), 3)
        assert s["annual_index"] is None and s["annual_excess_index"] is None
        assert s["annual_excess_index_ci"] is None and s["n_index_missing"] == 1
        assert s["annual_excess"] is not None                        # 全社平均の側は影響を受けない

    def test_the_ci_reads_the_index_column(self):
        iv = {"months": 3, "gross": 0.03, "net": 0.03, "bench": 0.01, "index": 0.02, "index_stale": False,
              "sold": [], "forced": [], "n_stopped": 0, "n_missing": 0}
        ivs = [dict(iv) for _ in range(6)]
        ci = op.annual_excess_ci(ivs, bench_key="index")
        point = (1.03 ** 4 - 1) - (1.02 ** 4 - 1)
        assert ci["ci_lo"] == pytest.approx(point) and ci["ci_hi"] == pytest.approx(point)
        assert op.summarize(ivs, 3)["annual_excess_index_ci"] == ci
        # 既定は全社平均のまま
        assert op.annual_excess_ci(ivs)["ci_lo"] == pytest.approx((1.03 ** 4 - 1) - (1.01 ** 4 - 1))

    def test_perfect_foresight_beats_a_flat_index_and_loses_to_a_soaring_one(self):
        periods, future, last, ret = TestSign._world(seed=3)
        scores = {**future, **last}
        flat = op.summarize(op.replay(periods, scores, scores, ret, 5, 1, 0.0, None, lambda a, b: (0.0, False)), 5)
        soaring = op.summarize(op.replay(periods, scores, scores, ret, 5, 1, 0.0, None, lambda a, b: (1.0, False)), 5)
        assert flat["annual_excess_index"] > 0 > soaring["annual_excess_index"]


class TestIndexBars:
    # 2020-03-30（月）〜04-03（金）は月をまたぐ週。株価の週次表では、足の日付は週の最後の営業日（4 月）になる
    DAILY = {"2020-03-26": 98.0, "2020-03-27": 99.0, "2020-03-30": 100.0, "2020-03-31": 101.0,
             "2020-04-01": 102.0, "2020-04-03": 103.0, "2020-04-10": 104.0}

    def test_bars_are_the_last_trading_day_of_each_week_in_date_order(self):
        assert op.index_weekly_bars(dict(reversed(self.DAILY.items()))) == [
            ("2020-03-27", 99.0), ("2020-04-03", 103.0), ("2020-04-10", 104.0)]
        assert op.index_weekly_bars({}) == []

    def test_a_week_straddling_the_month_end_belongs_to_the_later_month(self):
        """月末の週の決め方が保有側と同じ: 3 月の月末の終値は 03-27 の足で、月をまたぐ週（終値 103）ではない。"""
        book = op.PriceBook({op.INDEX_SERIES: op.index_weekly_bars(self.DAILY)})
        assert book.close_at(op.INDEX_SERIES, "2020-03") == (99.0, False)
        assert book.close_at(op.INDEX_SERIES, "2020-04") == (104.0, False)
        assert book.period_return(op.INDEX_SERIES, "2020-03", "2020-04") == (pytest.approx(104.0 / 99.0 - 1), False)
        # 同じ日次を株価の週次の行にしたものと、月末の終値が一致する
        weeks = database.aggregate_weeks(("E1", d, c, None) for d, c in self.DAILY.items())
        stock = [PX(w["trade_date"], w["close_last"], None) for w in sorted(weeks, key=lambda w: w["trade_date"])]
        assert op.PriceBook({"E1": stock}).close_at("E1", "2020-03") == (99.0, False)

    def test_since_is_a_month_before_the_first_test_month(self):
        assert op.index_since("2021-02") == "2021-01-01"
        assert op.index_since("2021-01") == "2020-12-01"

    def test_load_reads_only_the_benchmark_series_from_since(self, db):
        from database import MacroData
        db.add_all([
            MacroData(series_code=op.INDEX_SERIES, trade_date="2020-12-30", close=90.0),    # since より前
            MacroData(series_code=op.INDEX_SERIES, trade_date="2021-01-01", close=95.0),    # since 当日は入る
            MacroData(series_code=op.INDEX_SERIES, trade_date="2021-01-04", close=100.0),
            MacroData(series_code="USDJPY", trade_date="2021-01-04", close=103.0),           # 別の系列
        ])
        db.commit()
        assert op.load_index_daily(db, "2021-01-01") == {"2021-01-01": 95.0, "2021-01-04": 100.0}

    def test_the_series_name_is_borrowed_from_the_ledger(self):
        """比べる相手は docs/VISION.md で 1 つに決めた。売買の記録の成績と別の系列を見ない。"""
        import portfolio_ledger
        assert op.INDEX_SERIES is portfolio_ledger.BENCHMARK_SERIES


class TestBandBreakdown:
    """TestReplayByHand の保有（P1: A・B・C、P2: B・D・E）を帯に分ける。全社平均は P1→P2 が 0.0、P2→P3 が 0.10。"""

    BANDS = {(P1, "A"): 0, (P1, "B"): 2, (P2, "B"): 2, (P2, "D"): 0}     # C と E は帯なし

    def _run(self, ret=None):
        ivs = TestReplayByHand()._run()
        return op.band_breakdown(ivs, self.BANDS, _table_ret(TestReplayByHand.RET if ret is None else ret))

    def test_shares_and_mean_excess_match_hand_calculation(self):
        b = self._run()
        assert list(b) == ["0", "1", "2", op.NO_BAND]
        assert [b[k]["n_slots"] for k in b] == [2, 0, 2, 2]
        assert [b[k]["share"] for k in b] == pytest.approx([1 / 3, 0.0, 1 / 3, 1 / 3])
        assert b["0"]["mean_excess"] == pytest.approx((0.10 - 0.0 + 0.30 - 0.10) / 2)      # A@P1・D@P2
        assert b["2"]["mean_excess"] == pytest.approx((0.20 - 0.0 + 0.10 - 0.10) / 2)      # B@P1・B@P2
        assert b[op.NO_BAND]["mean_excess"] == pytest.approx((-0.30 - 0.0 - 0.10 - 0.10) / 2)   # C@P1・E@P2
        assert b["1"]["mean_excess"] is None and b["1"]["contribution"] == 0.0

    def test_contributions_add_up_to_the_mean_excess_per_slot(self):
        """帯ごとの寄与を足すと、保有全体の延べ数あたりの平均超過（コスト前）になる。"""
        ivs = TestReplayByHand()._run()
        b = self._run()
        per_slot = sum((iv["gross"] - iv["bench"]) * len(iv["holdings"]) for iv in ivs) / 6
        assert sum(v["contribution"] for v in b.values()) == pytest.approx(per_slot)
        assert b["0"]["contribution"] == pytest.approx(0.30 / 6)

    def test_the_band_is_the_one_at_the_start_of_the_interval(self):
        """B は P1 でも P2 でも保有。帯は保有を組んだ期のものを引く（後の期の帯を先取りしない）。"""
        bands = {(P1, "B"): 0, (P2, "B"): 2}
        b = op.band_breakdown(TestReplayByHand()._run(), bands, _table_ret(TestReplayByHand.RET))
        assert (b["0"]["n_slots"], b["2"]["n_slots"]) == (1, 1)
        assert b["0"]["mean_excess"] == pytest.approx(0.20) and b["2"]["mean_excess"] == pytest.approx(0.0)

    def test_a_holding_without_a_return_is_counted_but_not_averaged(self):
        ret = {k: v for k, v in TestReplayByHand.RET.items() if k != ("B", P2, P3)}
        b = self._run(ret)
        assert (b["2"]["n_slots"], b["2"]["n_returns"]) == (2, 1)
        assert b["2"]["mean_excess"] == pytest.approx(0.20)

    def test_no_holdings_gives_no_shares(self):
        b = op.band_breakdown([], self.BANDS, _table_ret({}))
        assert all(v["share"] is None and v["contribution"] is None and v["n_slots"] == 0 for v in b.values())


class TestIndexAndBandsWiring:
    """`run_replays` と `main` の配線。M-4 は回さず、どの社もどの区間も +1% の偽物の株価で確かめる。"""

    KEY = "measure:oof-replay-bands"
    BASES = TestProductionRow.BASES

    def _rows(self, monkeypatch, **kwargs):
        row = TestProductionRow()
        row._patch(monkeypatch)
        return op.run_replays(row._common(), self.BASES, {}, None, list(self.BASES), "macro_enet", 3, 1, 10.0, **kwargs)

    def test_every_row_gets_the_index_excess(self, monkeypatch):
        res = self._rows(monkeypatch, index_bars=[])
        for name in self.BASES:
            s = res[name]["summary"]
            assert s["annual_excess_index"] == pytest.approx(s["annual_net"] - s["annual_index"])
            assert "bands" not in res[name]

    def test_the_default_rows_do_not_change(self, monkeypatch):
        plain, both = self._rows(monkeypatch), self._rows(monkeypatch, index_bars=[], bands={})
        for name in self.BASES:
            assert "annual_index" not in plain[name]["summary"] and "bands" not in plain[name]
            assert {k: both[name]["summary"][k] for k in plain[name]["summary"]} == plain[name]["summary"]
            assert both[name]["ordering"] == plain[name]["ordering"]
            assert both[name]["bands"][op.NO_BAND]["share"] == 1.0   # 帯の表が空＝全ての保有が帯なし

    def test_job_runs_the_bands_alone_into_its_own_file(self):
        job = rd.JOBS[self.KEY]
        assert job.argv[1:3] == ("-m", "scripts.oof_portfolio_replay")
        args = op.build_parser().parse_args(list(job.argv[3:]))
        assert args.cost_bands_bps == (30.0, 15.0, 8.0) and args.start_shifts == (0,)
        assert args.allow_full_pull and args.refresh_cache and not args.smoke
        others = {op.build_parser().parse_args(list(rd.JOBS[k].argv[3:])).out
                  for k in ("measure:oof-replay", "measure:oof-replay-paths")}
        assert args.out not in others                     # 既定と 3 経路の結果を上書きしない
        assert job.parallel_sensitive is True
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN

    def test_main_writes_the_index_and_the_band_breakdown(self, monkeypatch, tmp_path):
        import json
        from datetime import date, timedelta

        import scripts.measure_index_event as mie
        since = []

        def fake_daily(db, s):
            since.append(s)
            return {"2021-01-04": 100.0, "2021-01-08": 101.0, "2021-01-15": 102.0}

        def fake_turnover(db, codes, lo, hi):
            weeks = [date(2021, 7, 26) - timedelta(weeks=i) for i in range(90)]
            # E08 は 13 週に満たない＝帯なし。ほかは社の番号の順に売買代金が大きい
            return {ec: {w: float(int(ec[1:]) + 1) for w in (weeks[:5] if ec == "E08" else weeks)} for ec in codes}

        _fake_main(monkeypatch, _fake_common(11, 9), self.BASES, _fake_book(index_return=0.02))
        monkeypatch.setattr(op, "load_index_daily", fake_daily)
        monkeypatch.setattr(mie, "load_turnover", fake_turnover)
        out = tmp_path / "bands.json"
        assert op.main(["--cost-bands-bps", "30,15,8", "--models", "macro_risk_return", "macro_enet",
                        "--top-k", "3", "--sell-k", "1", "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert since == ["2020-12-01"]                     # 最初の test 月の 1 か月前から、1 回だけ読む
        assert payload["index"] == {"series": op.INDEX_SERIES, "n_weeks": 2,
                                    "first": "2021-01-08", "last": "2021-01-15"}
        assert {op.INDEX_NOTE, op.BAND_NOTE, op.BREAKDOWN_NOTE} <= set(payload["caveats"])
        for name in ("macro_risk_return", "macro_enet"):
            row = payload["results"][name]
            s = row["summary"]
            assert s["annual_index"] == pytest.approx(1.02 ** 4 - 1)
            assert s["annual_excess_index"] == pytest.approx(s["annual_net"] - s["annual_index"])
            assert "annual_net_banded" in s
            assert set(row["bands"]) == {"0", "1", "2", op.NO_BAND}
            assert sum(b["n_slots"] for b in row["bands"].values()) == 3 * 2          # K=3 × 2 区間
            assert sum(b["share"] for b in row["bands"].values()) == pytest.approx(1.0)

    def test_the_default_run_has_no_band_breakdown(self, monkeypatch, tmp_path):
        """帯の内訳は `--cost-bands-bps` を付けたときだけ。指数に対する超過は引数なしでも出る。"""
        import json
        _fake_main(monkeypatch, _fake_common(12, 8), self.BASES, _fake_book())
        monkeypatch.setattr(op, "load_index_daily", lambda db, since: {})
        out = tmp_path / "plain.json"
        assert op.main(["--models", "macro_enet", "--top-k", "3", "--sell-k", "1", "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        row = payload["results"]["macro_enet"]
        assert "bands" not in row and "annual_excess_index" in row["summary"]
        assert op.INDEX_NOTE in payload["caveats"] and op.BREAKDOWN_NOTE not in payload["caveats"]
        assert payload["index"]["n_weeks"] == 0


# ── 買い方の規則の計測（#1223・親 #1227・事前登録 docs/prereg/portfolio-construction.md）──────────

class TestReplayCombined:
    """K=2・k=1 の 2 つの運用（L1・L2）を合わせた手計算の検体。

    P1: L1 は A>B>C>D → {A, B}、L2 は B>C>A>D → {B, C}。和集合 {A, B, C}（B は 1 社）
    P2: L1 は自分の保有 {A, B} のうち売りのキーが低い A を売り、E を買う → {B, E}
        L2 は {B, C} のうち C を売り、A を買う（L1 が売った社でも L2 は買える）→ {A, B}
        和集合 {A, B, E}: 出たのは C だけ、入ったのは E だけ（A は L1 が売り L2 が買った＝和集合では動かない）
    """
    BUYS = {
        "L1": {**{(P1, ec): s for ec, s in zip("ABCD", [4, 3, 2, 1])},
               **{(P2, ec): s for ec, s in zip("EADCB", [5, 4, 3, 2, 1])}},
        "L2": {**{(P1, ec): s for ec, s in zip("BCAD", [4, 3, 2, 1])},
               **{(P2, ec): s for ec, s in zip("ADEBC", [5, 4, 3, 2, 1])}},
    }
    SELL = {**{(P1, ec): 0.0 for ec in "ABCD"},
            **{(P2, ec): s for ec, s in zip("ABCDE", [0.1, 0.5, 0.2, 0.3, 0.4])}}
    RET = {("A", P1, P2): 0.10, ("B", P1, P2): 0.20, ("C", P1, P2): 0.30, ("D", P1, P2): 0.00,
           ("A", P2, P3): 0.00, ("B", P2, P3): 0.10, ("C", P2, P3): -0.20, ("D", P2, P3): 0.00,
           ("E", P2, P3): 0.30}

    def _run(self, **kw):
        return op.replay_combined([P1, P2, P3], self.BUYS, self.SELL, _table_ret(self.RET), 2, 1, 100.0, **kw)

    def test_union_counts_a_company_held_by_both_once(self):
        iv1, _ = self._run()
        assert (iv1["holdings"], iv1["bought"], iv1["size"], iv1["n_overlap"]) == (["A", "B", "C"], ["A", "B", "C"], 3, 1)
        assert iv1["cost"] == pytest.approx(0.01 * 3 / 3)
        assert iv1["gross"] == pytest.approx((0.10 + 0.20 + 0.30) / 3)
        assert iv1["bench"] == pytest.approx((0.10 + 0.20 + 0.30 + 0.00) / 4)

    def test_a_sale_by_one_leg_kept_by_the_other_is_not_a_trade(self):
        _, iv2 = self._run()
        assert iv2["holdings"] == ["A", "B", "E"]
        assert (iv2["sold"], iv2["forced"], iv2["bought"]) == (["C"], [], ["E"])
        assert iv2["cost"] == pytest.approx(0.01 * 2 / 3)               # 和集合の保有数で割る
        assert iv2["gross"] == pytest.approx((0.00 + 0.10 + 0.30) / 3)
        assert iv2["swap_gain"] == pytest.approx(0.30 - (-0.20))

    def test_turnover_divides_by_the_union_size(self):
        s = op.summarize(self._run(), 2 * 2)
        assert s["turnover"] == pytest.approx(1 / 3)                     # 2 回目の入れ替え: 出た 1 社 ÷ 3 社

    def test_a_single_leg_matches_replay(self):
        legs = {"L1": self.BUYS["L1"]}
        combined = op.replay_combined([P1, P2, P3], legs, self.SELL, _table_ret(self.RET), 2, 1, 100.0)
        single = op.replay([P1, P2, P3], self.BUYS["L1"], self.SELL, _table_ret(self.RET), 2, 1, 100.0)
        for c, s in zip(combined, single):
            assert {k: c[k] for k in s} == s


class TestIndustryCap:
    """業種の上限 2 社。A・B・C は業種 X、D は Y、E は業種が空。"""
    IND = {"A": "X", "B": "X", "C": "X", "D": "Y", "E": None}
    BUYS = {"L1": {(P1, ec): s for ec, s in zip("ABCD", [4, 3, 2, 1])},
            "L2": {(P1, ec): s for ec, s in zip("BCED", [5, 4, 3, 2])}}
    SELL = {(P1, ec): 0.0 for ec in "ABCDE"}

    def _run(self, cap):
        return op.replay_combined([P1, P2], self.BUYS, self.SELL, _table_ret({}), 2, 1, 0.0, self.IND, cap)

    def test_cap_skips_and_refills_from_the_next_rank(self):
        (iv,) = self._run(2)
        # L1 が A・B（X が 2 社）。L2 の B は和集合に既にある＝数を増やさないので買える。C は X が上限で飛ばし、
        # 業種が空の E を買う
        assert iv["holdings"] == ["A", "B", "E"]
        assert (iv["n_cap_skipped"], iv["max_industry"]) == (1, 2)

    def test_without_cap_the_industry_piles_up(self):
        (iv,) = self._run(None)
        assert iv["holdings"] == ["A", "B", "C"]
        assert (iv["n_cap_skipped"], iv["max_industry"]) == (0, 3)

    def test_construction_stats(self):
        stats = op.construction_stats(self._run(2), self.IND)
        assert stats == {"mean_holdings": 3, "min_holdings": 3, "mean_overlap": 1, "max_industry": 2, "n_cap_skipped": 1}
        assert op.construction_stats([], self.IND)["mean_holdings"] is None


def _decision_rows(dd: dict, ex: dict, missing: int = 0) -> dict:
    """{構成: {"summary": 最大下落・対TOPIX の超過}}（判定に要る値だけ）。"""
    return {k: {"summary": {"max_drawdown": dd[k], "annual_excess_index": ex[k], "n_index_missing": missing}}
            for k in dd}


class TestPortfolioDecision:
    BASE_DD, BASE_EX = 0.30, 0.10

    def _paths(self, cand: dict, n: int = 3) -> dict:
        """cand = {構成: [(最大下落, 対TOPIX の超過) を経路ごとに]}。指定しない構成は基準と同じ（＝下落が浅くない）。"""
        out = {}
        for s in range(n):
            dd = {k: self.BASE_DD for k in op.PORTFOLIO_LABELS}
            ex = {k: self.BASE_EX for k in op.PORTFOLIO_LABELS}
            for k, vals in cand.items():
                dd[k], ex[k] = vals[s]
            out[s] = _decision_rows(dd, ex)
        return out

    def test_the_first_candidate_that_passes_is_picked(self):
        d = op.portfolio_decision(self._paths({"D4": [(0.25, 0.09)] * 3, "D3": [(0.20, 0.10)] * 3}))
        assert d["judged"] and d["decision"] == "D4"
        assert d["candidates"]["D4"]["excess_index_diff_mean"] == pytest.approx(-0.01)

    def test_a_deeper_drawdown_on_one_path_fails(self):
        d = op.portfolio_decision(self._paths({"D4": [(0.25, 0.10), (0.31, 0.10), (0.25, 0.10)],
                                               "D3": [(0.28, 0.10)] * 3}))
        assert not d["candidates"]["D4"]["dd_smaller_on_every_path"] and d["decision"] == "D3"

    def test_the_excess_drop_boundary(self):
        ok = op.portfolio_decision(self._paths({"A": [(0.2, self.BASE_EX - 0.02)] * 3}))
        ng = op.portfolio_decision(self._paths({"A": [(0.2, self.BASE_EX - 0.021)] * 3}))
        assert ok["decision"] == "A" and ng["decision"] == op.PORTFOLIO_BASELINE

    def test_nothing_passes_keeps_the_current_rule(self):
        d = op.portfolio_decision(self._paths({}))
        assert d["judged"] and d["decision"] == "C" and not any(c["passes"] for c in d["candidates"].values())

    @pytest.mark.parametrize("case", ["two_paths", "missing_path", "missing_index"])
    def test_unusable_inputs_are_not_judged(self, case):
        paths = self._paths({"D4": [(0.2, 0.1)] * 3}, n=2 if case == "two_paths" else 3)
        if case == "missing_path":
            paths[1] = None
        if case == "missing_index":
            paths[2]["C"]["summary"]["n_index_missing"] = 1
        d = op.portfolio_decision(paths)
        assert d["judged"] is False and d["decision"] is None and d["reasons"]


class TestRunPortfolio:
    """7 構成を 1 つの経路で回す。買いの並びは予測そのまま、株価はどの社も +1%（TestProductionRow と同じ差し替え）。"""
    BASES = TestProductionRow.BASES
    YMS = TestProductionRow.YMS

    def _common(self):
        rng = random.Random(9)
        return {ym: [(f"E{i:02d}", (rng.random(), rng.random(), float(i)), rng.gauss(0, 0.1)) for i in range(25)]
                for ym in self.YMS}

    def _run(self, monkeypatch, production, industry_of=None):
        TestProductionRow()._patch(monkeypatch)
        return op.run_portfolio(self._common(), self.BASES, {}, None, production, "macro_enet", 10.0,
                                industry_of or {})

    def test_every_construction_is_built(self, monkeypatch):
        prod = {(ym, f"E{i:02d}"): float(-i) for ym in self.YMS for i in range(25)}   # M-6 と逆の並び
        rows, why = self._run(monkeypatch, prod)
        assert why is None and list(rows) == list(op.PORTFOLIO_LABELS)
        first = {k: rows[k]["intervals"][0]["holdings"] for k in rows}
        top_a, top_b = [f"E{i:02d}" for i in range(15, 25)], [f"E{i:02d}" for i in range(10)]
        assert first["A"] == top_a and first["B"] == top_b
        assert first["C"] == sorted(top_a + top_b) and rows["C"]["construction"]["mean_overlap"] == 0
        assert len(first["A20"]) == 20 and len(first["B20"]) == 20
        assert rows["C"]["label"] == "合算" and "annual_net" in rows["D4"]["summary"]

    def test_the_cap_binds_when_one_industry_dominates(self, monkeypatch):
        prod = {(ym, f"E{i:02d}"): float(i) for ym in self.YMS for i in range(25)}    # M-6 と同じ並び
        ind = {f"E{i:02d}": ("X" if i >= 15 else "Y") for i in range(25)}
        rows, _ = self._run(monkeypatch, prod, ind)
        assert rows["C"]["construction"]["max_industry"] == 10
        assert rows["D4"]["construction"]["max_industry"] <= 4 and rows["D3"]["construction"]["max_industry"] <= 3
        assert rows["D4"]["construction"]["n_cap_skipped"] > 0

    def test_no_production_is_not_run(self, monkeypatch):
        assert self._run(monkeypatch, None) == (None, "M-2*（本番の設定）の予測が無い")


class TestPortfolioCli:
    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        from scripts import prereg
        monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)

    @staticmethod
    def _sha():
        from scripts import prereg
        return prereg.body_sha256(prereg.read_document(op.PORTFOLIO_PREREG))

    def test_document_states_the_same_values(self):
        from scripts import prereg
        text = prereg.read_document(op.PORTFOLIO_PREREG)
        body, _ = prereg.split_document(text)
        assert prereg.recorded_sha(text) == self._sha()
        assert prereg.issue_url(text).endswith("/issues/1227")
        for phrase in ("業種の上限 4 社", "業種の上限 3 社", "−0.02 以上", "D4 → D3 → A", "--start-shifts 0,1,2",
                       "M-6 の運用が先", "#1238 で登録する"):
            assert phrase in body, phrase
        assert op.INDUSTRY_CAPS == {"D4": 4, "D3": 3} and op.PORTFOLIO_CANDIDATES == ("D4", "D3", "A")
        assert op.PORTFOLIO_MAX_EXCESS_DROP == 0.02 and op.PORTFOLIO_N_PATHS == 3
        assert op.COMBINED_LEGS == ("macro_enet", op.PRODUCTION_KEY)

    @pytest.mark.parametrize("argv", [
        ["--portfolio"],
        ["--portfolio", "--prereg", op.PORTFOLIO_PREREG, "--prereg-sha", "f" * 64],
        ["--prereg", op.PORTFOLIO_PREREG, "--prereg-sha", "f" * 64],
        ["--portfolio", "--prereg", "docs/prereg/live-oos.md", "--prereg-sha", "f" * 64],
    ])
    def test_bad_prereg_stops_before_loading(self, monkeypatch, argv):
        monkeypatch.setattr(op, "_load_panel", lambda args: pytest.fail("照合の前にパネルを読んだ"))
        assert op.main(argv) == 2

    def test_portfolio_keeps_the_preregistered_k_cost_and_sell_key(self, monkeypatch):
        monkeypatch.setattr(op, "_load_panel", lambda args: pytest.fail("照合の前にパネルを読んだ"))
        base = ["--portfolio", "--prereg", op.PORTFOLIO_PREREG, "--prereg-sha", self._sha()]
        s2 = ["--sell-source", op.SELL_S2_SOURCE]
        for extra in (["--top-k", "5"], ["--sell-k", "1"], ["--cost-bps", "20"], ["--cost-bands-bps", "30,15,8"]):
            assert op.main(base + s2 + extra) == 2, extra
        assert op.main(base) == 2                                  # 売りの並びが M-6 のまま（事前登録 §2.1 は S2）

    def test_main_writes_the_constructions_and_the_decision(self, monkeypatch, tmp_path):
        import json

        common = TestRunPortfolio()._common()
        _fake_main(monkeypatch, common, self.BASES, _fake_book())
        prod = {(ym, ec): -yh[2] for ym, rows in common.items() for ec, yh, _y in rows}
        # S2 の OOF。2021-09 は全社同じ値＝その期だけ M-6 の μ̂ へ倒れる
        s2 = {(ym, ec): (0.0 if ym == "2021-09" else -yh[0]) for ym, rows in common.items() for ec, yh, _y in rows}
        monkeypatch.setattr(op, "production_predictions", lambda panel, db, yms: (prod, {"available": True}))
        monkeypatch.setattr(op, "s2_predictions", lambda captured, common_, bases: (s2, {"n_folds": 3}))
        monkeypatch.setattr(op, "load_index_daily", lambda db, since: {})
        out = tmp_path / "portfolio.json"
        code = op.main(["--portfolio", "--prereg", op.PORTFOLIO_PREREG, "--prereg-sha", self._sha(),
                        "--sell-source", op.SELL_S2_SOURCE, "--models", "macro_enet", "--out", str(out)])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        pf = payload["portfolio"]
        assert pf["prereg_sha256"] == self._sha() and pf["prereg_issue"].endswith("/issues/1227")
        assert list(pf["paths"]["0"]["rows"]) == list(op.PORTFOLIO_LABELS)
        assert pf["decision"]["judged"] is False                  # 経路が 1 本だけ＝判定しない
        assert pf["sell_source"] == op.SELL_S2_SOURCE and pf["paths"]["0"]["sell_fallback_periods"] == ["2021-09"]
        assert payload["sell"]["paths"]["0"] == {"fallback_periods": ["2021-09"], "s2_fit": {"n_folds": 3}}
        assert op.PORTFOLIO_NOTE in payload["caveats"] and op.SELL_NOTE in payload["caveats"]
        assert list(payload["results"]) == ["macro_enet"]         # 既定の行は今のまま

    BASES = TestProductionRow.BASES

    def test_default_call_has_no_portfolio(self, monkeypatch, tmp_path):
        import json

        _fake_main(monkeypatch, TestRunPortfolio()._common(), self.BASES, _fake_book())
        monkeypatch.setattr(op, "production_predictions", lambda *a: pytest.fail("M-2 を選ばない既定で本番の M-2 を回した"))
        monkeypatch.setattr(op, "load_index_daily", lambda db, since: {})
        monkeypatch.setattr(op, "s2_predictions", lambda *a: pytest.fail("既定の売りの並び（M-6）で S2 を回した"))
        out = tmp_path / "plain.json"
        assert op.main(["--models", "macro_enet", "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert "portfolio" not in payload and op.PORTFOLIO_NOTE not in payload["caveats"]
        assert "sell" not in payload and op.SELL_NOTE not in payload["caveats"]
        assert payload["params"]["sell_source"] == "macro_enet"


# ── 売りの並びの S2（#1238）──────────────────────────────────────────────────────

class TestS2SellKeys:
    M6 = {("2021-06", "A"): 0.3, ("2021-06", "B"): 0.1, ("2021-09", "A"): 0.2, ("2021-09", "B"): 0.4}

    def test_flat_periods_fall_back_to_m6_and_others_keep_s2(self):
        s2 = {("2021-06", "A"): -0.1, ("2021-06", "B"): -0.5, ("2021-09", "A"): -0.2, ("2021-09", "B"): -0.2}
        keys, fallback = op.s2_sell_keys(s2, self.M6)
        assert fallback == ["2021-09"]
        assert keys == {("2021-06", "A"): -0.1, ("2021-06", "B"): -0.5,         # S2 のまま
                        ("2021-09", "A"): 0.2, ("2021-09", "B"): 0.4}           # 期ごと M-6 の μ̂（混ぜない）

    def test_no_flat_period_keeps_everything(self):
        s2 = {k: float(i) for i, k in enumerate(self.M6)}
        assert op.s2_sell_keys(s2, self.M6) == (s2, [])

    def test_sell_source_choices_and_label(self):
        assert op.SELL_S2_SOURCE == "s2_quantile_low" and op.SELL_S2_SOURCE in op.SELL_SOURCES
        assert op.build_parser().parse_args([]).sell_source == "macro_enet"     # 既定は今のまま
        assert op._sell_label(op.SELL_S2_SOURCE).startswith("S2") and op._sell_label("macro_enet") == "M-6"


class TestS2Predictions:
    def test_capture_records_the_m6_leg_inputs_and_passes_through(self, monkeypatch):
        calls = []
        monkeypatch.setattr(op.macro_ensemble.plugin, "_m6_cv",
                            lambda b, m6p, yms, fit_predict=None: calls.append(fit_predict) or ("aligned", "ctx"))
        store: dict = {}
        with op.capture_m6_args(store):
            assert op.macro_ensemble.plugin._m6_cv("b", {"l1_ratio": 0.5}, ["2021-06"]) == ("aligned", "ctx")
        assert store["args"] == ("b", {"l1_ratio": 0.5}, ["2021-06"]) and calls == [None]

    def test_uncaptured_inputs_stop(self):
        with pytest.raises(RuntimeError, match="捕まえられなかった"):
            op.s2_predictions({}, TestProductionRow()._common(), TestProductionRow.BASES)

    def test_runs_the_bakeoff_learner_on_the_m6_inputs_and_aligns_to_the_common_rows(self, monkeypatch):
        common = TestProductionRow()._common()
        seen = {}

        def fake_fit_predict(name, m6p, diag):
            seen["fit"] = (name, m6p)
            return "fp"

        def fake_m6_cv(b, m6p, yms, fit_predict=None):
            seen["cv"] = (b, yms, fit_predict)
            return {(ym, ec): (-yh[0], y) for ym, rows in common.items() for ec, yh, y in rows}, None

        monkeypatch.setattr(op, "sell_candidate_fit_predict", fake_fit_predict)
        monkeypatch.setattr(op.macro_ensemble.plugin, "_m6_cv", fake_m6_cv)
        preds, info = op.s2_predictions({"args": ("b", {"l1_ratio": 0.5}, ["2021-06"])}, common,
                                        TestProductionRow.BASES)
        assert seen == {"fit": (op.SELL_S2_SOURCE, {"l1_ratio": 0.5}), "cv": ("b", ["2021-06"], "fp")}
        assert preds == {(ym, ec): -yh[0] for ym, rows in common.items() for ec, yh, _y in rows}
        assert isinstance(info, dict)

    def test_run_replays_uses_the_given_sell_keys(self, monkeypatch):
        row = TestProductionRow()
        row._patch(monkeypatch)
        common = row._common()
        preds, _y = op.common_to_dicts(common, row.BASES)
        args = (common, row.BASES, {}, None, ["macro_enet"], "macro_enet", 3, 1, 10.0)
        plain = op.run_replays(*args)
        same = op.run_replays(*args, sell_keys=preds["macro_enet"])
        flipped = op.run_replays(*args, sell_keys={k: -v for k, v in preds["macro_enet"].items()})
        assert same == plain
        assert flipped["macro_enet"]["intervals"][1]["sold"] != plain["macro_enet"]["intervals"][1]["sold"]


class TestPortfolioJob:
    KEY = "measure:oof-replay-portfolio"

    def test_job_runs_the_preregistered_measurement(self):
        from scripts import prereg
        job = rd.JOBS[self.KEY]
        assert job.argv[1:3] == ("-m", "scripts.oof_portfolio_replay")
        args = op.build_parser().parse_args(list(job.argv[3:]))
        assert args.portfolio and args.start_shifts == (0, 1, 2) and args.sell_source == op.SELL_S2_SOURCE
        assert args.prereg == op.PORTFOLIO_PREREG
        assert args.prereg_sha == prereg.recorded_sha(prereg.read_document(op.PORTFOLIO_PREREG))
        assert (args.top_k, args.sell_k, args.cost_bps, args.cost_bands_bps) == (10, 2, 10.0, None)
        assert args.allow_full_pull and args.refresh_cache and not args.smoke
        others = {op.build_parser().parse_args(list(rd.JOBS[k].argv[3:])).out
                  for k in ("measure:oof-replay", "measure:oof-replay-paths", "measure:oof-replay-bands")}
        assert args.out not in others
        assert job.parallel_sensitive is True
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN


# ── 買いのスコアの同点の数え上げ（#1259・参考）──────────────────────────────────

class TestBuyTies:
    """`buy_ties` の検体: 全社同じ値の期・境目をまたぐ同点の期・同点の無い期・社が少ない期。"""

    def _buy(self):
        buy = {}
        buy.update({("2021-02", f"E{i:02d}"): 0.5 for i in range(12)})                    # 全社同じ値
        # 上位 2 社は単独、3〜5 番目が同点（0.3）＝K=3 の境目をまたぐ
        for i, v in enumerate([0.9, 0.8, 0.3, 0.3, 0.3, 0.1, 0.0]):
            buy[("2021-05", f"E{i:02d}")] = v
        buy.update({("2021-08", f"E{i:02d}"): float(i) for i in range(7)})                # 同点なし
        buy.update({("2021-11", f"E{i:02d}"): 1.0 for i in range(2)})                     # K より少ない
        return buy

    def test_counts_flat_crossing_and_clean_periods(self):
        t = op.buy_ties(self._buy(), (3, 6))
        assert list(t) == ["2021-02", "2021-05", "2021-08", "2021-11"]
        assert t["2021-02"] == {"n": 12, "n_unique": 1, "flat": True,
                                "at_k": {"3": {"tied": 12, "crosses": True}, "6": {"tied": 12, "crosses": True}}}
        assert t["2021-05"]["flat"] is False and t["2021-05"]["n_unique"] == 5
        assert t["2021-05"]["at_k"]["3"] == {"tied": 3, "crosses": True}      # 3 番目の 0.3 は 4・5 番目と同点
        assert t["2021-05"]["at_k"]["6"] == {"tied": 1, "crosses": False}     # 6 番目の 0.1 は単独
        assert t["2021-08"]["at_k"]["3"] == {"tied": 1, "crosses": False}
        assert t["2021-11"]["at_k"]["3"] == {"tied": None, "crosses": None}   # 社が K 以下＝境目なし

    def test_the_crossing_tie_is_what_decides_by_code_in_replay(self):
        """同点が境目をまたぐ期は、`replay` が社の順で買う＝数え上げが指す期と一致する。"""
        buy = self._buy()
        sell = {k: 0.0 for k in buy}
        ivs = op.replay(["2021-02", "2021-05", "2021-08"], buy, sell, lambda ec, a, b: (0.0, False), 3, 0, 0.0)
        assert ivs[0]["bought"] == ["E00", "E01", "E02"]                     # 全社同じ値＝コードの小さい 3 社
        assert op.buy_ties(buy, (3,))["2021-02"]["flat"] is True


class TestTiesWiring:
    """`run_replays(tie_report=True)` と `main --tie-report` の配線。既定では何も足さない。"""

    BASES = TestProductionRow.BASES

    def _rows(self, monkeypatch, **kwargs):
        row = TestProductionRow()
        row._patch(monkeypatch)
        return op.run_replays(row._common(), self.BASES, {}, None, list(self.BASES), "macro_enet", 3, 1, 10.0, **kwargs)

    def test_every_row_gets_the_ties_only_when_asked(self, monkeypatch):
        plain = self._rows(monkeypatch)
        prod = {(ym, f"E{i:02d}"): 1.0 for ym in TestProductionRow.YMS for i in range(8)}   # 本番の行は全社同じ値
        ties = self._rows(monkeypatch, tie_report=True, production=prod)
        for name in self.BASES:
            assert "buy_ties" not in plain[name]
            assert {k: ties[name][k] for k in plain[name]} == plain[name]               # 既存のキーの値は変わらない
            assert set(ties[name]["buy_ties"]) == set(TestProductionRow.YMS)
            assert set(ties[name]["buy_ties"]["2021-06"]["at_k"]) == {"3", "6"}           # K と 2K
        assert all(t["flat"] for t in ties[op.PRODUCTION_KEY]["buy_ties"].values())

    def test_main_writes_and_prints_the_ties(self, monkeypatch, tmp_path, capsys):
        import json
        common = _fake_common(13, 8)
        # 2021-04 だけ全社同じ値にする
        common["2021-04"] = [(ec, (0.5, 0.5, 0.5), y) for ec, _p, y in common["2021-04"]]
        _fake_main(monkeypatch, common, self.BASES, _fake_book())
        monkeypatch.setattr(op, "load_index_daily", lambda db, since: {})
        out = tmp_path / "ties.json"
        assert op.main(["--models", "macro_enet", "--top-k", "3", "--sell-k", "1", "--tie-report",
                        "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        ties = payload["results"]["macro_enet"]["buy_ties"]
        assert ties["2021-04"]["flat"] is True and ties["2021-01"]["flat"] is False
        assert op.TIE_NOTE in payload["caveats"]
        assert "全社同じ値: 2021-04(8社)" in capsys.readouterr().out

    def test_the_default_run_has_no_ties(self, monkeypatch, tmp_path):
        import json
        _fake_main(monkeypatch, _fake_common(13, 8), self.BASES, _fake_book())
        monkeypatch.setattr(op, "load_index_daily", lambda db, since: {})
        out = tmp_path / "plain.json"
        assert op.main(["--models", "macro_enet", "--top-k", "3", "--sell-k", "1", "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert "buy_ties" not in payload["results"]["macro_enet"]
        assert op.TIE_NOTE not in payload["caveats"] and "tie_report" not in payload["params"]


class TestTiesJob:
    KEY = "measure:oof-replay-ties"

    def test_job_counts_the_ties_on_three_paths_into_its_own_file(self):
        job = rd.JOBS[self.KEY]
        assert job.argv[1:3] == ("-m", "scripts.oof_portfolio_replay")
        args = op.build_parser().parse_args(list(job.argv[3:]))
        assert args.tie_report and args.start_shifts == (0, 1, 2) and not args.portfolio
        assert args.sell_source == op.CANDIDATE and args.cost_bands_bps is None   # S2 を回さない＝買いの側だけを数える
        assert args.allow_full_pull and args.refresh_cache and not args.smoke
        others = {op.build_parser().parse_args(list(rd.JOBS[k].argv[3:])).out
                  for k in ("measure:oof-replay", "measure:oof-replay-paths", "measure:oof-replay-bands",
                            "measure:oof-replay-portfolio")}
        assert args.out not in others
        assert job.parallel_sensitive is True
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN
