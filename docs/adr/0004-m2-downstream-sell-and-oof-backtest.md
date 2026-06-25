# M-2 を売り推奨（mu_source トグル）とアウトオブサンプル検証（OOF）へ連動

## Status

accepted（2026-06-26・grill/Opus で設計確定）。実装は GitHub Issue #234 系列で追跡。ADR-0003（M-2 本体）の続編。

## Context

ADR-0003 で M-2（`macro_gbdt`）を M-1 の非線形兄弟として実装したが、M-2 は**単独実行のみ**で下流機能に繋がっていなかった。要望は M-2 を「売り推奨（`sell_ranking`）」と「バックテスト」の双方へ連動させること。

ここに2つの構造がある。第一に、売り推奨は既に M-1（`macro_risk_return`）を **producer** として graceful-degrade で読む（`produced_output()`／`read_producer_scores()` → per-stock `{mu, r_macro, r1_prime}`）。ところが M-1 の producer μ は**線形ローディング**（`macro_beta`）からの read 時復元（μ = 切片 + Σβ·macro）で安価なのに対し、M-2（XGBoost）は線形 β 表現を持たず、μ を read 時に安価復元できない。第二に、既存「バックテスト」（`/api/backtest`）は過去 as-of 時点で `financial_metrics` VIEW から決定的に再スコアする preset 型で、**M-1 すら未連携**——学習済みモデルの μ を as-of 枠へ正しく載せるには過去時点での再学習が要り、最新 μ を過去日付へ当てれば[[メタ検証]]の無リーク原則に反する。

この ADR は、両連動を M-1 とのパリティ・無リーク原則・既存パターンに整合させるための決定を記録する。

## Decision

1. **producer μ̂ は M-2 の `execute()` が直書きする**（`sector_ols` → `regression_results` と同型）。新テーブル `macro_gbdt_scores`（`edinet_code` PK・`mu`・`snapshot_date`）へ、実行ごとに**全置換（スナップショット置換）**する。`macro_beta` のような推論バッチへは分離しない——ADR-0003 と同じく XGBoost は同期速度域で、`sector_ols` が既に「producer.execute() が直書き」の前例。R_macro は共有 `macro_beta` 由来のため本テーブルには持たず、r1_prime は M-2 に無いため列を持たない（最小列）。

2. **売り推奨は単一トグル `mu_source` で M-1/M-2 を切替**（既定 `macro_risk_return`・後方互換）。M-2 の `read_producer_scores()` は **M-1 と同一形** `{edinet_code: {mu, r_macro, r1_prime}}` を返す共通契約とし（`mu`=永続化 μ̂・`r_macro`=共有 `macro_beta` から read 時マージ・`r1_prime`=常に None）、`sell_ranking` 側は producer 参照を1点（`_get_plugin(mu_source)`）に一般化するだけで済む。`−R_macro`（[[系統的マクロリスク曝露]]）は共有でモデル非依存ゆえ `mu_source` に依らず不変。

3. **`r3_gate`（M-1 の r1_prime=予測SE 足切り）は `mu_source=macro_gbdt` で無効化**（graceful no-op＋UI注記）。M-2 は r1_prime を持たず、`r3`（バケット CV-RMSE）で代替するとゲートの意味が SE→RMSE へ静かに変質しスケール/粒度も変わる（CLAUDE.md の注意設計に反する）。明示ガードで no-op とする。

4. **バックテスト連動は「アウトオブサンプル検証（OOF）」**——既存 `/api/backtest`（preset/as-of ポートフォリオ模擬）とは**別概念**。M-2 が既に持つ walk-forward CV の**無リーク OOF 予測**（`residuals_by_ym = {test_ym:[(yhat,y_true),…]}`）だけを使い、**再学習・追加価格取得なしで**「μ̂ が将来リターンを順序付けるか」を分位リターン・rank-IC（Spearman）・ロングショート spread・hit-rate で評価する。μ̂ 水準の時系列ドリフトに頑健な**期内横断（per-period cross-sectional）分位**＋fold 毎 rank-IC。`execute()` の新キー `oof_backtest` で返す transient 出力（`cv_metrics` 同様・永続化しない）。

5. **対象は M-2 のみ・OOF 計算は共有ヘルパ**（`plugins/macro_snapshots.py::oof_backtest`）。M-1 も同じ residuals を持つため、後付けは M-1.execute から同ヘルパを呼ぶ1行で可能（今回スコープ外）。

## Considered Options

- **既存 `/api/backtest` の `source` に M-2 を as-of 再学習で追加**（却下）：過去 as-of 各時点でデータ≤当時のみで再学習すれば無リークだが、期間ごとに XGBoost 再学習で重く（multi は5回）、共有スナップショットビルダーに as-of cutoff 対応の追加実装が要る。WF-OOF は既存 CV の OOF 予測を再利用し、無リークかつ安価で同じ問い（μ̂ の予測力）に答える。
- **最新 μ スナップショットを既存バックテストへ流用**（却下）：最速だが、最新実行の μ を任意の過去日付に当てるため厳密な過去再現ができず先読みの懸念が残る——プロジェクトの無リーク原則と相性が悪い。
- **producer μ̂ を `macro_beta` のようにバッチ化**（却下・ADR-0003 と同じ理由）：XGBoost は同期速度域でバッチ分離の必然性が無く、永続化スキーマ・Actions 結線を無駄に増やす。`sector_ols` の前例に倣う。
- **売り推奨に `mu_gbdt` を別メトリクスとして追加**（却下）：M-1 μ と併存・ブレンド可能になるが、同一目的変数（52週先リターン）の二重計上で[[売りスコア]]の解釈が濁る。トグルが最小かつ明快。

## Consequences

- **新テーブル `macro_gbdt_scores`**（plain table・`Base.metadata.create_all` で生成・`financial_metrics` VIEW 非依存ゆえ「VIEW DROP→再作成」フローに干渉しない）。縦持ちでなく per-stock 1行・最新スナップショットのみ＝Supabase 容量は軽微。
- **`sell_ranking` の出力キー `m1_available` → `mu_available` 改名**＋`mu_source` 追加（フロント `analysis.js`／`templates/analysis.html` のセレクタ・通知も連動）。既定が M-1 のため挙動は後方互換。
- **用語「バックテスト」の二義化を CONTEXT.md で解消**：既存「バックテスト（preset/as-of）」と新「[[アウトオブサンプル検証]]（OOF・モデル評価）」を別語として登録。
- **producer 鮮度はローカル実行依存**：M-2 は `heavy=True`（Render は 403）。売り推奨で M-2 μ を使うには M-2 を一度ローカルで実行して `macro_gbdt_scores` を満たす必要がある（M-1 の `macro_beta` バッチが Actions 限定なのと同型の制約）。未実行なら graceful-degrade（μ 成分除外）。
- **将来エンハンス**：同 OOF ヘルパを M-1 にも結線（線形 vs 非線形の予測力を OOF で直接対比）、分位数の自動最適化、sector-neutral 分位、OOF の信頼区間。

実装・調査タスクは GitHub Issue #234 系列で追跡する。
