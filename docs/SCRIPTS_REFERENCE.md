# SCRIPTS_REFERENCE.md — スクリプト実装リファレンス

> **位置づけ**: [ARCHITECTURE.md](ARCHITECTURE.md) §10「ファイル役割一覧」から `scripts/` 配下とリポジトリ直下の起動用 `*.ps1` の詳細説明を分離した実装リファレンス（#843。1セルが 1,000〜3,000 字まで育ち、ファイル一覧としての索引性を失っていた。`plugins/` を [PLUGIN_REFERENCE.md](PLUGIN_REFERENCE.md) へ分けたのと同じ形）。
>
> | 知りたいこと | 参照先 |
> |---|---|
> | ファイル一覧としての索引・処理フロー・ER図 | [ARCHITECTURE.md](ARCHITECTURE.md) §10 |
> | 各スクリプトの役割・内部仕様・実測値 | **本書** |
> | バッチの起動日・時刻・窓・所要（暦） | [DEPLOYMENT.md](DEPLOYMENT.md) |
> | 叩くコマンド | [batch-ops スキル](../.claude/skills/batch-ops/SKILL.md) |
> | 再現条件と回避手順 | [GOTCHAS.md](GOTCHAS.md) |
> | 設計判断の経緯（ADR） | [adr/README.md](adr/README.md) |

**スクリプトを足したら、ここへ節を1つ、ARCHITECTURE.md §10 へ1行（リンク付き）を足す。** 本文は移設時（2026-10-08）の §10 のセルをそのまま移したもので、GOTCHAS・ADR と重なる経緯が残っている（整理は #856）。

---

## 索引

| ファイル | 種別 |
|---|---|
| [`scripts/setup_local_db.py`](#scriptssetup_local_dbpy) | ユーティリティ |
| [`scripts/mirror_common.py`](#scriptsmirror_commonpy) | ユーティリティ |
| [`scripts/mirror_pull.py`](#scriptsmirror_pullpy) | ユーティリティ |
| [`scripts/mirror_sync.py`](#scriptsmirror_syncpy) | ユーティリティ |
| [`scripts/mirror_verify.py`](#scriptsmirror_verifypy) | ユーティリティ |
| [`scripts/mirror_rehearse.py`](#scriptsmirror_rehearsepy) | ユーティリティ |
| [`scripts/egress_report.py`](#scriptsegress_reportpy) | ユーティリティ |
| [`scripts/_cache.py`](#scripts_cachepy) | ユーティリティ |
| [`scripts/event_study_disclosure_surprise.py`](#scriptsevent_study_disclosure_surprisepy) | 検証（実験） |
| [`scripts/event_study_multivariate_xgboost.py`](#scriptsevent_study_multivariate_xgboostpy) | 検証（実験） |
| [`scripts/measure_embargo_impact.py`](#scriptsmeasure_embargo_impactpy) | 検証（実験） |
| [`scripts/measure_strict_binding.py`](#scriptsmeasure_strict_bindingpy) | 検証（実験） |
| [`scripts/measure_sector_coverage.py`](#scriptsmeasure_sector_coveragepy) | 検証（実験） |
| [`scripts/measure_ridge_alpha_stability.py`](#scriptsmeasure_ridge_alpha_stabilitypy) | 検証（実験） |
| [`scripts/measure_split_valuation_bias.py`](#scriptsmeasure_split_valuation_biaspy) | 検証（実験） |
| [`scripts/measure_split_bias_oof.py`](#scriptsmeasure_split_bias_oofpy) | 検証（実験） |
| [`scripts/measure_split_leak.py`](#scriptsmeasure_split_leakpy) | 検証（実験） |
| [`scripts/measure_industry_fill_impact.py`](#scriptsmeasure_industry_fill_impactpy) | 検証（実験） |
| [`scripts/candidate_bakeoff.py`](#scriptscandidate_bakeoffpy) | 検証（実験） |
| [`scripts/ensemble_base_bakeoff.py`](#scriptsensemble_base_bakeoffpy) | 検証（実験） |
| [`scripts/macro_feature_bakeoff.py`](#scriptsmacro_feature_bakeoffpy) | 検証（実験） |
| [`scripts/momentum_gate.py`](#scriptsmomentum_gatepy) | 検証（実験） |
| [`scripts/model_comparison_run.py`](#scriptsmodel_comparison_runpy) | 検証 |
| [`scripts/macro_dlm_feature_bakeoff.py`](#scriptsmacro_dlm_feature_bakeoffpy) | 検証（実験） |
| [`scripts/preset_ic_gate.py`](#scriptspreset_ic_gatepy) | 検証（実験） |
| [`scripts/preset_weight_walkforward.py`](#scriptspreset_weight_walkforwardpy) | 検証（実験） |
| [`scripts/sell_mu_source_bakeoff.py`](#scriptssell_mu_source_bakeoffpy) | 検証（実験） |
| [`scripts/experiment_pooled_rhat.py`](#scriptsexperiment_pooled_rhatpy) | 検証（実験） |
| [`scripts/bench_macro_beta.py`](#scriptsbench_macro_betapy) | 検証（計測） |
| [`scripts/bench_macro_beta_report.py`](#scriptsbench_macro_beta_reportpy) | 検証（計測） |
| [`scripts/grid_macro_beta.py`](#scriptsgrid_macro_betapy) | 検証（計測） |
| [`scripts/macro_beta_gate_history.py`](#scriptsmacro_beta_gate_historypy) | 検証（計測） |
| [`scripts/nightly_diag_report.py`](#scriptsnightly_diag_reportpy) | 検証（計測） |
| [`run_local.ps1`](#run_localps1) | ユーティリティ |
| [`scripts/run_nightly.py`](#scriptsrun_nightlypy) | ユーティリティ |
| [`run_nightly.ps1`](#run_nightlyps1) | ユーティリティ |
| [`scripts/install_nightly_task.ps1`](#scriptsinstall_nightly_taskps1) | ユーティリティ |
| [`scripts/check_batch_freshness.py`](#scriptscheck_batch_freshnesspy) | ユーティリティ |
| [`scripts/resolve_price_suffix.py`](#scriptsresolve_price_suffixpy) | ユーティリティ |
| [`scripts/fix_naive_jst_timestamps.py`](#scriptsfix_naive_jst_timestampspy) | ユーティリティ |
| [`scripts/backfill_adj_factor_events.py`](#scriptsbackfill_adj_factor_eventspy) | ユーティリティ |
| [`scripts/repair_splits_from_jquants.py`](#scriptsrepair_splits_from_jquantspy) | ユーティリティ |
| [`scripts/repair_consolidation_prices.py`](#scriptsrepair_consolidation_pricespy) | ユーティリティ |
| [`scripts/repair_scale_mixture.py`](#scriptsrepair_scale_mixturepy) | ユーティリティ |
| [`scripts/check_nightly_collect.py`](#scriptscheck_nightly_collectpy) | ユーティリティ |
| [`scripts/_textwidth.py`](#scripts_textwidthpy) | ユーティリティ |
| [`run_watchdog.ps1`](#run_watchdogps1) | ユーティリティ |
| [`scripts/install_watchdog_task.ps1`](#scriptsinstall_watchdog_taskps1) | ユーティリティ |
| [`scripts/batch_common.py`](#scriptsbatch_commonpy) | ユーティリティ |
| [`scripts/check_heavy_imports.py`](#scriptscheck_heavy_importspy) | ユーティリティ |
| [`scripts/run_monthly.py`](#scriptsrun_monthlypy) | ユーティリティ |
| [`scripts/run_monthly_beta.py`](#scriptsrun_monthly_betapy) | バッチ |
| [`scripts/run_monthly_m1.py`](#scriptsrun_monthly_m1py) | ユーティリティ |
| [`run_monthly.ps1`](#run_monthlyps1) | ユーティリティ |
| [`scripts/install_monthly_task.ps1`](#scriptsinstall_monthly_taskps1) | ユーティリティ |
| [`run_monthly_beta.ps1`](#run_monthly_betaps1) | ユーティリティ |
| [`run_monthly_m1.ps1`](#run_monthly_m1ps1) | ユーティリティ |
| [`scripts/install_monthly_beta_task.ps1`](#scriptsinstall_monthly_beta_taskps1) | ユーティリティ |
| [`scripts/install_monthly_m1_task.ps1`](#scriptsinstall_monthly_m1_taskps1) | ユーティリティ |
| [`scripts/run_backup.py`](#scriptsrun_backuppy) | バッチ |
| [`run_backup.ps1` / `scripts/install_backup_task.ps1`](#run_backupps1--scriptsinstall_backup_taskps1) | ユーティリティ |
| [`scripts/run_daytime.py`](#scriptsrun_daytimepy) | バッチ |
| [`run_daytime.ps1` / `scripts/install_daytime_task.ps1`](#run_daytimeps1--scriptsinstall_daytime_taskps1) | ユーティリティ |
| [`scripts/backup_push.py`](#scriptsbackup_pushpy) | ユーティリティ |
| [`scripts/backup_restore.py`](#scriptsbackup_restorepy) | ユーティリティ |
| [`scripts/check_macro_health.py`](#scriptscheck_macro_healthpy) | GitHub Actions / ユーティリティ |
| [`scripts/check_egress_health.py`](#scriptscheck_egress_healthpy) | GitHub Actions / ユーティリティ |

---

## `scripts/setup_local_db.py`

ローカル PostgreSQL を本アプリのスキーマで初期化する（Issue #481 B-0・**Supabase へは接続しない**）。`database._is_local` で接続先を検証してから `init_db()` を呼び、全テーブル＋VIEW 3本の生成・`security_invoker` の適用可否・温存した旧日次 OHLCV の行数を検証レポートで出す。既定はドライラン（`--apply` で実行）。旧スキーマの掃除は「素の `stock_price_history` が在る かつ `stock_price_weekly` が無い」をマーカーに**1回だけ**走るので、ミラー投入後に誤実行しても中身を消さない

種別: ユーティリティ ／ 依存先: database.py

## `scripts/mirror_common.py`

ミラー3本の共有基盤（**source/dest を引数で受けるので、両方ローカルなら Supabase 不要で予行できる**・Issue #481 B-2〜B-4・[ADR-0035](adr/0035-mirror-endpoints-are-parameterized.md)）。ミラー範囲（`Base.metadata.sorted_tables` の全表から `MIRROR_EXCLUDED`＝`xbrl_raw_documents` と `ttm_financial_records`（派生・毎晩全置換なので引く必要が無く、TTM を作らない側から全置換で引くとローカルの TTM が全消えする）を除く。`stock_price_daily` は #503 で範囲へ入れた。表の数は表を足すたびに変わるので書かない）を**FK 依存順で導出**、テーブル別の同期方針 `SYNC_PLAN`、`pg_dump`/`pg_restore` の argv 組み立て（純関数・`--strict-names` / `--compress=0` / 表ごと restore）、エンドポイント解決（`database.resolve_database_url()` へ委譲）、**dest ローカル限定ガード**、サーバ側の件数/バイト数/順序非依存チェックサム、`decode_pg_output`（utf-8 → cp932 フォールバック）

種別: ユーティリティ ／ 依存先: database.py, db_egress.py

## `scripts/mirror_pull.py`

正本 → ミラーの一括取り込み（B-2）。列差分プリフライト → `octet_length` 見積り → `egress_budget()` 内で `pg_dump --compress=0` → TRUNCATE（明示列挙・CASCADE 不使用）→ **FK 依存順に1表ずつ `pg_restore`** → シーケンス再同期 → `ANALYZE` → 突合。既定ドライラン、`--apply` ＋ 見積り超過時は `--allow-full-pull` が要る

種別: ユーティリティ ／ 依存先: scripts/mirror_common.py, scripts/mirror_verify.py

## `scripts/mirror_sync.py`

正本 → ミラーの増分同期（B-3）。dest の高水位から `SYNC_PLAN` のオーバーラップぶん遡って取り直し、PK で upsert（`FULL` 指定表は全置換）。週次は `DAILY_WINDOW_DAYS` 由来の27週窓

種別: ユーティリティ ／ 依存先: scripts/mirror_common.py, scripts/mirror_verify.py

## `scripts/mirror_verify.py`

ミラーと正本の突合（B-4）。`--level schema`（`information_schema.columns` の列差分＝pull の事前確認）/ `counts`（既定・`count(*)` と最新キー）/ `checksum`（値レベル）。終了コード 0=一致 / 1=乖離 / 2=接続不可。エンドポイント引数の定義元でもある

種別: ユーティリティ ／ 依存先: scripts/mirror_common.py

## `scripts/mirror_rehearse.py`

ミラー3本のローカル予行演習（**Supabase へ接続しない**）。専用の2 DB を作り合成シードを入れて pull → 突合一致 → 遡及訂正を注入 → 乖離検出 → sync → 再び一致 を回し、`--drop` で DB ごと捨てる（実 `financial_db` に触れない）。CREATEDB 権限が無ければコマンドを表示して停止

種別: ユーティリティ ／ 依存先: scripts/mirror_*.py, scripts/setup_local_db.py

## `scripts/egress_report.py`

`db_egress` の台帳ロールアップ（Issue #478）。JSONL 台帳（既定 `.egress/ledger.jsonl`）と `gh run view --log` を保存したテキストの `[egress] summary` 行を読み、**ジョブ別・テーブル別**に月次で積み上げる。DB へ繋がないので集計自体は Egress を使わない

種別: ユーティリティ ／ 依存先: —

## `scripts/_cache.py`

`scripts/` 検証系の共通 pickle キャッシュ（Issue #355）。本番 Supabase へのフルロード反復が Egress を枯渇させた実例への恒久対策で、同一キーを各スクリプトが共有し `--refresh-cache` で明示無効化する

種別: ユーティリティ ／ 依存先: —

## `scripts/event_study_disclosure_surprise.py`

決算開示サプライズ×フォワードリターンのイベントスタディ（Issue #323 フェーズ0・単一特徴量 m_pm1 で rank-IC 0.031）。本番書込なし・手動実行（`python -m scripts.xxx`）

種別: 検証（実験） ／ 依存先: database.py, feature_disclosure.py, scripts/_cache.py

## `scripts/event_study_multivariate_xgboost.py`

同フェーズ1の多変量 XGBoost 版＋target 相対化 ablation（#337）＋断面/自己履歴特徴量×target 軸マトリクス（#336）。本番書込なし・手動実行

種別: 検証（実験） ／ 依存先: database.py, feature_disclosure.py, scripts/_cache.py

## `scripts/measure_embargo_impact.py`

purge/embargo 導入の IC への影響実測（Issue #363/#375・ADR-0014。旧 IC 0.33 が 52週先ラベルの前方リーク由来と判明した根拠スクリプト）。本番書込なし・手動実行

種別: 検証（実験） ／ 依存先: plugins/macro_snapshots.py, scripts/_cache.py

## `scripts/measure_strict_binding.py`

M-1 strict（`macro_nan_ok=False`）が学習窓を律速しているかの実測診断（ADR-0016 追試・Issue #411）。①既定マクロ各特徴が最初に非 None になる月 ②strict / nan_ok / マクロ無しの3条件でスナップショット母集団（月数・サンプル数）が変わるか ③変わらない場合に窓を決めているのは週次株価か財務か、を出し `VERDICT` 1行にまとめる。`strict IS binding` なら latest-start 特徴を律速候補として列挙し ADR-0016 の手順（既定から外す＋非truncate代替）へ誘導する。`--features` で任意のマクロ集合も診断可。データは `scripts/_cache.py` 経由（Egress ゼロ）・本番書込なし・手動実行

種別: 検証（実験） ／ 依存先: plugins/macro_snapshots.py, scripts/candidate_bakeoff.py, scripts/_cache.py

## `scripts/measure_sector_coverage.py`

`sector_ols` のカバレッジと説明力を設定別に実測する（Issue #434 の昇格ゲート）。`_classify_by_sector` の AND フィルタが「無配で `dps` が NULL」「銀行業に売上総利益が無い」といった**構造的欠損**で企業・業種を丸ごと落としていた（実測 76.0%）ため、案1 `zero_fill_no_dividend`／案2 `sector_missing_rate` の ON/OFF を総当たりし、**対象社数と業種別 R² を並べて出す**（カバレッジが上がっても説明力が落ちるなら採らない、を実測で決めるための道具）。実測値の正本は [MODELS.md](MODELS.md)

種別: 検証（実験） ／ 依存先: plugins/sector_ols.py, plugins/__init__.py (execute_plugin), database.py

## `scripts/measure_ridge_alpha_stability.py`

`sector_ols` の ridge α の候補を、旧7点（1桁刻み）と現行の `RIDGE_ALPHAS`（1桁10点の61点）で**同じ入力（夜間の `NIGHTLY_PARAMS`）に当てて比べる**（#761・[ADR-0064](adr/0064-ridge-alpha-candidates-are-a-fine-log-grid.md)）。谷の平坦さ・LOO 誤差の変化・切替の一度きりの gap の移動・各業種から社を抜いたときの α の跳びと gap の動きを出す。DB に触れない `_prepare_fit`→`_fit_sector` だけを通り書き込まない。旧格子は `plugins.utils.RIDGE_ALPHAS` の一時差し替えで当てる。候補を変えるときはこれで測り直す（ADR-0064 の実測値の正本）

種別: 検証（実験） ／ 依存先: plugins/sector_ols.py, plugins/utils.py, nightly_scores.py

## `scripts/measure_split_valuation_bias.py`

**検出器そのものは企業イベント台帳（`corporate_actions.py`）が唯一の源で、ここはそれを import して測る CLI**（#746）。過去断面の `per`/`pbr`/`div_yield`/`market_cap`/`nc_ratio` が「遡及調整済み株価 ÷ 当時の1株指標」で歪んでいる量を実測する（Issue #653）。分割比は **DB 内在の列だけ**で全期間を復元し、J-Quants 契約窓（2年）に縛られない。検出経路は2本あり、どちらも「候補ゲート1つ ＋ 独立な書き手による交差検証1つ」の形をする——第1経路（`source="shares"`）は `issued_shares` の年次比 × `bs_bps` の逆比、第2経路（`source="bps"`・#656・`--bps-path`）は `bs_bps` の年次比 × `pl_eps` の比。**第2経路の倍率は翌年の `issued_shares` 比から取り、既定 ON**（公式 `AdjFactor` との一致率 0.962・ADR-0055 決定4-3。bps 比を倍率にしていた頃は 0.367）。**第1経路には純資産総額のチェックがある**（`--equity-tol`・既定 1.0＝株数と同じ向きに純資産が2倍を超えて動いたら増資と読んで採らない・#657・決定4-4。`bs_bps ≈ 純資産÷株数` の社では bps の交差検証と同じものを見るので、落とせるのは両者が食い違う社だけ）。`detect` が該当社数・歪み倍率 F のバンド別行数・断面パーセンタイル順位の移動量・株価基準（adjusted/raw）と、`--sweep` で閾値と純資産比の感度を出す。`verify-sample` が公式 `AdjFactor` と陰性対照つきで突合し（公式の日次バーがイベント窓を覆っていない社は `no_official_bars` として分母から外す・#668）、`--census` なら契約窓内の全イベントを突合して偽陽性率（経路・種別ごと）と、純資産比チェックが許容値ごとに消すイベントの突合ステータス（`equity_gate_crosstab`）を出す。`verify-sources` は整合度照合（#751・決定4-10）で増えるイベントを、DB に取り込んだ公式 `AdjFactor` と Yahoo の分割履歴（`v8/finance/chart` の `events=split`）に照らして事前登録した基準1〜3を判定し、照合を切った台帳との差分（既存のイベントが動いていないこと）も出す。整合度照合で認めたイベント（期末後分割）だけは照合窓の右端を期末+90日まで広げ（`--post-period-end-days`・#755・本番の窓は変えない）、1 つの分割を同じ社の 2 つのイベントが一致に数えない。**読み取り専用・本番書込なし**・手動実行

種別: 検証（実験） ／ 依存先: corporate_actions.py, database.py (financial_records / financial_metrics / stock_price_weekly), scripts/repair_splits_from_jquants.py (extract_events / collect_official), collector_prices.py (_learn_jquants_coverage)

## `scripts/measure_split_bias_oof.py`

分割補正の第2経路（#656）を入れる前後で OOF rank-IC を測り比べる（ADR-0055 決定7）。係数表を `bps_path=False` / `True` で往復させ、そのたびに `model_comparison.run_comparison` を呼ぶだけで**測る手続きを持たない**（ADR-0041）。**1プロセスで前後を回すのは、補正前の断面がもう DB に残っていないから**（VIEW が係数表を LEFT JOIN して当てている）。係数表は必ず True の状態で終える。`run_daytime.py` の `JOBS` へ登録し、**#659 で第2経路の既定が ON になったのでキューへ積んだ**（2026-09-12。それまで積まなかったのは、既定 OFF の間は本番に無い設定の rank-IC を測ることになるため）。2026-09-17 の実走（8.6分）は M-6 −0.0190・M-2 −0.0215 で、同じ期どうしでは 17 期中 16 / 15 期で下がった。期別の対応差と符号検定も出し、`--summarize <json>` で保存済みの結果を測り直さずに集計し直せる

種別: 検証（実験） ／ 依存先: corporate_actions.rebuild_split_adjustment_factors, model_comparison.run_comparison

## `scripts/measure_split_leak.py`

ADR-0055 決定7 の直接証拠（#685）。係数表と**同じ入力・同じ関数**（`corporate_actions.build_ledger`）でイベントと F を作り、表との突合が一致しなければ exit 2。層と標本の F は登録表のスピンオフを除いて作る（`split_events_and_factors`・#740＝分割の読みの対象ではない）。学習パネルと同じ定義（形成月＝月末の週・`_find_applicable_fin`・52週先 log リターン）のサンプルを「財務行より後で最も近いイベントまでの年数」で層別し、月内平均を引いた超過リターンを社単位ブートストラップの CI 付きで出す。判定（1年>2年>3年>4年以上 かつ 1年の CI 下限>0）は測る前に #685 へ固定した。2026-09-17 の実走は**単調でなく「読みを捨てる」**（2年 +0.158 が頂点・1年 +0.028）。**この判定は #687 で退役した**——年 Y の行が効く形成日の範囲 `[期末_Y+45日, 期末_{Y+1}+45日)` は年 Y+1 のイベント窓 `(期末_Y−45日, 期末_{Y+1}+45日]` に常に含まれるので、1年層は 100% が「分割が形成月より前か後か分からない」サンプル＝原理的に通らない層を判定に入れていた（`is_separable`・層の表の `separable` 列）。`verdict()` の計算と出力は #685 のまま残し（当時の記録の再現が退役の説明の裏付け）、`retired` を返して毎回 `RETIRED` と理由を出す。実行のたびに再測定の着手条件（公式 `AdjFactor` の日付で「形成月より後」と言い切れる社数 / 100）を数えて出す（`count_datable_future_companies`・事前登録は #690）。**数えるのは `split_1` の層だけ**——公式の日付が新しい情報を足すのはそこだけで、混ぜると閾値を即座に満たしたように見える（実測 全層 177 社 / 11,057 件 vs 1年層 177 社 / 947 件）。2026-09-18 実測で既に **READY**（177 社・形成月 25（2023-08〜2025-08）・月をまたぐ相関を社単位 CI が拾えないのが弱点）。READY のときだけ、数えたのと**同じ述語**（`datable_future_points`＝カウンタと共有）のサンプルを月内平均を引いた後に `split_1_dated` として抜き出し、#687 で測る前に登録した判定（`retry_verdict`: CI 下限>0 かつ平均>`none`）を出す（`measure_retry`・READY でなければ平均を計算しない）。**2026-09-19 の再測定（#690）は KEEP**——947 件 / 177 社で +0.1035・95%CI [+0.0611, +0.1497]（`none` −0.0116）・達成 CI 半幅 0.0443（設計の最小検出効果 0.05 以内）。同じ形成月に限った `none` は参考値で判定に使わない

種別: 検証（実験） ／ 依存先: —

## `scripts/measure_industry_fill_impact.py`

業種の空欄補完（#797）を入れる前と後で、過去年度の分析がどれだけ動くかを**書き込みなしで**測る（[ADR-0065](adr/0065-empty-industry-is-filled-regardless-of-listing.md)）。1つのセッション・1つのトランザクションの中で「補完前を測る → 本番と同じ `_plan_industry_fill` / `_apply_industry_fill` / `_propagate_company_industry` で補完を流す → 補完後を測る → ROLLBACK」とし、正本へは何も書かない。模擬の間（`simulated`）は `db.commit` を flush に、`db.rollback` を「数えて例外」に差し替え、差し替えを迂回する commit は `before_commit` で止める——`run_comparison` は失敗したモデルで rollback するので、**黙って補完が消えると「補完後」が補完前を測る**（例外は握られうるので回数で中断する）。測るのは当日 gap（夜間と同じ設定・完全一致を要求）・業種内Z（前から業種があった社／埋めた社／空のまま残る社の3群）・時点再現の gap パネル（`build_period_panel(with_gap_ratio=True)` の母集団減少率と、中の `build_asof_gaps` の戻りを写し取った行ごとの gap）・M-1/M-2/M-6 の OOF（`model_comparison.run_comparison` そのもの）で、測る手続きは持たない（ADR-0041）。**OOF の rank-IC の符号は採否に使わない**（空の業種は後の廃止の印＝未来情報）。止める条件（当日 gap の変化・母集団の縮小・業種名の種類の変化・模擬の破綻）に当たれば exit 1。`--skip-oof` / `--skip-panel` / `--summarize <json>`

種別: 検証（実験） ／ 依存先: collector_master, model_comparison.run_comparison, recommend_factor_premia.build_period_panel, sector_gap_asof

## `scripts/candidate_bakeoff.py`

兄弟モデル候補の OOF 横並び実測（#372・ADR-0021）。M-2 既定 config（`params_schema` を `coerce_params({})`＝`model_comparison` と同一）でスナップショットを**1回だけ**構築し、`plugins/model_candidates.py` の全候補＋基準線（XGBoost/素OLS）を同一 fold・同一特徴量で `walk_forward_cv_monthly(embargo_months=12)`→`oof_backtest` に通す。`model_stats.paired_ic_significance`（ADR-0018）で各候補と M-2 の rank-IC 差を定常ブートストラップ検定し昇格可否を判定。価格は既存 `weekly_prices_close` キャッシュ、financial_metrics/companies/macro も軽量 namedtuple で `scripts/.cache` へ保存（2回目以降 Egress ゼロ）。`--smoke`/`--stride`/`--pca`/`--json`。本番書込なし・手動実行

種別: 検証（実験） ／ 依存先: plugins/model_candidates.py, plugins/macro_snapshots.py, model_stats.py, scripts/_cache.py

## `scripts/ensemble_base_bakeoff.py`

M-4 の**基底構成**（M-1+M-2 vs M-1+M-2+M-6）の OOF 横並び実測（#397）。`macro_ensemble.BASE_MODELS` を差し替えて M-4 を2回実行し、同一 honest 前提（embargo=12）で rank-IC を比較する。主判定は ADR-0015 の base-on-common（3基底 M-4 vs 同一共通域に制限した各基底）、参考として 2基底 vs 3基底（母集団差を含む）も出す。有意差は `model_stats.paired_ic_significance`（ADR-0018）。データは `scripts/_cache.py` 経由（Egress ゼロ）、`tuning_dry_run()`＋`tuning_objective_only()` で本番 producer 非汚染。`--smoke`/`--refresh-cache`。手動実行

種別: 検証（実験） ／ 依存先: plugins/macro_ensemble.py, scripts/candidate_bakeoff.py, model_stats.py, scripts/_cache.py

## `scripts/macro_feature_bakeoff.py`

新規マクロ系列を `DEFAULT_MACRO_FEATURES` へ昇格すべきかの OOF 実測（昇格ゲート・ADR-0023 で定式化・#404 の EPU 専用スクリプトを #406 で一般化）。同一モデル・同一 fold のまま `build_snapshots` の `macro_names` だけを差し替えた2条件（base / with_cand）をM-2・M-6 × 買い側 rank-IC・売り側 short_side_spread の4検定で比較し Bonferroni α=0.0125 で判定する。strict（`macro_nan_ok=False`）母集団が縮むかも実測（#381 の律速チェック）。候補は `--preset`（`PRESETS`＝`epu` / `attention`）か `--features`。データは `scripts/_cache.py` 経由（Egress ゼロ）・本番書込なし・手動実行

種別: 検証（実験） ／ 依存先: plugins/macro_snapshots.py, scripts/candidate_bakeoff.py, model_stats.py, scripts/_cache.py

## `scripts/momentum_gate.py`

**母集団を動かしうる探索軸を足す前に、共通域で測る昇格ゲート**（`python -m scripts.momentum_gate`・ADR-0045・ADR-0050・#592・#604）。同一モデル・同一 fold のまま軸だけを差し替えた条件を、両条件に共通する (年月, 社) 域へ揃えて比較する（揃えないと縮む側が必ず有利に見える）。モード（窓・マクロ有無・交互作用・列数上限・TTM 行・目的変数の demean・リスク軸）は1回に1つ（#615。一覧と排他の正本は引数の検証部）。`--smoke` の共通域は間引きで壊れるので読まない

種別: 検証（実験） ／ 依存先: plugins/macro_snapshots, model_stats, scripts/candidate_bakeoff

## `scripts/model_comparison_run.py`

**モデル比較（OOF）の CLI 入口**（`python -m scripts.model_comparison_run`）。UI の「モデル比較（OOF）」と同一手続きで `model_comparison.run_comparison()` を呼ぶだけで、測る手続きを持たない（再実装すると本番と別物を測る・ADR-0041 と同型）。`--models` で部分集合、`--json` で保存。`tuning_objective_only()` と `tuning_dry_run()` の中で回すので本番の `macro_*_scores` を上書きしない

種別: 検証 ／ 依存先: model_comparison, model_stats

## `scripts/macro_dlm_feature_bakeoff.py`

`scripts/macro_feature_bakeoff.py` の **M-3（週次 DLM）版**昇格ゲート（#409）。月次スナップショット判定では日次系列のスパイクが落ちるため、同じ作法（ADR-0023）を週次の M-3 へ適用する。`plugins/macro_dlm.py` の `DEFAULT_MACRO_FEATURES` から候補を除いた base と base+候補で `execute` を2回走らせ（`tuning_objective_only()`＋`tuning_dry_run()` で全社スコアリング省略・producer 非汚染）、買い側 rank-IC・売り側 short_side_spread の2検定を Bonferroni α=0.025 で判定。M-3 に strict 母集団の概念は無いため、代わりに OOF サンプル数（非正水準ガードによる週落ち）と実行時間（状態次元 +N のコスト）を base/with_cand で比較する。週次株価は volume 込みキャッシュ `weekly_prices_full_v1`（`weekly_prices_close` とは別キー・px_volz が volume を要求するため）。本番書込なし・手動実行

種別: 検証（実験） ／ 依存先: plugins/macro_dlm.py, plugins/macro_snapshots.py, model_stats.py, scripts/_cache.py

## `scripts/preset_ic_gate.py`

**プリセット/重みの昇格ゲート**（#529・[ADR-0041](adr/0041-preset-weight-gate-has-an-implementation.md)）。ADR-0028 のゲートは #509 と #517 で2回適用されたが**どちらもアドホックで実体が残らなかった**ため、重み dict の集合を入力に取る CLI として実体化した。重みの出所は問わない（`PRESETS` の静的4本／`recommend_factor_premia` の run_id 指定／手書き JSON）。**標準化も合成も消費側の関数をそのまま import する**（標準化は `fit_view_metric_stats` → `standardize_metric`、合成は `weighted_score`＝`RecommendPlugin.execute` と同じ分母・同じ被覆率の除外・#745）。パネルに無い列の重みは被覆率の分母へ入れない（どの社にも無い列は行ごとの欠けではない・測れない重みは下記の比率で別に出す）。パネルは `recommend_factor_premia.build_period_panel` を共有し、`gap_ratio` を持たない（ADR-0008 Decision 1）ぶんは**「測れなかった重み比率」として必ず表に出す**（割安重視 44.4%・既定 20% 超は判定から外す）。パネルの `z_momentum` は生 log return なので本番の `compute_momentum_z` と同じ2関数で揃える（揃えないとバランス型が +0.0210→+0.0238 と動く）。Bonferroni の α と検定数は実際の検定回数から出し、p の下限 `2/(n_boot+1)` に達した行へ `[p at floor]` を付ける。パネルは毎晩伸びるので過去の実測と比べるときは `--until`。`--with-gap-ratio` は時点再現の gap_ratio（`sector_gap_asof`・ADR-0057）を足したパネルで測り、gap の無い社が落ちたぶんの**母集団の減少率と、測る前に固定した載せる基準（有効期間 > 60・減少率の中央値 ≤ 5%）の成否を必ず印字する**（キャッシュキーは gap の有無で分ける）。`--compare-standardization` は同じ重みを是正後/前の両方で測り共通期でペア検定する（#509 型）。キャッシュは `--cache-panel` の opt-in・DB 非書込・手動実行

種別: 検証（実験） ／ 依存先: plugins/recommend.py, recommend_factor_premia.py, model_stats.py, plugins/macro_snapshots.py, scripts/_cache.py

## `scripts/preset_weight_walkforward.py`

**静的プリセットの重みを walk-forward で推定し直す**（#625・#546 の前提1・[ADR-0059](adr/0059-preset-weights-are-re-estimated-walk-forward-keeping-their-order.md)）。`preset_ic_gate` は与えられた重みを測る評価器で推定はしないため、#546 には実行すべきコマンドが無かった。**尺度は rank-IC を直接高める**＝学習期間の月平均 Pearson(スコア, 順位化した52週先リターン) を、月ごとに前計算した列の共分散から `w'c/√(w'Sw)` の閉形式で出し SLSQP で最大化する（学習を二乗誤差にすると評価の順位とずれる＝#615 と同型）。**性格は静的重みの大小順で残す**（使う指標・非負・重い指標は軽い指標以上・同値同士は自由・合計は静的と同じ）。無制約だと4つが同じ重みへ寄る。**標準化は消費側の関数を通す**（`_panel_rows`→`build_view_stats`→`standardize_metric`。列の統計は他の列の重みに依存しないので `score_period` のスコアと一致し、テストが縛る）。**embargo は `LABEL_HORIZON_MONTHS`（12）固定で CLI から変えられない**（下げると先読み）。学習月は暦で切る。パネルは時点再現の gap あり（ADR-0057 の基準を満たさなければ exit 2）。**評価は `preset_ic_gate.ic_series` へ1期だけのパネルとして渡す**（評価を書き直さない）。推定重みと静的重みの OOF rank-IC を同じ月で対にし、`paired_ic_significance`＋Bonferroni（プリセット数）で判定する。**昇格の根拠はこの OOF 比較だけ**——`--weights-out` の最終重みを `preset_ic_gate --weights-json` で測るのは in-sample。未収束の月は静的で埋めず外して数える（埋めると差が 0 に寄って ns 側へ倒れる）。スクリプト自身は `PRESETS` を書き換えない（反映は人が行い、#546 で OOF を通った成長重視だけを反映した・ADR-0059 追記）。DB へは書かず、書き出しは完走後。`run_daytime.JOBS["wf:preset-weights"]` に登録（`parallel_sensitive=False`＝同じ入力から同じ答え）

種別: 検証（実験） ／ 依存先: scripts/preset_ic_gate.py, recommend_factor_premia.build_period_panel, model_stats

## `scripts/sell_mu_source_bakeoff.py`

`sell_ranking` の既定 μ 出所（`mu_source`）を M-2 / M-6 / M-4 から選ぶ実測（#402・ADR-0022）。**`/api/backtest` では測れない**——`source="sell"` は recommend プリセット加重和の符号反転で μ 観点を持たず、producer スコア表は現在時点のスナップショットしか持たないため as-of 再現が look-ahead になる。よって `oof_backtest` の**売り側指標**（`short_side_spread`）で比較する。**買い側 rank-IC とは順位が逆転する**ことの実証でもある。実測値の正本は [MODELS.md](MODELS.md)

種別: 検証（実験） ／ 依存先: scripts/ensemble_base_bakeoff.py, model_stats.py, scripts/_cache.py

## `scripts/experiment_pooled_rhat.py`

r_hat プール仮説の read-only 実験（Issue #341）。`experiment-pooled-rhat.yml` から workflow_dispatch 起動・本番DB非書込

種別: 検証（実験） ／ 依存先: macro_beta_inference.py

## `scripts/bench_macro_beta.py`

`macro_beta` の所要を段階別に測る計測ハーネス（Issue #512「ローカルが GHA の6.4倍以上遅い」の切り分け）。`--mode synth` は**決定的な合成パネル**（seed 固定）で DB を一切触らず、ローカルと GHA で同一データ・同一事後幾何になる＝A/B の比が規模やデータ世代で汚れない。`--mode real` は本番パネルを間引いて本番幾何の steps/draw を測りフル規模へ外挿する。`draws` を2点振って**固定費（コンパイル＋warmup）と 1 draw の限界費**を分離し、`sample_stats` から leapfrog 歩数・max treedepth 到達率・発散を取る。**計測前に probe を1本流す**のが要点＝JAX はコンパイル済みカーネルを使い回すため、probe 無しだと2点目が速くなり傾きが負に出る（実測）。環境指紋（jax 版・デバイス数・x64・`pytensor.cxx`・`XLA_FLAGS`）を JSONL へ残す。**#540 で統計効率も測る**: `--max-tree-depth`（`8` / `8,10`＝warmup だけ切る）で軌道長を振り、`beta` の ESS_bulk（min/p10/median）と r_hat を**生値**で取る（`beta` は #541 で posterior に載らないため自由 RV から再構成＝本番ゲートと同じ量）。主指標は **ESS/leapfrog 歩**＝時間を含まないのでローカルの時間帯ドリフト（2.4倍）に汚されない。`--panel-stamp` で real パネルの世代を固定できる（日を跨ぐ格子で全セルへ同一パネルを見せる）。**#600 で極値の位置も出す**: `ess_bulk_argmin` / `r_hat_argmax` が「その値を出している母数」（変数名・銘柄・因子・EDINET コード）を持つ——`alpha` か `beta` かで対策が変わるのに、値だけでは分からず本番を6.7時間回し直す羽目になっていた。位置の解決は `macro_beta_inference.locate_extreme` を本番と共有する。**#664 で `--panel-seed` を分けた**: 合成パネルの生成・real の間引きに使う seed で、未指定なら `--seed` と同じ（既存の使い方は不変）。本番の run 間差は「同じデータで chain の乱数だけが違う」2回の差なので、それに対応する反復（パネル固定・sampler seed だけ振る）を作るために要る

種別: 検証（計測） ／ 依存先: macro_beta_inference.py, scripts/experiment_pooled_rhat.py, scripts/_cache.py

## `scripts/bench_macro_beta_report.py`

`bench_macro_beta.py` が吐いた JSONL（ローカル実行と GHA アーティファクトの両方）を1枚の表へ畳む。ビューは2つ: `--view cost`（既定・#512）は**総 leapfrog 歩数に対する回帰**で 1歩の実費を出し観測1件あたりへ正規化する＝銘柄数・tune・チェーン数が違う run を横に並べられる（`--draws` 2点以上が要る）。`--view ess`（#540）は軌道長の格子を統計効率で並べる（`ESS/1e6step` が主指標・`ESS/sec` が従指標）。`--view scale`（#664）は収束ゲートの量（変数別 `r_hat` p99）を銘柄数に対して並べ、銘柄数ごとの seed 間の幅（本番の run 間差 0.0146 と並べる）と `p99 ~ ln(n_stock)` の傾き・95%CI を出す。**判定規則は事前固定**（CI が 0 をまたげば NOT DETECTED・下端が正なら INCREASING）で、発散した run は傾きから外して印を付ける（`macro_beta_gate_history` と同じ扱い）。`mu_universe` は規模に依らず12個なので対照になる（そこに傾きが出れば順序統計ではなく事後幾何の変化）。条件（tune・draws・軌道長等）の違う record は別の節にして1本の傾きへ混ぜない。合否は `mb.gate_verdict` を import（書き写さない）。**表の実装はここ1箇所**＝`grid_macro_beta.py --report-only` もここへ委譲する

種別: 検証（計測） ／ 依存先: scripts/bench_macro_beta.py

## `scripts/grid_macro_beta.py`

NUTS 軌道長（`max_tree_depth`）× `target_accept` の格子ドライバ（Issue #540）。**1セル1プロセス**で `bench_macro_beta` を subprocess 起動し、**安い順**に回して JSONL へ追記する（途中で kill されてもそこまでが残る／JAX 状態がセル間で混ざらない／窓が切れたとき失うのは高いセルだけ）。全セルへ**同一の `--panel-stamp`** を配り、`--probe-draws 0`（1点測定に probe は不要で warmup 1本ぶんの純損）。`--dry-run` でセル一覧と所要見積り、`--report-only` で生値の表。**測定をアドホックなスクリプトで終わらせない**ための実体（ADR-0041 と同じ作法）。**#664 で銘柄数 × seed の軸を足した**: `--n-stock` / `--seed` は複数値を取り（ラベルには振った軸だけを付ける＝1値なら従来ラベルのまま）、`--panel-seed` で全セルのパネルを固定する。**締切の手前で畳む**＝日中枠が渡す `FINAPP_STEP_DEADLINE_UTC`（`hyperparameter_search.resolve_deadline` を再利用）に、見積り ×1.25 ＋2分が入らないセルは始めない（bench は完走して初めて JSONL を書くので、殺されたセルは何も残らない）。1セルも回せずに残りがある回は exit 3（毎日何も進まないのに成功、にしない）。**`--resume` は JSONL の record の条件で照合する**（ラベルではない・ESS の無い record は済みにしない）ので、積み直せば続きから回り、全セル済みなら即 exit 0。所要見積りは同じ銘柄数の実測があればその最大値、無ければ `(n/250)^1.6` の見積りに実測で較正した倍率を掛ける。`--view scale` で規模の表。日中枠の `bench:rhat-scale` がこの形で回す

種別: 検証（計測） ／ 依存先: scripts/bench_macro_beta.py, scripts/bench_macro_beta_report.py, hyperparameter_search.py

## `scripts/macro_beta_gate_history.py`

収束ゲートの余裕を **run 横断**で読む読み取り専用 CLI（Issue #612）。`macro_beta_meta.hyperparams.diagnostics.by_param` を古い順に並べ、変数別の `r_hat` p99・閾値までの余裕・`gate_verdict` の合否・`n_stock`・`n_divergences` を表へ出す。**判定は `macro_beta_inference` の `gate_values` / `gate_verdict` / `MONTHLY_RHAT_THRESHOLD` / `PERSIST_MARGIN_WARN` を import して共有**し、p99 の比較を書き写さない（本番と別基準の表を見ても設定を選べない＝#613 と同じ作法）。**`n_divergences > 0` の run と `by_param` を持たない旧 run には印を付ける**——前者を余裕の推移へ混ぜると「規模とともに縮んでいる」と誤読し（2026-09-07 の並走回は発散344回で `alpha` の p99 が 1.1843 まで悪化したが、並走を止めたら 0 に戻った）、後者は `r_hat_max` が#356 の丸め値なので生値と並べられない。集計は純関数 `summarize_runs` に切り出してテスト可能にし、出力は **ASCII の区切りだけ**（cp932 の標準出力でリダイレクト時に落ちないため）。**余裕の推移を溜める手段が無かったため、#612 では健全な2点を比べるだけで毎回 DB を手で引き直していた**

種別: 検証（計測） ／ 依存先: macro_beta_inference.py, database.py (macro_beta_meta), scripts/_textwidth.py

## `scripts/nightly_diag_report.py`

夜間 producer の診断値を**夜をまたいで**読む読み取り専用 CLI（#726・[ADR-0061](adr/0061-nightly-keeps-the-diagnostics-it-already-computes.md)）。`--view timeline`（既定）は追っている値（sector_ols: 業種別 α・端の業種数／macro_enet: α・l1_ratio・非ゼロ係数数・OOF rank-IC など）が**前の夜から動いた夜だけ**を出し、同時に何が変わっていたか（`code` / `code?`＝unknown か dirty / `preprocess` / `data`＝断面日か件数）を添える。**どれも変わらずに動いた夜は `no-context-change`**＝#697 型の並び依存の候補（入力ハッシュは持たないので断定ではない）。`--view edges` は α が候補の端に張り付いた夜（下端＝罰なし同等・上端＝特徴量が効いていない、の読み方つき）、`--view sectors` は業種ごとの ridge α の推移。集計は純関数に切り出し、出力は ASCII の区切りだけ（cp932）。接続文字列は出さない

種別: 検証（計測） ／ 依存先: database.py (nightly_model_diagnostics), scripts/_textwidth.py

## `run_local.ps1`

`launch.py` を**正本（ローカル PostgreSQL）**で起動する PowerShell ショートカット。#503 で既定そのものが local になったため `FINAPP_DB_TARGET=local` の明示は冗長だが、親シェルが prod を持っていても引きずらないことを保証する。起動前にローカルDBへ疎通＋`companies` 件数と週次株価の最新週を表示し、繋がらなければランチャーを起こす前に落とす。`FINAPP_EGRESS_ENFORCE=0` / `FINAPP_EGRESS_LEDGER=0` も併せて立てる（ローカル読取は Egress を消費しないため）。`-Console` でランチャー無しの uvicorn 直起動。本番・CI からは未参照

種別: ユーティリティ ／ 依存先: launch.py, uvicorn

## `scripts/run_nightly.py`

**ローカル夜間バッチの実体**（#503 Phase 2・ADR-0038。骨格は `scripts/batch_common.py` と共有）。`_pipeline_incremental.py`（XBRL 差分＋マクロ＋市場データ）→ `nightly_scores.py` の順に回す。**ステップ間で止めない**（収集が落ちてもスコア更新は走り、両方の結果がログに残る）。実行のたび `app_settings` の `nightly_last_run` / `nightly_last_success` へ足跡を書き、失敗は `gh issue create` で起票する（**通知・記録の失敗はバッチを落とさない**）。収集の入口が `collector.py --incremental` ではないのが要点＝あちらは株価を1バイトも更新しない。`WINDOW_MIN`(360分) と `BUDGET_MIN`（pipeline 240 / scores 60）を持つ（#530・ADR-0040）

種別: ユーティリティ ／ 依存先: _pipeline_incremental.py, nightly_scores.py, database

## `run_nightly.ps1`

`scripts/run_nightly.py` の薄い起動口（Windows タスクスケジューラから呼ばれる）。実体を Python に置いているのは、PowerShell の「BOM 無しは cp932 扱い」「`python -c` のダブルクォートが native exe 引数で剥がれる」という実行するまで出ない罠を避けるため。`-DryRun` / `-Steps` / `-NoIssue`

種別: ユーティリティ ／ 依存先: scripts/run_nightly.py

## `scripts/install_nightly_task.ps1`

夜間バッチをタスクスケジューラへ登録する（毎日 JST 17:20・`StartWhenAvailable` で停止していた日は次回起動時に追いつく・上限6時間）。手順を人の記憶に置かないための再現用。`-Unregister` で削除。**`LogonType S4U`**（#515）＝既定の InteractiveToken は対話コンソールの CTRL_C 相当に巻き込まれ `0xC000013A` で即死しうる。登録には昇格が要り、登録後は `Export-ScheduledTask` で実物を読み戻して検証する（cmdlet は失敗しても非終了エラーで返す＝確認しないと「登録しました」と嘘をつく）

種別: ユーティリティ ／ 依存先: run_nightly.ps1

## `scripts/check_batch_freshness.py`

**バッチ鮮度 watchdog**（`python -m scripts.check_batch_freshness`・#515 手順3・[ADR-0042](adr/0042-batch-footprints-need-a-reader.md)）。判定ロジック（`Watched` / `WATCHED` / `collect`）は `batch_freshness.py` にあり `/api/morning` と共有する（#561）。ここが持つのは起票・CLI・ログ。exit 0/2/3（3＝検出したが起票できなかった、または復旧を伝えられなかった＝最も静かな故障）。

- **起票は事実ごとに別の Issue にする**: 「走っていない」（`app_settings` の `*_last_run`）／「走ったが値が書けていない」（`collect_producers()` を `problems()` へ合流・#504）／「走ったが中身がおかしい」（夜間ログの警告・#767）。原因も対処も違うので混ぜない。同一タイトルの open があればコメント追記（毎日走っても積み上がらない）。
- **閾値は `cadence + 窓` から導出する**（`run_nightly.WINDOW_MIN` / `run_monthly.WINDOW_MIN` から引く）。窓を広げれば閾値も広がり、「実行中は鳴らない」が構造的に成立する。
- **判定は `*_last_run` のみ**（`monthly_last_success` は使わない・理由は ADR-0042）。自分も監視対象に含め、**読んでから書く**（次回に自分の沈黙期間を検出できる）。
- **通知経路（`shutil.which("gh")` ＋ `gh auth status`）は健全な回にも確かめ**、レポートへ `通知経路: gh 到達可` を出す（異常時にしか `gh` を叩かないと、通知の死は一番届いてほしい回に判明する。S4U のセッション0では PATH と `hosts.yml` の解決が対話セッションと変わりうる）。到達不能なら起票を試さず exit 3 とログにだけ痕跡を残す。
- **`ok` へ戻った対象の open な起票は、復旧の根拠（読んだ場所・値・判定時刻）付きで閉じる**（#635・`recoveries()` / `close_recovered()`）。1回の ok で閉じる（連続回数の状態は持たない）。候補は ok を積極的に言えた対象だけで、`problems()` の補集合にはしない（DB を読めない回は行が空なので、補集合だと全部閉じる）。
- **人の手が入った Issue は閉じない**。起票も人のコメントも同じ gh アカウントから出るので、watchdog が本文に埋めた HTML コメントの目印（`WATCHDOG_MARKER`）の有無で見分ける。目印の無い本文・コメントがあれば閉じずに復旧コメント（`RECOVERY_MARKER`）を1回だけ残す。`--now` の回は閉じない（過去時刻だと stale の対象が ok に見える）／`--dry-run` は候補の列挙のみ／閉じ損ねも exit 3。
- **夜間ログの警告**（#767）: 最新の夜間ログ1本を `check_nightly_collect.warning_items()`（判定の唯一の源）で読み、`WARNING_KINDS` の**種類ごとに別タイトル**（`[ops] 夜間収集の警告: <種類>`）で起票・自動クローズする（新しい種類は新規起票として届き、長く続く種類に埋もれない）。
  - **終了行（`夜間バッチ終了`）の無いログは判定しない**（夜間がまだ走っていると偽警告になる・実測の最遅終了は 19:23 JST）
  - **閉じるのは元の値を読めた種類だけ**（行が無い＝不明を ok と読まない）
  - **警告文が前回と同じ晩は追記しない**。比べる相手は Issue 上で watchdog が最後に書いた署名（`NIGHTLY_SIGNATURE`）で、ローカルに状態は持たない（実測「解決済みなのに空」は15晩連続）
  - 夜間ログはファイルなので、DB を読めない回も判定する

経緯: 足跡を書く仕組みはあったが読む側が無く、2026-08-21 の欠落を丸1日誰も知らなかった（ADR-0042）。閉じるのを人に任せていた間、#634 は起票の16分後に解消したのに翌日まで open のまま残った（#635）。#765 の比率倍の株価は4か月、毎晩ログに出ながら exit 0 で誰にも届かなかった（#767）。

種別: ユーティリティ ／ 依存先: database, scripts/batch_common.py, scripts/run_nightly.py, scripts/run_monthly.py, scripts/check_nightly_collect.py

## `scripts/resolve_price_suffix.py`

**地方取引所の単独上場を Yahoo から取れるようにする一度きりの解決スクリプト**（`python -m scripts.resolve_price_suffix`・#555）。株価を1件も持たない社を `.S`（札証）/`.F`（福証）でプローブし、採用できたサフィックスを `companies.yahoo_suffix` へ永続化する。**毎晩 `.S`/`.F` も叩く方式にはしない**——取れない社 × 2サフィックス ≒ 5〜6分/晩の新しい無駄になり #475 で削った 4.2分/晩 を上回るため。採用は `exchangeName ∈ {SAP, FKA}` **かつ** `currency=JPY` **かつ出来高>0 のバーが1本以上**の AND のみ（`.F` は Frankfurt と衝突し、`377A.F`/`6461.F` は HTTP200 で254バー返すが `exchangeName=FRA`＝別会社。**件数だけ見ていると別会社の株価を書き込む**）。**バー数の下限は設けない**（低流動銘柄を落とさないため）が、**約定の証拠は要る**（2024 年に廃止済みの `1734.S` が `SAP`/`JPY`/実名＋出来高0の1本を返して誤採用され、幽霊株価が財務行へ入った・#769）。棄却は `reject_bucket` が `mismatch > empty > not_found` の優先順で畳み分ける＝「銘柄は実在するがバーが0本」（`231A.F` は `exchangeName=FKA` を返しつつ1年窓で0バー）を「Yahoo が知らない」と混ぜない。既定はドライラン・`--apply` で書込・`--backfill-weekly` で採用社だけ5年遡及（解決しただけでは daily 保持窓183日＝約26週しか付かず `z_momentum` の52週に届かない）。再開は `yahoo_suffix IS NULL` 条件だけで成立し状態ファイルを持たない。**`--reprobe` は株価を持つ解決済みの社も対象にし、確定した棄却では接尾辞を外す**（株価ゼロ限定だと解決済みの社に永久に届かず、夜間の「解決済みなのに空」が鳴り続けた。株価を持つ未解決の社は入れない＝重複上場が `.F` へ切り替わる。429・5xx 等の一時失敗の社は判定不能として触らない・#769）。**棄却理由は `companies.yahoo_probe_bucket` へ永続化する（#560）**——分類は #555 からあったが printf されて消えており「取引所は分かっているのに絞り込めない」状態だった。`--bucket empty` で対象を5社へ絞れるので、月次バッチ（`price_suffix` ステップ・予算3分）が正しい取引所で再プローブできる（**全数 454社の約8分は窓に入らない**＝Σ925+マージン30 に対し窓960）。採用時はバケットを NULL へ戻す（解決済みに棄却理由が残らない）

種別: ユーティリティ ／ 依存先: collector_prices, collector_utils, database

## `scripts/fix_naive_jst_timestamps.py`

**naive `timestamp` 列に混入した JST 値を UTC へ引き直す一度きりのスクリプト**（`python -m scripts.fix_naive_jst_timestamps`・#565・[ADR-0043](adr/0043-session-settings-travel-with-the-connection.md)）。再発は `database.SESSION_FIXES`（接続時に `SET TimeZone = 'UTC'`）が止め、**ここは既存行の引き直しだけを担う**。**対象列は `Base.metadata` から導出する**（`DateTime` かつ `timezone=False`）＝一覧を書き写すと「表を足したのに直っていない」が静かに起きる（ADR-0031 と同型）。`information_schema` も引いて **metadata に無い naive 列を警告として列挙**する。**境界は仮定でなく検査**——cutoff の直前24時間に1行でも居たら `--apply` でも書かず `exit 2`（実測の空白は pre-flip 最終行 `2026-08-18 21:10:23` と post-flip 初行 `2026-08-20 19:43:32` の間の約46時間）。**更新は生 SQL**（ORM だと `financial_records.updated_at` の `onupdate` が発火して全対象行を現在時刻で潰す）。式は `(col AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'UTC'`（セッション TZ 非依存）。冪等スタンプ `app_settings.tz_jst_backfill_applied` が2度掛け（18時間ずれる）を止め、接続先が local でなければ `SystemExit`。既定はドライラン

種別: ユーティリティ ／ 依存先: database

## `scripts/backfill_adj_factor_events.py`

**公式 `AdjFactor` のイベントを契約窓の過去2年ぶん `jquants_adj_factor_events` へ取り込む**（`python -m scripts.backfill_adj_factor_events [--dry-run] [--only ...]`・#661・ADR-0055 決定4-5）。夜間 catchup が残すのは `today-90〜today-80` の日付だけなので、それより前に窓を通り過ぎたイベントを一回きりで埋める。対象は検出器の出力から選ぶ社だけ（倍率待ちの第2経路ペアを持つ社＋翌年株数と公式を突き合わせる社＋契約窓に窓が完全に収まる第1経路の社・`choose_targets`・`--reasons awaiting,crosscheck,absence` で絞れる・`--consistency-crosscheck` で整合度照合（#751）を有効にした検出結果から選ぶ）で、1社1リクエスト＋20秒・1社ずつ書く。**イベントと一緒に、受け取った日次バーの区間を `jquants_adj_factor_coverage` へ同じ commit で追記する**（#668・決定4-8。区間は要求した期間ではなく返ったバーで決め、10 日を超える空白で切る。0 本の社は書かず報告に並べる）。**夜間バッチ（JST 17:20〜）と重ねない**（レート制限を取り合う）。ローカル正本専用。upsert なので回し直せば取りこぼしが埋まる

種別: ユーティリティ ／ 依存先: —

## `scripts/repair_splits_from_jquants.py`

**Yahoo が遡及反映しない分割で残る週次段差を、JPX 公式の裏付けを取ってから直す**（`python -m scripts.repair_splits_from_jquants`・#466）。Yahoo は `0.909091`（1:1.1）等の**無償割当を splits として持たず**価格も調整しないので、`repair_price_scale_breaks`（Yahoo 全履歴取り直し）はこの銘柄群に無効。**公式値で置き換えるだけでも駄目**——J-Quants 契約窓は2年で、対象銘柄の週次 370週のうち **253週（68%）が窓の外**にあり、窓内だけ置換すると窓境界に新しい段差ができる。したがって**窓内で実測した `AdjC / close_last` を補正比とし、窓外へ延長して掛ける**。**補正してよいかの判定だけを公式 `AdjFactor` が担う**＝比の段差はすべて公式イベント日で説明できなければならない（説明できない段差を直すのは分割の無い銘柄にニセの分割を作る行為で、M-1/M-2/M-6 へ誤った企業イベントを伝播させる）。**逆向き（イベントがあるのに段差が無い）は正常**——Yahoo が既に知っている分割は DB 側で調整済みなので比が動かない（要求すると正しい銘柄を弾く）。銘柄単位の取得は `collector_prices._jquants_fetch_code`（v2 の `code=`。**#466 本文の「日付単位の窓全走査 174分が唯一の経路」は v1 時点の記述で誤り**＝1社1リクエスト）。`volume_sum` / `turnover_sum` は触らない（`px_volz` 経由で macro 系5本の特徴量が動き昇格ゲートの再測定が要る）。更新は生 SQL で `record_prices_batch` を通さず、週次キャッシュの世代印を進める（ADR-0036）。冪等スタンプ `app_settings.splits_repaired_from_jquants`（2度掛けは係数の二乗）。既定はドライラン。**「直せない」を1つの箱に入れない**——`post_window_adjustment` が `AdjC / C != 1.0`（公式は窓内を遡及調整済みで返すのに `AdjFactor` の行が無い）で**エンバーゴ群**（分割が直近12週の中で起きており、普通の分割なので Yahoo 経路の担当）を分離する。実測 2026-08-29 は 17社検出 → 補正9社 / エンバーゴ6社 / 真に説明できない2社（E32779・E02086）で、**混ぜると打ち手を取り違える**（前者は12週待てば直り、後者は EDINET 等の第3のソースが要る）。**うち E02086 は後に第3のソースで株式分配型スピンオフと判明した**（#568）——公式 `AdjFactor` が持たず Yahoo だけが調整するので DB 側が正しい。`measured_ratios` は公式値を `corporate_actions.SPINOFF_ADJUSTMENTS` で DB のスケールへ換算してから比べ、同社は「乖離なし」になる（DB を公式へ寄せる向きには直さない）。**逆向き（公式だけが調整し DB が正しい）の企業イベントは直さない**（#652）——E34165 の新株予約権無償割当は公式 `AdjC` だけが理論係数 2/3 を掛け、契約窓に入ると `validate` を綺麗に通る。`corporate_actions.WITHHELD_OFFICIAL_ADJUSTMENTS` に「社＋日付の窓」で登録すると、窓の中に公式イベントがある社は `judge_company`（1社ぶんの群分けの純関数）が `validate` より前に `withheld` 群へ分け、`--apply` でも書かない（換算にしないのは日程が動くイベントで日付を誤ると誤った係数を書くため・ADR-0053 追記）。エンバーゴ群も分割とは限らないので、Yahoo 経路へ回す前に DB と Yahoo の一致を見る

種別: ユーティリティ ／ 依存先: collector_prices, database, weekly_price_cache

## `scripts/repair_consolidation_prices.py`

**Yahoo が株式併合を split として返した比率倍の株価を戻す一回性の修復**（`python -m scripts.repair_consolidation_prices [--apply]`・#765）。直す行は Issue #765 の表 D を1行ずつ書いた台帳 `FIXES`（日次18・週次4。うち日次2件は 2026-09-30 の Yahoo が返していた廃止後の幽霊バーで、DB に無ければ処理済みとして読む）に限る——取引の無い日（上場廃止日の幽霊バー）を戻すか消すかは外部の事実で決まり、値の形からは決められないため。観測した異常値は実値と比率から `float32` でビット単位まで再現できることをテストが縛る。**状態は値の完全一致ではなく単位で読む**（夜間が同じ単位のまま違う値で書き直すため）。**ドライランと適用は同じ手順**＝1トランザクションで日次を直す→触れた週を`_recompute_weeks_from_daily` で作り直す→日次が空になった保持窓内の週は週次も消す→E03530 の週次を直接直す→**確定の前に株価表を走査し直し、段差が残れば巻き戻して拒否**（exit 2）。ドライランは必ず巻き戻す。適用後に世代印（確定の後）と6社限定の point-in-time（`update_market_data_from_history(only=...)`）を回す。接続先が local でなければ `SystemExit`

種別: ユーティリティ ／ 依存先: collector_prices, database, weekly_price_cache

## `scripts/repair_scale_mixture.py`

**1つの価格列に2つのスケールが混ざった帯を Yahoo で取り直して均す**（`python -m scripts.repair_scale_mixture`・#620・ADR-0053）。J-Quants catchup が `AdjC != C` の行を書いていた頃に残った帯が対象。**仕組み側（catchup が `AdjC != C` を書かない）を直してから走らせる**（逆だと次の晩に書き戻される）。往復段差の形だけでは本物を選べないので、公式値と Yahoo の現値の両方と突合して確定する（再現条件は GOTCHAS.md）

種別: ユーティリティ ／ 依存先: collector_prices, collector_utils

## `scripts/check_nightly_collect.py`

**夜間バッチの収集ログを晩ごとに並べて読む**（`python -m scripts.check_nightly_collect`・#556 / #620）。`.logs/nightly_*.log` だけを読み、**DB にもネットワークにも触らない**——見たい値（対象社数・`new_rows`・gap-fill 所要・Yahoo 並行度と HTTP 429/5xx・catchup の `スケール不一致で不採用`・往復段差・株価鮮度 p50/p05）は全部そこに出ており、DB を引くと「今の値」しか分からず**その晩に何が起きたか**が残らない。設計上の要点は3つ。①**既定で3晩ぶん並べ、社数や所要の増減は警告にしない**——同じ逐次実装のまま夜ごとに 0.646 → 0.936 s/社と +45% 振れた前例があり（#556）、1回の実測を基準線にすると分散をロールアウトの効果と読み違える。曜日も併記する（平日 4078社に対し土曜 442社＝母数が桁で違う）。②**「0」と「不明」を混ぜない**。`スケール不一致で不採用 N行` は 0 件のとき**行ごと出ない**ので、行の不在を 0 と読むと #620 以前のログまで「0件で健全」に見える。往復段差の行——#620 で同じ PR に入った1組——が出ている晩だけ本物の 0 と判定し、それ以外は `None` を返して表では `-` と出す。③警告は「収集が終わっていない」「429/5xx が非ゼロ」「404 以外の 4xx が非ゼロ」「解決済みなのに空」「往復段差」「往復段差の検知の失敗」「鮮度 level≠fresh」「Yahoo の値を新たに不採用にした社（#765・同じ基準日のまま弾き続けている既知の社だけの晩は鳴らさない）」「株価表に100倍以上の段差が残っている」「その走査の失敗」の10個だけ。404 は上場廃止社が毎晩一定数（実測 約320件）返すので、内訳 `（うち404=N）` を引いた残りだけを拒否（401/403 等）の疑いとして警告する。内訳の無い旧書式の晩は判定しない（#556）。④**watchdog が毎晩これを呼ぶ**（#767）——以前は手で叩いたときにしか動かず、警告は誰にも届かなかった。警告は `warning_items()` が `(種類, 文)` で返し、種類の表 `WARNING_KINDS`（Issue タイトルのラベルと「ok と言うのに必要な元の値」）もここが持つ。`parse_nightly_log()` は `completed`（最後の `夜間バッチ開始` の後に `夜間バッチ終了` がある）を返し、書きかけのログを watchdog が判定しないための根拠になる。exit 0/2

種別: ユーティリティ ／ 依存先: collector_utils.py, scripts/_textwidth.py

## `scripts/_textwidth.py`

端末の表示幅（`f"{s:<14}"` は文字数で数えるので全角混じりだと列が崩れる）。**East Asian Width の Ambiguous は端末依存**（cp932 コンソールで2幅・UTF-8 の Windows Terminal で1幅）なので、`display_width(s, ambiguous=1|2)` として**呼び出し側が決める**。寄せる前は `check_batch_freshness`（`"FWA"`）と `preset_ic_gate`（`"WF"`）が同じ2行を各自で持ち、**Ambiguous の扱いだけが割れていた**——列がわずかにずれるだけなので失敗としては現れない（#623 と同型）

種別: ユーティリティ ／ 依存先: —

## `run_watchdog.ps1`

`scripts/check_batch_freshness.py` の薄い起動口（タスクスケジューラから呼ばれる）。`-WarnOnly` / `-DryRun` / `-Now`

種別: ユーティリティ ／ 依存先: scripts/check_batch_freshness.py

## `scripts/install_watchdog_task.ps1`

watchdog をタスクスケジューラへ登録する（毎日 JST 20:00・上限15分・`LogonType S4U`）。**時刻は判定に影響しない**（閾値が観測時刻に依存しない導出）ので、選ぶ基準は「その時刻に PC が点いている確率」だけ＝走らない監視は監視ではない。上限15分は 24h より十分小さいことが要点（`MultipleInstances IgnoreNew` の下では固まった1本が翌日ぶんを抑止する）。`-Unregister` で削除

種別: ユーティリティ ／ 依存先: run_watchdog.ps1

## `scripts/batch_common.py`

**ローカル駆動バッチの共通骨格**（#504）。`Step` / `Runner` / `BatchSpec` / `run_batch()` と、足跡（`app_settings`）・通知（`gh issue create`）・ログ綴じを持つ。各バッチは cadence も中身も違うが「走らなかったことを検知する」骨格は同じなので、ここが唯一の源（コピーすると片方だけ直す事故が起きる）。`models_from_steps()` は argv から `--model` を抜き、`HEAVY_AUTOMATION` の照合に使う（列挙を二重に持たない）。

- **子の出力はログファイルへ直結する**（`stdout=fh` / `stderr=STDOUT` ＋ 子へ `PYTHONUNBUFFERED` / `PYTHONIOENCODING`・#504）。途中で kill されてもそこまでの出力がディスクに残る。`capture_output=True` で溜めていた頃は、親ごと落ちると START 行だけが残り、「順調に長い」と「死んだ」がログ上で区別できなかった。
- **待ち合わせは `Popen` ＋ `wait(timeout=HEARTBEAT_SEC)` の heartbeat 付きで、全ステップが自動で対象**（#522・スクリプト側の opt-in にしない）。刻むのは待っている親自身なので「親が生きていて子がまだ終わっていない」の証拠になるが、**heartbeat は生存を示すが進行を示さない**（進行の裏取りは CPU 時間）。
- END 行の要約はログ末尾 `TAIL_BYTES`(8KB) だけを読み、heartbeat 行は選ばない（#521。以前は全量を str へ読み直しており、直結にした意味が END 行の瞬間に消えていた）。
- `with` の外で `run` を呼んだら**走らせずに 126 を返す**（DEVNULL へ流すと出力が丸ごと消える）。閉じたハンドルの `ValueError` は `write` も `Popen` も握って exit code へ翻訳する（漏らすとステップループごと落ち、足跡も起票も走らない）。
- **ログの先頭に実行環境を刻む**（`env_lines()`・#550）: python パス（venv か否か）・cwd・DB 接続先の**表示名**・Windows のセッション ID（S4U はセッション0）・`FINAPP_*` の一覧。`--dry-run` でも出すので叩く前に接続先を確認できる。理由（接続先の食い違いは沈黙する）は [GOTCHAS.md](GOTCHAS.md)。
  - **生の接続文字列は出さない**（`db_target_info()` の表示名だけ使う。`.logs` は Issue 本文へ貼られうる）。値に機微が入りうる変数名（KEY/TOKEN/SECRET/PASSWORD/CRED）は `***` に伏せる
  - `database` の import で落ちても行を「解決できない」に替えるだけでバッチは始める（診断行のために本業を壊さない）
- **窓はステップ予算へ分割する**（#530・[ADR-0040](adr/0040-batch-window-is-split-into-step-budgets.md)）: `Step.budget_min` を超えたら `kill_tree()` がツリーごと落として `TIMEOUT_EXIT`(124) を返す（Windows は `taskkill /F /T` が必須）。窓の終わりの打ち切りは failure として現れないので、見える失敗に変えるため。`window_problem()` が Σ予算+マージン ≤ 窓 と予算漏れを検証し、CI が呼ぶ（`install_*_task.ps1` が登録する窓とも突き合わせる）。
- **heartbeat と `env_lines()` は `sysmem.format_line()` で子ツリーの常駐メモリと空き物理メモリを刻む**。メモリによる自動打ち切りは入れない（`macro_beta` の NUTS はサンプリング中に進捗行を出さず、正常な長さを誤殺する）。
- **予算の締切は環境変数 `FINAPP_STEP_DEADLINE_UTC` で子へ渡す**（#638・[ADR-0054](adr/0054-searches-fold-before-the-budget-instead-of-losing-everything.md)）。打ち切りは猶予ゼロなので、途中経過を残せるのは子が自分で畳んだときだけ。
  - 渡すのは**時刻であって分数ではない**（子が自分の起動時刻から数え直すと、起動の遅れぶん判定が甘くなる）
  - **予算の無いステップでは明示的に消す**（継承した古い値が残ると、無期限のはずのステップが勝手に畳み、失敗として現れない）
  - 読む側は `hyperparameter_search.resolve_deadline()`。定数名が root 側と2箇所に分かれる（root から `scripts/` を import しない）ので `tests/test_hyperparameter_search.py` が照合する

種別: ユーティリティ ／ 依存先: database, sysmem

## `scripts/check_heavy_imports.py`

**重い依存が実際に import できるかの smoke**（`python -m scripts.check_heavy_imports`・月次の `deps_smoke` ステップ）。numpy/scipy/pandas/sklearn/statsmodels と pymc/pytensor/arviz/jax/numpyro を**1つずつ**読み、`jax.devices()` まで踏み込む。**未導入（`ModuleNotFoundError`）は skip、導入済みで import 失敗は error**——同一視すると Smart App Control のブロックが「入っていないだけ」に見えて黙って通る（`ModuleNotFoundError` は `ImportError` のサブクラスなので捕捉順が効く）。2026-09-01 の初実走で SAC が 8/21 の jaxlib 更新で入った未評価の `_ifrt_proxy.pyd` を初回ロードでブロックし `macro_beta` が exit=1／1か月ぶんの `macro_beta_loadings` が固着した。**未評価 DLL の初回ロードをここが引き受ける**ので本番ステップは通り、それでも落ちるなら数百分の予算を待たず起票される。SAC は OFF にしない（不可逆）。**本番の推論と同じ条件で測るため冒頭で `jax_import_guard.install()` を呼び、拡張を代替したら `[warn ]` 行で残す（失敗に数えない・#782）**——判定の反転は CodeIntegrity ログ（約4時間で上書き）に残らず、このログが唯一の時系列になる。**`--profile base` は基盤5つだけ（M-1・月次本体＝jax 系を import しないバッチ）、既定の `inference` は推論系＋`jax.devices()` まで（月次 beta・日中枠の beta / bench）**——M-1 が使わない `jaxlib/cpu/_sparse.pyd` の遮断で M-1 が失敗扱いになった（#789）

種別: ユーティリティ ／ 依存先: jax_import_guard

## `scripts/run_monthly.py`

**ローカル月次バッチの実体**（#504・親 #503）。`_pipeline_vacuum.py`（#290・ACCESS EXCLUSIVE ロックを取るので先頭固定）→ `scripts/resolve_price_suffix` → `scripts/check_heavy_imports`（#584）→ `recommend_factor_premia.py` → `hyperparameter_search.py` ×2（**M-3 / M-2**）の順に回す。**M-1 は `scripts/run_monthly_m1.py` へ切り出した**（#584・ADR-0046）。**並びは「依存順 ∧ 軽い順」**＝打ち切られても前方が揃うよう軽い順。M-1 の入力 `macro_beta_loadings` の推論は #579 で `scripts/run_monthly_beta.py` へ出たので、その依存順は日をまたいで「beta（2日）→ M-1 探索（3日）」になった。引数は GHA の3本（tune / macro-beta / factor-premia）から**そのまま移設**（探索規模を変えると #291 の品質ゲートが別条件の値と比較される）。足跡は `monthly_last_run` / `monthly_last_success`（日次と別キー＝月次の停止が日次の成功で隠れない）。**`WINDOW_MIN`(960分) と `BUDGET_MIN` を持つ**（#530・ADR-0040）＝並びの「打ち切られても前方は揃う」は「前方が窓を食い尽くさない」が前提で、実際には `macro_beta` が16時間を使い切り `tune×3` が一度も起動しない状態だった。予算は名前引きの表から与える（`Step` へ直書きすると付け忘れを検出できない）。**tune の予算は「窓に収まる」だけでは足りず、実測に対する余裕を持たせる**（#633）＝`window_problem` は Σ が窓を超えないことしか見ないので、1本が実測ぎりぎりでも Σ さえ収まれば通る。だが超過は `exit=124` の打ち切りになり、**当時は完走してからしか永続化しなかったためその月ぶんの探索結果が丸ごと消えた**（#638・ADR-0054 で締切の手前から畳んで永続化するようにしたが、**予算を薄くしてよい理由にはならない**——畳んだ回は探索空間の一部しか見ていない）。2026-09-01 の `tune:macro_dlm` は当時の予算250分で `[199/294]` まで進んで打ち切られ、`plugin_tuned_params` の μ̂ は 2026-07-10 のまま **59.5日** 固着した（`macro_beta` が16時間を食った 8/21・`factor_premia` で終わった 8/23 と合わせて3か月ぶん）。気づけたのは #504 の producer 監視が起票したときで、バッチ失敗の Issue（#587）は既にクローズ済みだった。以後 `tests/test_run_monthly.py::TestTuneBudgetsMatchTheMeasurement` が **予算 ≥ `run_daytime.JOBS[...].measured_min` × 1.25** を CI で縛る（実測の唯一の源は日中枠の `Job`。倍率の根拠は `macro_gbdt` の 240/179.4 = 1.34倍で、「余裕2%では次回落ちる」として積んだ実績値を下回らない範囲を採った）。**パネルは毎晩伸びるので所要は据え置かず伸びる**＝余裕も据え置きでは足りない

種別: ユーティリティ ／ 依存先: scripts/batch_common.py, recommend_factor_premia, hyperparameter_search

## `scripts/run_monthly_beta.py`

**M-1 の入力 `macro_beta_loadings` を作る専用バッチ**（毎月2日 JST 01:00・窓16h・#579）。本番規模の実測 **360分**で月次本体の予算180分に収まらず、本体の窓にも空きが無かったため切り出した。所要が縮む見込みは #600 で否定済み（軌道長のレバーは存在しなかった）。**M-1 探索（毎月3日）より前の日でなければならない**——`macro_beta_loadings` は探索の入力で、後ろに置くと毎月「前月の値で探索」になり**失敗として現れない**。収束ゲートに落ちた run は `status=quarantined` で保全され exit は非0。`--force` は人手で精査したときだけの経路

種別: バッチ ／ 依存先: scripts/batch_common.py, macro_beta_inference.py

## `scripts/run_monthly_m1.py`

**M-1（macro_risk_return）探索の専用バッチ**（#584・[ADR-0046](adr/0046-steps-that-cannot-finish-get-their-own-task.md)）。`scripts/check_heavy_imports` → `hyperparameter_search.py --model macro_risk_return` の2ステップ。**月次本体から切り出した理由**は所要——実測 2.61分/件 × 288件 ＝ **約752分**で本体の窓（960分）のほぼ全部を1本で食う。当時は `search()` が完走してからしか永続化せず、**予算内に終わらないステップは時間を使い切って何も残さなかった**（2026-09-01 は M-1 に250分・M-3 に250分を与えて両方とも成果ゼロ）。#638・ADR-0054 で畳んで残すようにしたが**切り出しの判断は覆さない**——畳んで残るのは探索空間の一部を見た結果であって、毎月それでよいわけではない。起動は毎月3日 JST 01:00（`scripts/run_monthly_beta.py` の翌日・`TRIGGER_DAY`）・窓16時間、Σ予算905+マージン30 ≤ 960。足跡は `monthly_m1_last_run` / `monthly_m1_last_success`（本体と別キー＝M-1 が走らなかった月が本体の成功で隠れない）。`macro_beta_loadings` への依存は**日をまたぐ**（`run_monthly_beta.py` が2日に推論し、3日にこれが使う・#579）。`batch_freshness.WATCHED` に載せてあり、載せ忘れは`TestEveryLocalBatchIsWatched` が CI で落とす

種別: ユーティリティ ／ 依存先: scripts/batch_common.py, hyperparameter_search

## `run_monthly.ps1`

`scripts/run_monthly.py` の薄い起動口（タスクスケジューラから呼ばれる）。`-DryRun` / `-Steps` / `-NoIssue`

種別: ユーティリティ ／ 依存先: scripts/run_monthly.py

## `scripts/install_monthly_task.ps1`

月次バッチをタスクスケジューラへ登録する（毎月1日 JST 01:00・上限16時間＝日次 17:20 までの窓の幅）。**タスク定義 XML を直接渡す**——`New-ScheduledTaskTrigger` に `-Monthly` は無く、CIM の `MSFT_TaskMonthlyTrigger` も `schtasks` 産のトリガの渡し直しも `Register`/`Set-ScheduledTask` が "The parameter is incorrect" で弾く（実測）。しかも**非終了エラーで「登録しました」と嘘が出る**ため、登録後に `Export-ScheduledTask` で日・上限・`StartWhenAvailable` を読み直して検証する。**既定 `-Hours 16` は `run_monthly.WINDOW_MIN` と CI で照合される**（#530・窓と予算はセットでしか意味を持たず、片方だけ動かしても失敗として現れない）。`-Unregister` で削除

種別: ユーティリティ ／ 依存先: run_monthly.ps1

## `run_monthly_beta.ps1`

`scripts/run_monthly_beta.py` の薄い起動口（タスクスケジューラから呼ばれる・毎月2日）。`-DryRun` / `-Steps` / `-NoIssue` / `-Force`（収束ゲートを無視して live で persist）

種別: ユーティリティ ／ 依存先: scripts/run_monthly_beta.py

## `run_monthly_m1.ps1`

`scripts/run_monthly_m1.py` の薄い起動口（毎月3日）。`-DryRun` / `-Steps` / `-NoIssue`

種別: ユーティリティ ／ 依存先: scripts/run_monthly_m1.py

## `scripts/install_monthly_beta_task.ps1`

macro_beta 専用タスクの登録（既定 毎月2日 JST 01:00・16時間）。登録ロジックは `install_monthly_task.ps1` へ委譲し、既定値だけを持つ。**登録後に1回手動実行して足跡を入れる**（足跡が無いと watchdog が起票する）

種別: ユーティリティ ／ 依存先: scripts/install_monthly_task.ps1

## `scripts/install_monthly_m1_task.ps1`

M-1 探索専用タスクの登録（既定 毎月3日 JST 01:00・16時間）。登録ロジックは `install_monthly_task.ps1` へ委譲する

種別: ユーティリティ ／ 依存先: scripts/install_monthly_task.ps1

## `scripts/run_backup.py`

**週次バックアップの実体**（毎週日曜 JST 21:00・窓2h・#606。骨格は `scripts/batch_common.py` と共有）。`scripts/backup_push.py --apply --dest storage` を子プロセスで1本回すだけだが、バッチの骨格へ乗せているのは**「走らなかったこと」を検知できる形にするため**——手で叩く CLI のままだと取り忘れも失敗も同じ「何も起きない」に見え、実効 RPO が「最後に人が思い出した日」になる（#503 で復元経路は通したのに取る側が止まっていた）。`--apply` と `--dest storage` は**両方必須**で、どちらが欠けても exit 0 で返る（ドライラン／ローカルだけ＝正本と同じディスクで一緒に失われる）ため、`tests/test_run_backup.py` が argv を固定する。`WINDOW_MIN`(120分) と `BUDGET_MIN`（push 90）は窓から導出（ADR-0040）

種別: バッチ ／ 依存先: scripts/batch_common.py, scripts/backup_push.py

## `run_backup.ps1` / `scripts/install_backup_task.ps1`

薄い起動口と、タスクスケジューラへの登録（毎週日曜 21:00・上限2時間）。**登録ロジックを `install_monthly_task.ps1` へ委譲できない**——あちらは月次専用のタスク XML を組み立て `-Day` を 1..28 に制限しているため。週次は cmdlet で表現できる（`-Weekly -DaysOfWeek`）ので `install_nightly_task.ps1` の形を写した。nightly 側をパラメータ化して共有しないのは、あちらがトリガ種別も実行時間上限も引数に持たず、共有化が毎晩動いている本番経路の書き換えになるため。代わりに**登録後の検証4項目が nightly と同じであること**を `tests/test_run_backup.py` が照合する

種別: ユーティリティ ／ 依存先: scripts/run_backup.py

## `scripts/run_daytime.py`

**平日日中の枠で重い計算をキューから窓に収まるだけ進める**（#707・[ADR-0060](adr/0060-the-daytime-window-takes-as-many-jobs-as-it-fits.md)）。平日 JST 08:00・窓8h（#618）。骨格は `scripts/batch_common.py` と共有。

**なぜ日中なのか**: 重い計算の裏で別の作業を並走させると、同じ入力から違う答えが出る（`macro_beta` の発散が 0 → 344回・[ADR-0002](adr/0002-m1-per-stock-hierarchical-macro-beta.md)）。本番の推論経路には `bench_macro_beta.apply_thread_limits` に相当するスレッド固定が無く、XLA が使うコア数が実行時の混み具合で決まり、浮動小数の加算順序が変わるため。**キュー方式**（`app_settings.daytime_queue` に JSON 配列）にしたのは、曜日固定だと「今週はこれを先に」が効かず、都度の手動登録は積み忘れても気づけないため。

**守ること**

- **失敗しても先頭は取り除く**（戻すと同じ計算を毎日繰り返して先へ進まない）。
- **ただし、結論を出す前にプロセスごと消えた中断だけは戻す**（#639・[ADR-0054](adr/0054-searches-fold-before-the-budget-instead-of-losing-everything.md)）。見分けるのが in-flight マーカー（`app_settings.daytime_inflight`）:
  - pop の直後に `state=running` で立て、**戻り値によらず `finally` で消す**。`running` のまま残っていれば OS ごと消された証拠
  - 次の実走の冒頭で `reclaim_inflight()` が回収し、**キュー先頭へ**戻す（末尾だと数日進まない）
  - 戻すのは **1回だけ**（`MAX_REQUEUE=1`）。回数は `state=queued` のマーカーで引き継ぎ、2回目は戻さず起票して捨てる
  - **未完了の仕事を全部持つ**（`{"jobs": [...], "job": <先頭>, ...}`。`jobs` の無い古い値は `[job]` として読む＝`inflight_jobs()`）。`batch_common.Hooks.on_step_done` で1件終わるごとに縮める（成否は問わない）。回収時の重複除去は「戻す1件につき1つまで」（同名を複数積んだ回に残りまで消さない）
  - `--clear-queue` はマーカーも消す。壊れた値は無いものとして読む。マーカーは日中キュー固有なので `batch_common.py` には置かない
- **`--peek`・`--queue`・ドライランは中断の回収を見込んだ計画を出す**（#742）。回収の判断は純関数 `plan_reclaim` に置き、実走の `reclaim_inflight` はその結果を書くだけ。読み手は `apply_schedule(write=False, preview_reclaim=True)` で同じ並びを見る（以前は `--peek` が回収予定の敏感な仕事を見落とし、`-Now` が人の作業中に実走を起動した）。
- **キューが空の日は exit 0**（平日毎日走るので、起票すると鳴りっぱなしになる）。
- **引数はキューに触る前に全部解析する**（#692・`build_parser` は共通パーサにキュー操作を足した上位集合）。以前は `"--x" in args` の手書き判定のあと `bc.run_batch` の中で初めて解析していたため、`--help` や打ち間違いが `take` の後で `SystemExit` になり、先頭の仕事が exit 0 のまま黙って消えた（in-flight マーカーも `finally` で消えるので回収にも掛からない）。`--steps` の検証も `take` の前。**キュー操作は互いに排他**（2つ渡すと exit 2）。`--steps` で絞った日は生き残ったステップの仕事だけを `take` する。
- **`Job.parallel_sensitive` は「裏で作業されると同じ入力から違う答えが出るか」**を表す（所要が延びるかではない）。`beta` / `tune:*` / `gate:*` / `bench:rhat-scale` は True、`interim` / `disclosures` / `wf:preset-weights` は False。**既定値を持たせない必須フィールド**（足し忘れを `TypeError` で落とす）。分岐するのは `-Now` の手動キックだけ。
- **`JOBS` に無い名前と予算超過の仕事は `enqueue` の時点で弾く**（M-1 探索 752分は積めない＝[ADR-0046](adr/0046-steps-that-cannot-finish-get-their-own-task.md) の専用タスク）。引数は `run_monthly*.py` と同一であることをテストが照合する。
- **仕事は手で作ったキャッシュに依存させない**（#674）。`--refresh-cache` を渡し、入力を毎回ローカル DB から作り直す。書き忘れは `tests/test_run_daytime.py::TestJobsBuildTheirOwnInputs` が落とす（経緯は [GOTCHAS.md](GOTCHAS.md)）。
- **暦**（#681・[ADR-0056](adr/0056-the-daytime-queue-follows-a-calendar.md)）: 日付で決まる仕事は `SCHEDULE` が積む（`disclosures` は毎月1日以降、`interim` は毎月16日以降の最初の実走で**キュー先頭へ1回だけ**。判定した月は `app_settings.daytime_schedule`）。今月ぶんが入っているかは成果物の `created_at`（`h1_created_at` / `disclosure_created_at`＝`batch_freshness.PRODUCERS` と同じ読み手）で判定し、`updated_at` は使わない。測れなければ積む側へ倒す。
  - **月次系のバッチ（`MONTHLY_BATCHES`）と時間が重なる日は並走に敏感な仕事を取り出さない**（`monthly_overlap_days()` が `run_monthly*.TRIGGER_DAY/TRIGGER_TIME/WINDOW_MIN` から導出・実測の所要では判定しない）。その日は敏感でない仕事を先頭から探し、無ければ足跡だけ残す。`-Now -Force` でも見送る
  - **祝日・年末年始（`HOLIDAYS` 表・毎年足す）も敏感な仕事を見送る**（#684）。こちらは `-Now -Force` だけが当日限りの解除印で外せる
  - 判断は純関数 `plan_schedule` / `select_jobs` に置き、実走・ドライラン・`--peek`・`--queue` が共有する（見せた並びと走る並びをずらさない）
- **何件取り出すか**（#707・ADR-0060）: 守っているのは「重い計算の裏で並走させない」ことであって件数ではない（同じ窓の中で順番に回すのは並走ではない）。
  - `select_jobs` は分け前 `share(N) = JOB_BUDGET_MIN / N` に `measured_min × JOB_HEADROOM(2.0)` が全員収まる間だけ件数を伸ばす。**先頭1件は無条件**（先頭が窓に入ることは `enqueue` が保証済み）
  - **予算は窓の等分で、所要の按分ではない**（`measured_min` は件数の計画にだけ使う。按分すると ADR-0040 の「実測から逆算しない」が崩れる）。N=1 の日は窓を丸ごと渡す。件数の上限定数は置かない
  - 同じ名前は1日に2回取らない（`bench:rhat-scale` はわざと3つ積む）。`--peek` は `keys` / `total_measured_min` を足し、`sensitive` は1件でも敏感なら true

**経緯と実測（ADR に無いもの）**

- 2026-09-09: Windows Update の再起動（12:45〜12:51 に Kernel-Power 109 が3連発・月例パッチ KB5124008 と .NET KB5126052）が `tune:macro_dlm` を 285分・255/294件で殺し、キューからも消えた（#638）。ログ末尾に `END` 行が無く、タスクの `LastTaskResult` は `0x41306`（強制終了）。`daytime_last_run` は閾値内で watchdog は起票せず、producer 監視が M-3 の成果物の60.5日固着を見ていただけだった（#639 の動機）
- 2026-09-08（初の実走）: `disclosures` 14.1分・4052件、`interim` 62.0分・新規0件（真因は #647）。`tune:macro_gbdt` は 179.4分・151候補を完走して `objective_value=0.2268`（同一パネルの champion 0.1442 から改善。`prev_objective_value=0.5068` はパネル世代が違うので比べない・[ADR-0047](adr/0047-tuning-gate-compares-on-one-panel.md)）
- 2026-09-16: `interim` 64.6分・saved=3967・failed=0（H1 の `year=2026` が 298 → 3875行）。ZIP 失敗は成功した doc_id しか `skip_existing` に残らないので回すたび繰り返し、exit=0 なので失敗として現れない（#630）

種別: バッチ ／ 依存先: scripts/batch_common.py, macro_beta_inference.py, hyperparameter_search.py

## `run_daytime.ps1` / `scripts/install_daytime_task.ps1`

薄い起動口と、タスクスケジューラへの登録（平日 08:00・上限8時間・`-Weekly -DaysOfWeek Monday..Friday`）。窓を9時間に広げないのは、夜間バッチ（17:20）まで20分しか残らず**日中枠が長引いた日にメモリを取り合う**ため——それは上記の発散を招いた条件そのもの。`StartWhenAvailable` は諸刃で、PC が落ちていた日の回が夜に走ると「触らない時間帯」の前提が崩れるが、外出が流れた日に丸1日進まない方が痛いので付けている（夜に走ったことはログの開始時刻で分かる）。**`-Now` は平日8時の枠を待たずに次の回ぶん（窓に収まるだけ・#707）を消化する**（休暇などで平日昼に PC を触れる日用）——実体は `Start-ScheduledTask` で**登録済みタスクを叩く**形で、`run_daytime.ps1` を対話ターミナルで直に走らせない。直だとプロセスがその端末の子孫になり、ターミナルや IDE を閉じた瞬間に死ぬ（#515 と同型）。タスク経由ならセッション0・`ExecutionTimeLimit` 8時間・`MultipleInstances IgnoreNew` がそのまま効く。**次に取り出す仕事に `parallel_sensitive=True` が1件でもあれば `-Force` 無しでは起動しない**（複数件を取り出す日は安全側へ倒す・#707）——手動キックは人が PC を触っている時間帯に叩かれるのが前提で、それはこのバッチが避けるために作られた条件そのもの。判定は `--peek`（キューを減らさない機械可読口）が返す JSON で行い、**JOBS に定義が無い名前は敏感側へ倒す**（判断材料が無いときに黙って走らせない）。走行中に叩いた場合は起動せず開始時刻を出して終わる（IgnoreNew は無視したことを戻り値に出さない）。起動後は State が `Running` になったことを確かめてから成功を出す（`Start-ScheduledTask` は起動しなくても例外を投げない）

種別: ユーティリティ ／ 依存先: scripts/run_daytime.py

## `scripts/backup_push.py`

正本を表ごとに `pg_dump --compress=9` して Supabase Storage へ置く（#503 Phase 3）。**バックアップ元は正本（ローカル）限定**（`guard_source_is_primary`）。ダンプ順は FK 依存の**逆順**（子が先・親が後）＝表ごとに別プロセスでスナップショット時点が揃わないため。保持は直近4世代＋各月の最初を6か月で、**`--dest storage` のときもローカル世代へ同じ保持ポリシーを適用する**（`prune_local` を dest 分岐の外で呼ぶ。storage 経路もいったんローカルへダンプしてから上げるため、掃除を storage 側だけにかけると週次自動実行で年 1.9GB が正本と同じディスクへ積み上がった・#606）。`.backups/_from_storage/` は直下に manifest を持たないので世代として数えず、掃除で消えない。Free プランの 50MB/ファイル・1GB に対し実測 38.1MB/世代。`--dest local` なら認証情報なしで検証できる

種別: ユーティリティ ／ 依存先: scripts/mirror_common.py, httpx

## `scripts/backup_restore.py`

バックアップ世代からローカルへ復元する（#503 Phase 3）。**FK 依存順に1表ずつ**（`pg_restore --disable-triggers` は非 superuser では使えない）。復元順はマニフェストの並びを信用せず毎回 `mirror_tables()` から取り直す。復元後にマニフェストの行数と突合し、食い違えば exit 1。書込先は `guard_dest_local` でローカル限定。取得元は `--source local`（既定・`.backups/`）と `--source storage`（Storage から `.backups/_from_storage/<stamp>/` へ落としてから合流。落とした直後にマニフェストの `bytes` と突合＝転送の欠けを復元より手前で落とす。**ローカル世代と場所を分ける**のは `backup_push` の保持ポリシーが `.backups/` 直下を数えるため）

種別: ユーティリティ ／ 依存先: scripts/backup_push.py, scripts/mirror_common.py

## `scripts/check_macro_health.py`

`macro_health.py` の判定 CLI（`python -m scripts.check_macro_health`）。critical 系列が不健全なら exit 2 → `notify-failure.yml` が Issue 起票。`macro-health.yml` から起動。**収集パイプライン本体からは意図的に分離**（収集ジョブを failure にすると `nightly-scores` の `workflow_run` チェーンが発火せず `sector_ols` 夜間更新まで止まるため・#425/#432）

種別: GitHub Actions / ユーティリティ ／ 依存先: macro_health.py, database.py

## `scripts/check_egress_health.py`

Supabase 枠消費の判定 CLI（`python -m scripts.check_egress_health`・#478/#483・[ADR-0037](adr/0037-egress-cycle-budget-is-a-second-axis.md)）。`app_settings` の Egress サイクル累計（warn 80%）と `pg_database_size`（warn 85%・**Egress より厳しい**＝超過は read-only で収集が止まるため）を突き合わせ、超過なら exit 2 → `notify-failure.yml` が Issue 起票。**印が現サイクルでなければ累計は 0 として読む**（前サイクルを繰り越すとリセット直後に必ず誤警報が出る）。Management API の PAT は不要＝判定材料は DB の中にあるので #483 のブロッカーを迂回する。`--warn-only` で常に exit 0

種別: GitHub Actions / ユーティリティ ／ 依存先: db_egress.py, database.py
