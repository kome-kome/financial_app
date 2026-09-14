"""scripts/backfill_adj_factor_events.py のテスト — Issue #661 / #668。

対象社の選び方と、取得区間を記録表の行へ直す部分（純関数）だけを縛る。取得と書き込みは既存の
`fetch_official` と `upsert_jquants_adj_factor_events` / `upsert_jquants_adj_factor_coverage` を
呼ぶだけで、それぞれのテストがある。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backfill_adj_factor_events as B  # noqa: E402
from scripts import measure_split_valuation_bias as M  # noqa: E402


def row(year, shares, bps, eps, ec):
    return M.AnnualRow(edinet_code=ec, year=year, period_end="%d-03-31" % year,
                       issued_shares=shares, bs_bps=bps, pl_eps=eps, dps=10.0,
                       stock_price=1000.0, per=10.0, pbr=1.0, div_yield=1.0, market_cap=500.0)


ROWS = [
    # 倍率待ち（最新年 2025 に bps / eps が半分・翌年の行が無い）
    row(2024, 1000.0, 200.0, 20.0, "E00002"), row(2025, 1000.0, 100.0, 10.0, "E00002"),
    # 翌年の株数で決まる第2経路（2025 の分割・2026 に株数 x2）。窓 (2024-02-15, 2025-05-15]
    row(2024, 1000.0, 200.0, 20.0, "E00001"), row(2025, 1000.0, 100.0, 10.0, "E00001"),
    row(2026, 2000.0, 100.0, 10.0, "E00001"),
    # 同じ形だが古い（2019 の分割）。窓 (2018-02-14, 2019-05-15] は契約窓と重ならない
    row(2018, 1000.0, 200.0, 20.0, "E00003"), row(2019, 1000.0, 100.0, 10.0, "E00003"),
    row(2020, 2000.0, 100.0, 10.0, "E00003"),
    # 普通の分割（第1経路）。窓 (2024-02-15, 2025-05-15] の頭が契約窓の外なので、不在も確かめられず取らない
    row(2024, 1000.0, 200.0, 20.0, "E00004"), row(2025, 2000.0, 100.0, 10.0, "E00004"),
]
COVER = ("2024-06-20", "2026-06-20")


def test_awaiting_and_crosscheck_companies_are_chosen():
    events, stats = M.detect_events(ROWS, bps_path=True)
    assert B.choose_targets(events, stats, COVER) == {
        "E00001": ["crosscheck"],
        "E00002": ["awaiting"],
    }


def test_awaiting_is_not_narrowed_by_the_contract_window():
    """倍率待ちは窓で絞らない（取得しても空が返るだけで、判定を書き写すより安い）。"""
    events, stats = M.detect_events(ROWS, bps_path=True)
    got = B.choose_targets(events, stats, ("2030-01-01", "2031-01-01"))
    assert got == {"E00002": ["awaiting"]}


def test_nothing_to_fetch_when_the_bps_path_is_off():
    events, stats = M.detect_events(ROWS, bps_path=False)
    assert B.choose_targets(events, stats, COVER) == {}


# 第1経路で窓 (2025-02-14, 2026-05-15] が契約窓に完全に収まる社（#668 の不在確認の対象）
ABSENCE_ROWS = ROWS + [
    row(2025, 1000.0, 200.0, 20.0, "E00005"), row(2026, 2000.0, 100.0, 10.0, "E00005"),
]


def test_first_route_inside_the_contract_window_is_chosen_for_absence():
    """公式に分割が無いと確かめられれば外れうる社。窓が契約窓に**完全に**収まるときだけ選ぶ
    （重なるだけでは、窓の外側で起きた分割を不在と区別できない）。E00004 は窓の頭が外。"""
    events, stats = M.detect_events(ABSENCE_ROWS, bps_path=True)
    assert B.choose_targets(events, stats, COVER) == {
        "E00001": ["crosscheck"],
        "E00002": ["awaiting"],
        "E00005": ["absence"],
    }


def test_reasons_narrow_the_targets():
    events, stats = M.detect_events(ABSENCE_ROWS, bps_path=True)
    assert B.choose_targets(events, stats, COVER, reasons=("absence",)) == {"E00005": ["absence"]}
    assert B.choose_targets(events, stats, COVER, reasons=("awaiting", "crosscheck")) == {
        "E00001": ["crosscheck"], "E00002": ["awaiting"]}


def test_unknown_reason_is_rejected():
    events, stats = M.detect_events(ABSENCE_ROWS, bps_path=True)
    with pytest.raises(ValueError):
        B.choose_targets(events, stats, COVER, reasons=("absense",))
    with pytest.raises(argparse.ArgumentTypeError):
        B._parse_reasons("absence,typo")
    assert B._parse_reasons(" absence , awaiting ") == ("absence", "awaiting")


def test_zero_bars_writes_no_coverage_row():
    """**0 本の社は区間を書かない**（#668）。書くと「イベントが無い」と読まれ、本物の分割まで外れる。"""
    assert B.coverage_rows("E03474", "30320", M.bars_spans([])) == []
    assert B.coverage_rows("E01121", "52020", [("2024-06-24", "2026-06-22", 486)]) == [{
        "edinet_code": "E01121", "first_bar_date": "2024-06-24", "last_bar_date": "2026-06-22",
        "n_bars": 486, "jq_code": "52020"}]
