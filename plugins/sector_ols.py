"""
業種別OLS回帰分析プラグイン

全銘柄一括回帰ではなく業種ごとに個別OLSを実行することで
業種間の構造差（P/E・P/B水準の違い）を排除し、
業種内での相対的な割安・割高スコアリングを実現する。

次元整合性（CLAUDE.md制約準拠）:
  target=market_cap [百万円]: 説明変数は絶対額 [円] のみ
    PL/BS/CF の絶対額は業種内でスケールが揃うため意味ある係数が得られる
  target=stock_price [円/株]: 説明変数は per-share [円/株] のみ
    Ohlsonモデル型。ratio特徴量は次元ミスマッチのため使用不可

前処理: winsorize(p1-p99) → z-score正規化（業種内） → OLS
"""
import math
from collections import defaultdict
from typing import Any

from .base import AnalysisPlugin
from .utils import normalize, ols, winsorize


# 絶対額特徴量 [円] — market_cap ターゲット向け
FEATURE_OPTIONS_ABS = [
    # PL（損益計算書）
    {"value": "pl_revenue",                "label": "[PL] 売上高"},
    {"value": "pl_cost_of_sales",          "label": "[PL] 売上原価"},
    {"value": "pl_gross_profit",           "label": "[PL] 売上総利益"},
    {"value": "pl_sga",                    "label": "[PL] 販売費及び一般管理費"},
    {"value": "pl_operating_profit",       "label": "[PL] 営業利益"},
    {"value": "pl_nonoperating_income",    "label": "[PL] 営業外損益（純額）"},
    {"value": "pl_net_income",             "label": "[PL] 当期純利益"},
    # BS — 資産
    {"value": "bs_total_assets",           "label": "[BS資産] 総資産"},
    {"value": "bs_current_assets",         "label": "[BS資産] 流動資産"},
    {"value": "bs_receivables",            "label": "[BS資産] 売掛金"},
    {"value": "bs_inventory",              "label": "[BS資産] 棚卸資産"},
    {"value": "bs_cash",                   "label": "[BS資産] 現金・預金"},
    {"value": "bs_noncurrent_assets",      "label": "[BS資産] 固定資産"},
    {"value": "bs_buildings",              "label": "[BS資産] 建物及び構築物"},
    {"value": "bs_machinery",              "label": "[BS資産] 機械装置"},
    {"value": "bs_intangible_assets",      "label": "[BS資産] 無形固定資産"},
    # BS — 負債
    {"value": "bs_total_liabilities",      "label": "[BS負債] 総負債"},
    {"value": "bs_current_liabilities",    "label": "[BS負債] 流動負債"},
    {"value": "bs_payables",               "label": "[BS負債] 買掛金"},
    {"value": "bs_short_term_debt",        "label": "[BS負債] 短期借入金"},
    {"value": "bs_noncurrent_liabilities", "label": "[BS負債] 固定負債"},
    {"value": "bs_long_term_debt",         "label": "[BS負債] 長期借入金"},
    {"value": "bs_bonds_payable",          "label": "[BS負債] 社債"},
    # BS — 純資産
    {"value": "bs_total_equity",           "label": "[BS純資産] 純資産"},
    {"value": "bs_paid_in_capital",        "label": "[BS純資産] 資本金"},
    {"value": "bs_retained_earnings",      "label": "[BS純資産] 利益剰余金"},
    # CF（キャッシュフロー）
    {"value": "cf_operating_cf",           "label": "[CF] 営業CF"},
    {"value": "cf_investing_cf",           "label": "[CF] 投資CF"},
    {"value": "cf_financing_cf",           "label": "[CF] 財務CF"},
]

# per-share特徴量 [円/株] — stock_price ターゲット向け
FEATURE_OPTIONS_PER_SHARE = [
    {"value": "pl_eps", "label": "[PL] EPS（円/株）"},
    {"value": "bs_bps", "label": "[BS] BPS（円/株）"},
    {"value": "dps",    "label": "DPS 1株配当（円/株）"},
]

FEATURE_OPTIONS = FEATURE_OPTIONS_ABS + FEATURE_OPTIONS_PER_SHARE

# market_cap ターゲット時のデフォルト（次元整合: 絶対額のみ）
DEFAULT_FEATURES_MKTCAP = [
    "pl_revenue", "pl_operating_profit", "pl_net_income",
    "bs_total_equity", "cf_operating_cf",
]

# stock_price ターゲット時のデフォルト（次元整合: per-share のみ・Ohlsonモデル型）
DEFAULT_FEATURES_PRICE = ["pl_eps", "bs_bps", "dps"]


class SectorOLSPlugin(AnalysisPlugin):
    name = "sector_ols"
    label = "業種別OLS"
    description = (
        "業種ごとに個別OLS回帰を実行し、業種内の割安・割高スコアリングを行います。"
        "実行すると predicted_market_cap / gap_ratio がDBに書き込まれ、乖離分析タブに反映されます。"
        "【次元整合】market_cap=絶対額[円]の説明変数のみ / stock_price=per-share[円/株]のみ"
    )
    depends_on = []

    def params_schema(self) -> dict:
        return {
            "target": {
                "type": "select",
                "label": "目的変数",
                "options": [
                    {"value": "market_cap",  "label": "時価総額（百万円）"},
                    {"value": "stock_price", "label": "株価（円/株）— Ohlsonモデル型"},
                ],
                "default": "market_cap",
            },
            "features": {
                "type": "multiselect",
                "label": "説明変数",
                "options": FEATURE_OPTIONS,
                "default": DEFAULT_FEATURES_MKTCAP,
                "description": (
                    "market_cap ターゲット時は [PL]/[BS]/[CF] 絶対額を選択。"
                    "stock_price ターゲット時は per-share 項目（EPS/BPS/DPS）のみ選択。"
                ),
            },
            "min_samples": {
                "type": "number",
                "label": "業種最低サンプル数",
                "default": 10,
            },
            "year": {
                "type": "number",
                "label": "対象年度（空=最新年度）",
                "default": None,
                "optional": True,
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        from sqlalchemy import func
        from database import FinancialRecord

        target      = params.get("target", "market_cap")
        features    = params.get("features") or DEFAULT_FEATURES_MKTCAP
        if isinstance(features, str):
            features = [f.strip() for f in features.split(",") if f.strip()]
        min_samples = max(5, int(params.get("min_samples") or 10))
        year        = params.get("year")

        if not features:
            raise ValueError("説明変数を1つ以上選択してください")

        subq = (
            db.query(FinancialRecord.edinet_code,
                     func.max(FinancialRecord.year).label("max_year"))
            .group_by(FinancialRecord.edinet_code)
            .subquery()
        )
        query = (
            db.query(FinancialRecord)
            .join(subq, (FinancialRecord.edinet_code == subq.c.edinet_code) &
                        (FinancialRecord.year == subq.c.max_year))
        )
        if year:
            query = db.query(FinancialRecord).filter(FinancialRecord.year == int(year))
        records = query.all()

        if not records:
            raise ValueError("データがありません。先にデータ収集を実行してください。")

        # 業種ごとにサンプルを分類
        by_sector: dict[str, list] = defaultdict(list)
        for r in records:
            if not r.industry:
                continue
            y_val = getattr(r, target, None)
            if y_val is None or y_val <= 0:
                continue
            row, ok = [], True
            for feat in features:
                v = getattr(r, feat, None)
                if v is None:
                    ok = False
                    break
                row.append(float(v))
            if ok:
                by_sector[r.industry].append((row, float(y_val), r))

        sector_stats = []
        all_predictions = []
        n_skipped = 0

        for sector, samples in sorted(by_sector.items()):
            if len(samples) < min_samples:
                n_skipped += 1
                continue

            raw_X = [s[0] for s in samples]
            raw_y = [s[1] for s in samples]

            # 外れ値処理（必須）: 特徴量・目的変数ともに winsorize(p1-p99)
            X_win_cols = []
            for fi in range(len(features)):
                col = [row[fi] for row in raw_X]
                col_w, _, _ = winsorize(col)
                X_win_cols.append(col_w)
            raw_X_win = [
                [X_win_cols[fi][ri] for fi in range(len(features))]
                for ri in range(len(samples))
            ]
            raw_y_win, _, _ = winsorize(raw_y)

            # z-score 正規化（業種内）
            X_norm = []
            for fi in range(len(features)):
                col = [row[fi] for row in raw_X_win]
                normed, _, _ = normalize(col, "zscore")
                for ri, v in enumerate(normed):
                    if fi == 0:
                        X_norm.append([1.0, v])
                    else:
                        X_norm[ri].append(v)
            y_normed, y_mu, y_sd = normalize(raw_y_win, "zscore")

            result = ols(X_norm, y_normed)
            if not result:
                n_skipped += 1
                continue

            beta = result["beta"]
            all_yhat_norm = [sum(x * b for x, b in zip(row, beta)) for row in X_norm]
            all_yhat = [v * y_sd + y_mu for v in all_yhat_norm]

            # 各レコードに予測値・乖離率を書き込み
            sector_preds = []
            for i, (_, actual, r) in enumerate(samples):
                predicted = all_yhat[i]
                gap = round((predicted - actual) / actual * 100, 2) if actual else None

                if target == "stock_price" and r.market_cap and r.stock_price and r.stock_price > 0:
                    # 次元整合モデル: 予測株価 → 時価総額に換算して保存
                    r.predicted_market_cap = round(predicted / r.stock_price * r.market_cap, 0)
                else:
                    r.predicted_market_cap = round(predicted, 0)
                r.gap_ratio = gap

                sector_preds.append({
                    "sec_code":     r.sec_code or r.edinet_code,
                    "company_name": r.company_name,
                    "industry":     sector,
                    "year":         r.year,
                    "actual":       round(actual, 0),
                    "predicted":    round(predicted, 0),
                    "gap_ratio":    gap,
                    "sector_rank":  None,
                    "sector_total": len(samples),
                })

            db.commit()

            # 業種内ランク付け（gap_ratio 低い順 = 割安が1位）
            sorted_preds = sorted(
                range(len(sector_preds)),
                key=lambda i: sector_preds[i]["gap_ratio"] or 0
            )
            for rank, idx in enumerate(sorted_preds, 1):
                sector_preds[idx]["sector_rank"] = rank

            all_predictions.extend(sector_preds)
            # 説明変数の有意性カウント（切片を除く、p < 0.05 を有意とみなす）
            p_values = result.get("p_value", [])
            n_significant = sum(
                1 for pv in p_values[1:] if pv == pv and pv < 0.05
            )
            sector_stats.append({
                "industry": sector,
                "n":        len(samples),
                "r2":       round(result["r2"], 4),
                "adj_r2":   round(result["adj_r2"], 4),
                "rmse":     round(result["rmse"] * y_sd, 2),
                "df":       result.get("df"),
                "n_significant_features": n_significant,
                "p_values": [round(pv, 4) if pv == pv else None for pv in p_values],
                "t_stats":  [round(t, 4) if t == t else None for t in result.get("t_stat", [])],
            })

        if not sector_stats:
            raise ValueError(
                f"分析可能な業種がありません（各業種 {min_samples}社以上が必要）。"
                "min_samples を下げるか、データを収集してください。"
            )

        sector_stats.sort(key=lambda s: s["r2"], reverse=True)

        return {
            "n_sectors":         len(sector_stats),
            "n_total":           sum(s["n"] for s in sector_stats),
            "n_skipped_sectors": n_skipped,
            "sector_stats":      sector_stats,
            "results":           all_predictions,
        }


plugin = SectorOLSPlugin()
