# M-1/M-2/M-3 共有ハイパーパラメータ自動探索エンジン（walk-forward OOF rank-IC を目的関数に採用）

## Status

accepted（2026-07-05）。実装は GitHub Issue #264（基盤）・#265（M-1）・#266（M-2）・#267（M-3）、
前提として #272（M-1 の oof_backtest 結線）を含む。

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
   UI は読取専用の `GET /api/plugins/{name}/tuned` バッジ＋「調整値を読込」ボタン
   （フォームへの反映のみ・再計算はしない）を持つ。

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
