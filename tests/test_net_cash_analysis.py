"""plugins/net_cash_analysis.py のユニットテスト。

清原達郎『わが投資術』式ネットキャッシュ指標の計算ロジックを検証する。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
from plugins.net_cash_analysis import (
    INVESTMENT_DISCOUNT,
    NC_RATIO_CHEAP,
    NC_RATIO_VERY_CHEAP,
    NCAV_BARGAIN_RATIO,
    SANITY_MAX_NC_RATIO,
    compute_ncav,
    compute_ncav_ratio,
    compute_net_cash,
    compute_nc_ratio,
    plugin,
)


class TestComputeNetCash:
    def test_basic_formula(self):
        # 流動資産 1000億 + 投資有価証券 500億 × 0.7 − 総負債 800億 = 1000 + 350 − 800 = 550億
        nc = compute_net_cash(1000e8, 500e8, 800e8)
        assert nc == pytest.approx(550e8)

    def test_zero_investment_securities_treated_as_ncav(self):
        # 投資有価証券=Noneは古い未収集レコード。0として扱う = グレアム流NCAV相当
        nc = compute_net_cash(1000e8, None, 600e8)
        assert nc == pytest.approx(400e8)

    def test_negative_net_cash(self):
        # 負債過多
        nc = compute_net_cash(100e8, 0, 500e8)
        assert nc == pytest.approx(-400e8)

    def test_both_required_fields_none_returns_none(self):
        # 流動資産も総負債も無いケースは計算不能
        assert compute_net_cash(None, 500e8, None) is None

    def test_only_current_assets_present(self):
        # 流動資産だけあれば計算する（総負債=0扱い）
        nc = compute_net_cash(500e8, None, None)
        assert nc == pytest.approx(500e8)

    def test_only_liabilities_present(self):
        # 負債だけある会社はネットキャッシュは大きくマイナス
        nc = compute_net_cash(None, None, 800e8)
        assert nc == pytest.approx(-800e8)

    def test_investment_discount_constant_is_70_percent(self):
        # 清原氏が指定する「投資有価証券は 0.7 倍評価」
        assert INVESTMENT_DISCOUNT == 0.7

    def test_investment_discount_applied(self):
        # 流動資産=0、投資有価証券 1000億、負債=0 → 700億になる
        nc = compute_net_cash(0, 1000e8, 0)
        assert nc == pytest.approx(700e8)


class TestComputeNcRatio:
    def test_basic_ratio(self):
        # net_cash=550億円, market_cap=500億円(=50000百万円)
        # ratio = 550e8 / (50000 * 1e6) = 550e8 / 5e10 = 1.1
        r = compute_nc_ratio(550e8, 50_000)
        assert r == pytest.approx(1.1)

    def test_unit_consistency(self):
        # market_cap=1000億円(=100000百万円), net_cash=1000億円
        # ratio = 1000e8 / (100000 × 1e6) = 1.0
        r = compute_nc_ratio(1000e8, 100_000)
        assert r == pytest.approx(1.0)

    def test_none_net_cash_returns_none(self):
        assert compute_nc_ratio(None, 50_000) is None

    def test_none_market_cap_returns_none(self):
        assert compute_nc_ratio(100e8, None) is None

    def test_zero_market_cap_returns_none(self):
        # ゼロ除算は防ぐ
        assert compute_nc_ratio(100e8, 0) is None

    def test_negative_net_cash_gives_negative_ratio(self):
        r = compute_nc_ratio(-200e8, 50_000)
        assert r is not None
        assert r < 0


class TestComputeNcav:
    def test_basic_formula(self):
        # NCAV = 流動資産 1000億 − 総負債 600億 = 400億（投資有価証券の0.7補正なし）
        ncav = compute_ncav(1000e8, 600e8)
        assert ncav == pytest.approx(400e8)

    def test_ncav_ignores_investment_securities(self):
        # compute_ncav は投資有価証券を引数に取らない＝清原式より保守的
        ncav = compute_ncav(500e8, 500e8)
        nc = compute_net_cash(500e8, 1000e8, 500e8)
        assert ncav == pytest.approx(0)
        # 同じ BS でも清原式は投資有価証券×0.7 を上乗せするため NCAV ≤ net_cash
        assert ncav < nc

    def test_only_current_assets(self):
        ncav = compute_ncav(500e8, None)
        assert ncav == pytest.approx(500e8)

    def test_both_none_returns_none(self):
        assert compute_ncav(None, None) is None

    def test_negative_when_liabilities_exceed_assets(self):
        ncav = compute_ncav(100e8, 800e8)
        assert ncav == pytest.approx(-700e8)


class TestComputeNcavRatio:
    def test_basic_ratio(self):
        # ncav=750億円, market_cap=500億円(=50000百万円) → 1.5（グレアムのネットネット境界）
        r = compute_ncav_ratio(750e8, 50_000)
        assert r == pytest.approx(1.5)

    def test_none_ncav_returns_none(self):
        assert compute_ncav_ratio(None, 50_000) is None

    def test_zero_market_cap_returns_none(self):
        assert compute_ncav_ratio(100e8, 0) is None


class TestExecuteActiveFilter:
    """execute() の is_active フィルタ（Issue #315・上場廃止銘柄は割安ランキング対象外）。"""

    def _metric(self, make_metric, **over):
        kw = dict(bs_current_assets=1000e8, bs_total_liabilities=200e8, market_cap=50_000.0)
        kw.update(over)
        return make_metric(**kw)

    def test_delisted_company_excluded(self, db, make_metric):
        db.add_all([
            self._metric(make_metric, edinet_code="E00001", is_active=False),
            self._metric(make_metric, edinet_code="E00002"),
        ])
        db.commit()
        res = asyncio.run(execute_plugin(plugin, {}, db))
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]

    def test_is_active_unset_still_included(self, db, make_metric):
        db.add(self._metric(make_metric, edinet_code="E00001"))
        db.commit()
        res = asyncio.run(execute_plugin(plugin, {}, db))
        assert res["count"] == 1


class TestThresholds:
    def test_very_cheap_threshold(self):
        # 清原氏の「現金で買える」水準
        assert NC_RATIO_VERY_CHEAP == 1.0

    def test_cheap_threshold(self):
        # 「半額バーゲン」水準
        assert NC_RATIO_CHEAP == 0.5

    def test_thresholds_ordered(self):
        # very_cheap > cheap であること（割安度の階段が崩れないように）
        assert NC_RATIO_VERY_CHEAP > NC_RATIO_CHEAP

    def test_ncav_bargain_is_graham_two_thirds_rule(self):
        # グレアムの2/3ルール: price < 2/3×NCAV ⟺ NCAV/price > 1.5
        assert NCAV_BARGAIN_RATIO == 1.5

    def test_sanity_cap_default(self):
        # データ品質ガードの既定上限
        assert SANITY_MAX_NC_RATIO == 5.0
