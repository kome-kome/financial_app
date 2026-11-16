"""バックテスト分析（プリセット・スコアリング上位 N 社の実績リターン計算）。

api.py の routing から引き上げた分析ロジック。interface は `(db, params) -> dict` で、
FastAPI app に依存しないため HTTP 往復なしで直接テストできる（tests/test_backtest.py）。
価格取得は database のヘルパ（prices_on_or_after / latest_prices）に集約。
"""
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

# 分割補正の向き（×F か ÷F か）と、期末後分割の年の基準の遅れを当てる列。VIEW と同じ表を読む（書き写さない・#1248）
from corporate_actions import BASIS_LAG_COLUMN, COLUMN_DIRECTION
from database import FinancialMetric, MacroData, StockPriceWeekly, prices_on_or_after, latest_prices
# 決算日から公表までの遅れ。M 系のモデルと同じ値を読む（書き写さない・#1226）
from plugins.macro_snapshots import FINANCIAL_LAG_DAYS
# 比べる相手の指数。売買の記録の成績（docs/VISION.md「運用の成功指標」）と同じ系列を借りる（書き写さない・#1234）
from portfolio_ledger import BENCHMARK_SERIES
from plugins.recommend import (DEFAULT_MIN_COVERAGE, SELECT_COLS, compute_momentum_z,
                               fit_view_metric_stats, resolve_weights, weighted_score)
from plugins.net_cash_analysis import compute_net_cash, compute_nc_ratio

# 複数保有期間バックテスト（/api/backtest/multi）の保有月数。
MULTI_PERIODS = [3, 6, 12, 18, 24]

# バックテストで検証できるスコアリング手法（ランキングを出す一次分析を as-of で再現する）。
# どれも「スコアが高いほど買い候補」で、上位 N 社のその後リターンがベンチマークを上回れば有効。
#   recommend : 本番の買い推奨と同じ合成スコア（plugins.recommend.weighted_score・#745）
#   valuation : バリュエーション分析の期待総リターン（gap_ratio + 配当利回り）
#   net_cash  : 清原式ネットキャッシュ比率
# 以前あった `sell`（recommend の合成スコアの符号反転）は外した（ADR-0071）。売り候補ランキングの
# 手法（売り専用の予測 S2 の下位・ADR-0076）とは別物で、売りとして画面に出す手法を 1 つに絞るため。売りの評価は
# OOF の売り側spread（`scripts.sell_mu_source_bakeoff`）で行う。
SCORING_SOURCES = ("recommend", "valuation", "net_cash")

# 配当利回りの異常値ガード（％）。gap_analysis（バリュエーション分析）と整合。
_DIV_YIELD_CAP = 30.0

# 開始日の時点で買えた社だけを候補にする（#1242）。as-of の財務は社ごとに「境目以前で最新の年度」を
# 取るので、古さの上限が無いと、何年も前に決算が止まった社（上場廃止・非公開化）の最後の決算が
# 開始日の候補に混ざり、上位を埋めたうえで「株価なし」になる（既定の条件で 20 社中 20 社）。
# **今の上場状態（`is_active`）では絞らない**（開始日より後に廃止した社を落とすと生存者バイアスを
# 逆に持ち込む・#315）。開始日の時点で分かっていた事実＝開始日の N 日前〜開始日の週次の終値で絞る
# （M 系の断面も、その月に株価がある社だけで作る）。None なら絞らない。
# 決算の古さ（境目の 18 か月前より古い決算）で絞る案も数えたが、株価で絞った後の上位に古い決算の社は
# 残らなかったので足していない（#1242 の計測・2026-11-16）。
# **同じ窓の最後の終値で、株価を使う列を開始日の値へ作り直す**（下の ASOF_REPRICED_COLUMNS・#1248）。
# None は絞り込みと作り直しの両方を止める（開始日の株価を持たない検体で採点の規則を測るテスト用）。
ASOF_PRICE_WINDOW_DAYS: Optional[int] = 14

# 開始日の株価で作り直す列（#1248）。as-of の行のこれらの列は、毎晩の収集（`collector_prices.
# _compute_market_values`）が**今の株価**で上書きしている。開始日が半年前なら as-of の行の多くは今の最新の
# 通期行なので、そのまま順位を付けると「開始日から今までに値下がりした社ほど割安に見えて上位に来る」
# （2026-11-16 の計測: 6 か月前の開始日で、保存された株価が開始日の終値から 3% を超えてずれる行が 82%。
# 直す前のネットキャッシュの上位 20 社は −42.04%・直した後は +4.11%）。作り直しは毎晩の収集と同じ関数へ
# 開始日の終値を渡し、VIEW と同じ補正（F と基準の遅れ・向きは corporate_actions の表）を当てる。
# 保存された時価総額を比で直す形は採らない——株数を後から直した古い行では、保存された時価総額だけが古い
# 株数のまま残っている（同じ計測で 24 か月前の 17 行）。gap_ratio は `sector_gap_asof.gaps_on` が作り直す。
ASOF_REPRICED_COLUMNS = ("market_cap", "div_yield")

# backtest が読む `financial_metrics` の列（Issue #489）。VIEW は 97 列ある。
# `recommend.SELECT_COLS`（_DISPLAY_COLS ＋ METRICS − RUNTIME_METRICS）を土台に、
# `score_record` が source 別に読む列と `period_end` を足す。
#
# **weights のキーは `coerce_params` が METRICS へ制限する**ので SELECT_COLS で覆える。
# RUNTIME_METRICS のうち `z_momentum` は `momentum_z` から取り、`mu` は backtest では
# reject される（as-of 再現ができない・#423 子4）＝どちらも VIEW 列として引く必要が無い。
#
# `score_record`（→ `weighted_score`）は `getattr(r, metric, None)` で読むため、**列が欠けると
# 例外ではなく「その指標だけ黙って欠け扱い」になる**（#482 で踏んだ罠と同型）。漏れは
# `tests/test_backtest.py` のメタテストが CI で落とす。
_BACKTEST_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    SELECT_COLS + (
        "period_end",                                       # best 判定と結果 dict
        "gap_ratio", "div_yield",                           # source="valuation"
        "bs_current_assets", "bs_investment_securities",    # source="net_cash"
        "bs_total_liabilities", "market_cap",
        "is_active", "delisted_date",                        # 生存バイアス測定（#315）
        # 開始日の株価での作り直し（asof_row・#1248）: 毎晩の収集が読む入力と、VIEW の分割補正の列
        "pl_eps", "bs_bps", "issued_shares", "bs_total_equity", "dps",
        "split_factor", "split_shares_lag", "split_dps_lag",
    )))


def score_record(r, source: str, weights: dict, momentum_z: dict | None = None,
                 view_stats: dict | None = None) -> float | None:
    """1レコードのスコア（高いほど買い候補）。算出不能なら None（候補から除外）。

    各 source は financial_metrics VIEW の as-of スナップショット（FinancialMetric）から
    一次分析のランキングキーを再現する。recommend は preset の重みを、本番の買い推奨と
    同じ `plugins.recommend.weighted_score` で合成する（値がある指標の |w| 和で割る・被覆率が
    画面の既定 `DEFAULT_MIN_COVERAGE` 未満なら None・#745）。以前は Σw·ẑ のまま（欠けた指標が
    実質 0）で被覆率でも除外せず、欠けの多い銘柄を系統的に中央へ寄せる＝本番とは別の
    ランキングを測っていた。

    momentum_z: {edinet_code: z} 形式の事前計算済み z_momentum（compute_momentum_z）。
    weights に z_momentum が含まれる場合のみ呼び出し側が渡す（他 source では未使用）。

    view_stats: `fit_view_metric_stats` が断面から作った {metric: (mean, sd)}（Issue #509）。
    **1レコードでは断面統計を持てない**ため、`momentum_z` と同じく呼び出し側（run）が作って
    渡す。渡さなければ VIEW 値をそのまま線形結合する是正前の挙動になる——`recommend` プラグイン
    側だけを是正して here を素通りさせると、**as-of 再現が是正前のスコアを測る**ことになり
    検証が噛み合わない（#509 検証3）。
    """
    if source == "valuation":
        # 期待総リターン[%] = gap_ratio[%] + 配当利回り[%]（gap_ratio 必須）。run は両方を開始日の株価で
        # 作り直した行を渡す（gap は開始日 1 日分の業種別回帰・#1248）
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
    # recommend（既定）: 本番の買い推奨と同じ合成スコア。
    # z_momentum は算出側（compute_momentum_z）で期内標準化済み＝二重に標準化しない。
    return weighted_score(r, weights, view_stats or {}, min_coverage=DEFAULT_MIN_COVERAGE,
                          runtime={"z_momentum": momentum_z or {}}).score


def percentile(sorted_arr: list, p: float) -> float:
    """pパーセンタイル値（0〜100）。numpy.percentile（線形補間）を使用。"""
    n = len(sorted_arr)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_arr[0])
    import numpy as np
    return float(np.percentile(sorted_arr, p, method="linear"))


def index_return(db: Session, start_date_str: str) -> dict:
    """同じ期間の TOPIX のリターン（開始日以降の最初の終値 → 直近の終値・#1234）。

    系列は `macro_data` の `BENCHMARK_SERIES`（連動 ETF の終値・配当なし。保有側の株価も配当を含まない）。読むのは
    両端の 2 行だけ。値が取れない（系列が無い・開始日以降に 1 行しか無い）ときは `return_pct` を None にする
    （0 として黙って超過を出さない）。
    """
    base = (db.query(MacroData.trade_date, MacroData.close)
            .filter(MacroData.series_code == BENCHMARK_SERIES, MacroData.trade_date >= start_date_str,
                    MacroData.close > 0))
    first = base.order_by(MacroData.trade_date.asc()).first()
    last = base.order_by(MacroData.trade_date.desc()).first()
    out = {"series": BENCHMARK_SERIES, "start_date": first[0] if first else None,
           "end_date": last[0] if last else None, "return_pct": None}
    if first and last and first[0] < last[0]:
        out["return_pct"] = round((last[1] - first[1]) / first[1] * 100, 2)
    return out


def asof_closes(db: Session, start_date: date) -> Optional[dict]:
    """開始日の時点の株価 `{edinet_code: 終値}`（#1242・#1248）。窓（ASOF_PRICE_WINDOW_DAYS）が None なら None。

    開始日の N 日前〜開始日の週次の足のうち、社ごとに最後の終値（分割を遡って調整済みの値）。開始日より後の
    足は見ない（先を読まない）。終値が 0 以下の足は数えない（割り算に使えない）。
    """
    if ASOF_PRICE_WINDOW_DAYS is None:
        return None
    lo = (start_date - timedelta(days=ASOF_PRICE_WINDOW_DAYS)).isoformat()
    last: dict = {}
    for ec, td, close in (db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                                   StockPriceWeekly.close_last)
                          .filter(StockPriceWeekly.trade_date >= lo,
                                  StockPriceWeekly.trade_date <= start_date.isoformat(),
                                  StockPriceWeekly.close_last > 0)):
        if ec not in last or td > last[ec][0]:
            last[ec] = (td, float(close))
    return {ec: close for ec, (_td, close) in last.items()}


def tradable_at_start(records: list, closes: Optional[dict]) -> tuple[list, int]:
    """as-of の候補を、開始日の時点で買えた社だけへ絞る（#1242）。`(残した行, 外した社の数)` を返す。

    `closes` は `asof_closes` の戻り（None なら絞らない）。今の上場状態は見ない（#315）。外した数は応答の
    `n_untradable_at_start` に出す（黙って消さない）。数えるのは社の数で、行の数ではない。
    """
    if closes is None or not records:
        return list(records), 0
    unpriced = {r.edinet_code for r in records} - set(closes)
    return [r for r in records if r.edinet_code not in unpriced], len(unpriced)


def _view_basis(r, col: str, raw: Optional[float]) -> Optional[float]:
    """生の値へ VIEW と同じ分割補正（F と、期末後分割の年の基準の遅れ）を当てる（#1248）。

    向き（×F か ÷F か）は `corporate_actions.COLUMN_DIRECTION`、どの遅れを当てるかは `BASIS_LAG_COLUMN` が
    唯一の源。係数は VIEW の行の `split_factor` / `split_<遅れ>`（無ければ 1.0＝VIEW の COALESCE と同じ）。
    """
    if raw is None:
        return None
    lag = BASIS_LAG_COLUMN[col]
    k = (r.split_factor or 1.0) * ((getattr(r, f"split_{lag}") or 1.0) if lag else 1.0)
    return raw * k if COLUMN_DIRECTION[col] > 0 else raw / k


def asof_row(r, close: float, gap_ratio: Optional[float]):
    """as-of の行の株価を使う列を、開始日の終値で作り直した行（#1248）。他の列は VIEW の値のまま。

    作り直しは毎晩の収集と同じ `_compute_market_values` へ開始日の終値を渡す（式を書き写さない）。`gap_ratio`
    は呼び出し側が `sector_gap_asof.gaps_on` で作った値（使わない手法では None）。VIEW の gap は今の株価で
    回した値なので、どの手法でも残さない。
    """
    from collector_prices import _compute_market_values

    raw = _compute_market_values(close, r.pl_eps, r.bs_bps, r.issued_shares, r.bs_total_equity, r.dps)
    vals = r._asdict()
    vals.update({col: _view_basis(r, col, raw.get(col)) for col in ASOF_REPRICED_COLUMNS})
    vals["gap_ratio"] = gap_ratio
    return SimpleNamespace(**vals)


def _uses_gap_ratio(source: str, weights: dict) -> bool:
    """開始日の gap_ratio（業種別回帰 1 回）が要るか。要らない手法では回帰を回さない。"""
    return source == "valuation" or (source == "recommend" and bool(weights.get("gap_ratio")))


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
    # 開始日の時点で公表済みとみなせる決算だけを使う（#1226）。決算日から公表までには遅れがあり
    # （3 月期なら決算短信が 5 月・有価証券報告書が 6 月）、`period_end <= start_date` だけで絞ると
    # まだ出ていない数字で順位を付けてしまう＝成績が実際より良く見える。M 系のスナップショット
    # （`macro_snapshots._latest_fin`）と同じ「決算日 + FINANCIAL_LAG_DAYS ≦ 基準日」にそろえる。
    # 会社ごとの実際の公表日は VIEW に無いので、一律の日数で近似する。
    fin_cutoff = start_date - timedelta(days=FINANCIAL_LAG_DAYS)
    fin_cutoff_str = fin_cutoff.strftime("%Y-%m-%d")

    # スコア指標（z_roe / z_op_margin / gap_ratio 等）は financial_metrics VIEW が算出する
    # 派生値のため、FinancialRecord ではなく読み取りモデル FinancialMetric を引く。
    subq = (
        db.query(FinancialMetric.edinet_code,
                 func.max(FinancialMetric.year).label("max_year"))
        .filter(FinancialMetric.period_end <= fin_cutoff)
        .group_by(FinancialMetric.edinet_code)
        .subquery()
    )
    cols = [getattr(FinancialMetric, f) for f in _BACKTEST_FIELDS]
    query = (
        db.query(*cols)
        .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                    (FinancialMetric.year == subq.c.max_year))
        .filter(FinancialMetric.period_end <= fin_cutoff)
    )
    if industry:
        query = query.filter(FinancialMetric.industry == industry)
    records = query.all()
    # 開始日の時点で買えた社だけを残す（#1242）。断面標準化（view_stats）と z_momentum の母集団もこの後の行
    closes = asof_closes(db, start_date)
    records, untradable = tradable_at_start(records, closes)
    # 株価を使う列を開始日の値へ作り直す（#1248）。gap_ratio は使う手法だけ開始日 1 日分の業種別回帰を回す
    # （母集団は開始日に株価がある全社＝上の業種・下の時価総額で絞る前の社）。
    asof_gap: Optional[dict] = None
    if closes is not None:
        gaps: dict = {}
        if _uses_gap_ratio(source, weights):
            import sector_gap_asof
            gaps, asof_gap = sector_gap_asof.gaps_on(db, closes, start_date_str)
        records = [asof_row(r, closes[r.edinet_code], gaps.get((r.edinet_code, r.year)))
                   for r in records]
    # 時価総額の下限は開始日の時価総額で掛ける（今の時価総額で絞ると、開始日の後に値上がりした社だけが残る）。
    # 開始日に株価が無かった社は上で外れているので、`n_untradable_at_start` は下限で絞る前の数。
    if min_market_cap is not None:
        records = [r for r in records
                   if r.market_cap is not None and r.market_cap >= float(min_market_cap)]

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
                  if source == "recommend" else {})

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
            "financial_cutoff": fin_cutoff_str, "financial_lag_days": FINANCIAL_LAG_DAYS,
            "n_untradable_at_start": untradable,
            "asof_valuation": closes is not None, "asof_gap": asof_gap,
            "summary": None, "results": [], "total_candidates": 0,
            "message": (f"{start_date_str} 時点で公表済みとみなせる財務データ"
                        f"（決算日が {fin_cutoff_str} 以前）が見つかりませんでした"
                        + (f"（開始日の時点で株価が無かった {untradable} 社は候補から外しています）"
                           if untradable else "")
                        + (f"（開始日の時点の gap_ratio を作れませんでした: {asof_gap['skipped']}）"
                           if asof_gap and asof_gap.get("skipped") else "")),
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
        # TOPIX に対する超過（#1234）。上の「全社平均」は同じスコアで選んだ上位 500 社の平均で、市場に対する値ではない。
        # TOPIX は連動 ETF 1 本で買えて売買のコストはほぼ無いので、コスト後の超過は保有側だけからコストを引く
        idx = index_return(db, start_date_str)
        i_ret = idx["return_pct"]
        summary.update({
            "index_series":            idx["series"],
            "index_start_date":        idx["start_date"],
            "index_end_date":          idx["end_date"],
            "index_return_pct":        i_ret,
            "excess_vs_index_pct":     round(avg - i_ret, 2) if i_ret is not None else None,
            "excess_vs_index_net_pct": round(avg_net - i_ret, 2) if i_ret is not None else None,
        })
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
        # 順位付けに使った財務の境目（決算日がこの日以前の行だけ・#1226）と、公表までの遅れとして見た日数
        "financial_cutoff":   fin_cutoff_str,
        "financial_lag_days": FINANCIAL_LAG_DAYS,
        "total_candidates": len(scored),
        # 開始日の時点で株価が無かった（買えなかった）ので候補から外した社の数（#1242）
        "n_untradable_at_start": untradable,
        # 時価総額・配当利回り・gap_ratio を開始日の株価で作り直したか（#1248）と、開始日の gap の回帰の件数
        # （`{"n_universe", "n_gap"[, "skipped"]}`。gap を使わない手法では None）
        "asof_valuation": closes is not None,
        "asof_gap": asof_gap,
        "n_delisted_in_top_n": n_delisted_in_top_n,
        "n_delisted_no_price": n_delisted_no_price,
        "summary":          summary,
        "results":          results,
    }
