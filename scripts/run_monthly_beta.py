"""M-1 の入力 `macro_beta_loadings` を専用タスクで作る（Issue #579・親 #532 / #504）。

## なぜ月次本体から切り出すか

`macro_beta`（PyMC/NUTS 階層マクロ・ベータ）の本番規模の実測は **360分**（2026-09-05・
3,837銘柄・n_obs 95,010・draws=800・`.logs/bench_600_argmin.jsonl`）。月次本体の
`BUDGET_MIN["macro_beta"]` は 180分で、**予算を増やそうにも本体の窓（960分）に空きが無い**
（Σ863 + マージン30）。#584 が M-1 探索を切り出したのと同じ形で、ここへ出す。

**所要が縮む見込みは無い**ことは #600 で確定した:

- `target_accept` を下げる案は実測で棄却（飽和は解けるが `ess_bulk_min` 155.2 → 73.59）
- `max_tree_depth` を上げる案も実測で棄却。そもそも **「`steps/draw` が 1023 に張り付いて
  いる」という前提自体が誤り**で、上限を倍にしても歩数も ESS もビット単位で同一だった
  （深さ10の自然な U ターン）＝**軌道長のレバーは最初から存在しなかった**

## 起動日と依存順

| タスク | 日 | 中身 |
|---|---|---|
| `financial_app-monthly` | 1日 | 収集・vacuum・factor_premia・M-2/M-3 探索 |
| **`financial_app-monthly-beta`（これ）** | **2日** | `macro_beta` |
| `financial_app-monthly-m1` | **3日** | M-1 探索（`tune:macro_risk_return`） |

**M-1 探索より前でなければならない**——`macro_beta_loadings` は `tune:macro_risk_return` の
入力なので、後ろに置くと探索は常に1か月前の loadings を見ることになる（#584 が2日に置いた
M-1 探索を3日へずらしたのはこのため）。

## 落ちても結果を捨てない（#609）

収束ゲート（`r_hat_max <= 1.05`）に落ちた run は **`status=quarantined` で persist** され、
producer からは見えないまま保全される。2026-09-03 は 6時間かけて完走した run が reject で
丸ごと消え、測り直しになった。ただし **exit は非0のまま**にしてある——隔離は「M-1 が
更新されていない」という運用上の失敗であり、静かに固着させない（それが #579 の元の症状）。

`--force` を付けると収束ゲートを無視して `live` として書く。**タスク登録側では渡さない**
（毎月の自動実行は通常判定）。人手で結果を精査したときの1回きりの経路で、初回の足跡入れも
これで兼ねる。

## 走らなかったことの検知

月次本体と同じく `app_settings` へ足跡を残し、`batch_freshness.WATCHED` が読む。
**監視表への追加を忘れると「走らなかったのに誰も気づかない」形でしか現れない**ので、
`tests/test_check_batch_freshness.py::TestEveryLocalBatchIsWatched` が `scripts/run_*.py` の
集合と監視表を CI で照合する。

実行:
    python -m scripts.run_monthly_beta                # 通常（ゲート判定あり）
    python -m scripts.run_monthly_beta --dry-run      # 実行計画だけ出す
    python -m scripts.run_monthly_beta --force        # ゲートを無視して live で persist

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

KEY_LAST_RUN = "monthly_beta_last_run"
KEY_LAST_SUCCESS = "monthly_beta_last_success"

ISSUE_LABELS = bc.ISSUE_LABELS

# タスクスケジューラの窓（`install_monthly_beta_task.ps1` の既定 `-Hours 16`）。月次本体と
# 同じ幅で起動日だけずらす。**この値と下の予算はセットでしか意味を持たない**ので
# `tests/test_run_monthly_beta.py` が ps1 側の既定と突き合わせる。
WINDOW_MIN = 16 * 60

# ステップごとの時間予算（分・#530・ADR-0040）。**Σ + マージン ≤ WINDOW_MIN**。
#
# 900分は窓から導出した値（960 − マージン30 − deps_smoke 5 ＝ 925 が上限）であって、
# 実測 360分から逆算したものではない（ADR-0040）。パネルは毎晩伸びるので所要は据え置かず
# 伸びる——**実測へ寄せると、伸びた月に予算切れで全損になる**（探索と違い macro_beta は
# 打ち切られると persist に到達しない）。
BUDGET_MIN: dict[str, float] = {
    "deps_smoke": 5,
    "macro_beta": 900,
}

SPEC = bc.BatchSpec(
    name="月次バッチ（macro_beta）",
    log_prefix="monthly_beta",
    key_run=KEY_LAST_RUN,
    key_success=KEY_LAST_SUCCESS,
    job_label="monthly-beta-local",
    issue_title="[ops] ローカル月次バッチ（macro_beta）失敗: {failed}",
    headline="ローカル月次バッチ（`scripts/run_monthly_beta.py`）でステップが失敗した。",
)


def steps_for(python: str, force: bool = False) -> tuple[Step, ...]:
    """実行するステップ列。

    `deps_smoke` を先に置くのは月次本体と同じ理由——重い依存を import できないなら以降は
    全部同じ理由で落ちるので、900分の予算を待たずに失敗として現れる方がよい。2026-09-01 は
    Smart App Control が未評価の jaxlib DLL を弾いて `macro_beta` が 1.4分で落ちた。
    """
    beta_argv = [
        python, "macro_beta_inference.py",
        "--draws", "800", "--tune", "800", "--target-accept", "0.95",
        "--chains", "2", "--r-hat-threshold", "1.05",
        "--nuts-sampler", "numpyro", "--init", "adapt_diag",
        # warmup だけ軌道長を 2**8-1 歩へ切る（#540・ADR-0002）。**draws 側は既定 10 のまま**。
        # 一律キャップ（`--max-tree-depth 8` 等）は ESS/歩 では最良に見えるのに
        # ess_bulk_min が 3.55 まで落ち r_hat が 1.63 になる＝**採ってはいけない**。
        "--max-tree-depth", "8,10",
    ]
    if force:
        beta_argv.append("--force")

    steps: list[Step] = [
        Step("deps_smoke", (python, "-m", "scripts.check_heavy_imports"),
             why="重い依存（pymc / jax / numpyro 等）が実際に import できるかを確かめる。"
                 "2026-09-01 の月次では Smart App Control が 8/21 の jaxlib 更新で入った"
                 "未評価の `_ifrt_proxy.pyd` を初回ロードでブロックし、`macro_beta` が "
                 "exit=1 で落ちて 1か月ぶんの `macro_beta_loadings` が固着した。"
                 "**未評価 DLL の初回ロードをここが引き受ける**"),
        Step("macro_beta", tuple(beta_argv),
             why="M-1 の入力 macro_beta_loadings（PyMC/NUTS 階層マクロ・ベータ）。"
                 "本番規模の実測 360分で月次本体の窓に入らないためここへ切り出した（#579）。"
                 "収束ゲートに落ちた run は status=quarantined で保全され、"
                 "exit は非0になる（#609）"),
    ]
    # 予算は**名前で引く**（Step へ直書きしない）。付け忘れを `window_problem` が CI で落とす。
    return tuple(replace(s, budget_min=BUDGET_MIN.get(s.name)) for s in steps)


def heavy_models() -> tuple[str, ...]:
    """このバッチが回す heavy プラグイン名（`HEAVY_AUTOMATION` の照合先）。

    `macro_beta` は分析プラグインではなく推論バッチなので `--model` を持たず、ここは空になる。
    ステップの argv から抜き出す形は月次本体・M-1 探索と揃える（列挙を二重に持たない）。
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
    # `--force` は共通パーサ（`batch_common.build_parser`）が知らないので、ここで先に外す。
    # 共通側へ足さないのは、**force を持ってよいバッチはここだけ**だから（収集や探索に
    # 「ゲートを無視する」概念は無く、増やすと意味が曖昧になる）。
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    while "--force" in args:
        args.remove("--force")

    # フックは**ここでモジュール属性として解決する**——テストが差し替えたときに効くように。
    hooks = bc.Hooks(log_path=log_path, record_footprint=record_footprint, notify=notify)
    return bc.run_batch(SPEC, steps_for(sys.executable, force=force), hooks, args)


if __name__ == "__main__":
    raise SystemExit(main())
