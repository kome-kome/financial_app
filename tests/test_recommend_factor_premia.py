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


# ── 第1段階の estimator 切替（Issue #469）────────────────────────────────────

def _collinear_panel(n_periods: int = 30, n: int = 60, seed: int = 11) -> dict:
    """相関 0.99 の2列を持つ共線パネル。素の断面OLSが打ち消し合う巨大係数を出す状況を作る。"""
    rng = np.random.default_rng(seed)
    panel = {}
    for t in range(n_periods):
        f1 = rng.normal(size=n)
        f2 = 0.99 * f1 + 0.01 * rng.normal(size=n)   # ほぼ同一の説明変数
        X = np.column_stack([f1, f2])
        y = 0.02 * f1 + rng.normal(scale=0.05, size=n)
        panel[f"p{t:03d}"] = (X, y)
    return panel


class TestEstimatorOption:
    def test_default_is_ols_and_matches_explicit_ols(self):
        """既定は ols で、明示指定と完全一致する（既定挙動を動かしていないことの回帰防止）。"""
        panel = _collinear_panel(n_periods=12)
        implicit = fama_macbeth_regression(panel, ["f1", "f2"])
        explicit = fama_macbeth_regression(panel, ["f1", "f2"], estimator="ols")

        assert implicit.estimator == "ols"
        assert implicit.mean_b == explicit.mean_b
        assert implicit.t_stat == explicit.t_stat

    def test_ridge_shrinks_collinear_coefficients(self):
        """共線パネルでは ridge の |λ̄| が ols より小さい（ADR-0021 と同じ向き）。"""
        panel = _collinear_panel()
        ols_res = fama_macbeth_regression(panel, ["f1", "f2"], estimator="ols")
        ridge_res = fama_macbeth_regression(panel, ["f1", "f2"], estimator="ridge")

        max_abs_ols = max(abs(v) for v in ols_res.mean_b.values())
        max_abs_ridge = max(abs(v) for v in ridge_res.mean_b.values())

        assert ridge_res.estimator == "ridge"
        assert ridge_res.n_periods == ols_res.n_periods
        assert max_abs_ridge < max_abs_ols
        # 符号反転ペア（多重共線性の signature）が ols 側に出ていることも確認する
        assert ols_res.mean_b["f1"] * ols_res.mean_b["f2"] < 0

    def test_unknown_estimator_raises(self):
        with pytest.raises(ValueError, match="estimator"):
            fama_macbeth_regression(_collinear_panel(n_periods=3), ["f1", "f2"], estimator="lasso")

    def test_preprocesses_each_period_once(self, monkeypatch):
        """期内前処理は1期あたり1回（Issue #519）。

        条件数の診断が内部で `fit_feature_columns` を呼び直していたため、#509 で ols 経路も
        標準化を通すようになった時点で 1回→2回に倍化していた。
        """
        import plugins.utils as _u

        original = _u.fit_feature_columns
        calls = []

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(_u, "fit_feature_columns", counting)
        panel = _collinear_panel(n_periods=4)
        result = fama_macbeth_regression(panel, ["f1", "f2"], estimator="ols")

        assert result.n_periods == 4
        assert len(calls) == 4

    def test_empty_cross_section_is_skipped_by_the_caller(self):
        """行ゼロの断面は呼び出し側が弾く（Issue #518・共有ヘルパは fail-fast のまま）。"""
        factor_names = ["f1"]
        period_panel = {
            "p000": (np.empty((0, 1)), np.empty((0,))),
            "p001": (np.array([[1.0], [2.0], [3.0], [4.0]]), np.array([1.0, 2.0, 3.0, 4.1])),
        }
        result = fama_macbeth_regression(period_panel, factor_names)
        assert result.n_periods == 1
        assert len(result.condition_numbers) == 1

    def test_condition_numbers_recorded_per_used_period(self):
        """条件数は estimator に依らず同じ尺度で記録する（#469 検証1・共線なら大きくなる）。"""
        panel = _collinear_panel(n_periods=10)
        ols_res = fama_macbeth_regression(panel, ["f1", "f2"], estimator="ols")
        ridge_res = fama_macbeth_regression(panel, ["f1", "f2"], estimator="ridge")

        assert len(ols_res.condition_numbers) == ols_res.n_periods
        assert ols_res.condition_numbers == ridge_res.condition_numbers   # 設計行列の性質
        assert min(ols_res.condition_numbers) > 5.0                       # 共線パネルなので大きい


class TestCliEstimatorPersistGuard:
    def test_ridge_with_persist_exits_before_touching_db(self, monkeypatch):
        """--estimator ridge --persist は DB へ接続する前に落ちる（ADR-0028 の昇格ゲート保護）。"""
        import database
        import recommend_factor_premia as rfp

        opened = {"n": 0}

        def _boom():
            opened["n"] += 1
            raise AssertionError("DB へ接続してはいけない")

        monkeypatch.setattr(database, "SessionLocal", _boom)
        monkeypatch.setattr("sys.argv", ["recommend_factor_premia.py", "--estimator", "ridge",
                                         "--persist"])
        with pytest.raises(SystemExit):
            rfp.main()
        assert opened["n"] == 0

    def test_ridge_without_persist_is_allowed(self, monkeypatch):
        """ridge 単独（表示のみ）は通る＝ガードが estimator 単体を塞いでいないこと。"""
        import database
        import recommend_factor_premia as rfp

        called = {"estimator": None}

        def _fake_compute(db, min_companies_per_period, maxlags, estimator):
            called["estimator"] = estimator
            return rfp.FactorPremiaResult(
                run_id="rfp_test", factor_names=["f1"], mean_b={"f1": 0.1},
                newey_west_se={"f1": 0.05}, t_stat={"f1": 2.0}, p_value={"f1": 0.05},
                n_periods=5, estimator=estimator, condition_numbers=[3.0, 4.0],
            )

        monkeypatch.setattr(database, "SessionLocal", lambda: MagicMock())
        monkeypatch.setattr(rfp, "compute_factor_premia", _fake_compute)
        monkeypatch.setattr("sys.argv", ["recommend_factor_premia.py", "--estimator", "ridge"])
        rfp.main()
        assert called["estimator"] == "ridge"


# ── build_period_panel: load_data/build_snapshots 経由（MagicMockでDB模擬）──────

def _make_fin(period_end_str: str, **kwargs):
    """load_data の列指定クエリが返す行（FIN_LOAD_FIELDS 順の tuple・Issue #459）。

    `_load_data_impl` は ORM 行ではなく tuple を受け取って `_FinRow` へ組み直すので、
    モックも同じ形にする（**行の幅で分岐せず要求した列で決める**＝本物とズレたら落ちる）。
    """
    from plugins.macro_snapshots import FIN_LOAD_FIELDS

    values = {f: None for f in FIN_LOAD_FIELDS}
    values.update(
        edinet_code="E00000", sec_code="1234", company_name="テスト株式会社",
        industry="製造業", period_end=date.fromisoformat(period_end_str),
        bs_total_assets=1.0e5,
        z_roe=0.5, z_op_margin=0.3, z_revenue=0.2, z_cf_ratio=0.1,
        z_equity_ratio=0.4, z_eps=0.2, gap_ratio=1.0, z_de_ratio=-0.1,
    )
    values.update(kwargs)
    return tuple(values[f] for f in FIN_LOAD_FIELDS)


def _build_mock_recommend_db(ref: date = date(2025, 6, 1), n_weeks: int = 120, n_companies: int = 3):
    """合成データ（n社・週次価格・6決算）で build_period_panel が通ることを確認する。

    macro を使わないため（macro_names=[]）、load_data の3クエリ
    （StockPriceWeekly→FinancialMetric→Company）のみモックすればよい
    （tests/test_macro_beta_inference.py::_build_mock_db と同じ call_count 方式）。
    """
    codes = [f"E{i:05d}" for i in range(n_companies)]
    bases = [1000.0 + i * 300 for i in range(n_companies)]

    # volume_sum は M-1 系（本テスト）が参照しないため固定値でよい（列数のみ合わせる・Issue #317）。
    price_tuples = [
        (ec, (ref - timedelta(days=(n_weeks - i) * 7)).isoformat(),
         base * math.exp(0.002 * i * (1 + j * 0.3)), 1_000_000.0)
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
        elif "FinancialMetric" in s0:           # FinancialMetric（FIN_LOAD_FIELDS の列指定）
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
        # mu（producer μ̂）は財務パネルの列ではないので説明変数に入れない（ADR-0030）。
        from plugins.recommend import METRICS

        db, _codes = _build_mock_recommend_db()
        period_panel, factor_names = build_period_panel(db, min_companies_per_period=2)

        expected = [m for m in METRICS if m not in ("gap_ratio", "mu")]
        assert factor_names == expected
        assert "gap_ratio" not in factor_names
        assert "mu" not in factor_names
        assert len(period_panel) > 0
        any_ym = next(iter(period_panel))
        X, y = period_panel[any_ym]
        assert X.shape[1] == len(expected)
        assert len(y) == X.shape[0]

    def test_momentum_column_is_left_raw(self):
        """momentum は **生の log return のまま** 渡す（Issue #519）。

        ここで winsorize→Z スコア化すると、#509 で ols 経路も通るようになった
        `fit_feature_columns` の p1-p99 が二重に掛かり、この1因子だけ推定時と適用時
        （`recommend.compute_momentum_z`）で単位が食い違う。
        """
        db, _codes = _build_mock_recommend_db()
        period_panel, factor_names = build_period_panel(db, min_companies_per_period=2)
        mom_idx = factor_names.index("z_momentum")

        for X, _y in period_panel.values():
            col = X[:, mom_idx]
            if len(col) < 2:
                continue
            # Z スコア化済みなら sd は定義上ちょうど 1.0 になる（`normalize` は ddof=1）。
            assert not math.isclose(float(col.std(ddof=1)), 1.0, rel_tol=1e-9)
            assert float(abs(col).max()) < 1.0            # log return のオーダー

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
