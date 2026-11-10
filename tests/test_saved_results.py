"""heavy のタブを開いたときの「保存済みの結果」（`GET /api/plugins/{name}/saved`・ADR-0072）。

分析画面の既定は「読む場所」。夜間などのバッチが保存した結果があるのに空のフォームから始めると、
見るだけで数分の再計算が要り、その再計算が保存済みの値を置き換えていた。
"""
import os
import sys
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402

client = TestClient(api.app)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def local_db(db):
    api.app.dependency_overrides[api.get_db] = lambda: db
    yield db
    api.app.dependency_overrides.clear()


def _seed_enet(db, make_company, mus, inactive=()):
    from database import MacroEnetScore
    for i, (ec, mu) in enumerate(mus.items(), 1):
        db.add(make_company(edinet_code=ec, sec_code=f"{1000 + i}", name=f"社{i}",
                            industry="電気機器", is_active=ec not in inactive))
        db.add(MacroEnetScore(edinet_code=ec, mu=mu, snapshot_date="2026-11-06",
                              snapshot_date_min="2026-11-06", n_stale=0,
                              created_at=datetime(2026, 11, 10, 8, 30)))
    db.commit()


class TestProducerMu:
    def test_top_mu_of_tradable_companies_in_order(self, local_db, make_company):
        _seed_enet(local_db, make_company,
                   {"E00001": 0.05, "E00002": 0.20, "E00003": -0.10, "E00004": 0.50},
                   inactive=("E00004",))     # 廃止の社は μ̂ が高くても出さない（tradable_results）
        d = client.get("/api/plugins/macro_enet/saved").json()
        assert d["saved"] is True and d["kind"] == "mu"
        assert [r["edinet_code"] for r in d["rows"]] == ["E00002", "E00001", "E00003"]
        assert [r["rank"] for r in d["rows"]] == [1, 2, 3]
        assert d["rows"][0]["company_name"] == "社2" and d["rows"][0]["industry"] == "電気機器"
        assert d["n_total"] == 3
        assert d["untradable_excluded"]["delisted"] == 1
        assert d["asof"]["snapshot_date"] == "2026-11-06"
        assert d["saved_at"].startswith("2026-11-10T08:30")
        assert d["updated_by"] == "毎晩の夜間バッチ"

    def test_limit_cuts_the_list_but_not_the_total(self, local_db, make_company):
        _seed_enet(local_db, make_company, {f"E{i:05d}": i / 100 for i in range(1, 8)})
        d = client.get("/api/plugins/macro_enet/saved?limit=3").json()
        assert len(d["rows"]) == 3 and d["n_total"] == 7

    def test_nothing_saved_yet(self, local_db):
        d = client.get("/api/plugins/macro_gbdt/saved").json()
        assert d["saved"] is False and d["kind"] == "mu"
        assert d["updated_by"] == "平日の日中バッチ"

    def test_limit_out_of_range_is_400(self, local_db):
        assert client.get("/api/plugins/macro_enet/saved?limit=0").status_code == 400
        assert client.get("/api/plugins/macro_enet/saved?limit=201").status_code == 400


class TestSectorOls:
    def test_counts_companies_and_names_the_model(self, local_db):
        from database import RegressionResult
        for ec, yr in (("E00001", 2024), ("E00001", 2025), ("E00002", 2025)):
            local_db.add(RegressionResult(edinet_code=ec, year=yr, period_end=date(yr, 3, 31),
                                          predicted_market_cap=1.0, gap_ratio=1.0, model="ridge",
                                          sector="x"))
        local_db.commit()
        d = client.get("/api/plugins/sector_ols/saved").json()
        assert d["saved"] is True and d["kind"] == "gap"
        assert d["n_companies"] == 2
        assert d["models"] == ["ridge"]
        assert d["computed_at"]


class TestNotSavingAnalyses:
    @pytest.mark.parametrize("name", ["macro_risk_return", "model_comparison"])
    def test_says_it_does_not_save(self, local_db, name):
        d = client.get(f"/api/plugins/{name}/saved").json()
        assert d["saved"] is False and d["kind"] == "none"

    def test_light_analysis_is_404(self, local_db):
        assert client.get("/api/plugins/net_cash_analysis/saved").status_code == 404


class TestScreenWiring:
    @pytest.fixture(scope="class")
    def js(self):
        with open(os.path.join(ROOT, "static", "js", "analysis.js"), encoding="utf-8") as f:
            return f.read()

    def test_opening_a_tab_reads_the_saved_result(self, js):
        assert "_loadSaved(t);" in js
        assert "/saved`" in js
        assert "_tabPlugin[tabId] = plugin.name;" in js

    def test_the_form_moves_under_rerun(self, js):
        assert "det.className = 'rerun';" in js
        assert "設定を変えて再計算" in js

    def test_a_saved_rerun_refreshes_the_card(self, js):
        assert "if (persist) _loadSaved(tabId, true);" in js

    def test_css_exists(self):
        with open(os.path.join(ROOT, "templates", "analysis.html"), encoding="utf-8") as f:
            html = f.read()
        assert "details.rerun{" in html and ".saved-meta{" in html
