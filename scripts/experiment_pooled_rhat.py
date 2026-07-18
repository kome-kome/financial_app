"""実験: 複数の 2チェーン run をプールして r_hat が下がるか検証する（Issue #341 フォロー）。

背景
----
PR#350（#341）で macro_beta_inference.py の月次 cron 化にあたり、収束ゲートの相互作用が判明した。
chains=2 のランナーでは r_hat_max が構造的に ~1.02 で頭打ちになり（本番の唯一 persist 済み
run 2026-07-04 も r_hat_max=1.02・n_divergences=0）、ADR-0002 の strict ゲート（<1.01）を
毎回超過する。暫定対応として cron のゲート閾値を 1.05 へ緩めた（--r-hat-threshold）。

本スクリプトは「2チェーンの run を複数回まわし、生サンプルをプールして 2×N チェーン相当の
r_hat を計算し直せば 1.01 を切れるか」を検証する。これは pm.sample(chains=2N) を別ジョブに
分割するのと統計的に等価（same data / same model / 別 seed が条件）で、chains=4 が1ランナーで
OOM クラッシュした問題も「2チェーンずつ分散」で回避できる。

判定
----
- プール最終（8チェーン）の r_hat_max < 1.01 かつ傾向が単調減少 → プールで収束基準を満たせる。
  #341 のゲートを 1.01 へ戻せる本命改善（別Issue）へ。
- プールしても頭打ち → 1.02 は本物。現行の緩和閾値 1.05（PR#350）が妥当だったと確定。

read-only / 本番無改修
----------------------
production コード（macro_beta_inference.py 等）は無改修で、その公開部品を組み直して
生 idata を保持する（run_inference は idata を破棄するため使わない）。DB は build_panel の
SELECT のみ（persist 系は一切呼ばない・接続先は本番 Supabase＝feedback_local_scripts_hit_production_db）。

実行例（必ず -m 形式・feedback_scripts_dir_needs_module_invocation）
------------------------------------------------------------------
    # Stage 1: ローカル smoke（DB 不使用の小合成データ・数分）
    python -m scripts.experiment_pooled_rhat --synthetic --k-runs 4 --draws 50 --tune 50

    # Stage 2: 縮小規模（実データ 500 銘柄・GitHub Actions 1ジョブ）
    python -m scripts.experiment_pooled_rhat --n-stocks 500 --k-runs 4 \
        --draws 800 --tune 800 --nuts-sampler numpyro --init adapt_diag

    # Stage 3: 本番規模（全銘柄・要 fan-out 化。本スクリプトの in-process 実行は非推奨＝時間超過）
"""
from __future__ import annotations

import sys

# Windows cp932 コンソールでの記号クラッシュ・ログ文字化け回避（feedback_windows_cp932_stdout_symbols）。
# stdout（レポート）だけでなく stderr（logging 出力）も UTF-8 化する（tee で捕捉する日本語ログの化け対策）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # 古い Python / 非対応ストリームでは無視
        pass

import argparse
import logging

import numpy as np

logger = logging.getLogger("experiment_pooled_rhat")

STRICT_GATE = 1.01          # ADR-0002 の strict 収束基準
CHAINS_PER_RUN = 2          # 1ランナーに収まる固定値（chains=4 は OOM クラッシュ実測）


# ── プール集計（純ロジック・MCMC 不要＝高速テスト可能）────────────────────────────

def pool_and_diagnose(idatas: list) -> list[dict]:
    """k=1..K で先頭 k 個の idata を chain 次元で連結し、r_hat 等の傾向表を返す。

    az.concat(..., dim="chain") は reset_dim=True（既定）で chain 座標を 0..2k-1 へ振り直す
    ため、別 run（それぞれ chain=[0,1]）を素直に積み上げて 2k チェーンにできる。診断計算は
    production の summarize_diagnostics をそのまま再利用する（r_hat_max/ess_bulk_min/n_divergences）。
    """
    import arviz as az

    from macro_beta_inference import summarize_diagnostics

    rows: list[dict] = []
    for k in range(1, len(idatas) + 1):
        pooled = idatas[0] if k == 1 else az.concat(idatas[:k], dim="chain")
        diag = summarize_diagnostics(pooled)
        rows.append({
            "n_runs": k,
            "n_chains": int(pooled.posterior.sizes["chain"]),
            "r_hat_max": diag["r_hat_max"],
            "ess_bulk_min": diag["ess_bulk_min"],
            "n_divergences": diag["n_divergences"],
        })
    return rows


# ── 縮小規模用パネル間引き（Stage 2・純 numpy）─────────────────────────────────────

def subsample_panel(panel: tuple, n_stocks: int, seed: int = 0) -> tuple:
    """build_panel の返り値を n_stocks 銘柄へランダムに絞り、index を連番へ remap する。

    stock_idx は観測粒度、sector_idx は銘柄粒度（len == n_stock）。両方を連番へ振り直し、
    消えたセクターも詰めて build_hierarchical_model の n_sector/n_factor 前提に合わせる。
    n_stocks<=0 または全銘柄数以上なら panel をそのまま返す。
    """
    returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names = panel
    n_total = len(edinet_codes)
    if n_stocks <= 0 or n_stocks >= n_total:
        return panel

    rng = np.random.default_rng(seed)
    keep = sorted(rng.choice(n_total, size=n_stocks, replace=False).tolist())
    keep_set = set(keep)

    obs_mask = np.array([int(s) in keep_set for s in stock_idx], dtype=bool)
    new_returns = np.asarray(returns)[obs_mask]
    new_macro = np.asarray(macro)[obs_mask]

    old_to_new = {old: new for new, old in enumerate(keep)}
    new_stock_idx = np.array([old_to_new[int(s)] for s in np.asarray(stock_idx)[obs_mask]], dtype=int)

    kept_sector = np.asarray(sector_idx)[keep]                  # 銘柄粒度→kept 銘柄のセクター
    uniq_sectors = sorted(set(int(s) for s in kept_sector))
    sec_old_to_new = {old: new for new, old in enumerate(uniq_sectors)}
    new_sector_idx = np.array([sec_old_to_new[int(s)] for s in kept_sector], dtype=int)
    new_sector_names = [sector_names[s] for s in uniq_sectors]
    new_edinet = [edinet_codes[i] for i in keep]

    return (new_returns, new_macro, new_stock_idx, new_sector_idx,
            factor_names, new_edinet, new_sector_names)


# ── K 回サンプリング（同一 model・同一データ・別 seed でプール妥当性を担保）──────────

def run_k_samples(returns, macro_sel, stock_idx, sector_idx, n_stock, n_sector, n_factor,
                  k_runs: int, draws: int, tune: int, target_accept: float,
                  nuts_sampler: str | None, init: str | None, base_seed: int) -> list:
    """モデルを1回構築し、seed を run ごとに変えて chains=2 の pm.sample を K 回まわす。

    same model / same data / 別 seed により、K×2 チェーンは独立した事後サンプルとなり
    プール（chain 連結）が統計的に妥当になる。numpyro のデバイス数はチェーン数に合わせる。
    """
    import pymc as pm

    from macro_beta_inference import build_hierarchical_model

    model = build_hierarchical_model(
        returns, macro_sel, stock_idx, sector_idx,
        n_stock=n_stock, n_sector=n_sector, n_factor=n_factor,
    )

    if nuts_sampler == "numpyro":
        import os

        import numpyro
        os.environ.setdefault("XLA_FLAGS", f"--xla_force_host_platform_device_count={CHAINS_PER_RUN}")
        numpyro.set_host_device_count(CHAINS_PER_RUN)

    idatas = []
    for k in range(k_runs):
        seed = base_seed + k                       # run ごとに別 seed＝独立チェーン
        sample_kwargs: dict = dict(draws=draws, tune=tune, target_accept=target_accept,
                                   random_seed=seed, chains=CHAINS_PER_RUN, progressbar=False)
        if nuts_sampler:
            sample_kwargs["nuts_sampler"] = nuts_sampler
        if init:
            sample_kwargs["init"] = init
        with model:
            idata = pm.sample(**sample_kwargs)
        idatas.append(idata)
        logger.info("run %d/%d 完了（seed=%d）", k + 1, k_runs, seed)
    return idatas


# ── パネル構築（本番 / 合成）───────────────────────────────────────────────────────

def build_real_panel(macro_names: list[str] | None = None) -> tuple:
    """本番 DB から build_panel（read-only）。ロック解放のため build_panel 直後に commit。"""
    from database import SessionLocal

    from macro_beta_inference import build_panel

    db = SessionLocal()
    try:
        panel = build_panel(db, macro_names=macro_names)
        db.commit()   # SELECT トランザクションを解放（run_inference と同じ・書き込みではない）
    finally:
        db.close()
    return panel


def synthetic_inputs(seed: int):
    """DB 不使用の小合成データ（Stage 1 smoke 用）。因子選択は省略し2因子固定。"""
    rng = np.random.default_rng(seed)
    n_stock, n_sector, n_factor, n_obs = 6, 2, 2, 300
    stock_idx = rng.integers(0, n_stock, size=n_obs)
    sector_idx = np.array([i % n_sector for i in range(n_stock)], dtype=int)
    macro = rng.normal(size=(n_obs, n_factor))
    true_beta = rng.normal(size=(n_stock, n_factor)) * 0.3
    returns = np.array([macro[o] @ true_beta[stock_idx[o]] for o in range(n_obs)])
    returns = returns + rng.normal(scale=0.05, size=n_obs)
    selected = [f"f{i}" for i in range(n_factor)]
    return returns, macro, stock_idx, sector_idx, selected, n_stock, n_sector, n_factor


# ── レポート ──────────────────────────────────────────────────────────────────────

def print_report(rows: list[dict], selected: list[str]) -> None:
    print("\n=== pooled r_hat trend (Issue #341 experiment) ===")
    print(f"selected factors: {selected}")
    print(f"{'runs':>5} {'chains':>7} {'r_hat_max':>10} {'ess_bulk_min':>13} {'n_div':>6}")
    for r in rows:
        print(f"{r['n_runs']:>5} {r['n_chains']:>7} {r['r_hat_max']:>10.4f} "
              f"{r['ess_bulk_min']:>13.1f} {str(r['n_divergences']):>6}")
    first, final = rows[0], rows[-1]
    verdict = "PASS" if final["r_hat_max"] < STRICT_GATE else "FAIL"
    print(f"\nfinal pooled: {final['n_chains']} chains -> r_hat_max={final['r_hat_max']:.4f} "
          f"(strict gate <{STRICT_GATE}: {verdict})")
    print(f"trend: {first['r_hat_max']:.4f} ({first['n_chains']}ch) -> "
          f"{final['r_hat_max']:.4f} ({final['n_chains']}ch), "
          f"delta={final['r_hat_max'] - first['r_hat_max']:+.4f}")


# ── CLI ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="複数 run プール → r_hat 検証（Issue #341）")
    ap.add_argument("--synthetic", action="store_true",
                    help="DB 不使用の小合成データで実行（Stage 1 smoke）")
    ap.add_argument("--n-stocks", type=int, default=0,
                    help="実データを N 銘柄へ間引く（0=全件・Stage 2 は 500 想定）")
    ap.add_argument("--k-runs", type=int, default=4, help="サンプリング回数（プール後は 2K チェーン）")
    ap.add_argument("--draws", type=int, default=800)
    ap.add_argument("--tune", type=int, default=800)
    ap.add_argument("--target-accept", type=float, default=0.95)
    ap.add_argument("--nuts-sampler", default=None,
                    help="本番規模は 'numpyro' 指定（合成 smoke は既定 None の純 Python で可）")
    ap.add_argument("--init", default=None, help="numpyro 使用時は 'adapt_diag' 推奨")
    ap.add_argument("--base-seed", type=int, default=0, help="run k は base_seed+k を使う")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.synthetic:
        (returns, macro_sel, stock_idx, sector_idx,
         selected, n_stock, n_sector, n_factor) = synthetic_inputs(args.base_seed)
    else:
        from macro_beta_inference import select_shared_factors

        panel = build_real_panel()
        if args.n_stocks > 0:
            panel = subsample_panel(panel, args.n_stocks, seed=args.base_seed)
        returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names = panel
        sel = select_shared_factors(macro, returns, factor_names,
                                    max_features=min(12, macro.shape[1]))
        if not sel:
            raise SystemExit("select_shared_factors: 有効なマクロ因子が選ばれませんでした。")
        macro_sel = macro[:, sel]
        selected = [factor_names[i] for i in sel]
        n_stock, n_sector, n_factor = len(edinet_codes), len(sector_names), len(sel)

    logger.info("panel: n_obs=%d n_stock=%d n_sector=%d n_factor=%d",
                len(returns), n_stock, n_sector, n_factor)

    idatas = run_k_samples(
        returns, macro_sel, stock_idx, sector_idx, n_stock, n_sector, n_factor,
        k_runs=args.k_runs, draws=args.draws, tune=args.tune,
        target_accept=args.target_accept, nuts_sampler=args.nuts_sampler,
        init=args.init, base_seed=args.base_seed,
    )
    rows = pool_and_diagnose(idatas)
    print_report(rows, selected)


if __name__ == "__main__":
    main()
