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


def _report(info=None, yutai=None):
    return M.build_report(_parsed(), _info() if info is None else info,
                          M.parse_yutai_csv(YUTAI) if yutai is None else yutai, AS_OF,
                          db_used=info is None, source="sample.csv")


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
        lines = M.summary_lines(_report())
        assert all(line.isascii() for line in lines)
        assert not any("架空" in line for line in lines)


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


def test_runs_under_isolated_mode():
    """`python -I`（カレントフォルダを sys.path に入れない）でも portfolio_ledger を import できる。"""
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "holdings_report.py"), "--help"],
                          capture_output=True, cwd=str(ROOT.parent), timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
