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
歪むのは学習・バックテストのパネル（M-1 / M-2 / M-6）である。例外は期末後分割の年の行で、株数と配当だけが
分割前の基準に残るため、最新行でも market_cap / div_yield / nc_ratio が歪む（#751・#753）。

## このスクリプトが測るもの・測らないもの

測る: 該当社数・該当行数・歪み倍率の分布・断面順位への影響・株価基準の内訳・公式との一致率。
測らない: 修正後の rank-IC（M-2 / M-6 の OOF は数時間〜数日かかるので日中バッチのキュー行き）。

**DB へは1バイトも書かない。** 修正方式（#653 の案A/B/C）は、ここで出た数字を見てから別 issue
で決める。issue 本文は「どれを採るかは実測後に決める」としており、先に直すと根拠が残らない。

## 検出の中身は台帳にある

歪みの向き（`COLUMN_DIRECTION`）・分割比の復元（第1経路・第2経路・純資産チェック・上場廃止を
またぐペアの除外）・既定値は**本番の台帳 `corporate_actions.py` が唯一の源**で、このスクリプトは
それを import して測るだけである（#746・ADR-0062。以前は本番がここを import していた）。
設計の説明も台帳の docstring にある。

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
    python -m scripts.measure_split_valuation_bias verify-sources   # 整合度照合（#751）で増えるイベントを
                                     # DB の公式と Yahoo の分割履歴に照らし、事前登録した基準1〜3を判定する

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 検出器は台帳（本番）が唯一の源。測定はそれを import するだけで写さない（#746・ADR-0062）。
from corporate_actions import (  # noqa: E402
    COLUMN_DIRECTION, DEFAULT_BPS_PATH, DEFAULT_BPS_TOL, DEFAULT_CONSISTENCY_CROSSCHECK,
    DEFAULT_EQUITY_TOL, DEFAULT_MIN_RATIO, DEFAULT_SNAP_TOL, LISTING_GAP_MIN_DAYS, AnnualRow,
    ShareEvent, MatchResult, _iso, _log, _usable, bars_spans, compute_ledger, cumulative_factors,
    detect_events, event_window, in_coverage, load_ledger_inputs, match_event, merge_spans,
    official_ratio_in_window, window_confirmed,
)

# 感度表と既定判定に使う格子。0.15 は本物の分割の伸び（第1経路 split の p95 1.168）に掛かる。
EQUITY_TOL_GRID: tuple[float, ...] = (0.15, 0.25, 0.40, 0.60, 1.00)

# verify-sources の基準（#751・ADR-0055 決定4-10）。**測る前に固定した値で、結果を見て動かさない。**
# 基準1: 公式と比べられる新規イベントの倍率一致率 >= SOURCES_MIN_AGREE_RATE（分母 >= SOURCES_MIN_DENOMINATOR）
# 基準2: 公式の受信区間が窓を覆うのに公式イベントが無い（＝分割は無かったと確かめられた）新規イベントが 0 件
# 基準3: 公式で確かめられない新規イベントの Yahoo との一致率 >= SOURCES_MIN_AGREE_RATE（分母 >= 同上）
# 一致の許容は verify-sample の `--match-tol` 既定（`match_event` の 5%）と同じ。
SOURCES_MIN_AGREE_RATE = 0.90
SOURCES_MIN_DENOMINATOR = 20
SOURCES_MATCH_TOL = 0.05
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
# 権利落ち日の UNIX 秒を日付へ直すときの時差（Yahoo の東証銘柄は 00:00 UTC＝09:00 JST で返る）。
JST = timezone(timedelta(hours=9))

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
DEFAULT_SOURCES_JSON = ROOT / "scripts" / ".cache" / "measure_split_valuation_bias_sources.json"


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


#: 一致率の分母に入れる status。**ここが分母の唯一の源**で、CLI へ書き写さない。
#: `no_official_event` が入るのは full 窓のときだけ——公式が返さない＝分割が無かったと
#: 読めるので「検出が間違い」として数える。partial 窓の同じ状況は契約窓の外で起きた分割と
#: 区別できないので `no_official_event_partial` にして分母から外す（#659）。
#: 公式のバーが窓を覆っていない `no_official_bars` も同じ理由で外す（#668）。
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


# ── verify-sources（#751・純関数）───────────────────────────────────────────────

def parse_yahoo_splits(chart: Mapping) -> tuple[Optional[list[tuple[str, float]]], dict]:
    """Yahoo `v8/finance/chart`（`events=split`）の応答から `([(権利落ち日, 株数比), ...], meta)` を取り出す。

    株数比は `numerator / denominator`（2:1 分割は 2.0・1:10 併合は 0.1）＝検出器の `sh_ratio` と同じ向き。
    日付は `date`（権利落ち日の UNIX 秒）を JST の日付にする（キーは月足の開始時刻で、日付ではない）。
    `events` が無いのは「分割が無い」＝`[]`。**応答が壊れている・`chart.error` がある・分割の中身が
    読めないときは None**——「分割が無い」と同じ形にすると、取れなかった社が不一致に数えられる。
    """
    try:
        result = chart["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None, {}
    if not isinstance(result, dict):
        return None, {}
    meta = result.get("meta") or {}
    out = []
    for ev in ((result.get("events") or {}).get("splits") or {}).values():
        try:
            num, den, ts = float(ev["numerator"]), float(ev["denominator"]), int(ev["date"])
        except (KeyError, TypeError, ValueError):
            return None, meta
        if num <= 0 or den <= 0:
            return None, meta
        out.append((datetime.fromtimestamp(ts, tz=JST).date().isoformat(), num / den))
    return sorted(out), meta


def yahoo_ratio_in_window(splits: Sequence[tuple[str, float]], window: Optional[tuple[str, str]]
                          ) -> tuple[Optional[float], int]:
    """窓 (w0, w1] の中の Yahoo 分割の株数比の積と件数。無ければ (None, 0)。

    窓の意味を割らないために `official_ratio_in_window` をそのまま使う（Yahoo の株数比を公式の
    `AdjFactor` の向き＝逆数へ直して渡す）。
    """
    return official_ratio_in_window([(d, 1.0 / r) for d, r in splits], window)


def _agree(observed: float, magnitude: float, tol: float) -> bool:
    return abs(_log(observed / magnitude)) <= _log(1 + tol)


def judge_against_official(ev: ShareEvent, official: Sequence[tuple[str, float]],
                           spans: Sequence[Sequence[str]], *, tol: float = SOURCES_MATCH_TOL
                           ) -> tuple[str, Optional[float]]:
    """公式との照合。`(status, 公式の株数比)`。

    - `official_magnitude`: 倍率を公式から決めたイベント。公式と比べれば定義上必ず一致するので基準1の
      分母に入れない（整合度との一致は検出器が採る条件として既に確かめている）
    - `agree` / `disagree`: 窓の中に公式イベントがある（基準1の分母）
    - `absent`: 公式のバーを受け取った区間が窓を覆うのに公式イベントが無い＝分割は無かったと確かめられた（基準2）
    - `unconfirmed`: 公式では確かめられない（区間が窓を覆わない・取り込んでいない）。Yahoo で確かめる（基準3）
    """
    if ev.official_ratio is not None:
        return "official_magnitude", ev.official_ratio
    ratio, _ = official_ratio_in_window(official, event_window(ev))
    if ratio is not None:
        return ("agree" if _agree(ratio, ev.canonical, tol) else "disagree"), ratio
    if window_confirmed(ev, merge_spans(spans)):
        return "absent", None
    return "unconfirmed", None


def judge_against_yahoo(ev: ShareEvent, splits: Optional[Sequence[tuple[str, float]]], *,
                        tol: float = SOURCES_MATCH_TOL) -> tuple[str, Optional[float]]:
    """Yahoo との照合。`(status, Yahoo の株数比)`。

    `unavailable`（取得できない・ティッカーが無い・取引所が違う）は基準3の分母に入れない。
    `no_split`（窓の中に Yahoo の分割が無い）は**不一致として分母に入れる**（決定4-10 で測る前に決めた規則）。
    """
    if splits is None:
        return "unavailable", None
    ratio, _ = yahoo_ratio_in_window(splits, event_window(ev))
    if ratio is None:
        return "no_split", None
    return ("agree" if _agree(ratio, ev.canonical, tol) else "disagree"), ratio


def judge_sources(official_status: Mapping[str, int], yahoo_status: Mapping[str, int], *,
                  min_rate: float = SOURCES_MIN_AGREE_RATE,
                  min_n: int = SOURCES_MIN_DENOMINATOR) -> dict:
    """事前登録した基準1〜3（ADR-0055 決定4-10）の判定。**分母が `min_n` に届かない基準は満たさない。**"""
    o, y = Counter(official_status), Counter(yahoo_status)
    d1 = o["agree"] + o["disagree"]
    r1 = o["agree"] / d1 if d1 else None
    d3 = y["agree"] + y["disagree"] + y["no_split"]
    r3 = y["agree"] / d3 if d3 else None
    c1 = d1 >= min_n and r1 is not None and r1 >= min_rate
    c2 = o["absent"] == 0
    c3 = d3 >= min_n and r3 is not None and r3 >= min_rate
    return {
        "criterion1": {"pass": c1, "agree_rate": r1, "denominator": d1},
        "criterion2": {"pass": c2, "absent": o["absent"]},
        "criterion3": {"pass": c3, "agree_rate": r3, "denominator": d3,
                       "unavailable": y["unavailable"]},
        "min_agree_rate": min_rate, "min_denominator": min_n,
        "pass": c1 and c2 and c3,
    }


def ledger_diff(off, on) -> dict:
    """同じ入力で整合度照合を切った台帳 `off` と入れた台帳 `on` の差（#751）。

    **照合で認めたイベントが足されるだけで、既存のイベントは1件も動かない**ことを確かめるための数を返す
    （`changed_existing` と `added_not_by_consistency` と `removed` が空であるべき）。
    """
    def key(e):
        return (e.edinet_code, e.year, _iso(e.period_end), e.source)
    off_ev = {key(e): e for e in off.events}
    on_ev = {key(e): e for e in on.events}
    added = sorted(on_ev.keys() - off_ev.keys())
    removed = sorted(off_ev.keys() - on_ev.keys())
    changed_existing = sorted(k for k in off_ev.keys() & on_ev.keys()
                              if off_ev[k].canonical != on_ev[k].canonical)
    rows = sorted(k for k in set(on.factors) | set(off.factors)
                  if on.factors.get(k, 1.0) != off.factors.get(k, 1.0))
    aw = lambda led: len((led.stats.get("bps_path") or {}).get("awaiting_magnitude") or ())  # noqa: E731
    return {
        "n_added": len(added),
        "added_not_by_consistency": [list(k) for k in added
                                     if on_ev[k].cross_check != "consistency"],
        "removed": [list(k) for k in removed],
        "changed_existing": [list(k) for k in changed_existing],
        "n_rows_changed": len(rows),
        "n_rows_newly_corrected": sum(1 for k in rows if off.factors.get(k, 1.0) == 1.0),
        "n_companies": len({ec for ec, _ in rows}),
        "awaiting": {"off": aw(off), "on": aw(on)},
        "ttm_windows": {"off": sum(len(w) for w in off.windows_by_company().values()),
                        "on": sum(len(w) for w in on.windows_by_company().values())},
    }


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


async def fetch_official(ec_secs: Sequence[tuple[str, str]], cover: tuple[str, str], *,
                         on_progress=None) -> dict[str, tuple[list, list]]:
    """`{edinet_code: (公式の企業イベント, バーを受け取った区間)}`。

    **イベントと区間は必ず同じバーから作る**（#668）。イベントだけ返すと「バーが0本」と「イベントが
    無い」を呼び出し側が区別できない。レート制御も認証も既存実装を再利用する（二重実装しない）。
    """
    from scripts.repair_splits_from_jquants import collect_official, extract_events

    bars = await collect_official(list(ec_secs), cover, on_progress=on_progress)
    return {ec: (extract_events(rows), bars_spans(rows)) for ec, rows in bars.items()}


def load_yahoo_tickers(db, ecs: Sequence[str]) -> dict[str, tuple[str, Optional[str]]]:
    """`{edinet_code: (Yahoo のティッカー, companies.yahoo_suffix)}`。証券コードの無い社は含めない。

    ティッカーの作り方は収集器と同じ `collector_utils.yahoo_ticker`（東証以外の単独上場は
    `yahoo_suffix` が要る・#555）。
    """
    from sqlalchemy import text as sqla_text

    from collector_utils import yahoo_ticker
    if not ecs:
        return {}
    rows = db.execute(sqla_text(
        "SELECT edinet_code, sec_code, yahoo_suffix FROM companies "
        "WHERE edinet_code = ANY(:ecs)"), {"ecs": list(ecs)}).fetchall()
    db.commit()
    return {r[0]: (yahoo_ticker(r[1], r[2]), r[2]) for r in rows if r[1]}


def fetch_yahoo_splits(targets: Mapping[str, tuple[str, Optional[str], str, str]], *,
                       sleep: float = 1.0, on_progress=None
                       ) -> dict[str, Optional[list[tuple[str, float]]]]:
    """`{edinet_code: [(権利落ち日, 株数比), ...] or None}`。`targets` は `{ec: (ticker, suffix, 開始日, 終了日)}`。

    1社1リクエスト（月足・`events=split`）で、期間は呼び出し側がイベント窓を覆うように渡す。取得失敗・
    `chart.error`・取引所の食い違い（`.F` は Frankfurt と衝突する・#555）・円建てでない応答は None
    （＝`unavailable`。「分割が無い」とは区別する）。**読み取りのみ**で、DB には書かない。
    """
    import time

    import httpx

    from collector_utils import YAHOO_EXPECT_CURRENCY, yahoo_expect_exchanges

    out: dict[str, Optional[list[tuple[str, float]]]] = {}
    with httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0",
                                           "Accept": "application/json"}) as client:
        for i, (ec, (ticker, suffix, d0, d1)) in enumerate(sorted(targets.items()), 1):
            if i > 1 and sleep > 0:
                time.sleep(sleep)
            p1 = int(datetime.fromisoformat(d0).replace(tzinfo=timezone.utc).timestamp())
            p2 = int(datetime.fromisoformat(d1).replace(tzinfo=timezone.utc).timestamp()) + 86399
            try:
                r = client.get(YAHOO_CHART_URL.format(ticker=ticker),
                               params={"interval": "1mo", "period1": p1, "period2": p2,
                                       "events": "split"})
                chart = r.json()
            except (httpx.HTTPError, ValueError):
                out[ec] = None
                continue
            splits, meta = parse_yahoo_splits(chart)
            expect = yahoo_expect_exchanges(suffix)
            if splits is not None and (
                    (expect and meta.get("exchangeName") not in expect)
                    or (meta.get("currency") and meta.get("currency") != YAHOO_EXPECT_CURRENCY)):
                splits = None
            out[ec] = splits
            if on_progress:
                on_progress(i, len(targets), "%s %s 分割 %s" % (
                    ec, ticker, "取得失敗" if splits is None else len(splits)))
    return out


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
                                      price_series=series,
                                      consistency_crosscheck=args.consistency_crosscheck)
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
                                           price_series=series,
                                           consistency_crosscheck=args.consistency_crosscheck)
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
                                       equity_tol=et, price_series=series,
                                       consistency_crosscheck=args.consistency_crosscheck)
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
                         price_series=series,
                         consistency_crosscheck=args.consistency_crosscheck)
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

        fetched = asyncio.run(fetch_official(
            targets, cover, on_progress=lambda i, t, m: print("  %s" % m, flush=True)))
        official = {ec: ev for ec, (ev, _) in fetched.items()}
        spans_of = {ec: sp for ec, (_, sp) in fetched.items()}

        results: list[MatchResult] = []
        pos_set = set(pos)
        for e in events:
            if e.edinet_code not in pos_set:
                continue
            results.append(match_event(e, official.get(e.edinet_code, []),
                                       slack_days=args.window_slack_days,
                                       tol=args.match_tol, coverage=cover,
                                       coverage_mode=args.coverage,
                                       official_spans=spans_of.get(e.edinet_code, [])))
        misses = [ec for ec in ctrl if official.get(ec)]
        # 対照群でバーが0本の社は「見逃し 0」の根拠にならない（#668）。数だけ並べる。
        ctrl_no_bars = [ec for ec in ctrl if not spans_of.get(ec)]

        tally, denom, rate, rate_raw = tally_rates(results)

        print()
        print("突合結果: " + ", ".join("%s=%d" % kv for kv in sorted(tally.items())))
        print("一致率（スナップ後）= %.3f / （生比も許容）= %.3f / 分母 %d"
              % (rate, rate_raw, denom))
        print("対照群の見逃し（公式にイベントがあった社）= %d 社 %s"
              % (len(misses), misses or ""))
        if ctrl_no_bars:
            print("対照群のうち公式のバーが0本で判定できない社 = %d 社 %s"
                  % (len(ctrl_no_bars), ctrl_no_bars))
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
                "control_misses": misses, "control_no_bars": ctrl_no_bars,
                "rows": [r._asdict() for r in results],
            }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print("JSON: %s" % p)
        return 0
    finally:
        db.close()


def _cmd_verify_sources(args) -> int:
    """整合度照合（#751）で新しく採るイベントを、DB の公式 AdjFactor と Yahoo の分割履歴に照らす。

    **本番と同じ入力（`load_ledger_inputs`）を1回だけ読み**、整合度照合を切った台帳と入れた台帳を並べる。
    公式は `scripts/backfill_adj_factor_events.py` が DB に取り込んだものだけを使い、J-Quants は叩かない。
    """
    import database as D

    db = D.SessionLocal()
    try:
        inputs = load_ledger_inputs(db)
        db.commit()
        if inputs is None:
            raise SystemExit("annual 行がありません")
        kw = dict(official=inputs["official"], coverage=inputs["coverage"], series=inputs["series"])
        off = compute_ledger(inputs["rows"], consistency_crosscheck=False, **kw)
        on = compute_ledger(inputs["rows"], consistency_crosscheck=True, **kw)
        diff = ledger_diff(off, on)
        print("annual %d行・接続先=%s" % (len(inputs["rows"]), D.DB_TARGET))
        print("整合度照合で増えるイベント %d件 / F が変わる行 %d（新たに補正 %d）/ %d社 / "
              "倍率待ち %d -> %d / TTM の分割窓 %d -> %d"
              % (diff["n_added"], diff["n_rows_changed"], diff["n_rows_newly_corrected"],
                 diff["n_companies"], diff["awaiting"]["off"], diff["awaiting"]["on"],
                 diff["ttm_windows"]["off"], diff["ttm_windows"]["on"]))
        print("既存イベントの変化: 倍率が変わった %d / 消えた %d / 照合以外で増えた %d（すべて 0 であるべき）"
              % (len(diff["changed_existing"]), len(diff["removed"]),
                 len(diff["added_not_by_consistency"])))

        targets = [e for e in on.events if e.source == "bps" and e.cross_check == args.cross_check]
        if args.only:
            only = {x.strip() for x in args.only.split(",") if x.strip()}
            targets = [e for e in targets if e.edinet_code in only]
        results = []
        for e in sorted(targets, key=lambda e: (e.edinet_code, e.year)):
            st, ratio = judge_against_official(e, on.official.get(e.edinet_code, ()),
                                               inputs["coverage"].get(e.edinet_code, ()),
                                               tol=args.match_tol)
            results.append({"edinet_code": e.edinet_code, "year": e.year, "kind": e.kind,
                            "magnitude": e.canonical, "consistency": e.consistency,
                            "lagged_sh_ratio": e.lagged_sh_ratio, "window": event_window(e),
                            "official_status": st, "official_ratio": ratio,
                            "yahoo_status": None, "yahoo_ratio": None})
        o_tally = Counter(r["official_status"] for r in results)
        print()
        print("照合する %s 経路のイベント %d件・公式との照合: %s"
              % (args.cross_check, len(results), dict(sorted(o_tally.items()))))

        y_tally: Counter = Counter()
        pending = [r for r in results if r["official_status"] == "unconfirmed"]
        if pending and not args.no_yahoo:
            tickers = load_yahoo_tickers(db, sorted({r["edinet_code"] for r in pending}))
            spans: dict[str, tuple[str, str]] = {}
            for r in pending:
                if r["window"] is None:
                    continue
                w0, w1 = r["window"]
                s = spans.get(r["edinet_code"])
                spans[r["edinet_code"]] = (min(w0, s[0]), max(w1, s[1])) if s else (w0, w1)
            fetch = {ec: (tickers[ec][0], tickers[ec][1], w0, w1)
                     for ec, (w0, w1) in spans.items() if ec in tickers}
            print("Yahoo から %d社の分割履歴を取ります（ティッカー無し %d社）..."
                  % (len(fetch), len(spans) - len(fetch)), flush=True)
            got = fetch_yahoo_splits(fetch, sleep=args.sleep)
            ev_of = {(e.edinet_code, e.year): e for e in targets}
            for r in pending:
                st, ratio = judge_against_yahoo(ev_of[(r["edinet_code"], r["year"])],
                                                got.get(r["edinet_code"]), tol=args.match_tol)
                r["yahoo_status"], r["yahoo_ratio"] = st, ratio
            y_tally = Counter(r["yahoo_status"] for r in pending)
            print("Yahoo との照合: %s" % dict(sorted(y_tally.items())))

        verdict = judge_sources(o_tally, y_tally)
        c1, c2, c3 = verdict["criterion1"], verdict["criterion2"], verdict["criterion3"]
        print()
        print("基準1（公式との一致率 >= %.2f・分母 >= %d）: %s（一致率 %s・分母 %d）"
              % (verdict["min_agree_rate"], verdict["min_denominator"],
                 "OK" if c1["pass"] else "NG", _fmt(c1["agree_rate"], "%.3f"), c1["denominator"]))
        print("基準2（公式で分割なしと確かめられた新規イベント 0 件）: %s（%d 件）"
              % ("OK" if c2["pass"] else "NG", c2["absent"]))
        print("基準3（Yahoo との一致率 >= %.2f・分母 >= %d）: %s（一致率 %s・分母 %d・取得できず %d）"
              % (verdict["min_agree_rate"], verdict["min_denominator"],
                 "OK" if c3["pass"] else "NG", _fmt(c3["agree_rate"], "%.3f"), c3["denominator"],
                 c3["unavailable"]))
        print("判定: %s" % ("採用（基準をすべて満たした）" if verdict["pass"] else "見送り"))
        for r in results:
            if r["official_status"] in ("disagree", "absent") or r["yahoo_status"] in (
                    "disagree", "no_split"):
                print("  %-9s %s 倍率 %s 整合度 %s 公式 %s(%s) Yahoo %s(%s) 窓 %s"
                      % (r["edinet_code"], r["year"], _fmt(r["magnitude"]),
                         _fmt(r["consistency"]), r["official_status"], _fmt(r["official_ratio"]),
                         r["yahoo_status"], _fmt(r["yahoo_ratio"]), r["window"]))

        if args.json:
            p = Path(args.json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "db_target": D.DB_TARGET, "cross_check": args.cross_check,
                "match_tol": args.match_tol, "diff": diff, "official_tally": dict(o_tally),
                "yahoo_tally": dict(y_tally), "verdict": verdict, "rows": results,
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
    # 第2経路の整合度照合（#751）。既定は `DEFAULT_CONSISTENCY_CROSSCHECK`＝毎晩の係数表と同じ。
    common.add_argument("--consistency-crosscheck", dest="consistency_crosscheck",
                        action="store_true", default=DEFAULT_CONSISTENCY_CROSSCHECK,
                        help="第2経路の EPS 照合に落ちたペアを整合度照合で救う（#751）")
    common.add_argument("--no-consistency-crosscheck", dest="consistency_crosscheck",
                        action="store_false", help="整合度照合を使わない（#740 までの検出と同一）")

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

    # 検出器の設定は本番の既定（台帳）で固定する＝`common` を持たない。測る相手は本番がこれから採るもの。
    s = sub.add_parser("verify-sources",
                       help="整合度照合（#751）で増えるイベントを DB の公式と Yahoo の分割履歴に照らす")
    s.add_argument("--cross-check", choices=("consistency", "eps"), default="consistency",
                   help="照らすイベントの交差検証（既定: 整合度照合で認めたもの）")
    s.add_argument("--match-tol", type=float, default=SOURCES_MATCH_TOL)
    s.add_argument("--sleep", type=float, default=1.0, help="Yahoo へのリクエスト間隔（秒）")
    s.add_argument("--no-yahoo", action="store_true", help="Yahoo を叩かない（基準3は判定しない）")
    s.add_argument("--only", default="", help="edinet_code をカンマ区切りで絞る")
    s.add_argument("--json", nargs="?", const=str(DEFAULT_SOURCES_JSON),
                   default=str(DEFAULT_SOURCES_JSON))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    args = build_parser().parse_args(argv)
    return {"detect": _cmd_detect, "verify-sample": _cmd_verify_sample,
            "verify-sources": _cmd_verify_sources}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
