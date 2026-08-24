"""計測結果（bench_macro_beta の JSONL）を1枚の表に畳む（Issue #512）。

なぜ別スクリプトか
------------------
比較の分母は run ごとに違う（銘柄数・tune・draws・チェーン数）。**素の所要を並べても
比較にならない**ので、ここで共通の土俵へ直す:

- **1 leapfrog 歩の実費**（`total_steps` に対する回帰の傾き）。draws を分母にすると、run 間で
  steps/draw が変わった瞬間に傾きが汚染される（実測 n_stock=1000 で 1023 歩と 709.6 歩の
  run が混ざった）
- **観測1件あたり**（us/step/obs）。銘柄数の違う run を横に並べるための正規化

読む先は `--inputs` で与える JSONL（ローカル実行と GHA アーティファクトの両方）。

実行例（必ず -m 形式）::

    python -m scripts.bench_macro_beta_report --inputs .logs/bench_512.jsonl \\
        .logs/gha_bench/b0/bench-macro-beta/bench_512.jsonl
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


def main() -> None:
    ap = argparse.ArgumentParser(description="bench_macro_beta の JSONL を1枚の表へ")
    ap.add_argument("--inputs", nargs="+", required=True, help="JSONL（glob 可）")
    args = ap.parse_args()

    records = load(args.inputs)
    if not records:
        raise SystemExit("入力が空です")

    header = "{0:<22} {1:>7} {2:>7} {3:>7} {4:>6} {5:>10} {6:>11} {7:>10} {8:>9}".format(
        "label", "n_stock", "n_obs", "chains", "cpus", "us/step", "us/step/obs",
        "steps/draw", "cpu/wall")
    print(header)
    print("-" * len(header))
    for rec in records:
        slope = per_step_seconds(rec)
        panel = rec.get("panel", {})
        cfg = rec.get("config", {})
        env = rec.get("env", {})
        last = (rec.get("runs") or [{}])[-1]
        steps_mean = (last.get("steps") or {}).get("mean")
        print("{0:<22} {1:>7} {2:>7} {3:>7} {4:>6} {5:>10} {6:>11} {7:>10} {8:>9}".format(
            str(rec.get("label"))[:22], panel.get("n_stock"), panel.get("n_obs"),
            cfg.get("chains"), env.get("cpu_count"),
            "n/a" if slope is None else "{0:.1f}".format(slope * 1e6),
            "n/a" if slope is None else "{0:.4f}".format(slope * 1e6 / panel["n_obs"]),
            "n/a" if steps_mean is None else "{0:.0f}".format(steps_mean),
            "n/a" if last.get("cpu_per_wall") is None else "{0:.2f}".format(last["cpu_per_wall"])))


if __name__ == "__main__":
    main()
