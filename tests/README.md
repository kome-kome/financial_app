# tests/

自動テスト一覧。CLAUDE.md「テスト方針」は Claude 自身による手動 Python 実行を主軸としているが、
本ディレクトリは回帰テストとして純関数レベルの動作を継続的に保証する。

## 実行方法

### Windows（プロジェクトの venv）
```powershell
.\venv\Scripts\python.exe -m pytest tests/ -v
```

### Linux / macOS / CI
```bash
python -m pytest tests/ -v
```

## ファイル構成

| ファイル | 対象 | 外部依存 |
|---|---|---|
| `test_utils.py` | `plugins/utils.py`（OLS・winsorize・kfold/walk-forward CV・統計診断） | numpy / scipy / statsmodels |

## 設計方針

- **純関数を優先してテスト対象に**: DB やネットワークに依存するコード（`api.py`/`collector.py`/`database.py`）は今のところテスト対象外。フィクスチャ整備を要する。
- **科学計算ライブラリは利用可**: VISION.md「サードパーティーライブラリ採用基準」に従い、numpy / scipy / statsmodels / scikit-learn は利用許可（requirements.txt 参照）。
- **回帰検出を優先**: 「OLS の数値安定性」「winsorize が p1-p99 を切る」等の CLAUDE.md に明記された制約を担保する。
- **scipy 参照値との一致検証**: p 値計算は `scipy.stats.t.sf` の値と一致することをテストで担保（旧 Pure Python 近似からの移行）。

## 将来の拡張候補

- `test_database.py`: SQLite in-memory で `upsert_financial`・`calc_growth_rates`・`calc_zscore_normalization` を検証
- `test_plugins.py`: 各プラグインの `params_schema` / `execute` の境界値チェック（モック DB）
- `test_api.py`: `httpx.AsyncClient` で主要エンドポイントの 200/401/404 を検証

これらは `docs/IMPROVEMENTS.md` で追跡する。
