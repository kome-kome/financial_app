# 売り候補ランキングは実測で最良の 1 手法（M-6 の μ̂ の下位 1/5）だけを残す

## Status

**accepted（2026-11-05）**。利用者の依頼「一番期待値の高い手法だけを残し、それ以外は候補としても出てこない設計に」
（/ux-critique の場で合意）。[ADR-0022](0022-short-side-oof-metric-and-default-mu-source.md)（売り側spread と既定の出所 M-6）・
[ADR-0069](0069-evaluation-metrics-have-roles.md)（上位層の売り側spread・#1073 の −R_macro の除外）の実測をそのまま使い、
新しい計測はしていない。

## Context

売り候補ランキング（`plugins/sell_ranking.py`）の画面には、売りの決め方が何通りも並んでいた。

| つまみ | 選択肢 | 売りとしての実測 |
|---|---|---|
| プリセット | マクロ予測型（μ̂ だけ・既定）・バランス型・割高警戒型・業績悪化重視 | μ̂ だけ: 全社の売り側spread +0.1097（M-6）。財務の 3 つ: **なし**（OOF の過去の期の財務を作り直せないので測れない・#1073） |
| 観点ウェイト | 9 観点のスライダー | なし |
| μ の出所 | M-1・M-3・M-6（既定） | M-6 +0.1097 > M-1 +0.0968（p=0.10）。M-3 は共通域に入らず測っていない |
| 札 | 売りスコアの絶対閾値 0.8 / 0.3 で SELL / REDUCE / HOLD | なし |
| タイミング補正 | 価格トレンドで札を 1 段ずらす（既定 ON） | なし |
| R3 足切り | μ̂ の確実性で SELL を REDUCE へ（既定 OFF） | なし |
| バックテストの `sell` | recommend の合成スコアの符号反転 | 売り側の指標では測っていない（買いの合成スコアの逆） |

実測で効いていたのは「1 つ前の期に買いの並びの上位にいた社（＝保有に近い母集団）の中で、M-6 の μ̂ の下位 1/5 を
売る」形だけで、層の平均を下回る社を選べていた（期平均 +1.5〜+6.1%・正の期 71〜94%・17 期・#1043）。−R_macro を
足した版は未来情報込みの上限でも有意に悪化した（#1073）。残りの選択肢は「測って負けた」か「測られていない」で、
画面の上では実測で勝った手法と同じ重さで並んでいた。

## Considered Options

1. **今のまま（既定を最良にして他は残す）**——却下。利用者が測られていない手法を選べる。選んでも画面は何も警告しない
2. **他の手法を「詳細」に畳む**——却下。依頼は「候補としても出てこない」
3. **他の手法を API には残し画面からだけ消す**——却下。二重管理（画面と schema）が残り、API からは選べる
4. **札を今の絶対閾値で固定して残す**——却下。閾値は未測定。測った運用は「保有の中の下位 1/5」
5. **手法を 1 つにし、札も測った切り方にそろえる**——採用

## Decision

1. `sell_ranking` のパラメータは `holdings` だけ。並びは M-6（`MU_SOURCE="macro_enet"`）の現在μ̂ の昇順、μ̂ が
   そろった m 社の先頭 `m // 5` 社が `SELL`、残りが `HOLD`、μ̂ 無しは「データ不足」で末尾
2. 下位の切り方は計測（`in_layer_short_side`・昇順の先頭 m // n_quantiles）と同じにする。**m < 5 では売りなし**。
   少ない保有に合わせて 1 社へ切り上げる規則は測っていないので足さない
3. `depends_on` を `sector_ols` から `macro_enet` へ替える（割高度は使わない。μ̂ が無ければ判定できない）
4. 外す: `PRESETS`・`SELL_METRICS`・観点ウェイト・`mu_source`・絶対閾値と `REDUCE`・タイミング補正・R3 足切り・
   バックテストの `source=sell`。価格トレンドと損益は参考表示として残す（並びにも札にも使わない）
5. 旧式の売りスコア（`sell_score`）は上位層の売り側spread の上限版の計測が使うので
   `scripts/sell_mu_source_bakeoff.py` へ移す（被覆率の下限は当時の既定 0.4 を `UPPER_MIN_COVERAGE` に固定）
6. 画面に使い方（何をするか・何社売るか・売った後に何をするか・手法が 1 つの理由）を書く

## Consequences

- 売りの決め方を選ぶ場面が無くなる。μ̂ の出所や切り方を変えるときは、`scripts.sell_mu_source_bakeoff` で測ってから
  新しい ADR で変える（`MU_SOURCE`・`SELL_QUANTILES` は 1 箇所）
- 保有が 5 銘柄未満の利用者には売却札が出ない。画面と解説ページに明記する
- M-6 が一度も走っていない環境では 400 になる（以前は全保有が「データ不足」）。M-6 は夜間バッチで毎晩更新される
- 財務指標の売りの観点（割高度・業績悪化・ネットキャッシュ毀損）は、売りとして測る方法ができるまで画面に出ない。
  測る方法ができたら、上位層の売り側spread で M-6 の μ̂ と比べ、勝ったときだけ戻す
- バックテスト画面の検証手法は買いの 3 つだけになる

## 関連

- [ADR-0004](0004-m2-downstream-sell-and-oof-backtest.md)（producer → sell_ranking）・[ADR-0022](0022-short-side-oof-metric-and-default-mu-source.md)・
  [ADR-0069](0069-evaluation-metrics-have-roles.md)・[ADR-0044](0044-retire-underperforming-models-by-hiding.md)（退役は非表示。本 ADR は手法の削除で、モデルは消さない）
- 実装: `plugins/sell_ranking.py`・`static/js/analysis.js`・`templates/analysis.html`・`backtest.py`。解説: MODELS.md §10・`templates/models.html#m10`・`templates/guide.html#sell_ranking`
