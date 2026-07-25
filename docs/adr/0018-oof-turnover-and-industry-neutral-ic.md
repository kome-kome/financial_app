# OOF バックテストの現実性強化：業種中立rank-IC＋ネットターンオーバーコスト

## Status

accepted（2026-07-25）。Issue #368 の設計決定。ADR-0004（OOF バックテスト本体）の続編。

## Context

`oof_backtest`（`plugins/macro_snapshots.py`・ADR-0004）は 3〜5 兄弟モデル（M-1〜M-5）の無リーク
walk-forward 残差から rank-IC・分位リターン・ロングショート spread・hit-rate を算出し、
`model_comparison` が横並び比較する。ここに2つの盲点があった。

1. **ターンオーバー未考慮**: `long_short_spread_net`（#316）は「毎期100%回転」固定の往復コスト控除
   で、docstring 自身が「頻度差を呼び出し側で考慮せよ」と注意していたが、考慮する側が存在しなかった。
   結果、安定・低回転の M-1 と週次高回転の M-3 が同一 bp で控除され、ネット比較が歪む。
2. **セクター傾斜の混入**: 生 rank-IC は「素材>ハイテクを WTI で一括に並べる」ようなセクターベット
   だけで高 IC を稼げ、業種内の真の銘柄選択力と区別できない。

どちらも residuals タプル `(yhat, y_true)` に銘柄ID／業種が載っておらず、当時は算出不能だった。
一方 `build_snapshots(return_stock_ids=True)` は既存で、`sample_meta_by_ym[ym][j].industry` も
既構築。residuals はサンプル順を保存する（`_compute_r3_buckets`・`macro_ensemble._align` が依拠する
既存契約）ため、index で 1:1 突合できる。

## Decision

1. **`oof_backtest` に任意引数 `meta_by_ym`（＋ `rebalance_per_year`）を足す**。`meta_by_ym` は残差と
   同順の `{test_ym: [(stock_id, industry), ...]}`。`build_oof_meta(stock_ids_by_ym, sample_meta_by_ym,
   yms)` が build_snapshots 出力から組む。**渡したときのみ**新指標を算出し、**無印キーは完全に不変**
   （`meta_by_ym=None` で従来と bit 一致＝後方互換）。追加学習・追加 Egress ゼロ・stdlib のみ。

2. **業種中立 rank-IC**（`rank_ic_industry_neutral`）: 各期・業種内で yhat/y_true をそれぞれ平均順位化
   →業種平均順位を引く（順位デミーン）→全業種プールして Spearman → サンプル数加重平均。業種ベット
   （セクター傾斜）で稼いだ IC を除去し業種内の銘柄選択力だけを測る。`industry=None` の行・単独業種
   （デミーンで消える）は除外。

3. **実効ターンオーバー＋ブレークイーブンbps**: 隣接期の top/bottom 分位メンバー（stock_id）の
   Jaccard 非重複（入替割合 0..1）を top/bottom 平均→期間平均＝`effective_turnover`。
   `breakeven_cost_bps = gross·50/turnover`＝ロングショート spread が消える片道コスト水準（既存
   `cost_bps` と同一規約 net = gross − (cost_bps/100)·2·turnover = 0 の解）。`gross` も `turnover`
   もリバランス頻度に比例するため **breakeven は比で頻度不変**＝モデル横断で直接比較できる単一
   スカラー。低回転な安定モデルほど大きい（コスト耐性が強い）。併せて `long_short_spread_net_turnover`
   （実効回転で控除したネット）と `annual_turnover`（＝回転×`rebalance_per_year`・参考値）を返す。

4. **全兄弟へ配線**: M-1/M-2 は `build_snapshots(return_stock_ids=True)` へ切替＋`build_oof_meta`
   （M-5 は M-2 の `execute` を継承するため自動）、M-3 は per-stock 残差収集ループで `(ec, industry)`
   を並行蓄積、M-4 は共通 OOF `common[ym]`（ec 先頭）から業種を lookup。`model_comparison` は各
   モデルの `oof_backtest` dict をそのまま集約するため透過し、`/analysis` 比較ビューが「生IC ／
   業種中立IC」「ブレークイーブンbps ／ 実効ターンオーバー」を並置する。

## Considered Options

- **residuals タプル自体を `(yhat, y_true, stock_id, industry)` へ拡張**（却下）: `walk_forward_cv_monthly`
  は汎用 CV 関数でメタを持たない。メタ添付を関数へ押し込むと汎用性が崩れ、既存の全呼び出し側
  （scripts・tests・M-4 の base_oof）が 4-tuple 対応を迫られる。呼び出し側が同順のメタを別引数で
  渡す方式なら CV 関数は不変・後方互換が保てる。
- **業種中立を「全断面ランク→業種デミーン→Pearson」で実装**（不採用）: 業種内ランクの方が
  業種サイズ差の影響を受けにくく、`_avg_ranks`／`_spearman` の既存ヘルパを再利用できる。両者は
  検証ケースで一致した（決定的テスト参照）。
- **ターンオーバーを年率絶対量で比較**（却下）: 頻度の異なる M-1(四半期)/M-3(週次) を絶対回転で
  並べると高頻度モデルが不当に不利。ブレークイーブンbps は頻度が比で相殺され公平。

## Consequences

- **M-3 のターンオーバーは近似**: M-3 は週次残差を月へ束ねるため1銘柄が同月に複数行入りうる。分位
  メンバーは stock_id 集合で dedup するため Jaccard は算出可能だが厳密なポートフォリオ回転ではない
  （directionally「週次＝高回転」を示す）。コード内コメントに明記。
- **`build_snapshots` 呼び出しの戻り値変更**: M-1/M-2 が 4-tuple→5-tuple（`stock_ids_by_ym` 追加）。
  `build_snapshots` を patch する既存テストのモックを 5-tuple へ更新（`tests/test_macro_risk_return.py`）。
- **UI 拡張**: `static/js/analysis.js` の `_mcModelCard` に2タイル追加（CSP 準拠・inline ハンドラなし）。
- **将来**: ADR-0004「将来エンハンス」の「sector-neutral 分位」「OOF の信頼区間」の前者を本 ADR が
  実装（後者は #369 の定常ブートストラップで別途対応済み）。

参考: Grinold & Kahn "Active Portfolio Management" 2nd ed.（turnover-adjusted performance・業種中立化）。
