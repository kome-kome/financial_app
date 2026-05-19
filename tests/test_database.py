"""database.py の ORM・upsert・成長率・Zスコア計算テスト"""
import pytest

from database import (
    Company,
    FinancialRecord,
    StockPriceHistory,
    CollectionLog,
    upsert_company,
    upsert_financial,
    calc_growth_rates,
    calc_zscore_normalization,
)
from tests.conftest import make_company, make_record


# ─── upsert_company ──────────────────────────────────────────────────

class TestUpsertCompany:
    def test_insert_new(self, db):
        data = {"edinet_code": "E000001", "sec_code": "1301",
                "name": "テスト水産", "industry": "水産・農林業"}
        obj = upsert_company(db, data)
        db.commit()
        assert obj.id is not None
        assert obj.edinet_code == "E000001"
        assert obj.name == "テスト水産"

    def test_update_existing(self, db):
        make_company(db, name="旧名称")
        new_data = {"edinet_code": "E000001", "name": "新名称",
                    "industry": "新業種"}
        obj = upsert_company(db, new_data)
        db.commit()
        # 既存レコードを更新（新規作成ではない）
        assert db.query(Company).count() == 1
        assert obj.name == "新名称"
        assert obj.industry == "新業種"

    def test_skips_none_values_on_update(self, db):
        make_company(db, name="保持される名前")
        # name=None を渡しても上書きされない
        upsert_company(db, {"edinet_code": "E000001", "name": None,
                            "industry": "新業種"})
        db.commit()
        c = db.query(Company).filter_by(edinet_code="E000001").first()
        assert c.name == "保持される名前"
        assert c.industry == "新業種"


# ─── upsert_financial ────────────────────────────────────────────────

class TestUpsertFinancial:
    def test_flatten_bs_pl_cf(self, db):
        make_company(db)
        data = {
            "edinet_code": "E000001", "sec_code": "1301",
            "company_name": "テスト水産", "year": 2024,
            "period_end": "2024-03-31",
            "bs": {"total_assets": 1000.0, "total_equity": 600.0},
            "pl": {"revenue": 5000.0, "operating_profit": 500.0, "eps": 100.0},
            "cf": {"operating_cf": 400.0, "free_cf": 300.0},
            "derived": {"op_margin": 10.0, "roe": 8.0},
            "val": {"market_cap": 1500.0, "per": 15.0},
        }
        obj = upsert_financial(db, data)
        db.commit()
        assert obj.bs_total_assets == 1000.0
        assert obj.pl_revenue == 5000.0
        assert obj.cf_operating_cf == 400.0
        assert obj.op_margin == 10.0
        assert obj.market_cap == 1500.0

    def test_raw_xbrl_json_saved(self, db):
        make_company(db)
        data = {
            "edinet_code": "E000001", "year": 2024, "period_end": "2024-03-31",
            "bs": {"total_assets": 100.0},
            "pl": {"revenue": 500.0},
            "cf": {"operating_cf": 50.0},
        }
        obj = upsert_financial(db, data)
        db.commit()
        assert obj.raw_xbrl_json == {
            "bs": {"total_assets": 100.0},
            "pl": {"revenue": 500.0},
            "cf": {"operating_cf": 50.0},
        }

    def test_idempotent(self, db):
        """同一 (edinet_code, year, period_end) で 2 回 upsert すると 1 レコード"""
        make_company(db)
        data = {"edinet_code": "E000001", "year": 2024, "period_end": "2024-03-31",
                "bs": {}, "pl": {"revenue": 1000.0}, "cf": {}}
        upsert_financial(db, data)
        db.commit()
        data["pl"] = {"revenue": 2000.0}
        upsert_financial(db, data)
        db.commit()
        records = db.query(FinancialRecord).filter_by(edinet_code="E000001").all()
        assert len(records) == 1
        assert records[0].pl_revenue == 2000.0  # 上書きされている


# ─── calc_growth_rates ───────────────────────────────────────────────

class TestCalcGrowthRates:
    def test_growth_rates_computed(self, db):
        make_company(db)
        make_record(db, year=2023, period_end="2023-03-31",
                    pl_revenue=1000.0, pl_operating_profit=100.0, pl_eps=50.0)
        make_record(db, year=2024, period_end="2024-03-31",
                    pl_revenue=1200.0, pl_operating_profit=150.0, pl_eps=60.0)

        calc_growth_rates(db)

        latest = db.query(FinancialRecord).filter_by(year=2024).first()
        assert latest.rev_growth == pytest.approx(20.0)  # (1200/1000-1)*100
        assert latest.op_growth == pytest.approx(50.0)   # (150/100-1)*100
        assert latest.eps_growth == pytest.approx(20.0)  # (60/50-1)*100

    def test_first_record_has_no_growth(self, db):
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31", pl_revenue=1000.0)
        calc_growth_rates(db)
        r = db.query(FinancialRecord).filter_by(year=2024).first()
        assert r.rev_growth is None

    def test_zero_prev_does_not_crash(self, db):
        make_company(db)
        # 前期売上 None でゼロ除算を避ける
        make_record(db, year=2023, period_end="2023-03-31",
                    pl_revenue=None, pl_operating_profit=None, pl_eps=None)
        make_record(db, year=2024, period_end="2024-03-31",
                    pl_revenue=1000.0, pl_operating_profit=100.0, pl_eps=50.0)
        calc_growth_rates(db)  # 例外を投げないことが確認
        latest = db.query(FinancialRecord).filter_by(year=2024).first()
        assert latest.rev_growth is None  # 前期が None なので計算されない


# ─── calc_zscore_normalization ───────────────────────────────────────

class TestZscoreNormalization:
    def test_zscore_within_year(self, db):
        # 2024年に 5社、roe が 10/12/14/16/18 → 平均14, std=stdev≈3.16
        make_company(db, edinet_code="E000001", sec_code="0001")
        make_company(db, edinet_code="E000002", sec_code="0002")
        make_company(db, edinet_code="E000003", sec_code="0003")
        make_company(db, edinet_code="E000004", sec_code="0004")
        make_company(db, edinet_code="E000005", sec_code="0005")
        for i, ec in enumerate(["E000001", "E000002", "E000003", "E000004", "E000005"]):
            make_record(db, edinet_code=ec, year=2024,
                        period_end="2024-03-31", roe=10.0 + i * 2)
        calc_zscore_normalization(db, year=2024)
        records = db.query(FinancialRecord).filter_by(year=2024) \
                                            .order_by(FinancialRecord.edinet_code).all()
        # 中央値（roe=14）の z_roe は 0 に近い
        mid = records[2]
        assert abs(mid.z_roe) < 0.01

    def test_different_years_isolated(self, db):
        """異なる年度は別母集団で計算される（CLAUDE.md 制約準拠）"""
        for i, ec in enumerate(["E001", "E002", "E003"]):
            make_company(db, edinet_code=ec, sec_code=f"000{i}")
        # 2023: roe 10/20/30、平均20
        for i, ec in enumerate(["E001", "E002", "E003"]):
            make_record(db, edinet_code=ec, year=2023,
                        period_end="2023-03-31", roe=10.0 + i * 10)
        # 2024: roe 100/200/300、平均200（マクロが違う）
        for i, ec in enumerate(["E001", "E002", "E003"]):
            make_record(db, edinet_code=ec, year=2024,
                        period_end="2024-03-31", roe=100.0 + i * 100)
        calc_zscore_normalization(db)  # year=None → 全年度

        rec_2023 = db.query(FinancialRecord).filter_by(
            edinet_code="E002", year=2023).first()
        rec_2024 = db.query(FinancialRecord).filter_by(
            edinet_code="E002", year=2024).first()
        # 両方とも年度内の中央値 → z=0 付近
        assert abs(rec_2023.z_roe) < 0.01
        assert abs(rec_2024.z_roe) < 0.01

    def test_skips_when_less_than_2_records(self, db):
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31", roe=10.0)
        calc_zscore_normalization(db, year=2024)
        r = db.query(FinancialRecord).filter_by(year=2024).first()
        assert r.z_roe is None  # 1件だけなので Z スコア計算されない


# ─── モデル定義の基本チェック ────────────────────────────────────────

class TestModels:
    def test_company_unique_edinet_code(self, db):
        """edinet_code は unique 制約あり"""
        make_company(db, edinet_code="E000001")
        with pytest.raises(Exception):
            # 同じ edinet_code を 2 回作ろうとすると IntegrityError
            make_company(db, edinet_code="E000001", sec_code="9999")

    def test_financial_record_unique_constraint(self, db):
        """(edinet_code, year, period_end) は unique"""
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31")
        with pytest.raises(Exception):
            make_record(db, year=2024, period_end="2024-03-31")

    def test_stock_price_history_can_create(self, db):
        make_company(db)
        sp = StockPriceHistory(edinet_code="E000001", sec_code="1301",
                               trade_date="2024-05-01", close=1000.0)
        db.add(sp)
        db.commit()
        assert sp.id is not None

    def test_collection_log_defaults(self, db):
        cl = CollectionLog(job_type="full", status="running")
        db.add(cl)
        db.commit()
        assert cl.companies_processed == 0
        assert cl.records_saved == 0
        assert cl.errors_count == 0
