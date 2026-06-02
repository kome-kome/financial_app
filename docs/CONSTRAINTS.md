# 外部サービス制約一覧

新機能・改修・データ収集ロジックを設計する際は **必ずこの表を参照**し、各サービスの無料プラン制約に違反しない方式を選ぶこと。CLAUDE.md からリンクされる索引先。

---

## GitHub Actions（無料アカウント）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| 月間利用上限 | **2,000 分/月**（パブリックリポジトリは無制限） | 通常運用は Private。上限到達時は一時的に Public 化し、翌月1日のリセット後に Private 復帰 |
| 1ジョブの最大実行時間 | **6時間（360分）** | 長時間処理は各ジョブを6時間枠内に収める |
| 同時実行数 | **20並列**（`max-parallel: 1` で逐次化） | full-pipeline は逐次実行。並列化すると Supabase 接続数上限に当たる |
| Runner の IP | **Azure クラウド IP** | stooq: 完全ブロック。Yahoo Finance: GitHub Actions からは動作。J-Quants / EDINET: 動作 |
| Artifact 保存期間 | `retention-days: 7` に統一 | — |

### Private ↔ Public 切替方針

- **通常は Private**。月 2,000 分を使い切ったら Public 化 → Actions 実行 → 翌月1日リセット後に Private 復帰
- 切替: GitHub UI → `Settings → Danger Zone → Change repository visibility`
- secrets（DATABASE_URL / EDINET_API_KEY 等）は可視性と独立して保護されるため切替時の操作不要

### finalize ジョブの所要時間（設計参考値）

`full-pipeline.yml` の finalize ジョブ（Phase 3〜5）は **200分前後**。`timeout-minutes: 240` 設定済み。

| Phase | 処理 | 実測値 |
|---|---|---|
| 3 | 成長率・Zスコア再計算 | 約2分 |
| 4 | マクロデータ（Yahoo Finance × 9系列） | 約27分 |
| 5 | J-Quants 株価収集（`JQUANTS_BACKFILL_DAYS=730`） | 約163〜200分 |

`JQUANTS_BACKFILL_DAYS` を変更する場合は必ずこの見積もりを再計算すること。

### backfill-stock-history ジョブの所要時間

| 処理 | 目安 |
|---|---|
| 対象: stock_price が NULL かつ period_end が 730日超前（初回: 約3,800社） | — |
| `YAHOO_STOCK_RATE_SLEEP = 0.5秒`、1社1リクエスト | 約60〜90分 |
| `timeout-minutes: 150` 設定済み | — |

---

## Supabase（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| DB ストレージ | **500 MB** | `SKIP_XBRL_RAW=true` を維持（xbrl_raw_documents の大量書き込みを避ける） |
| 接続数 | **最大60接続**（pgbouncer 経由） | 並列パイプライン実行を禁止。`max-parallel: 1` を維持 |
| 一時的 read-only 移行 | トランザクションが長すぎると自動移行 | `run_full_collection` は `MASTER_BATCH=200` 件ごとに commit |
| プロジェクト停止 | **1週間アクセスなしで自動停止** | 長期不使用時は要注意 |

---

## Render（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| メモリ | **512 MB** | 大量データのオンメモリ処理禁止。バッチ分割・ストリーミングを使うこと |
| スピンダウン | **15分無通信で停止** | SSE で長時間接続する処理は timeout 設計が必要 |
| HTTP タイムアウト | **30秒** | 長時間処理は `BackgroundTasks` + SSE 進捗配信 |
| デプロイ | `main` push で自動デプロイ | 動作確認前に main へ push しないこと |
| SSH | **不可** | ログは Render ダッシュボードから確認 |

Render 運用の詳細は [DEPLOYMENT.md](DEPLOYMENT.md) を参照。

---

## J-Quants API（無料プラン）

### プラン制約

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| レート制限 | **約5リクエスト/60秒** | `JQUANTS_RATE_SLEEP = 20.0` 秒間隔を維持 |
| 取得可能期間 | **過去2年分** | `days_back ≤ 730`。UI の選択肢もこれに合わせること |
| 429 リトライ | 指数バックオフ禁止 | 429 発生時は **90秒待機→1回のみ再試行**。失敗したら skip |
| 営業日データのみ | 土日祝は空レスポンス | 空レスポンスを skip として扱う |

### 設計制約（実装時の必須ルール）

- **認証情報**: `.env` に `JQUANTS_API_KEY` を設定。未設定時は `ValueError` で明示エラー。
- **データ優先度**: J-Quants = JPX公式 → stooq より正確。`ON CONFLICT DO UPDATE` で上書き（stooq は `ON CONFLICT DO NOTHING`）。
- **コード変換**: J-Quants は5桁コード（例: `"13010"`）。先頭4桁が証券コード（`code[:4]`）。
- **取得単位**: 日付単位で全銘柄を一括取得。1営業日 = 1〜数リクエスト（ページネーション対応済み）。
- **無料プランの上限**: 過去2年分（`days_back ≤ 730`）。UI の選択肢もこれに合わせること。
- **`close` は nullable=False**: `Close` が `None` の行はスキップ（停止銘柄等）。
- **レート制限**: `JQUANTS_RATE_SLEEP = 20.0` 秒間隔を維持。
- **429 リトライ戦略**: 429 発生時は90秒待機してから1回だけ再試行し、それでも429 なら skip（指数バックオフ禁止）。
- **CardinalityViolation 対策**: 5桁コードが同じ4桁 sec_code にマップされる場合がある。INSERT前に edinet_code で重複排除（先着1件採用）。
