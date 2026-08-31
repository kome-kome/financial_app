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

    vacuum → price_suffix → deps_smoke → factor_premia → macro_beta
           → tune:macro_risk_return → tune:macro_dlm → tune:macro_gbdt

`deps_smoke` は**重い依存を import できるかだけを確かめる軽いステップ**。2026-09-01 の初実走で
Smart App Control が未評価の jaxlib DLL を初回ロードで弾き、`macro_beta` が exit=1 で落ちた
（`scripts/check_heavy_imports.py`）。ここで消化すれば本番ステップは通り、消化できなければ
180分の予算を待たずに失敗として現れる。**`macro_beta` より前**でありさえすれば役目は果たすので、
位置は下の「軽い順」に従う。

残りは2つの制約の積:

1. **依存順**: `macro_beta_loadings` は M-1（`macro_risk_return`）の入力なので、
   推論が tune より先。
2. **軽い順**: 途中で打ち切られても先に終わったものは当月分が揃う（`nightly_scores.py`
   の `NIGHTLY_MODELS` と同じ思想）。実測 factor_premia は約2分、tune は GHA で
   300〜355分の timeout を積んでいた。tune の中では M-1 を先に置く——止まって困る
   度合いが最も高いのが M-1 の μ̂ だから。

`vacuum` を先頭に置くのは、**ACCESS EXCLUSIVE ロックを取るから**である。後ろに置くと
tune が長引いたぶんだけ実行機会が減り、上限（既定16時間）で打ち切られると一度も走らない。
先頭なら必ず走り、他ステップとロックを奪い合うこともない。

週次ではなく月次で足りると判断した理由: `_pipeline_vacuum.py` が前段で per-table の
`autovacuum_vacuum_scale_factor` を 0.02 へ較正するので、dead tuple は 2% 溜まった時点で
通常 VACUUM が回収し続ける（テーブル内再利用）。月次で要るのは**物理サイズの頭打ち**だけで、
ローカルには Supabase の 500MB 枠のような崖が無い。Supabase 時代に週次だったのは、
枠を超えた瞬間に read-only になる崖があったから。

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
from dataclasses import replace
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

# タスクスケジューラの窓（`install_monthly_task.ps1` の既定 `-Hours 16`）。**この値と
# 下の予算はセットでしか意味を持たない**ので、`tests/test_run_monthly.py` が ps1 側の
# 既定と突き合わせる（#530）。窓を動かすときは予算も動かすこと。
WINDOW_MIN = 16 * 60

# ステップごとの時間予算（分・#530）。**Σ + マージン ≤ WINDOW_MIN**。
#
# なぜ要るか: 予算が無いと1ステップが窓を食い尽くし、後続は起動すらしない。しかも窓の
# 終わりの打ち切りは failure として現れない（タスクスケジューラがプロセスを止めるだけで
# 足跡も起票も走らない）＝**tune×3 が静かに餓死する**。2026-09-01 にそうなる直前だった。
#
# 値は GHA 時代の実績から取った（推定ではなく実測）。GHA は実質2コアなので、ローカルが
# これを超えるならローカル固有の問題を疑う根拠になる:
#   macro_beta 116分（run 30698937879）/ tune は run 29118260702 のジョブ別で
#   macro_risk_return 178分・macro_dlm 175分・macro_gbdt 82分 / factor_premia はローカル実測 1.3〜2.6分
#
# macro_beta の 180分は「GHA 実績 116分 ＋ 余裕」であって、ローカルの実測ではない
# （ローカルは741.5分でも未完走・#512）。**#512 が解けるまで毎月ここで落ちる**のは想定内で、
# 静かに tune が餓死するより起票される失敗の方が良い、という判断（#530）。
#
# #540（`--max-tree-depth 8,10`）で総 leapfrog 歩数を 37.5% 削ったが、本番規模ローカルは
# 約20.2時間 → 約12.6時間で**まだ桁が違う**。予算は実測から逆算しない（ADR-0040）ので 180分は
# 据え置き＝毎月 exit=124 で起票される状態は継続する。窓の問題は #530 / #532 の担当で、
# #540 は「軌道長のレバーは使い切った」ことを示したに留まる。
BUDGET_MIN: dict[str, float] = {
    # 実測 0.3分（2026-09-01 の初実走・DB 839→831MB）／0.25分（2026-08-25 の手動 1回目）。
    # ADR-0040 は「予算値は実測が出たら見直す。特に vacuum はローカルでの実測を持っていない」
    # と留保していた枠で、その実測が出たので 45 → 15 へ引く。**実測へ寄せるのではなく桁の
    # 余裕を残す**（50倍）——VACUUM FULL はテーブルサイズに比例し、週次株価は増え続ける。
    # ここで空けた30分が deps_smoke の枠と窓の余裕になる。
    "vacuum": 15,
    # 重い依存を import するだけ（実測 20秒級）。**本番ステップの手前で未評価 DLL の初回
    # ロードを消化するのが役目**なので、ここが遅いこと自体は起きない。5分は起動と
    # jax.devices() の初期化を含めた余裕。
    "deps_smoke": 5,
    # 5社 × 2サフィックス × YAHOO_STOCK_RATE_SLEEP(0.5s) ≒ 5秒（#560）。3分は十分な余裕。
    # **全数 454社の `--reprobe` は約8分で入らない**——Σ925 + マージン30 = 955 に対し窓 960 で
    # 余裕が5分しかなく、窓の拡張も日次 17:20 起動と衝突するため不可（だから対象を絞った）。
    "price_suffix": 3,
    "factor_premia": 20,
    "macro_beta": 180,
    "tune:macro_risk_return": 250,
    "tune:macro_dlm": 250,
    "tune:macro_gbdt": 180,
}

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
        Step("vacuum", (python, "_pipeline_vacuum.py"),
             why="stock_price_daily / stock_price_weekly の index bloat 回収と "
                 "per-table autovacuum の較正（#290）。正本がローカルへ移ってから"
                 "メンテ経路が無かった"),
        Step("price_suffix",
             (python, "-m", "scripts.resolve_price_suffix", "--apply", "--bucket", "empty",
              "--backfill-weekly"),
             why="地方取引所に実在すると分かっている社（Yahoo が SAP/FKA と実名を返すのに"
                 "バーが0本）を、正しい取引所で再プローブする（#560）。バーが供給され始めた"
                 "瞬間に自動で拾う。**全数 454社は約8分で月次の窓に入らない**ので "
                 "`--bucket empty` の数社だけに絞ってある（約5秒）。"
                 "**`--backfill-weekly` は採用できた社にだけ走る**——解決しただけでは "
                 "daily 保持窓183日＝約26週しか付かず z_momentum の52週に届かない（#555）。"
                 "対象は最大でもバケットの社数なので窓を脅かさない"),
        Step("deps_smoke", (python, "-m", "scripts.check_heavy_imports"),
             why="重い依存（pymc / jax / numpyro 等）が実際に import できるかを確かめる。"
                 "2026-09-01 の初実走では Smart App Control が 8/21 の jaxlib 更新で入った"
                 "未評価の `_ifrt_proxy.pyd` を**初回ロードでブロック**し、`macro_beta` が "
                 "exit=1 で落ちて 1か月ぶんの `macro_beta_loadings` が固着した"
                 "（CodeIntegrity 3118/3077/3033・以後は同じ DLL が通る一過性の挙動）。"
                 "**未評価 DLL の初回ロードをここが引き受ける**ので本番ステップの手前で消化でき、"
                 "それでも落ちるなら 180分の予算を待たず起票される。"
                 "位置は「軽い順」の原則に従う（vacuum を除く先頭は最軽量の price_suffix）——"
                 "重い依存を実際に使う最初のステップ `macro_beta` より前でありさえすれば役目は果たす"),
        Step("factor_premia",
             (python, "recommend_factor_premia.py",
              "--min-companies-per-period", "30", "--maxlags", "11", "--persist"),
             why="recommend「統計的最適化」プリセットの Fama-MacBeth 重み"
                 "（止めると #423 子5 で直した 37期固着へ戻る）"),
        Step("macro_beta",
             (python, "macro_beta_inference.py",
              "--draws", "800", "--tune", "800", "--target-accept", "0.95",
              "--chains", "2", "--r-hat-threshold", "1.05",
              "--nuts-sampler", "numpyro", "--init", "adapt_diag",
              # warmup だけ軌道長を 2**8-1 歩へ切る（#540・ADR-0002）。**draws 側は既定 10 の
              # まま**＝事後分布の探索能力は変えずに総 leapfrog 歩数だけ 37.5% 減らす。
              # 一律キャップ（--max-tree-depth 8 等）は ESS/歩 では最良に見えるのに
              # ess_bulk_min が 3.55 まで落ち r_hat が 1.63 になる＝**採ってはいけない**。
              "--max-tree-depth", "8,10"),
             why="M-1 の入力 macro_beta_loadings（PyMC/NUTS 階層マクロ・ベータ）"),
    ]
    for model, strategy, extra in TUNE_MATRIX:
        steps.append(Step(
            f"tune:{model}",
            (python, "hyperparameter_search.py", "--model", model,
             "--strategy", strategy, *extra,
             "--objective", "rank_ic", "--persist", "--persist-scores", "--seed", "0"),
            why=f"{model} の best params 探索と mu-hat の永続化（--persist-scores）"))
    # 予算は**名前で引く**（Step の定義に直書きしない）。ステップを足して `BUDGET_MIN` へ
    # 入れ忘れると `budget_min=None` のまま残り、`window_problem` が CI で落とす（#530）。
    # 直書きだと「予算を付け忘れた」ことを機械的に検出できない。
    return tuple(replace(s, budget_min=BUDGET_MIN.get(s.name)) for s in steps)


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
