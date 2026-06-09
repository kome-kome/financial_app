# 日本株財務分析ツール — ドメイン用語

EDINET / J-Quants から収集した日本上場企業の財務データを、画面表示と回帰分析の双方に供するツール。本ファイルは用語集（グロッサリー）であり、実装詳細・仕様は含めない。

## 財務データ項目

**再分類項目 (reclassified item)**:
XBRL の生タグを JGAAP / IFRS / US-GAAP 横断で共通スキーマ（`bs_` / `pl_` / `cf_` プレフィックス）へ正規化した財務値。`FinancialRecord` のカラム実体。複数の生タグが1つの再分類項目へ集約される**多対一**（例: NetSales / Revenues / OperatingRevenues → revenue）。
_Avoid_: 財務カラム, XBRL項目（生タグと混同するため）

**源泉タグ付き項目 / 派生列 (source-tagged item / derived column)**:
`FinancialRecord` の列は2種に分かれる。1つ以上の生タグから採取する**源泉タグ付き再分類項目**と、生タグを持たず計算・市場データが埋める**派生列**（例: cf_free_cf, pl_ebitda, stock_price）。源泉タグの有無がその列を XBRL parse の対象とするか否かを決める。

**表示項目 (display field)**:
company 画面の PL/BS/CF チャートで利用者に見せる再分類項目。総額（売上高・総資産）に帳尻を合わせる**残差表示**が前提で、標準間で値が信頼できる少数項目のみを素で使う。
_Avoid_: GUI項目, 表示カラム

**分析特徴量 (analysis feature)**:
回帰分析モデルの説明変数として使う項目。要件は表示とは別で、per-share の次元整合性・winsorize・パネルでの欠損率（標準間カバレッジ）が効く。GUI 表示の有無とは独立に価値を持つ。
_Avoid_: （口語の「説明変数」は可。正式にはこちら）

**回帰結果 (regression result)**:
業種別OLS（producer）が `regression_results` テーブルへ書き込む銘柄×年度の出力（predicted_market_cap / gap_ratio / model[ols|ridge] / sector / computed_at）。乖離分析（consumer・`depends_on=["sector_ols"]`）が消費する seam の通貨。producer 未実行なら乖離分析は前提条件エラー（`plugins.ensure_dependencies` が `depends_on` を runner/専用エンドポイントで強制）。回帰が財務データ更新より古い＝stale。
_Avoid_: 予測結果, OLS結果（モデル混在を曖昧にするため）

## データソースと収集

**全件収集 (full collection)**:
EDINET 全上場企業（約3,800社）× 指定年数の有価証券報告書を取得して DB を作り直す処理。

**差分収集 (incremental collection)**:
DB 未保存の `doc_id` のみを取りに行く処理。現状の差分判定は **doc_id 単位のスキップ**であり、訂正報告書・過年度修正は対象外。
_Avoid_: 更新収集, アップデート

**収集ジョブ (collection job)**:
収集処理の実行時インスタンスの状態（running / progress / log / cancel）。種別は collection（full/incremental/smart 共有）/ market / history / jquants / macro / reparse。job 名キーの単一 registry（`collection_jobs.jobs`）が状態を保持し SSE で進捗配信する。全件収集・差分収集が「処理の種類」を指すのに対し、収集ジョブは「実行中スロットの状態」を指す。
_Avoid_: ステータス辞書, status dict（実装詳細・旧称）

## 分析プラグイン

**パラメータ契約 (param contract)**:
分析プラグインの `params_schema()` を UI フォーム定義かつ型契約として使う宣言。各フィールドは `type`（ウィジェット: select/multiselect/slider/number/checkbox/text/weights）と `dtype`（データ型: int/float/str/list[str]/bool/dict）の2軸を持ち、dtype は数値（number/slider）にのみ明示し他は type から推論する。単一の coerce seam（`coerce_params`）がこの契約から raw params の型付け・default 補完・bounds/membership 検証を行い、execute には意味的 validation（features 非空・weights 合計≠0 等）だけが残る。bounds/membership 違反は reject（ValueError）。
_Avoid_: パラメータスキーマ, フォーム定義（型契約の側面が落ちるため）
