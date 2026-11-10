# 事前登録: 売り専用に学習した候補 S2（下側 20% 点の分位回帰）を、売りの μ̂（M-6）と比べる

- 事前登録の Issue: https://github.com/kome-kome/financial_app/issues/1158
- 計測道具: `python -m scripts.sell_mu_source_bakeoff --sell-candidates --sell-set s2`（日中枠 `measure:sell-quantile`）。
- **凍結**: この文書は main へのマージで凍結する。凍結値は、マーカー行より上（本文）を改行 LF・UTF-8 にそろえた sha256（`scripts/prereg.py`）。
- 実走は日中キューで単独に回す。積むのは計画を承認したセッション（#1158）。
- #1152 の事前登録（`docs/prereg/sell-dedicated-model.md`）は凍結済みで変えない。この文書はその続きの別の計測。

## 1. 問い

売り候補ランキング（`sell_ranking`）は、M-6（`macro_enet`）の μ̂ を昇順に並べ、保有の下位 1/5 を SELL にしている（ADR-0071）。M-6 は 52 週先リターンの**平均**を二乗誤差で当てる。売りで知りたいのは「大きく下がる社」なので、当てる答えを**下側 20% 点**（分位 τ = 0.2）に替えた候補 S2 は、売りとして M-6 の μ̂ より当たるか。分位回帰は平均ではなく分布の下側を直接当てる（Koenker & Bassett 1978）。#1152 の S1（下位 1/5 に入る確率）は据え置きだった（上位層の差 +0.0029・p = 0.588）。S2 は S1 と違い、下位に入るかどうかの 0/1 ではなく、下側の水準そのものを当てる。今の手法は強く、期は 17〜19 しかないので、最もありそうな結果は「差なし」。だから測る前に規則を固定する。

## 2. 対象

- 候補: **S2 だけ**。S1 は #1152 で判定済み（keep）なので測り直さない。
- S2 の当てる答え: 52 週先リターンの下側 τ = 0.2 点。学習器は L1+L2 の線形分位回帰（`plugins.model_candidates.make_quantile_enet_fit_predict`）。
  - 損失: ピンボール損失の角を幅 h = 0.1（標準化した y の単位）の二次関数で丸めたもの（Huber 型・`smoothed_pinball`）。
  - 罰則: α(l1_ratio·|w|₁ + (1−l1_ratio)/2·|w|²)（sklearn の ElasticNet と同じ形・切片は罰しない）。
  - 解き方: FISTA（Beck & Teboulle 2009）、運動量は勾配の向きで再始動（O'Donoghue & Candès 2015）。α 1 点あたりの反復の上限 300、収束の許容 1e-5。α の経路は上端から温めて順に解く。
  - α の経路: 係数が全て 0 のままになる最小の α から、比 1e-3 まで等比で 20 点（M-6 と同じ点数と比・`quantile_alpha_grid`）。
  - l1_ratio は本番の M-6 と同じ候補。内側 CV の区切り方と分割数も本番の M-6 と同じ（`macro_enet._INNER_CV`・`_CV_SPLITS`）で、検証行の**丸めない**ピンボール損失の平均を split で平均した値が最小の組を選ぶ（同点は α の大きい側・l1_ratio の与えた順）。選んだ組で全学習行を解き直す。
  - 予測は分位の予測値を元の尺度へ戻したもの（高いほど売る理由が小さい＝μ̂ と同じ向き）。
- 分母: 本番の売りの μ̂ の出どころ M-6（`sell_ranking.MU_SOURCE`）。
- 両側で同じもの: M-4 の中の M-6 のレグ（`MacroEnsemblePlugin._m6_cv`）が受け取るパネル・月（特徴量は本番の M-6 の既定）、外側の walk-forward（同じ fold・embargo）、前処理（学習 fold だけで fit する winsorize → zscore。y も M-6 と同じく winsorize → zscore）、OOF への規模の中立化（`macro_enet._SIZE_NEUTRAL`）。違うのは当てる答え（損失）と学習器だけ。
- 比べる域: M-4（3 基底）の共通 (ym, ec) 域。共通域の行が候補に無い、または実現リターンが食い違うときは道具が止まる。
- 間引き: なし（`--smoke` なし）。

## 3. 量

- 主審（2 本）:
  - (a) 上位層の売り側spread の期別系列（`in_layer_short_side`）。上位層は 1 つ前の test 期に **M-6 の買いの並び（μ̂）** で上位 20% だった社のうち、その期にも予測がある社（#1152 と同じ）。
  - (b) 全社の売り側spread の期別系列（`whole_universe_short_side`・ADR-0069 の売りの主審）。
- 差: S2 − M-6。両側に共通する期だけで対にする（`model_stats.paired_ic_significance`・定常ブートストラップ・**両側 p**）。
- 有意水準: Bonferroni で α = 0.05 / (1 候補 × 2 指標) = 0.025（`sell_mu_source_bakeoff.sell_alpha`）。
- 参考（検定しない・判定に使わない）: M-2・M-1 の買いの並びで作った上位層の (a)、当たり率、precision・誤売りの損、各売りのキーを買いの並びとして読んだ rank-IC、選ばれた α・l1_ratio・非ゼロ係数の数・α パスの端への張り付き・反復の上限に当たった α の数、両側の期数、所要。

## 4. 判定

- **採用**: (a) の差が正で p < 0.025、**かつ** (b) の差が「負で p < 0.025」ではない（全社の売り側spread は拒否権）。
- **据え置き**: それ以外。`sell_ranking` は M-6 の μ̂ のまま。
- 検定が出ない指標（共通の期が 2 未満）は有意でないものとして扱う（(a) が出なければ据え置き、(b) が出なければ拒否権は発動しない）。
- 判定は計測道具が `prereg_decision` に機械的に出す（`sell_mu_source_bakeoff.sell_dedicated_decision`）。読み手は手で判定し直さない。
- 反復の上限に当たった α があっても判定は変えない（参考に数を書く）。
- 道具が非 0 で止まった（凍結値の不一致・共通域の行の欠け・実現リターンの食い違い・全社の再計算が M-4 と合わない）ときは判定しない。
- 採用でも、本番の切り替えは別の PR にする（ADR-0071 を覆す新しい ADR・producer への昇格・登録表）。

## 5. 手順

1. 日中キューに `measure:sell-quantile` を積む。計測道具は `--prereg` と `--prereg-sha`（追記節の凍結値）を必須にし、本文が凍結値と一致しなければ終了コード 2 で止まる。
2. 出力（`scripts/.cache/sell_dedicated_s2.json`）の主審の 2 本・判定・参考の値を、追記節の「結果」に書き写す。
3. 採用なら本番を切り替える別の Issue を起票する。据え置きなら結果を記録して #1158 を閉じる。

## 6. 凍結し直す条件

計測道具を一度も回していない間に限り、本文と凍結値を同じ PR で変えてよい（間引いた動作確認 `--smoke` は回したうちに数えない）。回した後に対象・量・判定を変えるなら、新しい ADR で覆す。

## 参考文献

- Koenker, R. & Bassett, G. (1978). "Regression Quantiles." *Econometrica*, 46(1), 33–50. https://doi.org/10.2307/1913643
- Beck, A. & Teboulle, M. (2009). "A Fast Iterative Shrinkage-Thresholding Algorithm for Linear Inverse Problems." *SIAM Journal on Imaging Sciences*, 2(1), 183–202. https://doi.org/10.1137/080716542
- O'Donoghue, B. & Candès, E. (2015). "Adaptive Restart for Accelerated Gradient Schemes." *Foundations of Computational Mathematics*, 15, 715–732. https://doi.org/10.1007/s10208-013-9150-3

<!-- prereg:append-below -->

## 追記節

凍結値（本文の sha256）: `91d540f1ca5ab31859049fe0fc52bb42c0bb7bd44e092fdf7ab4ad172b688ee7`

### 結果

未記入（日中枠の実走の後に書き写す）。
