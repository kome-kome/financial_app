"""dependency-audit.yml（pin した依存の脆弱性照合）の不変条件ガード（Issue #723）。

## なぜ CI で縛るのか

この検査が守る穴は「pin した版に後から脆弱性が公表されても、何も起きていないように見え続ける」
こと。検査そのものが壊れた場合も**同じ見え方になる**——照合対象から1本漏れても、失敗を握り
潰しても、定時実行が消えても、ワークフローは緑のまま（あるいは走らないまま）で誰も気づかない。
だから「検査が検査として機能している形」をここで固定する。

守るのは次のとおり:

1. 週次の定時実行と、`requirements*.txt` を変える PR / main push の両方で走る。
2. 照合対象は `requirements*.txt` の glob で拾う（ファイル名の列挙に戻さない＝増やしたときの
   登録漏れが失敗として現れないため）。
3. pip-audit の版は `requirements-dev.txt` の pin に従い、Render のビルドには入れない。
4. 失敗を握り潰さない（`--strict`・終了コードの伝播・`continue-on-error` 無し）。
5. `cancel-in-progress` を付けない（取り消しは notify-failure が理由不明の cancelled として
   起票する＝誤報になる）。
6. 起票は notify-failure.yml の担当なので、権限は `contents: read` だけ。
7. `--ignore-vuln` で外すときは、同じ行に理由と Issue 番号を書く（黙って外さない）。
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-audit.yml"
REQ_GLOB = "requirements*.txt"
AUDIT_STEP = "Audit pinned dependencies"

# `--ignore-vuln <ID>` の行に置く Issue 参照（例: `# 修正版なし・到達しない経路 #812`）。
_ISSUE_REF = re.compile(r"#\d+")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


def _steps(doc: dict) -> list[dict]:
    return [s for job in doc["jobs"].values() for s in (job.get("steps") or [])]


def unexplained_ignores(script: str) -> list[str]:
    """`--ignore-vuln` を含むのに Issue 参照が無い行を返す（コメントだけの行＝書式の説明は除く）。"""
    return [
        line.strip()
        for line in script.splitlines()
        if "--ignore-vuln" in line
        and not line.lstrip().startswith("#")
        and not _ISSUE_REF.search(line)
    ]


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def audit_step(workflow) -> dict:
    found = [s for s in _steps(workflow) if s.get("name") == AUDIT_STEP]
    assert len(found) == 1, f"'{AUDIT_STEP}' ステップが {len(found)} 個ある（1個のはず）"
    return found[0]


@pytest.fixture(scope="module")
def script(audit_step) -> str:
    return audit_step["run"]


class TestItRuns:
    """走らなければ、脆弱性が無いのと同じ見え方になる。"""

    def test_has_a_live_weekly_schedule(self, workflow):
        crons = [e["cron"] for e in (_triggers(workflow).get("schedule") or []) if "cron" in e]
        assert crons, "定時実行が無い＝PR が来ない限り、後から公表された脆弱性を二度と見ない"

    def test_can_be_started_by_hand(self, workflow):
        assert "workflow_dispatch" in _triggers(workflow)

    @pytest.mark.parametrize("event", ["pull_request", "push"])
    def test_runs_when_pins_change(self, workflow, event):
        spec = _triggers(workflow).get(event) or {}
        assert REQ_GLOB in (spec.get("paths") or []), (
            f"{event} の paths に {REQ_GLOB} が無い＝pin を変えた変更を照合しない"
        )

    def test_push_is_limited_to_main(self, workflow):
        assert (_triggers(workflow)["push"] or {}).get("branches") == ["main"]


class TestItAuditsEveryPinFile:
    def test_targets_are_collected_by_glob(self, script):
        assert f"for f in {REQ_GLOB}" in script, (
            f"照合対象を {REQ_GLOB} の glob で拾っていない。ファイル名の列挙に戻すと、"
            "requirements を増やしたときの登録漏れが失敗として現れない"
        )
        assert not re.search(r"-r\s+requirements", script), (
            "requirements ファイルを名指しで -r している（列挙に戻っている）"
        )

    def test_glob_is_not_vacuous(self):
        """glob が当たらない配置に変わると、照合対象ゼロで「何も検査せず」になる。"""
        found = sorted(p.name for p in ROOT.glob(REQ_GLOB))
        assert len(found) >= 3, f"{REQ_GLOB} が {found} しか当たらない"
        assert "requirements.txt" in found

    def test_empty_glob_is_a_failure(self, script):
        assert "nullglob" in script and "exit 1" in script, (
            "glob が空のときに失敗させていない＝照合対象ゼロが緑で通る"
        )

    def test_pin_files_are_read_as_utf8(self, audit_step):
        """requirements は日本語コメントを含み、cp932 等のロケールでは照合前に落ちる（実測）。"""
        non_ascii = [p.name for p in ROOT.glob(REQ_GLOB)
                     if not p.read_text(encoding="utf-8").isascii()]
        assert non_ascii, "前提が変わった（全ファイル ASCII）。このテストの意味を見直すこと"
        assert (audit_step.get("env") or {}).get("PYTHONUTF8") == "1"


class TestToolIsPinned:
    def test_pip_audit_is_pinned_in_dev_requirements(self):
        text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        assert re.search(r"^pip-audit==\d+(\.\d+)+$", text, re.MULTILINE), (
            "pip-audit が requirements-dev.txt に == で pin されていない"
        )

    def test_pip_audit_stays_out_of_the_render_build(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        assert "pip-audit" not in text, "Render のビルド（requirements.txt）へ入れない"

    def test_workflow_installs_it_from_the_pin(self, workflow):
        runs = "\n".join(str(s.get("run") or "") for s in _steps(workflow))
        assert "pip install -r requirements-dev.txt" in runs
        assert not re.search(r"pip install\s+(-U\s+)?pip-audit", runs), (
            "素の `pip install pip-audit` は版が固定されない（requirements-dev.txt の pin に従う）"
        )


class TestFailureIsNotSwallowed:
    def test_strict_mode(self, script):
        assert "--strict" in script, "依存の収集失敗を黙って飛ばす＝照合できなかったが緑になる"

    def test_exit_code_is_propagated(self, script):
        assert "|| status=$?" in script
        assert script.rstrip().endswith('exit "${status}"'), (
            "pip-audit の終了コードで終わっていない＝検出しても緑になりうる"
        )

    def test_stale_report_is_removed_before_the_run(self, script):
        """脆弱性が無いと --output を書かない。消さないと exit=0 で前回の表が出る（実測）。"""
        assert 0 <= script.index('rm -f "${report}"') < script.index("pip-audit --strict")

    def test_no_continue_on_error(self, workflow):
        for job in workflow["jobs"].values():
            assert not job.get("continue-on-error")
            for step in job.get("steps") or []:
                assert not step.get("continue-on-error"), step.get("name")

    def test_runs_are_never_cancelled_by_concurrency(self, workflow):
        blocks = [workflow.get("concurrency")] + [
            j.get("concurrency") for j in workflow["jobs"].values()
        ]
        for block in blocks:
            if isinstance(block, dict):
                assert not block.get("cancel-in-progress"), (
                    "取り消しは cancelled で終わり、notify-failure が理由不明として起票する（誤報）"
                )


class TestIssueFilingIsDelegated:
    def test_permissions_are_read_only(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}, (
            "起票は notify-failure.yml の担当。このジョブは失敗するだけにする"
        )


class TestIgnoresAreExplained:
    """除外は `ignores` 配列に1行1件（行継続の `\\` の後ろにはコメントを置けないため）。"""

    def test_every_ignore_names_an_issue(self, script):
        assert unexplained_ignores(script) == []

    def test_ignores_are_passed_to_pip_audit(self, script):
        assert "ignores=(" in script and '"${ignores[@]}"' in script

    def test_detector_flags_a_bare_ignore(self):
        text = "ignores=(\n  --ignore-vuln GHSA-xxxx\n)\n"
        assert unexplained_ignores(text) == ["--ignore-vuln GHSA-xxxx"]

    def test_detector_flags_a_reason_without_an_issue(self):
        text = "ignores=(\n  --ignore-vuln GHSA-xxxx   # 到達しない経路\n)\n"
        assert unexplained_ignores(text) == ["--ignore-vuln GHSA-xxxx   # 到達しない経路"]

    def test_detector_accepts_an_explained_ignore(self):
        text = "ignores=(\n  --ignore-vuln GHSA-xxxx   # 修正版なし・到達しない経路 #812\n)\n"
        assert unexplained_ignores(text) == []

    def test_detector_skips_the_format_comment(self):
        assert unexplained_ignores("#   書式:  --ignore-vuln <ID>   # <理由> #<Issue 番号>\n") == []
