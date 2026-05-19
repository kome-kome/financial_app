"""HttpOnly Cookie 認証 + CSRF Double-Submit パターンのテスト"""
import pytest


@pytest.fixture
def auth_client(client):
    """APP_PASSWORD を一時的に設定して認証必須モードで動作させる TestClient"""
    import api as _api
    saved = _api.APP_PASSWORD
    _api.APP_PASSWORD = "test-secret-pw"
    yield client
    _api.APP_PASSWORD = saved
    client.cookies.clear()


def _login_and_get_csrf(client, password="test-secret-pw") -> str:
    """ログインし、CSRF トークンを取得して返す（auth_token Cookie も自動セット）"""
    r = client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, f"ログイン失敗: {r.status_code} {r.text}"
    csrf = r.cookies.get("csrf_token")
    assert csrf, "csrf_token Cookie が発行されていない"
    return csrf


class TestCookieIssuance:
    def test_login_sets_httponly_auth_cookie(self, auth_client):
        r = auth_client.post("/api/auth/login", json={"password": "test-secret-pw"})
        assert r.status_code == 200
        # auth_token は HttpOnly フラグ付き
        set_cookie = r.headers.get("set-cookie", "")
        assert "auth_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        # SameSite=strict（CSRF 防御の追加層）
        assert "SameSite=strict" in set_cookie

    def test_login_sets_non_httponly_csrf_cookie(self, auth_client):
        r = auth_client.post("/api/auth/login", json={"password": "test-secret-pw"})
        # CSRF Cookie は JS から読めるよう HttpOnly なし
        set_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") \
                       else [r.headers.get("set-cookie", "")]
        csrf_cookie_line = next((s for s in set_cookies if "csrf_token=" in s), "")
        assert csrf_cookie_line, "csrf_token Cookie が発行されていない"
        assert "HttpOnly" not in csrf_cookie_line

    def test_login_wrong_password_returns_401(self, auth_client):
        r = auth_client.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401

    def test_logout_clears_cookies(self, auth_client):
        _login_and_get_csrf(auth_client)
        r = auth_client.post("/api/auth/logout")
        assert r.status_code == 200
        # Cookie 削除のため Max-Age=0 または expires=過去日 を含む
        set_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") \
                       else [r.headers.get("set-cookie", "")]
        assert any("auth_token" in s for s in set_cookies)


class TestProtectedEndpointRequiresAuth:
    def test_get_without_cookie_returns_401(self, auth_client):
        # ログインしていない状態で GET → 401
        r = auth_client.get("/api/stats")
        assert r.status_code == 401

    def test_get_with_auth_cookie_succeeds(self, auth_client):
        _login_and_get_csrf(auth_client)
        r = auth_client.get("/api/stats")
        assert r.status_code == 200

    def test_get_with_invalid_token_returns_401(self, auth_client):
        # 不正な auth_token Cookie を直接セット
        auth_client.cookies.set("auth_token", "invalid-token-value")
        r = auth_client.get("/api/stats")
        assert r.status_code == 401


class TestCsrfDoubleSubmit:
    def test_post_without_csrf_header_returns_403(self, auth_client):
        """POST に X-CSRF-Token ヘッダがないと 403"""
        _login_and_get_csrf(auth_client)
        # CSRF ヘッダ無しで POST
        r = auth_client.post("/api/screen", json={})
        assert r.status_code == 403
        assert "CSRF" in r.json()["detail"]

    def test_post_with_wrong_csrf_returns_403(self, auth_client):
        _login_and_get_csrf(auth_client)
        r = auth_client.post(
            "/api/screen", json={},
            headers={"X-CSRF-Token": "wrong-csrf-value"},
        )
        assert r.status_code == 403

    def test_post_with_correct_csrf_succeeds(self, auth_client):
        csrf = _login_and_get_csrf(auth_client)
        r = auth_client.post(
            "/api/screen", json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200

    def test_get_does_not_require_csrf(self, auth_client):
        """GET は CSRF チェック対象外（idempotent）"""
        _login_and_get_csrf(auth_client)
        r = auth_client.get("/api/stats")  # CSRF ヘッダなし
        assert r.status_code == 200

    def test_auth_endpoint_exempt_from_csrf(self, auth_client):
        """/api/auth/* は CSRF・auth ともに免除（login 自体を保護するとデッドロック）"""
        # /api/auth/login は CSRF ヘッダなしでも 200/401（パスワード次第）
        r = auth_client.post("/api/auth/login", json={"password": "test-secret-pw"})
        assert r.status_code == 200
        # /api/auth/logout も CSRF なしで動く
        r = auth_client.post("/api/auth/logout")
        assert r.status_code == 200


class TestCorsAllowCredentials:
    """allow_credentials=True と allow_origins (specific) の組み合わせを確認"""

    def test_preflight_includes_credentials_header(self, auth_client):
        """OPTIONS（プリフライト）の応答に Access-Control-Allow-Credentials: true が含まれる"""
        # CORS は Origin ヘッダがないと CORSMiddleware が応答しないため明示
        r = auth_client.options(
            "/api/stats",
            headers={
                "Origin": "http://testserver",
                "Access-Control-Request-Method": "POST",
            },
        )
        # Allow-Credentials が true なら Cookie ベース認証が有効に動く
        assert r.headers.get("access-control-allow-credentials") == "true"
