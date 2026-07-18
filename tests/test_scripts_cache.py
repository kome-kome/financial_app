"""scripts/_cache.py のローカルキャッシュ挙動テスト（Issue #355・Egress 削減）。

本番 DB 非依存。producer 呼び出し回数を数えることで「2 回目以降は本番へ pull しない
（producer が再実行されない）」ことを検証する＝Egress 削減の中核契約。
"""
import pytest

from scripts import _cache


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    # キャッシュディレクトリをテスト専用 tmp へ隔離し、リフレッシュ状態も毎回リセット
    monkeypatch.setattr(_cache, "_CACHE_DIR", tmp_path / "cache")
    _cache.set_refresh(False)
    yield
    _cache.set_refresh(False)


def test_producer_runs_once_then_served_from_cache():
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"data": calls["n"]}

    first = _cache.cached("k", producer)
    second = _cache.cached("k", producer)

    assert calls["n"] == 1                 # 2 回目は producer を呼ばない＝本番 DB 非アクセス
    assert first == {"data": 1}
    assert second == {"data": 1}           # 同一値をキャッシュから返す


def test_refresh_forces_reproduce():
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return calls["n"]

    assert _cache.cached("k", producer) == 1
    _cache.set_refresh(True)
    assert _cache.cached("k", producer) == 2   # --refresh-cache 相当で再取得
    _cache.set_refresh(False)
    assert _cache.cached("k", producer) == 2   # 再取得後はまたキャッシュ優先


def test_distinct_keys_do_not_collide():
    a = _cache.cached("weekly_prices_close", lambda: "prices")
    b = _cache.cached("disclosures_all", lambda: "disc")
    assert a == "prices"
    assert b == "disc"


def test_roundtrip_preserves_value():
    import pandas as pd

    payload = {"E00001": pd.DataFrame({"week_start": ["2026-01-05"], "close_last": [123.4]})}
    _cache.cached("weekly_prices_close", lambda: payload)
    loaded = _cache.cached("weekly_prices_close", lambda: {})  # producer は呼ばれず前回値を返す
    pd.testing.assert_frame_equal(loaded["E00001"], payload["E00001"])
