# 集中コードレビュー: データエクスポート出力の安全性（CSV 出力・リソース上限）

- **日付**: 2026-06-28
- **着眼点（スコープ）**: クライアントへ**データを書き出す出口**に絞った集中レビュー。具体的には CSV エクスポート 2 本（`/api/export/csv`・`/api/db/export/{table}`、`routers/market.py`）の、出力コンテンツの安全性（数式インジェクション）とリソース上限（レート制限・メモリ展開）。前日 2026-06-27 のレビューは **Web API 層の認証・入力検証・SQL 安全性**を扱い、CSV エクスポートは「CSRF/exfil の観点でのみ」問題なしと判定していた。本書はその**未評価だったコンテンツ層・リソース層**を補完する。
- **対象コミット**: `41a3c41`（= origin/main）
- **総評**: **致命的な欠陥は無し**。データソースが EDINET / JPX（公的開示）であり攻撃者が任意セル値を注入する敷居が高いため、実害リスクは低い。以下はいずれも **defense-in-depth（出力ハードニング）の軽微な指摘**。書き出し以外（収集ジョブのライフサイクル＝`routers/collect.py`・`collection_jobs.py`）も併せて確認したが、並行ガードは `is_running()` 判定と `reset_for_run()` の間に `await` が無く同一コルーチン内で原子的に閉じており、TOCTOU レースは無い（記録のみ・指摘なし）。

> 本書はレビュー記録（report）であり、残タスクの正本ではない。各指摘は GitHub Issue 化済み（CLAUDE.md のタスク運用＝残タスクの正本は GitHub Issues）。

---

## F-1 [低] CSV エクスポートが数式インジェクションを中和していない → #257

**該当**: [routers/market.py:686-693](../../routers/market.py#L686)（`/api/export/csv`）、[routers/market.py:649-658](../../routers/market.py#L649)（`/api/db/export/{table}`）

**問題**: セル値の先頭メタ文字（`=` `+` `-` `@` `\t` `\r`）を無害化せずに `writerow` している。出力 CSV を Excel / Google スプレッドシートで開くと `=...` 等で始まるセルが**数式として評価される**（CSV/数式インジェクション, CWE-1236）。前日レビューは CSV を CSRF/exfil の観点のみで「問題なし」とし、**コンテンツレベルの数式評価は未評価**だった。

> 影響度: ソースが EDINET/JPX のため任意セル注入の敷居は高く**実害リスクは低い**。ただし「外部由来データの CSV 出力は数式を中和する」は標準ハードニング。

**推奨対応**: 先頭が危険文字の文字列セルを単引用符でエスケープする共通ヘルパ（`_csv_safe`）を両エンドポイントで共用。数値カラムは対象外。`tests/` に中和ケースを追加。

- **ラベル**: `priority:low`, `security`

---

## F-2 [低] CSV エクスポート 2 本がレート制限なし＋全行を一括メモリ展開 → #258

**該当**: [routers/market.py:615](../../routers/market.py#L615)（`/api/db/export/{table}`、`limit` 上限 100,000・レート制限デコレータ無し・[market.py:660](../../routers/market.py#L660) で一括生成）、[routers/market.py:669](../../routers/market.py#L669)（`/api/export/csv`、同じく `@api.limiter.limit` 無し・[market.py:695](../../routers/market.py#L695) で一括生成）

**問題**: (1) 他の重い入口（`/api/screen`=`RATELIMIT_ANALYSIS`、`/api/collect/*`=`RATELIMIT_COLLECT`）と異なりレート制限が無い。(2) 応答全体を `io.StringIO` にメモリ展開してから `iter([output.getvalue()])` で返す（実ストリーミングではない）。最大行数が大きく Render Free（512MB）でメモリ圧迫・連打の余地。認証済みのみ到達可能のため**深刻度は低い**。前日の入力検証レビュー（#245）は `companies`/`screen` の limit 検証を扱ったが、エクスポート系のレート制限・一括展開は対象外だった。

**推奨対応**: 両エンドポイントに `@api.limiter.limit(...)` を付与、`db_export` の `limit` 上限 100,000 を再検討、余力があれば行単位ジェネレータで真のストリーミング化。#257 と同じ 2 関数が対象のため一括対応が効率的。

- **ラベル**: `priority:low`, `performance`

---

## 確認したが問題なしと判断した項目（記録）

- **収集ジョブの並行ガード**（`routers/collect.py:111` 他、`collection_jobs.py:116`）: `is_running()` 判定 → `reset_for_run()`（`running=True`）の間に `await` が無く、`async def` ハンドラが同一コルーチン内で原子的に実行されるため、二重起動の TOCTOU レースは生じない。
- **`Content-Disposition: filename={table}.csv`**（`market.py:663`）: `table` はホワイトリスト（`_DB_VIEWER_TABLES`）検証済みでヘッダインジェクション不可。
- **エクスポート対象の文字列カラム**（`collection_logs.message` 等）: いずれもサーバ生成文字列で、利用者がタイプした自由文ではない＝数式注入の現実的な経路は EDINET/JPX 由来データに限られる。
- **数値カラムの CSV 出力**: 数式メタ文字を含まないため F-1 の対象外。
