# 集中コードレビュー: M-2 マクロ×財務 勾配ブースティング（macro_gbdt）

- **日付**: 2026-06-26
- **着眼点（スコープ）**: 直近追加の M-2 プラグイン（XGBoost、Issue #234 / ADR-0003）と、その CV ハーネス・SHAP 表示・テストへの結線に絞った集中レビュー。
- **対象コミット**: `865a72b`（= origin/main）
- **総評**: 全体として丁寧に実装・テストされており、**致命的な欠陥は無し**。データリーク無し（学習サンプルは月次昇順・early_stopping の検証分割は学習データ末尾のみ・winsorize は fold 内学習 y のみ）、CV 比較も XGB/OLS 双方が同一の元スケール（対数リターン）で RMSE/R² を算出しており公平。Render Free では `RENDER_LIGHT_MODE` が heavy プラグインを 403 で遮断するため本番タイムアウト懸念も限定的。以下は **軽微〜中程度の堅牢性・UX・テスト品質の指摘**。

> 本書はレビュー記録（report）であり、残タスクの正本ではない。各指摘は GitHub Issue 起票用のドラフト（推奨タイトル/ラベル付き）。`gh` が当セッションのポリシーで遮断されていたため Issue を直接起票できず、本レビュー記録としてコミットした。Issue 化は別途 `gh issue create` で行うこと（CLAUDE.md のタスク運用＝残タスクの正本は GitHub Issues）。

---

## F-1 [中] best_iteration のゼロ判定誤り＋フォールバック値が最終モデル木数を汚染する

**該当**: [plugins/macro_gbdt.py:90-91](../../plugins/macro_gbdt.py#L90)、[plugins/macro_gbdt.py:77-80](../../plugins/macro_gbdt.py#L77)、最終モデル木数決定 [plugins/macro_gbdt.py:383-387](../../plugins/macro_gbdt.py#L383)

```python
bi = getattr(model, "best_iteration", None)
best_iterations.append(bi if (bi and bi > 0) else n_estimators_max)
```

**問題**:
1. XGBoost の `best_iteration` は **0 始まりのインデックス**。最良反復が最初の木（`best_iteration == 0`）の場合 `bi` が falsy となり、`n_estimators_max`（既定 500）が記録される。「early_stopping がごく少数の木を選んだ」状況を逆に「上限まで使う」へ反転させてしまう。
2. `n_fit < _MIN_FIT_N`（学習サンプルが少ない初期フォールド）でも `n_estimators_max` がそのまま追加される。
3. 最終モデルの木数は `n_est_final = median(best_iterations)`。上記のフォールバック・センチネル（500 等）が混入すると中央値が上振れし、**early_stopping で抑えたはずの正則化が最終モデルで失われ過学習方向に傾く**。
4. 厳密には木数 = `best_iteration + 1`。中央値利用なので実害は軽微だが off-by-one が残る。

> 本番の大規模ユニバースでは各フォールドの学習件数が大きく `n_fit >> _MIN_FIT_N` となるため発生頻度は低い。ただし特徴量・期間を絞った設定や少社実行では顕在化しうる。

**推奨対応**: `best_iteration` を `bi if (bi is not None and bi >= 0) else n_estimators_max` とし、有効な early-stop 値（`bi + 1`）のみを `best_iterations` に積む。フォールバックで `n_estimators_max` を積む経路は、最終木数の中央値計算では別管理にする（フォールバック多数時に上限へ張り付かせない）。

- **推奨 Issue タイトル**: `fix(#234): M-2 best_iteration のゼロ判定誤りと最終木数中央値のフォールバック汚染`
- **ラベル**: `priority:low`

---

## F-2 [低] mean|SHAP|（無符号の重要度）を符号付き発散バー（OLS 係数用）で描画している

**該当**: 出力 [plugins/macro_gbdt.py:457](../../plugins/macro_gbdt.py#L457)（`feature_coefs = global_shap`）／描画 [static/js/analysis.js:1584](../../static/js/analysis.js#L1584) が M-1 の `_mrrPaintCoefBars`（[static/js/analysis.js:1309](../../static/js/analysis.js#L1309)）を流用。

**問題**: `_mrrPaintCoefBars` は「標準化係数（ゼロ中心・正右/負左）」の発散バー。M-2 が渡す `mean|SHAP|` は常に ≥0 のため全バーが右側へ寄り、M-1（左=負）に慣れたユーザーには「全特徴量が正方向に効く」と読めてしまう。見出しは「大きさのみ・方向なし」と正しく明記している（[static/js/analysis.js:1563](../../static/js/analysis.js#L1563)）ものの、可視化コンポーネントの含意（符号）と矛盾する。
- 付随: 描画呼び出しは `legendId='mg-coef-legend'` を渡すが、その DOM 要素は生成されていない（[static/js/analysis.js:1564](../../static/js/analysis.js#L1564) は `mg-coef-bars` のみ）。`_mrrPaintCoefBars` は `if (legend)` でガードするため**凡例は静かに省略**される（実害は無いが意図不一致）。

**推奨対応**: M-2 用に片側（0→max）の単方向重要度バーを用意するか、既存関数に「unsigned モード」を追加。凡例要素を出すなら `mg-coef-legend` を生成する。

- **推奨 Issue タイトル**: `fix(#234): M-2 SHAP 重要度を符号付き発散バーで表示している（無符号バーへ）`
- **ラベル**: `priority:low`

---

## F-3 [低] デッドコード（未使用 import・到達不能分岐）

**該当**: [plugins/macro_gbdt.py:20](../../plugins/macro_gbdt.py#L20)（`import math` は本ファイルで未使用）／[plugins/macro_gbdt.py:77](../../plugins/macro_gbdt.py#L77)（`or early_stopping_rounds is None`）。

**問題**: `early_stopping_rounds` は常に params 由来の int（schema 既定 40・min 10）であり `None` にならないため、`early_stopping_rounds is None` 分岐は到達不能。`import math` も統計処理は `statistics` で行っており未使用。

**推奨対応**: 未使用 import の削除、到達不能分岐の除去（または `early_stopping_rounds` を任意無効化できる契約にするなら schema 側で明示）。

- **推奨 Issue タイトル**: `chore(#234): macro_gbdt の未使用 import math と到達不能 early_stopping 分岐を除去`
- **ラベル**: `priority:low`

---

## F-4 [低] リーク防止テストが実質ノーオペで誤った安心感を与える

**該当**: [tests/test_macro_gbdt.py:156-198](../../tests/test_macro_gbdt.py#L156)（`TestLeak.test_eval_set_is_subset_of_train`）。

**問題**: テスト名は「early_stopping の eval_set が学習月に厳密包含（テスト月を含まない）」を主張するが、実体は `patch` ブロック内が `pass` で、最後に `n_fit + n_valid == n_train` の算術しか検証していない。**eval_set が実際に学習データ末尾と一致する（= リークしない）ことを一切確認していない**。`eval_sets_received` も収集されるだけで未アサート。回帰テストとして機能していない。

**推奨対応**: `_make_xgb_fit_predict` のコールバックを実呼び出しし、`XGBRegressor.fit` をモックして `eval_set` に渡る `X_valid` が `X_train_all[n_fit:]` と一致することをアサートする。

- **推奨 Issue タイトル**: `test(#234): M-2 リーク防止テストが実質ノーオペ — eval_set 末尾一致を実検証する`
- **ラベル**: `priority:low`

---

## 確認できた良い点（回帰防止メモ）

- **リーク無し**: `walk_forward_cv_monthly` は学習 = index<i の全月、テスト = index=i のみ（[plugins/utils.py:727-734](../../plugins/utils.py#L727)）。`train_samples` は月次昇順連結なので early_stopping の末尾分割が時系列的に妥当。
- **公平な CV 比較**: XGB/OLS とも `(yhat_orig, y_test_orig)` を元スケールで返し、同一 `samples_by_ym`・同一 fold 境界で RMSE/R² 算出。
- **winsorize の範囲**: 学習 y のみ（fold 内）。X は木の単調不変性により非 winsorize（設計通り）。
- **本番安全弁**: heavy=True ＋ `RENDER_LIGHT_MODE` で本番 403 ガード（[routers/analysis.py:94](../../routers/analysis.py#L94)）。
- **モメンタム**: M-2 は `macro_snapshots._momentum`（`close_last` 参照）を使用しており、`utils.get_momentum_return`（`r.close` 参照）の属性差異の影響を受けない。
