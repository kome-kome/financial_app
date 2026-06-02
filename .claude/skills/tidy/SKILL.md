---
name: tidy
description: >
  financial_app プロジェクトの軽量化点検 Skill。
  デッドファイル・壊れリンク・doc⇔code 乖離・重複・肥大セクションを
  financial-app-explorer で調査し、確信度別に報告・整理する。
  ユーザーが /tidy を叩いたときに起動。
---

# /tidy — プロジェクト軽量化点検

## 概要

`financial-app-explorer`（read-only エージェント）に調査を委譲してメインコンテキストのトークンを節約しながら、ドキュメント・ファイル構成の定期的な点検・整理を行う。

---

## ステップ 1: 調査（financial-app-explorer に委譲）

以下の観点で横断スキャンを依頼し、結論のみ受領する:

### 1-1. doc ⇔ code 乖離チェック
`docs/ARCHITECTURE.md` のファイル役割表・API エンドポイント一覧・プラグイン一覧を実体と照合:
- `plugins/` 配下のファイル一覧 vs ARCHITECTURE.md の記載
- `templates/*.html` / `static/js/*.js` の実在ファイル vs ARCHITECTURE.md の記載
- `tests/test_*.py` の実在ファイル vs ARCHITECTURE.md の記載
- `.claude/` 配下の agents / skills 定義ファイル vs ARCHITECTURE.md の記載
- `docs/FUTURE_TASKS.md` に「✅ 完了済み」マークや「実装済み」言及がある項目 → 削除候補

### 1-2. 壊れた内部リンク
- `*.md` ファイル内の `[...](...)` が指すファイルが実在するか
- 特に `docs/archive/` 移動後のリンク更新漏れ
- `daily-incremental.yml.disabled` など `.disabled` ファイルへの言及（一時停止中・再有効化が必要なものは「運用ノートあり」と区別する）

### 1-3. デッドファイル検出
- `*.disabled` の workflow ファイル（一時停止以外の理由のもの）
- どこからも `import` / 参照されない `.py` ファイル（`scripts/` 配下を重点確認）
- `_old`, `_backup`, `_v2` など命名で明らかに古いファイル

### 1-4. 重複記述
- 同じ説明が複数 .md ファイルに存在する箇所（「正本」を指す参照リンクに変換できるもの）
- CLAUDE.md の索引と実ファイルの内容定義が乖離している箇所

### 1-5. 肥大セクション
- 単一ファイルで 50 行以上のセクションで、要約・圧縮余地があるもの（MODELS.md 共通事項、ARCHITECTURE.md の長いシーケンス図等）

---

## ステップ 2: 報告

調査結果を以下の形式でカテゴリ別に提示:

```
## [カテゴリ名]
- [高] ファイルパス:行範囲 — 問題の説明 → 推奨アクション
- [中] ファイルパス:行範囲 — 問題の説明 → 推奨アクション（確認後実施）
- [低] ファイルパス:行範囲 — 問題の説明 → スキップ or 後回し
```

「一時停止ファイル（運用ノートあり）」は確信度・低として別掲し、削除候補と混同しないこと。

---

## ステップ 3: 実行

**確信度・高のみ自動実施**（ユーザー確認なし）:
- 壊れた内部リンクの修正
- `docs/FUTURE_TASKS.md` の完了済み項目削除
- ARCHITECTURE.md のファイル役割表への追記（実在するが未記載のファイル）

**確信度・中/低はユーザー確認後に実施**:
- ファイル削除（`git grep` で能動的参照がゼロであることを先に確認）
- ファイル統合・重複削除（構造変更を伴うもの）
- 肥大セクションの圧縮

---

## ステップ 4: 検証

実施後に以下を確認:
1. `git grep CONSTRAINTS` → 削除済みなら 0 件であること（DEPLOYMENT.md に統合済み）
2. 主要な `*.md` 内リンク先の実在確認（`git grep` でパターンマッチ）
3. `pytest`（`testpaths=tests`）— 全件パス
4. `uvicorn api:app --reload` で起動確認 → `/`, `/collection`, `/analysis`, `/company/{code}` が 200

---

## ガードレール

**絶対に触れない**:
- `.env` / `.env.local` 等の機密ファイル
- `collector.py` / `api.py` / `database.py` / `plugins/` の中核ロジック
- `requirements.txt`（完全 pin 維持）
- `daily-incremental.yml.disabled`（全件収集中の一時停止。運用ノートは DEPLOYMENT.md に記載）

**archive は削除しない**（統合・整理のみ）:
- `docs/archive/` 配下は完了済み作業記録。現行参照には使わないが git 履歴の補助として残す。

**削除の前に必ず**:
- `git grep <ファイル名>` で能動的な参照が 0 件であることを確認

**セッション終了時**:
- CLAUDE.md の規約どおり `.env`・機密を除外して `git add` → `git commit` → `git push`

---

## 参考：主なドキュメント構成

| ファイル | 役割 | 触れていい |
|---|---|---|
| `CLAUDE.md` | 索引＋必須ルール | ✅（索引更新のみ） |
| `docs/ARCHITECTURE.md` | 設計の真実の源 | ✅（追記・整合更新） |
| `docs/DEPLOYMENT.md` | 運用＋外部サービス制約 | ✅（追記のみ） |
| `docs/MODELS.md` | 分析モデル理論 | ✅（重複削除のみ） |
| `docs/GOTCHAS.md` | 実装ハマりどころ | ✅（整理のみ） |
| `docs/FUTURE_TASKS.md` | 未実装の課題 | ✅（完了済み項目削除） |
| `docs/VISION.md` | プロジェクト方針 | ✅（古いステータス更新のみ） |
| `docs/archive/*` | 完了済み記録 | ⚠️ 統合のみ・削除禁止 |
| `plugins/*` / `api.py` 等 | 中核ロジック | ❌ 触れない |
