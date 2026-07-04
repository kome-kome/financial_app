# 今後の課題・改善案

> **残タスクの正本は GitHub Issues**（`kome-kome/financial_app`）。
> web版・ローカル版 Claude Code の双方が同じ Issue を参照することで、**コードと残タスクの乖離を防ぐ**。
> 本ファイルは「Issue 運用ガイド＋設計制約（注意事項）」に限定し、**タスク実体は二重記載しない**（過去はこの二重記載が乖離源になった）。
> 完了済み項目は `docs/archive/IMPROVEMENTS.md` に集約（git 履歴で詳細参照可能）。

---

## 残タスクの参照・運用

```bash
gh issue list --state open                 # 残タスク一覧（正本）
gh issue list --label "priority:high"      # 優先度で絞る
gh issue view <N>                          # 詳細
gh issue create --label "priority:low,ops" # 新タスク起票
```

- **優先度ラベル**: `priority:high` / `priority:medium` / `priority:low`
- **種別**: `ops`（本番運用・インフラ＝コード変更なし）／ `enhancement`・`refactor`・`docs`・`ci`・`bug`（コード）
- **着手→完了の同期**: PR 本文に `Closes #N` を書く。**main マージで Issue が自動クローズ**され、コード状態と残タスクが構造的に一致する。
- 各 Issue は「該当（`ファイル:行`）／問題／改善案／検証」の粒度で記述する（旧 FUTURE_TASKS.md の凡例を踏襲）。
- **ADR の「スコープ外」「将来エンハンス」は prose だけで終わらせず、同一 PR 内で `gh issue create` により追跡 Issue を起票する**。ADR に書いただけの未起票タスクは検索対象にならず放置されやすい（ADR-0004 が「M-1 にも OOF ヘルパを1行で結線可能（今回スコープ外）」と記録したが Issue 化されず、後続の Issue #240 の記述でも「M-1 は対応済み」という誤前提のまま伝播し、実際には Issue #272 まで未実装で残っていた実例）。ADR/PR レビュー時に「本文中の将来対応の記述に対応する Issue 番号があるか」を確認する。

> **直近完了（2026-06）**: Tier 1 リファクタ全件（T1-1〜T1-9）／ 発行済株式数の正規取得（G）／ `period_end` DATE 型移行（H）／ 財務項目網羅性 C1・C2（**本番フル再収集 DF-2 も 2026-06-19 完了**）／ **M-1 マクロ×リスク-リターン推奨モデル（Phase A–D 全件＋モメンタム独立化）** ／ UI/UX IA 再編（PR#197）／ スライダー step 契約（PR#196）。詳細は `docs/archive/IMPROVEMENTS.md`「Phase 4」。

---

## 注意事項（設計制約）

変更・実装時の設計制約（次元整合性・`winsorize(p1-p99)`・Zスコア年度別計算・科学計算ライブラリ採用基準・`docs/ARCHITECTURE.md` 同時更新・Render デプロイ前提）の**正本は [CLAUDE.md](../CLAUDE.md) の「設計制約」セクション**を参照する。二重記載が乖離源になるため、本書では再掲しない（残タスクの正本＝GitHub Issues と同じ方針）。
