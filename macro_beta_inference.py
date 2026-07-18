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
    diagnostics: dict | None = None                 # r_hat_max/ess_bulk_min 等（収束診断・ADR-0002 検証）
    hyperparams: dict | None = None                 # draws/tune/target_accept/seed（persist で meta へ）


def _drop_unusable_macro(macro_cache: dict, macro_names: list[str],
                         prices_by_co: dict) -> tuple[list[str], list[str]]:
    """全観測日で None になる（一切値が出ない）マクロ特徴量を除外する（Issue #352）。

    build_panel は `macro_nan_ok=False` で build_snapshots を呼ぶため、1系列でも None の
    サンプルは即破棄される（macro_snapshots.py の ANY-None ゲート・min_coverage 判定より前）。
    公表頻度が低すぎて zscore の最小点数（trailing 5年に 20点）を満たさない系列——例えば
    IMF WEO 見通し（年2回公表・#284）は約10点しか無く全 snap_date で None になる——が1つでも
    混ざると、全スナップショットが脱落して producer 全体が落ちる。

    マクロ特徴量は日付にのみ依存するので、観測されうる全 trade_date で `_macro_from_cache` が
    None を返す特徴量＝どのスナップショットでも使えない特徴量を落とす。部分的に値が出る系列
    （例: 収集開始が新しく古い日付では None の HY_OAS 等）は残す（それらは自身の利用可能窓で
    正しくサンプルを制約するだけで、全滅の原因にはならない）。

    Returns: (usable, dropped)
    """
    from plugins.macro_snapshots import _macro_from_cache

    # 最新日から探索（通常のアクティブ系列は最新日で必ず値が出るので短絡が効く）。
    probe_dates = sorted({r.trade_date for rows in prices_by_co.values() for r in rows},
                         reverse=True)
    usable, dropped = [], []
    for fname in macro_names:
        has_value = any(
            _macro_from_cache(macro_cache, d, [fname])[fname] is not None
            for d in probe_dates
        )
        (usable if has_value else dropped).append(fname)
    return usable, dropped


def build_panel(db, macro_names: list[str] | None = None) -> tuple:
    """DB から週次リターン・マクロ因子・セクターを読み、パネル（銘柄×時点×因子）を構築する。

    plugins.macro_snapshots の load_data / preload_macro / build_snapshots を再利用する
    （ADR-0002 §2: マクロは主効果のみ・交差項なし・欠損サンプルは破棄）。財務特徴量は
    階層モデルの説明変数に含めない（fin_features=[]）ため build_snapshots には要求しないが、
    「その月に適用可能な財務レコードが存在する」ことはスナップショット採用の前提として残る
    （build_snapshots 内部の _find_applicable_fin ゲート）。

    macro_names 省略時は MACRO_FEATURE_NAMES（全系列・BIC 選択の候補プール）を使う。
    テストでは小さい集合を明示的に渡せる。

    Returns:
        (returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names)
    """
    from plugins.macro_snapshots import (
        MACRO_FEATURE_NAMES,
        build_snapshots,
        load_data,
        preload_macro,
    )

    prices_by_co, fin_by_co, companies = load_data(db)
    if not prices_by_co:
        raise ValueError("build_panel: 株価週次履歴がありません。先に収集を実行してください。")

    macro_names = list(macro_names) if macro_names is not None else list(MACRO_FEATURE_NAMES)
    macro_cache = preload_macro(db, prices_by_co, macro_names)

    # Issue #352: 全観測日で None になるマクロ特徴量（IMF WEO 等・公表頻度が低く zscore
    # 最小点数未満）が1つでも混ざると macro_nan_ok=False の下で全サンプルが脱落するため、
    # build_snapshots へ渡す前に除外する。除外内容はログに残す（silent-drop しない）。
    macro_names, dropped = _drop_unusable_macro(macro_cache, macro_names, prices_by_co)
    if dropped:
        logger.warning("build_panel: 全観測日で値が出ないマクロ特徴量を除外しました: %s", dropped)
    if not macro_names:
        raise ValueError(
            "build_panel: 使用可能なマクロ特徴量がありません（マクロデータの蓄積状況を確認してください）。")

    samples_by_ym, sample_meta_by_ym, _current_snaps, factor_names, stock_ids_by_ym = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache,
        fin_features=[], macro_names=macro_names,
        use_momentum=False, mom_window=0, min_coverage=1.0,
        build_interactions=False, macro_nan_ok=False,
        return_stock_ids=True,
    )

    returns: list[float] = []
    macro_rows: list[list[float]] = []
    edinet_code_seq: list[str] = []
    sector_seq: list[str] = []
    for ym in sorted(samples_by_ym.keys()):
        pairs = samples_by_ym[ym]
        metas = sample_meta_by_ym[ym]
        codes = stock_ids_by_ym[ym]
        for (feat_row, log_ret), (industry, _size), code in zip(pairs, metas, codes):
            returns.append(log_ret)
            macro_rows.append(feat_row)
            edinet_code_seq.append(code)
            sector_seq.append(industry)

    if not returns:
        raise ValueError(
            "build_panel: 有効なサンプルがありません（株価週次履歴・マクロ・財務データの蓄積状況を確認してください）"
        )

    edinet_codes = sorted(set(edinet_code_seq))
    code_to_idx = {c: i for i, c in enumerate(edinet_codes)}

    # 銘柄ごとのセクター（build_hierarchical_model の mu_sector[sector_idx] は
    # 「銘柄→セクター」の写像を要求する。observation粒度の sector_seq とは別物）。
    stock_sector: dict[str, str] = dict(zip(edinet_code_seq, sector_seq))
    sector_names = sorted(set(stock_sector.values()))
    sector_to_idx = {s: i for i, s in enumerate(sector_names)}

    stock_idx = np.array([code_to_idx[c] for c in edinet_code_seq], dtype=int)          # observation粒度（beta[stock_idx] 用）
    sector_idx = np.array([sector_to_idx[stock_sector[c]] for c in edinet_codes], dtype=int)  # 銘柄粒度（mu_sector[sector_idx] 用）
    returns_arr = np.asarray(returns, dtype=float)
    macro_arr = np.asarray(macro_rows, dtype=float)

    return returns_arr, macro_arr, stock_idx, sector_idx, factor_names, edinet_codes, sector_names


def select_shared_factors(macro: np.ndarray, returns: np.ndarray,
                          factor_names: list[str], max_features: int) -> list[int]:
    """共有マクロ因子集合を pooled データ上で BIC（LassoLarsIC）選択する。

    ADR-0002 §1: 因子集合は全銘柄共通（pooled / large-n で次元爆発に耐性）。実体は
    plugins.macro_snapshots.select_features_bic（macro_risk_return._select_macro_features
    と同一の pooled BIC 選択手続き・ADR-0002 Considered Options）。
    """
    from plugins.macro_snapshots import select_features_bic

    return select_features_bic(macro, returns, max_features)


def build_hierarchical_model(returns: np.ndarray, macro: np.ndarray,
                             stock_idx: np.ndarray, sector_idx: np.ndarray,
                             n_stock: int, n_sector: int, n_factor: int):
    """全体→セクター→銘柄の二層階層モデルを構築して返す（pm.Model）。

    階層（ADR-0002 §1 で確定した二層 partial pooling。non-centered パラメータ化で実装）::

        mu_universe[f]      ~ Normal(0, 1)                                        # ユニバース事前
        sigma_sector[f]     ~ HalfNormal(1)
        mu_sector_raw[s, f] ~ Normal(0, 1)
        mu_sector[s, f]     := mu_universe[f] + mu_sector_raw[s, f] * sigma_sector[f]   # セクター層
        sigma_stock[f]      ~ HalfNormal(1)
        beta_raw[i, f]      ~ Normal(0, 1)
        beta[i, f]          := mu_sector[sector(i), f] + beta_raw[i, f] * sigma_stock[f] # 銘柄層
        alpha[i]            ~ Normal(0, 1)                                        # 銘柄切片
        r[obs] ~ Normal(alpha[stock] + sum_f beta[stock,f]*macro[obs,f], sigma_obs)

    non-centered 化（offset×scale の Deterministic 合成）は funnel（漏斗状の事後分布）に
    起因する発散遷移を抑え、小 n（実効サンプル一桁／銘柄）での NUTS 収束を改善する
    （Betancourt & Girolami 2013・Neal's funnel）。beta/mu_sector は Deterministic のため
    idata.posterior からそのまま参照可能（summarize() は変更不要）。
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
        mu_sector_raw = pm.Normal("mu_sector_raw", 0.0, 1.0, dims=("sector", "factor"))
        mu_sector = pm.Deterministic(
            "mu_sector", mu_universe + mu_sector_raw * sigma_sector, dims=("sector", "factor")
        )

        sigma_stock = pm.HalfNormal("sigma_stock", 1.0, dims="factor")
        beta_raw = pm.Normal("beta_raw", 0.0, 1.0, dims=("stock", "factor"))
        beta = pm.Deterministic(
            "beta", mu_sector[sector_idx] + beta_raw * sigma_stock, dims=("stock", "factor")
        )
        alpha = pm.Normal("alpha", 0.0, 1.0, dims="stock")

        macro_data = pm.Data("macro", macro)
        mu_obs = alpha[stock_idx] + (beta[stock_idx] * macro_data).sum(axis=-1)
        sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
        pm.Normal("r", mu_obs, sigma_obs, observed=returns)
    return model


def run_inference(draws: int = 1000, tune: int = 1000, target_accept: float = 0.9,
                  seed: int = 0, db=None, macro_names: list[str] | None = None,
                  chains: int = 4, nuts_sampler: str | None = None,
                  init: str | None = None) -> InferenceResult:
    """推論バッチの本体。build_panel → 因子選択 → 階層モデル → NUTS → 事後要約。

    macro_names はテスト用（小さい候補プールを注入）。省略時は build_panel の既定
    （MACRO_FEATURE_NAMES 全系列）を使う。chains は既定 4（pymc 既定と同じ）だが、
    テストでは軽量化のため小さくできる。

    nuts_sampler/init は既定 None（PyMC 既定の純 Python バックエンド・jitter+adapt_diag
    初期化）。本番規模（n_stock~3800）は純 Python バックエンド実測 75秒/draw で
    GitHub Actions のジョブ上限（6時間）に収まらないため、本番実行は
    nuts_sampler="numpyro" を明示指定する（実測 10.5秒/draw・約7倍高速、
    requirements-inference.txt の jax/numpyro 追加が前提）。numpyro 既定初期化は
    この規模のモデルで発散が多発したため、init="adapt_diag"（PyMC 既定と同等）を
    併用すること。詳細は ADR-0002 参照。
    """
    import pymc as pm  # 遅延 import

    returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names = build_panel(
        db, macro_names=macro_names
    )
    # Issue #269: ここでcommitしないと load_data/preload_macro のSELECTで開いたトランザクションが
    # 後続の数時間に及ぶMCMC計算中も残留し、companies等へのAccessShareロックが他セッション（例:
    # ローカルAPI起動時の冪等ALTER TABLE）のACCESS EXCLUSIVE取得をブロックし続ける。MCMC自体は
    # DB接続を使わないため、ここで解放してよい。persist() はコミット後の同一db（pool_pre_ping=True・
    # pool_recycle=180 により長時間後の再利用でも安全）をそのまま使う。
    db.commit()

    sel = select_shared_factors(macro, returns, factor_names,
                                max_features=min(12, macro.shape[1]))
    if not sel:
        raise ValueError("select_shared_factors: 有効なマクロ因子が選択されませんでした。データを確認してください。")
    macro_sel = macro[:, sel]
    selected = [factor_names[i] for i in sel]

    model = build_hierarchical_model(
        returns, macro_sel, stock_idx, sector_idx,
        n_stock=len(edinet_codes), n_sector=len(sector_names), n_factor=len(sel),
    )
    sample_kwargs: dict = dict(draws=draws, tune=tune, target_accept=target_accept,
                               random_seed=seed, chains=chains, progressbar=False)
    if nuts_sampler == "numpyro":
        import os
        import numpyro
        os.environ.setdefault("XLA_FLAGS", f"--xla_force_host_platform_device_count={chains}")
        numpyro.set_host_device_count(chains)
        sample_kwargs["nuts_sampler"] = "numpyro"
    elif nuts_sampler:
        sample_kwargs["nuts_sampler"] = nuts_sampler
    if init:
        sample_kwargs["init"] = init
    with model:
        idata = pm.sample(**sample_kwargs)

    diagnostics = summarize_diagnostics(idata)
    if diagnostics.get("r_hat_max") is not None and diagnostics["r_hat_max"] > 1.01:
        logger.warning(
            "収束診断: r_hat_max=%.4f が ADR-0002 検証基準（<1.01）を超過。"
            "draws/tune を増やすか再実行を検討してください（ess_bulk_min=%s, n_divergences=%s）",
            diagnostics["r_hat_max"], diagnostics.get("ess_bulk_min"), diagnostics.get("n_divergences"),
        )

    result = summarize(idata, selected, macro_sel, edinet_codes)
    if not result.run_id:
        result.run_id = datetime.now(timezone.utc).strftime("mb_%Y%m%dT%H%M%SZ")
    if not result.snapshot_date:
        result.snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result.diagnostics = diagnostics
    result.hyperparams = {"draws": draws, "tune": tune, "target_accept": target_accept, "seed": seed,
                          "chains": chains, "nuts_sampler": nuts_sampler or "pymc", "init": init}
    return result


def summarize_diagnostics(idata) -> dict:
    """r_hat・ESS の収束診断サマリ（ADR-0002 検証基準: r_hat<1.01・ESS 十分性・発散遷移数）。"""
    import arviz as az

    summ = az.summary(idata, var_names=["beta", "alpha", "mu_universe"], kind="diagnostics")
    diverging = idata.sample_stats.get("diverging") if hasattr(idata, "sample_stats") else None
    return {
        "r_hat_max":     float(summ["r_hat"].max()),
        "ess_bulk_min":  float(summ["ess_bulk"].min()),
        "ess_tail_min":  float(summ["ess_tail"].min()),
        "n_divergences": int(diverging.sum()) if diverging is not None else None,
    }


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
        "hyperparams":      {**(result.hyperparams or {}), "diagnostics": result.diagnostics},
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
    db.commit()


def persist_allowed(r_hat_max: float | None, threshold: float, force: bool) -> bool:
    """収束診断に基づく persist 可否判定（純関数・テスト可能に切り出し）。

    persist を許可するのは以下のいずれか:
    - `force=True`（人手で結果を精査した上での強制書き込み）
    - `r_hat_max` が算出できない（診断不能＝ゲート対象外・従来挙動を踏襲）
    - `r_hat_max <= threshold`（収束基準を満たす）

    threshold は既定 1.01（ADR-0002 の strict 基準）。chains=2 のランナーでは r_hat が
    構造的に 1.02 前後で頭打ちになる（PyMC も「信頼できる r_hat には4 chain以上推奨」と
    警告する通り 2 chain では保守的に出る）ため、月次 cron 等の無人自動実行では緩和した
    threshold（例 1.05）を渡し、構造的な ~1.02 は自動 persist しつつ、本当に収束していない
    run（r_hat が threshold を大きく超過）は依然 reject する運用にできる（Issue #341）。
    """
    if force:
        return True
    if r_hat_max is None:
        return True
    return r_hat_max <= threshold


def main() -> None:
    ap = argparse.ArgumentParser(description="M-1 per-stock 階層マクロ・ベータ推論バッチ")
    ap.add_argument("--draws", type=int, default=1000)
    ap.add_argument("--tune", type=int, default=1000)
    ap.add_argument("--target-accept", type=float, default=0.9)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--chains", type=int, default=4)
    ap.add_argument("--nuts-sampler", default=None,
                    help="既定は PyMC 純 Python バックエンド。本番規模では 'numpyro' 指定が必須"
                         "（純 Pythonは実測75秒/draw・GitHub Actionsの6時間上限に収まらない）")
    ap.add_argument("--init", default=None,
                    help="numpyro 使用時は 'adapt_diag' 推奨（既定初期化は発散多発の実測あり）")
    ap.add_argument("--r-hat-threshold", type=float, default=1.01,
                    help="persist を許可する r_hat_max の上限（既定 1.01＝ADR-0002 strict 基準）。"
                         "chains=2 では r_hat が構造的に ~1.02 で頭打ちのため、無人 cron では 1.05 等へ"
                         "緩和して構造的 ~1.02 を自動 persist しつつ真の未収束は reject する（Issue #341）")
    ap.add_argument("--force", action="store_true",
                    help="収束診断が threshold（既定 r_hat_max<=1.01）未達でも DB へ persist する"
                         "（既定は拒否。producer に品質ゲートが無く即座にライブ推奨へ反映されるため）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    from database import SessionLocal  # 遅延 import（本番と共有のセッションファクトリ）

    db = SessionLocal()
    try:
        result = run_inference(draws=args.draws, tune=args.tune, target_accept=args.target_accept,
                               seed=args.seed, db=db, chains=args.chains,
                               nuts_sampler=args.nuts_sampler, init=args.init)
        logger.info("収束診断: %s", result.diagnostics)
        r_hat_max = result.diagnostics.get("r_hat_max")
        if not persist_allowed(r_hat_max, args.r_hat_threshold, args.force):
            logger.error(
                "persist を拒否: r_hat_max=%.4f が threshold（<=%.4f）を超過（n_divergences=%s）。"
                "macro_risk_return の producer には品質ゲートが無く persist 即ライブ反映されるため、"
                "既定では書き込まない。draws/tune/target_accept を見直すか、--r-hat-threshold を"
                "緩和するか、--force で強制書き込み可能。",
                r_hat_max, args.r_hat_threshold, result.diagnostics.get("n_divergences"),
            )
            raise SystemExit(1)
        persist(db, result)
        logger.info("推論完了・DB永続化済み: %d 銘柄 / 因子 %s", len(result.loadings), result.selected_factors)
    finally:
        db.close()


if __name__ == "__main__":
    main()
