# 日本マクロ統計（CPI/M2/短観DI）を e-Stat API / 日銀 REST API から取得する

## Status

accepted（2026-06-27・grill-with-docs/Opus で確定）。Issue #251（第2弾）の設計決定。実装は Sonnet 担当。

## Context

FRED チャネル（#250・第1弾）では日本の主要マクロ指標の一部が取得できない——CPI（`CPALTT01JPM657N` は2021/6 で更新停止）・M2（`MYAGM2JPM189S` は2017/2 で停止）・日銀短観 業況判断DI（FRED 未提供）。これらは原データ提供元から直接取りに行く必要がある。

日本の公的統計には複数の取得経路があり、ソース選択がフェッチャー実装の形を決める：

- **物価（CPI）**: 総務省統計局が e-Stat API（`api.e-stat.go.jp`・要 application ID・JSON/XML）で提供。FRED 同様、政府公式の長期安定 API。
- **マネーストック（M2）・短観DI**: 日銀が2系統で提供——(a) 新 **REST API**（`api.boj.or.jp`・認証不要・JSON）、(b) 旧 **CSV ダウンロード**（`stat-search.boj.or.jp`・ZIP 内 Shift_JIS の CSV・20年超の実績）。

既存の FRED コネクタ（`fetch_fred_series` in `collector_prices.py`）が確立した方式——key 未設定時スキップ・`lag_days` で `trade_date` を公表ラグ補正・`macro_data` テーブルへ upsert——を踏襲できれば、分析側（`_MACRO_MAP` / スナップショット）は series_code を追記するだけで載る。

## Decision

1. **CPI は e-Stat API から取得**。`ESTAT_API_KEY` 環境変数でゲートし、未設定時はスキップ（`FRED_API_KEY` と同挙動）。新フェッチャー `fetch_estat_series`。

2. **M2・短観DI は日銀 REST API（`api.boj.or.jp`）から取得**。認証不要ゆえゲートなし・常時収集。新フェッチャー `fetch_boj_series`。**旧 CSV ダウンロード（`stat-search.boj.or.jp`）は採らない**。

3. **配置は `collector_prices.py`**。FRED フェッチャーと同居・同型（CLAUDE.md ファイル役割表「株価・市場データ更新・マクロ収集」と整合）。肥大化したら `collector_macro.py` 分離を別 Issue 化。

4. **対象3指標とデフォルト公開**（[[マクロ系列バリアント / デフォルト公開]]）：
   - **CPI**: 全国総合・全国コア（生鮮除く）・東京都区部総合を全収集。`_MACRO_MAP` デフォルトは `JP_CPI_CORE`（BOJ が政策判断に使う基準で M-1 の金利文脈と整合）・transform=`yoy`・lag=30。
   - **短観DI**: 製造業/非製造業 × 大企業、製造業中小の4バリアントを全収集。デフォルト `JP_TANKAN_MFG_LARGE`（上場企業の製造業比率が高く景気代理変数として直接的）・transform=`zscore`（DI は既に拡張/収縮の相対水準値ゆえ yoy は解釈が歪む）・lag=14。
   - **M2**: `JP_M2` 単独（バリアント間の経済的解釈差が小さく M2+CD/M3 を増やす便益が薄い）・transform=`yoy`・lag=21。
   - category 値は `"price"` / `"money"` / `"survey"` を新設（`macro_data.category` は CHECK 制約なし・後方互換）。

5. **GDP 需要項目（個人消費・設備投資・住宅投資・公共投資）・在庫指数は本 PR スコープ外**（第3弾へ先送り）。内閣府 SNA の構造が複雑で別 PR に値し、景気センチメントは短観で代替できる。

## Considered Options

- **日銀を CSV ダウンロード（`stat-search.boj.or.jp`）で取得**（却下）：20年超の実績があり最も安定だが、ZIP 内 Shift_JIS CSV の解析実装が重くメンテコストが高い。REST API は JSON・認証不要で FRED フェッチャーと同型に書け、実装サーフェスが小さい。REST が比較的新しい点はリスクだが、M2・短観ともエンドポイントは実用段階。
- **CPI も日銀経由 or 別ソース**（却下）：CPI の一次提供元は総務省＝e-Stat であり、政府公式 API を直接使うのが素直。
- **`collector_macro.py` に新規分離**（却下）：責務分離は綺麗だが、FRED と同型のフェッチャー2本のために CLAUDE.md・ARCHITECTURE.md・`collector.py` import を更新するコストが、現時点の便益を上回る。肥大化を別 Issue で対処する方が安全。
- **全指標を一度に実装（GDP 需要項目含む）**（却下）：検証サーフェスが広がりすぎる。CPI/M2/短観の3本柱に絞り、SNA 需要項目は第3弾へ。

## Consequences

- **新規収集サーフェス**：`fetch_estat_series` / `fetch_boj_series`（`collector_prices.py`）・`ESTAT_SERIES` / `BOJ_SERIES` 定数・`collect_macro_data` への結線・`_MACRO_MAP`（`plugins/macro_snapshots.py`）へ3デフォルト系列追記。
- **新規 env var**：`ESTAT_API_KEY`（Render / GitHub Actions Secrets へ追加が必要・DEPLOYMENT.md に FRED と並べて手順記載）。日銀はキー不要。
- **公表ラグの正しさが収集層に集中**（[[公表ラグ補正]]）：各系列の `lag_days` を実際の公表スケジュールに合わせる責任は収集層にあり、分析側はリークを意識しない。
- **容量影響は軽微**：月次/四半期の追加で数 MB 程度。Supabase 500MB 制約に対し問題なし。
- **検証**：各系列の最終更新日・季調有無・単位を確認。`tests/test_macro_*` 追加＋ M-1/M-3 の run でカバレッジ確認。
- **ドキュメント更新**：ARCHITECTURE.md（マクロ収集チャネル）・DEPLOYMENT.md（外部サービス制約に e-Stat / 日銀を追記）・GOTCHAS.md（季調・公表ラグの注意）・MODELS.md（特徴量追加）。実装後 `/tidy` で doc⇔code 乖離点検。
- **歴史的記録は不変**：本 ADR は設計時点の決定の記録。実装で REST エンドポイントが想定と異なれば、その差分は実装 PR / GOTCHAS.md に記録し本 ADR は据え置く。
