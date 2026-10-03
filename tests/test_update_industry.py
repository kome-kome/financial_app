"""update_industry_from_jpx / fill_industry_from_edinet_codelist のユニットテスト (#78・#784)。

HTTP 呼び出しをモックし、Company/FinancialRecord の業種が
バルク UPDATE で正しく更新されることを検証する。
"""
import asyncio
import io
import logging
import os
import sys
import zipfile

import httpx
import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import (_read_edinet_codelist, _read_jpx_excel, fill_industry_from_edinet_codelist,
                       resolve_jpx_excel_url, update_industry_from_jpx)
from collector_utils import (EDINET_CODELIST_URL, JPX_EXCEL_URL, JPX_LISTING_URL,
                             EdinetCodelistError, JpxIndustryError)
from database import (KEY_EDINET_CODELIST_LAST_SUCCESS, KEY_JPX_INDUSTRY_LAST_SUCCESS, Company,
                      FinancialRecord, get_setting)

# 一覧ページの検体。**実物から写す**（`href` の形が違えば解決は静かに既定値へ倒れる）。
# 2026-09-08 実測: リンクは相対パスで、拡張子は `.xlsx`。
LISTING_HTML = (
    '<html><body><a href="/markets/statistics-equities/misc/'
    'tvdivq0000001vg2-att/data_j.xlsx">その他統計資料</a></body></html>'
)
RESOLVED_URL = ("https://www.jpx.co.jp/markets/statistics-equities/misc/"
                "tvdivq0000001vg2-att/data_j.xlsx")


def _make_jpx_xlsx(rows: list) -> bytes:
    """テスト用 JPX 業種マスタ xlsx を生成（col1=sec_code, col5=industry）"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["col0", "sec_code", "col2", "col3", "col4", "industry_name"])
    for sec, ind in rows:
        ws.append(["", sec, "", "", "", ind])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _mock_client(content: bytes, listing: str = LISTING_HTML) -> httpx.AsyncClient:
    """一覧ページと Excel を**URL で出し分ける**モック。

    1つの応答を全リクエストに返すと、URL 解決の経路が素通りして検証にならない。
    """
    def handler(request):
        if str(request.url) == JPX_LISTING_URL:
            return httpx.Response(200, text=listing)
        return httpx.Response(200, content=content)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _FakeXlsSheet:
    """xlrd の sheet インターフェース（cell_value / nrows）を最小再現するスタブ。"""
    def __init__(self, rows):
        self._rows = rows
    @property
    def nrows(self):
        return len(self._rows)
    def cell_value(self, row, col):
        return self._rows[row][col]   # 列不足は IndexError（実 xlrd と同挙動）


class _FakeXlsBook:
    def __init__(self, rows):
        self._sheet = _FakeXlsSheet(rows)
    def sheet_by_index(self, idx):
        return self._sheet


class TestReadJpxExcel:
    """Excel バイト列 → 業種辞書の純粋変換 `_read_jpx_excel` の単体テスト。"""

    def test_parses_real_xlsx(self):
        """.xlsx 形式（xlrd が XLRDError → openpyxl フォールバック経路）を実データで検証。"""
        content = _make_jpx_xlsx([("1001", "情報・通信業"), ("2001", "小売業")])
        result = _read_jpx_excel(content)
        assert result == {"1001": "情報・通信業", "2001": "小売業"}

    def test_xlsx_skips_blank_industry_rows(self):
        """業種が空（'-'/None）の行はスキップされる。"""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["col0", "sec_code", "c2", "c3", "c4", "industry"])
        ws.append(["", "2001", "", "", "", "小売業"])   # 採用
        ws.append(["", "3001", "", "", "", "-"])        # 業種 '-' → スキップ
        ws.append(["", "4001", "", "", "", None])       # 業種 None → スキップ
        buf = io.BytesIO(); wb.save(buf)
        result = _read_jpx_excel(buf.getvalue())
        assert result == {"2001": "小売業"}

    def test_empty_sheet_returns_empty_dict(self):
        """ヘッダのみ（データ行なし）の空シートは空辞書を返す。"""
        wb = openpyxl.Workbook()
        wb.active.append(["col0", "sec_code", "c2", "c3", "c4", "industry"])
        buf = io.BytesIO(); wb.save(buf)
        assert _read_jpx_excel(buf.getvalue()) == {}

    def test_missing_required_columns_skipped(self):
        """業種列（6列目）を欠く行は IndexError をスキップして空辞書になる。"""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["col0", "sec_code"])      # 2列しかない
        ws.append(["", "1001"])              # 業種列なし → スキップ
        buf = io.BytesIO(); wb.save(buf)
        assert _read_jpx_excel(buf.getvalue()) == {}

    def test_parses_xls_via_xlrd(self, monkeypatch):
        """.xls 形式（xlrd 成功経路）を、xlrd.open_workbook をスタブ化して検証。"""
        rows = [
            ["col0", "sec_code", "c2", "c3", "c4", "industry"],  # ヘッダ
            ["", 1001.0, "", "", "", "情報・通信業"],            # xls は数値が float
            ["", 101.0, "", "", "", "建設業"],                   # float → 4桁ゼロ埋め "0101"
            ["", "9999", "", "", "", "サービス業"],
        ]
        import xlrd
        monkeypatch.setattr(xlrd, "open_workbook",
                            lambda *a, **k: _FakeXlsBook(rows))
        result = _read_jpx_excel(b"dummy-xls-bytes")
        assert result == {"1001": "情報・通信業", "0101": "建設業", "9999": "サービス業"}


class TestUpdateIndustryFromJpx:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_updates_company_industry(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=""))
        db.commit()
        content = _make_jpx_xlsx([("1001", "情報・通信業")])
        client = _mock_client(content)
        co_updated, fr_updated = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 1
        from database import Company
        co = db.query(Company).filter_by(edinet_code="E00001").first()
        assert co.industry == "情報・通信業"

    def test_updates_financial_record_industry(self, db, make_company, make_fin):
        db.add(make_company(edinet_code="E00001", sec_code="2001"))
        db.add(make_fin(edinet_code="E00001", sec_code="2001", industry="旧業種"))
        db.commit()
        content = _make_jpx_xlsx([("2001", "小売業")])
        client = _mock_client(content)
        _, fr_updated = self._run(update_industry_from_jpx(client, db))
        assert fr_updated == 1
        from database import FinancialRecord
        fr = db.query(FinancialRecord).filter_by(edinet_code="E00001").first()
        assert fr.industry == "小売業"

    def test_no_change_when_industry_already_correct(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1001"))
        db.query(type(make_company())).filter_by(edinet_code="E00001").update({"industry": "情報・通信業"})
        db.commit()
        content = _make_jpx_xlsx([("1001", "情報・通信業")])
        client = _mock_client(content)
        co_updated, _ = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 0

    def test_zero_padded_sec_code_matches(self, db, make_company):
        # DB に "0101"、JPX マップは "0101" → ゼロ埋め形式での一致
        db.add(make_company(edinet_code="E00001", sec_code="0101"))
        db.commit()
        content = _make_jpx_xlsx([("0101", "建設業")])
        client = _mock_client(content)
        co_updated, _ = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 1

    def test_http_error_raises(self, db):
        """**握って (0,0) を返さない**（#632）。「変化が無かった」と区別できなくなる。"""
        def handler(request):
            return httpx.Response(500)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(JpxIndustryError):
            self._run(update_industry_from_jpx(client, db))


class TestOpenpyxlReturnsInts:
    """xlsx を読む openpyxl は数値セルを **`int`** で返す（#632）。

    旧実装は `float` / `str` しか受けておらず、JPX が `.xls` から `.xlsx` へ切り替えた
    2026-09-03 以降、業種を持つ 3,899行のうち 3,606行を黙って捨てて 293件を返していた。
    **検体は実出力に合わせる**——コードを文字列で書いた検体は、この欠落を検出できない。
    """

    def test_int_codes_are_accepted(self):
        content = _make_jpx_xlsx([(1301, "水産・農林業"), (101, "建設業")])
        assert _read_jpx_excel(content) == {"1301": "水産・農林業", "0101": "建設業"}

    def test_int_and_alphanumeric_codes_coexist(self):
        """英字混じりの新形式（`130A`）は文字列で返る。旧実装が拾えていたのはこれだけ。"""
        content = _make_jpx_xlsx([(1301, "水産・農林業"), ("130A", "医薬品")])
        assert _read_jpx_excel(content) == {"1301": "水産・農林業", "130A": "医薬品"}

    def test_booleans_are_not_codes(self):
        """`bool` は `int` の派生。True が "0001" に化けないこと。"""
        content = _make_jpx_xlsx([(1301, "水産・農林業")] + [(True, "小売業")] * 30)
        with pytest.raises(JpxIndustryError):
            _read_jpx_excel(content)


class TestUnreadableCodeColumnIsAFailure:
    """業種はあるのにコードを解釈できない行が多い＝読み手か列構成の異常（#632）。

    件数が減るだけでは失敗として現れないので、取りこぼし率で止める。
    """

    def test_mostly_unreadable_raises(self):
        rows = [(1301, "水産・農林業")] + [(None, "小売業")] * 30
        with pytest.raises(JpxIndustryError) as ei:
            _read_jpx_excel(_make_jpx_xlsx(rows))
        assert "30/31" in str(ei.value)

    def test_a_few_unreadable_rows_are_tolerated(self):
        """脚注・小計のような端数行では鳴らない（正常なファイルでは 0 行）。"""
        rows = [(1300 + i, "水産・農林業") for i in range(99)] + [(None, "小売業")]
        result = _read_jpx_excel(_make_jpx_xlsx(rows))
        assert len(result) == 99

    def test_an_all_blank_industry_sheet_is_not_a_drop(self):
        """業種列が全部 '-'（ETF だけの断面）は候補0件＝割り算をしない。"""
        assert _read_jpx_excel(_make_jpx_xlsx([(1305, "-"), (1306, "-")])) == {}


class TestResolveJpxExcelUrl:
    """URL は一覧ページから解決する。定数で持つと変わった晩から静かに 404 になる（#632）。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_resolves_from_the_listing_page(self):
        client = _mock_client(b"", listing=LISTING_HTML)
        assert self._run(resolve_jpx_excel_url(client)) == RESOLVED_URL

    def test_follows_a_changed_filename(self):
        """次に `.xls` へ戻されても、ハッシュが変わっても追随する。"""
        listing = ('<a href="/markets/statistics-equities/misc/'
                   'newhash0000000000-att/data_j.xls">一覧</a>')
        client = _mock_client(b"", listing=listing)
        assert self._run(resolve_jpx_excel_url(client)).endswith(
            "/newhash0000000000-att/data_j.xls")

    def test_falls_back_when_the_page_is_gone(self):
        def handler(request):
            return httpx.Response(404)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert self._run(resolve_jpx_excel_url(client)) == JPX_EXCEL_URL

    def test_falls_back_when_the_link_is_missing(self):
        client = _mock_client(b"", listing="<html><body>リンクなし</body></html>")
        assert self._run(resolve_jpx_excel_url(client)) == JPX_EXCEL_URL


class TestSuccessLeavesAFootprint:
    """成功の足跡が `batch_freshness.PRODUCERS` の見る唯一の証拠（#632）。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_footprint_is_written_on_success(self, db, make_company):
        db.add(make_company(edinet_code="E00001", sec_code="1301", industry=""))
        db.commit()
        client = _mock_client(_make_jpx_xlsx([(1301, "水産・農林業")]))
        self._run(update_industry_from_jpx(client, db))
        assert get_setting(db, KEY_JPX_INDUSTRY_LAST_SUCCESS)

    def test_footprint_is_written_even_when_nothing_changed(self, db):
        """更新0件は「変化が無かった」であって失敗ではない。"""
        client = _mock_client(_make_jpx_xlsx([(1301, "水産・農林業")]))
        co_updated, _ = self._run(update_industry_from_jpx(client, db))
        assert co_updated == 0
        assert get_setting(db, KEY_JPX_INDUSTRY_LAST_SUCCESS)

    def test_no_footprint_when_the_fetch_fails(self, db):
        def handler(request):
            return httpx.Response(404)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(JpxIndustryError):
            self._run(update_industry_from_jpx(client, db))
        assert get_setting(db, KEY_JPX_INDUSTRY_LAST_SUCCESS) is None


class TestJpxUpdatesNullIndustry:
    """`industry != ind` は NULL の行を更新しない（SQL の三値論理・#784）。"""

    def test_null_company_and_record_are_updated(self, db, make_company, make_fin):
        db.add(make_company(edinet_code="E00001", sec_code="1001", industry=None))
        db.add(make_fin(edinet_code="E00001", sec_code="1001", industry=None))
        db.commit()
        client = _mock_client(_make_jpx_xlsx([("1001", "情報・通信業")]))
        co_updated, fr_updated = asyncio.run(update_industry_from_jpx(client, db))
        assert (co_updated, fr_updated) == (1, 1)
        assert db.query(Company).one().industry == "情報・通信業"
        assert db.query(FinancialRecord).one().industry == "情報・通信業"


# ── EDINET コードリスト（#784）────────────────────────────────────────────────
# 検体は **2026-10-02 版の実ファイルから写した行**（`EdinetcodeDlInfo.csv`・cp932・CRLF）。
# 1行目はメタ行、2行目が見出し、データ行は全項目を引用符で囲む。推測で書いた検体は、
# 本物を読めないことを検出できない。
_CODELIST_META = "ダウンロード実行日,2026年10月02日現在,件数,11402件"
_CODELIST_HEADER = ("ＥＤＩＮＥＴコード,提出者種別,上場区分,連結の有無,資本金,決算日,提出者名,"
                    "提出者名（英字）,提出者名（ヨミ）,所在地,提出者業種,証券コード,提出者法人番号")
_CODELIST_ROWS = [
    # 福証の単独上場（JPX に載らない＝#784 の本題）
    ["E02813", "内国法人・組合", "上場", "有", "1690", "3月31日", "株式会社Ｍｉｓｕｍｉ",
     "MISUMI CO., LTD.", "カブシキガイシャミスミ", "鹿児島市卸本町７番地２０", "卸売業", "74410",
     "4340001004160"],
    # 東証上場だが JPX の一覧で業種が付かない（優先出資証券）
    ["E03729", "内国法人・組合", "上場", "有", "890998", "3月31日", "信金中央金庫",
     "Shinkin Central Bank", "シンキンチュウオウキンコ", "中央区八重洲一丁目３番７号", "その他金融業",
     "84210", "3010005002392"],
    # 表記差: EDINET は「倉庫・運輸関連」、JPX は「倉庫・運輸関連業」
    ["E04369", "内国法人・組合", "上場", "有", "500", "2月末日", "株式会社エーアイテイー",
     "AIT CORPORATION", "カブシキガイシャエーアイティー", "大阪市中央区本町二丁目１番６号",
     "倉庫・運輸関連", "93810", "3120001075217"],
    # 非上場（33業種名なので埋める・#797）
    ["E00033", "内国法人・組合", "非上場", "有", "2141", "3月31日", "常磐興産株式会社",
     "Joban Kosan Co.,Ltd.", "ジョウバンコウサンカブシキガイシャ", "いわき市常磐藤原町蕨平５０番地",
     "サービス業", "", "9380001014473"],
    # 上場区分が空欄（提出義務者以外）
    ["E00050", "内国法人・組合（有価証券報告書等の提出義務者以外）", "", "", "10000", "",
     "日本コムシス株式会社", "", "ニッポンコムシスカブシキガイシャ", "品川区東五反田２丁目１７番１号",
     "内国法人・組合（有価証券報告書等の提出義務者以外）", "", "4010701022825"],
]


def _make_codelist_zip(rows=_CODELIST_ROWS, header=_CODELIST_HEADER, meta=_CODELIST_META) -> bytes:
    lines = [meta, header] + [",".join(f'"{v}"' for v in r) for r in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EdinetcodeDlInfo.csv", ("\r\n".join(lines) + "\r\n").encode("cp932"))
    return buf.getvalue()


def _codelist_client(content: bytes = None, status: int = 200) -> httpx.AsyncClient:
    """コードリストの URL だけに応答する。別の URL を叩いたら 599 で落ちる（素通りを検出する）。"""
    content = _make_codelist_zip() if content is None else content
    def handler(request):
        if str(request.url) == EDINET_CODELIST_URL:
            return httpx.Response(status, content=content)
        return httpx.Response(599)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestReadEdinetCodelist:
    def test_returns_all_rows_with_the_listed_flag(self):
        """上場区分によらず全行を返す（#797）。業種名は生のまま（正規化・照合は呼び出し側）。"""
        assert _read_edinet_codelist(_make_codelist_zip()) == {
            "E02813": ("卸売業", True), "E03729": ("その他金融業", True),
            "E04369": ("倉庫・運輸関連", True), "E00033": ("サービス業", False),
            "E00050": ("内国法人・組合（有価証券報告書等の提出義務者以外）", False)}

    def test_columns_are_found_by_name(self):
        """列の位置ではなく見出しの名前で引く（列が足されてもずれない）。"""
        header = "追加列," + _CODELIST_HEADER
        rows = [["x"] + r for r in _CODELIST_ROWS[:1]]
        assert _read_edinet_codelist(_make_codelist_zip(rows, header)) == {"E02813": ("卸売業", True)}

    def test_missing_header_column_raises(self):
        header = _CODELIST_HEADER.replace("提出者業種", "業種")
        with pytest.raises(EdinetCodelistError, match="提出者業種"):
            _read_edinet_codelist(_make_codelist_zip(header=header))

    def test_no_listed_rows_raises(self):
        """件数が減ったまま通さない（#632 と同じ考え方）。"""
        with pytest.raises(EdinetCodelistError, match="上場"):
            _read_edinet_codelist(_make_codelist_zip(rows=_CODELIST_ROWS[3:]))

    def test_not_a_zip_raises(self):
        with pytest.raises(EdinetCodelistError):
            _read_edinet_codelist(b"<html>maintenance</html>")


class TestFillIndustryFromEdinetCodelist:
    """空欄だけを埋め、JPX を上書きせず、許す名前は JPX が書いた名前だけ（#784）。"""

    def _seed_jpx_names(self, db, make_company):
        # 照合先＝JPX が書いた業種名（DB に既にある名前）
        for i, ind in enumerate(["卸売業", "その他金融業", "倉庫・運輸関連業", "サービス業"]):
            db.add(make_company(edinet_code=f"E9000{i}", sec_code=f"900{i}", industry=ind))

    def _industry(self, db, code):
        return db.query(Company).filter_by(edinet_code=code).one().industry

    def test_fills_empty_and_null_listed_companies(self, db, make_company):
        self._seed_jpx_names(db, make_company)
        db.add(make_company(edinet_code="E02813", sec_code="7441", industry=""))
        db.add(make_company(edinet_code="E03729", sec_code="8421", industry=None))
        db.commit()
        filled_co, _ = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_co == 2
        assert self._industry(db, "E02813") == "卸売業"
        assert self._industry(db, "E03729") == "その他金融業"

    def test_alias_maps_to_the_jpx_name(self, db, make_company):
        self._seed_jpx_names(db, make_company)
        db.add(make_company(edinet_code="E04369", sec_code="9381", industry=""))
        db.commit()
        asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert self._industry(db, "E04369") == "倉庫・運輸関連業"

    def test_does_not_overwrite_jpx(self, db, make_company):
        """JPX の分類は EDINET と 4.6% 食い違う（実測）。正本は JPX のまま。"""
        self._seed_jpx_names(db, make_company)
        db.add(make_company(edinet_code="E02813", sec_code="7441", industry="サービス業"))
        db.commit()
        filled_co, _ = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_co == 0
        assert self._industry(db, "E02813") == "サービス業"

    def test_non_listed_companies_are_filled(self, db, make_company):
        """非上場の社（大半は廃止社）も33業種名なら埋める（#797・ADR-0065）。空のままだと過去の
        断面で「後に廃止した社」という未来情報の疑似業種になる。"""
        self._seed_jpx_names(db, make_company)
        db.add(make_company(edinet_code="E00033", sec_code="9675", industry="", is_active=False))
        db.commit()
        filled_co, _ = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_co == 1
        assert self._industry(db, "E00033") == "サービス業"

    def test_out_of_scope_names_of_non_listed_are_not_written_nor_warned(self, db, make_company,
                                                                         caplog):
        """33業種外（提出者の種別）は書かない。想定内なので WARNING にしない（毎晩70件が鳴る）。"""
        self._seed_jpx_names(db, make_company)
        db.add(make_company(edinet_code="E00050", sec_code=None, industry=""))
        db.commit()
        with caplog.at_level(logging.INFO):
            filled_co, _ = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_co == 0
        assert self._industry(db, "E00050") == ""
        assert not [r for r in caplog.records
                    if r.levelno >= logging.WARNING and "提出義務者以外" in r.getMessage()]
        assert any("提出義務者以外" in r.getMessage() for r in caplog.records
                   if r.levelno == logging.INFO)

    def test_name_unknown_to_jpx_is_not_written(self, db, make_company, caplog):
        """JPX の名前に無い提出者業種を書くと、業種別回帰に1社だけの業種ができる。"""
        db.add(make_company(edinet_code="E90000", sec_code="9000", industry="卸売業"))
        db.add(make_company(edinet_code="E03729", sec_code="8421", industry=""))
        db.commit()
        with caplog.at_level(logging.WARNING):
            filled_co, _ = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_co == 0
        assert self._industry(db, "E03729") == ""
        assert "その他金融業" in caplog.text

    def test_no_jpx_names_raises(self, db, make_company):
        """照合先が空＝JPX が一度も成功していない。黙って0件にしない。"""
        db.add(make_company(edinet_code="E02813", sec_code="7441", industry=""))
        db.commit()
        with pytest.raises(EdinetCodelistError, match="照合先"):
            asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert get_setting(db, KEY_EDINET_CODELIST_LAST_SUCCESS) is None


class TestPropagateToFinancialRecords:
    """収集は財務行の業種を空で書き直すので、会社の業種を毎晩写す（#784）。"""

    def test_empty_and_null_rows_are_filled_and_filled_rows_kept(self, db, make_company, make_fin):
        db.add(make_company(edinet_code="E90000", sec_code="9000", industry="卸売業"))
        db.add(make_company(edinet_code="E02813", sec_code="7441", industry=""))
        db.add(make_fin(edinet_code="E02813", sec_code="7441", year=2024, industry=""))
        db.add(make_fin(edinet_code="E02813", sec_code="7441", year=2025, industry=None))
        db.add(make_fin(edinet_code="E02813", sec_code="7441", year=2026, period_type="H1",
                        industry="小売業"))
        db.commit()
        _, filled_fr = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_fr == 2
        got = {(r.year, r.period_type): r.industry
               for r in db.query(FinancialRecord).filter_by(edinet_code="E02813")}
        assert got == {(2024, "annual"): "卸売業", (2025, "annual"): "卸売業",
                       (2026, "H1"): "小売業"}

    def test_company_filled_earlier_is_propagated(self, db, make_company, make_fin):
        """JPX から外れた社（TOB 等）の、後から空で作られた H1 行も直る。"""
        db.add(make_company(edinet_code="E99999", sec_code="8283", industry="卸売業"))
        db.add(make_fin(edinet_code="E99999", sec_code="8283", period_type="H1", industry=""))
        db.commit()
        _, filled_fr = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_fr == 1
        assert db.query(FinancialRecord).one().industry == "卸売業"

    def test_rows_of_companies_without_industry_stay_empty(self, db, make_company, make_fin):
        db.add(make_company(edinet_code="E90000", sec_code="9000", industry="卸売業"))
        db.add(make_company(edinet_code="E00033", sec_code="9675", industry=""))
        db.add(make_fin(edinet_code="E00033", sec_code="9675", industry=""))
        db.commit()
        _, filled_fr = asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db))
        assert filled_fr == 0
        assert db.query(FinancialRecord).one().industry == ""


class TestEdinetCodelistFootprint:
    """足跡が `batch_freshness.PRODUCERS` の見る唯一の証拠（#784・#632 と同じ作法）。"""

    def test_footprint_is_written_even_when_nothing_was_filled(self, db, make_company):
        db.add(make_company(edinet_code="E90000", sec_code="9000", industry="卸売業"))
        db.commit()
        assert asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(), db)) == (0, 0)
        assert get_setting(db, KEY_EDINET_CODELIST_LAST_SUCCESS)

    def test_no_footprint_when_the_fetch_fails(self, db, make_company):
        """**握って (0,0) を返さない**。「埋める社が無かった」と区別できなくなる。"""
        db.add(make_company(edinet_code="E90000", sec_code="9000", industry="卸売業"))
        db.commit()
        with pytest.raises(EdinetCodelistError):
            asyncio.run(fill_industry_from_edinet_codelist(_codelist_client(status=404), db))
        assert get_setting(db, KEY_EDINET_CODELIST_LAST_SUCCESS) is None
