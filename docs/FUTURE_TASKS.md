# 今後の課題・改善案

> **残タスクの正本は GitHub Issues**（`kome-kome/financial_app`）。
> どのセッションも同じ Issue を参照することで、**コードと残タスクの乖離を防ぐ**。
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
- **種別**: `ops`（本番運用・インフラ＝コード変更なし）／ `enhancement`・`refactor`・`docs`・`ci`・`bug`（コード）／ `triage`（自動起票・人の確認待ち。下の「自動解決の流れ」を参照）
- **着手→完了の同期**: PR 本文に `Closes #N` を書く。**main マージで Issue が自動クローズ**され、コード状態と残タスクが構造的に一致する。
- 各 Issue は「該当（`ファイル:行`）／問題／改善案／検証」の粒度で記述する（旧 FUTURE_TASKS.md の凡例を踏襲）。
- **ADR の「スコープ外」「将来エンハンス」は prose だけで終わらせず、同一 PR 内で `gh issue create` により追跡 Issue を起票する**。ADR に書いただけの未起票タスクは検索対象にならず放置されやすい（ADR-0004 が「M-1 にも OOF ヘルパを1行で結線可能（今回スコープ外）」と記録したが Issue 化されず、後続の Issue #240 の記述でも「M-1 は対応済み」という誤前提のまま伝播し、実際には Issue #272 まで未実装で残っていた実例）。ADR/PR レビュー時に「本文中の将来対応の記述に対応する Issue 番号があるか」を確認する。

---

## 自動解決の流れ（`/next-issue`）

**Issue は `/next-issue` が優先度順に1件ずつ処理する。人が判断するのは計画の承認だけ**で、承認後は worktree での実装 → 検証 → PR → CI 待ち → `gh pr merge --squash --match-head-commit` → 片付けまで止まらずに進み、次の Issue の計画へ連鎖する（2026-10-07 に「Web 版でレビューして手動マージ」から切り替えた）。手順の正本はユーザーレベルのコマンド `~/.claude/commands/next-issue.md` で、ここには書き写さない。本節はこのリポジトリ固有の規則と、その理由だけを持つ。

### 自動選定から外れる Issue

- **ラベル**: `ops`（コード変更なし）・`triage`（自動起票・人の確認待ち）・`help wanted`・`wontfix`・`duplicate`・`invalid`・`question`
- **内容**: 実測や重いバッチが要るもの（日中枠のキューへ積む＝CLAUDE.md「セッション開始時」）、未来の日付を待つもの、人の方針判断が要るもの
- `/next-issue` が範囲外として起票した Issue には `triage` が付く。中身を確かめて `triage` を外すと、次から自動選定の対象になる。

### Issue 外の変更も PR 経由にする

会話で直接頼まれた変更も main へ直接 push せず、PR → CI → squash 自動マージで入れる（`/next-issue` の CI 待ち〜片付けと同じ手順を、そのセッションが自分で行う）。

- **main マージは Render の自動デプロイを兼ねる**。直接 push は CI を通らずに本番へ出る。
- CI（`ci.yml`）は job 本体で約3分（PR #704 の実測 168秒）なので、待ちは小さい。
- 直接 push が混ざると、連鎖中の PR が `BEHIND` になり、取り込みと CI のやり直しが起きる。

### 元のフォルダは main から動かさない

スケジュールタスク（`financial_app-*`）は元のフォルダ（リポジトリ直下）のコードをそのまま import する。そこでブランチを切り替えると、未マージのコードが正本 DB に対して走る。ブランチ作業は `.claude/worktrees/<名前>` の worktree で行い、元のフォルダは main・未コミット変更なしに保つ。

### バッチ実行中はローカル検証と pull を見送る

日中枠・夜間・月次のバッチが走っている間は、次の2つを行わない。

| 見送るもの | 代わりに | 理由 |
|---|---|---|
| ローカルの pytest | CI の結果で検証する（マージ条件は元から CI 通過） | 並走は MCMC の結論そのものを変える（seed 固定でも発散 0→344 の実測）。全件 pytest の60秒ガード（`tests/conftest.py`）も負荷で偽陽性になる（62.3秒→単独 7.4秒の実測） |
| 元のフォルダへの `git pull --ff-only` | 次の Issue の片付けか、次のセッション開始時に回す | バッチは各ステップを子プロセスで起動するので、実行中に pull すると後続のステップだけが新しいコードで走る |

連鎖そのものは止めない（実装・PR・CI 待ち・マージはローカルの計算資源をほぼ使わない）。ただし CI で skip されるテスト（`FINAPP_TEST_PG_URL` 必須の実測など）に関わる変更は、バッチが終わってからローカルで回し、通ってからマージする。

判定（PowerShell。行が出たら実行中）:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'scripts[\\./]run_(daytime|nightly|monthly)' }
```

`run_*.ps1` は `-m scripts.run_<名>` で起動するので、スケジュールタスク経由でも手動起動でも拾える（タスクの `State` だけを見ると手動起動を見落とす）。バックアップ（`run_backup`）と watchdog は軽いので対象にしない。各バッチの起動時刻と窓は [DEPLOYMENT.md](DEPLOYMENT.md) の冒頭の表が正本。

---

## 注意事項（設計制約）

変更・実装時の設計制約（次元整合性・`winsorize(p1-p99)`・Zスコア年度別計算・科学計算ライブラリ採用基準・`docs/ARCHITECTURE.md` 同時更新・Render デプロイ前提）の**正本は [CLAUDE.md](../CLAUDE.md) の「設計制約」セクション**を参照する。二重記載が乖離源になるため、本書では再掲しない（残タスクの正本＝GitHub Issues と同じ方針）。
