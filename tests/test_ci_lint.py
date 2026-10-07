"""ci.yml の ruff（F 系＝未定義名・import 漏れの静的検出）の不変条件ガード（Issue #724）。

## なぜ CI で縛るのか

この検査が守る穴は「テストが通らない経路（except 節やまれな分岐）の未定義名が、本番で
その経路が実行されるまで出ない」こと。検査そのものが壊れた場合も**同じ見え方になる**——
ステップを消しても、`select` を狭めても、黙って `ignore` を増やしても、CI は緑のままで
誰も気づかない。だから「検査が検査として機能している形」をここで固定する。

守るのは次のとおり:

1. `ci.yml` が `ruff check` を実行し、失敗を握り潰さない（`continue-on-error` 無し）。
2. ルールは `ruff.toml` が持ち、F 系を選ぶ（ローカルと CI で同じ判定）。
3. 外している系統は下の登録表と一致する（増やすなら理由と Issue をここへ1行足す）。
4. ruff の版は `requirements-dev.txt` の pin に従い、Render のビルドには入れない。
5. 未定義名を実際に失敗にする（空振りしない）。
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RUFF_TOML = ROOT / "ruff.toml"

# 外している系統の登録表（理由＝解消する Issue、または残すと決めた Issue）。解消したら
# 行を消し、ruff.toml からも外す。理由の無い除外を増やさないための照合。
EXPECTED_IGNORES = {
    "F401": "#825 未使用 import の後片付け（再エクスポートを壊さずに消す）",
}
EXPECTED_PER_FILE_IGNORES = {
    "collector.py": ({"F403", "F405"}, "#824 後方互換の再エクスポート層としてスター import を残す（決定）"),
}


def _steps(doc: dict) -> list[dict]:
    return [s for job in doc["jobs"].values() for s in (job.get("steps") or [])]


@pytest.fixture(scope="module")
def config() -> dict:
    return tomllib.loads(RUFF_TOML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ruff_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [s for s in _steps(doc) if "ruff check" in (s.get("run") or "")]


class TestWorkflowRunsRuff:
    def test_ci_runs_ruff_check(self, ruff_steps):
        assert ruff_steps, "ci.yml に `ruff check` を実行するステップが無い"

    def test_ruff_failure_is_not_swallowed(self, ruff_steps):
        for step in ruff_steps:
            assert not step.get("continue-on-error"), "ruff の失敗を握り潰している"
            assert "|| true" not in step["run"], "ruff の終了コードを握り潰している"

    def test_rules_come_from_the_config_file(self, ruff_steps):
        """`--select` を引数で渡すと ruff.toml とローカルの判定から外れる。"""
        for step in ruff_steps:
            assert "--select" not in step["run"] and "--config" not in step["run"]


class TestConfig:
    def test_selects_f_rules(self, config):
        assert "F" in config["lint"]["select"], "ruff.toml が F 系を選んでいない"

    def test_ignores_match_the_registry(self, config):
        assert set(config["lint"].get("ignore", [])) == set(EXPECTED_IGNORES), (
            "ruff.toml の ignore が登録表と違う。外すなら理由と Issue を EXPECTED_IGNORES へ"
        )

    def test_per_file_ignores_match_the_registry(self, config):
        actual = {
            path: set(codes)
            for path, codes in config["lint"].get("per-file-ignores", {}).items()
        }
        expected = {path: codes for path, (codes, _) in EXPECTED_PER_FILE_IGNORES.items()}
        assert actual == expected, (
            "ruff.toml の per-file-ignores が登録表と違う。"
            "外すなら理由と Issue を EXPECTED_PER_FILE_IGNORES へ"
        )

    def test_every_exemption_names_an_issue(self):
        reasons = list(EXPECTED_IGNORES.values()) + [r for _, r in EXPECTED_PER_FILE_IGNORES.values()]
        assert all(r.startswith("#") for r in reasons)


class TestPin:
    def test_ruff_is_pinned_in_dev_requirements(self):
        text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        assert any(line.startswith("ruff==") for line in text.splitlines()), (
            "ruff が requirements-dev.txt に == で pin されていない"
        )

    def test_ruff_stays_out_of_the_render_build(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert not any(line.startswith("ruff") for line in text.splitlines())


class TestNotVacuous:
    def test_undefined_name_fails(self, tmp_path):
        """未定義名を入れたファイルを、リポジトリの設定で実際に失敗にする。"""
        pytest.importorskip("ruff")
        bad = tmp_path / "bad.py"
        bad.write_text("def f():\n    return undefined_name_for_724\n", encoding="utf-8")
        res = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--config", str(RUFF_TOML),
             "--output-format", "concise", str(bad)],
            capture_output=True, text=True,
        )
        assert res.returncode == 1, res.stdout + res.stderr
        assert "F821" in res.stdout
