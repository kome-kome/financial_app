# tests/

回帰テスト。CLAUDE.md「テスト方針」のとおり、実装後は Claude 自身がここを走らせてから報告する。
ファイルの一覧は書かない（本数は増え続け、書き写すと黙って古くなる）。何を照合するテストかは各ファイルの docstring が正本。

## 実行方法

テスト実行には本番依存（numpy/scipy/statsmodels 等）に加え `pytest` が必要。
`pytest` は本番には載せないため `requirements-dev.txt` に分離している。

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

### Windows（プロジェクトの venv）
```powershell
.\venv\Scripts\python.exe -m pytest
```

### Linux / macOS / CI
```bash
python -m pytest
```

- `pytest.ini` が `testpaths=tests` と `-v --tb=short` を固定しているので、引数なしで全件になる。
- CI（`.github/workflows/ci.yml`）は PR と main への push で `pytest --durations=20` を全件走らせる。docs だけの PR でも走る。
- **夜間・日中・月次のバッチが走っている間は、手元で全件を走らせず CI に任せる**（重い計算と並走すると所要の上限で偽陽性が出る。判定は [FUTURE_TASKS.md](../docs/FUTURE_TASKS.md)）。

## 設計方針

- **2 層構成**:
  - *純粋関数・定数テスト* — DB 不要。スコア式・AR(1) 推定・価格特徴量・MECE/次元整合性の定数を直接検証。
  - *execute() 挙動テスト* — `conftest.py` の in-memory SQLite fixture（`db`）に合成データを投入し、
    ランキング順・カバレッジフィルタ・予測値書き込み・`ValueError` ガード（空DB/サンプル不足）を検証。
- **プラグインの `execute()` は同期（`def`）**。本番と同じ単一入口 `plugins.execute_plugin(plugin, raw, db)` か `execute()` を直接呼ぶ（`async def` は禁止・理由は [GOTCHAS.md](../docs/GOTCHAS.md)・#357）。
- **SQLite fixture の注意**: `init_db()` は Postgres 専用 SQL（gin / DOUBLE PRECISION）を含むため呼ばず、
  `Base.metadata.create_all()` でテーブル生成する。`financial_metrics` は本番では VIEW だが、テストでは読み取りモデルの列定義からテーブルを作り、派生値を直接 INSERT する。
- **Postgres でしか再現しないもの**は `*_postgres.py` などが受け持ち、`FINAPP_TEST_PG_URL` が無ければ skip する（CI では skip）。
- **1本の上限は 60 秒**（`conftest.py` の `SLOW_TEST_BUDGET_S`・#703）。超えると失敗にする。意図的に重いテストは `@pytest.mark.slow(reason="...")`（reason 必須）。
- **副作用は fixture で構造的に遮断する**。週次株価キャッシュの書き先と、バッチの実行中マーカーは `autouse` の fixture が tmp へ向ける（monkeypatch を各テストに任せると1本の書き忘れで本物へ届く）。
- **「増やしたら登録表へ1行足す」の照合テスト**は CLAUDE.md の「ドキュメント更新ルール」の表が一覧の正本。
- **科学計算ライブラリは利用可**: VISION.md「サードパーティーライブラリ採用基準」に従い、numpy / scipy / statsmodels / scikit-learn は利用許可（requirements.txt 参照）。

## ネットワーク・DB のモック方針

- **HTTP**: `httpx` 組み込みの `MockTransport` でレスポンスを擬似（新規依存なし）。`client: httpx.AsyncClient`
  を引数に取る関数（`fetch_doc_list`/`fetch_xbrl_csv`/
  `_jquants_fetch_date`）にモック client を渡す。レート制限の `asyncio.sleep` は `monkeypatch` で無効化。
- **DB エンドポイント**: FastAPI `app.dependency_overrides[get_db]` を `conftest.py` の SQLite `db` に差し替え。
  TestClient は別スレッドで実行するため `db` fixture は `StaticPool`＋`check_same_thread=False`。
  `/health` は `SessionLocal` を直接呼ぶため `monkeypatch.setattr(api, "SessionLocal", ...)` で差し替える。

## テストしにくい部分

- `financial_metrics` VIEW（`LAG() OVER`/`::numeric` を使う PostgreSQL 専用 VIEW）は SQLite では張れない。実 PG での確認は `*_postgres.py` が受け持つ。
- `api.py` の SSE ストリーミング・バックグラウンドジョブを起動する重い収集/分析 POST。
- `collector.py` の大きな統合フロー（`run_full_collection`/`update_market_data`/400日スキャンループ）。
  なお `update_industry_from_jpx` の Excel 解析は純粋関数 `_read_jpx_excel`、`fill_industry_from_edinet_codelist`（#784）のコードリスト解析は `_read_edinet_codelist` として抽出済みで、どちらも `tests/test_update_industry.py` がカバーする。

足りないテストは GitHub Issues に起票する（残タスクの正本は Issues）。
