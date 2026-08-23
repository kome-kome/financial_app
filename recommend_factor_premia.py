"""recommend の Fama-MacBeth 断面回帰バッチ（ローカル専用CLI）。

Issue #271: recommend の4プリセット重みは根拠のないヒューリスティック（docs/MODELS.md §6
「仮定・限界」に自己申告済み）。Fama & MacBeth (1973) の断面回帰でファクタープレミアムを
推定し、データ駆動の重みを「統計的最適化」プリセットとして提供する。

役割
----
各月末スナップショットについて cross-sectional OLS

    return_i,t+52w = Σ_k b_k,t・z_k,i,t + e_i,t

を実行し、時系列平均 b_k = mean(b_k,t) をプリセット重みとする。52週先リターンを毎月
ずらして観測するオーバーラップに起因する自己相関は Newey-West（HAC）標準誤差で補正する。

`plugins/utils.py::walk_forward_cv_monthly`（M-1 が使う pooled panel OLS＝複数月をプール
して単一の OLS を学習）とは異なり、期間ごとに別々の断面 OLS を行う点が Fama-MacBeth の
本質（詳細は docs/adr/0008-recommend-factor-premia-fama-macbeth.md）。

母集団・目的変数・fold は M-1/M-2/M-3 と共有する。`plugins.macro_snapshots.build_snapshots`
を無改修で再利用し（recommend の指標を fin_features として渡すだけ）、月末 cadence・
52週先 log return 目的変数・公表ラグ fill-forward をそのまま流用する。gap_ratio は
sector_ols 依存で2025年度以前ほぼ0%充足のため回帰対象から除外する（詳細は
build_period_panel の docstring・ADR-0008）。

永続化は macro_beta_inference.py に倣う producer/consumer 分離（本バッチ→
recommend_factor_premia テーブル→ plugins.recommend.resolve_weights() が読む）。

実行
----
    python recommend_factor_premia.py --persist
    python recommend_factor_premia.py --estimator ridge   # 共線性の比較・診断（persist 不可）
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger("recommend_factor_premia")

DEFAULT_MIN_COMPANIES_PER_PERIOD = 30
DEFAULT_MAXLAGS = 11   # 52週(≈12ヶ月)のオーバーラップに対する経験則（lag = horizon - 1）


@dataclass
class FactorPremiaResult:
    """回帰バッチの成果物。persist() で DB へ書き出す単位。"""
    run_id: str
    factor_names: list[str]
    mean_b: dict[str, float]
    newey_west_se: dict[str, float | None]
    t_stat: dict[str, float | None]
    p_value: dict[str, float | None]
    n_periods: int
    per_period_betas: dict[str, list[float]] = field(default_factory=dict)   # 診断用（persist しない）
    estimator: str = "ols"                                   # 第1段階の推定手法（診断用）
    condition_numbers: list[float] = field(default_factory=list)   # 期別の設計行列条件数（診断用）


def build_period_panel(db, min_companies_per_period: int = DEFAULT_MIN_COMPANIES_PER_PERIOD) -> tuple:
    """月末スナップショットごとの横断面パネルを構築する（Fama-MacBeth 用）。

    `plugins.macro_snapshots.build_snapshots` を無改修で再利用する。fin_features に
    recommend の指標（後述の理由で gap_ratio・z_momentum を除く7指標）を渡すことで、
    M-1/M-2/M-3 と同一の月末 cadence・52週先 log return 目的変数・fill-forward 済み
    財務データを共有する（Issue #271 要求）。

    **gap_ratio は回帰の特徴量から除外する**（実データ検証で判明・ADR-0008）。
    `gap_ratio` は sector_ols の回帰結果に依存するが、本番DBでは 2020〜2024年度が
    0%・2025年度以降で初めて 67%超という極端な分布だった（sector_ols が直近年度しか
    遡及計算されていないため）。build_snapshots の fin_features は全指標が同時に非NULL
    という条件のため、gap_ratio を含めると 2025年度の財務データが適用可能になる直近
    2ヶ月分の月末スナップショットしか有効サンプルが残らず、Fama-MacBeth の時系列平均・
    Newey-West補正が統計的に無意味になる（実測: 有効期間2、係数が非現実的な値に発散）。
    他7指標は2020年以降96〜100%の充足率があり、gap_ratio を除くことで60ヶ月超の
    期間数を確保できる。「統計的最適化」プリセットはこの7指標＋z_momentumの重みのみを
    持ち、gap_ratio の重みは持たない（recommend.execute() 側は未指定キーを0重み相当として
    自然に無視するため、コード変更は不要）。

    momentum 列は build_snapshots が生の log return を返す（macro_snapshots._momentum）。
    **ここでは標準化せず生値のまま渡す**（Issue #519）。#509 で ols 経路も
    `fit_feature_columns` を通すようになったため、ここで先に winsorize→Z スコア化すると
    p1-p99 クリップが二重に掛かり、この1因子だけ推定時と適用時（`compute_momentum_z`）で
    単位が食い違う。8因子とも `fama_macbeth_regression` の期内前処理へ一様に委ねる。

    Returns:
        (period_panel: dict[str, tuple[np.ndarray X, np.ndarray y]], factor_names: list[str])
        factor_names は recommend.METRICS から gap_ratio と mu（RUNTIME_METRICS の μ̂）を
        除いた並び（intercept は含まない）。
        min_companies_per_period 未満の期間は破棄する。
    """
    from plugins.macro_snapshots import build_snapshots, load_data
    from plugins.recommend import METRICS, RUNTIME_METRICS

    # RUNTIME_METRICS（z_momentum / mu）は財務パネルの列ではない。z_momentum は build_snapshots
    # が momentum_12m1 として別途組み込み（下でリネーム）、mu は producer 由来で断面回帰の
    # 説明変数にならないため、どちらも fin_features から外す（Issue #423 子4）。
    fin_metrics = [m for m in METRICS if m not in RUNTIME_METRICS and m != "gap_ratio"]

    # px_* を使わない（build_snapshots に price_features を渡さない）ので volume_sum は引かない
    # （Issue #446）。
    prices_by_co, fin_by_co, companies = load_data(db, with_volume=False)
    if not prices_by_co:
        raise ValueError("build_period_panel: 株価週次履歴がありません。先に収集を実行してください。")

    samples_by_ym, _meta, _current, factor_names, _stock_ids = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache={},
        fin_features=fin_metrics, macro_names=[],
        use_momentum=True, mom_window=12, min_coverage=0.0,
        build_interactions=False, macro_nan_ok=False,
        return_stock_ids=True,
    )
    if not samples_by_ym:
        raise ValueError(
            "build_period_panel: 有効なサンプルがありません（財務・株価データの蓄積状況を確認してください）")

    # momentum 列だけを個別に winsorize→Z スコア化する処理は **持たない**（Issue #519）。
    # #509 で ols 経路も `fit_feature_columns` を通すようになり、全列が p1-p99 クリップ＋標準化を
    # 受けるため、ここで先に標準化すると **1回目が意図的に残した裾を2回目の p1-p99 が切る**
    # ＝この1因子だけ推定時と適用時で単位が食い違っていた。生値のまま渡し、8因子すべてを
    # `fit_feature_columns` に一様に通す。
    period_panel: dict = {}
    for ym, pairs in samples_by_ym.items():
        if len(pairs) < min_companies_per_period:
            continue
        X = np.asarray([p[0] for p in pairs], dtype=float)
        y = np.asarray([p[1] for p in pairs], dtype=float)
        period_panel[ym] = (X, y)

    if not period_panel:
        raise ValueError(
            f"build_period_panel: min_companies_per_period={min_companies_per_period} を"
            "満たす期間が1つもありません（閾値を下げるか、データ蓄積状況を確認してください）")

    # "momentum_12m1" を "z_momentum" にリネームして recommend.METRICS と一致させる。
    factor_names = ["z_momentum" if f == "momentum_12m1" else f for f in factor_names]
    return period_panel, factor_names


def average_premia(betas_by_factor: dict[str, list[float]], factor_names: list[str],
                   n_periods: int, maxlags: int = DEFAULT_MAXLAGS) -> FactorPremiaResult:
    """期別係数 {β_{k,t}} の時系列平均を Newey-West（HAC）補正付きで畳む。

    Fama-MacBeth の「第2段階」だけを切り出した純関数。各因子 k の時系列 {β_{k,t}} を
    定数項のみの OLS に HAC 共分散で回帰することで、平均値・補正済み SE・t 統計量・p 値が
    一度に得られる（statsmodels 標準の実装パターン）。

    第1段階（期別の断面回帰）の推定手法から独立しているため、OLS 版
    （`fama_macbeth_regression`）だけでなく、共線性へ強い Ridge 版の断面回帰
    （`plugins/model_candidates.py` の Fama-MacBeth 予測ヘッド・Issue #372）からも
    同じ第2段階を共有できる（HAC 補正ロジックの二重化を避ける）。
    """
    import statsmodels.api as sm

    if n_periods == 0:
        raise ValueError("average_premia: 有効な期間が1つもありません")

    mean_b: dict[str, float] = {}
    newey_west_se: dict[str, float | None] = {}
    t_stat: dict[str, float | None] = {}
    p_value: dict[str, float | None] = {}
    for f in factor_names:
        series = np.asarray(betas_by_factor[f], dtype=float)
        if len(series) > 1:
            hac = sm.OLS(series, np.ones(len(series))).fit(
                cov_type="HAC", cov_kwds={"maxlags": min(maxlags, len(series) - 1)})
            mean_b[f] = float(hac.params[0])
            newey_west_se[f] = float(hac.bse[0])
            t_stat[f] = float(hac.tvalues[0])
            p_value[f] = float(hac.pvalues[0])
        else:
            mean_b[f] = float(series[0]) if len(series) else 0.0
            newey_west_se[f] = None
            t_stat[f] = None
            p_value[f] = None

    run_id = datetime.now(timezone.utc).strftime("rfp_%Y%m%dT%H%M%SZ")
    return FactorPremiaResult(
        run_id=run_id, factor_names=factor_names, mean_b=mean_b,
        newey_west_se=newey_west_se, t_stat=t_stat, p_value=p_value,
        n_periods=n_periods, per_period_betas=betas_by_factor,
    )


def _cross_section_condition_number(X_norm: list[list[float]]) -> float:
    """断面設計行列（winsorize→zscore 済み・切片列付き）の条件数。

    共線性を推定手法に依らず同じ尺度で見るための診断値（Issue #469 検証1）。生スケールの
    まま測ると単位差だけで条件数が跳ねるため、`fit_feature_columns` の標準化後に測る。
    `estimator` に関わらず同じ値になるので ols / ridge の比較にそのまま使える
    （`ridge_regression` は条件数を NaN で返すため、両者を並べるにはここで測り直すしかない）。

    **#509 以前は「ここで測る値」と「OLS が実際に解く行列」が別物だった**——是正前の ols は
    生スケールを解いており、median 3.4（標準化後）に対し実際は median 53.1 / max 1,880。
    ログの値だけ見て「共線性は無い」と誤読した前例がある。現在は両 estimator とも
    `fit_feature_columns` を通すので、この値が実際に解かれる行列の条件数と一致する。

    引数は **呼び出し側が既に算出した正規化済み設計行列**（切片列付き）。ここで
    `fit_feature_columns` を呼び直すと ols 経路の前処理が1期あたり2回走る（Issue #519）。
    """
    try:
        return float(np.linalg.cond(np.asarray(X_norm, dtype=float)))
    except np.linalg.LinAlgError:
        return float("nan")


def fama_macbeth_regression(period_panel: dict, factor_names: list[str],
                            maxlags: int = DEFAULT_MAXLAGS,
                            estimator: str = "ols") -> FactorPremiaResult:
    """期間ごとの断面回帰（Fama-MacBeth）→ 係数の時系列平均・Newey-West 標準誤差。

    各期間 ym について β_t を推定する（walk_forward_cv_monthly のような複数期間プールでは
    なく、期間ごとに独立した断面回帰）。第2段階（HAC 補正付き時系列平均）は `average_premia()`
    が担い、`estimator` に依らず共有する。

    **両 estimator とも期内 winsorize→zscore を通す**（Issue #509 で ols 側を是正）。
    `fit_feature_columns` が p1-p99 のクリップと標準化を担い、CLAUDE.md の設計制約
    「OLS 学習前に各特徴量を winsorize」をここでも守る。よって係数の単位は **「1sd あたり」**。

    是正前の ols は VIEW の `z_*` を生スケールのまま解いており、共通分母 `pl_revenue` が
    ゼロ近傍の1社が `z_op_margin` / `z_cf_ratio` を支配して（実測 r=+0.9993）巨大な係数を
    生んでいた（`z_eps` = −3.34 で p=0.29・唯一有意な `z_revenue` の 239倍）。#469 の実測では
    **winsorize+標準化した素の OLS が Ridge とほぼ同じ答えを出す**（`z_eps` −3.3367 → +0.0175、
    `z_op_margin` +0.4830 → −0.0003）＝効いていたのは L2 ではなく前処理だった。

    `estimator`:
      - `"ols"`（既定・Fama & MacBeth 1973 に忠実）: `fit_feature_columns` の設計行列
        （先頭が intercept 列）で `plugins.utils.ols()` を実行する。
      - `"ridge"`: 第1段階だけ `plugins.utils.ridge_regression`（RidgeCV・L2）へ差し替える
        （Issue #469）。ADR-0021 は `scripts/candidate_bakeoff.py` で λ̄ 最大 5.37→0.027・
        OOF rank-IC −0.0131→0.1653 の回復を実測しており、その追試用の口として残している。
        **ridge だけは切片列を渡さない**（実装は `plugins/model_candidates.py` の Fama-MacBeth
        ヘッドを踏襲）: `ridge_regression` は `fit_intercept=False` で切片列も**罰則対象**に
        するため、切片が 0 方向へ縮んだ分を傾きが吸収して λ_t が歪む。標準化で特徴量の平均は
        0 なので、y を期内平均で中心化すれば切片は構造的に 0 となり、傾きだけを正しく縮小推定
        できる。ols は罰則が無いのでこの配慮は要らず、intercept 列をそのまま使う。
    """
    if estimator not in ("ols", "ridge"):
        raise ValueError(f"estimator は 'ols' か 'ridge': {estimator}")

    from plugins.utils import fit_feature_columns, ols, ridge_regression

    n_factor = len(factor_names)
    betas_by_factor: dict[str, list[float]] = {f: [] for f in factor_names}
    used_yms: list[str] = []
    cond_numbers: list[float] = []
    for ym in sorted(period_panel.keys()):
        X, y = period_panel[ym]
        if len(X) == 0:
            # 行が1つも無い断面はここで弾く（Issue #518）。`fit_feature_columns` 側で中立な
            # パラメータを返して救うと、params を保持する別の呼び出し側で「どんな入力も 0.0」
            # という silent-wrong を作る。共有ヘルパは fail-fast のままにする。
            continue
        # 期内 winsorize→zscore（Issue #509）。Xn の先頭は intercept 列。
        # estimator に依らず1回だけ算出し、条件数の診断にも使い回す（Issue #519）。
        Xn, _, _ = fit_feature_columns(X.tolist(), n_factor)
        if estimator == "ols":
            result = ols(Xn, y.tolist())
            n_expected = n_factor + 1
            offset = 1          # beta[0] は intercept
        else:
            X_std = [row[1:] for row in Xn]          # 切片列を落とす（上記の理由）
            y_arr = np.asarray(y, dtype=float)
            result = ridge_regression(X_std, (y_arr - y_arr.mean()).tolist(), cv_folds=3)
            n_expected = n_factor
            offset = 0
        if result is None:
            continue
        beta = result["beta"]
        if len(beta) != n_expected:
            continue
        for i, f in enumerate(factor_names):
            betas_by_factor[f].append(beta[i + offset])
        cond_numbers.append(_cross_section_condition_number(Xn))
        used_yms.append(ym)

    if not used_yms:
        raise ValueError("fama_macbeth_regression: 有効な期間が1つもありません")
    result = average_premia(betas_by_factor, factor_names, len(used_yms), maxlags=maxlags)
    result.estimator = estimator
    result.condition_numbers = cond_numbers
    return result


def compute_factor_premia(db, min_companies_per_period: int = DEFAULT_MIN_COMPANIES_PER_PERIOD,
                          maxlags: int = DEFAULT_MAXLAGS,
                          estimator: str = "ols") -> FactorPremiaResult:
    """バッチ本体。build_period_panel → 断面回帰・時系列平均。

    build_period_panel 後に db.commit() する（#269 と同じ配慮。本バッチは MCMC のような
    長時間計算ではないが、読込トランザクションを後続の CPU 計算中に残さない習慣を踏襲）。
    """
    period_panel, factor_names = build_period_panel(db, min_companies_per_period)
    db.commit()
    return fama_macbeth_regression(period_panel, factor_names, maxlags=maxlags,
                                   estimator=estimator)


def persist(db, result: FactorPremiaResult) -> int:
    """回帰結果を recommend_factor_premia へ upsert する。"""
    from database import upsert_recommend_factor_premia

    rows = [
        {
            "run_id": result.run_id,
            "factor_name": f,
            "mean_b": result.mean_b[f],
            "newey_west_se": result.newey_west_se[f],
            "t_stat": result.t_stat[f],
            "p_value": result.p_value[f],
            "n_periods": result.n_periods,
        }
        for f in result.factor_names
    ]
    n = upsert_recommend_factor_premia(db, result.run_id, rows)
    db.commit()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="recommend の Fama-MacBeth 断面回帰バッチ")
    ap.add_argument("--min-companies-per-period", type=int, default=DEFAULT_MIN_COMPANIES_PER_PERIOD,
                    help="この社数未満の月は断面OLSから除外する（既定30）")
    ap.add_argument("--maxlags", type=int, default=DEFAULT_MAXLAGS,
                    help="Newey-West補正のラグ数（既定11＝52週先リターンのオーバーラップ月数-1）")
    ap.add_argument("--estimator", choices=("ols", "ridge"), default="ols",
                    help="第1段階（期別の断面回帰）の推定手法。既定 ols＝従来と同一。"
                         "ridge は共線性診断・比較用で --persist と併用できない（Issue #469）")
    ap.add_argument("--persist", action="store_true",
                    help="recommend_factor_premia テーブルへ保存する（既定は計算結果の表示のみ）")
    args = ap.parse_args()

    # ridge の結果を書くと `get_latest_factor_premia` が最新 run_id を読む仕様上、昇格ゲート
    # （ADR-0028: 増減どちらの向きも補正後 α を通る実測）を通さないまま「統計的最適化」
    # プリセットの中身が変わってしまう。DB へ触る前に落とす。
    if args.estimator == "ridge" and args.persist:
        raise SystemExit(
            "--estimator ridge と --persist は併用できません（Issue #469）。ridge は比較・診断用で、"
            "既定プリセットの重みを入れ替えるには ADR-0028 の昇格ゲートを通す必要があります。")

    logging.basicConfig(level=logging.INFO)
    from database import SessionLocal

    db = SessionLocal()
    try:
        result = compute_factor_premia(
            db, min_companies_per_period=args.min_companies_per_period, maxlags=args.maxlags,
            estimator=args.estimator)
        logger.info("有効期間数: %d（estimator=%s）", result.n_periods, result.estimator)
        if result.condition_numbers:
            conds = np.asarray(result.condition_numbers, dtype=float)
            logger.info("断面設計行列の条件数: median=%.1f max=%.1f（標準化後・共線性診断）",
                        float(np.nanmedian(conds)), float(np.nanmax(conds)))
        for f in result.factor_names:
            se = result.newey_west_se[f]
            t = result.t_stat[f]
            p = result.p_value[f]
            logger.info("  %-16s b=%+.4f  NW_se=%s  t=%s  p=%s",
                       f, result.mean_b[f],
                       f"{se:.4f}" if se is not None else "n/a",
                       f"{t:+.2f}" if t is not None else "n/a",
                       f"{p:.3f}" if p is not None else "n/a")
        logger.info("※ 係数は期内 winsorize→標準化後＝「1sd あたり」（#509 で ols も揃えた）。"
                    "ridge との差は L2 の有無だけなので、そのまま並べて比較できる")
        if args.persist:
            n = persist(db, result)
            logger.info("recommend_factor_premia へ %d 行 persist 完了（run_id=%s）", n, result.run_id)
        else:
            logger.info("--persist 未指定のため DB 書き込みなし（計算結果の表示のみ）")
    finally:
        db.close()


if __name__ == "__main__":
    main()
