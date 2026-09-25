"""TTM 合成の純関数（#424 子2・ADR-0051）。

ここで縛るのは**「合成しない」と判断できること**が中心である。合成そのものは足し引きで、
壊れたら値が飛ぶのですぐ分かる。危ないのは逆で、分割・会計基準の変更・提出の遅れを
見落として合成した行は**もっともらしい数字**になり、例外も出ない。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import corporate_actions as C  # noqa: E402
import ttm_composite as T  # noqa: E402

COLUMNS = ("pl_revenue", "pl_operating_profit", "pl_net_income_attr", "pl_eps",
           "pl_rd_expenses", "cf_operating_cf", "bs_total_assets", "bs_total_equity",
           "bs_bps", "bs_investment_securities", "issued_shares", "dps", "employees",
           "stock_price", "per", "pbr", "market_cap", "div_yield")


def row(period_type: str, year: int, period_end: str, *, filing: str | None = None, **values):
    """材料の行。値は列名で渡す（省略した列は None）。"""
    base = dict(pl_revenue=1000.0, pl_operating_profit=100.0, pl_net_income_attr=60.0,
                pl_eps=60.0, cf_operating_cf=90.0, bs_total_assets=5000.0,
                bs_total_equity=2000.0, bs_bps=200.0, issued_shares=10.0,
                dps=10.0, employees=100.0)
    if period_type == "H1":
        base = {k: (v / 2 if k in ("pl_revenue", "pl_operating_profit", "pl_net_income_attr",
                                   "pl_eps", "cf_operating_cf") else v)
                for k, v in base.items()}
        base["bs_bps"] = None          # H1 はほぼ全行が空（実測）
        base["dps"] = None
        base["employees"] = None
    base.update(values)
    return T.SourceRow(
        edinet_code="E00001", year=year, period_type=period_type,
        period_end=date.fromisoformat(period_end),
        filing_date=date.fromisoformat(filing) if filing else None,
        doc_id="S1", sec_code="1234", company_name="テスト", industry="小売業",
        market="プライム", values={c: base.get(c) for c in COLUMNS})


def materials(**kw):
    """標準の 3 材料（3月期・H1 は 9月末・提出は 11/14）。"""
    a = row("annual", 2025, "2025-03-31")
    p = row("H1", 2025, "2024-09-30", filing="2024-11-14")
    c = row("H1", 2026, "2025-09-30", filing="2025-11-14")
    for name, over in kw.items():
        target = {"annual_prev": a, "h1_prev": p, "h1_cur": c}[name]
        vals = dict(target.values, **over.pop("values", {}))
        target = target._replace(values=vals, **over)
        if name == "annual_prev":
            a = target
        elif name == "h1_prev":
            p = target
        else:
            c = target
    return a, p, c


def reason(*, windows=(), basis_prev=None, basis_cur=None, **kw):
    a, p, c = materials(**kw)
    return T.reject_reason(a, p, c, annual_cur=None, windows=list(windows),
                           basis_prev=basis_prev, basis_cur=basis_cur,
                           split_gate_ratio=1.4)


class TestCompose:
    def test_flow_is_annual_minus_prev_half_plus_current_half(self):
        a, p, c = materials()
        v = T.compose_values(a, p, c, columns=COLUMNS)
        assert v["pl_revenue"] == pytest.approx(1000.0 - 500.0 + 500.0)
        assert v["cf_operating_cf"] == pytest.approx(90.0 - 45.0 + 45.0)

    def test_stock_comes_from_current_half(self):
        a, p, c = materials(h1_cur={"values": {"bs_total_assets": 5500.0}})
        v = T.compose_values(a, p, c, columns=COLUMNS)
        assert v["bs_total_assets"] == 5500.0

    def test_bps_is_stretched_by_equity(self):
        """H1 の BPS は空なので、通期の BPS を純資産の比で伸ばす。"""
        a, p, c = materials(h1_cur={"values": {"bs_total_equity": 2200.0}})
        v = T.compose_values(a, p, c, columns=COLUMNS)
        assert v["bs_bps"] == pytest.approx(200.0 * 2200.0 / 2000.0)

    def test_dividend_and_employees_come_from_annual(self):
        a, p, c = materials()
        v = T.compose_values(a, p, c, columns=COLUMNS)
        assert v["dps"] == 10.0 and v["employees"] == 100.0
        assert v["issued_shares"] == 10.0          # 株数は今期 H1

    def test_missing_material_makes_the_column_null(self):
        a, p, c = materials(h1_cur={"values": {"pl_operating_profit": None}})
        v = T.compose_values(a, p, c, columns=COLUMNS)
        assert v["pl_operating_profit"] is None
        assert v["pl_revenue"] is not None          # 列ごとに伝播する（行は捨てない）

    def test_notes_only_flow_falls_back_to_annual(self):
        """研究開発費は半期で開示されないことが多い。空のままだと VIEW が 0 として扱う。"""
        a, p, c = materials(annual_prev={"values": {"pl_rd_expenses": 30.0}})
        carried: T.Counter = T.Counter()
        v = T.compose_values(a, p, c, columns=COLUMNS, carried=carried)
        assert v["pl_rd_expenses"] == 30.0
        assert carried["flow:pl_rd_expenses"] == 1

    def test_carried_bs_column_comes_from_annual(self):
        a, p, c = materials(annual_prev={"values": {"bs_investment_securities": 700.0}})
        carried: T.Counter = T.Counter()
        v = T.compose_values(a, p, c, columns=COLUMNS, carried=carried)
        assert v["bs_investment_securities"] == 700.0
        assert carried["bs:bs_investment_securities"] == 1

    def test_market_columns_are_not_composed(self):
        a, p, c = materials()
        v = T.compose_values(a, p, c, columns=COLUMNS)
        assert not set(T.MARKET_COLUMNS) & set(v)

    def test_unknown_column_raises(self):
        a, p, c = materials()
        with pytest.raises(ValueError):
            T.compose_values(a, p, c, columns=COLUMNS + ("mystery_metric",))


class TestRejectStructure:
    def test_ordinary_case_is_composed(self):
        assert reason() is None

    def test_fiscal_period_change(self):
        assert reason(h1_cur={"period_end": date(2025, 12, 31)}) == "period_shift"

    def test_late_filing(self):
        assert reason(h1_cur={"filing_date": date(2025, 11, 30)}) == "late_filing"

    def test_missing_filing_date(self):
        assert reason(h1_cur={"filing_date": None}) == "no_filing_date"

    def test_annual_of_the_year_must_follow_the_half(self):
        a, p, c = materials()
        bad = row("annual", 2026, "2025-10-31")
        assert T.reject_reason(a, p, c, annual_cur=bad, windows=[], basis_prev=None,
                               basis_cur=None, split_gate_ratio=1.4) == "period_shift"


class TestRejectBasis:
    def test_accounting_standard_change(self):
        assert reason(basis_prev=T.Basis("Consolidated", "JP"),
                      basis_cur=T.Basis("Consolidated", "IFRS")) == "standard_change"

    def test_consolidation_change(self):
        assert reason(basis_prev=T.Basis("Consolidated", "JP"),
                      basis_cur=T.Basis("NonConsolidated", "JP")) == "consolidation_change"

    def test_same_basis_is_fine(self):
        assert reason(basis_prev=T.Basis("Consolidated", "JP"),
                      basis_cur=T.Basis("Consolidated", "JP")) is None

    def test_doc_type_parsing(self):
        assert T.parse_doc_type("FYFinancialStatements_Consolidated_JP") == \
            T.Basis("Consolidated", "JP")
        assert T.parse_doc_type("2QFinancialStatements_NonConsolidated_IFRS") == \
            T.Basis("NonConsolidated", "IFRS")
        assert T.parse_doc_type("EarnForecastRevision") is None
        assert T.parse_doc_type(None) is None


class TestRejectSplit:
    def test_issued_shares_doubling(self):
        assert reason(h1_cur={"values": {"issued_shares": 20.0}}) == "split_shares"

    def test_buyback_sized_change_is_allowed(self):
        """1.4 倍未満の増資・消却では作る。EPS は期中平均株数ベースで基準が変わらない。"""
        assert reason(h1_cur={"values": {"issued_shares": 10.9}}) is None

    def test_split_between_period_end_and_filing_moves_only_implied_shares(self):
        """期末後・提出前の分割。EPS だけ遡って直り、`issued_shares` は期末の株数のまま残る。"""
        assert reason(h1_cur={"values": {"pl_eps": 15.0}}) == "split_implied_shares"

    def test_tiny_eps_is_not_used_for_implied_shares(self):
        """|EPS| < 1 円は丸めが効くので判定に使わない（作る側へ倒す）。"""
        assert reason(h1_cur={"values": {"pl_eps": 0.5, "pl_net_income_attr": 0.05}}) is None

    def test_detected_event_window_overlapping_the_danger_window(self):
        w = C.SplitWindow(date(2025, 3, 31), date(2026, 3, 31), 2.0, "detected")
        assert reason(windows=[w]) == "split_detected"

    def test_awaiting_magnitude_event(self):
        w = C.SplitWindow(date(2025, 3, 31), date(2026, 3, 31), None, "awaiting")
        assert reason(windows=[w]) == "split_awaiting"

    def test_official_event_inside_the_danger_window(self):
        w = C.SplitWindow(date(2025, 6, 30), date(2025, 7, 1), 2.0, "official")
        assert reason(windows=[w]) == "split_official"

    def test_event_before_the_previous_half_filing_is_fine(self):
        """前期 H1 の提出より前のイベントは、3 つの材料すべてに反映済み。"""
        w = C.SplitWindow(date(2023, 3, 31), date(2024, 3, 31), 2.0, "detected")
        assert reason(windows=[w]) is None

    def test_event_after_the_current_filing_is_fine(self):
        w = C.SplitWindow(date(2026, 3, 31), date(2027, 3, 31), 2.0, "detected")
        assert reason(windows=[w]) is None

    def test_window_with_unknown_edges_is_treated_as_overlapping(self):
        w = C.SplitWindow(None, None, 2.0, "detected")
        assert reason(windows=[w]) == "split_detected"


class TestSplitFactor:
    def test_only_events_after_the_filing_are_multiplied(self):
        after = C.SplitWindow(date(2026, 3, 31), date(2027, 3, 31), 2.0, "detected")
        before = C.SplitWindow(date(2023, 3, 31), date(2024, 3, 31), 5.0, "detected")
        assert C.factor_after([after, before], date(2025, 11, 14)) == 2.0

    def test_events_without_a_magnitude_are_not_multiplied(self):
        w = C.SplitWindow(date(2026, 3, 31), date(2027, 3, 31), None, "awaiting")
        assert C.factor_after([w], date(2025, 11, 14)) == 1.0

    def test_multiple_events_multiply(self):
        w1 = C.SplitWindow(date(2026, 3, 31), date(2027, 3, 31), 2.0, "detected")
        w2 = C.SplitWindow(date(2027, 3, 31), date(2028, 3, 31), 3.0, "detected")
        assert C.factor_after([w1, w2], date(2025, 11, 14)) == pytest.approx(6.0)

    def test_official_factor_is_inverted_into_a_share_ratio(self):
        """公式 `AdjFactor` は過去株価に掛ける係数（1:2 分割で 0.5）。株数比は逆数。"""
        (w,) = C.split_windows((), (), (("2026-05-01", 0.5),))
        assert w.canonical == pytest.approx(2.0)
        assert w.source == "official" and w.end == date(2026, 5, 1)


class TestMagnitude:
    def test_revenue_ratio_outside_bounds(self):
        a, _, _ = materials()
        assert T.magnitude_reason({"pl_revenue": 9000.0}, a) == "magnitude_revenue"

    def test_assets_ratio_outside_bounds(self):
        a, _, _ = materials()
        assert T.magnitude_reason({"bs_total_assets": 50.0}, a) == "magnitude_assets"

    def test_ordinary_values_pass(self):
        a, _, _ = materials()
        assert T.magnitude_reason({"pl_revenue": 1100.0, "bs_total_assets": 5200.0}, a) is None

    def test_second_half_revenue_must_be_positive(self):
        a, p, _ = materials(h1_prev={"values": {"pl_revenue": 1200.0}})
        assert T.second_half_reason(a, p) == "second_half_nonpositive"


class TestBuildRows:
    def _build(self, rows, **kw):
        kw.setdefault("windows_by_ec", {})
        kw.setdefault("basis_by_key", {})
        kw.setdefault("columns", COLUMNS)
        kw.setdefault("split_gate_ratio", 1.4)
        kw.setdefault("embargo_days", 84)
        kw.setdefault("today", date(2026, 1, 15))
        kw.setdefault("price_for", lambda ec, pe, latest: 1200.0 if latest else 1000.0)
        kw.setdefault("market_values",
                      lambda price, eps, bps, shares, equity, dps: {"stock_price": price})
        return T.build_ttm_rows(rows, **kw)

    def test_row_is_keyed_by_the_current_half(self):
        a, p, c = materials()
        out, stats = self._build([a, p, c])
        assert len(out) == 1
        r = out[0]
        assert (r["year"], r["period_end"], r["filing_date"]) == (2026, c.period_end, c.filing_date)
        assert r["prev_annual_period_end"] == a.period_end
        assert r["prev_h1_period_end"] == p.period_end
        assert stats["by_year"] == {2026: 1}

    def test_latest_row_uses_the_current_price(self):
        a, p, c = materials()
        out, _ = self._build([a, p, c])
        assert out[0]["stock_price"] == 1200.0       # TTM がその社の最新の行

    def test_older_row_uses_the_period_end_price(self):
        a, p, c = materials()
        newer = row("annual", 2026, "2026-03-31")
        out, _ = self._build([a, p, c, newer])
        assert out[0]["stock_price"] == 1000.0

    def test_missing_prev_year_is_counted(self):
        a, _, c = materials()
        out, stats = self._build([a, c])
        assert out == [] and stats["rejected"]["no_prev_h1"] == 1

    def test_duplicate_rows_are_counted(self):
        a, p, c = materials()
        dup = c._replace(period_end=date(2025, 9, 29))
        out, stats = self._build([a, p, c, dup])
        assert out == [] and stats["rejected"]["dup_rows"] == 1

    def test_missing_disclosure_is_noted_as_embargo_or_unknown(self):
        a, p, c = materials()
        _, stats = self._build([a, p, c], today=date(2025, 11, 20))
        assert stats["notes"]["standard_embargo"] == 1
        _, stats = self._build([a, p, c], today=date(2026, 6, 1))
        assert stats["notes"]["standard_unknown"] == 1

    def test_split_factor_lands_on_the_row(self):
        a, p, c = materials()
        w = C.SplitWindow(date(2026, 3, 31), date(2027, 3, 31), 2.0, "detected")
        out, _ = self._build([a, p, c], windows_by_ec={"E00001": [w]})
        assert out[0]["split_factor"] == 2.0

    def test_rows_without_a_price_are_counted(self):
        a, p, c = materials()
        out, stats = self._build([a, p, c], price_for=lambda *_: None)
        assert len(out) == 1 and stats["notes"]["no_price"] == 1


class TestModuleBoundary:
    def test_pure_part_does_not_import_database_or_scripts(self):
        """`# ── I/O ──` より上で重い import をしない（検出器と同じ規約）。

        ここを破ると、純関数のテストが DB エンジンの生成と `.env` の読み込みを道連れにする。
        """
        src = (Path(__file__).resolve().parents[1] / "ttm_composite.py").read_text(encoding="utf-8")
        head = src.split("# ── I/O ──")[0]
        for bad in ("import database", "from database", "from scripts", "import scripts",
                    "from collector", "import collector"):
            assert bad not in head, f"純関数ブロックが {bad} を引いている"
