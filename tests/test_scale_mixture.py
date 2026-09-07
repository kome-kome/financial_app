"""1つの価格列に2つのスケールが混ざる事故を防ぐ仕組みのテスト（#620）。

この壊れ方は**エラーとして現れない**——どちらの値も妥当な株価で、upsert は成功し、
行数も鮮度も正常に見える。したがって「書かなかったこと」と「往復の帯を見つけること」を
テストで押さえるしかない。
"""
import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from collector_prices import (
    _pair_roundtrip_steps, _steps_from_closes, collect_stock_price_history_jquants,
)
from collector_utils import rounding_tolerance, same_price_scale
from scripts.repair_scale_mixture import confirm_official_scale


class TestSamePriceScale:
    """丸め差と本物のスケール差を分ける（判定は収集側と修復側で共有する）。"""

    def test_rounding_difference_is_same_scale(self):
        assert same_price_scale(2861.0, 2861.4) is True

    def test_adjustment_difference_is_not(self):
        # #620 の実測: E32779 の 2026-05-29（Yahoo=C=2861.0・公式 AdjC=2384.2）
        assert same_price_scale(2861.0, 2384.2) is False

    def test_low_priced_stock_gets_a_wider_tolerance(self):
        # 丸め幅 1円は株価 21円では 4.8% にあたる（#466 の E01300）
        assert rounding_tolerance(21.0, 24.7) == pytest.approx(1.0 / 21.0)
        assert same_price_scale(21.0, 21.9) is True

    def test_non_positive_is_never_the_same_scale(self):
        assert same_price_scale(0.0, 100.0) is False
        assert same_price_scale(None, 100.0) is False


class TestRoundtripBands:
    """「飛んで数日で戻る」帯を、実際の値動きと分けて拾う。"""

    # #620 の実測（E32779 3482 ロードスターキャピタル）。帯の中は公式 AdjC が入っており、
    # 両端の段差は企業イベントでは説明できない（11営業日で戻る分割は存在しない）。
    _MIXED = [
        ("2026-05-28", 2845.0), ("2026-05-29", 2861.0),
        ("2026-06-01", 2306.7), ("2026-06-02", 2283.3), ("2026-06-03", 2285.0),
        ("2026-06-04", 2247.5), ("2026-06-05", 2284.2), ("2026-06-08", 2265.0),
        ("2026-06-09", 2296.7), ("2026-06-10", 2346.7), ("2026-06-11", 2355.0),
        ("2026-06-12", 2449.2), ("2026-06-15", 2587.5),
        ("2026-06-16", 2938.0), ("2026-06-17", 2960.0),
    ]

    def _bands(self, rows):
        return _pair_roundtrip_steps(_steps_from_closes(rows))

    def test_finds_the_mixed_band(self):
        bands = self._bands(self._MIXED)
        assert len(bands) == 1
        assert (bands[0]["start"], bands[0]["end"]) == ("2026-06-01", "2026-06-15")
        assert bands[0]["ratio_out"] < 1.0 < bands[0]["ratio_back"]

    def test_ignores_a_one_way_move(self):
        # #620 の E02293（6966 三井ハイテック）の形。上げて戻らない＝本物の値動き。
        assert self._bands([("2026-06-15", 808.0), ("2026-06-16", 1108.0),
                            ("2026-06-17", 1120.0), ("2026-06-18", 1130.0)]) == []

    def test_ignores_a_one_day_round_trip(self):
        # 1日下げて翌日戻す形は値動き。catchup が作る帯は窓ぶんの長さを必ず持つ。
        assert self._bands([("2026-06-15", 1000.0), ("2026-06-16", 800.0),
                            ("2026-06-17", 1000.0)]) == []

    def test_ignores_a_round_trip_that_is_too_far_apart(self):
        assert self._bands([("2026-05-01", 1000.0), ("2026-05-04", 800.0),
                            ("2026-06-02", 1000.0)]) == []

    def test_ignores_a_move_that_does_not_come_back(self):
        # 下げた 20% のうち 5% しか戻らない＝往復ではない（積が 1.0 から離れる）
        assert self._bands([("2026-06-01", 1000.0), ("2026-06-04", 800.0),
                            ("2026-06-12", 840.0)]) == []

    def test_rounding_noise_is_not_a_step(self):
        assert _steps_from_closes([("2026-06-01", 1000.0), ("2026-06-02", 1005.0)]) == []

    def test_picks_the_best_partner_not_the_first(self):
        """相手が複数成立するとき、最も戻り切っている方（積が 1.0 に近い方）と組む。"""
        rows = [("2026-06-01", 1000.0), ("2026-06-05", 800.0),
                ("2026-06-10", 880.0),    # ここで組むと積 0.88（先着ならこちら）
                ("2026-06-15", 1010.0)]   # ここで組むと積 0.918（こちらが最良）
        bands = self._bands(rows)
        assert len(bands) == 1
        assert (bands[0]["start"], bands[0]["end"]) == ("2026-06-05", "2026-06-10")


class TestCatchupSkipsMismatchedScale:
    """J-Quants catchup は AdjC≠C の行を書かない（本丸）。"""

    _MON = date(2024, 1, 8)

    def _row(self, *, c: float, adjc: float) -> dict:
        return {"Code": "10010", "Date": "2024-01-08",
                "O": c, "H": c, "L": c, "C": c, "Vo": 5000.0,
                "AdjO": adjc, "AdjH": adjc, "AdjL": adjc, "AdjC": adjc, "AdjVo": 6000.0,
                "AdjFactor": 1.0}

    def _run(self, db, row):
        with patch("collector_prices._jquants_fetch_date",
                   new_callable=AsyncMock, return_value=[row]):
            with patch("collector_prices.record_prices_batch", return_value=1) as batch:
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                        with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                            result = asyncio.run(collect_stock_price_history_jquants(
                                db, date_from=self._MON, date_to=self._MON))
        return result, batch

    def _add_company(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="テスト"))
        db.commit()

    def test_writes_when_the_adjustment_matches(self, db, make_company):
        self._add_company(db, make_company)
        result, batch = self._run(db, self._row(c=1005.0, adjc=1005.0))
        batch.assert_called_once()
        assert result["scale_mismatch"] == 0
        assert result["scale_mismatch_companies"] == []

    def test_does_not_write_when_the_adjustment_differs(self, db, make_company):
        self._add_company(db, make_company)
        result, batch = self._run(db, self._row(c=2768.0, adjc=2306.7))
        batch.assert_not_called()
        assert result["scale_mismatch"] == 1
        assert result["scale_mismatch_companies"] == ["E00001"]

    def test_rounding_difference_still_gets_written(self, db, make_company):
        """1円の丸め差で公式値を捨てない（捨てると是正の機会を全社で失う）。"""
        self._add_company(db, make_company)
        result, batch = self._run(db, self._row(c=1005.0, adjc=1005.4))
        batch.assert_called_once()
        assert result["scale_mismatch"] == 0


class TestConfirmOfficialScale:
    """候補を確定させる突合（形だけでは本物を選べない・実測 234社中2社）。"""

    _BANDS = [{"start": "2026-06-01", "end": "2026-06-15"}]

    def test_confirms_when_db_equals_official_adjusted(self):
        rows = [{"Date": "2026-06-01", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-06-01": 2306.7}, self._BANDS)
        assert ok is True
        assert "1/1" in why

    def test_rejects_when_there_is_no_adjustment_difference(self):
        """E02293 型: 調整差が無い＝誰が書いても同じ値＝往復は本物の値動き。"""
        rows = [{"Date": "2026-06-01", "C": 808.0, "AdjC": 808.0}]
        ok, why = confirm_official_scale(rows, {"2026-06-01": 808.0}, self._BANDS)
        assert ok is False
        assert "AdjC≠C の日が無い" in why

    def test_rejects_when_db_does_not_hold_the_official_value(self):
        rows = [{"Date": "2026-06-01", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-06-01": 2768.0}, self._BANDS)
        assert ok is False
        assert "AdjC と一致しない" in why

    def test_ignores_days_outside_the_band(self):
        rows = [{"Date": "2026-05-20", "C": 2768.0, "AdjC": 2306.7}]
        ok, why = confirm_official_scale(rows, {"2026-05-20": 2306.7}, self._BANDS)
        assert ok is False

    def test_rejects_when_official_returned_nothing(self):
        ok, why = confirm_official_scale([], {}, self._BANDS)
        assert ok is False
        assert "1行も取得できなかった" in why
