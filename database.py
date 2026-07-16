"""
PostgreSQL スキーマ定義・ORM・upsert処理
テーブル構成:
  companies          — 企業マスタ（EDINETコード・証券コード・業種）
  financial_records  — BS/PL/CF 再分類済み年次財務データ
  stock_price_daily  — 日次株価
  stock_price_weekly — 週次株価
  collection_logs    — 収集ジョブログ
  macro_data         — マクロ経済指標
  xbrl_raw_documents — XBRL生データキャッシュ
  regression_results — OLS回帰結果キャッシュ
  app_settings       — アプリ設定（APP_PASSWORD 等）永続化
VIEW:
  financial_metrics  — 派生指標・Zスコア・成長率（financial_records から都度算出）
"""

import os, gzip, json, logging, contextvars
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean, DateTime, Date,
    Text, UniqueConstraint, PrimaryKeyConstraint, Index, JSON, LargeBinary, ForeignKey, text, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.dialects.postgresql import insert as pg_insert

load_dotenv()

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://edinet:edinet@localhost:5432/financial_db"
)
# Supabase/Heroku は "postgres://" を返すが SQLAlchemy 2.x は "postgresql://" が必要
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ローカル以外（クラウドDB）は SSL を強制し、コネクション数を抑える
_is_local = "localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL
_connect_args = {} if _is_local else {"sslmode": "require"}
_pool_size    = 10 if _is_local else 3
_max_overflow = 20 if _is_local else 5

engine = create_engine(
    DATABASE_URL,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_pre_ping=True,
    pool_recycle=180,
    connect_args=_connect_args,
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── 1. 企業マスタ ──────────────────────────────────────────────────────────

class Company(Base):
    __tablename__ = "companies"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    edinet_code  = Column(String(10), unique=True, nullable=False, index=True)
    sec_code     = Column(String(6),  index=True)          # 証券コード4桁
    name         = Column(String(200), nullable=False)
    name_en      = Column(String(200))
    industry     = Column(String(100))                     # 業種（EDINET分類）
    market       = Column(String(50))                      # プライム/スタンダード/グロース
    fiscal_month = Column(Integer)                         # 決算月
    accounting_standard = Column(String(20))               # JGAAP/IFRS/US-GAAP
    issued_shares = Column(Float, nullable=True)           # 発行済株式数（J-Quants 取得・最新値）
    is_active     = Column(Boolean, nullable=False, default=True)  # 上場中フラグ（J-Quants listed/info突合で自動更新。Issue #315）
    delisted_date = Column(Date, nullable=True)             # is_active=False へ遷移した日（再上場等で復帰した場合はNoneへ戻す）
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    records = relationship("FinancialRecord", back_populates="company",
                           cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Company {self.sec_code} {self.name}>"


# ── 2. 財務レコード（BS/PL/CF 再分類済み） ────────────────────────────────

class FinancialRecord(Base):
    __tablename__ = "financial_records"
    __table_args__ = (
        # 開示粒度(period_type)を含む複合一意（Issue #219② フェーズB）。半期(H1)行は通期と
        # period_end が異なるため旧3列制約でも衝突しないが、訂正再開示や同一 period_end の
        # 別粒度を厳密に区別するため period_type を制約に含める。名称も _pt を付けて改称。
        UniqueConstraint("edinet_code", "year", "period_end", "period_type",
                         name="uq_edinet_year_period_pt"),
        Index("ix_sec_year", "sec_code", "year"),
        Index("ix_industry_year", "industry", "year"),
        Index("ix_period_type", "period_type"),   # financial_metrics VIEW の annual フィルタ用
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    edinet_code  = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    sec_code     = Column(String(6))
    company_name = Column(String(200))
    industry     = Column(String(100))
    market       = Column(String(50))
    year         = Column(Integer, nullable=False)
    period_end   = Column(Date, nullable=True)              # 決算期末日
    doc_id       = Column(String(20))                      # EDINET書類管理番号
    source       = Column(String(50), default="EDINET_XBRL")
    accounting_standard = Column(String(20))
    # ── 開示粒度（Issue #219② フェーズA）─────────────────────────────────────
    # period_type: annual/H1/Q1/Q2/Q3。既存行・通期収集は既定 'annual'。半期報告書(H1)等の
    # 非通期行を同一テーブルに同居させるための識別列。financial_metrics VIEW は
    # WHERE period_type='annual' で通期のみを露出し既存プラグインを完全不変に保つ（年度単位の
    # Zスコア/成長率 WINDOW が期間混在で壊れるのを防ぐ）。非通期行は period_type<>'annual' で参照。
    # ※フェーズB（半期収集）で UNIQUE 制約に period_type を追加する（現状は非通期行ゼロで衝突なし）。
    period_type  = Column(String(10), nullable=False, default="annual")
    filing_date  = Column(Date, nullable=True)             # 提出日（point-in-time 基準・リーク防止用）

    # ── BS（貸借対照表）再分類項目。info["xbrl"] = この列へ集約する生タグ群（多対一） ──
    bs_total_assets         = Column(Float, info={"xbrl": ["Assets", "AssetsIFRS", "TotalAssetsUSGAAPSummaryOfBusinessResults"]})  # 総資産
    bs_current_assets       = Column(Float, info={"xbrl": ["CurrentAssets", "CurrentAssetsIFRS"]})  # 流動資産
    bs_receivables          = Column(Float, info={"xbrl": ["NotesAndAccountsReceivableTrade", "AccountsReceivableTrade", "TradeAndOtherReceivablesCurrentIFRS"]})  # 売掛金（売上債権）
    bs_inventory            = Column(Float, info={"xbrl": ["Inventories", "InventoriesIFRS"]})  # 棚卸資産
    bs_noncurrent_assets    = Column(Float, info={"xbrl": ["NoncurrentAssets", "NoncurrentAssetsIFRS"]})  # 固定資産
    # 建物及び構築物（純額のみ）。BuildingsAndStructures（Net無し）は取得原価=グロスで bs_ppe_total（純額）を超え
    # balance invariant を壊すため除外。代替綴り BuildingsNet・IFRS 純額 BuildingsAndStructuresIFRS を採用。
    bs_buildings            = Column(Float, info={"xbrl": ["BuildingsAndStructuresNet", "BuildingsNet", "BuildingsAndStructuresIFRS"]})  # 建物及び構築物（純額）
    # 機械装置（純額のみ）。MachineryAndEquipment/MachineryAndVehicles（Net無し）はグロスのため除外。
    # MachineryEquipmentAndVehiclesNet は「機械装置及び運搬具（純額）」の別名タグ（実XBRL診断で確認済み）。
    bs_machinery            = Column(Float, info={"xbrl": ["MachineryAndEquipmentNet", "MachineryEquipmentAndVehiclesNet"]})  # 機械装置及び運搬具（純額）
    bs_ppe_total            = Column(Float, info={"xbrl": ["PropertyPlantAndEquipment", "PropertyPlantAndEquipmentIFRS"]})  # 有形固定資産合計（内訳=建物+機械等の整合用。C2）
    bs_intangible_assets    = Column(Float, info={"xbrl": ["IntangibleAssets", "IntangibleAssetsIFRS", "GoodwillAndIntangibleAssetsIFRS"]})  # 無形固定資産
    bs_investments_other_assets = Column(Float, info={"xbrl": ["InvestmentsAndOtherAssets"]})  # 投資その他の資産合計（JGAAP固定資産構造。C2）
    bs_cash                 = Column(Float, info={"xbrl": ["CashAndCashEquivalents", "CashAndCashEquivalentsIFRS", "CashAndCashEquivalentsUSGAAPSummaryOfBusinessResults"]})  # 現金・預金
    # 投資有価証券（清原式ネットキャッシュ用）。IFRS は非流動その他金融資産で近似（流動性の高い金融資産は別科目のため除外）
    bs_investment_securities = Column(Float, info={"xbrl": ["InvestmentSecurities", "InvestmentsInSecurities", "ShortTermInvestmentSecurities", "OtherFinancialAssetsNonCurrentIFRS"]})  # 投資有価証券
    bs_total_liabilities    = Column(Float, info={"xbrl": ["Liabilities", "LiabilitiesIFRS"]})  # 総負債
    bs_current_liabilities  = Column(Float, info={"xbrl": ["CurrentLiabilities", "CurrentLiabilitiesIFRS"]})  # 流動負債
    bs_payables             = Column(Float, info={"xbrl": ["NotesAndAccountsPayableTrade", "AccountsPayableTrade", "TradeAndOtherPayablesCurrentIFRS"]})  # 買掛金（仕入債務）
    bs_noncurrent_liabilities = Column(Float, info={"xbrl": ["NoncurrentLiabilities", "NoncurrentLiabilitiesIFRS"]})  # 固定負債
    bs_short_term_debt      = Column(Float, info={"xbrl": ["ShortTermLoansPayable"]})  # 短期借入金
    bs_long_term_debt       = Column(Float, info={"xbrl": ["LongTermLoansPayable"]})  # 長期借入金
    bs_bonds_payable        = Column(Float, info={"xbrl": ["BondsPayable"]})  # 社債
    # 純資産（連結）。US-GAAP は「株主資本」「純資産額(NCI含む)」のどちらか一方のみ載る企業があり両方登録（同優先度では先勝ち）
    bs_total_equity         = Column(Float, info={"xbrl": ["Equity", "NetAssets", "EquityIFRS", "EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults", "EquityIncludingPortionAttributableToNonControllingInterestUSGAAPSummaryOfBusinessResults"]})  # 純資産（連結）
    bs_equity_parent        = Column(Float, info={"xbrl": ["EquityAttributableToOwnersOfParent", "EquityAttributableToOwnersOfParentIFRS"]})  # 親会社株主帰属持分（IFRS）
    bs_paid_in_capital      = Column(Float, info={"xbrl": ["CapitalStock", "IssuedCapitalIFRS"]})  # 資本金
    bs_retained_earnings    = Column(Float, info={"xbrl": ["RetainedEarnings", "RetainedEarningsIFRS"]})  # 利益剰余金
    bs_bps                  = Column(Float, info={"xbrl": ["BookValuePerShare", "NetAssetsPerShareSummaryOfBusinessResults", "EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults"]})  # 1株純資産

    # ── PL（損益計算書）再分類項目 ──────────────────────────────────────
    # 売上高。生 OperatingRevenue1（PL本体）は登録しない: 金融持株会社が単体営業収益を誤採用するため
    # Summary 変種のみ採用。NetSalesIFRS はソニー等が Revenue でなく NetSales を使う IFRS 企業対策。
    pl_revenue              = Column(Float, info={"xbrl": [
        "NetSales", "Revenues", "NetRevenues", "OperatingRevenues", "Revenue",
        "OperatingRevenue1SummaryOfBusinessResults",
        "RevenueIFRS", "RevenueIFRSSummaryOfBusinessResults",
        "NetSalesIFRS", "NetSalesIFRSSummaryOfBusinessResults",
        "RevenuesUSGAAPSummaryOfBusinessResults",
    ]})  # 売上高
    pl_cost_of_sales        = Column(Float, info={"xbrl": ["CostOfSales", "CostOfSalesIFRS"]})  # 売上原価
    pl_gross_profit         = Column(Float, info={"xbrl": ["GrossProfit", "GrossProfitIFRS"]})  # 売上総利益
    pl_sga                  = Column(Float, info={"xbrl": ["SellingGeneralAndAdministrativeExpenses"]})  # 販売費及び一般管理費
    pl_operating_profit     = Column(Float, info={"xbrl": ["OperatingIncome", "OperatingProfit", "ProfitFromOperatingActivities", "OperatingProfitLossIFRS", "ProfitFromOperatingActivitiesIFRS"]})  # 営業利益
    pl_nonoperating_income  = Column(Float)   # 営業外損益（純額）= 経常利益 - 営業利益（派生列・tagなし）
    pl_ordinary_profit      = Column(Float, info={"xbrl": ["OrdinaryIncome"]})  # 経常利益
    pl_pretax_profit        = Column(Float, info={"xbrl": ["IncomeBeforeIncomeTaxes", "ProfitLossBeforeTaxIFRS", "ProfitLossBeforeTaxIFRSSummaryOfBusinessResults", "ProfitLossBeforeTaxUSGAAPSummaryOfBusinessResults", "ProfitLossBeforeIncomeTaxes"]})  # 税前利益（JGAAP=IncomeBeforeIncomeTaxes / IFRS=ProfitLossBeforeTaxIFRS。旧ProfitLossBeforeIncomeTaxesは誤りだが互換で末尾保持）
    pl_net_income           = Column(Float, info={"xbrl": ["NetIncomeLoss", "ProfitLoss", "ProfitLossIFRS", "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults"]})  # 当期純利益
    pl_net_income_attr      = Column(Float, info={"xbrl": ["ProfitLossAttributableToOwnersOfParent", "ProfitLossAttributableToOwnersOfParentIFRS"]})  # 親会社帰属純利益（IFRS）
    pl_eps                  = Column(Float, info={"xbrl": ["EarningsPerShare", "BasicEarningsLossPerShare", "BasicEarningsLossPerShareSummaryOfBusinessResults", "BasicEarningsLossPerShareIFRS", "EarningsPerShareIFRS", "BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults"]})  # EPS（円）
    pl_ebitda               = Column(Float)   # EBITDA（計算値=営業利益+減価償却費・派生列・tagなし）
    # ── PL 網羅性追加（C2）──
    pl_rd_expenses          = Column(Float, info={"xbrl": ["ResearchAndDevelopmentExpensesResearchAndDevelopmentActivities", "ResearchAndDevelopmentExpensesSGA"]})  # 研究開発費
    pl_depreciation         = Column(Float, info={"xbrl": ["DepreciationAndAmortizationOpeCF", "DepreciationAndAmortizationOpeCFIFRS"]})  # 減価償却費及び償却費（D&A・CF add-back。EBITDA入力）
    pl_extraordinary_income = Column(Float, info={"xbrl": ["ExtraordinaryIncome"]})  # 特別利益（JGAAP概念。IFRS/US-GAAP連結は概ねnull）
    pl_extraordinary_loss   = Column(Float, info={"xbrl": ["ExtraordinaryLoss"]})  # 特別損失（JGAAP概念。IFRS/US-GAAP連結は概ねnull）

    # ── CF（キャッシュフロー）再分類項目 ────────────────────────────────
    # CF 合計: JGAAP=CashFlowsFrom…系、IFRS/共通=NetCashProvidedByUsedIn…系、IFRS/US-GAAP 経営指標等=…SummaryOfBusinessResults。
    # IFRS/US-GAAP は本体CF計算書が独自拡張要素のため、経営指標等セクションが確実な取得源（トヨタ等268社対策）。
    cf_operating_cf         = Column(Float, info={"xbrl": ["NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesIFRS", "CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults", "CashFlowsFromUsedInOperatingActivitiesIFRS", "CashFlowsFromUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults"]})  # 営業CF
    cf_investing_cf         = Column(Float, info={"xbrl": ["NetCashProvidedByUsedInInvestmentActivities", "NetCashProvidedByUsedInInvestingActivities", "CashFlowsFromInvestingActivities", "NetCashProvidedByUsedInInvestingActivitiesIFRS", "CashFlowsFromUsedInInvestingActivitiesIFRSSummaryOfBusinessResults", "CashFlowsFromUsedInInvestmentActivitiesIFRS", "CashFlowsFromUsedInInvestingActivitiesIFRS", "CashFlowsFromUsedInInvestingActivitiesUSGAAPSummaryOfBusinessResults"]})  # 投資CF
    cf_financing_cf         = Column(Float, info={"xbrl": ["NetCashProvidedByUsedInFinancingActivities", "CashFlowsFromFinancingActivities", "NetCashProvidedByUsedInFinancingActivitiesIFRS", "CashFlowsFromUsedInFinancingActivitiesIFRSSummaryOfBusinessResults", "CashFlowsFromUsedInFinancingActivitiesIFRS", "CashFlowsFromUsedInFinancingActivitiesUSGAAPSummaryOfBusinessResults"]})  # 財務CF
    cf_free_cf              = Column(Float)   # フリーCF（計算値・派生列・tagなし）
    cf_net_change_cash      = Column(Float, info={"xbrl": ["NetIncreaseDecreaseInCashAndCashEquivalents", "CashAndCashEquivalentsIncreaseDecrease", "CashAndCashEquivalentsPeriodIncreaseDecrease", "NetIncreaseDecreaseInCashAndCashEquivalentsIFRS"]})  # 現金増減
    # 設備投資。要素ID照合に加え _match_capex_by_label のラベル照合でも捕捉（企業独自の拡張要素対策）
    cf_capex                = Column(Float, info={"xbrl": ["PurchaseOfPropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets", "PurchaseOfPropertyPlantAndEquipmentInvestmentCF", "PaymentsForPurchaseOfPropertyPlantAndEquipment", "CapitalExpendituresForTangibleAssets", "PurchaseOfPropertyPlantAndEquipmentIFRS", "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsIFRS"]})  # 設備投資額

    # ── 市場データ（株価・バリュエーション・収集時点スナップショット）────
    stock_price             = Column(Float)   # 株価（収集時点）
    market_cap              = Column(Float)   # 時価総額（百万円）
    per                     = Column(Float)   # PER
    pbr                     = Column(Float)   # PBR
    div_yield               = Column(Float)   # 配当利回り %
    # 1株配当。section=val: 接頭辞なしで直接列にマップ（build_xbrl_map が列名から section を判定できないため明示）
    dps                     = Column(Float, info={"xbrl": ["DividendPaidPerShare", "DividendPaidPerShareSummaryOfBusinessResults"], "section": "val"})  # 1株配当

    # ── 非財務（C2・nonfin セクション経由で直接列にマップ）。section=nonfin を明示 ────────
    employees               = Column(Float, info={"xbrl": ["NumberOfEmployees"], "section": "nonfin"})  # 従業員数（連結・整数値をFloat格納）
    issued_shares           = Column(Float, info={"xbrl": ["NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc", "TotalNumberOfIssuedSharesSummaryOfBusinessResults"], "section": "nonfin"})  # 期末発行済株式総数（表示・参考。OLS分母はshares_outstanding維持）

    # 計算結果（派生比率・Zスコア・成長率・OLS予測値）は financial_records には保持しない。
    #   - 軽い派生／Zスコア／成長率 → financial_metrics VIEW（ソース列から都度算出）
    #   - OLS予測値（predicted_market_cap / gap_ratio）→ regression_results テーブル
    # 旧計算列は本コミットで DROP 済み（init_db の DROP マイグレーション参照）。
    # raw_xbrl_json（デバッグ用に保存していた parse 済み bs/pl/cf dict）も Issue #219 ①で
    # DROP 済み（読取箇所ゼロ・生タグを保持せず reparse 用途にも使えなかったため。GOTCHAS.md参照）。

    created_at              = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at              = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="records")


# ── 再分類項目レジストリ: FinancialRecord の列定義を唯一の源とした射影 ──────────────
# XBRL_MAP（生タグ → (section, field)）は手書きせず、各列の info["xbrl"] から逆引き生成する。
# 「源泉タグ付き列」= info["xbrl"] を持つ列（parse 対象）／「派生列」= 持たない列（calc_derived・市場データ）。
# section は bs_/pl_/cf_ 接頭辞から推論し、接頭辞なし列（val/nonfin）は info["section"] で明示する。

def _column_target(col) -> tuple[str, str]:
    """列から (section, field) を決める。pl_revenue→("pl","revenue")、dps→("val","dps")。"""
    section = col.info.get("section")
    if section:                       # 接頭辞なし列（val/nonfin）は section 明示・field=列名
        return section, col.name
    section, _, field = col.name.partition("_")   # bs_/pl_/cf_ 接頭辞から分解
    return section, field


def build_xbrl_map() -> dict[str, tuple[str, str]]:
    """FinancialRecord の各列 info["xbrl"] を逆引きし、生タグ → (section, field) を生成する。
    同一の生タグが2列に現れたら ValueError（多対一の一意性違反を import 時に検出）。"""
    mapping: dict[str, tuple[str, str]] = {}
    for col in FinancialRecord.__table__.columns:
        for tag in col.info.get("xbrl", ()):
            if tag in mapping:
                raise ValueError(
                    f"XBRL 生タグ '{tag}' が複数列に重複登録: {mapping[tag]} と {_column_target(col)}"
                )
            mapping[tag] = _column_target(col)
    return mapping


# upsert_financial の未知キー検出に使う、書込み可能な (section, field) の集合。
VALID_TARGETS: frozenset[tuple[str, str]] = frozenset(build_xbrl_map().values())


# ── 3. 株価履歴（2本立て: 直近=日次 / 全履歴=週次。close-only・source-only）────────
# Supabase Free 500MB 制約の恒久対策。旧 stock_price_history（日次OHLCV全履歴・359MB）を
#   - StockPriceDaily : 直近 DAILY_WINDOW_DAYS の日次終値（チャート日次ズーム・短期バックテスト）
#   - StockPriceWeekly: 全履歴の週次集約（チャート全期間・長期バックテスト・将来モデル）
# に分離。OHLC のうち close のみ保持（チャートは終値ライン）。VWAP・相対流動性は
# turnover_sum/volume_sum から「派生」（保存しない＝financial_metrics VIEW と同じ流儀）。

DAILY_WINDOW_DAYS = 183   # daily の保持窓（約6か月）。weekly が全履歴を持つため自由に変更可・移行不要


class StockPriceDaily(Base):
    """直近 DAILY_WINDOW_DAYS 日の日次終値。ローリング削除で一定サイズに保つ。"""
    __tablename__ = "stock_price_daily"
    __table_args__ = (
        PrimaryKeyConstraint("edinet_code", "trade_date", name="pk_stock_price_daily"),
        Index("ix_spd_trade_date", "trade_date"),   # 全社横断 trim（trade_date < cutoff）用
    )

    edinet_code = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    trade_date  = Column(String(10), nullable=False)   # "YYYY-MM-DD"
    close       = Column(Float, nullable=False)
    volume      = Column(Float)                         # VWAP 算出用（週次集約時に消費）


class StockPriceWeekly(Base):
    """全履歴の週次集約（追記専用・trim しない）。1 ISO週 = 1 レコード。source-only。"""
    __tablename__ = "stock_price_weekly"
    __table_args__ = (
        PrimaryKeyConstraint("edinet_code", "week_start", name="pk_stock_price_weekly"),
    )

    edinet_code  = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    week_start   = Column(String(10), nullable=False)   # ISO週の月曜 "YYYY-MM-DD"
    trade_date   = Column(String(10))                   # 週内最終営業日の実日付
    close_last   = Column(Float, nullable=False)        # 最終営業日終値（実約定・チャート/バックテスト）
    volume_sum   = Column(Float)                         # 週内出来高合計（VWAP分母）。volume欠落週は None
    turnover_sum = Column(Float)                         # 週内売買代金合計 Σ(close*vol)（VWAP分子・流動性変量）
    n_days       = Column(Integer)                       # 週内に集約した営業日数（祝日週の信頼度判定）


def iso_week_start(trade_date: str) -> str:
    """'YYYY-MM-DD' → その ISO 週の月曜日 'YYYY-MM-DD'。"""
    d = date.fromisoformat(trade_date[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def aggregate_weeks(rows) -> list:
    """日次行を ISO 週ごとに集約する純粋関数（DB 非依存・テスト対象）。

    入力 rows: iterable of (edinet_code, trade_date, close, volume)
    出力: [{edinet_code, week_start, trade_date, close_last, volume_sum, turnover_sum, n_days}, ...]

    - close_last = 週内最終営業日の終値（trade_date 昇順の末尾）
    - volume_sum / turnover_sum = 週内に volume が取得できた日のみ合計。1日も無ければ None
      （VWAP 派生側は turnover_sum/volume_sum、None の週は close_last にフォールバック）
    - n_days = 集約に使った営業日数
    """
    groups: dict = {}
    for ec, td, close, vol in rows:
        if close is None:
            continue
        ws = iso_week_start(td)
        groups.setdefault((ec, ws), []).append((td[:10], close, vol))

    out = []
    for (ec, ws), items in groups.items():
        items.sort(key=lambda x: x[0])          # trade_date 昇順
        last_td, last_close, _ = items[-1]
        with_vol = [(c, v) for _, c, v in items if v is not None]
        if with_vol:
            volume_sum   = sum(v for _, v in with_vol)
            turnover_sum = sum(c * v for c, v in with_vol)
        else:
            volume_sum = turnover_sum = None
        out.append(dict(
            edinet_code=ec, week_start=ws, trade_date=last_td,
            close_last=last_close, volume_sum=volume_sum,
            turnover_sum=turnover_sum, n_days=len(items),
        ))
    return out


def _daily_cutoff(window_days: int = DAILY_WINDOW_DAYS) -> str:
    """daily テーブルの保持下限日（today - window_days）を 'YYYY-MM-DD' で返す。"""
    return (date.today() - timedelta(days=window_days)).isoformat()


def trim_daily(db, window_days: int = DAILY_WINDOW_DAYS) -> int:
    """daily の保持窓より古い行を削除する（ループ収集の末尾で1回だけ呼ぶ用）。戻り値: 削除行数。"""
    res = db.execute(
        StockPriceDaily.__table__.delete()
        .where(StockPriceDaily.trade_date < _daily_cutoff(window_days))
    )
    db.commit()
    return res.rowcount or 0


def record_prices_batch(db, rows: list, *, trim: bool = True) -> int:
    """価格収集の単一チョークポイント（J-Quants/stooq/yahoo 全経路が通る）。

    rows: [{edinet_code, trade_date, close, volume?}, ...]（同一キーは呼び出し側で重複排除済み前提）
    手順: ① daily upsert → ② 触れた週のみ daily から weekly を再集約 upsert → ③ daily の trim。
    Postgres 専用（pg_insert ON CONFLICT）。集約ロジックは aggregate_weeks（純粋・テスト済）に委譲。
    戻り値: upsert した daily 行数。
    """
    rows = [r for r in rows if r.get("close") is not None and r.get("trade_date")]
    if not rows:
        return 0

    # ① daily upsert
    daily_vals = [{
        "edinet_code": r["edinet_code"], "trade_date": r["trade_date"][:10],
        "close": float(r["close"]),
        "volume": float(r["volume"]) if r.get("volume") is not None else None,
    } for r in rows]
    ins = pg_insert(StockPriceDaily).values(daily_vals)
    db.execute(ins.on_conflict_do_update(
        constraint="pk_stock_price_daily",
        set_={"close": ins.excluded.close, "volume": ins.excluded.volume},
    ))

    # ② 触れた週を daily から再集約（過去 run の部分週も含めて完全な週で確定）
    _recompute_weeks_from_daily(db, daily_vals)

    # ③ trim（古い daily を削除。weekly が全履歴を持つので情報損失なし）
    if trim:
        db.execute(
            StockPriceDaily.__table__.delete()
            .where(StockPriceDaily.trade_date < _daily_cutoff())
        )
    db.commit()
    return len(daily_vals)


def _recompute_weeks_from_daily(db, daily_vals: list) -> None:
    """daily_vals が触れた (edinet_code, week_start) の週を daily から再集約し weekly へ upsert。"""
    affected = {(r["edinet_code"], iso_week_start(r["trade_date"])) for r in daily_vals}
    if not affected:
        return
    ecs   = {ec for ec, _ in affected}
    weeks = sorted(ws for _, ws in affected)
    lo = weeks[0]
    hi = (date.fromisoformat(weeks[-1]) + timedelta(days=6)).isoformat()

    daily_rows = (
        db.query(StockPriceDaily.edinet_code, StockPriceDaily.trade_date,
                 StockPriceDaily.close, StockPriceDaily.volume)
        .filter(StockPriceDaily.edinet_code.in_(ecs),
                StockPriceDaily.trade_date >= lo,
                StockPriceDaily.trade_date <= hi)
        .all()
    )
    agg = aggregate_weeks(
        (r.edinet_code, r.trade_date, r.close, r.volume) for r in daily_rows
    )
    weekly_vals = [a for a in agg if (a["edinet_code"], a["week_start"]) in affected]
    if not weekly_vals:
        return
    wins = pg_insert(StockPriceWeekly).values(weekly_vals)
    db.execute(wins.on_conflict_do_update(
        constraint="pk_stock_price_weekly",
        set_={
            "trade_date":   wins.excluded.trade_date,
            "close_last":   wins.excluded.close_last,
            "volume_sum":   wins.excluded.volume_sum,
            "turnover_sum": wins.excluded.turnover_sum,
            "n_days":       wins.excluded.n_days,
        },
    ))


def upsert_macro_batch(db, vals: list) -> int:
    """macro_data への系列横断バルク upsert（(series_code, trade_date) 競合で最新値上書き）。

    `collect_macro_data` の単一書き込み口。系列ごとの行単位 INSERT/UPDATE 分岐を
    1 ステートメントのバルク upsert に圧縮する（Supabase pool_size 制約下の N+1 解消）。
    Postgres / SQLite 両対応（dialect に応じて insert を選択）。
    vals: [{series_code, series_name, category, trade_date, open, high, low, close, volume}, ...]
    戻り値: upsert を試みた行数（新規＋更新の合計）。
    """
    if not vals:
        return 0
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert
    stmt = _insert(MacroData).values(vals)
    # 既存行は最新値で上書き（series_name/category は系列固定のため不変＝OHLCV のみ更新）
    stmt = stmt.on_conflict_do_update(
        index_elements=["series_code", "trade_date"],
        set_={
            "open":   stmt.excluded.open,
            "high":   stmt.excluded.high,
            "low":    stmt.excluded.low,
            "close":  stmt.excluded.close,
            "volume": stmt.excluded.volume,
        },
    )
    db.execute(stmt)
    return len(vals)


def upsert_macro_beta(db, meta: dict, loadings: list) -> int:
    """M-1 per-stock 階層ベイズ推論結果を macro_beta_meta / macro_beta_loadings へ upsert。

    meta:     {run_id, snapshot_date, selected_factors[list], factor_cov[list[list]], hyperparams[dict]}
    loadings: [{run_id, edinet_code, factor_name, loading_mean, loading_se}, ...]
              （per-stock 切片は factor_name="_intercept" 行として渡す）
    run_id で冪等（既存ランは上書き）。Postgres / SQLite 両対応。戻り値は loadings 行数。
    """
    if not meta or not meta.get("run_id"):
        raise ValueError("upsert_macro_beta: meta['run_id'] は必須です")
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert

    mstmt = _insert(MacroBetaMeta).values(**meta)
    mstmt = mstmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "snapshot_date":    mstmt.excluded.snapshot_date,
            "selected_factors": mstmt.excluded.selected_factors,
            "factor_cov":       mstmt.excluded.factor_cov,
            "hyperparams":      mstmt.excluded.hyperparams,
        },
    )
    db.execute(mstmt)

    if loadings:
        lstmt = _insert(MacroBetaLoading).values(loadings)
        lstmt = lstmt.on_conflict_do_update(
            index_elements=["run_id", "edinet_code", "factor_name"],
            set_={
                "loading_mean": lstmt.excluded.loading_mean,
                "loading_se":   lstmt.excluded.loading_se,
            },
        )
        db.execute(lstmt)
    return len(loadings)


def get_macro_beta(db, run_id: str | None = None):
    """macro_beta 推論結果を読む（M-1 producer 用）。

    run_id 未指定なら最新ラン（created_at 最大・同時刻は id で決定）。戻り値:
      (meta: dict | None, loadings: {edinet_code: {factor_name: (mean, se)}})
    未蓄積なら (None, {})。
    """
    if run_id is None:
        row = (db.query(MacroBetaMeta)
               .order_by(MacroBetaMeta.created_at.desc(), MacroBetaMeta.id.desc())
               .first())
    else:
        row = db.query(MacroBetaMeta).filter_by(run_id=run_id).first()
    if row is None:
        return None, {}
    meta = {
        "run_id":           row.run_id,
        "snapshot_date":    row.snapshot_date,
        "selected_factors": row.selected_factors,
        "factor_cov":       row.factor_cov,
        "hyperparams":      row.hyperparams,
    }
    loadings: dict = {}
    for lr in db.query(MacroBetaLoading).filter_by(run_id=row.run_id).all():
        loadings.setdefault(lr.edinet_code, {})[lr.factor_name] = (lr.loading_mean, lr.loading_se)
    return meta, loadings


# ── ハイパーパラメータ探索中の producer 永続化抑止（Issue #264）─────────────────
# 探索（plugins/tuning.py）は候補パラメータごとに各プラグインの execute() をフル実行する。
# 対策なしでは M-2/M-3 の producer スコア（macro_gbdt_scores/macro_dlm_scores）が探索中の
# 中間的な（最適でない）候補予測値で都度上書きされてしまう。tuning_dry_run() 中は
# replace_macro_gbdt_scores/replace_macro_dlm_scores を no-op にし、最終選定後の本採用実行
# （tuning_dry_run() の外）でのみ実際に永続化する。
_tuning_dry_run: contextvars.ContextVar = contextvars.ContextVar("_tuning_dry_run", default=False)


@contextmanager
def tuning_dry_run():
    """このブロック内では producer スコアの永続化（replace_macro_*_scores）を no-op にする。"""
    token = _tuning_dry_run.set(True)
    try:
        yield
    finally:
        _tuning_dry_run.reset(token)


# ── ハイパーパラメータ探索中のスコアリング省略モード（Issue #299）───────────────────
# tuning_snapshot_cache()（Issue #298）で load_data/build_snapshots の重複計算は解消したが、
# M-1/M-2/M-3 の execute() は候補ごとに oof_backtest 算出後も「最終モデル再学習＋全社
# スコアリング」（M-1: _fit_final/_score_companies、M-2: raw_items構築+SHAP計算、
# M-3: 全社分の β 経路・r_macro 整形）までフル実行しており、探索が読むのは oof_backtest
# のみ（plugins/tuning.py::search()）のため無駄。tuning_dry_run() と対になる
# contextvars.ContextVar パターンで、探索中だけ各プラグインの execute() に
# 「oof_backtest 算出後、全社スコアリングをスキップして早期returnしてよい」ことを伝える。
# 通常の API 実行（/api/plugins/{name}/run）はこのコンテキストが未設定のため常にフル実行する。
_tuning_objective_only: contextvars.ContextVar = contextvars.ContextVar(
    "_tuning_objective_only", default=False
)


@contextmanager
def tuning_objective_only():
    """このブロック内では is_tuning_objective_only() が True を返す。

    各プラグインの execute() はこれを見て、oof_backtest 算出後の全社スコアリングを
    スキップした早期return分岐に入ってよい（探索の目的関数算出には不要なため）。
    """
    token = _tuning_objective_only.set(True)
    try:
        yield
    finally:
        _tuning_objective_only.reset(token)


def is_tuning_objective_only() -> bool:
    """探索中「oof_backtest算出のみで十分（全社スコアリング省略可）」モードが有効か（Issue #299）。"""
    return _tuning_objective_only.get()


# ── 5c. M-2 per-stock 勾配ブースティング予測 μ̂（ADR-0004 / Issue #234）───────────
# M-2（macro_gbdt）プラグインが execute() 末尾で書き込み、sell_ranking（consumer）が読む。
# XGBoost は線形 β 表現を持たないため、M-1 の macro_beta（β 縦持ち・read 時 μ 復元）と異なり
# per-stock μ̂ を直接保存する。最新スナップショットのみ保持（履歴不要）＝ replace 方式。
# 「producer.execute() が直書き」は sector_ols→regression_results と同じパターン（ADR-0004）。

class MacroGbdtScore(Base):
    """M-2 の per-stock 期待リターン μ̂（最新実行スナップショット）。

    sell_ranking が mu_source="macro_gbdt" 選択時に read_producer_scores 経由で読む。
    R_macro は共有 macro_beta から別途マージするため本テーブルには持たない。"""
    __tablename__ = "macro_gbdt_scores"

    edinet_code   = Column(String(10), primary_key=True)
    mu            = Column(Float, nullable=False)   # XGBoost 予測 52週先対数リターン（無次元）
    snapshot_date = Column(String(10))              # "YYYY-MM-DD"（実行時スナップ基準日）
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def replace_macro_gbdt_scores(db, rows: list, snapshot_date: str | None = None) -> int:
    """M-2 producer μ̂ を全置換する（最新スナップショットのみ保持）。

    rows = [{"edinet_code": str, "mu": float}, ...]。1 txn で全削除→一括 insert。
    mu が None の行はスキップ（予測不能銘柄を保存しない）。戻り値は保存件数。
    `tuning_dry_run()` 内では no-op（0 を返す・Issue #264）。
    """
    if _tuning_dry_run.get():
        return 0
    db.query(MacroGbdtScore).delete()
    objs = [
        MacroGbdtScore(edinet_code=r["edinet_code"], mu=float(r["mu"]),
                       snapshot_date=snapshot_date)
        for r in rows
        if r.get("edinet_code") and r.get("mu") is not None
    ]
    if objs:
        db.add_all(objs)
    db.commit()
    return len(objs)


def get_macro_gbdt_scores(db) -> dict:
    """M-2 producer μ̂ を {edinet_code: mu} で返す（未蓄積なら {}・graceful degrade）。"""
    try:
        return {r.edinet_code: r.mu for r in db.query(MacroGbdtScore).all()}
    except Exception:
        return {}


# ── 5d. M-3 per-stock ベイズ DLM 年率化アルファ μ̂（Issue #238）────────────────
# M-3（macro_dlm）プラグインが execute() 末尾で書き込み、sell_ranking（consumer）が読む。
# macro_gbdt_scores と同型。最新スナップショットのみ保持（replace 方式）。

class MacroDlmScore(Base):
    """M-3 の per-stock 期待リターン μ̂（最新実行スナップショット）。

    sell_ranking が mu_source="macro_dlm" 選択時に read_producer_scores 経由で読む。
    R_macro は共有 macro_beta から別途マージするため本テーブルには持たない。"""
    __tablename__ = "macro_dlm_scores"

    edinet_code   = Column(String(10), primary_key=True)
    mu            = Column(Float, nullable=False)   # 年率化アルファ α_T × 52（無次元）
    snapshot_date = Column(String(10))              # "YYYY-MM-DD"（実行時の最終週基準日）
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def replace_macro_dlm_scores(db, rows: list, snapshot_date: str | None = None) -> int:
    """M-3 producer μ̂ を全置換する（最新スナップショットのみ保持）。

    rows = [{"edinet_code": str, "mu": float}, ...]。1 txn で全削除→一括 insert。
    mu が None の行はスキップ（推定不能銘柄を保存しない）。戻り値は保存件数。
    `tuning_dry_run()` 内では no-op（0 を返す・Issue #264）。
    """
    if _tuning_dry_run.get():
        return 0
    db.query(MacroDlmScore).delete()
    objs = [
        MacroDlmScore(edinet_code=r["edinet_code"], mu=float(r["mu"]),
                      snapshot_date=snapshot_date)
        for r in rows
        if r.get("edinet_code") and r.get("mu") is not None
    ]
    if objs:
        db.add_all(objs)
    db.commit()
    return len(objs)


def get_macro_dlm_scores(db) -> dict:
    """M-3 producer μ̂ を {edinet_code: mu} で返す（未蓄積なら {}・graceful degrade）。"""
    try:
        return {r.edinet_code: r.mu for r in db.query(MacroDlmScore).all()}
    except Exception:
        return {}


# ── 5e. ハイパーパラメータ自動探索の結果永続化（Issue #264）─────────────────────
# hyperparameter_search.py（ローカル専用CLI）が walk-forward OOF rank-IC 等を目的関数として
# 探索した best params を保存する。plugin_name 単位で最新1件のみ保持（履歴不要）。

class PluginTunedParams(Base):
    """プラグインごとの自動調整済みハイパーパラメータ（最新1件のみ・plugin_name PK）。"""
    __tablename__ = "plugin_tuned_params"

    plugin_name      = Column(String(40), primary_key=True)
    params_json      = Column(JSON, nullable=False)    # best params（execute にそのまま渡せる形）
    objective_name   = Column(String(20), nullable=False)  # "rank_ic" | "ic_ir" | "long_short"
    objective_value  = Column(Float)
    leaderboard_json = Column(JSON)                    # 上位20件のみ（肥大化防止）
    n_combos         = Column(Integer)
    data_fingerprint = Column(String(64))              # 探索に使ったデータのハッシュ（鮮度警告用）
    tuned_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def upsert_tuned_params(db, plugin_name: str, params: dict, objective_name: str,
                        objective_value: float | None, leaderboard: list,
                        n_combos: int, data_fingerprint: str | None = None) -> None:
    """探索結果を plugin_tuned_params へ upsert する（plugin_name で冪等・db.merge）。"""
    db.merge(PluginTunedParams(
        plugin_name=plugin_name, params_json=params, objective_name=objective_name,
        objective_value=objective_value, leaderboard_json=leaderboard[:20],
        n_combos=n_combos, data_fingerprint=data_fingerprint,
        tuned_at=datetime.now(timezone.utc),
    ))
    db.commit()


def get_tuned_params(db, plugin_name: str) -> dict | None:
    """自動調整済みパラメータを読む（未調整なら None・graceful degrade）。"""
    row = db.query(PluginTunedParams).filter_by(plugin_name=plugin_name).first()
    if row is None:
        return None
    return {
        "params":           row.params_json,
        "objective_name":   row.objective_name,
        "objective_value":  row.objective_value,
        "n_combos":         row.n_combos,
        "data_fingerprint": row.data_fingerprint,
        "tuned_at":         row.tuned_at.isoformat() if row.tuned_at else None,
    }


def prices_on_or_after(db, codes: list, after: str) -> dict:
    """各 edinet_code の after 以降・最初の終値を返す（バックテストのエントリー用）。

    解像度自動切替: after が daily 窓内なら daily を引き、無ければ weekly にフォールバック。
    戻り値: {edinet_code: {"price": close, "date": trade_date}}。
    """
    if not codes:
        return {}
    result: dict = {}
    if after >= _daily_cutoff():
        result.update(_first_from(db, StockPriceDaily, StockPriceDaily.close, codes, after))
    missing = [c for c in codes if c not in result]
    if missing:
        result.update(_first_from(db, StockPriceWeekly, StockPriceWeekly.close_last, missing, after))
    return result


def latest_prices(db, codes: list) -> dict:
    """各 edinet_code の最新終値を返す（バックテストのイグジット='now' 用）。daily 優先・無ければ weekly。"""
    if not codes:
        return {}
    result = _latest_from(db, StockPriceDaily, StockPriceDaily.close, codes)
    missing = [c for c in codes if c not in result]
    if missing:
        result.update(_latest_from(db, StockPriceWeekly, StockPriceWeekly.close_last, missing))
    return result


def _first_from(db, model, price_col, codes: list, after: str) -> dict:
    from sqlalchemy import func as _f
    sq = (
        db.query(model.edinet_code, _f.min(model.trade_date).label("d"))
        .filter(model.edinet_code.in_(codes), model.trade_date >= after)
        .group_by(model.edinet_code).subquery()
    )
    rows = (
        db.query(model.edinet_code, price_col, model.trade_date)
        .join(sq, (model.edinet_code == sq.c.edinet_code) & (model.trade_date == sq.c.d))
        .all()
    )
    return {r[0]: {"price": r[1], "date": r[2]} for r in rows}


def _latest_from(db, model, price_col, codes: list) -> dict:
    from sqlalchemy import func as _f
    sq = (
        db.query(model.edinet_code, _f.max(model.trade_date).label("d"))
        .filter(model.edinet_code.in_(codes))
        .group_by(model.edinet_code).subquery()
    )
    rows = (
        db.query(model.edinet_code, price_col, model.trade_date)
        .join(sq, (model.edinet_code == sq.c.edinet_code) & (model.trade_date == sq.c.d))
        .all()
    )
    return {r[0]: {"price": r[1], "date": r[2]} for r in rows}


# ── 4. 収集ジョブログ ──────────────────────────────────────────────────────

class CollectionLog(Base):
    __tablename__ = "collection_logs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    job_type     = Column(String(50))          # full / incremental / single
    status       = Column(String(20))          # running / done / error
    started_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at  = Column(DateTime)
    companies_processed = Column(Integer, default=0)
    records_saved       = Column(Integer, default=0)
    errors_count        = Column(Integer, default=0)
    message      = Column(Text)


# ── 4b. アプリ設定永続化 ───────────────────────────────────────────────────────
# Render 等 ephemeral FS 環境でも設定が再起動後も保持されるよう DB に格納する。
# APP_PASSWORD のリセット結果はここに書き込まれ、起動時に env より優先して読まれる。

class AppSetting(Base):
    __tablename__ = "app_settings"

    key        = Column(String(64), primary_key=True)
    value      = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ── 5. マクロデータ（為替・金利・指数・コモディティ） ──────────────────────

class MacroData(Base):
    __tablename__ = "macro_data"
    __table_args__ = (
        UniqueConstraint("series_code", "trade_date", name="uq_macro_series_date"),
        Index("ix_macro_series_date", "series_code", "trade_date"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    series_code = Column(String(32), nullable=False)  # "USDJPY" / "JP_TANKAN_NONMFG_LARGE" 等
    series_name = Column(String(50))                  # 表示名（"USD/JPY"・"米10年金利" 等）
    category    = Column(String(20))                  # "fx" / "rate" / "equity" / "commodity"
    trade_date  = Column(String(10), nullable=False)  # "YYYY-MM-DD"
    open        = Column(Float)
    high        = Column(Float)
    low         = Column(Float)
    close       = Column(Float, nullable=False)
    volume      = Column(Float)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ── 5b. M-1 per-stock 階層マクロ・ベータ推論結果（#214 / ADR-0002）─────────────
# macro_beta_inference.py（GitHub Actions 推論バッチ・本番非搭載）が書き込み、M-1
# プラグイン（producer）が読む。MCMC はリクエスト時に回せないためバッチ分離する
# （ADR-0002「実行アーキ＝推論バッチ分離」）。縦持ち・DDL 追加のみ・Supabase 容量軽微。

class MacroBetaLoading(Base):
    """per-stock × 共有マクロ因子の事後ローディング（平均・SE）。

    銘柄切片は factor_name="_intercept" の行として格納する（producer が
    μ = intercept + Σ_f beta_f · macro_f を復元する）。事後SE は R1' の素。"""
    __tablename__ = "macro_beta_loadings"
    __table_args__ = (
        UniqueConstraint("run_id", "edinet_code", "factor_name", name="uq_macro_beta_loading"),
        Index("ix_macro_beta_loading_code", "edinet_code"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(String(40), nullable=False)
    edinet_code  = Column(String(10), nullable=False)
    factor_name  = Column(String(40), nullable=False)   # マクロ feature 名 or "_intercept"
    loading_mean = Column(Float, nullable=False)         # 事後平均 β（= μ の予測係数）
    loading_se   = Column(Float)                         # 事後SE（R1' の素）
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class MacroBetaMeta(Base):
    """推論ラン単位のメタ（選択因子集合・因子共分散 Σ_macro・ハイパラ）。"""
    __tablename__ = "macro_beta_meta"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_macro_beta_meta_run"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    run_id           = Column(String(40), nullable=False)
    snapshot_date    = Column(String(10))               # "YYYY-MM-DD"
    selected_factors = Column(JSON)                     # list[str]
    factor_cov       = Column(JSON)                     # list[list[float]]（Σ_macro・R_macro 用）
    hyperparams      = Column(JSON)                     # dict（draws/tune/target_accept 等）
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ── 5f. recommend ファクタープレミアム（Fama-MacBeth・Issue #271）───────────────
# recommend_factor_premia.py（ローカル専用CLI）が期間ごとの断面OLS係数を時系列平均した
# ファクタープレミアム（Newey-West補正済みSE付き）を書き込み、recommend プラグインの
# 「統計的最適化」プリセットが読む。macro_beta と同じ producer/consumer 分離パターン。

class RecommendFactorPremium(Base):
    """recommend の指標ごとのFama-MacBethファクタープレミアム（縦持ち・run_id単位）。"""
    __tablename__ = "recommend_factor_premia"
    __table_args__ = (
        UniqueConstraint("run_id", "factor_name", name="uq_recommend_factor_premia"),
    )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    run_id         = Column(String(40), nullable=False)
    factor_name    = Column(String(40), nullable=False)   # recommend.METRICS の1つ
    mean_b         = Column(Float, nullable=False)         # 期間別β_tの時系列平均（プリセット重み）
    newey_west_se  = Column(Float)                          # Newey-West補正済みSE
    t_stat         = Column(Float)
    p_value        = Column(Float)
    n_periods      = Column(Integer)                        # 回帰に使った有効期間数
    computed_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def upsert_recommend_factor_premia(db, run_id: str, rows: list) -> int:
    """recommend_factor_premia.py の結果を upsert する（run_id+factor_name で冪等）。

    rows = [{run_id, factor_name, mean_b, newey_west_se, t_stat, p_value, n_periods}, ...]
    Postgres / SQLite 両対応。戻り値は書き込み行数。
    """
    if not rows:
        return 0
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert

    stmt = _insert(RecommendFactorPremium).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id", "factor_name"],
        set_={
            "mean_b":        stmt.excluded.mean_b,
            "newey_west_se": stmt.excluded.newey_west_se,
            "t_stat":        stmt.excluded.t_stat,
            "p_value":       stmt.excluded.p_value,
            "n_periods":     stmt.excluded.n_periods,
        },
    )
    db.execute(stmt)
    return len(rows)


def get_latest_factor_premia(db, run_id: str | None = None) -> dict:
    """recommend ファクタープレミアムを読む（recommend.resolve_weights 用）。

    run_id 未指定なら最新ラン（computed_at 最大・同時刻は id で決定）。
    戻り値: {factor_name: {"mean_b", "newey_west_se", "t_stat", "p_value", "n_periods"}}。
    未蓄積なら空dict。
    """
    if run_id is None:
        latest = (db.query(RecommendFactorPremium)
                  .order_by(RecommendFactorPremium.computed_at.desc(),
                            RecommendFactorPremium.id.desc())
                  .first())
        if latest is None:
            return {}
        run_id = latest.run_id
    rows = db.query(RecommendFactorPremium).filter_by(run_id=run_id).all()
    return {
        r.factor_name: {
            "mean_b":        r.mean_b,
            "newey_west_se": r.newey_west_se,
            "t_stat":        r.t_stat,
            "p_value":       r.p_value,
            "n_periods":     r.n_periods,
        }
        for r in rows
    }


# ── 5g. 会社予想開示（statement_disclosure・Issue #322）───────────────────────
# J-Quants /fins/summary（決算短信サマリー）の生データをそのまま蓄積する。
# DisclosedDate 基準の point-in-time 原則（ルックアヘッド防止）で使うため disc_date を
# 素直に保持し、同一銘柄・同一日の複数開示（本開示＋ForecastRevision 等）も disc_no
# （J-Quants の開示番号・グローバルに一意）をキーに全件そのまま残す。予想対比サプライズ
# 等の特徴量化（f_*/m_*/d_f_*）は別タスク（Issue #322 改善案③）で行う。

class StatementDisclosure(Base):
    """決算短信サマリーの生開示データ（1開示 = 1レコード）。"""
    __tablename__ = "statement_disclosure"
    __table_args__ = (
        Index("ix_statement_disclosure_edinet_date", "edinet_code", "disc_date"),
    )

    disc_no      = Column(String(20), primary_key=True)   # DiscNo（開示番号・グローバルに一意）
    edinet_code  = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    sec_code     = Column(String(6))
    disc_date    = Column(String(10), nullable=False)      # DiscDate "YYYY-MM-DD"（point-in-time キー）
    disc_time    = Column(String(8))                       # DiscTime "HH:MM:SS"
    doc_type     = Column(String(60))                      # DocType（FYFinancialStatements_Consolidated_IFRS 等）
    cur_per_type = Column(String(4))                       # CurPerType（FY/1Q/2Q/3Q）
    cur_per_st   = Column(String(10))
    cur_per_en   = Column(String(10))
    cur_fy_st    = Column(String(10))
    cur_fy_en    = Column(String(10))
    nxt_fy_st    = Column(String(10))
    nxt_fy_en    = Column(String(10))

    # 実績
    sales   = Column(Float)
    op      = Column(Float)
    odp     = Column(Float)   # 経常利益（IFRS採用企業は空＝概念なし）
    np      = Column(Float)
    eps     = Column(Float)
    deps    = Column(Float)
    div_ann = Column(Float)   # 実績年間配当

    # 予想（当期）
    f_sales   = Column(Float)
    f_op      = Column(Float)
    f_odp     = Column(Float)
    f_np      = Column(Float)
    f_eps     = Column(Float)
    f_div_ann = Column(Float)

    # 予想（翌期）
    nxf_sales = Column(Float)
    nxf_op    = Column(Float)
    nxf_odp   = Column(Float)
    nxf_np    = Column(Float)
    nxf_eps   = Column(Float)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def upsert_statement_disclosures(db, rows: list) -> int:
    """statement_disclosure を disc_no（PK）で upsert する。Postgres / SQLite 両対応。

    rows = [{disc_no, edinet_code, sec_code, disc_date, ..., f_eps, nxf_eps}, ...]
    戻り値は書き込み行数。
    """
    if not rows:
        return 0
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert

    update_cols = [c.name for c in StatementDisclosure.__table__.columns
                   if c.name not in ("disc_no", "created_at")]
    stmt = _insert(StatementDisclosure).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["disc_no"],
        set_={col: getattr(stmt.excluded, col) for col in update_cols},
    )
    db.execute(stmt)
    return len(rows)


# ── 6. XBRL 生データ中間テーブル ──────────────────────────────────────────

class XbrlRawDocument(Base):
    """EDINET XBRL CSV の生データ。新指標追加時に再 parse する用。1 書類 = 1 レコード。"""
    __tablename__ = "xbrl_raw_documents"
    __table_args__ = (
        UniqueConstraint("doc_id", name="uq_xbrl_raw_doc_id"),
        Index("ix_xbrl_raw_edinet_period", "edinet_code", "period_end"),
    )

    id              = Column(Integer, primary_key=True, autoincrement=True)
    doc_id          = Column(String(20), nullable=False, index=True)
    edinet_code     = Column(String(10), nullable=False, index=True)
    period_end      = Column(Date, nullable=True)
    elements_gz     = Column(LargeBinary, nullable=False)
    elements_format = Column(String(10), default="gzip+json")
    n_rows          = Column(Integer)
    fetched_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def pack_elements(rows: list) -> bytes:
    """[{element, context, value}, ...] を gzip(JSON) に圧縮"""
    return gzip.compress(json.dumps(rows, ensure_ascii=False).encode("utf-8"))


def unpack_elements(blob: bytes) -> list:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def _parse_period_end(s) -> "date | None":
    """文字列 'YYYY-MM-DD' または date オブジェクトを date に変換する。None/空文字は None を返す。"""
    if s is None:
        return None
    if isinstance(s, date):
        return s
    s = str(s).strip()
    if not s or s in ("", "NULL", "None"):
        return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def upsert_xbrl_raw(db, doc_id: str, edinet_code: str, period_end: str, rows: list):
    blob = pack_elements(rows)
    now  = datetime.now(timezone.utc)
    stmt = pg_insert(XbrlRawDocument).values(
        doc_id=doc_id, edinet_code=edinet_code, period_end=_parse_period_end(period_end),
        elements_gz=blob, elements_format="gzip+json", n_rows=len(rows), fetched_at=now,
    ).on_conflict_do_update(
        index_elements=["doc_id"],
        set_={"elements_gz": blob, "n_rows": len(rows), "fetched_at": now},
    )
    db.execute(stmt)


# ── 8. 回帰分析の出力（重い派生・本体から隔離） ────────────────────────────
# 業種別OLS/Ridge の予測値・乖離率。financial_records（ソース＋軽い派生）とは
# 別テーブルに保持し、「計算結果」と「生データ」をDB上で分離する。
# 重い回帰計算はローカルで実行し、ここへ書き込む（Render は読むだけ）。

class RegressionResult(Base):
    __tablename__ = "regression_results"
    __table_args__ = (
        PrimaryKeyConstraint("edinet_code", "year", "period_end",
                             name="pk_regression_results"),
        Index("ix_regr_industry_year", "sector", "year"),
    )

    edinet_code          = Column(String(10), nullable=False)
    year                 = Column(Integer, nullable=False)
    period_end           = Column(Date, nullable=True)
    predicted_market_cap = Column(Float)   # 回帰モデル予測時価総額（百万円）
    gap_ratio            = Column(Float)   # 乖離率 %（(予測-実績)/実績*100）
    model                = Column(String(20))   # "ols" / "ridge"
    sector               = Column(String(100))  # 学習に使った業種
    computed_at          = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                                  onupdate=lambda: datetime.now(timezone.utc))


def upsert_regression_result(db, *, edinet_code: str, year: int, period_end: str,
                             predicted_market_cap, gap_ratio, model: str, sector: str):
    """OLS/Ridge の予測値を regression_results に upsert する。

    主キー (edinet_code, year, period_end) で merge するため PostgreSQL / SQLite の
    どちらでも動作する（pg_insert の ON CONFLICT は Postgres 専用のため使わない）。
    """
    db.merge(RegressionResult(
        edinet_code=edinet_code, year=year, period_end=_parse_period_end(period_end),
        predicted_market_cap=predicted_market_cap, gap_ratio=gap_ratio,
        model=model, sector=sector, computed_at=datetime.now(timezone.utc),
    ))


# ── 9. 読み取りモデル: financial_metrics VIEW ──────────────────────────────
# financial_records（ソース列）から軽い派生（比率・Zスコア・成長率）を「都度SQL算出」し、
# regression_results を LEFT JOIN して予測値も合成する読み取り専用 VIEW。
# 派生値はDBに保存しない（関数型）。計算は Supabase 側で走るため Render の CPU を使わない。
# 式は collector.calc_derived / database._calc_zscore_for_year / calc_growth_rates と一致させてある
# （truthy フォールバック・標本SD・sd=0→1.0・n>=2・丸め桁）。

ViewBase = declarative_base()   # create_all に VIEW を CREATE TABLE させないため別メタデータ


class FinancialMetric(ViewBase):
    """financial_metrics VIEW の読み取り専用 ORM マッピング。属性名は FinancialRecord と一致。"""
    __tablename__ = "financial_metrics"

    id           = Column(Integer, primary_key=True)
    edinet_code  = Column(String(10))
    sec_code     = Column(String(6))
    company_name = Column(String(200))
    industry     = Column(String(100))
    market       = Column(String(50))
    year         = Column(Integer)
    period_end   = Column(Date, nullable=True)
    doc_id       = Column(String(20))
    source       = Column(String(50))
    accounting_standard = Column(String(20))
    # companies を LEFT JOIN して合成（Issue #315）。既存行との後方互換のため NOT NULL 制約は付けない。
    is_active     = Column(Boolean)
    delisted_date = Column(Date, nullable=True)
    # ソース（financial_records からそのまま）
    bs_total_assets = Column(Float); bs_current_assets = Column(Float)
    bs_receivables = Column(Float); bs_inventory = Column(Float)
    bs_noncurrent_assets = Column(Float); bs_buildings = Column(Float)
    bs_machinery = Column(Float); bs_ppe_total = Column(Float)
    bs_intangible_assets = Column(Float); bs_investments_other_assets = Column(Float)
    bs_cash = Column(Float); bs_investment_securities = Column(Float)
    bs_total_liabilities = Column(Float); bs_current_liabilities = Column(Float)
    bs_payables = Column(Float); bs_noncurrent_liabilities = Column(Float)
    bs_short_term_debt = Column(Float); bs_long_term_debt = Column(Float)
    bs_bonds_payable = Column(Float); bs_total_equity = Column(Float)
    bs_equity_parent = Column(Float); bs_paid_in_capital = Column(Float)
    bs_retained_earnings = Column(Float); bs_bps = Column(Float)
    pl_revenue = Column(Float); pl_cost_of_sales = Column(Float)
    pl_gross_profit = Column(Float); pl_sga = Column(Float)
    pl_operating_profit = Column(Float); pl_nonoperating_income = Column(Float)
    pl_ordinary_profit = Column(Float); pl_pretax_profit = Column(Float)
    pl_net_income = Column(Float); pl_net_income_attr = Column(Float)
    pl_eps = Column(Float); pl_ebitda = Column(Float)
    pl_rd_expenses = Column(Float); pl_depreciation = Column(Float)
    pl_extraordinary_income = Column(Float); pl_extraordinary_loss = Column(Float)
    cf_operating_cf = Column(Float); cf_investing_cf = Column(Float)
    cf_financing_cf = Column(Float); cf_free_cf = Column(Float)
    cf_net_change_cash = Column(Float); cf_capex = Column(Float)
    stock_price = Column(Float); market_cap = Column(Float)
    per = Column(Float); pbr = Column(Float); div_yield = Column(Float); dps = Column(Float)
    employees = Column(Float); issued_shares = Column(Float)
    # 軽い派生（VIEW が都度算出）
    op_margin = Column(Float); net_margin = Column(Float)
    roe = Column(Float); roa = Column(Float)
    equity_ratio = Column(Float); de_ratio = Column(Float); cf_ratio = Column(Float)
    rd_intensity = Column(Float); da_intensity = Column(Float)
    asset_turnover = Column(Float)
    net_cash = Column(Float); nc_ratio = Column(Float)
    z_revenue = Column(Float); z_op_margin = Column(Float); z_roe = Column(Float)
    z_equity_ratio = Column(Float); z_cf_ratio = Column(Float); z_eps = Column(Float)
    z_de_ratio = Column(Float); z_nc_ratio = Column(Float)
    rev_growth = Column(Float); op_growth = Column(Float); eps_growth = Column(Float)
    # 回帰出力（regression_results を LEFT JOIN）
    predicted_market_cap = Column(Float); gap_ratio = Column(Float)


class FinancialMetricInterim(ViewBase):
    """financial_metrics_interim VIEW（非通期=半期H1等）の読み取り専用 ORM（Issue #219② フェーズC）。

    通期用 FinancialMetric と対をなす。#323 イベント駆動モデルへ H1 実績ファンダを供給する。
    通期版との差分: period_type/filing_date を持つ／Zスコア・回帰予測（predicted/gap）は持たない
    （#323 は独自正規化・年次OLS予測は H1 に非該当）／成長率は同一 period_type の前年同期比。"""
    __tablename__ = "financial_metrics_interim"

    id           = Column(Integer, primary_key=True)
    edinet_code  = Column(String(10))
    sec_code     = Column(String(6))
    company_name = Column(String(200))
    industry     = Column(String(100))
    market       = Column(String(50))
    year         = Column(Integer)
    period_end   = Column(Date, nullable=True)
    period_type  = Column(String(10))       # 'H1' 等（非通期）
    filing_date  = Column(Date, nullable=True)  # 提出日（point-in-time 基準）
    doc_id       = Column(String(20))
    source       = Column(String(50))
    accounting_standard = Column(String(20))
    is_active     = Column(Boolean)
    delisted_date = Column(Date, nullable=True)
    # ソース（financial_records からそのまま）
    bs_total_assets = Column(Float); bs_current_assets = Column(Float)
    bs_receivables = Column(Float); bs_inventory = Column(Float)
    bs_noncurrent_assets = Column(Float); bs_buildings = Column(Float)
    bs_machinery = Column(Float); bs_ppe_total = Column(Float)
    bs_intangible_assets = Column(Float); bs_investments_other_assets = Column(Float)
    bs_cash = Column(Float); bs_investment_securities = Column(Float)
    bs_total_liabilities = Column(Float); bs_current_liabilities = Column(Float)
    bs_payables = Column(Float); bs_noncurrent_liabilities = Column(Float)
    bs_short_term_debt = Column(Float); bs_long_term_debt = Column(Float)
    bs_bonds_payable = Column(Float); bs_total_equity = Column(Float)
    bs_equity_parent = Column(Float); bs_paid_in_capital = Column(Float)
    bs_retained_earnings = Column(Float); bs_bps = Column(Float)
    pl_revenue = Column(Float); pl_cost_of_sales = Column(Float)
    pl_gross_profit = Column(Float); pl_sga = Column(Float)
    pl_operating_profit = Column(Float); pl_nonoperating_income = Column(Float)
    pl_ordinary_profit = Column(Float); pl_pretax_profit = Column(Float)
    pl_net_income = Column(Float); pl_net_income_attr = Column(Float)
    pl_eps = Column(Float); pl_ebitda = Column(Float)
    pl_rd_expenses = Column(Float); pl_depreciation = Column(Float)
    pl_extraordinary_income = Column(Float); pl_extraordinary_loss = Column(Float)
    cf_operating_cf = Column(Float); cf_investing_cf = Column(Float)
    cf_financing_cf = Column(Float); cf_free_cf = Column(Float)
    cf_net_change_cash = Column(Float); cf_capex = Column(Float)
    stock_price = Column(Float); market_cap = Column(Float)
    per = Column(Float); pbr = Column(Float); div_yield = Column(Float); dps = Column(Float)
    employees = Column(Float); issued_shares = Column(Float)
    # 軽い派生（VIEW が都度算出・通期版と同一式）。市場依存（nc_ratio 等）は H1 で NULL になりうる。
    op_margin = Column(Float); net_margin = Column(Float)
    roe = Column(Float); roa = Column(Float)
    equity_ratio = Column(Float); de_ratio = Column(Float); cf_ratio = Column(Float)
    rd_intensity = Column(Float); da_intensity = Column(Float)
    asset_turnover = Column(Float)
    net_cash = Column(Float); nc_ratio = Column(Float)
    # 前年同期比（同一 period_type 内の YoY・H1 vs 前年 H1）
    rev_growth = Column(Float); op_growth = Column(Float); eps_growth = Column(Float)


# financial_metrics VIEW DDL（sql/financial_metrics_view.sql から読み込み）
FINANCIAL_METRICS_VIEW_SQL = (Path(__file__).parent / "sql" / "financial_metrics_view.sql").read_text(encoding="utf-8")
# financial_metrics_interim VIEW DDL（Issue #219② フェーズC）
FINANCIAL_METRICS_INTERIM_VIEW_SQL = (Path(__file__).parent / "sql" / "financial_metrics_interim_view.sql").read_text(encoding="utf-8")


# ── 10. DB初期化 ───────────────────────────────────────────────────────────

# 新規ソース列（冪等 ADD）。計算列は含めない（VIEW が担う）。
_NEW_COLS = [
    "pl_cost_of_sales", "pl_sga", "pl_nonoperating_income",
    "bs_receivables", "bs_inventory",
    "bs_buildings", "bs_machinery", "bs_intangible_assets",
    "bs_payables", "bs_bonds_payable",
    "bs_paid_in_capital", "bs_retained_earnings",
    "bs_investment_securities",
    "bs_ppe_total", "bs_investments_other_assets",
    "pl_rd_expenses", "pl_depreciation",
    "pl_extraordinary_income", "pl_extraordinary_loss",
    "employees", "issued_shares",
]

# 旧計算列（冪等 DROP）。派生指標は financial_metrics VIEW・OLS予測値は regression_results に移行済み。
_LEGACY_COMPUTED_COLS = [
    "op_margin", "net_margin", "roe", "roa", "equity_ratio", "de_ratio",
    "cf_ratio", "net_cash", "nc_ratio",
    "z_revenue", "z_op_margin", "z_roe", "z_equity_ratio", "z_cf_ratio",
    "z_eps", "z_de_ratio", "z_nc_ratio",
    "rev_growth", "op_growth", "eps_growth",
    "predicted_market_cap", "gap_ratio",
]

# デバッグ用に保存していた列（冪等 DROP・容量削減・Issue #219 ①）。読取箇所ゼロ・
# parse済み値のみで生タグを保持せず reparse 用途にも使えなかった（GOTCHAS.md参照）。
# financial_records 約73MBの主因＝Supabase 500MB制約下の第2の容量レバー。
_DEBUG_ONLY_COLS = [
    "raw_xbrl_json",
]


def _ensure_tables() -> None:
    """Phase 1: テーブル作成・インデックス・カラムマイグレーション（すべて冪等）"""
    import re as _re
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_companies_name_gin "
            "ON companies USING gin(to_tsvector('simple', name))"
        ))
        conn.execute(text(
            "ALTER TABLE companies ADD COLUMN IF NOT EXISTS issued_shares DOUBLE PRECISION"
        ))
        conn.execute(text(
            "ALTER TABLE companies ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        conn.execute(text(
            "ALTER TABLE companies ADD COLUMN IF NOT EXISTS delisted_date DATE"
        ))
        for col in _NEW_COLS:
            conn.execute(text(
                f"ALTER TABLE financial_records ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION"
            ))
        # 開示粒度列（Issue #219② フェーズA・加算的マイグレーション＝非破壊）。
        # NOT NULL DEFAULT 'annual' で既存全行を通期にバックフィル（原子的）。filing_date は
        # 通期の既存行では未捕捉のため nullable（フェーズB の半期収集で提出日を投入）。
        conn.execute(text(
            "ALTER TABLE financial_records ADD COLUMN IF NOT EXISTS "
            "period_type VARCHAR(10) NOT NULL DEFAULT 'annual'"
        ))
        conn.execute(text(
            "ALTER TABLE financial_records ADD COLUMN IF NOT EXISTS filing_date DATE"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_period_type ON financial_records (period_type)"
        ))
        # UNIQUE 制約に period_type を追加して改称（Issue #219② フェーズB・冪等）。
        # 新制約が既に在れば何もしない。無ければ旧3列制約を落として4列制約を張る。
        # 列追加（範囲拡大）のみで既存行の一意性は保たれるため ADD は失敗しない。
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'uq_edinet_year_period_pt'
                      AND table_name = 'financial_records'
                ) THEN
                    ALTER TABLE financial_records
                        DROP CONSTRAINT IF EXISTS uq_edinet_year_period;
                    ALTER TABLE financial_records
                        ADD CONSTRAINT uq_edinet_year_period_pt
                        UNIQUE (edinet_code, year, period_end, period_type);
                END IF;
            END $$
        """))
        for col in _LEGACY_COMPUTED_COLS + _DEBUG_ONLY_COLS:
            conn.execute(text(
                f"ALTER TABLE financial_records DROP COLUMN IF EXISTS {col}"
            ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_xbrl_raw_edinet_period "
            "ON xbrl_raw_documents (edinet_code, period_end)"
        ))
        # period_end を VARCHAR(20) → DATE 型に変換するマイグレーション（冪等）
        # SKIP_PERIOD_END_MIGRATION=1 で skip できるフェールセーフ付き
        if not os.environ.get("SKIP_PERIOD_END_MIGRATION"):
            try:
                # financial_metrics VIEW が financial_records.period_end に依存するため、
                # ALTER 前に VIEW を DROP する（直後に init_db→_ensure_view が再作成）。
                # VIEW を落とさないと "cannot alter type of a column used by a view" で
                # 失敗し、period_end が varchar のまま残る（→ date 比較クエリが全滅）。
                # 移行が必要なとき（financial_records.period_end が varchar）だけ落とし、
                # 既に DATE の DB（本番）は何もしない（冪等・VIEW churn なし）。
                needs_migration = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='financial_records' AND column_name='period_end'
                        AND data_type='character varying'
                    )
                """)).scalar()
                if needs_migration:
                    conn.execute(text("DROP VIEW IF EXISTS financial_metrics"))
                    for tbl in ("financial_records", "xbrl_raw_documents", "regression_results"):
                        conn.execute(text(f"""
                            DO $$
                            BEGIN
                                IF EXISTS (
                                    SELECT 1 FROM information_schema.columns
                                    WHERE table_name='{tbl}' AND column_name='period_end'
                                    AND data_type='character varying'
                                ) THEN
                                    ALTER TABLE {tbl}
                                    ALTER COLUMN period_end TYPE DATE
                                    USING NULLIF(NULLIF(period_end,''), 'NULL')::DATE;
                                END IF;
                            END $$
                        """))
            except Exception as _e:
                log.warning(
                    f"period_end DATE 型マイグレーション失敗"
                    f"（SKIP_PERIOD_END_MIGRATION=1 で回避可能）: {_e}"
                )
        conn.commit()


def _ensure_one_view(view_name: str, view_sql: str) -> None:
    """VIEW を定義変更時のみ DROP+再作成する（毎起動 DROP を避ける）。

    pg_get_viewdef() で現行定義を取得して view_sql と比較し、差異がなければスキップ。
    VIEW 未存在・比較不能（SQLite等）の場合は無条件に再作成する。
    列の追加・並び替えは CREATE OR REPLACE が「末尾追加のみ可」で失敗するため DROP→再作成する
    （両 VIEW とも依存オブジェクトは無く安全）。
    """
    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"\s+", " ", s.strip().rstrip(";"))

    needs_recreate = True
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT pg_get_viewdef(:v, true)").bindparams(v=view_name)
            ).first()
            if row and row[0]:
                needs_recreate = (_norm(row[0]) != _norm(view_sql))
    except Exception as e:
        # pg_get_viewdef 未対応（SQLite 等）・VIEW 未存在・接続エラーのいずれか → 再作成。
        log.debug(f"{view_name} VIEW 定義の取得失敗 → 再作成する（理由: {e!r}）")
        needs_recreate = True

    if needs_recreate:
        with engine.connect() as conn:
            conn.execute(text(f"DROP VIEW IF EXISTS {view_name}"))
            conn.execute(text(view_sql))
            conn.commit()

    # security_invoker=true を冪等に保証する（Issue #344・security_definer_view 解消）。
    # pg_get_viewdef() は security_invoker オプションを SQL 定義に含めないため上の
    # needs_recreate 比較では検出できない。再作成の有無に関わらずここで常に設定する
    # （冪等・churn なし）。これにより VIEW 経由の基テーブルアクセスは querying user の
    # 権限・RLS に従い、anon からの VIEW 越し読み取りを遮断する（アプリは postgres 直結の
    # BYPASSRLS ロールのため無影響）。SQLite は VIEW の SET オプション非対応のため無視
    # （テストは postgres VIEW を生成せず本パスを通らない）。
    try:
        with engine.connect() as conn:
            conn.execute(text(f"ALTER VIEW {view_name} SET (security_invoker = true)"))
            conn.commit()
    except Exception as e:
        log.debug(f"{view_name} の security_invoker 設定をスキップ（SQLite 等・理由: {e!r}）")


def _ensure_view() -> None:
    """Phase 2: 読み取り専用 VIEW を定義変更時のみ再作成する。

    financial_metrics（通期）と financial_metrics_interim（非通期=半期H1等・Issue #219② フェーズC）
    の両方。両者は独立で依存関係が無いため順序は任意。regression_results は create_all 後なので
    financial_metrics の LEFT JOIN は可能。
    """
    _ensure_one_view("financial_metrics", FINANCIAL_METRICS_VIEW_SQL)
    _ensure_one_view("financial_metrics_interim", FINANCIAL_METRICS_INTERIM_VIEW_SQL)


def init_db():
    """テーブル作成・インデックス構築・カラムマイグレーション"""
    _ensure_tables()
    _ensure_view()


# ── 7. Upsert 処理 ─────────────────────────────────────────────────────────

def get_setting(db, key: str):
    """app_settings から値を取得。未設定なら None。"""
    row = db.query(AppSetting).filter_by(key=key).first()
    return row.value if row else None


def upsert_setting(db, key: str, value: str) -> None:
    """app_settings へ key=value を upsert しコミット。"""
    row = db.query(AppSetting).filter_by(key=key).first()
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_at=datetime.now(timezone.utc)))
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    db.commit()

def upsert_company(db, data: dict) -> Company:
    obj = db.query(Company).filter_by(edinet_code=data["edinet_code"]).first()
    if obj is None:
        obj = Company(**{k: v for k, v in data.items() if hasattr(Company, k)})
        db.add(obj)
        obj.updated_at = datetime.now(timezone.utc)
        return obj
    # 既存: 実値が変わるフィールドだけ更新する（空文字/None で実値を潰さない）。
    # 3500+ 社の dirty UPDATE が一気に流れると Supabase が read-only に転ぶため
    changed = False
    for k, v in data.items():
        if not hasattr(Company, k):
            continue
        if v in (None, ""):
            continue
        if getattr(obj, k) != v:
            setattr(obj, k, v)
            changed = True
    if changed:
        obj.updated_at = datetime.now(timezone.utc)
    return obj


def sync_active_status(db, active_sec_codes: set) -> dict:
    """企業マスタの上場状態を、現在の上場銘柄集合（J-Quants listed/info 由来）と同期する（Issue #315）。

    is_active=True の企業が active_sec_codes に無ければ delisted 扱い（is_active=False,
    delisted_date=today）。逆に is_active=False の企業が active_sec_codes に含まれていれば
    再上場・誤検出からの復帰とみなし is_active=True・delisted_date=None に戻す（自己修復）。
    sec_code が空の企業は突合不能のため対象外。
    戻り値: {"delisted": 新規delisted件数, "reactivated": 復帰件数}
    """
    from sqlalchemy import update as sa_update

    currently_active = {
        sec for (sec,) in db.query(Company.sec_code)
        .filter(Company.is_active.is_(True), Company.sec_code.isnot(None), Company.sec_code != "")
        .all()
    }
    currently_inactive = {
        sec for (sec,) in db.query(Company.sec_code)
        .filter(Company.is_active.is_(False), Company.sec_code.isnot(None), Company.sec_code != "")
        .all()
    }
    newly_delisted = currently_active - active_sec_codes
    reactivated    = currently_inactive & active_sec_codes

    if newly_delisted:
        db.execute(
            sa_update(Company).where(Company.sec_code.in_(newly_delisted))
            .values(is_active=False, delisted_date=date.today())
            .execution_options(synchronize_session=False)
        )
    if reactivated:
        db.execute(
            sa_update(Company).where(Company.sec_code.in_(reactivated))
            .values(is_active=True, delisted_date=None)
            .execution_options(synchronize_session=False)
        )
    if newly_delisted or reactivated:
        db.commit()
    return {"delisted": len(newly_delisted), "reactivated": len(reactivated)}


def upsert_financial(db, data: dict) -> FinancialRecord:
    """BS/PL/CF辞書をフラット化してUpsert"""
    flat = {
        "edinet_code":        data.get("edinet_code"),
        "sec_code":           data.get("sec_code"),
        "company_name":       data.get("company_name"),
        "industry":           data.get("industry"),
        "market":             data.get("market"),
        "accounting_standard":data.get("accounting_standard"),
        "year":               data.get("year"),
        "period_end":         _parse_period_end(data.get("period_end")),
        "doc_id":             data.get("doc_id"),
        "source":             data.get("source", "EDINET_XBRL"),
        # 開示粒度（Issue #219②）。通期収集は未指定→既定 'annual'。半期収集は 'H1' 等を渡す。
        "period_type":        data.get("period_type", "annual"),
        "filing_date":        _parse_period_end(data.get("filing_date")),
    }
    # BS
    for k, v in data.get("bs", {}).items():
        flat[f"bs_{k}"] = v
    # PL
    for k, v in data.get("pl", {}).items():
        flat[f"pl_{k}"] = v
    # CF
    for k, v in data.get("cf", {}).items():
        flat[f"cf_{k}"] = v
    # derived（op_margin / roe / net_cash 等の計算結果）は financial_records には保存しない。
    # financial_metrics VIEW がソース列から都度算出する（計算結果と生データのDB分離）。
    # val (market data) は市場スナップショットのため保存する。
    for k, v in data.get("val", {}).items():
        flat[k] = v
    # nonfin（従業員数・発行済株式数など非財務）はプレフィックス無しの直接列にマップ（C2）
    for k, v in data.get("nonfin", {}).items():
        flat[k] = v

    # 未知キーは silent-drop せず fail fast。bs/pl/cf は XBRL_MAP=列 info 由来で構造保証されるため、
    # 実際に発火し得るのは collector が手で組む val/nonfin キーの typo（開発時バグ）に限られる。
    unknown = [k for k in flat if not hasattr(FinancialRecord, k)]
    if unknown:
        raise ValueError(
            f"upsert_financial: FinancialRecord に無い未知キー {unknown}"
            f"（val/nonfin の typo か列追加忘れ）"
        )

    obj = db.query(FinancialRecord).filter_by(
        edinet_code=flat["edinet_code"],
        year=flat["year"],
        period_end=flat.get("period_end"),
        period_type=flat["period_type"],
    ).first()

    if obj is None:
        # flat のキーは上の検証で全て FinancialRecord 列であることを保証済み
        obj = FinancialRecord(**flat)
        db.add(obj)
        db.flush()  # autoflush=False のため明示的にフラッシュ（同一セッション内の重複を防ぐ）
    else:
        for k, v in flat.items():
            if v is not None:
                setattr(obj, k, v)
    obj.updated_at = datetime.now(timezone.utc)
    return obj


# 成長率・Zスコアの事前計算関数（calc_growth_rates / calc_zscore_normalization /
# _calc_zscore_for_year）は廃止した。これらは financial_records の計算列へ書き戻す実装
# だったが、派生指標は financial_metrics VIEW がソース列から都度算出する方式へ移行済み
# （計算結果と生データのDB分離）。算出ロジックは FINANCIAL_METRICS_VIEW_SQL を参照。


def latest_year_subq(db, model):
    """企業ごとの最新年度レコードを1行に絞るサブクエリを返す。

    model には FinancialRecord または FinancialMetric を渡す。
    用途: 最新年度のみを対象にするクエリで join に利用する。

    例:
        subq = latest_year_subq(db, FinancialRecord)
        rows = db.query(FinancialRecord).join(
            subq,
            (FinancialRecord.edinet_code == subq.c.edinet_code) &
            (FinancialRecord.year == subq.c.max_year)
        ).all()
    """
    return (
        db.query(model.edinet_code, func.max(model.year).label("max_year"))
        .group_by(model.edinet_code)
        .subquery()
    )
