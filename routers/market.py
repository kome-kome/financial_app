"""市場データ・財務データ・スクリーニング・DBビューア API ルーター。

/api/stock/*, /api/macro/*, /api/stats, /api/companies,
/api/financials/*, /api/screen, /api/db/*, /api/export/csv を担当。
"""
import csv
import io
import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session

import api
import serializers
from database import (
    Company, FinancialRecord, FinancialMetric, RegressionResult,
    StockPriceDaily, StockPriceWeekly, MacroData, CollectionLog,
    latest_year_subq,
)
from collector import MACRO_SERIES

router = APIRouter()
log = logging.getLogger(__name__)


# ── DB ビューア設定 ─────────────────────────────────────────────────────────

_DB_VIEWER_TABLES = {
    "companies":           Company,
    "financial_records":   FinancialRecord,
    "stock_price_daily":   StockPriceDaily,
    "stock_price_weekly":  StockPriceWeekly,
    "macro_data":          MacroData,
    "collection_logs":     CollectionLog,
}

_DB_VIEWER_RELATIONS = [
    ("financial_records",  "edinet_code", "companies", "edinet_code", "1社:N年度"),
    ("stock_price_daily",  "edinet_code", "companies", "edinet_code", "1社:N日次(直近)"),
    ("stock_price_weekly", "edinet_code", "companies", "edinet_code", "1社:N週次(全期間)"),
]


def _column_meta(col):
    """SQLAlchemy カラム→ {name, type, nullable, pk, fk, numeric} の辞書"""
    py_type = getattr(col.type, "python_type", None)
    try:
        py_name = py_type.__name__ if py_type else str(col.type)
    except NotImplementedError:
        py_name = str(col.type)
    is_numeric = py_name in ("int", "float")
    fks = [f"{fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys]
    return {
        "name":     col.name,
        "type":     py_name,
        "nullable": bool(col.nullable),
        "pk":       bool(col.primary_key),
        "fk":       fks[0] if fks else None,
        "numeric":  is_numeric,
    }


def _normalize_row(row) -> dict:
    """SQLAlchemy 行 → JSON 可能な dict（datetime/dict は文字列化）"""
    out = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name)
        if isinstance(v, datetime):
            v = v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)[:200]
        out[col.name] = v
    return out


# ── 株価履歴 ─────────────────────────────────────────────────────────────

@router.get("/api/stock/history/{edinet_code}")
async def get_stock_history(
    edinet_code: str, days: int = 365,
    resolution: str = "daily",
    db: Session = Depends(api.get_db),
):
    """指定企業の終値時系列を返す（close-only）。
    resolution=daily : 直近 days 日の日次終値
    resolution=weekly: 全履歴の週次終値（全期間表示・長期用）
    """
    if not api._EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days は 1〜3650 の範囲で指定してください")
    if resolution not in ("daily", "weekly"):
        raise HTTPException(400, "resolution は daily / weekly のいずれか")

    if resolution == "weekly":
        rows = (
            db.query(StockPriceWeekly.trade_date, StockPriceWeekly.close_last)
            .filter(StockPriceWeekly.edinet_code == edinet_code)
            .order_by(StockPriceWeekly.trade_date.desc())
            .limit(days)
            .all()
        )
    else:
        rows = (
            db.query(StockPriceDaily.trade_date, StockPriceDaily.close)
            .filter(StockPriceDaily.edinet_code == edinet_code)
            .order_by(StockPriceDaily.trade_date.desc())
            .limit(days)
            .all()
        )
    return [{"trade_date": r[0], "close": r[1]} for r in reversed(rows)]


# ── マクロデータ ──────────────────────────────────────────────────────────

@router.get("/api/macro/series")
async def list_macro_series(db: Session = Depends(api.get_db)):
    """マクロ系列のカバレッジ一覧（系列ごとの件数・最新日・最古日）"""
    rows = (
        db.query(
            MacroData.series_code,
            MacroData.series_name,
            MacroData.category,
            func.count(MacroData.id).label("rows"),
            func.min(MacroData.trade_date).label("oldest"),
            func.max(MacroData.trade_date).label("newest"),
        )
        .group_by(MacroData.series_code, MacroData.series_name, MacroData.category)
        .all()
    )
    by_code = {r.series_code: r for r in rows}
    items = []
    for s in MACRO_SERIES:
        r = by_code.get(s["code"])
        items.append({
            "code":     s["code"],
            "name":     s["name"],
            "category": s["category"],
            "ticker":   s["ticker"],
            "rows":     int(r.rows) if r else 0,
            "oldest":   r.oldest if r else None,
            "newest":   r.newest if r else None,
        })
    return {"series": items}


@router.get("/api/macro/data/{series_code}")
async def get_macro_data(series_code: str, days: int = 365, db: Session = Depends(api.get_db)):
    """指定系列の日次データを最新 days 日分返す"""
    if series_code not in {s["code"] for s in MACRO_SERIES}:
        raise HTTPException(404, "未知の系列コードです")
    if not (1 <= days <= 10000):
        raise HTTPException(400, "days は 1〜10000 の範囲で指定してください")
    rows = (
        db.query(MacroData)
        .filter(MacroData.series_code == series_code)
        .order_by(MacroData.trade_date.desc())
        .limit(days)
        .all()
    )
    return {
        "series_code": series_code,
        "rows": [
            {"trade_date": r.trade_date, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in reversed(rows)
        ],
    }


# ── 統計サマリー ──────────────────────────────────────────────────────────

@router.get("/api/stats")
async def get_stats(db: Session = Depends(api.get_db)):
    from datetime import date, timezone as _tz
    n_companies   = db.query(Company).count()
    n_records     = db.query(FinancialRecord).count()
    n_stock_price = db.query(func.count(StockPriceWeekly.edinet_code)).scalar() or 0
    n_predicted = (
        db.query(func.count(RegressionResult.edinet_code))
        .filter(RegressionResult.gap_ratio.isnot(None))
        .scalar()
    ) or 0

    latest_fr = (
        db.query(FinancialRecord.year, FinancialRecord.period_end, FinancialRecord.updated_at)
        .order_by(FinancialRecord.year.desc(), FinancialRecord.period_end.desc())
        .first()
    )
    last_db_update = db.query(func.max(FinancialRecord.updated_at)).scalar()

    today = date.today()
    expected_year = today.year if today.month >= 7 else today.year - 1

    days_since: Optional[int] = None
    if last_db_update:
        _ref = last_db_update if last_db_update.tzinfo else last_db_update.replace(tzinfo=_tz.utc)
        days_since = (datetime.now(_tz.utc) - _ref).days
    if days_since is None:
        freshness = "empty"
    elif days_since <= 2:
        freshness = "fresh"
    elif days_since <= 14:
        freshness = "ok"
    elif days_since <= 60:
        freshness = "stale"
    else:
        freshness = "outdated"

    return {
        "companies":            n_companies,
        "records":              n_records,
        "stock_price_records":  n_stock_price,
        "records_with_prediction": n_predicted,
        "latest_year":          latest_fr.year       if latest_fr else None,
        "latest_period_end":    str(latest_fr.period_end) if latest_fr and latest_fr.period_end else None,
        "last_db_update":       api._utc_to_jst_str(last_db_update),
        "days_since_update":    days_since,
        "expected_latest_year": expected_year,
        "freshness":            freshness,
    }


# ── 企業検索 ────────────────────────────────────────────────────────────

@router.get("/api/companies")
async def list_companies(
    q: Optional[str] = None,
    industry: Optional[str] = None,
    market: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    include_latest: bool = False,
    db: Session = Depends(api.get_db),
):
    if not (1 <= limit <= 500):
        raise HTTPException(400, "limit は 1〜500 の範囲で指定してください")
    if offset < 0:
        raise HTTPException(400, "offset は 0 以上で指定してください")
    query = db.query(Company)
    if q:
        query = query.filter(Company.name.ilike(f"%{q}%") | Company.sec_code.ilike(f"%{q}%"))
    if industry:
        query = query.filter(Company.industry == industry)
    if market:
        query = query.filter(Company.market == market)
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [{"edinet_code": c.edinet_code, "sec_code": c.sec_code,
              "name": c.name, "industry": c.industry, "market": c.market}
             for c in rows]
    if include_latest and rows:
        codes = [c.edinet_code for c in rows]
        subq = (
            db.query(FinancialRecord.edinet_code, func.max(FinancialRecord.year).label("max_year"))
            .filter(FinancialRecord.edinet_code.in_(codes))
            .group_by(FinancialRecord.edinet_code)
            .subquery()
        )
        latest_recs = (
            db.query(FinancialMetric)
            .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                        (FinancialMetric.year == subq.c.max_year))
            .all()
        )
        latest_map = {r.edinet_code: serializers.record_to_dict(r) for r in latest_recs}
        for item in items:
            item["latest"] = latest_map.get(item["edinet_code"])
    return {"total": total, "items": items}


# ── 財務データ取得 ─────────────────────────────────────────────────────────

@router.get("/api/financials/{edinet_code}")
async def get_financials(edinet_code: str, db: Session = Depends(api.get_db)):
    if not api._EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    records = (db.query(FinancialMetric)
               .filter_by(edinet_code=edinet_code)
               .order_by(FinancialMetric.year)
               .all())
    if not records:
        raise HTTPException(404, "データが見つかりません")
    return {"edinet_code": edinet_code, "records": [serializers.record_to_dict(r) for r in records]}


# ── スクリーニング ─────────────────────────────────────────────────────────

class ScreenRequest(BaseModel):
    year: Optional[int] = None
    industry: Optional[str] = None
    market: Optional[str] = None
    min_rev_growth: Optional[float] = None
    min_op_margin: Optional[float] = None
    min_net_margin: Optional[float] = None
    min_roe: Optional[float] = None
    min_roa: Optional[float] = None
    min_equity_ratio: Optional[float] = None
    max_de_ratio: Optional[float] = None
    max_per: Optional[float] = None
    max_pbr: Optional[float] = None
    min_div_yield: Optional[float] = None
    min_cf_ratio: Optional[float] = None
    limit: int = Field(default=200, ge=1, le=500)


@router.post("/api/screen")
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def screening(request: Request, req: ScreenRequest, db: Session = Depends(api.get_db)):
    subq = latest_year_subq(db, FinancialRecord)
    query = (db.query(FinancialMetric)
               .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                           (FinancialMetric.year == subq.c.max_year)))

    if req.year:
        query = query.filter(FinancialMetric.year == req.year)
    if req.industry:
        query = query.filter(FinancialMetric.industry == req.industry)
    if req.market:
        query = query.filter(FinancialMetric.market == req.market)
    if req.min_rev_growth is not None:
        query = query.filter(FinancialMetric.rev_growth >= req.min_rev_growth)
    if req.min_op_margin is not None:
        query = query.filter(FinancialMetric.op_margin >= req.min_op_margin)
    if req.min_net_margin is not None:
        query = query.filter(FinancialMetric.net_margin >= req.min_net_margin)
    if req.min_roe is not None:
        query = query.filter(FinancialMetric.roe >= req.min_roe)
    if req.min_roa is not None:
        query = query.filter(FinancialMetric.roa >= req.min_roa)
    if req.min_equity_ratio is not None:
        query = query.filter(FinancialMetric.equity_ratio >= req.min_equity_ratio)
    if req.max_de_ratio is not None:
        query = query.filter(FinancialMetric.de_ratio <= req.max_de_ratio)
    if req.max_per is not None:
        query = query.filter(FinancialMetric.per <= req.max_per)
    if req.max_pbr is not None:
        query = query.filter(FinancialMetric.pbr <= req.max_pbr)
    if req.min_div_yield is not None:
        query = query.filter(FinancialMetric.div_yield >= req.min_div_yield)
    if req.min_cf_ratio is not None:
        query = query.filter(FinancialMetric.cf_ratio >= req.min_cf_ratio)

    rows = query.limit(req.limit).all()
    return {"count": len(rows), "results": [serializers.record_to_dict(r) for r in rows]}


# ── DB ビューア ─────────────────────────────────────────────────────────────

@router.get("/api/db/tables")
async def db_tables(db: Session = Depends(api.get_db)):
    items = []
    for name, model in _DB_VIEWER_TABLES.items():
        row_count = db.query(func.count()).select_from(model).scalar() or 0
        cols = list(model.__table__.columns)
        last_updated = None
        if hasattr(model, "updated_at"):
            last_updated = db.query(func.max(model.updated_at)).scalar()
        elif hasattr(model, "created_at"):
            last_updated = db.query(func.max(model.created_at)).scalar()
        items.append({
            "name":         name,
            "row_count":    row_count,
            "column_count": len(cols),
            "last_updated": api._utc_to_jst_str(last_updated),
        })
    return {"tables": items}


@router.get("/api/db/schema/{table}")
async def db_schema(table: str, db: Session = Depends(api.get_db)):
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    model = _DB_VIEWER_TABLES[table]
    row_count = db.query(func.count()).select_from(model).scalar() or 0
    table_cols = list(model.__table__.columns)
    null_counts: dict[str, int] = {}
    if row_count > 0:
        null_exprs = [
            func.count().filter(col.is_(None)).label(col.name)
            for col in table_cols
        ]
        row = db.query(*null_exprs).select_from(model).first()
        null_counts = {col.name: (getattr(row, col.name) or 0) for col in table_cols}

    cols = []
    for col in table_cols:
        meta = _column_meta(col)
        if row_count > 0:
            null_count = null_counts.get(col.name, 0)
            meta["null_rate"] = round(null_count / row_count * 100, 1)
            meta["null_count"] = null_count
        else:
            meta["null_rate"] = None
            meta["null_count"] = 0
        cols.append(meta)
    return {"table": table, "row_count": row_count, "columns": cols}


@router.get("/api/db/preview/{table}")
async def db_preview(
    table: str,
    limit:      int = 50,
    offset:     int = 0,
    sort:       Optional[str] = None,
    order:      str = "desc",
    filter_col: Optional[str] = None,
    filter_val: Optional[str] = None,
    db: Session = Depends(api.get_db),
):
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    if not (1 <= limit <= 500):
        raise HTTPException(400, "limit は 1〜500 の範囲で指定してください")
    if offset < 0:
        raise HTTPException(400, "offset は 0 以上で指定してください")
    if order not in ("asc", "desc"):
        raise HTTPException(400, "order は asc / desc のいずれか")

    model   = _DB_VIEWER_TABLES[table]
    col_map = {c.name: c for c in model.__table__.columns}
    query   = db.query(model)

    if filter_col and filter_val:
        if filter_col not in col_map:
            raise HTTPException(400, "filter_col が不正です")
        col_meta = _column_meta(col_map[filter_col])
        typed_val: Any = filter_val
        if col_meta["numeric"]:
            try:
                typed_val = int(filter_val) if col_meta["type"] == "int" else float(filter_val)
            except ValueError:
                raise HTTPException(400, f"filter_val は数値カラム {filter_col} に変換できません")
        query = query.filter(col_map[filter_col] == typed_val)
    total = query.count()

    if sort:
        if sort not in col_map:
            raise HTTPException(400, "sort カラムが不正です")
        sort_col = col_map[sort]
        query = query.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    else:
        pk_cols = [c for c in model.__table__.columns if c.primary_key]
        if pk_cols:
            query = query.order_by(pk_cols[0].desc())

    rows = query.offset(offset).limit(limit).all()
    return {
        "table":   table,
        "total":   total,
        "limit":   limit,
        "offset":  offset,
        "columns": [c.name for c in model.__table__.columns],
        "rows":    [_normalize_row(r) for r in rows],
    }


@router.get("/api/db/stats/{table}")
async def db_stats(table: str, db: Session = Depends(api.get_db)):
    """テーブルの統計サマリー（数値カラムは min/max/avg/p50/p99、文字列カラムはユニーク数）"""
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    model = _DB_VIEWER_TABLES[table]
    row_count = db.query(func.count()).select_from(model).scalar() or 0

    all_cols   = list(model.__table__.columns)
    col_metas  = {col.name: _column_meta(col) for col in all_cols}
    num_cols   = [col for col in all_cols if col_metas[col.name]["numeric"]]
    str_cols   = [col for col in all_cols if not col_metas[col.name]["numeric"]]

    num_agg: dict = {}
    if row_count > 0 and num_cols:
        exprs = []
        for col in num_cols:
            exprs += [
                func.min(col).label(f"{col.name}__min"),
                func.max(col).label(f"{col.name}__max"),
                func.avg(col).label(f"{col.name}__avg"),
                func.count(col).label(f"{col.name}__cnt"),
            ]
        row = db.query(*exprs).select_from(model).first()
        for col in num_cols:
            num_agg[col.name] = {
                "min":   getattr(row, f"{col.name}__min"),
                "max":   getattr(row, f"{col.name}__max"),
                "avg":   getattr(row, f"{col.name}__avg"),
                "count": getattr(row, f"{col.name}__cnt"),
            }

    pct_agg: dict = {}
    if row_count > 0 and num_cols:
        try:
            table_name = model.__tablename__
            col_names = {c.name for c in model.__table__.columns}
            select_parts = ", ".join(
                f'percentile_cont(0.5) WITHIN GROUP (ORDER BY "{c.name}") AS "{c.name}__p50", '
                f'percentile_cont(0.99) WITHIN GROUP (ORDER BY "{c.name}") AS "{c.name}__p99"'
                for c in num_cols
                if c.name in col_names
            )
            p = db.execute(text(f'SELECT {select_parts} FROM "{table_name}"')).first()
            for col in num_cols:
                p50 = getattr(p, f"{col.name}__p50", None)
                p99 = getattr(p, f"{col.name}__p99", None)
                pct_agg[col.name] = {
                    "p50": round(float(p50), 4) if p50 is not None else None,
                    "p99": round(float(p99), 4) if p99 is not None else None,
                }
        except Exception as e:
            log.warning("percentile_cont 一括取得失敗 [%s]: %s: %s", table, type(e).__name__, e)

    str_agg: dict = {}
    if row_count > 0 and str_cols:
        try:
            exprs = [
                func.count(func.distinct(col)).label(col.name)
                for col in str_cols
            ]
            row = db.query(*exprs).select_from(model).first()
            for col in str_cols:
                str_agg[col.name] = int(getattr(row, col.name) or 0)
        except Exception as e:
            log.warning("distinct 一括取得失敗 [%s]: %s: %s", table, type(e).__name__, e)

    stats = []
    for col in all_cols:
        meta = col_metas[col.name]
        s = {"name": col.name, "type": meta["type"], "numeric": meta["numeric"]}
        if row_count == 0:
            stats.append(s)
            continue
        if meta["numeric"]:
            agg = num_agg.get(col.name, {})
            mn, mx, avg, cnt = agg.get("min"), agg.get("max"), agg.get("avg"), agg.get("count")
            s["min"]   = float(mn)  if mn  is not None else None
            s["max"]   = float(mx)  if mx  is not None else None
            s["avg"]   = round(float(avg), 4) if avg is not None else None
            s["count"] = int(cnt or 0)
            p = pct_agg.get(col.name, {})
            s["p50"] = p.get("p50")
            s["p99"] = p.get("p99")
        else:
            s["distinct"] = str_agg.get(col.name)
        stats.append(s)
    return {"table": table, "row_count": row_count, "stats": stats}


@router.get("/api/db/relations")
async def db_relations():
    return {
        "tables": [
            {
                "name":    name,
                "columns": [c.name for c in model.__table__.columns],
                "pk":      [c.name for c in model.__table__.columns if c.primary_key],
            }
            for name, model in _DB_VIEWER_TABLES.items()
        ],
        "relations": [
            {
                "from_table":  ft, "from_column": fc,
                "to_table":    tt, "to_column":   tc,
                "label":       lbl,
            }
            for (ft, fc, tt, tc, lbl) in _DB_VIEWER_RELATIONS
        ],
    }


@router.get("/api/db/company/{edinet_code}")
async def db_company_drilldown(edinet_code: str, db: Session = Depends(api.get_db)):
    """企業別ドリルダウン: 1企業に紐づく全テーブルのレコードを横断取得"""
    if not api._EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    company = db.query(Company).filter_by(edinet_code=edinet_code).first()
    if not company:
        raise HTTPException(404, "企業が見つかりません")

    fr_rows = (
        db.query(FinancialRecord)
        .filter_by(edinet_code=edinet_code)
        .order_by(FinancialRecord.year.desc())
        .all()
    )
    sph_count  = db.query(func.count(StockPriceWeekly.edinet_code)).filter_by(edinet_code=edinet_code).scalar() or 0
    sph_oldest = db.query(func.min(StockPriceWeekly.trade_date)).filter_by(edinet_code=edinet_code).scalar()
    sph_newest = db.query(func.max(StockPriceWeekly.trade_date)).filter_by(edinet_code=edinet_code).scalar()
    sph_recent = (
        db.query(StockPriceDaily)
        .filter_by(edinet_code=edinet_code)
        .order_by(StockPriceDaily.trade_date.desc())
        .limit(30).all()
    )

    return {
        "company":           _normalize_row(company),
        "financial_records": [_normalize_row(r) for r in fr_rows],
        "stock_price_history": {
            "total":       sph_count,
            "oldest_date": sph_oldest,
            "newest_date": sph_newest,
            "recent":      [_normalize_row(r) for r in sph_recent],
        },
    }


@router.get("/api/db/export/{table}")
async def db_export_table(
    table: str,
    limit:      int = 10000,
    filter_col: Optional[str] = None,
    filter_val: Optional[str] = None,
    db: Session = Depends(api.get_db),
):
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    if not (1 <= limit <= 100000):
        raise HTTPException(400, "limit は 1〜100000 の範囲で指定してください")

    model   = _DB_VIEWER_TABLES[table]
    col_map = {c.name: c for c in model.__table__.columns}
    cols    = list(model.__table__.columns)

    query = db.query(model)
    if filter_col and filter_val:
        if filter_col not in col_map:
            raise HTTPException(400, "filter_col が不正です")
        col_meta = _column_meta(col_map[filter_col])
        typed_val: Any = filter_val
        if col_meta["numeric"]:
            try:
                typed_val = int(filter_val) if col_meta["type"] == "int" else float(filter_val)
            except ValueError:
                raise HTTPException(400, f"filter_val は数値カラム {filter_col} に変換できません")
        query = query.filter(col_map[filter_col] == typed_val)
    rows = query.limit(limit).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([c.name for c in cols])
    for r in rows:
        row_vals = []
        for c in cols:
            v = getattr(r, c.name)
            if isinstance(v, datetime):
                v = v.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            row_vals.append(v)
        writer.writerow(row_vals)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table}.csv"},
    )


# ── CSV エクスポート ──────────────────────────────────────────────────────

@router.get("/api/export/csv")
async def export_csv(year: Optional[int] = None, db: Session = Depends(api.get_db)):
    query = db.query(FinancialMetric)
    if year:
        query = query.filter_by(year=year)
    records = query.limit(10000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "証券コード", "企業名", "業種", "期", "決算期末",
        "売上高", "営業利益", "純利益", "総資産", "純資産",
        "営業CF", "時価総額", "PER", "PBR", "ROE", "自己資本比率",
        "営業利益率", "純利益率", "D/Eレシオ",
        "予測時価総額", "乖離率%"
    ])
    for r in records:
        writer.writerow([
            r.sec_code, r.company_name, r.industry, r.year, r.period_end.isoformat() if r.period_end else "",
            r.pl_revenue, r.pl_operating_profit, r.pl_net_income,
            r.bs_total_assets, r.bs_total_equity,
            r.cf_operating_cf, r.market_cap, r.per, r.pbr, r.roe, r.equity_ratio,
            r.op_margin, r.net_margin, r.de_ratio,
            r.predicted_market_cap, r.gap_ratio
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=financial_db.csv"}
    )
