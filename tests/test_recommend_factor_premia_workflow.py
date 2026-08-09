"""recommend-factor-premia.yml（Fama-MacBeth ファクタープレミアム月次バッチ）の
不変条件ガード（Issue #423 子5・#342）。

このワークフローは長らく `workflow_dispatch` のみで**実行履歴ゼロ**だった。その間
「統計的最適化」プリセットは 2026-07-05 のローカル手動実行（有効期間 37）の重みを使い
続けており、2026-08-08 に回し直したら 61 期になった＝#438 型の静かな劣化。cron 化で
これを断つのが本ファイルが守る対象。

守るのは5系統:

1. cron が存在し、月次（日付固定）であること。消えても失敗しない＝無実行は
   notify-failure では検知できない（DEPLOYMENT.md「この仕組みで検知できないもの」）。
2. cron が他ワークフローと衝突しないこと（毎月1日は tune-hyperparameters と
   macro-beta-inference で最大 16:40Z まで埋まっている／UTC 18〜23時は
   daily-incremental → nightly-scores のチェーン帯）。
3. **schedule で起動しても CLI 引数が空にならないこと。** `workflow_dispatch` 専用
   ワークフローを cron 化するときの典型的な踏み抜きどころで、`github.event.inputs.X` は
   schedule イベントでは空になるため、`|| '既定値'` が無いと argparse が空文字を受けて落ちる。
4. ワークフローの入力既定値が CLI の既定定数と一致すること（片方だけ動くと、手動実行と
   cron 実行で別のパラメータになる）。
5. `--persist` が付いていること・`tee` に `set -o pipefail` があること（#352）・
   権限が最小であること。
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOW = WORKFLOW_DIR / "recommend-factor-premia.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """`on:` は YAML 1.1 で bool True にパースされるため両方を見る。"""
    return doc.get("on", doc.get(True)) or {}


def _crons(doc: dict) -> list[str]:
    schedule = _triggers(doc).get("schedule") or []
    return [entry["cron"] for entry in schedule if "cron" in entry]


def _run_scripts(doc: dict) -> str:
    return " ".join(step.get("run", "") for step in doc["jobs"]["estimate"]["steps"])


def _max_timeout_minutes(doc: dict) -> int:
    """そのワークフローが占有しうる最大分数（jobs の timeout-minutes の最大値）。

    matrix で job ごとに違う値を持つ場合（tune-hyperparameters）は式が入りうるので、
    int に落ちないものは無視する。
    """
    vals = []
    for job in (doc.get("jobs") or {}).values():
        t = job.get("timeout-minutes")
        if isinstance(t, int):
            vals.append(t)
    return max(vals, default=0)


def _daily_chain_hours_utc() -> set[int]:
    """daily-incremental → nightly-scores チェーンが占有しうる UTC 時刻の集合。

    **ここを定数で持たない**のが要点（#476）。旧実装は `range(18, 24)` を直書きしており、
    daily-incremental を 18:00Z → 08:17Z へ動かした瞬間に「守っている対象」と実態が
    ずれた——しかもテストは緑のままではなく**無関係な時間帯を禁止し続けた**。
    ADR-0031 と同型の「登録と実体の乖離」なので、実ファイルから導出する。

    幅は名目起動時刻 ＋ 両ワークフローの timeout-minutes の合計（最悪ケース）。
    キュー遅延は含まない＝#446 のとおり最終確認は実起動ログで行う。
    """
    daily = _load(WORKFLOW_DIR / "daily-incremental.yml")
    start = int(_crons(daily)[0].split()[1])
    minutes = _max_timeout_minutes(daily) + _max_timeout_minutes(
        _load(WORKFLOW_DIR / "nightly-scores.yml"))
    span_hours = -(-minutes // 60)                     # 切り上げ
    return {(start + i) % 24 for i in range(span_hours + 1)}


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def cron(workflow) -> list[str]:
    return _crons(workflow)[0].split()


# ── 1. 定期実行の存在 ────────────────────────────────────────────────────────

class TestSchedule:
    def test_monthly_cron_exists(self, workflow):
        """cron が消えても「失敗」しない＝無実行は notify-failure で検知できない。"""
        assert _crons(workflow), (
            "schedule cron が無い。手動のみへ戻すと、実行されないこと自体に誰も気づかず"
            "『統計的最適化』プリセットが古い重みで固まる（#423 子5 の発生経緯そのもの）"
        )

    def test_cron_is_monthly_on_a_fixed_day(self, cron):
        minute, hour, dom, month, dow = cron
        assert dom.isdigit(), f"日付が固定されていない（dom={dom!r}）＝月次ではない"
        assert hour.isdigit() and minute.isdigit()
        assert month == "*" and dow == "*"

    def test_manual_dispatch_is_kept(self, workflow):
        """cron 化しても手動の口は残す（データ修復後の即時再推定・#465 のような場面）。"""
        assert "workflow_dispatch" in _triggers(workflow)


# ── 2. 他ワークフローとの非重複 ──────────────────────────────────────────────

class TestNoScheduleCollision:
    @staticmethod
    def _other_workflows() -> list[Path]:
        """`.github/workflows` 直下のみ（old/ はサブディレクトリで GHA 対象外）。"""
        return sorted(p for p in WORKFLOW_DIR.glob("*.yml")
                      if p.is_file() and p.name != WORKFLOW.name)

    def test_no_other_workflow_shares_the_same_cron(self, workflow):
        mine = set(_crons(workflow))
        for path in self._other_workflows():
            assert mine.isdisjoint(_crons(_load(path))), (
                f"{path.name} と同一の cron 文字列。同時刻起動は Supabase への"
                "同時フルロードになる（Egress とプーラ接続の両方で不利）"
            )

    def test_monthly_batches_do_not_share_a_day(self, cron):
        """毎月1日は macro-beta-inference（00:00Z〜最大 05:40Z）と
        tune-hyperparameters（16:30Z〜最大 22:25Z）で埋まっている（#476 で再配置）。"""
        my_dom = cron[2]
        for path in self._other_workflows():
            for other in _crons(_load(path)):
                other_dom = other.split()[2]
                if other_dom == "*":
                    continue
                assert other_dom != my_dom, (
                    f"{path.name} と同じ日（{my_dom}日）に月次実行が重なっている。"
                    "長時間ジョブ（最大340分）と重ねると本バッチが待たされる"
                )

    def test_not_inside_the_daily_chain_window(self, cron):
        """daily-incremental → nightly-scores のチェーン帯を避ける。

        帯は `daily-incremental.yml` の cron と両ワークフローの timeout-minutes から
        **実ファイルを読んで導出する**（#476）。定数で持つと収集の時刻を動かした瞬間に
        守る対象がずれる。
        """
        window = _daily_chain_hours_utc()
        assert int(cron[1]) not in window, (
            f"daily-incremental → nightly-scores のチェーン帯 {sorted(window)}（UTC 時）と"
            "重なる時間帯。週次株価のフルロードが二重に走る"
        )


# ── 3〜4. schedule 起動時の引数と CLI 既定値の整合 ─────────────────────────────

class TestInputsSurviveScheduleEvent:
    def test_every_input_reference_has_a_fallback(self, workflow):
        """schedule イベントでは `github.event.inputs.*` が空になる（cron 化の典型的な罠）。

        `|| '既定値'` が無いと CLI へ空文字が渡り argparse が落ちる。しかも
        workflow_dispatch では通るため、手動テストでは絶対に踏めない。
        """
        exprs = re.findall(r"\$\{\{([^}]*github\.event\.inputs[^}]*)\}\}",
                           _run_scripts(workflow))
        assert exprs, (
            "`github.event.inputs.*` の参照が1つも見つからない。参照の書き方を変えたなら"
            "この正規表現も追随させること（空振りで緑になるのを防ぐ）"
        )
        for expr in exprs:
            assert "||" in expr, (
                f"`{expr.strip()}` に既定値のフォールバックが無い。"
                "schedule 起動では空文字が渡って必ず失敗する"
            )

    def test_workflow_defaults_match_cli_defaults(self, workflow):
        """ワークフロー側の既定値と CLI 定数の乖離を防ぐ（手動と cron で別設定になる）。"""
        from recommend_factor_premia import (
            DEFAULT_MAXLAGS,
            DEFAULT_MIN_COMPANIES_PER_PERIOD,
        )

        inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
        expected = {
            "min_companies_per_period": DEFAULT_MIN_COMPANIES_PER_PERIOD,
            "maxlags": DEFAULT_MAXLAGS,
        }
        script = _run_scripts(workflow)
        for key, cli_default in expected.items():
            assert inputs[key]["default"] == str(cli_default), (
                f"{key} の既定値がワークフロー（{inputs[key]['default']}）と "
                f"CLI（{cli_default}）で食い違っている"
            )
            # schedule 起動時に実際に使われるのは `||` の右辺なのでそちらも一致させる
            assert f"github.event.inputs.{key} || '{cli_default}'" in script, (
                f"{key} の cron 時フォールバックが CLI 既定（{cli_default}）と一致しない"
            )


# ── 5. 実行と最小権限 ────────────────────────────────────────────────────────

class TestJob:
    def test_runs_the_cli_with_persist(self, workflow):
        """--persist が無いと計算するだけで DB へ書かれない＝静かな空振りになる。"""
        script = _run_scripts(workflow)
        assert "python recommend_factor_premia.py" in script
        assert "--persist" in script

    def test_piped_steps_set_pipefail(self, workflow):
        """tee すると exit code が上書きされ、失敗が success に化ける（#352）。"""
        for step in workflow["jobs"]["estimate"]["steps"]:
            script = step.get("run", "")
            if "| tee" in script:
                assert "set -o pipefail" in script, (
                    f"'{step.get('name')}' が pipefail 無しで tee している"
                )

    def test_permissions_are_minimal(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}

    def test_timeout_is_bounded(self, workflow):
        """実測 job wall 2分54秒（2026-08-08・run 31265047095）。

        起票時の 120 分は未計測の当て推量で、ハングしても2時間気づけなかった。
        上限を設けるのは「締めすぎると平常運転が誤起票になる」（#453）との綱引き。
        """
        timeout = workflow["jobs"]["estimate"]["timeout-minutes"]
        assert 0 < timeout <= 60, (
            f"timeout-minutes={timeout}。実測の桁（3分）に対して緩すぎる／厳しすぎる。"
            "変更するなら実走ログの job wall を根拠に添えること"
        )
