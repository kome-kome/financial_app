"""plugins/price_predictor.py のユニットテスト。

純粋関数（価格特徴量・日付・財務ラグ照合）が中心。numpy のみで DB 不要。
execute(): 株価履歴なし／学習サンプル不足の ValueError ガードを検証。
"""
import asyncio
import math
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.price_predictor import (
    FINANCIAL_LAG_DAYS,
    _add_days,
    _atr_ratio,
    _compute_price_features,
    _find_applicable_fin,
    _log_vol,
    _ma,
    _rsi,
    plugin,
)


# ── 純粋: 価格特徴量ヘルパー ─────────────────────────────────────────────────

class TestMovingAverage:
    def test_basic(self):
        assert _ma([2.0, 4.0, 6.0], 3) == 4.0

    def test_too_short_returns_none(self):
        assert _ma([1.0, 2.0], 3) is None


class TestRsi:
    def test_all_gains_is_100(self):
        assert _rsi([float(x) for x in range(1, 17)], 14) == 100.0

    def test_all_losses_is_0(self):
        assert _rsi([float(x) for x in range(16, 0, -1)], 14) == 0.0

    def test_too_short_returns_none(self):
        assert _rsi([float(x) for x in range(1, 15)], 14) is None


class TestLogVol:
    def test_constant_series_zero_vol(self):
        assert _log_vol([100.0] * 61, 60) == 0.0

    def test_too_short_returns_none(self):
        assert _log_vol([100.0] * 60, 60) is None


class TestAtrRatio:
    def test_known_value(self):
        highs = [101.0] * 15
        lows = [99.0] * 15
        closes = [100.0] * 15
        assert _atr_ratio(highs, lows, closes, 14) == pytest.approx(0.02)

    def test_too_short_returns_none(self):
        assert _atr_ratio([1.0] * 10, [1.0] * 10, [1.0] * 10, 14) is None


class TestAddDays:
    def test_crosses_month_boundary(self):
        assert _add_days("2023-03-31", FINANCIAL_LAG_DAYS) == "2023-05-15"


class TestFindApplicableFin:
    def _fin(self, period_end):
        return SimpleNamespace(period_end=period_end)

    def test_picks_latest_available(self):
        fins = [self._fin("2022-03-31"), self._fin("2023-03-31")]
        # 2023-06-30 では両方利用可（period_end + 45日 <= snap）→ 最新を選ぶ
        assert _find_applicable_fin(fins, "2023-06-30") is fins[1]

    def test_excludes_not_yet_available(self):
        fins = [self._fin("2022-03-31"), self._fin("2023-03-31")]
        # 2023-04-01 では 2023-03-31 はまだラグ未経過（avail 2023-05-15）→ 2022 を選ぶ
        assert _find_applicable_fin(fins, "2023-04-01") is fins[0]

    def test_none_when_no_applicable(self):
        fins = [self._fin("2022-03-31")]
        assert _find_applicable_fin(fins, "2021-01-01") is None

    def test_skips_records_without_period_end(self):
        fins = [self._fin(None), self._fin("2022-03-31")]
        assert _find_applicable_fin(fins, "2023-06-30") is fins[1]


class TestComputePriceFeatures:
    def _series(self, n):
        closes = [1000.0 + 2.0 * i + (i % 7) for i in range(n)]
        highs = [c + 5.0 for c in closes]
        lows = [c - 5.0 for c in closes]
        return closes, highs, lows

    def test_returns_dict_with_sufficient_history(self):
        closes, highs, lows = self._series(80)
        feats = _compute_price_features(closes, highs, lows, 79)
        assert feats is not None
        assert set(feats) == {"ma20_dev", "vol60", "rsi14", "atr_ratio"}

    def test_returns_none_when_too_short(self):
        closes, highs, lows = self._series(30)
        assert _compute_price_features(closes, highs, lows, 29) is None


# ── execute(): in-memory SQLite（ガードのみ・軽量）────────────────────────────

class TestExecute:
    def test_no_price_history_raises(self, db):
        with pytest.raises(ValueError):
            asyncio.run(plugin.execute({}, db))

    def test_insufficient_samples_raises(self, db, make_metric, make_price):
        # 1 社・100 営業日。月末スナップショットが学習に必要な最低数(10)に届かない。
        start = datetime(2023, 1, 1)
        prices = [
            make_price(
                edinet_code="E00001",
                trade_date=(start + timedelta(days=i)).strftime("%Y-%m-%d"),
                close=1000.0 + i + (i % 5) * 3.0,
                high=1005.0 + i, low=995.0 + i,
            )
            for i in range(100)
        ]
        db.add_all(prices)
        db.add(make_metric(edinet_code="E00001", period_end="2022-12-31",
                        per=15.0, pbr=1.2, roe=8.0))
        db.commit()
        with pytest.raises(ValueError):
            asyncio.run(plugin.execute({}, db))
