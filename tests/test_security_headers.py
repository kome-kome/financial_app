"""セキュリティレスポンスヘッダの検証。

CSP の追加ハードニングディレクティブ・Permissions-Policy・HSTS（HTTPS 時のみ）を確認する。
UI を壊さない方針のため script-src の 'unsafe-inline' と jsdelivr が維持されていることも検証する。

DB 不要の静的ページ（"/" = dashboard.html, FileResponse）を対象にする。
TestClient を with 無しで使うことで lifespan（init_db / Postgres 依存）を回避する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


def test_csp_contains_hardening_directives():
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy", "")
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-src 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp, f"CSP に {directive} が含まれていない: {csp}"


def test_permissions_policy_present():
    r = client.get("/")
    pp = r.headers.get("Permissions-Policy", "")
    assert "geolocation=()" in pp and "camera=()" in pp, pp


def test_hsts_only_on_https():
    # 平文 HTTP（既定）では HSTS を付与しない（誤って HTTPS 固定化しないため）
    r_http = client.get("/")
    assert "Strict-Transport-Security" not in r_http.headers
    # X-Forwarded-Proto: https（Render のリバースプロキシ想定）では付与する
    r_https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert r_https.headers.get("Strict-Transport-Security") == "max-age=31536000"


def test_script_src_unchanged_no_ui_break():
    # PR-1 は UI を変更しないため、script-src の 'unsafe-inline' と jsdelivr は維持する
    r = client.get("/")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "'unsafe-inline'" in csp
    assert "https://cdn.jsdelivr.net" in csp
