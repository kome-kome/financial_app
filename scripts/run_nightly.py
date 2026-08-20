"""夜間バッチのローカル駆動（Issue #503 Phase 2・ADR-0038）。

## なぜ必要か

正本がローカル PostgreSQL へ移り、GHA の cron を全て止めた（#503）。GHA はクラウドで
走るのでローカル DB へは書けない＝**収集とスコア更新の駆動主体をこちら側へ持ってくる**
必要がある。Windows タスクスケジューラから叩かれるのがこのモジュール。

`.ps1` ではなく Python で実体を書いている理由は2つある: PowerShell だと BOM 無しで
cp932 扱いになって日本語が化ける／`python -c` に渡す文字列のクォートが native exe の
引数で剥がれる、という**実行するまで出ない**罠が2つとも回避できること。もう一つは、
ステップ順や失敗時の扱いを pytest で縛れること（`tests/test_run_nightly.py`）。
`run_nightly.ps1` はこのモジュールを呼ぶだけの薄い起動口である。

骨格（ステップ間で止めない・足跡を残す・失敗を起票する）は `scripts/batch_common.py`
にあり、月次（`run_monthly.py`）と共有する。ここが持つのは**何を回すか**だけ。

## 設計の中心: ステップ間で止めない

収集が落ちてもスコア更新は前日データで走らせ、**両方の結果をログに残す**。片方の失敗で
全部を落とすと、翌朝に分かるのが「最初の失敗」だけになり、原因の切り分けにもう1日かかる。
`nightly_scores.py` が1モデルの失敗で他を止めないのと同じ方針を、1段上へ広げたもの。

## 「走らなかった」ことを検知できるようにする

失敗より **そもそも起動しなかった** 方が静かで危ない（PC がスリープ・タスクが無効化・
venv が壊れた）。失敗は誰も見ていなければ気づけないので、実行のたびに `app_settings` へ
足跡を残す:

- `nightly_last_run`      … 最後に走った時刻（成否によらず）
- `nightly_last_success`  … 最後に全ステップ成功した時刻

GHA の notify-failure（#414）に相当する通知は `gh issue create` で行う。gh CLI は
認証済みの前提で、**使えなくてもバッチは落とさない**（通知の失敗が本業を止めるのは本末転倒）。

実行:
    python -m scripts.run_nightly              # 全ステップ
    python -m scripts.run_nightly --dry-run    # 実行計画だけ出す
    python -m scripts.run_nightly --steps pipeline,scores
    python -m scripts.run_nightly --no-issue   # 失敗しても Issue を起票しない

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from scripts import batch_common as bc
from scripts.batch_common import LOG_DIR, ROOT, Runner, Step  # noqa: F401 （既存 import 互換）

# 最終成功・最終実行の足跡を置く app_settings のキー。
KEY_LAST_RUN = "nightly_last_run"
KEY_LAST_SUCCESS = "nightly_last_success"

# 失敗を起票するときのラベル（GHA の notify-failure と同じ運用に載せる）。
ISSUE_LABELS = bc.ISSUE_LABELS

SPEC = bc.BatchSpec(
    name="夜間バッチ",
    log_prefix="nightly",
    key_run=KEY_LAST_RUN,
    key_success=KEY_LAST_SUCCESS,
    job_label="nightly-local",
    issue_title="[ops] ローカル夜間バッチ失敗: {failed}",
    headline="ローカル夜間バッチ（`scripts/run_nightly.py`）でステップが失敗した。",
)


def steps_for(python: str) -> tuple[Step, ...]:
    """実行するステップ列。**順序に意味がある**（鮮度 → スコア）。

    スコアは株価・財務を入力に取るので、収集より先に回すと前日のデータで上書きされる。
    #423 の「鮮度が先」と同じ依存順で、GHA では daily-incremental → nightly-scores の
    workflow_run チェーンがこれを表現していた。ローカルでは並びがその契約になる。

    **収集は `_pipeline_incremental.py` を呼ぶ。** `collector.py --incremental` ではない——
    あちらが回すのは `run_full_collection`（企業マスタ・書類スキャン・XBRL・業種補完）だけで、
    **株価を1バイトも更新しない**。株価鮮度の担い手は pipeline の Phase 4
    （`fill_recent_stock_price_gap_yahoo` → J-Quants で公式値へ置換）にある。
    2026-08-20 に `collector.py --incremental` で12日ぶんの欠測を埋めようとして、
    財務だけ通り株価が動かないのを実測した。GHA が回していたのと同じ入口を使う。
    """
    return (
        Step("pipeline", (python, "_pipeline_incremental.py"),
             why="XBRL 差分 ＋ マクロ ＋ 市場データ（株価鮮度の担い手・GHA と同じ入口）"),
        Step("scores", (python, "nightly_scores.py"),
             why="sector_ols / macro_enet のスコア更新（producer の永続化）"),
    )


def heavy_models() -> tuple[str, ...]:
    """このバッチが実際に回す heavy プラグイン名（`HEAVY_AUTOMATION` の照合先）。

    `nightly_scores.py` を引数なしで呼ぶ＝既定の `NIGHTLY_MODELS` がそのまま対象。
    列挙をここへ書き写すと二重管理になるので、実体から取る。
    """
    from nightly_scores import NIGHTLY_MODELS

    return tuple(NIGHTLY_MODELS)


def log_path(now=None) -> Path:
    return bc.log_path(SPEC.log_prefix, now)


def record_footprint(results: dict[str, int]) -> Optional[str]:
    return bc.record_footprint(results, SPEC.key_run, SPEC.key_success)


def issue_body(results: dict[str, int], log: Path) -> str:
    return bc.issue_body(results, log, SPEC.headline)


def notify(results: dict[str, int], log: Path, run=subprocess.run) -> Optional[str]:
    return bc.notify(results, log, SPEC.issue_title, issue_body(results, log), run=run)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # フックは**ここでモジュール属性として解決する**——テストが差し替えたときに効くように。
    hooks = bc.Hooks(log_path=log_path, record_footprint=record_footprint, notify=notify)
    return bc.run_batch(SPEC, steps_for(sys.executable), hooks, argv)


if __name__ == "__main__":
    raise SystemExit(main())
