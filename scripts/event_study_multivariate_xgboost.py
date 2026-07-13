"""決算開示イベント駆動モデル フェーズ1（Issue #323）。

フェーズ0（scripts/event_study_disclosure_surprise.py）の単一特徴量(m_pm1)による
弱い有意シグナル(rank-IC 0.031)を受け、多変量XGBoostで強化できるかを検証する。
新規データ収集なし（既存の週次株価＋feature_disclosure.pyのみ）。

特徴量:
- ファンダ（feature_disclosure.pyのスケールフリー特徴量 24個: pm/cost の r_*/f_*/m_*/d_f_*）
- サプライズの相対値2個（m_sales, d_f_salesをf_sales.shift(1)の絶対値でスケール）
- テクニカル（週次近似・#317と同型の制約下）: 開示週の直前週までのトレーリング
  リターン(4/12/26週)・ボラティリティ(12/26週)。開示自体の価格反応をリークしないよう
  「基準週の1つ前」までのデータのみ使用。

ラベル: フェーズ0と同じ基準週からのNw週後フォワードリターン（既定4週）。

評価: plugins/utils.py::walk_forward_cv_monthly（月次ウォークフォワード・
fit_predictにXGBoost注入）→ plugins/macro_snapshots.py::oof_backtest
（rank-IC・quantile spread・hit_rate）。M-1/M-2/M-3と同一の評価関数を再利用。

実行: python scripts/event_study_multivariate_xgboost.py [--forward-weeks 4] [--limit 0]
"""
import argparse
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from dotenv import load_dotenv
load_dotenv()

from database import SessionLocal, StatementDisclosure, StockPriceWeekly  # noqa: E402
from feature_disclosure import build_disclosure_features  # noqa: E402
from plugins.utils import walk_forward_cv_monthly  # noqa: E402
from plugins.macro_gbdt import _make_xgb_fit_predict  # noqa: E402
from plugins.macro_snapshots import oof_backtest  # noqa: E402

FEATURE_NAMES = [
    "r_pm1", "r_pm2", "r_pm3", "f_pm1", "f_pm2", "f_pm3",
    "r_cost1", "r_cost2", "r_cost3", "f_cost1", "f_cost2", "f_cost3",
    "m_pm1", "m_pm2", "m_pm3", "m_cost1", "m_cost2", "m_cost3",
    "d_f_pm1", "d_f_pm2", "d_f_pm3", "d_f_cost1", "d_f_cost2", "d_f_cost3",
    "m_sales_rel", "d_f_sales_rel",
    "ret_4w", "ret_12w", "ret_26w", "vol_12w", "vol_26w",
]


def _load_disclosures(db) -> dict[str, list[dict]]:
    cols = [c.name for c in StatementDisclosure.__table__.columns]
    rows = db.query(StatementDisclosure).all()
    by_company = defaultdict(list)
    for r in rows:
        by_company[r.edinet_code].append({c: getattr(r, c) for c in cols})
    return by_company


def _load_weekly_prices(db) -> dict[str, pd.DataFrame]:
    rows = db.query(
        StockPriceWeekly.edinet_code, StockPriceWeekly.week_start, StockPriceWeekly.close_last
    ).all()
    by_company = defaultdict(list)
    for edinet_code, week_start, close_last in rows:
        by_company[edinet_code].append((week_start, close_last))
    out = {}
    for edinet_code, pairs in by_company.items():
        df = pd.DataFrame(pairs, columns=["week_start", "close_last"]).sort_values("week_start")
        out[edinet_code] = df.reset_index(drop=True)
    return out


def _technical_features(prices: pd.DataFrame, disc_date: str) -> dict | None:
    """開示基準週の直前週までの情報のみでトレーリング特徴量を計算（リーク防止）。"""
    idx = prices["week_start"].searchsorted(disc_date, side="left")
    ref_idx = idx - 1
    if ref_idx < 26:
        return None
    closes = prices["close_last"]
    ref_close = closes.iloc[ref_idx]
    if not ref_close or ref_close <= 0:
        return None

    def _ret(n_weeks):
        base = closes.iloc[ref_idx - n_weeks]
        if not base or base <= 0:
            return np.nan
        return ref_close / base - 1.0

    def _vol(n_weeks):
        window = closes.iloc[ref_idx - n_weeks:ref_idx + 1]
        rets = window.pct_change().dropna()
        return rets.std() if len(rets) > 2 else np.nan

    return {
        "ret_4w": _ret(4), "ret_12w": _ret(12), "ret_26w": _ret(26),
        "vol_12w": _vol(12), "vol_26w": _vol(26),
    }


def _forward_return(prices: pd.DataFrame, disc_date: str, forward_weeks: int) -> float:
    idx = prices["week_start"].searchsorted(disc_date, side="left")
    if idx >= len(prices) or idx + forward_weeks >= len(prices):
        return np.nan
    base = prices["close_last"].iloc[idx]
    fwd = prices["close_last"].iloc[idx + forward_weeks]
    if not base or not fwd or base <= 0:
        return np.nan
    return fwd / base - 1.0


def build_samples(forward_weeks: int, limit: int):
    db = SessionLocal()
    print("開示データ取得中...")
    disclosures = _load_disclosures(db)
    print(f"  {len(disclosures)} 社")

    print("週次株価取得中...")
    prices = _load_weekly_prices(db)
    print(f"  {len(prices)} 社")

    samples_by_ym: dict[str, list[tuple]] = defaultdict(list)
    n_events = 0
    n_companies = 0
    for edinet_code, rows in disclosures.items():
        if limit and n_companies >= limit:
            break
        if edinet_code not in prices:
            continue
        feats = build_disclosure_features(rows)
        if feats.empty:
            continue
        n_companies += 1
        price_df = prices[edinet_code]

        for _, row in feats.iterrows():
            fwd = _forward_return(price_df, row["disc_date"], forward_weeks)
            if pd.isna(fwd):
                continue
            tech = _technical_features(price_df, row["disc_date"])
            if tech is None:
                continue

            f_sales_prev_abs = abs(row["f_sales"]) if pd.notna(row["f_sales"]) and row["f_sales"] != 0 else np.nan
            m_sales_rel = row["m_sales"] / f_sales_prev_abs if pd.notna(f_sales_prev_abs) else np.nan
            d_f_sales_rel = row["d_f_sales"] / f_sales_prev_abs if pd.notna(f_sales_prev_abs) else np.nan

            feat_vals = [row.get(c, np.nan) for c in FEATURE_NAMES[:24]]
            feat_vals += [m_sales_rel, d_f_sales_rel]
            feat_vals += [tech["ret_4w"], tech["ret_12w"], tech["ret_26w"], tech["vol_12w"], tech["vol_26w"]]
            feat_vals = [float(v) if v is not None and not (isinstance(v, float) and math.isnan(v)) else np.nan
                         for v in feat_vals]
            if all(math.isnan(v) for v in feat_vals):
                continue

            ym = row["disc_date"][:7]
            samples_by_ym[ym].append((feat_vals, fwd))
            n_events += 1

    print(f"\n対象企業数: {n_companies}")
    print(f"有効サンプル数: {n_events}")
    print(f"対象月数: {len(samples_by_ym)}")
    return samples_by_ym


def run(forward_weeks: int, limit: int):
    samples_by_ym = build_samples(forward_weeks, limit)
    if sum(len(v) for v in samples_by_ym.values()) < 100:
        print("サンプル不足のため評価をスキップ")
        return

    xgb_params = {
        "max_depth": 3,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "reg_lambda": 1.0,
        "reg_alpha": 0.0,
        "n_estimators": 300,
        "early_stopping_rounds": 30,
        "tree_method": "hist",
        "objective": "reg:squarederror",
        "random_state": 42,
    }
    best_iterations: list[int] = []
    fit_predict = _make_xgb_fit_predict(xgb_params, best_iterations)

    print("\nウォークフォワードCV実行中（多変量XGBoost）...")
    fold_results, residuals_by_ym = walk_forward_cv_monthly(
        samples_by_ym, FEATURE_NAMES,
        min_train_months=8, step_months=1,
        return_residuals=True, fit_predict=fit_predict,
    )
    print(f"フォールド数: {len(fold_results)}")

    if not fold_results:
        print("フォールドが構築できず評価不可（学習月数不足の可能性）")
        return

    result = oof_backtest(residuals_by_ym, n_quantiles=5, cost_bps=0.0)
    print("\n=== 多変量XGBoost OOF評価 ===")
    print(f"n_periods (フォールド数): {result['n_periods']}")
    print(f"n_oof_samples: {result['n_oof_samples']}")
    print(f"rank-IC: mean={result['rank_ic']['mean']}, std={result['rank_ic']['std']}, n={result['rank_ic']['n']}")
    print(f"quantile_returns (低→高μ̂): {result['quantile_returns']}")
    print(f"long_short_spread: {result['long_short_spread']}")
    print(f"hit_rate: {result['hit_rate']}")

    print("\n=== フォールド別 R2/RMSE ===")
    for f in fold_results:
        print(f"  {f['test_ym']}: n_train={f['n_train']:>6} n_test={f['n_test']:>4} r2={f['r2']:>7} rmse={f['rmse']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-weeks", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="対象企業数の上限（0=無制限）")
    args = parser.parse_args()
    run(args.forward_weeks, args.limit)
