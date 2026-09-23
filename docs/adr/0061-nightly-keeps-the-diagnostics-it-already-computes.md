# 夜間バッチは計算済みの診断値を捨てずに積み、測る実験は足さない

## Status

accepted（2026-09-23）。Issue [#726](https://github.com/kome-kome/financial_app/issues/726)。

## Context

「日次バッチにハイパーパラメータの計測を入れて今後の改善につなげたい」という相談から始まった。
調べると、**新しい計測を足す前に、夜間バッチが毎晩すでに計算している診断値を捨てていた**。

- 夜間の scores ステップ（`nightly_scores.py`）は、`macro_enet`（M-6）の walk-forward OOF 成績
  （rank-IC・業種中立 rank-IC・期別 rank-IC・long-short など）、CV が選んだ α と l1_ratio（fold ごとと
  最終モデル）、係数を毎晩計算している。`sector_ols` は業種ごとに ridge の α を LOO で選び直している
- `_summarize` は dict の値を落とし、list は件数にするので、夜間ログにも残らない（2026-09-22 の
  夜間ログで α と OOF を検索すると0件）。保存先の表も無い——`macro_enet_scores` は μ̂ だけを全置換し、
  `regression_results` は gap_ratio だけを持つ
- 残す価値の根拠は既にあった。[ADR-0058](0058-ridge-alpha-is-chosen-by-loo.md) の実測では、33業種のうち
  3業種（機械 208社を含む）の ridge α が候補の下端 0.001 に張り付いていた。端への張り付きは
  「候補の外にもっと良い α がある」ことを示しうるが、毎晩それを確かめる手段が無かった
- 時間は余っている（窓360分に対して夜間の実測は 25〜38分）。ただし重い計算は日中キューへ積む
  運用で（ADR-0038・#618）、夜間に実験を足すとその線引きが崩れる
- リポジトリは public

## Decision

1. **中身は捨てていた値の保存だけ。追加の計算はしない。** 固定値のハイパラ（winsorize の分位・
   `min_coverage`・縮約閾値など）を振って比べる感度掃引は夜間に入れない。1晩に何本も比べると偶然の
   差を拾いやすく（多重比較）、重い計算は日中キューという線引きとも衝突する
2. **保存先はローカル PG の追記専用の表 `nightly_model_diagnostics`**（run_id × model で1行・診断は
   JSON 列）。前例は `macro_beta_meta` の run ごとの JSON。SQL で期間を切って読め、`run_backup` に含まれる。
   ミラーの `SYNC_PLAN` へ1行足した（`created_at` の高水位）
3. **読み手は読み出し CLI（`python -m scripts.nightly_diag_report`）だけ。自動起票はしない。**
   α が候補の端に張り付いた夜は夜間ログに WARN を出す。警報の基準は、実測が溜まってから別の Issue で
   決める（先に閾値を置くと根拠のない値になる・ADR-0042 の「閾値は約束から導出する」）。公開 Issue に
   診断値の数値を載せないことも兼ねる
4. **書き込みに失敗した夜は scores を非ゼロで終える。** μ̂ / gap_ratio の永続化と検証が済んだ後にだけ
   書くので、失敗しても本番のスコアは巻き戻さない。失敗のラベルは `<model>:diagnostics` で、
   モデル自体の失敗と区別する
5. **見張りはその場の検証とテスト。`batch_freshness.PRODUCERS` には載せない。** 書いた直後に
   `(run_id, model)` の行と `created_at` を直接クエリで確かめる。PRODUCERS に載せると、書き込み失敗の夜に
   「夜間バッチ失敗」と「診断値が前進していない」の Issue が同じ原因で2枚立つ（`sector_ols` を
   PRODUCERS の対象外にした理由と同じ）。PRODUCERS にしか拾えない「書く処理ごと消える」事故は、
   `tests/test_nightly_scores.py::TestDiagnosticsRegistry`（`NIGHTLY_MODELS` の全部に抽出器がある）と
   `TestDiagnosticsRecording`（`run_models` が1モデル1行書く）で CI が止める
6. **一緒に残す文脈は軽いものだけ**: git の HEAD と未コミット変更の有無（夜間バッチは作業ツリーを
   そのまま import する）、`PREPROCESS_VERSION`、学習サンプル数、`snapshot_date`。入力行列のハッシュは
   取らない。そのため CLI の `no-context-change`（どれも変わっていないのに動いた夜）は「#697 型の
   並び依存の候補」であって断定ではない
7. **抽出はモデルごとの allowlist**（`nightly_scores.DIAG_EXTRACTORS`）。結果の dict を丸ごと入れない——
   `results` の社別の行・社名が入り、何が入っているかを誰も説明できなくなる。PostgreSQL の JSON は
   NaN を受け付けないので `_json_safe` で None にしてから書く

端の判定に要る値はプラグイン側が返す（出力のキーを足すだけで、μ̂ は変わらない）:
`make_elasticnet_fit_predict` が fold ごとに `alpha_at_path_min` / `alpha_at_path_max` を 0/1 で積み
（`summarize_diag` の平均が「端に張り付いた fold の割合」になる）、`macro_enet` の `final_model` が
α パスの両端と判定・`l1_ratio_grid` を返す。ridge の候補は `plugins.utils.RIDGE_ALPHAS` へ切り出し、
端の判定もそこを参照する（候補の値は変えない・ADR-0058 決定1）。

## Consequences

- 夜ごとに2行（sector_ols・macro_enet）増える。実測は sector_ols 5.6KB・macro_enet 7.4KB
  （2026-09-23・ローカル。macro_enet は期別 rank-IC と係数78本が大半）
- 初回の記録で既に端が見えた（2026-09-23・ローカル）。sector_ols は ridge α が下端 0.001 の業種が3つ
  （海運業 n=9・鉱業 n=5・機械 n=208。分布 `{0.001: 3, 0.1: 1, 1: 7, 10: 13, 100: 9}` は ADR-0058 の実測と
  一致）。macro_enet は最終モデルの l1_ratio が候補の端 0.1（最も Ridge 寄り）で、CV の fold の 11.8%
  （17 fold 中2）が α パスの下端だった。OOF rank-IC は 0.1541。どれも今は読むだけで、候補を広げるかは後続で決める
- Render 起動時の `create_all` が Supabase 側に**空の表**を作る。データは流れない（書き込み先はローカル
  だけで、Supabase の Postgres へ書き戻す経路は作らない・ADR-0038）
- `db_egress.EGRESS_COST_TABLE` には載せない。この表を読むのはローカルの CLI だけで、較正していない表は
  `DEFAULT_BYTES_PER_COLUMN` へ倒れる（測っていない数字を書かない）
- OOF の値は学習月が替わるまで基本的に動かない（ラベルは52週先で、パネルは月単位で伸びる）。
  月の途中で OOF が動いた夜は、入力データが遡って変わった合図として読める
- 後続: 蓄積を読んで警報の基準を決める Issue（着手条件は新しく溜まった行だけで数え、20夜以上かつ
  学習月が1回以上切り替わっていること）。固定値ハイパラの感度掃引を日中キューへ積む案もそこへ記録する

## Considered Options

- **A. 捨てていた値をローカル PG の表に積む（採用）**
- **B. 夜間に感度掃引も足す** → 却下。窓は足りるが、多重比較で偶然の差を拾い、重い計算は日中キューという
  線引きとも衝突する。端に張り付いているかを先に知らないと、どの軸を振るべきかも決められない
- **C. `.logs/` に JSONL で追記** → 却下。表定義もミラーも触らずに済むが、バックアップの対象外で PC の
  故障で消え、読むには専用のパーサが要る
- **D. `_summarize` を直してログ行に出すだけ** → 却下。変更は最小だが、33業種×指標の表形式を夜をまたいで
  読むには日ごとのログを grep でつなぐしかない
- **E. watchdog による自動起票** → 却下（当面）。起票条件を実測の前に決めることになり、公開 Issue に
  診断値が載る
- **F. その場の検証と PRODUCERS の両方** → 却下。同じ障害に Issue が2枚立ち、前例（sector_ols の exempt）
  と食い違う
- **G. 入力行列のハッシュも記録** → 却下（当面）。「同じ入力なのに動いた」を機械的に判定できるが、
  sector_ols と macro_enet の内部に手を入れることになる。軽い文脈で足りないと分かってから足す
