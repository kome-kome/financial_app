"""M-7 時系列 Transformer プラグイン（`plugins/macro_transformer.py`）のテスト。

jax が要るテスト（実行の smoke）は `jax_ok` を使う。扱いは `tests/test_seq_transformer.py` と同じ:
CI で jax が無ければ fail、ローカルで未導入なら skip、導入済みで読めない（Smart App Control の遮断）なら fail。

守ること:
- 比較用のモデルで、本番の札・朝の買い候補・おすすめ銘柄の μ̂ の出所に入らない
- パネルの引数は M-6 のスキーマ既定から読む（書き写さない＝同じ母集団・同じ構築物）
- jax を読めなければ、データを読む前に止まる
- 探索・比較の中（objective_only・dry_run）では保存しない
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import plugins as plugin_registry
from plugins import macro_transformer as mt
from plugins import seq_transformer as st
from plugins.macro_snapshots import M2_M6_DEFAULT_FIN_FEATURES
from plugins.utils import coerce_params
from tests.test_macro_gbdt import _make_fin

plugin = mt.plugin
WPX = namedtuple("WPX", "trade_date close_last volume_sum")


@pytest.fixture(autouse=True)
def _no_tradable_lookup(monkeypatch):
    """as-of の母集団判定を素通しにする（MagicMock の db では比較式が組めない・test_macro_gbdt と同じ）。"""
    monkeypatch.setattr("database.non_tradable_codes", lambda db: set())


@pytest.fixture(scope="module")
def jax_ok():
    if importlib.util.find_spec("jax") is None:
        if os.environ.get("CI"):
            pytest.fail("CI に jax が無い（requirements-dev.txt に jax・jaxlib を入れる）", pytrace=False)
        pytest.skip("jax 未導入")
    try:
        st.require_jax()
    except st.JaxUnavailable as e:
        pytest.fail(str(e), pytrace=False)


def _params(**overrides):
    base = {"d_model": 16, "n_layers": 1, "max_epochs": 5, "patience": 2, "dropout": 0.0}
    base.update(overrides)
    return coerce_params(plugin.params_schema(), base)


def _make_data(n_companies: int = 40, n_weeks: int = 210, seed: int = 0):
    """パネル用（出来高なし）と系列用（出来高あり）の週次株価・財務・会社を返す。"""
    import numpy as np
    rng = np.random.default_rng(seed)
    base = dt.date(2018, 1, 5)
    PX = type("PX", (), {})
    prices, seq_rows, fin, companies = {}, {}, {}, {}
    for ci in range(n_companies):
        ec = f"E{ci:05d}"
        p = 1000.0 + ci * 10
        pr, sr = [], []
        for w in range(n_weeks):
            p *= float(np.exp(rng.normal(0.0, 0.03)))
            d = (base + dt.timedelta(weeks=w)).isoformat()
            row = PX()
            row.trade_date = d
            row.close_last = p
            pr.append(row)
            sr.append(WPX(d, p, float(rng.integers(1_000, 100_000))))
        prices[ec] = pr
        seq_rows[ec] = sr
        pe = (base - dt.timedelta(days=60)).isoformat()
        fin[ec] = [_make_fin(pe, edinet_code=ec, company_name=f"会社{ci}", sec_code=str(1000 + ci),
                             industry="テスト業", per=10.0 + ci * 0.5, roe=5.0 + (ci % 7),
                             bs_total_assets=1e9 * (1 + ci))]
        companies[ec] = SimpleNamespace(edinet_code=ec, name=f"会社{ci}", sec_code=str(1000 + ci),
                                        industry="テスト業")
    return prices, seq_rows, fin, companies


_NO_MACRO_PANEL = {"macro_names": [], "use_momentum": False, "momentum_window": 12,
                   "min_coverage": 0.5, "price_features": []}


def _run(params, db=None, data=None):
    prices, seq_rows, fin, companies = data or _make_data()
    db = db if db is not None else MagicMock()
    with patch("plugins.macro_transformer.load_data", return_value=(prices, fin, companies)), \
         patch("plugins.macro_transformer.load_weekly_prices_chunked", return_value=seq_rows), \
         patch("plugins.macro_transformer.preload_macro", return_value={}), \
         patch("plugins.macro_transformer.get_producer_scores", return_value={}), \
         patch("plugins.macro_transformer._m6_panel_args", return_value=dict(_NO_MACRO_PANEL)):
        return plugin.execute(params, db)


# ── jax 不要 ─────────────────────────────────────────────────────────────────

class TestPluginMeta:
    def test_registered(self):
        assert isinstance(plugin_registry.get_plugin("macro_transformer"), mt.MacroTransformerPlugin)

    def test_label_and_category(self):
        assert plugin.label.endswith("（M-7）")          # 画面の表記は「手法名（M 番号）」（CONTEXT.md）
        assert "×" not in plugin.label
        assert plugin.category == "比較用のモデル"
        assert plugin.heavy is True and plugin.hidden is False

    def test_writes_and_impact(self):
        assert plugin.writes == ("macro_transformer_scores",)
        assert plugin.writes_impact and "朝" in plugin.writes_impact

    def test_not_a_production_or_mu_source_model(self):
        from plugins.recommend import MU_SOURCE_OPTIONS
        from plugins.sell_ranking import MU_SOURCE
        from routers.analysis import PRODUCTION_MODELS
        assert "macro_transformer" not in PRODUCTION_MODELS
        assert "macro_transformer" not in {o["value"] for o in MU_SOURCE_OPTIONS}
        assert MU_SOURCE != "macro_transformer"

    def test_in_model_comparison_as_m7(self):
        from model_comparison import COMPARISON_MODELS
        assert ("macro_transformer", "M-7") in COMPARISON_MODELS

    def test_has_no_search_space(self):
        """探索空間は持たない（ADR-0079・比べるときはスキーマ既定・ADR-0070）。"""
        assert not hasattr(plugin, "tuning_search_space")
        assert not hasattr(plugin, "tuning_seed_base")

    def test_not_snapshotted(self):
        import prediction_snapshots as PS
        assert "macro_transformer" not in PS.MU_SOURCES
        assert "macro_transformer" in PS.NOT_SNAPSHOT_PRODUCERS


class TestParams:
    def test_defaults(self):
        p = coerce_params(plugin.params_schema(), {})
        assert p["fin_features"] == list(M2_M6_DEFAULT_FIN_FEATURES)
        assert p["use_sequence"] is True
        assert (p["d_model"], p["n_layers"], p["max_epochs"], p["patience"]) == (32, 2, 30, 5)
        assert p["lambda_risk"] == 0.0 and p["risk_axis"] == "r2"

    @pytest.mark.parametrize("bad", [
        {"d_model": 80}, {"n_layers": 0}, {"dropout": 0.6}, {"learning_rate": 0.5},
        {"max_epochs": 1}, {"patience": 50}, {"risk_axis": "r1"},
    ])
    def test_out_of_range_is_rejected(self, bad):
        with pytest.raises(ValueError):
            coerce_params(plugin.params_schema(), bad)

    def test_d_model_must_divide_by_heads(self):
        with pytest.raises(ValueError, match="倍数"):
            plugin.execute(_params(d_model=18), MagicMock())

    def test_empty_fin_features(self):
        with pytest.raises(ValueError, match="財務特徴量"):
            plugin.execute(_params(fin_features=[]), MagicMock())


class TestPanelArgs:
    def test_follow_m6_schema_defaults(self):
        """書き写さず M-6 のスキーマ既定から読む＝M-6 の既定を変えれば M-7 も追随する。"""
        from plugins.macro_enet import plugin as m6
        s = m6.params_schema()
        got = mt._m6_panel_args()
        assert got["macro_names"] == (list(s["macro_features"]["default"]) if s["use_macro"]["default"] else [])
        assert got["use_momentum"] == s["use_momentum"]["default"]
        assert got["momentum_window"] == s["momentum_window"]["default"]
        assert got["min_coverage"] == s["min_coverage"]["default"]
        assert got["price_features"] == list(s["price_features"]["default"])


class TestJaxFailureStopsEarly:
    def test_jax_failure_stops_before_loading_data(self, monkeypatch):
        def boom():
            raise st.JaxUnavailable("jax を読めない（テスト）")
        monkeypatch.setattr(st, "require_jax", boom)
        loader = MagicMock(side_effect=AssertionError("データを読んではいけない"))
        with patch("plugins.macro_transformer.load_data", loader):
            with pytest.raises(st.JaxUnavailable):
                plugin.execute(_params(), MagicMock())
        loader.assert_not_called()


class TestExpectedFolds:
    def test_counts_like_walk_forward(self):
        samples = {f"{2020 + i // 12}-{i % 12 + 1:02d}": [([0.0], 0.0, 0)] * 3 for i in range(30)}
        # 最低学習 6＋embargo 12 → 18 か月目から 3 か月刻み: 18, 21, 24, 27
        assert mt._expected_folds(samples) == 4

    def test_thin_training_is_not_counted(self):
        samples = {f"2020-{m:02d}": [] for m in range(1, 13)}
        samples.update({f"2021-{m:02d}": [([0.0], 0.0, 0)] for m in range(1, 13)})
        assert mt._expected_folds(samples) == 0


# ── jax が要る ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def smoke_result(jax_ok):
    import database
    with patch.object(database, "non_tradable_codes", lambda db: set()):
        return _run(_params())


class TestExecuteSmoke:
    def test_required_keys(self, smoke_result):
        required = {"cv_metrics", "selected_features", "cv_diagnostics", "final_model", "sequence",
                    "env", "n_train_samples", "n_companies", "results", "model_type", "oof_backtest"}
        assert required <= set(smoke_result)
        assert smoke_result["model_type"] == "transformer"

    def test_oof_covers_every_fold(self, smoke_result):
        oof = smoke_result["oof_backtest"]
        assert oof["rank_ic"]["n"] > 0                      # ADR-0014: OOF が空のまま緑にしない
        assert smoke_result["cv_diagnostics"]["n_folds"] == len(smoke_result["cv_diagnostics"]["epochs"])

    def test_results_sorted_with_risk_axes(self, smoke_result):
        rows = smoke_result["results"]
        assert len(rows) == smoke_result["n_companies"] == 40
        mus = [r["mu_raw"] for r in rows]
        assert mus == sorted(mus, reverse=True)
        for key in ("edinet_code", "company_name", "mu_raw", "mu_rel", "r1", "r2", "r3", "r_macro"):
            assert key in rows[0]

    def test_sequence_was_used(self, smoke_result):
        seq = smoke_result["sequence"]
        assert seq["used"] is True and seq["n_weeks"] == 210
        assert seq["mean_observed_patches"] > 0

    def test_selected_features_are_financial_only(self, smoke_result):
        assert smoke_result["selected_features"] == list(M2_M6_DEFAULT_FIN_FEATURES)


class TestWithJax:
    def test_no_sequence_does_not_read_volume(self, jax_ok):
        prices, seq_rows, fin, companies = _make_data()
        vol_loader = MagicMock(side_effect=AssertionError("出来高を読んではいけない"))
        with patch("plugins.macro_transformer.load_data", return_value=(prices, fin, companies)), \
             patch("plugins.macro_transformer.load_weekly_prices_chunked", vol_loader), \
             patch("plugins.macro_transformer.preload_macro", return_value={}), \
             patch("plugins.macro_transformer.get_producer_scores", return_value={}), \
             patch("plugins.macro_transformer._m6_panel_args", return_value=dict(_NO_MACRO_PANEL)):
            res = plugin.execute(_params(use_sequence=False), MagicMock())
        vol_loader.assert_not_called()
        assert res["sequence"] == {"used": False}

    def test_objective_only_returns_oof_without_saving(self, jax_ok):
        from database import tuning_objective_only
        saver = MagicMock()
        with tuning_objective_only(), patch.object(mt.MacroTransformerPlugin, "_persist_producer", saver):
            res = _run(_params())
        assert res["results"] == [] and res["oof_backtest"]["rank_ic"]["n"] > 0
        saver.assert_not_called()

    def test_dry_run_does_not_write(self, jax_ok, db):
        from database import MacroTransformerScore, tuning_dry_run
        data = _make_data()
        _run(_params(), db=db, data=data)
        assert db.query(MacroTransformerScore).count() == 40
        db.query(MacroTransformerScore).delete()
        db.commit()
        with tuning_dry_run():
            _run(_params(), db=db, data=data)
        assert db.query(MacroTransformerScore).count() == 0

    def test_mismatched_volume_load_is_refused(self, jax_ok):
        prices, seq_rows, fin, companies = _make_data()
        seq_rows["E00003"] = seq_rows["E00003"][:-1]       # 2 回の読み込みの間に書き換わった
        with pytest.raises(ValueError, match="食い違う"):
            _run(_params(), data=(prices, seq_rows, fin, companies))
