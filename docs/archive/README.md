# docs/archive — 完了済み作業記録

このディレクトリは**完了した作業の記録**を保管する。現行の仕様・設計の参照には使わないこと。
現行情報は [CLAUDE.md](../../CLAUDE.md)・[ARCHITECTURE.md](../ARCHITECTURE.md)・[MODELS.md](../MODELS.md) を参照。

| ファイル | 内容 | 状態 |
|---|---|---|
| `IMPROVEMENTS.md` | コードベース改善トラッキング（全項目完了） | ✅ 完了 |
| `VISUALIZATION_IMPROVEMENTS.md` | 企業データ可視化強化（Phase1-4 実装済み） | ✅ 完了 |
| `REFACTORING.md` | DB 一本化・XBRL 生データ保存の設計書 | ✅ 完了 |
| `REFACTORING_PHASE2.md` | リファクタリング第2弾 | ✅ 完了 |

各ファイルの変更履歴は git で追える（`git log --follow docs/archive/<file>`）。

> 注: archive へ移動した際、各文書内の相対リンク（`../api.py` や 同階層の `VISION.md` 等）は移動前の階層基準のままずれている。これらは当時の記録であり現行の参照には使わないため未修正。現行のリンクは [CLAUDE.md](../../CLAUDE.md) と docs ルートの各文書が正。
