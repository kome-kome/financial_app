# 画面からの heavy 実行は既定で保存しない（保存は「結果を保存する」を選んだときだけ）

## Status

**accepted（2026-11-11）**。分析画面の `/ux-critique`（主要タスク「分析を回して結果を読む」）の P1 から。利用者の決定
「画面の既定は読む場所にする」「保存はチェックで選ぶ・既定は保存しない」に従う。
[ADR-0004](0004-m2-downstream-sell-and-oof-backtest.md) 決定 1（producer の `execute()` が保存表へ直書きする）と
[ADR-0066](0066-missing-consolidated-items-are-excluded-not-imputed.md) 決定 2（画面からの手動実行でも外れた行を消す）の
**画面から実行したときの部分だけ**を置き換える。バッチから回したときの保存は変えない。

## Context

分析画面から heavy（業種別OLS・M-2・M-3・M-6、退役した M-4）を回すと、`execute()` が保存表
（`regression_results`・`macro_{gbdt,dlm,enet,ensemble}_scores`）を置き換えていた。押す前の確認ダイアログに出るのは
表名だけで、何が変わるかは読めなかった。

しかも**画面の既定値は本番の設定と一致しない**。

| 分析 | 画面の既定 | 保存表を書く本番の設定 |
|---|---|---|
| 業種別OLS | `regularization="none"` | 夜間 `ridge`（`nightly_scores.NIGHTLY_PARAMS`） |
| M-2・M-3 | `params_schema` の既定 | 探索で選んだ値（日中の探索バッチ・月次バッチが `--persist-scores` で書く） |
| M-6 | `params_schema` の既定 | 同じ（夜間） |

そのため「既定値のまま押せば安全」とは言えず、試しの設定（モメンタム ON など）で M-6 を回すと、売り候補ランキングの
μ̂ が次の夜間までその設定の値になった。画面 run は `batch_freshness.PRODUCERS` の前進には数えられないが、朝の推奨の
乖離率の鮮度（`max(computed_at)`）は動くので、夜間が止まっていても画面 run が鮮度の遅れを隠しえた。

## Considered Options

1. **今のまま（確認ダイアログの文言だけ直す）**——却下。押せば置き換わる構造が残る
2. **画面からは常に保存しない**——却下。夜間が失敗したときに画面から直す道が消え、業種別OLS→バリュエーション分析の
   流れ（保存済みの `regression_results` を読む）も使えなくなる
3. **本番と同じ設定のときだけ保存する**——却下。モデルごとに本番の設定を取り出して比べる処理が要り、比べ忘れた
   設定があると黙って保存される
4. **既定は保存しない。保存は画面の「結果を保存する」（影響先を読んで選ぶ）で明示したときだけ**——採用

## Decision

1. `POST /api/plugins/{name}/run` は `?persist=1` のときだけ保存する。既定（`persist` 無し）の heavy は
   `database.tuning_dry_run()` の中で回す（探索・モデル比較と同じ止め方）。ContextVar なので `asyncio.to_thread` の
   先の保存関数まで届く。所要の記録（`app_settings` の `plugin_last_run_min:*`）は保存の有無に関わらず残す
2. `tuning_dry_run()` を `regression_results` の `upsert_regression_results_batch`・`upsert_regression_result`・
   `delete_regression_results` にも効かせる。これで **heavy の保存先はすべて同じ一か所で止まる**。
   保存関数を足したら `tuning_dry_run()` を見ること（`tests/test_screen_run_persist.py` が heavy の `writes` ごとに
   照合する）
3. `AnalysisPlugin.writes_impact` に「その表が変わると利用者から何が変わるか」と「次にどのバッチが書き直すか」を書く。
   `writes` を持つ heavy は必須（同テストが照合）。画面は実行ボタンの直前に「結果を保存する（{writes_impact}）」の
   チェック（既定オフ）を出し、確認ダイアログも「保存しません」／「保存します: {writes_impact}」と言い分ける
4. 保存しない実行の進捗は `progress.persisting()` が `dry_step`（「結果は保存しません」）を流す。業種別OLS を
   保存せずに回したときは、バリュエーション分析タブへ導かず「前回保存した結果のまま」と出す

## Consequences

- 試しの設定で回しても、売り候補・朝の推奨・おすすめ銘柄の値は変わらない。変わるのは「結果を保存する」を選んだときだけ
- 夜間が失敗したときは、チェックを入れて回せば画面から直せる（そのとき確認ダイアログが影響先を出す）
- 画面 run が朝の推奨の乖離率の鮮度を進めなくなる（保存しない既定のとき）。夜間が止まれば、鮮度の遅れがそのまま見える
- バッチ（`nightly_scores`・`run_daytime`・`run_monthly*`）は HTTP を通らないので、保存の挙動は変わらない
- 戻すときはこの変更を revert するだけ（表・VIEW の定義は変えていない）

## 関連

- 分析画面の `/ux-critique`（2026-11-10・22/40・P1 3 件の 1 つ）
- [ADR-0007](0007-hyperparameter-tuning-shared-engine.md)（`tuning_dry_run()` の導入）
- [ADR-0038](0038-local-postgres-is-the-primary.md) 追補（heavy は `writes_blocked()` のとき 403。変えない）
