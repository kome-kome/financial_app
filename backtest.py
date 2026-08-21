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
from plugins.recommend import (SELECT_COLS, compute_momentum_z,
                               fit_view_metric_stats, resolve_weights,
                               standardize_metric)
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

# backtest が読む `financial_metrics` の列（Issue #489）。VIEW は 97 列ある。
# `recommend.SELECT_COLS`（_DISPLAY_COLS ＋ METRICS − RUNTIME_METRICS）を土台に、
# `score_record` が source 別に読む列と `period_end` を足す。
#
# **weights のキーは `coerce_params` が METRICS へ制限する**ので SELECT_COLS で覆える。
# RUNTIME_METRICS のうち `z_momentum` は `momentum_z` から取り、`mu` は backtest では
# reject される（as-of 再現ができない・#423 子4）＝どちらも VIEW 列として引く必要が無い。
#
# `score_record` は `getattr(r, metric, None)` で読むため、**列が欠けると例外ではなく
# 「その指標だけ黙って 0 扱い」になる**（#482 で踏んだ罠と同型）。漏れは
# `tests/test_backtest.py` のメタテストが CI で落とす。
_BACKTEST_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    SELECT_COLS + (
        "period_end",                                       # best 判定と結果 dict
        "gap_ratio", "div_yield",                           # source="valuation"
        "bs_current_assets", "bs_investment_securities",    # source="net_cash"
        "bs_total_liabilities", "market_cap",
        "is_active", "delisted_date",                        # 生存バイアス測定（#315）
    )))


def score_record(r, source: str, weights: dict, momentum_z: dict | None = None,
                 view_stats: dict | None = None) -> float | None:
    """1レコードのスコア（高いほど買い候補）。算出不能なら None（候補から除外）。

    各 source は financial_metrics VIEW の as-of スナップショット（FinancialMetric）から
    一次分析のランキングキーを再現する。recommend のみ preset 加重を使う。

    momentum_z: {edinet_code: z} 形式の事前計算済み z_momentum（compute_momentum_z）。
    weights に z_momentum が含まれる場合のみ呼び出し側が渡す（他 source では未使用）。

    view_stats: `fit_view_metric_stats` が断面から作った {metric: (mean, sd)}（Issue #509）。
    **1レコードでは断面統計を持てない**ため、`momentum_z` と同じく呼び出し側（run）が作って
    渡す。渡さなければ VIEW 値をそのまま線形結合する是正前の挙動になる——`recommend` プラグイン
    側だけを是正して here を素通りさせると、**as-of 再現が是正前のスコアを測る**ことになり
    検証が噛み合わない（#509 検証3）。
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
            # 算出側（compute_momentum_z）で期内標準化済み＝二重に標準化しない。
            val = (momentum_z or {}).get(r.edinet_code)
        else:
            val = getattr(r, metric, None)
            if val is not None and view_stats is not None:
                val = standardize_metric(val, metric, view_stats)
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
    cost_bps: float = 0.0,
) -> dict:
    """バックテストを1期間分実行してdictを返す（例外はそのまま伝播）。

    source で検証対象の一次分析を切替（recommend / valuation / net_cash）。
    仕組み（as-of スコア→上位N社→実現リターン→ベンチマーク超過）は source 非依存。

    cost_bps: 片道売買コスト（bp、1bp=0.01%）。往復（買い+売り）で2倍控除した
    ネットリターンを `*_net` キーへ併記する（デフォルト0＝控除なし・既存の
    無印キーは cost_bps に関わらず常にコスト控除前のまま＝後方互換固定）。
    """
    if source not in SCORING_SOURCES:
        raise ValueError(
            f"未知の scoring source: {source!r}（{', '.join(SCORING_SOURCES)} のいずれか）"
        )
    weights = resolve_weights(db, preset_name)
    # μ̂（mu）は as-of 再現ができないため明示的に非対応（Issue #423 子4）。producer スコア
    # （macro_enet_scores 等）は最新 snapshot_date の1断面しか持たず、months_ago 時点の
    # μ̂ を復元できない。黙って getattr→None に落とすと「μ̂ 込みで検証した」と誤読される
    # ため reject する。μ̂ 自体の時系列評価は各 producer の OOF バックテスト側が担う
    # （plugins/macro_snapshots.py::oof_backtest・ADR-0022）。
    if weights.get("mu"):
        raise ValueError(
            "バックテストは mu（μ̂）を含む重みに対応していません。"
            "μ̂ は最新スナップショットの1断面のみで過去時点を再現できないため、"
            "mu の評価は各モデル（M-1〜M-6）の OOF バックテストを使ってください。")
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
    cols = [getattr(FinancialMetric, f) for f in _BACKTEST_FIELDS]
    query = (
        db.query(*cols)
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

    # VIEW 由来指標の断面標準化パラメータ（Issue #509）。プリセット加重を使う source だけが
    # 必要とする（valuation / net_cash は専用のスコア式で early return する）。
    # 母集団は **as-of 断面のレコード全体**で、
    # 1社1行へ畳む前に作る——`best` の dedup は「スコアが算出できた行のうち period_end 最大」で
    # あってスコアに依存するため、先に畳むと「最新期は gap_ratio が無いので前期を使う」経路
    # （source="valuation"）の挙動が変わってしまう。`subq` が edinet_code ごとの max(year) で
    # join 済みなので重複行は決算期変更などの稀なケースに限られ、統計への影響は無視できる。
    view_stats = (fit_view_metric_stats(records, weights)
                  if source in ("recommend", "sell") else {})

    best: dict = {}
    for r in records:
        score = score_record(r, source, weights, momentum_z, view_stats)
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

    # bp→%変換かつ往復（買い+売り）分の2倍。cost_bps=0なら常に0（後方互換）。
    round_trip_cost_pct = cost_bps / 100.0 * 2

    results = []
    for rank, (score, r) in enumerate(top, 1):
        c = r.edinet_code
        sp = sp_all.get(c)
        ep = ep_all.get(c)
        if (sp and ep and sp["price"] and ep["price"]
                and sp["date"] < ep["date"]):
            ret_pct = round((ep["price"] - sp["price"]) / sp["price"] * 100, 2)
            ret_pct_net = round(ret_pct - round_trip_cost_pct, 2)
        else:
            ret_pct = None
            ret_pct_net = None
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
            "return_pct_net": ret_pct_net,
            "has_price_data": ret_pct is not None,
            # is_active は「現在」の上場状態（as-of の start_date 時点ではない）。start_date 時点で
            # 存在した候補を今の上場状態でフィルタすると生存者バイアスを逆に持ち込むため、
            # スコアリング対象からは除外せず情報表示のみに留める（Issue #315・検証用）。
            "is_active":      r.is_active is not False,
            "delisted_date":  r.delisted_date.isoformat() if r.delisted_date else None,
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

        valid_net = [r["return_pct_net"] for r in results if r["return_pct_net"] is not None]
        avg_net = float(np.mean(valid_net))
        b_avg_net = (b_avg - round_trip_cost_pct) if b_avg is not None else None

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
            "cost_bps":              cost_bps,
            "avg_return_net_pct":    round(avg_net, 2),
            "win_rate_net_pct":      round(sum(1 for x in valid_net if x > 0) / n * 100, 1),
            "benchmark_avg_net_pct": round(b_avg_net, 2) if b_avg_net is not None else None,
            "excess_return_net_pct": round(avg_net - b_avg_net, 2) if b_avg_net is not None else None,
        }
    else:
        summary = None

    # 生存者バイアスの規模測定（Issue #315「検証」）: top_n のうち現在は上場廃止の件数と、
    # そのうち価格データ欠損で n_with_data から自然脱落した件数。0 でも常に返す（summary が
    # None＝価格データ皆無のケースでも測定値自体は意味を持つため）。
    n_delisted_in_top_n = sum(1 for r in results if r["is_active"] is False)
    n_delisted_no_price = sum(1 for r in results if r["is_active"] is False and not r["has_price_data"])

    return {
        "start_date":       start_date_str,
        "end_date":         today_str,
        "holding_months":   months_ago,
        "top_n":            top_n,
        "preset":           preset_name,
        "source":           source,
        "cost_bps":         cost_bps,
        "total_candidates": len(scored),
        "n_delisted_in_top_n": n_delisted_in_top_n,
        "n_delisted_no_price": n_delisted_no_price,
        "summary":          summary,
        "results":          results,
    }
