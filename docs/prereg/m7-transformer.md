# 事前登録: M-7（時系列 Transformer）を M-6・M-2 と比べ、値動きの系列の上乗せを測る

- 事前登録の Issue: https://github.com/kome-kome/financial_app/issues/1316
- 計測道具: `python -m scripts.measure_transformer`（日中枠 `measure:m7-transformer`）。道具は #1317 の PR3 で足す。
- **凍結**: この文書は main へのマージで凍結する。凍結値は、マーカー行より上（本文）を改行 LF・UTF-8 にそろえた sha256（`scripts/prereg.py`）。
- M-7 のプラグイン（`plugins/macro_transformer.py`）と同じ PR で凍結する＝M-7 が実行できるようになった時点で、判定の規則は決まっている。
- 実走は日中キューで単独に回す。積むのは計画を承認したセッション（#1316 の親）。

## 1. 問い

朝の買い候補を出す M-6（ElasticNet）と M-2（XGBoost）は、どちらも「1 社×1 月末」の断面を 1 行として見るだけで、各社の過去の値動きの**並び**は読んでいない。M-7 は直近 52 週の値動きと出来高の並びを Transformer で読み、M-2/M-6 と同じ財務の列と合わせて 52 週先の対数リターンを当てる。

1. M-7 は M-6・M-2（スキーマ既定の設定・ADR-0070）より、52 週先リターンの**月内の順位**をよく当てるか。
2. M-7 の値動きの系列は、同じ手順で財務の列だけを学習した版（系列なし）に上乗せするか。

見立ては「差なし」が最有力。株価由来の特徴（モメンタム）は共通域で効かなかった（ADR-0045）。木の M-2 も線形の M-6 を超えていない（MODELS.md §16）。期は 18〜19 しかない。だから測る前に規則を固定する。

## 2. 対象

- **M-7**: `macro_transformer` をスキーマ既定のパラメータで回す。凍結する値は次のとおり。
  - パラメータ: `fin_features` = `M2_M6_DEFAULT_FIN_FEATURES`・`use_sequence` = 真・`d_model` = 32・`n_layers` = 2・`dropout` = 0.1・`learning_rate` = 0.001・`max_epochs` = 30・`patience` = 5
  - 学習の定数（`plugins/seq_transformer.py`）: head 4・batch 512・weight decay 1e-4・AdamW（β1 0.9・β2 0.999・ε 1e-8）・global norm の clip 1.0・early stopping の検証は末尾の月の塊で行の約 20%・学習との空白は最大 12 か月（学習側に最低 3 か月残す）・組めないときの固定回数 10・答えは fold 内で p1-p99 winsorize → 学習月ごとに平均を引く → z・損失は二乗誤差・決めた回数で学習期間の全体を学習し直してから予想・最終モデルの回数は fold の中央値
  - 系列の定数（`plugins/weekly_sequences.py`）: 窓 52 週・パッチ 4 週（13 トークン＋CLS）・チャネル 3（週内横断 z の週次リターン・自社の直前 52 週（観測 8 以上）に対する出来高の外れを週内横断 z・観測の印）・週内の有効社 30 未満の週は観測なし・横断 z の上下限 ±5・上場 52 週未満は 0 埋めで行を落とさない
  - 種: `_RANDOM_STATE` = 42（`tuning.seeded`）。fold の種は test 月から作る
- **対照（系列なし）**: M-7 を `use_sequence` = 偽で回す（ほかは M-7 と同じ）。
- **M-6・M-2**: スキーマ既定（`model_comparison.run_comparison` が `{}` で回す）。
- **両者で同じもの**: パネル（M-7 の `build_snapshots` の引数は M-6 のスキーマ既定から読む）、外側の walk-forward（最低学習 6 か月・3 か月刻み・embargo 12 か月）、比べる test 期。
- **確かめること**: 3 モデルと対照の OOF の期数（`n_periods`）と行数（`n_oof_samples`）が一致しなければ、道具は終了コード 3 で止まる。間引きはしない。

## 3. 量

- **主審（2 組）**: rank-IC の M-7 対 M-6 と、M-7 対 M-2。
  - `model_comparison.run_comparison(only_models=["macro_gbdt", "macro_enet", "macro_transformer"])` の `significance_matrix` をそのまま読む。中身は共通 test 期の期別 rank-IC の差の対比較で、定常ブートストラップを使い、両側で検定する。
  - 道具は 3 組（M-2|M-6・M-2|M-7・M-6|M-7）を数えるので、Bonferroni で α = 0.05 / 3 ≈ 0.0167。M-2 対 M-6 の組は数えるが判定には使わない。
  - 再計算しない（ADR-0041・ADR-0044 決定 5）。
- **対照（別の家族・1 検定）**: M-7 と系列なしの rank-IC の差。`model_stats.paired_ic_significance` を両側、α = 0.05 で使う。
- **参考（検定しない・判定に使わない）**:
  - 上位 10 社超過（`top_k_excess_return_by_period["10"]`）の M-7 − M-6 と M-7 − M-2 の差（ADR-0069 で買いの拒否権の K は未成立）
  - 売り側spread・業種中立 rank-IC・ターンオーバー・損益分岐 bp
  - fold ごとの学習回数・early stopping を組めた fold の数・系列の観測の薄さ（観測の無い行の数）
  - 所要
  - 種 43・44 で回した M-7 の rank-IC の平均（種 42 との幅を見る。判定は種 42 だけで行う）

## 4. 判定

M-6|M-7 の組の差は M-6 − M-7、M-2|M-7 の組の差は M-2 − M-7 で記録される。下の「勝つ」「負ける」は M-7 から見た向き。

- **昇格を検討する Issue を立てる**: M-7 が M-6 に有意に勝ち、**かつ** M-2 に有意に負けない。
  - 対照で上乗せが有意でなければ、その Issue に「利得は値動きの系列から来ていない」と書く。
- **退役**: M-7 が M-6 にも M-2 にも有意に負ける。退役の手順は ADR-0044 に従う（`hidden=True`・`HEAVY_AUTOMATION` は exempt のまま・guide の退役節へ移す）。
- **比較用で据え置き**: それ以外。
- 検定が出ない組（共通の期が 2 未満）は、有意でないものとして扱う。
- 判定は計測道具が `prereg_decision` に機械的に出す。読み手は手で判定し直さない。
- 種や設定を変えて測り直し、判定を覆すことはしない。
- 道具が非 0 で止まったときは判定しない（凍結値の不一致・期数や行数の食い違い・いずれかのモデルの失敗）。
- 昇格を検討するとなっても、朝の買い候補への追加は別の PR にする（新しい ADR・`PRODUCTION_MODELS`・`CAND_MODELS`・暦）。

## 5. 手順

1. #1317 の PR1〜3 がすべてマージされたことを確かめる。それまでも計測までも、実データで M-7 を回さない（画面の実行・`model:macro_transformer` を含む）。
2. 日中キューに `measure:m7-transformer` を単独で積む。道具は `--prereg` と `--prereg-sha`（追記節の凍結値）を必須にし、本文が凍結値と一致しなければ終了コード 2 で止まる。
3. 出力の主審の 2 組・対照・判定・参考の値を、追記節の「結果」へ書き写す。
4. 判定どおりの Issue を立てる（昇格の検討・退役・据え置き）。据え置き・昇格なら、月 1 回の暦の Issue も立てる。

## 6. 凍結し直す条件

実データで M-7 を一度も回していない間に限り、本文と凍結値を同じ PR で変えてよい。画面からの実行と `model:macro_transformer` も「回した」に数える（合成データのテストは数えない）。回した後に対象・量・判定を変えるなら、新しい ADR で覆す。

## 参考文献

- Vaswani, A. et al. (2017). "Attention Is All You Need." *Advances in Neural Information Processing Systems 30*. https://arxiv.org/abs/1706.03762
- Nie, Y. et al. (2023). "A Time Series is Worth 64 Words: Long-term Forecasting with Transformers." *ICLR 2023*. https://arxiv.org/abs/2211.14730
- Loshchilov, I. & Hutter, F. (2019). "Decoupled Weight Decay Regularization." *ICLR 2019*. https://arxiv.org/abs/1711.05101
- Gu, S., Kelly, B. & Xiu, D. (2020). "Empirical Asset Pricing via Machine Learning." *The Review of Financial Studies*, 33(5), 2223–2273. https://doi.org/10.1093/rfs/hhaa009

<!-- prereg:append-below -->

## 追記節

凍結値（本文の sha256）: `66585b6dbaf653242995e0c79b19fb136d4b8ae981cb49538eca2b3ce831a421`

### 結果

（計測の後に書き写す）
