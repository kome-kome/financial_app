"""分割補正の歪みが未来情報のリークかを確かめる（ADR-0055 決定7 の直接証拠）。

## 何を確かめるのか

補正前の過去断面では、分割より前の年の PER が F 倍だけ小さく見えていた。分割するのは株価が
上がった会社なので、ADR-0055 決定7 は「歪んだ PER は『この先上がる』という未来を過去の断面へ
持ち込んでいる」と読み、**補正で rank-IC が下がるのが正しい**とした。2026-09-17 の実測で
第2経路の前後は M-6 −0.0190・M-2 −0.0215 と、ほぼすべての期で下がった。

ただし「補正で下がった」は、補正が**リークを除いた**場合にも、補正が**信号を壊した**場合にも
起きる。決定7 は区別のために次の測定を約束していた:

    補正量 F と 52週先リターンの関係を、分割が起きるまでの年数で層別して並べる。
    リークなら「直近の分割ほど強く、遠い分割ほど弱い」単調な形が出る。出なければ読みを捨てる。

## 測り方

1. 係数表と**同じ入力・同じ関数**でイベントと F を作る（`corporate_actions.build_ledger`）。
   作った F を `split_adjustment_factors` 表と突き合わせ、ずれていれば結果を信用しない
2. 学習パネルと**同じ定義**でサンプルを作る。形成月は各社の月末の週、財務行は
   `macro_snapshots._find_applicable_fin`（期末＋45日）、ラベルは `log(close[i+52] / close[i])`
3. その財務行より後で最も近いイベントまでの年数と向き（分割 F>1 / 併合 F<1）で層に分ける
4. 月ごとに全サンプルの平均を引いた超過リターンを層ごとに平均し、社単位のブートストラップで CI を付ける

## 判定（結果を見る前に決めた・Issue #685 の本文と同じ）— **退役した（#687）**

分割側の点推定が **1年 > 2年 > 3年 > 4年以上** の順に並び、かつ **1年の 95%CI の下限が 0 を超える**
なら、リークの読みを維持する。どちらかが崩れれば読みを捨てる。併合の層は件数が少ないので参考値。

2026-09-17 の実走は単調でなく（2年 +0.1581 が頂点・1年 +0.0283）「読みを捨てる」になった。

**この判定は #687 で退役した。原理的に通らない層を含んでいたためである。**
`_find_applicable_fin` が年 Y の行を返す形成日の範囲は `[期末_Y + 45日, 期末_{Y+1} + 45日)` で、
年 Y+1 のイベントの窓（`corporate_actions.event_window`）は
`(期末_Y - 45日, 期末_{Y+1} + 45日]`。**前者は必ず後者に含まれる**ので、「分割まで1年」の層は
**全サンプルが**「分割が形成月より前か後か」を `period_end` から判定できない。
2年以上離れた層は形成日が必ず窓より前なので、この曖昧さを持たない（`is_separable`）。

`verdict()` は #685 時点の記録として残してあり、計算も出力も変えていないが、**新しい判断の
根拠にしない**。決定7 の根拠は定義へ置き直した（ADR-0055 の追記 2026-09-18）——保存された
per は「今日まで遡及調整された株価 ÷ 当時の EPS」で、F はその期より後の分割で決まる＝
補正前の値は時点情報でない。これは測定に依らない。

## 再測定の着手条件（#687 で測る前に登録した）

**`split_1` の層のうち**、公式 `AdjFactor` の日付（`jquants_adj_factor_events`）で「形成月より後」
と言い切れる分割だけを集め、**それが `RETRY_MIN_COMPANIES` 社以上**になったら測り直す。
本スクリプトは実行のたびにその社数を数えて出す（`count_datable_future_companies`）。
層別の平均は出さない——着手条件を満たす前に結果を見ると、事前登録が意味を失う。

**数えるのを 1年層に限るのは、公式の日付が新しい情報を足すのがそこだけだから**である。
2年以上の層は元から綺麗なので、混ぜて数えると閾値を即座に満たしたように見えるだけになる。

## 再測定（#690）— 判定式は #687 で測る前に登録したもの。測った後に動かさない

着手条件を満たしたときだけ、数えたのと**同じサンプル**（`datable_future_points`）を
`split_1_dated` の層として取り出し、平均と CI を出す。層は月内平均を引いた**後**に抜き出す
（demean の母集団は #685 と同じ全層の全サンプル。層の中で引くと平均が 0 になる）。

    `split_1_dated` の 95%CI（社単位ブートストラップ・2,000回・seed 0）の下限が 0 を超え、
    かつ平均が `none`（将来の分割も併合も無い）層の平均を上回るなら、リークの読みを維持する。
    どちらかが崩れれば、決定7 は定義上の理由だけで立つものとして確定させ、以後リークの
    量的裏付けは求めない。

判定とは別に次の2つを出すが、**どちらも判定を変えない**（事前登録に無いため）:

- 達成された CI 半幅と、閾値 100 社を導いた最小検出効果 `MIN_DETECTABLE_EFFECT` との比較。
  半幅がそれを超えていれば、DROP は「効果が無い」ではなく「測れる幅に届かなかった」かもしれない
- 参考値として、`none` を `split_1_dated` と**同じ形成月に限った**平均と CI。公式の日付は
  契約窓（2024-06 以降）にしか無く形成月が 25 か月しか無いので、全期間の `none` と比べると
  局面の違いが混ざりうる。**これは結果を見る前に足した参考値で、判定の比較相手ではない**

2026-09-19 の実走は **KEEP**（947 件 / 177 社・+0.1035・95%CI [+0.0611, +0.1497]・`none` −0.0116・
CI 半幅 0.0443）。判定コードは実測の前にコミットした（`e5c6a80`）。記録は ADR-0055 決定7 の追記。

実行:
    python -m scripts.measure_split_leak
    python -m scripts.measure_split_leak --refresh-cache      # 週次株価キャッシュを取り直す
    python -m scripts.measure_split_leak --json out.json

接続先は `FINAPP_DB_TARGET`（既定 local＝ローカル正本・#503/ADR-0038）に従う。
出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Mapping, NamedTuple, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_JSON = Path(__file__).resolve().parent / ".cache" / "measure_split_leak.json"
HORIZON_WEEKS = 52          # macro_snapshots.HORIZON_WEEKS と同じ（テストが照合する）
N_BOOT = 2000
SEED = 0
MAX_YEARS_BUCKET = 4        # 4年以上は1つの層に畳む
SPLIT_ORDER = ("split_1", "split_2", "split_3", "split_4+")
REVERSE_ORDER = ("reverse_1", "reverse_2+")
STRATA_ORDER = SPLIT_ORDER + REVERSE_ORDER + ("none",)

# 形成日がイベント窓より前だと `period_end` だけで言い切れる最小の年差（#687）。
# 年差 1 は窓の内側に必ず入る（上の docstring の包含関係）ので分けられない。
SEPARABLE_MIN_YEARS = 2
# 再測定の着手条件（#687 で測る前に登録した）。#685 の実測で `split_2` は 494 社・CI 半幅
# 0.0224 だった。半幅は概ね 1/√社数 で縮むので、最小検出効果を +0.05（`split_4+` の +0.0696 と
# 同程度）に置くと 494 x (0.0224 / 0.05)^2 = 99 社。**外挿なので、実際に測るときは達成された
# CI 半幅を確かめる**（効果量が小さければこの社数でも足りない）。
RETRY_MIN_COMPANIES = 100
# 上の 100 社を導いたときに置いた最小検出効果。再測定（#690）で**達成された CI 半幅**を
# これと比べて出す（判定は変えない）。
MIN_DETECTABLE_EFFECT = 0.05
# 公式の日付で「形成月より後」と確定した `split_1` のサンプルだけの層（#690）。#685 の表
# （`STRATA_ORDER`）には混ぜない——あちらは当時の記録の再現で、この層は別の節に出す。
DATED_STRATUM = "split_1_dated"


class Sample(NamedTuple):
    ym: str
    edinet_code: str
    stratum: str
    log_f: float
    label: float


# ── 純関数（DB に触れない・テスト対象）──────────────────────────────────────────

def _iso(v) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat()[:10] if hasattr(v, "isoformat") else str(v)[:10]


def events_by_company(events: Sequence) -> dict[str, list]:
    """定番比へ寄せられたイベントだけを社ごとに年の昇順で持つ（`cumulative_factors` と同じ除外）。"""
    out: dict[str, list] = defaultdict(list)
    for e in events:
        if e.canonical is None or e.canonical == 1.0:
            continue
        out[e.edinet_code].append(e)
    for evs in out.values():
        evs.sort(key=lambda e: e.year)
    return out


def nearest_future_event(evs: Sequence, year: int):
    """`year` の行より後で最も近いイベント。**`e.year > year`** は `cumulative_factors` と同じ述語。"""
    for e in evs:
        if e.year > year:
            return e
    return None


def stratum_of(event, row_year: int) -> str:
    """層の名前。イベントが無ければ `none`。"""
    if event is None:
        return "none"
    years = event.year - row_year
    if event.canonical > 1.0:
        return "split_4+" if years >= MAX_YEARS_BUCKET else f"split_{years}"
    return "reverse_2+" if years >= 2 else "reverse_1"


class _GapEvent(NamedTuple):
    """`stratum_of` へ年差だけを渡すための最小のイベント（`AMBIGUOUS_STRATA` の導出用）。"""
    year: int
    canonical: float


# 形成月と分割日の前後を `period_end` から分けられない層（#687）。**層名を書き写さない**
# ——`stratum_of` から導出するので、層の刻み方（`MAX_YEARS_BUCKET`）を変えても追随する。
AMBIGUOUS_STRATA = frozenset(
    stratum_of(_GapEvent(years, ratio), 0)
    for years in range(1, SEPARABLE_MIN_YEARS)
    for ratio in (2.0, 0.5)
)


def is_separable(stratum: str) -> bool:
    """その層で「分割が形成月より前か後か」を `period_end` から言い切れるか（#687）。

    `_find_applicable_fin` が年 Y の行を返す形成日の範囲 `[期末_Y + 45日, 期末_{Y+1} + 45日)` は、
    年 Y+1 のイベントの窓 `(期末_Y - 45日, 期末_{Y+1} + 45日]` に**必ず含まれる**。
    よって年差 1 の層（`split_1` / `reverse_1`）は全サンプルが曖昧で、そこへ単調性の判定を
    置くと原理的に通らない。年差 2 以上なら形成日は必ず窓より前になる。

    `none`（将来のイベントが無い）は分ける対象そのものが無いので True を返す。
    """
    return stratum not in AMBIGUOUS_STRATA


def build_samples(prices_by_co: Mapping[str, Sequence], rows_by_ec: Mapping[str, Sequence],
                  evs_by_ec: Mapping[str, Sequence], factors: Mapping[tuple[str, int], float],
                  find_applicable_fin) -> list[Sample]:
    """学習パネルと同じ定義で (形成月, 社, 層, log F, 52週先ログリターン) を作る。

    `prices_by_co` の各行は `trade_date` / `close_last` を持つ（`macro_snapshots._WEEKLY_PX`）。
    `rows_by_ec` の各行は `year` / `period_end` を持ち、**period_end の昇順**であること
    （`_find_applicable_fin` は条件を満たす最後の行を返す）。
    """
    out: list[Sample] = []
    for ec, price_rows in prices_by_co.items():
        evs = evs_by_ec.get(ec, ())
        for d, c0, c1, fin in usable_formation_points(
                price_rows, rows_by_ec.get(ec), find_applicable_fin):
            f = factors.get((ec, fin.year), 1.0)
            ev = nearest_future_event(evs, fin.year)
            out.append(Sample(d[:7], ec, stratum_of(ev, fin.year),
                              math.log(f) if f > 0 else float("nan"),
                              math.log(c1 / c0)))
    return out


def usable_formation_points(price_rows: Optional[Sequence], fin_recs: Optional[Sequence],
                            find_applicable_fin):
    """52週先ラベルが作れる形成日を `(形成日, 当時の終値, 52週先の終値, 効いている財務行)` で返す。

    `build_samples` と着手条件のカウンタ（`count_datable_future_companies`）が**共有する**
    ——片方だけで条件を変えると、数えている母集団と測っている母集団が静かにずれる。
    """
    if not fin_recs or not price_rows:
        return
    n = len(price_rows)
    dates = [r.trade_date for r in price_rows]
    closes = [r.close_last for r in price_rows]
    month_ends = [i for i in range(n - 1) if dates[i][:7] != dates[i + 1][:7]] + [n - 1]
    for i in month_ends:
        if i < 4 or i + HORIZON_WEEKS >= n:
            continue
        c0, c1 = closes[i], closes[i + HORIZON_WEEKS]
        if not c0 or not c1 or c0 <= 0 or c1 <= 0:
            continue
        fin = find_applicable_fin(fin_recs, dates[i])
        if fin is None:
            continue
        yield dates[i], c0, c1, fin


def datable_future_points(prices_by_co: Mapping[str, Sequence],
                          rows_by_ec: Mapping[str, Sequence],
                          evs_by_ec: Mapping[str, Sequence],
                          official_by_ec: Mapping[str, Sequence],
                          find_applicable_fin, window_of):
    """公式の日付で「分割が形成月より後」と言い切れるサンプルを `(社, 形成日)` で返す。

    着手条件のカウンタ（`count_datable_future_companies`）と再測定の層（#690・
    `dated_split_samples`）が**共有する唯一の述語**である。片方だけで条件を変えると、
    数えた母集団と測った母集団が静かにずれる。条件の意味はカウンタの docstring を参照。
    """
    for ec, price_rows in prices_by_co.items():
        evs = evs_by_ec.get(ec)
        official = official_by_ec.get(ec)
        if not evs or not official:
            continue
        for d, _c0, _c1, fin in usable_formation_points(
                price_rows, rows_by_ec.get(ec), find_applicable_fin):
            ev = nearest_future_event(evs, fin.year)
            if ev is None or ev.canonical is None or ev.canonical <= 1.0:
                continue
            # 公式の日付で新しく綺麗になるのは、`period_end` では分けられない層だけ
            if is_separable(stratum_of(ev, fin.year)):
                continue
            win = window_of(ev)
            if win is None:
                continue
            w0, w1 = win
            inside = [dt for dt, _f in official if w0 < dt <= w1]
            if len(inside) != 1 or inside[0] <= d:
                continue
            yield ec, d


def count_datable_future_companies(prices_by_co: Mapping[str, Sequence],
                                   rows_by_ec: Mapping[str, Sequence],
                                   evs_by_ec: Mapping[str, Sequence],
                                   official_by_ec: Mapping[str, Sequence],
                                   find_applicable_fin, window_of) -> dict:
    """再測定の着手条件を数える（#687 で測る前に登録した条件）。**平均は出さない。**

    数えるのは **`split_1`（分割まで1年）の層だけ**である。公式の日付が新しい情報を足すのは
    この層に限られる——2年以上の層は形成日が必ず窓より前で元から綺麗なので、そこを数えても
    検定力は増えず、**閾値を即座に満たしたように見せるだけ**になる（実測 全層だと 177 社 /
    11,057 件で、1年層だけなら桁が違う）。`RETRY_MIN_COMPANIES` は 1年層に必要な社数として
    導いた値である。

    採る条件は、公式 `AdjFactor` の日付（`jquants_adj_factor_events`）が最寄り未来イベントの
    窓の中に**ちょうど1件**あり、その日付が形成日より**後**であること。窓の中に2件以上あると
    どれが層を決めたイベントか分けられないので採らない。併合（`reverse_1`）は事前登録した
    判定式の対象外なので数えない。

    `window_of` には `corporate_actions.event_window` を渡す（窓の定義を写さない）。
    戻り値は `{"companies": 社数, "samples": 件数, "threshold": RETRY_MIN_COMPANIES,
    "ready": bool}`。述語の実体は `datable_future_points` にあり、再測定（#690）の層も
    同じ関数から作る。
    """
    companies: set[str] = set()
    months: set[str] = set()
    n_samples = 0
    for ec, d in datable_future_points(prices_by_co, rows_by_ec, evs_by_ec, official_by_ec,
                                       find_applicable_fin, window_of):
        companies.add(ec)
        months.add(d[:7])
        n_samples += 1
    return {"companies": len(companies), "samples": n_samples,
            "threshold": RETRY_MIN_COMPANIES,
            # 形成月の広さ。公式の日付は契約窓（2024-06 以降）にしか無いので、社数が閾値を
            # 超えても**月が数えるほどしか無い**ことがある。そのときは社単位の CI が
            # 月をまたぐ相関を拾えず、外挿した検定力より実際は弱い。
            "months": len(months),
            "month_range": [min(months), max(months)] if months else None,
            "ready": len(companies) >= RETRY_MIN_COMPANIES}


def demean_by_month(samples: Sequence[Sample]) -> list[Sample]:
    """月ごとに全サンプルの平均ラベルを引く（市場全体の上げ下げを消す）。"""
    total: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for s in samples:
        total[s.ym] += s.label
        count[s.ym] += 1
    return [s._replace(label=s.label - total[s.ym] / count[s.ym]) for s in samples]


def cluster_bootstrap_ci(values_by_company: Mapping[str, Sequence[float]], *,
                         n_boot: int = N_BOOT, seed: int = SEED,
                         alpha: float = 0.05) -> tuple[Optional[float], Optional[float]]:
    """社を単位に復元抽出したサンプル平均の CI。同じ社の月は独立ではないので社ごと引く。"""
    import numpy as np

    sums = np.array([sum(v) for v in values_by_company.values()], dtype=float)
    cnts = np.array([len(v) for v in values_by_company.values()], dtype=float)
    k = len(sums)
    if k < 2:
        return None, None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n_boot, k))
    means = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def _stratum_stats(samples: Sequence[Sample], *, n_boot: int = N_BOOT,
                   seed: int = SEED) -> dict:
    """1つの層の件数・社数・平均超過リターン・社単位 CI・平均 log F。

    `summarize`（#685 の表）と再測定の層（#690）が共有する——CI の引き方を層ごとに
    書き分けない。
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        groups[s.edinet_code].append(s.label)
    if not groups:
        return {"n": 0, "n_companies": 0, "mean": None, "ci": [None, None],
                "mean_log_f": None}
    vals = [v for g in groups.values() for v in g]
    lo, hi = cluster_bootstrap_ci(groups, n_boot=n_boot, seed=seed)
    lf = [s.log_f for s in samples if s.log_f == s.log_f]
    return {
        "n": len(vals),
        "n_companies": len(groups),
        "mean": sum(vals) / len(vals),
        "ci": [lo, hi],
        "mean_log_f": (sum(lf) / len(lf)) if lf else None,
    }


def summarize(samples: Sequence[Sample], *, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """層ごとの件数・社数・平均超過リターン・CI・平均 log F。"""
    by: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by[s.stratum].append(s)
    # `separable`: 形成月と分割日の前後を分けられる層か（#687）。分けられない層に判定を
    # 置くと原理的に通らない。
    return {name: {**_stratum_stats(by.get(name, ()), n_boot=n_boot, seed=seed),
                   "separable": is_separable(name)}
            for name in STRATA_ORDER}


def dated_split_samples(samples: Sequence[Sample], keys) -> list[Sample]:
    """再測定の層（#690）: `datable_future_points` が返した `(社, 形成日)` のサンプルだけを取る。

    `samples` は**月内平均を引いた後**のものを渡す（demean の母集団は #685 と同じ全層の
    全サンプル。層の中で引き直すと層の平均が 0 になり、判定が意味を失う）。層名は
    `DATED_STRATUM` に付け替える。

    形成日は各社の月末の週なので `(社, 形成月)` で一意に引ける。キーに対応するサンプルが
    見つからない、または `split_1` 以外だったら `RuntimeError`——両者は同じ
    `usable_formation_points` と `stratum_of` を通っているので、起きたら定義がずれた合図であり、
    黙って測り続けると数えた母集団と別のものを測ることになる。
    """
    wanted = {(ec, d[:7]) for ec, d in keys}
    picked: list[Sample] = []
    found: set[tuple[str, str]] = set()
    for s in samples:
        k = (s.edinet_code, s.ym)
        if k not in wanted:
            continue
        if s.stratum != "split_1":
            raise RuntimeError(f"{k} は着手条件で数えたのに層が {s.stratum}（split_1 のはず）")
        found.add(k)
        picked.append(s._replace(stratum=DATED_STRATUM))
    missing = wanted - found
    if missing:
        raise RuntimeError(f"着手条件で数えたサンプルが {len(missing)} 件見つからない"
                           f"（例 {sorted(missing)[:3]}）")
    return picked


RETIRED_REASON = (
    "分割まで1年の層は形成日が必ずイベント窓の内側に入るので、分割が既に起きたサンプルと"
    "未来のサンプルを period_end からは分けられない。この判定は原理的に通らない層を"
    "含んでいた。この行は #685 時点の記録であって、新しい判断の根拠にしない"
    "（ADR-0055 決定7・#687）")


def verdict(summary: Mapping[str, Mapping]) -> dict:
    """結果を見る前に決めた判定（Issue #685）。**#687 で退役した**。

    計算も出力も #685 のまま変えない——当時の記録を再現できること自体が、退役の説明の
    裏付けになる。読み手が新しい判断へ使わないよう `retired` を返し、`report` が明示する。
    """
    means = [summary.get(k, {}).get("mean") for k in SPLIT_ORDER]
    retired = {"retired": True, "superseded_by": 687, "retired_reason": RETIRED_REASON}
    if any(m is None for m in means):
        return {"keep_leak_reading": False,
                "reason": "分割側の層に空きがあり、単調性を判定できない", **retired}
    monotone = all(a > b for a, b in zip(means, means[1:]))
    lo = (summary["split_1"].get("ci") or [None])[0]
    ci_ok = lo is not None and lo > 0
    if monotone and ci_ok:
        reason = "分割側が 1年>2年>3年>4年以上 の順に並び、1年の CI 下限が 0 を超えた"
    elif not monotone:
        reason = "分割側の平均が年数の順に並ばなかった（単調でない）"
    else:
        reason = "1年の層の CI が 0 を含む（または下回る）"
    return {"keep_leak_reading": bool(monotone and ci_ok), "monotone": monotone,
            "split_1_ci_above_zero": ci_ok, "reason": reason, **retired}


def retry_verdict(dated: Mapping, none: Mapping) -> dict:
    """再測定の判定（#690）。**#687 で測る前に登録した式そのもの**で、測った後に動かさない。

    維持の条件は `dated` の 95%CI 下限 > 0 **かつ** 平均 > `none` の平均（どちらも厳密な
    不等号）。どちらかが崩れれば読みを捨てる＝決定7 は定義上の理由だけで立つ。

    CI 半幅と `MIN_DETECTABLE_EFFECT` の比較（`powered`）は**判定を変えない**。半幅が
    設計値より広いときの DROP は「効果が無い」ではなく「測れる幅に届かなかった」かもしれず、
    それを読み手へ残すためだけに返す。
    """
    mean = dated.get("mean")
    lo, hi = (list(dated.get("ci") or []) + [None, None])[:2]
    none_mean = none.get("mean")
    half = (hi - lo) / 2 if lo is not None and hi is not None else None
    base = {"preregistered": 690, "ci_half_width": half,
            "min_detectable_effect": MIN_DETECTABLE_EFFECT,
            "powered": half is not None and half <= MIN_DETECTABLE_EFFECT}
    if mean is None or lo is None or none_mean is None:
        return {"keep_leak_reading": False, "ci_lo_above_zero": None, "beats_none": None,
                "reason": "日付で確定した層か将来の分割なしの層が空で（または CI が引けず）"
                          "判定できない", **base}
    ci_ok = lo > 0
    beats = mean > none_mean
    if ci_ok and beats:
        reason = "日付で確定した層の CI 下限が 0 を超え、平均が将来の分割なしの層を上回った"
    elif not ci_ok and not beats:
        reason = "CI が 0 を含み（または下回り）、平均も将来の分割なしの層を上回らなかった"
    elif not ci_ok:
        reason = "日付で確定した層の CI が 0 を含む（または下回る）"
    else:
        reason = "日付で確定した層の平均が将来の分割なしの層を上回らなかった"
    return {"keep_leak_reading": bool(ci_ok and beats), "ci_lo_above_zero": ci_ok,
            "beats_none": beats, "reason": reason, **base}


def measure_retry(samples: Sequence[Sample], keys, ready: bool, *,
                  n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """再測定（#690）の取りまとめ。**着手条件を満たすまで平均を計算しない。**

    `samples` は月内平均を引いた後の全サンプル、`keys` は `datable_future_points` の出力。
    `ready` が False なら `{"ready": False}` だけを返す——満たす前に結果を見ると事前登録が
    意味を失うので、関数の形で「見られない」ようにしておく。

    `none_same_months` は `none` を日付で確定した層と同じ形成月に限った**参考値**で、判定の
    比較相手ではない（比較相手は事前登録の文言どおり全期間の `none`）。
    """
    if not ready:
        return {"ready": False}
    dated = dated_split_samples(samples, keys)
    months = sorted({s.ym for s in dated})
    none_all = [s for s in samples if s.stratum == "none"]
    month_set = set(months)
    none_same = [s for s in none_all if s.ym in month_set]
    dated_stats = _stratum_stats(dated, n_boot=n_boot, seed=seed)
    none_stats = _stratum_stats(none_all, n_boot=n_boot, seed=seed)
    return {
        "ready": True,
        DATED_STRATUM: dated_stats,
        "none": none_stats,
        "none_same_months": {**_stratum_stats(none_same, n_boot=n_boot, seed=seed),
                             "reference_only": True},
        "months": len(months),
        "month_range": [months[0], months[-1]] if months else None,
        "verdict": retry_verdict(dated_stats, none_stats),
    }


def compare_factors(computed: Mapping[tuple[str, int], float],
                    table: Mapping[tuple[str, int], float], tol: float = 1e-9) -> dict:
    """作り直した F と係数表の突合。表は F≠1 の行だけを持つ。"""
    nontrivial = {k: v for k, v in computed.items() if v != 1.0}
    only_computed = sorted(set(nontrivial) - set(table))
    only_table = sorted(set(table) - set(nontrivial))
    differ = sorted(k for k in set(nontrivial) & set(table)
                    if abs(nontrivial[k] - table[k]) > tol)
    return {"n_computed": len(nontrivial), "n_table": len(table),
            "only_computed": len(only_computed), "only_table": len(only_table),
            "differ": len(differ),
            "examples": [list(k) for k in (only_computed + only_table + differ)[:5]],
            "match": not (only_computed or only_table or differ)}


def _num(v, digits: int = 4) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}" if isinstance(v, float) else str(v)


def report_retry(retry: Mapping) -> None:
    """再測定（#690）の節。ASCII 記号のみ（cp932 リダイレクト対策）。"""
    print("")
    print("=== 再測定（#690・測る前に登録した判定） ===")
    if not retry.get("ready"):
        print("  着手条件を満たしていないので層別の平均は出さない")
        return
    print("stratum                  samples  companies     mean     ci_lo     ci_hi  mean_logF")
    for name, r in ((DATED_STRATUM, retry[DATED_STRATUM]), ("none", retry["none"]),
                    ("none(same months,ref)", retry["none_same_months"])):
        lo, hi = r["ci"]
        print(f"{name:<22} {r['n']:>9} {r['n_companies']:>10} {_num(r['mean']):>8} "
              f"{_num(lo):>9} {_num(hi):>9} {_num(r['mean_log_f'], 3):>10}")
    rng = retry.get("month_range")
    print(f"  形成月 {retry.get('months', 0)}"
          f"{'（' + rng[0] + ' .. ' + rng[1] + '）' if rng else ''}")
    print("  none(same months,ref) は同じ形成月に限った参考値。判定の比較相手は全期間の none")
    v = retry["verdict"]
    half = v.get("ci_half_width")
    print(f"  CI 半幅 = {_num(half)}（100 社を導いた最小検出効果 "
          f"{MIN_DETECTABLE_EFFECT:.2f} -> "
          f"{'届いた' if v.get('powered') else '届かなかった（判定は変えない）'}）")
    print(f"=== verdict (#690 preregistered) === "
          f"{'KEEP' if v['keep_leak_reading'] else 'DROP'} the leak reading: {v['reason']}")


def report(summary: Mapping[str, Mapping], check: Mapping, result: Mapping,
           trigger: Optional[Mapping] = None, retry: Optional[Mapping] = None) -> None:
    print("")
    print("=== F の突合（作り直した値 vs split_adjustment_factors） ===")
    print(f"  computed={check['n_computed']} table={check['n_table']} "
          f"only_computed={check['only_computed']} only_table={check['only_table']} "
          f"differ={check['differ']} -> {'MATCH' if check['match'] else 'MISMATCH'}")
    print("")
    print("=== 52週先の超過リターン（月内平均を引いた log リターン）を層別 ===")
    print("stratum        samples  companies     mean     ci_lo     ci_hi  mean_logF  separable")
    for name in STRATA_ORDER:
        r = summary[name]
        lo, hi = r["ci"]
        sep = "yes" if r.get("separable", is_separable(name)) else "NO"
        print(f"{name:<12} {r['n']:>9} {r['n_companies']:>10} {_num(r['mean']):>8} "
              f"{_num(lo):>9} {_num(hi):>9} {_num(r['mean_log_f'], 3):>10} {sep:>10}")
    print("  separable=NO は形成月と分割日の前後を period_end から分けられない層（#687）")
    print("")
    print(f"=== verdict (RETIRED #685 -> #687) === "
          f"{'KEEP' if result['keep_leak_reading'] else 'DROP'} the leak reading: "
          f"{result['reason']}")
    print(f"  [retired] {result.get('retired_reason', RETIRED_REASON)}")
    if trigger is not None:
        print("")
        print("=== 再測定の着手条件（#687 で測る前に登録した） ===")
        rng = trigger.get("month_range")
        print(f"  公式の分割日で「形成月より後」と言い切れる社数 = {trigger['companies']}"
              f" / {trigger['threshold']}（サンプル {trigger['samples']}・"
              f"形成月 {trigger.get('months', 0)}"
              f"{'（' + rng[0] + '〜' + rng[1] + '）' if rng else ''}）"
              f" -> {'READY' if trigger['ready'] else 'NOT YET'}")
    if retry is not None:
        report_retry(retry)
    if not check["match"]:
        print("  [warn] F が係数表と一致しない。この結果で判断しないこと")


# ── I/O ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()

    ap = argparse.ArgumentParser(
        prog="python -m scripts.measure_split_leak",
        description="分割補正の歪みがリークかを層別で確かめる（ADR-0055 決定7）")
    ap.add_argument("--json", dest="json_out", default=str(DEFAULT_JSON))
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に DB からのフルロードを許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="週次株価キャッシュを取り直す")
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    args = ap.parse_args(argv)

    from corporate_actions import build_ledger, event_window
    from database import SessionLocal, SplitAdjustmentFactor
    from plugins.macro_snapshots import _find_applicable_fin
    from scripts._cache import set_refresh
    from scripts.candidate_bakeoff import _load_prices

    db = SessionLocal()
    try:
        ledger = build_ledger(db)
        if ledger is None:
            print("annual 行が0件。測れない")
            return 1
        rows, events, factors = ledger.rows, ledger.events, ledger.factors
        table = {(ec, int(y)): float(f) for ec, y, f in db.query(
            SplitAdjustmentFactor.edinet_code, SplitAdjustmentFactor.year,
            SplitAdjustmentFactor.factor).all()}
        # 着手条件（#687）を数えるための公式の分割日。検出器へ渡したものと同じ（台帳が持つ）。
        official = ledger.official
        db.commit()
    finally:
        db.close()

    check = compare_factors(factors, table)
    rows_by_ec: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.period_end is not None:
            rows_by_ec[r.edinet_code].append(r)
    for rs in rows_by_ec.values():
        rs.sort(key=lambda r: _iso(r.period_end))

    set_refresh(args.refresh_cache)
    prices = _load_prices(args.allow_full_pull)
    evs_by_ec = events_by_company(events)
    samples = demean_by_month(build_samples(
        prices, rows_by_ec, evs_by_ec, factors, _find_applicable_fin))
    summary = summarize(samples, n_boot=args.n_boot)
    result = verdict(summary)
    trigger = count_datable_future_companies(
        prices, rows_by_ec, evs_by_ec, official, _find_applicable_fin, event_window)
    # 再測定（#690）: 数えたのと同じ述語でサンプルを取り出す。READY でなければ平均は出さない
    keys = list(datable_future_points(
        prices, rows_by_ec, evs_by_ec, official, _find_applicable_fin, event_window))
    retry = measure_retry(samples, keys, trigger["ready"], n_boot=args.n_boot)
    report(summary, check, result, trigger, retry)

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "factor_check": check, "summary": summary, "verdict": result,
            "retry_trigger": trigger, "retry": retry,
            "n_samples": len(samples), "n_boot": args.n_boot, "seed": SEED,
            "months": [min(s.ym for s in samples), max(s.ym for s in samples)] if samples else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {p}")
    return 0 if check["match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
