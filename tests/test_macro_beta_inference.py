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

import macro_beta_inference as mbi
from macro_beta_inference import (
    _drop_unusable_macro,
    build_panel,
    persist_allowed,
    select_shared_factors,
    summarize_diagnostics,
)

MACRO_TEST_NAMES = ["macro_usdjpy_yoy", "macro_sp500_yoy"]
_TEST_SERIES = {"macro_usdjpy_yoy": "USDJPY", "macro_sp500_yoy": "SP500"}


# ── フィクスチャ ──────────────────────────────────────────────────────────────

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
    )
    values.update(kwargs)
    return tuple(values[f] for f in FIN_LOAD_FIELDS)


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
        elif "FinancialMetric" in s0:           # FinancialMetric（FIN_LOAD_FIELDS の列指定）
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

        # #541: beta / mu_sector は Deterministic ではないのでトレースに載らない。
        # ここが載る側に戻ったら約584MB の常駐が復活しているということ。
        assert "beta" not in idata.posterior
        assert "mu_sector" not in idata.posterior

        # 代わりに再構成の材料（自由 RV）が揃っていること。
        for name in ("beta_raw", "sigma_stock", "mu_universe", "mu_sector_raw", "sigma_sector"):
            assert name in idata.posterior, name
        assert idata.posterior["beta_raw"].shape[-2:] == (n_stock, n_factor)


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
        # 完全一致で縛る（hyperparams はそのまま macro_beta_meta へ入る＝「どの設定の run か」の
        # 記録そのもの）。max_tree_depth は #540 で足した枠で、**既定は None＝サンプラー既定**。
        assert result.hyperparams == {"draws": 50, "tune": 50, "target_accept": 0.9, "seed": 0,
                                      "chains": 2, "nuts_sampler": "pymc", "init": None,
                                      "max_tree_depth": None}

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


class TestMaxTreeDepthWiring:
    """#540: 軌道長の設定が **pm.sample まで実際に届く**こと。

    `nuts={...}` は外部サンプラー（numpyro）経路では黙って捨てられ、逆に
    `nuts_sampler_kwargs={...}` は純 PyMC 経路で黙って無視される——**どちらも例外も警告も
    出さない**ので、取り違えると「指定したのに効いていない run」が測定結果に混ざる。
    格子の結論が本番へ移るかどうかがここに懸かっている。
    """

    def test_default_none_changes_nothing(self):
        # 既定では pm.sample へ渡る引数が現状と1バイトも変わらないこと。
        assert mbi.nuts_depth_kwargs("numpyro") == {}
        assert mbi.nuts_depth_kwargs(None) == {}

    def test_numpyro_goes_through_nuts_sampler_kwargs(self):
        got = mbi.nuts_depth_kwargs("numpyro", 8)
        assert got == {"nuts_sampler_kwargs": {"nuts_kwargs": {"max_tree_depth": 8}}}

    def test_numpyro_tuple_is_passed_through(self):
        # numpyro は (warmup, sampling) を受ける＝warmup だけ切る案が本番でも指定できる。
        got = mbi.nuts_depth_kwargs("numpyro", (8, 10))
        assert got["nuts_sampler_kwargs"]["nuts_kwargs"]["max_tree_depth"] == (8, 10)

    def test_pymc_path_uses_the_step_kwarg(self):
        assert mbi.nuts_depth_kwargs(None, 8) == {"nuts": {"max_treedepth": 8}}
        assert mbi.nuts_depth_kwargs("pymc", (8, 10)) == {
            "nuts": {"early_max_treedepth": 8, "max_treedepth": 10}}

    def test_unknown_sampler_raises_instead_of_dropping(self):
        with pytest.raises(ValueError):
            mbi.nuts_depth_kwargs("blackjax", 8)

    @pytest.mark.parametrize("text,want", [("8", 8), ("8,10", (8, 10)), (None, None), ("", None)])
    def test_cli_string_parsing(self, text, want):
        assert mbi.parse_max_tree_depth(text) == want

    @pytest.mark.parametrize("bad", ["abc", "0", "21", "7,8,9", "8.5"])
    def test_cli_string_parsing_rejects_garbage(self, bad):
        # 黙って既定へ倒すと「指定したのに効いていない run」が測定へ混ざる。
        with pytest.raises(ValueError):
            mbi.parse_max_tree_depth(bad)

    def test_run_inference_forwards_it_to_pm_sample_and_hyperparams(self, monkeypatch):
        """純 PyMC 経路で実際に pm.sample の kwargs に載ることを実行して確かめる。"""
        pytest.importorskip("pymc")
        import pymc as pm

        monkeypatch.setattr(
            mbi, "select_shared_factors",
            lambda macro, returns, factor_names, max_features: list(
                range(min(len(factor_names), max_features))),
        )
        db, _codes, _sectors = _build_mock_db(n_weeks=100, n_companies=3)

        seen = {}
        orig_sample = pm.sample

        def _spy_sample(*args, **kwargs):
            seen.update(kwargs)
            return orig_sample(*args, **kwargs)

        monkeypatch.setattr(pm, "sample", _spy_sample)

        result = mbi.run_inference(draws=6, tune=6, target_accept=0.9, seed=0, db=db,
                                   macro_names=MACRO_TEST_NAMES, chains=2, max_tree_depth=3)

        # pm.sample は `target_accept` を**この同じ dict へその場で合流**させる
        # （mcmc.py: kwargs["nuts"]["target_accept"] = kwargs.pop("target_accept")）。
        # 両立していること自体が確認事項——`nuts` の中に target_accept を自前で入れると
        # 「二重指定」で落ちる仕様なので、こちらは max_treedepth だけを渡すのが正しい。
        assert seen["nuts"]["max_treedepth"] == 3
        assert seen["nuts"]["target_accept"] == 0.9
        # 測定条件は DB へも残す（後から「どの設定の run か」を hyperparams で辿れる）。
        assert result.hyperparams["max_tree_depth"] == 3


class TestSummarizeDiagnostics:
    """summarize_diagnostics: 収束診断は丸めていない生値であること（Issue #356）。

    az.summary は round_to 省略時に r_hat を小数2桁・ess を整数へ丸める。丸め幅が strict
    ゲート（1.01）と同じ桁のため、丸めたままでは persist_allowed が 1.00/1.01/1.02 の3値
    解像度でしか判定できず、収束改善の効果測定もできない。実 MCMC は回さず az.from_dict の
    手組み idata で検証する（arviz 必須のため CI では importorskip でスキップ）。
    """

    def _idata(self, az, seed=0, n_chains=4, n_draws=250, n_stock=3, n_factor=2):
        """summarize_diagnostics が読む beta/alpha/mu_universe + sample_stats.diverging。

        チェーンごとに平均をずらし r_hat > 1 を確実に生む（すべて同分布だと r_hat≈1.00 に
        張り付き、丸めの有無が観測できない）。
        """
        rng = np.random.default_rng(seed)
        offsets = np.linspace(0.0, 0.25, n_chains).reshape(n_chains, 1, 1, 1)
        beta = rng.normal(size=(n_chains, n_draws, n_stock, n_factor)) + offsets
        posterior = {
            "beta": beta,
            "alpha": rng.normal(size=(n_chains, n_draws, n_stock)) + offsets[:, :, :, 0],
            "mu_universe": rng.normal(size=(n_chains, n_draws, n_factor)) + offsets[:, :, :, 0],
        }
        sample_stats = {"diverging": np.zeros((n_chains, n_draws), dtype=bool)}
        return az.from_dict(posterior=posterior, sample_stats=sample_stats)

    def test_returns_unrounded_values(self):
        az = pytest.importorskip("arviz")
        idata = self._idata(az)
        var_names = ["beta", "alpha", "mu_universe"]
        raw = az.summary(idata, var_names=var_names, kind="diagnostics", round_to="none")

        diag = summarize_diagnostics(idata)
        assert diag["r_hat_max"] == float(raw["r_hat"].max())
        assert diag["ess_bulk_min"] == float(raw["ess_bulk"].min())
        assert diag["ess_tail_min"] == float(raw["ess_tail"].min())

    def test_default_arviz_rounding_would_lose_gate_resolution(self):
        """回帰検知: az.summary 既定は r_hat を2桁・ess を整数へ丸める（＝修正前の挙動）。

        この assert が落ちるときは arviz 側の丸め仕様が変わったとき。そのときは
        summarize_diagnostics の round_to="none" が不要になったかを再判断する。
        """
        az = pytest.importorskip("arviz")
        idata = self._idata(az)
        var_names = ["beta", "alpha", "mu_universe"]
        rounded = az.summary(idata, var_names=var_names, kind="diagnostics")
        raw = az.summary(idata, var_names=var_names, kind="diagnostics", round_to="none")

        # r_hat は小数2桁へ丸められる＝strict ゲート(1.01)と同じ桁の解像度しか残らない
        assert float(rounded["r_hat"].max()) == round(float(raw["r_hat"].max()), 2)
        # ess は整数へ丸められる（生値は非整数）
        assert float(rounded["ess_bulk"].min()).is_integer()
        assert not float(raw["ess_bulk"].min()).is_integer()

    def test_diagnostics_distinguish_values_inside_one_rounding_bucket(self):
        """丸め前提だと同値に潰れる2つの run が、生値では区別できること。

        真値が同じ 2桁バケット（例 1.01）に入る run 同士でも r_hat_max が異なることを確認する。
        これが成り立たないと Issue #341 の「r_hat_max が完全平坦」のような偽の停滞が再発する。
        """
        az = pytest.importorskip("arviz")
        a = summarize_diagnostics(self._idata(az, seed=1))
        b = summarize_diagnostics(self._idata(az, seed=2))
        assert a["r_hat_max"] != b["r_hat_max"]


def _raw_idata(az, seed=0, n_chains=4, n_draws=120, n_stock=7, n_sector=3, n_factor=2):
    """beta を載せず、再構成の材料（自由 RV）だけを持つ合成 idata を返す（#541）。

    dims を明示するのが要点。az.from_dict は省略すると `beta_raw_dim_0` のような自動名を
    付けるため、本番同様の `stock`/`factor`/`sector` にしておかないと `.isel(stock=...)` も
    `post.sizes["stock"]` も効かない。

    チェーンごとに平均をずらして r_hat > 1 を確実に作る（全チェーン同分布だと r_hat≈1.00 に
    張り付き、経路間の一致を見ても何も検証できない）。
    """
    rng = np.random.default_rng(seed)
    off = np.linspace(0.0, 0.25, n_chains).reshape(n_chains, 1, 1)
    posterior = {
        "mu_universe":   rng.normal(size=(n_chains, n_draws, n_factor)) + off,
        "sigma_sector":  np.abs(rng.normal(size=(n_chains, n_draws, n_factor))) + 0.1,
        "mu_sector_raw": rng.normal(size=(n_chains, n_draws, n_sector, n_factor)) + off[:, :, :, None],
        "sigma_stock":   np.abs(rng.normal(size=(n_chains, n_draws, n_factor))) + 0.1,
        "beta_raw":      rng.normal(size=(n_chains, n_draws, n_stock, n_factor)) + off[:, :, :, None],
        "alpha":         rng.normal(size=(n_chains, n_draws, n_stock)) + off,
    }
    coords = {"stock": list(range(n_stock)), "sector": list(range(n_sector)),
              "factor": list(range(n_factor))}
    dims = {"mu_universe": ["factor"], "sigma_sector": ["factor"], "sigma_stock": ["factor"],
            "mu_sector_raw": ["sector", "factor"], "beta_raw": ["stock", "factor"],
            "alpha": ["stock"]}
    sample_stats = {"diverging": np.zeros((n_chains, n_draws), dtype=bool)}
    idata = az.from_dict(posterior=posterior, sample_stats=sample_stats,
                         coords=coords, dims=dims)
    sector_idx = np.array([i % n_sector for i in range(n_stock)])
    return idata, sector_idx


def _naive_beta(post, sector_idx):
    """チャンク分割を使わずに beta 全体を素朴に組む（比較用の参照実装）。"""
    mu_sector = (post["mu_universe"].values[:, :, None, :]
                 + post["mu_sector_raw"].values * post["sigma_sector"].values[:, :, None, :])
    return (mu_sector[:, :, np.asarray(sector_idx), :]
            + post["beta_raw"].values * post["sigma_stock"].values[:, :, None, :])


class TestReconstructBeta:
    """#541: beta をトレースへ載せずに自由 RV から組み直す。

    再構成が厳密でないと、posterior を削った瞬間に macro_beta_loadings が静かに変わる。
    素朴な全体計算を参照実装として突き合わせる。
    """

    def test_chunk_matches_naive_full_reconstruction(self):
        az = pytest.importorskip("arviz")
        idata, sector_idx = _raw_idata(az)
        post = idata.posterior
        expected = _naive_beta(post, sector_idx)

        n_stock = post.sizes["stock"]
        got = np.concatenate(
            [mbi._reconstruct_beta_chunk(post, sector_idx, lo, min(lo + 3, n_stock))
             for lo in range(0, n_stock, 3)],
            axis=2,
        )
        np.testing.assert_allclose(got, expected, rtol=1e-12, atol=0)

    def test_chunk_boundary_not_a_divisor_of_n_stock(self):
        """n_stock がチャンク幅の倍数でないとき、末尾の端数が落ちないこと。"""
        az = pytest.importorskip("arviz")
        idata, sector_idx = _raw_idata(az, n_stock=7)   # 7 は 3 の倍数ではない
        post = idata.posterior
        last = mbi._reconstruct_beta_chunk(post, sector_idx, 6, 7)
        assert last.shape[2] == 1
        np.testing.assert_allclose(last[:, :, 0, :],
                                   _naive_beta(post, sector_idx)[:, :, 6, :],
                                   rtol=1e-12, atol=0)

    def test_summarize_uses_reconstruction_and_matches_direct_beta(self, monkeypatch):
        """summarize の mean/sd が「beta を直接持っていた頃」と一致すること。

        sd は ddof=0（xarray の .std(dim=...) と numpy の既定が揃っている）。事後平均から
        組み直すのでは駄目な理由もここで効く——sigma_stock が確率変数なので
        mean(beta_raw * sigma_stock) ≠ mean(beta_raw) * mean(sigma_stock)。
        """
        az = pytest.importorskip("arviz")
        monkeypatch.setattr(mbi, "BETA_CHUNK_STOCKS", 3)   # 境界跨ぎを強制
        idata, sector_idx = _raw_idata(az)
        beta = _naive_beta(idata.posterior, sector_idx)
        macro_sel = np.random.default_rng(0).normal(size=(40, 2))
        codes = [f"E{i:05d}" for i in range(idata.posterior.sizes["stock"])]

        res = mbi.summarize(idata, ["f0", "f1"], macro_sel, codes, sector_idx)

        expected_mean = beta.mean(axis=(0, 1))
        expected_sd = beta.std(axis=(0, 1))
        for i, code in enumerate(codes):
            for j, f in enumerate(["f0", "f1"]):
                m, s = res.loadings[code][f]
                assert m == pytest.approx(float(expected_mean[i, j]), rel=1e-12)
                assert s == pytest.approx(float(expected_sd[i, j]), rel=1e-12)


class TestDiagnosticsGateUnchanged:
    """#541: 診断ゲートの意味が変わっていないこと。

    persist_allowed の strict 1.01 は **beta の r_hat** に対して較正された値。beta_raw で
    代用すると non-centered の raw パラメータは混合が良いぶん r_hat が小さく出て、ゲートが
    黙って緩くなる。チャンク経路が「beta を直接持っていた頃」と同じ値を返すことを確かめる。
    """

    def test_chunked_path_equals_direct_beta_path(self, monkeypatch):
        az = pytest.importorskip("arviz")
        monkeypatch.setattr(mbi, "BETA_CHUNK_STOCKS", 3)   # 境界跨ぎを強制
        idata, sector_idx = _raw_idata(az)

        # 「beta を Deterministic で持っていた頃」の idata を組み直して参照にする。
        beta = _naive_beta(idata.posterior, sector_idx)
        direct = az.from_dict(
            posterior={"beta": beta,
                       "alpha": idata.posterior["alpha"].values,
                       "mu_universe": idata.posterior["mu_universe"].values},
            sample_stats={"diverging": idata.sample_stats["diverging"].values},
        )

        got = summarize_diagnostics(idata, sector_idx)
        want = summarize_diagnostics(direct)

        assert got["r_hat_max"] == pytest.approx(want["r_hat_max"], rel=1e-12)
        assert got["ess_bulk_min"] == pytest.approx(want["ess_bulk_min"], rel=1e-12)
        assert got["ess_tail_min"] == pytest.approx(want["ess_tail_min"], rel=1e-12)
        assert got["n_divergences"] == want["n_divergences"]


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


# ── 進捗の可視化（2026-08-21 の消えた7時間・#504）──────────────────────────
#
# NUTS は数時間かかるのに progressbar=False で無音になる。**無音は「順調」と「死亡」を
# 区別しない**——実際に7時間走った形跡がどこにも残らず、プロセスが消えたことに12時間
# 気づけなかった。heartbeat はその穴を塞ぐためのもの。

class TestHeartbeat:
    def test_ticks_while_the_block_runs(self, caplog):
        import time
        with caplog.at_level("INFO", logger="macro_beta_inference"):
            with mbi._heartbeat("テスト処理", interval=0.05):
                time.sleep(0.28)
        ticks = [r for r in caplog.records if "[heartbeat]" in r.getMessage()]
        assert len(ticks) >= 2, "長時間ブロック中に heartbeat が刻まれていない"
        assert "テスト処理" in ticks[0].getMessage()

    def test_stops_after_the_block(self, caplog):
        import time
        with caplog.at_level("INFO", logger="macro_beta_inference"):
            with mbi._heartbeat("テスト処理", interval=0.05):
                time.sleep(0.12)
            caplog.clear()
            time.sleep(0.25)
        assert not [r for r in caplog.records if "[heartbeat]" in r.getMessage()], \
            "ブロックを抜けた後も heartbeat が残っている"

    def test_exception_does_not_leak_the_thread(self, caplog):
        import threading
        before = threading.active_count()
        with pytest.raises(ValueError):
            with mbi._heartbeat("テスト処理", interval=0.05):
                raise ValueError("boom")
        assert threading.active_count() <= before, "例外時に heartbeat スレッドが残っている"

    def test_interval_is_five_minutes_by_default(self):
        """既定を短くしても NUTS の中身は分からない＝細かくする意味がない。"""
        assert mbi.HEARTBEAT_SEC == 300.0
