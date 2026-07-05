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
| **手動のみ** | 全件収集（全社 × 5年分） | workflow_dispatch で起動 | GitHub Actions `full-pipeline.yml` |
| **手動のみ** | マクロのみ収集（為替・金利等） | workflow_dispatch で起動 | GitHub Actions `collect-macro.yml` |
| **手動のみ（アーカイブ）** | bs_inventory 補完 | workflow_dispatch で起動 | GitHub Actions `old/` 配下（一回性・完了済み） |
| **UIから手動** | 差分収集・株価更新 | ユーザーがボタン押下 | Render Web UI |
| **自動（CI）** | `pytest` 回帰テスト（Secrets・本番DB非依存） | PR / main への push | GitHub Actions `ci.yml` |

### GitHub Actions workflow 早見表（いつ・何を・どれを使うか）

#### アクティブ（`.github/workflows/` 直下・Actions 対象）

| カテゴリ | workflow 名 | ファイル | 使うタイミング | 所要時間の目安 |
|---|---|---|---|---|
| `[CI]` | pytest 自動テスト | `ci.yml` | PR・main push で自動実行（手動起動不要） | 〜1分 |
| `[定常]` | 差分収集・毎日自動実行 | `daily-incremental.yml` | 毎日 JST 03:00 に自動。手動で即時更新したい場合は `workflow_dispatch` | 5〜15分 |
| `[全件]` | XBRL収集・財務データ全件更新 | `full-pipeline.yml` | DB初期構築時・全社バックフィル必要時（`daily-incremental` を `.disabled` に退避して同時実行回避） | 200〜240分 |
| `[補完]` | マクロのみ収集 | `collect-macro.yml` | `MACRO_SERIES`（為替・金利・指数・コモディティ・ボラ）を Yahoo から収集。新規系列追加や macro_data の鮮度補完。`workflow_dispatch`（years 既定5） | 〜数分 |
| `[推論]` | M-1 per-stock 階層マクロβ推論 | `macro-beta-inference.yml` | ADR-0002 の PyMC 階層ベイズ推論バッチ（`macro_beta_inference.py`）。本番 `requirements.txt` ではなく `requirements-inference.txt`（+PyMC）を使用。`workflow_dispatch`（draws/tune/target_accept 指定可・既定 1000/1000/0.9）。マクロ環境・銘柄構成の変化に応じて随時手動実行する想定で、現時点では定期スケジュールなし | 未計測（本番規模での初回実行後に実測値を追記予定。ローカル検証: 4銘柄合成データ・draws/tune=50・chains=2・g++無しの Python フォールバックで約8分） |

#### アーカイブ済み（`.github/workflows/old/` 配下・一回性・Actions 対象外）

一回性バックフィル完了後に `old/` へ退避済み。再実行が必要な場合のみ `workflow_dispatch` で起動する。定期スケジュールは持たない。

> 株価履歴バックフィル / 週次株価バックフィル / C2 NULL バックフィル / CF NULL バックフィル / bs_machinery バックフィルの5本は完了済みにつき削除済み（Issue #259）。

| カテゴリ | workflow 名 | ファイル | 用途 | 所要時間の目安 |
|---|---|---|---|---|
| `[補完]` | PL/BS NULL バックフィル | `old/.github/workflows/old/refill-pl-bs.yml` | `bs_inventory` 等 旧コホート（〜2022年）が NULL の場合に再取得 | 4〜5時間 |

> **CI（`ci.yml`）**: データ収集系ワークフローとは独立した回帰検知用。`pull_request` と main への `push` で Python 3.13.7 上に `requirements.txt` + `requirements-dev.txt` を入れて `pytest` を実行する。Secrets・外部ネットワーク・本番 DB には一切触れず、`conftest.py` の in-memory SQLite / モックで完結する範囲のみを検証する。

### daily-incremental の動作詳細

毎日 UTC 18:00 に自動起動する `_pipeline_incremental.py` は:
1. EDINET API で「60日以内の提出書類」を取得し、未収集書類のみ XBRL 収集・DB保存
2. 株価更新（Phase 4）: J-Quants（JPX公式）を `days_back=14` で取得 → **J-Quants 無料は直近12週を配信しない**ため、最新日が7日以上古い場合は **Yahoo Finance で `latest+1 〜 today` をギャップ補完**（`fill_recent_stock_price_gap_yahoo`）。鮮度はこの Yahoo 補完が担う（J-Quants 制約表の「配信遅延」行を参照）
3. 成長率・Zスコアを再計算
4. 所要時間: 約 5〜15分（差分量による）

> **注（運用パターン）**: 全件収集（`full-pipeline.yml`）を回している間は、Supabase 接続上限での同時実行を避けるため、本ワークフローを一時的に `daily-incremental.yml.disabled` へリネームして停止する（例: コミット `4764d96`「全件収集中の同時実行回避」）。全件収集が終わったら `.yml` に戻して再有効化する。**現在ファイル名が `.disabled` の場合は自動の定時収集が止まっている状態**なので、UI / 手動収集で補う。
>
> **✅ cron 再開済み（2026-06-22〜）**: dual-table 移行後の `workflow_dispatch` 手動実行で Yahoo ギャップ補完・J-Quants 株価取得が GitHub Actions（Azure IP）から正常動作することを確認 → `on.schedule` を有効化。以降、UTC 18:00（JST 03:00）の定時実行が毎日成功している（直近: 2026-06-26 ✅）。

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
| 月間利用上限 | **2,000 分/月**（パブリックリポジトリは無制限） | 通常運用は Private。上限到達時は一時的に Public 化し、翌月1日のリセット後に Private 復帰 |
| 1ジョブの最大実行時間 | **6時間（360分）** | 長時間処理は各ジョブを6時間枠内に収める |
| 同時実行数 | **20並列**（`max-parallel: 1` で逐次化） | full-pipeline は逐次実行。並列化すると Supabase 接続数上限に当たる |
| Runner の IP | **Azure クラウド IP** | stooq: 完全ブロック。Yahoo Finance: GitHub Actions からは動作。J-Quants / EDINET: 動作 |
| Artifact 保存期間 | `retention-days: 7` に統一 | — |

**Private ↔ Public 切替**: 通常は Private。月 2,000 分を使い切ったら Public 化 → Actions 実行 → 翌月1日リセット後に Private 復帰（GitHub UI → `Settings → Danger Zone → Change repository visibility`）。secrets は可視性と独立して保護されるため切替時の操作不要。

**ジョブ所要時間（設計参考値）**:
- `full-pipeline.yml` finalize（Phase 3〜5）: **200分前後**（`timeout-minutes: 240`）。内訳 = 成長率/Zスコア再計算 約2分 ／ マクロ13系列 約27分→系列数に概ね比例（#218 で 9→13・要再計測） ／ J-Quants 株価（`JQUANTS_BACKFILL_DAYS=730`）約163〜200分。`JQUANTS_BACKFILL_DAYS` 変更時は再計算。
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

- **`stock_price_daily`**：直近 `DAILY_WINDOW_DAYS`（≒6か月）の日次終値のみ。収集のたびにローリング削除（trim）でサイズが頭打ち（autovacuum が死領域を再利用）。チャートの日次ズーム・短期バックテスト用。
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
| **配信遅延（直近側）** | **直近約12週間は配信されない** | 無料プランは株価を**約12週遅れ**で配信。`today − 12週` より新しい日付は空レスポンスになり、J-Quants だけでは鮮度がここで頭打ちになる（例: 2026-06-10 時点の最新は ≈2026-03-17）。**直近12週の鮮度は Yahoo Finance ギャップ補完（`fill_recent_stock_price_gap_yahoo`）で埋める**。この補完を持つのは差分パイプライン（`_pipeline_incremental.py` Phase 4）のみで、手動 `full-pipeline` の finalize は J-Quants のみ＝12週境界で止まる点に注意 |
| 429 リトライ | 指数バックオフ禁止 | 429 発生時は **90秒待機→1回のみ再試行**。失敗したら skip |
| 営業日データのみ | 土日祝は空レスポンス | 空レスポンスを skip として扱う |

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
| レート制限 | **120 req/分** | `FRED_RATE_SLEEP = 0.6` 秒。5系列なら総所要 < 5秒 |
| 頻度混在 | 日次（HY/IG/BEI/T10Y2Y）+ 月次（JP10Y_FRED） | 月次系列は月初日1レコードのみ保存。M-1 の zscore 計算で年次集計するため支障なし |
| 欠損値 | `"."` で返却 | `fetch_fred_series()` でスキップ |
| 認証未設定時 | `FRED_API_KEY=""` | `collect_macro_data()` が「FRED_API_KEY 未設定のためスキップ」でパスする（安全弁） |

**収集系列（`FRED_SERIES`）**:

| `series_code` | FRED series_id | チャネル | 頻度 |
|---|---|---|---|
| `HY_OAS` | `BAMLH0A0HYM2` | クレジット（HY） | 日次 |
| `IG_OAS` | `BAMLC0A0CM` | クレジット（IG） | 日次 |
| `BREAKEVEN10Y` | `T10YIE` | インフレ期待 | 日次 |
| `JP10Y_FRED` | `IRLTLT01JPM156N` | JP10年金利 | 月次 |
| `T10Y2Y` | `T10Y2Y` | 期間構造 | 日次 |

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

### e-Stat API（総務省統計局・要アカウント登録）

ADR-0006 §Decision-1 が定める CPI チャネル。

| 項目 | 値 |
|---|---|
| エンドポイント | `https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData` |
| APIキー | [e-stat.go.jp/api/](https://www.e-stat.go.jp/api/) で無料登録 → `ESTAT_API_KEY` 環境変数に設定 |
| 収集系列 | `JP_CPI_TOTAL`・`JP_CPI_CORE`（全国コア・非季調）・`JP_CPI_TOKYO`（statsDataId=0003427113） |
| 認証未設定時 | `collect_macro_data()` が「ESTAT_API_KEY 未設定のためスキップ」でパスする（安全弁） |

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
