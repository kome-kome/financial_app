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
from datetime import datetime, date

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
        d = r.json()
        # Cookie 認証移行後: dev モード（APP_PASSWORD 未設定）は token を返さず ok/dev_mode を返す
        assert d["ok"] is True and d.get("dev_mode") is True

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
        # 予測値は regression_results に保存される。gap_ratio 付きの件数をカウント（OLS実行済み判定）
        from database import RegressionResult
        db.add(make_fin(edinet_code="E00001", year=2023))
        db.add(RegressionResult(edinet_code="E00001", year=2023,
                                period_end=date(2023, 3, 31), gap_ratio=12.3))
        db.add(RegressionResult(edinet_code="E00002", year=2023,
                                period_end=date(2023, 3, 31), gap_ratio=None))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        body = client.get("/api/stats").json()
        assert body["records_with_prediction"] == 1


class TestHeavyPluginRenderBlock:
    def test_sector_ols_blocked_in_light_mode(self, db, monkeypatch):
        # Render 軽量モードでは重い回帰プラグインは 403（ローカル実行を促す）
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", True)
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.post("/api/plugins/sector_ols/run", json={})
        assert r.status_code == 403

    def test_sector_ols_not_blocked_when_not_light(self, db, monkeypatch):
        # 通常モードでは heavy ブロックは発火しない（データ無しで実行→400 になる）
        monkeypatch.setattr(api, "RENDER_LIGHT_MODE", False)
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.post("/api/plugins/sector_ols/run", json={})
        assert r.status_code != 403


class TestGapAnalysisDependency:
    """candidate4: depends_on の実行時強制を HTTP レイヤで検証（C1: 専用404 / 汎用400）。"""

    def test_gap_analysis_404_when_sector_ols_not_run(self, db):
        # 回帰未実行（regression_results 空）→ depends_on 未充足で 404
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/gap-analysis").status_code == 404

    def test_run_plugin_400_when_dependency_unsatisfied(self, db):
        # 汎用 runner 経由は同じ前提条件未充足を 400 で返す
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.post("/api/plugins/gap_analysis/run", json={}).status_code == 400

    def test_gap_analysis_200_empty_when_regression_exists_but_no_rows(self, db):
        # 回帰はある（depends_on 充足）が当該フィルタに該当行なし → 200・空結果（404 ではない）
        from database import RegressionResult
        db.add(RegressionResult(edinet_code="E00001", year=2023, period_end=date(2023, 3, 31),
                                predicted_market_cap=1.0, gap_ratio=5.0, model="ols", sector="x"))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/gap-analysis")
        assert r.status_code == 200
        assert r.json()["count"] == 0


class TestBacktestEndpoint:
    """candidate3: backtest 抽出後のエンドポイント配線（routing→backtest.run 委譲）を検証。"""

    def test_validation_rejects_bad_months_ago(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/backtest", params={"months_ago": 0}).status_code == 400

    def test_returns_no_data_on_empty_db(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/backtest")
        assert r.status_code == 200
        assert r.json()["total_candidates"] == 0

    def test_scores_via_financial_metric(self, db, make_metric):
        # 旧バグ（FinancialRecord 引きで常に空）の HTTP レイヤ回帰
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           market_cap=10000.0, z_roe=2.0))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/backtest")
        assert r.status_code == 200
        assert r.json()["total_candidates"] == 1

    def test_rejects_unknown_source(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/backtest", params={"source": "nope"}).status_code == 400

    def test_valuation_source_passes_through(self, db, make_metric):
        # source=valuation: gap_ratio を持つ銘柄のみ候補化（HTTP レイヤ）
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           gap_ratio=15.0, div_yield=2.0))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/backtest", params={"source": "valuation"})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "valuation"
        assert body["total_candidates"] == 1


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

    def test_limit_out_of_range_returns_400(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/companies", params={"limit": 0}).status_code == 400
        assert client.get("/api/companies", params={"limit": 501}).status_code == 400
        assert client.get("/api/companies", params={"limit": 100000000}).status_code == 400

    def test_negative_offset_returns_400(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/companies", params={"offset": -1}).status_code == 400

    def test_valid_limit_and_offset_returns_200(self, db, make_company):
        db.add(make_company(edinet_code="E00001", name="テスト", industry="情報"))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/companies", params={"limit": 1, "offset": 0}).status_code == 200
        assert client.get("/api/companies", params={"limit": 500, "offset": 0}).status_code == 200


class TestFinancialsEndpoint:
    def test_returns_records_year_ascending(self, db, make_metric):
        # get_financials は financial_metrics（読み取りモデル）を参照する
        db.add(make_metric(edinet_code="E00001", year=2022, period_end="2022-03-31"))
        db.add(make_metric(edinet_code="E00001", year=2023, period_end="2023-03-31"))
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

    def test_400_when_invalid_edinet_code(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/financials/INVALID").status_code == 400


class TestStockHistoryEndpoint:
    def test_returns_rows_date_ascending(self, db, make_price):
        # DB からは trade_date 降順で取得し、reversed で昇順に整列して返す
        db.add(make_price(edinet_code="E00001", trade_date="2023-01-04", close=1000.0))
        db.add(make_price(edinet_code="E00001", trade_date="2023-01-05", close=1010.0))
        db.commit()
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.get("/api/stock/history/E00001")
        assert r.status_code == 200
        rows = r.json()
        assert [row["trade_date"] for row in rows] == ["2023-01-04", "2023-01-05"]

    def test_400_when_invalid_edinet_code(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/stock/history/INVALID").status_code == 400

    def test_400_when_days_out_of_range(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.get("/api/stock/history/E00001", params={"days": 0}).status_code == 400
        assert client.get("/api/stock/history/E00001", params={"days": 99999}).status_code == 400


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


class TestScreenEndpoint:
    """#74: /api/screen フィルタ条件の統合テスト。"""

    def _setup(self, db, make_fin, make_metric, rows):
        """FinancialRecord + FinancialMetric を同一 edinet_code/year でペア挿入。"""
        for kw in rows:
            db.add(make_fin(edinet_code=kw["edinet_code"], year=kw.get("year", 2023)))
            db.add(make_metric(**kw))
        db.commit()

    def test_no_filter_returns_all(self, db, make_fin, make_metric):
        self._setup(db, make_fin, make_metric, [
            {"edinet_code": "E00001", "year": 2023, "roe": 0.15},
            {"edinet_code": "E00002", "year": 2023, "roe": 0.08},
        ])
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.post("/api/screen", json={})
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_combined_filters_industry_and_roe(self, db, make_fin, make_metric):
        self._setup(db, make_fin, make_metric, [
            {"edinet_code": "E00001", "year": 2023, "industry": "情報・通信業", "roe": 0.20},
            {"edinet_code": "E00002", "year": 2023, "industry": "銀行業", "roe": 0.20},
        ])
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.post("/api/screen", json={"min_roe": 0.15, "industry": "情報・通信業"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["results"][0]["edinet_code"] == "E00001"

    def test_no_match_returns_empty(self, db, make_fin, make_metric):
        self._setup(db, make_fin, make_metric, [
            {"edinet_code": "E00001", "year": 2023, "roe": 0.05},
        ])
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.post("/api/screen", json={"min_roe": 999.0})
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_invalid_limit_type_returns_422(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        r = client.post("/api/screen", json={"limit": "not_a_number"})
        assert r.status_code == 422

    def test_limit_out_of_range_returns_422(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.post("/api/screen", json={"limit": 0}).status_code == 422
        assert client.post("/api/screen", json={"limit": 501}).status_code == 422

    def test_valid_limit_boundary_returns_200(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        assert client.post("/api/screen", json={"limit": 1}).status_code == 200
        assert client.post("/api/screen", json={"limit": 500}).status_code == 200
