# CLAUDE.md

日本株財務分析ツール。Claude Code への動作指示ファイル。詳細な参照情報は下記ドキュメントへ分離している（毎セッションのトークン節約のため、CLAUDE.md は **索引＋必須ルール** に限定）。

## ドキュメント索引

| 文書 | 内容 | 読むタイミング |
|---|---|---|
| [VISION.md](docs/VISION.md) | プロジェクト目的・ロードマップ・ライブラリ採用基準 | 方針・採用判断時 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 全体構成・ER図・各種フロー図・APIエンドポイント・ファイル役割 | 設計詳細が必要なとき |
| [GOTCHAS.md](docs/GOTCHAS.md) | 既知のハマりどころ（XBRL / CF / capex / 時刻 / 業種 / 認証実装メモ / 進捗仕様） | 収集・分析の実装時 |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Render デプロイ運用＋データ収集の自動/手動の仕組み＋外部サービス制約（GitHub Actions / Supabase / J-Quants） | デプロイ・収集・インフラ設計時 |
| [MODELS.md](docs/MODELS.md) | 分析モデル解説＋モデル固有の制約 | 分析モデル変更時 |
| [PLUGIN_REFERENCE.md](docs/PLUGIN_REFERENCE.md) | `plugins/` 各ファイルの実装リファレンス（内部契約・producer・heavy・実測値）。理論は MODELS.md が正本 | プラグイン実装を触るとき |
| [M1_MACRO_MODEL_GUIDE.md](docs/M1_MACRO_MODEL_GUIDE.md) | M-1（マクロ×リスク-リターン推奨）の初心者向け副読本。予備知識ゼロから設計思想を解説。正式版は MODELS.md §9 | M-1 の考え方を噛み砕いて把握したいとき |
| [SKILLS_AND_AGENTS.md](docs/SKILLS_AND_AGENTS.md) | スキル／エージェントの索引マニュアル | スラッシュコマンドや調査エージェントを使うとき |
| [FUTURE_TASKS.md](docs/FUTURE_TASKS.md) | **Issue 運用ガイド＋設計制約**（残タスクの正本は GitHub Issues。本書はタスク実体を持たない）。完了項目は `docs/archive/IMPROVEMENTS.md` へ集約 | リファクタ着手・改善項目の参照時 |
| [CONTEXT.md](CONTEXT.md) | ドメイン用語集（再分類項目・分析特徴量・表示項目・パラメータ契約・分析の階層等の用語定義） | 用語の定義・整合性を確認したいとき |

> **設計の前に [DEPLOYMENT.md](docs/DEPLOYMENT.md) の「外部サービス制約」節を必ず参照**：無料プラン制約（stooq ブロック・Supabase 500MB・J-Quants レート制限等）に違反しない方式を選ぶこと。

---

## 動作設定

- **日本語応答**。
- **ツール実行前（許可ダイアログが出る場合のみ）**: 次形式の前置きを出力する（allow リスト登録済みで自動実行されるものは不要）:
  ```
  🔧 実行: [操作名] / 目的: [なぜ必要か]
  ↓ 次の許可ダイアログで「許可」を選択してください
  ```
- **ツール実行後**: 結果の要点を日本語で表示。

### サブエージェント運用方針（トークン節約）

- **広範な多ファイル調査・大きいドキュメント（ARCHITECTURE.md / MODELS.md 等）の全文精読は、サブエージェント（`Explore` または `financial-app-explorer`）へ逃がし、結論だけ受け取る**。メインコンテキストに全文を載せないことでトークンを節約する。
- **単純な編集・既知ファイルのピンポイント変更ではエージェントを起動しない**。コールドスタートで再探索が走り、かえって高コストになる。
- 起動するのは「調査範囲が不確実」「複数箇所を横断」「全文読込が必要」なときに限る。

---

## 起動・実行コマンド

```powershell
# ローカル（Windows）— 正本はローカル PostgreSQL（#503・ADR-0038）
./venv/Scripts/Activate.ps1
uvicorn api:app --reload                 # → http://localhost:8000/
python launch.py                         # GUI ランチャー（既定=ローカル正本）
./run_local.ps1                          # 接続先をローカルに固定して起動
./run_local.ps1 -Console -Port 8010      # 同上・ランチャー無しでコンソール起動

# 夜間バッチ（収集 → スコア更新）。GHA の cron は #503 で全停止した
./run_nightly.ps1                        # 手動で1回
./run_nightly.ps1 -DryRun                # 実行計画だけ
./scripts/install_nightly_task.ps1       # タスクスケジューラへ登録（毎日 JST 17:20）

# 月次バッチ（Fama-MacBeth 重み → M-1 マクロβ推論 → M-1/M-2/M-3 探索）。#504
./run_monthly.ps1                        # 手動で1回
./run_monthly.ps1 -DryRun                # 実行計画だけ
./run_monthly.ps1 -Steps factor_premia   # 一部だけ
./scripts/install_monthly_task.ps1       # タスクスケジューラへ登録（毎月1日 JST 01:00・上限16h）

# バックアップ（Supabase Storage・50MB/ファイル・1GB。実測 37.5MB/世代）
python -m scripts.backup_push --apply                 # ローカルに世代を作る
python -m scripts.backup_push --apply --dest storage  # Storage へ push
python -m scripts.backup_restore --apply --create-schema --dest-url <local-url>

python _pipeline_incremental.py         # 差分収集（XBRL＋マクロ＋株価）＝鮮度の担い手
python collector.py --years 5           # 全件収集（5年分）
python collector.py --years 1 --max 10  # テスト用（10社）
python collector.py --company E02167    # 特定企業更新
python collector.py --market            # 株価のみ更新
python collector.py --incremental       # XBRL 差分のみ（**株価は更新しない**）
python collector.py --macro                              # マクロ全系列
python collector.py --macro --macro-series JP10Y_FRED    # 指定系列のみ（定義是正後の再収集用）
python collector.py --repair-price-breaks                # 週次株価の分割段差を検出（dry-run）
python collector.py --repair-price-breaks --persist      # 同上＋該当銘柄をYahooで取り直し検算
python edinet_ping.py                    # EDINET API接続テスト
```

```bash
pytest                      # テスト全件
pytest tests/test_utils.py  # 単一ファイル
```

---

## ファイル構成（主要のみ）

| ファイル | 役割 |
|---|---|
| `database.py` | テーブル定義・upsert・成長率/Zスコア計算 |
| `db_egress.py` | Egress 台帳＋サーキットブレーカ（#478・ADR-0034/0037）。engine の `after_cursor_execute` で全経路の転送を計測。**歯止めは2軸**＝①プロセス予算超過で `EgressBudgetExceeded` ②**請求サイクル累計**（`app_settings.egress_cycle_bytes`・warn 80%/block 95%）。プロセス予算だけでは「1日12プロセスで 4.8GB」が素通りする。台帳は**既定で `.egress/ledger.jsonl`** へ書く（無効化は `FINAPP_EGRESS_LEDGER=0`）。集計は `python -m scripts.egress_report`、閾値監視は `egress-health.yml`（毎日 UTC 21:00） |
| `scripts/batch_common.py` | ローカル駆動バッチ（`run_nightly.py` 日次 / `run_monthly.py` 月次）の**共通骨格**（#504）。`Step` / `Runner` / `BatchSpec` / `run_batch()`。**「走らなかったことを検知する」仕組みの唯一の源**＝ステップ間で止めない・`app_settings` へ足跡（`*_last_run` / `*_last_success`）・失敗は `gh issue create`（通知の失敗は本業を止めない）。**子の出力はログファイルへ直結**する（`PYTHONUNBUFFERED` / `PYTHONIOENCODING` を子へ渡すのが対で必要）＝途中で kill されてもそこまでが残る。溜め込む実装だと「順調に長い」と「死んだ」が区別できない。**待ち合わせは `Popen` ＋ `wait(timeout=HEARTBEAT_SEC)`**（#522）で全ステップに heartbeat が効く（スクリプト側の opt-in にすると登録漏れが構造的に起きる）。ただし**heartbeat は生存を示すが進行を示さない**＝進行の裏取りは CPU 時間 |
| `scripts/mirror_*.py` | ミラーの pull / sync / verify ＋予行演習（#481 B-2〜B-4・ADR-0035）。共有基盤は `mirror_common.py`。**source/dest を引数で受け、両方ローカルなら Supabase 不要で予行できる**。#503 で正本が反転したため **pull / sync は定常運転では使わない**（2026-08-20 の pull が引き渡し点）。`verify` はバックアップの復元先突合に転用する |
| `weekly_price_cache.py` | 週次株価の run 間差分ロードキャッシュ（#480・ADR-0036）。指紋（`max(week_start)`＋`count(*)`）＋直近27週の再取得＋DB 側の世代印（`app_settings`）。**キャッシュは速さだけを担い、正しさは指紋・世代印・行数照合が持つ**（無い/壊れている/古いは全てフルロードへ倒れる）。緊急停止は `FINAPP_WEEKLY_CACHE=0` |
| `collector.py` | オーケストレータ＋後方互換の再エクスポート層＋CLI（実体は下記6分割） |
| `collector_utils.py` | 収集系共通の設定定数・ロガー |
| `collector_master.py` | 企業/業種マスタ収集（EDINET コードリスト・JPX 業種） |
| `collector_financials.py` | XBRL 財務収集・パース・CF/PL-BS 補完・全件収集 |
| `collector_prices.py` | 株価（stooq/J-Quants/Yahoo）・市場データ更新・マクロ収集 |
| `collector_interim.py` | 半期(H1)財務収集（EDINET 半期報告書043A00/旧四半期Q2・period_type='H1'・Issue #219②） |
| `collector_disclosures.py` | 会社予想（ガイダンス）開示収集（J-Quants `/fins/summary`・Issue #322）。`statement_disclosure` へ実績/予想値を蓄積する。特徴量化層 `feature_disclosure.py` の入力元だが、その呼び出し元は `scripts/event_study_*.py` の2本だけで、**本番の API / プラグイン経路からは使われていない**（親 #323 は 2026-07-16 に wontfix クローズ済み）。`recommend_factor_premia.py` は本テーブルに依存せず、`fin_features` は `financial_metrics` VIEW の `z_*`（`plugins/recommend.py::METRICS`）から取る |
| `api.py` | FastAPI アプリ本体（HTMLページ配信・認証/CORSミドルウェア・`/health`）。REST ルート実体は `routers/` へ委譲 |
| `routers/` | REST ルーター5本（`auth` / `collect` / `market` / `analysis` / `morning`）。エンドポイント定義の実体 |
| `plugins/` | 分析モデル（自動検出方式）。理論は [MODELS.md](docs/MODELS.md)、実装詳細は [PLUGIN_REFERENCE.md](docs/PLUGIN_REFERENCE.md) |
| `templates/*.html` | 画面（`/`=dashboard, `/collection`, `/analysis`, `/company/{code}`, `/models`=モデル解説[技術版], `/guide`=やさしい解説[初心者向け]）。JS は `static/js/<page>.js`（`guide.html` は `models.js` を再利用） |
| `_pipeline_gh.py` / `_pipeline_incremental.py` | GitHub Actions 用・全件 / 差分収集 |

完全なファイル役割一覧・処理フロー・ER図は [ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照。

---

## GitHub 協調ワークフロー

デスクトップ版 Claude Code（ここ）と Web版（claude.ai/code）が `kome-kome/financial_app` 経由で協調。PR は Web版でレビュー → main マージ。

| 規模 | ブランチ | 手順 |
|---|---|---|
| バグ修正・小改善 | `main` 直接 | commit → push |
| 機能追加・大きな変更 | `feature/xxx` | branch → push → PR |

### タスク管理（残タスクの正本＝GitHub Issues）

web版・ローカル版の双方が同じ Issue を見ることで、**コードと残タスクの乖離を防ぐ**。タスク実体をメモリや docs に二重記載しない（過去はこの二重記載が乖離源になった）。

- **起票**: `gh issue create`（優先度 `priority:high|medium|low`、本番運用は `ops`）。粒度は「該当／問題／改善案／検証」。
- **着手**: `gh issue list --state open` で確認 → 対応 Issue を見て作業。
- **完了の同期**: PR 本文に `Closes #N` を書く。**main マージで Issue が自動クローズ**＝コード状態と残タスクが構造的に一致する。
- 運用詳細は [FUTURE_TASKS.md](docs/FUTURE_TASKS.md)。

**セッション終了時**: `git status` →（`.env`・機密を除外して）`git add` → `git commit` → `git push`。**残タスクが生じたら Issue 化**してから終了する。

---

## 設計制約（変えてはいけないこと）

- **`upsert_financial` の入力**: `{bs,pl,cf,derived,val,nonfin}`。bs/pl/cf は `bs_` 等プレフィックス付きで DB カラムにマップ、derived は破棄（VIEW で算出）、val/nonfin はプレフィックス無しの直接列へ。**未知キーは silent-drop せず raise**（fail fast）。
  - **補足（`calc_derived` の永続化列との関係）**: 破棄されるのは入力の `derived` キーのみ。`calc_derived`（collector_financials.py）は free_cf / ebitda / nonoperating_income 等を **`cf`/`pl` セクションに入れて返す**ため、これらは `cf_free_cf` / `pl_ebitda` 等の**実列へ永続化される**（VIEW 算出ではない）。「軽い派生（成長率・Zスコア等）は VIEW で都度算出・非永続」と「収集時に確定する派生額は実列へ保存」を区別すること。
- **再分類項目の追加は1箇所**: `FinancialRecord`（database.py）の列に `info={"xbrl": [...]}` で生タグを併記するだけ。`XBRL_MAP`（collector_financials.py）は `build_xbrl_map()` が列 info から逆引き生成するため手書きしない（列定義が唯一の源）。接頭辞なし列（val/nonfin）は `info["section"]` を明示。parse 側の例外ロジック（連結優先度・capex ラベル照合・OperatingRevenue1 フィルタ）は collector_financials.py に残す。
- **`run_full_collection` の `df_master` は常に全件**（`max_companies` で絞らない）。`max_companies` は書類収集件数の上限のみ。`collect_doc_ids_for_period` の `max_companies` は全期間スキャン後に先着N社へ絞る（早期終了禁止）。
- **認証ミドルウェアは `/api/auth/` プレフィックスを常に通過**させる（ログインAPI自体を守ると詰まる）。
- **`/api/gap-analysis` は業種別OLS（sector_ols）実行後でないと404**。`depends_on` を `plugins.ensure_dependencies` が runner（`/api/plugins/{name}/run`→400）と専用エンドポイント（`/api/gap-analysis`→404）で強制する（producer の `produced_output` で判定）。`/api/regression` という実エンドポイントは無く、回帰は `/api/plugins/sector_ols/run` 経由。
- **プラグイン起動は `plugins.execute_plugin(plugin, raw, db)` が単一入口**（runner / `/api/recommend` / `/api/gap-analysis` が共用・テストもこれ）。内部で `coerce_params`→`ensure_dependencies`→`execute` の順。例外（`ValueError`/`DependencyError`）は握らず送出し、各 endpoint の except が HTTP へマップ（gap-analysis→404・runner→400 の差を保つ）。**`execute` は同期（`def`）で実装する（`async def` 禁止・Issue #357）**: execute_plugin が `asyncio.to_thread` でワーカースレッドへオフロードするため、CPU-bound でもイベントループを塞がない（heartbeat watchdog の誤停止防止）。`async def` で実装すると to_thread が未 await のコルーチンを返して壊れる。
- **`params_schema()` はパラメータ契約**（CONTEXT.md「パラメータ契約」）。`type`（ウィジェット）と `dtype`（データ型: int/float/str/list[str]/bool/dict）の2軸を持ち、dtype は `number`/`slider` にのみ明示必須（他は type から推論）。型変換・default 補完・bounds(min/max)/membership(options) 検証は `coerce_params`（`plugins/utils.py`）が一手に担い、**違反は reject（ValueError）**。`execute` は coerce 済み typed params を受け取り、意味的 validation（features 非空・weights 合計≠0 等）だけ持つ。bool ウィジェットは `checkbox` に統一（`boolean`/`bool` 禁止）。
- **CORS は `ALLOWED_ORIGIN` 環境変数で制御**（デフォルト `http://localhost:8000`）。
- **正本はローカル PostgreSQL**（#503・ADR-0038）。Supabase は **2026-08-07 の閲覧用断面（Render 専用）＋ Storage のバックアップ置き場**。2回目の停止の真因は Egress ではなく **NANO の実効メモリ 408MB に DB が乗らないこと**（#500）で、VACUUM FULL 後も余裕は 13〜28MB＝週次株価が増え続ける以上、正本を置く限り必ず再発する。**Supabase の Postgres へ書き戻す経路は作らない**（だから ADR-0035 の dest ローカル限定ガードはそのまま生きる）。収集・スコア更新は `scripts/run_nightly.py`（タスクスケジューラ・JST 17:20）、月次3本（Fama-MacBeth 重み・M-1 マクロβ推論・M-1/M-2/M-3 探索）は `scripts/run_monthly.py`（毎月1日 JST 01:00・#504）が回し、**収集の入口は `_pipeline_incremental.py`**——`collector.py --incremental` は `run_full_collection` だけで**株価を1バイトも更新しない**（取り違えてもエラーは出ない）。バックアップは `scripts/backup_push.py` / 復元は `scripts/backup_restore.py`（Storage は 50MB/ファイル・1GB。表ごと分割で実測 37.5MB/世代）。
- **ミラー（`scripts/mirror_*.py`）の書き込み先はローカル限定**（#481・ADR-0035）。`guard_dest_local()` がリモート dest を `SystemExit` で弾く＝**ローカルから本番 DB へ書く経路をコードとして持たない**（#503 の反転後もこの向きは変わらない）。エンドポイント解決は `database.resolve_database_url()` へ委譲し**二重実装しない**。restore は **FK 依存順に1表ずつ**（ダンプの TOC はアルファベット順であって `--table` の指定順ではない）。週次の再取得窓は `DAILY_WINDOW_DAYS` から導出（27週）。ミラー範囲は全18表から `xbrl_raw_documents`（0行）を除いた**17表**＝`stock_price_daily` は #503 で範囲へ入れた（正本が移れば daily はローカルにしか無い正本データになる）。
- **週次株価の差分ロード（`weekly_price_cache.py`）は「速さだけ」を担う**（#480・ADR-0036）。正しさは①指紋（`max(week_start)`＋`count(*)`）②DB 側の世代印③行数照合の3つが持ち、**どれかが外れたら必ずフルロードへ倒す**（「不一致だが続行」の分岐を作らない）。触るときの不変条件は3つ: **差分条件 `week_start >= :since` は既存の500社チャンクの中に足す**（単独では PK 先頭列にならず seq scan）／**SELECT の列を増やさない**（`EGRESS_COST_TABLE` の4列較正は volume 込みの値・キャッシュ側は ISO 週の不変条件から `trade_date` で切れる）／**キャッシュのワイヤ形式は素タプル**（`_VOLUME_NOT_LOADED` は pickle で同一性が壊れ `px_volz` が全 nan 化する）。過去週を遡って書き換える処理を足したら世代印を進めること（`_recompute_weeks_from_daily` の構造的条件で自動的に進むが、経路によっては明示 bump が要る）。`27週` の導出は `database.WEEKLY_OVERLAP_DAYS` が唯一の源でミラー同期と共有する。
- **接続先は `FINAPP_DB_TARGET`（`local` 既定 / `prod`）で切り替える**（#481 B-1・**#503 で既定を反転**・`database.resolve_database_url()`）。既定が local なのは正本がローカルだから＝**`.env` に `DATABASE_URL` があるだけでは Supabase へ行かない**。`prod` を明示するのは `render.yaml` の1箇所だけ（無いと Render は localhost を見にいき「接続失敗」ではなく**空の DB に繋がって0件**に化ける）。**保険として、`FINAPP_DB_TARGET` 未設定でも `RENDER` 環境変数があれば prod へ倒す**（Render は `RENDER=true` を必ず設定する＝Blueprint 管理外で render.yaml の env が反映されない構成でも空DBを掴まない）。明示指定は常に最優先。`local` は `DATABASE_URL_LOCAL`（未設定ならローカル既定）を使い、**解決先がリモートなら import 時に `RuntimeError`**。逆に **`prod` で `DATABASE_URL` 未設定は警告どまりで raise しない**（反転前は `ci.yml` がこの経路を踏んでいた。緩さ自体は Render 側で生きている）。未知の target 値は `ValueError`。接続先は `/api/system/info` → `static/js/common.js` のバッジで全画面に出す（ローカル時のみ）。`launch.py` は既定値を文字列で写しているので `tests/test_db_target.py` が database 側と照合する。
- **分析モデルの次元整合性（必須）**: 説明変数と被説明変数は同一次元（per-share財務金額[円/株]→株価[円/株]の Ohlson 型）。OLS学習前に各特徴量を `winsorize`（p1-p99、`plugins/utils.py`）。詳細・根拠は [MODELS.md](docs/MODELS.md)。
- **科学計算ライブラリ**（numpy/scipy/statsmodels/scikit-learn）は利用可。採用基準は [VISION.md](docs/VISION.md)。

---

## パッケージ管理（pip install 前に必須）

1. **セキュリティ評価**: WebSearch で「パッケージ名 + CVE / security vulnerability」を検索
2. **評価提示**: バージョン・既知CVE・DL数・総合判定（✅低 / ⚠️注意 / ❌高リスク）
3. **ユーザーの明示承認を得てから実行**

`requirements.txt` は完全 pin（`==`）。アップグレードは単独 PR + `pytest` + 主要画面確認をセットで。

**ローカル探索専用の依存は `requirements-optional.txt` へ**（本番 `requirements.txt` に入れない＝Render 無料プランのビルド footprint を増やさない）。導入は `pip install -r requirements-optional.txt`。未導入でもアプリは無影響で動くこと（lazy import ＋ 機能側で自動スキップ）が条件。正式採用（本番コードパスで必須化）に昇格した時点で `requirements.txt` へ移す。

---

## ドキュメント更新ルール（コード変更と同じ作業内で必須）

- **ARCHITECTURE.md**: DBテーブル / 処理フロー / APIエンドポイント / 画面 / プラグインを追加・変更したら対応セクションを更新。
- **MODELS.md** と `templates/models.html`: 分析モデル追加・変更時に更新。参考文献は原著論文の DOI / 公式 URL（Wikipedia不可）。
- **MODELS.md §9（M-1）の章立てを変えたら初心者向け副読本 `docs/M1_MACRO_MODEL_GUIDE.md` も見直す**（Issue #472）: 副読本は**設計思想・章立てのみ追随**し、マクロ系列の全リスト・既定値・実測値は正本へのリンクに留める（書き写すと黙って陳腐化する）。見直し後、副読本冒頭の `models-sync` マーカーを更新すること。`tests/test_docs_sync.py` が CI で照合し、**乖離は失敗として現れないので通知では拾えない**（ADR-0031 と同型）。
- **`heavy=True` のプラグインを追加したら自動実行を登録する**（ADR-0031）: `nightly_scores.HEAVY_AUTOMATION` へ「回すワークフロー名」か `exempt: <理由>` を必ず足す。未登録・実在しないワークフロー名・空理由は `tests/test_nightly_scores.py::TestHeavyAutomationRegistry` が CI で落とす。**「heavy を足したが自動実行が無い」は失敗しないので通知では拾えない**（#432/#443/#423 子5 で3回発生）。
- **断面の前処理を変えたら `plugins/utils.py::PREPROCESS_VERSION` を上げる**（ADR-0039・#517）: `recommend_factor_premia` の `mean_b` は「その前処理での1単位あたり」であり、前処理を変えると**永続化済みの重みの意味が変わる**。世代を上げれば `get_dynamic_preset` が旧世代の行を採らずバランス型へ倒し、`factor_premia` を回すまで安全側に留まる。**上げ忘れは CI で拾えない**（前処理の変更を機械的に検出する手段が無い）＝ #509 で実際に旧単位の重み × 新単位の特徴量が本番へ出た（実測 rank-IC −0.0881）。
- **分析プラグイン追加・変更時のメタ検証網羅性**: 独自のランキング/スコアを出すプラグインは、`/api/backtest` の `SCORING_SOURCES`（backtest.py）へ追加するか、`plugins/macro_snapshots.py::oof_backtest` による OOF 評価を実装するかをその場で判断する。対象外にする場合は理由をコード内コメントまたは ADR に明記する（「後で対応」と ADR の prose に書くだけで終わらせない＝ Issue #272 のように M-1 だけ OOF 未対応のまま放置された実例あり）。比較ファミリー（M-1/M-2/M-3 等）内で1モデルだけ評価手段が欠けていないか確認する。
- **HTML 構成**: CSS はインライン1ファイル維持（分割禁止）。JS は CSP 対応で `static/js/<page>.js` に外部化（インラインイベントハンドラは `data-*`＋イベント委譲）。
- ファイル名・URL は機能名で命名（フェーズ番号禁止）。定数はファイル冒頭に集約。
- **軽量化の継続**: 機能追加・リファクタ後は `/tidy` を叩いてデッドファイル・壊れリンク・doc⇔code 乖離を点検すること。

---

## テスト方針

実装後は必ず Claude 自身が Python でテストを実行し、動作確認してから報告する。`tests/` は `pytest.ini` で `testpaths=tests` 固定。プラグイン追加時は `tests/test_<plugin>.py` を作成。
