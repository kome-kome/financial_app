"""slowapi レート制限の動作テスト

conftest.py で RATELIMIT_ENABLED=false に設定されているため、
このファイル内では `limiter.enabled = True` に切り替え、
各テスト前後で `limiter.reset()` してカウンタをクリーンにする。
"""
import pytest

from tests.conftest import make_company, make_record


@pytest.fixture
def rate_limited_client(client):
    """レート制限を有効化した TestClient を提供。

    既存テスト用に conftest で `RATELIMIT_ENABLED=false` を設定しているため、
    fixture でフラグを差し替え、終了時に元の状態に戻す。
    """
    from api import limiter
    saved = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    yield client
    limiter.reset()
    limiter.enabled = saved


class TestAuthRateLimit:
    """/api/auth/login は 10/minute"""

    def test_login_429_after_limit_exceeded(self, rate_limited_client):
        # 10 回は通る、11 回目で 429
        for i in range(10):
            r = rate_limited_client.post("/api/auth/login",
                                         json={"password": "anything"})
            assert r.status_code == 200, f"{i+1}回目で予期せず失敗: {r.status_code}"
        r = rate_limited_client.post("/api/auth/login",
                                     json={"password": "anything"})
        assert r.status_code == 429
        assert "rate limit" in r.text.lower() or "429" in r.text or "exceeded" in r.text.lower()


class TestResetPasswordRateLimit:
    """/api/auth/reset-password は 3/minute"""

    def test_reset_password_429_after_3_requests(self, rate_limited_client):
        # APP_RECOVERY_KEY 未設定なので 503 を返すが、レート制限カウントは進む
        for i in range(3):
            r = rate_limited_client.post(
                "/api/auth/reset-password",
                json={"recovery_key": "x", "new_password": "y" * 8}
            )
            assert r.status_code != 429, f"{i+1}回目で 429 発生（早すぎ）"
        r = rate_limited_client.post(
            "/api/auth/reset-password",
            json={"recovery_key": "x", "new_password": "y" * 8}
        )
        assert r.status_code == 429


class TestAnalysisRateLimit:
    """/api/screen, /api/recommend は 20/minute"""

    def test_screen_429_after_20_requests(self, rate_limited_client, db):
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31", op_margin=10.0)
        for i in range(20):
            r = rate_limited_client.post("/api/screen", json={})
            assert r.status_code == 200, f"{i+1}回目: {r.status_code}"
        r = rate_limited_client.post("/api/screen", json={})
        assert r.status_code == 429

    def test_recommend_shares_separate_counter_from_screen(self, rate_limited_client, db):
        """/api/recommend と /api/screen は独立したカウンタ（path別キーではないがエンドポイント別）"""
        # /api/screen を 20回呼ぶ
        for _ in range(20):
            rate_limited_client.post("/api/screen", json={})
        # /api/recommend は別カウンタなので 1回目は通る
        r = rate_limited_client.post("/api/recommend", json={"preset": "バランス型"})
        assert r.status_code in (200, 400), f"recommend が予期せず失敗: {r.status_code} {r.text[:100]}"


class TestCollectRateLimit:
    """/api/collect/* は 3/minute"""

    def test_smart_start_429_after_3_requests(self, rate_limited_client, db):
        """レート制限はエンドポイント本体より先に評価される。
        既に実行中フラグを立てておけば全て即座に 400 で返り、4回目で 429 になる
        （バックグラウンドタスクが実 EDINET API を叩かないようにするための工夫）。
        """
        from api import _job_status
        saved = _job_status["running"]
        _job_status["running"] = True
        try:
            responses = []
            for _ in range(4):
                r = rate_limited_client.post("/api/collect/smart-start",
                                             json={"years_back": 1})
                responses.append(r.status_code)
            # 1-3回目: 400（既に実行中）、4回目: 429
            assert responses[:3] == [400, 400, 400], f"想定外: {responses}"
            assert responses[3] == 429, f"4回目が 429 でない: {responses}"
        finally:
            _job_status["running"] = saved

    def test_collect_start_429_after_3_requests(self, rate_limited_client, db):
        """/api/collect/start も同様に 3/minute"""
        from api import _job_status
        saved = _job_status["running"]
        _job_status["running"] = True
        try:
            responses = []
            for _ in range(4):
                r = rate_limited_client.post("/api/collect/start",
                                             json={"years_back": 1})
                responses.append(r.status_code)
            assert responses[3] == 429, f"想定外: {responses}"
        finally:
            _job_status["running"] = saved


class TestRateLimitDisabledByDefault:
    """conftest で RATELIMIT_ENABLED=false → デフォルトでは何度叩いても通る"""

    def test_screen_no_limit_in_default_mode(self, client, db):
        # client は通常のフィクスチャ（limiter 無効）
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31")
        for _ in range(25):  # 制限値 20 を超える
            r = client.post("/api/screen", json={})
            assert r.status_code == 200

    def test_login_no_limit_in_default_mode(self, client):
        for _ in range(15):  # 制限値 10 を超える
            r = client.post("/api/auth/login", json={"password": "x"})
            assert r.status_code == 200
