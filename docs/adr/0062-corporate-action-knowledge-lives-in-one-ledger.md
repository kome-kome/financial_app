# 企業イベントの知識は台帳 1 つに置く（本番は測定用スクリプトに依存しない）

## Status

accepted（2026-09-26）。Issue [#746](https://github.com/kome-kome/financial_app/issues/746)、
散っていたことで生じた穴は [#739](https://github.com/kome-kome/financial_app/issues/739)（保留の窓が TTM で無視される）と
[#740](https://github.com/kome-kome/financial_app/issues/740)（スピンオフが F に入らない）。
[ADR-0055](0055-valuation-basis-mismatch-is-corrected-in-the-view.md) の決定2（向きの唯一の源）と決定3（検出器の再利用）の
**置き場所の記述だけ**を置き換える。「コピーしない」「F は VIEW で毎晩全置換」「検出0件なら表に触らず失敗」は変えない。
[ADR-0053](0053-one-scale-per-price-column.md) の登録表（スピンオフ・保留の窓）の置き場も同じく移す（中身と使い方は変えない）。

## Context

「どの社のどの日に、株数や株価の基準を変える企業イベントがあったか」は1つの知識だが、
2026-09-26 の時点で約8つの module に散っていた。

| 役 | 置き場所（変更前） |
|---|---|
| 検出器の純関数・既定値・向き（`COLUMN_DIRECTION`） | `scripts/measure_split_valuation_bias.py`（測定用スクリプト） |
| 入口（入力の読み込み→検出→累積 F） | `collector_prices.compute_split_adjustments` / `rebuild_split_adjustment_factors` |
| 検出器の入力の形をしたローダ3本 | `database.py` |
| TTM の分割窓（検出・倍率待ち・公式の3種を寄せる） | `ttm_composite.split_windows` / `ttm_split_factor` |
| 登録表（スピンオフ・保留の窓・`AdjFactor` のイベント判定） | `collector_utils` |

ADR-0055 決定3 が選んだのは「検出器を**コピーしない**」ことで、測定用スクリプトの中に置き続けること自体は
目的ではなかった。しかし置き場所の帰結として次が起きていた。

1. **本番が測定用スクリプトに依存していた。** `collector_prices` と `ttm_composite` が
   `scripts.measure_split_valuation_bias` を遅延 import し、測定器は修復スクリプト経由で `collector_prices` を
   import し返す循環があった。
2. **検出が一晩に2回走っていた。** 係数表の洗い替えと TTM 合成がそれぞれ `compute_split_adjustments` を呼び、
   TTM はその結果に**別途読み直した**公式イベントを足していた。
3. **読み手ごとに入力を組み立てていたので、登録表を通る読み手と通らない読み手ができた。**
   - #652 で登録した保留の窓（E34165 の新株予約権無償割当）を読むのは修復スクリプトだけで、TTM の分割窓は
     公式イベントを素通しで使う。J-Quants の catchup が 2026-09-11 のイベントに届く 2026-12 上旬以降、
     同社の過去の TTM 行すべてに F=1.5 が付く（実データへの注入で確認・#739）。
   - スピンオフの登録表は価格側（catchup・修復）しか読まず、E02086 は株価だけがスピンオフ分を遡及調整された
     まま F の行を1つも持たない（#740）。

どちらもエラーを出さない（どの値も妥当な株価指標）。登録表を1つ足すたびに「どの読み手が読むべきか」を
人が思い出す構造である限り、同じ形の穴は増え続ける。

## Decision

### 1. 企業イベントの知識は `corporate_actions.py`（企業イベント台帳）に集める

登録表 → 検出器（純関数）→ 台帳（純関数）→ I/O の順に1ファイルへ置く。

- 登録表: `SPINOFF_ADJUSTMENTS` / `spinoff_factor` / `before_spinoff_ex_date`、
  `WITHHELD_OFFICIAL_ADJUSTMENTS` / `withheld_official_events`、`ADJ_FACTOR_EVENT_EPS` / `adj_factor_event`
- 検出器: `detect_events` / `cumulative_factors` と既定値・定番比・`COLUMN_DIRECTION` ほか
  （**向きの唯一の源は `corporate_actions.COLUMN_DIRECTION`**＝ADR-0055 決定2 の置き場所を置き換える）
- 台帳: `SplitWindow` / `split_windows` / `factor_after`（旧 `ttm_split_factor`）/ `Ledger` / `compute_ledger`
- I/O: ローダ3本 / `build_ledger` / `rebuild_split_adjustment_factors`

価格の段差検出（`repair_price_scale_breaks` 系）と J-Quants の通信部分は入れない。前者は「1つの価格列の
スケール」（ADR-0053）の話で企業イベントの知識ではなく、後者は候補 I（J-Quants クライアントの一本化）で扱う。

### 2. 呼び出し側は入力を組み立てない

F や分割窓を得る道は2つだけにする。

- 本番: `build_ledger(db)`——入力（通期行・公式 `AdjFactor`・その受信区間・週次株価の系列）を台帳が自分で読む
- テスト: `compute_ledger(rows, official=..., coverage=..., series=...)`——DB に触らない。入力は**すべて
  キーワードで必須**（1つ渡し忘れても検出はもっともらしい結果を返すので、渡し忘れを型で止める）

**公式イベントは台帳が1回だけ読み、検出器と TTM の窓の両方に同じ値を使う**（`Ledger.official`）。
公式イベントの見え方を変えるとき（#739 の保留の窓）は `compute_ledger` の1か所を変えれば両方に効く。
これをテストが縛る（検出器へ渡した公式イベントと TTM の official 窓が同じ集合であること）。

### 3. パイプラインは台帳を一晩に1回だけ作り、係数表と TTM へ同じものを渡す

`_pipeline_incremental.py` / `_pipeline_gh.py` は `ledger = build_ledger(db)` を作り、
`rebuild_split_adjustment_factors(db, ledger=ledger)` と `rebuild_ttm_financial_records(db, ledger=ledger)` へ渡す。
新しい表は足さない（ミラーの `SYNC_PLAN`・バックアップの範囲は不変）。ADR-0055 決定5（係数の洗い替えは
収集パイプラインの Phase 4 の直後・例外は握らない）と #580（TTM の失敗だけ握って後続の自己検証を続ける）は
そのまま保つ。`ledger` と `bps_path` を同時に渡すと `ValueError`（台帳は作った時点の `bps_path` で固まっている）。

### 4. 本番は `scripts/` を import しない

`scripts/measure_split_valuation_bias.py` は台帳を import する測定 CLI（突合・レポート）だけになる。
ADR-0055 決定3 の「検出器をコピーしない」は、import の向きを逆にして守る。台帳の I/O より上が
database / collector_prices / httpx / scripts をトップで import しないこと、本番の module
（`corporate_actions` / `collector_prices` / `ttm_composite` / `database`）が `scripts` を import しないことを
テストで固定する。

### 5. この変更では挙動を変えない

#739・#740 の是正は台帳の上で別に行う。移動の前後で、実データから作った `factors`・`events`・`stats`・
係数表へ書く行・TTM の分割窓・`build_ttm_rows` の出力が**完全一致**することを確かめた（2026-09-26・
イベント 668 件・F の行 2,748・TTM 17,247 行。DB 呼び出しは公式イベントの二重読みが消えて 11→10 回）。

## Consequences

- 登録表を足すときに「どの読み手が読むべきか」を思い出す必要が無くなる。台帳の中で決めれば、係数表・TTM・
  修復・測定のすべてに同じ見え方で届く。
- 検出は一晩1回になる（TTM 合成が検出を走らせ直さない。所要の差は測っていない）。
- 本番と scripts の循環が消え、`collector_prices`（4,219 行）から F の洗い替えの約160行が抜ける。
- `collector_utils` から登録表が抜けた。**再エクスポートは置かない**（素通しの module を増やさない）ので、
  import 元はすべて `corporate_actions` へ書き換えた。
- 検出器のテストは `tests/test_corporate_actions.py`、測定専用の関数のテストは
  `tests/test_measure_split_valuation_bias.py` に分かれた。
- ADR-0055 の本文にある `scripts/measure_split_valuation_bias.py::COLUMN_DIRECTION` 等のパスは当時の記録として
  残し、Status に本 ADR への参照を足した（ADR は書き換えない運用）。

## Considered Options

### 案A: 検出したイベントを表へ永続化し、TTM や測定はその表を読む

「なぜ補正したか」を SQL で追える利点はあるが、表が1つ増え（`init_db`・ミラー・バックアップの対象）、
イベントの表と係数表の世代がずれる経路が新たに生まれる。TTM と係数表は同じパイプラインの隣り合う工程なので、
メモリ上の台帳を渡せば世代は構造的に揃う。**不採用。**

### 案B: 検出器は `scripts/` に残し、台帳 module は遅延 import で包むだけにする

差分は小さいが、本番が測定用スクリプトに依存する形と循環が残る。台帳の interface の後ろに測定ツールが
隠れるだけで、locality は上がらない。**不採用。**

### 案C: 評価側（F の検出・洗い替え・TTM の窓）だけを台帳にし、登録表は `collector_utils` に残す

差分は小さいが、#739・#740 はどちらも**登録表を読む読み手と読まない読み手**の食い違いから生じている。
登録表が価格側に残る限り、新しい種類の登録表を足したときに台帳側が読み忘れる余地が残る。**不採用。**
