# 今後の課題・改善案

未実装の改善項目を記録する。完了済み項目は `docs/IMPROVEMENTS.md` に集約してあるため、本書からは削除済み（git 履歴で参照可能）。

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

> **完了済み**: 認証の HttpOnly Cookie 化（旧項目 F）は実装済み（Tier3-3）。`auth_token`（HttpOnly）＋`csrf_token` の2 Cookie + CSRF Double-Submit。詳細は [GOTCHAS.md](GOTCHAS.md) を参照。

---

## 注意事項（設計制約）

変更・実装時は以下の制約を必ず守ること（`CLAUDE.md` より）：

1. **次元整合性**: 無次元比率で絶対額を予測しない（Ohlsonモデル型で per-share 設計）
2. **外れ値処理**: OLS学習前に `winsorize(p1-p99)` を適用
3. **Zスコアは年度別に計算**（年度を跨いで計算しない）
4. **科学計算ライブラリ採用基準**: numpy/scipy/statsmodels/scikit-learn は採用可（`docs/VISION.md` の採用基準参照）。新規ライブラリ追加時は同基準と CLAUDE.md「パッケージ管理方針」に従う
5. **`docs/ARCHITECTURE.md` を同じ作業内で更新**
6. **Render デプロイ前提**: メモリ 512MB・スピンダウン 15 分・SSH 不可・永続ディスクなし。スキーマ変更は `init_db()` の冪等マイグレーションで対応。詳細は `docs/DEPLOYMENT.md`
