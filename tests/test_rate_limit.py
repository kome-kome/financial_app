"""slowapi レート制限の検証。

レート制限はテスト全体では誤発火防止のため既定で無効（conftest が RATELIMIT_ENABLED=false）。
本ファイルでは limiter.enabled を実行時に True へ切り替え、429 が返ることを検証する。
DB 不要の /api/auth/login（パスワード照合のみ）を対象にする。
"""
import os
import sys


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


# ── 送り手ごとの枠（#1096）────────────────────────────────────────────────
# Render では接続元がいつも中継の IP。X-Forwarded-For の末尾から N 番目だけを信じる。

from starlette.requests import Request  # noqa: E402

_PEER = "10.0.0.5"


def _req(*xff_lines):
    headers = [(b"x-forwarded-for", line.encode()) for line in xff_lines]
    return Request({"type": "http", "headers": headers, "client": (_PEER, 1234)})


class TestRateLimitKey:
    def test_hops_zero_keeps_peer(self, monkeypatch):
        """既定（0）は従来どおり接続元。ヘッダがあっても読まない。"""
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 0)
        assert api._rate_limit_key(_req("198.51.100.1")) == _PEER

    def test_hops_one_takes_last(self, monkeypatch):
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 1)
        assert api._rate_limit_key(_req("198.51.100.1")) == "198.51.100.1"

    def test_hops_two_takes_second_from_right(self, monkeypatch):
        """中継が2段（例: CDN → ロードバランサ）なら末尾から2番目が送り手。"""
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 2)
        assert api._rate_limit_key(_req("198.51.100.1, 192.0.2.7")) == "198.51.100.1"

    def test_spoofed_leading_entries_are_ignored(self, monkeypatch):
        """送り手が先頭に偽の値を書いても、中継が足した末尾の値が採られる。"""
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 1)
        assert api._rate_limit_key(_req("203.0.113.9, 203.0.113.10, 198.51.100.1")) == "198.51.100.1"

    def test_too_few_entries_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 2)
        assert api._rate_limit_key(_req("198.51.100.1")) == _PEER
        assert api._rate_limit_key(_req()) == _PEER

    def test_unparsable_entry_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 1)
        assert api._rate_limit_key(_req("unknown")) == _PEER

    def test_multiple_header_lines_are_joined(self, monkeypatch):
        """中継が行を分けて足しても、全行をつないだ末尾を採る。"""
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 1)
        assert api._rate_limit_key(_req("203.0.113.9", "198.51.100.1")) == "198.51.100.1"

    def test_ipv6_is_normalized(self, monkeypatch):
        monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 1)
        assert api._rate_limit_key(_req("2001:DB8::0001")) == "2001:db8::1"


def _login(xff):
    return client.post("/api/auth/login", json={"password": "wrong"},
                       headers={"X-Forwarded-For": xff}).status_code


def test_other_sender_is_not_locked_out(monkeypatch):
    """N=1 で、ある送り手が 429 になっても、別の送り手は 429 にならない（#1096 の本題）。
    先頭に偽の値を書いても、詰まった送り手の枠からは抜けられない。"""
    monkeypatch.setattr(api, "RATELIMIT_XFF_HOPS", 1)
    api.limiter.reset()
    api.limiter.enabled = True
    try:
        codes = [_login("198.51.100.1") for _ in range(15)]
        spoofed = _login("203.0.113.9, 198.51.100.1")
        other = _login("198.51.100.2")
    finally:
        api.limiter.enabled = False
        api.limiter.reset()
    assert 429 in codes, f"レート制限が効かず429が出ない: {codes}"
    assert spoofed == 429, "先頭に偽の値を書くと詰まった枠から抜けられた"
    assert other != 429, "別の送り手まで締め出された"
