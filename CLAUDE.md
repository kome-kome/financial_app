# CLAUDE.md

日本株財務分析ツール。Claude Codeへの動作指示ファイル。プロジェクト目的・方針は [VISION.md](docs/VISION.md) を参照。

## デプロイ環境（最重要）

**本プロジェクトは [Render](https://render.com/) にデプロイ済みで稼働中**。DB は **Supabase PostgreSQL**（外部）。
新機能・改修は必ず Render Free プランの制約を前提に設計すること（メモリ 512MB・スピンダウン 15 分・SSH 不可）。
詳細は [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) を **必読**。

> **🔄 進行中の構造改修**: ローカル PG → Supabase への一本化、および XBRL 生データ
> 中間テーブル追加を進行中。設計書 [docs/REFACTORING.md](docs/REFACTORING.md) を参照。
> 移行用スクリプトは `scripts/compare_db_counts.py`、`scripts/migrate_local_to_supabase.py`
> （後者は実装予定）。実 DB 操作は手元 PC から行う（Web 版 Claude Code は到達不可）。

主要ポイント（うっかり破らないために）:
- マイグレーションは `init_db()` 内で **冪等** に実装（起動時に自動実行）
- 永続化はすべて Supabase。ローカルファイル書き込みは再デプロイで消える
- 長時間処理は `BackgroundTasks` + SSE 進捗配信（30 秒で HTTP タイムアウト）
- 環境変数は `render.yaml` の `envVars` に追記し Render ダッシュボードで値を設定
- 設定変更は GitHub `main` への push で自動デプロイ。ロールバックは Render ダッシュボードから可能
- **定時スケジューラ (`_daily_scheduler`) は Free プランでは走らない**。15 分アイドルで停止するため、毎日3時に起動している保証がない。代わりに `_startup_catchup` がスピンアップ時に「最終自動収集から 22h 以上経過していたら差分収集を非同期実行」する。新規の定期処理を追加する場合は同じパターン（startup hook + 経過時間判定）で実装すること
- **scheduler は JST 固定**: `api.py:_now_jst()` ヘルパで Render の OS TZ (UTC) に依存させない。新しい時間帯固定処理 (○時に実行 等) は `datetime.now()` ではなく `_now_jst()` を使うこと
- **GitHub Actions keepalive (`.github/workflows/keepalive.yml`)** が JST 2:50-3:10 と業務時間 9-23 JST に `/health` を叩く。月稼働時間は約 475h で Render Free 750h 無料枠内。cron 変更時は枠を超えないか確認
- **全件収集は GitHub Actions runner で実行**（Render Free の 30秒タイムアウト・512MB メモリでは完走しない）。`.github/workflows/full-pipeline.yml` を `workflow_dispatch` で起動 → `_pipeline_gh.py` が Supabase に直接書き込む。ログは Artifact (`pipeline-log`, 7日保存)。差分収集など軽量処理だけ Render 側の `_startup_catchup` に残す方針

## GitHub 協調ワークフロー

**デスクトップ版 Claude Code（ここ）とWeb版（claude.ai/code）が `kome-kome/financial_app` を介して協調する。**

### ブランチ戦略

| 規模 | ブランチ | 手順 |
|---|---|---|
| バグ修正・小改善 | `main` 直接 | commit → push |
| 機能追加・大きな変更 | `feature/xxx` | branch → commit → push → PR |

### セッション終了時の必須手順（最重要）

作業完了後は **必ず** 以下を実行してからセッションを終了すること。
Web版 Claude Code は GitHub のコードを参照するため、push しないと変更が共有されない。

```powershell
# 変更を確認
git status

# ステージング（変更ファイルを個別に指定。.env や機密ファイルを含めないこと）
git add [変更ファイル]

# コミット（変更内容を端的に記述）
git commit -m "変更内容の説明"

# プッシュ
git push origin main          # main の場合
git push origin feature/xxx   # feature ブランチの場合
```

### feature ブランチ → PR のフロー

```powershell
git checkout -b feature/xxx   # ブランチ作成
# ...実装...
git add [ファイル]
git commit -m "..."
git push origin feature/xxx
gh pr create --title "タイトル" --body "説明"  # PR 作成
```

PR はWeb版 Claude Code でレビュー → コメント → main へマージ。

---

## 動作設定
- 日本語応答
- **ツール実行前（必須・最重要）**: 許可ダイアログが出る可能性があるコマンドを実行する前に、
  必ず以下の形式でテキストを出力してから tool use を呼ぶこと。
  ユーザーはダイアログに表示される生のコマンド文字列だけでは意味が分からないため、
  目的を日本語で先に伝えることが最重要。

  ```
  🔧 実行: [操作名（例: パッケージ一覧確認、サーバー起動、DB書き込み確認）]
  目的: [なぜこのコマンドが必要か]
  ↓ 次の許可ダイアログで「許可」を選択してください
  ```

  例（pip list の場合）:
  ```
  🔧 実行: インストール済みパッケージの確認
  目的: 現在の venv にインストールされているパッケージとバージョンを確認するため
  ↓ 次の許可ダイアログで「許可」を選択してください
  ```

  **例外（説明不要）**: allow リストに登録済みで自動実行されるコマンドは出力不要。
  許可ダイアログが表示されるコマンド（deny・未登録）には必ず出力する。

- ツール実行後: 結果の要点を日本語で表示（「完了しました」または結果サマリ）

## 起動・実行コマンド

```powershell
.\venv\Scripts\Activate.ps1             # 仮想環境の有効化
uvicorn api:app --reload                # サーバー起動
# UI: http://localhost:8000/（ダッシュボード）
#     http://localhost:8000/collection（収集・スクリーニング）
#     http://localhost:8000/analysis（回帰・乖離分析）

python collector.py --years 5           # 全件収集（5年分）
python collector.py --years 1 --max 10  # テスト用（10社）
python collector.py --company E000001   # 特定企業更新
python collector.py --market            # 株価のみ更新
python collector.py --incremental      # 差分収集（収集済みをスキップ）
python check.py                         # EDINET API接続テスト
```

## ファイル構成

| ファイル | 役割 |
|---|---|
| `database.py` | テーブル定義・upsert・成長率/Zスコア計算 |
| `collector.py` | EDINET+stooqからデータ収集→DB保存 |
| `api.py` | FastAPI REST・SSE・回帰分析 |
| `templates/dashboard.html` | トップページ・ダッシュボード（`/`） |
| `templates/collection.html` | 収集・スクリーニング画面（`/collection`） |
| `templates/analysis.html` | 回帰・乖離分析画面（`/analysis`） |
| `check.py` | EDINET API疎通確認用ワンショット |
| `render.yaml` | Render デプロイ定義（IaC） |
| `Procfile` | Render の起動コマンド（uvicorn 起動） |
| `docs/DEPLOYMENT.md` | **Render デプロイ運用ガイド（必読）** |

## 収集フロー別進捗仕様

長時間処理はすべてリアルタイム進捗をUIに届けること。`on_progress(current, total, message)` コールバックを通じてSSEに流す。

| フェーズ | 進捗メッセージ形式 |
|---|---|
| 企業マスタ保存 | `[企業マスタ保存] X/Y社完了` |
| 書類一覧スキャン | `[書類スキャン X/Y日] YYYY-MM-DD  累計 Z社` |
| XBRL取得・保存 | `[X/Y] 企業名(証券コード) 決算期末` |
| 差分スキップ | `[X/Y] スキップ（収集済み）: 企業名 決算期末` |

SSEエンドポイント: 収集=`/api/collect/stream`、市場データ=`/api/collect/market-stream`

## 設計制約（変えてはいけないこと）

- `upsert_financial` の入力: `{bs,pl,cf,derived,val}`。bs/pl/cfは`bs_`等プレフィックス付きでDBカラムにマップ、derived/valはそのまま。XBRL項目追加時は`XBRL_MAP`（collector.py）と`FinancialRecord`（database.py）の両方を更新。
- `ols()`（`plugins/utils.py`）は現状 Pure Python 実装だが、numpy/scipy/statsmodels/scikit-learn 等の成熟ライブラリの利用は **許可** されている（`docs/VISION.md` 「サードパーティーライブラリ採用基準」参照）。新規導入時は同基準に従い、PyPI DL 数・GitHub Stars・CVE 履歴を WebSearch で評価しユーザー承認を得ること。
- CORS は `ALLOWED_ORIGIN` 環境変数で制御（デフォルト: `http://localhost:8000`）。本番は `.env` に正しいオリジンを設定すること。
- `/api/gap-analysis` は `/api/regression` 実行後でないと404になる。
- 認証ミドルウェアは `/api/auth/` プレフィックスを常に通過させる（ログインAPI自体を守ると詰まる）。
- `run_full_collection` の `df_master` は **常に全件**（`max_companies` で絞らない）。`max_companies` は書類収集件数の上限のみ。
- `collect_doc_ids_for_period` の `max_companies` は**全期間スキャン後に先着N社へ絞り込む**方式（早期終了禁止）。

## 分析手法の既知問題・制約

- **Zスコアは年度別に計算すること**。`calc_zscore_normalization` は年度を跨いで計算しない（異なるマクロ環境の年を混在させると比較が無意味になる）。内部的に `_calc_zscore_for_year(db, year)` を年度ごとに呼ぶ。
- **gap_analysis の収束予測**: 履歴 ≥ 8 観測の銘柄は `statsmodels` ARIMA(1,0,0) による AR(1) MLE で半減期を推定（`HL = -ln(2)/ln(φ)`）。履歴不足の銘柄はヒューリスティック（`half_life = |gap|/2`）にフォールバックする。`conv_score = 50 + gap×0.8` は引き続きヒューリスティックなので「参考値」として扱うこと。出力の `method` フィールドで AR(1) / ヒューリスティックの判別が可能。
- **成長率計算は (edinet_code, year, period_end) で副ソート**済み。同年複数レコードがある企業の前期比が不定にならないようにしている。
- **フリーCF = 営業CF + 投資CF**（設備投資以外の投資活動も含む近似値）。
- **市場データの株数推計** = `total_equity / bps`（発行済株式数の近似）。IFRS・JGAAP混在時に精度が下がる場合あり。
- **単位の例外**: `market_cap` のみ百万円。`pl_revenue` 等は円。直接比較・演算しないこと。
- **分析モデルの次元整合性（必須）**: 説明変数と被説明変数は同一次元で設計すること。無次元量（比率・マージン等）で絶対額（株価・時価総額）を予測するのは次元的に不整合でOLS係数が経済的に解釈できなくなる。✅ 正しい例: per-share財務金額[円/株]（EPS/BPS/DPS）→ 株価[円/株]（Ohlsonモデル型）　❌ 誤った例: op_margin[%]・equity_ratio[%] → market_cap[百万円]
- **財務データの外れ値処理（必須）**: OLS学習前に各特徴量を winsorize（p1-p99クリッピング）すること。日本株データはBPS・EPSにp99の数百倍の外れ値が存在し、無処理ではOLS（行列反転）が数値的に破綻する（R²が-10³²になる等）。実装は `plugins/utils.py` の `winsorize()` を使用。
- **横断的R²の解釈**: プラグインが返す `cv_metrics.mean_r2` は横断的OLSの評価指標。全業種一括回帰では業種間のP/E・P/Bの構造差により構造的に低くなる（-0.1〜0.4程度）。R² < 0 はモデルが無価値ではなく「業種固定効果なしの一括回帰の限界」を反映している。ランキングの有用性はR²ではなく銘柄選択の将来パフォーマンスで評価する。

## 業種データの取得方法

- **業種はXBRLから取得できない**。EDINETのXBRLに TSE 33業種コードは含まれていない。
- **正規ソース**: JPX上場会社一覧Excel (`JPX_EXCEL_URL` = `data_j.xls`, 33業種コード列=col4/col5)
- `update_industry_from_jpx(client, db)` がJPX Excelをダウンロードして Company/FinancialRecord を更新する。`run_full_collection` の末尾で自動実行される。
- 証券コードは4桁数字(`1301`)とアルファベット混在(`350A`)の両形式がある。両方に対応済み。
- `xlrd` と `openpyxl` をvenvに追加済み（`pip install xlrd openpyxl`）。

## J-Quants API の設計制約

- **認証情報**: `.env` に `JQUANTS_API_KEY` を設定（J-Quants ダッシュボードの「API Keys」ページから取得。有効期限なし）。未設定時は `ValueError` で明示エラー。
- **データ優先度**: J-Quants = JPX公式 → stooq より正確。`ON CONFLICT DO UPDATE` で上書き（stooq は `ON CONFLICT DO NOTHING`）。
- **コード変換**: J-Quants は5桁コード（例: `"13010"`）。先頭4桁が証券コード（`code[:4]`）。アルファベット混在コード（`"350A"`）も同様。
- **取得単位**: 日付単位（`date` パラメータ）で全銘柄を一括取得。1営業日 = 1〜数リクエスト（ページネーション対応済み）。
- **無料プランの上限**: 過去2年分（`days_back ≤ 730`）。UI の選択肢もこれに合わせること。
- **`close` は nullable=False**: J-Quants から `Close` が `None` の行はスキップ（停止銘柄等）。
- **レート制限の実測値**: 無料プランは約5リクエスト/60秒が上限とみられる。`JQUANTS_RATE_SLEEP = 20.0` で20秒間隔を確保。データ日（約8秒/レスポンス）でも非営業日（約3秒/レスポンス）でも間隔が20秒未満になる場合は自動的に補完スリープを入れる。
- **429 リトライ戦略**: 指数バックオフは採用しない（リトライ自体がクォータを消費してさらに429を招くため）。429 発生時は90秒待機してから1回だけ再試行し、それでも429 なら skip。
- **CardinalityViolation 対策**: J-Quants は5桁コード（普通株・優先株等）が同じ4桁 sec_code にマップされる場合がある。INSERT前に edinet_code で重複排除（先着1件採用）。

## パッケージ管理方針（pip install）

`pip install` の実行前に、必ず以下の手順を踏むこと：

1. **セキュリティリスク評価を実施** — WebSearch でパッケージ名＋"CVE" / "security vulnerability" / "malicious" を検索し、最新情報を収集する
2. **評価結果を以下の形式で提示する**：
   - パッケージ名・インストール予定バージョン
   - 既知のCVE・脆弱性（あれば番号と概要）
   - PyPI / OSV.dev / Snyk 等での評価サマリ
   - メンテナ状況・ダウンロード数（信頼性の目安）
   - 総合判定：✅ 低リスク / ⚠️ 要注意 / ❌ 高リスク
3. **ユーザーの明示的な承認を得てから実行** — 評価提示後、承認の返答があるまで `pip install` を実行しない

**バージョン pin**: `requirements.txt` は完全 pin（`==`）。numpy/pandas/scipy/scikit-learn/statsmodels も特定バージョンで固定済み（科学計算系の break change を避けるため）。アップグレードは単独 PR で行い、`pytest` と主要画面の動作確認をセットで実施すること。

## リファクタリング方針

- ファイル名・URL：機能名で命名（フェーズ番号禁止）
- HTML：CSS/JSインライン1ファイル維持（分割禁止）
- ライブラリ追加：仮想環境変更を伴う場合はCLAUDE.mdに記載
- 定数：ファイル冒頭に集約（コード中にハードコード禁止）
- **`docs/ARCHITECTURE.md` の随時更新（必須）**: コード変更・機能追加・リファクタリングを行ったら、同じ作業内で `docs/ARCHITECTURE.md` も更新すること。`docs/ARCHITECTURE.md` はプロジェクトの設計を網羅的に記述したドキュメントであり、常にコードと同期した状態を保つ。更新対象の対応は以下の通り。
  - DBテーブル追加・変更 → ER図（セクション3）
  - 新しい処理フローの追加 → 該当シーケンス図を追加（セクション4）
  - APIエンドポイント追加 → コンポーネント図・エンドポイント一覧（セクション1・8）
  - HTMLタブ・画面追加 → 画面遷移図（セクション5）
  - プラグイン追加 → クラス図（セクション7）
  - ファイル・モジュール追加 → ファイル役割一覧（セクション10）
- **`docs/MODELS.md` と `templates/models.html` の随時更新（必須）**: 新しい分析モデルを追加・変更したとき、または既存モデルの数式・パラメータ・仮定を変更したときは、同じ作業内で以下を更新すること。
  - `docs/MODELS.md` — モデル番号・名称・数式・パラメータ表・既知問題・参考文献（DOIリンク付き）を Markdown で追記/更新
  - `templates/models.html` — 同内容を `.formula-block` / `.param-table` スタイルで HTML に反映し、ブラウザの `/models` ページから閲覧できる状態を保つ
  - 参考文献は必ず原著論文の DOI または公式 URL を記載すること（二次資料・Wikipedia 不可）

## テスト方針

実装後は必ずClaude自身がPythonで直接テストを実行し、動作を確認してからユーザーに報告する。

### テスト優先順位・制限時間

| 種別 | 内容 | 制限時間 |
|---|---|---|
| API疎通 | EDINET/stooq レスポンス確認 | 30秒 |
| 収集ロジック | 3社分の収集→DB書き込み確認 | 2分 |
| 収集フル | 10社分の収集→スクリーニング確認 | 5分 |
| UI確認 | API エンドポイントの HTTP 200 確認 | 10秒 |

```powershell
# 基本パターン（タイムアウト付き）
.\venv\Scripts\python.exe -c @"
import asyncio, os, sys
os.environ['EDINET_API_KEY'] = '...'
os.environ['DATABASE_URL'] = '...'
sys.path.insert(0, '.')
asyncio.run(test())
"@ 2>&1 | Select-String -Pattern "テスト|FAIL|ERROR|companies|records"
```

### pytest（プラグイン・ユーティリティの単体テスト）

`tests/` 配下は `pytest.ini` で `testpaths=tests` に固定。プラグイン追加時は `tests/test_<plugin>.py` を作成すること。

```powershell
.\venv\Scripts\Activate.ps1
pytest                                              # 全件
pytest tests/test_utils.py                          # 単一ファイル
pytest tests/test_utils.py::test_winsorize_basic    # 単一関数
```

## 既知の注意事項

- **EDINET XBRL CSV** は UTF-8 と UTF-16 LE（タブ区切り）が混在。`fetch_xbrl_csv` で両方対応済み。
- **XBRL要素選択**: 連結優先判定は `"NonConsolidated" not in ctx` を必ず含めること。含めないと非連結が連結を上書きする。優先度：連結=2 > 非メンバー=1 > メンバー付き=0。
- **`check.py` の日付**は自動計算（祝日は非対応、祝日前後は失敗する場合あり）。
- **URLとHTMLファイル名の対応**を崩さない：`/` ↔ `dashboard.html`、`/collection` ↔ `collection.html`、`/analysis` ↔ `analysis.html`。
- **`CollectionLog.status`** の値: `running` / `done` / `error` / `resolved`（修正済みエラー）。UIは `resolved` を緑扱い。
- **.env は UTF-8（BOMなし）で保存すること**。BOM付きだと最初のキーが読み込めずAPIキーが空になる。
- 本番運用前に `APP_PASSWORD`・`APP_SECRET_KEY`・`APP_RECOVERY_KEY` を必ず設定する（`APP_SECRET_KEY` 未設定時は起動時 Warning が出る）。
- ~~【Tier2 既知リスク・要対応】 `api.py` のパスワード比較は `hmac.compare_digest()` を使うこと（タイミング攻撃対策）。~~ → **対応済み**（`auth_login` で `hmac.compare_digest()` 適用）
- ~~【Tier2 既知リスク・要対応】 プラグインエラーは `detail=str(e)` をそのまま返さず、ログ出力してユーザーには汎用メッセージを返すこと（情報漏洩防止）。~~ → **対応済み**（`except Exception` を全プラグインエンドポイントに追加、`log.error()` でサーバーログに出力）
- ~~【Tier2 既知リスク・要対応】 CORS `allow_origins=["*"]`。~~ → **対応済み**（`ALLOWED_ORIGIN` 環境変数で制御、デフォルト `http://localhost:8000`）
- ~~【Tier2 既知リスク・要対応】 `login.html` の `next` パラメータが未検証リダイレクト。~~ → **対応済み**（正規表現で相対パスのみ許可）
- ~~【Tier2 既知リスク・要対応】 EDINET ZIP の展開サイズ無制限（ZIP爆弾リスク）。~~ → **対応済み**（`collector.py` で200MB上限チェック）
- ~~【Tier2 既知リスク・要対応】 `edinet_code` パスパラメータの形式検証なし。~~ → **対応済み**（`^E\d{6}$` 正規表現バリデーション追加）
- ~~【Tier2 既知リスク・要対応】 パスワードリセット時の最低文字数チェックなし。~~ → **対応済み**（8文字以上を要求）
- ~~【Tier2 既知リスク・要対応】 スケジューラーエラーの `str(e)` をAPIレスポンスにそのまま含める。~~ → **対応済み**（汎用メッセージに差し替え、詳細はサーバーログのみ）
- **【Tier3 将来対応】** 認証トークンを `localStorage` に保存している（XSS時に盗難リスク）。HttpOnly Cookie 方式への移行は認証フロー全体の再設計が必要。
- **【Tier3 将来対応】** POST リクエストに CSRF トークンなし。Cookie 認証移行後に実施。
- **【Tier3 将来対応】** 重い処理（収集・分析）にレート制限なし。`slowapi` 等の導入が必要。
- **【Tier3 将来対応】** CSP の `unsafe-inline` を削除するには全テンプレートのインラインJS/CSSを外部ファイル化が必要。
