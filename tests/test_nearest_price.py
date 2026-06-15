"""`collector._nearest_price` の境界値テスト。

point-in-time マッチと Yahoo backfill の最近傍探索で共用される bisect ヘルパー。
前後どちらにも候補がある／片方のみ／gap 超過で None／同 gap のタイブレークを検証する。
"""
import os
import sys

# プロジェクトルートを import パスに追加
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collector import _nearest_price  # noqa: E402

MAX_GAP = 30


def _pd(*pairs):
    """(日付文字列, 価格) のペアから sorted_dates と price_dict を作る。"""
    pairs = sorted(pairs)
    return [d for d, _ in pairs], dict(pairs)


def test_exact_match():
    dates, pd_ = _pd(("2024-03-29", 100.0), ("2024-06-28", 110.0))
    assert _nearest_price(dates, pd_, "2024-03-29", MAX_GAP) == 100.0


def test_both_neighbors_picks_closer():
    # target の前後どちらにも候補。後ろの方が近い。
    dates, pd_ = _pd(("2024-03-01", 90.0), ("2024-03-31", 110.0))
    assert _nearest_price(dates, pd_, "2024-03-29", MAX_GAP) == 110.0


def test_both_neighbors_picks_closer_before():
    # 前の方が近い。
    dates, pd_ = _pd(("2024-03-25", 90.0), ("2024-04-30", 130.0))
    assert _nearest_price(dates, pd_, "2024-03-29", MAX_GAP) == 90.0


def test_only_before_candidate():
    # target より後の候補が無い（pos == len）。前の候補が gap 内なら採用。
    dates, pd_ = _pd(("2024-03-01", 90.0), ("2024-03-15", 95.0))
    assert _nearest_price(dates, pd_, "2024-03-20", MAX_GAP) == 95.0


def test_only_after_candidate():
    # target より前の候補が無い（pos == 0）。後の候補が gap 内なら採用。
    dates, pd_ = _pd(("2024-03-15", 95.0), ("2024-04-01", 120.0))
    assert _nearest_price(dates, pd_, "2024-03-05", MAX_GAP) == 95.0


def test_gap_exceeded_returns_none():
    # 最近傍でも max_gap を超えるため None。
    dates, pd_ = _pd(("2024-01-01", 90.0), ("2024-06-01", 130.0))
    assert _nearest_price(dates, pd_, "2024-03-15", MAX_GAP) is None


def test_tie_break_prefers_before():
    # 前後が同 gap の場合、bisect の前候補（pos-1）が先に best_gap を確定するため前が勝つ。
    dates, pd_ = _pd(("2024-03-09", 90.0), ("2024-03-19", 110.0))
    assert _nearest_price(dates, pd_, "2024-03-14", MAX_GAP) == 90.0


def test_empty_dates_returns_none():
    assert _nearest_price([], {}, "2024-03-14", MAX_GAP) is None


def test_invalid_target_returns_none():
    dates, pd_ = _pd(("2024-03-15", 95.0))
    assert _nearest_price(dates, pd_, "not-a-date", MAX_GAP) is None
    assert _nearest_price(dates, pd_, None, MAX_GAP) is None
