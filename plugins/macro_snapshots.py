"""
plugins/macro_snapshots.py — M-1/M-2 共有スナップショット構築モジュール（ADR-0003 §3）

M-1（macro_risk_return）と M-2（macro_gbdt）の共通面を集約し、M-2→M-1 の
直接結合をゼロにする。utils.py の _MACRO_MAP 遅延 import 循環ハックも解消。

正本として保有するもの:
  - FINANCIAL_LAG_DAYS / HORIZON_WEEKS
  - FIN_BASE_OPTIONS / DEFAULT_FIN_FEATURES
  - _MACRO_MAP / MACRO_FEATURE_NAMES / MACRO_FEATURE_OPTIONS / DEFAULT_MACRO_FEATURES
  - スナップショット構築（build_snapshots / preload_macro / load_data）
  - リーク感応 helpers（_find_applicable_fin / _macro_from_cache / _realized_vol）
  - producer スコア（producer_scores / get_producer_scores）
"""
import math
import statistics
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .utils import macro_risk_exposure

# ── 定数 ──────────────────────────────────────────────────────────────────

FINANCIAL_LAG_DAYS = 45
HORIZON_WEEKS = 52

# 財務ベース特徴量の選択肢（全て financial_metrics VIEW の実列）。
FIN_BASE_OPTIONS = [
    {"value": "per",            "label": "PER"},
    {"value": "pbr",            "label": "PBR"},
    {"value": "div_yield",      "label": "配当利回り（%）"},
    {"value": "roe",            "label": "ROE（%）"},
    {"value": "roa",            "label": "ROA（%）"},
    {"value": "op_margin",      "label": "営業利益率（%）"},
    {"value": "net_margin",     "label": "純利益率（%）"},
    {"value": "asset_turnover", "label": "総資産回転率（回）"},
    {"value": "equity_ratio",   "label": "自己資本比率（%）"},
    {"value": "de_ratio",       "label": "D/Eレシオ"},
    {"value": "nc_ratio",       "label": "ネットキャッシュ比率"},
    {"value": "cf_ratio",       "label": "営業CF/売上（%）"},
    {"value": "eps_growth",     "label": "EPS成長率（%）"},
    {"value": "op_growth",      "label": "営業利益成長率（%）"},
    {"value": "rev_growth",     "label": "売上成長率（%）"},
    {"value": "rd_intensity",   "label": "R&D集約度"},
    {"value": "da_intensity",   "label": "D&A集約度"},
    {"value": "z_op_margin",    "label": "営業利益率Zスコア"},
    {"value": "z_roe",          "label": "ROE Zスコア"},
    {"value": "z_cf_ratio",     "label": "CF比率Zスコア"},
]
DEFAULT_FIN_FEATURES = ["per", "pbr", "roe", "equity_ratio", "roa", "eps_growth"]

# feature_name → (series_code, transform: "yoy" | "zscore") の正本。
# utils.py の _macro_feature_map() はここから import する（循環依存ハック解消）。
_MACRO_MAP = {
    "macro_usdjpy_yoy":    ("USDJPY",    "yoy"),
    "macro_eurjpy_yoy":    ("EURJPY",    "yoy"),
    "macro_dxy_yoy":       ("DXY",       "yoy"),
    "macro_sp500_yoy":     ("SP500",     "yoy"),
    "macro_us5y_zscore":   ("US5Y",      "zscore"),
    "macro_us10y_zscore":  ("US10Y",     "zscore"),
    "macro_us30y_zscore":  ("US30Y",     "zscore"),
    "macro_nikkei225_yoy": ("NIKKEI225", "yoy"),
    "macro_vix_zscore":    ("VIX",       "zscore"),
    "macro_wti_yoy":       ("WTI",       "yoy"),
    "macro_gold_yoy":      ("GOLD",      "yoy"),
    # ── FRED チャネル（#221・2026-06-24 本番蓄積確認済み） ──────────────────────
    "macro_hy_oas_zscore":       ("HY_OAS",      "zscore"),
    "macro_ig_oas_zscore":       ("IG_OAS",       "zscore"),
    "macro_breakeven10y_zscore": ("BREAKEVEN10Y", "zscore"),
    "macro_jp10y_fred_zscore":   ("JP10Y_FRED",   "zscore"),
    "macro_t10y2y_zscore":       ("T10Y2Y",       "zscore"),
}
MACRO_FEATURE_NAMES = list(_MACRO_MAP.keys())

MACRO_FEATURE_OPTIONS = [
    {"value": "macro_usdjpy_yoy",    "label": "USD/JPY 前年比（YoY）"},
    {"value": "macro_eurjpy_yoy",    "label": "EUR/JPY 前年比（YoY）"},
    {"value": "macro_dxy_yoy",       "label": "ドル指数（DXY）前年比（YoY）"},
    {"value": "macro_sp500_yoy",     "label": "S&P500 前年比（YoY）"},
    {"value": "macro_us5y_zscore",   "label": "米5年金利 Zスコア"},
    {"value": "macro_us10y_zscore",  "label": "米10年金利 Zスコア"},
    {"value": "macro_us30y_zscore",  "label": "米30年金利 Zスコア"},
    {"value": "macro_nikkei225_yoy", "label": "日経225 前年比（YoY）"},
    {"value": "macro_vix_zscore",    "label": "VIX恐怖指数 Zスコア"},
    {"value": "macro_wti_yoy",       "label": "WTI原油 前年比（YoY）"},
    {"value": "macro_gold_yoy",      "label": "金（ゴールド）前年比（YoY）"},
    {"value": "macro_hy_oas_zscore",       "label": "米HYスプレッド（OAS）Zスコア"},
    {"value": "macro_ig_oas_zscore",       "label": "米IGスプレッド（OAS）Zスコア"},
    {"value": "macro_breakeven10y_zscore", "label": "米10年BEI（インフレ期待）Zスコア"},
    {"value": "macro_jp10y_fred_zscore",   "label": "日10年金利（FRED）Zスコア"},
    {"value": "macro_t10y2y_zscore",       "label": "米10y−2yスプレッド Zスコア"},
]
DEFAULT_MACRO_FEATURES = ["macro_usdjpy_yoy", "macro_sp500_yoy", "macro_us10y_zscore"]


# ── 日付 / 財務 helpers ────────────────────────────────────────────────────

def _add_days(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _find_applicable_fin(fin_recs: list, snap_date: str):
    """snap_date より FINANCIAL_LAG_DAYS 前以前に period_end がある最新の財務レコードを返す。"""
    result = None
    for fr in fin_recs:
        if not fr.period_end:
            continue
        pe = fr.period_end
        pe_str = pe.isoformat() if hasattr(pe, "isoformat") else str(pe)[:10]
        if _add_days(pe_str, FINANCIAL_LAG_DAYS) <= snap_date:
            result = fr
    return result


# ── マクロ helpers ─────────────────────────────────────────────────────────

def _macro_from_cache(
    by_series: dict[str, dict[str, float]],
    ref_date: str,
    feature_names: list[str],
    window_days: int = 30,
    zscore_years: int = 5,
) -> dict[str, float | None]:
    """プリロード済みマクロデータから特徴量を計算（DB クエリなし）。"""
    from datetime import date as _date, timedelta as _td
    ref = _date.fromisoformat(ref_date)
    win_start = (ref - _td(days=window_days)).isoformat()
    result: dict[str, float | None] = {}

    for fname in feature_names:
        if fname not in _MACRO_MAP:
            result[fname] = None
            continue
        scode, ttype = _MACRO_MAP[fname]
        date_close = by_series.get(scode, {})
        if not date_close:
            result[fname] = None
            continue

        current_vals = [v for d, v in date_close.items() if win_start <= d <= ref_date]
        if not current_vals:
            past = sorted((d, v) for d, v in date_close.items() if d <= ref_date)
            if not past:
                result[fname] = None
                continue
            current_vals = [past[-1][1]]
        current_avg = statistics.mean(current_vals)

        if ttype == "yoy":
            from datetime import date as _d2, timedelta as _td2
            ref_1y = ref - _td2(days=365)
            p_s = (ref_1y - _td2(days=window_days)).isoformat()
            p_e = (ref_1y + _td2(days=window_days)).isoformat()
            prev_vals = [v for d, v in date_close.items() if p_s <= d <= p_e]
            if not prev_vals:
                result[fname] = None
                continue
            prev_avg = statistics.mean(prev_vals)
            result[fname] = (current_avg - prev_avg) / prev_avg if prev_avg else None

        elif ttype == "zscore":
            from datetime import date as _d3, timedelta as _td3
            hist_start = (ref - _td3(days=zscore_years * 366)).isoformat()
            all_vals = [v for d, v in date_close.items() if hist_start <= d <= ref_date]
            if len(all_vals) < 20:
                result[fname] = None
                continue
            mu = statistics.mean(all_vals)
            sigma = statistics.stdev(all_vals) if len(all_vals) > 1 else 0.0
            result[fname] = (current_avg - mu) / sigma if sigma else None

    return result


# ── 実現ボラ ───────────────────────────────────────────────────────────────

def _realized_vol(price_rows: list, ref_date: str, weeks: int = 52) -> float | None:
    """ref_date 直前 weeks 週の実現ボラティリティ（年率）を返す。リークなし。"""
    eligible = [(r.trade_date, r.close_last)
                for r in price_rows
                if r.trade_date <= ref_date and r.close_last and r.close_last > 0]
    if len(eligible) < 4:
        return None
    recent = eligible[max(0, len(eligible) - weeks - 1):]
    if len(recent) < 4:
        return None
    log_rets = [
        math.log(recent[i][1] / recent[i - 1][1])
        for i in range(1, len(recent))
        if recent[i - 1][1] > 0
    ]
    if len(log_rets) < 3:
        return None
    return statistics.stdev(log_rets) * math.sqrt(52)


# ── データロード ───────────────────────────────────────────────────────────

def load_data(db) -> tuple:
    """Company / FinancialMetric / StockPriceWeekly を一括ロード。"""
    from database import Company, FinancialMetric, StockPriceWeekly
    _PX = namedtuple("_PX", "trade_date close_last")
    raw = (
        db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                 StockPriceWeekly.close_last)
        .order_by(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date)
        .all()
    )
    prices_by_co: dict[str, list] = defaultdict(list)
    for ec, td, cl in raw:
        prices_by_co[ec].append(_PX(td, cl))

    fin_by_co: dict[str, list] = defaultdict(list)
    for r in (db.query(FinancialMetric)
              .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end)
              .all()):
        fin_by_co[r.edinet_code].append(r)

    companies = {c.edinet_code: c for c in db.query(Company).all()}
    return prices_by_co, fin_by_co, companies


def preload_macro(db, prices_by_co: dict, macro_names: list[str] | None = None) -> dict:
    """MacroData を一括プリロードしキャッシュ dict を返す。"""
    from database import MacroData
    from datetime import date as _date, timedelta as _td
    all_dates = [row.trade_date for rows in prices_by_co.values() for row in rows]
    if not all_dates:
        return {}
    min_d = min(all_dates)
    since = (_date.fromisoformat(min_d) - _td(days=5 * 366)).isoformat()
    max_d = max(all_dates)
    series_codes = sorted({_MACRO_MAP[n][0] for n in (macro_names or MACRO_FEATURE_NAMES) if n in _MACRO_MAP})
    q = (
        db.query(MacroData)
        .filter(
            MacroData.trade_date >= since,
            MacroData.trade_date <= max_d,
            MacroData.close.isnot(None),
        )
    )
    if series_codes:
        q = q.filter(MacroData.series_code.in_(series_codes))
    rows = q.order_by(MacroData.series_code, MacroData.trade_date).all()
    by_series: dict[str, dict[str, float]] = {}
    for r in rows:
        by_series.setdefault(r.series_code, {})[r.trade_date] = r.close
    return by_series


# ── モメンタム helper ──────────────────────────────────────────────────────

def _momentum(closes: list, dates: list, snap_idx: int, long_months: int) -> float | None:
    """12-1 モメンタム（log リターン）。データ不足は None。"""
    short_months = 1
    snap_date = dates[snap_idx]
    from datetime import date as _date, timedelta as _td
    ref = _date.fromisoformat(snap_date)
    short_cutoff = (ref - _td(days=short_months * 30)).isoformat()
    long_cutoff  = (ref - _td(days=long_months  * 30)).isoformat()
    eligible = [(dates[i], closes[i]) for i in range(snap_idx + 1)
                if closes[i] and closes[i] > 0]
    if not eligible:
        return None
    short_cands = [(d, c) for d, c in eligible if d <= short_cutoff]
    long_cands  = [(d, c) for d, c in eligible if d <= long_cutoff]
    if not short_cands or not long_cands:
        return None
    return math.log(short_cands[-1][1] / long_cands[-1][1])


# ── スナップショット構築（M-1/M-2 共通。build_interactions=False で M-2 用）──

def build_snapshots(
    prices_by_co: dict,
    fin_by_co: dict,
    companies: dict,
    macro_cache: dict,
    fin_features: list[str],
    macro_names: list[str],
    use_momentum: bool,
    mom_window: int,
    min_coverage: float,
    build_interactions: bool = True,
) -> tuple[dict, dict, dict, list[str]]:
    """M-1/M-2 共有スナップショット構築。

    build_interactions=True（M-1 既定）: fin×macro 交差項を生成。
    build_interactions=False（M-2）: 交差項なし＝同一母集団を保証しつつ特徴量を削減。
    """
    use_macro = bool(macro_names)
    momentum_name = ["momentum_12m1"] if use_momentum else []

    interaction_names: list[str] = []
    if build_interactions and use_macro:
        for fn in fin_features:
            for mn in macro_names:
                interaction_names.append(f"{fn}_x_{mn}")

    all_feat_names = fin_features + macro_names + momentum_name + interaction_names
    n_feat = len(all_feat_names)

    samples_by_ym: dict[str, list] = defaultdict(list)
    sample_meta_by_ym: dict[str, list] = defaultdict(list)
    current_snaps: dict[str, tuple] = {}
    min_rows = HORIZON_WEEKS + 4
    macro_memo: dict[str, dict] = {}

    for edinet_code, price_rows in prices_by_co.items():
        n = len(price_rows)
        if n < min_rows:
            continue
        fin_recs = fin_by_co.get(edinet_code, [])
        if not fin_recs:
            continue

        dates  = [r.trade_date for r in price_rows]
        closes = [r.close_last  for r in price_rows]

        month_ends = [
            i for i in range(n - 1) if dates[i][:7] != dates[i + 1][:7]
        ] + [n - 1]

        for snap_idx in month_ends:
            if snap_idx < 4:
                continue
            snap_date = dates[snap_idx]
            snap_ym   = snap_date[:7]
            is_current = (snap_idx == n - 1)
            has_future = (snap_idx + HORIZON_WEEKS < n)

            fin_rec = _find_applicable_fin(fin_recs, snap_date)
            if fin_rec is None:
                continue

            fin_row: list[float] = []
            ok = True
            for fn in fin_features:
                v = getattr(fin_rec, fn, None)
                if v is None:
                    ok = False
                    break
                fin_row.append(float(v))
            if not ok:
                continue

            macro_row: list[float] = []
            macro_dict: dict[str, float] = {}
            if use_macro:
                m_feats = macro_memo.get(snap_date)
                if m_feats is None:
                    m_feats = _macro_from_cache(macro_cache, snap_date, macro_names)
                    macro_memo[snap_date] = m_feats
                if any(v is None for v in m_feats.values()):
                    continue
                for mn in macro_names:
                    val = m_feats[mn]
                    macro_row.append(float(val))  # type: ignore[arg-type]
                    macro_dict[mn] = float(val)   # type: ignore[arg-type]

            mom_row: list[float] = []
            if use_momentum:
                mom = _momentum(closes, dates, snap_idx, mom_window)
                if mom is None:
                    continue
                mom_row = [mom]

            industry = (
                fin_rec.industry
                or (companies.get(edinet_code) and companies[edinet_code].industry)
                or "不明"
            )

            size_val = getattr(fin_rec, "bs_total_assets", None)
            size_val = float(size_val) if (size_val is not None and size_val > 0) else None

            inter_row: list[float] = []
            if build_interactions and use_macro:
                for fn, fv in zip(fin_features, fin_row):
                    for mn in macro_names:
                        inter_row.append(fv * macro_dict[mn])

            feat_row = fin_row + macro_row + mom_row + inter_row

            non_null = sum(1 for v in feat_row if v == v)
            if non_null / n_feat < min_coverage:
                continue

            if has_future:
                c_snap, c_fut = closes[snap_idx], closes[snap_idx + HORIZON_WEEKS]
                if c_snap and c_fut and c_snap > 0 and c_fut > 0:
                    samples_by_ym[snap_ym].append((feat_row, math.log(c_fut / c_snap)))
                    sample_meta_by_ym[snap_ym].append((industry, size_val))

            if is_current:
                comp = companies.get(edinet_code)
                current_snaps[edinet_code] = (feat_row, {
                    "sec_code":     fin_rec.sec_code or (comp.sec_code if comp else ""),
                    "company_name": fin_rec.company_name or (comp.name if comp else edinet_code),
                    "industry":     industry,
                    "size":         size_val,
                    "price_rows":   price_rows,
                    "snap_date":    snap_date,
                })

    return dict(samples_by_ym), dict(sample_meta_by_ym), current_snaps, all_feat_names


# ── producer スコア ────────────────────────────────────────────────────────

def producer_scores(meta: dict, loadings: dict, macro_snapshot: dict | None = None) -> dict:
    """macro_beta 推論結果から per-stock μ・R_macro・R1' を算出（ADR-0002 §5）。"""
    factors = list(meta.get("selected_factors") or [])
    cov = meta.get("factor_cov") or []
    out: dict = {}
    for code, fmap in loadings.items():
        beta = [float(fmap.get(f, (0.0, None))[0]) for f in factors]
        rec: dict = {"r_macro": macro_risk_exposure(beta, cov) if (cov and beta) else 0.0}
        if macro_snapshot is not None:
            m = [float(macro_snapshot.get(f) or 0.0) for f in factors]
            se = [float(fmap.get(f, (0.0, 0.0))[1] or 0.0) for f in factors]
            a_mean, a_se = fmap.get("_intercept", (0.0, 0.0))
            a_se = float(a_se or 0.0)
            rec["mu"] = float(a_mean) + sum(b * mm for b, mm in zip(beta, m))
            rec["r1_prime"] = math.sqrt(a_se ** 2 + sum((s * mm) ** 2 for s, mm in zip(se, m)))
        out[code] = rec
    return out


def get_producer_scores(db: Any, macro_snapshot: dict | None = None) -> dict:
    """DB から macro_beta を読み producer_scores を返す。未蓄積なら {}（graceful degrade）。"""
    try:
        from database import get_macro_beta
        meta, loadings = get_macro_beta(db)
        if not meta or not loadings:
            return {}
        return producer_scores(meta, loadings, macro_snapshot)
    except Exception:
        return {}


# ── アウトオブサンプル検証（OOF）: 無リーク walk-forward 予測のモデル評価（ADR-0004）──
# 既存「バックテスト」(/api/backtest・preset/as-of ポートフォリオ模擬) とは別概念。
# こちらは「μ̂ が将来リターンを順序付けるか」を OOF 予測のみで評価する（再学習・追加価格取得なし）。
# M-2（macro_gbdt）が使用。M-1 も同じ residuals を持つため後付け可能（共有ヘルパ・ADR-0004 §6）。

def _avg_ranks(vals: list) -> list:
    """同順位を平均順位に割り当てた順位列（1始まり）を返す。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0   # i..j の平均順位（1始まり）
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list, ys: list) -> float | None:
    """Spearman 順位相関（= 順位の Pearson）。n<3 または無分散なら None。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def oof_backtest(residuals_by_ym: dict, n_quantiles: int = 5) -> dict:
    """無リーク OOF 予測から「アウトオブサンプル検証（OOF）」指標を算出する（ADR-0004）。

    residuals_by_ym = {test_ym: [(yhat, y_true), ...]}（walk_forward_cv_monthly の
    return_residuals=True 出力・テストサンプル順）。再学習・追加の価格取得は不要。

    手法（ADR-0004「分位の作り方」）:
      - 各 test_ym 内で yhat を横断ランク→ n_quantiles 分位→分位平均 y_true（期内）
        →期間平均（per-period cross-sectional・μ̂ 水準の時系列ドリフトに頑健）。
      - rank-IC = Spearman(yhat, y_true) を fold 毎→ mean/std/n。
      - long_short_spread = top分位平均 − bottom分位平均（期間平均）。
      - hit_rate = top分位 > bottom分位 だった期の割合。
      - 期内サンプルが n_quantiles*2 未満の期は分位計算から自動除外（IC には使用）。
    quantile_returns[0]=最低 μ̂ バケット, [-1]=最高 μ̂ バケットの実現リターン。
    """
    yms = sorted(residuals_by_ym.keys())
    n_oof = sum(len(residuals_by_ym[y]) for y in yms)

    # rank-IC（fold 毎・サンプル<3 の期は除外）
    ics: list[float] = []
    for ym in yms:
        pairs = residuals_by_ym[ym]
        ic = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append(ic)
    ic_mean = statistics.mean(ics) if ics else None
    ic_std = statistics.pstdev(ics) if len(ics) > 1 else (0.0 if ics else None)

    # 期内横断分位リターン（各期で yhat 昇順→ n_quantiles 等分→分位平均 y_true）
    q_sums = [0.0] * n_quantiles
    q_periods = 0
    ls_spreads: list[float] = []
    hits = 0
    for ym in yms:
        pairs = residuals_by_ym[ym]
        if len(pairs) < n_quantiles * 2:
            continue
        ordered = sorted(pairs, key=lambda p: p[0])   # yhat 昇順
        m = len(ordered)
        q_means = []
        for q in range(n_quantiles):
            lo = q * m // n_quantiles
            hi = (q + 1) * m // n_quantiles
            seg = ordered[lo:hi]
            q_means.append(sum(p[1] for p in seg) / len(seg))
        for q in range(n_quantiles):
            q_sums[q] += q_means[q]
        q_periods += 1
        ls = q_means[-1] - q_means[0]   # top（高 yhat）− bottom（低 yhat）
        ls_spreads.append(ls)
        if ls > 0:
            hits += 1

    quantile_returns = [round(s / q_periods, 6) for s in q_sums] if q_periods else []
    long_short_spread = round(statistics.mean(ls_spreads), 6) if ls_spreads else None
    hit_rate = round(hits / q_periods, 4) if q_periods else None

    return {
        "n_quantiles":        n_quantiles,
        "n_periods":          len(yms),
        "n_periods_quantile": q_periods,
        "n_oof_samples":      n_oof,
        "quantile_returns":   quantile_returns,
        "rank_ic": {
            "mean": round(ic_mean, 4) if ic_mean is not None else None,
            "std":  round(ic_std, 4) if ic_std is not None else None,
            "n":    len(ics),
        },
        "long_short_spread": long_short_spread,
        "hit_rate":          hit_rate,
    }
