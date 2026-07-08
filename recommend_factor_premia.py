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

    momentum 列のみ build_snapshots は生の log return を返す（macro_snapshots._momentum）
    ため、recommend.execute() が実際に重みを掛ける Z スコア済み z_momentum と揃えるべく、
    期間ごとに winsorize→Z スコア化する後処理をここで行う（macro_snapshots.py 自体は
    変更しない）。

    Returns:
        (period_panel: dict[str, tuple[np.ndarray X, np.ndarray y]], factor_names: list[str])
        factor_names は recommend.METRICS から gap_ratio を除いた並び（intercept は含まない）。
        min_companies_per_period 未満の期間は破棄する。
    """
    from plugins.macro_snapshots import build_snapshots, load_data
    from plugins.recommend import METRICS
    from plugins.utils import normalize_transform, winsorize

    fin_metrics = [m for m in METRICS if m not in ("z_momentum", "gap_ratio")]

    prices_by_co, fin_by_co, companies = load_data(db)
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

    mom_idx = factor_names.index("momentum_12m1")
    period_panel: dict = {}
    for ym, pairs in samples_by_ym.items():
        if len(pairs) < min_companies_per_period:
            continue
        X = np.asarray([p[0] for p in pairs], dtype=float)
        y = np.asarray([p[1] for p in pairs], dtype=float)

        # momentum 列のみ期間内で winsorize→Z スコア化（recommend.compute_momentum_z と同じ変換）。
        mom_raw = X[:, mom_idx].tolist()
        wv, _, _ = winsorize(mom_raw)
        mean_ = sum(wv) / len(wv)
        var = sum((v - mean_) ** 2 for v in wv) / (len(wv) - 1) if len(wv) > 1 else 0.0
        sd = var ** 0.5 or 1.0
        X[:, mom_idx] = [normalize_transform(v, mean_, sd, "zscore") for v in mom_raw]

        period_panel[ym] = (X, y)

    if not period_panel:
        raise ValueError(
            f"build_period_panel: min_companies_per_period={min_companies_per_period} を"
            "満たす期間が1つもありません（閾値を下げるか、データ蓄積状況を確認してください）")

    # "momentum_12m1" を "z_momentum" にリネームして recommend.METRICS と一致させる。
    factor_names = ["z_momentum" if f == "momentum_12m1" else f for f in factor_names]
    return period_panel, factor_names


def fama_macbeth_regression(period_panel: dict, factor_names: list[str],
                            maxlags: int = DEFAULT_MAXLAGS) -> FactorPremiaResult:
    """期間ごとの断面 OLS（Fama-MacBeth）→ 係数の時系列平均・Newey-West 標準誤差。

    各期間 ym について、intercept 付き設計行列で `plugins.utils.ols()` を実行し β_t を得る
    （walk_forward_cv_monthly のような複数期間プールではなく、期間ごとに独立した断面回帰）。
    各因子 k の時系列 {β_{k,t}} を定数項のみの OLS に HAC（Newey-West）共分散で回帰することで、
    平均値・補正済み SE・t 統計量が一度に得られる（statsmodels 標準の実装パターン）。
    """
    import statsmodels.api as sm

    from plugins.utils import ols

    n_factor = len(factor_names)
    betas_by_factor: dict[str, list[float]] = {f: [] for f in factor_names}
    used_yms: list[str] = []
    for ym in sorted(period_panel.keys()):
        X, y = period_panel[ym]
        X_design = [[1.0] + row.tolist() for row in X]
        result = ols(X_design, y.tolist())
        if result is None:
            continue
        beta = result["beta"]
        if len(beta) != n_factor + 1:
            continue
        for i, f in enumerate(factor_names):
            betas_by_factor[f].append(beta[i + 1])   # beta[0] は intercept
        used_yms.append(ym)

    n_periods = len(used_yms)
    if n_periods == 0:
        raise ValueError("fama_macbeth_regression: 有効な期間が1つもありません")

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


def compute_factor_premia(db, min_companies_per_period: int = DEFAULT_MIN_COMPANIES_PER_PERIOD,
                          maxlags: int = DEFAULT_MAXLAGS) -> FactorPremiaResult:
    """バッチ本体。build_period_panel → 断面回帰・時系列平均。

    build_period_panel 後に db.commit() する（#269 と同じ配慮。本バッチは MCMC のような
    長時間計算ではないが、読込トランザクションを後続の CPU 計算中に残さない習慣を踏襲）。
    """
    period_panel, factor_names = build_period_panel(db, min_companies_per_period)
    db.commit()
    return fama_macbeth_regression(period_panel, factor_names, maxlags=maxlags)


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
    ap.add_argument("--persist", action="store_true",
                    help="recommend_factor_premia テーブルへ保存する（既定は計算結果の表示のみ）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    from database import SessionLocal

    db = SessionLocal()
    try:
        result = compute_factor_premia(
            db, min_companies_per_period=args.min_companies_per_period, maxlags=args.maxlags)
        logger.info("有効期間数: %d", result.n_periods)
        for f in result.factor_names:
            se = result.newey_west_se[f]
            t = result.t_stat[f]
            logger.info("  %-16s b=%+.4f  NW_se=%s  t=%s",
                       f, result.mean_b[f],
                       f"{se:.4f}" if se is not None else "n/a",
                       f"{t:+.2f}" if t is not None else "n/a")
        if args.persist:
            n = persist(db, result)
            logger.info("recommend_factor_premia へ %d 行 persist 完了（run_id=%s）", n, result.run_id)
        else:
            logger.info("--persist 未指定のため DB 書き込みなし（計算結果の表示のみ）")
    finally:
        db.close()


if __name__ == "__main__":
    main()
