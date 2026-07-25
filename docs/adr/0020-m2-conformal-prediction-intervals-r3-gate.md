# M-2 に分割コンフォーマル予測区間を付与し R3 足切りゲートを再有効化

## Status

accepted（2026-07-25）。Issue #365 の設計決定。ADR-0003（M-2 設計）／ADR-0004（M-2 下流の売り推奨・OOF）の追補。

## Context

M-2（`macro_gbdt`）は予測 μ̂ のみを出し、**確実性軸 `r1_prime` を持たなかった**（`read_producer_scores` が常に `None`）。このため `sell_ranking` の **R3 足切りゲート**（`r1_prime > r3_gate` の SELL 銘柄を REDUCE へ格下げ＝過信抑止）は `mu_source=macro_gbdt` 選択時に **no-op**（実地確認済み・除外集合 `("macro_gbdt","macro_dlm","macro_ensemble")` にハードコード）。

これは比較ファミリー内の**評価手段の非対称**である。M-1（§9.11）は OLS の閉形式予測 SE を `r1_prime` に持ちゲートに使う。M-3 は標準化誤差²の自己診断を持つ。M-2 だけ確実性軸を欠き、CLAUDE.md の「比較ファミリー内で1モデルだけ評価手段が欠けていないか確認する」メタ検証網羅性ルールに抵触（#272 の M-1 だけ OOF 未対応の再来リスク）。また §11.8 に quantile regression が将来案として prose 記載されるのみで Issue 未起票だった（残タスク正本ルール違反状態）。

XGBoost は OLS のような閉形式の予測 SE を持たない。素朴な quantile regression（`reg:quantileerror`・複数 α）は最終学習後に**追加 fit を要し M-2 単独にしか効かない**。

## Decision

**無リーク OOF 残差ベースの分割コンフォーマル予測区間（Lei et al. 2018）で `r1_prime` を埋め、R3 足切りゲートを M-2 でも機能させる。** quantile regression（再学習要）ではなくコンフォーマル（再学習不要・family-wide・被覆保証）を採用する。

1. **per-stock 半幅＝バケット条件付きコンフォーマル**（`macro_snapshots.conformal_bucket_halfwidths` / `conformal_halfwidth_for`）:
   - walk-forward CV の OOF 残差 `|resid|` を **(業種×サイズ三分位)/業種/global** の3粒度で集計し、各粒度の **τ 分位（既定 τ=0.9）を区間半幅**とする。
   - 解決は `_compute_r3_buckets`／`_r3_for` と**同一の bucket→sector→global フォールバック規約**（標本数 <`CONFORMAL_MIN_BUCKET`=20 は下位粒度へ）。
   - **marginal（全銘柄一定半幅）版を採らない理由**: R3 ゲートが「全通過 or 全遮断」の二択に退化し per-stock 確実性軸にならないため。バケット条件付きで低信頼バケット（モデルが苦手とする業種×サイズ）の銘柄が選択的にゲートされる。
   - ADR-0003 の階層（`macro_snapshots` が基盤・M-1/M-2 がその上）を保つため、三分位ロジックは `macro_risk_return` へ上向き依存せず本モジュール内に複製（M-1 の `_size_bucket` と同一挙動）。
2. **被覆診断を `oof_backtest` に追加**（family-wide）: `interval_coverage` = honest walk-forward split-conformal 被覆率（各 test 期を**それより過去の全** `|resid|` で較正した marginal 半幅で被覆判定→標本加重平均・Lei et al. 2018 の妥当性検査）。`interval_tau` / `interval_halfwidth` / `n_interval_calib` を併記。**全モデル（M-1/M-2/M-4/M-5）が共有 `oof_backtest` を通るため自動で family-wide**、`model_comparison` に横並び表示（理想は ≈`interval_tau`）。追加学習・Egress ゼロの純後処理。
3. **永続化・下流連携**:
   - `macro_gbdt_scores` に **`r1_prime` 列を加算的マイグレーション**（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`・非破壊・冪等）。`init_db` 無条件実行で本番へ反映されるため**列追加はユーザー確認済み**（memory「init_db 無条件実行の罠」）。
   - `replace_macro_gbdt_scores` を `(mu, r1_prime)` 保存へ拡張。`get_macro_gbdt_scores` は後方互換で `{ec: mu}` を維持し、`get_macro_gbdt_producer`（新）が `{ec: {mu, r1_prime}}` を返す。
   - `read_producer_scores` が `r1_prime` を埋める。`sell_ranking` の除外集合から `macro_gbdt` を外す（`macro_dlm`/`macro_ensemble` は依然 `None` のため残置）。
4. **graceful degrade**: `r1_prime=None`（列未 migration・旧スナップショット・フォールバック不能）はゲート素通り（従来挙動＝安全側）。

## Consequences

- **過信抑止**: M-2 選択時も R3 ゲートが機能し、低信頼バケットの SELL が REDUCE へ格下げされる。M-1 と対称な確実性軸を獲得しメタ検証網羅性ルールを満たす。
- **R3 と r1_prime の関係**: どちらも同一 OOF 残差から出るが役割は別。R3=√平均二乗残差（リスク軸・EV 減点）／`r1_prime`=`|resid|` の τ 分位（確実性軸・ゲート）。M-2 では両者はバケット水準で単調関係だが、`r1_prime` はコンフォーマルの**被覆較正**を伴う点が異なる。
- **family-wide 被覆診断**: `interval_coverage` が全モデルで揃い、区間の較正良否（実測被覆 vs 名目 τ）をモデル横断で比較できる。
- **限界**: バケット条件付きコンフォーマルは各バケットの**周辺被覆**を保証するが、条件付き被覆（個別銘柄水準）は保証しない。honest coverage は marginal 版で報告する（バケット別 honest 被覆は将来拡張）。τ=0.9 固定（パラメータ化は将来）。

## References

- Lei, J., G'Sell, M., Rinaldo, A., Tibshirani, R.J. & Wasserman, L. (2018). "Distribution-Free Predictive Inference for Regression." *JASA*, 113(523), 1094–1111. https://doi.org/10.1080/01621459.2017.1307116
- Koenker, R. & Bassett, G. (1978). "Regression Quantiles." *Econometrica*, 46(1), 33–50. https://doi.org/10.2307/1913643
