"""保有の口座別レポート（scripts/holdings_report.py）のテスト。

検体の見出し・列名・数値の書き方（`+` 付きの損益・行末の空白・`口` 付きの口数・小数の現在値・全角の
括弧と英数）は、2026-11-22 の実際の書き出しから写した。銘柄・株数・金額はすべて作り物
（リポジトリが PUBLIC なので、実際の保有は書かない）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import holdings_report as M  # noqa: E402

_SAMPLE = """
保有証券一覧

株式（特定預り）合計

評価額合計,評価損益合計
243150,+13150

株式（特定預り）

銘柄コード,銘柄名称,保有株数,売却注文中,取得単価,現在値,取得金額,評価額,評価損益
"1111","架空食品",100,,1000,1200,100000,120000,+20000
"2222","架空電機",200,,500,450,100000,90000,-10000
"130A","架空新興",300,,100,110.5,30000,33150,+3150

株式（NISA預り（成長投資枠））合計

評価額合計,評価損益合計
335000,+25000

株式（NISA預り（成長投資枠））

銘柄コード,銘柄名称,保有株数,売却注文中,取得単価,現在値,取得金額,評価額,評価損益
"1111","架空食品",100,,900,1200,90000,120000,+30000
"3333","架空リート",2,,100000,95000,200000,190000,-10000
"4444","架空ＥＴＦ",10,,2000,2500,20000,25000,+5000

株式（旧NISA預り）合計

評価額合計,評価損益合計
220000,-10000

株式（旧NISA預り）

銘柄コード,銘柄名称,保有株数,売却注文中,取得単価,現在値,取得金額,評価額,評価損益
"5555","架空機械",100,,800,1000,80000,100000,+20000
"6666","架空商事",100,,1500,1200,150000,120000,-30000

投資信託（金額/NISA預り（成長投資枠））合計

評価額合計,評価損益合計
250000,+50000

投資信託（金額/NISA預り（成長投資枠））

ファンド名,保有口数,売却注文中,取得単価,基準価額,取得金額,評価額,評価損益,分配金受取方法
"架空インデックス　全世界株式（オール）",100000口,,20000,25000,200000,250000,+50000,再投資

投資信託（金額/NISA預り（つみたて投資枠））合計

評価額合計,評価損益合計
500000,+80000

投資信託（金額/NISA預り（つみたて投資枠））

ファンド名,保有口数,売却注文中,取得単価,基準価額,取得金額,評価額,評価損益,分配金受取方法
"架空インデックス　全世界株式（オール）",200000口,,21000,25000,420000,500000,+80000,再投資




"""
# 実際の書き出しは株式の明細の行末に空白が 1 つ付く（エディタが行末の空白を消すので、ここで足す）
SAMPLE = "\n".join(line + " " if re.match(r'^"[0-9][0-9A-Z]{3}",', line) else line
                   for line in _SAMPLE.split("\n"))

YUTAI = """﻿code,name,has_yutai,min_shares,record_months,content,est_value_yen,source_url,checked_on,confirmed
1111,架空食品,あり,100,3,自社製品,2000,https://example.com/1111,2026-11-22,済
2222,架空電機,なし,,,,,https://example.com/2222,2026-11-22,
3333,架空リート,不明,,,,,,2026-11-22,
5555,架空機械,なし,,,,,,2026-11-22,済
6666,架空商事,あり,200,9,割引券,,,2026-11-22,
"""

FUND = "fund:架空インデックス 全世界株式(オール)"
AS_OF = date(2026, 11, 22)

# 特定口座の譲渡損益の明細。見出し・期間の行（列名の行頭の空白を含む）・合計の見出し・注記・列名・
# 銘柄コードの後ろの空白・`株` 付きの数量・`--`・税の行の形は 2026-11-22 の実際の書き出しから写した。
# 銘柄・日付・金額は作り物。売り 3 件（+10000・-2000・0）と配当 1 件、税は 徴収 2031 − 還付 406 = 1625。
REALIZED = """
特定口座損益明細

 受渡開始年月日,受渡終了年月日,明細数,明細指定開始,明細指定終了
"2026年01月01日","2026年12月31日","4","1","4"

譲渡益税徴収額合計
1625

損益金額合計
8000

配当所得税徴収額合計
203

配当金額等合計 (税引き前)
1000

（注）明細数および合計金額はご指定された期間の合計です。

銘柄コード,銘柄,譲渡益取消区分,約定日,数量,取引,受渡日,売却/決済金額,費用,取得/新規年月日,取得/新規金額,損益金額/徴収額,地方税
"1111     ","架空食品"," ","2026/02/02",100株,"現物売","2026/02/04",120000,--,"2025/05/01",110000,+10000
譲渡益税徴収額,,,,,,"2026/02/04",,,,,2031,500
"2222     ","架空電機"," ","2026/03/02",100株,"現物売","2026/03/04",48000,--,"2026/01/05",50000,-2000
譲渡益税還付金,,,,,,"2026/03/04",,,,,406,100
"5555     ","架空機械"," ","2026/06/01",100株,"株式配当金","2026/06/03",--,--,"----/--/--",--,+1000
配当所得税徴収額,,,,,,"2026/06/03",,,,,203,50
"130A     ","架空新興"," ","2026/07/01",300株,"現物売","2026/07/03",30000,--,"2026/02/01",30000,--



"""

OLD_NISA_YEARS = "﻿code,buy_year\n5555,2022\n"


def _realized(text: str = REALIZED, year: int = 2026):
    return M.parse_realized_csv(text.encode("cp932"), year)


def _bytes(text: str = SAMPLE) -> bytes:
    return text.encode("cp932")


def _parsed():
    return M.parse_holdings_csv(_bytes())


def _info():
    """DB の補足の作り物（ETF・REIT・個別株の区分と、配当利回り）。"""
    return M.merge_security_info(
        ["1111", "2222", "130A", "3333", "4444", "5555", "6666"],
        {"1111": "プライム（内国株式）", "2222": "スタンダード（内国株式）", "130A": "グロース（内国株式）",
         "3333": "REIT・ベンチャーファンド・カントリーファンド・インフラファンド", "4444": "ETF・ETN",
         "5555": "プライム（内国株式）", "6666": "プライム（内国株式）"},
        {"1111": "食料品", "2222": "電気機器", "130A": "情報・通信業", "5555": "機械", "6666": "卸売業"},
        {"1111": 2.0, "2222": 3.0, "5555": 1.0},
    )


def _report(info=None, yutai=None, **kw):
    return M.build_report(_parsed(), _info() if info is None else info,
                          M.parse_yutai_csv(YUTAI) if yutai is None else yutai, AS_OF,
                          db_used=info is None, source="sample.csv", **kw)


class TestParse:
    def test_reads_every_section(self):
        p = _parsed()
        by = {}
        for h in p.holdings:
            by.setdefault((h.kind, h.account), []).append(h)
        assert {k: len(v) for k, v in by.items()} == {
            (M.KIND_STOCK, M.ACCOUNT_TAXABLE): 3,
            (M.KIND_STOCK, M.ACCOUNT_NISA_GROWTH): 3,
            (M.KIND_STOCK, M.ACCOUNT_OLD_NISA): 2,
            (M.KIND_FUND, M.ACCOUNT_NISA_GROWTH): 1,
            (M.KIND_FUND, M.ACCOUNT_NISA_TSUMITATE): 1,
        }
        assert p.totals[(M.KIND_STOCK, M.ACCOUNT_TAXABLE)] == (243150, 13150)

    def test_letter_code_decimal_price_and_fund_units(self):
        p = _parsed()
        new = next(h for h in p.holdings if h.code == "130A")
        assert (new.price, new.quantity, new.pnl) == (110.5, 300.0, 3150)
        fund = next(h for h in p.holdings if h.account == M.ACCOUNT_NISA_TSUMITATE)
        assert fund.code is None and fund.quantity == 200000.0
        assert fund.key == FUND                       # 全角の空白・括弧を NFKC でそろえる
        assert next(h for h in p.holdings if h.code == "4444").name == "架空ETF"

    def test_total_mismatch_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="合計の行"):
            M.parse_holdings_csv(_bytes(SAMPLE.replace("243150,+13150", "243151,+13150")))

    def test_sample_keeps_the_trailing_space_of_stock_rows(self):
        assert '"130A","架空新興",300,,100,110.5,30000,33150,+3150 \n' in SAMPLE

    def test_row_that_does_not_add_up_raises(self):
        bad = SAMPLE.replace('100000,120000,+20000 ', '100000,120000,+20001 ')
        assert bad != SAMPLE
        with pytest.raises(M.HoldingsCsvError, match="評価損益"):
            M.parse_holdings_csv(_bytes(bad))

    def test_unknown_account_raises(self):
        bad = SAMPLE.replace("株式（旧NISA預り）", "株式（外国預り）")
        with pytest.raises(M.HoldingsCsvError, match="知らない口座"):
            M.parse_holdings_csv(_bytes(bad))

    def test_unknown_fund_unit_raises(self):
        bad = SAMPLE.replace("金額/NISA預り（つみたて投資枠）", "単位/NISA預り（つみたて投資枠）")
        with pytest.raises(M.HoldingsCsvError, match="形を知らない"):
            M.parse_holdings_csv(_bytes(bad))

    def test_column_count_mismatch_raises(self):
        bad = SAMPLE.replace('"2222","架空電機",200,,', '"2222","架空電機",200,')
        with pytest.raises(M.HoldingsCsvError, match="列の数"):
            M.parse_holdings_csv(_bytes(bad))

    def test_changed_header_raises(self):
        bad = SAMPLE.replace("銘柄コード,銘柄名称,保有株数", "銘柄コード,銘柄名,保有株数", 1)
        with pytest.raises(M.HoldingsCsvError, match="列名の行"):
            M.parse_holdings_csv(_bytes(bad))

    def test_section_without_total_raises(self):
        bad = SAMPLE.replace("株式（旧NISA預り）合計\n\n評価額合計,評価損益合計\n220000,-10000\n", "")
        with pytest.raises(M.HoldingsCsvError, match="片方しか無い"):
            M.parse_holdings_csv(_bytes(bad))

    def test_not_cp932_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="cp932"):
            M.parse_holdings_csv(b"\x82")

    def test_stray_line_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="見出し"):
            M.parse_holdings_csv(_bytes("保有証券一覧\n\nなにか,別の,表\n"))


class TestYutai:
    def test_reads_statuses_and_confirmation(self):
        y = M.parse_yutai_csv(YUTAI)                  # BOM 付きでも読める
        assert y["1111"].status == M.YUTAI_YES and y["1111"].confirmed
        assert y["1111"].min_shares == 100 and y["1111"].est_value_yen == 2000
        assert y["3333"].status == M.YUTAI_UNKNOWN and not y["3333"].confirmed

    def test_bad_status_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="has_yutai"):
            M.parse_yutai_csv("code,has_yutai\n1111,ある\n")

    def test_duplicate_code_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="2 回"):
            M.parse_yutai_csv("code,has_yutai\n1111,あり\n1111,なし\n")

    def test_missing_column_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="has_yutai"):
            M.parse_yutai_csv("code,name\n1111,架空\n")


class TestRealized:
    def test_reads_totals_and_rows(self):
        r = _realized()
        assert (r.start, r.end) == (date(2026, 1, 1), date(2026, 12, 31))
        assert (r.sale_pnl, r.sale_tax, r.dividends, r.div_tax) == (8000, 1625, 1000, 203)
        assert r.sells == 3 and len(r.rows) == 4
        zero = next(x for x in r.rows if x.code == "130A")          # 損益の `--` は 0
        assert (zero.amount, zero.quantity, zero.trade) == (0, 300.0, M.TRADE_SELL)
        div = next(x for x in r.rows if x.trade == M.TRADE_DIVIDEND)
        assert (div.code, div.amount, div.settle_date) == ("5555", 1000, date(2026, 6, 3))

    def test_sample_keeps_the_leading_space_and_padded_codes(self):
        assert "\n 受渡開始年月日," in REALIZED and '"1111     ",' in REALIZED

    @pytest.mark.parametrize("old, new, msg", [
        ("損益金額合計\n8000", "損益金額合計\n8001", "損益金額合計"),
        ("譲渡益税徴収額合計\n1625", "譲渡益税徴収額合計\n2031", "譲渡益税徴収額合計"),     # 還付を引いていない
        ("配当金額等合計 (税引き前)\n1000", "配当金額等合計 (税引き前)\n999", "配当金額等合計"),
        ('"4","1","4"', '"5","1","5"', "明細数"),
    ])
    def test_totals_must_match_the_rows(self, old, new, msg):
        bad = REALIZED.replace(old, new)
        assert bad != REALIZED
        with pytest.raises(M.HoldingsCsvError, match=msg):
            _realized(bad)

    def test_partial_export_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="全件"):
            _realized(REALIZED.replace('"4","1","4"', '"4","1","2"'))

    def test_period_must_start_on_january_first_of_the_year(self):
        with pytest.raises(M.HoldingsCsvError, match="1 月 1 日"):
            _realized(year=2027)
        with pytest.raises(M.HoldingsCsvError, match="1 月 1 日"):
            _realized(REALIZED.replace('"2026年01月01日"', '"2026年04月01日"'))

    def test_row_of_another_year_raises(self):
        bad = REALIZED.replace('"2026/02/04",120000', '"2025/12/26",120000')
        assert bad != REALIZED
        with pytest.raises(M.HoldingsCsvError, match="2026 年でない"):
            _realized(bad)

    def test_unknown_trade_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="知らない取引"):
            _realized(REALIZED.replace('"現物売","2026/03/04"', '"信用返済売","2026/03/04"'))

    def test_unknown_tax_row_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="列の数"):
            _realized(REALIZED.replace("譲渡益税還付金,", "譲渡益税戻し,"))

    def test_cancelled_transfer_flag_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="譲渡益取消区分"):
            _realized(REALIZED.replace('"架空電機"," "', '"架空電機","取消"'))

    def test_changed_header_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="列名の行"):
            _realized(REALIZED.replace("売却/決済金額,費用", "売却金額,費用"))

    def test_holdings_csv_is_not_a_statement(self):
        with pytest.raises(M.HoldingsCsvError, match="特定口座損益明細"):
            _realized(SAMPLE)

    def test_not_cp932_raises(self):
        with pytest.raises(M.HoldingsCsvError, match="cp932"):
            M.parse_realized_csv(b"\x82", 2026)


class TestOldNisaYears:
    def test_reads_years_with_bom(self):
        assert M.parse_old_nisa_years_csv(OLD_NISA_YEARS) == {"5555": 2022}

    @pytest.mark.parametrize("text, msg", [
        ("code,buy_year\n5555,2024\n", "2014〜2023"),
        ("code,buy_year\n5555,2022\n5555,2023\n", "2 回"),
        ("code,year\n5555,2022\n", "buy_year"),
        ("code,buy_year\n55,2022\n", "証券コード"),
        ("code,buy_year\n5555,\n", "数値でない"),
    ])
    def test_bad_rows_raise(self, text, msg):
        with pytest.raises(M.HoldingsCsvError, match=msg):
            M.parse_old_nisa_years_csv(text)


class TestAssetType:
    def test_segments(self):
        p = _parsed()
        stock = next(h for h in p.holdings if h.code == "1111")
        fund = next(h for h in p.holdings if h.code is None)
        assert M.asset_type_of(fund, None) == M.ASSET_FUND
        assert M.asset_type_of(stock, None) == M.ASSET_UNKNOWN
        assert M.asset_type_of(stock, "ETF・ETN") == M.ASSET_ETF
        assert M.asset_type_of(stock, "REIT・ベンチャーファンド・カントリーファンド・インフラファンド") == M.ASSET_REIT
        assert M.asset_type_of(stock, "出資証券") == M.ASSET_OTHER
        assert M.asset_type_of(stock, "プライム（内国株式）") == M.ASSET_STOCK

    def test_segment_names_match_the_collector(self):
        """区分の名前は collector_utils の一覧と同じ（写した値が黙って古くならないように）。"""
        from collector_utils import JPX_NON_MARKET_SEGMENTS
        assert M.SEGMENT_ETF in JPX_NON_MARKET_SEGMENTS
        assert M.SEGMENT_OTHER in JPX_NON_MARKET_SEGMENTS
        assert any(s.startswith(M.SEGMENT_REIT_PREFIX) for s in JPX_NON_MARKET_SEGMENTS)

    def test_merge_drops_abnormal_yield_and_dash_industry(self):
        info = M.merge_security_info(["1111", "2222"], {}, {"1111": "-"}, {"1111": 45.0, "2222": 2.5})
        assert info["1111"].div_yield is None and info["1111"].industry is None
        assert info["2222"].div_yield == 2.5


class TestViews:
    def test_summary_totals(self):
        s = _report()["summary"]
        assert (s["total"]["value"], s["total"]["pnl"], s["total"]["count"]) == (1_548_150, 158_150, 10)
        taxable = next(r for r in s["subtotals"] if r["account"] == M.ACCOUNT_TAXABLE)
        assert (taxable["value"], taxable["pnl"]) == (243150, 13150)
        growth = [r for r in s["rows"] if r["account"] == M.ACCOUNT_NISA_GROWTH]
        assert {r["asset"] for r in growth} == {M.ASSET_STOCK, M.ASSET_REIT, M.ASSET_ETF, M.ASSET_FUND}

    def test_taxable_tax_estimates(self):
        t = _report()["taxable"]
        assert (t["gains"], t["losses"]) == (23150, -10000)
        assert t["tax_gains_only"] == int(23150 * M.TAX_RATE)
        assert t["tax_netted"] == int(13150 * M.TAX_RATE)
        assert [h.code for h in t["loss_rows"]] == ["2222"]
        # 配当: 1111 は 120000 × 2% = 2400、2222 は 90000 × 3% = 2700、130A は利回り無し
        assert t["div_total"] == 5100 and [h.code for h in t["div_missing"]] == ["130A"]

    def test_taxable_yutai_counts_all_accounts(self):
        rows = _report()["taxable"]["yutai_rows"]
        assert [(r["holding"].code, r["qty_here"], r["qty_all"]) for r in rows] == [("1111", 100.0, 200.0)]

    def test_nisa_flags_every_yutai_stock(self):
        n = _report()["nisa"]
        assert [r["holding"].code for r in n["flagged"]] == ["1111"]
        flag = n["flagged"][0]
        assert flag["reaches_min"] is True
        assert flag["yutai_yield"] == pytest.approx(2000 / (100 * 1200) * 100)
        assert flag["div_yield"] == 2.0
        assert [h.code for h in n["unconfirmed"]] == ["3333"]     # ETF の 4444 は確認から外す
        assert n["loss_total"] == -10000

    def test_nisa_without_db_keeps_unknown_kinds_in_the_check(self):
        n = _report(info={})["nisa"]
        assert [h.code for h in n["unconfirmed"]] == ["3333", "4444"]

    def test_nisa_lifetime_usage(self):
        n = _report()["nisa"]
        assert n["used_total"] == 90000 + 200000 + 20000 + 200000 + 420000
        assert n["used_growth"] == 90000 + 200000 + 20000 + 200000
        assert n["left_total"] == M.NISA_LIFETIME_LIMIT - n["used_total"]
        assert n["tax_free_gains"] == int((30000 + 5000 + 50000 + 80000) * M.TAX_RATE)

    def test_old_nisa_effects(self):
        o = _report()["old_nisa"]
        rows = {r["holding"].code: r for r in o["rows"]}
        assert rows["5555"]["gain"] and rows["5555"]["tax_effect"] == int(20000 * M.TAX_RATE)
        assert not rows["6666"]["gain"] and rows["6666"]["tax_effect"] == int(30000 * M.TAX_RATE)
        assert rows["6666"]["yutai"] == M.YUTAI_YES
        assert o["expiries"] == [date(2026, 12, 31), date(2027, 12, 31)]

    def test_taxable_without_statement_has_no_netting(self):
        assert _report()["taxable"]["realized"] is None

    def test_realized_netting_with_gain(self):
        # R = 8000、特定口座の含み損 L = -10000（2222）→ R + L = -2000 なので税の見込みは 0、徴収分 1625 が全部戻る
        r = _report(realized=_realized())["taxable"]["realized"]
        assert (r["pnl"], r["tax_withheld"], r["loss"], r["netted"]) == (8000, 1625, -10000, -2000)
        assert (r["tax_after"], r["refund"], r["room"], r["sells"]) == (0, 1625, 0, 3)

    def test_realized_netting_keeps_tax_on_the_rest(self):
        realized = M.Realized(start=date(2026, 1, 1), end=date(2026, 12, 31), rows=(), sale_pnl=50000,
                              sale_tax=10157, dividends=0, div_tax=0)
        r = _report(realized=realized)["taxable"]["realized"]
        assert r["netted"] == 40000 and r["tax_after"] == int(40000 * M.TAX_RATE)
        assert r["refund"] == 10157 - int(40000 * M.TAX_RATE)

    def test_realized_loss_leaves_room_for_gains(self):
        realized = M.Realized(start=date(2026, 1, 1), end=date(2026, 12, 31), rows=(), sale_pnl=-5000,
                              sale_tax=0, dividends=0, div_tax=0)
        r = _report(realized=realized)["taxable"]["realized"]
        assert (r["room"], r["tax_after"], r["refund"]) == (5000, 0, 0)

    def test_general_account_losses_are_not_netted(self):
        p = _parsed()
        moved = [M.Holding(**{**h.__dict__, "account": M.ACCOUNT_GENERAL}) if h.code == "2222" else h
                 for h in p.holdings]
        r = M.realized_netting(_realized(), moved)
        assert (r["loss"], r["netted"]) == (0, 8000)

    def test_old_nisa_expiry_from_the_table(self):
        o = _report(buy_years={"5555": 2022, "9999": 2023})["old_nisa"]
        assert [r["holding"].code for r in o["rows"]] == ["5555", "6666"]     # 終わる日の近い順・不明は後ろ
        first, second = o["rows"]
        assert (first["buy_year"], first["expiry"], first["days_left"]) == (2022, date(2026, 12, 31), 39)
        assert (second["buy_year"], second["expiry"], second["days_left"]) == (None, None, None)
        assert o["unknown"] == 1 and o["expiries"] == [date(2026, 12, 31), date(2027, 12, 31)]
        assert o["not_held"] == ["9999"] and o["years_given"] == 2

    def test_old_nisa_without_unknowns_lists_no_candidates(self):
        o = _report(buy_years={"5555": 2023, "6666": 2022})["old_nisa"]
        assert [r["holding"].code for r in o["rows"]] == ["6666", "5555"]
        assert o["unknown"] == 0 and o["expiries"] == []

    def test_old_nisa_past_expiry_is_negative(self):
        report = M.build_report(_parsed(), {}, {}, date(2027, 1, 5), db_used=False, source="s.csv",
                                buy_years={"5555": 2022})
        row = next(r for r in report["old_nisa"]["rows"] if r["holding"].code == "5555")
        assert row["days_left"] == -5
        assert "終了済み" in M.render_html(report)

    def test_cross_view_merges_the_same_security(self):
        c = _report()["cross"]
        multi = {g["key"]: g for g in c["multi"]}
        assert set(multi) == {"1111", FUND}
        assert multi[FUND]["value"] == 750000 and multi["1111"]["quantity"] == 200.0
        assert c["top"][0]["key"] == FUND
        assert c["top5_pct"] == pytest.approx((750000 + 240000 + 190000 + 120000 + 100000) / 1_548_150 * 100)


class TestOldNisaExpiry:
    def test_expiry_from_buy_year(self):
        assert M.old_nisa_expiry(2022) == date(2026, 12, 31)
        assert M.old_nisa_expiry(2014) == date(2018, 12, 31)
        with pytest.raises(ValueError):
            M.old_nisa_expiry(2024)

    @pytest.mark.parametrize("as_of, want", [
        (date(2026, 11, 22), [date(2026, 12, 31), date(2027, 12, 31)]),
        (date(2027, 1, 1), [date(2027, 12, 31)]),
        (date(2028, 1, 1), []),
    ])
    def test_possible_expiries(self, as_of, want):
        assert M.possible_old_nisa_expiries(as_of) == want


class TestRender:
    def test_html_escapes_names(self):
        evil = SAMPLE.replace("架空電機", "<script>x</script>")
        report = M.build_report(M.parse_holdings_csv(_bytes(evil)), {}, {}, AS_OF, db_used=False, source="s.csv")
        page = M.render_html(report)
        assert "<script>x</script>" not in page and "&lt;script&gt;" in page

    def test_summary_is_ascii_and_has_no_names(self):
        for report in (_report(), _report(realized=_realized(), buy_years={"5555": 2022})):
            lines = M.summary_lines(report)
            assert all(line.isascii() for line in lines)
            assert not any("架空" in line for line in lines)

    def test_summary_counts_expiries_and_realized(self):
        out = "\n".join(M.summary_lines(_report(realized=_realized(), buy_years={"5555": 2022})))
        assert "known=1 unknown=1 nearest_expiry=2026-12-31" in out
        assert "realized: sells=3 pnl=+8,000 tax_withheld=1,625" in out
        assert "realized: none" in "\n".join(M.summary_lines(_report()))

    def test_html_shows_netting_and_expiries(self):
        page = M.render_html(_report(realized=_realized(), buy_years={"5555": 2022}, realized_source="r.csv"))
        assert "今年の確定損益との損益通算" in page and "2026-12-31" in page and "39 日" in page
        assert "譲渡損益の明細: r.csv" in page
        loss = M.Realized(start=date(2026, 1, 1), end=date(2026, 12, 31), rows=(), sale_pnl=-5000,
                          sale_tax=0, dividends=0, div_tax=0)
        assert "あと 5,000 円確定するまでは税がかかりません" in M.render_html(_report(realized=loss))
        assert "--realized" in M.render_html(_report())


class TestCli:
    def _setup(self, tmp_path, monkeypatch):
        data = tmp_path / "portfolio"
        monkeypatch.setattr(M, "DEFAULT_DIR", data)
        raw = data / "raw"
        raw.mkdir(parents=True)
        csv_path = raw / "sample.csv"
        csv_path.write_bytes(_bytes())
        work = tmp_path / "work"
        work.mkdir()
        return data, csv_path, work

    def test_writes_report_without_db(self, tmp_path, monkeypatch, capsys):
        data, csv_path, work = self._setup(tmp_path, monkeypatch)
        (data / "yutai.csv").write_text(YUTAI, encoding="utf-8")
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--no-db", "--as-of", "2026-11-22"]) == 0
        page = (data / "reports" / "holdings_20261122.html").read_text(encoding="utf-8")
        assert "方針チェック" in page and "架空食品" in page and "なし（未取得）" in page
        out = capsys.readouterr().out
        assert "TOTAL" in out and "1,548,150" in out and "yutai=5" in out

    def test_refuses_inside_the_data_folder(self, tmp_path, monkeypatch):
        data, csv_path, _ = self._setup(tmp_path, monkeypatch)
        monkeypatch.chdir(data)
        assert M.main(["--csv", str(csv_path), "--no-db"]) == M.EXIT_NG

    def test_refuses_inside_the_csv_folder(self, tmp_path, monkeypatch):
        _, _, work = self._setup(tmp_path, monkeypatch)
        other = work / "dl.csv"
        other.write_bytes(_bytes())
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(other), "--no-db"]) == M.EXIT_NG

    def test_bad_csv_is_exit_ng(self, tmp_path, monkeypatch, capsys):
        _, csv_path, work = self._setup(tmp_path, monkeypatch)
        csv_path.write_bytes(_bytes(SAMPLE.replace("243150,+13150", "1,+1")))
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--no-db"]) == M.EXIT_NG
        assert "合計の行" in capsys.readouterr().err

    def test_missing_explicit_yutai_is_exit_ng(self, tmp_path, monkeypatch):
        _, csv_path, work = self._setup(tmp_path, monkeypatch)
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--no-db", "--yutai", str(work / "none.csv")]) == M.EXIT_NG

    def test_statement_and_years(self, tmp_path, monkeypatch, capsys):
        data, csv_path, work = self._setup(tmp_path, monkeypatch)
        realized = data / "raw" / "realized.csv"
        realized.write_bytes(REALIZED.encode("cp932"))
        years = tmp_path / "years.csv"
        years.write_text(OLD_NISA_YEARS, encoding="utf-8")
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--realized", str(realized), "--old-nisa-years", str(years),
                       "--no-db", "--as-of", "2026-11-22"]) == 0
        page = (data / "reports" / "holdings_20261122.html").read_text(encoding="utf-8")
        assert "今年の確定損益との損益通算" in page and "2026-12-31" in page
        out = capsys.readouterr().out
        assert "realized: sells=3" in out and "known=1 unknown=1" in out

    def test_default_years_table_is_read(self, tmp_path, monkeypatch, capsys):
        data, csv_path, work = self._setup(tmp_path, monkeypatch)
        (data / "old_nisa_years.csv").write_text("code,buy_year\n5555,2022\n6666,2023\n", encoding="utf-8")
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--no-db", "--as-of", "2026-11-22"]) == 0
        assert "known=2 unknown=0" in capsys.readouterr().out

    @pytest.mark.parametrize("args, msg", [
        (["--as-of", "2027-01-10"], "1 月 1 日"),                  # 去年の明細を今年の数字にしない
        (["--as-of", "2026-11-22", "--old-nisa-years", "none.csv"], "none.csv"),
    ])
    def test_bad_statement_or_years_is_exit_ng(self, tmp_path, monkeypatch, capsys, args, msg):
        data, csv_path, work = self._setup(tmp_path, monkeypatch)
        realized = data / "raw" / "realized.csv"
        realized.write_bytes(REALIZED.encode("cp932"))
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--realized", str(realized), "--no-db", *args]) == M.EXIT_NG
        assert msg in capsys.readouterr().err

    def test_refuses_inside_the_statement_folder(self, tmp_path, monkeypatch):
        _, csv_path, work = self._setup(tmp_path, monkeypatch)
        realized = work / "realized.csv"
        realized.write_bytes(REALIZED.encode("cp932"))
        monkeypatch.chdir(work)
        assert M.main(["--csv", str(csv_path), "--realized", str(realized), "--no-db"]) == M.EXIT_NG


def test_runs_under_isolated_mode():
    """`python -I`（カレントフォルダを sys.path に入れない）でも portfolio_ledger を import できる。"""
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "holdings_report.py"), "--help"],
                          capture_output=True, cwd=str(ROOT.parent), timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
