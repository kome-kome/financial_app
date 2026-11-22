# MODULE_REFERENCE.md — モジュール・画面・workflow 実装リファレンス

> **位置づけ**: [ARCHITECTURE.md](ARCHITECTURE.md) §10「ファイル役割一覧」のうち、`plugins/`・`scripts/` 以外の行（リポジトリ直下のモジュール・画面・`tests/`・`.github/workflows/`）で役割の欄が 300 字を超えていたものの詳細を分離した実装リファレンス（#1293。セルが 2,500 字まで育ち、一覧として読めなくなっていた。`plugins/` を [PLUGIN_REFERENCE.md](PLUGIN_REFERENCE.md) へ、`scripts/` を [SCRIPTS_REFERENCE.md](SCRIPTS_REFERENCE.md) へ分けたのと同じ理由）。
>
> | 知りたいこと | 参照先 |
> |---|---|
> | ファイル一覧としての索引・処理フロー・ER 図 | [ARCHITECTURE.md](ARCHITECTURE.md) §10 |
> | 各ファイルの役割の詳細・守ること・実測値 | **本書** |
> | 分析プラグインの内部契約 | [PLUGIN_REFERENCE.md](PLUGIN_REFERENCE.md) |
> | スクリプトの詳細 | [SCRIPTS_REFERENCE.md](SCRIPTS_REFERENCE.md) |
> | 再現条件と回避手順 | [GOTCHAS.md](GOTCHAS.md) |
> | 設計判断の経緯（ADR） | [adr/README.md](adr/README.md) |

**ファイルを足したら §10 へ1行（役割は1〜2文）を足し、詳しく書くことがあればここへ節を足して §10 の行からリンクする。** §10 の行が 300 字以内に収まっていたファイルは節を持たない。本文は移設時（2026-11-22）の §10 のセルをそのまま移したもので、コードの定数の書き写しや経緯が残っている。

---

## 索引

| ファイル | 種別 |
|---|---|
| [`routers/*.py`](#routerspy) | バックエンド |
| [`collection_jobs.py`](#collection_jobspy) | バックエンド |
| [`backtest.py`](#backtestpy) | バックエンド |
| [`model_comparison.py`](#model_comparisonpy) | バックエンド |
| [`model_stats.py`](#model_statspy) | バックエンド |
| [`database.py`](#databasepy) | バックエンド |
| [`db_egress.py`](#db_egresspy) | バックエンド |
| [`weekly_price_cache.py`](#weekly_price_cachepy) | バックエンド |
| [`corporate_actions.py`](#corporate_actionspy) | バックエンド |
| [`index_event_study.py`](#index_event_studypy) | バックエンド |
| [`index_event_notes.py`](#index_event_notespy) | バックエンド |
| [`portfolio_ledger.py`](#portfolio_ledgerpy) | バックエンド |
| [`index_events.py`](#index_eventspy) | バックエンド |
| [`ttm_composite.py`](#ttm_compositepy) | バックエンド |
| [`sysmem.py`](#sysmempy) | バックエンド |
| [`jax_import_guard.py`](#jax_import_guardpy) | バックエンド |
| [`collector_utils.py`](#collector_utilspy) | バックエンド |
| [`collector_master.py`](#collector_masterpy) | バックエンド |
| [`collector_prices.py`](#collector_pricespy) | バックエンド |
| [`tuning_cycle.py`](#tuning_cyclepy) | ユーティリティ |
| [`sector_gap_asof.py`](#sector_gap_asofpy) | バックエンド |
| [`collector_disclosures.py`](#collector_disclosurespy) | バックエンド |
| [`collector_interim.py`](#collector_interimpy) | バックエンド |
| [`tests/`](#tests) | テスト |
| [`dashboard.html`](#dashboardhtml) | フロントエンド |
| [`morning.html` / `static/js/morning.js`](#morninghtml--staticjsmorningjs) | フロントエンド |
| [`collection.html`](#collectionhtml) | フロントエンド |
| [`analysis.html`](#analysishtml) | フロントエンド |
| [`models.html`](#modelshtml) | フロントエンド |
| [`guide.html`](#guidehtml) | フロントエンド |
| [`company.html`](#companyhtml) | フロントエンド |
| [`static/js/*.js`](#staticjsjs) | フロントエンド |
| [`_pipeline_gh.py`](#_pipeline_ghpy) | GitHub Actions |
| [`_pipeline_vacuum.py`](#_pipeline_vacuumpy) | ローカル月次バッチ |
| [`launch.py`](#launchpy) | ユーティリティ |
| [`batch_activity.py`](#batch_activitypy) | ユーティリティ |
| [`batch_freshness.py`](#batch_freshnesspy) | ユーティリティ |
| [`macro_beta_inference.py`](#macro_beta_inferencepy) | ローカル月次バッチ |
| [`hyperparameter_search.py`](#hyperparameter_searchpy) | ローカル月次バッチ / ユーティリティ |
| [`nightly_scores.py`](#nightly_scorespy) | GitHub Actions / ユーティリティ |
| [`prediction_snapshots.py`](#prediction_snapshotspy) | ユーティリティ |
| [`macro_health.py`](#macro_healthpy) | 収集 / GitHub Actions |
| [`recommend_factor_premia.py`](#recommend_factor_premiapy) | ローカル月次バッチ / ユーティリティ |
| [`.github/workflows/ci.yml`](#githubworkflowsciyml) | GitHub Actions |
| [`.github/workflows/daily-incremental.yml`](#githubworkflowsdaily-incrementalyml) | GitHub Actions（**手動のみ**） |
| [`.github/workflows/nightly-scores.yml`](#githubworkflowsnightly-scoresyml) | GitHub Actions |
| [`.github/workflows/egress-health.yml`](#githubworkflowsegress-healthyml) | GitHub Actions |
| [`.github/workflows/dependency-audit.yml`](#githubworkflowsdependency-audityml) | GitHub Actions |
| [`.github/workflows/bench-macro-beta.yml`](#githubworkflowsbench-macro-betayml) | GitHub Actions |
| [`.github/workflows/notify-failure.yml`](#githubworkflowsnotify-failureyml) | GitHub Actions |


## `routers/*.py`

`api.py` が `include_router` で束ねる REST ルーター6本（いずれも `APIRouter()`・フルパス保持）。`auth`（認証・Cookie/CSRF 発行）/ `collect`（収集管理・進捗SSE・売買の記録の書く口）/ `portfolio`（売買の記録と運用成績の読む口・#1222）/ `market`（統計・企業一覧・株価/履歴・マクロ・CSV・スクリーニング・DBビューア）/ `analysis`（プラグイン実行・推薦・乖離・バックテスト）/ `morning`（朝の推奨＝永続化済みスコア＋鮮度ブロックの読み出し専用・#423 子3）。REST ルート定義の実体はここにある

種別: バックエンド ／ 依存先: database.py, collection_jobs.py, plugins/, data_quality.py

## `collection_jobs.py`

収集ジョブの実行時状態を集約する registry。job 名キーの `JobState`（running/progress/log/cancel）＋ start/cancel/snapshot/stream を提供。旧6本の並列 status dict を1箇所に畳む。SSE 配信ジェネレータ（`_sse_stream`）を内包。収集ジョブ専用ではなく job 名キーが衝突しなければ任意のバックグラウンドジョブに使える汎用 registry。heavy プラグインの進捗（#545）は `plugin:{name}` スロットへ相乗りし、`stream_awaiting_start` で配信する（**完了まで返らない POST と併走する**ため、開始前に開かれたストリームを猶予つきで待ち合わせる。素の `_sse_stream` は running=False で即切れる）

種別: バックエンド ／ 依存先: fastapi

## `backtest.py`

バックテスト分析（スコアリング上位N社の実績リターン）。`run(db, …, source, cost_bps=0.0)->dict` / `score_record` / `percentile` / `SCORING_SOURCES`(`recommend`/`valuation`/`net_cash`。`sell` は ADR-0071 で外した) / `MULTI_PERIODS`。**株価を使う列（`market_cap`・`div_yield`・`gap_ratio`）は開始日の株価で作り直す**（`asof_row` と `sector_gap_asof.gaps_on`・#1248。VIEW の値は今の株価なので、そのままだと開始日の後の値下がりで順位が付く。応答の `asof_valuation`・`asof_gap`）。**断面標準化は `run` が `fit_view_metric_stats` で作り `score_record` へ渡す**（1レコードでは断面統計を持てないため・#509。渡さないと as-of 再現が是正前のスコアを測ることになる）。**recommend の合成は本番の買い推奨と同じ `plugins.recommend.weighted_score`**（値がある指標の |w| 和で割る・被覆率が画面の既定 0.5 未満の社は採点しない・#745。以前は Σw·ẑ のままで欠けの多い銘柄を中央へ寄せていた）。`source` で検証対象の一次分析を切替（メタ層の一般化）。`cost_bps`（Issue #316・片道bp）は往復2倍控除したネット値を `return_pct_net`/`avg_return_net_pct` 等 `*_net` キーへ併記し、無印キー（コスト控除前）は常に不変＝後方互換固定。FastAPI 非依存で直接テスト可能。スコア指標は `FinancialMetric`（VIEW 派生）を引く

種別: バックエンド ／ 依存先: database.py, plugins.recommend, plugins.net_cash_analysis

## `model_comparison.py`

M-1〜M-6 の `oof_backtest` を横並び集約（OOF-first・`POST /api/backtest/model-comparison`）。**モデル単位で `progress.emit` する**（#593）——共通骨格（macro_snapshots）の進捗はそのまま流れるが、それだけでは「3本のどれを回しているか」が出ない。終端（`current == total`）も必ず流す＝出さないと画面は「2/3 のまま完了」に見える。`tuning_objective_only`/`tuning_dry_run` で重い全社スコアを省く。**`display_scoring` で画面の並びの採点（`oof_backtest_display`＝U = μ̂ − λR の順の OOF・参考）を出させる**（#1042・ADR-0069。探索は出さない）。**全モデルペアの rank-IC 差有意性 `significance_matrix` を併算**（#369）。**全ペアで Bonferroni を掛ける**（`correction="bonferroni"`・#741・ADR-0063。以前は alpha が判定に届かず15組を補正なしで判定していた）。**`include_production=True` で M-2 の本番の設定の行を既定の行の直後に足す**（#1115・ADR-0070 決定 6。`plugin_tuned_params` を `plugins.tuning.project_tuned_params` で射影・`variant="production"`・`selection_optimism`。▲▼ と Bonferroni の組数には入れない・本番の設定が無ければ理由だけの行）。画面の API と `measure_top_k_veto` が有効にし、既定は無効

種別: バックエンド ／ 依存先: plugins/, model_stats.py

## `model_stats.py`

モデル比較の統計後処理（#369）。定常ブートストラップ（Politis-Romano 1994）で per-fold rank-IC 差の有意性を検定（`significance_matrix`）、分位単調性を要約（`monotonicity_summary`）。**有意の規則は `_is_significant` の1つ**（p < α かつ同じ α の CI が 0 を跨がない）、**多重比較の補正は `paired_family_significance` が実際に検定した組数から出す**（比較画面・`candidate_bakeoff`・`ensemble_base_bakeoff` が共有・#741・ADR-0063）。`paired_ic_significance` を並べて既定 alpha のまま `significant` を読むと補正なしになる。stdlib のみ・追加学習/Egress ゼロ・seed 固定で決定的。`oof_backtest`（分位単調性）と `model_comparison`（有意差）が共用。**前向き監視の群逐次境界**（Lan-DeMets の O'Brien-Fleming 型 α 消費・`obf_spending` / `ld_obf_boundaries`・#1007）も持ち、事前登録文書の境界表はこの出力（`tests/test_prereg.py` が照合）

種別: バックエンド ／ 依存先: —

## `database.py`

DBテーブル定義・upsert。主要テーブル（Company / FinancialRecord / StockPriceDaily / StockPriceWeekly / MacroData / CollectionLog / XbrlRawDocument / **RegressionResult** ほか。全表は §3 の ER 図）＋ **`financial_metrics` VIEW**（通期・派生指標を都度SQL算出・読み取り専用 ORM `FinancialMetric`）＋ **`financial_metrics_interim` VIEW**（非通期=半期H1等・ORM `FinancialMetricInterim`・#219②フェーズC）。両 VIEW は `_ensure_view`→`_ensure_one_view` が**呼ばれたら必ず**再作成する（再作成の要否は `init_db()` のスキーマ指紋ゲートが決める・#597 / ADR-0048。かつてここに `pg_get_viewdef()` との比較があったが一致することがなく、「定義差分時のみ」は実際には成立していなかった）。`upsert_financial` は **ソース列のみ**保存（derived 取り込み廃止）。`upsert_regression_result`（merge・方言非依存・単数）と `upsert_regression_results_batch`（ON CONFLICT の一括 upsert・`sector_ols` の永続化経路・#506）。派生指標は VIEW へ移行し旧 `calc_growth_rates`/`calc_zscore_normalization` は削除、旧計算列は `init_db` の冪等 `DROP COLUMN` で除去。`pack_elements`/`unpack_elements`/`upsert_xbrl_raw` ヘルパを含む

種別: バックエンド ／ 依存先: PostgreSQL

### 上場状態と今買える母集団

（[ARCHITECTURE.md](ARCHITECTURE.md) §3 の前置きから移した・#1300）

- **上場状態（`is_active`/`delisted_date`、Issue #315）**: `companies` を LEFT JOIN して VIEW へ合成。
  `collect_stock_price_history_jquants`（夜間の J-Quants catchup・`_pipeline_incremental.py`）が J-Quants `/equities/master`
  （現在の上場銘柄集合・実測4,446銘柄）と突合し `sync_active_status()`（database.py）で自動更新する。
  v1 の `/markets/listed/info` は **v2 に存在せず**、#425 の v2 移行以降この同期は停止していた（#462）。
  マスタは東証銘柄だけを載せるので、**札証・福証の単独上場（`yahoo_suffix` 解決済み・#555）は株価で判定する**（#779）:
  最終足が `PRICE_STALE_ALERT_BDAYS` 以内で、マスタの as-of より後に出来高>0 の足がある社は delisted にせず、
  delisted なら復帰させる（戻り値 `price_alive`）。東証の社はマスタ突合と #463 の `protected` だけで判定する。
  「今買える銘柄」を選ぶプラグイン（recommend / gap_analysis / net_cash_analysis）と
  sell_ranking の**標準化の基準となる母集団**は `database.tradable_filters(db)` で対象を絞るが、
  **backtest は今の上場状態ではフィルタしない**（as-of の start_date 時点の候補を「現在」の上場状態で絞ると
  生存者バイアスを逆向きに持ち込むため）。代わりに `backtest.tradable_at_start` が、開始日の時点で分かっていた
  事実だけで「開始日に買えた社」へ絞る（開始日の `ASOF_PRICE_WINDOW_DAYS` 日前〜開始日に週次の終値がある社・#1242。
  外した数は応答の `n_untradable_at_start`）。**同じ窓の最後の終値で、株価を使う列を開始日の値へ作り直す**（#1248）:
  as-of の行の `market_cap`・`div_yield` は毎晩の収集が今の株価で上書きし、`gap_ratio` は毎晩の回帰が今の株価で
  作るので、そのまま順位を付けると「開始日から今までに値下がりした社ほど割安に見える」（6 か月前の開始日で、保存
  された株価が開始日の終値から 3% 超ずれる行が 82%）。時価総額・配当利回りは毎晩の収集と同じ
  `_compute_market_values` へ開始日の終値を渡して VIEW と同じ分割補正（`COLUMN_DIRECTION`）を当て、gap_ratio は
  開始日 1 日分の業種別回帰（`sector_gap_asof.gaps_on`）で作り直す。時価総額の下限もこの値で掛ける。
  sell_ranking のユーザー入力の保有銘柄自体は
  除外せず情報表示のみ（`is_active` を行の属性として返す）。
- **価格が止まった社の除外（Issue #605）**: `is_active` だけでは足りない。`/equities/master` の
  as-of は「今日−84日」（J-Quants のエンバーゴ）で、**廃止の反映が最大12週遅れる**。
  実測（2026-09-04）では値の付かない29社が `is_active=True` のまま残り、`regression_results`
  29社 / `macro_beta_loadings` 28社 / `macro_enet_scores` 6社へ入って μ̂ 付きで推奨候補に
  並んでいた（#315 の生存者バイアスの裏返し＝ゾンビ銘柄）。`delisted_date` の実測は
  「as-of が停止日を追い越した日」と一致し、毎日1〜5社ずつ落ちる待ち行列になっている。
  マスタは自分の as-of より後のことを証言できない（#463 が逆向きで書いたのと同じ制約）が、
  **価格が止まったという事実は今日わかる**ので、判定を「マスタ収載」から「価格が生きている」へ
  寄せた。閾値は `PRICE_STALE_ALERT_BDAYS`（=10営業日）を再利用する——`price_freshness` が
  「この結果で発注しない」と言う線と別に持つと片方だけ動いたときに黙ってずれる。WARN(=5)は
  連休や一時取得失敗で生きた銘柄を落とす危険が最も高い（#555 と同型の静かな欠測）。
  価格行を1本も持たない銘柄は落とさない（判定できるのは「止まった」であって「無い」ではない）。
  4経路が同じ `tradable_filters` を共有することは `tests/test_tradable_universe.py` が AST で
  縛る——1箇所だけ直すと「推奨には出ないが売却候補には出る」形で静かに食い違う。
- **計算の母集団も同じ判定を共有する（Issue #780）**: #605 が直したのは表示母集団だけで、計算には
  廃止・停止の社が残っていた。次の3つも `tradable_filters` を共有する（同じく AST で縛る）。
  - **sector_ols の当日回帰**（既定・`year` 指定）: `financial_records`＋`companies` の JOIN を読むので、
    `tradable_filters(db, is_active=Company.is_active, edinet_code=FinancialRecord.edinet_code)` と列を
    渡して掛ける。実測（2026-10-02）で廃止25社・停止17社が最終株価のまま回帰に入り、整理銘柄の暴落価格
    （`stock_price=1.0` → gap +47,963%）が業種の係数と α を通じて現役社の gap を動かしていた。外すと
    現役社の gap は全体で順位相関 0.991・中央値 0.79pt 動き、ridge の α が切り替わった4業種
    （輸送用機器 100→1000 ほか）で最大 1,092pt 動いた（α が平坦な谷で跳ぶ問題は #761）。
    **時点再現（`all_years=True`・`sector_gap_asof`）には掛けない**——「その月末に週次の足を持つ社」で
    もともと廃止社を含まず、今日の上場状態で絞ると生存者バイアスになる。今回の変更で本番の当日回帰が
    学習パネルの母集団に揃った（だから `PREPROCESS_VERSION` は上げていない）。外した社の
    `regression_results` の既存行は消さない（表示は `tradable_filters` が隠す）。これに対し、**読んだのに
    回帰に入らなかった上場中の社**（採用列の空欄・業種が `min_samples` 割れ等）の行は消す（#905・ADR-0066）
    ——隠す仕組みが無いので、前回までの乖離率が乖離分析と推奨に出続けるため。
  - **sell_ranking の `mu` / `neg_r_macro` の標準化基準**: producer の全行ではなく universe にいる社の μ̂。
    M-6 では 1,719行のうち77行が外れ、標準偏差が 0.0642 → 0.0559 になった。
  - **producer の代表 as-of**（M-2・M-3・M-6・M-4）: μ̂ の行は全社ぶん保存したまま（読み手が各自の
    母集団で絞る）、as-of だけを `macro_snapshots.tradable_snapshot_asof` で今買える社から作る。社ごとの
    スナップ日は保存しないので絞れるのは保存時だけ。除く社は `database.non_tradable_codes`
    （`tradable_filters` の否定）。全社が落ちたら全社で代表させる（「未蓄積」ではなく古さを見せる）。
  - `macro_beta_loadings`（M-1 の学習データ）には掛けない（階層ベイズのプーリングは過去の社を要する・#315）。
- **分析タブの結果表も同じ判定を通す（Issue #806）**: #605・#780 は推奨系と計算の母集団を揃えたが、
  **M 系の `execute` が返す `results` そのもの**が読み手であることは扱われていなかった。M 系の「現在」の
  μ̂ は社ごとに最終週次バーで作るので、株価が止まった社は止まった日付のまま並ぶ。線形の M-6 では
  その日付のマクロ値が全社共通の定数として μ̂ を押し上げ、実測（2026-10-04）で上位10が全部・上位30の
  23社が廃止社だった（最終バーの月ごとの M-6 μ̂ 平均は 2025-06 の +0.187 から 2026-10 の −0.225 へ
  ほぼ単調に下がる。木の M-2 は外挿しないので段差が出ない）。OOF rank-IC が検証したのは同じ月の中の
  順位なので、日付の違う社を混ぜた表は検証の外にある。M-1/M-2/M-3/M-4/M-6 は**永続化の後に**
  `macro_snapshots.tradable_results` で表だけを絞り（保存する μ̂ は全社のまま）、内訳
  `untradable_excluded`（上場廃止 / 価格停止。`database.non_tradable_breakdown` が `non_tradable_codes`
  に理由の札を付ける）を画面へ1行出す（`analysis.js::_showUntradableNote`。結果本体は λ 変更で
  描き直されるので注記は本体の外に置く）。M-3 は絞ってから `top_n` を取り、α/β 経路も表示する社に付ける。
  呼び出しは `tests/test_tradable_universe.py::TestResultsUseTradableResults` が AST で縛る。

## `db_egress.py`

**Egress 台帳とサーキットブレーカ**（#478・[ADR-0034](adr/0034-client-side-egress-ledger-and-circuit-breaker.md)）。`database.py` が import 時に `install(engine)` で `after_cursor_execute` リスナを1本張り、全プロセス（GHA バッチ・ローカル CLI・Render）で「どのテーブルから何行・何列を引いたか」を記録する。結果は消費しないので既存挙動に干渉しない。プロセス終了時に `[egress] summary` を標準エラーへ1行、**既定で `.egress/ledger.jsonl`** へ1行 append（帰属は `FINAPP_JOB`・無効化は `FINAPP_EGRESS_LEDGER=0`）。**歯止めは2軸**（#478・[ADR-0037](adr/0037-egress-cycle-budget-is-a-second-axis.md)）＝①行数/MB のプロセス予算を超えると `EgressBudgetExceeded`（局所解除は `egress_budget()` CM・全体解除は `FINAPP_EGRESS_ENFORCE=0`）、②**請求サイクル累計**を `app_settings.egress_cycle_bytes` に持ち warn 80% / block 95%（プロセス予算 400MB では「1日12プロセスで 4.8GB」が素通りする＝2026-08 の超過はこの形だった）。累計を DB に置くのは**ローカル CLI と GHA が同じカウンタを見られる唯一の場所**だから。ミラー接続（`_is_local`）と **pytest 実行中は積まない**（後者を怠ると全テストが本番へ接続し atexit が本番へ書き込む）。**`database` を import しない**（逆向き依存を作らない）。なお**列スコープの静的検査には使えない**（`_Bucket` は n_cols を保持せず SQLite は rowcount=-1）ため、そちらは `tests/test_column_scoping.py` の AST 検査が担う

種別: バックエンド ／ 依存先: sqlalchemy.event

## `weekly_price_cache.py`

**週次株価の run 間差分ロードキャッシュ**（#480・[ADR-0036](adr/0036-weekly-prices-incremental-load.md)）。夜間バッチが毎晩引き直していた 1,282,436 行（39.3MB／月 1.98GB＝枠の40%）を、指紋（`max(week_start)`＋`count(*)` のサーバ側集約）＋直近27週の再取得＋DB 側の世代印（`app_settings.weekly_prices_generation`）で差分化する。**キャッシュは速さだけを担い、正しさは指紋・世代印・行数照合が持つ**（無い／壊れている／古い／保存失敗はすべてフルロードへ倒れる）。歯止めは4層＝行数照合のハードゲート・鮮度アサートの raise・週1回の強制コールド・コールド時のドリフト監査。**行の型を知らない**（`_VOLUME_NOT_LOADED` 番兵は pickle で同一性が壊れるため、ワイヤは素タプルで番兵の再付与は呼び出し側の責務）。`scripts/_cache.py`（検証専用・TTL 無し）とは別物

種別: バックエンド ／ 依存先: database（遅延 import のみ）

## `corporate_actions.py`

**企業イベント台帳**（#746・[ADR-0062](adr/0062-corporate-action-knowledge-lives-in-one-ledger.md)）。株数や株価の基準を変える企業イベントの知識の唯一の源——登録表（`SPINOFF_ADJUSTMENTS`・`WITHHELD_OFFICIAL_ADJUSTMENTS`・`adj_factor_event`）、分割補正係数 F の検出器（第1経路・第2経路・純資産比チェック・上場廃止をまたぐペアの除外・向き `COLUMN_DIRECTION`）、TTM の分割窓（`SplitWindow`・`factor_after`）。`build_ledger(db)` が入力（通期行・公式 `AdjFactor` とその受信区間・週次株価の系列）を**自分で読み**、パイプラインは一晩に1回だけ作って `rebuild_split_adjustment_factors`（`split_adjustment_factors` を毎晩全置換・#655・ADR-0055）と TTM 合成へ同じものを渡す＝係数表の F と TTM の窓が同じ検出・同じ公式イベントから決まる。**保留の窓（`WITHHELD_OFFICIAL_ADJUSTMENTS`・#652）に入る公式イベントは `compute_ledger` が外してから両方へ渡し**、外した件数を毎晩ログへ出す（#739）。**株数の動かない登録済みスピンオフ（`SPINOFF_ADJUSTMENTS`）は同じ1か所で F の寄与イベント（`kinds` の `spinoff`）と TTM の権利落ち日1点の窓に足し**、F に入れた件数と中身を毎晩ログへ出す（#740・ADR-0055 決定4-9）。第2経路の最新年の倍率は `jquants_adj_factor_events` の公式値から取り J-Quants は叩かない（#661）。**検出0件なら既存の表に触らず `RuntimeError`**（消してから失敗すると補正が静かに全部外れる。登録表の行は数えない＝`Ledger.detected_events`）。I/O より上は database / collector_prices / httpx / scripts を import しない（テストで固定）。以前は本番（`collector_prices`・`ttm_composite`）が測定用の `scripts/measure_split_valuation_bias.py` を import しており、登録表を読む読み手と読まない読み手ができていた（#739・#740）

種別: バックエンド ／ 依存先: database.py

## `index_event_study.py`

**指数イベントのイベントスタディの部品**（#1031・親 #1008）。事前登録 `docs/prereg/index-event-2026.md` の測り方を純関数にしたもの: 暦（D_m の前営業日 P_m と圧力週 w_m・祝日は 1・4・7・10 月の 22 日以降だけ手で持ち、範囲外は `CalendarError`）・33→17 業種・対照 C1/C2・Mahalanobis の k 最近傍（17 業種の完全一致・caliper）・SMD・first stage・AR̄・平均を引かない Newey-West の c'Ωc・σ / SE(δ̂) / MDE・placebo の誤検出率・R0。`stage_a` は**入口で公表週以降の終値を落とす**（盲検をコードで守る）。事前登録が決めていなかった点は `DEFINITIONS` に書いて出力にも残す。M 族（§6.2・#1009）の E_t・P_t・対の域・期ごとの偏り・回帰・層別の擬似処置群・順列 p も持つ。stage B（#1032）の窓の CAR・I・R・κ・δ̂・CAR_post・ρ、E族（E1・E2・E4）の Newey-West の片側 p・残差の定常ブートストラップ（`model_stats._stationary_bootstrap_sample`）・Holm・経験 p・Lee bounds・ラベル水準の DiD も持つ（`ci` は I・δ̂・κ・ρ）。前向き監視（§6.3・#1061）の `monitor` は、stage A と同じ 4 仕様のマッチング `match_specs` を 2026 のマッチ日で使い、到来した回 k の Z_k を `model_stats.ld_obf_boundaries(8, 0.05)` の境界と比べる（t_k = k/8 固定・k 回目の Ω は [A* − 26 週, w_k + 2 週]・決めた点は `MONITOR_DEFINITIONS`）

種別: バックエンド ／ 依存先: numpy, pandas（database・scripts は import しない・テストで固定）

## `index_event_notes.py`

**指数イベントの画面注記**（#1013）。推奨・売り（`results[].index_event` と全体の `index_events`）と `/api/financials/{edinet_code}`（`index_event` に日程付き）へ、TOPIX 移行措置・新規採用の事実の印（「TOPIX移行」「TOPIX新規」）・注記・表の上の 1 行（表示した社のうちの件数と母集団比率）を付ける。出すのは移行措置・新規採用で公表済み・適用中・見直し待ちの社だけで、最後の係数変更 + 52 週で計算で外す。**文言はここで作り、画面（JS）は表示だけ**（禁止語 `FORBIDDEN_WORDS` の検査を 1 か所でかける）。台帳はプロセス内で 10 分持つ。本番（Render）は一覧を持たない（ADR-0068 決定 2）ので `ledger_ok=false` と「注記が無いこと＝対象外ではない」のバナー。**順位・スコア・判定は変えない**

種別: バックエンド ／ 依存先: index_events.py

## `portfolio_ledger.py`

**売買の記録からの保有・損益・期間リターン**（#1222）。運用の成功指標（[VISION.md](VISION.md)）を測る層で、予測ではなく**実際に持った銘柄**の成績を出す。純関数ブロック（`validate_entry` / `prepare_entry` / `effective_entries` / `to_current_scale` / `positions` / `performance` / `split_warnings`・database を import しない）と、読むだけの DB ブロック（`load_*` / `summary`）に分かれる。記録（`trade_ledger`）は追記のみで、訂正は取消の行（`side=void`）を足す。保有の表は持たず毎回導出する。**株価の水準**: DB の株価は分割があると過去の値も今の水準へ直るが、記録は当時の生の値なので、分割を 1 行（`side=split`）として書いてもらい、その行より前の売買を今の水準へ直してから計算する。公式の係数（`jquants_adj_factor_events`）は**補正には使わず**、保有中の銘柄に分割の記録があるのに行が無いときの警告にだけ使う（係数の表が覆っていない所で黙って間違うのを避ける）。**期間リターン**は週ごとの時間加重（その週の買いは週の初め・売りは週の終わりとみなす＝買い増しを増減と読まず、買値・売値までの値動きも落とさない）で、比べる相手は `macro_data` の `TOPIX`（1306.T・配当なし）を**同じ週だけ**つなぐ。全履歴があるのは週次の株価だけなので、週の途中の下落は見えない。書く口は `routers/collect.py`（閲覧専用の環境で 403）、読む口は `routers/portfolio.py`。**画面は分析画面の「保有を見直す」タブ**（`templates/analysis.html` の「売買の記録」「保有と運用成績」・`static/js/analysis.js` の同名の節）で、タブを開いたときに読み、記録の保有を売り候補の入力欄へ写せる。取消は画面内の確認を通す。配線は `tests/test_portfolio_screen.py` が照合する。Render の断面には行が無い（空の結果を返す）。週次のバックアップには含める（`SYNC_PLAN`）

種別: バックエンド ／ 依存先: database.py（遅延 import）

## `index_events.py`

**指数イベント台帳**（#1003・[ADR-0068](adr/0068-index-events-live-apart-from-corporate-actions.md)）。TOPIX の移行措置・新規採用・2022 前例の段階的ウエイト低減を、JPX の公式一覧の登録表（`REVIEWS`・公表日・反映日・件数・元資料と正規化 CSV の sha256）と係数の道筋（`SCHEDULES`・`1 - m*step` の式・日付ごとの根拠）から公表日 as-of で時点再現する。入口は `load_ledger(db)`（`.index_lists/<公表>.csv` と企業表を読む。ファイルが無ければ `ok=False`）、問い合わせは `status_at(ledger, ec, on, known_at)` / `group_labels` / `panel_index_status` / `members`、次の見直しは `next_review_expected()`。件数・前後の一覧の差（`DELISTED_DURING` の d1 と `CODE_SUCCESSIONS`）・突合率 98%（一覧ごと）が合わなければ例外。**F・TTM・`tradable_filters` へは流さない**（`corporate_actions` / `ttm_composite` / `database` は import しない・テストで固定）。一覧は `scripts/import_index_list.py` が作る

種別: バックエンド ／ 依存先: 標準ライブラリのみ（I/O は database・panel は pandas を遅延 import）

## `ttm_composite.py`

**TTM 行の合成**（#424 子2・[ADR-0051](adr/0051-latest-results-enter-analysis-as-a-ttm-composite.md)）。「前期の通期 − 前期の H1 + 今期の H1」で 12 か月ぶんのフローを作り、ストックは今期 H1 の期末値を使う行を `ttm_financial_records` へ**毎晩全置換**する（夜間の分割補正係数の直後・入口は差分と全件の2本）。上半分は DB を引かない純関数で、分割窓と F の掛け方は企業イベント台帳（`corporate_actions`）から取り、`collector_prices` は遅延 import する。台帳はパイプラインが係数表と共有するものを受け取る（渡されなければ自分で作る）。**材料の間に分割があると1株指標の基準が混ざるので作らない**——危ない窓は（前期 H1 の提出日, 今期 H1 の提出日]で、判定は①株数比と「純利益÷EPS」の逆算株数が 1.4 倍以上②検出器のイベント窓が重なる（倍率待ちを含む）③公式 `AdjFactor` の日付が窓の中、の3信号（登録表のスピンオフは権利落ち日1点の窓として②と同じく効き、理由は `split_spinoff`・#740）。会計基準の変更（材料3つの `financial_records.accounting_standard` が1つでも違う・#966）・連結範囲の変更（`statement_disclosure.doc_type`）・決算期の変更・提出が期末＋45日より遅い行・桁の外れ（前期通期比が5倍/1/5の外）でも作らず、**理由ごとに件数を毎晩ログへ出す**。H1 で空になる列は、翌年の通期にあると確かめた列だけ引き継ぐ（`bs_bps` は通期の BPS を純資産比で伸ばす）。F は今期 H1 の**提出日より後**のイベントの積。実測 17,250 行 / 4,026 社・15.4 秒（2026-09-18）

種別: バックエンド ／ 依存先: corporate_actions.py, database.py, collector_prices.py

## `sysmem.py`

**プロセス常駐メモリと物理メモリの実測**（2026-09-01）。`rss_mb` / `peak_rss_mb` / `available_mb` / `total_mb` / `tree_rss_mb` / `format_line` を持ち、Windows は ctypes（`GetProcessMemoryInfo` / `GlobalMemoryStatusEx` / `CreateToolhelp32Snapshot`）、Linux は `/proc` を読む（**psutil を入れない**＝本番 `requirements.txt` の footprint を増やさない）。**測るのはプロセスツリーの合計**——`venv\Scripts\python.exe` はランチャースタブで、`Popen` の pid を単体で測ると実体が 313MB でも 4MB と返り、エラーにならず「静かに正しく見える」。**取れなければ例外ではなく None**（計測の失敗が本業を止めない／`format_line` は欠測を `?` で出す＝0.0 と混ぜない）。消費者は `scripts/batch_common.py` の heartbeat・`env_lines()` と `scripts/bench_macro_beta.py` の `peak_rss_mb` / `env_fingerprint`（**ctypes をあちらへ書き写さない**）。月次バッチ初実走で `tune` が 156分間 heartbeat を出しながら1件も進まず、CPU 39% に対し空き物理メモリ 0.6GB でページアウトしていた——所要だけのログでは「遅い」と「止まっている」を区別できない

種別: バックエンド ／ 依存先: （標準ライブラリのみ）

## `jax_import_guard.py`

**Smart App Control が CPU の NUTS で使わない jaxlib 拡張を遮断しても CPU の jax を立ち上げる**（#782・#789）。jax は CPU でも `_mosaic_gpu_ext` を起動時に無条件 import するので、GPU 専用の1ファイルの遮断で jax・numpyro 全体が落ちる。`install()` が meta path finder を先頭へ挿し、`GPU_ONLY_EXTENSIONS` の読み込みが**遮断（Windows エラー 4551 の文言）で**失敗したときだけ空モジュールで代替する。**import 時に属性を読まれる部品は `STUBBED_EXTENSIONS` に読まれる属性だけを持つスタブで代替する**（`jaxlib.cpu._sparse` は numpyro の import が `registrations()` を呼ぶ。`{}` を返すとカーネルが未登録になるだけで NUTS はビット一致・#789）——遮断された DLL は実行しない／別の失敗と一覧外は従来どおり送出／Windows 以外は何も挿さない。`macro_beta_inference` がモジュール冒頭で、`scripts/check_heavy_imports` が `main()` 冒頭で呼ぶ。代替は `substituted()` で取り、両者がログへ残す。経緯は [GOTCHAS.md](GOTCHAS.md)

種別: バックエンド ／ 依存先: （標準ライブラリのみ）

## `collector_utils.py`

収集系モジュール共通の設定定数（EDINET/J-Quants/Yahoo のレート・並列数・バッチ閾値。stooq の定数は #736 で経路ごと撤去）とロガー `log`。価格スケール突合の丸め許容 `rounding_tolerance`（#466/#620）もここに置き、J-Quants の取り込みと修復スクリプトが書き写さずに共有する。公式 `AdjFactor` が持たないスピンオフ調整の登録表（`SPINOFF_ADJUSTMENTS` / `spinoff_factor`・#568）は企業イベント台帳 `corporate_actions.py`（[ADR-0062](adr/0062-corporate-action-knowledge-lives-in-one-ledger.md)）

種別: バックエンド ／ 依存先: dotenv

## `collector_master.py`

企業/業種マスタ収集（企業一覧 `fetch_edinet_code_list`＝名前に反して書類一覧 API の走査・JPX 業種マスタ `resolve_jpx_excel_url` / `update_industry_from_jpx` / `_read_jpx_excel`（見出し名で読む `read_jpx_listing` の窓口・#1004）・同じダウンロードから上場銘柄一覧の月次断面 `parse_jpx_snapshot` / `record_jpx_listing_snapshot`（`jpx_listing_snapshots`・#1004）・その断面から `companies.market` を埋める `plan_company_market` / `update_company_market`（#1015）・業種が空の社の補完 `fill_industry_from_edinet_codelist`（選び方 `_plan_industry_fill`・書き込み `_apply_industry_fill`）/ `_read_edinet_codelist`＝EDINET コードリストの提出者業種・#784。上場区分によらない・#797・ADR-0065）

種別: バックエンド ／ 依存先: EDINET API, EDINET コードリスト, JPX, collector_utils

### `jpx_listing_snapshots` の表

（[ARCHITECTURE.md](ARCHITECTURE.md) §3 の前置きから移した・#1300）

**`jpx_listing_snapshots`（#1004・親 #1001）**: 東証上場銘柄一覧（data_j）の月次断面を (源 × 時点 × 証券コード) で積む。JPX は過去版を配らないので、取り逃した月は永久に欠ける。書き手は `collector_master.record_jpx_listing_snapshot` だけで、夜間 Phase 5 の `update_industry_from_jpx` が**業種用に取ったのと同じダウンロード**から、業種の更新の後に呼ぶ（失敗は WARNING＝業種は止めない）。列は見出しの名前で引き（`collector_utils.JPX_SNAPSHOT_COLUMNS`）、市場・商品区分・33業種区分・規模コード・規模区分は生値のまま。`edinet_code` は書き込み時点の企業表で解決する（ETF 等は NULL のまま残す）。書く前の健全性（行数・規模区分の語彙・'-' 以外の件数・日付が1種類）が外れたら書かない。同じ時点は上書きせず `last_seen_at` だけ進め、中身が違えば WARNING と差の件数を出す。**可視判定は `first_seen_at`**（`database.jpx_listing_snapshot_at`）。前進は `batch_freshness.PRODUCERS` が2本で見る（取得＝毎晩の `last_seen_at`・時点＝`as_of_date` が月の最長31日＋公表遅れ `JPX_LISTING_PUBLICATION_LAG_DAYS` を超えて止まっていないか）。断面の記録の後、`collector_master.update_company_market` が最新の断面から `companies.market`（最新の表示用）を変わった社だけ埋める（#1015・別名表 `JPX_MARKET_ALIASES`・年度の行は埋めない）。

## `collector_prices.py`

株価収集（Yahoo / J-Quants。stooq は個別銘柄の株価履歴収集 `/api/collect/history/{start,stop,status,stream}` とマクロのフォールバックごと撤去済み＝どの実行環境からもボット検証で CSV が取れない・#736）＋市場データ更新＋マクロ指標収集。株価は夜間バッチ（`_pipeline_incremental.py`）の Yahoo 欠損補完 → J-Quants catchup（公式値への置き換え）で入る。**`financial_records` への株価・PER/PBR/時価総額の反映は `update_market_data_from_history` に一本化**（#428。旧 `update_market_data`＝stooq へ全社逐次リクエストする経路は削除。GUI `/api/collect/market-data`・CLI `--market` も同関数を呼ぶ）。`MACRO_SERIES` で為替・金利・指数・コモディティ・ボラ、`FRED_SERIES` で FRED 11系列（米クレジット/インフレ＋#381 非ICE信用代替 `BAA_SPREAD`＝`BAA10Y` Baa−10Y＋#250 日本実体経済3種＋#404 政策不確実性2種（`US_EPU`＝`USEPUINDXD`・`US_EQUITY_EPU`＝`WLEMUINDXD`・Baker-Bloom-Davis EPU・日次1985〜・日本版 `JPNEPUINDXM` は 2016-04 凍結のため不採用）。`HY_OAS`/`IG_OAS` は FRED の ICE BofA 3年窓制限で 2023-06 以降のみ＝strict の既定からは除外・選択肢としては残置・ADR-0016）、`BOJ_SERIES` で日銀 API（M2 月次＋短観DI の各バリアント・四半期・認証不要）、`OECD_SERIES` で OECD SDMX API 1系列（`JP_CLI`＝日本 Composite Leading Indicator・振幅調整済・認証不要・ADR-0009・#283）、`ESRI_SERIES` で内閣府ESRI直接CSV配布4系列（`JP_GDP_PRIVATE_CONSUMPTION`個人消費・`JP_GDP_RESIDENTIAL_INV`住宅投資・`JP_GDP_CAPEX`設備投資・`JP_GDP_PUBLIC_INV`公共投資・認証不要・1994Q1〜最新を含む単一CSVを直近4四半期×速報2種のURLプロービングで取得・#286）、`IMF_SERIES` で IMF WEO 見通し2系列（`JP_WEO_GDP_FCAST`実質GDP成長率見通し・`JP_WEO_CPI_FCAST`インフレ率見通し・いずれも翌年予測・認証不要・唯一のforward-lookingチャネル・バックフィルは `WEOhistorical.xlsx`（point-in-timeパネル・vintage先読みバイアス回避）、継続収集は現行dataflowをtrade_date=収集日で固定・ADR-0011・#284）、`ESTAT_SERIES` で e-Stat API 3系列（全国CPI総合/コア・東京CPI）、`ESTAT_INDEX_SERIES` で e-Stat 鉱工業指数2系列（`JP_IIP`生産・`JP_IIP_INVENTORY`在庫・#253のFRED凍結代替・#281）、`GDELT_SERIES` で GDELT DOC 2.0 API 3系列（`JP_NEWS_TONE`／`JP_NEWS_ECON_TONE`＝ニュース平均トーン・`JP_NEWS_ECON_VOL`＝報道量%・認証不要・2017-01-01〜・全期間を1リクエストで日次取得・レート制限1req/5sで**超過時も HTTP 200＋プレーンテキスト**のため本文JSON判定でリトライ・ADR-0024・#406）、`WIKIMEDIA_SERIES` で Wikimedia Pageviews API 2系列（`JP_WIKI_MARKET_ATTN`／`JP_WIKI_MACRO_ATTN`＝ja.wikipedia 記事バスケットの日次閲覧数合算・認証不要だが User-Agent に連絡先必須・2015-07-01〜・欠測日は0埋めせず除外）を定義。**`MOF_SERIES`** で財務省「国債金利情報」CSV 1系列（`JP10Y_MOF`＝日次の日本10年金利・認証不要・PDL1.0・1986-07〜・cp932/和暦・`parse_mof_jgb_csv`。**初回は全期間版 `jgbcm_all.csv`＋当月版 `jgbcm.csv`、以降は当月版のみ**＝全期間版は月次更新で当月分を持たない・#458/ADR-0029）（e-Stat 系はいずれも `ESTAT_API_KEY` 要。GDELT/Wikimedia は**マクロ集約のみ＝銘柄別日次は370MB/年で無料枠に入らないため採らない**）。公表ラグは各系列の `lag_days` で `trade_date` をシフトして先読みバイアスを防ぐ。TOPIX は指数 ^TPX 配信停止のため ETF 1306.T で収集（#250）。**週次株価の段差（分割の遡及調整もれ）検出・修復**（#465）: `detect_price_scale_breaks`（契約窓を1リクエストで学習 → weekly に実在する `trade_date` から月次で突合日を選定 → `AdjC` と `weekly.close_last` を突合）と `repair_price_scale_breaks`（該当銘柄だけ Yahoo で全履歴を取り直し `record_prices_batch` へ通し、同じ突合日で検算）。CLI は `--repair-price-breaks`（既定 dry-run・`--persist` で書込・`PRICE_BREAK_MAX_REPAIR` 超過で中止）。5桁コード→4桁の縮約は `is_common_stock_code` で**普通株を優先**（日次収集の dedup と突合の両方が同じ規則を使う）。**毎晩の鮮度確保 `fill_recent_stock_price_gap_yahoo(gap_days=0)` の対象判定は `last_closed_session`（閉場済みの最新 JST 営業日）**＝ランナーの UTC 日付と比べると JST 日曜/月曜の早朝に全社が空振り対象になる（#474・本番実測 4,437社→735社）。**そのうち 454社は価格を1件も持たない `is_active=False` の社**で、`should_retry_priceless_delisted` により `DELISTED_RETRY_INTERVAL_DAYS=7` 日に1回だけ試す（恒久除外にすると #463 の誤 delisted 判定を拾い直せない・#475）。**株価履歴を持つ廃止社**（`is_active=False`・サフィックス未解決・最終株価が `DELISTED_STALE_DAYS=30` 日より前）も同じ間隔で試す（#556・実測 263社が毎晩 404 を返していた。最終株価の古さを条件に入れるので、一度履歴を得た新規上場は巻き込まない）。Yahoo の HTTP 失敗は `yahoo_http_stats()` で数え、4xx は 404 を内訳として分けて出す（`4xx=N（うち404=M）`）。基準セッションより後の `trade_date`（Yahoo が場中に返す進行中バー）は取り込まない。J-Quants catchup は受け取った日次バーのうち `AdjFactor != 1`（普通株のみ）を**価格行の選別より前に** `jquants_adj_factor_events` へ upsert する（#661・API 呼び出しは増えない・保存失敗は戻り値 `adj_factor_events=None` で価格収集の結果は返す）。分割補正係数 F の洗い替え（`rebuild_split_adjustment_factors`）と公式イベント・スピンオフの判定は企業イベント台帳（`corporate_actions.py`）が持つ（#746・ADR-0062）。Yahoo の株価の書き手は DB の直前値と100倍以上離れた値を書かず（gap-fill は離れたバーだけ捨てる・弾いた社は `app_settings.yahoo_scale_rejections` に「社＋基準日」で記録して新規だけ WARNING）、夜間の末尾で株価表全体の100倍段差を走査する（`scan_price_scale_steps`・#765・4-2 節）

種別: バックエンド ／ 依存先: J-Quants, Yahoo, FRED, 日銀API, OECD API, ESRI, IMF API, e-Stat, collector_utils, corporate_actions

## `tuning_cycle.py`

**探索の周回の状態と段の移り変わり**（#1114・[ADR-0070](adr/0070-search-selects-in-two-stages-for-top-k-buying.md) 決定 4・5）。日中枠の空きで M-2 の固定の候補集合（月次と同じ random 150 件・種 0）を数日に分けて回し切る。1 日ぶんは「決勝候補（前日までの上位 `--confirm-top` 件の params）＋次の束」を**その日のデータで**測る勝ち抜き戦で、評価値は翌日へ持ち越さない（世代の違う値を混ぜない・ADR-0054 の「続きから再開」は却下のまま、続くのは**どこまで測ったか**だけ）。回し切ったら決勝候補＋本番の設定を種を変えて確かめ直し（2 段の選び方）、**周回の終わりに 1 回だけ**本番へ反映する。次の周回は「済んでいて暦の月（JST）が変わった後の最初の取り出せる日」、候補集合の定義が変わればすぐ、31 日たっても終わらなければ捨てて始め直す。状態は `app_settings.tuning_cycle:<model>`（JSON）で、周回ごとの要約（戻す規則の材料＝2 段目が rank-IC の 1 位と違う候補を選んだか・前半/後半の差）を `history` に 24 周回ぶん残す。**database も plugins も import しない純関数**（日中枠の起動判定と watchdog が軽く読む）。実行は `hyperparameter_search.run_cycle`、組み込みは `scripts/run_daytime.py`、見張りは `batch_freshness.PRODUCERS`（約束＝毎月始まり 14 日以内に終わる・まだ 1 度も終わっていない間は導入日から数える）

種別: ユーティリティ ／ 依存先: hyperparameter_search.py, scripts/run_daytime.py, batch_freshness.py

## `sector_gap_asof.py`

**学習パネル用の時点再現の gap_ratio**（#626・[ADR-0057](adr/0057-past-gap-ratio-is-reconstructed-as-of-each-month.md)）。`recommend_factor_premia.build_period_panel(with_gap_ratio=True)` と、バックテストの開始日 1 日分の `gaps_on`（#1248・決算日がその日の 45 日前以前の各社の最新の 1 行だけを読む＝全年度を読まない）が使う。月末ごとに「その月末に見えていた各社の最新の通期行（期末＋45日・`_find_applicable_fin`）」と「その月末の週次終値（`month_end_indices` の足＝`build_snapshots` と共有）× 分割補正係数 F」で`SectorOLSPlugin.predict_gaps`（保存しない経路）を回し、`{(edinet_code, ym): gap_ratio}` を返す。**`regression_results` へは書かない**（画面・バックテスト・鮮度表示に混ざらない）。**株価は `financial_records.stock_price` を読まない**——過去行には「その年度が最新だった頃に毎晩上書きされた現在株価」＝最大1年先の値が残っており（期末±3% に入るのは 2023年度 70%・2024年度 65%）、年度ごとに回すと先読みになる。回帰の母集団はその月末に株価がある全社（パネルの標本＝52週先リターンがある社ではない＝生存バイアスを入れない）。回帰の設定は `nightly_scores.NIGHTLY_PARAMS` を唯一の源にする。**行は edinet_code 順に固定**（回帰は並びに依存しない＝ridge の α は LOO・#697・[ADR-0058](adr/0058-ridge-alpha-is-chosen-by-loo.md)。固定は浮動小数の加算順まで揃えてパネルをビット単位で再現するため）。上半分は DB を引かない純関数

種別: バックエンド ／ 依存先: plugins/sector_ols.py, plugins/macro_snapshots.py, nightly_scores.py, database.py

## `collector_disclosures.py`

会社予想（ガイダンス）開示収集（J-Quants `/fins/summary`・Issue #322）。**`recommend_factor_premia.py` はこのテーブルに依存せず、`fin_features` は `financial_metrics` VIEW の `z_*`（`plugins/recommend.py::METRICS`）から取る**（#576 で CLAUDE.md から移設）。決算短信サマリーの生データ（実績 Sales/OP/OdP/NP/EPS・当期予想 FSales/FOP/FOdP/FNP/FEPS・翌期予想 NxFSales〜NxFEPS・DiscDate/DiscTime/DocType・**株式数 ShOutFY/TrShFY**）を `statement_disclosure`（PK=disc_no・disc_no はJ-Quants開示番号でグローバルに一意）へそのまま蓄積。**`companies.issued_shares` の J-Quants 経路もここが唯一の入口**（#462・v2 の `/equities/master` は株式数を持たないため。主経路は XBRL で、`_update_issued_shares` は NULL のみ補完）。無料プラン制約は株価と同一設計（`JQUANTS_BACKFILL_DAYS`＝730日遡及、`JQUANTS_DISCLOSURE_DELAY_DAYS`＝84日＝12週固定の配信遅延・実測）

種別: バックエンド ／ 依存先: J-Quants, collector_utils, collector_prices, database.py

### `statement_disclosure` の表

（[ARCHITECTURE.md](ARCHITECTURE.md) §3 の前置きから移した・#1300）

`statement_disclosure`（Issue #322・`collector_disclosures.py` が J-Quants
`/fins/summary` から蓄積する生データ）は**会社予想の中身を分析に使っていないが、収集は止めない**。
読み手は2つある: ①収集の途中で `ShOutFY` を拾い `companies.issued_shares` を更新する
（発行済株式数の J-Quants 経路の唯一の入口・`collector_prices._update_issued_shares`・#462）
②夜間の TTM 合成が会計基準と連結・単体の別を読む（`ttm_composite._load_basis`・ADR-0051）。
**`disc_date` は常に `today − 84日` が天井**（`JQUANTS_DISCLOSURE_DELAY_DAYS`＝無料プランの
構造的上限）で、収集頻度を上げても前進しない＝齢を「壊れている」と誤診しないこと（#424 子4）。
予想対比サプライズの特徴量化層 `feature_disclosure.py` は、唯一の予定消費者 #323 が not planned で
閉じ、呼び出し元も無くなったので #1290 で退役した（取り出し方は `docs/archive/IMPROVEMENTS.md`）。

## `collector_interim.py`

半期(H1)財務収集（Issue #219② フェーズB）。EDINET 半期報告書（新式 formCode=043A00/docType160）と旧四半期報告書（043000/docType140）の **Q2(中間=H1累計)** を収集し `financial_records` に `period_type='H1'` で保存。**通期(annual)収集とは独立**・低レベルヘルパ（`fetch_xbrl_csv`/`parse_xbrl_csv`/`calc_derived`/`upsert_financial`）を再利用。新旧いずれも P/L・CF は context `CurrentYTDDuration`(H1累計)・BS は `CurrentQuarterInstant` で既存パーサが無改修で抽出。真の H1 期末は DEI `CurrentPeriodEndDateDEI`（metadata periodEnd は新式でFY末を返すため不使用）、H1 判定は DEI `TypeOfCurrentPeriodDEI` が `Q2` または `HY`（旧四半期の Q1/Q3 を除外。2025年提出の新式半期報告書は `HY` を名乗り、context も `InterimDuration`。半期報告書＝docType 160 が H1 と名乗らなければ `failed_dei_type` として失敗に数える・#647）。旧四半期は metadata periodEnd 月で事前選別しダウンロード数を削減（`build_fy_end_month_map`/`prefilter_interim_docs`）。CLI `python collector.py --interim --years N`（常に差分）・手動WF `collect-interim.yml`

種別: バックエンド ／ 依存先: EDINET API, collector_utils, collector_financials, database.py

## `tests/`

pytest 回帰テスト（件数は書き写さない。数えるときは `pytest --collect-only -q`）。ドキュメント同期のメタ検査（`test_docs_sync.py`＝MODELS.md §9 の章立てと M-1 副読本・#472）と、**リンク実在のメタ検査**（`test_docs_links.py`＝全 `*.md` の相対リンクが実在するかを照合。リンク切れはテストも通りアプリも動き通知も鳴らない＝気づく手段が人間の定期点検しか無かったため、`/tidy` が手で `git grep` していた検査を CI へ移した。実際に ADR 3本のリネーム追随漏れを取り逃していた。失敗メッセージは同じ ADR 番号・同じベース名の実ファイルを**修正候補として出す**。外部 URL と同一ファイル内アンカーは対象外＝前者はネットワーク依存で CI を不安定にし、後者は見出しの表記ゆれで偽陽性を量産する。**走査本数とリンク総数の下限を assert する**——正規表現が壊れて0件抽出になると「リンク切れゼロ」で静かに通る）と、クエリ列スコープのメタ検査（`test_column_scoping.py`＝`db.query(Model)` の全列 ORM ロードを AST で検出し許可リストと双方向差分・#482）を含む。プラグイン＋utils＋`database.py`（upsert・RegressionResult merge・derived非永続）＋`collector.py`（XBRLパース・派生指標＋ネットワーク取得を httpx MockTransport でモック）＋`api.py`（純関数・`/health`・DB-backed 読取・heavy回帰のRenderブロック）をカバー。in-memory SQLite fixture（StaticPool）／FastAPI TestClient／httpx MockTransport で検証。`financial_metrics` は SQLite では `FinancialMetric` 列定義から生成したテーブルで代替し、派生値・予測値はテストが直接注入（`make_metric`）。計算式の同値性は Postgres で別途検証。共通 fixture は `tests/conftest.py`（`db`/`make_fin`/`make_metric` 等）

種別: テスト ／ 依存先: pytest, sqlalchemy, fastapi, httpx

## `dashboard.html`

トップページ・全体サマリー（`/`）。「よく使う」の先頭に朝の推奨（`/morning`）への導線。**「自動収集」カードは静的な予定表を持たず**、`/api/stats` の `batch`（足跡）から `common.js::renderSchedule` が描く（#563・収集画面と同じ関数・#1212）——以前は「GitHub Actions で毎日 03:00 JST」を緑ドット付きで固定表示しており、#503 で cron が止まった後もトップページが嘘をつき続けていた（しかも 03:00 は GHA 時代ですら誤りで、実際の cron は 17:17 JST）。**名目の起動時刻も書かない**——実起動は最大 +1h41m ずれる（#551）

種別: フロントエンド ／ 依存先: api.py

## `morning.html` / `static/js/morning.js`

朝の推奨（`/morning`・Issue #423 子3）。`GET /api/morning` を1本叩くだけで、**学習も再計算もしない**。判定と鮮度の下に**今日の売買候補**（#1228）を置く——実際の売買は M-6・M-2 の上位を買い、保有の下位 1/5 を売る（ADR-0070）ので、`/api/plugins/{macro_enet,macro_gbdt}/saved?limit=10`（今買える社の μ̂ の上位 10 社・基準日つき）と `/api/portfolio/sell-candidates`（売買の記録の保有の売り判定）を読んで 3 枚に並べる。両方の上位に入る社に「両方」、保有に「保有中」の印。**μ̂ を買い推奨ランキングの点数へ混ぜない**（並べるだけ・混ぜるなら昇格ゲート＝ADR-0030）。3 本は独立に読み（1 本の失敗で他を消さない）、プリセット・件数を選び直しても読み直さない。M-6 の μ̂ が古いときはその一覧の基準日を注意色にする（総合判定は買い推奨ランキングの元データだけを見るまま）。配線は `tests/test_morning_page.py::TestTradeCandidates` が照合する。総合判定バナー（fresh 緑／warn 黄／alert 赤＋理由リスト＋該当ワークフローへのリンク。赤まで効くのはランキングの元データ＝夜間バッチ・株価・乖離率だけで、スコアに入っていない μ̂・マクロは黄が上限・`routers/morning.py::_overall_verdict`）＋鮮度5カード（**夜間バッチの足跡** / 株価 as-of p50 / 乖離率 computed_at / μ̂ snapshot_date / マクロ critical 系列）。鮮度は `<details>` で畳め、要約1行（各項目の ✓ / ! / ×）を常に出す。**768px 以下では畳んで始める**（開いたままだとカード5枚でランキングが4画面下へ押し出される）。ランキングは 480px 以下で1社1枚のカード（名前・スコア・乖離率・株価 as-of）になる。乖離率は `gap_ratio` が保存時点で % なのでそのまま出す（×100 していた間は分析画面の 100 倍に見えていた）。**バッチのカードは先頭**——下流（株価・スコア）の古さはバッチが走らなかった結果でしかないため（#561）。正常時も「いつ走ったか」を必ず出す（異常時だけ出す設計では沈黙が読めず、**健全なのに「止まっているのでは」と疑われる**）。リンク先は停止済みの GitHub Actions ではなく `docs/DEPLOYMENT.md`＋買い推奨ランキング表。**赤でもランキングは隠さない**（隠すと別経路で古い値を見に行くだけなので、出した上で「この結果で発注しないでください」を明示する）

種別: フロントエンド ／ 依存先: api.py, routers/morning.py

## `collection.html`

収集管理・スクリーニング・DBブラウザ（`/collection`）。「財務データ収集」タブの先頭に、ホームと同じ「自動収集」の表（`/api/stats` の `batch`・`common.js::renderSchedule`）を置き、遅れた行があれば復旧手順（`batch.url`）へのリンクを出す。「スマート収集」は財務データがあるとき畳み、1画面目の主ボタンは1つ（#1212）。**タブとスクリーニングの条件は URL に載せる**（`?tab=`・`?per=` など。朝の推奨は `?preset=&n=`、企業詳細は `?tab=`）。読み書きは `common.js` の `urlStateGet` / `urlStateSet` の1か所で、`replaceState` だけを使い履歴を積まない。URL の値は一覧・選択肢・数値として確かめ、外れたら既定で開く。企業名のリンクは全画面で新しいタブに開く（#1213）。業種プルダウンは `/api/industries`、BS/PL/CF 再分類ビューの企業は検索欄で絞って選ぶ（全社のプルダウンは `/api/companies` の上限 500 を超えるため作らない・#803）

種別: フロントエンド ／ 依存先: api.py

## `analysis.html`

分析ハブ（`/analysis`）。左サイドバーを `/api/plugins` のメタ（category/ui_order）からよく使う順の6カテゴリ（将来リターンを予測/保有を見直す/銘柄を探す/割安度を測る/比較用のモデル/戦略を検証・番号は振らない）で動的生成（`buildSidebar`）。先頭（＝既定タブ）は本番のモデルで、`/api/plugins` の `production`（`routers/analysis.PRODUCTION_MODELS`＝朝の買い候補∪売り判定の出所・`test_analysis_meta.py` が照合）に「本番」の札を付ける。名前に記号を足さない（表記は CONTEXT.md「画面の表記」）。売り候補ランキング（`#tab-sell_ranking`・保有銘柄の売り時）は静的タブ＋保有入力 textarea（localStorage 記憶）。バリュエーション分析に横断分布（理論vs実績の散布図・乖離率ヒストグラム）を Chart.js で表示。スクリーニングは特例エントリとして `/collection` へリンク。動的タブの結果描画は `RESULT_RENDERERS`（plugin名→描画関数の登録制・未登録は汎用フォールバック）。リスク-リターン散布図は M-2 と M-6 が共通ビュー `_riskReturnView(cfg)`（タブごとの状態・λ/横軸/R3/件数の再実行なし再描画・行クリックは `data-click` 委譲＝インライン onclick は CSP で遮断される）を共有し、M-1・M-3 は個別実装のまま（#807。M-3 の行クリックも `data-click` 委譲へ移した・#811）。CSV出力は単一の `exportCSV(name)` ディスパッチャ（`CSV_EXPORTERS` 登録制）に統一。バリュエーション分析タブに**モデル鮮度バー**（`#model-freshness-bar`）を常設 — `/api/model/status` から computed_at/staleness_days を取得して表示し、OLSロック演出を廃止。買い推奨タブには**株価 as-of バー**（`#price-freshness-bar`・#416）を常設 — `/api/stats` の p50/p05 と齢（営業日）を表示し、5営業日超で黄・10営業日超で赤＋「この結果で発注しないでください」。赤のときは実行前に `confirm()` で確認（**実行はブロックしない**＝z_momentum を外した分析など正当な用途を殺さない）。結果テーブルとCSVに銘柄別「株価日」列。**heavy のタブは「読む場所」が既定**（ADR-0072）: 開くと `/api/plugins/{name}/saved` の保存済みの結果（M-2/M-3/M-6 は μ̂ の上位・業種別OLS は計算時刻と社数）を先頭に出し、設定フォームは `<details class="rerun">`「設定を変えて再計算」へ移す（保存済みがあれば閉じる）。再計算は既定で保存せず、実行ボタンの直前の「結果を保存する」（`writes_impact` を併記）を選んだときだけ `?persist=1`

種別: フロントエンド ／ 依存先: api.py, Chart.js (CDN)

## `models.html`

モデル解説・参考文献ページ（`/models`）。全モデルの数式・パラメータ・DOIリンクをインラインHTMLで表示（節の顔ぶれは `MODELS.md` の章と1対1で対応する・#712。節の数をここへ書き写さない＝増減しても失敗として現れない）。冒頭に**分析の3層モデル**（一次分析／双対／メタ検証・`#layers`）の枠組みを置き、本文は `guide.html` と揃えた**目的別5カテゴリ**（銘柄を探す/割安度を測る/将来リターンを予測/戦略を検証/保有を見直す・学ぶ順。サイドバーと名前はそろえるが番号は振らない）で `cat-header` グルーピング。各モデルは `#mN` でディープリンク可能（番号表示は廃止しアンカーIDのみ維持）。旧「総合リターン予測」(`#m1`) はバリュエーション分析へ統合し削除（ADR-0001）。

種別: フロントエンド ／ 依存先: —

## `guide.html`

初心者向け「やさしい解説」ページ（`/guide`・ログイン前でも読める）。読者は「投資の基本は知っているが統計とこのアプリは初めて」の人で、数式を使わない。**第1部 はじめに**（できること・最初に見る順番・使う前の注意）／**第2部 画面の歩き方**（`#screen-<画面>`）／**第3部 分析手法**（節の id はプラグイン名。ひとことで言うと／何が分かる／どう使う／注意点、末尾から技術版 `/models#mN` へ。退役モデルは末尾の「退役したモデル」）／**第4部 用語集**（`#term-<slug>`）／もっと詳しく（`/models` と GitHub 上の M1 副読本・MODELS・README）。入口は gnav・ホームの「はじめての方へ」・各画面ヘッダーの「この画面の見方」・分析画面の「❓ やさしい解説」・朝の推奨の「表の見方」・ログイン画面。**`/guide#<id>` のリンク先の実在と、全プラグインに節があることは `tests/test_guide_links.py` が照合する**（節の id をここへ書き写さない）。TOC 追従は `models.js` を再利用（目次が指す見出しだけを見て、光った項目を目次の枠内へ寄せる）

種別: フロントエンド ／ 依存先: —

## `company.html`

企業詳細（`/company`・`/company/{edinet_code}`）。個別企業の業績・財務(BS)・CF・per-share/配当・バリュエーション（理論時価総額乖離）・日次株価・業種内Zスコアレーダー・清原式ネットキャッシュ・同業比較を Chart.js の時系列グラフで可視化。企業名・証券コード検索付き。財務(BS)タブはバフェットコード型で各年「左＝資産（借方）／右＝負債・純資産（貸方）」を並列表示し、粒度（粗/中/細）切替で内訳の細かさを変更できる（どの粒度でも資産バー＝負債純資産バー＝総資産になるよう補正）。業績(PL)タブは売上高を費用・利益に分解した積み上げ棒（最上部＝純利益）を粒度（粗/中/細）切替で表示（合計＝売上高、信頼性の低い stored gross_profit は不使用）。CFタブも粒度（粗＝フリー+財務／中＝営業/投資/財務／細＝営業/設備投資/その他投資/財務）切替に対応し、CFデータ未収集の企業には明示メッセージを表示。同業比較タブは選択企業を必ず表示し業種内時価総額順位を併記（並べるのは上場中の社だけ。廃止社は業種が付いていても古い財務のまま順位に混ざるため・#797。並べ替え・順位・母数は `/api/peers` がサーバー側で業種の全件に対して決める＝件数で切ってから画面で並べると、300社を超える業種で上位の社が漏れ母数も小さく出た・#801。廃止社のページでは順位を出さない）。**相互リンク**：理論時価総額/乖離率チャート→`/analysis?tab=gap`・Zスコアチャート→`/analysis?tab=recommend`・ネットキャッシュチャート→`/analysis?tab=net_cash`（逆方向のバリュエーション分析表→`/company/{code}` は既存）

種別: フロントエンド ／ 依存先: api.py, Chart.js (CDN)

## `static/js/*.js`

各HTMLテンプレから外部化したページ別JS（CSP対応）。common（`esc`/`apiFetch`/`initAuth`/`logout` 等の共通ユーティリティ・全ページ読込。`apiFetch` は失敗を本文の `detail` だけの `Error` に `status` を付けて投げ、404 も理由の文を捨てない。通知の文は `_errDetail(e)` で取り出す・#1175）+ theme-init（`localStorage`/`prefers-color-scheme` から `data-theme` を描画前に適用する FOUC 防止・全ページ `<head>` 読込）+ dashboard / collection / analysis / company / db / models / login / morning の10ファイル。`/static` で配信（api.py の `StaticFiles` マウント）。`<style>` はHTML側に残置。インラインイベントハンドラ（`onclick=` 等）はテンプレートにも JS が組み立てる HTML にも置かず、`data-click` 等＋委譲（`analysis.js` の `_wireDelegate`）で書く——CSP の `script-src-attr` で黙って止まるため。`tests/test_security_headers.py` がテンプレートと `static/js/*.js` の両方を照合する（#811）。

種別: フロントエンド ／ 依存先: api.py

## `_pipeline_gh.py`

全件収集パイプライン（full-pipeline.yml から workflow_dispatch 手動起動）。`--refill-cf`（CF NULL 補完: 投資CF/現金増減/capex）・`--refill-capex-only`（capex のみワンショット）・`--refill-cf-missing`（CF全NULL社=IFRS決算大企業の営業/投資/財務CFを補完）・`--refill-pl-bs`（bs_inventory NULL 補完: 旧コホート〜2022の PL/BS 列を XBRL 再取得で是正・古い順／`refill-pl-bs.yml`）・`--diagnose-cf`（CF ラベル診断）モードを持つ。`normal` CF補完は 2026-05-31 に完了（capex 88.8%充足）、IFRS/US-GAAP決算企業の CF全NULL は 2026-06-03 に `--refill-cf-missing` で補完し CF未収集 268社→0社（詳細は GOTCHAS.md「IFRS/US-GAAP決算のCF・売上要素名」「CF NULL補完の運用」「bs_inventory バックフィルの運用」）。

種別: GitHub Actions ／ 依存先: collector.py, database.py

## `_pipeline_vacuum.py`

月次 DB メンテナンス（`scripts/run_monthly.py` の先頭ステップ `vacuum` から起動・Issue #290 / #504。GHA の `vacuum-maintenance.yml` は 2026-08-25 に schedule 停止＝断面用の手動口だけ残る・#505）。AUTOCOMMIT 接続で **`TARGET_TABLES`（`stock_price_daily` / `stock_price_weekly`）を1表ずつ `VACUUM FULL`** し、前後の容量をログに残す。前段の `_tune_autovacuum()` が per-table の `autovacuum_vacuum_scale_factor` を 0.02 へ揃える（**冪等**＝既に望みの値なら ALTER を投げない）。クラスタ既定 0.2 では 128万行の `stock_price_weekly` の発火閾値が 256,943 行になり、dead 200,498 行を抱えたまま autovacuum が一度も回らなかった（2026-08-19・**故障ではなく閾値未達**）。チューニングは「これから溜まるのを止める」、VACUUM FULL は「既に溜まった分を回収する」で**両方要る**。ロック待ち（55P03）だけ 120秒間隔で最大3回再試行し、それ以外は即送出。`lock_timeout=90s`。`statement_timeout` は**親の締切から導く**（月次バッチの `vacuum`＝予算15分の「残り − 60秒」でサーバー側も打ち切る。締切切れ・再試行の待ちが締切を越えるときは始めず失敗・#868）。締切の無い起動（手動の GHA・手での直接実行）は `0`＝無制限（ADR-0032 とその追補）

種別: ローカル月次バッチ ／ 依存先: database.py, _pipeline_utils.py, hyperparameter_search.py

## `launch.py`

Windows ローカル開発用 tkinter ランチャー（uvicorn 起動 GUI）。ポート8000占有者を `/health` の `{"db": ...}` 応答で本アプリか識別し、別アプリ占有時は 8001〜 の空きポートへ退避起動。子プロセスへ `FINAPP_AUTO_SHUTDOWN=1` を設定しブラウザ切断（`/heartbeat` 途絶30秒・収集ジョブ実行中は保留）でサーバー自動停止＋ランチャーも自動クローズ。**接続先ラジオ（本番 / ローカル・#481 B-1）**を持ち、切り替えると `FINAPP_DB_TARGET` を変えてサーバーを入れ替える（世代番号 `state["gen"]` で旧監視スレッドが再起動を「停止検知」と誤表示しないようにする）。選択は永続化せず、毎回 `FINAPP_DB_TARGET`（未設定なら `local`＝正本）から始まる（#503 で既定を反転）。本番・CI からは未参照の独立ツール

種別: ユーティリティ ／ 依存先: uvicorn

## `batch_activity.py`

ローカルバッチの**「いま走っているか」**＝実行中マーカー（`.logs/<log_prefix>.running.json`）の唯一の源。足跡（`*_last_run`）は終わったときにしか書かれないので、画面から heavy を回す前に並走を知る手段がこれしか無い（並走は所要ではなく**計算結果そのもの**を変える）。書き手は `scripts/batch_common.run_batch` だけ（開始時に書き、ステップ開始と heartbeat で `heartbeat_at` を更新し、`finally` で消す＝手で `python -m scripts.run_*` を叩いた分も拾う）。**書けなくてもバッチは落とさない**（警告を1回ログへ）。読み手は `routers/analysis.py`（heavy の 409・`/api/batch/activity`・`/api/plugins/{name}/preflight`）で、`batch_freshness.WATCHED` の `log_prefix` を回す。**OS ごと落ちて残ったマーカーで heavy を永久に止めない**ため、マーカー自身が書いた `heartbeat_sec` から導出した期限（`2 × heartbeat_sec + STALE_GRACE_SEC`＝既定15分）を過ぎたものは `stale`（残骸）として別に返す。テストは conftest の `batch_activity_sandbox` が置き場所を tmp へ向ける（`run_batch` を呼ぶテストが走っている本物のマーカーを消さないように）。**逆向き（画面の heavy 実行中にバッチが起動する）は防げない**——バッチ側はアプリを見ていない

種別: ユーティリティ ／ 依存先: scripts/batch_common.py, routers/analysis.py

## `batch_freshness.py`

バッチ足跡（`app_settings` の `*_last_run`）を**測る**層（#561）。`Watched` / `WATCHED` / `collect()` / `status_of()` に加え、**画面表示用の `summarize()`**（#563）。**測るのは2軸**（#504）＝「走ったか」（足跡）と「走った結果として値が前進したか」（`Produced` / `PRODUCERS` / `collect_producers()`）。後者は月次3系統（`plugin_tuned_params` の M-1/M-2/M-3・`macro_beta_meta` の **live 行のみ**・`recommend_factor_premia`）と、夜間の JPX 業種マスタ（#632）、日中枠の暦が積む収集2系統（H1 と会社予想の `created_at`・読み手は `run_daytime.SCHEDULE` と共有・#681）、日中枠の残りで回す M-2 の探索の周回（周回の状態の `last_finished_at`・#1114。`plugin_tuned_params` は月次も書くので読まない）を読み、**watchdog の起票にだけ出す**（`collect()` / `summarize()` を触らないので `/api/morning` のレスポンス構造は変わらない）。分けた理由は 2026-09-01 の実例——月次のステップ失敗は #587 として起票・クローズされたのに、`plugin_tuned_params` は M-2 が 50日・M-3 が 59日 古いまま誰も気づかなかった。どの heavy を見てどれを見ないかは `PRODUCER_COVERAGE`（`watched` / `exempt:<理由>`）が持ち、`HEAVY_AUTOMATION` との照合を CI が行う。読み手は `scripts/check_batch_freshness.py`（毎日 JST 20:00・起票する側）と `routers/morning.py`（`/api/morning` の鮮度カード）、`routers/market.py`（`/api/stats` → ダッシュボードの「自動収集」）の3人。**表示の語彙と閾値を1箇所に集める**のが `summarize` の役目——ルータごとに「何時間前なら緑」を書くと、窓を広げたときに片方だけ黙って古くなる。`fmt_time` は呼び出し側から注入する（`api` を import すると循環する）。**watchdog 本体を API から import できない**のが切り出しの理由——あちらは import 時に `load_dotenv()` と `FINAPP_DB_TARGET=local` を実行するので、Render（prod）の接続先を書き換えかねない（#508 と同型で、接続先の食い違いは例外を出さず静かに別の DB を読む）。閾値は `run_*.WINDOW_MIN` から導出し**書き写さない**

種別: ユーティリティ ／ 依存先: scripts/run_nightly.py, scripts/run_monthly.py

## `macro_beta_inference.py`

ADR-0002「per-stock 階層マクロβ」の推論バッチ（`requirements-inference.txt` 対応）。全体→セクター→銘柄の二層フルベイズ階層モデル（PyMC・NUTS・non-centered パラメータ化）を `build_panel`（`plugins/macro_snapshots.py` のデータ経路を再利用）→`select_shared_factors`（pooled BIC・`select_features_bic` 共有）→`build_hierarchical_model`→`persist` で実行し、`macro_beta_loadings`/`macro_beta_meta` へ永続化する。収束診断（r_hat/ESS/発散数）は hyperparams に保存。`plugins/macro_risk_return.py`（producer/consumer）から参照。**起動は `scripts/run_monthly_beta.py`**（#579 で月次本体から分離＝実測 360〜419分で本体の窓に入らない。#503 で GHA cron を停止し、#504 で `macro-beta-inference.yml` ごと削除した）。NUTS は数時間かかるうえ `progressbar=False` なので、`_heartbeat()` が `HEARTBEAT_SEC`（5分）間隔で経過をログへ刻む——**無音のままだと「順調に長い」と「死んだ」が区別できない**（#504 で実際に12時間誤認した）

種別: ローカル月次バッチ ／ 依存先: database.py, plugins/macro_snapshots.py, plugins/macro_risk_return.py

## `hyperparameter_search.py`

M-1/M-2/M-3 ハイパーパラメータ自動探索（ADR-0007・Issue #264/#291/#292/#293）。CLI（`--model`＋`--persist`/`--persist-scores`）と月次バッチ（`scripts/run_monthly.py` の `tune:*` 3本）が共有ロジック `run_search()` を呼ぶ（GUIからの手動トリガーはIssue #293で廃止・#292へ一本化）。**`tune-hyperparameters.yml` は #503 で cron を停止し #504 で削除**＝現在の駆動はローカルのタスクスケジューラ経由（M-3 は月次本体、M-1 は `run_monthly_m1.py`・#584、M-2 は日中枠の探索の周回・#1118）。`tuning_search_space()` を `plugins/tuning.py::search()` で評価し、`persist=True` で best params を `plugin_tuned_params` へ永続化、`persist_scores=True` 併用で最終 `execute_plugin` を1回実行し producer スコアを実反映する。**品質ゲート（Issue #291 → #590 で作り直し・ADR-0047）**: 目的（人手レビュー無しの月次自動実行で本番値を悪化させない）は同じだが、**手段が保存値との比較から候補プールへの champion 投入に変わった**。永続化済みの `objective_value` は「そのとき存在したパネルでの値」で、パネルは毎晩伸びるため月をまたいだ単純比較が成立しない（実測: 0.5068=10 fold / 0.0221=55 fold＝**fold が少ない候補ほど高く出る**）＝一度たまたま高い値が入ると永久に閉じる（`macro_gbdt` が 2026-07-19 で固着した）。いまは `persist=True` 時に既存行の `params` を `search(champion_params=...)` へ渡して**同一パネル上で**測り直し、`best >= champion` が構造的に成立するので persist は常に行い CLI は **exit 0**。水準の移動は WARNING ログと `plugin_tuned_params` の `prev_objective_value` / `champion_objective_value` / `n_periods` / `n_oof_samples` に残す。champion が現在の探索空間の外なら投入しない（`dims` を狭めるのは退役の手段でもあるため）。真因（M-1/M-2 の rank-IC 水準）の追及は #592。**`--cycle`（#1114）は探索の周回の 1 日ぶん**（`run_cycle`）: 固定の候補集合を「決勝候補＋次の束」ずつ締切まで測り、束の段では本番の表へ何も書かない（書くのは周回の状態だけ）。回し切って確かめ直す段が残り時間に入れば同じ日に続け、`run_search(candidates=決勝候補, checkpoint=False)` で**1 回だけ**永続化する（段が入らなければ次の日）。規則は `tuning_cycle.py`。新規 pip 依存なし（`requirements-inference.txt` 分離は不要）

種別: ローカル月次バッチ / ユーティリティ ／ 依存先: database.py, plugins/tuning.py, tuning_cycle.py

### producer/consumer の補助テーブルと `plugin_tuned_params` の射影

（[ARCHITECTURE.md](ARCHITECTURE.md) §3 の前置きから移した・#1300）

**producer/consumer 系の補助テーブル**: `macro_beta_meta`/`macro_beta_loadings`（ADR-0002・
`macro_beta_inference.py` が月次バッチで書き込み、`plugins/macro_risk_return.py` が読む）、
`recommend_factor_premia`（ADR-0008・`recommend_factor_premia.py` が書き込み、
`plugins/recommend.py::resolve_weights()` が「統計的最適化」プリセットとして読む）、
`plugin_tuned_params`（ADR-0007・`hyperparameter_search.py` が書き込み、
`GET /api/plugins/{name}/tuned` が読む。**保存値は「そのとき探索した空間」の記録**なので、
読む側が `project_tuned_params()` で現在の探索空間へ射影してから返す——`base_params` の
キーはその値で上書き、dims にある軸は保存値のまま、どちらにも無いキーは落として
`params_schema` の既定へ倒す。生値は `params_as_tuned`、扱いが変わったキーは
`stale_params` に載せ、画面が注記として出す。**射影が無いと、探索から外した軸の古い値が
「🔧 自動調整済み」として推奨され続ける**＝M-1 のモメンタムが実際にそうなっていた。
**射影が見るのは探索空間の形だけで、パネルの世代は別の目で見る**（#711・ADR-0047）——
`current_panel_fingerprint()` が `hyperparameter_search._data_fingerprint()`（規則の唯一の源・
TTL 60秒のキャッシュで1ページ読込3回を1回へまとめる）を呼び、保存時の `data_fingerprint`
と比べた結果を `panel_changed`（True=違う / False=同じ / None=判定不能）として返す。
画面は **False と積極的に言えたときだけ**自動プリフィルする＝指紋を持たない古い行も、
指紋を読めなかった回も、自動適用しない。**パネルは毎晩伸びるので自動適用は事実上ほぼ
止まる**が、バッジと「調整済みの値に戻す」ボタンは残るので手動適用の導線は生きている。
これが無いと、いま探索中の軸に残った古い値が素通りする——M-1 の `max_features=5`
（2026-09-02・分割補正前のパネル）は rank-IC +0.0052・fold 間 std 0（予測値が月内で
全銘柄同じ）だったのにプリフィルされ続けた。その後 #791 で `max_features` を探索軸から
外して `base_params` に本番既定 20 を固定したので、この値は今は射影が 20 へ倒す
（軸が無くなった M-1 の探索は μ̂ の永続化のための1候補で完走する）。
ADR-0050・#604）は、いずれも「重い推論/探索バッチが結果だけを
永続化し、軽量なAPI/プラグインが読む」producer/consumer 分離パターン（`regression_results`
と同型）。`statement_disclosure`（Issue #322・`collector_disclosures.py` が J-Quants

## `nightly_scores.py`

夜間スコア更新バッチ（Issue #432/#443・親 #423）。**起動は `scripts/run_nightly.py` の `scores` ステップ**（#503 で `daily-incremental` の cron が止まり、`nightly-scores.yml` の `workflow_run` チェーンは親ごと発火しなくなった＝手動起動の口だけ残る）。`NIGHTLY_MODELS`（`sector_ols` → `regression_results` の `predicted_market_cap`/`gap_ratio`、`macro_enet`＝M-6 → `macro_enet_scores` の μ̂ と、同じ実行が解く `sell_quantile_scores` の S2＝`sell_ranking` の既定の並びのキー・ADR-0076）を `plugins.execute_plugin` 経由で順に回す。`NIGHTLY_PARAMS` は `params_schema` の default から変えるキーだけを持つ（`sector_ols` は `regularization=ridge`／M-6 はエントリ無し＝ADR-0021/0022 の実測と同一の既定構成で回す）。**1モデルの失敗は他を巻き込まず**、全実行後に失敗があれば非ゼロ終了（→ `notify-failure.yml` が Issue 起票）。実行ごとに `VERIFIERS` が DB を直接クエリし、プロセス開始時刻より新しい書き込みがあることを確認する（例外なし＝コミット済みとしない）。**producer が失敗しても止まらない作りで一緒に書く表は `SIDE_OUTPUT_CHECKS` が点検する**（#1237）＝M-6 の S2 は、応答の学習・保存の失敗・結果の欠落・M-6 の表との行数や基準日の食い違いを非ゼロ終了の理由 `macro_enet:sell_quantile` にする（μ̂ は巻き戻さない・全社同値の晩は失敗にしない）。μ̂ テーブル（`*_scores`）は M-2/M-3/M-4/M-6 で列構成が同型かつ全置換のため、verifier は `_make_score_table_verifier` ファクトリ1本で足りる。全モデルを `shared_snapshot_cache()` で包み、`load_data`（週次127万行）/`preload_macro`/`build_snapshots` をモデル間で共有する（包まないと Supabase Egress がモデル数に比例・#443）。**M-2 は非搭載**（既定 mu_source ではなく、月次 tune の `--persist-scores` 経路が μ̂ を更新するため）、**M-4 も非搭載**（基底を全部回すのに M-6 単体を上回らない・ADR-0022）。あわせて **`HEAVY_AUTOMATION`＝`heavy=True` なプラグインの自動実行レジストリ**（ADR-0031・#423 子6）を持つ。値はワークフローファイル名か `exempt: <理由>` で、未登録の heavy・実在しないワークフロー名・そのモデルを回さないワークフロー・空理由の exempt は `tests/test_nightly_scores.py::TestHeavyAutomationRegistry` が CI で落とす（「heavy を足したが自動実行が無い」は failure にならず `notify-failure` で拾えないため。#432/#443/#423 子5 で3回発生）。**登録があること ≠ 動いていること**で、鮮度監視は `/api/morning` の as-of・夜間バッチの `macro_health` ステップ・**成果物側は `batch_freshness.PRODUCERS`（#504）**が担う。**計算した診断値は捨てずに `nightly_model_diagnostics` へ積む**（#726・ADR-0061）＝`DIAG_EXTRACTORS` がモデルごとの allowlist で抜き出し、`_json_safe` で NaN を None にして書く。文脈として git HEAD / dirty（夜間は作業ツリーをそのまま import する）と `PREPROCESS_VERSION` を添える。ridge α が候補の**上端**に張り付いた夜（と M-6 の最終 α がパスの端の夜）は WARN を出すだけで起票しない（**ridge の下端は INFO**＝罰なしと同等で対処できない・#728 の実測）。書き込みの失敗は PRODUCERS では見ない（同じ障害に Issue が2枚立つ）——`NIGHTLY_MODELS` の全部に抽出器があることは `TestDiagnosticsRegistry` が縛る

種別: GitHub Actions / ユーティリティ ／ 依存先: database.py, plugins/__init__.py, plugins/sector_ols.py, plugins/macro_enet.py, plugins/macro_snapshots.py

### `nightly_model_diagnostics` の表

（[ARCHITECTURE.md](ARCHITECTURE.md) §3 の前置きから移した・#1300）

**`nightly_model_diagnostics`（#726・[ADR-0061](adr/0061-nightly-keeps-the-diagnostics-it-already-computes.md)）**: 夜間の producer が毎晩計算して捨てていた診断値（`sector_ols` の業種別 ridge α・R²・VIF 警告、`macro_enet` の CV が選んだ α / l1_ratio と α パスの端・OOF 成績・係数）を run_id × model で1行ずつ**追記**する。書き手は `nightly_scores.py` だけで、μ̂ / gap_ratio の検証が済んだ後に書き、直接クエリで確かめる（書けなければ `<model>:diagnostics` で非ゼロ終了）。入れるのは `DIAG_EXTRACTORS` の allowlist だけ（社別の行・社名は入れない）。**本番のスコアも起票もこれを読まない**——読み手は `python -m scripts.nightly_diag_report` だけ。

## `prediction_snapshots.py`

**本番予測の月次の写し**（#1005・親 #1001）。`nightly_scores.py` が全モデルの後に `snapshot_month` を呼び、当月分が無い源（全 mu_source と sector_ols の gap と S2・#1208）だけを `prediction_snapshots` へ写す（写すだけ・新しい推定はしない・追記のみ）。空・読めない源は失敗として返し（S2 の空だけは見送り）、接続先がローカルでなければ写さない。手で写すのは `python -m prediction_snapshots`（`--dry-run` で行数だけ）。実測（2026-10-21）: enet 3,169・gbdt 1,704・dlm 3,598・M-1 3,839・gap 3,634 行＝月約 1.6 万行

種別: ユーティリティ ／ 依存先: database.py, plugins/__init__.py, collector_utils.py（JST）

### `prediction_snapshots` の表

（[ARCHITECTURE.md](ARCHITECTURE.md) §3 の前置きから移した・#1300）

**`prediction_snapshots`（#1005・親 #1001）**: 全置換で最新しか持たない `*_scores`（μ̂）と `regression_results`（gap_ratio）を、月に1回その時点の値のまま (写した日 × 源 × 社) で**追記**する。源は永続化している全ての mu_source（`macro_enet` / `macro_gbdt` / `macro_dlm` / `macro_risk_return`）と `sector_ols` の gap と売り専用の候補 S2（源 `sell_quantile`・値は `mu` 列に q20・M-6 と断面の日付がそろう夜だけ写す・#1208）（退役した M-4 は除く）。書き手は `prediction_snapshots.snapshot_month` だけで、夜間バッチ（`nightly_scores.py`）が全モデルの後に呼び、**源ごとに当月分が無いときだけ**写す（初回の夜に失敗しても翌晩に取り直す）。読む入口は画面・売りランキングと同じ（`read_producer_scores`・各社の最新年度の `financial_metrics.gap_ratio`）で、新しい推定はしない。空・読めない源は `prediction_snapshots:<源>` で非ゼロ終了（**S2 の空だけは見送り**＝予測が全社同値の夜は表が空で全置換されるので失敗にせず、翌晩また試す・`EMPTY_IS_NORMAL`）、接続先がローカルでなければ写さない（行はローカル正本だけ）。表は `init_db()` を待たず書き込み側でも `checkfirst` で作る。手で写すときは `python -m prediction_snapshots`（`--dry-run` で行数だけ）。前進は `batch_freshness.PRODUCERS`（源ごとの最新の写しの最古・月の最長31日＋夜間の窓）が見る（見る源は `prediction_snapshots.WATCHED_SOURCES`＝S2 を除く。仕様どおり空の月に起票しないため・#1208）。答え合わせは `scripts/measure_live_oos.py`（#1157・読むだけ）で、52 週の起点は写した日（`data_asof` ではない）。

## `macro_health.py`

マクロ系列の鮮度判定（Issue #420）。`collector_prices` の系列定義（`MACRO_SERIES` ほか9グループ）から収集対象を生成し、`macro_data` の系列別 `max(trade_date)` を許容遅延と突き合わせて `stale` / `missing` / `future` を返す。許容遅延は2階建て（ADR-0028）＝**系列個別の `stale_days` が本命**、`FREQ_STALE_DAYS`（daily 14 / monthly 105 / quarterly 210 日等）は指定の無い系列を拾う粗い網。`future` は `trade_date` が基準日より先＝`lag_days` が実配信ラグを超えた状態で、数日は warn・観測周期超過で CRITICAL（#447）。`critical_series_codes()` が M-1/M-2/M-6（`macro_snapshots.DEFAULT_MACRO_FEATURES`）と M-3（`macro_dlm.DEFAULT_MACRO_FEATURES`）から逆引きするため、**昇格ゲートで棄却済みの系列は自動的に critical から外れる**。除外は `EXCLUDED_SERIES`（理由必須・レポートには必ず出す）

種別: 収集 / GitHub Actions ／ 依存先: database.py, collector_prices.py, plugins/macro_snapshots.py, plugins/macro_dlm.py

## `recommend_factor_premia.py`

recommend の Fama-MacBeth 断面回帰CLI（ADR-0008・Issue #271）。月末スナップショットごとの横断面OLS（`build_period_panel`が`plugins/macro_snapshots.py::build_snapshots`を無改修で再利用・M-1/M-2/M-3と目的変数/母集団/fold共有）→係数の時系列平均をNewey-West（HAC）標準誤差付きで算出（`fama_macbeth_regression`）→`--persist`で`recommend_factor_premia`テーブルへ永続化。第1段階は **両 estimator とも期内 winsorize→標準化してから解く**（`fit_feature_columns`・#509。係数の単位は「1sd あたり」）。`build_period_panel(with_gap_ratio=True)` は時点再現の gap_ratio（`sector_gap_asof`・ADR-0057）を末尾の列に足す評価専用の切替（既定 False＝月次の `--persist` は gap なしのまま・TTM 行の基準の内側では拒否）。`--estimator ols|ridge` で切替可（既定 ols。ridge は L2 の寄与を測る比較・診断用で `--persist` と併用不可＝昇格ゲート未通過の重みがプリセットへ入るのを防ぐ・#469/ADR-0028）。`plugins/recommend.py::resolve_weights()`が読み「統計的最適化」プリセットとして提供（**`preprocess_version` が一致する行だけ**＝旧単位の重みが新単位の特徴量へ黙って掛かるのを構造的に止める・#517/ADR-0039）。**実行は `scripts/run_monthly.py` の `factor_premia` ステップ**（#503 で cron を停止し #504 で `recommend-factor-premia.yml` を削除。CLI 既定との一致は `tests/test_run_monthly.py` が引き継いだ）。実測 2.6分と軽い

種別: ローカル月次バッチ / ユーティリティ ／ 依存先: database.py, plugins/macro_snapshots.py, plugins/recommend.py

## `.github/workflows/ci.yml`

push/PR で `ruff check`（F 系＝未定義名・import 漏れ。ルールは `ruff.toml`・除外は理由と Issue 付きの登録表を `tests/test_ci_lint.py` が照合・#724）→ `pytest --durations=20`（testpaths=tests）を実行する CI。`timeout-minutes: 15` はハング止め（#703）——ランナー準備の空白（実測最大 114秒）も算入される。1本の call が 60秒を超えたテストは `tests/conftest.py` が失敗にし、意図的に重いテストは `@pytest.mark.slow(reason=...)` で外す（空理由は収集時に使用エラー）。経緯は [GOTCHAS.md](GOTCHAS.md)「テスト・CI」

種別: GitHub Actions ／ 依存先: —

## `.github/workflows/daily-incremental.yml`

差分収集。`_pipeline_incremental.py` を起動。**schedule は #503（ADR-0038）で恒久停止**＝正本がローカル PostgreSQL へ移り、動かすと Supabase だけが前進して分岐する（停止の発端は #477 の Egress 超過だが、#503 の反転で復帰の前提が消えた。**復帰条件は「正本を Supabase へ戻す決定をしたとき」だけ**で、その場合は #503 を再オープンしローカル→Supabase の引き渡し経路を先に用意する）。停止前の cron は **JST 17:17**＝UTC 08:17（#476 で JST 03:00 から前倒し）＝**起動時刻の根拠としてだけ残す値**。`workflow_dispatch` は残るが**手動起動すると Supabase へ書く**

種別: GitHub Actions（**手動のみ**） ／ 依存先: `_pipeline_incremental.py`

## `.github/workflows/nightly-scores.yml`

夜間スコア更新＝`sector_ols` + M-6（Issue #432/#443・親 #423）。`daily-incremental` が **success** で終わった直後の `workflow_run` チェーンで起動し `nightly_scores.py` を実行（＋`workflow_dispatch`）。**親の cron が #503 で停止したためチェーンは発火しない**（現在の駆動は `scripts/run_nightly.py` の `scores` ステップ）。`workflows:` は `["**"]` 固定で対象は job の `if` の名前一致（`[定常]` の角括弧はフィルタパターンとして壊れる）。**リポジトリで唯一 `actions/cache` を使う**（週次株価の run 間キャッシュ・#480／key は run_id でユニーク＋`restore-keys` 前方一致／save は `if: always()`）。Egress 台帳の JSONL（`.egress/*.jsonl`）も artifact に含める（**全ワークフロー共通**・#478 の穴1）

種別: GitHub Actions ／ 依存先: `nightly_scores.py`, `weekly_price_cache.py`

## `.github/workflows/egress-health.yml`

Supabase 枠消費ゲート（Issue #478/#483・[ADR-0037](adr/0037-egress-cycle-budget-is-a-second-axis.md)）。**毎日 UTC 21:00 の cron** で `python -m scripts.check_egress_health` を実行（＋`workflow_dispatch`）。**`workflow_run` チェーンにしない**のは、Egress がワークフローの成否と無関係に積み上がりローカル CLI からも積まれるため。閾値超過で exit 2 → `notify-failure.yml` が Issue 起票

種別: GitHub Actions ／ 依存先: `scripts/check_egress_health.py`

## `.github/workflows/dependency-audit.yml`

pin した依存の脆弱性照合（Issue #723）。**毎週 UTC 22:23・日の cron**＋`requirements*.txt` を変える PR / main push＋`workflow_dispatch`。`requirements*.txt` を **glob で全本**拾って `pip-audit --strict -r …` に渡し（推移依存も解決して照合）、既知の脆弱性があれば failure → `notify-failure.yml` が Issue 起票（このジョブは `contents: read` のみ）。除外は run の `ignores` 配列に理由と Issue 番号付きで。不変条件は `tests/test_dependency_audit.py`、運用は [DEPLOYMENT.md](DEPLOYMENT.md) の同名節

種別: GitHub Actions ／ 依存先: `requirements-dev.txt`（pip-audit の pin）

## `.github/workflows/bench-macro-beta.yml`

Issue #512 の**基準側**（Linux ランナーでの macro_beta 所要）を取る計測ジョブ（workflow_dispatch 専用・定期実行なし）。`scripts/bench_macro_beta.py --mode synth` を回す＝**DB も secrets も使わない**ので Egress ゼロ・`FINAPP_DB_TARGET` 非依存（実データを使うと #503 反転後は Supabase の 2026-08-07 断面とローカル正本で規模が違い、比が無意味になる）。結果 JSONL をアーティファクトで持ち帰り、ローカル実行値と突き合わせる。**#540 で `max_tree_depth` / `probe_draws` 入力を追加**＝軌道長の格子を Linux 側でも取り、ローカル実データとの**順位の一致**で結論がプラットフォームに依らないことを確かめる（幾何が違うので絶対値は比較しない）

種別: GitHub Actions ／ 依存先: `scripts/bench_macro_beta.py`

## `.github/workflows/notify-failure.yml`

他ワークフローの `failure`／`cancelled` を Issue へ自動起票（`workflow_run`・Issue #414）。`workflows:` は列挙せず全ワークフロー対象（列挙するとフィルタパターン扱いで `[定常] …` が startup_failure を起こす）、`ci.yml` のみ job の `if` で除外し、fork の PR で走った run も数えない（`head_repository.full_name == github.repository`）。同一タイトルかつ bot が立てた open Issue があればコメント追記。`cancelled` は job の annotation で timeout 打ち切り（起票する）と手動キャンセル（起票しない・#453）を振り分ける＝denylist で、判別できないものは起票側へ倒す。不変条件は `tests/test_workflow_failure_notification.py` が強制

種別: GitHub Actions ／ 依存先: GitHub Issues（`gh issue create`／`comment`）
