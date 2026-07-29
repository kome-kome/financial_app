"""M-4（macro_ensemble）プラグインのテスト（ADR-0015 / Issue #367）。

二段ウォークフォワード・スタッキングの契約を検証:
  - (ym, edinet_code) 整列・intersection
  - 月 t の重みが t 未満の共通 OOF だけで決まる（無リーク）
  - NNLS / rank_ic_grid / equal の重み最適化
  - execute の oof_backtest 非空（embargo=12 でも fold 生存・#363 教訓）
  - producer 往復（tuning_dry_run no-op 含む）・objective_only 早期return
"""
import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402
from plugins import macro_ensemble  # noqa: E402
from plugins.macro_ensemble import (  # noqa: E402
    BASE_MODELS,
    _MIN_META_PAIRS,
    _align,
    _equal_w,
    _fit_weights,
    _same_build_config,
    _simplex_grid,
    _simplex_grid_size,
    _stack_walk_forward,
    plugin,
)
from plugins.utils import coerce_params  # noqa: E402
from tests.test_macro_gbdt import _make_fin  # noqa: E402  — 全財務列デフォルト付きヘルパを再利用

try:
    import xgboost  # noqa: F401

    HAS_XGBOOST = True
except Exception:  # pragma: no cover
    HAS_XGBOOST = False


# ── 合成データ（財務が会社間で変動＝BIC 選択が成立し、価格ドリフトも会社別＝
#    52週先リターンが横断変動する。210週 ≥ min_train(6)+embargo(12) 分の有効月を確保）──

def _make_db(n_companies=6, n_weeks=210):
    import datetime

    db = MagicMock()
    base = datetime.date(2018, 1, 5)
    PX = type("PX", (), {})
    CO = type("CO", (), {})

    prices_by_co: dict = {}
    fin_by_co: dict = {}
    companies: dict = {}
    for ci in range(n_companies):
        ec = f"E{ci:05d}"
        drift = 0.0005 * (1 + ci)               # 会社別ドリフト → y が横断で単調変動
        rows = []
        for w in range(n_weeks):
            p = PX()
            p.trade_date = (base + datetime.timedelta(weeks=w)).isoformat()
            p.close_last = 1000.0 * math.exp(drift * w)
            rows.append(p)
        prices_by_co[ec] = rows

        pe = (base - datetime.timedelta(days=60)).isoformat()
        fin_by_co[ec] = [_make_fin(
            pe, edinet_code=ec, company_name=f"会社{ci}", sec_code=str(1000 + ci),
            industry="テスト業",
            per=15.0 + ci, pbr=1.0 + 0.2 * ci, roe=5.0 + ci, equity_ratio=40.0 + ci,
            roa=4.0 + ci, eps_growth=5.0 + 2 * ci,   # ドリフトと相関 → BIC が拾える
            bs_total_assets=(ci + 1) * 1.0e5,
        )]

        co = CO()
        co.edinet_code = ec; co.name = f"会社{ci}"; co.sec_code = str(1000 + ci)
        co.industry = "テスト業"
        companies[ec] = co

    return db, prices_by_co, fin_by_co, companies


def _rows(ym_seed: int, n=6, w=(0.5, 0.3, 0.2), noise=0.0):
    """(ec, (yh_1..yh_k), y=Σ w_i·yh_i + noise) の合成ペア（非共線・基底数は len(w)）。"""
    import random
    rng = random.Random(ym_seed)
    out = []
    for i in range(n):
        yh = tuple(rng.gauss(0, 1) for _ in w)
        out.append((f"e{i}", yh,
                    sum(wi * v for wi, v in zip(w, yh)) + rng.gauss(0, noise)))
    return out


# ── 純関数: _align ──────────────────────────────────────────────────────────

class TestAlign:
    def test_index_pairing(self):
        out = _align({"2020-01": [(0.1, 0.2), (0.3, 0.4)]}, {"2020-01": ["A", "B"]})
        assert out == {("2020-01", "A"): (0.1, 0.2), ("2020-01", "B"): (0.3, 0.4)}

    def test_nan_rows_skipped(self):
        out = _align({"m": [(float("nan"), 0.2), (0.3, 0.4)]}, {"m": ["A", "B"]})
        assert ("m", "A") not in out and ("m", "B") in out

    def test_fold_months_are_subset_of_ids(self):
        # residuals は fold 月のみ・ids に無い月は来ない前提だが、来ても安全に無視
        out = _align({"m2": [(0.1, 0.2)]}, {"m1": ["A"]})
        assert out == {}


# ── 純関数: _drop_dead_macro_features（M-1 strict 全滅防止・#378 対応時に追加）──

class TestDropDeadMacroFeatures:
    def _prices(self, end="2025-06-27"):
        import datetime
        PX = type("PX", (), {})
        rows = []
        d0 = datetime.date.fromisoformat(end)
        for w in range(220, -1, -1):        # プローブ5点(0〜1100日前)を価格履歴が覆う
            p = PX()
            p.trade_date = (d0 - datetime.timedelta(weeks=w)).isoformat()
            p.close_last = 100.0
            rows.append(p)
        return {"E00001": rows}

    def _series(self, end="2025-06-27", years=6):
        import datetime
        d0 = datetime.date.fromisoformat(end)
        return {(d0 - datetime.timedelta(days=30 * i)).isoformat(): 100.0 + i
                for i in range(12 * years)}

    def test_dead_feature_dropped_alive_kept(self):
        from plugins.macro_ensemble import _drop_dead_macro_features
        mc = {"USDJPY": self._series(), "GOLD": {}}   # GOLD はデータ皆無 → 全プローブ None
        names = ["macro_usdjpy_yoy", "macro_gold_yoy"]
        kept, dropped = _drop_dead_macro_features(mc, names, self._prices())
        assert kept == ["macro_usdjpy_yoy"]
        assert dropped == ["macro_gold_yoy"]

    def test_all_alive_no_drop(self):
        from plugins.macro_ensemble import _drop_dead_macro_features
        mc = {"USDJPY": self._series(), "GOLD": self._series()}
        names = ["macro_usdjpy_yoy", "macro_gold_yoy"]
        kept, dropped = _drop_dead_macro_features(mc, names, self._prices())
        assert kept == names and dropped == []

    def test_empty_macro_names_passthrough(self):
        from plugins.macro_ensemble import _drop_dead_macro_features
        assert _drop_dead_macro_features({}, [], self._prices()) == ([], [])

    def test_recent_only_feature_dropped(self):
        """直近だけ生きる部分カバレッジ特徴は labeled 領域プローブで dropped になる。

        本番実測の macro_jp_real_gdp_yoy 型: 価格末尾近傍のみ非None だと素朴な末尾プローブでは
        「生存」誤判定になり、labeled 領域（末尾-52週以前）の strict 行が全滅して OOF が空になる。"""
        import datetime
        from plugins.macro_ensemble import _drop_dead_macro_features
        d0 = datetime.date.fromisoformat("2025-06-27")
        recent_only = {(d0 - datetime.timedelta(days=i)).isoformat(): 100.0 + i
                       for i in range(0, 200, 10)}          # 直近200日ぶんのみ
        mc = {"USDJPY": self._series(), "GOLD": recent_only}
        kept, dropped = _drop_dead_macro_features(
            mc, ["macro_usdjpy_yoy", "macro_gold_yoy"], self._prices())
        assert kept == ["macro_usdjpy_yoy"]
        assert dropped == ["macro_gold_yoy"]

    def test_thin_slice_below_coverage_dropped(self):
        """labeled 領域の1プローブ点でだけ生きる薄い特徴はカバレッジ閾値(3/5)で dropped。

        本番 real_gdp 型: label_end 近傍のみ非None だと「1点生存で keep」では strict が
        labeled 領域の大半を殺し fold 形成不能になる（E2E 実測で検出）。"""
        import datetime
        from plugins.macro_ensemble import _drop_dead_macro_features
        end = datetime.date.fromisoformat("2025-06-27")
        label_end = end - datetime.timedelta(weeks=52)
        # label_end 近傍の約60日ぶんのみデータ（YoY 用に1年前の点も同幅で用意）
        thin = {}
        for i in range(0, 60, 5):
            d = label_end - datetime.timedelta(days=i)
            thin[d.isoformat()] = 100.0 + i
            thin[(d - datetime.timedelta(days=365)).isoformat()] = 90.0 + i
        mc = {"USDJPY": self._series(), "GOLD": thin}
        kept, dropped = _drop_dead_macro_features(
            mc, ["macro_usdjpy_yoy", "macro_gold_yoy"], self._prices())
        assert kept == ["macro_usdjpy_yoy"]
        assert dropped == ["macro_gold_yoy"]


# ── 純関数: _fit_weights ────────────────────────────────────────────────────

class TestWeightOptimization:
    def test_nnls_recovers_true_weights(self):
        tb = {"2020-01": _rows(1, n=20, w=(0.6, 0.3, 0.1)),
              "2020-02": _rows(2, n=20, w=(0.6, 0.3, 0.1))}
        w = _fit_weights(tb, "nnls", 0.05)
        assert w == pytest.approx((0.6, 0.3, 0.1), abs=0.05)
        assert sum(w) == pytest.approx(1.0, abs=1e-9)

    def test_nnls_all_zero_falls_back_to_equal(self):
        # y が全基底と負相関 → 非負制約下で w=(0,0,0) → 等重みフォールバック
        tb = {"m": [(f"e{i}", (i + 1.0, 2.0 * (i + 1), 3.0 * (i + 1)), -(i + 1.0))
                    for i in range(10)]}
        assert _fit_weights(tb, "nnls", 0.05) == _equal_w(3)

    def test_grid_recovers_direction(self):
        tb = {"2020-01": _rows(3, n=20, w=(0.8, 0.15, 0.05))}
        w = _fit_weights(tb, "rank_ic_grid", 0.05)
        assert w[0] > w[1] and w[0] > w[2]      # 支配的な基底を選ぶ
        assert sum(w) == pytest.approx(1.0, abs=1e-9)

    def test_equal_method(self):
        tb = {"m": _rows(4, n=20)}
        assert _fit_weights(tb, "equal", 0.05) == _equal_w(3)

    def test_too_few_pairs_equal(self):
        tb = {"m": _rows(5, n=_MIN_META_PAIRS - 1)}
        assert _fit_weights(tb, "nnls", 0.05) == _equal_w(3)

    def test_n_base_used_when_no_pairs(self):
        """空データでも n_base 指定で基底数どおりの等重みを返す（フォールバック契約）。"""
        assert _fit_weights({}, "nnls", 0.05, n_base=3) == _equal_w(3)
        assert _fit_weights({}, "nnls", 0.05, n_base=2) == (0.5, 0.5)

    def test_two_base_path_still_works(self):
        """基底数2でも一般化コードが従来どおり動く（M-6 追加前の契約を保持）。"""
        tb = {"m1": _rows(6, n=20, w=(0.7, 0.3)), "m2": _rows(7, n=20, w=(0.7, 0.3))}
        w = _fit_weights(tb, "nnls", 0.05)
        assert len(w) == 2
        assert w == pytest.approx((0.7, 0.3), abs=0.05)


# ── 純関数: _simplex_grid / _same_build_config（N 基底一般化・#397）──────────

class TestSimplexGrid:
    def test_two_base_grid_matches_legacy_scan(self):
        """n=2 は旧実装（w1 を 1.0→0.0 で走査）と同じ点列になる。"""
        g = list(_simplex_grid(2, 0.05))
        assert len(g) == 21
        assert g[0] == (1.0, 0.0) and g[-1] == (0.0, 1.0)

    def test_three_base_grid_size_and_simplex(self):
        g = list(_simplex_grid(3, 0.05))
        assert len(g) == 231                                  # C(20+2, 2)
        assert all(abs(sum(w) - 1.0) < 1e-9 for w in g)
        assert all(all(wi >= 0 for wi in w) for w in g)

    def test_coarse_step(self):
        assert len(list(_simplex_grid(3, 0.5))) == 6           # C(2+2, 2)

    def test_size_helper_matches_actual_grid(self):
        """点数カウンタは格子を実体化せずに一致する（爆発ガードが自分で飽和しないため）。"""
        for n in (2, 3, 4):
            for step in (0.5, 0.2, 0.05):
                assert _simplex_grid_size(n, step) == len(list(_simplex_grid(n, step)))
        # 実体化したら破綻する規模でも即座に数えられる
        assert _simplex_grid_size(6, 0.01) > 50_000_000


class TestSameBuildConfig:
    def test_identical_configs_share_build(self):
        a = {"fin_features": ["per"], "use_macro": True, "macro_features": ["x"],
             "use_momentum": False, "momentum_window": 12, "min_coverage": 0.5,
             "price_features": []}
        assert _same_build_config(a, dict(a)) is True

    def test_differing_build_key_forces_rebuild(self):
        a = {"fin_features": ["per"], "min_coverage": 0.5}
        assert _same_build_config(a, {**a, "min_coverage": 0.7}) is False

    def test_non_build_key_ignored(self):
        """build に効かないキー（学習器固有）の差は共有可否に影響しない。"""
        a = {"fin_features": ["per"], "min_coverage": 0.5, "max_depth": 4}
        assert _same_build_config(a, {**a, "max_depth": 8, "l1_ratio": "auto"}) is True

    def test_m2_and_m6_defaults_are_shareable(self):
        """既定 config では M-2/M-6 のスナップショットを共有できる（二重構築の回避）。"""
        from plugins import get_plugin
        m2p = coerce_params(get_plugin("macro_gbdt").params_schema(), {})
        m6p = coerce_params(get_plugin("macro_enet").params_schema(), {})
        assert _same_build_config(m2p, m6p) is True


# ── 純関数: _stack_walk_forward（二段の無リーク性）──────────────────────────

class TestStackWalkForward:
    def _common(self, n_months=6, n=8):
        return {f"2020-{m:02d}": _rows(m, n=n, w=(0.5, 0.3, 0.2), noise=0.1)
                for m in range(1, n_months + 1)}

    def test_early_folds_use_equal_weights(self):
        common = self._common()
        _, wby = _stack_walk_forward(common, "nnls", 0.05, min_meta_months=2)
        yms = sorted(common)
        assert wby[yms[0]] == _equal_w(3)
        assert wby[yms[1]] == _equal_w(3)
        assert wby[yms[2]] != _equal_w(3)   # 3ヶ月目以降は学習重み

    def test_no_leak_month_t_target_does_not_affect_weights_upto_t(self):
        """月 t の y_true を破壊しても、t 以前の月に適用される重みは不変（無リーク）。"""
        common = self._common()
        yms = sorted(common)
        t = yms[3]
        _, w_before = _stack_walk_forward(common, "nnls", 0.05, min_meta_months=2)

        tampered = {ym: list(v) for ym, v in common.items()}
        tampered[t] = [(ec, yhats, y * 100.0) for (ec, yhats, y) in tampered[t]]
        _, w_after = _stack_walk_forward(tampered, "nnls", 0.05, min_meta_months=2)

        for ym in yms[: yms.index(t) + 1]:      # t 自身を含む＝t の重みは t 未満だけで決まる
            assert w_after[ym] == w_before[ym], f"{ym} の重みが月 {t} の y_true に依存している"
        assert w_after[yms[4]] != w_before[yms[4]]  # t より後は t を学習に使う（変わってよい）

    def test_stacked_values_are_weighted_combination(self):
        common = {"2020-01": [("A", (1.0, 3.0, 5.0), 2.0)],
                  "2020-02": [("B", (2.0, 4.0, 6.0), 3.0)]}
        stacked, _wby = _stack_walk_forward(common, "equal", 0.05, min_meta_months=1)
        assert stacked["2020-01"] == [(pytest.approx((1.0 + 3.0 + 5.0) / 3), 2.0)]
        assert set(stacked) == {"2020-01", "2020-02"}


# ── params_schema / coerce 契約 ─────────────────────────────────────────────

class TestCoerce:
    schema = plugin.params_schema()

    def test_defaults_valid(self):
        params = coerce_params(self.schema, {})
        # 既定は rank-IC 最大化（#397・評価指標と学習目的を一致させる。NNLS は MSE 最小化で
        # 縮小推定の M-6 を重み0にし、OOF の改善が現在μ̂へ反映されない非整合が出た）。
        assert params["weight_method"] == "rank_ic_grid"
        assert params["n_quantiles"] == 5
        assert params["min_meta_months"] == 2

    def test_invalid_weight_method_rejected(self):
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"weight_method": "ols"})

    def test_n_quantiles_bounds_rejected(self):
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"n_quantiles": 99})

    def test_min_meta_months_bounds_rejected(self):
        with pytest.raises(ValueError):
            coerce_params(self.schema, {"min_meta_months": 0})

    def test_base_models_not_in_schema(self):
        # 統合対象は定数（M-3 の dead option を UI に出さない設計・ADR-0015）
        assert "base_models" not in self.schema


# ── プラグインメタ ──────────────────────────────────────────────────────────

class TestPluginMeta:
    def test_identity(self):
        assert plugin.name == "macro_ensemble"
        assert plugin.heavy is True
        assert plugin.ui_order == 370   # M-3=360 の後（M-1→M-2→M-3→M-4 順・#378）
        assert plugin.category == "③ 将来リターンを予測"
        assert plugin.depends_on == []

    def test_to_meta_keys(self):
        meta = plugin.to_meta()
        for k in ("name", "label", "description", "heavy", "category", "params_schema"):
            assert k in meta


# ── execute（合成データ・xgboost 必須）─────────────────────────────────────

@unittest.skipUnless(HAS_XGBOOST, "xgboost 未インストール")
class TestExecuteSmoke(unittest.TestCase):
    """use_macro=False（SUB_PARAM_OVERRIDES）で M-1 strict のマクロ欠損全滅を回避しつつ、
    M-1(BIC+OLS)・M-2(XGB) の実パイプラインを通す。"""

    def setUp(self):
        self._saved = dict(macro_ensemble.SUB_PARAM_OVERRIDES)
        macro_ensemble.SUB_PARAM_OVERRIDES.clear()
        macro_ensemble.SUB_PARAM_OVERRIDES.update({
            "macro_risk_return": {"use_macro": False},
            "macro_gbdt":        {"use_macro": False},
            "macro_enet":        {"use_macro": False},
        })

    def tearDown(self):
        macro_ensemble.SUB_PARAM_OVERRIDES.clear()
        macro_ensemble.SUB_PARAM_OVERRIDES.update(self._saved)

    def _run(self, params_overrides=None):
        db, prices_by_co, fin_by_co, companies = _make_db()
        params = coerce_params(plugin.params_schema(), params_overrides or {})
        with patch("plugins.macro_ensemble.load_data",
                   return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_ensemble.preload_macro", return_value={}), \
             patch("plugins.macro_ensemble.get_producer_scores", return_value={}):
            return plugin.execute(params, db)

    def test_execute_returns_required_keys(self):
        result = self._run()
        for k in ("oof_backtest", "base_oof_backtest", "weights", "weight_method",
                  "n_common_pairs", "base_models", "selected_features_m1",
                  "n_companies", "results"):
            assert k in result, f"出力に '{k}' がない"
        # base_oof_backtest は共通域に制限した各基底の OOF（優劣判定の apples-to-apples）
        assert set(result["base_oof_backtest"]) == set(BASE_MODELS)
        for name in BASE_MODELS:
            assert result["base_oof_backtest"][name]["n_oof_samples"] == \
                result["oof_backtest"]["n_oof_samples"], "共通域の行数が M-4 と不一致"

    def test_oof_backtest_not_hollow(self):
        """embargo=12 適用後も OOF fold が生存（キー存在だけの検証はサイレント空洞化する・#363）。"""
        result = self._run()
        oof = result["oof_backtest"]
        assert oof["n_periods"] > 0, "embargo 適用で OOF fold が全滅している"
        assert oof["n_oof_samples"] > 0
        assert result["n_common_pairs"] > 0, "M-1×M-2 の intersection が空"

    def test_weights_sum_to_one(self):
        result = self._run()
        w = result["weights"]
        assert set(w) == set(BASE_MODELS)                  # M-1 / M-2 / M-6（#397）
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
        assert all(v >= 0 for v in w.values())             # NNLS の非負制約

    def test_results_have_component_mus(self):
        result = self._run()
        assert result["n_companies"] == len(result["results"]) > 0
        for it in result["results"]:
            for k in ("edinet_code", "mu_raw", "mu_m1", "mu_m2", "mu_m6", "r_macro"):
                assert k in it

    def test_stacked_mu_is_weighted_sum_of_component_mus(self):
        """統合μ̂ = Σ w_i·μ̂_i（3基底の重み合成が結果行と整合している）。"""
        result = self._run()
        w = result["weights"]
        it = result["results"][0]
        expected = (w["macro_risk_return"] * it["mu_m1"]
                    + w["macro_gbdt"] * it["mu_m2"]
                    + w["macro_enet"] * it["mu_m6"])
        assert it["mu_raw"] == pytest.approx(expected, abs=1e-4)

    def test_two_base_configuration_still_executes(self):
        """BASE_MODELS を2基底へ差し替えても動く（#397 の 2基底 vs 3基底 実測の前提）。

        レグの組み立てが BASE_MODELS 駆動であることの回帰テスト。基底を戻したときに
        M-6 由来のキーが消え、重みも2要素になる。"""
        saved = list(macro_ensemble.BASE_MODELS)
        macro_ensemble.BASE_MODELS[:] = ["macro_risk_return", "macro_gbdt"]
        try:
            result = self._run()
        finally:
            macro_ensemble.BASE_MODELS[:] = saved
        assert set(result["weights"]) == {"macro_risk_return", "macro_gbdt"}
        assert set(result["base_oof_backtest"]) == {"macro_risk_return", "macro_gbdt"}
        assert result["n_common_pairs"] > 0
        assert result["oof_backtest"]["n_periods"] > 0
        assert all("mu_m6" not in it and "mu_m1" in it for it in result["results"])

    def test_objective_only_skips_scoring_but_same_oof(self):
        full = self._run()
        with database.tuning_objective_only():
            light = self._run()
        assert light["results"] == [] and light["n_companies"] == 0
        assert light["oof_backtest"] == full["oof_backtest"]   # oof は同値（統計妥当性）
        assert light["weights"] == full["weights"]

    def test_month_grid_misalignment_is_realigned(self):
        """片方のスナップショット月集合が 1 ヶ月ずれても共通月グリッドへ揃えて交差が成立する。

        fold 月は all_yms の index 基準のため、揃えないと位相シフトで (ym,ec) intersection が
        空になる（#367 のオフライン実測で検出した実バグの回帰テスト）。"""
        real_build = macro_ensemble.build_snapshots
        call_no = {"n": 0}

        def shifted_build(*args, **kwargs):
            out = real_build(*args, **kwargs)
            call_no["n"] += 1
            if call_no["n"] == 2:          # M-2 側の build だけ最古月を落とす（月ずれを再現）
                s, meta, cur, feats, ids = out
                drop = sorted(s)[0]
                return ({k: v for k, v in s.items() if k != drop},
                        {k: v for k, v in meta.items() if k != drop},
                        cur, feats,
                        {k: v for k, v in ids.items() if k != drop})
            return out

        db, prices_by_co, fin_by_co, companies = _make_db()
        params = coerce_params(plugin.params_schema(), {})
        with patch("plugins.macro_ensemble.load_data",
                   return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_ensemble.preload_macro", return_value={}), \
             patch("plugins.macro_ensemble.get_producer_scores", return_value={}), \
             patch("plugins.macro_ensemble.build_snapshots", side_effect=shifted_build):
            result = plugin.execute(params, db)
        assert result["n_common_pairs"] > 0, "月ずれ時に intersection が空（位相シフト未対策）"
        assert result["oof_backtest"]["n_periods"] > 0


# ── producer 往復（in-memory SQLite・conftest の db fixture）────────────────

class TestProducer:
    def test_replace_and_read_round_trip(self, db):
        from database import replace_macro_ensemble_scores, get_macro_ensemble_scores
        assert plugin.produced_output(db) is False
        n = replace_macro_ensemble_scores(
            db, [{"edinet_code": "E1", "mu": 0.12}, {"edinet_code": "E2", "mu": None}],
            "2026-07-01")
        assert n == 1                      # mu=None はスキップ
        assert get_macro_ensemble_scores(db) == {"E1": 0.12}
        assert plugin.produced_output(db) is True

        scores = plugin.read_producer_scores(db)
        assert scores["E1"]["mu"] == pytest.approx(0.12)
        assert scores["E1"]["r1_prime"] is None
        assert "r_macro" in scores["E1"]

    def test_replace_is_snapshot_overwrite(self, db):
        from database import replace_macro_ensemble_scores, get_macro_ensemble_scores
        replace_macro_ensemble_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "2026-07-01")
        replace_macro_ensemble_scores(db, [{"edinet_code": "E2", "mu": 0.2}], "2026-07-02")
        assert get_macro_ensemble_scores(db) == {"E2": 0.2}

    def test_dry_run_noop(self, db):
        from database import replace_macro_ensemble_scores, get_macro_ensemble_scores
        with database.tuning_dry_run():
            n = replace_macro_ensemble_scores(db, [{"edinet_code": "E1", "mu": 0.1}], None)
        assert n == 0
        assert get_macro_ensemble_scores(db) == {}
