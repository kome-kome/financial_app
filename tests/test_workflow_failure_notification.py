"""notify-failure.yml（GHA 失敗の自動 Issue 起票）の不変条件ガード（Issue #414）。

守るのは3点:

1. `workflows:` を列挙しない（＝全ワークフローが対象）。
   GitHub は `workflow_run.workflows` の各要素を**フィルタパターン**として解釈するため、
   本リポジトリのように `[定常] …` と角括弧で始まる名前を列挙すると
   "Encountered an issue parsing workflow trigger(s)" で startup_failure になる
   （2026-08-02 実測・run 30747596548）。列挙しないことで、ワークフロー追加時の
   列挙漏れ（＝静かな通知欠落・#414 と同型）も構造的に起きない。
2. 除外は job の `if` の名前一致で行うため、`ci.yml` の `name:` と文字列が一致すること。
   ci.yml を改名して整合が崩れると、PR ごとに Issue が乱立する。
3. `cancelled` を条件から落とさないこと（timeout 打ち切りは failure にならない）。
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
NOTIFIER = WORKFLOW_DIR / "notify-failure.yml"
CI = WORKFLOW_DIR / "ci.yml"


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


@pytest.fixture(scope="module")
def condition(notifier) -> str:
    return notifier["jobs"]["notify"]["if"]


def test_workflows_is_match_all_pattern(notifier):
    """`workflows:` は全マッチ1本に固定する（列挙も省略も startup_failure を招く）。"""
    workflow_run = _triggers(notifier)["workflow_run"] or {}
    assert workflow_run.get("workflows") == ["**"], (
        "workflows: は ['**'] から変えないこと。2026-08-02 に両側を実測: "
        "個別列挙は各要素がフィルタパターン扱いとなり '[定常] …' の角括弧で "
        "startup_failure（run 30747596548）／省略は "
        "'on.workflow_run does not reference any workflows' で startup_failure"
        "（run 30748527156）。どちらも起動すらしないため通知欠落に気づけない"
    )


def test_triggers_on_completed_workflow_run(notifier):
    trg = _triggers(notifier)
    assert "workflow_run" in trg
    assert trg["workflow_run"]["types"] == ["completed"]


def test_condition_covers_failure_and_cancelled(condition):
    """timeout 打ち切りは failure ではなく cancelled で終わるため両方必須。"""
    assert "failure" in condition
    assert "cancelled" in condition, (
        "cancelled が条件に無い＝timeout 由来の打ち切り（tune-hyperparameters 300分・"
        "collect-interim 4h の実例）を取りこぼす"
    )


def test_ci_is_excluded_by_exact_name(condition):
    """ci.yml の除外は名前一致。ci.yml を改名したら if も直す（PR ごとの Issue 乱立防止）。"""
    ci_name = _load(CI)["name"]
    assert f"!= '{ci_name}'" in condition, (
        f"job の if が ci.yml の name（{ci_name!r}）を除外していない。"
        "ci.yml を改名したなら if の文字列も追随させること"
    )


def test_no_other_workflow_is_excluded(condition):
    """ci.yml 以外を黙って除外していないこと（除外が増えるほど通知漏れが増える）。"""
    assert condition.count("!=") == 1, (
        "除外条件が2つ以上ある。意図的に増やす場合は本テストと DEPLOYMENT.md を更新すること"
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
