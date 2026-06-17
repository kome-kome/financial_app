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

**背景**  
マクロデータ収集基盤（`MacroData` テーブル・`collect_macro_data`・9系列・`/api/collect/macro/start`）は完成済み。9系列（USDJPY, EURJPY, US10Y, JP10Y, NIKKEI225, TOPIX, SP500, WTI, GOLD）を日次 OHLCV で蓄積。しかし **分析プラグイン側はマクロ特徴量を一切使っていない**（`plugins/utils.py` にマクロ取得関数ゼロ）。

**対象プラグインと除外理由**

| プラグイン | 対応 | 理由 |
|---|---|---|
| `price_predictor.py` | ✅ 優先 | 月次スナップ・対数リターン(無次元)目的・`snap_date` 基準でマクロを時点整合できる |
| `total_return.py` | ✅ 次フェーズ | 年次・`period_end+45日` 基準で結合。Ohlson 型の per-share 特徴量と無次元マクロを混在させる際、正規化パイプラインが吸収する |
| `recommend.py` | ❌ 除外 | クロスセクション Z スコアは全企業で **同一値** → 分散=0 → 特徴量として機能しない。レジームフィルタへの拡張は別設計（スコープ外）|

**マクロ特徴量の設計（次元整合性を守る）**

生データは [円/ドル] や [%] の次元を持つため **無次元化してから OLS 投入** する。2方式を採用：

| 特徴量名 | series_code | 変換式 | 次元 | 適用先 |
|---|---|---|---|---|
| `macro_usdjpy_yoy` | USDJPY | (ref30日平均 − 1y前30日平均) / 1y前30日平均 | 無次元率 | price_predictor, total_return |
| `macro_us10y_zscore` | US10Y | (ref30日平均 − 5y平均) / 5y標準偏差 | 無次元 Z | price_predictor |
| `macro_jp10y_zscore` | JP10Y | (ref30日平均 − 5y平均) / 5y標準偏差 | 無次元 Z | total_return |
| `macro_sp500_yoy` | SP500 | (ref30日平均 − 1y前30日平均) / 1y前30日平均 | 無次元率 | price_predictor |

`ref30日平均` = ref_date 直前 30 日の close 平均（日次ノイズ軽減）。金利は水準の YoY 変化率でなく Z スコアを採用（% 点変化は直感的でないため）。

**周波数ミスマッチ（日次 vs 年次）の対処**  
`price_predictor._find_applicable_fin` の「`period_end + 45日 ≤ snap_date` を満たす最新財務」パターンを流用。snap_date 時点のマクロ値（上記30日平均）を財務スナップと同じ snap_dict に追加するだけで、既存の OLS パイプラインに乗る。

**実装場所と関数仕様**

```python
# plugins/utils.py — 新規追加
def get_macro_features(
    db,
    ref_date: date,
    feature_names: list[str],   # ["macro_usdjpy_yoy", "macro_us10y_zscore", ...]
    window_days: int = 30,      # ref_date 直前 N 日の close 平均
    zscore_years: int = 5,      # Z スコア算出用の歴史窓
) -> dict[str, float | None]:
    """
    MacroData テーブルから ref_date 時点のマクロ特徴量 dict を返す。
    DB 未蓄積（NULL）は None を返し、呼び出し側でサンプルを除外する。
    """
```

```python
# plugins/price_predictor.py — 追加箇所
MACRO_FEATURE_OPTIONS = [
    {"value": "macro_usdjpy_yoy",   "label": "USD/JPY 前年比変化率"},
    {"value": "macro_us10y_zscore", "label": "米10年金利 5年Zスコア"},
    {"value": "macro_sp500_yoy",    "label": "S&P500 前年比リターン"},
]
# 1. params_schema() の features.options に MACRO_FEATURE_OPTIONS を追記
# 2. _build_snapshots() で get_macro_features(db, snap_date, ...) を呼び出し
#    → snap_dict に "macro_*" キーを追加
#    → _fit_final_model はキー名で X 列を構築するため改修不要（既設計）
```

**実装順序**

1. `plugins/utils.py` に `get_macro_features` を追加（DB クエリ + 変換ロジック）
2. `tests/test_price_predictor.py` にマクロ特徴量 None ケースのテスト追加
3. `plugins/price_predictor.py` に `MACRO_FEATURE_OPTIONS` + `_build_snapshots` 拡張
4. `plugins/total_return.py` に同様追加（次フェーズ）
5. `docs/MODELS.md` の price_predictor セクション更新（マクロ特徴量の説明追記）
6. `docs/ARCHITECTURE.md` の plugins セクション更新

**前提条件の確認方法**  
蓄積状況は Supabase SQL エディタで確認：
```sql
SELECT series_code, MIN(trade_date), MAX(trade_date), COUNT(*)
FROM macro_data GROUP BY series_code ORDER BY series_code;
```
5年分（≈1250行/系列）が揃っていれば準備完了。足りない場合は `/api/collect/macro/start` で再収集。

**注意事項**
- OLS 学習前の `winsorize(p1-p99)` は `price_predictor._fit_final_model` 内で **全特徴量に一括適用** されるため、マクロ特徴量も自動で外れ値処理される
- `get_macro_features` が `None` を返したサンプル（未収集期間）は `_build_snapshots` 内の既存 None 除外ロジックで自動スキップ
- 多重共線性（SP500 と日経225 は高相関）は `check_collinearity`（`plugins/utils.py`）で事前確認推奨

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
