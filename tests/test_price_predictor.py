"""plugins/price_predictor.py のユニットテスト。

純粋関数（価格特徴量・日付・財務ラグ照合）が中心。numpy のみで DB 不要。
execute(): 株価履歴なし／学習サンプル不足の ValueError ガードを検証。
"""
import asyncio
import math
import os
import sys
from collections import namedtuple
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins import execute_plugin
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
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_insufficient_samples_raises(self, db, make_metric, make_weekly):
        # 1 社・100 週（週次刻み）。学習に必要な月末サンプル最低数(10)に届かない。
        start = datetime(2023, 1, 2)   # 月曜
        prices = [
            make_weekly(
                edinet_code="E00001",
                trade_date=(start + timedelta(days=i * 7)).strftime("%Y-%m-%d"),
                close_last=1000.0 + i + (i % 5) * 3.0,
            )
            for i in range(100)
        ]
        db.add_all(prices)
        db.add(make_metric(edinet_code="E00001", period_end="2022-12-31",
                        per=15.0, pbr=1.2, roe=8.0))
        db.commit()
        with pytest.raises(ValueError):
            asyncio.run(execute_plugin(plugin, {}, db))

    def test_execute_with_c2_intensity_features(self, db, make_metric, make_weekly):
        # rd_intensity / da_intensity を選択して coerce→execute が通り、特徴量重みと
        # 予測結果が返ること（FinancialMetric 経由の C2 intensity 結線の通し確認）。
        start = datetime(2023, 1, 2)  # 月曜
        # (edinet_code, rd_intensity, da_intensity, 価格スロープ) — (rd,da) は非共線に配置
        specs = [
            ("E00001", 2.0, 6.0, 1.3),
            ("E00002", 7.0, 3.0, 0.7),
            ("E00003", 4.0, 9.0, 1.9),
            ("E00004", 9.0, 5.0, 1.1),
        ]
        n_weeks = 90  # min_rows = 60 + horizon(5) = 65 を超える長さ
        for ec, rd, da, slope in specs:
            db.add_all([
                make_weekly(
                    edinet_code=ec,
                    trade_date=(start + timedelta(days=i * 7)).strftime("%Y-%m-%d"),
                    close_last=1000.0 + i * slope + (i % 6) * 4.0,
                )
                for i in range(n_weeks)
            ])
            db.add(make_metric(edinet_code=ec, period_end="2022-12-31",
                               rd_intensity=rd, da_intensity=da))
        db.commit()

        res = asyncio.run(execute_plugin(
            plugin,
            {"horizon": 5, "use_price_features": False,
             "features": ["rd_intensity", "da_intensity"]},
            db,
        ))
        assert res["n_train_samples"] >= 10
        assert set(res["feature_weights"]) == {"rd_intensity", "da_intensity"}
        assert res["results"]  # 最新スナップショットのスコアリング結果（4社）


# ── 財務特徴量オプション定数（C2 intensity 結線の回帰防止）─────────────────────

class TestFinFeatureOptions:
    def test_c2_intensity_options_present(self):
        # C2 列由来の無次元 intensity が選択肢に結線されていること
        from plugins.price_predictor import FIN_FEATURE_LABELS, FIN_FEATURE_OPTIONS
        vals = {o["value"] for o in FIN_FEATURE_OPTIONS}
        assert {"rd_intensity", "da_intensity"} <= vals
        # ラベルは FIN_FEATURE_OPTIONS から自動派生される
        assert FIN_FEATURE_LABELS["rd_intensity"]
        assert FIN_FEATURE_LABELS["da_intensity"]

    def test_c2_intensity_not_in_defaults(self):
        # 欠損縮小回避のためデフォルト財務特徴量（per/pbr/roe）には含めない
        from plugins.price_predictor import DEFAULT_FIN_FEATURES
        assert "rd_intensity" not in DEFAULT_FIN_FEATURES
        assert "da_intensity" not in DEFAULT_FIN_FEATURES


# ── _build_snapshots の数値テスト ──────────────────────────────────────────────

_PX = namedtuple("_PX", "edinet_code trade_date close high low")


class TestBuildSnapshots:
    """月次スナップショット構築ロジックの境界値テスト（DB不要・直接呼び出し）。"""

    def _prices(self, n: int, start: str = "2022-01-03") -> list:
        """n 件の週次価格スタブを生成する（月曜始まり）。"""
        base = date.fromisoformat(start)
        return [
            _PX("E00001", (base + timedelta(weeks=i)).isoformat(),
                1000.0 + i, 1005.0 + i, 995.0 + i)
            for i in range(n)
        ]

    def _fin_rec(self, period_end: str, per: float = 15.0):
        return SimpleNamespace(
            period_end=period_end,
            per=per,
            sec_code="1001",
            company_name="テスト",
            industry="情報・通信業",
        )

    def _company(self):
        return SimpleNamespace(sec_code="1001", name="テスト", industry="情報・通信業")

    def test_month_end_indices_detected_as_ym_keys(self):
        """月境界を持つ週次列から samples_by_ym キーが YYYY-MM 形式で正しく生成される。"""
        prices = self._prices(80)
        prices_by_co = {"E00001": prices}
        fin_by_co   = {"E00001": [self._fin_rec("2021-12-31")]}
        companies   = {"E00001": self._company()}

        samples_by_ym, _, _ = plugin._build_snapshots(
            prices_by_co, fin_by_co, companies,
            use_price=False, fin_features=["per"], horizon=5,
        )

        # snap_idx >= 60 の月末スナップ点が YYYY-MM キーとして格納される
        assert len(samples_by_ym) >= 1
        for ym in samples_by_ym:
            assert len(ym) == 7 and ym[4] == "-"  # YYYY-MM 形式チェック
        # キーは prices 内の実在する年月のみ
        valid_yms = {p.trade_date[:7] for p in prices}
        assert all(ym in valid_yms for ym in samples_by_ym)

    def test_horizon_exceeds_n_no_future_samples(self):
        """snap_idx + horizon >= n の場合は samples_by_ym に追加されない。"""
        horizon = 5
        n = 60 + horizon  # 全 snap_idx（>= 60）で has_future = False
        prices = self._prices(n)
        prices_by_co = {"E00001": prices}
        fin_by_co   = {"E00001": [self._fin_rec("2021-12-31")]}
        companies   = {"E00001": self._company()}

        samples_by_ym, current_snaps, _ = plugin._build_snapshots(
            prices_by_co, fin_by_co, companies,
            use_price=False, fin_features=["per"], horizon=horizon,
        )

        # has_future = False の全スナップはサンプルに追加されない
        assert len(samples_by_ym) == 0
        # is_current（最終行）は current_snaps には格納される
        assert "E00001" in current_snaps

    def test_current_snaps_stores_only_latest_snapshot(self):
        """is_current フラグが立つ最新スナップのみ current_snaps に格納される。"""
        prices = self._prices(80)
        prices_by_co = {"E00001": prices}
        fin_by_co   = {"E00001": [self._fin_rec("2021-12-31")]}
        companies   = {"E00001": self._company()}

        _, current_snaps, _ = plugin._build_snapshots(
            prices_by_co, fin_by_co, companies,
            use_price=False, fin_features=["per"], horizon=5,
        )

        # 最新スナップショット 1 社分のみ格納
        assert list(current_snaps.keys()) == ["E00001"]
        feat_row, info = current_snaps["E00001"]
        assert isinstance(feat_row, list) and len(feat_row) == 1  # fin_features=["per"] のみ
        assert "per" in info["fin_features"]
