"""
総合リターン予測プラグイン

【次元設計】
  目的変数  : stock_price  [円/株]
  説明変数  : per-share 財務金額  [円/株]
    - PL 系  : pl_eps     (EPS = 当期純利益 ÷ 発行株数)
    - BS 系  : bs_bps     (BPS = 純資産     ÷ 発行株数)
    - CF 系  : cf_ops_ps  (営業CF ÷ 発行株数 ← 計算カラム)
    - 配当   : dps        (1株配当)

  OLS 係数の意味
    β_eps ≈ implied P/E 倍率
    β_bps ≈ implied P/B 倍率（簿価 1 円が株価何円に反映されるか）
    β_cf  ≈ implied Price/CF 倍率
    β_dps ≈ implied 配当割引倍率

  MECE 分類
    フロー (PL/CF) : pl_eps, cf_ops_ps
    ストック (BS)  : bs_bps
    配当           : dps
"""
import math
import statistics
from typing import Any
from .base import AnalysisPlugin
from .utils import normalize, normalize_transform, ols, kfold_cv, winsorize, LOG_PRED_CAP


# MECE グループ定義（すべて [円/株] の次元）
PL_FEATURES  = ["pl_eps"]        # PL フロー因子
CF_FEATURES  = ["cf_ops_ps"]     # CF フロー因子（計算値）
BS_FEATURES  = ["bs_bps"]        # BS ストック因子
DIV_FEATURES = ["dps"]           # 配当因子

ALL_MECE_FEATURES = PL_FEATURES + CF_FEATURES + BS_FEATURES + DIV_FEATURES

FEATURE_LABELS = {
    "pl_eps":    "EPS（1株当期純利益）",
    "cf_ops_ps": "1株営業CF",
    "bs_bps":    "BPS（1株純資産）",
    "dps":       "DPS（1株配当）",
}

# NULL を 0 で補完するフィールド（ゼロが経済的に自然）
NULLABLE_AS_ZERO = {"dps", "cf_ops_ps"}


class TotalReturnPlugin(AnalysisPlugin):
    name = "total_return"
    label = "総合リターン予測"
    description = (
        "1株当たり財務金額（EPS/BPS/CF/DPS）から理論株価を OLS 推定し、"
        "株価上昇余地＋配当利回りの期待総合リターンでランキング"
    )
    depends_on = []

    def params_schema(self) -> dict:
        return {
            "use_cf": {
                "type": "bool",
                "label": "CF因子を使用（1株営業CF）",
                "default": True,
            },
            "n_folds": {
                "type": "slider",
                "label": "CVフォールド数（k-fold）",
                "min": 3, "max": 10, "step": 1,
                "default": 5,
            },
            "top_n": {
                "type": "slider",
                "label": "表示件数",
                "min": 10, "max": 50, "step": 5,
                "default": 20,
            },
            "min_div_yield": {
                "type": "number",
                "label": "最低配当利回り（%、0=フィルタなし）",
                "default": 0.0,
                "optional": True,
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        from database import FinancialRecord

        use_cf        = params.get("use_cf", True)
        n_folds       = int(params.get("n_folds", 5))
        top_n         = int(params.get("top_n", 20))
        min_div_yield = float(params.get("min_div_yield") or 0.0)

        # 使用する特徴量（すべて [円/株]）
        features = list(PL_FEATURES)
        if use_cf:
            features += CF_FEATURES
        features += BS_FEATURES + DIV_FEATURES

        # stock_price と pl_eps と bs_bps が揃っているレコードを取得
        records = (db.query(FinancialRecord)
                     .filter(FinancialRecord.stock_price.isnot(None))
                     .filter(FinancialRecord.stock_price > 0)
                     .filter(FinancialRecord.pl_eps.isnot(None))
                     .filter(FinancialRecord.bs_bps.isnot(None))
                     .all())

        if len(records) < 20:
            raise ValueError(
                f"データが不足しています（{len(records)}件）。"
                "市場データ更新・財務データ収集を先に実行してください。"
            )

        def shares_outstanding(r) -> float | None:
            """発行済株式数を推計（純資産 ÷ BPS）。

            精度低下が起こる条件（CLAUDE.md 既知事項を補強）:
              - IFRS と JGAAP で「純資産」「BPS」の定義が微妙に異なる場合
                （IFRS は親会社株主帰属持分、JGAAP は連結純資産が分母）
              - 期中の増資・自己株消却で BPS と純資産の比が日次でずれている場合
              - 優先株・転換社債が存在し普通株数と乖離する場合
            根本対応案（FUTURE_TASKS.md）: J-Quants `/markets/listed/info` の
            IssuedShares フィールドから正規の発行済株式数を取得して直接利用する。
            """
            eq = r.bs_total_equity
            bps = r.bs_bps
            if eq and bps and bps > 0:
                return float(eq) / float(bps)
            return None

        def extract(r):
            """[円/株] の特徴量ベクトルと株価を返す"""
            row = []
            for feat in features:
                if feat == "cf_ops_ps":
                    # 1株営業CF = 営業CF ÷ 発行株数
                    shares = shares_outstanding(r)
                    cf = r.cf_operating_cf
                    if shares and shares > 0 and cf is not None:
                        v = float(cf) / shares
                    else:
                        v = 0.0  # NULL 許容
                else:
                    v = getattr(r, feat, None)
                    if v is None:
                        if feat in NULLABLE_AS_ZERO:
                            v = 0.0
                        else:
                            return None, None, None
                    v = float(v)
                row.append(v)

            sp = r.stock_price
            if sp is None or sp <= 0:
                return None, None, None
            return row, float(sp), r

        # 有効サンプル収集
        valid_records = []
        for r in records:
            row, sp, rec = extract(r)
            if row is not None:
                valid_records.append((row, sp, rec))

        if len(valid_records) < 20:
            raise ValueError(
                f"有効サンプルが不足しています（{len(valid_records)}件）。"
                "財務データ収集を先に実行してください。"
            )

        samples = [(vr[0], vr[1]) for vr in valid_records]
        n_feat = len(features)

        # ─── k-fold CV ───────────────────────────────────────────────────────
        # 株価も対数正規分布に近い（分布の裾が重い）ため log 正規化を使用
        cv_results = kfold_cv(samples, n_folds=n_folds, y_norm_method="log")

        # ─── 全サンプルで最終モデルを学習 ─────────────────────────────────────
        X_all_raw = [s[0] for s in samples]
        y_all_raw = [s[1] for s in samples]

        norm_params: list[tuple[float, float]] = []
        win_params:  list[tuple[float, float]] = []
        X_all_norm = [[1.0] + [0.0] * n_feat for _ in range(len(X_all_raw))]
        for fi in range(n_feat):
            col = [row[fi] for row in X_all_raw]
            col_w, w_lo, w_hi = winsorize(col)
            win_params.append((w_lo, w_hi))
            normed, p1, p2 = normalize(col_w, "zscore")
            norm_params.append((p1, p2))
            for ri, v in enumerate(normed):
                X_all_norm[ri][fi + 1] = v

        y_all, y_mu, y_sd = normalize(y_all_raw, "log")
        final_model = ols(X_all_norm, y_all)
        if not final_model:
            raise ValueError("最終モデルの学習に失敗しました（行列が特異）")
        beta = final_model["beta"]

        # ─── 全有効銘柄で予測・ランキング ─────────────────────────────────────
        ranking = []
        for feat_row, sp, r in valid_records:
            x_norm = [1.0]
            for fi, v in enumerate(feat_row):
                w_lo, w_hi = win_params[fi]
                v_w = max(w_lo, min(w_hi, v))
                p1, p2 = norm_params[fi]
                x_norm.append(normalize_transform(v_w, p1, p2))

            pred_norm  = sum(x_norm[j] * beta[j] for j in range(len(beta)))
            pred_price = math.exp(min(pred_norm * y_sd + y_mu, LOG_PRED_CAP))  # 円/株

            upside = (pred_price - sp) / sp

            # 予測乖離が異常に大きい場合はスキップ（外挿過多・データ異常）
            if upside > 5.0 or upside < -1.0:
                continue

            # 配当利回り（30%超はデータ異常）
            dy = float(r.div_yield or 0.0)
            if dy > 30.0:
                dy = 0.0

            if min_div_yield > 0 and dy < min_div_yield:
                continue

            total_return = upside + dy / 100.0
            name = getattr(r, "company_name", None) or r.edinet_code

            # implied 倍率を参考情報として付記
            eps = float(r.pl_eps) if r.pl_eps else None
            bps = float(r.bs_bps) if r.bs_bps else None
            implied_per = round(pred_price / eps, 1) if eps and eps > 0 else None
            implied_pbr = round(pred_price / bps, 2) if bps and bps > 0 else None

            ranking.append({
                "edinet_code":     r.edinet_code,
                "sec_code":        r.sec_code or "",
                "name":            name,
                "industry":        r.industry or "",
                "year":            r.year,
                "total_return_pct":  round(total_return * 100, 2),
                "upside_pct":        round(upside * 100, 2),
                "div_yield_pct":     round(dy, 2),
                "pred_price":        round(pred_price, 1),
                "actual_price":      round(sp, 1),
                "implied_per":       implied_per,
                "implied_pbr":       implied_pbr,
            })

        ranking.sort(key=lambda x: x["total_return_pct"], reverse=True)
        ranking = ranking[:top_n]
        for i, item in enumerate(ranking):
            item["rank"] = i + 1

        # 係数の解釈（implied 倍率として表示）
        feature_weights = {}
        for fi, feat in enumerate(features):
            b = beta[fi + 1]
            interp = ""
            if feat == "pl_eps":
                interp = "implied P/E 倍率の近似"
            elif feat == "bs_bps":
                interp = "implied P/B 倍率の近似"
            elif feat == "cf_ops_ps":
                interp = "implied Price/CF 倍率の近似"
            elif feat == "dps":
                interp = "implied 配当還元倍率の近似"
            feature_weights[feat] = {
                "weight": round(b, 6),
                "label":  FEATURE_LABELS.get(feat, feat),
                "group":  ("pl" if feat in PL_FEATURES
                           else "cf" if feat in CF_FEATURES
                           else "bs" if feat in BS_FEATURES
                           else "div"),
                "interpretation": interp,
            }

        mean_r2   = round(statistics.mean(f["r2"] for f in cv_results), 4) if cv_results else None
        mean_rmse = round(statistics.mean(f["rmse_pct"] for f in cv_results), 2) if cv_results else None

        return {
            "cv_metrics": {
                "folds":         cv_results,
                "mean_r2":       mean_r2,
                "mean_rmse_pct": mean_rmse,
                "n_samples":     len(samples),
                "cv_type":       "k-fold 横断的CV（銘柄間バイアス除去）",
                "model_note":    "目的変数=株価[円/株]、説明変数=EPS/BPS/CF/DPS[円/株]",
            },
            "feature_weights": feature_weights,
            "feature_groups": {
                "pl":  [f for f in features if f in PL_FEATURES],
                "cf":  [f for f in features if f in CF_FEATURES],
                "bs":  [f for f in features if f in BS_FEATURES],
                "div": [f for f in features if f in DIV_FEATURES],
            },
            "ranking":         ranking,
            "n_total_samples": len(samples),
        }


plugin = TotalReturnPlugin()
