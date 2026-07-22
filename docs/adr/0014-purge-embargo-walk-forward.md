# walk-forward CV に purge/embargo を導入し 52週先ラベルのリークを遮断する

## Status

accepted（2026-07-22）。Issue #363 の設計決定。

## Context

M-1（`macro_risk_return`）と M-2（`macro_gbdt`）の目的変数は「テスト月起点の 52週（≒12ヶ月）先
対数リターン」（`HORIZON_WEEKS = 52`・`build_snapshots`・`plugins/macro_snapshots.py`）。両者が共有する
walk-forward CV ヘルパ `walk_forward_cv_monthly`（`plugins/utils.py`）は、テスト月 `i` に対し
`train_yms = all_yms[:i]`（＝テスト月直前月まで全月）を学習に使っていた。

これは López de Prado (2018, Ch.7) の **purge が未実装**の状態である。テスト月 `i` のラベル窓
`[i, i+52週]` と、テスト直前の約12ヶ月分の学習サンプルのラベル窓 `[j, j+52週]`（`i-12 < j < i`）が
時間重複するため、学習ラベルにテスト期間の情報が混入する（前方リーク）。既存レビュー
`docs/reviews/2026-06-26-m2-macro-gbdt-review.md` の「リーク無し」評価は「テスト月データを学習に
入れない」観点のみを見ており、**ラベルの前方重複は見落としていた**。

本番 OOF rank-IC（M-2≈0.33 / M-1≈0.23）はこの分だけ上方バイアスがかかっている可能性があり、
VISION の核心である 3兄弟（M-1/M-2/M-3）並置比較の公平性を損なう。共有ヘルパのため M-1/M-2 が
同時に影響を受ける。M-3（`macro_dlm`）は本ヘルパを経由せず、週次 DLM の 1週先ラベルを 1-step-ahead
予測と比較する逐次フィルタで OOF を自前収集しており、ラベル窓が 1週のため構造的にこの重複を持たない。

## Decision

1. **`walk_forward_cv_monthly` に `embargo_months: int = 0`（既定0＝完全な後方互換）を追加**する。
   ループを **案B**（`range(min_train_months + embargo_months, len(all_yms), step_months)` かつ
   `train_yms = all_yms[:max(0, i - embargo_months)]`）とする。`embargo_months=0` のとき従来の
   `range(min_train_months, ...)` / `all_yms[:i]` と完全一致する。

2. **ラベル窓由来の定数 `LABEL_HORIZON_MONTHS = math.ceil(HORIZON_WEEKS*12/52) = 12`** を
   `plugins/macro_snapshots.py`（`HORIZON_WEEKS` の所在）に定義し、月換算の単一情報源とする。

3. **M-1 と M-2 の両方**に `embargo_months=LABEL_HORIZON_MONTHS` を対称適用する。M-1 だけ purge せず
   放置すると「M-1＝楽観バイアス込み・M-2＝honest」という**非対称比較**になり、CLAUDE.md の
   「比較ファミリーで1モデルだけ評価手段が欠ける非対称を残すな」（#272 の再発防止）に直接違反する
   ため、同一 PR で両方に適用することを必須とする。M-2 は XGBoost CV と OLS ベースライン CV の両
   呼び出しへ揃えて渡す（内蔵比較の fold 一致を保つ）。

4. **M-3 は月次 purge を適用しない**。週次 1週先ラベルはラベル窓が 1週で train/test の重複が構造的に
   生じないため。これは「論証された非適用」であり非対称の放置ではない（根拠を `macro_dlm.py` の
   OOF 収集ループのコメントと本 ADR に固定）。将来 M-3 に週次ギャップが必要になった場合は
   `walk_forward_cv_monthly` ではなく DLM の週インデックス基準で別途扱う。

5. **rank-IC は再測定で新しい honest 値になる**。過去に観測した 0.33/0.23 とは非連続で、案B により
   OOF 期間の先頭が `embargo_months` 分後ろ倒しされるため、厳密には別系列である。数値は下がりうるが
   「比較可能で正しい（honest）」値になることが本改修の本質。

## Considered Options

- **案A（ループ範囲を変えず train だけ削る）**: `train_yms = all_yms[:max(0, i - embargo_months)]`
  のみ。前半フォールド（`i < embargo_months + 学習下限`）が train 不足で黙ってスキップされ、
  `min_train_months` の指定意図（N ヶ月学習したら予測開始）が崩れる。→ 却下。
- **案B（採用）**: ループ開始を `min_train_months + embargo_months` へ後ろ倒し。embargo 後も必ず
  `min_train_months` 分の学習を確保し、`embargo_months=0` で現行と完全一致（既存テスト不変）。
- **M-2 のみ適用（M-1 は別 Issue）**: #272 型の非対称を一時的に再発させ、`model_comparison` の
  M-1 vs M-2 が不公平になる期間が生じる。→ 却下（同一 PR で両方）。
- **embargo_months をパラメータ契約（params_schema）に露出**: ユーザーが値を変えると比較が壊れる。
  ラベル窓から機械的に決まる定数のため内部固定とし、探索軸にも入れない。→ 却下。

## Consequences

- OOF 期間の先頭が `embargo_months`（=12）分短縮する。`model_comparison`（`POST /api/backtest/model-comparison`）
  の M-1/M-2 rank-IC は再測定で新しい値になり、過去値との直接比較は不可。
- 下流 `sell_ranking` の `mu_source` 選択判断（どの兄弟の µ̂ を使うか）が過大評価是正で健全化する。
- テスト合成データへの影響: M-1/M-2 の execute テストは 52週先ラベル差引後の有効月が
  `min_train_months(6) + embargo(12) = 18` を下回ると全フォールドがスキップされ OOF が空になる。
  `oof_backtest({})` はキーを返すため**キー存在チェックだけのテストは緑のままサイレントに空洞化する**。
  対策として M-1 `_build_mock_db`（120→210週）・M-2 `_make_db`（160→210週）の合成データを延伸し、
  OOF テストへ `rank_ic["n"] > 0`・`n_periods > 0` を追加した（M-3 が既に持つ非空検証と対称化）。
- M-3 のみ非適用の非対称は構造差として許容し、根拠を本 ADR と `macro_dlm.py` のコメントに固定した。

参考: López de Prado, M. (2018). *Advances in Financial Machine Learning*, Ch.7（Purged K-Fold CV & Embargo）,
Wiley. 関連 ADR: 0003（M-1/M-2 の同一母集団・同一fold・injectable fit_predict による公平性保証）,
0004（OOF 定義・無リーク原則）, 0007（#272 の非対称の教訓・目的関数統一）, 0012（M-3 週次専用）。
