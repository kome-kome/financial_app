"""計測結果（bench_macro_beta の JSONL）を1枚の表に畳む（Issue #512 / #540）。

なぜ別スクリプトか
------------------
比較の分母は run ごとに違う（銘柄数・tune・draws・チェーン数）。**素の所要を並べても
比較にならない**ので、ここで共通の土俵へ直す:

- **1 leapfrog 歩の実費**（`total_steps` に対する回帰の傾き）。draws を分母にすると、run 間で
  steps/draw が変わった瞬間に傾きが汚染される（実測 n_stock=1000 で 1023 歩と 709.6 歩の
  run が混ざった）
- **観測1件あたり**（us/step/obs）。銘柄数の違う run を横に並べるための正規化

2つのビュー
-----------
- `--view cost`（既定・#512）: 上記のコスト表。**`--draws` を2点以上振った run 用**
  （1点しか無い run は傾きが出ないので us/step が n/a になる）
- `--view ess`（#540）: 統計効率の表。軌道長（`max_tree_depth`）の格子を並べる。
  **主指標は `ESS/1e6step`＝時間を含まない量**——`ESS/秒 = (ESS/歩) × (歩/秒)` で `歩/秒` は
  マシンとパネルの性質であって `max_tree_depth` の関数ではなく、ローカルの us/step は
  **時間帯で 2.4倍振れる**（GOTCHAS）。数時間かかる格子を所要で並べるとドリフトが差に化ける。

どちらのビューも**生値を出す**（丸めた表示で判断しない・#466）。

読む先は `--inputs` で与える JSONL（ローカル実行と GHA アーティファクトの両方）。

実行例（必ず -m 形式）::

    python -m scripts.bench_macro_beta_report --inputs .logs/bench_512.jsonl \\
        .logs/gha_bench/b0/bench-macro-beta/bench_512.jsonl
    python -m scripts.bench_macro_beta_report --view ess --inputs .logs/bench_540.jsonl
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
    """
    if not records:
        return "入力が空です（JSONL がまだ無いか、1行も書かれていない）"
    header = ("{0:<14} {1:>7} {2:>6} {3:>9} {4:>7} {5:>6} {6:>9} {7:>9} {8:>9} {9:>12} "
              "{10:>10} {11:>9}").format(
        "label", "md", "ta", "steps/dr", "td_rate", "n_div", "r_hat_max", "ess_min",
        "ess_med", "ESS/1e6step", "ESS/sec", "sec")
    lines = ["=" * len(header), "bench ESS grid (raw values)", "=" * len(header),
             header, "-" * len(header)]
    for rec in records:
        cfg = rec.get("config") or {}
        for run in rec.get("runs") or []:
            st = run.get("steps") or {}
            ess = run.get("ess") or {}
            lines.append(
                ("{0:<14} {1:>7} {2:>6} {3:>9} {4:>7} {5:>6} {6:>9} {7:>9} {8:>9} {9:>12} "
                 "{10:>10} {11:>9}").format(
                    str(rec.get("label"))[:14],
                    str(cfg.get("max_tree_depth")),
                    fmt(cfg.get("target_accept"), "{0:.2f}"),
                    fmt(st.get("mean"), "{0:.1f}"),
                    fmt(st.get("max_treedepth_rate"), "{0:.3f}"),
                    fmt(run.get("n_divergences"), "{0:d}"),
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
    lines.append("=" * len(header))
    return chr(10).join(lines)


VIEWS = {"cost": cost_table, "ess": ess_table}


def main() -> None:
    ap = argparse.ArgumentParser(description="bench_macro_beta の JSONL を1枚の表へ")
    ap.add_argument("--inputs", nargs="+", required=True, help="JSONL（glob 可）")
    ap.add_argument("--view", choices=sorted(VIEWS), default="cost",
                    help="cost=1歩の実費（#512・draws 2点以上が要る） / ess=統計効率（#540）")
    args = ap.parse_args()

    records = load(args.inputs)
    if not records:
        raise SystemExit("入力が空です")
    print(VIEWS[args.view](records))


if __name__ == "__main__":
    main()
