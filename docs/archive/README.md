# docs/archive — 完了済み作業記録

このディレクトリは**完了した作業の記録**を保管する。現行の仕様・設計の参照には使わないこと。
現行情報は [CLAUDE.md](../../CLAUDE.md)・[ARCHITECTURE.md](../ARCHITECTURE.md)・[MODELS.md](../MODELS.md) を参照。

| ファイル | 内容 | 状態 |
|---|---|---|
| `IMPROVEMENTS.md` | コードベース改善トラッキング（全項目完了。見出しの「未対応 — 緊急度」は当時の分類のまま残している） | ✅ 完了 |
| `VISUALIZATION_IMPROVEMENTS.md` | 企業データ可視化強化（Phase 1〜3 と Phase 4 の導線リンクは実装済み。グラフ画像・CSV の書き出しとレスポンシブの仕上げは未着手のまま記録として保管） | 📦 記録 |
| `REFACTORING.md` | DB 一本化・XBRL 生データ保存の設計書 ＋ 付録: コード品質改善 (Phase 2) | ✅ 完了 |
| `DEPLOYMENT_HISTORY.md` | DEPLOYMENT.md から移した完了済みの経緯（ローカル PG の初回セットアップ結果・ミラーの旧定常手順と予行演習・Supabase 正本時代のバックアップ運用・`vacuum-maintenance.yml` の設計記録・Supabase の Egress と容量設計の実測・バッチ暦とワークフロー早見表の旧記述・CF 補完と bs_inventory バックフィルの完了状態）。#844 | 📦 記録 |
| `MODELS_HISTORY.md` | MODELS.md 末尾の改訂履歴の表（2026-05-14〜2026-10-07）。以後の変更の記録は git log と ADR。#844 | 📦 記録 |
| `reviews/2026-06-26-m2-macro-gbdt-review.md` | 集中コードレビュー: M-2（マクロ×財務 GBDT）。「リーク無し」の評価は ADR-0014 で訂正済み（ラベルの前方重複を見落としていた） | 📦 記録 |
| `reviews/2026-06-27-web-api-auth-input-validation-review.md` | 集中コードレビュー: Web API 層（認証・入力検証・SQL 安全性） | 📦 記録 |
| `reviews/2026-06-28-csv-export-output-safety-review.md` | 集中コードレビュー: データエクスポート出力の安全性（CSV 出力・リソース上限） | 📦 記録 |

`reviews/` は 2026-06 の集中コードレビューの記録で、各指摘は当時 GitHub Issue 化済み（残タスクの正本は Issues）。以前は `docs/reviews/` にあった。

各ファイルの変更履歴は git で追える（`git log --follow docs/archive/<file>`）。

> 注: archive の文書も `tests/test_docs_links.py` の検査対象で、相対リンクは現在の置き場所から解決する。移すときはリンクも一緒に直す。
