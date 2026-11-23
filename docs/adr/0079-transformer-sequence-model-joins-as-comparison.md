# 時系列 Transformer（M-7）を比較用のモデルとして足し、事前登録の判定で処遇を決める

## Status

**accepted（2026-11-23）**。#1317（実装）・#1316（計測と判定）。処遇（昇格の検討・据え置き・退役）は
事前登録 [`docs/prereg/m7-transformer.md`](../prereg/m7-transformer.md) §4 の機械的な判定に従う。

## Context

朝の買い候補を出す M-6（ElasticNet）と M-2（XGBoost）は、どちらも「1 社×1 月末」の断面を 1 行として見る。
各社の過去の値動きの**並び**を読むモデルは M-3（銘柄ごとの状態空間）だけで、M-3 は比較用のうえ成績も弱い。
利用者から「Transformer のような時系列の要素を入れた分析で、買う株を検討する仕組みを作って」と依頼があった（2026-11-23）。

先に分かっている前例:

- 株価由来の特徴（12-1 モメンタム）は、母集団をそろえると M-2/M-6 の成績を良くしなかった（[ADR-0045](0045-momentum-gains-vanish-when-the-population-is-aligned.md)）
- 木の M-2 も線形の M-6 を超えていない。低 S/N の日本株では非線形の柔軟さより縮小推定が効いた（MODELS.md §15・§16）
- 柔軟なモデルほど未来の情報の混入を吸う。M-2 は purge で rank-IC が 0.348 → 0.141 に下がった（[ADR-0014](0014-purge-embargo-walk-forward.md)）
- 順位で学習した M-5 は、二乗誤差の M-2 に負けた（[ADR-0017](0017-m5-learning-to-rank.md)）

## Considered Options

1. **比較用のモデルとしてプラグインにし、画面とモデル比較に出す（採用）**: 朝の買い候補は変えない。
   測る前に判定の規則を事前登録し、結果どおりに処遇を決める
2. 候補メニュー（`plugins/model_candidates.py`）に置いて `candidate_bakeoff` で測ってから昇格させる（[ADR-0021](0021-sibling-model-candidate-menu.md) の順）:
   候補メニューは M-2 と同じ特徴行を受け取る兄弟の置き場で、M-7 の入力（社ごとの 52 週の系列）とは形が違う。
   測る前から買い候補の画面が見たいという利用者の依頼にも合わない。M-3 も同じ理由で直接プラグインにした
3. 朝の買い候補に最初から並べる: 検証前のモデルで実際の買いが変わる（[ADR-0070](0070-search-selects-in-two-stages-for-top-k-buying.md)・[ADR-0078](0078-portfolio-construction-stays-merged-equal-weight.md)）
4. PyTorch・flax で書く: 新しい依存（評価と承認が要り、Windows では大きい）で、未署名の DLL が増える。
   jax はローカルの venv と `requirements-inference.txt` に既にあり、小型の Transformer と AdamW は手で書ける

## Decision

- M-7 = `plugins/macro_transformer.py`（category「比較用のモデル」・heavy）。JAX の実装は `plugins/seq_transformer.py`、
  系列の組み立ては `plugins/weekly_sequences.py`（numpy だけ）
- 答え・パネル・fold は M-2/M-6 と同じ（52 週先の対数リターン・M-6 のスキーマ既定のパネル・6/3/embargo 12）。
  上場 52 週未満の社も行を落とさない（ADR-0045）
- `model_comparison.COMPARISON_MODELS` に `M-7` として足す
- μ̂ は `macro_transformer_scores` へ全置換する。読むのは分析画面の保存済み一覧だけで、朝の推奨・売り判定・
  おすすめ銘柄（`MU_SOURCE_OPTIONS`）・予測の写し（`prediction_snapshots`）は読まない
- 暦は持たない（`HEAVY_AUTOMATION`・`PRODUCER_COVERAGE` は exempt）。日中キューの `model:macro_transformer` を人が積んだときだけ回る
- 探索空間は持たない。比べるときの設定はスキーマ既定（ADR-0070）
- CI で jax のテストを回すため、`requirements-dev.txt` に `requirements-inference.txt` と同じ版の jax・jaxlib を足す。
  本番の `requirements.txt` には入れない（Render では heavy の分析を回さない）

## Consequences

- 画面のモデル比較の組が 15 → 21 に増え、▲▼ の Bonferroni の基準が 0.05/15 から 0.05/21 に締まる。
  既存の組の ▲▼ が消えることがある。画面から比較を回すと M-7 の分だけ長くなる（見積り数十分・未実測）
- `walk_forward_cv_monthly` は fit_predict の例外を黙って捨てるので、M-7 は例外を記録して CV の後に送出し、
  fold の数も期待と照合する（黙って縮んだ OOF で比べない）
- ローカルでは Smart App Control が jaxlib の部品を止める日がある（docs/GOTCHAS.md）。その日は M-7 を回せない
  （データを読む前に止まる）。日中キューのジョブは `deps_smoke` を先に回す
- 計測までは実データで M-7 を回さない（事前登録 §6）
- 判定が退役なら [ADR-0044](0044-retire-underperforming-models-by-hiding.md) の手順で `hidden=True` にする（削除しない）

## 関連

- 事前登録: [`docs/prereg/m7-transformer.md`](../prereg/m7-transformer.md)
- Issue: #1316（計測と判定）・#1317（実装）
