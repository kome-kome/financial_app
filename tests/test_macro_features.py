"""tests/test_macro_features.py — M-1 Phase A: get_macro_features / get_momentum_return"""
import math
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugins.utils import get_macro_features, get_momentum_return
# 正本マップは macro_risk_return._MACRO_MAP（#218 で utils の重複定義を統合）。
from plugins.macro_risk_return import _MACRO_MAP as _MACRO_FEATURE_MAP


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_price_row(trade_date: str, close: float):
    return SimpleNamespace(trade_date=trade_date, close=close)


def _make_macro_row(series_code: str, trade_date: str, close: float):
    return SimpleNamespace(series_code=series_code, trade_date=trade_date, close=close)


def _build_mock_db(rows: list):
    """db.query(...).filter(...).order_by(...).all() → rows を返すモック。"""
    query_mock = MagicMock()
    query_mock.filter.return_value = query_mock
    query_mock.order_by.return_value = query_mock
    query_mock.all.return_value = rows
    db = MagicMock()
    db.query.return_value = query_mock
    return db


# ── _MACRO_FEATURE_MAP ───────────────────────────────────────────────────────

def test_macro_feature_map_keys():
    assert "macro_usdjpy_yoy"   in _MACRO_FEATURE_MAP
    assert "macro_sp500_yoy"    in _MACRO_FEATURE_MAP
    assert "macro_us10y_zscore" in _MACRO_FEATURE_MAP
    # #218 フェーズ1: 収集済みの EURJPY/WTI/GOLD を M-1 特徴量として公開
    assert "macro_eurjpy_yoy"   in _MACRO_FEATURE_MAP
    assert "macro_wti_yoy"      in _MACRO_FEATURE_MAP
    assert "macro_gold_yoy"     in _MACRO_FEATURE_MAP
    # #218 フェーズ1: VIX/DXY/US5Y/US30Y は collect-macro.yml の Actions 実行で蓄積実証後に公開
    assert "macro_vix_zscore"   in _MACRO_FEATURE_MAP
    assert "macro_dxy_yoy"      in _MACRO_FEATURE_MAP
    assert "macro_us5y_zscore"  in _MACRO_FEATURE_MAP
    assert "macro_us30y_zscore" in _MACRO_FEATURE_MAP
    # macro_jp10y_zscore は stooq/Yahoo Finance で取得不可のため未公開
    assert "macro_jp10y_zscore" not in _MACRO_FEATURE_MAP
    # #250 日本マクロのリバランス: 実体経済指標4種＋TOPIX を公開
    assert _MACRO_FEATURE_MAP["macro_jp_real_gdp_yoy"]     == ("JP_REAL_GDP",  "yoy")
    assert _MACRO_FEATURE_MAP["macro_jp_unemp_zscore"]     == ("JP_UNEMP",     "zscore")
    # macro_jp_ip_yoy は JPNPROINDMISMEI 凍結のため除外中 (#253)
    assert "macro_jp_ip_yoy" not in _MACRO_FEATURE_MAP
    assert _MACRO_FEATURE_MAP["macro_jp_trade_bal_zscore"] == ("JP_TRADE_BAL", "zscore")
    assert _MACRO_FEATURE_MAP["macro_topix_yoy"]           == ("TOPIX",        "yoy")
    for fname, (scode, ttype) in _MACRO_FEATURE_MAP.items():
        assert ttype in ("yoy", "zscore"), f"{fname} の transform が不正"


# ── get_macro_features ───────────────────────────────────────────────────────

class TestGetMacroFeatures:

    def _usdjpy_rows(self, ref: date, n_days_history: int = 400):
        """USDJPY の合成日次データ（n_days_history 日分）を生成する。
        1年前ウィンドウ（ref-395 〜 ref-335）を全て 130.0、
        現在ウィンドウ（ref-30 〜 ref-0）を全て 150.0 とし YoY≈+15.4% を期待値とする。
        切り替えは day=330 前後（1年前ウィンドウの端 ref-335 よりも安全に遠い）。
        """
        rows = []
        for i in range(n_days_history, 0, -1):
            d = (ref - timedelta(days=i)).isoformat()
            close = 130.0 if i > 330 else 150.0
            rows.append(_make_macro_row("USDJPY", d, close))
        return rows

    def test_yoy_basic(self):
        ref = date(2025, 6, 1)
        rows = self._usdjpy_rows(ref)
        db = _build_mock_db(rows)

        # db をモック — MacroData は実クラスを使う（SQLAlchemy 比較式が必要なため patch 不可）
        result = get_macro_features(db, ref.isoformat(), ["macro_usdjpy_yoy"])

        val = result.get("macro_usdjpy_yoy")
        assert val is not None
        # (150 - 130) / 130 ≈ 0.1538
        assert abs(val - (150 - 130) / 130) < 0.01

    def test_unknown_feature_returns_none(self):
        db = _build_mock_db([])
        result = get_macro_features(db, "2025-06-01", ["macro_unknown_xyz"])
        assert result["macro_unknown_xyz"] is None

    def test_empty_db_returns_none(self):
        db = _build_mock_db([])
        result = get_macro_features(db, "2025-06-01", ["macro_usdjpy_yoy"])
        assert result["macro_usdjpy_yoy"] is None

    def test_no_data_in_window_returns_none(self):
        """直近 window_days 以内に行がない → None。"""
        ref = date(2025, 6, 1)
        # 直近30日より古い行だけ
        rows = [_make_macro_row("USDJPY", "2024-12-01", 150.0)]
        db = _build_mock_db(rows)
        result = get_macro_features(db, ref.isoformat(), ["macro_usdjpy_yoy"])
        assert result["macro_usdjpy_yoy"] is None

    def test_no_prev_year_data_returns_none(self):
        """直近データはあるが1年前データがない → YoY は None。"""
        ref = date(2025, 6, 1)
        rows = [_make_macro_row("USDJPY", "2025-05-20", 150.0)]
        db = _build_mock_db(rows)
        result = get_macro_features(db, ref.isoformat(), ["macro_usdjpy_yoy"])
        assert result["macro_usdjpy_yoy"] is None

    def test_zscore_basic(self):
        """zscore: 歴史データの平均から乖離を標準偏差で割る。"""
        ref = date(2025, 6, 1)
        # 1825日（5年）分の合成データ: 値は全て 1.0 → 直近30日を 2.0 に
        rows = []
        for i in range(1825, 0, -1):
            d = (ref - timedelta(days=i)).isoformat()
            close = 2.0 if i <= 30 else 1.0
            rows.append(_make_macro_row("US10Y", d, close))
        db = _build_mock_db(rows)
        result = get_macro_features(db, ref.isoformat(), ["macro_us10y_zscore"])
        val = result.get("macro_us10y_zscore")
        assert val is not None
        assert val > 0  # 現在値 > 平均 → Z > 0

    def test_zscore_insufficient_data_returns_none(self):
        """20件未満は None。"""
        ref = date(2025, 6, 1)
        rows = [_make_macro_row("US10Y", (ref - timedelta(days=i)).isoformat(), 1.0)
                for i in range(1, 10)]
        db = _build_mock_db(rows)
        result = get_macro_features(db, ref.isoformat(), ["macro_us10y_zscore"])
        assert result["macro_us10y_zscore"] is None

    def test_multiple_features(self):
        """複数特徴量を1度に取得しても全て返す。"""
        ref = date(2025, 6, 1)
        rows = []
        for i in range(400, 0, -1):
            d = (ref - timedelta(days=i)).isoformat()
            rows.append(_make_macro_row("USDJPY", d, 150.0 if i <= 365 else 130.0))
            rows.append(_make_macro_row("SP500",  d, 5000.0 if i <= 365 else 4000.0))
        db = _build_mock_db(rows)
        result = get_macro_features(
            db, ref.isoformat(), ["macro_usdjpy_yoy", "macro_sp500_yoy"]
        )
        assert set(result.keys()) == {"macro_usdjpy_yoy", "macro_sp500_yoy"}
        for val in result.values():
            assert val is not None


# ── get_momentum_return ──────────────────────────────────────────────────────

class TestGetMomentumReturn:

    def _make_weekly(self, ref: date, long_price: float, short_price: float, n_weeks: int = 60):
        """long_cutoff (12ヶ月前) と short_cutoff (1ヶ月前) に価格を埋め込んだ週次データ。"""
        rows = []
        for w in range(n_weeks, 0, -1):
            d = (ref - timedelta(days=w * 7)).isoformat()
            # 12ヶ月前 (≈360日) の週に long_price
            if 380 >= w * 7 >= 340:
                close = long_price
            # 1ヶ月前 (≈30日) の週に short_price
            elif 50 >= w * 7 >= 20:
                close = short_price
            else:
                close = (long_price + short_price) / 2
            rows.append(_make_price_row(d, close))
        return rows

    def test_basic_positive_momentum(self):
        ref = date(2025, 6, 1)
        rows = self._make_weekly(ref, long_price=1000.0, short_price=1200.0)
        val = get_momentum_return(rows, ref.isoformat())
        assert val is not None
        assert val > 0  # 上昇→正のリターン

    def test_basic_negative_momentum(self):
        ref = date(2025, 6, 1)
        rows = self._make_weekly(ref, long_price=1200.0, short_price=1000.0)
        val = get_momentum_return(rows, ref.isoformat())
        assert val is not None
        assert val < 0  # 下落→負のリターン

    def test_flat_momentum_near_zero(self):
        ref = date(2025, 6, 1)
        rows = self._make_weekly(ref, long_price=1000.0, short_price=1000.0)
        val = get_momentum_return(rows, ref.isoformat())
        assert val is not None
        assert abs(val) < 0.05  # ほぼ 0

    def test_empty_returns_none(self):
        assert get_momentum_return([], "2025-06-01") is None

    def test_no_future_leak(self):
        """ref_date より未来の行は無視される。"""
        ref = date(2025, 6, 1)
        future_row = _make_price_row("2025-12-01", 9999.0)
        # ref の12ヶ月前と1ヶ月前に正しい行を入れる
        past_rows = [
            _make_price_row("2024-06-01", 1000.0),  # 12ヶ月前
            _make_price_row("2025-05-01", 1100.0),  # 1ヶ月前
        ]
        rows = past_rows + [future_row]
        val = get_momentum_return(rows, ref.isoformat())
        assert val is not None
        assert abs(val - math.log(1100.0 / 1000.0)) < 0.01

    def test_insufficient_history_returns_none(self):
        """1ヶ月前データしかなく12ヶ月前まで届かない → None。"""
        ref = date(2025, 6, 1)
        rows = [_make_price_row("2025-05-01", 1000.0)]
        val = get_momentum_return(rows, ref.isoformat())
        assert val is None

    def test_zero_close_excluded(self):
        """close=0 の行はスキップ（ゼロ除算防止）。"""
        ref = date(2025, 6, 1)
        rows = [
            _make_price_row("2024-06-01", 0.0),   # 無効
            _make_price_row("2025-05-01", 1000.0),
        ]
        val = get_momentum_return(rows, ref.isoformat())
        assert val is None  # long_cands が空になるはず

    def test_log_return_formula(self):
        """算出値が log(short/long) と一致するか厳密に確認。"""
        ref = date(2025, 6, 1)
        long_p, short_p = 800.0, 1200.0
        rows = [
            _make_price_row((ref - timedelta(days=365)).isoformat(), long_p),
            _make_price_row((ref - timedelta(days=30)).isoformat(),  short_p),
        ]
        val = get_momentum_return(rows, ref.isoformat())
        expected = math.log(short_p / long_p)
        assert val is not None
        assert abs(val - expected) < 1e-10
