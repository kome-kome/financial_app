# M-4 兄弟μ̂スタッキング・アンサンブル（M-1+M-2 の二段ウォークフォワード統合）

## Status

accepted（2026-07-23）。Issue #367 の設計決定。

## Context

3兄弟（M-1 線形OLS / M-2 XGBoost / M-3 週次DLM）は個別評価のみで、相補性（線形の頑健さ ×
非線形の表現力）を活かす統合器が無かった。VISION 核心「並置してどちらが有効か → さらに
超えられるか」を直接実験できていない。

前提の転換（#375・ADR-0014）: purge/embargo 導入後の honest 実測で **M-2 rank-IC は
0.33→0.14（旧値は52週先ラベルの前方リーク由来）**、M-1（財務のみ OLS）は 0.24→0.19。
「M-2 独走」の前提が崩れて両者が肩を並べたため、予測誤差が低相関なら制約付き加重で
相殺できる余地（多様性の価値）が上がった。統合の判定基準も旧 0.33 ではなく
**honest 値（embargo=12）で単体最良を上回るか**に更新される。

技術的制約: M-1/M-2 の `execute()` は集約 `oof_backtest` と現在μ̂ `results` しか返さず、
スタッキングに必要な **per-(ym, 銘柄) の OOF 予測を露出しない**。また M-1（strict＝
`macro_nan_ok=False`）と M-2（`macro_nan_ok=True`）は同一 ym でも母集団（銘柄集合）が異なる。

## Decision

1. **新プラグイン `plugins/macro_ensemble.py`（M-4・`heavy=True`・`ui_order=350`）**。
   統合対象は**初版 M-1+M-2 のみ**（モジュール定数 `BASE_MODELS`・UI 非露出）。
2. **M-4 は基底モデルの OOF を自前で再現する**: 各モデルの config（`params_schema` 既定を
   `coerce_params` で補完＝`model_comparison` と同一）で `build_snapshots(return_stock_ids=True)`
   → M-1 は `_select_macro_features`(BIC)+既定OLS、M-2 は `_make_xgb_fit_predict` 注入で
   `walk_forward_cv_monthly(return_residuals=True, embargo_months=LABEL_HORIZON_MONTHS)` を回す。
   `stock_ids_by_ym[ym][k] ↔ residuals_by_ym[ym][k]` の順序保証（同一 samples_by_ym 順）で
   (ym, edinet_code, yhat, y_true) を突合し、**両モデル共通の (ym, edinet_code) の intersection**
   をアンサンブル母集団とする（「両モデルが予測できる銘柄」＝正しい統合対象）。
3. **二段ウォークフォワード（`_stack_walk_forward`）**: 月 t の統合重みは t より厳密に前の月の
   共通 OOF ペアだけで学習（expanding）。基底 μ̂ 自体が embargo=12 の purged OOF のため
   二段目もリークしない。重み学習前（`min_meta_months` 未満/ペア僅少）は等重み (0.5,0.5)。
4. **重み最適化 `_fit_weights`**: 既定 `nnls`（`scipy.optimize.nnls` 非負最小二乗→和1正規化・
   和0は等重みフォールバック）。代替 `rank_ic_grid`（期内平均 Spearman 最大化・`grid_step` 刻み）
   / `equal`。rank-IC は期内シフト/スケール不変のため切片は持たず比 w1:w2 のみが効く。
5. **統合残差 `{t:[(yhat_stack, y_true)]}` → 共有 `oof_backtest`** で M-1/M-2/M-3 と同一指標。
   `tuning_objective_only()` 中は oof 算出直後に早期 return（全社スコアリング/永続化を省略）。
6. **現在μ̂と producer 化**: M-1 は `_fit_final`+`_score_companies`（専用テーブルが無いため自前
   実行）、M-2 は全データ最終 XGB（`n_estimators=median(best_iterations)`）で現在μ̂を出し、
   edinet_code intersection に**全共通 OOF で学習した最終重み `w_final`** を適用（`w_final` は
   現在μ̂専用・OOF 評価には使い回さない＝リーク防止）。`macro_ensemble_scores` テーブル
   （`replace_/get_macro_ensemble_scores`・`tuning_dry_run` no-op）へ全置換永続化し、
   `read_producer_scores` は M-2 と同一形 `{ec:{mu, r_macro(共有macro_beta), r1_prime:None}}`。
   `sell_ranking` の `mu_source` に `macro_ensemble` を追加（既定は据え置き・r3_gate は
   r1_prime を持たないため no-op 除外集合へ）。
7. **評価登録**: `model_comparison.COMPARISON_MODELS` に `("macro_ensemble","M-4")`。メタ検証
   網羅性（CLAUDE.md）は `oof_backtest` 実装＋比較登録で充足（`backtest.py::SCORING_SOURCES`
   は as-of VIEW スコア用で M-4 対象外）。
8. **M-3 は初版除外（論証された非適用）**: 週次専用（ADR-0012）で目的頻度（52週 vs 1週先）・
   母集団が異なり (ym,銘柄) 整列が非自明。honest 値も ≈0.01 と弱く NNLS では重みがゼロ寄りに
   なる見込み。将来追加する場合は週次→月次集約の整列設計を別途行う。

## Considered Options

- **基底モデルの execute() を改修して OOF を返させる**: M-1/M-2 の戻り値契約が肥大し、
  探索/比較のペイロードも増える。→ 却下（M-4 側で公開シンボルを再利用して自前再現）。
- **index ベースの整列**: M-1（strict）と M-2（nan_ok）は同一 ym でも銘柄集合・`all_yms`
  自体が異なり index 対応が壊れる。→ (ym, edinet_code) キーの intersection を採用。
- **メタ重みに切片/リッジ回帰**: 和1・非負の 1 自由度に対し過剰。look-ahead 容量も増える。
  → 却下（NNLS＋和1正規化）。
- **`base_models` のパラメータ化**: M-3 の dead option を UI に出すだけ。→ 定数化。

## Consequences

- M-4 の実行コストは概ね M-1+M-2 の合算（`return_stock_ids=True` で snapshot キャッシュ
  キーが分岐し CV は再計算・共有されるのは `load_data` のみ）。Render 軽量モードでは
  `heavy_render` で自動スキップ（従来 3 モデルと同じ）。
- `model_comparison` は 4 モデルになり最重量が 1 本増える。
- 判定は honest 基準: **M-4 の OOF rank-IC が max(M-1, M-2) を上回れば「多様性が効く」を
  定量実証し `mu_source` の推奨候補へ、上回らなければ「単体で十分」を確定**（どちらも
  モデル選択の確定知見・結果は本 ADR 末尾に実測で追記する）。
- テスト: 二段の無リーク性（月 t の y_true 破壊で t 以前の重み不変）・intersection・
  NNLS 復元・fold 生存（`n_periods>0`）を `tests/test_macro_ensemble.py` が固定。

### 実測（#367・offline 検証・2026-07-23・`scripts/measure_embargo_impact.py`＋`base_oof_backtest`）

honest（embargo=12）・キャッシュ価格再利用（週次97万行・#355・低Egress）・M-1 レグは offline 制約
（week_start プロキシで strict 全マクロが 0 サンプル）のため財務のみ（`SUB_PARAM_OVERRIDES`）:

| 対象 | rank-IC | long-short | n_oof |
|---|---|---|---|
| **M-4（stack・NNLS ≈ 50/50）** | **0.1593** | 0.129 | 13,539（9期） |
| M-1 を共通域に制限 | 0.1414 | 0.117 | 13,539（同一行） |
| M-2 を共通域に制限 | 0.1414 | 0.111 | 13,539（同一行） |
| （参考）M-1 単体・自グリッド | 0.1946 | 0.159 | 29,751（10期） |

**判定: 同一 (ym,銘柄) 域の apples-to-apples で M-4 は両基底を +0.018（相対 +13%）上回り
「多様性が効く」を定量実証**。ic_std も M-1 共通域比で低下（0.115 vs 0.125）。一方、M-1（財務
のみ）は自身のより広い母集団（M-2 が予測できない約1.6万行を含む）では 0.1946 と M-4 の共通域値を
上回る＝**M-1 の優位の実体は係数の質ではなく母集団の広さ**（共通域に制限すると M-2 と同値 0.1414
まで低下）。**`mu_source` の既定は当面 M-2 のまま**（M-4 は選択肢として利用可能）とし、本番
full-config（M-1 全マクロ strict）での `model_comparison` 実行で `base_oof_backtest` を確認後に
既定切替を判断する。

開発中の副産物: fold 月が各モデル自身の all_yms の **index 基準**のため、母集団差で月集合が
1 ヶ月ずれるだけで fold 月が全て位相シフトし (ym,ec) 交差が空になる実バグを offline 実測が検出
（M-1: 2023-02,05,08… vs M-2: 2023-06,09,12… で共通月ゼロ）。共通月グリッドへ揃えてから各 CV を
回す設計（`_m1_build`/`_m2_build` → `common_yms` → `_m1_cv`/`_m2_cv`）で根治し、回帰テスト
（`test_month_grid_misalignment_is_realigned`）で固定した。

参考: Wolpert, D. H. (1992). "Stacked Generalization." *Neural Networks*, 5(2), 241–259.
https://doi.org/10.1016/S0893-6080(05)80023-1 / Breiman, L. (1996). "Stacked Regressions."
*Machine Learning*, 24, 49–64. https://doi.org/10.1007/BF00117832
関連 ADR: 0003（M-1/M-2 公平性）, 0004（OOF 定義）, 0012（M-3 週次専用）, 0014（purge/embargo）。
