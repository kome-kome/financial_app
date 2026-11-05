# 事前登録: M-6 の内側 CV で α・l1_ratio を選ぶ基準（二乗誤差 か rank-IC か）

- 事前登録の Issue: https://github.com/kome-kome/financial_app/issues/1116
- 親: #1112（方針は ADR-0070 決定 7）。計測道具: `python -m scripts.momentum_gate --inner-cv-score`（日中枠 `gate:inner-cv-score`）。
- **凍結**: この文書は main へのマージで凍結する。凍結値は、マーカー行より上（本文）を改行 LF・UTF-8 にそろえた sha256（`scripts/prereg.py`）。
- 実走は日中キューで単独に回す。積むのは人（親 #1112）。

## 1. 問い

M-6（`macro_enet`）は、外側の各 fold の学習期間の中で、α・l1_ratio を内側 CV の**二乗誤差**で選んでいる（`ElasticNetCV`）。一方で M-6 の評価と探索の基準は rank-IC で、選ぶ基準だけが食い違っている。内側の検証月ごとの Spearman の平均が最大の組を選ぶ版（`rank_ic`）に替えると、今の版（`mse`）より買いの rank-IC が上がるか。M-6 は売りの既定の μ̂ の出どころでもあるので、売り側を悪くしないかも同時に問う。前例は両方向にある（M-4 の重みは NNLS から rank-IC の格子へ替えて効いた・ADR-0015／M-5 は学習の目的を順位にして M-2 に負けた・ADR-0017）ので、測る前に規則を固定する。

## 2. 対象

- モデル: M-6（`macro_enet`）だけ。M-2・M-1 にこの軸は無い。
- 条件: `mse`（`ElasticNetCV` の二乗誤差・本番・分母）と `rank_ic`（`model_candidates.fit_elasticnet` の `inner_score="rank_ic"`）の 2 つに固定する。
- 両側で同じもの: パネル（`panel_cond` で 1 枚を共有＝母集団は動かない）、外側の walk-forward（本番と同じ fold・embargo）、内側の区切り方（本番の `months_purged`）、試す候補（l1_ratio 0.1 / 0.5 / 0.9 × α 20 点。α の並びは `ElasticNetCV` と同じ式で、学習全体から l1_ratio ごとに 1 回作る）、予測の後処理（本番の規模の中立化・`_SIZE_NEUTRAL`）。
- `rank_ic` の点数: 内側の各検証月で、予測と**生のラベル**（winsorize 前）の Spearman を取り、全 split の検証月を 1 つに並べた単純平均。3 社未満の月と、予測が月内で全て同じ値の月は除く。同点は先に来た候補（l1_ratio は上の順・α は大きい側）。全候補の点数が出なければ最初の l1_ratio の最大の α。
- 間引き: なし（`--stride 1`・`--smoke` なし）。

## 3. 量

- 主審（2 本）: 共通 (ym, ec) 域で測った OOF の、(a) 買いの rank-IC の期別系列（`rank_ic_by_period`）、(b) 売り側spread の期別系列（`short_side_spread_by_period`）。ADR-0069 の主審（買いは rank-IC・売りは売り側spread）。
- 差: `rank_ic` − `mse`。両側に共通する期だけで対にする（`model_stats.paired_ic_significance`・定常ブートストラップ・**両側 p**）。
- 有意水準: Bonferroni で α = 0.05 / 2 = 0.025（1 モデル × 2 指標・`momentum_gate.bonferroni_alpha`）。
- 参考（検定しない・判定に使わない）: 上位 10 社超過の差の平均（両側に共通する期だけ・`OPERATIONAL_TOP_K`）、raw の水準、選ばれた α・l1_ratio と α パスの端に張り付いた fold の割合、`rank_ic` 側の内側の点数（`inner_rank_ic`）、両側の期数。

## 4. 判定

- **採用**: (a) の差が正で p < 0.025、**かつ** (b) の差が「負で p < 0.025」ではない（売り側spread は拒否権）。
- **据え置き**: それ以外。M-6 の本番の基準は `mse` のまま。
- 検定が出ない指標（共通の期が 2 未満）は有意でないものとして扱う（(a) が出なければ据え置き、(b) が出なければ拒否権は発動しない）。
- 判定は計測道具が `prereg_decision` に機械的に出す（`momentum_gate.inner_cv_score_decision`）。読み手は手で判定し直さない。
- 両側で期数が違う（係数が全て 0 の fold で予測が全社同じになり、その期が rank-IC から外れる）ときは、期数を記述したうえで、共通の期の対で判定する（規則は変えない）。
- 道具が非 0 で止まった（凍結値の不一致・選ぶ基準が CV に届かない・分母が本番でない）ときは判定しない。
- 採用でも、本番の既定（`macro_enet._INNER_SCORE`）の切り替えは親 #1112 で別の PR にする。

## 5. 手順

1. 日中キューに `gate:inner-cv-score` を積む（人・親 #1112）。計測道具は `--prereg` と `--prereg-sha`（追記節の凍結値）を必須にし、本文が凍結値と一致しなければ終了コード 2 で止まる。
2. 出力（`scripts/.cache/momentum_gate_inner_cv_score.json`）の主審の 2 本・判定・参考の値を、追記節の「結果」に書き写す。
3. 採用なら親 #1112 で本番の既定を切り替える PR を作る。据え置きなら ADR-0070 に結果を記録して閉じる。

## 6. 凍結し直す条件

計測道具を一度も回していない間に限り、本文と凍結値を同じ PR で変えてよい。回した後に対象・量・判定を変えるなら、新しい ADR で覆す。

<!-- prereg:append-below -->

## 追記節

凍結値（本文の sha256）: `11266083b9ac5bd7f4ac3443f6823766171ca84e4a4573cdb6e3a4656595e876`

### 結果

2026-11-05 の日中枠（並走なし・`.logs/daytime_20261105.log`・15.8 分・exit=0）で 1 回回した。出力は `scripts/.cache/momentum_gate_inner_cv_score.json`（`prereg_sha256` は上の凍結値と一致・道具の `[prereg] OK`）。

- **判定: 据え置き（`prereg_decision.decision = "keep"`）**。M-6 の本番の基準は `mse` のまま（`macro_enet._INNER_SCORE` は変えない）
- 母集団: パネル 1 枚を共有（75 か月・174,913 行・3,346 社）。共通 (ym, ec) 域は 51,156 行で、両側とも落ちた行は 0・外側の fold は両側 19
- 主審（`rank_ic` − `mse`・共通の期 n=13・Bonferroni α=0.025・両側 p）

  | 指標 | 差 | 95% CI | p | 判定 |
  |---|---|---|---|---|
  | (a) 買いの rank-IC | −0.0050 | [−0.0138, +0.0030] | 0.254 | 有意でない |
  | (b) 売り側spread | −0.0041 | [−0.0079, −0.0009] | 0.004 | **有意に負（拒否権）** |

  (a) が有意に正でないので、それだけで据え置き。(b) は拒否権の条件も満たしている（`rank_ic_better: false`・`short_side_spread_worse: true`）
- 参考（検定しない）: 上位 10 社超過の差の平均 +0.0483（共通の期 n=13）。raw の水準は `mse` が rank-IC +0.2070（std 0.1126）・売り側 +0.1097・LS spread +0.1652、`rank_ic` が +0.2037（std 0.1249）・+0.1066・+0.1603（各条件の自分の母集団・fold の位相が違うので比べない）
