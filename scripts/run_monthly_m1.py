"""M-1（macro_risk_return）の探索を専用タスクで回す（Issue #584・親 #532 / #579）。

## なぜ月次本体から切り出すか

2026-09-01 の月次バッチ初実走で、`tune:macro_risk_return` は **250分の予算を使い切って
15/288 しか進まなかった**（うち最後の 156分は heartbeat を出しながら1件も進んでいない）。
`plugins/macro_snapshots` のキャッシュ修正（#588）でメモリ枯渇による停止は解消したが、
**所要そのものは変わらない**——実測 2.61分/件 × 288件 ＝ **約752分**で、月次の窓（16時間＝
960分）のほぼ全部を1本で食う。

一方 `hyperparameter_search.py` の永続化は `search()` が**完走してから**しか走らない。
予算内に終わらないステップは時間を使い切って**何も残さない**ので、中途半端な予算は節約では
なく全損になる。9/1 は M-1 に 250分・M-3 に 250分を与えて**両方とも成果ゼロ**だった。

そこで「完走見込みのあるステップにだけ予算を与える」を原則に置き、**窓に入らない M-1 を
別タスクへ出す**。月次本体は Σ863分（+マージン30）で収まり、M-2 / M-3 は毎月完走できる。
M-1 はここで 900分の予算を得て完走する。

## 実測（12〜14候補・同一手続き・DB 非書き込み）

| | 修正前 | 修正後（#588） |
|---|---|---|
| peak ツリー常駐 | 4,908MB | 3,885MB |
| 定常常駐 | 約4,500MB | 約2,700MB |
| [9]以降の劣化 | +21%（3.37分/件） | なし（2.62分/件） |
| `[13/288]`（実走の停止点） | 156分間0件 | 2.98分/件で継続 |

GHA 実績は 178分（0.618分/件）で、ローカルは**メモリが足りていても 4.2倍遅い**。これは
マシンの性質（i5-8400・6コア6スレッド）であってキャッシュ修正では消えない。

## 2026-09-02 の初実走で前提が2つ動いた（**予算はまだ動かさない**）

1. **上の 2.61分/件・4.2倍は対話セッションを開いたまま測った値**だった。実走を無人で回すと
   同じ `use_macro=True` の区間で **2.18分/件**（対話ありは 3.28分/件＝1.5倍）、288件の完走は
   **513.9分**（GHA 比 2.9倍）。「ローカルは4〜6倍遅い」はマシンの性質ではなく、
   かなりの部分が**測定時に画面を開いていたこと**を測っていた
2. **探索空間が 288 → 72件になった**（#596・ADR-0049）。`min_coverage` の4値は結果を1ミリも
   変えていない（288件全件で72群すべて群内不変・`min_coverage=1.0` でもサンプル総数が不変）
   ＝72通りの結果を288回計算していた

実測ペース（無人）で外挿すると 72件は約112分。**ただし確定は 10/2 の実走まで待つ**ので
`BUDGET_MIN` は 900分のまま据え置く（ADR-0040「予算は実測から逆算しない」）。
月次本体へ戻すかどうかも、その実測を見てから判断する（本体の空きは67分しかない）。

## 走らなかったことの検知

月次本体と同じく `app_settings` へ足跡を残し、`batch_freshness.WATCHED` が読む。
**監視表への追加を忘れると「走らなかったのに誰も気づかない」形でしか現れない**ので、
`tests/test_check_batch_freshness.py::TestEveryLocalBatchIsWatched` が `scripts/run_*.py` の
集合と監視表を CI で照合する（ADR-0031 と同型の穴を塞ぐ仕組み）。

実行:
    python -m scripts.run_monthly_m1                          # 全ステップ
    python -m scripts.run_monthly_m1 --dry-run                # 実行計画だけ出す
    python -m scripts.run_monthly_m1 --no-issue               # 失敗しても Issue を起票しない

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

KEY_LAST_RUN = "monthly_m1_last_run"
KEY_LAST_SUCCESS = "monthly_m1_last_success"

ISSUE_LABELS = bc.ISSUE_LABELS

# 回すモデル。**月次本体の TUNE_MATRIX からここへ移した**ので、両方に書かない。
MODEL = "macro_risk_return"
STRATEGY = "grid"

# タスクスケジューラの窓（`install_monthly_task.ps1` の既定 `-Hours 16`）。月次本体と同じ幅で、
# 起動日だけ翌日へずらす（本体は1日・こちらは2日）。**この値と下の予算はセットでしか
# 意味を持たない**ので `tests/test_run_monthly_m1.py` が ps1 側の既定と突き合わせる。
WINDOW_MIN = 16 * 60

# ステップごとの時間予算（分・#530・ADR-0040）。**Σ + マージン ≤ WINDOW_MIN**。
#
# `tune:macro_risk_return` の 900分は **実測 752分（2.61分/件 × 288件）＋ 約20%の余裕**。
# ADR-0040 の「予算は実測から逆算しない」に従い実測へ寄せていない——パネルは毎晩伸びるので
# 所要は据え置かず伸びる（#446 と同じ話）。窓 960 − マージン 30 − deps_smoke 5 ＝ 925 が
# 上限なので、ここが実質いっぱいまで取った値になる。
BUDGET_MIN: dict[str, float] = {
    "deps_smoke": 5,
    "tune:macro_risk_return": 900,
}

SPEC = bc.BatchSpec(
    name="月次バッチ（M-1 探索）",
    log_prefix="monthly_m1",
    key_run=KEY_LAST_RUN,
    key_success=KEY_LAST_SUCCESS,
    job_label="monthly-m1-local",
    issue_title="[ops] ローカル月次バッチ（M-1 探索）失敗: {failed}",
    headline="ローカル月次バッチ（`scripts/run_monthly_m1.py`）でステップが失敗した。",
)


def steps_for(python: str) -> tuple[Step, ...]:
    """実行するステップ列。

    `deps_smoke` を先に置くのは月次本体と同じ理由——重い依存を import できないなら以降は
    全部同じ理由で落ちるので、900分の予算を待たずに失敗として現れる方がよい（#584）。
    """
    steps: list[Step] = [
        Step("deps_smoke", (python, "-m", "scripts.check_heavy_imports"),
             why="重い依存（numpy / scipy / sklearn 等）が実際に import できるかを確かめる。"
                 "2026-09-01 の実走では Smart App Control が未評価の DLL を初回ロードで"
                 "ブロックし、本番ステップが exit=1 で落ちた。ここで消化しておけば "
                 "900分の予算を待たずに失敗が現れる"),
        Step(f"tune:{MODEL}",
             (python, "hyperparameter_search.py", "--model", MODEL,
              "--strategy", STRATEGY,
              "--objective", "rank_ic", "--persist", "--persist-scores", "--seed", "0"),
             why="M-1 の best params 探索と mu-hat の永続化（--persist-scores）。"
                 "**止まって最も困るのが M-1 の μ̂**（`sell_ranking` の mu_source 候補）。"
                 "実測 2.61分/件 × 288件 ＝ 約752分で月次本体の窓に入らないため、"
                 "ここへ切り出してある（#584）"),
    ]
    # 予算は**名前で引く**（Step へ直書きしない）。付け忘れを `window_problem` が CI で落とす。
    return tuple(replace(s, budget_min=BUDGET_MIN.get(s.name)) for s in steps)


def heavy_models() -> tuple[str, ...]:
    """このバッチが実際に回す heavy プラグイン名（`HEAVY_AUTOMATION` の照合先）。

    ステップの argv から `--model` を抜き出す＝**列挙を二重に持たない**。
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
