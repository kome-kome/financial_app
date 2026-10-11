"""serializers.py（record_to_dict）のユニットテスト。

api.py の routing から引き上げた純粋関数を、FastAPI app を読み込まず直接検証する。
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serializers

_JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"
# 画面が record_to_dict の入れ子をキー名で読む箇所（`r.bs.cash` / `latest?.pl.revenue` / `r.zscore?.z_roe`）。
_JS_GROUP_KEY = re.compile(r"(\w+)\??\.(bs|pl|cf|val|nc|nonfin|zscore)\??\.(\w+)")
# `charts.bs` / `charts.cf` は Chart.js のインスタンスで、API の応答ではない。
_NOT_A_RECORD = {"charts"}


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


def test_record_to_dict_includes_liabilities_split(make_metric):
    # 収集画面の正規化ビューが読む流動負債・固定負債（#928）
    r = make_metric(bs_current_liabilities=700.0, bs_noncurrent_liabilities=300.0)
    d = serializers.record_to_dict(r)
    assert d["bs"]["current_liabilities"] == 700.0
    assert d["bs"]["noncurrent_liabilities"] == 300.0


def test_record_to_dict_includes_interest_bearing_debt(make_metric):
    """BS グラフの有利子負債は VIEW の合計と短期側の小計を読む（JS で構成要素を足さない・#926）。"""
    r = make_metric(interest_bearing_debt=600.0, bs_short_term_debt=100.0,
                    bs_current_portion_long_term_debt=50.0, bs_commercial_paper=30.0,
                    bs_current_portion_bonds_payable=20.0, bs_long_term_debt=200.0,
                    bs_bonds_payable=200.0)
    d = serializers.record_to_dict(r)
    assert d["bs"]["interest_bearing_debt"] == 600.0
    assert d["bs"]["interest_bearing_debt_current"] == 200.0      # 100 + 50 + 30 + 20


def test_interest_bearing_debt_current_is_blank_when_the_total_is_blank(make_metric):
    """合計が NULL（負債の側を観測していない・#915）なら短期側も 0 ではなく NULL。"""
    d = serializers.record_to_dict(make_metric(interest_bearing_debt=None))
    assert d["bs"]["interest_bearing_debt"] is None
    assert d["bs"]["interest_bearing_debt_current"] is None


def test_every_key_the_screens_read_is_serialized(make_metric):
    """static/js が読む入れ子のキーは、すべて record_to_dict が出している（#928）。

    出していないキーを JS が読むと undefined になり、エラーを出さずに空欄で表示される。
    `const z = latest.zscore || {}; z[k]` のように別名を経由する読み方は拾えない
    （company.js の Zスコア 8 キーがこの形）。
    """
    d = serializers.record_to_dict(make_metric())
    refs = {}
    for js in sorted(_JS_DIR.glob("*.js")):
        for m in _JS_GROUP_KEY.finditer(js.read_text(encoding="utf-8")):
            base, group, key = m.groups()
            if base in _NOT_A_RECORD:
                continue
            refs.setdefault((group, key), js.name)
    # 正規表現が腐って何も拾わなくなったら、照合が空振りで通ってしまう
    assert ("bs", "current_liabilities") in refs
    missing = sorted(f"{g}.{k} ({f})" for (g, k), f in refs.items() if k not in d[g])
    assert not missing, f"画面が読むのに record_to_dict が出していないキー: {missing}"
