# price_predictor を削除（リターン予測役を比較ファミリー M-1〜M-3 に集約）

## Status

accepted（2026-06-27・grill-with-docs/Opus で確定）。分析プラグイン整理の一環。

## Context

「分析プラグインが増えすぎた」ため、プロジェクト目的に照らして不要なモデルを整理する。③ 将来リターンを予測の層に4モデルが密集していた——`price_predictor`（最古・OLS・テクニカル価格特徴量[MA乖離/vol/RSI/ATR]＋財務比率→N日先[5/20/60日]対数リターン・マクロ非依存）と、[[比較ファミリー]] M-1（`macro_risk_return`・線形）／M-2（`macro_gbdt`・非線形）／M-3（`macro_dlm`・時変β）。

削除基準を1つに絞った：**(A) 役割が重複しかつ劣後するものだけを削除**する。VISION の「手法を増やして比較・改善し続ける」価値ゆえ、同一目的変数・同一 walk-forward fold で線形 vs 非線形 vs 時変を対比する比較ファミリーは**保護対象**とし、「多いから減らす」漠然基準（(B)ビジョン適合・(C)保守コスト）は今回採らない。

全層に (A) を当てた結果、削除に該当するのは厳密に `price_predictor` 1本のみ（①recommend/net_cash＝方法が別物かつ net_cash は sell_ranking が参照、②sector_ols/gap_analysis＝producer/consumer、⑤sell_ranking＝双対の単独実体）。

## Decision

1. **`price_predictor` を削除する。** その中核（線形 OLS で財務特徴量→リターン予測）は **M-1 が上位互換で吸収済み**（M-1 も線形だが、マクロ＋リスク-リターン幾何＋per-stock β を加えた richer 版）。`price_predictor` を独自たらしめる差分はテクニカル価格特徴量（RSI/ATR/MA）と短期ホライズン（5–60日）のみで、これはファンダ＋マクロのプラットフォームにおいて「おまけ」＝ミッション外と確認した（ユーザー判断）。よって役割は M-1 と重複し、固有差分はプロジェクトが欲しない領域＝劣後と判定。

2. **比較ファミリー（M-1/M-2/M-3）は保護**。手法軸（線形/非線形/時変）が異なり対比較価値を持つため、(A) の「役割重複」には当たらない。

3. **保持原則を確立**：今後モデル比較を重ね、ファミリーのメンバーが[[メタ検証]]で継続的に劣後と判明した場合は、削除するか[[対照モデル]]（control model・推奨/売り経路から外し新モデルのベースラインとして残す）へ降格する。`price_predictor` はファミリー外で手法軸の対比価値も無く（線形は M-1 と重複）、対照ベースラインとしての固有価値も無いため、降格でなく削除とした。

## Considered Options

- **`price_predictor` を独自baselineとして残す**（却下）：短期ホライズン・テクニカル特徴量は確かに他モデルに無いが、それが唯一の独自性でありミッション外。ファンダ由来のリターン予測役は M-1 に完全吸収されており、残すと「役割重複の劣化版」を抱え続ける。
- **対照モデルへ降格して残す**（却下）：対照ベースラインの価値は「異なる手法軸での対比」にあるが、`price_predictor` の線形 OLS は M-1 と手法軸が重なる。比較対照としての固有価値が無いため降格の意味が薄い。
- **基準を (B)/(C) へ広げて複数削除**（却下）：比較ファミリー保護と矛盾し、VISION の「手法の多さ＝価値」を損なう。今回は (A) 厳密限定。

## Consequences

- **削除サーフェス**（コード＋ドキュメント＋UI＋テスト）：
  - `plugins/price_predictor.py`（本体）、`tests/test_price_predictor.py`、`tests/README.md` の言及
  - `docs/MODELS.md` §4 は **tombstone 化**（§1「総合リターン予測→§3統合」の先例に倣い、番号振り直し・アンカー破壊を回避）。目次と §4 本文を「削除・M-1 へ集約」の短い案内に置換
  - `docs/ARCHITECTURE.md`、`templates/models.html`、`templates/guide.html`、`static/js/analysis.js` の price_predictor 関連記述/要素を除去
  - `CONTEXT.md` メタ検証項の例示（"price_predictor / macro は WF-CV を内蔵"）から price_predictor を除去
- **VIEW は変更しない**：`sql/financial_metrics_view.sql` の price_predictor 参照は**コメントのみ**（null 伝播の説明）。`rd_intensity`/`da_intensity` 列は汎用なので残し、コメントを一般化する。
- **歴史的記録は改変しない**：`docs/adr/0001-*`、`docs/archive/IMPROVEMENTS.md` の言及は当時の決定の記録ゆえそのまま。
- **本セッションで先行実施済み**：CONTEXT.md へ M-3 の項を追加（文書化ギャップ解消）し、[[比較ファミリー]]／[[対照モデル]]を用語登録。
- **整合点検**：削除実行後に `/tidy` で参照の残骸・壊れリンク・doc⇔code 乖離を点検する。
