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
        assert set(f) >= {"price", "gap_ratio", "mu", "macro",
                          "overall_verdict", "tradable", "reasons", "actions_url"}

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

    def test_each_block_links_to_its_workflow(self, db):
        _seed_gap(db, age_days=30)
        f = client.get("/api/morning").json()["freshness"]
        assert f["gap_ratio"]["url"].endswith("nightly-scores.yml")
        assert f["macro"]["url"].endswith("macro-health.yml")
        assert f["actions_url"].endswith("daily-incremental.yml")


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
