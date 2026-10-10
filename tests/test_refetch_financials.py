"""scripts/refetch_financials.py のテスト（#870）。

検体は 2026-10-09 に EDINET から取得した実際の書類の行（要素ID・コンテキストID・値）を写した。
推測で書くと、実際の書類のタグ名・コンテキスト名を読めないことを検出できない。
旧値は #852 の修正前の読み方（名前に Consolidated を含むセグメントが連結総額に勝つ）で
同じ書類から出ていた値（#858 の検証で確認した E00317 の 938億・20億・530人）。
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

import database as D
from database import AppSetting, FinancialRecord, financial_columns, get_setting
from scripts import refetch_financials as rf
from scripts import run_daytime as rd
from tests.test_collector import (
    _E01033_SINGLE_S100W071, _TOYOTA_S100Y8NY, _TOYOTA_USGAAP_S100G1ZO,
)

_SEG = "jpcrp030000-asr_E00317-000ConsolidatedSubsidiariesReportableSegmentsMember"

# E00317 2026年度（S100YHJP）
_E00317_S100YHJP = [
    ("jppfs_cor:NetSales", f"CurrentYearDuration_{_SEG}", "93814000000"),
    ("jppfs_cor:NetSales", "CurrentYearDuration", "439615000000"),
    ("jppfs_cor:OperatingIncome", f"CurrentYearDuration_{_SEG}", "2020000000"),
    ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "33618000000"),
    ("jppfs_cor:OrdinaryIncome", "CurrentYearDuration", "33257000000"),
    ("jppfs_cor:DepreciationAndAmortizationOpeCF", "CurrentYearDuration", "3658000000"),
    ("jppfs_cor:NetCashProvidedByUsedInOperatingActivities", "CurrentYearDuration", "28432000000"),
    ("jppfs_cor:NetCashProvidedByUsedInInvestmentActivities", "CurrentYearDuration", "-6363000000"),
    ("jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults", "CurrentYearDuration", "189.68"),
    ("jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults",
     "CurrentYearDuration_NonConsolidatedMember", "184.78"),
    ("jpcrp_cor:NumberOfEmployees", f"CurrentYearInstant_{_SEG}", "530"),
    ("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant", "3957"),
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31"),
]
# E02144 トヨタ 2026年度（S100Y8NY）の DEI（売上高の行は test_collector の _TOYOTA_S100Y8NY）
_TOYOTA_DEI_S100Y8NY = [
    ("jpdei_cor:CurrentPeriodEndDateDEI", "FilingDateInstant", "2026-03-31"),
]

# #852 修正前の読み方で E00317 の行に入っていた値。DB に書類に無い列（総資産）と市場データも持たせる。
_E00317_OLD = dict(
    year=2026, period_end=date(2026, 3, 31), period_type="annual",
    pl_revenue=93814000000.0, pl_operating_profit=2020000000.0, employees=530.0,
    pl_ordinary_profit=33257000000.0, pl_depreciation=3658000000.0,
    cf_operating_cf=28432000000.0, cf_investing_cf=-6363000000.0, cf_free_cf=22069000000.0,
    pl_ebitda=2020000000.0 + 3658000000.0,                 # 旧営業利益 + 減価償却費
    pl_nonoperating_income=33257000000.0 - 2020000000.0,   # 経常利益 - 旧営業利益
    pl_eps=189.68,
    bs_total_assets=47000000000.0,                         # 書類（検体）に無い列
    stock_price=1500.0, market_cap=90000.0, per=7.9, pbr=1.1,
)

_CHANGED = {"pl_revenue", "pl_operating_profit", "employees", "pl_ebitda", "pl_nonoperating_income"}


def _df(rows):
    return pd.DataFrame(rows, columns=["要素ID", "コンテキストID", "値"])


class Crash(BaseException):
    """途中で落ちたことの模擬（取得関数の `except Exception` を素通りする）。"""


class FakeEdinet:
    """doc_id -> 行。無い doc_id は取得失敗（fetch_xbrl_csv と同じく None）。"""

    def __init__(self, docs, crash_on=None):
        self.docs = docs
        self.crash_on = crash_on
        self.calls = []

    async def __call__(self, doc_id):
        self.calls.append(doc_id)
        if doc_id == self.crash_on:
            raise Crash(doc_id)
        rows = self.docs.get(doc_id)
        return None if rows is None else _df(rows)


def _run(db, fetch, **kw):
    kw.setdefault("sleep_sec", 0)
    return asyncio.run(rf.refetch(db, fetch=fetch, log=lambda m: None, **kw))


def _add_rows(db, make_fin, n, **overrides):
    """E00317 と同じ旧値の行を n 社ぶん（doc_id D1..Dn）作る。"""
    rows = []
    for i in range(1, n + 1):
        kw = {**_E00317_OLD, "edinet_code": f"E9000{i}", "doc_id": f"D{i}", **overrides}
        rows.append(make_fin(**kw))
    db.add_all(rows)
    db.commit()
    return [r.id for r in rows]


def _snapshot(db, rid):
    db.expire_all()
    row = db.get(FinancialRecord, rid)
    return {c.name: getattr(row, c.name) for c in FinancialRecord.__table__.columns}


# ── 書類 -> 列（DB に触らない）────────────────────────────────────────────

class TestDocumentColumns:
    def test_consolidated_values_win(self):
        v = rf.document_columns(_df(_E00317_S100YHJP), "E00317", "2026-03-31")
        assert v["pl_revenue"] == 439615000000.0
        assert v["pl_operating_profit"] == 33618000000.0
        assert v["employees"] == 3957.0

    def test_persisted_derived_amounts_come_from_calc_derived(self):
        v = rf.document_columns(_df(_E00317_S100YHJP), "E00317", "2026-03-31")
        assert v["pl_ebitda"] == 33618000000.0 + 3658000000.0
        assert v["pl_nonoperating_income"] == 33257000000.0 - 33618000000.0
        assert v["cf_free_cf"] == 28432000000.0 - 6363000000.0

    def test_free_cf_needs_both_cf_inputs(self):
        """calc_derived は欠けた入力を0とみなす。投資CFが読めない書類で既存のフリーCFを潰さない。"""
        rows = [r for r in _E00317_S100YHJP if "InvestmentActivities" not in r[0]]
        v = rf.document_columns(_df(rows), "E00317", "2026-03-31")
        assert v["cf_operating_cf"] == 28432000000.0
        assert "cf_free_cf" not in v

    def test_only_document_columns(self):
        """行のキー・会社の属性・市場データは候補に入らない。"""
        v = rf.document_columns(_df(_E00317_S100YHJP), "E00317", "2026-03-31")
        untouchable = {"edinet_code", "year", "period_end", "period_type", "company_name",
                       "sec_code", "industry", "doc_id", "source", "stock_price", "market_cap",
                       "per", "pbr", "div_yield"}
        assert not untouchable & set(v)
        assert all(val is not None for val in v.values())

    def test_unreadable_document_is_none(self):
        assert rf.document_columns(_df(_TOYOTA_DEI_S100Y8NY), "E02144", "2026-03-31") is None


class TestFinancialColumns:
    """upsert_financial と共有する写像（#870 で切り出した）。"""

    def test_sections_map_to_columns(self):
        flat = financial_columns({"bs": {"total_assets": 1.0}, "pl": {"revenue": 2.0},
                                  "cf": {"free_cf": 3.0}, "val": {"dps": 4.0},
                                  "nonfin": {"employees": 5.0}, "derived": {"roe": 6.0},
                                  "meta": {"industry_name": "x"}})
        assert flat == {"bs_total_assets": 1.0, "pl_revenue": 2.0, "cf_free_cf": 3.0,
                        "dps": 4.0, "employees": 5.0}

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="未知キー"):
            financial_columns({"nonfin": {"employes": 1.0}})


# ── 上書き規則 ─────────────────────────────────────────────────────────────

class TestOverwriteRules:
    def test_apply_rewrites_only_differing_document_columns(self, db, make_fin):
        (rid,) = _add_rows(db, make_fin, 1)
        before = _snapshot(db, rid)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP}), apply=True,
                 filters=rf.Filters(edinet_codes=("E90001",)))
        after = _snapshot(db, rid)

        # ①値が違う列は新しい値（④calc_derived の派生額を含む）
        assert after["pl_revenue"] == 439615000000.0
        assert after["pl_operating_profit"] == 33618000000.0
        assert after["employees"] == 3957.0
        assert after["pl_ebitda"] == 33618000000.0 + 3658000000.0
        assert after["pl_nonoperating_income"] == 33257000000.0 - 33618000000.0
        # ②書類から取れなかった列は旧値のまま
        assert after["bs_total_assets"] == 47000000000.0
        # ③値が同じ列は変化に数えない
        assert set(r.column_counts) == _CHANGED
        assert r.changed_rows == 1
        # キー・市場データは触らない
        for col in ("edinet_code", "year", "period_end", "period_type", "doc_id", "company_name",
                    "stock_price", "market_cap", "per", "pbr", "cf_free_cf", "pl_eps"):
            assert after[col] == before[col], col

    def test_toyota_revenue_becomes_consolidated(self, db, make_fin):
        """#858 の本体: トヨタ 2026年度の売上高が単体 18.26兆 -> 連結 50.68兆。売上原価は同値。"""
        db.add(make_fin(edinet_code="E02144", year=2026, period_end=date(2026, 3, 31),
                        doc_id="S100Y8NY", pl_revenue=18259979000000.0,
                        pl_cost_of_sales=39141418000000.0))
        db.commit()
        fake = FakeEdinet({"S100Y8NY": _TOYOTA_S100Y8NY + _TOYOTA_DEI_S100Y8NY})
        r = _run(db, fake, apply=True, filters=rf.Filters(edinet_codes=("E02144",)))
        row = db.query(FinancialRecord).filter_by(edinet_code="E02144").one()
        assert row.pl_revenue == 50684952000000.0
        assert row.pl_cost_of_sales == 39141418000000.0
        assert "pl_cost_of_sales" not in r.column_counts
        assert r.examples["pl_revenue"][0][-2:] == (18259979000000.0, 50684952000000.0)

    def test_null_column_is_filled(self, db, make_fin):
        """NULL -> 値も変化として書く（トヨタ H1 の売上高のように、旧タグ対応で欠けていた列）。"""
        (rid,) = _add_rows(db, make_fin, 1, pl_revenue=None)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP}), apply=True,
                 filters=rf.Filters(doc_ids=("D1",)))
        assert _snapshot(db, rid)["pl_revenue"] == 439615000000.0
        assert r.examples["pl_revenue"][0][-2:] == (None, 439615000000.0)

    def test_period_mismatch_is_not_written(self, db, make_fin):
        """書類が名乗る当期末日が行と違えば書かない（別の期の値で上書きしない）。"""
        (rid,) = _add_rows(db, make_fin, 1, year=2025, period_end=date(2025, 3, 31))
        before = _snapshot(db, rid)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP}), apply=True,
                 filters=rf.Filters(doc_ids=("D1",)))
        assert _snapshot(db, rid) == before
        assert r.period_mismatch == [("E90001", "D1", "2025-03-31", "2026-03-31")]
        assert r.changed_rows == 0

    def test_document_without_dei_is_not_written(self, db, make_fin):
        rows = [r for r in _E00317_S100YHJP if "DEI" not in r[0]]
        (rid,) = _add_rows(db, make_fin, 1)
        before = _snapshot(db, rid)
        r = _run(db, FakeEdinet({"D1": rows}), apply=True, filters=rf.Filters(doc_ids=("D1",)))
        assert _snapshot(db, rid) == before
        assert r.period_mismatch == [("E90001", "D1", "2026-03-31", None)]

    def test_accounting_standard_is_filled_from_dei(self, db, make_fin):
        """会計基準も書類（DEI）由来で、取り直しで埋まる（#859・既存行は #858 で埋める）。"""
        rows = _E00317_S100YHJP + [
            ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "Japan GAAP"),  # S100YHJP の実物
        ]
        (rid,) = _add_rows(db, make_fin, 1)
        r = _run(db, FakeEdinet({"D1": rows}), apply=True, filters=rf.Filters(doc_ids=("D1",)))
        assert _snapshot(db, rid)["accounting_standard"] == "JGAAP"
        assert r.examples["accounting_standard"][0][-2:] == (None, "JGAAP")

    def test_unreadable_standard_keeps_the_existing_value(self, db, make_fin):
        (rid,) = _add_rows(db, make_fin, 1, accounting_standard="JGAAP")
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP}), apply=True,
                 filters=rf.Filters(doc_ids=("D1",)))
        assert _snapshot(db, rid)["accounting_standard"] == "JGAAP"
        assert "accounting_standard" not in r.column_counts

    def test_valuation_inputs_are_reported(self, db, make_fin):
        """PER・PBR 等の入力（EPS）が変わった社を一覧にする（市場データは再計算しない）。"""
        (rid,) = _add_rows(db, make_fin, 1, pl_eps=184.78)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP}), apply=True,
                 filters=rf.Filters(doc_ids=("D1",)))
        assert r.valuation_changed == {"E90001"}
        assert _snapshot(db, rid)["per"] == 7.9
        assert "E90001" in rf.format_report(r)


# ── 試運転 ────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_changes_nothing(self, db, make_fin):
        ids = _add_rows(db, make_fin, 2)
        before = [_snapshot(db, rid) for rid in ids]
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP, "D2": _E00317_S100YHJP}))
        assert [_snapshot(db, rid) for rid in ids] == before
        assert db.query(AppSetting).count() == 0          # 進捗も書かない
        # 変わる見込みは数えて出す
        assert r.changed_rows == 2
        assert set(r.column_counts) == _CHANGED
        assert r.examples["pl_revenue"][0][-2:] == (93814000000.0, 439615000000.0)
        out = rf.format_report(r)
        assert "dry-run" in out and "pl_revenue" in out

    def test_examples_per_column_is_capped(self, db, make_fin):
        _add_rows(db, make_fin, 3)
        docs = {f"D{i}": _E00317_S100YHJP for i in range(1, 4)}
        assert len(_run(db, FakeEdinet(docs)).examples["pl_revenue"]) == rf.EXAMPLES_PER_COLUMN
        assert len(_run(db, FakeEdinet(docs), examples_per_column=1).examples["pl_revenue"]) == 1
        assert rf._parse_args(["--examples", "20"]).examples == 20

    def test_zero_change_is_stated(self, db, make_fin):
        _add_rows(db, make_fin, 1, pl_revenue=439615000000.0, pl_operating_profit=33618000000.0,
                  employees=3957.0, pl_ebitda=33618000000.0 + 3658000000.0,
                  pl_nonoperating_income=33257000000.0 - 33618000000.0)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP}))
        assert r.changed_rows == 0 and r.unchanged_rows == 1
        assert "変化は0件" in rf.format_report(r)


# ── 単体の値の掃除（--clear-nonconsolidated・#896）─────────────────────────────

# トヨタ 2019年度（S100G1ZO）の行に、#896 の前の読み方で入っていた値（連結の要約＋単体の明細）。
_TOYOTA_2019_OLD = dict(
    edinet_code="E02144", year=2019, period_end=date(2019, 3, 31), doc_id="S100G1ZO",
    accounting_standard="US-GAAP",
    pl_revenue=30225681000000.0, pl_net_income=1882873000000.0, pl_eps=650.55,
    bs_total_assets=51936949000000.0, bs_total_equity=20565210000000.0, bs_bps=6830.92,
    cf_operating_cf=3766597000000.0, dps=220.0, employees=370870.0, issued_shares=3310097492.0,
    # ここから下が単体の値（規則で空欄になる列）
    pl_cost_of_sales=9991345000000.0, pl_gross_profit=2643093000000.0,
    pl_sga=1316956000000.0, pl_operating_profit=1326137000000.0,
    pl_ordinary_profit=2323121000000.0,
    pl_nonoperating_income=2323121000000.0 - 1326137000000.0,   # 単体の経常 - 単体の営業
    bs_total_liabilities=5266718000000.0, bs_current_assets=7078259000000.0,
    bs_inventory=187526000000.0 + 86559000000.0 + 155428000000.0,
)
_TOYOTA_2019_CLEARED = {
    "pl_cost_of_sales", "pl_gross_profit", "pl_sga", "pl_operating_profit", "pl_ordinary_profit",
    "pl_nonoperating_income", "bs_total_liabilities", "bs_current_assets", "bs_inventory",
}
_TOYOTA_2019_KEPT = (
    "pl_revenue", "pl_net_income", "pl_eps", "bs_total_assets", "bs_total_equity", "bs_bps",
    "cf_operating_cf", "dps", "employees", "issued_shares",
)


def _add_toyota_2019(db, make_fin):
    row = make_fin(**_TOYOTA_2019_OLD)
    db.add(row)
    db.commit()
    return row.id


class TestClearNonConsolidated:
    """連結を作る書類の単体の値で入っていた列を NULL へ戻す（#896）。既定の取り直しは従来どおり消さない。"""

    DOCS = {"S100G1ZO": _TOYOTA_USGAAP_S100G1ZO}

    def test_cleared_columns_are_what_the_rule_empties(self):
        df = _df(_TOYOTA_USGAAP_S100G1ZO)
        values = rf.document_columns(df, "E02144", "2019-03-31")
        assert rf.cleared_columns(df, "E02144", "2019-03-31", values) == _TOYOTA_2019_CLEARED

    def test_single_only_company_has_nothing_to_clear(self):
        df = _df(_E01033_SINGLE_S100W071)
        values = rf.document_columns(df, "E01033", "2025-03-31")
        assert rf.cleared_columns(df, "E01033", "2025-03-31", values) == set()

    def test_apply_clears_only_the_rule_columns(self, db, make_fin):
        rid = _add_toyota_2019(db, make_fin)
        r = _run(db, FakeEdinet(self.DOCS), apply=True, clear_nonconsolidated=True,
                 filters=rf.Filters(edinet_codes=("E02144",)))
        after = _snapshot(db, rid)
        for col in _TOYOTA_2019_CLEARED:
            assert after[col] is None, col
        for col in _TOYOTA_2019_KEPT:
            assert after[col] == _TOYOTA_2019_OLD[col], col
        assert set(r.column_counts) == _TOYOTA_2019_CLEARED
        assert r.examples["pl_operating_profit"][0][-2:] == (1326137000000.0, None)
        out = rf.format_report(r)
        assert "--clear-nonconsolidated" in out and "1,326,137,000,000 -> NULL" in out

    def test_dry_run_writes_nothing(self, db, make_fin):
        rid = _add_toyota_2019(db, make_fin)
        before = _snapshot(db, rid)
        r = _run(db, FakeEdinet(self.DOCS), clear_nonconsolidated=True)
        assert _snapshot(db, rid) == before
        assert db.query(AppSetting).count() == 0          # 進捗も書かない
        assert set(r.column_counts) == _TOYOTA_2019_CLEARED   # 消える見込みは数えて出す
        assert "dry-run" in rf.format_report(r)

    def test_without_the_flag_nothing_is_cleared(self, db, make_fin):
        rid = _add_toyota_2019(db, make_fin)
        before = _snapshot(db, rid)
        r = _run(db, FakeEdinet(self.DOCS), apply=True,
                 filters=rf.Filters(edinet_codes=("E02144",)))
        assert _snapshot(db, rid) == before
        assert r.changed_rows == 0
        assert "単体の値の掃除: なし" in rf.format_report(r)

    def test_single_only_company_is_not_cleared(self, db, make_fin):
        row = make_fin(edinet_code="E01033", year=2025, period_end=date(2025, 3, 31),
                       doc_id="S100W071", accounting_standard="JGAAP",
                       pl_revenue=14661000000.0, pl_operating_profit=1820000000.0,
                       pl_ordinary_profit=1882000000.0, bs_total_liabilities=6400000000.0)
        db.add(row)
        db.commit()
        before = _snapshot(db, row.id)
        r = _run(db, FakeEdinet({"S100W071": _E01033_SINGLE_S100W071}), apply=True,
                 clear_nonconsolidated=True, filters=rf.Filters(doc_ids=("S100W071",)))
        after = _snapshot(db, row.id)
        for col in ("pl_revenue", "pl_operating_profit", "pl_ordinary_profit", "bs_total_liabilities"):
            assert after[col] == before[col], col
        assert not any(new is None for ex in r.examples.values() for *_, new in ex)

    def test_progress_is_kept_under_its_own_key(self, db, make_fin):
        _add_toyota_2019(db, make_fin)
        _run(db, FakeEdinet(self.DOCS), apply=True, clear_nonconsolidated=True)
        assert get_setting(db, rf.CLEAR_CURSOR_KEY) is not None
        assert get_setting(db, rf.CURSOR_KEY) is None

    def test_cli_flag(self):
        assert rf._parse_args(["--clear-nonconsolidated"]).clear_nonconsolidated is True
        assert rf._parse_args([]).clear_nonconsolidated is False


# ── 再開 ──────────────────────────────────────────────────────────────────

class TestResume:
    DOCS = {f"D{i}": _E00317_S100YHJP for i in range(1, 4)}

    def test_restarts_after_the_last_committed_row(self, db, make_fin):
        ids = _add_rows(db, make_fin, 3)
        with pytest.raises(Crash):
            _run(db, FakeEdinet(self.DOCS, crash_on="D3"), apply=True, commit_every=1)
        db.rollback()
        assert get_setting(db, rf.CURSOR_KEY) == str(ids[1])
        assert _snapshot(db, ids[0])["pl_revenue"] == 439615000000.0   # 処理済みは確定している
        assert _snapshot(db, ids[2])["pl_revenue"] == 93814000000.0

        fake = FakeEdinet(self.DOCS)
        r = _run(db, fake, apply=True, commit_every=1)
        assert fake.calls == ["D3"]                                     # 処理済みの行は飛ばす
        assert r.start_after == ids[1]
        assert _snapshot(db, ids[2])["pl_revenue"] == 439615000000.0
        assert r.cursor == ids[2] and r.remaining == 0

    def test_finished_run_does_nothing_until_restart(self, db, make_fin):
        _add_rows(db, make_fin, 3)
        _run(db, FakeEdinet(self.DOCS), apply=True)
        fake = FakeEdinet(self.DOCS)
        r = _run(db, fake, apply=True)
        assert fake.calls == [] and r.remaining == 0
        assert "全件処理済み" in rf.format_report(r)

        fake = FakeEdinet(self.DOCS)
        _run(db, fake, apply=True, restart=True)
        assert fake.calls == ["D1", "D2", "D3"]

    def test_limit_keeps_the_cursor(self, db, make_fin):
        ids = _add_rows(db, make_fin, 3)
        _run(db, FakeEdinet(self.DOCS), apply=True, limit=2)
        assert get_setting(db, rf.CURSOR_KEY) == str(ids[1])
        fake = FakeEdinet(self.DOCS)
        _run(db, fake, apply=True)
        assert fake.calls == ["D3"]

    def test_narrowed_run_neither_reads_nor_writes_the_cursor(self, db, make_fin):
        ids = _add_rows(db, make_fin, 3)
        D.upsert_setting(db, rf.CURSOR_KEY, str(ids[2]))
        fake = FakeEdinet(self.DOCS)
        r = _run(db, fake, apply=True, filters=rf.Filters(edinet_codes=("E90001",)))
        assert fake.calls == ["D1"]
        assert not r.use_cursor
        assert get_setting(db, rf.CURSOR_KEY) == str(ids[2])


# ── 締切 ──────────────────────────────────────────────────────────────────

class TestDeadline:
    T0 = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    DOCS = {f"D{i}": _E00317_S100YHJP for i in range(1, 4)}

    def test_stops_before_the_deadline_and_commits(self, db, make_fin):
        ids = _add_rows(db, make_fin, 3)
        fake = FakeEdinet(self.DOCS)
        # 1件目の後: 締切まで1分＝次の1件（余白2分）が入らない
        r = _run(db, fake, apply=True, deadline=self.T0 + timedelta(minutes=10),
                 now=lambda: self.T0 + timedelta(minutes=9))
        assert fake.calls == ["D1"]                       # 新しい書類を取りに行かない
        assert r.stopped_by_deadline and rf.exit_code(r) == 0
        db.rollback()
        assert get_setting(db, rf.CURSOR_KEY) == str(ids[0])           # 処理済みは確定
        assert _snapshot(db, ids[0])["pl_revenue"] == 439615000000.0
        assert r.remaining == 2
        assert "締切の手前で停止" in rf.format_report(r)

    def test_first_document_is_processed_even_past_the_deadline(self, db, make_fin):
        """ADR-0054: 1件目は締切を見ずに必ず処理する（見積りが外れても何かは進む）。"""
        _add_rows(db, make_fin, 3)
        fake = FakeEdinet(self.DOCS)
        _run(db, fake, apply=True, deadline=self.T0, now=lambda: self.T0 + timedelta(hours=1))
        assert fake.calls == ["D1"]

    def test_runs_everything_when_the_deadline_is_far(self, db, make_fin):
        _add_rows(db, make_fin, 3)
        fake = FakeEdinet(self.DOCS)
        r = _run(db, fake, apply=True, deadline=self.T0 + timedelta(hours=8), now=lambda: self.T0)
        assert fake.calls == ["D1", "D2", "D3"] and not r.stopped_by_deadline

    def test_fits_before(self):
        assert rf.fits_before(None, self.T0, 1e9)
        assert rf.fits_before(self.T0 + timedelta(minutes=3), self.T0, 10.0)
        assert not rf.fits_before(self.T0 + timedelta(minutes=2), self.T0, 10.0)


# ── 失敗の数え方 ──────────────────────────────────────────────────────────

class TestFailures:
    def test_all_failed_is_a_failure(self, db, make_fin):
        _add_rows(db, make_fin, 3)
        r = _run(db, FakeEdinet({}), apply=True)
        assert r.fetch_failed == ["D1", "D2", "D3"]
        assert rf.exit_code(r) == rf.EXIT_FAILED

    def test_isolated_failure_is_listed_and_skipped(self, db, make_fin):
        ids = _add_rows(db, make_fin, 3)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP, "D3": _E00317_S100YHJP}), apply=True)
        assert r.fetch_failed == ["D2"] and rf.exit_code(r) == 0
        assert r.cursor == ids[2]
        assert "D2" in rf.format_report(r)

    def test_parse_failure_is_counted_separately(self, db, make_fin):
        _add_rows(db, make_fin, 2)
        r = _run(db, FakeEdinet({"D1": _E00317_S100YHJP, "D2": _TOYOTA_DEI_S100Y8NY}), apply=True)
        assert r.parse_failed == ["D2"] and r.fetch_failed == []

    def test_consecutive_failures_stop_without_advancing(self, db, make_fin, monkeypatch):
        """API キー切れ等。失敗した区間は進捗に含めず、次の実行で取り直す。"""
        monkeypatch.setattr(rf, "EDINET_MAX_CONSECUTIVE_FAILURES", 2)
        ids = _add_rows(db, make_fin, 4)
        fake = FakeEdinet({"D1": _E00317_S100YHJP, "D4": _E00317_S100YHJP})
        r = _run(db, fake, apply=True)
        assert fake.calls == ["D1", "D2", "D3"]
        assert r.aborted_consecutive and rf.exit_code(r) == rf.EXIT_FAILED
        assert get_setting(db, rf.CURSOR_KEY) == str(ids[0])

    def test_trailing_failure_is_retried_next_time(self, db, make_fin):
        ids = _add_rows(db, make_fin, 2)
        _run(db, FakeEdinet({"D1": _E00317_S100YHJP}), apply=True)
        assert get_setting(db, rf.CURSOR_KEY) == str(ids[0])
        fake = FakeEdinet({"D2": _E00317_S100YHJP})
        _run(db, fake, apply=True)
        assert fake.calls == ["D2"]

    def test_fetch_exception_is_a_fetch_failure(self, db, make_fin):
        _add_rows(db, make_fin, 1)

        async def boom(doc_id):
            raise RuntimeError("network")

        r = _run(db, boom, apply=True)
        assert r.fetch_failed == ["D1"]


# ── CLI・日中キュー ───────────────────────────────────────────────────────

class TestCli:
    def test_refuses_a_non_local_target(self, monkeypatch):
        monkeypatch.setattr(D, "DB_TARGET", "prod")
        with pytest.raises(SystemExit, match="ローカル正本専用"):
            rf.main([])

    def test_default_is_dry_run(self):
        assert rf._parse_args([]).apply is False
        assert rf._parse_args(["--apply"]).apply is True


class TestDaytimeJob:
    def test_registered_as_an_apply_job(self):
        job = rd.JOBS["refetch:financials"]
        assert job.argv == ("{python}", "-m", "scripts.refetch_financials", "--apply")
        assert job.parallel_sensitive is False
        assert job.measured_min <= rd.JOB_BUDGET_MIN
