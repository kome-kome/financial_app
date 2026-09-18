"""計測: NUTS の軌道長（`max_tree_depth`）× `target_accept`（Issue #540）と銘柄数 × seed（Issue #664）の格子を回す。

背景
----
`macro_beta` は**ローカル・GHA・合成/実データのいずれでも `steps/draw` が 1023
（= 2**10 − 1・numpyro 既定 `max_tree_depth=10` の上限）に 100% 張り付いている**（#512 の実測）。
NUTS は本来 U ターンで軌道を打ち切るので、これは毎 draw が構造的に最大コストを払っている状態。
所要は `steps/draw × 1歩の実費` で決まるため、ここはプラットフォームに依らず効く唯一の大きい
レバーになる（#512 の 6.6倍はマシン側・#541 の 584MB は常駐メモリ側で、いずれも別軸）。

**上の前提は 2026-09-05（#600）に反証された。** 1500銘柄で上限を 10 → 11 へ上げて測ると、
歩数は mean も max も 1023 のまま（cap 2047 に対する到達率 0.000）で、`step_size`・ESS・r_hat が
**ビット単位で同一**だった——**1023 は「上限で切られた」のではなく「深さ 10 で U ターンした」**。
NUTS は木を倍々に伸ばすので自然停止した軌道の歩数もちょうど 2**d − 1 になり、
`steps >= cap` はその2つを区別できない（`bench_macro_beta.summarize_steps` の注記を読むこと）。
**軌道長のレバーは残っていない**。この格子は「棄却の実体」として残す。

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

規模の軸（Issue #664）
---------------------
`--n-stock` と `--seed` は複数値を取る。**収束ゲートの余裕（変数別 `r_hat` p99）が銘柄数とともに
縮むか**を測るための軸で、#609 の3案はどれもその向きを前提にしているのに、#612 の時点で健全な
run は同一規模の2点しかなかった。約束は3つ:

- **反復で振るのは sampler の seed だけ**。`--panel-seed` を渡すと全セルが同じパネル生成 seed を
  使う（本番の run 間差＝同じデータで chain の乱数だけが違う、に対応させる）
- **締切の手前で畳む**。日中枠は子へ締切（`FINAPP_STEP_DEADLINE_UTC`）を渡し、過ぎたら猶予なしで
  プロセスツリーごと殺す。bench は完走して初めて JSONL を書くので、殺されたセルは何も残らない。
  だから**見積りが残り時間に入らないセルは始めない**（ADR-0054 と同じ考え方）。1セルも回せずに
  残りがある回は exit 3（「毎日何も進まないのに成功」にしない）
- **`--resume` で済んだセルを飛ばす**。照合は JSONL の record の条件（モード・銘柄数・seed・
  panel seed・chains・tune・draws・target_accept・軌道長・real の stamp）で、ラベルは使わない。
  ESS を測っていない record は済み扱いしない。**所要見積りは同じ銘柄数の実測があればそれを使い**、
  無ければ見積り（`(n/250)^1.6`）に実測で較正した倍率を掛ける（区間ペースの外挿は外れる）

実行例（必ず -m 形式・feedback_scripts_dir_needs_module_invocation）
--------------------------------------------------------------------
    python -m scripts.grid_macro_beta --dry-run
    python -m scripts.grid_macro_beta --mode real --n-stock 250 --tune 800 --draws 400
    python -m scripts.grid_macro_beta --report-only

    # #664: 規模の軸（日中枠の `bench:rhat-scale` と同じ形）
    python -m scripts.grid_macro_beta --mode synth --n-stock 250 500 1000 2000 --seed 0 1 2 \\
        --panel-seed 0 --tune 800 --draws 800 --depths 8,10 --us-per-step 190.2 \\
        --resume --view scale --out .logs/bench_664_scale.jsonl --dry-run
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
import json  # noqa: E402
import os  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

DEFAULT_OUT = os.path.join(".logs", "bench_540.jsonl")

# Stage 1（#540）: target_accept は本番と同じ 0.95 に固定し、軌道長だけ振る。
# "8,10" は **warmup だけ 8 に切る**案＝draws 側の軌道長を一切変えないので、
# 統計効率を落とさずに総コストだけ落とせる可能性がある（warmup は全 iter の半分）。
DEFAULT_DEPTHS = ("7", "8", "8,10", "9", "10")

# 所要見積りの係数。`.logs/bench_512.jsonl` の synth n_stock=250 / chains=2 実測（A-local）。
# **見積りであって実測ではない**（規模・時間帯で 2.4倍振れる）。順序決めと目安表示にだけ使う。
DEFAULT_US_PER_STEP = 2084.9

# ---- 規模の軸（#664）の見積り係数。**すべて見積りであって実測ではない**（セル順と締切判定にだけ使う）
# `--us-per-step` を測った銘柄数。上の 2084.9 も synth 250銘柄の値。
REF_N_STOCK = 250
# 1歩の実費が銘柄数の何乗で伸びるか。real の 1000→1500 銘柄（md 8,10・draws 400）が
# 1985 → 3883 秒＝1.5倍の銘柄で 1.96倍（`.logs/bench_540.jsonl`）から置いた。
SCALE_EXP = 1.6
# 1セルぶんの起動（JAX の import・コンパイル）と診断の見込み。record の段階別所要には入らない。
CELL_OVERHEAD_MIN = 1.0
# 締切判定で見積りに掛ける安全率と余白。外れる向きは遅い側で、外れたら殺されてセルが丸ごと消える。
DEADLINE_SAFETY = 1.25
DEADLINE_MARGIN_MIN = 2.0
# 1セルも回せずに残りがある回の終了コード（「何も進まないのに成功」にしない）。
EXIT_NOTHING_FITS = 3


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


def scale_factor(n_stock: int) -> float:
    """1歩の実費の銘柄数による倍率（`REF_N_STOCK` で 1.0）。見積りであって実測ではない。"""
    return (float(n_stock) / float(REF_N_STOCK)) ** SCALE_EXP


def build_cells(depths, target_accepts, tune: int, draws: int, chains: int,
                us_per_step: float, n_stocks=(REF_N_STOCK,), seeds=(0,)) -> list[dict]:
    """セル一覧を**安い順**に並べて返す（銘柄数 × seed × 軌道長 × target_accept の直積）。

    安い順にする理由: 窓が足りなくなったとき失われるのが高いセルだけで済む。高いセル
    （＝現状の md=10）は結果が既に分かっている量に最も近いので、最後に回して損が小さい。

    ラベルには**振った軸だけ**を付ける（#664）。銘柄数も seed も1値なら従来どおり
    `md8w10-ta095` のまま＝既存の JSONL と表の見え方を変えない。
    """
    vary_n = len(set(n_stocks)) > 1
    vary_seed = len(set(seeds)) > 1
    cells = []
    for n in n_stocks:
        for seed in seeds:
            for ta in target_accepts:
                for d in depths:
                    steps = cell_total_steps(d, tune, draws, chains)
                    label = "md{0}-ta{1}".format(str(d).replace(",", "w"),
                                                 str(ta).replace(".", ""))
                    if vary_n:
                        label += "-n{0:04d}".format(int(n))
                    if vary_seed:
                        label += "-s{0}".format(int(seed))
                    cells.append({
                        "label": label,
                        "max_tree_depth": d,
                        "target_accept": ta,
                        "n_stock": int(n),
                        "seed": int(seed),
                        "est_total_steps": steps,
                        "est_minutes": steps * us_per_step / 1e6 / 60.0 * scale_factor(n),
                    })
    return sorted(cells, key=lambda c: (c["est_minutes"], c["n_stock"], c["seed"]))


def depth_key(value) -> tuple:
    """軌道長の指定を比較できる形 `(warmup, sampling)` へ（セルの文字列と record の値の両方）。

    record には `parse_max_tree_depth` の結果が JSON で入る（int・`[8, 10]`・None）。
    None はサンプラー既定＝10 で、セルの `"10"` と同じ意味になる。
    """
    if isinstance(value, (list, tuple)):
        return (int(value[0]), int(value[1]))
    if isinstance(value, int):
        return (value, value)
    return parse_depth_spec(value)


def cell_key(mode, chains, tune, draws, target_accept, depth, panel_stamp,
             panel_seed, n_stock, seed) -> tuple:
    """再開の照合キー。先頭7つが「銘柄数と seed 以外の条件」（`key[:7]`＝所要の較正に使う）。

    stamp は real のときだけ意味を持つ（synth は DB を触らない）。panel seed 未指定は seed と
    同じ（`bench_macro_beta.panel_seed_of`）。
    """
    return (str(mode), int(chains), int(tune), int(draws), round(float(target_accept), 6),
            depth_key(depth), panel_stamp if mode == "real" else None,
            int(seed if panel_seed is None else panel_seed), int(n_stock), int(seed))


def record_key(rec: dict):
    """record から `cell_key` を組む。1セル1 draws 点の record でなければ None（照合しない）。"""
    cfg = rec.get("config") or {}
    draws = cfg.get("draws_list") or []
    n_stock = (rec.get("panel") or {}).get("n_stock")
    try:
        if len(draws) != 1 or n_stock is None:
            return None
        return cell_key(rec.get("mode"), cfg["chains"], cfg["tune"], draws[0],
                        cfg["target_accept"], cfg.get("max_tree_depth"), cfg.get("panel_stamp"),
                        cfg.get("panel_seed"), n_stock, cfg.get("seed", 0))
    except (KeyError, TypeError, ValueError):
        return None


def record_done(rec: dict) -> bool:
    """済み扱いにしてよいか。ESS を測っていない run は済みにしない（規模の表に点を作れない）。"""
    runs = rec.get("runs") or []
    return bool(runs) and all(r.get("ess") for r in runs)


def done_keys(records: list) -> set:
    return {k for k in (record_key(r) for r in records if record_done(r)) if k is not None}


def record_minutes(rec: dict) -> float:
    """record が実際に使った分数（段階別所要＋診断＋probe＋起動の見込み）。"""
    stage = rec.get("stage_sec") or {}
    sec = sum(float(v) for v in stage.values() if v)
    sec += sum(float(r.get("diag_sec") or 0.0) for r in rec.get("runs") or [])
    sec += float((rec.get("probe") or {}).get("seconds") or 0.0)
    return sec / 60.0 + CELL_OVERHEAD_MIN


def estimate_minutes(cell: dict, records: list) -> float:
    """そのセルの所要見積り[分]。`records` は**銘柄数と seed 以外の条件が同じ** record だけ。

    1. 同じ銘柄数の実測があれば、その最大値（seed で所要は変わらない。遅い側を採る）
    2. 無ければ見積り（`est_minutes`・`(n/250)^1.6`）に、実測済みの銘柄数で較正した倍率
       （実測 / 見積りの中央値）を掛ける——係数そのものがこのマシンで合っている保証は無い
    3. 実測が1つも無ければ見積りのまま
    """
    same_n = [record_minutes(r) for r in records
              if (r.get("panel") or {}).get("n_stock") == cell["n_stock"]]
    if same_n:
        return max(same_n)
    base = cell["est_minutes"] / scale_factor(cell["n_stock"])
    ratios = []
    for r in records:
        n = (r.get("panel") or {}).get("n_stock")
        if n:
            model = base * scale_factor(n) + CELL_OVERHEAD_MIN
            ratios.append(record_minutes(r) / model)
    factor = statistics.median(ratios) if ratios else 1.0
    return (cell["est_minutes"] + CELL_OVERHEAD_MIN) * factor


def fits_before(deadline, now, est_min: float) -> bool:
    """見積り（安全率と余白込み）が締切までに収まるか。締切が無ければ常に True。"""
    if deadline is None:
        return True
    need = timedelta(minutes=est_min * DEADLINE_SAFETY + DEADLINE_MARGIN_MIN)
    return now + need <= deadline


def load_records(path: str) -> list:
    """JSONL を読む。**壊れた行は飛ばす**（殺されたセルの書きかけで再開ごと止めない）。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def bench_command(python: str, cell: dict, args) -> list[str]:
    """1セルぶんの `bench_macro_beta` 起動コマンド。

    `--probe-draws 0` は意図的: probe は「固定費と限界費を2点回帰で分離する」ための道具で、
    ここは 1 draws 点しか測らないので不要。tune=800 の warmup を1本余分に払うのは
    セルあたり数十分の純損になる（compile 代は本番も払うので、含めたままの方がむしろ実態に近い）。
    """
    cmd = [python, "-m", "scripts.bench_macro_beta",
           "--mode", args.mode,
           "--n-stock", str(cell.get("n_stock", args.n_stock)),
           "--chains", str(args.chains),
           "--tune", str(args.tune),
           "--draws", str(args.draws),
           "--target-accept", str(cell["target_accept"]),
           "--max-tree-depth", str(cell["max_tree_depth"]),
           "--probe-draws", "0",
           "--repeat", str(args.repeat),
           "--seed", str(cell.get("seed", args.seed)),
           "--nuts-sampler", args.nuts_sampler,
           "--init", args.init,
           "--label", cell["label"],
           "--out", args.out]
    if args.mode == "real":
        cmd += ["--panel-stamp", args.panel_stamp]
    if getattr(args, "panel_seed", None) is not None:
        cmd += ["--panel-seed", str(args.panel_seed)]
    return cmd


def report(path: str, view: str = "ess") -> str:
    """JSONL から**生値の表**を組む（ADR へ貼る成果物）。

    表そのものは `scripts.bench_macro_beta_report` が持つ（`ess` / `scale`）——JSONL を読む主体が
    ドライバと後追い集計の2箇所に分かれるのは構わないが、**表の作り方が2実装あると
    「どちらの数字を貼ったか」が後から分からなくなる**。ここは委譲だけする。
    """
    from scripts.bench_macro_beta_report import VIEWS

    if not os.path.exists(path):
        return "JSONL がまだ無い: " + path
    return VIEWS[view](load_records(path))


def key_of(cell: dict, args) -> tuple:
    """セルの照合キー（`record_key` と同じ形）。"""
    return cell_key(args.mode, args.chains, args.tune, args.draws, cell["target_accept"],
                    cell["max_tree_depth"], args.panel_stamp, args.panel_seed,
                    cell["n_stock"], cell["seed"])


def main() -> None:
    ap = argparse.ArgumentParser(description="NUTS 軌道長（#540）・銘柄数（#664）の格子を回す")
    ap.add_argument("--mode", choices=("real", "synth"), default="real")
    ap.add_argument("--n-stock", type=int, nargs="+", default=[REF_N_STOCK],
                    help="空白区切りで複数可（#664 の規模の軸）")
    ap.add_argument("--chains", type=int, default=2)
    ap.add_argument("--tune", type=int, default=800,
                    help="**切り詰めないこと**。tune=25 では NUTS が別レジームへ落ちる"
                         "（steps/draw 1023->63・発散78）。本番 regime は 800")
    ap.add_argument("--draws", type=int, default=400)
    ap.add_argument("--depths", nargs="+", default=list(DEFAULT_DEPTHS),
                    help="空白区切りのセル指定。'8,10' は warmup だけ 8 の意（カンマは1セル内）")
    ap.add_argument("--target-accepts", nargs="+", type=float, default=[0.95])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seed", type=int, nargs="+", default=[0],
                    help="sampler の seed。空白区切りで複数可（#664 の反復）")
    ap.add_argument("--panel-seed", type=int, default=None,
                    help="全セル共通のパネル生成（synth）・間引き（real）の seed。"
                         "未指定は各セルの --seed と同じ（#664: 反復で chain の乱数だけを振る）")
    ap.add_argument("--nuts-sampler", default="numpyro")
    ap.add_argument("--init", default="adapt_diag")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--panel-stamp", default=None,
                    help="real モードのパネル世代（YYYYMMDD）。既定は今日。全セルへ同一値を配る。"
                         "日をまたいで --resume するなら固定すること（違う stamp は別セル扱い）")
    ap.add_argument("--us-per-step", type=float, default=DEFAULT_US_PER_STEP,
                    help="所要見積りの係数（{0} 銘柄での値。並べ替えと締切判定にのみ使用）".format(
                        REF_N_STOCK))
    ap.add_argument("--resume", action="store_true",
                    help="--out の JSONL に済んだセル（同じ条件・ESS あり）を飛ばす（#664）")
    ap.add_argument("--view", choices=("ess", "scale"), default="ess",
                    help="最後に出す表。ess=統計効率（#540） / scale=r_hat p99 の規模依存（#664）")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true", help="セル一覧と見積りだけ出して何も回さない")
    ap.add_argument("--report-only", action="store_true", help="JSONL から生値の表を出すだけ")
    args = ap.parse_args()

    if args.report_only:
        print(report(args.out, args.view))
        return

    args.panel_stamp = args.panel_stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    cells = build_cells(args.depths, args.target_accepts, args.tune, args.draws,
                        args.chains, args.us_per_step, n_stocks=args.n_stock, seeds=args.seed)

    records = load_records(args.out)
    done = done_keys(records) if args.resume else set()
    pending = [c for c in cells if key_of(c, args) not in done]

    def same_config(cell):
        k = key_of(cell, args)[:7]
        return [r for r in load_records(args.out) if (record_key(r) or ())[:7] == k]

    total_min = sum(estimate_minutes(c, same_config(c)) for c in pending)
    print("=" * 78)
    print("grid_macro_beta: {0} cells ({1} pending)  mode={2} n_stock={3} seed={4} "
          "panel_seed={5}".format(len(cells), len(pending), args.mode, args.n_stock, args.seed,
                                  args.panel_seed))
    print("chains={0} tune={1} draws={2}  panel_stamp={3}  out={4}".format(
        args.chains, args.tune, args.draws, args.panel_stamp, args.out))
    print("-" * 78)
    print("{0:<24} {1:>10} {2:>6} {3:>7} {4:>5} {5:>14} {6:>10} {7:>6}".format(
        "label", "max_depth", "ta", "n_stock", "seed", "est_steps", "est_min", "state"))
    for c in cells:
        is_done = key_of(c, args) in done
        print("{0:<24} {1:>10} {2:>6} {3:>7} {4:>5} {5:>14,} {6:>10.1f} {7:>6}".format(
            c["label"], c["max_tree_depth"], c["target_accept"], c["n_stock"], c["seed"],
            c["est_total_steps"], estimate_minutes(c, same_config(c)),
            "done" if is_done else "-"))
    print("-" * 78)
    print("estimated total (pending): {0:.1f} min ({1:.1f} h) at {2:.1f} us/step x (n/{3})^{4}  "
          "[estimate only: calibrated by measured cells when present]".format(
              total_min, total_min / 60.0, args.us_per_step, REF_N_STOCK, SCALE_EXP))
    print("=" * 78)
    if args.dry_run:
        return

    from hyperparameter_search import resolve_deadline

    deadline = resolve_deadline()
    if deadline is not None:
        print("deadline: {0}（見積り x{1} + {2}分 が入らないセルは始めない）".format(
            deadline.isoformat(), DEADLINE_SAFETY, DEADLINE_MARGIN_MIN), flush=True)

    started = time.monotonic()
    env = dict(os.environ)
    # 子の出力は溜め込まず流す。溜めると途中で死んでも START 行しか残らず「順調に長い」と
    # 区別が付かない（feedback_capture_output_hides_death）。
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    failed = []
    ran = 0
    remaining: list = []
    for i, cell in enumerate(pending, 1):
        est = estimate_minutes(cell, same_config(cell))
        if not fits_before(deadline, datetime.now(timezone.utc), est):
            # 安い順なので、これ以降のセルも入らない。殺されてセルが丸ごと消えるより、
            # 始めずに残して次の起動へ回す（--resume が続きから拾う）。
            remaining = pending[i - 1:]
            print("# stop before cell {0} (est {1:.1f} min): deadline {2} に入らない".format(
                cell["label"], est, deadline.isoformat()), flush=True)
            break
        cmd = bench_command(args.python, cell, args)
        print(chr(10) + "#" * 78)
        print("# cell {0}/{1}: {2}  (est {3:.1f} min, elapsed {4:.1f} min)".format(
            i, len(pending), cell["label"], est, (time.monotonic() - started) / 60.0))
        print("# " + " ".join(cmd))
        print("#" * 78, flush=True)
        t0 = time.monotonic()
        rc = subprocess.run(cmd, env=env).returncode
        took = (time.monotonic() - t0) / 60.0
        ran += 1
        print("# cell {0} done rc={1} in {2:.1f} min".format(cell["label"], rc, took), flush=True)
        if rc != 0:
            # 1セル落ちても止めない（batch_common と同じ思想＝残りのセルは測れる）。
            failed.append((cell["label"], rc))

    print(chr(10) + report(args.out, args.view))
    print(chr(10) + "grid total: {0:.1f} min ({1} cell(s) run)".format(
        (time.monotonic() - started) / 60.0, ran))
    if remaining:
        print("未完 {0} セル: {1}。同じコマンドを --resume 付きでもう一度回せば続きから回る".format(
            len(remaining), ", ".join(c["label"] for c in remaining)))
    elif not pending:
        print("全セル済み（--resume が JSONL の record と照合した）")
    if failed:
        print("FAILED cells: " + ", ".join("{0}(rc={1})".format(a, b) for a, b in failed))
        raise SystemExit(1)
    if remaining and ran == 0:
        # 残りの最小セルすら締切に入らない＝何日回しても進まない。成功扱いにしない。
        print("1セルも回せなかった（残りの最小セルが締切に入らない）")
        raise SystemExit(EXIT_NOTHING_FITS)


if __name__ == "__main__":
    main()
