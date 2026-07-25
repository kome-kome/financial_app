# M-2 に monotone_constraints で経済符号の事前知識を注入

## Status

accepted（2026-07-25）。Issue #366 の設計決定。ADR-0003（M-2 設計）の追補。

## Context

M-2（`macro_gbdt`）は M-1 の BIC/LASSO のような特徴量の事前選択を持たず、全特徴量を勾配ブースティング決定木へ丸投げして「木の暗黙選択に委ねる」（MODELS.md §11）。低 S/N な日本株 52週先リターンでは、この自由度が**符号が経済理論と逆の過学習分岐**（例: 高 PBR ほど将来リターンが高い、と局所的に学習してしまう）を生みうる。

さらに解釈側でも、M-2 の唯一の特徴量寄与指標である SHAP は `np.abs(shap).mean()`（`plugins/macro_gbdt.py::execute`）で**大きさのみ・方向を持たない**。結果として、価値・クオリティ・レバレッジといった符号がほぼ確立した経済的事前知識が、**学習にも解釈にも一切使われていない**。

XGBoost は `monotone_constraints`（各特徴量に対し予測を単調増 +1 / 単調減 −1 / 制約なし 0）を native に持ち、追加パッケージ不要・次元不変で符号の事前知識を木構造へ直接注入できる。

## Decision

**符号が経済理論から明確な財務比率のみに単調性制約を課すトグル `use_monotone_constraints`（checkbox・既定 OFF）を M-2 に追加する。**

1. **符号表を単一の定数へ集約**（`plugins/macro_gbdt.py::_MONOTONE_SIGN`・唯一の源）:
   - **−1**: `pbr` / `per`（割高→バリュープレミアムの逆→将来リターン低）、`de_ratio`（高レバレッジ→財務リスク高→将来リターン低）
   - **+1**: `roe` / `roa` / `op_margin`（高収益性=クオリティ→将来リターン高）、`div_yield`（高インカム→将来リターン高）
2. **収載しない = 0（制約なし）**: マクロ系（符号がレジーム依存）・業種内Zスコア（`z_*`）・曖昧な成長/流動性指標（`equity_ratio` / `sales_growth` / `profit_growth` / `current_ratio`）・モメンタム・`px_*`・交差項。「符号に確信のあるものだけ ±1」の保守原則。
3. **列位置整合タプルで注入**: `_build_monotone_constraints(all_feat_names)` が `all_feat_names` の並び順に沿って `_MONOTONE_SIGN.get(name, 0)` のタプルを組み、`xgb_params` へ入れる。numpy 入力は列名を持たないため位置整合が必須。`xgb_params` 経由のため **CV の `fit_predict`（`_make_cv_callback`→`base_params`）と最終モデル（`final_params`）双方**へ自動伝播する。
4. **既定 OFF**: `use_momentum` / `px_*` と同じ保守ゲート。OOF rank-IC（§11.7）の ON/OFF 比較で有効性（特に **fold 間 std の低下＝頑健化**）を確認してから既定化を判断する。
5. **ハイパラ探索軸に追加**: `tuning_search_space` へ `use_monotone_constraints ∈ {False, True}` を1軸追加（合計10軸）。`monotone_constraints` は `build_snapshots` のキャッシュキーに影響しない純 `xgb_param` のため、スナップショット再構築を誘発せず LRU も圧迫しない。

## Consequences

- **学習**: ON 時、符号が理論と逆の分岐が木構造レベルで禁止される。制約は正則化として働き、低 S/N で過学習を抑止しうる。機能的検証（xgboost 3.3.0・`tree_method=hist`）で −1 は予測の非増加、+1 は非減少を厳守することを確認済み（サイレント無視でない）。
- **解釈**: 収載特徴の符号がモデル構造として保証され、SHAP の大きさ情報と併せて方向が読める。M-7 の signed SHAP と併用すれば符号表そのものの妥当性検証も可能。
- **M-5（`macro_gbdt_rank`・§14）への波及**: M-5 は `params_schema`（`super()` 継承でトグルを取得）と `execute` を継承するため、同一符号表が **XGBRanker** にもそのまま効く。ランカーも `monotone_constraints` を受理し、順位スコアは μ̂ と同方向のため符号の意味は保たれる（別途のコード変更不要）。
- **既定挙動は不変**: OFF が既定のため、既存の M-2 producer μ̂（`macro_gbdt_scores`）・model_comparison・OOF 値は変わらない。ON は明示選択でのみ有効。
- **リスク**: ある符号が実データで逆だった場合、制約が誤りを固定して性能を害しうる。これを「符号に確信のある財務比率のみ ±1・曖昧なものは 0」で緩和し、最終判断を OOF の ON/OFF 比較に委ねる（既定 OFF の理由）。

## Alternatives considered

- **全特徴に符号を付与**: マクロ・`z_*`・成長/流動性は符号がレジーム依存または不明瞭で、誤った制約が過学習より有害になりうる。確信のある財務比率のみに限定して却下。
- **BIC/LASSO 型の事前選択を M-2 に導入**: M-2 の設計思想（木の暗黙選択・M-1 との対比・ADR-0003）を崩す。単調性制約は選択でなく符号方向の注入で、この思想に非抵触。
- **既定 ON**: 検証前に producer μ̂ を変えてしまう。`use_momentum`/`px_*` の「検証→既定化」慣行に反するため既定 OFF。
- **交差項の手動生成で符号を表現**: ADR-0002 が M-1 で交差項を却下済み。本決定は交差項と別軸（既存特徴の符号方向の制約のみ）で非抵触。

## 参考

- XGBoost Monotonic Constraints: https://xgboost.readthedocs.io/en/stable/tutorials/monotonic.html
- Chen & Guestrin 2016, "XGBoost: A Scalable Tree Boosting System", KDD. https://doi.org/10.1145/2939672.2939785
- 関連 ADR: 0002（M-1 交差項却下）, 0003（M-2 設計・M-1 との公平比較）, 0007（ハイパラ探索基盤）, 0017（M-5 learning-to-rank）。
- 関連 Issue: #366（本 ADR）。
