# コードベース改善トラッキング

> **ステータス: ✅ 全14項目完了済み（2026-05）**
> 本書は完了済みの改善作業の記録として保存する。新規タスクは `docs/FUTURE_TASKS.md` に追加すること。

最終更新: 2026-05-31

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

### [済] 4. `api.py` の `str(e)` を固定文言化（情報漏洩対策）
- 対応: `_run_collection_bg` / `_run_smart_collection_bg` / 株価履歴 / J-Quants / 市場データの 5 箇所で `str(e)` を固定文言に置換し、サーバーログには `log.error(..., exc_info=True)` を追加
- 市場データ収集には except 節がなかったため新規追加（クラッシュ時に状態が更新されない問題を併せて修正）
- プラグインの `ValueError` 系（`HTTPException(400, str(e))`）はユーザー向け文言（業種未指定等）が前提のため変更なし

### [済] 5. グローバルジョブ状態の整合性確保
- 分析の結果、asyncio 単一イベントループでは mutations が await を跨がないため Lock は不要。実際の問題は次の 2 点だった:
  - SSE 消費者がログ切り捨て時にインデックスがずれてログを取りこぼす
  - `_market_status` / `_history_status` / `_jquants_status` が無制限に成長（メモリリーク）
- 対応: `_append_log()` ヘルパー導入、各ステータス辞書に `log_seq`（単調増加カウンタ）を追加、SSE 消費者を seq ベースに変更、全状態辞書で 500 件で truncate
- シングル Worker 前提のコメントをグローバル定義箇所に追加

### [済] 6. テストフレームワーク導入（pytest）
- `tests/` ディレクトリ作成、`pytest.ini` 設定、`tests/README.md` で運用方針記載
- `tests/test_utils.py`: `plugins/utils.py` の純関数を 20 ケース（winsorize/normalize/normalize_transform/ols/kfold_cv/walk_forward_cv/walk_forward_cv_monthly）→ 全パス
- DB / API のテストは外部依存（SQLAlchemy セッション・FastAPI クライアント）が必要なため次フェーズへ繰り越し（tests/README.md に拡張候補を記載）

---

## 未対応 — 緊急度: 中

### [済] 7. `calc_growth_rates` の SQL window function 化
- `database.py:calc_growth_rates` を CTE + `LAG() OVER (PARTITION BY edinet_code ORDER BY year, period_end)` の UPDATE 1 本に書き換え
- メモリ展開ループを廃止し、PostgreSQL 側で完結。大規模データでも OOM リスク解消
- セマンティクス（旧実装との一致点）を docstring に明記: 前期・当期が共に非 NULL かつ 非 0 の場合のみ更新、% で小数 2 桁丸め
- `docs/ARCHITECTURE.md` セクション 6 のノードを更新
- 動作検証: PostgreSQL 環境での実機テストはこの環境では未実施（DB 接続未設定）。本番反映前に手動確認推奨

### [済] 8. OLS に t 統計量・p 値を追加
- `plugins/utils.py:ols()` の戻り値に `se` / `t_stat` / `p_value` / `df` を追加
- p 値計算: df ≥ 30 は正規近似（`math.erf`）、df < 30 は簡易補正（小サンプルは参考値扱い）
- `plugins/sector_ols.py` の `sector_stats` に `n_significant_features`（p<0.05 の説明変数数）、`p_values`、`t_stats` を併記
- `docs/MODELS.md` / `templates/models.html` に数式と運用方針を追記
- 既存の `result["beta"]` 等を読む既存呼び出し（total_return, price_predictor）は変更なし（追加キーのみ）
- `tests/test_utils.py` に 3 ケース追加（合計 23 → 全パス）

### [済] 9. 多重共線性チェック（VIF + Pearson 相関）
- `plugins/utils.py` に `check_collinearity()` を追加。VIF と Pearson 相関行列を返す
- 閾値超過（VIF > 10 / |r| > 0.9）は `high_vif` / `high_corr_pairs` に集約
- `plugins/sector_ols.py` の `sector_stats` に `collinearity_warnings` を追加（業種ごとに警告を返す）
- `docs/MODELS.md` / `templates/models.html` に計算式と閾値を明記
- `tests/test_utils.py` に 4 ケース追加（合計 27 → 全パス）

---

## 未対応 — 緊急度: 低

### [済] 10. `setInterval` / `EventSource` のクリーンアップ
- `collection.html` 末尾の `setInterval(loadSchedulerStatus, 30000)` を `const _schedulerTimer` に格納
- `beforeunload` リスナーを追加し、`clearInterval` + `searchTimer` の `clearTimeout` + 全 SSE (`_smartSSE`, `_collectSSE`, `_historySSE`, `_jqSSE`, `marketSSE`) の `close()` を実施
- analysis.html / dashboard.html は `setInterval`・`EventSource` を使っておらず、`setTimeout` も通知の自己削除のみのため対応不要
- 再帰的な `setTimeout(pollJobStatus, ...)` は再帰条件 (`d.running`) が満たされなくなれば停止するため不要なクリーンアップは入れない

### [済] 11. アクセシビリティ（aria 属性）の最低限対応
- `dashboard.html` / `analysis.html` の `showNotif` に `role="alert"`（エラー時）/ `role="status"`（成功時）と `aria-live` を動的付与
- `collection.html` の 4 つの log-box に `role="log"` + `aria-live="polite"` + `aria-label`
- 全 7 個の `.progress-bar` に `role="progressbar"` + `aria-valuemin="0"` + `aria-valuemax="100"`
- `login.html` の input は元々 `<label for>` 紐付け済み（変更なし）
- 残課題: collection.html / analysis.html のフォーム input（`<label>X</label><input>` 兄弟パターン）は数十箇所あり、別タスクとして繰り越し
- 残課題: `aria-valuenow` の動的更新（progress-fill width 変更時）はヘルパー導入が必要。視覚ラベル併記しているため緊急度低

### [済] 12. レスポンシブ対応（モバイル幅）
- `collection.html` / `analysis.html` / `dashboard.html` に `@media (max-width: 768px)` と `@media (max-width: 480px)` を追加
- 768px 以下: 4 列 → 2 列、コンテナ padding 縮小
- 480px 以下: 全グリッド 1 列、カード padding 縮小
- 全テンプレートに viewport meta は元から存在（変更なし）
- 残課題: テンプレート内の `style="grid-template-columns:..."` インライン指定（10 箇所程度）はそのまま。本格対応は CSS クラス化が必要

### [済] 13. `recommend.py` の欠損指標ハンドリング
- スコアを weighted **mean** に変更: `Σ(w_j × z_j) / Σ|w_j|`（present のみ）
- `min_coverage` パラメータを追加（デフォルト 0.5、`Σ|w_j of present| / Σ|w_j of all|`）
- 結果に `coverage`・`skipped_low_coverage` を併記
- `docs/MODELS.md` セクション 6 / `templates/models.html` の数式を更新
- 旧実装の「単純和」では値が揃った銘柄が有利だった問題を解消

### [済] 14. `total_return.py` の発行株式数推計（短期対応）
- `shares_outstanding` の docstring を拡充し、精度低下が起こる 3 条件（IFRS/JGAAP 差・期中増資/自己株消却・優先株存在）を明記
- 根本対応案として「J-Quants `/markets/listed/info` の `IssuedShares` 利用」を `docs/FUTURE_TASKS.md` の Tier 2-G として追加

---

## 大規模な改修（FUTURE_TASKS.md に移管済み）

### [移管済] 15. `period_end` を VARCHAR から DATE 型へ移行
- 本番データで非 ISO 値・空文字が混じっていた場合のマイグレーション失敗リスクが大きく、テスト環境（PostgreSQL + 実データ）での事前検証が不可欠
- `docs/FUTURE_TASKS.md` の Tier 2-H として移管。移行 SQL とリスク・前提条件を記載

---

## 進め方

緊急度高 → 中 → 低の順に実施。各項目完了ごとに本ファイルにチェックを入れて commit する。

## 全項目の完了サマリ

| 番号 | 項目 | 状態 |
|---|---|---|
| 1 | requirements.txt の numpy 削除 | ✅ 完了 |
| 2 | /health エンドポイント追加 | ✅ 完了 |
| 3 | launch.py のログハンドル解放 | ✅ 完了 |
| 4 | str(e) の固定文言化（情報漏洩対策） | ✅ 完了 |
| 5 | SSE ログ取りこぼし & メモリリーク修正 | ✅ 完了 |
| 6 | pytest 導入 + 27 ケース | ✅ 完了 |
| 7 | calc_growth_rates の SQL window function 化 | ✅ 完了 |
| 8 | OLS に t統計量・p値追加 | ✅ 完了 |
| 9 | 多重共線性チェック（VIF + Pearson） | ✅ 完了 |
| 10 | setInterval / EventSource クリーンアップ | ✅ 完了 |
| 11 | aria 属性の最低限対応 | ✅ 完了（残課題は別タスクへ） |
| 12 | レスポンシブ対応のメディアクエリ | ✅ 完了 |
| 13 | recommend.py を weighted mean + min_coverage に | ✅ 完了 |
| 14 | total_return.py の docstring 改善 | ✅ 完了 |
| 15 | period_end の DATE 型化 | 🔄 FUTURE_TASKS.md に移管 |

---

## Phase 2: サードパーティーライブラリ導入（VISION.md 方針緩和後）

VISION.md「サードパーティーライブラリ採用基準」を満たす numpy / scipy / statsmodels /
scikit-learn の導入が承認され、統計解析の質を向上させる改善を実施。

### [済] P2-1. scipy.stats.t による正確な p 値
- `plugins/utils.py:_two_sided_pvalue` を `scipy.stats.t.sf` ベースに置換
- 旧: df ≥ 30 は正規近似、df < 30 は Cornish-Fisher 風の簡易補正（「参考値」扱い）
- 新: 全 df で `scipy.stats.t.sf(|t|, df) * 2`（数値安定、業界標準）
- 効果: df = 10〜29 の小サンプル業種で正確な有意性判定
- テスト: `tests/test_utils.py::TestScipyPvalue` 2 ケース追加（scipy リファレンスとの一致確認）

### [済] P2-2. numpy.linalg.lstsq による OLS の数値安定化
- `plugins/utils.py:ols()` の Gauss-Jordan 消去法を SVD ベースの `numpy.linalg.lstsq` に置換
- 旧: 条件数の悪い行列で丸め誤差が累積し係数が暴れる
- 新: SVD で rank-deficient な行列でも安定して解を得る + 返り値に `rank`, `condition_number` 追加
- 既存呼び出し（`beta`/`yhat`/`r2`/`adj_r2`/`rmse`/`mae`/`se`/`t_stat`/`p_value`/`df`）は完全に後方互換
- テスト: `TestOlsExtras` 2 ケース追加（rank-full / rank-deficient）

### [済] P2-3. statsmodels.OLS による詳細統計診断
- `plugins/utils.py` に新関数 `ols_with_diagnostics(X, y, cov_type)` を追加
- 標準の `ols()` 出力に加えて以下を返す:
  - `durbin_watson`: 残差自己相関
  - `jarque_bera`: 残差正規性 ({stat, pvalue, skew, kurtosis})
  - `f_stat`, `f_pvalue`: モデル全体の F 検定
  - `cov_type` で HC0/HC1/HC2/HC3 のロバスト標準誤差を選択可能
- `plugins/sector_ols.py` の `sector_stats` に `diagnostics` フィールドとして併記
- テスト: `TestOlsWithDiagnostics` 2 ケース追加（基本診断 + HC3 vs nonrobust 比較）

### 合計テスト数: 27 → 33 ケース（全パス）

### [済] P2-7. AR(1) MLE で gap_analysis の半減期を統計的に推定
- `plugins/gap_analysis.py` に `_estimate_ar1_half_life_years()` を追加
  - `statsmodels.tsa.arima.model.ARIMA(order=(1,0,0))` で MLE
  - 平均回帰条件（`0 < φ < 1`）と妥当範囲（`0.25 ≤ HL ≤ 20 年`）でガード
  - `HL = -ln(2)/ln(φ)`（連続時間 OU の `HL = ln(2)/κ` と対応：`φ = exp(-κΔt)`）
- 各銘柄の年次 `gap_ratio` 履歴（≥ 8 観測）を DB から集めて MLE
- 履歴不足や条件未達の銘柄は旧ヒューリスティック（`|gap|/2`）にフォールバック
- 出力に `method` / `ar1_phi` / `n_history` / `half_life_months` を併記
- レスポンスに `n_ar1_estimated` / `n_heuristic_fallback` サマリ
- `docs/MODELS.md` セクション 3 / `templates/models.html` を AR(1) ベースに改訂
- `FUTURE_TASKS.md` Tier 2-B（OU 過程 ML 推定）の実質的な解決
- テスト: `TestAr1HalfLife` 3 ケース（φ 回復・短系列拒否・単位根拒否）

### [済] P2-5. `sklearn.TimeSeriesSplit` との一致検証テストを追加
- `walk_forward_cv_monthly` の置換は実施せず（実装は既に正しく、置換コストが大きい）
- 代わりに `tests/test_utils.py::TestWalkForwardSklearnConsistency` を追加
- `min_train_months=18` 以降のテスト月インデックスが業界標準の TimeSeriesSplit の
  「train が test より厳密に過去」セマンティクスを満たすことを検証
- 効果: ルックアヘッドバイアス回避の独立検証ができ、リファクタ時の回帰検出が可能

### [済] P2-8. `numpy.percentile` / `numpy.mean`/`std` への置換
- `api.py:_bt_percentile` を `numpy.percentile(method="linear")` に置換
- `_backtest_single` の集計を `numpy` ベース（mean, std）に
- セマンティクスは同一（線形補間パーセンタイル、population std）。コードが簡潔に

### [済] P2-6. Ridge 回帰（sklearn.linear_model.RidgeCV）を選択肢として追加
- `plugins/utils.py` に `ridge_regression(X, y, alphas, cv_folds)` を新設
  - `sklearn.linear_model.RidgeCV` で α を CV 自動選択
  - 戻り値スキーマは `ols()` と同形（SE / t / p は NaN、Ridge では伝統的に未定義）
  - 追加で `alpha`（選択された正則化パラメータ）と `method="ridge"` を返す
- `plugins/sector_ols.py` の params に `regularization` を追加（none / ridge）
  - Ridge 選択時は OLS の代わりに ridge_regression を呼び、`stat_entry.method` / `.alpha` を併記
  - 統計診断（Durbin-Watson 等）は OLS のみ実施（Ridge では skip）
- `docs/MODELS.md` / `templates/models.html` に Ridge 数式と運用方針を追記
- テスト: `TestRidgeRegression` 2 ケース追加（係数回復 + 多重共線性下の安定性）

### [済] P2-4. 価格特徴量を numpy ベース化（pandas rolling は不採用）
- `plugins/price_predictor.py` の `_ma` / `_log_vol` / `_rsi` / `_atr_ratio` を Pure Python の `sum()` / list comprehension から `numpy` の vectorized 演算に置換
- `pandas.DataFrame.rolling()` で全インデックスを一括計算する案も試したが、実ワークロード（n=500, snaps=24）でベンチマークの結果、**70 倍遅くなる**ことが判明
- 理由: スナップショット数 ≪ 価格履歴長のため、末尾 n+1 本のみ計算する旧方式の方が pandas DataFrame セットアップコストを払わない分速い
- 採用: numpy ベースの helper（旧 Pure Python と同等の `O(n)` だがコードがクリーン）
- 不採用: pandas rolling 全インデックス計算（教訓: ベンチマーク不在の最適化は信頼できない）
- テスト: `TestPriceFeatures` 5 ケース追加（合計 38 ケース）

## Phase 3: FUTURE_TASKS Tier 2 の実装

### [済] T2-A. total_return.py に業種固定効果を追加
- `plugins/total_return.py` に `use_sector_fe` パラメータ（デフォルト True）を追加
- サンプル数 ≥ 5 の業種を One-hot ダミー変数化（最初の業種を基準としてドロップ）
- シミュレーション検証: 業種別 P/E 差を含むデータで R² が **0.83 → 0.97** に改善
- 出力に `sector_fixed_effects: {enabled, baseline, effects, n_dummies}` を追加
- `docs/MODELS.md` / `templates/models.html` のモデル 1 を更新

### [済] T2-C. 会計基準別の外れ値統計の可視化
- `checker.py` に `_check_by_accounting_standard()` を追加
- 9 項目（売上高・営業利益・EPS・純資産・BPS・営業CF・ROE・PER・PBR）の NULL 率と
  外れ値率を JGAAP / IFRS / US-GAAP / 未設定で集計
- `/api/collect/data-quality` のレスポンスに `accounting_standard` フィールド追加
- `templates/collection.html` の品質レポートに会計基準別テーブルを表示

### [既存] T2-D. バックテスト機能（FUTURE_TASKS の D）
- 過去の実装でカバー済み: `GET /api/backtest` / `GET /api/backtest/multi`
- ARCHITECTURE.md セクション 8 に記載済み

## Phase 2 残課題（中・低優先度）

| 番号 | 項目 | 状態 |
|---|---|---|
| P2-4 | 価格特徴量を numpy ベース化（pandas rolling は不採用） | ✅ 完了 |
| P2-5 | `sklearn.TimeSeriesSplit` との一致検証テストを追加 | ✅ 完了 |
| P2-6 | `sklearn.linear_model.Ridge` で正則化回帰の選択肢追加 | ✅ 完了 |
| P2-7 | `statsmodels.tsa.ARIMA(1,0,0)` で AR(1) MLE による半減期推定 | ✅ 完了（履歴不足時はヒューリスティックにフォールバック） |
| P2-8 | `numpy.percentile` / `numpy.mean`/`std` への置換 | ✅ 完了 |
