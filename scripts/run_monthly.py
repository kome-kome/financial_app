"""月次バッチのローカル駆動（Issue #504・親 #503・ADR-0038）。

## なぜ必要か

#503 で正本がローカル PostgreSQL へ移り、GHA の cron を全て止めた。日次
（`run_nightly.py`）はローカルへ移したが、**月次3本は止まったままだった**:

| 止まっていたもの | 影響 |
|---|---|
| `tune-hyperparameters.yml` | M-1 / M-2 / M-3 の唯一の自動更新経路。μ̂ の鮮度が止まる |
| `macro-beta-inference.yml` | `macro_beta_loadings`（M-1 の入力）が固着する |
| `recommend-factor-premia.yml` | #423 子5 で「実行履歴ゼロのまま37期の重みで固着」を直した cron |

いずれも**止めても failure は出ない**（無実行は成功でも失敗でもない）ので、
notify-failure でも macro-health でも拾えない。ADR-0031 が防ごうとした穴と同型で、
今回は自分たちの都合で開けた穴である。ここがそれを塞ぐ。

## ステップの並び

    factor_premia → macro_beta → tune:macro_risk_return → tune:macro_dlm → tune:macro_gbdt

2つの制約の積:

1. **依存順**: `macro_beta_loadings` は M-1（`macro_risk_return`）の入力なので、
   推論が tune より先。
2. **軽い順**: 途中で打ち切られても先に終わったものは当月分が揃う（`nightly_scores.py`
   の `NIGHTLY_MODELS` と同じ思想）。実測 factor_premia は約2分、tune は GHA で
   300〜355分の timeout を積んでいた。tune の中では M-1 を先に置く——止まって困る
   度合いが最も高いのが M-1 の μ̂ だから。

## GHA と同じ引数を使う

探索空間・`--n-iter`・`--r-hat-threshold` は **GHA の yml に書いてあった値をそのまま**
持ってくる。ローカルには6時間のジョブ上限が無いので `--n-iter` を増やす余地はあるが、
増やすと `plugin_tuned_params` の `objective_value` 比較（#291 の品質ゲート）が
別条件の値と比較されることになる。**探索規模を動かすのは、動かした影響を測る回**で
やるべきで、移設のついでにやらない。`--chains 2` と `--r-hat-threshold 1.05` も同じ理由で
据え置く（chains を増やすと r_hat の水準が変わり、閾値の意味も変わる。生値での再実測に
基づいて判断する宿題が #356 にある）。

## 「走らなかった」ことを検知できるようにする

日次と同じく `app_settings` へ足跡を残す。月次は**1か月に1度しか走らない**ぶん、
気づかず止まっているとその期間がまるごと固着する:

- `monthly_last_run`      … 最後に走った時刻（成否によらず）
- `monthly_last_success`  … 最後に全ステップ成功した時刻

実行:
    python -m scripts.run_monthly                        # 全ステップ
    python -m scripts.run_monthly --dry-run              # 実行計画だけ出す
    python -m scripts.run_monthly --steps factor_premia  # 一部だけ
    python -m scripts.run_monthly --no-issue             # 失敗しても Issue を起票しない

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from scripts import batch_common as bc
from scripts.batch_common import LOG_DIR, ROOT, Runner, Step  # noqa: F401 （既存 import 互換）

KEY_LAST_RUN = "monthly_last_run"
KEY_LAST_SUCCESS = "monthly_last_success"

ISSUE_LABELS = bc.ISSUE_LABELS

# tune の matrix（GHA の tune-hyperparameters.yml から移設）。
# (モデル名, 探索戦略, 追加引数) — 並びがそのまま実行順になる。
TUNE_MATRIX: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # M-1: 止まって最も困る（`sell_ranking` の mu_source 候補・macro_beta_loadings の消費者）
    ("macro_risk_return", "grid", ()),
    ("macro_dlm", "grid", ()),
    # M-2 だけ random なのは、grid では 1イテレーションの walk-forward OOF 評価が重すぎて
    # 空間を張れないため（GHA では n_iter=200 相当で4〜8時間＝6時間上限に収まらなかった）。
    ("macro_gbdt", "random", ("--n-iter", "150")),
)

SPEC = bc.BatchSpec(
    name="月次バッチ",
    log_prefix="monthly",
    key_run=KEY_LAST_RUN,
    key_success=KEY_LAST_SUCCESS,
    job_label="monthly-local",
    issue_title="[ops] ローカル月次バッチ失敗: {failed}",
    headline="ローカル月次バッチ（`scripts/run_monthly.py`）でステップが失敗した。",
)


def steps_for(python: str) -> tuple[Step, ...]:
    """実行するステップ列。**順序に意味がある**（依存順 ∧ 軽い順・モジュール docstring 参照）。"""
    steps: list[Step] = [
        Step("factor_premia",
             (python, "recommend_factor_premia.py",
              "--min-companies-per-period", "30", "--maxlags", "11", "--persist"),
             why="recommend「統計的最適化」プリセットの Fama-MacBeth 重み"
                 "（止めると #423 子5 で直した 37期固着へ戻る）"),
        Step("macro_beta",
             (python, "macro_beta_inference.py",
              "--draws", "800", "--tune", "800", "--target-accept", "0.95",
              "--chains", "2", "--r-hat-threshold", "1.05",
              "--nuts-sampler", "numpyro", "--init", "adapt_diag"),
             why="M-1 の入力 macro_beta_loadings（PyMC/NUTS 階層マクロ・ベータ）"),
    ]
    for model, strategy, extra in TUNE_MATRIX:
        steps.append(Step(
            f"tune:{model}",
            (python, "hyperparameter_search.py", "--model", model,
             "--strategy", strategy, *extra,
             "--objective", "rank_ic", "--persist", "--persist-scores", "--seed", "0"),
            why=f"{model} の best params 探索と mu-hat の永続化（--persist-scores）"))
    return tuple(steps)


def heavy_models() -> tuple[str, ...]:
    """このバッチが実際に回す heavy プラグイン名（`HEAVY_AUTOMATION` の照合先）。

    ステップの argv から `--model` を抜き出す＝**列挙を二重に持たない**。
    `TUNE_MATRIX` を書き換えれば自動的にここへ反映される。
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
