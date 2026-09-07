# M-2 は解釈と基準線のために残し、μ̂ の供給者からは降ろす（退役とは別の第3の状態）

## Status

accepted（2026-09-07）。Issue [#572](https://github.com/kome-kome/financial_app/issues/572)。
[ADR-0044](0044-retire-underperforming-models-by-hiding.md) が Consequences に残した
「M-4 を退役させた以上『M-2 は何のために残っているか』は別途明文化する（#572）」への回答。
[ADR-0004](0004-m2-downstream-sell-and-oof-backtest.md)（M-2 を `mu_source` トグルへ連動させた決定）を
**`mu_source` の部分についてのみ supersede する**（OOF 連動・producer 契約・R1' はそのまま生きる）。

## Context

ADR-0044 で M-4（統合）と M-5（ランク学習）を退役させた結果、**M-2 の位置づけが宙に浮いた**。
M-2 を残す判断は「劣後しているか」だけでなく「**他に何を支えているか**」に依存しており、
支えのほうが先に消えたためである。

#572 で棚卸しした4つの支えは、いま次の状態にある。

| M-2 を残す理由 | 現状 |
|---|---|
| 1. M-4（`macro_ensemble`）の基底 | M-4 自体が ADR-0044 で退役。**支えとしては弱い**が、M-4 は削除されておらず実行すれば今も M-2 の μ̂ を読む |
| 2. SHAP による非線形の解釈 | **健在**。M-6 の `feature_coefs` は線形係数で「使われなかった特徴が係数0で読める」が、非線形の交互作用は表現できない。M-2 の mean\|SHAP\| はこれを見る唯一の窓 |
| 3. monotone constraints（ADR-0019）と分割コンフォーマル R1'（ADR-0020）の実装元 | **半分 M-6 へ移った**。R1' は #396 で M-6 にも同型実装済み |
| 4. 比較の基準線 | **健在**。ADR-0021 の bake-off は `xgb_m2` を基準線として全候補を測っており、`COMPARISON_MODELS` の M-2 行も同じ役 |

一方で M-2 は honest OOF rank-IC で M-6 に**有意に劣後**している
（0.1419 vs 0.1713・差 +0.0294・95%CI [+0.0116, +0.0469]・p=0.002・ADR-0021）。
売り側でも ADR-0022 で既定を M-2 → M-6 へ切替済み（`short_side_spread` +0.0656 vs +0.0511・p=0.001）。
**つまり「選べるが、選ぶ理由が実測で否定されている」状態が `mu_source` トグルに残っていた。**

ADR-0044 が用意した状態は「通常」と「退役（`hidden = True`）」の2つだけで、
どちらも M-2 には当たらない。退役させると 2 と 4 の役ごと画面から消える
（M-2 は静的タブ・SHAP パネル・per-stock SHAP を持ち、`hidden` は `/api/plugins` の1箇所で
サイドバーから外すだけなので**タブ本体を外す作業が別途要る**・ADR-0044）。
かといって現状維持だと、次に「モデルが多い」と感じたときに**同じ調査を最初からやり直す**。

## Decision

**`AnalysisPlugin.hidden` は立てない。`mu_source` の選択肢からだけ M-2 を外す。**
ADR-0044 の2状態に対し、**「表示するが供給者ではない」第3の状態**を置く
（CONTEXT.md の用語では[[供給者から降ろす]]）。

外すのは3箇所（二重管理なので必ず同時に直す）:

- `plugins/sell_ranking.py` の `mu_source` options
- `plugins/recommend.py` の `MU_SOURCE_OPTIONS`
- `templates/analysis.html` の静的 `<select id="sell-mu-source">`

`options` は `coerce_params`（`plugins/utils.py`）の membership 検証の入力そのものなので、
外した時点で `mu_source="macro_gbdt"` は **`execute_plugin` 経由で ValueError → 400 になる**。
「黙って既定へ倒す」ことはしない（効いていないことが画面から分からなくなるため・ADR-0030 と同じ作法）。

**据え置くもの**（第3の状態の実体そのもの）:

- `model_comparison.COMPARISON_MODELS` の M-2 行（基準線）
- `plugins/macro_ensemble.BASE_MODELS`（M-4 の基底）
- M-2 の SHAP 生成と `/analysis` の描画、静的タブ一式
- **月次探索の `--persist-scores`**（`scripts/run_monthly.py` の `TUNE_MATRIX`）と
  `nightly_scores.HEAVY_AUTOMATION` / `batch_freshness.PRODUCER_COVERAGE` / `plugins/progress.PROGRESS_COVERAGE`
- `plugins/macro_gbdt.py` の `produced_output` / `read_producer_scores` / `_persist_producer`
- `sell_ranking` の R3 足切りゲート実装（`mu_source not in ("macro_dlm", "macro_ensemble")` の行）。
  M-2 が来なくなるだけで、分岐そのものは無改変

**`--persist-scores` を残す理由**を明示しておく。μ̂ の消費者は `mu_source` だけではない——
M-4 は削除されておらず、実行すれば `macro_gbdt_scores` を基底として読む。
落とすと「M-4 を回したら基底が空だった」という**実行時にしか現れない壊れ方**を作る。
`macro_gbdt_scores` は全置換（スナップショット置換）なので、残しても容量は増え続けない。

`/api/morning` の `mu_source` クエリは**鮮度表示専用**で `recommend` へ結線されていない
（`get_producer_asof` を呼ぶだけ・ADR-0030）。membership 検証も持たないため、
`?mu_source=macro_gbdt` は今後も 200 を返す。`--persist-scores` 据え置きにより
`macro_gbdt_scores` は生き続けるので、返る as-of も正しい。**ここは変えない**
（表示専用の口に検証を足すと、鮮度が見たいだけの操作が 400 で止まる）。

## Consequences

- **M-2 は「アクションへ繋がらないモデル」になった。** 分析タブで実行でき、SHAP も出て、
  モデル比較にも並ぶが、売り判定と買い推奨には μ̂ を供給しない。
- **`mu_source` の選択肢を誰も守っていなかった穴を塞ぐ。** 既存の
  `tests/test_analysis_meta.py::TestHiddenPlugins` は「hidden ⇒ options から外れる」の
  一方向しか縛っておらず、**hidden でない M-2 が外れている状態は不変条件を持たない**＝
  黙って復活しても失敗として現れない。`mu_source` の options が期待集合と**完全一致**する
  ことを直接アサートするテストを足す。
- **「増やしたら登録表へ1行足す」系のレジストリは1つも変わらない。** 探索も永続化も続くため
  `HEAVY_AUTOMATION` / `PRODUCER_COVERAGE` / `PROGRESS_COVERAGE` は据え置き＝
  月次バッチの所要も変わらない。M-2 を本当に降ろすときは、この3つと `TUNE_MATRIX` の
  予算（`Step.budget_min`）がセットで動く。
- **戻すときも同じ手続きを踏む**（ADR-0044 の作法を継承）。`options` へ1行足すだけでなく、
  同一世代のパネルで測り直した実測を添えて ADR を追記する。
  ADR-0021 の 0.1419 vs 0.1713 は `lag_days` 是正前・#411 の履歴延伸前の 43ヶ月パネルの値で、
  #570 の実測では M-2 は 17期パネルで 0.1578 だった＝**世代をまたいで絶対値を比較しない**。
- **M-6 に SHAP 相当の非線形解釈を用意する道は閉じていない**（#572 の案B）。用意できた時点で
  M-2 は 2 の役を失い、残るのは 4（基準線）だけになる＝そのとき初めて `hidden` の判断が立つ。

## Considered Options

- **案A（採用）: 解釈と基準線に限定して残す。** 実測を追加で回さずに棚卸しだけで確定でき、
  月次探索も無改変なので平日日中バッチのキュー（`tune:macro_gbdt`）と競合しない。
- **案B: M-6 に SHAP 相当の非線形解釈を用意してから M-2 も退役。** 線形係数では見えない
  交互作用を別手段（M-6 残差に対する木モデルの部分依存など）で賄えるなら成立するが、
  退役の前に `python -m scripts.model_comparison_run --models macro_gbdt,macro_enet` を
  **同一世代で**回す必要があり（ADR-0047）、その計測は `tune:macro_gbdt` と同じ土俵を使う。
  今のキューと競合するため見送った。**否決ではなく順序の問題**で、案Aは案Bの前提を壊さない。
- **案C: 現状維持（何も決めない）。** 選択肢に残る理由が実測で否定されたままになり、
  #572 が指摘した「次に『モデルが多い』と感じたときに同じ調査をやり直す」状態が続く。却下。

関連 ADR: 0004（M-2 の `mu_source` 連動・本 ADR が一部 supersede）, 0015（M-4 スタッキング）,
0019（monotone constraints）, 0020（分割コンフォーマル R1'）, 0021（候補メニューと昇格ゲート）,
0022（既定 mu_source の M-6 化）, 0030（買い側 μ̂ は既定 OFF）, 0044（hidden による退役）,
0047（探索の品質ゲートは同一パネル上でしか比較しない）
