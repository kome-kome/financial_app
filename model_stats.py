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

有意判定の規則（ADR-0063・#741）:
  「p 値 < α」かつ「同じ α の CI が 0 を跨がない」。p の規則は CI の規則より厳しい側にあるので
  実用上は「p < α」と同じで、CI 側は p の丸めの端で「有意なのに表示の CI が 0 を含む」を防ぐ安全装置。
  昇格・退役の手続き（ADR-0021/0028/0044）とゲート系スクリプトが p と補正後 α で判定しているのに
  揃える。複数の組を同時に検定するときは `paired_family_significance` が組数から補正後 α を出す——
  `paired_ic_significance` を並べて既定の alpha のまま `significant` を読むと補正なしになる。
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

# 多重比較の補正（ADR-0063）。bonferroni = 家族の α を実際に検定した組数で割る。
# none = 渡された alpha をそのまま1検定あたりの α として使う（呼び出し元が補正済みの α を持つ場合）。
CORRECTIONS = ("bonferroni", "none")


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
             系列相関を保存するため素朴 t より保守的。p は alpha に依存しない（CI だけが alpha で決まる）。
             有意判定は `_is_significant`（p と同じ alpha の CI の両方）で別途行う。
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
    # 「paired-t より保守的」という趣旨に反するため。有意判定は `_is_significant` で別途行う。
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


def p_floor(n_boot: int = DEFAULT_N_BOOT) -> float:
    """`bootstrap_mean_ci` が返しうる p の下限（Davison-Hinkley フロア・返り値と同じ4桁丸め）。

    有意は「p < α」なので、1検定あたりの α がこれ以下だとどの組も有意になりえない。
    """
    return round(2.0 / (n_boot + 1), 4)


def ci_level_label(alpha: float) -> str:
    """CI の水準を表示用の文字列にする（0.05 → '95%'、0.05/15 → '99.67%'）。

    CI は判定と同じ alpha で作るので、補正後は 95% ではない。表示側に「95%」を焼き付けない。
    """
    return f"{100 * (1 - alpha):.2f}".rstrip("0").rstrip(".") + "%"


def _common_periods(ic_by_period_a: dict, ic_by_period_b: dict) -> list:
    """ペアリングに使う共通 test 期（昇順）。2期未満の組は検定しない（検定数にも数えない）。"""
    return sorted(set(ic_by_period_a) & set(ic_by_period_b))


def _is_significant(stats: dict, alpha: float) -> bool:
    """有意判定の唯一の規則（ADR-0063）: p < alpha かつ 同じ alpha の CI が 0 を跨がない。

    p は返り値と同じ丸め後の値で比べる（画面・スクリプトが並べる数字と同じもので判定する）。
    p の規則は CI の規則より厳しい側にあり、実用上は p < alpha と同値。CI 側の条件は、p の
    4桁丸めの端で「有意なのに表示の CI が 0 を含む」が起きないための安全装置。
    """
    return bool(stats["p_value"] < alpha and (stats["ci_lo"] > 0 or stats["ci_hi"] < 0))


def paired_ic_significance(ic_by_period_a: dict, ic_by_period_b: dict, *,
                           alpha: float = 0.05,
                           n_boot: int = DEFAULT_N_BOOT,
                           avg_block: float = DEFAULT_AVG_BLOCK,
                           seed: int = DEFAULT_SEED) -> dict | None:
    """2モデルの per-fold IC を**共通 test 期でペアリング**し、差 IC_A−IC_B の平均を検定。

    ic_by_period_* = {test_ym: ic}（oof_backtest の rank_ic_by_period）。
    共通する test_ym のみで差系列を作る（学習窓が揃わないモデル同士でも fair）。
    alpha: **この1検定**の有意水準。CI は (1−alpha) の区間になり、`significant` も同じ alpha で
           判定する。複数の組を同時に検定するなら補正後の値が要る——`paired_family_significance`
           が組数から出す（既定の 0.05 のまま並べると補正なし・#741）。
    返り値は bootstrap_mean_ci に n_common・alpha・significant を足したもの、または共通期<2 で None。
    """
    common = _common_periods(ic_by_period_a, ic_by_period_b)
    if len(common) < 2:
        return None
    diffs = [ic_by_period_a[ym] - ic_by_period_b[ym] for ym in common]
    stats = bootstrap_mean_ci(diffs, n_boot=n_boot, avg_block=avg_block, seed=seed, alpha=alpha)
    if stats is None:
        return None
    stats["n_common"] = len(common)
    stats["alpha"] = alpha
    stats["significant"] = _is_significant(stats, alpha)
    return stats


def paired_family_significance(pairs: dict, *, alpha: float = 0.05,
                               correction: str = "bonferroni",
                               n_boot: int = DEFAULT_N_BOOT,
                               avg_block: float = DEFAULT_AVG_BLOCK,
                               seed: int = DEFAULT_SEED) -> dict:
    """同時に検定する組の家族へ多重比較の補正を掛け、各組を `paired_ic_significance` で判定する。

    pairs = {key: (ic_by_period_a, ic_by_period_b)}（差は a − b）。
    correction="bonferroni": 1検定あたりの α = alpha / m。m は**実際に検定した組の数**で、共通期が
      2 未満で検定できない組は数えない（ADR-0041 §5「検定数は実際に走らせた数から機械が出す」）。
    correction="none": alpha をそのまま1検定あたりの α として使う（呼び出し元が補正済みの α を持つ場合）。

    返り値: {"alpha": 1検定あたりの α（判定と CI に使った値）, "family_alpha": alpha,
             "correction", "n_tests": m, "p_floor",
             "alpha_below_p_floor": 1検定あたりの α が p の下限以下＝どの組も有意になりえない,
             "results": {key: paired_ic_significance の返り値 or None}}
    """
    if correction not in CORRECTIONS:
        raise ValueError(f"correction は {CORRECTIONS} のいずれか: {correction!r}")
    testable = {key for key, (a, b) in pairs.items() if len(_common_periods(a, b)) >= 2}
    n_tests = len(testable)
    per_test = alpha / n_tests if (correction == "bonferroni" and n_tests) else alpha
    results = {
        key: (paired_ic_significance(a, b, alpha=per_test, n_boot=n_boot,
                                     avg_block=avg_block, seed=seed)
              if key in testable else None)
        for key, (a, b) in pairs.items()
    }
    floor = p_floor(n_boot)
    return {
        "alpha": per_test,
        "family_alpha": alpha,
        "correction": correction,
        "n_tests": n_tests,
        "p_floor": floor,
        "alpha_below_p_floor": per_test <= floor,
        "results": results,
    }


def significance_matrix(ic_by_period_by_model: dict, *, alpha: float = 0.05,
                        correction: str = "bonferroni",
                        n_boot: int = DEFAULT_N_BOOT,
                        avg_block: float = DEFAULT_AVG_BLOCK,
                        seed: int = DEFAULT_SEED) -> dict:
    """全モデルペアの IC 差有意性マトリクス（多重比較の補正込み・ADR-0063）。

    ic_by_period_by_model = {model_key: {test_ym: ic}}（IC 系列を持つモデルのみ）。
    alpha と correction の意味は `paired_family_significance` と同じ（既定は全ペアで Bonferroni）。
    返り値: {"models": [key,...], "pairs": {"A|B": {mean_diff, ci_lo, ci_hi, p_value,
             significant, n_common, better}}, "alpha": 1検定あたりの α, "family_alpha",
             "correction", "n_tests", "p_floor", "alpha_below_p_floor"}。
             better = 差が有意なとき優位なモデルキー、有意でなければ None。
             順序対の重複を避け a<b の上三角のみ格納。
    """
    keys = list(ic_by_period_by_model.keys())
    order = [(a, b) for i, a in enumerate(keys) for b in keys[i + 1:]]
    family = paired_family_significance(
        {f"{a}|{b}": (ic_by_period_by_model[a], ic_by_period_by_model[b]) for a, b in order},
        alpha=alpha, correction=correction, n_boot=n_boot, avg_block=avg_block, seed=seed,
    )
    pairs: dict[str, dict] = {}
    for a, b in order:
        res = family["results"][f"{a}|{b}"]
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
    meta = {k: family[k] for k in ("alpha", "family_alpha", "correction", "n_tests",
                                   "p_floor", "alpha_below_p_floor")}
    return {"models": keys, "pairs": pairs, **meta}


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
