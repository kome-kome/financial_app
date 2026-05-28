# 日本株財務分析ツール — 完全アーキテクチャ図

> **閲覧方法**: VS Code に「Markdown Preview Mermaid Support」拡張をインストールし、`Ctrl+Shift+V` でプレビューを開くと図が表示されます。

---

## 目次

1. [全体構成図（コンポーネント図）](#1-全体構成図コンポーネント図)
2. [ユースケース図](#2-ユースケース図)
3. [データベース設計（ER図）](#3-データベース設計er図)
4. シーケンス図
   - [4-1. 財務データ収集フロー](#4-1-財務データ収集フロー)
   - [4-2. 株価履歴収集フロー](#4-2-株価履歴収集フロー)
   - [4-3. 認証フロー](#4-3-認証フロー)
   - [4-4. OLS回帰分析フロー](#4-4-ols回帰分析フロー)
   - [4-5. スクリーニングフロー](#4-5-スクリーニングフロー)
   - [4-6. Zスコア正規化フロー](#4-6-zスコア正規化フロー)
   - [4-7. エラー・キャンセルフロー](#4-7-エラーキャンセルフロー)
5. [画面遷移図](#5-画面遷移図)
6. [データ変換フロー](#6-データ変換フロー財務データが分析結果になるまで)
7. [プラグインシステム（クラス図）](#7-プラグインシステムクラス図)
8. [REST API エンドポイント一覧](#8-rest-api-エンドポイント一覧)
9. [デプロイ構成図](#9-デプロイ構成図)
10. [ファイル役割一覧](#10-ファイル役割一覧)

---

## 1. 全体構成図（コンポーネント図）

> ブラウザ・サーバー・DB・外部APIの全体像と接続関係を示します。
> ローカルと Render は同一 Supabase DB を共有し、役割で使い分けます。

```mermaid
graph LR
    subgraph USER["👤 ユーザー（ブラウザ）"]
        direction TB
        LOGIN["🔐 ログイン画面\nlogin.html"]
        D["🏠 ダッシュボード\ndashboard.html\n企業数・収録状況サマリー"]
        C["📦 収集管理\ncollection.html\n財務収集/株価収集/市場データ更新/DB閲覧\n（4タブ構成・ウィザードUX）"]
        A["📊 分析画面\nanalysis.html\n乖離分析/プラグイン/バックテスト\n（4タブ構成・ステータスバー・ウィザードUX）"]
        M["📖 モデル解説\nmodels.html\n数式・参考文献・DOIリンク"]
        DB["🗃️ DB ビューア\ndb.html\nスキーマ/プレビュー/統計/リレーション/ドリルダウン"]
    end

    subgraph LOCAL["💻 ローカル PC（制限なし）"]
        direction TB
        API_L["⚡ api.py\n全操作可能\n・全件収集\n・株価履歴再構築\n・J-Quants大量収集\n・分析・スクリーニング"]
        COL_L["🔄 collector.py\n・EDINET全社XBRL収集\n・stooq株価取得\n・JPX業種補完"]
    end

    subgraph RENDER["☁️ Render（軽量モード RENDER_LIGHT_MODE=true）"]
        direction TB
        API_R["⚡ api.py\n・差分収集のみ許可\n・全件収集はブロック（403）\n・株価履歴・J-Quantsはブロック（403）\n・スクリーニング・分析は通常通り\n・自動収集なし（手動のみ）"]
        COL_R["🔄 collector.py\n差分収集・市場データ更新"]
    end

    subgraph SUPABASE["🗄️ Supabase PostgreSQL（共有DB）"]
        direction TB
        CO[("companies\n企業マスタ\n約4,000社")]
        FR[("financial_records\n財務データ\nBS / PL / CF")]
        SPH[("stock_price_history\n日次株価履歴\nOHLCV")]
        MD[("macro_data\n為替・金利・指数")]
        CL[("collection_logs\n収集ジョブログ")]
    end

    subgraph EXT["🌍 外部サービス"]
        direction TB
        EDINET["📋 EDINET API\n金融庁\n有価証券報告書\nXBRL形式"]
        STOOQ["📈 stooq API\n現在株価\n日次OHLCV"]
        JPX["🏢 JPX（東証）\n上場会社一覧Excel\nTSE33業種コード"]
    end

    USER -->|"HTTP / REST / SSE"| LOCAL
    USER -->|"HTTP / REST / SSE"| RENDER
    LOCAL <-->|"SQL (Supabase)"| SUPABASE
    RENDER <-->|"SQL (Supabase)"| SUPABASE
    COL_L -->|"HTTPリクエスト"| EXT
    COL_R -->|"HTTPリクエスト"| EXT
```

---

## 2. ユースケース図

> ユーザーがこのツールで「できること」の全体像です。

```mermaid
graph TD
    U(["👤 ユーザー"])

    subgraph AUTH["🔐 認証"]
        A1["ログイン（パスワード認証）"]
        A2["パスワードリセット（回復キー）"]
        A3["認証なし開発モード"]
    end

    subgraph COLLECT["📦 データ収集"]
        C1["差分収集\n新しいデータだけ取得"]
        C2["全件収集\n全上場企業を一括取得"]
        C3["株価履歴収集\n日次OHLCVを保存"]
        C4["収集を途中で停止"]
        C5["個別企業の再取得"]
        C6["手動差分収集\n過去1年・skip_existing"]
        C7["マクロデータ収集\n為替・金利・指数・コモディティ"]
    end

    subgraph VIEW["🗃️ データ閲覧"]
        V1["企業を名前・業種で検索"]
        V2["財務データの詳細確認\nBS / PL / CF"]
        V3["株価履歴の確認\n直近30日のOHLCV"]
        V4["BS/PL/CF 再分類ビュー"]
        V5["CSVエクスポート"]
        V6["DBビューア\nスキーマ・プレビュー・統計・リレーション"]
        V7["企業横断ドリルダウン\nedinet_code → 全テーブル"]
    end

    subgraph SCREEN["🔍 スクリーニング"]
        S1["財務条件で銘柄を絞り込み\nPER・PBR・ROE・自己資本比率等"]
        S2["条件の保存・読込"]
        S3["スクリーニング結果のCSV出力"]
    end

    subgraph ANALYZE["📊 分析"]
        AN1["OLS回帰分析\n財務指標→理論株価を推定"]
        AN2["乖離分析\n割安・割高ランキング"]
        AN3["Zスコア正規化\n年度内での相対順位"]
        AN4["推薦プラグイン\n複合スコアでランキング"]
        AN5["業種別OLS分析"]
    end

    subgraph QUALITY["✅ 品質管理"]
        Q1["データ品質チェック\nNULL率・外れ値確認"]
        Q2["EDINET収録状況確認"]
        Q3["株価データ収録状況確認"]
        Q4["株価履歴収録状況確認"]
    end

    U --> AUTH
    U --> COLLECT
    U --> VIEW
    U --> SCREEN
    U --> ANALYZE
    U --> QUALITY
```

---

## 3. データベース設計（ER図）

> 5つのテーブルの構造と主要カラム、テーブル間の関係を示します。
> `||--o{` は「1対多」（1社に対して複数の財務レコードが存在する）を意味します。
> `macro_data` は企業に紐づかない独立テーブル（マクロ環境データ）です。

```mermaid
erDiagram
    companies {
        int       id            PK  "自動採番ID"
        string    edinet_code   UK  "EDINETコード（E00001など）"
        string    sec_code          "証券コード（4桁 例:7203）"
        string    name              "会社名"
        string    industry          "業種（TSE33業種）"
        string    market            "市場区分（プライム/スタンダード/グロース）"
        int       fiscal_month      "決算月（3=3月決算など）"
        string    accounting_standard "会計基準（JGAAP/IFRS/US-GAAP）"
        datetime  created_at        "登録日時"
        datetime  updated_at        "更新日時"
    }

    financial_records {
        int     id                  PK "自動採番ID"
        string  edinet_code         FK "企業への紐付け"
        string  sec_code               "証券コード"
        int     year                   "決算年度（例:2024）"
        string  period_end             "決算期末日（YYYY-MM-DD）"
        string  doc_id                 "EDINET書類管理番号"
        string  accounting_standard    "会計基準"
        float   pl_revenue             "売上高（円）"
        float   pl_cost_of_sales      "売上原価（円）"
        float   pl_gross_profit       "売上総利益（円）"
        float   pl_sga                "販売費及び一般管理費（円）"
        float   pl_operating_profit   "営業利益（円）"
        float   pl_nonoperating_income "営業外損益純額=経常-営業（円）"
        float   pl_net_income          "純利益（円）"
        float   pl_eps                 "EPS 1株利益（円）"
        float   bs_total_assets        "総資産（円）"
        float   bs_receivables         "売掛金（円）"
        float   bs_inventory           "棚卸資産（円）"
        float   bs_buildings           "建物及び構築物（円）"
        float   bs_machinery           "機械装置（円）"
        float   bs_intangible_assets   "無形固定資産（円）"
        float   bs_payables            "買掛金（円）"
        float   bs_bonds_payable       "社債（円）"
        float   bs_paid_in_capital     "資本金（円）"
        float   bs_retained_earnings   "利益剰余金（円）"
        float   bs_total_equity        "純資産（円）"
        float   bs_bps                 "BPS 1株純資産（円）"
        float   bs_investment_securities "投資有価証券（円・清原式NC用）"
        float   cf_operating_cf        "営業キャッシュフロー（円）"
        float   cf_free_cf             "フリーCF=営業CF+投資CF（円）"
        float   op_margin              "営業利益率（%）"
        float   roe                    "ROE 自己資本利益率（%）"
        float   equity_ratio           "自己資本比率（%）"
        float   de_ratio               "D/Eレシオ"
        float   net_cash               "ネットキャッシュ（円・清原式）"
        float   nc_ratio               "ネットキャッシュ比率=net_cash/時価総額"
        float   rev_growth             "売上高成長率（%）"
        float   z_roe                  "ROEのZスコア（年度内正規化）"
        float   z_op_margin            "営業利益率のZスコア"
        float   z_nc_ratio             "NC比率のZスコア（年度内正規化）"
        float   stock_price            "株価（収集時点）"
        float   market_cap             "時価総額（百万円）※単位注意"
        float   per                    "PER 株価収益率"
        float   pbr                    "PBR 株価純資産倍率"
        float   predicted_market_cap   "OLS予測時価総額（百万円）"
        float   gap_ratio              "乖離率（%）=(実際-予測)/予測"
        datetime created_at            "登録日時"
        datetime updated_at            "更新日時"
    }

    stock_price_history {
        int     id           PK "自動採番ID"
        string  edinet_code  FK "企業への紐付け"
        string  sec_code        "証券コード"
        string  trade_date      "取引日（YYYY-MM-DD）"
        float   open            "始値"
        float   high            "高値"
        float   low             "安値"
        float   close           "終値（NOT NULL）"
        float   volume          "出来高"
        datetime created_at     "登録日時"
    }

    collection_logs {
        int      id                 PK "自動採番ID"
        string   job_type              "収集種別（full/incremental/single）"
        string   status                "状態（running/done/error/resolved）"
        datetime started_at           "開始日時"
        datetime finished_at          "終了日時"
        int      companies_processed  "処理企業数"
        int      records_saved        "保存レコード数"
        int      errors_count         "エラー件数"
        text     message              "補足メッセージ"
    }

    macro_data {
        int      id           PK "自動採番ID"
        string   series_code     "系列コード（USDJPY/US10Y/NIKKEI225 等）"
        string   series_name     "表示名"
        string   category        "fx / rate / equity / commodity"
        string   trade_date      "取引日（YYYY-MM-DD）"
        float    open            "始値"
        float    high            "高値"
        float    low             "安値"
        float    close           "終値（NOT NULL）"
        float    volume          "出来高（FX/金利系は NULL）"
        datetime created_at      "登録日時"
    }

    xbrl_raw_documents {
        int      id              PK "自動採番ID"
        string   doc_id          UK "EDINET書類管理番号（UNIQUE）"
        string   edinet_code        "EDINETコード（インデックス）"
        string   period_end         "決算期末日（YYYY-MM-DD）"
        bytes    elements_gz        "XBRL全行をgzip圧縮したJSON（BYTEA）"
        string   elements_format    "圧縮方式（gzip+json）"
        int      n_rows             "圧縮前の行数（健全性チェック用）"
        datetime fetched_at         "raw保存日時"
    }

    companies         ||--o{  financial_records    : "1社 → 複数年度の財務データ"
    companies         ||--o{  stock_price_history  : "1社 → 複数日分の株価履歴"
    xbrl_raw_documents }o--|| financial_records   : "doc_id で紐付け（再解析用）"
```

---

## 4-1. 財務データ収集フロー

> 「収集開始」ボタンから完了までの処理の流れです。

```mermaid
sequenceDiagram
    actor       User  as 👤 ユーザー
    participant UI    as 🌐 collection.html
    participant API   as ⚡ api.py
    participant BGT   as 🔄 バックグラウンドタスク
    participant EDI   as 📋 EDINET API
    participant JPX   as 🏢 JPX Excel
    participant DB    as 🗄️ PostgreSQL

    User ->> UI  : 「収集開始」をクリック
    UI   ->> API : POST /api/collect/start { years_back, skip_existing }
    API  ->> BGT : バックグラウンドタスクを起動
    API -->> UI  : 200 { "message": "収集開始しました" }
    UI   ->> API : GET /api/collect/stream（SSE接続開始）

    Note over BGT,EDI: フェーズ①: 企業マスタ取得（直近60日の書類一覧をスキャン）
    BGT  ->> EDI : 書類一覧API（日付ループ60日分）
    EDI -->> BGT : 上場企業リスト（約4,000社）
    BGT  ->> DB  : companies テーブルへ upsert（全件）
    BGT -->> API : on_progress → "[企業マスタ保存] X/Y社完了"
    API -->> UI  : SSEイベント（進捗%・ログ更新）

    Note over BGT,EDI: フェーズ②: 財務書類の取得（全書類をループ処理）
    loop 対象書類（数千件）を1件ずつ
        BGT  ->> EDI : XBRL取得 GET /documents/{doc_id}?type=5
        EDI -->> BGT : ZIPファイル（XBRL CSV含む）
        BGT  ->> BGT : ZIP解凍 → XBRLをパース<br/>BS/PL/CF に分類<br/>ROE・営業利益率など派生指標を計算
        BGT  ->> DB  : financial_records へ upsert
        BGT -->> API : on_progress → "[X/Y] 企業名(コード) 決算期末"
        API -->> UI  : SSEイベント
    end

    Note over BGT,JPX: フェーズ③: 業種データの補完
    BGT  ->> JPX : TSE33業種一覧Excelをダウンロード
    JPX -->> BGT : 証券コード→業種名の対応表
    BGT  ->> DB  : companies・financial_records の industry を更新

    Note over BGT,DB: フェーズ④: 後処理（収集完了後に自動実行）
    BGT  ->> DB  : 前期比成長率を全レコード再計算（calc_growth_rates）
    BGT  ->> DB  : Zスコア正規化を年度別に再計算（calc_zscore_normalization）

    BGT -->> API : 完了通知
    API -->> UI  : SSEイベント（running=false）
    UI  ->> User : 「収集完了」を表示
```

---

## 4-2. 株価履歴収集フロー

> stooq から日次OHLCV（始値・高値・安値・終値・出来高）を取得して保存するフローです。

```mermaid
sequenceDiagram
    actor       User  as 👤 ユーザー
    participant UI    as 🌐 collection.html（株価履歴タブ）
    participant API   as ⚡ api.py
    participant BGT   as 🔄 バックグラウンドタスク
    participant STQ   as 📈 stooq API
    participant DB    as 🗄️ PostgreSQL

    User ->> UI  : 「収集開始」をクリック（取得年数・最大社数を指定）
    UI   ->> API : POST /api/collect/history/start { years_back, max_companies }
    API  ->> BGT : バックグラウンドタスクを起動
    API -->> UI  : 200 { "message": "開始しました" }
    UI   ->> API : GET /api/collect/history/stream（SSE接続開始）

    Note over BGT,DB: DBから sec_code を持つ企業一覧を取得
    BGT  ->> DB  : SELECT edinet_code, sec_code, name FROM companies WHERE sec_code IS NOT NULL

    loop 全企業を1社ずつ（レート制限: 1.5秒/社）
        BGT  ->> STQ : GET stooq.com/q/d/l/?s={code}.jp&d1=...&d2=...&i=d
        STQ -->> BGT : CSV形式の日次OHLCV（日付,始値,高値,安値,終値,出来高）
        BGT  ->> BGT : CSVをパース → レコードリスト化
        BGT  ->> DB  : stock_price_history へ INSERT ... ON CONFLICT DO NOTHING<br/>（同じ日のデータは重複保存しない）
        BGT -->> API : on_progress → "[X/Y] 企業名(コード) 株価履歴取得中"
        API -->> UI  : SSEイベント（進捗%・ログ更新）
    end

    BGT -->> API : 完了通知 { inserted: N件 }
    API -->> UI  : SSEイベント（running=false）
    UI  ->> User : 「収集完了」を表示
    UI   ->> API : GET /api/collect/history/coverage（収録状況を更新表示）
```

---

## 4-3. 認証フロー

> `APP_PASSWORD` が設定されている場合のみ認証が有効になります。未設定時は開発モードとして全APIが素通りします。

```mermaid
sequenceDiagram
    actor       User  as 👤 ユーザー
    participant UI    as 🌐 login.html
    participant MW    as 🛡️ 認証ミドルウェア（api.py）
    participant AUTH  as 🔐 /api/auth/login

    Note over User,AUTH: ① 初回アクセス時
    User ->> MW   : GET /collection
    MW  -->> User : 302 → /login にリダイレクト（APP_PASSWORD設定時のみ）

    Note over User,AUTH: ② ログイン
    User ->> UI   : パスワードを入力して「ログイン」
    UI   ->> AUTH : POST /api/auth/login { "password": "***" }
    AUTH ->> AUTH : hmac.compare_digest() でパスワードを検証<br/>（タイミング攻撃対策）
    AUTH ->> AUTH : トークン生成: base64( timestamp + ":" + HMAC-SHA256(timestamp) )
    AUTH -->> UI  : 200 { "token": "xxxx" }
    UI   ->> UI   : localStorage に token を保存
    UI  -->> User : /collection にリダイレクト

    Note over User,AUTH: ③ API呼び出し時（毎回）
    User ->> MW   : GET/POST /api/... ヘッダー: Authorization: Bearer {token}
    MW   ->> MW   : トークンを base64 デコード
    MW   ->> MW   : HMAC-SHA256 で署名を検証
    MW   ->> MW   : タイムスタンプで有効期限を確認（30日）
    alt 検証OK
        MW -->> User : 200 正常レスポンス
    else 検証NG / 期限切れ
        MW -->> User : 401 Unauthorized
        UI  -->> User : /login にリダイレクト
    end

    Note over User,AUTH: ④ パスワードリセット（APP_RECOVERY_KEY使用）
    User ->> AUTH : POST /api/auth/reset-password { recovery_key, new_password }
    AUTH ->> AUTH : hmac.compare_digest() で回復キーを検証
    AUTH ->> AUTH : .env ファイルの APP_PASSWORD を更新
    AUTH -->> User : 200 { "message": "パスワードを更新しました" }
```

---

## 4-4. 業種別OLS分析フロー

> 業種ごとに個別OLSを実行して理論価格を推定し、乖離率（割安・割高度合い）を計算するフローです。

```mermaid
sequenceDiagram
    actor       User  as 👤 ユーザー
    participant UI    as 🌐 analysis.html
    participant API   as ⚡ api.py
    participant PLG   as 🧩 plugins/sector_ols.py
    participant GAP   as 🧩 plugins/gap_analysis.py
    participant DB    as 🗄️ PostgreSQL

    Note over User,DB: ① 業種別OLS分析の実行（target=stock_price 固定・per-share 説明変数）
    User ->> UI  : 説明変数（per-share[円/株]）・業種最低サンプル数・正則化を選択して「実行」
    UI   ->> API : POST /api/plugins/sector_ols/run { target=stock_price, features:[ps_*...], min_samples }
    API  ->> PLG : plugin.execute(params, db)

    PLG  ->> DB  : SELECT financial_records（最新年度）
    DB  -->> PLG : 全レコード

    loop 各業種
        PLG  ->> PLG : 各 record で shares = bs_total_equity / bs_bps を計算<br/>bs_bps NULL/0 の銘柄はスキップ
        PLG  ->> PLG : 派生 per-share (ps_*) を「絶対額 / shares」で実行時計算
        PLG  ->> PLG : winsorize() で外れ値を p1-p99 にクリッピング
        PLG  ->> PLG : normalize() で特徴量を z-score 正規化（業種内）
        PLG  ->> PLG : ols() / ridge_regression() で β を推定
        PLG  ->> DB  : predicted_market_cap（円/株 → 百万円換算）/ gap_ratio を書き戻し
    end

    PLG -->> API : { sector_stats, results }
    API -->> UI  : 業種別 R²・予測値一覧

    Note over User,DB: ② 乖離分析の実行（業種別OLS完了後に利用可能）
    User ->> UI  : 「乖離分析」タブを選択
    UI   ->> API : GET /api/gap-analysis?sort=asc
    API  ->> GAP : plugin.execute(params, db)
    GAP  ->> DB  : SELECT WHERE gap_ratio IS NOT NULL
    DB  -->> GAP : 予測値付きレコード
    GAP -->> API : { results: [{company, gap_ratio, market_cap, predicted, ...}] }
    API -->> UI  : ランキングデータ
    UI  -->> User: 割安・割高ランキング表を表示
```

---

## 4-5. スクリーニングフロー

> 財務条件を指定して条件に合う銘柄を絞り込むフローです。

```mermaid
sequenceDiagram
    actor       User  as 👤 ユーザー
    participant UI    as 🌐 collection.html（スクリーニングタブ）
    participant API   as ⚡ api.py
    participant DB    as 🗄️ PostgreSQL

    User ->> UI  : 条件を入力（PER≤20, ROE≥10%, 自己資本比率≥40% など）
    UI   ->> API : POST /api/screen { max_per:20, min_roe:10, min_equity_ratio:40, ... }

    Note over API,DB: 各企業の「最新年度」レコードのみを対象にサブクエリで絞り込む
    API  ->> DB  : SELECT fr.* FROM financial_records fr<br/>JOIN (SELECT edinet_code, MAX(year)) subq<br/>WHERE [各条件フィルタ]<br/>LIMIT 200
    DB  -->> API : 条件に合致したレコード一覧

    API -->> UI  : { count: N, results: [...] }
    UI  -->> User: 条件合致銘柄の一覧テーブルを表示<br/>（PER・PBR・ROE・営業利益率・自己資本比率・配当利回・スコア）

    opt 銘柄詳細の確認
        User ->> UI  : 銘柄名をクリック
        UI   ->> API : GET /api/financials/{edinet_code}（財務データ全年度）
        UI   ->> API : GET /api/stock/history/{edinet_code}?days=30（株価履歴）
        Note over UI: Promise.all で並列取得
        API -->> UI  : 財務データ + 株価OHLCV（直近30日）
        UI  -->> User: 詳細モーダル（財務サマリー + 株価履歴テーブル）
    end
```

---

## 4-6. Zスコア正規化フロー

> 年度ごとに業界内での相対位置（偏差値に近い概念）を計算するフローです。

```mermaid
sequenceDiagram
    participant API   as ⚡ api.py
    participant BGT   as 🔄 収集完了後の後処理
    participant DB    as 🗄️ PostgreSQL

    Note over API,BGT: 収集完了後・または手動実行時に自動的に呼ばれる

    BGT  ->> DB  : SELECT DISTINCT year FROM financial_records
    DB  -->> BGT : [2019, 2020, 2021, 2022, 2023, 2024]

    loop 各年度を個別に処理（年度をまたいで混ぜない）
        BGT  ->> DB  : SELECT * FROM financial_records WHERE year = {year}
        DB  -->> BGT : その年度の全レコード

        Note over BGT: 年度内の平均(μ)と標準偏差(σ)を計算
        loop 各指標（pl_revenue, op_margin, roe, equity_ratio, cf_ratio, pl_eps, de_ratio）
            BGT  ->> BGT : values = [各社の値]<br/>μ = mean(values)<br/>σ = stdev(values)<br/>Z = (値 - μ) / σ
            BGT  ->> DB  : UPDATE financial_records<br/>SET z_{field} = Z WHERE year={year}
        end
    end

    BGT -->> API : 正規化完了
```

---

## 4-7. エラー・キャンセルフロー

> 収集中にエラーが発生した場合、またはユーザーが停止ボタンを押した場合の挙動です。

```mermaid
sequenceDiagram
    actor       User  as 👤 ユーザー
    participant UI    as 🌐 collection.html
    participant API   as ⚡ api.py
    participant BGT   as 🔄 バックグラウンドタスク
    participant DB    as 🗄️ PostgreSQL

    Note over User,BGT: ケース① ユーザーによる手動停止
    User ->> UI  : 「停止」ボタンをクリック
    UI   ->> API : POST /api/collect/stop
    API  ->> API : _job_status["cancel_requested"] = True
    API -->> UI  : 200 { "message": "停止リクエストを送信..." }

    Note over BGT: 次のループ先頭で cancel_check() を確認
    BGT  ->> BGT : if cancel_check(): return True（cancelled）
    BGT  ->> DB  : DB.commit()（処理済み分は保存）
    BGT  ->> DB  : collection_logs.status = "done"<br/>message = "ユーザーにより停止"
    BGT -->> API : running = False
    API -->> UI  : SSEイベント（running=false）
    UI  -->> User: 「収集停止」を表示

    Note over User,BGT: ケース② EDINET APIエラー（1件単位でスキップ）
    BGT  ->> BGT : try: XBRL取得・パース
    BGT  ->> BGT : except: log.warning(f"スキップ: {doc_id}")
    Note over BGT: エラーでも収集ループは継続（次の書類へ）

    Note over User,BGT: ケース③ 重大エラー（ループ全体が止まる）
    BGT  ->> BGT : 予期しない例外が発生
    BGT  ->> DB  : collection_logs.status = "error"<br/>message = str(e)
    BGT -->> API : running = False
    API -->> UI  : SSEイベント（running=false）
    UI  -->> User: エラー状態を表示
```

---

## 5. 画面遷移図

> 5画面とその中のタブ構成、遷移ルートを示します。

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Login : APP_PASSWORD設定時<br/>未認証でアクセス

    Login : 🔐 ログイン画面\nlogin.html\nパスワード入力

    Login --> Dashboard : 認証成功

    state Dashboard {
        [*] --> DashMain
        DashMain : 🏠 ダッシュボード\ndashboard.html\n・企業数・レコード数\n・最新年度\n・API接続状況
    }

    state Collection {
        direction TB
        [*] --> Collect
        Collect     : 📦 財務データ収集\n差分収集 / 全件収集 / 停止\nスケジューラー管理\nデータ品質チェック（折りたたみ）
        StockMarket : 📈 株価・市場データ\n市場データ更新 / stooq / J-Quants\n※ウィザード: 財務データ0件時にボタン無効
        DataView    : 🗃️ データ確認\nDBブラウザ（企業検索・財務詳細）\nBS/PL/CF 再分類ビュー
        Screening   : 🔍 スクリーニング\n条件絞り込み・CSV出力

        Collect     --> StockMarket : タブ切替
        Collect     --> DataView    : タブ切替
        Collect     --> Screening   : タブ切替
    }

    state Analysis {
        direction TB
        [*] --> Gap
        Gap      : 🎯 乖離分析\n割安・割高ランキング\n（財務データ必須・ウィザード制御）
        Plugin   : 🧩 プラグイン分析\n推薦・総合リターン予測など\n（財務データ必須・ウィザード制御）
        Backtest : 🔁 バックテスト\n過去のスコアリング精度検証\n（株価データ必須・ウィザード制御）

        Gap      --> Plugin   : タブ切替
        Gap      --> Backtest : タブ切替
    }

    note right of Analysis
      ステータスバー（常時表示）:
      財務データ件数 / 株価データ件数
      不足時に各実行ボタンを自動 disabled
    end note

    state Models {
        [*] --> ModelDoc
        ModelDoc : 📖 モデル解説\nmodels.html\n・数式・パラメータ表\n・参考文献DOIリンク\n（7モデル）
    }

    state DBViewer {
        direction TB
        [*] --> DBTables
        DBTables    : 🗃️ テーブル一覧\n4テーブル行数・カラム数・最終更新
        DBSchema    : 📋 スキーマ\nカラム定義・NULL率・PK/FK
        DBPreview   : 🔍 プレビュー\nページネーション・ソート・フィルタ・CSV
        DBStats     : 📊 統計サマリー\nmin/max/avg/p50/p99（数値）・distinct（文字列）
        DBER        : 🔗 リレーション図\nFK 関係を ER 風に可視化
        DBDrill     : 🎯 企業ドリルダウン\nedinet_code → 全テーブル横断
        DBTables    --> DBSchema  : 切替
        DBTables    --> DBPreview : 切替
        DBTables    --> DBStats   : 切替
    }

    state Company {
        direction TB
        [*] --> CoSearch
        CoSearch : 🔎 企業検索\ncompany.html\n企業名・証券コードで検索
        CoDetail : 🏢 企業詳細\n業績/財務(BS)/CF/per-share・配当/\nバリュエーション/株価/業種内Z/ネットキャッシュ/同業比較\nChart.js 時系列グラフ
        CoSearch --> CoDetail : 企業選択（/company/{edinet_code}）
    }

    [*]        --> Dashboard  : APP_PASSWORD未設定時\n（開発モード）
    Dashboard  --> Collection : 「収集ページへ」
    Dashboard  --> Analysis   : 「分析ページへ」
    Dashboard  --> Company    : 「企業を検索して開く」
    Dashboard  --> Models     : 「モデル解説を開く」
    Dashboard  --> DBViewer   : 「DB を開く」
    Collection --> Dashboard  : 「ホーム」
    Analysis   --> Dashboard  : 「ホーム」
    Analysis   --> Models     : 「モデル解説」リンク
    Collection --> Analysis   : 「分析」リンク
    Analysis   --> Collection : 「← データ収集ページへ」リンク
    DBViewer   --> Dashboard  : 「← ホーム」
    Company    --> Dashboard  : 「ダッシュボード」
    Collection --> Company    : 企業名クリック（スクリーニング/DB一覧）
    Analysis   --> Company    : 企業名クリック（乖離/推薦/総合/NC/BT）
    DBViewer   --> Company    : ドリルダウンの「企業ページを開く」
```

---

## 6. データ変換フロー（財務データが分析結果になるまで）

> XBRLデータが割安銘柄ランキングになるまでの変換過程を示します。

```mermaid
flowchart TD
    A["📋 EDINET\nXBRL書類（ZIP）\n有価証券報告書"]

    B["🔄 XBRLパース\nparse_xbrl_csv()\n─────────────────\nCSV内の要素名をXBRL_MAPで照合\n連結優先（NonConsolidated除外）\n前期比較データ除外（Prior含む行）"]

    C["📦 データ分類\n─────────────────\nBS: 総資産・純資産・現金など\nPL: 売上高・営業利益・純利益・EPSなど\nCF: 営業CF・投資CF・財務CFなど"]

    D["🧮 派生指標計算\ncalc_derived()\n─────────────────\nROE = 純利益 ÷ 純資産\n営業利益率 = 営業利益 ÷ 売上高\n自己資本比率 = 純資産 ÷ 総資産\nD/Eレシオ = 負債 ÷ 純資産\nフリーCF = 営業CF + 投資CF"]

    E["🗄️ DB保存\nupsert_financial()\n─────────────────\n(edinet_code, year, period_end)\nで重複チェック → 存在すれば更新"]

    F["📈 株価取得\nstooq API\n─────────────────\n株価 → PER = 株価÷EPS\n株価 → PBR = 株価÷BPS\n時価総額 = 株価×（純資産÷BPS）"]

    G["📐 前期比成長率\ncalc_growth_rates()\n─────────────────\nPostgreSQL の LAG() window function で\nedinet_code 単位の前期値と比較\n（DB 側で完結、大規模データでも OOM 回避）\n売上成長率・営業利益成長率・EPS成長率"]

    H["📊 Zスコア正規化\ncalc_zscore_normalization()\n─────────────────\n年度ごとに計算（年度混在禁止）\nZ = (値 - 年度内平均) ÷ 年度内標準偏差\nROE・営業利益率・自己資本比率など7指標"]

    I["🧩 業種別OLS分析\nplugins/sector_ols.py\n─────────────────\n業種ごとに個別OLSを実行\nwinsorize で外れ値除去（p1-p99）\nnormalize で z-score 正規化（業種内）\n→ 理論時価総額をDBに書込み"]

    J["🎯 乖離率計算\nplugins/gap_analysis.py\n─────────────────\ngap_ratio = (実際 - 予測) ÷ 予測 × 100\nマイナス → 予測より安い（割安候補）\nプラス  → 予測より高い（割高候補）"]

    K["🏆 割安銘柄ランキング\nanalysis.html\n─────────────────\n乖離率の小さい順に表示\n収束スコア・半減期は参考値として表示"]

    A --> B --> C --> D --> E --> F --> G --> H --> I --> J --> K

    style A  fill:#1e3a5f,color:#93c5fd
    style E  fill:#052e16,color:#86efac
    style H  fill:#1c1400,color:#fcd34d
    style K  fill:#2e1065,color:#c4b5fd
```

---

## 7. プラグインシステム（クラス図）

> 分析機能を差し込み式（プラグイン）で拡張できる構造を示します。

```mermaid
classDiagram
    class AnalysisPlugin {
        <<abstract>>
        +str name
        +str label
        +str description
        +list depends_on
        +params_schema() dict
        +execute(params, db) dict
        +to_meta() dict
    }

    class GapAnalysisPlugin {
        +name = "gap_analysis"
        +label = "乖離分析"
        +depends_on = ["sector_ols"]
        +params_schema() 年度・ソート順
        +execute() gap_ratio計算→ランキング生成
    }

    class RecommendPlugin {
        +name = "recommend"
        +label = "推薦スクリーニング"
        +depends_on = []
        +params_schema() プリセット選択・指標ウェイト
        +execute() 複合スコアでランキング生成
    }

    class TotalReturnPlugin {
        +name = "total_return"
        +label = "総合リターン予測"
        +depends_on = []
        +params_schema() use_cf / use_sector_fe / n_folds / top_n
        +execute() Ohlson 型 OLS + 業種固定効果（オプション）
    }

    class SectorOLSPlugin {
        +name = "sector_ols"
        +label = "業種別OLS分析"
        +depends_on = []
        +params_schema() 業種選択・説明変数
        +execute() 業種内OLS→予測値計算
    }

    class PricePredictorPlugin {
        +name = "price_predictor"
        +label = "株価リターン予測"
        +depends_on = []
        +params_schema() 予測期間・価格/財務特徴量選択
        +execute() 月次スナップショット×OLS→期待リターンランキング
    }

    class NetCashAnalysisPlugin {
        +name = "net_cash_analysis"
        +label = "ネットキャッシュ分析"
        +depends_on = []
        +params_schema() 最低NC比率・業種・最低時価総額・年度
        +execute() 清原式NC・NC比率でランキング生成（OLS不使用）
    }

    class PluginRegistry {
        -dict _registry
        +_load() プラグインファイルを自動スキャン
        +get_plugin(name) AnalysisPlugin
        +list_plugins() list
    }

    class Utils {
        <<module>>
        +ols(X, y) coefficients, r2
        +normalize(values, method) normalized_values
        +winsorize(values, p_low, p_high) clipped_values
        +walk_forward_cv(X, y, n_splits) cv_metrics
    }

    AnalysisPlugin <|-- GapAnalysisPlugin
    AnalysisPlugin <|-- RecommendPlugin
    AnalysisPlugin <|-- TotalReturnPlugin
    AnalysisPlugin <|-- SectorOLSPlugin
    AnalysisPlugin <|-- PricePredictorPlugin
    AnalysisPlugin <|-- NetCashAnalysisPlugin

    PluginRegistry --> AnalysisPlugin : 管理・呼び出し

    GapAnalysisPlugin    --> Utils : 統計処理を使用
    SectorOLSPlugin      --> Utils : ols() / winsorize() / normalize() を使用
    PricePredictorPlugin --> Utils : ols() / winsorize() / walk_forward_cv_monthly() を使用

    note for GapAnalysisPlugin "業種別OLS分析の実行後でないと\npredicted_market_capが空のため404になる"
    note for PricePredictorPlugin "StockPriceHistory + FinancialRecord を結合\n月次スナップショット × 全企業でパネルデータ構築\nルックアヘッドバイアス禁止: period_end + 45日ラグ厳守"
    note for NetCashAnalysisPlugin "清原達郎『わが投資術』式\nNC = 流動資産 + 投資有価証券×0.7 − 総負債\nOLS不使用・会計値からの直接計算"
    note for Utils "numpy / scipy は使用しない\n（純Python実装のみ）\nwalk_forward_cv_monthly() を含む"
```

---

## 8. REST API エンドポイント一覧

> このツールが提供する全APIエンドポイントの一覧です。

```mermaid
graph LR
    subgraph PAGE["📄 ページ配信"]
        P1["GET /\ndashboard.html を返す"]
        P2["GET /collection\ncollection.html を返す"]
        P3["GET /analysis\nanalysis.html を返す"]
        P4["GET /login\nlogin.html を返す"]
        P5["GET /models\nmodels.html を返す\n（モデル解説・参考文献）"]
        P6["GET /db\ndb.html を返す\n（DBビューア）"]
        P7["GET /company\ncompany.html を返す\n（企業検索）"]
        P8["GET /company/{edinet_code}\ncompany.html を返す\n（個別企業の業績・財務・CF可視化）"]
    end

    subgraph OPS["🩺 運用"]
        H1["GET /health\n死活監視（DB疎通確認、認証不要）\n200=ok / 503=degraded"]
    end

    subgraph AUTH["🔐 認証 /api/auth/"]
        A1["POST /api/auth/login\nパスワード認証 → Bearerトークン発行"]
        A2["POST /api/auth/reset-password\n回復キーでパスワード変更"]
        A3["GET /api/auth/status\n認証が必要かどうかを返す"]
    end

    subgraph STATS["📊 統計 /api/stats"]
        S1["GET /api/stats\n企業数・レコード数・最新年度\n+ データ鮮度（最終更新日時・経過日数・期待最新年度・freshness判定）"]
        S2["GET /api/companies\n企業一覧（検索・業種・市場フィルタ）"]
        S3["GET /api/financials/{edinet_code}\n指定企業の全年度財務データ"]
        S4["GET /api/stock/history/{edinet_code}\n日次株価履歴（OHLCVリスト）"]
        S5["GET /api/export/csv\n財務データをCSVでダウンロード"]
    end

    subgraph COLLECT["📦 収集管理 /api/collect/"]
        C1["POST /api/collect/start\n財務データ収集を開始"]
        C2["POST /api/collect/stop\n財務データ収集を停止"]
        C3["GET /api/collect/stream\nSSE: 収集進捗をリアルタイム配信"]
        C4["GET /api/collect/status\n直近ジョブの状態確認"]
        C5["POST /api/collect/refresh/{edinet_code}\n1社だけ再取得"]
        C6["POST /api/collect/market-data\n株価データ更新を開始"]
        C7["POST /api/collect/market-stop\n株価更新を停止"]
        C8["GET /api/collect/market-stream\nSSE: 株価更新進捗"]
        C9["GET /api/collect/market-data/status\n株価更新の状態"]
        C10["POST /api/collect/history/start\n株価履歴収集を開始"]
        C11["POST /api/collect/history/stop\n株価履歴収集を停止"]
        C12["GET /api/collect/history/stream\nSSE: 株価履歴収集進捗"]
        C13["GET /api/collect/history/status\n株価履歴収集の状態"]
        C14["GET /api/collect/history/coverage\n収集済み社数・レコード数"]
        C14b["POST /api/collect/jquants/start\nJ-Quants日次データ収集（上書き更新）"]
        C14c["POST /api/collect/jquants/stop\nJ-Quants収集を停止"]
        C14d["GET /api/collect/jquants/stream\nSSE: J-Quants収集進捗"]
        C14e["GET /api/collect/jquants/status\nJ-Quants収集の状態"]
        C15["GET /api/collect/edinet-coverage\nEDINET収録状況"]
        C16["GET /api/collect/market-coverage\n株価データ収録状況"]
        C17["GET /api/collect/data-quality\nNULL率・外れ値チェック\n会計基準別サマリ（JGAAP/IFRS/US-GAAP）"]
        C18["POST /api/collect/industry\nJPX Excelから業種データを更新"]
        C19["POST /api/collect/macro/start\nマクロデータ収集（為替・金利・指数・コモディティ）"]
        C20["POST /api/collect/macro/stop\nマクロ収集を停止"]
        C21["GET /api/collect/macro/status\nマクロ収集の状態"]
        C22["GET /api/collect/macro/stream\nSSE: マクロ収集進捗"]
        C23["POST /api/collect/reparse/start\nxbrl_raw_documentsから再解析（EDINET通信なし）\nRender可・年度/EDINETコードフィルタ対応"]
        C24["POST /api/collect/reparse/cancel\n再解析を停止"]
        C25["GET /api/collect/reparse/stream\nSSE: 再解析進捗"]
    end

    subgraph MACRO["🌐 マクロデータ /api/macro/"]
        MA1["GET /api/macro/series\n系列カバレッジ一覧（件数・最古日・最新日）"]
        MA2["GET /api/macro/data/{series_code}\n指定系列の日次データ（OHLCV）"]
    end

    subgraph SCHED["🖱️ 手動差分収集 /api/scheduler/"]
        SC3["POST /api/scheduler/run-now\n差分収集を手動実行\n（過去1年・skip_existing）"]
    end

    subgraph ANALYSIS["📊 分析 /api/"]
        AN1["GET /api/plugins\n利用可能なプラグイン一覧"]
        AN2["POST /api/plugins/{name}/run\nプラグインを実行"]
        AN4["GET /api/gap-analysis\n乖離分析（旧互換エンドポイント）"]
        AN5["POST /api/screen\nスクリーニング（条件絞り込み）"]
        AN6["GET /api/recommend/presets\n推薦プリセット一覧"]
        AN7["POST /api/recommend\n推薦スクリーニング実行"]
        AN8["GET /api/backtest\n過去スコアリングの実績リターン検証\n?preset&months_ago&top_n / summary+percentiles"]
        AN9["GET /api/backtest/multi\n複数保有期間（3/6/12/18/24ヶ月）一括比較\n?preset&top_n"]
    end

    subgraph DBV["🗃️ DBビューア /api/db/"]
        DB1["GET /api/db/tables\n全テーブルの行数・カラム数・最終更新"]
        DB2["GET /api/db/schema/{table}\nカラム定義・NULL率・PK/FK"]
        DB3["GET /api/db/preview/{table}\n行プレビュー（ページネーション・ソート・フィルタ）"]
        DB4["GET /api/db/stats/{table}\n統計サマリー（min/max/avg/p50/p99/distinct）"]
        DB5["GET /api/db/relations\nテーブル間FKリレーション一覧"]
        DB6["GET /api/db/company/{edinet_code}\n企業別ドリルダウン（全テーブル横断）"]
        DB7["GET /api/db/export/{table}\nテーブルをCSVでダウンロード（フィルタ対応）"]
    end
```

---

## 9. デプロイ構成図

> **稼働中の本番環境**: Render（Web Service）+ Supabase（PostgreSQL）。
> 詳細な運用ガイドは [docs/DEPLOYMENT.md](DEPLOYMENT.md) を参照。

```mermaid
graph TB
    subgraph INTERNET["🌍 インターネット"]
        USER["👤 ユーザー（ブラウザ）"]
    end

    subgraph RENDER["☁️ Render（Free Plan）"]
        subgraph EDGE["エッジ"]
            EDGE_NODE["Render Edge\n・HTTPS終端（自動）\n・カスタムドメイン対応"]
        end
        subgraph APP["Web Service"]
            UV["uvicorn api:app\n--host 0.0.0.0 --port $PORT\n（512MB / 0.1 vCPU）\n15分アイドルでスピンダウン"]
        end
        subgraph CONFIG["設定"]
            ENV["Render 環境変数\n・DATABASE_URL\n・EDINET_API_KEY / JQUANTS_API_KEY\n・APP_PASSWORD / SECRET / RECOVERY\n・ALLOWED_ORIGIN"]
            YAML["render.yaml\n（IaC 定義）"]
        end
    end

    subgraph SUPABASE["☁️ Supabase"]
        PG[("PostgreSQL\nfinancial_db\nSSL 必須\n自動バックアップ")]
    end

    subgraph EXT["🌍 外部サービス"]
        EDINET["EDINET API\n金融庁"]
        STOOQ["stooq API\n株価"]
        JPX["JPX Excel\n東証"]
        JQUANTS["J-Quants API\n（任意）"]
    end

    subgraph CICD["🔁 CI/CD"]
        GH["GitHub main\nブランチ"]
    end

    USER   -->|"HTTPS"| EDGE_NODE
    EDGE_NODE -->|"HTTP（内部）"| UV
    UV     <-->|"SQL / TLS"| PG
    UV     -->|"HTTPS"| EXT
    ENV    -.->|"環境変数"| UV
    YAML   -.->|"インフラ定義"| ENV
    GH     -->|"push で自動デプロイ"| UV

    style ENV fill:#1c1400,color:#fcd34d
    style YAML fill:#1c1400,color:#fcd34d

    note1["📌 Render Free 制約\n・15分アイドルでスピンダウン\n（自動収集は GitHub Actions が担うため問題なし）\n・SSH 不可 → ログは Render ダッシュボードのみ\n・永続ディスクなし → 永続化は Supabase のみ"]
    style note1 fill:#0c1a3a,color:#93c5fd
```

---

## 10. ファイル役割一覧

| ファイル | 種別 | 役割 | 主な依存先 |
|---|---|---|---|
| `api.py` | バックエンド | REST API窓口・認証・SSE・手動収集トリガー（自動収集は GitHub Actions が担当） | database.py, collector.py, plugins/ |
| `database.py` | バックエンド | DBテーブル定義・upsert・成長率/Zスコア計算。6テーブル（Company / FinancialRecord / StockPriceHistory / MacroData / CollectionLog / XbrlRawDocument）。`pack_elements`/`unpack_elements`/`upsert_xbrl_raw` ヘルパを含む | PostgreSQL |
| `collector.py` | バックエンド | EDINET/stooq/JPX/マクロデータからデータ収集→DB保存。`MACRO_SERIES` で為替・金利・指数・コモディティ9系列を定義 | EDINET API, stooq, JPX |
| `checker.py` | バックエンド | データ品質チェック（NULL率・外れ値・収録状況） | database.py |
| `plugins/base.py` | バックエンド | 分析プラグインの抽象基底クラス | — |
| `plugins/__init__.py` | バックエンド | プラグインを自動スキャン・レジストリ管理 | plugins/*.py |
| `plugins/gap_analysis.py` | バックエンド | 乖離分析（割安・割高ランキング） | plugins/utils.py |
| `plugins/recommend.py` | バックエンド | 複合スコアによる銘柄推薦 | plugins/utils.py |
| `plugins/total_return.py` | バックエンド | 配当込みトータルリターン分析 | plugins/utils.py |
| `plugins/sector_ols.py` | バックエンド | 業種別OLS回帰分析（次元整合・winsorize+z-score前処理） | plugins/utils.py |
| `plugins/price_predictor.py` | バックエンド | 株価リターン予測（価格×財務特徴量OLS・月次WFV） | plugins/utils.py |
| `plugins/net_cash_analysis.py` | バックエンド | ネットキャッシュ分析（清原達郎『わが投資術』式）。NC = 流動資産 + 投資有価証券×0.7 − 総負債 | database.py |
| `plugins/utils.py` | バックエンド | ols()・normalize()・winsorize()・walk_forward_cv()・walk_forward_cv_monthly() | — |
| `tests/` | テスト | pytest 回帰テスト（188件）。プラグイン7個＋utils＋`database.py`（upsert・年度別Zスコア）＋`collector.py`（XBRLパース・派生指標＋ネットワーク取得を httpx MockTransport でモック）＋`api.py`（純関数・`/health`・DB-backed 読取エンドポイント）をカバー。in-memory SQLite fixture（StaticPool）／FastAPI TestClient／httpx MockTransport で検証。共通 fixture は `tests/conftest.py`（`db`/`make_fin` 等） | pytest, sqlalchemy, fastapi, httpx |
| `requirements-dev.txt` | 設定 | 開発・テスト専用依存（`pytest`）。本番 `requirements.txt` と分離（Render メモリ節約） | — |
| `dashboard.html` | フロントエンド | トップページ・全体サマリー（`/`） | api.py |
| `collection.html` | フロントエンド | 収集管理・スクリーニング・DBブラウザ（`/collection`） | api.py |
| `analysis.html` | フロントエンド | 回帰分析・乖離分析・プラグイン（`/analysis`）。乖離分析タブに横断分布（理論vs実績の散布図・乖離率ヒストグラム）を Chart.js で表示 | api.py, Chart.js (CDN) |
| `login.html` | フロントエンド | 認証ログイン画面（`/login`） | api.py |
| `models.html` | フロントエンド | モデル解説・参考文献ページ（`/models`）。8モデルの数式・パラメータ・DOIリンクをインラインHTMLで表示。 | — |
| `db.html` | フロントエンド | DBビューア（`/db`）。4テーブルのスキーマ・プレビュー・統計サマリー・ER 風リレーション・企業ドリルダウン・CSV エクスポート。 | api.py |
| `company.html` | フロントエンド | 企業詳細（`/company`・`/company/{edinet_code}`）。個別企業の業績・財務(BS)・CF・per-share/配当・バリュエーション（理論時価総額乖離）・日次株価・業種内Zスコアレーダー・清原式ネットキャッシュ・同業比較を Chart.js の時系列グラフで可視化。企業名・証券コード検索付き。 | api.py, Chart.js (CDN) |
| `_pipeline_gh.py` | GitHub Actions | 全件収集パイプライン（full-pipeline.yml から workflow_dispatch 手動起動） | collector.py, database.py |
| `_pipeline_incremental.py` | GitHub Actions | 差分収集パイプライン（daily-incremental.yml で毎日 JST 03:00 自動実行） | collector.py, database.py |
| `check.py` | ユーティリティ | EDINET API 疎通確認ワンショット | EDINET API |
| `.env` | 設定 | APIキー・DB接続・認証情報（UTF-8 BOMなし） | — |
| `ARCHITECTURE.md` | ドキュメント | 本ファイル。コード変更時は必ず更新する | — |
| `MODELS.md` | ドキュメント | 分析モデルの数式・パラメータ・参考文献（Markdown版）。モデル変更時は `models.html` とセットで更新する。 | — |
| `FUTURE_TASKS.md` | ドキュメント | 今後実装予定の機能仕様（時系列予測モデルなど） | — |
| `VISUALIZATION_IMPROVEMENTS.md` | ドキュメント | 企業データ可視化強化の改善案（バフェット・コード型・Chart.js・企業詳細ページ） | — |
| `VISION.md` | ドキュメント | プロジェクトの目的・方針 | — |
| `CLAUDE.md` | 設定 | Claude Codeへの動作指示 | — |
