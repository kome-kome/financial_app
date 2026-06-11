"""backtest.py のユニットテスト。

api.py の routing から引き上げた分析ロジックを、HTTP 往復なしで直接検証する。
candidate3 の抽出が暴いた「スコア指標が FinancialMetric 専用なのに FinancialRecord を
引いていた＝常に no-data」バグの回帰テストを含む。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest


class TestPercentile:
    def test_empty_returns_zero(self):
        assert backtest.percentile([], 50) == 0.0

    def test_single_element(self):
        assert backtest.percentile([7.0], 95) == 7.0

    def test_linear_interpolation(self):
        # [0,10] の 50 パーセンタイル = 5.0（線形補間）
        assert backtest.percentile([0.0, 10.0], 50) == 5.0


class TestRun:
    def test_no_data_returns_message(self, db):
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["total_candidates"] == 0
        assert res["results"] == []
        assert "message" in res

    def test_scores_on_financial_metric_zscore(self, db, make_metric):
        # 回帰: スコア指標（z_roe 等）は FinancialMetric の VIEW 派生値。run が候補を拾えること。
        # 旧実装は FinancialRecord を引いており z-score が None → 常に total_candidates=0 だった。
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           market_cap=10000.0, z_roe=2.0, z_op_margin=1.5))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           market_cap=8000.0, z_roe=1.0, z_op_margin=0.5))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["total_candidates"] == 2
        assert len(res["results"]) == 2
        # z_roe+z_op_margin が高い E00001 が上位
        assert res["results"][0]["edinet_code"] == "E00001"

    def test_industry_filter(self, db, make_metric):
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           industry="情報・通信業", z_roe=2.0))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           industry="銀行業", z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, "銀行業", None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]

    def test_top_n_limits_results(self, db, make_metric):
        # 5社登録して top_n=2 → results は上位2社のみ、total_candidates は5
        for i, roe in enumerate([5.0, 4.0, 3.0, 2.0, 1.0], 1):
            db.add(make_metric(
                edinet_code=f"E0000{i}", year=2020, period_end="2020-03-31", z_roe=roe
            ))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 2, None, None)
        assert res["total_candidates"] == 5
        assert len(res["results"]) == 2
        assert res["results"][0]["edinet_code"] == "E00001"
        assert res["results"][1]["edinet_code"] == "E00002"

    def test_min_market_cap_filters_small_caps(self, db, make_metric):
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           market_cap=5000.0, z_roe=2.0))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           market_cap=500.0, z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, 1000.0)
        assert res["total_candidates"] == 1
        assert res["results"][0]["edinet_code"] == "E00001"

    def test_period_end_excludes_future_records(self, db, make_metric):
        # months_ago=12: start_date ≈ 2025-07、period_end="2030-03-31" は除外される
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=2.0))
        db.add(make_metric(edinet_code="E00002", year=2030, period_end="2030-03-31",
                           z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 12, 20, None, None)
        assert res["total_candidates"] == 1
        assert res["results"][0]["edinet_code"] == "E00001"

    def test_ranking_by_score(self, db, make_metric):
        # バランス型: z_roe(×1.0) + z_op_margin(×1.0) でスコア算出
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=1.0, z_op_margin=0.5))   # score=1.5
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           z_roe=3.0, z_op_margin=2.0))   # score=5.0
        db.add(make_metric(edinet_code="E00003", year=2020, period_end="2020-03-31",
                           z_roe=2.0, z_op_margin=1.0))   # score=3.0
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00002", "E00003", "E00001"]
