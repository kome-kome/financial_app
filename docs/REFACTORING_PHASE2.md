# コード品質改善 (Phase 2)

## Context

2026-05-25 のリポジトリ整理セッション（デスクトップ版 Claude Code 実施）で、
ファイル整理・設定整合性・ドキュメント同期・タイムゾーンルール明確化までを完了した。
本書は **残ったコード品質改善タスク** を Web 版 Claude Code 引き継ぎ用にまとめたもの。

> `docs/REFACTORING.md` の「DB 一本化 + XBRL 生データ保存」とは別文脈の作業。
> あちらは全 Phase 完了済み。本書は「Section 4: コード品質」の継続作業を扱う。

### 完了済み（前セッション、`ab47d20..ff17c48`）

- **Section 1**: `migration_dumps/` (420MB) の `.gitignore` 追加 / `_check_state.py` を `scripts/check_db_state.py` に移動 / `_full_pipeline.py` 削除 / 420MB ダンプ削除
- **Section 2**: `.env.example` に `RENDER_LIGHT_MODE` 追記 / GitHub Actions Python を 3.13.7 に統一 / `render.yaml` コメント整備
- **Section 3**: `docs/REFACTORING.md` ステータスを「全 Phase 完了」に更新 / `docs/FUTURE_TASKS.md` の完了済み項目削除 / `README.md` のファイル構成表を `docs/ARCHITECTURE.md` へ集約
- **Section 4-1**: API レスポンスの DB タイムスタンプを JST 表示に統一（`_utc_to_jst_str()` 追加 + CLAUDE.md に時刻系ルール明記）

---

## 残タスク

### 4-2. `_now_jst()` を共通ユーティリティに移動  【M・優先度: 低】

- **現状**: [api.py:24](../api.py#L24-L25) で定義。`collector.py` や `database.py` でも JST が必要な箇所で `datetime.now()` が使われている可能性
- **対応**: `plugins/utils.py` または新規 `utils_common.py` に移動して全モジュールから import
- **依存**: 4-1 (完了) で `_utc_to_jst_str()` も追加済み。両方まとめて共通化するのが効率的
- **ファイル**: `api.py`, `collector.py`, `database.py`, 移動先のユーティリティモジュール

### 4-3. プラグインの単体テスト追加  【L・優先度: 中】 ✅ 完了

- **対応済み**: 未テストだった 5 プラグイン（`sector_ols` / `recommend` / `total_return` / `gap_analysis` /
  `price_predictor`）に `tests/test_<plugin>.py` を追加。プラグイン 7 個（utils 含む）すべてカバー。
- **方式**: 「純粋関数・定数テスト」＋「in-memory SQLite fixture（[tests/conftest.py](../tests/conftest.py) の
  `db` / `make_fin` 等）で `execute()` の挙動テスト」の 2 層。本番プラグインコードは無改変。
  `execute()` は `asyncio.run()` で直接呼ぶ（内部に実 I/O await が無いため pytest-asyncio 不要）。
  `init_db()` は Postgres 専用 SQL のため呼ばず `Base.metadata.create_all()` で SQLite にテーブル生成。
- **依存**: `pytest==9.0.3` を新設 `requirements-dev.txt` に pin（本番 `requirements.txt` とは分離＝Render メモリ節約）。
- **結果**: `pytest` 全 111 件パス（新規 50 件 + 既存 61 件）。各プラグインで正常系・異常系・空DB を網羅。

### 4-4. XBRL パース関数の重複統合  【L・優先度: 中】

- **現状**: 監査エージェントの指摘では [collector.py:378-407 parse_raw_rows()](../collector.py#L378-L407) と [collector.py:410-453 parse_xbrl_csv()](../collector.py#L410-L453) で優先度判定・値抽出ロジックが重複（**要直接確認**）
- **対応**: 共通ヘルパに抽出
- **ファイル**: `collector.py`
- **検証**: リファクタ前後で `_pipeline_gh.py` 実行結果（`financial_records` 件数）が一致すること

### 4-5. `api.py` (1940 行) / `collector.py` (1376 行) の分割  【L・優先度: 低】

- **現状**: 単一ファイルに複数の関心事が混在
  - `api.py`: 認証 / 収集 SSE / 分析 / DB ビューア / バックテスト
  - `collector.py`: EDINET / stooq / J-Quants / JPX / XBRL パース / マクロ
- **対応案**: 責務単位で分割（例: `api_collection.py`, `api_analysis.py`, `xbrl_parser.py`）
- **リスク**: 大きなリファクタは git の blame・diff レビューを難しくする
- **判定**: 現状で機能追加に支障が出ているかを再評価。出ていなければ後回し可

### 4-6. `walk_forward_cv()` の利用 or 削除判断  【M・優先度: 低】

- **現状**: [plugins/utils.py](../plugins/utils.py) で定義済みだが、プラグインからの呼び出しなし（テストのみ）
- **対応案**:
  - A: `sector_ols` / `price_predictor` で月次 backtest に組み込む
  - B: 未使用なら削除して `docs/MODELS.md` の関連記述も外す
- **要確認**: 設計意図（将来の使用予定があるか）

### 4-7. `check.py` と `checker.py` の命名整理  【S・優先度: 低】

- **現状**: 名前が似ているが役割は完全に異なる
  - [check.py](../check.py): EDINET API 疎通確認のワンショット
  - [checker.py](../checker.py): データ品質チェック関数群
- **対応**: 改名するなら `check.py` → `edinet_ping.py`、`checker.py` → `data_quality.py`
- **リスク**: import を全て書き換える必要あり。`check.py` は CLAUDE.md L130 に手順記載あり（同時更新）

---

## 推奨実行順序

1. **4-2** を先に片付ける（4-1 の延長で 30 分以内、コード規約を完全に統一）
2. **4-3** をプラグインごとに分割して順次（pytest 導入の承認が必要）
3. **4-7** （改名）— 影響範囲は限定的だが CLAUDE.md / docs も連動
4. **4-4** — リファクタ前後で `_pipeline_gh.py` の結果同一性を保証してから着手
5. **4-5** と **4-6** は別ブランチで設計検討（必要性を再判断）

---

## Verification

| Section | 検証方法 |
|---|---|
| 4-2 | 全モジュールから `_now_jst()` が import 可能か、import パスが壊れていないか |
| 4-3 | `pytest` 全件パス。各プラグインの `execute()` に正常系・異常系・空 DB ケースを最低 3 つ |
| 4-4 | リファクタ前後で `_pipeline_gh.py` 実行結果（`SELECT COUNT(*) FROM financial_records`）が一致 |
| 4-5 | 分割後も全 API エンドポイントが起動し、ブラウザの各画面が動作する |
| 4-6 | 採用なら sector_ols / price_predictor のテストが追加されること。削除なら `docs/MODELS.md` の関連記述も同時削除 |
| 4-7 | `python check.py` / pytest / 起動が成功。CLAUDE.md と docs の旧名 import 記述が全て更新済み |

---

## 元の Plan ファイル

詳細な経緯・監査エージェントの全指摘は `C:\Users\user\.claude\plans\calm-tickling-abelson.md`
（デスクトップ版 Claude Code のローカル `.claude/plans/` 配下、リポジトリ外）に保存されている。
Web 版から参照不可なので、必要なら本書を参考に進めること。
