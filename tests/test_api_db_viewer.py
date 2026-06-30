"""DB ビューア系エンドポイント（routers/market.py）の最小テスト。

`{table}` がクエリに渡る経路（schema/preview/stats/export）を最優先で押さえ、
許可外テーブル名で 404 を返すこと（情報漏えい・SQLi 回帰の検出）を検証する。
正常系は conftest の TestClient / fixture DB・ファクトリを再利用して 200＋期待スキーマを確認。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
from routers.market import _csv_safe  # noqa: E402

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _override_db(db):
    """conftest の in-memory Session を api.get_db に注入し、後始末で override をクリア。"""
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield
    api.app.dependency_overrides.clear()


# ── /api/db/tables ──────────────────────────────────────────────────────────

class TestDbTables:
    def test_returns_table_list(self):
        r = client.get("/api/db/tables")
        assert r.status_code == 200
        body = r.json()
        names = {t["name"] for t in body["tables"]}
        assert {"companies", "financial_records", "macro_data"} <= names
        for t in body["tables"]:
            assert {"name", "row_count", "column_count"} <= t.keys()


# ── /api/db/schema/{table} ──────────────────────────────────────────────────

class TestDbSchema:
    def test_valid_table_returns_columns(self, db, make_company):
        db.add(make_company()); db.commit()
        r = client.get("/api/db/schema/companies")
        assert r.status_code == 200
        body = r.json()
        assert body["table"] == "companies"
        assert any(c["name"] == "edinet_code" for c in body["columns"])

    def test_unknown_table_returns_404(self):
        r = client.get("/api/db/schema/secret_table")
        assert r.status_code == 404

    def test_sql_injection_attempt_returns_404(self):
        r = client.get("/api/db/schema/companies;DROP TABLE companies")
        assert r.status_code == 404


# ── /api/db/preview/{table} ─────────────────────────────────────────────────

class TestDbPreview:
    def test_valid_table_returns_rows(self, db, make_company):
        db.add(make_company()); db.commit()
        r = client.get("/api/db/preview/companies")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["edinet_code"] == "E00001"

    def test_unknown_table_returns_404(self):
        assert client.get("/api/db/preview/users").status_code == 404

    def test_invalid_limit_returns_400(self):
        assert client.get("/api/db/preview/companies?limit=0").status_code == 400

    def test_invalid_order_returns_400(self):
        assert client.get("/api/db/preview/companies?order=sideways").status_code == 400

    def test_invalid_sort_column_returns_400(self):
        assert client.get("/api/db/preview/companies?sort=nonexistent").status_code == 400

    def test_numeric_col_with_non_numeric_filter_returns_400(self):
        r = client.get("/api/db/preview/financial_records?filter_col=year&filter_val=abc")
        assert r.status_code == 400

    def test_numeric_col_with_valid_filter_returns_200(self, db, make_fin):
        db.add(make_fin(year=2023)); db.commit()
        r = client.get("/api/db/preview/financial_records?filter_col=year&filter_val=2023")
        assert r.status_code == 200


# ── /api/db/stats/{table} ───────────────────────────────────────────────────

class TestDbStats:
    def test_valid_table_returns_stats(self, db, make_fin):
        db.add(make_fin(pl_revenue=1000.0)); db.commit()
        r = client.get("/api/db/stats/financial_records")
        assert r.status_code == 200
        body = r.json()
        assert body["row_count"] == 1
        assert any(s["name"] == "pl_revenue" for s in body["stats"])

    def test_empty_table_returns_200(self):
        r = client.get("/api/db/stats/macro_data")
        assert r.status_code == 200
        assert r.json()["row_count"] == 0

    def test_unknown_table_returns_404(self):
        assert client.get("/api/db/stats/__proc").status_code == 404


# ── /api/db/relations ───────────────────────────────────────────────────────

class TestDbRelations:
    def test_returns_tables_and_relations(self):
        r = client.get("/api/db/relations")
        assert r.status_code == 200
        body = r.json()
        assert body["tables"] and body["relations"]
        rel = body["relations"][0]
        assert {"from_table", "from_column", "to_table", "to_column"} <= rel.keys()


# ── /api/db/company/{edinet_code} ───────────────────────────────────────────

class TestDbCompanyDrilldown:
    def test_valid_company_returns_records(self, db, make_company, make_fin):
        db.add(make_company()); db.add(make_fin(year=2023)); db.commit()
        r = client.get("/api/db/company/E00001")
        assert r.status_code == 200
        body = r.json()
        assert body["company"]["edinet_code"] == "E00001"
        assert len(body["financial_records"]) == 1

    def test_malformed_code_returns_400(self):
        assert client.get("/api/db/company/BAD").status_code == 400

    def test_unknown_code_returns_404(self):
        assert client.get("/api/db/company/E99999").status_code == 404


# ── _csv_safe ────────────────────────────────────────────────────────────────

class TestCsvSafe:
    @pytest.mark.parametrize("raw,expected", [
        ("=SUM(A1)", "'=SUM(A1)"),
        ("+1234",   "'+1234"),
        ("-1",      "'-1"),
        ("@foo",    "'@foo"),
        ("\t注入",  "'\t注入"),
        ("\r注入",  "'\r注入"),
        ("普通のテキスト", "普通のテキスト"),
        ("",        ""),
        (42,        42),
        (3.14,      3.14),
        (None,      None),
    ])
    def test_sanitizes_dangerous_prefix(self, raw, expected):
        assert _csv_safe(raw) == expected


# ── /api/db/export/{table} ──────────────────────────────────────────────────

class TestDbExport:
    def test_valid_table_returns_csv(self, db, make_company):
        db.add(make_company()); db.commit()
        r = client.get("/api/db/export/companies")
        assert r.status_code == 200
        assert "edinet_code" in r.text
        assert "E00001" in r.text

    def test_formula_prefix_is_escaped(self, db, make_company):
        db.add(make_company(name="=HYPERLINK(\"evil.com\",\"click\")")); db.commit()
        r = client.get("/api/db/export/companies")
        assert r.status_code == 200
        assert "'=HYPERLINK" in r.text

    def test_unknown_table_returns_404(self):
        assert client.get("/api/db/export/pg_user").status_code == 404

    def test_invalid_limit_zero_returns_400(self):
        assert client.get("/api/db/export/companies?limit=0").status_code == 400

    def test_invalid_limit_over_max_returns_400(self):
        assert client.get("/api/db/export/companies?limit=10001").status_code == 400

    def test_numeric_col_with_non_numeric_filter_returns_400(self):
        r = client.get("/api/db/export/financial_records?filter_col=year&filter_val=abc")
        assert r.status_code == 400


# ── /api/export/csv ─────────────────────────────────────────────────────────

class TestExportCsv:
    def test_returns_csv_with_header(self, db, make_metric):
        db.add(make_metric(pl_revenue=1000.0)); db.commit()
        r = client.get("/api/export/csv")
        assert r.status_code == 200
        assert "証券コード" in r.text

    def test_company_name_formula_is_escaped(self, db, make_metric):
        db.add(make_metric(company_name="=IMPORTDATA(\"http://evil.example\")")); db.commit()
        r = client.get("/api/export/csv")
        assert r.status_code == 200
        assert "'=IMPORTDATA" in r.text
