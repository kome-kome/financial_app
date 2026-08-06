"""tests/test_macro_snapshots_loaders.py — 週次株価/マクロのローダーと Egress 列絞り（Issue #446）

夜間バッチ（`nightly-scores.yml`）は Supabase 無料枠 5GB/月に対し 1回 86MB を引いていた
（2026-08-06 実測・内訳は docs/DEPLOYMENT.md）。うち `stock_price_weekly.volume_sum` の
12.1MB と `macro_data` の ORM 全列ぶん 6.1MB は、消費側が読まない列だった。

ここで固定するのは「引かない」ことそのものではなく、**引かなかったときに壊れ方が黙らない**
こと。`volume_sum` を落とした行で `px_volz` を計算すると全 nan になり、データが薄いのか
ロードを間違えたのかを後から区別できない（#438 の「静かな固定」と同型）ため、番兵で即時
例外にしている。
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from database import Company, MacroData, StockPriceWeekly
from plugins.macro_snapshots import (
    _VOLUME_NOT_LOADED,
    build_price_features,
    load_data,
    load_weekly_prices_chunked,
    preload_macro,
    shared_snapshot_cache,
)


def _seed(db):
    db.add(Company(edinet_code="E00001", name="テスト", sec_code="1001"))
    for i, (d, close, vol) in enumerate([
        ("2026-01-05", 100.0, 1000),
        ("2026-01-12", 101.0, 2000),
        ("2026-01-19", 102.0, 3000),
    ]):
        db.add(StockPriceWeekly(edinet_code="E00001", week_start=d, trade_date=d,
                                close_last=close, volume_sum=vol))
    db.commit()


class TestWeeklyVolumeColumnNarrowing:
    def test_with_volume_true_loads_actual_volume(self, db):
        _seed(db)
        rows = load_weekly_prices_chunked(db, with_volume=True)["E00001"]
        assert [r.volume_sum for r in rows] == [1000, 2000, 3000]

    def test_with_volume_false_marks_rows_not_loaded(self, db):
        """未ロードは None（＝欠測）ではなく番兵。close_last と順序は変わらない。"""
        _seed(db)
        rows = load_weekly_prices_chunked(db, with_volume=False)["E00001"]
        assert [r.trade_date for r in rows] == ["2026-01-05", "2026-01-12", "2026-01-19"]
        assert [r.close_last for r in rows] == [100.0, 101.0, 102.0]
        assert all(r.volume_sum is _VOLUME_NOT_LOADED for r in rows)

    def test_default_keeps_volume_for_m3(self, db):
        """M-3（`load_prices`）は既定 True のまま呼ぶ＝後方互換。"""
        _seed(db)
        rows = load_weekly_prices_chunked(db)["E00001"]
        assert rows[0].volume_sum == 1000


class TestPxVolzFailsFastWithoutVolume:
    @staticmethod
    def _rows(volume):
        return [SimpleNamespace(trade_date=f"2026-01-{i + 1:02d}", close_last=100.0 + i,
                                volume_sum=volume) for i in range(20)]

    def test_px_volz_raises_when_volume_not_loaded(self):
        with pytest.raises(ValueError, match="px_volz"):
            build_price_features(self._rows(_VOLUME_NOT_LOADED), ["px_volz"])

    def test_other_px_features_work_without_volume(self):
        """出来高を読まない特徴量は列を落としても計算できる（巻き添えにしない）。"""
        out = build_price_features(self._rows(_VOLUME_NOT_LOADED), ["px_rvol", "px_rev4w"])
        assert set(out) == {"px_rvol", "px_rev4w"}

    def test_missing_volume_is_still_nan_not_error(self):
        """本物の欠測（None）は従来どおり nan 扱い＝番兵と混同しない。"""
        out = build_price_features(self._rows(None), ["px_volz"])
        assert out["px_volz"] and all(v != v for v in out["px_volz"])  # 全 nan


class TestLoadDataCacheKeyIncludesWithVolume:
    def test_different_with_volume_does_not_reuse_cache(self, db):
        """False でロードした結果を True の要求へ流用しない（px_volz が壊れるため）。"""
        _seed(db)
        with patch("plugins.macro_snapshots._load_data_impl",
                   side_effect=lambda _db, wv=True: ({"wv": wv}, {}, {})) as impl:
            with shared_snapshot_cache():
                a = load_data(db, with_volume=False)
                b = load_data(db, with_volume=False)   # 同キー＝再クエリしない
                c = load_data(db, with_volume=True)
        assert impl.call_count == 2
        assert a[0] == b[0] == {"wv": False}
        assert c[0] == {"wv": True}


class TestPreloadMacroColumnNarrowing:
    def test_returns_series_date_close_mapping(self, db):
        _seed(db)
        for d, close in [("2026-01-05", 1.5), ("2026-01-12", 1.6)]:
            db.add(MacroData(series_code="US10Y", series_name="米10年金利",
                             category="rate", trade_date=d, close=close))
        # 別系列は選択された macro_names に含まれないので戻り値に出ない。
        db.add(MacroData(series_code="VIX", series_name="VIX", category="volatility",
                         trade_date="2026-01-05", close=20.0))
        db.commit()
        prices_by_co = load_weekly_prices_chunked(db, with_volume=False)
        out = preload_macro(db, prices_by_co, ["macro_us10y_zscore"])
        assert out == {"US10Y": {"2026-01-05": 1.5, "2026-01-12": 1.6}}


class TestCallersPassTheRightFlag:
    """既定パラメータでの呼び出しが volume を引かないこと（Egress 削減の実効性）。"""

    @staticmethod
    def _with_volume_of(module: str, execute) -> bool:
        seen = {}

        def _fake_load_data(_db, with_volume=True):
            seen["wv"] = with_volume
            raise RuntimeError("stop-after-load")   # ロード直後で止める

        with patch(f"{module}.load_data", side_effect=_fake_load_data):
            with pytest.raises(RuntimeError, match="stop-after-load"):
                execute()
        return seen["wv"]

    def test_m1_never_loads_volume(self, db):
        from plugins.macro_risk_return import MacroRiskReturnPlugin as P
        p = P()
        from plugins.utils import coerce_params
        params = coerce_params(p.params_schema(), {})
        assert self._with_volume_of(
            "plugins.macro_risk_return", lambda: p.execute(params, db)) is False

    def test_m6_default_does_not_load_volume(self, db):
        from plugins.macro_enet import MacroEnetPlugin as P
        from plugins.utils import coerce_params
        p = P()
        params = coerce_params(p.params_schema(), {})
        assert self._with_volume_of(
            "plugins.macro_enet", lambda: p.execute(params, db)) is False

    def test_m6_loads_volume_when_px_volz_selected(self, db):
        from plugins.macro_enet import MacroEnetPlugin as P
        from plugins.utils import coerce_params
        p = P()
        params = coerce_params(p.params_schema(), {"price_features": ["px_volz"]})
        assert self._with_volume_of(
            "plugins.macro_enet", lambda: p.execute(params, db)) is True
