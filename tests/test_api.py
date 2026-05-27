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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# import 時の APP_SECRET_KEY 未設定警告を避けるため、import 前にダミーを設定
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


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
        assert r.json()["token"] == "dev-mode"

    def test_refresh_invalid_edinet_code_returns_400(self):
        r = client.post("/api/collect/refresh/INVALID")
        assert r.status_code == 400
