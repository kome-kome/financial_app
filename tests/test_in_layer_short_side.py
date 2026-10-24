"""上位層の売り側spread（Issue #1043・ADR-0069 の決定 4）のユニットテスト。

`plugins/macro_snapshots.py` の upper_layers / in_layer_short_side と、画面の並びの部品
display_scores / display_risk_map を合成の検体で確かめる。CLI（`scripts/sell_mu_source_bakeoff.py
--in-layer`）の配線は tests/test_sell_mu_source_bakeoff.py。
"""
import random

import pytest

from plugins import get_plugin
from plugins.macro_snapshots import (
    display_risk_map,
    display_scores,
    in_layer_short_side,
    upper_layers,
)

YMS = ["2020-01", "2020-04", "2020-07"]
N = 50                      # 上位 1/5 = 10 社 → 層の下位 1/5 = 2 社


def _ec(i):
    return f"E{i:02d}"


def _buy(n=N, yms=YMS):
    """買いのスコア＝社の番号（大きいほど買う）。どの期も上位 10 社は E40〜E49。"""
    return {(ym, _ec(i)): float(i) for ym in yms for i in range(n)}


def _y(seed=0, n=N, yms=YMS):
    rng = random.Random(seed)
    return {(ym, _ec(i)): rng.gauss(0.0, 0.1) for ym in yms for i in range(n)}


def _domain(keys):
    out = {}
    for ym, ec in keys:
        out.setdefault(ym, set()).add(ec)
    return out


class TestUpperLayers:
    def test_layer_is_the_top_fifth_of_the_previous_period(self):
        layers = upper_layers(_buy(), _domain(_buy()))
        assert sorted(layers) == YMS[1:]          # 最初の期は前の期が無い
        for ym, prev in zip(YMS[1:], YMS):
            assert layers[ym]["prev_ym"] == prev
            assert layers[ym]["n_ranked"] == N
            assert layers[ym]["members"] == {_ec(i) for i in range(40, 50)}

    def test_layer_does_not_look_at_period_t(self):
        """t の買いのスコアを逆順にしても、t の y を入れ替えても層は変わらない（t−1 の並びだけで決まる）。"""
        buy = _buy()
        y = _y()
        base = in_layer_short_side(buy, {"s": y}, y)
        flipped_buy = {k: (-v if k[0] == "2020-07" else v) for k, v in buy.items()}
        vals = [y[("2020-07", _ec(i))] for i in range(N)]
        random.Random(1).shuffle(vals)
        shuffled_y = {**y, **{("2020-07", _ec(i)): v for i, v in enumerate(vals)}}
        layers = upper_layers(buy, _domain(buy))
        assert upper_layers(flipped_buy, _domain(buy)) == layers
        assert upper_layers(buy, _domain(shuffled_y)) == layers
        other = in_layer_short_side(flipped_buy, {"s": shuffled_y}, shuffled_y)
        assert other["periods"] == base["periods"]

    def test_companies_without_a_buy_score_are_not_ranked(self):
        """R が取れず買いのスコアが無い社は t−1 の順位付けから外れる（共通域の社数は変わらない）。"""
        buy = {k: v for k, v in _buy().items() if not (k[0] == "2020-01" and k[1] == _ec(49))}
        y = _y()
        res = in_layer_short_side(buy, {"s": y}, y)
        p = res["periods"]["2020-04"]
        assert (p["n_common"], p["n_ranked"]) == (N, N - 1)

    def test_previous_period_too_small_is_skipped(self):
        """t−1 の社数が 2×5 未満なら層を作らない（`quantile_period_ok`）。"""
        buy = _buy(n=9)
        assert upper_layers(buy, _domain(buy)) == {}


class TestInLayerShortSide:
    def test_perfect_sell_ordering_is_the_maximum_and_reverse_is_negative(self):
        y = _y(seed=3)
        res = in_layer_short_side(_buy(), {"perfect": y, "reverse": {k: -v for k, v in y.items()}}, y)
        for ym in YMS[1:]:
            layer_y = sorted(y[(ym, _ec(i))] for i in range(40, 50))
            mean = sum(layer_y) / len(layer_y)
            best = mean - sum(layer_y[:2]) / 2       # 層の中で y が最も低い 2 社を売る
            worst = mean - sum(layer_y[-2:]) / 2
            assert res["by_condition"]["perfect"]["by_period"][ym] == pytest.approx(best)
            assert res["by_condition"]["reverse"]["by_period"][ym] == pytest.approx(worst)
            assert worst < 0 < best
        perfect = res["by_condition"]["perfect"]
        assert perfect["n_periods"] == 2 and perfect["hit_rate"] == 1.0
        assert res["by_condition"]["reverse"]["hit_rate"] == 0.0

    def test_rows_outside_the_common_domain_drop_from_every_condition(self):
        """片方の売りの条件にしか無い行は、両方の条件から落ちる（母集団がそろう・ADR-0045）。

        E50〜E54 は買いのスコアが最大なので、数えれば層に入る。条件 a にだけ足しても、
        どちらの条件もその行を手で除いたデータと同じ値・同じ層になる。
        """
        y = _y(seed=5)
        a = {k: v for k, v in y.items()}
        b = {k: -v for k, v in y.items()}
        expected = in_layer_short_side(_buy(), {"a": a, "b": b}, y)

        extra = {(ym, _ec(i)) for ym in YMS for i in range(50, 55)}
        buy = {**_buy(), **{k: 100.0 + int(k[1][1:]) for k in extra}}
        y_x = {**y, **{k: 1.0 for k in extra}}
        a_x = {**a, **{k: -1.0 for k in extra}}
        got = in_layer_short_side(buy, {"a": a_x, "b": b}, y_x)

        assert got["periods"] == expected["periods"]
        assert got["n_common_rows"] == expected["n_common_rows"] == len(YMS) * N
        for name in ("a", "b"):
            assert got["by_condition"][name]["by_period"] == pytest.approx(
                expected["by_condition"][name]["by_period"])

    def test_layer_too_small_gives_no_value(self):
        """層が 10 社未満の期は値を出さない（20 社なら層は 4 社）。"""
        buy, y = _buy(n=20), _y(n=20)
        res = in_layer_short_side(buy, {"s": y}, y)
        assert res["periods"]["2020-04"]["n_layer"] == 4
        assert res["by_condition"]["s"] == {"mean": None, "hit_rate": None, "n_periods": 0,
                                            "by_period": {}}

    def test_constant_sell_key_in_the_layer_is_skipped(self):
        """層の中の売りのキーが全社同じ期は、社の順で値が決まるので外す（#977 と同じ）。"""
        y = _y()
        flat = {k: 0.0 for k in y}
        res = in_layer_short_side(_buy(), {"flat": flat, "s": y}, y)
        assert res["by_condition"]["flat"]["n_periods"] == 0
        assert res["by_condition"]["s"]["n_periods"] == 2


class TestDisplayParts:
    def test_display_scores_subtracts_raw_r(self):
        resid = {"2020-01": [(0.5, 0.1), (0.2, 0.0), (0.3, 0.2)]}
        meta = {"2020-01": [("A", None), ("B", None), ("C", None)]}
        u, m, dropped = display_scores(resid, meta, {("2020-01", "A"): 0.4, ("2020-01", "B"): 0.1}, 1.0)
        assert dropped == 1
        assert m == {"2020-01": [("A", None), ("B", None)]}
        assert [p[0] for p in u["2020-01"]] == pytest.approx([0.1, 0.1])

    def test_display_scores_drops_everything_without_r(self):
        """R が1社も無いときに μ̂ の並びをそのまま返さない（`apply_risk_axis` の空は恒等変換）。"""
        resid = {"2020-01": [(0.5, 0.1), (0.2, 0.0)]}
        meta = {"2020-01": [("A", None), ("B", None)]}
        assert display_scores(resid, meta, {}, 1.0) == ({}, {}, 2)

    def test_display_risk_map_skips_r_when_lambda_is_zero(self):
        """λ=0 の M-6 は R を引かない（DB も株価も読まない）。"""
        lam, _axis, rmap = display_risk_map(get_plugin("macro_enet").params_schema(),
                                            {"2020-01": [("A", None)]}, prices_by_co=None, db=None)
        assert lam == 0.0 and rmap == {}
