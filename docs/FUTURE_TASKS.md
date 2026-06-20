# 今後の課題・改善案

未実装の改善項目を記録する。完了済み項目は `docs/archive/IMPROVEMENTS.md` に集約済み（git 履歴で詳細参照可能）。

> **凡例**: 各項目は「該当（`ファイル:行`）／問題／改善案／検証」で issue 化可能な粒度。優先度は 【高 / 中 / 低】、種別は「運用」（本番環境の操作・コード変更なし）か「コード」（新規実装）で示す。
> **直近完了（2026-06）**: Tier 1 リファクタ全件（T1-1〜T1-9）／ 発行済株式数の正規取得（G）／ `period_end` DATE 型移行（H）／ 財務項目網羅性 C1・C2 / **M-1 マクロ×リスク-リターン推奨モデル（Phase A–D 全件）**。詳細は `docs/archive/IMPROVEMENTS.md`「Phase 4」。

---

## Tier 1 — 本番データの鮮度・完全性【運用・最優先】

> 既に実装・マージ済みの機能を本番で実データとして機能させるための運用作業。コード変更は基本不要だが、本番リソース（EDINET API キー・Supabase・GitHub Actions 実行権限）へのアクセスが要る。Claude のセッションからは実行不可で、ユーザー環境での操作が必要。

### DF-2. C2 新項目の本番フル再収集  【中・運用】
- **問題**: C2 の新8列（`pl_depreciation` / `bs_ppe_total` / `bs_investments_other_assets` / `pl_extraordinary_income`・`loss` / `pl_rd_expenses` / `employees` / `issued_shares`）はコード結線済みだが、**本番 DB の既存レコードは再収集まで NULL** のまま。company 画面の内訳チャート・分析特徴量（R&D/D&A 集約度）に新項目が表示されない。
- **改善案**: `python collector.py --years 5`（**方式(あ)=既存 upsert・最小変更で確定**）。新項目の追加コストはほぼゼロ（同じ XBRL ZIP を再パースするだけ）・列追加の容量増は約1.6MB で、DB 容量 165MB/500MB・ヘッドルーム約335MB（2026-06-06 計測）に余裕で収まる。
- **注意**: 本番は `SKIP_XBRL_RAW=true`（Supabase 容量対策）で raw 未保存のため、`/api/collect/reparse`（再解析）は使えず **EDINET からの全件再取得が必要**。`raw_xbrl_json` 削除＋`VACUUM FULL` の事前領域確保は容量目的では不要（やる場合は独立 PR）。
- **該当**: `collector.py`（`run_full_collection`）／要 `EDINET_API_KEY`・数時間

### DF-3. 週次株価の履歴延伸（M-1 walk-forward CV の成立用）  【中・運用】
- **問題**: 週次株価（`stock_price_weekly`）の被覆が約2年分（本番 2026-06 時点で **2024-05-28〜**）しかなく、**M-1（マクロ×リスク-リターン推奨）の `use_macro=true` で walk-forward CV が 0 フォルド（`mean_r2=None`）**になる。学習サンプルは「52週先リターン（未来必要）」かつ「12ヶ月モメンタム（過去必要）」を同時要求するため、両条件を満たす月が**約1ヶ月の薄帯**に収縮するのが原因。`use_macro=false`（モメンタム非使用）なら複数月が確保され CV 成立（mean R²≈0.0122）。
- **改善案**: 週次株価を**5年程度までバックフィル**して履歴を延ばす（Yahoo Finance backfill 等。週次は容量影響が小さく、`stock_price_history` 2本立て＝daily 6か月＋weekly 全履歴の設計と整合）。延伸後に use_macro=true で CV フォルドが複数になることを確認。
- **検証**: バックフィル後 `plugins/macro_risk_return` を use_macro=true で実行し `cv_metrics.n_folds ≥ 2` / `mean_r2 != None` を確認。`SELECT min(trade_date) FROM stock_price_weekly` で被覆開始日を確認。
- **該当**: `collector_prices.py`（株価収集・Yahoo backfill）／`plugins/macro_risk_return.py`（§9.8 既知制約として記載済み）／要本番収集権限。詳細診断は MODELS.md §9.8。

---

## Tier 2 — 分析モデルの拡張【コード】

> 現在、未実装の分析モデル課題はありません。直近の **M-1（マクロ×リスク-リターン推奨モデル）** は Phase A–D 全件 + R3 リスク指標まで実装完了（2026-06-18・PR #189）。設計思想・数式・参考文献は [`MODELS.md`](MODELS.md) §9、実装の完了記録は [`archive/IMPROVEMENTS.md`](archive/IMPROVEMENTS.md)「Phase 4」を参照。新しい分析モデル課題はここへ追記する。

---

## Tier 3 — 本番運用の堅牢化【運用・インフラ】

### O-2. 外形監視の追加  【低・運用】
- Render ダッシュボード + UptimeRobot 等の外形監視を追加検討。死活監視用の `/health` エンドポイントは既設（DB 疎通で 200/503）。

---

## 注意事項（設計制約）

変更・実装時は以下の制約を必ず守ること（`CLAUDE.md` より）：

1. **次元整合性**: 無次元比率で絶対額を予測しない（Ohlsonモデル型で per-share 設計）
2. **外れ値処理**: OLS学習前に `winsorize(p1-p99)` を適用
3. **Zスコアは年度別に計算**（年度を跨いで計算しない）
4. **科学計算ライブラリ採用基準**: numpy/scipy/statsmodels/scikit-learn は採用可（`docs/VISION.md` の採用基準参照）。新規ライブラリ追加時は同基準と CLAUDE.md「パッケージ管理方針」に従う
5. **`docs/ARCHITECTURE.md` を同じ作業内で更新**
6. **Render デプロイ前提**: メモリ 512MB・スピンダウン 15 分・SSH 不可・永続ディスクなし。スキーマ変更は `init_db()` の冪等マイグレーションで対応。詳細は `docs/DEPLOYMENT.md`
