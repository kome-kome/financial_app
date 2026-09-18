"""計測結果（bench_macro_beta の JSONL）を1枚の表に畳む（Issue #512 / #540 / #664）。

なぜ別スクリプトか
------------------
比較の分母は run ごとに違う（銘柄数・tune・draws・チェーン数）。**素の所要を並べても
比較にならない**ので、ここで共通の土俵へ直す:

- **1 leapfrog 歩の実費**（`total_steps` に対する回帰の傾き）。draws を分母にすると、run 間で
  steps/draw が変わった瞬間に傾きが汚染される（実測 n_stock=1000 で 1023 歩と 709.6 歩の
  run が混ざった）
- **観測1件あたり**（us/step/obs）。銘柄数の違う run を横に並べるための正規化

3つのビュー
-----------
- `--view cost`（既定・#512）: 上記のコスト表。**`--draws` を2点以上振った run 用**
  （1点しか無い run は傾きが出ないので us/step が n/a になる）
- `--view ess`（#540）: 統計効率の表。軌道長（`max_tree_depth`）の格子を並べる。
  **主指標は `ESS/1e6step`＝時間を含まない量**——`ESS/秒 = (ESS/歩) × (歩/秒)` で `歩/秒` は
  マシンとパネルの性質であって `max_tree_depth` の関数ではなく、ローカルの us/step は
  **時間帯で 2.4倍振れる**（GOTCHAS）。数時間かかる格子を所要で並べるとドリフトが差に化ける。
- `--view scale`（#664）: 収束ゲートの量（変数別 `r_hat` p99）を銘柄数に対して並べ、seed 間の
  幅と `p99 ~ ln(n_stock)` の傾き（95%CI・判定規則は事前固定）を出す。本番規模への外挿は参考値。

どのビューも**生値を出す**（丸めた表示で判断しない・#466）。

読む先は `--inputs` で与える JSONL（ローカル実行と GHA アーティファクトの両方）。

実行例（必ず -m 形式）::

    python -m scripts.bench_macro_beta_report --inputs .logs/bench_512.jsonl \\
        .logs/gha_bench/b0/bench-macro-beta/bench_512.jsonl
    python -m scripts.bench_macro_beta_report --view ess --inputs .logs/bench_540.jsonl
    python -m scripts.bench_macro_beta_report --view scale --inputs .logs/bench_664_scale.jsonl
"""
from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402

# 本番の収束ゲートそのもの（`gate_values` / `persist_allowed` / `MONTHLY_RHAT_THRESHOLD`）。
# pymc は同モジュール内で遅延 import されるので、ここでの import は実測 0.43 秒で済む。
import macro_beta_inference as mb  # noqa: E402


def per_step_seconds(record: dict):
    """総 leapfrog 歩数に対する回帰の傾き＝1歩の実費[秒]。2点未満・歩数が同じなら None。"""
    points = [(r.get("total_steps"), r.get("seconds")) for r in record.get("runs", [])]
    points = [(x, y) for x, y in points if x and y]
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    if float(xs.max() - xs.min()) <= 0.0:
        return None
    slope, _ = np.polyfit(xs, ys, 1)
    return float(slope)


def load(paths: list) -> list:
    records = []
    for pattern in paths:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
    return records


def fmt(value, spec: str = "{0:.4g}") -> str:
    """生値をそのまま出す。測れなかったものは **n/a**（0 と区別する）。"""
    if value is None:
        return "n/a"
    try:
        return spec.format(value)
    except (TypeError, ValueError):
        return str(value)


def cost_table(records: list) -> str:
    """#512 のコスト表: 1歩の実費と、観測1件あたりへの正規化。"""
    header = "{0:<22} {1:>7} {2:>7} {3:>7} {4:>6} {5:>10} {6:>11} {7:>10} {8:>9}".format(
        "label", "n_stock", "n_obs", "chains", "cpus", "us/step", "us/step/obs",
        "steps/draw", "cpu/wall")
    lines = [header, "-" * len(header)]
    for rec in records:
        slope = per_step_seconds(rec)
        panel = rec.get("panel", {})
        cfg = rec.get("config", {})
        env = rec.get("env", {})
        last = (rec.get("runs") or [{}])[-1]
        steps_mean = (last.get("steps") or {}).get("mean")
        lines.append("{0:<22} {1:>7} {2:>7} {3:>7} {4:>6} {5:>10} {6:>11} {7:>10} {8:>9}".format(
            str(rec.get("label"))[:22], panel.get("n_stock"), panel.get("n_obs"),
            cfg.get("chains"), env.get("cpu_count"),
            fmt(None if slope is None else slope * 1e6, "{0:.1f}"),
            fmt(None if slope is None else slope * 1e6 / panel["n_obs"], "{0:.4f}"),
            fmt(steps_mean, "{0:.0f}"), fmt(last.get("cpu_per_wall"), "{0:.2f}")))
    return chr(10).join(lines)


def ess_table(records: list) -> str:
    """#540 の統計効率表: 軌道長の格子を並べる（1 run に複数 draws 点があれば全部出す）。

    `td_rate` は**その run の上限に対する**到達率。1.000 なら軌道はまだ切られている側にあり、
    下回っていれば U ターンで自然に止まり始めている＝上限がもう律速でないことの合図。

    `gate` / `p99_worst` は #613 で足した。#611 で本番のゲートが「変数ごとの `r_hat` の p99」に
    なったのに、この表は `r_hat_max` を出したままだった——人は表の値を 1.05 と見比べて設定を
    選ぶので、**その基準はもう本番の合否と一致しない**。ずれる向きは「通る側」だけ＝良い設定を
    誤って捨てる。実例: 本番の run（`mb_20260906T055243Z`）は `r_hat_max=1.1242` で表の上では
    失格に見えるが、新ゲートでは通る。
    """
    if not records:
        return "入力が空です（JSONL がまだ無いか、1行も書かれていない）"
    header = ("{0:<14} {1:>7} {2:>6} {3:>9} {4:>7} {5:>6} {6:>6} {7:>16} {8:>9} {9:>9} "
              "{10:>9} {11:>12} {12:>10} {13:>9}").format(
        "label", "md", "ta", "steps/dr", "td_rate", "n_div", "gate", "gate_worst",
        "r_hat_max", "ess_min", "ess_med", "ESS/1e6step", "ESS/sec", "sec")
    lines = ["=" * len(header), "bench ESS grid (raw values)", "=" * len(header),
             header, "-" * len(header)]
    for rec in records:
        cfg = rec.get("config") or {}
        for run in rec.get("runs") or []:
            st = run.get("steps") or {}
            ess = run.get("ess") or {}
            verdict, worst = mb.gate_verdict(ess)
            lines.append(
                ("{0:<14} {1:>7} {2:>6} {3:>9} {4:>7} {5:>6} {6:>6} {7:>16} {8:>9} {9:>9} "
                 "{10:>9} {11:>12} {12:>10} {13:>9}").format(
                    str(rec.get("label"))[:14],
                    str(cfg.get("max_tree_depth")),
                    fmt(cfg.get("target_accept"), "{0:.2f}"),
                    fmt(st.get("mean"), "{0:.1f}"),
                    fmt(st.get("max_treedepth_rate"), "{0:.3f}"),
                    fmt(run.get("n_divergences"), "{0:d}"),
                    verdict, worst or "n/a",
                    fmt(ess.get("r_hat_max"), "{0:.4f}"),
                    fmt(ess.get("ess_bulk_min"), "{0:.4g}"),
                    fmt(ess.get("ess_bulk_median"), "{0:.4g}"),
                    fmt(run.get("ess_bulk_median_per_1e6step"), "{0:.4g}"),
                    fmt(run.get("ess_bulk_median_per_sec"), "{0:.4g}"),
                    fmt(run.get("seconds"), "{0:.1f}")))
    lines.append("-" * len(header))
    first = records[0]
    p, c = first.get("panel") or {}, first.get("config") or {}
    lines.append("panel: n_stock={0} n_obs={1} n_factor={2} / chains={3} tune={4} draws={5} "
                 "stamp={6}".format(p.get("n_stock"), p.get("n_obs"), p.get("n_factor"),
                                    c.get("chains"), c.get("tune"), c.get("draws_list"),
                                    c.get("panel_stamp")))
    lines.append("primary metric = ESS/1e6step (time-free; local us/step drifts 2.4x by hour)")
    lines.append("gate = persist_allowed(): per-variable r_hat p99 <= {0} (#611). "
                 "gate_worst names the variable that decides the verdict. A row labelled "
                 "`r_hat_max` predates by_param (#608) and falls back to the single global max, "
                 "so its verdict is the OLD, stricter one".format(mb.MONTHLY_RHAT_THRESHOLD))
    lines.append("NOTE: r_hat_max is NOT the gate quantity (#613). It is a max over ~50k params, "
                 "so it always rises with scale: the production run had r_hat_max 1.1242 yet "
                 "PASSes. Read `gate`, not r_hat_max.")
    lines.append("also watch ess_MIN (median can look healthy while the min collapses: "
                 "md=8 gave ess_med 821.6 with ess_min 3.55 / r_hat 1.6347)")
    note = regime_note(records)
    if note:
        lines.append(note)
    lines.append("=" * len(header))
    return chr(10).join(lines)


def regime_note(records: list) -> str:
    """**このパネルが #540 の対象 regime に居るか**を判定して警告を返す（居るなら空文字）。

    #540 の前提は「全 draw が `max_tree_depth` の上限に張り付いている」こと。**上限が最も緩い
    セル**（＝現行設定に相当）の `td_rate` が 1.000 を割っていたら、その軌道は U ターンで自然に
    止まっており、上限はそもそも律速ではない——そこで測った順位は本番へ移らない。

    2026-08-25 に実際に踏んだ: 実データを 250銘柄へ間引くと `select_shared_factors` が 5因子
    しか選ばず、md=9/10 の `td_rate` が 0.000（軌道長 255 で自然停止）になった。表は何事も
    無かったように並ぶので、**警告が無ければ間違った regime の数字をそのまま ADR へ書いていた**。
    """
    worst = None
    for rec in records:
        for run in rec.get("runs") or []:
            st = run.get("steps") or {}
            cap, rate = st.get("cap_steps"), st.get("max_treedepth_rate")
            if cap is None or rate is None:
                continue
            if worst is None or cap > worst[0]:
                worst = (cap, rate, rec.get("label"))
    if worst is None or worst[1] >= 1.0:
        return ""
    return ("WARNING: 最も緩い上限のセル（{0} / cap={1} 歩）で td_rate={2:.3f} < 1.000 ＝ 軌道は "
            "U ターンで自然停止しており上限は律速ではない。**このパネルは #540 が対象とする "
            "「上限に張り付く」regime に居ない**＝ここでの順位は本番へ移らない（銘柄数・因子数を "
            "上げて td_rate が 1.000 へ戻る規模で測り直すこと）").format(worst[2], worst[0], worst[1])


# ---- 規模依存（#664）------------------------------------------------------------------
#
# 収束ゲート（変数別 × `r_hat` p99）の余裕が**銘柄数とともに縮むか**を測るビュー。#609 の3案
# （alpha だけ別閾値／MCSE／p95）はどれも「縮む」を前提にしているが、#612 の時点で健全な
# run は同一規模（3,837銘柄）の2点しかなく、その向きは一度も測られていなかった。

# 本番の run 間差（ADR-0002 #612 節）。9/06 と 9/11 の `alpha` p99（1.0463 → 1.0317）＝同一規模・
# seed 固定・並走なしの2回の差。**seed 間の幅がこれと同程度なら、1点ずつの比較は幅に埋もれる**。
PROD_RUN_TO_RUN_ALPHA = 0.0146

# 本番の規模（外挿の参考点）。`mb_20260911T051941Z` の `alpha` の個数＝銘柄数。
PROD_N_STOCK = 3837

# 傾きの信頼区間（事前に固定した判定規則。データを見てから水準を選ばない）。
SCALE_CI_LEVEL = 0.95


def _config_signature(rec: dict) -> tuple:
    """銘柄数と seed **以外**の条件。これが違う record を1本の傾きへ混ぜない。"""
    cfg = rec.get("config") or {}
    md = cfg.get("max_tree_depth")
    return (rec.get("mode"), cfg.get("chains"), cfg.get("tune"),
            tuple(cfg.get("draws_list") or ()), cfg.get("target_accept"),
            tuple(md) if isinstance(md, list) else md)


def scale_points(records: list) -> list:
    """record から (銘柄数, seed, 変数, p99) の点を取り出す（純関数）。

    `healthy` は `n_divergences == 0`。発散した run は**推移に混ぜない**が、点としては返して
    表に印付きで出す（`macro_beta_gate_history` と同じ扱い＝落とすと何点あったかが見えない）。
    `by_param` を持たない run（#608 以前・`--no-ess`）は点を作らない。
    """
    points = []
    for rec in records:
        panel, cfg = rec.get("panel") or {}, rec.get("config") or {}
        for run in rec.get("runs") or []:
            by_param = (run.get("ess") or {}).get("by_param") or {}
            n_div = run.get("n_divergences")
            for var, g in by_param.items():
                if not isinstance(g, dict) or g.get("r_hat_p99") is None:
                    continue
                points.append({"signature": _config_signature(rec),
                               "n_stock": panel.get("n_stock"), "seed": cfg.get("seed"),
                               "var": var, "p99": float(g["r_hat_p99"]),
                               "n_params": g.get("n"), "n_divergences": n_div,
                               "healthy": n_div == 0, "label": rec.get("label")})
    return points


def scale_fit(points: list, level: float = SCALE_CI_LEVEL) -> dict:
    """1変数ぶんの点へ `p99 = a + b·ln(n_stock)` を当て、傾きの信頼区間と判定を返す（純関数）。

    健全な点だけを使う。判定は**事前に固定**: CI が 0 をまたげば `NOT DETECTED`、下端が正なら
    `INCREASING`（規模とともに悪化）、上端が負なら `DECREASING`。seed 違いの点は独立な反復
    として扱う（同じパネルで chain の乱数だけが違う）。
    """
    use = [p for p in points if p["healthy"] and p["n_stock"]]
    ns = sorted({p["n_stock"] for p in use})
    out = {"k": len(use), "n_values": ns, "slope": None, "intercept": None,
           "ci": None, "verdict": "n/a"}
    if len(use) < 3 or len(ns) < 2:
        return out
    x = np.log(np.array([p["n_stock"] for p in use], dtype=float))
    y = np.array([p["p99"] for p in use], dtype=float)
    b, a = np.polyfit(x, y, 1)
    out["slope"], out["intercept"] = float(b), float(a)
    dof = len(use) - 2
    sxx = float(np.sum((x - x.mean()) ** 2))
    if dof < 1 or sxx <= 0.0:
        return out
    from scipy import stats

    resid = y - (a + b * x)
    se = float(np.sqrt(float(np.sum(resid ** 2)) / dof / sxx))
    half = float(stats.t.ppf(0.5 + level / 2.0, dof)) * se
    lo, hi = float(b) - half, float(b) + half
    out["se"] = se
    out["ci"] = (lo, hi)
    out["verdict"] = ("INCREASING" if lo > 0 else "DECREASING" if hi < 0 else "NOT DETECTED")
    return out


def scale_table(records: list) -> str:
    """#664 の規模依存の表: 銘柄数 × seed の p99、seed 間の幅、傾きと判定、本番規模への外挿。"""
    points = scale_points(records)
    if not points:
        return ("規模の表を作れる run がありません（by_param を持つ run が無い。"
                "--no-ess で測ったか、#608 以前の JSONL）")
    th = mb.MONTHLY_RHAT_THRESHOLD
    lines = []
    for sig in sorted({p["signature"] for p in points}, key=str):
        pts = [p for p in points if p["signature"] == sig]
        mode, chains, tune, draws, ta, md = sig
        lines += ["=" * 78,
                  "r_hat p99 vs n_stock (#664)  mode={0} chains={1} tune={2} draws={3} "
                  "ta={4} md={5}".format(mode, chains, tune, list(draws), ta, md),
                  "=" * 78]

        # 1) セルごと（本番と同じ合否つき）。
        lines.append("{0:<26} {1:>7} {2:>5} {3:>6} {4:>6} {5:>18} {6:>10} {7:>10} {8:>12}".format(
            "label", "n_stock", "seed", "n_div", "gate", "gate_worst",
            "alpha", "beta", "mu_universe"))
        for rec in records:
            if _config_signature(rec) != sig:
                continue
            for run in rec.get("runs") or []:
                ess = run.get("ess") or {}
                by_param = ess.get("by_param") or {}
                if not by_param:
                    continue
                verdict, worst = mb.gate_verdict(ess)
                vals = [fmt((by_param.get(v) or {}).get("r_hat_p99"), "{0:.4f}")
                        for v in ("alpha", "beta", "mu_universe")]
                lines.append("{0:<26} {1:>7} {2:>5} {3:>6} {4:>6} {5:>18} {6:>10} {7:>10} "
                             "{8:>12}".format(
                                 str(rec.get("label"))[:26], (rec.get("panel") or {}).get("n_stock"),
                                 fmt((rec.get("config") or {}).get("seed"), "{0}"),
                                 fmt(run.get("n_divergences"), "{0}"), verdict, worst or "n/a",
                                 *vals))

        # 2) 変数ごと: 銘柄数別の seed 間の幅と、傾き。
        for var in sorted({p["var"] for p in pts}):
            vp = [p for p in pts if p["var"] == var]
            lines += ["-" * 78, "[{0}] r_hat p99 by n_stock (healthy runs only)".format(var)]
            for n in sorted({p["n_stock"] for p in vp if p["n_stock"]}):
                at = [p for p in vp if p["n_stock"] == n]
                ok = [p["p99"] for p in at if p["healthy"]]
                bad = len(at) - len(ok)
                lines.append("  n={0:<6} k={1}  p99={2}  spread(max-min)={3}{4}".format(
                    n, len(ok), ", ".join("{0:.4f}".format(v) for v in ok) or "n/a",
                    fmt(max(ok) - min(ok) if len(ok) >= 2 else None, "{0:.4f}"),
                    "  [excluded {0} run(s) with divergences]".format(bad) if bad else ""))
            fit = scale_fit(vp)
            if fit["slope"] is None:
                lines.append("  slope: n/a（健全な点が3未満、または銘柄数が1種類）")
                continue
            ci = fit.get("ci")
            lines.append("  slope per ln(n) = {0:+.5f}  ({1:+.5f} per doubling)  {2:.0%} CI {3}  "
                         "-> {4}".format(
                             fit["slope"], fit["slope"] * np.log(2.0), SCALE_CI_LEVEL,
                             "n/a" if ci is None else "[{0:+.5f}, {1:+.5f}]".format(*ci),
                             fit["verdict"]))
            pred = fit["intercept"] + fit["slope"] * np.log(PROD_N_STOCK)
            lines.append("  extrapolated p99 at n={0} = {1:.4f}  (margin to {2} = {3:+.4f})  "
                         "[reference only: synth geometry != production]".format(
                             PROD_N_STOCK, pred, th, th - pred))

        lines += ["-" * 78,
                  "verdict rule (fixed before measuring): {0:.0%} CI of the slope crosses 0 -> "
                  "NOT DETECTED / lower bound > 0 -> INCREASING (worse with scale) / "
                  "upper bound < 0 -> DECREASING".format(SCALE_CI_LEVEL),
                  "production run-to-run difference of alpha p99 (9/06 vs 9/11, same n={0}) = "
                  "{1:.4f}: compare it with the seed spread above".format(
                      PROD_N_STOCK, PROD_RUN_TO_RUN_ALPHA),
                  "mu_universe has 12 params at every scale = the control: a slope there means the "
                  "posterior geometry changed (step size etc.), not the order statistic",
                  "gate = persist_allowed(): per-variable r_hat p99 <= {0} (#611). production "
                  "history: python -m scripts.macro_beta_gate_history".format(th)]
    lines.append("=" * 78)
    return chr(10).join(lines)


VIEWS = {"cost": cost_table, "ess": ess_table, "scale": scale_table}


def main() -> None:
    ap = argparse.ArgumentParser(description="bench_macro_beta の JSONL を1枚の表へ")
    ap.add_argument("--inputs", nargs="+", required=True, help="JSONL（glob 可）")
    ap.add_argument("--view", choices=sorted(VIEWS), default="cost",
                    help="cost=1歩の実費（#512・draws 2点以上が要る） / ess=統計効率（#540） / "
                         "scale=r_hat p99 の規模依存（#664）")
    args = ap.parse_args()

    records = load(args.inputs)
    if not records:
        raise SystemExit("入力が空です")
    print(VIEWS[args.view](records))


if __name__ == "__main__":
    main()
