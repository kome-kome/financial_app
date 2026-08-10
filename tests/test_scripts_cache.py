"""scripts/_cache.py のローカルキャッシュ挙動テスト（Issue #355・Egress 削減）。

本番 DB 非依存。producer 呼び出し回数を数えることで「2 回目以降は本番へ pull しない
（producer が再実行されない）」ことを検証する＝Egress 削減の中核契約。
"""
import pytest

from scripts import _cache


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    # キャッシュディレクトリをテスト専用 tmp へ隔離し、リフレッシュ状態・累計も毎回リセット
    monkeypatch.setattr(_cache, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(_cache, "_stats", {"hits": 0, "misses": 0, "produced_bytes": 0})
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


class TestHitMissVisibility:
    """Issue #478: 黙ってミスしても気づけない状態を潰す（#438 の静かな劣化と同型）。"""

    def test_miss_then_hit_then_refresh_are_logged(self, capsys):
        _cache.cached("k", lambda: "v")
        first = capsys.readouterr().err
        assert "MISS" in first and "k" in first          # 1 回目＝本番 DB を引いた

        _cache.cached("k", lambda: "v")
        second = capsys.readouterr().err
        assert "HIT" in second and "MISS" not in second  # 2 回目＝Egress ゼロ

        _cache.set_refresh(True)
        _cache.cached("k", lambda: "v")
        third = capsys.readouterr().err
        assert "REFRESH" in third

    def test_log_is_ascii_only(self, capsys):
        """cp932 コンソールへのリダイレクトで落ちないこと（非 ASCII 記号は出力ごとクラッシュする）。"""
        _cache.cached("k", lambda: "v")
        err = capsys.readouterr().err
        err.encode("cp932")                              # 例外が出なければ安全
        assert err.isascii()

    def test_summary_counts_match_actual_calls(self, capsys):
        _cache.cached("a", lambda: "va")     # MISS
        _cache.cached("b", lambda: "vb")     # MISS
        _cache.cached("a", lambda: "va")     # HIT
        capsys.readouterr()

        _cache._emit_summary()
        summary = capsys.readouterr().err
        assert "hits=1" in summary
        assert "misses=2" in summary

    def test_summary_is_silent_when_cache_unused(self, capsys):
        _cache._emit_summary()
        assert capsys.readouterr().err == ""


def test_roundtrip_preserves_value():
    import pandas as pd

    payload = {"E00001": pd.DataFrame({"week_start": ["2026-01-05"], "close_last": [123.4]})}
    _cache.cached("weekly_prices_close", lambda: payload)
    loaded = _cache.cached("weekly_prices_close", lambda: {})  # producer は呼ばれず前回値を返す
    pd.testing.assert_frame_equal(loaded["E00001"], payload["E00001"])
