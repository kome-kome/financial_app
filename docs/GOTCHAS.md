# 既知のハマりどころ・実装上の注意

収集・分析を実装するときに踏みやすい落とし穴と、その対処を集約する。CLAUDE.md からリンクされる索引先。分析モデルの理論的制約は [MODELS.md](MODELS.md) を参照。

---

## 収集フロー別進捗仕様

長時間処理はすべてリアルタイム進捗を UI に届けること。`on_progress(current, total, message)` コールバックで SSE に流す。

| フェーズ | 進捗メッセージ形式 |
|---|---|
| 企業マスタ保存 | `[企業マスタ保存] X/Y社完了` |
| 書類一覧スキャン | `[書類スキャン X/Y日] YYYY-MM-DD  累計 Z社` |
| XBRL取得・保存 | `[X/Y] 企業名(証券コード) 決算期末` |
| 差分スキップ | `[X/Y] スキップ（収集済み）: 企業名 決算期末` |

SSEエンドポイント: 収集=`/api/collect/stream`、市場データ=`/api/collect/market-stream`

---

## 業種データの取得方法

- **業種はXBRLから取得できない**。EDINETのXBRLに TSE 33業種コードは含まれていない。
- **正規ソース**: JPX上場会社一覧Excel（`JPX_EXCEL_URL` = `data_j.xls`、33業種コード列=col4/col5）
- `update_industry_from_jpx(client, db)` が `run_full_collection` の末尾で自動実行される。
- 証券コードは4桁数字（`1301`）とアルファベット混在（`350A`）の両形式に対応済み。

---

## EDINET / XBRL の取り扱い

- **EDINET XBRL CSV** は UTF-8 と UTF-16 LE（タブ区切り）が混在。`fetch_xbrl_csv` で両方対応済み。
- **XBRL要素選択**: 連結優先判定は `"NonConsolidated" not in ctx` を必ず含めること。優先度: 連結=2 > 非メンバー=1 > メンバー付き=0。
- **CF要素名・XBRL ZIP 構造**: EDINET XBRL type=5 ZIP には**複数の CSV ファイル**が含まれる。CF合計は概要ファイルに、CF明細は別ファイルに存在。ZIP内の全CSVを concat して parse する（`fetch_xbrl_csv`）。投資CFのEDINET標準要素は `NetCashProvidedByUsedInInvestmentActivities`（Investment、旧 Investing は誤り）。
- **capex（設備投資）はラベル照合で取得**: 設備投資のCF明細行は**企業独自の拡張要素ID**でタグ付けされることが多く、標準要素ID（`PurchaseOfPropertyPlantAndEquipment`）では捕捉できない（実証: 3,000件中0件）。EDINET CSV の**「項目名」列**で照合する（`_match_capex_by_label` / `CAPEX_LABEL_*` 定数）。「有形固定資産の取得による支出」等を捕捉し、売却収入・無形のみは除外。capex は支出＝負（`-abs(val)`）で統一。
- **CF NULL補完の運用**: `refill-cf.yml` の通常補完は **2026-05-31 に完了**（remaining=0）。スケジュール無効化済み（PR #31）。capex 充足率 88.8%、残り 2,144件はアセットライト企業で再実行しても変わらない。将来の新規データで NULL が出た場合は `mode=refill` で workflow_dispatch を手動実行すること。
- **`check.py` の日付**は自動計算（祝日は非対応、祝日前後は失敗する場合あり）。

---

## DB・運用上の注意

- **URLとHTMLファイル名の対応**を崩さない: `/` ↔ `dashboard.html`、`/collection` ↔ `collection.html`、`/analysis` ↔ `analysis.html`。
- **`CollectionLog.status`** の値: `running` / `done` / `error` / `resolved`（修正済みエラー）。UIは `resolved` を緑扱い。
- **.env は UTF-8（BOMなし）で保存すること**。BOM付きだと最初のキーが読み込めずAPIキーが空になる。
- 本番運用前に `APP_PASSWORD`・`APP_SECRET_KEY`・`APP_RECOVERY_KEY` を必ず設定する。

---

## 認証・セキュリティ実装メモ

- **【実装済み（Tier3-3）】** 認証を HttpOnly Cookie 方式へ移行（`localStorage` 廃止＝XSS によるトークン盗難を防止）。`auth_token`（HttpOnly）＋`csrf_token`（JS可読）の2 Cookie、`SameSite=Lax`、本番は `COOKIE_SECURE=true` で Secure 属性。`Authorization: Bearer` は廃止。
- **【実装済み（Tier3-3）】** 非冪等メソッド（POST/PUT/DELETE/PATCH）に CSRF Double-Submit（`X-CSRF-Token` ヘッダ == `csrf_token` Cookie）を要求。`/api/auth/` 配下は免除（ログイン前のため）。フロントは各 `apiFetch` が `csrf_token` Cookie を読みヘッダ付与。ログアウトは `POST /api/auth/logout` で Cookie 削除。
- **【実装済み（Tier3-1）】** 重い処理（収集・分析）と認証に `slowapi` でレート制限を導入（収集 3/分・分析 20/分・ログイン 10/分・リセット 3/分・単一更新 10/分）。IP単位（`get_remote_address`）。`APP_RATELIMIT_ENABLED=false` で無効化可能（テスト時等）。環境変数名は slowapi 予約キー `RATELIMIT_*` との衝突を避けるため `APP_` 接頭辞必須。
- **【実装済み（Tier3-2）】** CSP の `script-src` から `'unsafe-inline'` を除去済み（インライン `<script>` を `static/js/` へ外部化＋インラインイベントハンドラを `data-*` 属性＋イベント委譲へ移行）。`style-src 'unsafe-inline'` はインライン `<style>`/`style=` 属性が残るため維持（script-src より低リスク）。

---

## 分析・計算の実装メモ

- **成長率計算は (edinet_code, year, period_end) で副ソート**済み。同年複数レコードがある企業の前期比が不定にならないようにしている。
- **フリーCF = 営業CF + 投資CF**（設備投資以外の投資活動も含む近似値）。
- 分析モデルの理論・次元整合性・外れ値処理（winsorize）・株数推計・Zスコア年度別計算の詳細は [MODELS.md](MODELS.md) を参照。
