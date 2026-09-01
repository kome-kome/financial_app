# PLUGIN_REFERENCE.md — 分析プラグイン実装リファレンス

> **位置づけ**: [ARCHITECTURE.md](ARCHITECTURE.md) §10「ファイル役割一覧」から `plugins/` 配下の詳細説明を分離した実装リファレンス（1セルが 500〜600 字まで肥大し、役割表としての索引性を失っていたため）。
>
> | 知りたいこと | 参照先 |
> |---|---|
> | モデルの理論・数式・参考文献 | [MODELS.md](MODELS.md)（**正本**） |
> | 実装の内部仕様・契約・実測値 | **本書** |
> | ファイル一覧としての索引・処理フロー・ER図 | [ARCHITECTURE.md](ARCHITECTURE.md) |
> | 設計判断の経緯（ADR） | [adr/README.md](adr/README.md) |
> | 用語定義（パラメータ契約・分析の階層等） | [../CONTEXT.md](../CONTEXT.md) |

プラグインの共通契約（`params_schema()` のパラメータ契約・`execute` は同期実装・`plugins.execute_plugin` が単一入口・`depends_on` の強制）は [../CLAUDE.md](../CLAUDE.md)「設計制約」を参照。

---

## 索引

| ファイル | モデル | heavy | producer（永続化先） | ui_order |
|---|---|---|---|---|
| [`plugins/base.py`](#pluginsbasepy) | — | — | — | — |
| [`plugins/__init__.py`](#plugins__init__py) | — | — | — | — |
| [`plugins/utils.py`](#pluginsutilspy) | — | — | — | — |
| [`plugins/progress.py`](#pluginsprogresspy) | — | — | — | — |
| [`plugins/recommend.py`](#pluginsrecommendpy) | — | | — | 110 |
| [`plugins/net_cash_analysis.py`](#pluginsnet_cash_analysispy) | — | | — | 120 |
| [`plugins/sector_ols.py`](#pluginssector_olspy) | — | ✅ | `regression_results` | 210 |
| [`plugins/gap_analysis.py`](#pluginsgap_analysispy) | — | | — | 220 |
| [`plugins/macro_risk_return.py`](#pluginsmacro_risk_returnpy) | M-1 | ✅ | `macro_beta`（共有） | 330 |
| [`plugins/macro_gbdt.py`](#pluginsmacro_gbdtpy) | M-2 | ✅ | `macro_gbdt_scores` | 340 |
| [`plugins/macro_dlm.py`](#pluginsmacro_dlmpy) | M-3 | ✅ | `macro_dlm_scores` | 360 |
| [`plugins/macro_ensemble.py`](#pluginsmacro_ensemblepy) | M-4 **（退役）** | ✅ | `macro_ensemble_scores` | 370 |
| [`plugins/macro_gbdt_rank.py`](#pluginsmacro_gbdt_rankpy) | M-5 **（退役）** | ✅ | **なし**（OOF 比較専用） | 380 |
| [`plugins/macro_enet.py`](#pluginsmacro_enetpy) | M-6 | ✅ | `macro_enet_scores` | 390 |
| [`plugins/sell_ranking.py`](#pluginssell_rankingpy) | — | | — | 510 |
| [`plugins/macro_snapshots.py`](#pluginsmacro_snapshotspy) | 共有基盤 | — | — | — |
| [`plugins/tuning.py`](#pluginstuningpy) | 共有基盤 | — | — | — |
| [`plugins/model_candidates.py`](#pluginsmodel_candidatespy) | 探索枠 | — | — | — |

`heavy=True` は Render 軽量モードで 403（ローカル / GitHub Actions 実行に限定）。**heavy を追加したら `nightly_scores.HEAVY_AUTOMATION` への自動実行登録が必須**（ADR-0031・未登録は CI が落とす）。

**（退役）** は `hidden=True`（#570・[ADR-0044](adr/0044-retire-underperforming-models-by-hiding.md)）＝`/api/plugins` から除外されサイドバーに出ない。**削除ではない**ので `get_plugin` / `POST /api/plugins/{name}/run` / `model_comparison.COMPARISON_MODELS` / テストは従来どおり生きており、`python -m scripts.model_comparison_run --models <a>,<b>` で測り直せる。`hidden` は「選択肢として勧めない」＝評価の結論で、`heavy`（実行環境の制約）とは別軸。

---

## `plugins/base.py`

分析プラグインの抽象基底クラス。`heavy`（既定 False・True なら Render 軽量モードでブロックしローカル実行を促す）・`hidden`（既定 False・True なら `/api/plugins` から除外＝サイドバーに出ない**退役**・ADR-0044）・`ui_order`（既定 999）・`produced_output(db)` 等の共通契約を定義する。`heavy` は実行環境の制約、`hidden` は評価の結論で**別軸**（hidden にしてもレジストリ・`execute_plugin`・`model_comparison` には残る）。

依存先: —

## `plugins/__init__.py`

プラグインを自動スキャン・レジストリ管理。`execute_plugin(plugin, raw, db)` が起動の単一入口（内部で `coerce_params`→`ensure_dependencies`→`execute`）。

依存先: `plugins/*.py`

## `plugins/utils.py`

`coerce_params()`・`ols()`・`normalize()`・`winsorize()`・`walk_forward_cv()`・`walk_forward_cv_monthly(fit_predict=None, pass_train_groups=False)`（fit_predict コールバックで OLS/XGBoost を切替可・ADR-0003 §3。`pass_train_groups=True` で各学習月のサンプル数配列を3引数コールバックへ渡し XGBRanker の月クエリグループを復元・M-5・#362。既定 False で M-1/M-2/M-3 は不変）・`get_macro_features()`・`get_momentum_return()`・`fit_feature_columns()`・`transform_feature_row()`・`fit_zscore_stats()`（断面の生値から winsorize 済み (mean, sd) を返す。消費側の期内標準化の前段で、4件未満は `None`＝生値フォールバック・#509。**非有限値（NaN / ±inf）は入口で落とす**——1件混ざると winsorize の `np.percentile` 経由で全要素が nan になり、`sd = var ** 0.5 or 1.0` は nan が truthy なので守れず、断面の全社スコアが無言で nan 化する・#516）。**`fit_feature_columns()` は行ゼロの入力を fail-fast で落とす**（`min() on empty`）。中立な `win_params=(0.0, 0.0)` を返して救うと `transform_feature_row` がどんな入力も 0.0 へ潰し、切片だけの予測が例外もログも無く出るため（#518・#509 のガードは撤回）。空断面を飛ばしたい呼び出し側が自分で弾く。

`winsorize`/`normalize`/`fit_feature_columns` は numpy ベクトル化済み（Issue #304）。要素ごとの変換（ソート・比較・クリップ・log・減算・除算）は numpy でベクトル化しつつ、平均・標準偏差の集計（`statistics.mean`/`stdev`・`sum()/len()`）は Pure Python のまま維持し、**旧実装とビット単位で同一の数値を返す**（np.mean/np.sum は総和順序が異なり丸め誤差が生じ得るため）。

依存先: —

---

## `plugins/progress.py`

heavy プラグイン実行の進捗を画面へ流す唯一の経路（#545）。`emit(step, current, total, every=)` / `progress_sink(fn)` / `active()` ＋ カバレッジ表 `PROGRESS_COVERAGE`。

sink は **ContextVar**。`execute` のシグネチャは `(params, db)` に固定されていて引数を増やせず、`execute_plugin` は execute を `asyncio.to_thread` へ逃がす（#357）が、**to_thread はコンテキストを複製するので ContextVar は素通しで伝播する**（`tuning_dry_run` / `shared_snapshot_cache` と同じ手）＝ `execute_plugin` は無改造。包むのは `routers/analysis.py::_execute_with_progress`（heavy のときだけ）で、**sink 未設定なら emit は完全な no-op**——月次バッチ（`scripts/run_monthly*.py`）や `/api/recommend`・`/api/gap-analysis` 経路は進捗機構に一切触られない。

発生源は共通骨格に置く（M-1/M-2/M-3/M-6 系は全部 `load_data → preload_macro → build_snapshots` を通るため、ここに入れれば一括で進捗を持つ）: `load_weekly_prices_chunked`（500社チャンク）・`_preload_macro_impl`・`_build_snapshots_impl`（全社ループ・`EVERY_COMPANIES=100` で間引き）。`sector_ols`（業種ループ）と `macro_dlm`（銘柄ループ）は自前で足す。**キャッシュヒット時は `_cached_or_computed` が「キャッシュから復元」を出す**——黙って飛ばすと件数ゼロのまま完了し「0件で終わった」と区別できない。

間引きは **最初（current=0）と最後（current=total）を必ず通す**。終端を落とすと「4300/4400 のまま完了」に見え、止まったのか終わったのか分からなくなる。

`PROGRESS_COVERAGE` は heavy 名 → `common`（共通骨格を通る）/ `own`（自前 emit）/ `exempt: <理由>`。**「heavy を足したが進捗が無い」は画面が沈黙するだけで例外もログも出ない**ため、`tests/test_plugin_progress.py::TestProgressCoverageRegistry` が表と実体（MRO を辿ったモジュールソース）を CI で照合する（ADR-0031 の `HEAVY_AUTOMATION` と同型）。

依存先: —

---

## `plugins/recommend.py`

複合スコアによる銘柄推薦（z_roe 等 `financial_metrics` VIEW 8指標＝`VIEW_METRICS`＋`RUNTIME_METRICS`＝z_momentum/mu）。

- **VIEW 由来指標は加重前に断面で winsorize→標準化する**（`fit_view_metric_stats(records, weights)` → `standardize_metric`・Issue #509）。VIEW の `z_*` は年度窓の `(x-AVG)/STDDEV_SAMP` で **winsorize を通らない**ため、`op_margin` と `cf_ratio`（分母が共に `pl_revenue`）でゼロ近傍の1社が sd を支配し、実測で `z_op_margin` の 99.4% が \|z\|<0.2 まで潰れていた＝**同じ重み 1.0 の実効影響力が列間で最大 73倍違う**（#469 で決着）。`gap_ratio`（％単位で Z ですらない）も同じスコア合成へ入る以上は対象。`RUNTIME_METRICS` は `compute_momentum_z`／`compute_mu_z` が既に期内標準化しているので**除外**（二重標準化しない）。有効サンプル4件未満の列は生値へフォールバック。`results[].detail` は**生値のまま**（画面の表示値とスコアの合成単位は別物・`sell_ranking` と同じ扱い）。`backtest.py` も `run()` が同じ stats を作って `score_record` へ渡す＝as-of 再現が同じ土俵になる。
- **`mu`（μ̂）は opt-in・既定 OFF**（Issue #423 子4・ADR-0030）— `mu_source`（M-1/M-2/M-3/M-4/M-6・既定 None）で producer を選び、`compute_mu_z` が sell_ranking と同じ producer 契約（`read_producer_scores`）で読んで候補集団内 winsorize→Z化する。**mu に重みがあるのに mu_source 未指定は ValueError→400**（黙って欠測にしない）、producer 未実行は graceful-degrade＋`mu_available=false`／`mu_asof` をレスポンスへ明示。4プリセットは mu 重みを持たず（`test_no_preset_carries_mu` が強制）、mu 重み 0 なら producer を読まない＝**既定経路のコストは 0**。
- `z_momentum` も VIEW 外の実行時計算（`compute_momentum_z`）で、候補集団の `StockPriceWeekly` を **as_of − 400日の下限付き・500社チャンク**で取得し（Issue #418・下限は PK 第2列の `week_start` へ掛けて範囲スキャン化）`get_momentum_return`（12-1モメンタム）を winsorize+z標準化。`backtest.py` も同関数を as-of 日付付きで再利用（as-of検証のリークセーフ）。
- `resolve_weights()`（Issue #271）はプリセット名から重みを解決し、静的4プリセットに加え「統計的最適化」（`recommend_factor_premia.py` が永続化した Fama-MacBeth ファクタープレミアム・`get_dynamic_preset` 経由・未算出時はバランス型へフォールバック）を `backtest.py` と共用で提供。**`get_dynamic_preset` は永続化行の `preprocess_version` が `plugins/utils.py::PREPROCESS_VERSION` と一致しない限り採らない**（#517・ADR-0039）——`mean_b` の単位は推定時の断面前処理に依存し（#509 で「生スケール1単位あたり」→「1sd あたり」へ変わった）、世代印が無かったため**旧単位の重み × 新単位の特徴量**という昇格ゲート未測定の組み合わせが実際に本番へ出た（実測 rank-IC −0.0881）。不一致時は古い一致ランを探さずバランス型へ倒す（実測でフォールバック先の方が有意に良い）。
- **レスポンスに株価 as-of を同梱**（`price_freshness`＝p50/p05/max・stale_bdays・level、各行に `price_asof`＝その銘柄の最終株価日・#416）＝件数だけでは「19日古いランキング」を見分けられないため。判定軸は p50（max は少数銘柄で新しく見える）。
- **転送は `SELECT_COLS` に絞る**（Issue #441）— `financial_metrics` VIEW 97列のうち表示・フィルタ用の固定列＋`METRICS`（`RUNTIME_METRICS`＝z_momentum/mu は VIEW 外なので除く）だけを `db.query(*cols)` で引き、戻りは ORM ではなく Row タプル。列集合は METRICS から導出するので指標追加に自動追従する。株価 as-of も同様に、分位・鮮度レベルは DB 側集約（`price_freshness(db)`）、行ごとの as-of は上位 `top_n` 社だけ（`price_asof_by_code(db, codes)`）。weights のキーは `coerce_params` が METRICS へ制限し（範囲外は 400）、動的プリセットも `get_dynamic_preset` が METRICS 外の factor を落とす＝絞った列に無いキーが黙って None にならない。
- ローカル実測（2026-08-04・バランス型・top_n=30）は warm 中央値 5.93s → 4.34s／初回 38.70s → 14.88s で、候補4,249件・上位の顔ぶれは前後で一致。

依存先: `plugins/utils.py`, `database.py`

## `plugins/net_cash_analysis.py`

ネットキャッシュ分析（清原達郎『わが投資術』式）＋グレアム NCAV。NC = 流動資産 + 投資有価証券×0.7 − 総負債、NCAV = 流動資産 − 総負債。推計時価総額の崩れによる異常比率はサニティ上限で自動除外し、任意で営業CF>0等のバリュートラップ除外も可能。

依存先: `database.py`

## `plugins/sector_ols.py`

業種別OLS回帰分析（次元整合・winsorize+z-score前処理）。`heavy=True`（Render 軽量モードで 403・業種ごとの行列回帰で Render Free では OOM するためローカル実行に限定）。予測値は `regression_results` へ保存。

- **転送列は `sector_load_fields(features)` が選択 features から導出する**（#482）。`financial_records` は69列あるが、既定10項目なら20列で足りる（メタ11列＋features 由来の絶対額列）。未知キーは `ValueError` で fail-fast——列を落としたまま進むと `_resolve_per_share_value` の `getattr(..., None)` が欠測へ倒し、`_classify_by_sector` の AND フィルタが全社を除外して「業種0件」に化ける（#459 と同型で failure としては現れない）。
- **`shares_outstanding` の J-Quants マスタ経路（#462）は SQL 側の `COALESCE` で温存する**。列指定 Row にリレーションは無いので `record.company.issued_shares` は黙って None になる。`_load_records` が `COALESCE(FinancialRecord.issued_shares, Company.issued_shares)` を `issued_shares` として返すことで、`plugins/utils.py` 側は無改修のまま優先順位（XBRL期末値 → マスタ → 純資産÷BPS）が保たれる。副産物として N+1 遅延ロードが JOIN 1本になる。

依存先: `plugins/utils.py`

## `plugins/gap_analysis.py`

バリュエーション分析（割安度＋AR(1)半減期＋期待総リターン）。`gap_ratio` は `financial_metrics` VIEW（`regression_results` を JOIN）から読む。期待総リターン＝gap_ratio＋配当利回り、implied PER/PBR＝予測株価÷EPS/BPS（旧 total_return を吸収）。内部 slug・`/api/gap-analysis` は後方互換で維持・表示ラベルは「バリュエーション分析」。

依存先: `plugins/utils.py`

## `plugins/sell_ranking.py`

売り候補ランキング（保有銘柄の売り時）。買い系の逆観点（割高度 gap_ratio 反転・業績悪化・**ネットキャッシュ余力 nc_ratio 毀損**・価格モメンタム）を最新年度ユニバースで winsorize+z 標準化して合成し、相対ランキング＋SELL/REDUCE/HOLD 絶対ラベル（トレンド補正）を付与。

- `nc_ratio` は VIEW にも同名列があるが `_resolve_metric` が BS3列から実行時計算する（VIEW 側は `ROUND(...,4)` 付きで式が一致しないため・net_cash_analysis の `compute_*` を再利用）。保有は都度入力（サーバ非保存）・購入単価は損益表示のみ。`depends_on=["sector_ols"]`（gap_ratio 用）。価格モメンタムは `stock_price_weekly`。
- **転送列は `SELL_SELECT_COLS`（97列 → 18列＝表示9＋VIEW指標6＋`nc_ratio` の入力3）**（#482）。`recommend.SELECT_COLS` と同じく `SELL_METRICS` から導出するので、指標を足せば転送列が自動追従する（列リストの二重管理をしない）。
- **週次は `week_start >= today − 400日`＋500社チャンク＋3列**（#482）。400日は 52週ドローダウン（`closes[-52:]`＝364日）に余裕1ヶ月を足した値で情報損失ゼロ。下限は PK 第2列の `week_start` へ掛けて範囲スキャンにする（`trade_date` は非インデックス列）。
  - `recommend.compute_momentum_z` の `row_number() OVER`（各社最終バー1本）は**流用できない**——`_compute_trend` は13週前と52週高値を見るので系列そのものが要る。
  - `macro_snapshots.load_weekly_prices_chunked` も**流用できない**：①対象が `Company` 全社固定で保有20銘柄には過剰 ②戻り値は常に全履歴（#480 で DB からは差分だけ引くようになったが、キャッシュとマージして**返す形は全履歴のまま**＝学習用ローダーの契約は不変） ③`week_start` を返さない（#480 後も意図的にそのまま。差分の切り出しは ISO 週の不変条件を使って `trade_date` で行う） ④`_VOLUME_NOT_LOADED` 番兵は volume 用で不要。
- **μ／−R_macro 観点の出所は `mu_source` トグル**（M-1 `macro_risk_return`／M-2 `macro_gbdt`／M-3 `macro_dlm`／M-4 `macro_ensemble`／M-6 `macro_enet`＝**既定**・#396/#402）で切替——選択 producer の `read_producer_scores` を読み、未実行なら graceful-degrade（`mu_available=false`）。
- **R3 足切りゲートは `r1_prime`（M-1=予測SE／M-2・M-6=コンフォーマル区間半幅・ADR-0020/#365）で M-1・M-2・M-6 とも機能**（M-3/M-4 は r1_prime 不在で無効・`r1_prime=None` はゲート素通り）。
- **`mu_asof`（producer スコアの代表 as-of・最古・古い銘柄数）を返却**（`database.get_producer_asof`・#417。M-1 は meta の日付が推論実行日でデータ as-of ではないため None）。

依存先: `plugins/utils.py`, `database.py`, `plugins.net_cash_analysis`

---

## `plugins/macro_snapshots.py`

M-1/M-2 共有スナップショット構築モジュール（ADR-0003 §3）。

- **`_MACRO_MAP` 正本**（**#373 で ESRI 既収集の GDP 需要4項目＝民間消費/住宅投資/設備投資/公共投資を yoy で登録**＝追加収集ゼロ。四半期→月次 ffill のため週次変化が疎で M-3=DLM には不適・ADR-0012）。
- **`FIN_BASE_OPTIONS`**（**#373 で accruals / delta_roe / delta_op_margin / z_roe_sec / z_op_margin_sec を追加＝1行追加で M-1/M-2 双方へ自動反映**）。
- **`build_snapshots`**（`build_interactions`／`macro_nan_ok` フラグ。後者＝M-2 専用でマクロ欠損を NaN 保持＝企業を落とさず XGBoost に委ねる／`return_stock_ids`＝ADR-0002 M-1' per-stock 階層ベイズ専用で観測ごとの edinet_code を追加返却／`price_features`＝Issue #364 で M-2 に価格行動系 px_* を注入。momentum の直後に snap_idx 時点の既知値を追加）。
- **`build_price_features`**（px_rvol/px_volz/px_high52dev/px_rev4w の週インデックス整列事前計算。Issue #317 で M-3 に実装→#364 で M-2/M-3 共有化。M-3＝`macro_dlm.py` が re-export）。
- **`load_data`**（`with_volume` で週次 `volume_sum` の要否を切替＝`px_volz` 選択時のみ引く・Egress 削減 #446。未ロードは番兵 `_VOLUME_NOT_LOADED` で、読むと即 ValueError／**財務は `FIN_LOAD_FIELDS`（36列）だけを引き軽量 namedtuple `_FinRow` で返す**＝VIEW 全97列 22.5MB/回の削減・#459。範囲外の列を `fin_features` に渡すと `build_snapshots` が ValueError＝欠測に化けて全社が静かに消えるのを防ぐ。消費側との対応は `tests/test_macro_snapshots_loaders.py` のメタテストが CI で照合）・**`preload_macro`**（3列のみ取得）・`_realized_vol`。
- **`select_features_bic`**（pooled BIC 選択の共有実体。`macro_risk_return._select_macro_features` と `macro_beta_inference.select_shared_factors` が共用）・`producer_scores`/`get_producer_scores`。
- **`oof_backtest`**（アウトオブサンプル検証ヘルパ・ADR-0004。`cost_bps` 往復コスト控除オプション＝Issue #316 で `long_short_spread_net` を併記、M-1/M-2/M-3 共通のため1関数で全モデルへ波及）。

M-2→M-1 結合ゼロ。

**`shared_snapshot_cache()`**（`database.tuning_dry_run()` と対の `contextvars.ContextVar` パターン・ADR-0007 Update・Issue #298）: このコンテキスト内では `load_data`/`preload_macro`/`build_snapshots` の結果を小さい LRU（`_BoundedCache`・maxsize=8）でプロセス内キャッシュし、ハイパーパラメータに依存しない重複計算（DB全件ロード・スナップショット構築）を使い回す。コンテキスト未設定時（通常の `/api/plugins/{name}/run`）は常にフル計算。**利用者は2つ**——ハイパーパラメータ探索（候補間・#298/#304）と夜間スコア更新バッチ（モデル間・`nightly_scores.py`・#443。包まないとモデルを増やすたびに Supabase Egress が線形に増える）。#443 で `tuning_snapshot_cache` から改称（探索専用ではなくなったため）。

**`shared_cache_get_or_compute(namespace, key, compute)`**（Issue #304）: 同じキャッシュ機構を他モジュールから使う汎用ヘルパー。M-3（`macro_dlm.py`）の `load_prices`/`load_macro_levels` と M-1（`macro_risk_return.py`）の BIC 選択結果（`selected_names`）に紐づく Walk-Forward CV 結果（`cv_by_selected_features`）がこの名前空間を利用。

依存先: `plugins/utils.py`

## `plugins/tuning.py`

M-1/M-2/M-3 共有ハイパーパラメータ自動探索エンジン（ADR-0007・Issue #264/#298/#299）。

`SearchDim`（探索軸・`only_if` で条件付き軸を values[0] へ縮退）から grid/random で候補を生成し、各候補を `execute_plugin` でフル実行して `oof_backtest` から目的関数（`rank_ic`/`ic_ir`/`long_short`）スコアを抽出する `search()`。M-2/M-3 の producer 永続化は `database.tuning_dry_run()` で候補評価中のみ抑止。

探索ループ全体を `macro_snapshots.shared_snapshot_cache()` で包み、構造パラメータが同一の候補間で `load_data`/`preload_macro`/`build_snapshots` を使い回す（Issue #298・各プラグインの `execute()` は無改修）。**同時に `database.tuning_objective_only()` でも包む（Issue #299）**: `search()` が読むのは `oof_backtest` のみのため、このコンテキスト内では各プラグインの `execute()` が `oof_backtest` 算出直後に全社スコアリング（M-1: `_fit_final`/`_score_companies`、M-2: raw_items 構築+SHAP 計算、M-3: β経路整形+R_macro 計算）を省略して早期 return する（3プラグイン側に分岐追加・`oof_backtest` の値には影響しない）。

呼び出し元は CLI（`hyperparameter_search.py`）・GitHub Actions 月次実行（#292）のみ（Issue #293 で GUI からの手動トリガーを廃止）。M-3 の `load_prices`/`load_macro_levels` キャッシュと M-1 の BIC 選択結果紐づき CV キャッシュ（いずれも Issue #304）も同じ `shared_snapshot_cache()` の名前空間拡張で自動的に有効化される（`search()` 自体の `with` 文は #298 のまま変更なし）。

依存先: `plugins/utils.py`, `plugins/__init__.py`, `database.py`

---

## `plugins/macro_risk_return.py`

**M-1 マクロ×リスク-リターン推奨**（交差項OLS+`LassoLarsIC(BIC)`選択+OLS再フィット+Walk-forward CV）。**全社 raw を返却し JS 後処理**。`heavy=True`・`ui_order=330`。

共有ロジックは `macro_snapshots.py` に移管（ADR-0003）。**`oof_backtest` 結線済み（#272）**・`tuning_search_space()`（use_macro/use_momentum/momentum_window/min_coverage/max_features の少数軸グリッド・#265）。探索中（`shared_snapshot_cache()`）は異なる `max_features` 候補で BIC 選択結果（`selected_names`）が偶然一致した場合、後続の `walk_forward_cv_monthly()` を再実行せず使い回す（`cv_by_selected_features`・Issue #304。真の重複排除＝近似ではない）。

producer は共有 `macro_beta`（`read_producer_scores` は `macro_snapshots.get_producer_scores` の thin wrapper）。

依存先: `plugins/utils.py`, `macro_snapshots.py`

## `plugins/macro_gbdt.py`

**M-2 マクロ×財務 勾配ブースティング**（ADR-0003 / ADR-0004 / #234）。XGBoost が交互作用を自動学習。同一 fold で OLS ベースライン比較・SHAP グローバル+per-stock 全社添付。`heavy=True`・`ui_order=340`。

- **価格行動系特徴量 `price_features`（px_*・M-3 と共有・既定 OFF・#364）を `use_momentum` と同型でゲート**（`build_snapshots(price_features=...)` 経由）。
- **セクター/サイズのカテゴリ特徴量 `use_sector_features`（既定 OFF・#370）**: 業種のリークフリー target encoding（各 fold 内の業種平均リターン・`_wrap_sector_target_encoding` が per-fold fit＝リーク厳禁）＋`log_size`（log 総資産・欠損 NaN）を `execute` 内で後付け連結（`build_snapshots` 無改変＝M-1 の OLS 特徴不干渉／OLS ベースラインは sector-free）。native categorical(A) は np.array→pandas category 改修が要るため見送り target encoding(B) を採用。
- **`oof_backtest`（アウトオブサンプル検証＝無リーク OOF 予測の分位/rank-IC/LS/hit-rate＋コンフォーマル区間被覆率 `interval_coverage`・ADR-0020）を返却**し、**per-stock μ̂ と確実性軸 `r1_prime`（コンフォーマル区間半幅・#365）を `macro_gbdt_scores` へ全置換で永続化**（producer）。`produced_output`/`read_producer_scores`（M-1 と同一形）で売り推奨が `mu_source` 経由で読み、**R3 足切りゲートが機能する**。
- `tuning_search_space()`（XGBoost 7軸・ランダムサーチ既定・#266）。

依存先: `plugins/utils.py`, `macro_snapshots.py`, xgboost, shap

## `plugins/macro_dlm.py`

**M-3 ベイズ状態空間モデル（時変マクロβ DLM）**。`heavy=True`・`ui_order=360`。

銘柄ごとに週次リターンを主要マクロの週次変化へ回帰し、係数（α/β）が時間変動する動的線形モデルを自前の割引係数 DLM（West & Harrison 型・numpy）で逐次ベイズ推定。観測分散は Normal-Gamma 共役で学習し α/β の信用区間を解析的に出力。最新フィルタ α_T を年率化して µ̂ ランキング、β 経路＋1期先予測診断（校正/RMSE/カバレッジ）を返す。

- 週次変化マクロ（`_DLM_MACRO_MAP`）は M-1/M-2 の水準 YoY/Z とは別系。`load_prices`/`load_macro_levels` で価格+マクロのみロード（財務不使用）。ハイパーパラメータ探索中（`shared_snapshot_cache()`）はこの2関数も `macro_snapshots.shared_cache_get_or_compute()` 経由でキャッシュされ、候補ごとの DB 全件ロードを避ける（Issue #304。#298 は M-1/M-2 専用の `load_data`/`preload_macro`/`build_snapshots` のみが対象で M-3 はスコープ外だった）。
- カバレッジ `_MIN_FACTOR_COVERAGE`（既定0.5）未満の薄い factor は自動除外し企業母集団を factor 選択から切り離す（`diagnostics.dropped_factors`/`factor_coverage`）。
- `tuning_search_space()`（δ/β_v は既存 `_AUTO_DELTA_GRID`/`_AUTO_BV_GRID` を再利用・alpha_phi は alpha_ar1=True 時のみ有効・#267。旧 in-UI 周辺尤度 `auto_hyperparams` チェックボックスは tuned 既定値の UI 注入で役目を終え撤去・ADR-0007 改訂）。
- **producer 化済み**: µ̂ を `macro_dlm_scores` へ永続化（`replace_macro_dlm_scores`）し、`produced_output`/`read_producer_scores` は M-1 と同一形 `{edinet_code: {mu, r_macro, r1_prime}}`（mu は永続化済み `macro_dlm_scores`、r_macro は共有 `macro_beta` producer から）。sell_ranking の `mu_source=macro_dlm` で選択可。**`r1_prime` は持たないため R3 足切りゲートは無効**（素通り）。

依存先: numpy, scipy, `plugins/utils.py`

## `plugins/macro_ensemble.py`

**M-4 兄弟μ̂スタッキング・アンサンブル**（#367・ADR-0015、#397 で M-6 を基底追加）。`heavy=True`・`hidden=True`（**退役**・#570/ADR-0044）・`ui_order=370`（M-3 の後・#378）。

- **退役の根拠**: 統合は M-6 単体を上回らない（rank-IC +0.0006・p=0.810／売り側 spread p=0.655）のに、実行コストは基底 M-1+M-2+M-6 の合算。ADR-0015 本文の「上回らなければ単体で十分」判定に該当する。サイドバーと `mu_source`（`sell_ranking` / `recommend` / `templates/analysis.html` の静的 select）から外したが、**プラグイン・テスト・`COMPARISON_MODELS` の M-4 行は残す**。基底構成を変えたら `scripts/ensemble_base_bakeoff.py` か `python -m scripts.model_comparison_run` で測り直す。

- **基底は `BASE_MODELS` 定数駆動（M-1+M-2+M-6）**——レグの build/CV/現在μ̂ を名前でディスパッチするため、基底の増減は定数変更だけで済む（`scripts/ensemble_base_bakeoff.py` が構成間 OOF を同一 honest 前提で横並び実測）。重みは非負・和1（NNLS／`rank_ic_grid` は n 次元シンプレックス格子 `_simplex_grid`）ゆえ効かない基底は重み ~0 へ落ちる。M-2/M-6 は build 契約が同じで config 同値ならスナップショットを共有（`_same_build_config`）。
- M-1（BIC+OLS）と M-2（`_make_xgb_fit_predict`）と M-6（`make_elasticnet_fit_predict`）の per-(ym,銘柄) OOF を `build_snapshots(return_stock_ids=True)`＋`walk_forward_cv_monthly(return_residuals=True, embargo_months=12)` で自前再現し、(ym, edinet_code) の intersection を母集団に**二段ウォークフォワード**（月 t の統合重みは t 未満の共通 OOF だけで NNLS 学習＝無リーク・`_stack_walk_forward`）で統合。統合残差を共有 `oof_backtest` に通し model_comparison に M-4 として並ぶ。
- 現在μ̂は M-1 `_fit_final`/`_score_companies`＋M-2 全データ最終 XGB＋M-6 `_fit_final_and_score` を最終重みで合成し（結果行に内訳 `mu_m1`/`mu_m2`/`mu_m6` を併記）`macro_ensemble_scores` へ全置換永続化（producer・`tuning_dry_run` no-op）。`read_producer_scores` は M-2 と同一形＝sell_ranking の `mu_source=macro_ensemble` で利用可。
- M-3 は週次専用（ADR-0012）・M-5 は順位スコアで水準なし（ADR-0017）ゆえ基底から除外。`SCORING_SOURCES`（as-of VIEW スコア用）は対象外＝メタ検証は `oof_backtest`＋`COMPARISON_MODELS` で充足。

依存先: `plugins/macro_snapshots.py`, `plugins/macro_gbdt.py`, `plugins/macro_risk_return.py`, scipy, xgboost

## `plugins/macro_gbdt_rank.py`

**M-5 マクロ×財務 ランク学習**（learning-to-rank・#362・ADR-0017）。M-2 の rank-IC 整合版。`heavy=True`・`hidden=True`（**退役**・#570/ADR-0044）・`ui_order=380`。

- **退役の根拠（2026-08-30 実測・ADR-0017 の実測節）**: rank-IC **0.0808 vs M-2 0.1578**（差 −0.0771・95%CI [−0.0995, −0.0558]・p=0.001・17期／OOF 25,738ペア）＝ADR-0017 の「上回らなければ MSE で十分」に該当。弱いのは下位分位（最下位分位リターン 0.0145→0.0520・売り側 spread 0.0676→0.0302）。**ただし early_stopping 不使用の固定木数で正則化が M-2 と非対称**なので「learning-to-rank が無効」ではなく「この実装では下回った」と読む（再挑戦は group 付き eval_set を組んでから）。producer が無いため下流の切断は不要で、サイドバーから外しただけ。

学習目的を MSE→XGBoost の learning-to-rank（`rank:pairwise` 既定・`rank:ndcg` 選択可）へ差し替え、各 test 月を1クエリグループとして期内順位を直接最適化。M-2 を無改変ベースラインとして残すため `MacroGbdtPlugin` を継承し `execute()` 本体を共有、4フック（`_objective`/`_make_cv_callback`/`_fit_final_model`/`_persist_producer`）＋`_model_type`/`params_schema` のみ override。`walk_forward_cv_monthly(pass_train_groups=True)` で月クエリグループ境界を `XGBRanker.fit(group=…)` へ受け渡す。ラベルは pairwise＝生リターン素通し／ndcg＝期内分位グレード（`_prep_rank_labels`・2^rel 発散回避）。

予測は順位スコア（リターン単位でない）ため **producer なし**（`produced_output=False`/`read_producer_scores={}`/永続化 no-op）・**OOF 比較専用**（sell_ranking 統合は見送り）。model_comparison に M-5 として並び M-2(MSE) と純比較。

依存先: `plugins/macro_gbdt.py`, `plugins/utils.py`, xgboost, shap

## `plugins/macro_enet.py`

**M-6 マクロ×財務 正則化線形（ElasticNet・#372・ADR-0021）**。`heavy=True`・`ui_order=390`。

候補メニューの bake-off で M-2(XGBoost) を **honest OOF rank-IC で有意に上回った**（0.1713 vs 0.1419・差 +0.0294・95%CI [+0.0116,+0.0469]・p=0.002・8候補中で唯一 Bonferroni α/8 を通過）ため正式兄弟へ昇格。

- CV の fit_predict は候補実装 `model_candidates.make_elasticnet_fit_predict` を**そのまま注入**し、ADR-0021 の実測と同一コードパスであることを保証する（本番データで rank-IC 0.1713 の再現を確認済み）。スナップショット・fold（`min_train_months=6`/`step=3`/`embargo=12`）は M-2 と同値。α・l1_ratio は**学習 fold 内 `TimeSeriesSplit`** で選択（ランダム K-fold の楽観バイアス回避）。`feature_coefs` に符号付き係数（L1 でゼロ＝未使用がそのまま読める）。
- **per-stock μ̂ と確実性軸 `r1_prime`（コンフォーマル区間半幅）を `macro_enet_scores` へ全置換で永続化**（producer・#396・`tuning_dry_run` no-op）。`produced_output`/`read_producer_scores` は M-2 と同一形＝売り推奨が `mu_source=macro_enet` で読み、**R3 足切りゲートも機能する**（**既定 `mu_source`＝M-6**・#402/ADR-0022 で M-2 から切替＝売り側 OOF 指標 `short_side_spread` で有意優位）。
- `results` は上位 `top_n` のみ返す（汎用レンダラの DOM 肥大回避）。マクロ fold 内 PCA は実測で無効果のため**非搭載**。
- **`use_momentum` は既定 OFF＝実測で棄却**（ADR-0045・測定入口 `python -m scripts.momentum_gate`）。M-2/M-6 の ON/OFF を共通 (ym,ec) 域で比較して4検定すべて非有意かつ符号が負。**raw の母集団のままだと ON が改善して見える**（モメンタム不可の行＝履歴の浅い銘柄が落ちる効果）ため、この軸を測り直すときは共通月＋共通 (ym,ec) の2段の制限を外さないこと。

依存先: `plugins/model_candidates.py`, `plugins/macro_snapshots.py`, `plugins/utils.py`, scikit-learn

## `plugins/model_candidates.py`

**兄弟モデル候補メニュー（#372・ADR-0021・探索枠）**。`walk_forward_cv_monthly(fit_predict=…)` 注入点へ差し込む候補の `fit_predict` ファクトリ集約。

- ElasticNet（`ElasticNetCV`＋fold内 `TimeSeriesSplit`）
- ExtraTrees（葉モーメントの全分散分解＋木予測分位の2種予測区間を診断出力）
- Fama-MacBeth 予測ヘッド（`pass_train_groups=True` で月境界を受け、断面定数のマクロ列を自動除外→`recommend_factor_premia.fama_macbeth_regression`／Ridge 版は `average_premia` を共有）
- regime-switch 閾値線形（学習 fold 中央値で VIX 分割・regime 別 Ridge・pooled フォールバック）
- LightGBM・CatBoost（**任意依存**・`requirements-optional.txt`・未導入なら `candidate_available()` が False）
- XGBoost(M-2) 基準線

`wrap_macro_pca` はマクロ列だけを **fold 内 PCA** で直交少数因子へ畳む合成可能ラッパー（任意候補に被せられる／`*rest` 素通しで3引数候補にも対応）。前処理は全て学習 fold 内 fit（`fit_feature_columns`/`transform_feature_row` 流用）。

**プラグインではない**（`plugin` 属性を持たないためレジストリ非登録・API 非公開）。

依存先: `plugins/utils.py`, `recommend_factor_premia.py`, scikit-learn, (lightgbm/catboost 任意)
