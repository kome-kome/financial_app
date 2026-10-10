"""collector.py のユニットテスト（純粋関数・DB/ネットワーク不要）。

対象: XBRL_MAP/CONSOLIDATED_KEYS 定数、XBRL パース（連結優先・前期スキップ・
値整形）、派生指標計算 calc_derived、列検出、raw 変換。
"""
import asyncio
import io
import os
import sys
import zipfile
from datetime import date, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
import collector_financials
import collector_utils
from collector import (
    CONSOLIDATED_KEYS,
    XBRL_MAP,
    _detect_xbrl_columns,
    _jquants_fetch_code,
    _jquants_fetch_date,
    calc_derived,
    collect_doc_ids_for_period,
    df_to_raw_rows,
    fetch_doc_list,
    fetch_xbrl_csv,
    parse_raw_rows,
    parse_xbrl_csv,
)
from collector_utils import (
    EDINET_MAX_CONSECUTIVE_FAILURES,
    EdinetAccessError,
    redact_secrets,
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


# ── 連結の売上高の選択（#852）────────────────────────────────────────────────
# 検体は 2026-10-08 に EDINET から取得した実際の書類の行（要素ID・コンテキストID・値）を写した。
# 推測で書くと、実際の書類のタグ名・コンテキスト名を読めないことを検出できない。

def _xbrl_df(rows):
    return pd.DataFrame(rows, columns=["要素ID", "コンテキストID", "値"])


# E02144 トヨタ 2026年度（S100Y8NY）: 連結の営業収益は独自拡張タグ。単体の NetSales が採られていた
_TOYOTA_S100Y8NY = [
    ("jpcrp030000-asr_E02144-000:OperatingRevenuesIFRSKeyFinancialData", "CurrentYearDuration", "50684952000000"),
    ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "18259979000000"),
    ("jpcrp030000-asr_E02144-000:SalesOfProductsIFRS", "CurrentYearDuration", "45865949000000"),
    ("jpigp_cor:CostOfSalesIFRS", "CurrentYearDuration", "39141418000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "18259979000000"),
]
# E05156 2026年度（S100YIB5）: 売上を「収益」と呼ぶ IFRS の標準タグ Revenue2IFRS
_E05156_S100YIB5 = [
    ("jpcrp030000-asr_E05156-000:Revenue2IFRSSummaryOfBusinessResults", "CurrentYearDuration", "40971000000"),
    ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "10171000000"),
    ("jpigp_cor:Revenue2IFRS", "CurrentYearDuration", "40971000000"),
    ("jpigp_cor:CostOfSalesIFRS", "CurrentYearDuration", "13285000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "10171000000"),
]
# E03345 2023年度（S100QTB3）: 営業収益の独自拡張タグ（トヨタと同名）
_E03345_S100QTB3 = [
    ("jpcrp030000-asr_E03345-000:OperatingRevenuesIFRSKeyFinancialData", "CurrentYearDuration", "1000385000000"),
    ("jpcrp030000-asr_E03345-000:OperatingRevenuesIFRS", "CurrentYearDuration", "1000385000000"),
    ("jpigp_cor:CostOfSalesIFRS", "CurrentYearDuration", "473074000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "26419000000"),
]
# E00317 2026年度（S100YHJP）: 名前に Consolidated を含むセグメントが連結総額に勝っていた
_E00317_S100YHJP = [
    ("jppfs_cor:NetSales",
     "CurrentYearDuration_jpcrp030000-asr_E00317-000ConsolidatedSubsidiariesReportableSegmentsMember",
     "93814000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration", "439615000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "413353000000"),
]
# E05663 2026年度（S100Z38F）: 同上
_E05663_S100Z38F = [
    ("jppfs_cor:NetSales",
     "CurrentYearDuration_jpcrp030000-asr_E05663-000ConsolidatedFinancialDisclosureBusinessReportableSegmentMember",
     "9648249000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration", "30481393000"),
    ("jppfs_cor:OperatingRevenue1", "CurrentYearDuration_NonConsolidatedMember", "5524677000"),
]
# E01254 2026年度（S100YDBT）: 日本基準の連結。売上総利益が本当に赤字（売上原価 > 売上高）で、選択は変わらない
_E01254_S100YDBT = [
    ("jppfs_cor:NetSales", "CurrentYearDuration", "9414000000"),
    ("jppfs_cor:CostOfSales", "CurrentYearDuration", "12555000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "8679000000"),
    ("jppfs_cor:CostOfSales", "CurrentYearDuration_NonConsolidatedMember", "11861000000"),
]


class TestConsolidatedRevenue:
    """連結の売上高が単体・セグメントの値に負けないこと（#852）。行の並びに依存しないことも縛る。"""

    @pytest.mark.parametrize("rows, revenue", [
        (_TOYOTA_S100Y8NY, 50684952000000.0),
        (_E05156_S100YIB5, 40971000000.0),
        (_E03345_S100QTB3, 1000385000000.0),
        (_E00317_S100YHJP, 439615000000.0),
        (_E05663_S100Z38F, 30481393000.0),
        (_E01254_S100YDBT, 9414000000.0),
    ], ids=["E02144", "E05156", "E03345", "E00317", "E05663", "E01254-unchanged"])
    @pytest.mark.parametrize("order", ["as_filed", "reversed"])
    def test_consolidated_revenue_is_chosen(self, rows, revenue, order):
        rows = rows if order == "as_filed" else rows[::-1]
        assert parse_xbrl_csv(_xbrl_df(rows), "E00000", "2026-03-31")["pl"]["revenue"] == revenue

    @pytest.mark.parametrize("order", ["as_filed", "reversed"])
    def test_parse_raw_rows_agrees(self, order):
        rows = _E00317_S100YHJP if order == "as_filed" else _E00317_S100YHJP[::-1]
        raw = [{"element": e.split(":")[-1], "context": c, "value": v} for e, c, v in rows]
        assert parse_raw_rows(raw)["pl"]["revenue"] == 439615000000.0

    def test_revenue_and_cost_share_the_consolidated_basis(self):
        """トヨタ: 売上高と売上原価が同じ連結の基準になる（食い違いの本体）。"""
        pl = parse_xbrl_csv(_xbrl_df(_TOYOTA_S100Y8NY), "E02144", "2026-03-31")["pl"]
        assert pl["revenue"] == 50684952000000.0
        assert pl["cost_of_sales"] == 39141418000000.0
        assert pl["cost_of_sales"] < pl["revenue"]

    def test_new_revenue_tags_are_mapped(self):
        for tag in ("Revenue2IFRS", "Revenue2IFRSSummaryOfBusinessResults",
                    "OperatingRevenuesIFRS", "OperatingRevenuesIFRSKeyFinancialData"):
            assert XBRL_MAP[tag] == ("pl", "revenue")

    def test_component_extension_tags_are_not_mapped(self):
        """トヨタの SalesOfProductsIFRS は商品・製品売上だけの内訳。合計に似た名前を足していない。"""
        assert "SalesOfProductsIFRS" not in XBRL_MAP

    @pytest.mark.parametrize("ctx, priority", [
        ("CurrentYearConsolidatedDuration", 2),  # 旧形式: 名前で連結を名乗る
        ("CurrentYearDuration", 1),               # 現行の連結総額
        ("CurrentYearDuration_NonConsolidatedMember", 0),
        ("CurrentYearDuration_jpcrp030000-asr_E00317-000ConsolidatedSubsidiariesReportableSegmentsMember", 0),
        ("CurrentYearDuration_ConsolidatedAccountingGroupMember", 0),
        ("CurrentYearInstant_jpcrp030000-asr_E03144-000NITORIReportableSegmentMember", 0),
    ])
    def test_context_priority(self, ctx, priority):
        assert collector_financials._context_priority(ctx) == priority


# ── 旧様式の四半期報告書の H1: 半期累計と直近3か月（#871）──────────────────────
# 検体は E02144 トヨタ 2021年 H1（S100K3J3）の実際の行。同じ要素が半期の累計
# （CurrentYTDDuration）と直近3か月（CurrentQuarterDuration）の2つで並び、どちらも
# メンバーを持たないので優先度が同じ＝先に来た方が採られていた。
_TOYOTA_S100K3J3 = [
    ("jpcrp040300-q2r_E02144-000:OperatingRevenuesIFRSKeyFinancialData", "CurrentYTDDuration", "11375223000000"),
    ("jpcrp040300-q2r_E02144-000:OperatingRevenuesIFRSKeyFinancialData", "CurrentQuarterDuration", "6774427000000"),
]


class TestInterimYtdOverQuarter:
    """H1 行の P/L・CF は並び順に関わらず半期累計から採る（#871）。"""

    @pytest.mark.parametrize("order", ["as_filed", "reversed"])
    def test_ytd_is_chosen_regardless_of_order(self, order):
        rows = _TOYOTA_S100K3J3 if order == "as_filed" else _TOYOTA_S100K3J3[::-1]
        pl = parse_xbrl_csv(_xbrl_df(rows), "E02144", "2021-09-30")["pl"]
        assert pl["revenue"] == 11375223000000.0, "3か月分（7〜9月）の値が採られている"

    @pytest.mark.parametrize("order", ["as_filed", "reversed"])
    def test_parse_raw_rows_agrees(self, order):
        rows = _TOYOTA_S100K3J3 if order == "as_filed" else _TOYOTA_S100K3J3[::-1]
        raw = [{"element": e.split(":")[-1], "context": c, "value": v} for e, c, v in rows]
        assert parse_raw_rows(raw)["pl"]["revenue"] == 11375223000000.0

    def test_quarter_only_value_is_not_taken(self):
        """3か月しか無い項目は NULL のまま（H1 行へ3か月分が入ると黙って誤る）。"""
        rows = [r for r in _TOYOTA_S100K3J3 if r[1] == "CurrentQuarterDuration"]
        assert "revenue" not in parse_xbrl_csv(_xbrl_df(rows), "E02144", "2021-09-30")["pl"]

    def test_quarter_end_balance_is_still_taken(self):
        """B/S は時点（CurrentQuarterInstant＝第2四半期末＝半期末の残高）から採る。Duration ではない。"""
        rows = [("jppfs_cor:Assets", "CurrentQuarterInstant", "999")]
        assert parse_xbrl_csv(_xbrl_df(rows), "E00000", "2021-09-30")["bs"]["total_assets"] == 999.0

    def test_quarter_inventory_parts_are_not_collected(self):
        """棚卸資産の集約も同じ判定を通る（#852 では2箇所に同じ穴があった）。"""
        elem = next(iter(collector_financials._INVENTORY_SUB_ELEMS))
        parts, prio = {}, {}
        collector_financials._collect_inventory_row(elem, "CurrentQuarterDuration", "5", parts, prio)
        collector_financials._collect_inventory_row(elem, "CurrentQuarterInstant", "7", parts, prio)
        assert parts == {elem: 7.0}

    @pytest.mark.parametrize("ctx, outside", [
        ("CurrentYTDDuration", False),                          # 旧様式 H1 の累計
        ("CurrentYTDDuration_NonConsolidatedMember", False),
        ("CurrentQuarterDuration", True),                       # 旧様式の直近3か月
        ("CurrentQuarterDuration_NonConsolidatedMember", True),
        ("CurrentQuarterConsolidatedDuration", True),           # 旧形式の3か月
        ("CurrentQuarterInstant", False),                       # 半期末の残高
        ("CurrentYearDuration", False),                         # 年度
        ("CurrentYearInstant", False),
        ("InterimDuration", False),                             # 新式半期（#647）
        ("Prior1InterimDuration", True),
        ("Prior1YTDDuration", True),
        ("Prior1YearDuration", True),
        ("Prior1YearInstant", True),
    ])
    def test_outside_current_period(self, ctx, outside):
        assert collector_financials._outside_current_period(ctx) is outside

    def test_period_rule_lives_in_one_place(self):
        """期間の判定の式は `_outside_current_period` だけに置く（呼び出し側へ書き写さない）。"""
        import inspect
        for fn in (collector_financials._apply_row, collector_financials._collect_inventory_row):
            src = inspect.getsource(fn)
            assert "_outside_current_period(ctx)" in src, fn.__name__
            assert '"Prior" in ctx' not in src and '"Quarter" in ctx' not in src, (
                f"{fn.__name__} が期間の判定を書き写している"
            )


# ── 会計基準（DEI `AccountingStandardsDEI`・#859）──────────────────────────────
# 検体は 2026-10-09 に EDINET から取得した実際の書類の DEI 行。表記は年度・半期・旧四半期で同じだった。

_DEI_JGAAP_S100YHJP = [  # E00317 有価証券報告書
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "Japan GAAP"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31"),
    ("jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "FY"),
]
_DEI_IFRS_S100Y8NY = [   # トヨタ 有価証券報告書
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "IFRS"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31"),
    ("jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "FY"),
]
_DEI_USGAAP_S100XTLJ = [  # キヤノン 有価証券報告書
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "US GAAP"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2025-12-31"),
    ("jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "FY"),
]


class TestAccountingStandard:
    @pytest.mark.parametrize("rows, expected", [
        (_DEI_JGAAP_S100YHJP, "JGAAP"),
        (_DEI_IFRS_S100Y8NY, "IFRS"),
        (_DEI_USGAAP_S100XTLJ, "US-GAAP"),
    ], ids=["E00317-JGAAP", "E02144-IFRS", "E02274-USGAAP"])
    def test_real_dei_maps_to_column_values(self, rows, expected):
        assert collector_financials.accounting_standard_of(_xbrl_df(rows), "S100") == expected

    def test_dei_also_keeps_the_period_meta(self):
        """会計基準を足しても、半期収集と取り直しが読む期間の要素は同じ1パスで取れる。"""
        dei = collector_financials._extract_dei(_xbrl_df(_DEI_USGAAP_S100XTLJ))
        assert dei["CurrentPeriodEndDateDEI"] == "2025-12-31"
        assert dei["TypeOfCurrentPeriodDEI"] == "FY"
        assert dei["AccountingStandardsDEI"] == "US GAAP"

    @pytest.mark.parametrize("rows", [
        [("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31")],   # 要素が無い
        [("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "－")],            # EDINET の空欄
    ], ids=["missing", "blank"])
    def test_missing_or_blank_is_none_without_warning(self, rows, caplog):
        with caplog.at_level("WARNING", logger="collector"):
            assert collector_financials.accounting_standard_of(_xbrl_df(rows), "S100") is None
        assert not caplog.records

    def test_unknown_notation_is_none_with_warning(self, caplog):
        """推測で丸めない。現れたら実物で表記を確かめて写し表へ足す。"""
        rows = [("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "Unknown GAAP")]
        with caplog.at_level("WARNING", logger="collector"):
            assert collector_financials.accounting_standard_of(_xbrl_df(rows), "S100X") is None
        assert "Unknown GAAP" in caplog.text and "S100X" in caplog.text

    @pytest.mark.parametrize("df", [None, pd.DataFrame()], ids=["none", "empty"])
    def test_no_document_is_none(self, df):
        assert collector_financials.accounting_standard_of(df) is None

    def test_interim_collection_shares_the_extractor(self):
        """DEI の抽出を写して二重に持たない（年度・半期・取り直しが同じ関数を使う）。"""
        import collector_interim
        assert collector_interim._extract_dei is collector_financials._extract_dei


# ── 連結を作る会社の書類では単体の値を採らない（#896）─────────────────────────────
# 検体は 2026-10-10 に EDINET から取得した実際の書類の行を写した（要素ID・コンテキストID・値）。
# 推測で書くと、実際の書類のタグ名・コンテキスト名・DEI の表記を読めないことを検出できない。

# E02144 トヨタ 2019年度（S100G1ZO・US-GAAP）: 連結は要約（…USGAAPSummaryOfBusinessResults）だけで、
# 明細は単体（NonConsolidatedMember）しか無い。営業利益率が「単体の営業利益 ÷ 連結の売上高」になっていた
_TOYOTA_USGAAP_S100G1ZO = [
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "US GAAP"),
    ("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "FilingDateInstant", "true"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2019-03-31"),
    ("jpcrp_cor:RevenuesUSGAAPSummaryOfBusinessResults", "CurrentYearDuration", "30225681000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "12634439000000"),
    ("jppfs_cor:CostOfSales", "CurrentYearDuration_NonConsolidatedMember", "9991345000000"),
    ("jppfs_cor:GrossProfit", "CurrentYearDuration_NonConsolidatedMember", "2643093000000"),
    ("jppfs_cor:SellingGeneralAndAdministrativeExpenses", "CurrentYearDuration_NonConsolidatedMember", "1316956000000"),
    ("jppfs_cor:OperatingIncome", "CurrentYearDuration_NonConsolidatedMember", "1326137000000"),
    ("jppfs_cor:OrdinaryIncome", "CurrentYearDuration_NonConsolidatedMember", "2323121000000"),
    ("jpcrp_cor:NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults", "CurrentYearDuration", "1882873000000"),
    ("jppfs_cor:ProfitLoss", "CurrentYearDuration_NonConsolidatedMember", "1896824000000"),
    ("jpcrp_cor:BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults", "CurrentYearDuration", "650.55"),
    ("jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "657.10"),
    ("jpcrp_cor:TotalAssetsUSGAAPSummaryOfBusinessResults", "CurrentYearInstant", "51936949000000"),
    ("jppfs_cor:Assets", "CurrentYearInstant_NonConsolidatedMember", "17716993000000"),
    ("jppfs_cor:Liabilities", "CurrentYearInstant_NonConsolidatedMember", "5266718000000"),
    ("jppfs_cor:CurrentAssets", "CurrentYearInstant_NonConsolidatedMember", "7078259000000"),
    ("jpcrp_cor:EquityIncludingPortionAttributableToNonControllingInterestUSGAAPSummaryOfBusinessResults", "CurrentYearInstant", "20565210000000"),
    ("jppfs_cor:NetAssets", "CurrentYearInstant_NonConsolidatedMember", "12450274000000"),
    ("jpcrp_cor:EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults", "CurrentYearInstant", "6830.92"),
    ("jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", "4225.55"),
    ("jpcrp_cor:CashFlowsFromUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults", "CurrentYearDuration", "3766597000000"),
    ("jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "220"),
    ("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant", "370870"),
    ("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant_NonConsolidatedMember", "74515"),
    ("jpcrp_cor:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc", "FilingDateInstant", "3310097492"),
    ("jppfs_cor:MerchandiseAndFinishedGoods", "CurrentYearInstant_NonConsolidatedMember", "187526000000"),
    ("jppfs_cor:WorkInProcess", "CurrentYearInstant_NonConsolidatedMember", "86559000000"),
    ("jppfs_cor:RawMaterialsAndSupplies", "CurrentYearInstant_NonConsolidatedMember", "155428000000"),
]
# E02144 トヨタ 2026年度（S100Y8NY・IFRS）: 販管費・EPS・BPS の連結は未登録のタグにだけあり、単体の値が
# 採られていた。BPS の要素名は EquityToAssetRatio…だが EDINET のラベルは「１株当たり親会社所有者帰属持分」
_TOYOTA_IFRS_S100Y8NY = [
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "IFRS"),
    ("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "FilingDateInstant", "true"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31"),
    ("jpcrp030000-asr_E02144-000:OperatingRevenuesIFRSKeyFinancialData", "CurrentYearDuration", "50684952000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "18259979000000"),
    ("jpigp_cor:CostOfSalesIFRS", "CurrentYearDuration", "39141418000000"),
    ("jppfs_cor:CostOfSales", "CurrentYearDuration_NonConsolidatedMember", "14279645000000"),
    ("jppfs_cor:GrossProfit", "CurrentYearDuration_NonConsolidatedMember", "3980334000000"),
    ("jpigp_cor:SellingGeneralAndAdministrativeExpensesIFRS", "CurrentYearDuration", "4697524000000"),
    ("jppfs_cor:SellingGeneralAndAdministrativeExpenses", "CurrentYearDuration_NonConsolidatedMember", "2174945000000"),
    ("jpigp_cor:OperatingProfitLossIFRS", "CurrentYearDuration", "3766216000000"),
    ("jppfs_cor:OperatingIncome", "CurrentYearDuration_NonConsolidatedMember", "1805389000000"),
    ("jppfs_cor:OrdinaryIncome", "CurrentYearDuration_NonConsolidatedMember", "4197319000000"),
    ("jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults", "CurrentYearDuration", "295.25"),
    ("jpigp_cor:BasicAndDilutedEarningsLossPerShareIFRS", "CurrentYearDuration", "295.25"),
    ("jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "260.28"),
    ("jpcrp_cor:EquityToAssetRatioIFRSSummaryOfBusinessResults", "CurrentYearInstant", "3062.82"),
    ("jpcrp_cor:RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults", "CurrentYearInstant", "0.378"),
    ("jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", "1815.72"),
    ("jpigp_cor:LiabilitiesIFRS", "CurrentYearInstant", "64502263000000"),
    ("jppfs_cor:Liabilities", "CurrentYearInstant_NonConsolidatedMember", "7991401000000"),
    ("jppfs_cor:CurrentLiabilities", "CurrentYearInstant_NonConsolidatedMember", "6185881000000"),
    ("jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "95"),
]
# E01033 ヤスハラケミカル 2025年度（S100W071・連結を作らない）: 全ての値を NonConsolidatedMember で載せる。
# context だけでは連結を作る会社と区別できない＝判定は DEI の連結決算の有無（書類単位）
_E01033_SINGLE_S100W071 = [
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "Japan GAAP"),
    ("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "FilingDateInstant", "false"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2025-03-31"),
    ("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "14661000000"),
    ("jppfs_cor:GrossProfit", "CurrentYearDuration_NonConsolidatedMember", "3837000000"),
    ("jppfs_cor:SellingGeneralAndAdministrativeExpenses", "CurrentYearDuration_NonConsolidatedMember", "2017000000"),
    ("jppfs_cor:OperatingIncome", "CurrentYearDuration_NonConsolidatedMember", "1820000000"),
    ("jppfs_cor:OrdinaryIncome", "CurrentYearDuration_NonConsolidatedMember", "1882000000"),
    ("jppfs_cor:ProfitLoss", "CurrentYearDuration_NonConsolidatedMember", "1376000000"),
    ("jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "150.79"),
    ("jppfs_cor:Assets", "CurrentYearInstant_NonConsolidatedMember", "27468000000"),
    ("jppfs_cor:Liabilities", "CurrentYearInstant_NonConsolidatedMember", "6400000000"),
    ("jppfs_cor:NetAssets", "CurrentYearInstant_NonConsolidatedMember", "21067000000"),
    ("jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", "2321.33"),
    ("jppfs_cor:NetCashProvidedByUsedInOperatingActivities", "CurrentYearDuration_NonConsolidatedMember", "3057000000"),
    ("jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "12.00"),
    ("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant_NonConsolidatedMember", "231"),
    ("jppfs_cor:WorkInProcess", "CurrentYearInstant_NonConsolidatedMember", "2159000000"),
    ("jppfs_cor:RawMaterialsAndSupplies", "CurrentYearInstant_NonConsolidatedMember", "5883000000"),
]
# E02144 トヨタ 半期報告書（S100WYZE・2025-09-30）: 新しい様式の半期報告書は単体の値を持たない。
# 販管費・EPS の連結は #896 で登録したタグにある
_TOYOTA_H1_S100WYZE = [
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "IFRS"),
    ("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "FilingDateInstant", "true"),
    ("jpdei_cor:TypeOfCurrentPeriodDEI", "FilingDateInstant", "HY"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2025-09-30"),
    ("jpcrp040300-ssr_E02144-000:OperatingRevenuesIFRSKeyFinancialData", "InterimDuration", "24630753000000"),
    ("jpigp_cor:SellingGeneralAndAdministrativeExpensesIFRS", "InterimDuration", "2158959000000"),
    ("jpigp_cor:OperatingProfitLossIFRS", "InterimDuration", "2005692000000"),
    ("jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults", "InterimDuration", "136.07"),
    ("jpigp_cor:BasicAndDilutedEarningsLossPerShareIFRS", "InterimDuration", "136.07"),
]

# ── 売上債権（#904）: 検体は 2026-10-11 に EDINET から取得した実際の書類の行を写した ─────────
_DEI_JGAAP_CONSOLIDATED_2025_03 = [
    ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "Japan GAAP"),
    ("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "FilingDateInstant", "true"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2025-03-31"),
]
# 極東貿易 2025年3月期（S100W0AA）: 連結 BS は「受取手形、売掛金及び契約資産」。注記は文章だけで内訳のタグが無い。
# 未登録だった間は単体の売掛金（10,738百万円）が入り、#896 の後は空欄になっていた
_E8093_S100W0AA = _DEI_JGAAP_CONSOLIDATED_2025_03 + [
    ("jppfs_cor:NotesAndAccountsReceivableTradeAndContractAssets", "Prior1YearInstant", "16025000000"),
    ("jppfs_cor:NotesAndAccountsReceivableTradeAndContractAssets", "CurrentYearInstant", "20891000000"),
    ("jppfs_cor:AccountsReceivableTrade", "Prior1YearInstant_NonConsolidatedMember", "9783000000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant_NonConsolidatedMember", "10738000000"),
]
# E02769 2025年3月期（S100W4DK）: BS の合算の行（CSV 496行目）のあとに、注記の内訳（売掛金・契約資産。
# 891・893行目）が同じタグ・同じ文脈で並ぶ。契約資産が売掛金より大きい
_E02769_S100W4DK = _DEI_JGAAP_CONSOLIDATED_2025_03 + [
    ("jppfs_cor:NotesAndAccountsReceivableTradeAndContractAssets", "CurrentYearInstant", "33414000000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant", "14778000000"),
    ("jppfs_cor:ContractAssets", "CurrentYearInstant", "17940000000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant_NonConsolidatedMember", "4931000000"),
]
# E04191 2025年3月期（S100W5BJ）: BS の「受取手形及び売掛金」と注記の「売掛金」（登録済みのタグどうし）
_E04191_S100W5BJ = _DEI_JGAAP_CONSOLIDATED_2025_03 + [
    ("jppfs_cor:NotesAndAccountsReceivableTrade", "CurrentYearInstant", "37079000000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant", "36726000000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant_NonConsolidatedMember", "976000000"),
]
# E30466 2025年3月期（S100W9FR）: BS は「売掛金及び契約資産」。注記に売掛金・契約資産の内訳
_E30466_S100W9FR = _DEI_JGAAP_CONSOLIDATED_2025_03 + [
    ("jppfs_cor:AccountsReceivableTradeAndContractAssets", "CurrentYearInstant", "382131000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant", "363851000"),
    ("jppfs_cor:ContractAssets", "CurrentYearInstant", "18279000"),
    ("jppfs_cor:AccountsReceivableTradeAndContractAssets", "CurrentYearInstant_NonConsolidatedMember", "286511000"),
]
# E05332 2025年3月期（S100W5KF）: BS が「売掛金」と「契約資産」を別の行で載せる（注記に内訳のタグは無い）
_E05332_S100W5KF = _DEI_JGAAP_CONSOLIDATED_2025_03 + [
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant", "5829956000"),
    ("jppfs_cor:ContractAssets", "CurrentYearInstant", "2030603000"),
    ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant_NonConsolidatedMember", "5080999000"),
    ("jppfs_cor:ContractAssets", "CurrentYearInstant_NonConsolidatedMember", "1344147000"),
]


def _without_dei(rows):
    return [r for r in rows if not r[0].startswith("jpdei_cor:")]


def _raw(rows):
    return [{"element": e.split(":")[-1], "context": c, "value": v} for e, c, v in rows]


_ORDERS = pytest.mark.parametrize("order", ["as_filed", "reversed"])


def _parse(rows, order="as_filed", **kw):
    rows = rows if order == "as_filed" else rows[::-1]
    return parse_xbrl_csv(_xbrl_df(rows), "E02144", "", **kw)


class TestNonConsolidatedValuesAreDropped:
    """連結を作る会社の書類では、財務諸表の列に単体の値を採らない（#896）。行の並びに依存しない。"""

    @_ORDERS
    def test_usgaap_keeps_only_the_consolidated_summary(self, order):
        p = _parse(_TOYOTA_USGAAP_S100G1ZO, order)
        assert p["pl"] == {"revenue": 30225681000000.0, "net_income": 1882873000000.0,
                           "eps": 650.55}
        assert p["bs"] == {"total_assets": 51936949000000.0, "total_equity": 20565210000000.0,
                           "bps": 6830.92}
        assert p["cf"] == {"operating_cf": 3766597000000.0}

    @_ORDERS
    def test_ifrs_takes_consolidated_sga_eps_bps_and_no_ordinary_profit(self, order):
        p = _parse(_TOYOTA_IFRS_S100Y8NY, order)
        assert p["pl"]["sga"] == 4697524000000.0           # 連結（登録したタグ）。単体は 2,174,945百万円
        assert p["pl"]["operating_profit"] == 3766216000000.0
        assert p["pl"]["cost_of_sales"] == 39141418000000.0
        assert p["pl"]["eps"] == 295.25                    # 単体は 260.28
        assert p["bs"]["bps"] == 3062.82                   # 単体は 1815.72・比率 0.378 ではない
        assert p["bs"]["total_liabilities"] == 64502263000000.0
        for col in ("ordinary_profit", "gross_profit"):    # IFRS に無い概念・連結の値が無い
            assert col not in p["pl"], col
        assert "current_liabilities" not in p["bs"]

    @_ORDERS
    def test_company_level_values_are_kept(self, order):
        """1株配当（提出会社の欄にしか載らない）・従業員数・発行済株式数は規則の外。"""
        p = _parse(_TOYOTA_USGAAP_S100G1ZO, order)
        assert p["val"] == {"dps": 220.0}
        assert p["nonfin"] == {"employees": 370870.0, "issued_shares": 3310097492.0}
        assert _parse(_TOYOTA_IFRS_S100Y8NY, order)["val"] == {"dps": 95.0}

    def test_inventory_is_not_summed_from_nonconsolidated_parts(self):
        assert "inventory" not in _parse(_TOYOTA_USGAAP_S100G1ZO)["bs"]

    @_ORDERS
    def test_single_only_company_is_read_as_before(self, order):
        """連結を作らない会社（DEI=false）は、NonConsolidatedMember の値を従来どおり全部採る。"""
        p = parse_xbrl_csv(_xbrl_df(_E01033_SINGLE_S100W071 if order == "as_filed"
                                    else _E01033_SINGLE_S100W071[::-1]), "E01033", "")
        assert p["pl"] == {"revenue": 14661000000.0, "gross_profit": 3837000000.0,
                           "sga": 2017000000.0, "operating_profit": 1820000000.0,
                           "ordinary_profit": 1882000000.0, "net_income": 1376000000.0,
                           "eps": 150.79}
        assert p["bs"] == {"total_assets": 27468000000.0, "total_liabilities": 6400000000.0,
                           "total_equity": 21067000000.0, "bps": 2321.33,
                           "inventory": 2159000000.0 + 5883000000.0}
        assert p["cf"] == {"operating_cf": 3057000000.0}
        assert p["val"] == {"dps": 12.0} and p["nonfin"] == {"employees": 231.0}

    def test_document_without_dei_is_read_as_before(self):
        """DEI が読めない書類は従来どおり（単体の値で埋まる）。規則で値をまとめて消さない。"""
        p = _parse(_without_dei(_TOYOTA_USGAAP_S100G1ZO))
        assert p["pl"]["operating_profit"] == 1326137000000.0
        assert p["pl"]["revenue"] == 30225681000000.0      # 連結の優先は従来どおり

    def test_fallback_reads_the_old_way(self):
        """掃除の差分（refetch_financials --clear-nonconsolidated）が比べる旧来の読み方。"""
        p = _parse(_TOYOTA_USGAAP_S100G1ZO, nonconsolidated_fallback=True)
        assert p["pl"]["operating_profit"] == 1326137000000.0
        assert p["bs"]["total_liabilities"] == 5266718000000.0
        assert p["bs"]["inventory"] == 187526000000.0 + 86559000000.0 + 155428000000.0

    @_ORDERS
    def test_interim_report_takes_the_new_consolidated_tags(self, order):
        p = _parse(_TOYOTA_H1_S100WYZE, order)
        assert p["pl"]["sga"] == 2158959000000.0
        assert p["pl"]["eps"] == 136.07
        assert p["pl"]["operating_profit"] == 2005692000000.0

    @pytest.mark.parametrize("rows", [
        _TOYOTA_USGAAP_S100G1ZO, _TOYOTA_IFRS_S100Y8NY, _E01033_SINGLE_S100W071, _TOYOTA_H1_S100WYZE,
        _E8093_S100W0AA, _E02769_S100W4DK, _E04191_S100W5BJ, _E30466_S100W9FR, _E05332_S100W5KF,
    ], ids=["usgaap", "ifrs", "single-only", "h1",
            "recv-combined", "recv-combined-and-note", "recv-notes-and-ar", "recv-ar-and-ca",
            "recv-ar-only"])
    @_ORDERS
    def test_parse_raw_rows_agrees(self, rows, order):
        rows = rows if order == "as_filed" else rows[::-1]
        csv = parse_xbrl_csv(_xbrl_df(rows), "E02144", "")
        raw = parse_raw_rows(_raw(rows))
        for cat in ("bs", "pl", "cf", "val", "nonfin"):
            assert raw[cat] == csv[cat], cat

    def test_new_consolidated_tags_are_mapped(self):
        assert XBRL_MAP["SellingGeneralAndAdministrativeExpensesIFRS"] == ("pl", "sga")
        assert XBRL_MAP["BasicEarningsLossPerShareIFRSSummaryOfBusinessResults"] == ("pl", "eps")
        assert XBRL_MAP["BasicAndDilutedEarningsLossPerShareIFRS"] == ("pl", "eps")
        assert XBRL_MAP["EquityToAssetRatioIFRSSummaryOfBusinessResults"] == ("bs", "bps")
        assert "RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults" not in XBRL_MAP

    @pytest.mark.parametrize("value, expected", [
        ("true", True), ("false", False), ("TRUE", True), ("", None), ("－", None),
    ])
    def test_prepares_consolidated(self, value, expected):
        dei = {"WhetherConsolidatedFinancialStatementsArePreparedDEI": value}
        assert collector_financials.prepares_consolidated(dei) is expected

    def test_members_other_than_nonconsolidated_are_not_dropped(self):
        """セグメント・株式種類のメンバーは単体ではない（規則は NonConsolidated だけ）。"""
        drops = collector_financials._drops_nonconsolidated
        assert drops("CurrentYearDuration_NonConsolidatedMember", "pl", True)
        assert not drops("CurrentYearDuration_NonConsolidatedMember", "pl", False)
        assert not drops("CurrentYearDuration_NonConsolidatedMember", "val", True)
        assert not drops("CurrentYearInstant_NonConsolidatedMember", "nonfin", True)
        assert not drops(
            "CurrentYearDuration_jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember",
            "pl", True)
        assert not drops("FilingDateInstant_OrdinaryShareMember", "bs", True)

    def test_rule_lives_in_one_place(self):
        """単体の判定の式は `_drops_nonconsolidated` だけに置く（呼び出し側へ書き写さない）。"""
        import inspect
        for fn in (collector_financials._apply_row, collector_financials._collect_inventory_row):
            assert "_drops_nonconsolidated(" in inspect.getsource(fn), fn.__name__


class TestReceivablesTakeTheBalanceSheetLine:
    """売上債権は BS に載る行を採る。注記の内訳の「売掛金」が同じ文脈で並んでも、行の並びに依存しない（#904）。"""

    @_ORDERS
    @pytest.mark.parametrize("rows, expected", [
        (_E8093_S100W0AA, 20891000000.0),    # 合算の行だけ。単体の 10,738百万円ではない
        (_E02769_S100W4DK, 33414000000.0),   # 注記の売掛金 14,778百万円ではない
        (_E04191_S100W5BJ, 37079000000.0),   # 注記の売掛金 36,726百万円ではない
        (_E30466_S100W9FR, 382131000.0),     # 注記の売掛金 363,851千円ではない
        (_E05332_S100W5KF, 5829956000.0),    # BS の行が売掛金（契約資産は別の行）
    ], ids=["combined", "combined-and-note", "notes-and-ar", "ar-and-ca", "ar-only"])
    def test_balance_sheet_line_wins(self, rows, expected, order):
        assert _parse(rows, order)["bs"]["receivables"] == expected

    def test_combined_tags_are_mapped(self):
        assert XBRL_MAP["NotesAndAccountsReceivableTradeAndContractAssets"] == ("bs", "receivables")
        assert XBRL_MAP["AccountsReceivableTradeAndContractAssets"] == ("bs", "receivables")
        assert "ContractAssets" not in XBRL_MAP   # 契約資産だけの行は売上債権の列へ足さない

    def test_tags_losing_ties_are_registered(self):
        """順位を下げるタグは登録済みの列のタグであること（綴り違いだと黙って効かない）。"""
        for tag in collector_financials._TAGS_LOSING_TIES:
            assert tag in XBRL_MAP, tag

    def test_context_priority_still_comes_first(self):
        """タグの順位は文脈の優先度が同じときだけ効く。連結の売掛金は単体の合算の行に勝つ。"""
        rows = [
            ("jppfs_cor:AccountsReceivableTrade", "CurrentYearInstant", "100"),
            ("jppfs_cor:NotesAndAccountsReceivableTrade", "CurrentYearInstant_NonConsolidatedMember", "200"),
        ]
        for order in ("as_filed", "reversed"):
            assert _parse(rows, order)["bs"]["receivables"] == 100.0


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

    def test_missing_operating_profit_yields_none_not_zero(self):
        """営業利益が取れない行は営業利益率を 0% にせず、営業利益から作る派生額も作らない（#896）。"""
        rec = self._rec()
        del rec["pl"]["operating_profit"]
        rec["pl"]["depreciation"] = 30.0
        out = calc_derived(rec)
        assert out["derived"]["op_margin"] is None
        assert out["derived"]["net_margin"] == 10.0
        assert "ebitda" not in out["pl"] and "nonoperating_income" not in out["pl"]

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

    def test_http_error_raises_instead_of_returning_empty(self):
        """失敗は `[]` にしない（#577）。

        旧実装は `except Exception` で `[]` を返しており、このテスト自身が
        「失敗も空リスト」という**穴のほうをピン留め**していた。EDINET が 2026-08-29 に
        ホストを移設して 301 を返し始めたとき、892/892 の失敗が「提出ゼロの日が892日続いた」
        に化けて `exit=0` で通ったのはこの契約が原因である。
        """
        client = _client(_const(httpx.Response(500)))
        with pytest.raises(EdinetAccessError):
            asyncio.run(fetch_doc_list(client, date(2023, 6, 30)))

    def test_redirect_is_not_silently_empty(self):
        """301/302 を黙って空にしない（旧ホストの再発を型で止める）。

        `disclosure.edinet-fsa.go.jp` は 301 を返すようになったが、リダイレクト先は
        API ではなく人間用画面なので `follow_redirects=True` も解ではない。どちらにせよ
        **JSON が取れないことが例外として現れる**ことだけをここで固定する。
        """
        client = _client(_const(httpx.Response(
            301, headers={"location": "https://disclosure2.edinet-fsa.go.jp/api/v2/documents.json"})))
        with pytest.raises(EdinetAccessError):
            asyncio.run(fetch_doc_list(client, date(2023, 6, 30)))

    def test_error_message_redacts_api_key(self):
        """例外文字列にクエリ付き URL が乗るので、キーが素通りしないこと（#577）。"""
        key = "s3cret-edinet-subscription-key"
        os.environ["EDINET_API_KEY"] = key
        try:
            client = _client(_const(httpx.Response(
                401, request=httpx.Request(
                    "GET", f"https://api.edinet-fsa.go.jp/api/v2/documents.json?Subscription-Key={key}"))))
            with pytest.raises(EdinetAccessError) as ei:
                asyncio.run(fetch_doc_list(client, date(2023, 6, 30)))
            assert key not in str(ei.value)
        finally:
            os.environ.pop("EDINET_API_KEY", None)


class TestScanAbortsOnConsecutiveFailures:
    """`collect_doc_ids_for_period` は単発失敗を許し、連続失敗で打ち切る（#577）。"""

    def _run(self, handler, days: int):
        client = _client(handler)
        return asyncio.run(collect_doc_ids_for_period(
            client, date(2023, 6, 1), date(2023, 6, 1) + timedelta(days=days - 1)))

    def test_isolated_failure_is_tolerated(self, monkeypatch):
        # 差分収集は翌日に同じ日付を再スキャンするので、単発の失敗で全体を止めない。
        monkeypatch.setattr(collector_financials, "RATE_SLEEP", 0)
        ok = {"results": [
            {"ordinanceCode": "010", "formCode": "030000", "secCode": "1301", "edinetCode": "E00001"}]}
        out = self._run(_queue(httpx.Response(500),
                               httpx.Response(200, json=ok),
                               httpx.Response(200, json=ok)), days=3)
        assert [d["edinetCode"] for d in out] == ["E00001", "E00001"]

    def test_consecutive_failures_abort_the_scan(self, monkeypatch):
        monkeypatch.setattr(collector_financials, "RATE_SLEEP", 0)
        # 窓は上限よりずっと長いが、上限に達した時点で送出するので全部は叩かない。
        with pytest.raises(EdinetAccessError) as ei:
            self._run(_const(httpx.Response(500)), days=EDINET_MAX_CONSECUTIVE_FAILURES * 5)
        assert ei.value.consecutive == EDINET_MAX_CONSECUTIVE_FAILURES

    def test_success_resets_the_counter(self, monkeypatch):
        """成功を挟めばカウンタは 0 へ戻る（「通算 N 回」で誤爆させない）。"""
        monkeypatch.setattr(collector_financials, "RATE_SLEEP", 0)
        ok = httpx.Response(200, json={"results": []})
        n = EDINET_MAX_CONSECUTIVE_FAILURES
        # 失敗 N-1 → 成功 → 失敗 N-1。通算は 2N-2 回だが連続は一度も N に達しない。
        seq = [httpx.Response(500)] * (n - 1) + [ok] + [httpx.Response(500)] * (n - 1)
        assert self._run(_queue(*seq), days=len(seq)) == []


class TestRedactSecrets:
    def test_masks_long_secret_values(self):
        os.environ["FAKE_API_KEY"] = "abcdefgh-very-secret"
        try:
            assert "abcdefgh-very-secret" not in redact_secrets("boom: abcdefgh-very-secret")
        finally:
            os.environ.pop("FAKE_API_KEY", None)

    def test_ignores_short_values(self):
        # 短い値を消すと無関係な文字列まで壊れる（`PASS=1` で "1" が全滅する）。
        os.environ["FAKE_PASSWORD"] = "1234"
        try:
            assert redact_secrets("port 1234") == "port 1234"
        finally:
            os.environ.pop("FAKE_PASSWORD", None)

    def test_ignores_non_secret_names(self):
        os.environ["FAKE_PUBLIC_URL"] = "https://example.com/public"
        try:
            assert redact_secrets("https://example.com/public") == "https://example.com/public"
        finally:
            os.environ.pop("FAKE_PUBLIC_URL", None)

    def test_hints_match_batch_common(self):
        """`scripts/batch_common._SECRET_HINTS` の写しが黙って割れないよう CI で照合する。

        機構は別（あちらは**名前**で伏せる／こちらは**値**を消す）だが語彙は同じであるべきで、
        片方にだけ新しい手掛かりが足されると、もう片方が素通りし始める。
        """
        from scripts import batch_common
        assert collector_utils.SECRET_HINTS == batch_common._SECRET_HINTS


class TestEdinetBase:
    def test_points_at_migrated_host(self):
        # 旧ホストは 301 を返す（#577）。戻したら収集が無言で 0 件になる。
        assert collector_utils.EDINET_BASE == "https://api.edinet-fsa.go.jp/api/v2"

    def test_no_hardcoded_edinet_urls_remain(self):
        """URL 直書きが再び散らないこと（`yahoo_ticker` 集約・#555 と同じ作法）。

        ホスト移設で実際に取り残されたのは `edinet_ping.py` の2箇所だった＝**疎通確認**
        だけが旧ホストを見ていると、本体が直っているのに ping が落ちて切り分けが濁る。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in os.listdir(root):
            if not name.endswith(".py"):
                continue
            body = open(os.path.join(root, name), encoding="utf-8").read()
            for line in body.splitlines():
                if "edinet-fsa.go.jp" in line and "EDINET_BASE   =" not in line \
                        and "EDINET_CODELIST_URL =" not in line \
                        and not line.lstrip().startswith("#"):
                    offenders.append(f"{name}: {line.strip()}")
        assert not offenders, ("EDINET の URL は collector_utils.EDINET_BASE（書類 API）か "
                               "EDINET_CODELIST_URL（コードリスト・#784）経由にする: " + str(offenders))


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


class TestXbrlFetchStats:
    """失敗を理由別に数える（#630）。理由を1つに畳むと恒久的失敗と一時的失敗が区別できない。"""

    def test_counts_badzip(self):
        # EDINET は CSV 形式を持たない書類へ HTTP 200 で JSON 本文を返す（実測・#630）。
        body = b'{"metadata":{"status":"404","message":"Not Found"}}'
        client = _client(_const(httpx.Response(200, content=body)))
        with collector_financials.xbrl_fetch_stats() as stats:
            assert asyncio.run(fetch_xbrl_csv(client, "S100T096")) is None
        assert stats["badzip"] == 1
        assert stats["no_csv"] == stats["http"] == stats["other"] == 0

    def test_counts_no_csv(self):
        client = _client(_const(httpx.Response(200, content=_zip_bytes("readme.txt", b"hello"))))
        with collector_financials.xbrl_fetch_stats() as stats:
            assert asyncio.run(fetch_xbrl_csv(client, "S100ABCD")) is None
        assert stats["no_csv"] == 1
        assert stats["badzip"] == 0

    def test_counts_http_error(self):
        client = _client(_const(httpx.Response(503, content=b"upstream down")))
        with collector_financials.xbrl_fetch_stats() as stats:
            assert asyncio.run(fetch_xbrl_csv(client, "S100ABCD")) is None
        assert stats["http"] == 1
        assert stats["badzip"] == stats["no_csv"] == 0

    def test_outside_context_does_not_raise(self):
        # 集計を張っていない経路（通期収集・単社更新）からも従来どおり呼べる。
        client = _client(_const(httpx.Response(200, content=b"not a zip")))
        assert asyncio.run(fetch_xbrl_csv(client, "S100ABCD")) is None

    def test_format_emits_zero_counts(self):
        # 0 のときも出す。出ていないことが読めないと監視にならない。
        line = collector_financials.format_xbrl_fetch_stats({"badzip": 2})
        assert "badzip=2" in line
        for k in ("no_csv", "oversize", "http", "other"):
            assert f"{k}=0" in line


class TestJquantsFetchDate:
    def test_single_page(self):
        payload = {"data": [{"Code": "13010", "Date": "2023-09-01", "C": 2000}]}
        client = _client(_const(httpx.Response(200, json=payload)))
        assert asyncio.run(_jquants_fetch_date(client, "key", "2023-09-01")) == payload["data"]

    def test_400_returns_empty(self):
        client = _client(_const(httpx.Response(400)))
        assert asyncio.run(_jquants_fetch_date(client, "key", "2023-01-01")) == []

    def test_403_raises_access_error_with_reason(self):
        """403 は raise_for_status ではなく JQuantsAccessError で返し、
        ボディから理由を分類して呼び出し側に切り分けさせる（#412・#461・#462）。

        **カバレッジ境界はここに来ない**（境界は 400・#462 実測）。403 は契約失効・
        プラン対象外・URL 不在のいずれかで、どれも日付を変えても直らない。
        """
        from collector_prices import JQuantsAccessError

        cases = [
            ('{"message": "No active subscription found."}', "no_subscription"),
            ('{"message": "This API is not available on your subscription."}', "plan_restricted"),
            ('{"message": "The requested endpoint does not exist."}', "endpoint_missing"),
            ("", "unknown"),
        ]
        for body, expected in cases:
            client = _client(_const(httpx.Response(403, text=body)))
            with pytest.raises(JQuantsAccessError) as ei:
                asyncio.run(_jquants_fetch_date(client, "key", "2024-08-01"))
            assert ei.value.reason == expected
            assert ei.value.no_subscription is (expected == "no_subscription")

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


class TestJquantsFetchCode:
    """銘柄単位の履歴取得（#466）。**日付単位経路しか無かったため**、#466 本文は
    「523営業日の全走査（≒174分）が公式値を取る唯一の経路」と書いていたが、これは
    v1 時点の記述で、v2 は `code=` で1銘柄ぶんを1リクエストで返す（実測 487行）。

    エラー処理は `_jquants_fetch_date` と**同じ規約**であること。揃っていないと、
    片方だけが知っている失敗の形が出たときに握りつぶす。
    """

    def test_sends_code_and_period(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"data": [{"Date": "2025-01-06", "AdjC": 1.0}]})

        rows = asyncio.run(_jquants_fetch_code(
            _client(handler), "key", "82270", "2024-06-06", "2026-06-06"))
        assert seen["code"] == "82270"
        assert seen["from"] == "2024-06-06" and seen["to"] == "2026-06-06"
        assert len(rows) == 1

    def test_pagination_is_followed(self, monkeypatch):
        async def _noop(*a, **k):
            pass
        monkeypatch.setattr(collector.asyncio, "sleep", _noop)
        client = _client(_queue(
            httpx.Response(200, json={"data": [{"Date": "2025-01-06"}], "pagination_key": "K2"}),
            httpx.Response(200, json={"data": [{"Date": "2025-01-07"}]}),
        ))
        rows = asyncio.run(_jquants_fetch_code(client, "key", "82270", "a", "b"))
        assert [r["Date"] for r in rows] == ["2025-01-06", "2025-01-07"]

    def test_429_then_success(self, monkeypatch):
        async def _noop(*a, **k):
            pass
        monkeypatch.setattr(collector.asyncio, "sleep", _noop)
        client = _client(_queue(
            httpx.Response(429),
            httpx.Response(200, json={"data": [{"Date": "2025-01-06"}]}),
        ))
        rows = asyncio.run(_jquants_fetch_code(client, "key", "82270", "a", "b"))
        assert len(rows) == 1

    def test_400_without_coverage_text_is_empty(self):
        """上場前・廃止後などは空で正常終了する（例外にしない）。"""
        rows = asyncio.run(_jquants_fetch_code(
            _client(_const(httpx.Response(400, text="no data"))), "key", "99990", "a", "b"))
        assert rows == []


class TestCliOutputSurvivesCp932:
    """**cp932 コンソールへリダイレクトすると出力済みの内容ごとクラッシュする。**

    2026-08-29 に `--repair-price-breaks` の結果表示が `U+2014 EM DASH` で
    `UnicodeEncodeError` を投げ、**8分ぶんの J-Quants 突合結果が丸ごと捨てられた**
    （cp932 に EM DASH は無い。見た目が同じ `U+2015 HORIZONTAL BAR` は在る）。
    個別の記号を1つずつ直すのは過去に2度繰り返しているので、ここで縛る。
    """

    def test_collector_source_is_cp932_encodable(self):
        src = io.open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "collector.py"), encoding="utf-8").read()
        try:
            src.encode("cp932")
        except UnicodeEncodeError as e:
            bad = src[e.start:e.end]
            raise AssertionError(
                f"collector.py に cp932 で出せない文字がある: {bad!r} "
                f"(U+{ord(bad[0]):04X})。リダイレクト時に出力ごとクラッシュする"
            ) from None
