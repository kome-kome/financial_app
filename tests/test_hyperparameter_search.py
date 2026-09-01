"""tests/test_hyperparameter_search.py — run_search() 共有ロジック（Issue #264）。

CLI（hyperparameter_search.py の _run）・GitHub Actions（Issue #292）から呼ばれる
run_search() が、persist/persist_scores を正しく plugins.tuning.search() へ
橋渡しすることを検証する。
"""
import asyncio

import pytest

import hyperparameter_search as hs


class _FakePlugin:
    """x=5 が最適解の1軸探索プラグイン。execute 呼び出し回数を記録する。"""
    name = "fake_model"
    depends_on: list = []
    execute_calls = 0

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 10}}

    def tuning_search_space(self):
        from plugins.tuning import SearchDim
        return {}, [SearchDim("x", [0, 3, 5, 7])]

    def execute(self, params: dict, db) -> dict:
        type(self).execute_calls += 1
        score = -((params["x"] - 5) ** 2)
        return {"oof_backtest": {
            "rank_ic": {"mean": float(score), "std": 1.0, "n": 3},
            "n_periods": 3, "n_oof_samples": 300,
        }}


class _NoSpacePlugin:
    """tuning_search_space() 未実装プラグイン。"""
    name = "no_space"
    depends_on: list = []

    def params_schema(self) -> dict:
        return {}

    def execute(self, params: dict, db) -> dict:
        return {}


@pytest.fixture(autouse=True)
def _reset_execute_calls():
    _FakePlugin.execute_calls = 0
    yield


class TestRunSearch:

    def test_plugin_not_found_raises_value_error(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: None)
        with pytest.raises(ValueError, match="見つかりません"):
            asyncio.run(hs.run_search("macro_gbdt", "grid", 50, "rank_ic", 0, db))

    def test_missing_tuning_search_space_raises_value_error(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _NoSpacePlugin())
        with pytest.raises(ValueError, match="tuning_search_space"):
            asyncio.run(hs.run_search("no_space", "grid", 50, "rank_ic", 0, db))

    def test_default_does_not_persist(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search("fake_model", "grid", 50, "rank_ic", 0, db))
        assert result["persisted"] is False
        assert result["best_params"]["x"] == 5
        from database import get_tuned_params
        assert get_tuned_params(db, "fake_model") is None

    def test_persist_writes_plugin_tuned_params(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))
        assert result["persisted"] is True
        from database import get_tuned_params
        tuned = get_tuned_params(db, "fake_model")
        assert tuned is not None
        assert tuned["params"]["x"] == 5
        # persist_scores=False（既定）なので execute は探索中の候補評価分のみ（追加の1回は無い）
        assert _FakePlugin.execute_calls == 4

    def test_persist_scores_runs_extra_execute_with_best_params(self, db, monkeypatch):
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db,
            persist=True, persist_scores=True,
        ))
        # 探索4候補 + best params での最終 execute 1回 = 5回
        assert _FakePlugin.execute_calls == 5

    def test_persist_scores_without_persist_is_ignored(self, db, monkeypatch):
        """persist=False のとき persist_scores=True を渡しても無視される（永続化されない）。"""
        import plugins
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db,
            persist=False, persist_scores=True,
        ))
        assert result["persisted"] is False
        assert _FakePlugin.execute_calls == 4  # 追加 execute は起きない

class TestQualityGate:
    """persist=True 時の劣化防止（Issue #291 → #590 で作り直し・ADR-0047）。

    **保存値との単純比較はしない。** 保存された objective_value は「そのとき存在した
    パネルでの値」で、パネルは毎晩伸びるため月をまたいだ比較が成立しない（実測: 10 fold の
    0.5068 が 55 fold の 0.0221 より高く出る）。劣化防止は champion を候補プールへ入れる
    ことで担い、persist は常に行う。水準の移動は WARNING と列に残す。
    """

    def test_improved_score_persists(self, db, monkeypatch):
        """前回 objective_value より今回 best_score が高ければ persist される。"""
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 3}, "rank_ic", -100.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["persisted"] is True
        tuned = get_tuned_params(db, "fake_model")
        assert tuned["params"]["x"] == 5
        assert tuned["objective_value"] == result["best_score"]
        assert tuned["prev_objective_value"] == -100.0

    def test_degraded_score_still_persists_and_warns(self, db, monkeypatch, caplog):
        """前回の保存値を下回っても persist する（champion がプールに居るので劣化ではない）。

        ここが #291 からの反転点。旧実装はここで persist をスキップして非ゼロ終了しており、
        一度たまたま高い値が入ると**永久に更新されなかった**（macro_gbdt が 2026-07-19 で固着）。
        """
        import logging
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        # x=5 が最適解（score=0）なので、前回スコアをそれより高くすると必ず「下回った」形になる。
        upsert_tuned_params(db, "fake_model", {"x": 5}, "rank_ic", 100.0, [], 4, "fp")

        with caplog.at_level(logging.WARNING, logger="hyperparameter_search"):
            result = asyncio.run(hs.run_search(
                "fake_model", "grid", 50, "rank_ic", 0, db, persist=True, persist_scores=True,
            ))

        assert result["persisted"] is True
        assert "前回の保存値" in caplog.text
        tuned = get_tuned_params(db, "fake_model")
        assert tuned["objective_value"] == result["best_score"]   # 上書きされている
        assert tuned["prev_objective_value"] == 100.0             # 根拠が行に残る
        # persist_scores の追加 execute が走る（探索4候補 + 1）
        assert _FakePlugin.execute_calls == 5

    def test_first_time_no_existing_row_always_persists(self, db, monkeypatch):
        """plugin_tuned_params に該当行がない初回は champion が無く、常に persist される。"""
        import plugins
        from database import get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        assert get_tuned_params(db, "fake_model") is None

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["persisted"] is True
        assert result["champion_injected"] is False
        tuned = get_tuned_params(db, "fake_model")
        assert tuned["params"]["x"] == 5
        assert tuned["prev_objective_value"] is None
        assert tuned["champion_objective_value"] is None

    def test_different_objective_is_not_compared(self, db, monkeypatch, caplog):
        """前回が別の目的関数なら比較しない（rank_ic と long_short は次元が違う）。"""
        import logging
        import plugins
        from database import upsert_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 5}, "long_short", 100.0, [], 4, "fp")

        with caplog.at_level(logging.WARNING, logger="hyperparameter_search"):
            result = asyncio.run(hs.run_search(
                "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
            ))

        assert result["persisted"] is True
        assert "前回の保存値" not in caplog.text

    def test_cli_run_exits_zero_when_score_drops(self, db, monkeypatch):
        """CLI の _run() は保存値を下回っても正常終了する（バッチを失敗させない）。"""
        import argparse
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 5}, "rank_ic", 100.0, [], 4, "fp")
        monkeypatch.setattr("database.SessionLocal", lambda: db)
        monkeypatch.setattr(db, "close", lambda: None)

        args = argparse.Namespace(
            model="fake_model", strategy="grid", n_iter=50, objective="rank_ic",
            seed=0, persist=True, persist_scores=False,
        )
        asyncio.run(hs._run(args))  # SystemExit を送出しない
        assert get_tuned_params(db, "fake_model")["objective_value"] == 0.0


class TestChampionInjection:
    """本番稼働中の params を今回のパネルで測り直す（#590・ADR-0047）。"""

    def test_champion_in_space_is_measured_on_this_panel(self, db, monkeypatch):
        """値域内の champion は候補として評価され、その実測値が列へ入る。"""
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 3}, "rank_ic", 999.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["champion_injected"] is True
        # x=3 の実測スコアは -(3-5)^2 = -4。保存されていた 999.0 ではない。
        assert result["champion_score"] == -4.0
        assert get_tuned_params(db, "fake_model")["champion_objective_value"] == -4.0

    def test_champion_already_in_grid_is_not_duplicated(self, db, monkeypatch):
        """grid では champion が既に combos にあるので追加評価は起きない（コストゼロ）。"""
        import plugins
        from database import upsert_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 3}, "rank_ic", 999.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["config"]["n_combos"] == 4  # SearchDim("x", [0, 3, 5, 7]) のまま
        assert _FakePlugin.execute_calls == 4

    def test_champion_missing_from_random_pool_is_added(self, db, monkeypatch):
        """random では champion が引かれないことがあるので、そのときだけ1件増える。"""
        import plugins
        from database import upsert_tuned_params
        from plugins.tuning import SearchDim
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        monkeypatch.setattr(_FakePlugin, "tuning_search_space",
                            lambda self: ({}, [SearchDim("x", [0, 3, 5, 7])]))
        # seed=0・n_iter=1 のサンプリングは x=7 を引く。champion をそれ以外にしておく。
        upsert_tuned_params(db, "fake_model", {"x": 0}, "rank_ic", 999.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "random", 1, "rank_ic", 0, db, persist=True,
        ))

        assert result["champion_injected"] is True
        assert result["champion_score"] == -25.0  # -(0-5)^2
        assert result["config"]["n_combos"] == 2  # champion + サンプリング1件

    def test_champion_outside_the_space_is_not_resurrected(self, db, monkeypatch):
        """値域外の champion は投入しない（空間を狭めた意図を尊重する）。

        `dims` を狭めるのは退役の手段でもある（ADR-0045 のモメンタム既定・#583）。
        投入すると「前回勝ったから」で毎月復活し続け、ゲートを直した意味が消える。
        """
        import plugins
        from database import upsert_tuned_params, get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 99}, "rank_ic", 999.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["champion_injected"] is False
        assert result["champion_score"] is None
        assert result["persisted"] is True                     # persist は止めない
        assert get_tuned_params(db, "fake_model")["params"]["x"] == 5

    def test_champion_missing_an_axis_is_filled_from_the_default(self, db, monkeypatch):
        """軸が後から増えた champion は schema の default で補って投入する。

        **実測: `macro_gbdt` の 2026-07-19 の行がこの形**（その後 #366/#402 で足された
        `use_monotone_constraints` / `use_sector_features` を持たない）。補わずに諦めると、
        軸を1本足しただけで champion 再測定が黙って止まる。値域から外された値の扱い
        （投入しない＝退役の尊重）とは分けている——`tests/test_tuning.py::TestProjectChampion`。
        """
        import plugins
        from database import upsert_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"y": 1}, "rank_ic", 999.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=True,
        ))

        assert result["champion_injected"] is True
        assert result["champion_score"] == -25.0  # x は default 0 → -(0-5)^2
        assert result["persisted"] is True

    def test_no_champion_when_not_persisting(self, db, monkeypatch):
        """persist=False の試し撃ちでは champion を混ぜない（1件ぶんの時間を使わない）。"""
        import plugins
        from database import upsert_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())
        upsert_tuned_params(db, "fake_model", {"x": 3}, "rank_ic", 999.0, [], 4, "fp")

        result = asyncio.run(hs.run_search(
            "fake_model", "grid", 50, "rank_ic", 0, db, persist=False,
        ))

        assert result["champion_injected"] is False

    def test_fold_count_is_persisted_as_a_column(self, db, monkeypatch):
        """比較可能性の根拠（fold 数・サンプル数）が列に残る。

        今回の診断（0.5068=10 fold / 0.0221=55 fold）は leaderboard_json を展開しないと
        見えなかった。列にあれば SQL 1発で分かる。
        """
        import plugins
        from database import get_tuned_params
        monkeypatch.setattr(plugins, "get_plugin", lambda name: _FakePlugin())

        asyncio.run(hs.run_search("fake_model", "grid", 50, "rank_ic", 0, db, persist=True))

        tuned = get_tuned_params(db, "fake_model")
        assert tuned["n_periods"] == 3
        assert tuned["n_oof_samples"] == 300


class TestDataFingerprint:
    """指紋の高水位を `week_start` へ揃えたこと（Issue #497・ADR-0036）。

    `trade_date` は週内の最終営業日で PK に含まれず nullable、しかも
    `_recompute_weeks_from_daily` の再集約で同じ週でも書き換わりうる。同じテーブルに
    対する高水位の規則が2つ同居すると、次に触る人が古い方をコピーする。
    """

    def test_uses_the_weekly_cache_fingerprint(self, db, monkeypatch):
        """`weekly_price_cache.fingerprint` を経由すること（自前で max を書かない）。"""
        import weekly_price_cache

        called = []
        real = weekly_price_cache.fingerprint
        monkeypatch.setattr(weekly_price_cache, "fingerprint",
                            lambda d: (called.append(1), real(d))[1])
        hs._data_fingerprint(db)
        assert called, "weekly_price_cache.fingerprint を通っていない"

    def test_is_stable_for_the_same_data(self, db, make_weekly):
        db.add(make_weekly(week_start="2026-01-05")); db.commit()
        a = hs._data_fingerprint(db)
        b = hs._data_fingerprint(db)
        assert a == b

    def test_changes_when_a_week_is_added(self, db, make_weekly):
        db.add(make_weekly(week_start="2026-01-05")); db.commit()
        before = hs._data_fingerprint(db)
        db.add(make_weekly(week_start="2026-01-12")); db.commit()
        assert hs._data_fingerprint(db) != before

    def test_changes_when_the_generation_is_bumped(self, db, make_weekly):
        """**値だけの訂正は max/count では見えない**（#465 の分割段差修復がその形）。

        世代印を指紋へ含めているので、印が進めば探索も「データが変わった」と分かる。
        """
        import weekly_price_cache

        db.add(make_weekly(week_start="2026-01-05")); db.commit()
        before = hs._data_fingerprint(db)
        weekly_price_cache.bump_generation(db, "test")
        assert hs._data_fingerprint(db) != before
