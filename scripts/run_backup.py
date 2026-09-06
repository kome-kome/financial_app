"""正本のバックアップを週次で取り、Supabase Storage へ置く（Issue #606・親 #503 Phase 3）。

## なぜ自動化するか

**戻せることは確かめたのに、取るほうが人の記憶に置かれたままだった。** #503 検証4 で
Storage から 17表を落として使い捨てクラスタへ復元し、行数一致と画面9本の表示まで通した
（復元経路は生きている）。しかし `scripts/backup_push.py` はどのバッチにも入っておらず、
`grep backup scripts/run_*.py` はヒット0件だった。

#503 は「Supabase の停止でサービスが2週間止まった」ことへの対策だった。その対策が完了した
今、**同じ長さの損失が「取り忘れ」という別の形で残っている**のが本件である。

起票時（2026-09-04）の Storage は `20260821T013818Z` の1本だけで実効 RPO 14日だったが、
翌朝に手で叩かれて2本になった（`.logs/backup_20260905.log`）。**2日で済んでいたのは
たまたま最近叩いたからで、次にいつ取られるかは誰も保証していない**。守るべきは「今の間隔」
ではなく「最悪でもこれを超えない」という上限で、それは自動化でしか作れない。

## 週次・独立タスクである理由

`docs/DEPLOYMENT.md` が元から「夜間バッチと**別タスク**にする（遅延が道連れにならない）」と
書いており、それに従う。月次本体のステップにすると、本体が長引いた月はバックアップも一緒に
遅れる——**バックアップは他が全部こけた日にこそ効く**ものなので、他の処理と運命を共有させない。
月次にすると RPO も31日になり、起票時点の14日より悪くなる。

時間帯は衝突しない（夜間バッチは 17:20 開始・実測約70分で 18:30 頃に終わる）。

## 1ステップしか無いのにバッチの骨格へ乗せる理由

`batch_common` が提供するのは実行そのものではなく、**「走らなかったこと」を検知できる形**
である（足跡・通知・heartbeat・ステップ予算）。手で叩く CLI のままだと、取り忘れも失敗も
同じ「何も起きない」に見える。`batch_freshness.WATCHED` へ載せて初めて、止まったことが
watchdog（毎日 JST 20:00）から Issue として現れる。

実行:
    python -m scripts.run_backup                # 通常
    python -m scripts.run_backup --dry-run      # 実行計画だけ

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from scripts import batch_common as bc
from scripts.batch_common import LOG_DIR, ROOT, Runner, Step  # noqa: F401 （既存 import 互換）

KEY_LAST_RUN = "backup_last_run"
KEY_LAST_SUCCESS = "backup_last_success"

ISSUE_LABELS = bc.ISSUE_LABELS

# タスクスケジューラの窓（`install_backup_task.ps1` の既定 `-Hours 2`）。
# **この値と下の予算はセットでしか意味を持たない**ので `tests/test_run_backup.py` が
# ps1 側の既定と突き合わせる。
WINDOW_MIN = 2 * 60

# ステップごとの時間予算（分・ADR-0040）。**Σ + マージン ≤ WINDOW_MIN**。
#
# 90分は窓から導出した値（120 − マージン30）であって、実測から逆算したものではない。
# 実測は 2026-09-05 で 38.1MB / 17表・ダンプ1分未満＋アップロードで数分規模だが、
# **パネルは毎晩伸びる**（`stock_price_weekly` が最大表）ので実測へ寄せると、伸びた週に
# 打ち切られて世代がまるごと残らない。窓を広く取る害は「ハングした回が最大2時間居座る」
# ことだけで、次の実行まで7日ある。
BUDGET_MIN: dict[str, float] = {
    "push": 90,
}

SPEC = bc.BatchSpec(
    name="週次バックアップ",
    log_prefix="backup",
    key_run=KEY_LAST_RUN,
    key_success=KEY_LAST_SUCCESS,
    job_label="backup-local",
    issue_title="[ops] ローカル週次バックアップ失敗: {failed}",
    headline="週次バックアップ（`scripts/run_backup.py`）でステップが失敗した。",
)


def steps_for(python: str) -> tuple[Step, ...]:
    """実行するステップ列。

    **`--apply` と `--dest storage` は両方が必須**である。どちらが欠けても
    `backup_push` は exit 0 を返すため、失敗としては現れない:

    - `--apply` が無い … ドライラン（計画を出して何もしない）
    - `--dest storage` が無い … ローカルの `.backups/` に作るだけで、**避難先へ届かない**
      （同じディスク上なので正本と一緒に失われる＝バックアップとして意味を成さない）

    `tests/test_run_backup.py` が argv を文字列ごと固定する。
    """
    steps: list[Step] = [
        Step("push", (python, "-m", "scripts.backup_push", "--apply", "--dest", "storage"),
             why="正本（ローカル PG）を17表ぶんダンプして Supabase Storage へ置く。"
                 "取る側が止まっていると、#503 で通した復元経路があっても"
                 "戻せるのは最後に人が手で叩いた日までになる（#606）"),
    ]
    # 予算は**名前で引く**（Step へ直書きしない）。付け忘れを `window_problem` が CI で落とす。
    return tuple(replace(s, budget_min=BUDGET_MIN.get(s.name)) for s in steps)


def heavy_models() -> tuple[str, ...]:
    """このバッチが回す heavy プラグイン名（`HEAVY_AUTOMATION` の照合先）。

    バックアップは分析ではないので `--model` を持たず、ここは空になる。列挙を二重に持たない
    形（ステップの argv から抜く）を他バッチと揃える。
    """
    return bc.models_from_steps(steps_for(sys.executable))


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
