"""分析メタ（category / ui_order）とサイドバーIA（/api/plugins）のテスト。

PR1（目的別IA再設計の土台）:
  - 各プラグインの to_meta() が category（非空 str）と ui_order（int）を持つ
  - /api/plugins が「プラグイン + 特例エントリ(screen/backtest)」を ui_order 昇順で返す
  - category のグルーピング順が投資フロー順4分類になる
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# import 時の APP_SECRET_KEY 未設定警告を避けるため、import 前にダミーを設定
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from plugins import list_plugins  # noqa: E402
from routers.analysis import SPECIAL_ANALYSES  # noqa: E402

client = TestClient(api.app)

# 投資フロー順の期待カテゴリ並び（ui_order 帯: 100/200/300/400/500）
EXPECTED_CATEGORY_ORDER = [
    "① 銘柄を探す",
    "② 割安度を測る",
    "③ 将来リターンを予測",
    "④ 戦略を検証",
    "⑤ 保有を見直す",
]


class TestPluginMeta:
    def test_every_plugin_has_category_and_ui_order(self):
        for p in list_plugins():
            meta = p.to_meta()
            assert isinstance(meta.get("category"), str) and meta["category"], \
                f"{p.name} に category が無い"
            assert isinstance(meta.get("ui_order"), int), \
                f"{p.name} の ui_order が int でない"

    def test_ui_orders_are_unique(self):
        # サイドバーの並びが安定するよう、プラグイン + 特例の ui_order は一意であること
        orders = [p.to_meta()["ui_order"] for p in list_plugins()]
        orders += [s["ui_order"] for s in SPECIAL_ANALYSES]
        assert len(orders) == len(set(orders)), f"ui_order が重複: {sorted(orders)}"


class TestPluginsEndpoint:
    def test_returns_plugins_sorted_by_ui_order(self):
        r = client.get("/api/plugins")
        assert r.status_code == 200
        orders = [m["ui_order"] for m in r.json()["plugins"]]
        assert orders == sorted(orders), "ui_order 昇順で返っていない"

    def test_includes_special_entries(self):
        metas = {m["name"]: m for m in client.get("/api/plugins").json()["plugins"]}
        assert "screen" in metas and "backtest" in metas
        # screen は別ページへのリンク（href あり）、backtest は専用タブ（href なし）
        assert metas["screen"].get("href") == "/collection"
        assert "href" not in metas["backtest"]

    def test_category_grouping_order(self):
        metas = client.get("/api/plugins").json()["plugins"]
        seen: list[str] = []
        for m in metas:
            cat = m.get("category") or "その他"
            if cat not in seen:
                seen.append(cat)
        assert seen == EXPECTED_CATEGORY_ORDER


class TestHiddenPlugins:
    """`hidden=True`（退役・ADR-0044）の契約。

    退役は**削除ではなく非表示**で行う。よって「サイドバーから消えている」ことと
    「レジストリ・API・model_comparison には残っている」ことを**両方**縛る必要がある。
    片方だけだと、退役したつもりで実行経路まで壊す／消したつもりで UI に残る、の
    どちらにも倒れうる。加えて `mu_source` の選択肢は sell_ranking の params_schema と
    analysis.html の静的 select の**二重管理**なので、その一致もここで機械照合する
    （退役したモデルが select にだけ残ると、選んだ瞬間 coerce_params が reject する）。
    """

    @staticmethod
    def _hidden_names() -> set[str]:
        return {p.name for p in list_plugins() if p.hidden}

    @staticmethod
    def _mu_source_options(plugin_name: str) -> set[str]:
        from plugins import get_plugin

        schema = get_plugin(plugin_name).params_schema()
        return {o["value"] for o in schema["mu_source"]["options"] if o["value"]}

    def test_to_meta_exposes_hidden(self):
        for p in list_plugins():
            assert isinstance(p.to_meta().get("hidden"), bool), f"{p.name} の hidden が bool でない"

    def test_hidden_plugins_are_absent_from_api(self):
        names = {m["name"] for m in client.get("/api/plugins").json()["plugins"]}
        leaked = self._hidden_names() & names
        assert not leaked, f"hidden なのに /api/plugins に出ている: {sorted(leaked)}"

    def test_hidden_plugins_stay_in_the_registry(self):
        """退役しても実行経路は残す（再評価・復帰できる）。"""
        from plugins import get_plugin

        for name in self._hidden_names():
            assert get_plugin(name) is not None, f"{name} がレジストリから消えている"

    def test_hidden_plugins_stay_in_model_comparison(self):
        """比較ファミリーの一員としての役割は退役後も残る（ADR-0021）。"""
        from model_comparison import COMPARISON_MODELS

        compared = {n for n, _ in COMPARISON_MODELS}
        for name in self._hidden_names():
            assert name in compared, (
                f"{name} は hidden だが COMPARISON_MODELS から外れている。"
                "退役は非表示であって削除ではない（ADR-0044）"
            )

    def test_hidden_plugins_are_not_offered_as_mu_source(self):
        hidden = self._hidden_names()
        for consumer in ("sell_ranking", "recommend"):
            leaked = hidden & self._mu_source_options(consumer)
            assert not leaked, f"{consumer} の mu_source に退役モデルが残っている: {sorted(leaked)}"

    def test_static_select_matches_sell_ranking_options(self):
        """analysis.html の静的 select と sell_ranking の options の二重管理を照合する。"""
        import re

        body = client.get("/analysis").text
        m = re.search(r'<select id="sell-mu-source".*?</select>', body, re.S)
        assert m, "sell-mu-source の select が見つからない"
        in_html = set(re.findall(r'<option value="([^"]+)"', m.group(0)))
        assert in_html == self._mu_source_options("sell_ranking"), (
            "analysis.html の mu_source option と sell_ranking.params_schema() が食い違う"
            f"（html={sorted(in_html)} / schema={sorted(self._mu_source_options('sell_ranking'))}）"
        )


class TestModelStatusEndpoint:
    """/api/model/status は DB を参照するため、get_db を in-memory SQLite fixture に
    差し替えて本番 DB 非依存で検証する（空 DB → computed_at None・n_results 0）。"""

    @pytest.fixture(autouse=True)
    def _override_db(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        yield
        api.app.dependency_overrides.clear()

    def test_returns_200_with_required_keys(self):
        r = client.get("/api/model/status")
        assert r.status_code == 200
        d = r.json()
        assert "computed_at" in d
        assert "staleness_days" in d
        assert "n_results" in d
        assert "is_stale" in d

    def test_field_types(self):
        d = client.get("/api/model/status").json()
        # computed_at は ISO文字列か None
        assert d["computed_at"] is None or isinstance(d["computed_at"], str)
        # staleness_days は int か None（DBが空の場合は None）
        assert d["staleness_days"] is None or isinstance(d["staleness_days"], int)
        # n_results は 0 以上の int
        assert isinstance(d["n_results"], int) and d["n_results"] >= 0
        # is_stale は bool
        assert isinstance(d["is_stale"], bool)

    def test_no_render_light_mode_field(self):
        """render_light_mode は /api/system/info が担当し model/status には含まない。"""
        d = client.get("/api/model/status").json()
        assert "render_light_mode" not in d


class TestFreshnessBarHtml:
    def test_freshness_bar_element_exists(self):
        r = client.get("/analysis")
        assert r.status_code == 200
        body = r.text
        assert 'id="model-freshness-bar"' in body
        assert 'id="freshness-content"' in body

    def test_gap_locked_removed(self):
        """gap-locked カードは鮮度バーに置き換えられ、HTMLに残っていないこと。"""
        body = client.get("/analysis").text
        assert 'id="gap-locked"' not in body


class TestTunedParamsEndpoint:
    """/api/plugins/{name}/tuned（Issue #264・自動調整済みハイパーパラメータの読取専用API）。"""

    @pytest.fixture(autouse=True)
    def _override_db(self, db):
        api.app.dependency_overrides[api.get_db] = lambda: db
        yield
        api.app.dependency_overrides.clear()

    def test_404_when_not_tuned(self):
        r = client.get("/api/plugins/macro_gbdt/tuned")
        assert r.status_code == 404

    def test_200_after_persist(self, db):
        from database import upsert_tuned_params
        upsert_tuned_params(
            db, "macro_gbdt", {"max_depth": 4}, "rank_ic", 0.083,
            [{"params": {"max_depth": 4}, "score": 0.083}], n_combos=42,
            data_fingerprint="abc123",
        )
        r = client.get("/api/plugins/macro_gbdt/tuned")
        assert r.status_code == 200
        d = r.json()
        assert d["params"] == {"max_depth": 4}
        assert d["objective_name"] == "rank_ic"
        assert d["objective_value"] == pytest.approx(0.083)
        assert d["n_combos"] == 42
        assert d["data_fingerprint"] == "abc123"
        assert isinstance(d["tuned_at"], str)

    def test_other_plugin_unaffected(self, db):
        """macro_gbdt を調整しても macro_risk_return は未調整のまま（plugin_name 単位）。"""
        from database import upsert_tuned_params
        upsert_tuned_params(db, "macro_gbdt", {"max_depth": 4}, "rank_ic", 0.083, [], 1, None)
        assert client.get("/api/plugins/macro_risk_return/tuned").status_code == 404

    def test_stale_axis_is_projected_and_reported(self, db):
        """探索から外れた軸は既定へ射影し、**変えたことを申告する**（#604）。

        画面はこの応答をフォームへプリフィルする。黙って値を変えると「調整済みと
        言いながら別の値」という逆の混乱になるので、`stale_params` と生値
        （`params_as_tuned`）を必ず添える。
        """
        from database import upsert_tuned_params
        upsert_tuned_params(
            db, "macro_risk_return",
            {"use_macro": True, "use_momentum": True, "momentum_window": 18,
             "max_features": 5},
            "rank_ic", 0.3003, [], 72, None,
        )
        d = client.get("/api/plugins/macro_risk_return/tuned").json()
        assert d["params"]["use_momentum"] is False          # 探索が固定した値へ
        assert "momentum_window" not in d["params"]          # 空間から消えた軸は落とす
        assert d["params"]["max_features"] == 5              # 探索中の軸は保存値のまま
        assert set(d["stale_params"]) == {"use_momentum", "momentum_window"}
        assert d["params_as_tuned"]["use_momentum"] is True  # 生値は監査用に残す

    def test_unprojected_plugin_still_returns_params(self, db):
        """射影対象外（探索空間を持たない特例）でも従来どおり params を返す。"""
        from database import upsert_tuned_params
        upsert_tuned_params(db, "macro_gbdt", {"max_depth": 4}, "rank_ic", 0.083, [], 1, None)
        d = client.get("/api/plugins/macro_gbdt/tuned").json()
        assert d["params"]["max_depth"] == 4


class TestTunedBadgeHtml:
    """analysis.html の自動調整済みバッジ用プレースホルダ（Issue #264）。"""

    def test_badge_placeholders_exist_for_all_three_models(self):
        body = client.get("/analysis").text
        for name in ("macro_risk_return", "macro_gbdt", "macro_dlm"):
            assert f'id="tuned-badge-{name}"' in body


class TestCrossLinks:
    def test_company_page_has_gap_crosslink(self):
        """company ページの理論時価総額チャートに /analysis?tab=gap リンクがある。"""
        r = client.get("/company/E02167")
        assert r.status_code == 200
        assert '/analysis?tab=gap' in r.text

    def test_company_page_has_nc_crosslink(self):
        """company ページのネットキャッシュチャートに /analysis?tab=net_cash リンクがある。"""
        assert '/analysis?tab=net_cash' in client.get("/company/E02167").text

    def test_company_page_has_recommend_crosslink(self):
        """company ページのZスコアチャートに /analysis?tab=recommend リンクがある。"""
        assert '/analysis?tab=recommend' in client.get("/company/E02167").text
