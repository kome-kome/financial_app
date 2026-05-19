"""plugins/gap_analysis.py の execute() テスト"""
import math

import pytest

from plugins.gap_analysis import GapAnalysisPlugin
from tests.conftest import make_company, make_record


@pytest.fixture
def plugin():
    return GapAnalysisPlugin()


class TestGapAnalysis:
    async def test_raises_when_no_data(self, plugin, db):
        """gap_ratio が DB にないと ValueError"""
        with pytest.raises(ValueError, match="先に業種別OLS分析"):
            await plugin.execute({}, db)

    async def test_returns_sorted_asc(self, plugin, db):
        """sort=asc で gap_ratio 昇順（割安が先頭）"""
        for i, ec in enumerate(["E001", "E002", "E003"]):
            make_company(db, edinet_code=ec, sec_code=f"000{i+1}")
            make_record(db, edinet_code=ec, year=2024,
                        period_end="2024-03-31",
                        gap_ratio=-30.0 + i * 30.0,  # -30, 0, 30
                        market_cap=1000.0,
                        predicted_market_cap=900.0)
        result = await plugin.execute({"sort": "asc"}, db)
        assert result["count"] == 3
        # 最も割安（gap_ratio=-30）が先頭
        assert result["results"][0]["gap_ratio"] == -30.0
        assert result["results"][2]["gap_ratio"] == 30.0

    async def test_returns_sorted_desc(self, plugin, db):
        """sort=desc で gap_ratio 降順（割高が先頭）"""
        for i, ec in enumerate(["E001", "E002"]):
            make_company(db, edinet_code=ec, sec_code=f"000{i+1}")
            make_record(db, edinet_code=ec, year=2024,
                        period_end="2024-03-31",
                        gap_ratio=10.0 * (i + 1),
                        market_cap=1000.0)
        result = await plugin.execute({"sort": "desc"}, db)
        assert result["results"][0]["gap_ratio"] == 20.0
        assert result["results"][1]["gap_ratio"] == 10.0

    async def test_year_filter(self, plugin, db):
        for i, ec in enumerate(["E001", "E002"]):
            make_company(db, edinet_code=ec, sec_code=f"000{i+1}")
        make_record(db, edinet_code="E001", year=2023,
                    period_end="2023-03-31", gap_ratio=-10.0)
        make_record(db, edinet_code="E002", year=2024,
                    period_end="2024-03-31", gap_ratio=20.0)

        result = await plugin.execute({"year": 2024}, db)
        assert result["count"] == 1
        assert result["results"][0]["edinet_code"] == "E002"

    async def test_heuristic_decay_within_bounds(self, plugin, db):
        """expected_gap_6m / 12m は OU 過程の減衰で 0 に近づく（参考値）"""
        make_company(db)
        make_record(db, year=2024, period_end="2024-03-31",
                    gap_ratio=50.0, market_cap=1000.0)
        result = await plugin.execute({}, db)
        r = result["results"][0]
        # 12ヶ月後は 6ヶ月後より絶対値が小さい（指数減衰）
        assert abs(r["expected_gap_12m"]) <= abs(r["expected_gap_6m"])
        # 6ヶ月後は元の gap より絶対値が小さい
        assert abs(r["expected_gap_6m"]) < abs(r["gap_ratio"])
        # conv_score は [5, 95] にクランプ
        assert 5 <= r["conv_score_12m"] <= 95
