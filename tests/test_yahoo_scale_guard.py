"""取引では起こりえない段差のガードと夜間の走査（#765）。

検体は Issue #765 の実出力から写す（推測で書いた検体は、本物を読めないことを検出できない）:
- 1909.T（E25282）: 9/8 の 3,700 の後に Yahoo が 16,278,046,720・出来高0 を返した
- 7082.T（E35289）: 9/25 の 1,406 の後に 1,688,467,584
- 7999.T（E02305）: 5/15 の 7,570 → 5/18〜5/21 が 4,347,327,488 → 5/22 に 7,580 へ戻る往復
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector_prices import (
    classify_scale_rejections, find_scale_break_against, scale_rejection_example,
    scale_rejection_log_line, scale_step_log_line, scan_price_scale_steps,
)
from collector_utils import (
    SCALE_BREAK_RATIO, first_scale_break, is_scale_break, merge_with_anchor, scale_keep_mask,
)
from database import KEY_YAHOO_SCALE_REJECTIONS, get_setting, upsert_setting

SQUEEZE_1909 = 16278046720.0     # 3,700 ÷ round(1/4,400,000, 10) の float32
SQUEEZE_7082 = 1688467584.0      # 1,407 ÷ round(1/1,200,000, 10) の float32
MUTOH_BAND = 4347327488.0        # 7,580 ÷ round(1/573,512, 10) の float32


class TestIsScaleBreak:
    def test_threshold_is_100x_both_ways(self):
        assert SCALE_BREAK_RATIO == 100.0
        assert is_scale_break(100.0, 9999.0) is False
        assert is_scale_break(100.0, 10000.0) is True
        assert is_scale_break(100.0, 1.0) is True
        assert is_scale_break(100.0, 1.01) is False

    @pytest.mark.parametrize("prev,cur", [(None, 5.0), (5.0, None), (0.0, 5.0),
                                          (5.0, 0.0), (-1.0, 500.0)])
    def test_missing_or_non_positive_is_not_a_break(self, prev, cur):
        assert is_scale_break(prev, cur) is False

    def test_real_specimens(self):
        assert is_scale_break(3700.0, SQUEEZE_1909)
        assert is_scale_break(1406.0, SQUEEZE_7082)
        assert is_scale_break(MUTOH_BAND, 7580.0)          # 戻り側も段差

    def test_ordinary_moves_and_splits_are_not_breaks(self):
        assert not is_scale_break(1000.0, 500.0)           # 1:2 分割
        assert not is_scale_break(1000.0, 100.0)           # 1:10 分割
        assert not is_scale_break(593.0, 591.0)


class TestFirstScaleBreak:
    def test_returns_first_step(self):
        pts = [("2026-09-08", 3700.0), ("2026-09-09", SQUEEZE_1909), ("2026-09-10", 3700.0)]
        assert first_scale_break(pts) == ("2026-09-08", 3700.0, "2026-09-09", SQUEEZE_1909)

    def test_skips_missing_values(self):
        pts = [("d1", 1000.0), ("d2", None), ("d3", 0.0), ("d4", 1010.0)]
        assert first_scale_break(pts) is None

    def test_merge_puts_anchor_before_same_date_bar(self):
        merged = merge_with_anchor(("2026-09-25", 1406.0),
                                   [("2026-09-24", 1400.0), ("2026-09-25", SQUEEZE_7082)])
        assert merged == [("2026-09-24", 1400.0), ("2026-09-25", 1406.0),
                          ("2026-09-25", SQUEEZE_7082)]
        assert first_scale_break(merged)[2:] == ("2026-09-25", SQUEEZE_7082)


class TestScaleKeepMask:
    def test_overlap_normal_new_bars_scaled(self):
        """Yahoo は split 登録後の新しいバーを先に比率倍で返す（E35289 の 9/29 の晩）。"""
        assert scale_keep_mask(1406.0, [1400.0, 1406.0, SQUEEZE_7082]) == [True, True, False]

    def test_all_bars_scaled_drops_everything(self):
        """遡及調整が終わると全バーが比率倍（1909.T の 8/25〜9/14）。"""
        assert scale_keep_mask(3700.0, [SQUEEZE_1909] * 3) == [False, False, False]

    def test_roundtrip_keeps_the_normal_tail(self):
        """E02305: 比率倍の帯だけ捨てれば、5/22 の正常値が入って最終日付が進む。"""
        closes = [7570.0, MUTOH_BAND, MUTOH_BAND, 7580.0]
        assert scale_keep_mask(7570.0, closes) == [True, False, False, True]

    def test_without_anchor_any_internal_step_drops_all(self):
        """株価を1件も持たない社は、どちらの単位が正しいか決められない。"""
        assert scale_keep_mask(None, [1406.0, SQUEEZE_7082]) == [False, False]
        assert scale_keep_mask(None, [1000.0, 1010.0]) == [True, True]

    def test_ordinary_split_is_kept(self):
        """1:2 分割を Yahoo が遡及調整して返しても100倍には届かない＝従来どおり書く。"""
        assert scale_keep_mask(1000.0, [500.0, 505.0]) == [True, True]

    def test_kept_bars_with_internal_step_drop_all(self):
        """基準から100倍以内でも、残したバーどうしに100倍の段差があれば全部捨てる。"""
        assert scale_keep_mask(1000.0, [20.0, 50.0 * 99]) == [False, False]


class TestFindScaleBreakAgainst:
    def test_anchor_after_backfill_range(self):
        """週次の遡及: 取ってきた過去側が比率倍なら、DB の最古の週との境目が段差になる。"""
        recs = [{"trade_date": "2023-09-20", "close": 55319998464.0},
                {"trade_date": "2023-09-27", "close": 55319998464.0}]
        brk = find_scale_break_against({"price": 1731.0, "date": "2025-12-19"}, recs)
        assert brk["to_date"] == "2025-12-19" and brk["anchor_date"] == "2025-12-19"

    def test_consistent_history_passes(self):
        recs = [{"trade_date": "2026-01-05", "close": 500.0},
                {"trade_date": "2026-01-06", "close": 505.0}]
        assert find_scale_break_against({"price": 510.0, "date": "2026-01-07"}, recs) is None
        assert find_scale_break_against(None, recs) is None


class TestLogLines:
    def test_rejection_line_always_carries_counts(self):
        line = scale_rejection_log_line({"scale_rejected": 0, "scale_rejected_new": 0,
                                         "scale_dropped_bars": 0})
        assert line == "Yahoo スケール段差で不採用: 0社・0本（うち新規 0社）"

    def test_rejection_line_with_example(self):
        ex = {"edinet_code": "E35289", "from_date": "2026-09-25", "from_close": 1406.0,
              "to_date": "2026-09-28", "to_close": SQUEEZE_7082}
        line = scale_rejection_log_line({"scale_rejected": 1, "scale_rejected_new": 1,
                                         "scale_dropped_bars": 1, "scale_rejected_examples": [ex]})
        assert "（例: E35289 2026-09-25 1,406 → 2026-09-28 1,688,467,584）" in line
        assert scale_rejection_example(ex) == "E35289 2026-09-25 1,406 → 2026-09-28 1,688,467,584"


class TestClassifyScaleRejections:
    def _rej(self, ec="E25282", anchor="2026-09-11"):
        return {"edinet_code": ec, "anchor_date": anchor, "from_date": anchor,
                "from_close": 3700.0, "to_date": "2026-09-14", "to_close": 16280000512.0}

    def test_no_rejection_touches_nothing(self, db):
        assert classify_scale_rejections(db, []) == set()
        assert get_setting(db, KEY_YAHOO_SCALE_REJECTIONS) is None

    def test_first_time_is_new_then_known(self, db):
        from datetime import date
        assert classify_scale_rejections(db, [self._rej()], today=date(2026, 9, 30)) == {"E25282"}
        assert classify_scale_rejections(db, [self._rej()], today=date(2026, 10, 1)) == set()

    def test_moved_anchor_is_a_new_event(self, db):
        """修復や回復で基準日が変わったあとの再発は新しい事象（社だけで覚えない）。"""
        from datetime import date
        classify_scale_rejections(db, [self._rej(anchor="2026-09-14")], today=date(2026, 9, 30))
        assert classify_scale_rejections(
            db, [self._rej(anchor="2026-09-11")], today=date(2026, 10, 1)) == {"E25282"}

    def test_stale_entries_are_pruned(self, db):
        import json
        from datetime import date
        classify_scale_rejections(db, [self._rej("E00001")], today=date(2026, 1, 1))
        classify_scale_rejections(db, [self._rej("E00002")], today=date(2026, 9, 30))
        doc = json.loads(get_setting(db, KEY_YAHOO_SCALE_REJECTIONS))
        assert sorted(doc["companies"]) == ["E00002"]

    def test_broken_record_counts_all_as_new(self, db, caplog):
        upsert_setting(db, KEY_YAHOO_SCALE_REJECTIONS, "{not json")
        with caplog.at_level("WARNING", logger="collector"):
            assert classify_scale_rejections(db, [self._rej()]) == {"E25282"}
        assert "全社を新規として扱う" in caplog.text


class TestScanPriceScaleSteps:
    def test_finds_daily_and_weekly_steps(self, db, make_price, make_weekly):
        db.add_all([
            make_price(edinet_code="E25282", trade_date="2026-09-08", close=3700.0),
            make_price(edinet_code="E25282", trade_date="2026-09-09", close=SQUEEZE_1909),
            make_price(edinet_code="E00001", trade_date="2026-09-08", close=100.0),
            make_price(edinet_code="E00001", trade_date="2026-09-09", close=9900.0),   # 99倍
            make_price(edinet_code="E00002", trade_date="2026-09-08", close=0.0),
            make_price(edinet_code="E00002", trade_date="2026-09-09", close=500.0),    # 0 は無視
            make_weekly(edinet_code="E03530", trade_date="2023-09-22", close_last=2796.0),
            make_weekly(edinet_code="E03530", trade_date="2023-09-27", close_last=55319998464.0),
        ])
        db.commit()
        res = scan_price_scale_steps(db)
        assert res["companies"] == ["E03530", "E25282"]
        assert (res["daily_steps"], res["weekly_steps"]) == (1, 1)
        line = scale_step_log_line(res)
        assert line.startswith("株価スケール段差（≥100倍）: 2社（日次 1件・週次 1件）")
        assert "daily E25282 2026-09-08 3,700 → 2026-09-09 16,278,046,720" in line

    def test_clean_tables(self, db, make_price):
        db.add(make_price(close=1000.0))
        db.commit()
        res = scan_price_scale_steps(db)
        assert scale_step_log_line(res) == "株価スケール段差（≥100倍）: 0社（日次 0件・週次 0件）"
