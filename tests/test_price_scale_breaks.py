"""週次株価の段差（株式分割の遡及調整もれ）検出・修復のテスト（Issue #465）。

stock_price_weekly は append-only で、分割が起きても過去行が再調整されない。
J-Quants の AdjC（JPX公式・調整後）と突合して段差を検出し、該当銘柄だけ Yahoo で
取り直す経路を、HTTP を遮断して検証する（DB は conftest の SQLite セッション）。
"""
import asyncio
import os
import sys
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector_prices import (            # noqa: E402
    _pick_probe_dates,
    _probe_official_closes,
    collect_stock_price_history_jquants,
    compare_official_vs_weekly,
    detect_price_scale_breaks,
    repair_price_scale_breaks,
)
from collector_utils import (             # noqa: E402
    JQuantsOutOfCoverage,
    is_common_stock_code,
)


class TestCommonStockCode:
    """J-Quants 5桁コードの普通株判定（#465）。"""

    def test_five_digit_trailing_zero_is_common(self):
        assert is_common_stock_code("13010")

    def test_other_suffix_is_not_common(self):
        """末尾が0以外＝優先株・優先出資証券などの別クラス。"""
        assert not is_common_stock_code("94345")

    def test_four_digit_sec_code_is_not_common(self):
        """4桁の sec_code には種類の桁が無いので普通株とは判定しない。"""
        assert not is_common_stock_code("1301")


class TestJquantsDedupPrefersCommonStock:
    """同一 edinet_code に複数クラスがあるとき、普通株の終値を採る（#465 の回帰）。

    旧実装はレスポンス到着順の先着勝ちで、順序が変われば優先株の終値が普通株の枠に
    入っていた（実測で先頭4桁が重複するのは 9434 / 5076 / 2593）。
    """

    _PX = {"94340": 200.0, "94345": 999.0}

    @pytest.mark.parametrize("order", [["94340", "94345"], ["94345", "94340"]])
    def test_daily_upsert_uses_common_stock_close(self, db, make_company, order):
        db.add(make_company(edinet_code="E90001", sec_code="9434", name="多クラス社"))
        db.commit()
        captured: list = []

        async def fetch(session, api_key, date_str):
            return [{"Code": c, "Date": date_str,
                     "AdjO": None, "AdjH": None, "AdjL": None,
                     "AdjC": self._PX[c], "AdjVo": None} for c in order]

        def capture(db_, rows, **kw):
            captured.extend(rows)
            return len(rows)

        with patch("collector_prices._jquants_fetch_date", new=fetch):
            with patch("collector_prices.record_prices_batch", new=capture):
                with patch("collector_prices.trim_daily", return_value=0):
                    with patch("collector_prices._fetch_jquants_equity_master",
                               new_callable=AsyncMock, return_value=(set(), None)):
                        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}):
                            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                                asyncio.run(collect_stock_price_history_jquants(
                                    db, date_from=date(2025, 1, 10),
                                    date_to=date(2025, 1, 10)))

        assert len(captured) == 1                      # 1 edinet_code につき1行
        assert captured[0]["close"] == 200.0           # 普通株（94340）の値


class TestProbeOfficialCloses:
    """突合用の公式終値取得（#465）。"""

    @pytest.mark.parametrize("order", [["94340", "94345"], ["94345", "94340"]])
    def test_prefers_common_regardless_of_arrival_order(self, db, make_company, order):
        """突合側も普通株優先にしないと、段差でない銘柄を段差として拾う。"""
        db.add(make_company(edinet_code="E90001", sec_code="9434", name="多クラス社"))
        db.commit()
        px = {"94340": 200.0, "94345": 999.0}

        async def fetch(session, api_key, date_str):
            return [{"Code": c, "Date": date_str, "AdjC": px[c]} for c in order]

        with patch("collector_prices._jquants_fetch_date", new=fetch):
            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                out = asyncio.run(
                    _probe_official_closes(db, None, "key", ["2025-01-10"]))

        assert out["2025-01-10"]["E90001"] == 200.0

    def test_out_of_coverage_date_is_skipped_not_fatal(self, db, make_company):
        """窓外の突合日は落とさずスキップする（窓は月をまたぐと端が外れうる）。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.commit()

        async def fetch(session, api_key, date_str):
            if date_str == "2023-01-06":
                raise JQuantsOutOfCoverage(date_str, "2024-05-16", "2026-05-16")
            return [{"Code": "10010", "Date": date_str, "AdjC": 500.0}]

        with patch("collector_prices._jquants_fetch_date", new=fetch):
            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                out = asyncio.run(_probe_official_closes(
                    db, None, "key", ["2023-01-06", "2025-01-10"]))

        assert "2023-01-06" not in out
        assert out["2025-01-10"]["E00001"] == 500.0


class TestPickProbeDates:
    """突合日の選定（#465）。カレンダーの金曜決め打ちではなく weekly の実在日から選ぶ。"""

    def test_picks_busiest_date_per_month(self, db, make_weekly):
        """月内で最も行数の多い trade_date を選ぶ＝突合できる銘柄数が最大になる。"""
        for ec in ("E00001", "E00002"):
            db.add(make_weekly(edinet_code=ec, trade_date="2025-01-10"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-17"))
        db.commit()

        assert _pick_probe_dates(db, 0, "2025-01-01", "2025-12-31") == ["2025-01-10"]

    def test_tie_is_broken_deterministically(self, db, make_weekly):
        """同数のときも実行ごとに入れ替わらない（ORDER BY 無しの最大値走査を避ける・#464）。"""
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-17"))
        db.commit()

        picked = [_pick_probe_dates(db, 0, "2025-01-01", "2025-12-31") for _ in range(5)]
        assert picked == [["2025-01-17"]] * 5

    def test_thins_to_requested_count_keeping_both_ends(self, db, make_weekly):
        """months で間引いても先頭と末尾は残す（期間の両端を必ず突合する）。"""
        for m in range(1, 13):
            db.add(make_weekly(edinet_code="E00001", trade_date=f"2025-{m:02d}-10"))
        db.commit()

        picked = _pick_probe_dates(db, 3, "2025-01-01", "2025-12-31")
        assert len(picked) == 3
        assert picked[0] == "2025-01-10"
        assert picked[-1] == "2025-12-10"

    def test_window_bounds_are_respected(self, db, make_weekly):
        """契約窓の外側の週は候補に入れない（叩いても 400 で 20秒を捨てるだけ）。"""
        db.add(make_weekly(edinet_code="E00001", trade_date="2023-06-09"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10"))
        db.commit()

        assert _pick_probe_dates(db, 0, "2024-05-16", "2026-05-16") == ["2025-01-10"]


class TestCompareOfficialVsWeekly:
    """公式値と weekly.close_last の突合（#465）。"""

    def _setup(self, db, make_company, make_weekly, close_last=1000.0):
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="分割社"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10",
                           close_last=close_last))
        db.commit()

    def test_below_threshold_not_reported(self, db, make_company, make_weekly):
        """0.5% の差は段差ではない（丸め・配当調整の差）。"""
        self._setup(db, make_company, make_weekly)
        r = compare_official_vs_weekly(db, {"2025-01-10": {"E00001": 1005.0}}, 0.01)
        assert r["breaks"] == []
        assert r["compared"] == 1

    def test_split_break_reported_with_ratio_and_meta(self, db, make_company, make_weekly):
        """1:5 分割の取り残しは ratio 0.2 として出る（ソニーG E01777 の実測と同型）。"""
        self._setup(db, make_company, make_weekly)
        r = compare_official_vs_weekly(db, {"2025-01-10": {"E00001": 200.0}}, 0.01)

        assert len(r["breaks"]) == 1
        b = r["breaks"][0]
        assert b["edinet_code"] == "E00001"
        assert b["sec_code"] == "1001"
        assert b["name"] == "分割社"
        assert b["ratio"] == pytest.approx(0.2)
        assert b["max_dev"] == pytest.approx(0.8)
        assert b["hits"] == 1
        assert b["worst_date"] == "2025-01-10"

    def test_hits_counts_days_and_max_dev_wins(self, db, make_company, make_weekly):
        """複数日でずれたら hits に数え、最大乖離の日を worst_date に採る。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="分割社"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10", close_last=1000.0))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-02-14", close_last=1000.0))
        db.commit()

        r = compare_official_vs_weekly(db, {
            "2025-01-10": {"E00001": 500.0},    # 乖離 50%
            "2025-02-14": {"E00001": 200.0},    # 乖離 80% ← こちらが worst
        }, 0.01)

        b = r["breaks"][0]
        assert b["hits"] == 2
        assert b["worst_date"] == "2025-02-14"
        assert b["max_dev"] == pytest.approx(0.8)

    def test_order_is_deterministic(self, db, make_company, make_weekly):
        """並びは max_dev 降順・同値は edinet_code 昇順で固定する。"""
        for i, (ec, jq) in enumerate([("E00003", 200.0), ("E00001", 500.0),
                                      ("E00002", 500.0)]):
            db.add(make_company(edinet_code=ec, sec_code=f"100{i}", name=ec))
            db.add(make_weekly(edinet_code=ec, trade_date="2025-01-10", close_last=1000.0))
        db.commit()

        official = {"2025-01-10": {"E00003": 200.0, "E00001": 500.0, "E00002": 500.0}}
        for _ in range(5):
            r = compare_official_vs_weekly(db, official, 0.01)
            assert [b["edinet_code"] for b in r["breaks"]] == ["E00003", "E00001", "E00002"]

    def test_zero_or_missing_close_is_skipped(self, db, make_company, make_weekly):
        """close_last が 0 の行は相対乖離を計算できない＝突合対象から外す。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10", close_last=0.0))
        db.commit()

        r = compare_official_vs_weekly(db, {"2025-01-10": {"E00001": 200.0}}, 0.01)
        assert r["compared"] == 0
        assert r["breaks"] == []


class TestDetectPriceScaleBreaks:
    """検出の一連（窓の学習 → 突合日選定 → 突合）（#465）。"""

    def test_learns_window_before_probing(self, db, make_company, make_weekly):
        db.add(make_company(edinet_code="E00001", sec_code="1001", name="分割社"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10", close_last=1000.0))
        db.commit()
        seen: list = []

        async def fetch(session, api_key, date_str):
            seen.append(date_str)
            if date_str == date.today().isoformat():
                raise JQuantsOutOfCoverage(date_str, "2024-05-16", "2026-05-16")
            return [{"Code": "10010", "Date": date_str, "AdjC": 200.0}]

        with patch("collector_prices._jquants_fetch_date", new=fetch):
            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                r = asyncio.run(detect_price_scale_breaks(db, "key", months=0))

        assert seen[0] == date.today().isoformat()     # 窓の学習が先（窓外を叩かないため）
        assert r["coverage"] == ("2024-05-16", "2026-05-16")
        assert r["probe_dates"] == ["2025-01-10"]
        assert r["breaks"][0]["edinet_code"] == "E00001"

    def test_raises_when_window_cannot_be_learned(self, db):
        """窓が分からないまま突合日を決めない（窓外を叩き続ける事故を防ぐ）。"""
        async def fetch(session, api_key, date_str):
            return []

        with patch("collector_prices._jquants_fetch_date", new=fetch):
            with pytest.raises(RuntimeError, match="カバレッジ窓"):
                asyncio.run(detect_price_scale_breaks(db, "key"))

    def test_given_probe_dates_skips_window_learning(self, db, make_company, make_weekly):
        """probe_dates を渡した検算パスでは窓の学習を省く（20秒 × 1 の節約）。"""
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.add(make_weekly(edinet_code="E00001", trade_date="2025-01-10", close_last=1000.0))
        db.commit()
        seen: list = []

        async def fetch(session, api_key, date_str):
            seen.append(date_str)
            return [{"Code": "10010", "Date": date_str, "AdjC": 1000.0}]

        with patch("collector_prices._jquants_fetch_date", new=fetch):
            with patch("collector_prices.JQUANTS_RATE_SLEEP", 0):
                r = asyncio.run(detect_price_scale_breaks(
                    db, "key", probe_dates=["2025-01-10"]))

        assert seen == ["2025-01-10"]
        assert r["breaks"] == []


class TestRepairPriceScaleBreaks:
    """修復の安全弁と検算（#465）。"""

    @staticmethod
    def _found(n, **over):
        r = {
            "breaks": [{"edinet_code": f"E{i:05d}", "sec_code": f"{1000 + i}",
                        "name": f"社{i}", "max_dev": 0.8, "ratio": 0.2,
                        "hits": 1, "worst_date": "2025-01-10"} for i in range(n)],
            "compared": 100, "probe_dates": ["2025-01-10"], "coverage": (None, None),
        }
        r.update(over)
        return r

    @staticmethod
    def _patched(detect_returns, yahoo_rows=None):
        """detect をモックし、Yahoo と書き込みを差し替えたコンテキストを返す。"""
        return (
            patch("collector_prices.detect_price_scale_breaks",
                  new_callable=AsyncMock, side_effect=detect_returns),
            patch("collector_prices.fetch_yahoo_history", new_callable=AsyncMock,
                  return_value=yahoo_rows if yahoo_rows is not None
                  else [{"trade_date": "2025-01-10", "close": 200.0, "volume": 1.0}]),
            patch("collector_prices.YAHOO_STOCK_RATE_SLEEP", 0),
        )

    def test_dry_run_does_not_write(self, db):
        """既定は dry-run。検出だけして weekly には触れない。"""
        p_detect, p_yahoo, p_sleep = self._patched([self._found(3)])
        with p_detect, p_yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch") as rec:
                r = asyncio.run(repair_price_scale_breaks(db, "key", persist=False))

        rec.assert_not_called()
        assert r["persisted"] is False
        assert r["detected"] == 3
        assert r["repaired"] == 0

    def test_aborts_above_max_repair_without_writing(self, db):
        """検出が上限を超えたら書かずに中止する。

        想定は全体の1〜2%。大きく超えるのは突合側の前提（コード対応・窓・API 仕様）が
        壊れた疑いが濃く、その状態で全銘柄を上書きするほうが危ない。
        """
        p_detect, p_yahoo, p_sleep = self._patched([self._found(5)])
        with p_detect, p_yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch") as rec:
                r = asyncio.run(repair_price_scale_breaks(
                    db, "key", persist=True, max_repair=4))

        rec.assert_not_called()
        assert r["aborted"] is True
        assert r["persisted"] is False

    def test_persist_repairs_and_verifies(self, db):
        """修復後に同じ突合日で検算し、乖離が消えていれば remaining は空。"""
        after = self._found(0)
        p_detect, p_yahoo, p_sleep = self._patched([self._found(1), after])
        with p_detect, p_yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch", return_value=1) as rec:
                r = asyncio.run(repair_price_scale_breaks(db, "key", persist=True))

        rec.assert_called_once()
        assert rec.call_args.kwargs["trim"] is True    # 1社ごとに trim＝daily を溜めない
        assert r["persisted"] is True
        assert r["repaired"] == 1
        assert r["remaining"] == []
        assert r["introduced"] == []

    def test_remaining_break_is_reported_not_swallowed(self, db):
        """Yahoo で取り直しても収束しない銘柄は残存として報告する（黙って通さない）。"""
        p_detect, p_yahoo, p_sleep = self._patched([self._found(1), self._found(1)])
        with p_detect, p_yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch", return_value=1):
                r = asyncio.run(repair_price_scale_breaks(db, "key", persist=True))

        assert r["repaired"] == 1
        assert [b["edinet_code"] for b in r["remaining"]] == ["E00000"]

    def test_newly_broken_company_is_reported_as_introduced(self, db):
        """修復対象でなかった銘柄が検算で段差になったら introduced として出す。"""
        after = self._found(0, breaks=[{"edinet_code": "E99999", "sec_code": "9999",
                                        "name": "巻き添え社", "max_dev": 0.5,
                                        "ratio": 0.5, "hits": 1,
                                        "worst_date": "2025-01-10"}])
        p_detect, p_yahoo, p_sleep = self._patched([self._found(1), after])
        with p_detect, p_yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch", return_value=1):
                r = asyncio.run(repair_price_scale_breaks(db, "key", persist=True))

        assert [b["edinet_code"] for b in r["introduced"]] == ["E99999"]
        assert r["remaining"] == []

    def test_empty_yahoo_response_is_recorded_as_failure(self, db):
        """Yahoo が空を返したら「修復した」に数えない（#438 の静かな劣化を繰り返さない）。"""
        p_detect, p_yahoo, p_sleep = self._patched([self._found(1), self._found(1)],
                                                   yahoo_rows=[])
        with p_detect, p_yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch") as rec:
                r = asyncio.run(repair_price_scale_breaks(db, "key", persist=True))

        rec.assert_not_called()
        assert r["repaired"] == 0
        assert r["failed"][0]["edinet_code"] == "E00000"

    def test_repair_range_starts_at_oldest_weekly_row(self, db, make_weekly):
        """取り直しは weekly の最古日から。分割日より前を残すと段差が消えない。"""
        db.add(make_weekly(edinet_code="E00000", trade_date="2019-03-08", close_last=1.0))
        db.commit()
        p_detect, p_yahoo, p_sleep = self._patched([self._found(1), self._found(0)])
        with p_detect, p_yahoo as yahoo, p_sleep:
            with patch("collector_prices.record_prices_batch", return_value=1):
                asyncio.run(repair_price_scale_breaks(db, "key", persist=True))

        assert yahoo.call_args.args[1] == "1000.T"
        assert yahoo.call_args.args[2] == "20190308"
