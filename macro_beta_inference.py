"""M-1 per-stock 階層マクロ・ベータの推論バッチ（GitHub Actions 専用・本番非搭載）。

ADR-0002「M-1 を per-stock 階層マクロ・ベータへ再設計」の spine（Issue #214）。

役割
----
全体→セクター→銘柄の二層フルベイズ階層モデル（PyMC・NUTS）で、共有マクロ因子への
ローディングを銘柄ごとに部分プーリング推定する。MCMC は /api/plugins/*/run の同期
リクエストでは回せない（Render タイムアウト）ため、本モジュールは GitHub Actions の
推論ジョブから実行し、結果（per-stock 事後ローディング平均・SE、選択因子集合、因子
共分散 Sigma_macro）を DB へ永続化する。M-1 プラグイン（producer）はそれを読むだけ。

依存
----
PyMC は本番 Render の requirements.txt には載せない。requirements-inference.txt で
のみ導入する。本番コードからの誤 import 事故を避けるため、pymc は本モジュール先頭では
なく run_inference / build_hierarchical_model 内で遅延 import する。

実行
----
    pip install -r requirements-inference.txt
    python macro_beta_inference.py --draws 1000 --tune 1000

検証
----
WF-CV で単一β比 R² 非劣化・全銘柄が共有因子集合上のローディングを持つこと（commensurable）・
R1 退化の解消（R1' が銘柄間で分散を持つこと）。詳細は ADR-0002 Consequences・Issue #214。
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger("macro_beta_inference")

# 永続化テーブル名（DDL は database.py 側で定義する。スキーマは Issue #214 を正本とする）。
LOADINGS_TABLE = "macro_beta_loadings"   # (edinet_code, factor_name, loading_mean, loading_se, run_id)
META_TABLE = "macro_beta_meta"           # (run_id, snapshot_date, selected_factors, factor_cov, hyperparams)


@dataclass
class InferenceResult:
    """推論バッチの成果物。persist() で DB へ書き出す単位。"""
    run_id: str
    snapshot_date: str
    selected_factors: list[str]
    # per-stock 事後要約: edinet_code -> {factor_name -> (mean, se)}
    loadings: dict[str, dict[str, tuple[float, float]]]
    alpha: dict[str, tuple[float, float]]          # 銘柄切片（事後平均・SE）
    mu_pred: dict[str, float]                       # per-stock 予測リターン μ（事後平均予測）
    factor_cov: list[list[float]]                   # Sigma_macro（選択因子の共分散・R_macro 用）


def build_panel(db) -> tuple:
    """DB から週次リターン・マクロ因子・セクターを読み、パネル（銘柄×時点×因子）を構築する。

    既存の plugins.macro_risk_return の _load_data / _preload_macro / _build_snapshots と
    同じデータ経路を再利用する想定（重複ロジックは共通ヘルパーへ切り出す — Issue #214）。

    Returns:
        (returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names)
    """
    raise NotImplementedError(
        "build_panel: macro_risk_return のデータ経路を共通化して接続する（Issue #214）"
    )


def select_shared_factors(macro: np.ndarray, returns: np.ndarray,
                          factor_names: list[str], max_features: int) -> list[int]:
    """共有マクロ因子集合を pooled データ上で BIC（LassoLarsIC）選択する。

    ADR-0002 §1: 因子集合は全銘柄共通（pooled / large-n で次元爆発に耐性）。これは現行
    plugins.macro_risk_return._select_macro_features と同じ手続き（per-stock 化後も「共有因子選択」
    として据え置く）。実装は既存関数の再利用を想定。
    """
    raise NotImplementedError(
        "select_shared_factors: _select_macro_features 相当を pooled で適用（Issue #214）"
    )


def build_hierarchical_model(returns: np.ndarray, macro: np.ndarray,
                             stock_idx: np.ndarray, sector_idx: np.ndarray,
                             n_stock: int, n_sector: int, n_factor: int):
    """全体→セクター→銘柄の二層階層モデルを構築して返す（pm.Model）。

    階層（ADR-0002 §1 で確定した二層 partial pooling）::

        mu_universe[f]  ~ Normal(0, 1)                                   # ユニバース事前
        sigma_sector[f] ~ HalfNormal(1)
        mu_sector[s, f] ~ Normal(mu_universe[f], sigma_sector[f])        # セクター層
        sigma_stock[f]  ~ HalfNormal(1)
        beta[i, f]      ~ Normal(mu_sector[sector(i), f], sigma_stock[f])# 銘柄層
        alpha[i]        ~ Normal(0, 1)                                   # 銘柄切片
        r[obs] ~ Normal(alpha[stock] + sum_f beta[stock,f]*macro[obs,f], sigma_obs)

    NOTE: 収束改善のため non-centered パラメータ化（offset×scale）への置換を推奨（夜間で検討）。
    """
    import pymc as pm  # 遅延 import（本番ランタイムからの誤 import 事故を防ぐ）

    coords = {
        "factor": list(range(n_factor)),
        "sector": list(range(n_sector)),
        "stock": list(range(n_stock)),
    }
    with pm.Model(coords=coords) as model:
        mu_universe = pm.Normal("mu_universe", 0.0, 1.0, dims="factor")
        sigma_sector = pm.HalfNormal("sigma_sector", 1.0, dims="factor")
        mu_sector = pm.Normal(
            "mu_sector", mu_universe, sigma_sector, dims=("sector", "factor")
        )
        sigma_stock = pm.HalfNormal("sigma_stock", 1.0, dims="factor")
        beta = pm.Normal(
            "beta", mu_sector[sector_idx], sigma_stock, dims=("stock", "factor")
        )
        alpha = pm.Normal("alpha", 0.0, 1.0, dims="stock")

        macro_data = pm.Data("macro", macro)
        mu_obs = alpha[stock_idx] + (beta[stock_idx] * macro_data).sum(axis=-1)
        sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
        pm.Normal("r", mu_obs, sigma_obs, observed=returns)
    return model


def run_inference(draws: int = 1000, tune: int = 1000, target_accept: float = 0.9,
                  seed: int = 0, db=None) -> InferenceResult:
    """推論バッチの本体。build_panel → 因子選択 → 階層モデル → NUTS → 事後要約。"""
    import pymc as pm  # 遅延 import

    returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names = build_panel(db)

    sel = select_shared_factors(macro, returns, factor_names,
                                max_features=min(12, macro.shape[1]))
    macro_sel = macro[:, sel]
    selected = [factor_names[i] for i in sel]

    model = build_hierarchical_model(
        returns, macro_sel, stock_idx, sector_idx,
        n_stock=len(edinet_codes), n_sector=len(sector_names), n_factor=len(sel),
    )
    with model:
        idata = pm.sample(draws=draws, tune=tune, target_accept=target_accept,
                          random_seed=seed, progressbar=False)

    result = summarize(idata, selected, macro_sel, edinet_codes)
    if not result.run_id:
        result.run_id = datetime.now(timezone.utc).strftime("mb_%Y%m%dT%H%M%SZ")
    return result


def summarize(idata, selected: list[str], macro_sel: np.ndarray,
              edinet_codes: list[str]) -> InferenceResult:
    """事後分布から per-stock ローディング平均・SE と Sigma_macro を抽出する。"""
    post = idata.posterior
    beta_mean = post["beta"].mean(dim=("chain", "draw")).values   # (n_stock, n_factor)
    beta_sd = post["beta"].std(dim=("chain", "draw")).values
    alpha_mean = post["alpha"].mean(dim=("chain", "draw")).values
    alpha_sd = post["alpha"].std(dim=("chain", "draw")).values

    # Sigma_macro: 選択マクロ因子の標本共分散（R_macro = sqrt(betaᵀ Sigma beta) 用）。
    factor_cov = np.atleast_2d(np.cov(macro_sel, rowvar=False))

    loadings: dict[str, dict[str, tuple[float, float]]] = {}
    alpha_out: dict[str, tuple[float, float]] = {}
    mu_pred: dict[str, float] = {}
    macro_means = macro_sel.mean(axis=0)
    for i, code in enumerate(edinet_codes):
        loadings[code] = {
            f: (float(beta_mean[i, j]), float(beta_sd[i, j]))
            for j, f in enumerate(selected)
        }
        alpha_out[code] = (float(alpha_mean[i]), float(alpha_sd[i]))
        mu_pred[code] = float(alpha_mean[i] + beta_mean[i] @ macro_means)

    return InferenceResult(
        run_id="", snapshot_date="", selected_factors=selected,
        loadings=loadings, alpha=alpha_out, mu_pred=mu_pred,
        factor_cov=factor_cov.tolist(),
    )


def persist(db, result: InferenceResult) -> None:
    """推論結果を macro_beta_meta / macro_beta_loadings へ upsert する（#214）。

    per-stock 切片は factor_name="_intercept" 行として格納し、producer が μ を復元する。
    スキーマ・upsert 本体は database.upsert_macro_beta（縦持ち・DDL 追加のみ・Supabase 容量軽微）。
    """
    from database import upsert_macro_beta  # 遅延 import（バッチ実行時のみ DB へ接続）

    meta = {
        "run_id":           result.run_id,
        "snapshot_date":    result.snapshot_date,
        "selected_factors": result.selected_factors,
        "factor_cov":       result.factor_cov,
        "hyperparams":      {},
    }
    rows: list[dict] = []
    for code, fmap in result.loadings.items():
        for fname, (mean, se) in fmap.items():
            rows.append({"run_id": result.run_id, "edinet_code": code,
                         "factor_name": fname, "loading_mean": mean, "loading_se": se})
        a_mean, a_se = result.alpha.get(code, (0.0, None))
        rows.append({"run_id": result.run_id, "edinet_code": code,
                     "factor_name": "_intercept", "loading_mean": a_mean, "loading_se": a_se})
    upsert_macro_beta(db, meta, rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="M-1 per-stock 階層マクロ・ベータ推論バッチ")
    ap.add_argument("--draws", type=int, default=1000)
    ap.add_argument("--tune", type=int, default=1000)
    ap.add_argument("--target-accept", type=float, default=0.9)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    from database import SessionLocal  # 遅延 import（本番と共有のセッションファクトリ）

    db = SessionLocal()
    try:
        result = run_inference(draws=args.draws, tune=args.tune,
                               target_accept=args.target_accept, seed=args.seed, db=db)
        persist(db, result)
        logger.info("推論完了: %d 銘柄 / 因子 %s", len(result.loadings), result.selected_factors)
    finally:
        db.close()


if __name__ == "__main__":
    main()
