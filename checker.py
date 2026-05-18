"""
データ品質チェッカー
収集済み財務データの NULL 率・外れ値・年度連続性を検査してレポートを返す。
"""

from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from database import FinancialRecord


def run_data_quality_check(db: Session) -> dict:
    return {
        "null_fields":  _check_null_fields(db),
        "outliers":     _check_outliers(db),
        "year_summary": _check_year_summary(db),
        "checked_at":   datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _check_null_fields(db: Session) -> list:
    total = db.query(func.count(FinancialRecord.id)).scalar() or 1
    fields = [
        ("pl_revenue",          "売上高"),
        ("pl_operating_profit", "営業利益"),
        ("pl_net_income",       "純利益"),
        ("bs_total_assets",     "総資産"),
        ("bs_total_equity",     "純資産"),
        ("pl_eps",              "EPS"),
        ("bs_bps",              "BPS"),
        ("cf_operating_cf",     "営業CF"),
    ]
    result = []
    for col, label in fields:
        null_count = (
            db.query(func.count(FinancialRecord.id))
            .filter(getattr(FinancialRecord, col).is_(None))
            .scalar()
        )
        result.append({
            "field":      col,
            "label":      label,
            "null_count": null_count,
            "null_pct":   round(null_count / total * 100, 1),
        })
    return result


def _check_outliers(db: Session) -> list:
    issues = []

    checks = [
        ("ROE絶対値 > 1000%",       FinancialRecord.roe,             lambda c: func.abs(c) > 1000),
        ("負の売上高",               FinancialRecord.pl_revenue,      lambda c: c < 0),
        ("PBR < 0 または > 500",    FinancialRecord.pbr,             lambda c: (c < 0) | (c > 500)),
        ("PER < 0 または > 5000",   FinancialRecord.per,             lambda c: (c < 0) | (c > 5000)),
        ("自己資本比率 < -100%",     FinancialRecord.equity_ratio,    lambda c: c < -100),
    ]
    for label, col, cond_fn in checks:
        count = (
            db.query(func.count())
            .filter(col.isnot(None), cond_fn(col))
            .scalar()
        )
        if count:
            issues.append({"label": label, "count": count})

    return issues


def _check_year_summary(db: Session) -> dict:
    total = db.query(FinancialRecord.edinet_code).distinct().count()

    single = (
        db.query(FinancialRecord.edinet_code)
        .group_by(FinancialRecord.edinet_code)
        .having(func.count(FinancialRecord.year) == 1)
        .count()
    )
    multi = (
        db.query(FinancialRecord.edinet_code)
        .group_by(FinancialRecord.edinet_code)
        .having(func.count(FinancialRecord.year) >= 3)
        .count()
    )
    no_market = (
        db.query(FinancialRecord.edinet_code)
        .filter(FinancialRecord.market_cap.is_(None))
        .distinct()
        .count()
    )
    return {
        "total_companies":     total,
        "single_year_only":    single,
        "three_or_more_years": multi,
        "no_market_data":      no_market,
    }
