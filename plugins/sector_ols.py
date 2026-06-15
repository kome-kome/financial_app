"""
業種別OLS回帰分析プラグイン

全銘柄一括回帰ではなく業種ごとに個別OLSを実行することで
業種間の構造差（P/E・P/B水準の違い）を排除し、
業種内での相対的な割安・割高スコアリングを実現する。

次元整合性（CLAUDE.md制約・UI/APIレベルで強制）:
  target = stock_price [円/株] に固定。market_cap モードは削除済み。
  説明変数 = per-share [円/株] のみ。Ohlsonモデル拡張型。
    - DB永続 per-share: pl_eps / bs_bps / dps
    - 派生 per-share (ps_*): PL/BS/CFの絶対額を発行株数で割って実行時計算
    - 発行株数 = bs_total_equity / bs_bps（utils.shares_outstanding）
    - bs_bps が NULL/0 の銘柄は株数推計不能のため自動的に対象外

前処理: winsorize(p1-p99) → z-score正規化（業種内） → OLS / Ridge
"""
import math
from collections import defaultdict
from typing import Any

from .base import AnalysisPlugin
from .utils import (
    check_collinearity,
    normalize,
    ols,
    ols_with_diagnostics,
    ridge_regression,
    shares_outstanding,
    winsorize,
)


# 派生 per-share キー → 対応する絶対額カラム名 のマッピング
# 「pl_/bs_/cf_」プレフィックスの絶対額を発行株数で割って ps_* per-share 値を作る
PER_SHARE_DERIVED: dict[str, str] = {
    # PL（損益計算書）
    "ps_revenue":              "pl_revenue",
    "ps_cost_of_sales":        "pl_cost_of_sales",
    "ps_gross_profit":         "pl_gross_profit",
    "ps_sga":                  "pl_sga",
    "ps_rd_expenses":          "pl_rd_expenses",          # C2: 研究開発費（無形投資の代理変数）
    "ps_operating_profit":     "pl_operating_profit",
    "ps_depreciation":         "pl_depreciation",         # C2: 減価償却費（D&A・EBITDA 入力）
    "ps_nonoperating_income":  "pl_nonoperating_income",
    "ps_ordinary_profit":      "pl_ordinary_profit",
    "ps_extraordinary_income": "pl_extraordinary_income", # C2: 特別利益（JGAAP概念・IFRS/USは概ねnull）
    "ps_extraordinary_loss":   "pl_extraordinary_loss",   # C2: 特別損失（JGAAP概念・IFRS/USは概ねnull）
    "ps_pretax_profit":        "pl_pretax_profit",        # 税引前利益（標準項目だが従来未結線）
    "ps_net_income":           "pl_net_income",
    # BS — 資産
    "ps_total_assets":         "bs_total_assets",
    "ps_current_assets":       "bs_current_assets",
    "ps_receivables":          "bs_receivables",
    "ps_inventory":            "bs_inventory",
    "ps_cash":                 "bs_cash",
    "ps_noncurrent_assets":    "bs_noncurrent_assets",
    "ps_buildings":            "bs_buildings",
    "ps_machinery":            "bs_machinery",
    "ps_ppe_total":            "bs_ppe_total",            # C2: 有形固定資産合計（建物+機械の整合用）
    "ps_intangible_assets":    "bs_intangible_assets",
    "ps_investments_other_assets": "bs_investments_other_assets",  # C2: 投資その他の資産合計（JGAAP）
    "ps_investment_securities":"bs_investment_securities",
    # BS — 負債
    "ps_total_liabilities":    "bs_total_liabilities",
    "ps_current_liabilities":  "bs_current_liabilities",
    "ps_payables":             "bs_payables",
    "ps_noncurrent_liabilities":"bs_noncurrent_liabilities",
    "ps_short_term_debt":      "bs_short_term_debt",
    "ps_long_term_debt":       "bs_long_term_debt",
    "ps_bonds_payable":        "bs_bonds_payable",
    # BS — 純資産
    "ps_total_equity":         "bs_total_equity",
    "ps_paid_in_capital":      "bs_paid_in_capital",
    "ps_retained_earnings":    "bs_retained_earnings",
    # CF（キャッシュフロー）
    "ps_operating_cf":         "cf_operating_cf",
    "ps_investing_cf":         "cf_investing_cf",
    "ps_financing_cf":         "cf_financing_cf",
    "ps_free_cf":              "cf_free_cf",
    "ps_net_change_cash":      "cf_net_change_cash",
    "ps_capex":                "cf_capex",
}

# DB に直接保存されている per-share カラム（株数除算不要）
DB_PER_SHARE_KEYS: set[str] = {"pl_eps", "bs_bps", "dps"}

# per-share 特徴量 [円/株] — stock_price ターゲット向け（次元整合）
FEATURE_OPTIONS_PER_SHARE = [
    # DB永続 per-share（公式開示値）
    {"value": "pl_eps", "label": "[PL/株] EPS（円/株・公式）"},
    {"value": "bs_bps", "label": "[BS/株] BPS（円/株・公式）"},
    {"value": "dps",    "label": "[株主還元/株] DPS 1株配当（円/株・公式）"},
    # 派生 per-share — PL（純資産/BPSで株数推計して割り算）
    {"value": "ps_revenue",              "label": "[PL/株] 売上高（円/株）"},
    {"value": "ps_cost_of_sales",        "label": "[PL/株] 売上原価（円/株）"},
    {"value": "ps_gross_profit",         "label": "[PL/株] 売上総利益（円/株）"},
    {"value": "ps_sga",                  "label": "[PL/株] 販管費（円/株）"},
    {"value": "ps_rd_expenses",          "label": "[PL/株] 研究開発費（円/株・C2）"},
    {"value": "ps_operating_profit",     "label": "[PL/株] 営業利益（円/株）"},
    {"value": "ps_depreciation",         "label": "[PL/株] 減価償却費（円/株・C2）"},
    {"value": "ps_nonoperating_income",  "label": "[PL/株] 営業外損益（円/株）"},
    {"value": "ps_ordinary_profit",      "label": "[PL/株] 経常利益（円/株）"},
    {"value": "ps_extraordinary_income", "label": "[PL/株] 特別利益（円/株・C2・JGAAP）"},
    {"value": "ps_extraordinary_loss",   "label": "[PL/株] 特別損失（円/株・C2・JGAAP）"},
    {"value": "ps_pretax_profit",        "label": "[PL/株] 税引前利益（円/株）"},
    {"value": "ps_net_income",           "label": "[PL/株] 当期純利益（円/株）"},
    # 派生 per-share — BS資産
    {"value": "ps_total_assets",         "label": "[BS資産/株] 総資産（円/株）"},
    {"value": "ps_current_assets",       "label": "[BS資産/株] 流動資産（円/株）"},
    {"value": "ps_receivables",          "label": "[BS資産/株] 売掛金（円/株）"},
    {"value": "ps_inventory",            "label": "[BS資産/株] 棚卸資産（円/株）"},
    {"value": "ps_cash",                 "label": "[BS資産/株] 現金・預金（円/株）"},
    {"value": "ps_noncurrent_assets",    "label": "[BS資産/株] 固定資産（円/株）"},
    {"value": "ps_buildings",            "label": "[BS資産/株] 建物及び構築物（円/株）"},
    {"value": "ps_machinery",            "label": "[BS資産/株] 機械装置（円/株）"},
    {"value": "ps_ppe_total",            "label": "[BS資産/株] 有形固定資産合計（円/株・C2）"},
    {"value": "ps_intangible_assets",    "label": "[BS資産/株] 無形固定資産（円/株）"},
    {"value": "ps_investments_other_assets","label": "[BS資産/株] 投資その他の資産合計（円/株・C2）"},
    {"value": "ps_investment_securities","label": "[BS資産/株] 投資有価証券（円/株）"},
    # 派生 per-share — BS負債
    {"value": "ps_total_liabilities",    "label": "[BS負債/株] 総負債（円/株）"},
    {"value": "ps_current_liabilities",  "label": "[BS負債/株] 流動負債（円/株）"},
    {"value": "ps_payables",             "label": "[BS負債/株] 買掛金（円/株）"},
    {"value": "ps_noncurrent_liabilities","label": "[BS負債/株] 固定負債（円/株）"},
    {"value": "ps_short_term_debt",      "label": "[BS負債/株] 短期借入金（円/株）"},
    {"value": "ps_long_term_debt",       "label": "[BS負債/株] 長期借入金（円/株）"},
    {"value": "ps_bonds_payable",        "label": "[BS負債/株] 社債（円/株）"},
    # 派生 per-share — BS純資産
    {"value": "ps_total_equity",         "label": "[BS純資産/株] 純資産（円/株・BPS近似）"},
    {"value": "ps_paid_in_capital",      "label": "[BS純資産/株] 資本金（円/株）"},
    {"value": "ps_retained_earnings",    "label": "[BS純資産/株] 利益剰余金（円/株）"},
    # 派生 per-share — CF
    {"value": "ps_operating_cf",         "label": "[CF/株] 営業CF（円/株）"},
    {"value": "ps_investing_cf",         "label": "[CF/株] 投資CF（円/株）"},
    {"value": "ps_financing_cf",         "label": "[CF/株] 財務CF（円/株）"},
    {"value": "ps_free_cf",              "label": "[CF/株] フリーCF（円/株）"},
    {"value": "ps_net_change_cash",      "label": "[CF/株] 現金増減（円/株）"},
    {"value": "ps_capex",                "label": "[CF/株] 設備投資（円/株）"},
]

# 互換: 旧名称 FEATURE_OPTIONS は per-share 統一後の参照用に維持
FEATURE_OPTIONS = FEATURE_OPTIONS_PER_SHARE

# value → label の逆引き（自動ドロップ警告の表示用）
FEATURE_LABELS: dict[str, str] = {o["value"]: o["label"] for o in FEATURE_OPTIONS_PER_SHARE}

# 説明変数の欠損率がこの割合を超えたら実行時に自動ドロップする。
# _classify_by_sector は「選択列が1つでも NULL なら企業を丸ごと除外」する AND フィルタのため、
# 欠損の多い1項目（特別損益・社債・機械装置等）が全社を巻き込んで 0 業種に潰す事故が起きる。
# 母集団全体での欠損率がしきい値超の列を事前に間引くことでこれを防ぐ。
MAX_FEATURE_MISSING_RATE = 0.5

# stock_price ターゲット時のデフォルト10項目
# PL/BS/CF を網羅しつつ多重共線性を抑える主要指標
DEFAULT_FEATURES_PRICE = [
    "pl_eps",                # 収益力（DB永続）
    "bs_bps",                # 簿価（DB永続・Ohlson モデル中核）
    "dps",                   # 配当（DB永続）
    "ps_revenue",            # 売上トップライン
    "ps_gross_profit",       # 粗利・コスト構造
    "ps_operating_profit",   # 本業収益
    "ps_total_assets",       # 企業規模
    "ps_total_liabilities",  # 負債規模
    "ps_operating_cf",       # 実キャッシュ創出力
    "ps_free_cf",            # 株主還元原資
]


def _resolve_per_share_value(record, feat: str, shares: float) -> float | None:
    """説明変数キーから per-share[円/株] の値を取得する。

    `feat` の種別:
      - DB永続 per-share（pl_eps/bs_bps/dps）: そのまま getattr
      - 派生 per-share（ps_*）: 対応する絶対額カラム ÷ shares
      - 未知キー: None（呼び出し側で record スキップ）
    """
    if feat in DB_PER_SHARE_KEYS:
        v = getattr(record, feat, None)
        return float(v) if v is not None else None
    src = PER_SHARE_DERIVED.get(feat)
    if not src:
        return None
    src_val = getattr(record, src, None)
    if src_val is None or shares <= 0:
        return None
    return float(src_val) / shares


class SectorOLSPlugin(AnalysisPlugin):
    name = "sector_ols"
    label = "業種別OLS"
    description = (
        "業種ごとに個別OLS回帰を実行し、業種内の割安・割高スコアリングを行います。"
        "目的変数は株価[円/株]、説明変数は per-share[円/株] に固定（次元整合性を構造的に強制）。"
        "実行すると predicted_market_cap / gap_ratio が regression_results に書き込まれ、乖離分析タブに反映されます。"
    )
    depends_on = []
    heavy = True   # 業種ごとの行列回帰。Render Free では OOM するためローカル実行に限定
    category = "② 割安度を測る"
    ui_order = 210

    def produced_output(self, db) -> bool:
        """regression_results に gap_ratio 付きの予測値を書き終えているか。
        gap_analysis（depends_on=["sector_ols"]）の前提充足判定に使う。"""
        from database import RegressionResult
        return (
            db.query(RegressionResult.edinet_code)
              .filter(RegressionResult.gap_ratio.isnot(None))
              .first() is not None
        )

    def params_schema(self) -> dict:
        return {
            "target": {
                "type": "select",
                "label": "目的変数",
                "options": [
                    {"value": "stock_price", "label": "株価（円/株）— Ohlsonモデル型"},
                ],
                "default": "stock_price",
                "description": (
                    "次元整合性のため stock_price[円/株]に固定。"
                    "説明変数 [円/株] と被説明変数 [円/株] の単位を揃えることで"
                    "OLS 係数が経済的に解釈可能になる（β = implied 倍率）。"
                ),
            },
            "features": {
                "type": "multiselect",
                "label": "説明変数（per-share[円/株]）",
                "options": FEATURE_OPTIONS_PER_SHARE,
                "default": DEFAULT_FEATURES_PRICE,
                "description": (
                    "全項目 [円/株] per-share。派生 ps_* は実行時に "
                    "「絶対額 ÷ 発行株数」で計算（株数 = 純資産 ÷ BPS）。"
                    "bs_bps 欠損企業は株数推計不能のため自動的に対象外。"
                ),
            },
            "min_samples": {
                "type": "number",
                "dtype": "int",
                "label": "業種最低サンプル数",
                "default": 5,
                "min": 5,
            },
            "year": {
                "type": "number",
                "dtype": "int",
                "label": "対象年度（空=最新年度）",
                "default": None,
                "optional": True,
            },
            "regularization": {
                "type": "select",
                "label": "正則化（多重共線性対策）",
                "options": [
                    {"value": "none",  "label": "なし（OLS）"},
                    {"value": "ridge", "label": "Ridge（L2 正則化、α は CV で自動選択）"},
                ],
                "default": "none",
                "description": (
                    "VIF > 10 や |相関| > 0.9 の特徴量がある業種では Ridge を推奨。"
                    "per-share 10項目以上選択時は PL同士・BS同士の比例関係から"
                    "VIF>10 が頻発するため Ridge を強く推奨。"
                ),
            },
        }

    def _load_records(self, db, year: int | None) -> list:
        from database import FinancialRecord, latest_year_subq
        subq = latest_year_subq(db, FinancialRecord)
        query = (
            db.query(FinancialRecord)
            .join(subq, (FinancialRecord.edinet_code == subq.c.edinet_code) &
                        (FinancialRecord.year == subq.c.max_year))
        )
        if year:
            query = db.query(FinancialRecord).filter(FinancialRecord.year == year)
        records = query.all()
        if not records:
            raise ValueError("データがありません。先にデータ収集を実行してください。")
        return records

    def _eligible_base(self, records: list, target: str) -> list:
        """回帰の母集団を作る。(record, shares, y) のタプル列を返す。

        除外条件は 業種なし / target が None・非正 / 株数推計不能 の3つのみ。
        株数は issued_shares 優先（fill率100%）で bs_bps はフォールバック
        （utils.shares_outstanding）のため、bs_bps 欠損だけでは除外されない。
        """
        base = []
        for r in records:
            if not r.industry:
                continue
            y_val = getattr(r, target, None)
            if y_val is None or y_val <= 0:
                continue
            shares = shares_outstanding(r)
            if shares is None or shares <= 0:
                continue
            base.append((r, float(shares), float(y_val)))
        return base

    def _select_features(self, base: list, features: list,
                         min_samples: int) -> tuple[list, list]:
        """欠損の多い説明変数を自動ドロップし、採用列集合を返す（AND全滅の根本対策）。

        _classify_by_sector の AND フィルタは「選択列が1つでも NULL の企業を全除外」
        するため、欠損のある列を重ねるほど全列が揃う企業が積で減り、最終的に 0 業種へ
        潰れる。これを2段階で防ぐ:
          段階1: 母集団での欠損率が MAX_FEATURE_MISSING_RATE 超の列を一括ドロップ
                 （特別損益・社債等の極端に疎な列）。
          段階2: なお全業種が min_samples 未満なら、欠損最多の列から1つずつ貪欲に
                 ドロップし、いずれかの業種が min_samples に届くまで繰り返す
                 （中欠損列の AND 累積による全滅を解消）。
        (kept_features, dropped_info) を返す。dropped_info は警告表示用の
        [{feature,label,missing,missing_rate}, ...]。
        """
        n = len(base)
        if n == 0 or not features:
            return list(features), []

        # 各企業の「存在する選択列」集合と、列ごとの欠損数を前計算
        present: list[tuple[str, set]] = []
        miss_count: dict[str, int] = {f: 0 for f in features}
        for (r, sh, _y) in base:
            s = set()
            for f in features:
                if _resolve_per_share_value(r, f, sh) is not None:
                    s.add(f)
                else:
                    miss_count[f] += 1
            present.append((r.industry, s))

        kept = list(features)
        dropped: list[dict] = []

        def _drop(feat: str) -> None:
            kept.remove(feat)
            dropped.append({
                "feature":      feat,
                "label":        FEATURE_LABELS.get(feat, feat),
                "missing":      miss_count[feat],
                "missing_rate": round(miss_count[feat] / n * 100, 1),
            })

        def _max_sector_size() -> int:
            kset = set(kept)
            counts: dict[str, int] = defaultdict(int)
            for ind, s in present:
                if kset <= s:          # kept 列が全て present（=AND を通過）
                    counts[ind] += 1
            return max(counts.values(), default=0)

        # 段階1: 欠損率しきい値超を一括ドロップ
        for f in list(kept):
            if miss_count[f] / n > MAX_FEATURE_MISSING_RATE:
                _drop(f)

        # 段階2: どの業種も min_samples に届かない間、欠損最多列を貪欲ドロップ
        while kept and _max_sector_size() < min_samples:
            worst = max(kept, key=lambda f: miss_count[f])
            if miss_count[worst] == 0:
                break  # 残るは欠損ゼロ列のみ＝これ以上は純粋なサンプル不足
            _drop(worst)

        return kept, dropped

    def _classify_by_sector(self, base: list, features: list) -> dict:
        """母集団を業種ごとに振り分ける。features が全て揃う企業のみ採用（AND）。"""
        by_sector: dict[str, list] = defaultdict(list)
        for (r, shares, y_val) in base:
            row, ok = [], True
            for feat in features:
                v = _resolve_per_share_value(r, feat, shares)
                if v is None:
                    ok = False
                    break
                row.append(v)
            if ok:
                by_sector[r.industry].append((row, y_val, r))
        return by_sector

    def _preprocess_sector(self, samples: list, features: list) -> tuple:
        """winsorize + z-score 正規化。(X_norm, y_normed, y_mu, y_sd, X_win_cols, raw_y_win) を返す。"""
        raw_X = [s[0] for s in samples]
        raw_y = [s[1] for s in samples]
        X_win_cols = []
        for fi in range(len(features)):
            col_w, _, _ = winsorize([row[fi] for row in raw_X])
            X_win_cols.append(col_w)
        raw_X_win = [
            [X_win_cols[fi][ri] for fi in range(len(features))]
            for ri in range(len(samples))
        ]
        raw_y_win, _, _ = winsorize(raw_y)
        X_norm: list = []
        for fi in range(len(features)):
            normed, _, _ = normalize([row[fi] for row in raw_X_win], "zscore")
            for ri, v in enumerate(normed):
                if fi == 0:
                    X_norm.append([1.0, v])
                else:
                    X_norm[ri].append(v)
        y_normed, y_mu, y_sd = normalize(raw_y_win, "zscore")
        return X_norm, y_normed, y_mu, y_sd, X_win_cols, raw_y_win

    def _fit_and_predict(self, X_norm: list, y_normed: list,
                         y_mu: float, y_sd: float, regularization: str) -> tuple:
        """OLS/Ridge フィット + 逆正規化予測値。失敗時は (None, None)。"""
        result = ridge_regression(X_norm, y_normed) if regularization == "ridge" \
            else ols(X_norm, y_normed)
        if not result:
            return None, None
        beta = result["beta"]
        all_yhat_norm = [sum(x * b for x, b in zip(row, beta)) for row in X_norm]
        all_yhat = [v * y_sd + y_mu for v in all_yhat_norm]
        return result, all_yhat

    def _persist_and_rank(self, db, sector: str, samples: list,
                          all_yhat: list, regularization: str) -> list:
        """予測値・乖離率を DB に保存し、業種内ランク付きリストを返す。"""
        from database import upsert_regression_result
        sector_preds = []
        for i, (_, actual, r) in enumerate(samples):
            predicted = all_yhat[i]
            gap = round((predicted - actual) / actual * 100, 2) if actual else None
            predicted_mcap = (
                round(predicted / r.stock_price * r.market_cap, 0)
                if r.market_cap and r.stock_price and r.stock_price > 0 else None
            )
            upsert_regression_result(
                db,
                edinet_code=r.edinet_code, year=r.year, period_end=r.period_end,
                predicted_market_cap=predicted_mcap, gap_ratio=gap,
                model=("ridge" if regularization == "ridge" else "ols"),
                sector=sector,
            )
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
        sorted_idxs = sorted(range(len(sector_preds)),
                             key=lambda i: sector_preds[i]["gap_ratio"] or 0)
        for rank, idx in enumerate(sorted_idxs, 1):
            sector_preds[idx]["sector_rank"] = rank
        return sector_preds

    def _build_stat_entry(self, sector: str, samples: list, result: dict,
                          y_sd: float, X_norm: list, y_normed: list,
                          features: list, regularization: str,
                          X_win_cols: list) -> dict:
        """業種統計エントリ（診断統計・多重共線性チェック含む）を構築する。"""
        p_values = result.get("p_value", [])
        n_significant = (
            sum(1 for pv in p_values[1:] if not math.isnan(pv) and pv < 0.05)
            if regularization != "ridge" else None
        )
        collinearity = check_collinearity(X_win_cols, list(features))
        diag = None
        if regularization != "ridge":
            try:
                diag = ols_with_diagnostics(X_norm, y_normed, cov_type="HC3")
            except Exception:
                diag = None
        stat_entry = {
            "industry": sector,
            "n":        len(samples),
            "r2":       round(result["r2"], 4),
            "adj_r2":   round(result["adj_r2"], 4),
            "rmse":     round(result["rmse"] * y_sd, 2),
            "df":       result.get("df"),
            "rank":     result.get("rank"),
            "method":   result.get("method", "ols"),
            "alpha":    result.get("alpha"),
            "condition_number": (
                round(result["condition_number"], 2)
                if result.get("condition_number") is not None
                and math.isfinite(result.get("condition_number", float("inf")))
                else None
            ),
            "n_significant_features": n_significant,
            "p_values": [round(pv, 4) if not math.isnan(pv) else None for pv in p_values],
            "t_stats":  [round(t, 4) if t == t else None for t in result.get("t_stat", [])],
            "collinearity_warnings": {
                "high_corr_pairs": collinearity["high_corr_pairs"],
                "high_vif":        collinearity["high_vif"],
            },
        }
        if diag is not None:
            stat_entry["diagnostics"] = {
                "durbin_watson": round(diag["durbin_watson"], 3),
                "jarque_bera": (
                    {
                        "stat":     round(diag["jarque_bera"]["stat"], 3),
                        "pvalue":   round(diag["jarque_bera"]["pvalue"], 4),
                        "skew":     round(diag["jarque_bera"]["skew"], 3),
                        "kurtosis": round(diag["jarque_bera"]["kurtosis"], 3),
                    }
                    if diag.get("jarque_bera") is not None else None
                ),
                "f_stat":   round(diag["f_stat"], 3) if math.isfinite(diag.get("f_stat", float("nan"))) else None,
                "f_pvalue": round(diag["f_pvalue"], 6) if math.isfinite(diag.get("f_pvalue", float("nan"))) else None,
                "se_hc3":   [round(s, 4) if math.isfinite(s) else None for s in diag["se"]],
                "cov_type": diag["cov_type"],
            }
        return stat_entry

    async def execute(self, params: dict, db: Any) -> dict:
        target         = params["target"]
        features       = params["features"]
        min_samples    = params["min_samples"]
        regularization = params["regularization"]
        year           = params["year"]

        if not features:
            raise ValueError("説明変数を1つ以上選択してください")

        records = self._load_records(db, year)
        base    = self._eligible_base(records, target)
        # 欠損率が高い説明変数を自動ドロップ（1項目の NULL で全社除外される事故を防ぐ）。
        # 以降の features は採用列のみを指す。
        features, dropped_features = self._select_features(base, features, min_samples)
        if not features:
            raise ValueError(
                f"選択した説明変数はすべて欠損率が高く（>{int(MAX_FEATURE_MISSING_RATE * 100)}%）"
                "自動除外されました（"
                + "、".join(f"{d['label']}: NULL {d['missing_rate']}%" for d in dropped_features)
                + "）。EPS・BPS・売上高・総資産など欠損の少ない項目を選択してください。"
            )

        by_sector = self._classify_by_sector(base, features)

        sector_stats, all_predictions, n_skipped = [], [], 0

        for sector, samples in sorted(by_sector.items()):
            if len(samples) < min_samples:
                n_skipped += 1
                continue

            X_norm, y_normed, y_mu, y_sd, X_win_cols, _ = self._preprocess_sector(samples, features)
            result, all_yhat = self._fit_and_predict(X_norm, y_normed, y_mu, y_sd, regularization)
            if result is None:
                n_skipped += 1
                continue

            sector_preds = self._persist_and_rank(db, sector, samples, all_yhat, regularization)
            stat_entry   = self._build_stat_entry(
                sector, samples, result, y_sd, X_norm, y_normed, features, regularization, X_win_cols
            )
            all_predictions.extend(sector_preds)
            sector_stats.append(stat_entry)

        if not sector_stats:
            msg = (
                f"分析可能な業種がありません（各業種 {min_samples}社以上が必要）。"
                "min_samples を下げるか、説明変数を減らしてください。"
            )
            if dropped_features:
                msg += ("（欠損が多く自動除外: "
                        + "、".join(d["label"] for d in dropped_features) + "）")
            raise ValueError(msg)

        sector_stats.sort(key=lambda s: s["r2"], reverse=True)

        return {
            "n_sectors":         len(sector_stats),
            "n_total":           sum(s["n"] for s in sector_stats),
            "n_skipped_sectors": n_skipped,
            "features_used":     features,
            "dropped_features":  dropped_features,
            "sector_stats":      sector_stats,
            "results":           all_predictions,
        }


plugin = SectorOLSPlugin()
