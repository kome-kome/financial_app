# tests/

自動テスト一覧。CLAUDE.md「テスト方針」は Claude 自身による手動 Python 実行を主軸としているが、
本ディレクトリは回帰テストとして純関数レベルの動作を継続的に保証する。

## 実行方法

テスト実行には本番依存（numpy/scipy/statsmodels 等）に加え `pytest` が必要。
`pytest` は本番には載せないため `requirements-dev.txt` に分離している。

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

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
| `conftest.py` | 共通 fixture（in-memory SQLite の `db` セッション・データファクトリ `make_fin`/`make_company`/`make_price`） | sqlalchemy |
| `test_utils.py` | `plugins/utils.py`（OLS・winsorize・kfold/walk-forward CV・統計診断） | numpy / scipy / statsmodels |
| `test_net_cash_analysis.py` | `plugins/net_cash_analysis.py`（清原式ネットキャッシュ計算） | なし（純関数） |
| `test_recommend.py` | `plugins/recommend.py`（Zスコア重み付けスコアリング） | SQLite fixture |
| `test_gap_analysis.py` | `plugins/gap_analysis.py`（AR(1) 半減期推定・乖離分析） | statsmodels / SQLite fixture |
| `test_sector_ols.py` | `plugins/sector_ols.py`（業種別OLS回帰・予測値書き込み） | numpy / SQLite fixture |
| `test_total_return.py` | `plugins/total_return.py`（per-share OLS 総合リターン） | numpy / SQLite fixture |
| `test_price_predictor.py` | `plugins/price_predictor.py`（価格特徴量・N日先リターン予測） | numpy / SQLite fixture |
| `test_database.py` | `database.py`（pack/unpack・upsert_company・upsert_financial・年度別Zスコア） | SQLite fixture |
| `test_collector.py` | `collector.py`（XBRL パース・連結優先・派生指標 calc_derived・列検出・定数＋ネットワーク系：書類一覧/XBRL ZIP/stooq/J-Quants） | pandas / httpx MockTransport |
| `test_api.py` | `api.py`（JST変換・edinet_code 検証・トークン署名/検証・`/health`・DB-backed 読取エンドポイント） | fastapi TestClient |

## 設計方針

- **2 層構成**:
  - *純粋関数・定数テスト* — DB 不要。スコア式・AR(1) 推定・価格特徴量・MECE/次元整合性の定数を直接検証。
  - *execute() 挙動テスト* — `conftest.py` の in-memory SQLite fixture（`db`）に合成データを投入し、
    ランキング順・カバレッジフィルタ・予測値書き込み・`ValueError` ガード（空DB/サンプル不足）を検証。
- **本番コードは無改変**: プラグインのロジックを抽出・変更せず、`execute()` を `asyncio.run()` で直接呼ぶ
  （`execute()` は async だが内部に実 I/O await が無いため pytest-asyncio は不要）。
- **SQLite fixture の注意**: `init_db()` は Postgres 専用 SQL（gin / DOUBLE PRECISION）を含むため呼ばず、
  `Base.metadata.create_all()` でテーブル生成する。モデルは SQLite 互換の型のみ。
- **科学計算ライブラリは利用可**: VISION.md「サードパーティーライブラリ採用基準」に従い、numpy / scipy / statsmodels / scikit-learn は利用許可（requirements.txt 参照）。
- **回帰検出を優先**: 「OLS の数値安定性」「winsorize が p1-p99 を切る」等の CLAUDE.md に明記された制約を担保する。

## ネットワーク・DB のモック方針

- **HTTP**: `httpx` 組み込みの `MockTransport` でレスポンスを擬似（新規依存なし）。`client: httpx.AsyncClient`
  を引数に取る関数（`fetch_doc_list`/`fetch_xbrl_csv`/`fetch_stock_price_stooq`/`fetch_stock_history_stooq`/
  `_jquants_fetch_date`）にモック client を渡す。レート制限の `asyncio.sleep` は `monkeypatch` で無効化。
- **DB エンドポイント**: FastAPI `app.dependency_overrides[get_db]` を `conftest.py` の SQLite `db` に差し替え。
  TestClient は別スレッドで実行するため `db` fixture は `StaticPool`＋`check_same_thread=False`。
  `/health` は `SessionLocal` を直接呼ぶため `monkeypatch.setattr(api, "SessionLocal", ...)` で差し替える。

## カバレッジの現状と残課題

- **カバー済み**: プラグイン 7 個（utils 含む）、`database.py`（upsert・年度別Zスコア・pack/unpack）、
  `collector.py`（XBRL パース・派生指標などの純関数＋ネットワーク取得をモック）、
  `api.py`（純関数＋`/health`＋DB-backed 読取エンドポイント）。
- **未カバー（テストしにくい部分）**:
  - `database.py` の `calc_growth_rates` — PostgreSQL 専用 SQL（`LAG() OVER`・`::numeric`）で SQLite では検証不可。
  - `api.py` の SSE ストリーミング・バックグラウンドジョブを起動する重い収集/分析 POST。
  - `collector.py` の大きな統合フロー（`run_full_collection`/`update_market_data`/400日スキャンループ）と
    `update_industry_from_jpx`（JPX Excel 解析）。

これらは `docs/IMPROVEMENTS.md` で追跡する。
