# M-2（マクロ×財務 勾配ブースティング）を M-1 の非線形兄弟として同期 in-execute で実装

## Status

accepted（2026-06-25・**設計決定**）。実装は GitHub Issue #234 で追跡。

## Context

VISION の核心は「投資判断モデルを自作・改善し続ける」こと。M-1（`macro_risk_return`）は線形（`LassoLarsIC(BIC)` → OLS 再フィット）で、交互作用を手動の `fin×macro` 交差項でしか表現できない。金利レジーム×バリュー、ボラ局面×収益性のような**非線形・高次の交互作用**は捉えきれない。勾配ブースティング（XGBoost）はこれを自動学習し、単調変換不変・欠損ネイティブという性質も持つ。

ここに設計上の緊張がある。ADR-0002 は「重い推論（per-stock 階層ベイズ＝`macro_beta`）は GitHub Actions の推論バッチへ分離し、本番 Render には載せない」という前例を確立した。XGBoost も「重い学習」であり、**同じくバッチ化すべきか**という問いが生じる。この ADR はその問いへの回答と、M-1 との比較を構造的に valid に保つためのアーキテクチャを記録する。

## Decision

1. **M-2 は M-1 の非線形兄弟**として新規独立プラグイン（`macro_gbdt`）。M-1 は温存・並置し、**同一の目的変数（52週先対数リターン）・同一スナップショット母集団・同一リスク-リターン幾何**（μ／R2／R3／[[系統的マクロリスク曝露]]／[[非効率的フロンティア]]）を共有する。価値は「線形（M-1）vs 非線形（M-2）を同一データで比較・改善し続けられる」こと。

2. **同期 in-execute・`heavy=True`（`macro_beta` バッチに倣わない）**。XGBoost 学習＋walk-forward CV＋SHAP は MCMC/NUTS と違い数秒〜数分の速度域で、M-1 の OLS 経路・`sector_ols` と同じ「同期実行・Render では 403・ローカル限定」regime に収まる。モデルは DB 永続化せず毎回学習する。ハイパラ探索は固定デフォルト＋`early_stopping` に限定し（grid/optuna なし）、同期実行を破綻させない。

3. **同一母集団・同一 CV を構造保証する共有モジュール**。`plugins/macro_snapshots.py`（新規）に M-1/M-2 共通面（スナップショットビルダー＋リーク感応の日付/マクロ/実現ボラ helpers＋`_MACRO_MAP` 正本＋producer スコア関数）を集約し、両プラグインが中立地から import する（**M-2→M-1 結合ゼロ**）。ビルダーは `build_interactions` フラグで交差項を opt-in 化（M-1=True / M-2=False）。`walk_forward_cv_monthly` には `fit_predict` コールバックを注入（default＝OLS で M-1 不変、M-2 は `early_stopping` 付き XGBoost）。これにより M-1 と M-2 が**同一母集団・同一 fold・同一 r2/rmse 式**を通り、比較が構造的に valid になる。

4. **内蔵比較は同一特徴量 OLS ベースライン**。M-2 は同一 fold で XGBoost と「同一特徴量（交差項なし・BIC なし）の素 OLS」を回し、side-by-side の `cv_metrics`（`mean_r2`/`mean_rmse`）を出す。モデルクラス効果（線形 vs 非線形）を isolate する公正な比較。「M-1 as deployed（交差項＋BIC 選択込み）との比較」は M-1 画面の `cv_metrics` 目視で別途行う。

5. **解釈は SHAP、R1 は撤去**。グローバル mean|SHAP|（M-1 の標準化係数 `feature_coefs` スロットに対応・**大きさのみ・方向なし**）＋per-stock SHAP（全社へ丸めて添付・stateless）。OLS 固有の R1（予測 SE）は出さない——R1 はそもそも効用軸でなく（`risk_axis` は r2/r_macro のみ・[[結果リスクと信頼度の分離]]）、欠落しても効用幾何は壊れない。R_macro は既存 `macro_beta` producer から流用し M-1 と軸パリティを保つ。

6. **専用レンダラ**。plugin-keyed の `renderMacroGbdt`（新ルートではない・`/analysis` 内）。散布図・フロンティアの低レベル補助は共有抽出し、per-stock SHAP パネルを追加。

7. **強正則化デフォルト・特徴量選択なし**。日本株 52週リターンは低シグナル（M-1 walk-forward R²≈0.01）。`max_depth=4`・`min_child_weight=5`・`subsample/colsample_bytree=0.8`・`reg_lambda=1.0`・`learning_rate=0.05` で過学習を抑制し、`n_estimators` は `early_stopping` が決定。BIC/LASSO 事前選択は行わず木の暗黙選択に委ねる（`max_features` パラメータは M-2 では持たない）。外れ値処理は特徴量 winsorize を撤去（木は単調不変）し、目的変数 y のみ p1-p99 winsorize（`reg:squarederror` で M-1 と同一の目的分布を学習）。

## Considered Options

- **XGBoost も `macro_beta` のようにバッチ化**（却下）：ADR-0002 のバッチ分離は MCMC が同期リクエスト時間内に終わらないことが理由。XGBoost は数秒〜数分でその制約に当たらず、バッチ化はモデル成果物の永続化スキーマ・バッチ結線・本番反映フローを無駄に増やす。M-1 の OLS 経路・`sector_ols` が既に「同期・heavy・ローカル限定」の前例。
- **M-2 専用に snapshot/CV を複製**（却下）：リーク感応コード（45日ラグ・52週ホライズン・カバレッジ）が2実装に分かれ drift し、「同一母集団で比較」が無効化しうる。共有モジュール＋injectable `fit_predict` が同一性を構造保証する。
- **R1 を quantile regression / ensemble 分散で代替**（保留）：信頼区間は価値があるが、R1 は効用軸でないため v1 の成立に不要。将来エンハンス（`reg:quantileerror` の 5/95 パーセンタイル）。
- **NaN ネイティブで coverage を緩める**（保留）：XGBoost は NaN を分岐で扱えるが、母集団が M-1 と変わり比較が M-2-vs-M-2 になる。v1 は coverage を M-1 と同一に据え置く。

## Consequences

- **M-1 への blast radius**：共有モジュール抽出で `macro_risk_return.py` が薄い consumer になり、`utils.py` の `_MACRO_MAP` 遅延 import 循環ハックが解消する。**parity テスト**（共有ビルダーが M-1/M-2 に交差項列を除き同一母集団を返す）＋既存 `test_macro_risk_return.py` の green 維持で挙動保存を守る。
- **新依存**：`xgboost`（Apache-2.0）・`shap`（MIT）を VISION/CLAUDE.md の採用基準で評価・承認後に `requirements.txt` へ完全 pin。本番 Render は `heavy=True` で 403 のため、これらが本番ランタイムで import されても実行はされない（ローカル限定）。
- **検証責務**：`early_stopping` の `eval_set` は train の時系列末尾（リーク安全・実運用設定を模す）、train 月数が閾値未満の早期フォールドは固定 `n_estimators` フォールバック、最終モデルは `best_iteration` で全データ refit。
- **将来エンハンス**（MODELS.md に記載）：inner-CV グリッド/optuna、quantile-regression 信頼区間、sector のカテゴリ特徴量化、SHAP interaction values、M-2 初心者ガイド。

実装・調査タスクは GitHub Issue #234（残タスクの正本）で追跡する。

## Addendum（2026-06-30）— マクロ NaN 許容を採用（v1 の「保留」を解除）

**変更**：Considered Options の「NaN ネイティブで coverage を緩める（保留）」を**採用**する。`build_snapshots` に `macro_nan_ok` フラグを追加し、**M-2 のみ `True`**（M-1 は `False` 据え置き）。マクロ特徴量が欠損したスナップショットを破棄せず `float('nan')` として保持する。

**動機**：本番でカバレッジの薄いマクロ系列（最近追加・凍結・年次のみ等）を1つ選ぶだけで、現在スナップショット日付（企業ごとに異なる最終週次株価日）にその系列が欠損する企業が**一斉に脱落**し、表示企業数が激減する回帰が観測された（全 all-or-nothing マクロフィルタ `any(v is None)` が原因）。マクロは日付単位で全企業共通かつ補完可能なため、**表示母集団をマクロ選択で決めるべきではない**。

**v1 で保留した理由（母集団が M-1 と変わり M-2-vs-M-2 比較になる）への回答**：
- 比較対象は「同一特徴量・同一 fold の素 OLS ベースライン」（Decision 4）であり、M-1 as deployed ではない。NaN 行も同一母集団で比較するため、OLS ベースライン経路（`fit_feature_columns`/`transform_feature_row`）に**学習フォールド列平均の NaN 補完**を入れた（学習統計のみ使用＝リークなし・正規化後ほぼ 0 の中立値）。XGBoost 経路は補完せず NaN のまま学習・予測・SHAP する。
- 財務特徴量の欠損は従来どおり厳格除外（企業固有・コア指標）。NaN 許容は**マクロのみ**。表示可否は `min_coverage`（既定 0.5）が graceful に制御。
- M-1（`build_interactions=True`・`macro_nan_ok=False`）の挙動は完全に不変（交差項に NaN を混入させない構造保証）。

詳細は MODELS.md §11.3.1。
