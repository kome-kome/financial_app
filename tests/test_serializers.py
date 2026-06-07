"""serializers.py（record_to_dict）のユニットテスト。

api.py の routing から引き上げた純粋関数を、FastAPI app を読み込まず直接検証する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serializers


def test_record_to_dict_groups_fields(make_metric):
    r = make_metric(
        pl_revenue=1000.0, pl_operating_profit=120.0,
        bs_total_assets=5000.0, cf_operating_cf=150.0,
        roe=8.0, op_margin=12.0, gap_ratio=-15.0, z_roe=1.2,
    )
    d = serializers.record_to_dict(r)
    assert d["edinet_code"] == "E00001"
    assert d["pl"]["revenue"] == 1000.0
    assert d["pl"]["op_margin"] == 12.0          # VIEW 派生も拾う
    assert d["bs"]["total_assets"] == 5000.0
    assert d["cf"]["operating_cf"] == 150.0
    assert d["val"]["roe"] == 8.0
    assert d["zscore"]["z_roe"] == 1.2
    assert d["gap_ratio"] == -15.0
    # 表示グループが揃っている
    assert {"bs", "pl", "cf", "val", "nc", "zscore", "nonfin"} <= set(d)


def test_record_to_dict_unset_fields_are_none(make_metric):
    d = serializers.record_to_dict(make_metric())
    assert d["pl"]["ebitda"] is None
    assert d["val"]["per"] is None


def test_record_to_dict_includes_c1_breakdown_fields(make_metric):
    # C1: 内訳表示に必要な C1/C2 列が bs/pl/nonfin に露出していること
    r = make_metric(
        bs_receivables=300.0, bs_inventory=200.0, bs_buildings=60.0,
        bs_machinery=40.0, bs_ppe_total=150.0, bs_intangible_assets=20.0,
        bs_investments_other_assets=80.0,
        pl_ordinary_profit=110.0, pl_pretax_profit=105.0,
        pl_extraordinary_income=5.0, pl_extraordinary_loss=10.0,
        pl_rd_expenses=70.0, pl_depreciation=45.0,
        employees=12345.0, issued_shares=5_000_000.0,
    )
    d = serializers.record_to_dict(r)
    assert d["bs"]["receivables"] == 300.0
    assert d["bs"]["inventory"] == 200.0
    assert d["bs"]["buildings"] == 60.0
    assert d["bs"]["machinery"] == 40.0
    assert d["bs"]["ppe_total"] == 150.0
    assert d["bs"]["intangible_assets"] == 20.0
    assert d["bs"]["investments_other_assets"] == 80.0
    assert d["pl"]["pretax_profit"] == 105.0
    assert d["pl"]["extraordinary_income"] == 5.0
    assert d["pl"]["extraordinary_loss"] == 10.0
    assert d["pl"]["rd_expenses"] == 70.0
    assert d["pl"]["depreciation"] == 45.0
    assert d["nonfin"]["employees"] == 12345.0
    assert d["nonfin"]["issued_shares"] == 5_000_000.0


def test_record_to_dict_nonfin_unset_is_none(make_metric):
    d = serializers.record_to_dict(make_metric())
    assert d["nonfin"]["employees"] is None
    assert d["nonfin"]["issued_shares"] is None
    assert d["bs"]["ppe_total"] is None
