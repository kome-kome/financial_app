"""M-7（時系列 Transformer）の入力系列を組み立てる（numpy だけ・jax を読まない）。

何を作るか
----------
各サンプル（社, 月末）について、**月末の週を末尾とする直近 52 週**の週次の値動きを並べる。
月末の位置は `macro_snapshots.month_end_indices`（`build_snapshots` と同じ関数）で決めるので、
パネルの行とその行の系列は必ず同じ週を指す。build_snapshots は変更しない。

週ごとのチャネル（`CHANNELS`）:

- `ret_z`: 週次対数リターン（前週も観測されているときだけ）を、**同じ週の全社の中で**
  p1-p99 winsorize → z → ±`Z_CLIP`。市場全体の上げ下げはここで消える（M-7 は月内の順位だけを
  当てるので、市場の成分は説明できない雑音になる）。有効社が `MIN_CROSS_SECTION` 未満の週は
  観測なし扱い。
- `vol_z`: `log1p(週の出来高)` から**自社の直前 52 週**（観測 `VOL_MIN_OBS` 以上）の平均を引いた外れを、
  同じ週の全社で winsorize → z → ±`Z_CLIP`。欠けは 0。
- `obs`: その週の `ret_z` が観測されていれば 1、無ければ 0（Transformer の mask の元）。

上場 52 週未満の社や系列の先頭より前は 0 埋め（`obs`=0）にし、**行は落とさない**——M-2/M-6 と
比べる母集団を同じにするため（行を落とす側は必ず有利に見える・ADR-0045）。

リークしない理由
----------------
- 週 w の横断統計は**週 w の値だけ**で作る（ラベルも fold も使わない＝特徴量の定義）
- 出来高の基準は週 w より前の 52 週だけ（累積和の差なので未来の値に触れない）
- 窓は月末の週より後を読まない。過去のサンプルの 52 週先ラベルは入力にしない（重なってリークする）
- 既知の極小の漏れ: 同じ暦週で、ある社の週の最終足が木曜（金曜に売買停止）でも、他社の金曜の
  終値がその週の横断の正規化にだけ混ざる（1 日分・MODELS.md §17 に注記）

出来高の読み方
--------------
パネルは M-6 と同じ `load_data(db, with_volume=False)` で作り（キャッシュと母集団を共有する）、
出来高は `load_weekly_prices_chunked(db, with_volume=True)` で別に 1 回だけ読む。2 回の読み込みの
間に週次株価が書き換わると系列とパネルが別の週を見るので、`check_same_dates` で社ごとの日付列が
一致することを確かめ、違えば止める。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import numpy as np

from .macro_snapshots import _VOLUME_NOT_LOADED, month_end_indices
from .utils import winsorize

# ── 定数 ──────────────────────────────────────────────────────────────────
SEQ_WEEKS = 52                     # 窓の長さ（週）
PATCH_WEEKS = 4                    # 1 トークンにまとめる週数（PatchTST 型）
N_PATCHES = SEQ_WEEKS // PATCH_WEEKS          # = 13
CHANNELS = ("ret_z", "vol_z", "obs")
N_CHANNELS = len(CHANNELS)
OBS_CHANNEL = CHANNELS.index("obs")
PATCH_DIM = PATCH_WEEKS * N_CHANNELS          # = 12
MIN_CROSS_SECTION = 30             # 週の横断統計に要る有効社数（未満の週は観測なし扱い）
VOL_LOOKBACK_WEEKS = 52            # 出来高の自社内基準の窓
VOL_MIN_OBS = 8                    # 出来高の基準に要る観測週数
Z_CLIP = 5.0                       # 横断 z の上下限

assert SEQ_WEEKS % PATCH_WEEKS == 0


def week_key(trade_date) -> str:
    """trade_date（'YYYY-MM-DD'）が属する週の月曜（ISO 週・`stock_price_weekly.week_start` と同じ規則）。"""
    d = _dt.date.fromisoformat(str(trade_date)[:10])
    return (d - _dt.timedelta(days=d.weekday())).isoformat()


@dataclass(frozen=True)
class WeeklyStore:
    """全社の週次チャネルを暦の週へ整列した密配列。

    `padded` は先頭に `SEQ_WEEKS - 1` 週の 0（`obs`=0）を足した (社, 週, チャネル) の float32。
    週 k を末尾とする窓は `padded[社, k : k + SEQ_WEEKS]` で取れる。
    """
    week_keys: tuple
    week_index: dict
    code_index: dict
    padded: np.ndarray
    n_masked_weeks: int            # 有効社が MIN_CROSS_SECTION 未満で観測なし扱いにした週の数

    @property
    def n_weeks(self) -> int:
        return len(self.week_keys)


def _cross_section_z(vals: np.ndarray) -> np.ndarray:
    """1 週ぶんの有効値（1 次元・NaN 無し）を p1-p99 winsorize → z → ±Z_CLIP。"""
    clipped, _, _ = winsorize(vals.tolist())
    arr = np.asarray(clipped, dtype=float)
    sd = float(arr.std())
    if not np.isfinite(sd) or sd <= 0.0:
        return np.zeros_like(arr)
    z = (arr - float(arr.mean())) / sd
    return np.clip(z, -Z_CLIP, Z_CLIP)


def build_weekly_store(rows_by_co: dict) -> WeeklyStore:
    """{edinet_code: [_WeeklyPX(trade_date, close_last, volume_sum), ...]}（各社 trade_date 昇順）から作る。

    行は `load_weekly_prices_chunked(db, with_volume=True)` の形。出来高を読まずに作った行
    （番兵 `_VOLUME_NOT_LOADED`）を渡したら ValueError——黙って出来高のチャネルを 0 にしない。
    """
    codes = sorted(rows_by_co)
    wk_memo: dict = {}

    def _wk(td):
        k = wk_memo.get(td)
        if k is None:
            k = week_key(td)
            wk_memo[td] = k
        return k

    weeks = sorted({_wk(r.trade_date) for ec in codes for r in rows_by_co[ec]})
    week_index = {w: i for i, w in enumerate(weeks)}
    n_co, n_w = len(codes), len(weeks)

    close = np.full((n_co, n_w), np.nan)
    logvol = np.full((n_co, n_w), np.nan)
    for ci, ec in enumerate(codes):
        for r in rows_by_co[ec]:
            vs = r.volume_sum
            if vs is _VOLUME_NOT_LOADED:
                raise ValueError(
                    "出来高を読まずに作った週次株価が渡された（with_volume=False）。"
                    "M-7 の系列は load_weekly_prices_chunked(db, with_volume=True) で読む")
            wi = week_index[_wk(r.trade_date)]
            c = r.close_last
            close[ci, wi] = float(c) if c is not None else np.nan
            logvol[ci, wi] = np.log1p(float(vs)) if (vs is not None and vs >= 0) else np.nan

    # 週次対数リターン: 前週も観測されていて終値が正のときだけ
    ret = np.full((n_co, n_w), np.nan)
    if n_w > 1:
        prev, cur = close[:, :-1], close[:, 1:]
        ok = np.isfinite(prev) & np.isfinite(cur) & (prev > 0) & (cur > 0)
        safe_prev = np.where(ok, prev, 1.0)
        safe_cur = np.where(ok, cur, 1.0)
        ret[:, 1:] = np.where(ok, np.log(safe_cur / safe_prev), np.nan)

    # 出来高の自社内基準: 週 w より前の VOL_LOOKBACK_WEEKS 週の平均（累積和の差＝未来に触れない）
    lv_ok = np.isfinite(logvol)
    cs = np.zeros((n_co, n_w + 1))
    cn = np.zeros((n_co, n_w + 1))
    cs[:, 1:] = np.cumsum(np.where(lv_ok, logvol, 0.0), axis=1)
    cn[:, 1:] = np.cumsum(lv_ok, axis=1)
    hi = np.arange(n_w)                       # 窓の終わり（その週を含まない）= CS の添字 w
    lo = np.maximum(0, hi - VOL_LOOKBACK_WEEKS)
    win_sum = cs[:, hi] - cs[:, lo]
    win_n = cn[:, hi] - cn[:, lo]
    base_ok = win_n >= VOL_MIN_OBS
    base = np.where(base_ok, win_sum / np.where(base_ok, win_n, 1.0), np.nan)
    vol_rel = np.where(lv_ok & base_ok, logvol - base, np.nan)

    chans = np.zeros((n_co, n_w, N_CHANNELS), dtype=np.float32)
    n_masked = 0
    for wi in range(n_w):
        r_col = ret[:, wi]
        r_ok = np.isfinite(r_col)
        if int(r_ok.sum()) >= MIN_CROSS_SECTION:
            chans[r_ok, wi, 0] = _cross_section_z(r_col[r_ok])
            chans[r_ok, wi, OBS_CHANNEL] = 1.0
        else:
            n_masked += 1
        v_col = vol_rel[:, wi]
        v_ok = np.isfinite(v_col)
        if int(v_ok.sum()) >= MIN_CROSS_SECTION:
            chans[v_ok, wi, 1] = _cross_section_z(v_col[v_ok])

    padded = np.concatenate(
        [np.zeros((n_co, SEQ_WEEKS - 1, N_CHANNELS), dtype=np.float32), chans], axis=1)
    return WeeklyStore(
        week_keys=tuple(weeks), week_index=week_index,
        code_index={ec: i for i, ec in enumerate(codes)},
        padded=padded, n_masked_weeks=n_masked,
    )


def gather_windows(store: WeeklyStore, codes: list, snap_dates: list) -> np.ndarray:
    """(社, 月末の日付) の並びに対し、月末の週を末尾とする 52 週の窓 (N, SEQ_WEEKS, N_CHANNELS) を返す。"""
    if len(codes) != len(snap_dates):
        raise ValueError(f"社 {len(codes)} 件と日付 {len(snap_dates)} 件の数が合わない")
    if not codes:
        return np.zeros((0, SEQ_WEEKS, N_CHANNELS), dtype=np.float32)
    try:
        co = np.fromiter((store.code_index[c] for c in codes), dtype=np.int64, count=len(codes))
    except KeyError as e:
        raise ValueError(f"系列の週次株価に無い社: {e.args[0]}（2 回の読み込みが食い違う）") from e
    try:
        wk = np.fromiter((store.week_index[week_key(d)] for d in snap_dates),
                         dtype=np.int64, count=len(snap_dates))
    except KeyError as e:
        raise ValueError(f"系列の週次株価に無い週: {e.args[0]}") from e
    idx = wk[:, None] + np.arange(SEQ_WEEKS)[None, :]
    return store.padded[co[:, None], idx]


def to_patches(windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """窓 (N, 52, C) を 4 週ずつのパッチ (N, 13, 4·C) と、パッチ内に観測があるかの mask (N, 13) へ。"""
    n = windows.shape[0]
    patches = np.ascontiguousarray(
        windows.reshape(n, N_PATCHES, PATCH_DIM), dtype=np.float32)
    mask = windows[:, :, OBS_CHANNEL].reshape(n, N_PATCHES, PATCH_WEEKS).max(axis=2) > 0
    return patches, mask


def snap_dates_by_ym(prices_by_co: dict, stock_ids_by_ym: dict) -> dict:
    """`build_snapshots(return_stock_ids=True)` の行の並びどおりに、各行の月末の日付を返す。

    月末は `month_end_indices`（build_snapshots と同じ関数）で決める＝(社, 月) から一意に決まる。
    """
    memo: dict = {}

    def _ends(ec):
        m = memo.get(ec)
        if m is None:
            dates = [r.trade_date for r in prices_by_co[ec]]
            m = {dates[i][:7]: dates[i] for i in month_end_indices(dates)}
            memo[ec] = m
        return m

    out: dict = {}
    for ym, ecs in stock_ids_by_ym.items():
        row = []
        for ec in ecs:
            d = _ends(ec).get(ym)
            if d is None:
                raise ValueError(f"{ec} の {ym} に月末の足が無い（パネルと株価が食い違う）")
            row.append(d)
        out[ym] = row
    return out


def check_same_dates(panel_prices: dict, seq_rows: dict, codes) -> None:
    """パネル用と系列用の 2 回の読み込みで、社ごとの日付列が一致することを確かめる（違えば ValueError）。"""
    for ec in codes:
        a = [r.trade_date for r in panel_prices.get(ec, ())]
        b = [r.trade_date for r in seq_rows.get(ec, ())]
        if a != b:
            raise ValueError(
                f"{ec} の週次株価がパネル用（{len(a)} 週）と系列用（{len(b)} 週）で食い違う。"
                "読み込みの間に週次株価が書き換わった可能性がある。時間を置いて回し直す")
