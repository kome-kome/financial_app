# FRED ICE BofA 系列の履歴制限に対する非ICE信用スプレッド代替（M-1/M-4 既定の信用ファクター差替）

## Status

accepted（2026-07-24）。Issue #381 の設計決定。#379 の派生。

## Context

M-1（`macro_risk_return`・strict = `macro_nan_ok=False`）は「選択中の全マクロ特徴が同時に非None」の
行しか使わない（同一母集団の構造保証・ADR-0003）ため、学習可能期間が**最も収集開始の新しい系列**に
律速される。#379（低頻度マクロ変換バグ）修正後も、本番実測（2026-07-23）で信用スプレッド2系列
`HY_OAS`（FRED `BAMLH0A0HYM2`）/ `IG_OAS`（`BAMLC0A0CM`）**だけ**が 2023-06-26 開始（他の FRED 日次
系列 `T10Y2Y`/`BREAKEVEN10Y` は 2016-06、コモディティ8系列は 2020-07）で、M-1 strict のスナップショットが
**24ヶ月（2023-07〜2025-06）しか立たない**。`walk_forward_cv_monthly(min_train_months=6, step_months=3)`
に embargo=12（ADR-0014）を掛けると fold は `(24-6-12)/3 ≒ 2` 期しか取れず、honest OOF rank-IC が
ほぼ点推定（`ic_n=2`）になり、M-1/M-4 の評価も既定切替の判断も下せない（ADR-0015 の caveat）。

Issue #381 は当初「`FRED_MIN_YEARS_BACK=10` は既にコードにあるのでマクロ再収集を1回流すだけで
backfill される（要確認）」を第一改善案としていた。**これを検証した結果、原理的に不可能と判明した。**

### 真因の切り分け（2系統の独立した証拠）

1. **DB 実測**: `HY_OAS`/`IG_OAS` は同じ FRED 日次収集ループ・同一 `observation_start`（10年前）で
   取得しているにもかかわらず 2023-06 開始（803行）。**同ループの非ICE系列 `T10Y2Y`/`BREAKEVEN10Y` は
   2016-06 まで backfill 済み（2514行）**。同一パラメータで2系列だけ短い＝FRED 側がこの2系列の履歴を
   短くしか返していない。
2. **外部確認**: FRED は **2026-04 以降、ICE BofA 指数系列（`BAMLH0A0HYM2` 等を含む）をローリング3年窓に
   制限**し、2023年以前の履歴を配信しなくなった。完全系列（1996-12〜）は ICE Data Indices の資産で、
   ICE / Bloomberg（`H0A0`）/ Refinitiv 等の**商用ライセンス経由でのみ**取得可能。無料枠の当プロジェクト
   では入手できない。

結論: **ICE BofA 系列は再収集しても 2023-06 以前へ遡れない**。`FRED_MIN_YEARS_BACK=10` は非ICE系列
（Moody's / Treasury 由来）には効くが、ICE 系列の licensing 制限は上書きできない。

## Decision

**M-1/M-4 の既定の信用ファクターを、非ICE代替の信用スプレッド `BAA_SPREAD` へ移行する。**

1. **新収集系列 `BAA_SPREAD`（FRED `BAA10Y`）を `FRED_SERIES` に追加**（`collector_prices.py`）。
   `BAA10Y` は「Moody's Seasoned Baa Corporate Bond Yield − 10-Year Treasury Constant Maturity」＝
   最下位投資適格（Baa）社債の対国債クレジットスプレッド。**Moody's/Treasury 由来で ICE licensing の
   truncate を受けず、日次・1986〜取得可能**。信用サイクルの標準的な指標であり、`HY_OAS`/`IG_OAS` が
   担っていた信用リスク軸を経済的に代替する。変換は `zscore`（スプレッド水準・従来の HY/IG と同規約）。
2. **`macro_baa_spread_zscore` を `_MACRO_MAP` / `MACRO_FEATURE_OPTIONS` へ追加**（`macro_snapshots.py`）。
3. **`HY_OAS`/`IG_OAS` を `DEFAULT_MACRO_FEATURES` から除外**（`_STRICT_TRUNCATED_FEATURES`）。
   ただし **`MACRO_FEATURE_OPTIONS`（選択肢）としては残す**＝直近3年窓で使いたいユーザーは手動 ON 可能。
   収集も継続する（rolling 3年窓・小テーブルなので容量影響なし）。
4. **strict の同一母集団保証（ADR-0003）は変更しない**。`macro_nan_ok` オプションの M-1 への導入
   （#381 改善案 2b）は strict の設計思想（Ohlson 型の次元整合・同一母集団）を崩すため**採らない**。
   律速の解消は「律速していた truncate 系列を既定から外し、非truncate 代替を入れる」データ側の対処で
   達成し、モデルの母集団定義は不変に保つ。

順序制約（重要）: BAA を既定へ入れる前に**必ず本番収集で `BAA_SPREAD` を蓄積する**。strict は候補
マクロ特徴が1つでも全期間 None なら total_samples<20 で M-1 が全滅する（#379 と同型）ため、
「収集 → 既定配線」の順を守る（本 PR ではブランチ上で `collect-macro.yml` を先行実行して蓄積を実証
してから既定を変更した）。

## Consequences

- **strict の律速がコモディティ8系列（2020-07 開始・yoy で 2021-07 以降）まで緩む**。M-1 strict の
  snapshot 月数が 24ヶ月 → 約48ヶ月へ拡大し、honest OOF の fold が実用水準に増える。
  **実測（2026-07-24・`scripts.measure_embargo_impact`・honest embargo=12）**: M-1 の `n_periods` が
  **2 → 10**（`n_oof` 6,131 → 29,751・約5倍）、rank-IC=0.1982。M-4 も `n_periods` 9・rank-IC=0.1569 で
  共通域 M-1(0.1513)/M-2(0.1430) を上回り、ADR-0015 の「fold 2 期」caveat が統計的に意味を持つ水準
  （9〜10期）で解消した。
- **実証（2026-07-24・本番収集後）**: `BAA_SPREAD` は **2493行・2016-07-25〜2026-07-22**（`FRED_MIN_YEARS_BACK=10`
  の下限そのもの＝1986〜提供のうち我々の窓が律速）。同時再収集後も `HY_OAS`/`IG_OAS` は 2023-06 のまま
  ＝ICE truncate を実証。
- **信用スプレッドの粒度は落ちる**（HY 固有の劣後リスク・IG/HY の質スプレッドは既定から消える）が、
  pooled BIC 選択があるため既定を広げても最終モデルは自動的に絞られる。HY/IG が本当に効く局面は
  手動選択で復元できる。より粒度の高い非ICE代替（例 Baa−Aaa 質スプレッドの導出系列）が要れば
  別 Issue で追加する。
- **M-4**（`macro_ensemble`）の M-1 レグは M-1 の coerced `macro_features`（＝新既定）を継承するため、
  `_drop_dead_macro_features` に依存せず自然に BAA を使い HY/IG を落とす。ADR-0015 の「fold 2 期」caveat が
  解消される（同 ADR §追試を #381 後の値で更新）。
- **M-3**（`macro_dlm`・週次DLM・ADR-0012）は週次専用ファクターの別設計で本 ADR のスコープ外。

## Alternatives considered

- **改善案1（再収集で backfill）**: 上記の通り FRED ICE truncate により**不可能**。却下（原理的に達成不能）。
- **改善案2a（HY/IG を既定から外すのみ・代替なし）**: 律速は解けるが、信用スプレッド軸が既定から
  完全に消える。BAA 代替を入れれば経済的情報を保てるため、2a を包含する本決定（2a + 非ICE代替）を採用。
- **改善案2b（M-1 に `macro_nan_ok` を導入）**: strict の同一母集団保証（ADR-0003）を崩し、M-1 の
  Ohlson 型次元整合の前提に触れる。設計思想の変更コストが便益を上回るため却下。
- **商用ライセンスで ICE 完全系列を取得**: 無料枠の採用基準（VISION.md）に反する。却下。

## 参考

- ICE BofA US High Yield Index OAS（`BAMLH0A0HYM2`）/ US Corporate Index OAS（`BAMLC0A0CM`）: ICE Data Indices, LLC.
- Moody's Seasoned Baa Corporate Bond Yield Relative to 10-Year Treasury（`BAA10Y`）:
  https://fred.stlouisfed.org/series/BAA10Y （FRED 計算・日次・1986-01-02〜）
- 関連 ADR: 0002（M-1 per-stock macro-beta）, 0003（M-1/M-2 公平性・同一母集団）, 0013（コモディティ拡張）,
  0014（purge/embargo）, 0015（M-4 スタッキング・fold 2 期 caveat）。
- 関連 Issue: #379（低頻度マクロ変換 → M-1 学習0件）, #381（本 ADR）。
