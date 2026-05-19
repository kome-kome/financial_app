"""plugins/price_predictor.py の純粋ヘルパー＋execute() テスト"""
import math
from datetime import date, timedelta

import pytest

from plugins.price_predictor import (
    PricePredictorPlugin,
    _add_days,
    _atr_ratio,
    _find_applicable_fin,
    _log_vol,
    _ma,
    _rsi,
    _compute_price_features,
    FINANCIAL_LAG_DAYS,
)
from tests.conftest import make_company, make_record, make_price_history


@pytest.fixture
def plugin():
    return PricePredictorPlugin()


# ─── ヘルパー関数（Pure Python） ─────────────────────────────────────

class TestHelpers:
    def test_ma_simple(self):
        assert _ma([1.0, 2.0, 3.0, 4.0], 4) == 2.5
        assert _ma([1.0, 2.0, 3.0, 4.0], 2) == 3.5  # 末尾 [3,4]

    def test_ma_insufficient_returns_none(self):
        assert _ma([1.0, 2.0], 5) is None

    def test_log_vol_constant_returns_zero(self):
        """価格一定 → ログリターン=0 → 標準偏差=0"""
        closes = [100.0] * 70
        assert _log_vol(closes, 60) == pytest.approx(0.0, abs=1e-9)

    def test_log_vol_insufficient_returns_none(self):
        assert _log_vol([100.0] * 30, 60) is None

    def test_rsi_only_gains(self):
        """純粋な上昇トレンドで RSI = 100"""
        closes = [100.0 + i for i in range(20)]  # 単調増加
        rsi = _rsi(closes, 14)
        assert rsi == 100.0

    def test_rsi_only_losses(self):
        """純粋な下落トレンドで RSI = 0（loss>0, gain=0）"""
        closes = [200.0 - i for i in range(20)]  # 単調減少
        rsi = _rsi(closes, 14)
        assert rsi == 0.0

    def test_rsi_insufficient_returns_none(self):
        assert _rsi([100.0, 101.0], 14) is None

    def test_atr_ratio_basic(self):
        # high - low = 10、close=100 → ATR/close = 0.1
        closes = [100.0] * 20
        highs = [105.0] * 20
        lows  = [95.0] * 20
        atr = _atr_ratio(highs, lows, closes, 14)
        assert atr == pytest.approx(0.1, abs=1e-9)

    def test_atr_ratio_insufficient_returns_none(self):
        assert _atr_ratio([1.0], [1.0], [1.0], 14) is None

    def test_add_days(self):
        assert _add_days("2024-03-31", 45) == "2024-05-15"
        assert _add_days("2024-01-01", -1) == "2023-12-31"

    def test_find_applicable_fin_returns_latest_applicable(self):
        """snap_date 時点で公表済（period_end + 45日 <= snap）の最新 FY"""
        class FakeFR:
            def __init__(self, period_end):
                self.period_end = period_end

        recs = [
            FakeFR("2022-03-31"),
            FakeFR("2023-03-31"),
            FakeFR("2024-03-31"),
        ]
        # 2024-06-30 時点で 2024-03-31 + 45 = 2024-05-15 → 利用可能
        result = _find_applicable_fin(recs, "2024-06-30")
        assert result.period_end == "2024-03-31"

        # 2024-04-15 時点では 2024-03-31 はまだ未公表（lag45日）→ 一つ前
        result = _find_applicable_fin(recs, "2024-04-15")
        assert result.period_end == "2023-03-31"

    def test_find_applicable_fin_returns_none_when_too_early(self):
        class FakeFR:
            def __init__(self, period_end):
                self.period_end = period_end
        recs = [FakeFR("2024-03-31")]
        # 2022 時点では何も適用不可
        assert _find_applicable_fin(recs, "2022-01-01") is None

    def test_compute_price_features_normal(self):
        # 80日分の単調増加データ
        closes = [100.0 + i * 0.5 for i in range(80)]
        highs  = [c * 1.01 for c in closes]
        lows   = [c * 0.99 for c in closes]
        result = _compute_price_features(closes, highs, lows, 79)
        assert result is not None
        assert "ma20_dev" in result
        assert "vol60" in result
        assert "rsi14" in result
        assert "atr_ratio" in result
        # 単調増加なので ma20_dev > 0
        assert result["ma20_dev"] > 0
        # 単調増加なので RSI が高い
        assert result["rsi14"] > 50

    def test_compute_price_features_insufficient_returns_none(self):
        closes = [100.0] * 30  # vol60 に不足
        result = _compute_price_features(closes, closes, closes, 29)
        assert result is None


# ─── execute() の最小ケース ───────────────────────────────────────

class TestPricePredictorExecute:
    async def test_raises_when_no_price_history(self, plugin, db):
        with pytest.raises(ValueError, match="株価履歴データがありません"):
            await plugin.execute({}, db)

    async def test_raises_when_insufficient_samples(self, plugin, db):
        """株価履歴あり・財務なし → 学習サンプル不足"""
        make_company(db, edinet_code="E001", sec_code="0001")
        make_price_history(db, "E001", "0001", start_date="2024-01-01",
                           n_days=100, base_close=1000.0, drift_per_day=1.0)
        # 財務レコードなし → fin_recs 空でサンプル収集できず
        with pytest.raises(ValueError, match="学習サンプルが不足"):
            await plugin.execute({"horizon": 5}, db)

    async def test_raises_when_no_features_selected(self, plugin, db):
        # `features=[]` は `or DEFAULT_FIN_FEATURES` でフォールバックされるため、
        # カンマだけの文字列を渡し split 後に空 list となるパスでバリデーションを発火させる
        with pytest.raises(ValueError, match="価格特徴量か財務特徴量"):
            await plugin.execute(
                {"use_price_features": False, "features": ","}, db
            )
