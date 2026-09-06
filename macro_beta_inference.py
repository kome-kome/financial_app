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
import contextlib
import logging
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger("macro_beta_inference")

# heartbeat の間隔[秒]。5分は「生死の判定に十分な粒度」と「数時間ぶんでもログが読める量
# （3時間で36行）」の折り合い。短くしても NUTS の中身は分からないので細かくする意味がない。
HEARTBEAT_SEC = 300.0

# beta を事後再構成するときの銘柄チャンク幅（#541）。beta は自由 RV から一意に組み直せる
# ので**トレースには載せない**が、mean/sd も r_hat も draw ごとの値が要るため、どこかで
# 一度は (chain, draw, stock, factor) を作る必要がある。全銘柄まとめて作ると、削減した
# はずの約584MB がそのまま戻る——だから銘柄方向へ刻む。
# 1チャンクの実体は chain × draw × 256 × n_factor × 8B ＝ 2×800×256×25×8 ≒ 82MB、
# 4chains×1000draws でも約205MB。この幅なら常駐の山を作らない。
BETA_CHUNK_STOCKS = 256

# 収束ゲート（`persist_allowed`）が変数ごとに保持する「悪い順」の件数（#609）。
# ゲートは p99 で判定するので、p99 と max のあいだにいる個体は**通ってしまう**——
# 誰がそこに居たかを名前で残さないと、後から「この銘柄の μ がおかしい」となったときの
# 手がかりが無い。本番規模では beta が 46,044 個あり全件は診断 JSON に載せられないので
# 上位だけ残す。10件なのは、変数3つで30行＝ログ1画面に収まる量。
WORST_KEEP = 10

# 閾値までの余裕がこれを切ったら警告する（#609）。`alpha` の p99 は本番実測で 1.0463＝
# 閾値 1.05 まで 0.0037 しかなく、銘柄が増えれば縮む方向にある。**落ちてから気づくと
# 6.7時間の run が隔離される**ので、落ちる前の回で予告を出す。
PERSIST_MARGIN_WARN = 0.005

# 無人の月次実行（`scripts/run_monthly_beta.py`）が渡す緩和閾値（Issue #341）。CLI 既定の
# 1.01（ADR-0002 の strict 基準）ではなくこちらを使う理由は `persist_allowed` の docstring。
# **これが唯一の源**——格子（`scripts/bench_macro_beta_report.py`）が本番の合否を出すのに
# 同じ値を要るので、両者が別々に 1.05 を書くと片方だけ動いたとき黙ってずれる（#613）。
# `run_monthly_beta.py` は子プロセスの argv にリテラルで持つが、一致は
# `tests/test_run_monthly_beta.py` が照合する。
MONTHLY_RHAT_THRESHOLD = 1.05


def parse_max_tree_depth(text):
    """`--max-tree-depth` の文字列を numpyro が受ける形へ（#540）。

    - `"8"`    → `8`（warmup も sampling も 8）
    - `"8,10"` → `(8, 10)`（**warmup だけ 8・sampling は 10**）。numpyro は `max_tree_depth` に
      タプルを受け `(warmup, sampling)` として解釈する（`numpyro/infer/hmc.py`）。PyMC 自身も
      `early_max_treedepth=8` で同じことをしている。warmup は本番で全 iter の半分
      （tune 800 / draws 800）を占めるので、**draws 側の軌道長を変えずに総コストだけ落とす**候補。
    - `None` / 空 → `None`（＝**現状維持**。既定値を勝手に埋めない）

    不正値は raise する（`upsert_financial` の未知キーと同じ fail fast。黙って既定へ倒すと
    「指定したのに効いていない run」が測定結果に混ざる）。
    """
    if text is None:
        return None
    parts = [p.strip() for p in str(text).split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) > 2:
        raise ValueError("max_tree_depth は '8' か '8,10'（warmup,sampling）の形式: " + str(text))
    vals = []
    for p in parts:
        try:
            v = int(p)
        except ValueError:
            raise ValueError("max_tree_depth に整数以外が入っている: " + str(text)) from None
        if not (1 <= v <= 20):
            raise ValueError("max_tree_depth は 1..20 の範囲（2**20 歩で既に非現実的）: " + str(text))
        vals.append(v)
    return vals[0] if len(vals) == 1 else (vals[0], vals[1])


def nuts_depth_kwargs(nuts_sampler, max_tree_depth=None) -> dict:
    """`max_tree_depth` を `pm.sample` の引数へ載せる。**届く場所がサンプラーで違う**（#540）。

    - numpyro: `nuts_sampler_kwargs={"nuts_kwargs": {"max_tree_depth": ...}}` →
      `pymc.sampling.jax.sample_jax_nuts(nuts_kwargs=...)` → `numpyro.infer.NUTS(...)`
    - 純 PyMC: `nuts={"max_treedepth": ...}`（step 引数の経路）

    **`nuts={...}` は外部サンプラー経路では捨てられる**（`pymc/sampling/mcmc.py` は
    `kwargs.pop("nuts", {}).get("target_accept")` しか見ない）＝取り違えるとエラーも警告も
    出ずに設定だけが効かない。だから経路を明示的に分け、未知のサンプラーには raise する。

    `max_tree_depth=None`（既定）では **空 dict を返す**＝`pm.sample` へ渡る引数は現状と
    1バイトも変わらない。
    """
    if max_tree_depth is None:
        return {}
    if nuts_sampler == "numpyro":
        return {"nuts_sampler_kwargs": {"nuts_kwargs": {"max_tree_depth": max_tree_depth}}}
    if not nuts_sampler or nuts_sampler == "pymc":
        if isinstance(max_tree_depth, tuple):
            # PyMC の early_max_treedepth は warmup の**前半**にだけ効く（numpyro のタプルは
            # warmup 全体）。厳密には同義でないが、本番経路は numpyro なのでここは
            # 「同じ意図を最も近い形で通す」に留める。
            return {"nuts": {"early_max_treedepth": max_tree_depth[0],
                             "max_treedepth": max_tree_depth[1]}}
        return {"nuts": {"max_treedepth": int(max_tree_depth)}}
    raise ValueError(
        "max_tree_depth の渡し方が未定義のサンプラー: " + str(nuts_sampler)
        + "（numpyro か pymc のみ対応。黙って無視されるより落とす）")


@contextlib.contextmanager
def _heartbeat(what: str, interval: float = HEARTBEAT_SEC):
    """ブロック実行中、`interval` ごとに経過をログへ刻む。

    NUTS は数時間かかるのに `progressbar=False`（tqdm の `\\r` 連打はファイルログを壊す）で
    無音になる。**無音は「順調」と「死亡」を区別しない**——2026-08-21 の実行は7時間走った
    形跡がどこにも残らず、プロセスが消えたことに12時間気づけなかった。

    daemon スレッドなので、本体が例外で抜けても・強制終了されても後始末を邪魔しない。
    """
    stop = threading.Event()
    started = _time.monotonic()

    def _tick() -> None:
        while not stop.wait(interval):
            logger.info("[heartbeat] %s 継続中: 経過 %.0f分",
                        what, (_time.monotonic() - started) / 60.0)

    t = threading.Thread(target=_tick, name="heartbeat", daemon=True)
    t.start()
    try:
        yield
    finally:
        # set() だけではスレッドは「これから起きる」状態で残る。join まで待って、
        # ブロックを抜けた時点で確実に居なくなっていることを保証する（daemon なので
        # 万一 join がタイムアウトしてもプロセス終了は妨げない）。
        stop.set()
        t.join(timeout=1.0)

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

    # build_snapshots へ price_features を渡さない＝週次 volume_sum を読まないので引かない
    # （Issue #446・1回 12.1MB）。
    prices_by_co, fin_by_co, companies = load_data(db, with_volume=False)
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

    non-centered 化（offset×scale の合成）は funnel（漏斗状の事後分布）に起因する発散遷移を
    抑え、小 n（実効サンプル一桁／銘柄）での NUTS 収束を改善する（Betancourt & Girolami
    2013・Neal's funnel）。

    **beta / mu_sector は Deterministic にしない**（#541）。Deterministic はトレースへ全 draw
    保存されるため、本番規模（3,800銘柄・draws 800・chains 2）で beta 単体 約584MB を常駐
    させていた。両者は自由 RV（mu_universe / mu_sector_raw / sigma_sector / beta_raw /
    sigma_stock）から一意に再構成できるので、posterior には載せず必要時に組み直す
    （`_reconstruct_beta_chunk`）。**確率構造は変わっていない**＝統計的な変更ではない。
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
        # 素の式（Deterministic にしない）。ここを Deterministic に戻すと #541 の常駐が復活する。
        mu_sector = mu_universe + mu_sector_raw * sigma_sector

        sigma_stock = pm.HalfNormal("sigma_stock", 1.0, dims="factor")
        beta_raw = pm.Normal("beta_raw", 0.0, 1.0, dims=("stock", "factor"))
        beta = mu_sector[sector_idx] + beta_raw * sigma_stock
        alpha = pm.Normal("alpha", 0.0, 1.0, dims="stock")

        macro_data = pm.Data("macro", macro)
        mu_obs = alpha[stock_idx] + (beta[stock_idx] * macro_data).sum(axis=-1)
        sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
        pm.Normal("r", mu_obs, sigma_obs, observed=returns)
    return model


def run_inference(draws: int = 1000, tune: int = 1000, target_accept: float = 0.9,
                  seed: int = 0, db=None, macro_names: list[str] | None = None,
                  chains: int = 4, nuts_sampler: str | None = None,
                  init: str | None = None, max_tree_depth=None) -> InferenceResult:
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

    max_tree_depth（#540）は既定 None＝サンプラー既定（numpyro は 10）で、**渡さなければ
    現状と1バイトも変わらない**。本番で使う値は `scripts/run_monthly.py` の macro_beta ステップ
    引数に明示する（draws/tune/chains/r-hat-threshold と同じ扱い＝実行条件が1箇所に並ぶ）。
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
    # NUTS は数時間かかるうえ `progressbar=False`（tqdm の \r 連打はファイルログを壊す）。
    # **無音のまま数時間**では「走っているのか死んでいるのか」が区別できず、2026-08-21 に
    # 実際に12時間気づけなかった。規模と開始時刻をここで残し、以降は heartbeat が刻む。
    logger.info("サンプリング開始: n_stock=%d n_sector=%d n_factor=%d n_obs=%d "
                "draws=%d tune=%d chains=%d sampler=%s max_tree_depth=%s",
                len(edinet_codes), len(sector_names), len(sel), len(returns),
                draws, tune, chains, nuts_sampler or "pymc", max_tree_depth)
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
    sample_kwargs.update(nuts_depth_kwargs(nuts_sampler, max_tree_depth))
    sampling_started = _time.monotonic()
    with model, _heartbeat("NUTS サンプリング"):
        idata = pm.sample(**sample_kwargs)
    logger.info("サンプリング完了: %.1f分", (_time.monotonic() - sampling_started) / 60.0)

    diagnostics = summarize_diagnostics(idata, sector_idx, edinet_codes=edinet_codes,
                                        factor_names=selected)
    if diagnostics.get("r_hat_max") is not None and diagnostics["r_hat_max"] > 1.01:
        # **これはゲートではない**（ゲートは変数別 p99・#609）。本番規模では
        # `r_hat_max` は 49,893 個の順序統計で 1.01 を超えるのが常態なので、
        # 過去 run と同じ定義の量として記録するだけに留める。
        logger.info(
            "収束診断（参考値）: r_hat_max=%.4f が ADR-0002 の strict 基準（<1.01）を超過。"
            "順序統計なので規模とともに上がる量であり、persist の可否は変数別の p99 が決める"
            "（ess_bulk_min=%s, n_divergences=%s）",
            diagnostics["r_hat_max"], diagnostics.get("ess_bulk_min"), diagnostics.get("n_divergences"),
        )
    # 極値を出している母数まで出す（#600）。beta なら永続化対象そのもの＝ゲートの見方を
    # 変える案は採れない。これが無いと本番を1回（実測 6.7時間）回し直すまで分からない。
    logger.info("収束診断の極値: ess_bulk_min=%s / r_hat_max=%s",
                diagnostics.get("ess_bulk_argmin"), diagnostics.get("r_hat_argmax"))

    result = summarize(idata, selected, macro_sel, edinet_codes, sector_idx)
    if not result.run_id:
        result.run_id = datetime.now(timezone.utc).strftime("mb_%Y%m%dT%H%M%SZ")
    if not result.snapshot_date:
        result.snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result.diagnostics = diagnostics
    result.hyperparams = {"draws": draws, "tune": tune, "target_accept": target_accept, "seed": seed,
                          "chains": chains, "nuts_sampler": nuts_sampler or "pymc", "init": init,
                          "max_tree_depth": max_tree_depth}
    return result


def _reconstruct_mu_sector(post) -> np.ndarray:
    """posterior の自由 RV から mu_sector を組み直す → (chain, draw, sector, factor)。

    実体は 2×800×34×25×8B ≒ 11MB と小さいので銘柄チャンクの外で1度だけ作る
    （チャンクごとに作り直すと同じ計算を n_stock/256 回繰り返すことになる）。
    """
    mu_universe = post["mu_universe"].values          # (chain, draw, factor)
    mu_sector_raw = post["mu_sector_raw"].values      # (chain, draw, sector, factor)
    sigma_sector = post["sigma_sector"].values        # (chain, draw, factor)
    return mu_universe[:, :, None, :] + mu_sector_raw * sigma_sector[:, :, None, :]


def _reconstruct_beta_chunk(post, sector_idx, lo: int, hi: int,
                            mu_sector: np.ndarray | None = None) -> np.ndarray:
    """beta[:, :, lo:hi, :] を自由 RV から再構成する → (chain, draw, hi-lo, factor)。

    beta[i, f] = mu_sector[sector(i), f] + beta_raw[i, f] * sigma_stock[f]

    **draw ごとに組む必要がある**。sigma_stock は確率変数なので
    mean(beta_raw * sigma_stock) ≠ mean(beta_raw) * mean(sigma_stock)——事後平均から
    組み直しても正しい beta の平均にはならず、SD に至っては全く別物になる。

    beta_raw は (chain, draw, n_stock, factor) と大きいので、`.values` を取る前に
    `.isel` で銘柄方向を切る（全体を実体化させない）。
    """
    if mu_sector is None:
        mu_sector = _reconstruct_mu_sector(post)
    sector_idx = np.asarray(sector_idx)
    beta_raw = post["beta_raw"].isel(stock=slice(lo, hi)).values   # (chain, draw, hi-lo, factor)
    sigma_stock = post["sigma_stock"].values                       # (chain, draw, factor)
    return mu_sector[:, :, sector_idx[lo:hi], :] + beta_raw * sigma_stock[:, :, None, :]


_PARAM_INDEX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[([0-9,\s]+)\]$")


def locate_extreme(values, labels, kind: str, stock_offset: int = 0) -> dict | None:
    """診断値の1ブロックから極値を出している母数を1件だけ拾う（#600）。

    `ess_bulk_min` / `r_hat_max` は本番規模では 49,893 個の順序統計であって、**どの母数が
    出しているかは値からは分からない**。`alpha`（銘柄切片）ならゲートの見方を変える余地が
    あるが、`beta` なら `macro_beta_loadings` の永続化対象そのもの——判断が変わるのに、
    これまでは本番を1回（実測 6.7時間）回し直さないと分からなかった。

    ブロックごとに1件だけ拾い、呼び側がグローバルの極値を `pick_extreme` で更新する
    （49,893 要素ぶんのラベル配列は作らない）。`stock_offset` は beta チャンクのローカル
    銘柄 index を全体の index へ直すためのもの——**足し忘れても値は正しいままラベルだけ
    静かにずれる**ので、tests がチャンク境界をまたぐケースで縛っている。

    ラベルの解釈に失敗しても落とさない（`stock`/`factor` を None にして生ラベルを残す）。
    ここはゲート量ではなく付帯情報であり、arviz の表記が変わったときに本番を止める価値がない。
    """
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return None
    try:
        pos = int(np.nanargmin(arr) if kind == "min" else np.nanargmax(arr))
    except ValueError:      # 全 nan（極値が存在しない）
        return None
    return describe_param(labels[pos], float(arr[pos]), stock_offset)


def describe_param(raw_label, value: float, stock_offset: int = 0) -> dict:
    """`alpha[883]` のようなラベル1件を、銘柄 index・因子 index つきの dict へ解く。

    `locate_extreme`（極値1件）と `locate_worst`（悪い順 上位N件）の共通部分。ラベルの
    解釈に失敗しても落とさない（`stock`/`factor` を None にして生ラベルを残す）——ここは
    ゲート量ではなく付帯情報であり、arviz の表記が変わったときに本番を止める価値がない。
    """
    raw = str(raw_label)
    out = {"label": raw, "param": raw.split("[")[0], "stock": None, "factor": None,
           "value": float(value)}
    m = _PARAM_INDEX_RE.match(raw)
    if not m:
        return out
    dims = [int(p) for p in m.group(2).split(",") if p.strip()]
    if out["param"] == "beta" and len(dims) == 2:
        out["stock"], out["factor"] = dims[0] + int(stock_offset), dims[1]
        out["label"] = "beta[{0}, {1}]".format(out["stock"], out["factor"])
    elif out["param"] == "alpha" and len(dims) == 1:
        out["stock"] = dims[0]
    elif out["param"] == "mu_universe" and len(dims) == 1:
        out["factor"] = dims[0]
    return out


def locate_worst(values, labels, kind: str, k: int = WORST_KEEP,
                 stock_offset: int = 0, idx=None) -> list[dict]:
    """診断値の1ブロックから「悪い順」上位 k 件を拾う（#609）。

    ゲートが p99 を見るようになると、**p99 と max のあいだの個体はゲートを通る**。
    通ったこと自体は設計どおりだが、誰が通ったかを名前で残さないと後から追えない
    （`alpha` は `_intercept` 行として永続化され producer が μ の復元に使う）。

    ラベルは**拾った k 件だけ**を文字列化する。ブロックは本番規模で 6,000 行あり、
    全件を `list(labels)` するのは無駄（`locate_extreme` が1件でそうしているのと同じ理由）。
    `idx` を渡すと `values` / `labels` の一部だけを見る（変数ごとに分けて拾う用）。
    """
    arr = np.asarray(values, dtype=float)
    sel = np.arange(arr.size) if idx is None else np.asarray(idx, dtype=int)
    if sel.size == 0:
        return []
    finite = sel[~np.isnan(arr[sel])]
    if finite.size == 0:
        return []
    vals = arr[finite]
    order = np.argsort(vals if kind == "min" else -vals, kind="stable")[:max(int(k), 0)]
    return [describe_param(labels[int(p)], float(arr[int(p)]), stock_offset)
            for p in finite[order]]


def pick_extreme(current: dict | None, candidate: dict | None, kind: str) -> dict | None:
    """ブロックごとの極値からグローバルの極値を選ぶ（`locate_extreme` の畳み込み）。"""
    if candidate is None:
        return current
    if current is None:
        return candidate
    better = (candidate["value"] < current["value"]) if kind == "min" \
        else (candidate["value"] > current["value"])
    return candidate if better else current


def annotate_extreme(loc: dict | None, edinet_codes=None, factor_names=None) -> dict | None:
    """極値の位置へ銘柄コード・因子名を解決して足す（**畳み込みが終わってから1回だけ**）。

    ラベルだけでは `beta[1234, 3]` が何なのか人には読めない。範囲外・未指定は None のまま
    にする（診断の付帯情報なので、名前が引けないことを失敗にしない）。
    """
    if loc is None:
        return None
    out = dict(loc)
    idx = out.get("stock")
    if edinet_codes is not None and idx is not None and 0 <= idx < len(edinet_codes):
        out["edinet_code"] = str(edinet_codes[idx])
    else:
        out["edinet_code"] = None
    idx = out.get("factor")
    if factor_names is not None and idx is not None and 0 <= idx < len(factor_names):
        out["factor_name"] = str(factor_names[idx])
    else:
        out["factor_name"] = None
    return out


def param_group_stats(r_hat, ess_bulk) -> dict:
    """1変数ぶんの診断統計（極値と分位）を作る（#609）。

    **なぜ変数別に要るのか**: 本番規模の `ess_bulk_min=13.39` も `r_hat_max=1.1156` も、
    49,893個のうち `alpha` の1個が出していた（#600）。`beta`（`macro_beta_loadings` として
    永続化しリスク量に効く）は健全なのに、全変数を混ぜた max/min では両者が区別できない。
    ゲートの定義を「何に対して課すか」から見直すには、変数別の分布が要る。

    分位まで持つのは**測り直しのコストが本番規模で約6時間**だから——極値だけ残して後から
    「分位も見たかった」となると、その6時間をもう一度払うことになる（#600 で実際に払った）。
    """
    r = np.asarray(r_hat, dtype=float)
    e = np.asarray(ess_bulk, dtype=float)
    if r.size == 0 or e.size == 0:
        return {"n": 0}
    return {
        "n":            int(e.size),
        "r_hat_max":    float(np.nanmax(r)),
        "r_hat_p99":    float(np.nanpercentile(r, 99)),
        "r_hat_median": float(np.nanpercentile(r, 50)),
        "ess_bulk_min":    float(np.nanmin(e)),
        "ess_bulk_p1":     float(np.nanpercentile(e, 1)),
        "ess_bulk_p10":    float(np.nanpercentile(e, 10)),
        "ess_bulk_median": float(np.nanpercentile(e, 50)),
    }


def _split_by_param(labels) -> dict:
    """az.summary の index を変数名ごとの位置リストへ分ける（`alpha[3]` → "alpha"）。"""
    groups: dict[str, list[int]] = {}
    for i, lbl in enumerate(labels):
        groups.setdefault(str(lbl).split("[")[0], []).append(i)
    return groups


def _accumulate_param_stats(acc: dict, r_hat, ess_bulk, labels=None) -> None:
    """診断値の1ブロックを変数名ごとに貯める（`labels=None` は beta チャンク＝全行 beta）。

    beta は本番規模で 46,044 行あり、チャンクごとにラベルを文字列分割すると無駄が大きい。
    呼び側が「このブロックは全部 beta」と分かっているときは labels を渡さない。
    """
    r = np.asarray(r_hat, dtype=float)
    e = np.asarray(ess_bulk, dtype=float)
    if labels is None:
        bucket = acc.setdefault("beta", {"r": [], "e": []})
        bucket["r"].append(r)
        bucket["e"].append(e)
        return
    for name, idxs in _split_by_param(labels).items():
        bucket = acc.setdefault(name, {"r": [], "e": []})
        idx = np.asarray(idxs, dtype=int)
        bucket["r"].append(r[idx])
        bucket["e"].append(e[idx])


def _accumulate_worst(acc: dict, r_hat, labels, stock_offset: int = 0,
                      param: str | None = None) -> None:
    """`r_hat` の悪い順 上位 `WORST_KEEP` 件を変数ごとに貯める（#609）。

    `param` を渡すとラベルの変数名分割を省く（beta チャンクは全行 beta と分かっている）。
    ブロックごとに上位 k を取って畳み込むので、全体の上位 k は正しく残る。
    """
    groups = ({param: None} if param is not None
              else {n: np.asarray(i, dtype=int) for n, i in _split_by_param(labels).items()})
    for name, idx in groups.items():
        bucket = acc.setdefault(name, {"r": [], "e": []})
        got = locate_worst(r_hat, labels, "max", stock_offset=stock_offset, idx=idx)
        merged = bucket.setdefault("w", []) + got
        merged.sort(key=lambda d: d["value"], reverse=True)
        bucket["w"] = merged[:WORST_KEEP]


def summarize_diagnostics(idata, sector_idx=None, edinet_codes=None, factor_names=None) -> dict:
    """r_hat・ESS の収束診断サマリ（ADR-0002 検証基準: r_hat<1.01・ESS 十分性・発散遷移数）。

    `round_to="none"` は必須（Issue #356）。az.summary は round_to 省略時、列ごとに固定桁で
    丸める——**r_hat は小数2桁・ess_bulk/ess_tail は整数**（arviz/stats/stats.py の
    `decimals = {col: 3 ... else 2 if col == "r_hat" else 0}`）。r_hat の丸め幅は strict ゲート
    （1.01）と同じ桁なので、丸めたままでは:

    - `persist_allowed` が受け取る r_hat_max は 1.00/1.01/1.02… の3値解像度しか持たず、
      真値 1.0051 も 1.0149 も同じ 1.01 として通る（strict 基準の実体は「真値 < 1.015」）。
    - 収束改善の効果測定ができない。Issue #341/PR#354 の「2/4/6/8 チェーンで r_hat_max=1.0100
      が完全平坦」も、床の証拠ではなく **この2桁丸めの解像度そのもの** だった疑いが強い。

    生値を返すことで、ゲート判定と収束改善の実測（experiment_pooled_rhat.py）の双方が
    ADR-0002 の基準どおりの精度で機能する。

    sector_idx（#541）
    ------------------
    beta は posterior に載らなくなったので、診断も再構成して取る。**ゲートの意味を一切
    変えないこと**が要件——`persist_allowed` の strict 1.01 は beta の r_hat に対して
    較正された値であり、代わりに beta_raw を見ると non-centered の raw パラメータは混合が
    良いぶん r_hat が小さく出て**ゲートが黙って緩くなる**。

    r_hat・ESS はスカラーパラメータごとに独立に計算されるため、銘柄チャンクへ分割して
    max/min を取っても全体と厳密に一致する。これが分割してよいことの根拠。

    sector_idx=None のときは posterior の beta を直接読む従来経路。合成 idata に beta を
    直接注入するテスト（#356 の arviz 丸め回帰検知）はこちらを通る。

    極値の位置（#600）
    ------------------
    `ess_bulk_argmin` / `r_hat_argmax` に「その値を出している母数」を付ける。`edinet_codes` /
    `factor_names` を渡せば銘柄コード・因子名まで解決する（省略すれば index だけ）。

    収束ゲートが読む量（#609）
    --------------------------
    `persist_allowed` が見るのは **`by_param[*]["r_hat_p99"]`**（変数別の p99）であって
    `r_hat_max` ではない。`r_hat_max` / `ess_bulk_min` / `ess_tail_min` / `n_divergences` は
    ログと過去 run との比較のために従来どおりの定義で残す——**定義を変えると 2026-07 以前の
    実測値と並べられなくなる**（#356 で丸め値と生値を混ぜて一度この過ちを踏んでいる）。
    """
    import arviz as az

    diverging = idata.sample_stats.get("diverging") if hasattr(idata, "sample_stats") else None
    n_div = int(diverging.sum()) if diverging is not None else None
    by_param: dict = {}

    if sector_idx is None:
        summ = az.summary(idata, var_names=["beta", "alpha", "mu_universe"], kind="diagnostics",
                          round_to="none")
        r_hat_max, ess_bulk_min, ess_tail_min = (
            float(summ["r_hat"].max()), float(summ["ess_bulk"].min()), float(summ["ess_tail"].min()),
        )
        ess_argmin = locate_extreme(summ["ess_bulk"], summ.index, "min")
        r_hat_argmax = locate_extreme(summ["r_hat"], summ.index, "max")
        _accumulate_param_stats(by_param, summ["r_hat"], summ["ess_bulk"], summ.index)
        _accumulate_worst(by_param, summ["r_hat"], summ.index)
    else:
        post = idata.posterior
        summ = az.summary(idata, var_names=["alpha", "mu_universe"], kind="diagnostics",
                          round_to="none")
        r_hat_max = float(summ["r_hat"].max())
        ess_bulk_min = float(summ["ess_bulk"].min())
        ess_tail_min = float(summ["ess_tail"].min())
        ess_argmin = locate_extreme(summ["ess_bulk"], summ.index, "min")
        r_hat_argmax = locate_extreme(summ["r_hat"], summ.index, "max")
        _accumulate_param_stats(by_param, summ["r_hat"], summ["ess_bulk"], summ.index)
        _accumulate_worst(by_param, summ["r_hat"], summ.index)

        n_stock = post.sizes["stock"]
        mu_sector = _reconstruct_mu_sector(post)
        for lo in range(0, n_stock, BETA_CHUNK_STOCKS):
            hi = min(lo + BETA_CHUNK_STOCKS, n_stock)
            chunk = _reconstruct_beta_chunk(post, sector_idx, lo, hi, mu_sector=mu_sector)
            csumm = az.summary(az.from_dict(posterior={"beta": chunk}), kind="diagnostics",
                               round_to="none")
            r_hat_max = max(r_hat_max, float(csumm["r_hat"].max()))
            ess_bulk_min = min(ess_bulk_min, float(csumm["ess_bulk"].min()))
            ess_tail_min = min(ess_tail_min, float(csumm["ess_tail"].min()))
            # チャンク内のローカル銘柄 index を全体へ直す（lo を足す）。
            ess_argmin = pick_extreme(
                ess_argmin, locate_extreme(csumm["ess_bulk"], csumm.index, "min", lo), "min")
            r_hat_argmax = pick_extreme(
                r_hat_argmax, locate_extreme(csumm["r_hat"], csumm.index, "max", lo), "max")
            _accumulate_param_stats(by_param, csumm["r_hat"], csumm["ess_bulk"])
            _accumulate_worst(by_param, csumm["r_hat"], csumm.index, stock_offset=lo,
                              param="beta")

    return {
        "r_hat_max":     r_hat_max,
        "ess_bulk_min":  ess_bulk_min,
        "ess_tail_min":  ess_tail_min,
        "n_divergences": n_div,
        # 極値“そのもの”ではなく**それを出している母数**（#600）。alpha か beta かで対策が変わる。
        "ess_bulk_argmin": annotate_extreme(ess_argmin, edinet_codes, factor_names),
        "r_hat_argmax":    annotate_extreme(r_hat_argmax, edinet_codes, factor_names),
        # 変数別の極値と分位（#609）。**収束ゲート `persist_allowed` はここの `r_hat_p99` を
        # 読む**（全体の max ではない）。`r_hat_worst` は p99 と max のあいだを通った個体の
        # 名前で、ゲートには効かないがログへ出す。本番規模の再測定は約6時間かかるので
        # **この1回で取り切る**。
        "by_param": {name: {**param_group_stats(np.concatenate(v["r"]), np.concatenate(v["e"])),
                            "r_hat_worst": [annotate_extreme(d, edinet_codes, factor_names)
                                            for d in v.get("w", [])]}
                     for name, v in by_param.items()},
    }


def summarize(idata, selected: list[str], macro_sel: np.ndarray,
              edinet_codes: list[str], sector_idx=None) -> InferenceResult:
    """事後分布から per-stock ローディング平均・SE と Sigma_macro を抽出する。

    sector_idx を渡すと beta を自由 RV から再構成する（#541・posterior に載らないため）。
    銘柄チャンクごとに mean/sd を確定させて捨てるので、(chain, draw, n_stock, factor) を
    一度に実体化しない——ここでまとめて作ると削減した約584MB がそのまま戻る。

    sector_idx=None は posterior に beta がある場合の従来経路。
    """
    post = idata.posterior
    if sector_idx is None:
        beta_mean = post["beta"].mean(dim=("chain", "draw")).values   # (n_stock, n_factor)
        beta_sd = post["beta"].std(dim=("chain", "draw")).values
    else:
        n_stock = post.sizes["stock"]
        n_factor = post.sizes["factor"]
        beta_mean = np.empty((n_stock, n_factor))
        beta_sd = np.empty((n_stock, n_factor))
        mu_sector = _reconstruct_mu_sector(post)
        for lo in range(0, n_stock, BETA_CHUNK_STOCKS):
            hi = min(lo + BETA_CHUNK_STOCKS, n_stock)
            chunk = _reconstruct_beta_chunk(post, sector_idx, lo, hi, mu_sector=mu_sector)
            # ddof は xarray の .std(dim=...) と揃えて 0（numpy の既定と同じ）。
            beta_mean[lo:hi] = chunk.mean(axis=(0, 1))
            beta_sd[lo:hi] = chunk.std(axis=(0, 1))
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


# `persist` が書く meta の列。**推論の前に存在を確かめる**ためだけに持つ（#609）。
PERSIST_META_COLUMNS = ("run_id", "snapshot_date", "selected_factors", "factor_cov",
                        "hyperparams", "status")


def assert_persist_schema(db) -> None:
    """書き込み先が今のコードの列を受け付けるか、**推論を始める前に**確かめる（#609）。

    DDL を打つのは `database.init_db()`（API 起動時に走る）であって、バッチではない。
    だがバッチは DDL 未適用の DB に対しても**6時間走り切ってから** persist で
    「column does not exist」に当たる——結果は返らず、run は丸ごと無駄になる。
    ここは「足りないことを早く言う」役に徹する（`deps_smoke` が重い依存に対してやるのと同じ）。
    """
    from sqlalchemy import inspect

    if db.bind is None:
        return
    have = {c["name"] for c in inspect(db.bind).get_columns("macro_beta_meta")}
    missing = [c for c in PERSIST_META_COLUMNS if c not in have]
    if missing:
        raise SystemExit(
            "中止: macro_beta_meta に列が足りない {0}（DDL が未適用）。"
            "`init_db()` を一度走らせてから再実行すること——"
            "このまま進めても数時間後の persist で落ちて結果が消える。".format(missing)
        )


def persist(db, result: InferenceResult, status: str | None = None) -> None:
    """推論結果を macro_beta_meta / macro_beta_loadings へ upsert する（#214）。

    per-stock 切片は factor_name="_intercept" 行として格納し、producer が μ を復元する。
    スキーマ・upsert 本体は database.upsert_macro_beta（縦持ち・DDL 追加のみ・Supabase 容量軽微）。

    status（#609）
    --------------
    `live` なら producer が読む。`quarantined` は**保全のみ**で `get_macro_beta` からは見えない。
    収束ゲートに落ちた run をここへ書けるようにしたのは、**落とすことと捨てることを分ける**
    ため——2026-09-03 は 6時間かけて完走した run が reject されて丸ごと消えた。省略時は live
    （テストや手動の呼び出しが黙って隔離されないように、明示した側だけが隔離する）。
    """
    from database import MACRO_BETA_STATUS_LIVE, upsert_macro_beta  # 遅延 import

    meta = {
        "run_id":           result.run_id,
        "snapshot_date":    result.snapshot_date,
        "selected_factors": result.selected_factors,
        "factor_cov":       result.factor_cov,
        "hyperparams":      {**(result.hyperparams or {}), "diagnostics": result.diagnostics},
        "status":           status or MACRO_BETA_STATUS_LIVE,
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


def gate_values(diagnostics: dict | None) -> dict[str, float]:
    """収束ゲートが比べる量を診断から取り出す（変数名 → `r_hat_p99`）。

    `by_param` が無い診断（列を足す前の run・診断を手で組んだテスト）では、従来どおり
    全体の `r_hat_max` を1本だけ返す。空 dict は「診断不能＝ゲート対象外」を意味する。
    """
    d = diagnostics or {}
    by_param = d.get("by_param") or {}
    out = {name: float(g["r_hat_p99"]) for name, g in by_param.items()
           if isinstance(g, dict) and g.get("n") and g.get("r_hat_p99") is not None}
    if out:
        return out
    r_hat_max = d.get("r_hat_max")
    return {} if r_hat_max is None else {"r_hat_max": float(r_hat_max)}


def persist_allowed(diagnostics: dict | None, threshold: float, force: bool) -> bool:
    """収束診断に基づく persist 可否判定（純関数・テスト可能に切り出し）。

    persist を許可するのは以下のいずれか:
    - `force=True`（人手で結果を精査した上での強制書き込み）
    - 診断が取れない（ゲート対象外・従来挙動を踏襲）
    - **変数ごとの `r_hat` の p99 が全部 threshold 以下**

    なぜ p99 で、しかも変数別なのか（#609）
    ---------------------------------------
    以前は全パラメータの `r_hat_max` 1本で見ていたが、**max 順序統計はパラメータが増えれば
    必ず上がる**。本番規模は 49,893 個あり、そこから最大を取れば「最も混ざらなかった1個」が
    必ず現れる——実測でも `alpha` のたった1個（26観測しかない平凡な銘柄の切片）が
    `r_hat_max=1.1242` を出し、2026-08-01 から5週間 persist できずに `macro_beta_loadings` が
    固着した。これはモデルの不具合ではなくゲートの定義に由来する（#600）。

    かといって全体の p99 へ緩めると、**本物の崩壊を見逃す**。`max_tree_depth=8` の崩壊ケース
    （ADR-0002）で壊れていたのは `mu_universe`（12個）だけで、本番規模ならそれは全体の
    0.024% ＝ p99 には現れない。

    変数別 × p99 なら両方を満たす（実測は ADR-0002 の #609 節）:

    | ゲート | 本番の run | 崩壊ケース |
    |---|---|---|
    | 全体の max（旧） | 落ちる（1.1242） | reject |
    | 全体の p99 | 通る | **見逃す** |
    | 変数別 × p99 | 通る（beta 1.0193 / alpha 1.0463 / mu 1.0373） | reject（mu 1.6791） |

    この形は**パラメータ数に応じて厳しさが自動的に決まる**——12個の `mu_universe` では
    p99 が実質 max になり、46,044個の `beta` では外れ値1個を無視できる。だから閾値は
    変数共通のまま（規模の関数にしない）でよい。

    ESS はゲートに入れない。実測した2ケース（本番・崩壊）はどちらも `r_hat` だけで正しく
    判定でき、ESS を足しても結論は動かない。「何個あれば十分か」を根拠なく決めた閾値は
    偽の安心になる。変数別 `ess_bulk_p1` は診断へ残してあるので、必要になったら測って決める。

    threshold は既定 1.01（ADR-0002 の strict 基準）。chains=2 のランナーでは r_hat が
    構造的に 1.02 前後で頭打ちになる（PyMC も「信頼できる r_hat には4 chain以上推奨」と
    警告する通り 2 chain では保守的に出る）ため、無人の月次実行では緩和した threshold
    （1.05）を渡す（Issue #341）。

    注記（Issue #356）: 上記「~1.02 で頭打ち」の根拠となった 2026-07 以前の観測値は、
    summarize_diagnostics が az.summary の既定丸め（r_hat は小数2桁）を経ていたときのもので、
    真値ではなく 1.00/1.01/1.02 の3値へ量子化された表示だった。診断は生値へ修正済みのため、
    本関数が受け取る r_hat は現在 4桁精度の実値である。したがって strict 1.01 は文字どおり
    「真値 <= 1.01」を要求する（丸め時代の実効基準は「真値 < 1.015」と緩かった）。閾値の
    再設定は生値での再実測（experiment_pooled_rhat.py）に基づいて判断すること。
    """
    if force:
        return True
    values = gate_values(diagnostics)
    if not values:
        return True
    return max(values.values()) <= threshold


def gate_verdict(diagnostics: dict | None, threshold: float | None = None) -> tuple:
    """ゲートの合否と**それを決めた変数**を返す: `("PASS"|"FAIL"|"n/a", "alpha 1.0463")`。

    格子（`scripts/bench_macro_beta*.py`）が表へ本番の合否を出すために使う（#613）。判定を
    向こうへ書き写さないための共有点で、**ここが唯一の源**——本番と格子で基準がずれたら
    格子を回す意味が無くなる。`threshold` 省略時は無人の月次実行と同じ `MONTHLY_RHAT_THRESHOLD`。

    値だけでは対策が選べないので変数名を併せて返す。`alpha` なら永続化対象ではないので
    ゲートの見方を変える余地があり、`beta` なら `macro_beta_loadings` そのもの＝対策が変わる。
    """
    values = gate_values(diagnostics)
    if not values:
        return "n/a", ""
    th = MONTHLY_RHAT_THRESHOLD if threshold is None else threshold
    name, p99 = max(values.items(), key=lambda kv: kv[1])
    ok = persist_allowed(diagnostics, th, force=False)
    return ("PASS" if ok else "FAIL"), "{0} {1:.4f}".format(name, p99)


def log_gate_report(diagnostics: dict | None, threshold: float) -> None:
    """ゲートが見た量と、その裏に隠れた個体をログへ出す（#609）。

    2つのことを現す:

    1. **余裕**——`p99` が `threshold - PERSIST_MARGIN_WARN` を超えた変数を警告する。
       `alpha` は本番実測で余裕 0.0037 しかなく、銘柄が増えれば縮む。落ちてから気づくと
       6.7時間の run が隔離されるので、落ちる前の回で予告する
    2. **p99 と max のあいだを通った個体**——p99 で判定する以上、`alpha` なら 3,837 個中
       38番目まで悪い値は通る。その切片は `_intercept` 行として永続化され producer が μ の
       復元に使うので、**通ったこと自体は設計どおりでも、誰が通ったかは残す**
    """
    d = diagnostics or {}
    for name, p99 in sorted(gate_values(d).items(), key=lambda kv: -kv[1]):
        if p99 > threshold - PERSIST_MARGIN_WARN:
            logger.warning("収束ゲートの余裕が薄い: %s の r_hat p99=%.4f（threshold %.4f まで %.4f）",
                           name, p99, threshold, threshold - p99)
    for name, group in (d.get("by_param") or {}).items():
        if not isinstance(group, dict):
            continue
        over = [w for w in (group.get("r_hat_worst") or [])
                if w and w.get("value") is not None and w["value"] > threshold]
        if over:
            logger.warning(
                "%s: r_hat が threshold（%.4f）を超えた個体（p99 では通る・上位%d件まで）: %s",
                name, threshold, WORST_KEEP,
                [{"label": w["label"], "edinet_code": w.get("edinet_code"),
                  "factor_name": w.get("factor_name"), "r_hat": round(w["value"], 4)}
                 for w in over],
            )


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
    ap.add_argument("--max-tree-depth", default=None,
                    help="NUTS の軌道長上限（#540）。'8' で一律、'8,10' で warmup だけ 8。"
                         "未指定はサンプラー既定（numpyro は 10）＝現行と同一。"
                         "**下げれば速いが ESS が落ちる**ので、根拠は ADR-0002 の格子実測に依る")
    ap.add_argument("--r-hat-threshold", type=float, default=1.01,
                    help="persist を許可する r_hat の上限（既定 1.01＝ADR-0002 strict 基準）。"
                         "**比べるのは変数ごとの p99**（beta / alpha / mu_universe それぞれ）で、"
                         "全パラメータの最大値ではない（#609＝max はパラメータが増えれば必ず閉じる）。"
                         "chains=2 では r_hat が構造的に ~1.02 で頭打ちのため、無人の月次実行では 1.05 へ"
                         "緩和して構造的 ~1.02 を自動 persist しつつ真の未収束は reject する（Issue #341）。"
                         "なお比較対象の r_hat は #356 で生値化済み（2026-07 以前のログ値は"
                         "arviz の小数2桁丸めを経た表示値なので閾値の根拠に流用しない）")
    ap.add_argument("--force", action="store_true",
                    help="収束診断が threshold（既定は変数別 r_hat p99 <= 1.01）未達でも DB へ persist する"
                         "（既定は拒否。producer に品質ゲートが無く即座にライブ推奨へ反映されるため）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    # 引数の検証は DB へ繋ぐ前に済ませる（数時間の run の入口で落とす方が安い）。
    try:
        max_tree_depth = parse_max_tree_depth(args.max_tree_depth)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    from database import SessionLocal  # 遅延 import（本番と共有のセッションファクトリ）

    db = SessionLocal()
    try:
        # **数時間の推論を始める前に**書き込み先を確かめる（#609）。
        assert_persist_schema(db)
        result = run_inference(draws=args.draws, tune=args.tune, target_accept=args.target_accept,
                               seed=args.seed, db=db, chains=args.chains,
                               nuts_sampler=args.nuts_sampler, init=args.init,
                               max_tree_depth=max_tree_depth)
        logger.info("収束診断: %s", result.diagnostics)
        log_gate_report(result.diagnostics, args.r_hat_threshold)
        if not persist_allowed(result.diagnostics, args.r_hat_threshold, args.force):
            # **落とすことと捨てることを分ける**（#609）。ゲートの役目は「品質の悪い結果が
            # 即ライブ反映されるのを防ぐ」ことであって、6時間の計算を消すことではない。
            # quarantined で書けば producer からは見えないまま結果が残り、後から
            # `get_macro_beta(db, run_id=...)` で読んで精査できる（2026-09-03 は捨てて
            # 測り直しになった）。昇格が要るなら status を live へ更新する。
            from database import MACRO_BETA_STATUS_QUARANTINED

            persist(db, result, status=MACRO_BETA_STATUS_QUARANTINED)
            over = {n: v for n, v in gate_values(result.diagnostics).items()
                    if v > args.r_hat_threshold}
            logger.error(
                "persist を隔離: r_hat の p99 が threshold（<=%.4f）を超えた変数 %s"
                "（n_divergences=%s）。run_id=%s を status=quarantined で保存した"
                "（producer は読まない）。結果は残っているので精査でき、再実行するなら"
                " --force で live として書ける。",
                args.r_hat_threshold,
                {n: round(v, 4) for n, v in sorted(over.items(), key=lambda kv: -kv[1])},
                result.diagnostics.get("n_divergences"), result.run_id,
            )
            # **exit を非0のままにする**のが要点。status を入れたことで隔離は正常終了に見えるが、
            # 「M-1 が更新されていない」は運用上の失敗であり、静かに固着させない（#579 の再来を防ぐ）。
            raise SystemExit(1)
        persist(db, result)
        logger.info("推論完了・DB永続化済み: %d 銘柄 / 因子 %s", len(result.loadings), result.selected_factors)
    finally:
        db.close()


if __name__ == "__main__":
    main()
