"""計測: NUTS の軌道長（`max_tree_depth`）× `target_accept` の格子を回す（Issue #540）。

背景
----
`macro_beta` は**ローカル・GHA・合成/実データのいずれでも `steps/draw` が 1023
（= 2**10 − 1・numpyro 既定 `max_tree_depth=10` の上限）に 100% 張り付いている**（#512 の実測）。
NUTS は本来 U ターンで軌道を打ち切るので、これは毎 draw が構造的に最大コストを払っている状態。
所要は `steps/draw × 1歩の実費` で決まるため、ここはプラットフォームに依らず効く唯一の大きい
レバーになる（#512 の 6.6倍はマシン側・#541 の 584MB は常駐メモリ側で、いずれも別軸）。

ただし **「上限を下げれば速くなる」ではない**。軌道を切れば 1 draw あたりの実効サンプル（ESS）が
落ちる。だからこの格子の成果物は所要ではなく **統計効率あたりのコスト**であり、結果が
「現状が最良」なら **コード変更0行で ADR-0002 へ棄却を記録して終わる**のが正しい結末になる。

なぜ専用のドライバを置くか（ADR-0041 の教訓）
---------------------------------------------
ADR-0028 の昇格ゲートは #509 と #517 で2回適用されたが、**どちらもアドホックなスクリプトで
実体が残らなかった**——だから ADR-0041 で `scripts/preset_ic_gate.py` として実装を残した。
同じ轍を踏まないため、格子測定も**残るコード**にする。手で 5 回コマンドを打つと、条件の
写し間違い（tune を1セルだけ変えた等）が結果からは見分けられない。

指標（**wall time で比べてはいけない**）
----------------------------------------
主指標は `bench_macro_beta` が出す **ESS_bulk / leapfrog 歩**（`ESS/1e6step`）。
`ESS/秒 = (ESS/歩) × (歩/秒)` で、`歩/秒` はマシンとパネルの性質であって `max_tree_depth` の
関数ではない。ローカルの us/step は**時間帯で 2.4倍振れる**（GOTCHAS）ので、数時間かかる格子を
所要で並べるとドリフトがそのまま格子の差に化ける。`ESS/秒` は本番所要の見積り用の従指標。

設計上の約束
------------
- **1セル1プロセス**。途中で kill されてもそこまでの JSONL が残り、JAX の状態がセル間で混ざらない
- **安い順に回す**。窓が足りなくなったとき、失われるのは高い（＝現状に近い）セルだけで済む
- **全セルへ同一の `--panel-stamp` を配る**。格子は数時間＝日付を跨ぐので、既定のままだと
  途中のセルだけ別キーでパネルを取り直す（比較の前提が壊れても出力は何事も無く並ぶ・#454/#456）
- **子の出力はそのまま流す**（capture しない）。溜め込むと「順調に長い」と「死んだ」が
  区別できない（feedback_capture_output_hides_death・#504/PR#511）

実行例（必ず -m 形式・feedback_scripts_dir_needs_module_invocation）
--------------------------------------------------------------------
    python -m scripts.grid_macro_beta --dry-run
    python -m scripts.grid_macro_beta --mode real --n-stock 250 --tune 800 --draws 400
    python -m scripts.grid_macro_beta --report-only
"""
from __future__ import annotations

import sys

# Windows cp932 コンソールでの記号クラッシュ回避（feedback_windows_cp932_stdout_symbols）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

DEFAULT_OUT = os.path.join(".logs", "bench_540.jsonl")

# Stage 1（#540）: target_accept は本番と同じ 0.95 に固定し、軌道長だけ振る。
# "8,10" は **warmup だけ 8 に切る**案＝draws 側の軌道長を一切変えないので、
# 統計効率を落とさずに総コストだけ落とせる可能性がある（warmup は全 iter の半分）。
DEFAULT_DEPTHS = ("7", "8", "8,10", "9", "10")

# 所要見積りの係数。`.logs/bench_512.jsonl` の synth n_stock=250 / chains=2 実測（A-local）。
# **見積りであって実測ではない**（規模・時間帯で 2.4倍振れる）。順序決めと目安表示にだけ使う。
DEFAULT_US_PER_STEP = 2084.9


def parse_depth_spec(text: str):
    """"8,10" のような1セル指定を `(warmup_depth, sampling_depth)` へ。

    見積りと並べ替えのためだけに使う数値化。`None`/"" は「サンプラー既定」＝10 とみなす
    （**bench へ渡す文字列はそのまま**で、ここで既定値を埋めたりはしない）。
    """
    parts = [p.strip() for p in str(text or "").split(",") if p.strip()]
    if not parts:
        return (10, 10)
    if len(parts) == 1:
        d = int(parts[0])
        return (d, d)
    return (int(parts[0]), int(parts[1]))


def cell_total_steps(depth_spec: str, tune: int, draws: int, chains: int) -> int:
    """そのセルが踏む leapfrog 歩数の上限（warmup ＋ draws・全チェーン合計）。

    上限であって実測ではない。**全 draw が上限に張り付いている**という #512 の観測が
    成り立つ領域なので、上限そのものが良い近似になる（張り付きが外れたセルは、
    bench の `max_treedepth_rate` が 1.000 を割ることで結果から分かる）。
    """
    warm_d, samp_d = parse_depth_spec(depth_spec)
    return chains * (tune * (2 ** warm_d - 1) + draws * (2 ** samp_d - 1))


def build_cells(depths, target_accepts, tune: int, draws: int, chains: int,
                us_per_step: float) -> list[dict]:
    """セル一覧を**安い順**に並べて返す。

    安い順にする理由: 窓が足りなくなったとき失われるのが高いセルだけで済む。高いセル
    （＝現状の md=10）は結果が既に分かっている量に最も近いので、最後に回して損が小さい。
    """
    cells = []
    for ta in target_accepts:
        for d in depths:
            steps = cell_total_steps(d, tune, draws, chains)
            cells.append({
                "label": "md{0}-ta{1}".format(str(d).replace(",", "w"), str(ta).replace(".", "")),
                "max_tree_depth": d,
                "target_accept": ta,
                "est_total_steps": steps,
                "est_minutes": steps * us_per_step / 1e6 / 60.0,
            })
    return sorted(cells, key=lambda c: c["est_total_steps"])


def bench_command(python: str, cell: dict, args) -> list[str]:
    """1セルぶんの `bench_macro_beta` 起動コマンド。

    `--probe-draws 0` は意図的: probe は「固定費と限界費を2点回帰で分離する」ための道具で、
    ここは 1 draws 点しか測らないので不要。tune=800 の warmup を1本余分に払うのは
    セルあたり数十分の純損になる（compile 代は本番も払うので、含めたままの方がむしろ実態に近い）。
    """
    cmd = [python, "-m", "scripts.bench_macro_beta",
           "--mode", args.mode,
           "--n-stock", str(args.n_stock),
           "--chains", str(args.chains),
           "--tune", str(args.tune),
           "--draws", str(args.draws),
           "--target-accept", str(cell["target_accept"]),
           "--max-tree-depth", str(cell["max_tree_depth"]),
           "--probe-draws", "0",
           "--repeat", str(args.repeat),
           "--seed", str(args.seed),
           "--nuts-sampler", args.nuts_sampler,
           "--init", args.init,
           "--label", cell["label"],
           "--out", args.out]
    if args.mode == "real":
        cmd += ["--panel-stamp", args.panel_stamp]
    return cmd


def report(path: str) -> str:
    """JSONL から**生値の表**を組む（ADR へ貼る成果物）。

    表そのものは `scripts.bench_macro_beta_report.ess_table` が持つ——JSONL を読む主体が
    ドライバと後追い集計の2箇所に分かれるのは構わないが、**表の作り方が2実装あると
    「どちらの数字を貼ったか」が後から分からなくなる**。ここは委譲だけする。
    """
    from scripts.bench_macro_beta_report import ess_table, load

    if not os.path.exists(path):
        return "JSONL がまだ無い: " + path
    return ess_table(load([path]))


def main() -> None:
    ap = argparse.ArgumentParser(description="NUTS 軌道長の格子を回す（Issue #540）")
    ap.add_argument("--mode", choices=("real", "synth"), default="real")
    ap.add_argument("--n-stock", type=int, default=250)
    ap.add_argument("--chains", type=int, default=2)
    ap.add_argument("--tune", type=int, default=800,
                    help="**切り詰めないこと**。tune=25 では NUTS が別レジームへ落ちる"
                         "（steps/draw 1023->63・発散78）。本番 regime は 800")
    ap.add_argument("--draws", type=int, default=400)
    ap.add_argument("--depths", nargs="+", default=list(DEFAULT_DEPTHS),
                    help="空白区切りのセル指定。'8,10' は warmup だけ 8 の意（カンマは1セル内）")
    ap.add_argument("--target-accepts", nargs="+", type=float, default=[0.95])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--nuts-sampler", default="numpyro")
    ap.add_argument("--init", default="adapt_diag")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--panel-stamp", default=None,
                    help="real モードのパネル世代（YYYYMMDD）。既定は今日。全セルへ同一値を配る")
    ap.add_argument("--us-per-step", type=float, default=DEFAULT_US_PER_STEP,
                    help="所要見積りの係数（表示と並べ替えにのみ使用）")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true", help="セル一覧と見積りだけ出して何も回さない")
    ap.add_argument("--report-only", action="store_true", help="JSONL から生値の表を出すだけ")
    args = ap.parse_args()

    if args.report_only:
        print(report(args.out))
        return

    args.panel_stamp = args.panel_stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    cells = build_cells(args.depths, args.target_accepts, args.tune, args.draws,
                        args.chains, args.us_per_step)

    total_min = sum(c["est_minutes"] for c in cells)
    print("=" * 78)
    print("grid_macro_beta: {0} cells  mode={1} n_stock={2} chains={3} tune={4} draws={5}".format(
        len(cells), args.mode, args.n_stock, args.chains, args.tune, args.draws))
    print("panel_stamp={0}  out={1}".format(args.panel_stamp, args.out))
    print("-" * 78)
    print("{0:<14} {1:>10} {2:>6} {3:>14} {4:>12}".format(
        "label", "max_depth", "ta", "est_steps", "est_min"))
    for c in cells:
        print("{0:<14} {1:>10} {2:>6} {3:>14,} {4:>12.1f}".format(
            c["label"], c["max_tree_depth"], c["target_accept"],
            c["est_total_steps"], c["est_minutes"]))
    print("-" * 78)
    print("estimated total: {0:.1f} min ({1:.1f} h) at {2:.1f} us/step  "
          "[estimate only: local us/step drifts 2.4x by hour]".format(
              total_min, total_min / 60.0, args.us_per_step))
    print("=" * 78)
    if args.dry_run:
        return

    started = time.monotonic()
    env = dict(os.environ)
    # 子の出力は溜め込まず流す。溜めると途中で死んでも START 行しか残らず「順調に長い」と
    # 区別が付かない（feedback_capture_output_hides_death）。
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    failed = []
    for i, cell in enumerate(cells, 1):
        cmd = bench_command(args.python, cell, args)
        print(chr(10) + "#" * 78)
        print("# cell {0}/{1}: {2}  (est {3:.1f} min, elapsed {4:.1f} min)".format(
            i, len(cells), cell["label"], cell["est_minutes"], (time.monotonic() - started) / 60.0))
        print("# " + " ".join(cmd))
        print("#" * 78, flush=True)
        t0 = time.monotonic()
        rc = subprocess.run(cmd, env=env).returncode
        took = (time.monotonic() - t0) / 60.0
        print("# cell {0} done rc={1} in {2:.1f} min".format(cell["label"], rc, took), flush=True)
        if rc != 0:
            # 1セル落ちても止めない（batch_common と同じ思想＝残りのセルは測れる）。
            failed.append((cell["label"], rc))

    print(chr(10) + report(args.out))
    print(chr(10) + "grid total: {0:.1f} min".format((time.monotonic() - started) / 60.0))
    if failed:
        print("FAILED cells: " + ", ".join("{0}(rc={1})".format(a, b) for a, b in failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
