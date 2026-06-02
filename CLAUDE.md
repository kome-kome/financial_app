# CLAUDE.md

日本株財務分析ツール。Claude Code への動作指示ファイル。プロジェクト目的・方針は [VISION.md](docs/VISION.md)、設計詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照。

---

## データ収集の仕組み（まずここを読む）

### 自動実行 vs 手動実行の整理

| 種別 | 処理内容 | 実行タイミング | 実行場所 |
|---|---|---|---|
| **自動（毎日）** | 差分収集（新規書類 + 株価更新） | UTC 18:00（JST 03:00）毎日 | GitHub Actions `daily-incremental.yml` |
| **手動のみ** | 全件収集（全社 × 5年分） | workflow_dispatch で起動 | GitHub Actions `full-pipeline.yml` |
| **手動のみ** | 株価バックフィル（過去2年分） | workflow_dispatch で起動 | GitHub Actions `backfill-stock-history.yml` |
| **手動のみ** | CF補完・capex補完 | workflow_dispatch で起動 | GitHub Actions `refill-cf.yml` |
| **UIから手動** | 差分収集・株価更新 | ユーザーがボタン押下 | Render Web UI |

### Render（本番Webサーバー）の役割

Render は **UI表示・API・手動収集操作のみ**。自動収集は一切持たない。
メモリ 512MB・スピンダウン 15分・SSH不可の制約があるため、重い処理（全件収集・XBRL大量ダウンロード）は **すべて GitHub Actions のみ** で実行する。
`RENDER_LIGHT_MODE=true` の場合、全件収集・J-Quants株価収集・株価履歴再構築は API で 403 を返してブロックする。

### daily-incremental の動作詳細

毎日 UTC 18:00 に自動起動する `_pipeline_incremental.py` は:
1. EDINET API で「60日以内の提出書類」を取得し、未収集書類のみ XBRL 収集・DB保存
2. J-Quants（JPX公式）から直近の株価を更新
3. 成長率・Zスコアを再計算
4. 所要時間: 約 5〜15分（差分量による）

**重要**: GitHub Actions の Runner は Azure IP のため **stooq は完全ブロック**（403）。株価取得は J-Quants のみ使用。Claude Code リモート環境（このセッション）からも Yahoo Finance はブロックされる。

### CF補完の完了状態（2026-05-31 完了）

| 指標 | 状態 |
|---|---|
| 通常補完（`cf_net_change_cash IS NULL`）| ✅ **全件完了**（remaining=0） |
| capex 充足率 | **88.8%**（CF文を持つ 19,073件中 16,929件取得済み） |
| 残り 2,144件 | アセットライト企業（持株会社・IT等）で capex 行が元々無いため永続的に NULL。再実行しても変わらない |
| `refill-cf.yml` スケジュール | **無効化済み**（PR #31、2026-05-31）。workflow_dispatch は手動実行用として残存 |

---

## 外部サービス制約一覧（設計時に必ず参照すること）

新機能・改修・データ収集ロジックを設計する際は **必ずこの表を参照**し、各サービスの無料プラン制約に違反しない方式を選ぶこと。

### GitHub Actions（無料アカウント）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| 月間利用上限 | **2,000 分/月**（パブリックリポジトリは無制限） | 通常運用は Private。上限到達時は一時的に Public 化し、翌月1日のリセット後に Private 復帰 |
| 1ジョブの最大実行時間 | **6時間（360分）** | 長時間処理は各ジョブを6時間枠内に収める |
| 同時実行数 | **20並列**（`max-parallel: 1` で逐次化） | full-pipeline は逐次実行。並列化すると Supabase 接続数上限に当たる |
| Runner の IP | **Azure クラウド IP** | stooq: 完全ブロック。Yahoo Finance: GitHub Actions からは動作。J-Quants / EDINET: 動作 |
| Artifact 保存期間 | `retention-days: 7` に統一 | — |

#### Private ↔ Public 切替方針

- **通常は Private**。月 2,000 分を使い切ったら Public 化 → Actions 実行 → 翌月1日リセット後に Private 復帰
- 切替: GitHub UI → `Settings → Danger Zone → Change repository visibility`
- secrets（DATABASE_URL / EDINET_API_KEY 等）は可視性と独立して保護されるため切替時の操作不要

#### finalize ジョブの所要時間（設計参考値）

`full-pipeline.yml` の finalize ジョブ（Phase 3〜5）は **200分前後**。`timeout-minutes: 240` 設定済み。

| Phase | 処理 | 実測値 |
|---|---|---|
| 3 | 成長率・Zスコア再計算 | 約2分 |
| 4 | マクロデータ（Yahoo Finance × 9系列） | 約27分 |
| 5 | J-Quants 株価収集（`JQUANTS_BACKFILL_DAYS=730`） | 約163〜200分 |

`JQUANTS_BACKFILL_DAYS` を変更する場合は必ずこの見積もりを再計算すること。

#### backfill-stock-history ジョブの所要時間

| 処理 | 目安 |
|---|---|
| 対象: stock_price が NULL かつ period_end が 730日超前（初回: 約3,800社） | — |
| `YAHOO_STOCK_RATE_SLEEP = 0.5秒`、1社1リクエスト | 約60〜90分 |
| `timeout-minutes: 150` 設定済み | — |

### Supabase（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| DB ストレージ | **500 MB** | `SKIP_XBRL_RAW=true` を維持（xbrl_raw_documents の大量書き込みを避ける） |
| 接続数 | **最大60接続**（pgbouncer 経由） | 並列パイプライン実行を禁止。`max-parallel: 1` を維持 |
| 一時的 read-only 移行 | トランザクションが長すぎると自動移行 | `run_full_collection` は `MASTER_BATCH=200` 件ごとに commit |
| プロジェクト停止 | **1週間アクセスなしで自動停止** | 長期不使用時は要注意 |

### Render（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| メモリ | **512 MB** | 大量データのオンメモリ処理禁止。バッチ分割・ストリーミングを使うこと |
| スピンダウン | **15分無通信で停止** | SSE で長時間接続する処理は timeout 設計が必要 |
| HTTP タイムアウト | **30秒** | 長時間処理は `BackgroundTasks` + SSE 進捗配信 |
| デプロイ | `main` push で自動デプロイ | 動作確認前に main へ push しないこと |
| SSH | **不可** | ログは Render ダッシュボードから確認 |

### J-Quants API（無料プラン）

| 項目 | 制約値 | 設計への影響 |
|---|---|---|
| レート制限 | **約5リクエスト/60秒** | `JQUANTS_RATE_SLEEP = 20.0` 秒間隔を維持 |
| 取得可能期間 | **過去2年分** | `days_back ≤ 730`。UI の選択肢もこれに合わせること |
| 429 リトライ | 指数バックオフ禁止 | 429 発生時は **90秒待機→1回のみ再試行**。失敗したら skip |
| 営業日データのみ | 土日祝は空レスポンス | 空レスポンスを skip として扱う |

---

## デプロイ環境

**本プロジェクトは [Render](https://render.com/) にデプロイ済みで稼働中**。DB は **Supabase PostgreSQL**（外部）。
詳細は [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) を必読。

主要ポイント:
- マイグレーションは `init_db()` 内で **冪等** に実装（起動時に自動実行）
- 永続化はすべて Supabase。ローカルファイル書き込みは再デプロイで消える
- 長時間処理は `BackgroundTasks` + SSE 進捗配信（30秒で HTTP タイムアウト）
- 環境変数は `render.yaml` の `envVars` に追記し Render ダッシュボードで値を設定
- **Render 側に自動収集は持たない**。`_daily_scheduler` / `_startup_catchup` は廃止済み
- **時刻系**: DB 書き込みは `datetime.utcnow()` で UTC 統一。API レスポンス表示は `+ timedelta(hours=9)` で JST 変換し「JST」と明示

---

## GitHub 協調ワークフロー

**デスクトップ版 Claude Code（ここ）と Web版（claude.ai/code）が `kome-kome/financial_app` を介して協調する。**

### ブランチ戦略

| 規模 | ブランチ | 手順 |
|---|---|---|
| バグ修正・小改善 | `main` 直接 | commit → push |
| 機能追加・大きな変更 | `feature/xxx` | branch → commit → push → PR |

### セッション終了時の必須手順

```bash
git status
git add [変更ファイル]   # .env や機密ファイルを含めないこと
git commit -m "変更内容の説明"
git push origin main   # または feature/xxx
```

PR は Web版 Claude Code でレビュー → コメント → main へマージ。

---

## 動作設定

- 日本語応答
- **ツール実行前（必須）**: 許可ダイアログが出るコマンドを実行する前に、必ず以下の形式でテキストを出力する:
  ```
  🔧 実行: [操作名]
  目的: [なぜこのコマンドが必要か]
  ↓ 次の許可ダイアログで「許可」を選択してください
  ```
  **例外**: allow リストに登録済みで自動実行されるコマンドは不要。
- ツール実行後: 結果の要点を日本語で表示

---

## 起動・実行コマンド

```powershell
# ローカル（Windows）
.\venv\Scripts\Activate.ps1
uvicorn api:app --reload           # サーバー起動 → http://localhost:8000/

python collector.py --years 5      # 全件収集（5年分）
python collector.py --years 1 --max 10  # テスト用（10社）
python collector.py --company E02167    # 特定企業更新
python collector.py --market            # 株価のみ更新
python collector.py --incremental       # 差分収集
python check.py                         # EDINET API接続テスト
```

```bash
# テスト
pytest                                   # 全件
pytest tests/test_utils.py              # 単一ファイル
```

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `database.py` | テーブル定義・upsert・成長率/Zスコア計算 |
| `collector.py` | EDINET+J-Quantsからデータ収集→DB保存 |
| `api.py` | FastAPI REST・SSE・回帰分析 |
| `templates/dashboard.html` | トップページ・ダッシュボード（`/`） |
| `templates/collection.html` | 収集・スクリーニング画面（`/collection`） |
| `templates/analysis.html` | 回帰・乖離分析画面（`/analysis`） |
| `templates/company.html` | 企業詳細画面（`/company/{edinet_code}`）。業績・CF・バリュエーション・Zスコア・同業比較を Chart.js で可視化 |
| `_pipeline_gh.py` | GitHub Actions 用・全件収集パイプライン（workflow_dispatch 手動起動） |
| `_pipeline_incremental.py` | GitHub Actions 用・差分収集（daily-incremental.yml で毎日自動実行） |
| `check.py` | EDINET API疎通確認用ワンショット |
| `render.yaml` | Render デプロイ定義 |
| `docs/DEPLOYMENT.md` | Render デプロイ運用ガイド |
| `docs/ARCHITECTURE.md` | 完全アーキテクチャ図（必読） |

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

## 設計制約（変えてはいけないこと）

- `upsert_financial` の入力: `{bs,pl,cf,derived,val}`。bs/pl/cf は `bs_` 等プレフィックス付きで DB カラムにマップ、derived/val はそのまま。XBRL項目追加時は `XBRL_MAP`（collector.py）と `FinancialRecord`（database.py）の両方を更新。
- `ols()`（`plugins/utils.py`）: numpy/scipy/statsmodels/scikit-learn の利用は許可（`docs/VISION.md` の採用基準参照）。新規導入時は WebSearch で CVE・DL数・Stars を評価しユーザー承認を得ること。
- CORS は `ALLOWED_ORIGIN` 環境変数で制御（デフォルト: `http://localhost:8000`）。
- `/api/gap-analysis` は `/api/regression` 実行後でないと404になる。
- 認証ミドルウェアは `/api/auth/` プレフィックスを常に通過させる（ログインAPI自体を守ると詰まる）。
- `run_full_collection` の `df_master` は **常に全件**（`max_companies` で絞らない）。`max_companies` は書類収集件数の上限のみ。
- `collect_doc_ids_for_period` の `max_companies` は**全期間スキャン後に先着N社へ絞り込む**方式（早期終了禁止）。

---

## 分析手法の既知問題・制約

- **Zスコアは年度別に計算すること**。`calc_zscore_normalization` は年度を跨いで計算しない。内部的に `_calc_zscore_for_year(db, year)` を年度ごとに呼ぶ。
- **gap_analysis の収束予測**: 履歴 ≥ 8 観測の銘柄は `statsmodels` ARIMA(1,0,0) による AR(1) MLE で半減期を推定（`HL = -ln(2)/ln(φ)`）。履歴不足はヒューリスティック（`half_life = |gap|/2`）にフォールバック。出力の `method` フィールドで判別可能。
- **成長率計算は (edinet_code, year, period_end) で副ソート**済み。同年複数レコードがある企業の前期比が不定にならないようにしている。
- **フリーCF = 営業CF + 投資CF**（設備投資以外の投資活動も含む近似値）。
- **市場データの株数推計** = `total_equity / bps`（発行済株式数の近似）。IFRS・JGAAP混在時に精度が下がる場合あり。
- **単位の例外**: `market_cap` のみ百万円。`pl_revenue` 等は円。直接比較・演算しないこと。
- **分析モデルの次元整合性（必須）**: 説明変数と被説明変数は同一次元で設計すること。✅ 正しい例: per-share財務金額[円/株]（EPS/BPS/DPS）→ 株価[円/株]（Ohlsonモデル型）。❌ 誤った例: op_margin[%] → market_cap[百万円]。業種別OLS（`plugins/sector_ols.py`）は target=stock_price 固定 + 説明変数 per-share 固定で UI/API レベルで強制。
- **財務データの外れ値処理（必須）**: OLS学習前に各特徴量を winsorize（p1-p99クリッピング）。`plugins/utils.py` の `winsorize()` を使用。
- **横断的R²の解釈**: `cv_metrics.mean_r2` は業種間の構造差により構造的に低くなる（-0.1〜0.4程度）。R² < 0 はモデルが無価値ではなく「業種固定効果なしの一括回帰の限界」を反映している。

---

## 業種データの取得方法

- **業種はXBRLから取得できない**。EDINETのXBRLに TSE 33業種コードは含まれていない。
- **正規ソース**: JPX上場会社一覧Excel（`JPX_EXCEL_URL` = `data_j.xls`、33業種コード列=col4/col5）
- `update_industry_from_jpx(client, db)` が `run_full_collection` の末尾で自動実行される。
- 証券コードは4桁数字（`1301`）とアルファベット混在（`350A`）の両形式に対応済み。

---

## J-Quants API の設計制約

- **認証情報**: `.env` に `JQUANTS_API_KEY` を設定。未設定時は `ValueError` で明示エラー。
- **データ優先度**: J-Quants = JPX公式 → stooq より正確。`ON CONFLICT DO UPDATE` で上書き（stooq は `ON CONFLICT DO NOTHING`）。
- **コード変換**: J-Quants は5桁コード（例: `"13010"`）。先頭4桁が証券コード（`code[:4]`）。
- **取得単位**: 日付単位で全銘柄を一括取得。1営業日 = 1〜数リクエスト（ページネーション対応済み）。
- **無料プランの上限**: 過去2年分（`days_back ≤ 730`）。UI の選択肢もこれに合わせること。
- **`close` は nullable=False**: `Close` が `None` の行はスキップ（停止銘柄等）。
- **レート制限**: `JQUANTS_RATE_SLEEP = 20.0` 秒間隔を維持。
- **429 リトライ戦略**: 429 発生時は90秒待機してから1回だけ再試行し、それでも429 なら skip（指数バックオフ禁止）。
- **CardinalityViolation 対策**: 5桁コードが同じ4桁 sec_code にマップされる場合がある。INSERT前に edinet_code で重複排除（先着1件採用）。

---

## パッケージ管理方針（pip install）

`pip install` の実行前に必ず:
1. **セキュリティリスク評価** — WebSearch でパッケージ名＋"CVE" / "security vulnerability" を検索
2. **評価結果を提示**: パッケージ名・バージョン・既知CVE・DL数・総合判定（✅低リスク / ⚠️要注意 / ❌高リスク）
3. **ユーザーの明示的な承認を得てから実行**

**バージョン pin**: `requirements.txt` は完全 pin（`==`）。アップグレードは単独 PR で行い、`pytest` と主要画面の動作確認をセットで実施すること。

---

## リファクタリング方針

- ファイル名・URL: 機能名で命名（フェーズ番号禁止）
- HTML: CSS/JSインライン1ファイル維持（分割禁止）
- 定数: ファイル冒頭に集約（コード中にハードコード禁止）
- **`docs/ARCHITECTURE.md` の随時更新（必須）**: コード変更と同じ作業内で更新すること。
  - DBテーブル追加・変更 → ER図（セクション3）
  - 新しい処理フロー追加 → シーケンス図（セクション4）
  - APIエンドポイント追加 → エンドポイント一覧（セクション8）
  - HTMLタブ・画面追加 → 画面遷移図（セクション5）
  - プラグイン追加 → クラス図（セクション7）
- **`docs/MODELS.md` と `templates/models.html` の随時更新（必須）**: 分析モデル追加・変更時に更新。参考文献は必ず原著論文の DOI または公式 URL を記載（Wikipedia不可）。

---

## テスト方針

実装後は必ず Claude 自身が Python でテストを実行し、動作を確認してからユーザーに報告する。

| 種別 | 内容 | 制限時間 |
|---|---|---|
| API疎通 | EDINET/stooq レスポンス確認 | 30秒 |
| 収集ロジック | 3社分の収集→DB書き込み確認 | 2分 |
| 収集フル | 10社分の収集→スクリーニング確認 | 5分 |
| UI確認 | API エンドポイントの HTTP 200 確認 | 10秒 |

`tests/` 配下は `pytest.ini` で `testpaths=tests` に固定。プラグイン追加時は `tests/test_<plugin>.py` を作成すること。

---

## 既知の注意事項

- **EDINET XBRL CSV** は UTF-8 と UTF-16 LE（タブ区切り）が混在。`fetch_xbrl_csv` で両方対応済み。
- **XBRL要素選択**: 連結優先判定は `"NonConsolidated" not in ctx` を必ず含めること。優先度: 連結=2 > 非メンバー=1 > メンバー付き=0。
- **CF要素名・XBRL ZIP 構造**: EDINET XBRL type=5 ZIP には**複数の CSV ファイル**が含まれる。CF合計は概要ファイルに、CF明細は別ファイルに存在。ZIP内の全CSVを concat して parse する（`fetch_xbrl_csv`）。投資CFのEDINET標準要素は `NetCashProvidedByUsedInInvestmentActivities`（Investment、旧 Investing は誤り）。
- **capex（設備投資）はラベル照合で取得**: 設備投資のCF明細行は**企業独自の拡張要素ID**でタグ付けされることが多く、標準要素ID（`PurchaseOfPropertyPlantAndEquipment`）では捕捉できない（実証: 3,000件中0件）。EDINET CSV の**「項目名」列**で照合する（`_match_capex_by_label` / `CAPEX_LABEL_*` 定数）。「有形固定資産の取得による支出」等を捕捉し、売却収入・無形のみは除外。capex は支出＝負（`-abs(val)`）で統一。
- **CF NULL補完の運用**: `refill-cf.yml` の通常補完は **2026-05-31 に完了**（remaining=0）。スケジュール無効化済み（PR #31）。capex 充足率 88.8%、残り 2,144件はアセットライト企業で再実行しても変わらない。将来の新規データで NULL が出た場合は `mode=refill` で workflow_dispatch を手動実行すること。
- **`check.py` の日付**は自動計算（祝日は非対応、祝日前後は失敗する場合あり）。
- **URLとHTMLファイル名の対応**を崩さない: `/` ↔ `dashboard.html`、`/collection` ↔ `collection.html`、`/analysis` ↔ `analysis.html`。
- **`CollectionLog.status`** の値: `running` / `done` / `error` / `resolved`（修正済みエラー）。UIは `resolved` を緑扱い。
- **.env は UTF-8（BOMなし）で保存すること**。BOM付きだと最初のキーが読み込めずAPIキーが空になる。
- 本番運用前に `APP_PASSWORD`・`APP_SECRET_KEY`・`APP_RECOVERY_KEY` を必ず設定する。
- **【Tier3 将来対応】** 認証トークンを `localStorage` に保存している（XSS時に盗難リスク）。HttpOnly Cookie 方式への移行は認証フロー全体の再設計が必要。
- **【Tier3 将来対応】** POST リクエストに CSRF トークンなし。Cookie 認証移行後に実施。
- **【実装済み（Tier3-1）】** 重い処理（収集・分析）と認証に `slowapi` でレート制限を導入（収集 3/分・分析 20/分・ログイン 10/分・リセット 3/分・単一更新 10/分）。IP単位（`get_remote_address`）。`APP_RATELIMIT_ENABLED=false` で無効化可能（テスト時等）。環境変数名は slowapi 予約キー `RATELIMIT_*` との衝突を避けるため `APP_` 接頭辞必須。
- **【Tier3 将来対応】** CSP の `unsafe-inline` を削除するには全テンプレートのインラインJS/CSSを外部ファイル化が必要。
