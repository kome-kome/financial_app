"""決算開示サプライズ×株価フォワードリターンのイベントスタディ（Issue #323 フェーズ0）。

目的: UKI型イベント駆動モデル（#323）に予測余地があるかを、既存データ
（stock_price_weekly の週次終値＋ feature_disclosure.py のサプライズ特徴量）だけで
低コストに検証する。日足OHLC収集(#290と要調整)やfiling_date整備(#219)を待たずに
実施できるフェーズ0。

方法:
- 各開示(disc_date)について、その週以降で最初に到達する週次終値を「基準終値」とする
  （開示当日はまだ株価に反映されていない可能性があるため、当該週足を起点にする）
- 基準終値から N 週後（既定4週）の終値までのリターンを「フォワードリターン」とする
- サプライズ指標は m_pm1（純利益率の予想対比サプライズ。原典m_npは会社規模で絶対額が
  ばらつくため、比率で規模非依存にした代理指標）
- サプライズとフォワードリターンの Spearman 順位相関（rank-IC）をプールして算出し、
  上位/下位quintileのロング・ショート・スプレッドも参考値として出す

実行: python scripts/event_study_disclosure_surprise.py [--forward-weeks 4] [--limit 0]
"""
import argparse
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy import stats

from dotenv import load_dotenv
load_dotenv()

from database import SessionLocal, StatementDisclosure, StockPriceWeekly  # noqa: E402
from feature_disclosure import build_disclosure_features  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402


def _load_disclosures(db) -> dict[str, list[dict]]:
    def _produce():
        cols = [c.name for c in StatementDisclosure.__table__.columns]
        rows = db.query(StatementDisclosure).all()
        by_company = defaultdict(list)
        for r in rows:
            by_company[r.edinet_code].append({c: getattr(r, c) for c in cols})
        return by_company

    return cached("disclosures_all", _produce)


def _load_weekly_prices(db) -> dict[str, pd.DataFrame]:
    def _produce():
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

    return cached("weekly_prices_close", _produce)


def _forward_return(prices: pd.DataFrame, disc_date: str, forward_weeks: int) -> float:
    """disc_date以降で最初の週足を基準に、forward_weeks週後の終値までのリターン。"""
    idx = prices["week_start"].searchsorted(disc_date, side="left")
    if idx >= len(prices) or idx + forward_weeks >= len(prices):
        return np.nan
    base = prices["close_last"].iloc[idx]
    fwd = prices["close_last"].iloc[idx + forward_weeks]
    if base is None or fwd is None or base == 0:
        return np.nan
    return fwd / base - 1.0


def run(forward_weeks: int, limit: int) -> None:
    db = SessionLocal()
    print("開示データ取得中...")
    disclosures = _load_disclosures(db)
    print(f"  {len(disclosures)} 社")

    print("週次株価取得中...")
    prices = _load_weekly_prices(db)
    print(f"  {len(prices)} 社")

    pairs = []  # (m_pm1, forward_return, disc_date, edinet_code)
    n_companies = 0
    for edinet_code, rows in disclosures.items():
        if limit and n_companies >= limit:
            break
        if edinet_code not in prices:
            continue
        feats = build_disclosure_features(rows)
        if feats.empty or "m_pm1" not in feats:
            continue
        n_companies += 1
        price_df = prices[edinet_code]
        for _, row in feats.iterrows():
            surprise = row["m_pm1"]
            if pd.isna(surprise):
                continue
            fwd = _forward_return(price_df, row["disc_date"], forward_weeks)
            if pd.isna(fwd):
                continue
            pairs.append((surprise, fwd, row["disc_date"], edinet_code))

    print(f"\n対象企業数: {n_companies}")
    print(f"有効サンプル数（サプライズ×フォワードリターンとも欠損なし）: {len(pairs)}")

    if len(pairs) < 30:
        print("サンプル不足のため統計評価をスキップ")
        return

    df = pd.DataFrame(pairs, columns=["m_pm1", "fwd_return", "disc_date", "edinet_code"])

    rho, pval = stats.spearmanr(df["m_pm1"], df["fwd_return"])
    print(f"\n[全期間プール] rank-IC(Spearman) = {rho:.4f}  (p={pval:.4g}, n={len(df)})")

    df["year"] = df["disc_date"].str[:4]
    print("\n年別 rank-IC:")
    for year, g in df.groupby("year"):
        if len(g) < 20:
            continue
        r, p = stats.spearmanr(g["m_pm1"], g["fwd_return"])
        print(f"  {year}: rank-IC={r:.4f} (p={p:.4g}, n={len(g)})")

    df["quintile"] = pd.qcut(df["m_pm1"], 5, labels=False, duplicates="drop")
    q_means = df.groupby("quintile")["fwd_return"].mean()
    print("\nquintile別 平均フォワードリターン（0=サプライズ最低 → 4=最高）:")
    print(q_means.to_string())
    if len(q_means) >= 2:
        spread = q_means.iloc[-1] - q_means.iloc[0]
        print(f"\nロング・ショート・スプレッド（上位quintile−下位quintile）: {spread:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-weeks", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="対象企業数の上限（0=無制限）")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="本番DBから再取得してローカルキャッシュを更新（既定=キャッシュ優先・Egress削減）")
    args = parser.parse_args()
    set_refresh(args.refresh_cache)
    run(args.forward_weeks, args.limit)
