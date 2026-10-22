"""OOF の上位K社の参考指標（Issue #1041・ADR-0069）のユニットテスト。

`plugins/macro_snapshots.py::oof_backtest` が返す top_k / top_k_excess_return_by_period /
top_decile_rank_ic を合成の検体で確かめる。判定には使わない参考値なので、足しても既存のキーの
値が変わらないことも同じファイルで確かめる。
"""
import json
import math
import random

import pytest

import plugins.macro_snapshots as ms
from plugins.macro_snapshots import _spearman, oof_backtest

KS = (5, 10, 30)
NEW_KEYS = {"top_k", "top_k_excess_return_by_period", "top_decile_rank_ic"}
EMPTY_K = {"n_periods": 0, "excess_return": None, "precision": None, "ndcg": None}


@pytest.fixture(autouse=True)
def _pin_reference(monkeypatch):
    """手計算は K=5/10/30・上位 1/10 を前提にする（参考の K を変えても計算の検査は残す）。"""
    monkeypatch.setattr(ms, "TOP_K_REFERENCE", KS)
    monkeypatch.setattr(ms, "TOP_DECILE_DIVISOR", 10)


def _ramp(m, sign=1.0):
    """yhat=i・y=sign*i の m 行（sign=1 で予測と実現が同じ順、-1 で逆順）。"""
    return [(float(i), sign * float(i)) for i in range(m)]


def _shuffled(rows, seed):
    out = list(rows)
    random.Random(seed).shuffle(out)
    return out


def _discounts(k):
    return [1.0 / math.log2(p + 2) for p in range(k)]


class TestHandComputed:
    """4〜6 位が同点（yhat=6）で K=5 の境目をまたぐ 10 社。同点の 3 社は残り 1 枠を 1/3 ずつ分ける。"""

    YHAT = [10, 9, 8, 7, 6, 6, 6, 3, 2, 1]
    Y = [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]      # 期内平均 4.5

    def _o(self):
        return oof_backtest({"2020-01": list(zip(self.YHAT, self.Y))}, n_quantiles=5)

    def test_excess_return_splits_the_tied_slot(self):
        # 上位4社 9+8+7+6=30、同点3社(5,4,3)の 1/3 ずつ → 34/5=6.8 − 4.5
        o = self._o()
        assert o["top_k"]["5"]["excess_return"] == pytest.approx(2.3, abs=1e-6)
        assert o["top_k_excess_return_by_period"]["5"]["2020-01"] == pytest.approx(2.3, abs=1e-6)

    def test_precision_counts_the_tied_slot_by_its_share(self):
        # 上位4社は全て 4.5 超、同点3社のうち y=5 だけが超える → (4 + 1/3) / 5
        assert self._o()["top_k"]["5"]["precision"] == pytest.approx(13 / 15, abs=5e-5)

    def test_ndcg_averages_the_discount_over_the_tied_positions(self):
        gains = [y / 9 for y in self.Y]      # y の期内順位を 0〜1 に（y=9 が 1、y=0 が 0）
        d = _discounts(5)
        dcg = sum(g * w for g, w in zip(gains[:4], d[:4])) + (gains[4] + gains[5] + gains[6]) / 3 * d[4]
        idcg = sum(g * w for g, w in zip(gains[:5], d))
        assert self._o()["top_k"]["5"]["ndcg"] == pytest.approx(dcg / idcg, abs=5e-5)

    def test_larger_k_is_left_out_below_2k_rows(self):
        o = self._o()
        assert o["top_k"]["5"]["n_periods"] == 1
        for k in ("10", "30"):
            assert o["top_k"][k] == EMPTY_K
            assert o["top_k_excess_return_by_period"][k] == {}


class TestOrdering:
    M = 60   # K=30 まで測れる（2K=60）

    def test_perfect_order_is_the_best_case(self):
        o = oof_backtest({"m": _ramp(self.M)}, n_quantiles=5)
        for k in KS:
            t = o["top_k"][str(k)]
            # 上位 K 社の y の平均 M-(K+1)/2 − 全体平均 (M-1)/2
            assert t["excess_return"] == pytest.approx((self.M - k) / 2, abs=1e-6)
            assert t["precision"] == 1.0
            assert t["ndcg"] == 1.0
        assert o["top_decile_rank_ic"] == {"mean": 1.0, "n": 1}

    def test_reverse_order_is_the_worst_case(self):
        rev = oof_backtest({"m": _ramp(self.M, sign=-1.0)}, n_quantiles=5)
        assert rev["top_decile_rank_ic"] == {"mean": -1.0, "n": 1}
        ys = [float(i) for i in range(self.M)]
        for seed in range(5):
            other = oof_backtest({"m": list(zip(ys, _shuffled(ys, seed)))}, n_quantiles=5)
            for k in KS:
                r, t = rev["top_k"][str(k)], other["top_k"][str(k)]
                assert r["excess_return"] == pytest.approx(-(self.M - k) / 2, abs=1e-6)
                assert r["precision"] == 0.0
                assert r["excess_return"] <= t["excess_return"]
                assert r["ndcg"] <= t["ndcg"]

    def test_value_does_not_depend_on_row_order(self):
        """予測が同じ社が境目をまたいでも、行（社）の順を入れ替えて値が変わらない（#977 と同じ理由）。"""
        rng = random.Random(1041)
        rows = [(float(rng.randint(0, 8)), rng.gauss(0.0, 1.0)) for _ in range(70)]   # 同点だらけ
        base = oof_backtest({"m": rows}, n_quantiles=5)
        for seed in range(3):
            o = oof_backtest({"m": _shuffled(rows, seed)}, n_quantiles=5)
            for k in KS:
                for field in ("excess_return", "precision", "ndcg"):
                    assert o["top_k"][str(k)][field] == pytest.approx(
                        base["top_k"][str(k)][field], abs=1e-6), (k, field)
            assert o["top_decile_rank_ic"]["mean"] == pytest.approx(
                base["top_decile_rank_ic"]["mean"], abs=1e-6)


class TestTopDecile:
    def test_boundary_ties_are_all_included(self):
        # 30 社 → 上位 3 社。3 位の yhat=18 に 3 社が並ぶので 5 社で Spearman を取る
        yhat = [20, 19, 18, 18, 18] + [-(i + 1) for i in range(25)]
        y = [5, 1, 4, 2, 3] + [0] * 25
        o = oof_backtest({"m": list(zip(yhat, y))}, n_quantiles=5)
        expected = _spearman(yhat[:5], y[:5])
        assert o["top_decile_rank_ic"]["mean"] == pytest.approx(expected, abs=5e-5)

    def test_top_share_is_rounded_up_by_integer_division(self):
        # 30 社 → 上位 3 社（30*0.1 を切り上げると 4 社になる）。4 位を入れると相関が崩れる
        yhat = [float(v) for v in range(30, 0, -1)]
        y = [3, 2, 1, 100] + [0] * 26
        o = oof_backtest({"m": list(zip(yhat, y))}, n_quantiles=5)
        assert o["top_decile_rank_ic"] == {"mean": 1.0, "n": 1}

    def test_fewer_than_three_in_the_top_share_is_left_out(self):
        o = oof_backtest({"m": _ramp(20)}, n_quantiles=5)   # 20 社 → 上位 2 社
        assert o["top_decile_rank_ic"] == {"mean": None, "n": 0}


class TestPeriodSelection:
    def test_flat_prediction_period_is_left_out_of_every_metric(self):
        flat = [(0.0, 0.01 * i) for i in range(60)]
        o = oof_backtest({"m": flat}, n_quantiles=5)
        for k in KS:
            assert o["top_k"][str(k)] == EMPTY_K
        assert o["top_decile_rank_ic"] == {"mean": None, "n": 0}

    def test_thin_period_counts_only_for_small_k(self):
        o = oof_backtest({"a": _ramp(60), "b": _ramp(25)}, n_quantiles=5)   # b は 2K≤25 の K だけ
        assert o["top_k"]["5"]["n_periods"] == 2
        assert o["top_k"]["10"]["n_periods"] == 2
        assert o["top_k"]["30"]["n_periods"] == 1
        assert set(o["top_k_excess_return_by_period"]["30"]) == {"a"}
        # 期間平均は測れた期だけの平均
        assert o["top_k"]["5"]["excess_return"] == pytest.approx(((60 - 5) / 2 + (25 - 5) / 2) / 2, abs=1e-6)

    def test_empty_input_returns_the_full_shape(self):
        """M-3 は `oof_backtest({})` がフル形状を返すことに頼っている（macro_dlm.py）。"""
        o = oof_backtest({}, n_quantiles=5)
        for k in KS:
            assert o["top_k"][str(k)] == EMPTY_K
            assert o["top_k_excess_return_by_period"][str(k)] == {}
        assert o["top_decile_rank_ic"] == {"mean": None, "n": 0}

    def test_keys_survive_a_json_round_trip(self):
        """K のキーは文字列（画面の応答・夜間の診断値の JSON へ出しても同じ形）。"""
        o = oof_backtest({"m": _ramp(60)}, n_quantiles=5)
        for key in NEW_KEYS:
            assert json.loads(json.dumps(o[key])) == o[key]


class TestAnalysisPageReadsTheSameKeys:
    """分析画面の `_oofTopKHTML`（static/js/analysis.js・#1049）が読むキーと `oof_backtest` の戻りが一致する。

    Python 側でキーの名前を変えると、画面の表は例外を出さずに空欄（'-'）になる＝失敗として現れない。
    """

    @staticmethod
    def _js_body():
        import pathlib
        js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "js" / "analysis.js").read_text(
            encoding="utf-8")
        start = js.index("function _oofTopKHTML")
        return js[start:js.index("\n}\n", start)]

    def test_every_key_the_page_reads_is_returned(self):
        body = self._js_body()
        o = oof_backtest({"m": _ramp(60)}, n_quantiles=5)
        assert ".top_k" in body and ".top_decile_rank_ic" in body
        for field in ("excess_return", "precision", "ndcg", "n_periods"):
            assert f"r.{field}" in body, f"画面が {field} を読んでいない"
            assert field in o["top_k"]["5"], f"oof_backtest の top_k に {field} が無い"
        for field in ("mean", "n"):
            assert f"dic.{field}" in body
            assert field in o["top_decile_rank_ic"]


class TestExistingKeysUnchanged:
    def test_existing_keys_do_not_move(self, monkeypatch):
        """上位K社を測らないとき（K が空）と、既存のキーの値が同じ。"""
        rng = random.Random(1041)
        r, meta = {}, {}
        for p, ym in enumerate(["2020-01", "2020-04", "2020-07", "2020-10"]):
            r[ym] = [(round(rng.gauss(0.0, 1.0), 4), round(rng.gauss(0.0, 1.0), 4)) for _ in range(64)]
            meta[ym] = [(f"E{(i + 5 * p) % 80:02d}", "ABC"[i % 3]) for i in range(64)]
        kw = dict(n_quantiles=5, cost_bps=10.0, meta_by_ym=meta, rebalance_per_year=4)
        with_top_k = oof_backtest(r, **kw)
        monkeypatch.setattr(ms, "TOP_K_REFERENCE", ())
        without = oof_backtest(r, **kw)
        assert set(with_top_k) == set(without)
        assert with_top_k["top_k"]["30"]["n_periods"] == 4 and without["top_k"] == {}
        for key in set(with_top_k) - NEW_KEYS:
            assert with_top_k[key] == without[key], key
