# IMF WEO 見通し（GDP成長率・インフレ率）を forward-looking チャネルとして追加する

## Status

accepted（2026-07-11）。Issue #284（ref #280）の設計決定。

## Context

現行の全マクロチャネル（FX・金利・株式・コモディティ・VIX・信用・インフレ期待・JP実体経済・物価・マネー・サーベイ・OECD CLI）は実績値または同時性〜先行性の指標だが、**予測・見通し（forward-looking）チャネルが皆無**だった。IMF World Economic Outlook（WEO）は GDP成長率・インフレ率の公式見通しを年2回（4月・10月）公表しており、実績値とは性質の異なる情報を追加できる。

Issue #284 は着手前に「OECD 同様、キー登録による自然な導入ゲートが無い可能性が高い」go/no-go 人判断と、「vintage（先読みバイアス）の扱い」「更新頻度が年2回のみで zscore/BIC の安定性が確保できるか」の設計検証を求めていた。実 API 検証（2026-07-11）の結果:

- `api.imf.org/external/sdmx/3.0` は匿名アクセス可・APIキー不要（structure/dataクエリとも HTTP 200 実測）。OECD 同様、キー登録ゲートは無い。
- **vintage の扱いが本チャネル最大の設計課題**。IMF.RES 配下の全22 dataflow を実測列挙した結果:
  - 公式「vintage archive」制度（`WEO_2025_OCT_VINTAGE` のような命名の dataflow）は **2025年10月開始の新しい仕組みで1本のみ**存在。過去vintageの体系的な提供はまだ薄い。
  - 現行（最新）dataflow（`+`）は**公式vintage境界と無関係に随時改定される**ことを実証済み：`WEO_2025_OCT_VINTAGE`（Oct2025固定）と当時の現行 dataflow（v9.0.0）は同一の `COUNTRY_UPDATE_DATE`（9/26/2025）属性を持ちながら日本の2024年GDP成長率が `0.104` vs `-0.24232` と異なる値を返した。つまり現行 dataflow を過去日付に遡って割り当てると先読みバイアス化するため、これは backfill には使えない。
  - IMF 公式「Historical WEO Forecasts Database」（`WEOhistorical.xlsx`・匿名で直接ダウンロード可・HTTP 200 実測）が **vintage×国×指標×対象年の point-in-time パネル**を1990年から収録しており、先読みバイアスの心配なくバックフィルできる。2026-07-11取得版は Spring1990〜Fall2022 まで収録（実データ確認：66件/系列、1990-05-16〜2022-11-15）。
  - `www.imf.org` の HTML ページ（`download-entire-database` 等）は bot 保護で 403（UA偽装でも不可）。ただし静的ファイル直リンク（`WEOhistorical.xlsx` 等）は **Range ヘッダー付きリクエストなら 200/206 で応答する**（bot判定の実装差・実証済み。プレーンGETは403）。

## Decision

1. **2系列を追加**：`JP_WEO_GDP_FCAST`（実質GDP成長率見通し・翌年）・`JP_WEO_CPI_FCAST`（インフレ率見通し・翌年）。対象は「vintage発表年の翌年」の予測値（＝「今年」の見通しではなく明確に forward-looking な1年先予測を採用し、既に判明済みの当年実績に近い値との混同を避ける）。

2. **2系統のデータソースを併用**（`collector_prices.py`）:
   - **バックフィル**：`fetch_imf_weo_historical` が `WEOhistorical.xlsx` を1回のfetchで取得・パースし、vintage（Spring/Fall）ごとの翌年予測値を抽出する。ダウンロードは `Range: bytes=0-` ヘッダー付きで行う（bot保護回避・実証済み）。trade_date は vintage 公表日（4月/10月の月初）+ `lag_days=45`。
   - **継続収集**：`fetch_imf_weo_current` が現行 dataflow から「収集日時点で分かっている翌年予測値」を1点取得し、**trade_date=収集日そのもの**で追加する。他の市場系列（Yahoo/stooq等）と同じ「その日に真に既知だった値」方式のため先読みバイアスが原理的に発生しない。lag_days は適用しない（既に「今日」に紐づけているため）。

3. **既知の空白を許容**：2023年4月〜2025年4月の4vintage分は WEOhistorical.xlsx にも公式vintage archive にも収録されておらず復元不可。継続収集の開始（本ADR以降）から自然に埋まっていく。zscore の疎さ懸念は、バックフィル66件（1990-2022・32年分）で十分に解消される。

4. **`_MACRO_MAP` では zscore 変換を採用**。GDP成長率・インフレ率見通しとも符号が反転しうる（マイナス成長・デフレ見通し）ため、既存の実績GDP（yoy）とは区別し、貿易収支と同じ規約（zscore）を適用する。

5. **APIキー不要のため常時収集**（BOJ/OECD/ESRIコネクタと同じゲート無し方式）。

## Considered Options

- **現行dataflowのみで簡略実装（vintage日付を計算で割り当て）**（却下）：現行dataflowが公式vintage境界と無関係に随時改定される実証結果（COUNTRY_UPDATE_DATE同一でも値が異なる）を踏まえると、過去日付割当は先読みバイアスを埋め込む。安全性を優先し「今日」固定に統一。
- **imf-reader（PyPI・ONE Campaign）のスクレイピング方式を利用**（却下）：`download-entire-database` ページ自体が bot 保護で403（UA偽装でも不可・実測）となり脆弱。`WEOhistorical.xlsx` 直リンクの方が単純・安定（新規依存も不要）。
- **当年（vintage発表年）の予測値を採用**（却下）：vintage発表時点で既に3〜9か月経過しており実績データに近づいている（forward-lookingの意味が薄れる）。翌年予測の方が明確にforward-looking。
- **新規依存追加**（不要）：`openpyxl==3.1.5`・`pandas==3.0.2` は既に requirements.txt にpin済みで、xlsxパースに追加ライブラリ不要。

## Consequences

- **新規収集サーフェス**：`fetch_imf_weo_historical`/`fetch_imf_weo_current`/`_parse_imf_weo_sheet`（`collector_prices.py`）・`IMF_SERIES`/`IMF_HIST_URL`/`IMF_BASE_URL` 定数・`collect_macro_data` への結線・`_MACRO_MAP`/`MACRO_FEATURE_OPTIONS`（`plugins/macro_snapshots.py`）へ `macro_jp_weo_gdp_fcast_zscore`/`macro_jp_weo_cpi_fcast_zscore` 追記。
- **新規 env var なし**：認証不要のため Render / GitHub Actions Secrets への追加作業は発生しない。
- **収集コスト**：`WEOhistorical.xlsx` は約8.6MBあり、収集のたびに全量ダウンロードする（年2回しか内容が変わらないため無駄は大きいが、現状はESRI/OECDと同型の「常時再取得」方針に合わせて簡潔さを優先。将来コストが問題になれば条件付き取得（If-Modified-Since等）を検討）。
- **検証**：本番初回実行（GitHub Actions・Azure IP）で `WEOhistorical.xlsx` への疎通確認（Range ヘッダー workaround が Azure IP でも有効か）。pooled BIC で既存チャネルとの直交性を確認。
- **ドキュメント更新**：DEPLOYMENT.md（外部サービス制約に IMF 節を追加）・GOTCHAS.md（vintage先読みバイアス・bot保護Range回避の注意）・ARCHITECTURE.md（マクロ収集チャネル）・MODELS.md（特徴量追加）。
- **既知の空白（2023-04〜2025-04）は許容**：将来 IMF が過去vintageのarchive制度を拡充した場合、追加のvintage dataflowを取り込んで空白を埋める余地を残す。
