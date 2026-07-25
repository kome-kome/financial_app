"""モデル比較の統計的厳密化（Issue #369）: rank-IC 差の有意性検定＋分位単調性。

`model_comparison` / `plugins.macro_snapshots.oof_backtest` の**純後処理**層。
追加学習・価格取得・Egress ゼロ（stdlib の `random`/`statistics`/`math` のみ）。

なぜ素朴な paired-t ではダメか（Issue #369）:
  walk-forward の各 fold は学習窓が重複し、per-fold の IC 系列は iid ではない
  （系列相関を持つ）。素朴な paired-t はこの相関を無視して分散を過小評価し、
  有意差を過大に主張する（楽観的すぎる）。ここでは分布仮定を置かず系列相関を
  保存する **定常ブートストラップ**（Politis & Romano 1994）で平均の分布を得る。
  リサンプル単位を「1点」ではなく「幾何長のブロック」にすることで、隣接 fold の
  相関構造をブートストラップ標本内に温存する。

参考:
  - Politis, D.N. & Romano, J.P. (1994) "The Stationary Bootstrap"
    J. Amer. Statist. Assoc. 89(428), 1303-1313. DOI:10.1080/01621459.1994.10476870
  - Nadeau, C. & Bengio, Y. (2003) "Inference for the Generalization Error"
    Machine Learning 52, 239-281. DOI:10.1023/A:1024068626366

決定性: すべての乱数は `random.Random(seed)`（既定 seed=0）で再現可能。
テスト・本番・比較ビューで同じ入力 → 同じ p 値／CI（フレーク無し）。
"""
from __future__ import annotations

import math
import random
import statistics

# ブートストラップ既定。avg_block=平均ブロック長（fold 間相関のカバー範囲）。
# 月次 walk-forward の IC 系列は数十 fold 程度のため、n_boot=2000 で CI は安定。
DEFAULT_N_BOOT = 2000
DEFAULT_AVG_BLOCK = 3
DEFAULT_SEED = 0


def _percentile(sorted_vals: list[float], q: float) -> float:
    """昇順ソート済み系列の q パーセンタイル（0-100・線形補間）。"""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _stationary_bootstrap_sample(series: list[float], rng: random.Random,
                                 avg_block: float) -> list[float]:
    """定常ブートストラップで長さ len(series) の1標本を生成（Politis-Romano 1994）。

    開始点をランダムに選び、各ステップで確率 p=1/avg_block でブロックを打ち切って
    新しいランダム開始点へ跳ぶ。そうでなければ隣（円環）へ進む。ブロック長は
    幾何分布（平均 avg_block）に従い、系列相関を標本内に保存する。
    """
    L = len(series)
    p = 1.0 / max(avg_block, 1.0)
    out: list[float] = []
    i = rng.randrange(L)
    while len(out) < L:
        out.append(series[i])
        if rng.random() < p:
            i = rng.randrange(L)
        else:
            i = (i + 1) % L
    return out


def bootstrap_mean_ci(series: list[float], *, n_boot: int = DEFAULT_N_BOOT,
                      avg_block: float = DEFAULT_AVG_BLOCK, seed: int = DEFAULT_SEED,
                      alpha: float = 0.05) -> dict | None:
    """系列平均の定常ブートストラップ CI と「平均=0」に対する両側 p 値。

    返り値:
      {mean, ci_lo, ci_hi, p_value, n, n_boot} または n<2 で None。
    p_value: 両側。各裾を (count+1)/(n_boot+1) で推定（Davison-Hinkley フロア）した
             小さい方×2・[0,1] クランプ。全リサンプル同符号でも 0 にならず p≥2/(n_boot+1)。
             系列相関を保存するため素朴 t より保守的。有意判定は CI 基準（下記）で別途行う。
    """
    n = len(series)
    if n < 2:
        return None
    obs = statistics.mean(series)
    rng = random.Random(seed)
    boot_means = sorted(
        statistics.mean(_stationary_bootstrap_sample(series, rng, avg_block))
        for _ in range(n_boot)
    )
    ci_lo = _percentile(boot_means, 100 * (alpha / 2))
    ci_hi = _percentile(boot_means, 100 * (1 - alpha / 2))
    # 両側 p: 帰無 H0=平均0。観測平均の符号と逆側の裾確率×2。
    # Davison-Hinkley フロア (count+1)/(n_boot+1)（Monte-Carlo p 値の標準推定）で
    # p を厳密 0 にしない（全リサンプル同符号でも p≥2/(n_boot+1)）。有限回数の
    # リサンプルで「H0 下の確率ゼロ」を主張するのは反保守的で、本モジュールの
    # 「paired-t より保守的」という趣旨に反するため。有意判定は CI 基準で別途行う。
    b_le0 = sum(1 for x in boot_means if x <= 0.0)
    p_lower = (b_le0 + 1) / (n_boot + 1)               # H0: mean<=0 片側
    p_upper = (n_boot - b_le0 + 1) / (n_boot + 1)       # H0: mean>=0 片側
    p_value = min(1.0, 2.0 * min(p_lower, p_upper))
    return {
        "mean": round(obs, 6),
        "ci_lo": round(ci_lo, 6),
        "ci_hi": round(ci_hi, 6),
        "p_value": round(p_value, 4),
        "n": n,
        "n_boot": n_boot,
    }


def paired_ic_significance(ic_by_period_a: dict, ic_by_period_b: dict, *,
                           n_boot: int = DEFAULT_N_BOOT,
                           avg_block: float = DEFAULT_AVG_BLOCK,
                           seed: int = DEFAULT_SEED) -> dict | None:
    """2モデルの per-fold IC を**共通 test 期でペアリング**し、差 IC_A−IC_B の平均を検定。

    ic_by_period_* = {test_ym: ic}（oof_backtest の rank_ic_by_period）。
    共通する test_ym のみで差系列を作る（学習窓が揃わないモデル同士でも fair）。
    返り値は bootstrap_mean_ci に n_common を足したもの、または共通期<2 で None。
    """
    common = sorted(set(ic_by_period_a) & set(ic_by_period_b))
    if len(common) < 2:
        return None
    diffs = [ic_by_period_a[ym] - ic_by_period_b[ym] for ym in common]
    stats = bootstrap_mean_ci(diffs, n_boot=n_boot, avg_block=avg_block, seed=seed)
    if stats is None:
        return None
    stats["n_common"] = len(common)
    stats["significant"] = bool(stats["ci_lo"] > 0 or stats["ci_hi"] < 0)
    return stats


def significance_matrix(ic_by_period_by_model: dict, *, alpha: float = 0.05,
                        n_boot: int = DEFAULT_N_BOOT,
                        avg_block: float = DEFAULT_AVG_BLOCK,
                        seed: int = DEFAULT_SEED) -> dict:
    """全モデルペアの IC 差有意性マトリクス。

    ic_by_period_by_model = {model_key: {test_ym: ic}}（IC 系列を持つモデルのみ）。
    返り値: {"models": [key,...], "pairs": {"A|B": {mean_diff, ci_lo, ci_hi, p_value,
             significant, n_common, better}}}。better = 差が有意なとき優位なモデルキー、
             有意でなければ None。順序対の重複を避け a<b の上三角のみ格納。
    """
    keys = list(ic_by_period_by_model.keys())
    pairs: dict[str, dict] = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            res = paired_ic_significance(
                ic_by_period_by_model[a], ic_by_period_by_model[b],
                n_boot=n_boot, avg_block=avg_block, seed=seed,
            )
            if res is None:
                pairs[f"{a}|{b}"] = {"n_common": 0, "significant": False,
                                     "better": None, "mean_diff": None,
                                     "ci_lo": None, "ci_hi": None, "p_value": None}
                continue
            better = None
            if res["significant"]:
                better = a if res["mean"] > 0 else b
            pairs[f"{a}|{b}"] = {
                "mean_diff": res["mean"], "ci_lo": res["ci_lo"], "ci_hi": res["ci_hi"],
                "p_value": res["p_value"], "significant": res["significant"],
                "n_common": res["n_common"], "better": better,
            }
    return {"models": keys, "pairs": pairs, "alpha": alpha}


def monotonicity_summary(quantile_spearmans: list[float], adj_increasing: int,
                         adj_total: int, *, n_boot: int = DEFAULT_N_BOOT,
                         avg_block: float = DEFAULT_AVG_BLOCK,
                         seed: int = DEFAULT_SEED) -> dict:
    """分位単調性のサマリ（oof_backtest から渡される期毎統計を畳む）。

    quantile_spearmans: 期毎の Spearman(分位idx, 分位平均リターン) の系列。
    adj_increasing / adj_total: 全期・全隣接分位ペアのうち「上位分位>下位分位」の数と総数。

    返り値:
      spearman_mean/std: 期毎 Spearman の mean/std（+1 で完全単調増加）。
      adjacent_increasing_rate: 隣接分位が正順（過学習の U 字なら低下）。
      p_value: 「期毎 Spearman の平均 <= 0」に対する片側ブートストラップ p 値。
               小さいほど「単調増加が偶然でない」。系列が短い/無分散なら None。
      n_periods: 単調性を評価できた期数。
    """
    n = len(quantile_spearmans)
    spearman_mean = round(statistics.mean(quantile_spearmans), 4) if n else None
    spearman_std = round(statistics.pstdev(quantile_spearmans), 4) if n > 1 else (
        0.0 if n == 1 else None)
    adjacent_rate = round(adj_increasing / adj_total, 4) if adj_total else None

    p_value = None
    if n >= 2:
        ci = bootstrap_mean_ci(quantile_spearmans, n_boot=n_boot,
                               avg_block=avg_block, seed=seed)
        if ci is not None:
            rng = random.Random(seed + 1)
            boot_means = [
                statistics.mean(_stationary_bootstrap_sample(quantile_spearmans, rng, avg_block))
                for _ in range(n_boot)
            ]
            # 片側 H0: mean<=0。ブートストラップ平均が 0 以下の割合。
            # (count+1)/(n_boot+1) フロアで p を厳密 0 にしない（bootstrap_mean_ci と同方針）。
            p_value = round((sum(1 for x in boot_means if x <= 0.0) + 1) / (n_boot + 1), 4)
    return {
        "spearman_mean": spearman_mean,
        "spearman_std": spearman_std,
        "adjacent_increasing_rate": adjacent_rate,
        "p_value": p_value,
        "n_periods": n,
    }
