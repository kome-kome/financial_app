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
| [SKILLS_AND_AGENTS.md](docs/SKILLS_AND_AGENTS.md) | スキル／エージェントの索引マニュアル | スラッシュコマンドや調査エージェントを使うとき |

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
# ローカル（Windows）
.\venv\Scripts\Activate.ps1
uvicorn api:app --reload                 # → http://localhost:8000/

python collector.py --years 5           # 全件収集（5年分）
python collector.py --years 1 --max 10  # テスト用（10社）
python collector.py --company E02167    # 特定企業更新
python collector.py --market            # 株価のみ更新
python collector.py --incremental       # 差分収集
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
| `collector.py` | EDINET+J-Quants からデータ収集→DB保存 |
| `api.py` | FastAPI REST・SSE・回帰分析 |
| `plugins/` | 分析モデル（自動検出方式）。詳細は [MODELS.md](docs/MODELS.md) |
| `templates/*.html` | 画面（`/`=dashboard, `/collection`, `/analysis`, `/company/{code}`）。JS は `static/js/<page>.js` |
| `_pipeline_gh.py` / `_pipeline_incremental.py` | GitHub Actions 用・全件 / 差分収集 |

完全なファイル役割一覧・処理フロー・ER図は [ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照。

---

## GitHub 協調ワークフロー

デスクトップ版 Claude Code（ここ）と Web版（claude.ai/code）が `kome-kome/financial_app` 経由で協調。PR は Web版でレビュー → main マージ。

| 規模 | ブランチ | 手順 |
|---|---|---|
| バグ修正・小改善 | `main` 直接 | commit → push |
| 機能追加・大きな変更 | `feature/xxx` | branch → push → PR |

**セッション終了時**: `git status` →（`.env`・機密を除外して）`git add` → `git commit` → `git push`。

---

## 設計制約（変えてはいけないこと）

- **`upsert_financial` の入力**: `{bs,pl,cf,derived,val,nonfin}`。bs/pl/cf は `bs_` 等プレフィックス付きで DB カラムにマップ、derived は破棄（VIEW で算出）、val/nonfin はプレフィックス無しの直接列へ。**未知キーは silent-drop せず raise**（fail fast）。
  - **補足（`calc_derived` の永続化列との関係）**: 破棄されるのは入力の `derived` キーのみ。`calc_derived`（collector.py）は free_cf / ebitda / nonoperating_income 等を **`cf`/`pl` セクションに入れて返す**ため、これらは `cf_free_cf` / `pl_ebitda` 等の**実列へ永続化される**（VIEW 算出ではない）。「軽い派生（成長率・Zスコア等）は VIEW で都度算出・非永続」と「収集時に確定する派生額は実列へ保存」を区別すること。
- **再分類項目の追加は1箇所**: `FinancialRecord`（database.py）の列に `info={"xbrl": [...]}` で生タグを併記するだけ。`XBRL_MAP`（collector.py）は `build_xbrl_map()` が列 info から逆引き生成するため手書きしない（列定義が唯一の源）。接頭辞なし列（val/nonfin）は `info["section"]` を明示。parse 側の例外ロジック（連結優先度・capex ラベル照合・OperatingRevenue1 フィルタ）は collector.py に残す。
- **`run_full_collection` の `df_master` は常に全件**（`max_companies` で絞らない）。`max_companies` は書類収集件数の上限のみ。`collect_doc_ids_for_period` の `max_companies` は全期間スキャン後に先着N社へ絞る（早期終了禁止）。
- **認証ミドルウェアは `/api/auth/` プレフィックスを常に通過**させる（ログインAPI自体を守ると詰まる）。
- **`/api/gap-analysis` は業種別OLS（sector_ols）実行後でないと404**。`depends_on` を `plugins.ensure_dependencies` が runner（`/api/plugins/{name}/run`→400）と専用エンドポイント（`/api/gap-analysis`→404）で強制する（producer の `produced_output` で判定）。`/api/regression` という実エンドポイントは無く、回帰は `/api/plugins/sector_ols/run` 経由。
- **プラグイン起動は `plugins.execute_plugin(plugin, raw, db)` が単一入口**（runner / `/api/recommend` / `/api/gap-analysis` が共用・テストもこれ）。内部で `coerce_params`→`ensure_dependencies`→`execute` の順。例外（`ValueError`/`DependencyError`）は握らず送出し、各 endpoint の except が HTTP へマップ（gap-analysis→404・runner→400 の差を保つ）。
- **`params_schema()` はパラメータ契約**（CONTEXT.md「パラメータ契約」）。`type`（ウィジェット）と `dtype`（データ型: int/float/str/list[str]/bool/dict）の2軸を持ち、dtype は `number`/`slider` にのみ明示必須（他は type から推論）。型変換・default 補完・bounds(min/max)/membership(options) 検証は `coerce_params`（`plugins/utils.py`）が一手に担い、**違反は reject（ValueError）**。`execute` は coerce 済み typed params を受け取り、意味的 validation（features 非空・weights 合計≠0 等）だけ持つ。bool ウィジェットは `checkbox` に統一（`boolean`/`bool` 禁止）。
- **CORS は `ALLOWED_ORIGIN` 環境変数で制御**（デフォルト `http://localhost:8000`）。
- **分析モデルの次元整合性（必須）**: 説明変数と被説明変数は同一次元（per-share財務金額[円/株]→株価[円/株]の Ohlson 型）。OLS学習前に各特徴量を `winsorize`（p1-p99、`plugins/utils.py`）。詳細・根拠は [MODELS.md](docs/MODELS.md)。
- **科学計算ライブラリ**（numpy/scipy/statsmodels/scikit-learn）は利用可。採用基準は [VISION.md](docs/VISION.md)。

---

## パッケージ管理（pip install 前に必須）

1. **セキュリティ評価**: WebSearch で「パッケージ名 + CVE / security vulnerability」を検索
2. **評価提示**: バージョン・既知CVE・DL数・総合判定（✅低 / ⚠️注意 / ❌高リスク）
3. **ユーザーの明示承認を得てから実行**

`requirements.txt` は完全 pin（`==`）。アップグレードは単独 PR + `pytest` + 主要画面確認をセットで。

---

## ドキュメント更新ルール（コード変更と同じ作業内で必須）

- **ARCHITECTURE.md**: DBテーブル / 処理フロー / APIエンドポイント / 画面 / プラグインを追加・変更したら対応セクションを更新。
- **MODELS.md** と `templates/models.html`: 分析モデル追加・変更時に更新。参考文献は原著論文の DOI / 公式 URL（Wikipedia不可）。
- **HTML 構成**: CSS はインライン1ファイル維持（分割禁止）。JS は CSP 対応で `static/js/<page>.js` に外部化（インラインイベントハンドラは `data-*`＋イベント委譲）。
- ファイル名・URL は機能名で命名（フェーズ番号禁止）。定数はファイル冒頭に集約。
- **軽量化の継続**: 機能追加・リファクタ後は `/tidy` を叩いてデッドファイル・壊れリンク・doc⇔code 乖離を点検すること。

---

## テスト方針

実装後は必ず Claude 自身が Python でテストを実行し、動作確認してから報告する。`tests/` は `pytest.ini` で `testpaths=tests` 固定。プラグイン追加時は `tests/test_<plugin>.py` を作成。
