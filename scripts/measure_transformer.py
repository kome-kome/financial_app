"""事前登録 `docs/prereg/m7-transformer.md` の計測（#1316・#1317）。

M-7（時系列 Transformer）を M-6・M-2（スキーマ既定）と比べ、値動きの系列の上乗せを測る。

1. 主審: `model_comparison.run_comparison(only_models=[M-2, M-6, M-7])` の `significance_matrix` をそのまま読む
   （3 組の Bonferroni・再計算しない＝ADR-0041）
2. 対照: M-7 を `use_sequence=False` で採点し、M-7 との rank-IC の差を `paired_ic_significance`（α 0.05）で検定する
3. 参考: 上位10社超過の差（検定しない）・各モデルの要約・種 43・44 の M-7 の rank-IC（判定には使わない）
4. 判定: `prereg_decision`（事前登録 §4 を機械的に）

比較の外の採点は `model_comparison.score_once`（比較の行と同じ手続き）を使う。保存はしない（dry_run）。

実行: python -m scripts.measure_transformer --prereg docs/prereg/m7-transformer.md --prereg-sha <凍結値>
終了コード: 0＝計測した／2＝凍結値の不一致／3＝モデルの失敗・期数や行数の食い違い（判定しない）
出力: scripts/.cache/m7_transformer.json（ASCII 以外も UTF-8 で書く）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import prereg  # noqa: E402

PREREG = "docs/prereg/m7-transformer.md"
EXIT_INPUT = 3
MODELS = ("macro_gbdt", "macro_enet", "macro_transformer")
M7, M6, M2 = "M-7", "M-6", "M-2"
ABLATION_PARAMS = {"use_sequence": False}
REFERENCE_SEEDS = (43, 44)
ABLATION_ALPHA = 0.05
TOP_K = "10"
_OUT = ROOT / "scripts" / ".cache" / "m7_transformer.json"

DECISION_PROMOTE = "consider_promotion"
DECISION_RETIRE = "retire"
DECISION_KEEP = "keep_as_comparison"


class InputMissing(RuntimeError):
    """判定に要る入力がそろわない（モデルの失敗・期数や行数の食い違い）。"""


def _pair(sig: dict | None, a: str, b: str) -> dict:
    """significance_matrix の組（a|b の順で格納・無ければ有意でない扱い）。"""
    pairs = (sig or {}).get("pairs") or {}
    got = pairs.get(f"{a}|{b}")
    if got is not None:
        return got
    got = pairs.get(f"{b}|{a}")
    if got is None:
        return {"significant": False, "better": None, "mean_diff": None, "n_common": 0, "p_value": None}
    flipped = dict(got)
    if flipped.get("mean_diff") is not None:
        flipped["mean_diff"] = -flipped["mean_diff"]
        lo, hi = flipped.get("ci_lo"), flipped.get("ci_hi")
        flipped["ci_lo"], flipped["ci_hi"] = (-hi if hi is not None else None), (-lo if lo is not None else None)
    return flipped


def prereg_decision(sig: dict | None) -> dict:
    """事前登録 §4 を機械的に。差は M-7 から見た向き（勝つ＝M-7 が有意に優位）。"""
    vs_m6 = _pair(sig, M6, M7)
    vs_m2 = _pair(sig, M2, M7)
    beats_m6 = bool(vs_m6.get("significant")) and vs_m6.get("better") == M7
    loses_m6 = bool(vs_m6.get("significant")) and vs_m6.get("better") == M6
    loses_m2 = bool(vs_m2.get("significant")) and vs_m2.get("better") == M2
    if beats_m6 and not loses_m2:
        decision = DECISION_PROMOTE
    elif loses_m6 and loses_m2:
        decision = DECISION_RETIRE
    else:
        decision = DECISION_KEEP
    return {"decision": decision, "m7_beats_m6": beats_m6, "m7_loses_to_m6": loses_m6,
            "m7_loses_to_m2": loses_m2, "vs_m6": vs_m6, "vs_m2": vs_m2}


def _oof(entry: dict) -> dict:
    return entry.get("oof_backtest") or {}


def check_alignment(rows: dict[str, dict]) -> dict:
    """全行が採点でき、OOF の期数と行数がそろっていることを確かめる（そろわなければ InputMissing）。"""
    failed = {k: f"{e.get('reason')}: {e.get('error')}" for k, e in rows.items() if not e.get("available")}
    if failed:
        raise InputMissing(f"採点できなかった行: {failed}")
    shape = {k: (_oof(e).get("n_periods"), _oof(e).get("n_oof_samples")) for k, e in rows.items()}
    if len(set(shape.values())) != 1 or None in next(iter(shape.values())):
        raise InputMissing(f"OOF の期数・行数がそろわない（期数, 行数）: {shape}")
    n_periods, n_rows = next(iter(shape.values()))
    return {"n_periods": n_periods, "n_oof_samples": n_rows}


def _mean_diff_on_common(a: dict | None, b: dict | None) -> dict:
    """期別の系列の差の平均（共通の期だけ・検定しない参考）。"""
    a, b = a or {}, b or {}
    common = sorted(set(a) & set(b))
    if not common:
        return {"mean_diff": None, "n_common": 0}
    diffs = [a[ym] - b[ym] for ym in common]
    return {"mean_diff": round(sum(diffs) / len(diffs), 6), "n_common": len(common)}


def _summary(entry: dict) -> dict:
    o = _oof(entry)
    tk = (o.get("top_k") or {}).get(TOP_K) or {}
    return {
        "rank_ic": (o.get("rank_ic") or {}).get("mean"),
        "rank_ic_industry_neutral": (o.get("rank_ic_industry_neutral") or {}).get("mean"),
        "top10_excess": tk.get("excess_return"),
        "short_side_spread": o.get("short_side_spread"),
        "effective_turnover": o.get("effective_turnover"),
        "breakeven_cost_bps": o.get("breakeven_cost_bps"),
        "n_periods": o.get("n_periods"),
        "n_oof_samples": o.get("n_oof_samples"),
    }


def build_report(comparison: dict, ablation: dict, seed_rows: dict[int, dict]) -> dict:
    """計測の結果から判定と参考の値をまとめる（純関数・テストはここを表で縛る）。"""
    from model_stats import paired_ic_significance

    by_short = {m["short"]: m for m in comparison.get("models", [])}
    missing = [s for s in (M2, M6, M7) if s not in by_short]
    if missing:
        raise InputMissing(f"比較に行が無い: {missing}")
    rows = {M2: by_short[M2], M6: by_short[M6], M7: by_short[M7], "M-7（系列なし）": ablation}
    shape = check_alignment(rows)
    sig = comparison.get("significance_matrix")
    decision = prereg_decision(sig)

    m7_ic = _oof(by_short[M7]).get("rank_ic_by_period") or {}
    abl_ic = _oof(ablation).get("rank_ic_by_period") or {}
    abl_sig = paired_ic_significance(m7_ic, abl_ic, alpha=ABLATION_ALPHA)
    ablation_out = {"m7_minus_flat": abl_sig,
                    "sequence_adds": bool(abl_sig and abl_sig.get("significant") and abl_sig.get("mean", 0) > 0)}
    if decision["decision"] == DECISION_PROMOTE and not ablation_out["sequence_adds"]:
        decision["note"] = "利得は値動きの系列から来ていない（対照で上乗せが有意でない）"

    def _topk(e):
        return (_oof(e).get("top_k_excess_return_by_period") or {}).get(TOP_K)

    reference = {
        "top10_excess_m7_minus_m6": _mean_diff_on_common(_topk(by_short[M7]), _topk(by_short[M6])),
        "top10_excess_m7_minus_m2": _mean_diff_on_common(_topk(by_short[M7]), _topk(by_short[M2])),
        "summary": {k: _summary(e) for k, e in rows.items()},
        "seeds": {str(s): (_oof(e).get("rank_ic") or {}).get("mean") if e.get("available")
                  else f"{e.get('reason')}: {e.get('error')}" for s, e in seed_rows.items()},
    }
    return {"alignment": shape, "significance_matrix": sig, "prereg_decision": decision,
            "ablation": ablation_out, "reference": reference}


async def _measure(db) -> tuple[dict, dict, dict, dict]:
    from model_comparison import run_comparison, score_once
    from plugins import get_plugin
    from plugins.macro_snapshots import shared_snapshot_cache
    from plugins.tuning import seed_override

    timings: dict = {}
    t0 = time.time()
    comparison = await run_comparison(db, only_models=list(MODELS))
    timings["comparison_min"] = round((time.time() - t0) / 60, 1)
    m7 = get_plugin("macro_transformer")
    seed_rows: dict[int, dict] = {}
    with shared_snapshot_cache():          # 系列なし・別の種はパネルを 1 回だけ作って共有する
        t1 = time.time()
        ablation = await score_once(m7, dict(ABLATION_PARAMS), db)
        timings["ablation_min"] = round((time.time() - t1) / 60, 1)
        for s in REFERENCE_SEEDS:
            t2 = time.time()
            with seed_override(s):
                seed_rows[s] = await score_once(m7, {}, db)
            timings[f"seed_{s}_min"] = round((time.time() - t2) / 60, 1)
    timings["total_min"] = round((time.time() - t0) / 60, 1)
    return comparison, ablation, seed_rows, timings


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.measure_transformer",
                                 description="M-7 を M-6・M-2 と比べ、値動きの系列の上乗せを測る（#1316）")
    prereg.add_arguments(ap)
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def _print(rep: dict, timings: dict) -> None:
    d = rep["prereg_decision"]
    for key in ("vs_m6", "vs_m2"):
        s = d[key]
        print(f"  {key}: diff(相手-M7)={s.get('mean_diff')} p={s.get('p_value')} "
              f"n={s.get('n_common')} better={s.get('better')}", flush=True)
    a = rep["ablation"]["m7_minus_flat"] or {}
    print(f"  ablation: diff(M7-flat)={a.get('mean')} p={a.get('p_value')} n={a.get('n_common')}", flush=True)
    print(f"  所要: {timings}", flush=True)
    print(f"判定（事前登録）: {d['decision']}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)          # 不一致は重い計算の前に終了コード 2
    if Path(args.prereg).as_posix() != PREREG:
        print(f"[m7] NG: 事前登録は {PREREG}（受け取った値: {args.prereg}）", file=sys.stderr)
        return prereg.EXIT_MISMATCH

    from database import SessionLocal
    db = SessionLocal()
    try:
        comparison, ablation, seed_rows, timings = asyncio.run(_measure(db))
        try:
            rep = build_report(comparison, ablation, seed_rows)
        except InputMissing as e:
            print(f"[m7] NG: {e}（判定しない）", file=sys.stderr)
            return EXIT_INPUT
    finally:
        db.close()
    _print(rep, timings)
    payload = {
        **stamp,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "rule": {"models": list(MODELS), "ablation_params": ABLATION_PARAMS, "ablation_alpha": ABLATION_ALPHA,
                 "reference_seeds": list(REFERENCE_SEEDS), "top_k": TOP_K},
        "timings": timings,
        **rep,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
