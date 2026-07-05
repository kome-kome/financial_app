"""バックテスト分析（プリセット・スコアリング上位 N 社の実績リターン計算）。

api.py の routing から引き上げた分析ロジック。interface は `(db, params) -> dict` で、
FastAPI app に依存しないため HTTP 往復なしで直接テストできる（tests/test_backtest.py）。
価格取得は database のヘルパ（prices_on_or_after / latest_prices）に集約。
"""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import FinancialMetric, prices_on_or_after, latest_prices
from plugins.recommend import PRESETS, compute_momentum_z
from plugins.net_cash_analysis import compute_net_cash, compute_nc_ratio

# 複数保有期間バックテスト（/api/backtest/multi）の保有月数。
MULTI_PERIODS = [3, 6, 12, 18, 24]

# バックテストで検証できるスコアリング手法（ランキングを出す一次分析を as-of で再現する）。
# 買い系（recommend/valuation/net_cash）は「スコアが高いほど買い候補」で、上位 N 社の
# その後リターンがベンチマークを上回れば有効。sell は双対（買い系の逆観点）で、上位 N 社＝
# 最も売り向きの銘柄。**sell は超過収益が負（＝下回る）ほど売りシグナルが有効**と解釈する。
#   recommend : recommend のプリセット加重和（z_roe 等）
#   valuation : バリュエーション分析の期待総リターン（gap_ratio + 配当利回り）
#   net_cash  : 清原式ネットキャッシュ比率
#   sell      : 売り候補（recommend 加重和の符号反転＝買い系スコアの逆観点・メタ×双対）
SCORING_SOURCES = ("recommend", "valuation", "net_cash", "sell")

# 配当利回りの異常値ガード（％）。gap_analysis（バリュエーション分析）と整合。
_DIV_YIELD_CAP = 30.0


def score_record(r, source: str, weights: dict, momentum_z: dict | None = None) -> float | None:
    """1レコードのスコア（高いほど買い候補）。算出不能なら None（候補から除外）。

    各 source は financial_metrics VIEW の as-of スナップショット（FinancialMetric）から
    一次分析のランキングキーを再現する。recommend のみ preset 加重を使う。

    momentum_z: {edinet_code: z} 形式の事前計算済み z_momentum（compute_momentum_z）。
    weights に z_momentum が含まれる場合のみ呼び出し側が渡す（他 source では未使用）。
    """
    if source == "valuation":
        # 期待総リターン[%] = gap_ratio[%] + 配当利回り[%]（gap_ratio 必須＝sector_ols 実行済み年度のみ）
        if r.gap_ratio is None:
            return None
        dy = float(r.div_yield) if r.div_yield is not None else 0.0
        if dy > _DIV_YIELD_CAP:
            dy = 0.0
        return float(r.gap_ratio) + dy
    if source == "net_cash":
        # 清原式ネットキャッシュ比率 = (流動資産 + 投資有価証券×0.7 − 総負債) / 時価総額
        nc = compute_net_cash(r.bs_current_assets, r.bs_investment_securities,
                              r.bs_total_liabilities)
        return compute_nc_ratio(nc, r.market_cap)
    # recommend（既定）: プリセット加重和。sell は同一加重の符号反転（買い系の逆観点）。
    score, has_any = 0.0, False
    for metric, weight in weights.items():
        if metric == "z_momentum":
            val = (momentum_z or {}).get(r.edinet_code)
        else:
            val = getattr(r, metric, None)
        if val is not None:
            score += weight * val
            has_any = True
    if not has_any:
        return None
    return -score if source == "sell" else score


def percentile(sorted_arr: list, p: float) -> float:
    """pパーセンタイル値（0〜100）。numpy.percentile（線形補間）を使用。"""
    n = len(sorted_arr)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_arr[0])
    import numpy as np
    return float(np.percentile(sorted_arr, p, method="linear"))


def run(
    db: Session,
    preset_name: str,
    months_ago: int,
    top_n: int,
    industry: Optional[str],
    min_market_cap: Optional[float],
    source: str = "recommend",
) -> dict:
    """バックテストを1期間分実行してdictを返す（例外はそのまま伝播）。

    source で検証対象の一次分析を切替（recommend / valuation / net_cash）。
    仕組み（as-of スコア→上位N社→実現リターン→ベンチマーク超過）は source 非依存。
    """
    if source not in SCORING_SOURCES:
        raise ValueError(
            f"未知の scoring source: {source!r}（{', '.join(SCORING_SOURCES)} のいずれか）"
        )
    weights = PRESETS.get(preset_name, PRESETS["バランス型"])
    today = date.today()
    start_date = today - timedelta(days=months_ago * 30)
    start_date_str = start_date.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    # スコア指標（z_roe / z_op_margin / gap_ratio 等）は financial_metrics VIEW が算出する
    # 派生値のため、FinancialRecord ではなく読み取りモデル FinancialMetric を引く。
    subq = (
        db.query(FinancialMetric.edinet_code,
                 func.max(FinancialMetric.year).label("max_year"))
        .filter(FinancialMetric.period_end <= start_date)
        .group_by(FinancialMetric.edinet_code)
        .subquery()
    )
    query = (
        db.query(FinancialMetric)
        .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                    (FinancialMetric.year == subq.c.max_year))
        .filter(FinancialMetric.period_end <= start_date)
    )
    if industry:
        query = query.filter(FinancialMetric.industry == industry)
    if min_market_cap is not None:
        query = query.filter(FinancialMetric.market_cap >= float(min_market_cap))
    records = query.all()

    # z_momentum は VIEW 外の実行時計算（compute_momentum_z）。as-of日=start_date_str で
    # 参照するため、start_date より後の価格変動はスコアに影響しない（リークセーフ）。
    momentum_z: dict = {}
    if "z_momentum" in weights:
        momentum_z = compute_momentum_z(
            db, [r.edinet_code for r in records if r.edinet_code], start_date_str)

    best: dict = {}
    for r in records:
        score = score_record(r, source, weights, momentum_z)
        if score is None:
            continue
        if r.edinet_code not in best or r.period_end > best[r.edinet_code][1].period_end:
            best[r.edinet_code] = (score, r)

    scored = sorted(best.values(), key=lambda x: x[0], reverse=True)
    if not scored:
        return {
            "start_date": start_date_str, "end_date": today_str,
            "holding_months": months_ago, "top_n": top_n, "preset": preset_name,
            "source": source,
            "summary": None, "results": [], "total_candidates": 0,
            "message": f"{start_date_str} 時点の財務データが見つかりませんでした",
        }

    top = scored[:top_n]
    bench_limit = min(500, len(scored))
    bench_codes = [r.edinet_code for _, r in scored[:bench_limit]]

    # エントリー=start_date 以降の最初の終値（daily窓内なら日次・古ければ週次へ自動切替）。
    # イグジット="now"=最新終値（daily優先）。価格取得は database のヘルパに集約。
    sp_all = prices_on_or_after(db, bench_codes, start_date_str)
    ep_all = latest_prices(db, bench_codes)

    results = []
    for rank, (score, r) in enumerate(top, 1):
        c = r.edinet_code
        sp = sp_all.get(c)
        ep = ep_all.get(c)
        if (sp and ep and sp["price"] and ep["price"]
                and sp["date"] < ep["date"]):
            ret_pct = round((ep["price"] - sp["price"]) / sp["price"] * 100, 2)
        else:
            ret_pct = None
        results.append({
            "rank":           rank,
            "edinet_code":    c,
            "sec_code":       r.sec_code or "",
            "company_name":   r.company_name or "",
            "industry":       r.industry or "",
            "score":          round(score, 3),
            "year":           r.year,
            "period_end":     r.period_end.isoformat() if r.period_end else None,
            "start_price":    sp["price"] if sp else None,
            "start_date":     sp["date"]  if sp else None,
            "end_price":      ep["price"] if ep else None,
            "end_date":       ep["date"]  if ep else None,
            "return_pct":     ret_pct,
            "has_price_data": ret_pct is not None,
        })

    bench_returns = [
        (ep_all[c]["price"] - sp_all[c]["price"]) / sp_all[c]["price"] * 100
        for c in bench_codes
        if (c in sp_all and c in ep_all
            and sp_all[c]["price"] and ep_all[c]["price"]
            and sp_all[c]["date"] < ep_all[c]["date"])
    ]

    valid = [r["return_pct"] for r in results if r["return_pct"] is not None]
    if valid:
        import numpy as np
        n = len(valid)
        arr = np.asarray(valid, dtype=float)
        avg = float(arr.mean())
        srt = sorted(valid)
        std = float(arr.std(ddof=0))
        b_avg = float(np.mean(bench_returns)) if bench_returns else None
        summary = {
            "avg_return_pct":    round(avg, 2),
            "median_return_pct": round(percentile(srt, 50), 2),
            "std_dev_pct":       round(std, 2),
            "p5_pct":            round(percentile(srt,  5), 2),
            "p25_pct":           round(percentile(srt, 25), 2),
            "p75_pct":           round(percentile(srt, 75), 2),
            "p95_pct":           round(percentile(srt, 95), 2),
            "win_rate_pct":      round(sum(1 for x in valid if x > 0) / n * 100, 1),
            "n_with_data":       n,
            "benchmark_avg_pct": round(b_avg, 2) if b_avg is not None else None,
            "excess_return_pct": round(avg - b_avg, 2) if b_avg is not None else None,
            "n_benchmark":       len(bench_returns),
        }
    else:
        summary = None

    return {
        "start_date":       start_date_str,
        "end_date":         today_str,
        "holding_months":   months_ago,
        "top_n":            top_n,
        "preset":           preset_name,
        "source":           source,
        "total_candidates": len(scored),
        "summary":          summary,
        "results":          results,
    }
