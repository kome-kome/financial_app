"""update_industry_from_jpx のユニットテスト (#78)。

HTTP 呼び出しをモックし、Company/FinancialRecord の業種が
バルク UPDATE で正しく更新されることを検証する。
"""
import asyncio
import io
import os
import sys

import httpx
import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import _read_jpx_excel, resolve_jpx_excel_url, update_industry_from_jpx
from collector_utils import JPX_EXCEL_URL, JPX_LISTING_URL, JpxIndustryError
from database import KEY_JPX_INDUSTRY_LAST_SUCCESS, get_setting

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
