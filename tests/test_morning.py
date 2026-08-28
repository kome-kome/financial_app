"""朝ダッシュボード API（routers/morning.py・Issue #423 子3）のテスト。

守るのは4点:
  1. 鮮度ブロックが**必ず**返ること（price / gap_ratio / mu / macro / overall_verdict）
  2. 何かが古いとき overall_verdict が悪化し、理由が人間の言葉で並ぶこと
  3. **古くてもランキングは返す**こと（隠すと別経路で古い値を見に行くだけ）。
     代わりに tradable=false で「この結果で発注しない」を明示する
  4. 学習・再計算をしないこと（heavy プラグインを呼ばない＝Render Free で動く）
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
from database import RegressionResult  # noqa: E402
from routers import morning as _mor  # noqa: E402

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _override_db(db):
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield
    api.app.dependency_overrides.clear()


def _seed_gap(db, *, age_days: int, n: int = 3):
    computed = datetime.now(timezone.utc) - timedelta(days=age_days)
    for i in range(1, n + 1):
        db.add(RegressionResult(
            edinet_code=f"E{i:05d}", year=2026, period_end=date(2026, 3, 31),
            sector="情報・通信業", model="ridge",
            predicted_market_cap=1000.0, gap_ratio=0.1 * i,
            computed_at=computed,
        ))
    db.commit()


class TestFreshnessBlock:
    def test_always_returns_freshness_keys(self):
        r = client.get("/api/morning")
        assert r.status_code == 200
        f = r.json()["freshness"]
        assert set(f) >= {"price", "gap_ratio", "mu", "macro", "batch",
                          "overall_verdict", "tradable", "reasons", "runbook_url"}

    def test_empty_db_is_not_tradable(self):
        """空DBでも 500 にせず「発注不可」を明示する。"""
        body = client.get("/api/morning").json()
        assert body["freshness"]["overall_verdict"] != "fresh"
        assert body["freshness"]["tradable"] is False
        assert body["recommend"]["count"] == 0

    def test_fresh_gap_ratio_is_not_flagged(self, db):
        _seed_gap(db, age_days=0)
        gap = client.get("/api/morning").json()["freshness"]["gap_ratio"]
        assert gap["level"] == "fresh"
        assert gap["n_rows"] == 3
        assert gap["age_days"] == 0

    def test_stale_gap_ratio_degrades_verdict_with_reason(self, db):
        _seed_gap(db, age_days=30)
        f = client.get("/api/morning").json()["freshness"]
        assert f["gap_ratio"]["level"] == "alert"
        assert f["overall_verdict"] == "alert"
        assert f["tradable"] is False
        assert any("gap_ratio" in s for s in f["reasons"])

    def test_missing_gap_ratio_reports_empty(self):
        gap = client.get("/api/morning").json()["freshness"]["gap_ratio"]
        assert gap["level"] == "empty"
        assert gap["n_rows"] == 0

    def test_mu_block_reports_source_even_when_absent(self):
        mu = client.get("/api/morning").json()["freshness"]["mu"]
        assert mu["source"] == "macro_enet"
        assert mu["level"] == "empty"
        assert mu["snapshot_date"] is None

    def test_mu_source_is_selectable(self):
        mu = client.get("/api/morning?mu_source=macro_gbdt").json()["freshness"]["mu"]
        assert mu["source"] == "macro_gbdt"

    def test_each_block_points_at_the_local_runbook(self, db):
        """#503 で駆動がローカルへ移った。**停止済みの GHA へ誘導しない**（#561）。

        以前はここが `actions/workflows/*.yml` を指しており、cron が全て止まった後も
        リンクだけが残って「押すともう動いていないページに着く」状態だった。
        """
        _seed_gap(db, age_days=30)
        f = client.get("/api/morning").json()["freshness"]
        for block in ("gap_ratio", "mu", "macro", "batch"):
            assert f[block]["url"].endswith("docs/DEPLOYMENT.md"), block
            assert "actions/workflows" not in f[block]["url"], block
        assert f["runbook_url"].endswith("docs/DEPLOYMENT.md")
        # 手で回すコマンドが画面から読める（DEPLOYMENT.md を開かずとも打てる）
        assert f["gap_ratio"]["command"] == "./run_nightly.ps1"
        assert f["gap_ratio"]["driver"] == "local:scripts/run_nightly.py"


# `_batch_block` の**本物**を import 時に1度だけ捕まえる。テスト内で monkeypatch した後に
# `mor._batch_block` を読むと差し替え済みのラムダが返り、二重に包んだ側の `get` が捨てられる
# （実際それで unreadable が missing に化けた）。
_ORIG_BATCH_BLOCK = _mor._batch_block


class TestBatchBlock:
    """バッチの足跡（#561）。**「昨夜そもそも走ったのか」を画面へ出す。**

    足跡は `_batch_block` の `get` 継ぎ目から注入する——本番の `app_settings` を
    書き換えて赤を再現すると正本を汚すし、テストが実 DB の状態に依存してしまう。
    """

    @staticmethod
    def _with_store(monkeypatch, store: dict):
        """`app_settings` の中身を差し替えて /api/morning の鮮度ブロックを返す。

        **本番の `app_settings` は触らない**（正本を汚すし、実 DB の状態に依存すると
        判定が観測時刻で揺れる）。`_batch_block` の `get` 継ぎ目だけを差し替える。
        """
        monkeypatch.setattr(
            _mor, "_batch_block",
            lambda db, get=None, now=None: _ORIG_BATCH_BLOCK(
                db, get=lambda _db, key: store.get(key), now=now))
        return client.get("/api/morning").json()["freshness"]

    @classmethod
    def _freshness(cls, monkeypatch, ages_h: dict):
        """`{key: 何時間前}` を足跡として注入する。"""
        now = datetime.now(timezone.utc)
        store = {k: (now - timedelta(hours=h)).isoformat() if h is not None else None
                 for k, h in ages_h.items()}
        return cls._with_store(monkeypatch, store)

    @staticmethod
    def _healthy(**over):
        """全部健全な足跡（時間前）。個別に上書きして異常系を作る。"""
        base = {"nightly_last_run": 1.0, "nightly_last_success": 1.0,
                "monthly_last_run": 25 * 24, "monthly_last_success": 90 * 24,
                "watchdog_last_run": 20.0}
        base.update(over)
        return base

    def test_a_normal_night_is_not_flagged(self, monkeypatch):
        f = self._freshness(monkeypatch, self._healthy())
        assert f["batch"]["level"] == "fresh"
        night = next(r for r in f["batch"]["rows"] if r["gates_verdict"])
        assert night["status"] == "ok"
        assert night["task_name"] == "financial_app-nightly"
        assert not any("夜間バッチ" in r for r in f["reasons"])

    def test_a_skipped_night_degrades_the_verdict_with_a_reason(self, monkeypatch):
        """閾値は 24h + 窓 6h = 30h（`cadence + 窓` の導出・watchdog と共有）。"""
        f = self._freshness(monkeypatch, self._healthy(nightly_last_run=31.0))
        assert f["batch"]["level"] == "alert"
        assert f["overall_verdict"] == "alert"
        assert f["tradable"] is False
        assert any("夜間バッチが" in r and "走っていない" in r for r in f["reasons"])

    def test_a_run_still_in_flight_is_silent(self, monkeypatch):
        """窓の中はまだ走っていてよい時間＝実行中に鳴らないことが構造的に成立する。"""
        f = self._freshness(monkeypatch, self._healthy(nightly_last_run=29.0))
        assert f["batch"]["level"] == "fresh"

    def test_a_stale_monthly_does_not_touch_the_verdict(self, monkeypatch):
        """月次は表示だけ。**混ぜると次の月次まで毎日 warn が出て狼少年になる。**"""
        f = self._freshness(monkeypatch, self._healthy(monthly_last_run=40 * 24))
        monthly = next(r for r in f["batch"]["rows"] if r["label"] == "月次バッチ")
        assert monthly["status"] == "stale"
        assert monthly["gates_verdict"] is False
        assert f["batch"]["level"] == "fresh"          # verdict に効くのは夜間だけ
        assert not any("月次" in r for r in f["reasons"])

    def test_missing_and_unreadable_do_not_share_a_face(self, monkeypatch):
        """「まだ一度も走っていない」と「値が壊れている」は原因が違う（watchdog と同じ語彙）。"""
        f = self._freshness(monkeypatch, self._healthy(nightly_last_run=None))
        night = next(r for r in f["batch"]["rows"] if r["gates_verdict"])
        assert night["status"] == "missing"
        assert any("足跡が app_settings に無い" in r for r in f["reasons"])

        f = self._with_store(monkeypatch, {"nightly_last_run": "壊れた値"})
        night = next(r for r in f["batch"]["rows"] if r["gates_verdict"])
        assert night["status"] == "unreadable"
        assert night["level"] == "alert"

    def test_the_watchdogs_own_first_run_is_not_an_alert(self, monkeypatch):
        """自分の行を書くのは自分だけ＝初回 missing は正常（`missing_is_problem=False`）。"""
        f = self._freshness(monkeypatch, self._healthy(watchdog_last_run=None))
        self_row = next(r for r in f["batch"]["rows"] if r["label"] == "watchdog 自身")
        assert self_row["status"] == "missing"
        assert self_row["level"] == "fresh"
        assert f["batch"]["level"] == "fresh"

    def test_a_broken_lookup_keeps_the_page_alive(self, monkeypatch):
        """判定不能でも 200 で返す（`_macro_block` と同じ作法＝朝の表示を止めない）。"""
        def _boom(db, key):
            raise RuntimeError("app_settings を読めない")

        monkeypatch.setattr(_mor, "_batch_block",
                            lambda db, get=None, now=None: _ORIG_BATCH_BLOCK(
                                db, get=_boom, now=now))
        r = client.get("/api/morning")
        assert r.status_code == 200
        f = r.json()["freshness"]
        assert f["batch"]["level"] == "unknown"
        assert f["batch"]["rows"] == []
        assert any("判定できなかった" in s for s in f["reasons"])

    def test_footprints_are_shown_in_jst(self, monkeypatch):
        """画面は JST。UTC の生値をそのまま出すと 9時間ぶん古く見える。"""
        f = self._freshness(monkeypatch, self._healthy())
        night = next(r for r in f["batch"]["rows"] if r["gates_verdict"])
        assert night["last_run"] and "JST" in night["last_run"]
        assert night["last_success"]


class TestRankingIsAlwaysReturned:
    def test_ranking_returned_even_when_stale(self, db, make_metric):
        """鮮度が赤でもランキング自体は返す（tradable=false で警告する）。"""
        _seed_gap(db, age_days=30)
        # バランス型の重みは Zスコア列。min_coverage=0.5 を超える程度に埋める。
        db.add(make_metric(edinet_code="E00001", year=2026, z_roe=2.0,
                           z_op_margin=1.0, z_revenue=1.0, z_cf_ratio=0.5))
        db.commit()

        body = client.get("/api/morning").json()
        assert body["freshness"]["tradable"] is False
        assert body["recommend"]["count"] >= 1

    def test_preset_is_passed_through(self):
        body = client.get("/api/morning?preset=バランス型").json()
        assert body["preset"] == "バランス型"

    def test_top_n_out_of_range_rejected(self):
        assert client.get("/api/morning?top_n=1").status_code == 400
        assert client.get("/api/morning?top_n=999").status_code == 400


class TestNoHeavyRecompute:
    def test_does_not_execute_heavy_plugins(self, monkeypatch):
        """朝の経路は heavy=True のプラグインを一切呼ばない（Render Free の 30秒上限）。"""
        import plugins as plugin_registry
        called = []
        orig = plugin_registry.execute_plugin

        async def _spy(plugin, raw, db):
            called.append(plugin.name)
            return await orig(plugin, raw, db)

        monkeypatch.setattr("routers.morning.plugin_registry.execute_plugin", _spy)
        assert client.get("/api/morning").status_code == 200
        assert called == ["recommend"]
        assert plugin_registry.get_plugin("recommend").heavy is False
