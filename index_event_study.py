"""指数イベントのイベントスタディの部品 — 2022 前例（段階的ウエイト低減 493 社）を事前登録どおりに測る（#1031・親 #1008）。

事前登録 `docs/prereg/index-event-2026.md`（#1007）が決めた測り方を、データを渡せば答えが返る純関数に
する。CLI は `scripts/measure_index_event.py`。この module が今持つのは **stage A（盲検の下調べ）** の
部品だけ: 暦（§3.1）・対照（§4.2）・マッチング（§4.3）・バランス・first stage（§5.3）・異常リターン（§0）・
Newey-West（§5.2）・σ / SE(δ̂) / MDE（§7）・placebo の誤検出率（§5.2）・R0 の照合（§2）。事象窓の量と
検定（E 族・κ・δ̂・ρ）は stage B（#1032）で足す。

**M 族（§6.2・#1009）** も持つ: 本番の OOF（M-2 は raw・M-6 は size_neutral）で、期ごとの処置群と対照の
placement bias の差 d_t を E_t・P_t へ回帰し、層別に引いて全期で固定した擬似処置群の順列で p を出す。
OOF は `scripts.momentum_gate --size-neutral` が書く CSV から読む。

## 盲検を構造で守る

`stage_a` は入口で終値の表から**公表週（公表日を含む ISO 週）以降の行を落としてから**計算する。
売買代金（first stage）はリターンではないので落とさない。`tests/test_index_event_study.py` が
「公表週以降の終値を書き換えても出力が変わらない」ことを固定する（stage B の関数を足しても残る検査）。

## 事前登録が決めていなかった点（ここで決めた。出力の `definitions` にも残す）

- **マッチ日の値**は、マッチ日以前の最後の週次の足（学習パネルの月末と同じ `month_end_closes` の足）。
  日次株価は 183 日しか残らないので、マッチ日当日の終値は取れない。
- **直前 52 週の log リターン**は、その足の週から 52〜56 週前で最も新しい足との比。無ければ欠落
  （52 週ぶんの履歴が無い社）。**52 週の売買代金合計**は、その足の週を含む直近 52 週の足の和
  （足の無い週は約定なし＝0）で、履歴が 52 週に満たない社は欠落。
- caliper の標準偏差・Mahalanobis の共分散・**SMD の分母**は、処置候補＋対照候補をプールした集団から
  取る（caliper と SMD を同じ物差しで読める・マッチの結果で分母が動かない）。
- **first stage** の基準（直前 26 週の売買代金の中央値）は、足のある週が 13 週以上のときだけ取る。
  対照の平均は値のある対照だけで取る。t が出ない週（2 社未満）は不成立に数える。
- **placebo の Newey-West の Ω** は事前期間の系列から推定する。E1 の Ω は事象窓を含む解析期間から
  取る決まりで、stage A でそれを読むと盲検が破れる。
- Newey-West の自己共分散は、欠けた週を積に入れず、値のある週の数で割る（平均は引かない・§5.2）。

## 祝日

圧力日 P_m（D_m の前営業日）と placebo の週は営業日で決まる。祝日は計算で導かず手で入れる
（ADR-0068 決定 5）。持つのは月末（22 日以降）にかかる東証の休業日だけで、対象は 2019〜2021 年の全月
（placebo の月末の週・#1033）と 2022〜2028 年の 1・4・7・10 月（指数修正日）。それ以外の月日・年を聞かれたら
`CalendarError`（黙って誤った日を返さない）。

純関数だけで、database・scripts を import しない（テストで固定）。
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta
from statistics import NormalDist
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence

import numpy as np
import pandas as pd

# ── 事前登録の値（本文 §3〜§7。変えるなら本文と凍結値を同じ PR で変える）────────────────

REVIEW_2022 = "topix-2022-10"            # 台帳（index_events.REVIEWS）の 2022 前例のキー
MATCH_DATES_2022 = {"main": date(2022, 3, 31), "robust": date(2022, 9, 30)}   # §3.3
CONTROL_SETS = ("C1", "C2")              # §4.2（主は C1）
MAIN_SPEC = ("C1", "main")
K_NEIGHBORS = 5                          # §4.3 Mahalanobis の k 最近傍（復元あり）
CALIPER_SD = 0.25                        # §4.3 変数ごとにプールした標準偏差の倍数
SMD_MAX = 0.1                            # §4.3 主の仕様で全変数 |SMD| < 0.1
MATCH_VARS = ("logcap", "log_turnover_ratio", "log_pbr", "ret_52w")   # §4.3
FIRST_STAGE_BASE_WEEKS = 26              # §5.3 直前 26 週の売買代金の中央値
FIRST_STAGE_T_MIN = 3.0                  # §5.3 全 m で t ≥ 3
NW_LAG = 4                               # §5.2 Bartlett 核・lag 4
# §7 MDE の Ω を推定する事前期間（#1033 で改訂）。始まりは週次株価の最古の週、終わりは主のマッチ日の
# 変数の窓（52 週リターンの基準の足は最大 56 週前）より前——窓の中では処置と対照のリターンをそろえて
# いるので、AR̄ のばらつきが作りとして小さくなり MDE を楽観側へ誤る。2020 年の新型コロナ期は残す。
PRE_PERIOD = (date(2019, 7, 29), date(2021, 2, 21))
PLACEBO_FIRST = (2019, 8)                # §5.2 擬似の圧力週の範囲（年, 月）。事前期間の各月の月末の週
PLACEBO_LAST = (2021, 1)
PLACEBO_MONTHS = tuple(range(1, 13))     # 月末の週（四半期末だけでは 6 個で下限 15 個に届かない・#1033）
PLACEBO_PICK = 10                        # 10 個を非復元で引く
PLACEBO_DRAWS = 10_000
PLACEBO_MIN_WEEKS = 15                   # 未満なら誤検出率も経験 p も出さない（切り替えも起きない）
# 名目 5% ± 5pt。外れたら E1 の p を経験 p へ切り替え、E4 は確証から外す（擬似の 52 週窓がそろわない）
PLACEBO_FPR_BAND = (0.0, 0.10)
ALPHA = 0.05                             # E1 と同じ片側 5%
POWER = 0.80                             # §7 MDE の検出力
SEED = 0
REVIEW_MONTHS = (1, 4, 7, 10)            # 定期の係数変更の月（指数修正日）

# ── ここで決めた値（docstring の「事前登録が決めていなかった点」）──────────────────────

RET_BASE_SLACK_WEEKS = 4                 # 52 週前の足が無ければ 56 週前まで遡る
FIRST_STAGE_MIN_BASE = 13                # 基準の中央値に要る足の数（26 週の半分）

# data_j の市場・商品区分のうち TOPIX の母集団（内国株式）。C1 はこの中から取る。
DOMESTIC_SEGMENTS = ("プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）")
NON_TOPIX_SIZE_CLASS = "-"

Z_ALPHA = NormalDist().inv_cdf(1 - ALPHA)
Z_POWER = NormalDist().inv_cdf(POWER)
WEEK = timedelta(days=7)

DEFINITIONS = {
    "match_value": "マッチ日以前の最後の週次の足（month_end_closes）。株価は終値 x F（提出当時の基準）",
    "ret_52w": f"その足の週から 52〜{52 + RET_BASE_SLACK_WEEKS} 週前で最も新しい足との log 比",
    "turnover_52w": "その足の週を含む直近 52 週の turnover_sum の和（足の無い週は 0）÷ 時価総額",
    "caliper_sd_smd_denominator": "処置候補＋対照候補をプールした標準偏差（ddof=1）",
    "first_stage_base": f"直前 {FIRST_STAGE_BASE_WEEKS} 週のうち足のある週が {FIRST_STAGE_MIN_BASE} 週以上"
                        "のときの中央値。t が出ない週は不成立",
    "placebo_omega": "事前期間の AR̄ から推定（解析期間の Ω は事象窓を含むので stage A では読まない）",
    "newey_west": f"Bartlett・lag {NW_LAG}・平均を引かない。欠けた週は積に入れず、値のある週の数で割る",
}

# ── 33 業種 → 17 業種（data_j 2026-10-20 版の「33業種区分」「17業種区分」で 33 → 17 が一対一）────

SECTOR17 = {
    "水産・農林業": "食品", "食料品": "食品",
    "鉱業": "エネルギー資源", "石油・石炭製品": "エネルギー資源",
    "建設業": "建設・資材", "ガラス・土石製品": "建設・資材", "金属製品": "建設・資材",
    "繊維製品": "素材・化学", "パルプ・紙": "素材・化学", "化学": "素材・化学",
    "医薬品": "医薬品",
    "ゴム製品": "自動車・輸送機", "輸送用機器": "自動車・輸送機",
    "鉄鋼": "鉄鋼・非鉄", "非鉄金属": "鉄鋼・非鉄",
    "機械": "機械",
    "電気機器": "電機・精密", "精密機器": "電機・精密",
    "その他製品": "情報通信・サービスその他", "情報・通信業": "情報通信・サービスその他",
    "サービス業": "情報通信・サービスその他",
    "電気・ガス業": "電力・ガス",
    "陸運業": "運輸・物流", "海運業": "運輸・物流", "空運業": "運輸・物流", "倉庫・運輸関連業": "運輸・物流",
    "卸売業": "商社・卸売",
    "小売業": "小売",
    "銀行業": "銀行",
    "証券、商品先物取引業": "金融（除く銀行）", "保険業": "金融（除く銀行）", "その他金融業": "金融（除く銀行）",
    "不動産業": "不動産",
}

# ── 暦（§3.1・§5.2）───────────────────────────────────────────────────────────────

# 表が覆う月: 2019〜2021 年は全月（placebo の月末の週・#1033）、2022〜2028 年は 1・4・7・10 月（指数修正日）。
CALENDAR_MONTHS = {**{y: tuple(range(1, 13)) for y in range(2019, 2022)},
                   **{y: REVIEW_MONTHS for y in range(2022, 2029)}}
CALENDAR_YEARS = frozenset(CALENDAR_MONTHS)
CALENDAR_FROM_DAY = 22
# 上の月の 22 日以降にかかる国民の祝日・振替休日・国民の休日（内閣府「国民の祝日について」
# https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html）と東証の年末の休業日（12-31）。2019〜2021 年は
# 即位礼正殿の儀（2019-10-22）と東京大会の祝日の移動（2020・2021 年の 7 月）を含む。2022〜2028 年の
# 1・4・7・10 月の 22 日以降は昭和の日（4/29）だけ（成人の日・海の日・スポーツの日は 21 日以前）。
CLOSED_DAYS = frozenset({
    date(2019, 4, 29), date(2019, 4, 30), date(2019, 9, 23), date(2019, 10, 22), date(2019, 11, 23),
    date(2019, 12, 31),
    date(2020, 2, 23), date(2020, 2, 24), date(2020, 4, 29), date(2020, 7, 23), date(2020, 7, 24),
    date(2020, 9, 22), date(2020, 11, 23), date(2020, 12, 31),
    date(2021, 2, 23), date(2021, 4, 29), date(2021, 7, 22), date(2021, 7, 23), date(2021, 9, 23),
    date(2021, 11, 23), date(2021, 12, 31),
    date(2022, 4, 29), date(2023, 4, 29), date(2024, 4, 29), date(2025, 4, 29),
    date(2026, 4, 29), date(2027, 4, 29), date(2028, 4, 29),
})


class CalendarError(ValueError):
    """手で入れた休業日の表が覆わない日付を聞かれた。"""


class PressureWeek(NamedTuple):
    m: int
    effective_on: date      # 指数修正日 D_m
    pressure_day: date      # 圧力日 P_m（D_m の前営業日）
    week: date              # 圧力週 w_m（P_m を含む ISO 週の月曜）
    date_basis: str         # "primary" / "rule"（台帳 `index_events.Step` から）


def iso_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def is_business_day(d: date) -> bool:
    if d.month not in CALENDAR_MONTHS.get(d.year, ()) or d.day < CALENDAR_FROM_DAY:
        raise CalendarError(
            f"{d} は休業日の表の範囲外（2019〜2021 年の全月・2022〜2028 年の {list(REVIEW_MONTHS)} 月の"
            f" {CALENDAR_FROM_DAY} 日以降だけ）")
    return d.weekday() < 5 and d not in CLOSED_DAYS


def previous_business_day(d: date) -> date:
    p = d - timedelta(days=1)
    while not is_business_day(p):
        p -= timedelta(days=1)
    return p


def last_business_day(year: int, month: int) -> date:
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def pressure_calendar(steps: Iterable) -> tuple:
    """台帳の `Step`（m・指数修正日・根拠）→ `PressureWeek` の並び（§3.1・§3.2 の表）。"""
    out = []
    for s in steps:
        p = previous_business_day(s.effective_on)
        out.append(PressureWeek(s.m, s.effective_on, p, iso_monday(p), s.date_basis))
    return tuple(out)


def placebo_pressure_weeks(first: tuple = PLACEBO_FIRST, last: tuple = PLACEBO_LAST,
                           months: tuple = PLACEBO_MONTHS) -> list:
    """各月（`months`）の最終営業日の前営業日を含む週（§5.2 の擬似の圧力週）。"""
    out = []
    y, m = first
    while (y, m) <= tuple(last):
        if m in months:
            out.append(iso_monday(previous_business_day(last_business_day(y, m))))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def blind_from(announced: date) -> date:
    """盲検で落とす最初の週＝公表日を含む ISO 週の月曜（引け後の公表でもその週から落とす＝安全側）。"""
    return iso_monday(announced)


# ── 週次の表 ─────────────────────────────────────────────────────────────────────

def weekly_frame(values_by_co: Mapping[str, Mapping[date, float]]) -> pd.DataFrame:
    """{社: {週の月曜: 値}} → 週 × 社の表。行は欠けの無い月曜の連続（差分・ラグを位置で取れる）。"""
    cols = sorted(values_by_co)
    weeks = sorted({w for v in values_by_co.values() for w in v})
    if not weeks:
        return pd.DataFrame(columns=cols, dtype=float)
    n = (weeks[-1] - weeks[0]).days // 7 + 1
    index = pd.Index([weeks[0] + WEEK * i for i in range(n)], dtype=object)
    data = {ec: pd.Series(values_by_co[ec], dtype=float) for ec in cols}
    return pd.DataFrame(data, columns=cols).reindex(index)


def rows_between(frame, lo: date, hi: date):
    """週の月曜が [lo, hi] に入る行（index は date の object 型なので、スライスでなく比較で選ぶ）。"""
    return frame[[lo <= w <= hi for w in frame.index]]


def blind(closes: pd.DataFrame, cutoff: date) -> pd.DataFrame:
    """`cutoff` の週以降の終値を落とす（stage A の入口）。"""
    return closes[[w < cutoff for w in closes.index]]


def weekly_log_returns(closes: pd.DataFrame) -> pd.DataFrame:
    """r_{i,w} = log(close_w / close_{w−1})（隣り合う週だけ。欠けた週をまたがない）。"""
    lc = np.log(closes.where(closes > 0))
    return lc - lc.shift(1)


# ── マッチ変数（§4.3）────────────────────────────────────────────────────────────

class Firm(NamedTuple):
    """マッチ日に見えていた 1 社の値（CLI が `sector_gap_asof.asof_records` から作る）。"""
    bar: date                    # 足の日付（trade_date）
    price: float                 # 終値 × F（提出当時の 1 株の基準）
    shares: Optional[float]      # 発行済株式数（提出当時の基準）
    bps: Optional[float]
    industry33: Optional[str]


def _base_week(closes: pd.DataFrame, ec: str, w0: date) -> Optional[date]:
    for k in range(52, 52 + RET_BASE_SLACK_WEEKS + 1):
        w = w0 - WEEK * k
        if w in closes.index and pd.notna(closes.at[w, ec]) and closes.at[w, ec] > 0:
            return w
    return None


def match_variables(firms: Mapping[str, Firm], closes: pd.DataFrame,
                    turnover: pd.DataFrame) -> pd.DataFrame:
    """社 × (4 変数 + 17 業種) の表。計算できない変数は NaN、業種が対応表に無ければ None。"""
    rows = {}
    for ec in sorted(firms):
        f = firms[ec]
        row: dict = dict.fromkeys(MATCH_VARS, math.nan)
        row["sector17"] = SECTOR17.get(f.industry33 or "")
        mcap = f.price * f.shares if (f.shares and f.shares > 0 and f.price and f.price > 0) else None
        if mcap:
            row["logcap"] = math.log(mcap)
        if f.bps and f.bps > 0 and f.price and f.price > 0:
            row["log_pbr"] = math.log(f.price / f.bps)
        w0 = iso_monday(f.bar)
        if ec in closes.columns and w0 in closes.index and pd.notna(closes.at[w0, ec]):
            base = _base_week(closes, ec, w0)
            if base is not None and closes.at[w0, ec] > 0:
                row["ret_52w"] = math.log(closes.at[w0, ec] / closes.at[base, ec])
                if mcap and ec in turnover.columns:
                    total = turnover[ec].reindex([w0 - WEEK * k for k in range(52)]).sum()
                    if total > 0:
                        row["log_turnover_ratio"] = math.log(total / mcap)
        rows[ec] = row
    return pd.DataFrame.from_dict(rows, orient="index", columns=[*MATCH_VARS, "sector17"])


# ── 対照（§4.2）────────────────────────────────────────────────────────────────────

def c1_controls(snapshot_rows: Iterable, ledger_codes: Iterable[str]) -> set:
    """C1: data_j の規模区分が '-'・内国株式・台帳のどの一覧にも載っていない社。"""
    excluded = set(ledger_codes)
    return {r.edinet_code for r in snapshot_rows
            if r.edinet_code and r.size_class == NON_TOPIX_SIZE_CLASS
            and r.market_segment in DOMESTIC_SEGMENTS and r.edinet_code not in excluded}


def c2_controls(continuing: Iterable[str], excluded: Iterable[str]) -> set:
    """C2: TOPIX 継続と推定される社＝2026-10 の継続銘柄から 2022 前例の群を除いたもの。

    2022 時点の構成銘柄の全一覧は無い（ADR-0068 決定 4）ので、2022 以降に TOPIX へ入った社が混ざる。
    """
    return set(continuing) - set(excluded)


# ── マッチング（§4.3）──────────────────────────────────────────────────────────────

class Matching(NamedTuple):
    pairs: dict              # {処置社: (対照, ...)}（距離の近い順）
    unmatched: dict          # {処置社: 理由}
    sd: dict                 # {変数: プールした標準偏差}
    n_pool_treated: int      # 4 変数と業種がそろった処置社
    n_pool_controls: int     # 4 変数と業種がそろった対照候補


UNMATCHED_REASONS = ("no_data", "missing_variables", "unknown_sector", "no_control_in_caliper")


def _complete(variables: pd.DataFrame) -> pd.DataFrame:
    ok = variables[list(MATCH_VARS)].notna().all(axis=1)
    ok &= variables["sector17"].map(lambda s: isinstance(s, str))
    return variables[ok]


def match_controls(variables: pd.DataFrame, treated: Iterable[str], controls: Iterable[str], *,
                   k: int = K_NEIGHBORS, caliper: float = CALIPER_SD) -> Matching:
    """17 業種の完全一致 → caliper（全変数）→ Mahalanobis 距離の近い順に最大 k 社（復元あり）。

    距離が同じときはコード順。標準偏差と共分散は、処置候補＋対照候補をプールした集団から取る。
    """
    treated = sorted(set(treated))
    tset = set(treated)
    complete = _complete(variables)
    pool_t = [t for t in treated if t in complete.index]
    pool_c = sorted(c for c in set(controls) - tset if c in complete.index)
    pool = complete.loc[pool_t + pool_c, list(MATCH_VARS)].to_numpy(float)
    if len(pool) < len(MATCH_VARS) + 1:
        sd = np.full(len(MATCH_VARS), math.nan)
        inv = None
    else:
        sd = pool.std(axis=0, ddof=1)
        inv = np.linalg.inv(np.cov(pool, rowvar=False))
    by_sector: dict = {}
    for c in pool_c:
        by_sector.setdefault(complete.at[c, "sector17"], []).append(c)
    pairs, unmatched = {}, {}
    for t in treated:
        if t not in variables.index:
            unmatched[t] = "no_data"
            continue
        if variables.loc[t, list(MATCH_VARS)].isna().any():
            unmatched[t] = "missing_variables"
            continue
        sector = variables.at[t, "sector17"]
        if not isinstance(sector, str):
            unmatched[t] = "unknown_sector"
            continue
        codes = by_sector.get(sector, [])
        if inv is None or not codes:
            unmatched[t] = "no_control_in_caliper"
            continue
        xt = complete.loc[t, list(MATCH_VARS)].to_numpy(float)
        d = complete.loc[codes, list(MATCH_VARS)].to_numpy(float) - xt
        inside = (np.abs(d) <= caliper * sd).all(axis=1)
        if not inside.any():
            unmatched[t] = "no_control_in_caliper"
            continue
        di = d[inside]
        dist = np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", di, inv, di), 0.0))
        ranked = sorted(zip(dist.tolist(), [c for c, ok in zip(codes, inside) if ok]))
        pairs[t] = tuple(c for _, c in ranked[:k])
    return Matching(pairs, unmatched, dict(zip(MATCH_VARS, sd.tolist())), len(pool_t), len(pool_c))


def balance(variables: pd.DataFrame, matching: Matching) -> dict:
    """{変数: SMD}。処置群の平均 − 対照の平均（処置社ごとに対照を平均してから処置社で平均）を、
    プールした標準偏差で割る。マッチした処置社が無ければ None。"""
    treated = sorted(matching.pairs)
    if not treated:
        return dict.fromkeys(MATCH_VARS)
    cols = list(MATCH_VARS)
    xt = variables.loc[treated, cols].to_numpy(float)
    xc = np.array([variables.loc[list(matching.pairs[t]), cols].to_numpy(float).mean(axis=0)
                   for t in treated])
    diff = xt.mean(axis=0) - xc.mean(axis=0)
    return {v: (float(diff[i] / matching.sd[v]) if matching.sd[v] and matching.sd[v] > 0 else None)
            for i, v in enumerate(cols)}


def balance_ok(smd: Mapping[str, Optional[float]]) -> Optional[bool]:
    if any(v is None for v in smd.values()):
        return None
    return all(abs(v) < SMD_MAX for v in smd.values())


# ── first stage（§5.3）────────────────────────────────────────────────────────────

def abnormal_turnover(turnover: pd.DataFrame, week: date, *,
                      base_weeks: int = FIRST_STAGE_BASE_WEEKS,
                      min_base: int = FIRST_STAGE_MIN_BASE) -> pd.Series:
    """社ごとの log(週 `week` の売買代金 ÷ 直前 `base_weeks` 週の売買代金の中央値)。"""
    if week not in turnover.index:
        return pd.Series(dtype=float)
    cur = turnover.loc[week]
    base = turnover.reindex([week - WEEK * k for k in range(1, base_weeks + 1)])
    med = base.median(skipna=True)
    ok = (base.notna().sum() >= min_base) & (med > 0) & (cur > 0)
    return np.log(cur[ok] / med[ok])


def first_stage(turnover: pd.DataFrame, pairs: Mapping[str, Sequence[str]],
                calendar: Sequence[PressureWeek]) -> list:
    """各圧力週で、処置社の異常売買代金 − 対照の平均 の処置社間の t。"""
    out = []
    for pw in calendar:
        q = abnormal_turnover(turnover, pw.week)
        x = []
        for t in sorted(pairs):
            if t not in q.index:
                continue
            qc = [q[c] for c in pairs[t] if c in q.index]
            if qc:
                x.append(float(q[t]) - sum(qc) / len(qc))
        n = len(x)
        mean = sum(x) / n if n else None
        sd = float(np.std(x, ddof=1)) if n >= 2 else None
        t_val = mean / (sd / math.sqrt(n)) if sd else None
        out.append({"m": pw.m, "week": pw.week.isoformat(), "n": n, "mean": mean, "t": t_val})
    return out


def first_stage_ok(rows: Sequence[dict]) -> Optional[bool]:
    if not rows:
        return None
    return all(r["t"] is not None and r["t"] >= FIRST_STAGE_T_MIN for r in rows)


# ── 異常リターン（§0）と Newey-West（§5.2）──────────────────────────────────────────

def abnormal_return_series(returns: pd.DataFrame, pairs: Mapping[str, Sequence[str]]) -> pd.Series:
    """AR̄_w = 週 w に処置社と対照（1 社以上）の両方の値がそろう処置社で AR_{i,w} を等加重平均した値。"""
    cols = {}
    for t in sorted(pairs):
        cs = [c for c in pairs[t] if c in returns.columns]
        if t in returns.columns and cs:
            cols[t] = returns[t] - returns[cs].mean(axis=1, skipna=True)
    if not cols:
        return pd.Series(np.nan, index=returns.index, dtype=float)
    return pd.DataFrame(cols, index=returns.index).mean(axis=1, skipna=True)


def nw_autocovariances(series: pd.Series, lag: int = NW_LAG) -> Optional[list]:
    """平均を引かない自己共分散 γ_0..γ_lag（行は欠けの無い週の連続）。値のある週が無ければ None。"""
    x = series.to_numpy(float)
    finite = np.isfinite(x)
    n = int(finite.sum())
    if n == 0:
        return None
    z = np.where(finite, x, 0.0)
    return [float(np.dot(z[k:], z[:len(z) - k]) / n) for k in range(lag + 1)]


def nw_variance(weights: Mapping[date, float], gammas: Sequence[float], lag: int = NW_LAG) -> float:
    """Σ_w c_w AR̄_w の分散 c'Ωc（Ω_{ij} = Bartlett(|i−j|) × γ_{|i−j|}、lag を超える組は 0）。"""
    if not weights:
        return 0.0
    weeks = sorted(weights)
    pos = np.array([(w - weeks[0]).days // 7 for w in weeks])
    c = np.array([weights[w] for w in weeks], dtype=float)
    k = np.abs(pos[:, None] - pos[None, :])
    near = k <= lag
    g = np.zeros_like(k, dtype=float)
    g[near] = np.asarray(gammas, dtype=float)[k[near]] * (1.0 - k[near] / (lag + 1))
    return float(c @ g @ c)


def window_weights(weeks: Sequence[date], lo: int, hi: int, scale: float) -> dict:
    """各週 w の [w + lo, w + hi] に重み `scale` を置く（重なれば足す）。"""
    out: dict = {}
    for w in weeks:
        for j in range(lo, hi + 1):
            d = w + WEEK * j
            out[d] = out.get(d, 0.0) + scale
    return out


def impact_weights(weeks: Sequence[date]) -> dict:
    """I = (1/n) Σ_m CAR[w_m − 1, w_m] の重み（§6.1）。"""
    return window_weights(weeks, -1, 0, 1.0 / len(weeks))


def delta_hat_weights(weeks: Sequence[date]) -> dict:
    """δ̂ = 4 × (I + R) = (4/n) Σ_m CAR[w_m − 1, w_m + 4] の重み（§6.1・§7）。"""
    return window_weights(weeks, -1, 4, 4.0 / len(weeks))


def power_summary(ar_pre: pd.Series, weeks: Sequence[date], lag: int = NW_LAG) -> dict:
    """σ（週次）・SE(δ̂) = √(c'Ωc)・MDE = (z_0.95 + z_0.80) × SE（§7）。Ω は事前期間の AR̄ から。"""
    n = int(np.isfinite(ar_pre.to_numpy(float)).sum())
    out = {"period": [PRE_PERIOD[0].isoformat(), PRE_PERIOD[1].isoformat()], "n_weeks": n,
           "sigma_weekly": None, "gammas": None, "se_delta_hat": None, "mde": None,
           "z": {"alpha": Z_ALPHA, "power": Z_POWER}}
    if n <= lag:
        out["reason"] = f"事前期間の AR̄ が {n} 週（lag {lag} より多く要る）。週次株価が期間を覆っていない"
        return out
    g = nw_autocovariances(ar_pre, lag)
    se = math.sqrt(max(nw_variance(delta_hat_weights(weeks), g, lag), 0.0))
    out.update(sigma_weekly=math.sqrt(g[0]), gammas=g, se_delta_hat=se, mde=(Z_ALPHA + Z_POWER) * se)
    return out


# ── placebo（§5.2）──────────────────────────────────────────────────────────────

def placebo_draws(ar: pd.Series, weeks: Sequence[date], gammas: Sequence[float], *,
                  pick: int = PLACEBO_PICK, draws: int = PLACEBO_DRAWS, seed: int = SEED,
                  lag: int = NW_LAG) -> tuple:
    """擬似の I*（`pick` 個の擬似圧力窓 CAR の平均）と、その Newey-West の SE を `draws` 回ぶん。"""
    car = np.array([ar[w - WEEK] + ar[w] for w in weeks], dtype=float)
    rng = np.random.default_rng(seed)
    impacts = np.empty(draws)
    ses = np.empty(draws)
    for i in range(draws):
        idx = np.sort(rng.choice(len(weeks), size=pick, replace=False))
        impacts[i] = car[idx].mean()
        ses[i] = math.sqrt(max(nw_variance(impact_weights([weeks[j] for j in idx]), gammas, lag), 0.0))
    return impacts, ses


def placebo_test(ar: pd.Series, weeks: Sequence[date], gammas: Optional[Sequence[float]], *,
                 pick: int = PLACEBO_PICK, draws: int = PLACEBO_DRAWS, seed: int = SEED,
                 min_weeks: int = PLACEBO_MIN_WEEKS, lag: int = NW_LAG) -> dict:
    """E1 と同じ片側 5% の Newey-West 検定を擬似の I* に当てて棄却した割合（誤検出率）と、
    経験 p へ切り替えるか。使える週（w − 1 と w の AR̄ がそろう）が `min_weeks` 未満なら出さない。"""
    usable = [w for w in weeks
              if w in ar.index and (w - WEEK) in ar.index
              and np.isfinite(ar[w]) and np.isfinite(ar[w - WEEK])]
    out = {"n_candidates": len(weeks), "n_usable": len(usable), "fpr": None,
           "switch_to_empirical_p": False, "pick": pick, "draws": draws, "seed": seed}
    if len(usable) < min_weeks or gammas is None:
        out["reason"] = (f"使える擬似の圧力週が {len(usable)}/{len(weeks)} 個（{min_weeks} 個未満なら"
                         "誤検出率も経験 p も出さず、切り替えも起きない）")
        return out
    impacts, ses = placebo_draws(ar, usable, gammas, pick=pick, draws=draws, seed=seed, lag=lag)
    with np.errstate(divide="ignore", invalid="ignore"):
        rejected = int(np.sum((ses > 0) & (impacts / ses < -Z_ALPHA)))
    fpr = rejected / draws
    lo, hi = PLACEBO_FPR_BAND
    out.update(fpr=fpr, switch_to_empirical_p=not (lo <= fpr <= hi))
    return out


# ── R0（§2）───────────────────────────────────────────────────────────────────────

def r0_summary(*, counts: Mapping[str, int], d1: int, d2: Optional[int],
               resolution: Mapping[str, float], min_resolution: float,
               first_stage: Optional[bool], balance: Optional[bool]) -> dict:
    """R0 の照合値。`status` は fail（どれかが不成立）/ pending（未登録・計算不能が残る）/ pass。"""
    closes = counts["transition"] == counts["released"] + counts["continuing"] + d1
    items = {
        "counts_2022": {**counts, "d1": d1, "pass": closes},
        "excluded_2025_01": {
            "value": None if d2 is None else counts["continuing"] - d2,
            "pass": None if d2 is None else True,
            "note": "439 − d2。d2（2023-10〜2025-01 の上場廃止等）が未登録（#1022）" if d2 is None else ""},
        "resolution": {"rates": dict(resolution), "min": min_resolution,
                       "pass": bool(resolution) and all(r >= min_resolution for r in resolution.values())},
        "first_stage": {"pass": first_stage},
        "balance": {"pass": balance},
    }
    verdicts = [v["pass"] for v in items.values()]
    status = ("fail" if any(p is False for p in verdicts)
              else "pending" if any(p is None for p in verdicts) else "pass")
    return {"items": items, "status": status}


# ── stage A ────────────────────────────────────────────────────────────────────

def spec_key(controls: str, role: str) -> str:
    return f"{controls}@{MATCH_DATES_2022[role].isoformat()}"


def _unmatched_counts(matching: Matching, subgroups: Mapping[str, set]) -> dict:
    def group_of(ec: str) -> str:
        return next((name for name, members in subgroups.items() if ec in members), "other")
    return {
        "total": len(matching.unmatched),
        "by_reason": {r: n for r, n in Counter(matching.unmatched.values()).items()},
        "by_subgroup": dict(Counter(group_of(ec) for ec in matching.unmatched)),
    }


def stage_a(*, closes: pd.DataFrame, turnover: pd.DataFrame,
            firms_by_role: Mapping[str, Mapping[str, Firm]], treated: Iterable[str],
            subgroups: Mapping[str, set], controls: Mapping[str, set],
            calendar: Sequence[PressureWeek], cutoff: date, r0_inputs: Mapping) -> tuple:
    """stage A（盲検）。戻り値は (要約, マッチ対)。要約は件数と統計だけで銘柄コードを含めない。

    `closes` は `cutoff`（公表週）以降を落としてから使う。`turnover` は first stage が事象週を読む。
    """
    treated = set(treated)
    closes = blind(closes, cutoff)
    variables = {role: match_variables(firms, closes, turnover) for role, firms in firms_by_role.items()}
    specs, pairs = {}, {}
    matchings = {}
    for c_name in CONTROL_SETS:
        for role in MATCH_DATES_2022:
            m = match_controls(variables[role], treated, controls[c_name])
            smd = balance(variables[role], m)
            key = spec_key(c_name, role)
            matchings[(c_name, role)] = m
            pairs[key] = {t: list(cs) for t, cs in sorted(m.pairs.items())}
            specs[key] = {
                "controls": c_name, "match_date": MATCH_DATES_2022[role].isoformat(),
                "main": (c_name, role) == MAIN_SPEC,
                "n_treated": len(treated), "n_control_candidates": len(controls[c_name] - treated),
                "n_pool_treated": m.n_pool_treated, "n_pool_controls": m.n_pool_controls,
                "n_matched": len(m.pairs),
                "mean_k": (sum(len(v) for v in m.pairs.values()) / len(m.pairs)) if m.pairs else None,
                "n_controls_used": len({c for v in m.pairs.values() for c in v}),
                "unmatched": _unmatched_counts(m, subgroups),
                "sd": m.sd, "smd": smd, "balance_ok": balance_ok(smd),
            }
    main = matchings[MAIN_SPEC]
    fs_rows = first_stage(turnover, main.pairs, calendar)
    fs_ok = first_stage_ok(fs_rows)
    returns = weekly_log_returns(closes)
    ar_pre = rows_between(abnormal_return_series(returns, main.pairs), *PRE_PERIOD)
    power = power_summary(ar_pre, [pw.week for pw in calendar])
    placebo = placebo_test(ar_pre, placebo_pressure_weeks(), power["gammas"])
    main_balance = specs[spec_key(*MAIN_SPEC)]["balance_ok"]
    summary = {
        "calendar": [{"m": pw.m, "effective_on": pw.effective_on.isoformat(),
                      "pressure_day": pw.pressure_day.isoformat(), "week": pw.week.isoformat(),
                      "date_basis": pw.date_basis} for pw in calendar],
        "blind_from": cutoff.isoformat(),
        "groups": {"treated": len(treated), **{k: len(v) for k, v in subgroups.items()}},
        "matching": specs,
        "first_stage": {"rows": fs_rows, "t_min": FIRST_STAGE_T_MIN, "pass": fs_ok},
        "power": power,
        "placebo": placebo,
        "r0": r0_summary(first_stage=fs_ok, balance=main_balance, **r0_inputs),
        "definitions": DEFINITIONS,
    }
    return summary, pairs


# ── M 族（§6.2）: 本番の OOF が処置群を対照より上や下へ置いていたか ─────────────────────

# 本番の構成でそろえる（ADR-0041 / ADR-0047）: M-2 は raw、M-6 は size_neutral の OOF。
M_MODELS = {"xgb_m2": "raw", "elasticnet": "size_neutral"}
M_COEFS = ("b", "c")                     # d_t = a + b·E_t + c·P_t の検定する係数
M_ALPHA = 0.05 / (len(M_MODELS) * len(M_COEFS))   # Bonferroni（各 0.0125）
M_PERMUTATIONS = 1000
M_NW_LAG = 3                             # 併記するだけ（判定は順列の p）
M_LEAD_YEAR = "2021"                     # 先回りが混じる期（回帰に入れず記述する）
M_LABEL_DAYS = 364                       # ラベル窓（月末の足から 52 週）
SIZE_BINS = 10                           # 擬似処置群の層: 規模十分位 × 流動性三分位 × 17 業種
LIQUIDITY_BINS = 3
M_DEFINITIONS = {
    "E_t": f"月末から {M_LABEL_DAYS} 日の窓に圧力日 P_m が入る回数 ÷ 4",
    "P_t": "同じ窓のうち除外（最後の指数修正日）より後の日数の割合",
    "paired_domain": "期ごとに、処置社とその対照が 1 社以上そろう処置社を T、そのそろった対照を C（重複は 1 社）",
    "bias": "期ごとの、全体の中での予測の順位分位 − 実現の順位分位の群平均（blank_group_breakdown と同じ）",
    "strata": f"マッチ日（主）の logcap の {SIZE_BINS} 分位 × log(52 週の売買代金 ÷ 時価総額) の "
              f"{LIQUIDITY_BINS} 分位 × 17 業種。分位は値の順・同じ値はコード順",
    "pseudo": "層ごとに、マッチした処置社と同じ社数を処置群（ITT）・使った対照以外から非復元で引く。"
              "足りない層はその層の全社を使い、不足数を出す。全期で固定",
    "p_permutation": f"(#{{|b*| >= |b|}} + 1) / (有効な回数 + 1)。c も同じ。既定 {M_PERMUTATIONS} 回",
}


def month_end(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[5:7])
    return date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def label_window_exposure(ym: str, calendar: Sequence[PressureWeek], *,
                          days: int = M_LABEL_DAYS) -> tuple:
    """(E_t, P_t)。窓は月末 (ym) の翌日から `days` 日。除外日は暦の最後の指数修正日。"""
    start = month_end(ym)
    end = start + timedelta(days=days)
    e = sum(1 for pw in calendar if start < pw.pressure_day <= end) / 4
    p = min(max((end - calendar[-1].effective_on).days, 0), days) / days
    return e, p


def placements(aligned: Mapping[tuple, tuple]) -> pd.DataFrame:
    """{(ym, 社): (予測, 実現)} → 行ごとの placement（予測の順位分位 − 実現の順位分位）の表。

    順位は期の断面全体での平均順位（1 始まり）を社数で割ったもの。全体が 3 社未満の期は外す
    （`scripts.momentum_gate.blank_group_breakdown` と同じ）。
    """
    if not aligned:
        return pd.DataFrame(columns=["ym", "ec", "yhat", "y", "p"])
    keys = sorted(aligned)
    df = pd.DataFrame({"ym": [k[0] for k in keys], "ec": [k[1] for k in keys],
                       "yhat": [float(aligned[k][0]) for k in keys],
                       "y": [float(aligned[k][1]) for k in keys]})
    g = df.groupby("ym")
    n = g["ec"].transform("size")
    df["p"] = (g["yhat"].rank(method="average") - g["y"].rank(method="average")) / n
    return df[n >= 3].reset_index(drop=True)


def paired_domain(present_by_ym: Mapping[str, set], pairs: Mapping[str, Sequence[str]]) -> dict:
    """{期: (T, C)}。T は対照が 1 社以上そろう処置社、C はそのそろった対照（ADR-0045）。"""
    out = {}
    for ym, present in present_by_ym.items():
        t = {i for i, cs in pairs.items() if i in present and any(c in present for c in cs)}
        c = {j for i in t for j in pairs[i] if j in present}
        out[ym] = (t, c)
    return out


def _member_rows(df: pd.DataFrame, members_by_ym: Mapping[str, set]) -> pd.DataFrame:
    return df[[ec in members_by_ym.get(ym, ()) for ym, ec in zip(df["ym"], df["ec"])]]


def group_bias(df: pd.DataFrame, members_by_ym: Mapping[str, set]) -> dict:
    """{期: 群の placement の平均}（群の社が 1 社以上いる期だけ）。"""
    return {ym: float(v) for ym, v in _member_rows(df, members_by_ym).groupby("ym")["p"].mean().items()}


def _group_rank_ic(df: pd.DataFrame, members_by_ym: Mapping[str, set]) -> dict:
    out = {}
    for ym, sub in _member_rows(df, members_by_ym).groupby("ym"):
        if len(sub) >= 3 and sub["yhat"].nunique() > 1 and sub["y"].nunique() > 1:
            out[ym] = float(sub["yhat"].rank().corr(sub["y"].rank()))
    return out


def _regression_periods(d: Mapping[str, Optional[float]]) -> list:
    return sorted(ym for ym, v in d.items()
                  if not ym.startswith(M_LEAD_YEAR) and v is not None and math.isfinite(v))


def fit_m_regression(d: Mapping[str, float], exposure: Mapping[str, tuple]) -> dict:
    """d_t = a + b·E_t + c·P_t の最小二乗（先回りの年の期は外す）。係数が識別できなければ None。"""
    yms = _regression_periods(d)
    out = {"n_periods": len(yms), "first": yms[0] if yms else None, "last": yms[-1] if yms else None,
           "a": None, "b": None, "c": None}
    if len(yms) < 4:
        return out
    x = np.array([[1.0, *exposure[ym]] for ym in yms])
    if np.linalg.matrix_rank(x) < x.shape[1]:
        return out
    a, b, c = np.linalg.lstsq(x, np.array([d[ym] for ym in yms]), rcond=None)[0]
    out.update(a=float(a), b=float(b), c=float(c))
    return out


def nw_regression_p(d: Mapping[str, float], exposure: Mapping[str, tuple], *, lag: int = M_NW_LAG) -> dict:
    """同じ回帰の Newey-West（lag 3）の両側 p（併記用・判定には使わない）。"""
    import statsmodels.api as sm

    yms = _regression_periods(d)
    out = {"lag": lag, "se": None, "p": None}
    if len(yms) < 5:
        return out
    x = np.array([[1.0, *exposure[ym]] for ym in yms])
    if np.linalg.matrix_rank(x) < x.shape[1]:
        return out
    fit = sm.OLS(np.array([d[ym] for ym in yms]), x).fit(
        cov_type="HAC", cov_kwds={"maxlags": min(lag, len(yms) - 1)})
    out.update(se={"b": float(fit.bse[1]), "c": float(fit.bse[2])},
               p={"b": float(fit.pvalues[1]), "c": float(fit.pvalues[2])})
    return out


def _bins(values: Mapping[str, float], n_bins: int) -> dict:
    ordered = sorted(values, key=lambda ec: (values[ec], ec))
    m = len(ordered)
    return {ec: j * n_bins // m for j, ec in enumerate(ordered)}


def strata(variables: pd.DataFrame) -> dict:
    """{社: (規模十分位, 流動性三分位, 17 業種)}。3 つがそろう社だけ（分位はその集団で切る）。"""
    ok = variables["logcap"].notna() & variables["log_turnover_ratio"].notna()
    ok &= variables["sector17"].map(lambda s: isinstance(s, str))
    v = variables[ok]
    if v.empty:
        return {}
    size = _bins(v["logcap"].to_dict(), SIZE_BINS)
    liq = _bins(v["log_turnover_ratio"].to_dict(), LIQUIDITY_BINS)
    return {ec: (size[ec], liq[ec], v.at[ec, "sector17"]) for ec in v.index}


def stratum_name(s: tuple) -> str:
    return f"size{s[0]}|liq{s[1]}|{s[2]}"


def pseudo_groups(strata_by_ec: Mapping[str, tuple], treated: Iterable[str], eligible: Iterable[str], *,
                  n: int = M_PERMUTATIONS, seed: int = SEED) -> tuple:
    """層ごとに処置社と同じ社数を `eligible` から非復元で引いた擬似処置群を `n` 個。戻り値は (群のリスト, 不足)。"""
    need = Counter(strata_by_ec[t] for t in treated if t in strata_by_ec)
    pool: dict = {}
    for ec in sorted(set(eligible)):
        if ec in strata_by_ec:
            pool.setdefault(strata_by_ec[ec], []).append(ec)
    shortfall = {stratum_name(s): k - len(pool.get(s, [])) for s, k in sorted(need.items())
                 if len(pool.get(s, [])) < k}
    rng = np.random.default_rng(seed)
    groups = []
    for _ in range(n):
        g: set = set()
        for s, k in sorted(need.items()):
            cand = pool.get(s, [])
            take = min(k, len(cand))
            if take:
                g.update(cand[i] for i in rng.choice(len(cand), size=take, replace=False))
        groups.append(g)
    return groups, shortfall


def m_family_test(aligned: Mapping[tuple, tuple], pairs: Mapping[str, Sequence[str]],
                  calendar: Sequence[PressureWeek], strata_by_ec: Mapping[str, tuple],
                  eligible: Iterable[str], *, permutations: int = M_PERMUTATIONS,
                  seed: int = SEED) -> dict:
    """1 モデルの M 族: d_t = bias_T(t) − bias_C(t) を E_t・P_t へ回帰し、固定した擬似処置群の順列で p を出す。

    要約は件数と統計だけで、銘柄コードを含めない。
    """
    df = placements(aligned)
    present = {ym: set(sub["ec"]) for ym, sub in df.groupby("ym")}
    domain = paired_domain(present, pairs)
    t_by_ym = {ym: t for ym, (t, _c) in domain.items() if t}
    c_by_ym = {ym: c for ym, (t, c) in domain.items() if t}
    bias_t, bias_c = group_bias(df, t_by_ym), group_bias(df, c_by_ym)
    d = {ym: bias_t[ym] - bias_c[ym] for ym in sorted(bias_t) if ym in bias_c}
    exposure = {ym: label_window_exposure(ym, calendar) for ym in present}
    fit = fit_m_regression(d, exposure)
    # 順列: 行ごとの placement を 1 回だけ配列にし、擬似群ごとの期の平均を bincount で取る
    yms = sorted(present)
    ym_idx = {ym: i for i, ym in enumerate(yms)}
    codes = sorted(set(df["ec"]))
    ec_idx = {ec: i for i, ec in enumerate(codes)}
    row_ym = df["ym"].map(ym_idx).to_numpy(dtype=int)
    row_ec = df["ec"].map(ec_idx).to_numpy(dtype=int)
    row_p = df["p"].to_numpy(float)
    matched = [t for t in sorted(pairs) if t in strata_by_ec]
    groups, shortfall = pseudo_groups(strata_by_ec, matched, eligible, n=permutations, seed=seed)
    exceed = dict.fromkeys(M_COEFS, 0)
    n_valid = 0
    if fit["b"] is not None:
        for g in groups:
            member = np.zeros(len(codes), dtype=bool)
            member[[ec_idx[ec] for ec in g if ec in ec_idx]] = True
            rows = member[row_ec]
            cnt = np.bincount(row_ym[rows], minlength=len(yms))
            tot = np.bincount(row_ym[rows], weights=row_p[rows], minlength=len(yms))
            d_star = {ym: tot[i] / cnt[i] - bias_c[ym] for ym, i in ym_idx.items()
                      if cnt[i] and ym in bias_c}
            f = fit_m_regression(d_star, exposure)
            if f["b"] is None:
                continue
            n_valid += 1
            for k in M_COEFS:
                exceed[k] += abs(f[k]) >= abs(fit[k])
    p_perm = ({k: (exceed[k] + 1) / (n_valid + 1) for k in M_COEFS} if n_valid
              else dict.fromkeys(M_COEFS))
    ic_t, ic_c = _group_rank_ic(df, t_by_ym), _group_rank_ic(df, c_by_ym)
    return {
        "n_rows": int(len(df)),
        "n_matched_treated": len(matched),
        "fit": fit,
        "newey_west": nw_regression_p(d, exposure),
        "permutation": {"n": permutations, "n_valid": n_valid, "seed": seed, "p": p_perm,
                        "pseudo_size": len(groups[0]) if groups else 0, "shortfall": shortfall},
        "d_by_period": d,
        "exposure": {ym: list(v) for ym, v in sorted(exposure.items())},
        "lead_periods": {ym: v for ym, v in d.items() if ym.startswith(M_LEAD_YEAR)},
        "groups": {
            "n_treated_by_period": {ym: len(t) for ym, t in sorted(t_by_ym.items())},
            "n_control_by_period": {ym: len(c) for ym, c in sorted(c_by_ym.items())},
            "treated_rank_ic": float(np.mean(list(ic_t.values()))) if ic_t else None,
            "control_rank_ic": float(np.mean(list(ic_c.values()))) if ic_c else None,
        },
    }


def m_family_verdict(results: Mapping[str, dict]) -> dict:
    """{モデル: {係数: {coef, p, significant}}}。Bonferroni（各 α = 0.0125・両側）。"""
    out = {}
    for model, r in results.items():
        out[model] = {}
        for k in M_COEFS:
            p = r["permutation"]["p"][k]
            out[model][k] = {"coef": r["fit"][k], "p": p,
                             "significant": None if p is None else p < M_ALPHA}
    return out
