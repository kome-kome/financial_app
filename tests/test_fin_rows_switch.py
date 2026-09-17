"""学習パネルが読む行の基準の切替（#424 子2・ADR-0051 決定3）。

**既定は通期**で、切替を足しても画面・推薦・夜間スコアの結果は変わらない。危ないのは
「TTM で回した結果が本番の表へ入る」ことと「キャッシュが前の基準のパネルを返す」ことで、
どちらも値としてはもっともらしいのでエラーにならない。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database  # noqa: E402
from plugins import macro_snapshots as ms  # noqa: E402


def seed_both_views(db):
    """通期の VIEW と「通期＋TTM」の VIEW に、見分けのつく値を入れる。

    SQLite では VIEW を作れないので、conftest が ORM の列から実テーブルを作っている。
    """
    db.add(database.FinancialMetric(edinet_code="E00001", year=2025,
                                    period_end=date(2025, 3, 31), per=10.0))
    db.add(database.FinancialMetricWithTTM(id=1, edinet_code="E00001", year=2025,
                                           period_end=date(2025, 3, 31), per=10.0,
                                           basis="annual"))
    db.add(database.FinancialMetricWithTTM(id=-2, edinet_code="E00001", year=2026,
                                           period_end=date(2025, 9, 30), per=11.0,
                                           basis="ttm"))
    db.add(database.Company(edinet_code="E00001", name="テスト"))
    db.commit()


class TestSwitch:
    def test_default_is_annual(self):
        assert ms.current_fin_rows() == "annual"

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError):
            with ms.use_fin_rows("ttm_only"):
                pass

    def test_switch_is_restored_on_exit(self):
        with ms.use_fin_rows("with_ttm"):
            assert ms.current_fin_rows() == "with_ttm"
        assert ms.current_fin_rows() == "annual"

    def test_with_ttm_blocks_producer_persistence(self, db):
        """TTM で回した μ̂ が本番の表へ入ると、売りランキングが測定用の断面を読む。"""
        rows = [{"edinet_code": "E00001", "mu": 0.1}]
        with ms.use_fin_rows("with_ttm"):
            assert database.replace_macro_enet_scores(db, rows) == 0
        assert database.replace_macro_enet_scores(db, rows) == 1

    def test_factor_premia_persist_refuses_under_ttm(self, db):
        import recommend_factor_premia as rfp
        with ms.use_fin_rows("with_ttm"):
            with pytest.raises(RuntimeError):
                rfp.persist(db, object())

    def test_changing_inside_the_shared_cache_is_refused(self):
        """断面キャッシュ（上限1）と M-1 の CV キャッシュが前の基準の結果を返しうる。"""
        with ms.shared_snapshot_cache():
            with pytest.raises(RuntimeError):
                with ms.use_fin_rows("with_ttm"):
                    pass

    def test_same_value_inside_the_cache_is_fine(self):
        with ms.shared_snapshot_cache():
            with ms.use_fin_rows("annual"):
                assert ms.current_fin_rows() == "annual"

    def test_switch_outside_then_cache_inside_is_fine(self):
        with ms.use_fin_rows("with_ttm"):
            with ms.shared_snapshot_cache():
                assert ms.current_fin_rows() == "with_ttm"


class TestLoadData:
    def test_annual_reads_financial_metrics(self, db, monkeypatch):
        seed_both_views(db)
        monkeypatch.setattr(ms, "load_weekly_prices_chunked", lambda *a, **k: {})
        _, fin_by_co, _ = ms._load_data_impl(db, with_volume=False)
        assert [r.per for r in fin_by_co["E00001"]] == [10.0]

    def test_with_ttm_reads_the_union_view(self, db, monkeypatch):
        seed_both_views(db)
        monkeypatch.setattr(ms, "load_weekly_prices_chunked", lambda *a, **k: {})
        with ms.use_fin_rows("with_ttm"):
            _, fin_by_co, _ = ms._load_data_impl(db, with_volume=False)
        # period_end 昇順＝`_find_applicable_fin` が「その時点で最新の行」を選べる並び。
        assert [r.per for r in fin_by_co["E00001"]] == [10.0, 11.0]

    def test_cache_key_separates_the_two_sources(self, db, monkeypatch):
        """同じセッションで両方を読んだとき、先に読んだ側が返ってはいけない。"""
        seed_both_views(db)
        monkeypatch.setattr(ms, "load_weekly_prices_chunked", lambda *a, **k: {})
        with ms.shared_snapshot_cache():
            _, annual, _ = ms.load_data(db, with_volume=False)
            assert len(annual["E00001"]) == 1
        with ms.use_fin_rows("with_ttm"), ms.shared_snapshot_cache():
            _, ttm, _ = ms.load_data(db, with_volume=False)
            assert len(ttm["E00001"]) == 2
        cache = {"load_data": ms._BoundedCache(4)}
        token = ms._shared_cache.set(cache)
        try:
            ms.load_data(db, with_volume=False)
            with ms.use_fin_rows("annual"):
                ms.load_data(db, with_volume=False)
            assert list(cache["load_data"]._data)[0][2] == "annual"
        finally:
            ms._shared_cache.reset(token)
