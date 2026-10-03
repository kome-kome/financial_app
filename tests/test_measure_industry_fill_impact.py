"""scripts/measure_industry_fill_impact.py の安全装置と判定のテスト（#797・ADR-0065）。

計測そのもの（OOF・gap パネル）は重く SQLite では回らないので、ここでは**正本へ書かないこと**と
**止める条件**だけを縛る。模擬実行が壊れても例外は出ず、もっともらしい値が返る——だから
装置のほうを確かめる。
"""
import pytest
from sqlalchemy.orm import Session

from database import Company
from scripts.measure_industry_fill_impact import (SimulationBroken, apply_fill, compare_today_gap,
                                                  simulated, stop_reasons)


def _industry(db, code):
    return db.query(Company.industry).filter_by(edinet_code=code).scalar()


class TestSimulated:
    def test_writes_are_visible_inside_and_gone_after(self, db, make_company):
        db.add(make_company(edinet_code="E00001", industry=""))
        db.commit()
        with simulated(db) as state:
            db.query(Company).filter_by(edinet_code="E00001").update({"industry": "卸売業"})
            db.commit()     # flush に化ける＝トランザクションの中にだけ残る
            assert _industry(db, "E00001") == "卸売業"
        assert state["rollback_calls"] == 0
        assert _industry(db, "E00001") == ""

    def test_rollback_inside_is_counted_and_raises(self, db, make_company):
        """`run_comparison` は失敗したモデルで rollback する。黙って補完が消えると補完後が補完前を測る。"""
        db.add(make_company(edinet_code="E00001", industry=""))
        db.commit()
        with simulated(db) as state:
            db.query(Company).filter_by(edinet_code="E00001").update({"industry": "卸売業"})
            with pytest.raises(SimulationBroken):
                db.rollback()
            assert _industry(db, "E00001") == "卸売業"     # 消えていない
        assert state["rollback_calls"] == 1
        assert _industry(db, "E00001") == ""

    def test_commit_that_bypasses_the_swap_is_blocked(self, db, make_company):
        db.add(make_company(edinet_code="E00001", industry=""))
        db.commit()
        with simulated(db) as state:
            db.query(Company).filter_by(edinet_code="E00001").update({"industry": "卸売業"})
            with pytest.raises(SimulationBroken):
                Session.commit(db)
        assert state["commit_blocked"] == 1
        assert _industry(db, "E00001") == ""

    def test_methods_are_restored_even_when_the_body_raises(self, db, make_company):
        db.add(make_company(edinet_code="E00001", industry=""))
        db.commit()
        with pytest.raises(ValueError):
            with simulated(db):
                db.query(Company).filter_by(edinet_code="E00001").update({"industry": "卸売業"})
                raise ValueError("計測の途中で落ちた")
        assert "commit" not in vars(db) and "rollback" not in vars(db)
        assert _industry(db, "E00001") == ""
        db.query(Company).filter_by(edinet_code="E00001").update({"industry": "小売業"})
        db.commit()     # 元の commit に戻っている＝ここでは本当に書く
        db.expire_all()
        assert _industry(db, "E00001") == "小売業"


class TestApplyFill:
    def test_fills_with_the_production_selection_and_leaves_nothing(self, db, make_company, make_fin):
        """本番（`_plan_industry_fill`）と同じ選び方で埋め、抜けた後は1行も残さない。"""
        db.add(make_company(edinet_code="E90000", sec_code="9000", industry="サービス業"))
        db.add(make_company(edinet_code="E00033", sec_code="9675", industry=""))     # 非上場・33業種名
        db.add(make_company(edinet_code="E00050", sec_code=None, industry=""))       # 33業種外
        db.add(make_fin(edinet_code="E00033", sec_code="9675", industry=""))
        db.commit()
        codelist = {"E00033": ("サービス業", False),
                    "E00050": ("内国法人・組合（有価証券報告書等の提出義務者以外）", False)}
        with simulated(db):
            fill = apply_fill(db, codelist)
            assert _industry(db, "E00033") == "サービス業"
        assert fill["filled_codes"] == ["E00033"]
        assert fill["filled_financial_records"] == 1
        assert fill["out_of_scope"] == {"内国法人・組合（有価証券報告書等の提出義務者以外）": 1}
        assert _industry(db, "E00033") == ""
        assert _industry(db, "E00050") == ""


def _result(**overrides):
    base = {
        "simulation": {"rollback_calls": 0, "commit_blocked": 0, "fill_visible": True,
                       "empty_before": {"companies": 587}, "empty_after_rollback": {"companies": 587},
                       "industries_before": 33, "industries_after": 33},
        "today_gap": {"identical": True},
        "sector_z": {"others": {"z_roe_sec": {"to_null": 0}, "z_op_margin_sec": {"to_null": 0}}},
        "gap_panel": {"months_where_rows_shrank": []},
        "oof": {"models": {"macro_enet": {"available": True, "errors": [],
                                          "rank_ic": [0.05, 0.03], "n_oof_samples": [1000, 1000]}}},
    }
    base.update(overrides)
    return base


class TestStopReasons:
    def test_lower_rank_ic_alone_does_not_stop(self):
        """空の業種は未来情報（後の廃止の印）。埋めて rank-IC が下がっても止めない（ADR-0065）。"""
        assert stop_reasons(_result()) == []

    def test_today_gap_change_stops(self):
        r = _result(today_gap={"identical": False, "only_before": 0, "only_after": 0,
                               "none_mismatch": 0, "alpha_changed": ["機械"]})
        assert any("当日 gap" in s for s in stop_reasons(r))

    def test_population_shrink_stops(self):
        r = _result(oof={"models": {"macro_enet": {"available": True, "errors": [],
                                                   "rank_ic": [0.05, 0.05],
                                                   "n_oof_samples": [1000, 990]}}})
        assert any("n_oof_samples" in s for s in stop_reasons(r))

    def test_broken_simulation_stops(self):
        sim = dict(_result()["simulation"], rollback_calls=1)
        assert any("rollback" in s for s in stop_reasons(_result(simulation=sim)))

    def test_unrestored_rollback_stops(self):
        sim = dict(_result()["simulation"], empty_after_rollback={"companies": 70})
        assert any("ROLLBACK" in s for s in stop_reasons(_result(simulation=sim)))


class TestCompareTodayGap:
    def test_identical(self):
        g = {"gap": {"E1|2025|2026-03-31": 12.5}, "alpha": {"機械": 10.0}}
        assert compare_today_gap(g, g)["identical"] is True

    def test_any_difference_is_not_identical(self):
        b = {"gap": {"E1|2025|2026-03-31": 12.5}, "alpha": {"機械": 10.0}}
        a = {"gap": {"E1|2025|2026-03-31": 12.5000001}, "alpha": {"機械": 10.0}}
        assert compare_today_gap(b, a)["identical"] is False
