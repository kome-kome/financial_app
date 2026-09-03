# CLAUDE.md

日本株財務分析ツール。Claude Code への動作指示ファイル。詳細な参照情報は下記ドキュメントへ分離している（毎セッションのトークン節約のため、CLAUDE.md は **索引＋必須ルール** に限定）。

## ドキュメント索引

| 文書 | 内容 | 読むタイミング |
|---|---|---|
| [VISION.md](docs/VISION.md) | プロジェクト目的・ロードマップ・ライブラリ採用基準 | 方針・採用判断時 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 全体構成・ER図・各種フロー図・APIエンドポイント・ファイル役割 | 設計詳細が必要なとき |
| [GOTCHAS.md](docs/GOTCHAS.md) | 既知のハマりどころ（XBRL / CF / capex / 時刻 / 業種 / 認証実装メモ / 進捗仕様 / **ローカルバッチの実行環境**） | 収集・分析の実装時 |
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

# バッチ鮮度 watchdog（走らなかったことを検知して起票）。#515・ADR-0042
python -m scripts.check_batch_freshness             # 判定（停止なら起票 + exit 2）
./run_watchdog.ps1 -DryRun                          # 起票せず本文だけ見る
./run_watchdog.ps1 -DryRun -Now 2026-08-28T00:00:00+00:00   # 欠落を再現（DB を汚さない）
./scripts/install_watchdog_task.ps1                 # タスクスケジューラへ登録（毎日 JST 20:00）

# 月次バッチ（Fama-MacBeth 重み → M-1 マクロβ推論 → M-1/M-2/M-3 探索）。#504
./run_monthly.ps1                        # 手動で1回
./run_monthly.ps1 -DryRun                # 実行計画だけ
./run_monthly.ps1 -Steps factor_premia   # 一部だけ
./scripts/install_monthly_task.ps1       # タスクスケジューラへ登録（毎月1日 JST 01:00・上限16h）

# M-1 探索は別タスク（毎月2日 JST 01:00）。実測 約752分で月次本体の窓に入らない（#584・ADR-0046）
./run_monthly_m1.ps1                     # 手動で1回
./run_monthly_m1.ps1 -DryRun             # 実行計画だけ
./scripts/install_monthly_m1_task.ps1    # 登録（毎月2日 JST 01:00・上限16h）。**登録後に1回手動実行して足跡を入れる**

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

# Yahoo が遡及反映しない分割を公式(J-Quants AdjFactor)の裏付け付きで直す（#466）。既定はドライラン
python -m scripts.repair_splits_from_jquants                   # 検出→判定（書かない）
python -m scripts.repair_splits_from_jquants --only E03137     # 1社だけ（検出を省く）
python -m scripts.repair_splits_from_jquants --apply

# 地方取引所の単独上場を拾う（#555）。既定はドライラン＝棄却理由まで出す
python -m scripts.resolve_price_suffix                            # 何も書かない
python -m scripts.resolve_price_suffix --apply --backfill-weekly  # 採用＋5年weekly
python -m scripts.resolve_price_suffix --reprobe                  # 解決済みも測り直す
python -m scripts.resolve_price_suffix --apply --bucket empty     # 取引所判明・バー0本の5社だけ（月次が回す・#560）
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
| `db_egress.py` | Egress 台帳＋サーキットブレーカ（ADR-0034/0037）。engine の `after_cursor_execute` で全経路を計測。**歯止めは2軸**＝プロセス予算と**請求サイクル累計**（前者だけでは日をまたぐ累積が素通りする）。集計は `python -m scripts.egress_report`。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `scripts/batch_common.py` | ローカル駆動バッチ（日次／月次／M-1）の共通骨格＝**「走らなかったことを検知する」仕組みの唯一の源**（足跡・通知・heartbeat・ステップ予算）。**子の出力はログへ直結する／生の接続文字列は出さない／Σ予算＋マージン ≤ 窓**。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `batch_freshness.py` | バッチ足跡（`*_last_run`）を**測る**層（#561）。watchdog と `/api/morning` の2人が共有する。**閾値は `run_*.WINDOW_MIN` から導出し書き写さない**。**API から `scripts/check_batch_freshness.py` を import しない**（import 時に接続先を書き換える）。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `scripts/check_batch_freshness.py` | **バッチ鮮度 watchdog**（ADR-0042）。起票・CLI・ログを持ち、判定は `batch_freshness.py` と共有する。**閾値は `cadence + 窓` の導出（実測から逆算しない）／判定は `*_last_run` のみ／通知経路（`gh`）は健全な回にも毎回確かめる**。exit 0/2/3。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `scripts/mirror_*.py` | ミラーの pull / sync / verify（ADR-0035・共有基盤 `mirror_common.py`）。**書き込み先はローカル限定**。#503 の正本反転により **pull / sync は定常運転では使わない**（`verify` はバックアップ復元先の突合へ転用）。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `weekly_price_cache.py` | 週次株価の run 間差分ロードキャッシュ（ADR-0036）。**速さだけを担い、正しさは指紋・世代印・行数照合が持つ**＝どれかが外れたら必ずフルロードへ倒す。緊急停止は `FINAPP_WEEKLY_CACHE=0`。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `sysmem.py` | 常駐メモリ／物理メモリ実測の**唯一の源**（`batch_common` の heartbeat・`bench_macro_beta` が共有）。**ctypes を書き写さない／psutil は入れない／測るのはプロセスツリーの合計**（単体 pid は静かに誤る）。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `collector.py` | オーケストレータ＋後方互換の再エクスポート層＋CLI（実体は下記6分割） |
| `collector_utils.py` | 収集系共通の設定定数・ロガー。**`EDINET_BASE` は `api.edinet-fsa.go.jp`**（旧 `disclosure.` は `follow_redirects` では直らない・#577）。**例外文字列は `redact_secrets()` で API キーを消す**／**「走ったが全部失敗した」を失敗として現す**。詳細は [GOTCHAS.md](docs/GOTCHAS.md) |
| `collector_master.py` | 企業/業種マスタ収集（EDINET コードリスト・JPX 業種） |
| `collector_financials.py` | XBRL 財務収集・パース・CF/PL-BS 補完・全件収集 |
| `collector_prices.py` | 株価（stooq/J-Quants/Yahoo）・市場データ更新・マクロ収集 |
| `collector_interim.py` | 半期(H1)財務収集（EDINET 半期報告書043A00/旧四半期Q2・period_type='H1'・Issue #219②） |
| `collector_disclosures.py` | 会社予想（ガイダンス）開示収集（J-Quants `/fins/summary`）。`statement_disclosure` へ蓄積する。**本番の API / プラグイン経路からは使われていない**（利用は `scripts/event_study_*.py` の2本のみ・親 #323 は wontfix）。詳細は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| `api.py` | FastAPI アプリ本体（HTMLページ配信・認証/CORSミドルウェア・`/health`）。REST ルート実体は `routers/` へ委譲 |
| `routers/` | REST ルーター5本（`auth` / `collect` / `market` / `analysis` / `morning`）。エンドポイント定義の実体 |
| `plugins/` | 分析モデル（自動検出方式）。理論は [MODELS.md](docs/MODELS.md)、実装詳細は [PLUGIN_REFERENCE.md](docs/PLUGIN_REFERENCE.md) |
| `templates/*.html` | 画面テンプレート。JS は CSP 対応で `static/js/<page>.js` へ外部化。**ダッシュボードとログイン以外の全画面はグローバルナビ `.gnav` を持つ**——貼り忘れは失敗として現れないので `tests/test_templates_nav.py` が照合する。画面一覧は [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
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
- **正本はローカル PostgreSQL**（#503・ADR-0038）。Supabase は **2026-08-07 の閲覧用断面（Render 専用）＋ Storage のバックアップ置き場**。2回目の停止の真因は Egress ではなく **NANO の実効メモリ 408MB に DB が乗らないこと**（#500）で、VACUUM FULL 後も余裕は 13〜28MB＝週次株価が増え続ける以上、正本を置く限り必ず再発する。**Supabase の Postgres へ書き戻す経路は作らない**（だから ADR-0035 の dest ローカル限定ガードはそのまま生きる）。収集・スコア更新は `scripts/run_nightly.py`（タスクスケジューラ・JST 17:20）、月次は `scripts/run_monthly.py`（毎月1日 JST 01:00・#504）が Fama-MacBeth 重み・M-1 マクロβ推論・**M-2/M-3 探索**を回し、**M-1 探索だけは `scripts/run_monthly_m1.py`（毎月2日 JST 01:00・#584・ADR-0046）が別タスクで回す**——実測 約752分で本体の窓（960分）に入らず、`hyperparameter_search` は完走してからしか永続化しないので月次に置くと毎月250分を捨てることになる、**「走らなかったこと」の検知は `scripts/check_batch_freshness.py`（毎日 JST 20:00・#515・ADR-0042）が担う**——GHA はクラウドなのでローカル DB を見られず、この役だけは原理的にあちら側へ置けない。**収集の入口は `_pipeline_incremental.py`**——`collector.py --incremental` は `run_full_collection` だけで**株価を1バイトも更新しない**（取り違えてもエラーは出ない）。バックアップは `scripts/backup_push.py` / 復元は `scripts/backup_restore.py`（Storage は 50MB/ファイル・1GB。表ごと分割で実測 37.5MB/世代）。
- **ミラー（`scripts/mirror_*.py`）の書き込み先はローカル限定**（#481・ADR-0035）。`guard_dest_local()` がリモート dest を `SystemExit` で弾く＝**ローカルから本番 DB へ書く経路をコードとして持たない**（#503 の反転後もこの向きは変わらない）。エンドポイント解決は `database.resolve_database_url()` へ委譲し**二重実装しない**。restore は **FK 依存順に1表ずつ**（ダンプの TOC はアルファベット順であって `--table` の指定順ではない）。週次の再取得窓は `DAILY_WINDOW_DAYS` から導出（27週）。ミラー範囲は全18表から `xbrl_raw_documents`（0行）を除いた**17表**＝`stock_price_daily` は #503 で範囲へ入れた（正本が移れば daily はローカルにしか無い正本データになる）。
- **週次株価の差分ロード（`weekly_price_cache.py`）は「速さだけ」を担う**（#480・ADR-0036）。正しさは①指紋（`max(week_start)`＋`count(*)`）②DB 側の世代印③行数照合の3つが持ち、**どれかが外れたら必ずフルロードへ倒す**（「不一致だが続行」の分岐を作らない）。触るときの不変条件は3つ: **差分条件 `week_start >= :since` は既存の500社チャンクの中に足す**（単独では PK 先頭列にならず seq scan）／**SELECT の列を増やさない**（`EGRESS_COST_TABLE` の4列較正は volume 込みの値・キャッシュ側は ISO 週の不変条件から `trade_date` で切れる）／**キャッシュのワイヤ形式は素タプル**（`_VOLUME_NOT_LOADED` は pickle で同一性が壊れ `px_volz` が全 nan 化する）。過去週を遡って書き換える処理を足したら世代印を進めること（`_recompute_weeks_from_daily` の構造的条件で自動的に進むが、経路によっては明示 bump が要る）。`27週` の導出は `database.WEEKLY_OVERLAP_DAYS` が唯一の源でミラー同期と共有する。
- **接続先は `FINAPP_DB_TARGET`（`local` 既定 / `prod`）で切り替える**（#481 B-1・**#503 で既定を反転**・`database.resolve_database_url()`）。既定が local なのは正本がローカルだから＝**`.env` に `DATABASE_URL` があるだけでは Supabase へ行かない**。`prod` を明示するのは `render.yaml` の1箇所だけ（無いと Render は localhost を見にいき「接続失敗」ではなく**空の DB に繋がって0件**に化ける）。**保険として、`FINAPP_DB_TARGET` 未設定でも `RENDER` 環境変数があれば prod へ倒す**（Render は `RENDER=true` を必ず設定する＝Blueprint 管理外で render.yaml の env が反映されない構成でも空DBを掴まない）。明示指定は常に最優先。`local` は `DATABASE_URL_LOCAL`（未設定ならローカル既定）を使い、**解決先がリモートなら import 時に `RuntimeError`**。逆に **`prod` で `DATABASE_URL` 未設定は警告どまりで raise しない**（反転前は `ci.yml` がこの経路を踏んでいた。緩さ自体は Render 側で生きている）。未知の target 値は `ValueError`。接続先は `/api/system/info` → `static/js/common.js` のバッジで全画面に出す（ローカル時のみ）。`launch.py` は既定値を文字列で写しているので `tests/test_db_target.py` が database 側と照合する。
- **セッション設定の唯一の源は `database.SESSION_FIXES`**（#565・ADR-0043）。`engine` の `connect` フックで自動適用し、`scripts/mirror_common._SESSION_FIXES` はこれを再利用する（ミラー固有の `extra_float_digits` だけ足す）。**書き写して二重定義に戻さない**——かつてミラー側だけが `SET TimeZone = 'UTC'` を持ち `database.engine` が素通しだったため、#503 で正本がローカル PG（実測 `TimeZone=Asia/Tokyo`）へ移った瞬間に **naive な DateTime 列が JST で保存される**ようになり、画面が「9時間先の時刻」を鮮度として出していた。**どちらの接続先でも書き込みは成功するので沈黙する**（#508 と同型）。ロール既定や `postgresql.conf` は変えない（別マシンで再現しない設定に頼らない）。再発の**実測**は `tests/test_tz_postgres.py`（`FINAPP_TEST_PG_URL` 必須）にしか置けず **CI では skip される**ので、CI 側は `tests/test_db_session_fixes.py` が「フックが張ってある・ミラーが共有部を包含する」までを縛る。
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

- **どこに書くかの判定**: 「これを読まないと Claude は**別の行動をとるか**？」——とるなら CLAUDE.md（**行動を変える命令**だけ・1項目 120字目安）、とらないなら [ARCHITECTURE.md](docs/ARCHITECTURE.md)（仕組み・経緯・実測値）か [GOTCHAS.md](docs/GOTCHAS.md)（再現条件と回避手順）へ。**教訓は捨てず、命令形の1行だけ残して「なぜ」を移す**。CLAUDE.md は冒頭で宣言したとおり**索引＋必須ルール**に限る（#576）。
- **ARCHITECTURE.md**: DBテーブル / 処理フロー / APIエンドポイント / 画面 / プラグインを追加・変更したら対応セクションを更新。
- **MODELS.md** と `templates/models.html`: 分析モデル追加・変更時に更新。参考文献は原著論文の DOI / 公式 URL（Wikipedia不可）。
- **MODELS.md §9（M-1）の章立てを変えたら初心者向け副読本 `docs/M1_MACRO_MODEL_GUIDE.md` も見直す**（Issue #472）: 副読本は**設計思想・章立てのみ追随**し、マクロ系列の全リスト・既定値・実測値は正本へのリンクに留める（書き写すと黙って陳腐化する）。見直し後、副読本冒頭の `models-sync` マーカーを更新すること。`tests/test_docs_sync.py` が CI で照合し、**乖離は失敗として現れないので通知では拾えない**（ADR-0031 と同型）。
- **「増やしたら登録表へ1行足す」ルール（共通形）**: いずれも**忘れても失敗として現れない**ので、CI が実体と表を照合する。理由を書いた `exempt:` は可・空理由は不可。

  | 増やしたもの | 登録先 | 照合するテスト |
  |---|---|---|
  | `heavy=True` のプラグイン | `nightly_scores.HEAVY_AUTOMATION`（回す経路 or `exempt:`） | `test_nightly_scores.py::TestHeavyAutomationRegistry` |
  | heavy な分析（プラグイン **および `SPECIAL_ANALYSES` の特例エントリ**・#593） | `plugins/progress.py::PROGRESS_COVERAGE`（`common` / `own` / `exempt:`） | `test_plugin_progress.py::TestProgressCoverageRegistry` |
  | ローカル駆動バッチ（`BatchSpec`） | `batch_freshness.py::WATCHED`（閾値は書かず `cadence` と `WINDOW_MIN` を渡す） | `test_check_batch_freshness.py::TestEveryLocalBatchIsWatched` |
  | スキル／エージェント | `docs/SKILLS_AND_AGENTS.md` へ1行（#575） | `test_docs_sync.py`（**`~/.claude/` は CI の checkout に無くローカル pytest でのみ照合**） |

  進捗は必ず**ステップ名**を持たせる（経過時間だけでは固まっていても健全に見える）。経緯は各 ADR と [ARCHITECTURE.md](docs/ARCHITECTURE.md)。
- **断面の前処理を変えたら `plugins/utils.py::PREPROCESS_VERSION` を上げる**（ADR-0039・#517）: `recommend_factor_premia` の `mean_b` は「その前処理での1単位あたり」であり、前処理を変えると**永続化済みの重みの意味が変わる**。世代を上げれば `get_dynamic_preset` が旧世代の行を採らずバランス型へ倒し、`factor_premia` を回すまで安全側に留まる。**上げ忘れは CI で拾えない**（前処理の変更を機械的に検出する手段が無い）＝ #509 で実際に旧単位の重み × 新単位の特徴量が本番へ出た（実測 rank-IC −0.0881）。
- **プリセット/重みの rank-IC は `scripts/preset_ic_gate.py` で測る**（ADR-0041・#529）: ADR-0028 の昇格ゲートは #509 と #517 で2回適用されたが**どちらもアドホックなスクリプトで、実体が残らなかった**。標準化経路の選択（消費側の `fit_zscore_stats`＋`normalize_transform` か学習系の `fit_feature_columns` か）で測るものが本番と別物になるため、**書き直さずこの CLI を使う**。パネルは毎晩伸びるので過去の実測と比べるときは `--until <ym>` で期を揃える（揃えないと 0.002 程度ずれて「手続きが違う」ように見える）。**ADR に並ぶ p=0.001 はブートストラップの下限**（`2/(n_boot+1)`）であって強さではない。
- **行を落とす探索軸は `momentum_gate --windows` で共通域を測ってから足す**（ADR-0050・#592）: `hyperparameter_search` は各候補を**その候補自身の母集団**で評価するため、母集団を動かす軸（`use_momentum`/`momentum_window` 等）はスコアと交絡し、探索は必ず「縮む側」を選ぶ。列を選ぶだけの軸（`max_features` 等）は対象外。**`--smoke` の共通域は間引きで壊れるので読まない**。
- **分析プラグイン追加・変更時のメタ検証網羅性**: 独自のランキング/スコアを出すプラグインは、`/api/backtest` の `SCORING_SOURCES`（backtest.py）へ追加するか、`plugins/macro_snapshots.py::oof_backtest` による OOF 評価を実装するかをその場で判断する。対象外にする場合は理由をコード内コメントまたは ADR に明記する（「後で対応」と ADR の prose に書くだけで終わらせない＝ Issue #272 のように M-1 だけ OOF 未対応のまま放置された実例あり）。比較ファミリー（M-1/M-2/M-3 等）内で1モデルだけ評価手段が欠けていないか確認する。
- **HTML 構成**: CSS はインライン1ファイル維持（分割禁止）。JS は CSP 対応で `static/js/<page>.js` に外部化（インラインイベントハンドラは `data-*`＋イベント委譲）。
- ファイル名・URL は機能名で命名（フェーズ番号禁止）。定数はファイル冒頭に集約。
- **軽量化の継続**: 機能追加・リファクタ後は `/tidy` を叩いてデッドファイル・壊れリンク・doc⇔code 乖離を点検すること。

---

## テスト方針

実装後は必ず Claude 自身が Python でテストを実行し、動作確認してから報告する。`tests/` は `pytest.ini` で `testpaths=tests` 固定。プラグイン追加時は `tests/test_<plugin>.py` を作成。
