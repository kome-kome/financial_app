# 重い計算は GitHub Actions が回し Render は読むだけ（heavy は自動実行の登録を必須にする）

## Status

accepted（2026-08-09）。Issue #423 子6 の設計決定。[ADR-0010](0010-hyperparameter-tuning-github-actions-automation.md)（探索の GHA 一本化）と同じ向きの決定を、**heavy なプラグイン全体の契約**へ広げる。

**2026-08-21 改訂（Issue #504）**: [ADR-0038](0038-local-postgres-is-the-primary.md) で正本がローカル PostgreSQL へ移り、**Decision 1 の「実行主体は GitHub Actions」が反転した**。契約そのもの（heavy は自動実行を登録する・CI で強制する・exempt は理由付き）は変えず、**値の語彙にローカル駆動を足し**、検査を2つ強めた。下の「2026-08-21 の改訂」節を参照。

## Context

`AnalysisPlugin.heavy`（`plugins/base.py`）は「Render 軽量モードでブロックしてローカル実行を促す」フラグでしかなく、**では誰がいつ回すのか**は決めていなかった。その結果「heavy を足したが自動実行が無い」が繰り返し起きた。

| 発覚 | 何が起きていたか |
|---|---|
| #432 | `sector_ols` を回す自動経路が**1本も無く**、`regression_results.computed_at` が 33〜36 日前だった。`gap_ratio` はバランス型プリセットの既定重みに入っており、ユーザーが毎朝必ず踏むパス |
| #443 | M-6 `macro_enet` は `sell_ranking` の**既定** `mu_source`（ADR-0022）なのに `tune-hyperparameters.yml` の matrix に無く、`--persist-scores` の副作用でも更新されない＝ローカル手動実行が唯一の更新経路だった |
| #423 子5 | `recommend_factor_premia`（producer）は GHA 実行履歴ゼロで、プリセット重みが 2026-07-05 のローカル実行（有効期間 37）のまま固着していた |

3件に共通するのは、**「壊れた」のではなく「動かなかった」**という点である。失敗していないので `notify-failure.yml`（#414）は発火せず、画面にはそれらしい数字が出続ける。人間が as-of を疑って初めて気づく＝#438 と同型の静かな劣化で、**検知の仕組みが構造的に存在しない**カテゴリだった。

方針そのものは既に `docs/VISION.md` に書かれている（「ローカル PC を 24 時間つけっぱなしにしないため定期実行は GitHub Actions に置く／Render は結果を見る口でスリープする／重い分析は GitHub Actions かローカル実行が担う」）。足りないのは**実装がそれを守っているかを機械的に確かめる手段**である。

## Decision

1. **重い計算（`heavy=True`）は Render で実行しない。**（**主体は #504 でローカルのタスクスケジューラへ反転**——下の改訂節を参照）当初の実行主体は GitHub Actions、フォールバックはローカル CLI（`nightly_scores.py` / `hyperparameter_search.py` / `macro_beta_inference.py` と同じ様式で、本番 Supabase へ直接永続化する）。Render 側は永続化済みテーブルを読むだけ（`GET /api/morning`・#423 子3）。
2. **`heavy=True` のプラグインは `nightly_scores.HEAVY_AUTOMATION` へ必ず登録する。** 値は次の2種類のいずれか（**#504 で `local:<スクリプト>` を追加して3種類**）。
   - ワークフローファイル名（例 `nightly-scores.yml`）＝そのワークフローが実際にこのモデルを回す
   - `exempt: <理由>` ＝自動実行しないと決めた場合。**理由を値に書く**
3. **CI（pytest）で強制する。** `tests/test_nightly_scores.py::TestHeavyAutomationRegistry` が
   - 未登録の heavy があれば落ちる（新しい heavy を足して登録を忘れると赤くなる）
   - 登録が古い（heavy でないのに残っている）と落ちる
   - ワークフロー名を書いた場合、**そのファイルが実在し、かつ中身にモデル名が現れる**ことまで検査する（存在しないファイル名や、実際には別モデルしか回さないワークフローを書いても通らない）
   - `NIGHTLY_MODELS` とレジストリの整合を両方向で見る
   - `exempt:` の理由が空・極端に短い場合は落ちる
4. **exempt は正当な選択肢**である。全 heavy を無条件で夜間へ載せる契約にはしない（コストが便益を上回るものがある）。ただし判断はコードに残す。
5. 2026-08-09 時点の登録内容:

| プラグイン | 自動実行 | 備考 |
|---|---|---|
| `sector_ols` | `nightly-scores.yml` | `regression_results`（`gap_ratio`） |
| `macro_enet`（M-6） | `nightly-scores.yml` | 既定 `mu_source` の μ̂ |
| `macro_risk_return`（M-1） | `tune-hyperparameters.yml` | `--persist-scores` の副作用。300分 timeout で cancelled が続く（#423 子7） |
| `macro_gbdt`（M-2） | `tune-hyperparameters.yml` | 同上 |
| `macro_dlm`（M-3） | `tune-hyperparameters.yml` | 同上 |
| `macro_ensemble`（M-4） | exempt | 基底 M-1/M-2/M-6 を全部回してコストが合算になるのに M-6 単体を上回らない（+0.0006・p=0.810・ADR-0022） |
| `macro_gbdt_rank`（M-5） | exempt | `produced_output=False`＝順位スコアでリターン単位ではなく、永続化する μ̂ が無い（#362） |

## 2026-08-21 の改訂（#504・正本がローカルへ移った後）

### 何が変わったか

`heavy` を回す主体が **GitHub Actions → ローカルの Windows タスクスケジューラ**へ移った
（#503・ADR-0038）。GHA はクラウドで走るので、ローカルの正本 DB へは書けない。

このとき **レジストリの語彙が yml しか持っていなかったことが害になった**。#503 で月次3本の
cron を止めた時点で、`macro_risk_return` / `macro_gbdt` / `macro_dlm` の登録は
`tune-hyperparameters.yml` を指したまま残り、**その yml の schedule はコメントアウト済み**
だった。つまり CI は緑のまま「登録はあるが動かない」を素通しした——本 ADR が防ごうとした
まさにその状態を、自分たちの都合で作った。

### 改訂内容

1. **値の語彙に `local:<スクリプト>` を追加**する（3種類目）。

   | 値 | 意味 | CI が確かめること |
   |---|---|---|
   | `local:scripts/run_monthly.py` | ローカルバッチが回す | ファイルが実在し、そのモジュールの `heavy_models()` にモデル名が現れる |
   | `nightly-scores.yml` | GHA ワークフローが回す | ファイルが実在し、中身にモデル名が現れ、**かつ schedule が生きている** |
   | `exempt: <理由>` | 自動実行しないと決めた | 理由が書かれている |

2. **yml エントリは schedule が生きていることまで見る**（新規）。止めた cron を指したままの
   登録は、今後は CI で落ちる。ローカルへ移したなら `local:` へ書き換えるしかない。

3. **`heavy_models()` を各バッチの契約にする**。列挙を書き写した定数ではなく、実体から導く:
   - `scripts/run_nightly.py` → `nightly_scores.NIGHTLY_MODELS`（引数なしで呼ぶので既定がそのまま対象）
   - `scripts/run_monthly.py` → ステップの argv から `--model` を抜く（`batch_common.models_from_steps`）

   ステップを入れ替えれば照合先が自動的に追随する＝`XBRL_MAP` を列 info から逆引き生成するのと同じ形。

4. **`local:` には登録スクリプトの実在も要求する**（`scripts/install_<name>_task.ps1`）。
   タスクスケジューラ登録は CI から見えないので、せめて手順が再現可能な形で存在することを縛る。
   PC を入れ替えた時点で黙って消えるのを防ぐ。

### 2026-08-21 時点の登録内容

| プラグイン | 自動実行 | 備考 |
|---|---|---|
| `sector_ols` | `local:scripts/run_nightly.py` | 毎日 JST 17:20 |
| `macro_enet`（M-6） | `local:scripts/run_nightly.py` | 同上。既定 `mu_source` の μ̂ |
| `macro_risk_return`（M-1） | `local:scripts/run_monthly.py` | 毎月1日 JST 01:00。`--persist-scores` の副作用 |
| `macro_gbdt`（M-2） | `local:scripts/run_monthly.py` | 同上 |
| `macro_dlm`（M-3） | `local:scripts/run_monthly.py` | 同上 |
| `macro_ensemble`（M-4） | exempt | 変更なし |
| `macro_gbdt_rank`（M-5） | exempt | 変更なし |

### 残る限界（ローカルでは1段深くなった）

「登録があること ≠ 動いていること」は健在で、**ローカルではその距離が1段伸びた**。GHA なら
「yml に schedule がある」＝有効だったが、ローカルは `local:` の登録に加えて

- タスクスケジューラに登録されているか（`Get-ScheduledTask`）
- PC が起動していたか（`StartWhenAvailable` が追いつく前提）
- `ExecutionTimeLimit` で打ち切られていないか（**打ち切りは失敗として現れない**）

の3つが CI の外にある。特に3つ目は、Issue も起票されず `monthly_last_success` が進まない
ことでしか分からない。月次は1か月に1度しか機会が無いので、初回は必ずログを見ること。

## Consequences

- **良い点**: heavy を足した瞬間に「自動実行をどうするか」を決めさせられる。決定が exempt でも、理由がコードに残り後から追える。ワークフロー名の実在とモデル名の出現まで見るため、レジストリが飾りにならない。
- **限界（重要）**: **登録があること ≠ 実際に動いていること。** `macro_risk_return` は `tune-hyperparameters.yml` に登録されているが 300分 timeout で cancelled が続いており（#423 子7）、レジストリ上は automation ありでも鮮度は出ていない。本 ADR の検査は**経路の有無**だけを見る静的検査であり、鮮度そのものの監視は `/api/morning` の as-of ブロック（#416/#417）・`macro-health.yml`（#420）・`notify-failure.yml`（#414）が担当する。3層は代替関係ではなく補完関係にある。
- CI は本番 DB にも Secrets にも触らない（レジストリ定数と `.github/workflows/*.yml` の静的検査のみ）ので、`ci.yml` の「Secrets・外部ネットワーク・本番 DB に一切触れない」制約を崩さない。
- `tune-hyperparameters.yml` は本来「探索」のワークフローであり、そこに μ̂ 更新（`--persist-scores`）が相乗りしている現状は cadence が探索に縛られる構造的な歪みとして残る（#423 子2 の宿題）。本 ADR はその歪みを**可視化する**が解消はしない。

## Considered Options

- **全 heavy を無条件で `nightly-scores.yml` へ載せる**: 契約としては最も単純だが、M-4 のように基底を全部回してコストが合算になるのに便益がゼロ（M-6 単体を上回らない）のものまで毎晩走る。Supabase Egress と GHA 実行時間を無駄に食うため却下。
- **ドキュメントに原則を書くだけ**: VISION.md には既に書かれていた。それでも #432 / #443 / #423 子5 の3件が起きた＝**散文の規約は守られない**ことが実測されているため却下。
- **`heavy` を廃止し `automation` 属性へ置換する**: プラグイン基底クラスへ運用情報を持たせると、`heavy` の本来の役割（Render でのブロック判定）と混ざる。レジストリを ops 側（`nightly_scores.py`）に置き、プラグイン定義は計算の関心だけを持つ現状の分離を維持した。
