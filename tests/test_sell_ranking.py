"""plugins/sell_ranking.py のユニットテスト。

手法は 1 つだけ（ADR-0071）: M-6 の μ̂ の低い順に並べ、下位 1/5 が「売却」。
純粋: parse_holdings / _compute_trend / n_to_sell / assign_actions / 外した手法が戻っていないこと。
execute(): μ̂ の並び・札・データ不足・not_found / invalid・損益・トレンド（参考）（in-memory SQLite）。
依存ゲート（M-6 の μ̂）は execute_plugin 経由で確かめる。
"""
import asyncio
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import FinancialMetric, iso_week_start
from plugins import execute_plugin
from plugins import sell_ranking as sr
from plugins.base import DependencyError
from plugins.utils import coerce_params
from plugins.sell_ranking import (
    MU_SOURCE, SELL_QUANTILES, SELL_SELECT_COLS, plugin, _WeeklyBar, _TREND_LOOKBACK_DAYS,
    parse_holdings, _compute_trend, n_to_sell, assign_actions,
)

ROOT = Path(__file__).resolve().parent.parent


def _run(raw, db):
    """依存ゲートをバイパスして execute を直接実行（coerce 済み params を渡す）。"""
    return plugin.execute(coerce_params(plugin.params_schema(), raw), db)


def _wk(week_start, close):
    return SimpleNamespace(week_start=week_start, close_last=close)


def _universe(make_metric, n, **kw):
    """最新年度の会社を n 社（sec_code=1001.., edinet=E0001..）。"""
    return [make_metric(edinet_code=f"E{i:04d}", sec_code=f"{1000 + i}",
                        company_name=f"会社{i}", year=2023, **kw) for i in range(1, n + 1)]


def _seed_m6(db, mus, r1_primes=None):
    from database import replace_macro_enet_scores
    r1_primes = r1_primes or {}
    replace_macro_enet_scores(
        db, [{"edinet_code": ec, "mu": v, "r1_prime": r1_primes.get(ec)} for ec, v in mus.items()],
        "2026-07-29")


# ── 手法は 1 つだけ（ADR-0071）──────────────────────────────────────────────

class TestSingleMethod:
    def test_mu_source_is_m6(self):
        assert MU_SOURCE == "macro_enet"
        assert plugin.depends_on == ["macro_enet"]

    def test_only_holdings_is_a_parameter(self):
        """プリセット・ウェイト・閾値・トレンド補正・μ の出所・R3 足切りは選べない。"""
        assert set(plugin.params_schema()) == {"holdings"}

    def test_retired_parameters_have_no_effect(self):
        """外した手法の値を送っても execute へ届かない（coerce_params は schema に無いキーを捨てる）。
        並びが μ̂ だけで決まることは下の TestExecute が確かめる。"""
        typed = coerce_params(plugin.params_schema(),
                              {"holdings": "1001", "preset": "バランス型", "mu_source": "macro_dlm"})
        assert set(typed) == {"holdings"}

    def test_retired_names_are_gone_from_the_module(self):
        for name in ("PRESETS", "SELL_METRICS", "sell_score", "_apply_timing", "_base_action"):
            assert not hasattr(sr, name), f"{name} が sell_ranking に残っている"

    def test_screen_offers_no_alternative(self):
        """画面にプリセットのボタン・ウェイト・閾値・μ の出所の選択が残っていない。"""
        html = (ROOT / "templates" / "analysis.html").read_text(encoding="utf-8")
        tab = re.search(r'<div id="tab-sell_ranking".*?<!-- /tab-sell_ranking -->', html, re.S)
        assert tab, "売り候補タブの終端コメントが無い"
        body = tab.group(0)
        for gone in ("applySellPreset", "sell-weight-grid", 'id="sell-th"', 'id="reduce-th"',
                     "sell-timing-adjust", "sell-mu-source"):
            assert gone not in body, f"{gone} が売り候補タブに残っている"
        js = (ROOT / "static" / "js" / "analysis.js").read_text(encoding="utf-8")
        for gone in ("SELL_PRESETS", "SELL_WEIGHT_LABELS", "applySellPreset"):
            assert gone not in js, f"{gone} が analysis.js に残っている"


# ── 転送列の契約（Issue #482）───────────────────────────────────────────────

class TestSellSelectCols:
    def test_all_columns_exist_on_the_view(self):
        view_cols = set(FinancialMetric.__table__.columns.keys())
        assert not [c for c in SELL_SELECT_COLS if c not in view_cols]

    def test_only_display_columns(self):
        assert set(SELL_SELECT_COLS) == {"edinet_code", "sec_code", "company_name", "industry",
                                         "year", "stock_price", "is_active", "delisted_date"}

    def test_weekly_bar_carries_only_what_compute_trend_reads(self):
        assert _WeeklyBar._fields == ("week_start", "close_last")
        out = _compute_trend([_WeeklyBar("2026-01-05", 100.0), _WeeklyBar("2026-01-12", 90.0)])
        assert out["last_close"] == 90.0


# ── 純粋: parse_holdings ─────────────────────────────────────────────────────

class TestParseHoldings:
    def test_code_only(self):
        parsed, invalid = parse_holdings("7203\n9984")
        assert [p["sec_code"] for p in parsed] == ["7203", "9984"]
        assert all(p["avg_cost"] is None and p["buy_date"] is None for p in parsed)
        assert invalid == []

    def test_cost_and_date(self):
        parsed, invalid = parse_holdings("7203, @1500, 2024-03-10")
        assert parsed[0] == {"sec_code": "7203", "avg_cost": 1500.0, "buy_date": "2024-03-10"}
        assert invalid == []

    def test_cost_without_at_and_whitespace_sep(self):
        parsed, _ = parse_holdings("6758 12000")
        assert parsed[0]["sec_code"] == "6758"
        assert parsed[0]["avg_cost"] == 12000.0

    def test_invalid_line_collected(self):
        parsed, invalid = parse_holdings("7203\nAAPL\nへんな行")
        assert [p["sec_code"] for p in parsed] == ["7203"]
        assert invalid == ["AAPL", "へんな行"]

    def test_duplicate_skipped(self):
        parsed, _ = parse_holdings("7203, @1000\n7203, @2000")
        assert len(parsed) == 1
        assert parsed[0]["avg_cost"] == 1000.0   # 先勝ち

    def test_blank_and_comment_ignored(self):
        parsed, invalid = parse_holdings("\n# メモ\n7203\n")
        assert [p["sec_code"] for p in parsed] == ["7203"]
        assert invalid == []


# ── 純粋: _compute_trend（参考表示）─────────────────────────────────────────

class TestComputeTrend:
    def _series(self, prices):
        return [_wk(f"2023-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d}", p)
                for i, p in enumerate(prices)]

    def test_uptrend(self):
        res = _compute_trend(self._series([1000 + i * 25 for i in range(14)]))
        assert res["trend"] == "上昇" and res["ret_13w"] > 10

    def test_downtrend(self):
        res = _compute_trend(self._series([1000 - i * 25 for i in range(14)]))
        assert res["trend"] == "下落" and res["ret_13w"] < -10

    def test_flat(self):
        assert _compute_trend(self._series([1000 + (i % 2) for i in range(14)]))["trend"] == "横ばい"

    def test_insufficient_weeks(self):
        res = _compute_trend(self._series([1000, 1100, 1200]))
        assert res["trend"] == "不明" and res["ret_13w"] is None and res["last_close"] == 1200


# ── 純粋: 下位 1/5 の切り方 ──────────────────────────────────────────────────

class TestBottomFifth:
    def test_same_cut_as_the_measurement(self):
        """`in_layer_short_side` の下位（昇順の先頭 m // n_quantiles 社）と同じ。"""
        assert SELL_QUANTILES == 5
        assert [n_to_sell(m) for m in (0, 1, 4, 5, 9, 10, 14, 15, 23)] == [0, 0, 0, 1, 1, 2, 2, 3, 4]

    def test_lowest_mu_is_sold(self):
        mus = {f"C{i}": 0.01 * i for i in range(10)}
        order, actions = assign_actions(mus)
        assert order == [f"C{i}" for i in range(10)]
        assert [k for k in order if actions[k] == "SELL"] == ["C0", "C1"]
        assert all(actions[f"C{i}"] == "HOLD" for i in range(2, 10))

    def test_missing_mu_goes_last_and_does_not_count(self):
        mus = {"A": None, **{f"C{i}": -0.01 * i for i in range(5)}, "B": None}
        order, actions = assign_actions(mus)
        assert order[-2:] == ["A", "B"]
        assert actions["A"] == actions["B"] == "データ不足"
        # μ̂ がそろった 5 社のうち 1 社（最も低い C4）
        assert [k for k in order if actions[k] == "SELL"] == ["C4"]

    def test_ties_break_by_key(self):
        order, _ = assign_actions({"B": 0.0, "A": 0.0, "C": -0.1})
        assert order == ["C", "A", "B"]

    def test_fewer_than_five_sells_nothing(self):
        _, actions = assign_actions({"A": -0.5, "B": 0.1, "C": 0.2})
        assert set(actions.values()) == {"HOLD"}


# ── execute(): in-memory SQLite ──────────────────────────────────────────────

class TestExecute:
    def test_ranks_by_mu_and_sells_bottom_fifth(self, db, make_metric):
        db.add_all(_universe(make_metric, 10, roe=10.0))
        db.commit()
        # 財務（roe）が全社同じでも、並びは μ̂ だけで決まる
        _seed_m6(db, {f"E{i:04d}": 0.02 * i - 0.1 for i in range(1, 11)})
        res = _run({"holdings": "\n".join(str(1000 + i) for i in range(10, 0, -1))}, db)
        assert res["count"] == 10 and res["mu_available"] is True
        assert [r["sec_code"] for r in res["results"]] == [str(1000 + i) for i in range(1, 11)]
        assert [r["action"] for r in res["results"]] == ["SELL"] * 2 + ["HOLD"] * 8
        assert (res["n_sell"], res["n_hold"], res["n_no_data"]) == (2, 8, 0)
        assert res["results"][0]["mu"] == pytest.approx(-0.08)
        assert res["results"][0]["rank"] == 1 and res["mu_source"] == "macro_enet"

    def test_holding_without_mu_is_no_data(self, db, make_metric):
        db.add_all(_universe(make_metric, 6))
        db.commit()
        _seed_m6(db, {f"E{i:04d}": 0.01 * i for i in range(1, 6)})   # E0006 は μ̂ なし
        res = _run({"holdings": "1006\n1001\n1002\n1003\n1004\n1005"}, db)
        assert res["results"][-1]["sec_code"] == "1006"
        assert res["results"][-1]["action"] == "データ不足"
        assert res["results"][0]["sec_code"] == "1001" and res["results"][0]["action"] == "SELL"
        assert res["n_no_data"] == 1

    def test_without_mu_everything_is_no_data(self, db, make_metric):
        db.add_all(_universe(make_metric, 3))
        db.commit()
        res = _run({"holdings": "1001\n1002"}, db)
        assert res["mu_available"] is False
        assert {r["action"] for r in res["results"]} == {"データ不足"}

    def test_trend_does_not_change_the_action(self, db, make_metric, make_weekly):
        """下落トレンドでも札は μ̂ だけで決まる（トレンド補正は外した）。"""
        db.add_all(_universe(make_metric, 5))
        today = date.today()
        for i in range(14):
            db.add(make_weekly(edinet_code="E0005",
                               week_start=iso_week_start((today - timedelta(weeks=13 - i)).isoformat()),
                               close_last=1000 - i * 30))
        db.commit()
        _seed_m6(db, {f"E{i:04d}": 0.01 * i for i in range(1, 6)})
        by = {r["sec_code"]: r for r in _run({"holdings": "1001\n1002\n1003\n1004\n1005"}, db)["results"]}
        assert by["1005"]["trend"] == "下落"
        assert by["1005"]["action"] == "HOLD"     # μ̂ が最も高い
        assert by["1001"]["action"] == "SELL"

    def test_not_found_and_invalid(self, db, make_metric):
        db.add_all(_universe(make_metric, 3))
        db.commit()
        res = _run({"holdings": "1001\n9999\nNOPE"}, db)
        assert res["not_found"] == ["9999"]
        assert res["invalid"] == ["NOPE"]
        assert res["count"] == 1

    def test_pnl_from_avg_cost(self, db, make_metric):
        db.add(make_metric(edinet_code="E0001", sec_code="1001", stock_price=1200.0))
        db.commit()
        row = _run({"holdings": "1001, @1000"}, db)["results"][0]
        # 週次が無いので stock_price(1200) にフォールバック → +20%
        assert row["pnl_pct"] == 20.0

    def test_empty_holdings_returns_empty(self, db, make_metric):
        res = _run({"holdings": ""}, db)
        assert res["count"] == 0 and res["results"] == []

    def test_delisted_holding_surfaces_flag_but_not_excluded(self, db, make_metric):
        db.add_all([make_metric(edinet_code="E9001", sec_code="9001",
                                is_active=False, delisted_date=date(2024, 6, 1)),
                    *_universe(make_metric, 4)])
        db.commit()
        res = _run({"holdings": "9001"}, db)
        assert res["not_found"] == []
        row = res["results"][0]
        assert row["is_active"] is False and row["delisted_date"] == "2024-06-01"


# ── 週次ロードの窓とチャンク（Issue #482）───────────────────────────────────

class TestWeeklyLoadWindow:
    def test_bars_older_than_lookback_are_not_loaded(self, db, make_metric, make_weekly):
        db.add_all(_universe(make_metric, 5))
        old = date.today() - timedelta(days=_TREND_LOOKBACK_DAYS + 30)
        for i in range(14):
            db.add(make_weekly(edinet_code="E0003",
                               week_start=iso_week_start((old - timedelta(weeks=13 - i)).isoformat()),
                               close_last=1000 - i * 30))
        db.commit()
        row = _run({"holdings": "1003"}, db)["results"][0]
        assert row["trend"] == "不明" and row["ret_13w"] is None

    def test_all_holdings_are_loaded_across_chunk_boundary(self, db, make_metric, make_weekly, monkeypatch):
        monkeypatch.setattr(sr, "_TREND_CODE_BATCH", 2)
        db.add_all(_universe(make_metric, 5))
        today = date.today()
        for i in range(1, 6):
            for w in range(14):
                db.add(make_weekly(edinet_code=f"E{i:04d}",
                                   week_start=iso_week_start((today - timedelta(weeks=13 - w)).isoformat()),
                                   close_last=1000 - w * 30))
        db.commit()
        res = _run({"holdings": "1001\n1002\n1003\n1004\n1005"}, db)
        assert res["count"] == 5
        assert all(r["trend"] == "下落" for r in res["results"])
        assert all(r["last_close"] == 610.0 for r in res["results"])


# ── 依存ゲート（M-6 の μ̂）────────────────────────────────────────────────────

class TestDependencyGate:
    def test_blocks_without_m6(self, db, make_metric):
        db.add_all(_universe(make_metric, 3))
        db.commit()
        with pytest.raises(DependencyError):
            asyncio.run(execute_plugin(plugin, {"holdings": "1001"}, db))

    def test_runs_with_m6(self, db, make_metric):
        db.add_all(_universe(make_metric, 3))
        db.commit()
        _seed_m6(db, {"E0001": 0.01})
        res = asyncio.run(execute_plugin(plugin, {"holdings": "1001"}, db))
        assert res["count"] == 1 and res["results"][0]["mu"] == pytest.approx(0.01)


# ── 並びのキーの出どころ（#1202）: 既定は M-6 の μ̂・S2 は定数で切り替える経路だけ持つ ──────────

def _seed_s2(db, q20s, snapshot_date="2026-07-29"):
    from database import replace_sell_quantile_scores
    replace_sell_quantile_scores(db, [{"edinet_code": ec, "q20": v} for ec, v in q20s.items()], snapshot_date)


class TestSellKeySource:
    def test_default_is_m6_and_not_a_parameter(self):
        """既定は M-6 の μ̂（ADR-0071 のまま）。S2 への切り替えは画面のパラメータにしない（決定 4）。"""
        assert sr.SELL_KEY_SOURCE == MU_SOURCE == "macro_enet"
        assert sr.SELL_KEY_S2 == "sell_quantile"
        assert set(plugin.params_schema()) == {"holdings"}

    def test_choose_default_keeps_mu(self):
        mus = {"a": 0.1, "b": None}
        assert sr.choose_sell_keys(MU_SOURCE, mus, {"a": "E1", "b": "E2"}, {}, None) == (mus, MU_SOURCE, None)

    def test_choose_s2_uses_q20_and_leaves_missing_as_no_data(self):
        keys, src, why = sr.choose_sell_keys(
            sr.SELL_KEY_S2, {"a": 0.1, "b": 0.2, "c": 0.3}, {"a": "E1", "b": "E2", "c": None},
            {"q20": {"E1": -0.3, "E2": -0.1}, "snapshot_date": "2026-11-12"}, "2026-11-12")
        assert (keys, src, why) == ({"a": -0.3, "b": -0.1, "c": None}, sr.SELL_KEY_S2, None)

    def test_choose_s2_falls_back_when_empty(self):
        mus = {"a": 0.1}
        keys, src, why = sr.choose_sell_keys(sr.SELL_KEY_S2, mus, {"a": "E1"},
                                             {"q20": {}, "snapshot_date": None}, "2026-11-12")
        assert keys is mus and src == MU_SOURCE and "保存されていない" in why

    def test_choose_s2_falls_back_when_the_dates_differ(self):
        """S2 だけ古い（学習に失敗して前回のまま）なら M-6 へ倒す。M-6 の日付が無いときも倒す。"""
        s2 = {"q20": {"E1": -0.3}, "snapshot_date": "2026-11-11"}
        for m6_date in ("2026-11-12", None):
            keys, src, why = sr.choose_sell_keys(sr.SELL_KEY_S2, {"a": 0.1}, {"a": "E1"}, s2, m6_date)
            assert src == MU_SOURCE and "2026-11-11" in why

    def test_choose_unknown_source_is_rejected(self):
        with pytest.raises(ValueError):
            sr.choose_sell_keys("macro_dlm", {}, {}, {}, None)

    def test_execute_default_does_not_read_s2(self, db, make_metric):
        db.add_all(_universe(make_metric, 5))
        db.commit()
        _seed_m6(db, {f"E{i:04d}": 0.01 * i for i in range(1, 6)})
        _seed_s2(db, {f"E{i:04d}": -0.01 * i for i in range(1, 6)})   # 逆の並び
        res = _run({"holdings": "1001\n1002\n1003\n1004\n1005"}, db)
        assert res["results"][0]["sec_code"] == "1001"                # μ̂ の最小
        assert (res["sell_key_source"], res["sell_key_fallback"]) == (MU_SOURCE, None)
        assert "q20" not in res["results"][0]

    def test_execute_s2_ranks_by_q20_and_keeps_mu_for_display(self, db, make_metric, monkeypatch):
        monkeypatch.setattr(sr, "SELL_KEY_SOURCE", sr.SELL_KEY_S2)
        db.add_all(_universe(make_metric, 5))
        db.commit()
        _seed_m6(db, {f"E{i:04d}": 0.01 * i for i in range(1, 6)})
        _seed_s2(db, {f"E{i:04d}": -0.01 * i for i in range(1, 6)})
        res = _run({"holdings": "1001\n1002\n1003\n1004\n1005"}, db)
        assert [r["sec_code"] for r in res["results"]] == ["1005", "1004", "1003", "1002", "1001"]
        assert res["results"][0]["action"] == "SELL" and res["n_sell"] == 1
        assert res["results"][0]["q20"] == pytest.approx(-0.05)
        assert res["results"][0]["mu"] == pytest.approx(0.05)         # 表示する μ̂ は M-6 のまま
        assert (res["sell_key_source"], res["sell_key_fallback"]) == (sr.SELL_KEY_S2, None)

    def test_execute_s2_falls_back_to_m6_when_s2_is_empty(self, db, make_metric, monkeypatch):
        monkeypatch.setattr(sr, "SELL_KEY_SOURCE", sr.SELL_KEY_S2)
        db.add_all(_universe(make_metric, 5))
        db.commit()
        _seed_m6(db, {f"E{i:04d}": 0.01 * i for i in range(1, 6)})
        res = _run({"holdings": "1001\n1002\n1003\n1004\n1005"}, db)
        assert res["results"][0]["sec_code"] == "1001"
        assert res["sell_key_source"] == MU_SOURCE and res["sell_key_fallback"]

    def test_execute_s2_falls_back_when_s2_is_older_than_m6(self, db, make_metric, monkeypatch):
        monkeypatch.setattr(sr, "SELL_KEY_SOURCE", sr.SELL_KEY_S2)
        db.add_all(_universe(make_metric, 5))
        db.commit()
        _seed_m6(db, {f"E{i:04d}": 0.01 * i for i in range(1, 6)})   # 2026-07-29
        _seed_s2(db, {f"E{i:04d}": -0.01 * i for i in range(1, 6)}, snapshot_date="2026-07-22")
        res = _run({"holdings": "1001\n1002\n1003\n1004\n1005"}, db)
        assert res["results"][0]["sec_code"] == "1001"
        assert res["sell_key_source"] == MU_SOURCE and "2026-07-22" in res["sell_key_fallback"]
