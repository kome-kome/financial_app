"""M-7 の入力系列（`plugins/weekly_sequences.py`）のテスト。numpy だけで回る（jax 不要）。

守ること:
- 月末より後の値（全社・他社とも）を書き換えても、その月末の窓はビット一致する（リークしない）
- 上場 52 週未満の社も行を落とさず、空欄の印（obs=0）で埋める（母集団をそろえる・ADR-0045）
- 月末の位置は build_snapshots と同じ（ラベルの終値の位置で確かめる）
- 出来高を読まずに作った行・2 回の読み込みの食い違いは黙って通さない
"""
from __future__ import annotations

import datetime as dt
import math
from collections import namedtuple
from types import SimpleNamespace

import numpy as np
import pytest

from plugins import weekly_sequences as ws
from plugins.macro_snapshots import _VOLUME_NOT_LOADED, HORIZON_WEEKS, build_snapshots

PX = namedtuple("PX", "trade_date close_last volume_sum")

START = dt.date(2020, 1, 3)        # 金曜


def _friday(i: int) -> str:
    return (START + dt.timedelta(weeks=i)).isoformat()


def _rows(n_co: int = 40, n_weeks: int = 120, seed: int = 0, late: dict | None = None,
          gaps: dict | None = None) -> dict:
    """{ec: [PX,...]}。late={ec: 開始週}、gaps={ec: 欠ける週の集合}。"""
    rng = np.random.default_rng(seed)
    late = late or {}
    gaps = gaps or {}
    out = {}
    for c in range(n_co):
        ec = f"E{c:05d}"
        price = 100.0 + c
        rows = []
        for i in range(n_weeks):
            price *= math.exp(rng.normal(0.0, 0.03))
            vol = float(rng.integers(1_000, 100_000))
            if i < late.get(ec, 0) or i in gaps.get(ec, ()):
                continue
            rows.append(PX(_friday(i), price, vol))
        out[ec] = rows
    return out


def _mutate_after(rows_by_co: dict, cutoff: str, only: set | None = None) -> dict:
    """cutoff より後の行の終値と出来高を書き換えた写し。"""
    out = {}
    for ec, rows in rows_by_co.items():
        if only is not None and ec not in only:
            out[ec] = list(rows)
            continue
        out[ec] = [PX(r.trade_date, r.close_last * 3.7, r.volume_sum * 11.0)
                   if r.trade_date > cutoff else r for r in rows]
    return out


class TestWeekKey:
    def test_friday_maps_to_monday(self):
        assert ws.week_key("2020-01-03") == "2019-12-30"

    def test_monday_maps_to_itself(self):
        assert ws.week_key("2020-01-06") == "2020-01-06"

    def test_accepts_datetime_like_prefix(self):
        assert ws.week_key("2020-01-08T00:00:00") == "2020-01-06"


class TestNoLeak:
    def test_future_values_of_all_companies_do_not_change_window(self):
        rows = _rows()
        snap = _friday(80)
        codes = sorted(rows)
        a = ws.gather_windows(ws.build_weekly_store(rows), codes, [snap] * len(codes))
        mutated = _mutate_after(rows, snap)
        b = ws.gather_windows(ws.build_weekly_store(mutated), codes, [snap] * len(codes))
        assert np.array_equal(a, b)
        # 書き換えは効いている（空振りしていない）: 後の週の窓は変わる
        later = _friday(100)
        c = ws.gather_windows(ws.build_weekly_store(rows), codes, [later] * len(codes))
        d = ws.gather_windows(ws.build_weekly_store(mutated), codes, [later] * len(codes))
        assert not np.array_equal(c, d)

    def test_other_company_future_does_not_change_window(self):
        rows = _rows()
        snap = _friday(70)
        target = ["E00000"]
        a = ws.gather_windows(ws.build_weekly_store(rows), target, [snap])
        mutated = _mutate_after(rows, snap, only={"E00001", "E00002"})
        b = ws.gather_windows(ws.build_weekly_store(mutated), target, [snap])
        assert np.array_equal(a, b)

    def test_window_ends_at_the_snap_week(self):
        rows = _rows()
        store = ws.build_weekly_store(rows)
        snap = _friday(60)
        w = ws.gather_windows(store, ["E00003"], [snap])[0]
        # 末尾の週＝snap の週。その週の値は store の該当週と同じ
        k = store.week_index[ws.week_key(snap)]
        assert np.array_equal(w[-1], store.padded[store.code_index["E00003"], k + ws.SEQ_WEEKS - 1])


class TestChannels:
    def test_shapes_and_dtype(self):
        rows = _rows(n_co=35, n_weeks=70)
        store = ws.build_weekly_store(rows)
        assert store.padded.dtype == np.float32
        assert store.padded.shape == (35, ws.SEQ_WEEKS - 1 + 70, ws.N_CHANNELS)

    def test_return_channel_is_cross_sectionally_standardized(self):
        rows = _rows(n_co=50, n_weeks=60)
        store = ws.build_weekly_store(rows)
        col = store.padded[:, ws.SEQ_WEEKS - 1 + 30, 0]
        assert abs(float(col.mean())) < 1e-5
        assert np.all(np.abs(store.padded[:, :, 0]) <= ws.Z_CLIP)

    def test_first_week_has_no_return(self):
        rows = _rows(n_co=40, n_weeks=20)
        store = ws.build_weekly_store(rows)
        first = store.padded[:, ws.SEQ_WEEKS - 1, ws.OBS_CHANNEL]
        assert float(first.sum()) == 0.0

    def test_gap_week_breaks_the_return(self):
        rows = _rows(n_co=40, n_weeks=40, gaps={"E00007": {20}})
        store = ws.build_weekly_store(rows)
        ci = store.code_index["E00007"]
        base = ws.SEQ_WEEKS - 1
        assert store.padded[ci, base + 20, ws.OBS_CHANNEL] == 0.0   # 欠けた週
        assert store.padded[ci, base + 21, ws.OBS_CHANNEL] == 0.0   # 前週が欠けた週
        assert store.padded[ci, base + 22, ws.OBS_CHANNEL] == 1.0

    def test_thin_cross_section_weeks_are_masked(self):
        rows = _rows(n_co=ws.MIN_CROSS_SECTION - 1, n_weeks=30)
        store = ws.build_weekly_store(rows)
        assert store.n_masked_weeks == store.n_weeks
        assert float(store.padded[:, :, ws.OBS_CHANNEL].sum()) == 0.0
        assert float(np.abs(store.padded[:, :, 0]).sum()) == 0.0

    def test_volume_needs_history(self):
        rows = _rows(n_co=40, n_weeks=30)
        store = ws.build_weekly_store(rows)
        base = ws.SEQ_WEEKS - 1
        # 出来高の基準は直前 VOL_MIN_OBS 週が要る＝それより前の週の vol_z は 0
        assert float(np.abs(store.padded[:, base:base + ws.VOL_MIN_OBS, 1]).sum()) == 0.0
        assert float(np.abs(store.padded[:, base + ws.VOL_MIN_OBS + 1, 1]).sum()) > 0.0

    def test_volume_not_loaded_raises(self):
        rows = {f"E{c:05d}": [PX(_friday(i), 100.0, _VOLUME_NOT_LOADED) for i in range(5)]
                for c in range(3)}
        with pytest.raises(ValueError, match="with_volume=False"):
            ws.build_weekly_store(rows)


class TestYoungCompanies:
    def test_rows_are_kept_and_padded(self):
        rows = _rows(late={"E00009": 70})
        store = ws.build_weekly_store(rows)
        snap = _friday(80)                     # 上場 10 週後
        w = ws.gather_windows(store, ["E00009"], [snap])[0]
        obs = w[:, ws.OBS_CHANNEL]
        assert obs[: ws.SEQ_WEEKS - 10].sum() == 0.0
        assert 0 < obs.sum() <= 10

    def test_window_before_store_start_is_zero_padded(self):
        rows = _rows(n_weeks=30)
        store = ws.build_weekly_store(rows)
        w = ws.gather_windows(store, ["E00001"], [_friday(5)])[0]
        assert float(np.abs(w[: ws.SEQ_WEEKS - 6]).sum()) == 0.0


class TestGather:
    def test_unknown_code_raises(self):
        store = ws.build_weekly_store(_rows(n_weeks=10))
        with pytest.raises(ValueError, match="無い社"):
            ws.gather_windows(store, ["NOPE"], [_friday(3)])

    def test_unknown_week_raises(self):
        store = ws.build_weekly_store(_rows(n_weeks=10))
        with pytest.raises(ValueError, match="無い週"):
            ws.gather_windows(store, ["E00000"], [_friday(50)])

    def test_length_mismatch_raises(self):
        store = ws.build_weekly_store(_rows(n_weeks=10))
        with pytest.raises(ValueError):
            ws.gather_windows(store, ["E00000"], [])

    def test_empty(self):
        store = ws.build_weekly_store(_rows(n_weeks=10))
        assert ws.gather_windows(store, [], []).shape == (0, ws.SEQ_WEEKS, ws.N_CHANNELS)


class TestPatches:
    def test_shapes_and_mask(self):
        w = np.zeros((2, ws.SEQ_WEEKS, ws.N_CHANNELS), dtype=np.float32)
        w[0, -1, ws.OBS_CHANNEL] = 1.0         # 最後のパッチだけ観測あり
        w[0, -1, 0] = 2.5
        p, m = ws.to_patches(w)
        assert p.shape == (2, ws.N_PATCHES, ws.PATCH_DIM)
        assert m.shape == (2, ws.N_PATCHES)
        assert m[0].tolist() == [False] * (ws.N_PATCHES - 1) + [True]
        assert not m[1].any()
        # パッチ内の並びは (週, チャネル) の順: 最後の週の ret_z は最後のパッチの末尾から 3 番目
        assert p[0, -1, ws.PATCH_DIM - ws.N_CHANNELS] == 2.5


def _fin_rec(ec: str, year: int):
    return SimpleNamespace(
        edinet_code=ec, period_end=f"{year}-03-31", industry="製造業", sec_code="1000",
        company_name=ec, bs_total_assets=1e9, x1=float(year - 2018),
    )


class TestSnapDatesMatchPanel:
    def test_snap_dates_reproduce_build_snapshots_labels(self):
        rows = _rows(n_co=6, n_weeks=150, seed=3, late={"E00002": 20})
        fin = {ec: [_fin_rec(ec, y) for y in (2019, 2020, 2021)] for ec in rows}
        companies = {ec: SimpleNamespace(industry="製造業", sec_code="1000", name=ec) for ec in rows}
        samples, _meta, _cur, _names, sids = build_snapshots(
            rows, fin, companies, {}, ["x1"], [], False, 12, 0.0,
            build_interactions=False, return_stock_ids=True)
        assert samples
        snaps = ws.snap_dates_by_ym(rows, sids)
        n_checked = 0
        for ym, recs in samples.items():
            for (_feat, y), ec, d in zip(recs, sids[ym], snaps[ym]):
                dates = [r.trade_date for r in rows[ec]]
                i = dates.index(d)
                expect = math.log(rows[ec][i + HORIZON_WEEKS].close_last / rows[ec][i].close_last)
                assert y == expect
                assert d[:7] == ym
                n_checked += 1
        assert n_checked > 50

    def test_missing_month_raises(self):
        rows = _rows(n_co=1, n_weeks=10)
        with pytest.raises(ValueError, match="月末の足が無い"):
            ws.snap_dates_by_ym(rows, {"2030-01": ["E00000"]})


class TestCheckSameDates:
    def test_equal_passes(self):
        rows = _rows(n_co=3, n_weeks=10)
        ws.check_same_dates(rows, rows, rows.keys())

    def test_mismatch_raises(self):
        rows = _rows(n_co=3, n_weeks=10)
        other = dict(rows)
        other["E00001"] = rows["E00001"][:-1]
        with pytest.raises(ValueError, match="食い違う"):
            ws.check_same_dates(rows, other, rows.keys())
