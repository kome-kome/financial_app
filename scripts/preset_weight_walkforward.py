"""Issue #625: 静的プリセットの重みを walk-forward で推定し直すローカル専用CLI（#546 の前提1）。

`scripts/preset_ic_gate.py`（ADR-0041）は**与えられた重みを測る**評価器で、推定はしない。
そのため #546（重みの再チューニング）は実行すべきコマンドを持たないまま止まっていた。
本スクリプトがその推定側で、**評価は preset_ic_gate の関数へ委譲する**（書き直さない）。

設計の要点（ADR-0059）
----------------------
1. **尺度は rank-IC を直接高める**。学習期間の各月について Pearson(スコア, 順位化した
   52週先リターン) を求め、その平均を最大化する。スコアは標準化済みの列 Z_t の線形結合なので
   この値は `w'c_t / sqrt(w'S_t w)`（S_t = 列の共分散、c_t = 列と順位の共分散 / 順位の sd）の
   閉形式で出せ、月ごとに1回前計算すれば最適化は数値だけで回る。評価は本物の Spearman。
   学習を二乗誤差にすると評価（順位）とずれる——#615 で M-1 に見つかった問題と同型。
2. **性格は静的重みの大小順で残す**。使う指標は静的プリセットのキーだけ（他は 0）、重みは
   非負、静的に重い指標は推定後も軽い指標以上（同じ重みの指標同士は自由）。重みの合計は静的と
   同じに固定する（順位は定数倍で変わらないので、見比べるための正規化）。無制約だと4つが同じ
   重みへ収束して区別が消える（#625 本文）。
3. **標準化は消費側の関数をそのまま通す**。`preset_ic_gate._panel_rows` → `build_view_stats`
   → `standardize_metric`。列の統計は他の列の重みに依存しないので、最適化するスコアは
   `preset_ic_gate.score_period` が合成するスコアと一致する（テストで縛る）。
4. **embargo は `LABEL_HORIZON_MONTHS`（=12）に固定**。目的変数は 52週先リターンなので、
   テスト月との差が 12か月以内の月のラベルはテスト月の時点でまだ確定していない。学習月の判定は
   暦で行う（月の抜けが無ければ `walk_forward_cv_monthly` の `all_yms[:i-12]` と同じ境界。
   抜けがあっても境界がずれない）。CLI では変えさせない＝下げると先読みになる。
5. **パネルは時点再現の gap あり**（`build_period_panel(with_gap_ratio=True)`・ADR-0057）。
   4プリセット中3つが `gap_ratio` を使う。載せる基準を満たさなければ推定しない。
6. **昇格の根拠は OOF の対比較だけ**。月ごとの推定重みの rank-IC と、同じ月の静的重みの
   rank-IC を `paired_ic_significance` で対にし、Bonferroni（プリセット数）で判定する。
   `--weights-out` の最終重みを `preset_ic_gate --weights-json` で測るのは in-sample
   （学習に使った期間での成績）なので、並べて眺める以上の意味を持たせない。
7. `PRESETS` は変えない。変えるかは #546 がこの結果を見て決める。DB へは書かない。
   書き出しは全計算の完走後（日中枠 `run_daytime.JOBS["wf:preset-weights"]`）。

実行
----
    python -m scripts.preset_weight_walkforward --json --weights-out
    python -m scripts.preset_weight_walkforward --preset 成長重視 --window-months 36
    python -m scripts.preset_ic_gate --with-gap-ratio --weights-json scripts/.cache/preset_weight_walkforward_weights.json

パネルは毎晩伸びるので、過去の実測と比べるときは `--until <ym>` で期を揃える。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal   # noqa: E402
from model_stats import paired_ic_significance   # noqa: E402
from plugins.macro_snapshots import LABEL_HORIZON_MONTHS, _avg_ranks   # noqa: E402
from plugins.recommend import PRESETS, standardize_metric   # noqa: E402
from plugins.utils import PREPROCESS_VERSION   # noqa: E402
from scripts.preset_ic_gate import (   # noqa: E402
    DEFAULT_MIN_COMPANIES, DEFAULT_N_BOOT,
    _panel_rows, build_view_stats, fmt_sig, gap_coverage_summary, ic_series, load_panel,
    panel_info, print_gap_summary, print_panel_info,
)
from scripts._textwidth import pad as _pad   # noqa: E402

_OUT_DIR = Path(__file__).resolve().parent / ".cache"
DEFAULT_JSON = _OUT_DIR / "preset_weight_walkforward.json"
DEFAULT_WEIGHTS_OUT = _OUT_DIR / "preset_weight_walkforward_weights.json"

# 目的変数（52週先リターン）の確定待ち。**CLI の引数にしない**（下げると先読み）。
EMBARGO_MONTHS = LABEL_HORIZON_MONTHS
# 学習期間の最小月数（`walk_forward_cv_monthly` の既定と同じ）。
DEFAULT_MIN_TRAIN_MONTHS = 18
# `--weights-out` のラベルの接尾辞（`preset_ic_gate` の表で静的プリセットと並ぶ）。
WF_LABEL_SUFFIX = "(wf)"
# 最適化後に制約（合計・大小順・非負）を確かめる許容。外れたら未収束として扱う。
CONSTRAINT_TOL = 1e-6


# ── 制約（プリセットの性格）─────────────────────────────────────────────────

def order_pairs(static: dict) -> list[tuple[str, str]]:
    """静的重みの隣り合う水準について (重い指標, 軽い指標) の組を返す。

    水準は重みの値そのもの。同じ重みの指標同士には組を作らない（入れ替わってよい）。
    隣り合う水準だけで足りる（推移律で全体の大小順が保たれる）。
    """
    levels = sorted(set(static.values()), reverse=True)
    by_level = {lv: [m for m, w in static.items() if w == lv] for lv in levels}
    return [(hi, lo)
            for upper, lower in zip(levels, levels[1:])
            for hi in by_level[upper] for lo in by_level[lower]]


def validate_static(name: str, static: dict) -> None:
    """推定の前提（重みが全部正）を確かめる。負の重みがあると「非負＋大小順」の意味が崩れる。"""
    bad = {m: w for m, w in static.items() if not w > 0}
    if bad:
        raise ValueError(f"{name}: 静的重みが正でない指標 {bad}（ADR-0059 は正の重みだけを扱う）")


def constraint_violation(weights: dict, static: dict) -> float:
    """制約からの最大の外れ（0 なら満たす）。合計・非負・大小順・支持集合を1つの数へ畳む。"""
    worst = abs(sum(weights.values()) - sum(static.values()))
    worst = max([worst] + [-w for w in weights.values()])
    worst = max([worst] + [weights[lo] - weights[hi] for hi, lo in order_pairs(static)])
    extra = set(weights) - set(static)
    if extra:
        worst = max([worst] + [abs(weights[m]) for m in extra])
    return worst


# ── 消費側と同じ標準化・月ごとの前計算 ───────────────────────────────────────

def standardized_matrix(X, factor_names: list, metrics: list) -> np.ndarray:
    """1期分のパネルを、消費側と同じ経路で列ごとに標準化した行列（行=社・列=metrics）。

    `build_view_stats` の統計は列ごとに独立（他の列の重みに依存しない）ので、全列を重み 1.0
    で渡して一度に作ってよい。z_momentum は `build_view_stats` が本番の `compute_momentum_z`
    と同じ2関数で揃える（ADR-0041 Decision 2）。
    """
    missing = [m for m in metrics if m not in factor_names]
    if missing:
        raise ValueError(f"パネルに無い列: {missing}（パネルの列: {factor_names}）")
    records = _panel_rows(X, factor_names)
    stats = build_view_stats(records, {m: 1.0 for m in metrics}, factor_names)
    return np.asarray([[standardize_metric(getattr(r, m), m, stats) for m in metrics]
                       for r in records], dtype=float)


def period_moments(Z: np.ndarray, y) -> tuple | None:
    """(c, S)。c = cov(Z, rank(y)) / sd(rank(y))、S = cov(Z)（どちらも 1/n）。

    順位は `_spearman` と同じ平均順位（同点は平均）。順位が無分散なら None（その月は学習に
    使えない）。
    """
    r = np.asarray(_avg_ranks([float(v) for v in y]), dtype=float)
    n = len(r)
    if n < 3:
        return None
    rc = r - r.mean()
    sd_r = float(np.sqrt(rc @ rc / n))
    if sd_r <= 0:
        return None
    Zc = Z - Z.mean(axis=0)
    return Zc.T @ rc / n / sd_r, Zc.T @ Zc / n


def panel_moments(panel: dict, factor_names: list, metrics: list) -> dict:
    """{ym: (c, S) | None}。全プリセットの列の和集合で1回だけ作り、プリセットは部分を取る。"""
    return {ym: period_moments(standardized_matrix(X, factor_names, metrics), y)
            for ym, (X, y) in sorted(panel.items())}


def sub_moments(moments: tuple | None, idx: list) -> tuple | None:
    if moments is None:
        return None
    c, S = moments
    return c[idx], S[np.ix_(idx, idx)]


def surrogate_ic(w: np.ndarray, moments: list) -> tuple[float, np.ndarray]:
    """月ごとの Pearson(Z w, rank(y)) の平均とその勾配。

    f_t = w'c / sqrt(w'Sw)、∂f_t/∂w = c/s − (w'c)·Sw/(q·s)（q = w'Sw, s = √q）。
    スコアが定数（q ≤ 0）の月は IC 0・勾配 0 として数える（分母には含める）。
    """
    total = 0.0
    grad = np.zeros_like(w, dtype=float)
    for c, S in moments:
        Sw = S @ w
        q = float(w @ Sw)
        if q <= 0:
            continue
        s = math.sqrt(q)
        wc = float(w @ c)
        total += wc / s
        grad += c / s - wc * Sw / (q * s)
    n = len(moments)
    return total / n, grad / n


# ── 推定 ────────────────────────────────────────────────────────────────────

@dataclass
class Fit:
    weights: dict
    success: bool
    message: str
    objective: float            # 学習期間の代理 IC（推定後）
    static_objective: float     # 同じ学習期間の代理 IC（静的重み）


def fit_weights(static: dict, moments: list) -> Fit:
    """制約付きで学習期間の代理 IC を最大化する（SLSQP・初期値は静的重み＝実行可能点）。"""
    from scipy.optimize import minimize

    metrics = list(static)
    idx = {m: i for i, m in enumerate(metrics)}
    w0 = np.asarray([static[m] for m in metrics], dtype=float)
    total = float(w0.sum())
    static_obj = surrogate_ic(w0, moments)[0]

    def neg(w):
        f, g = surrogate_ic(w, moments)
        return -f, -g

    constraints = [{"type": "eq", "fun": lambda w: float(w.sum()) - total,
                    "jac": lambda w: np.ones_like(w)}]
    for hi, lo in order_pairs(static):
        vec = np.zeros(len(metrics))
        vec[idx[hi]], vec[idx[lo]] = 1.0, -1.0
        constraints.append({"type": "ineq", "fun": lambda w, v=vec: float(v @ w),
                            "jac": lambda w, v=vec: v})

    res = minimize(neg, w0, jac=True, method="SLSQP", bounds=[(0.0, None)] * len(metrics),
                   constraints=constraints, options={"maxiter": 500, "ftol": 1e-12})
    # SLSQP は境界を ~1e-12 だけ踏むことがあるので、非負へ寄せてから合計を戻す。
    w = np.clip(np.asarray(res.x, dtype=float), 0.0, None)
    w = w * (total / w.sum()) if w.sum() > 0 else w0
    weights = {m: float(w[i]) for m, i in idx.items()}
    success = bool(res.success) and constraint_violation(weights, static) <= CONSTRAINT_TOL
    message = str(res.message) if success or not res.success else "constraint violated"
    return Fit(weights=weights, success=success, message=message,
               objective=surrogate_ic(w, moments)[0], static_objective=static_obj)


def _month_index(ym: str) -> int:
    year, month = ym.split("-")
    return int(year) * 12 + int(month) - 1


def train_months(all_yms: list, test_ym: str, *, embargo: int = EMBARGO_MONTHS,
                 window: int | None = None) -> list:
    """テスト月より embargo か月を超えて前の月（暦で判定）。window があれば直近 window か月。"""
    t = _month_index(test_ym)
    out = []
    for ym in sorted(all_yms):
        lag = t - _month_index(ym)
        if lag <= embargo:
            continue
        if window is not None and lag > embargo + window:
            continue
        out.append(ym)
    return out


def walk_forward(moments_by_ym: dict, static: dict, *, min_train: int = DEFAULT_MIN_TRAIN_MONTHS,
                 window: int | None = None, embargo: int = EMBARGO_MONTHS) -> dict:
    """{test_ym: fold}。学習月が min_train に満たないテスト月は作らない。"""
    usable = sorted(ym for ym, mom in moments_by_ym.items() if mom is not None)
    folds: dict = {}
    for test_ym in sorted(moments_by_ym):
        train = train_months(usable, test_ym, embargo=embargo, window=window)
        if len(train) < min_train:
            continue
        fit = fit_weights(static, [moments_by_ym[ym] for ym in train])
        folds[test_ym] = {
            "weights":          fit.weights,
            "success":          fit.success,
            "message":          fit.message,
            "train_objective":  fit.objective,
            "static_objective": fit.static_objective,
            "n_train":          len(train),
            "train_first":      train[0],
            "train_last":       train[-1],
        }
    return folds


# ── 評価（preset_ic_gate へ委譲）───────────────────────────────────────────

def oof_ic(panel: dict, factor_names: list, folds: dict) -> dict:
    """{test_ym: rank_IC}。月ごとに違う重みを、`ic_series` へ1期だけのパネルとして渡す。"""
    out: dict = {}
    for ym, fold in sorted(folds.items()):
        if not fold["success"]:
            continue
        ics = ic_series({ym: panel[ym]}, factor_names, fold["weights"])
        if ym in ics:
            out[ym] = ics[ym]
    return out


def evaluate_preset(name: str, static: dict, panel: dict, factor_names: list,
                    moments_union: dict, union: list, *, min_train: int,
                    window: int | None, n_boot: int) -> dict:
    """1プリセット分: walk-forward → OOF の対比較 → 全月での最終重み。"""
    validate_static(name, static)
    metrics = list(static)
    idx = [union.index(m) for m in metrics]
    moments = {ym: sub_moments(mom, idx) for ym, mom in moments_union.items()}

    folds = walk_forward(moments, static, min_train=min_train, window=window)
    wf_ic = oof_ic(panel, factor_names, folds)
    static_ic = ic_series({ym: panel[ym] for ym in wf_ic}, factor_names, static)
    sig = paired_ic_significance(wf_ic, static_ic, n_boot=n_boot)

    usable = [mom for _ym, mom in sorted(moments.items()) if mom is not None]
    final = fit_weights(static, usable)
    return {
        "static":         dict(static),
        "order_pairs":    [list(p) for p in order_pairs(static)],
        "n_folds":        len(folds),
        "n_failed":       sum(1 for f in folds.values() if not f["success"]),
        "folds":          folds,
        "oof_ic_wf":      wf_ic,
        "oof_ic_static":  static_ic,
        "mean_ic_wf":     statistics.fmean(wf_ic.values()) if wf_ic else None,
        "mean_ic_static": statistics.fmean(static_ic.values()) if static_ic else None,
        "significance":   sig,
        "final": {
            "weights":          final.weights,
            "success":          final.success,
            "message":          final.message,
            "train_objective":  final.objective,
            "static_objective": final.static_objective,
            "n_train":          len(usable),
        },
    }


def verdict_of(results: dict, alpha: float) -> str:
    """判定文。符号ではなく補正後 α で書く（ADR-0028 規則2）。"""
    better, worse = [], []
    for name, res in results.items():
        sig = res["significance"]
        if not sig or sig.get("p_value") is None or sig["p_value"] >= alpha:
            continue
        (better if sig["mean"] > 0 else worse).append(name)
    parts = []
    if better:
        parts.append(f"walk-forward significantly BETTER than static: {', '.join(better)}")
    if worse:
        parts.append(f"walk-forward significantly WORSE than static: {', '.join(worse)}")
    if not parts:
        parts.append("no preset differs from its static weights after correction")
    return " | ".join(parts) + f" (Bonferroni alpha={alpha:.4f})"


def weights_out_payload(results: dict) -> dict:
    """`preset_ic_gate --weights-json` がそのまま読める形（ラベル → 重み）。"""
    return {f"{name}{WF_LABEL_SUFFIX}": res["final"]["weights"]
            for name, res in results.items() if res["final"]["success"]}


# ── 出力 ────────────────────────────────────────────────────────────────────

def print_preset(name: str, res: dict, alpha: float, p_floor: float) -> None:
    mean_wf, mean_st = res["mean_ic_wf"], res["mean_ic_static"]
    wf_s = "n/a" if mean_wf is None else f"{mean_wf:+.4f}"
    st_s = "n/a" if mean_st is None else f"{mean_st:+.4f}"
    print(f"\n=== {name} ===", flush=True)
    print(f"  folds={res['n_folds']} failed={res['n_failed']} oof_months={len(res['oof_ic_wf'])} "
          f"rank-IC wf={wf_s} static={st_s}", flush=True)
    if res["n_failed"]:
        print(f"  !! 未収束の月 {res['n_failed']} 件を OOF 系列から外した（静的で埋めない）",
              flush=True)
    print(f"  wf - static: {fmt_sig(res['significance'], alpha, p_floor)}", flush=True)
    final = res["final"]
    print(f"  final weights (train months={final['n_train']} success={final['success']} "
          f"surrogate {final['static_objective']:+.4f} -> {final['train_objective']:+.4f}):",
          flush=True)
    for m, w_static in res["static"].items():
        print(f"    {_pad(m, 16)} static={w_static:5.2f}  wf={final['weights'][m]:6.3f}",
              flush=True)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"wrote {path}", flush=True)


def main() -> None:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Issue #625: 静的プリセットの重みを walk-forward で推定し、OOF で静的と比べる")
    ap.add_argument("--preset", action="append",
                    help=f"推定するプリセット（複数可）。既定は静的4つ（{', '.join(PRESETS)}）")
    ap.add_argument("--until", default=None,
                    help="この ym までの期だけ使う（過去の実測と期を揃えるとき）")
    ap.add_argument("--window-months", type=int, default=None,
                    help="学習をローリング窓（直近 N か月）にする。既定は拡大窓")
    ap.add_argument("--min-train-months", type=int, default=DEFAULT_MIN_TRAIN_MONTHS,
                    help="学習月がこれ未満のテスト月は作らない")
    ap.add_argument("--min-companies", type=int, default=DEFAULT_MIN_COMPANIES,
                    help="1期あたりの最小社数（これ未満の期は破棄）")
    ap.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT,
                    help="定常ブートストラップの反復数（p 値の下限は 2/(n_boot+1)）")
    ap.add_argument("--cache-panel", action="store_true",
                    help="パネルを scripts/.cache へ保存/再利用する（既定はフルビルド）")
    ap.add_argument("--json", nargs="?", const=str(DEFAULT_JSON), default=None,
                    help="全結果（fold ごとの重み・OOF 系列・検定）を JSON で書き出す")
    ap.add_argument("--weights-out", nargs="?", const=str(DEFAULT_WEIGHTS_OUT), default=None,
                    help="最終重みを preset_ic_gate --weights-json の形で書き出す（in-sample 用）")
    args = ap.parse_args()

    names = args.preset or list(PRESETS)
    unknown = [n for n in names if n not in PRESETS]
    if unknown:
        raise SystemExit(f"未知のプリセット: {unknown}（推定できるのは静的の {', '.join(PRESETS)}）")
    if args.window_months is not None and args.window_months < args.min_train_months:
        raise SystemExit("--window-months が --min-train-months より短いと fold が1つも作れません")
    for n in names:
        validate_static(n, PRESETS[n])

    started = time.monotonic()
    db = SessionLocal()
    try:
        print("[step] パネル構築（時点再現の gap_ratio あり）", flush=True)
        panel, factor_names, coverage = load_panel(db, args.min_companies, args.cache_panel,
                                                   with_gap_ratio=True)
        db.commit()
    finally:
        db.close()
    if args.until:
        panel = {ym: v for ym, v in panel.items() if ym <= args.until}
        if not panel:
            raise SystemExit(f"--until {args.until} で残る期がありません")
    info = panel_info(panel, factor_names)
    print_panel_info(info)
    gap_summary = gap_coverage_summary(coverage, args.min_companies, args.until)
    print_gap_summary(gap_summary)
    if not gap_summary["meets_criterion"]:
        print("gap_ratio のパネルが ADR-0057 の載せる基準を満たさないので推定しない", flush=True)
        raise SystemExit(2)

    union = list(dict.fromkeys(m for n in names for m in PRESETS[n]))
    print(f"[step] 月ごとの前計算（{len(panel)}期 × {len(union)}列）", flush=True)
    moments_union = panel_moments(panel, factor_names, union)
    skipped = sorted(ym for ym, mom in moments_union.items() if mom is None)
    if skipped:
        print(f"  前計算できなかった月（学習に使わない）: {skipped}", flush=True)

    print(f"\nembargo={EMBARGO_MONTHS} min_train={args.min_train_months} "
          f"window={'expanding' if args.window_months is None else args.window_months} "
          f"preprocess_version={PREPROCESS_VERSION}", flush=True)

    results: dict = {}
    for i, name in enumerate(names, 1):
        print(f"[step] walk-forward {i}/{len(names)}: {name}", flush=True)
        results[name] = evaluate_preset(name, PRESETS[name], panel, factor_names,
                                        moments_union, union,
                                        min_train=args.min_train_months,
                                        window=args.window_months, n_boot=args.n_boot)

    alpha = 0.05 / max(1, len(names))
    p_floor = 2.0 / (args.n_boot + 1)
    for name, res in results.items():
        print_preset(name, res, alpha, p_floor)

    verdict = verdict_of(results, alpha)
    print(f"\n=== VERDICT: {verdict} ===", flush=True)
    print("PRESETS は変えていない（反映は #546 が補正後 α を通ったものだけ行う）。"
          "最終重みを preset_ic_gate で測る比較は in-sample なので昇格の根拠にしない。", flush=True)
    elapsed_min = (time.monotonic() - started) / 60
    print(f"elapsed {elapsed_min:.1f} min", flush=True)

    no_oof = [n for n, r in results.items() if not r["oof_ic_wf"]]

    # 書き出しは全計算の完走後（途中で落ちたら何も残さない）。
    if args.json:
        _write_json(Path(args.json), {
            "panel": info,
            "gap_ratio_coverage": {"summary": gap_summary},
            "settings": {
                "presets":            names,
                "embargo_months":     EMBARGO_MONTHS,
                "min_train_months":   args.min_train_months,
                "window_months":      args.window_months,
                "until":              args.until,
                "min_companies":      args.min_companies,
                "n_boot":             args.n_boot,
                "p_value_floor":      p_floor,
                "alpha":              alpha,
                "n_tests":            len(names),
                "preprocess_version": PREPROCESS_VERSION,
            },
            "skipped_months": skipped,
            "results":        results,
            "verdict":        verdict,
            "elapsed_min":    elapsed_min,
        })
    if args.weights_out:
        _write_json(Path(args.weights_out), weights_out_payload(results))

    if no_oof:
        print(f"OOF の月が1つも無いプリセット: {no_oof}（結論を出していない）", flush=True)
        raise SystemExit(3)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
