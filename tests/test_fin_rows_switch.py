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


class TestFinRowsModel:
    """基準 → VIEW の対応は `fin_rows_model` だけに置く（検証スクリプトと共有する）。"""

    def test_annual_reads_financial_metrics(self):
        model, order = ms.fin_rows_model()
        assert model is database.FinancialMetric
        assert [c.key for c in order] == ["edinet_code", "period_end"]

    def test_with_ttm_reads_the_union_view_in_a_fixed_order(self):
        with ms.use_fin_rows("with_ttm"):
            model, order = ms.fin_rows_model()
        assert model is database.FinancialMetricWithTTM
        assert [c.key for c in order] == ["edinet_code", "period_end", "id"]


class TestBakeoffFinancials:
    """検証スクリプトの財務ロード（`candidate_bakeoff._load_financials`・#424 子3）。

    かつては `FinancialMetric` を直に読み、キャッシュのキーにも基準が無かった。
    `use_fin_rows("with_ttm")` で包んでも**黙って通期だけを測る**——どちらの基準でも妥当な
    パネルができるので例外は出ず、昇格ゲートは「差なし」を返す。
    """

    def test_every_source_has_its_own_cache_key(self):
        from scripts import candidate_bakeoff as cb
        assert set(cb._FIN_CACHE_KEYS) == set(ms.FIN_ROW_SOURCES)
        assert len(set(cb._FIN_CACHE_KEYS.values())) == len(ms.FIN_ROW_SOURCES)
        # 通期のキーは据え置く＝既存のキャッシュもこれまでのゲートの測定条件も変わらない
        assert cb._FIN_CACHE_KEYS["annual"] == "bakeoff_fin_metrics_v2"

    def test_follows_the_switch(self, db, monkeypatch):
        from scripts import candidate_bakeoff as cb
        # 会社マスタは `scripts/.cache` の実 pickle を読みにいく。テストから本物に触らない。
        monkeypatch.setattr(cb, "cached", lambda key, producer: producer())
        seed_both_views(db)
        annual, companies = cb._load_financials(db, use_cache=False)
        with ms.use_fin_rows("with_ttm"):
            ttm, _ = cb._load_financials(db, use_cache=False)
        assert len(annual["E00001"]) == 1
        # period_end 昇順＝`_find_applicable_fin` が「その時点で最新の行」を選べる並び
        assert [r.period_end for r in ttm["E00001"]] == [date(2025, 3, 31), date(2025, 9, 30)]
        assert "E00001" in companies

    def test_cache_key_follows_the_switch(self, db, monkeypatch):
        """キーが1つだと、先に作った基準の pickle がもう一方の要求へ返る。"""
        from scripts import candidate_bakeoff as cb
        keys = []
        monkeypatch.setattr(cb, "cached", lambda key, producer: (keys.append(key), {})[1])
        cb._load_financials(db)
        with ms.use_fin_rows("with_ttm"):
            cb._load_financials(db)
        fin_keys = [k for k in keys if k != cb._CO_CACHE_KEY]
        assert fin_keys == [cb._FIN_CACHE_KEYS["annual"], cb._FIN_CACHE_KEYS["with_ttm"]]

    def test_use_cache_false_bypasses_only_the_financials(self, db, monkeypatch):
        """2つの基準を比べるときは同じ実行で読む（片方だけ古い世代だと鮮度の差が混ざる）。"""
        from scripts import candidate_bakeoff as cb
        seed_both_views(db)
        keys = []
        monkeypatch.setattr(cb, "cached", lambda key, producer: (keys.append(key), producer())[1])
        cb._load_financials(db, use_cache=False)
        assert keys == [cb._CO_CACHE_KEY]
