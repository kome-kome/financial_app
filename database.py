"""
PostgreSQL スキーマ定義・ORM・upsert処理
テーブル構成:
  companies        — 企業マスタ（EDINETコード・証券コード・業種）
  financial_records — BS/PL/CF 再分類済み年次財務データ
  derived_metrics   — 正規化・計算済み財務指標
  collection_logs   — 収集ジョブログ
"""

import os, gzip, json
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, DateTime,
    Text, UniqueConstraint, PrimaryKeyConstraint, Index, JSON, LargeBinary, ForeignKey, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.dialects.postgresql import insert as pg_insert

load_dotenv()

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
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    records = relationship("FinancialRecord", back_populates="company",
                           cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Company {self.sec_code} {self.name}>"


# ── 2. 財務レコード（BS/PL/CF 再分類済み） ────────────────────────────────

class FinancialRecord(Base):
    __tablename__ = "financial_records"
    __table_args__ = (
        UniqueConstraint("edinet_code", "year", "period_end", name="uq_edinet_year_period"),
        Index("ix_sec_year", "sec_code", "year"),
        Index("ix_industry_year", "industry", "year"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    edinet_code  = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    sec_code     = Column(String(6))
    company_name = Column(String(200))
    industry     = Column(String(100))
    market       = Column(String(50))
    year         = Column(Integer, nullable=False)
    period_end   = Column(String(20))                      # 決算期末日 YYYY-MM-DD
    doc_id       = Column(String(20))                      # EDINET書類管理番号
    source       = Column(String(50), default="EDINET_XBRL")
    accounting_standard = Column(String(20))

    # ── BS（貸借対照表）再分類項目 ──────────────────────────────────────
    bs_total_assets         = Column(Float)   # 総資産
    bs_current_assets       = Column(Float)   # 流動資産
    bs_receivables          = Column(Float)   # 売掛金（売上債権）
    bs_inventory            = Column(Float)   # 棚卸資産
    bs_noncurrent_assets    = Column(Float)   # 固定資産
    bs_buildings            = Column(Float)   # 建物及び構築物
    bs_machinery            = Column(Float)   # 機械装置及び運搬具
    bs_intangible_assets    = Column(Float)   # 無形固定資産
    bs_cash                 = Column(Float)   # 現金・預金
    bs_investment_securities = Column(Float)  # 投資有価証券（清原式ネットキャッシュ用）
    bs_total_liabilities    = Column(Float)   # 総負債
    bs_current_liabilities  = Column(Float)   # 流動負債
    bs_payables             = Column(Float)   # 買掛金（仕入債務）
    bs_noncurrent_liabilities = Column(Float) # 固定負債
    bs_short_term_debt      = Column(Float)   # 短期借入金
    bs_long_term_debt       = Column(Float)   # 長期借入金
    bs_bonds_payable        = Column(Float)   # 社債
    bs_total_equity         = Column(Float)   # 純資産（連結）
    bs_equity_parent        = Column(Float)   # 親会社株主帰属持分（IFRS）
    bs_paid_in_capital      = Column(Float)   # 資本金
    bs_retained_earnings    = Column(Float)   # 利益剰余金
    bs_bps                  = Column(Float)   # 1株純資産

    # ── PL（損益計算書）再分類項目 ──────────────────────────────────────
    pl_revenue              = Column(Float)   # 売上高
    pl_cost_of_sales        = Column(Float)   # 売上原価
    pl_gross_profit         = Column(Float)   # 売上総利益
    pl_sga                  = Column(Float)   # 販売費及び一般管理費
    pl_operating_profit     = Column(Float)   # 営業利益
    pl_nonoperating_income  = Column(Float)   # 営業外損益（純額）= 経常利益 - 営業利益
    pl_ordinary_profit      = Column(Float)   # 経常利益
    pl_pretax_profit        = Column(Float)   # 税前利益
    pl_net_income           = Column(Float)   # 当期純利益
    pl_net_income_attr      = Column(Float)   # 親会社帰属純利益（IFRS）
    pl_eps                  = Column(Float)   # EPS（円）
    pl_ebitda               = Column(Float)   # EBITDA（計算値）

    # ── CF（キャッシュフロー）再分類項目 ────────────────────────────────
    cf_operating_cf         = Column(Float)   # 営業CF
    cf_investing_cf         = Column(Float)   # 投資CF
    cf_financing_cf         = Column(Float)   # 財務CF
    cf_free_cf              = Column(Float)   # フリーCF（計算値）
    cf_net_change_cash      = Column(Float)   # 現金増減
    cf_capex                = Column(Float)   # 設備投資額

    # ── 市場データ（株価・バリュエーション・収集時点スナップショット）────
    stock_price             = Column(Float)   # 株価（収集時点）
    market_cap              = Column(Float)   # 時価総額（百万円）
    per                     = Column(Float)   # PER
    pbr                     = Column(Float)   # PBR
    div_yield               = Column(Float)   # 配当利回り %
    dps                     = Column(Float)   # 1株配当

    # 計算結果（派生比率・Zスコア・成長率・OLS予測値）は financial_records には保持しない。
    #   - 軽い派生／Zスコア／成長率 → financial_metrics VIEW（ソース列から都度算出）
    #   - OLS予測値（predicted_market_cap / gap_ratio）→ regression_results テーブル
    # 旧計算列は本コミットで DROP 済み（init_db の DROP マイグレーション参照）。

    raw_xbrl_json           = Column(JSON)    # 生XBRLデータ（デバッグ用）
    created_at              = Column(DateTime, default=datetime.utcnow)
    updated_at              = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="records")


# ── 3. 株価履歴（日次OHLCV） ───────────────────────────────────────────────

class StockPriceHistory(Base):
    __tablename__ = "stock_price_history"
    __table_args__ = (
        PrimaryKeyConstraint("edinet_code", "trade_date", name="uq_sph_edinet_date"),
        Index("ix_sph_sec_date", "sec_code", "trade_date"),
    )

    edinet_code = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    sec_code    = Column(String(6),  nullable=False)
    trade_date  = Column(String(10), nullable=False)   # "YYYY-MM-DD"
    open        = Column(Float)
    high        = Column(Float)
    low         = Column(Float)
    close       = Column(Float, nullable=False)
    volume      = Column(Float)
    created_at  = Column(DateTime, default=datetime.utcnow)


# ── 4. 収集ジョブログ ──────────────────────────────────────────────────────

class CollectionLog(Base):
    __tablename__ = "collection_logs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    job_type     = Column(String(50))          # full / incremental / single
    status       = Column(String(20))          # running / done / error
    started_at   = Column(DateTime, default=datetime.utcnow)
    finished_at  = Column(DateTime)
    companies_processed = Column(Integer, default=0)
    records_saved       = Column(Integer, default=0)
    errors_count        = Column(Integer, default=0)
    message      = Column(Text)


# ── 5. マクロデータ（為替・金利・指数・コモディティ） ──────────────────────

class MacroData(Base):
    __tablename__ = "macro_data"
    __table_args__ = (
        UniqueConstraint("series_code", "trade_date", name="uq_macro_series_date"),
        Index("ix_macro_series_date", "series_code", "trade_date"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    series_code = Column(String(20), nullable=False)  # "USDJPY" / "TNX10Y" / "NIKKEI225" 等
    series_name = Column(String(50))                  # 表示名（"USD/JPY"・"米10年金利" 等）
    category    = Column(String(20))                  # "fx" / "rate" / "equity" / "commodity"
    trade_date  = Column(String(10), nullable=False)  # "YYYY-MM-DD"
    open        = Column(Float)
    high        = Column(Float)
    low         = Column(Float)
    close       = Column(Float, nullable=False)
    volume      = Column(Float)
    created_at  = Column(DateTime, default=datetime.utcnow)


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
    period_end      = Column(String(20))
    elements_gz     = Column(LargeBinary, nullable=False)
    elements_format = Column(String(10), default="gzip+json")
    n_rows          = Column(Integer)
    fetched_at      = Column(DateTime, default=datetime.utcnow)


def pack_elements(rows: list) -> bytes:
    """[{element, context, value}, ...] を gzip(JSON) に圧縮"""
    return gzip.compress(json.dumps(rows, ensure_ascii=False).encode("utf-8"))


def unpack_elements(blob: bytes) -> list:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def upsert_xbrl_raw(db, doc_id: str, edinet_code: str, period_end: str, rows: list):
    blob = pack_elements(rows)
    now  = datetime.utcnow()
    stmt = pg_insert(XbrlRawDocument).values(
        doc_id=doc_id, edinet_code=edinet_code, period_end=period_end,
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
    period_end           = Column(String(20), nullable=False, default="")
    predicted_market_cap = Column(Float)   # 回帰モデル予測時価総額（百万円）
    gap_ratio            = Column(Float)   # 乖離率 %（(予測-実績)/実績*100）
    model                = Column(String(20))   # "ols" / "ridge"
    sector               = Column(String(100))  # 学習に使った業種
    computed_at          = Column(DateTime, default=datetime.utcnow,
                                  onupdate=datetime.utcnow)


def upsert_regression_result(db, *, edinet_code: str, year: int, period_end: str,
                             predicted_market_cap, gap_ratio, model: str, sector: str):
    """OLS/Ridge の予測値を regression_results に upsert する。

    主キー (edinet_code, year, period_end) で merge するため PostgreSQL / SQLite の
    どちらでも動作する（pg_insert の ON CONFLICT は Postgres 専用のため使わない）。
    """
    db.merge(RegressionResult(
        edinet_code=edinet_code, year=year, period_end=period_end or "",
        predicted_market_cap=predicted_market_cap, gap_ratio=gap_ratio,
        model=model, sector=sector, computed_at=datetime.utcnow(),
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
    period_end   = Column(String(20))
    doc_id       = Column(String(20))
    source       = Column(String(50))
    accounting_standard = Column(String(20))
    # ソース（financial_records からそのまま）
    bs_total_assets = Column(Float); bs_current_assets = Column(Float)
    bs_receivables = Column(Float); bs_inventory = Column(Float)
    bs_noncurrent_assets = Column(Float); bs_buildings = Column(Float)
    bs_machinery = Column(Float); bs_intangible_assets = Column(Float)
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
    cf_operating_cf = Column(Float); cf_investing_cf = Column(Float)
    cf_financing_cf = Column(Float); cf_free_cf = Column(Float)
    cf_net_change_cash = Column(Float); cf_capex = Column(Float)
    stock_price = Column(Float); market_cap = Column(Float)
    per = Column(Float); pbr = Column(Float); div_yield = Column(Float); dps = Column(Float)
    # 軽い派生（VIEW が都度算出）
    op_margin = Column(Float); net_margin = Column(Float)
    roe = Column(Float); roa = Column(Float)
    equity_ratio = Column(Float); de_ratio = Column(Float); cf_ratio = Column(Float)
    net_cash = Column(Float); nc_ratio = Column(Float)
    z_revenue = Column(Float); z_op_margin = Column(Float); z_roe = Column(Float)
    z_equity_ratio = Column(Float); z_cf_ratio = Column(Float); z_eps = Column(Float)
    z_de_ratio = Column(Float); z_nc_ratio = Column(Float)
    rev_growth = Column(Float); op_growth = Column(Float); eps_growth = Column(Float)
    # 回帰出力（regression_results を LEFT JOIN）
    predicted_market_cap = Column(Float); gap_ratio = Column(Float)


# financial_metrics VIEW DDL。式は既存 Python 実装と一致させてある。
FINANCIAL_METRICS_VIEW_SQL = """
CREATE OR REPLACE VIEW financial_metrics AS
WITH d AS (
    SELECT
        fr.id, fr.edinet_code, fr.sec_code, fr.company_name, fr.industry, fr.market,
        fr.year, fr.period_end, fr.doc_id, fr.source, fr.accounting_standard,
        fr.bs_total_assets, fr.bs_current_assets, fr.bs_receivables, fr.bs_inventory,
        fr.bs_noncurrent_assets, fr.bs_buildings, fr.bs_machinery, fr.bs_intangible_assets,
        fr.bs_cash, fr.bs_investment_securities, fr.bs_total_liabilities, fr.bs_current_liabilities,
        fr.bs_payables, fr.bs_noncurrent_liabilities, fr.bs_short_term_debt, fr.bs_long_term_debt,
        fr.bs_bonds_payable, fr.bs_total_equity, fr.bs_equity_parent, fr.bs_paid_in_capital,
        fr.bs_retained_earnings, fr.bs_bps,
        fr.pl_revenue, fr.pl_cost_of_sales, fr.pl_gross_profit, fr.pl_sga, fr.pl_operating_profit,
        fr.pl_nonoperating_income, fr.pl_ordinary_profit, fr.pl_pretax_profit, fr.pl_net_income,
        fr.pl_net_income_attr, fr.pl_eps, fr.pl_ebitda,
        fr.cf_operating_cf, fr.cf_investing_cf, fr.cf_financing_cf, fr.cf_free_cf,
        fr.cf_net_change_cash, fr.cf_capex,
        fr.stock_price, fr.market_cap, fr.per, fr.pbr, fr.div_yield, fr.dps,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((COALESCE(fr.pl_operating_profit,0) / fr.pl_revenue * 100)::numeric, 2) END AS op_margin,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0), 0) / fr.pl_revenue * 100)::numeric, 2) END AS net_margin,
        CASE WHEN COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0), 0) / COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) * 100)::numeric, 2) END AS roe,
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.pl_net_income,0), NULLIF(fr.pl_net_income_attr,0), 0) / fr.bs_total_assets * 100)::numeric, 2) END AS roa,
        CASE WHEN COALESCE(fr.bs_total_assets,0) <> 0
             THEN ROUND((COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) / fr.bs_total_assets * 100)::numeric, 2) END AS equity_ratio,
        CASE WHEN COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0) <> 0
             THEN ROUND(((COALESCE(fr.bs_short_term_debt,0) + COALESCE(fr.bs_long_term_debt,0)) / COALESCE(NULLIF(fr.bs_total_equity,0), NULLIF(fr.bs_equity_parent,0), 0))::numeric, 4) END AS de_ratio,
        CASE WHEN COALESCE(fr.pl_revenue,0) <> 0
             THEN ROUND((COALESCE(fr.cf_operating_cf,0) / fr.pl_revenue * 100)::numeric, 2) END AS cf_ratio,
        CASE WHEN COALESCE(fr.bs_current_assets,0) <> 0 OR COALESCE(fr.bs_total_liabilities,0) <> 0
             THEN ROUND((COALESCE(fr.bs_current_assets,0) + COALESCE(fr.bs_investment_securities,0) * 0.7 - COALESCE(fr.bs_total_liabilities,0))::numeric, 0) END AS net_cash
    FROM financial_records fr
),
n AS (
    SELECT d.*,
        CASE WHEN d.net_cash IS NOT NULL AND COALESCE(d.market_cap,0) <> 0
             THEN ROUND((d.net_cash / (d.market_cap * 1000000))::numeric, 4) END AS nc_ratio
    FROM d
)
SELECT
    n.*,
    CASE WHEN COUNT(n.pl_revenue) OVER yw >= 2
         THEN ROUND(((n.pl_revenue - AVG(n.pl_revenue) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.pl_revenue) OVER yw, 0), 1.0))::numeric, 4) END AS z_revenue,
    CASE WHEN COUNT(n.op_margin) OVER yw >= 2
         THEN ROUND(((n.op_margin - AVG(n.op_margin) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.op_margin) OVER yw, 0), 1.0))::numeric, 4) END AS z_op_margin,
    CASE WHEN COUNT(n.roe) OVER yw >= 2
         THEN ROUND(((n.roe - AVG(n.roe) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.roe) OVER yw, 0), 1.0))::numeric, 4) END AS z_roe,
    CASE WHEN COUNT(n.equity_ratio) OVER yw >= 2
         THEN ROUND(((n.equity_ratio - AVG(n.equity_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.equity_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_equity_ratio,
    CASE WHEN COUNT(n.cf_ratio) OVER yw >= 2
         THEN ROUND(((n.cf_ratio - AVG(n.cf_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.cf_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_cf_ratio,
    CASE WHEN COUNT(n.pl_eps) OVER yw >= 2
         THEN ROUND(((n.pl_eps - AVG(n.pl_eps) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.pl_eps) OVER yw, 0), 1.0))::numeric, 4) END AS z_eps,
    CASE WHEN COUNT(n.de_ratio) OVER yw >= 2
         THEN ROUND(((n.de_ratio - AVG(n.de_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.de_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_de_ratio,
    CASE WHEN COUNT(n.nc_ratio) OVER yw >= 2
         THEN ROUND(((n.nc_ratio - AVG(n.nc_ratio) OVER yw) / COALESCE(NULLIF(STDDEV_SAMP(n.nc_ratio) OVER yw, 0), 1.0))::numeric, 4) END AS z_nc_ratio,
    CASE WHEN n.pl_revenue IS NOT NULL AND n.pl_revenue <> 0
          AND LAG(n.pl_revenue) OVER cw IS NOT NULL AND LAG(n.pl_revenue) OVER cw <> 0
         THEN ROUND(((n.pl_revenue / LAG(n.pl_revenue) OVER cw - 1) * 100)::numeric, 2) END AS rev_growth,
    CASE WHEN n.pl_operating_profit IS NOT NULL AND n.pl_operating_profit <> 0
          AND LAG(n.pl_operating_profit) OVER cw IS NOT NULL AND LAG(n.pl_operating_profit) OVER cw <> 0
         THEN ROUND(((n.pl_operating_profit / LAG(n.pl_operating_profit) OVER cw - 1) * 100)::numeric, 2) END AS op_growth,
    CASE WHEN n.pl_eps IS NOT NULL AND n.pl_eps <> 0
          AND LAG(n.pl_eps) OVER cw IS NOT NULL AND LAG(n.pl_eps) OVER cw <> 0
         THEN ROUND(((n.pl_eps / LAG(n.pl_eps) OVER cw - 1) * 100)::numeric, 2) END AS eps_growth,
    rr.predicted_market_cap,
    rr.gap_ratio
FROM n
LEFT JOIN regression_results rr
       ON rr.edinet_code = n.edinet_code AND rr.year = n.year AND rr.period_end = n.period_end
WINDOW yw AS (PARTITION BY n.year),
       cw AS (PARTITION BY n.edinet_code ORDER BY n.year, n.period_end)
"""


# ── 10. DB初期化 ───────────────────────────────────────────────────────────

def init_db():
    """テーブル作成・インデックス構築・カラムマイグレーション"""
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # 全文検索インデックス
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_companies_name_gin "
            "ON companies USING gin(to_tsvector('simple', name))"
        ))
        # 新規ソース列のマイグレーション（冪等）。計算列はここに含めない（VIEW が担う）。
        _new_cols = [
            "pl_cost_of_sales", "pl_sga", "pl_nonoperating_income",
            "bs_receivables", "bs_inventory",
            "bs_buildings", "bs_machinery", "bs_intangible_assets",
            "bs_payables", "bs_bonds_payable",
            "bs_paid_in_capital", "bs_retained_earnings",
            "bs_investment_securities",
        ]
        for col in _new_cols:
            conn.execute(text(
                f"ALTER TABLE financial_records ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION"
            ))
        # 旧計算列の DROP（冪等）。派生指標は financial_metrics VIEW、OLS予測値は
        # regression_results へ移行済みのため financial_records からは恒久的に削除する。
        # VIEW はソース列から派生を計算しており、これらの列を参照しないため DROP 可能。
        _legacy_computed_cols = [
            "op_margin", "net_margin", "roe", "roa", "equity_ratio", "de_ratio",
            "cf_ratio", "net_cash", "nc_ratio",
            "z_revenue", "z_op_margin", "z_roe", "z_equity_ratio", "z_cf_ratio",
            "z_eps", "z_de_ratio", "z_nc_ratio",
            "rev_growth", "op_growth", "eps_growth",
            "predicted_market_cap", "gap_ratio",
        ]
        for col in _legacy_computed_cols:
            conn.execute(text(
                f"ALTER TABLE financial_records DROP COLUMN IF EXISTS {col}"
            ))
        # xbrl_raw_documents インデックス（テーブル自体は create_all で作成済み）
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_xbrl_raw_edinet_period "
            "ON xbrl_raw_documents (edinet_code, period_end)"
        ))
        # financial_metrics VIEW（ソース列から軽い派生を都度算出＋regression_results を合成）。
        # regression_results は create_all で先に作成済みのため LEFT JOIN 可能。
        conn.execute(text(FINANCIAL_METRICS_VIEW_SQL))
        conn.commit()


# ── 7. Upsert 処理 ─────────────────────────────────────────────────────────

def upsert_company(db, data: dict) -> Company:
    obj = db.query(Company).filter_by(edinet_code=data["edinet_code"]).first()
    if obj is None:
        obj = Company(**{k: v for k, v in data.items() if hasattr(Company, k)})
        db.add(obj)
        obj.updated_at = datetime.utcnow()
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
        obj.updated_at = datetime.utcnow()
    return obj


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
        "period_end":         data.get("period_end"),
        "doc_id":             data.get("doc_id"),
        "source":             data.get("source", "EDINET_XBRL"),
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

    # 生データ保存
    flat["raw_xbrl_json"] = {
        "bs": data.get("bs", {}),
        "pl": data.get("pl", {}),
        "cf": data.get("cf", {}),
    }

    obj = db.query(FinancialRecord).filter_by(
        edinet_code=flat["edinet_code"],
        year=flat["year"],
        period_end=flat.get("period_end", ""),
    ).first()

    if obj is None:
        valid = {k: v for k, v in flat.items() if hasattr(FinancialRecord, k)}
        obj = FinancialRecord(**valid)
        db.add(obj)
        db.flush()  # autoflush=False のため明示的にフラッシュ（同一セッション内の重複を防ぐ）
    else:
        for k, v in flat.items():
            if hasattr(FinancialRecord, k) and v is not None:
                setattr(obj, k, v)
    obj.updated_at = datetime.utcnow()
    return obj


# 成長率・Zスコアの事前計算関数（calc_growth_rates / calc_zscore_normalization /
# _calc_zscore_for_year）は廃止した。これらは financial_records の計算列へ書き戻す実装
# だったが、派生指標は financial_metrics VIEW がソース列から都度算出する方式へ移行済み
# （計算結果と生データのDB分離）。算出ロジックは FINANCIAL_METRICS_VIEW_SQL を参照。
