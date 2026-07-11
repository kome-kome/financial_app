# M-1/M-2/M-3 共有ハイパーパラメータ自動探索エンジン（walk-forward OOF rank-IC を目的関数に採用）

## Status

accepted（2026-07-05）。実装は GitHub Issue #264（基盤）・#265（M-1）・#266（M-2）・#267（M-3）、
前提として #272（M-1 の oof_backtest 結線）を含む。

**superseded by 0010**（2026-07-10・GUI手動トリガー廃止・GitHub Actions月次自動実行へ移行）。
本 ADR が前提としていた「GUI から探索を手動起動する」実行手段は Issue #293 で廃止された。
共有探索エンジン（`plugins/tuning.py`）自体の設計（目的関数・探索空間宣言・dry-run 永続化
抑止等、以下 Decision の各項目）は現行実装の正本のままであり、本文は履歴として保持する。
実行手段の変更・品質ゲート追加・snapshot キャッシュ化の経緯は
[0010](0010-hyperparameter-tuning-github-actions-automation.md) を参照。

## Context

M-1（`macro_risk_return`）・M-2（`macro_gbdt`）・M-3（`macro_dlm`）は多数のハイパーパラメータを
持ち、`/analysis` の各タブで毎回手動チューニングしていた。特に M-2（XGBoost）は9パラメータ
あり、最適点は探索しないと分からず手動チューニングは再現性・客観性に欠ける。

3モデルとも既に walk-forward CV の無リーク OOF 予測から `oof_backtest`（分位リターン・
rank-IC・ロングショート spread・hit-rate）を算出する仕組みを持つ（ADR-0004）。ただし実装調査
時点では M-1 だけ `execute()` がこれを返しておらず（cv_residuals_by_ym は既に計算済みで
`oof_backtest()` に渡すだけの1行差分だった＝#272）、このギャップを埋めない限り探索エンジンが
「M-1だけ特別扱い」を要する非対称な設計になる。

唯一の既存自動探索は M-3 の `auto_hyperparams`（δ/β_v を**周辺尤度**でグリッド選択・
`_AUTO_SAMPLE_N=50` 銘柄）のみで、(a) 2軸限定、(b) 目的が in-sample 周辺尤度（OOF ではない）、
(c) M-1/M-2 に流用不可、という3つの限界を持つ。

## Decision

1. **目的関数は共有 walk-forward OOF 指標**（`oof_backtest` の `rank_ic.mean`（既定）/
   `rank_ic.mean/std`（`ic_ir`）/ `long_short_spread`）を採用する。3モデルとも
   `execute()` が同じ形の `oof_backtest` を返す（#272 で M-1 も統一）ため、探索エンジン
   （`plugins/tuning.py`）はモデル固有の特殊処理を持たず、`execute_plugin()` の戻り値から
   一様にスコアを読む。

2. **候補評価は各モデルの `execute_plugin()` をフル実行**する（brute-force）。スナップショット
   再構築の再利用（構造パラメータ固定時に1回だけ構築するキャッシュ）は主要な高速化レバーとして
   認識しつつ、3モデルの `execute()` は既にテストで厳密に守られた複雑なロジックのため、今回は
   侵襲的リファクタを避け正しさを優先する（実際に遅すぎると判明してから最適化する・YAGNI）。
   速度は呼び出し側の `--n-iter`／`--strategy` で調整する。

3. **producer 永続化は探索中 dry-run で抑止する**。M-2/M-3 の `execute()` は末尾で
   producer スコア（`macro_gbdt_scores`/`macro_dlm_scores`）を全置換永続化するが、探索は
   候補ごとに同じ `execute()` を呼ぶため、対策なしでは本番テーブルが中間的な（最適でない）
   候補予測値で都度上書きされる。`database.py` に `contextvars.ContextVar` ベースの
   `tuning_dry_run()` を追加し、`replace_macro_gbdt_scores`/`replace_macro_dlm_scores` を
   このコンテキスト内では no-op にする。最終選定後の本採用実行（`--persist-scores`）のみ
   このコンテキスト外で呼び、実際に永続化する。

4. **探索空間はモデルごとに `tuning_search_space()`** （`AnalysisPlugin` の任意拡張メソッド・
   `read_producer_scores` と同様に抽象基底には追加しない）で宣言し、`(base_params, dims)` を
   返す。`SearchDim.only_if` で条件付き軸（例: M-3 の `alpha_phi` は `alpha_ar1=True` の
   ときのみ意味を持つ）を表現し、条件を満たさない部分 combo ではその軸を `values[0]` に
   **縮退**させる（除外ではない＝`alpha_ar1=False` という有効な候補自体は失われない）。

5. **チューニング対象から表示専用パラメータを除外**する：`lambda_risk`（ユーザー選好。実現
   リターンに対して最適化すると λ→0 に潰れる）・`top_n`・`r3_gate`・`risk_axis` は fit ではなく
   選好/表示のため、探索空間から常に除外しユーザー操作に残す。`fin_features`/`macro_features`
   の部分集合探索は 2^N で不可能なため対象外とし、`use_macro`/`use_momentum` 等のチャネル
   単位トグルに縮約する（M-1/M-2）。M-3 の `macro_features` 部分集合探索は同様の理由に加え
   具体的な縮約案が無いため、既定値に固定し対象外とする。

6. **永続化は `plugin_tuned_params`（plugin_name 単位・最新1件のみ）**。`params_json` に
   best params、`leaderboard_json` に上位20件のみ（肥大化防止）、`data_fingerprint`
   （最終 trade_date＋行数のハッシュ）で「古いデータで調整済み」を UI が警告可能にする。
   UI は読取専用の `GET /api/plugins/{name}/tuned` バッジを持ち、調整済み値があれば
   ページ読込時にフォームへ自動反映する。手動で値を変更した後に調整済み値へ戻すための
   「初期値にリセット」ボタンも持つ（いずれもフォームへの反映のみ・再計算はしない）。

## Considered Options

- **inner-CV グリッド探索を各プラグインの `execute()` 内部に個別実装**（却下）：3モデルで
  目的関数・探索ループが重複し、比較可能性（同一指標での横並び）が保証されない。共有エンジン
  なら「同一 rank-IC 定義で3モデルを比較できる」という設計目標（ADR-0004 の思想の延長）を
  自然に満たす。
- **スナップショット再利用の高速化を初版から実装**（見送り）：構造パラメータとモデルパラ
  メータを分離し、構造固定時はスナップショットを1回だけ構築して使い回す設計は理論上大きな
  高速化になるが、3モデルの `build_snapshots`/`execute()` 呼び出しパスへの侵襲的変更を要し、
  既存の厳密なテスト網を壊すリスクがある。正しさを優先し、`--n-iter` で速度をユーザー制御
  可能にする現行案を採用。将来遅すぎると判明すれば別 Issue で対応する。
- **producer 永続化をモンキーパッチで抑止**（却下）：`unittest.mock.patch` を本番コードパスで
  使うのはテスト専用の技法を本番ロジックに持ち込む臭いがある。`contextvars.ContextVar` は
  「ある処理中は特定のモードが有効」を表現する標準的な機構であり、`replace_macro_*_scores`
  側に明示的な分岐として現れるため発見しやすい。
- **M-3 の既存 `auto_hyperparams`（周辺尤度）を rank-IC ベースへ置き換え**（却下）：後方互換を
  崩し、`_AUTO_SAMPLE_N=50` 銘柄のみで完結する高速な in-UI フォールバックの価値を失う。
  周辺尤度モードは維持し、rank-IC ベースの探索は CLI 経由の別経路として追加する。

## Consequences

- **新規テーブル `plugin_tuned_params`**（`Base.metadata.create_all` で生成・DDL/マイグレーション
  不要）。
- **新規モジュール `plugins/tuning.py`・新規 CLI `hyperparameter_search.py`**（ローカル専用・
  新規 pip 依存なし）。
- **`database.py` に `tuning_dry_run()` という新しい「モード」概念が加わる**：以後、producer
  永続化を行う新しいプラグインを追加する際は、同様に dry-run 対応を検討すること。
- **速度はユーザー制御**：M-2 の7軸は組合せ爆発するためランダムサーチが既定。M-3 は
  全銘柄で DLM フィルタを回すため候補数が多いと実行時間が伸びる（`--n-iter`/`--strategy grid`
  の小さめグリッドで様子を見ることを推奨）。
- **将来エンハンス**：スナップショット再利用によるモデルパラメータ探索の高速化、M-1/M-2 の
  `fin_features`/`macro_features` をチャネル単位トグルより細かく探索する仕組み、M-3 の
  `macro_features` 部分集合探索、探索の多重比較問題への対処（1-SE ルール的な倹約選択の
  UI 表示）。

実装・調査タスクは GitHub Issue #264/#265/#266/#267（および前提 #272）で追跡する。

## Update（2026-07-09・Issue #291）

項目6の永続化に**品質ゲート**を追加した。`run_search(persist=True)` は `plugin_tuned_params` に
既存行があれば、その `objective_value` と今回の `best_score` を比較し、劣化していれば
`upsert_tuned_params`（`persist_scores=True` 併用時の producer スコア永続化を含む）を
スキップする（初回＝該当行なしはゲート対象外で常に persist）。CLI はスキップ時に非ゼロ終了する。
GitHub Actions での月次自動実行（Consequences 節「将来エンハンス」で言及した運用像）を人手
レビュー無しで回すにあたり、探索対象データの一時的な劣化が本番値を悪化させたまま上書きする
リスクを先に塞ぐ目的。

## Update（2026-07-10・Issue #298）

Considered Options で「見送り」としていた**スナップショット再利用の高速化を実施した**。
`tune-hyperparameters.yml`（#292）の本番初回実行で M-1（`macro_risk_return`）が
`timeout-minutes=240` でも完走しなかったため、`/diagnose` で原因を特定：`plugins/tuning.py`
の `search()` が候補ごとに `execute_plugin()` をフル実行するが、M-1/M-2 の `execute()` が
毎回呼ぶ `load_data`（DB全件ロード）・`build_snapshots`（特徴量スナップショット構築）は
いずれも探索軸（モデルのハイパーパラメータ）に依存しない処理で、構造パラメータ
（`fin_features`/`macro_features`/`use_momentum`/`min_coverage` 等）が同一の候補間では
結果が完全に一致していた（実測: `load_data` 約23〜33秒 + `build_snapshots` 約25秒 ≒
1候補あたり55〜65秒とほぼ一致）。M-1 は288候補中48通りしか構造パターンが無く、
6倍の重複計算が timeout の主因と判明した。

対応は当初見送った理由（「3モデルの `build_snapshots`/`execute()` 呼び出しパスへの侵襲的
変更を要し、既存の厳密なテスト網を壊すリスクがある」）を、**各プラグインの `execute()` を
一切変更しない**設計で解消して実施した。`database.tuning_dry_run()` と対になる
`contextvars.ContextVar` パターンを `plugins/macro_snapshots.py` に追加し
（`tuning_snapshot_cache()`）、`load_data`/`preload_macro`/`build_snapshots` の内部実装
だけを「コンテキストが有効なら結果をプロセス内メモリキャッシュ（小さい LRU・maxsize=8）
から返す」ように変更した。`plugins/tuning.py::search()` は探索ループ全体を
`with tuning_snapshot_cache():` で包むだけ（1箇所の変更）。各プラグインの `execute()` 内の
呼び出し方（引数・呼び出し順）は無改修のため、既存テストは無傷（959件 green）。コンテキスト
未設定時（通常の `/api/plugins/{name}/run`）は常にフル計算し、キャッシュの副作用は漏れ出さない。

実測効果（本番DB・読取専用プロファイリング・persist なし・M-1 `max_features` 6値グリッド
`[5, 10, 15, 20, 30, 40]`・`use_macro`/`use_momentum`/`min_coverage` は既定値に固定）は
Issue #298 のコメント・PR 本文に Before/After 秒数を記録した。要旨:

- **`load_data`/`preload_macro`/`build_snapshots` 単体**: 6回連続呼び出し 296.88秒
  （キャッシュ外・#298導入前と等価）→ `tuning_snapshot_cache()` 内で同じ6回を実行すると
  54.20秒（初回のみフル計算・2〜6回目はキャッシュヒットでほぼ0秒）＝ **5.5倍高速化**。
  呼び出し回数は6回→1回に削減。これは Issue #298 が対象とした重複計算そのものであり、
  設計どおりに機能していることをモック（ユニットテスト）だけでなく本番データでも確認した
  （`_load_data_impl`/`_preload_macro_impl`/`_build_snapshots_impl` の実行回数を計測し、
  いずれも3候補の探索で通算1回のみだったことを直接確認）。

- **`search()` 経由のフルパイプライン（M-1 `execute()` 全体・6候補）**: 405.05秒。
  理論値ほどの短縮にならなかった。内訳を分解すると、スナップショット構築コスト
  （キャッシュにより1回のみ発生）は54.20秒だが、残り350.85秒は BIC特徴量選択
  （`LassoLarsIC`）・Walk-Forward CV（`min_train_months=6, step_months=3`・数十フォールド
  で `winsorize`/`normalize` を純 Python ループで実行）・最終 OLS 再学習・全社スコアリング
  が占めていた（1候補平均約58秒）。この部分は `max_features` に応じて BIC が選ぶ特徴量集合
  （`selected_names`）自体が変わるため**原理的に候補間で使い回せず、#298 のスコープ外**
  （キャッシュ対象外は設計上正しい）。つまり **M-1 の `execute()` には、探索軸に依存しない
  `load_data`/`build_snapshots`（#298 で解消）とは別に、探索軸に依存する高コストな
  CV/BIC/OLS 段階が存在し、後者は前者と同程度かそれ以上に重い**。

  この結果、フルパイプラインの高速化率は Before（推定・スナップショット部分は実測296.88秒＋
  CV/BIC/OLS部分は405.05−54.20=350.85秒から逆算した推定647.73秒）に対し After（実測
  405.05秒）で **約1.6倍**にとどまる（288候補全数ではなく6候補サブセットでの推定値）。
  Issue #298 が見積もった「6倍高速化・336分→56分」は load_data/build_snapshots のみを
  対象にした試算であり、CV/BIC/OLS 段階の重さを織り込んでいなかったため、288候補フル
  グリッドの実行時間は本 Update 後も `timeout-minutes=240` を超過するリスクが残る
  （タイムアウト値の再調整・CV/BIC/OLS 段階自体の高速化は本 Issue のスコープ外とし、
  発見した新しいボトルネックは Issue #299 で追跡する）。

## Update（2026-07-11・Issue #299）

Issue #298 が特定した「CV/BIC/OLS/スコアリング段階」のうち、**探索候補の評価そのものには
不要な部分（oof_backtest 算出後の全社スコアリング）を省略する**設計フェーズ分の対応を実施
した（同 Issue 本文のうち winsorize ベクトル化・CV フォールド数削減等は段階的対応の方針
によりスコープ外とし、まずスコアリング省略のみを実装・実測した）。

`plugins/tuning.py::search()` が候補評価から読むのは `execute_plugin()` の戻り値のうち
`oof_backtest` のみであり、それ以外（`results`＝全社スコア等）は完全に破棄されているにも
かかわらず、3モデルとも `execute()` は毎候補で全社（約4000社）分の最終スコアリングまで
フル実行していた:

- **M-1**（`macro_risk_return`）: `oof_backtest` 算出直後に `_fit_final`（全データでの最終
  OLS 再学習）・`_score_companies`（全社スコアリング）へ進んでいた。
- **M-2**（`macro_gbdt`）: `oof_bt` の算出（`oof_backtest(cv_residuals_xgb, ...)`）が、
  全社 raw_items 構築＋SHAP 計算（`shap.TreeExplainer`・高コスト）より**後**のコード順序に
  あったため、単純に「算出後に return」するだけでは足りず、`oof_bt` の算出位置を
  `cv_folds_xgb`/`cv_residuals_xgb` が揃った直後（OLS ベースライン CV・最終モデル再学習・
  SHAP 計算より前）へ移動するコード再編を行った。
- **M-3**（`macro_dlm`）: per-company ループが `dlm_filter`（1銘柄1フォワードパス）の
  呼び出しから OOF 残差と最終推定値（µ̂・β経路・R_macro）を**同一パスで**生成するため、
  M-1/M-2 と異なり `dlm_filter` 自体の呼び出しは省略できない。ループ内で OOF 残差収集
  （`oof_residuals.setdefault(...)`）の直後に分岐し、β経路整形・R_macro計算（`np.cov`）・
  1期先診断集計・`rows` へのフル dict 構築のみをスキップする（`rows` には件数カウント用の
  最小限プレースホルダのみ追加）設計とした。ループ後の top_n 経路構築・診断集計・producer
  永続化も同様にスキップする。

**設計**: `database.py` に `tuning_dry_run()`・`plugins/macro_snapshots.py` の
`tuning_snapshot_cache()`（Issue #298）と同じ `contextvars.ContextVar` パターンで
`tuning_objective_only()` を追加した（配置は依存の向き上 `database.py`。各プラグインは
既に `database` を import 済みのため）。`plugins/tuning.py::search()` の探索ループを
`with tuning_snapshot_cache(), tuning_objective_only():` で両方同時に包む。3プラグインの
`execute()` は `oof_backtest` 算出直後に `database.is_tuning_objective_only()` を見て、
真なら全社スコアリングをスキップし、`execute_plugin()` の戻り値契約（`hyperparameter_search.py`
のログ出力・テスト等が依存するキー）を壊さない最小限のプレースホルダ（`results: []`・
`n_companies: 0`・`feature_coefs: {}` 等）を含む dict を早期returnする。通常の API 実行
（`/api/plugins/{name}/run`、コンテキスト無効時）はこの分岐に入らず常にフル計算する。
best params での本採用実行（`hyperparameter_search.py::run_search` の `persist_scores=True`
時の `execute_plugin()` 呼び出し）は `search()` の `with` ブロックの外側で呼ばれるため、
このコンテキストは無効＝フルスコアリングされる（producer 永続化が正しく行われることを担保）。

**実測**（本番DB・読取専用・`persist=False`・M-1 `max_features` 6値グリッド
`[5, 10, 15, 20, 30, 40]`・`use_macro`/`use_momentum`/`min_coverage` は既定値固定・
Issue #298/#299 と同一手法）:

- **M-1 フルパイプライン（6候補）**: Before（Issue #298 の実測・#299 未実装）405.05秒 →
  After（本 Update・#299 実装後）**266.65秒**。**約1.52倍高速化（34.2%削減）**。
  3候補サブセットでは138.20秒（1候補平均46.07秒。6候補では1候補平均44.44秒とほぼ整合）。
  oof_backtest の値（`best_score`=0.2563・`best_params`）は Before/After で完全一致
  （スコアリング省略が目的関数算出に一切影響しないことをテスト
  `TestObjectiveOnlyMode::test_oof_backtest_identical_with_and_without_objective_only`
  でも確認済み）。
- **M-2（`macro_gbdt`）簡易確認**: `n_iter=3`（random）で92.74秒（1候補平均30.91秒）。
  `shap.TreeExplainer` が探索中は一切呼ばれないことをユニットテストで確認。
- **M-3（`macro_dlm`）簡易確認**: `n_iter=4`（random）で215.54秒（1候補平均53.89秒）。
  M-3 は `dlm_filter` 自体の呼び出しを省略できない構造（上記）のため、M-1/M-2 ほどの
  相対的な削減効果は見込めない（`load_prices`/`load_macro_levels` も #298 の
  `tuning_snapshot_cache()` の対象外＝候補ごとにDB全件ロードが残る。Issue #304 で追跡）。

**残課題**: 6候補サブセット（構造パターン1通り）の改善率（×1.52）を Issue #299 本文が
見積もった「288候補×平均~108秒/候補（#298後・#299前）＝約8.6時間」に単純比例させると、
288候補（構造パターン48通り）の推定所要時間は約5.7時間（約340分）となり、
`timeout-minutes=240` を依然として超過するリスクが残る（この外挿は構造パターンが1通りの
測定を48通りに単純比例させた粗い推定であり、正確な検証には288候補フル実行またはGitHub
Actions月次自動実行の実績ログが必要）。Issue #299 本文が当初スコープ外とした改善案
（winsorize/normalize のベクトル化・CVフォールド数削減・BIC選択コストの分離計測・
M-1のCV/OLS結果キャッシュ・M-3のload_prices/load_macro_levelsキャッシュ化）は
Issue #304 で追跡する。
