# Skills & Agents 索引マニュアル

Claude Code で使える **スキル（Skill）** と **エージェント（Agent）** の早見表です。  
初心者向けに「何ができるか」「どう呼び出すか」だけに絞っています。

---

## 1. 基本のキ

### スキル（Skill）とは？
特定タスク用に用意された **手順書付きツール**。  
呼び出し方は2通り：

| 方法 | やり方 | 例 |
|---|---|---|
| **スラッシュコマンド** | チャットで `/スキル名` と打つ | `/code-review` |
| **自動トリガー** | 関連キーワードを発言すれば Claude が勝手に起動 | 「バグを診断して」→ `diagnose` 起動 |

### エージェント（Agent）とは？
**別タブで動く調査役・実装役**。Claude が必要に応じて裏で呼び出します。  
ユーザーが直接呼ぶことは少ないですが、「Explore で広く調べて」のように指示するとピンポイントで起動できます。

---

## 2. スキル一覧（用途別）

> **凡例**: 末尾 † のコマンドは現在この環境に未収録（呼び出しても動かない）。利用するには別途セットアップが必要。それ以外は組み込みコマンド、または `.claude/skills/` に収録済み。

### 🛠 コード品質チェック

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/code-review` | 差分のバグ・改善点をレビュー | コミット前、PR 出す前 |
| `/security-review` | セキュリティ観点でレビュー | 認証・入力処理を触ったとき |
| `/simplify` | コードの単純化・重複削減 | レビュー後の手直し |
| `/review` | プルリクをレビュー | PR の最終確認 |
| `/verify` | アプリを実起動して動作確認 | 「テストは通ったが本当に動く？」 |

### 🚀 開発ワークフロー

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/tdd` | テスト先行（Red-Green-Refactor）で実装 | 新機能・バグ修正 |
| `/diagnose` | 再現→最小化→修正 の体系的デバッグ | 原因不明バグ |
| `/prototype` | 使い捨て試作で設計を確かめる | UI 案・データモデル検討 |
| `/run` | プロジェクトのアプリを起動して見せる | 「動かしてみせて」 |
| `/init` | `CLAUDE.md` を新規生成 | リポジトリ初期設定 |

### 🏛 設計・リファクタ

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/improve-codebase-architecture` | 改善ポイントを洗い出す | 大規模リファクタ前 |
| `/zoom-out` | コードを上空視点で俯瞰説明 | 知らないモジュールを把握 |
| `/tidy` | デッドコード・doc 乖離を点検 | 機能追加・リファクタ後 |
| `/grill-me` | 設計案を詰問形式で叩く | 設計を固める前 |
| `/grill-with-docs` | 既存ドキュメントと突き合わせて詰問 | ADR/CONTEXT.md と整合性確認 |

### 📝 ドキュメント・計画

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/to-prd` | 会話内容を PRD（要件書）化 | アイデア固まったとき |
| `/to-issues` | 計画を独立 Issue に分解 | PRD を実装単位に落とす |
| `/triage` | Issue を選別・整理 | バックログ整備 |
| `/handoff` | 別エージェントに引き継ぐ要約を作成 | コンテキスト圧迫時 |
| `/deep-research` † | Web 検索で多角的に調査・レポート化 | 技術選定・最新情報 |

### ⚙️ Claude Code 設定

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/update-config` | `settings.json` を編集（権限・フック等） | 「npm を自動許可」など |
| `/keybindings-help` | キーバインド変更を支援 | ショートカット変更 |
| `/fewer-permission-prompts` | 許可ダイアログを減らす自動許可リスト作成 | 確認が煩わしいとき |
| `/git-guardrails-claude-code` | `git push --force` 等の危険コマンドをブロック | 事故防止 |
| `/setup-pre-commit` | Husky + lint-staged 導入 | コミット時の自動整形 |
| `/session-start-hook` † | Web セッション開始時の準備フック作成 | Claude Code on the web 導入時 |
| `/write-a-skill` | 新しいスキルを自作 | 独自スキルを作りたい |
| `/setup-matt-pocock-skills` | 上記スキル群が動く前提を整備 | 初回セットアップ |

### 💬 コミュニケーション

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/caveman` | 超簡潔モード（トークン節約） | 短いやり取りで節約したい |

### 🔧 その他

| コマンド | 何をする | 使う場面 |
|---|---|---|
| `/claude-api` | Anthropic SDK の使い方・モデル更新支援 | API コードを書く・移行する |
| `/loop` | プロンプトを定期実行 | 「5分ごとにデプロイ確認」など |
| `/migrate-to-shoehorn` | テストの `as` を shoehorn に置換 | TS テスト改善 |
| `/scaffold-exercises` | 練習問題スキャフォールド生成 | 教材作成用 |

---

## 3. エージェント一覧

ユーザーは普段意識しなくて OK。「○○ エージェントで調べて」と指定すると指名できます。

| エージェント | 役割 | 使い分け |
|---|---|---|
| **claude**（既定） | 何でも屋 | 種類を指定しないとき |
| **Explore** | 高速 read-only 検索 | 「どこで定義されてる？」 |
| **financial-app-explorer** | **本プロジェクト専用**の探索 | 複数ファイル横断調査・大ドキュメント要約 |
| **Plan** | 実装計画を設計 | 着手前に手順を組みたい |
| **general-purpose** | 汎用調査＋実装 | 範囲不明な多段タスク |
| **claude-code-guide** | Claude Code/SDK/API の質問回答 | 「フックの書き方は？」 |
| **statusline-setup** | ステータスライン設定 | 表示カスタム |

> 本プロジェクトでは **大規模調査は `financial-app-explorer` または `Explore` に逃がす**のがルール（`CLAUDE.md` 参照）。

---

## 4. 典型シナリオ早見表

| やりたいこと | 推奨コマンド |
|---|---|
| バグ直したい | `/diagnose` → `/tdd` → `/code-review` |
| 新機能を作る | `/grill-me`（設計） → `/tdd` → `/verify` → `/code-review` |
| 大改修の前 | `/zoom-out` → `/improve-codebase-architecture` → `/to-prd` → `/to-issues` |
| PR レビュー | `/review`（または `/code-review`） → `/security-review` |
| 後片付け | `/tidy` → `/simplify` |
| 設定を変えたい | `/update-config` / `/fewer-permission-prompts` |
| 技術調査 | `/deep-research` |
| 短く話して | `/caveman`（戻すときは「normal mode」） |

---

## 5. 関連ドキュメント

- 動作ルール本体: [`/CLAUDE.md`](../CLAUDE.md)
- アーキ詳細: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- ハマりどころ: [`GOTCHAS.md`](GOTCHAS.md)
- デプロイ運用: [`DEPLOYMENT.md`](DEPLOYMENT.md)
- 分析モデル: [`MODELS.md`](MODELS.md)

---

## 6. 困ったら

- スキル一覧を再確認 → このファイル
- スラッシュコマンドが効かない → 名前タイポ確認（例: `/code-review`、`/codereview` は NG）
- スキルが勝手に起動した → トリガーキーワードに反応している。止めたければ「stop ○○」「normal mode」
- 新規スキル作りたい → `/write-a-skill`
