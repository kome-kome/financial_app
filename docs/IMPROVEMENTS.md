# コードベース改善トラッキング

CLAUDE.md・FUTURE_TASKS.md に未記載の改善項目を順次対応する。完了したら ✅ を付けてここを更新する。

最終更新: 2026-05-18

---

## ✅ 完了済み

### [済] 1. `requirements.txt` から未使用パッケージを削除
- `numpy==2.4.4` を削除（プロジェクトは Pure Python 実装方針）
- 対応コミット: numpy 削除・/health 追加・launch.py のログハンドル解放

### [済] 2. `/health` ヘルスチェックエンドポイント追加
- `GET /health` で DB 疎通を確認し 200/503 を返す（認証不要）
- `docs/ARCHITECTURE.md` セクション8 も更新

### [済] 3. `launch.py` のログファイルハンドルリーク修正
- `_start_server()` で開いた `server.log` を `_shutdown()` で明示的に close

---

## 未対応 — 緊急度: 高

### [ ] 4. `api.py:271` の `str(e)` を固定文言化（情報漏洩対策）
- **現状**: `log_obj.message = str(e)` が `_run_collection_bg` で UI 表示用 DB カラムに保存
- **リスク**: 例外メッセージにスタックトレース・SQL・パス等が混入する可能性
- **対応**: `log.error(..., exc_info=True)` でサーバーログに残し、DB には固定文言を保存
- **影響箇所**: `api.py` 内の `_run_*_bg` 系すべて（grep で要確認）

### [ ] 5. グローバルジョブ状態の並行安全性確保
- **現状**: `_job_status` / `_market_status` / `_history_status` / `_jquants_status` を複数の非同期コンテキストから直接 dict 操作
- **リスク**: 単一ワーカー前提で偶然動作。複数 Worker・複数同時アクセス時にレース条件
- **対応**: `asyncio.Lock` で囲むラッパー関数を提供、または `dataclass` + 単一更新関数に集約

### [ ] 6. テストフレームワーク導入（pytest または unittest）
- **現状**: `test_*.py` ファイルが 0 個
- **対応**:
  - `tests/` ディレクトリ作成
  - `tests/test_utils.py`: `plugins/utils.py` の純関数（`ols`/`winsorize`/`kfold_cv`）
  - `tests/test_plugins.py`: 各プラグインのパラメータバリデーション
  - `tests/test_api_validation.py`: Pydantic モデルのバリデーション境界値
  - `pyproject.toml` か `pytest.ini` でテスト発見設定
- **注意**: CLAUDE.md「テスト方針」の手動実行と併存。CI 必須化は別途検討

---

## 未対応 — 緊急度: 中

### [ ] 7. `calc_growth_rates` の SQL window function 化
- **現状**: `database.py:317` で全 FinancialRecord をメモリにロードしてループ計算
- **リスク**: 数万件規模で OOM
- **対応**: PostgreSQL の `LAG() OVER (PARTITION BY edinet_code ORDER BY year)` で SQL 側に押し込み
- **検証**: 既存の `calc_growth_rates` の結果と一致することを確認

### [ ] 8. OLS に t 統計量・p 値を追加
- **現状**: `plugins/utils.py:44` の `ols()` は係数と R² のみ
- **対応**: 残差分散 → 係数の標準誤差 → t 値・p 値（自由度n-k-1の t分布）
- **UI**: `templates/analysis.html` で `|t| > 1.96` (p<0.05) をハイライト
- **MODELS.md**: 数式追記

### [ ] 9. 多重共線性チェック（VIF または相関行列）
- **現状**: 特徴量間の相関を未検査。相関が高いと `mat_inv()` が不安定化
- **対応**: `plugins/utils.py` に `check_collinearity(X)` を追加。VIF > 10 や |相関| > 0.9 の組を警告として返す
- **UI**: 警告メッセージを分析結果に併記

---

## 未対応 — 緊急度: 低

### [ ] 10. `setInterval` / `EventSource` のクリーンアップ
- **現状**: `collection.html` / `analysis.html` で `setInterval(loadSchedulerStatus, 30000)` 等が解放されない
- **対応**:
  - 各テンプレートで `setInterval` ID を変数に保存
  - `beforeunload` でクリーンアップ
  - 既存の `EventSource` 二重接続防止コードと併用

### [ ] 11. アクセシビリティ（aria 属性）の最低限対応
- **現状**: 全テンプレートに `role`/`aria-label`/`aria-live` なし
- **対応**:
  - プログレスバーに `role="progressbar"` + `aria-valuenow`
  - 通知 (`showNotif`) のコンテナに `role="alert"` + `aria-live="polite"`
  - フォーム input に `<label for>` の紐付け

### [ ] 12. レスポンシブ対応（モバイル幅）
- **現状**: `grid-template-columns: repeat(4, 1fr)` 固定、メディアクエリなし
- **対応**: 各テンプレートに `@media (max-width: 768px)` を追加し、grid 2列または1列にフォールバック
- **影響**: VISION.md「外出先からブラウザで使う」前提に直結

### [ ] 13. `recommend.py` の欠損指標ハンドリング
- **現状**: `plugins/recommend.py:93` で NULL を含む企業と全指標揃った企業を同じスコアで順位付け
- **対応**: 「必須指標」を定義し、欠損企業は除外（または別ランキング）。`MODELS.md` に明記

### [ ] 14. `total_return.py` の発行株式数推計の改善
- **現状**: `plugins/total_return.py:113` で `total_equity / bps` 推定
- **対応**:
  - 短期: ドキュメントに精度低下条件を明示
  - 長期: J-Quants `/markets/listed/info` から正規の発行済株式数を取得（別タスク化、FUTURE_TASKS 行き）

---

## 大規模な改修（FUTURE_TASKS.md 行き候補）

### [ ] 15. `period_end` を VARCHAR から DATE 型へ移行
- **現状**: YYYYMMDD 文字列。期間比較が辞書順依存、JOIN・インデックス効率が悪い
- **対応**: マイグレーション必要。既存データの DATE 変換 + 全クエリの修正
- **判断**: 単独タスク化し、データ移行プラン込みで FUTURE_TASKS.md に移管

---

## 進め方

緊急度高 → 中 → 低の順に実施。各項目完了ごとに本ファイルにチェックを入れて commit する。
