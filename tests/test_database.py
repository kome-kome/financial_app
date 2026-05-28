"""database.py のユニットテスト（in-memory SQLite）。

対象: pack/unpack・upsert_company・upsert_financial（辞書フラット化）・
calc_zscore_normalization（年度別Zスコア）。
calc_growth_rates は PostgreSQL 専用 SQL（LAG OVER・::numeric）のため SQLite では検証不可（対象外）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (
    Company,
    FinancialRecord,
    calc_zscore_normalization,
    pack_elements,
    unpack_elements,
    upsert_company,
    upsert_financial,
)


class TestPackUnpack:
    def test_roundtrip(self):
        rows = [
            {"element": "NetSales", "context": "CurrentYear", "value": "1000"},
            {"element": "Assets", "context": "CurrentYear", "value": "5000"},
        ]
        assert unpack_elements(pack_elements(rows)) == rows

    def test_pack_returns_bytes(self):
        assert isinstance(pack_elements([{"a": 1}]), (bytes, bytearray))


class TestUpsertCompany:
    def test_insert_new(self, db):
        upsert_company(db, {"edinet_code": "E00001", "name": "テストA", "industry": "化学"})
        db.commit()
        obj = db.query(Company).filter_by(edinet_code="E00001").one()
        assert obj.name == "テストA"
        assert obj.industry == "化学"
        assert obj.updated_at is not None

    def test_empty_value_does_not_overwrite(self, db):
        upsert_company(db, {"edinet_code": "E00001", "name": "元名", "industry": "化学"})
        db.commit()
        # 空文字は実値を潰さない／実値は更新する
        upsert_company(db, {"edinet_code": "E00001", "name": "", "industry": "医薬品"})
        db.commit()
        obj = db.query(Company).filter_by(edinet_code="E00001").one()
        assert obj.name == "元名"
        assert obj.industry == "医薬品"

    def test_no_duplicate_on_upsert(self, db):
        upsert_company(db, {"edinet_code": "E00001", "name": "A"})
        db.commit()
        upsert_company(db, {"edinet_code": "E00001", "name": "B"})
        db.commit()
        assert db.query(Company).count() == 1
        assert db.query(Company).one().name == "B"


class TestUpsertFinancial:
    def _data(self, **over):
        d = {
            "edinet_code": "E00001", "year": 2023, "period_end": "2023-03-31",
            "bs": {"total_assets": 5000.0, "bps": 100.0},
            "pl": {"revenue": 1000.0, "eps": 50.0},
            "cf": {"operating_cf": 150.0},
            "derived": {"roe": 8.0},
            "val": {"per": 15.0, "dps": 10.0},
        }
        d.update(over)
        return d

    def test_flattens_sections(self, db):
        obj = upsert_financial(db, self._data())
        db.commit()
        assert obj.bs_total_assets == 5000.0   # bs_ プレフィックス
        assert obj.bs_bps == 100.0
        assert obj.pl_revenue == 1000.0         # pl_ プレフィックス
        assert obj.pl_eps == 50.0
        assert obj.cf_operating_cf == 150.0     # cf_ プレフィックス
        assert obj.roe == 8.0                    # derived はそのまま
        assert obj.per == 15.0                   # val もそのまま
        assert obj.dps == 10.0

    def test_raw_xbrl_json_populated(self, db):
        obj = upsert_financial(db, self._data())
        db.commit()
        assert obj.raw_xbrl_json["bs"]["total_assets"] == 5000.0
        assert set(obj.raw_xbrl_json) == {"bs", "pl", "cf"}

    def test_update_existing_no_duplicate(self, db):
        upsert_financial(db, self._data())
        db.commit()
        upsert_financial(db, self._data(pl={"revenue": 2000.0}))
        db.commit()
        rows = db.query(FinancialRecord).filter_by(edinet_code="E00001", year=2023).all()
        assert len(rows) == 1
        assert rows[0].pl_revenue == 2000.0


class TestZscoreNormalization:
    def _add(self, db, make_fin, year, code, op_margin):
        db.add(make_fin(edinet_code=code, year=year,
                        period_end=f"{year}-03-31", op_margin=op_margin))

    def test_per_year_isolation(self, db, make_fin):
        # 2023: [10,20,30] → mean=20, stdev=10 → z = -1.0/0.0/+1.0
        for i, v in enumerate([10.0, 20.0, 30.0]):
            self._add(db, make_fin, 2023, f"E2023{i}", v)
        # 2022 は別母集団（巨大値）。2023 の z に混ざらないこと
        for i, v in enumerate([100.0, 200.0]):
            self._add(db, make_fin, 2022, f"E2022{i}", v)
        db.commit()
        calc_zscore_normalization(db)
        rows = {r.edinet_code: r for r in db.query(FinancialRecord).filter_by(year=2023).all()}
        assert rows["E20230"].z_op_margin == -1.0
        assert rows["E20231"].z_op_margin == 0.0
        assert rows["E20232"].z_op_margin == 1.0

    def test_fewer_than_two_skipped(self, db, make_fin):
        self._add(db, make_fin, 2023, "E00001", 12.0)
        db.commit()
        calc_zscore_normalization(db)
        r = db.query(FinancialRecord).filter_by(year=2023).one()
        assert r.z_op_margin is None

    def test_specific_year_only(self, db, make_fin):
        for i, v in enumerate([10.0, 20.0, 30.0]):
            self._add(db, make_fin, 2023, f"E2023{i}", v)
        for i, v in enumerate([10.0, 20.0, 30.0]):
            self._add(db, make_fin, 2022, f"E2022{i}", v)
        db.commit()
        calc_zscore_normalization(db, year=2023)
        r2023 = db.query(FinancialRecord).filter_by(year=2023, edinet_code="E20231").one()
        r2022 = db.query(FinancialRecord).filter_by(year=2022, edinet_code="E20221").one()
        assert r2023.z_op_margin == 0.0
        assert r2022.z_op_margin is None  # 指定年度のみ計算

    def test_zero_stdev_fallback(self, db, make_fin):
        # 全て同値 → stdev 0 → `or 1.0` で割り 0.0（ゼロ除算/inf を防ぐ）
        for i in range(3):
            self._add(db, make_fin, 2023, f"E{i:05d}", 50.0)
        db.commit()
        calc_zscore_normalization(db)
        rows = db.query(FinancialRecord).filter_by(year=2023).all()
        assert all(r.z_op_margin == 0.0 for r in rows)
