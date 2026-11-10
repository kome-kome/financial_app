"""画面からの heavy 実行は既定で保存しない（ADR-0072）。

画面の既定値は本番の設定と一致しない（業種別OLS は画面 `none`・夜間 `ridge`）ので、押しただけで
夜間などのバッチが書いた結果を別の設定の値で置き換えていた。保存は `?persist=1` のときだけ。

縛ること:
  1. `run` は既定で保存せず、`?persist=1` で保存する（ContextVar が `to_thread` の先まで届く）
  2. 保存を止める一か所（`tuning_dry_run()`）が heavy の全保存先に効く。**heavy の `writes` を
     足したら `SAVERS` に保存関数を足す**——書き忘れると、画面の既定の実行が黙って保存する
  3. `writes` を持つ heavy は、利用者向けの影響先（`writes_impact`）を持つ
  4. 画面が「結果を保存する」を差し込み、チェックしたときだけ `?persist=1` を付ける
"""
import inspect
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import database  # noqa: E402
from tests.test_sector_ols import _regression_keys, _seed_regression_rows, _seed_sector  # noqa: E402

client = TestClient(api.app)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# heavy の保存先 → それを書く database.py の関数（`tuning_dry_run()` を見ていなければならない）
SAVERS = {
    "regression_results": ("upsert_regression_results_batch", "upsert_regression_result",
                           "delete_regression_results"),
    "macro_gbdt_scores": ("replace_macro_gbdt_scores",),
    "macro_dlm_scores": ("replace_macro_dlm_scores",),
    "macro_enet_scores": ("replace_macro_enet_scores",),
    "macro_ensemble_scores": ("replace_macro_ensemble_scores",),
}


def _heavy_writers():
    from plugins import list_plugins
    return [p for p in list_plugins() if p.heavy and p.writes]


@pytest.fixture
def local_db(db, monkeypatch):
    """ローカル扱い（heavy を止めない）・バッチ実行中の判定を外す。"""
    import routers.analysis as ra
    monkeypatch.setattr(api, "RENDER_LIGHT_MODE", False)
    monkeypatch.setattr(database, "DB_TARGET", "local")
    monkeypatch.setattr(ra, "_refuse_heavy_while_unsafe", lambda *a, **k: None)
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield db
    api.app.dependency_overrides.clear()


class TestRunDoesNotSaveUnlessAsked:
    """業種別OLS を HTTP で実際に回し、保存表を直接見る（関数の差し替えでは届き方を確かめられない）。"""

    def test_default_run_returns_results_but_saves_nothing(self, local_db, make_fin):
        _seed_sector(local_db, make_fin, n=12)
        r = client.post("/api/plugins/sector_ols/run", json={})
        assert r.status_code == 200, r.text
        assert r.json()["n_total"] == 12            # 計算はして画面に返す
        assert _regression_keys(local_db) == set()  # 保存はしない

    def test_default_run_leaves_the_saved_rows_alone(self, local_db, make_fin):
        # 外れた行の削除（#905）も止まる。夜間が書いた行が残る
        _seed_sector(local_db, make_fin, n=12)
        _seed_regression_rows(local_db, [("E00001", 2023, "2023-03-31")], gap=999.0)
        assert client.post("/api/plugins/sector_ols/run", json={}).status_code == 200
        from database import RegressionResult
        assert [r.gap_ratio for r in local_db.query(RegressionResult).all()] == [999.0]

    def test_persist_saves(self, local_db, make_fin):
        _seed_sector(local_db, make_fin, n=12)
        r = client.post("/api/plugins/sector_ols/run?persist=1", json={})
        assert r.status_code == 200, r.text
        assert len(_regression_keys(local_db)) == 12

    def test_the_dry_run_does_not_leak_past_the_request(self, local_db, make_fin):
        _seed_sector(local_db, make_fin, n=12)
        client.post("/api/plugins/sector_ols/run", json={})
        assert database.is_tuning_dry_run() is False


class TestOneSwitchStopsEverySaver:
    def test_every_heavy_save_target_is_registered(self):
        missing = sorted({t for p in _heavy_writers() for t in p.writes} - set(SAVERS))
        assert not missing, (f"heavy の保存先 {missing} の保存関数が SAVERS に無い。"
                             "tuning_dry_run() を見る関数を書いて登録すること（ADR-0072）")

    @pytest.mark.parametrize("fn", sorted({f for fns in SAVERS.values() for f in fns}))
    def test_saver_honors_the_dry_run(self, fn):
        src = inspect.getsource(getattr(database, fn))
        assert "_tuning_dry_run.get()" in src, f"{fn} が tuning_dry_run() を見ていない"

    def test_regression_results_writes_are_no_ops_inside(self, db):
        row = {"edinet_code": "E00001", "year": 2023, "period_end": "2023-03-31",
               "predicted_market_cap": 1.0, "gap_ratio": 5.0, "model": "ridge", "sector": "x"}
        with database.tuning_dry_run():
            assert database.upsert_regression_results_batch(db, [row]) == 0
            database.upsert_regression_result(db, **row)
        db.commit()
        assert _regression_keys(db) == set()
        _seed_regression_rows(db, [("E00001", 2023, "2023-03-31")])
        with database.tuning_dry_run():
            assert database.delete_regression_results(db, [("E00001", 2023, "2023-03-31")]) == 0
        db.commit()
        assert _regression_keys(db) == {("E00001", 2023, "2023-03-31")}


class TestEveryWriterSaysWhatChanges:
    def test_heavy_writers_have_an_impact(self):
        missing = [p.name for p in _heavy_writers() if not (p.writes_impact or "").strip()]
        assert not missing, f"writes_impact（利用者から何が変わるか）が無い heavy: {missing}"

    def test_meta_and_preflight_carry_the_impact(self, local_db):
        from plugins import get_plugin
        assert get_plugin("macro_enet").to_meta()["writes_impact"]
        body = client.get("/api/plugins/macro_enet/preflight").json()
        assert "売り候補" in body["writes_impact"]
        assert client.get("/api/plugins/model_comparison/preflight").json()["writes_impact"] == ""


class TestProgressSaysNotSaving:
    def test_persisting_names_the_no_save_step_inside_the_dry_run(self):
        from plugins import progress
        steps = []
        with progress.progress_sink(lambda s, c, t: steps.append(s)):
            with database.tuning_dry_run():
                with progress.persisting():
                    pass
            with progress.persisting():
                pass
        assert steps == [progress.NO_PERSIST_STEP, progress.PERSIST_STEP]


class TestScreenWiring:
    """画面の配線（文字列で縛る。JS を実行する環境が無いので、既存の配線テストと同じ型）。"""

    @pytest.fixture(scope="class")
    def js(self):
        with open(os.path.join(ROOT, "static", "js", "analysis.js"), encoding="utf-8") as f:
            return f.read()

    def test_toggle_is_injected_after_the_tabs_exist(self, js):
        assert "function _injectPersistToggles()" in js
        assert "_injectPersistToggles();" in js
        assert 'id="persist-${esc(name)}"' in js

    def test_only_a_checked_toggle_saves(self, js):
        assert "_persistChecked(pluginName)" in js
        assert "${persist ? '?persist=1' : ''}" in js
        assert "_confirmHeavyRun(pluginName, persist)" in js

    def test_toggle_has_its_css(self):
        with open(os.path.join(ROOT, "templates", "analysis.html"), encoding="utf-8") as f:
            assert ".persist-toggle{" in f.read()
