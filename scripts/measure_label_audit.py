"""採点の物差しの点検（#1154）: 答え（52 週先リターン）が配当を含まない影響と、52 週以内に上場廃止した社が
答えから消える影響を、事前登録した規則で測る計測 CLI。

規則は `docs/prereg/label-audit.md`（本文は main へのマージで凍結）。判定は道具が `prereg_decision` に
機械的に出す。読み手は手で判定し直さない。

## 手続き

- **予測は変えない。** `sell_mu_source_bakeoff --in-layer`（#1043）と同じく M-4 を 1 回回して共通域の社ごとの
  OOF を取る（`_run_config(keep_common=True)`・M-4 の基底は既定の設定・`M4_BASES_CAVEAT`）。本番の設定の M-2 は
  参考で `oof_portfolio_replay.production_predictions`（#1123）から取り、共通域の行だけ使う。学習の手続きはどれも
  既存のまま＝予測は今の計測と同じもの。変えるのは**答えだけ**
- **答えの作り直しと照合**: 各 (月, 社) の月末の週の終値と 52 行先の終値を、パネルと同じ規則
  （`month_end_indices`・`HORIZON_WEEKS`）で引く。値上がりだけの答え `log(c_fut / c_snap)` がパネルの答えと
  1 行でも食い違えば終了コード 3（別の答えを作っていたら比較が無意味になる）。値上がりだけの採点の rank-IC と
  全社の売り側spread の期別系列が M-4 の `base_oof` と食い違っても終了コード 3（同じ共通域を測れていない）
- **配当込みの答え** `y_total = log((c_fut + D) / c_snap)`。D は「スナップ日より後に期末を迎える最初の通期」の
  年間 1 株配当 `dps` を `split_factor × split_dps_lag` で割って今の株数の基準へそろえたもの（`financial_metrics`
  VIEW の `div_yield` と同じ補正・`corporate_actions.COLUMN_DIRECTION`）。DB に配当の基準日・権利落ち日は無いので、
  期末日を基準日とみなし、52 週の窓に入る配当をほぼ 1 年分（期末＋中間）として年間配当 1 回分で近似する。その通期の
  行が無ければスナップ時点で見えていた通期の `dps` で代用し、`dps` が空なら 0 とする（件数を出す）
- **消えた行**: M-2 / M-6 のレグの構築（`MacroEnsemblePlugin._m2_build`）にだけ `build_snapshots(return_no_future=True)`
  を足し、特徴量の条件をすべて通ったのに答えを作れなかった行を取る（条件を書き写さない）。株価の日付で
  「直近（まだ 52 週たっていない＝数えない）」「系列が途中で終わった（上場廃止・売買停止）」「系列は続くが週の欠けで
  52 行に届かない」に分け、途中で終わった行を値動きの形で「TOB/MBO の疑い」「倒産の疑い」「不明」に分ける
  （分類は参考。閾値は途中で終わった行の割合にだけ当てる）
- **限界**: 株価の収集を始める前に廃止した社（株価履歴ゼロ）はパネルにそもそも出てこないので、初期の月の
  件数は下限。分類は終値の形だけで、出来高（廃止後の出来高 0 の足・#769）は見ない（パネルのキャッシュに無い）

日中キュー `measure:label-audit`（並走に敏感）で単独に回す。結果は `scripts/.cache/label_audit.json`。

終了コード: 0 = 完了 / 2 = 事前登録の不一致 / 3 = 入力がそろわない・照合が合わない。

実行:
    python -m scripts.measure_label_audit --allow-full-pull --refresh-cache \\
        --prereg docs/prereg/label-audit.md --prereg-sha <凍結値>
    python -m scripts.measure_label_audit --smoke --allow-full-pull --prereg ... --prereg-sha ...  # 動作確認
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_stats import paired_ic_significance  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    HORIZON_WEEKS, OPERATIONAL_TOP_K, _add_days, in_layer_short_side, month_end_indices, oof_backtest,
)
from scripts import prereg  # noqa: E402

PREREG = "docs/prereg/label-audit.md"
_OUT = ROOT / "scripts" / ".cache" / "label_audit.json"
EXIT_INPUT = 3

# 事前登録 §2・§3 の対象と量（本文と同じ値。変えるなら文書を凍結し直す）。
BASES = ["macro_risk_return", "macro_gbdt", "macro_enet"]          # M-4 の基底の並び（M-1・M-2・M-6）
SHORT = {"macro_risk_return": "M-1", "macro_gbdt": "M-2", "macro_enet": "M-6", "production": "M-2*"}
PAIR = ("macro_enet", "macro_gbdt")                                   # 判定の対 M-6 − M-2（既定の設定）
PRIMARY_SERIES = ("rank_ic_by_period", "short_side_spread_by_period")  # 主審 2 本
ALPHA = 0.05 / len(PRIMARY_SERIES)                                    # Bonferroni（1 検定あたり 0.025）
TOP_K = OPERATIONAL_TOP_K                                             # 上位10社（ADR-0070）
DELIST_SHARE_THRESHOLD = 0.01                                         # ユーザー判断（2026-11-09）
LAYER_BUY = "macro_enet"                                              # 上位層を作る買いの並び（M-6・λ=0）

# 消えた行の分け方（事前登録 §2・参考）
HORIZON_DAYS = HORIZON_WEEKS * 7      # 52 週＝364 日
ENDED_MARGIN_DAYS = 28                # 全体の最新週から 4 週以内に終わる系列は「収集の遅れ」とみなし直近に数える
PIN_WEEKS = 4                         # TOB/MBO の疑い: 最後の 4 本の終値の値幅が
PIN_RANGE = 0.03                      #   3% 以内に張り付く
COLLAPSE_RATIO = 0.30                 # 倒産の疑い: 最後の終値がスナップ時点の 30% 以下（70% 以上の下落）
NEXT_FY_MAX_DAYS = 400                # 次の通期の期末がこれより先なら、その年の行が欠けているとみなし代用する
YIELD_BANDS = (0.0, 0.01, 0.02, 0.03, 0.04)   # 配当利回り D / c_snap の帯の下限（0 ちょうどは別の帯）

REASONS = ("tob_suspect", "bankruptcy_suspect", "unknown")
DIV_SOURCES = ("next_fy", "fallback_asof", "none")


class InputMismatch(RuntimeError):
    """照合が合わない・入力がそろわない（終了コード 3）。"""


# ── 答えの作り直し ──────────────────────────────────────────────────────────────

def snap_positions(prices_by_co: dict, keys) -> dict:
    """`{(ym, 社): (snap_idx, 日付列, 終値列)}`。月末の週の位置はパネルと同じ `month_end_indices`。"""
    per_co: dict[str, set] = defaultdict(set)
    for ym, ec in keys:
        per_co[ec].add(ym)
    out: dict[tuple, tuple] = {}
    for ec, yms in per_co.items():
        rows = prices_by_co.get(ec) or []
        dates = [r.trade_date for r in rows]
        closes = [r.close_last for r in rows]
        for i in month_end_indices(dates):
            ym = dates[i][:7]
            if ym in yms:
                out[(ym, ec)] = (i, dates, closes)
    return out


def label_prices(prices_by_co: dict, y: dict) -> dict:
    """答えのある行の `{(ym, 社): (c_snap, c_fut, snap_date)}`。パネルの答えと照合し、合わなければ InputMismatch。"""
    pos = snap_positions(prices_by_co, y)
    out: dict[tuple, tuple] = {}
    bad: list = []
    for key, yt in y.items():
        if key not in pos:
            bad.append((key, "no_snap"))
            continue
        i, dates, closes = pos[key]
        if i + HORIZON_WEEKS >= len(closes):
            bad.append((key, "no_future"))
            continue
        c_snap, c_fut = closes[i], closes[i + HORIZON_WEEKS]
        if not (c_snap and c_fut and c_snap > 0 and c_fut > 0) or abs(math.log(c_fut / c_snap) - yt) > 1e-12:
            bad.append((key, "mismatch"))
            continue
        out[key] = (float(c_snap), float(c_fut), dates[i])
    if bad:
        raise InputMismatch(f"作り直した答えがパネルの答えと合わない行が {len(bad)} 件（例: {bad[:3]}）。"
                            "別の答えを作っているので比べない")
    return out


# ── 配当 ────────────────────────────────────────────────────────────────────────

def dividend_in_window(recs: list, snap_date: str) -> tuple[float, str, bool]:
    """52 週の窓で受け取る 1 株配当の近似 `(D, 出どころ, dps が空だったか)`。D は今の株数の基準。

    recs = 期末の昇順の `(period_end 'YYYY-MM-DD', dps, split_factor, split_dps_lag)`（通期の行）。
    スナップ日より後に期末を迎える最初の通期の dps を使う（期末日を基準日とみなす）。その行が無い、または期末が
    NEXT_FY_MAX_DAYS より先（その年の行が欠けている）なら、スナップ日以前に期末を迎えた最新の通期で代用する。
    """
    nxt = next((r for r in recs if r[0] > snap_date), None)
    if nxt is not None and nxt[0] <= _add_days(snap_date, NEXT_FY_MAX_DAYS):
        rec, source = nxt, "next_fy"
    else:
        prev = [r for r in recs if r[0] <= snap_date]
        if not prev:
            return 0.0, "none", False
        rec, source = prev[-1], "fallback_asof"
    _pe, dps, factor, lag = rec
    if dps is None:
        return 0.0, source, True
    scale = (factor or 1.0) * (lag or 1.0)
    if not scale > 0:
        scale = 1.0
    return float(dps) / scale, source, False


def total_return_labels(prices: dict, dividends: dict) -> tuple[dict, dict, dict]:
    """配当込みの答え `{(ym, 社): y_total}`、配当 `{(ym, 社): D}`、出どころの件数。

    prices = `label_prices` の戻り値。dividends = `{社: [(period_end, dps, split_factor, split_dps_lag)…]}`。
    D = 0 の行の y_total は値上がりだけの答えとビット一致する（同じ式に 0 を足すだけ）。
    """
    y_total: dict[tuple, float] = {}
    div: dict[tuple, float] = {}
    counts: Counter = Counter()
    for key, (c_snap, c_fut, snap_date) in prices.items():
        d, source, null = dividend_in_window(dividends.get(key[1]) or [], snap_date)
        counts[source] += 1
        if null:
            counts["dps_null"] += 1
        div[key] = d
        y_total[key] = math.log((c_fut + d) / c_snap)
    return y_total, div, {k: counts.get(k, 0) for k in (*DIV_SOURCES, "dps_null")}


def yield_band(dy: float) -> str:
    """配当利回り D / c_snap の帯の名前。"""
    if dy <= 0:
        return "0"
    lo = max(b for b in YIELD_BANDS if b < dy)
    hi = next((b for b in YIELD_BANDS if b > lo), None)
    return f"({lo:.0%},{hi:.0%}]" if hi is not None and dy <= hi else f">{lo:.0%}"


def _describe(vals: list[float]) -> dict:
    return {"n": len(vals),
            "mean": round(statistics.mean(vals), 6) if vals else None,
            "median": round(statistics.median(vals), 6) if vals else None}


def dividend_effect(prices: dict, y: dict, y_total: dict, div: dict, companies: dict) -> dict:
    """`y_total − y` の分布（利回り帯別・業種別）。"""
    by_band: dict[str, list] = defaultdict(list)
    by_ind: dict[str, list] = defaultdict(list)
    for key, (c_snap, _c_fut, _d) in prices.items():
        diff = y_total[key] - y[key]
        by_band[yield_band(div[key] / c_snap)].append(diff)
        comp = companies.get(key[1])
        by_ind[getattr(comp, "industry", None) or "不明"].append(diff)
    order = ["0"] + [yield_band(b + 1e-9) for b in YIELD_BANDS]
    return {
        "all": _describe([y_total[k] - y[k] for k in prices]),
        "by_yield_band": {b: _describe(by_band[b]) for b in order if b in by_band},
        "by_industry": {i: _describe(v) for i, v in sorted(by_ind.items(), key=lambda kv: -len(kv[1]))},
    }


# ── 消えた行 ────────────────────────────────────────────────────────────────────

def classify_no_future(key: tuple, prices_by_co: dict, global_last: str) -> dict:
    """答えの無い行 1 つを分ける。`{"kind": recent|ended|gaps, "reason", "r_last"}`。

    kind: 直近（52 週たっていない or 全体の最新週から ENDED_MARGIN_DAYS 以内）は数えない。系列の最後の足が
    スナップ日 + 52 週より前なら ended（上場廃止・売買停止）、そうでなければ gaps（続いているが週の欠けで 52 行に
    届かない）。ended は値動きの形で reason を付ける（参考）。r_last = 最後の終値で手仕舞った単純リターン。
    """
    ym, ec = key
    rows = prices_by_co.get(ec) or []
    dates = [r.trade_date for r in rows]
    closes = [r.close_last for r in rows]
    snap_idx = next((i for i in month_end_indices(dates) if dates[i][:7] == ym), None)
    if snap_idx is None:
        return {"kind": "missing", "reason": None, "r_last": None}
    snap_date = dates[snap_idx]
    horizon_end = _add_days(snap_date, HORIZON_DAYS)
    if horizon_end > _add_days(global_last, -ENDED_MARGIN_DAYS):
        return {"kind": "recent", "reason": None, "r_last": None}
    if dates[-1] >= horizon_end:
        return {"kind": "gaps", "reason": None, "r_last": None}
    c_snap = closes[snap_idx]
    tail = [c for c in closes[snap_idx:] if c and c > 0]
    last = tail[-1] if tail else None
    r_last = (last / c_snap - 1.0) if (last and c_snap and c_snap > 0) else None
    if r_last is not None and last / c_snap <= COLLAPSE_RATIO:
        reason = "bankruptcy_suspect"
    elif len(tail) >= PIN_WEEKS and max(tail[-PIN_WEEKS:]) / min(tail[-PIN_WEEKS:]) - 1.0 <= PIN_RANGE:
        reason = "tob_suspect"
    else:
        reason = "unknown"
    return {"kind": "ended", "reason": reason, "r_last": r_last}


def delisting_report(no_future: dict, labelled: dict, prices_by_co: dict, yms: Sequence[str],
                     y: dict) -> dict:
    """消えた行の件数・割合・理由・リターンの水準。

    no_future = `{ym: [社]}`（`build_snapshots(return_no_future=True)`）、labelled = `{ym: [社]}`（同じ構築の答えの
    ある行）。割合 = ended ÷（答えのある行＋ended＋gaps）。yms（共通域の test 月）で主の値を、全月で参考を出す。
    答えのある行も、直近（全体の最新週から ENDED_MARGIN_DAYS 以内に 52 週目が来る）は分母から外す（両側をそろえる）。
    リターンの水準は、答えのある行（y は対数）を単純リターンへ直したものと並べる（y は共通域の行だけ）。
    """
    global_last = max((rows[-1].trade_date for rows in prices_by_co.values() if rows), default="")
    cutoff = _add_days(global_last, -ENDED_MARGIN_DAYS) if global_last else ""
    lab_pos = snap_positions(prices_by_co, [(ym, ec) for ym, ecs in labelled.items() for ec in ecs])

    def _tally(month_set: set) -> dict:
        n_lab = 0
        for (ym, _ec), (i, dates, _c) in lab_pos.items():
            if ym in month_set and _add_days(dates[i], HORIZON_DAYS) <= cutoff:
                n_lab += 1
        kinds: Counter = Counter()
        reasons: Counter = Counter()
        r_last: dict[str, list] = defaultdict(list)
        by_year: dict[str, Counter] = defaultdict(Counter)
        by_month: dict[str, Counter] = defaultdict(Counter)
        for ym, ecs in no_future.items():
            if ym not in month_set:
                continue
            for ec in ecs:
                c = classify_no_future((ym, ec), prices_by_co, global_last)
                kinds[c["kind"]] += 1
                by_month[ym][c["kind"]] += 1
                by_year[ym[:4]][c["kind"]] += 1
                if c["kind"] == "ended":
                    reasons[c["reason"]] += 1
                    if c["r_last"] is not None:
                        r_last[c["reason"]].append(c["r_last"])
        for (ym, _ec), (i, dates, _c) in lab_pos.items():
            if ym in month_set and _add_days(dates[i], HORIZON_DAYS) <= cutoff:
                by_month[ym]["labelled"] += 1
                by_year[ym[:4]]["labelled"] += 1
        denom = n_lab + kinds["ended"] + kinds["gaps"]
        all_r = [v for vals in r_last.values() for v in vals]
        bankrupt_minus_100 = [(-1.0 if reason == "bankruptcy_suspect" else v)
                              for reason, vals in r_last.items() for v in vals]

        def _share(cnt: Counter) -> dict:
            d = cnt["labelled"] + cnt["ended"] + cnt["gaps"]
            return {"labelled": cnt["labelled"], "ended": cnt["ended"], "gaps": cnt["gaps"],
                    "recent": cnt["recent"], "ended_share": round(cnt["ended"] / d, 6) if d else None}

        return {
            "n_labelled": n_lab, "n_ended": kinds["ended"], "n_gaps": kinds["gaps"],
            "n_recent_excluded": kinds["recent"], "n_missing_snap": kinds["missing"],
            "ended_share": round(kinds["ended"] / denom, 6) if denom else None,
            "gaps_share": round(kinds["gaps"] / denom, 6) if denom else None,
            "ended_by_reason": {r: reasons.get(r, 0) for r in REASONS},
            "ended_return": {
                "exit_at_last_close": _describe(all_r),
                "bankruptcy_suspect_minus_100": _describe(bankrupt_minus_100),
                "by_reason": {r: _describe(r_last.get(r, [])) for r in REASONS},
            },
            "by_year": {yr: _share(c) for yr, c in sorted(by_year.items())},
            "by_month": {m: _share(c) for m, c in sorted(by_month.items())},
        }

    main_months = set(yms)
    kept_simple = [math.exp(v) - 1.0 for (ym, _ec), v in y.items() if ym in main_months]
    return {
        "global_last_week": global_last,
        "test_months": _tally(main_months),
        "all_months": _tally(set(no_future) | set(labelled)),
        "labelled_return_test_months": _describe(kept_simple),
    }


# ── 採点 ────────────────────────────────────────────────────────────────────────

def score_all(preds: dict, order: dict, y: dict) -> dict:
    """モデルごとに `oof_backtest` を答え y で呼ぶ。行の並びは M-4 の共通域のまま（order = `{ym: [社]}`）。"""
    out: dict[str, dict] = {}
    for name, pred in preds.items():
        resid = {ym: [(pred[(ym, ec)], y[(ym, ec)]) for ec in ecs if (ym, ec) in pred and (ym, ec) in y]
                 for ym, ecs in order.items()}
        out[name] = oof_backtest({ym: rows for ym, rows in resid.items() if rows})
    return out


def check_against_base_oof(scores: dict, base_oof: dict) -> None:
    """値上がりだけの採点が M-4 の `base_oof`（同じ域で M-4 が出した値）と一致するか。外れたら InputMismatch。"""
    for name in BASES:
        ref = base_oof.get(name) or {}
        for series in PRIMARY_SERIES:
            want, got = ref.get(series) or {}, scores[name].get(series) or {}
            bad = sorted(ym for ym in set(want) | set(got)
                         if ym not in want or ym not in got or abs(want[ym] - got[ym]) > 1e-9)
            if bad:
                raise InputMismatch(f"{SHORT[name]} の {series} が M-4 の base_oof と一致しない（期 {bad[:5]}）。"
                                    "同じ共通域を測れていないので判定に使わない")


def metrics_row(o: dict, in_layer: dict | None) -> dict:
    """判定・参考に並べる指標を 1 行にする。"""
    k = (o.get("top_k") or {}).get(str(TOP_K)) or {}
    return {
        "rank_ic": (o.get("rank_ic") or {}).get("mean"),
        "short_side_spread": o.get("short_side_spread"),
        "in_layer_short_side": in_layer,
        "top_k_excess_return": k.get("excess_return"),
        "precision": k.get("precision"),
        "mean_pct_rank": k.get("mean_pct_rank"),
        "lift": k.get("lift"),
        "n_periods": o.get("n_periods"),
    }


def in_layer_means(preds: dict, y: dict) -> dict:
    """上位層の売り側spread（M-6 の買いの並びの上位層・#1043）の平均を基底ごとに。"""
    res = in_layer_short_side(preds[LAYER_BUY], {name: preds[name] for name in BASES}, y)
    return {name: res["by_condition"][name]["mean"] for name in BASES}


def _verdict(sig: dict | None) -> str:
    """対比較の判定: "+"（正に有意）/ "-"（負に有意）/ "0"（有意でない・検定が出ない）。"""
    if not sig or not sig.get("significant"):
        return "0"
    return "+" if sig["mean"] > 0 else "-"


def _top_k_diff(a: dict, b: dict) -> float | None:
    """上位10社超過の期別系列の差 a − b の、両側に共通する期での平均。"""
    sa = (a.get("top_k_excess_return_by_period") or {}).get(str(TOP_K)) or {}
    sb = (b.get("top_k_excess_return_by_period") or {}).get(str(TOP_K)) or {}
    common = sorted(set(sa) & set(sb))
    return statistics.mean(sa[ym] - sb[ym] for ym in common) if common else None


def dividend_decision(price_only: dict, total: dict) -> dict:
    """事前登録 §4 の配当の判定。(i) 主審 2 本の判定の入れ替わり、(ii) 上位10社超過の差の符号の入れ替わり。"""
    a, b = PAIR
    tests: dict[str, dict] = {}
    flip_i = False
    for series in PRIMARY_SERIES:
        sp = paired_ic_significance(price_only[a].get(series) or {}, price_only[b].get(series) or {}, alpha=ALPHA)
        st = paired_ic_significance(total[a].get(series) or {}, total[b].get(series) or {}, alpha=ALPHA)
        vp, vt = _verdict(sp), _verdict(st)
        tests[series] = {"price_only": sp, "total": st, "verdict_price_only": vp, "verdict_total": vt,
                         "flipped": vp != vt}
        flip_i = flip_i or vp != vt
    dp, dt = _top_k_diff(price_only[a], price_only[b]), _top_k_diff(total[a], total[b])
    flip_ii = dp is not None and dt is not None and (dp > 0) != (dt > 0)
    return {
        "pair": f"{SHORT[a]} - {SHORT[b]}", "alpha": ALPHA, "tests": tests,
        "top_k_excess_diff": {"price_only": dp, "total": dt, "flipped": flip_ii},
        "flip_primary": flip_i, "flip_top_k": flip_ii,
        "decision": "propose_switch" if (flip_i or flip_ii) else "keep",
    }


def delisting_decision(ended_share: float | None) -> dict:
    """事前登録 §4 の上場廃止の判定。割合が出ない（分母 0）なら判定しない。"""
    if ended_share is None:
        return {"threshold": DELIST_SHARE_THRESHOLD, "ended_share": None, "decision": "undetermined"}
    return {"threshold": DELIST_SHARE_THRESHOLD, "ended_share": ended_share,
            "decision": "redesign" if ended_share > DELIST_SHARE_THRESHOLD else "keep"}


# ── 入力 ────────────────────────────────────────────────────────────────────────

def load_dividends(db) -> dict:
    """`{社: [(period_end, dps, split_factor, split_dps_lag)…]}`（通期・期末の昇順）。読むだけ。"""
    from database import FinancialMetric
    from scripts._cache import cached

    def _pull():
        out: dict[str, list] = defaultdict(list)
        q = (db.query(FinancialMetric.edinet_code, FinancialMetric.period_end, FinancialMetric.dps,
                      FinancialMetric.split_factor, FinancialMetric.split_dps_lag)
             .filter(FinancialMetric.period_end.isnot(None)))
        for ec, pe, dps, factor, lag in q.all():
            pe_str = pe.isoformat() if hasattr(pe, "isoformat") else str(pe)[:10]
            out[ec].append((pe_str, None if dps is None else float(dps),
                            None if factor is None else float(factor), None if lag is None else float(lag)))
        db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない
        return {ec: sorted(rows) for ec, rows in out.items()}

    return cached("label_audit_dividends_v1", _pull)


def run_m4_with_no_future(panel: tuple, db) -> tuple[dict, tuple]:
    """M-4 を 1 回回し、共通域と M-2 / M-6 のレグの構築（答えの無い行つき）を返す。"""
    from plugins import macro_ensemble
    from scripts.ensemble_base_bakeoff import _run_config

    captured: dict = {}
    orig = macro_ensemble.build_snapshots

    def _spy(*a, **kw):
        # M-2 / M-6 のレグ（`_m2_build`・交差項なし）にだけ答えの無い行を足す。M-1 のレグはそのまま
        if kw.get("build_interactions") is False:
            out = orig(*a, **{**kw, "return_no_future": True})
            captured["m2"] = out
            return out[:-1]
        return orig(*a, **kw)

    with patch.object(macro_ensemble, "build_snapshots", _spy):
        run = _run_config("M-4(3基底・rank_ic_grid)", BASES, panel, db,
                          weight_method="rank_ic_grid", keep_common=True)
    if "m2" not in captured:
        raise InputMismatch("M-2 / M-6 のレグの構築を捕まえられなかった（build_snapshots が呼ばれていない）")
    if list(run["bases"]) != BASES:
        raise InputMismatch(f"M-4 の基底の並びが想定と違う: {run['bases']}")
    return run, captured["m2"]


def build_report(run: dict, m2_build: tuple, prices_by_co: dict, companies: dict, dividends: dict,
                 production: dict | None) -> dict:
    """計測の本体（DB を触らない）。照合が合わなければ InputMismatch。"""
    from scripts.sell_mu_source_bakeoff import common_to_dicts

    preds, y = common_to_dicts(run["common"], BASES)
    order = {ym: [ec for ec, _yh, _y in rows] for ym, rows in run["common"].items()}
    prices = label_prices(prices_by_co, y)
    y_total, div, div_counts = total_return_labels(prices, dividends)

    all_preds = dict(preds)
    if production:
        all_preds["production"] = {k: v for k, v in production.items() if k in y}
    price_only = score_all(all_preds, order, y)
    total = score_all(all_preds, order, y_total)
    check_against_base_oof(price_only, run["base_oof"])

    layer_p, layer_t = in_layer_means(preds, y), in_layer_means(preds, y_total)
    table = {
        name: {"price_only": metrics_row(price_only[name], layer_p.get(name)),
               "total": metrics_row(total[name], layer_t.get(name))}
        for name in all_preds
    }

    ids = m2_build[4]
    no_future = m2_build[5]
    delist = delisting_report(no_future, ids, prices_by_co, sorted(order), y)
    return {
        "n_common_rows": len(y),
        "test_months": sorted(order),
        "dividend_sources": div_counts,
        "dividend_effect": dividend_effect(prices, y, y_total, div, companies),
        "scores": table,
        "delisting": delist,
        "prereg_decision": {
            "dividend": dividend_decision(price_only, total),
            "delisting": delisting_decision(delist["test_months"]["ended_share"]),
        },
    }


# ── 出力 ────────────────────────────────────────────────────────────────────────

def _f(v, digits: int = 4) -> str:
    return "-" if v is None else f"{v:+.{digits}f}"


def _print(report: dict, production_info: dict | None) -> None:
    print(f"\n=== 採点の物差しの点検（#1154）共通域 {report['n_common_rows']} 行・"
          f"test 月 {len(report['test_months'])} ===", flush=True)
    print(f"配当の出どころ: {report['dividend_sources']}", flush=True)
    eff = report["dividend_effect"]["all"]
    print(f"y_total - y: mean={_f(eff['mean'])} median={_f(eff['median'])} n={eff['n']}", flush=True)
    print(f"\n{'model':<6}{'label':<12}{'rank-IC':>9}{'spread':>9}{'layer':>9}{'top10':>9}"
          f"{'prec':>8}{'pctrank':>9}{'lift':>8}", flush=True)
    for name, rows in report["scores"].items():
        for label, r in rows.items():
            print(f"{SHORT.get(name, name):<6}{label:<12}{_f(r['rank_ic']):>9}{_f(r['short_side_spread']):>9}"
                  f"{_f(r['in_layer_short_side']):>9}{_f(r['top_k_excess_return']):>9}"
                  f"{_f(r['precision'], 3):>8}{_f(r['mean_pct_rank'], 3):>9}{_f(r['lift'], 3):>8}", flush=True)
    if production_info and not production_info.get("available"):
        print(f"M-2* は出せなかった: {production_info.get('reason')} {production_info.get('error', '')}", flush=True)
    d = report["delisting"]["test_months"]
    print(f"\n消えた行（test 月）: 答えあり {d['n_labelled']} / 途中で終わった {d['n_ended']} "
          f"（割合 {d['ended_share']}）/ 週の欠け {d['n_gaps']} / 直近で数えない {d['n_recent_excluded']}", flush=True)
    print(f"  理由（参考）: {d['ended_by_reason']}", flush=True)
    dec = report["prereg_decision"]
    print(f"\n判定（事前登録）: 配当={dec['dividend']['decision']}"
          f"（主審の入れ替わり={dec['dividend']['flip_primary']}・上位10社={dec['dividend']['flip_top_k']}）"
          f" / 上場廃止={dec['delisting']['decision']}（閾値 {DELIST_SHARE_THRESHOLD:.0%}）", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.measure_label_audit",
                                 description="採点の物差しの点検（#1154・配当込みの答え・52 週以内の上場廃止）")
    prereg.add_arguments(ap)
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認（判定には使わない）")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)          # 不一致は重い計算の前に終了コード 2
    if Path(args.prereg).as_posix() != PREREG:
        print(f"[label_audit] NG: 事前登録は {PREREG}（受け取った値: {args.prereg}）", file=sys.stderr)
        return prereg.EXIT_MISMATCH

    from database import SessionLocal
    from scripts._cache import set_refresh
    from scripts.ensemble_base_bakeoff import _load_panel
    from scripts.oof_portfolio_replay import production_predictions

    set_refresh(args.refresh_cache)
    panel = _load_panel(args)
    db = SessionLocal()
    try:
        try:
            run, m2_build = run_m4_with_no_future(panel, db)
            dividends = load_dividends(db)
            prod_preds, prod_info = production_predictions(panel, db, sorted(run["common"]))
            report = build_report(run, m2_build, panel[0], panel[2], dividends, prod_preds)
        except InputMismatch as e:
            print(f"[label_audit] NG: {e}", file=sys.stderr)
            return EXIT_INPUT
    finally:
        db.close()
    _print(report, prod_info)

    from scripts.sell_mu_source_bakeoff import M4_BASES_CAVEAT
    payload = {
        **stamp,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "smoke": args.smoke,
        "caveats": [M4_BASES_CAVEAT,
                    "株価の収集を始める前に廃止した社はパネルに出てこないので、初期の月の消えた行は下限",
                    "消えた行の理由は終値の形だけで分けた参考（出来高は見ない）。閾値は途中で終わった行の割合にだけ当てる"],
        "rule": {"pair": list(PAIR), "primary_series": list(PRIMARY_SERIES), "alpha": ALPHA, "top_k": TOP_K,
                 "delist_share_threshold": DELIST_SHARE_THRESHOLD, "ended_margin_days": ENDED_MARGIN_DAYS,
                 "pin_weeks": PIN_WEEKS, "pin_range": PIN_RANGE, "collapse_ratio": COLLAPSE_RATIO,
                 "next_fy_max_days": NEXT_FY_MAX_DAYS},
        "production": prod_info,
        **report,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
