# ハイパーパラメータ探索の実行手段を GUI 手動トリガーから GitHub Actions 月次自動実行へ一本化する

## Status

accepted（2026-07-10）。実装は GitHub Issue #291（品質ゲート）・#292（月次自動実行ワークフロー・
PR #296）・#298（snapshot キャッシュ・PR #300）・#293（GUI手動トリガー廃止・PR #301）・
#294（GUI自動プリフィル・PR #302）。本 ADR はドキュメント整備の Issue #295 で作成。

## Context

ADR-0007 で導入した共有探索エンジン（`plugins/tuning.py` + `hyperparameter_search.py`）は
当初 `/analysis` の各タブに「探索を開始」ボタン（`tune-start`/`tune-stop`/`tune-stream`
エンドポイント・SSE進捗配信）を持ち、ユーザーがブラウザから手動で探索を起動する設計だった。

この GUI 手動トリガー方式には2つの実運用上の問題があった。

1. **ローカル環境限定**: 探索は `execute_plugin()` を候補ごとにフル実行する brute-force
   方式（ADR-0007）で、特に M-2（`macro_gbdt`）は XGBoost のランダムサーチが重く、
   ローカル実測で `n_iter=200` 相当が4〜8時間かかる。ブラウザセッションを開いたまま
   ローカル PC を長時間占有する運用は非現実的だった。
2. **再現性・定期性の欠如**: 「気が向いたときに手動で回す」運用では、マクロ環境や銘柄
   構成の変化に対してハイパーパラメータが追随するタイミングが不定期になり、
   `plugin_tuned_params` の鮮度（`data_fingerprint`）が長期間更新されないリスクがあった。

`macro-beta-inference.yml`（ADR-0002 の PyMC 階層ベイズ推論バッチ）が既に確立していた
「Render では動かせない重いバッチを GitHub Actions 上で実行し、本番 Supabase へ直接
永続化する」パターンを、ハイパーパラメータ探索にも適用できることが分かった。

## Decision

1. **月次 cron 自動実行＋ matrix 並列3ジョブ**（`.github/workflows/tune-hyperparameters.yml`・
   Issue #292）。`schedule: cron: '0 3 1 * *'`（UTC 03:00 = JST 12:00、毎月1日）に加え
   `workflow_dispatch` で即時手動実行も可能にする。M-1（`macro_risk_return`）・
   M-2（`macro_gbdt`）・M-3（`macro_dlm`）を独立ジョブに分け（`fail-fast: false`）、
   1モデルの失敗・品質ゲートスキップが他モデルの正常完了を妨げないようにする。
   独立ジョブに分けることで、モデルごとに適切な `timeout-minutes` を設定できる。

2. **探索戦略はモデル特性で使い分ける**。`macro_risk_return`/`macro_dlm` は探索空間が
   小さいため `--strategy grid`（実測10〜60分）。`macro_gbdt` は7軸の組合せ爆発を
   避けるため `--strategy random --n-iter 150`。150 という値は、ローカル実測で
   `n_iter=200` 相当が4〜8時間かかると判明したのに対し、GitHub Actions ホスト型
   ランナーのジョブ実行時間ハード上限が6時間であるため、上限に収まる安全マージンを
   確保する目的で抑えた値（`timeout-minutes: 355`＝6時間ギリギリを避ける）。

3. **`macro_risk_return`/`macro_dlm` の `timeout-minutes` は当初90分→240分へ調整した**
   （commit `8bb7d14`）。ローカル実測「10〜60分」を根拠に当初 `timeout-minutes: 90` と
   していたが、これはローカル8コア環境での実測値であり、GitHub Actions ホスト型ランナーは
   実質2コア程度（`macro-beta-inference.yml` の既存コメントにも同様の指摘あり）のため
   所要時間が想定より伸びる。2026-07-09 の初回 `workflow_dispatch` 実行で実際に両ジョブとも
   `timeout-minutes: 90` に達し `cancelled` となったため、240 へ引き上げた。

4. **品質ゲート（Issue #291・ADR-0007 Update）を人手レビュー無し運用の安全弁とする**。
   `run_search(persist=True)` は `plugin_tuned_params` に既存行があれば、その
   `objective_value` と今回の `best_score` を比較し、劣化していれば `upsert_tuned_params`
   （`persist_scores=True` 併用時の producer スコア永続化を含む）をスキップし
   `SystemExit` で非ゼロ終了する（初回＝該当行なしはゲート対象外）。ジョブは `failed`
   扱いになり GitHub 標準の失敗通知（メール等）が飛ぶ——これは意図した挙動であり、
   `continue-on-error` 等で握りつぶさない。月次自動実行を無人で回すにあたり、探索対象
   データの一時的な劣化（例: 特定期間のデータ品質低下・外れ値混入）が本番値を
   悪化させたまま上書きするリスクを先に塞ぐ。

5. **snapshot キャッシュ（Issue #298・PR #300）で timeout を安定的に回避する**。
   `tune-hyperparameters.yml` の本番初回実行で M-1 が `timeout-minutes=240` でも
   完走しなかった原因を `/diagnose` で特定した結果、`execute_plugin()` が候補ごとに
   毎回呼ぶ `load_data`（DB全件ロード）・`build_snapshots`（特徴量スナップショット構築）
   が探索軸に依存しない重複計算（M-1 は288候補中48通りしか構造パターンが無く6倍の
   重複計算）だったことが判明した。`plugins/macro_snapshots.py::tuning_snapshot_cache()`
   （`database.tuning_dry_run()` と対になる `contextvars.ContextVar` パターン）を追加し、
   各プラグインの `execute()` を一切変更せずに `load_data`/`preload_macro`/
   `build_snapshots` の結果をプロセス内 LRU キャッシュ（`maxsize=8`）で候補間再利用する
   ことで、3モデルとも `timeout-minutes` 内で正常完走することを確認した。

6. **GUI 手動トリガーを廃止し、読取専用バッジ＋自動プリフィルへ一本化する**
   （Issue #293・#294）。`routers/analysis.py` の `tune-start`/`tune-stop`/`tune-stream`
   エンドポイントと `/analysis` の探索パネル UI を削除。実行手段を GitHub Actions
   （月次自動 + `workflow_dispatch`）のみに一本化する。`GET /api/plugins/{name}/tuned`
   （読取専用・軽量）は維持し、`static/js/analysis.js` の `_loadTunedBadge()` が
   ページ読込時に自動で `applyTunedParams()` を呼びフォームへ反映するよう変更した
   （従来「調整値を読込」ボタンだったものは「初期値にリセット」に役割変更し、
   自動反映後にユーザーが手動で戻したい場合の導線として残す）。

## Considered Options

- **GUI 手動トリガーと GitHub Actions 自動実行を併存させる**（却下）：実行手段が複数
  あると「今どちらの経路で最後に調整されたか」が分かりにくくなり、`plugin_tuned_params`
  が最新1件のみを保持する設計（ADR-0007 項目6）と噛み合わせるための追加の出所管理
  （UI起動かActions起動かのラベリング等）が必要になる。保守コストに対して得られる
  価値（ユーザーが即座に手元で1回だけ回したいケース）は小さいと判断し、`workflow_dispatch`
  による即時手動実行で代替可能なため一本化した。
- **`timeout-minutes` を最初から余裕を持った値（240/355）に設定する**（見送り）：
  本番実行時間の実測データが無い段階で過大な値を設定すると、無限ループ等の異常時に
  ジョブが不必要に長時間コストを消費するリスクがある。ADR-0007 の「正しさを優先し
  実際に遅すぎると判明してから最適化する（YAGNI）」の方針を踏襲し、まず控えめな値
  （90分）で実行し、実測に基づいて調整する段階的アプローチを採った。
- **snapshot キャッシュを月次自動実行の導入と同時に実装する**（見送り）：ADR-0007
  時点では「3モデルの `build_snapshots`/`execute()` 呼び出しパスへの侵襲的変更を要し、
  既存の厳密なテスト網を壊すリスクがある」として意図的に見送っていた。実際に
  timeout が発生してから対応することで、本当に必要な最小限の変更（各プラグインの
  `execute()` は無改修）に絞り込めた。

## Consequences

- **新規ワークフローファイル**: `.github/workflows/tune-hyperparameters.yml`
  （matrix 3ジョブ・月次 cron + `workflow_dispatch`）。
- **新規テーブルなし**: `plugin_tuned_params` は ADR-0007（Issue #264）で既に導入済み。
  本 ADR による新規スキーマ変更は無い。
- **削除された API エンドポイント**: `POST /api/plugins/{name}/tune-start`・
  `POST /api/plugins/{name}/tune-stop`・`GET /api/plugins/{name}/tune-stream`
  （`routers/analysis.py`）。維持されるのは `GET /api/plugins/{name}/tuned`（読取専用）。
- **GUI の変化**: `/analysis` の探索パネル UI が削除され、各タブのバッジがページ読込時に
  自動で最新の調整済みパラメータをフォームへ反映する。ユーザーが探索そのものを
  ブラウザから起動する手段は無くなり、`workflow_dispatch` での手動実行（GitHub UI/CLI）
  のみが即時実行の経路になる。
- **運用**: 月次自動実行の失敗・品質ゲートスキップは GitHub Actions の標準失敗通知で
  検知する（DEPLOYMENT.md に運用手順を記載）。ログは `actions/upload-artifact` で
  30日間保持され、`workflow_dispatch` で任意タイミングの再実行が可能。
- **将来エンハンス**: ADR-0007 で言及した CV/BIC/OLS 段階の高速化（Issue #299）は
  本 ADR のスコープ外。`macro_gbdt` の `n_iter=150` は6時間上限に収める安全側の値で
  あり、ランナー性能やコスト事情が変われば再調整の余地がある。
