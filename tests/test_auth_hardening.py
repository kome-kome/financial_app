"""認証まわりのフェイルセーフ強化（Issue #345）の検証。

対象:
  - _is_production_like(): RENDER / RENDER_LIGHT_MODE による本番相当判定
  - _hash_password / _verify_password / _is_password_hashed: scrypt ハッシュ化と
    平文/ハッシュ両対応の照合、salt によるランダム化、平文→ハッシュ移行
  - login: メモリ側 APP_PASSWORD がハッシュでも平文 POST で認証が通る（reset 後の状態）
  - fail-fast: 本番相当環境で APP_SECRET_KEY 未設定なら import 時に起動停止（subprocess）

レート制限は conftest.py で無効化済み。
"""
import os
import subprocess
import sys

os.environ.setdefault("APP_RATELIMIT_ENABLED", "false")

import pytest  # noqa: E402
import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


# ── _is_production_like ─────────────────────────────────────────────────────
@pytest.mark.parametrize("render,light,expected", [
    ("true",  False, True),   # Render 自動注入
    ("1",     False, True),
    ("yes",   False, True),
    ("",      True,  True),    # render.yaml の RENDER_LIGHT_MODE
    ("false", False, False),   # ローカル/テスト
    ("",      False, False),
])
def test_is_production_like(monkeypatch, render, light, expected):
    monkeypatch.setenv("RENDER", render)
    monkeypatch.setattr(api, "RENDER_LIGHT_MODE", light)
    assert api._is_production_like() is expected


# ── パスワードハッシュ化 ────────────────────────────────────────────────────
def test_hash_password_roundtrip():
    h = api._hash_password("mypw-123")
    assert h.startswith("scrypt$")
    assert "mypw-123" not in h                 # 平文を含まない
    assert api._is_password_hashed(h)
    assert api._verify_password("mypw-123", h)  # 正しい PW は通る
    assert not api._verify_password("wrong", h)  # 誤りは弾く


def test_hash_password_is_salted():
    # 同一 PW でも salt によりハッシュは毎回異なる
    assert api._hash_password("same") != api._hash_password("same")


def test_verify_password_plaintext_backcompat():
    # env 直指定の平文値（移行前）との後方互換照合
    assert api._verify_password("plain", "plain")
    assert not api._verify_password("plain", "other")


def test_verify_password_empty_stored_is_false():
    assert not api._verify_password("anything", "")


def test_is_password_hashed_discriminates():
    assert api._is_password_hashed("scrypt$16384$8$1$abc$def")
    assert not api._is_password_hashed("plaintext")
    assert not api._is_password_hashed("")


def test_plaintext_to_hash_migration():
    # lifespan の移行ロジック相当: 平文 → ハッシュ化 → 照合可能・平文除去
    legacy_plain = "legacy-pw-xyz"
    assert not api._is_password_hashed(legacy_plain)
    migrated = api._hash_password(legacy_plain)
    assert api._is_password_hashed(migrated)
    assert legacy_plain not in migrated
    assert api._verify_password(legacy_plain, migrated)


def test_corrupt_hash_verifies_false():
    # 壊れたハッシュ文字列でも例外を投げず False
    assert not api._verify_password("pw", "scrypt$broken")


# ── login（ハッシュ保持でも平文 POST で認証成功＝reset 後の状態）────────────────
def test_login_with_hashed_password(monkeypatch):
    pw = "secret-pw-123"
    old_pw, old_sk = api.APP_PASSWORD, api.APP_SECRET_KEY
    monkeypatch.setattr(api, "APP_PASSWORD", api._hash_password(pw))
    monkeypatch.setattr(api, "APP_SECRET_KEY", "test-secret-key")
    try:
        r_ok = client.post("/api/auth/login", json={"password": pw})
        assert r_ok.status_code == 200
        assert r_ok.json().get("ok") is True

        r_ng = client.post("/api/auth/login", json={"password": "wrong-pw"})
        assert r_ng.status_code == 401
    finally:
        api.APP_PASSWORD, api.APP_SECRET_KEY = old_pw, old_sk


def test_login_with_plaintext_password_backcompat(monkeypatch):
    # env 直指定の平文 APP_PASSWORD 運用（従来）でも login が通る
    pw = "plain-env-pw"
    monkeypatch.setattr(api, "APP_PASSWORD", pw)
    monkeypatch.setattr(api, "APP_SECRET_KEY", "test-secret-key")
    r = client.post("/api/auth/login", json={"password": pw})
    assert r.status_code == 200


# ── fail-fast（本番相当で必須秘密が未設定なら import 時に起動停止）─────────────
def test_fail_fast_missing_secret_key_in_production():
    """RENDER=true かつ APP_SECRET_KEY 未設定で `import api` が停止する（非0 exit）。"""
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["RENDER"] = "true"
    env["APP_SECRET_KEY"] = ""
    env["APP_PASSWORD"] = "dummy"          # PASSWORD は lifespan 判定なので import では無関係
    env["APP_RATELIMIT_ENABLED"] = "false"
    r = subprocess.run(
        [sys.executable, "-c", "import api"],
        cwd=proj, env=env, capture_output=True, text=True,
        # Windows ローカルでは子プロセス出力(日本語)が cp932 デコードで落ちるため明示指定
        encoding="utf-8", errors="replace",
    )
    combined = (r.stdout or "") + (r.stderr or "")
    assert r.returncode != 0, f"fail-fast せず起動した: {combined}"
    assert "APP_SECRET_KEY" in combined


def test_no_fail_fast_when_not_production():
    """本番相当でなければ（RENDER/RENDER_LIGHT_MODE 無し）import は成功する。"""
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["RENDER"] = "false"            # .env に混入していても本番判定にしない
    env["RENDER_LIGHT_MODE"] = ""
    env["APP_SECRET_KEY"] = ""
    env["APP_PASSWORD"] = ""
    env["APP_RATELIMIT_ENABLED"] = "false"
    r = subprocess.run(
        [sys.executable, "-c", "import api"],
        cwd=proj, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
