"""過去断面の per/pbr が「調整済み株価 ÷ 当時の1株指標」になっている歪みを実測する（Issue #653）。

## なぜ必要か

`collector_prices._compute_market_values` は per / pbr / div_yield / market_cap を
「その時点の株価 ÷（または ×）その期の開示1株指標」で計算し、`financial_records` の実列へ
焼き込む。株価は Yahoo / J-Quants 由来で**株式分割を遡及調整済み**だが、`pl_eps` /
`bs_bps` / `dps` / `issued_shares` は**当時の提出値のまま**である。

したがって分割年より前の行だけが「調整後株価 ÷ 旧株数基準の1株指標」になり、後に分割した
社の過去 per / pbr が分割比ぶん小さく（＝割安に）出る。実測（#653）では ソニーG の
2024年3月期 PER が DB 上 3.29 だが、実際は 17〜20倍台＝ちょうど 1:5 分割ぶんずれている。

**最新行は現在株価と最新の提出値で基準が揃う**ので画面（スクリーニング・推奨）は歪まない。
歪むのは学習・バックテストのパネル（M-1 / M-2 / M-6）である。

## このスクリプトが測るもの・測らないもの

測る: 該当社数・該当行数・歪み倍率の分布・断面順位への影響・株価基準の内訳・公式との一致率。
測らない: 修正後の rank-IC（M-2 / M-6 の OOF は数時間〜数日かかるので日中バッチのキュー行き）。

**DB へは1バイトも書かない。** 修正方式（#653 の案A/B/C）は、ここで出た数字を見てから別 issue
で決める。issue 本文は「どれを採るかは実測後に決める」としており、先に直すと根拠が残らない。

## 歪みの向きは列によって逆になる

分割 1:F が年 Y に起きたとき、年 y < Y の行は:

    per        = (生株価 / F) / eps          -> 真値へは × F   （過小＝割安に見える）
    pbr        = (生株価 / F) / bps          -> 真値へは × F   （過小＝割安に見える）
    market_cap = (生株価 / F) * 旧株数       -> 真値へは × F   （過小）
    div_yield  = dps / (生株価 / F)          -> 真値へは / F   （過大＝高利回りに見える）
    nc_ratio   = net_cash / market_cap       -> 真値へは / F   （過大）

累積倍率は「その行**より後**に起きた全イベントの積」。`e.year > y` であって `>=` ではない
（分割当年の行は既に新基準なので歪まない）。逆分割は F < 1 となり向きが反転するが同じ式で扱う。

## 分割比をどこから取るか

**全件は DB 内在の2列だけで復元する**: `issued_shares`（期末発行済株式総数）の年次比と、
`bs_bps` の逆比が同じ倍率で一致すること。両者は XBRL の別タグ＝独立した書き手なので、
両方が同じ比で逆向きに動くことが交差検証になる。`period_type='annual'` の全行で
`issued_shares` は非 NULL なので、J-Quants 契約窓（2年）の外も遡れる。

ただしこれは「株数と1株純資産が両方動いた」という**必要条件**しか見ていないので、
`verify-sample` で公式 `AdjFactor` とサンプル突合して一致率を出す（陰性対照つき）。

**株数が分割に追随しない社のために第2経路がある**（#656）。`bs_bps` の年次比を候補ゲートに、
`pl_eps` の比を交差検証にする。株数と1株指標が同じ年に動く前提を外した経路で、
`--bps-path` / `--no-bps-path` で切り替える（既定は `DEFAULT_BPS_PATH`）。

実行:
    python -m scripts.measure_split_valuation_bias detect
    python -m scripts.measure_split_valuation_bias detect --sweep --price-basis
    python -m scripts.measure_split_valuation_bias detect --bps-path        # 第2経路つき
    python -m scripts.measure_split_valuation_bias verify-sample --dry-run
    python -m scripts.measure_split_valuation_bias verify-sample        # 約14分
    python -m scripts.measure_split_valuation_bias verify-sample --bps-path --source bps

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 候補ゲート。対数対称に使う（逆側は 1/1.4 = 0.7143）。分割と併合で検出力を変えない。
DEFAULT_MIN_RATIO = 1.4
# bps 逆比との一致許容。偽陽性（増資・自社株買い）を止めているのは閾値ではなくこちら。
DEFAULT_BPS_TOL = 0.15
# 定番比へのスナップ許容（対数距離）。
DEFAULT_SNAP_TOL = 0.02
# 第2経路（`bs_bps` を候補ゲート・`pl_eps` を交差検証にする経路・#656）を既定で使うか。
# **`rebuild_split_adjustment_factors` が既定のまま呼ぶ＝ここが毎晩の係数表の中身を決める。**
#
# **False のままなのは実測で倒したからである**（2026-09-12・
# `verify-sample --bps-path --source bps --n 30 --controls 10`）。公式 `AdjFactor` との
# 一致率は **0.367（11/30）**で、#654 の第1経路の 0.967 に届かない。
#
# **外れ方はランダムではなく、見つけた 16 件すべてで検出 < 公式だった。**
# `bs_bps` は分割以外（内部留保・有価証券の評価差額）でも増えるので、年次比は
# 真の分割比 F に対し `F / (1 + g)` になる（g は bps の成長率）。実測の g は
# **18%〜67%** と幅が広く、隣り合う定番比の間隔（例 2.0 と 2.5）を超える。
# 「`raw` 以上で最小の定番比を採る」上向きスナップでも **0.778（21/27）**で止まる。
# つまり **bps 比は「分割があった」は言えるが「何倍か」を決められない**。
#
# 存在の検出としては優秀で、抽出 30 社のうち 27 社に公式イベントがあり
# （偽陽性 3 社）、**陰性対照の見逃しは 0 社**だった。倍率を独立な第3の信号から
# 取る改良は #656 のコメントと後継 issue へ送った。True へ倒すのはそれが済んでから。
DEFAULT_BPS_PATH = False
# 合成（分割＋増資）とみなす残差の範囲。これを外れたら丸めずに unsnapped で別枠へ出す。
COMPOSITE_LO, COMPOSITE_HI = 0.8, 1.25

# 実在する分割・併合比。0.05 は 1:20 併合。
CANONICAL_RATIOS: tuple[float, ...] = (
    1.1, 1.2, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0,
    1 / 2, 1 / 4, 1 / 5, 1 / 10, 1 / 20,
)

# 歪みの向き。+1 は「真値へ × F」、-1 は「真値へ / F」。取り違えると全部逆になる。
COLUMN_DIRECTION: dict[str, int] = {
    "per": +1, "pbr": +1, "market_cap": +1, "div_yield": -1, "nc_ratio": -1,
}

# 断面順位の指標を出す列と、その最小断面サイズ。
RANK_COLUMNS = ("per", "pbr", "div_yield", "nc_ratio")
DEFAULT_MIN_CROSS_N = 30

# 歪み倍率のバンド（報告の本丸）。
BANDS_UP = (1.2, 1.5, 2.0, 3.0, 5.0, 10.0)
BANDS_DOWN = (0.83, 0.5, 0.2, 0.05)

# どのモデルがどの歪んだ列を読むか（plugins 側の定義から機械的に引けないので明示する。
# 増えたらここへ足す＝`docs/MODELS.md` と `plugins/macro_snapshots.py` が正本）。
MODEL_EXPOSURE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("M-1", "macro_risk_return", ("per", "pbr")),
    ("M-2", "macro_gbdt", ("per", "pbr", "div_yield", "nc_ratio")),
    ("M-6", "macro_enet", ("per", "pbr", "div_yield", "nc_ratio")),
    ("M-3", "macro_dlm", ()),
)

JSON_KEYS = (
    "input", "detect", "damage", "factors", "by_year", "columns",
    "rank_shift", "model_exposure", "price_basis", "top", "settings", "verdict",
)

DEFAULT_JSON = ROOT / "scripts" / ".cache" / "measure_split_valuation_bias.json"
DEFAULT_VERIFY_JSON = ROOT / "scripts" / ".cache" / "measure_split_valuation_bias_verify.json"


# ── 純関数（ネットワークにも DB にも触らない・ここがテスト対象）─────────────────

class AnnualRow(NamedTuple):
    edinet_code: str
    year: int
    period_end: Optional[str]
    issued_shares: Optional[float]
    bs_bps: Optional[float]
    pl_eps: Optional[float]
    dps: Optional[float]
    stock_price: Optional[float]
    per: Optional[float]
    pbr: Optional[float]
    div_yield: Optional[float]
    market_cap: Optional[float]


class ShareEvent(NamedTuple):
    edinet_code: str
    year: int
    prev_year: int
    gap_years: int
    period_end: Optional[str]
    prev_period_end: Optional[str]
    sh_ratio: float
    bps_ratio: float
    canonical: Optional[float]
    residual: float
    kind: str            # split | reverse | composite | unsnapped
    # どちらの経路が拾ったか（#656）。"shares" は株数比を候補ゲートにした第1経路、
    # "bps" は `bs_bps` の年次比を候補ゲートにし `pl_eps` の比で交差検証した第2経路。
    # 既定値を持つのは、既存の呼び出し（テストの `ev()` ヘルパー含む）を壊さないため。
    source: str = "shares"


class MatchResult(NamedTuple):
    edinet_code: str
    year: int
    detected: float               # canonical（無ければ生比）
    raw_detected: float           # スナップ前の生比（経路の候補ゲートに使った量）
    official: Optional[float]
    n_official_events: int
    status: str
    # status: agree | agree_raw_only | disagree_magnitude | no_official_event
    #         | official_only | out_of_coverage | no_sec_code


def _log(x: float) -> float:
    return math.log(x)


def snap_to_canonical(ratio: float, *, tol: float = DEFAULT_SNAP_TOL
                      ) -> tuple[Optional[float], float, str]:
    """観測比を実在する分割比へ寄せる。戻り値 (canonical, residual, kind)。

    株価の遡及調整は**分割にしか掛からない**（時価発行増資では過去株価は調整されない）ので、
    観測した株数比をそのまま歪み倍率に使うと合成ケースで過大評価になる。定番比へ寄せ、
    残差 `ratio / canonical` を増資・自社株買い成分として分離する。

    実測の合成例 4.7891 は 5.0 へ寄る（対数残差 0.043）。4.0 は選ばれない（同 0.18）。
    どの定番比にも合成としても寄らない比（例 7.0 は最近傍 5.0 に対し残差 1.4）は
    **勝手に丸めず** `unsnapped` にして主集計から外す
    （丸めると「測っていない値」が数字に混ざる）。
    """
    if ratio <= 0:
        return None, ratio, "unsnapped"
    best = min(CANONICAL_RATIOS, key=lambda c: abs(_log(ratio / c)))
    residual = ratio / best
    if abs(_log(residual)) <= _log(1 + tol):
        return best, residual, ("split" if best > 1 else "reverse")
    if COMPOSITE_LO <= residual <= COMPOSITE_HI:
        return best, residual, "composite"
    return None, ratio, "unsnapped"


def _usable(row: AnnualRow) -> Optional[str]:
    """イベント判定に使える行か。使えないなら理由コードを返す。"""
    if row.issued_shares is None or row.issued_shares <= 0:
        return "missing_shares"
    if row.bs_bps is None:
        return "missing_bps"
    if row.bs_bps < 0:
        return "negative_bps"          # 債務超過。逆比の符号が反転して幽霊イベントになる
    if row.bs_bps == 0:
        return "missing_bps"
    return None


def detect_events(rows: Sequence[AnnualRow], *,
                  min_ratio: float = DEFAULT_MIN_RATIO,
                  bps_tol: float = DEFAULT_BPS_TOL,
                  snap_tol: float = DEFAULT_SNAP_TOL,
                  bps_path: bool = DEFAULT_BPS_PATH,
                  ) -> tuple[list[ShareEvent], dict]:
    """株数基準が変わった年を検出する。戻り値 (events, stats)。

    経路は2本あり、**どちらも「候補ゲート1つ ＋ 独立した第2の書き手による交差検証1つ」**と
    いう同じ形をしている。閾値が偽陽性を止めているのではなく、交差検証が止めている。

    - 第1経路（`source="shares"`）: `issued_shares` の年次比が閾値を超えること（候補ゲート）と、
      `bs_bps` の逆比が同じ倍率で一致すること（交差検証）。増資・自社株買いはここで落ちる。
    - 第2経路（`source="bps"`・#656）: `bs_bps` の年次比が閾値を超えること（候補ゲート）と、
      `pl_eps` の比が同じ倍率で一致すること（交差検証）。減損・大幅赤字はここで落ちる。

    **第2経路が要るのは、株数と1株指標が同じ年に動くとは限らないから。** 分割を 1 株指標には
    反映しているのに `issued_shares` が据え置きの社は第1経路の候補にすら上がらない
    （実測 E03137 しまむらは `shares x1.0000` のまま `bps x1.8670 / eps x1.8971`）。
    逆にこの社は株数が動いた年の `bs_bps` が**上がって**いるため、第1経路の交差検証でも落ちる。
    同一年ペアの中で株数と bps を突き合わせる設計では原理的に拾えない。

    **同じ (edinet_code, year) を両経路が拾ったら第1経路を採る**（畳む）。株数は分割で必ず動く
    量で、bps のように内部留保や配当で毎年動く量より基準として素直だからである。

    欠損年があるときは `year-1` ではなく**直前の使える行**とペアを組み、`gap_years` を残す。
    2以上なら複数イベントの積を1件と見ている可能性があるので、突合サンプルへ優先的に入れる。
    """
    gate = _log(min_ratio)
    by_ec: dict[str, list[AnnualRow]] = defaultdict(list)
    for r in rows:
        by_ec[r.edinet_code].append(r)

    events: list[ShareEvent] = []
    skipped: Counter = Counter()
    candidate_ecs: set[str] = set()
    n_candidates = 0
    bps_candidate_ecs: set[str] = set()
    n_bps_candidates = 0
    bps_rejected: Counter = Counter()
    for ec, rs in by_ec.items():
        prev: Optional[AnnualRow] = None
        for cur in sorted(rs, key=lambda r: r.year):
            why = _usable(cur)
            if why:
                skipped[why] += 1
                continue
            if prev is not None:
                took = False
                sh_ratio = cur.issued_shares / prev.issued_shares
                bps_ratio = prev.bs_bps / cur.bs_bps
                if abs(_log(sh_ratio)) >= gate:
                    n_candidates += 1
                    candidate_ecs.add(ec)
                    if abs(bps_ratio / sh_ratio - 1.0) <= bps_tol:
                        canonical, residual, kind = snap_to_canonical(sh_ratio, tol=snap_tol)
                        events.append(ShareEvent(
                            edinet_code=ec, year=cur.year, prev_year=prev.year,
                            gap_years=cur.year - prev.year,
                            period_end=cur.period_end, prev_period_end=prev.period_end,
                            sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                            canonical=canonical, residual=residual, kind=kind,
                            source="shares"))
                        took = True

                if bps_path and abs(_log(bps_ratio)) >= gate:
                    n_bps_candidates += 1
                    bps_candidate_ecs.add(ec)
                    if took:
                        # 第1経路が同じペアを既に採った。両方が同じ実体を指しているので
                        # 2件に数えない（数えると `cumulative_factors` が比を二乗する）。
                        bps_rejected["dup_with_shares"] += 1
                    elif (prev.pl_eps is None or cur.pl_eps is None
                            or prev.pl_eps <= 0 or cur.pl_eps <= 0):
                        # **符号が跨ぐ年・赤字の年は比の意味が壊れる。** 赤字継続（両年とも負）でも
                        # 比は数学的には出るが、赤字幅の増減が分割比に化けるので落とす側を採る。
                        bps_rejected["eps_sign"] += 1
                    elif abs((prev.pl_eps / cur.pl_eps) / bps_ratio - 1.0) > bps_tol:
                        # 交差検証で落ちた本体。減損・大幅赤字・タグ基準の変更はここへ来る
                        # （bps だけが動いて eps が追随しない）。
                        bps_rejected["eps_mismatch"] += 1
                    else:
                        canonical, residual, kind = snap_to_canonical(bps_ratio, tol=snap_tol)
                        if canonical is None:
                            bps_rejected["unsnapped"] += 1
                        else:
                            events.append(ShareEvent(
                                edinet_code=ec, year=cur.year, prev_year=prev.year,
                                gap_years=cur.year - prev.year,
                                period_end=cur.period_end, prev_period_end=prev.period_end,
                                sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                                canonical=canonical, residual=residual, kind=kind,
                                source="bps"))
            prev = cur

    bps_events = [e for e in events if e.source == "bps"]
    stats = {
        "n_candidate_pairs": n_candidates,
        "n_events": len(events),
        "n_event_companies": len({e.edinet_code for e in events}),
        "n_candidate_companies": len(candidate_ecs),
        "skipped": dict(skipped),
        "by_kind": dict(Counter(e.kind for e in events)),
        "n_gap_years_ge2": sum(1 for e in events if e.gap_years >= 2),
        "n_events_by_source": dict(Counter(e.source for e in events)),
        "bps_path": {
            "enabled": bool(bps_path),
            "n_candidate_pairs": n_bps_candidates,
            "n_candidate_companies": len(bps_candidate_ecs),
            "n_events": len(bps_events),
            "n_event_companies": len({e.edinet_code for e in bps_events}),
            "by_kind": dict(Counter(e.kind for e in bps_events)),
            "rejected": dict(bps_rejected),
        },
    }
    return events, stats


def cumulative_factors(rows: Sequence[AnnualRow], events: Sequence[ShareEvent], *,
                       use_canonical: bool = True) -> dict[tuple[str, int], float]:
    """行ごとの累積歪み倍率 F。**その行より後**に起きたイベントの積だけを掛ける。

    `e.year > row.year` が正しい。`>=` にすると分割当年の行まで被害に数え、社あたり1行ずつ
    被害行数が膨らむ。当年の行は既に新株数基準の提出値なので歪んでいない。

    `use_canonical=True` では `unsnapped`（丸められない比）を**無視する**＝主集計から外す。
    上限見積りが要るときは False を渡して観測比の積を取る。
    """
    ev_by_ec: dict[str, list[ShareEvent]] = defaultdict(list)
    for e in events:
        ratio = e.canonical if use_canonical else e.sh_ratio
        if ratio is None:
            continue
        ev_by_ec[e.edinet_code].append(e._replace(canonical=ratio))

    out: dict[tuple[str, int], float] = {}
    for r in rows:
        f = 1.0
        for e in ev_by_ec.get(r.edinet_code, ()):
            if e.year > r.year:
                f *= e.canonical
        out[(r.edinet_code, r.year)] = f
    return out


def corrected_values(row: AnnualRow, factor: float) -> dict[str, float]:
    """歪んだ列を真値へ戻した値。向きは `COLUMN_DIRECTION` が唯一の源。"""
    out: dict[str, float] = {}
    for col, sign in COLUMN_DIRECTION.items():
        if col == "nc_ratio":
            continue                      # AnnualRow は持たない（VIEW 側の列）
        v = getattr(row, col)
        if v is None:
            continue
        out[col] = v * factor if sign > 0 else v / factor
    return out


def severity_bands(factors: Iterable[float]) -> dict[str, int]:
    """歪み倍率のバンド別件数。ここが「直すべきか」を決める本丸。"""
    fs = [f for f in factors if f != 1.0]
    out: dict[str, int] = {}
    for b in BANDS_UP:
        out[">=%g" % b] = sum(1 for f in fs if f >= b)
    for b in BANDS_DOWN:
        out["<=%g" % b] = sum(1 for f in fs if f <= b)
    return out


def quantiles(values: Sequence[float], qs: Sequence[float] = (0.5, 0.75, 0.9, 0.95, 1.0)
              ) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    out: dict[str, float] = {}
    for q in qs:
        i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        out["p%g" % (q * 100)] = s[i]
    return out


def _pct_ranks(values: Sequence[float]) -> list[float]:
    """0-100 のパーセンタイル順位（同値は平均順位）。"""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = 100.0 * avg / (n - 1) if n > 1 else 50.0
        i = j + 1
    return ranks


def percentile_shift(year_rows: Sequence[tuple[str, Optional[float]]],
                     factors: Mapping[str, float], column: str, *,
                     min_n: int = DEFAULT_MIN_CROSS_N) -> dict:
    """1年ぶんの断面で、被害行のパーセンタイル順位が補正でどれだけ動くかを測る。

    M-1 / M-2 / M-6 が使うのは水準そのものではなく断面の相対位置なので、**順位が動かない
    なら実害は小さい**。`year_rows` は [(edinet_code, 値)]、`factors` は {edinet_code: F}。
    """
    sign = COLUMN_DIRECTION[column]
    pairs = [(ec, v) for ec, v in year_rows if v is not None]
    if len(pairs) < min_n:
        return {"n": len(pairs), "thin_cross_section": True}

    stored = [v for _, v in pairs]
    corr = []
    for ec, v in pairs:
        f = factors.get(ec, 1.0)
        corr.append(v * f if sign > 0 else v / f)

    rs, rc = _pct_ranks(stored), _pct_ranks(corr)
    shifts = [abs(rc[i] - rs[i]) for i, (ec, _) in enumerate(pairs) if factors.get(ec, 1.0) != 1.0]
    if not shifts:
        return {"n": len(pairs), "n_affected": 0, "thin_cross_section": False}
    q = quantiles(shifts, (0.5, 0.9, 1.0))
    return {
        "n": len(pairs), "n_affected": len(shifts), "thin_cross_section": False,
        "median_shift_pt": q.get("p50", 0.0), "p90_shift_pt": q.get("p90", 0.0),
        "max_shift_pt": q.get("p100", 0.0),
        "n_gt_10pt": sum(1 for s in shifts if s > 10.0),
        "n_gt_25pt": sum(1 for s in shifts if s > 25.0),
    }


def classify_price_basis(stored: Optional[float],
                         weekly: Sequence[tuple[str, float]],
                         factor: float, *, tol: float = 0.03) -> str:
    """焼き込まれた `stock_price` が調整後系列から来たのか生値のままかを判別する。

    交差検証（株数 × bps）は「分割があった」しか言っておらず、その行の株価が**実際に
    調整後で上書きされたか**は別問題である。分割前に書かれてその後一度も再収集されて
    いない行は旧株価 ÷ 旧 EPS で整合している＝歪んでいない。

    戻り値 adjusted（真に歪み）/ raw（実は無害）/ unknown。F が 1 に近いと両仮説が
    分離できないので unknown へ倒す（分離できないものを断定しない）。
    """
    if stored is None or stored <= 0 or not weekly or factor <= 0:
        return "unknown"
    lf = abs(_log(factor))
    if lf < _log(1.05):
        return "unknown"
    closes = [c for _, c in weekly if c and c > 0]
    if not closes:
        return "unknown"
    d_adj = min(abs(_log(stored / c)) for c in closes)
    d_raw = min(abs(_log(stored / (c * factor))) for c in closes)
    if abs(d_adj - d_raw) < lf / 2:
        return "unknown"
    if d_adj <= d_raw:
        return "adjusted" if d_adj <= _log(1 + tol) else "unknown"
    return "raw" if d_raw <= _log(1 + tol) else "unknown"


def _iso(d) -> Optional[str]:
    if d is None:
        return None
    return str(d)[:10]


def event_window(ev: ShareEvent, *, slack_days: int = 45) -> Optional[tuple[str, str]]:
    """突合に使う日付窓 (前期末 - slack, 当期末 + slack]。

    分割の効力発生日と、株数がどちらの期に計上されるかのズレを吸収するための余裕。
    """
    p0, p1 = _iso(ev.prev_period_end), _iso(ev.period_end)
    if not p0 or not p1:
        return None
    return ((date.fromisoformat(p0) - timedelta(days=slack_days)).isoformat(),
            (date.fromisoformat(p1) + timedelta(days=slack_days)).isoformat())


def in_coverage(ev: ShareEvent, coverage: Optional[tuple[str, str]], *,
                slack_days: int = 45) -> bool:
    """公式がこのイベントを判定できるか。窓が契約期間へ完全に収まるときだけ True。"""
    win = event_window(ev, slack_days=slack_days)
    if win is None:
        return False
    if not coverage or not coverage[0] or not coverage[1]:
        return True
    return coverage[0] <= win[0] and win[1] <= coverage[1]


def match_event(ev: ShareEvent, official: Sequence[tuple[str, float]], *,
                slack_days: int = 45, tol: float = 0.05,
                coverage: Optional[tuple[str, str]] = None) -> MatchResult:
    """検出したイベントを公式 `AdjFactor` と突き合わせる。

    公式の `AdjFactor` は**過去株価に掛ける係数**なので 1:2 分割は 0.5 で返る。株数比へ
    直すため逆数を取る。同一窓に複数イベントがあれば積になり、DB 側の年次比も積なので整合する。

    窓は (前期末 - slack, 当期末 + slack]。分割の効力発生日と株数の計上期のズレを吸収する。
    契約窓の外は `out_of_coverage` にして**一致率の分母から外す**（混ぜると理由なく下がる）。
    """
    # 生比は**そのイベントを拾った経路の候補ゲートに使った量**を採る（#656）。
    # bps 経路の `sh_ratio` は 1.0 近傍なので、そちらを見ると `agree_raw_only` が原理的に
    # 立たなくなり、「スナップが悪さをしている」を分けて数える仕組みが黙って死ぬ。
    raw = ev.bps_ratio if ev.source == "bps" else ev.sh_ratio
    detected = ev.canonical if ev.canonical is not None else raw
    win = event_window(ev, slack_days=slack_days)
    if win is None or not in_coverage(ev, coverage, slack_days=slack_days):
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           None, 0, "out_of_coverage")
    w0, w1 = win

    inside = [f for d, f in official if w0 < d <= w1 and f and f > 0]
    if not inside:
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           None, 0, "no_official_event")
    prod = 1.0
    for f in inside:
        prod *= f
    official_ratio = 1.0 / prod

    lim = _log(1 + tol)
    if abs(_log(official_ratio / detected)) <= lim:
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           official_ratio, len(inside), "agree")
    if abs(_log(official_ratio / raw)) <= lim:
        # 生比では合うがスナップ後で外れる＝スナップが悪さをしている側。分けて数える。
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           official_ratio, len(inside), "agree_raw_only")
    return MatchResult(ev.edinet_code, ev.year, detected, raw,
                       official_ratio, len(inside), "disagree_magnitude")


def choose_sample(events: Sequence[ShareEvent], flat_ecs: Sequence[str], *,
                  n: int = 30, controls: int = 10, seed: int = 0
                  ) -> tuple[list[str], list[str]]:
    """突合サンプル (陽性, 陰性対照) を層化抽出する。同一 seed で完全に再現する。

    層 = kind x (gap_years >= 2)。各層に最低1社を割り当ててから残りを層サイズ比で配る。
    **陰性対照が要点**で、陽性だけの一致率は偽陽性率しか測れない。株数がほぼ動かない社を
    引いて「公式にもイベントが無い」ことを確かめ、見逃し率を押さえる。
    """
    rng = random.Random(seed)
    strata: dict[tuple[str, bool], list[str]] = defaultdict(list)
    for e in events:
        strata[(e.kind, e.gap_years >= 2)].append(e.edinet_code)
    for k in strata:
        strata[k] = sorted(set(strata[k]))

    picked: list[str] = []
    keys = sorted(strata, key=lambda k: (k[0], k[1]))
    for k in keys:                                    # まず各層から1社
        if len(picked) < n and strata[k]:
            picked.append(rng.choice(strata[k]))
    pool = sorted({ec for k in keys for ec in strata[k]} - set(picked))
    rng.shuffle(pool)
    picked.extend(pool[: max(0, n - len(picked))])

    positives = set(picked)
    ctrl_pool = sorted(set(flat_ecs) - positives)
    rng.shuffle(ctrl_pool)
    return sorted(picked), sorted(ctrl_pool[:controls])


def build_report(rows: Sequence[AnnualRow], events: Sequence[ShareEvent],
                 factors: Mapping[tuple[str, int], float], *, extras: dict) -> dict:
    """出力する指標をひとまとめにする。`render_text` と `to_json_dict` が読む。"""
    damaged = {k: f for k, f in factors.items() if f != 1.0}
    years = [r.year for r in rows]

    by_year: dict[int, dict] = {}
    for y in sorted(set(years)):
        rs = [r for r in rows if r.year == y]
        dmg = [factors.get((r.edinet_code, r.year), 1.0) for r in rs]
        dmg = [f for f in dmg if f != 1.0]
        by_year[y] = {
            "n_rows": len(rs), "n_damaged": len(dmg),
            "share": (len(dmg) / len(rs)) if rs else 0.0,
            "median_factor": quantiles(dmg, (0.5,)).get("p50"),
        }

    ec_factor_by_year: dict[int, dict[str, float]] = defaultdict(dict)
    for (ec, y), f in factors.items():
        ec_factor_by_year[y][ec] = f

    rank_shift: dict[str, dict] = {}
    for col in RANK_COLUMNS:
        per_year = {}
        for y in sorted(set(years)):
            if col == "nc_ratio":
                vals = [(r.edinet_code, extras.get("nc_ratio", {}).get((r.edinet_code, r.year)))
                        for r in rows if r.year == y]
            else:
                vals = [(r.edinet_code, getattr(r, col)) for r in rows if r.year == y]
            per_year[y] = percentile_shift(vals, ec_factor_by_year[y], col,
                                           min_n=extras.get("min_cross_n", DEFAULT_MIN_CROSS_N))
        rank_shift[col] = per_year

    n_all = len(rows)
    return {
        "input": extras.get("input", {}),
        "detect": extras.get("detect", {}),
        "damage": {
            "n_damaged_rows": len(damaged),
            "n_damaged_companies": len({ec for ec, _ in damaged}),
            "n_annual_rows": n_all,
            "share_of_rows": (len(damaged) / n_all) if n_all else 0.0,
        },
        "factors": {
            "quantiles": quantiles(list(damaged.values())),
            "bands": severity_bands(damaged.values()),
        },
        "by_year": by_year,
        "columns": {c: ("x F" if s > 0 else "/ F") for c, s in COLUMN_DIRECTION.items()},
        "rank_shift": rank_shift,
        "model_exposure": [
            {"model": m, "plugin": p, "distorted_columns": list(cols)}
            for m, p, cols in MODEL_EXPOSURE
        ],
        "price_basis": extras.get("price_basis"),
        "top": extras.get("top", []),
        "settings": extras.get("settings", {}),
        "verdict": extras.get("verdict", ""),
    }


def _fmt(v, spec: str = "%.4f") -> str:
    return "n/a" if v is None else (spec % v)


def render_text(report: dict) -> str:
    """人が読む形。**ASCII 記号のみ**（cp932 リダイレクトで落とさないため）。"""
    L: list[str] = []
    add = L.append
    bar = "-" * 78

    add(bar)
    add("A. 入力")
    inp = report.get("input", {})
    add("  接続先=%s / annual %s行 / %s社 / 年 %s-%s"
        % (inp.get("db_target"), inp.get("n_rows"), inp.get("n_companies"),
           inp.get("year_from"), inp.get("year_to")))
    nn = inp.get("non_null", {})
    add("  非NULL: " + ", ".join("%s=%s" % (k, nn[k]) for k in sorted(nn)))

    add(bar)
    add("B. 検出")
    det = report.get("detect", {})
    add("  候補ペア=%s (%s社) -> 交差検証通過=%s (%s社)"
        % (det.get("n_candidate_pairs"), det.get("n_candidate_companies"),
           det.get("n_events"), det.get("n_event_companies")))
    add("  種別: " + ", ".join("%s=%s" % kv for kv in sorted(det.get("by_kind", {}).items())))
    add("  gap_years>=2: %s / 除外: %s"
        % (det.get("n_gap_years_ge2"), det.get("skipped") or "なし"))
    bp = det.get("bps_path") or {}
    if bp.get("enabled"):
        add("  経路別: " + ", ".join("%s=%s" % kv
                                     for kv in sorted(det.get("n_events_by_source", {}).items())))
        add("  bps経路: 候補ペア=%s (%s社) -> 通過=%s (%s社) / 種別 %s"
            % (bp.get("n_candidate_pairs"), bp.get("n_candidate_companies"),
               bp.get("n_events"), bp.get("n_event_companies"), bp.get("by_kind") or {}))
        add("  bps経路の棄却: %s" % (bp.get("rejected") or "なし"))
    else:
        add("  bps経路: 無効 (--bps-path で有効化)")
    hist = det.get("canonical_hist", {})
    if hist:
        add("  canonical: " + ", ".join(
            "%g x%d" % (float(k), v) for k, v in sorted(hist.items(), key=lambda x: float(x[0]))))

    add(bar)
    add("C. 被害範囲")
    dmg = report.get("damage", {})
    add("  被害行=%s / 被害社=%s / 全annual行=%s (%.2f%%)"
        % (dmg.get("n_damaged_rows"), dmg.get("n_damaged_companies"),
           dmg.get("n_annual_rows"), 100.0 * dmg.get("share_of_rows", 0.0)))

    add(bar)
    add("D. 歪み倍率 F の分布")
    fq = report.get("factors", {}).get("quantiles", {})
    add("  " + ", ".join("%s=%s" % (k, _fmt(v)) for k, v in sorted(fq.items())))
    add("  バンド別行数: " + ", ".join(
        "%s:%d" % kv for kv in report.get("factors", {}).get("bands", {}).items()))

    add(bar)
    add("E. 年別内訳 (古い年ほど悪化するはず。そうでなければ検出器を疑う)")
    add("  year |  rows | damaged |  share | median F")
    for y, d in sorted(report.get("by_year", {}).items()):
        add("  %4s | %5s | %7s | %5.1f%% | %s"
            % (y, d["n_rows"], d["n_damaged"], 100.0 * d["share"], _fmt(d["median_factor"])))

    add(bar)
    add("F. 列別の歪みの向き (per/pbr と div_yield/nc_ratio は逆向き)")
    for c, d in report.get("columns", {}).items():
        add("  %-11s 真値へ %s" % (c, d))

    add(bar)
    add("G. 断面順位への影響 (水準でなく相対位置が動くかが実害)")
    for col, per_year in report.get("rank_shift", {}).items():
        tot10 = sum(d.get("n_gt_10pt", 0) for d in per_year.values())
        tot25 = sum(d.get("n_gt_25pt", 0) for d in per_year.values())
        meds = [d["median_shift_pt"] for d in per_year.values() if "median_shift_pt" in d]
        thin = sum(1 for d in per_year.values() if d.get("thin_cross_section"))
        add("  %-10s median=%spt / >10pt=%d行 / >25pt=%d行 / 薄い断面=%d年"
            % (col, _fmt(sum(meds) / len(meds) if meds else None, "%.1f"), tot10, tot25, thin))

    add(bar)
    add("H. モデル別の露出")
    for m in report.get("model_exposure", []):
        cols = ", ".join(m["distorted_columns"]) or "なし (fin_features を持たない)"
        add("  %-4s %-18s %s" % (m["model"], m["plugin"], cols))
    add("  注: scripts/preset_ic_gate.py のパネルは z_* 7因子のみで per/pbr を含まない")
    add("      ため、このゲートでは本件を測れない (issue #653 の検証欄は誤り)")

    pb = report.get("price_basis")
    if pb:
        add(bar)
        add("I. 株価基準の内訳 (adjusted=真に歪み / raw=実は無害)")
        tot = sum(pb.values()) or 1
        add("  " + ", ".join("%s=%d (%.1f%%)" % (k, v, 100.0 * v / tot)
                             for k, v in sorted(pb.items())))

    top = report.get("top") or []
    if top:
        add(bar)
        add("J. 歪みの大きい順")
        add("  edinet    code   year  F         kind       per(保存) -> per(補正)")
        for t in top:
            add("  %-9s %-6s %4s  %-9s %-10s %s -> %s"
                % (t["edinet_code"], t.get("sec_code") or "-", t["year"], _fmt(t["factor"]),
                   t["kind"], _fmt(t.get("per"), "%.2f"), _fmt(t.get("per_corrected"), "%.2f")))

    add(bar)
    add("VERDICT: " + report.get("verdict", ""))
    add(bar)
    return "\n".join(L)


def to_json_dict(report: dict) -> dict:
    """機械可読な形。トップレベルのキー集合はテストで固定する（指標の黙った欠落を落とす）。"""
    return {k: report.get(k) for k in JSON_KEYS}


def build_verdict(report: dict) -> str:
    d = report.get("damage", {})
    bands = report.get("factors", {}).get("bands", {})
    rs = report.get("rank_shift", {}).get("per", {})
    gt10 = sum(v.get("n_gt_10pt", 0) for v in rs.values())
    q = report.get("factors", {}).get("quantiles", {})
    return ("%s companies / %s rows distorted (%.2f%% of annual rows); F median %s, "
            "F>=2 in %s rows; PER cross-sectional rank moves >10pt in %s rows"
            % (d.get("n_damaged_companies"), d.get("n_damaged_rows"),
               100.0 * d.get("share_of_rows", 0.0), _fmt(q.get("p50"), "%.2f"),
               bands.get(">=2", 0), gt10))


# ── I/O ─────────────────────────────────────────────────────────────────────

_SQL_ANNUAL = """
SELECT edinet_code, year, period_end, issued_shares, bs_bps, pl_eps, dps,
       stock_price, per, pbr, div_yield, market_cap
  FROM financial_records
 WHERE period_type = 'annual' AND year >= :yf
   AND (:yt = 0 OR year <= :yt)
 ORDER BY edinet_code, year
"""


def load_annual_rows(db, *, year_from: int = 2018, year_to: Optional[int] = None
                     ) -> list[AnnualRow]:
    """通期行を読む。**読み終えたら commit する**（GOTCHAS #411）。

    `SessionLocal()` のトランザクションを開いたまま計算へ入ると、pooler 経由で
    `idle in transaction` が滞留し `init_db()` の ALTER を止める。同じ罠を
    `measure_embargo_impact._load_prices` が踏んで3時間15分ブロックした前例がある。
    """
    from sqlalchemy import text as sqla_text
    rows = db.execute(sqla_text(_SQL_ANNUAL),
                      {"yf": year_from, "yt": year_to or 0}).fetchall()
    db.commit()
    return [AnnualRow(
        edinet_code=r[0], year=int(r[1]), period_end=_iso(r[2]),
        issued_shares=r[3], bs_bps=r[4], pl_eps=r[5], dps=r[6],
        stock_price=r[7], per=r[8], pbr=r[9], div_yield=r[10], market_cap=r[11],
    ) for r in rows]


def load_company_meta(db, ecs: Sequence[str]) -> dict[str, tuple[str, str]]:
    from sqlalchemy import text as sqla_text
    if not ecs:
        return {}
    rows = db.execute(sqla_text(
        "SELECT edinet_code, sec_code, name FROM companies "
        "WHERE edinet_code = ANY(:ecs)"), {"ecs": list(ecs)}).fetchall()
    db.commit()
    return {r[0]: (r[1] or "", r[2] or "") for r in rows}


def load_nc_ratio(db, year_from: int, year_to: Optional[int]) -> dict[tuple[str, int], float]:
    """`financial_metrics` VIEW の nc_ratio（market_cap を分母に持つので同型に歪む）。"""
    from sqlalchemy import text as sqla_text
    rows = db.execute(sqla_text(
        "SELECT edinet_code, year, nc_ratio FROM financial_metrics "
        "WHERE year >= :yf AND (:yt = 0 OR year <= :yt) AND nc_ratio IS NOT NULL"),
        {"yf": year_from, "yt": year_to or 0}).fetchall()
    db.commit()
    return {(r[0], int(r[1])): float(r[2]) for r in rows}


def load_weekly_closes(db, targets: Sequence[tuple[str, str]], *, span_days: int = 120
                       ) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """対象行の (edinet_code, period_end) 近傍の週次終値だけを引く。

    全件 pull すると週次 97万行を読む。ここは絞り込みが必須（Egress・#480 の較正対象）。
    """
    from sqlalchemy import text as sqla_text
    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    if not targets:
        return out
    sql = sqla_text(
        "SELECT trade_date, close_last FROM stock_price_weekly "
        " WHERE edinet_code = :ec AND trade_date BETWEEN :d0 AND :d1 "
        "   AND close_last > 0 ORDER BY trade_date")
    for ec, pe in targets:
        if not pe:
            continue
        d = date.fromisoformat(pe)
        rows = db.execute(sql, {"ec": ec,
                                "d0": (d - timedelta(days=span_days)).isoformat(),
                                "d1": (d + timedelta(days=span_days)).isoformat()}).fetchall()
        out[(ec, pe)] = [(_iso(r[0]), float(r[1])) for r in rows]
    db.commit()
    return out


async def learn_coverage() -> tuple[str, str]:
    """J-Quants 契約窓（無料は直近2年）。**抽出より先に呼ぶ**。

    窓を知らずに全期間からサンプルを引くと、大半が `out_of_coverage` に落ちて分母が消える
    （実測 2026-09-12: 30件中 21件が窓外で分母 9 まで縮み、一致率が結論に使えなかった）。
    公式が判定できる範囲から引くのが、突合という手続きの前提そのものである。
    """
    import httpx                                               # 遅延 import（テストを軽く保つ）
    from collector_prices import _learn_jquants_coverage

    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise SystemExit("環境変数 JQUANTS_API_KEY が未設定です")
    async with httpx.AsyncClient(timeout=60) as s:
        cover = await _learn_jquants_coverage(s, api_key)
    if not cover[0]:
        raise SystemExit("J-Quants のカバレッジ窓を取得できませんでした")
    return cover


async def fetch_official_events(ec_secs: Sequence[tuple[str, str]], cover: tuple[str, str], *,
                                on_progress=None) -> dict[str, list]:
    """公式の企業イベントを取る。**レート制御も認証も既存実装を再利用する**（二重実装しない）。"""
    from scripts.repair_splits_from_jquants import collect_official, extract_events

    bars = await collect_official(list(ec_secs), cover, on_progress=on_progress)
    return {ec: extract_events(rows) for ec, rows in bars.items()}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cmd_detect(args) -> int:
    import database as D

    db = D.SessionLocal()
    try:
        rows = load_annual_rows(db, year_from=args.year_from, year_to=args.year_to)
        nc = load_nc_ratio(db, args.year_from, args.year_to)
        print("annual %d行を読み込み（接続先=%s）" % (len(rows), D.DB_TARGET), flush=True)

        events, stats = detect_events(rows, min_ratio=args.min_ratio,
                                      bps_tol=args.bps_tol, snap_tol=args.snap_tol,
                                      bps_path=args.bps_path)
        stats["canonical_hist"] = dict(
            Counter("%g" % e.canonical for e in events if e.canonical is not None))

        factors = cumulative_factors(rows, events)
        damaged = {k: f for k, f in factors.items() if f != 1.0}
        row_by_key = {(r.edinet_code, r.year): r for r in rows}

        price_basis = None
        if args.price_basis:
            targets = [(ec, row_by_key[(ec, y)].period_end) for ec, y in damaged
                       if (ec, y) in row_by_key]
            print("株価基準の判別のため週次を %d 窓ぶん読みます..." % len(targets), flush=True)
            weekly = load_weekly_closes(db, targets)
            pb: Counter = Counter()
            for (ec, y), f in damaged.items():
                r = row_by_key.get((ec, y))
                if r is None:
                    continue
                pb[classify_price_basis(r.stock_price, weekly.get((ec, r.period_end), []), f)] += 1
            price_basis = dict(pb)

        meta = load_company_meta(db, sorted({ec for ec, _ in damaged}))
        kind_by_ec: dict[str, str] = {}
        for e in events:
            kind_by_ec.setdefault(e.edinet_code, e.kind)
        worst = sorted(damaged.items(), key=lambda kv: -abs(_log(kv[1])))[: args.top]
        top = []
        for (ec, y), f in worst:
            r = row_by_key.get((ec, y))
            top.append({
                "edinet_code": ec, "sec_code": meta.get(ec, ("", ""))[0], "year": y,
                "factor": f, "kind": kind_by_ec.get(ec, "?"),
                "per": r.per if r else None,
                "per_corrected": (r.per * f) if (r and r.per is not None) else None,
            })

        non_null = {c: sum(1 for r in rows if getattr(r, c) is not None)
                    for c in ("issued_shares", "bs_bps", "pl_eps", "dps",
                              "stock_price", "per", "pbr", "div_yield", "market_cap")}
        report = build_report(rows, events, factors, extras={
            "input": {"db_target": D.DB_TARGET, "n_rows": len(rows),
                      "n_companies": len({r.edinet_code for r in rows}),
                      "year_from": min((r.year for r in rows), default=None),
                      "year_to": max((r.year for r in rows), default=None),
                      "non_null": non_null},
            "detect": stats, "nc_ratio": nc, "price_basis": price_basis, "top": top,
            "min_cross_n": args.min_cross_n,
            "settings": {"min_ratio": args.min_ratio, "bps_tol": args.bps_tol,
                         "snap_tol": args.snap_tol, "price_basis": bool(args.price_basis),
                         "bps_path": bool(args.bps_path)},
        })
        report["verdict"] = build_verdict(report)

        print(render_text(report))

        if args.sweep:
            print()
            print("閾値感度 (min_ratio x bps_tol -> 通過ペア/社数/F>=2 の被害行)")
            for mr in (1.05, 1.1, 1.2, 1.4, 1.5, 2.0):
                cells = []
                for bt in (0.05, 0.10, 0.15, 0.25):
                    ev, st = detect_events(rows, min_ratio=mr, bps_tol=bt,
                                           snap_tol=args.snap_tol, bps_path=args.bps_path)
                    ff = cumulative_factors(rows, ev)
                    n2 = sum(1 for v in ff.values() if v >= 2.0)
                    cells.append("%4d/%4d/%5d" % (st["n_events"], st["n_event_companies"], n2))
                print("  min_ratio=%-5g | %s" % (mr, " | ".join(cells)))
            print("  (列は bps_tol=0.05 / 0.10 / 0.15 / 0.25)")

        if args.json:
            p = Path(args.json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(to_json_dict(report), ensure_ascii=False,
                                    indent=2, default=str), encoding="utf-8")
            print("JSON: %s" % p)
        return 0
    finally:
        db.close()


def _cmd_verify_sample(args) -> int:
    import database as D

    db = D.SessionLocal()
    try:
        rows = load_annual_rows(db, year_from=args.year_from, year_to=args.year_to)
        all_events, _ = detect_events(rows, min_ratio=args.min_ratio,
                                      bps_tol=args.bps_tol, snap_tol=args.snap_tol,
                                      bps_path=args.bps_path)
        # **抽出より先に契約窓を学習する**。窓の外から引いたサンプルは公式が判定できず、
        # `out_of_coverage` で分母だけが消える（実測 2026-09-12: 30件中 21件が窓外）。
        cover = asyncio.run(learn_coverage())
        print("契約窓: %s 〜 %s" % cover)
        events = [e for e in all_events
                  if in_coverage(e, cover, slack_days=args.window_slack_days)]
        print("検出イベント %d件のうち、公式が判定できるのは %d件（この中から抽出）"
              % (len(all_events), len(events)))
        if args.source != "any":
            # **その経路が「新たに」拾った社に絞る。** 経路でイベントを選ぶだけでは足りない
            # ——両経路が別の年で同じ社を拾っていると、既に #654 が突合済みの社が
            # 分母へ混ざって一致率が薄まる。社ごと他方の経路を持たないものだけ残す。
            other = {e.edinet_code for e in all_events if e.source != args.source}
            events = [e for e in events
                      if e.source == args.source and e.edinet_code not in other]
            print("  うち source=%s だけで拾った社のイベント: %d件 (%d社)"
                  % (args.source, len(events), len({e.edinet_code for e in events})))
        if not events:
            raise SystemExit("契約窓の内側に検出イベントがありません")
        ev_ecs = {e.edinet_code for e in all_events}

        # 陰性対照: 全隣接年で株数がほぼ動かない社（見逃し率を測る相手）。
        flat: list[str] = []
        by_ec: dict[str, list[AnnualRow]] = defaultdict(list)
        for r in rows:
            by_ec[r.edinet_code].append(r)
        for ec, rs in by_ec.items():
            if ec in ev_ecs:
                continue
            rs = [r for r in sorted(rs, key=lambda r: r.year) if _usable(r) is None]
            if len(rs) >= 3 and all(abs(_log(b.issued_shares / a.issued_shares)) < _log(1.02)
                                    for a, b in zip(rs, rs[1:])):
                flat.append(ec)

        if args.only:
            pos = [e.strip() for e in args.only.split(",") if e.strip()]
            ctrl: list[str] = []
        else:
            pos, ctrl = choose_sample(events, flat, n=args.n, controls=args.controls,
                                      seed=args.seed)
        meta = load_company_meta(db, pos + ctrl)
        targets = [(ec, meta[ec][0]) for ec in pos + ctrl if meta.get(ec, ("", ""))[0]]
        no_sec = [ec for ec in pos + ctrl if not meta.get(ec, ("", ""))[0]]

        print("陽性 %d社 / 対照 %d社 / sec_code 欠落 %d社（母数から除外）"
              % (len(pos), len(ctrl), len(no_sec)))
        for ec in pos:
            print("  + %s %s %s" % (ec, meta.get(ec, ("", ""))[0], meta.get(ec, ("", ""))[1]))
        for ec in ctrl:
            print("  - %s %s %s" % (ec, meta.get(ec, ("", ""))[0], meta.get(ec, ("", ""))[1]))
        if args.dry_run:
            print("dry-run: 銘柄ごとの取得は行いません（窓の学習だけ済ませた・"
                  "本番は約 %.0f 分）" % (len(targets) * 20 / 60.0))
            return 0

        official = asyncio.run(fetch_official_events(
            targets, cover, on_progress=lambda i, t, m: print("  %s" % m, flush=True)))

        results: list[MatchResult] = []
        for e in events:
            if e.edinet_code not in set(pos):
                continue
            results.append(match_event(e, official.get(e.edinet_code, []),
                                       slack_days=args.window_slack_days,
                                       tol=args.match_tol, coverage=cover))
        misses = [ec for ec in ctrl if official.get(ec)]

        tally = Counter(r.status for r in results)
        denom = sum(tally[k] for k in ("agree", "agree_raw_only",
                                       "disagree_magnitude", "no_official_event"))
        rate = (tally["agree"] / denom) if denom else 0.0
        rate_raw = ((tally["agree"] + tally["agree_raw_only"]) / denom) if denom else 0.0

        print()
        print("突合結果: " + ", ".join("%s=%d" % kv for kv in sorted(tally.items())))
        print("一致率（スナップ後）= %.3f / （生比も許容）= %.3f / 分母 %d"
              % (rate, rate_raw, denom))
        print("対照群の見逃し（公式にイベントがあった社）= %d 社 %s"
              % (len(misses), misses or ""))
        for r in results:
            if r.status not in ("agree",):
                print("  %-9s year=%s 検出=%s 生比=%s 公式=%s ev=%d %s"
                      % (r.edinet_code, r.year, _fmt(r.detected), _fmt(r.raw_detected),
                         _fmt(r.official), r.n_official_events, r.status))

        if args.json:
            p = Path(args.json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "coverage": list(cover), "source": args.source,
                "bps_path": bool(args.bps_path),
                "positives": pos, "controls": ctrl,
                "no_sec_code": no_sec, "tally": dict(tally),
                "agree_rate": rate, "agree_rate_raw_ok": rate_raw,
                "control_misses": misses,
                "rows": [r._asdict() for r in results],
            }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print("JSON: %s" % p)
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.measure_split_valuation_bias",
        description="過去断面の per/pbr 等が分割で歪んでいる量を測る（Issue #653・読み取り専用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--year-from", type=int, default=2018)
    common.add_argument("--year-to", type=int, default=None)
    common.add_argument("--min-ratio", type=float, default=DEFAULT_MIN_RATIO)
    common.add_argument("--bps-tol", type=float, default=DEFAULT_BPS_TOL)
    common.add_argument("--snap-tol", type=float, default=DEFAULT_SNAP_TOL)
    # 第2経路（#656）。既定は `DEFAULT_BPS_PATH`＝毎晩の係数表と同じ設定で測れるようにする。
    # ON/OFF を両方回して factors を差分照合するのが「既存の検出が変わっていない」の確かめ方。
    common.add_argument("--bps-path", dest="bps_path", action="store_true",
                        default=DEFAULT_BPS_PATH,
                        help="bs_bps を候補ゲートにする第2経路も使う（#656）")
    common.add_argument("--no-bps-path", dest="bps_path", action="store_false",
                        help="第2経路を使わない（#655 までの検出と同一）")

    d = sub.add_parser("detect", parents=[common], help="全件の検出と指標の出力")
    d.add_argument("--sweep", action="store_true", help="閾値感度表も出す")
    d.add_argument("--price-basis", action="store_true",
                   help="週次と突合して adjusted/raw を判別（週次を読むので既定 OFF）")
    d.add_argument("--min-cross-n", type=int, default=DEFAULT_MIN_CROSS_N)
    d.add_argument("--top", type=int, default=20)
    d.add_argument("--json", nargs="?", const=str(DEFAULT_JSON), default=str(DEFAULT_JSON))

    v = sub.add_parser("verify-sample", parents=[common], help="J-Quants の公式値とサンプル突合")
    v.add_argument("--n", type=int, default=30)
    v.add_argument("--controls", type=int, default=10)
    v.add_argument("--seed", type=int, default=0)
    v.add_argument("--match-tol", type=float, default=0.05)
    v.add_argument("--window-slack-days", type=int, default=45)
    v.add_argument("--source", choices=("any", "shares", "bps"), default="any",
                   help="突合する経路を絞る（#656 の第2経路だけの一致率を出すときは bps）")
    v.add_argument("--only", default="")
    v.add_argument("--dry-run", action="store_true", help="抽出される社だけ出して API を叩かない")
    v.add_argument("--json", nargs="?", const=str(DEFAULT_VERIFY_JSON),
                   default=str(DEFAULT_VERIFY_JSON))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    args = build_parser().parse_args(argv)
    return _cmd_detect(args) if args.cmd == "detect" else _cmd_verify_sample(args)


if __name__ == "__main__":
    raise SystemExit(main())
