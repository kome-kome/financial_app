# Render デプロイ運用ガイド

本プロジェクトは **Render** にデプロイ済みで稼働している。今後の改修・新機能は
Render の制約と運用形態に合わせて設計すること。

最終更新: 2026-06-24

---

## データ収集の仕組み（自動 vs 手動）

### 自動実行 vs 手動実行の整理

| 種別 | 処理内容 | 実行タイミング | 実行場所 |
|---|---|---|---|
| **自動（毎日）** | 差分収集（新規書類 + 株価更新） | **UTC 08:17（JST 17:17）毎日**（#476） | GitHub Actions `daily-incremental.yml` |
| **自動（毎日・チェーン）** | 夜間スコア更新（`sector_ols` → `regression_results`、M-6 → `macro_enet_scores`） | `daily-incremental` が **success** で終わった直後（`workflow_run`） | GitHub Actions `nightly-scores.yml` |
| **自動（毎月）** | M-1/M-2/M-3 ハイパーパラメータ探索・永続化 | UTC 16:30（JST 翌01:30）毎月1日（#476） | GitHub Actions `tune-hyperparameters.yml` |
| **自動（毎月）** | M-1 per-stock 階層マクロβ推論・永続化（producer） | UTC 00:00（JST 09:00）毎月1日（#476） | GitHub Actions `macro-beta-inference.yml` |
| **自動（毎月）** | recommend Fama-MacBeth ファクタープレミアム推定・永続化（producer） | UTC 22:00（JST 翌07:00）毎月5日（#476） | GitHub Actions `recommend-factor-premia.yml` |
| **自動（毎週）** | `stock_price_daily` の VACUUM FULL（index bloat 対策） | UTC 23:30・土（JST 08:30・日）※#446 で 22:00 から後ろ倒し＝夜間チェーン終端（実測 最遅 22:37Z）との重なり解消 | GitHub Actions `vacuum-maintenance.yml` |
| **手動のみ** | 全件収集（全社 × 5年分） | workflow_dispatch で起動 | GitHub Actions `full-pipeline.yml` |
| **手動のみ** | マクロのみ収集（為替・金利等） | workflow_dispatch で起動 | GitHub Actions `collect-macro.yml` |
| **手動のみ** | 会社予想開示収集（J-Quants /fins/summary） | workflow_dispatch で起動 | GitHub Actions `collect-disclosures.yml` |
| **手動のみ** | 半期(H1)財務収集（EDINET 半期/旧四半期Q2） | workflow_dispatch で起動 | GitHub Actions `collect-interim.yml` |
| **手動のみ（アーカイブ）** | bs_inventory 補完 | workflow_dispatch で起動 | GitHub Actions `old/` 配下（一回性・完了済み） |
| **UIから手動** | 差分収集・株価更新 | ユーザーがボタン押下 | Render Web UI |
| **自動（CI）** | `pytest` 回帰テスト（Secrets・本番DB非依存） | PR / main への push | GitHub Actions `ci.yml` |
| **自動（イベント）** | 他ワークフローの failure / cancelled を Issue 化 | 対象ワークフロー完了時（`workflow_run`） | GitHub Actions `notify-failure.yml` |

### GitHub Actions workflow 早見表（いつ・何を・どれを使うか）

#### 定期実行の占有表（時刻を動かす前に必ずここを見る）

> **2026-08-20（#503・[ADR-0038](adr/0038-local-postgres-is-the-primary.md)）に駆動主体が変わった。**
> 正本がローカル PostgreSQL へ移り、**GHA はクラウドで走るのでローカル DB へ書けない**ため、
> 収集とスコア更新の cron を停止してローカルのタスクスケジューラへ移した。
> 停止した yml には理由・復旧条件・代替経路を書いてある（`tests/test_workflow_schedule_pauses.py` が CI で強制）。

**ローカル（Windows タスクスケジューラ・JST 表記）**

| JST | タスク | 頻度 | 中身 | 備考 |
|---|---|---|---|---|
| **17:20** | `financial_app-nightly`（`run_nightly.ps1` → `scripts/run_nightly.py`） | 毎日 | `_pipeline_incremental.py`（XBRL 差分＋マクロ＋市場データ）→ `nightly_scores.py` | 大引 15:30・J-Quants 四本値 16:30・EDINET 受付終了 17:15 の後（#476 の確定時刻表）。`StartWhenAvailable` で停止していた日は次回起動時に追いつく。上限6時間（最悪 23:20 終了） |
| **1日 01:00** | `financial_app-monthly`（`run_monthly.ps1` → `scripts/run_monthly.py`） | 毎月 | `recommend_factor_premia.py` → `macro_beta_inference.py` → `hyperparameter_search.py` ×3（M-1/M-3/M-2） | 日次の最悪ケース（23:20）の後で、翌日の日次 17:20 までの**16時間の窓**。上限もその幅（`PT16H`）。GHA 時代の3本（tune / macro-beta / factor-premia）の移設先（#504） |
| 任意（週次を想定） | `scripts/backup_push.py` | 週次 | 17表を `--compress=9` でダンプ → Storage へ | 夜間バッチと**別タスク**にする（遅延が道連れにならない）。実測 37.5MB/世代 |

- **Egress はローカル駆動では発生しない**（ローカル読取は Supabase を1バイトも使わない）。
- ステップ間で止めない設計なので、収集が落ちてもスコア更新は走る。両方の結果が `.logs/<batch>_YYYYMMDD.log` に残る。
- 「走らなかった」ことは `app_settings` の `nightly_last_run` / `nightly_last_success`（月次は `monthly_*`）と、`/api/morning` の as-of ブロック（#416/#417）で見る。
- **月次の並びは「依存順 ∧ 軽い順」**＝`macro_beta_loadings` は M-1 の入力なので推論が tune より先、かつ打ち切られても前方が揃うよう軽い順（factor_premia 実測 2.6分 → macro_beta → tune）。
- **上限で打ち切られても「失敗」としては現れない**（タスクスケジューラがプロセスを止めるだけで Issue も起票されない）。検知できるのは `monthly_last_success` が進まないことだけ。
- **月次タスクの登録は XML 直渡し**（`scripts/install_monthly_task.ps1`）。PowerShell の `New-ScheduledTaskTrigger` に `-Monthly` は無く、CIM の `MSFT_TaskMonthlyTrigger` を組んでも `schtasks` の産物を渡し直しても `Register`/`Set-ScheduledTask` が "The parameter is incorrect" で弾く（2026-08-21 に実測）。**しかも非終了エラーなので `$ErrorActionPreference=Stop` でも止まらず「登録しました」と嘘が出る**ため、登録後に `Export-ScheduledTask` で日・上限・`StartWhenAvailable` を読み直して検証している。

**GitHub Actions（残っているもの・すべて UTC）**

| UTC | ワークフロー | 頻度 | 占有（上限まで） | JST | 1回あたり Egress |
|---|---|---|---|---|---|
| **21:00** | **`egress-health`** | **毎日** | → 21:10（10分） | **翌06:00** | ほぼ0（`app_settings` 2行＋`pg_database_size` 1行） |
| 23:30 | `vacuum-maintenance` | 毎週土 | → 24:00 | 日 08:30 | ほぼ0（サーバ内処理） |
| （push/PR） | `ci.yml` | 随時 | 〜15分 | — | 0（DB へ接続しない） |
| （失敗時） | `notify-failure` | 随時 | 〜10分 | — | 0 |

**GitHub Actions（#503 で停止したもの）**

| 旧 UTC | ワークフロー | 停止理由 | 代替 |
|---|---|---|---|
| 08:17 | `daily-incremental` → `nightly-scores` / `macro-health` | 正本がローカルへ移り、動かすと Supabase だけが前進して分岐する | ローカル `run_nightly.ps1`（JST 17:20） |
| 00:00 | `macro-beta-inference` | 同上（`macro_beta_loadings` が分岐する） | ローカル月次の `macro_beta` ステップ（#504・毎月1日 JST 01:00） |
| 16:30 | `tune-hyperparameters` | 同上。M-1/M-2/M-3 の**唯一の自動更新経路**だったので、止めた時点で μ̂ の鮮度も止まる | ローカル月次の `tune:<model>` ステップ（#504・matrix と同じ探索戦略・同じ `--n-iter`） |
| 22:00 | `recommend-factor-premia` | 同上。#423 子5 で「実行履歴ゼロのまま 37 期の重みで固着」を直した cron なので、**止めれば同じ固着へ戻る** | ローカル月次の `factor_premia` ステップ（#504・先頭に置いて打ち切りに強くしてある） |

- `nightly-scores` と `macro-health` は `daily-incremental` の `workflow_run` チェーンなので、親を止めれば連動して止まる（yml 側の schedule は元から無い）。
- `full-pipeline` / `backfill-*` / `collect-interim` / `collect-disclosures` / `collect-macro` は `workflow_dispatch` 専用。放置で害はないが、**手動起動すると Supabase へ書く**＝正本と分岐するので注意。
- `nightly_scores.HEAVY_AUTOMATION` は #504 で語彙に `local:<スクリプト>` を足し、全エントリがローカルバッチを指すようになった。**yml を指すエントリは schedule が生きていることまで CI が確かめる**（`tests/test_nightly_scores.py`）＝「登録はあるが cron は止まっている」という嘘を構造的に作れなくした。ただし `local:` には**タスクスケジューラ登録**という CI から見えない一段が残る（ADR-0031 の「登録があること ≠ 動いていること」は健在）。

#### Storage バックアップの初期設定（#503 Phase 3・初回だけ）

`scripts/backup_push.py --dest storage` を使う前に、Supabase 側で2つ用意する。

1. **private バケットを作る**（ダッシュボード > Storage > New bucket）
   - 名前: `db-backups`
   - Public: **オフ**（バックアップを公開しない）
2. **`.env` へ2つ追記する**（ダッシュボード > Project Settings > API）
   ```
   SUPABASE_URL=https://ndebkuazchtzkxiutiqn.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=<service_role の値>
   ```
   **anon key では private バケットへ書けない**（403 になる）。`backup_push.py` はこの2つを
   取り違えたときに、そのことを名指しで言う。

未設定のままでも `--dest local`（既定）なら動く——ダンプ生成・マニフェスト・保持ポリシーの
判定まではローカルだけで検証できる（ミラー3本を Supabase 抜きで実証したのと同じ考え方・ADR-0035）。

| 制約 | 値 | 実測との関係 |
|---|---|---|
| ファイルサイズ | **50MB**（Free） | 最大表 `stock_price_weekly` が 17.4MB。表ごとに分けているので余裕がある |
| Storage 容量 | **1GB**（Free） | 1世代 37.5MB。保持ポリシー（直近4＋月次6＝10世代）で約 375MB |
| Egress | 5GB/月（DB と共用） | **upload は Egress に載らない**。download は復元時だけ |


#### バックアップからの復元（#503 Phase 3）

Supabase Storage に置いた世代から戻す。**復元先はローカル限定**（`guard_dest_local`）。

```powershell
python -m scripts.backup_push --list --dest storage        # 世代を確認
python -m scripts.backup_restore                           # 最新世代の内容（ドライラン）
python -m scripts.backup_restore --apply --create-schema    # 既定のローカル DB へ
```

- 復元は **FK 依存順に1表ずつ**（`pg_restore --disable-triggers` は非 superuser では使えない）。
- 復元後にマニフェストの行数と突合し、食い違えば exit 1。
- **四半期に1度、空の DB へ実際に流す**。`edinet` は `createdb` 権限を持たないので、予行には
  `initdb` の使い捨てクラスタ（port 5433）を使う——2026-08-20 の実証もこの経路で、17表すべて行数一致を確認した。

#### アクティブ（`.github/workflows/` 直下・Actions 対象）

| カテゴリ | workflow 名 | ファイル | 使うタイミング | 所要時間の目安 |
|---|---|---|---|---|
| `[CI]` | pytest 自動テスト | `ci.yml` | PR・main push で自動実行（手動起動不要） | 〜1分 |
| `[定常]` | 差分収集・毎日自動実行 | `daily-incremental.yml` | **毎日 UTC 08:17（JST 17:17）** に自動（#476 で JST 03:00 から前倒し＝大引け 15:30 と EDINET 受付終了 17:15 の直後。根拠は下記「daily-incremental の動作詳細」）。手動で即時更新したい場合は `workflow_dispatch` | **2h05m〜2h38m**（2026-08-02 実測）。#474 以降、週末・祝日明けは gap-fill をほぼ飛ばすため大幅に短い |
| `[全件]` | XBRL収集・財務データ全件更新 | `full-pipeline.yml` | DB初期構築時・全社バックフィル必要時（`daily-incremental` を `.disabled` に退避して同時実行回避） | 200〜240分 |
| `[補完]` | マクロのみ収集 | `collect-macro.yml` | `MACRO_SERIES`（為替・金利・指数・コモディティ・ボラ）を Yahoo から収集。新規系列追加や macro_data の鮮度補完。`workflow_dispatch`（years 既定5）。**新系列のバックフィルは years=6 で起動**（yoy は1年+30日で足りるが、将来 zscore 版追加時に再バックフィル不要な余裕幅。#358 コモディティ8系列追加時の運用）。入力 `series` に series_code（カンマ区切り）を渡すと**その系列だけ**を収集する（#444・定義是正後の再収集で GDELT 累積クエリ制限を消費しないため） | 〜数分 |
| `[推論]` | M-1 per-stock 階層マクロβ推論（producer） | `macro-beta-inference.yml` | ADR-0002 の PyMC 階層ベイズ推論バッチ（`macro_beta_inference.py`）→ `macro_beta_loadings`/`macro_beta_meta` へ永続化（M-1 `macro_risk_return` が consumer）。本番 `requirements.txt` ではなく `requirements-inference.txt`（+PyMC）を使用。**毎月1日 UTC 00:00（JST 09:00）自動**（Issue #341・鮮度が人力任せで滞留した反省。#476 で 11:00 から移動＝`daily-incremental` の 08:17 前に 340分を収める）。手動即時実行は `workflow_dispatch`（draws/tune/target_accept/chains/r_hat_threshold/force 指定可・既定 800/800/0.95/2/1.05/false）。**収束ゲートは `--r-hat-threshold` で可変化**（Issue #341）＝ADR-0002 strict 基準は 1.01 だが、chains=2 では r_hat が構造的に ~1.02 で頭打ち（実 persist 済み 2026-07-04 run も r_hat_max=1.02・n_divergences=0）のため cron 既定を 1.05 とし、構造的 ~1.02 は自動 persist しつつ真の未収束（r_hat が 1.05 を大きく超過）は persist せず失敗させる。閾値を緩めても足りない例外運用時のみ `force=true` | 本番規模で最大 340分（`timeout-minutes: 340`・numpyro で実測 10.5〜11.2秒/draw。ローカル検証: 4銘柄合成データ・draws/tune=50・chains=2・g++無しの Python フォールバックで約8分） |
| `[定常]` | 夜間スコア更新（`sector_ols` + M-6） | `nightly-scores.yml` | `nightly_scores.py`（Issue #432/#443・親 #423）を実行し、①`sector_ols` → `regression_results`（`predicted_market_cap` / `gap_ratio`）②`macro_enet`（M-6）→ `macro_enet_scores`（μ̂・`sell_ranking` の**既定** mu_source）を更新する。**起動は `daily-incremental` の `workflow_run` チェーンで `conclusion == 'success'` のときだけ**（株価が前進していない日にスコアだけ更新すると、古い株価由来の値が「今日のランキング」として出るため）。`sector_ols` は `regularization=ridge` 固定（既定 features 10項目は VIF>10 が頻発）、M-6 は params_schema の既定のまま（ADR-0021/0022 の実測と同一構成）。1モデルの失敗は他を巻き込まず、実行後に `max(computed_at)` / `max(created_at)` を直接クエリして永続化を確認する（例外なし＝コミット済みとしない）。モデル間の `load_data`（週次127万行）は `shared_snapshot_cache()` で共有し、Egress がモデル数に比例しないようにしている。手動即時実行は `workflow_dispatch` | **総所要 33.5分**（2026-08-04 本番実走・[run 30954182465](https://github.com/kome-kome/financial_app/actions/runs/30954182465)＝`sector_ols` 30.3分 + M-6 3.2分・job wall 34.5分）／**32.6分**（08-05・[run 31050406971](https://github.com/kome-kome/financial_app/actions/runs/31050406971)＝29.4分 + 3.2分）。`timeout-minutes` は実測 job wall の 2.0倍で **70分**（#446 で 150 から）。重いのは `sector_ols` 側で M-6 は 3.2分。起票時の 16.1分（2026-08-03・run 30808053564・30業種/2,837社）は #434 の構造的NULL対応前の値＝**銘柄数・業種数とともに伸びるので実走ログで追う** |
| `[定常]` | M-1/M-2/M-3 ハイパーパラメータ月次自動探索 | `tune-hyperparameters.yml` | `hyperparameter_search.py`（Issue #264/#278/#291）を matrix strategy で3モデル並列実行し `plugin_tuned_params` へ永続化（Issue #292）。`macro_risk_return`/`macro_dlm` は `--strategy grid`、`macro_gbdt` は `--strategy random --n-iter 150`（6時間上限に収める設計判断）。共通 `--objective rank_ic --persist --persist-scores --seed 0`。品質ゲート（#291）でスコア劣化時は該当ジョブが failed 終了（意図した挙動）。**毎月1日 UTC 16:30（JST 翌01:30）自動**（#476 で 03:00 から移動＝旧設定は 355分走ると 08:55 まで伸び、`daily-incremental` の 08:17 と毎月確実に38分重なっていた）。手動即時実行は `workflow_dispatch` | macro_risk_return/macro_dlm: 10〜60分、macro_gbdt: 4〜8時間相当を n_iter=150 で圧縮（timeout-minutes: 355） |
| `[補完]` | 半期(H1)財務収集 | `collect-interim.yml` | EDINET 半期報告書（043A00/docType160）と旧四半期報告書（043000/docType140）の Q2(中間=H1累計)を収集し `financial_records` に `period_type='H1'` で保存（Issue #219② フェーズB）。通期収集とは独立・常に差分（収集済み doc_id をスキップ）。`workflow_dispatch`（years_back 既定6＝既存通期窓に整合）。240分に収まらない場合は years_back を分割 | 数時間（過去6年・事前選別でQ1/Q3を除外し概ね1社1半期1DL） |
| `[推論]` | recommend Fama-MacBeth ファクタープレミアム推定（producer） | `recommend-factor-premia.yml` | `recommend_factor_premia.py --persist`（Issue #271/#342・ADR-0008）を実行し、月次断面 OLS（Fama & MacBeth 1973・Newey-West HAC）で推定したファクタープレミアムを `recommend_factor_premia` テーブルへ永続化（`plugins.recommend.resolve_weights()` が「統計的最適化」プリセットとして読む consumer）。依存は `requirements.txt` で充足（PyMC 不要）。**毎月5日 UTC 22:00（JST 翌07:00）自動**（Issue #423 子5・#476 で 12:00 から移動）＝Fama-MacBeth 自体が月末スナップショットの月次 cadence なので、増える新情報は「月末が1つ増える」ことだけ。毎月1日は `macro-beta-inference`（〜05:40Z）と `tune-hyperparameters`（16:30〜22:25Z）で埋まっているため5日へ。UTC 12:00 は `daily-incremental` の最悪ケース（08:17〜14:17Z）に飲まれるため 22:00 へ。手動即時実行は `workflow_dispatch`（`min_companies_per_period` 既定30・`maxlags` 既定11）。**schedule 起動では `github.event.inputs.*` が空になる**ため run: 側の `|| '既定値'` を外さないこと（`tests/test_recommend_factor_premia_workflow.py` が強制）。MCMC のような収束ゲートは無し（断面 OLS は決定的） | **job wall 2分54秒**（2026-08-08 手動実走・[run 31265047095](https://github.com/kome-kome/financial_app/actions/runs/31265047095)＝バッチ本体 1分55秒）。`timeout-minutes` は実測の約7倍で **20分**（起票時の 120 は未計測の当て推量でハングしても2時間気づけなかった）。Egress は `load_data` 1回分 ≈ 68MB／月 |
| `[定常]` | マクロ鮮度ゲート | `macro-health.yml` | `python -m scripts.check_macro_health`（Issue #420）が `macro_data` の系列別 `max(trade_date)` を期待更新頻度（`macro_health.FREQ_STALE_DAYS`）と突き合わせ、**既定モデルが使う系列**（`DEFAULT_MACRO_FEATURES` から逆引き）が古ければ exit 2 → `notify-failure` が Issue 起票。`collect_macro_data` は 1 系列失敗しても `continue` するため部分失敗が exit 0 で通り、#414 の失敗通知では拾えないのを塞ぐ。**収集本体（`daily-incremental` / `full-pipeline`）を落とさず独立ジョブに分離しているのが要点**——あちらを failure にすると `nightly-scores` の `workflow_run` チェーン（`success` 条件）が発火せず、マクロと無関係な `sector_ols` の夜間更新まで巻き添えで止まる（#425 の構造をワークフロー間へ適用）。収集側は同じレポートを run ログに出すだけ。誤検知が続く系列は `macro_health.EXCLUDED_SERIES` へ**理由付きで**登録する（現在: `JP_IP`＝FRED 凍結 #253／`JP_IIP`・`JP_IIP_INVENTORY`＝e-Stat が年単位更新 #451。`JP10Y` は #442 で `MACRO_SERIES` ごと削除したため除外指定も不要になった。`BCOM` は #438 の Yahoo 配信停止で一時除外していたが、収集元を連動 ETN `DJP` へ差し替えて 2026-08-06 に除外解除＝**直った系列は必ず除外から外す**（残すと代替ソース側の停止を検知できなくなる）） | 〜2分（GROUP BY 集約1本・`timeout-minutes: 10`） |
| `[定常]` | Supabase 枠消費ゲート | `egress-health.yml` | `python -m scripts.check_egress_health`（Issue #478 / #483・[ADR-0037](adr/0037-egress-cycle-budget-is-a-second-axis.md)）が **Egress のサイクル累計**（`app_settings.egress_cycle_bytes`）と **Database Size**（`pg_database_size`）を閾値と突き合わせ、超過なら exit 2 → `notify-failure` が Issue 起票。**毎日 UTC 21:00（JST 06:00）自動**。閾値は Egress 80%（`db_egress.CYCLE_WARN_RATIO`）／DB 85%（`check_egress_health.DB_WARN_RATIO`）で、**DB 側を厳しくしてある**——Egress は超えても翌サイクルで戻るが、Database Size 超過は read-only で収集そのものが止まるため。**DB の判定値は `pg_database_size` で、Usage ページの課金判定値より約 35MB 低く出る**（2026-08-19 実測: Usage 430MB / Infrastructure 409.8MB / `pg_database_size` 395MB）＝閾値 0.90 のままだと Usage 基準で 97% 相当になり手遅れなので 0.85 に置いた。**この3つの数字を混ぜないこと。****`workflow_run` チェーンにせず cron で回すのが要点**：Egress はワークフローの成否と無関係に積み上がり、開発者のローカル CLI からも積まれる（過去2回の超過はどちらもローカル検証の反復が主因）ので「収集が成功した後に見る」では見落とす経路が残る。Management API の PAT は不要（判定材料は DB の中にある＝#483 のブロッカーを迂回）。手動即時実行は `workflow_dispatch`（`warn_only` で常に exit 0） | 〜2分（`timeout-minutes: 10`） |
| `[定常]` | ワークフロー失敗の自動 Issue 起票 | `notify-failure.yml` | 上記ワークフロー（`ci.yml` を除く全本数・列挙しない設計）＋セルフテストが `failure` または `cancelled` で終わると自動起票（`workflow_run`）。手動起動しない。詳細は下記「ワークフロー失敗の通知」節 | 〜1分 |
| `[検証]` | notify-failure セルフテスト | `notify-failure-selftest.yml` | `notify-failure.yml` の変更後に発火を実証するための、意図的に失敗するだけのワークフロー（本番データ不使用）。`workflow_dispatch`（`mode=fail`／`mode=cancel`） | 〜1分（cancel は約1分） |
| `[定常]` | DBメンテナンス（VACUUM FULL・週次） | `vacuum-maintenance.yml` | `stock_price_daily` の DELETE ベース trim による index bloat 対策（Issue #290）。`_pipeline_vacuum.py` が AUTOCOMMIT 接続で **`TARGET_TABLES`（`stock_price_daily` / `stock_price_weekly`）を1表ずつ** `VACUUM FULL` し、前後の容量をログ出力。**2026-08-19 に対象を2表へ拡大し、前段で per-table の autovacuum チューニング（冪等な `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = 0.02)`）を行うようにした**——`stock_price_weekly`（195MB）に dead tuple が 200,498 行溜まり autovacuum の最終実行が 2026-07-31 で止まっていたが、これは故障ではなく **per-table 設定が無く（`reloptions = null`）クラスタ既定 0.2 が効いて発火閾値 `50 + 0.2 × 1,284,465 = 256,943` 行に一度も届いていなかった**だけ（当時 200,498 行＝その 78%）。**128万行の表に既定のスケール係数 20% が粗すぎる。** 0.02 で閾値は 25,739 行。チューニングだけでは既存の dead は物理サイズを返さず（通常 VACUUM は死領域をテーブル内で再利用するだけ）、VACUUM FULL だけでは翌週また溜まるので**両方要る**。per-table 設定を `init_db()` / `_ensure_tables()` へ入れてはいけない（lifespan が無条件実行するためローカル API 起動だけで本番へ不可逆反映される）。毎週 UTC 23:30・土（JST 08:30・日）自動（#446 で 22:00 から後ろ倒し。**cron の名目時刻ではなくキュー遅延込みの実起動時刻で設計する**——`daily-incremental` は cron UTC 18:00 に対し実起動 19:45Z 前後で、旧設定では夜間チェーン終端と日曜だけ約24分重なっていた）。**#476 で `daily-incremental` が 08:17Z へ前倒しされ、この 22:37Z 前提は解消した**（間隔が大きく開いたので時刻は据え置き＝動かす必然が無いものを動かさない）。手動即時実行は `workflow_dispatch`（ローカル・GitHub Actions 双方で Supabase pooler 経由の正常動作を確認済み・2026-07-12）。**時間帯は #427 で JST 04:00 → 07:00 へ移動**——差分収集（JST 03:00 開始・実測 2h05m〜2h38m）の最中に `VACUUM FULL`（ACCESS EXCLUSIVE ロック）が走っており、ずらす設計意図が成立していなかった。現行チェーンは 03:00 収集 → 最長 05:40 → nightly-scores（`sector_ols` 16分 + M-6・総所要は #443 の初回実走で実測）。**M-6 追加でチェーン後端が伸びるため、日曜だけは VACUUM FULL（07:00）と重なりうる**——ただし `VACUUM FULL` が排他ロックを取るのは `stock_price_daily` のみで、夜間バッチが読むのは `stock_price_weekly`／`financial_metrics` ゆえロック競合はしない（I/O は共有）。実測で 07:00 に食い込むようなら時間帯を再調整する | 数秒〜数分（対象テーブルは実測 ~50MB・42万行） |

#### アーカイブ済み（`.github/workflows/old/` 配下・一回性・Actions 対象外）

一回性バックフィル完了後に `old/` へ退避済み。再実行が必要な場合のみ `workflow_dispatch` で起動する。定期スケジュールは持たない。

> 株価履歴バックフィル / 週次株価バックフィル / C2 NULL バックフィル / CF NULL バックフィル / bs_machinery バックフィルの5本は完了済みにつき削除済み（Issue #259）。

| カテゴリ | workflow 名 | ファイル | 用途 | 所要時間の目安 |
|---|---|---|---|---|
| `[補完]` | PL/BS NULL バックフィル | `old/.github/workflows/old/refill-pl-bs.yml` | `bs_inventory` 等 旧コホート（〜2022年）が NULL の場合に再取得 | 4〜5時間 |

> **CI（`ci.yml`）**: データ収集系ワークフローとは独立した回帰検知用。`pull_request` と main への `push` で Python 3.13.7 上に `requirements.txt` + `requirements-dev.txt` を入れて `pytest` を実行する。Secrets・外部ネットワーク・本番 DB には一切触れず、`conftest.py` の in-memory SQLite / モックで完結する範囲のみを検証する。

### ワークフロー失敗の通知（`notify-failure.yml`・Issue #414）

**GitHub 標準のメール通知は当てにしない。** `daily-incremental` が 2026-07-14〜08-01 の **19日連続 failure** で止まっていたのに誰も気づかず、その間ずっと19日古い株価でランキングを見ていた実例がある。この再発防止として、失敗を「必ず目に触れる Issue」へ変換する。

| 項目 | 仕様 |
|---|---|
| トリガー | `workflow_run: types: [completed]`。**`workflows:` は列挙せず全ワークフローを対象**にし、`ci.yml` だけ job の `if` で名前一致除外する |
| 発火条件 | `conclusion == 'failure'` **または `'cancelled'`**。timeout 打ち切りは failure ではなく **cancelled** で終わるため両方必須（実例: `tune-hyperparameters` 300分・`collect-interim` 4h）。ただし cancelled には**人が Cancel を押した run** も混ざるため、job の annotation で振り分ける（下記） |
| 起票内容 | タイトル `[ops] ワークフロー失敗: <workflow name>`／ラベル `ops` `priority:high` `ci`／本文に run URL・発火イベント・ブランチ・**失敗ジョブ名**・**失敗ステップのログ末尾30行** |
| 重複防止 | 同一タイトルの open Issue があれば新規起票せず**コメント追記** |
| 権限 | `issues: write` は本ワークフローのみ。収集系の `contents: read` 最小権限は崩さない。ログ取得のため `actions: read`、cancelled の annotation 取得のため `checks: read` |

**`cancelled` の振り分け（#453）**: timeout 打ち切りと手動キャンセルは **conclusion も、ログ末尾（`The operation was canceled.`）も同一**で、run/job の JSON にも区別が無い。唯一の判別材料が **job の annotation**（`GET /repos/{repo}/check-runs/{job_id}/annotations`）で、2026-08-04 に両側を実測した。

| annotation | 実測した run | 判定 |
|---|---|---|
| `The job has exceeded the maximum execution time of 1m0s` | [30748982170](https://github.com/kome-kome/financial_app/actions/runs/30748982170)（selftest `mode=cancel`） | timeout → **起票する** |
| `The run was canceled by @kome-kome.` | [30917108939](https://github.com/kome-kome/financial_app/actions/runs/30917108939)（開始4秒後に人が押した） | 手動 → **起票しない** |
| どちらも無い／annotation 取得に失敗 | — | 不明 → **起票する**（安全側） |

**denylist（手動と確認できたものだけスキップ）から allowlist（timeout と確認できたものだけ起票）へ反転させないこと。** allowlist にすると、GitHub が annotation の文言を変えた瞬間や取得に失敗した瞬間に **timeout 通知が静かに消える**＝#414 と同型の欠落になる。判別できない cancelled は必ず起票側へ倒す。向きは `tests/test_workflow_failure_notification.py::test_manual_cancel_is_the_only_skip_path` が固定する。

> この振り分けが無かった 2026-08-04 に、人が押したキャンセル1件が `priority:high` の Issue #453 として自動起票された。**「復旧すべき障害」ではないノイズが high として積まれると、本物の失敗の優先度が相対的に薄まる**（Issue 本文自身が「open のまま放置すると次の失敗がコメントに埋もれる」と警告している通り）。

**運用ルール（重要）**:

- **復旧を確認したら Issue をクローズする。** open のまま放置すると次回以降の失敗が同じ Issue へのコメントになり、埋もれて #414 の再現になる。
- `ci.yml` は対象外（PR の pytest 失敗は PR 画面で即見えるうえ、feature ブランチの試行錯誤で Issue が乱立するため）。
- `.github/workflows/old/` 配下はサブディレクトリのため GitHub Actions が認識せず、対象外。

**この仕組みで検知できないもの（限界）**:

- **`notify-failure.yml` 自身の失敗**。`workflow_run` で起動されたワークフローは、さらに別の `workflow_run` を発火しない（GITHUB_TOKEN 由来イベントの無限ループ防止）。この経路が生きているかは下記セルフテストで確認する。
- **ワークフローがそもそも起動しなくなったケース**（cron の無効化・ファイル名 `.disabled` 化など）。失敗ではなく無実行なので発火しない。鮮度そのものの監視は別系統（買い推奨画面の as-of 表示・#416）が担う。

**ワークフローを追加したとき**: 何もしなくてよい（`workflows:` を列挙しない設計＝新規ワークフローも自動で対象）。列挙方式に戻すと、**列挙漏れによる静かな通知欠落**（#414 と同型）が復活するうえ、そもそも起動しない（下記）。`ci.yml` を改名したときだけ job の `if` の除外文字列を追随させる。`tests/test_workflow_failure_notification.py` が「列挙方式に戻っていないか」「`ci.yml` の `name:` と `if` の文字列が一致するか」「`cancelled` が条件から落ちていないか」を pytest で強制する。

> **⚠️ `workflows:` にワークフロー名を列挙してはいけない（2026-08-02 実測）**: GitHub は `workflow_run.workflows` の各要素を**フィルタパターン**として解釈するため、本リポジトリのように名前が `[定常] …` と角括弧で始まると `Encountered an issue parsing workflow trigger(s)` でワークフローごと **`startup_failure`** になる（run [30747596548](https://github.com/kome-kome/financial_app/actions/runs/30747596548)）。しかもこの状態では `types:` フィルタも効かず、`requested` を含む全イベントに反応して毎回 `startup_failure` を量産する。**「起動して失敗」ではなく「起動すらしない」ため、通知が来ないこと自体に気づけない。**

**修正時の注意**: `workflow_run` は **default branch（main）上のファイルだけ**が実行される。feature ブランチでこのファイルを直しても実火では動かないため、検証は main 反映後に行う。

**検証手段**: `notify-failure-selftest.yml`（`[検証] notify-failure セルフテスト`）を `workflow_dispatch` で起動する。本番データには一切触れず、意図的に失敗するだけのワークフロー。

```bash
gh workflow run notify-failure-selftest.yml -f mode=fail     # exit 1 → conclusion: failure
gh workflow run notify-failure-selftest.yml -f mode=cancel   # timeout 打ち切り → conclusion: cancelled（約1分）

# 手動キャンセルの再現（#453 の振り分けを検証する）: cancel モード（sleep 300）を起動し、
# timeout-minutes: 1 に達する前に自分で止める＝専用モードは不要
gh workflow run notify-failure-selftest.yml -f mode=cancel
gh run cancel <run-id>   # → annotation は "The run was canceled by @…" → 起票されないこと
```

`notify-failure.yml` を変更したら main 反映後にこれを1回流し、Issue が起票される（2回目以降はコメント追記になる）ことを確認する。確認後は起票された Issue をクローズすること。**cancelled の振り分けを触ったときは両側を流す**（手動キャンセル＝起票されない／timeout 打ち切り＝起票される）。片側だけでは「除外しすぎて timeout まで落としていないか」が実証できない。

### daily-incremental の動作詳細

毎日 **UTC 08:17（JST 17:17）** に自動起動する `_pipeline_incremental.py` は:

> **⏰ 起動時刻の根拠（#476・2026-08-09 に JST 03:00 から移動）**
>
> | JST | 確定するもの |
> |---|---|
> | 15:30 | 東証 大引け（2024-11-05 のクロージング・オークション導入で 15:00 から延伸） |
> | 16:30頃 | J-Quants 株価四本値（無料は12週遅延＝当日ぶんは来ない） |
> | **17:15** | EDINET 提出受付終了 → その日の書類一覧が確定 |
> | **17:17** | ← 起動 |
> | 18:00頃 / 24:30頃 | J-Quants 財務情報（速報 / 確報） |
> | 22:30（夏）/ 23:30（冬） | 米国市場オープン |
> | 05:00（夏）/ 06:00（冬） | 米国市場クローズ |
>
> **市場データの中身は JST 03:00 起動と完全に同じ。** 日本株はどちらも同じ大引けを見る。米国系も、JST 03:00 の時点では米国が**場中**なので直近の確定クローズは同一セッション。変わるのは ①ユーザーが結果を見られる時刻が約9時間早い ②EDINET が当日提出ぶんを拾える の2点だけ。
>
> **東証も NYSE も閉じている唯一の窓（JST 16:00〜22:30）**に置いてあるので、#474 で問題にした「進行中セッションの途中経過バー」を平時は踏まない。ただし **GHA の cron 遅延は実測で最大6時間**（18:00Z が 00:08Z 起動＝run 31133560740）なので、**スケジュールは第一防御でしかなく、保証はコード側**（`last_closed_session` / in-progress バー除外）が持つ。分を `:00` からずらしてあるのは毎時ピークの遅延を避けるため。
>
> チェーン終端の見込みは JST 21〜22時台（本体 2h05m〜2h38m ＋ `nightly-scores` 実測33〜35分）。週次 VACUUM は 土 23:30Z なので衝突しない。

1. EDINET API の書類一覧を **前年1月1日〜「確定済みの最新 JST 日付」**の全日スキャンで取得し（`years_back=1`・`collector_financials.py` Phase 3）、未収集 `doc_id` のみ XBRL 収集・DB保存。「60日以内」は旧実装の記述で実態と乖離していた（#422）。<br>**終端は `latest_settled_edinet_date`（JST 基準・#476）**: EDINET 提出受付は平日 9:00〜17:15 で、受付終了後はその日の一覧が確定するため、`EDINET_CUTOFF_JST` を過ぎていれば当日を含める。旧実装は `date.today() - 1日` ＝**ランナーの UTC 日付**から引いており、UTC 18:00 起動（JST 翌03:00）では終端が JST の前々日まで下がって、**確定済みの JST 前日ぶんを丸ごと落としていた**（提出から反映まで約33時間）。JST 17:17 起動＋JST 基準の終端で**約10時間**になる。締切間際の提出が一覧へ載るまでのラグで取りこぼしても、差分収集が翌日に同じ日付を再スキャンするので欠落にはならない
2. 株価更新（Phase 4）: **①Yahoo Finance が銘柄ごとに `その社の最終日+1 〜 today` をギャップ補完**（`fill_recent_stock_price_gap_yahoo`）→ **②J-Quants catchup（`today-90`〜`today-80`）で Yahoo 暫定値を公式値へ置換** → ③`update_market_data_from_history`。<br>**J-Quants 無料は直近84日（12週）を配信しない**ため、旧実装の `days_back=14` はエンバーゴ内で構造的に常に0件かつ全日403となり、中断ガードを誤発火させて Yahoo 補完まで巻き添えで止めていた（#419 / #425）。撤去済み。鮮度を担う Yahoo を先に置き、J-Quants catchup の失敗は握って継続する（片方の収集元の障害がもう片方を止めない）。鮮度はこの Yahoo 補完が担う（J-Quants 制約表の「配信遅延」行を参照）。<br>起点は**銘柄別**（Issue #415）。全社横断の最大日を1つ選んで全社へ適用すると、一部銘柄だけ先行して復旧した場合に遅延銘柄の欠測が永久に埋まらない（2026-07 に発生。2銘柄が 07-31 / 3,677銘柄が 07-13 の状態で 14営業日分が穴のまま残った）。起点は daily 保持窓（`DAILY_WINDOW_DAYS`）でクリップし、それ以前は `backfill-weekly.yml` の管轄とする。<br>**対象社の判定基準は「閉場済みの最新 JST 営業日」**（`last_closed_session`・#474）。当時の cron は UTC 18:00 ＝ JST 03:00 で、UTC 日付と比べる旧実装では **UTC 土日（JST 日曜/月曜の早朝）に全社が「1日遅れ」に見え**、全社が既に持つ金曜バーを取り直していた（run 31272807314 で 4,437社 / 2h11m）。本番実測で **4,437社 → 735社**。基準セッションより後の `trade_date`（Yahoo が場中に返す**進行中バー**）は取り込まない
3. 成長率・Zスコアを再計算
4. 所要時間: **2h05m〜2h38m**（2026-08-02 の success run 2本で実測。大半は EDINET 全日スキャンと Yahoo ギャップ補完の逐次リクエスト。`timeout-minutes: 360`）。**#474 以降、週末・祝日明けの run は gap-fill をほぼ丸ごと飛ばすため大きく短くなる**（平日は全社取得のままなので据え置き）

> **注（運用パターン）**: 全件収集（`full-pipeline.yml`）を回している間は、Supabase 接続上限での同時実行を避けるため、本ワークフローを一時的に `daily-incremental.yml.disabled` へリネームして停止する（例: コミット `4764d96`「全件収集中の同時実行回避」）。全件収集が終わったら `.yml` に戻して再有効化する。**現在ファイル名が `.disabled` の場合は自動の定時収集が止まっている状態**なので、UI / 手動収集で補う。<br>停止中の株価鮮度は `full-pipeline` の finalize（Phase 5）が同じ Yahoo ギャップ補完を持つため、全件収集の完了時点で追いつく（#426）。それでも collect フェーズ中（実測 約4時間）は前進しない点は変わらない。
>
> **✅ cron 再開済み（2026-06-22〜）**: dual-table 移行後の `workflow_dispatch` 手動実行で Yahoo ギャップ補完・J-Quants 株価取得が GitHub Actions（Azure IP）から正常動作することを確認 → `on.schedule` を有効化。**2026-08-09 に UTC 18:00（JST 03:00）→ UTC 08:17（JST 17:17）へ移動**（#476・上の起動時刻の根拠を参照）。
>
> **⚠️ 2026-07-14 〜 08-01 は19日連続で failure していた**（真因: Phase 4 冒頭の J-Quants 403 が例外送出しプロセスが exit 1 → 鮮度の担い手である Yahoo ギャップ補完に一度も到達しなかった）。#412 で 403 をカバレッジ境界の欠測として継続扱いに修正済み。**この障害に19日間誰も気づかなかった**のは全ワークフローに失敗通知が無いためで、#414 で対応する。成功時の所要は実測 2h05m〜2h38m。

> **鮮度の確認は max ではなく分位で行うこと。** 全社横断の `max(trade_date)` は先行復旧した数銘柄だけで新しくなり、大多数が19日古くても緑に見える（2026-08-02 の実測: max=07-31 は2銘柄のみ、3,677銘柄は 07-13）。
> ```sql
> SELECT percentile_disc(0.05) WITHIN GROUP (ORDER BY d) AS p05,
>        percentile_disc(0.50) WITHIN GROUP (ORDER BY d) AS p50, max(d)
> FROM (SELECT edinet_code, max(trade_date) d FROM stock_price_daily GROUP BY 1) t;
> ```

**重要**: GitHub Actions の Runner は Azure IP のため **stooq は完全ブロック**（403）。株価取得は J-Quants のみ使用。Claude Code リモート環境からも Yahoo Finance はブロックされる。外部サービスの制約値は本ファイル「外部サービス制約（無料プラン）」節を参照。

### CF補完の完了状態（2026-05-31 完了）

| 指標 | 状態 |
|---|---|
| 通常補完（`cf_net_change_cash IS NULL`）| ✅ **全件完了**（remaining=0） |
| capex 充足率 | **88.8%**（CF文を持つ 19,073件中 16,929件取得済み） |
| 残り 2,144件 | アセットライト企業（持株会社・IT等）で capex 行が元々無いため永続的に NULL。再実行しても変わらない |
| `refill-cf.yml` スケジュール | **cron を撤去し手動（workflow_dispatch）のみ**に確定（Issue #117・案B）。下記の実態計測により定期実行は便益が無いと判断 |

#### cron を持たない理由（Issue #117 / 本番DB 20,548行の実態計測）

| CF 区分 | 充足率 |
|---|---|
| 営業CF `cf_operating_cf` | 100.0% |
| 投資CF `cf_investing_cf` | 99.9% |
| 財務CF `cf_financing_cf` | 99.7% |
| 現金増減 `cf_net_change_cash` | 98.3% |
| 設備投資 `cf_capex` | 88.9% |

- 主要3区分（営業/投資/財務CF）は初回 XBRL 収集で ≧99.7% 充足し、「CF NULL が蓄積し続ける」懸念は実態として発生していない。
- 唯一の有意な欠損は capex（~11%）だが全年度で安定した**構造的欠損**（提出企業ごとのタグ揺れ）であり、同じ XBRL を再パースする日次 cron では改善しない。本質的改善は parse 側のラベル照合拡充（別 Issue）で扱う。
- よって定期 cron の便益はほぼ無く、J-Quants レート制限・Render スリープのコストのみ残るため cron は撤去。欠損補完が必要な場合は `workflow_dispatch`（mode=refill/capex-only/diagnose・件数指定）で随時実行する。

### bs_inventory バックフィル（`.github/workflows/old/refill-pl-bs.yml`）

`bs_inventory` の NULL はタグ漏れではなく**時系列コホート**が原因（パーサ修正前に収集した〜2022年度が backfill 未実施。2026-06-15 実測で旧年度 57〜94% null・新年度は ~3%）。`.github/workflows/old/refill-pl-bs.yml` を **workflow_dispatch（limit 省略＝全件・約4〜5時間）** で起動し、古い順に XBRL を再取得して是正する。詳細・残件の見方は GOTCHAS.md「bs_inventory バックフィルの運用」。

| 項目 | 状態 |
|---|---|
| 自動化整備 | ✅ `_pipeline_gh.py --refill-pl-bs` + `.github/workflows/old/refill-pl-bs.yml` を結線 |
| 本番バックフィル実行 | ✅ **完了**（2026-06-24 実測: 全年度 82〜87% カバレッジ。残 NULL はサービス業・金融等の構造的欠損） |
| 完了判定 | 全年度で一様な欠損率（≒13〜18%）になっており旧コホート偏りは解消済み |

### tune-hyperparameters の運用詳細（`.github/workflows/tune-hyperparameters.yml`）

M-1（`macro_risk_return`）・M-2（`macro_gbdt`）・M-3（`macro_dlm`）のハイパーパラメータ
自動探索（ADR-0007／ADR-0010）を GitHub Actions で月次実行する。GUI からの手動トリガーは
Issue #293 で廃止済みのため、探索の実行手段は本ワークフローの自動実行と
`workflow_dispatch` による手動実行のみ。

- **実行頻度**: `cron: '0 3 1 * *'`（UTC 03:00 = JST 12:00、毎月1日）。matrix で3モデルを
  並列ジョブに分割し（`fail-fast: false`）、`macro_risk_return`/`macro_dlm` は
  `--strategy grid`（`timeout-minutes: 240`）、`macro_gbdt` は
  `--strategy random --n-iter 150`（`timeout-minutes: 355`＝6時間上限ギリギリを避ける値）。
- **品質ゲートの挙動（Issue #291）**: `hyperparameter_search.py --persist` は
  `plugin_tuned_params` の既存 `objective_value` と今回の `best_score` を比較し、劣化して
  いれば永続化（`--persist-scores` 併用時の producer スコア更新も含む）をスキップして
  `SystemExit` で非ゼロ終了する。**この場合ジョブは `failed` 扱いになり GitHub 標準の
  失敗通知（Actions の通知設定に従いメール等）が飛ぶ**——これは意図した挙動であり、
  `continue-on-error` 等では握りつぶさない。1モデルの品質ゲートスキップ・実行失敗は
  `fail-fast: false` により他モデルのジョブへ波及しない。
- **失敗時の対応手順**:
  1. GitHub リポジトリの Actions タブ → `[定常] M-1/M-2/M-3 ハイパーパラメータ月次自動探索`
     の該当実行を開き、失敗した matrix ジョブ（`model` 名で識別）を確認する。
  2. 各ジョブの `Upload log` ステップが `hyperparameter_search_<model>.log` を
     artifact として30日間保持する（`actions/upload-artifact`・`if: always()` のため
     ジョブ失敗時も取得可能）。ダウンロードして探索の詳細ログ（各候補のスコア・
     品質ゲートでスキップされた場合はその理由）を確認する。
  3. 品質ゲートによる意図的なスキップ（データの一時的な劣化等）であれば、次回月次実行を
     待つか、原因（マクロデータの欠損・異常値等）を先に是正してから
     `workflow_dispatch` で手動再実行する（GitHub UI の Actions タブ → 対象ワークフロー →
     `Run workflow`、または `gh workflow run tune-hyperparameters.yml`）。
  4. 品質ゲート以外の失敗（DB接続エラー・依存関係エラー等）はログから原因を特定し、
     修正後に同様の手順で再実行する。

---

## ローカル / Render 役割分担

両環境が同一の **Supabase DB** を共有し、重さに応じて作業を分担する。

| 操作 | ローカル PC | Render（Web） |
|---|---|---|
| 全件収集（初回・全社XBRL） | ✅ 推奨 | ❌ ブロック（OOM リスク） |
| 株価履歴再構築 | ✅ 推奨 | ❌ ブロック |
| J-Quants 大量収集 | ✅ 推奨 | ❌ ブロック |
| 差分収集（`skip_existing=True`） | ✅ 可（手動） | ✅ 可（手動・UIボタン） |
| 市場データ更新 | ✅ 可 | ✅ 可 |
| スクリーニング・分析・UI 閲覧 | ✅ 可 | ✅ 可 |

**`RENDER_LIGHT_MODE=true`**（`render.yaml` に設定済み）を Render に設定することで、
重い操作を API レベルでブロックし、UI 上でもボタンを無効化する。
ローカル `.env` にはこの変数を設定しない（制限なし）。

### ローカル PostgreSQL（**正本**・Issue #481 → #503）

Supabase が restricted になると**アプリも分析も一切動かせない**（2026-07・2026-08 に2回発生し、2回目は8日間まるごと停止）。#481 は障害時の継続と検証反復の Egress ゼロ化のためにローカルへ**読取レプリカ**を置いた。

**2026-08-20（#503・[ADR-0038](adr/0038-local-postgres-is-the-primary.md)）にこれが正本へ昇格した。**2回目の停止の真因は Egress ではなく **NANO の実効メモリ 408MB に DB 409.8MB が乗らないこと**（#500）で、VACUUM FULL 後も余裕は 13〜28MB しかない——**週次株価は毎週増える**のでこの余裕は自然には戻らず、正本を置く限り同じ停止が周期的に再発する。収集も夜間バッチもローカル（Windows タスクスケジューラ）で回し、Supabase は **2026-08-07 の閲覧用断面（Render 専用）＋ Storage のバックアップ置き場**として残す。**Supabase の Postgres へ書き戻す経路は作らない**（ミラーの dest ローカル限定ガードはそのまま生きる）。

#### このマシンの実測（2026-08-15）

| 項目 | 値 |
|---|---|
| サーバ | **PostgreSQL 18.6**（Windows サービス `postgresql-x64-18`・port 5432） |
| クライアント | `C:\Program Files\PostgreSQL\18\bin`。**PATH に無い**ので `pg_dump` 等はフルパスで呼ぶ |
| 認証 | `pg_hba.conf` は local/host とも `scram-sha-256` |
| 接続文字列 | `postgresql://edinet:edinet@localhost:5432/financial_db` ＝ [database.py](../database.py) の**既定フォールバックと同一**（`DATABASE_URL` 未設定ならここへ繋がる） |
| encoding | **UTF8**（collate は `Japanese_Japan.932`＝並び順のみ OS 依存。Supabase と並び順が違う点は text の `ORDER BY` にのみ影響） |
| `edinet` の権限 | **superuser でも createdb でもない**。所有する `financial_db` 内の CREATE は可 |

**pg_dump のバージョン整合**: クライアントはサーバ以上である必要がある。PG18 クライアントで Supabase（15/17）を dump するのは正方向なので問題ない（逆は不可）。

#### セットアップ（`scripts/setup_local_db.py`）

```powershell
$env:DATABASE_URL = "postgresql://edinet:edinet@localhost:5432/financial_db"
python -m scripts.setup_local_db            # ドライラン（何も変更しない）
python -m scripts.setup_local_db --apply    # 実行
```

- **接続先ガードが最初に走る**。`database._is_local` がローカルを指していなければ即 `SystemExit`。`init_db()` は起動のたび無条件に DDL（`DROP COLUMN` 移行を含む）を打つため、本番へ誤射すると不可逆。
- **既定はドライラン**（`--persist` と同じ作法）。
- **旧スキーマの掃除は1回だけ**走る（マーカー＝「素の `stock_price_history` が在る かつ `stock_price_weekly` が無い」）。2回目以降は `init_db()` だけが走るので、**ミラー投入後に誤って実行しても中身を消さない**。

2026-08-15 の実行結果: 18テーブル ＋ VIEW 2本を生成、`financial_metrics` / `financial_metrics_interim` とも `SELECT` 可、`security_invoker=true` が**非 superuser でも適用できた**、2回目の実行は差分なし。**`sql/financial_metrics_view.sql` の `STDDEV_SAMP` / `::numeric` / 名前付き `WINDOW` 句が PG18 で通ることの実証になっている。**

同日、この空スキーマに対して **Web アプリが起動することも実証済み**（`DATABASE_URL` をローカルへ向けて `uvicorn api:app`）:

| 確認 | 結果 |
|---|---|
| `GET /health` | `200 {"status":"ok","db":"ok"}` |
| `GET /api/stats` | `200`（全件0・`freshness: "empty"`＝データが無いだけで経路は生きている） |
| `GET /` | `200`（ダッシュボード HTML 17,873 bytes） |

`APP_SECRET_KEY` 未設定の警告が出るが、これは開発用既定鍵で継続する正常な挙動（本番相当環境＝`RENDER`/`RENDER_LIGHT_MODE` でのみ起動を停止する）。**つまり Supabase が restricted でもローカルだけでアプリは動く。あとはデータを入れるだけ**（8/18 以降の mirror pull）。

#### `legacy_stock_price_history_2026_02`（温存した旧日次 OHLCV）

このマシンには Supabase 移行前（2026-02〜05 で凍結）の開発 DB が残っていた。うち旧 `stock_price_history` は **2024-05-17〜2026-02-20・3,960社・1,636,505行の日次 OHLCV** で、**現行 Supabase にはもう存在しない**——`stock_price_daily` は `DAILY_WINDOW_DAYS=183` でローリング削除され、`stock_price_weekly` は `close_last` と集約しか持たず O/H/L を残さない。Yahoo から取り直すと 3,960社ぶんで数十時間かかるため、**DROP せず改名して温存**した（478MB）。

`stock_price_daily` の窓は今日時点で 2026-02-13 以降なので **7日重なって連続する**が、**この旧データに分割の遡及調整が入っているかは未確認**（#465 で週次に段差が見つかっている）。調整済みの現行値と混ぜる前に接合検証が要る。ミラー範囲には含めない。

同時に DROP した旧4テーブル（`companies` / `financial_records` / `macro_data` / `collection_logs`）は `migration_dumps/legacy_pre_mirror_20260815.dump` へ退避済み（ローカル間なので Egress ゼロ・`.gitignore` 配下）。

#### 接続先の切り替え（`FINAPP_DB_TARGET`・Issue #481 B-1）

**既定は `prod`。環境変数を触らなければ従来どおり Supabase を見る。**

| 変数 | 値 | 解決される接続先 |
|---|---|---|
| `FINAPP_DB_TARGET` | 未設定 / `prod` | `DATABASE_URL`（無ければローカル既定へフォールバック＋警告） |
| `FINAPP_DB_TARGET` | `local` | `DATABASE_URL_LOCAL`（無ければ `postgresql://edinet:edinet@localhost:5432/financial_db`） |

**`DATABASE_URL_LOCAL` の既定はこのマシンの実環境と一致している**ので、`.env` を編集せず `FINAPP_DB_TARGET=local` だけで切り替わる。

```powershell
$env:FINAPP_DB_TARGET = "local"; python -m uvicorn api:app     # CLI
.
un_local.ps1                                                # GUI（ランチャーをローカル始まりで起動）
.
un_local.ps1 -Console -Port 8010                            # ランチャー無し・コンソール起動
```

GUI（`launch.py`）は窓に**接続先ラジオ**を持ち、切り替えるとサーバーを入れ替える（ブラウザは開き直さない）。**選択は永続化しない**——毎回 `prod` から始めるほうが、古いミラーで起動していることに気づかず使い続ける事故を防げる。環境変数で明示した場合のみそれが初期値になる。既に別プロセスが起動済みのときはそのプロセスを掌握していないためラジオは無効化される。

**`run_local.ps1`（Supabase 障害中の常用導線）**: ランチャーは毎回 `prod` 始まりなので、restricted 中に素で起動すると**接続先ラジオを切り替える前に Supabase を叩いて固まる**。`run_local.ps1` は `FINAPP_DB_TARGET=local` を先に立ててからランチャーを起動するので、一度も本番へ触らせない。起動前に `companies` の件数と週次株価の最新週を出して**どの世代のミラーを見ているか**を明示し、ローカルへ繋がらなければランチャーを起こす前に落とす。

併せて `FINAPP_EGRESS_ENFORCE=0` / `FINAPP_EGRESS_LEDGER=0` を立てる——ローカル読取は Egress を1バイトも使わないので、400MB のプロセス予算で GUI が `EgressBudgetExceeded` に落ちる意味が無く、`.egress/ledger.jsonl` に混ぜると `scripts.egress_report` の集計が Supabase の実測でなくなる（請求サイクル累計のほうは `_is_local` で自動的に無効）。**`.env` は書き換えない**ので、復旧後は素の `python launch.py` に戻すだけで prod へ復帰する（戻し忘れが起きない）。

**ガードは強さを2種に分けてある**（[database.py](../database.py) の `resolve_database_url()`）:

- **`local` 指定なのに解決先がリモート → `RuntimeError` で import 時に落とす。** ミラーのつもりで本番へ書く事故を確実に止める。`local` を明示した人しか踏まないので CI に影響しない
- **`prod` 指定で `DATABASE_URL` 未設定（＝ローカルへフォールバック）→ 警告どまり。** ここを raise にすると `ci.yml`（`DATABASE_URL` を渡さずに走る）が全滅する
- `FINAPP_DB_TARGET` の**未知の値は `ValueError`**。`localhost` のような打ち間違いを黙って `prod` に落とすと、本人はローカルのつもりで本番を叩き続ける

**接続先は常時見えるようにしてある。** ミラーは pull/sync した時点で止まるため、どちらの DB を見ているか分からないと**古いスコアを最新と誤読する**（#438 の「静かな配信停止」と同型で、`/api/stats` の `freshness` は「データの古さ」は見るが「どちらの DB か」は見ない）。

- `/api/system/info` が `db_target` / `db_is_local` / `db_label` を返す（**接続文字列そのものは返さない**・表示用ラベルのみ）
- [static/js/common.js](../static/js/common.js) が全9画面でこれを読み、**ローカル接続時だけ**右下にバッジを出す。本番接続時は何も描画しないので普段の見た目は不変
- ランチャー窓は稼働中ラベルを「● 稼働中（ローカルDB）」に変え、色も橙にする

**ローカル接続中の書き込みは許している**（収集・分析結果の永続化）。Supabase 障害中でも作業を止めないため。ただしミラーは Supabase と乖離しうるので、乖離検知は `mirror_sync`（B-3）側で担保する。

#### ミラー3本（`scripts/mirror_*.py`・Issue #481 B-2〜B-4・[ADR-0035](adr/0035-mirror-endpoints-are-parameterized.md)）

3本とも **source / dest を引数で受ける**。両方をローカルへ向ければ予行演習になり、Supabase へ触れるのは接続文字列1つだけになる。**書き込み先はローカル限定**で、リモートを指すと `SystemExit` で止まる（ミラーが本番へ書く経路をコードとして持たない）。

| スクリプト | 役割 | Egress |
|---|---|---|
| `mirror_verify` | 突合。`--level schema`（列差分）/ `counts`（既定・件数と最新キー）/ `checksum`（値レベル） | `schema`・`counts` はほぼ0。`checksum` は両端フルスキャン |
| `mirror_pull` | 初回一括（`pg_dump --compress=0` → 表ごとに `pg_restore`） | 実測（初回コア約300MB） |
| `mirror_sync` | 増分。高水位＋オーバーラップで取り直す | 週次で約4MB/回の見込み |
| `mirror_rehearse` | 予行演習。専用の2 DB を作って一連を回し `--drop` で捨てる | **0** |

```powershell
# 通常運転（8/18 以降）
python -m scripts.mirror_verify --level schema      # pull の前に列差分を確認
python -m scripts.mirror_pull                       # ドライラン（見積りと操作予定）
python -m scripts.mirror_pull --apply --allow-full-pull
python -m scripts.mirror_sync --apply               # 以後は増分
python -m scripts.mirror_verify                     # 0=一致 / 1=乖離 / 2=接続不可

# 予行演習（Supabase 不要・実 financial_db に触れない）
python -m scripts.mirror_rehearse --apply
python -m scripts.mirror_rehearse --drop
```

**予行演習には CREATEDB 権限が要る**（`edinet` は既定で持たない・実測 `rolcreatedb=f`）。案は2つある。

**案A（postgres のパスワードが分かる場合）**——superuser で1回だけ付与する。`financial_db` の中身には触れない:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -c "ALTER ROLE edinet CREATEDB;"
```

**案B（パスワードが不明でも可・2026-08-16 の実走はこちら）**——別ポートに使い捨てクラスタを立てる。`initdb` で作るクラスタは **OS ユーザーが bootstrap superuser** になるため、既存クラスタの認証情報も管理者権限も要らず、**既存クラスタ・Windows サービス・`financial_db` に一切触れない**。

```powershell
$PGD = "<使い捨てディレクトリ>"; $B = "C:\Program Files\PostgreSQL\18\bin"
"edinet" | Out-File -FilePath "<pwfile>" -Encoding ascii -NoNewline   # パスワードは argv に載せない
& "$B\initdb.exe" -D $PGD -U edinet -A scram-sha-256 --pwfile="<pwfile>" -E UTF8 --locale=C
Remove-Item "<pwfile>" -Force
& "$B\pg_ctl.exe" -D $PGD -l "<logfile>" -o "-p 5433" -w start

$env:FINAPP_DB_TARGET   = "local"
$env:DATABASE_URL_LOCAL = "postgresql://edinet:edinet@localhost:5433/postgres"
python -m scripts.mirror_rehearse --apply         # コード変更は不要

& "$B\pg_ctl.exe" -D $PGD -m fast stop            # 後片付け（ディレクトリごと削除）
```

`--locale=C` にすると照合順が既存 `financial_db`（`Japanese_Japan.932`）と違うが、突合のチェックサムは**順序非依存**に組んであるので影響しない（ADR-0035）。

**restore の注意**: `edinet` は superuser でないため `pg_restore --disable-triggers` が使えず、FK は順序だけで満たす。**ダンプの TOC は `--table` の指定順ではなくアルファベット順**（2026-08-16 実測）なので、1回の restore にまとめず **FK 依存順に1表ずつ流す**（並列 `--jobs` も順序が崩れるので使わない）。TRUNCATE は `CASCADE` を使わず、`stock_price_daily`（ミラー範囲外だが `companies` を参照する）まで明示列挙する。

**増分のオーバーラップ**: `stock_price_weekly` は `_recompute_weeks_from_daily` が最大 `DAILY_WINDOW_DAYS=183` 日遡って過去週を上書きするため、**27週（189日）を無条件に取り直す**。Issue #481 / #480 の当初案「末尾8週」では 56 日しか覆えず取り落とす。`macro_data` / `statement_disclosure` は `created_at` が upsert で進まないので日付列＋90日。

#### 未了（8/18 の Egress リセット後）

実際の pull（コア約300MB）と `mirror_sync` の本番実行。詳細は Issue #481 と復旧当日ランブック（#493）。

**`EGRESS_COST_TABLE` の較正取り直しは完了**（2026-08-19・commit 191481a）＝`mirror_verify --level counts --bytes --warn-only` が表ごとに1行（`count(*)` と `sum(octet_length(x::text))`）返すので、16表を一度に測れた。合計 131.1MB / 1,569,144 行。**この 131.1MB は「全列を転送したらこうなる」という見積りであって実際の転送ではない**（サーバ側集約なので返るのは16行）。

**restricted は 2026-08-19 に解除済み**。判定は #493 の3基準（バナー／Usage を `All projects` で見る／大きい表のスキャン時間）で行うこと——`financial_records`(68MB) の `count(*)` が **0.373秒**（8/10 は 25.9秒、解除前日の 8/19 未明は2分超 timeout）。**MCP `get_project` の `ACTIVE_HEALTHY` は組織のクォータ制限を反映しないので判定に使えない。**

**未検証の前提**: `pg_dump` が Supabase の session pooler（`...pooler.supabase.com:5432`）越しに通るか（transaction pooler :6543 は不可）。通らなければ `--source-url` で direct 接続へ差し替える＝コード変更は不要。

---

## 現在の構成

| 項目 | 値 |
|---|---|
| **ホスティング** | Render（[公式](https://render.com/)） |
| **サービス種別** | Web Service（Python） |
| **プラン** | Free（変更時は本ファイルを更新） |
| **Python バージョン** | 3.13.7（`render.yaml` で固定） |
| **起動コマンド** | `uvicorn api:app --host 0.0.0.0 --port $PORT` |
| **ビルドコマンド** | `pip install -r requirements.txt` |
| **DB** | Supabase PostgreSQL（外部、`DATABASE_URL` で接続） |
| **HTTPS** | Render が自動提供（証明書管理不要） |
| **CI/CD** | GitHub `main` ブランチへの push で自動デプロイ |

設定ファイル: `render.yaml` がインフラ定義のソース。環境変数の sync 設定は
Render ダッシュボードで管理。

---

## Render Free プランの制約

設計判断に直結する制約。新機能はこれを前提に作る:

### 1. インスタンス制限
- **メモリ**: 512 MB（OOM に注意。`numpy`/`scipy` 等の重い処理は控えめに）
- **CPU**: 0.1 共有 vCPU 相当（同時実行数は控えめに）
- **稼働時間**: 750 時間/月の無料枠
- **ディスク**: エフェメラル（再デプロイで消える）。永続化は外部 DB のみ

### 2. アイドル時のスピンダウン
- 15 分間アクセスがないとインスタンスが停止する
- 次回アクセス時に **コールドスタート**（数秒〜数十秒）が発生
- 自動収集は **GitHub Actions に統一済み** のため Render の常時稼働は不要:
  - 差分収集: `.github/workflows/daily-incremental.yml` が UTC 18:00 (JST 03:00) に実行
  - 全件収集: `.github/workflows/full-pipeline.yml` を `workflow_dispatch` で手動起動
  - Render 側の `_daily_scheduler` / `_startup_catchup` および keepalive ワークフローは廃止済み
  - ユーザーが Web UI を開いたときのコールドスタートは許容する設計

### 3. シェルアクセスなし
- SSH 接続は不可。デバッグは **Render ダッシュボードのログ閲覧** のみ
- ローカルで再現してから push するワークフロー前提
- DB へのアドホッククエリは Supabase のダッシュボード or psql 経由

### 4. デプロイの仕組み
- `main` ブランチに push すると Render が自動的にビルド＆デプロイ
- ビルド失敗は Render ダッシュボードでログ確認
- ロールバックは Render ダッシュボードの "Manual Deploy" → 過去コミット選択

---

## 外部サービス制約（無料プラン）

> 新機能・改修・データ収集ロジックを設計する際は **必ずこの節を参照**し、各サービスの無料プラン制約に違反しない方式を選ぶこと（旧 `CONSTRAINTS.md` を統合）。Render 自体の制約は上記「Render Free プランの制約」節を参照。

### GitHub Actions（無料アカウント）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| 月間利用上限 | **無制限**（本リポジトリは **PUBLIC**・2026-08-03 に `gh repo view --json isPrivate` → `false` で実測） | **分数は制約ではない**（#422）。Private 無料枠 2,000 分だったら `daily-incremental` の実測 2h05m〜2h38m/日 ≈ **4,500分/月** で足りず、そもそも現行運用が回らない。「分数が足りないから夜間バッチを増やせない」という誤った制約認識で方式選定しないこと。真の制約は下記「1ジョブの最大実行時間」と Supabase の Egress 5GB/月 |
| 1ジョブの最大実行時間 | **6時間（360分）** | 長時間処理は各ジョブを6時間枠内に収める |
| 同時実行数 | **20並列**（`max-parallel: 1` で逐次化） | full-pipeline は逐次実行。並列化すると Supabase 接続数上限に当たる |
| Runner の IP | **Azure クラウド IP** | stooq: 完全ブロック。Yahoo Finance: GitHub Actions からは動作。J-Quants / EDINET: 動作 |
| Artifact 保存期間 | `retention-days: 7` に統一 | — |

**可視性**: 現在 **PUBLIC**（Actions 分数は無制限）。Private へ戻すと無料枠 2,000 分/月に収まらず定常運用が破綻する（上表参照）ため、戻す判断をする場合はワークフローの本数・頻度の再設計とセットで行う。secrets は可視性と独立して保護される。

**ジョブ所要時間（設計参考値）**:
- `full-pipeline.yml` finalize（Phase 3〜5）: **run 31229841870（2026-08-08・`jquants_days=180`）で 255.8分 実測**（`timeout-minutes: 355`）。内訳 = 成長率/Zスコアは VIEW 算出でスキップ ／ **マクロ 6.0分** ／ **Yahoo ギャップ補完 83.3分**（3,708件・4,437社 × `YAHOO_STOCK_RATE_SLEEP=0.5秒`＝差分が1日でも全社ループの固定費がかかる） ／ **J-Quants 23.4分**（130営業日中70日を取得＝235,359件 upsert・60日は契約窓外でリクエストせずスキップ） ／ **`update_market_data_from_history` 143.1分**（42,289レコード）。
  - **J-Quants は窓長にほぼ比例**（1営業日 = `JQUANTS_RATE_SLEEP` 20秒）。`jquants_days=730` なら窓内約462営業日＝約154分。`full-pipeline.yml` の `jquants_days` 入力で切り替える。
  - **`update_market_data_from_history` の143.1分は #464 で解消済み**（1件1 UPDATE → `UPDATE ... FROM` の一括更新でローカル約56秒）。GHA では往復回数が 42,394 → 22 に減るため、この項は数分以下になる見込み。**次回の実走で再計測すること。** 解消後は 730日でも合計約250分で 355分に収まり、公式値置換のために専用ワークフローを分ける必要はない。
- `backfill-stock-history.yml`: 対象＝stock_price NULL かつ period_end 730日超前（初回 約3,800社）。`YAHOO_STOCK_RATE_SLEEP=0.5秒`・1社1リクエストで **約60〜90分**（`timeout-minutes: 150`）。
- `backfill-weekly-history.yml`（#198）: 対象＝`stock_price_weekly` の最古日が `today-years` より新しい社。`backfill_weekly_history_yahoo` が Yahoo から過去方向に取得し、**1社ごとに `record_prices_batch(trim=True)`** で daily→weekly 再集約しつつ daily を都度 trim する（5年×全社の daily 同時展開を避け Supabase 500MB を超えない）。`YAHOO_STOCK_RATE_SLEEP=0.5秒`で **約60〜150分**（`timeout-minutes: 150`）。

### Supabase（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| DB ストレージ | **500 MB** | `SKIP_XBRL_RAW=true` を維持＋株価は close-only 2本立て（下記「容量設計」） |
| 接続数 | **最大60接続**（pgbouncer 経由） | 並列パイプライン実行を禁止。`max-parallel: 1` を維持。枯渇時は接続確立自体が `FATAL: (ECHECKOUTTIMEOUT) unable to check out connection from the pool after 15000ms in Session mode` で失敗する（`pool_pre_ping` では救えない＝**再試行で拾う**・#470） |
| クエリのタイムアウト | **`statement_timeout=2min`** / `lock_timeout=0`（無制限待ち）/ `idle_in_transaction_session_timeout=0`（`postgres` ロールの既定・2026-08-09 実測） | GHA↔Supabase から走る重い1文（一括 UPDATE・大量 upsert・`VACUUM FULL`）はこの 2min に当たる（#470/#471 は**同じ日に2本のワークフローを落とした**）。引き上げは `db_timeouts`（database.py）で**その文の実行中だけ**行い、ロールやプロセス全体の既定は変えない。詳細は [GOTCHAS.md](GOTCHAS.md) |
| 一時的 read-only 移行 | トランザクションが長すぎると自動移行 | `run_full_collection` は `MASTER_BATCH=200` 件ごとに commit |
| プロジェクト停止 | **1週間アクセスなしで自動停止** | 長期不使用時は要注意 |
| **Egress（転送量）** | **5 GB / 月** | 超過で全サービス restricted（402）＝2026-07 に検証系のフルロード反復で 61.2GB（1224%）まで出した実績あり。**日次で回るジョブは1回あたりの転送量が月次上限に直結する**（下記「Egress 設計」） |

#### Egress 設計（日次ジョブ = 最大の消費者・Issue #446）

**測り方は二階建て**（Issue #478・[ADR-0034](adr/0034-client-side-egress-ledger-and-circuit-breaker.md)）。

| 層 | 何を使うか | いつ | 精度 |
|---|---|---|---|
| **正本** | サーバ側 `sum(octet_length(<列>::text))` | 手動・設計判断のとき | 実測（誤差数%） |
| **常時** | `db_egress` のクライアント側台帳（行数 × 較正済み B/行） | 全プロセス・全経路で自動 | 推定 |

**正本**: 行を持ってこずにサーバ側で `octet_length(<列>::text)` を集計する（psycopg2 はテキストプロトコルなので、これが実際に流れるペイロード長。TLS/TCP ヘッダは含まないが誤差は数%）。ダッシュボードの数字は他の消費と混ざって分離できないため、**列単位で積み上げた実測をこちらの正本とする**。

**常時計測（`db_egress.py`）**: `engine` の `after_cursor_execute` フックが「どのクエリが何行・何列を返したか」を全プロセス（GHA バッチ・ローカル CLI・Render）で記録する。psycopg2 の既定カーソルはクライアント側バッファなので、このフックの時点で行は既に転送済み＝`cursor.rowcount` がそのまま転送行数になる（結果は消費しないので既存挙動に干渉しない）。プロセス終了時に `[egress] summary job=... total=...MB rows=... top=<テーブル>:<MB>,...` を標準エラーへ1行出す。ワークフローの帰属は各 yml の `FINAPP_JOB` で付く。

- **なぜ要ったか**: 2026-07（61.2GB）・2026-08（7.312GB）の2回とも、超過後に「誰が食ったか」を答えられなかった。当時の計測は `scripts/_cache.py` の HIT/MISS だけで、**夜間バッチ本体・`routers/`・`collector*.py` は完全に無計測**だった。
- **推定は正本ではない**。較正値（B/行）は下表の実測から導出しており、未較正の組み合わせは 17.5 B/列/行 の保守的な既定を使う（#446 時点は 12.0。#493 の全表実測で `macro_beta_loadings` が 17.3 とそれを上回っていたため引き上げた＝**既定は実測レンジの上側に置く**）。**台帳の役目は帰属とブレーカであって、実測の置き換えではない。**
- **較正には2世代ある**。#446（2026-08-06）は消費側が実際に投げる**部分列**、#493（2026-08-19）は mirror 16 表の**全列**。**部分列エントリを全列エントリで上書きしてはいけない**——列ごとに B/値 が違うため過小評価になる（`stock_price_weekly` は 3列で 10.7 B/列/行、全列平均では 7.8 B/列/行）。
- **ロールアップ**: `python -m scripts.egress_report`（JSONL 台帳）／`--log <gh run view --log の保存先>`（run ログの `[egress] summary` 行）。DB に繋がないので実行しても Egress は増えない。**JSONL は既定で `.egress/ledger.jsonl` へ書かれる**（2026-08-19・#478 の穴3）——以前は `FINAPP_EGRESS_LEDGER` を人が手で立てる運用で、**過去2回の超過の主因だったローカル検証の反復が1バイトも記録されていなかった**。無効化は `FINAPP_EGRESS_LEDGER=0`。GHA では全ワークフローが `upload-artifact` の `path` に `.egress/*.jsonl` を含める（`tests/test_db_egress.py::TestWorkflowLedgerCollection` が回収漏れを CI で落とす＝**漏れは failure を出さないので通知では拾えない**）。

**サーキットブレーカ**: プロセス単位で `FINAPP_EGRESS_ROW_LIMIT`（既定 3,000,000 行）/ `FINAPP_EGRESS_MB_LIMIT`（既定 400MB）を超えると `EgressBudgetExceeded` を送出する。既定は既知の最大実行（夜間バッチ ≒ 1.39M 行 / 67.7MB）の約2倍。GHA では例外＝failure ＝ `notify-failure.yml` が Issue を自動起票するので、**ブレーカは自分で自分を報告する**。

- 正当に重い一回性の処理は `db_egress.egress_budget(mb=..., rows=...)` で**その区間だけ**引き上げる（ADR-0032 の `db_timeouts` と同じ原則＝上書きは局所・既定は変えない）。
- 緊急時の全体解除は `FINAPP_EGRESS_ENFORCE=0`。**計測は続く**ので台帳は埋まる。

**請求サイクル累計（第2の軸・Issue #478・[ADR-0037](adr/0037-egress-cycle-budget-is-a-second-axis.md)）**: プロセス予算だけでは月枠を守れない。400MB/プロセスなので**1日に12プロセス走れば 4.8GB/日を流しても一度も踏まれない**。過去2回の超過は形が違っていた——2026-07（61.2GB）は暴走型でブレーカが効くが、**2026-08（7.312GB）はスパイク約2GB＋平常運転 約5GB の「じわじわ型」で、プロセス予算は原理的に無力**だった。

| 軸 | 何を守るか | 置き場所 | 閾値 |
|---|---|---|---|
| プロセス予算 | 1回の暴走をその場で止める | プロセスメモリ | 3,000,000 行 / 400MB |
| **サイクル累計** | **月枠に対する残量をプロセス跨ぎで守る** | **`app_settings.egress_cycle_bytes`** | warn 80% / block 95% |

- **DB に置く理由**: ローカル CLI と GHA ランナーが**同じカウンタを見られる唯一の場所**だから（`weekly_price_cache` が世代印を DB に置いたのと同じ理屈）。ファイルに置くと手元で払った Egress が GHA から見えない。
- **サイクル境界**は `db_egress.EGRESS_CYCLE_DAY = 18`（ダッシュボード表記 "18 Aug 2026 - 18 Sep 2026"）。印が現サイクルと違えば累計は 0 から数え直す＝**前サイクルの値を繰り越さない**（リセット直後の誤警報を作らない）。
- **block を 100% に置かない**。使い切った瞬間に全ジョブが死ぬより、残り 5%（256MB）を人の判断用に残す方が復旧が速い（restricted のコストは実測で8日間）。
- **ミラー接続では積まない**（`FINAPP_DB_TARGET=local`）。ローカルからの読取は Egress を1バイトも使わないので、積むと**ミラーへ逃がす動機を自分で壊す**。台帳 JSONL には残る。
- **pytest 実行中は必ず無効**。ローカルの pytest は `.env` の `DATABASE_URL` を読んだ本番向け engine を import するため、有効なままだと全テストが本番へ接続し atexit が本番へ書き込む（実装中に実際に起きた）。
- 緊急停止は `FINAPP_EGRESS_CYCLE=0`（プロセス予算と JSONL 台帳は生きたまま）。
- 閾値超過の通知は `egress-health.yml`（毎日 UTC 21:00）が exit 2 → `notify-failure` が Issue 起票。

夜間スコア更新（`sector_ols` + M-6）の実測。**2026-08-20 の回は正本＝ローカル PostgreSQL に対する実走**で、
台帳（`.egress/ledger.jsonl`・job=`nightly-local`）のテーブル別内訳がそのまま取れる:

| 引くもの | 行数 | 削減前（2026-08-06） | 実測（2026-08-20・#482） |
|---|---|---|---|
| `financial_metrics` VIEW（97列 → 消費36列・#459） | 30,298 | 22.5 MB | **8.76 MB**（`octet_length` 実測は 6.69MB） |
| `macro_data`（`{series: {date: close}}` にしか使わない） | 87,355 | 8.7 MB | **3.50 MB** |
| `macro_beta_loadings`（7列 → 消費4列・#482） | 49,283 | 4.86 MB | **3.41 MB**（実測 3.18MB） |
| `stock_price_weekly`（3列・差分ロード後の定常・#480） | 103,976 | 51.4 MB | **3.34 MB** |
| `financial_records` 最新 annual（69列 → 消費20列・#482） | 4,430 | 2.8 MB | **0.82 MB** |
| `companies` | 8,876 | 0.5 MB | **0.63 MB**（`calls=2`） |
| `regression_results`（sector_ols の**書き込み**） | 3,611 | — | 0.30 MB（`calls=3,623`＝1社1文） |
| **1回あたり合計** | 287,831 | **86.0 MB** | **20.8 MB** |

> **見積り 52.0MB に対し実測 20.8MB**。差の主因は #480（週次差分ロード）で、`stock_price_weekly` が
> 39.3MB → 3.34MB に落ちた（初回だけフルロード）。#459/#482 の列絞りも効いており、
> `financial_metrics` は 22.5 → 8.76MB、`financial_records` は 2.8 → 0.82MB。
>
> **`companies` の `calls=2` が #482 の副産物の確認になっている**——列指定 Row から `relationship` が
> 消えて `record.company.issued_shares` が黙って None になる罠を SQL 側 COALESCE で潰した結果、
> N+1 が JOIN 1本になった（N+1 が残っていれば 4,430 回級の calls が出る）。
>
> 一方 **`regression_results` は `calls=3,623`＝1社1文の書き込み**が残っている。読み取り列の話ではないので
> #482/#489 の対象外だが、所要には効く（→ 別 Issue）。
>
> なお **この測定はもう Supabase の枠を1バイトも使わない**（#503 で正本がローカルへ移った）。
> 「restricted 中は測定自体が枠を食う」という #478 当時の制約は解けている。

- **削減した2列は「消費側が一度も読まない列」だけ**。`volume_sum` は `px_volz`（出来高z-score）専用で、M-1 は price_features を持たず M-2/M-6 は既定 OFF＝`load_data(db, with_volume=...)` で選択時のみ引く。M-3 は既定 ON なので従来どおり引く。
- **未ロードは `None` ではなく番兵**（`macro_snapshots._VOLUME_NOT_LOADED`）にして、読もうとしたら即 `ValueError`。欠測として扱うと `px_volz` が全 nan になり「データが薄い」と見分けがつかない＝#438 と同型の静かな故障になる。
- **残る最大項だった `financial_metrics` VIEW の全列 22.5MB は #459 で列指定へ切り替えた**（2026-08-10）。`plugins/macro_snapshots.py::FIN_LOAD_FIELDS`（36列＝`FIN_BASE_OPTIONS` の選択肢＋`recommend.METRICS` の非 RUNTIME 列＋突合/メタ6列）だけを引き、軽量 namedtuple `_FinRow` で返す。**削減後の実測は 2026-08-21 に取得済み**＝36列 220.8 B/行（6.69MB / 30,298行）に対し全列 97 は 685.4 B/行（20.77MB）＝**1/3 へ落ちた**（上表）。
  - 列を落とした結果が黙って欠測に化けないよう、`FIN_LOAD_FIELDS` 外の列を `fin_features` に渡したら `build_snapshots` が `ValueError` を投げる。`getattr(..., None)` 任せだと全社が捨てられて学習0件になり「データが薄い」と誤読する（`volume_sum` の番兵と同じ考え方）。
  - 消費側との対応（`recommend.METRICS` / `FIN_BASE_OPTIONS` / `scripts/candidate_bakeoff._FIN_FIELDS`）は `tests/test_macro_snapshots_loaders.py` のメタテストが CI で照合する。列の追加漏れは failure ではなく「静かに全社が消える」形で出るため、通知では拾えない（ADR-0031 と同型）。
- **残る5経路も列指定へ広げた（#482・2026-08-15）**。#459 と #441 以外は全列 ORM ロードのままで、「静かに枠を食う」形でしか現れないため誰も気づかない状態だった。
  - `plugins/sector_ols.py::_load_records` — `sector_load_fields(features)` が選択 features から列を導出（既定10項目なら **69列 → 20列**）。`shares_outstanding` の第2優先（`record.company.issued_shares`・#462）は列指定 Row にリレーションが無く消えるため、SQL 側の `COALESCE(FinancialRecord.issued_shares, Company.issued_shares)` で優先順位を保つ。副産物として `issued_shares` が NULL の社ごとに `companies` を引いていた N+1 が JOIN 1本になる。
  - `plugins/sell_ranking.py` — `SELL_SELECT_COLS`（**97列 → 18列**＝表示9＋VIEW指標6＋`nc_ratio` の入力3）。週次は `week_start >= today − 400日` の下限＋500社チャンク＋3列で、保有20銘柄あたり 0.43 → 0.037 MB。**ユーザーが押すたびに払う経路**なので効き方が日次ジョブと違う（下記）。
  - `plugins/utils.py::get_macro_features` — `macro_data` 11列 → 3列（`_preload_macro_impl` と同じ。非対称の解消）。
  - `database.py::get_macro_beta` — `macro_beta_loadings` 7列 → 4列。加えて `with_loadings=False` を新設した。**4呼び出しのうち3つは `meta` の `selected_factors` だけを見て loadings を捨てていた**ので、そこは転送自体を止める。
- **新規の全列ロードは CI で落とす**: `tests/test_column_scoping.py` が `plugins/` ＋ `routers/` ＋ ルート直下を **AST で走査**し、`db.query(Model)`（＝全列）を検出して許可リスト `FULL_ROW_LOADS` と**双方向差分**を取る（未登録＝fail／実体の消えた登録＝fail／理由文20文字未満＝fail）。`.first()` 等の単一行終端と `with_entities` は対象外。正規表現ではなく AST なのは、`db.query(M.col)` との判別と docstring 内コード例の除外のため。`scripts/` は `scripts/_cache.py`（#355）の別制御下なので対象外。
  - **静的解析で完結させる理由**: `db_egress._Bucket` は `n_cols` を保持せず、SQLite では `cursor.rowcount = -1` で `unknown_calls` にしか積まれない。「このクエリが何列引いたか」を実行時に測る手段が無い。
- **他の消費**: `daily-incremental`（収集は主に ingress だが `update_market_data_from_history` の読みがある）・ローカルの `scripts/` 検証（`scripts/.cache/` の pickle キャッシュで反復 pull を抑える・Issue #355）。1.98GB は**このワークフロー単独の値**なので、他を足した余裕で判断すること。

##### 週次株価の差分ロード（#480・[ADR-0036](adr/0036-weekly-prices-incremental-load.md)）

上表の最大項（`stock_price_weekly` 39.3MB）は**毎晩ほぼ同じ行を送り直していた**。1日の増分は約4,400行＝転送の 99.7% が不変データの再送。列を削る（#446/#459/#482）とは別の軸で、**行を送らない**手当てが要った。

| | 転送 | 月30回 |
|---|---|---|
| 従来（毎晩フルロード） | 39.3 MB | 1.98 GB（枠の40%） |
| 差分ロード（定常） | 約 3.7 MB（27週 ≒ 9.3%） | 約 1.06 GB |
| ＋週1回の強制コールド | 39.3 MB × 4 = 157 MB | **実効 約1.11 GB（枠の22%）** |

- **仕組み**: 指紋（`max(week_start)` + `count(*)`＝サーバ側集約なので Egress は2行ぶん）でキャッシュ世代を判定し、**直近27週は常に再取得**して訂正を吸収する。27週は `database.WEEKLY_OVERLAP_DAYS`（`DAILY_WINDOW_DAYS` からの導出）で、ミラー同期（#481）と**同一オブジェクト**を共有する。
- **キャッシュの置き場**: ローカルは `.weekly_cache/`、GHA は `actions/cache`（`nightly-scores.yml` のみ）。`scripts/.cache/`（#355）とは別物で、ログ接頭辞も `[wpcache]` と `[cache]` で分けてある。あちらは検証用で TTL 無し・明示リフレッシュのみ＝要求が逆を向いている。
- **指紋では見えない訂正がある**。`repair_price_scale_breaks`（#465）は該当社の**全期間**を書き換えるので、行数も `max(week_start)` も変わらない。そこで `app_settings.weekly_prices_generation` を世代印とし、**書き手が印を進める**。印を DB に置くのは、修復 CLI がローカルで走りキャッシュは GHA ランナーに載るため（ディスク上の印では届かない）。
- **印を進めるのは構造的な条件**: `_recompute_weeks_from_daily` が「保持窓より古い週を実際に書き換えた」とき。定常経路は取得開始日を `today − DAILY_WINDOW_DAYS` でクリップするのでここへは来ない。明示フック（repair / backfill-weekly）は保険であって主ではない——列挙は必ず漏れる（ADR-0031「登録≠実行」と同型）。
- **ログの読み方**: `[wpcache] HIT ... fresh=<行数>` と `[egress] summary` の `top=stock_price_weekly:<MB>` を突き合わせる。乖離したら**キャッシュを通らない別経路が残っている**（#478 で `scripts/_cache.py` について学んだ読み方と同じ）。全ワークフローが `.egress/*.jsonl` を artifact に含めるので、`python -m scripts.egress_report` で構造化データから読める。
- **静かな劣化への歯止め4層**: ①行数照合はハードゲート（不一致は必ずフルロード）②鮮度アサートは raise（GHA では failure ＝自動起票）③週1回の強制コールド（`FINAPP_WEEKLY_CACHE_MAX_AGE_DAYS`・既定7）④コールド時のドリフト監査（差分が触らない過去区間を旧キャッシュと突合・追加 Egress ゼロ）。stale なパネルで μ̂ を出しても failure は出ないので、設計側で塞ぐしかない。
- **初回は必ずフルロード**。GHA キャッシュが載る翌晩から効く。復帰判断は従来値で行うこと。
- **緊急停止**: `FINAPP_WEEKLY_CACHE=0`。このとき**指紋クエリすら発行しない**＝従来と1文も変わらない。

**リクエスト経路の Egress（#482）**: 上表は日次ジョブの話だが、`/api/plugins/sell_ranking/run` と `/api/morning` は**ユーザーが押すたび**に払う。回数が cron ではなく操作頻度で決まるので、1回の重さがそのまま効く。

| 引くもの | 削減前 | 削減後（見積り） |
|---|---|---|
| `financial_metrics` ユニバース（97 → 18列・4,430行） | 3.45 MB | 0.64 MB |
| `financial_metrics` 保有分（同上・20行） | 0.02 MB | 0.003 MB |
| `stock_price_weekly`（7列全期間 → 3列400日窓） | 0.43 MB | 0.04 MB |
| `macro_data`（11 → 3列） | 1.34 MB | 0.40 MB |
| `macro_beta_loadings`（`with_loadings=False` で転送ゼロ） | 4.86 MB | 0 MB |
| **1リクエスト合計** | **10.10 MB** | **1.08 MB（−89%）** |

`macro_beta_loadings` の B/行は #493（2026-08-19）で実測へ差し替えた：**121.4 B/行（7列・10.5MB / 90,841 行）**。それまでは較正値が無く 12 B/列/行 = 84.0 B/行 の既定を当てており、**実測はその 1.44 倍＝保守側に置いたつもりの既定が過小だった**（上表の 3.36 MB → 4.86 MB はこの比で引き直した値）。`DEFAULT_BYTES_PER_COLUMN` を 17.5 へ引き上げたのはこの実例が根拠。

**全表較正（#493・2026-08-19）**: Egress リセット直後に mirror 16 表を全列で測り直した。**明細（B/行・列数・行数）の正本は `db_egress.EGRESS_COST_TABLE` のエントリと note**（ここに書き写すと黙って陳腐化する）。設計判断に効く要点だけ:

```powershell
$env:FINAPP_JOB = "egress-calibration"
python -m scripts.mirror_verify --level counts --bytes --warn-only   # 表ごとに1行＝Egress ほぼゼロ
```

- **16 表・全列の合計は 131.1 MB**（1,569,144 行）。これが **ミラー初回 pull（#481 手順3）の見積りの母数**であり、枠 5GB の 2.6%。1,284,465 行の `stock_price_weekly` だけで 66.6MB＝**半分がここ**
- **通常の表の B/列/行 は 7.9〜17.3 に収まる**。ただし JSON 列を持つ2表（`macro_beta_meta` 492、`plugin_tuned_params` 1238）は桁が違う＝**平均から外して読む**
- **`statement_timeout=2min`（ADR-0032）には当たらなかった**。128万行の `octet_length` 全走査も含めて全16表が通ったので、`table_stats()` に timeout の局所引き上げは足していない（当たるようになったら `database.db_timeouts` で包む）
- `financial_metrics` は VIEW でミラー対象外＝この回では未測。#446 の 779 B/行（97列）が引き続き唯一の実測

**キャッシュが効いているかを見る（#478）**: `scripts/` 系の検証 CLI は実行のたびに `[cache] HIT/MISS/REFRESH <key>` を標準エラーへ出し、終了時に `[cache] summary hits=N misses=M produced=X.XMB` を出す。**MISS と REFRESH は本番 DB を引いた＝Egress を使った**という意味なので、2回目以降の実行で misses が減らないならキーが実質毎回ミスしている。2026-07 と 2026-08 の2回とも、超過後にこの内訳が分からず原因の切り分けに時間を要した（黙ってミスしても気づけない＝#438 と同型）。`produced` は pickle のバイト数であって Egress そのものではない（正本は上記の `octet_length` 実測）。

#### 容量設計（株価 = 最大の消費者）

旧 `stock_price_history`（日次OHLCV全履歴）が約 359MB / 全体80% を占め、年約220MB で増加して 500MB 上限の主犯だった。**close-only の2本立て**へ移行して恒久対策とする：

- **`stock_price_daily`**：直近 `DAILY_WINDOW_DAYS`（≒6か月）の日次終値のみ。収集のたびにローリング削除（trim）でサイズが頭打ち……のはずだが、DELETE ベースの trim は btree インデックス（`pk_stock_price_daily`/`ix_spd_trade_date`）を bloat させ続け、autovacuum は死領域をテーブル内で再利用するのみでファイルサイズは縮まない（**Issue #290**・実測: 2026-07-09 VACUUM FULL 直後 48MB→3日後 72MB）。週次 `vacuum-maintenance.yml`（VACUUM FULL・毎週自動実行）で物理サイズを頭打ちにする。チャートの日次ズーム・短期バックテスト用。**VACUUM 本体の所要は伸びている**（43〜92MB で 7.6〜10.4秒／2026-07-18〜08-01 → **79MB で 37.4秒**／2026-08-09 手動実行・79MB→49MB・DB 426MB→395MB）。`statement_timeout` は `'0'` にして時間で殺さず、歯止めはワークフローの `timeout-minutes: 30` 側に置く（#471）。
- **`stock_price_weekly`**：全履歴の週次集約（追記専用・trim しない）。`close_last`＋生集約 `volume_sum`/`turnover_sum`/`n_days` のみ保持し、**VWAP・相対流動性は派生**（保存しない）。チャート全期間・長期バックテスト・将来の予測モデル用。
- 見通し：5年分 weekly ≈ 145MB、総計 ≈ 285MB / 500MB、+約37MB/年（runway 約6年）。書き込みは単一チョークポイント `record_prices_batch`（daily upsert→触れた週を weekly 再集約→trim）。

**移行（一回限り・ローカル実行 `migrate_stock_price_dual.py`・2026-06 完了済みでスクリプトは撤去／以下は手順記録）**：満杯DB（≈448MB）で新旧テーブルを併存させると 500MB 超で read-only に墜落するため、**ローカルで集約計算 → 旧テーブル DROP（即解放）→ コンパクトな新テーブルをアップロード** の順で Supabase 側ピークを上げない（[GOTCHAS.md](GOTCHAS.md) 参照）。

**将来オプション（いずれも未実装・発動条件つき）**：
- *別ストア退避*（S3互換 / 別Postgres 等）：真の日次OHLCV・出来高分析・intraday が要件化したとき、または Supabase 使用量が 400MB を再突破したとき検討。
- *ティアード in-place 圧縮*（1テーブルで日次→週次→月次に加齢圧縮）：上記2本立てで容量・UXとも足りているため不要。再考は別ストアと同条件。

**後続PR（本対策に連なる別タスク）**：
- *予測モデルの平滑化ターゲット化*：`turnover_sum`/`volume_sum` 由来の VWAP・相対流動性を説明/被説明変数に。年次株価変動ノイズ対策。MODELS.md 更新を伴う。
- *`financial_records.raw_xbrl_json` の drop*：**実装済み（Issue #219 ①）**。financial_records 73MBの主因＝第2の容量レバーだった列を冪等DROPマイグレーション（`database.py::_DEBUG_ONLY_COLS`）で削除し、ヘッドルームを確保。
- *過去2〜5年の Yahoo 週次バックフィル*：J-Quants 無料は2年上限のため、5年時系列（財務5年と整合）を Yahoo から `stock_price_weekly` へ補填。**実装済み（#198・`backfill-weekly-history.yml` / `backfill_weekly_history_yahoo`）。本番実行は use_momentum 常用時に手動で1回**。

#### バックアップ運用ポリシー

##### 自動バックアップ（Supabase 標準機能）

Supabase 無料プランは **毎日1回の自動バックアップを7日間保持** する（Point-in-Time Recovery は有料プランのみ）。

| 項目 | Free プラン |
|---|---|
| 自動バックアップ頻度 | 1日1回 |
| 保持期間 | **7日間** |
| PITR（任意時点復元） | 非対応（Pro プラン以上） |
| 確認場所 | Supabase ダッシュボード → Project Settings → Database → Backups |

##### 手動バックアップ（スキーマ変更・大規模更新前に実施）

重大な DB 変更（`ALTER TABLE`・データ移行・全件再収集）の前は手動バックアップを取得する。

```
# Supabase ダッシュボードから
Project Settings → Database → Backups → "Create Backup"（Pro）
 ↑ Free プランでは不可。代わりに pg_dump を使う：

pg_dump "$DATABASE_URL" \
  --no-acl --no-owner \
  --format=custom \
  --file="backup_$(date +%Y%m%d).dump"
```

`DATABASE_URL` は Render・ローカルの `.env` に設定されている接続文字列（`postgresql://...?sslmode=require`）を使う。

##### 復旧手順

**Supabase ダッシュボードから復元する場合（7日以内）**:
1. Supabase ダッシュボード → Project Settings → Database → Backups
2. 復元したい日時を選んで "Restore" をクリック
3. 復元中は DB が停止（数分〜十数分）→ Render の Web サービスも一時的に 503 になる
4. 完了後、`/health` で DB 疎通を確認

**pg_dump バックアップから復元する場合**:
```
# 既存 DB を全消去してから復元（⚠️ 不可逆操作）
pg_restore --clean --no-acl --no-owner \
  -d "$DATABASE_URL" \
  backup_YYYYMMDD.dump
```

##### プロジェクト停止（1週間無アクセス）からの復旧

Supabase 無料プランは **1週間アクセスなしで自動停止**する。

1. Supabase ダッシュボード → 該当プロジェクト → "Restore project" ボタン
2. 起動完了まで数分待つ
3. Render は `DATABASE_URL` で再接続を自動リトライするため、Render 側の操作は不要
4. GitHub Actions の差分収集（`daily-incremental.yml`）が翌日から再開されることを確認

**長期離席時の対策**: UptimeRobot 等で `/health` を定期 ping する（O-2 参照）と自動停止を防げる。

### J-Quants API（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| レート制限 | **約5リクエスト/60秒** | `JQUANTS_RATE_SLEEP = 20.0` 秒間隔を維持 |
| 取得可能期間（過去側） | **過去2年分** | `days_back ≤ 730`。UI の選択肢もこれに合わせること |
| **配信遅延（直近側）** | **直近約12週間は配信されない** | 無料プランは株価を**約12週遅れ**で配信。`today − 12週` より新しい日付は空レスポンスになり、J-Quants だけでは鮮度がここで頭打ちになる（例: 2026-06-10 時点の最新は ≈2026-03-17）。**直近12週の鮮度は Yahoo Finance ギャップ補完（`fill_recent_stock_price_gap_yahoo`）で埋める**。差分パイプライン（`_pipeline_incremental.py` Phase 4）に加え、**`full-pipeline` の finalize（`_pipeline_gh.py` Phase 5）も #426 で同じ順序（Yahoo → J-Quants → `update_market_data_from_history`）を持つ**。以前は finalize が J-Quants のみで、全件収集を回しても直近12週の株価が1日も前進しなかった |
| **会社予想開示（`/fins/summary`）の鮮度上限** | **`today − 84日`**（12週固定＝`JQUANTS_DISCLOSURE_DELAY_DAYS`） | `statement_disclosure.disc_date` は **どれだけ収集しても `today − 84日` より新しくならない**。2026-08-02 に観測した「MAX 2026-04-17 ＝ 3.5ヶ月前」は最終収集 2026-07-12 − 84日と**完全に一致**しており、**収集の失敗ではなく無料プランの構造的上限**である（#424 子4）。`collect-disclosures.yml` に cron を足しても天井は動かない（動くのは「84日前のデータが揃うまでの遅れ」だけ）。したがって開示由来の特徴量は**当日の売買判断には原理的に使えない**。齢を見て「壊れている」と誤診しないこと |
| 429 リトライ | 指数バックオフ禁止 | 429 発生時は **90秒待機→1回のみ再試行**。失敗したら skip |
| 営業日データのみ | 土日祝は空レスポンス（**covers 文言の無い 400**） | 空レスポンスを skip として扱う。covers 文言があれば窓外なので別扱い（下段） |
| **カバレッジ境界は 400**（#462・2026-08-08 実測） | 契約窓の外側は過去側・エンバーゴ側とも **400**＋ボディ `Your subscription covers the following dates: A ~ B`。窓は `[today − 84 − 730, today − 84]` のローリング（実測 `2024-05-16 ~ 2026-05-16`） | `JQuantsOutOfCoverage` として送出し、**窓を1日学習したら残りの窓外日は HTTP を投げずに飛ばす**（叩いてから 400 を受けると 1日 20秒＝730日窓で約20分を捨てる）。**エンバーゴ日数をコードに決め打ちしない**——固定値で切るとプランやエンバーゴが変わったとき取れるはずのデータを黙って捨てる（#438/#461 と同型の静かな劣化）。学習コストは境界を踏む1リクエストのみ。**非営業日の 400 と同一視しない**——同一視すると60営業日ぶんの空振りが「祝日」と同じログで消える |
| **403 は3種すべて「日付非依存」**（#412 → #425 → #461 → #462） | ①`No active subscription found`＝契約失効 ②`This API is not available on your subscription`＝プラン対象外（例 `/fins/details`） ③`The requested endpoint does not exist`＝v2 に存在しない URL。**カバレッジ境界は含まれない** | `JQuantsAccessError.reason` で分類し、失効だけ専用の `log.error` を出す（他と潰すと平常運転と読み違える）。どれも日付を変えても直らないため**連続 `JQUANTS_MAX_CONSECUTIVE_FORBIDDEN`(=10) 日 403 で残りを打ち切る**（打ち切らないと 523営業日 × 20秒 = 174分を空振りに使い finalize が timeout・run 31126473273 の実例）。**全日程403でも例外は投げない**（#425）: `all_forbidden: True` を返し呼び出し側が継続可否を決める——J-Quants の失敗が、同じキーに依存しない Yahoo ギャップ補完まで巻き添えで止めるのを防ぐ |
| **`/markets/listed/info` は v2 に存在しない**（#425 → #461 → #462 で決着） | ~~無料プランに権限が無い~~ → **URL 不在**。後継は **`/equities/master`**（実測 200・4,446銘柄）。ただし master は**発行済株式数を持たない**（`Code/CoName/Mkt/Mrgn/ProdCat/S17/S33/ScaleCat/Date` のみ） | `sync_active_status`（#315）は `/equities/master` へ移行。`issued_shares` は **`/fins/summary` の `ShOutFY`**（自己株 `TrShFY`・期中平均 `AvgSh` も同梱）へ移行し `statement_disclosure` へ保存する。#425 の v2 移行以降、この2機能は契約とは無関係にずっと停止していた |
| **`/equities/master` もエンバーゴされる**（#463・2026-08-08 実測） | レコードの `Date` は**全件 `2026-05-15` の単一断面**＝「今日−84日」。**as-of より後に新規上場した銘柄は載らない** | 「マスタに無い＝廃止」と読むと **IPO 直後の銘柄を誤って `is_active=False` にする**（589A/607A が実際に該当し、recommend / gap_analysis / net_cash_analysis の母集団から落ちた）。`sync_active_status` へ `master_as_of` を必ず渡し、**価格履歴の開始が as-of より後**の銘柄を delisted 判定から除外する。<br>**「as-of より後にも取引がある」で判定してはいけない**（実測で棄却）——as-of の数日後に廃止した銘柄（3593/4917/6901＝最終売買 2026-05-19）まで保護してしまう。前提として `DAILY_WINDOW_DAYS`（約6か月）がエンバーゴ 84日より長いこと |

**設計制約（実装時の必須ルール）**:
- **認証情報**: `.env` に `JQUANTS_API_KEY` を設定。未設定時は `ValueError` で明示エラー。
- **データ優先度**: J-Quants = JPX公式 → stooq より正確。`ON CONFLICT DO UPDATE` で上書き（stooq は `ON CONFLICT DO NOTHING`）。
- **コード変換**: J-Quants は5桁コード（例 `"13010"`）。先頭4桁が証券コード（`code[:4]`）。
- **取得単位**: 日付単位で全銘柄を一括取得。1営業日 = 1〜数リクエスト（ページネーション対応済み）。
- **`close` は nullable=False**: `Close` が `None` の行はスキップ（停止銘柄等）。
- **CardinalityViolation 対策**: 5桁コードが同じ4桁 sec_code にマップされる場合がある。INSERT前に edinet_code で重複排除（先着1件採用）。
- **ステータスコードだけ見て理由を推測しない**（#462）: J-Quants は 400 と 403 のどちらにも複数の意味を載せ、**区別できるのはボディの文言だけ**。#425 は `listed/info` の 403 を「無料プランの権限不足」と推測して docs に断定で書き、v2 での URL 変更を1年近く見落とした。#412 は境界の 403 という存在しない事象を前提にし、実際の契約失効（#461）を「平常運転」と読める警告で毎晩流し続けた。**新しいステータスに出会ったらボディを実測してから分類を足す**。

### FRED API（無料・要アカウント登録）

ADR-0002 §4 が求めるクレジット・インフレ・JP金利・期間構造の直交チャネルを取得する（#221）。

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| APIキー | **要無料アカウント登録** | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) でキー発行 → `FRED_API_KEY` 環境変数に設定 |
| レート制限 | **120 req/分** | `FRED_RATE_SLEEP = 0.6` 秒。11系列なら総所要 < 10秒 |
| 頻度混在 | 日次（HY/IG/BAA/BEI/T10Y2Y/EPU 2種）+ 月次（JP10Y_FRED）+ 四半期（JP_REAL_GDP/JP_TRADE_BAL） | 月次・四半期系列は期首日1レコードのみ保存。**低頻度系列は定義に `freq` を必ず明示する**——省くと `_GROUP_DEFAULT_FREQ["FRED_SERIES"]="daily"`（許容14日）で鮮度判定され、平常運転の公表ラグで毎晩 CRITICAL になる（#444 の実害）。`lag_days` を付けた系列は `trade_date` が後ろへシフトする＝保存日付は「期首+lag_days」 |
| 欠損値 | `"."` で返却 | `fetch_fred_series()` でスキップ |
| 認証未設定時 | `FRED_API_KEY=""` | `collect_macro_data()` が「FRED_API_KEY 未設定のためスキップ」でパスする（安全弁） |

**収集系列（`FRED_SERIES`）**:

| `series_code` | FRED series_id | チャネル | 頻度 |
|---|---|---|---|
| `HY_OAS` | `BAMLH0A0HYM2` | クレジット（HY・ICE BofA） | 日次 |
| `IG_OAS` | `BAMLC0A0CM` | クレジット（IG・ICE BofA） | 日次 |
| `BAA_SPREAD` | `BAA10Y` | クレジット（Baa−10Y・Moody's・非truncated） | 日次 |
| `BREAKEVEN10Y` | `T10YIE` | インフレ期待 | 日次 |
| `JP10Y_FRED` | `IRLTLT01JPM156N` | JP10年金利 | 月次（`lag_days=64` / `stale_days=62`・#444/#447） |
| `T10Y2Y` | `T10Y2Y` | 期間構造 | 日次 |
| `US_EPU` | `USEPUINDXD` | 政策不確実性（Baker-Bloom-Davis EPU・1985〜） | 日次 |
| `US_EQUITY_EPU` | `WLEMUINDXD` | 政策不確実性（株式市場関連・1985〜） | 日次 |

> **⚠️ ICE BofA 系列の履歴制限（#381）**: FRED は 2026-04 以降 ICE BofA 指数系列（`HY_OAS`=`BAMLH0A0HYM2` / `IG_OAS`=`BAMLC0A0CM`）を**ローリング3年窓に制限**し、2023年以前を配信しない（完全系列 1996〜 は ICE/Bloomberg/Refinitiv の商用ライセンスのみ）。`FRED_MIN_YEARS_BACK=10` があっても再収集で遡れないため、M-1/M-4 の**既定の信用ファクターは非ICE代替 `BAA_SPREAD`（`BAA10Y`＝Moody's Baa−10Y・truncate されず 2016 以前まで遡れる）へ移行済み**。`HY_OAS`/`IG_OAS` は選択肢としては残す（直近3年窓）が `DEFAULT_MACRO_FEATURES` からは除外（`plugins/macro_snapshots.py::_STRICT_TRUNCATED_FEATURES`）。詳細は [ADR-0016](adr/0016-ice-bofa-truncation-baa-credit-proxy.md)・GOTCHAS.md。

**✅ M-1 特徴量への公開完了（2026-06-24）**:
1. ~~`collect-macro.yml` を `workflow_dispatch` で実行~~ ✅ 実行済み
2. ~~Supabase でレコード数確認~~ ✅ 蓄積確認済み
3. ~~`plugins/macro_risk_return.py` の `_MACRO_MAP` と `MACRO_FEATURE_OPTIONS` のコメントアウト解除~~ ✅ `macro_snapshots.py` に統合済み（ADR-0003）

### 日銀 時系列統計 API（認証不要）

ADR-0006 §Decision-2 が定める M2・短観 DI チャネル。

| 項目 | 値 |
|---|---|
| エンドポイント | `https://www.stat-search.boj.or.jp/api/v1/getDataCode` |
| 認証 | 不要（常時収集） |
| 収集系列 | `JP_M2`（DB=MD02・月次）+ 短観 DI 4バリアント（DB=CO・四半期） |
| レート制限 | 非公開。`BOJ_RATE_SLEEP = 0.5` 秒。5系列で総所要 < 5秒 |

注: ADR-0006 は `api.boj.or.jp` と記したが実エンドポイントは `stat-search.boj.or.jp/api/v1`（GOTCHAS.md 参照）。

### OECD SDMX API（認証不要）

ADR-0009 が定める先行指標（leading indicator）チャネル。Issue #283。

| 項目 | 値 |
|---|---|
| エンドポイント | `https://sdmx.oecd.org/public/rest/data` |
| 認証 | 不要（匿名クエリのみサポート・APIキー無し。OECD公式ドキュメントで確認済み・2026-07-09） |
| 収集系列 | `JP_CLI`（日本 Composite Leading Indicator・振幅調整済・`OECD.SDD.STES,DSD_STES@DF_CLI,4.1` / `JPN.M.LI.IX._Z.AA.IX._Z.H`） |
| レート制限 | 非公開（「responsive experience維持のため導入」とのみ公式記載）。`OECD_RATE_SLEEP = 1.0` 秒で保守的に運用 |
| 応答形式 | `format=csvfilewithlabels`（CSV・`TIME_PERIOD`/`OBS_VALUE`列）。存在しない `series_key` は HTTP 404 + `NoRecordsFound` |
| 公表ラグ | CLI は対象月から2か月遅れで公表。先読みバイアス防止のため `lag_days=60`（e-Stat 鉱工業指数と同水準） |
| GitHub Actions 疎通 | Azure IP からのブロック事例は未確認（stooq のような事例は無い想定だが本番初回実行で要確認） |

### IMF WEO SDMX API（認証不要）

ADR-0011 が定める forward-looking（見通し）チャネル。Issue #284。

| 項目 | 値 |
|---|---|
| エンドポイント（継続収集） | `https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.RES/WEO/+/JPN.{indicator}.A` |
| エンドポイント（バックフィル） | `https://www.imf.org/external/pubs/ft/weo/data/WEOhistorical.xlsx`（IMF公式 point-in-time パネル・Spring1990〜Fall2022収録） |
| 認証 | 不要（匿名アクセス可。2026-07-11実API検証済み） |
| 収集系列 | `JP_WEO_GDP_FCAST`（実質GDP成長率見通し・翌年・indicator=`NGDP_RPCH`）・`JP_WEO_CPI_FCAST`（インフレ率見通し・翌年・indicator=`PCPIPCH`） |
| vintage先読みバイアス対策 | 現行dataflowは公式vintage境界と無関係に随時改定される（実証済み）ため、継続収集は必ず `trade_date=収集日` で固定。過去日付への割当はしない（詳細は GOTCHAS.md・ADR-0011） |
| bot保護回避 | `WEOhistorical.xlsx` はプレーンGETだと403だが `Range: bytes=0-` ヘッダー付きなら200/206で応答する（実証済み） |
| GitHub Actions 疎通 | Azure IP からのブロック事例は未確認（本番初回実行で要確認。特に `Range` ヘッダー workaround が有効か） |
| 既知の空白 | 2023年4月〜2025年4月の4vintage分はバックフィル・vintage archive双方に不在（復元不可・構造的空白として許容） |

### e-Stat API（総務省統計局・要アカウント登録）

ADR-0006 §Decision-1 が定める CPI チャネル。

| 項目 | 値 |
|---|---|
| エンドポイント | `https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData` |
| APIキー | [e-stat.go.jp/api/](https://www.e-stat.go.jp/api/) で無料登録 → `ESTAT_API_KEY` 環境変数に設定 |
| 収集系列 | `JP_CPI_TOTAL`・`JP_CPI_CORE`（全国コア・非季調）・`JP_CPI_TOKYO`（statsDataId=0003427113） |
| 認証未設定時 | `collect_macro_data()` が「ESTAT_API_KEY 未設定のためスキップ」でパスする（安全弁） |

**鉱工業指数チャネル（`ESTAT_INDEX_SERIES`・#253/#281）**: `JP_IP`（FRED系列 `JPNPROINDMISMEI`）が
2024-04-30で凍結した代替として、経済産業省「鉱工業指数」を e-Stat から直接取得する。

| 項目 | 値 |
|---|---|
| 統計表 | 鉱工業生産・出荷・在庫指数 2020年基準時系列データ（2018年1月～・業種別季節調整済指数【月次】） |
| 収集系列 | `JP_IIP`（`statsDataId=0004052177`・生産）・`JP_IIP_INVENTORY`（`statsDataId=0004052179`・在庫） |
| 絞込パラメータ | `cdCat01=0001000`（業種別分類「鉱工業総合」）。CPI と異なり `cdTab`/`cdArea`/`lvTime` は無い（表章項目・地域軸を持たないテーブル） |
| time 軸の特殊性 | `@time` が `"0500100"` 等の連番コードで年月を直接表現しない（CPI は自己記述コードで直接パース可）。`metaGetFlg="Y"` で同一レスポンスに `CLASS_INF`（code→"YYYYMM"）を同梱させ、`fetch_estat_index_series` が変換する |
| 基準改定リスク | 鉱工業指数は基準改定（2010→2015→2020年基準）のたびに `statsDataId` が別テーブルへ切替り、旧テーブルは更新停止する（JP_IP 凍結と同根）。次回改定時は本節の `statsDataId` を再調査すること |

> **⚠ 2026-08-04: e-Stat 側が停止していることが判明（#451・ADR-0028）**
>
> 統計表 0004052177 / 0004052179 は `UPDATED_DATE=2026-06-03` を最後に更新されず、収録は
> **2026年3月分**（`trade_date=2026-04-30`）まで。経産省サイトでは6月分が 2026-07-31 に公表済み。
> 実 API で統計名 `00550300` 配下の全162表を確認したところ、更新日は **2026-06-03 / 2024-07-05 /
> 2021-09-08 の3つしか無く**（各54表＝基準世代ごと）、**月次更新の痕跡が世代を通じて存在しない**
> （世代間隔 約23か月）。上表の「基準改定リスク」より深刻で、**e-Stat 経由では月次追随できない**
> のが実態だった。収集側は無実（API が返す 99 行を全て取得できている）。
>
> 対応: 昇格ゲート実測（3,981社・67ヶ月・91,482サンプル・17 fold）で**4検定すべて非有意**
> （M-2 rank-IC −0.0027 / M-2 spread −0.0008 / M-6 rank-IC +0.0001 / M-6 spread −0.0017）
> だったため既定特徴量から棄却（`_GATE_REJECTED_FEATURES`）、鮮度判定は
> `macro_health.EXCLUDED_SERIES` へ退避。**収集は継続**（e-Stat がいずれ更新すれば貯まる）。
> 代替の経産省直接CSVは、効いていない特徴量に ESRI 級の工数を投じる根拠が無いため実装しない。
> OECD SDMX も代替にならない（`DSD_STES@DF_INDSERV` に在庫指数が無く、生産も最新 2026-04 で
> lag ≒96日＝`lag_days=60` のまま差し替えると新たな先読みを作る）。

### GDELT DOC 2.0 API（認証不要・#406/ADR-0024）

ニューストーン（記事の極性）・報道量チャネル。**マクロ集約系列のみ**（銘柄別日次は 4,000社 ×
250営業日 ≈ 370MB/年で Supabase 無料枠に入らない＝構造的に不可。再提案しないこと）。

| 項目 | 値 |
|---|---|
| エンドポイント | `https://api.gdeltproject.org/api/v2/doc/doc`（`mode=timelinetone` / `timelinevol`・`format=json`） |
| 認証 | 不要 |
| 収集系列 | `JP_NEWS_TONE`・`JP_NEWS_ECON_TONE`・`JP_NEWS_ECON_VOL`（いずれも `sourcecountry:japan`、後2者は `theme:ECON_STOCKMARKET` で絞り込み） |
| 履歴 | **2017-01-01 開始**（`GDELT_START`）。それ以前を `startdatetime` に渡すと `Invalid query start date` |
| ページング | 不要。全期間（3,473日）を1リクエストで日次のまま返す（間引きなし・2026-07-31 実測） |
| レート制限 | **1リクエスト/5秒**。`GDELT_RATE_SLEEP = 6.0` 秒＋`GDELT_RETRIES = 3` で運用 |
| 超過時の応答 | **HTTP 200 のままプレーンテキストの警告本文**（JSON ではない）。ステータスコードでは検知できないため本文が `{` で始まるかで判定する |
| GitHub Actions 疎通 | 未確認（本番初回実行で要確認。レート制限は共有 IP 単位の可能性がある） |

### Wikimedia Pageviews API（認証不要・#406/ADR-0024）

一般大衆の関心度チャネル（ja.wikipedia の記事別日次閲覧数）。

| 項目 | 値 |
|---|---|
| エンドポイント | `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/ja.wikipedia/all-access/all-agents/{article}/daily/{from}/{to}` |
| 認証 | 不要。ただし **User-Agent に連絡先（URL かメール）が必須**——欠くと 403 `Please respect our robot policy`（`WIKIMEDIA_UA` にリポジトリ URL を設定済み） |
| 収集系列 | `JP_WIKI_MARKET_ATTN`（日経平均株価・東京証券取引所）・`JP_WIKI_MACRO_ATTN`（景気後退・インフレーション・日本銀行・金融政策）＝**記事バスケットの合算** |
| 履歴 | 2015-07-01 開始（`WIKIMEDIA_START`）。全期間が1リクエストで返る |
| レート制限 | 明示上限なし（robot policy 準拠）。`WIKIMEDIA_RATE_SLEEP = 1.0` 秒で保守的に運用 |
| 欠測・404 | 欠測日は 0 埋めせず合算から除外。存在しない記事（404）はその記事だけ落として残りで合算（graceful skip） |

**容量**: GDELT 3系列＋Wikimedia 2系列 ≈ 1.9万行 × 370 bytes ≈ **約7MB**（2026-07-30 時点の
ヘッドルーム 174MB の約4%）。

---

## 環境変数（Render ダッシュボードで設定）

`render.yaml` で `sync: false` のキーは Render ダッシュボードで手動設定する。
`generateValue: true` は Render が自動生成。

| キー | 用途 | デフォルト |
|---|---|---|
| `DATABASE_URL` | Supabase PostgreSQL 接続 URL | 手動設定（`postgresql://...?sslmode=require`） |
| `EDINET_API_KEY` | 金融庁 EDINET API キー | 手動設定 |
| `JQUANTS_API_KEY` | J-Quants API キー（任意） | 手動設定 |
| `FRED_API_KEY` | FRED（米セントルイス連銀）API キー（任意） | 手動設定。未設定時はマクロ収集の FRED チャネルをスキップ |
| `ESTAT_API_KEY` | 政府統計 e-Stat API キー（任意・CPI 収集用） | [e-stat.go.jp/api/](https://www.e-stat.go.jp/api/) で無料登録。未設定時は e-Stat チャネルをスキップ |
| `APP_PASSWORD` | ログインパスワード（初期値） | 手動設定（必須）。リセット後は `app_settings` テーブルの値が優先（Render 再起動後も永続） |
| `APP_SECRET_KEY` | トークン署名キー（HMAC） | Render 自動生成 |
| `APP_RECOVERY_KEY` | パスワードリセット用 | 手動設定 |
| `ALLOWED_ORIGIN` | CORS 許可オリジン | 手動設定（例: `https://<your-service>.onrender.com`） |
| `RENDER_LIGHT_MODE` | 重い操作をブロック（`"true"` 固定） | `render.yaml` に設定済み |

新規環境変数を追加するときは:
1. `render.yaml` の `envVars` に追記
2. Render ダッシュボードで値を設定
3. 自動再デプロイ

---

## 新機能を実装するときの設計原則

### ✅ Render と相性が良いパターン

- **DB マイグレーション**: `init_db()` で冪等に実行（既存パターン）。起動時に必要なら自動実行
- **環境変数からの設定**: `os.getenv("FOO", "default")` で全て吸収
- **長時間処理は BackgroundTasks**: ユーザーリクエストは即返し、`/api/*/stream` で SSE 進捗配信
- **Pure Python or `numpy`/`scipy` 等の wheel 配信ライブラリ**: ビルド時の問題が起きにくい
- **ヘルスチェック**: `GET /health`（実装済み）を Render が監視に使える

### ❌ Render Free で避けるべき設計

- **永続ローカルファイル**: ディスクは再デプロイで消える。必ず DB か外部ストレージへ
- **重い同期処理を 1 リクエストに詰め込む**: 30 秒超のリクエストはタイムアウト。SSE で進捗を返す
- **常時稼働前提のクロン**: 15 分アイドルで停止するため、上記の (A)〜(C) で対応
- **SSH 経由のメンテナンス**: できない。すべてコード or 環境変数で制御
- **C 拡張のソースビルドが必要なパッケージ**: ビルド時間オーバーになりやすい。wheel 配信があるものを優先

### 🔄 残課題タスクの Render 適合性

`docs/FUTURE_TASKS.md` 記載の残課題を Render 前提で再評価:

| 項目 | Render での実装方針 |
|---|---|
| **G**: J-Quants IssuedShares 取得 | ✅ **実装済み（Tier2-G・PR #181・2026-06-16）**。`Company.issued_shares` 追加 + `_ensure_tables()` の冪等 ALTER + J-Quants `/v2/markets/listed/info` から取得 |
| **H**: `period_end` を DATE 型に | ✅ **実装済み（Tier2-H・PR #182・2026-06-16）**。`init_db()` 内の冪等 DDL（`USING ...::DATE`・`SKIP_PERIOD_END_MIGRATION=1` フェールセーフ）で起動時 1 度だけ移行 |
| **F**: HttpOnly Cookie 認証 | ✅ **実装済み（Tier3-3）**。`auth_token`（HttpOnly）＋`csrf_token` の2 Cookie + CSRF Double-Submit。本番は `COOKIE_SECURE=true` |
| **E**: 本番デプロイ対応 | **大部分が完了済み**。残るのは Supabase の DB バックアップ運用ポリシー策定（Supabase の自動バックアップ機能を利用）と監視（Render ダッシュボード + UptimeRobot 等） |

---

## 既知の運用 Tips

### スピンダウン対策

**現状: 対策なし（許容）**

収集を `.github/workflows/daily-incremental.yml`（差分・自動）と `full-pipeline.yml`（全件・手動）
に統一したため、Render を常時起動させる必要がなくなった。ユーザーが Web UI を開いた
ときだけスピンアップする運用で、コールドスタート（数秒〜数十秒）は許容する。

**過去の対策（廃止済み、参考）:**
- `.github/workflows/keepalive.yml` で `/health` を定期 ping し Render を起こす方式
- `api.py:_startup_catchup` でスピンアップ時に最終収集から 22h 経過していたら差分収集を走らせる方式
- `api.py:_daily_scheduler` で JST 3 時に Render 上で差分収集を走らせる方式

いずれも Render Free のメモリ/タイムアウト制約と相性が悪く、本格運用に耐えなかった。

**将来、Web UI のコールドスタートを避けたい場合の選択肢:**
1. 有料プラン ($7/月) で常時稼働
2. 外部 cron-as-a-service (cron-job.org / UptimeRobot 等) で `/health` を定期 ping

#### UptimeRobot による外形監視（設定手順）

`/health` は DB 死活込みで実装済み（200 = `{"status":"ok","db":"ok"}` / 503 = `{"status":"degraded","db":"error"}`）。
[UptimeRobot 無料プラン](https://uptimerobot.com/) で以下の通り設定する（登録・モニタ作成はユーザー操作）:

| 項目 | 値 |
|---|---|
| Monitor type | HTTP(s) |
| **監視 URL** | `https://financial-app-l2r7.onrender.com/health` |
| **チェック間隔** | 5 分（スピンアップ兼用・無料プラン最小粒度）／純粋な死活監視のみなら 15 分でも可 |
| **期待ステータス** | `200` = 正常 ／ `200` 以外（特に `503`）= 異常アラート |
| **アラート先** | 登録メールアドレス |

### ログ閲覧
Render ダッシュボード → 該当サービス → "Logs" タブで stdout/stderr をストリーミング閲覧。
`log.error("...", exc_info=True)` がそのまま見える。

### ロールバック
"Manual Deploy" → "Deploy from previous commit" で過去コミットへ即座に戻せる。
DB マイグレーションを含む変更はロールバック時の整合性に注意。

### 設定変更後の反映
- `render.yaml` を変更 → push で自動再デプロイ
- Render ダッシュボードの環境変数のみ変更 → 「Save, Rebuild and Deploy」ボタンで反映

---

## このファイルの位置づけ

CLAUDE.md からも参照される。新セッションで Claude がデプロイ環境を把握できるよう、
**Render 前提の設計判断・運用方針はここに集約する**。`docs/ARCHITECTURE.md` セクション 9
（デプロイ構成図）は本ファイルへリンクする形で簡素化済み。

---

## DB 構成の履歴

開発初期に存在した「開発者 PC のローカル PostgreSQL」は廃止し、Render と同じ
**Supabase PostgreSQL** に一本化済み（2026年完了）。現在はローカル開発・Render 本番
ともに `DATABASE_URL`（Supabase）を共有する。移行・切り戻し手順の詳細は
[`docs/archive/REFACTORING.md`](archive/REFACTORING.md) と git 履歴を参照。
