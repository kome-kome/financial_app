"""nightly_scores.py と nightly-scores.yml の不変条件ガード（Issue #432/#443・親 #423）。

守るのは2系統:

CLI（nightly_scores.py）
  1. 登録モデルが実在の producer であり、NIGHTLY_PARAMS がパラメータ契約を通ること
  2. 実行後に「DB へ本当に書かれたか」を直接クエリで確認すること
     （例外が出なかったことを永続化の証明にしない）
  3. 1モデルの失敗が他モデルを巻き込まず、最後に非ゼロ終了すること
  3b. モデル間で重い共有ロード（load_data 等）を再計算しないこと（#443・Egress）

ワークフロー（nightly-scores.yml）
  4. `workflows:` は `["**"]` 固定（列挙は "[定常] …" の角括弧で startup_failure）
  5. daily-incremental の name と `success` で絞ること（株価が前進していない日に
     スコアだけ更新しない）
  6. `tee` するステップに `set -o pipefail` があること（exit code が化ける・#352）
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nightly_scores import (  # noqa: E402
    NIGHTLY_MODELS,
    NIGHTLY_PARAMS,
    VERIFIERS,
    VerificationError,
    _summarize,
    _verify_macro_enet,
    _verify_sector_ols,
    run_models,
)
from tests.test_sector_ols import _seed_sector  # noqa: E402

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "nightly-scores.yml"
DAILY = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-incremental.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


# ── CLI: 登録内容がパラメータ契約と producer 契約に整合しているか ──────────────

class TestRegistration:
    def test_models_exist_and_are_heavy_producers(self):
        from plugins import get_plugin

        for name in NIGHTLY_MODELS:
            plugin = get_plugin(name)
            assert plugin is not None, f"{name} は登録済みプラグインではない"
            # 夜間バッチへ載せる理由が「Render で動かせない heavy」であること（#423 の原則）
            assert plugin.heavy is True, f"{name} は heavy ではない＝夜間バッチの対象外"
            assert hasattr(plugin, "produced_output")

    def test_nightly_params_pass_the_parameter_contract(self):
        """NIGHTLY_PARAMS が params_schema に無いキーや範囲外の値を持たないこと。"""
        from plugins import get_plugin
        from plugins.utils import coerce_params

        for name, raw in NIGHTLY_PARAMS.items():
            plugin = get_plugin(name)
            assert plugin is not None
            schema = plugin.params_schema()
            for key in raw:
                assert key in schema, f"{name}: '{key}' は params_schema に存在しない"
            coerce_params(schema, dict(raw))   # 違反があれば ValueError

    def test_params_keys_are_registered_models(self):
        for name in NIGHTLY_PARAMS:
            assert name in NIGHTLY_MODELS, f"{name} は NIGHTLY_MODELS に無い"

    def test_sector_ols_uses_ridge(self):
        """既定 features 10項目は VIF>10 が頻発するため ridge 固定（本番の既存行も ridge）。"""
        assert NIGHTLY_PARAMS["sector_ols"]["regularization"] == "ridge"

    def test_default_mu_source_producer_is_registered(self):
        """`sell_ranking` の既定 mu_source を出す producer が夜間バッチに載っていること。

        既定 μ̂ の生成元が自動実行されていないと、朝の画面には「いつのものか分からない μ̂」
        が出る（#443）。既定を切り替えたら（#402 で M-2→M-6 のように）ここも追随させる。
        """
        from plugins import get_plugin

        schema = get_plugin("sell_ranking").params_schema()
        default_source = schema["mu_source"]["default"]
        assert default_source in NIGHTLY_MODELS, (
            f"sell_ranking の既定 mu_source '{default_source}' を更新する producer が "
            f"NIGHTLY_MODELS（{NIGHTLY_MODELS}）に無い"
        )

    def test_macro_enet_uses_schema_defaults(self):
        """M-6 は既定構成のまま回す（ADR-0021/0022 の実測と同一のパラメータ）。

        ここでパラメータを足すと、本番の μ̂ が「昇格ゲートで評価していない構成」で
        生成される。M-6 は tune-hyperparameters.yml の matrix にも入っていない。
        """
        assert "macro_enet" not in NIGHTLY_PARAMS

    def test_every_model_has_a_verifier(self):
        """永続化検証の無いモデルを黙って追加させない（#432 の設計上の約束）。"""
        for name in NIGHTLY_MODELS:
            assert name in VERIFIERS, f"{name} の永続化検証（VERIFIERS）が無い"


# ── CLI: 実行と永続化検証 ────────────────────────────────────────────────────

class TestRunModels:
    def test_runs_sector_ols_and_verifies_persistence(self, db, make_fin):
        from database import RegressionResult

        _seed_sector(db, make_fin, n=12)
        entries = asyncio.run(run_models(["sector_ols"], db))

        assert len(entries) == 1
        e = entries[0]
        assert e["ok"] is True, e["error"]
        assert e["error"] is None
        assert "n_sectors=" in e["summary"]
        assert "gap_ratio 非NULL 12件" in e["verified"]
        assert db.query(RegressionResult).count() == 12
        # ridge 指定が execute まで届いていること（params が素通りしていない証明）
        assert {r.model for r in db.query(RegressionResult).all()} == {"ridge"}

    def test_failure_is_captured_and_other_models_continue(self, db, make_fin):
        _seed_sector(db, make_fin, n=12)
        entries = asyncio.run(run_models(["no_such_plugin", "sector_ols"], db))

        assert [e["model"] for e in entries] == ["no_such_plugin", "sector_ols"]
        assert entries[0]["ok"] is False
        assert "見つかりません" in entries[0]["error"]
        # 先行モデルの失敗が後続を止めない（fail-fast: false と同じ思想）
        assert entries[1]["ok"] is True, entries[1]["error"]

    def test_execute_failure_does_not_raise(self, db):
        """空 DB で sector_ols は ValueError を投げるが、バッチは握って結果へ落とす。"""
        entries = asyncio.run(run_models(["sector_ols"], db))
        assert entries[0]["ok"] is False
        assert entries[0]["error"].startswith("ValueError")

    def test_elapsed_is_recorded(self, db, make_fin):
        _seed_sector(db, make_fin, n=12)
        entries = asyncio.run(run_models(["sector_ols"], db))
        assert entries[0]["elapsed_min"] >= 0


class TestSharedSnapshotCache:
    """モデル間で load_data 等を再計算しない（#443・Supabase Egress 5GB/月）。

    夜間バッチは1プロセスで複数の producer を回すため、包まないとモデル数だけ
    週次127万行を再ロードする。ContextVar なので execute_plugin の
    asyncio.to_thread オフロード先（別スレッド）まで伝播する必要がある。
    """

    @staticmethod
    def _fake_plugin(compute_calls: list):
        from plugins.base import AnalysisPlugin
        from plugins.macro_snapshots import shared_cache_get_or_compute

        class _Fake(AnalysisPlugin):
            name = "fake_heavy"
            label = "fake"
            description = "test"
            heavy = True

            def params_schema(self) -> dict:
                return {}

            def execute(self, params, db) -> dict:
                # 実プラグインの load_data と同じ経路でキャッシュを引く
                val = shared_cache_get_or_compute(
                    "load_data", "key", lambda: compute_calls.append(1) or "loaded")
                return {"loaded": val is not None}

        return _Fake()

    def test_heavy_load_is_computed_once_across_models(self, db, monkeypatch):
        import plugins

        calls: list = []
        monkeypatch.setattr(plugins, "get_plugin", lambda name: self._fake_plugin(calls))

        entries = asyncio.run(run_models(["fake_a", "fake_b"], db))

        assert all(e["ok"] for e in entries), [e["error"] for e in entries]
        assert len(calls) == 1, (
            "2モデルで load_data 相当が2回走っている＝shared_snapshot_cache が"
            "ワーカースレッドへ伝播していない（Egress がモデル数に比例する）"
        )

    def test_cache_does_not_leak_outside_run_models(self, db, monkeypatch):
        """バッチを抜けたらキャッシュは破棄される（通常の API 実行へ漏らさない）。"""
        import plugins
        from plugins import macro_snapshots as ms

        monkeypatch.setattr(plugins, "get_plugin", lambda name: self._fake_plugin([]))
        asyncio.run(run_models(["fake_a"], db))

        assert ms._shared_cache.get() is None


class TestVerifier:
    def test_empty_table_raises(self, db):
        with pytest.raises(VerificationError, match="空です"):
            _verify_sector_ols(db, datetime.now(timezone.utc))

    def test_stale_rows_raise(self, db, make_fin):
        """既存行があっても、今回の実行より古ければ「書けていない」と判定する。"""
        _seed_sector(db, make_fin, n=12)
        asyncio.run(run_models(["sector_ols"], db))
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        with pytest.raises(VerificationError, match="より古い"):
            _verify_sector_ols(db, future)

    def test_macro_enet_empty_table_raises(self, db):
        with pytest.raises(VerificationError, match="空です"):
            _verify_macro_enet(db, datetime.now(timezone.utc))

    def test_macro_enet_reports_rows_and_asof(self, db):
        """件数だけでなく as-of（代表値・最古・古い銘柄数）をログへ残す（#417）。"""
        from database import replace_macro_enet_scores

        replace_macro_enet_scores(
            db,
            [{"edinet_code": "E1", "mu": 0.10, "r1_prime": 0.3},
             {"edinet_code": "E2", "mu": 0.20, "r1_prime": None}],
            "2026-08-03", snapshot_date_min="2026-07-27", n_stale=1)

        msg = _verify_macro_enet(db, datetime.now(timezone.utc) - timedelta(minutes=5))
        assert "2社" in msg
        assert "snapshot_date=2026-08-03" in msg
        assert "2026-07-27" in msg and "1社" in msg

    def test_macro_enet_stale_rows_raise(self, db):
        """全置換なので、今回の実行より古い created_at は「書けていない」と判定する。"""
        from database import replace_macro_enet_scores

        replace_macro_enet_scores(db, [{"edinet_code": "E1", "mu": 0.1}], "2026-08-03")
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        with pytest.raises(VerificationError, match="より古い"):
            _verify_macro_enet(db, future)

    def test_summarize_keeps_scalars_and_short_string_lists(self):
        """採用特徴量は残し、巨大配列は件数へ畳む（自動ドロップの追跡に必要）。"""
        s = _summarize({
            "n_sectors": 3,
            "features_used": ["pl_eps", "bs_bps"],
            "results": [{"a": 1}] * 100,
            "sector_stats": [{"industry": "x"}] * 30,
        })
        assert s == "n_sectors=3, features_used=[pl_eps,bs_bps], results(n=100), sector_stats(n=30)"

    def test_summarize_truncates_long_string_lists(self):
        s = _summarize({"features_used": [f"f{i}" for i in range(21)]})
        assert s == "features_used(n=21)"

    def test_summarize_handles_empty_result(self):
        assert _summarize({}) == "(要約できる項目なし)"


# ── ワークフロー: 起動条件の不変条件 ────────────────────────────────────────

class TestWorkflow:
    def test_workflows_is_match_all_pattern(self, workflow):
        wr = _triggers(workflow)["workflow_run"] or {}
        assert wr.get("workflows") == ["**"], (
            "workflows: は ['**'] 固定。'[定常] …' を列挙すると角括弧がフィルタパターンの"
            "文字クラスとして解釈され startup_failure になる（notify-failure.yml と同じ罠）"
        )
        assert wr.get("types") == ["completed"]

    def test_manual_dispatch_is_available(self, workflow):
        assert "workflow_dispatch" in _triggers(workflow)

    def test_chained_on_daily_incremental_success(self, workflow):
        """daily-incremental が success のときだけ起動する（鮮度が先・#423 の依存）。"""
        condition = workflow["jobs"]["scores"]["if"]
        daily_name = _load(DAILY)["name"]
        assert f"== '{daily_name}'" in condition, (
            f"daily-incremental の name（{daily_name!r}）と一致していない。"
            "改名したなら if の文字列も追随させること"
        )
        assert "conclusion == 'success'" in condition, (
            "success 以外でも起動すると、株価が前進していない日に古い株価由来の"
            "スコアを『今日のランキング』として出してしまう"
        )
        assert "workflow_dispatch" in condition, "手動実行の口が if で塞がれている"

    def test_permissions_are_minimal(self, workflow):
        perms = workflow["permissions"]
        assert perms == {"contents": "read"}

    def test_piped_steps_set_pipefail(self, workflow):
        """tee すると exit code が上書きされ、失敗が success に化ける（#352）。"""
        for step in workflow["jobs"]["scores"]["steps"]:
            script = step.get("run", "")
            if "| tee" in script:
                assert "set -o pipefail" in script, (
                    f"'{step.get('name')}' が pipefail 無しで tee している"
                )

    def test_runs_the_cli(self, workflow):
        scripts = " ".join(s.get("run", "") for s in workflow["jobs"]["scores"]["steps"])
        assert "python nightly_scores.py" in scripts

    def test_timeout_is_set(self, workflow):
        assert workflow["jobs"]["scores"]["timeout-minutes"] > 0
