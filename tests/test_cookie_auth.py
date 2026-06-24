"""Cookie/CSRF 認証（HttpOnly Cookie + Double-Submit CSRF）のユニット検証。

APP_PASSWORD を fixture で一時設定して認証有効パスを検証する（他テストへ波及させない）。
TestClient（httpx）は Cookie をセッションとして保持するため、login 後の自動送信を検証できる。
保護対象の判定はミドルウェア層で行われるため、存在しない /api/ パスを叩いて
401/403（ミドルウェアでブロック）か、それ以外（通過して 404 等）かで判定する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

import pytest  # noqa: E402
import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROBE = "/api/__authprobe__"  # 存在しない /api/ パス（ミドルウェア層のみを検証）


@pytest.fixture
def auth_client():
    """APP_PASSWORD を有効化した独立 TestClient（Cookie 隔離）。テスト後に元へ戻す。"""
    old_pw, old_sk = api.APP_PASSWORD, api.APP_SECRET_KEY
    api.APP_PASSWORD = "testpass123"
    api.APP_SECRET_KEY = "test-secret-cookie"
    try:
        yield TestClient(api.app)
    finally:
        api.APP_PASSWORD, api.APP_SECRET_KEY = old_pw, old_sk


def _login(client, password="testpass123"):
    return client.post("/api/auth/login", json={"password": password})


def _set_cookie_str(resp):
    try:
        return " ".join(resp.headers.get_list("set-cookie")).lower()
    except Exception:
        return str(resp.headers.get("set-cookie", "")).lower()


def test_login_sets_httponly_auth_and_readable_csrf(auth_client):
    r = _login(auth_client)
    assert r.status_code == 200, r.text
    sc = _set_cookie_str(r)
    assert "auth_token=" in sc and "httponly" in sc, sc      # 認証Cookieは HttpOnly
    assert "csrf_token=" in sc                                # CSRF Cookie も発行
    assert "samesite=lax" in sc
    assert auth_client.cookies.get("auth_token")
    assert auth_client.cookies.get("csrf_token")


def test_wrong_password_is_401(auth_client):
    assert _login(auth_client, "wrong").status_code == 401


def test_unauthenticated_get_is_401(auth_client):
    assert auth_client.get(PROBE).status_code == 401


def test_authenticated_get_passes_auth(auth_client):
    _login(auth_client)
    # 認証通過後は存在しないパスのため 404（401 ではない）
    assert auth_client.get(PROBE).status_code != 401


def test_post_without_csrf_is_403(auth_client):
    _login(auth_client)
    assert auth_client.post(PROBE).status_code == 403


def test_post_with_matching_csrf_passes(auth_client):
    _login(auth_client)
    csrf = auth_client.cookies.get("csrf_token")
    r = auth_client.post(PROBE, headers={"X-CSRF-Token": csrf})
    assert r.status_code not in (401, 403)  # 認証・CSRF 通過（404 を想定）


def test_post_with_wrong_csrf_is_403(auth_client):
    _login(auth_client)
    assert auth_client.post(PROBE, headers={"X-CSRF-Token": "bogus"}).status_code == 403


def test_logout_clears_auth(auth_client):
    _login(auth_client)
    assert auth_client.get(PROBE).status_code != 401
    assert auth_client.post("/api/auth/logout").status_code == 200
    # logout 後は保護 GET が 401 に戻る
    assert auth_client.get(PROBE).status_code == 401


def test_auth_endpoints_are_exempt(auth_client):
    # 未認証でも /api/auth/status（GET）・login（POST, CSRF不要）は通る
    assert auth_client.get("/api/auth/status").status_code == 200
    assert auth_client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401


def test_no_authorization_header_used(auth_client):
    # Bearer ヘッダではなく Cookie 認証であること: ヘッダだけでは通らない
    _login(auth_client)
    token = api._create_token()
    fresh = TestClient(api.app)  # Cookie を持たない別クライアント
    r = fresh.get(PROBE, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401  # Authorization ヘッダは無視される


def test_token_invalidated_after_password_change(auth_client):
    """パスワード変更後に旧トークンが 401 になることを確認（#230）。"""
    _login(auth_client)
    assert auth_client.get(PROBE).status_code != 401  # ログイン済み: 通過

    # パスワード変更（reset-password が行う api.APP_PASSWORD 書き換えを模倣）
    old_pw = api.APP_PASSWORD
    api.APP_PASSWORD = "newpass_changed_456"
    try:
        # 旧 Cookie のトークンは旧パスワード由来の fingerprint で署名済み → 失効
        assert auth_client.get(PROBE).status_code == 401
    finally:
        api.APP_PASSWORD = old_pw
