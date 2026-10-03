"""sector_ols の ridge α の候補を、旧7点と現行の細かい格子で比べて実測する（Issue #761・ADR-0064）。

なぜ要るのか
------------
ridge の α は一個抜き誤差（LOO）で候補から選ぶ（ADR-0058）。候補が1桁刻み7点だった間、多くの業種で
LOO 誤差の谷が平坦で、桁違いに離れた候補（例: 10 と 100）がほぼ同点だった。数社の入力が変わるだけで
α が10倍・100倍へ跳び、**同じ業種の該当社以外の gap_ratio がまとめて動いた**（#758 の実測で機械 208社中
156社が 20pt 超）。どの α でも LOO 誤差はほぼ同じなので失敗としては現れない。

本スクリプトは同じ入力で旧7点（`LEGACY_ALPHAS`）と現行の `plugins.utils.RIDGE_ALPHAS` を当て、
次の4つを並べる。ADR-0064 の実測値の正本であり、候補を変えるときはこれで測り直す。

1. 谷の平坦さ   … 業種ごとの α（旧・新）と、旧格子で最小から1標準誤差以内に入る候補の数と幅
2. 精度         … 同じ LOO 曲線の上で、新 α の誤差が旧 α からどれだけ変わるか（悪化しないこと）
3. 切替の移動   … 同じ入力での旧格子と新格子の gap の差（本番が一度だけ動く量）
4. 安定性       … 各業種から社を抜いて当てはめ直し、α の跳びと残った社の gap の動きを旧・新で比べる
                  （抜く社は旧・新で共通＝対になった比較）

実行:
    python -m scripts.measure_ridge_alpha_stability
    python -m scripts.measure_ridge_alpha_stability --reps 10 --seed 1

注意:
  **書き込みは行わない。** `execute` も `_persist_and_rank` も呼ばず、DB に触れない `_prepare_fit` →
  `_fit_sector`（`execute` と `predict_gaps` が共有する本番の経路）だけを通る。入力は夜間と同じ
  （`nightly_scores.NIGHTLY_PARAMS["sector_ols"]`）で、財務レコードは1回だけ読む。

  旧格子は `plugins.utils.RIDGE_ALPHAS` を一時的に差し替えて当てる（`ridge_regression` は呼ぶたびに
  この定数を読む）。経路は同期なので差し替えが別スレッドへ漏れることはなく、`finally` で必ず戻す。

  プラグインの私的メソッドを使うので、シグネチャが変わると動かなくなる。`scripts/` は CI で実行されない
  ため、`tests/test_sector_ols.py::TestMeasureRidgeAlphaStabilityScript` が通しで走らせる。

  出力の区切りは ASCII 記号だけ（Windows cp932 のリダイレクトで落ちないため）。接続文字列は出さず、
  「ローカル／本番（リモート）」の別だけを出す。
"""
from __future__ import annotations

import argparse
import contextlib
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                              # noqa: E402

import plugins.utils as putils                                  # noqa: E402
from database import SessionLocal                               # noqa: E402
from plugins.sector_ols import SectorOLSPlugin, _row_key, gap_ratio_pct  # noqa: E402

# 1桁刻み7点（ADR-0058 時点の `RIDGE_ALPHAS`）。比べる相手として固定で持つ
LEGACY_ALPHAS: tuple[float, ...] = (1e-3, 1e-2, 0.1, 1.0, 10.0, 100.0, 1000.0)
# 「α が跳んだ」とみなす距離（log10 で半桁＝約3.2倍）。細かい格子の隣り合う候補（0.1桁）は数えない
JUMP_DECADES = 0.5
# 摂動の設計: (ラベル, 業種ごとに抜く割合, 最低限抜く社数)
DESIGNS = (("1社抜き", 0.0, 1), ("2%抜き", 0.02, 2))


# ── 差し替え ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def ridge_alphas(alphas):
    """`ridge_regression` の既定の候補を一時的に `alphas` へ差し替える（None なら現行のまま）。"""
    if alphas is None:
        yield
        return
    saved = putils.RIDGE_ALPHAS
    putils.RIDGE_ALPHAS = tuple(alphas)
    try:
        yield
    finally:
        putils.RIDGE_ALPHAS = saved


# ── 純関数（DB を触らない・テストから生値を食わせる）─────────────────────────

def loo_curve(X, y, alphas) -> tuple[np.ndarray, np.ndarray]:
    """候補ごとの LOO 二乗誤差の平均と、その標準誤差。`ridge_regression` と同じ設定（切片は X 側）。"""
    from sklearn.linear_model import RidgeCV
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    E = RidgeCV(alphas=list(alphas), fit_intercept=False, cv=None,
                store_cv_results=True).fit(X, y).cv_results_
    return E.mean(axis=0), E.std(axis=0, ddof=1) / math.sqrt(len(y))


def index_of(alphas, a) -> int:
    return min(range(len(alphas)), key=lambda i: abs(math.log10(alphas[i]) - math.log10(a)))


def within_one_se(alphas, mse, se) -> list[float]:
    """最小から1標準誤差以内に入る候補（谷の平坦さ）。"""
    i = int(np.argmin(mse))
    return [a for a, m in zip(alphas, mse) if m <= mse[i] + se[i]]


def alpha_jumped(a0, a1) -> bool:
    if a0 is None or a1 is None:
        return a0 is not a1
    return abs(math.log10(a1) - math.log10(a0)) >= JUMP_DECADES


def abs_diffs(base: dict, other: dict, keys=None) -> list[float]:
    """両方に値がある行の |gap の差|。`keys` を渡せばその行だけ。"""
    ks = base.keys() & other.keys() if keys is None else keys & base.keys() & other.keys()
    return [abs(other[k] - base[k]) for k in ks if base[k] is not None and other[k] is not None]


def summarize_diffs(d: list[float]) -> dict:
    if not d:
        return {"n": 0}
    a = np.asarray(d)
    return {"n": len(a), "median": float(np.median(a)), "p90": float(np.percentile(a, 90)),
            "p99": float(np.percentile(a, 99)), "over20": float(np.mean(a > 20) * 100),
            "over50": float(np.mean(a > 50) * 100)}


def spearman(base: dict, other: dict) -> float:
    from scipy.stats import spearmanr
    ks = [k for k in base.keys() & other.keys() if base[k] is not None and other[k] is not None]
    if len(ks) < 3:
        return float("nan")
    return float(spearmanr([base[k] for k in ks], [other[k] for k in ks]).statistic)


def drop_keys(keys_by_sector: dict, frac: float, min_drop: int, rng) -> set:
    """業種ごとに max(min_drop, round(frac×社数)) 行を無作為に選ぶ（業種名の順に引く＝seed で再現）。"""
    out = set()
    for sector in sorted(keys_by_sector):
        keys = sorted(keys_by_sector[sector], key=repr)
        k = min(len(keys), max(min_drop, round(frac * len(keys))))
        out.update(keys[i] for i in rng.choice(len(keys), k, replace=False))
    return out


# ── 本番の経路で当てはめる ───────────────────────────────────────────────────

def fit_all(plugin: SectorOLSPlugin, records: list, params: dict, alphas=None) -> dict:
    """`predict_gaps` と同じ手続きで全業種を当てはめ、業種の α・行の gap・当てはめ本体を返す。"""
    with ridge_alphas(alphas):
        prep = plugin._prepare_fit(records, params)
        alpha, gap, fits = {}, {}, {}
        for sector, samples in sorted(prep.by_sector.items()):
            fit = plugin._fit_sector(prep, sector, samples, params)
            if fit is None:
                continue
            fits[sector] = fit
            alpha[sector] = fit.result.get("alpha")
            for (_row, actual, r), predicted in zip(fit.samples, fit.all_yhat):
                gap[_row_key(r)] = gap_ratio_pct(predicted, actual)
    return {"alpha": alpha, "gap": gap, "fits": fits}


# ── 表示 ────────────────────────────────────────────────────────────────────

def _g(v) -> str:
    return "-" if v is None else "{0:.4g}".format(v)


def _row(label: str, s: dict) -> str:
    if not s.get("n"):
        return "  {0:<5} (比べられる行なし)".format(label)
    return ("  {0:<5} 中央値 {median:6.2f}  p90 {p90:7.2f}  p99 {p99:8.2f}  "
            "20pt超 {over20:5.2f}%  50pt超 {over50:5.2f}%  (n={n})").format(label, **s)


def main(argv=None) -> int:
    from scripts._textwidth import display_width, pad

    ap = argparse.ArgumentParser(description="ridge α の候補（旧7点 vs 現行）を同じ入力で比べる")
    ap.add_argument("--reps", type=int, default=30, help="摂動の繰り返し回数（設計ごと・既定 30）")
    ap.add_argument("--seed", type=int, default=0, help="抜く社を選ぶ乱数の種（既定 0）")
    args = ap.parse_args(argv)

    from database import _is_local
    from nightly_scores import NIGHTLY_PARAMS
    from plugins.utils import coerce_params

    plugin = SectorOLSPlugin()
    params = coerce_params(plugin.params_schema(), dict(NIGHTLY_PARAMS["sector_ols"]))
    db = SessionLocal()
    try:
        records = plugin._load_records(db, params["year"], params["features"])
    finally:
        db.close()
    new_alphas = tuple(putils.RIDGE_ALPHAS)
    print("接続先={0} / 読み込み {1} レコード / 旧 {2}点 / 新 {3}点 ({4:g}..{5:g})".format(
        "ローカル" if _is_local else "本番（リモート）", len(records),
        len(LEGACY_ALPHAS), len(new_alphas), new_alphas[0], new_alphas[-1]))

    timings = {}
    t = time.perf_counter()
    old = fit_all(plugin, records, params, LEGACY_ALPHAS)
    timings["old"] = time.perf_counter() - t
    t = time.perf_counter()
    new = fit_all(plugin, records, params)
    timings["new"] = time.perf_counter() - t

    # ── 1. 谷の平坦さ ・ 2. 精度 ──
    curve_alphas = sorted(set(LEGACY_ALPHAS) | set(new_alphas))
    sectors = sorted(new["fits"])
    width = max([display_width(s) for s in sectors] + [4])
    print()
    print("[1] 谷の平坦さ / [2] 精度（同じ LOO 曲線の上で 新alpha の誤差が 旧alpha からどれだけ変わるか）")
    print("  {0}  {1:>5} {2:>8} {3:>8} {4:>9} {5:>12}".format(
        pad("業種", width), "n", "旧alpha", "新alpha", "誤差変化%", "旧1SE内(幅)"))
    changes, weights, mismatch, ridge_secs = [], [], 0, {"old": 0.0, "new": 0.0}
    for s in sectors:
        fit = new["fits"][s]
        mse, se = loo_curve(fit.X_norm, fit.y_normed, curve_alphas)
        a_old, a_new = old["alpha"].get(s), new["alpha"].get(s)
        if a_new is None:
            continue
        # 本番の選び方（scoring 付き RidgeCV）と、曲線の argmin（新格子の範囲）が一致するかの突き合わせ
        new_idx = [index_of(curve_alphas, a) for a in new_alphas]
        if index_of(curve_alphas, a_new) != min(new_idx, key=lambda i: mse[i]):
            mismatch += 1
        legacy_idx = [index_of(curve_alphas, a) for a in LEGACY_ALPHAS]
        flat = within_one_se([curve_alphas[i] for i in legacy_idx], mse[legacy_idx], se[legacy_idx])
        span = math.log10(max(flat)) - math.log10(min(flat))
        ch = (100.0 * (mse[index_of(curve_alphas, a_new)] / mse[index_of(curve_alphas, a_old)] - 1.0)
              if a_old is not None else float("nan"))
        if math.isfinite(ch):
            changes.append(ch)
            weights.append(len(fit.samples))
        print("  {0}  {1:>5} {2:>8} {3:>8} {4:>9.2f} {5:>6d} ({6:.0f}桁)".format(
            pad(s, width), len(fit.samples), _g(a_old), _g(a_new), ch, len(flat), span))
        for label, grid in (("old", LEGACY_ALPHAS), ("new", new_alphas)):
            t = time.perf_counter()
            putils.ridge_regression(fit.X_norm, fit.y_normed, alphas=list(grid))
            ridge_secs[label] += time.perf_counter() - t
    if changes:
        print("  誤差変化%: 社数加重平均 {0:.3f} / 最大 {1:.3f} / 悪化した業種 {2} / 本番の alpha と曲線の"
              " argmin の不一致 {3}業種".format(np.average(changes, weights=weights), max(changes),
                                               sum(1 for c in changes if c > 1e-9), mismatch))

    # ── 3. 切替の一度きりの移動 ──
    moves = abs_diffs(old["gap"], new["gap"])
    n_changed = sum(1 for s in sectors if old["alpha"].get(s) != new["alpha"].get(s))
    n_jumped = sum(1 for s in sectors if alpha_jumped(old["alpha"].get(s), new["alpha"].get(s)))
    print()
    print("[3] 切替の一度きりの移動（同じ入力・旧格子 -> 新格子の gap の差 [pt]）")
    print("  alpha が変わった業種 {0} / {1}（うち半桁超 {2}）  |  0.01超動いた社 {3} / {4}  |  "
          "Spearman {5:.4f}".format(n_changed, len(sectors), n_jumped,
                                    sum(1 for x in moves if x > 0.01), len(moves),
                                    spearman(old["gap"], new["gap"])))
    print(_row("差", summarize_diffs(moves)))

    # ── 4. 安定性（摂動）──
    keys_by_sector = {s: [_row_key(r) for (_x, _y, r) in new["fits"][s].samples] for s in sectors}
    # 社数が縮約の閾値未満の業種は gap が全社プールへ寄せられるので、α の跳びが gap へ届きにくい
    unshrunk = {s for s in sectors if len(new["fits"][s].samples) >= params["shrink_threshold"]}
    print()
    print("[4] 安定性: 各業種から社を抜いて当てはめ直し、残った社の |gap の差| [pt]（抜く社は旧・新で共通・"
          "reps={0} seed={1}）".format(args.reps, args.seed))
    for label, frac, min_drop in DESIGNS:
        rng = np.random.default_rng(args.seed)
        diffs = {"old": [], "new": []}
        jumps = {"old": 0, "new": 0}
        jumps_unshrunk = {"old": 0, "new": 0}
        for _ in range(args.reps):
            drop = drop_keys(keys_by_sector, frac, min_drop, rng)
            sub = [r for r in records if _row_key(r) not in drop]
            kept = set(new["gap"]) - drop
            for name, grid, base in (("old", LEGACY_ALPHAS, old), ("new", None, new)):
                res = fit_all(plugin, sub, params, grid)
                diffs[name].extend(abs_diffs(base["gap"], res["gap"], kept))
                hit = {s for s in sectors if alpha_jumped(base["alpha"].get(s), res["alpha"].get(s))}
                jumps[name] += len(hit)
                jumps_unshrunk[name] += len(hit & unshrunk)
        n_cmp, n_cmp_u = len(sectors) * args.reps, max(1, len(unshrunk) * args.reps)
        print("  {0}: alpha の半桁超の跳び 旧 {1:.1f}% / 新 {2:.1f}%（業種x回 {3}）  |  縮約なしの{4}業種に限ると"
              " 旧 {5:.1f}% / 新 {6:.1f}%".format(
                  label, 100.0 * jumps["old"] / n_cmp, 100.0 * jumps["new"] / n_cmp, n_cmp, len(unshrunk),
                  100.0 * jumps_unshrunk["old"] / n_cmp_u, 100.0 * jumps_unshrunk["new"] / n_cmp_u))
        print(_row("旧", summarize_diffs(diffs["old"])))
        print(_row("新", summarize_diffs(diffs["new"])))

    print()
    print("所要: 全業種を1回当てはめる（_prepare_fit 込み） 旧 {0:.2f}秒 / 新 {1:.2f}秒  |  ridge だけ 旧 {2:.3f}秒"
          " / 新 {3:.3f}秒".format(timings["old"], timings["new"], ridge_secs["old"], ridge_secs["new"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
