# Render デプロイ運用ガイド

本プロジェクトは **Render** にデプロイ済みで稼働している。今後の改修・新機能は
Render の制約と運用形態に合わせて設計すること。

最終更新: 2026-06-24

---

## データ収集の仕組み（自動 vs 手動）

### 自動実行 vs 手動実行の整理

| 種別 | 処理内容 | 実行タイミング | 実行場所 |
|---|---|---|---|
| **自動（毎日）** | 差分収集（新規書類 + 株価更新） | UTC 18:00（JST 03:00）毎日 | GitHub Actions `daily-incremental.yml` |
| **自動（毎日・チェーン）** | 夜間スコア更新（`sector_ols` → `regression_results`、M-6 → `macro_enet_scores`） | `daily-incremental` が **success** で終わった直後（`workflow_run`） | GitHub Actions `nightly-scores.yml` |
| **自動（毎月）** | M-1/M-2/M-3 ハイパーパラメータ探索・永続化 | UTC 03:00（JST 12:00）毎月1日 | GitHub Actions `tune-hyperparameters.yml` |
| **自動（毎月）** | M-1 per-stock 階層マクロβ推論・永続化（producer） | UTC 11:00（JST 20:00）毎月1日 | GitHub Actions `macro-beta-inference.yml` |
| **自動（毎週）** | `stock_price_daily` の VACUUM FULL（index bloat 対策） | UTC 22:00・土（JST 07:00・日） | GitHub Actions `vacuum-maintenance.yml` |
| **手動のみ** | 全件収集（全社 × 5年分） | workflow_dispatch で起動 | GitHub Actions `full-pipeline.yml` |
| **手動のみ** | マクロのみ収集（為替・金利等） | workflow_dispatch で起動 | GitHub Actions `collect-macro.yml` |
| **手動のみ** | 会社予想開示収集（J-Quants /fins/summary） | workflow_dispatch で起動 | GitHub Actions `collect-disclosures.yml` |
| **手動のみ** | 半期(H1)財務収集（EDINET 半期/旧四半期Q2） | workflow_dispatch で起動 | GitHub Actions `collect-interim.yml` |
| **手動のみ** | recommend Fama-MacBeth ファクタープレミアム推定・永続化（producer） | workflow_dispatch で起動 | GitHub Actions `recommend-factor-premia.yml` |
| **手動のみ（アーカイブ）** | bs_inventory 補完 | workflow_dispatch で起動 | GitHub Actions `old/` 配下（一回性・完了済み） |
| **UIから手動** | 差分収集・株価更新 | ユーザーがボタン押下 | Render Web UI |
| **自動（CI）** | `pytest` 回帰テスト（Secrets・本番DB非依存） | PR / main への push | GitHub Actions `ci.yml` |
| **自動（イベント）** | 他ワークフローの failure / cancelled を Issue 化 | 対象ワークフロー完了時（`workflow_run`） | GitHub Actions `notify-failure.yml` |

### GitHub Actions workflow 早見表（いつ・何を・どれを使うか）

#### アクティブ（`.github/workflows/` 直下・Actions 対象）

| カテゴリ | workflow 名 | ファイル | 使うタイミング | 所要時間の目安 |
|---|---|---|---|---|
| `[CI]` | pytest 自動テスト | `ci.yml` | PR・main push で自動実行（手動起動不要） | 〜1分 |
| `[定常]` | 差分収集・毎日自動実行 | `daily-incremental.yml` | 毎日 JST 03:00 に自動。手動で即時更新したい場合は `workflow_dispatch` | **2h05m〜2h38m**（2026-08-02 実測） |
| `[全件]` | XBRL収集・財務データ全件更新 | `full-pipeline.yml` | DB初期構築時・全社バックフィル必要時（`daily-incremental` を `.disabled` に退避して同時実行回避） | 200〜240分 |
| `[補完]` | マクロのみ収集 | `collect-macro.yml` | `MACRO_SERIES`（為替・金利・指数・コモディティ・ボラ）を Yahoo から収集。新規系列追加や macro_data の鮮度補完。`workflow_dispatch`（years 既定5）。**新系列のバックフィルは years=6 で起動**（yoy は1年+30日で足りるが、将来 zscore 版追加時に再バックフィル不要な余裕幅。#358 コモディティ8系列追加時の運用） | 〜数分 |
| `[推論]` | M-1 per-stock 階層マクロβ推論（producer） | `macro-beta-inference.yml` | ADR-0002 の PyMC 階層ベイズ推論バッチ（`macro_beta_inference.py`）→ `macro_beta_loadings`/`macro_beta_meta` へ永続化（M-1 `macro_risk_return` が consumer）。本番 `requirements.txt` ではなく `requirements-inference.txt`（+PyMC）を使用。**毎月1日 UTC 11:00（JST 20:00）自動**（Issue #341・鮮度が人力任せで滞留した反省。`tune`(UTC 03:00〜)・`daily-incremental`(UTC 18:00〜)と非重複の時間帯）。手動即時実行は `workflow_dispatch`（draws/tune/target_accept/chains/r_hat_threshold/force 指定可・既定 800/800/0.95/2/1.05/false）。**収束ゲートは `--r-hat-threshold` で可変化**（Issue #341）＝ADR-0002 strict 基準は 1.01 だが、chains=2 では r_hat が構造的に ~1.02 で頭打ち（実 persist 済み 2026-07-04 run も r_hat_max=1.02・n_divergences=0）のため cron 既定を 1.05 とし、構造的 ~1.02 は自動 persist しつつ真の未収束（r_hat が 1.05 を大きく超過）は persist せず失敗させる。閾値を緩めても足りない例外運用時のみ `force=true` | 本番規模で最大 340分（`timeout-minutes: 340`・numpyro で実測 10.5〜11.2秒/draw。ローカル検証: 4銘柄合成データ・draws/tune=50・chains=2・g++無しの Python フォールバックで約8分） |
| `[定常]` | 夜間スコア更新（`sector_ols` + M-6） | `nightly-scores.yml` | `nightly_scores.py`（Issue #432/#443・親 #423）を実行し、①`sector_ols` → `regression_results`（`predicted_market_cap` / `gap_ratio`）②`macro_enet`（M-6）→ `macro_enet_scores`（μ̂・`sell_ranking` の**既定** mu_source）を更新する。**起動は `daily-incremental` の `workflow_run` チェーンで `conclusion == 'success'` のときだけ**（株価が前進していない日にスコアだけ更新すると、古い株価由来の値が「今日のランキング」として出るため）。`sector_ols` は `regularization=ridge` 固定（既定 features 10項目は VIF>10 が頻発）、M-6 は params_schema の既定のまま（ADR-0021/0022 の実測と同一構成）。1モデルの失敗は他を巻き込まず、実行後に `max(computed_at)` / `max(created_at)` を直接クエリして永続化を確認する（例外なし＝コミット済みとしない）。モデル間の `load_data`（週次127万行）は `shared_snapshot_cache()` で共有し、Egress がモデル数に比例しないようにしている。手動即時実行は `workflow_dispatch` | `sector_ols` は **16.1分**（2026-08-03 本番実走・run 30808053564・30業種/2,837社）。M-6 追加後の総所要は**未実測**（`timeout-minutes: 150` は安全側の暫定値＝初回実走で締める） |
| `[定常]` | M-1/M-2/M-3 ハイパーパラメータ月次自動探索 | `tune-hyperparameters.yml` | `hyperparameter_search.py`（Issue #264/#278/#291）を matrix strategy で3モデル並列実行し `plugin_tuned_params` へ永続化（Issue #292）。`macro_risk_return`/`macro_dlm` は `--strategy grid`、`macro_gbdt` は `--strategy random --n-iter 150`（6時間上限に収める設計判断）。共通 `--objective rank_ic --persist --persist-scores --seed 0`。品質ゲート（#291）でスコア劣化時は該当ジョブが failed 終了（意図した挙動）。毎月1日 UTC 03:00（JST 12:00）自動。手動即時実行は `workflow_dispatch` | macro_risk_return/macro_dlm: 10〜60分、macro_gbdt: 4〜8時間相当を n_iter=150 で圧縮（timeout-minutes: 355） |
| `[補完]` | 半期(H1)財務収集 | `collect-interim.yml` | EDINET 半期報告書（043A00/docType160）と旧四半期報告書（043000/docType140）の Q2(中間=H1累計)を収集し `financial_records` に `period_type='H1'` で保存（Issue #219② フェーズB）。通期収集とは独立・常に差分（収集済み doc_id をスキップ）。`workflow_dispatch`（years_back 既定6＝既存通期窓に整合）。240分に収まらない場合は years_back を分割 | 数時間（過去6年・事前選別でQ1/Q3を除外し概ね1社1半期1DL） |
| `[推論]` | recommend Fama-MacBeth ファクタープレミアム推定（producer） | `recommend-factor-premia.yml` | `recommend_factor_premia.py --persist`（Issue #271/#342・ADR-0008）を実行し、月次断面 OLS（Fama & MacBeth 1973・Newey-West HAC）で推定したファクタープレミアムを `recommend_factor_premia` テーブルへ永続化（`plugins.recommend.resolve_weights()` が「統計的最適化」プリセットとして読む consumer）。依存は `requirements.txt` で充足（PyMC 不要）。**当面 `workflow_dispatch` のみ**（cron 要否は別途判断・Issue #342）。`min_companies_per_period`（既定30）・`maxlags`（既定11）指定可。MCMC のような収束ゲートは無し（断面 OLS は決定的） | 未計測（`timeout-minutes: 120`。週次株価 build_snapshots ロード＋月次断面 OLS。計算自体は MCMC より遥かに軽量） |
| `[定常]` | マクロ鮮度ゲート | `macro-health.yml` | `python -m scripts.check_macro_health`（Issue #420）が `macro_data` の系列別 `max(trade_date)` を期待更新頻度（`macro_health.FREQ_STALE_DAYS`）と突き合わせ、**既定モデルが使う系列**（`DEFAULT_MACRO_FEATURES` から逆引き）が古ければ exit 2 → `notify-failure` が Issue 起票。`collect_macro_data` は 1 系列失敗しても `continue` するため部分失敗が exit 0 で通り、#414 の失敗通知では拾えないのを塞ぐ。**収集本体（`daily-incremental` / `full-pipeline`）を落とさず独立ジョブに分離しているのが要点**——あちらを failure にすると `nightly-scores` の `workflow_run` チェーン（`success` 条件）が発火せず、マクロと無関係な `sector_ols` の夜間更新まで巻き添えで止まる（#425 の構造をワークフロー間へ適用）。収集側は同じレポートを run ログに出すだけ。誤検知が続く系列は `macro_health.EXCLUDED_SERIES` へ**理由付きで**登録する（現在: `JP_IP`＝FRED 凍結 #253／`JP10Y`＝Yahoo ^JGB 廃止／`BCOM`＝Yahoo 配信停止 #438） | 〜2分（GROUP BY 集約1本・`timeout-minutes: 10`） |
| `[定常]` | ワークフロー失敗の自動 Issue 起票 | `notify-failure.yml` | 上記ワークフロー（`ci.yml` を除く全本数・列挙しない設計）＋セルフテストが `failure` または `cancelled` で終わると自動起票（`workflow_run`）。手動起動しない。詳細は下記「ワークフロー失敗の通知」節 | 〜1分 |
| `[検証]` | notify-failure セルフテスト | `notify-failure-selftest.yml` | `notify-failure.yml` の変更後に発火を実証するための、意図的に失敗するだけのワークフロー（本番データ不使用）。`workflow_dispatch`（`mode=fail`／`mode=cancel`） | 〜1分（cancel は約1分） |
| `[定常]` | DBメンテナンス（VACUUM FULL・週次） | `vacuum-maintenance.yml` | `stock_price_daily` の DELETE ベース trim による index bloat 対策（Issue #290）。`_pipeline_vacuum.py` が AUTOCOMMIT 接続で `VACUUM FULL stock_price_daily` を実行、前後の容量をログ出力。毎週 UTC 22:00・土（JST 07:00・日）自動。手動即時実行は `workflow_dispatch`（ローカル・GitHub Actions 双方で Supabase pooler 経由の正常動作を確認済み・2026-07-12）。**時間帯は #427 で JST 04:00 → 07:00 へ移動**——差分収集（JST 03:00 開始・実測 2h05m〜2h38m）の最中に `VACUUM FULL`（ACCESS EXCLUSIVE ロック）が走っており、ずらす設計意図が成立していなかった。現行チェーンは 03:00 収集 → 最長 05:40 → nightly-scores（`sector_ols` 16分 + M-6・総所要は #443 の初回実走で実測）。**M-6 追加でチェーン後端が伸びるため、日曜だけは VACUUM FULL（07:00）と重なりうる**——ただし `VACUUM FULL` が排他ロックを取るのは `stock_price_daily` のみで、夜間バッチが読むのは `stock_price_weekly`／`financial_metrics` ゆえロック競合はしない（I/O は共有）。実測で 07:00 に食い込むようなら時間帯を再調整する | 数秒〜数分（対象テーブルは実測 ~50MB・42万行） |

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
| 発火条件 | `conclusion == 'failure'` **または `'cancelled'`**。timeout 打ち切りは failure ではなく **cancelled** で終わるため両方必須（実例: `tune-hyperparameters` 300分・`collect-interim` 4h） |
| 起票内容 | タイトル `[ops] ワークフロー失敗: <workflow name>`／ラベル `ops` `priority:high` `ci`／本文に run URL・発火イベント・ブランチ・**失敗ジョブ名**・**失敗ステップのログ末尾30行** |
| 重複防止 | 同一タイトルの open Issue があれば新規起票せず**コメント追記** |
| 権限 | `issues: write` は本ワークフローのみ。収集系の `contents: read` 最小権限は崩さない。ログ取得のため `actions: read` |

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
```

`notify-failure.yml` を変更したら main 反映後にこれを1回流し、Issue が起票される（2回目以降はコメント追記になる）ことを確認する。確認後は起票された Issue をクローズすること。

### daily-incremental の動作詳細

毎日 UTC 18:00 に自動起動する `_pipeline_incremental.py` は:
1. EDINET API の書類一覧を **前年1月1日〜前日**の全日スキャンで取得し（`years_back=1`・`collector_financials.py` Phase 3）、未収集 `doc_id` のみ XBRL 収集・DB保存。「60日以内」は旧実装の記述で実態と乖離していた（#422）
2. 株価更新（Phase 4）: **①Yahoo Finance が銘柄ごとに `その社の最終日+1 〜 today` をギャップ補完**（`fill_recent_stock_price_gap_yahoo`）→ **②J-Quants catchup（`today-90`〜`today-80`）で Yahoo 暫定値を公式値へ置換** → ③`update_market_data_from_history`。<br>**J-Quants 無料は直近84日（12週）を配信しない**ため、旧実装の `days_back=14` はエンバーゴ内で構造的に常に0件かつ全日403となり、中断ガードを誤発火させて Yahoo 補完まで巻き添えで止めていた（#419 / #425）。撤去済み。鮮度を担う Yahoo を先に置き、J-Quants catchup の失敗は握って継続する（片方の収集元の障害がもう片方を止めない）。鮮度はこの Yahoo 補完が担う（J-Quants 制約表の「配信遅延」行を参照）。<br>起点は**銘柄別**（Issue #415）。全社横断の最大日を1つ選んで全社へ適用すると、一部銘柄だけ先行して復旧した場合に遅延銘柄の欠測が永久に埋まらない（2026-07 に発生。2銘柄が 07-31 / 3,677銘柄が 07-13 の状態で 14営業日分が穴のまま残った）。起点は daily 保持窓（`DAILY_WINDOW_DAYS`）でクリップし、それ以前は `backfill-weekly.yml` の管轄とする
3. 成長率・Zスコアを再計算
4. 所要時間: **2h05m〜2h38m**（2026-08-02 の success run 2本で実測。大半は EDINET 全日スキャンと Yahoo ギャップ補完の逐次リクエスト。`timeout-minutes: 360`）

> **注（運用パターン）**: 全件収集（`full-pipeline.yml`）を回している間は、Supabase 接続上限での同時実行を避けるため、本ワークフローを一時的に `daily-incremental.yml.disabled` へリネームして停止する（例: コミット `4764d96`「全件収集中の同時実行回避」）。全件収集が終わったら `.yml` に戻して再有効化する。**現在ファイル名が `.disabled` の場合は自動の定時収集が止まっている状態**なので、UI / 手動収集で補う。<br>停止中の株価鮮度は `full-pipeline` の finalize（Phase 5）が同じ Yahoo ギャップ補完を持つため、全件収集の完了時点で追いつく（#426）。それでも collect フェーズ中（実測 約4時間）は前進しない点は変わらない。
>
> **✅ cron 再開済み（2026-06-22〜）**: dual-table 移行後の `workflow_dispatch` 手動実行で Yahoo ギャップ補完・J-Quants 株価取得が GitHub Actions（Azure IP）から正常動作することを確認 → `on.schedule` を有効化。UTC 18:00（JST 03:00）に毎日起動する。
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
- `full-pipeline.yml` finalize（Phase 3〜5）: **250〜300分**（`timeout-minutes: 355`）。内訳 = 成長率/Zスコア再計算 約2分 ／ マクロ13系列 約27分→系列数に概ね比例（#218 で 9→13・#358 でコモディティ+8＝市場系21系列・要再計測。Yahoo 市場系は1コール/系列のため増分は数十秒で低頻度ソースの sleep が主因は不変） ／ **Yahoo ギャップ補完 35〜60分（#426 で追加・約4,000社 × `YAHOO_STOCK_RATE_SLEEP=0.5秒`）** ／ J-Quants 株価（`JQUANTS_BACKFILL_DAYS=730`）約163〜200分。`JQUANTS_BACKFILL_DAYS` 変更時は再計算。
- `backfill-stock-history.yml`: 対象＝stock_price NULL かつ period_end 730日超前（初回 約3,800社）。`YAHOO_STOCK_RATE_SLEEP=0.5秒`・1社1リクエストで **約60〜90分**（`timeout-minutes: 150`）。
- `backfill-weekly-history.yml`（#198）: 対象＝`stock_price_weekly` の最古日が `today-years` より新しい社。`backfill_weekly_history_yahoo` が Yahoo から過去方向に取得し、**1社ごとに `record_prices_batch(trim=True)`** で daily→weekly 再集約しつつ daily を都度 trim する（5年×全社の daily 同時展開を避け Supabase 500MB を超えない）。`YAHOO_STOCK_RATE_SLEEP=0.5秒`で **約60〜150分**（`timeout-minutes: 150`）。

### Supabase（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| DB ストレージ | **500 MB** | `SKIP_XBRL_RAW=true` を維持＋株価は close-only 2本立て（下記「容量設計」） |
| 接続数 | **最大60接続**（pgbouncer 経由） | 並列パイプライン実行を禁止。`max-parallel: 1` を維持 |
| 一時的 read-only 移行 | トランザクションが長すぎると自動移行 | `run_full_collection` は `MASTER_BATCH=200` 件ごとに commit |
| プロジェクト停止 | **1週間アクセスなしで自動停止** | 長期不使用時は要注意 |

#### 容量設計（株価 = 最大の消費者）

旧 `stock_price_history`（日次OHLCV全履歴）が約 359MB / 全体80% を占め、年約220MB で増加して 500MB 上限の主犯だった。**close-only の2本立て**へ移行して恒久対策とする：

- **`stock_price_daily`**：直近 `DAILY_WINDOW_DAYS`（≒6か月）の日次終値のみ。収集のたびにローリング削除（trim）でサイズが頭打ち……のはずだが、DELETE ベースの trim は btree インデックス（`pk_stock_price_daily`/`ix_spd_trade_date`）を bloat させ続け、autovacuum は死領域をテーブル内で再利用するのみでファイルサイズは縮まない（**Issue #290**・実測: 2026-07-09 VACUUM FULL 直後 48MB→3日後 72MB）。週次 `vacuum-maintenance.yml`（VACUUM FULL・毎週自動実行）で物理サイズを頭打ちにする。チャートの日次ズーム・短期バックテスト用。
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
| 429 リトライ | 指数バックオフ禁止 | 429 発生時は **90秒待機→1回のみ再試行**。失敗したら skip |
| 営業日データのみ | 土日祝は空レスポンス | 空レスポンスを skip として扱う |
| **カバレッジ境界の 403**（#412 / #425） | 過去側の実効境界（730日前付近）も直近84日エンバーゴ内も 400 ではなく **403** を返す | 403 は `JQuantsCoverageError` として送出し、**日付ループ側で欠測扱い（warning ログ）＋継続**。例外を伝播させると `days_back=730` の初日で必ず落ち、finalize 全体が異常終了する。<br>**全日程403でも例外は投げない**（#425）: `log.error` ＋ 戻り値 `all_forbidden: True` で返し、呼び出し側が継続可否を決める。J-Quants の失敗が、同じキーに依存しない Yahoo ギャップ補完まで巻き添えで止めるのを防ぐため |
| **`/markets/listed/info` は常に 403**（#425） | 無料プランに権限が無い | 実測（2026-07-09/12/13 の **success** run すべてに `listed/info 取得失敗 status=403`）。したがって `active_codes` は常に空で、**キー有効性の判定材料に使えない**。#412 の「全日403 かつ listed/info 失敗ならキー失効」ガードはこの前提が成立せず、エンバーゴ内の窓では構造的に必ず発火していた。`issued_shares` 更新と `sync_active_status`（#315）も無料プランでは常にスキップされる |

**設計制約（実装時の必須ルール）**:
- **認証情報**: `.env` に `JQUANTS_API_KEY` を設定。未設定時は `ValueError` で明示エラー。
- **データ優先度**: J-Quants = JPX公式 → stooq より正確。`ON CONFLICT DO UPDATE` で上書き（stooq は `ON CONFLICT DO NOTHING`）。
- **コード変換**: J-Quants は5桁コード（例 `"13010"`）。先頭4桁が証券コード（`code[:4]`）。
- **取得単位**: 日付単位で全銘柄を一括取得。1営業日 = 1〜数リクエスト（ページネーション対応済み）。
- **`close` は nullable=False**: `Close` が `None` の行はスキップ（停止銘柄等）。
- **CardinalityViolation 対策**: 5桁コードが同じ4桁 sec_code にマップされる場合がある。INSERT前に edinet_code で重複排除（先着1件採用）。

### FRED API（無料・要アカウント登録）

ADR-0002 §4 が求めるクレジット・インフレ・JP金利・期間構造の直交チャネルを取得する（#221）。

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| APIキー | **要無料アカウント登録** | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) でキー発行 → `FRED_API_KEY` 環境変数に設定 |
| レート制限 | **120 req/分** | `FRED_RATE_SLEEP = 0.6` 秒。11系列なら総所要 < 10秒 |
| 頻度混在 | 日次（HY/IG/BAA/BEI/T10Y2Y/EPU 2種）+ 月次（JP10Y_FRED） | 月次系列は月初日1レコードのみ保存。M-1 の zscore 計算で年次集計するため支障なし |
| 欠損値 | `"."` で返却 | `fetch_fred_series()` でスキップ |
| 認証未設定時 | `FRED_API_KEY=""` | `collect_macro_data()` が「FRED_API_KEY 未設定のためスキップ」でパスする（安全弁） |

**収集系列（`FRED_SERIES`）**:

| `series_code` | FRED series_id | チャネル | 頻度 |
|---|---|---|---|
| `HY_OAS` | `BAMLH0A0HYM2` | クレジット（HY・ICE BofA） | 日次 |
| `IG_OAS` | `BAMLC0A0CM` | クレジット（IG・ICE BofA） | 日次 |
| `BAA_SPREAD` | `BAA10Y` | クレジット（Baa−10Y・Moody's・非truncated） | 日次 |
| `BREAKEVEN10Y` | `T10YIE` | インフレ期待 | 日次 |
| `JP10Y_FRED` | `IRLTLT01JPM156N` | JP10年金利 | 月次 |
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
