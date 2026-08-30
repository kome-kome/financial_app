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

1. **新プラグイン `plugins/macro_ensemble.py`（M-4・`heavy=True`・`ui_order=370`）**。
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
- 本番で3マクロ特徴が全期間 None（#379: `macro_jp_real_gdp_yoy`/IMF WEO 2系列・変換窓不足）
  だと strict の M-1 レグが全滅するため、`_drop_dead_macro_features` が全プローブ日 None の
  特徴を自動除外して M-1 レグを生存させる（除外は `dropped_macro_features_m1` で明示・
  M-3 の `_MIN_FACTOR_COVERAGE` と同思想）。
  **#379 の family-wide 修正（`_macro_from_cache` の低頻度系列フォールバック）後は 41 特徴すべてが
  生存し本ガードは不発（`dropped_macro_features_m1=[]` を実測で確認・2026-07-24）**。将来
  マクロ系列を追加したときの再発防止として残置する（除去はしない）。

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

### 追試（#379 修正後・M-1 を本番既定 config で・2026-07-24・同スクリプト）

#379 で M-1 strict（全マクロ）が復活したため、`SUB_PARAM_OVERRIDES` を外し**3モデルとも本番既定
config** で再測した（honest embargo=12・同一キャッシュ価格）:

| 対象 | rank-IC | long-short | n_oof（fold 数） |
|---|---|---|---|
| **M-4（stack・NNLS 0.406/0.594）** | **0.1146** | 0.076 | 3,122（**2期**） |
| M-1 を共通域に制限 | 0.1138 | 0.116 | 3,122（同一行） |
| M-2 を共通域に制限 | 0.1023 | 0.058 | 3,122（同一行） |
| （参考）M-1 単体・全マクロ strict | 0.1572 | 0.153 | 6,131（**2期**） |
| （参考）M-2 単体・自グリッド | 0.1414 | 0.111 | 13,539（9期） |

**M-4 > 両基底の順序は保たれる**（対 M-2 +12%）が、当時は **M-1 strict の fold が 2 期しか立たず
統計的な結論に使えなかった**。原因は #379 の変換ではなくマクロ**収集開始日**の差で、`HY_OAS`/`IG_OAS`
のみ 2023-06 開始 → strict の snapshot が 24ヶ月に律速 → `min_train_months=6`+embargo=12 で fold 2 期。

### 再測（#381 解決後・2026-07-24・同スクリプト）

#381（ADR-0016）で真因が判明した——`HY_OAS`/`IG_OAS`（FRED ICE BofA）は 2026-04 以降ローリング3年窓に
制限され再収集でも遡れない。既定の信用ファクターを非ICE代替 `BAA_SPREAD`（`BAA10Y`=Baa−10Y・2016〜）へ
移し `HY_OAS`/`IG_OAS` を既定から除外した結果、**fold が実用水準へ回復**した（honest embargo=12・
同一キャッシュ価格）:

| 対象 | rank-IC | long-short | n_oof（fold 数） |
|---|---|---|---|
| **M-4（stack・embargo=12）** | **0.1569** | 0.121 | 13,539（**9期**） |
| M-1 を共通域に制限 | 0.1513 | 0.135 | 13,539（同一行） |
| M-2 を共通域に制限 | 0.1430 | 0.112 | 13,539（同一行） |
| （参考）M-1 単体・BAA 込み strict | 0.1982 | 0.167 | 29,751（**10期**） |
| （参考）M-2 単体・自グリッド | 0.1430 | 0.112 | 13,539（9期） |

**M-4 > 両基底の順序が統計的に意味を持つ fold 数（9期）で確認できた**（対 M-2 共通域 +9.7%・対 M-1
共通域 +3.7%）。M-1 単体は自身の広い母集団（BAA で 24→約48ヶ月・fold 10期）で 0.1982 と最良。以前の
「fold 2 期で結論不可」caveat は解消。`mu_source` の既定切替判断は本値（honest・fold 9〜10期）を根拠に
別途行える段階になった。→ **2026-07-30 の #402 / ADR-0022 で決着**: 既定は M-4 ではなく **M-6 単体**
へ切替（M-4 − M-6 は rank-IC で p=0.810・売り側 spread で p=0.655 の互角＝本 ADR の「統合が単体最良を
上回らなければ単体で十分」判定に該当）。

開発中の副産物: fold 月が各モデル自身の all_yms の **index 基準**のため、母集団差で月集合が
1 ヶ月ずれるだけで fold 月が全て位相シフトし (ym,ec) 交差が空になる実バグを offline 実測が検出
（M-1: 2023-02,05,08… vs M-2: 2023-06,09,12… で共通月ゼロ）。共通月グリッドへ揃えてから各 CV を
回す設計（`_m1_build`/`_m2_build` → `common_yms` → `_m1_cv`/`_m2_cv`）で根治し、回帰テスト
（`test_month_grid_misalignment_is_realigned`）で固定した。

## 追記（2026-07-30・Issue #397）: 基底に M-6（ElasticNet）を追加

ADR-0021 の bake-off で M-6 が M-2 を honest OOF rank-IC で有意に上回った（0.1713 vs 0.1419・
p=0.002）ことを受け、**基底を M-1+M-2 → M-1+M-2+M-6 へ拡張**した。判断の骨子:

- **下振れリスクが構造的に小さい**: 二段目は非負・和1の NNLS。効かない基底は重み ~0 に落ちる
  （実際、スモークパネルでは M-6 の重みが 0.0 になり統合は 2 基底時とほぼ同値に留まった）。
- **レグを `BASE_MODELS` 駆動へ一般化**した（build/CV/現在μ̂ を名前でディスパッチ）。基底の
  増減が定数変更だけで済み、**構成間の実測比較そのものが可能**になる。`_fit_weights` /
  `_stack_walk_forward` は n 基底一般化（`rank_ic_grid` は n 次元シンプレックス格子
  `_simplex_grid` を走査。格子点数 C(steps+n−1, n−1) が上限超で刻みを自動で粗くする）。
- **M-2 と M-6 は build 契約が同じ**（交差項なし・macro_nan_ok）ため、config 同値なら
  スナップショットを共有する（`_same_build_config`）。増分コストは ElasticNet の CV のみ。
- **判定は base-on-common を厳守**: 基底が増えると共通 (ym,ec) 域は狭まりうるので、2 基底構成
  との単純比較は母集団差を含む。主判定は「3 基底 M-4 vs 同一共通域に制限した各基底」で行う
  （`scripts/ensemble_base_bakeoff.py` が両構成を同一 honest 前提で実測し、ADR-0018 の
  定常ブートストラップで有意差を出す）。
- **M-5 は引き続き除外**（順位スコアで水準を持たない・ADR-0017）。M-3 の除外理由も不変。

### 実測（本番パネル・honest OOF・embargo=12・2026-07-30）

`python -m scripts.ensemble_base_bakeoff`。パネル: 3,979 社 / 9 fold / 共通 OOF 13,539 ペア。
**M-6 は M-2 と build を共有するため共通 (ym,ec) 域は 2 基底時と完全に同一（13,539 で不変）**
＝2基底 vs 3基底の比較も母集団差を含まない（当初 caveat として想定した狭まりは起きなかった）。

> **共通域を狭めているレグ（2026-08-01 実測・Issue #411・`python -m scripts.measure_strict_binding`）**:
> M-1 build（strict・交差項あり）は **47ヶ月 / 111,210 サンプル**、M-2 build（交差項なし・価格特徴あり）は
> **43ヶ月 / 57,955 サンプル**（同日中に ADR-0025 で履歴を延伸したため、以後は 71ヶ月 / 173,836 対
> 67ヶ月 / 91,482。非対称の向きは不変）。つまり intersection を絞っているのは **M-2 契約側**（`px_high52dev` の
> 52週 warmup 等）で、**「M-1 strict が共通域を律速する」は誤り**（strict は nan_ok と同一母集団＝
> 1行も落としていない）。ADR-0015 §Consequences の「M-1 の優位の実体は母集団の広さ」はこの実測と整合する。

| 構成 / 基底（同一共通域） | rank-IC | IC std | LS spread |
|---|---|---|---|
| M-4(M-1+M-2)・nnls | +0.1468 | – | – |
| **M-4(M-1+M-2+M-6)・nnls** | **+0.1692** | 0.1185 | +0.1321 |
| 基底 M-1（共通域） | +0.1142 | 0.1058 | +0.0978 |
| 基底 M-2（共通域） | +0.1419 | 0.1117 | +0.1117 |
| 基底 M-6（共通域） | +0.1713 | 0.1171 | +0.1359 |

有意差（`paired_ic_significance`・定常ブートストラップ・n=9 期）:

- 3基底 − 2基底: **+0.0224**・95%CI [+0.0112,+0.0344]・p=0.001 → **有意に改善**
- 3基底 − M-2: **+0.0273**・95%CI [+0.0171,+0.0382]・p=0.001 → 有意
- 3基底 − M-1: +0.0550・95%CI [−0.0013,+0.1209]・p=0.062 → 非有意
- 3基底 − M-6: **−0.0021**・95%CI [−0.0112,+0.0080]・p=0.632 → **M-6 単体と互角（超えない）**

**知見①**: 基底追加で M-4 は有意に改善したが、**M-4 は M-6 単体を上回らない**。

**知見②（既定 `weight_method` を `nnls` → `rank_ic_grid` へ変更した理由）**: 全共通 OOF で学習した
最終重み `w_final` は NNLS だと **M-6 = 0.0**（M-1 0.2563 / M-2 0.7437・2基底時と同一解）になる。
fold 別に見ると改善は序盤（学習月が少なく等重みに近い期）に集中し、2024-06 以降はほぼ 0 に収束する。
原因は二段目 NNLS が **MSE 最小化**である一方、評価は **rank-IC** であること（ADR-0007 が
`auto_hyperparams` を撤去した「周辺尤度 ≠ OOF rank-IC」と同型の目的関数不一致）。縮小推定で
予測分散が小さい M-6 は MSE 基準で不利になり、学習データが増えるほど重みを失う。この非整合は
**現在μ̂（producer）に直接効く**——`w_final` に M-6 が入らないため、実運用の統合μ̂が実質 M-1+M-2
のままになり、OOF の改善が運用へ届かない。

`rank_ic_grid`（n 次元シンプレックス格子・231点）で再実測した結果:

| 構成 | rank-IC | IC std | LS spread | 最終重み（M-1 / M-2 / M-6） |
|---|---|---|---|---|
| M-4 3基底・nnls | +0.1692 | 0.1185 | +0.1321 | 0.2563 / 0.7437 / **0.0000** |
| **M-4 3基底・rank_ic_grid（新既定）** | **+0.1720** | 0.1197 | +0.1291 | 0.30 / 0.10 / **0.60** |

- grid − nnls(3基底): +0.0028・p=0.326 → **OOF 性能自体は誤差レベル**
- grid − M-4(2基底): +0.0252・p=0.002 → 有意
- grid − M-2: +0.0301・p=0.001 / grid − M-1: +0.0578・p=0.040 → いずれも有意
- grid − M-6: +0.0006・p=0.810 → **M-6 単体と互角**

**決定**: 既定を `rank_ic_grid` にする。性能差は誤差レベルだが、(1) 評価指標と学習目的が一致し、
(2) 現在μ̂へ M-6 が 0.60 の重みで反映されて OOF と運用の乖離が消える。追加コストは +32 秒
（本番パネル 231 格子点）で許容範囲。`nnls` は UI から引き続き選択できる。

**未解決（Issue #402）**: M-4 はどの構成でも M-6 単体を上回らない（p=0.810）。ADR-0015 本文の
「上回らなければ単体で十分が確定知見」に該当するため、**売り候補ランキングの既定 μ 出所を
M-6 へ切り替えるか**を `/api/backtest`（`source=sell`）で検証する Issue #402 を起票した（#396 で
既定を M-2 に据え置いた判断に、実測根拠が揃ったため）。M-4 自体は M-2/M-1 に対しては有意に
優れるため据え置く（比較ファミリーの一員としての役割も変わらない）。

## 追記（2026-08-30・Issue #570）: M-4 を退役（GUI 非表示・`mu_source` から除去）

上の実測（`grid − M-6`: +0.0006・p=0.810／売り側 spread p=0.655）は、本 ADR 本文が定めた
**「統合が単体最良を上回らなければ『単体で十分』が確定知見」に該当する**。ADR-0022 が既定
`mu_source` を M-6 単体へ切り替えて以降、M-4 は「選択肢としては残っているが誰も選ばない・
`HEAVY_AUTOMATION` でも exempt＝自動更新経路が無い」状態で、**実行コストだけが基底
M-1+M-2+M-6 の合算**という非対称が続いていた。

[ADR-0044](0044-retire-underperforming-models-by-hiding.md) の手続きで退役させる:

- `plugins/macro_ensemble.py` に `hidden = True`。サイドバー「③ 将来リターンを予測」から消える。
- `sell_ranking` / `recommend` の `mu_source` options と `templates/analysis.html` の静的
  select から除去（既定 `macro_enet` は不変）。
- **プラグイン本体・テスト・`model_comparison.COMPARISON_MODELS` の M-4 行は残す**。基底を
  増やしたときの再評価は `python -m scripts.model_comparison_run --models macro_ensemble,macro_enet`
  で再開でき、`scripts/ensemble_base_bakeoff.py` も従来どおり使える。

退役は「統合という発想が誤り」を意味しない。本 ADR の実測は **M-1+M-2+M-6 という基底構成での
結論**であって、誤差の低相関な基底（例: 週次の M-3 を水準へ写像できたとき、あるいは M-2 と
異なる情報源を持つ新兄弟）が現れれば前提は変わる。そのときは hidden を外す前に、
base-on-common（同一共通域での各基底との比較）で測り直すこと。

参考: Wolpert, D. H. (1992). "Stacked Generalization." *Neural Networks*, 5(2), 241–259.
https://doi.org/10.1016/S0893-6080(05)80023-1 / Breiman, L. (1996). "Stacked Regressions."
*Machine Learning*, 24, 49–64. https://doi.org/10.1007/BF00117832
関連 ADR: 0003（M-1/M-2 公平性）, 0004（OOF 定義）, 0012（M-3 週次専用）, 0014（purge/embargo）。
