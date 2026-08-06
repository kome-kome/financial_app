# コモディティ価格チャネルを8系列に拡張する

## Status

accepted（2026-07-20）。Issue #358 の設計決定。

## Context

現行のコモディティ・チャネルは `WTI原油`・`金` の2系列のみ（`MACRO_SERIES`・`collector_prices.py`）。ADR-0002 が定めたマクロ増量方針「count ではなく直交する経済チャネル（金利・FX・株式β・クレジット・**コモディティ**・ボラ・インフレ）で 8-12」に照らすと、コモディティ枠が手薄で、日本株の業種別コモディティ感応度が捕捉できていない:

- 銅（Dr. Copper）→ 非鉄金属・電線・機械（景気循環の代表的センサー）
- 天然ガス → 電力ガス・化学（燃料/原料コスト）
- 貴金属（銀・プラチナ）→ 商社・自動車触媒・電子材料
- 穀物（小麦・トウモロコシ・大豆）→ 食品・飼料（原材料コスト）

ユーザー要望は「銘柄によって特定コモディティ価格との関係があるものがあり、それを取り込んで予測精度を上げたい」。これは M-2（XGBoost が財務×コモディティの非線形交互作用を自動学習・SHAP で銘柄別寄与を可視化）と M-3（銘柄別の時変マクロβを直接推定）が最も直接的に応える。

Phase 0 疎通検証（2026-07-20・DB書込なし・`fetch_yahoo_history` 経由）で、候補8系列が全て Yahoo Finance v8 chart API から取得可能・6年で 1506-1510 行・最新値が直近営業日であることを確認済み。総合指数 `^BCOM` も生存（`^JGB` 404・`^TPX` 0件のような死にティッカーではない）。stooq はコモディティ先物がローカル IP でも全滅（0件）。

## Decision

1. **Yahoo Finance v8 chart API（既存 `fetch_yahoo_history`）で日次コモディティ8系列を追加**。新規ライブラリを増やさない（ADR-0011 の方針踏襲・requirements は `httpx` のみ）。`series_code` は英大文字:

   | code | name | yf_ticker | 業種チャネル |
   |---|---|---|---|
   | BCOM | ブルームバーグ商品指数 | ^BCOM | 総合コモディティ因子 |
   | COPPER | 銅先物 | HG=F | 非鉄/電線/機械 |
   | NATGAS | 天然ガス先物 | NG=F | 電力ガス/化学 |
   | SILVER | 銀先物 | SI=F | 貴金属/電子材料 |
   | WHEAT | 小麦先物 | ZW=F | 食品 |
   | CORN | トウモロコシ先物 | ZC=F | 食品/飼料 |
   | SOYBEAN | 大豆先物 | ZS=F | 食品/飼料 |
   | PLATINUM | プラチナ先物 | PL=F | 自動車触媒/商社 |

2. **transform は yoy 単一**。商品価格は常に正の水準系で、`_MACRO_MAP` の規約（水準系は yoy）に合致。**変換の正本はコード `_MACRO_MAP`（`plugins/macro_snapshots.py`）**であり、既存 WTI/GOLD もコード上は yoy。yoy/zscore の併用はチャネル情報の追加が無く M-1 の LassoLars 候補・M-2 の次元を無駄に増やすため見送る（必要になれば後続 Issue）。

3. **2 PR 構成で公開**（#218 の公開フロー準拠）。第1PR で `MACRO_SERIES` へ収集定義を追加 → `collect-macro.yml` で macro_data への蓄積を Actions（Azure IP）上で実証 → 第2PR で `_MACRO_MAP`/`MACRO_FEATURE_OPTIONS`/`_DLM_MACRO_MAP` へ公開。GHA Azure IP での `^BCOM` 最終疎通はこの公開フローが担保する。

4. **M-3（`_DLM_MACRO_MAP`）へも全8系列を追加**（logret）。日次先物は ADR-0012 の週次高頻度要件に適合する。ユーザーが「銘柄×特定コモディティの関係」の可視化を最重視したため、per-stock 時変βの完全対称性（総合指数含む全チャネル）を優先し、状態次元の増加（14→22）を許容する。`macro_dlm.py` の `DEFAULT_MACRO_FEATURES = 全OPTIONS` 連動により追加分は自動で既定 ON になる。**per-stock DLM の応答体感が劣化した場合は既定リストの明示絞り込み（総合指数のみ既定 ON 等）を後続 Issue で対応する**。

5. **不採用系列と理由**:
   - `BZ=F`（ブレント原油）: 既存 WTI と相関 ~0.98 で冗長。
   - `ALI=F`（アルミ）: Yahoo の履歴が薄い。
   - `^SPGSCI`（S&P GSCI）: エネルギー偏重で WTI と重複、総合指数は `^BCOM` に一本化。
   - `PA=F`（パラジウム）: PGM は `PL=F` を代表に採用（触媒需要は連動）。後続候補。

## Considered Options

- **総合指数のみ（^BCOM 1本）追加**（却下）: 銘柄別の業種感応度という要望に応えられない。個別商品の解像度が価値。
- **transform に yoy/zscore を併用**（却下）: §Decision 2。
- **M-3 には主要（銅・天然ガス）のみ追加**（却下）: ユーザーが完全対称性を明示選択。劣化時は既定絞り込みで縮退可能なため許容。
- **1 PR で収集＋公開を同時**（却下）: GHA Azure IP での指数系ティッカー（^BCOM）の疎通が本番で初めて確定するため、蓄積実証を挟む #218 の2PR フローが安全。

## Consequences

- **新規収集サーフェス**: `MACRO_SERIES`（`collector_prices.py`）へ8行。`fetch_yahoo_history`・`collect_macro_data`・`upsert_macro_batch`・`macro_data` スキーマ（category enum 'commodity' 確立済み）・GHA 3ワークフローは**変更不要**（縦持ち・系列汎用）。
- **新規モデルサーフェス**: `_MACRO_MAP`/`MACRO_FEATURE_OPTIONS`（`plugins/macro_snapshots.py`）へ8エントリ、`_DLM_MACRO_MAP`（`plugins/macro_dlm.py`）へ8エントリ。M-1（`macro_risk_return.py`）・M-2（`macro_gbdt.py`）本体は import 派生で**変更不要**。ハイパラ探索空間（macro_features は探索対象外・#264）・oof_backtest・スコア保存テーブルも変更不要。
- **新規 env var なし**: Yahoo は認証不要。
- **容量影響は軽微**: 8系列 × 6年 × ~251営業日 ≈ 12,000行（実績 1,255件/5年/系列）。Supabase 500MB に対し軽微。
- **疎データ耐性**: yoy 採用で5年 zscore 蓄積を待たず公開可。全 None 系列は `macro_beta_inference._drop_unusable_macro`（#352）が除外、M-2 は `macro_nan_ok=True` が吸収。`DEFAULT_MACRO_FEATURES`（M-1 既定3本）は不変のため既存ユーザーの結果は変わらない。
- **USD建て→円建て影響**: 商品価格はUSD建てで、円建て影響は既存 `USDJPY` 特徴量との組合せで M-2 XGBoost が捕捉する。マクロ×マクロの明示交差項は現行設計に無く、将来課題として記録。
- **ドキュメント更新**: MODELS.md（§9.2 の変換誤記 DXY/WTI/金→YoY 修正 + 新系列追記・チャネル数更新・M-3 ファクター一覧）・ARCHITECTURE.md（マクロ系列一覧）・DEPLOYMENT.md（バックフィル years=6 運用）・GOTCHAS.md（Phase 0 検証結果・^BCOM 配信状況）。
- **検証**: 第2PR 後にローカル /analysis で M-2 の SHAP 寄与・M-3 の β 出力に新系列が現れることを確認。`pytest` に `test_commodity_series_defined`・`_MACRO_MAP`↔`MACRO_FEATURE_OPTIONS` 整合テスト・dlm メンバーシップテストを追加。

## 追記（2026-08-06・#438）: BCOM の収集元を `^BCOM` → `DJP` へ差し替え

**本 ADR の決定は覆っていない**（総合コモディティ因子を1本持つ・transform は yoy・`series_code` は `BCOM`）。変わったのはその系列をどこから取るかだけなので、新 ADR は起こさず本節に記録する。

§Context の「総合指数 `^BCOM` も生存」は**採用時点（2026-07-20）の事実でしかなかった**。`^BCOM` は 2026-07-17 を最後に配信を止め、以降は timestamp 行だけ返して `close` が全 null になる（`^JGB` 404・`^TPX` 0件と出方が違う3例目）。`fetch_yahoo_history` は null 行を落とすため収集は 0 件＝`continue` で例外もエラーも出ず、`macro_bcom_yoy`（M-1/M-2/M-6 既定）と `dlm_bcom`（M-3 既定）が固定値を返し続けた。検知したのは #420 の鮮度ゲート（lag 18日）。

差し替え先は **`DJP`（iPath Bloomberg Commodity ETN）**。TOPIX `^TPX`→`1306.T`（#250）と同じ「指数が死んだら連動商品を代理にする」パターンで、追従性の実測（2026-08-06・2014年以降・BCOM 比）は候補中で最良:

| 候補 | 週次 logret corr | yoy corr | 履歴 |
|---|---|---|---|
| **DJP** | **0.9906** | **0.9957** | BCOM の全 2,509 営業日をカバー |
| DBC | 0.9027 | 0.9715 | 同上 |
| GSG | 0.8646 | 0.9470 | 同上 |
| BCI | 0.8360 | 0.9034 | 2017-03〜 |
| PDBC | 0.7965 | 0.7844 | 2014-11〜 |

`^BCOMTR`/`^DJUBS` は Yahoo に存在せず、`^SPGSCI` は生存だが §Decision 5 で却下済み（エネルギー偏重・WTI と重複）。

- **DJP はトータルリターン型**（旧 `^BCOM` は ER 指数）。yoy 水準が平均 +2.05%pt シフトするが、系列内で水準体系が一貫していれば yoy/logret は問題にならない。**危険なのは新旧 level の混在**なので、差し替えは全期間再収集（`--years 11`）で旧行を上書きする。DJP が BCOM の全営業日をカバーするため取り残し行は出ない（DELETE 不要）。
- **昇格ゲートは回していない**。既定セットの入れ替えではなくデータソースの修復であり、TOPIX の前例（#250）と同型。再判定したくなったら `python -m scripts.macro_feature_bakeoff --features macro_bcom_yoy` / `macro_dlm_feature_bakeoff --features dlm_bcom`。その際は `scripts/.cache/bakeoff_macro_*.pkl` と `dlm_macro_levels_*.pkl` が**差し替え前の BCOM 水準を持ったまま**なので、退避してから回すこと（キャッシュキーはデータ世代を含まない）。
- `macro_health.EXCLUDED_SERIES` から `BCOM` を外した。**直った系列を除外に残すと、今度は代理（DJP）側の停止を検知できなくなる。**
