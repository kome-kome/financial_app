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
    assert {"bs", "pl", "cf", "val", "nc", "zscore"} <= set(d)


def test_record_to_dict_unset_fields_are_none(make_metric):
    d = serializers.record_to_dict(make_metric())
    assert d["pl"]["ebitda"] is None
    assert d["val"]["per"] is None
