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

SSEエンドポイント（進捗のリアルタイム配信・全5本）: 収集=`/api/collect/stream`、市場データ=`/api/collect/market-stream`、再解析=`/api/collect/reparse/stream`、J-Quants=`/api/collect/jquants/stream`、マクロ=`/api/collect/macro/stream`（一覧の正本は ARCHITECTURE.md §8）

---

## 業種データの取得方法

- **業種はXBRLから取得できない**。EDINETのXBRLに TSE 33業種コードは含まれていない。
- **正規ソース**: JPX上場会社一覧Excel（`data_j.xlsx`）。列は**見出しの名前で引く**（「コード」「33業種区分」・`collector_utils.JPX_INDUSTRY_COLUMNS`・#1004）。以前は列の位置（コード=2列目・業種=6列目）で読んでいて、JPX が列を足すと例外も出ずに別の列を業種として読む形だった。見出しが見つからなければ `JpxIndustryError`（位置へは倒さない）。JPX に載らない社の空欄だけ EDINET コードリストの「提出者業種」で補う（#784・上場区分によらない #797・下記）。
- **同じダウンロードから上場銘柄一覧の月次断面を積む（#1004）**: 「市場・商品区分」「規模コード」「規模区分」も見出し名で引き、`jpx_listing_snapshots` へ時点（「日付」列・月末）ごとに積む。JPX は過去版を配らないので、取り逃した月は永久に欠ける。書く前の健全性（行数 3,000 以上・規模区分の語彙外 1% 以下・'-' 以外 900〜2,300 件・日付が1種類）が外れたら書かずに WARNING——**業種の更新は止めない**（断面が前進しないことは `batch_freshness.PRODUCERS` が現す）。同じ時点は上書きせず `last_seen_at` だけ進める。可視判定は `first_seen_at`（`database.jpx_listing_snapshot_at`）で、`as_of_date` で選ぶと公表遅れ（月末の版が翌月の初めに出る）の分だけ先読みになる。
- **`companies.market` は最新の断面から毎晩埋める「最新の表示用」（#1015）**: 長く書き手がゼロで全社 NULL のまま、市場で絞ると黙って 0 件になっていた。「プライム（内国株式）」等を別名表（`collector_utils.JPX_MARKET_ALIASES`・外国株式も同じ市場名・PRO Market はそのまま）で短い名前にし、変わった社だけ一括更新する。ETF・REIT・出資証券は市場ではない既知の区分として空、表に無い区分は WARNING で数えて書かない、断面に載らない社は触らない。**年度の行（`financial_records.market`・VIEW の `market`）は埋めない**——過去の年度に現在の市場を付けると先読みになる。スクリーニングの市場の絞り込みは `companies.market` を見る。時点が要る用途は `jpx_listing_snapshots` を使う。実物（2026-09-30 版）で上場中の社の市場別件数は data_j と ±1% 以内（新規上場で企業表に未登録の社の分だけ少ない）。
- `update_industry_from_jpx(client, db)` → `fill_industry_from_edinet_codelist(client, db)` の順に `run_full_collection` の末尾（Phase 5）で自動実行される。
- 証券コードは4桁数字（`1301`）とアルファベット混在（`350A`）の両形式に対応済み。
- **URL は一覧ページから解決する。定数で持つと変わった晩から静かに 404 になる（#632・2026-09-03）**: JPX は `data_j.xls` を `data_j.xlsx` へ切り替えた。ディレクトリ（`tvdivq0000001vg2-att`）は変わっておらず、変わったのは拡張子だけ。6晩連続で 404 になったが、`update_industry_from_jpx` が `except Exception: return 0, 0` で握っていたため **WARNING 止まりで `exit=0`**、足跡（`nightly_last_run`）も入るので watchdog（#515）にも引っかからなかった。既存の業種は DB に残るので画面も壊れず（実測で業種が空なのは 3,725社中2社）、現れるのは「新規上場社の業種が入らない」という静かな陳腐化だけ。解決は `resolve_jpx_excel_url()` が `JPX_LISTING_URL`（一覧ページ）から `href="...data_j.xls|xlsx"` を拾う。ページを読めない／リンクが無いときは `JPX_EXCEL_URL` へ倒す（解決の失敗を致命傷にしない）。
- **xlrd は .xls 専用**（`xlrd==2.0.2` は `.xlsx` を読めず `XLRDError` を送出）。`openpyxl` へのフォールバックが `_read_jpx_excel` にある。
- **その fallback は「動く」のに 93% を落としていた（#632）**: **xlrd は数値セルを `float` で返すが、openpyxl は `int` で返す**。コード列の判定が `float` / `str` しか受けていなかったため、xlsx 経路では数値コードの社が全部 `continue` に落ちた。実測（2026-09-08）で、業種を持つ 3,899行のうち拾えたのは **293件**——`130A` のような英字混じり（文字列で返る新形式）だけ。**件数が減るだけで例外は出ない**ので、URL だけ直すと「JPX業種マップ: 293件」と INFO を出して正常終了する＝404 という分かりやすい症状が消えて欠落だけが残る。対処は2つ:
  - `_read_jpx_excel` は **業種は埋まっているのにコード列を解釈できなかった行を数え**、`JPX_CODE_DROP_LIMIT`（5%）を超えたら `JpxIndustryError` を送出する。**正常なファイルではこの数は 0** なので閾値をどこに置いても誤検知しない（今回の壊れ方は 3,606/3,899＝一桁違い）。`bool` は `int` の派生なので判定から除く（`True` が `"0001"` に化ける）。
  - 取得・解釈の失敗は `JpxIndustryError` として型にする（`EdinetAccessError` と同じ役どころ＝「変化が無かった」と「取れなかった」を分ける）。
- **失敗の現れ方は watchdog の producer 側（#632）**: 成功時だけ `app_settings.jpx_industry_last_success` を書き、`batch_freshness.PRODUCERS` がその鮮度を見る（閾値は `cadence 24h + run_nightly.WINDOW_MIN`）。**`companies.industry` の中身は見ない**——既存値は取得が止まっても残るので「更新できているか」の証拠にならない。**Phase 5 で例外を送出してはいけない**——`run_full_collection` は `_pipeline_incremental._run_with_retry` の内側にあり、最後の1歩で raise すると XBRL 差分収集を丸ごと retry させる。捕捉して収集は継続し、失敗は足跡が進まないことで翌日以降に現れる。手動経路（`POST /api/collect/industry`）だけは 502 で即返す（「0件更新」と区別する）。
- **JPX に載らない社の空欄は EDINET コードリストで埋める（#784・2026-10-03／上場区分によらない #797）**: JPX の一覧は東証銘柄だけなので、札証・福証の単独上場37社・JPX の一覧で業種が付かない社（信金中央金庫 8421 の優先出資証券）・JPX 未収載の新規上場は業種が空のまま `sector_ols._eligible_base`（`if not r.industry`）で回帰から外れ、`gap_ratio` が付かなかった（例外も警告も出ない）。`fill_industry_from_edinet_codelist` が JPX の直後（Phase 5）に `EDINET_CODELIST_URL`（`Edinetcode.zip`・日次更新・API キー不要・cp932 の CSV。1行目はメタ行）の「提出者業種」で埋める。守っている約束は4つ:
  - **JPX を上書きしない（空欄だけ埋める）**: JPX で業種が付いている 3,753社を突き合わせると **174社（4.6%）で EDINET の分類が食い違う**（2026-10-02 実測・例: JPX サービス業／EDINET 情報・通信業）。正本は JPX。
  - **上場区分によらない（#797・[ADR-0065](adr/0065-empty-industry-is-filled-regardless-of-listing.md)）**: #784 は上場の 108社（地方37・現役3・DB 上は廃止の68）だけを埋め、残る 587社（非上場・区分空欄＝すべて `is_active=False`）を「過去年度が広く動く」として外した。だが**業種が空の社は「今日の JPX 一覧に載っていない＝その後に廃止した社」とほぼ同じ集合**で、空文字のまま残すと過去の断面で未来情報の疑似業種になる（2019年度の通期行の 17%＝657/3,788 がこの1群）。効いていたのは業種内Z（`z_*_sec`＝M-2/M-6 の既定特徴量）・M-1 系の「不明」カテゴリ（R3 の業種バケット・業種 target encoding・業種中立 rank-IC）・時点再現の gap パネル（`_eligible_base` で落ちる＝生存者バイアス）。埋めるのは提出者業種が33業種名の **517社**（非上場516・区分空欄1）。**埋めて OOF の rank-IC が下がっても「リークが抜けた」と読み、採否の条件にしない**。
  - **33業種外の 70社は空のまま残る**: `内国法人・組合（有価証券報告書等の提出義務者以外）` 69社・`外国法人・組合` 1社（約370行）。業種ではなく提出者の種別なので埋める先が無い。この小さな群は今も疑似業種で、年によっては1社になり業種内Z が NULL になる（その行は M-2/M-6 のパネルから落ちる）。
  - **EDINET の分類は廃止時点の JPX の分類と一致する保証が無い**: 現役社で 4.6% 食い違う（上記）ので、廃止社にも同じ程度の食い違いはありうる。正本の JPX が無い社に限った次善の値として扱う。
  - **許す業種名は DB に既にある名前（＝JPX が書いた名前）だけ。33業種名をコードへ写さない**: 削除した旧 `TSE_INDUSTRY`（XBRL の到達不能な分岐でだけ使われていた）は「証券、商品先物取引業」を「証券・」と書いており、写しは黙って陳腐化していた。表記差は実測で1つだけ（EDINET `倉庫・運輸関連` → JPX `倉庫・運輸関連業`）で `EDINET_INDUSTRY_ALIASES` が吸収する。一致しない**上場社**の名前は書かずに WARNING で数える（書くと業種別回帰に1社だけの業種ができる・表記差か分類体系の変更の疑い）。**非上場社**の33業種外の値（上の70社）は想定内なので INFO で数える（WARNING にすると毎晩70件が鳴り、本物の表記差が埋もれる）。
  - **選び方と書き込みは関数を分けてある**（`_plan_industry_fill` / `_apply_industry_fill`）: 影響の計測（`scripts.measure_industry_fill_impact`）が本番と同じ選び方で模擬補完するため。
  - **会社の業種を空の財務行へ毎晩写す（`_propagate_company_industry`）**: 収集が財務行の業種を空文字で書き直すため、一度埋めるだけでは直らない——差分収集（Phase 4）は新しい行を `xbrl_industry or master_industry`（どちらも常に空）で作り、`refresh_company`・半期（H1）収集は既存行を空で上書きする。JPX は証券コードで財務行も直すが、JPX に載らない社の行は誰も直さない。実測で TOB 等で JPX から外れた現役13社の 2026 年 H1 行（9/15 の日中収集で作成）が、会社側に業種があるのに空のままだった。写しは埋まっている行を変えない。
  - 失敗の扱いは JPX と同じ: `EdinetCodelistError`・足跡 `app_settings.edinet_codelist_last_success`・`batch_freshness.PRODUCERS`（Phase 5 では送出しない／手動の `POST /api/collect/industry` は 502）。照合先（JPX の名前）が DB に1つも無いときも失敗にする。
  - 効き方（2026-10-03・ローカル正本で書き込みなしの模擬実行）: 補完は会社 108社・財務行 1,612行。地方37社のうち 36社に `gap_ratio` が付く（残る E01855 は業種以外＝特徴量の欠損で `_prepare_fit` が外す）。既存社の gap は順位相関 0.9995・ridge α は全業種で不変。20pt 超動いたのは 23社で、|gap| の中央値 475% の外れ値側（相対変化の中央値 10%）。1社足された金属製品は業種内の順位相関 0.9865（winsorize の境界と z-score が動く）。
  - 範囲を広げた効き方（#797・2026-10-03・同じく模擬実行・`python -m scripts.measure_industry_fill_impact`）: 補完は会社 517社・財務行 3,954行。当日 gap は 3,629行すべて完全一致（廃止社は当日回帰に入らない）。前から業種があった社の業種内Z は Spearman 0.972（`z_roe_sec`）/ 0.991（`z_op_margin_sec`）で、非 NULL→NULL は 0行。時点再現の gap パネルは母集団の減少率の中央値 1.48%→1.41%。OOF は M-1 が全期同じ・M-2 +0.0023・M-6 +0.0003 でどれも有意でない（数値の正本は ADR-0065）。**模擬実行は `db.rollback` の差し替えが要の安全装置**——`run_comparison` は失敗したモデルで rollback するので、素朴に「commit を flush に替えるだけ」だと補完が黙って消え、以後の「補完後」が補完前を測る（例外も出ない）。
- **`update_industry_from_jpx` の UPDATE 条件は `IS DISTINCT FROM`（#784）**: `industry != ind` は SQL の三値論理で NULL の行を更新しない（発見時点で NULL の行は0件＝潜在バグ）。

---

## EDINET / XBRL の取り扱い

- **EDINET の API ホストは `api.edinet-fsa.go.jp`。`follow_redirects` では直らない（#577・2026-08-29）**: 旧 `disclosure.edinet-fsa.go.jp` は **301** を返すようになり、収集の全経路（`fetch_edinet_code_list` / `collect_doc_ids_for_period` / `fetch_interim_doc_list` / `fetch_xbrl_csv`）が同日から 0 件になった。**リダイレクトに追従してはいけない**——転送先の `disclosure2.edinet-fsa.go.jp` は API ではなく**人間用画面**（`wzek0130.aspx`）へさらに 302 で送るため、`follow_redirects=True` は HTML を JSON としてパースして「その日は提出ゼロ」に化ける＝いまより悪い失敗の仕方になる。URL は `collector_utils.EDINET_BASE` に集約し、`tests/test_collector.py::TestEdinetBase` が直書きの再発を CI で縛る（実際に取り残されたのは `edinet_ping.py` の2箇所で、**疎通確認だけが旧ホストを見ている**と本体が直っているのに ping が落ちて切り分けが濁る）。
- **外部 API の全件失敗は `exit=0` で現れる。「走らなかった」検知はこれを拾わない（#577）**: 上のホスト移設では **892/892 回**の取得が失敗したのに、夜間バッチは `exit=0` ／ `OK pipeline` ／ watchdog `[鮮度] OK` を2晩出し続けた。機構は `fetch_doc_list` の `except Exception: return []` で、**「その日の提出がゼロ」と「取得に失敗した」が呼び出し側から同一に見えた**こと。`app_settings` の足跡を読む watchdog（#515・ADR-0042）は**走ってはいる**ので鳴らない。対処は2段で、いずれも「失敗を型にする」ことが本体:
  - **①連続失敗で打ち切る**（`EDINET_MAX_CONSECUTIVE_FAILURES=10`・`EdinetAccessError`）。`JQUANTS_MAX_CONSECUTIVE_FORBIDDEN` と同じ理屈＝連続失敗は構造的で日付を変えても直らない（実測から逆算した値ではない）。**単発の失敗は握って続行する**——EDINET は個別日でしばしば失敗し、差分収集が翌日同じ日付を再スキャンするので取りこぼしにならない。この寛容さは意図的なので壊さない。
  - **②「全件走査なのに 0 件」を invariant として落とす**（`_phase_upsert_master` のマスタ 0 社／`run_full_collection` Phase 3 の `all_docs` 空）。CLAUDE.md の設計制約「`df_master` は常に全件」を実行時にも効かせるもの。**`edinet_set` を指定した経路（`max_companies` 指定・`refresh_company` の1社指定）はこの判定から除く**——数社で 0 件は正常なので、区別しないと誤爆する。
  - **③検知を足すときに巻き添えを作らない**（#580）。①②の例外をそのまま `_pipeline_incremental.main()` へ投げ上げると、Phase 3（マクロ）と Phase 4（株価）が**丸ごと走らなくなる**——このファイルが3箇所で明言している「片方の収集元の失敗がもう片方を巻き添えにしない」（#425）に反する。EDINET が全滅していた 2026-08-30 の夜でもマクロ 41,306件・Yahoo gap-fill 341社・鮮度 `fresh` は取れており、**直した穴より持ち込んだ穴の方が高くつく**（発火条件は 10連続 × 0.6秒＝わずか6秒の断）。正解は**Phase 1 で握って記録し、Phase 2〜4 を最後まで回してから `SystemExit(1)` で抜ける**＝検知（#577）と非巻き添え（#425）の両立。握りっぱなしにすると「全件失敗が exit=0」がそのまま戻るので、**終了コードにするのを忘れない**。
  - 非ゼロで抜ければ `batch_common` が `nightly_last_success` を進めず起票する（同じタイトルの open Issue があれば追記・#885。`asyncio.run` を通しても `SystemExit` は素通りして終了コード 1 になる・実測）。**`_pipeline_gh.py`（手動の全件収集）は握らない**——XBRL が取れないなら走らせる意味が無いので早く落ちるのが正しい。意図的な非対称であり、揃えようとして握りを足すと手動実行が黙って空振りする（`tests/test_pipeline_incremental.py::TestFullPipelineKeepsFailFast` が CI で縛る）。
- **例外メッセージは httpx のログ抑制を素通りする（#577）**: `collector_utils` は httpx/httpcore の INFO ログを WARNING へ落として「クエリ付き URL がログに残る」のを防いでいるが、**`HTTPStatusError` の文字列は `for url '...'` としてクエリごと URL を持つ**。`log.warning(f"...{e}")` の1行で `Subscription-Key` が平文で `.logs/` へ落ちる（実際 2026-08-29〜30 の夜間ログ2本に **3,568箇所**残った。`.logs` は Issue へ貼られうるうえリポジトリは public）。EDINET の例外を出す箇所は必ず `collector_utils.redact_secrets()` を通す。同関数は環境変数の**値**を消す（`scripts/batch_common.env_lines` が変数**名**で伏せるのとは別機構で、語彙 `SECRET_HINTS` だけを CI で照合している）。8文字未満の値は誤爆するので対象外。
- **EDINET XBRL CSV** は UTF-8 と UTF-16 LE（タブ区切り）が混在。`fetch_xbrl_csv` で両方対応済み。
- **CSV 形式を持たない書類は「HTTP 200 + JSON」で返る＝ZIP エラーに化ける（#630・2026-09-08）**: EDINET の書類一覧は書類ごとに `csvFlag` を持ち、CSV が存在しない書類では `'0'` になる。これを見ずに `type=5`（CSV）を要求すると、EDINET は 404 を返さず **HTTP 200 で `{"metadata":{"status":"404","message":"Not Found"}}` という JSON 本文**を返す。`raise_for_status()` を素通りして `zipfile.BadZipFile` に落ちるため、ログ上は「ZIPエラー」＝壊れたファイルを掴んだように見えるが、実体は**その形式が最初から存在しない**。
  - **恒久的失敗なので待っても直らない。** 実測（16件）はいずれも `csvFlag='0'` / `xbrlFlag='0'` / `pdfFlag='1'` で、本文書 ZIP（`type=1`）に XBRL インスタンスを1つも含まない外国会社等の HTML のみ提出だった（例: S100T096＝オムニ・プラス・システム・リミテッド／シンガポール法人の四半期報告書）。`type=1` の ZIP と `type=2` の PDF は 200 で取れるので、**書類が消えたのではなく形式が無い**と切り分けられる。
  - **半期収集では毎回同じ書類を落とし続けていた。** 差分判定（`skip_existing`）が記録するのは**成功した doc_id だけ**なので、失敗した書類は次回も候補に残る。しかもステップは `exit=0` で終わるため**失敗として現れない**（#577 と同型）。対処は `collector_interim.split_by_csv_availability` で候補選別の一番手前に外すこと。**`csvFlag` が明示的に `'0'` のときだけ外す**——欠損を除外に含めると取りこぼす。
  - **失敗の理由は畳まない。** `fetch_xbrl_csv` は全失敗を `None` へ畳む設計（呼び出し側の分岐を増やさないため）なので、run 単位の集計を `collector_financials.xbrl_fetch_stats()` で外から張る（`yahoo_http_stats()`・#556 と同じ形）。`badzip` / `no_csv` / `oversize` / `http` / `other` を **0 のときも出す**——出ていないことが読めないと監視にならない。
- **新式の半期報告書は DEI の当期種別を `HY` と名乗る。`Q2` だけ見ると全件捨てて exit=0（#647・2026-09-11）**: `TypeOfCurrentPeriodDEI` は #219② 当時（2024年提出分）の新式半期報告書では `Q2` だったが、2025年提出分には `HY` を名乗るものがある（実物 S100WXIT・2025-10-30 提出で確認。同じ 2025-09-30 期でも `Q2` の社は残っていて混在する）。`== 'Q2'` の判定は 2026-09-08 の日中枠で **3,921件を取得して 3,905件を「Q2 ではない」として捨て、保存0件のまま exit=0** で終わった。#424 はこれで「供給を回復した」と完了扱いにしていた。
  - **沈黙の機構は3つ重なっている**: 「Q2 ではない」は旧四半期の Q1/Q3 を落とす正当な経路なので**スキップとして数えて失敗にしない**／`collector.py` の全件失敗ガードは `failed == attempted` しか見ない／`skip_existing` は保存した doc_id しか記録しないので、**捨てた書類を毎回62分かけて取り直す**。
  - **対処は「定義上 H1 の書類が H1 と名乗らない」を失敗にすること。** 半期報告書（docType 160）は年1回＝定義上 H1 なので、当期種別が `{Q2, HY}` の外なら判定側の想定が古い。`failed_dei_type` として数え、連続すれば `EdinetAccessError` で止まる。**旧四半期（docType 140）の不一致はスキップのまま**——こちらは Q1/Q3 の正当な除外で、失敗にすると年度末不明の社で誤爆する。
  - context 名も変わっている（`InterimDuration` / `Prior1InterimDuration`。旧来は `CurrentYTDDuration`）。`_apply_row` は `Prior*` を**除外する**方式なので無改修で当期だけが残る。**当期を列挙する方式に書き換えると、ここで黙って0件になる。**
  - **旧四半期の書類は同じ要素を半期累計（`CurrentYTDDuration`）と直近3か月（`CurrentQuarterDuration`）の2つで並べる（#871）**。どちらもメンバーを持たず優先度が同じなので、CSV の並び順で勝つ方が決まっていた——逆順の書類では H1 行の P/L・CF が3か月分の値になり、エラーは出ない（実物 S100K3J3 トヨタ 2021年 H1 の売上: 累計 11,375,223百万円／3か月 6,774,427百万円。この書類は累計が先で正しかった）。対処は期間の除外を `collector_financials._outside_current_period` の1か所に置き、`Quarter` と `Duration` を両方含む context を採らないこと（3か月しか無い項目は NULL）。**B/S は時点 `CurrentQuarterInstant`（第2四半期末＝半期末の残高）から採るので除外しない**。既存の H1 行の取り直しは `python -m scripts.refetch_financials --period-type H1`（まず試運転で変わる行数を数える）。
  - **供給が戻った日に、期種で絞らない「最新年度」が H1 を掴んだ（#680・2026-09-16）。** 修正後の初実走で H1 が 3967件入り、12月期などの H1（2026-06-30）が通期より新しい `year` を名乗るようになった。`/api/companies?include_latest=true` は `max(year)` を期種で絞らず annual 限定の `financial_metrics` と JOIN していたので **1611社の `latest` が NULL** になり、同業比較から黙って消えた。`/api/stats` の最新年度も H1 の 2027 を指した。**`financial_records` から最新年度・年数を取るときは `period_type='annual'` で絞る**（`database.latest_year_subq` の docstring と同じ理由）。H1 が少ないうちは目立たず、供給を直した瞬間に表へ出る。
- **XBRL要素選択**: 優先度は `collector_financials._context_priority` が唯一の源（式を書き写さない）。連結=2 > 非メンバー=1 > メンバー付き・単体=0。**現行の EDINET では連結総額のコンテキストは `CurrentYearDuration` で `Consolidated` を含まない**＝優先度1。2 になるのは旧形式（`CurrentYearConsolidatedDuration` 等）だけで、**名前に `Consolidated` を含むメンバー（`...ConsolidatedSubsidiariesReportableSegmentsMember` 等）は 0**。かつてはメンバーでも 2 を与えていたため、セグメント1つ分の値が連結総額に勝ち、売上高などが黙って小さくなっていた（#852・実測 E00317 は連結 4,396億円に対し 938億円）。
- **連結を作る会社の書類では、財務諸表の列に単体の値を採らない（#896）**: 優先度だけで選ぶと、連結の値がその列に1つも無いとき単体（`NonConsolidatedMember`・優先度0）が**エラーなしで**入り、同じ行に連結と単体が混ざる。US-GAAP の有報は連結の明細を標準タグで持たないので、売上原価・販管費・営業利益・経常利益・負債・流動資産・棚卸資産が単体だった（トヨタ 2019年度の営業利益率 4.4%＝単体の営業利益 ÷ 連結の売上高。連結は約8.2%）。IFRS でも販管費・経常利益（IFRS に無い概念）と、未登録タグの EPS・BPS が単体だった。規則は `collector_financials._drops_nonconsolidated` の1か所。**判定は書類単位で DEI の連結決算の有無（`WhetherConsolidatedFinancialStatementsArePreparedDEI`＝`true`/`false`）**——単体だけの会社も全ての値を `NonConsolidatedMember` で載せる（実物 E01033 S100W071）ので context だけでは決められない。DEI が読めない書類は従来どおり。対象は bs/pl/cf の列だけで、**1株配当は有報の提出会社（単体）の欄にしか載らない**ので外す（従業員数・発行済株式数も外す）。連結の値が書類にあるのに未登録だったタグは足した（`SellingGeneralAndAdministrativeExpensesIFRS`・`BasicEarningsLossPerShareIFRSSummaryOfBusinessResults`・`BasicAndDilutedEarningsLossPerShareIFRS`・`EquityToAssetRatioIFRSSummaryOfBusinessResults`。**最後の要素は名前に反して EDINET のラベルが「１株当たり親会社所有者帰属持分」＝BPS**で、比率は `RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults`）。営業利益が空欄の行は `op_margin` も空欄（0% にしない・3つの VIEW と `calc_derived`）。**既存行は取り直し（NULL で上書きしない）では消えない**ので `python -m scripts.refetch_financials --clear-nonconsolidated`（既定は試運転）で戻す。正本への適用は #900。
- **売上債権は注記の内訳と BS の行が同じタグ・同じ文脈で並ぶ（#904・2026-10-11）**: 現行の有報は収益認識の注記「顧客との契約から生じた債権」の内訳（売掛金 `AccountsReceivableTrade`・契約資産 `ContractAssets`）にも、BS と同じ `jppfs_cor` のタグと同じ `CurrentYearInstant` で数値を付ける。BS の行（「受取手形及び売掛金」や、収益認識基準後の「受取手形、売掛金及び契約資産」「売掛金及び契約資産」）と優先度が同じなので、**CSV の並び順で値が決まっていた**（2025-06 提出の日本基準・連結 200件中 58件。位置を見た実物4件は BS が先だった）。合算タグが未登録の間は、注記の「売掛金」か単体の値（#896 の後は空欄）が入っていた。対処は合算タグの登録と、同じ優先度では注記の売掛金を負けさせる `collector_financials._TAGS_LOSING_TIES`。**列の意味は「BS に載る売上債権の行」**で、合算の行は契約資産を含む（契約資産が売掛金より大きい社もある・実物 S100W4DK）。BS に売掛金と契約資産を別の行で載せる社は売掛金だけ（S100W5KF）。既存行は取り直しで変わる。
- **減価償却費（`pl_depreciation`）の CF のタグは会社ごとに名前が違う（#1145・2026-11-06）**: 長く登録していたのは `DepreciationAndAmortizationOpeCF`（と IFRS 版）だけで、日本基準の「減価償却費及びその他の償却費」`DepreciationAndOtherAmortizationOpeCF`、IFRS の `DepreciationAndAmortizationOfIntangibleAssetsOpeCFIFRS`（日立 S100YGBO・アステラス）・`DepreciationExpenseOpeCFIFRS`（NTT S100YCP3）を拾えず、**値が書類にあるのに空欄**だった。`da_intensity` が空欄になると M-2 / M-6 は財務特徴量が1つ欠けた社として丸ごと外す（日立・NTT の μ̂ が無かった）。`DepreciationExpenseOpeCFIFRS` は無形資産の償却を別の行に分ける社がある（日立建機 S100YIBR: 682億＋償却96億）ので、同点では合計のタグに負ける（`_TAGS_LOSING_TIES`）。会社独自のタグで載せる社（カメイ S100YKU0）は拾えない。既存行は `refill_c2_from_xbrl`（`pl_depreciation IS NULL` が駆動マーカー）で埋まる。
- **IFRS 企業の売上高タグは揃っていない（#852）**: 「収益」と呼ぶ社は標準タグ `Revenue2IFRS`、「営業収益」の社（トヨタ・E03345 等）は独自拡張 `OperatingRevenuesIFRS` / `OperatingRevenuesIFRSKeyFinancialData`。連結の売上が1つも一致しないと、単体の `NetSales`（NonConsolidatedMember・優先度0）が**エラーなしで採られ**、売上だけ単体・費用と利益は連結という行になる（トヨタは 2021〜2026年度の売上高が単体の 11.8〜18.3兆円だった）。目印は「売上原価 > 売上高」や「売上高だけが前年比で急落」。独自拡張は合計を指す名前だけ足す（トヨタの `SalesOfProductsIFRS` は商品・製品売上だけの内訳）。
- **IFRS の BS の小計は日本基準の名前に IFRS を付けた形ではない（#935・#930・2026-10-12）**: 連結の小計は
  `NonCurrentAssetsIFRS`（非流動資産・C が大文字）・`TotalCurrentLiabilitiesIFRS`（流動負債）・`NonCurrentLabilitiesIFRS`
  （非流動負債・**タクソノミ自体が Liabilities を Labilities と綴っている**。直すと拾えなくなる）。長く登録していた
  `NoncurrentAssetsIFRS` / `CurrentLiabilitiesIFRS` / `NoncurrentLiabilitiesIFRS` は 2019〜2025 年度の通期・H1 のどの書類にも
  無く、連結の小計を1つも拾えていなかった＝**単体の値が代わりに入り、エラーは出ない**（トヨタ 2026 の流動負債は単体の 6.19 兆円で、
  「流動＋固定」が負債合計の 12%）。流動資産 `CurrentAssetsIFRS`・総資産 `AssetsIFRS`・負債合計 `LiabilitiesIFRS` は合っていた。
  **タグを登録するときは、名前を推測せず実物の書類で確かめる**（合計どうしが一致していても小計だけ別物ということがある）。
- **会計基準は DEI `AccountingStandardsDEI` から読む（#859）**: 表記は `Japan GAAP` / `IFRS` / `US GAAP`（年度・半期・旧四半期で同じ・2026-10-09 に実物6書類で確認）で、`collector_financials.ACCOUNTING_STANDARDS` が列の値 `JGAAP` / `IFRS` / `US-GAAP` へ写す。**#859 まで収集側に書き手が無く全行 NULL だった**（データ品質の会計基準別の集計が「未設定」1群になり点検として働かない）。表に無い表記は推測で丸めず警告して NULL（修正国際基準 JMIS は実例を確かめられず未登録）。DEI の抽出 `_extract_dei` は年度・半期の収集と取り直し（`scripts/refetch_financials.py`）が共有する唯一の源。既存行は取り直しで埋まる（#858）。
- **CF要素名・XBRL ZIP 構造**: EDINET XBRL type=5 ZIP には**複数の CSV ファイル**が含まれる。CF合計は概要ファイルに、CF明細は別ファイルに存在。ZIP内の全CSVを concat して parse する（`fetch_xbrl_csv`）。投資CFのEDINET標準要素は `NetCashProvidedByUsedInInvestmentActivities`（Investment、旧 Investing は誤り）。
- **税前利益（税金等調整前当期純利益）の要素名（実証済み・2026-06-13修正）**: PL段階のうち税前利益だけが本番20,513件中 **99.8% NULL** だった。原因は登録タグ誤り — JGAAPの正しい要素は **`IncomeBeforeIncomeTaxes`**（旧 `ProfitLossBeforeIncomeTaxes` は実在せずほぼ1件も一致しなかった）、IFRSは **`ProfitLossBeforeTaxIFRS`**（＋「経営指標等」用 `ProfitLossBeforeTaxIFRSSummaryOfBusinessResults`。旧 `ProfitLossBeforeIncomeTaxesIFRS` も誤り）。隣接段階（経常 `OrdinaryIncome`・純利益 `ProfitLoss`/`NetIncomeLoss`）は100%取れており税前タグのみの不一致だった（実ファイル S100Y8UO 等で要素名を実証）。`database.py` の `pl_pretax_profit` 列 info を修正（`XBRL_MAP` は `build_xbrl_map` で自動反映）。**既収集レコードの是正は別途バックフィルが必要**: 生タグはDBに保持しない（`xbrl_raw_documents` は容量制約で本番0行）ため、保存済み doc_id からの再フェッチでしか埋まらない（`reparse_from_raw` 不可）。専用関数 **`refill_pl_bs_from_xbrl`（CLI: `python collector.py --refill-pl-bs`）** を用意（`refill_cf_from_xbrl` 同型）。駆動マーカー=`pl_pretax_profit IS NULL`＋`doc_id`、再取得した同一ファイルから **NULL の PL/BS 列のみ補完**（既存値は上書きしない）・税前が埋まると対象外になり繰り返しで自然終了。CF は `refill_cf_from_xbrl` が担当。
- **棚卸資産（bs_inventory）の XBRL 記載方式差異（2026-06-13 対応）**: `bs_inventory` が本番で **92% NULL** だった原因はタグ名エラーでなく、JGAAP ファイラーの大半が aggregate 要素 `Inventories` を出力せず**サブ項目のみ**（`MerchandiseAndFinishedGoods`・`WorkInProcess`・`RawMaterials`/`RawMaterialsAndSupplies` 等）を出力するため。8% の会社（六甲バター等）は `jppfs_cor:Inventories` を直接出力しており正常取得できていた。**解決策**: `parse_xbrl_csv` / `parse_raw_rows` に `_inventory_fallback` を追加 — `Inventories` が取れなかった場合にサブ項目を定義済みグループ（`_INVENTORY_GROUPS`）の precedence 順に合計して `bs["inventory"]` に設定する。二重計上防止: グループ内で上位要素（`MerchandiseAndFinishedGoods`）が取れたら下位（`Merchandise`/`FinishedGoods`）をスキップ。既存の連結優先・Priorスキップロジックを踏襲。これにより新規収集分からサブ項目フォールバックが機能する。**既存レコードの是正**: `python collector.py --refill-pl-bs` を再実行すると NULL の `bs_inventory` が埋まる。
- **IFRS/US-GAAP決算のCF・売上要素名（重要・実証済み）**: IFRS採用企業（トヨタ・ホンダ・クボタ等）のCF合計は **`NetCashProvidedByUsedInOperatingActivitiesIFRS`**（=`NetCash...IFRS` 系。`CashFlowsFromUsedIn...IFRS` ではない）でタグ付けされる。さらに全有報に必ず存在する「主要な経営指標等の推移」テーブルに **`CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults`** 系（営業/投資/財務）があり、本体が独自拡張要素の企業の保険として両系統を `XBRL_MAP` に登録している。当期は `context=CurrentYearDuration`（`Consolidated` を含まないが Prior 除外フィルタは通る）。IFRSのcapexは `PurchaseOfPropertyPlantAndEquipmentInvCFIFRS` 等の独自要素だが既存のラベル照合（「有形固定資産の取得による支出」）で捕捉される。
  - **US-GAAP採用企業**（キヤノン・コマツ・オリックス・野村HD・ソニー旧年度等）も同型で、CF合計・**連結売上**・純利益・総資産・純資産・EPS/BPS が `...USGAAPSummaryOfBusinessResults`（`CashFlowsFromUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults` 系・`RevenuesUSGAAPSummaryOfBusinessResults`・`NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults`・`TotalAssetsUSGAAPSummaryOfBusinessResults`・`EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults`・`BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults` 等）に集約される。**営業利益・売上原価・販管費・負債などの連結の明細は経営指標等に無く、書類に載るのは単体の値だけ**——以前は「取得不可」と書いていたが、実際は単体の値が入っていた（#896）。今は連結の値が無い列は空欄で、単体では埋めない（上の「連結を作る会社の書類では…」）。**注意**: これら未登録時は非連結 `NetSales`（メンバー・優先度0）を誤採用し連結値が大幅過小/欠損になっていた（例: キヤノン 連結4.6兆を1.8兆と誤記）。
  - **IFRSで「売上高(NetSales)」を使う企業**（ソニー等）の連結売上は `NetSalesIFRS`（「売上収益」の `RevenueIFRS` ではない）。これも未登録時は非連結 `NetSales` に負けていた。
  - **自己資本の注意**: US-GAAP社は「株主資本」(`EquityAttributableToOwnersOfParentUSGAAP…`)と「純資産額」(`EquityIncludingPortion…NCI…`)の**どちらか一方しか載らない**企業がある（キヤノン=株主資本のみ／野村=純資産額のみ）。両方を `total_equity` に登録し片方欠落でも連結自己資本が埋まるようにした（同優先度は先勝ち＝株主資本利益率に整合）。未対応だとキヤノンの `total_equity` が非連結のまま残り ROE が約2倍に誤算出されていた。
  - 既収集レコードは**保存済み doc_id からの再パース＋ upsert で是正済み**（2026-06-03、US-GAAP/移行組11社）。`upsert_financial` は市場データ列を保持するため CF専用 `--refill-cf-missing`（CF列のみ）とは別に PL/BS も含めて上書きできる。EPS/BPS 変更後の `per`/`pbr`/`market_cap` は保存列のため、保存済み株価＋新EPS/BPSで `_apply_price_to_record` により再計算した（次回の株価更新でも自動再計算される）。
- **「営業収益」型企業の売上（実証済み）**: 鉄道・電力・小売・不動産・サービス・倉庫・空運・証券等は「売上高(NetSales)」ではなく連結 **`OperatingRevenue1SummaryOfBusinessResults`「営業収益、経営指標等」**（context=CurrentYearDuration＝連結）で計上する。旧マップの `OperatingRevenues`（複数形）とは別綴り（単数+1）のため未取得だった。2026-06-03 に `XBRL_MAP` へ追加し、売上NULL社を **259→152** に補完（保存済み doc_id 再パース。107社が連結営業収益で埋まる）。
  - **連結のみ採用（重要）**: 生 `OperatingRevenue1`（PL本体・セグメント別）は登録せず、`OperatingRevenue1SummaryOfBusinessResults` のみを採用する。さらに parse で **`OperatingRevenue1` 系の売上は `NonConsolidated`/`_Member` コンテキストを除外**する（`parse_xbrl_csv`/`parse_raw_rows`）。金融持株会社（MUFG・みずほ・第一生命等）は連結営業収益を持たず**提出会社単体（NonConsolidatedMember）の営業収益**しか無く、これを採ると売上が単体値に過小化（例: MUFG 連結なし→単体1.3兆・純利益率144%）するため。連結を持つ非金融＋証券（大和証券・JR東日本は consolidated CurrentYearDuration を持つ）はそのまま採用される。
- **銀行・保険の売上は設計上 NULL（既知の制約）**: 銀行・保険・一部その他金融（残 売上NULL 約94社）は「売上高」概念がなく、有報が「一般企業の売上高に代えて経常収益を記載」と明記（銀行 `OrdinaryIncomeSummaryOfBusinessResults`「経常収益」／保険 `InsurancePremiumsAndOtherOIINS` 等）。経常収益を `revenue` に入れると利益率・業種OLS・スクリーニングで非金融と混ざり歪むため、**意図的に revenue へマップせず NULL のまま**にしている。※経常収益 `OrdinaryIncome…SummaryOfBusinessResults` と経常利益 `OrdinaryIncomeLoss…SummaryOfBusinessResults` は別物（後者＝profit）。
  - 非金融の残 売上NULL（約58社）は、持株会社が連結営業収益を経営指標等に出さない（例: 博報堂DY・日本郵政）／業種固有の収益要素／プレ収益バイオ（売上ゼロが正当）等。さらに別系統の既存issueとして「非NULLだが誤った売上（売上<純利益）」が数社（例: ローソン）残る。いずれも業種別収益要素の追加マッピングを要する別タスク。
- **capex（設備投資）はラベル照合で取得**: 設備投資のCF明細行は**企業独自の拡張要素ID**でタグ付けされることが多く、標準要素ID（`PurchaseOfPropertyPlantAndEquipment`）では捕捉できない（実証: 3,000件中0件）。EDINET CSV の**「項目名」列**で照合する（`_match_capex_by_label` / `CAPEX_LABEL_*` 定数）。「有形固定資産の取得による支出」等を捕捉し、売却収入・無形のみは除外。capex は支出＝負（`-abs(val)`）で統一。
- **`raw_xbrl_json` 列は削除済み（Issue #219 ①・容量削減）。復活させない**: 中身は parse 済みの bs/pl/cf dict で生タグを持たず、reparse にも使えなかった（`SKIP_XBRL_RAW` が止めるのは別テーブル `xbrl_raw_documents` だけ）。削除の経緯は [DEPLOYMENT_HISTORY.md](archive/DEPLOYMENT_HISTORY.md#容量設計-実装済みの後続-pr)。
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

テーマ別の小見出し（各項目の経緯と実測の記録は [archive/GOTCHAS_HISTORY.md](archive/GOTCHAS_HISTORY.md)）:

- [画面・設定の基本](#画面設定の基本)
- [接続先と DB セッション・タイムアウト](#接続先と-db-セッションタイムアウト)
- [大量 UPDATE と SQL の書き方](#大量-update-と-sql-の書き方)
- [Egress（転送量）の計測と削減](#egress転送量の計測と削減)
- [ローカル PostgreSQL・ダンプ・復元](#ローカル-postgresqlダンプ復元)
- [株価の保存（日次・週次の2本立て）と全件ロード](#株価の保存日次週次の2本立てと全件ロード)
- [日付・時刻の境界（JST）と株価の鮮度](#日付時刻の境界jstと株価の鮮度)
- [株価のスケール（1）J-Quants の調整後値と分割の修復](#株価のスケール1j-quants-の調整後値と分割の修復)
- [株価のスケール（2）Yahoo の株式併合](#株価のスケール2yahoo-の株式併合)
- [per / pbr の分割補正（ADR-0055）](#per--pbr-の分割補正adr-0055)
- [J-Quants API の応答とエンバーゴ](#j-quants-api-の応答とエンバーゴ)
- [Yahoo の取得（ティッカー・上場市場・絞り込み）](#yahoo-の取得ティッカー上場市場絞り込み)

### 画面・設定の基本

- **URLとHTMLファイル名の対応**を崩さない: `/` ↔ `dashboard.html`、`/collection` ↔ `collection.html`、`/analysis` ↔ `analysis.html`。
- **`CollectionLog.status`** の値: `running` / `done` / `error` / `resolved`（修正済みエラー）。UIは `resolved` を緑扱い。
- **.env は UTF-8（BOMなし）で保存すること**。BOM付きだと最初のキーが読み込めずAPIキーが空になる。
- 本番運用前に `APP_PASSWORD`・`APP_SECRET_KEY`・`APP_RECOVERY_KEY` を必ず設定する。

### 接続先と DB セッション・タイムアウト

- **`DATABASE_URL` 未設定は「接続失敗」ではなく「空のローカル DB に繋がる」に化けた（Issue #481 B-1）**: [database.py](../database.py) は昔から `DATABASE_URL` 未設定時に `postgresql://edinet:edinet@localhost:5432/financial_db` へフォールバックする。**ローカル PostgreSQL が存在しなかった頃はここで接続エラーになって気づけた**が、B-0 でローカル DB を作った今は**繋がってしまい、全件0件の画面が正常に見える**。`.env` の読み漏れ・環境変数のタイプミスが「データが無い」という誤った診断に化ける経路なので、(1) フォールバック時は警告ログを出す (2) `/api/system/info` の `db_is_local` を `common.js` が読んで全画面にバッジを出す、の2段で見えるようにしてある。**`prod` 指定でも raise はしない**——`ci.yml` は `DATABASE_URL` を渡さずに走るため、例外にすると CI が全滅する（`tests/test_db_target.py::TestGuards::test_prod_without_database_url_does_not_raise` がこの意図を固定している）。
- **接続先を環境変数で切り替えるときは、`import database` より先に立てる（Issue #481・#503）**: [database.py](../database.py) の `load_dotenv()` は既定 `override=False`＝既に立っている環境変数が `.env` に勝つ。#503 以降は `FINAPP_DB_TARGET` の既定が `local` で、local では `DATABASE_URL` を読まない（ローカルの接続先は `DATABASE_URL_LOCAL`）ので、この罠が残るのは `FINAPP_DB_TARGET=prod`（または `RENDER`）が立っているときだけ。`init_db()` のように不可逆な DDL を打つ処理は、**接続先を表示してから `database._is_local` で止める**（実装例 `scripts/setup_local_db.py`）。
- **長時間バッチはDBセッションを計算中に保持しない**（Issue #269）：`SessionLocal()` で開いたトランザクションは、直後のSELECTが終わっても明示的に `commit()`/`close()` するまでオープンのまま＝該当テーブルに `AccessShare` ロックが残り続け、別セッションの `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（`ACCESS EXCLUSIVE` 要求）等をブロックする。`macro_beta_inference.py` は数時間かかる MCMC 計算の前にデータ読込のみで `build_panel` を終える構造だったため、読込直後に `db.commit()` してロックを解放してから計算に入る（`persist()` は同じ db をコミット後に再利用＝`pool_pre_ping=True`・`pool_recycle=180` で長時間後の再利用も安全）。DB接続を使わない長時間処理を挟むバッチを書く際は同じ罠に注意。
  - **自前で `SessionLocal()` を開くローダも、読込直後に `commit()` する**（`close()` だけでは `idle in transaction` のまま残り、`init_db()` の `ALTER TABLE` をロック待ちで詰まらせた・#411）。**診断は `pg_stat_activity`（`state='idle in transaction'` と `now()-xact_start`）と `pg_locks`（`granted=false`）を突き合わせる**。滞留セッションは `pg_terminate_backend(pid)` で落とせる（読取専用のトランザクションなので失うデータはない）。
- **Supabase 既定の `statement_timeout=2min` は「重い1文」に足りない。引き上げは局所に、`lock_timeout` とセットで（#470 / #471）**: `postgres` ロールの既定は **`statement_timeout=2min` / `lock_timeout=0`（無制限待ち）/ `idle_in_transaction_session_timeout=0`**。2026-08-08 はこの 2min が差分収集の PER/PBR 反映と週次 VACUUM FULL を同じ日に落とした。
  - **引き上げは `db_timeouts`（database.py）で「その文の実行中だけ」行う**。プロセス全体やロール既定（`ALTER ROLE`）を書き換えると、想定外の暴走クエリまで 2min で止まらなくなり API/分析経路の安全網が消える。抜けるときは必ず `RESET`（接続はプールへ返って再利用されるため、差し替えたまま返すと無関係な経路へ漏れる）。Postgres 以外では no-op にする＝テストは SQLite で走るため、`SET` を投げると全件落ちる。
  - **`lock_timeout=0` のままだと原因が事後に確定できない**。ACCESS EXCLUSIVE を取る処理は「取れるまで待つ→ statement_timeout に殺される」形で落ちるため、**ロック待ち超過なのか文が重いのかログから区別できない**（#471 の初回がこれ。同型の前例が ADR-0025 にもある）。有限の `lock_timeout` を置いてロック待ちだけ先に諦めさせ、`pg_locks`×`pg_stat_activity` で保持者を run ログへ残す。
  - **「前は速かった」は上限に対する余裕の証明にならない**（VACUUM FULL は同じ規模で所要が4倍に伸びた実測がある）。文に固定の上限は置かず、**時間の歯止めは親の締切から導く**（#868: 月次バッチの `vacuum` は締切までの「残り − 60秒」を `statement_timeout` に入れる）。**クライアントを kill しても PostgreSQL 側の文は止まらない**（サーバーは次に返事を送ろうとするまで気付かず、`client_connection_check_interval` は Windows で使えない）ので、予算超過の `kill_tree` だけでは ACCESS EXCLUSIVE を握った VACUUM FULL が残りうる。GHA の `timeout-minutes` も同じで、止めるのはクライアントだけ。
  - **保存の失敗を warning 1行で流すと、鮮度の穴が静かに残る**。同 run では pooler 枯渇（`FATAL: (ECHECKOUTTIMEOUT) unable to check out connection from the pool after 15000ms in Session mode`）も2回起きており、どちらも1バッチぶんの株価を捨てていた（収集は成功扱い）。プール枯渇も timeout も一過性なので、`_price_collection_driver` は間を空けて数回再試行し、使い切ったときだけ warning を残す。**`except` では必ず `rollback()` する**——aborted transaction を持ち越すと1社の失敗が残り全社を道連れにする。
- **`init_db()` は「冪等」だがロックは取る（Issue #597・[ADR-0048](adr/0048-schema-migrations-run-only-when-the-fingerprint-changes.md)）**: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` と `DROP VIEW` は、列が既に存在しても ACCESS EXCLUSIVE を取る。呼び出し元（`api.py::lifespan`＝画面を開くたび・`_pipeline_incremental.py`＝毎晩・`_pipeline_gh.py`・`scripts/setup_local_db.py`）は、長時間バッチが AccessShareLock を握っている間だと**無言で待ち続け**、その後ろに同じ表へのクエリまで並ぶ。対策は指紋ゲート（`_schema_fingerprint()` が一致し実体も揃えば DDL を1本も発行しない）と、移行が要る回の `db_timeouts(lock=DDL_LOCK_TIMEOUT)`（5秒で `LockNotAvailable`＝ハングではなく失敗として現れる）。**指紋は移行を決める入力すべてから作る**ので、DDL を足せば自動で変わる。手で DB をいじった後に直したいときは `app_settings` の `schema_fingerprint` 行を消してから再実行する。

### 大量 UPDATE と SQL の書き方

- **大量 UPDATE は「1件1文」をやめる。ただし `bulk_update_mappings` では直らない（#464）**: ORM 属性代入＋`commit()` はダーティ1件につき UPDATE 1文を出す。律速は転送量ではなく**往復回数**で、同じコードが実行場所（往復の遅延）で10倍以上変わる。
  - **`bulk_update_mappings` に替えても 10.26分で変わらなかった**。psycopg2 の `executemany_mode` 既定（`values_only`）が `execute_values` で束ねるのは **INSERT だけ**で、UPDATE は `cursor.executemany()` ＝1行1往復のまま。`UPDATE ... FROM (SELECT ... UNION ALL ...)` を1文組んでチャンクあたり1往復にして **約56秒**（22チャンク）になった。
  - **方言で SQL を分岐させない**。Postgres の `FROM (VALUES ...) AS v(cols)` は SQLite が受け付けないが、そこで分岐すると**テスト（SQLite）が本番（Postgres）の文を一度も実行しない**。`_update_issued_shares` の `UPDATE ... FROM` がまさにそれで、エイリアス構文の誤りが長期間検出されなかった。両方言が通る `SELECT ... UNION ALL` 形へ揃える。
  - **パラメータに CAST を付ける**。psycopg2 はクライアント側で literal 展開するため、あるチャンクで1列が**全行 NULL** だと Postgres がその列を `text` と推論し `column "pbr" is of type double precision but expression is of type text` で落ちる。値が1つでも混ざれば通る＝**データ内容とチャンク境界次第で落ちる**不安定な失敗になる。
  - **列集合を行ごとに変えない**。計算できなかった指標は「更新しない」ではなく**現在値で埋め直す**（NULL で既存値を潰さないため）。
- **`ORDER BY` の無いクエリ結果を「最大値の走査」に使わない（#464）**: 同一 `(edinet_code, year)` の annual を複数持つ社があり（会計期間変更）、行順は `ORDER BY` が無い限り保証されないので、同じデータで走らせるたびに勝つ行が入れ替わる。`(year, period_end)` まで見れば `UniqueConstraint(edinet_code, year, period_end, period_type)` で一意に決まる（`_fetch_latest_fin_by_ec` の `ROW_NUMBER() OVER (ORDER BY year DESC, period_end DESC)`）。**同種の「最新を選ぶ」処理は全経路で同じ tie-break を使う。**
- **学習行の並びを読み出し順のままにしない（#954）**: XGBoost は `random_state` を固定しても、subsample と early stopping の検証集合を「何行目か」で選ぶので、**行の並びが変わると別の木になり、エラーは出ない**。`_build_snapshots_impl` は社を `edinet_code` 順に回す（読み出し側の `ORDER BY` では、キャッシュの差分ロードが新しい社を末尾へ足すので保証できない）。並びを固定しても M-2 は並びの選び方で ±0.005〜0.01 動くので、`momentum_gate` は M-2 を既定で 5 個の種で回して平均する（`--seeds`・#955・ADR-0050 の 2026-10-15 追記）。

### Egress（転送量）の計測と削減

- **Egress の計測は `cursor.rowcount` に乗るが、方言差がある（Issue #478・`db_egress.py`）**: psycopg2 の既定カーソルは**クライアント側バッファ**なので、`execute()` が返った時点で行は全部ネットワークを渡り終えており、SELECT でも `cursor.rowcount` が転送行数として確定している（SQLAlchemy の `after_cursor_execute` から結果を消費せずに読める）。一方 **SQLite は SELECT の `rowcount` が常に -1**（行数を事前に確定できない DB-API の仕様）。テストは全件 SQLite で走るため、**CI では実数の計上が一度も起きない**——`-1` を 0 に丸めると「引いていない」と見分けがつかなくなるので `unknown` バケットへ隔離する（#438 の「静かな停止」を作らない・`_VOLUME_NOT_LOADED` 番兵と同じ考え方）。計上対象の判定に「文字列が SELECT で始まるか」を使わないこと：`INSERT ... RETURNING` は行が返る＝Egress なので落としてしまう。正しい門は `cursor.description is not None`（結果セットを返したか）。
- **VIEW を ORM で引くと「使わない列」まで毎回転送される（Issue #441）**: `db.query(FinancialMetric)` は `financial_metrics` の全列 × 全社を SELECT し、VIEW の計算より行転送のほうが重い。毎朝必ず踏む経路（`/api/recommend`・`/api/morning`）ほど列を絞る。絞るときは `db.query(*cols)` で **Row タプル**にする（`options(load_only(...))` は未ロード列へのアクセスが行ごとの遅延ロード＝N+1 に化けうる）。**列集合は指標リスト（`METRICS`）から導出し、キー許可集合の検証（`coerce_params` の membership）とセットで入れる**——検証が無いと範囲外キーが `getattr(row, key, None)` で黙って None になり、スコアとカバレッジが静かに変わる。
  - **残る5経路も #482 で列指定へ広げた**（`sell_ranking` の VIEW 2箇所と週次・`sector_ols`・`utils.get_macro_features`・`database.get_macro_beta`）。以後の**新規追加は `tests/test_column_scoping.py` の AST メタ検査が CI で落とす**（許可リスト `FULL_ROW_LOADS` との双方向差分）。全列ロードは failure を出さないので `notify-failure.yml` では原理的に拾えない（ADR-0031 と同型）。
- **列指定 Row にするとリレーションが黙って消える（Issue #482）**: `db.query(Model)` を `db.query(Model.col, ...)` へ変えると戻りは Row タプルになり、`relationship` が辿れなくなる。`getattr(row, "company", None)` は **AttributeError ではなく None** を返すため、リレーション経由のフォールバックが**エラーも警告も無しに消える**。実例は `plugins/utils.py::shares_outstanding` の第2優先 `record.company.issued_shares`（J-Quants マスタ値・#462）で、消えると株数推計不能の企業が母集団から落ちるだけ＝回帰結果が静かに変わる。対策は SQL 側で潰すこと：`outerjoin(Company, ...)` ＋ `func.coalesce(FinancialRecord.issued_shares, Company.issued_shares)` を同じ名前でラベルすれば、消費側は無改修のまま優先順位が保たれる（副産物として N+1 遅延ロードが JOIN 1本になる）。**列を絞る前に「リレーションを辿っている消費者が居ないか」を必ず確認する**。

### ローカル PostgreSQL・ダンプ・復元

- **ローカルの `edinet` ロールは superuser ではない（Issue #481）**: PostgreSQL の Windows インストーラは superuser を `postgres` に置き、アプリ用ロールには権限を渡さない。実測で `rolsuper` / `rolcreatedb` / `rolcreaterole` すべて false。影響は2つ——**①新しいデータベースを作れない**（`CREATE DATABASE` には postgres での操作が1回要る）②**`pg_restore --disable-triggers` が使えない**（FK 用のシステムトリガを止めるには superuser が要る）。②の回避は「FK 依存順の単一スレッド restore」＝FK は4本ともに `companies.edinet_code` 向きなので親を先に入れれば足りる。`--jobs` の並列 restore は順序が崩れるので使わない（ADR-0035）。
- **ダンプの TOC は `--table` の指定順ではなくアルファベット順（Issue #481 B-2・2026-08-16 実測）**: `pg_dump --table=stock_price_weekly --table=financial_records --table=companies --table=statement_disclosure` としても、`pg_restore -l` の並びは `companies -> financial_records -> statement_disclosure -> stock_price_weekly` になる。つまり **16 表をまとめて1回の `pg_restore` に渡して FK が満たせているのは、`companies` の頭文字が c で子表より先に来るという偶然**にすぎない。表名を変えた瞬間に黙って壊れる（しかも非 superuser は `--disable-triggers` で逃げられない）。`Base.metadata.sorted_tables` の位相順で**1表ずつ `--table=` を指定して流す**こと。副産物として表ごとの所要時間が出る。
- **`pg_dump --strict-names` が無いと綴り誤りが無言で通る（Issue #481 B-2・2026-08-16 実測）**: 指定した `--table` パターンが**1つも**マッチしなければ既定でもエラーになるが、**16 本のうち1本だけ綴りを間違えた場合は exit 0 で通り、その表だけ入っていないダンプができる**（実測: 正しい表1本＋typo 1本で exit=0・1,433 バイトのダンプが生成された）。一回きりの本番実行でこれを踏むと、欠けた表に気づくのは restore 後の突合まで遅れる。`--strict-names` を必ず付ける。あわせて **`-w`（パスワードプロンプト禁止）**も必須——無いと認証失敗時に子プロセスが標準入力待ちで**無言でハングする**。
- **`pg_*` の子プロセス出力は UTF-8 単一 decode では読めない（Issue #481 B-2・2026-08-16 実測）**: Windows では2種類のエンコーディングが混ざる。pg_dump 自身のメッセージは ASCII 英語だが、**サーバから返る `FATAL` はサーバの `lc_messages`（`Japanese_Japan.932`）そのままの cp932 バイト列**で来る。しかも**認証失敗は client_encoding のネゴシエーション前に起きるので `PGCLIENTENCODING=UTF8` を立てても直らない**（`PGOPTIONS="-c lc_messages=C"` は非 superuser には設定権が無く `FATAL` になる）。`subprocess.run(..., text=True, encoding="utf-8", errors="replace")` だと「パスワード認証に失敗しました」が U+FFFD の羅列に潰れ、**原因が読めなくなる**。バイトのまま受けて **utf-8 -> cp932 の順に試す**（`mirror_common.decode_pg_output`）。`tests/test_auth_hardening.py` の前例は子プロセスが Python＝UTF-8 だったので当てはまらない。
- **`pg_dump` の圧縮は Egress の計測を狂わせる（Issue #481 B-2）**: custom 形式は既定でローカル側 zlib 圧縮するが、**ワイヤ上を流れるのは非圧縮の COPY ストリーム**である（libpq の通信圧縮は既定 OFF）。圧縮したままだとダンプファイルのサイズが実転送量を大幅に過小申告し、`LEDGER.record_external()` に嘘の数字が載る。ミラー pull は `--compress=0` で取り、ファイルサイズをそのまま台帳へ渡す。あわせて **`pg_dump` は SQLAlchemy を通らないのでサーキットブレーカ（ADR-0034）が途中で止められない**——歯止めは事前見積り（サーバ側 `sum(octet_length(...))`）＋ `--allow-full-pull` ゲートに置くこと。
- **両端の行を text へ落として比べるならセッション設定を固定する（Issue #481 B-4）**: `md5(row::text)` 型のチェックサムは `TimeZone` / `DateStyle` / `extra_float_digits` に依存する。ローカルは実測で `TimeZone=Asia/Tokyo`・`extra_float_digits=1`、Supabase はほぼ UTC。固定しないと `app_settings.updated_at`（唯一の timestamptz 列）だけで**値が同一でも恒常的に不一致**になり、「指紋が常に赤い＝誰も見なくなる」という #438 型の劣化を自分で作る。また `string_agg(... ORDER BY pk)` は 1.28M 行でソートと巨大な文字列連結が要るうえ**照合順の違い**（ローカルは `Japanese_Japan.932`）で両端がずれるので、`sum(md5 の先頭32bit)` の**順序非依存**な形にする。

### 株価の保存（日次・週次の2本立て）と全件ロード

- **株価は close-only の2本立て**（`stock_price_daily`＝直近6か月日次 / `stock_price_weekly`＝全履歴週次）。価格の読み書きは必ず単一ヘルパ経由：書き込み＝`record_prices_batch`（daily upsert→週次再集約→trim）、エントリー価格＝`prices_on_or_after`（窓内daily・古ければweekly・daily空ならweeklyフォールバック）、最新値＝`latest_prices`（daily優先）。VWAP/相対流動性は `turnover_sum/volume_sum` から派生（保存しない）。
- **`legacy_stock_price_history_2026_02` は遡及調整が入っていない**（#490）。#481 B-0 で旧開発 DB から温存した日次 OHLCV（2024-05-17〜2026-02-20）で、現行 `stock_price_daily` に無い O/H/L を持つ唯一の資料だが、収集当時の生値で以後の分割・併合が反映されていない。**無検証で現行データと接合すると、分割のあった社の段差が「実際の値動き」として分析へ流れ込む**ので、使うときは重複期間の比率で係数を求めてから当てる（2026-02-20 以降の分割も別途要る）。消費者は現時点で無い。
- **満杯 DB で新旧テーブルを併存させない**：`DELETE`/`ALTER DROP COLUMN` はファイルを縮めず（解放は `VACUUM FULL` が要るが満杯では実行できない）、併存の瞬間に上限を超えて read-only に落ちる。旧表を `DROP TABLE`（即解放）してから新表を入れる。2026-06 の株価移行（`migrate_stock_price_dual.py`・撤去済み）の手順は [DEPLOYMENT_HISTORY.md](archive/DEPLOYMENT_HISTORY.md#容量設計-500mb-に対する見通しと一回限りの移行2026-06-完了)。
- **`stock_price_weekly` の全件ロードは分割 fetch する**（Issue #311）：単一クエリの全件取得は `statement_timeout` 超過や `lost synchronization with server`（接続破損）を起こす。週次ロードは **`macro_snapshots.load_weekly_prices_chunked`**（500社ずつ IN 句・PK インデックス）を通し、生の `.order_by().all()` を書かない。タイムアウトの緩和や直接接続への切替では接続破損は直らない。
  - **全履歴が要らない用途では、チャンク分割よりまず下限日付を掛ける（#418）**: 下限は `trade_date`（非インデックス列）ではなく **PK 第2列の `week_start`** へ掛ける＝`(edinet_code, week_start)` の範囲スキャンになる。**母集団（winsorize の対象）が変わるので必ず実測する**（#418 では1年以上株価が止まった社が抜けた＝除外が正しかった）。
  - **差分ロード（`weekly_price_cache`・#480）の不変条件は CLAUDE.md の設計制約と [ADR-0036](adr/0036-weekly-prices-incremental-load.md)**。挙動が怪しいときの切り分けは `FINAPP_WEEKLY_CACHE=0`（指紋クエリすら発行しない従来動作に戻る）。

### 日付・時刻の境界（JST）と株価の鮮度

- **データ鮮度は「全社の max」で測らない＝分位で見る（Issue #415）**: 銘柄別の最終 `trade_date` は一様でなく、max は数銘柄だけが持つ日付になりうる。max を代表値にすると、as-of バッジが緑のまま古いランキングを出し、`fill_recent_stock_price_gap_yahoo` が遅延銘柄の欠測を永久に埋められない（両方とも実際に起きた）。週次バーの欠落は例外を出さず、特徴量ごとに壊れ方が違う（インデックス参照の52週ラベルやローリング窓はずれ、日付カットオフの `get_momentum_return` は壊れない）。鮮度の測定・表示・補完の起点はすべて**銘柄別（p05/p50 と件数）**で扱う。
  ```sql
  SELECT percentile_disc(0.05) WITHIN GROUP (ORDER BY d) AS p05,
         percentile_disc(0.50) WITHIN GROUP (ORDER BY d) AS p50, max(d)
  FROM (SELECT edinet_code, max(trade_date) d FROM stock_price_daily GROUP BY 1) t;
  ```
- **毎晩の gap-fill は「閉場済みの最新 JST 営業日」（`last_closed_session()`）を基準にする。UTC の `date.today()` と比べない（#474）**: UTC 日付と比べると JST 日曜/月曜の早朝に全社が「1日遅れ」に見え、全社を取り直していた。
  - **観測値（全社の最頻値・p50）を基準にしてはいけない**。平日 JST 03:00 の run は前営業日バーの**唯一の取得者**で、実行時点では全社が横並びに未取得＝観測基準だと誰も取りに行かず鮮度が永久停止する。基準は「あるべきセッション」であって「みんなが持っている日」ではない。#415 が「鮮度の**測定**は分位で」と言うのと**向きが逆**なので混同しないこと。
  - **祝日は営業日として扱う（安全側）**。祝日カレンダーを持たないため祝日明けは全社取得へ落ちる。無駄だが、外したときに欠測が例外を出さずに残る skip 側へは倒さない。
  - **Yahoo の `interval=1d` は場中でもその日の途中経過バーを1本返す**。J-Quants 無料は直近12週を配信しないので暫定終値は訂正されず、対象社を絞ると翌晩の取り直しによる偶然の上書きも来ない。基準セッションより後の `trade_date` は取り込み時に落とす。実際に GHA の遅延起動（cron 18:00Z が 00:08Z へ 6時間ずれた run 31133560740・JST 09:08＝場中）が 3,749社ぶんの暫定バーを書いていた。
  - **`record_prices_batch` の戻り値は「投入行数」であって新規行数ではない**（`ON CONFLICT DO UPDATE`）。ログの「3,677件 追加」は実際には全社ぶんの取り直しだった＝**この数字を鮮度の指標に使うと空振りを検知できない**。正味の増分は別に数え、鮮度そのものは `price_freshness(db)` の p50 を run ログへ残して run 間で比較する。
  - **大引けは 15:00 ではなく 15:30**（2024-11-05 のクロージング・オークション導入で延伸）。`MARKET_CLOSE_JST` を 15:10 に置くと 15:10〜15:30 の**場中**に走った run が当日を「引けた」と誤判定する。定時実行が JST 17:17 になった（#476）ことでこの境界は実際に効くようになった＝16:00（大引け＋Yahoo 反映の余裕）。
- **収集の日付境界は必ず JST で判定する。`date.today()` はランナーの UTC 日付（#476・2026-08-09）**: GitHub Actions は UTC、東証と EDINET の「日付」は JST。UTC 18:00 起動（JST 翌03:00）では両者が1日ずれるため、UTC 基準の境界はことごとく1日ぶん保守的に外す。
  - **EDINET のスキャン終端がこれで1日損していた**。`end = date.today() - 1日` は JST では**前々日**を指し、受付（平日 9:00〜17:15）が終わって確定済みの JST 前日ぶんを丸ごと落としていた＝提出から分析反映まで約33時間。`latest_settled_edinet_date`（`EDINET_CUTOFF_JST=17:15` を過ぎていれば当日）＋ JST 17:17 起動で**約10時間**になる。差分収集（`skip_existing`）なので**取りすぎ側へ倒す**のが正解——締切間際の提出が一覧へ載るまでのラグで取りこぼしても翌日に再スキャンされる。
  - **起動時刻を動かす前に「その時刻にどのソースが確定しているか」を並べる**（暦は `docs/DEPLOYMENT.md`）。「早く回せば新しいデータが入る」は自明ではない。スケジュールは第一防御で、本防御はコード側（`last_closed_session`・進行中バーの除外）。
- **naive な `timestamp` 列へ aware UTC を書くと、入る値は「接続先のセッション TZ」で決まる（#565・2026-08-29・[ADR-0043](adr/0043-session-settings-travel-with-the-connection.md)）**: DateTime 列は `timestamp without time zone` なのに Python 側は `datetime.now(timezone.utc)`（aware）を渡している。psycopg2 は aware のまま送るので、**PG は naive 列へキャストする際にセッション TZ でローカル時刻へ変換して tz を落とす**。セッション TZ が UTC なら UTC naive、`Asia/Tokyo` なら **JST naive** が入る。表示側 `api._utc_to_jst_str` は前者を仮定して +9h するので、後者だと画面に **9 時間先の時刻**が出る（ダッシュボードが「バッチが走っていない 03:02」を最終更新として表示していた）。
  - **例外は出ない**（どちらの TZ でも接続でき、書き込みも成功する）。#503 でローカル PG（実測 `Asia/Tokyo`）へ移った 2026-08-20 を境に、同じ列に UTC と JST が混在した。
  - **表示だけでなく判定に効く**。`routers/market.py::days_since_update` と `routers/morning.py::_gap_ratio_block` の `age_days` が 9 時間新しく見える＝鮮度が甘くなる。さらに `nightly_scores._make_score_table_verifier` の `max(created_at) < started_at` は、9 時間進んだ値のせいで**常に通過する空検査**に化けていた（書けていなくても気づけない）。
  - **対策は `database.SESSION_FIXES` が接続時に `SET TimeZone = 'UTC'` を流すこと**（規則は CLAUDE.md の設計制約・[ADR-0043](adr/0043-session-settings-travel-with-the-connection.md)）。
  - **`app_settings` の足跡（`*_last_run`）は無関係**。tz 付き ISO **文字列**で保存しているので最初から正しく、`/api/morning` と `/api/stats` の batch カード（#561/#563）は正しい時刻を出していた。**この2系統が食い違わないことが、時刻まわりの唯一の交差検証**になる。`snapshot_date` 等の `date` 列も無関係。
  - **既存行の引き直しは `python -m scripts.fix_naive_jst_timestamps`**（既定ドライラン）。**先にコード修正を入れてから**走らせる（逆にすると次の夜間バッチがまた JST で書く）。**再発を実測で捕まえられるのは `tests/test_tz_postgres.py`（`FINAPP_TEST_PG_URL` 必須）だけ**——壊れ方が PG 固有で SQLite には再現できず、**CI では skip される**（ADR-0034 が `rowcount = -1` で当たったのと同型の限界）。

### 株価のスケール（1）J-Quants の調整後値と分割の修復

- **J-Quants V2 は必ず調整後フィールド（`AdjC`/`AdjO`/`AdjH`/`AdjL`/`AdjVo`）を使う**（Issue #314）：`C`/`O`/`H`/`L`/`Vo` は分割・併合を遡及反映しない未調整値で、分割日を境に系列が段差になる（`AdjC` 等は遡及調整済みなので自前でスケーリングし直さない）。`stock_price_weekly` は追記専用で、過去に未調整値で書いた行は再取得しても自動では直らない（該当週の daily が再収集されて `_recompute_weeks_from_daily` が走った週だけ更新される）。
  - **段差の残存量を実測してから直し方を決める（#465）**: 公式値（`AdjC`）と `weekly.close_last` の乖離は「ほぼ0」と「1%以上」に二分し、比率は分割比の逆数になる（閾値は 1% 前後ならどこに置いても同じ集合を拾う）。
  - **「全期間を公式値で置換」は解ではない**（大半が既に一致していて得るものが無く、未 trim の daily が積み上がる）。段差銘柄だけを取り直す `python collector.py --repair-price-breaks`（既定 dry-run・`--persist` で書込・上限超過で中止）を使う。**再発は前提**——weekly は append-only なので分割のたびに同じ段差ができる。だから自動化ではなく「いつでも回せる検出コマンド」として残してある。
  - **突合日をカレンダーから決め打ちしない**。weekly の `trade_date` は「週内最終営業日」で、祝日が入れば金曜とは限らない。**weekly に実在する `trade_date` のうち月内で最多の日**を選べば、非営業日を叩いて `JQUANTS_RATE_SLEEP` を捨てずに済み、突合できる銘柄数も最大になる。
  - **Yahoo は一部の分割を過去へ遡及反映しない＝修復ソースとして万能ではない**: 取り直しても残る社では DB と Yahoo が一致していて、Yahoo 自身が公式と食い違う（無償割当型の小規模分割で顕著）。**残存を見たら、まず DB と修復ソースが一致しているかを確かめる**（公式値で直す方法は [SCRIPTS_REFERENCE.md](SCRIPTS_REFERENCE.md) の `repair_splits_from_jquants` の節）。Yahoo の `quote.close` は分割調整済み・配当未調整で、J-Quants の `AdjC` と同じ規約。
  - **分割・併合の後で過去の株価は自動では調整されない＝夜間は見つけるだけで直さない（#1253・2026-11-16）**: 週次は追記だけで、過去の行を直すのは手で回す修復だけ。100 倍以上の段差の走査（#765）は 2〜10 倍の分割・併合を拾わないので、2026-10-01 付けの分割の 60 社と 6574 の 10:1 併合が 7 週残っていた（#1254 で `--repair-price-breaks --persist` により 61 社を修復）。夜間の [4/4] は契約窓から等間隔の 3 日だけ公式 `AdjC` と突合し（約 1 分）、**比が分割・併合の比にそろう社だけ**を「未調整の分割・併合: N社」として出し、watchdog が起票する（形だけでは選ばない）。`WITHHELD_OFFICIAL_ADJUSTMENTS` の社と `collector_utils.PRICE_BREAK_ACCEPTED`（確かめて直さないと決めた社・理由必須）は除いて数だけ出す。警告が出たら `python collector.py --repair-price-breaks`（ドライラン）で一覧を確かめ、見本の社で Yahoo の履歴が分割の比で調整済みかを読み比べてから `--persist` で直す。
  - **修復した週次は `financial_records` へ自動では伝播しない**。`update_market_data_from_history(db, point_in_time=True)` を同じ作業内で回すこと（本番実測 42,289件 / **44.2秒**・#464 の一括 UPDATE 化後）。併せて `scripts/.cache/weekly_prices_*.pkl` を退避する（#454/#456 のとおり検証キャッシュはデータ世代を持たない）。
  - **公式 `AdjFactor` は株式分配型スピンオフを持たない（#568）**: Yahoo（= DB）だけが権利落ち日より前へ調整を掛ける。見分け方は3つ——**公式 > DB の向き**（分割の取り残しは DB > 公式）・段差が権利落ち日にある・`AdjFactor` に対応するイベントが無い。**DB を公式へ寄せてはいけない**（週次リターンを使う M-1/M-2/M-6 には DB 側が正しく、寄せると権利落ち日に偽の暴落を作る）。`corporate_actions.SPINOFF_ADJUSTMENTS` へ根拠付きで登録すれば `--repair-price-breaks` と `repair_splits_from_jquants` の両方が公式値を DB のスケールへ換算し、**J-Quants の取得経路は登録済みの社の権利落ち日より前を書かない**（#651・ADR-0053 の追記）。新しいスピンオフは検出器に「公式 > DB・イベント無し」として現れるので、第3のソース（適時開示・初値）で裏を取って登録する。**登録すると分割補正係数 F も変わり、誤登録は VIEW の値を動かす**（#740・ADR-0055 決定4-9・ADR-0062）。F に入れた一覧は毎晩のログ「分割補正係数: 登録表のスピンオフ N 件を F に入れた」に出る。
  - **逆向きもある: 公式だけが調整し DB が正しい企業イベント（#652）**: 新株予約権無償割当（買収防衛策）では、公式 `AdjC` が権利落ち日より前へ理論係数を掛けるが、Yahoo は split として持たず DB と一致する。**エンバーゴ群（`post_window_adjustment`）は分割とは限らない**ので、`--repair-price-breaks --persist` の前に **DB と Yahoo が既に一致していないか**を見る。契約窓に入ると `repair_splits_from_jquants --apply` が実際の取引に無い跳ねを書く。**回避**: `corporate_actions.WITHHELD_OFFICIAL_ADJUSTMENTS` に「社＋日付の窓」で根拠付きで登録する（窓の中の公式イベントは書かれず、F の検出と TTM の分割窓からも外れる・#739・ADR-0062）。外した件数は毎晩のログ「分割補正係数: 保留の窓で外した公式イベント N 件」に出る。**比が分割にそろっても、権利落ち日の実際の値動きと開示を見るまで分割と決めない**（2例目の E05716・#1254 は、夜間の検知も `repair_splits_from_jquants` も「分割」と出した）。
  - **Yahoo で取り直すと、公式だけで直した補正が消える（#1279・2026-11-20）**: `repair_splits_from_jquants --apply` は Yahoo が調整しない企業イベント（自己株式を使った株式無償割当など）を公式 `AdjFactor` だけで週次へ当てるが、`--repair-price-breaks --persist` は該当社の**全履歴**を Yahoo で取り直すので、後で別の分割を直すとその補正も未調整へ戻る。実例は E00484（2814 佐藤食品工業）: 2025-12-29 の 1:0.1 の無償割当を 8/29 に公式で直した 335 週が、2026-09-16 の 1:2 分割の取り直し（#1254）で消え、夜間の検知（#1253）が「公式/DB=0.9091」として拾った。**今は取り直しを止めず、`--persist` の出力に「公式補正消失」と掛け直しのコマンドを出し、その社を印（`app_settings.splits_repaired_from_jquants`）から外す**——新しい分割は Yahoo で直す必要があるので拒まない。印は社ごとで、`--apply` は印に載る社だけを拒み（2度掛け＝係数の二乗を防ぐ）、書くときは他社の記録を残して足す（以前は印が全体で1つで、`--force` の掛け直しが他社の記録を消した）。
  - **`AdjC` を書いてよいのは `AdjC == C` の行だけ（#620・ADR-0053）**: 上の「必ず調整後を使う」は
    **J-Quants が唯一の書き手なら**正しい。実際には同じ列を Yahoo と J-Quants の2人が書くので（書き手と
    日付の分担は ADR-0053）、無償割当の社では**同じ列に2つのスケールが日付で交互に入る**。
    実測（2026-09-07）では E32779・E05716 の 2026-06-01〜06-15 の 11営業日が `close`＝`AdjC`・
    `volume`＝`AdjVo` と1桁まで一致し、帯の両端に**企業イベントではない段差**ができていた。
    **11営業日で元へ戻る企業イベントは存在しない**（分割でも併合でもない）。
    - **エラーは出ない。** どちらの値も妥当な株価で `ON CONFLICT DO UPDATE` は成功し、行数も鮮度も
      正常に見える。現れるのは M-1 / M-2 / M-6 のリターン計算の中だけ。書き手を示す列も無いので
      **どの行がどちらのスケールかは事後に判別できない**。
    - **`repair_splits_from_jquants` は帯を「エンバーゴ内の分割」と誤診する。** `validate()` の条件1
      （最新の測定比が 1.0）は**帯のおかげで**通り（DB が正しいからではない）、条件2（全段差が公式
      イベント日で説明できる）は**帯の左端で**落ちる。しかも公式値を取れる窓は契約窓で終わるので
      **帯の右端＝戻りは原理的に観測できない**。#568 が2度にわたって群分けを外したのはこれが理由。
    - **回避**: catchup は `AdjC != C` の行を書かない（`collector_prices._jquants_batch_gen`・判定を返ってきた
      行の中だけで完結させる理由と丸めの許容は ADR-0053 の決定）。
    - **既存の帯は仕組みの修正では消えない**。`python -m scripts.repair_scale_mixture --only <ec> --apply`
      が直近 `DAILY_WINDOW_DAYS` を Yahoo で取り直す。**順序を逆にしない**——先にデータだけ直すと
      次の晩の catchup が書き戻す。
    - **往復段差の検知だけでは本物を選べない**。全社走査では候補が 234社出て本物は2社だった
      （2026-09-08 実測）。帯の長さでも、帯の内側が一定比かでも分離できない（E32779 は帯の中で
      株価が 13% 動いている）。毎晩の判定は**その晩 `AdjC != C` を報告した社との交差**で行い、
      確定は**3条件**で行う: `AdjC != C`（分割がある）・帯の日の DB 値が `AdjC` と一致・
      **その日の Yahoo 値が `AdjC` と一致しない**。3つ目を落とすと**分割のあった高ボラ銘柄を
      必ず誤検知する**——`AdjC != C` は分割日より前の全期間で成り立つので「分割がある」と
      しか言っておらず、Yahoo がその分割を遡及調整していれば Yahoo 値 = `AdjC` となり、
      DB が Yahoo 由来でも2つ目まで成り立つ。実測: E01717（6834 日本工機）は 2026-05-15〜05-21 と
      06-10〜06-18 が確定と判定されたが、Yahoo が返す値は DB と1円まで同じで OHLC も内部整合
      していた（6/10 は `open 5436 / high 5474 / low 4940 / close 4986` の実際の値動き）。
      **Yahoo を取得できなかったときは確定させない**（取れないことを「一致しない」と読むと
      同じ誤検知に戻る）。
    - **社単位の交差だけでは、判定済みの非該当が毎晩鳴り続ける（#644）**。交差は3条件の1・3を
      見ていないので、分割のある社で実際の値動きが往復すると毎晩警告される（実測 9/8〜9/11 に
      E01332・E01717・E34165）。**回避**: 突合で非該当と判定した帯を記録し、夜間はそれを除いて数える
      （記録の鍵と記録先は ADR-0053・ARCHITECTURE.md）。夜間ログの「判定済みの非該当 N帯を除外」は 0 でも出る（出ない晩は記録の前の書式＝不明）。
    - **保持窓の端にかかった帯は、判定済みでも数日おきに鳴り直す（#1086）**。段差は前日の終値と比べるので、
      前日が保持窓から消えるとその日の段差も組めず、帯の始まりが後ろへずれて往き比も変わる＝鍵が変わる
      （実測 E40060: 始まり 04-22 → 04-30 → 05-01、終わり 05-11・戻り比 0.7561 は不変。#1039 が閉じた翌晩に
      #1085 が立った）。**回避**: 夜間は `oldest_daily_trade_date` を `exclude_judged_bands(oldest=)` へ渡し、
      終わりの日と戻り比が同じで記録の始まりが表の最古の日以前の帯を判定済みに数える。最古の日を
      「今日 − 183日」で代用しない（04-29 の祝日で 1日ずれ、E40060 は当たらなかった）。
    - **始まりの段差が保持窓から出ると、判定済みの帯の「すき間」が別の帯として鳴る（#1235）**。往きの段差を
      組めなくなると帯そのものが消え、余った戻りの段差が次の逆向きの段差と組み直される。できる帯は
      始まりが元の帯の終わりの次の営業日で、終わりも戻り比も別物なので、上の #1086 の照合は効かない
      （実測 E01717: 記録 05-15〜05-21・06-10〜06-18 に対し、05-14 が表から消えた 2026-11-14 の晩に
      05-22〜06-09 が鳴り、記録は 2 本とも当たらなくなった＝夜間ログの「判定済みの非該当」が 6帯 → 4帯）。
      **帯の内側の日は前の判定の範囲外**なので、照合を広げて黙らせず
      `python -m scripts.repair_scale_mixture --only <ec>` で突合し直す（1社20秒。非該当なら記録されて
      翌晩から消える。ドライランでも記録は書く）。組み直された帯の始まりが 2026-06-20 より後なら、
      下の構造除外（#773）に落ちて鳴らない。
    - **分割の波が来ると、記録の無い実際の往復がまとめて鳴る（#773）**。権利落ちで `AdjC != C` の社が
      急増し（実測 9/28 15社 → 9/29 68社）、4社・13帯が警告されて watchdog が `priority:high` で
      起票した（#770）。公式突合の結果は13帯すべて非該当で、うち9帯は**突合しなくても混在ではありえない**
      帯だった。**回避**: 夜間は開始日が `collector_utils.OFFICIAL_SCALE_WRITABLE_UNTIL`（2026-06-20・
      導き方は ADR-0053）より後の帯を、判定済みの除外より先に落とす（`exclude_unmixable_bands`）。
      ログの「2026-06-20 より後に始まる帯 N帯を除外」は 0 でも出る（出ない晩は #773 より前＝不明）。
      **境界を跨ぐ帯・境界以前に始まる帯は従来どおり突合が要る**（#770 の残り4帯）。
      **2026-12-21 に境界以前の行が保持窓から消え、以後この検知は構造上鳴らない**（退役は #775）。

### 株価のスケール（2）Yahoo の株式併合

- **Yahoo は株式併合（スクイーズアウト）を split として返し、終値が併合比率倍になる（#765・2026-09-29 実測）**:
  TOB 後の株式併合（例: 4,400,000株→1株）を Yahoo は split 1:4,400,000 として登録し、
  `quote.close` を比率で調整して返す（1909.T の 3,700円 → 16,278,046,720円・出来高0）。上場廃止で
  系列が止まるので、異常値が各社の「最新株価」として固定され、`/api/backtest`・`financial_records` の
  per/pbr/時価総額・週次の52週ラベルへ流れた。半年で5社（月1社前後）起きており、夜間は毎晩 `exit=0`。
  - **値の正体はビット単位で再現できる**: split 日以降の形は `float32(実値 × 比率)`、それより前の
    遡及調整の形は `float32(実値 ÷ round(1/比率, 10))`。比率で単純に割り戻しても呼値に戻らない
    （16,278,046,720 ÷ 4,400,000 = 3,699.556）。出来高0は「出来高 ÷ 比率」の丸めで、取引が無かった
    印ではない。Yahoo は登録後の新しいバーを先に比率倍で返し、手前の遡及調整は後日まとめて行う。
  - **回避（書く側）**: Yahoo の株価の書き手は、DB の直前値と100倍以上（1/100以下）離れた値を書かない
    （`collector_utils.scale_keep_mask` / `collector_prices.find_scale_break_against`。100倍の根拠・
    離れたバーだけ捨てる理由・弾いた社の記録と WARNING の出し方は ADR-0053 の追記）。夜間ログの「Yahoo スケール段差で不採用: N社・M本（うち新規 K社）」は 0 でも出る。
  - **回避（表の側）**: 夜間の最後に株価表（日次＝全行・週次＝全履歴）の100倍段差を走査し
    「株価スケール段差（≥100倍）: N社」を出す（`scan_price_scale_steps`）。
    どちらの行も `python -m scripts.check_nightly_collect` が警告として拾い、毎日 20:00 の watchdog が
    種類ごとに起票する（`WARNING_KINDS` の `scale_rejected` / `scale_steps`・#767）。
  - **既知の穴**: 株価を1件も持たない社で、応答の全バーが比率倍（応答の中に段差が無い）だと見逃す。
    逆に、何年も前の週次しか持たない社が100倍以上離れた値で再上場すると、止まったまま「既知」になる。
  - **既存データは `python -m scripts.repair_consolidation_prices`（台帳方式・既定はドライラン）で直す**。
    **Yahoo で取り直す修復コマンド（`repair_scale_mixture --apply`・`collector.py --repair-price-breaks
    --persist`）は使わない**——今の Yahoo は該当社の保持窓ぶんを比率倍で返すので汚染を広げる
    （台帳方式にした理由は SCRIPTS_REFERENCE.md の `repair_consolidation_prices` の節）。

### per / pbr の分割補正（ADR-0055）

- **`per` / `pbr` が「調整済み株価 ÷ 当時の1株指標」になっている（#653・#655・[ADR-0055](adr/0055-valuation-basis-mismatch-is-corrected-in-the-view.md)）**: 分割より前の年の行だけ per/pbr/market_cap が分割比ぶん小さく、div_yield と（`market_cap` を分母に持つ）nc_ratio/z_nc_ratio が同じだけ大きい。**エラーは出ず、最新行は基準が揃うので既定の画面には出ない**（例外は期末後分割の年の行・下記の #753）。出るのは学習パネル（M-1/M-2/M-6）と、**年度を指定した**スクリーニング・CSV・M-8・`/api/backtest`。
  - **補正は `financial_metrics` VIEW が当てる。`financial_records` の生値は歪んだまま**が正しい状態（F は行の固有値ではないので実列へ焼かない・ADR-0055 決定1）。
  - **`scripts/measure_split_valuation_bias detect` はこの方式の回帰テストにならない**（生値を読むので、補正後も同じ行を報告し続ける）。確認は `financial_metrics.per = ROUND(financial_records.per × split_factor, 2)` の突合（market_cap は `× split_factor × split_shares_lag`、div_yield は `÷ (split_factor × split_dps_lag)`）で行い、**join は `fm.id = fr.id` で取る**——`(edinet_code, year)` で結ぶと、同じ year に annual 行が2本ある社で別の行と比べてしまう。
  - **補正された行では `per <> stock_price / pl_eps` になる**。`stock_price` を補正しないのは**意図した非対称**（ADR-0055）で、適用した F は `financial_metrics.split_factor` で見える。消費側が重ねて掛けてはいけない。
  - **期末後分割の年の行は、F ではなく基準の遅れで直す（#753・ADR-0055 決定4-11）**: その行は1株指標が分割後なのに、株数（と多くの社で1株配当）が分割前のまま。F は「その行より後のイベントの積」なのでこの行は F=1.0 で、**`e.year >= y` にして F に混ぜると同じ行の正しい per / pbr を壊す**。補正された行では `market_cap <> stock_price × issued_shares / 1e6` になる（意図した非対称）。**係数表には F=1.0 で遅れだけを持つ行がある**ので、F の突合では `factor != 1.0` に絞る（`scripts/measure_split_leak.compare_factors`）。**VIEW を通らない `sector_ols` は読込（`_load_records`）で同じ遅れを当て、`market_cap` は生のまま読む**（#758・決定4-12。VIEW が `predicted_market_cap` に `shares_lag` を掛けるので、読込で直すと二重になる）。当て損ねても失敗は出ず、該当社の gap_ratio が割安側へ寄るだけ。
  - **`point_in_time=True` を回すと H1（半期）行にも歪みが焼かれる**。`financial_metrics_interim` は現在「市場データ未収集で nc_ratio 等は NULL（想定内）」だが、`_pipeline_gh.py` の全件 finalize は `period_end` を持つ**全行**を更新する。検出は annual 行だけなので、H1 に値が入った時点で補正の効かない歪みが生まれる。
  - **`bs_bps` の年次比では「分割があった」は言えるが「何倍か」は決められない（#656・ADR-0055 決定4-2）**: 内部留保や評価差額でも増えるので、倍率は独立な第3の信号から取る。**`per` の比は第3の検証に使えない**（株価の値動きが混ざる）。
  - **第2経路の倍率は翌年の `issued_shares` から取り、第3信号が取れない年のイベントは採らない（#659・決定4-3）**。突合には `--coverage partial` が要る（既定の `full` では「公式で判定できる」と「翌年の行がある」が排他になり分母が 0 になる。分母の規則は `MATCH_DENOMINATOR` が唯一の源）。**`partial` で測れるのは倍率だけで偽陽性率は測れない**ので、既定は `full` のまま。
  - **期末後分割は、利益が大きく動いた年に EPS 照合で落ちる（#751・#836・決定4-10）**: 期末後・提出前に分割を**決議**していれば（効力が提出より後でも）、その期の EPS / BPS だけが分割後の基準になる。提出時点で知りえた情報なので時点再現の漏れではない。整合度（`corporate_actions.consistency_ratio`）で見分けて入れる。**Yahoo や公式と照らすときは窓の幅に注意する**（効力日が期末+45日を越えうるので、`event_window` の外に同じ比の分割があるだけで「分割なし」に見える）。**続けて期末後分割した社は整合度が倍率と食い違って採られず**、夜間ログの「整合度照合: 倍率の食い違い」に出る。
  - **同じ年に期中の分割と期末後分割が重なると、第2経路は期末後の分しか F に入れない（#756・決定4-13）**: 期中の分を足す仕組みは `DEFAULT_IN_PERIOD_DECOMPOSE`。**Issue に書いた条件は代数で展開してから採る**（案1 は整合度の定義を代入すると「純資産比 ≈ 1」と同じ条件だった）。分割した年の行の遅れ（`basis_lags`）も、期中の分を除いてから判定する。
  - **純資産総額の前年比は深い割引の増資を分離できない（#657・決定4-4）**: 落とせるのは `bs_bps` と純資産総額が食い違う社だけで、閾値を下げると本物の分割を巻き込む。**許容（`DEFAULT_EQUITY_TOL`）を動かすなら `verify-sample --census`（契約窓内の全数突合・約 35 分）と、窓外で落ちる社の Yahoo split 照合をやり直す**（余裕は両側とも薄い）。偽陽性率は抽出では測れない。
  - **最新年の第2経路は公式 `AdjFactor` で倍率を取るが、表に行が無いことを「分割は無かった」と読まない（#661・決定4-5）**: 夜の取りこぼし・エンバーゴ中と区別できないので、無ければ採らない（取りこぼしは `python -m scripts.backfill_adj_factor_events` を回し直せば埋まる・冪等）。**`rebuild_split_adjustment_factors` から J-Quants を叩かない**。**測定器の CLI（`detect` / `verify-sample`）は公式を渡さない**——公式から倍率を決めたイベントを公式と突き合わせると定義上必ず一致し、一致率が黙って膨らむ。
  - **定番比の表（`CANONICAL_RATIOS`）に比を足すと、確かめた社以外の丸め先も動く（#669・決定4-6）**: 表に無い比は `unsnapped` として F に入らないか、近い定番比へ丸まる（どちらもエラーは出ない）。足すのは、本物の比を公式 `AdjFactor`（契約窓の内側）か Yahoo の splits（外側）で確かめ、足した前後で倍率が変わるイベントを全部確かめたときだけ。前後差は、`rebuild_split_adjustment_factors` と同じ入力に対して表だけを差し替え、`detect_events` / `cumulative_factors` を2回呼べば DB に書かずに出せる。
  - **同じ EDINET コードでも、上場廃止→再上場で別の実体の行が欠損年をまたいで隣り合う（#672・決定4-7）**: **判定を `gap_years` 単独にしない**（本物の分割にも欠損年はある）。判定材料は週次株価の空白（`listing_gap_in_pair`）で、**系列の開始が遅い**形と**系列が途中で途切れる**形の両方がある。収集の都合で週次が遅く始まる社もあるので、`rebuild_split_adjustment_factors` が毎晩ログへ出す「比べなかったペア」が増えたら中身を確かめる。
  - **公式 `AdjFactor` に分割が「無い」と読めるのは、その社の日次バーを実際に受け取った区間の中だけ（#668・決定4-8）**: J-Quants は扱わない社でもエラーを返さず 0 本で返し、`_jquants_fetch_code` は 429 が2回続いても `[]` を返す。公式の不在で外すのは、受け取った区間（`bars_spans`）がイベント窓を覆うときだけ。区間は `jquants_adj_factor_coverage` に**追記**する（上書きすると契約窓が進んだあとに古い期間の確認が消え、外した補正が黙って戻る）。
  - **歪みは未来情報のリークでありうる**。**だから補正すると rank-IC が下がるのが正しい**（理由と、ADR-0028 の昇格ゲートをそのまま当てるとリークを残す判断になる点は ADR-0055 決定7）。

### J-Quants API の応答とエンバーゴ

- **J-Quants の5桁 `Code` を4桁へ潰すときは普通株（末尾0）を優先する（#465）**: 5桁は「証券コード4桁＋種類1桁」で、末尾 `0` が普通株、それ以外は優先株・優先出資証券などの別クラス。収集側は `code[:4]` で `sec_code` に落として `edinet_code` を引くため、複数クラスを持つ企業では1社に複数行が対応する。旧実装の重複排除は**レスポンス到着順の先着勝ち**で、順序が変われば別クラスの終値が普通株の枠に入っていた（実測で先頭4桁が重複するのは 9434 / 5076 / 2593）。判定は `is_common_stock_code()`（collector_utils.py）に集約し、**日次収集の dedup と段差突合の両方で同じ規則を使う**——片方だけ直すと、突合側が種類株を拾って「段差でない銘柄」を修復対象にする。
- **外部APIの権限判定を「別エンドポイントの成否」に委ねない（Issue #425）**: 判定に使う信号は、それ自体が正常時に成功することを先に実測で確かめる（#412 のガードは、常に 403 を返すエンドポイントの成否に頼っていた）。**収集元Aの失敗が収集元Bをブロックしない構造にする**（鮮度を担う Yahoo の補完を先に置き、J-Quants の失敗は握って継続する・`_pipeline_incremental.py` Phase 4）。**「修正済み」の Issue も本番の実行を1周回すまで検証済みとしない**。
- **ステータスコードだけ見て理由を推測し、それを docs へ断定で書かない（#462）**: J-Quants は 400 と 403 のどちらにも複数の意味を載せていて、区別できるのはボディの文言だけ（分類と実装での扱いは [DEPLOYMENT.md](DEPLOYMENT.md#j-quants-api無料プラン) の J-Quants 制約表が正本）。#412 / #425 が「403＝カバー範囲外」と読んだ観測は、すべて契約失効（#461）だった。新しいステータスに出会ったらボディを実測してから分類を足す。**空振りを削る最適化が、別種の静かな劣化を作り込む**（400 を「非営業日」と一括りにすると、取りこぼしが平常時の INFO ログに埋もれる）。
- **止まっていた同期を復活させるときは、復活直後の1回目の結果を必ず検算する（#463）**: 長く止まっていた処理は再開時に大量の差分を吐き、その量が「溜まっていたぶん」に見えて異常を隠す。#462 の `is_active` 同期の復活では、エンバーゴされた `/equities/master` に載らない新規上場が delisted にされた（as-of と `sync_active_status` の対処は [DEPLOYMENT.md](DEPLOYMENT.md#j-quants-api無料プラン) の J-Quants 制約表）。件数だけ見て納得せず、内訳をサンプリングして裏を取る。判定は **「価格履歴の開始が as-of より後」**＝「新しい」ではなく「古い側が無い」で見分ける（前提: `stock_price_daily` の保持窓がエンバーゴ日数より長いこと）。
- **開示データの「齢」は障害ではない。`today − 84日` が仕様上の天井（#424 子4）**: J-Quants 無料プランの `/fins/summary` は 12週固定で配信を遅らせるので（`JQUANTS_DISCLOSURE_DELAY_DAYS = 84`）、どれだけ頻繁に収集しても `statement_disclosure.disc_date` の天井は動かない。株価の鮮度ゲートと同じ物差しで「3ヶ月前＝壊れている」と読まない。開示由来の特徴量は当日の売買判断には原理的に使えない。<br>なお予想対比サプライズの特徴量化層 `feature_disclosure.py` は #1290 で退役し（唯一の予定消費者 #323 は not planned）、画面に会社予想由来の数字が出る経路は今のところ存在しない。**それでも収集は止めない**——発行済株式数の J-Quants 経路（`ShOutFY`）と夜間の TTM 合成の会計基準判定（`ttm_composite._load_basis`）がこの表を読む（[ARCHITECTURE.md](ARCHITECTURE.md)）。

### Yahoo の取得（ティッカー・上場市場・絞り込み）

- **Yahoo のティッカーは既定で `{sec_code}.T`＝東証以外の単独上場銘柄はそのままでは取れない（#475）**: 株価を1件も持たない社を `.S`（札証）/`.F`（福証）でプローブすると取得できる社がある（解決は下の `yahoo_suffix`）。財務レコードを持つのに株価が0件の社は、px_* 特徴量を要求する全モデルから例外を出さずに落ちる。
  - **`.F` は Frankfurt と衝突する。`meta.exchangeName` を必ず突き合わせる**：`377A.F` と `6461.F` は HTTP200 で 61 バー返すが `exchangeName=FRA`＝同記号の欧州銘柄。454社中2社（**0.44%**）が誤爆した。採用してよいのは `SAP`（札証）/ `FKA`（福証）だけ。**この誤爆は「取れた」に見えるので、件数だけ見ていると別会社の株価を書き込む。**
  - **`YHD` は取引所ではなく Yahoo の空箱。「バーが0本」を1つの棚に畳まない**：HTTP200・meta あり・0バーの応答は `exchangeName` で割れる——地方取引所（`FKA`/`SAP`）の実名なら Yahoo が価格を持たないだけ（ただし取引所名・通貨・実名は廃止済みの記号にも残るので、上場の証拠にならない・#769）。`exchangeName=YHD`・通貨なし・名前なしは東証を廃止された社などのプレースホルダで、実在する上場ではない（名証 `.NG` も同じ形）。`scripts/resolve_price_suffix.py::reject_bucket` が `mismatch > empty > placeholder > not_found` の優先順で分ける。
    - **同じ英数字コードでも `.F` の意味が社ごとに違う**（福証の実名を返す社と、Frankfurt の別会社を掴む社がある）ので、コード形式で群を作らない。
    - **採用には出来高>0 のバーを1本以上要求する**（`decide_suffix`・約定の無い応答は `empty` の棚へ畳む）。廃止済みの記号に Yahoo が出来高0の幽霊バーを返すことがある（#769）。
  - **解決済みサフィックスは `companies.yahoo_suffix` に持つ。NULL は `.T`（#555）**：NULL の意味は「未解決＝`.T` で試す」の1つだけで、3状態目は作らない（#475 のバックオフ `crc32(edinet_code) % 7` のステートレス判定を壊すため）。ティッカー生成は `collector_utils.yahoo_ticker(sec_code, suffix)` へ集約し、`.T` の直書きが散り直さないよう `tests/test_price_suffix.py::TestNoRawTickerLiteralsRemain` が照合する。
  - **棄却理由は `companies.yahoo_probe_bucket` へ残す。「3状態目を作らない」は `yahoo_suffix` の話（#560・2026-08-30）**：`reject_bucket` の4分類（`mismatch` / `empty` / `placeholder` / `not_found`）は #555 からあったが**レポートに printf されて消えていた**ため、「取引所は分かっているのに絞り込めない」状態だった。別列へ持たせれば `yahoo_suffix` の NULL の意味（未解決＝`.T`）も #475 のステートレス判定も壊れない——**この列が答えるのは「なぜ未解決なのか」だけ**。
    - **`empty` の社には `.T` を一切叩かない**。Yahoo が期待した取引所（SAP/FKA）と実名を返すのにバーが0本（または約定0）＝札証/福証の記号として Yahoo に載っている社なので、7日に1回のバックオフで間隔を空けても東証には永久に当たらない（廃止済みの記号も同じ形を返す＝実在の証明ではない・#769）。**間隔の問題ではないので、間隔で解こうとしない。** 正しい取引所（`.S`/`.F`）での再プローブは月次（`resolve_price_suffix --apply`）が担う。
    - **`placeholder` / `not_found` は従来どおり7日に1回**＝取得手段が無いだけで将来 Yahoo が載せる可能性は残る。ここを一緒に諦めると #475 の資産を捨てることになる。**ただしこの再取得は `.T` しか叩かない**ので、Yahoo が後から札証/福証の記号で載せ始めたかを拾えるのは月次の再プローブだけ。
    - **月次は未解決の全バケットを回す（#841）**。`--reprobe`（解決済みも測り直し、棄却なら接尾辞を外す）は月次では使わない（下の #769 の手順で人が回す）。予算の現在値は `scripts/run_monthly.py` の `BUDGET_MIN`。**ステップを足すときは必ず `window_problem()` を通すこと。**
  - **夜間の「解決済みなのに空」は `--reprobe` で直す。株価を持つ解決済みも対象、確定した棄却なら接尾辞を外す（#769・2026-10-01）**：解決できた社には翌晩から株価が入るので、株価ゼロ限定だった旧 `--reprobe` は**解決済みの社に永久に届かなかった**＝警告が案内する手順が警告の対象に届かない。しかも解決済みは廃止社の見送り（#556）を素通りするので、Yahoo が記号を消した社を毎晩叩き続ける（1734 は少なくとも 9/16 から毎晩鳴っていた）。**株価を持つ未解決の社は対象に入れない**（東証と福証の重複上場が `.F` に切り替わる）。**429・5xx・404 以外の 4xx・分類不能を踏んだ社は外さない**——外した社は「株価を持つ未解決」になって `--reprobe` の対象から消える＝元に戻す経路が無い。
    - **誤採用の幽霊バーは財務行まで汚す**：夜間の `_update_market_data_latest` は各社の最新の財務行へ最新の日次終値を押すので、1734 の 2026-07-17・657円は **2023-03 期の通期行**へ `stock_price=657`・`market_cap=427.05`（＝657×650,000株）として入った。`point_in_time` の再計算は株価の無い行の既存値を保持するので**消えない**。接尾辞を外すだけでは足りず、株価行の削除とその2列の NULL 戻しが要る。
  - **名証は Yahoo が持っていない**：`.NG` は HTTP200 だが **`timestamp` 0行・`exchangeName=YHD`**（`.N` は 404）。#438 の `^BCOM`（200 OK・close 全 null）と同型で、**例外を出さない壊れ方**。英数字コードの一部（231A/242A/259A/272A/353A/377A/391A/418A）も `.T`/`.TYO`/サフィックス無しの全てが 404 ＝ Yahoo 未収録（対照の `141A.T`・`147A.T` は 200 なのでコード形式の問題ではない）。**この2群は現時点で取得手段が無い**（J-Quants は東証のみ・stooq はブロック済み）。
  - **`companies.is_active=False` を「上場廃止」と読み替えない**：J-Quants `/equities/master`（東証銘柄）との突合結果でしかなく、真の廃止・地方取引所の単独上場・#463 のエンバーゴを1つのフラグに畳んでいる。`companies.delisted_date` も照合が走った日であって廃止日ではない。`companies.market` は #1015 以降、最新の断面（`jpx_listing_snapshots`）から毎晩埋めるが、断面に載らない社は NULL のままで、値は「今の市場」なので過去の時点には使えない（時点が要る用途は `jpx_listing_snapshots`）。
    - **地方単独上場は株価で判定する（#779・2026-10-02）**：マスタは東証しか載せないので、`yahoo_suffix` 解決済みの37社（札証15・福証22）は初回同期で一括 delisted になり、復帰の条件（マスタに再び載る）を永久に満たさなかった——株価は毎晩入っているので #605 の価格停止にも掛からず、**`is_active` だけが推奨・売却・ギャップ・ネットキャッシュから静かに落としていた**。`sync_active_status` は「`yahoo_suffix` 解決済み・最終足が `PRICE_STALE_ALERT_BDAYS` 以内・as-of より後に出来高>0 の足が1本以上」の社を delisted にせず、delisted なら復帰させる（戻り値 `price_alive`）。**復帰だけ足すと翌晩また delisted に戻る**ので両側に効かせてある。
    - **出来高>0 の窓を10営業日へ縮めない**：地方株は Yahoo が毎営業日バーを返すが約定の無い日は出来高0で、実測では約定が最大29営業日空く社があり、10営業日を超える空白を持つ社が37社中5社。縮めると生きている社が delisted と復帰を繰り返し、そのたび `delisted_date` が書き換わる。窓はマスタが証言できない区間（as-of より後）から取っている。出来高>0 を外すと幽霊足（#769）で廃止社が戻る。
    - **東証の社（接尾辞 NULL）は株価で救わない**：as-of 直後に廃止した 3593 型（#463）まで上場扱いに戻りうる。東証はマスタが証言できるので従来の `protected` だけで判定する。**`master_as_of` が無い晩はこの保護も掛からない**（37社がその晩だけ delisted に落ち、次に as-of が取れた晩に戻る）。
    - #779 後も `is_active=False` には**真の廃止と #463 の誤検出**が残り、`delisted_date` は検出日のまま（廃止日ではない・#785）。地方単独上場の業種は EDINET コードリストの提出者業種で埋める（#784・「業種データの取得方法」節）。
- **Yahoo は「絞られた」ことを例外で教えない（Issue #556）**: `fetch_yahoo_chart` は全エラー経路を `([], {})` へ畳むので、429（レート制限）と「データが無い」が区別できない。`yahoo_http_stats()` で status code 別に数え、`fill_recent_stock_price_gap_yahoo` の完了ログと戻り値へ `429 / 5xx / 4xx / その他` を必ず出す（**0 のときも出す**）。**関数の署名は変えない**（macro・weekly backfill・probe からも呼ばれる）。並行度は `YAHOO_STOCK_CONCURRENCY`（既定 4・`FINAPP_YAHOO_CONCURRENCY` で上書き・`1` で逐次と同じ挙動）。絞りすぎの判断材料は `new_rows`・`price_freshness` の p50/p05・429/5xx の件数で、`upserted` は投入行数なので使わない（#474）。**1回の実測を基準線にしない**。**4xx は 404 を内訳として分けて出す（`4xx=N（うち404=M）`）**——毎晩の 4xx の大半は上場廃止社への 404 なので、`scripts/check_nightly_collect.py` は **404 以外の 4xx を警告**にしている。
- **並行フェッチにすると「どこまで入ったか」が銘柄順でなくなる（Issue #556）**: `_yahoo_batch_gen` は `asyncio.as_completed` で**完了順**に yield する（#736 で撤去した旧 `_stooq_batch_gen` と同じ形）。DB は upsert なので最終状態は変わらないが、途中で落ちた回の部分反映は銘柄順に並ばない。**`records` の組み立てと `n_new` / `n_exchange_rejected` の加算はタスク側へ動かさない**——動かすとカウンタの意味が並行度に依存する（`n_new` は「その社の従来の最終日より後の日付」を数えるので、社と `last_iso` の対応が崩れると静かにずれる）。

---

## 半期（H1）の行を材料に使うときの穴（#424 子2・ADR-0051）

TTM 合成（`ttm_composite.py`）の実装で実測して分かったこと。**どれも例外を出さず、値だけが
おかしくなる**種類なので、H1 の行を読む新しいコードを書くたびに踏み直しうる。

- **H1 行の 1株純資産・配当・従業員は入っていない**: 実測（2026-09-18・候補 19,134 組）で `bs_bps` は
  99.0% が空、`dps` と `employees` もほぼ全行が空。BPS が要るなら合成する（前期通期の BPS × 純資産の比）。
  **純資産総額 ÷ 株数で作り直すと、自己株式を持つ社で系統的に小さく出る**（提出値の BPS は自己株式を
  除いた株数で割っている）。
- **通期行の `bs_equity_parent` は約 93% が空**: 純資産を読むなら `bs_total_equity` を先に見る。
- **H1 の BS は科目をまとめて出す**: 前期の通期にあって H1 に無い列は、建物 38.7% / 機械装置 67.5% /
  投資有価証券 39.1% など（2026-10-15・#900/#918/#936 の取り直しの後）。**`financial_metrics` は `net_cash` で
  投資有価証券の欠けを 0 として扱う**（下の項目）ので、H1 由来の値をそのまま流すとネットキャッシュが過小に出る。
  「表示の省略」と言える根拠は**同じ社の翌年の通期にその列があること**（建物・機械装置・投資有価証券は 97〜99%）。
  研究開発費は H1 の 43.3% で空（うち 91.0% は翌年の通期にある＝半期では注記に出ないだけ）で、空のままにすると
  `rd_intensity` を選んだ断面から 4 割の社が落ちる。
  **借入金は引き継がない（#946）**: H1 で空欄の借入金が翌年の通期にもあるのは、有利子負債 6 列の合計で 29.0%（61/210）
  しかない。返済でゼロになる科目なので、空欄の 7 割は「表示の省略」ではなく「返し終えた」で、前期の値を引き継ぐと
  TTM 行の D/E を過大にする。逆向きの誤りは残る（VIEW は `de_ratio` で欠けを 0 として扱うので、残り 3 割の社では
  借入金を 0 と読み、D/E が過小に出る）。誤る社が少ない側を選んだ。09-18 の「短期借入金 69.1%」は H1 に単体の値が
  混ざっていた状態の測定で、比べる基準に使わない。
- **D/E とネットキャッシュは「空欄＝0」と「空欄＝未観測」の境界で決まる（#915・2026-10-11）**: 有報は無い
  科目を載せないので、**無借金の会社は借入金の行が無く空欄になる**（最新の通期で約 890 社・自己資本比率の
  中央値 74%、他の社は 53%）。そのため VIEW は負債合計が載っていれば借入金の空欄を 0 として足し、
  **有利子負債の構成要素（`database.INTEREST_BEARING_DEBT_COLUMNS`・#926）と負債合計がすべて空欄の行だけ
  `interest_bearing_debt` と `de_ratio` を NULL にする**。「構成要素がすべて空欄なら NULL」に
  すると無借金の会社が factor premia・M-2/M-6 から大量に外れて母集団が偏る。逆に 0 のままだと、#900 の後に
  負債合計が空欄になる US-GAAP の社が無借金に見え、空欄の社を外す AND を素通りする。`net_cash` は流動資産と
  負債合計の**両方**を要する（流動資産の区分を持たない銀行・保険は NULL。以前は「−負債合計」だけが残り、
  比率が平均 −34 倍だった）。同じ規則が `plugins/net_cash_analysis.py::compute_net_cash` / `compute_ncav` と
  `collector_financials.calc_derived` にもある＝**変えるなら SQL 3 本と合わせて揃える**（画面のネットキャッシュ
  分析・売りランキング・バックテストは VIEW の列ではなく Python 側で計算する）。
- **銀行業・保険業の D/E は空欄（#925・ADR-0067）**: 銀行は借入を「借用金」で載せ、保険も借入金の区分を持たないので、
  上の「負債合計があれば借入金の空欄は 0」で D/E = 0（無借金）に見えていた（通期 782 行のうち 641 行）。VIEW 3 本の
  `de_ratio` は `COALESCE(d.industry, '') NOT IN ('銀行業', '保険業')` で外す（`interest_bearing_debt` 自体は外さない）。**業種名の唯一の源は
  `database.DE_RATIO_EXCLUDED_INDUSTRIES` だが SQL へも写してある**（テストが一致を照合する）——JPX が業種名を変えると
  黙って効かなくなる。`COALESCE` を外すと業種が NULL の行で `NOT IN` が NULL を返し、関係のない社の D/E まで消える。
  収集直後で `fr.industry` が空の行は、その晩の業種の書き写しまでは D/E が出る。
- **IFRS の行の `bs_short_term_debt` / `bs_long_term_debt` は「社債及び借入金」の流動・非流動（#925）**: IFRS の連結 BS は
  借入金を `jpigp_cor` の合算の行（社債及び借入金・有利子負債・借入金・長期債務）で載せ、日本基準の借入金タグは単体の
  文脈にしか無い。合算の行を2列へ別名として登録したので、**IFRS の行だけ2列が社債と1年内返済分を含む**（日本基準の行は
  別の列）。独自拡張のタグ（日立・ソニーの1年内償還の長期債務、ホンダの資金調達に係る債務、NTT、日本製鉄、ノンリコース
  ローン）は登録していない＝これらの社の有利子負債は小さく出る。日本基準でも電力会社は1年内返済分を電気事業専用の
  `CurrentPortionOfNoncurrentLiabilitiesCLELE`（１年以内に期限到来の固定負債）で載せ、登録していない（東京電力 2026 で 2,648億円）。
  **IFRS の行の新しい3列と `bs_bonds_payable` は空欄でなければならない**——2列が社債を含むので、ここに単体の社債
  （IFRS の書類で日本基準の `BondsPayable` が出るのは単体の財務諸表だけ）が残ると有利子負債が二重に数えられる
  （トヨタ 2026 の単体の社債 9,795億円）。通常の取り直しは空欄で上書きしないので消えない。#918 の取り直しを
  `refetch_financials --clear-nonconsolidated`（#900 の掃除と1回にまとめた）で走らせたのはこのため。
  **連結を作らない IFRS の社**は同じ書類に IFRS（`jpigp_cor`）と日本基準（`jppfs_cor`）の表を単体の文脈で両方載せ、
  接頭辞を見ない XBRL_MAP では同じ借入が「借入金（流動）」と「1年内返済予定の長期借入金」の2列へ入っていた
  （ベイカレント S100TI4B ほか 3社・24行）。IFRS の書類では有利子負債の列に `jppfs_cor` を割り当てない
  （`collector_financials._skips_jgaap_debt_tag`・#944）。IFRS の行で新しい3列・社債の列が埋まっていれば読み方の誤り。
- **H1 行の `per` / `stock_price` などの市場データ列は使わない**: `per` は半期 EPS で割った約2倍の値、
  `div_yield` は中間配当だけ。書き手は手動の全件実行（`_update_market_data_point_in_time`）だけなので
  鮮度もまちまち。合成した行では株価を当て直して計算する。
- **期末の後・提出の前に分割があると、EPS だけが遡って直り `issued_shares` は期末の株数のまま残る**:
  株数比だけを見ると「動いていない」ので分割を見落とす。「純利益 ÷ EPS」で逆算した株数と併せて見る
  （実測 222 件がこれだけで見つかった）。逆算は `|EPS| >= 1 円` のときだけ（提出値は小数第2位まで）。
- **`statement_disclosure` から会計基準を読めるのは 2024-07 以降**: J-Quants 無料プランは 84 日遅れで、
  最新の半期はエンバーゴ中。しかも前期の通期と今期 H1 しか比べず、前期 H1 の食い違いを見落とす。**会計基準は
  `financial_records.accounting_standard`（#859 の後に全行埋まった）で材料 3 つを比べる**（#966。開示の基準は
  連結・単体の判定と、列が空欄のときの予備）。
- **検出器の定番比へのスナップを「分割か」の判定に使わない**: `snap_to_canonical` は残差 0.8〜1.25 を
  「複合」として定番比へ寄せるので、**株数比が 0.88 以上ならほぼ全件が「定番比に乗った」になる**。
  分割の判定に使うのは 1.4 倍（`DEFAULT_MIN_RATIO`）以上の変化・検出器のイベント・公式 `AdjFactor`。

---

## ローカルバッチの実行環境（Windows・タスクスケジューラ）

2026-09-01、月次バッチ（`financial_app-monthly`）のタスクスケジューラ経由**初実走**で表面化した罠。いずれも「所要が伸びた」「exit code が非ゼロ」という形でしか現れず、原因はログに残っていなかった。

- **Smart App Control が未評価 DLL を初回ロードでブロックする（`macro_beta` が 1.4分・exit=1）**: `ImportError: DLL load failed while importing _ifrt_proxy: アプリケーション制御ポリシーによってこのファイルがブロックされました`。SAC は Enforced（`(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState` が `1`）で、8/21 の jaxlib 更新で入った `_ifrt_proxy.pyd`（8,704バイト）を弾いた。CodeIntegrity ログ（`Microsoft-Windows-CodeIntegrity/Operational` の **3118 / 3077 / 3033**）に記録は**その1回だけ**で、以後は同じ DLL の import が通る＝**未評価バイナリを1度弾いてから評価を取得する一過性の挙動**。
  - **問題は「月1回しか走らないバッチに当たった」こと**。初回ロードの1回きりの失敗が `macro_beta_loadings` の1か月ぶんの固着になる。`deps_smoke` ステップ（`scripts/check_heavy_imports.py`）を月次の先頭付近へ置き、**未評価 DLL の初回ロードを本番ステップの手前で消化する**。それでも落ちるなら数百分の予算を待たず起票される。
  - **pip でパッケージを更新したら、対話セッションで一度 import して評価を通しておく**（`python -m scripts.check_heavy_imports`）。バッチはセッション0（S4U）で走るので、そこで初めて触るのが一番まずい。
  - **既に読めていた DLL の判定も反転する（#782）**: 2026-10-02 01:00 の `monthly-beta` 月次で、10/1 01:00 の `deps_smoke` では読めていた `jaxlib/mlir/_mlir_libs/_mosaic_gpu_ext.pyd`（8/21 導入・中身不変）が遮断され、**対話セッションで import しても通らなかった**。1ファイルずつ `LoadLibrary` すると遮断は `_mosaic_gpu_ext.pyd` と `_mlirDialectsNVGPU.pyd` の2つだけ（どちらも NVIDIA GPU 専用）。同日 02:42 を最後に遮断の記録は止まり、03:15 には自然に読めた。逆向きもあり、lightgbm は 9/28 に遮断（WinError 4551）→ 10/2 には import 可。**判定は時間とともにどちらへも動く**ので、上の「対話で一度 import」は初回ロードにしか効かない。
    - jax は CPU でも `_mosaic_gpu_ext` を起動時に無条件 import する（`jax/_src/lib/__init__.py`）ので、GPU 専用の1ファイルで jax・numpyro 全体が立ち上がらなくなる。**`jax_import_guard.py` が GPU/TPU 専用の拡張に限り、SAC の遮断で落ちたときだけ空モジュールで代替する**（遮断された DLL は実行しない＝使わない部品を読まないだけ）。`deps_smoke` は代替を `[warn ]` 行で残し、失敗にしない。一覧の5つを全部空にしても CPU の NUTS は動き、検算モデルの結果はビット一致した（jax 0.10.2）——**jaxlib を上げたらこの確認をやり直す**。
    - **CPU の部品も遮断される（#789）**: 2026-10-03 01:00 の月次 M-1 で `jaxlib/cpu/_sparse.pyd`（CPU の疎行列カーネル）が遮断され、02:13 には読めた。これは**空モジュールでは救えない**——numpyro が import 時に `jax.experimental.sparse` を読み、その冒頭（`_lowerings.py`）が `cpu_sparse.registrations()` を無条件に呼ぶので AttributeError で落ちる。`jax_import_guard.STUBBED_EXTENSIONS` が**import 時に読まれる属性だけ**を持つスタブ（`registrations()` が `{}`）で代替する。カーネルが XLA へ登録されないだけで、NUTS（2チェーン・seed 0）は代替なしとビット一致した。カーネルを本当に使う BCSR 積は `NOT_FOUND: No FFI handler registered for cpu_csr_sparse_dense_ffi` で止まる（静かに誤らない）。import の段までは `tests/test_jax_import_guard.py` の結合テスト（Windows・numpyro 導入時のみ）が縛る。
    - **`deps_smoke` はバッチが使う依存だけを確かめる（#789）**: M-1 と月次本体は jax / numpyro / pymc を一切 import しない（使うのは `macro_beta_inference.py` だけ）ので `--profile base`（numpy / scipy / pandas / sklearn / statsmodels）。月次 beta と日中枠の beta / bench は既定の `inference`（推論系＋`jax.devices()`）。#789 は**M-1 が使わない部品の遮断で M-1 が失敗扱いになった誤報**で、本体の探索は exit=0 で μ̂ まで永続化していた。
    - **一覧外（スタブも無い CPU の部品）が遮断されたら従来どおり失敗する**。直す手段はコード側に無く、時間を置いて `python -m scripts.check_heavy_imports` で戻ったかを確かめてから回し直す。
      - **ローカルの全件 pytest でも、上の結合テストが遮断された `.pyd` を名指しして失敗する**（skip にしない・#792。CI は win32 限定かつ numpyro 未導入で常に skip）。かつては `pytest.importorskip("numpyro")` が ImportError 全般を skip に変えたため、2026-10-03 の `_chlo.pyd` 遮断（CHLO は CPU の lowering でも使う方言で一覧へ足せない）の最中も全件 green で通り、jax のバッチが今動かないことが見えなかった。導入の有無は `find_spec` で import せずに見る（テストの `_isolate` がガードを外すので、pytest 本体で import するとガードで救える遮断まで落ちる）。
    - **CodeIntegrity ログ（`Microsoft-Windows-CodeIntegrity/Operational`）は 1MB・約4時間ぶんしか残らない**（別アプリの 3033 が30秒ごとに流れて上書きする）。翌朝に見ても遮断の記録は消えている＝**判定の変化の時系列は `deps_smoke` のログにしか残らない**。調べるなら当日中に `Get-WinEvent` で取る。
  - **Smart App Control は OFF にしない**。一度切ると Windows の再インストール無しに再び ON にできず（不可逆）、マルウェア防御が恒久に一段下がる。1回きりのブロックに対して代償が大きすぎる。
- **`venv\Scripts\python.exe` はランチャースタブ＝`Popen` の pid を測ると実体を測り損ねる**: 子の常駐メモリを親から測ると、実体が 313MB でも **4MB**（ページフォールト千件台）と返る。エラーにならず「静かに正しく見える」のが厄介。実体はベース Python（`AppData\Local\Programs\Python\Python313\python.exe`）の**孫**として動く——CodeIntegrity のイベントもプロセス名をベース Python で記録する。**計測はプロセスツリーの合計で行う**（`sysmem.tree_rss_mb`）。`taskkill /F /T` がツリーごと落とす必要があるのと同じ根。
- **CPU が余っていてもメモリが枯れれば止まる**: `tune:macro_risk_return` は **156分間 heartbeat を出し続けながら探索候補を1件も進めなかった**。CPU は 39%（i5-8400・6コア6スレッド）で余っており、空き物理メモリが 0.6GB（総量 7.9GB）まで枯れて WorkingSetSize が測るたび減っていた（1974MB → 1013MB → 799MB＝ページアウト）。**所要だけを記録したログからは「遅い」と「止まっている」も、その原因も読み取れない**。`batch_common` の heartbeat と `env_lines()` は `sysmem.format_line()` でツリー常駐量と空き物理メモリを刻む。
  - **`heartbeat` は生存を示すが進行を示さない**（既知）。今回はそれが実害として出た最初の例で、予算 250分は進捗ゼロでも満額待つ。**資源はログに出ていなければ無かったことになる**。
  - GHA（2コア7GB）とローカル（6コア8GB）を所要で比べるときは、**空きメモリが前提条件**。`scripts/bench_macro_beta.py` の `env_fingerprint()` は `mem_total_mb` / `mem_avail_mb` を残す。
- **`Stop-ScheduledTask` は S4U タスクのプロセスツリーを落とさない（2026-09-01 実測）**: タスクの `State` は `Ready` に戻り `LastTaskResult` も `267014`（終了処理中）になるのに、**python のツリーは走り続ける**（ログへの書き込みが継続する）。さらに悪いことに、孤児化したツリーは**非昇格の対話セッションからは `taskkill /F /T` できない**（`Access is denied`）——同じユーザーでもセッション0の S4U ログオンはトークンが別だから。**タスクが Ready に戻ると窓（`ExecutionTimeLimit`）による打ち切りも効かなくなる**ので、止める手段が予算 kill だけになる（親の `Runner` は生きているので `Step.budget_min` は機能し続ける）。**止めるなら最初から管理者権限で `taskkill /F /T /PID <ルート>`**。`Stop-ScheduledTask` を「安全な停止」と思って叩くと、止まったつもりで走り続ける状態を作る。
  - **`Start-Process -Verb RunAs` が UAC でキャンセルされても、PowerShell 全体の exit code は 0 になりうる**。`The operation was canceled by the user` は本文にしか出ない＝**exit code だけ見ると成功に見える**（`pytest | tail` が pytest の失敗を隠すのと同型）。昇格を伴う操作は、実行後に**状態そのもの**（プロセスが消えたか）で確かめる。
- **セッション0（S4U）と対話セッションでは挙動が変わる**: 上記の jax は**対話セッションでは正常に import できた**（0.10.2）。逆に、対話セッションから他セッションのプロセスは `OpenProcess` できないので pid 指定の計測が拒否される。**「手元で試したら動いた」はバッチで動く証明にならない**（`.env` や PATH が変わりうる #550 と同型）。S4U で登録する理由は [SCRIPTS_REFERENCE.md](SCRIPTS_REFERENCE.md#scriptsinstall_nightly_taskps1)。
- **日中キューに積んだ探索スクリプトは、待っている間にキャッシュが退避されると即死する（#674・2026-09-14）**: `candidate_bakeoff._load_prices` を使う系統（`momentum_gate` ほか探索・bakeoff 系）は、`scripts/.cache/weekly_prices_close.pkl` が無いと既定で読み込みを拒否して `exit=1` になる。9/7 に `gate:interactions` を積んだ時点ではキャッシュがあったが、9/8 に #620 の修復で `_stale_pre620/` へ退避され、9/14 に順番が来たジョブは 0.1分で落ちた。結論を出した失敗なので**キューに戻らない**（#639 の in-flight 回収は「プロセスごと消えた」場合だけ）。
  - **回避**: 日中枠の `Job.argv` には `--allow-full-pull` と `--refresh-cache` を**両方**書く。前者だけだと株価だけ新しい世代になり、世代の印を持たない財務・マクロのキャッシュ（このときは #655 の分割補正より前）と混ざったパネルを測る。書き忘れは `tests/test_run_daytime.py::TestJobsBuildTheirOwnInputs` が落とす。
  - 「97万行の pull はストールしやすい」という拒否理由は Supabase 時代のもの。ローカル PG からの全件読込は**実測 12.5秒・131.8万行**（2026-09-14）。
  - **キャッシュを `_stale_pre<N>/` へ退避したら、日中キューに探索系の仕事が残っていないかも見る**（`python -m scripts.run_daytime --queue`）。

## 認証・セキュリティ実装メモ

- **【実装済み（Tier3-3）】** 認証を HttpOnly Cookie 方式へ移行（`localStorage` 廃止＝XSS によるトークン盗難を防止）。`auth_token`（HttpOnly）＋`csrf_token`（JS可読）の2 Cookie、`SameSite=Lax`、本番は `COOKIE_SECURE=true` で Secure 属性。`Authorization: Bearer` は廃止。
- **【実装済み（Tier3-3）】** 非冪等メソッド（POST/PUT/DELETE/PATCH）に CSRF Double-Submit（`X-CSRF-Token` ヘッダ == `csrf_token` Cookie）を要求。`/api/auth/` 配下は免除（ログイン前のため）。フロントは各 `apiFetch` が `csrf_token` Cookie を読みヘッダ付与。ログアウトは `POST /api/auth/logout` で Cookie 削除。
- **【実装済み（Tier3-1）】** 重い処理（収集・分析）と認証に `slowapi` でレート制限を導入（収集 3/分・分析 20/分・ログイン 10/分・リセット 3/分・単一更新 10/分）。IP単位（`api._rate_limit_key`。Render 越しの数え方は下の #1096）。`APP_RATELIMIT_ENABLED=false` で無効化可能（テスト時等）。環境変数名は slowapi 予約キー `RATELIMIT_*` との衝突を避けるため `APP_` 接頭辞必須。
- **【#1096】Render 越しの接続元は X-Forwarded-For の末尾から数える**: Render では uvicorn が `X-Forwarded-For` の**先頭**を接続元（`request.client`）として採る（全部の中継を信じる設定で動いている。Environment に `FORWARDED_ALLOW_IPS` は無いので、Render 側の既定と読める・未確認）。先頭は送り手が自由に書ける値なので、素の `get_remote_address` だと**偽の `X-Forwarded-For` を毎回変えるだけでログイン 10 回/分の上限をすり抜けられた**。issue の起票時は「接続元がいつも中継の IP になり全員で1枠」と見ていたが、2026-11-01 の実測で逆（すり抜け）だった。Render は受け取った値の後ろへ **[送り手, Cloudflare の端（`104.23.x.x` 等・毎回変わる）, Render 内部の中継（`10.x.x.x`）] の3つ**を書き足す（偽の値はその前に残るだけ）ので、`api._rate_limit_key` は末尾から `APP_RATELIMIT_XFF_HOPS`＝**3** 番目を採る（Render の Environment に設定済み・0＝従来どおり・足りない/IP でなければ接続元へ倒すが、Render では中継が必ず3つ足すので来ない）。**`--forwarded-allow-ips='*'` は解決にならない**——`'*'` のとき uvicorn が採るのはまさに先頭（`uvicorn/middleware/proxy_headers.py` の `get_trusted_client_address`）。残る穴: Cloudflare を通らずに Render の中継へ直接つなげる経路があれば、末尾から3番目を偽れる（未確認）。N は推測で決めず、`/api/auth/status` が出す INFO ログ `auth/status 転送経路: peer=... xff=[...] hops=... key=...` を、偽の `X-Forwarded-For` を付けた要求と付けない要求で見比べて決める。Render の構成が変わったら同じログで確かめ直す（`hops=3` のとき `key` が偽の値でなく送り手の IP になること）。
- **【実装済み（Tier3-2）】** CSP の `script-src` から `'unsafe-inline'` を除去済み（インライン `<script>` を `static/js/` へ外部化＋インラインイベントハンドラを `data-*` 属性＋イベント委譲へ移行）。`style-src 'unsafe-inline'` はインライン `<style>`/`style=` 属性が残るため維持（script-src より低リスク）。
- **【実装済み・#281】httpx の INFO ログが APIキーをそのまま露出する**: `collector_utils.py` の `logging.basicConfig(level=logging.INFO, ...)` が root ロガーを INFO にするため、httpx が出す `"HTTP Request: GET <url> ..."` ログにクエリパラメータの `api_key`/`appId`（FRED_API_KEY・ESTAT_API_KEY）がそのまま記録される。`fetch_fred_series` の例外ハンドラは「レスポンスをそのまま出すと鍵が漏洩する」ことを認識し `status_code` のみログしていたが、httpx 自身が出す**正常系**のリクエストログは対象外だった＝ローカル実行はもちろん GitHub Actions（`collect-macro.yml`）のログにも同様に記録され続けていた抜け穴。`collector_utils.py` で `httpx`/`httpcore` ロガーを `WARNING` に引き上げて解消。新しい外部APIキーを URL クエリで渡す実装を追加する際は、この抑制がまだ効いているか（ロガー名を変えていないか）を確認すること。

---

## 分析・計算の実装メモ

- **プラグイン execute は同期実装＋`asyncio.to_thread` オフロード（#357）**: execute を `async def` のままイベントループ内で同期CPU処理（XGBoost CV 等・数分）すると、`/heartbeat` ハンドラが走れず30秒途絶 → launch.py 経由起動時の watchdog（別スレッド・api.py）が「ブラウザ閉」と誤認し `os._exit(0)` でサーバーを殺す（M-2 UI実行が「実行失敗」→サーバー消滅として顕在化した実障害）。対策として全プラグイン execute は同期 `def` とし、`plugins.execute_plugin` が `asyncio.to_thread` でオフロードする。留意点: (1) 実行制御の ContextVar（`tuning_dry_run`/`tuning_objective_only`/`shared_snapshot_cache`）は **to_thread ならコンテキスト複製で伝播するが `loop.run_in_executor` 直接使用では伝播しない**（to_thread 以外へ書き換え禁止）。(2) db セッションはワーカースレッドへの逐次ハンドオフのみ許可（await 中にループ側から同一セッションを触らない）。(3) 実行中は `plugins.any_executing()` が True になり watchdog が停止を保留する（タブ閉のまま長時間分析でも殺されない）。
- **成長率計算は (edinet_code, year, period_end) で副ソート**済み。同年複数レコードがある企業の前期比が不定にならないようにしている。
- **フリーCF = 営業CF + 投資CF**（設備投資以外の投資活動も含む近似値）。
- **`scripts/` のキャッシュに namedtuple / 自作クラスをそのまま pickle しない（#372）**: `scripts/*.py` は `python -m scripts.foo` で実行されるためモジュール名が `__main__` になり、そこで定義した namedtuple を pickle すると `__main__._FinRow` として記録される。同じキャッシュを他モジュールから import して読むと `AttributeError: Can't get attribute '_FinRow' on <module '__main__' ...>` で落ちる（`scripts/candidate_bakeoff.py` の財務キャッシュを別スクリプトから再利用しようとして実際に踏んだ）。**pickle には素の tuple/dict だけを載せ、読み出し後にクラスへ復元する**。形式を変えるときはキャッシュキーも更新する（旧 pickle は読めないため）。
- **断面が定数の列は「標準偏差 > 0」で判定できない（#372）**: マクロ特徴量は同一月の全銘柄で同値だが、平均の丸め誤差により `np.std` は厳密な 0 ではなく 1e-16〜1e-18 を返す。断面回帰（Fama-MacBeth）で定数列を除外する判定は**列スケール相対の閾値**（`sd / max(平均|値|, 1) > 1e-8`）で行う。厳密比較だと定数列を取りこぼし、切片と完全共線な列が回帰に残る。
- **`az.summary` は既定で丸める＝収束診断をゲートに使うなら `round_to="none"` 必須（#356）**: arviz は `round_to` 省略時に列ごとの固定桁で丸める（`arviz/stats/stats.py`: **r_hat は小数2桁**・`ess_bulk`/`ess_tail` は整数・その他3桁）。r_hat の丸め幅は M-1 の strict 収束ゲート（1.01）と同じ桁なので、丸めたままでは観測値が 1.00/1.01/1.02 の3値解像度しか持たず、(1) `persist_allowed` の strict 判定が実質「真値 < 1.015」に緩む、(2) 収束改善の効果が測れない、という二重の実害が出る。実際 Issue #341/PR#354 の「2/4/6/8 チェーンで r_hat_max=1.0100 が完全平坦」＝「1.01–1.02 はモデル幾何の床」という結論は、床の証拠ではなくこの丸め解像度そのものだった（#356 で `summarize_diagnostics` を生値へ修正）。**閾値比較・傾向比較に使う統計量は、ライブラリの表示用整形を経ていない生値であることを必ず確認する**（表示桁 == 判定桁のときは特に危険）。
- **as-of は max で代表しない＝銘柄別に違う日付を1個へ潰すと嘘になる（#417/#416）**: μ̂ は銘柄ごとに「その銘柄の最終週次バー」時点で計算される（`build_snapshots` の `is_current = (snap_idx == n-1)`）。producer 4本（M-2/M-3/M-4/M-6）は代表値を `max(snap_date)` で `*_scores.snapshot_date` に書いていたが、実測（2026-08-02）は **2銘柄が 2026-07-31・3,677銘柄が 2026-07-13** で、max は実態より19日新しい。**代表値は中央値（p50・偶数個は古い側）＋最古＋古い銘柄数の3点で持つ**（`plugins/macro_snapshots.py::representative_snapshot_date` が唯一の算出元・4プラグインで共有）。同じ罠は株価鮮度にもあり、`/api/stats` の株価 as-of も p50 で判定して max は参考値としてしか出さない（`database.py::price_freshness`）。#356 の「表示桁 == 判定桁」と同型で、**集約の仕方が実態を隠すタイプの罠**。
- **ローカルの `macro_beta` の所要はこう測る（#512・`scripts/bench_macro_beta.py`）**: 同じ設定で回しても素の所要は比較にならない。①**probe を1本先に流す**（JAX はコンパイル済みカーネルを使い回すので、probe 無しだと1点目だけがコンパイル代を負担し、2点目が速くなって限界費が負に出る＝実測 draws=20 が 15.1s・draws=60 が 7.7s）②**総 leapfrog 歩数に対して回帰する**（draws を分母にすると run 間で steps/draw が変われば傾きが汚染される）③**tune を切り詰めない**——tune=25 では NUTS が別レジームへ落ち steps/draw が 1023→63・発散 78 まで動いた（本番 tune=800 は全 draw が max treedepth に張り付く領域＝ここで測らないと比較にならない。ハーネスが歩数比 1.2 超か発散ありで警告する）④**同一セッション内で交互に測る**——ローカルの値は時間帯で 2.4倍振れる（同一設定・同一レジームで 20:50 に 356us/step、04:00 に 870us/step）。ローカル↔GHA の A/B は `--mode synth`（seed 固定の合成パネル・DB 非使用）で取る＝#503 の反転後、実データだと Supabase は 2026-08-07 断面でローカル正本と規模が違い比が汚れる。実測（2026-08-25）: 本番規模でローカルは **22,176us/step・約20時間相当**、GHA は同一形状の合成パネルで 0.0356us/step/obs 頭打ち＝**6.6倍**。差は規模依存（n≤250 では 1.3〜1.5倍）なので「g++ 不在で PyTensor が Python フォールバック」では説明が付かない（それなら規模に依らず一律に遅い）。
- **M-1 の軌道長（`max_tree_depth`）を測るときの罠（#540・ADR-0002）**: 3つある。①**縮小規模は別レジームになる**——実データを 250銘柄へ間引くと `select_shared_factors` が **5因子しか選ばない**（本番は 12）ため事後が低次元化し、`max_tree_depth=10` でも軌道が 255歩で自然に U ターンして到達率 0.000 になる＝**「1023 に張り付く」という測りたい現象がそもそも起きていない**のに、表は何事も無かったように並ぶ（`bench_macro_beta_report.regime_note` が「最も緩い上限のセルの到達率が 1.000 を割ったら」警告する）。実データで本番と同じ12因子が選ばれるのは **500銘柄以上**、張り付き regime を再現するなら **`--mode synth`（12因子）** が確実。②**ESS/歩（統計効率）だけで選ぶと壊れた設定を採る**——一律 `max_tree_depth=8` は ESS/1e6歩 4027 と最良に見え `ess_bulk_med` も 821.6 と健全なのに、`ess_bulk_min` が **3.55**・`r_hat_max` が **1.6347** で 1.05 ゲートを大きく外す（軌道を切ると funnel 側のスケール母数だけが完全に固着し、大多数の母数は良く混ざったままになるので**中央値には出ない**）。必ず `r_hat_max` と **`ess_bulk_min`** と併せて読む。③**wall time で格子を比べない**——ローカルの us/step は時間帯で 2.4倍振れるので、数時間かかる格子では順位がドリフトに化ける。主指標は時間を含まない **ESS/leapfrog 歩**にし、`ESS/秒` は所要見積り用に併記する。なお `r_hat_max` は**シード間で ±0.014 振れる**（chains=2）ので、それ未満の差を設定の優劣として読まないこと（現行設定でも seed 次第で 1.0551＝ゲート外れが出る）。
- **全パラメータの max / min にゲートを課すと、規模が増えるだけで必ず閉じる（#609・ADR-0002）**: `r_hat_max <= 1.05` は M-1 の収束ゲートとして2年動いたが、本番規模でパラメータが 49,893 個になると「最も混ざらなかった1個」が必ず現れて落ちる。実測では 26観測しかない**平凡な銘柄**の切片1個（`alpha[883]`）が `r_hat_max=1.1242` を出し、2026-08-01 から5週間 `macro_beta_loadings` が更新できなくなった。**モデルの不具合ではなくゲートの定義そのものが原因**なので、`draws` を増やしても `target_accept` を動かしても直らない（#600 で全部実測棄却した）。対処は**分位（p99）で見ること・ただし変数ごとに取ること**の2点セット。全体の p99 へ緩めるだけだと、逆に本物の崩壊を見逃す——`max_tree_depth=8` の崩壊で壊れていたのは `mu_universe`（12個）だけで、本番規模ならそれは全体の **0.024%** ＝ p99 には現れない。変数別 p99 は**パラメータ数に応じて厳しさが自動的に決まる**（12個の変数では p99 が実質 max・46,044個の変数では外れ値1個を無視できる）ので、閾値を規模の関数にする必要はない。**同型の罠**は「代表 as-of を max で取る」（#417）と「表示桁 == 判定桁」（#356）——いずれも**集約の仕方が実態を隠す**。
- **M-1 の `beta` はトレースに載せず事後再構成する（#512 → #541 で解消）**: かつて `beta`（n_stock x n_factor）と `mu_sector` は `pm.Deterministic` ＝ **トレースに保存**され、本番（3800銘柄・draws 800・chains 2）で **約1.2GB** が posterior だけで乗っていた（実測ピーク常駐は draws=15 の時点で 1,205MB、計測中のこの PC の空き物理メモリは 1.8〜2.4GB）。**A/B 実測（2026-08-26・同一条件 3,800銘柄・chains=2・tune=25・draws=[5,15]・`--no-ess`）で 1,205MB → 1,026MB（−179MB）**。draws=15 での `beta` トレース実体は約7.3MB しかないので、**消えたぶんの大半は保存分ではなく `pm.Deterministic` の後処理**（全 draw を vmap で展開する一時確保）である＝本番 draws=800 では保存分 約584MB がこれに上乗せで消える。同 A/B で 1歩あたりのコストも 22,176us → 13,017us と落ちた（**各1本の観測で下記②の時間帯ドリフトを排除しきれていないので、この 41% を確定値として引用しない**）。**所要だけ見ていると律速がメモリ側にあることに気づけない**（compute-bound な BLAS ベンチは同時刻に1.2倍しか遅くならないのに NUTS は 2.4倍遅くなる）。現在は素の式にして、自由 RV から `_reconstruct_beta_chunk` で組み直す。触るときの不変条件は3つ:
  - **draw ごとに再構成する**。`sigma_stock` が確率変数なので `mean(beta_raw * sigma_stock) != mean(beta_raw) * mean(sigma_stock)`＝事後平均から組み直すと平均がずれ、SD は全く別物になる。
  - **銘柄チャンク（`BETA_CHUNK_STOCKS`）で刻む**。全銘柄まとめて `(chain, draw, stock, factor)` を作ると削減した約584MB がそのまま戻る。
  - **収束診断は `beta` に対して計算し続ける**。`persist_allowed` は `beta` / `alpha` / `mu_universe` それぞれの r_hat を見るので、`beta` を `beta_raw` で代用すると non-centered の raw パラメータは混合が良いぶん r_hat が小さく出て**ゲートが黙って緩くなる**（失敗として現れない）。チャンクへ分割して r_hat=max・ESS=min を累積するのは、これらがスカラーパラメータごとに独立に計算されるため全体と厳密に一致するから。
- **M-1 の `macro_beta_meta.snapshot_date` は推論バッチの実行日であってデータ as-of ではない**（`macro_beta_inference.py` が UTC today を入れる）。株価が止まっていても「今日」と読めてしまうので as-of には使わない。データ側の日付は推論バッチが `build_panel(return_last_bars=True)` の最終週次バーから作り、`hyperparams.data_asof` に残す（#781・列にしないのは DDL 未適用の DB でバッチが開始時に止まるため）。`get_producer_asof` は live run のそれを返し、持たない run には**日付が None の dict（as-of 不明）**を返す——None（未蓄積）と同じ顔にすると、#781 以前のように「μ̂ が揃っているのに未蓄積・発注不可」と出る。
- **学習パネルには 2024 年より前に上場廃止した社が居ない＝初期の test 月は生き残った社だけを採点している（#1188・ADR-0074）**: 週次株価（`stock_price_weekly`・2019-07-29〜）の系列が 2023 年以前に終わる社は **0 社**で、財務だけあって株価が 1 本も無い社が、最後の期末 2019〜2023 年で計 338 社ある（2026-11-11 実測）。Yahoo は廃止した銘柄に 404 を返し、J-Quants の契約窓は約 2 年なので、**今の情報源では取り直せない**。株価の無い社はスナップショットを作れないので、答えが「消える」のではなく**社ごと居ない**——`build_snapshots(return_no_future=True)` の「途中で終わった行」も 2021〜2023 年の test 月は 0 件になる（#1154 の全期間 0.527% は下限で、2025 年は 2.87%）。**消えた行の割合や生存者バイアスを測るときは、廃止社の株価がそろう 2024 年以降の test 月で読む**。**OOF の上位10社超過の値そのものは、途中で廃止した社が入らないぶん高めに出る**——2026-11-13 の計測（#1197）で足すと 19 期で M-6 +0.2131 → +0.1665、M-2 +0.1228 → +0.0941（差の向きは変わらない）。OOF の比較と学習は今の答えのまま、本番予測の答え合わせ（#1157）の主審にだけ足す（#1195・[ADR-0075](adr/0075-delisted-rows-are-scored-only-in-the-live-check.md)）ので、両者の上位10社超過をそのまま並べて比べない。
- 分析モデルの理論・次元整合性・外れ値処理（winsorize）・株数推計・Zスコア年度別計算の詳細は [MODELS.md](MODELS.md) を参照。

---

## マクロ指標（FRED 低頻度系列・データソース）

- **FRED 低頻度系列は公表ラグ分シフトして格納する（先読みバイアス防止・#250）**: FRED は観測値を「期の参照開始日」の日付で返す。実体経済指標（GDP・鉱工業生産・失業率・貿易収支）は公表ラグが大きい（四半期≈期末+1.5〜2か月、月次≈+1か月）ため、`obs_date` をそのまま `trade_date` にすると、まだ公表されていない値をスナップショット（`trade_date <= ref_date` で突合）が「見て」しまう。`FRED_SERIES` の各系列に `lag_days`（四半期135日・月次60日）を持たせ、`fetch_fred_series` が収集時に `trade_date = obs_date + lag_days` へシフトして「この日には知れた値」へ正規化する。既存5系列は `lag_days` 未指定＝0 で後方互換。
- **FRED 系列の `freq` 記載漏れは「毎晩 CRITICAL」を生む（#444・実害が出た）**: `_GROUP_DEFAULT_FREQ["FRED_SERIES"]` は **`daily`（許容14日）** なので、月次・四半期系列に `freq` を書き忘れると平常運転の公表ラグがそのまま stale 判定になる。`JP10Y_FRED`（`IRLTLT01JPM156N`）は OECD MEI 由来の月次系列だが `freq`/`lag_days` とも未指定のまま登録されており、鮮度ゲート（#420）が **lag=64日 > 許容14日** で毎晩 exit 2 →自動起票（#414）が繰り返される状態になっていた。**FRED の系列 ID 末尾は更新頻度を示す**（`...M156N`/`...M156S`＝月次・`...Q664S`＝四半期・`...INDXD`＝日次）ので、追加時は ID から頻度を読み取って `freq` を明示すること。同時に、月次値が期首日付のまま入る＝先読みバイアスにもなっていた（`lag_days` 未指定＝0）。
- **`lag_days` を付けた系列は鮮度ゲートの検知力が同じ日数ぶん落ちる（#444）**: `trade_date` を後ろへシフトする＝`max(trade_date)` が実際より新しく見えるため、配信が止まってもシフト分だけ stale 判定が遅れる（#438 の BCOM「静かな固定」と同型の穴）。対策は系列定義の任意キー **`stale_days`**（`macro_health.py` が `FREQ_STALE_DAYS[freq]` より優先）。`JP10Y_FRED` は `lag_days=64`／`stale_days=62`。**freq 既定から `lag_days` を一律に差し引く実装にしてはいけない**——`lag_days` は先読み防止の保守的シフト量であって実配信ラグの推定値ではなく、2026-08-04 の本番実測では `JP_REAL_GDP`/`JP_TRADE_BAL`（実測lag 80日 > 210−135=75）と `JP_IIP`/`JP_IIP_INVENTORY`（実測96日 > 130−60=70）の4系列が即 CRITICAL に落ちる（`tests/test_macro_health.py::test_lag_days_alone_does_not_tighten_limit` が回帰を止める）。
- **実配信ラグは `macro_data.created_at` で実測できる（#447）**: 「その観測が外部ソース上で取れるようになった日」は行の初回挿入時刻に残る。収集はほぼ毎晩走るので誤差 0〜1 日で、2026-08-04 の実測は公表カレンダーと一致した（全国CPIの2026年6月分＝07-24・M2＝07-09）。**ただしバックフィルで一括挿入された行は `created_at` がその収集日に潰れている**ため、系列ごとに「バックフィル日より後に初出した観測」だけを採る。`lag_days` を変えると再収集で全行が潰れる＝**是正の前に実測を取ること**（`JP10Y_FRED` は #444 の再収集で実測点を失い、#447 では公表カレンダーからの推定に頼らざるを得なかった）。
- **収集元の実配信ラグは原統計の公表日と違う（#447）**: `JP_UNEMP`（労働力調査）は総務省が調査月の翌月末に公表するが、収集元の FRED（OECD MEI 経由）へ載るのはさらに約20日後（2026年5月分＝総務省 06-30 → FRED 07-20 実測）。**総務省の公表日で `lag_days` を引くと、その20日ぶんがそのまま先読みになる**。先読み判定は必ず**収集元での**取得可能日で行う。
- **`lag_days` を実配信ラグより大きく取ると `trade_date` が未来日になる（#447）**: 安全側の失敗に見えるが、その観測はスナップショット（`trade_date <= ref_date`）に現れないまま情報だけが遅れ、しかも経過日数が負の間は stale 判定が構造的に成立しない。`JP10Y_FRED` は #444 の `lag_days=70`（実配信ラグ上限 64日を超過）で `max(trade_date)=2026-08-10`＝6日先になり、stale にも missing にも該当せず**どのゲートにも掛からなかった**。`macro_health` は独立区分 `future` で検知する（数日は warn・観測周期超過で CRITICAL）。
- **1系列だけ再収集したいときは `--macro-series` を使う（#444）**: `collector.py --macro` は MACRO/FRED/BOJ/OECD/ESRI/IMF/e-Stat/GDELT/Wikimedia の全系列を叩くため、定義是正に伴う 1 系列の再収集で **GDELT の累積クエリ制限**まで消費してしまう。`--macro-series JP10Y_FRED`（カンマ区切りで複数可）でグループ横断に絞れる。未知コードは `ValueError` で即停止する（「0件収集・成功」で黙って返さない）。ESRI/IMF は 1 リクエストで全系列ぶん取る構造のため、対象が空ならグループごと fetch を省く。
- **FRED の OECD 旧系列は凍結（更新停止）があり、採用前に最終更新日を必ず確認する**: 例 月次CPI `CPALTT01JPM657N`（2021/6 で停止）・M2 `MYAGM2JPM189S`（2017/2 で停止）。#250 で採用した4系列のうち `JPNPROINDMISMEI`（鉱工業生産）は **2024-04-30 で凍結が確認された（#253）**。**e-Stat「鉱工業指数」直接取得（`ESTAT_INDEX_SERIES`・`JP_IIP`/`JP_IIP_INVENTORY`）へ切替済み（#281）**。残存3系列（JPNRGDPEXP / LRUNTTTTJPM156S / XTNTVA01JPQ664S）は引き続き更新中を確認済み。CPI・M2・短観は e-Stat/日銀コネクタ済み（ADR-0006・#251）。**#404 の EPU 追加時も同型の凍結に遭遇**: 日本版 EPU `JPNEPUINDXM` は **2016-04 で凍結**（policyuncertainty.com 本家は更新中だが FRED への配信が止まっている）ため不採用とし、米国版 `USEPUINDXD` / `WLEMUINDXD`（ともに日次・1985-01-01〜現役）のみ採用した。
- **FRED 系列の生存確認は `fredgraph.csv` で行う（#404）**: 採用可否は「最終観測日」で決まるが、`https://fred.stlouisfed.org/series/<ID>` の HTML ページは **Cloudflare が 403** を返して機械的に読めない。API キー無しで確実なのは CSV ダウンロード経路 `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>`（単一 ID なら CSV がそのまま返る。**カンマ区切りで複数 ID を渡すと ZIP になる**ので1本ずつ叩くのが確実）。取得した末尾行の日付が「凍結済みか現役か」の唯一の証拠になる。GDP需要項目（個人消費・設備投資・住宅投資・公共投資）は当初e-Stat APIで検討したが `statsCode=00100409` は「四半期別ＧＤＰ速報 過去の値」というvintage別アーカイブテーブル（四半期ごとに個別statsDataId）しか持たず継続更新系列が存在しないため不採用と判明（#281調査時）。代わりに**内閣府ESRIが直接配布するCSV**（`ESRI_SERIES`・`fetch_esri_gdp_csv`）で解決した（#286）。在庫（需要側内訳の在庫変動）は引き続きスコープ外。
- **e-Stat「時系列データ」テーブルは経済指標ごとに `@time` のフォーマットが異なる**: CPI（statsDataId=0003427113）は `@time` が `"YYYY"+"00"+"MM"+"MM"` の自己記述コードで直接パース可能だが、鉱工業指数（`ESTAT_INDEX_SERIES`・statsDataId=0004052177等）は `@time` が `"0500100"` のような**連番コード**で年月を直接表現しない（コード自体に規則性があるように見えても統計表ごとに異なる可能性があり決め打ちパースは危険）。対応: `metaGetFlg="Y"` を付けて `getStatsData` を呼ぶと、同一レスポンスの `CLASS_INF.CLASS_OBJ`（`@id="time"`）に code→`"YYYYMM"` の対応表が同梱される（追加の `getMetaInfo` 呼び出し不要）。`fetch_estat_index_series` はこの対応表を使って変換し、対応表の名前が6桁数字（YYYYMM）でない行（「付加生産ウエイト」等のマスタ行）はスキップする。
- **鉱工業指数は基準改定のたびに `statsDataId` が別テーブルへ切り替わる**: 2010年基準（2008年1月〜・`open_date=2018-10-31` で更新停止済み＝既に凍結済みアーカイブ）→2015年基準（2013年1月〜）→2020年基準（2018年1月〜・`open_date=2026-05-19`＝現行）の3世代が e-Stat 上に共存する。`getStatsList` に `statsCode=00550300`（鉱工業指数の政府統計コード）で問い合わせても新旧3世代が混在して返るため、`STATISTICS_NAME` の基準年表記と `OPEN_DATE`（直近更新日）を必ず確認してから採用する。FRED 版 `JPNPROINDMISMEI` の凍結（#253）もこの基準改定に起因する可能性が高い。
- **Yahoo は 1306.T（TOPIX の代理）の 2026-03-30・31 を前後の約 1/10 で返す（#1283・2026-11-20 確認）**: 出来高も約 10 倍で、分割を 2 日だけ取り違えた形。マクロ系列は毎晩 5 年ぶんを取り直して上書きするので、DB の行を消すだけでは翌晩に戻る。`macro_topix_yoy` は前 30 日の平均を使うため、2026-03-30〜04-29 を基準日とする値が約 9 ポイント低く出ていた。**収集の Yahoo 経路で「1〜2 日だけ前後の 5 倍・1/5 に跳んで戻る」行を書かず、DB の同じ日の行も消す**（`collector_prices.drop_isolated_spikes`・閾値は `collector_utils.MACRO_SPIKE_*`。消した日は収集のログに WARNING で出る）。戻らない段差（本物の急変）と系列の両端は弾かない。なお 5 年より前の行は取り直されないので、Yahoo が後から過去を調整した系列（1306.T の 2016-07 の 1:10）は DB に古い基準のまま残る——直すときは `collect_macro_data(db, years_back=<その年まで>, only=[...])` で取り直す。
- **日本の市場系インデックスは Yahoo の指数ティッカーが死んでいる**: 日10年金利 `^JGB` は上場廃止（HTTP 404）、TOPIX 指数 `^TPX` は 200 OK だが 0 件配信。**TOPIX は ETF 1306.T（NEXT FUNDS TOPIX）を代理に収集**（指数とほぼ同追従・#250）。**日次の日本10年金利は「ソースが無い」のではなく財務省の「国債金利情報」CSV で取れる**（#456 調査・2026-08-06 実 HTTP 確認で従来の記述を撤回）: 全期間版 `https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv`（cp932・和暦 `R8.7.31` 形式・CRLF・ヘッダ2行・欠測 `-`・末尾に注意書き行。10年物は 1986-07-05 以降 9,909 営業日）、当月版 `https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv`（1〜2営業日遅れ）。ライセンスは PDL1.0（出典表示のみで加工・再配布可）。Yahoo/stooq が全滅していたことを「日次ソース不在」と一般化していたのが誤りで、**公的機関の直接配信を当たっていなかった**。M-3 の `dlm_jp10y` は **`JP10Y_MOF`（財務省・日次）へ差し替え済み**（#458・ADR-0029）。週次差分のゼロ率は 76.89% → **0.91%**（日次の `dlm_us10y` は 0.59%）。**ただし日次化した状態で測ると売り側 spread が有意に悪化したため既定からは外した**（p=0.023 < α=0.025）——月次版が無害だったのは「ほぼ何もしない列」だったからで、**ゼロ差分率の高さは「情報量が乏しい」の指標ではあっても「差し替えれば成績が上がる」の根拠にはならない**。なお **日次系列の `lag_days` は実配信ラグを超えて置けない**: 超えると `trade_date` が未来日になり、鮮度ゲートが `ahead_days > 観測周期(日次=1日)` で CRITICAL にする（実測: `lag_days=4` で max が today+2 になり即 CRITICAL → 2 へ是正）。低頻度系列で「理論最大へ寄せる」と決めた ADR-0028 の運用は、日次にはそのまま適用できない。
- **指数ティッカーの「生存確認」は採用時点の事実でしかない（#358 → #438）**: `^BCOM`（ブルームバーグ商品指数）は 2026-07-20 の Phase 0 疎通検証では生存していた（6年 1506 行）が、**2026-07-17 を最後に配信停止した**（`^JGB` 404・`^TPX` 0件に続く3例目）。停止の出方が新しく、**timestamp 行だけは返り続けて `close` が全 null になる**ため「200 OK・行はある」だけでは生存判定にならない（`fetch_yahoo_history` は null 行を落とすので DB 上は `max(trade_date)` が固定＝#420 の鮮度ゲートで初めて検知できた）。対応は TOPIX と同じ**連動商品の代理**で、`DJP`（iPath Bloomberg Commodity ETN）へ差し替えた（2026-08-06・`series_code` は `BCOM` のまま＝特徴量名・既定セットは不変）。代理候補の追従性実測（2014〜・BCOM 比）: **DJP 週次 logret corr 0.9906 / yoy corr 0.9957**・DBC 0.9027／0.9715・BCI 0.836／0.903・PDBC 0.797／0.784。`^BCOMTR`/`^DJUBS` は Yahoo に無く、`^SPGSCI` は生存だがエネルギー偏重で WTI と重複（ADR-0013 が却下済み）。**差し替えは全期間（`--years 11`）で再収集する**——DJP はトータルリターン型で旧 ER 指数と水準体系が違い、期間を絞ると新旧 level が混在して yoy が1年間壊れる（DJP は BCOM の全 2,509 営業日をカバーするため上書きで断層は残らない）。個別先物 `HG=F`（銅）/`NG=F`（天然ガス）/`SI=F`（銀）/`ZW=F`（小麦）/`ZC=F`（トウモロコシ）/`ZS=F`（大豆）/`PL=F`（プラチナ）も全て 6年 1508-1510 行取得可を確認。**stooq はコモディティ先物がローカル IP でも全滅（0件）** のため `MACRO_SERIES` の `ticker` は空文字（Yahoo フォールバック時に安全に skip）。GHA Azure IP 上での `^BCOM` 最終疎通は collect-macro.yml 実行で確認する（#218 の2PR 公開フロー）。
- **GDELT はレート超過を HTTP 200＋プレーンテキストで返す（#406・ADR-0024）**: DOC 2.0 API (`api.gdeltproject.org/api/v2/doc/doc`) の制限は「1リクエスト/5秒」だが、超過時のレスポンスは **ステータス 200 のまま本文が `Please limit requests to one every 5 seconds...` というプレーンテキスト**になる。`raise_for_status()` は通ってしまい `r.json()` で初めて壊れるため、`fetch_gdelt_timeline` は**本文が `{` で始まるか**で判定し、`GDELT_RATE_SLEEP × 試行回数`（線形バックオフ・最大 60 秒/系列）待って再試行する。**制限は間隔だけでなく短時間の累積クエリ数にも効く**：開発中に数十回叩いた直後は 60 秒バックオフしても3系列中2系列が全滅し、3 分放置したら即復帰した（2026-07-31 実測）。通常運用（3系列×日1回）では触れないが、調査で連射したときは数分空けること。履歴は 2017-01-01 開始（それ以前は `Invalid query start date`）で、全期間を1リクエストで日次取得できる（間引きなし・2026-07-31 実測）。
- **Wikimedia Pageviews は User-Agent に連絡先が無いと 403（#406・ADR-0024）**: 認証は不要だが robot policy により、`financial_app/1.0` のような連絡先なし UA は `403 Please respect our robot policy` で拒否される。URL かメールを括弧内に含めた `WIKIMEDIA_UA` を必ず付ける。欠測日は API が項目ごと落とすため、**0 埋めせず合算から除外する**（0 と「データ無し」を同一視すると zscore の分布が歪む）。
- **低頻度マクロ系列の変換窓は「観測間隔 > 窓幅」で構造的に全滅する（#379・実害が出た）**: `_macro_from_cache`（`plugins/macro_snapshots.py`）の既定窓は日次〜月次（median gap 1〜31日）前提で、四半期（92日）・年2回（183日）の系列では変換が**全期間 None** になる。本番実測（2026-07-23）: `JP_REAL_GDP`（yoy）は前年同期窓 ±30日 に点が無く None率 33%、IMF WEO 2系列（zscore）は5年窓に 5 点しか入らず `>= 20` 点の閾値を構造的に満たせず None率 **100%**。**strict（`macro_nan_ok=False`）の M-1 は1特徴でも None なら行を落とす**ため、#358 の全マクロ既定 ON 以降 M-1 の既定実行が**学習0件**になっていた（#615 で `use_macro` の既定が OFF になったので、**この罠は手動で ON にしたときだけ再燃する**）（UI は空表示・例外は出ない＝サイレント故障）。fix: yoy は current 側と対称に前年同期を ffill（`_YOY_PREV_FLOOR_DAYS=366` で下限を切り、古すぎる点は使わない）、zscore は5年窓が足りなければ「直近 `_ZSCORE_MIN_PTS`(=20) 点」へ窓を切り替える（＝低頻度ほど実効窓が伸びる。全履歴でも足りなければ `_ZSCORE_MIN_PTS_SPARSE`(=8) 点まで許容し、それ未満のみ None）。**高頻度系列の値は不変**（回帰テストで固定）。また四半期 zscore の点数確保のため `collect_macro_data` は FRED の取得窓を `FRED_MIN_YEARS_BACK`（10年）まで広げている。
- **マクロ系列の収集開始日の差が strict モデルの学習期間を律速する（#379 の派生・#381 で解決。M-1 は #615 で既定 OFF になったので、律速するのは手動で ON にしたときと M-2/M-6 側）**: strict は全系列同時に非 None を要求するため、**最も新しい系列**が学習可能期間の下限を決める。2026-07-23 時点で `HY_OAS`/`IG_OAS` のみ 2023-06-26 開始（他は 2016-06 開始・コモディティ8系列は 2020-07 開始）で、M-1 strict のスナップショットは **24ヶ月（2023-07〜2025-06）しか立たなかった**。`min_train_months=6` + embargo=12（ADR-0014）では fold が 2 期しか取れず honest OOF の統計的信頼性が低い。
  - **真因（#381・要注意）**: この `HY_OAS`/`IG_OAS`（FRED `BAMLH0A0HYM2`/`BAMLC0A0CM`）だけが短いのは **FRED が 2026-04 以降 ICE BofA 指数系列をローリング3年窓に制限し、2023年以前の履歴を配信しなくなった**ため。ICE BofA 系列は再収集しても 2023-06 以前へ遡れない（完全系列 1996〜 は ICE/Bloomberg/Refinitiv の商用ライセンスのみ）。同じ FRED 日次ループの非ICE系列（`T10Y2Y`/`BREAKEVEN10Y`）が 2016-06 まで backfill 済みなのが対照的な証左。**`FRED_MIN_YEARS_BACK=10` があっても ICE 系列の backfill は不可能**なので「再収集で伸びる」と誤認しないこと。
  - **対策（#381）**: 既定の信用ファクターを非ICE代替 `BAA_SPREAD`（FRED `BAA10Y`＝Moody's Baa 社債利回り−米10年金利・日次・1986〜で truncate されない）へ移行。`HY_OAS`/`IG_OAS` は選択肢としては残す（手動 ON で直近3年窓を使える）が **`DEFAULT_MACRO_FEATURES` からは除外**（`plugins/macro_snapshots.py` の `_STRICT_TRUNCATED_FEATURES`）。系列ごとのカバレッジは集約クエリ `SELECT series_code, min(trade_date), max(trade_date) FROM macro_data GROUP BY 1` で確認できる。
  - **2026-08-01 の実測（#411 / ADR-0025）**: 除外後の既定マクロ46本は**全て 2019-07 から非 None**で、strict / nan_ok / マクロ無しの母集団が完全一致（**71ヶ月・173,836サンプル**）＝ **strict はもう1行も落としていない**。コモディティ8系列も再収集で 2016-07-25 開始まで遡っており「次の律速」ではない。**この節の推測を prose で更新するのではなく `python -m scripts.measure_strict_binding` を回して `VERDICT` を見ること**（`strict IS binding` なら律速候補を列挙する）。学習窓を決めているのは週次株価・財務のデータ履歴長で、マクロ既定では伸びない。実際 #411 では株価を 2019-08 まで・財務を 2018 まで延伸して窓が 47→71ヶ月（fold 10→18 期）に広がった（ADR-0025）。
- **日銀 API エンドポイントの ADR との差分（ADR-0006）**: ADR-0006 は `api.boj.or.jp` と記したが、2026-02 公開の実エンドポイントは `https://www.stat-search.boj.or.jp/api/v1/getDataCode`。実装ではこちらを使用。
- **BOJ 短観の SURVEY_DATES フォーマット**: 月次系列（M2）は `YYYYMM`（例 `202501`）、四半期系列（短観）は `YYYYQQ`（`01`=Q1/4月, `02`=Q2/7月, `03`=Q3/10月, `04`=Q4/翌年1月）。`fetch_boj_series` は `freq` フィールドで分岐して正しいカレンダー日付へ変換。
- **e-Stat CPI cdCat01 コード（statsDataId=0003427113・2020年基準）**: `0001`=全国総合, `0161`=生鮮食品を除く総合（非季調コア）。季節調整済みコア `0902` との混同注意（BOJ が政策判断に使うのは非季調コア `JP_CPI_CORE`）。東京都区部は `cdArea=13A01`（表示名は「13100 東京都区部」だが実際の `@code` は `13A01`。旧 `13100` を指定すると `STATUS=1`「該当データなし」で0件）。
- **e-Stat CPI 年次行混入問題（解決済み #262・2026-07-02 実API検証済み）**: statsDataId=0003427113 は月次（1970年〜現在）と年度（会計年度）集計が同一テーブルに混在。原因は3つ重なっていた: (1) `cdTab`（表章項目=1:指数）未指定、(2) `lvTime`（時間軸レベル=4:月次）未指定、(3) `@time` のパースが先頭6文字を YYYYMM とみなしていたが実際のフォーマットは `"YYYY"+"00"+"MM"+"MM"`（月次・月を2回繰り返す。例 2024年12月=`"2024001212"`）で、年度行は `"YYYY"+"10"+"0000"`（例 `"2024100000"`）。先頭6文字方式だと年度行の `[4:6]="10"` を月10（10月）と誤読し「10月のみ10件」に見えていた。**fix**: `cdTab=1` と `lvTime=4` を両方指定し、月は `@time[8:10]`（末尾2文字）から取り出す。`lvTime` 単体（#256）が失敗したのは `cdTab` 未指定のままだったため＝両方揃って初めて機能する。修正後は3系列とも月次125〜126行（2016-01〜2026-05）を実データで確認。FRED代替は不採用のまま：core-CPI 相当の `JPNCPICORMINMEI`/`JPNCPICORQINMEI`（ex food&energy）は2021-06で更新停止しておりそもそも「生鮮食品を除く総合」とは定義も異なる。
- **日銀時系列統計データに JGB 年限別流通利回り（イールドカーブ）は存在しない（#282 で判明）**: Issue #282 は「日銀は年限別（2年・5年・20年等）の利回りを提供」という前提で起票されたが、実 API（`getDataCode`/`getMetadata`）で全34 DB名（IR01-04・FM01-09・MD01-14・LA01-05・BS01-02・FF・CO・PR01-04・PF01-02・BP01・BIS・DER・OT・OB01-02・PS01-02）を確認した結果、国債流通利回りに該当する系列は存在しなかった（IR01-04は預金・貸出金利、FM05/06は公社債発行額・現存額であり利回りではない）。JGB利回りは財務省が公表する別データソースであり日銀時系列統計の対象外。**#282のスコープからJGB利回りカーブを除外し、CGPI（`JP_CGPI`・db=PR01・boj_code=PRCG20_2200000000）とマネタリーベース（`JP_MONETARY_BASE`・db=MD01・boj_code=MABS1AN11）のみ実装した**。日10年金利は引き続き `JP10Y_FRED`（月次・単点）のみ。
- **OECD SDMX API は9次元キーを明示指定する（空欄=ワイルドカードは避ける・#283 で実API検証）**: `sdmx.oecd.org/public/rest/data/{dataflow}/{series_key}` の `series_key` はドット区切り9次元（`REF_AREA.FREQ.MEASURE.UNIT_MEASURE.ACTIVITY.ADJUSTMENT.TRANSFORMATION.TIME_HORIZ.METHODOLOGY`）。OECD Data Explorer が生成するサンプルURL（例 `JPN.M.LI...AA...H`）は一部次元を空欄にしていても現状は唯一解に解決されるが、将来別系列が追加されると空欄次元がワイルドカードとして複数系列にマッチし得るため、`OECD_SERIES`（`collector_prices.py`）では全次元を明示した完全キー（`JPN.M.LI.IX._Z.AA.IX._Z.H`）を使う。存在しない `series_key` は HTTP 404 + ボディ `NoRecordsFound`（JSON ではなくプレーンテキスト）を返す。認証: 匿名クエリのみサポート・APIキー不要（OECD公式ドキュメントに明記、2026-07-09確認）。CLI（Composite Leading Indicator）は対象月から2か月遅れで公表されるため `lag_days=60` を設定（e-Stat鉱工業指数と同水準）。
- **IMF WEO見通しのvintage先読みバイアスとbot保護回避（#284・2026-07-11実API検証済み）**: 現行（最新）IMF SDMX dataflow（`api.imf.org/external/sdmx/3.0/.../WEO/+/...`）は公式vintage境界と無関係に随時改定される（`WEO_2025_OCT_VINTAGE`固定版と当時の現行dataflowが同一`COUNTRY_UPDATE_DATE`属性を持ちながら日本2024年GDP成長率が`0.104` vs `-0.24232`と異なる値を返すことを実証）。過去日付へ割り当てると先読みバイアス化するため、`fetch_imf_weo_current`は常にtrade_date=収集日（今日）に固定し、過去vintageのバックフィルには使わない。バックフィルはIMF公式「Historical WEO Forecasts Database」（`WEOhistorical.xlsx`）を使う——vintage×国×指標×対象年のpoint-in-timeパネルで、2026-07-11取得版はSpring1990〜Fall2022を収録（2023-04〜2025-04の4vintage分は同ファイルにも新設のvintage archive制度（`WEO_2025_OCT_VINTAGE`が唯一）にも収録されておらず復元不可＝構造的空白として許容）。**`www.imf.org`のHTMLページ（`download-entire-database`等）はbot保護で403（UA偽装でも不可）だが、静的ファイル直リンクはRangeヘッダー付きリクエスト（`Range: bytes=0-`）なら200/206で応答する**（プレーンGETは403。bot判定の実装差）。詳細はADR-0011。
- **内閣府ESRI GDP需要項目CSVはShift-JIS（cp932）でカンマ区切り数値をダブルクォートで囲む（#286）**: `https://www.esri.cao.go.jp/jp/sna/data/data_list/sokuhou/files/{year}/qe{yy}{q}_{report}/tables/gaku-jk{yy}{q}{report}.csv`（`{yy}`=西暦下2桁・`{q}`=四半期1-4・`{report}`=速報回1/2）は1994年1-3月期〜最新四半期までの連続時系列を単一ファイルに含む（vintageアーカイブではなく継続更新ファイル）ため1回のfetchで全期間バックフィルできる。ただし四半期ごとに新ディレクトリが発行され、**旧ディレクトリは消滅する**（2026-07-10実API確認: 2024年分の1次速報ディレクトリは404）。固定URLではなく、直近`ESRI_QUARTERS_BACK`（4）四半期×速報2種（2次優先）のURLを新しい順に試すプロービング方式（`_esri_candidate_urls`/`fetch_esri_gdp_csv`）で採用。HEAD/GETとも同一ステータスを返すことを実地確認済みだがファイルが小さい（約40KB）ためGET一発でプローブと取得を兼ねる。CSVはカンマ区切り数値がダブルクォートで囲まれる箇所があり（例 `"465,065.0 "`）、内部カンマを区切りと誤認しないよう `csv` モジュールでパースする。列は英語ヘッダー行（`PrivateConsumption`/`PrivateResidentialInvestment`/`Private Non-Resi.Investment`（=設備投資）/`PublicInvestment`）で特定し、1列目の期ラベルは年が変わる行のみ `"1994/ 1- 3."` のように年を明示し、以降は `"4- 6."` のように年を省略して前行の年を引き継ぐ（先頭の月番号1/4/7/10がそのまま四半期開始月になる）。`trade_date` は「参照四半期の翌四半期初日」を基準にlag_days（60日・1次速報の公表遅延45〜50日+安全マージン）を加算。既存 `JP_REAL_GDP`（FRED版・`lag_days=135`）とは併存（GDP総額の二重収集は許容・スコープ外）。
- **stooq はローカルからも HTTP 200 で JavaScript のボット検証ページを返し、パーサは 0 行を返して例外にならなかった（2026-09-25 実測・#734 → #736 で撤去）**: クラウド IP（GitHub Actions）からの 403 は以前から既知だったが、家庭回線からも CSV は取れない。旧 `_fetch_stooq_ohlcv` の `raise_for_status()` は 200 を素通りし、旧 `_parse_stooq_csv` は HTML の行を「数値にならない行」として黙って飛ばした＝**「取れなかった」が「データが無かった」に化ける**。どの実行環境からも成功しないため、#736 で個別銘柄の株価履歴収集（`/api/collect/history/{start,stop,status,stream}`）とマクロの Yahoo→stooq フォールバックをまとめて撤去した（マクロ系列の停止は `macro_health` の鮮度判定が拾う）。**ボット検証は回避しない**。**外部ソースを足し直すときは、200 の応答が期待した形式か（CSV なら先頭行が `Date,Open,...` か）を確かめてから 0 行を「データ無し」と読む**

---

## テスト・CI

- **HTTP をモックしても、本番用の待機は本物の秒数で走る（#703）**: `tests/test_collect_macro.py` の3本は `collect_macro_data` を全ソース込みで回し、Yahoo/stooq 以外へ 500/404 を返していた。GDELT は失敗の種類を問わず `GDELT_RATE_SLEEP * (attempt+1)` の線形バックオフで `GDELT_RETRIES` 回待ち（3系列で 180秒）、系列間の `*_RATE_SLEEP` も加わって**1本 208.5秒**（`asyncio.sleep` の要求秒数の集計が CI の実測 208.7秒と一致）。3本で CI の pytest 約735秒のうち626秒（86〜90%）を占め、残り約3,900本は合計 約100秒だった。**失敗としては現れず**、job 本体が最大 801秒＝`timeout-minutes: 15` に対し余裕 99秒の状態で、ランナー準備の空白 114秒が乗った 2026-09-19 に #702 が取り消されて初めて表に出た。直し方は `_run` の中で `asyncio.sleep` を即時返却へ差し替える（待機の定数を個別に 0 にすると、ソースを足したときに同じ穴が再発する）。
- **1本の call が 60秒を超えたテストは `tests/conftest.py` が失敗にする（#703）**: 意図的に重いテスト（`pm.sample` を実際に回す M-1 の4本など）は `@pytest.mark.slow(reason="...")` を付ける。理由の無い `slow` は速いテストでも収集時に使用エラーにする（速いうちは誰も気づかず、遅くなってから素通しになるため）。上限は ini の `slow_test_budget`（既定は conftest の `SLOW_TEST_BUDGET_S`）で、ガード自身のテスト `tests/test_slow_test_guard.py` だけが pytester の内側で下げる。
- **CI の実行環境は `ubuntu-24.04` に固定する（2026-11-06）**: `ubuntu-latest` が Ubuntu 26.04 へ切り替わり、`actions/setup-python` が `The version '3.13.7' with architecture 'x64' was not found for Ubuntu 26.04` で落ちて全 PR の CI が起動直後に失敗した（再実行しても同じ）。Python は `render.yaml` の `PYTHON_VERSION` と同じ 3.13.7 に保つ約束なので、版を `3.13` へ緩めず実行機を固定した。26.04 へ移すのは、python-versions の配布表（`versions-manifest.json`）に 3.13.7 の 26.04 版が載ってから。`.github/workflows/old/` は GitHub が読まない置き場なので触っていない。
- **CI の所要の見方（#703）**: pytest の所要は**テスト数とほぼ無相関**（3,395→3,954本で 8 ms/本・r=0.115）で、2峰（平均 705.7秒 / 732.1秒）はランナー差。見かけの伸びを増加傾向と読まない。どのテストが時間を占めるかは `ci.yml` の `--durations=20` の出力で見る。`timeout-minutes` には job 開始から `Set up job` が始まるまでの空白（通常 0〜1秒・まれに1〜2分）も入る。

---

## フロントエンド（Chart.js・静的アセット）

- **【#1095】Chart.js は `static/vendor/chart.umd.js`（v4.4.1）に同梱し、CSP の `script-src` は `'self'` だけ**: 以前は jsdelivr から読み、CSP で `https://cdn.jsdelivr.net` 全体を許していた（CDN の乗っ取りや、将来の HTML 注入の穴の迂回路になる）。**外部の `<script src>` をテンプレートへ書き戻さない**——CSP 違反はコンソールにしか出ず、その画面のグラフが黙って消える（`tests/test_security_headers.py` が照合）。同梱しているのは **npm 公式の `dist/chart.umd.js`**（tgz の sha512 を registry の `dist.integrity` と照合済み・sha256 `74401d73…d8f3`）。jsdelivr の `chart.umd.min.js` は公式パッケージに無いファイルで、jsdelivr が元ファイルの先頭に注記（274 バイト）を足して返していた（中身はバイト一致）。その注記どおり**動的に作られるファイルに SRI は使えない**ので、SRI 案は採らなかった。`static/vendor/**` は `.gitattributes` で改行を変換しない（Windows の `core.autocrlf` で公式とバイトがずれるのを防ぐ）。**版を上げるとき**: registry の `https://registry.npmjs.org/chart.js/<版>` の `dist.integrity` と tgz の sha512 を照合してから、`package/dist/chart.umd.js` と `package/LICENSE.md`（`chart.js.LICENSE.md`）を差し替え、テンプレートの `?v=` を版に合わせる。版の更新を知らせる仕組みは無い（`dependency-audit.yml` は pip だけ）。

- **Chart.js 混在チャートの x 軸は `type:'linear'` を明示する（重要・実証済み 2026-06-20）**: `line` データセットを含むと Chart.js は x 軸を**既定で `category` スケール**にし、数値の `min`/`max` とデータのクランプを**無視**して点を等間隔インデックス配置する（y は既定 `linear` なので効く）。症状は「**Y 軸のレンジ設定は効くのに X 軸だけ外れ値で潰れる**」非対称。散布図（bubble）＋フロンティア（line）のような混在チャートでは `scales.x.type='linear'`（必要なら y も）を必ず指定する。M-1 のリスク-リターン散布図がこれで何度も X 軸潰れを起こした。
- **静的 JS/CSS の更新がブラウザに反映されない（キャッシュ）**: `StaticFiles` は既定で `Cache-Control` を付けず、ブラウザが旧アセットを再検証せず使い続ける。`api.py` は `/static` を `_RevalidateStaticFiles`（`Cache-Control: no-cache`＝ETag 再検証強制）でマウント済み。テンプレートの `<script src=...?v=YYYYMMDD>` 版クエリは、no-cache ヘッダが未反映の既存キャッシュを一度で破棄するための保険。**HTML 自体は各ルートが `_NO_CACHE`（no-store）で配信**されるため版クエリの変更は即時届く。切り分けは DevTools→Network で `analysis.js` の Status（200＝新規取得／`(memory/disk cache)`＝キャッシュ）を見る。
- **径エンコードは値が分散する指標にのみ使う**: M-1 で R1（予測標準誤差）はレバレッジがほぼ一定で銘柄間差がほぼ無いため、バブル径への R1 マッピングは退化して全点が最小径になっていた。径は固定にし R1 はツールチップへ。同根の現象が μ 収縮（`w=R1/R1_max≈1`）でも起きる（[MODELS.md](MODELS.md) §9.6）。
