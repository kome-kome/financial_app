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
業種別OLS（producer）が `regression_results` テーブルへ書き込む銘柄×年度の出力（predicted_market_cap / gap_ratio / model[ols|ridge] / sector / computed_at）。バリュエーション分析（consumer・`depends_on=["sector_ols"]`）が消費する seam の通貨。producer 未実行なら乖離分析は前提条件エラー（`plugins.ensure_dependencies` が `depends_on` を runner/専用エンドポイントで強制）。回帰が財務データ更新より古い＝stale。
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

## 分析の階層

分析手法は3つの層に整理する。層が違えば「種類の違う導出」であり、フラットな行列（base × operator）では扱わない。

**一次分析 (primary analysis)**:
個別銘柄を入力にとり、銘柄ごとの**シグナル**（スコア／割安度／リターン予測）を出力する分析。目的で下位分類する — スクリーニング（探す）／バリュエーション（割安度）／リターン予測。利用者はこの出力に直接アクションする。
_Avoid_: 基本分析, ベース分析（層を曖昧にするため）

**双対分析 (dual analysis)**:
一次分析を**逆観点**で**保有銘柄**へ向け直し、売り判断を生む分析（売り候補ランキングが唯一の実体）。一次分析と数式は連続（符号反転）だが、**対象ユニバース（全市場→保有）と意思決定（買い→売り）が変わる**ため、任意の一次分析へ自由に適用できる operator ではない。スクリーニング系にのみ自然に成立する。
_Avoid_: 逆分析, 反転スコア（双対の「対象が変わる」性質が落ちるため）

**メタ検証 (meta-validation)**:
一次分析そのものの**実績有効性**を評価する分析（バックテスト・各モデル内蔵の WF-CV）。出力は銘柄選択ではなく**品質指標**。recommend は内蔵検証を持たずバックテストへ外付け、price_predictor / macro は WF-CV を内蔵 ＝ メタ検証は「既に一部解決済みの横断的関心事」であり、各一次分析へ一律に増設する variant ではない。
_Avoid_: 検証分析, 評価（メタ＝分析の分析である性質が落ちるため）

## 分析プラグイン

**バリュエーション分析 (valuation analysis)**:
業種内OLS（sector_ols）の `gap_ratio` seam を起点に、**割安度（gap）・平均回帰タイミング（AR(1)半減期）・期待総リターン（gap＋配当利回り）** を一括で出す一次分析（バリュエーション系の唯一のハブ）。旧「乖離分析（gap_analysis）」を改名・拡張したもので、旧 total_return プラグインの「理論株価乖離＋配当利回りで総リターンランキング」機能を吸収した（独自OLSは廃止し sector_ols を消費）。implied P/E・P/B は予測株価÷EPS・BPS で再現する。OLSエンジンは sector_ols 1本に統一。
_Avoid_: 乖離分析（gap だけを指す旧称・責務が狭い）, 総合リターン予測（吸収された旧プラグイン名）

**パラメータ契約 (param contract)**:
分析プラグインの `params_schema()` を UI フォーム定義かつ型契約として使う宣言。各フィールドは `type`（ウィジェット: select/multiselect/slider/number/checkbox/text/weights）と `dtype`（データ型: int/float/str/list[str]/bool/dict）の2軸を持ち、dtype は数値（number/slider）にのみ明示し他は type から推論する。単一の coerce seam（`coerce_params`）がこの契約から raw params の型付け・default 補完・bounds/membership 検証を行い、execute には意味的 validation（features 非空・weights 合計≠0 等）だけが残る。bounds/membership 違反は reject（ValueError）。スライダー（type=slider）は粒度 `step` を必ず宣言し、int dtype の step は整数とする（未宣言だと HTML range が連続値になり int で端数を吐いて reject されるため。JS 側も dtype から安全側の step を導出する二重防御）。
_Avoid_: パラメータスキーマ, フォーム定義（型契約の側面が落ちるため）

**売りスコア (sell score)**:
売り候補ランキング（`plugins/sell_ranking.py`）が保有銘柄に付ける「手放すべき度合い」。買い系スコアの逆観点（割高度＝`gap_ratio` 反転・業績悪化＝ROE/利益率/CF/成長の低さ）を最新年度ユニバースで winsorize→z 標準化し、非負ウェイトで `Σ w·(−z)/Σ w` として合成する（平均並み≈0、劣る銘柄ほど正に大きい）。価格モメンタムはスコアに混ぜず別軸（trend）として扱う。
_Avoid_: 売り推奨度（ラベルと混同するため）

**アクションラベル (action label)**:
売りスコアに絶対閾値を当てて付ける `SELL`／`REDUCE`／`HOLD`（値不足は「データ不足」）。相対ランキングと併用し、優良な保有のみなら全 `HOLD` になる。
_Avoid_: シグナル, 判定（曖昧）

**タイミング補正 (timing adjustment)**:
価格トレンド（週次株価の 13週リターン等から `下落`/`上昇`/`横ばい`/`不明` に分類）でアクションラベルを 1 段ずらす規則。`下落`＝売り圧力↑（HOLD→REDUCE→SELL）、`上昇`＝SELL→REDUCE へ緩和（上昇中の即売り回避）。
_Avoid_: モメンタムフィルタ（スコアを除外する意味に取れるため）
