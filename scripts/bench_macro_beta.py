"""計測: macro_beta（PyMC/NUTS 階層マクロ・ベータ）の所要をローカルと GHA で比較する（Issue #512）。

背景
----
月次バッチの `macro_beta` は GHA 実績 116分（run 30698937879・3,791銘柄）に対し、ローカルは
**741.5分でも未完走**（2026-08-21・手動停止）。同一パラメータ・同一規模・同一 pin で 6.4倍以上
遅い。#530（ADR-0040）でステップ予算 180分を入れたため、原因が解けるまで毎月 exit=124 で落ちる。

Issue #512 は「pytensor が g++ 不在で Python フォールバック」という仮説で起票されたが、本番経路は
`--nuts-sampler numpyro` ＝ サンプリング本体は JAX/XLA が回しており、`g++ not detected` の効き所と
時間が溶けている場所が一致しない。**したがって本スクリプトの成果物は「どこに時間が溶けているか」の
実測**であって高速化ではない（#500 で症状から原因を推測して遠回りした前例を踏まない）。

2つのモード
-----------
- `--mode synth`: 決定的な合成パネル（seed 固定）。**DB を一切触らない**ので Egress ゼロ・
  `FINAPP_DB_TARGET` 非依存で、ローカルと GHA で**完全に同一のデータ・同一の事後幾何**になる。
  ローカル↔Linux ランナーの A/B はこちらで取る（規模やデータ世代の言い訳が入らない）。
- `--mode real`: 本番パネルを間引いて本番幾何での steps/draw を測り、フル規模へ外挿する。
  ローカル正本を read-only で読むだけ（persist 系は呼ばない）。

測る量
------
1. 段階別所要（panel / model build / sample）
2. `draws` だけ2点振って線形回帰 → **固定費（コンパイル＋warmup）と 1 draw の限界費**を分離
3. `sample_stats` から **leapfrog 歩数**・step_size・発散・max treedepth 到達率
4. 導出値: us/leapfrog-step、steps/draw、n_obs で正規化した us/step/obs
5. 環境指紋（jax/デバイス数/x64/pytensor.cxx/floatX/CPU/メモリ/XLA_FLAGS）
6. **統計効率**（#540）: `beta` の ESS_bulk（min / p10 / median）と r_hat_max を生値で。
   主指標は **ESS / leapfrog 歩** ＝ 時間を含まない量

軌道長を測るときの指標（Issue #540）
-----------------------------------
`max_tree_depth` を下げれば1 draw は安くなるが軌道が切られて ESS が落ちる。**wall time で比べては
いけない**——ローカルの us/step は時間帯で 2.4倍振れる（GOTCHAS）ので、数時間かかる格子を所要で
並べるとドリフトがそのまま格子の差に化ける。

そこで主指標は **`ESS_bulk / total_leapfrog_steps`（ESS/歩）** とする。
`ESS/秒 = (ESS/歩) × (歩/秒)` で、`歩/秒` はマシンとパネルの性質であって `max_tree_depth` の
関数ではない。時間を含まない ESS/歩 で順位を付ければドリフトが順位に混入しない。`ESS/秒` は
本番所要の見積りに要るので従指標として併記する。

ESS は **`beta` に対して**取る（`alpha` / `mu_universe` も混ぜる）。本番ゲート `persist_allowed` の
r_hat は `beta` に対して較正された値であり、`beta_raw` で代用すると黙って緩くなる（ADR-0002 の
#541 節）。なお #541 以降 `beta` は `pm.Deterministic` ではなく posterior に載っていないので、
`az.ess(idata, var_names=["beta"])` は動かない——自由 RV から再構成する（`diagnose`）。

実行例（必ず -m 形式・feedback_scripts_dir_needs_module_invocation）
--------------------------------------------------------------------
    # A: ローカル基準（本番経路と同じ設定）
    python -m scripts.bench_macro_beta --mode synth --label A-local --out .logs/bench.jsonl

    # C1: 1チェーンの素の throughput / C2: デバイス強制なし / C3: スレッド上限
    python -m scripts.bench_macro_beta --mode synth --chains 1 --label C1 --out .logs/bench.jsonl
    python -m scripts.bench_macro_beta --mode synth --no-force-devices --chain-method sequential --label C2 --out .logs/bench.jsonl
    python -m scripts.bench_macro_beta --mode synth --threads 1 --label C3 --out .logs/bench.jsonl

    # D: 実データ幾何での外挿（ローカルのみ）
    python -m scripts.bench_macro_beta --mode real --n-stock 250 --tune 800 --draws 200,800 --label D-real --out .logs/bench.jsonl

    # E: 軌道長の1セル（#540）。格子は scripts/grid_macro_beta.py が回す
    python -m scripts.bench_macro_beta --mode real --n-stock 250 --tune 800 --draws 400 \
        --max-tree-depth 8 --probe-draws 0 --label md8 --out .logs/bench_540.jsonl
"""
from __future__ import annotations

import sys

# Windows cp932 コンソールでの記号クラッシュ回避（feedback_windows_cp932_stdout_symbols）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # 古い Python / 非対応ストリームでは無視
        pass

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import numpy as np  # noqa: E402

logger = logging.getLogger("bench_macro_beta")

# 合成パネルの既定形状。本番（n_stock=3795 / n_sector=34 / n_factor=12 / n_obs=90449 ＝
# 銘柄あたり約24観測）と同じ「形」を保ったまま銘柄数だけ縮める。形が違うと事後幾何が変わり、
# 測った us/step をフル規模へ外挿できない。
DEFAULT_N_SECTOR = 34
DEFAULT_OBS_PER_STOCK = 24
MAX_TREEDEPTH_STEPS = 1023          # numpyro 既定 max_tree_depth=10 ＝ 2**10 - 1 歩

# フル規模の既知の事実（Issue #512 で GHA ログとローカルログから実測突合済み）。外挿の突き合わせ先。
FULL_SCALE = {"n_stock": 3795, "n_sector": 34, "n_factor": 12, "n_obs": 90449,
              "draws": 800, "tune": 800, "chains": 2}
GHA_FULL_MINUTES = 116.0                # run 30698937879（2026-08-01・3,791銘柄）
LOCAL_FULL_MINUTES_INCOMPLETE = 741.5   # 2026-08-21・未完走（下限）


# ---- 純粋部分（MCMC 不要＝テストで縛れる）------------------------------------------

def synth_panel(n_stock: int, n_factor: int, obs_per_stock: int,
                n_sector: int, seed: int) -> dict:
    """決定的な合成パネルを作る（同 seed なら OS を跨いでも同一配列）。

    build_hierarchical_model が期待する形をそのまま満たす: 観測粒度の `stock_idx`、
    銘柄粒度の `sector_idx`、標準化済みマクロ `macro`、被説明変数 `returns`。
    真値は二層階層（universe -> sector -> stock）から生成する＝モデルが仮定する幾何と
    整合し、**縮小しても病的な事後にならない**（A/B の比が幾何差で汚れない）。
    """
    rng = np.random.default_rng(seed)
    n_sector = max(1, min(n_sector, n_stock))
    n_obs = n_stock * obs_per_stock

    sector_idx = np.arange(n_stock, dtype=int) % n_sector
    stock_idx = np.repeat(np.arange(n_stock, dtype=int), obs_per_stock)
    macro = rng.standard_normal((n_obs, n_factor))

    mu_universe = rng.normal(0.0, 0.3, size=n_factor)
    mu_sector = mu_universe + rng.normal(0.0, 0.2, size=(n_sector, n_factor))
    beta = mu_sector[sector_idx] + rng.normal(0.0, 0.15, size=(n_stock, n_factor))
    alpha = rng.normal(0.0, 0.1, size=n_stock)

    mu_obs = alpha[stock_idx] + (beta[stock_idx] * macro).sum(axis=-1)
    returns = mu_obs + rng.normal(0.0, 0.05, size=n_obs)

    return {"returns": returns, "macro": macro, "stock_idx": stock_idx,
            "sector_idx": sector_idx, "n_stock": n_stock,
            "n_sector": int(sector_idx.max()) + 1, "n_factor": n_factor, "n_obs": n_obs}


def two_point_fit(points: list) -> dict:
    """(draws, seconds) の点列から「固定費」と「1 draw の限界費」を分離する。

    固定費 = JAX のトレース/コンパイル ＋ warmup（tune は全点で同一なので切片に入る）。
    2点なら厳密解、3点以上は最小二乗。draws が全て同じなら分離できないので None を返す。
    """
    if len(points) < 2:
        return {"fixed_sec": None, "per_draw_sec": None}
    xs = np.array([float(p[0]) for p in points], dtype=float)
    ys = np.array([float(p[1]) for p in points], dtype=float)
    if float(xs.max() - xs.min()) <= 0.0:
        return {"fixed_sec": None, "per_draw_sec": None}
    slope, intercept = np.polyfit(xs, ys, 1)
    return {"fixed_sec": float(intercept), "per_draw_sec": float(slope)}


def extract_steps(stats: dict) -> dict:
    """sample_stats から leapfrog 歩数を取り出す（実装差で名前が違うので順に探す）。

    `n_steps` / `num_steps` はそのまま歩数。無ければ `tree_depth` から 2**depth - 1 で復元する
    （numpyro/PyMC のどちらの命名で来ても測れるようにしておく＝名前が変わっても黙って欠測しない）。
    """
    for name in ("n_steps", "num_steps"):
        if name in stats:
            return {"steps": np.asarray(stats[name], dtype=float).ravel(), "source": name}
    for name in ("tree_depth", "treedepth"):
        if name in stats:
            depth = np.asarray(stats[name], dtype=float).ravel()
            return {"steps": np.power(2.0, depth) - 1.0, "source": name}
    return {"steps": np.array([], dtype=float), "source": "none"}


def pick_best(repeats: list) -> dict:
    """同一条件の反復から**最速**を採る（共有デスクトップでの計測に必須）。

    外乱（他プロセスの CPU / キャッシュ圧・周波数低下）は必ず「遅い側」へ出るので、最小値が
    最も素の実行コストに近い。平均や中央値は外乱を混ぜ込む。実測 2026-08-24: 同一設定
    （chains=2・n_stock=250）の限界費が 0.838 と 3.235 s/draw に割れ、**同じ仕事量
    （steps/draw=1023 固定）に対し CPU 時間まで伸びていた**＝ 1回の観測では比較できない。
    ばらつき自体も残す（大きければその測定は信用しない材料になる）。
    """
    best = dict(min(repeats, key=lambda r: r["seconds"]))
    secs = [r["seconds"] for r in repeats]
    best["repeats"] = len(repeats)
    best["seconds_all"] = secs
    best["seconds_spread"] = (max(secs) / min(secs)) if min(secs) > 0 else None
    return best


def treedepth_cap_steps(max_tree_depth=None) -> int:
    """その設定で1 draw が踏みうる最大 leapfrog 歩数（= 2**depth - 1）。

    `max_tree_depth` を下げたのに 1023 を基準に到達率を測ると**常に 0.000** になり、
    「張り付いていない」と読めてしまう（#540 の観測対象そのものが見えなくなる）。
    タプル `(warmup, sampling)` のときは **sampling 側**を採る——`sample_stats` は
    warmup を含まないので、比較すべき上限は draws 側の上限。
    """
    if max_tree_depth is None:
        return MAX_TREEDEPTH_STEPS
    depth = max_tree_depth[1] if isinstance(max_tree_depth, tuple) else int(max_tree_depth)
    return 2 ** int(depth) - 1


def summarize_steps(steps: np.ndarray, cap_steps: int = MAX_TREEDEPTH_STEPS) -> dict:
    """歩数配列の要約。空なら全て None（「測れなかった」を 0 と区別する）。

    **`max_treedepth_rate` は「上限で切られた率」ではない**（#600・2026-09-05）。NUTS は木を
    倍々に伸ばして U ターンで止めるので、**深さ d で自然停止した軌道の歩数もちょうど
    2**d - 1 になる**——`steps >= cap_steps` はその2つを区別できない。1500銘柄で
    `max_tree_depth` を 10 → 11 へ上げて実測すると、歩数は mean も max も 1023 のまま
    （cap 2047 に対して率は 0.000）で `step_size`・ESS・r_hat が**ビット単位で同一**だった
    ＝上限は律速ではなく、1023 は深さ 10 の U ターンだった。#512 以来の「1023 に 100%
    張り付いている＝毎 draw が構造的に最大コストを払っている」という読みは誤りである。

    率が 1.000 のときに「切られている」と言えるのは、**上限を1つ上げて歩数が伸びることを
    確かめた場合だけ**。numpyro の `sample_stats` に木の深さそのものが出るならそちらを見ること。
    """
    if steps.size == 0:
        return {"mean": None, "p50": None, "p90": None, "max": None,
                "max_treedepth_rate": None, "cap_steps": int(cap_steps)}
    return {"mean": float(np.mean(steps)), "p50": float(np.percentile(steps, 50)),
            "p90": float(np.percentile(steps, 90)), "max": float(np.max(steps)),
            "max_treedepth_rate": float(np.mean(steps >= cap_steps)),
            "cap_steps": int(cap_steps)}


def regime_check(runs: list) -> dict:
    """run 間で NUTS が同じレジームに居るかを見る（違えば比較が成立しない）。

    本番（tune=800）は全 draw が max treedepth に張り付く（steps/draw=1023・発散0）。
    warmup を切り詰めると適応が終わらず、**同じ設定のはずの run が別レジームへ落ちる**——
    実測 2026-08-25 の tune=25 では steps/draw が 1023 → 63 まで動き発散が 78 出て、
    所要の回帰が負の傾きになった。歩数の比が開いていたら、その測定は捨てる。
    """
    means = [r["steps"]["mean"] for r in runs
             if (r.get("steps") or {}).get("mean")]
    divs = [int(r.get("n_divergences") or 0) for r in runs]
    if len(means) < 2:
        return {"ok": None, "steps_ratio": None,
                "n_divergences_max": max(divs) if divs else None}
    ratio = float(max(means) / min(means))
    return {"ok": bool(ratio <= 1.2 and max(divs) == 0), "steps_ratio": ratio,
            "n_divergences_max": int(max(divs))}


def parse_max_tree_depth(text):
    """`--max-tree-depth` の文字列を numpyro が受ける形へ（#540）。

    実体は `macro_beta_inference.parse_max_tree_depth`。**本番と bench で解釈を分けない**
    ——分けると「格子で測った条件」と「本番で回る条件」が静かにずれる。
    """
    from macro_beta_inference import parse_max_tree_depth as _parse
    return _parse(text)


def sampler_kwargs(nuts_sampler, max_tree_depth=None, chain_method=None) -> dict:
    """`pm.sample` へ渡す追加 kwargs（軌道長 ＋ bench 専用の chain_method）。

    軌道長の載せ方は本番と同一実装（`macro_beta_inference.nuts_depth_kwargs`）を使う。
    `chain_method` は計測用のレバーなので bench 側でだけ足す。
    """
    from macro_beta_inference import nuts_depth_kwargs

    out: dict = dict(nuts_depth_kwargs(nuts_sampler, max_tree_depth))
    if chain_method:
        nested = dict(out.get("nuts_sampler_kwargs") or {})
        nested["chain_method"] = chain_method
        out["nuts_sampler_kwargs"] = nested
    return out


def diagnose(idata, sector_idx, edinet_codes=None, factor_names=None) -> dict:
    """`beta` の ESS_bulk / ESS_tail / r_hat を**生値の分布として**取る（#540）。

    本番ゲートと同じ量を測るため、`macro_beta_inference.summarize_diagnostics` と同一の経路を通る:
    `beta` は #541 以降 posterior に載っていないので自由 RV から銘柄チャンクごとに再構成し、
    `az.summary(..., kind="diagnostics", round_to="none")` を掛ける。**丸めた表示で判断しない**
    （#356: 既定の az.summary は r_hat を小数2桁・ess を整数へ丸める）。

    min だけでなく p10 / median も返す理由: `ess_bulk_min` は 3,000 パラメータ
    （250銘柄 × 12因子）の最小順序統計でノイズが大きく、格子の**順位付け**には向かない。
    ゲート較正値としての min は残しつつ、順位は median で見る（判断は生値の表で行う）。

    極値の位置（#600）
    ------------------
    `ess_bulk_argmin` / `r_hat_argmax` に「その値を出している母数」も付ける。位置の解決は
    `macro_beta_inference.locate_extreme` / `pick_extreme` / `annotate_extreme` が唯一の実装で、
    **ここへ書き写さない**——本番と bench で極値の指し方がずれたら、格子で見た母数と本番で
    起きている母数が別物になる（`parse_max_tree_depth` を共有しているのと同じ理由）。
    """
    import arviz as az

    from macro_beta_inference import (BETA_CHUNK_STOCKS, _reconstruct_beta_chunk,
                                      _reconstruct_mu_sector, annotate_extreme, locate_extreme,
                                      pick_extreme)

    post = idata.posterior
    summ = az.summary(idata, var_names=["alpha", "mu_universe"], kind="diagnostics",
                      round_to="none")
    ess_bulk = [np.asarray(summ["ess_bulk"], dtype=float)]
    ess_tail = [np.asarray(summ["ess_tail"], dtype=float)]
    r_hat = [np.asarray(summ["r_hat"], dtype=float)]
    ess_argmin = locate_extreme(summ["ess_bulk"], summ.index, "min")
    r_hat_argmax = locate_extreme(summ["r_hat"], summ.index, "max")

    n_stock = post.sizes["stock"]
    mu_sector = _reconstruct_mu_sector(post)
    for lo in range(0, n_stock, BETA_CHUNK_STOCKS):
        hi = min(lo + BETA_CHUNK_STOCKS, n_stock)
        chunk = _reconstruct_beta_chunk(post, sector_idx, lo, hi, mu_sector=mu_sector)
        csumm = az.summary(az.from_dict(posterior={"beta": chunk}), kind="diagnostics",
                           round_to="none")
        ess_bulk.append(np.asarray(csumm["ess_bulk"], dtype=float))
        ess_tail.append(np.asarray(csumm["ess_tail"], dtype=float))
        r_hat.append(np.asarray(csumm["r_hat"], dtype=float))
        # lo を足してチャンク内ローカル index を全体へ直す（忘れると値は正しいままラベルがずれる）。
        ess_argmin = pick_extreme(
            ess_argmin, locate_extreme(csumm["ess_bulk"], csumm.index, "min", lo), "min")
        r_hat_argmax = pick_extreme(
            r_hat_argmax, locate_extreme(csumm["r_hat"], csumm.index, "max", lo), "max")

    eb = np.concatenate(ess_bulk)
    et = np.concatenate(ess_tail)
    rh = np.concatenate(r_hat)
    return {"r_hat_max": float(np.nanmax(rh)),
            "ess_bulk_min": float(np.nanmin(eb)),
            "ess_bulk_p10": float(np.nanpercentile(eb, 10)),
            "ess_bulk_median": float(np.nanpercentile(eb, 50)),
            "ess_tail_min": float(np.nanmin(et)),
            "n_params": int(eb.size),
            "ess_bulk_argmin": annotate_extreme(ess_argmin, edinet_codes, factor_names),
            "r_hat_argmax": annotate_extreme(r_hat_argmax, edinet_codes, factor_names)}


def ess_efficiency(ess: dict | None, total_steps, seconds) -> dict:
    """ESS を「1 leapfrog 歩あたり」「1秒あたり」へ正規化する（#540 の主指標・従指標）。

    測れなかったものは **None**（0 と区別する）。ESS/歩 は時間を含まないのでマシンの
    時間帯ドリフト（2.4倍）に汚されない＝格子の順位はこちらで付ける。
    """
    out = {"ess_bulk_median_per_1e6step": None, "ess_bulk_min_per_1e6step": None,
           "ess_bulk_median_per_sec": None, "ess_bulk_min_per_sec": None}
    if not ess:
        return out
    med, mn = ess.get("ess_bulk_median"), ess.get("ess_bulk_min")
    if total_steps:
        out["ess_bulk_median_per_1e6step"] = float(med) / float(total_steps) * 1e6
        out["ess_bulk_min_per_1e6step"] = float(mn) / float(total_steps) * 1e6
    if seconds and seconds > 0:
        out["ess_bulk_median_per_sec"] = float(med) / float(seconds)
        out["ess_bulk_min_per_sec"] = float(mn) / float(seconds)
    return out


def predict_full_minutes(per_draw_sec, n_obs_bench: int) -> dict:
    """縮小規模の限界費からフル規模の所要を外挿する。

    仮定は2つ、いずれも出力へ明記する: ①1反復のコストは n_obs に比例 ②tune 1反復は
    draw 1反復と同コスト（NUTS は warmup でも同じ leapfrog を踏む）。steps/draw が規模で
    変わればずれるので、**外挿値は当たりを付けるための量**であって実測の代わりではない。
    """
    if not per_draw_sec or n_obs_bench <= 0:
        return {"minutes": None, "assumptions": "per_draw_sec または n_obs が無効"}
    scale = FULL_SCALE["n_obs"] / float(n_obs_bench)
    iters = FULL_SCALE["draws"] + FULL_SCALE["tune"]
    minutes = per_draw_sec * scale * iters / 60.0
    return {"minutes": float(minutes), "n_obs_scale": float(scale), "iters": int(iters),
            "assumptions": "cost は n_obs へ比例 / tune 1反復 = draw 1反復 / steps per draw は規模で不変",
            "gha_full_minutes": GHA_FULL_MINUTES,
            "local_full_minutes_incomplete": LOCAL_FULL_MINUTES_INCOMPLETE}


def peak_rss_mb():
    """プロセスのピーク常駐メモリ[MB]。取れなければ None。

    本番規模の posterior は `beta` / `beta_raw`（いずれも n_stock x n_factor）が draws x chains
    ぶん保存されるため GB 級になる。**この PC の空きメモリは 2GB 前後**しかないので、
    「1歩あたりのコスト」とは別に常駐サイズ自体が律速になりうる。所要と一緒に残す。

    実装は `sysmem` が唯一の源（**ここへ書き写さない**）。2026-09-01 に月次バッチの heartbeat
    でも同じ計測が要ることが分かり、ctypes の重複を作る前に切り出した。
    """
    import sysmem
    return sysmem.peak_rss_mb()


def env_fingerprint() -> dict:
    """実行環境の指紋。**比較の前提が同じかを後から検算できる形で残す**（#512 の突合で必要になった）。"""
    fp = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "xla_flags": os.environ.get("XLA_FLAGS", ""),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS", ""),
    }
    # 物理メモリは**比較の前提そのもの**。GHA（2コア7GB）とローカルを所要で並べたとき、
    # 空きが枯れていればページアウトが所要に化ける。docstring は「CPU/メモリ」と言いながら
    # メモリを残していなかったため、#512 の「6.4倍遅い」は原因を切り分けられずに残った。
    import sysmem
    fp["mem_total_mb"] = sysmem.total_mb()
    fp["mem_avail_mb"] = sysmem.available_mb()
    try:
        import jax
        fp["jax"] = jax.__version__
        fp["jax_devices"] = [str(d) for d in jax.devices()]
        fp["jax_local_device_count"] = int(jax.local_device_count())
        try:
            fp["jax_enable_x64"] = bool(jax.config.read("jax_enable_x64"))
        except Exception:  # 版によって read() が無い（属性側へ退避）
            fp["jax_enable_x64"] = bool(getattr(jax.config, "jax_enable_x64", None))
    except Exception as exc:
        fp["jax_error"] = str(exc)
    try:
        import pytensor
        fp["pytensor"] = pytensor.__version__
        fp["pytensor_cxx"] = pytensor.config.cxx
        fp["pytensor_floatX"] = pytensor.config.floatX
    except Exception as exc:
        fp["pytensor_error"] = str(exc)
    try:
        import pymc
        fp["pymc"] = pymc.__version__
    except Exception as exc:
        fp["pymc_error"] = str(exc)
    return fp


def apply_thread_limits(threads: int) -> None:
    """XLA/BLAS のスレッド数を上限で縛る（**jax の import より前に呼ぶこと**）。

    6コアに対しデバイス強制 2 でスレッドが過剰購読されている疑い（実測 3.86コア使用）を
    切り分けるための操作。0 以下なら何もしない。
    """
    if threads <= 0:
        return
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS"):
        os.environ[var] = str(threads)
    extra = "--xla_cpu_multi_thread_eigen=false intra_op_parallelism_threads=" + str(threads)
    current = os.environ.get("XLA_FLAGS", "")
    os.environ["XLA_FLAGS"] = (current + " " + extra).strip()


# ---- パネル取得 ---------------------------------------------------------------------

def load_real_panel(n_stock: int, seed: int, stamp: str | None = None) -> dict:
    """本番パネル（read-only）を間引き、因子選択まで済ませて返す。

    キャッシュキーに**日付印**を入れる: 世代を持たないキャッシュは、データが伸びても黙って
    旧世代を返す（#454 / #456 の実例）。日を跨いだら必ず取り直す。

    `stamp`（#540）: 日付印を外から固定する。格子測定は数時間かかり**日付を跨ぐ**ので、
    既定のままだと途中のセルだけ別キーでパネルを取り直す＝全セルが同一パネルを見る保証が消える
    （比較の前提が壊れているのに、出力は何事も無かったように並ぶ）。ドライバが1つの stamp を
    全セルへ配ることで、世代を**明示的に**固定する。
    """
    from scripts._cache import cached
    from scripts.experiment_pooled_rhat import build_real_panel, subsample_panel

    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d")
    panel = cached("macro_beta_panel_" + stamp, build_real_panel)
    if n_stock > 0:
        panel = subsample_panel(panel, n_stock, seed=seed)
    returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names = panel

    from macro_beta_inference import select_shared_factors
    sel = select_shared_factors(macro, returns, factor_names, max_features=min(12, macro.shape[1]))
    if not sel:
        raise SystemExit("select_shared_factors: 有効なマクロ因子が選ばれませんでした。")
    return {"returns": np.asarray(returns), "macro": macro[:, sel], "stock_idx": stock_idx,
            "sector_idx": sector_idx, "n_stock": len(edinet_codes),
            "n_sector": len(sector_names), "n_factor": len(sel), "n_obs": len(returns),
            "selected": [factor_names[i] for i in sel],
            # 極値がどの銘柄で起きているかを診断に載せるため（#600）。synth は持たない。
            "edinet_codes": list(edinet_codes)}


# ---- 計測本体 -----------------------------------------------------------------------

def run_sample(model, draws: int, tune: int, chains: int, target_accept: float,
               seed: int, nuts_sampler, init, chain_method,
               max_tree_depth=None, sector_idx=None,
               edinet_codes=None, factor_names=None) -> dict:
    """1回の pm.sample を計測する。返すのは所要と sample_stats の要約（＋要求されれば ESS）。

    `sector_idx` を渡した run だけ `diagnose` を掛ける（#540）。probe には掛けない——
    診断は本番規模で分単位かかるので、**サンプリングの wall とは別に `diag_sec` で測る**
    （所要へ混ぜると us/step が汚れる）。
    """
    import pymc as pm

    kwargs: dict = dict(draws=draws, tune=tune, target_accept=target_accept,
                        random_seed=seed, chains=chains, progressbar=False)
    if nuts_sampler:
        kwargs["nuts_sampler"] = nuts_sampler
    if init:
        kwargs["init"] = init
    kwargs.update(sampler_kwargs(nuts_sampler, max_tree_depth, chain_method))

    # 経過だけでは「並列で速い」と「待っているだけ」を区別できない。**CPU 時間も測る**
    # （process_time は全スレッドの合計）。cpu/wall が 1 に近ければ実質1コアしか使えておらず、
    # コア数を超えて伸びていれば競合でCPUを焼いているだけ＝どちらも経過時間からは見えない。
    started = time.monotonic()
    cpu_started = time.process_time()
    with model:
        idata = pm.sample(**kwargs)
    elapsed = time.monotonic() - started
    cpu_elapsed = time.process_time() - cpu_started

    stats = {}
    for name in idata.sample_stats.data_vars:
        stats[str(name)] = idata.sample_stats[name].values
    found = extract_steps(stats)
    steps = summarize_steps(found["steps"], cap_steps=treedepth_cap_steps(max_tree_depth))
    divergences = None
    if "diverging" in stats:
        divergences = int(np.sum(np.asarray(stats["diverging"], dtype=bool)))
    step_size = None
    if "step_size" in stats:
        step_size = float(np.mean(np.asarray(stats["step_size"], dtype=float)))

    total_steps = float(np.sum(found["steps"])) if found["steps"].size else None

    ess = None
    diag_sec = None
    if sector_idx is not None:
        diag_started = time.monotonic()
        ess = diagnose(idata, sector_idx, edinet_codes=edinet_codes, factor_names=factor_names)
        diag_sec = time.monotonic() - diag_started

    return {"draws": draws, "seconds": elapsed, "cpu_seconds": cpu_elapsed,
            "cpu_per_wall": (cpu_elapsed / elapsed) if elapsed > 0 else None,
            "steps_source": found["source"], "steps": steps, "n_divergences": divergences,
            "step_size_mean": step_size,
            "total_steps": total_steps,
            "ess": ess, "diag_sec": diag_sec,
            **ess_efficiency(ess, total_steps, elapsed)}


def format_report(record: dict) -> str:
    """人が読む表（ASCII のみ・cp932 でも壊れない）。"""
    lines = []
    bar = "=" * 78
    lines.append(bar)
    lines.append("bench_macro_beta: {0}  (mode={1})".format(record["label"], record["mode"]))
    lines.append(bar)
    p = record["panel"]
    lines.append("panel   : n_stock={0} n_sector={1} n_factor={2} n_obs={3}".format(
        p["n_stock"], p["n_sector"], p["n_factor"], p["n_obs"]))
    c = record["config"]
    lines.append("config  : chains={0} tune={1} draws={2} target_accept={3} sampler={4}".format(
        c["chains"], c["tune"], c["draws_list"], c["target_accept"], c["nuts_sampler"]))
    lines.append("          init={0} chain_method={1} force_devices={2} threads={3}".format(
        c["init"], c["chain_method"], c["force_devices"], c["threads"]))
    lines.append("          max_tree_depth={0} (None = numpyro default 10)".format(
        c.get("max_tree_depth")))
    lines.append("-" * 78)
    lines.append("{0:>8} {1:>10} {2:>10} {3:>12} {4:>12} {5:>8}".format(
        "draws", "sec", "cpu/wall", "steps/draw", "max_td_rate", "n_div"))
    for r in record["runs"]:
        st = r["steps"]
        lines.append("{0:>8} {1:>10.1f} {2:>10} {3:>12} {4:>12} {5:>8}".format(
            r["draws"], r["seconds"],
            "n/a" if r.get("cpu_per_wall") is None else "{0:.2f}".format(r["cpu_per_wall"]),
            "n/a" if st["mean"] is None else "{0:.1f}".format(st["mean"]),
            "n/a" if st["max_treedepth_rate"] is None else "{0:.3f}".format(st["max_treedepth_rate"]),
            "n/a" if r["n_divergences"] is None else r["n_divergences"]))
    # ESS の表（#540）。**主指標は ESS/1e6step**（時間を含まない＝時間帯ドリフトに汚されない）。
    # ESS/sec は本番所要の見積り用の従指標。判断は生値で行うので有効数字を落とさない。
    if any(r.get("ess") for r in record["runs"]):
        lines.append("-" * 78)
        lines.append("{0:>8} {1:>10} {2:>10} {3:>10} {4:>10} {5:>12} {6:>10}".format(
            "draws", "ess_min", "ess_p10", "ess_med", "r_hat_max", "ESS/1e6step", "ESS/sec"))
        for r in record["runs"]:
            e = r.get("ess")
            if not e:
                continue
            lines.append("{0:>8} {1:>10.4g} {2:>10.4g} {3:>10.4g} {4:>10.4f} {5:>12.4g} {6:>10.4g}".format(
                r["draws"], e["ess_bulk_min"], e["ess_bulk_p10"], e["ess_bulk_median"],
                e["r_hat_max"], r.get("ess_bulk_median_per_1e6step") or float("nan"),
                r.get("ess_bulk_median_per_sec") or float("nan")))
        # 極値“を出している母数”（#600）。alpha なら永続化対象ではないのでゲートの見方を
        # 変える余地があり、beta なら macro_beta_loadings そのもの＝対策が変わる。
        for r in record["runs"]:
            e = r.get("ess") or {}
            for key, title in (("ess_bulk_argmin", "ess_min "), ("r_hat_argmax", "r_hat_max")):
                loc = e.get(key)
                if loc:
                    lines.append("  {0} at {1} (edinet={2} factor={3}) = {4:.4g}".format(
                        title, loc.get("label"), loc.get("edinet_code") or "-",
                        loc.get("factor_name") or "-", loc.get("value")))
        diag = [r.get("diag_sec") for r in record["runs"] if r.get("diag_sec")]
        if diag:
            lines.append("          ESS は beta+alpha+mu_universe の生値（n_params={0}）・"
                         "診断所要 {1:.1f}s は上の sec に含めない".format(
                             next(r["ess"]["n_params"] for r in record["runs"] if r.get("ess")),
                             max(diag)))
    spreads = [r.get("seconds_spread") for r in record["runs"] if r.get("seconds_spread")]
    if spreads:
        lines.append("repeats : {0} 回/点・最速を採用（ばらつき max/min = {1}）".format(
            record["runs"][0].get("repeats"),
            ", ".join("{0:.2f}".format(s) for s in spreads)))
    regime = record.get("regime") or {}
    if regime.get("ok") is False:
        lines.append("WARNING : run 間で NUTS のレジームが違う"
                     "（steps/draw 比 {0:.2f}・発散 max {1}）＝この測定は比較に使えない。"
                     "tune を伸ばすこと".format(regime.get("steps_ratio") or 0.0,
                                                regime.get("n_divergences_max")))
    lines.append("-" * 78)
    probe = record.get("probe")
    if probe:
        lines.append("probe   : {0:.1f}s (draws={1}, compile 代を先払い)".format(
            probe["seconds"], probe["draws"]))
    fit = record["fit"]
    if fit["per_draw_sec"] is not None:
        lines.append("fit     : fixed(warmup tune={0})={1:.1f}s  marginal={2:.3f}s/draw".format(
            record["config"]["tune"], fit["fixed_sec"], fit["per_draw_sec"]))
    if record.get("per_step_us") is not None:
        lines.append("through : {0:.1f} us/leapfrog-step ({1:.4f} us/step/obs)".format(
            record["per_step_us"], record["per_step_us_per_obs"]))
    pred = record.get("predicted_full") or {}
    if pred.get("minutes") is not None:
        lines.append("extrapol: full scale {0:.0f} min (GHA {1:.0f} min / local {2:.0f} min unfinished)".format(
            pred["minutes"], pred["gha_full_minutes"], pred["local_full_minutes_incomplete"]))
        lines.append("          assumptions: {0}".format(pred["assumptions"]))
    if record.get("peak_rss_mb"):
        lines.append("memory  : peak RSS {0:.0f}MB".format(record["peak_rss_mb"]))
    lines.append("timing  : panel={0:.1f}s model_build={1:.1f}s sample_total={2:.1f}s".format(
        record["stage_sec"]["panel"], record["stage_sec"]["model_build"],
        record["stage_sec"]["sample_total"]))
    env = record["env"]
    lines.append("env     : jax={0} devices={1} x64={2} cpu_count={3}".format(
        env.get("jax"), env.get("jax_local_device_count"), env.get("jax_enable_x64"),
        env.get("cpu_count")))
    lines.append("          pytensor.cxx={0!r} floatX={1} XLA_FLAGS={2!r}".format(
        env.get("pytensor_cxx"), env.get("pytensor_floatX"), env.get("xla_flags")))
    lines.append(bar)
    return chr(10).join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="macro_beta の所要を段階別に計測する（Issue #512）")
    ap.add_argument("--mode", choices=("synth", "real"), default="synth",
                    help="synth=DB 不使用の合成パネル（GHA A/B 用） / real=本番パネル間引き")
    ap.add_argument("--n-stock", type=int, default=250, help="銘柄数（real では間引き数）")
    ap.add_argument("--n-factor", type=int, default=FULL_SCALE["n_factor"], help="synth のみ")
    ap.add_argument("--n-sector", type=int, default=DEFAULT_N_SECTOR, help="synth のみ")
    ap.add_argument("--obs-per-stock", type=int, default=DEFAULT_OBS_PER_STOCK, help="synth のみ")
    ap.add_argument("--draws", default="50,200",
                    help="カンマ区切りの2点以上（固定費と限界費を分離するため）")
    ap.add_argument("--tune", type=int, default=200)
    ap.add_argument("--repeat", type=int, default=1,
                    help="各 draws 点の反復回数。最速を採る（共有デスクトップの外乱対策）")
    ap.add_argument("--probe-draws", type=int, default=5,
                    help="計測前に1本流してコンパイル代を先に払う draws 数（0 で無効）")
    ap.add_argument("--chains", type=int, default=2)
    ap.add_argument("--target-accept", type=float, default=0.95)
    ap.add_argument("--max-tree-depth", default=None,
                    help="NUTS の軌道長上限（#540）。'8' で一律、'8,10' で warmup だけ 8。"
                         "未指定は numpyro 既定 10（＝現行本番と同一）")
    ap.add_argument("--no-ess", action="store_true",
                    help="ESS 診断を測らない（所要だけ見たいとき。既定は測る）")
    ap.add_argument("--panel-stamp", default=None,
                    help="real モードのパネルキャッシュ日付印（YYYYMMDD）を固定する。"
                         "日を跨ぐ格子で全セルに同一パネルを見せるために使う")
    ap.add_argument("--nuts-sampler", default="numpyro")
    ap.add_argument("--init", default="adapt_diag")
    ap.add_argument("--chain-method", default=None,
                    help="numpyro の chain_method（parallel/sequential/vectorized）。既定は numpyro 既定")
    ap.add_argument("--no-force-devices", action="store_true",
                    help="XLA_FLAGS のホストデバイス強制と set_host_device_count を行わない")
    ap.add_argument("--threads", type=int, default=0, help="0 より大きい値で XLA/BLAS のスレッド数を縛る")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--label", default="bench")
    ap.add_argument("--out", default=None, help="JSONL の追記先（1実行=1行）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    draws_list = [int(x) for x in str(args.draws).split(",") if str(x).strip()]
    if not draws_list:
        raise SystemExit("--draws が空です")
    try:
        max_tree_depth = parse_max_tree_depth(args.max_tree_depth)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    # スレッド制限は jax の import より前（環境変数は初期化時にしか読まれない）。
    apply_thread_limits(args.threads)
    if args.nuts_sampler == "numpyro" and not args.no_force_devices:
        os.environ.setdefault(
            "XLA_FLAGS", "--xla_force_host_platform_device_count=" + str(args.chains))

    t0 = time.monotonic()
    if args.mode == "synth":
        panel = synth_panel(args.n_stock, args.n_factor, args.obs_per_stock,
                            args.n_sector, args.seed)
    else:
        panel = load_real_panel(args.n_stock, args.seed, stamp=args.panel_stamp)
    panel_sec = time.monotonic() - t0
    logger.info("panel: n_stock=%d n_sector=%d n_factor=%d n_obs=%d (%.1fs)",
                panel["n_stock"], panel["n_sector"], panel["n_factor"], panel["n_obs"], panel_sec)

    if args.nuts_sampler == "numpyro" and not args.no_force_devices:
        import numpyro
        numpyro.set_host_device_count(args.chains)

    from macro_beta_inference import build_hierarchical_model
    t0 = time.monotonic()
    model = build_hierarchical_model(
        panel["returns"], panel["macro"], panel["stock_idx"], panel["sector_idx"],
        n_stock=panel["n_stock"], n_sector=panel["n_sector"], n_factor=panel["n_factor"])
    model_sec = time.monotonic() - t0

    # **コンパイルを先に払っておく**。JAX/XLA は同一 tune の実行で compile 済みカーネルを
    # 使い回すため、1点目だけがコンパイル代を負担して2点目が速くなり、回帰の傾き（限界費）が
    # 負に出る（smoke で実測: draws=20 が 15.1s、draws=60 が 7.7s）。probe を1本挟むと
    # 以降の点は同条件になり、切片が warmup(tune) の実費、傾きが 1 draw の実費になる。
    probe = None
    if args.probe_draws > 0:
        logger.info("probe: draws=%d tune=%d（コンパイルを先に払う）", args.probe_draws, args.tune)
        probe = run_sample(model, draws=args.probe_draws, tune=args.tune, chains=args.chains,
                           target_accept=args.target_accept, seed=args.seed,
                           nuts_sampler=args.nuts_sampler, init=args.init,
                           chain_method=args.chain_method, max_tree_depth=max_tree_depth)
        logger.info("  -> probe %.1fs", probe["seconds"])

    # ESS は本番ゲートと同じ `beta` に対して取るので sector_idx が要る（#540・#541）。
    diag_sector_idx = None if args.no_ess else panel["sector_idx"]
    runs = []
    for draws in draws_list:
        repeats = []
        for rep in range(max(1, args.repeat)):
            logger.info("sampling: draws=%d tune=%d chains=%d max_tree_depth=%s (rep %d/%d)",
                        draws, args.tune, args.chains, max_tree_depth,
                        rep + 1, max(1, args.repeat))
            r = run_sample(model, draws=draws, tune=args.tune, chains=args.chains,
                           target_accept=args.target_accept, seed=args.seed,
                           nuts_sampler=args.nuts_sampler, init=args.init,
                           chain_method=args.chain_method, max_tree_depth=max_tree_depth,
                           sector_idx=diag_sector_idx,
                           edinet_codes=panel.get("edinet_codes"),
                           factor_names=panel.get("selected"))
            logger.info("  -> %.1fs cpu/wall=%.2f steps/draw=%s ess_bulk_med=%s", r["seconds"],
                        r["cpu_per_wall"] or 0.0,
                        "n/a" if r["steps"]["mean"] is None else round(r["steps"]["mean"], 1),
                        "n/a" if not r.get("ess") else round(r["ess"]["ess_bulk_median"], 1))
            repeats.append(r)
        runs.append(pick_best(repeats))

    fit = two_point_fit([(r["draws"], r["seconds"]) for r in runs])
    # **歩数を分母にした回帰**。draws を分母にすると、run 間で steps/draw が変われば傾きが
    # 汚染される（実測 n_stock=1000: 一方が 1023 歩・他方が 709.6 歩＝ max treedepth を
    # 抜けた run が混ざった）。総 leapfrog 歩数に対して回帰すれば、傾きがそのまま 1歩の実費。
    step_fit = two_point_fit([(r["total_steps"], r["seconds"]) for r in runs
                              if r.get("total_steps")])
    per_step_us = None
    per_step_us_per_obs = None
    if step_fit.get("per_draw_sec"):     # ここでの "per_draw" は「1 leapfrog 歩」の意味
        per_step_us = step_fit["per_draw_sec"] * 1e6
        per_step_us_per_obs = per_step_us / float(panel["n_obs"])

    record = {
        "label": args.label,
        "mode": args.mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "panel": {k: panel[k] for k in ("n_stock", "n_sector", "n_factor", "n_obs")},
        "config": {"chains": args.chains, "tune": args.tune, "draws_list": draws_list,
                   "target_accept": args.target_accept, "nuts_sampler": args.nuts_sampler,
                   "init": args.init, "chain_method": args.chain_method,
                   "force_devices": not args.no_force_devices, "threads": args.threads,
                   "seed": args.seed, "max_tree_depth": max_tree_depth,
                   "panel_stamp": args.panel_stamp, "probe_draws": args.probe_draws},
        "probe": probe,
        "runs": runs,
        "regime": regime_check(runs),
        "fit": fit,
        "step_fit": step_fit,
        "per_step_us": per_step_us,
        "per_step_us_per_obs": per_step_us_per_obs,
        "predicted_full": predict_full_minutes(fit["per_draw_sec"], panel["n_obs"]),
        "stage_sec": {"panel": panel_sec, "model_build": model_sec,
                      "sample_total": float(sum(r["seconds"] for r in runs))},
        "peak_rss_mb": peak_rss_mb(),
        "env": env_fingerprint(),
    }

    print(format_report(record))
    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + chr(10))
        logger.info("JSONL 追記: %s", args.out)


if __name__ == "__main__":
    main()
