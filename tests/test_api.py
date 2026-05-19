"""FastAPI エンドポイントの統合テスト（SQLite in-memory）"""
import pytest

from tests.conftest import make_company, make_record


# ─── 静的ページ ────────────────────────────────────────────────────────

class TestStaticPages:
    def test_dashboard(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_collection_page(self, client):
        assert client.get("/collection").status_code == 200

    def test_analysis_page(self, client):
        assert client.get("/analysis").status_code == 200

    def test_login_page(self, client):
        assert client.get("/login").status_code == 200

    def test_models_page(self, client):
        assert client.get("/models").status_code == 200


# ─── セキュリティヘッダー ──────────────────────────────────────────────

class TestSecurityHeaders:
    def test_csp_present(self, client):
        r = client.get("/")
        assert "Content-Security-Policy" in r.headers
        assert "frame-ancestors" in r.headers["Content-Security-Policy"]

    def test_x_frame_options(self, client):
        r = client.get("/")
        assert r.headers.get("X-Frame-Options") == "DENY"

    def test_referrer_policy(self, client):
        r = client.get("/")
        assert "Referrer-Policy" in r.headers


# ─── 認証エンドポイント ───────────────────────────────────────────────

class TestAuthAPI:
    def test_auth_status_no_password(self, client):
        r = client.get("/api/auth/status")
        assert r.status_code == 200
        body = r.json()
        # conftest で APP_PASSWORD="" にしているので auth_required=False
        assert body["auth_required"] is False

    def test_login_in_dev_mode_returns_dev_token(self, client):
        r = client.post("/api/auth/login", json={"password": "anything"})
        assert r.status_code == 200
        assert r.json()["token"] == "dev-mode"


# ─── /api/stats ──────────────────────────────────────────────────────

class TestStatsAPI:
    def test_empty_db(self, client, db):
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["companies"] == 0
        assert body["records"] == 0

    def test_with_data(self, client, db):
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31", pl_revenue=1000.0)
        r = client.get("/api/stats")
        body = r.json()
        assert body["companies"] == 1
        assert body["records"] == 1
        assert body["latest_year"] == 2024


# ─── /api/companies ──────────────────────────────────────────────────

class TestCompaniesAPI:
    def test_empty(self, client, db):
        r = client.get("/api/companies")
        assert r.status_code == 200
        assert r.json() == {"total": 0, "items": []}

    def test_list_with_data(self, client, db):
        make_company(db, edinet_code="E000001", sec_code="1301",
                     name="水産A", industry="水産・農林業")
        make_company(db, edinet_code="E000002", sec_code="2050",
                     name="建設B", industry="建設業")
        r = client.get("/api/companies")
        body = r.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_filter_by_industry(self, client, db):
        make_company(db, edinet_code="E000001", industry="水産・農林業")
        make_company(db, edinet_code="E000002", sec_code="9999",
                     industry="建設業")
        r = client.get("/api/companies?industry=建設業")
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["edinet_code"] == "E000002"

    def test_search_by_query(self, client, db):
        make_company(db, edinet_code="E000001", name="日本水産")
        make_company(db, edinet_code="E000002", sec_code="2050",
                     name="大和ハウス工業")
        r = client.get("/api/companies?q=水産")
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "日本水産"

    def test_limit_and_offset(self, client, db):
        for i in range(5):
            make_company(db, edinet_code=f"E00000{i+1}",
                         sec_code=f"100{i}", name=f"会社{i}")
        r = client.get("/api/companies?limit=2&offset=1")
        body = r.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2


# ─── /api/financials/{edinet_code} ──────────────────────────────────

class TestFinancialsAPI:
    def test_404_when_no_data(self, client, db):
        r = client.get("/api/financials/E999999")
        assert r.status_code == 404

    def test_returns_records(self, client, db):
        make_company(db)
        make_record(db, year=2023, period_end="2023-03-31",
                    pl_revenue=1000.0, op_margin=10.0)
        make_record(db, year=2024, period_end="2024-03-31",
                    pl_revenue=1200.0, op_margin=12.0)
        r = client.get("/api/financials/E000001")
        assert r.status_code == 200
        body = r.json()
        assert body["edinet_code"] == "E000001"
        assert len(body["records"]) == 2
        # 年度昇順
        assert body["records"][0]["year"] == 2023
        assert body["records"][1]["year"] == 2024
        assert body["records"][0]["pl"]["revenue"] == 1000.0


# ─── /api/screen ─────────────────────────────────────────────────────

class TestScreenAPI:
    def test_empty_filter(self, client, db):
        make_company(db, edinet_code="E001", sec_code="0001")
        make_company(db, edinet_code="E002", sec_code="0002")
        make_record(db, edinet_code="E001", year=2024,
                    period_end="2024-03-31", op_margin=15.0)
        make_record(db, edinet_code="E002", year=2024,
                    period_end="2024-03-31", op_margin=5.0)
        r = client.post("/api/screen", json={})
        body = r.json()
        assert body["count"] == 2

    def test_filter_by_op_margin(self, client, db):
        make_company(db, edinet_code="E001", sec_code="0001")
        make_company(db, edinet_code="E002", sec_code="0002")
        make_record(db, edinet_code="E001", year=2024,
                    period_end="2024-03-31", op_margin=15.0)
        make_record(db, edinet_code="E002", year=2024,
                    period_end="2024-03-31", op_margin=5.0)
        r = client.post("/api/screen", json={"min_op_margin": 10.0})
        body = r.json()
        assert body["count"] == 1
        assert body["results"][0]["edinet_code"] == "E001"

    def test_filter_by_industry(self, client, db):
        make_company(db, edinet_code="E001", sec_code="0001",
                     industry="水産・農林業")
        make_company(db, edinet_code="E002", sec_code="0002",
                     industry="建設業")
        make_record(db, edinet_code="E001", year=2024, period_end="2024-03-31",
                    industry="水産・農林業", op_margin=10.0)
        make_record(db, edinet_code="E002", year=2024, period_end="2024-03-31",
                    industry="建設業", op_margin=10.0)
        r = client.post("/api/screen", json={"industry": "建設業"})
        body = r.json()
        assert body["count"] == 1
        assert body["results"][0]["edinet_code"] == "E002"


# ─── /api/plugins ────────────────────────────────────────────────────

class TestPluginsAPI:
    def test_list_plugins(self, client):
        r = client.get("/api/plugins")
        assert r.status_code == 200
        names = {p["name"] for p in r.json()["plugins"]}
        # 主要プラグインが揃っているか
        assert "recommend" in names
        assert "sector_ols" in names
        assert "gap_analysis" in names
        assert "total_return" in names

    def test_plugin_meta_has_params_schema(self, client):
        r = client.get("/api/plugins")
        for p in r.json()["plugins"]:
            assert "params_schema" in p
            assert "label" in p
            assert isinstance(p["params_schema"], dict)

    def test_run_unknown_plugin_returns_404(self, client):
        r = client.post("/api/plugins/__not_exist__/run", json={})
        assert r.status_code == 404


# ─── /api/recommend ──────────────────────────────────────────────────

class TestRecommendAPI:
    def test_presets_endpoint(self, client):
        r = client.get("/api/recommend/presets")
        assert r.status_code == 200
        body = r.json()
        assert "presets" in body
        assert "バランス型" in body["presets"]
        assert "metrics" in body

    def test_recommend_with_zscore_data(self, client, db):
        # 5社作成、z_roe を 0/0.5/1.0/1.5/2.0 にする
        for i in range(5):
            ec = f"E00000{i+1}"
            make_company(db, edinet_code=ec, sec_code=f"100{i}",
                         name=f"会社{i}")
            make_record(db, edinet_code=ec, year=2024,
                        period_end="2024-03-31",
                        z_roe=0.5 * i, z_op_margin=0.5 * i,
                        z_revenue=0.5 * i, market_cap=1000.0 + i)
        r = client.post("/api/recommend",
                        json={"preset": "高収益重視", "top_n": 3})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 3
        # 高 z_roe が上位
        assert body["results"][0]["edinet_code"] == "E000005"
        assert body["results"][0]["rank"] == 1


# ─── /api/gap-analysis ───────────────────────────────────────────────

class TestGapAnalysisAPI:
    def test_no_data_returns_error(self, client, db):
        # gap_ratio が無いと「先に業種別OLSを実行してください」
        r = client.get("/api/gap-analysis")
        # ValueError → 404
        assert r.status_code == 404


# ─── /api/scheduler ──────────────────────────────────────────────────

class TestSchedulerAPI:
    def test_status(self, client):
        r = client.get("/api/scheduler/status")
        assert r.status_code == 200
        body = r.json()
        assert "enabled" in body
        assert "next_run" in body

    def test_toggle(self, client):
        before = client.get("/api/scheduler/status").json()["enabled"]
        r = client.post("/api/scheduler/toggle")
        assert r.status_code == 200
        after = r.json()["enabled"]
        assert after != before
        # 元に戻す
        client.post("/api/scheduler/toggle")


# ─── /api/collect/status ─────────────────────────────────────────────

class TestCollectStatus:
    def test_initial_state(self, client, db):
        r = client.get("/api/collect/status")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] in (True, False)
        assert "recent_jobs" in body


# ─── /api/collect/edinet-coverage ────────────────────────────────────

class TestEdinetCoverage:
    def test_empty_db(self, client, db):
        r = client.get("/api/collect/edinet-coverage")
        assert r.status_code == 200

    def test_with_data(self, client, db):
        make_company(db, edinet_code="E001", sec_code="0001")
        make_company(db, edinet_code="E002", sec_code="0002")
        make_record(db, edinet_code="E001", year=2024, period_end="2024-03-31")
        r = client.get("/api/collect/edinet-coverage")
        body = r.json()
        assert "total_companies" in body or "companies" in body
