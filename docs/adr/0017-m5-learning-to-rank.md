# M-5 マクロ×財務 ランク学習（learning-to-rank・M-2 の rank-IC 整合版）

## Status

accepted（2026-07-24）。Issue #362 の設計決定。

## Context

M-2（`macro_gbdt`）は XGBoost を **MSE 最小化**（`objective="reg:squarederror"`）で学習する。
一方、評価（`oof_backtest`）・ハイパラ探索（`tuning_search_space`）・VISION の3兄弟比較は
すべて **期内クロスセクション Spearman rank-IC**。この「学習目的 ≠ 評価指標」不一致は、
ADR-0007 が `auto_hyperparams` を撤去した理由「周辺尤度 ≠ OOF rank-IC」と**同型**で、
今度は M-2 自身が学習側に抱えている。

MSE 最小化は期内クロスセクション順位の最適化を保証しない。二乗損失は外れリターン
（テールの大きな実現リターン）に引きずられ、順位を歪める方向に係数を動かしうる。予測を
**順位（どの銘柄が相対的に上か）**でしか使わない本プロジェクト（分位ロングショート・rank-IC）
では、学習目的を順位に一致させれば押し上げの余地がある。

技術的制約: `walk_forward_cv_monthly`（`utils.py`）は学習月を月横断で **flat 化**し、
fit_predict コールバックへは月境界を持たない `train_samples` を渡す。learning-to-rank
（`XGBRanker`）は各クエリグループ（＝各月）の境界を `fit(group=...)` で要求するため、
月境界の受け渡し口が無いと期内順位学習ができない。

## Decision

1. **新プラグイン `plugins/macro_gbdt_rank.py`（M-5・`heavy=True`・`ui_order=380`）**。
   MSE 版 M-2 を**無改変のベースライン**として残すため、M-2 の `params_schema` に `objective`
   を足す案ではなく**新兄弟モデル**として追加する（Issue #362 推奨案）。`model_comparison` に
   独立エントリとして並べることで、M-2(MSE) と rank 目的を**同一 fold・同一特徴量で純比較**できる
   （既定パラメータで走る comparison では、objective を param 化しても rank 版が別行に現れない）。
2. **execute() 本体は `MacroGbdtPlugin` から継承**し、rank 固有の4点だけを**フック**で差し替える
   （DRY・M-2 の挙動は不変）:
   - `_objective(params)` → `params["objective"]`（M-2 は `"reg:squarederror"` 固定）。
   - `_make_cv_callback(...)` → `XGBRanker` コールバック＋ `{"pass_train_groups": True}`。
   - `_fit_final_model(...)` → 全データ再学習を `XGBRanker`（月グループ復元）で行う。
   - `_persist_producer(...)` → no-op（producer を持たない）。
   加えて `_model_type()`→`"xgboost_ranker"`、`params_schema()` に `objective` select を追加。
   M-2 側にはこれら4フック＋`_model_type` の**既定実装**を導入したが、いずれも従来インラインの
   ロジックそのままで**振る舞いは不変**（既存 `tests/test_macro_gbdt.py` 全通過で担保）。
3. **月クエリグループ境界の受け渡し（`utils.py` の後方互換な最小拡張）**: `walk_forward_cv_monthly`
   に `pass_train_groups: bool = False` を追加。True のとき fit_predict を3引数
   `fit_predict(train_samples, test_samples, train_groups)` で呼ぶ。`train_groups` は各学習月の
   サンプル数配列（`train_samples` の連結順と一致・合計＝件数）＝ `XGBRanker` の各クエリグループ。
   既定 False では従来の2引数呼び出しで **M-1/M-2/M-3 は完全に不変**。
4. **ラベルの扱い（`_prep_rank_labels`）**: `rank:pairwise` は順序のみ使うため生の 52 週先対数
   リターンをそのまま渡す（負値可）。`rank:ndcg` は非負の段階的関連度を要求し 2^rel ゲインが
   発散するため、各クエリグループ内で `_NDCG_GRADES`（=16）段の分位グレード（0..K-1）へ変換
   （順序保存・大グループでもグレード上限クリップで発散しない）。
5. **early_stopping は使わず固定 `n_estimators`**: ランカーの eval_set は group 付き検証が必要で
   walk-forward の1テスト月では成立しにくいため、初版は固定木数で単純化（`best_iterations` には
   `n_estimators` を記録）。
6. **producer なし・OOF 比較専用**: 予測は**順位スコア**でリターン単位を持たない。よって
   `produced_output=False` / `read_producer_scores={}` / `_persist_producer` no-op とし、下流
   `sell_ranking`（`mu_source`）統合は「順位→分位期待リターン写像」を別途定義するまで見送る。
   `macro_ensemble`（M-4）は `BASE_MODELS=["macro_risk_return","macro_gbdt"]` 固定で M-5 を
   取り込まない（順位スコアと μ̂ 水準は統合器の入力として非互換）。
7. **評価登録**: `model_comparison.COMPARISON_MODELS` に `("macro_gbdt_rank","M-5")`。メタ検証
   網羅性（CLAUDE.md）は `oof_backtest` 継承＋比較登録で充足（`backtest.py::SCORING_SOURCES`
   は as-of VIEW スコア用で M-5 対象外）。

## Considered Options

- **M-2 の `params_schema` に `objective` 選択を足す（membership 検証）**: UI から MSE/rank を
  切替えられる利点はあるが、`model_comparison` は各モデルを**既定パラメータ**で走らせるため
  rank 版が比較行に現れず、Issue の受け入れ基準（純比較）を満たすには結局追加工作が要る。
  → 却下（新兄弟モデルなら比較行が自然に増える）。
- **`walk_forward_cv_monthly` の callback シグネチャを常に3引数へ変更**: M-1/M-2/M-3 の
  既存コールバックを全て書き換える必要があり回帰リスクが高い。→ 却下（オプトイン
  `pass_train_groups` で後方互換を維持）。
- **execute 内に月境界を保つ軽量 walk-forward を別実装**: `walk_forward_cv_monthly` と CV 骨格が
  二重化し、embargo/fold 境界のドリフトで M-2 と不公平な比較になりうる。→ 却下（共有関数の
  最小拡張で同一 fold を保証）。
- **ラベルを常に生リターンで渡す**: `rank:ndcg`/`map` は非負関連度を要求し 2^rel が発散する。
  → 目的関数に応じて `_prep_rank_labels` でグレード化（pairwise は素通し）。

## Consequences

- M-5 の実行コストは概ね M-2 と同等（同一 CV 骨格・SHAP は `XGBRanker` でも算出可）。Render
  軽量モードでは `heavy_render` で自動スキップ。`model_comparison` は 5 モデルになる。
- 予測は順位のみで**水準を持たない**ため、期待リターン水準が要る用途（分位期待値・効用計算・
  sell_ranking）には未対応（producer 見送りの理由）。順位→水準の写像を定義できれば将来
  producer 化しうる。
- 判定は honest 基準（embargo=12・ADR-0014）: **M-5 の OOF rank-IC が M-2(MSE) を上回れば
  「学習目的の整合が効く」を定量実証、上回らなければ「MSE で十分」を確定**（どちらもモデル
  選択の確定知見・本番 `model_comparison` 実測を本 ADR 末尾に追記する）。
- テスト（`tests/test_macro_gbdt_rank.py`）: producer 不在・objective membership・`_prep_rank_labels`
  の非負/上限クリップ/順序保存・`pass_train_groups` の3引数呼び出しとグループ合計整合・
  既定 False の2引数後方互換・execute の `model_type=xgboost_ranker` と producer 非永続化を固定。

### 実測（本番 model_comparison・未実施）

本番 full-config（週次株価＋マクロ5年蓄積）での `POST /api/backtest/model-comparison` により
M-2(MSE) と M-5(rank) の honest OOF rank-IC を同一 fold・同一特徴量で取得し、ここに追記する。
上回れば `mu_source` 統合（順位→水準写像）の検討へ、上回らなければ MSE 維持を確定する。

参考: Burges, C. J. C. (2010). "From RankNet to LambdaRank to LambdaMART: An Overview."
Microsoft Research Technical Report MSR-TR-2010-82.
https://www.microsoft.com/en-us/research/publication/from-ranknet-to-lambdarank-to-lambdamart-an-overview/
関連 ADR: 0003（M-1/M-2 公平性）, 0004（OOF 定義）, 0007（目的関数不一致で auto_hyperparams 撤去）,
0014（purge/embargo）, 0015（M-4 スタッキング）。
