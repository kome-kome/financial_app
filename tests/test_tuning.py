"""tests/test_tuning.py — 共有ハイパーパラメータ探索エンジン（Issue #264）"""
import asyncio

import pytest

from plugins.tuning import SearchDim, _grid_combos, _random_combos, _score, search
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

    async def execute(self, params: dict, db) -> dict:
        score = -((params["x"] - 5) ** 2) - ((params["y"] - 2) ** 2)
        return {"oof_backtest": {"rank_ic": {"mean": float(score), "std": 1.0, "n": 5},
                                 "long_short_spread": float(score)}}


class _AlwaysFailsPlugin:
    """全候補で例外を送出する（探索が例外で落ちず ValueError を送出することを確認する用）。"""
    name = "fake_fails"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 3}}

    async def execute(self, params: dict, db) -> dict:
        raise ValueError("常に失敗する")


class _PartialFailPlugin:
    """x=3 のときだけ例外を送出する。残りの候補で探索が継続することを確認する用。"""
    name = "fake_partial_fail"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 5}}

    async def execute(self, params: dict, db) -> dict:
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

    def test_on_progress_called_once_per_candidate(self):
        dims = [SearchDim("x", [0, 3, 5, 7])]
        calls = []
        asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None, strategy="grid",
                           on_progress=lambda cur, tot, msg: calls.append((cur, tot, msg))))
        assert [c[:2] for c in calls] == [(1, 4), (2, 4), (3, 4), (4, 4)]
        assert all(isinstance(c[2], str) and c[2] for c in calls)

    def test_cancel_check_stops_early_with_partial_leaderboard(self):
        """2候補目の直前でキャンセル要求 → 3件目以降は評価されず、済んだ分だけ返す。"""
        dims = [SearchDim("x", [0, 3, 5, 7])]
        seen = {"n": 0}

        def cancel_check():
            return seen["n"] >= 1

        def on_progress(cur, tot, msg):
            seen["n"] = cur

        result = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None, strategy="grid",
                                    on_progress=on_progress, cancel_check=cancel_check))
        assert result["config"]["cancelled"] is True
        assert len(result["leaderboard"]) == 1
        assert result["best_params"]["x"] == 0  # 唯一評価された候補がそのまま best

    def test_cancel_check_before_any_candidate_returns_empty_result_without_raising(self):
        """即キャンセル（score済み0件）でも ValueError にはしない（ユーザー意図の停止）。"""
        dims = [SearchDim("x", [0, 1, 2])]
        result = asyncio.run(search(_QuadraticPlugin(), {}, dims, db=None, strategy="grid",
                                    cancel_check=lambda: True))
        assert result["config"]["cancelled"] is True
        assert result["best_params"] is None
        assert result["best_score"] is None
        assert result["leaderboard"] == []

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

            async def execute(self, params, db):
                dry_run_seen.append(database._tuning_dry_run.get())
                return {"oof_backtest": {"rank_ic": {"mean": 1.0, "std": 1.0, "n": 3}}}

        dims = [SearchDim("x", [0, 1, 2, 3])]
        asyncio.run(search(_Probe(), {}, dims, db=None, strategy="grid"))

        assert dry_run_seen == [True, True, True, True]
        assert database._tuning_dry_run.get() is False  # コンテキスト外では解除されている
