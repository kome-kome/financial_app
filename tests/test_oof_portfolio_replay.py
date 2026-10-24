"""OOF 運用再現（Issue #1045・ADR-0069 の参考）のユニットテスト。

`scripts/oof_portfolio_replay.py` の入れ替えの再現・期間リターン・指標と、引数・日中キューの登録を
合成の検体で確かめる。M-4 は回さない（共通域の取り出しは tests/test_sell_mu_source_bakeoff.py）。
"""
import math
import random
from collections import namedtuple

import pytest

import scripts.oof_portfolio_replay as op
import scripts.run_daytime as rd

PX = namedtuple("PX", "trade_date close_last volume_sum")
P1, P2, P3 = "2020-01", "2020-04", "2020-07"


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
