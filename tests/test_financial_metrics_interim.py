"""financial_metrics_interim VIEW / FinancialMetricInterim 読取モデル（Issue #219② フェーズC）。

VIEW の SQL ロジック（period_type<>'annual' フィルタ・行単位比率・YoY成長）は Postgres 専用の
ため SQLite では検証不可（README のとおり financial_metrics と同様に Postgres 側で別途検証。
本 PR では本番 H1 実データに対し Supabase MCP の SELECT で式を検証済み）。ここでは:
  - 読取モデル FinancialMetricInterim のスキーマ契約（通期版との差分）
  - conftest の in-memory SQLite で読取モデルが生成・往復できること（#323 が消費可能）
  - VIEW DDL 文字列が期待要素を含むこと（回帰防止）
を検証する。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (  # noqa: E402
    FinancialMetric, FinancialMetricInterim, FINANCIAL_METRICS_INTERIM_VIEW_SQL,
)


class TestInterimReadModelSchema:
    def test_has_period_type_and_filing_date(self):
        # 非通期版は point-in-time 用に period_type/filing_date を持つ（通期版には無い）。
        assert hasattr(FinancialMetricInterim, "period_type")
        assert hasattr(FinancialMetricInterim, "filing_date")
        assert not hasattr(FinancialMetric, "period_type")
        assert not hasattr(FinancialMetric, "filing_date")

    def test_excludes_zscores_and_regression_outputs(self):
        # 非通期版は Zスコア・回帰予測（predicted/gap）を持たない（#323 が独自正規化・
        # 年次OLS予測は H1 に非該当）。通期版はこれらを持つ。
        for excluded in ("z_revenue", "z_roe", "z_nc_ratio",
                         "predicted_market_cap", "gap_ratio"):
            assert not hasattr(FinancialMetricInterim, excluded), excluded
            assert hasattr(FinancialMetric, excluded), excluded

    def test_has_the_ratios_that_disclosure_features_lacked(self):
        # #322(/fins/summary)でスコープ外だった質シグナル系を充足する。
        for ratio in ("roe", "roa", "asset_turnover", "equity_ratio", "cf_ratio",
                      "op_margin", "net_margin", "rev_growth", "op_growth", "eps_growth"):
            assert hasattr(FinancialMetricInterim, ratio), ratio


class TestInterimReadModelRoundTrip:
    def test_insert_and_query_h1_row(self, db):
        # conftest の ViewBase.create_all が interim テーブルを生成する。#323 が読取モデルとして
        # H1 行（比率・filing_date 付き）を往復できることを確認する。
        db.add(FinancialMetricInterim(
            id=1, edinet_code="E00001", year=2024,
            period_end=date(2023, 9, 30), period_type="H1",
            filing_date=date(2023, 11, 14),
            pl_revenue=1000.0, pl_operating_profit=120.0, bs_total_assets=5000.0,
            roe=8.5, roa=3.2, asset_turnover=0.2, op_margin=12.0, rev_growth=5.5,
        ))
        db.commit()
        row = db.query(FinancialMetricInterim).filter_by(
            edinet_code="E00001", period_type="H1").one()
        assert row.period_end == date(2023, 9, 30)
        assert row.filing_date == date(2023, 11, 14)
        assert row.roe == 8.5 and row.asset_turnover == 0.2
        assert row.rev_growth == 5.5


class TestInterimViewSql:
    def test_view_sql_key_elements(self):
        sql = FINANCIAL_METRICS_INTERIM_VIEW_SQL
        assert "CREATE OR REPLACE VIEW financial_metrics_interim" in sql
        # 非通期のみを対象にする隔離フィルタ。
        assert "period_type <> 'annual'" in sql
        # 質シグナル比率と YoY 成長（同一 period_type 内）を算出する。
        assert "AS roe" in sql and "AS asset_turnover" in sql
        assert "PARTITION BY n.edinet_code, n.period_type" in sql
        # point-in-time 列を露出する。
        assert "fr.filing_date" in sql
        # 通期専用の要素は算出/JOIN しない（説明コメントには言及があるため実クエリ部分で判定）。
        assert "JOIN regression_results" not in sql
        assert "AS z_revenue" not in sql
        assert "predicted_market_cap" not in sql
