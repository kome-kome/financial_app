# Render デプロイ運用ガイド

本プロジェクトは **Render** にデプロイ済みで稼働している。今後の改修・新機能は
Render の制約と運用形態に合わせて設計すること。

最終更新: 2026-06-02

---

## データ収集の仕組み（自動 vs 手動）

### 自動実行 vs 手動実行の整理

| 種別 | 処理内容 | 実行タイミング | 実行場所 |
|---|---|---|---|
| **自動（毎日）** | 差分収集（新規書類 + 株価更新） | UTC 18:00（JST 03:00）毎日 | GitHub Actions `daily-incremental.yml` |
| **手動のみ** | 全件収集（全社 × 5年分） | workflow_dispatch で起動 | GitHub Actions `full-pipeline.yml` |
| **手動のみ** | 株価バックフィル（過去2年分） | workflow_dispatch で起動 | GitHub Actions `backfill-stock-history.yml` |
| **手動のみ** | CF補完・capex補完 | workflow_dispatch で起動 | GitHub Actions `refill-cf.yml` |
| **UIから手動** | 差分収集・株価更新 | ユーザーがボタン押下 | Render Web UI |

### daily-incremental の動作詳細

毎日 UTC 18:00 に自動起動する `_pipeline_incremental.py` は:
1. EDINET API で「60日以内の提出書類」を取得し、未収集書類のみ XBRL 収集・DB保存
2. J-Quants（JPX公式）から直近の株価を更新
3. 成長率・Zスコアを再計算
4. 所要時間: 約 5〜15分（差分量による）

**重要**: GitHub Actions の Runner は Azure IP のため **stooq は完全ブロック**（403）。株価取得は J-Quants のみ使用。Claude Code リモート環境からも Yahoo Finance はブロックされる。外部サービスの制約値は [CONSTRAINTS.md](CONSTRAINTS.md) を参照。

### CF補完の完了状態（2026-05-31 完了）

| 指標 | 状態 |
|---|---|
| 通常補完（`cf_net_change_cash IS NULL`）| ✅ **全件完了**（remaining=0） |
| capex 充足率 | **88.8%**（CF文を持つ 19,073件中 16,929件取得済み） |
| 残り 2,144件 | アセットライト企業（持株会社・IT等）で capex 行が元々無いため永続的に NULL。再実行しても変わらない |
| `refill-cf.yml` スケジュール | **無効化済み**（PR #31、2026-05-31）。workflow_dispatch は手動実行用として残存 |

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

## 環境変数（Render ダッシュボードで設定）

`render.yaml` で `sync: false` のキーは Render ダッシュボードで手動設定する。
`generateValue: true` は Render が自動生成。

| キー | 用途 | デフォルト |
|---|---|---|
| `DATABASE_URL` | Supabase PostgreSQL 接続 URL | 手動設定（`postgresql://...?sslmode=require`） |
| `EDINET_API_KEY` | 金融庁 EDINET API キー | 手動設定 |
| `JQUANTS_API_KEY` | J-Quants API キー（任意） | 手動設定 |
| `APP_PASSWORD` | ログインパスワード | 手動設定（必須） |
| `APP_SECRET_KEY` | トークン署名キー（HMAC） | Render 自動生成 |
| `APP_RECOVERY_KEY` | パスワードリセット用 | 手動設定 |
| `ALLOWED_ORIGIN` | CORS 許可オリジン | 手動設定（例: `https://financial-app.onrender.com`） |
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
| **G**: J-Quants IssuedShares 取得 | コード変更のみ。`collector.py` の拡張と DB スキーマ追加で対応可。`init_db()` でマイグレーション |
| **H**: `period_end` を DATE 型に | マイグレーションを `init_db()` 内に冪等な `ALTER COLUMN` で書き、起動時に 1 度だけ実行されるようにする。Supabase ダッシュボードでバックアップ取得 → 起動 → 失敗時は環境変数で skip フラグを立てて旧スキーマに戻す導線も用意 |
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
