# recommend の Fama-MacBeth ファクタープレミアム推定（統計的最適化プリセット）

## Status

accepted（2026-07-05）。実装は GitHub Issue #271（#270 に依存・2026-07-05 マージ済みのため着手）。
実データ検証で `gap_ratio` の除外が必要と判明し、設計を1点修正（Decision 1・Consequences 参照）。

**改訂（2026-07-18・Issue #342）**: Decision §5 および Considered Options の「GitHub Actions
ワークフロー化は見送り（需要が出てから検討）」を**反転**し、`recommend-factor-premia.yml`
（`workflow_dispatch` のみ・cron なし）を新設した。当初の見送り理由は「計算が軽く Render Free
枠回避という macro-beta-inference.yml の目的に合致しない」だったが、#339 の再学習 cadence 棚卸し
で別の需要が顕在化した——producer/consumer 分離である以上、ローカル CLI 手動実行だと実行有無・
頻度が git 履歴にもワークフローログにも残らず鮮度が不明になる（`feedback_local_scripts_hit_production_db`
のとおり接続先は本番 Supabase）。「Actions 経由で実行した記録が残る」ことがワークフロー化の
新たな目的。計算が軽い性質は変わらないため cron 定期化は引き続き別途判断（当面 dispatch のみ）。

## Context

`recommend`（おすすめ銘柄）の4プリセット重みは `docs/MODELS.md` §6「仮定・限界」に
「ウェイト設定に数学的・経済学的な根拠はなく、直感的なヒューリスティック」と自己申告されて
いる。学術的にはファクターの重み（プレミアム）は Fama & MacBeth (1973) の断面回帰で時系列
平均を取り推定するのが標準的手法であり、現行の直感的ウェイトはこれに沿っていない。

## Decision

1. **母集団・目的変数・fold は M-1/M-2/M-3 と共有する。ただし gap_ratio は回帰の
   特徴量から除外する**（実データ検証で判明）。
   `plugins/macro_snapshots.py::build_snapshots()` を無改修で再利用し、`fin_features` に
   recommend の指標（`z_roe, z_op_margin, z_revenue, z_cf_ratio, z_equity_ratio, z_eps,
   z_de_ratio`。全て `FinancialMetric` の実属性）を渡すことで、M-1/M-2/M-3 と完全に同一の
   月末 cadence・52週先 log return 目的変数・公表ラグ fill-forward を得る。
   `macro_snapshots.py` 自体・M-1/M-2/M-3 のホットパスは一切変更しない
   （`min_coverage` は fin_features 全指標が既に必須のため実質 no-op で影響なし）。

   **gap_ratio を含めなかった理由**: 本番DBを直接集計したところ、`gap_ratio`
   （sector_ols の回帰結果に依存）の非NULL率は年度別に 2020〜2024年度=0%、
   2025年度=67%、2026年度=72%と極端に偏っていた（sector_ols が直近年度のみ計算され
   過去年度へ遡及していないため）。`build_snapshots` の `fin_features` は全指標が同時に
   非NULLという条件で企業を選別するため、gap_ratio を含めると「2025年度の財務データが
   適用可能になる直近2ヶ月分の月末スナップショット」しか有効サンプルが残らず
   （実測: 有効期間2・min_companies_per_period=30時点）、Fama-MacBeth の時系列平均・
   Newey-West補正が統計的に無意味になり、係数も非現実的な値（例: z_eps の b=-221.8）に
   発散した。他7指標は2020年以降96〜100%の充足率があり、gap_ratio を除くことで60ヶ月超
   の期間数を確保できる（ユーザー確認の上でこの除外を採用）。

2. **期間ごとの断面 OLS → 係数の時系列平均**（真の Fama-MacBeth）を実装する。
   `plugins/utils.py::walk_forward_cv_monthly`（M-1 が使う）は複数月の samples_by_ym を
   プールして単一の OLS を学習する **pooled panel OLS** であり、Fama-MacBeth（各期間で
   別々の断面 OLS を実行し、係数 β_t の時系列を後から平均する）とは異なる概念のため流用
   できない。`recommend_factor_premia.py` に期間ループを新規実装し、各期間 `ym` の
   横断面のみで `plugins/utils.py::ols()` を実行して β_t を得る。

3. **momentum は期間内で再Zスコア化する**。`build_snapshots` の `momentum_12m1` 列は生の
   12-1 log リターン（`macro_snapshots._momentum()`）であり、cross-sectional Zスコア化は
   していない。recommend が実際に重みを掛けるのは Zスコア済みの `z_momentum`
   （`compute_momentum_z`）のため、回帰も同じスケールで行う必要がある。
   `recommend_factor_premia.py::build_period_panel()` が momentum 列のみ期間ごとに
   winsorize→Zスコア化する後処理を行う（`macro_snapshots.py` は変更しない）。

4. **Newey-West（HAC）標準誤差**を使う。52週先リターンを毎月ずらして観測することによる
   オーバーラップは β_t の時系列に自己相関を生む。`statsmodels`（既存 pin 依存）の
   `sm.OLS(β_series, const).fit(cov_type="HAC", cov_kwds={"maxlags": 11})` で、時系列平均
   （プリセット重み）と補正済み SE・t統計量・p値を同時に得る。`maxlags=11` は
   52週（≈12ヶ月）のオーバーラップに対する標準的な経験則（lag = horizon_months − 1）。

5. **producer/consumer 分離で永続化**（`macro_beta_inference.py` と同型）。
   `recommend_factor_premia.py`（ローカル専用CLI）が計算→`recommend_factor_premia`
   テーブル（`RecommendFactorPremium`・factor_name 縦持ち・run_id+factor_name 一意）へ
   `--persist`→`plugins/recommend.py::resolve_weights()` が読む。GitHub Actions ワーク
   フローは作らない：単純な OLS ループ（MCMC のような重い計算ではない）であり、
   Render Free枠回避という既存ワークフロー（`macro-beta-inference.yml`）の存在理由に
   合致しないため、ローカル実行で十分と判断した。

6. **新プリセット「統計的最適化」は既存4プリセットに追加**し、置き換えない
   （Issue #271 自身が「新プリセットの重み」と明記）。`PRESETS` 静的辞書には含めず、
   `resolve_weights(db, preset_name)` が `PRESETS` → DB の動的プリセット → バランス型
   フォールバック、の順で解決する。`recommend.execute()` と `backtest.run()` の両方が
   この関数を共用する（`params_schema()` の `preset` select オプションにも追加）。

## Considered Options

- **`walk_forward_cv_monthly` を流用**（却下）：pooled panel OLS であり Fama-MacBeth の
  「期間ごとの断面回帰→時系列平均」という統計的手続きそのものが異なる。無理に流用すると
  Newey-West 補正の前提（β_t の時系列）が成立しない。
- **リッジ回帰で断面 OLS を安定化**（却下）：Fama-MacBeth は慣習的に素の OLS で β_t を
  推定する。正則化を混ぜると Newey-West 標準誤差・t統計量の解釈が崩れるため、v1では
  正則化なしの `ols()` のみを使う。
- **`build_snapshots` に「recommend 用モード」を新設**（却下）：`macro_nan_ok`/
  `build_interactions` に続く3つ目の分岐を持ち込むと、M-1/M-2/M-3 の既存テスト済み
  ホットパスへの侵襲が増える。既存の `fin_features`/`use_momentum` パラメータのみで
  recommend の要件を満たせたため、無改修再利用を優先した（#270 で確立した方針の継続）。
- **GitHub Actions ワークフロー化**（見送り）：計算コストが軽く（MCMC 不要）、
  Render Free枠の制約回避という既存ワークフローの目的にも合致しないため。需要が出てから
  検討する。
- **gap_ratio を含めたまま有効期間2のみで実行**（却下・実データ検証後に判明）：
  当初案は recommend の8指標全てを回帰対象としていたが、本番データで実行した結果
  有効期間がわずか2つしかなく係数が非現実的な値に発散したため、ユーザー確認の上で
  gap_ratio を除外する方針に変更した（詳細は Decision 1）。

## Consequences

- **新規テーブル `recommend_factor_premia`**（`Base.metadata.create_all` で生成・
  DDL/マイグレーション不要）。
- **新規 CLI `recommend_factor_premia.py`・`plugins/recommend.py` に
  `resolve_weights()`/`get_dynamic_preset()`**。`backtest.py` の preset 解決もこれに統一。
- **データ未算出時は常にバランス型へ graceful degrade** するため、「統計的最適化」を
  選んでもバッチ未実行の環境で壊れない。
- **最小社数未満の期間はスキップ**（既定30社）。上場企業数が少ない極端なフィルタ条件下では
  有効期間数が減り、Newey-West の効きが弱くなる可能性がある。
- **「統計的最適化」プリセットは gap_ratio（割安度）の重みを持たない**。
  `recommend.execute()` は weights 辞書に無いキーを単に無視する（0重み相当）ため、
  他プリセットとの混在利用（例: カスタムウェイトで gap_ratio を別途足す）は妨げない。
  sector_ols の遡及計算が将来実施されれば、gap_ratio を回帰対象へ戻す余地がある。
- **将来エンハンス**：sector_ols の過去年度への遡及計算（実現すれば gap_ratio を回帰対象へ
  戻せる）、定期再計算（社数増加・期間経過に応じた再学習）、業種別 Fama-MacBeth への拡張、
  複数期間ホライズンでの感応度分析。

実装・調査タスクは GitHub Issue #271 で追跡する。
