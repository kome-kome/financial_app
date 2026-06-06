# 今後の課題・改善案

未実装の改善項目を記録する。完了済み項目は `docs/archive/IMPROVEMENTS.md` に集約してあるため、本書からは削除済み（git 履歴で参照可能）。

---

## Tier 2 — 分析品質の改善

### G. 発行済株式数の正規ソース取得
- **問題**: `plugins/total_return.py` の `shares_outstanding` は `bs_total_equity / bs_bps` で推計しているが、IFRS/JGAAP 混在・期中増資・優先株存在時に精度が低下する
- **改善案**: J-Quants `/markets/listed/info` の `IssuedShares` フィールドから正規の発行済株式数を取得し、`companies` テーブルに `issued_shares` カラム追加 + `cf_ops_ps` 計算に直接利用
- **前提**: `JQUANTS_API_KEY` が設定済みであること（プレミアムプラン要否は要確認）
- **Render 適合**: コード変更のみ。`init_db()` で `ALTER TABLE companies ADD COLUMN IF NOT EXISTS issued_shares` を冪等実装すれば起動時に自動マイグレーション
- **実装場所**: `collector.py` の `collect_stock_price_history_jquants` 拡張、`database.py` のスキーマ更新、`plugins/total_return.py` の置換

### H. `period_end` を VARCHAR から DATE 型へ移行
- **問題**: 現状 `String(20)` で `"YYYY-MM-DD"` を格納。期間比較は辞書順依存、JOIN や範囲インデックスの効率が悪い
- **改善案**: PostgreSQL の DATE 型へ移行
  ```sql
  ALTER TABLE financial_records
    ALTER COLUMN period_end TYPE DATE
    USING NULLIF(period_end, '')::DATE;
  ```
- **リスク**:
  - 非 ISO 形式値や空文字が含まれていた場合に移行失敗
  - `upsert_financial` のキー検索条件・各クエリで `String` → `date` 変換が必要
  - `calc_growth_rates` の `ORDER BY period_end` は型変更後も動くが要動作確認
- **前提**: Supabase ダッシュボードで `SELECT DISTINCT period_end FROM financial_records WHERE period_end !~ '^\d{4}-\d{2}-\d{2}$'` で異常値が無いことを確認 → 自動バックアップを取ってからマイグレーション
- **Render 適合**: マイグレーションを `init_db()` 内に冪等な `DO $$ ... $$` ブロックで書き、起動時に 1 度だけ実行。失敗時に環境変数 `SKIP_PERIOD_END_MIGRATION=1` で skip できるフェールセーフを用意
- **実装場所**: `database.py`（スキーマ・upsert・init_db）、`collector.py`（doc.get("periodEnd") の値変換）

---

## Tier 3 — 機能追加

### Macro. マクロ要因を組み込んだ分析モデル
- **問題**: マクロデータ（金利・為替）取り込み基盤は完成したが、これを使った分析モデルがまだない
- **改善案**: 既存プラグイン（`recommend.py` / `total_return.py` / `price_predictor.py`）に
  マクロ特徴量を追加（例: 10年金利水準・USDJPY変動率を特徴量として）
- **前提**: 過去5年のマクロデータがDB蓄積されていること（`/api/collect/macro/start` で取得）
- **実装場所**: `plugins/utils.py`（マクロ特徴量取得関数）、各プラグイン
- **設計留意**: マクロ系列は財務データと頻度が違う（日次 vs 年次）。決算月の前後Nヶ月の
  値や前年同月比などに変換してから OLS 特徴量に投入する必要がある

### 本番運用の残課題
- **DB バックアップ運用ポリシー**: Supabase の自動バックアップ機能を利用しつつ、復旧手順を文書化
- **監視**: Render ダッシュボード + UptimeRobot 等の外形監視追加検討

---

## Tier 2/3 — 財務項目の網羅性↑（収集パイプライン仕様変更）【grill 検討中・2026-06-05 保留】

`/grill-with-docs` で「データ収集パイプラインの仕様変更」を検討した到達点。用語は **root `CONTEXT.md`**（表示項目 / 分析特徴量 / 再分類項目）参照。目的は (1)鮮度↑ (2)網羅性↑ (4)コスト制約 の三立。**TDnet（真の四半期・年4点）派生は保留**（データ量制約大）。本命は **XBRL 項目の深掘り**。

欲しい項目を **C1（既に DB にある＝パイプライン変更不要・GUI 改修のみ）** と **C2（真に未収集＝要収集追加）** に仕分けた。

### C1 — 既存カラムの GUI 表示（パイプライン変更ゼロ・別 PR の company.js 改修）
- company.js が残差設計で捨てている既存カラムを表示する: 売上債権 `bs_receivables` / 棚卸資産 `bs_inventory` / 建物 `bs_buildings` / 機械 `bs_machinery` / 無形固定資産 `bs_intangible_assets` / 経常利益 `pl_ordinary_profit` / 特別損益純額（`pl_pretax_profit − pl_ordinary_profit` で導出）
- **注意**: 経常利益・特別損益は **JGAAP 専用概念**（IFRS/US-GAAP 企業は null）。「有形固定資産の内訳」は建物+機械のみで合計と不一致→チャートの balance invariant が壊れる。クリーンには有形固定資産合計タグの C2 収集が要る

### C2 — 新規 XBRL 項目の収集（要パイプライン変更＝本題）
- **表示用**: 減価償却費(合計) / 有形固定資産合計・投資その他資産合計 / 特別損益の内訳
- **分析特徴量用**: 研究開発費 / 減価償却費の内訳 / 特別損益の内訳 / 従業員数(Int・非財務) / 発行済株式数（→ **既存タスク G と統合**。G は J-Quants `IssuedShares`、本検討は XBRL 期末株式数タグを想定。per-share 正規化=MODELS.md の要なのでどちらかに一本化すること）
- **実装**: `XBRL_MAP`(collector.py) と `FinancialRecord`(database.py) の **両方更新**（CLAUDE.md 制約）
- **再収集方式 = (a) フル再収集1回でユーザー確定**。新項目の追加コストはほぼゼロ（同じ XBRL ZIP を再パースするだけ）。列追加の容量増は約1.6MB で些少＝**網羅性↑は容量問題ではない**

### ブロッカー（容量）— ✅ 解消済み（2026-06-06）
- ~~Supabase は **448MB/500MB（90%）**。主因は `stock_price_history`（359MB＝80%）~~
- **stock_price 移行を完遂**（`migrate_stock_price_dual.py` をローカル実行・旧 `stock_price_history` を DROP → 新 daily(225,616行)/weekly(354,684行)へ投入・照合 OK）。
- **再計測（2026-06-06）: DB 総容量 165MB/500MB・ヘッドルーム約335MB**。旧表 359MB が消え主因解消。weekly 49MB＋daily 27MB＋financial_records 73MB が主構成。
- フル再収集の一時肥大（全件 UPDATE で約60MB の dead tuple）は **335MB ヘッドルームに余裕で収まる** → C2 のフル再収集は**容量的に着手可能**。
- 補足: `raw_xbrl_json` drop（financial_records 73MB の第2レバー = PR-B）は容量緊急性が下がったが有効な打ち手として温存（[[project-collection-expansion]] / 容量プラン参照）。

### 未解決（再開時の最初の質問 = Q5）
**フル再収集をどの方式で回すか**（容量ブロッカーは解消済み・335MB ヘッドルームが前提）:
- (あ) 既存 upsert 方式でそのまま再収集（**最小変更・推奨**）。335MB ヘッドルームがあるため、従来必要だった `raw_xbrl_json` 削除＋`VACUUM FULL` の事前領域確保は**容量目的では不要**になった（やる場合は PR-B として独立実施）。
- (い) `TRUNCATE` → 全件 INSERT（肥大ゼロだが収集中サイトが数時間空・`MASTER_BATCH` 設計と不整合）
- ~~(う) `stock_price_history` 最適化~~ → **済**（dual-table 移行 2026-06-06 完了）。残るのは **鮮度 goal(1)=daily 差分の再有効化**（株価が 2026-03-06 で停止中。別タスク）。

推奨: **(あ) 即実施**。daily 差分再有効化は別枝。

---

## 注意事項（設計制約）

変更・実装時は以下の制約を必ず守ること（`CLAUDE.md` より）：

1. **次元整合性**: 無次元比率で絶対額を予測しない（Ohlsonモデル型で per-share 設計）
2. **外れ値処理**: OLS学習前に `winsorize(p1-p99)` を適用
3. **Zスコアは年度別に計算**（年度を跨いで計算しない）
4. **科学計算ライブラリ採用基準**: numpy/scipy/statsmodels/scikit-learn は採用可（`docs/VISION.md` の採用基準参照）。新規ライブラリ追加時は同基準と CLAUDE.md「パッケージ管理方針」に従う
5. **`docs/ARCHITECTURE.md` を同じ作業内で更新**
6. **Render デプロイ前提**: メモリ 512MB・スピンダウン 15 分・SSH 不可・永続ディスクなし。スキーマ変更は `init_db()` の冪等マイグレーションで対応。詳細は `docs/DEPLOYMENT.md`
