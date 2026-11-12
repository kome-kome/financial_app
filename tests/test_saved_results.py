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


def _seed_diag(db, model, diagnostics, at, snapshot_date="2026-11-11", run_id=None):
    from database import NightlyModelDiagnostic
    db.add(NightlyModelDiagnostic(run_id=run_id or f"run-{at.isoformat()}", model=model,
                                  snapshot_date=snapshot_date, code_version="abc", preprocess_version="v",
                                  diagnostics=diagnostics, created_at=at))
    db.commit()


_ENET_DIAG = {
    "n_companies": 3000,
    "feature_coefs": {"roe": 0.1},
    "oof": {"n_periods": 19, "rank_ic": {"mean": 0.20171, "n": 19},
            "rank_ic_industry_neutral": {"mean": 0.1523},
            "short_side_spread": 0.103618, "short_side_hit_rate": 0.9474,
            "rank_ic_by_period": {"2021-02": 0.2, "2021-05": 0.1},
            "top_k": {"10": {"n_periods": 19, "excess_return": 0.05, "precision": 0.6}},
            "top_decile_rank_ic": {"mean": 0.05, "n": 19}},
    "sell_quantile": {"flat": False, "n_rows": 3000},
}
_OLS_DIAG = {
    "n_sectors": 3, "n_total": 900,
    "sectors": [{"industry": "銀行業", "n": 80, "r2": 0.81, "adj_r2": 0.8},
                {"industry": "電気機器", "n": 240, "r2": 0.42, "adj_r2": 0.41},
                {"industry": "サービス業", "n": 300, "r2": 0.15, "adj_r2": 0.14},
                {"industry": "鉱業", "n": 5, "r2": None}],   # 標本不足で r2 が無い業種は数えない
}


class TestNightlyDiagnostics:
    """保存済みの結果に夜間の検証の要約を添える（#1178・ADR-0061 の診断値の最新1行）。"""

    def test_m6_shows_the_latest_oof_summary(self, local_db, make_company):
        _seed_enet(local_db, make_company, {"E00001": 0.05})
        _seed_diag(local_db, "macro_enet", {"oof": {"n_periods": 18, "rank_ic": {"mean": 0.1}}},
                   datetime(2026, 11, 10, 8, 40), snapshot_date="2026-11-10")
        _seed_diag(local_db, "macro_enet", _ENET_DIAG, datetime(2026, 11, 11, 8, 40))
        _seed_diag(local_db, "sector_ols", _OLS_DIAG, datetime(2026, 11, 11, 8, 50))   # 別モデルの新しい行は読まない
        g = client.get("/api/plugins/macro_enet/saved").json()["diagnostics"]
        assert g["model"] == "macro_enet" and g["snapshot_date"] == "2026-11-11"
        assert g["recorded_at"].startswith("2026-11-11T08:40")
        assert (g["n_periods"], g["rank_ic"], g["rank_ic_industry_neutral"]) == (19, 0.2017, 0.1523)
        assert (g["short_side_spread"], g["short_side_hit_rate"]) == (0.1036, 0.9474)
        assert g["top_k"] == _ENET_DIAG["oof"]["top_k"]
        assert g["top_decile_rank_ic"] == {"mean": 0.05, "n": 19}

    def test_series_and_coefficients_are_not_sent(self, local_db, make_company):
        """タブを開くたびに読まれるので、期ごとの系列・係数・S2 の内訳は返さない。"""
        _seed_enet(local_db, make_company, {"E00001": 0.05})
        _seed_diag(local_db, "macro_enet", _ENET_DIAG, datetime(2026, 11, 11, 8, 40))
        g = client.get("/api/plugins/macro_enet/saved").json()["diagnostics"]
        assert set(g) == {"model", "snapshot_date", "recorded_at", "n_periods", "rank_ic",
                          "rank_ic_industry_neutral", "short_side_spread", "short_side_hit_rate",
                          "top_k", "top_decile_rank_ic"}

    def test_sector_ols_shows_the_r2_summary(self, local_db):
        from database import RegressionResult
        local_db.add(RegressionResult(edinet_code="E00001", year=2025, period_end=date(2025, 3, 31),
                                      predicted_market_cap=1.0, gap_ratio=1.0, model="ridge", sector="x"))
        local_db.commit()
        _seed_diag(local_db, "sector_ols", _OLS_DIAG, datetime(2026, 11, 11, 8, 50))
        g = client.get("/api/plugins/sector_ols/saved").json()["diagnostics"]
        assert (g["n_sectors"], g["n_total"], g["n_with_r2"]) == (3, 900, 3)
        assert g["r2_median"] == 0.42
        assert g["r2_min"] == {"industry": "サービス業", "r2": 0.15, "n": 300}
        assert g["r2_max"] == {"industry": "銀行業", "r2": 0.81, "n": 80}

    def test_without_rows_the_card_still_loads(self, local_db, make_company):
        """夜間が一度も記録していない（Render の断面など）ときも 200 で、diagnostics は None。"""
        _seed_enet(local_db, make_company, {"E00001": 0.05})
        r = client.get("/api/plugins/macro_enet/saved")
        assert r.status_code == 200 and r.json()["diagnostics"] is None
        d = client.get("/api/plugins/macro_enet/saved").json()
        assert d["saved"] is True
        assert client.get("/api/plugins/sector_ols/saved").json()["diagnostics"] is None

    def test_unreadable_table_degrades_to_none(self, local_db, monkeypatch):
        import database

        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("表が無い")
        assert database.get_latest_nightly_diagnostic(Broken(), "macro_enet") is None

    def test_models_without_nightly_diagnostics_do_not_read_the_table(self, local_db, monkeypatch):
        """夜間が診断値を積まないモデル（M-2 等）は DB を読まずに None。"""
        import database
        monkeypatch.setattr(database, "get_latest_nightly_diagnostic",
                            lambda db, model: pytest.fail(f"{model} で診断値の表を読んだ"))
        d = client.get("/api/plugins/macro_gbdt/saved").json()
        assert d["diagnostics"] is None

    def test_summary_tolerates_missing_keys(self):
        from routers.analysis import _diag_summary
        g = _diag_summary("macro_enet", {"snapshot_date": None, "created_at": None, "diagnostics": {}})
        assert g["rank_ic"] is None and g["top_k"] == {} and g["recorded_at"] is None
        g = _diag_summary("sector_ols", {"snapshot_date": "2026-11-11", "created_at": None,
                                         "diagnostics": {"sectors": [{"industry": "x", "r2": None}]}})
        assert g["n_with_r2"] == 0 and g["r2_median"] is None and g["r2_min"] is None
        assert _diag_summary("macro_gbdt", {"diagnostics": {}}) is None
        assert _diag_summary("macro_enet", None) is None


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

    def test_card_shows_the_nightly_check(self, js):
        """夜間の検証の要約を、保存済みの有無と種類（μ̂ / 乖離率）のどの形のカードにも添える（#1178）。"""
        body = js[js.index("function _savedHTML("):js.index("async function _loadSaved(")]
        assert "const diag = _savedDiagHTML(d.name, d.diagnostics);" in body
        assert body.count("+ diag") >= 2 and "` + diag;" in body
        helper = js[js.index("function _savedDiagHTML("):js.index("function _savedHTML(")]
        assert "new Set(['sector_ols', 'macro_enet'])" in js
        assert "_oofTopKHTML({top_k: g.top_k, top_decile_rank_ic: g.top_decile_rank_ic}" in helper
        assert "innerHTML" not in helper   # 文字列は esc() を通して組み立てる（呼び出し側が入れる）

    def test_css_exists(self):
        with open(os.path.join(ROOT, "templates", "analysis.html"), encoding="utf-8") as f:
            html = f.read()
        assert "details.rerun{" in html and ".saved-meta{" in html
