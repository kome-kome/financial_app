"""
株価リターン予測プラグイン

StockPriceWeekly（全履歴の週次終値 close_last）と FinancialRecord（年次財務）を
edinet_code で結合し、月次スナップショットを学習データとして OLS でN期先の対数リターンを
予測する。価格系列は週次刻み（容量対策で日次は直近6か月のみ保持のため）。

特徴量:
  価格系: MA20乖離率 / 60期ボラティリティ / RSI(14) / ATR比率（週次は close 退化）
  財務系: PER / PBR / ROE / 自己資本比率 / R&D集約度 / D&A集約度 / Zスコア群 / gap_ratio（無次元）
          ※ R&D集約度=pl_rd_expenses/売上・D&A集約度=pl_depreciation/売上（C2列の結線・VIEW算出）

次元整合性（CLAUDE.md制約準拠）:
  目的変数 = 対数リターン（無次元）
  説明変数 = 無次元比率・Zスコア・倍率 のみ
  → 次元不整合なし

前処理: winsorize(p1-p99) → z-score正規化 → OLS
評価: 月次ウォークフォワードCV（ルックアヘッドバイアスなし）
"""
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .base import AnalysisPlugin
from .utils import normalize, normalize_transform, ols, walk_forward_cv_monthly, winsorize

FINANCIAL_LAG_DAYS = 45  # 決算公表ラグ: period_end からこの日数後に財務データが利用可能とみなす

# 財務特徴量の選択肢（無次元比率・Zスコアのみ）
FIN_FEATURE_OPTIONS = [
    {"value": "per",          "label": "PER（株価収益率）"},
    {"value": "pbr",          "label": "PBR（株価純資産倍率）"},
    {"value": "roe",          "label": "ROE（%）"},
    {"value": "equity_ratio", "label": "自己資本比率（%）"},
    {"value": "rd_intensity", "label": "研究開発集約度 R&D/売上（%・C2）"},
    {"value": "da_intensity", "label": "減価償却集約度 D&A/売上（%・C2）"},
    {"value": "z_op_margin",  "label": "営業利益率Zスコア"},
    {"value": "z_roe",        "label": "ROE Zスコア"},
    {"value": "z_cf_ratio",   "label": "CF比率Zスコア"},
    {"value": "gap_ratio",    "label": "乖離率 gap_ratio（%）"},
]

FIN_FEATURE_LABELS = {o["value"]: o["label"] for o in FIN_FEATURE_OPTIONS}

PRICE_FEATURE_LABELS = {
    "ma20_dev":  "MA20乖離率",
    "vol60":     "60日ボラティリティ",
    "rsi14":     "RSI(14)",
    "atr_ratio": "ATR比率(14)",
}

DEFAULT_FIN_FEATURES = ["per", "pbr", "roe"]


# ── 価格特徴量ヘルパー（numpy ベース・末尾値のみ計算） ─────────────────
# スナップショット数 << 価格履歴長 のため、全行ベクトル化（pandas rolling）よりも
# 末尾 n+1 本のみで計算する方式が高速（ベンチで約 2.8 倍速）。
# 過去に `_compute_all_price_features_vec` で全インデックス計算を試したが
# 実ワークロード（500日×24スナップ）でかえって遅くなったため不採用。

def _ma(prices: list, n: int) -> float | None:
    """末尾 n 本の単純移動平均。"""
    if len(prices) < n:
        return None
    return float(np.mean(prices[-n:]))


def _log_vol(closes: list, n: int = 60) -> float | None:
    """過去 n 日のログリターン標準偏差（population）。"""
    if len(closes) < n + 1:
        return None
    arr = np.asarray(closes[-(n + 1):], dtype=float)
    rets = np.log(arr[1:] / arr[:-1])
    valid = rets[np.isfinite(rets)]
    if len(valid) < 5:
        return None
    return float(np.std(valid, ddof=0))


def _rsi(closes: list, n: int = 14) -> float | None:
    """RSI(n)。"""
    if len(closes) < n + 1:
        return None
    arr = np.asarray(closes[-(n + 1):], dtype=float)
    changes = arr[1:] - arr[:-1]
    avg_gain = float(np.mean(np.clip(changes, 0, None)))
    avg_loss = float(np.mean(np.clip(-changes, 0, None)))
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr_ratio(highs: list, lows: list, closes: list, n: int = 14) -> float | None:
    """ATR(n) / 当日終値。"""
    if len(closes) < n + 1:
        return None
    h = np.asarray(highs[-(n + 1):], dtype=float)
    lo = np.asarray(lows[-(n + 1):], dtype=float)
    c = np.asarray(closes[-(n + 1):], dtype=float)
    prev = c[:-1]
    tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - prev), np.abs(lo[1:] - prev)])
    atr = float(np.mean(tr))
    curr_close = float(c[-1])
    if curr_close <= 0:
        return None
    return atr / curr_close


def _compute_price_features(closes: list, highs: list, lows: list, snap_idx: int) -> dict | None:
    """snap_idx 時点の価格特徴量を計算。None があれば None を返す。"""
    c = closes[: snap_idx + 1]
    h = highs[: snap_idx + 1]
    lo = lows[: snap_idx + 1]
    curr = c[-1]
    if curr <= 0:
        return None

    ma20 = _ma(c, 20)
    if ma20 is None or ma20 <= 0:
        return None
    ma20_dev = (curr - ma20) / ma20

    vol60 = _log_vol(c, 60)
    if vol60 is None:
        return None

    rsi14 = _rsi(c, 14)
    if rsi14 is None:
        return None

    atr = _atr_ratio(h, lo, c, 14)
    if atr is None:
        return None

    return {"ma20_dev": ma20_dev, "vol60": vol60, "rsi14": rsi14, "atr_ratio": atr}


def _add_days(date_str: str, days: int) -> str:
    """'YYYY-MM-DD' 文字列に days を加算して返す。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _find_applicable_fin(fin_recs: list, snap_date: str):
    """period_end + FINANCIAL_LAG_DAYS <= snap_date を満たす最新の FinancialRecord を返す。"""
    result = None
    for fr in fin_recs:
        if not fr.period_end:
            continue
        avail_date = _add_days(fr.period_end[:10], FINANCIAL_LAG_DAYS)
        if avail_date <= snap_date:
            result = fr
    return result


class PricePredictorPlugin(AnalysisPlugin):
    name = "price_predictor"
    label = "株価リターン予測"
    description = (
        "価格特徴量（MA乖離・ボラティリティ・RSI・ATR）と財務指標（PER・PBR・ROE等）を組み合わせ、"
        "N日先の対数リターンを OLS で予測します。"
        "学習には月次スナップショット × 全企業のパネルデータを使用し、"
        "月次ウォークフォワードCVで評価します（ルックアヘッドバイアスなし）。"
        "【注意】株価履歴データが必要です。先に株価履歴収集を実行してください。"
    )
    depends_on = []

    def params_schema(self) -> dict:
        return {
            "horizon": {
                "type": "select",
                "dtype": "int",   # options の value が整数のため明示（membership 整合）
                "label": "予測期間（日）",
                "options": [
                    {"value": 5,  "label": "5日先"},
                    {"value": 20, "label": "20日先"},
                    {"value": 60, "label": "60日先"},
                ],
                "default": 20,
            },
            "use_price_features": {
                "type": "checkbox",
                "label": "価格特徴量を使用（MA20乖離・ボラティリティ・RSI・ATR）",
                "default": True,
            },
            "features": {
                "type": "multiselect",
                "label": "財務特徴量（無次元比率・Zスコアのみ）",
                "options": FIN_FEATURE_OPTIONS,
                "default": DEFAULT_FIN_FEATURES,
                "description": "次元整合性のため無次元比率・Zスコアのみ選択可能。絶対額は目的変数と次元が異なり使用不可。",
            },
            "top_n": {
                "type": "number",
                "dtype": "int",
                "label": "上位表示件数",
                "default": 30,
                "min": 5,
                "max": 100,
            },
        }

    def _load_data(self, db) -> tuple:
        """週次株価・財務メトリクス・企業マスタを DB から読み込む。"""
        from collections import namedtuple as _nt
        from database import Company, FinancialMetric, StockPriceWeekly

        _PX = _nt("_PX", "edinet_code trade_date close high low")
        raw = (
            db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                     StockPriceWeekly.close_last)
            .order_by(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date)
            .all()
        )
        if not raw:
            raise ValueError(
                "株価履歴データがありません。先に「株価履歴収集」タブで収集を実行してください。"
            )
        prices_by_co: dict[str, list] = defaultdict(list)
        for ec, td, cl in raw:
            prices_by_co[ec].append(_PX(ec, td, cl, None, None))

        fin_by_co: dict[str, list] = defaultdict(list)
        for r in (db.query(FinancialMetric)
                  .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end)
                  .all()):
            fin_by_co[r.edinet_code].append(r)

        companies = {c.edinet_code: c for c in db.query(Company).all()}
        return prices_by_co, fin_by_co, companies

    def _build_snapshots(self, prices_by_co: dict, fin_by_co: dict, companies: dict,
                         use_price: bool, fin_features: list,
                         horizon: int) -> tuple:
        """月次スナップショットの学習データと現在スナップショットを構築する。"""
        price_feat_names = ["ma20_dev", "vol60", "rsi14", "atr_ratio"] if use_price else []
        all_feat_names = price_feat_names + fin_features
        samples_by_ym: dict[str, list] = defaultdict(list)
        current_snaps: dict[str, tuple] = {}
        min_rows = 60 + horizon

        for edinet_code, price_rows in prices_by_co.items():
            n = len(price_rows)
            if n < min_rows:
                continue
            dates  = [r.trade_date for r in price_rows]
            closes = [r.close             for r in price_rows]
            highs  = [r.high or r.close   for r in price_rows]
            lows   = [r.low  or r.close   for r in price_rows]
            fin_recs = fin_by_co.get(edinet_code, [])
            if not fin_recs:
                continue

            month_end_indices: list[int] = [
                i for i in range(n - 1) if dates[i][:7] != dates[i + 1][:7]
            ] + [n - 1]

            for snap_idx in month_end_indices:
                if snap_idx < 60:
                    continue
                snap_date = dates[snap_idx]
                snap_ym   = snap_date[:7]
                is_current = (snap_idx == n - 1)
                has_future = (snap_idx + horizon < n)

                if use_price:
                    pf = _compute_price_features(closes, highs, lows, snap_idx)
                    if pf is None:
                        continue
                    price_feat_row = [pf["ma20_dev"], pf["vol60"], pf["rsi14"], pf["atr_ratio"]]
                else:
                    price_feat_row, pf = [], {}

                fin_rec = _find_applicable_fin(fin_recs, snap_date)
                if fin_rec is None:
                    continue

                fin_feat_row, ok = [], True
                for fname in fin_features:
                    val = getattr(fin_rec, fname, None)
                    if val is None:
                        ok = False
                        break
                    fin_feat_row.append(float(val))
                if not ok:
                    continue

                feat_row = price_feat_row + fin_feat_row

                if has_future:
                    c_snap, c_fut = closes[snap_idx], closes[snap_idx + horizon]
                    if c_snap > 0 and c_fut > 0:
                        samples_by_ym[snap_ym].append((feat_row, math.log(c_fut / c_snap)))

                if is_current:
                    comp = companies.get(edinet_code)
                    current_snaps[edinet_code] = (
                        feat_row,
                        {
                            "sec_code":       fin_rec.sec_code or (comp.sec_code if comp else ""),
                            "company_name":   fin_rec.company_name or (comp.name if comp else edinet_code),
                            "industry":       fin_rec.industry or (comp.industry if comp else ""),
                            "price_features": pf,
                            "fin_features":   {k: getattr(fin_rec, k, None) for k in fin_features},
                        },
                    )

        return samples_by_ym, current_snaps, all_feat_names

    def _fit_final_model(self, samples_by_ym: dict, n_feat: int,
                         all_feat_names: list) -> tuple:
        """全サンプルで OLS 最終モデルを学習し、係数・正規化パラメータを返す。"""
        all_samples = [s for ym_s in samples_by_ym.values() for s in ym_s]
        X_raw = [s[0] for s in all_samples]
        y_raw = [s[1] for s in all_samples]

        final_win_params:  list[tuple] = []
        final_norm_params: list[tuple] = []
        X_norm = [[1.0] + [0.0] * n_feat for _ in range(len(X_raw))]
        for fi in range(n_feat):
            col_w, w_lo, w_hi = winsorize([row[fi] for row in X_raw])
            final_win_params.append((w_lo, w_hi))
            normed, p1, p2 = normalize(col_w, "zscore")
            final_norm_params.append((p1, p2))
            for ri, v in enumerate(normed):
                X_norm[ri][fi + 1] = v

        y_norm, y_mu, y_sd = normalize(y_raw, "zscore")
        result = ols(X_norm, y_norm)
        if not result:
            raise ValueError("OLS の計算に失敗しました。特徴量に多重共線性がある可能性があります。")

        beta = result["beta"]
        feature_weights = {
            fname: {
                "weight": round(beta[i + 1], 6),
                "label":  PRICE_FEATURE_LABELS.get(fname) or FIN_FEATURE_LABELS.get(fname, fname),
            }
            for i, fname in enumerate(all_feat_names)
        }
        return beta, feature_weights, final_win_params, final_norm_params, y_mu, y_sd

    def _score_companies(self, current_snaps: dict, n_feat: int,
                         beta: list, final_win_params: list, final_norm_params: list,
                         y_mu: float, y_sd: float,
                         fin_features: list, top_n: int) -> list:
        """現在スナップショットをスコアリングし上位 top_n を返す。"""
        scored: list[dict] = []
        for edinet_code, (feat_row, info) in current_snaps.items():
            if len(feat_row) != n_feat:
                continue
            x_norm = [1.0]
            for fi, v in enumerate(feat_row):
                w_lo, w_hi = final_win_params[fi]
                v_w = max(w_lo, min(w_hi, v))
                x_norm.append(normalize_transform(v_w, *final_norm_params[fi]))
            pred_log_ret = sum(x_norm[j] * beta[j] for j in range(len(beta))) * y_sd + y_mu
            row: dict = {
                "sec_code":        info["sec_code"],
                "company_name":    info["company_name"],
                "industry":        info["industry"],
                "pred_return_pct": round(pred_log_ret * 100, 2),
            }
            for k, v in info["price_features"].items():
                row[k] = round(v, 4) if v is not None else None
            for k, v in info["fin_features"].items():
                row[k] = round(v, 2) if v is not None else None
            scored.append(row)

        scored.sort(key=lambda r: r["pred_return_pct"] or 0, reverse=True)
        for rank, row in enumerate(scored[:top_n], 1):
            row["rank"] = rank
        return scored[:top_n]

    async def execute(self, params: dict, db: Any) -> dict:
        horizon      = params["horizon"]
        use_price    = params["use_price_features"]
        fin_features = params["features"]
        top_n        = params["top_n"]

        if not use_price and not fin_features:
            raise ValueError("価格特徴量か財務特徴量のいずれかを有効にしてください。")

        prices_by_co, fin_by_co, companies = self._load_data(db)
        samples_by_ym, current_snaps, all_feat_names = self._build_snapshots(
            prices_by_co, fin_by_co, companies, use_price, fin_features, horizon
        )

        total_samples = sum(len(v) for v in samples_by_ym.values())
        n_feat = len(all_feat_names)
        if total_samples < 10:
            raise ValueError(
                f"学習サンプルが不足しています（{total_samples} 件）。"
                "株価履歴を少なくとも3ヶ月以上収集し、財務データも揃っている必要があります。"
            )
        if total_samples <= n_feat + 1:
            raise ValueError(
                f"特徴量数（{n_feat}）に対してサンプル数（{total_samples}）が不足しています。"
                "特徴量を減らすか、データを増やしてください。"
            )

        cv_folds = walk_forward_cv_monthly(
            dict(samples_by_ym), all_feat_names, min_train_months=6, step_months=3
        )
        cv_metrics = {
            "folds":     cv_folds,
            "mean_r2":   round(statistics.mean(f["r2"] for f in cv_folds), 4) if cv_folds else None,
            "mean_rmse": round(statistics.mean(f["rmse"] for f in cv_folds), 4) if cv_folds else None,
            "n_folds":   len(cv_folds),
            "cv_note":   "月次ウォークフォワードCV（ルックアヘッドバイアスなし）",
        }

        beta, feature_weights, win_p, norm_p, y_mu, y_sd = self._fit_final_model(
            samples_by_ym, n_feat, all_feat_names
        )
        results = self._score_companies(
            current_snaps, n_feat, beta, win_p, norm_p, y_mu, y_sd, fin_features, top_n
        )

        return {
            "cv_metrics":      cv_metrics,
            "feature_weights": feature_weights,
            "n_train_samples": total_samples,
            "n_companies":     len(current_snaps),
            "horizon_days":    horizon,
            "results":         results,
        }


plugin = PricePredictorPlugin()
