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

SSEエンドポイント: 収集=`/api/collect/stream`、市場データ=`/api/collect/market-stream`

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
- **税前利益（税金等調整前当期純利益）の要素名（実証済み・2026-06-13修正）**: PL段階のうち税前利益だけが本番20,513件中 **99.8% NULL** だった。原因は登録タグ誤り — JGAAPの正しい要素は **`IncomeBeforeIncomeTaxes`**（旧 `ProfitLossBeforeIncomeTaxes` は実在せずほぼ1件も一致しなかった）、IFRSは **`ProfitLossBeforeTaxIFRS`**（＋「経営指標等」用 `ProfitLossBeforeTaxIFRSSummaryOfBusinessResults`。旧 `ProfitLossBeforeIncomeTaxesIFRS` も誤り）。隣接段階（経常 `OrdinaryIncome`・純利益 `ProfitLoss`/`NetIncomeLoss`）は100%取れており税前タグのみの不一致だった（実ファイル S100Y8UO 等で要素名を実証）。`database.py` の `pl_pretax_profit` 列 info を修正（`XBRL_MAP` は `build_xbrl_map` で自動反映）。**既収集レコードの是正は別途バックフィルが必要**: `raw_xbrl_json` はマップ済み値のみ保存し生タグを残さないため、保存済み doc_id からの再フェッチでしか埋まらない（`xbrl_raw_documents` は容量制約で本番0行＝`reparse_from_raw` 不可）。専用関数 **`refill_pl_bs_from_xbrl`（CLI: `python collector.py --refill-pl-bs`）** を用意（`refill_cf_from_xbrl` 同型）。駆動マーカー=`pl_pretax_profit IS NULL`＋`doc_id`、再取得した同一ファイルから **NULL の PL/BS 列のみ補完**（既存値は上書きしない）・税前が埋まると対象外になり繰り返しで自然終了。CF は `refill_cf_from_xbrl` が担当。
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
- **CF NULL補完の運用**: `refill_cf_from_xbrl` には3モードがある。
  - `normal`（既定）: `cf_net_change_cash IS NULL AND cf_operating_cf IS NOT NULL` を対象（投資CF/現金増減/capex を補完）。`refill-cf.yml` の通常補完は 2026-05-31 に remaining=0。
  - `capex_only`（`--refill-capex-only`）: capex のみワンショット補完。
  - **`missing`（`--refill-cf-missing`）**: `cf_operating_cf IS NULL`（＝CFが全NULL）を対象。IFRS/US-GAAP決算の大企業は営業CFすら取れておらず、`normal`/`capex_only` が `cf_operating_cf IS NOT NULL` を前提とするため**永久に対象外**になっていた。`XBRL_MAP` への IFRS/US-GAAP CF 要素追加と併せて 2026-06-03 に補完し、**CF未収集企業 268社 → 0社**（上記「IFRS/US-GAAP決算のCF・売上要素名」参照）。
  - **注意**: 旧「remaining=0 で完了」は `normal` モードの残件のみを数えており、CF全NULL社（IFRS大企業）はカウント外だった。新規データで CF が全NULL のレコードが出た場合は `--refill-cf-missing` を使うこと。
- **`edinet_ping.py` の日付**は自動計算（祝日は非対応、祝日前後は失敗する場合あり）。

---

## DB・運用上の注意

- **URLとHTMLファイル名の対応**を崩さない: `/` ↔ `dashboard.html`、`/collection` ↔ `collection.html`、`/analysis` ↔ `analysis.html`。
- **`CollectionLog.status`** の値: `running` / `done` / `error` / `resolved`（修正済みエラー）。UIは `resolved` を緑扱い。
- **.env は UTF-8（BOMなし）で保存すること**。BOM付きだと最初のキーが読み込めずAPIキーが空になる。
- 本番運用前に `APP_PASSWORD`・`APP_SECRET_KEY`・`APP_RECOVERY_KEY` を必ず設定する。
- **株価は close-only の2本立て**（`stock_price_daily`＝直近6か月日次 / `stock_price_weekly`＝全履歴週次）。価格の読み書きは必ず単一ヘルパ経由：書き込み＝`record_prices_batch`（daily upsert→週次再集約→trim）、エントリー価格＝`prices_on_or_after`（窓内daily・古ければweekly・daily空ならweeklyフォールバック）、最新値＝`latest_prices`（daily優先）。VWAP/相対流動性は `turnover_sum/volume_sum` から派生（保存しない）。
- **満杯DBでの株価移行の罠**：旧 `stock_price_history`（≈359MB）と新2テーブルを併存させると 448MB→553MB で **500MB 超＝read-only 墜落**。`DELETE`/`ALTER DROP COLUMN` はファイルを縮めず（解放は `VACUUM FULL` 必要だが満杯では実行不可）。→ `migrate_stock_price_dual.py` は **ローカルで集約計算 → 旧 `DROP TABLE`（即解放）→ コンパクトな新テーブルをアップロード** の順で Supabase ピークを現状から上げない。退避 dump は照合完了まで保持（再投入元）。

---

## 認証・セキュリティ実装メモ

- **【実装済み（Tier3-3）】** 認証を HttpOnly Cookie 方式へ移行（`localStorage` 廃止＝XSS によるトークン盗難を防止）。`auth_token`（HttpOnly）＋`csrf_token`（JS可読）の2 Cookie、`SameSite=Lax`、本番は `COOKIE_SECURE=true` で Secure 属性。`Authorization: Bearer` は廃止。
- **【実装済み（Tier3-3）】** 非冪等メソッド（POST/PUT/DELETE/PATCH）に CSRF Double-Submit（`X-CSRF-Token` ヘッダ == `csrf_token` Cookie）を要求。`/api/auth/` 配下は免除（ログイン前のため）。フロントは各 `apiFetch` が `csrf_token` Cookie を読みヘッダ付与。ログアウトは `POST /api/auth/logout` で Cookie 削除。
- **【実装済み（Tier3-1）】** 重い処理（収集・分析）と認証に `slowapi` でレート制限を導入（収集 3/分・分析 20/分・ログイン 10/分・リセット 3/分・単一更新 10/分）。IP単位（`get_remote_address`）。`APP_RATELIMIT_ENABLED=false` で無効化可能（テスト時等）。環境変数名は slowapi 予約キー `RATELIMIT_*` との衝突を避けるため `APP_` 接頭辞必須。
- **【実装済み（Tier3-2）】** CSP の `script-src` から `'unsafe-inline'` を除去済み（インライン `<script>` を `static/js/` へ外部化＋インラインイベントハンドラを `data-*` 属性＋イベント委譲へ移行）。`style-src 'unsafe-inline'` はインライン `<style>`/`style=` 属性が残るため維持（script-src より低リスク）。

---

## 分析・計算の実装メモ

- **成長率計算は (edinet_code, year, period_end) で副ソート**済み。同年複数レコードがある企業の前期比が不定にならないようにしている。
- **フリーCF = 営業CF + 投資CF**（設備投資以外の投資活動も含む近似値）。
- 分析モデルの理論・次元整合性・外れ値処理（winsorize）・株数推計・Zスコア年度別計算の詳細は [MODELS.md](MODELS.md) を参照。
