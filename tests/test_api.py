"""api.py のユニットテスト。

純粋関数（JST変換・edinet_code 検証・トークン署名/検証）と、DB 不要の軽量エンドポイント
（system/info・auth/status・auth/login dev-mode・edinet_code バリデーション 400）を検証。
DB 直結エンドポイント（/health 等は SessionLocal を直接使う）と SSE・収集系は対象外。
"""
import base64
import hashlib
import hmac
import os
import sys
import time as _time
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# import 時の APP_SECRET_KEY 未設定警告を避けるため、import 前にダミーを設定
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    """各テスト後に dependency_overrides を必ずクリア（状態汚染防止）。"""
    yield
    api.app.dependency_overrides.clear()


# ── 純粋関数 ─────────────────────────────────────────────────────────────────

class TestHtmlRoutes:
    """全画面が共通レイアウト（app-header-mount）を含むことを確認。"""
    def test_dashboard_uses_common_header(self):
        r = client.get('/')
        assert r.status_code == 200 and 'app-header-mount' in r.text

    def test_collection_uses_page_tabs(self):
        r = client.get('/collection')
        assert r.status_code == 200
        assert 'page-tabs' in r.text and 'app-header-mount' in r.text

    def test_analysis_uses_page_tabs(self):
        # /analysis のナビ混在解消: page-tabs が role="tablist" で存在し、
        # 旧 plugin-nav の混在マークアップは残っていない
        r = client.get('/analysis')
        assert r.status_code == 200
        assert 'id="page-tabs"' in r.text and 'role="tablist"' in r.text
        assert 'id="plugin-nav"' not in r.text

    def test_models_uses_common_header(self):
        r = client.get('/models')
        assert r.status_code == 200 and 'app-header-mount' in r.text

    def test_company_uses_common_header(self):
        r = client.get('/company')
        assert r.status_code == 200 and 'app-header-mount' in r.text

    def test_db_uses_common_header(self):
        r = client.get('/db')
        assert r.status_code == 200 and 'app-header-mount' in r.text

    def test_login_loads_app_css(self):
        r = client.get('/login')
        assert r.status_code == 200 and '/static/css/app.css' in r.text


class TestStaticAssets:
    """共通 CSS/JS が StaticFiles 経由で配信されることを確認。"""
    def test_app_css_served(self):
        r = client.get("/static/css/app.css")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/css")
        # 1日キャッシュが付与される
        assert "max-age=86400" in r.headers.get("cache-control", "")

    def test_core_js_served(self):
        r = client.get("/static/js/core.js")
        assert r.status_code == 200
        ct = r.headers["content-type"]
        # text/javascript or application/javascript 両方許容
        assert "javascript" in ct

    def test_nav_js_served(self):
        assert client.get("/static/js/nav.js").status_code == 200

    def test_static_unauthenticated_when_password_set(self, monkeypatch):
        # APP_PASSWORD が設定されていても /static/* はトークン不要で配信される
        monkeypatch.setattr(api, "APP_PASSWORD", "secret-pw")
        assert client.get("/static/css/app.css").status_code == 200


class TestUtcToJstStr:
    def test_none_returns_none(self):
        assert api._utc_to_jst_str(None) is None

    def test_adds_9_hours_and_suffix(self):
        assert api._utc_to_jst_str(datetime(2023, 1, 1, 0, 0, 0)) == "2023-01-01 09:00:00 JST"


class TestEdinetCodeRegex:
    @pytest.mark.parametrize("code", ["E12345", "E123456"])
    def test_valid(self, code):
        assert api._EDINET_CODE_RE.match(code)

    @pytest.mark.parametrize("code", ["E1234", "E1234567", "e12345", "12345", "E12A45", ""])
    def test_invalid(self, code):
        assert api._EDINET_CODE_RE.match(code) is None


class TestToken:
    def test_roundtrip(self, monkeypatch):
        monkeypatch.setattr(api, "APP_PASSWORD", "secret-pw")
        token = api._create_token()
        assert api._verify_token(token) is True

    def test_rejects_tampered(self, monkeypatch):
        monkeypatch.setattr(api, "APP_PASSWORD", "secret-pw")
        token = api._create_token()
        assert api._verify_token(token + "AAAA") is False
        assert api._verify_token("not-valid-base64!!!") is False

    def test_rejects_expired(self, monkeypatch):
        monkeypatch.setattr(api, "APP_PASSWORD", "secret-pw")
        old_ts = str(int(_time.time()) - api._TOKEN_TTL - 10)
        sig = hmac.new(api.APP_SECRET_KEY.encode(), old_ts.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(f"{old_ts}:{sig}".encode()).decode()
        assert api._verify_token(token) is False

    def test_devmode_accepts_anything(self, monkeypatch):
        monkeypatch.setattr(api, "APP_PASSWORD", "")
        assert api._verify_token("whatever") is True


# ── DB 不要の軽量エンドポイント（認証は APP_PASSWORD 未設定の dev モード）──────

class TestEndpoints:
    def test_system_info(self):
        r = client.get("/api/system/info")
        assert r.status_code == 200
        assert "render_light_mode" in r.json()

    def test_auth_status(self):
        r = client.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["auth_required"] is False

    def test_auth_login_devmode(self):
        r = client.post("/api/auth/login", json={"password": "x"})
        assert r.status_code == 200
        assert r.json()["token"] == "dev-mode"

    def test_refresh_invalid_edinet_code_returns_400(self):
        r = client.post("/api/collect/refresh/INVALID")
        assert r.status_code == 400


# ── /health（SessionLocal を直接呼ぶため monkeypatch で差し替え）──────────────

class TestHealth:
    def test_ok(self, monkeypatch):
        engine = create_engine("sqlite://")
        monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=engine))
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "db": "ok"}

    def test_degraded_on_db_error(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(api, "SessionLocal", boom)
        r = client.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"


# ── DB-backed エンドポイント（get_db を SQLite fixture に差し替え）─────────────

class TestStatsEndpoint:
    def test_counts_and_freshness(self, db, make_company, make_fin):
        db.add(make_company(edinet_code="E00001"))
        db.add(make_company(edinet_code="E00002", name="2社目"))
        db.add(make_fin(edinet_code="E00001", year=2023))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["companies"] == 2
        assert body["records"] == 1
        assert body["latest_year"] == 2023
        assert "freshness" in body
        # 予測値（gap_ratio）が無いので 0（乖離分析はUIでロック）
        assert body["records_with_prediction"] == 0

    def test_records_with_prediction_counts_gap_ratio(self, db, make_fin):
        # gap_ratio が書き込まれたレコードのみカウント（業種別OLS実行済み判定）
        db.add(make_fin(edinet_code="E00001", year=2023, gap_ratio=12.3))
        db.add(make_fin(edinet_code="E00002", year=2023, gap_ratio=None))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        body = client.get("/api/stats").json()
        assert body["records_with_prediction"] == 1


class TestCompaniesEndpoint:
    def test_list_and_filters(self, db, make_company):
        db.add(make_company(edinet_code="E00001", name="トヨタ自動車", industry="輸送用機器"))
        db.add(make_company(edinet_code="E00002", name="ソニーグループ", industry="電気機器"))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/companies").json()["total"] == 2
        q = client.get("/api/companies", params={"q": "トヨタ"}).json()
        assert [i["name"] for i in q["items"]] == ["トヨタ自動車"]
        ind = client.get("/api/companies", params={"industry": "電気機器"}).json()
        assert [i["edinet_code"] for i in ind["items"]] == ["E00002"]


class TestFinancialsEndpoint:
    def test_returns_records_year_ascending(self, db, make_fin):
        db.add(make_fin(edinet_code="E00001", year=2022, period_end="2022-03-31"))
        db.add(make_fin(edinet_code="E00001", year=2023, period_end="2023-03-31"))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/financials/E00001")
        assert r.status_code == 200
        body = r.json()
        assert body["edinet_code"] == "E00001"
        assert [rec["year"] for rec in body["records"]] == [2022, 2023]

    def test_404_when_missing(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/financials/E99999").status_code == 404


class TestCollectStatusEndpoint:
    def test_recent_jobs(self, db):
        from database import CollectionLog
        db.add(CollectionLog(job_type="full", status="done",
                             companies_processed=10, records_saved=50))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/collect/status")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is False
        assert len(body["recent_jobs"]) == 1
        assert body["recent_jobs"][0]["status"] == "done"
