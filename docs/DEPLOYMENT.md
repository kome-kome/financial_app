# Render デプロイ運用ガイド

本プロジェクトは **Render** にデプロイ済みで稼働している。今後の改修・新機能は
Render の制約と運用形態に合わせて設計すること。

最終更新: 2026-05-19

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
- バックグラウンドのスケジューラー（`api.py:_daily_scheduler`）は
  **スピンダウンすると停止する**。深夜の自動収集を確実に動かすには:
  - **(A)** 有料プラン（$7/月で常時稼働）にアップグレード
  - **(B)** Render Cron Jobs（別 service として定義）で起こす
  - **(C)** 外部の cron-as-a-service（cron-job.org 等）で 14 分おきに `/health` を叩く

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
| **F**: HttpOnly Cookie 認証 | コード変更のみ。Render は HTTPS なので `Secure` / `SameSite=Strict` を付けられる |
| **E**: 本番デプロイ対応 | **大部分が完了済み**。残るのは Supabase の DB バックアップ運用ポリシー策定（Supabase の自動バックアップ機能を利用）と監視（Render ダッシュボード + UptimeRobot 等） |

---

## 既知の運用 Tips

### スピンダウン対策
無料プランでバックグラウンドジョブを確実に動かしたい場合、外部の cron-job.org 等から
`/health` を 14 分おきに叩いて起き続けさせる方法が現実的。コストゼロ。
ただし無料プランの 750 時間 / 月 を消費する点に注意（常時稼働で 720 時間/月）。

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
