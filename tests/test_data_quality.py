"""data_quality.py のユニットテスト。

run_data_quality_check の4サブ関数を in-memory SQLite で検証する。
ROE/equity_ratio 外れ値は FinancialMetric（VIEW 代替テーブル）を使うため
conftest の make_metric fixture で注入する。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_quality import (
    _check_by_accounting_standard,
    _check_null_fields,
    _check_outliers,
    _check_year_summary,
    run_data_quality_check,
)


class TestCheckNullFields:
    def test_empty_db_returns_eight_fields_zero_null(self, db):
        result = _check_null_fields(db)
        assert len(result) == 8
        for entry in result:
            assert entry["null_count"] == 0
            assert entry["null_pct"] == 0.0

    def test_counts_nulls_correctly(self, db, make_fin):
        db.add(make_fin(pl_revenue=500.0))
        db.add(make_fin(edinet_code="E00002", year=2022, period_end="2022-03-31"))
        db.commit()
        result = _check_null_fields(db)
        rev = next(e for e in result if e["field"] == "pl_revenue")
        assert rev["null_count"] == 1
        assert rev["null_pct"] == 50.0

    def test_all_columns_null(self, db, make_fin):
        db.add(make_fin())
        db.commit()
        result = _check_null_fields(db)
        for entry in result:
            assert entry["null_count"] == 1
            assert entry["null_pct"] == 100.0

    def test_result_includes_expected_fields(self, db):
        result = _check_null_fields(db)
        col_names = {e["field"] for e in result}
        assert "pl_revenue" in col_names
        assert "cf_operating_cf" in col_names


class TestCheckOutliers:
    def test_empty_db_no_issues(self, db):
        assert _check_outliers(db) == []

    def test_normal_values_no_issues(self, db, make_fin):
        db.add(make_fin(pl_revenue=1000.0, pbr=1.5, per=15.0))
        db.commit()
        assert _check_outliers(db) == []

    def test_negative_revenue_flagged(self, db, make_fin):
        db.add(make_fin(pl_revenue=-100.0))
        db.commit()
        labels = [i["label"] for i in _check_outliers(db)]
        assert "負の売上高" in labels

    def test_extreme_pbr_flagged(self, db, make_fin):
        db.add(make_fin(pbr=600.0))
        db.commit()
        labels = [i["label"] for i in _check_outliers(db)]
        assert any("PBR" in l for l in labels)

    def test_extreme_per_flagged(self, db, make_fin):
        db.add(make_fin(per=6000.0))
        db.commit()
        labels = [i["label"] for i in _check_outliers(db)]
        assert any("PER" in l for l in labels)

    def test_extreme_roe_flagged(self, db, make_metric):
        db.add(make_metric(roe=2000.0))
        db.commit()
        labels = [i["label"] for i in _check_outliers(db)]
        assert any("ROE" in l for l in labels)

    def test_negative_equity_ratio_flagged(self, db, make_metric):
        db.add(make_metric(equity_ratio=-150.0))
        db.commit()
        labels = [i["label"] for i in _check_outliers(db)]
        assert any("自己資本比率" in l for l in labels)

    def test_count_reflects_multiple_rows(self, db, make_fin):
        db.add(make_fin(pl_revenue=-1.0))
        db.add(make_fin(edinet_code="E00002", year=2022, period_end="2022-03-31",
                        pl_revenue=-2.0))
        db.commit()
        issues = _check_outliers(db)
        neg_rev = next(i for i in issues if i["label"] == "負の売上高")
        assert neg_rev["count"] == 2


class TestCheckYearSummary:
    def test_empty_db(self, db):
        result = _check_year_summary(db)
        assert result["total_companies"] == 0
        assert result["single_year_only"] == 0
        assert result["three_or_more_years"] == 0
        assert result["no_market_data"] == 0

    def test_single_year_company(self, db, make_fin):
        db.add(make_fin())
        db.commit()
        result = _check_year_summary(db)
        assert result["total_companies"] == 1
        assert result["single_year_only"] == 1
        assert result["three_or_more_years"] == 0

    def test_three_year_company(self, db, make_fin):
        for yr in (2021, 2022, 2023):
            db.add(make_fin(year=yr, period_end=f"{yr}-03-31"))
        db.commit()
        result = _check_year_summary(db)
        assert result["total_companies"] == 1
        assert result["three_or_more_years"] == 1
        assert result["single_year_only"] == 0

    def test_no_market_data_counted(self, db, make_fin):
        db.add(make_fin(market_cap=None))
        db.commit()
        assert _check_year_summary(db)["no_market_data"] == 1

    def test_two_companies(self, db, make_fin):
        db.add(make_fin(edinet_code="E00001"))
        db.add(make_fin(edinet_code="E00002", year=2022, period_end="2022-03-31"))
        db.commit()
        assert _check_year_summary(db)["total_companies"] == 2


class TestCheckByAccountingStandard:
    def test_empty_db(self, db):
        assert _check_by_accounting_standard(db) == []

    def test_null_standard_shown_as_unset(self, db, make_fin):
        db.add(make_fin(accounting_standard=None))
        db.commit()
        result = _check_by_accounting_standard(db)
        assert len(result) == 1
        assert result[0]["standard"] == "未設定"
        assert result[0]["total"] == 1

    def test_groups_by_standard(self, db, make_fin):
        db.add(make_fin(accounting_standard="JGAAP"))
        db.add(make_fin(edinet_code="E00002", year=2022, period_end="2022-03-31",
                        accounting_standard="IFRS"))
        db.commit()
        result = _check_by_accounting_standard(db)
        standards = {r["standard"] for r in result}
        assert standards == {"IFRS", "JGAAP"}

    def test_share_pct_sums_to_100(self, db, make_fin):
        for i, ec in enumerate(("E00001", "E00002", "E00003")):
            db.add(make_fin(edinet_code=ec, year=2020 + i,
                            period_end=f"{2020+i}-03-31",
                            accounting_standard="JGAAP"))
        db.commit()
        result = _check_by_accounting_standard(db)
        assert abs(sum(r["share_pct"] for r in result) - 100.0) < 0.1

    def test_fields_contains_expected_columns(self, db, make_fin):
        db.add(make_fin(accounting_standard="JGAAP"))
        db.commit()
        result = _check_by_accounting_standard(db)
        field_names = {f["field"] for f in result[0]["fields"]}
        assert "pl_revenue" in field_names
        assert "cf_operating_cf" in field_names


class TestRunDataQualityCheck:
    def test_returns_all_top_level_keys(self, db):
        result = run_data_quality_check(db)
        assert set(result) == {
            "null_fields", "outliers", "year_summary", "accounting_standard", "checked_at"
        }

    def test_checked_at_ends_with_utc(self, db):
        result = run_data_quality_check(db)
        assert result["checked_at"].endswith("UTC")

    def test_null_fields_is_list(self, db):
        assert isinstance(run_data_quality_check(db)["null_fields"], list)

    def test_year_summary_is_dict(self, db):
        assert isinstance(run_data_quality_check(db)["year_summary"], dict)
