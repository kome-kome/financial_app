"""業種の空欄補完（#797）を入れる前と後で、過去年度の分析がどれだけ動くかを書き込みなしで測る。

## 何を測るのか

業種が空の社（大半は廃止社）を EDINET コードリストの提出者業種で埋めると、次の読み手が動く
（ADR-0065）。当日の gap_ratio だけは動かない（夜間の sector_ols は `tradable_filters` で廃止社を読まない）。

1. 当日 gap（夜間と同じ設定の sector_ols・保存しない経路）＝**完全一致を要求する**
2. 業種内Z（VIEW `financial_metrics` の `z_roe_sec` / `z_op_margin_sec`・M-2/M-6 の既定特徴量）
3. 時点再現の gap パネル（`build_period_panel(with_gap_ratio=True)`・ADR-0057 の母集団減少率）
4. M-1・M-2・M-6 の OOF（`model_comparison.run_comparison` そのもの）

どれも本番の関数を呼ぶだけで、測る手続きは持たない（ADR-0041）。

## なぜ1つのトランザクションの中で前後を測るのか

補完の前の状態を DB に残したまま、補完の後を見るため。同じセッションで「補完前を測る → 補完を流す
→ 補完後を測る → ROLLBACK」とし、正本には何も書かない。補完の関数は中で commit するので、
模擬の間（`simulated`）は次のように差し替える:

- `db.commit` は flush にする（書いた行はトランザクションの中だけに残る）
- `db.rollback` は「呼ばれたら数えて例外」にする。`run_comparison` は失敗したモデルで rollback するので、
  黙って補完が消えると**以後の「補完後」が補完前を測る**。例外は握られることがあるので、回数を見て中断する
- `Session.commit(db)` のような差し替えを迂回する commit は `before_commit` で止める
- 抜けるときに本物の rollback をし、新しいセッションで業種が空の社数が計測前と同じことを確かめる

## 止める条件（ADR-0065・どれか1つでも当たれば exit=1）

OOF の rank-IC の符号は**採否の条件にしない**。業種が空の群は「今日の JPX 一覧に載っていない＝その後に
廃止した社」の印で、過去の断面から見ると未来情報（リーク）なので、埋めて下がっても「リークが抜けた」と
読む。止めるのは壊れたとき:

- 当日 gap がキー集合・値のどちらかで1件でも変わる
- 母集団が縮む（gap パネルで gap を付けた後の行数が減る月・OOF の `n_oof_samples` の減少・
  補完しなかった行で業種内Z が非 NULL → NULL）
- 補完後の業種名の種類が変わる
- 模擬実行が壊れた（rollback が呼ばれた・補完が見えていない・ROLLBACK 後に空の社数が戻らない）

実行:
    python -m scripts.measure_industry_fill_impact
    python -m scripts.measure_industry_fill_impact --skip-oof
    python -m scripts.measure_industry_fill_impact --summarize scripts/.cache/measure_industry_fill_impact.json

接続先は `FINAPP_DB_TARGET`（既定 local＝ローカル正本・#503/ADR-0038）に従う。模擬の間は補完した
社と財務行に行ロックが掛かる（読みは塞がない）。重い計算なので、何も並走させずに回すこと。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event, func, or_, text  # noqa: E402

from database import Company, FinancialRecord, SessionLocal  # noqa: E402

OOF_MODELS = ("macro_risk_return", "macro_gbdt", "macro_enet")
DEFAULT_JSON = Path(__file__).resolve().parent / ".cache" / "measure_industry_fill_impact.json"


class SimulationBroken(RuntimeError):
    """模擬実行の前提（補完がトランザクションの中に見えている）が崩れた。"""


# ── 模擬実行の安全装置 ─────────────────────────────────────────────────────────

@contextlib.contextmanager
def simulated(db):
    """中で行った書き込みを、抜けるときに必ず捨てる（例外で抜けても）。

    yield する dict の `rollback_calls` が 0 でなければ、中の計測は補完前を測った可能性がある。
    """
    state = {"rollback_calls": 0, "commit_blocked": 0}
    real_rollback = db.rollback

    def _forbidden_rollback():
        state["rollback_calls"] += 1
        raise SimulationBroken("模擬実行中に rollback が呼ばれた（補完が消え、以後の計測が補完前を測る）")

    def _forbidden_commit(_session):
        state["commit_blocked"] += 1
        raise SimulationBroken("模擬実行中に commit されようとした（差し替えを迂回した経路）")

    db.commit = db.flush
    db.rollback = _forbidden_rollback
    event.listen(db, "before_commit", _forbidden_commit)
    try:
        yield state
    finally:
        event.remove(db, "before_commit", _forbidden_commit)
        del db.commit
        del db.rollback
        real_rollback()


def count_empty(db) -> dict:
    """業種が空（NULL・空文字）の会社数と財務行数。"""
    def _empty(col):
        return or_(col.is_(None), col == "")
    return {
        "companies": db.query(func.count(Company.edinet_code)).filter(_empty(Company.industry)).scalar(),
        "financial_records": db.query(func.count()).select_from(FinancialRecord)
                               .filter(_empty(FinancialRecord.industry)).scalar(),
    }


def distinct_industries(db) -> int:
    return (db.query(func.count(func.distinct(Company.industry)))
            .filter(Company.industry.isnot(None), Company.industry != "").scalar())


def apply_fill(db, codelist: dict) -> dict:
    """本番（Phase 5）と同じ選び方・書き方で補完を流す。`simulated` の内側で呼ぶこと。"""
    from collector_master import _apply_industry_fill, _plan_industry_fill, _propagate_company_industry

    by_industry, rejected_listed, out_of_scope = _plan_industry_fill(db, codelist)
    filled_co = _apply_industry_fill(db, by_industry)
    filled_fr = _propagate_company_industry(db)     # 中の commit は flush に化ける
    return {
        "filled_codes": sorted(c for codes in by_industry.values() for c in codes),
        "by_industry": {k: len(v) for k, v in sorted(by_industry.items(), key=lambda kv: -len(kv[1]))},
        "rejected_listed": dict(rejected_listed),
        "out_of_scope": dict(out_of_scope),
        "filled_companies": filled_co,
        "filled_financial_records": filled_fr,
    }


def fetch_codelist() -> dict:
    import httpx

    from collector_master import _read_edinet_codelist
    from collector_utils import EDINET_CODELIST_URL

    r = httpx.get(EDINET_CODELIST_URL, timeout=60)
    r.raise_for_status()
    return _read_edinet_codelist(r.content)


# ── 測るもの（本番の関数を呼ぶだけ）─────────────────────────────────────────────

def measure_today_gap(db) -> dict:
    """夜間と同じ設定で当日回帰を当てはめ、行ごとの gap と業種ごとの α を返す（保存しない）。"""
    from plugins.sector_ols import plugin
    from scripts.measure_ridge_alpha_stability import fit_all
    from sector_gap_asof import nightly_params

    params = nightly_params()
    records = plugin._load_records(db, params["year"], params["features"])
    res = fit_all(plugin, records, params)
    return {"gap": {"|".join(map(str, k)): v for k, v in res["gap"].items()},
            "alpha": res["alpha"], "n_records": len(records)}


def measure_sector_z(db) -> dict:
    """VIEW の業種内Z（通期行）を `{edinet_code|year|period_end: [z_roe_sec, z_op_margin_sec]}` で返す。"""
    rows = db.execute(text(
        "SELECT edinet_code, year, period_end, z_roe_sec, z_op_margin_sec FROM financial_metrics")).all()
    return {f"{ec}|{y}|{pe}": [_f(a), _f(b)] for ec, y, pe, a, b in rows}


def measure_gap_panel(db) -> dict:
    """時点再現の gap パネル。月ごとの (付ける前, 付けた後) の行数と、行ごとの gap を返す。

    行ごとの gap は `build_period_panel` が中で呼ぶ `build_asof_gaps` の戻りをそのまま写し取る
    （手続きは変えない。同じ計算を2回走らせない）。
    """
    import sector_gap_asof
    from recommend_factor_premia import build_period_panel

    captured: dict = {}
    original = sector_gap_asof.build_asof_gaps

    def _capture(*args, **kwargs):
        gaps, stats = original(*args, **kwargs)
        captured["gaps"], captured["stats"] = gaps, stats
        return gaps, stats

    coverage: dict = {}
    sector_gap_asof.build_asof_gaps = _capture
    try:
        build_period_panel(db, with_gap_ratio=True, gap_coverage=coverage)
    finally:
        sector_gap_asof.build_asof_gaps = original
    return {
        "coverage": {ym: list(v) for ym, v in sorted(coverage.items())},
        "gap": {f"{ec}|{ym}": g for (ec, ym), g in captured.get("gaps", {}).items()},
        "stats": captured.get("stats", {}),
    }


def measure_oof(db) -> dict:
    from model_comparison import run_comparison

    res = asyncio.run(run_comparison(db, only_models=list(OOF_MODELS)))
    out = {}
    for m in res.get("models", []):
        o = m.get("oof_backtest") or {}
        out[m["name"]] = {
            "available": bool(m.get("available")),
            "error": m.get("error"),
            "rank_ic": (o.get("rank_ic") or {}).get("mean"),
            "rank_ic_industry_neutral": (o.get("rank_ic_industry_neutral") or {}).get("mean"),
            "short_side_spread": o.get("short_side_spread"),
            "n_oof_samples": o.get("n_oof_samples"),
            "n_periods": o.get("n_periods"),
            "rank_ic_by_period": o.get("rank_ic_by_period") or {},
            "short_side_spread_by_period": o.get("short_side_spread_by_period") or {},
        }
    return out


# ── 比べる（純関数）─────────────────────────────────────────────────────────────

def _f(v):
    return None if v is None else float(v)


def _spearman(xs: list, ys: list):
    if len(xs) < 3:
        return None
    from scipy.stats import spearmanr
    v = float(spearmanr(xs, ys).statistic)
    return None if math.isnan(v) else round(v, 6)


def _abs_stats(diffs: list) -> dict:
    if not diffs:
        return {"n": 0}
    import numpy as np
    a = np.abs(np.asarray(diffs, dtype=float))
    return {"n": int(a.size), "median": round(float(np.median(a)), 6),
            "p90": round(float(np.quantile(a, 0.9)), 6), "max": round(float(a.max()), 6),
            "changed": int((a > 1e-12).sum())}


def compare_today_gap(before: dict, after: dict) -> dict:
    kb, ka = set(before["gap"]), set(after["gap"])
    common = kb & ka
    diffs = [after["gap"][k] - before["gap"][k] for k in common
             if before["gap"][k] is not None and after["gap"][k] is not None]
    none_mismatch = sum(1 for k in common if (before["gap"][k] is None) != (after["gap"][k] is None))
    alpha_changed = sorted(s for s in set(before["alpha"]) | set(after["alpha"])
                           if before["alpha"].get(s) != after["alpha"].get(s))
    identical = (kb == ka and none_mismatch == 0 and all(d == 0 for d in diffs) and not alpha_changed)
    return {"identical": identical, "only_before": len(kb - ka), "only_after": len(ka - kb),
            "none_mismatch": none_mismatch, "abs_diff": _abs_stats(diffs),
            "alpha_changed": alpha_changed}


def compare_sector_z(before: dict, after: dict, filled: set, still_empty: set) -> dict:
    """行を3群に分けて比べる。

    - `others`: 前から業種があった社（業種は同じで、群の顔ぶれだけが変わる）。止める条件はここだけ
    - `filled`: 今回埋めた社（空の群から実業種の群へ移る）
    - `still_empty`: 33業種外で空のまま残る社（空の群が小さくなる。1社だけの年は z が NULL になる）
    """
    groups = (("others", lambda ec: ec not in filled and ec not in still_empty),
              ("filled", lambda ec: ec in filled),
              ("still_empty", lambda ec: ec in still_empty))
    out = {}
    for label, keep in groups:
        cols = {}
        for i, col in enumerate(("z_roe_sec", "z_op_margin_sec")):
            xs, ys, to_null, from_null = [], [], 0, 0
            by_year: dict = {}
            for key, vb in before.items():
                ec, year = key.split("|")[:2]
                if not keep(ec) or key not in after:
                    continue
                b, a = vb[i], after[key][i]
                if b is not None and a is None:
                    to_null += 1
                elif b is None and a is not None:
                    from_null += 1
                elif b is not None and a is not None:
                    xs.append(b)
                    ys.append(a)
                    by_year.setdefault(year, ([], []))
                    by_year[year][0].append(b)
                    by_year[year][1].append(a)
            cols[col] = {
                "spearman": _spearman(xs, ys),
                "abs_diff": _abs_stats([y - x for x, y in zip(xs, ys)]),
                "to_null": to_null, "from_null": from_null,
                "spearman_by_year": {y: _spearman(*v) for y, v in sorted(by_year.items())},
            }
        out[label] = cols
    return out


def compare_gap_panel(before: dict, after: dict) -> dict:
    def _shrink(cov):
        vals = [1 - a / b for b, a in cov.values() if b]
        return sorted(vals)

    def _median(vals):
        if not vals:
            return None
        n = len(vals)
        return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 6)

    shrunk_months = sorted(ym for ym, (_b, a) in after["coverage"].items()
                           if ym in before["coverage"] and a < before["coverage"][ym][1])
    common = set(before["gap"]) & set(after["gap"])
    xs = [before["gap"][k] for k in sorted(common)]
    ys = [after["gap"][k] for k in sorted(common)]
    return {
        "n_months": len(after["coverage"]),
        "median_shrink_before": _median(_shrink(before["coverage"])),
        "median_shrink_after": _median(_shrink(after["coverage"])),
        "rows_with_gap_before": sum(a for _b, a in before["coverage"].values()),
        "rows_with_gap_after": sum(a for _b, a in after["coverage"].values()),
        "months_where_rows_shrank": shrunk_months,
        "gaps_before": len(before["gap"]), "gaps_after": len(after["gap"]),
        "gaps_lost": len(set(before["gap"]) - set(after["gap"])),
        "spearman_common": _spearman(xs, ys),
        "abs_diff_common": _abs_stats([y - x for x, y in zip(xs, ys)]),
    }


def compare_oof(before: dict, after: dict) -> dict:
    from model_stats import paired_family_significance

    pairs = {name: (after[name]["rank_ic_by_period"], before[name]["rank_ic_by_period"])
             for name in before if name in after
             and before[name]["available"] and after[name]["available"]}
    sig = paired_family_significance(pairs) if pairs else {"results": {}}
    out = {"family": {k: v for k, v in sig.items() if k != "results"}, "models": {}}
    for name in before:
        b, a = before[name], after.get(name, {})
        out["models"][name] = {
            "available": bool(b.get("available") and a.get("available")),
            "errors": [e for e in (b.get("error"), a.get("error")) if e],
            "rank_ic": [b.get("rank_ic"), a.get("rank_ic")],
            "rank_ic_industry_neutral": [b.get("rank_ic_industry_neutral"),
                                         a.get("rank_ic_industry_neutral")],
            "short_side_spread": [b.get("short_side_spread"), a.get("short_side_spread")],
            "n_oof_samples": [b.get("n_oof_samples"), a.get("n_oof_samples")],
            "n_periods": [b.get("n_periods"), a.get("n_periods")],
            "diff_after_minus_before": (sig.get("results") or {}).get(name),
        }
    return out


def stop_reasons(result: dict) -> list[str]:
    """止める条件（ADR-0065）。OOF の rank-IC の符号はここに入れない。"""
    reasons = []
    sim = result["simulation"]
    if sim["rollback_calls"] or sim["commit_blocked"]:
        reasons.append(f"模擬実行中に rollback {sim['rollback_calls']} 回 / commit {sim['commit_blocked']} 回")
    if not sim["fill_visible"]:
        reasons.append("補完後の計測の間に、補完がセッションから見えなくなった")
    if sim["empty_after_rollback"] != sim["empty_before"]:
        reasons.append(f"ROLLBACK 後の空の件数 {sim['empty_after_rollback']} が計測前 {sim['empty_before']} と違う")
    if sim["industries_after"] != sim["industries_before"]:
        reasons.append(f"業種名の種類が {sim['industries_before']} -> {sim['industries_after']}")
    tg = result["today_gap"]
    if not tg["identical"]:
        reasons.append("当日 gap が変わった: " + json.dumps(
            {k: tg[k] for k in ("only_before", "only_after", "none_mismatch", "alpha_changed")},
            ensure_ascii=False))
    z = result["sector_z"]["others"]
    for col, s in z.items():
        if s["to_null"]:
            reasons.append(f"補完しなかった行で {col} が非 NULL -> NULL: {s['to_null']} 行")
    gp = result.get("gap_panel")
    if gp and gp["months_where_rows_shrank"]:
        reasons.append(f"gap パネルで行数が減った月: {gp['months_where_rows_shrank']}")
    oof = result.get("oof")
    if oof:
        for name, m in oof["models"].items():
            if not m["available"]:
                reasons.append(f"{name} の OOF が測れない: {m['errors']}")
                continue
            b, a = m["n_oof_samples"]
            if a is not None and b is not None and a < b:
                reasons.append(f"{name} の n_oof_samples が減った: {b} -> {a}")
    return reasons


# ── 実行 ────────────────────────────────────────────────────────────────────────

def _measure_all(db, skip_oof: bool, skip_panel: bool, timings: dict, phase: str) -> dict:
    out = {}
    for key, fn, skip in (("today_gap", measure_today_gap, False),
                          ("sector_z", measure_sector_z, False),
                          ("gap_panel", measure_gap_panel, skip_panel),
                          ("oof", measure_oof, skip_oof)):
        if skip:
            continue
        t = time.perf_counter()
        print(f"[{phase}] {key} ...", flush=True)
        out[key] = fn(db)
        timings[f"{phase}:{key}"] = round(time.perf_counter() - t, 1)
    return out


def run(skip_oof: bool = False, skip_panel: bool = False) -> dict:
    from database import _is_local

    codelist = fetch_codelist()
    timings: dict = {}
    db = SessionLocal()
    try:
        empty_before = count_empty(db)
        empty_codes = {c for (c,) in db.query(Company.edinet_code)
                       .filter(or_(Company.industry.is_(None), Company.industry == ""))}
        industries_before = distinct_industries(db)
        before = _measure_all(db, skip_oof, skip_panel, timings, "before")
        db.rollback()          # 補完前の読み取りだけのトランザクションを閉じる（書いていない）
        db.expire_all()
        with simulated(db) as state:
            fill = apply_fill(db, codelist)
            empty_filled = count_empty(db)
            industries_after = distinct_industries(db)
            db.expire_all()
            after = _measure_all(db, skip_oof, skip_panel, timings, "after")
            fill_visible = count_empty(db) == empty_filled
    finally:
        db.close()
    check = SessionLocal()
    try:
        empty_after_rollback = count_empty(check)
    finally:
        check.close()

    filled = set(fill["filled_codes"])
    result = {
        "target": "local" if _is_local else "remote",
        "fill": {k: v for k, v in fill.items() if k != "filled_codes"},
        "simulation": {
            "rollback_calls": state["rollback_calls"], "commit_blocked": state["commit_blocked"],
            "empty_before": empty_before, "empty_while_filled": empty_filled,
            "empty_after_rollback": empty_after_rollback, "fill_visible": fill_visible,
            "industries_before": industries_before, "industries_after": industries_after,
        },
        "today_gap": compare_today_gap(before["today_gap"], after["today_gap"]),
        "sector_z": compare_sector_z(before["sector_z"], after["sector_z"], filled, empty_codes - filled),
        "timings_sec": timings,
    }
    if not skip_panel:
        result["gap_panel"] = compare_gap_panel(before["gap_panel"], after["gap_panel"])
    if not skip_oof:
        result["oof"] = compare_oof(before["oof"], after["oof"])
    result["stop_reasons"] = stop_reasons(result)
    return result


def summarize(result: dict) -> None:
    """結果を画面へ出す（cp932 で落ちる記号は使わない）。"""
    fill, sim = result["fill"], result["simulation"]
    print(f"接続先={result['target']}")
    print(f"[補完] 会社 {fill['filled_companies']}社・財務行 {fill['filled_financial_records']}行"
          f" / 書かなかった 上場 {fill['rejected_listed']} / 33業種外 {fill['out_of_scope']}")
    print(f"[模擬] 空の件数 前 {sim['empty_before']} -> 補完中 {sim['empty_while_filled']}"
          f" -> ROLLBACK 後 {sim['empty_after_rollback']} / rollback {sim['rollback_calls']}回"
          f" / 業種名 {sim['industries_before']} -> {sim['industries_after']}")
    tg = result["today_gap"]
    print(f"[当日 gap] 完全一致={tg['identical']} / |差| {tg['abs_diff']}")
    for label, cols in result["sector_z"].items():
        for col, s in cols.items():
            print(f"[業種内Z {label}] {col}: Spearman {s['spearman']} / |差| {s['abs_diff']}"
                  f" / 非NULL->NULL {s['to_null']} / NULL->非NULL {s['from_null']}")
    gp = result.get("gap_panel")
    if gp:
        print(f"[gap パネル] {gp['n_months']}か月 / 減少率の中央値 {gp['median_shrink_before']}"
              f" -> {gp['median_shrink_after']} / gap 付きの行 {gp['rows_with_gap_before']}"
              f" -> {gp['rows_with_gap_after']} / 共通行 Spearman {gp['spearman_common']}"
              f" / |差| {gp['abs_diff_common']}")
    oof = result.get("oof")
    if oof:
        print(f"[OOF] 1検定あたりの alpha {oof['family'].get('alpha')}（{oof['family'].get('n_tests')}組）")
        for name, m in oof["models"].items():
            d = m["diff_after_minus_before"] or {}
            print(f"  {name}: rank-IC {m['rank_ic'][0]} -> {m['rank_ic'][1]}"
                  f" / 業種中立 {m['rank_ic_industry_neutral'][0]} -> {m['rank_ic_industry_neutral'][1]}"
                  f" / short {m['short_side_spread'][0]} -> {m['short_side_spread'][1]}"
                  f" / n {m['n_oof_samples'][0]} -> {m['n_oof_samples'][1]}"
                  f" / 差 {d.get('mean')} p={d.get('p_value')} 有意={d.get('significant')}")
    print(f"[所要] {result.get('timings_sec')}")
    reasons = result["stop_reasons"]
    print("[判定] 止める条件: " + ("なし" if not reasons else ""))
    for r in reasons:
        print("  - " + r)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="業種の空欄補完（#797）の影響を書き込みなしで測る")
    ap.add_argument("--skip-oof", action="store_true", help="OOF（M-1/M-2/M-6）を測らない")
    ap.add_argument("--skip-panel", action="store_true", help="時点再現の gap パネルを測らない")
    ap.add_argument("--json", default=str(DEFAULT_JSON), help="結果の保存先")
    ap.add_argument("--summarize", metavar="JSON", help="保存済みの結果を測り直さずに表示する")
    args = ap.parse_args(argv)

    if args.summarize:
        result = json.loads(Path(args.summarize).read_text(encoding="utf-8"))
    else:
        result = run(skip_oof=args.skip_oof, skip_panel=args.skip_panel)
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"保存: {out}")
    summarize(result)
    return 1 if result["stop_reasons"] else 0


if __name__ == "__main__":
    sys.exit(main())
