"""tests/test_recommend_factor_premia.py — Issue #271 Fama-MacBeth 断面回帰バッチ

fama_macbeth_regression は numpy/statsmodels のみで完結する純粋関数のため合成パネルで検証。
build_period_panel は plugins.macro_snapshots.load_data/build_snapshots 経由でDBへ問い合わせる
ため、tests/test_macro_beta_inference.py の _build_mock_db と同じ MagicMock パターンで検証する。
"""
import math
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from recommend_factor_premia import build_period_panel, fama_macbeth_regression


# ── fama_macbeth_regression: 合成パネル（DB非依存の純粋関数）───────────────────

class TestFamaMacBethRegression:
    def test_recovers_true_beta_across_periods(self):
        rng = np.random.default_rng(42)
        true_beta = {"f1": 0.5, "f2": -0.3}
        factor_names = ["f1", "f2"]
        period_panel = {}
        for t in range(24):
            n = 50
            X = rng.normal(size=(n, 2))
            noise = rng.normal(scale=0.01, size=n)
            y = 0.01 + X[:, 0] * true_beta["f1"] + X[:, 1] * true_beta["f2"] + noise
            period_panel[f"p{t:03d}"] = (X, y)

        result = fama_macbeth_regression(period_panel, factor_names)

        assert result.n_periods == 24
        assert result.mean_b["f1"] == pytest.approx(true_beta["f1"], abs=0.05)
        assert result.mean_b["f2"] == pytest.approx(true_beta["f2"], abs=0.05)
        assert result.newey_west_se["f1"] > 0
        assert result.t_stat["f1"] is not None

    def test_newey_west_se_exceeds_naive_under_autocorrelation(self):
        rng = np.random.default_rng(7)
        factor_names = ["f1"]
        n_periods = 60

        # 期間ごとの真のβをAR(1)的に強く自己相関させる。ノイズを極小にしてOLS推定値
        # β_tがbetas_trueに追従するようにし、その系列自体が自己相関を持つようにする。
        betas_true = []
        persistent = 0.0
        for _ in range(n_periods):
            persistent = 0.9 * persistent + rng.normal(scale=1.0)
            betas_true.append(persistent)

        period_panel = {}
        for t in range(n_periods):
            n = 40
            X = rng.normal(size=(n, 1))
            y = X[:, 0] * betas_true[t] + rng.normal(scale=0.001, size=n)
            period_panel[f"p{t:03d}"] = (X, y)

        result = fama_macbeth_regression(period_panel, factor_names)
        series = np.asarray(result.per_period_betas["f1"])
        naive_se = float(series.std(ddof=1) / math.sqrt(len(series)))

        assert result.newey_west_se["f1"] > naive_se

    def test_skips_periods_where_ols_returns_none(self):
        factor_names = ["f1"]
        period_panel = {
            "p000": (np.empty((0, 1)), np.empty((0,))),   # ols() は空配列でNoneを返す
            "p001": (np.array([[1.0], [2.0], [3.0], [4.0]]), np.array([1.0, 2.0, 3.0, 4.1])),
        }
        result = fama_macbeth_regression(period_panel, factor_names)
        assert result.n_periods == 1

    def test_empty_panel_raises(self):
        with pytest.raises(ValueError):
            fama_macbeth_regression({}, ["f1"])


# ── build_period_panel: load_data/build_snapshots 経由（MagicMockでDB模擬）──────

def _make_fin(period_end_str: str, **kwargs):
    defaults = dict(
        edinet_code="E00000", sec_code="1234", company_name="テスト株式会社",
        industry="製造業", period_end=date.fromisoformat(period_end_str),
        bs_total_assets=1.0e5,
        z_roe=0.5, z_op_margin=0.3, z_revenue=0.2, z_cf_ratio=0.1,
        z_equity_ratio=0.4, z_eps=0.2, gap_ratio=1.0, z_de_ratio=-0.1,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _build_mock_recommend_db(ref: date = date(2025, 6, 1), n_weeks: int = 120, n_companies: int = 3):
    """合成データ（n社・週次価格・6決算）で build_period_panel が通ることを確認する。

    macro を使わないため（macro_names=[]）、load_data の3クエリ
    （StockPriceWeekly→FinancialMetric→Company）のみモックすればよい
    （tests/test_macro_beta_inference.py::_build_mock_db と同じ call_count 方式）。
    """
    codes = [f"E{i:05d}" for i in range(n_companies)]
    bases = [1000.0 + i * 300 for i in range(n_companies)]

    price_tuples = [
        (ec, (ref - timedelta(days=(n_weeks - i) * 7)).isoformat(),
         base * math.exp(0.002 * i * (1 + j * 0.3)))
        for j, (ec, base) in enumerate(zip(codes, bases))
        for i in range(n_weeks)
    ]
    fin_list = [
        _make_fin((ref - timedelta(days=365 * (5 - y))).isoformat(), edinet_code=ec)
        for ec in codes
        for y in range(6)
    ]
    companies = [
        SimpleNamespace(edinet_code=ec, sec_code=f"{1000 + j}", name=f"テスト{j + 1}", industry="製造業")
        for j, ec in enumerate(codes)
    ]

    # エンティティ識別ベースのモック（呼び出し順に依存しない）。load_weekly_prices_chunked
    # が Company.edinet_code のコード列 → StockPriceWeekly 列を分割 fetch するようになった
    # ため（Issue #311）、args[0] の str 表現で振り分ける。
    def _query_side_effect(*args):
        mock_q = MagicMock()
        mock_q.filter.return_value = mock_q
        mock_q.order_by.return_value = mock_q
        s0 = str(args[0]) if args else ""
        if "StockPriceWeekly" in s0:            # StockPriceWeekly 列クエリ
            mock_q.all.return_value = price_tuples
        elif "Company.edinet_code" in s0:       # チャンク分割用のコード列
            mock_q.all.return_value = [(ec,) for ec in codes]
        elif "FinancialMetric" in s0:           # FinancialMetric（全列）
            mock_q.all.return_value = fin_list
        else:                                   # db.query(Company)
            mock_q.all.return_value = companies
        return mock_q

    db = MagicMock()
    db.query.side_effect = _query_side_effect
    return db, codes


class TestBuildPeriodPanel:
    def test_factor_names_match_recommend_metrics_minus_gap_ratio(self):
        # gap_ratio は sector_ols 依存で2025年度以前ほぼ0%充足のため回帰対象から除外する
        # （実データ検証で判明・ADR-0008）。z_momentum は末尾に名前変換されて残る。
        from plugins.recommend import METRICS

        db, _codes = _build_mock_recommend_db()
        period_panel, factor_names = build_period_panel(db, min_companies_per_period=2)

        expected = [m for m in METRICS if m != "gap_ratio"]
        assert factor_names == expected
        assert "gap_ratio" not in factor_names
        assert len(period_panel) > 0
        any_ym = next(iter(period_panel))
        X, y = period_panel[any_ym]
        assert X.shape[1] == len(expected)
        assert len(y) == X.shape[0]

    def test_momentum_column_is_zscored_per_period(self):
        db, _codes = _build_mock_recommend_db()
        period_panel, factor_names = build_period_panel(db, min_companies_per_period=2)
        mom_idx = factor_names.index("z_momentum")

        for X, _y in period_panel.values():
            col = X[:, mom_idx]
            if len(col) >= 4:
                # winsorize→Zスコア化後は期間内平均が概ね0近辺（社数が少ないため厳密0にはならない）
                assert abs(float(col.mean())) < 1.0

    def test_min_companies_per_period_excludes_all_raises(self):
        db, codes = _build_mock_recommend_db()
        with pytest.raises(ValueError, match="min_companies_per_period"):
            build_period_panel(db, min_companies_per_period=len(codes) + 100)

    def test_no_price_data_raises(self):
        db = MagicMock()
        mock_q = MagicMock()
        mock_q.order_by.return_value = mock_q
        mock_q.all.return_value = []
        db.query.side_effect = lambda *a: mock_q
        with pytest.raises(ValueError, match="株価週次履歴"):
            build_period_panel(db)
