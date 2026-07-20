"""売り候補ランキング（保有銘柄の売り時判定）。

買い系モデル（recommend / gap_analysis 等）が全銘柄ユニバースから「買い」を探すのに対し、
本プラグインは **ユーザーが入力した保有銘柄リスト**の中から「売るべき銘柄と売り時」を
ランキングする。観点は買い系の「逆」:

  ① 割高度（gap_ratio が負＝割高）         … gap_analysis / sector_ols の割安スクリーニングの逆
  ② 業績悪化（ROE・営業利益率・CF・成長率の低さ） … recommend の優良スコアの逆
  ③ ネットキャッシュ余力の毀損（nc_ratio が低い） … 清原式ネットキャッシュ分析の逆（安全マージン消失）
  ④ 価格モメンタム（タイミング）            … 買い系には無い「売り時」軸

スコア設計（スケール整合）:
  各シグナルは「高いほど良い（売る理由が小さい）」指標なので、最新年度ユニバース全体で
  winsorize → z 標準化し、`売りスコア = Σ w_i·(−zstd_i) / Σ w_i`（w_i ≥ 0）で合成する。
  ユニバース平均並みの銘柄は ≈0、割高・業績不振の銘柄ほど正に大きくなる。
  CLAUDE.md「分析モデルの次元整合性」に従い、％指標（gap_ratio・rev_growth）と無次元 z を
  混在させず、全シグナルを同一ユニバースで標準化してから重み付けする。

タイミングは別軸（trend）として算出し、アクションラベル（SELL/REDUCE/HOLD）を補正する
（割高でも上昇中は即売りを避け、下落中は一段引き上げる）。購入単価は損益（PnL）表示のみで
スコアには使わない。
"""
import re
from typing import Any

from .base import AnalysisPlugin
from .utils import winsorize, normalize_transform
from .net_cash_analysis import compute_net_cash, compute_nc_ratio


# 売りシグナルに使う指標（すべて「高い＝売る理由が小さい」向き）。
# 多くは financial_metrics VIEW 列だが、nc_ratio は VIEW 列ではなく実行時計算（_resolve_metric）。
# UI のウェイトグリッドと一致させる（static/js/analysis.js: SELL_WEIGHT_LABELS）。
SELL_METRICS = ["gap_ratio", "roe", "op_margin", "cf_ratio", "rev_growth", "equity_ratio", "nc_ratio", "mu", "neg_r_macro"]

# プリセット（ウェイトは ≥0。値が大きいほどその観点を売り判断で重視）。
# nc_ratio = 清原式ネットキャッシュ比率の逆観点（クッション消失＝安全マージン喪失を売り軸へ）。
# mu       = M-1 期待リターン（高い＝保有理由あり＝売る理由が小さい）。M-1 未実行なら graceful-degrade。
# neg_r_macro = −R_macro（M-1 マクロリスク曝露の符号反転。高い＝低リスク＝売る理由が小さい）。
PRESETS = {
    # マクロ予測型: μ（期待リターン）と −Rᴹ（マクロリスク）の2軸のみで売り候補を判定する既定プリセット。
    # スコアは Σw(-z)/Σw で正規化されるため比率のみ有意（μ を主・リスクを従に）。両シグナルとも
    # 選択 μ モデル（既定 M-2）の実行結果に依存し、未実行なら全保有が「データ不足」になる（§10.5）。
    "マクロ予測型": {"mu": 1.0, "neg_r_macro": 0.5},
    "バランス型":   {"gap_ratio": 1.0, "roe": 1.0, "op_margin": 1.0, "cf_ratio": 0.8, "rev_growth": 0.6, "equity_ratio": 0.4, "nc_ratio": 0.4, "mu": 0.5, "neg_r_macro": 0.3},
    "割高警戒型":   {"gap_ratio": 2.5, "roe": 0.5, "op_margin": 0.5, "rev_growth": 0.3, "nc_ratio": 0.8, "neg_r_macro": 0.8},
    "業績悪化重視": {"roe": 2.0, "op_margin": 1.5, "cf_ratio": 1.0, "rev_growth": 1.5, "gap_ratio": 0.5, "nc_ratio": 0.3, "mu": 0.3},
}


def _resolve_metric(r, m: str) -> float | None:
    """売り指標の値を取り出す。多くは VIEW 列だが nc_ratio は実行時計算する。

    nc_ratio = 清原式ネットキャッシュ比率（高い＝クッション厚い＝売る理由が小さい）。
    クッション消失（低い/負）の銘柄ほど -z が大きくなり売りスコアへ加点される。
    """
    if m == "nc_ratio":
        nc = compute_net_cash(getattr(r, "bs_current_assets", None),
                              getattr(r, "bs_investment_securities", None),
                              getattr(r, "bs_total_liabilities", None))
        return compute_nc_ratio(nc, getattr(r, "market_cap", None))
    return getattr(r, m, None)

# トレンド判定の閾値（13週リターン）。
_TREND_UP   = 0.10    # +10% 以上 → 上昇
_TREND_DOWN = -0.10   # −10% 以下 → 下落
_MIN_WEEKS_FOR_TREND = 8   # 週次データがこれ未満なら trend=不明（補正しない）


def parse_holdings(text: str) -> tuple[list[dict], list[str]]:
    """保有入力テキストを構造化する。

    1 行 = `証券コード[, @取得単価][, 購入日]`。区切りはカンマ/空白いずれも可。
      例: "7203, @1500, 2024-03-10" / "6758 @12000" / "9984"
    取得単価は `@1500` でも `1500` でも可。`YYYY-MM-DD` 形式のトークンは購入日とみなす。

    Returns: (parsed, invalid)
      parsed  = [{"sec_code", "avg_cost"(float|None), "buy_date"(str|None)}, ...]
      invalid = 解釈できなかった行の原文リスト
    """
    parsed: list[dict] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = re.split(r"[,\s]+", line)
        code = tokens[0].strip().lstrip("#")
        if not re.fullmatch(r"\d{3,4}", code):
            invalid.append(line)
            continue
        if code in seen:        # 重複行はスキップ（先勝ち）
            continue
        seen.add(code)
        avg_cost = None
        buy_date = None
        for tok in tokens[1:]:
            tok = tok.strip()
            if not tok:
                continue
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
                buy_date = tok
                continue
            num = tok.lstrip("@¥\\")
            try:
                avg_cost = float(num)
            except ValueError:
                pass   # 不明トークンは無視（コードは有効なので行自体は採用）
        parsed.append({"sec_code": code, "avg_cost": avg_cost, "buy_date": buy_date})
    return parsed, invalid


def _compute_trend(weekly_rows: list) -> dict:
    """週次株価から価格モメンタム（タイミング軸）を算出する。

    weekly_rows: StockPriceWeekly の行（同一 edinet_code）。week_start 昇順でなくてよい。
    Returns: {"trend", "ret_13w"(%|None), "drawdown_52w"(%|None), "last_close"(float|None)}
    """
    series = sorted(
        ((r.week_start, r.close_last) for r in weekly_rows if r.close_last and r.close_last > 0),
        key=lambda x: x[0],
    )
    if len(series) < 2:
        return {"trend": "不明", "ret_13w": None, "drawdown_52w": None,
                "last_close": series[-1][1] if series else None}

    closes = [c for _, c in series]
    last_close = closes[-1]

    def _ago(n_weeks: int) -> float:
        idx = len(closes) - 1 - n_weeks
        return closes[idx] if idx >= 0 else closes[0]

    ret_13w = None
    if len(closes) >= _MIN_WEEKS_FOR_TREND:
        prev = _ago(13)
        if prev > 0:
            ret_13w = last_close / prev - 1.0

    high_52w = max(closes[-52:])
    drawdown_52w = (last_close / high_52w - 1.0) if high_52w > 0 else None

    if ret_13w is None:
        trend = "不明"
    elif ret_13w <= _TREND_DOWN:
        trend = "下落"
    elif ret_13w >= _TREND_UP:
        trend = "上昇"
    else:
        trend = "横ばい"

    return {
        "trend": trend,
        "ret_13w": round(ret_13w * 100, 1) if ret_13w is not None else None,
        "drawdown_52w": round(drawdown_52w * 100, 1) if drawdown_52w is not None else None,
        "last_close": last_close,
    }


# アクションラベルの段階（昇順＝売り圧力が強い）。タイミング補正で 1 段ずらす。
_ACTIONS = ["HOLD", "REDUCE", "SELL"]


def _base_action(score: float | None, sell_th: float, reduce_th: float) -> str:
    if score is None:
        return "データ不足"
    if score >= sell_th:
        return "SELL"
    if score >= reduce_th:
        return "REDUCE"
    return "HOLD"


def _apply_timing(action: str, trend: str) -> str:
    """トレンドでアクションを補正する。
    下落 → 1 段引き上げ（売り圧力↑）。上昇 → SELL は REDUCE へ緩和（上昇中の即売り回避）。
    """
    if action not in _ACTIONS:
        return action            # データ不足はそのまま
    i = _ACTIONS.index(action)
    if trend == "下落":
        i = min(i + 1, len(_ACTIONS) - 1)
    elif trend == "上昇" and action == "SELL":
        i = _ACTIONS.index("REDUCE")
    return _ACTIONS[i]


class SellRankingPlugin(AnalysisPlugin):
    name = "sell_ranking"
    label = "売り候補ランキング"
    description = (
        "保有銘柄リストの中から、割高度（回帰乖離）・業績悪化（収益性/CF/成長）・ネットキャッシュ余力の"
        "毀損・価格モメンタムを総合して「売るべき銘柄と売り時」をランキングします。各銘柄に SELL/REDUCE/HOLD を付与します。"
    )
    depends_on = ["sector_ols"]   # 割高度（gap_ratio）に regression_results を使うため
    category = "⑤ 保有を見直す"
    ui_order = 510

    def params_schema(self) -> dict:
        return {
            "holdings": {
                "type": "textarea",
                "dtype": "str",
                "label": "保有銘柄（1行ずつ: 証券コード[, @取得単価][, 購入日]）",
                "default": "",
                "description": "例: 7203, @1500, 2024-03-10 / 6758 @12000 / 9984",
            },
            "preset": {
                "type": "select",
                "label": "プリセット",
                "options": [{"value": k, "label": k} for k in PRESETS],
                "default": "マクロ予測型",
                "description": "カスタムウェイト未指定時のフォールバック",
            },
            "weights": {
                "type": "weights",
                "label": "観点ウェイト",
                "metrics": SELL_METRICS,
                "default": None,
                "optional": True,
                "description": "各観点を売り判断でどれだけ重視するか（0〜3）",
            },
            "sell_threshold": {
                "type": "slider", "dtype": "float",
                "label": "SELL 閾値（売りスコア）",
                "min": 0.0, "max": 2.0, "step": 0.1, "default": 0.8,
            },
            "reduce_threshold": {
                "type": "slider", "dtype": "float",
                "label": "REDUCE 閾値（売りスコア）",
                "min": 0.0, "max": 2.0, "step": 0.1, "default": 0.3,
            },
            "timing_adjust": {
                "type": "checkbox", "dtype": "bool",
                "label": "価格トレンドでラベル補正",
                "default": True,
                "description": "下落トレンドは売り圧力↑、上昇トレンドは即売り回避",
            },
            "min_coverage": {
                "type": "slider", "dtype": "float",
                "label": "必須指標カバレッジ（0-1）",
                "min": 0.0, "max": 1.0, "step": 0.1, "default": 0.4,
                "description": "値が揃う重み付き指標の比率下限。下回る銘柄は『データ不足』表示。",
            },
            "year": {
                "type": "number", "dtype": "int",
                "label": "対象年度（空=最新）",
                "default": None, "optional": True,
            },
            "mu_source": {
                "type": "select",
                "label": "μ（期待リターン）の出所",
                "options": [
                    {"value": "macro_risk_return", "label": "M-1: マクロ×リスク-リターン（OLS）"},
                    {"value": "macro_gbdt",        "label": "M-2: マクロ×財務 勾配ブースティング（XGBoost）"},
                    {"value": "macro_dlm",         "label": "M-3: ベイズ状態空間（時変マクロβ DLM）"},
                ],
                "default": "macro_gbdt",
                "description": "売りスコアの μ / −R_macro 観点に使う推奨モデル（既定 M-2）。未実行なら graceful-degrade（μ除外）。",
            },
            "r3_gate": {
                "type": "slider", "dtype": "float",
                "label": "R3 足切り（M-1 予測SE）",
                "min": 0.0, "max": 0.5, "step": 0.05, "default": 0.0,
                "description": "M-1 の μ 予測SE（r1_prime）がこの値を超える銘柄は SELL を出さない（0=無効・M-2選択時は無効）",
            },
        }

    def execute(self, params: dict, db: Any) -> dict:
        from database import FinancialMetric, StockPriceWeekly, latest_year_subq

        holdings, invalid = parse_holdings(params["holdings"])
        preset       = params["preset"]
        weights_raw  = params["weights"] or PRESETS.get(preset, PRESETS["バランス型"])
        # 既知メトリクスのみ・負ウェイトは0クリップ（売り重視度は非負）
        weights      = {m: max(0.0, float(w)) for m, w in weights_raw.items()
                        if m in SELL_METRICS and w}
        sell_th      = params["sell_threshold"]
        reduce_th    = params["reduce_threshold"]
        timing_adj   = params["timing_adjust"]
        min_coverage = params["min_coverage"]
        year         = params["year"]
        mu_source    = params.get("mu_source", "macro_risk_return")

        total_weight = sum(weights.values())
        if not holdings or total_weight == 0:
            return {"count": 0, "presets": PRESETS, "metrics": SELL_METRICS,
                    "results": [], "not_found": [], "invalid": invalid,
                    "gap_available": True, "mu_available": False,
                    "mu_source": mu_source}

        # ── ① ユニバース標準化パラメータ（最新年度の全銘柄から winsorize → mean/sd）──
        # 標準化の基準は「現在投資可能な母集団」に合わせるため、上場廃止銘柄は除く（Issue #315）。
        subq = latest_year_subq(db, FinancialMetric)
        uni_q = (db.query(FinancialMetric)
                   .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                               (FinancialMetric.year == subq.c.max_year))
                   .filter(FinancialMetric.is_active.isnot(False)))
        if year:
            uni_q = uni_q.filter(FinancialMetric.year == int(year))
        universe = uni_q.all()

        # ── 期待リターン μ producer スコア（mu_source で M-1/M-2 切替・graceful-degrade）──
        # ADR-0004: mu_source=macro_risk_return（既定）/ macro_gbdt。両者とも
        # read_producer_scores が {edinet_code: {mu, r_macro, r1_prime}} を返す共通契約。
        # r_macro は共有 macro_beta 由来でモデル非依存（neg_r_macro は mu_source に依らず不変）。
        import datetime as _dt
        from plugins import get_plugin as _get_plugin
        _producer = _get_plugin(mu_source)
        mu_scores: dict = {}
        if _producer and _producer.produced_output(db):
            try:
                from database import get_macro_beta
                from plugins.utils import get_macro_features
                _meta_m1, _ = get_macro_beta(db)
                _sel_factors = (_meta_m1 or {}).get("selected_factors") or []
                _macro_snap: dict | None = None
                if _sel_factors:
                    _raw_snap = get_macro_features(db, _dt.date.today().isoformat(), _sel_factors)
                    _macro_snap = {k: v for k, v in _raw_snap.items() if v is not None} or None
                mu_scores = _producer.read_producer_scores(db, _macro_snap)
            except Exception:
                pass
        mu_available = bool(mu_scores)

        stats: dict[str, tuple[float, float]] = {}
        for m in weights:
            if m == "mu":
                vals = [float(ps["mu"]) for ps in mu_scores.values() if ps.get("mu") is not None]
            elif m == "neg_r_macro":
                vals = [-float(ps["r_macro"]) for ps in mu_scores.values() if ps.get("r_macro") is not None]
            else:
                vals = [v for v in (_resolve_metric(r, m) for r in universe) if v is not None]
            if len(vals) < 4:
                continue
            wv, _, _ = winsorize(vals)
            mean_ = sum(wv) / len(wv)
            var = sum((v - mean_) ** 2 for v in wv) / (len(wv) - 1)
            sd = var ** 0.5 or 1.0
            stats[m] = (mean_, sd)
        gap_available = "gap_ratio" not in weights or "gap_ratio" in stats

        # ── 保有銘柄の最新年度レコードを sec_code で引く ──
        codes = [h["sec_code"] for h in holdings]
        rows = {r.sec_code: r for r in (
            db.query(FinancialMetric)
              .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                          (FinancialMetric.year == subq.c.max_year))
              .filter(FinancialMetric.sec_code.in_(codes))
              .all()
        )}

        # ── 価格モメンタム（保有 edinet_code をまとめて取得）──
        ecodes = [r.edinet_code for r in rows.values() if r.edinet_code]
        weekly_by_ec: dict[str, list] = {}
        if ecodes:
            for w in (db.query(StockPriceWeekly)
                        .filter(StockPriceWeekly.edinet_code.in_(ecodes)).all()):
                weekly_by_ec.setdefault(w.edinet_code, []).append(w)

        not_found: list[str] = []
        scored: list[dict] = []
        for h in holdings:
            r = rows.get(h["sec_code"])
            if r is None:
                not_found.append(h["sec_code"])
                continue

            weighted_sum = 0.0
            weight_present = 0.0
            detail: dict[str, float | None] = {}
            _ec = r.edinet_code
            _ps = mu_scores.get(_ec) if _ec else None
            for m, w in weights.items():
                if m == "mu":
                    raw = _ps.get("mu") if _ps else None
                elif m == "neg_r_macro":
                    _rm = _ps.get("r_macro") if _ps else None
                    raw = -_rm if _rm is not None else None
                else:
                    raw = _resolve_metric(r, m)
                if raw is not None and m in stats:
                    mean_, sd = stats[m]
                    zstd = normalize_transform(float(raw), mean_, sd, "zscore")
                    weighted_sum += w * (-zstd)        # 平均より下＝売り側に加点
                    weight_present += w
                detail[m] = round(float(raw), 4) if raw is not None else None
            coverage = weight_present / total_weight if total_weight else 0.0

            score = None
            if weight_present > 0 and coverage >= min_coverage:
                score = weighted_sum / weight_present

            mom = _compute_trend(weekly_by_ec.get(r.edinet_code, []))
            action = _base_action(score, sell_th, reduce_th)
            if timing_adj and action in _ACTIONS:
                action = _apply_timing(action, mom["trend"])
            # R3 足切りゲート: M-1 の μ 予測SE（r1_prime）が閾値超 → SELL を出さない。
            # mu_source=macro_gbdt（M-2）は r1_prime を持たないため無効（ADR-0004・no-op）。
            _r3_gate = params.get("r3_gate", 0.0)
            if _r3_gate and action == "SELL" and _ps and mu_source not in ("macro_gbdt", "macro_dlm"):
                _r1p = _ps.get("r1_prime")
                if _r1p is not None and float(_r1p) > _r3_gate:
                    action = "REDUCE"

            last_close = mom["last_close"] if mom["last_close"] is not None else r.stock_price
            pnl_pct = None
            if h["avg_cost"] and h["avg_cost"] > 0 and last_close:
                pnl_pct = round((last_close - h["avg_cost"]) / h["avg_cost"] * 100, 1)

            scored.append({
                "sec_code":     r.sec_code,
                "edinet_code":  r.edinet_code,
                "company_name": r.company_name,
                "industry":     r.industry,
                "year":         r.year,
                "score":        round(score, 4) if score is not None else None,
                "coverage":     round(coverage, 2),
                "action":       action,
                "trend":        mom["trend"],
                "ret_13w":      mom["ret_13w"],
                "drawdown_52w": mom["drawdown_52w"],
                "gap_ratio":    r.gap_ratio,
                "roe":          r.roe,
                "op_margin":    r.op_margin,
                "rev_growth":   r.rev_growth,
                "last_close":   round(last_close, 1) if last_close else None,
                "avg_cost":     h["avg_cost"],
                "buy_date":     h["buy_date"],
                "pnl_pct":      pnl_pct,
                "market_cap":   r.market_cap,
                # 保有銘柄はユーザー入力のため is_active では除外しないが、上場廃止済みなら
                # 明示的に知らせる（Issue #315・ゾンビ化した保有への気付き）。
                "is_active":      r.is_active is not False,
                "delisted_date":  r.delisted_date.isoformat() if r.delisted_date else None,
                "detail":       detail,
            })

        # 売りスコア降順（None=データ不足は末尾）→ rank 付与
        scored.sort(key=lambda x: (x["score"] is not None, x["score"] or 0.0), reverse=True)
        for rank, item in enumerate(scored, 1):
            item["rank"] = rank

        n_sell   = sum(1 for s in scored if s["action"] == "SELL")
        n_reduce = sum(1 for s in scored if s["action"] == "REDUCE")
        return {
            "count":         len(scored),
            "n_sell":        n_sell,
            "n_reduce":      n_reduce,
            "n_hold":        sum(1 for s in scored if s["action"] == "HOLD"),
            "presets":       PRESETS,
            "metrics":       SELL_METRICS,
            "gap_available": gap_available,
            "mu_available":  mu_available,
            "mu_source":     mu_source,
            "results":       scored,
            "not_found":     not_found,
            "invalid":       invalid,
        }


plugin = SellRankingPlugin()
