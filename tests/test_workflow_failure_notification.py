"""notify-failure.yml（GHA 失敗の自動 Issue 起票）の網羅性ガード（Issue #414）。

workflow_run の `workflows:` は **ファイル名ではなくワークフローの `name:` 値**を列挙する
仕様のため、ワークフローを追加・改名しても静かに通知対象から漏れる。
#414 の実害（19日間 failure に誰も気づかない）と同型の「静かな欠落」なので、
列挙漏れをテストで落とす。
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
NOTIFIER = WORKFLOW_DIR / "notify-failure.yml"

# 通知対象から意図的に外すファイル。
#   ci.yml         : PR の pytest 失敗は PR 画面で即見える／feature ブランチの試行錯誤で Issue が乱立する
#   notify-failure : 自分自身（GITHUB_TOKEN 起因の実行は workflow_run を再帰発火しない）
EXCLUDED_FILES = {"ci.yml", "notify-failure.yml"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


def _workflow_files() -> list[Path]:
    """`.github/workflows` 直下のみ。old/ 等サブディレクトリは GHA が認識しないため対象外。"""
    return sorted(p for p in WORKFLOW_DIR.glob("*.yml") if p.is_file())


@pytest.fixture(scope="module")
def notifier() -> dict:
    return _load(NOTIFIER)


def test_all_workflows_are_covered(notifier):
    """通知対象に列挙されていないワークフローが無いこと（＝通知漏れの検知）。"""
    listed = set(_triggers(notifier)["workflow_run"]["workflows"])

    expected = {}
    for path in _workflow_files():
        if path.name in EXCLUDED_FILES:
            continue
        name = _load(path).get("name")
        assert name, f"{path.name} に name: が無い（notify-failure.yml が紐付けられない）"
        expected[name] = path.name

    missing = {name: f for name, f in expected.items() if name not in listed}
    assert not missing, (
        "notify-failure.yml の workflows: に未登録のワークフローがある（失敗が通知されない）: "
        f"{missing}"
    )

    stale = listed - set(expected)
    assert not stale, (
        "notify-failure.yml の workflows: に実在しないワークフロー名が残っている"
        f"（改名の取り残し＝この分の通知は永久に発火しない）: {stale}"
    )


def test_triggers_on_completed_workflow_run(notifier):
    trg = _triggers(notifier)
    assert "workflow_run" in trg
    assert trg["workflow_run"]["types"] == ["completed"]


def test_condition_covers_failure_and_cancelled(notifier):
    """timeout 打ち切りは failure ではなく cancelled で終わるため両方必須。"""
    condition = notifier["jobs"]["notify"]["if"]
    assert "failure" in condition
    assert "cancelled" in condition, (
        "cancelled が条件に無い＝timeout 由来の打ち切り（tune-hyperparameters 300分・"
        "collect-interim 4h の実例）を取りこぼす"
    )


def test_permissions_are_scoped(notifier):
    perms = notifier["permissions"]
    assert perms["issues"] == "write"
    assert perms["actions"] == "read"
    assert perms["contents"] == "read"


def test_other_workflows_do_not_request_issue_write():
    """issues: write は notify-failure.yml だけに閉じる（収集系は contents: read 最小権限）。"""
    for path in _workflow_files():
        if path.name == "notify-failure.yml":
            continue
        perms = _load(path).get("permissions") or {}
        assert perms.get("issues") is None, f"{path.name} が issues 権限を要求している"


def test_duplicate_issues_are_avoided(notifier):
    """同一ワークフローの再失敗は新規起票せずコメント追記になること。"""
    script = notifier["jobs"]["notify"]["steps"][0]["run"]
    assert "gh issue comment" in script
    assert "gh issue create" in script
    assert "--state open" in script, "open Issue の検索なしでは毎回新規起票になる"
