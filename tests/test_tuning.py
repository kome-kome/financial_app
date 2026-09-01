"""tests/test_tuning.py — 共有ハイパーパラメータ探索エンジン（Issue #264・スナップショットキャッシュ #298）"""
import asyncio
from unittest.mock import MagicMock

import pytest

from plugins.tuning import (SearchDim, _grid_combos, _project_champion, _random_combos,
                            _score, search)
from plugins.utils import coerce_params


# ── フェイクプラグイン ─────────────────────────────────────────────────────────

class _QuadraticPlugin:
    """score = -(x-5)^2 - (y-2)^2（x=5, y=2 が真の最適解）を rank_ic.mean として返す。"""
    name = "fake_quadratic"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {
            "x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10},
            "y": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10},
        }

    def execute(self, params: dict, db) -> dict:
        score = -((params["x"] - 5) ** 2) - ((params["y"] - 2) ** 2)
        return {"oof_backtest": {"rank_ic": {"mean": float(score), "std": 1.0, "n": 5},
                                 "long_short_spread": float(score)}}


class _AlwaysFailsPlugin:
    """全候補で例外を送出する（探索が例外で落ちず ValueError を送出することを確認する用）。"""
    name = "fake_fails"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 3}}

    def execute(self, params: dict, db) -> dict:
        raise ValueError("常に失敗する")


class _PartialFailPlugin:
    """x=3 のときだけ例外を送出する。残りの候補で探索が継続することを確認する用。"""
    name = "fake_partial_fail"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 5}}

    def execute(self, params: dict, db) -> dict:
        if params["x"] == 3:
            raise ValueError("x=3 は失敗する")
        return {"oof_backtest": {"rank_ic": {"mean": float(params["x"]), "std": 1.0, "n": 3}}}


# ── _score ────────────────────────────────────────────────────────────────────

class TestScore:

    def test_rank_ic(self):
        oof = {"rank_ic": {"mean": 0.05, "std": 0.1, "n": 10}}
        assert _score(oof, "rank_ic") == 0.05

    def test_ic_ir(self):
        oof = {"rank_ic": {"mean": 0.1, "std": 0.2, "n": 10}}
        assert _score(oof, "ic_ir") == pytest.approx(0.5)

    def test_ic_ir_zero_std_returns_none(self):
        oof = {"rank_ic": {"mean": 0.1, "std": 0.0, "n": 1}}
        assert _score(oof, "ic_ir") is None

    def test_long_short(self):
        oof = {"long_short_spread": 0.03}
        assert _score(oof, "long_short") == 0.03

    def test_unknown_objective_raises(self):
        with pytest.raises(ValueError):
            _score({}, "not_an_objective")


# ── _grid_combos / _random_combos（only_if の縮退挙動） ─────────────────────────

class TestCombos:

    def test_grid_full_cartesian_without_only_if(self):
        dims = [SearchDim("a", [1, 2]), SearchDim("b", [10, 20, 30])]
        combos = _grid_combos(dims)
        assert len(combos) == 6
        assert {"a": 1, "b": 10} in combos
        assert {"a": 2, "b": 30} in combos

    def test_grid_only_if_collapses_inactive_branch(self):
        """flag=False では phi が values[0] に縮退し、3通りに展開されない。"""
        dims = [
            SearchDim("flag", [True, False]),
            SearchDim("phi", [0.5, 0.9, 0.99], only_if=lambda c: c.get("flag") is True),
        ]
        combos = _grid_combos(dims)
        true_combos = [c for c in combos if c["flag"] is True]
        false_combos = [c for c in combos if c["flag"] is False]
        assert len(true_combos) == 3   # phi が全展開
        assert len(false_combos) == 1  # phi は values[0]=0.5 に縮退
        assert false_combos[0]["phi"] == 0.5
        assert len(combos) == 4        # 2×3 ではなく 3+1

    def test_random_only_if_collapses_inactive_branch(self):
        dims = [
            SearchDim("flag", [True, False]),
            SearchDim("phi", [0.5, 0.9, 0.99], only_if=lambda c: c.get("flag") is True),
        ]
        combos = _random_combos(dims, n_iter=50, rng=__import__("random").Random(0))
        false_combos = [c for c in combos if c["flag"] is False]
        assert all(c["phi"] == 0.5 for c in false_combos)
        assert len(combos) == 4  # 一意な組合せは4通りしかない


# ── search（統合） ───────────────────────────────────────────────────────────

class TestSearch:

    def test_grid_search_finds_true_optimum(self):
        dims = [SearchDim("x", [0, 3, 5, 7, 10]), SearchDim("y", [0, 2, 4])]
        result = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None, strategy="grid"))
        assert result["best_params"]["x"] == 5
        assert result["best_params"]["y"] == 2
        assert result["best_score"] == 0.0
        assert result["objective"] == "rank_ic"
        assert result["config"]["n_combos"] == 15

    def test_random_search_deterministic_with_seed(self):
        dims = [SearchDim("x", list(range(11))), SearchDim("y", list(range(11)))]
        r1 = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None,
                                strategy="random", n_iter=8, seed=42))
        r2 = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None,
                                strategy="random", n_iter=8, seed=42))
        assert r1["best_params"] == r2["best_params"]
        assert [e["params"] for e in r1["leaderboard"]] == [e["params"] for e in r2["leaderboard"]]

    def test_random_search_different_seed_can_differ(self):
        dims = [SearchDim("x", list(range(11))), SearchDim("y", list(range(11)))]
        r1 = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None,
                                strategy="random", n_iter=3, seed=1))
        r2 = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None,
                                strategy="random", n_iter=3, seed=2))
        combos1 = [e["params"] for e in r1["leaderboard"]]
        combos2 = [e["params"] for e in r2["leaderboard"]]
        assert combos1 != combos2

    def test_base_params_merged_into_best_params(self):
        """探索対象外の base_params は best_params にそのまま引き継がれる。"""
        dims = [SearchDim("x", [5]), SearchDim("y", [2])]
        result = asyncio.run(search(_QuadraticPlugin(), {"unrelated": "kept"}, dims, db=None,
                                    strategy="grid"))
        assert result["best_params"]["unrelated"] == "kept"

    def test_contract_violation_rejected_but_search_continues(self):
        """schema の bounds 外（x=99）は coerce_params で reject され、他の候補は評価される。"""
        dims = [SearchDim("x", [1, 99])]  # x の max は 10（QuadraticPlugin の schema）

        async def _run():
            return await search(_QuadraticPlugin(), {"y": 2}, dims, db=None, strategy="grid")

        result = asyncio.run(_run())
        rejected = [e for e in result["leaderboard"] if e.get("error")]
        assert len(rejected) == 0  # scored のみ返す（reject は best 対象外）
        assert result["config"]["n_failed"] == 1  # x=99 が reject された分
        assert result["best_params"]["x"] == 1

    def test_execute_exception_does_not_abort_search(self):
        dims = [SearchDim("x", [0, 1, 2, 3, 4])]
        result = asyncio.run(search(_PartialFailPlugin(), {}, dims, db=None, strategy="grid"))
        assert result["best_params"]["x"] == 4  # 最大スコア（x=3 は失敗して対象外）
        assert result["config"]["n_failed"] == 1

    def test_all_candidates_fail_raises(self):
        dims = [SearchDim("x", [0, 1, 2])]
        with pytest.raises(ValueError):
            asyncio.run(search(_AlwaysFailsPlugin(), {}, dims, db=None, strategy="grid"))

    def test_unknown_objective_raises_before_running(self):
        dims = [SearchDim("x", [0, 1])]
        with pytest.raises(ValueError):
            asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None,
                               objective="bogus", strategy="grid"))

    def test_search_wraps_candidate_execution_in_tuning_dry_run(self):
        """各候補評価が database.tuning_dry_run() コンテキスト内で行われる（Issue #264）。
        M-2/M-3 の producer 永続化が探索中に本番テーブルを上書きしないための仕組み。"""
        import database

        dry_run_seen = []

        class _Probe:
            name = "fake_probe"
            depends_on: list = []

            def params_schema(self):
                return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 3}}

            def execute(self, params, db):
                dry_run_seen.append(database._tuning_dry_run.get())
                return {"oof_backtest": {"rank_ic": {"mean": 1.0, "std": 1.0, "n": 3}}}

        dims = [SearchDim("x", [0, 1, 2, 3])]
        asyncio.run(search(_Probe(), {}, dims, db=None, strategy="grid"))

        assert dry_run_seen == [True, True, True, True]
        assert database._tuning_dry_run.get() is False  # コンテキスト外では解除されている

    def test_search_wraps_candidate_execution_in_tuning_objective_only(self):
        """各候補評価が database.tuning_objective_only() コンテキスト内で行われる（Issue #299）。
        各プラグインの execute() はこれを見て oof_backtest 算出後の全社スコアリングを
        省略できる。"""
        import database

        objective_only_seen = []

        class _Probe:
            name = "fake_probe_objective_only"
            depends_on: list = []

            def params_schema(self):
                return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 3}}

            def execute(self, params, db):
                objective_only_seen.append(database.is_tuning_objective_only())
                return {"oof_backtest": {"rank_ic": {"mean": 1.0, "std": 1.0, "n": 3}}}

        dims = [SearchDim("x", [0, 1, 2, 3])]
        asyncio.run(search(_Probe(), {}, dims, db=None, strategy="grid"))

        assert objective_only_seen == [True, True, True, True]
        assert database.is_tuning_objective_only() is False  # コンテキスト外では解除されている


# ── plugins.macro_snapshots._BoundedCache（探索専用の簡易LRU・Issue #298） ──────────

class TestBoundedCache:

    def test_hit_returns_cached_value_without_recompute(self):
        from plugins.macro_snapshots import _BoundedCache
        cache = _BoundedCache(maxsize=8)
        compute = MagicMock(return_value="v")

        assert cache.get_or_compute("k", compute) == "v"
        assert cache.get_or_compute("k", compute) == "v"
        assert compute.call_count == 1

    def test_different_keys_both_compute(self):
        from plugins.macro_snapshots import _BoundedCache
        cache = _BoundedCache(maxsize=8)

        assert cache.get_or_compute("a", lambda: "va") == "va"
        assert cache.get_or_compute("b", lambda: "vb") == "vb"
        assert len(cache._data) == 2

    def test_evicts_least_recently_used_beyond_maxsize(self):
        from plugins.macro_snapshots import _BoundedCache
        cache = _BoundedCache(maxsize=2)
        cache.get_or_compute("a", lambda: "va")
        cache.get_or_compute("b", lambda: "vb")
        cache.get_or_compute("c", lambda: "vc")  # "a" は未再アクセスのまま最古→追い出される

        assert "a" not in cache._data
        assert "b" in cache._data
        assert "c" in cache._data

    def test_evicts_before_compute_so_peak_stays_at_maxsize(self):
        """**追い出しは compute の前**（#584）。ここが本修正の本体。

        作ってから捨てる順序だと compute 実行中は maxsize+1 エントリぶんが同時に生きる。
        `build_snapshots` は1エントリ約 1.7GB（実測）なので、この1個の差が 8GB のマシンでは
        致命的になる——実走では `min_coverage` が3値ぶん常駐して約 5GB を占め、
        `[13/288]` の直後から 156分間 1件も進まなくなった。**上限をいくつにしても直らない**
        （8→2 で peak は 4,908→4,645MB の 5% 減でしかなかった）のは、ピークを決めるのが
        上限そのものではなく「同時に生きている数」だから。
        """
        from plugins.macro_snapshots import _BoundedCache
        cache = _BoundedCache(maxsize=1)
        cache.get_or_compute("a", lambda: "va")

        during = {}

        def compute_b():
            during.update(cache._data)      # compute の最中に何が生きているか
            return "vb"

        cache.get_or_compute("b", compute_b)

        assert during == {}, (
            "compute の実行中に古いエントリが生きている＝peak が maxsize+1 になる"
        )
        assert cache._data == {"b": "vb"}

    def test_maxsize_below_one_is_clamped(self):
        """0 以下を許すと追い出しループが空の辞書を回して止まらない。"""
        from plugins.macro_snapshots import _BoundedCache
        cache = _BoundedCache(maxsize=0)

        assert cache.get_or_compute("a", lambda: "va") == "va"
        assert cache._data == {"a": "va"}

    def test_get_or_compute_refreshes_recency(self):
        from plugins.macro_snapshots import _BoundedCache
        cache = _BoundedCache(maxsize=2)
        cache.get_or_compute("a", lambda: "va")
        cache.get_or_compute("b", lambda: "vb")
        cache.get_or_compute("a", lambda: "va")  # "a" を再アクセス→最新扱いへ更新
        cache.get_or_compute("c", lambda: "vc")  # 最古の "b" が追い出される

        assert "b" not in cache._data
        assert "a" in cache._data
        assert "c" in cache._data


# ── plugins.macro_snapshots.shared_snapshot_cache（Issue #298） ────────────────────
# load_data/preload_macro/build_snapshots はいずれも探索軸に依存しない重い処理
# （DB全件ロード・特徴量スナップショット構築）。shared_snapshot_cache() コンテキスト内では
# 同一引数の呼び出しをキャッシュし、コンテキスト外（通常の /api/plugins/{name}/run 相当）
# では毎回フル計算される（副作用が漏れ出さない）ことを確認する。

class TestSnapshotCacheCaps:
    """名前空間ごとの上限（#584）。**上限は「1エントリの大きさ」で決まる**。"""

    def test_build_snapshots_has_a_tighter_cap(self):
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            cache = ms._shared_cache.get()
            assert cache["build_snapshots"]._maxsize == 1, (
                "build_snapshots は1エントリ約 1.7GB（実測）なので上限1でなければ "
                "min_coverage の3値ぶんが常駐して 8GB のマシンが詰む"
            )

    def test_other_namespaces_keep_the_default_cap(self):
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            cache = ms._shared_cache.get()
            for ns in ("load_data", "preload_macro", "cv_by_selected_features"):
                assert cache[ns]._maxsize == ms._CACHE_MAXSIZE, (
                    f"{ns} は軽い（実測: cv_by_selected_features は8エントリで 8MB）ので "
                    "既定の上限のままでよい"
                )

    def test_every_namespace_has_a_cache(self):
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            cache = ms._shared_cache.get()
            assert set(cache) == set(ms._CACHE_NAMESPACES)


class TestTuningSnapshotCache:

    def test_load_data_cached_within_context(self, monkeypatch):
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value=({}, {}, {}))
        monkeypatch.setattr(ms, "_load_data_impl", mock_impl)

        with ms.shared_snapshot_cache():
            ms.load_data("db1")
            ms.load_data("db1")

        assert mock_impl.call_count == 1

    def test_load_data_recomputes_outside_context(self, monkeypatch):
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value=({}, {}, {}))
        monkeypatch.setattr(ms, "_load_data_impl", mock_impl)

        ms.load_data("db1")
        ms.load_data("db1")

        assert mock_impl.call_count == 2

    def test_load_data_cache_is_isolated_per_context_entry(self, monkeypatch):
        """with ブロックを抜けた後の再入場は独立した新しいキャッシュになる（残留しない）。"""
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value=({}, {}, {}))
        monkeypatch.setattr(ms, "_load_data_impl", mock_impl)

        with ms.shared_snapshot_cache():
            ms.load_data("db1")
        with ms.shared_snapshot_cache():
            ms.load_data("db1")

        assert mock_impl.call_count == 2

    def test_preload_macro_cached_by_prices_and_macro_names(self, monkeypatch):
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value={})
        monkeypatch.setattr(ms, "_preload_macro_impl", mock_impl)
        prices = {"E1": []}

        with ms.shared_snapshot_cache():
            ms.preload_macro("db1", prices, ["macro_usdjpy_yoy"])
            ms.preload_macro("db1", prices, ["macro_usdjpy_yoy"])  # 同一キー→ヒット
            ms.preload_macro("db1", prices, ["macro_sp500_yoy"])   # macro_names が違う→ミス

        assert mock_impl.call_count == 2

    def test_preload_macro_different_prices_object_is_cache_miss(self, monkeypatch):
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value={})
        monkeypatch.setattr(ms, "_preload_macro_impl", mock_impl)

        with ms.shared_snapshot_cache():
            ms.preload_macro("db1", {"E1": []}, ["macro_usdjpy_yoy"])
            ms.preload_macro("db1", {"E1": []}, ["macro_usdjpy_yoy"])  # 別オブジェクト→ミス

        assert mock_impl.call_count == 2

    def test_build_snapshots_cached_by_full_key(self, monkeypatch):
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value=({}, {}, {}, []))
        monkeypatch.setattr(ms, "_build_snapshots_impl", mock_impl)
        prices, fin, companies, macro = {}, {}, {}, {}

        with ms.shared_snapshot_cache():
            ms.build_snapshots(prices, fin, companies, macro,
                               ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0,
                               build_interactions=True)
            ms.build_snapshots(prices, fin, companies, macro,
                               ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0,
                               build_interactions=True)

        assert mock_impl.call_count == 1

    def test_build_snapshots_distinguishes_build_interactions(self, monkeypatch):
        """build_interactions が異なれば別キー（M-1/M-2 が誤って同一結果を共有しない）。"""
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value=({}, {}, {}, []))
        monkeypatch.setattr(ms, "_build_snapshots_impl", mock_impl)
        prices, fin, companies, macro = {}, {}, {}, {}

        with ms.shared_snapshot_cache():
            ms.build_snapshots(prices, fin, companies, macro,
                               ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0,
                               build_interactions=True)
            ms.build_snapshots(prices, fin, companies, macro,
                               ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0,
                               build_interactions=False)

        assert mock_impl.call_count == 2

    def test_build_snapshots_recomputes_outside_context(self, monkeypatch):
        import plugins.macro_snapshots as ms
        mock_impl = MagicMock(return_value=({}, {}, {}, []))
        monkeypatch.setattr(ms, "_build_snapshots_impl", mock_impl)
        prices, fin, companies, macro = {}, {}, {}, {}

        ms.build_snapshots(prices, fin, companies, macro,
                           ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0)
        ms.build_snapshots(prices, fin, companies, macro,
                           ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0)

        assert mock_impl.call_count == 2


# ── shared_cache_get_or_compute（汎用キャッシュヘルパー・Issue #304） ────────────────
# macro_snapshots.py 外のモジュール（M-3の load_prices/load_macro_levels・M-1の
# cv_by_selected_features）が load_data 等と同じ contextvars パターンを再利用するための
# 汎用アクセサ。プリミティブ自体の挙動（キー一致/不一致・コンテキスト内外）を直接検証する。

class TestTuningCacheGetOrCompute:

    def test_hits_cache_within_context_for_same_key(self):
        import plugins.macro_snapshots as ms
        compute = MagicMock(return_value="v")

        with ms.shared_snapshot_cache():
            assert ms.shared_cache_get_or_compute("load_prices", "k1", compute) == "v"
            assert ms.shared_cache_get_or_compute("load_prices", "k1", compute) == "v"

        assert compute.call_count == 1

    def test_different_keys_both_compute(self):
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            assert ms.shared_cache_get_or_compute("load_prices", "a", lambda: "va") == "va"
            assert ms.shared_cache_get_or_compute("load_prices", "b", lambda: "vb") == "vb"

    def test_namespaces_are_isolated(self):
        """同じキーでも名前空間が違えば別エントリ（load_prices と load_macro_levels が
        誤って値を共有しない）。"""
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            v1 = ms.shared_cache_get_or_compute("load_prices", "k", lambda: "prices")
            v2 = ms.shared_cache_get_or_compute("load_macro_levels", "k", lambda: "macro")

        assert v1 == "prices"
        assert v2 == "macro"

    def test_recomputes_outside_context(self):
        import plugins.macro_snapshots as ms
        compute = MagicMock(return_value="v")

        ms.shared_cache_get_or_compute("load_prices", "k1", compute)
        ms.shared_cache_get_or_compute("load_prices", "k1", compute)

        assert compute.call_count == 2

    def test_unknown_namespace_raises(self):
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            with pytest.raises(KeyError):
                ms.shared_cache_get_or_compute("not_a_real_namespace", "k", lambda: "v")

    def test_cv_by_selected_features_namespace_exists(self):
        """M-1 の BIC選択結果キャッシュ（cv_by_selected_features）用の名前空間が
        shared_snapshot_cache() で確保されている（Issue #304）。"""
        import plugins.macro_snapshots as ms

        with ms.shared_snapshot_cache():
            v = ms.shared_cache_get_or_compute("cv_by_selected_features", "k", lambda: "cv")
        assert v == "cv"


# ── search() 統合テスト: 構造パラメータ共有候補間でのスナップショット再利用（Issue #298） ──

class _SnapshotAwarePlugin:
    """M-1/M-2 の execute() を模し、macro_snapshots の load_data/preload_macro/
    build_snapshots を実際に呼び出す。max_features はスナップショット構築に一切使わない
    ダミー軸（M-1 の max_features と同じ位置づけ＝BIC選択にのみ影響し構造は変えない）。"""
    name = "fake_snapshot_model"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {"max_features": {"type": "slider", "dtype": "int", "default": 3, "min": 1, "max": 6}}

    def execute(self, params: dict, db) -> dict:
        from plugins.macro_snapshots import build_snapshots, load_data, preload_macro
        prices_by_co, fin_by_co, companies = load_data(db)
        macro_cache = preload_macro(db, prices_by_co, ["macro_usdjpy_yoy"])
        build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            ["per"], ["macro_usdjpy_yoy"], False, 11, 1.0,
            build_interactions=True,
        )
        score = -((params["max_features"] - 3) ** 2)
        return {"oof_backtest": {"rank_ic": {"mean": float(score), "std": 1.0, "n": 3}}}


class TestSearchReusesSnapshotsAcrossCandidates:

    def test_grid_search_calls_snapshot_builders_once_for_shared_structure(self, monkeypatch):
        """max_features のみ違う6候補（M-1 相当）は同一スナップショットを共有するはずで、
        load_data/preload_macro/build_snapshots はそれぞれ1回だけ計算される。"""
        import plugins.macro_snapshots as ms
        load_mock = MagicMock(return_value=({}, {}, {}))
        preload_mock = MagicMock(return_value={})
        build_mock = MagicMock(return_value=({}, {}, {}, []))
        monkeypatch.setattr(ms, "_load_data_impl", load_mock)
        monkeypatch.setattr(ms, "_preload_macro_impl", preload_mock)
        monkeypatch.setattr(ms, "_build_snapshots_impl", build_mock)

        dims = [SearchDim("max_features", [1, 2, 3, 4, 5, 6])]
        result = asyncio.run(search(_SnapshotAwarePlugin(), {}, dims, db="db1", strategy="grid"))

        assert result["config"]["n_combos"] == 6
        assert result["config"]["n_failed"] == 0
        assert load_mock.call_count == 1
        assert preload_mock.call_count == 1
        assert build_mock.call_count == 1

    def test_two_search_calls_each_get_a_fresh_cache(self, monkeypatch):
        """search() を2回呼ぶと、2回目も独立してフル計算される（呼び出し間でキャッシュが
        残留しない＝データが変わっていた場合でも stale な結果を返さない）。"""
        import plugins.macro_snapshots as ms
        load_mock = MagicMock(return_value=({}, {}, {}))
        monkeypatch.setattr(ms, "_load_data_impl", load_mock)
        monkeypatch.setattr(ms, "_preload_macro_impl", MagicMock(return_value={}))
        monkeypatch.setattr(ms, "_build_snapshots_impl", MagicMock(return_value=({}, {}, {}, [])))

        dims = [SearchDim("max_features", [1, 2, 3])]
        asyncio.run(search(_SnapshotAwarePlugin(), {}, dims, db="db1", strategy="grid"))
        asyncio.run(search(_SnapshotAwarePlugin(), {}, dims, db="db1", strategy="grid"))

        assert load_mock.call_count == 2  # 各 search() 呼び出しで1回ずつ

    def test_snapshot_builders_not_cached_when_called_outside_search(self, monkeypatch):
        """search() を経由しない直接呼び出し（通常の /api/plugins/{name}/run 相当）は
        キャッシュの影響を受けず、呼ぶたびにフル計算される。"""
        import plugins.macro_snapshots as ms
        load_mock = MagicMock(return_value=({}, {}, {}))
        monkeypatch.setattr(ms, "_load_data_impl", load_mock)

        ms.load_data("db1")
        ms.load_data("db1")

        assert load_mock.call_count == 2


class _SchemaPlugin:
    """`_project_champion` 用の最小プラグイン（`params_schema()` だけ持てばよい）。"""

    def __init__(self, schema: dict):
        self._schema = schema

    def params_schema(self) -> dict:
        return self._schema


class TestProjectChampion:
    """`_project_champion()` の投影規則（#590・ADR-0047）。"""

    def test_conditional_axis_degenerates_like_the_grid(self):
        """only_if を満たさない軸は values[0] へ縮退させる（`_grid_combos` と同じ規則）。

        揃えないと、無効な軸の値違いだけで combos と一致せず、同じ結果を出す候補を1件余分に
        評価する（M-3 の alpha_phi が alpha_ar1=False のときこの形になる）。
        """
        dims = [
            SearchDim("alpha_ar1", [False, True]),
            SearchDim("alpha_phi", [0.9, 0.95], only_if=lambda c: c.get("alpha_ar1")),
        ]
        plugin = _SchemaPlugin({
            "alpha_ar1": {"type": "checkbox", "default": False},
            "alpha_phi": {"type": "number", "dtype": "float", "default": 0.9},
        })
        champion = {"alpha_ar1": False, "alpha_phi": 0.95}
        assert _project_champion(plugin, champion, dims) == {"alpha_ar1": False, "alpha_phi": 0.9}

    def test_extra_keys_in_champion_are_dropped(self):
        """combo は軸名だけを持つ（base_params 由来のキーは投影で落とす）。"""
        dims = [SearchDim("x", [0, 3, 5, 7])]
        plugin = _SchemaPlugin({
            "x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10},
            "use_macro": {"type": "checkbox", "default": True},
        })
        assert _project_champion(plugin, {"x": 3, "use_macro": True}, dims) == {"x": 3}

    def test_axis_added_after_the_champion_was_saved_is_filled_from_the_default(self):
        """後から足された軸は schema の default で補う（**実測: macro_gbdt がこの形**）。

        2026-07-19 に保存された `macro_gbdt` の params には、その後 #366/#402 で足された
        `use_monotone_constraints` / `use_sector_features` が無い。本番の
        `execute_plugin` も `coerce_params` で default を補うので、補完後の姿が
        「いま本番で動いている設定」である。ここを素通りさせると、**軸を1本足しただけで
        champion 再測定が黙って止まる**（#590 が直したのと同じ「失敗として現れない」形）。
        """
        dims = [SearchDim("x", [0, 3, 5, 7]), SearchDim("newer_axis", [False, True])]
        plugin = _SchemaPlugin({
            "x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10},
            "newer_axis": {"type": "checkbox", "default": False},
        })
        assert _project_champion(plugin, {"x": 3}, dims) == {"x": 3, "newer_axis": False}

    def test_value_removed_from_the_space_is_not_resurrected(self):
        """値域から外された値は投入しない（軸の追加とは扱いを分ける＝退役の尊重）。"""
        dims = [SearchDim("momentum_window", [3, 6, 12])]
        plugin = _SchemaPlugin({
            "momentum_window": {"type": "slider", "dtype": "int", "default": 3, "min": 1, "max": 60},
        })
        assert _project_champion(plugin, {"momentum_window": 18}, dims) is None

    def test_contract_violation_skips_injection(self):
        """保存値が現在のパラメータ契約を満たさないなら投入しない（例外を漏らさない）。"""
        dims = [SearchDim("x", [0, 3, 5, 7])]
        plugin = _SchemaPlugin({
            "x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10},
        })
        assert _project_champion(plugin, {"x": 999}, dims) is None  # bounds 違反
