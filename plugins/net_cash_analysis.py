"""ネットキャッシュ分析プラグイン（清原達郎『わが投資術』式）

【指標定義】
  ネットキャッシュ = 流動資産 + 投資有価証券 × 0.7 − 総負債          [円]
  ネットキャッシュ比率 = ネットキャッシュ / 時価総額                   [無次元]
    （market_cap は百万円単位のため、内部で 1e6 倍して整える）

【清原氏の銘柄選別基準】
  nc_ratio > 1.0 : 時価総額より多くのネットキャッシュを保有 → 現金で会社を買える状態
  nc_ratio > 0.5 : 時価総額の半分以上をネットキャッシュで保有
  nc_ratio ≤ 0  : ネットキャッシュがマイナス（負債過多）

【投資有価証券に 0.7 をかける理由】
  時価評価のブレや売却時の含み益課税（実効税率 ≈ 30%）を考慮し、保守的に
  簿価の 70% でカウントする（清原氏の「わが投資術」の経験則）。

【次元整合性】
  net_cash[円] と market_cap[百万円] の単位差は 1e6 倍で吸収する。
  プラグイン出力では市場慣行に合わせ net_cash を「億円」表示する。
"""
from collections import defaultdict
from typing import Any

from sqlalchemy import func

from .base import AnalysisPlugin


# 清原氏の指針値（『わが投資術』）
NC_RATIO_VERY_CHEAP = 1.0   # 現金で全部買える
NC_RATIO_CHEAP       = 0.5  # 時価総額の半分以上をネットキャッシュ
INVESTMENT_DISCOUNT  = 0.7  # 投資有価証券の割引率（含み益課税考慮）


def compute_net_cash(current_assets: float | None,
                     investment_securities: float | None,
                     total_liabilities: float | None) -> float | None:
    """清原式ネットキャッシュ = 流動資産 + 投資有価証券×0.7 − 総負債 [円]

    流動資産と総負債のどちらも欠損していたら None。
    投資有価証券は欠損時 0 として扱う（古いレコードは未収集のため）。
    """
    if current_assets is None and total_liabilities is None:
        return None
    ca = float(current_assets or 0)
    inv = float(investment_securities or 0)
    tl = float(total_liabilities or 0)
    return ca + inv * INVESTMENT_DISCOUNT - tl


def compute_nc_ratio(net_cash: float | None, market_cap_mn: float | None) -> float | None:
    """ネットキャッシュ比率 = net_cash[円] / (market_cap[百万円] × 1e6)。"""
    if net_cash is None or market_cap_mn is None or market_cap_mn <= 0:
        return None
    return net_cash / (float(market_cap_mn) * 1_000_000)


class NetCashAnalysisPlugin(AnalysisPlugin):
    name = "net_cash_analysis"
    label = "ネットキャッシュ分析"
    description = (
        "清原達郎『わが投資術』のネットキャッシュ指標で割安銘柄をランキング。"
        "ネットキャッシュ = 流動資産 + 投資有価証券×0.7 − 総負債。"
        "比率 > 1.0 は時価総額より多くのネットキャッシュを保有（現金で買える）。"
    )
    depends_on = []

    def params_schema(self) -> dict:
        return {
            "min_nc_ratio": {
                "type": "number",
                "label": "最低ネットキャッシュ比率",
                "default": 0.5,
                "description": (
                    "0.5 = 時価総額の半分以上をネットキャッシュで保有、"
                    "1.0 = 現金で会社を買える状態（清原氏の重視水準）"
                ),
            },
            "min_market_cap": {
                "type": "number",
                "label": "最低時価総額（百万円）",
                "default": 5000,
                "optional": True,
                "description": "流動性確保のため小型株を除外する。0 で無効化。",
            },
            "industry": {
                "type": "text",
                "label": "業種フィルタ（空=全業種）",
                "default": None,
                "optional": True,
            },
            "year": {
                "type": "number",
                "label": "対象年度（空=各社の最新）",
                "default": None,
                "optional": True,
            },
            "top_n": {
                "type": "slider",
                "label": "表示件数",
                "min": 10, "max": 200, "step": 10,
                "default": 50,
            },
            "sort": {
                "type": "select",
                "label": "ソート順",
                "options": [
                    {"value": "nc_ratio_desc", "label": "ネットキャッシュ比率 高い順（割安）"},
                    {"value": "nc_ratio_asc",  "label": "ネットキャッシュ比率 低い順"},
                    {"value": "net_cash_desc", "label": "ネットキャッシュ額 大きい順"},
                ],
                "default": "nc_ratio_desc",
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        from database import FinancialRecord

        min_nc_ratio    = float(params.get("min_nc_ratio") or 0.0)
        min_market_cap  = params.get("min_market_cap")
        industry        = params.get("industry")
        year            = params.get("year")
        top_n           = int(params.get("top_n") or 50)
        sort            = params.get("sort") or "nc_ratio_desc"

        # 年度指定がなければ各社の最新年度のみを対象にする
        if year:
            query = db.query(FinancialRecord).filter(FinancialRecord.year == int(year))
        else:
            subq = (db.query(FinancialRecord.edinet_code,
                             func.max(FinancialRecord.year).label("max_year"))
                      .group_by(FinancialRecord.edinet_code).subquery())
            query = (db.query(FinancialRecord)
                       .join(subq,
                             (FinancialRecord.edinet_code == subq.c.edinet_code) &
                             (FinancialRecord.year == subq.c.max_year)))

        # ネットキャッシュ計算には流動資産 or 総負債が必要、比率には市場時価が必要
        query = query.filter(FinancialRecord.market_cap.isnot(None))
        query = query.filter(FinancialRecord.market_cap > 0)
        if industry:
            query = query.filter(FinancialRecord.industry == industry)
        if min_market_cap is not None:
            query = query.filter(FinancialRecord.market_cap >= float(min_market_cap))

        records = query.all()

        # 各レコードでネットキャッシュを実時間で再計算（DB の値が古い・NULL でもフォールバック）
        # nc_ratio が DB に書き込まれていない年度（市場データ未反映）にも対応する
        rows = []
        n_inv_securities = 0  # 投資有価証券が取れている銘柄数
        for r in records:
            nc = compute_net_cash(
                r.bs_current_assets, r.bs_investment_securities, r.bs_total_liabilities
            )
            if nc is None:
                continue
            ratio = compute_nc_ratio(nc, r.market_cap)
            if ratio is None:
                continue
            if ratio < min_nc_ratio:
                continue
            if r.bs_investment_securities and r.bs_investment_securities > 0:
                n_inv_securities += 1
            rows.append({
                "edinet_code":            r.edinet_code,
                "sec_code":               r.sec_code,
                "company_name":           r.company_name,
                "industry":               r.industry,
                "year":                   r.year,
                "net_cash_oku":           round(nc / 100_000_000, 2),    # 億円
                "nc_ratio":               round(ratio, 4),
                "market_cap_oku":         round(r.market_cap / 100, 2),  # 百万円→億円
                "current_assets_oku":     round((r.bs_current_assets or 0) / 100_000_000, 2),
                "investment_sec_oku":     round((r.bs_investment_securities or 0) / 100_000_000, 2),
                "total_liabilities_oku":  round((r.bs_total_liabilities or 0) / 100_000_000, 2),
                "per":                    r.per,
                "pbr":                    r.pbr,
                "div_yield":              r.div_yield,
                "roe":                    r.roe,
                "has_investment_sec":     bool(r.bs_investment_securities),
            })

        # ソート
        if sort == "nc_ratio_asc":
            rows.sort(key=lambda x: x["nc_ratio"])
        elif sort == "net_cash_desc":
            rows.sort(key=lambda x: x["net_cash_oku"], reverse=True)
        else:  # nc_ratio_desc
            rows.sort(key=lambda x: x["nc_ratio"], reverse=True)

        ranked = rows[:top_n]
        for i, r in enumerate(ranked, 1):
            r["rank"] = i

        # ボリューム集計（業種別 トップ N の傾向）
        industry_counts: dict[str, int] = defaultdict(int)
        for r in ranked:
            industry_counts[r["industry"] or "未分類"] += 1
        industry_summary = sorted(
            ({"industry": k, "n": v} for k, v in industry_counts.items()),
            key=lambda x: x["n"], reverse=True
        )

        # 清原基準（参考表示用）
        n_very_cheap = sum(1 for r in rows if r["nc_ratio"] >= NC_RATIO_VERY_CHEAP)
        n_cheap      = sum(1 for r in rows if r["nc_ratio"] >= NC_RATIO_CHEAP)

        return {
            "count":              len(ranked),
            "n_total_candidates": len(rows),
            "n_very_cheap":       n_very_cheap,   # nc_ratio >= 1.0
            "n_cheap":            n_cheap,        # nc_ratio >= 0.5
            "n_inv_securities":   n_inv_securities,  # 投資有価証券が取れている銘柄数
            "thresholds": {
                "very_cheap_nc_ratio": NC_RATIO_VERY_CHEAP,
                "cheap_nc_ratio":      NC_RATIO_CHEAP,
                "investment_discount": INVESTMENT_DISCOUNT,
            },
            "industry_summary": industry_summary,
            "results":          ranked,
        }


plugin = NetCashAnalysisPlugin()
