"""
データ品質チェッカー
収集済み財務データの NULL 率・外れ値・年度連続性を検査してレポートを返す。
"""

from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from database import FinancialRecord, FinancialMetric


def run_data_quality_check(db: Session) -> dict:
    return {
        "null_fields":           _check_null_fields(db),
        "outliers":              _check_outliers(db),
        "year_summary":          _check_year_summary(db),
        "accounting_standard":   _check_by_accounting_standard(db),
        "checked_at":            datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
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

    # roe / equity_ratio は計算結果のため financial_metrics VIEW を参照する
    # （financial_records には存在しない）。売上高・PBR・PER はソース列。
    checks = [
        ("ROE絶対値 > 1000%",       FinancialMetric.roe,             lambda c: func.abs(c) > 1000),
        ("負の売上高",               FinancialRecord.pl_revenue,      lambda c: c < 0),
        ("PBR < 0 または > 500",    FinancialRecord.pbr,             lambda c: (c < 0) | (c > 500)),
        ("PER < 0 または > 5000",   FinancialRecord.per,             lambda c: (c < 0) | (c > 5000)),
        ("自己資本比率 < -100%",     FinancialMetric.equity_ratio,    lambda c: c < -100),
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


def _check_by_accounting_standard(db: Session) -> list:
    """会計基準（JGAAP / IFRS / US-GAAP）別に NULL 率・外れ値率を集計する。

    IFRS / JGAAP 混在の影響を可視化することが目的（FUTURE_TASKS Tier 2-C）:
      - bs_total_equity と bs_equity_parent の使い分け差
      - 営業利益・経常利益の定義差
      - 1株指標（pl_eps / bs_bps）の母数算定差
    """
    # 集計対象のフィールドと、外れ値判定条件
    field_specs = [
        ("pl_revenue",          "売上高",   None),
        ("pl_operating_profit", "営業利益", None),
        ("pl_eps",              "EPS",      None),
        ("bs_total_equity",     "純資産",   None),
        ("bs_bps",              "BPS",      None),
        ("cf_operating_cf",     "営業CF",   None),
        ("roe",                 "ROE(%)",   lambda c: func.abs(c) > 1000),
        ("per",                 "PER",      lambda c: (c < 0) | (c > 5000)),
        ("pbr",                 "PBR",      lambda c: (c < 0) | (c > 500)),
    ]

    # 基準ごとのレコード数
    std_counts = dict(
        db.query(
            func.coalesce(FinancialRecord.accounting_standard, "未設定"),
            func.count(FinancialRecord.id),
        )
        .group_by(FinancialRecord.accounting_standard)
        .all()
    )

    result = []
    for standard, total in sorted(std_counts.items()):
        if total == 0:
            continue
        std_filter = (
            FinancialRecord.accounting_standard.is_(None)
            if standard == "未設定"
            else FinancialRecord.accounting_standard == standard
        )
        fields_summary = []
        for col_name, label, outlier_fn in field_specs:
            col = getattr(FinancialRecord, col_name)
            null_count = (
                db.query(func.count(FinancialRecord.id))
                .filter(std_filter, col.is_(None))
                .scalar()
            ) or 0
            entry = {
                "field":      col_name,
                "label":      label,
                "null_count": null_count,
                "null_pct":   round(null_count / total * 100, 1),
            }
            if outlier_fn is not None:
                out_count = (
                    db.query(func.count(FinancialRecord.id))
                    .filter(std_filter, col.isnot(None), outlier_fn(col))
                    .scalar()
                ) or 0
                entry["outlier_count"] = out_count
                entry["outlier_pct"]   = round(out_count / total * 100, 2)
            fields_summary.append(entry)

        result.append({
            "standard": standard,
            "total":    total,
            "share_pct": round(total / sum(std_counts.values()) * 100, 1),
            "fields":   fields_summary,
        })
    return result
