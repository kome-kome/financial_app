"""M-6 と本番の設定の M-2 を上位10社で公平に比べる計測 CLI（#1155・ADR-0070 の選択肢 9 の却下理由に答える）。

規則は `docs/prereg/selection-optimism.md`（本文は main へのマージで凍結）。判定は道具が `prereg_decision` に
機械的に出す。読み手は手で判定し直さない。

## 何が公平でないか

本番の M-2 の設定は、採点と同じ期のデータで 150 候補から選んでいる（ADR-0070 の 2 段）。その設定の成績をそのまま
M-6 と比べると、選んだ分だけ M-2 が良く見える（選択の楽観）。そこで各 test 期 t について、**答えが t の 12 か月前
までに出そろった期の成績だけ**を使って本番と同じ規則で設定を選び直し（時間順に選んだ M-2）、選んだ設定の t の
予測をつないで M-6 と比べる。本番の設定の M-2（楽観込み）は上限として並べる。

## 手続き

- **予測**: M-4 を 1 回回して共通域と M-6 の予測を取る（`_run_config(keep_common=True)`）。本番の設定の M-2 は
  `oof_portfolio_replay.production_predictions`。候補は本番の探索と同じ固定の候補集合（`candidate_pool`・引数は
  `run_daytime.JOBS["tune:macro_gbdt"]` の argv から読む＝書き写さない）を**同じパネルで 1 回ずつ**、test 月を
  共通域にそろえて回す（`oof_portfolio_replay.predictions_with_params`・`shared_snapshot_cache` の内側）。全候補の
  期別系列（rank-IC・上位10社超過・M-2 自身の母集団）と予測（共通域の行の並びにそろえた配列）を持つ
- **時間順の選び直し**（`time_ordered_choices`）: 期 t で使える期は `months_between(p, t) >= 12` の期だけ。
  1. 種 42 の rank-IC の平均（使える期だけ）で並べ、上位 `--confirm-top` 件＋1 つ前の期に選んだ設定を決勝候補にする
     （本番の `_confirm_by_seeds` の champion にあたる）
  2. 決勝候補を種 42〜（`--confirm-seeds` 個）で測り直した系列を種平均し（`plugins.tuning._seed_average`）、
     種平均の rank-IC の降順に並べて 2 段の選び方（`plugins.tuning._two_stage_select`）で選ぶ。選べなければ種平均の
     rank-IC 最大（本番の `rank_ic_only` と同じ）
  3. 種の測り直しは、全期の決勝候補の和集合をまとめて種ごとの巡で回し、締切に入らない巡からは止める（本番と同じ
     畳み方）。種平均は回し切った種だけで取る
  - 使える期が 2 期に満たない期は選べないので、**比較から外す**（既定の設定で埋めない・事前登録 §2）
- **比較**: 選べた期だけで、M-6・時間順に選んだ M-2・本番の設定の M-2 の 3 つがそろう共通域の行に限り、どれも
  μ̂ の順に並べて `oof_backtest` で採点する。主審は上位10社超過の期別系列の対比較 2 本（M-6 − 本番の設定・
  M-6 − 時間順）。参考は実現順位の平均・precision@10・lift@10・rank-IC・売り側spread・μ̂ の順の運用再現（K=10・k=2）
- **答え**は計測の時点のパネルの答え（#1154 の結論に従う）

本番の探索の結果（`plugin_tuned_params`）・周回の状態（`app_settings`）・画面は変えない（どの実行も
`tuning_dry_run`・`tuning_objective_only` の内側）。

日中キュー `measure:selection-optimism`（並走に敏感・約 7 時間）で単独に回す。結果は
`scripts/.cache/selection_optimism.json`。

終了コード: 0 = 完了 / 2 = 事前登録の不一致 / 3 = 入力がそろわない（150 件を締切までに回し切れない・本番の設定の
M-2 が出せない・選べた期が無い）。

実行:
    python -m scripts.measure_selection_optimism --allow-full-pull --refresh-cache \\
        --prereg docs/prereg/selection-optimism.md --prereg-sha <凍結値>
    python -m scripts.measure_selection_optimism --smoke --allow-full-pull --prereg ... --prereg-sha ...  # 動作確認
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_stats import paired_ic_significance  # noqa: E402
from plugins.macro_snapshots import LABEL_HORIZON_MONTHS, OPERATIONAL_TOP_K, oof_backtest  # noqa: E402
from plugins.tuning import _period_series, _seed_average, _two_stage_select  # noqa: E402
from scripts import prereg  # noqa: E402

PREREG = "docs/prereg/selection-optimism.md"
_OUT = ROOT / "scripts" / ".cache" / "selection_optimism.json"
EXIT_INPUT = 3

MODEL = "macro_gbdt"
SEARCH_JOB = "tune:macro_gbdt"                     # 本番の探索の引数の唯一の源（run_daytime.JOBS の argv）
M6 = "macro_enet"
PRODUCTION = "production"                           # 本番の設定の M-2（楽観込み）
TIME_ORDERED = "time_ordered"                       # 時間順に選んだ M-2
SHORT = {M6: "M-6", PRODUCTION: "M-2*", TIME_ORDERED: "M-2(時間順)"}
PAIRS = ((M6, PRODUCTION), (M6, TIME_ORDERED))      # 主審 2 本（a − b）
ALPHA = 0.05 / len(PAIRS)                           # Bonferroni（1 検定あたり 0.025）
TOP_K = OPERATIONAL_TOP_K                           # 上位10社（ADR-0070）
GAP_MONTHS = LABEL_HORIZON_MONTHS                   # 52 週の答えが出そろうまでの月数（12）
MIN_SELECT_PERIODS = 2                              # 2 段の選び方が要る共通期の下限と同じ
RESERVE_MIN = 15.0                                  # 締切の手前に残す時間（比較と書き出し）
REPLAY_SELL_K = 2


# ── 本番の探索の引数 ────────────────────────────────────────────────────────────

def search_args(argv: Sequence[str]) -> dict:
    """`run_daytime.JOBS["tune:macro_gbdt"].argv` から候補集合と確かめ直す段の引数を読む（書き写さない）。"""
    argv = list(argv)

    def _val(flag: str) -> str:
        if flag not in argv:
            raise ValueError(f"{SEARCH_JOB} の argv に {flag} が無い")
        return argv[argv.index(flag) + 1]

    return {"strategy": _val("--strategy"), "n_iter": int(_val("--n-iter")), "seed": int(_val("--seed")),
            "confirm_top": int(_val("--confirm-top")), "confirm_seeds": int(_val("--confirm-seeds"))}


# ── 時間順の選び直し（純関数）────────────────────────────────────────────────────

def months_between(ym0: str, ym1: str) -> int:
    y0, m0 = map(int, ym0.split("-"))
    y1, m1 = map(int, ym1.split("-"))
    return (y1 - y0) * 12 + (m1 - m0)


def available_periods(t: str, periods: Sequence[str], gap: int = GAP_MONTHS) -> list[str]:
    """期 t で成績を使ってよい期（答えが t の gap か月前までに出そろった期）。"""
    return [p for p in periods if months_between(p, t) >= gap]


def _restrict(series: dict | None, avail: set) -> dict | None:
    if not series:
        return None
    d = {p: v for p, v in series.items() if p in avail}
    return d or None


def _ic_mean(series: dict | None, avail: set) -> float | None:
    d = _restrict(series, avail)
    return statistics.mean(d.values()) if d else None


def finalists_at(avail: set, base: list[dict], top: int, prev: int | None) -> list[int]:
    """決勝候補の添字。種 42 の rank-IC の平均（使える期だけ）の上位 top 件（同値は添字の小さい方）＋1 つ前の選択。

    base[i] = `{"ic": {期: 値} | None, "top": ... }`（種 42・失敗した候補は ic が None）。
    """
    scored = [(s, i) for i, c in enumerate(base) if (s := _ic_mean(c.get("ic"), avail)) is not None]
    picked = [i for _s, i in sorted(scored, key=lambda si: (-si[0], si[1]))[:top]]
    if prev is not None and prev not in picked and _ic_mean(base[prev].get("ic"), avail) is not None:
        picked.append(prev)
    return picked


def select_at(avail: set, finalists: list[int], per_seed: dict) -> dict:
    """決勝候補から本番と同じ規則で 1 件選ぶ（`_confirm_by_seeds` の 2 段の選び方を使える期に限って写したもの）。

    per_seed[i] = `[{"ic": …, "top": …}, …]`（回し切った種の順・先頭が種 42）。種のどれかが失敗（ic が None）の
    候補は外す（本番の eligible と同じ）。
    """
    eligible: list[tuple[int, float, dict | None, dict | None]] = []
    for i in finalists:
        runs = per_seed.get(i) or []
        means = [_ic_mean(r.get("ic"), avail) for r in runs]
        if not runs or any(m is None for m in means):
            continue
        ics = [_restrict(r.get("ic"), avail) for r in runs]
        tops = [_restrict(r.get("top"), avail) for r in runs]
        ic = _seed_average(ics) if all(ics) else None
        tp = _seed_average(tops) if all(tops) else None
        eligible.append((i, statistics.mean(means), ic, tp))
    if not eligible:
        return {"chosen": None, "selection": "none", "reason": "決勝候補に回し切れた候補が無い"}
    order = sorted(eligible, key=lambda e: -e[1])                  # 安定ソート＝同値は決勝候補の並び
    out = {"finalists": finalists, "seed_mean": {e[0]: round(e[1], 6) for e in order}}
    if len(order) >= 2:
        two = _two_stage_select([{"ic": e[2], "top": e[3]} for e in order])
        if two["ok"]:
            return {**out, "chosen": order[two["chosen"]][0], "leader": order[two["leader"]][0],
                    "selection": "two_stage", "n_common": two["n_common"],
                    "tie_set": [order[k][0] for k in two["tie_set"]]}
        return {**out, "chosen": order[0][0], "selection": "rank_ic_only", "reason": two["reason"]}
    return {**out, "chosen": order[0][0], "selection": "single"}


def finalist_union(periods: Sequence[str], base: list[dict], top: int) -> list[int]:
    """全期の決勝候補（種 42 の上位 top 件）の和集合。1 つ前の選択は前の期の決勝候補なので、必ずここに入る。"""
    union: set[int] = set()
    for t in periods:
        avail = set(available_periods(t, periods))
        if len(avail) >= MIN_SELECT_PERIODS:
            union.update(finalists_at(avail, base, top, None))
    return sorted(union)


def time_ordered_choices(periods: Sequence[str], base: list[dict], per_seed: dict, top: int) -> dict:
    """各期 t の選択 `{t: select_at の返り値＋n_avail}`。使える期が MIN_SELECT_PERIODS 未満の期は入れない。

    t の選択は、各候補の系列のうち `available_periods(t)` の期の値しか読まない（期 t の 12 か月前より後の成績を
    差し替えても t の選択は変わらない＝検証 1）。
    """
    choices: dict[str, dict] = {}
    prev: int | None = None
    for t in sorted(periods):
        avail = set(available_periods(t, periods))
        if len(avail) < MIN_SELECT_PERIODS:
            continue
        fin = finalists_at(avail, base, top, prev)
        sel = select_at(avail, fin, per_seed)
        if sel.get("chosen") is None:
            continue
        choices[t] = {**sel, "n_avail": len(avail)}
        prev = sel["chosen"]
    return choices


def splice(choices: dict, cand_preds: np.ndarray, rows: list[tuple]) -> dict:
    """選んだ候補の、その期の予測をつなぐ `{(ym, 社): 予測}`。cand_preds[i, j] は候補 i の行 j の予測（無ければ NaN）。"""
    out: dict[tuple, float] = {}
    for j, (ym, ec) in enumerate(rows):
        ch = choices.get(ym)
        if ch is None:
            continue
        v = float(cand_preds[ch["chosen"], j])
        if not math.isnan(v):
            out[(ym, ec)] = v
    return out


# ── 比較と判定（純関数）──────────────────────────────────────────────────────────

def compare(preds: dict, y: dict, order: dict, periods: Sequence[str]) -> tuple[dict, dict]:
    """periods の各期で、全モデルの予測と y がそろう行だけを `oof_backtest` で採点する。`({名: oof}, 期ごとの行数)`。"""
    domain: dict[str, list] = {}
    for ym in periods:
        domain[ym] = [ec for ec in order.get(ym, [])
                      if (ym, ec) in y and all((ym, ec) in p for p in preds.values())]
    scores = {name: oof_backtest({ym: [(p[(ym, ec)], y[(ym, ec)]) for ec in ecs]
                                  for ym, ecs in domain.items() if ecs})
              for name, p in preds.items()}
    return scores, {ym: len(ecs) for ym, ecs in domain.items()}


def _verdict(sig: dict | None) -> str:
    if not sig or not sig.get("significant"):
        return "0"
    return "+" if sig["mean"] > 0 else "-"


def decide(scores: dict) -> dict:
    """事前登録 §4 の判定。主審 2 本の上位10社超過の対比較（両側・Bonferroni α 0.025）。"""
    tests: dict[str, dict] = {}
    for a, b in PAIRS:
        sa = (scores[a].get("top_k_excess_return_by_period") or {}).get(str(TOP_K)) or {}
        sb = (scores[b].get("top_k_excess_return_by_period") or {}).get(str(TOP_K)) or {}
        sig = paired_ic_significance(sa, sb, alpha=ALPHA)
        tests[f"{a}|{b}"] = {"sig": sig, "verdict": _verdict(sig)}
    v_prod = tests[f"{M6}|{PRODUCTION}"]["verdict"]
    v_time = tests[f"{M6}|{TIME_ORDERED}"]["verdict"]
    if v_time == "-" and v_prod == "+":
        decision = "conflict"
    elif v_time == "-":
        decision = "consider_m2"           # 買いの主を M-2 に戻すかを利用者が判断する
    elif v_prod == "+":
        decision = "m6_confirmed"          # M-6 の優位は確か
    else:
        decision = "no_difference"         # 両方の画面を使ってよい
    return {"alpha": ALPHA, "top_k": TOP_K, "tests": tests, "decision": decision}


def metrics_row(o: dict) -> dict:
    k = (o.get("top_k") or {}).get(str(TOP_K)) or {}
    return {"top_k_excess_return": k.get("excess_return"), "mean_pct_rank": k.get("mean_pct_rank"),
            "precision": k.get("precision"), "lift": k.get("lift"),
            "rank_ic": (o.get("rank_ic") or {}).get("mean"), "short_side_spread": o.get("short_side_spread"),
            "n_periods": o.get("n_periods")}


def candidate_table(combos: list[dict], base: list[dict], infos: list[dict]) -> list[dict]:
    """全候補の設定・期別系列・所要（上位 20 件に限らない・検証 2）。"""
    return [{"index": i, "params": combos[i], "ic": base[i].get("ic"), "top": base[i].get("top"),
             "elapsed_sec": infos[i].get("elapsed_sec"), "error": infos[i].get("error")}
            for i in range(len(combos))]


# ── 実行 ────────────────────────────────────────────────────────────────────────

def _run_candidate(panel, db, test_yms, params, seed: int | None) -> tuple[dict | None, dict, dict]:
    """候補 1 件を回す。`(予測, {"ic","top"}, 付記)`。失敗は ic=None で返す（本番の探索と同じく評価済みに数える）。"""
    from plugins.tuning import seed_override
    from scripts.oof_portfolio_replay import predictions_with_params

    t0 = time.monotonic()
    if seed is None:
        preds, oof, info = predictions_with_params(panel, db, test_yms, params)
    else:
        with seed_override(seed):
            preds, oof, info = predictions_with_params(panel, db, test_yms, params)
    elapsed = time.monotonic() - t0
    if preds is None or oof is None:
        return None, {"ic": None, "top": None}, {**info, "elapsed_sec": round(elapsed, 1),
                                                 "error": info.get("error", "oof が無い")}
    ic, top = _period_series(oof)
    return preds, {"ic": ic, "top": top}, {**info, "elapsed_sec": round(elapsed, 1)}


def _remaining_min(deadline: datetime | None) -> float:
    if deadline is None:
        return math.inf
    return (deadline - datetime.now(timezone.utc)).total_seconds() / 60.0


def run(panel, db, deadline: datetime | None, n_candidates: int | None) -> dict:
    """計測の本体。入力がそろわないときは InputMissing。"""
    from plugins import get_plugin
    from plugins.macro_snapshots import shared_snapshot_cache
    from plugins.tuning import candidate_pool
    from scripts.ensemble_base_bakeoff import _run_config
    from scripts.oof_portfolio_replay import production_predictions, run_replays
    from scripts.run_daytime import JOBS
    from scripts.sell_mu_source_bakeoff import BASES, common_to_dicts
    from tuning_cycle import fingerprint

    sargs = search_args(JOBS[SEARCH_JOB].argv)
    base_params, dims = get_plugin(MODEL).tuning_search_space()
    combos = candidate_pool(dims, sargs["strategy"], sargs["n_iter"], sargs["seed"])
    fp_full = fingerprint(combos)
    if n_candidates is not None:
        combos = combos[:n_candidates]

    run_m4 = _run_config("M-4(3基底・rank_ic_grid)", BASES, panel, db, weight_method="rank_ic_grid", keep_common=True)
    common = run_m4["common"]
    preds_m4, y = common_to_dicts(common, BASES)
    order = {ym: [ec for ec, _yh, _y in rs] for ym, rs in common.items()}
    periods = sorted(order)
    rows = [(ym, ec) for ym in periods for ec in order[ym]]
    row_idx = {k: j for j, k in enumerate(rows)}

    prod, prod_info = production_predictions(panel, db, periods)
    if prod is None:
        raise InputMissing(f"本番の設定の M-2 が出せない: {prod_info.get('reason')} {prod_info.get('error', '')}")

    cand_preds = np.full((len(combos), len(rows)), np.nan)
    base: list[dict] = []
    infos: list[dict] = []
    worst = 0.0
    with shared_snapshot_cache():
        for i, combo in enumerate(combos):
            if i and _remaining_min(deadline) - worst / 60.0 < RESERVE_MIN:
                raise InputMissing(f"候補 {i}/{len(combos)} 件で締切に入らない（150 件を回し切れない）")
            preds, series, info = _run_candidate(panel, db, periods, {**base_params, **combo}, None)
            worst = max(worst, info["elapsed_sec"])
            if preds is not None:
                for k, v in preds.items():
                    j = row_idx.get(k)
                    if j is not None:
                        cand_preds[i, j] = v
            base.append(series)
            infos.append(info)
            print(f"[sel_opt] 候補 {i + 1}/{len(combos)} {info['elapsed_sec']}s "
                  f"ic={'-' if not series['ic'] else round(statistics.mean(series['ic'].values()), 4)}", flush=True)

        union = finalist_union(periods, base, sargs["confirm_top"])
        per_seed: dict[int, list] = {i: [base[i]] for i in union}
        seeds_done = [0]
        truncated = False
        est = sum(infos[i]["elapsed_sec"] for i in union) / 60.0
        for k in range(1, sargs["confirm_seeds"]):
            if _remaining_min(deadline) - est < RESERVE_MIN:
                truncated = True
                break
            for i in union:
                _p, series, _info = _run_candidate(panel, db, periods, {**base_params, **combos[i]}, 42 + k)
                per_seed[i].append(series)
            seeds_done.append(k)
            print(f"[sel_opt] 種 {42 + k} の巡（{len(union)} 件）を終えた", flush=True)

    choices = time_ordered_choices(periods, base, per_seed, sargs["confirm_top"])
    if not choices:
        raise InputMissing("選べた期が無い（使える期が 2 期以上の test 期が無い）")
    selected = sorted(choices)
    time_ordered = splice(choices, cand_preds, rows)
    prod_in = {k: v for k, v in prod.items() if k in y}
    model_preds = {M6: preds_m4[M6], PRODUCTION: prod_in, TIME_ORDERED: time_ordered}
    scores, n_rows = compare(model_preds, y, order, selected)

    common_sel = {ym: common[ym] for ym in selected}
    replays = run_replays(common_sel, BASES, panel[0], db, [M6], M6, TOP_K, REPLAY_SELL_K, 10.0,
                          production=None, extra_m2={PRODUCTION: prod_in, TIME_ORDERED: time_ordered})
    return {
        "search": {**sargs, "fingerprint": fp_full, "n_candidates": len(combos), "base_params": base_params},
        "periods": periods, "selected_periods": selected, "n_rows_by_period": n_rows,
        "seeds": {"union": union, "seeds_completed": [42 + k for k in seeds_done], "truncated": truncated},
        "choices": {t: {**c, "params": combos[c["chosen"]]} for t, c in choices.items()},
        "candidates": candidate_table(combos, base, infos),
        "production": prod_info,
        "scores": {name: metrics_row(o) for name, o in scores.items()},
        "series": {name: {"top_k_excess": (o.get("top_k_excess_return_by_period") or {}).get(str(TOP_K)),
                          "pct_rank": (o.get("top_k_pct_rank_by_period") or {}).get(str(TOP_K)),
                          "rank_ic": o.get("rank_ic_by_period")} for name, o in scores.items()},
        "replay": {name: r.get("summary") for name, r in replays.items()},
        "prereg_decision": decide(scores),
    }


class InputMissing(RuntimeError):
    """入力がそろわない（終了コード 3）。"""


def _print(rep: dict) -> None:
    print(f"\n=== M-6 と本番の設定の M-2 の公平な比較（#1155）選べた期 {len(rep['selected_periods'])}"
          f"/{len(rep['periods'])}・種 {rep['seeds']['seeds_completed']} ===", flush=True)
    print(f"{'model':<14}{'top10':>9}{'pctrank':>9}{'prec':>8}{'lift':>8}{'rank-IC':>9}{'spread':>9}", flush=True)

    def _f(v, d=4):
        return f"{v:+.{d}f}" if isinstance(v, (int, float)) else "-"

    for name, r in rep["scores"].items():
        print(f"{SHORT[name]:<14}{_f(r['top_k_excess_return']):>9}{_f(r['mean_pct_rank'], 3):>9}"
              f"{_f(r['precision'], 3):>8}{_f(r['lift'], 3):>8}{_f(r['rank_ic']):>9}"
              f"{_f(r['short_side_spread']):>9}", flush=True)
    for key, t in rep["prereg_decision"]["tests"].items():
        a, b = key.split("|")
        s = t["sig"]
        txt = "n/a" if not s else f"diff={s['mean']:+.4f} p={s.get('p_value', float('nan')):.3f} n={s['n_common']}"
        print(f"  {SHORT[a]} - {SHORT[b]}: {txt} -> {t['verdict']}", flush=True)
    print(f"判定（事前登録）: {rep['prereg_decision']['decision']}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.measure_selection_optimism",
                                 description="M-6 と本番の設定の M-2 を上位10社で公平に比べる（#1155）")
    prereg.add_arguments(ap)
    ap.add_argument("--smoke", action="store_true", help="企業と候補を絞った動作確認（判定には使わない）")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--smoke-candidates", type=int, default=3)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--budget-min", type=float, default=None,
                    help="締切（分）。無ければ親バッチの FINAPP_STEP_DEADLINE_UTC、それも無ければ無期限")
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)          # 不一致は重い計算の前に終了コード 2
    if Path(args.prereg).as_posix() != PREREG:
        print(f"[sel_opt] NG: 事前登録は {PREREG}（受け取った値: {args.prereg}）", file=sys.stderr)
        return prereg.EXIT_MISMATCH

    from database import SessionLocal
    from hyperparameter_search import resolve_deadline
    from scripts._cache import set_refresh
    from scripts.ensemble_base_bakeoff import _load_panel

    deadline = resolve_deadline(args.budget_min)
    set_refresh(args.refresh_cache)
    panel = _load_panel(args)
    db = SessionLocal()
    try:
        try:
            rep = run(panel, db, deadline, args.smoke_candidates if args.smoke else None)
        except InputMissing as e:
            print(f"[sel_opt] NG: {e}", file=sys.stderr)
            return EXIT_INPUT
    finally:
        db.close()
    _print(rep)

    from scripts.sell_mu_source_bakeoff import M4_BASES_CAVEAT
    payload = {
        **stamp,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "smoke": args.smoke,
        "deadline": None if deadline is None else deadline.isoformat(),
        "caveats": [M4_BASES_CAVEAT,
                    "M-2*（本番の設定）は採点と同じ期のデータで選んだ設定＝選択の楽観を含む上限（ADR-0070）",
                    "時間順に選んだ M-2 は、各期 t で t の 12 か月前までに答えが出そろった期の成績だけで本番と同じ規則で選んだ"],
        "rule": {"pairs": [list(p) for p in PAIRS], "alpha": ALPHA, "top_k": TOP_K, "gap_months": GAP_MONTHS,
                 "min_select_periods": MIN_SELECT_PERIODS, "reserve_min": RESERVE_MIN},
        **rep,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
