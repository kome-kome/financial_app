"""
株価リターン予測プラグイン

StockPriceHistory（日次OHLCV）と FinancialRecord（年次財務）を edinet_code で結合し、
月次スナップショットを学習データとして OLS でN日先の対数リターンを予測する。

特徴量:
  価格系: MA20乖離率 / 60日ボラティリティ / RSI(14) / ATR比率(14)
  財務系: PER / PBR / ROE / 自己資本比率 / Zスコア群 / gap_ratio（無次元）

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
                "label": "予測期間（日）",
                "options": [
                    {"value": 5,  "label": "5日先"},
                    {"value": 20, "label": "20日先"},
                    {"value": 60, "label": "60日先"},
                ],
                "default": 20,
            },
            "use_price_features": {
                "type": "boolean",
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
                "label": "上位表示件数",
                "default": 30,
                "min": 5,
                "max": 100,
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        from database import Company, FinancialRecord, StockPriceHistory

        horizon = int(params.get("horizon") or 20)
        use_price = bool(params.get("use_price_features", True))
        fin_features = params.get("features") or DEFAULT_FIN_FEATURES
        if isinstance(fin_features, str):
            fin_features = [f.strip() for f in fin_features.split(",") if f.strip()]
        top_n = max(5, min(100, int(params.get("top_n") or 30)))

        if not use_price and not fin_features:
            raise ValueError("価格特徴量か財務特徴量のいずれかを有効にしてください。")

        # ── データロード ────────────────────────────────────────────────────

        all_prices = (
            db.query(StockPriceHistory)
            .order_by(StockPriceHistory.edinet_code, StockPriceHistory.trade_date)
            .all()
        )
        if not all_prices:
            raise ValueError(
                "株価履歴データがありません。先に「株価履歴収集」タブで収集を実行してください。"
            )

        prices_by_co: dict[str, list] = defaultdict(list)
        for p in all_prices:
            prices_by_co[p.edinet_code].append(p)

        fin_recs_all = (
            db.query(FinancialRecord)
            .order_by(FinancialRecord.edinet_code, FinancialRecord.period_end)
            .all()
        )
        fin_by_co: dict[str, list] = defaultdict(list)
        for r in fin_recs_all:
            fin_by_co[r.edinet_code].append(r)

        companies = {c.edinet_code: c for c in db.query(Company).all()}

        # ── 月次スナップショットの学習データ構築 ──────────────────────────

        # 価格特徴量名リスト
        price_feat_names = ["ma20_dev", "vol60", "rsi14", "atr_ratio"] if use_price else []
        all_feat_names = price_feat_names + fin_features

        # {"YYYY-MM": [(feat_row, log_ret), ...]}
        samples_by_ym: dict[str, list] = defaultdict(list)

        # 現在スナップショット: {edinet_code: (feat_row, company_info)}
        current_snaps: dict[str, tuple] = {}

        min_rows = 60 + horizon  # 価格特徴量計算に必要な最低行数

        for edinet_code, price_rows in prices_by_co.items():
            n = len(price_rows)
            if n < min_rows:
                continue

            dates  = [r.trade_date for r in price_rows]
            closes = [r.close      for r in price_rows]
            highs  = [r.high or r.close  for r in price_rows]
            lows   = [r.low  or r.close  for r in price_rows]

            fin_recs = fin_by_co.get(edinet_code, [])
            if not fin_recs:
                continue

            # 月末インデックスを収集: 月が変わる直前の行 = 当月最終営業日
            month_end_indices: list[int] = []
            for i in range(n - 1):
                if dates[i][:7] != dates[i + 1][:7]:
                    month_end_indices.append(i)
            month_end_indices.append(n - 1)  # 末尾も含める

            for snap_idx in month_end_indices:
                snap_date = dates[snap_idx]
                snap_ym = snap_date[:7]

                # 価格特徴量の計算に必要な最低行数チェック
                if snap_idx < 60:
                    continue

                # 当月を「現在」として扱う場合（最新月 = スコアリング用）
                is_current = (snap_idx == n - 1)

                # 学習データとして使えるか（horizon 日後の終値が必要）
                has_future = (snap_idx + horizon < n)

                # 価格特徴量
                if use_price:
                    pf = _compute_price_features(closes, highs, lows, snap_idx)
                    if pf is None:
                        continue
                    price_feat_row = [pf["ma20_dev"], pf["vol60"], pf["rsi14"], pf["atr_ratio"]]
                else:
                    price_feat_row = []
                    pf = {}

                # 財務特徴量
                fin_rec = _find_applicable_fin(fin_recs, snap_date)
                if fin_rec is None:
                    continue

                fin_feat_row = []
                ok = True
                for fname in fin_features:
                    val = getattr(fin_rec, fname, None)
                    if val is None:
                        ok = False
                        break
                    fin_feat_row.append(float(val))
                if not ok:
                    continue

                feat_row = price_feat_row + fin_feat_row

                # 学習サンプル追加（ターゲットが取得できる場合のみ）
                if has_future:
                    c_snap = closes[snap_idx]
                    c_fut  = closes[snap_idx + horizon]
                    if c_snap > 0 and c_fut > 0:
                        log_ret = math.log(c_fut / c_snap)
                        samples_by_ym[snap_ym].append((feat_row, log_ret))

                # 現在スナップショット（最新月末）
                if is_current:
                    comp = companies.get(edinet_code)
                    current_snaps[edinet_code] = (
                        feat_row,
                        {
                            "sec_code":     fin_rec.sec_code or (comp.sec_code if comp else ""),
                            "company_name": fin_rec.company_name or (comp.name if comp else edinet_code),
                            "industry":     fin_rec.industry or (comp.industry if comp else ""),
                            "price_features": pf,
                            "fin_features": {k: getattr(fin_rec, k, None) for k in fin_features},
                        },
                    )

        # ── サンプル数チェック ──────────────────────────────────────────────

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

        # ── 月次ウォークフォワードCV ────────────────────────────────────────

        cv_folds = walk_forward_cv_monthly(
            dict(samples_by_ym),
            all_feat_names,
            min_train_months=6,   # データが少ない段階では 6ヶ月に緩和（仕様は18ヶ月）
            step_months=3,
        )
        cv_metrics = {
            "folds":    cv_folds,
            "mean_r2":  round(statistics.mean(f["r2"] for f in cv_folds), 4) if cv_folds else None,
            "mean_rmse": round(statistics.mean(f["rmse"] for f in cv_folds), 4) if cv_folds else None,
            "n_folds":  len(cv_folds),
            "cv_note":  "月次ウォークフォワードCV（ルックアヘッドバイアスなし）",
        }

        # ── 全サンプルで最終 OLS 学習 ──────────────────────────────────────

        all_samples = [(feat, ret) for ym_samples in samples_by_ym.values() for feat, ret in ym_samples]
        X_raw = [s[0] for s in all_samples]
        y_raw = [s[1] for s in all_samples]

        # winsorize → zscore 正規化
        final_win_params:  list[tuple] = []
        final_norm_params: list[tuple] = []
        X_norm = [[1.0] + [0.0] * n_feat for _ in range(len(X_raw))]
        for fi in range(n_feat):
            col = [row[fi] for row in X_raw]
            col_w, w_lo, w_hi = winsorize(col)
            final_win_params.append((w_lo, w_hi))
            normed, p1, p2 = normalize(col_w, "zscore")
            final_norm_params.append((p1, p2))
            for ri, v in enumerate(normed):
                X_norm[ri][fi + 1] = v

        y_norm, y_mu, y_sd = normalize(y_raw, "zscore")
        final_result = ols(X_norm, y_norm)
        if not final_result:
            raise ValueError("OLS の計算に失敗しました。特徴量に多重共線性がある可能性があります。")

        beta = final_result["beta"]  # [intercept, b1, b2, ...]

        # 特徴量の重みを出力
        feature_weights: dict[str, dict] = {}
        for i, fname in enumerate(all_feat_names):
            label = PRICE_FEATURE_LABELS.get(fname) or FIN_FEATURE_LABELS.get(fname, fname)
            feature_weights[fname] = {
                "weight": round(beta[i + 1], 6),
                "label":  label,
            }

        # ── 現在スナップショットをスコアリング ──────────────────────────────

        scored: list[dict] = []
        for edinet_code, (feat_row, info) in current_snaps.items():
            if len(feat_row) != n_feat:
                continue
            # winsorize → zscore → OLS 予測
            x_norm = [1.0]
            for fi, v in enumerate(feat_row):
                w_lo, w_hi = final_win_params[fi]
                v_w = max(w_lo, min(w_hi, v))
                p1, p2 = final_norm_params[fi]
                x_norm.append(normalize_transform(v_w, p1, p2))

            pred_norm = sum(x_norm[j] * beta[j] for j in range(len(beta)))
            pred_log_ret = pred_norm * y_sd + y_mu
            pred_return_pct = round(pred_log_ret * 100, 2)

            row = {
                "sec_code":       info["sec_code"],
                "company_name":   info["company_name"],
                "industry":       info["industry"],
                "pred_return_pct": pred_return_pct,
            }
            # 価格特徴量を追加
            for k, v in info["price_features"].items():
                row[k] = round(v, 4) if v is not None else None
            # 財務特徴量を追加
            for k, v in info["fin_features"].items():
                row[k] = round(v, 2) if v is not None else None

            scored.append(row)

        # pred_return_pct 降順でランキング
        scored.sort(key=lambda r: r["pred_return_pct"] or 0, reverse=True)
        for rank, row in enumerate(scored[:top_n], 1):
            row["rank"] = rank

        results = scored[:top_n]

        return {
            "cv_metrics":      cv_metrics,
            "feature_weights": feature_weights,
            "n_train_samples": total_samples,
            "n_companies":     len(current_snaps),
            "horizon_days":    horizon,
            "results":         results,
        }


plugin = PricePredictorPlugin()
