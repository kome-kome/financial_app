"""ネットキャッシュ分析プラグイン（清原達郎『わが投資術』式 + Graham NCAV）

【指標定義】
  ネットキャッシュ      = 流動資産 + 投資有価証券 × 0.7 − 総負債        [円]   ← 清原式
  ネットキャッシュ比率  = ネットキャッシュ / 時価総額                   [無次元]
  NCAV（純流動資産価値）= 流動資産 − 総負債                              [円]   ← Graham 1934
  NCAV比率              = NCAV / 時価総額                                [無次元]
    （market_cap は百万円単位のため、内部で 1e6 倍して整える）

【清原氏の銘柄選別基準（ネットキャッシュ比率）】
  nc_ratio ≥ 1.0 : 時価総額より多くのネットキャッシュを保有 → 現金で会社を買える状態
  nc_ratio ≥ 0.5 : 時価総額の半分以上をネットキャッシュで保有（半額バーゲン）
  nc_ratio ≤ 0  : ネットキャッシュがマイナス（負債過多）

【Graham のネットネット基準（NCAV比率）】
  ncav_ratio ≥ 1.5 : 時価総額 < NCAV × 2/3 → グレアムのネットネット安全域（2/3 ルール）
    （投資有価証券の 0.7 補正を行わない、より保守的なグレアムの原型）

【投資有価証券に 0.7 をかける理由】
  時価評価のブレや売却時の含み益課税（実効税率 ≈ 30%）を考慮し、保守的に
  簿価の 70% でカウントする（清原氏の「わが投資術」の経験則）。

【データ品質ガード（サニティ上限）】
  market_cap は実測ではなく推計株数（total_equity / bps）ベースの概算であり、
  推計が壊れた銘柄では時価総額がほぼ 0 になり比率が異常値（数十〜数万倍）になる。
  nc_ratio が sanity 上限を超える行は推計崩れとみなして除外する（既定 5.0、空/0 で無効）。
  これは「割安株を選ぶ基準」ではなく純粋にデータ品質のためのガードであり、
  かつてのような時価総額の一律下限（小型バーゲンまで巻き込んで除外する鈍器）を置かずに
  異常値だけをピンポイントで除ける。

【バリュートラップ除外（任意の健全性フィルタ）】
  割安でも現金を毀損し続ける企業は「万年割安」の罠になりやすい。
  営業CF>0 / 当期純利益>0 を任意で要求し、健全な割安株に絞れる（既定は無効）。

【次元整合性】
  net_cash / ncav は[円]、market_cap は[百万円]。単位差は 1e6 倍で吸収する。
  プラグイン出力では市場慣行に合わせ「億円」表示する。
"""
from collections import defaultdict
from typing import Any

from sqlalchemy import func

from .base import AnalysisPlugin


# 清原氏の指針値（『わが投資術』）
NC_RATIO_VERY_CHEAP = 1.0   # 現金で全部買える
NC_RATIO_CHEAP       = 0.5  # 時価総額の半分以上をネットキャッシュ
INVESTMENT_DISCOUNT  = 0.7  # 投資有価証券の割引率（含み益課税考慮）

# Graham のネットネット基準（2/3 ルール: price < 2/3 × NCAV ⟺ NCAV/price > 1.5）
NCAV_BARGAIN_RATIO   = 1.5

# データ品質ガード（推計時価総額の崩れによる異常比率を除外する既定の上限）
SANITY_MAX_NC_RATIO  = 5.0


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


def compute_ncav(current_assets: float | None,
                 total_liabilities: float | None) -> float | None:
    """Graham 流ネットネット（NCAV）= 流動資産 − 総負債 [円]。

    投資有価証券の 0.7 補正を行わない、より保守的なグレアムの原型（Graham 1934）。
    流動資産と総負債のどちらも欠損していたら None。
    """
    if current_assets is None and total_liabilities is None:
        return None
    return float(current_assets or 0) - float(total_liabilities or 0)


def compute_ncav_ratio(ncav: float | None, market_cap_mn: float | None) -> float | None:
    """NCAV比率 = ncav[円] / (market_cap[百万円] × 1e6)。"""
    if ncav is None or market_cap_mn is None or market_cap_mn <= 0:
        return None
    return ncav / (float(market_cap_mn) * 1_000_000)


def _num(v: Any) -> float | None:
    """空文字・None を None に正規化して float 化（UI からの空欄入力対応）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class NetCashAnalysisPlugin(AnalysisPlugin):
    name = "net_cash_analysis"
    label = "ネットキャッシュ分析"
    description = (
        "清原達郎『わが投資術』のネットキャッシュ指標＋グレアムのNCAVで割安銘柄をランキング。"
        "ネットキャッシュ = 流動資産 + 投資有価証券×0.7 − 総負債、NCAV = 流動資産 − 総負債。"
        "推計時価総額の崩れによる異常比率はサニティ上限で自動除外し、任意で営業CF>0等のバリュートラップ除外も行える。"
    )
    depends_on = []

    def params_schema(self) -> dict:
        return {
            "min_nc_ratio": {
                "type": "number",
                "label": "最低ネットキャッシュ比率（任意）",
                "default": 0.0,
                "optional": True,
                "description": (
                    "この値未満を除外（既定 0 = ネットキャッシュがプラスの全銘柄）。"
                    "0.5 = 半額バーゲン、1.0 = 現金で会社を買える水準（清原氏の重視水準）"
                ),
            },
            "max_nc_ratio": {
                "type": "number",
                "label": "NC比率の上限（データ品質ガード）",
                "default": SANITY_MAX_NC_RATIO,
                "optional": True,
                "description": (
                    "時価総額は推計株数ベースの概算のため、推計が壊れると比率が異常値になる。"
                    "この値を超える銘柄は推計崩れとみなして除外する（既定 5.0、空/0 で無効）"
                ),
            },
            "min_market_cap": {
                "type": "number",
                "label": "最低時価総額（百万円・任意）",
                "default": None,
                "optional": True,
                "description": "流動性確保のため任意で小型株を除外。空/0 で無効（既定）",
            },
            "min_ncav_ratio": {
                "type": "number",
                "label": "最低NCAV比率（任意・グレアム）",
                "default": None,
                "optional": True,
                "description": "1.5 でグレアムのネットネット（時価総額 < NCAV × 2/3）に限定",
            },
            "require_positive_ocf": {
                "type": "checkbox",
                "label": "営業CF>0 の企業のみ",
                "default": False,
                "optional": True,
                "description": "営業キャッシュフローがマイナスの企業を除外（現金毀損企業＝バリュートラップ回避）",
            },
            "require_positive_ni": {
                "type": "checkbox",
                "label": "純利益>0 の企業のみ",
                "default": False,
                "optional": True,
                "description": "当期純利益がマイナスの企業を除外（バリュートラップ回避）",
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
                    {"value": "nc_ratio_desc",   "label": "ネットキャッシュ比率 高い順（割安）"},
                    {"value": "nc_ratio_asc",    "label": "ネットキャッシュ比率 低い順"},
                    {"value": "net_cash_desc",   "label": "ネットキャッシュ額 大きい順"},
                    {"value": "ncav_ratio_desc", "label": "NCAV比率 高い順（グレアム割安）"},
                ],
                "default": "nc_ratio_desc",
            },
        }

    async def execute(self, params: dict, db: Any) -> dict:
        # net_cash / nc_ratio は本プラグインが自前計算（関数型）。roe のみ派生のため
        # 表示用に financial_metrics VIEW から読む。max_year 抽出は base テーブルで行う。
        from database import FinancialRecord, FinancialMetric

        min_nc_ratio   = _num(params.get("min_nc_ratio")) or 0.0
        max_nc_ratio   = _num(params.get("max_nc_ratio"))
        min_market_cap = _num(params.get("min_market_cap"))
        min_ncav_ratio = _num(params.get("min_ncav_ratio"))
        require_ocf    = bool(params.get("require_positive_ocf"))
        require_ni     = bool(params.get("require_positive_ni"))
        industry       = params.get("industry")
        year           = params.get("year")
        top_n          = int(params.get("top_n") or 50)
        sort           = params.get("sort") or "nc_ratio_desc"

        # サニティ上限は正の値のときのみ有効（空/0 で無効）
        sanity_cap = max_nc_ratio if (max_nc_ratio and max_nc_ratio > 0) else None

        # 年度指定がなければ各社の最新年度のみを対象にする
        if year:
            query = db.query(FinancialMetric).filter(FinancialMetric.year == int(year))
        else:
            subq = (db.query(FinancialRecord.edinet_code,
                             func.max(FinancialRecord.year).label("max_year"))
                      .group_by(FinancialRecord.edinet_code).subquery())
            query = (db.query(FinancialMetric)
                       .join(subq,
                             (FinancialMetric.edinet_code == subq.c.edinet_code) &
                             (FinancialMetric.year == subq.c.max_year)))

        # ネットキャッシュ計算には流動資産 or 総負債が必要、比率には市場時価が必要
        query = query.filter(FinancialMetric.market_cap.isnot(None))
        query = query.filter(FinancialMetric.market_cap > 0)
        if industry:
            query = query.filter(FinancialMetric.industry == industry)
        # 最低時価総額は任意。空/0 のときは絞らない（小型バーゲンを取りこぼさない）
        if min_market_cap is not None and min_market_cap > 0:
            query = query.filter(FinancialMetric.market_cap >= min_market_cap)

        records = query.all()

        # 各レコードでネットキャッシュ・NCAV を実時間で再計算（DB の値が古い・NULL でもフォールバック）
        rows = []
        n_inv_securities  = 0  # 投資有価証券が取れている銘柄数
        n_excluded_sanity = 0  # データ品質ガードで除外した件数（推計崩れの異常比率）
        n_excluded_trap   = 0  # バリュートラップ除外（営業CF/純利益）で落とした件数
        for r in records:
            nc = compute_net_cash(
                r.bs_current_assets, r.bs_investment_securities, r.bs_total_liabilities
            )
            if nc is None:
                continue
            ratio = compute_nc_ratio(nc, r.market_cap)
            if ratio is None:
                continue
            # データ品質ガード: 推計時価総額の崩れによる異常比率を除外
            if sanity_cap is not None and ratio > sanity_cap:
                n_excluded_sanity += 1
                continue
            if ratio < min_nc_ratio:
                continue
            ncav = compute_ncav(r.bs_current_assets, r.bs_total_liabilities)
            ncav_ratio = compute_ncav_ratio(ncav, r.market_cap)
            if min_ncav_ratio is not None and (ncav_ratio is None or ncav_ratio < min_ncav_ratio):
                continue
            # バリュートラップ除外（健全性フィルタ）。データ欠損（None）は判定不能として通す
            ocf = r.cf_operating_cf
            ni = r.pl_net_income_attr if r.pl_net_income_attr is not None else r.pl_net_income
            if require_ocf and ocf is not None and ocf <= 0:
                n_excluded_trap += 1
                continue
            if require_ni and ni is not None and ni <= 0:
                n_excluded_trap += 1
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
                "ncav_oku":               round(ncav / 100_000_000, 2) if ncav is not None else None,
                "ncav_ratio":             round(ncav_ratio, 4) if ncav_ratio is not None else None,
                "is_graham_netnet":       bool(ncav_ratio is not None and ncav_ratio >= NCAV_BARGAIN_RATIO),
                "market_cap_oku":         round(r.market_cap / 100, 2),  # 百万円→億円
                "current_assets_oku":     round((r.bs_current_assets or 0) / 100_000_000, 2),
                "investment_sec_oku":     round((r.bs_investment_securities or 0) / 100_000_000, 2),
                "total_liabilities_oku":  round((r.bs_total_liabilities or 0) / 100_000_000, 2),
                "operating_cf_oku":       round(ocf / 100_000_000, 2) if ocf is not None else None,
                "net_income_oku":         round(ni / 100_000_000, 2) if ni is not None else None,
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
        elif sort == "ncav_ratio_desc":
            rows.sort(
                key=lambda x: x["ncav_ratio"] if x["ncav_ratio"] is not None else float("-inf"),
                reverse=True,
            )
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

        # 各基準の該当数（候補プール全体に対する参考表示用）
        n_very_cheap    = sum(1 for r in rows if r["nc_ratio"] >= NC_RATIO_VERY_CHEAP)
        n_cheap         = sum(1 for r in rows if r["nc_ratio"] >= NC_RATIO_CHEAP)
        n_graham_netnet = sum(1 for r in rows if r["ncav_ratio"] is not None
                              and r["ncav_ratio"] >= NCAV_BARGAIN_RATIO)

        return {
            "count":              len(ranked),
            "n_total_candidates": len(rows),
            "n_very_cheap":       n_very_cheap,      # nc_ratio >= 1.0
            "n_cheap":            n_cheap,           # nc_ratio >= 0.5
            "n_graham_netnet":    n_graham_netnet,   # ncav_ratio >= 1.5（グレアム2/3ルール）
            "n_inv_securities":   n_inv_securities,  # 投資有価証券が取れている銘柄数
            "n_excluded_sanity":  n_excluded_sanity, # データ品質ガードで除外
            "n_excluded_trap":    n_excluded_trap,   # バリュートラップ除外で落とした件数
            "thresholds": {
                "very_cheap_nc_ratio": NC_RATIO_VERY_CHEAP,
                "cheap_nc_ratio":      NC_RATIO_CHEAP,
                "ncav_bargain_ratio":  NCAV_BARGAIN_RATIO,
                "investment_discount": INVESTMENT_DISCOUNT,
                "sanity_max_nc_ratio": sanity_cap,
            },
            "industry_summary": industry_summary,
            "results":          ranked,
        }


plugin = NetCashAnalysisPlugin()
