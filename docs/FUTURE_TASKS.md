# 今後の課題・改善案

未実装の改善項目を記録する。完了済み項目は `docs/archive/IMPROVEMENTS.md` に集約済み（git 履歴で詳細参照可能）。

> **凡例**: 各項目は「該当（`ファイル:行`）／問題／改善案／検証」で issue 化可能な粒度。優先度は 【高 / 中 / 低】、種別は「運用」（本番環境の操作・コード変更なし）か「コード」（新規実装）で示す。
> **直近完了（2026-06）**: Tier 1 リファクタ全件（T1-1〜T1-9）／ 発行済株式数の正規取得（G）／ `period_end` DATE 型移行（H）／ 財務項目網羅性 C1・C2。詳細は `docs/archive/IMPROVEMENTS.md`「Phase 4」。

---

## Tier 1 — 本番データの鮮度・完全性【運用・最優先】

> 既に実装・マージ済みの機能を本番で実データとして機能させるための運用作業。コード変更は基本不要だが、本番リソース（EDINET API キー・Supabase・GitHub Actions 実行権限）へのアクセスが要る。Claude のセッションからは実行不可で、ユーザー環境での操作が必要。

### DF-1. 株価 daily 差分収集（cron）の再有効化  【高・運用】
- **問題**: ライブの株価が **J-Quants 無料の約12週遅れ（最新 ≈2026-03 中旬）で頭打ち**。鮮度を担う Yahoo Finance ギャップ補完（`fill_recent_stock_price_gap_yahoo`）は差分パイプライン（`_pipeline_incremental.py` Phase 4）にのみ存在するが、その `daily-incremental.yml` の `on.schedule`（cron）が dual-table 移行（2026-06-06）後に**未実証のためコメントアウト中**。株価チャート・バックテストが鮮度を失っている。
- **改善案**: ① `workflow_dispatch` で手動1回実行 → 株価が当日付近まで前進し、Yahoo 補完が GitHub Actions の Azure IP から到達・成功することを確認 → ② `on.schedule` のコメントを外して cron を再開。
- **手順詳細**: `docs/DEPLOYMENT.md`「再有効化の段階運用（2026-06-10〜）」。
- **該当**: `.github/workflows/daily-incremental.yml` / `_pipeline_incremental.py`

### DF-2. C2 新項目の本番フル再収集  【中・運用】
- **問題**: C2 の新8列（`pl_depreciation` / `bs_ppe_total` / `bs_investments_other_assets` / `pl_extraordinary_income`・`loss` / `pl_rd_expenses` / `employees` / `issued_shares`）はコード結線済みだが、**本番 DB の既存レコードは再収集まで NULL** のまま。company 画面の内訳チャート・分析特徴量（R&D/D&A 集約度）に新項目が表示されない。
- **改善案**: `python collector.py --years 5`（**方式(あ)=既存 upsert・最小変更で確定**）。新項目の追加コストはほぼゼロ（同じ XBRL ZIP を再パースするだけ）・列追加の容量増は約1.6MB で、DB 容量 165MB/500MB・ヘッドルーム約335MB（2026-06-06 計測）に余裕で収まる。
- **注意**: 本番は `SKIP_XBRL_RAW=true`（Supabase 容量対策）で raw 未保存のため、`/api/collect/reparse`（再解析）は使えず **EDINET からの全件再取得が必要**。`raw_xbrl_json` 削除＋`VACUUM FULL` の事前領域確保は容量目的では不要（やる場合は独立 PR）。
- **該当**: `collector.py`（`run_full_collection`）／要 `EDINET_API_KEY`・数時間

---

## Tier 2 — 分析モデルの拡張【コード】

### M-1. マクロ要因を組み込んだ分析モデル  【中・コード】
- **問題**: マクロデータ（金利・為替）の収集基盤（`MacroData` テーブル・`collect_macro_data`・`MACRO_SERIES`・`/api/collect/macro/start`）は完成しているが、**これを使った分析モデルがまだない**（プラグインにマクロ特徴量の利用は皆無）。
- **改善案**: 既存プラグイン（`recommend.py` / `total_return.py` / `price_predictor.py`）にマクロ特徴量を追加（例: 10年金利水準・USDJPY 変動率）。
- **前提**: 過去5年のマクロデータが DB 蓄積されていること（`/api/collect/macro/start`）。
- **実装場所**: `plugins/utils.py`（マクロ特徴量取得関数）＋各プラグイン。
- **設計留意**: マクロ系列は財務データと頻度が違う（日次 vs 年次）。決算月の前後 N ヶ月の値や前年同月比などに変換してから OLS 特徴量に投入する。**次元整合性**（注意事項1）に注意し、水準そのものでなく無次元の変化率・偏差を特徴量とする。

---

## Tier 3 — 本番運用の堅牢化【運用・インフラ】

### O-1. DB バックアップ運用ポリシーの策定  【中・運用】
- Supabase の自動バックアップ機能を利用しつつ、復旧手順を `docs/DEPLOYMENT.md` に文書化する。

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
