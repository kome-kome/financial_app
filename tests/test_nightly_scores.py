"""nightly_scores.py と nightly-scores.yml の不変条件ガード（Issue #432・親 #423）。

守るのは2系統:

CLI（nightly_scores.py）
  1. 登録モデルが実在の producer であり、NIGHTLY_PARAMS がパラメータ契約を通ること
  2. 実行後に「DB へ本当に書かれたか」を直接クエリで確認すること
     （例外が出なかったことを永続化の証明にしない）
  3. 1モデルの失敗が他モデルを巻き込まず、最後に非ゼロ終了すること

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

    def test_summarize_drops_large_arrays(self):
        s = _summarize({"n_sectors": 3, "results": [{"a": 1}] * 100, "features_used": ["x"]})
        assert s == "n_sectors=3"


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
