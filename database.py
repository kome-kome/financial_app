"""
PostgreSQL スキーマ定義・ORM・upsert処理
テーブル構成:
  companies        — 企業マスタ（EDINETコード・証券コード・業種）
  financial_records — BS/PL/CF 再分類済み年次財務データ
  derived_metrics   — 正規化・計算済み財務指標
  collection_logs   — 収集ジョブログ
"""

import os
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, DateTime,
    Text, UniqueConstraint, Index, JSON, ForeignKey, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

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
    pool_recycle=300,
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

    # ── 計算済み指標（正規化前）──────────────────────────────────────────
    op_margin               = Column(Float)   # 営業利益率 %
    net_margin              = Column(Float)   # 純利益率 %
    roe                     = Column(Float)   # ROE %
    roa                     = Column(Float)   # ROA %
    equity_ratio            = Column(Float)   # 自己資本比率 %
    de_ratio                = Column(Float)   # D/Eレシオ
    cf_ratio                = Column(Float)   # 営業CF/売上比率 %

    # ── 市場データ（株価・バリュエーション）─────────────────────────────
    stock_price             = Column(Float)   # 株価（収集時点）
    market_cap              = Column(Float)   # 時価総額（百万円）
    per                     = Column(Float)   # PER
    pbr                     = Column(Float)   # PBR
    div_yield               = Column(Float)   # 配当利回り %
    dps                     = Column(Float)   # 1株配当

    # ── 正規化済みスコア（Zスコア・業種内偏差）──────────────────────────
    z_revenue               = Column(Float)
    z_op_margin             = Column(Float)
    z_roe                   = Column(Float)
    z_equity_ratio          = Column(Float)
    z_cf_ratio              = Column(Float)
    z_eps                   = Column(Float)
    z_de_ratio              = Column(Float)

    # ── 前期比成長率 ─────────────────────────────────────────────────────
    rev_growth              = Column(Float)   # 売上高成長率 %
    op_growth               = Column(Float)   # 営業利益成長率 %
    eps_growth              = Column(Float)   # EPS成長率 %

    # ── 予測関連 ─────────────────────────────────────────────────────────
    predicted_market_cap    = Column(Float)   # 回帰モデル予測時価総額
    gap_ratio               = Column(Float)   # 乖離率 %

    raw_xbrl_json           = Column(JSON)    # 生XBRLデータ（デバッグ用）
    created_at              = Column(DateTime, default=datetime.utcnow)
    updated_at              = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="records")


# ── 3. 株価履歴（日次OHLCV） ───────────────────────────────────────────────

class StockPriceHistory(Base):
    __tablename__ = "stock_price_history"
    __table_args__ = (
        UniqueConstraint("edinet_code", "trade_date", name="uq_sph_edinet_date"),
        Index("ix_sph_edinet_date", "edinet_code", "trade_date"),
        Index("ix_sph_sec_date",    "sec_code",    "trade_date"),
    )

    id          = Column(Integer, primary_key=True, autoincrement=True)
    edinet_code = Column(String(10), ForeignKey("companies.edinet_code"), nullable=False)
    sec_code    = Column(String(6),  nullable=False, index=True)
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


# ── 5. DB初期化 ────────────────────────────────────────────────────────────

def init_db():
    """テーブル作成・インデックス構築・カラムマイグレーション"""
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # 全文検索インデックス
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_companies_name_gin "
            "ON companies USING gin(to_tsvector('simple', name))"
        ))
        # 新規カラムのマイグレーション（冪等）
        _new_cols = [
            "pl_cost_of_sales", "pl_sga", "pl_nonoperating_income",
            "bs_receivables", "bs_inventory",
            "bs_buildings", "bs_machinery", "bs_intangible_assets",
            "bs_payables", "bs_bonds_payable",
            "bs_paid_in_capital", "bs_retained_earnings",
        ]
        for col in _new_cols:
            conn.execute(text(
                f"ALTER TABLE financial_records ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION"
            ))
        conn.commit()


# ── 6. Upsert 処理 ─────────────────────────────────────────────────────────

def upsert_company(db, data: dict) -> Company:
    obj = db.query(Company).filter_by(edinet_code=data["edinet_code"]).first()
    if obj is None:
        obj = Company(**{k: v for k, v in data.items() if hasattr(Company, k)})
        db.add(obj)
    else:
        for k, v in data.items():
            if hasattr(Company, k) and v is not None:
                setattr(obj, k, v)
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
    # derived
    for k, v in data.get("derived", {}).items():
        flat[k] = v
    # val (market data)
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


def calc_growth_rates(db):
    """前期比成長率を全レコードに対して計算・更新（PostgreSQL window function 版）。

    旧実装は全レコードをメモリに展開してループしていたため、件数が増えると OOM の
    リスクがあった。本実装は LAG() OVER (PARTITION BY edinet_code ORDER BY year,
    period_end) で SQL 側に処理を押し込み、DB の効率的なソート・スキャンを活用する。

    セマンティクス（旧実装との一致点）:
      - 前期と当期の両方が NULL でなく 0 でもないときのみ更新する
      - rev_growth / op_growth / eps_growth は各 % で小数 2 桁丸め
      - 順序は (edinet_code, year, period_end) で副ソート（CLAUDE.md 既知事項）
    """
    sql = text("""
        WITH lagged AS (
            SELECT
                id,
                pl_revenue           AS curr_rev,
                LAG(pl_revenue) OVER w           AS prev_rev,
                pl_operating_profit  AS curr_op,
                LAG(pl_operating_profit) OVER w  AS prev_op,
                pl_eps               AS curr_eps,
                LAG(pl_eps) OVER w               AS prev_eps
            FROM financial_records
            WINDOW w AS (PARTITION BY edinet_code ORDER BY year, period_end)
        )
        UPDATE financial_records fr
        SET
            rev_growth = CASE
                WHEN l.curr_rev IS NOT NULL AND l.curr_rev <> 0
                 AND l.prev_rev IS NOT NULL AND l.prev_rev <> 0
                THEN ROUND(((l.curr_rev / l.prev_rev - 1) * 100)::numeric, 2)
                ELSE fr.rev_growth
            END,
            op_growth = CASE
                WHEN l.curr_op IS NOT NULL AND l.curr_op <> 0
                 AND l.prev_op IS NOT NULL AND l.prev_op <> 0
                THEN ROUND(((l.curr_op / l.prev_op - 1) * 100)::numeric, 2)
                ELSE fr.op_growth
            END,
            eps_growth = CASE
                WHEN l.curr_eps IS NOT NULL AND l.curr_eps <> 0
                 AND l.prev_eps IS NOT NULL AND l.prev_eps <> 0
                THEN ROUND(((l.curr_eps / l.prev_eps - 1) * 100)::numeric, 2)
                ELSE fr.eps_growth
            END
        FROM lagged l
        WHERE fr.id = l.id
    """)
    db.execute(sql)
    db.commit()


def calc_zscore_normalization(db, year: Optional[int] = None):
    """
    Zスコア正規化を年度単位で計算する。
    year 指定時: その年度のみ再計算。
    year 省略時: DB内の全年度を個別に計算（年度内比較が正しくなる）。
    異なる年度（マクロ環境が異なる）を同一母集団にまとめると比較が歪むため
    必ず年度ごとに分けて計算すること。
    """
    import statistics

    if year is None:
        years = [row[0] for row in db.query(FinancialRecord.year).distinct().all()]
        for y in years:
            _calc_zscore_for_year(db, y)
    else:
        _calc_zscore_for_year(db, year)
    db.commit()


def _calc_zscore_for_year(db, year: int):
    """指定年度内でZスコアを計算して書き込む（commitは呼び出し元で行う）"""
    import statistics
    records = db.query(FinancialRecord).filter_by(year=year).all()
    if not records:
        return

    fields = ["pl_revenue", "op_margin", "roe", "equity_ratio", "cf_ratio", "pl_eps", "de_ratio"]
    z_cols  = ["z_revenue",  "z_op_margin", "z_roe", "z_equity_ratio", "z_cf_ratio", "z_eps", "z_de_ratio"]

    for src, dst in zip(fields, z_cols):
        vals = [getattr(r, src) for r in records if getattr(r, src) is not None]
        if len(vals) < 2:
            continue
        mu = statistics.mean(vals)
        sd = statistics.stdev(vals) or 1.0
        for r in records:
            v = getattr(r, src)
            if v is not None:
                setattr(r, dst, round((v - mu) / sd, 4))
