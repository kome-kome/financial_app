"""slowapi レート制限の検証。

レート制限はテスト全体では誤発火防止のため既定で無効（conftest が RATELIMIT_ENABLED=false）。
本ファイルでは limiter.enabled を実行時に True へ切り替え、429 が返ることを検証する。
DB 不要の /api/auth/login（パスワード照合のみ）を対象にする。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


def _spam_login(n):
    # 認証情報は不問（無効パスワードでも 401/200 を返す。ここでは 429 の有無のみを見る）
    return [client.post("/api/auth/login", json={"password": "wrong"}).status_code for _ in range(n)]


def test_disabled_by_default_no_429():
    """既定（RATELIMIT_ENABLED=false）では連打しても 429 にならない。"""
    assert api.limiter.enabled is False
    codes = _spam_login(15)
    assert 429 not in codes, f"無効化中に429が発生: {codes}"


def test_login_returns_429_when_enabled():
    """RATELIMIT_AUTH=10/minute。enabled=True で 11 連打すると 429 が混じる。"""
    # 直前まで enabled=False のためカウンタは未加算。有効化後の連打で 429 を確認する。
    api.limiter.enabled = True
    try:
        codes = _spam_login(15)
    finally:
        api.limiter.enabled = False
    assert 429 in codes, f"レート制限が効かず429が出ない: {codes}"


def test_constants_are_defined():
    for name in ("RATELIMIT_COLLECT", "RATELIMIT_ANALYSIS", "RATELIMIT_REFRESH",
                 "RATELIMIT_AUTH", "RATELIMIT_RESET"):
        assert hasattr(api, name), f"{name} が未定義"
