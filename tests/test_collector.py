"""collector.py のユニットテスト（純粋関数・DB/ネットワーク不要）。

対象: XBRL_MAP/TSE_INDUSTRY/CONSOLIDATED_KEYS 定数、XBRL パース（連結優先・前期スキップ・
値整形）、派生指標計算 calc_derived、列検出、raw 変換、_bisect_left。
"""
import bisect
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import (
    CONSOLIDATED_KEYS,
    TSE_INDUSTRY,
    XBRL_MAP,
    _bisect_left,
    _detect_xbrl_columns,
    calc_derived,
    df_to_raw_rows,
    parse_raw_rows,
    parse_xbrl_csv,
)


# ── 定数 ─────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_xbrl_map_structure(self):
        for elem, mapped in XBRL_MAP.items():
            assert isinstance(mapped, tuple) and len(mapped) == 2
            assert mapped[0] in {"bs", "pl", "cf", "val", "meta"}

    def test_xbrl_map_known_mappings(self):
        assert XBRL_MAP["NetSales"] == ("pl", "revenue")
        assert XBRL_MAP["Assets"] == ("bs", "total_assets")
        assert XBRL_MAP["NetCashProvidedByUsedInOperatingActivities"] == ("cf", "operating_cf")

    def test_tse_industry(self):
        assert TSE_INDUSTRY["5250"] == "情報・通信業"
        assert TSE_INDUSTRY["3650"] == "電気機器"

    def test_consolidated_keys(self):
        assert CONSOLIDATED_KEYS == ["Consolidated"]


# ── parse_raw_rows ───────────────────────────────────────────────────────────

class TestParseRawRows:
    def _row(self, element, context, value):
        return {"element": element, "context": context, "value": value}

    def test_maps_elements(self):
        rows = [
            self._row("NetSales", "CurrentYearConsolidatedDuration", "1000"),
            self._row("OperatingIncome", "CurrentYearConsolidatedDuration", "200"),
            self._row("Assets", "CurrentYearConsolidatedInstant", "5000"),
        ]
        res = parse_raw_rows(rows)
        assert res["pl"]["revenue"] == 1000.0
        assert res["pl"]["operating_profit"] == 200.0
        assert res["bs"]["total_assets"] == 5000.0

    def test_consolidated_beats_member(self):
        # 行順に依存せず、連結(優先度2) が メンバー付き(優先度0) に勝つ
        rows = [
            self._row("NetSales", "CurrentYearDuration_NonConsolidatedMember", "100"),
            self._row("NetSales", "CurrentYearConsolidatedDuration", "1000"),
        ]
        assert parse_raw_rows(rows)["pl"]["revenue"] == 1000.0

    def test_comma_in_value(self):
        rows = [self._row("NetSales", "CurrentYearConsolidatedDuration", "1,234")]
        assert parse_raw_rows(rows)["pl"]["revenue"] == 1234.0

    def test_prior_year_skipped(self):
        rows = [self._row("NetSales", "Prior1YearConsolidatedDuration", "999")]
        assert "revenue" not in parse_raw_rows(rows)["pl"]

    def test_invalid_value_skipped(self):
        rows = [self._row("NetSales", "CurrentYearConsolidatedDuration", "N/A")]
        assert "revenue" not in parse_raw_rows(rows)["pl"]

    def test_unknown_element_ignored(self):
        rows = [self._row("SomeUnknownTag", "CurrentYearConsolidatedDuration", "123")]
        res = parse_raw_rows(rows)
        assert res["bs"] == {} and res["pl"] == {} and res["cf"] == {}


# ── parse_xbrl_csv ───────────────────────────────────────────────────────────

class TestParseXbrlCsv:
    def test_parses_dataframe(self):
        df = pd.DataFrame({
            "要素ID": ["jppfs_cor:NetSales", "jppfs_cor:Assets"],
            "コンテキストID": ["CurrentYearConsolidatedDuration", "CurrentYearConsolidatedInstant"],
            "値": ["1000", "5000"],
        })
        res = parse_xbrl_csv(df, "E00001", "2023-03-31")
        assert res["pl"]["revenue"] == 1000.0
        assert res["bs"]["total_assets"] == 5000.0

    def test_none_or_empty_returns_empty(self):
        empty = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "meta": {}}
        assert parse_xbrl_csv(None, "E00001", "2023-03-31") == empty
        assert parse_xbrl_csv(pd.DataFrame(), "E00001", "2023-03-31") == empty

    def test_element_namespace_is_stripped(self):
        df = pd.DataFrame({
            "要素ID": ["jpcrp_cor:OperatingProfit"],
            "コンテキストID": ["CurrentYearConsolidatedDuration"],
            "値": ["321"],
        })
        assert parse_xbrl_csv(df, "E00001", "2023-03-31")["pl"]["operating_profit"] == 321.0


# ── calc_derived ─────────────────────────────────────────────────────────────

class TestCalcDerived:
    def _rec(self):
        return {
            "bs": {"total_assets": 5000.0, "total_equity": 2500.0,
                   "current_assets": 1000.0, "investment_securities": 500.0,
                   "total_liabilities": 800.0,
                   "short_term_debt": 100.0, "long_term_debt": 200.0},
            "pl": {"revenue": 1000.0, "operating_profit": 200.0,
                   "ordinary_profit": 250.0, "net_income": 100.0},
            "cf": {"operating_cf": 150.0, "investing_cf": -50.0},
        }

    def test_margins_and_ratios(self):
        d = calc_derived(self._rec())["derived"]
        assert d["op_margin"] == 20.0
        assert d["net_margin"] == 10.0
        assert d["roe"] == 4.0
        assert d["roa"] == 2.0
        assert d["equity_ratio"] == 50.0
        assert d["de_ratio"] == 0.12
        assert d["cf_ratio"] == 15.0

    def test_free_cf_added_to_cf_section(self):
        rec = calc_derived(self._rec())
        assert rec["cf"]["free_cf"] == 100.0  # 営業CF + 投資CF

    def test_nonoperating_income(self):
        rec = calc_derived(self._rec())
        assert rec["pl"]["nonoperating_income"] == 50.0  # 経常 - 営業

    def test_net_cash(self):
        # 流動資産 1000 + 投資有価証券 500×0.7 − 総負債 800 = 550
        assert calc_derived(self._rec())["derived"]["net_cash"] == 550.0

    def test_zero_revenue_yields_none(self):
        rec = self._rec()
        rec["pl"]["revenue"] = 0
        d = calc_derived(rec)["derived"]
        assert d["op_margin"] is None
        assert d["net_margin"] is None
        assert d["cf_ratio"] is None

    def test_net_cash_none_without_assets_or_liabilities(self):
        rec = self._rec()
        rec["bs"]["current_assets"] = 0
        rec["bs"]["total_liabilities"] = 0
        assert calc_derived(rec)["derived"]["net_cash"] is None


# ── 列検出・raw 変換・二分探索 ───────────────────────────────────────────────

class TestDetectColumns:
    def test_detects_japanese_columns(self):
        df = pd.DataFrame(columns=["要素ID", "項目名", "コンテキストID", "ユニットID", "値"])
        cm = _detect_xbrl_columns(df)
        assert cm["element"] == "要素ID"
        assert cm["context"] == "コンテキストID"
        assert cm["value"] == "値"

    def test_detects_english_element_context(self):
        df = pd.DataFrame(columns=["element", "context", "値"])
        cm = _detect_xbrl_columns(df)
        assert cm["element"] == "element"
        assert cm["context"] == "context"
        assert cm["value"] == "値"


class TestDfToRawRows:
    def test_converts_and_strips_namespace(self):
        df = pd.DataFrame({
            "要素ID": ["jppfs_cor:NetSales"],
            "コンテキストID": ["CurrentYearConsolidatedDuration"],
            "値": ["1000"],
        })
        assert df_to_raw_rows(df) == [
            {"element": "NetSales", "context": "CurrentYearConsolidatedDuration", "value": "1000"}
        ]

    def test_missing_columns_returns_empty(self):
        assert df_to_raw_rows(pd.DataFrame({"foo": [1]})) == []


class TestBisectLeft:
    def test_matches_stdlib(self):
        lst = ["a", "c", "e", "g"]
        for v in ["a", "b", "c", "f", "g", "h", ""]:
            assert _bisect_left(lst, v) == bisect.bisect_left(lst, v)
