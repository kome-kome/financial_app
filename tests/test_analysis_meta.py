"""分析メタ（category / ui_order）とサイドバーIA（/api/plugins）のテスト。

PR1（目的別IA再設計の土台）:
  - 各プラグインの to_meta() が category（非空 str）と ui_order（int）を持つ
  - /api/plugins が「プラグイン + 特例エントリ(screen/backtest)」を ui_order 昇順で返す
  - category のグルーピング順が投資フロー順4分類になる
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# import 時の APP_SECRET_KEY 未設定警告を避けるため、import 前にダミーを設定
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from plugins import list_plugins  # noqa: E402
from routers.analysis import SPECIAL_ANALYSES  # noqa: E402

client = TestClient(api.app)

# 投資フロー順の期待カテゴリ並び（ui_order 帯: 100/200/300/400）
EXPECTED_CATEGORY_ORDER = [
    "① 銘柄を探す",
    "② 割安度を測る",
    "③ 将来リターンを予測",
    "④ 戦略を検証",
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
