"""tests/test_macro_beta_inference.py — ADR-0002 M-1 per-stock 階層マクロ・ベータ推論バッチ

build_panel/select_shared_factors は numpy/sklearn のみで完結するため常時実行。
build_hierarchical_model/run_inference は PyMC 必須（requirements-inference.txt）のため
ci.yml（requirements.txt のみ）ではスキップされる（pytest.importorskip("pymc")）。
"""
import math
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from macro_beta_inference import (
    _drop_unusable_macro,
    build_panel,
    persist_allowed,
    select_shared_factors,
)

MACRO_TEST_NAMES = ["macro_usdjpy_yoy", "macro_sp500_yoy"]
_TEST_SERIES = {"macro_usdjpy_yoy": "USDJPY", "macro_sp500_yoy": "SP500"}


# ── フィクスチャ ──────────────────────────────────────────────────────────────

def _make_fin(period_end_str: str, **kwargs):
    defaults = dict(
        edinet_code="E00000", sec_code="1234", company_name="テスト株式会社",
        industry="製造業", period_end=date.fromisoformat(period_end_str),
        bs_total_assets=1.0e5,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _build_mock_db(ref: date = date(2025, 6, 1), n_weeks: int = 120, n_companies: int = 4,
                   macro_names: list[str] = MACRO_TEST_NAMES):
    """合成データ（n社・週次価格・6決算・マクロ2系列）で build_panel が通ることを確認する。"""
    codes = [f"E{i:05d}" for i in range(n_companies)]
    bases = [1000.0 + i * 300 for i in range(n_companies)]
    sectors = ["製造業", "情報通信業"]

    # volume_sum は M-1 系（本テスト）が参照しないため固定値でよい（列数のみ合わせる・Issue #317）。
    price_tuples = [
        (ec, (ref - timedelta(days=(n_weeks - i) * 7)).isoformat(),
         base * math.exp(0.001 * i * (1 + j * 0.2)), 1_000_000.0)
        for j, (ec, base) in enumerate(zip(codes, bases))
        for i in range(n_weeks)
    ]

    fin_list = [
        _make_fin((ref - timedelta(days=365 * (5 - y))).isoformat(),
                  edinet_code=ec, industry=sectors[j % 2],
                  bs_total_assets=(j + 1) * 1.0e5)
        for j, ec in enumerate(codes)
        for y in range(6)
    ]

    companies = [
        SimpleNamespace(edinet_code=ec, sec_code=f"{1000+j}", name=f"テスト{j+1}", industry=sectors[j % 2])
        for j, ec in enumerate(codes)
    ]

    # yoy系列の要件（当期±30日窓・1年前±30日窓）を満たすよう3日おきに5年+マージン分生成
    since = ref - timedelta(days=5 * 366 + 60)
    macro_rows = []
    d = since
    while d <= ref:
        for name in macro_names:
            scode = _TEST_SERIES[name]
            macro_rows.append(SimpleNamespace(series_code=scode, trade_date=d.isoformat(),
                                               close=100.0 + (d - since).days * 0.01))
        d += timedelta(days=3)

    # エンティティ識別ベースのモック（呼び出し順に依存しない）。load_weekly_prices_chunked
    # が Company.edinet_code のコード列 → StockPriceWeekly 列を分割 fetch するようになった
    # ため（Issue #311）、args[0] の str 表現で振り分ける。
    def _query_side_effect(*args):
        mock_q = MagicMock()
        mock_q.filter.return_value = mock_q
        mock_q.filter_by.return_value = mock_q
        mock_q.order_by.return_value = mock_q
        mock_q.first.return_value = None
        s0 = str(args[0]) if args else ""
        if "StockPriceWeekly" in s0:            # StockPriceWeekly 列クエリ
            mock_q.all.return_value = price_tuples
        elif "Company.edinet_code" in s0:       # チャンク分割用のコード列
            mock_q.all.return_value = [(ec,) for ec in codes]
        elif "FinancialMetric" in s0:           # FinancialMetric（全列）
            mock_q.all.return_value = fin_list
        elif "MacroData" in s0:                 # MacroData（マクロ系列）
            mock_q.all.return_value = macro_rows
        else:                                   # db.query(Company)
            mock_q.all.return_value = companies
        return mock_q

    db = MagicMock()
    db.query.side_effect = _query_side_effect
    return db, codes, sectors


# ── build_panel ───────────────────────────────────────────────────────────────

class TestBuildPanel:

    def test_shapes_and_consistency(self):
        db, codes, sectors = _build_mock_db()
        returns, macro, stock_idx, sector_idx, factor_names, edinet_codes, sector_names = build_panel(
            db, macro_names=MACRO_TEST_NAMES
        )
        n_obs = len(returns)
        assert n_obs > 0
        assert macro.shape == (n_obs, len(MACRO_TEST_NAMES))
        assert stock_idx.shape == (n_obs,)
        assert factor_names == MACRO_TEST_NAMES
        assert set(edinet_codes) == set(codes)
        # sector_idx は「銘柄粒度」（mu_sector[sector_idx] が beta を作るため）
        assert sector_idx.shape == (len(edinet_codes),)
        assert stock_idx.min() >= 0
        assert stock_idx.max() < len(edinet_codes)
        assert sector_idx.min() >= 0
        assert sector_idx.max() < len(sector_names)
        assert set(sector_names) <= set(sectors)
        assert not np.isnan(macro).any()
        assert not np.isnan(returns).any()

    def test_empty_prices_raises(self):
        db = MagicMock()
        mock_q = MagicMock()
        mock_q.order_by.return_value = mock_q
        mock_q.filter.return_value = mock_q
        mock_q.filter_by.return_value = mock_q
        mock_q.all.return_value = []
        db.query.side_effect = lambda *a: mock_q
        with pytest.raises(ValueError, match="株価週次履歴"):
            build_panel(db, macro_names=MACRO_TEST_NAMES)

    def test_stock_idx_matches_edinet_codes_order(self):
        """stock_idx[k] が edinet_codes[stock_idx[k]] に正しく対応する（観測粒度）。"""
        db, codes, _sectors = _build_mock_db()
        _returns, _macro, stock_idx, _sector_idx, _factor_names, edinet_codes, _sector_names = build_panel(
            db, macro_names=MACRO_TEST_NAMES
        )
        observed_codes = {edinet_codes[i] for i in stock_idx}
        assert observed_codes <= set(codes)


# ── select_shared_factors ──────────────────────────────────────────────────────

class TestSelectSharedFactors:

    def test_selects_correlated_factor(self):
        rng = np.random.default_rng(0)
        n = 200
        x1 = rng.normal(size=n)          # ノイズ因子（目的変数と無相関）
        x2 = rng.normal(size=n)          # 目的変数と強く相関する因子
        y = 0.8 * x2 + rng.normal(scale=0.1, size=n)
        macro = np.column_stack([x1, x2])
        idx = select_shared_factors(macro, y, ["f1", "f2"], max_features=2)
        assert 1 in idx

    def test_insufficient_samples_returns_empty(self):
        macro = np.random.default_rng(0).normal(size=(3, 2))
        y = np.random.default_rng(1).normal(size=3)
        idx = select_shared_factors(macro, y, ["f1", "f2"], max_features=2)
        assert idx == []

    def test_max_features_cap(self):
        rng = np.random.default_rng(0)
        n = 200
        macro = rng.normal(size=(n, 5))
        y = 0.5 * macro[:, 0] + 0.5 * macro[:, 1] + 0.5 * macro[:, 2] + rng.normal(scale=0.05, size=n)
        idx = select_shared_factors(macro, y, [f"f{i}" for i in range(5)], max_features=2)
        assert len(idx) <= 2

    def test_returns_sorted_indices(self):
        rng = np.random.default_rng(0)
        n = 200
        macro = rng.normal(size=(n, 4))
        y = macro[:, 3] + macro[:, 0] + rng.normal(scale=0.05, size=n)
        idx = select_shared_factors(macro, y, [f"f{i}" for i in range(4)], max_features=4)
        assert idx == sorted(idx)


# ── build_hierarchical_model / run_inference（PyMC 必須・CI では自動スキップ）──────

class TestBuildHierarchicalModel:

    def test_model_builds_and_samples_small(self):
        pymc = pytest.importorskip("pymc")
        from macro_beta_inference import build_hierarchical_model

        rng = np.random.default_rng(0)
        n_stock, n_sector, n_factor = 4, 2, 2
        n_obs = 60
        stock_idx = rng.integers(0, n_stock, size=n_obs)
        sector_idx = np.array([0, 0, 1, 1])  # 銘柄粒度（銘柄→セクター）
        macro = rng.normal(size=(n_obs, n_factor))
        true_beta = np.array([0.3, -0.2])
        returns = macro @ true_beta + rng.normal(scale=0.05, size=n_obs)

        model = build_hierarchical_model(returns, macro, stock_idx, sector_idx,
                                         n_stock=n_stock, n_sector=n_sector, n_factor=n_factor)
        with model:
            idata = pymc.sample(draws=50, tune=50, chains=2, target_accept=0.9,
                               random_seed=0, progressbar=False)

        assert "beta" in idata.posterior
        assert idata.posterior["beta"].shape[-2:] == (n_stock, n_factor)
        assert "mu_sector" in idata.posterior


class TestRunInferenceEndToEnd:

    def test_run_inference_small_converges(self, monkeypatch):
        """小規模合成データで build_panel→select→階層モデル→NUTS→summarize が一気通貫で動くことを確認。
        select_shared_factors 自体は TestSelectSharedFactors で別途検証済み。合成テストデータの
        マクロ系列は線形トレンドで YoY 変動がほぼゼロ（BIC が偶然 0 個選ぶと不安定なテストになる）
        のため、ここではモンキーパッチで「因子が選ばれた後」のパイプラインを確定的に検証する。
        収束基準（r_hat<1.01）そのものではなく「エンドツーエンドで壊れない・診断値が出る」ことの検証
        （draws/tune/chains は CI 実行時間を抑えるため最小構成。本番は main() の既定値を使う）。"""
        pytest.importorskip("pymc")
        import macro_beta_inference as mbi

        monkeypatch.setattr(
            mbi, "select_shared_factors",
            lambda macro, returns, factor_names, max_features: list(range(min(len(factor_names), max_features))),
        )

        db, codes, _sectors = _build_mock_db(n_weeks=100, n_companies=3)
        result = mbi.run_inference(draws=50, tune=50, target_accept=0.9, seed=0, db=db,
                                   macro_names=MACRO_TEST_NAMES, chains=2)

        assert result.run_id
        assert result.snapshot_date
        assert set(result.selected_factors) <= set(MACRO_TEST_NAMES)
        assert set(result.loadings.keys()) <= set(codes)
        for fmap in result.loadings.values():
            for fname in result.selected_factors:
                assert fname in fmap
        assert result.diagnostics is not None
        assert result.diagnostics["r_hat_max"] is not None
        assert result.hyperparams == {"draws": 50, "tune": 50, "target_accept": 0.9, "seed": 0,
                                      "chains": 2, "nuts_sampler": "pymc", "init": None}

    def test_commits_before_mcmc_sampling(self, monkeypatch):
        """Issue #269: build_panel直後にdb.commit()し、数時間に及ぶMCMC計算中はトランザクション・
        ロックを保持しないこと。pm.sample呼び出し時点でdb.commitが既に呼ばれているかで検証する
        （commitがsampleより後だと本番でAccessShareロックがMCMC計算中も残留し、他セッションの
        ALTER TABLE等をブロックする＝Issue #269で実際に発生した事象）。"""
        pytest.importorskip("pymc")
        import pymc as pm
        import macro_beta_inference as mbi

        monkeypatch.setattr(
            mbi, "select_shared_factors",
            lambda macro, returns, factor_names, max_features: list(range(min(len(factor_names), max_features))),
        )

        db, _codes, _sectors = _build_mock_db(n_weeks=100, n_companies=3)

        committed_before_sample = []
        orig_sample = pm.sample

        def _spy_sample(*args, **kwargs):
            committed_before_sample.append(db.commit.called)
            return orig_sample(*args, **kwargs)

        monkeypatch.setattr(pm, "sample", _spy_sample)

        mbi.run_inference(draws=10, tune=10, target_accept=0.9, seed=0, db=db,
                          macro_names=MACRO_TEST_NAMES, chains=2)

        assert committed_before_sample == [True]


class TestPersistGate:
    """persist_allowed: r_hat ゲート判定（Issue #341 で threshold 可変化）。

    純関数のため PyMC 不要（build_panel と同様に requirements.txt のみの CI でも実行される）。
    """

    def test_converged_persists_at_strict_default(self):
        # strict 既定 1.01：基準を満たす run は persist 許可
        assert persist_allowed(1.005, threshold=1.01, force=False) is True

    def test_marginal_1_02_rejected_at_strict_default(self):
        # chains=2 の構造的 ~1.02 は strict 既定では reject（cron が毎回落ちる原因）
        assert persist_allowed(1.02, threshold=1.01, force=False) is False

    def test_marginal_1_02_persists_under_relaxed_cron_threshold(self):
        # 月次 cron が渡す 1.05：構造的 ~1.02 は自動 persist される
        assert persist_allowed(1.02, threshold=1.05, force=False) is True

    def test_genuinely_unconverged_rejected_even_when_relaxed(self):
        # 緩和 1.05 でも、真に収束していない run（r_hat 大幅超過）は依然 reject
        assert persist_allowed(1.20, threshold=1.05, force=False) is False

    def test_threshold_boundary_is_inclusive(self):
        # threshold ちょうどは許可（<= 判定）、僅かに超えると reject
        assert persist_allowed(1.05, threshold=1.05, force=False) is True
        assert persist_allowed(1.0501, threshold=1.05, force=False) is False

    def test_force_overrides_any_threshold(self):
        # force=True は threshold を無視して常に persist（人手精査後の例外運用）
        assert persist_allowed(1.20, threshold=1.01, force=True) is True

    def test_none_r_hat_is_gate_exempt(self):
        # r_hat_max が算出不能（None）はゲート対象外＝従来挙動を踏襲
        assert persist_allowed(None, threshold=1.01, force=False) is True


class TestDropUnusableMacro:
    """_drop_unusable_macro: 全観測日で None のマクロ特徴量を落とす（Issue #352）。

    純ロジック（_macro_from_cache 依存・numpy/statistics のみ）ゆえ PyMC 不要で CI 実行される。
    """

    def _weekly_dates(self, start: str, end: str):
        d, e = date.fromisoformat(start), date.fromisoformat(end)
        out = []
        while d <= e:
            out.append(d.isoformat())
            d += timedelta(days=7)
        return out

    def _every_n_days(self, start: str, end: str, n: int):
        d, e = date.fromisoformat(start), date.fromisoformat(end)
        out = []
        while d <= e:
            out.append(d.isoformat())
            d += timedelta(days=n)
        return out

    def _cache_and_prices(self):
        # USDJPY: 密な週次（2019-2026）→ macro_usdjpy_yoy は全期間で値が出る
        usdjpy = {d: 150.0 for d in self._weekly_dates("2019-01-01", "2026-07-13")}
        # JP_WEO_GDP_FCAST: 年2回（182日毎）→ zscore の 5年窓に約10点 < 20点閾値で常に None
        weo = {d: 1.5 for d in self._every_n_days("2016-01-01", "2026-07-01", 182)}
        macro_cache = {"USDJPY": usdjpy, "JP_WEO_GDP_FCAST": weo}
        # prices（観測日＝probe 対象）は週次 2020-2026
        rows = [SimpleNamespace(trade_date=d) for d in self._weekly_dates("2020-01-06", "2026-07-13")]
        prices_by_co = {"E00001": rows}
        return macro_cache, prices_by_co

    def test_drops_all_none_feature_keeps_usable(self):
        cache, prices = self._cache_and_prices()
        names = ["macro_usdjpy_yoy", "macro_jp_weo_gdp_fcast_zscore"]
        usable, dropped = _drop_unusable_macro(cache, names, prices)
        assert usable == ["macro_usdjpy_yoy"]
        assert dropped == ["macro_jp_weo_gdp_fcast_zscore"]

    def test_all_usable_drops_nothing(self):
        cache, prices = self._cache_and_prices()
        usable, dropped = _drop_unusable_macro(cache, ["macro_usdjpy_yoy"], prices)
        assert usable == ["macro_usdjpy_yoy"]
        assert dropped == []
