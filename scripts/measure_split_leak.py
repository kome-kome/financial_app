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

1. 係数表と**同じ入力・同じ関数**でイベントと F を作る（`collector_prices.compute_split_adjustments`）。
   作った F を `split_adjustment_factors` 表と突き合わせ、ずれていれば結果を信用しない
2. 学習パネルと**同じ定義**でサンプルを作る。形成月は各社の月末の週、財務行は
   `macro_snapshots._find_applicable_fin`（期末＋45日）、ラベルは `log(close[i+52] / close[i])`
3. その財務行より後で最も近いイベントまでの年数と向き（分割 F>1 / 併合 F<1）で層に分ける
4. 月ごとに全サンプルの平均を引いた超過リターンを層ごとに平均し、社単位のブートストラップで CI を付ける

## 判定（結果を見る前に決めた・Issue #685 の本文と同じ）

分割側の点推定が **1年 > 2年 > 3年 > 4年以上** の順に並び、かつ **1年の 95%CI の下限が 0 を超える**
なら、リークの読みを維持する。どちらかが崩れれば読みを捨てる。併合の層は件数が少ないので参考値。

**注意**: 1年の層には、形成月の時点で分割が既に起きている（未来ではない）サンプルが混ざる。
イベントの正確な日付を持たないので分けられない。判定を「単調性」に置いたのはこのためで、
1年の層だけの大きさでは読まない。

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
        fin_recs = rows_by_ec.get(ec)
        if not fin_recs:
            continue
        n = len(price_rows)
        dates = [r.trade_date for r in price_rows]
        closes = [r.close_last for r in price_rows]
        month_ends = [i for i in range(n - 1) if dates[i][:7] != dates[i + 1][:7]] + [n - 1]
        evs = evs_by_ec.get(ec, ())
        for i in month_ends:
            if i < 4 or i + HORIZON_WEEKS >= n:
                continue
            c0, c1 = closes[i], closes[i + HORIZON_WEEKS]
            if not c0 or not c1 or c0 <= 0 or c1 <= 0:
                continue
            fin = find_applicable_fin(fin_recs, dates[i])
            if fin is None:
                continue
            f = factors.get((ec, fin.year), 1.0)
            ev = nearest_future_event(evs, fin.year)
            out.append(Sample(dates[i][:7], ec, stratum_of(ev, fin.year),
                              math.log(f) if f > 0 else float("nan"),
                              math.log(c1 / c0)))
    return out


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


def summarize(samples: Sequence[Sample], *, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """層ごとの件数・社数・平均超過リターン・CI・平均 log F。"""
    by: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    logf: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        by[s.stratum][s.edinet_code].append(s.label)
        logf[s.stratum].append(s.log_f)
    out: dict[str, dict] = {}
    for name in STRATA_ORDER:
        groups = by.get(name)
        if not groups:
            out[name] = {"n": 0, "n_companies": 0, "mean": None, "ci": [None, None],
                         "mean_log_f": None}
            continue
        vals = [v for g in groups.values() for v in g]
        lo, hi = cluster_bootstrap_ci(groups, n_boot=n_boot, seed=seed)
        lf = [x for x in logf[name] if x == x]
        out[name] = {
            "n": len(vals),
            "n_companies": len(groups),
            "mean": sum(vals) / len(vals),
            "ci": [lo, hi],
            "mean_log_f": (sum(lf) / len(lf)) if lf else None,
        }
    return out


def verdict(summary: Mapping[str, Mapping]) -> dict:
    """結果を見る前に決めた判定（Issue #685）。"""
    means = [summary.get(k, {}).get("mean") for k in SPLIT_ORDER]
    if any(m is None for m in means):
        return {"keep_leak_reading": False,
                "reason": "分割側の層に空きがあり、単調性を判定できない"}
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
            "split_1_ci_above_zero": ci_ok, "reason": reason}


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


def report(summary: Mapping[str, Mapping], check: Mapping, result: Mapping) -> None:
    print("")
    print("=== F の突合（作り直した値 vs split_adjustment_factors） ===")
    print(f"  computed={check['n_computed']} table={check['n_table']} "
          f"only_computed={check['only_computed']} only_table={check['only_table']} "
          f"differ={check['differ']} -> {'MATCH' if check['match'] else 'MISMATCH'}")
    print("")
    print("=== 52週先の超過リターン（月内平均を引いた log リターン）を層別 ===")
    print("stratum        samples  companies     mean     ci_lo     ci_hi  mean_logF")
    for name in STRATA_ORDER:
        r = summary[name]
        lo, hi = r["ci"]
        print(f"{name:<12} {r['n']:>9} {r['n_companies']:>10} {_num(r['mean']):>8} "
              f"{_num(lo):>9} {_num(hi):>9} {_num(r['mean_log_f'], 3):>10}")
    print("")
    print(f"=== verdict === {'KEEP' if result['keep_leak_reading'] else 'DROP'} the leak reading: "
          f"{result['reason']}")
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

    from collector_prices import compute_split_adjustments
    from database import SessionLocal, SplitAdjustmentFactor
    from plugins.macro_snapshots import _find_applicable_fin
    from scripts._cache import set_refresh
    from scripts.candidate_bakeoff import _load_prices

    db = SessionLocal()
    try:
        computed = compute_split_adjustments(db)
        if computed is None:
            print("annual 行が0件。測れない")
            return 1
        rows, events, _stats, factors = computed
        table = {(ec, int(y)): float(f) for ec, y, f in db.query(
            SplitAdjustmentFactor.edinet_code, SplitAdjustmentFactor.year,
            SplitAdjustmentFactor.factor).all()}
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
    samples = demean_by_month(build_samples(
        prices, rows_by_ec, events_by_company(events), factors, _find_applicable_fin))
    summary = summarize(samples, n_boot=args.n_boot)
    result = verdict(summary)
    report(summary, check, result)

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "factor_check": check, "summary": summary, "verdict": result,
            "n_samples": len(samples), "n_boot": args.n_boot, "seed": SEED,
            "months": [min(s.ym for s in samples), max(s.ym for s in samples)] if samples else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {p}")
    return 0 if check["match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
