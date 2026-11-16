"""backtest.py のユニットテスト。

api.py の routing から引き上げた分析ロジックを、HTTP 往復なしで直接検証する。
candidate3 の抽出が暴いた「スコア指標が FinancialMetric 専用なのに FinancialRecord を
引いていた＝常に no-data」バグの回帰テストを含む。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import backtest
from plugins.recommend import PRESETS, RUNTIME_METRICS

# バランス型の VIEW 指標（z_momentum は株価から実行時に計算するので含まない）。
_BALANCED_VIEW_METRICS = tuple(m for m in PRESETS["バランス型"] if m not in RUNTIME_METRICS)


@pytest.fixture
def make_metric(make_metric):
    """conftest の make_metric に、バランス型の VIEW 指標の既定値 0.0 を足したもの（#745）。

    backtest は本番の買い推奨と同じく**被覆率 0.5 未満の社を採点しない**。z_roe だけの検体は
    被覆率 1.0/5.1 で候補から消えるので、テストが指定しない列を全社共通の 0.0 で埋める。
    全社共通の定数は断面標準化で 0 になり（散らばり 0 は 1.0 で割る）、順位にも分子にも効かない。
    スコアの分母（値がある指標の |w| 和）は 4.6＝z_momentum（株価が無ければ欠ける）以外の重み。
    明示的に None を渡した列は欠けのまま。
    """
    def _make(**overrides):
        return make_metric(**{**{m: 0.0 for m in _BALANCED_VIEW_METRICS}, **overrides})
    return _make


# 本番の「開始日の時点で買えた社だけ」の絞り込み（#1242）の値。下の autouse で外す前に控える。
_PROD_PRICE_WINDOW_DAYS = backtest.ASOF_PRICE_WINDOW_DAYS


@pytest.fixture(autouse=True)
def _without_tradability_filter(monkeypatch):
    """既存の検体（2020 年の決算・株価なし）は採点・遅れ・コストの規則を測るためのもので、開始日の株価を
    持たない。絞り込みは `TestTradableAtStart` が `tradability_filter` で本番の値に戻して測る。"""
    monkeypatch.setattr(backtest, "ASOF_PRICE_WINDOW_DAYS", None)


@pytest.fixture
def tradability_filter(monkeypatch):
    monkeypatch.setattr(backtest, "ASOF_PRICE_WINDOW_DAYS", _PROD_PRICE_WINDOW_DAYS)


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
        # バランス型: 他の列は全社共通の 0.0 なので z_roe(×1.0) + z_op_margin(×1.0) の和で並ぶ
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=1.0, z_op_margin=0.5))   # 和=1.5
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           z_roe=3.0, z_op_margin=2.0))   # 和=5.0
        db.add(make_metric(edinet_code="E00003", year=2020, period_end="2020-03-31",
                           z_roe=2.0, z_op_margin=1.0))   # 和=3.0
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00002", "E00003", "E00001"]
        assert res["source"] == "recommend"   # 既定 source


# ── 公表前の決算を使わない（#1226）──────────────────────────────────────────────

def _days_before_start(months_ago: int, days: int) -> str:
    """開始日（today − months_ago × 30 日）の days 日前の日付。"""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=months_ago * 30 + days)).isoformat()


class TestFinancialLag:
    def test_lag_is_the_same_constant_as_the_macro_models(self):
        from plugins import macro_snapshots
        assert backtest.FINANCIAL_LAG_DAYS == macro_snapshots.FINANCIAL_LAG_DAYS
        assert 30 < backtest.FINANCIAL_LAG_DAYS < 60      # 下の 30 日前／60 日前の検体が境目をまたぐこと

    def test_unpublished_financials_are_not_used(self, db, make_metric):
        """決算日が開始日の 30 日前の行は選ばれず、60 日前の行は選ばれる。"""
        db.add(make_metric(edinet_code="E00001", year=2025, period_end=_days_before_start(6, 30), z_roe=2.0))
        db.add(make_metric(edinet_code="E00002", year=2025, period_end=_days_before_start(6, 60), z_roe=1.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]
        assert res["total_candidates"] == 1

    def test_lag_changes_the_ranking(self, db, make_metric, monkeypatch):
        """絞り込みが効いていることの確認: 遅れを 0 日（直す前）にすると、未公表の年度で順位が入れ替わる。"""
        old, new = _days_before_start(6, 400), _days_before_start(6, 30)
        db.add(make_metric(edinet_code="E00001", year=2024, period_end=old, z_roe=1.0))
        db.add(make_metric(edinet_code="E00001", year=2025, period_end=new, z_roe=5.0))   # 開始日の時点では未公表
        db.add(make_metric(edinet_code="E00002", year=2024, period_end=old, z_roe=3.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00002", "E00001"]
        assert (res["results"][1]["period_end"], res["results"][1]["year"]) == (old, 2024)
        monkeypatch.setattr(backtest, "FINANCIAL_LAG_DAYS", 0)
        before = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in before["results"]] == ["E00001", "E00002"]
        assert before["results"][0]["period_end"] == new

    def test_response_reports_the_cutoff(self, db, make_metric):
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["financial_lag_days"] == backtest.FINANCIAL_LAG_DAYS
        assert res["financial_cutoff"] == _days_before_start(6, backtest.FINANCIAL_LAG_DAYS)

    def test_no_data_message_reports_the_cutoff(self, db, make_metric):
        db.add(make_metric(edinet_code="E00001", year=2025, period_end=_days_before_start(6, 10), z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["total_candidates"] == 0
        assert res["financial_cutoff"] in res["message"]
        assert res["financial_lag_days"] == backtest.FINANCIAL_LAG_DAYS


# ── scoring source パラメータ化（#206・メタ層の一般化）──────────────────────

class TestScoringSource:
    def test_unknown_source_raises(self, db):
        import pytest
        with pytest.raises(ValueError):
            backtest.run(db, "バランス型", 6, 20, None, None, source="nope")

    def test_mu_weight_is_rejected(self, db, make_metric, monkeypatch):
        """μ̂ は as-of 再現できないため reject する（Issue #423 子4）。

        producer スコアは最新 snapshot_date の1断面しか持たない。黙って None へ
        落とすと「μ̂ 込みで検証した」と誤読されるので、理由付きで止める。
        """
        import pytest
        import plugins.recommend as recommend_mod
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=1.0))
        db.commit()
        monkeypatch.setattr(recommend_mod, "PRESETS",
                            {**recommend_mod.PRESETS, "μ̂検証": {"z_roe": 1.0, "mu": 1.0}})
        with pytest.raises(ValueError, match="mu"):
            backtest.run(db, "μ̂検証", 6, 20, None, None)

    def test_source_valuation_ranks_by_total_return(self, db, make_metric):
        # 期待総リターン = gap_ratio + 配当利回り。gap_ratio が None の銘柄は除外。
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           gap_ratio=20.0, div_yield=5.0))   # 25
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           gap_ratio=30.0, div_yield=0.0))   # 30
        db.add(make_metric(edinet_code="E00003", year=2020, period_end="2020-03-31",
                           gap_ratio=None,  div_yield=9.0))   # 除外（gap_ratio なし）
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="valuation")
        assert res["source"] == "valuation"
        assert res["total_candidates"] == 2
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00002", "E00001"]   # 30 > 25

    def test_source_valuation_caps_outlier_div_yield(self, db, make_metric):
        # 異常な配当利回り（>30%）は 0 とみなす → スコアは gap_ratio のみ
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           gap_ratio=10.0, div_yield=99.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="valuation")
        assert res["results"][0]["score"] == 10.0

    def test_source_net_cash_ranks_by_nc_ratio(self, db, make_metric):
        # nc_ratio = (流動資産 + 投資有価証券×0.7 − 総負債) / (market_cap[百万円]×1e6)
        # market_cap=1000(百万円)=1e9円。
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           market_cap=1000.0, bs_current_assets=2.0e9,
                           bs_investment_securities=0.0, bs_total_liabilities=5.0e8))  # nc=1.5e9 → 1.5
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           market_cap=1000.0, bs_current_assets=1.0e9,
                           bs_investment_securities=0.0, bs_total_liabilities=8.0e8))  # nc=2e8 → 0.2
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="net_cash")
        assert res["source"] == "net_cash"
        assert res["total_candidates"] == 2
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00001", "E00002"]   # 1.5 > 0.2
        assert res["results"][0]["score"] == 1.5

    def test_sell_source_is_gone(self):
        """売りとして画面に出す手法は売り候補ランキング（M-6 の μ̂）だけ（ADR-0071）。
        recommend の符号反転の `sell` は戻さない。"""
        assert backtest.SCORING_SOURCES == ("recommend", "valuation", "net_cash")


# ── z_momentum（Issue #270）: as-of リークセーフなランキング ─────────────────

class TestScoringSourceMomentum:
    def test_z_momentum_ranks_and_is_leak_safe(self, db, make_metric, make_weekly):
        from datetime import date, timedelta
        today = date.today()
        start_date = today - timedelta(days=6 * 30)   # months_ago=6 と一致させる
        old_date    = (start_date - timedelta(days=400)).isoformat()
        recent_date = (start_date - timedelta(days=40)).isoformat()

        # z_roe 等は未設定（None）のままにし、z_momentum のみでスコアが決まるようにする。
        # compute_momentum_z は winsorize の下限（4件）を要するため4社用意する。
        pairs = [
            ("E00001", 1000.0, 4000.0),   # ln(4)    最高
            ("E00002", 1000.0, 2000.0),   # ln(2)
            ("E00003", 1000.0, 1000.0),   # ln(1) = 0
            ("E00004", 2000.0,  500.0),   # ln(0.25) 最低
        ]
        for ec, old_close, recent_close in pairs:
            db.add(make_metric(edinet_code=ec, year=2020, period_end="2020-03-31"))
            db.add(make_weekly(edinet_code=ec, trade_date=old_date, close_last=old_close))
            db.add(make_weekly(edinet_code=ec, trade_date=recent_date, close_last=recent_close))
        db.commit()

        res = backtest.run(db, "バランス型", 6, 20, None, None)
        codes = [r["edinet_code"] for r in res["results"]]
        assert codes == ["E00001", "E00002", "E00003", "E00004"]

        # start_date より後の価格変動を追加してもスコア（ランキング）は変わらない
        db.add(make_weekly(edinet_code="E00004", trade_date=today.isoformat(), close_last=99999.0))
        db.commit()
        res2 = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res2["results"]] == codes


# ── 統計的最適化プリセット（Issue #271・Fama-MacBeth）─────────────────────────

class TestScoringSourceStatisticalPreset:
    def test_resolves_dynamic_preset_weights(self, db, make_metric):
        from database import upsert_recommend_factor_premia
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=2.0))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31", z_roe=1.0))
        upsert_recommend_factor_premia(db, "rfp_1", [
            {"run_id": "rfp_1", "factor_name": "z_roe", "mean_b": 0.5,
             "newey_west_se": None, "t_stat": None, "p_value": None, "n_periods": 12},
        ])
        db.commit()

        res = backtest.run(db, "統計的最適化", 6, 20, None, None)
        assert res["total_candidates"] == 2
        assert res["results"][0]["edinet_code"] == "E00001"   # z_roe が高い方が上位

    def test_falls_back_to_balanced_when_unset(self, db, make_metric):
        # データ未蓄積時はバランス型（z_roe+z_op_margin等）へフォールバック
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=2.0, z_op_margin=1.0))
        db.commit()

        res = backtest.run(db, "統計的最適化", 6, 20, None, None)
        assert res["total_candidates"] == 1
        # バランス型: (z_roe + z_op_margin) / 値がある指標の |w| 和 4.6（結果は3桁丸め）
        assert res["results"][0]["score"] == pytest.approx(3.0 / 4.6, abs=1e-3)


# ── TOPIX に対する超過（#1234）──────────────────────────────────────────────────

class TestIndexExcess:
    """1 社（開始の直後 1000 → 今日 1100＝+10%）と、作り物の TOPIX の系列。"""

    @staticmethod
    def _dates():
        from datetime import date, timedelta
        today = date.today()
        start = today - timedelta(days=6 * 30)
        return start, today

    def _seed(self, db, make_metric, make_weekly, index_rows):
        from database import MacroData
        TestCostBps()._setup_priced_company(db, make_metric, make_weekly)
        for d, c in index_rows:
            db.add(MacroData(series_code=backtest.BENCHMARK_SERIES, series_name="TOPIX", category="equity",
                             trade_date=d, close=c))
        db.add(MacroData(series_code="USDJPY", series_name="USD/JPY", category="fx",     # 別の系列は読まない
                         trade_date=self._dates()[1].isoformat(), close=999.0))
        db.commit()

    def test_index_return_and_excess_match_hand_calculation(self, db, make_metric, make_weekly):
        from datetime import timedelta
        start, today = self._dates()
        rows = [((start - timedelta(days=3)).isoformat(), 500.0),          # 開始日より前＝使わない
                ((start + timedelta(days=2)).isoformat(), 2000.0),         # 開始日以降の最初
                ((start + timedelta(days=60)).isoformat(), 2300.0),
                (today.isoformat(), 2100.0)]                               # 直近
        self._seed(db, make_metric, make_weekly, rows)
        s = backtest.run(db, "バランス型", 6, 20, None, None, cost_bps=10.0)["summary"]
        assert s["index_series"] == backtest.BENCHMARK_SERIES
        assert (s["index_start_date"], s["index_end_date"]) == (rows[1][0], rows[3][0])
        assert s["index_return_pct"] == 5.0                                # 2000 → 2100
        assert s["excess_vs_index_pct"] == pytest.approx(10.0 - 5.0)
        assert s["excess_vs_index_net_pct"] == pytest.approx(10.0 - 0.2 - 5.0)   # 往復 0.2% は保有側だけ
        assert s["excess_return_pct"] == 0.0                               # 既存の「全社平均」の値は変わらない

    def test_missing_index_is_none_not_zero(self, db, make_metric, make_weekly):
        self._seed(db, make_metric, make_weekly, [])
        s = backtest.run(db, "バランス型", 6, 20, None, None)["summary"]
        assert s["index_return_pct"] is None and s["excess_vs_index_pct"] is None
        assert s["excess_vs_index_net_pct"] is None and s["avg_return_pct"] == 10.0

    def test_single_index_row_is_none(self, db, make_metric, make_weekly):
        self._seed(db, make_metric, make_weekly, [(self._dates()[1].isoformat(), 2100.0)])
        s = backtest.run(db, "バランス型", 6, 20, None, None)["summary"]
        assert s["index_return_pct"] is None and s["index_start_date"] == s["index_end_date"]

    def test_existing_keys_do_not_change(self, db, make_metric, make_weekly):
        TestCostBps()._setup_priced_company(db, make_metric, make_weekly)
        before = backtest.run(db, "バランス型", 6, 20, None, None, cost_bps=10.0)["summary"]
        from database import MacroData
        start, today = self._dates()
        for d, c in ((start.isoformat(), 2000.0), (today.isoformat(), 2200.0)):
            db.add(MacroData(series_code=backtest.BENCHMARK_SERIES, trade_date=d, close=c))
        db.commit()
        after = backtest.run(db, "バランス型", 6, 20, None, None, cost_bps=10.0)["summary"]
        new_keys = {"index_series", "index_start_date", "index_end_date", "index_return_pct",
                    "excess_vs_index_pct", "excess_vs_index_net_pct"}
        assert set(after) - set(before) == set() and set(before) >= new_keys
        assert {k: v for k, v in after.items() if k not in new_keys} == \
            {k: v for k, v in before.items() if k not in new_keys}
        assert after["index_return_pct"] == 10.0 and before["index_return_pct"] is None


# ── 摩擦コスト（Issue #316）: cost_bps ────────────────────────────────────────

class TestCostBps:
    def _setup_priced_company(self, db, make_metric, make_weekly,
                               entry_close=1000.0, exit_close=1100.0):
        from datetime import date, timedelta
        today = date.today()
        start_date = today - timedelta(days=6 * 30)   # months_ago=6 と一致させる
        entry_date = (start_date + timedelta(days=5)).isoformat()
        exit_date = today.isoformat()
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=2.0))
        db.add(make_weekly(edinet_code="E00001", trade_date=entry_date, close_last=entry_close))
        db.add(make_weekly(edinet_code="E00001", trade_date=exit_date, close_last=exit_close))
        db.commit()

    def test_default_cost_bps_zero_matches_legacy_output(self, db, make_metric, make_weekly):
        # cost_bps 未指定（デフォルト0）は既存の無印キーと完全一致（後方互換）
        self._setup_priced_company(db, make_metric, make_weekly)
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        r = res["results"][0]
        assert r["return_pct"] == 10.0
        assert r["return_pct_net"] == r["return_pct"]
        s = res["summary"]
        assert s["avg_return_net_pct"] == s["avg_return_pct"]
        assert s["excess_return_net_pct"] == s["excess_return_pct"]
        assert s["benchmark_avg_net_pct"] == s["benchmark_avg_pct"]
        assert s["cost_bps"] == 0.0
        assert res["cost_bps"] == 0.0

    def test_cost_bps_deducts_round_trip_from_return(self, db, make_metric, make_weekly):
        # 片道cost_bps=10（0.10%）→ 往復0.20%を差し引いたネットリターンを併記
        self._setup_priced_company(db, make_metric, make_weekly)
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="recommend", cost_bps=10.0)
        r = res["results"][0]
        assert r["return_pct"] == 10.0
        assert r["return_pct_net"] == 9.8
        s = res["summary"]
        assert s["cost_bps"] == 10.0
        assert s["avg_return_net_pct"] == 9.8
        # ベンチマークも同一銘柄1社のみ＝同額控除のため超過収益は変わらない
        assert s["benchmark_avg_net_pct"] == 9.8
        assert s["excess_return_net_pct"] == 0.0

    def test_cost_bps_no_price_data_stays_none(self, db, make_metric):
        # 株価データが無ければ cost_bps を指定しても None のまま（ゼロ除算等が起きない）
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None, cost_bps=10.0)
        r = res["results"][0]
        assert r["return_pct"] is None
        assert r["return_pct_net"] is None
        assert res["summary"] is None


# ── 上場状態の情報表示・生存者バイアス測定（Issue #315）─────────────────────
#
# backtest はスコアリング対象を is_active でフィルタしない。as-of の start_date 時点で
# 存在した候補を「現在」の上場状態で絞ると、生存者バイアスを逆向きに持ち込むため
# （recommend 等の「今買える銘柄」プラグインとは意図的に扱いが異なる）。

class TestSurvivorshipBiasMeasurement:
    def test_delisted_candidate_not_excluded_from_scoring(self, db, make_metric):
        from datetime import date
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=5.0, is_active=False, delisted_date=date(2021, 1, 1)))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31", z_roe=1.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["total_candidates"] == 2
        assert res["results"][0]["edinet_code"] == "E00001"
        assert res["results"][0]["is_active"] is False
        assert res["results"][0]["delisted_date"] == "2021-01-01"
        assert res["results"][1]["is_active"] is True
        assert res["results"][1]["delisted_date"] is None

    def test_n_delisted_in_top_n_counts_correctly(self, db, make_metric):
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=5.0, is_active=False))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31",
                           z_roe=4.0, is_active=False))
        db.add(make_metric(edinet_code="E00003", year=2020, period_end="2020-03-31", z_roe=1.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["n_delisted_in_top_n"] == 2

    def test_n_delisted_no_price_subset_of_delisted(self, db, make_metric):
        # 価格データが全く無い銘柄は has_price_data=False → n_with_data から自然脱落する対象
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=5.0, is_active=False))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["n_delisted_in_top_n"] == 1
        assert res["n_delisted_no_price"] == 1
        assert res["results"][0]["has_price_data"] is False

    def test_is_active_unset_defaults_to_true(self, db, make_metric):
        # 旧データ（is_active 未設定=NULL）は誤って delisted 扱いしない
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=1.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["results"][0]["is_active"] is True
        assert res["n_delisted_in_top_n"] == 0
        assert res["n_delisted_no_price"] == 0


# ── 開始日の時点で買えた社だけを候補にする（#1242）──────────────────────────────

class TestTradableAtStart:
    """何年も前に決算が止まった社（株価なし）が上位を埋めないこと。開始日より後に廃止した社は残すこと。"""

    @staticmethod
    def _start():
        from datetime import date, timedelta
        return date.today() - timedelta(days=6 * 30)          # months_ago=6 と一致させる

    def _bar(self, make_weekly, ec, days_from_start, close=1000.0):
        from datetime import timedelta
        return make_weekly(edinet_code=ec, trade_date=(self._start() + timedelta(days=days_from_start)).isoformat(),
                           close_last=close)

    def test_production_filter_is_on(self):
        assert _PROD_PRICE_WINDOW_DAYS == 14

    def test_company_without_price_at_start_is_not_a_candidate(self, db, make_metric, make_weekly,
                                                              tradability_filter):
        """本文の症状: 2019 年に決算が止まって廃止した社は、スコアが高くても候補に入らない。"""
        from datetime import date
        db.add(make_metric(edinet_code="E00001", year=2019, period_end="2019-03-31", z_roe=5.0,
                           is_active=False, delisted_date=date(2020, 1, 1)))
        db.add(make_metric(edinet_code="E00002", year=2025, period_end=_days_before_start(6, 100), z_roe=1.0))
        db.add(self._bar(make_weekly, "E00002", -3))
        db.add(self._bar(make_weekly, "E00002", 5, 1000.0))       # 買い（開始日以降の最初の終値）
        db.add(self._bar(make_weekly, "E00002", 6 * 30, 1100.0))  # 今日（直近の終値）
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]
        assert res["total_candidates"] == 1
        assert res["n_untradable_at_start"] == 1
        assert res["n_delisted_in_top_n"] == 0
        assert res["summary"]["n_with_data"] == 1

    def test_company_delisted_after_start_stays(self, db, make_metric, make_weekly, tradability_filter):
        """生存者バイアスを逆に持ち込まない（#315）: 開始日に売買されていた社は、後で廃止しても候補に残る。"""
        from datetime import timedelta
        db.add(make_metric(edinet_code="E00001", year=2025, period_end=_days_before_start(6, 100), z_roe=5.0,
                           is_active=False, delisted_date=self._start() + timedelta(days=30)))
        db.add(make_metric(edinet_code="E00002", year=2025, period_end=_days_before_start(6, 100), z_roe=1.0))
        for ec in ("E00001", "E00002"):
            db.add(self._bar(make_weekly, ec, -3))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00001", "E00002"]
        assert res["results"][0]["is_active"] is False
        assert res["n_untradable_at_start"] == 0

    def test_only_prices_up_to_the_start_count(self, db, make_metric, make_weekly, tradability_filter):
        """開始日より後の終値では「買えた」に数えない（先を読まない）。窓より古い終値も数えない。"""
        db.add(make_metric(edinet_code="E00001", year=2025, period_end=_days_before_start(6, 100), z_roe=5.0))
        db.add(self._bar(make_weekly, "E00001", 5))                                     # 開始日の後だけ
        db.add(make_metric(edinet_code="E00002", year=2025, period_end=_days_before_start(6, 100), z_roe=4.0))
        db.add(self._bar(make_weekly, "E00002", -(_PROD_PRICE_WINDOW_DAYS + 7)))      # 窓より前だけ
        db.add(make_metric(edinet_code="E00003", year=2025, period_end=_days_before_start(6, 100), z_roe=1.0))
        db.add(self._bar(make_weekly, "E00003", -_PROD_PRICE_WINDOW_DAYS))            # 窓の端（含む）
        db.add(make_metric(edinet_code="E00004", year=2025, period_end=_days_before_start(6, 100), z_roe=0.5))
        db.add(self._bar(make_weekly, "E00004", 0))                                     # 開始日（含む）
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00003", "E00004"]
        assert res["n_untradable_at_start"] == 2

    def test_all_dropped_reports_the_count_in_the_message(self, db, make_metric, tradability_filter):
        db.add(make_metric(edinet_code="E00001", year=2019, period_end="2019-03-31", z_roe=1.0))
        db.add(make_metric(edinet_code="E00002", year=2019, period_end="2019-03-31", z_roe=2.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["n_untradable_at_start"] == 2
        assert res["total_candidates"] == 0
        assert "開始日の時点で株価が無かった 2 社" in res["message"]


# ── 株価を使う列を開始日の株価で作り直す（#1248）──────────────────────────────────

class TestAsOfValuation:
    """as-of の行の時価総額・配当利回り・gap_ratio は毎晩、今の株価で上書きされる。そのまま使うと
    「開始日から今までに値下がりした社ほど割安に見えて上位に来る」。開始日の株価で作り直すこと。"""

    YEAR = 2025

    @staticmethod
    def _start():
        from datetime import date, timedelta
        return date.today() - timedelta(days=6 * 30)          # months_ago=6 と一致させる

    def _seed(self, db, make_metric, make_weekly, ec, *, at_start, now, **kw):
        """開始日の終値 at_start・今の終値 now（＝毎晩の収集が保存した株価）の 1 社。

        株数 1e6 なので、時価総額[百万円]は株価と同じ数になる。保存された時価総額・配当利回りは今の株価のもの。
        """
        from datetime import date, timedelta
        dps = kw.pop("dps", None)
        db.add(make_metric(edinet_code=ec, year=self.YEAR, period_end=_days_before_start(6, 100),
                           issued_shares=1.0e6, stock_price=now, market_cap=now, dps=dps,
                           div_yield=(dps / now * 100 if dps else None), **kw))
        for days, close in ((-3, at_start), (5, at_start)):
            db.add(make_weekly(edinet_code=ec, close_last=close,
                               trade_date=(self._start() + timedelta(days=days)).isoformat()))
        db.add(make_weekly(edinet_code=ec, close_last=now, trade_date=date.today().isoformat()))

    @staticmethod
    def _fake_gaps(monkeypatch, gaps=None, calls=None):
        """開始日の gap の回帰（sector_gap_asof.gaps_on）の代わり。渡された終値だけから gap を決める。"""
        import sector_gap_asof

        def fake(db, closes, on_date):
            if calls is not None:
                calls.append((dict(closes), on_date))
            out = gaps if gaps is not None else {
                (ec, TestAsOfValuation.YEAR): 1.0e5 / c for ec, c in closes.items()}
            return dict(out), {"n_universe": len(closes), "n_gap": len(out)}
        monkeypatch.setattr(sector_gap_asof, "gaps_on", fake)

    @staticmethod
    def _ranking(res):
        return [(r["edinet_code"], r["score"]) for r in res["results"]]

    def test_net_cash_does_not_rank_a_company_by_its_fall_after_the_start(
            self, db, make_metric, make_weekly, tradability_filter):
        """本文の症状: 開始日の後に株価が半分になった社は、今の時価総額で割ると比率が倍になって上位に来る。"""
        self._seed(db, make_metric, make_weekly, "E00001", at_start=1000.0, now=500.0,
                   bs_current_assets=1.0e9, bs_total_liabilities=0.0)
        self._seed(db, make_metric, make_weekly, "E00002", at_start=1000.0, now=1000.0,
                   bs_current_assets=1.2e9, bs_total_liabilities=0.0)
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="net_cash")
        assert [r["edinet_code"] for r in res["results"]] == ["E00002", "E00001"]
        assert res["results"][1]["score"] == pytest.approx(1.0)      # 1e9 ÷ 開始日の時価総額 1,000 百万円
        assert res["asof_valuation"] is True
        assert res["asof_gap"] is None                               # net_cash は gap を使わない

    def test_valuation_uses_the_start_date_gap(self, db, make_metric, make_weekly, tradability_filter,
                                               monkeypatch):
        self._seed(db, make_metric, make_weekly, "E00001", at_start=1000.0, now=500.0, gap_ratio=500.0)
        self._seed(db, make_metric, make_weekly, "E00002", at_start=1000.0, now=1000.0, gap_ratio=1.0)
        db.commit()
        self._fake_gaps(monkeypatch, gaps={("E00001", self.YEAR): 10.0, ("E00002", self.YEAR): 20.0})
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="valuation")
        assert self._ranking(res) == [("E00002", 20.0), ("E00001", 10.0)]
        assert res["asof_gap"] == {"n_universe": 2, "n_gap": 2}

    def test_valuation_uses_the_start_date_dividend_yield(self, db, make_metric, make_weekly,
                                                          tradability_filter, monkeypatch):
        """配当利回りも今の株価で割ってある（値下がりした社ほど高い）。開始日の株価で割り直す。"""
        self._seed(db, make_metric, make_weekly, "E00001", at_start=1000.0, now=500.0, dps=50.0)   # 今 10% / 開始日 5%
        self._seed(db, make_metric, make_weekly, "E00002", at_start=1000.0, now=1000.0, dps=60.0)  # 6% / 6%
        db.commit()
        self._fake_gaps(monkeypatch, gaps={("E00001", self.YEAR): 10.0, ("E00002", self.YEAR): 10.0})
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="valuation")
        assert self._ranking(res) == [("E00002", 16.0), ("E00001", 15.0)]

    @pytest.mark.parametrize("source,preset", [("recommend", "割安重視"), ("valuation", "バランス型"),
                                               ("net_cash", "バランス型")])
    def test_price_moves_after_the_start_do_not_change_the_ranking(
            self, db, make_metric, make_weekly, tradability_filter, monkeypatch, source, preset):
        """開始日より後の値（保存された株価・時価総額・配当利回り・gap、開始日の後の終値）を書き換えても、
        順位とスコアは 1 つも変わらない（変わるのはその後のリターンだけ）。"""
        from database import FinancialMetric
        for i, ec in enumerate(("E00001", "E00002", "E00003"), 1):
            self._seed(db, make_metric, make_weekly, ec, at_start=800.0 + 150.0 * i, now=1000.0,
                       dps=10.0 * i, bs_current_assets=1.0e9 * (4 - i), bs_total_liabilities=1.0e8,
                       z_roe=0.3 * i, gap_ratio=5.0 * i)
        db.commit()
        self._fake_gaps(monkeypatch)
        before = self._ranking(backtest.run(db, preset, 6, 20, None, None, source=source))
        assert len(before) == 3
        for r in db.query(FinancialMetric).all():
            r.stock_price, r.market_cap = r.stock_price * 0.3, r.market_cap * 0.3
            r.div_yield, r.gap_ratio = (r.div_yield or 0.0) * 9.0, -100.0
        from datetime import date, timedelta
        db.add(make_weekly(edinet_code="E00003", close_last=1.0,
                           trade_date=(date.today() - timedelta(days=7)).isoformat()))
        db.commit()
        after = self._ranking(backtest.run(db, preset, 6, 20, None, None, source=source))
        assert after == before

    def test_gap_regression_gets_only_prices_up_to_the_start(self, db, make_metric, make_weekly,
                                                             tradability_filter, monkeypatch):
        self._seed(db, make_metric, make_weekly, "E00001", at_start=1000.0, now=500.0)
        db.commit()
        calls: list = []
        self._fake_gaps(monkeypatch, calls=calls)
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="valuation")
        assert calls == [({"E00001": 1000.0}, res["start_date"])]

    @pytest.mark.parametrize("source,preset", [("net_cash", "バランス型"), ("recommend", "高収益重視")])
    def test_gap_regression_runs_only_when_the_method_uses_it(
            self, db, make_metric, make_weekly, tradability_filter, monkeypatch, source, preset):
        import sector_gap_asof

        def must_not_run(*_a, **_k):
            raise AssertionError("gap を使わない手法で回帰を回した")
        monkeypatch.setattr(sector_gap_asof, "gaps_on", must_not_run)
        self._seed(db, make_metric, make_weekly, "E00001", at_start=1000.0, now=500.0,
                   bs_current_assets=1.0e9, bs_total_liabilities=0.0)
        db.commit()
        res = backtest.run(db, preset, 6, 20, None, None, source=source)
        assert res["total_candidates"] == 1
        assert res["asof_gap"] is None

    def test_min_market_cap_uses_the_start_date_market_cap(self, db, make_metric, make_weekly,
                                                            tradability_filter):
        """今の時価総額で絞ると、開始日の後に値上がりした社だけが残る（選んだ理由がその後の値動きになる）。"""
        self._seed(db, make_metric, make_weekly, "E00001", at_start=500.0, now=5000.0)
        self._seed(db, make_metric, make_weekly, "E00002", at_start=5000.0, now=500.0)
        db.commit()
        res = backtest.run(db, "高収益重視", 6, 20, None, 1000.0)
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]

    def test_same_correction_as_the_view_when_the_price_did_not_move(self):
        """開始日の株価が保存された株価と同じなら、作り直した値は VIEW の値（F と基準の遅れを当てた値）と一致する。

        株価 1,000 円・株数 1e6・1 株配当 40 円の生の値は 時価総額 1,000 百万円・配当利回り 4.0%。VIEW は
        F=2・株数の遅れ 1.5 を時価総額へ掛け、F=2・配当の遅れ 4 で配当利回りを割る（ADR-0055）。
        """
        from collections import namedtuple
        Row = namedtuple("Row", "pl_eps bs_bps issued_shares bs_total_equity dps split_factor "
                                "split_shares_lag split_dps_lag market_cap div_yield gap_ratio")
        r = Row(pl_eps=50.0, bs_bps=800.0, issued_shares=1.0e6, bs_total_equity=8.0e8, dps=40.0,
                split_factor=2.0, split_shares_lag=1.5, split_dps_lag=4.0,
                market_cap=3000.0, div_yield=0.5, gap_ratio=99.0)
        out = backtest.asof_row(r, 1000.0, None)
        assert out.market_cap == pytest.approx(r.market_cap)
        assert out.div_yield == pytest.approx(r.div_yield)
        assert out.gap_ratio is None                                  # VIEW の gap（今の株価で回した値）は残さない
        plain = backtest.asof_row(r._replace(split_factor=None, split_shares_lag=None, split_dps_lag=None),
                                  1000.0, 7.0)
        assert (plain.market_cap, plain.div_yield, plain.gap_ratio) == (1000.0, 4.0, 7.0)

    def test_regression_failure_is_reported_in_the_message(self, db, make_metric, make_weekly,
                                                           tradability_filter):
        """回帰が成り立たない（財務の元の行が無い）なら、候補 0 社とその理由を返す（黙って今の gap を使わない）。"""
        self._seed(db, make_metric, make_weekly, "E00001", at_start=1000.0, now=500.0, gap_ratio=50.0)
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None, source="valuation")
        assert res["total_candidates"] == 0
        assert res["asof_gap"]["skipped"]
        assert "gap_ratio を作れませんでした" in res["message"]

    def test_switch_off_keeps_the_view_values(self, db, make_metric):
        """窓（ASOF_PRICE_WINDOW_DAYS）が None のときは絞り込みも作り直しもしない（テスト用の切り替え）。"""
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31", z_roe=1.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert res["asof_valuation"] is False
        assert res["asof_gap"] is None


class TestColumnScoping:
    """`_BACKTEST_FIELDS` が `score_record` の読む列を覆っていること（Issue #489）。

    `score_record` は `getattr(r, metric, None)` で読むため、**列が欠けても例外にならず
    「その指標だけ黙って 0 扱い」**になる（#482 で踏んだ罠と同型）。スコアが少し変わる
    だけなので、ランキングを見ても気づけない。ここで構造的に縛る。
    """

    def test_every_weightable_metric_is_selected(self):
        from backtest import _BACKTEST_FIELDS
        from plugins.recommend import METRICS, RUNTIME_METRICS

        # weights のキーは coerce_params が METRICS へ制限する。RUNTIME_METRICS は
        # VIEW 列ではない（z_momentum は momentum_z 経由・mu は backtest では reject）。
        missing = [m for m in METRICS
                   if m not in RUNTIME_METRICS and m not in _BACKTEST_FIELDS]
        assert not missing, f"weights に来うるのに SELECT していない列: {missing}"

    def test_source_specific_columns_are_selected(self):
        """`source` ごとに `score_record` が直接読む列（METRICS 経由ではない）。"""
        from backtest import _BACKTEST_FIELDS

        for col in ("gap_ratio", "div_yield",                          # valuation
                    "bs_current_assets", "bs_investment_securities",   # net_cash
                    "bs_total_liabilities", "market_cap",
                    "is_active", "delisted_date",                      # 生存バイアス（#315）
                    "period_end", "edinet_code"):                      # 共通
            assert col in _BACKTEST_FIELDS, f"{col} が SELECT に無い"

    def test_every_attribute_read_from_a_record_is_selected(self):
        """`r.<attr>` で読む列を**ソースから拾って**照合する（列の足し忘れを CI で落とす）。

        列指定 Row は存在しない属性で AttributeError を投げるので実行時には気づけるが、
        気づくのは**その source を実際に叩いたとき**だけ。`net_cash` や生存バイアス測定は
        既定経路では踏まないため、テストで全部まとめて見る。
        """
        import re
        from pathlib import Path

        from backtest import _BACKTEST_FIELDS
        from database import FinancialMetric

        src = (Path(__file__).resolve().parents[1] / "backtest.py").read_text(encoding="utf-8")
        view_cols = {c.name for c in FinancialMetric.__table__.columns}
        read = {m for m in re.findall(r"\br\.([a-z_]+)", src)} & view_cols
        # **空集合だと何も検査しない**（ が実バックスペース文字へ化けて空回りした実例あり）
        assert len(read) >= 8, f"読み取り列の抽出に失敗している: {sorted(read)}"
        missing = sorted(read - set(_BACKTEST_FIELDS))
        assert not missing, f"レコードから読むのに SELECT していない列: {missing}"

    def test_scoping_actually_drops_most_of_the_view(self):
        """絞った意味があること（VIEW 97列に対して十分小さい）。"""
        from backtest import _BACKTEST_FIELDS
        from database import FinancialMetric

        view_cols = {c.name for c in FinancialMetric.__table__.columns}
        assert set(_BACKTEST_FIELDS) <= view_cols
        assert len(_BACKTEST_FIELDS) < len(view_cols) / 2


# ── 断面標準化を recommend と揃える（Issue #509）─────────────────────────────
#
# score_record は1レコードずつなので断面統計を持てない。run() が母集団から作って渡す。
# **ここを素通りさせると as-of 再現が是正前のスコアを測る**ことになり、#509 の検証3
# （是正前後のランキング比較）が噛み合わなくなる。

class TestCrossSectionStandardization:
    def test_run_passes_view_stats_to_score_record(self, db, make_metric, monkeypatch):
        """run() が view_stats を渡していること（片側だけ是正する事故への歯止め）。"""
        seen = {}
        orig = backtest.score_record

        def spy(r, source, weights, momentum_z=None, view_stats=None):
            seen["view_stats"] = view_stats
            return orig(r, source, weights, momentum_z, view_stats)

        monkeypatch.setattr(backtest, "score_record", spy)
        for i in range(1, 9):
            db.add(make_metric(edinet_code=f"E{i:05d}", year=2020,
                               period_end="2020-03-31", z_roe=float(i),
                               z_op_margin=float(i) / 2))
        db.commit()
        backtest.run(db, "バランス型", 6, 20, None, None)
        assert seen["view_stats"], "run() が断面統計を作らずに score_record を呼んでいる"
        assert "z_roe" in seen["view_stats"]

    def test_score_record_standardizes_only_with_stats(self, make_metric):
        """view_stats を渡さなければ従来どおり生値の線形結合（後方互換）。"""
        r = make_metric(edinet_code="E00001", z_roe=2.0)
        w = {"z_roe": 1.0}
        assert backtest.score_record(r, "recommend", w) == 2.0
        # mean=0/sd=2 で標準化すると 1.0
        assert backtest.score_record(r, "recommend", w, None, {"z_roe": (0.0, 2.0)}) == 1.0

    def test_outlier_no_longer_silences_a_metric(self, db, make_metric):
        """recommend プラグインと同型: 外れ値1社が他社の重みを呑まない。"""
        n = 40
        for i in range(1, n + 1):
            db.add(make_metric(
                edinet_code=f"E{i:05d}", year=2020, period_end="2020-03-31",
                z_op_margin=(-60.0 if i == 1 else (i % 5) * 0.02),
                z_equity_ratio=float(n - i) / 10.0))
        db.commit()
        both = backtest.run(db, "バランス型", 6, n, None, None,
                            source="recommend")
        # バランス型は z_equity_ratio(0.5) と z_op_margin(1.0) を両方持つ。
        # 是正前は z_op_margin が潰れて z_equity_ratio 単独の順位になっていた。
        order_both = [r["edinet_code"] for r in both["results"]]
        order_eq = [f"E{i:05d}" for i in range(1, n + 1)]   # z_equity_ratio 降順
        assert order_both != order_eq


# ── 本番の買い推奨と同じ採点（Issue #745）─────────────────────────────────────
#
# 以前の score_record は Σw·ẑ をそのまま返し（欠けた指標は実質 0）、被覆率でも除外しなかった。
# 欠けの多い銘柄を系統的に中央へ寄せる＝本番とは別のランキングを測っていた。

class TestSameRuleAsRecommend:
    def test_low_coverage_record_is_not_a_candidate(self, db, make_metric):
        # バランス型（|w| 和 5.1）で z_roe と z_op_margin 以外を欠けにする＝被覆率 2.0/5.1 < 0.5
        low = {m: None for m in _BALANCED_VIEW_METRICS if m not in ("z_roe", "z_op_margin")}
        db.add(make_metric(edinet_code="E00001", year=2020, period_end="2020-03-31",
                           z_roe=5.0, z_op_margin=5.0, **low))
        db.add(make_metric(edinet_code="E00002", year=2020, period_end="2020-03-31", z_roe=1.0))
        db.commit()
        res = backtest.run(db, "バランス型", 6, 20, None, None)
        assert [r["edinet_code"] for r in res["results"]] == ["E00002"]
        assert res["total_candidates"] == 1

    def test_three_paths_rank_alike(self, db, make_metric, monkeypatch):
        """指標が欠けた社を含む断面で、本番・backtest・昇格ゲートの順位が一致する（#745 検証1）。

        旧 backtest の式（欠けは 0・割らない・除外しない）とは順位が違うことも同じ検体で
        確かめる。違わない検体だと、一致のテストは中身に関係なく通ってしまう。
        """
        import asyncio
        from types import SimpleNamespace

        import plugins.recommend as recommend_mod
        from plugins import execute_plugin
        from scripts.preset_ic_gate import build_view_stats, score_period

        weights = {"z_roe": 1.0, "z_op_margin": 1.0, "z_revenue": 0.5}
        monkeypatch.setattr(recommend_mod, "PRESETS",
                            {**recommend_mod.PRESETS, "検証用": weights})
        rows = {   # (z_roe, z_op_margin, z_revenue)。None＝欠け
            "E00001": (2.0, None, 0.0),      # 1つ欠け（被覆率 0.6）
            "E00002": (0.5, 0.5, 0.5),
            "E00003": (1.0, 0.8, None),      # 1つ欠け（被覆率 0.8）
            "E00004": (-1.0, -0.5, 1.0),
            "E00005": (0.0, 1.5, -1.0),
            "E00006": (3.0, None, None),     # 2つ欠け（被覆率 0.4）＝本番に出ない
            "E00007": (-2.0, 0.2, 0.3),
            "E00008": (0.3, -1.0, 2.0),
        }
        for ec, (roe, op, rev) in rows.items():
            db.add(make_metric(edinet_code=ec, year=2020, period_end="2020-03-31",
                               z_roe=roe, z_op_margin=op, z_revenue=rev))
        db.commit()

        # 本番: min_coverage は画面の既定（0.5）
        prod = asyncio.run(execute_plugin(
            recommend_mod.plugin, {"weights": weights, "top_n": 10}, db))
        prod_order = [r["edinet_code"] for r in prod["results"]]

        bt = backtest.run(db, "検証用", 6, 20, None, None)
        bt_order = [r["edinet_code"] for r in bt["results"]]

        records = [SimpleNamespace(edinet_code=ec, z_roe=v[0], z_op_margin=v[1], z_revenue=v[2])
                   for ec, v in rows.items()]
        stats = build_view_stats(records, weights, list(weights))
        scores = score_period(records, weights, stats)
        gate_order = [ec for _, ec in sorted(
            ((s, r.edinet_code) for s, r in zip(scores, records) if s is not None),
            reverse=True)]

        assert prod_order == bt_order == gate_order
        assert len(prod_order) == 7 and "E00006" not in prod_order

        # 旧 backtest の式なら E00006 が首位に来て、E00001 と E00003 の順も入れ替わる
        naive = [ec for _, ec in sorted(
            ((sum(w * recommend_mod.standardize_metric(getattr(r, m), m, stats)
                  for m, w in weights.items() if getattr(r, m) is not None), r.edinet_code)
             for r in records), reverse=True)]
        assert naive[0] == "E00006"
        assert [ec for ec in naive if ec != "E00006"] != prod_order
