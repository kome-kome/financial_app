"""collector.py のユニットテスト（純粋関数・DB/ネットワーク不要）。

対象: XBRL_MAP/TSE_INDUSTRY/CONSOLIDATED_KEYS 定数、XBRL パース（連結優先・前期スキップ・
値整形）、派生指標計算 calc_derived、列検出、raw 変換。
"""
import asyncio
import bisect
import io
import os
import sys
import zipfile
from datetime import date

import httpx
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
from collector import (
    CONSOLIDATED_KEYS,
    TSE_INDUSTRY,
    XBRL_MAP,
    _detect_xbrl_columns,
    _jquants_fetch_date,
    calc_derived,
    df_to_raw_rows,
    fetch_doc_list,
    fetch_stock_history_stooq,
    fetch_stock_price_stooq,
    fetch_xbrl_csv,
    parse_raw_rows,
    parse_xbrl_csv,
)


# ── ネットワーク系のモック補助（httpx 組み込み MockTransport・新規依存なし）──────

def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _const(response: httpx.Response):
    def handler(request):
        return response
    return handler


def _queue(*responses):
    it = iter(responses)
    def handler(request):
        return next(it)
    return handler


def _zip_bytes(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


# ── 定数 ─────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_xbrl_map_structure(self):
        for elem, mapped in XBRL_MAP.items():
            assert isinstance(mapped, tuple) and len(mapped) == 2
            assert mapped[0] in {"bs", "pl", "cf", "val", "nonfin", "meta"}

    def test_xbrl_map_known_mappings(self):
        assert XBRL_MAP["NetSales"] == ("pl", "revenue")
        assert XBRL_MAP["Assets"] == ("bs", "total_assets")
        assert XBRL_MAP["NetCashProvidedByUsedInOperatingActivities"] == ("cf", "operating_cf")

    def test_xbrl_map_c2_mappings(self):
        # 網羅性追加（C2）の標準要素マッピング
        assert XBRL_MAP["PropertyPlantAndEquipment"] == ("bs", "ppe_total")
        assert XBRL_MAP["PropertyPlantAndEquipmentIFRS"] == ("bs", "ppe_total")
        assert XBRL_MAP["InvestmentsAndOtherAssets"] == ("bs", "investments_other_assets")
        assert XBRL_MAP["DepreciationAndAmortizationOpeCF"] == ("pl", "depreciation")
        assert XBRL_MAP["ExtraordinaryIncome"] == ("pl", "extraordinary_income")
        assert XBRL_MAP["ExtraordinaryLoss"] == ("pl", "extraordinary_loss")
        assert XBRL_MAP["NumberOfEmployees"] == ("nonfin", "employees")
        assert XBRL_MAP["NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc"] \
            == ("nonfin", "issued_shares")

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
        empty = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "nonfin": {}, "meta": {}}
        assert parse_xbrl_csv(None, "E00001", "2023-03-31") == empty
        assert parse_xbrl_csv(pd.DataFrame(), "E00001", "2023-03-31") == empty

    def test_element_namespace_is_stripped(self):
        df = pd.DataFrame({
            "要素ID": ["jpcrp_cor:OperatingProfit"],
            "コンテキストID": ["CurrentYearConsolidatedDuration"],
            "値": ["321"],
        })
        assert parse_xbrl_csv(df, "E00001", "2023-03-31")["pl"]["operating_profit"] == 321.0

    def test_capex_extracted_by_label_for_extension_element(self):
        """設備投資は企業独自の拡張要素IDでタグ付けされるため、項目名（ラベル）で捕捉する。
        値は支出＝負（アウトフロー）に統一される。"""
        df = pd.DataFrame({
            "要素ID": [
                "jppfs_cor:NetCashProvidedByUsedInInvestmentActivities",
                "jpcrp030000-asr_E99999-000:PurchaseOfPPEExtension",  # 拡張要素（要素IDでは不一致）
            ],
            "項目名": ["投資活動によるキャッシュ・フロー", "有形固定資産の取得による支出"],
            "コンテキストID": ["CurrentYearConsolidatedDuration", "CurrentYearConsolidatedDuration"],
            "値": ["-5000", "3000"],
        })
        cf = parse_xbrl_csv(df, "E99999", "2025-03-31")["cf"]
        assert cf["investing_cf"] == -5000.0
        assert cf["capex"] == -3000.0  # 支出＝負に統一（絶対値3000）

    def test_capex_label_does_not_match_sale_proceeds(self):
        """「有形固定資産の売却による収入」を capex に誤マッチしないこと。"""
        df = pd.DataFrame({
            "要素ID": ["jpcrp030000-asr_E99999-000:ProceedsExt"],
            "項目名": ["有形固定資産の売却による収入"],
            "コンテキストID": ["CurrentYearConsolidatedDuration"],
            "値": ["200"],
        })
        assert "capex" not in parse_xbrl_csv(df, "E99999", "2025-03-31")["cf"]

    def test_nan_cells_do_not_crash_parse(self):
        """pandas 3.0 で `astype(str)` が NaN を float のまま残す回帰の防止。

        要素ID/コンテキストID/項目名のいずれに NaN があってもクラッシュせず、
        正常行（有形固定資産・capex）は抽出される。修正前は label の NaN が
        `_match_capex_by_label` で `argument of type 'float' is not iterable` を
        投げ、C2 補完が全件失敗していた。
        """
        df = pd.DataFrame({
            "要素ID": ["jppfs_cor:PropertyPlantAndEquipment", np.nan,
                       "jpcrp030000-asr_E99999-000:PurchaseOfPPEExtension"],
            "コンテキストID": ["CurrentYearConsolidatedInstant", "CurrentYearConsolidatedDuration", np.nan],
            "項目名": [np.nan, "ダミー", "有形固定資産の取得による支出"],
            "値": ["1000", "999", "500"],
        })
        res = parse_xbrl_csv(df, "E99999", "2025-03-31")
        assert res["bs"]["ppe_total"] == 1000.0
        assert res["cf"]["capex"] == -500.0

    # ── C2: 網羅性追加項目 ──────────────────────────────────────────────────
    def test_c2_bs_pl_fields_extracted(self):
        """有形固定資産合計/投資その他/研究開発費/減価償却費/特別損益を標準要素から抽出。"""
        df = pd.DataFrame({
            "要素ID": [
                "jppfs_cor:PropertyPlantAndEquipment",
                "jppfs_cor:InvestmentsAndOtherAssets",
                "jpcrp_cor:ResearchAndDevelopmentExpensesResearchAndDevelopmentActivities",
                "jppfs_cor:DepreciationAndAmortizationOpeCF",
                "jppfs_cor:ExtraordinaryIncome",
                "jppfs_cor:ExtraordinaryLoss",
            ],
            "コンテキストID": ["CurrentYearConsolidatedInstant", "CurrentYearConsolidatedInstant",
                              "CurrentYearConsolidatedDuration", "CurrentYearConsolidatedDuration",
                              "CurrentYearConsolidatedDuration", "CurrentYearConsolidatedDuration"],
            "値": ["31778", "12066", "3241", "2757", "445", "41"],
        })
        res = parse_xbrl_csv(df, "E00001", "2025-03-31")
        assert res["bs"]["ppe_total"] == 31778.0
        assert res["bs"]["investments_other_assets"] == 12066.0
        assert res["pl"]["rd_expenses"] == 3241.0
        assert res["pl"]["depreciation"] == 2757.0
        assert res["pl"]["extraordinary_income"] == 445.0
        assert res["pl"]["extraordinary_loss"] == 41.0

    def test_c2_ppe_total_ifrs_variant(self):
        df = pd.DataFrame({
            "要素ID": ["jpigp_cor:PropertyPlantAndEquipmentIFRS"],
            "コンテキストID": ["CurrentYearInstant"],
            "値": ["15333693"],
        })
        assert parse_xbrl_csv(df, "E02144", "2025-03-31")["bs"]["ppe_total"] == 15333693.0

    def test_c2_employees_consolidated_total_beats_segments(self):
        """従業員数は連結総額(メンバー無し context=priority1)がセグメント/非連結(member=priority0)に勝つ。
        セグメント context は ...ReportableSegmentMember（直前にアンダースコア無し）でも breakdown 扱い。"""
        df = pd.DataFrame({
            "要素ID": ["jpcrp_cor:NumberOfEmployees"] * 3,
            "コンテキストID": [
                "CurrentYearInstant_jpcrp030000-asr_E03144-000NITORIReportableSegmentMember",  # segment → 0
                "CurrentYearInstant_NonConsolidatedMember",                                    # 非連結 → 0
                "CurrentYearInstant",                                                          # 連結総額 → 1
            ],
            "値": ["18670", "939", "19967"],
        })
        assert parse_xbrl_csv(df, "E03144", "2025-03-31")["nonfin"]["employees"] == 19967.0

    def test_c2_issued_shares_prefers_fiscal_year_end_exact(self):
        """期末発行済株式総数: 正確値(FilingDateInstant・メンバー無し=priority1)が
        経営指標等の丸めSummary(NonConsolidatedMember=priority0)に勝つ。大株数もfloatで保持。"""
        df = pd.DataFrame({
            "要素ID": [
                "jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults",
                "jpcrp_cor:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc",
            ],
            "コンテキストID": ["CurrentYearInstant_NonConsolidatedMember", "FilingDateInstant"],
            "値": ["15794987000", "15794987460"],
        })
        assert parse_xbrl_csv(df, "E02144", "2025-03-31")["nonfin"]["issued_shares"] == 15794987460.0

    def test_ifrs_cf_detail_elements_extracted(self):
        """IFRS決算のCF計算書本体（NetCash...IFRS）から営業/投資/財務CF・現金増減を抽出する。
        トヨタ等のIFRS大企業のCFが全NULLになっていた根本原因（要素ID未登録）の回帰防止。
        コンテキストは Consolidated を含まない CurrentYearDuration。"""
        df = pd.DataFrame({
            "要素ID": [
                "jpigp_cor:NetCashProvidedByUsedInOperatingActivitiesIFRS",
                "jpigp_cor:NetCashProvidedByUsedInInvestingActivitiesIFRS",
                "jpigp_cor:NetCashProvidedByUsedInFinancingActivitiesIFRS",
                "jpigp_cor:NetIncreaseDecreaseInCashAndCashEquivalentsIFRS",
            ],
            "コンテキストID": ["CurrentYearDuration"] * 4,
            "値": ["3696934", "-4189736", "197236", "-429656"],
        })
        cf = parse_xbrl_csv(df, "E02144", "2025-03-31")["cf"]
        assert cf["operating_cf"] == 3696934.0
        assert cf["investing_cf"] == -4189736.0
        assert cf["financing_cf"] == 197236.0
        assert cf["net_change_cash"] == -429656.0

    def test_ifrs_cf_summary_section_elements_extracted(self):
        """CF計算書本体を独自拡張要素でタグ付けする企業向けに、
        「主要な経営指標等の推移」(...IFRSSummaryOfBusinessResults) からも当期CFを拾う。
        Prior年度（Prior4YearDuration）は除外し CurrentYearDuration のみ採用する。"""
        df = pd.DataFrame({
            "要素ID": [
                "jpcrp_cor:CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults",
                "jpcrp_cor:CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults",
                "jpcrp_cor:CashFlowsFromUsedInInvestingActivitiesIFRSSummaryOfBusinessResults",
                "jpcrp_cor:CashFlowsFromUsedInFinancingActivitiesIFRSSummaryOfBusinessResults",
            ],
            "コンテキストID": [
                "Prior4YearDuration",   # 過年度 → 除外されること
                "CurrentYearDuration",
                "CurrentYearDuration",
                "CurrentYearDuration",
            ],
            "値": ["2727162", "3696934", "-4189736", "197236"],
        })
        cf = parse_xbrl_csv(df, "E02144", "2025-03-31")["cf"]
        assert cf["operating_cf"] == 3696934.0  # 過年度2727162ではなく当期
        assert cf["investing_cf"] == -4189736.0
        assert cf["financing_cf"] == 197236.0

    def test_usgaap_cf_and_consolidated_metrics_from_summary(self):
        """US-GAAP決算（キヤノン・コマツ・オリックス・野村等）のCF合計・連結売上・純利益・
        総資産・純資産・EPS/BPS は ...USGAAPSummaryOfBusinessResults に集約される。
        連結値(CurrentYear*,優先度1)が非連結NetSales(メンバー,優先度0)に勝つことも確認。"""
        df = pd.DataFrame({
            "要素ID": [
                "jpcrp_cor:RevenuesUSGAAPSummaryOfBusinessResults",
                "jppfs_cor:NetSales",  # 非連結（メンバー）→ 連結値に負ける
                "jpcrp_cor:NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
                "jpcrp_cor:TotalAssetsUSGAAPSummaryOfBusinessResults",
                "jpcrp_cor:EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",  # 株主資本 → total_equity
                "jpcrp_cor:BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults",
                "jpcrp_cor:EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults",
                "jpcrp_cor:CashFlowsFromUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults",
                "jpcrp_cor:CashFlowsFromUsedInInvestingActivitiesUSGAAPSummaryOfBusinessResults",
                "jpcrp_cor:CashFlowsFromUsedInFinancingActivitiesUSGAAPSummaryOfBusinessResults",
            ],
            "コンテキストID": [
                "CurrentYearDuration",
                "CurrentYearDuration_NonConsolidatedMember",
                "CurrentYearDuration",
                "CurrentYearInstant",
                "CurrentYearInstant",
                "CurrentYearDuration",
                "CurrentYearInstant",
                "CurrentYearDuration",
                "CurrentYearDuration",
                "CurrentYearDuration",
            ],
            "値": ["4624727", "1837606", "332053", "6135044", "3491808",
                   "367.48", "3974.81", "475903", "-237450", "-179221"],
        })
        res = parse_xbrl_csv(df, "E02274", "2025-12-31")
        assert res["pl"]["revenue"] == 4624727.0  # 連結。非連結1837606ではない
        assert res["pl"]["net_income"] == 332053.0
        assert res["bs"]["total_assets"] == 6135044.0
        assert res["bs"]["total_equity"] == 3491808.0  # 株主資本→total_equity（ROE/自己資本比率の整合）
        assert res["pl"]["eps"] == 367.48
        assert res["bs"]["bps"] == 3974.81
        assert res["cf"]["operating_cf"] == 475903.0
        assert res["cf"]["investing_cf"] == -237450.0
        assert res["cf"]["financing_cf"] == -179221.0

    def test_ifrs_netsales_beats_nonconsolidated(self):
        """「売上収益(Revenue)」ではなく「売上高(NetSales)」をIFRSで使う企業（ソニー等）。
        連結 NetSalesIFRS(CurrentYearDuration,優先度1) が非連結 NetSales(メンバー,優先度0)に勝つ。"""
        df = pd.DataFrame({
            "要素ID": [
                "jpigp_cor:NetSalesIFRS",
                "jppfs_cor:NetSales",  # 非連結（メンバー）
            ],
            "コンテキストID": [
                "CurrentYearDuration",
                "CurrentYearDuration_NonConsolidatedMember",
            ],
            "値": ["12034917", "173940"],
        })
        assert parse_xbrl_csv(df, "E01777", "2025-03-31")["pl"]["revenue"] == 12034917.0

    def test_operating_revenue_summary_mapped_to_revenue(self):
        """「売上高」ではなく「営業収益」を使う非金融＋証券（鉄道・電力・小売・不動産・証券等）。
        経営指標等の OperatingRevenue1SummaryOfBusinessResults（必ず連結, CurrentYearDuration）を
        売上に採り、非連結 NetSales(メンバー,優先度0) に勝つ。"""
        df = pd.DataFrame({
            "要素ID": [
                "jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults",
                "jppfs_cor:NetSales",  # 非連結（メンバー）
            ],
            "コンテキストID": [
                "CurrentYearDuration",
                "CurrentYearDuration_NonConsolidatedMember",
            ],
            "値": ["2887553", "100000"],
        })
        assert parse_xbrl_csv(df, "E04147", "2025-03-31")["pl"]["revenue"] == 2887553.0

    def test_holdco_nonconsolidated_operating_revenue_not_mapped(self):
        """金融持株会社（銀行・保険）対策: 連結営業収益が無く、提出会社単体（NonConsolidatedMember）の
        OperatingRevenue1SummaryOfBusinessResults しか無い場合は revenue に採らない（NULL維持）。
        経常収益(OrdinaryIncomeSummary)も売上ではないので未マップ。
        （非連結値を採ると MUFG 1.3兆=単体・純利益率144% になる回帰の防止）"""
        df = pd.DataFrame({
            "要素ID": [
                "jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults",  # 提出会社単体のみ（連結なし）
                "jpcrp_cor:OrdinaryIncomeSummaryOfBusinessResults",     # 経常収益（売上ではない）
            ],
            "コンテキストID": ["CurrentYearDuration_NonConsolidatedMember", "CurrentYearDuration"],
            "値": ["1343267", "13629997"],
        })
        assert "revenue" not in parse_xbrl_csv(df, "E03606", "2025-03-31")["pl"]

    def test_operating_revenue_consolidated_beats_nonconsolidated_member(self):
        """連結 OperatingRevenue1Summary(CurrentYearDuration) が、同一企業の提出会社単体
        (NonConsolidatedMember) の同要素に勝つ（大和証券・JR東日本のように両方ある場合）。"""
        df = pd.DataFrame({
            "要素ID": ["jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults"] * 2,
            "コンテキストID": ["CurrentYearDuration_NonConsolidatedMember", "CurrentYearDuration"],
            "値": ["111013", "1372014"],
        })
        assert parse_xbrl_csv(df, "E03753", "2025-03-31")["pl"]["revenue"] == 1372014.0


class TestMatchCapexByLabel:
    @pytest.mark.parametrize("label,expected", [
        ("有形固定資産の取得による支出", True),
        ("有形固定資産及び無形固定資産の取得による支出", True),
        ("有形固定資産の購入による支出", True),
        ("有形固定資産の売却による収入", False),
        ("無形固定資産の取得による支出", False),
        ("投資有価証券の取得による支出", False),
        ("有形固定資産の除却による減少", False),
        ("", False),
    ])
    def test_label_matching(self, label, expected):
        from collector import _match_capex_by_label
        assert _match_capex_by_label(label) is expected


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


# ── ネットワーク系（httpx MockTransport でレスポンスを擬似） ──────────────────

class TestFetchDocList:
    def test_filters_securities_reports(self):
        payload = {"results": [
            {"ordinanceCode": "010", "formCode": "030000", "secCode": "1301", "edinetCode": "E00001"},
            {"ordinanceCode": "010", "formCode": "030000", "secCode": None, "edinetCode": "E00002"},   # secCode 無し→除外
            {"ordinanceCode": "010", "formCode": "043000", "secCode": "1305", "edinetCode": "E00003"},  # 別 formCode→除外
            {"ordinanceCode": "999", "formCode": "030000", "secCode": "1306", "edinetCode": "E00004"},  # 別 ordinance→除外
        ]}
        client = _client(_const(httpx.Response(200, json=payload)))
        out = asyncio.run(fetch_doc_list(client, date(2023, 6, 30)))
        assert [d["edinetCode"] for d in out] == ["E00001"]

    def test_http_error_returns_empty(self):
        client = _client(_const(httpx.Response(500)))
        assert asyncio.run(fetch_doc_list(client, date(2023, 6, 30))) == []


class TestFetchXbrlCsv:
    def test_reads_utf8_csv(self):
        csv = "要素ID,コンテキストID,値\njppfs_cor:NetSales,CurrentYearConsolidatedDuration,1000\n"
        client = _client(_const(httpx.Response(200, content=_zip_bytes("XBRL_TO_CSV/x.csv", csv.encode("utf-8")))))
        df = asyncio.run(fetch_xbrl_csv(client, "S100ABCD"))
        assert df is not None
        # 取得した DataFrame は parse_xbrl_csv にそのまま通せる（統合確認）
        assert parse_xbrl_csv(df, "E00001", "2023-03-31")["pl"]["revenue"] == 1000.0

    def test_reads_utf16_tab_csv(self):
        # EDINET は UTF-16 LE + タブ区切りの場合がある（utf-8 読込失敗 → フォールバック）
        csv = "要素ID\tコンテキストID\t値\njppfs_cor:NetSales\tCurrentYearConsolidatedDuration\t1000\n"
        client = _client(_const(httpx.Response(200, content=_zip_bytes("XBRL_TO_CSV/x.csv", csv.encode("utf-16")))))
        df = asyncio.run(fetch_xbrl_csv(client, "S100ABCD"))
        assert df is not None
        assert parse_xbrl_csv(df, "E00001", "2023-03-31")["pl"]["revenue"] == 1000.0

    def test_no_csv_in_zip_returns_none(self):
        client = _client(_const(httpx.Response(200, content=_zip_bytes("readme.txt", b"hello"))))
        assert asyncio.run(fetch_xbrl_csv(client, "S100ABCD")) is None

    def test_bad_zip_returns_none(self):
        client = _client(_const(httpx.Response(200, content=b"this is not a zip")))
        assert asyncio.run(fetch_xbrl_csv(client, "S100ABCD")) is None


class TestFetchStockPriceStooq:
    def test_parses_close(self):
        csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\n7203.JP,2023-09-01,22:00:00,2000,2050,1990,2025,1000000\n"
        client = _client(_const(httpx.Response(200, text=csv)))
        assert asyncio.run(fetch_stock_price_stooq("7203", client)) == 2025.0

    def test_short_seccode_returns_none(self):
        client = _client(_const(httpx.Response(200, text="x")))
        assert asyncio.run(fetch_stock_price_stooq("12", client)) is None

    def test_nonpositive_close_returns_none(self):
        csv = "Symbol,Date,Time,Open,High,Low,Close,Volume\n7203.JP,2023-09-01,22:00:00,0,0,0,0,0\n"
        client = _client(_const(httpx.Response(200, text=csv)))
        assert asyncio.run(fetch_stock_price_stooq("7203", client)) is None

    def test_malformed_returns_none(self):
        client = _client(_const(httpx.Response(200, text="only-header-no-data")))
        assert asyncio.run(fetch_stock_price_stooq("7203", client)) is None


class TestFetchStockHistoryStooq:
    def test_parses_rows(self):
        csv = ("Date,Open,High,Low,Close,Volume\n"
               "2023-01-04,100,110,90,105,1000\n"
               "2023-01-05,106,108,104,107,2000\n")
        client = _client(_const(httpx.Response(200, text=csv)))
        rows = asyncio.run(fetch_stock_history_stooq(client, "7203", "20230101", "20230110"))
        assert len(rows) == 2
        assert rows[0]["trade_date"] == "2023-01-04"
        assert rows[0]["close"] == 105.0
        assert rows[1]["volume"] == 2000.0

    def test_skips_malformed_lines(self):
        csv = ("Date,Open,High,Low,Close,Volume\n"
               "2023-01-04,100,110,90,105,1000\n"
               "BADLINE\n"                                  # 列不足 → スキップ
               "2023-01-05,n/a,108,104,107,2000\n")         # float 変換失敗 → スキップ
        client = _client(_const(httpx.Response(200, text=csv)))
        rows = asyncio.run(fetch_stock_history_stooq(client, "7203", "20230101", "20230110"))
        assert len(rows) == 1


class TestJquantsFetchDate:
    def test_single_page(self):
        payload = {"data": [{"Code": "13010", "Date": "2023-09-01", "C": 2000}]}
        client = _client(_const(httpx.Response(200, json=payload)))
        assert asyncio.run(_jquants_fetch_date(client, "key", "2023-09-01")) == payload["data"]

    def test_400_returns_empty(self):
        client = _client(_const(httpx.Response(400)))
        assert asyncio.run(_jquants_fetch_date(client, "key", "2023-01-01")) == []

    def test_pagination(self, monkeypatch):
        async def _noop(*a, **k):
            pass
        monkeypatch.setattr(collector.asyncio, "sleep", _noop)  # ページ間スリープを無効化
        client = _client(_queue(
            httpx.Response(200, json={"data": [{"Code": "13010"}], "pagination_key": "K2"}),
            httpx.Response(200, json={"data": [{"Code": "99840"}]}),
        ))
        rows = asyncio.run(_jquants_fetch_date(client, "key", "2023-09-01"))
        assert [d["Code"] for d in rows] == ["13010", "99840"]

    def test_429_then_success(self, monkeypatch):
        async def _noop(*a, **k):
            pass
        monkeypatch.setattr(collector.asyncio, "sleep", _noop)  # 90秒待機を無効化
        client = _client(_queue(
            httpx.Response(429),
            httpx.Response(200, json={"data": [{"Code": "13010"}]}),
        ))
        rows = asyncio.run(_jquants_fetch_date(client, "key", "2023-09-01"))
        assert [d["Code"] for d in rows] == ["13010"]
