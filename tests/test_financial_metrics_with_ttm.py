"""`financial_metrics_with_ttm` VIEW（#424 子2・ADR-0051）の定義を SQL の文面で縛る。

VIEW を実 PostgreSQL で走らせるテストは CI に無い（conftest は SQLite）。壊れ方は
**「通期の行が `financial_metrics` と違う値になる」**——どちらも妥当な数字なので、
子3 のゲートが「TTM の効果」として測るものが実は式の差、という形で現れる。だから
2 つの SQL の**式そのもの**を突き合わせる。実 PG での作成と一致は
`tests/test_financial_metrics_with_ttm_postgres.py`（`FINAPP_TEST_PG_URL` が要る）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database  # noqa: E402

ANNUAL_SQL = database.FINANCIAL_METRICS_VIEW_SQL
TTM_SQL = database.FINANCIAL_METRICS_WITH_TTM_VIEW_SQL

# 2 つの VIEW が共有する派生列（比率・Zスコア・成長率）。
SHARED_ALIASES = (
    "op_margin", "net_margin", "roe", "roa", "equity_ratio", "de_ratio", "cf_ratio",
    "rd_intensity", "da_intensity", "asset_turnover", "net_cash", "accruals", "nc_ratio",
    "z_revenue", "z_op_margin", "z_roe", "z_equity_ratio", "z_cf_ratio", "z_eps",
    "z_de_ratio", "z_nc_ratio", "z_roe_sec", "z_op_margin_sec",
    "rev_growth", "op_growth", "eps_growth", "delta_roe", "delta_op_margin",
    "market_cap", "per", "pbr", "div_yield",
    # 期末後分割の年の行の株数の遅れを掛ける（#753）。通期の行で2つの VIEW が食い違わないように。
    "predicted_market_cap",
)
# TTM 側だけが持つ追加条件（間の空いた年度の成長率を出さない）。
BASIS_GUARD = "AND (n.basis = 'annual' OR LAG(n.year) OVER cw = n.year - 1)"

_ALIAS_RE = re.compile(r"\bAS\s+([a-z_][a-z0-9_]*)")


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _expr_start(body: str, end: int) -> int:
    """`AS <alias>` の直前の式が始まる位置（深さ 0 のカンマ、または開き括弧の次）。"""
    depth = 0
    for i in range(end - 1, -1, -1):
        ch = body[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                return i + 1
            depth -= 1
        elif ch == "," and depth == 0:
            return i + 1
    return 0


def expressions(sql: str) -> dict[str, str]:
    """`<式> AS <alias>` を alias -> 式（空白を詰めたもの）で返す。"""
    body = " ".join(_strip_comments(sql).split())
    out: dict[str, str] = {}
    for m in _ALIAS_RE.finditer(body):
        alias = m.group(1)
        if alias not in SHARED_ALIASES:
            continue
        out[alias] = body[_expr_start(body, m.start()):m.start()].strip()
    return out


class TestSharedExpressions:
    """比率・Zスコア・成長率の式が 2 つの VIEW で同一であること。"""

    @pytest.mark.parametrize("alias", SHARED_ALIASES)
    def test_expression_matches(self, alias):
        annual = expressions(ANNUAL_SQL)[alias]
        ttm = expressions(TTM_SQL)[alias]
        # 違ってよいのは 2 つだけ: F と基準の遅れ（#753）の出どころ（通期は係数表・TTM は行の列）と、
        # TTM 行にだけ掛ける前年度の条件。
        for col in ("factor", "shares_lag", "dps_lag"):
            annual = annual.replace(f"COALESCE(saf.{col}, 1.0)", f"fr.{col}")
        ttm = ttm.replace(" " + BASIS_GUARD, "")
        assert annual == ttm

    def test_every_shared_alias_is_present_in_both(self):
        assert set(expressions(ANNUAL_SQL)) == set(SHARED_ALIASES)
        assert set(expressions(TTM_SQL)) == set(SHARED_ALIASES)


class TestTtmSpecifics:
    def test_windows_are_partitioned_by_basis(self):
        body = " ".join(_strip_comments(TTM_SQL).split())
        assert "yw AS (PARTITION BY n.year, n.basis)" in body
        assert "yws AS (PARTITION BY n.year, n.industry, n.basis)" in body
        assert ("cw AS (PARTITION BY n.edinet_code, n.basis ORDER BY n.year, n.period_end)"
                in body)

    def test_regression_results_join_is_limited_to_annual(self):
        body = " ".join(_strip_comments(TTM_SQL).split())
        assert "LEFT JOIN regression_results rr" in body
        assert "AND n.basis = 'annual'" in body

    def test_reads_the_ttm_table_and_the_split_factor_table(self):
        body = " ".join(_strip_comments(TTM_SQL).split())
        assert "FROM ttm_financial_records t" in body
        assert "LEFT JOIN split_adjustment_factors saf" in body
        assert "WHERE fr.period_type = 'annual'" in body      # 通期側は period_type で絞る

    def test_ttm_ids_are_negated(self):
        """`financial_records.id` と衝突させない（どちらも妥当な整数なので気づけない）。"""
        assert "-t.id" in " ".join(_strip_comments(TTM_SQL).split())

    def test_growth_columns_carry_the_basis_guard(self):
        body = " ".join(_strip_comments(TTM_SQL).split())
        assert body.count(BASIS_GUARD) == 5      # rev/op/eps growth と delta 2 本

    def test_stock_price_is_not_corrected(self):
        """`stock_price` を補正しないのは通期と同じ規約（ADR-0055・GOTCHAS）。"""
        assert "fr.stock_price * fr.factor" not in " ".join(TTM_SQL.split())

    def test_ttm_rows_carry_no_basis_lag(self):
        """TTM 行の遅れは 1.0（#753）。材料の間に分割があると合成しないので、期末後分割の年は
        TTM の材料にならない。通期側は係数表から読む。"""
        body = " ".join(_strip_comments(TTM_SQL).split())
        assert ("COALESCE(t.split_factor, 1.0::double precision), "
                "1.0::double precision, 1.0::double precision,") in body
        assert "COALESCE(saf.shares_lag, 1.0::double precision) AS shares_lag" in body
        assert "COALESCE(saf.dps_lag, 1.0::double precision) AS dps_lag" in body


class TestOrmAndRegistry:
    def test_orm_is_financial_metric_plus_two_columns(self):
        base = {c.name for c in database.FinancialMetric.__table__.columns}
        ttm = {c.name for c in database.FinancialMetricWithTTM.__table__.columns}
        assert ttm == base | {"basis", "filing_date"}

    def test_orm_columns_and_sql_aliases_agree(self):
        """ORM の列がすべて SQL に現れること（VIEW に無い列を引くと実行時に落ちる）。"""
        body = " ".join(_strip_comments(TTM_SQL).split())
        for col in database.FinancialMetricWithTTM.__table__.columns:
            assert re.search(rf"\b{col.name}\b", body), f"{col.name} が SQL に無い"

    def test_view_is_managed_by_init_db(self):
        assert ("financial_metrics_with_ttm", TTM_SQL) in database._managed_views()

    def test_fingerprint_covers_the_new_sql(self):
        """SQL を差し替えたら指紋が変わる＝次の init_db が作り直す（ADR-0048）。"""
        before = database._schema_fingerprint()
        original = database.FINANCIAL_METRICS_WITH_TTM_VIEW_SQL
        try:
            database.FINANCIAL_METRICS_WITH_TTM_VIEW_SQL = original + "\n-- touched"
            assert database._schema_fingerprint() != before
        finally:
            database.FINANCIAL_METRICS_WITH_TTM_VIEW_SQL = original
