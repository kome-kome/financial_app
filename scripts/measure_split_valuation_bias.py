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

**第2経路の倍率は候補ゲートから取らず、翌年の `issued_shares` 比から取る**（#659）。
`bs_bps` は分割以外（内部留保・評価差額）でも増えるので年次比は `F / (1 + g)` になり、
「分割があった」は言えても「何倍か」を決められない（実測の一致率 0.367）。倍率だけを
独立な第3の信号＝1年遅れの株数比へ移し、その信号が無い年は**採らない**。

**第1経路には純資産総額のチェックがある**（#657・既定 `DEFAULT_EQUITY_TOL`・`--equity-tol`）。株数と同じ向きに
`bs_total_equity` が許容を超えて動いたら増資と読んで採らない。ただし `bs_bps ≈ 純資産 / 株数`
が成り立つ社では bps の交差検証と同じものを見ているので、**落とせるのは両者が食い違う社だけ**
で、深い割引の増資（純資産がほとんど増えない）は分離できない。偽陽性率は
`verify-sample --census` が契約窓内の全イベントで測る（抽出では候補が入る保証が無い）。

**上場廃止をまたいで別の実体の行が隣り合うペアは、どちらの経路でも比べない**（#672）。
同じ EDINET コードのまま上場廃止→再上場した社（実測 E05714）は、欠損年をまたいで旧社と新社の行が
ペアになり、株数と `bs_bps` がたまたま逆向きに動くと交差検証を通る。判定は週次株価に1年以上の
空白（系列の開始が遅い、または途中で途切れる）がペアの期間の中にあるかで行う（`listing_gap_in_pair`）。

実行:
    python -m scripts.measure_split_valuation_bias detect
    python -m scripts.measure_split_valuation_bias detect --sweep --price-basis
    python -m scripts.measure_split_valuation_bias detect --bps-path        # 第2経路つき
    python -m scripts.measure_split_valuation_bias verify-sample --dry-run
    python -m scripts.measure_split_valuation_bias verify-sample        # 約14分
    python -m scripts.measure_split_valuation_bias verify-sample --bps-path --source bps
    python -m scripts.measure_split_valuation_bias verify-sample --bps-path --source bps \
        --coverage partial        # 第2経路の倍率を測るときはこちら（#659）
    python -m scripts.measure_split_valuation_bias detect --no-equity-check  # 純資産比チェック無し
    python -m scripts.measure_split_valuation_bias verify-sample --census    # 窓内を全数（約30分）

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
# **True にしたのは実測で倒したからである**（2026-09-12・`verify-sample --bps-path
# --source bps --coverage partial --n 30 --controls 10`）。公式 `AdjFactor` との一致率は
# **0.962（25/26）**・陰性対照の見逃し **0 社**で、#654 の第1経路の 0.967 とほぼ同水準。
#
# **この経路が使えるようになったのは倍率の出どころを変えたからである**（#659）。
# `bs_bps` の年次比を倍率に使っていた頃の一致率は **0.367（11/30）**で、外れた 16 件
# すべてで検出 < 公式だった。`bs_bps` は分割以外（内部留保・有価証券の評価差額）でも
# 増えるので、年次比は真の分割比 F に対し `F / (1 + g)` になる。実測の g は **18%〜67%**
# と幅が広く、隣り合う定番比の間隔（例 2.0 と 2.5）を超える。上向きスナップでも
# **0.778（21/27）**で止まった。**bps 比は「分割があった」は言えるが「何倍か」を
# 決められない**——だから倍率だけを翌年の `issued_shares` 比へ移した（`detect_events`）。
#
# 第3信号を要求したぶん件数は減る（257 イベント/245社 -> 114/113）。**減ったのは
# 倍率の根拠が無い分**で、最新年のイベントは翌年の決算が入れば自動的に係数表へ入る。
DEFAULT_BPS_PATH = True
# 第1経路の純資産総額チェック（#657）の許容。`None` は無効。
# **`rebuild_split_adjustment_factors` が既定のまま呼ぶ＝ここが毎晩の係数表の中身を決める。**
#
# 株数と同じ向きに `bs_total_equity` が `1 + tol` 倍を超えて動いたら、増資（併合側なら減資）と
# 読んで採らない。**この信号で分離できるのは `bs_bps` と純資産総額が食い違う社だけ**である
# ——`bps逆比 / 株数比 = 1 / 純資産比` なので、`bs_bps ≈ 純資産 / 株数` が成り立つ社では
# bps の交差検証（`DEFAULT_BPS_TOL`）が既に純資産の伸びを見ている（2026-09-13 実測: 第1経路
# 492 件の整合度の中央値 1.000・5〜95% 0.951〜1.077）。深い割引の増資は純資産をほとんど
# 増やさないので、どちらの信号でも分離できない。
#
# **1.0（純資産が株数と同じ向きに2倍を超えて動いたら採らない）にしたのは、実測の前に宣言した
# 規則を当てはめた結果である**（2026-09-13・ADR-0055 決定4-4）。格子のうち (a) 契約窓内の全数
# 突合で公式と一致する本物を1件も落とさない (b) 公式に無いイベントを1件以上落とす (c) 窓外で
# 落とす第1経路のイベントに Yahoo の一致する split が無い、を全部満たす最小値を採った。
# 0.25〜0.60 は (c) で落ちた——E36173 は純資産 x1.9848 だが Yahoo に 1:2 分割があり本物。
# 全数突合の偽陽性 6/94 のうち、この値で落とせるのは E05716（純資産 x2.1611）の1件だけで、
# **E01121 は偽陽性と確定したが落とせない**（x1.3027 で落とすと E36173 も落ちる）。
# 余裕は両側とも薄い（E36173 まで 0.015・E05716 まで 0.16）ので、動かすなら測り直すこと。
DEFAULT_EQUITY_TOL: Optional[float] = 1.0
# 感度表と既定判定に使う格子。0.15 は本物の分割の伸び（第1経路 split の p95 1.168）に掛かる。
EQUITY_TOL_GRID: tuple[float, ...] = (0.15, 0.25, 0.40, 0.60, 1.00)
# 合成（分割＋増資）とみなす残差の範囲。これを外れたら丸めずに unsnapped で別枠へ出す。
COMPOSITE_LO, COMPOSITE_HI = 0.8, 1.25

# 上場廃止をまたぐペアの判定（#672・ADR-0055 決定4-7・`listing_gap_in_pair`）。
# **欠損年の長さだけでは切らない**——本物の分割にも欠損年はある（XBRL の取りこぼし・決算期変更。
# 実測 E03078 は 2018→2020 で gap_years 2）。長さは「少なくとも1期ぶんの通期行が無い」という
# 前提条件にだけ使い、別実体と読む根拠は**ペアの期間の中にこの社の市場価格が無い区間がある**
# ことに置く。上場廃止→再上場は週次株価に2つの形で現れる: 系列の開始が遅い（旧社の価格が
# 表に無い・E05714）と、系列の途中で途切れる（旧社の価格が残る・E03530 SBI新生銀行）。
# 365 日は「少なくとも1年、この社に市場価格が無かった」＝欠損した1期と釣り合う長さ。
# 実測（2026-09-14）で annual 全 30,379 行の隣接ペアのうち該当は E05714 2020→2026 と
# E03530 2023→2026 の2件（後者は交差検証で落ちるので今はイベントにならない）。
LISTING_GAP_MIN_YEARS = 2
LISTING_GAP_MIN_DAYS = 365

# 実在する分割・併合比。0.05 は 1:20 併合。
#
# **比を足すのは「検出したイベントの本物の比が公式 `AdjFactor` か Yahoo で確かめられ、かつ
# 足した前後で F が変わる行が全部確かめられたとき」だけ**（#669・ADR-0055 決定4-6／4-7）。
# 表の比は既存イベントの丸め先も変える（間に比が入ると composite の寄り先が動く）ので、
# 1社の裏付けだけで足すと、分割でない株数の動きまで補正へ入ることがある。足したら
# `verify-sample --census` の一致率・偽陽性をやり直すこと。
#   1/3  : E03717 unbanked の 3:1 併合（2024-09-27・公式 1/3・Yahoo 1:3）
#   1/15 : E37831 INEST の 15:1 併合（2025-09-29・公式 1/15・Yahoo 1:15）
#   15   : E05698 UT グループの 1:15（2025-12-29・公式/Yahoo とも 15）。#669 では E05714
#          （2020年に上場廃止し 2026年に別の株数で再上場・分割なし）が composite で巻き込まれる
#          ので保留し、#672 で上場廃止をまたぐペアを比べないようにしてから足した。第2経路で
#          E23634 アミタ HD も 15 で入るが、Yahoo の 5:1（2021-12-29）と 3:1（2022-09-29）は
#          どちらも F が変わる 2018〜2020 年の行より後にあるので、その3行の F=15 は正しい。
# **6 は足していない**——E05426 の 5.768 は 1:5 分割（Yahoo 2024-03-28）＋増資で、
# 今の composite 5.0 が正しい。
CANONICAL_RATIOS: tuple[float, ...] = (
    1.1, 1.2, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0, 15.0,
    1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 1 / 15, 1 / 20,
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
    # 純資産総額（#657）。末尾に既定値付きで置くのは、12 列の位置指定で作る既存の呼び出しを
    # 壊さないため。
    bs_total_equity: Optional[float] = None


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
    # 第2経路の倍率を決めた第3の信号＝**翌年**の `issued_shares` 比（#659）。
    # 第1経路では None（倍率は当年の `sh_ratio` そのもの）。
    lagged_sh_ratio: Optional[float] = None
    # 第1経路のペアの純資産総額比（#657）。チェックの有無によらず記録する（判定できなければ
    # None）。第2経路では None——倍率の出どころが別の年のペアなので、同じ量ではない。
    equity_ratio: Optional[float] = None
    # 第2経路で**翌年の行が無い**ときに倍率を決めた公式 `AdjFactor` の株数比（#661）。
    # イベント窓の中の公式イベントの積の逆数。`lagged_sh_ratio` とは排他（翌年があれば翌年を使う）。
    official_ratio: Optional[float] = None


class MatchResult(NamedTuple):
    edinet_code: str
    year: int
    detected: float               # canonical（無ければ生比）
    raw_detected: float           # スナップ前の生比（経路の候補ゲートに使った量）
    official: Optional[float]
    n_official_events: int
    status: str
    # status: agree | agree_raw_only | disagree_magnitude | no_official_event
    #         | no_official_event_partial | official_only | out_of_coverage | no_sec_code


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


def equity_ratio(prev: AnnualRow, cur: AnnualRow) -> Optional[float]:
    """純資産総額の前年比。どちらかが欠損・0 以下なら判定できないので None。"""
    a, b = prev.bs_total_equity, cur.bs_total_equity
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return b / a


def equity_contradicts_split(sh_ratio: float, eq_ratio: float, tol: float) -> bool:
    """株数の動きを「分割ではなく増資（併合側なら減資）」と読む純資産の動きか（#657）。

    **片側でしか判定しない。** 分割は純資産を変えないが、増資は株数と純資産を同じ向きに動かす。
    株数が増えた年に純資産が減るのは赤字・減損であって、増資の証拠にはならない。
    """
    le = _log(eq_ratio)
    return le * _log(sh_ratio) > 0 and abs(le) > _log(1 + tol)


def listing_gap_in_pair(prev: AnnualRow, cur: AnnualRow,
                        series: Optional[tuple[str, Sequence[tuple[str, str]]]],
                        coverage_start: Optional[str], *,
                        min_gap_years: int = LISTING_GAP_MIN_YEARS,
                        min_hole_days: int = LISTING_GAP_MIN_DAYS
                        ) -> Optional[tuple[Optional[str], str]]:
    """このペアは上場廃止をまたいで別の実体の行が隣り合ったものか（#672）。該当すれば価格の空白を返す。

    同じ EDINET コードのまま上場廃止→再上場すると、欠損年をまたいで旧社と新社の行がペアになる
    （実測 E05714: 2020-03-31 の旧ソニーフィナンシャルHD と 2026-03-31 の新社・株数比 15.56）。
    **ペアの期間の中に、この社の市場価格が1年以上無く、当期末までに価格が戻っている区間があれば、
    その間この銘柄は取引されていなかった**と読む。条件は次の2つ:

    - `gap_years >= min_gap_years`（少なくとも1期ぶんの通期行が無い）
    - 価格の空白 (a, b) のうち、b（価格が戻った週）が窓 `(max(前期末, coverage_start), 当期末]` の
      中にあり、窓の中に入っている長さ `b - max(a, 窓の始まり)` が `min_hole_days` 日以上のものがある

    空白は2種類: 系列の開始前 `(None, 最初の週)`（旧社の価格が表に無い・E05714）と、系列の途中の
    `series[1]` の各区間（旧社の価格が残っている・E03530）。`series` の形は
    `(最初の week_start, ((空白直前の週, 空白直後の週), ...))`（`database.load_price_series`）。

    戻り値は該当した空白 `(a, b)`（系列の開始前なら a は None）。該当しなければ None。

    価格が当期末より後に戻る空白は数えない——当期末の行が上場廃止中に提出されたものなら、旧社と
    同じ実体でありうるので何も言えない。`coverage_start` は週次株価表全体の開始日＝「価格が無い」を
    観測できる下限で、それより前はどの社にも価格が無いので空白に数えない。**系列を持たない社
    （`series` が None）は判定しない**——上場廃止済みで価格が消えた社と区別できないので、今日どおり
    比べる側へ倒す。
    """
    if series is None or coverage_start is None:
        return None
    if cur.year - prev.year < min_gap_years:
        return None
    p0, p1 = _iso(prev.period_end), _iso(cur.period_end)
    if not p0 or not p1:
        return None
    start, holes = series
    w0 = max(date.fromisoformat(p0), date.fromisoformat(coverage_start[:10]))
    w1 = date.fromisoformat(p1)
    for a, b in ((None, start), *holes):
        back = date.fromisoformat(b[:10])
        if not (w0 < back <= w1):
            continue
        since = w0 if a is None else max(date.fromisoformat(a[:10]), w0)
        if (back - since).days >= min_hole_days:
            return (a, b)
    return None


def detect_events(rows: Sequence[AnnualRow], *,
                  min_ratio: float = DEFAULT_MIN_RATIO,
                  bps_tol: float = DEFAULT_BPS_TOL,
                  snap_tol: float = DEFAULT_SNAP_TOL,
                  bps_path: bool = DEFAULT_BPS_PATH,
                  equity_tol: Optional[float] = DEFAULT_EQUITY_TOL,
                  official_events: Optional[Mapping[str, Sequence[tuple[str, float]]]] = None,
                  price_series: Optional[Mapping[str, tuple[str, Sequence[tuple[str, str]]]]] = None,
                  ) -> tuple[list[ShareEvent], dict]:
    """株数基準が変わった年を検出する。戻り値 (events, stats)。

    経路は2本あり、**どちらも「候補ゲート1つ ＋ 独立した第2の書き手による交差検証1つ」**と
    いう同じ形をしている。閾値が偽陽性を止めているのではなく、交差検証が止めている。

    - 第1経路（`source="shares"`）: `issued_shares` の年次比が閾値を超えること（候補ゲート）と、
      `bs_bps` の逆比が同じ倍率で一致すること（交差検証）。増資・自社株買いはここで落ちる。
    - 第2経路（`source="bps"`・#656）: `bs_bps` の年次比が閾値を超えること（候補ゲート）と、
      `pl_eps` の比が同じ倍率で一致すること（交差検証）。減損・大幅赤字はここで落ちる。

    **倍率の出どころは経路ごとに違う**（#659）。第1経路は候補ゲートに使った `sh_ratio` を
    そのまま倍率にできるが、第2経路はできない。`bs_bps` は分割以外（内部留保・有価証券の
    評価差額）でも増えるので、年次比は真の分割比 F に対し `F / (1 + g)` になる。実測の g は
    18%〜67% と幅が広く、隣り合う定番比の間隔（例 2.0 と 2.5）を超えるため、スナップ先が
    系統的に1段小さい側へ落ちる（公式との一致率 0.367・外れた16件すべてで検出 < 公式）。
    そこで第2経路の倍率は**翌年の `issued_shares` 比という独立な第3の信号**から取る。
    しまむら型は bps が動いた翌年に株数が動くので、そこに真の分割比が現れる。
    **第3の信号が取れない年（翌年の行がまだ無い等）は採らない**——倍率を間違えた補正は
    系統誤差を別の系統誤差へ置き換えるだけで、ADR-0055 決定4 が採った取引とは別物になる。
    係数表は毎晩全置換なので、翌年の決算が入れば自動で補正が入る。

    **第2経路が要るのは、株数と1株指標が同じ年に動くとは限らないから。** 分割を 1 株指標には
    反映しているのに `issued_shares` が据え置きの社は第1経路の候補にすら上がらない
    （実測 E03137 しまむらは `shares x1.0000` のまま `bps x1.8670 / eps x1.8971`）。
    逆にこの社は株数が動いた年の `bs_bps` が**上がって**いるため、第1経路の交差検証でも落ちる。
    同一年ペアの中で株数と bps を突き合わせる設計では原理的に拾えない。

    **同じ (edinet_code, year) を両経路が拾ったら第1経路を採る**（畳む）。株数は分割で必ず動く
    量で、bps のように内部留保や配当で毎年動く量より基準として素直だからである。

    欠損年があるときは `year-1` ではなく**直前の使える行**とペアを組み、`gap_years` を残す。
    2以上なら複数イベントの積を1件と見ている可能性があるので、突合サンプルへ優先的に入れる。

    **`equity_tol` を与えると、第1経路に純資産総額のチェックが加わる**（#657）。bps の交差検証を
    通ったあと、株数と同じ向きに `bs_total_equity` が `1 + equity_tol` 倍を超えて動いていたら
    採らない（`stats["equity"]["rejected"]` に残す）。純資産が欠損・0 以下で判定できない社は
    **今日までどおり採って** `n_unknown` に数える——「増資でない」とも「増資だ」とも読まない。
    第2経路には掛けない（倍率は翌年のペアから取る別の主張で、成長企業の本物の分割を巻き込む）。
    第1経路が落としたペアを第2経路が独立に拾うことは妨げない（畳むのは第1経路が**採った**とき
    だけ、という上の規則と揃える）。

    **`official_events` を与えると、翌年の行が無い第2経路のペアだけ公式 `AdjFactor` から倍率を
    取る**（#661・ADR-0055 決定4-5）。形は `{edinet_code: [(日付, AdjFactor), ...]}`。
    イベント窓（`event_window`）の中の公式イベントの積の逆数を第3の信号として、動いているか・
    向きが合うかを翌年の株数と同じ規則で判定し、**比は定番比へ丸めずそのまま倍率にする**
    （`AdjFactor` は株価の遡及調整に使われた係数そのもので、増資の分が混ざらない）。**窓の中に公式イベントが
    無ければ今日までどおり採らない**——行が無いことを「分割は無かった」とは読まない（取り込み前・
    夜の取りこぼし・エンバーゴ中と区別できない）ので、公式が落ちた晩も補正が誤るのではなく
    採らない側へ倒れる。翌年の行があるペアは公式を倍率に使わず、食い違いを
    `stats["bps_path"]["official_crosscheck"]` に数えるだけにする。
    None（既定）なら公式を一切見ない＝測定器の CLI はこちらを使う。公式から倍率を決めた
    イベントを公式と突合すると、定義上必ず一致して一致率が黙って膨らむためである。

    **`price_series` を与えると、上場廃止をまたいで別の実体の行が隣り合うペアを比べない**
    （#672・ADR-0055 決定4-7）。形は
    `{edinet_code: (最初の week_start, ((空白直前の週, 空白直後の週), ...))}`
    （`database.load_price_series`）。判定は `listing_gap_in_pair` で、週次株価表全体の開始日は
    各社の最初の週の最小値を使う。該当ペアは**どちらの経路にも渡さず**
    `stats["listing_gap"]["rejected"]` に残す。第2経路が翌年の株数を先読みするときも、
    (当年, 翌年) が該当ペアなら「同じ実体の翌年の行は無い」として扱う（翌年の行が無い年と同じ
    分岐＝公式か倍率待ちへ）。写像に居ない社は判定しない。None（既定）なら判定しない。
    """
    gate = _log(min_ratio)
    coverage_start = min(v[0] for v in price_series.values()) if price_series else None
    listing_gap_rejected: list[dict] = []
    n_lagged_listing_gap = 0
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
    # bps 経路が「翌年の株数の動き」を倍率に使ったとき、その動きの年を覚えておく。
    # 第1経路が同じ動きを別イベントとして採っていたら、あとで畳む（#659）。
    lagged_year_of: dict[int, int] = {}          # events の添字 -> 倍率に使った翌年の year
    shares_pairs: set[tuple[str, int]] = set()   # 第1経路が採った (ec, year)
    equity_rejected: list[dict] = []
    n_equity_unknown = 0
    # 倍率の出どころが無くて採らなかった第2経路のペア（#661）。取り込み CLI の対象選びと
    # 夜間ログが読む。件数は `bps_rejected["no_lagged_row"]` と常に一致する。
    awaiting: list[dict] = []

    for ec, rs in by_ec.items():
        # **使える行だけを先に並べる。** 翌年の株数を見るには次の行を先読みする必要があり、
        # 逐次ループのままでは書けない（`prev` は持てても `next` は持てない）。
        usable: list[AnnualRow] = []
        for cur in sorted(rs, key=lambda r: r.year):
            why = _usable(cur)
            if why:
                skipped[why] += 1
                continue
            usable.append(cur)

        series = price_series.get(ec) if price_series else None
        for i in range(1, len(usable)):
            prev, cur = usable[i - 1], usable[i]
            hole = listing_gap_in_pair(prev, cur, series, coverage_start)
            if hole is not None:
                # 旧社と新社の行を比べても分割の証拠にならない（#672）。**経路に渡す前に外す**
                # ——株数も bps も実体が替わったぶん動くので、どちらの交差検証も素通りしうる。
                listing_gap_rejected.append({
                    "edinet_code": ec, "year": cur.year, "prev_year": prev.year,
                    "prev_period_end": _iso(prev.period_end), "period_end": _iso(cur.period_end),
                    "no_price_after": hole[0], "price_back": hole[1],
                    "sh_ratio": cur.issued_shares / prev.issued_shares,
                    "bps_ratio": prev.bs_bps / cur.bs_bps,
                })
                continue
            nxt = usable[i + 1] if i + 1 < len(usable) else None
            next_crosses = (nxt is not None
                            and listing_gap_in_pair(cur, nxt, series, coverage_start) is not None)
            if next_crosses:
                nxt = None          # 別の実体の翌年行は、倍率を決める第3の信号にならない
            took = False
            sh_ratio = cur.issued_shares / prev.issued_shares
            bps_ratio = prev.bs_bps / cur.bs_bps
            if abs(_log(sh_ratio)) >= gate:
                n_candidates += 1
                candidate_ecs.add(ec)
                if abs(bps_ratio / sh_ratio - 1.0) <= bps_tol:
                    canonical, residual, kind = snap_to_canonical(sh_ratio, tol=snap_tol)
                    eq_ratio = equity_ratio(prev, cur)
                    if equity_tol is not None and eq_ratio is None:
                        n_equity_unknown += 1
                    if (equity_tol is not None and eq_ratio is not None
                            and equity_contradicts_split(sh_ratio, eq_ratio, equity_tol)):
                        # 純資産が株数と一緒に動いた＝増資（併合側なら減資）と読む（#657）。
                        # 種別はスナップしてから決めたものを残す（どの種別を落としたかが要る）。
                        equity_rejected.append({
                            "edinet_code": ec, "year": cur.year, "prev_year": prev.year,
                            "period_end": cur.period_end, "prev_period_end": prev.period_end,
                            "kind": kind, "canonical": canonical, "sh_ratio": sh_ratio,
                            "bps_ratio": bps_ratio, "equity_ratio": eq_ratio,
                        })
                    else:
                        events.append(ShareEvent(
                            edinet_code=ec, year=cur.year, prev_year=prev.year,
                            gap_years=cur.year - prev.year,
                            period_end=cur.period_end, prev_period_end=prev.period_end,
                            sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                            canonical=canonical, residual=residual, kind=kind,
                            source="shares", equity_ratio=eq_ratio))
                        shares_pairs.add((ec, cur.year))
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
                elif nxt is None:
                    if next_crosses:
                        n_lagged_listing_gap += 1
                    # **翌年の株数が無い。** 最新年のイベントはここへ来る（翌年の決算がまだ
                    # 提出されていない）。公式 `AdjFactor` が窓の中にあればそれを第3の信号に
                    # する（#661）。無ければ採らずに次回へ送る＝係数表は毎晩全置換なので、
                    # 公式が入るか翌年の行が入れば自動で補正が入る（#659）。
                    cand = ShareEvent(
                        edinet_code=ec, year=cur.year, prev_year=prev.year,
                        gap_years=cur.year - prev.year,
                        period_end=cur.period_end, prev_period_end=prev.period_end,
                        sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                        canonical=None, residual=bps_ratio, kind="unsnapped", source="bps")
                    off, _ = (official_ratio_in_window(official_events.get(ec, ()),
                                                       event_window(cand))
                              if official_events is not None else (None, 0))
                    if off is None:
                        bps_rejected["no_lagged_row"] += 1
                        awaiting.append({
                            "edinet_code": ec, "year": cur.year,
                            "prev_period_end": _iso(prev.period_end),
                            "period_end": _iso(cur.period_end), "bps_ratio": bps_ratio,
                        })
                    elif abs(_log(off)) < gate:
                        # 公式は窓の中で動いているが、1株指標の動きを説明するほどではない
                        # （1:1.1 の無償割当など）。`lagged_flat` と同じ理由で採らない。
                        bps_rejected["official_flat"] += 1
                    elif _log(off) * _log(bps_ratio) < 0:
                        bps_rejected["official_direction"] += 1
                    else:
                        # **公式の比は定番比へ丸めない。** 丸めは「株数比に増資の分が混ざる」のを
                        # 切り離すための仕組みで、`AdjFactor` は株価の遡及調整に使われた係数
                        # そのもの＝F の正本である。丸めると定番比の表に無い 1:6 が 5 へ寄って
                        # F が 17% 小さく入り、1:7 は採られない（実測 2026-09-13: E38205 /
                        # E38979 / E02128）。
                        events.append(cand._replace(
                            canonical=off, residual=1.0,
                            kind="split" if off > 1 else "reverse", official_ratio=off))
                else:
                    lag = nxt.issued_shares / cur.issued_shares
                    if abs(_log(lag)) < gate:
                        # 翌年も株数が動いていない。1株指標だけが動いた理由を分割だと
                        # 言い切れる材料が無いので採らない。
                        bps_rejected["lagged_flat"] += 1
                    elif _log(lag) * _log(bps_ratio) < 0:
                        # 向きが逆（bps は分割方向・株数は併合方向）。同じ事象ではない。
                        bps_rejected["lagged_direction"] += 1
                    else:
                        canonical, residual, kind = snap_to_canonical(lag, tol=snap_tol)
                        if canonical is None:
                            bps_rejected["lagged_unsnapped"] += 1
                        else:
                            # **倍率は `lag` から決め、`bps_ratio` / `sh_ratio` は観測値の
                            # まま残す**（あとから「当年は株数が動いていない」が読める）。
                            lagged_year_of[len(events)] = nxt.year
                            events.append(ShareEvent(
                                edinet_code=ec, year=cur.year, prev_year=prev.year,
                                gap_years=cur.year - prev.year,
                                period_end=cur.period_end, prev_period_end=prev.period_end,
                                sh_ratio=sh_ratio, bps_ratio=bps_ratio,
                                canonical=canonical, residual=residual, kind=kind,
                                source="bps", lagged_sh_ratio=lag))

    # **同じ株数の動きを2回数えない。** bps 経路が翌年の動きを倍率に使い、かつ第1経路が
    # その翌年を独立したイベントとして採っていたら、`cumulative_factors` が比を二乗する
    # （`dup_with_shares` と同じ実害）。第1経路を残す側に倒すのは、株数が分割で必ず動く量で
    # 基準として素直だからである。判定はループを抜けてから——第1経路が採るかどうかは
    # 翌年のペアを処理するまで決まらない。
    if lagged_year_of:
        kept: list[ShareEvent] = []
        for i, e in enumerate(events):
            ly = lagged_year_of.get(i)
            if ly is not None and (e.edinet_code, ly) in shares_pairs:
                bps_rejected["dup_lagged_with_shares"] += 1
                continue
            kept.append(e)
        events = kept

    # 翌年の株数から倍率を決めたイベントを公式と突き合わせる（#661）。**イベントは変えず数えるだけ**
    # ——両方が取れる年で2つの書き手が合っているかを、毎晩追加の取得なしに測り続けるため。
    # 公式イベントが窓に無いものは数えない（取り込み前・エンバーゴ中と区別できない）。
    crosscheck = {"agree": 0, "disagree": 0, "disagreements": []}
    if official_events is not None:
        for e in events:
            if e.source != "bps" or e.lagged_sh_ratio is None:
                continue
            m = match_event(e, official_events.get(e.edinet_code, ()))
            if m.official is None:
                continue
            if m.status == "agree":
                crosscheck["agree"] += 1
            else:
                crosscheck["disagree"] += 1
                crosscheck["disagreements"].append({
                    "edinet_code": e.edinet_code, "year": e.year, "status": m.status,
                    "lagged": e.canonical, "official": m.official,
                })

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
            # 倍率の出どころ（#661）。official は翌年の行が無い年に公式 AdjFactor で埋めた件数。
            "magnitude_source": {
                "lagged_shares": sum(1 for e in bps_events if e.lagged_sh_ratio is not None),
                "official": sum(1 for e in bps_events if e.official_ratio is not None),
            },
            "awaiting_magnitude": awaiting,
            "official": {
                "enabled": official_events is not None,
                "crosscheck": crosscheck,
            },
        },
        "equity": {
            "enabled": equity_tol is not None,
            "tol": equity_tol,
            "n_rejected": len(equity_rejected),
            "rejected_by_kind": dict(Counter(r["kind"] for r in equity_rejected)),
            "n_unknown": n_equity_unknown,
            "rejected": equity_rejected,
        },
        "listing_gap": {
            "enabled": price_series is not None,
            "coverage_start": coverage_start,
            "n_rejected": len(listing_gap_rejected),
            "rejected": listing_gap_rejected,
            # 第2経路が翌年の株数を先読みしようとして、そのペアが該当したので使わなかった件数。
            "n_lagged": n_lagged_listing_gap,
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


def official_ratio_in_window(official: Sequence[tuple[str, float]],
                             window: Optional[tuple[str, str]]
                             ) -> tuple[Optional[float], int]:
    """窓 (w0, w1] の中の公式イベントを株数比へ直した値と件数。無ければ (None, 0)。

    公式の `AdjFactor` は**過去株価に掛ける係数**なので 1:2 分割は 0.5 で返る。株数比へ
    直すため逆数を取る。同一窓に複数イベントがあれば積になる（DB 側の年次比も積なので整合する）。
    突合（`match_event`）と本番の倍率決め（`detect_events`・#661）が共有する＝窓の意味を割らない。
    """
    if window is None:
        return None, 0
    w0, w1 = window
    inside = [f for d, f in official if w0 < d <= w1 and f and f > 0]
    if not inside:
        return None, 0
    prod = 1.0
    for f in inside:
        prod *= f
    return 1.0 / prod, len(inside)


def in_coverage(ev: ShareEvent, coverage: Optional[tuple[str, str]], *,
                slack_days: int = 45, mode: str = "full") -> bool:
    """公式がこのイベントを判定できるか。

    `mode="full"`（既定）は窓が契約期間へ**完全に**収まるときだけ True。公式にイベントが
    無ければ「分割は無かった」と読めるので、偽陽性率まで測れる代わりに範囲が狭い。

    `mode="partial"` は窓が契約期間と**重なって**いれば True（#659）。
    **倍率が合っているかだけを測るための緩め方**で、重なりの中で公式イベントが見つかった件
    だけを分母に数える（見つからなかった件は「分割が無かった」と「契約窓の外で起きた」を
    区別できないので分母から外す＝`match_event` が `no_official_event_partial` を返す）。

    なぜ緩める必要があるか: 第2経路の倍率は翌年の `issued_shares` から取るので、突合には
    「公式が判定できる」と「翌年の行が提出済み」の両方が要る。契約窓は直近2年なので
    full では当期末が窓の後ろ寄りのイベントしか残らず、その翌年の決算はまだ存在しない
    ——**2つの条件は full のままでは今日の時点で排他**である（実測 2026-09-12: 契約窓
    2024-06-20〜2026-06-20 に対し、窓内の bps 経路イベント 57件はいずれも最新年）。
    """
    win = event_window(ev, slack_days=slack_days)
    if win is None:
        return False
    if not coverage or not coverage[0] or not coverage[1]:
        return True
    if mode == "partial":
        return win[0] <= coverage[1] and coverage[0] <= win[1]
    return coverage[0] <= win[0] and win[1] <= coverage[1]


def match_event(ev: ShareEvent, official: Sequence[tuple[str, float]], *,
                slack_days: int = 45, tol: float = 0.05,
                coverage: Optional[tuple[str, str]] = None,
                coverage_mode: str = "full") -> MatchResult:
    """検出したイベントを公式 `AdjFactor` と突き合わせる。

    公式の `AdjFactor` は**過去株価に掛ける係数**なので 1:2 分割は 0.5 で返る。株数比へ
    直すため逆数を取る。同一窓に複数イベントがあれば積になり、DB 側の年次比も積なので整合する。

    窓は (前期末 - slack, 当期末 + slack]。分割の効力発生日と株数の計上期のズレを吸収する。
    契約窓の外は `out_of_coverage` にして**一致率の分母から外す**（混ぜると理由なく下がる）。

    `coverage_mode="partial"` では窓と契約窓の重なりの中だけを探し、公式イベントが無ければ
    `no_official_event_partial` を返す（#659）。**この status は分母に入れない**——重なりの
    外で起きた分割は公式が返さないので、「分割が無かった」と区別できないためである。
    """
    # 生比は**そのイベントの倍率を決めた量**を採る。bps 経路は #659 で倍率の出どころが
    # 翌年の株数比へ移ったので、そちらを見る（`bps_ratio` を見ると `agree_raw_only` が
    # 「もう倍率に使っていない量では合う」を数えることになり、意味が黙って壊れる）。
    # 第1経路は候補ゲート＝倍率なので `sh_ratio` のまま。
    if ev.source == "bps":
        # 翌年の株数 → 公式（#661・翌年の行が無い年だけ） → bps の年次比、の順。
        raw = next(r for r in (ev.lagged_sh_ratio, ev.official_ratio, ev.bps_ratio)
                   if r is not None)
    else:
        raw = ev.sh_ratio
    detected = ev.canonical if ev.canonical is not None else raw
    win = event_window(ev, slack_days=slack_days)
    if win is None or not in_coverage(ev, coverage, slack_days=slack_days,
                                      mode=coverage_mode):
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           None, 0, "out_of_coverage")
    w0, w1 = win
    if coverage_mode == "partial" and coverage and coverage[0] and coverage[1]:
        w0, w1 = max(w0, coverage[0]), min(w1, coverage[1])

    official_ratio, n_inside = official_ratio_in_window(official, (w0, w1))
    if official_ratio is None:
        return MatchResult(
            ev.edinet_code, ev.year, detected, raw, None, 0,
            "no_official_event_partial" if coverage_mode == "partial"
            else "no_official_event")

    lim = _log(1 + tol)
    if abs(_log(official_ratio / detected)) <= lim:
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           official_ratio, n_inside, "agree")
    if abs(_log(official_ratio / raw)) <= lim:
        # 生比では合うがスナップ後で外れる＝スナップが悪さをしている側。分けて数える。
        return MatchResult(ev.edinet_code, ev.year, detected, raw,
                           official_ratio, n_inside, "agree_raw_only")
    return MatchResult(ev.edinet_code, ev.year, detected, raw,
                       official_ratio, n_inside, "disagree_magnitude")


#: 一致率の分母に入れる status。**ここが分母の唯一の源**で、CLI へ書き写さない。
#: `no_official_event` が入るのは full 窓のときだけ——公式が返さない＝分割が無かったと
#: 読めるので「検出が間違い」として数える。partial 窓の同じ状況は契約窓の外で起きた分割と
#: 区別できないので `no_official_event_partial` にして分母から外す（#659）。
MATCH_DENOMINATOR = ("agree", "agree_raw_only", "disagree_magnitude", "no_official_event")


def tally_rates(results: Sequence[MatchResult]) -> tuple[Counter, int, float, float]:
    """突合結果を数える。戻り値 (tally, denom, 一致率, 生比も許容した一致率)。

    CLI から切り出してあるのは**分母の規則をテストで固定するため**。
    分母が1件も無ければ率は 0.0（0除算を避ける）。
    """
    tally: Counter = Counter(r.status for r in results)
    denom = sum(tally[k] for k in MATCH_DENOMINATOR)
    rate = (tally["agree"] / denom) if denom else 0.0
    rate_raw = ((tally["agree"] + tally["agree_raw_only"]) / denom) if denom else 0.0
    return tally, denom, rate, rate_raw


def tally_by_group(results: Sequence[MatchResult], events: Sequence[ShareEvent]
                   ) -> dict[str, dict[str, int]]:
    """突合結果を `経路:種別` ごとに数える（#657）。偽陽性がどの種別に偏るかを見るため。"""
    group_of = {(e.edinet_code, e.year): "%s:%s" % (e.source, e.kind) for e in events}
    out: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        out[group_of.get((r.edinet_code, r.year), "?")][r.status] += 1
    return {k: dict(v) for k, v in out.items()}


#: 落とすと害になる status（公式と倍率が一致した本物）と、落とすと利益になる status
#: （公式に分割が無い＝偽陽性）。`disagree_magnitude` はどちらにも入れない——落としても
#: F=1.0 という別の誤りに置き換わるだけで、良くなったとも悪くなったとも言えない。
GATE_HARM_STATUSES = ("agree", "agree_raw_only")
GATE_BENEFIT_STATUSES = ("no_official_event",)


def equity_gate_crosstab(results: Sequence[MatchResult], ungated: Sequence[ShareEvent],
                         gated: Sequence[ShareEvent]) -> dict:
    """純資産比チェックが**実際に消した**イベントを、突合ステータスで数える（#657）。

    `ungated` はチェック無し・`gated` はチェック有りの検出結果（他の設定は同じ）。
    消えたかどうかは `(edinet_code, year)` にイベントが残っているかで決める——第1経路が
    落としたペアを第2経路が拾い直したら、そのイベントは消えていない（`readmitted`）。
    逆に、第1経路が落としたことで畳まれなくなった第2経路のイベントは `added` に出す
    （黙って増やさない）。突合の対象外（契約窓の外など）で消えたものは `not_in_census`。
    """
    src_before = {(e.edinet_code, e.year): e.source for e in ungated}
    src_after = {(e.edinet_code, e.year): e.source for e in gated}
    dropped = sorted(k for k in src_before if k not in src_after)
    readmitted = sorted(k for k in src_before
                        if k in src_after and src_after[k] != src_before[k])
    added = sorted(k for k in src_after if k not in src_before)
    status_of = {(r.edinet_code, r.year): r.status for r in results}
    dropped_status = Counter(status_of.get(k, "not_in_census") for k in dropped)
    return {
        "dropped": dropped,
        "dropped_status": dict(dropped_status),
        "harm": sum(dropped_status[s] for s in GATE_HARM_STATUSES),
        "benefit": sum(dropped_status[s] for s in GATE_BENEFIT_STATUSES),
        "not_in_census": [k for k in dropped if k not in status_of],
        "readmitted": readmitted,
        "added": added,
    }


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
    eq = det.get("equity") or {}
    if eq.get("enabled"):
        add("  純資産比チェック(第1経路): tol=%s -> 棄却=%s %s / 判定不能=%s"
            % (eq.get("tol"), eq.get("n_rejected"), eq.get("rejected_by_kind") or {},
               eq.get("n_unknown")))
        for r in eq.get("rejected") or []:
            add("    - %-9s %4s %-10s 株数 x%.4f / bps逆比 x%.4f / 純資産 x%.4f"
                % (r["edinet_code"], r["year"], r["kind"], r["sh_ratio"], r["bps_ratio"],
                   r["equity_ratio"]))
    else:
        add("  純資産比チェック: 無効 (--equity-tol で有効化)")
    lg = det.get("listing_gap") or {}
    if lg.get("enabled"):
        add("  上場廃止をまたぐペア(比べない): %s件 / 翌年先読みで外した %s件 / 週次の開始 %s"
            % (lg.get("n_rejected"), lg.get("n_lagged"), lg.get("coverage_start")))
        for r in lg.get("rejected") or []:
            add("    - %-9s %4s->%4s 価格の空白 %s -> %s / 株数 x%.4f / bps逆比 x%.4f"
                % (r["edinet_code"], r["prev_year"], r["year"],
                   r["no_price_after"] or "系列開始前", r["price_back"],
                   r["sh_ratio"], r["bps_ratio"]))
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
       stock_price, per, pbr, div_yield, market_cap, bs_total_equity
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
        bs_total_equity=r[12],
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
        # 本番の係数表と同じ検出で測る（#672）。読み終えたら commit する（GOTCHAS #411）。
        series = D.load_price_series(db, min_hole_days=LISTING_GAP_MIN_DAYS)
        db.commit()
        print("annual %d行を読み込み（接続先=%s）" % (len(rows), D.DB_TARGET), flush=True)

        events, stats = detect_events(rows, min_ratio=args.min_ratio,
                                      bps_tol=args.bps_tol, snap_tol=args.snap_tol,
                                      bps_path=args.bps_path, equity_tol=args.equity_tol,
                                      price_series=series)
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
                         "bps_path": bool(args.bps_path), "equity_tol": args.equity_tol},
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
                                           snap_tol=args.snap_tol, bps_path=args.bps_path,
                                           equity_tol=args.equity_tol,
                                           price_series=series)
                    ff = cumulative_factors(rows, ev)
                    n2 = sum(1 for v in ff.values() if v >= 2.0)
                    cells.append("%4d/%4d/%5d" % (st["n_events"], st["n_event_companies"], n2))
                print("  min_ratio=%-5g | %s" % (mr, " | ".join(cells)))
            print("  (列は bps_tol=0.05 / 0.10 / 0.15 / 0.25・equity_tol=%s)" % args.equity_tol)

            # 純資産比の軸は別表にする（#657）。3 軸の格子は読めない。他の設定は現在値に固定。
            print()
            print("純資産比の感度 (min_ratio=%g / bps_tol=%g 固定 -> "
                  "通過イベント/社数/F>=2 の被害行 | 被害行/社 | 棄却の種別内訳)"
                  % (args.min_ratio, args.bps_tol))
            for et in (None,) + EQUITY_TOL_GRID:
                ev, st = detect_events(rows, min_ratio=args.min_ratio, bps_tol=args.bps_tol,
                                       snap_tol=args.snap_tol, bps_path=args.bps_path,
                                       equity_tol=et, price_series=series)
                ff = cumulative_factors(rows, ev)
                dmg = [k for k, v in ff.items() if v != 1.0]
                n2 = sum(1 for v in ff.values() if v >= 2.0)
                print("  equity_tol=%-5s | %4d/%4d/%5d | %5d/%4d | %s"
                      % ("none" if et is None else "%g" % et, st["n_events"],
                         st["n_event_companies"], n2, len(dmg), len({ec for ec, _ in dmg}),
                         st["equity"]["rejected_by_kind"] or "-"))

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
        # **母集団は常に純資産比チェック無しで作る**（#657）。チェックが落とすイベントを
        # 突合に含めないと、チェックの害（本物を落とす）も利益（偽陽性を落とす）も測れない。
        # チェック有りの結果は、突合の後で `equity_gate_crosstab` が許容値ごとに引き直す。
        # 上場廃止をまたぐペアの判定は本番と同じく入れる（#672）。偽陽性率を本番の検出で測るため。
        series = D.load_price_series(db, min_hole_days=LISTING_GAP_MIN_DAYS)
        db.commit()
        detect_kw = dict(min_ratio=args.min_ratio, bps_tol=args.bps_tol,
                         snap_tol=args.snap_tol, bps_path=args.bps_path,
                         price_series=series)
        all_events, _ = detect_events(rows, equity_tol=None, **detect_kw)
        # **抽出より先に契約窓を学習する**。窓の外から引いたサンプルは公式が判定できず、
        # `out_of_coverage` で分母だけが消える（実測 2026-09-12: 30件中 21件が窓外）。
        cover = asyncio.run(learn_coverage())
        print("契約窓: %s 〜 %s（突合の窓=%s）" % (cover[0], cover[1], args.coverage))
        events = [e for e in all_events
                  if in_coverage(e, cover, slack_days=args.window_slack_days,
                                 mode=args.coverage)]
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
        elif args.census:
            # **窓内の全社を陽性にする**（#657）。抽出だと数件しかない偽陽性の候補が入る保証が
            # 無く、#654 の 30 社突合は `no_official_event` 0 件で偽陽性率を測れなかった。
            # 陰性対照は抽出と同じ関数で引く（n=0 なら陽性を選ばず対照だけを返す）。
            pos = sorted({e.edinet_code for e in events})
            _, ctrl = choose_sample(events, flat, n=0, controls=args.controls, seed=args.seed)
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
        pos_set = set(pos)
        for e in events:
            if e.edinet_code not in pos_set:
                continue
            results.append(match_event(e, official.get(e.edinet_code, []),
                                       slack_days=args.window_slack_days,
                                       tol=args.match_tol, coverage=cover,
                                       coverage_mode=args.coverage))
        misses = [ec for ec in ctrl if official.get(ec)]

        tally, denom, rate, rate_raw = tally_rates(results)

        print()
        print("突合結果: " + ", ".join("%s=%d" % kv for kv in sorted(tally.items())))
        print("一致率（スナップ後）= %.3f / （生比も許容）= %.3f / 分母 %d"
              % (rate, rate_raw, denom))
        print("対照群の見逃し（公式にイベントがあった社）= %d 社 %s"
              % (len(misses), misses or ""))
        by_group = tally_by_group(results, events)
        for g in sorted(by_group):
            t = Counter(by_group[g])
            d = sum(t[k] for k in MATCH_DENOMINATOR)
            print("  %-18s 分母 %3d / 偽陽性(no_official_event) %3d / %s"
                  % (g, d, t["no_official_event"], dict(t)))
        ev_of = {(e.edinet_code, e.year): e for e in events}
        for r in results:
            if r.status not in ("agree",):
                e = ev_of.get((r.edinet_code, r.year))
                print("  %-9s year=%s 検出=%s 生比=%s 公式=%s ev=%d %s 純資産=%s"
                      % (r.edinet_code, r.year, _fmt(r.detected), _fmt(r.raw_detected),
                         _fmt(r.official), r.n_official_events, r.status,
                         _fmt(e.equity_ratio if e else None)))

        # 純資産比チェック（#657）が消すイベントを、許容値ごとに突合ステータスで数える。
        # 害=公式と一致する本物を落とした件数 / 利益=公式に無いイベントを落とした件数。
        # 突合の外（契約窓の外など）で消えるものは Yahoo 等で別に確かめる材料として並べる。
        print()
        print("純資産比チェックが消すイベント (チェック無しの母集団との差)")
        ev_all = {(e.edinet_code, e.year): e for e in all_events}
        gate: dict[str, dict] = {}
        for et in EQUITY_TOL_GRID:
            gated, _ = detect_events(rows, equity_tol=et, **detect_kw)
            gate["%g" % et] = equity_gate_crosstab(results, all_events, gated)
        outside = sorted({k[0] for ct in gate.values() for k in ct["not_in_census"]})
        meta_out = load_company_meta(db, outside)
        for key, ct in gate.items():
            print("  equity_tol=%-5s 消えた %3d 件 / 害 %d / 利益 %d / 内訳 %s / "
                  "拾い直し %d / 増えた %d"
                  % (key, len(ct["dropped"]), ct["harm"], ct["benefit"],
                     ct["dropped_status"] or "-", len(ct["readmitted"]), len(ct["added"])))
            for k in ct["not_in_census"]:
                e = ev_all[k]
                print("    突合外: %-9s %-6s %4s %-6s %-10s 倍率 %s 窓 %s"
                      % (k[0], meta_out.get(k[0], ("", ""))[0] or "-", k[1], e.source,
                         e.kind, _fmt(e.canonical),
                         event_window(e, slack_days=args.window_slack_days)))

        if args.json:
            p = Path(args.json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "coverage": list(cover), "coverage_mode": args.coverage,
                "source": args.source, "bps_path": bool(args.bps_path),
                "census": bool(args.census),
                "positives": pos, "controls": ctrl,
                "no_sec_code": no_sec, "tally": dict(tally),
                "tally_by_group": by_group, "equity_gate": gate,
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
    # 第1経路の純資産比チェック（#657）。既定は `DEFAULT_EQUITY_TOL`＝毎晩の係数表と同じ。
    # verify-sample は母集団を常にチェック無しで作り、許容値ごとの差を別に出す。
    common.add_argument("--equity-tol", dest="equity_tol", type=float,
                        default=DEFAULT_EQUITY_TOL,
                        help="株数と同じ向きに純資産総額が 1+tol 倍を超えて動いたら採らない（#657）")
    common.add_argument("--no-equity-check", dest="equity_tol", action="store_const",
                        const=None, help="純資産比チェックを使わない（#659 までの検出と同一）")

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
    # 既定は full＝今日までと同じ意味。partial は倍率が合っているかだけを測る緩め方で、
    # 「公式が判定できる」と「翌年の行が提出済み」が full では排他になる第2経路のために足した（#659）。
    v.add_argument("--coverage", choices=("full", "partial"), default="full",
                   help="突合の窓。full=契約窓へ完全に収まる窓だけ / "
                        "partial=重なりの中で公式イベントが見つかった件だけを分母にする（#659）")
    v.add_argument("--only", default="")
    v.add_argument("--census", action="store_true",
                   help="抽出せず、突合の窓に収まる全イベントの社を陽性にする（#657・--only が優先）")
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
