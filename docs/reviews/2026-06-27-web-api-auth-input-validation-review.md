# 集中コードレビュー: Web API 層（認証・入力検証・SQL 安全性）

- **日付**: 2026-06-27
- **着眼点（スコープ）**: 公開 URL を持つ本番 Web アプリの**外向き攻撃面**に絞った集中レビュー。具体的には認証ミドルウェア（`api.py`）・認証ルーター（`routers/auth.py`）・市場/DB ビューアルーター（`routers/market.py`）・収集ルーター（`routers/collect.py`）の、認証・CSRF・入力検証・生 SQL 組み立て・秘密情報の取り扱い。
- **対象コミット**: `23387c7`（= origin/main）
- **総評**: **致命的な欠陥は無し**。Web セキュリティ面はよく作り込まれている — CSP / HSTS / X-Frame-Options / Referrer-Policy / Permissions-Policy（`_SecurityHeadersMiddleware`）、CSRF（double-submit + `hmac.compare_digest`）、レート制限（slowapi）、トークン署名へのパスワード fingerprint 混入（変更時に旧トークン失効）、定数時間比較。生 SQL を組み立てる f-string（`database.py` / `routers/market.py`）も**すべてホワイトリスト or モデル定義由来の識別子**で、ユーザー入力は注入されない。以下は **軽微〜中程度の堅牢性・運用・一貫性の指摘**。

> 本書はレビュー記録（report）であり、残タスクの正本ではない。各指摘は GitHub Issue 化済み（CLAUDE.md のタスク運用＝残タスクの正本は GitHub Issues）。

---

## F-1 [低] `/api/companies`・`/api/screen` の `limit`/`offset` 上限欠如（入力検証の不整合） → #245

**該当**: [routers/market.py:232](../../routers/market.py#L232)（`/api/companies`、`limit`/`offset` 無検証・レート制限デコレータ無し）、[routers/market.py:306](../../routers/market.py#L306)（`ScreenRequest.limit` に `Field` 制約無し）

**問題**: 同じ「件数上限」を他の入口では一貫検証している（DB ビューア `db_preview`/`db_export` は `1<=limit<=500`/`<=100000`、collect ルーターは `Field(ge/le)`）のに、この 2 つだけ `limit` 無制限・`offset` 負値も非検証。認証済みクライアントが巨大 `limit` を渡すと応答サイズ・メモリ・DB 負荷が増大しうる。

> 影響度: `companies` は数千行で自然に頭打ち、`financial_metrics` の方が膨らみやすい。深刻な DoS というより **defense-in-depth（検証の一貫性）の欠落**。

**推奨対応**: `/api/companies` は `limit` 1〜500・`offset>=0` を検証（必要なら rate-limit 付与）、`/api/screen` は `Field(default=200, ge=1, le=500)`。

- **ラベル**: `priority:low`, `security`

---

## F-2 [低] `/api/db/stats` の生 SQL f-string 組み立て（脆いパターン・defense-in-depth） → #246

**該当**: [routers/market.py:494](../../routers/market.py#L494)（`percentile_cont(...) ... FROM {table}` を f-string で組み立て）

**問題**: テーブル名・カラム名を f-string で生 SQL に直接埋め込む。**現状は注入不可**（`table` はホワイトリスト検証済み・`c.name` はモデル定義由来でユーザー入力ではない）が、「カラム名に SQL メタ文字が含まれない」という暗黙の不変条件に依存しており、将来のカラム/テーブル追加で壊れやすい。

**推奨対応**: 識別子を `quoted_name`/`func` で安全に組み立てる、テーブル名も `model.__tablename__` 経由で取得し外部入力文字列を直接埋め込まない。`percentile_cont` のための raw SQL 自体は妥当。

- **ラベル**: `priority:low`, `security`

---

## F-3 [中] パスワードリセットが Render 本番では再起動で揮発する → #247

**該当**: [routers/auth.py:78](../../routers/auth.py#L78)（`reset_password` → `_update_env_file`）、[api.py:22](../../api.py#L22)（`load_dotenv()` = `override=False`）

**問題**: リセットは (1) プロセス内 `api.APP_PASSWORD` 更新 + (2) `.env` 書き戻し、の 2 段。だが Render Free は ephemeral FS のため `.env` 書込は再起動で消え、かつ本番 `APP_PASSWORD` は dashboard 環境変数供給で `load_dotenv(override=False)` により**次回起動時は dashboard の旧値が優先**。結果、本番でリセットしても**再起動で旧パスワードに静かに戻る**（利用者は変更済みと誤認）。ARCHITECTURE.md/DEPLOYMENT.md にも揮発性の注記が無い。

**推奨対応**: 最小＝UI/DEPLOYMENT.md に「本番では一時変更・恒久化は dashboard 更新が必要」と明記。恒久＝パスワード（ハッシュ）を DB 設定テーブルに保存し起動時ロード、`.env` 書き戻し廃止。

- **ラベル**: `priority:medium`, `ops`, `security`

---

## 確認したが問題なしと判断した項目（記録）

- **認証ミドルウェア**（`api.py:208`）: `/api/` 配下のみ保護。HTML ページ（`/`,`/db` 等）は未保護だがシェルのみでデータは `/api/` 経由＝意図通り。`/api/auth/` と `/login` は正しく素通し。
- **CSRF**: 非安全メソッドで double-submit + `hmac.compare_digest`。GET エクスポート（`/api/export/csv` 等）は SameSite=lax Cookie + クロスオリジン応答読取不可のため exfil 不可。
- **CORS**: `allow_credentials=True` だが `allow_origins` は `ALLOWED_ORIGIN` env で明示制御（既定 localhost）。`*` ではない。
- **トークン**: HMAC + timestamp + パスワード fingerprint（変更時に旧トークン失効・#230 で対応済み）、TTL 30 日。
- **生 SQL の f-string**（`database.py:942/968`、`routers/market.py:500`、`scripts/migrate_*`）: いずれも静的リテラル or ホワイトリスト識別子。注入面なし。
- **秘密情報のログ出力**: `collector_prices.py` 等で URL/トークンをログに出す経路は検出されず（#231 で FRED ログ漏洩は修正済み）。
- **APP_SECRET_KEY 未設定時**: dev 既定値 + 警告。本番は env で必須化（GOTCHAS.md に記載）。
