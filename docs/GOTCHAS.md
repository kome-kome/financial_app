# 既知のハマりどころ・実装上の注意

収集・分析を実装するときに踏みやすい落とし穴と、その対処を集約する。CLAUDE.md からリンクされる索引先。分析モデルの理論的制約は [MODELS.md](MODELS.md) を参照。

---

## 収集フロー別進捗仕様

長時間処理はすべてリアルタイム進捗を UI に届けること。`on_progress(current, total, message)` コールバックで SSE に流す。

| フェーズ | 進捗メッセージ形式 |
|---|---|
| 企業マスタ保存 | `[企業マスタ保存] X/Y社完了` |
| 書類一覧スキャン | `[書類スキャン X/Y日] YYYY-MM-DD  累計 Z社` |
| XBRL取得・保存 | `[X/Y] 企業名(証券コード) 決算期末` |
| 差分スキップ | `[X/Y] スキップ（収集済み）: 企業名 決算期末` |

SSEエンドポイント（進捗のリアルタイム配信・全6本）: 収集=`/api/collect/stream`、市場データ=`/api/collect/market-stream`、株価履歴=`/api/collect/history/stream`、再解析=`/api/collect/reparse/stream`、J-Quants=`/api/collect/jquants/stream`、マクロ=`/api/collect/macro/stream`（一覧の正本は ARCHITECTURE.md §8）

---

## 業種データの取得方法

- **業種はXBRLから取得できない**。EDINETのXBRLに TSE 33業種コードは含まれていない。
- **正規ソース**: JPX上場会社一覧Excel（`JPX_EXCEL_URL` = `data_j.xls`、33業種コード列=col4/col5）
- `update_industry_from_jpx(client, db)` が `run_full_collection` の末尾で自動実行される。
- 証券コードは4桁数字（`1301`）とアルファベット混在（`350A`）の両形式に対応済み。
- **xlrd は .xls 専用**（`xlrd==2.0.2` は `.xlsx` を読めず `XLRDError` を送出）。JPX が将来 .xlsx に切り替えた場合の fallback として `openpyxl` による再読み込みを `update_industry_from_jpx` に実装済み（`xlrd.XLRDError` を捕捉 → `openpyxl.load_workbook` でリトライ）。

---

## EDINET / XBRL の取り扱い

- **EDINET XBRL CSV** は UTF-8 と UTF-16 LE（タブ区切り）が混在。`fetch_xbrl_csv` で両方対応済み。
- **XBRL要素選択**: 連結優先判定は `"NonConsolidated" not in ctx` を必ず含めること。優先度: 連結=2 > 非メンバー=1 > メンバー付き=0。
- **CF要素名・XBRL ZIP 構造**: EDINET XBRL type=5 ZIP には**複数の CSV ファイル**が含まれる。CF合計は概要ファイルに、CF明細は別ファイルに存在。ZIP内の全CSVを concat して parse する（`fetch_xbrl_csv`）。投資CFのEDINET標準要素は `NetCashProvidedByUsedInInvestmentActivities`（Investment、旧 Investing は誤り）。
- **税前利益（税金等調整前当期純利益）の要素名（実証済み・2026-06-13修正）**: PL段階のうち税前利益だけが本番20,513件中 **99.8% NULL** だった。原因は登録タグ誤り — JGAAPの正しい要素は **`IncomeBeforeIncomeTaxes`**（旧 `ProfitLossBeforeIncomeTaxes` は実在せずほぼ1件も一致しなかった）、IFRSは **`ProfitLossBeforeTaxIFRS`**（＋「経営指標等」用 `ProfitLossBeforeTaxIFRSSummaryOfBusinessResults`。旧 `ProfitLossBeforeIncomeTaxesIFRS` も誤り）。隣接段階（経常 `OrdinaryIncome`・純利益 `ProfitLoss`/`NetIncomeLoss`）は100%取れており税前タグのみの不一致だった（実ファイル S100Y8UO 等で要素名を実証）。`database.py` の `pl_pretax_profit` 列 info を修正（`XBRL_MAP` は `build_xbrl_map` で自動反映）。**既収集レコードの是正は別途バックフィルが必要**: 生タグはDBに保持しない（`xbrl_raw_documents` は容量制約で本番0行）ため、保存済み doc_id からの再フェッチでしか埋まらない（`reparse_from_raw` 不可）。専用関数 **`refill_pl_bs_from_xbrl`（CLI: `python collector.py --refill-pl-bs`）** を用意（`refill_cf_from_xbrl` 同型）。駆動マーカー=`pl_pretax_profit IS NULL`＋`doc_id`、再取得した同一ファイルから **NULL の PL/BS 列のみ補完**（既存値は上書きしない）・税前が埋まると対象外になり繰り返しで自然終了。CF は `refill_cf_from_xbrl` が担当。
- **棚卸資産（bs_inventory）の XBRL 記載方式差異（2026-06-13 対応）**: `bs_inventory` が本番で **92% NULL** だった原因はタグ名エラーでなく、JGAAP ファイラーの大半が aggregate 要素 `Inventories` を出力せず**サブ項目のみ**（`MerchandiseAndFinishedGoods`・`WorkInProcess`・`RawMaterials`/`RawMaterialsAndSupplies` 等）を出力するため。8% の会社（六甲バター等）は `jppfs_cor:Inventories` を直接出力しており正常取得できていた。**解決策**: `parse_xbrl_csv` / `parse_raw_rows` に `_inventory_fallback` を追加 — `Inventories` が取れなかった場合にサブ項目を定義済みグループ（`_INVENTORY_GROUPS`）の precedence 順に合計して `bs["inventory"]` に設定する。二重計上防止: グループ内で上位要素（`MerchandiseAndFinishedGoods`）が取れたら下位（`Merchandise`/`FinishedGoods`）をスキップ。既存の連結優先・Priorスキップロジックを踏襲。これにより新規収集分からサブ項目フォールバックが機能する。**既存レコードの是正**: `python collector.py --refill-pl-bs` を再実行すると NULL の `bs_inventory` が埋まる。
- **IFRS/US-GAAP決算のCF・売上要素名（重要・実証済み）**: IFRS採用企業（トヨタ・ホンダ・クボタ等）のCF合計は **`NetCashProvidedByUsedInOperatingActivitiesIFRS`**（=`NetCash...IFRS` 系。`CashFlowsFromUsedIn...IFRS` ではない）でタグ付けされる。さらに全有報に必ず存在する「主要な経営指標等の推移」テーブルに **`CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults`** 系（営業/投資/財務）があり、本体が独自拡張要素の企業の保険として両系統を `XBRL_MAP` に登録している。当期は `context=CurrentYearDuration`（`Consolidated` を含まないが Prior 除外フィルタは通る）。IFRSのcapexは `PurchaseOfPropertyPlantAndEquipmentInvCFIFRS` 等の独自要素だが既存のラベル照合（「有形固定資産の取得による支出」）で捕捉される。
  - **US-GAAP採用企業**（キヤノン・コマツ・オリックス・野村HD・ソニー旧年度等）も同型で、CF合計・**連結売上**・純利益・総資産・純資産・EPS/BPS が `...USGAAPSummaryOfBusinessResults`（`CashFlowsFromUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults` 系・`RevenuesUSGAAPSummaryOfBusinessResults`・`NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults`・`TotalAssetsUSGAAPSummaryOfBusinessResults`・`EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults`・`BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults` 等）に集約される（営業利益は経営指標等に存在せず取得不可）。**注意**: これら未登録時は非連結 `NetSales`（メンバー・優先度0）を誤採用し連結値が大幅過小/欠損になっていた（例: キヤノン 連結4.6兆を1.8兆と誤記）。
  - **IFRSで「売上高(NetSales)」を使う企業**（ソニー等）の連結売上は `NetSalesIFRS`（「売上収益」の `RevenueIFRS` ではない）。これも未登録時は非連結 `NetSales` に負けていた。
  - **自己資本の注意**: US-GAAP社は「株主資本」(`EquityAttributableToOwnersOfParentUSGAAP…`)と「純資産額」(`EquityIncludingPortion…NCI…`)の**どちらか一方しか載らない**企業がある（キヤノン=株主資本のみ／野村=純資産額のみ）。両方を `total_equity` に登録し片方欠落でも連結自己資本が埋まるようにした（同優先度は先勝ち＝株主資本利益率に整合）。未対応だとキヤノンの `total_equity` が非連結のまま残り ROE が約2倍に誤算出されていた。
  - 既収集レコードは**保存済み doc_id からの再パース＋ upsert で是正済み**（2026-06-03、US-GAAP/移行組11社）。`upsert_financial` は市場データ列を保持するため CF専用 `--refill-cf-missing`（CF列のみ）とは別に PL/BS も含めて上書きできる。EPS/BPS 変更後の `per`/`pbr`/`market_cap` は保存列のため、保存済み株価＋新EPS/BPSで `_apply_price_to_record` により再計算した（次回の株価更新でも自動再計算される）。
- **「営業収益」型企業の売上（実証済み）**: 鉄道・電力・小売・不動産・サービス・倉庫・空運・証券等は「売上高(NetSales)」ではなく連結 **`OperatingRevenue1SummaryOfBusinessResults`「営業収益、経営指標等」**（context=CurrentYearDuration＝連結）で計上する。旧マップの `OperatingRevenues`（複数形）とは別綴り（単数+1）のため未取得だった。2026-06-03 に `XBRL_MAP` へ追加し、売上NULL社を **259→152** に補完（保存済み doc_id 再パース。107社が連結営業収益で埋まる）。
  - **連結のみ採用（重要）**: 生 `OperatingRevenue1`（PL本体・セグメント別）は登録せず、`OperatingRevenue1SummaryOfBusinessResults` のみを採用する。さらに parse で **`OperatingRevenue1` 系の売上は `NonConsolidated`/`_Member` コンテキストを除外**する（`parse_xbrl_csv`/`parse_raw_rows`）。金融持株会社（MUFG・みずほ・第一生命等）は連結営業収益を持たず**提出会社単体（NonConsolidatedMember）の営業収益**しか無く、これを採ると売上が単体値に過小化（例: MUFG 連結なし→単体1.3兆・純利益率144%）するため。連結を持つ非金融＋証券（大和証券・JR東日本は consolidated CurrentYearDuration を持つ）はそのまま採用される。
- **銀行・保険の売上は設計上 NULL（既知の制約）**: 銀行・保険・一部その他金融（残 売上NULL 約94社）は「売上高」概念がなく、有報が「一般企業の売上高に代えて経常収益を記載」と明記（銀行 `OrdinaryIncomeSummaryOfBusinessResults`「経常収益」／保険 `InsurancePremiumsAndOtherOIINS` 等）。経常収益を `revenue` に入れると利益率・業種OLS・スクリーニングで非金融と混ざり歪むため、**意図的に revenue へマップせず NULL のまま**にしている。※経常収益 `OrdinaryIncome…SummaryOfBusinessResults` と経常利益 `OrdinaryIncomeLoss…SummaryOfBusinessResults` は別物（後者＝profit）。
  - 非金融の残 売上NULL（約58社）は、持株会社が連結営業収益を経営指標等に出さない（例: 博報堂DY・日本郵政）／業種固有の収益要素／プレ収益バイオ（売上ゼロが正当）等。さらに別系統の既存issueとして「非NULLだが誤った売上（売上<純利益）」が数社（例: ローソン）残る。いずれも業種別収益要素の追加マッピングを要する別タスク。
- **capex（設備投資）はラベル照合で取得**: 設備投資のCF明細行は**企業独自の拡張要素ID**でタグ付けされることが多く、標準要素ID（`PurchaseOfPropertyPlantAndEquipment`）では捕捉できない（実証: 3,000件中0件）。EDINET CSV の**「項目名」列**で照合する（`_match_capex_by_label` / `CAPEX_LABEL_*` 定数）。「有形固定資産の取得による支出」等を捕捉し、売却収入・無形のみは除外。capex は支出＝負（`-abs(val)`）で統一。
- **`raw_xbrl_json` 列は削除済み（Issue #219 ①・容量削減）**: 旧列は `upsert_financial` が毎回 bs/pl/cf のマップ済み dict を `financial_records.raw_xbrl_json`（JSON 列）へ常時書き込んでいた（`SKIP_XBRL_RAW` が抑止するのは**別テーブル `xbrl_raw_documents`（生 CSV 行・TOAST で大容量）への書き込みのみ**で、本列はフラグに関係なく常に保存されていた）。名前と列コメント「デバッグ用」に反して、この dict は parse 済み値しか持たず生タグは残らないため reparse 用途にも使えず、読取箇所も無かった。`financial_records` 約73MBの主因（Supabase 500MB制約下の第2の容量レバー）だったため冪等DROPマイグレーション（`_DEBUG_ONLY_COLS`・`database.py`）で削除し、ヘッドルームを確保した。
- **CF NULL補完の運用**: `refill_cf_from_xbrl` には3モードがある。
  - `normal`（既定）: `cf_net_change_cash IS NULL AND cf_operating_cf IS NOT NULL` を対象（投資CF/現金増減/capex を補完）。`refill-cf.yml` の通常補完は 2026-05-31 に remaining=0。
  - `capex_only`（`--refill-capex-only`）: capex のみワンショット補完。
  - **`missing`（`--refill-cf-missing`）**: `cf_operating_cf IS NULL`（＝CFが全NULL）を対象。IFRS/US-GAAP決算の大企業は営業CFすら取れておらず、`normal`/`capex_only` が `cf_operating_cf IS NOT NULL` を前提とするため**永久に対象外**になっていた。`XBRL_MAP` への IFRS/US-GAAP CF 要素追加と併せて 2026-06-03 に補完し、**CF未収集企業 268社 → 0社**（上記「IFRS/US-GAAP決算のCF・売上要素名」参照）。
  - **注意**: 旧「remaining=0 で完了」は `normal` モードの残件のみを数えており、CF全NULL社（IFRS大企業）はカウント外だった。新規データで CF が全NULL のレコードが出た場合は `--refill-cf-missing` を使うこと。
- **bs_inventory バックフィルの運用**: `refill_pl_bs_from_xbrl`（`_pipeline_gh.py --refill-pl-bs` / GitHub Actions `refill-pl-bs.yml`）は `bs_inventory IS NULL AND doc_id IS NOT NULL` を駆動マーカーに、NULL の PL/BS 列を XBRL 再取得で補完する。
  - **原因はタグ漏れではなく時系列コホート**: パーサ修正（`_inventory_fallback`＝棚卸サブ項目合計）以降に収集した新しい年度（2023+）は null 率 ~3%（金融子会社等の正当な残差）まで下がっているが、修正前に収集した旧コホート（〜2022）が backfill 未実施で残存していた（2026-06-15 実測で旧年度 57〜94% null）。よって `XBRL_MAP` の追加は不要で、旧データの再取得で是正する。
  - **古い順（`order="asc"`）で処理**: NULL は旧コホートに集中するため、`_refill_records_from_xbrl` の `order` を `asc` にして古い年度から処理する。limit 付き／タイムアウト時も本命の旧年度に着実に前進・再開できる（新しい順だと直近の正当 NULL=金融等で limit を浪費し旧年度に届かない）。**全件バックフィルは limit 省略（None）で一括実行**するのが基本。
  - **金融等の正当 NULL は残る**: 銀行（~99%）・保険（~94%）・証券等は棚卸資産を持たないため何度実行しても埋まらず、永続的な少数残件として残る（無害）。`remaining` がこの水準で下げ止まったら完了とみなす。
- **`edinet_ping.py` の日付**は自動計算（祝日は非対応、祝日前後は失敗する場合あり）。

---

## DB・運用上の注意

- **URLとHTMLファイル名の対応**を崩さない: `/` ↔ `dashboard.html`、`/collection` ↔ `collection.html`、`/analysis` ↔ `analysis.html`。
- **`CollectionLog.status`** の値: `running` / `done` / `error` / `resolved`（修正済みエラー）。UIは `resolved` を緑扱い。
- **.env は UTF-8（BOMなし）で保存すること**。BOM付きだと最初のキーが読み込めずAPIキーが空になる。
- 本番運用前に `APP_PASSWORD`・`APP_SECRET_KEY`・`APP_RECOVERY_KEY` を必ず設定する。
- **株価は close-only の2本立て**（`stock_price_daily`＝直近6か月日次 / `stock_price_weekly`＝全履歴週次）。価格の読み書きは必ず単一ヘルパ経由：書き込み＝`record_prices_batch`（daily upsert→週次再集約→trim）、エントリー価格＝`prices_on_or_after`（窓内daily・古ければweekly・daily空ならweeklyフォールバック）、最新値＝`latest_prices`（daily優先）。VWAP/相対流動性は `turnover_sum/volume_sum` から派生（保存しない）。
- **満杯DBでの株価移行の罠**：旧 `stock_price_history`（≈359MB）と新2テーブルを併存させると 448MB→553MB で **500MB 超＝read-only 墜落**。`DELETE`/`ALTER DROP COLUMN` はファイルを縮めず（解放は `VACUUM FULL` 必要だが満杯では実行不可）。→ `migrate_stock_price_dual.py`（2026-06 完了済みで撤去・以下は手順記録）は **ローカルで集約計算 → 旧 `DROP TABLE`（即解放）→ コンパクトな新テーブルをアップロード** の順で Supabase ピークを現状から上げない。退避 dump は照合完了まで保持（再投入元）。
- **長時間バッチはDBセッションを計算中に保持しない**（Issue #269）：`SessionLocal()` で開いたトランザクションは、直後のSELECTが終わっても明示的に `commit()`/`close()` するまでオープンのまま＝該当テーブルに `AccessShare` ロックが残り続け、別セッションの `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（`ACCESS EXCLUSIVE` 要求）等をブロックする。`macro_beta_inference.py` は数時間かかる MCMC 計算の前にデータ読込のみで `build_panel` を終える構造だったため、読込直後に `db.commit()` してロックを解放してから計算に入る（`persist()` は同じ db をコミット後に再利用＝`pool_pre_ping=True`・`pool_recycle=180` で長時間後の再利用も安全）。DB接続を使わない長時間処理を挟むバッチを書く際は同じ罠に注意。

---

## 認証・セキュリティ実装メモ

- **【実装済み（Tier3-3）】** 認証を HttpOnly Cookie 方式へ移行（`localStorage` 廃止＝XSS によるトークン盗難を防止）。`auth_token`（HttpOnly）＋`csrf_token`（JS可読）の2 Cookie、`SameSite=Lax`、本番は `COOKIE_SECURE=true` で Secure 属性。`Authorization: Bearer` は廃止。
- **【実装済み（Tier3-3）】** 非冪等メソッド（POST/PUT/DELETE/PATCH）に CSRF Double-Submit（`X-CSRF-Token` ヘッダ == `csrf_token` Cookie）を要求。`/api/auth/` 配下は免除（ログイン前のため）。フロントは各 `apiFetch` が `csrf_token` Cookie を読みヘッダ付与。ログアウトは `POST /api/auth/logout` で Cookie 削除。
- **【実装済み（Tier3-1）】** 重い処理（収集・分析）と認証に `slowapi` でレート制限を導入（収集 3/分・分析 20/分・ログイン 10/分・リセット 3/分・単一更新 10/分）。IP単位（`get_remote_address`）。`APP_RATELIMIT_ENABLED=false` で無効化可能（テスト時等）。環境変数名は slowapi 予約キー `RATELIMIT_*` との衝突を避けるため `APP_` 接頭辞必須。
- **【実装済み（Tier3-2）】** CSP の `script-src` から `'unsafe-inline'` を除去済み（インライン `<script>` を `static/js/` へ外部化＋インラインイベントハンドラを `data-*` 属性＋イベント委譲へ移行）。`style-src 'unsafe-inline'` はインライン `<style>`/`style=` 属性が残るため維持（script-src より低リスク）。
- **【実装済み・#281】httpx の INFO ログが APIキーをそのまま露出する**: `collector_utils.py` の `logging.basicConfig(level=logging.INFO, ...)` が root ロガーを INFO にするため、httpx が出す `"HTTP Request: GET <url> ..."` ログにクエリパラメータの `api_key`/`appId`（FRED_API_KEY・ESTAT_API_KEY）がそのまま記録される。`fetch_fred_series` の例外ハンドラは「レスポンスをそのまま出すと鍵が漏洩する」ことを認識し `status_code` のみログしていたが、httpx 自身が出す**正常系**のリクエストログは対象外だった＝ローカル実行はもちろん GitHub Actions（`collect-macro.yml`）のログにも同様に記録され続けていた抜け穴。`collector_utils.py` で `httpx`/`httpcore` ロガーを `WARNING` に引き上げて解消。新しい外部APIキーを URL クエリで渡す実装を追加する際は、この抑制がまだ効いているか（ロガー名を変えていないか）を確認すること。

---

## 分析・計算の実装メモ

- **成長率計算は (edinet_code, year, period_end) で副ソート**済み。同年複数レコードがある企業の前期比が不定にならないようにしている。
- **フリーCF = 営業CF + 投資CF**（設備投資以外の投資活動も含む近似値）。
- 分析モデルの理論・次元整合性・外れ値処理（winsorize）・株数推計・Zスコア年度別計算の詳細は [MODELS.md](MODELS.md) を参照。

---

## マクロ指標（FRED 低頻度系列・データソース）

- **FRED 低頻度系列は公表ラグ分シフトして格納する（先読みバイアス防止・#250）**: FRED は観測値を「期の参照開始日」の日付で返す。実体経済指標（GDP・鉱工業生産・失業率・貿易収支）は公表ラグが大きい（四半期≈期末+1.5〜2か月、月次≈+1か月）ため、`obs_date` をそのまま `trade_date` にすると、まだ公表されていない値をスナップショット（`trade_date <= ref_date` で突合）が「見て」しまう。`FRED_SERIES` の各系列に `lag_days`（四半期135日・月次60日）を持たせ、`fetch_fred_series` が収集時に `trade_date = obs_date + lag_days` へシフトして「この日には知れた値」へ正規化する。既存5系列は `lag_days` 未指定＝0 で後方互換。
- **FRED の OECD 旧系列は凍結（更新停止）があり、採用前に最終更新日を必ず確認する**: 例 月次CPI `CPALTT01JPM657N`（2021/6 で停止）・M2 `MYAGM2JPM189S`（2017/2 で停止）。#250 で採用した4系列のうち `JPNPROINDMISMEI`（鉱工業生産）は **2024-04-30 で凍結が確認された（#253）**。**e-Stat「鉱工業指数」直接取得（`ESTAT_INDEX_SERIES`・`JP_IIP`/`JP_IIP_INVENTORY`）へ切替済み（#281）**。残存3系列（JPNRGDPEXP / LRUNTTTTJPM156S / XTNTVA01JPQ664S）は引き続き更新中を確認済み。CPI・M2・短観は e-Stat/日銀コネクタ済み（ADR-0006・#251）。GDP需要項目（個人消費・設備投資・住宅投資・公共投資）・在庫の需要側内訳は統計表が複雑（vintage別に大量のテーブルが存在）なため別Issueへ分離（#281調査時に判明）。
- **e-Stat「時系列データ」テーブルは経済指標ごとに `@time` のフォーマットが異なる**: CPI（statsDataId=0003427113）は `@time` が `"YYYY"+"00"+"MM"+"MM"` の自己記述コードで直接パース可能だが、鉱工業指数（`ESTAT_INDEX_SERIES`・statsDataId=0004052177等）は `@time` が `"0500100"` のような**連番コード**で年月を直接表現しない（コード自体に規則性があるように見えても統計表ごとに異なる可能性があり決め打ちパースは危険）。対応: `metaGetFlg="Y"` を付けて `getStatsData` を呼ぶと、同一レスポンスの `CLASS_INF.CLASS_OBJ`（`@id="time"`）に code→`"YYYYMM"` の対応表が同梱される（追加の `getMetaInfo` 呼び出し不要）。`fetch_estat_index_series` はこの対応表を使って変換し、対応表の名前が6桁数字（YYYYMM）でない行（「付加生産ウエイト」等のマスタ行）はスキップする。
- **鉱工業指数は基準改定のたびに `statsDataId` が別テーブルへ切り替わる**: 2010年基準（2008年1月〜・`open_date=2018-10-31` で更新停止済み＝既に凍結済みアーカイブ）→2015年基準（2013年1月〜）→2020年基準（2018年1月〜・`open_date=2026-05-19`＝現行）の3世代が e-Stat 上に共存する。`getStatsList` に `statsCode=00550300`（鉱工業指数の政府統計コード）で問い合わせても新旧3世代が混在して返るため、`STATISTICS_NAME` の基準年表記と `OPEN_DATE`（直近更新日）を必ず確認してから採用する。FRED 版 `JPNPROINDMISMEI` の凍結（#253）もこの基準改定に起因する可能性が高い。
- **日本の市場系インデックスは Yahoo の指数ティッカーが死んでいる**: 日10年金利 `^JGB` は上場廃止（HTTP 404）、TOPIX 指数 `^TPX` は 200 OK だが 0 件配信。**TOPIX は ETF 1306.T（NEXT FUNDS TOPIX）を代理に収集**（指数とほぼ同追従・#250）。日次の日本10年金利は信頼ソースが無く、M-3 の `dlm_jp10y` は月次 FRED `JP10Y_FRED` を据置（週次差分は多くの週でゼロ＝情報量に限界）。
- 四半期系列の zscore は5年で約20点＝閾値ぎりぎりのため、`collect_macro_data` は FRED の取得窓を `FRED_MIN_YEARS_BACK`（10年）まで広げて観測点を確保する。yoy の前年同期窓（±30日）は四半期間隔90日に対し狭く、一部スナップショットで GDP-yoy が None→除外されうる（min_coverage が処理・実害が出れば前年窓拡大を別Issue化）。
- **日銀 API エンドポイントの ADR との差分（ADR-0006）**: ADR-0006 は `api.boj.or.jp` と記したが、2026-02 公開の実エンドポイントは `https://www.stat-search.boj.or.jp/api/v1/getDataCode`。実装ではこちらを使用。
- **BOJ 短観の SURVEY_DATES フォーマット**: 月次系列（M2）は `YYYYMM`（例 `202501`）、四半期系列（短観）は `YYYYQQ`（`01`=Q1/4月, `02`=Q2/7月, `03`=Q3/10月, `04`=Q4/翌年1月）。`fetch_boj_series` は `freq` フィールドで分岐して正しいカレンダー日付へ変換。
- **e-Stat CPI cdCat01 コード（statsDataId=0003427113・2020年基準）**: `0001`=全国総合, `0161`=生鮮食品を除く総合（非季調コア）。季節調整済みコア `0902` との混同注意（BOJ が政策判断に使うのは非季調コア `JP_CPI_CORE`）。東京都区部は `cdArea=13A01`（表示名は「13100 東京都区部」だが実際の `@code` は `13A01`。旧 `13100` を指定すると `STATUS=1`「該当データなし」で0件）。
- **e-Stat CPI 年次行混入問題（解決済み #262・2026-07-02 実API検証済み）**: statsDataId=0003427113 は月次（1970年〜現在）と年度（会計年度）集計が同一テーブルに混在。原因は3つ重なっていた: (1) `cdTab`（表章項目=1:指数）未指定、(2) `lvTime`（時間軸レベル=4:月次）未指定、(3) `@time` のパースが先頭6文字を YYYYMM とみなしていたが実際のフォーマットは `"YYYY"+"00"+"MM"+"MM"`（月次・月を2回繰り返す。例 2024年12月=`"2024001212"`）で、年度行は `"YYYY"+"10"+"0000"`（例 `"2024100000"`）。先頭6文字方式だと年度行の `[4:6]="10"` を月10（10月）と誤読し「10月のみ10件」に見えていた。**fix**: `cdTab=1` と `lvTime=4` を両方指定し、月は `@time[8:10]`（末尾2文字）から取り出す。`lvTime` 単体（#256）が失敗したのは `cdTab` 未指定のままだったため＝両方揃って初めて機能する。修正後は3系列とも月次125〜126行（2016-01〜2026-05）を実データで確認。FRED代替は不採用のまま：core-CPI 相当の `JPNCPICORMINMEI`/`JPNCPICORQINMEI`（ex food&energy）は2021-06で更新停止しておりそもそも「生鮮食品を除く総合」とは定義も異なる。

---

## フロントエンド（Chart.js・静的アセット）

- **Chart.js 混在チャートの x 軸は `type:'linear'` を明示する（重要・実証済み 2026-06-20）**: `line` データセットを含むと Chart.js は x 軸を**既定で `category` スケール**にし、数値の `min`/`max` とデータのクランプを**無視**して点を等間隔インデックス配置する（y は既定 `linear` なので効く）。症状は「**Y 軸のレンジ設定は効くのに X 軸だけ外れ値で潰れる**」非対称。散布図（bubble）＋フロンティア（line）のような混在チャートでは `scales.x.type='linear'`（必要なら y も）を必ず指定する。M-1 のリスク-リターン散布図がこれで何度も X 軸潰れを起こした。
- **静的 JS/CSS の更新がブラウザに反映されない（キャッシュ）**: `StaticFiles` は既定で `Cache-Control` を付けず、ブラウザが旧アセットを再検証せず使い続ける。`api.py` は `/static` を `_RevalidateStaticFiles`（`Cache-Control: no-cache`＝ETag 再検証強制）でマウント済み。テンプレートの `<script src=...?v=YYYYMMDD>` 版クエリは、no-cache ヘッダが未反映の既存キャッシュを一度で破棄するための保険。**HTML 自体は各ルートが `_NO_CACHE`（no-store）で配信**されるため版クエリの変更は即時届く。切り分けは DevTools→Network で `analysis.js` の Status（200＝新規取得／`(memory/disk cache)`＝キャッシュ）を見る。
- **径エンコードは値が分散する指標にのみ使う**: M-1 で R1（予測標準誤差）はレバレッジがほぼ一定で銘柄間差がほぼ無いため、バブル径への R1 マッピングは退化して全点が最小径になっていた。径は固定にし R1 はツールチップへ。同根の現象が μ 収縮（`w=R1/R1_max≈1`）でも起きる（[MODELS.md](MODELS.md) §9.6）。
