# 今後の課題・改善案

## ✅ 実装済み（アーカイブ）

### 財務データ × 株価の時系列予測モデル

| 項目 | 実装場所 |
|---|---|
| 月次ウォークフォワードCV | `plugins/utils.py` の `walk_forward_cv_monthly()` |
| 価格ベース特徴量（MA20乖離・ボラティリティ・RSI・ATR） | `plugins/price_predictor.py` |
| 財務特徴量（PER/PBR/ROE/Zスコア/gap_ratio）との結合 | `plugins/price_predictor.py` |
| ルックアヘッドバイアスなし設計 | `plugins/price_predictor.py` |
| プラグインとして `/api/plugins/price-predictor` で実行可能 | `api.py` + `plugins/price_predictor.py` |

---

## 未実装の課題

### Tier 2 — 分析品質の改善

#### A. total_return.py への業種固定効果追加
- **問題**: 全社一括 OLS では業種間の P/E・P/B 構造差でR²が構造的に低い（-0.1〜0.4）
- **改善案**: 業種ダミー変数を特徴量に追加（`sector_ols.py` は業種別に分けているが `total_return.py` は一括）
- **期待効果**: R²の向上、ランキング精度の改善
- **実装場所**: `plugins/total_return.py`、`plugins/utils.py`

#### B. gap_analysis の収束予測の改善
- **問題**: `half_life = abs(gap)/2`、`conv_score = 50 + gap×0.8` はヒューリスティック（統計的根拠なし）
- **改善案**: 過去の乖離率時系列から OU過程のパラメータ（平均回帰速度 κ）をMLE推定する
- **前提**: `stock_price_history` に十分な期間のデータがあること
- **実装場所**: `plugins/gap_analysis.py`、`plugins/utils.py`

#### C. 会計基準別の外れ値統計の可視化
- **問題**: `winsorize(p1-p99)` で対応済みだが「IFRS/JGAAP混在時に精度が下がる」ケースを可視化できていない
- **改善案**: `/api/collect/data-quality` か `checker.py` に会計基準別の統計を追加
- **実装場所**: `checker.py`、`api.py`

#### G. 発行済株式数の正規ソース取得
- **問題**: `plugins/total_return.py` の `shares_outstanding` は `bs_total_equity / bs_bps` で推計しているが、IFRS/JGAAP 混在・期中増資・優先株存在時に精度が低下する
- **改善案**: J-Quants `/markets/listed/info` の `IssuedShares` フィールドから正規の発行済株式数を取得し、`companies` テーブルに `issued_shares` カラム追加 + `cf_ops_ps` 計算に直接利用
- **前提**: `JQUANTS_API_KEY` が設定済みであること（プレミアムプラン要否は要確認）
- **実装場所**: `collector.py` の `collect_stock_price_history_jquants` 拡張、`database.py` のスキーマ更新、`plugins/total_return.py` の置換

#### H. `period_end` を VARCHAR から DATE 型へ移行
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
- **前提**: マイグレーション前に `SELECT DISTINCT period_end FROM financial_records WHERE period_end !~ '^\d{4}-\d{2}-\d{2}$'` で異常値が無いことを確認
- **実装場所**: `database.py`（スキーマ・upsert）、`collector.py`（doc.get("periodEnd") の値変換）

---

### Tier 3 — 機能追加

#### D. バックテスト機能
- **問題**: `recommend.py` / `total_return.py` のランキングが「将来パフォーマンスで評価する」と
  CLAUDE.md に書いてあるが、検証する仕組みがない
- **改善案**: 「Nヶ月前のスコア上位X社」を `stock_price_history` から実績リターン計算する API + UI
  - エンドポイント案: `GET /api/backtest?preset=balanced&months_ago=6&top_n=20`
  - レスポンス: スコア上位N社の平均リターン・中央値リターン・勝率 vs 全銘柄平均
- **価値**: 自分のモデルが実際に機能しているか定量検証できる
- **実装場所**: `api.py`（新エンドポイント）、`analysis.html`（結果表示）

#### E. 本番デプロイ対応
- **問題**: 現在はローカル専用構成（`localhost:5432`、開発用シークレット）
- **改善案**:
  - Nginx リバースプロキシ設定
  - `APP_SECRET_KEY` 等の秘密情報をシークレット管理（AWS Secrets Manager 等）
  - DB バックアップ自動化
- **参照**: `docs/ARCHITECTURE.md` セクション9（デプロイ構成図）

#### F. 認証の HttpOnly Cookie 化（セキュリティ強化）
- **問題**: 認証トークンを `localStorage` に保存（XSS時に盗難リスク）
- **改善案**: HttpOnly Cookie 方式へ移行
- **前提**: 認証フロー全体の再設計が必要（CSRF対策も同時実施）
- **参照**: `CLAUDE.md` の Tier3 既知リスク

---

## 注意事項（設計制約）

変更・実装時は以下の制約を必ず守ること（`CLAUDE.md` より）：

1. **次元整合性**: 無次元比率で絶対額を予測しない（Ohlsonモデル型で per-share 設計）
2. **外れ値処理**: OLS学習前に `winsorize(p1-p99)` を適用
3. **Zスコアは年度別に計算**（年度を跨いで計算しない）
4. **`ols()` は Pure Python 実装**（numpy/scipy 不使用）
5. **`docs/ARCHITECTURE.md` を同じ作業内で更新**
