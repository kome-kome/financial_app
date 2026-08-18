"""tests/test_weekly_price_cache.py — 週次株価の run 間差分ロード（Issue #480・ADR-0036）

夜間バッチは毎晩 1,282,436 行（39.3MB）を引き直していた。増分は約4,400行＝転送の 99.7% が
不変データの再送で、月 1.98GB＝無料枠 5GB の 40%。

ここで固定するのは「差分で引けること」ではなく、**差分が使えない状況で必ずフルロードへ倒れる**
ことと、**倒れ損ねたときに黙らない**こと。キャッシュは速さだけを担い、正しさは指紋・世代印・
行数照合の3つが持つ——この分担が崩れると stale なパネルで μ̂ を生成しても failure が出ない
（ADR-0031/0034 が繰り返し警戒している形）。
"""
import pickle
from datetime import date, timedelta

import pytest

import weekly_price_cache as wpc
from database import (
    DAILY_WINDOW_DAYS, WEEKLY_OVERLAP_DAYS, WEEKLY_OVERLAP_WEEKS,
    Company, StockPriceWeekly, iso_week_start,
)
from plugins.macro_snapshots import _VOLUME_NOT_LOADED, load_weekly_prices_chunked

# seed の最終週。today に依存させない（refresh_boundary は today の月曜で頭打ちにするので、
# 過去日で固定しても anchor は max_week_start 側が選ばれる）。
LAST_MONDAY = "2026-08-10"
N_WEEKS = 40
N_COMPANIES = 3


@pytest.fixture
def cache_on(tmp_path, monkeypatch):
    """conftest の autouse（既定 OFF）を打ち消してキャッシュを有効にする。"""
    d = tmp_path / "wc"
    monkeypatch.setenv("FINAPP_WEEKLY_CACHE", "1")
    monkeypatch.setenv("FINAPP_WEEKLY_CACHE_DIR", str(d))
    wpc._stats.update(hits=0, misses=0, fetched_rows=0)
    return d


def _mondays(n=N_WEEKS, last=LAST_MONDAY):
    end = date.fromisoformat(last)
    return [(end - timedelta(days=7 * i)).isoformat() for i in range(n - 1, -1, -1)]


def _friday(monday: str) -> str:
    """週内最終営業日。**week_start と trade_date をずらしておくことが重要**——差分ロードは
    「DB は week_start で切り、キャッシュは trade_date で切る」ので、両者が同じ値だと
    ISO 週の不変条件（week_start <= trade_date <= week_start+6）が効いているかを検証できない。
    """
    return (date.fromisoformat(monday) + timedelta(days=4)).isoformat()


def _seed(db, companies=N_COMPANIES, weeks=N_WEEKS):
    for c in range(companies):
        ec = f"E0000{c + 1}"
        db.add(Company(edinet_code=ec, name=f"テスト{c + 1}", sec_code=f"100{c + 1}"))
        for i, ws in enumerate(_mondays(weeks)):
            db.add(StockPriceWeekly(
                edinet_code=ec, week_start=ws, trade_date=_friday(ws),
                close_last=100.0 + i + c * 1000, volume_sum=1000.0 + i, n_days=5))
    db.commit()


def _since(db):
    return wpc.refresh_boundary(wpc.fingerprint(db).max_week_start)


def _full(db, with_volume=False):
    """キャッシュを介さない素のフルロード（比較の基準）。"""
    import os
    prev = os.environ.get("FINAPP_WEEKLY_CACHE")
    os.environ["FINAPP_WEEKLY_CACHE"] = "0"
    try:
        return load_weekly_prices_chunked(db, with_volume=with_volume)
    finally:
        if prev is None:
            os.environ.pop("FINAPP_WEEKLY_CACHE", None)
        else:
            os.environ["FINAPP_WEEKLY_CACHE"] = prev


class TestDerivedConstants:
    """27週は `DAILY_WINDOW_DAYS` からの導出であって直書きではない（ADR-0035 §5）。"""

    def test_overlap_covers_the_daily_rewrite_window(self):
        assert WEEKLY_OVERLAP_DAYS >= DAILY_WINDOW_DAYS
        assert WEEKLY_OVERLAP_DAYS % 7 == 0
        assert WEEKLY_OVERLAP_WEEKS * 7 == WEEKLY_OVERLAP_DAYS

    def test_eight_weeks_would_not_be_enough(self):
        """当初案「末尾8週」（56日）では 183 日の遡及上書きを取り落とす。"""
        assert WEEKLY_OVERLAP_DAYS > 8 * 7

    def test_mirror_and_cache_share_one_derivation(self):
        """ミラー同期と週次キャッシュが**同一オブジェクト**を見ていること。

        値の一致では足りない。別々のリテラルが偶然一致しても通ってしまい、片方だけ
        スラックを足す変更で黙って乖離する。
        """
        import scripts.mirror_common as mc
        assert mc.WEEKLY_OVERLAP_DAYS is WEEKLY_OVERLAP_DAYS
        assert mc.SYNC_PLAN["stock_price_weekly"].overlap_days is WEEKLY_OVERLAP_DAYS


class TestColdLoad:
    def test_cold_load_matches_full_load_and_writes_a_file(self, db, cache_on):
        _seed(db)
        expected = _full(db)
        got = load_weekly_prices_chunked(db, with_volume=False)
        assert got == expected
        assert wpc.cache_path(False).exists()
        assert wpc._stats["misses"] == 1

    def test_second_call_is_a_hit_and_still_bit_identical(self, db, cache_on):
        _seed(db)
        expected = _full(db)
        load_weekly_prices_chunked(db, with_volume=False)
        got = load_weekly_prices_chunked(db, with_volume=False)
        assert got == expected
        assert wpc._stats["hits"] == 1

    def test_third_call_stays_identical(self, db, cache_on):
        """2回目で書き戻したキャッシュを3回目が読む（自家中毒しない）。"""
        _seed(db)
        expected = _full(db)
        for _ in range(3):
            got = load_weekly_prices_chunked(db, with_volume=False)
        assert got == expected

    def test_empty_db_falls_back_to_full_load(self, db, cache_on):
        assert load_weekly_prices_chunked(db, with_volume=False) == {}


class TestOverlapWindow:
    def test_correction_inside_the_window_is_picked_up(self, db, cache_on):
        """27週窓の**中**の訂正は、指紋が変わらなくても差分で拾える。"""
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)

        since = _since(db)
        target = (db.query(StockPriceWeekly)
                  .filter(StockPriceWeekly.edinet_code == "E00001",
                          StockPriceWeekly.week_start >= since)
                  .order_by(StockPriceWeekly.week_start).first())
        target.close_last = 999999.0
        db.commit()

        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        assert 999999.0 in [r.close_last for r in rows]
        assert rows == _full(db)["E00001"]

    def test_correction_outside_the_window_is_missed_without_a_bump(self, db, cache_on):
        """**この設計の限界を明示的に固定する。**

        27週より前の値だけの訂正は、行数も max(week_start) も変えないので指紋では見えない。
        だから書き手（repair / backfill / 深い再集約）が世代印を進める責務を負う。
        """
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)

        since = _since(db)
        target = (db.query(StockPriceWeekly)
                  .filter(StockPriceWeekly.edinet_code == "E00001",
                          StockPriceWeekly.week_start < since)
                  .order_by(StockPriceWeekly.week_start).first())
        assert target is not None, "seed が短すぎて窓外の週が無い"
        target.close_last = 999999.0
        db.commit()

        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        assert 999999.0 not in [r.close_last for r in rows]   # 取り落とす（既知の限界）

        # 世代印を進めれば次のロードで直る
        wpc.bump_generation(db, "test")
        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        assert 999999.0 in [r.close_last for r in rows]

    def test_boundary_week_is_neither_duplicated_nor_dropped(self, db, cache_on):
        """since ちょうどの週が1回だけ現れること（マージの境界条件）。"""
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        since = _since(db)

        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        dates = [r.trade_date for r in rows]
        assert dates == sorted(dates)                 # 昇順の契約
        assert len(dates) == len(set(dates))          # 重複なし
        # since は月曜、trade_date は金曜。DB 側の `week_start >= since` と
        # キャッシュ側の `trade_date < since` が同じ週で切れていること＝境界週が1回だけ残る。
        assert _friday(since) in dates                # 欠落なし
        prev_week = (date.fromisoformat(since) - timedelta(days=7)).isoformat()
        assert _friday(prev_week) in dates            # 直前の週も落ちていない
        assert dates == [r.trade_date for r in _full(db)["E00001"]]


class TestGenerationToken:
    def test_bump_forces_a_full_load(self, db, cache_on):
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        assert wpc._stats["misses"] == 1

        wpc.bump_generation(db, "repair-price-breaks: 3 companies")
        load_weekly_prices_chunked(db, with_volume=False)
        assert wpc._stats["misses"] == 2, "世代印が変わったらフルロードへ倒れること"

    def test_bump_is_persisted_in_the_database_not_on_disk(self, db, cache_on):
        """印は DB に置く。修復 CLI はローカルで走りキャッシュは GHA ランナーに載るため。"""
        from database import get_setting
        token = wpc.bump_generation(db, "test")
        assert get_setting(db, wpc.GENERATION_KEY) == token

    def test_bump_failure_does_not_break_collection(self, db, cache_on, monkeypatch):
        """印の書き込み失敗で収集そのものを落とさない（ただし黙らない）。"""
        import database
        monkeypatch.setattr(database, "upsert_setting",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert wpc.bump_generation_safely(db, "test") is None


class TestRowCountReconciliation:
    def test_backfilled_old_rows_trigger_a_full_load(self, db, cache_on):
        """窓の外に行が増える（backfill 相当）と行数照合で不一致になり全再ロードされる。"""
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)

        oldest = _mondays()[0]
        db.add(StockPriceWeekly(
            edinet_code="E00001",
            week_start=(date.fromisoformat(oldest) - timedelta(days=7)).isoformat(),
            trade_date=(date.fromisoformat(oldest) - timedelta(days=7)).isoformat(),
            close_last=42.0, volume_sum=1.0, n_days=5))
        db.commit()

        got = load_weekly_prices_chunked(db, with_volume=False)
        assert wpc._stats["misses"] == 2
        assert got == _full(db)
        assert 42.0 in [r.close_last for r in got["E00001"]]

    def test_orphan_rows_do_not_cause_a_full_load_every_night(self, db, cache_on):
        """companies に居ない社の週次行（孤立行）があっても2回目は HIT になること。

        ローダーは `edinet_code IN (companies)` で引くので孤立行は取れず、count(*) と
        恒常的にずれる。これをオフセットとして学習しないと毎晩フルロードへ退化し、
        Issue の目的そのものが消える。
        """
        _seed(db)
        db.add(StockPriceWeekly(edinet_code="E09999", week_start=LAST_MONDAY,
                                trade_date=LAST_MONDAY, close_last=1.0, n_days=5))
        db.commit()

        load_weekly_prices_chunked(db, with_volume=False)
        assert wpc._stats["misses"] == 1
        got = load_weekly_prices_chunked(db, with_volume=False)
        assert wpc._stats["hits"] == 1, "孤立行のぶんでフルロードへ退化している"
        assert got == _full(db)

    def test_deleted_rows_trigger_a_full_load(self, db, cache_on):
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        (db.query(StockPriceWeekly)
           .filter(StockPriceWeekly.edinet_code == "E00002").delete())
        db.commit()
        got = load_weekly_prices_chunked(db, with_volume=False)
        assert got == _full(db)
        assert "E00002" not in got


class TestPeriodicRefreshAndDrift:
    def _age_the_cache(self, with_volume=False, days=99):
        p = wpc.cache_path(with_volume)
        blob = pickle.loads(p.read_bytes())
        blob["header"]["full_loaded_at"] = (date.today() - timedelta(days=days)).isoformat()
        p.write_bytes(pickle.dumps(blob))

    def test_stale_cache_is_refreshed_even_if_the_fingerprint_matches(self, db, cache_on):
        """未検知の乖離が生き延びる期間を上限で打ち切る（既定7日）。"""
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        self._age_the_cache()
        load_weekly_prices_chunked(db, with_volume=False)
        assert wpc._stats["misses"] == 2

    def test_drift_in_the_untouched_past_is_raised_not_swallowed(self, db, cache_on):
        """世代印を進めずに窓外を書き換えた場合、週1回のコールドで**例外になる**。

        GHA では failure ＝ notify-failure.yml が Issue を自動起票する。ここを警告に
        するとフック漏れが永久に見えなくなる（#480 の最終防衛線）。
        """
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)

        since = _since(db)
        target = (db.query(StockPriceWeekly)
                  .filter(StockPriceWeekly.edinet_code == "E00001",
                          StockPriceWeekly.week_start < since)
                  .order_by(StockPriceWeekly.week_start).first())
        target.close_last = 555555.0
        db.commit()
        self._age_the_cache()

        with pytest.raises(wpc.WeeklyCacheDrift):
            load_weekly_prices_chunked(db, with_volume=False)


class TestStaleGuard:
    def test_missing_latest_week_raises(self, db, cache_on):
        """差分が最新週を取れていないのに結果を返してはいけない。"""
        _seed(db)
        fp = wpc.fingerprint(db)
        stale = {"E00001": [("2020-01-06", 1.0)]}

        with pytest.raises(wpc.WeeklyCacheStale):
            wpc.load_incremental(
                db, with_volume=False,
                fetch=lambda since: {k: list(v) for k, v in stale.items()},
                to_wire=lambda r: r, from_wire=lambda t: t,
                trade_date_of=lambda r: r[0])
        assert fp.max_week_start == LAST_MONDAY


class TestVolumeSentinel:
    def test_sentinel_identity_survives_the_pickle_round_trip(self, db, cache_on):
        """番兵は `object()`。namedtuple ごと pickle すると `is` 判定が壊れ、px_volz が
        例外ではなく全 nan になる（#438/#446 が潰した静かな故障の再来）。"""
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        assert all(r.volume_sum is _VOLUME_NOT_LOADED for r in rows)

    def test_px_volz_still_fails_fast_after_a_cache_hit(self, db, cache_on):
        from plugins.macro_snapshots import build_price_features
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        with pytest.raises(ValueError):
            build_price_features(rows, ["px_volz"])

    def test_with_volume_true_round_trips_actual_volume(self, db, cache_on):
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=True)
        rows = load_weekly_prices_chunked(db, with_volume=True)["E00001"]
        assert [r.volume_sum for r in rows] == [r.volume_sum for r in _full(db, True)["E00001"]]

    def test_with_volume_variants_use_separate_files(self, db, cache_on):
        """False の結果を True の要求へ流用しない（#446 のキャッシュキー設計のプロセス外版）。"""
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        assert wpc.cache_path(False).exists()
        assert not wpc.cache_path(True).exists()

        got = load_weekly_prices_chunked(db, with_volume=True)
        assert all(r.volume_sum is not _VOLUME_NOT_LOADED for r in got["E00001"])


class TestResilience:
    def test_corrupt_cache_degrades_to_a_full_load(self, db, cache_on):
        _seed(db)
        load_weekly_prices_chunked(db, with_volume=False)
        wpc.cache_path(False).write_bytes(b"not a pickle at all")
        assert load_weekly_prices_chunked(db, with_volume=False) == _full(db)

    def test_save_failure_does_not_break_the_load(self, db, cache_on, monkeypatch):
        """保存に失敗しても結果は返す（次回 MISS になるだけ）。

        キャッシュは速さだけを担い、正しさには一切関与しない——この不変条件をここで固定する。
        """
        _seed(db)
        monkeypatch.setattr(wpc.pickle, "dump",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        assert load_weekly_prices_chunked(db, with_volume=False) == _full(db)
        assert not wpc.cache_path(False).exists()


class TestDisabledSwitch:
    def test_enabled_by_default(self, tmp_path, monkeypatch):
        """テストの既定 OFF（conftest）が本番の既定と混同されないよう固定する。"""
        monkeypatch.delenv("FINAPP_WEEKLY_CACHE", raising=False)
        assert wpc.enabled() is True

    def test_disabled_does_not_even_fingerprint(self, db, cache_on, monkeypatch):
        """緊急停止したら1文も変わらないこと（指紋クエリすら発行しない）。"""
        monkeypatch.setenv("FINAPP_WEEKLY_CACHE", "0")
        monkeypatch.setattr(wpc, "fingerprint",
                            lambda db: pytest.fail("無効化したのに指紋クエリが飛んでいる"))
        _seed(db)
        assert load_weekly_prices_chunked(db, with_volume=False) == _full(db)
        assert not wpc.cache_path(False).exists()


class TestRefreshBoundary:
    @pytest.mark.parametrize("offset", range(7))
    def test_boundary_is_always_a_monday(self, offset):
        """ISO 週の不変条件（week_start <= trade_date <= week_start+6）に依存するので、
        境界が月曜でないと `week_start >= since` と `trade_date >= since` が食い違う。"""
        d = (date.fromisoformat("2026-08-10") + timedelta(days=offset)).isoformat()
        since = wpc.refresh_boundary(iso_week_start(d))
        assert date.fromisoformat(since).weekday() == 0

    def test_future_watermark_is_clamped_to_this_week(self):
        """未来日のデータが入っても窓が未来へずれない（過去の訂正を取り落とさない）。"""
        far = (date.today() + timedelta(days=400)).isoformat()
        since = wpc.refresh_boundary(iso_week_start(far))
        assert since <= (date.today() - timedelta(days=DAILY_WINDOW_DAYS)).isoformat()
