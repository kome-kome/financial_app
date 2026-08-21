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

**再改訂（2026-08-08・Issue #423 子5）**: 上記の「cron は別途判断」を **月次 cron 化（毎月5日
UTC 12:00）で決着**。dispatch のみで運用した結果、ワークフローの実行履歴は**ゼロのまま**で、
本番 `recommend_factor_premia` は 2026-07-05 のローカル手動実行（有効期間 **37**）が最新だった。
つまり「記録が残る」形にしただけでは鮮度は保たれず、**プリセットが 37 期の重みで固まったまま
誰も気づかない**という #438 型の静かな劣化になっていた（無実行は failure ではないので
`notify-failure` でも検知できない）。2026-08-08 に回し直すと有効期間は **61**（#411 の
バックフィル等でパネルが伸びた分）。

cadence を月次にした根拠: Fama-MacBeth の推定は月末スナップショットの積み上げなので、1ヶ月で
増える新情報は「期間が1つ増える」ことだけ。実測所要も job wall 2分54秒と軽く、日次にする
便益がない。8因子の符号は 37 期版と全て一致し（有意なのは `z_revenue` のみ・t=+3.18→+4.87）、
月次更新で重みが乱高下しないことも確認した。

**未決（本 ADR の範囲外）**: `mean_b` をそのままプリセット重みに使う設計上、有意でない因子の
係数もそのまま重みになる。実測では `z_eps` が **b=−3.28（t=−1.07）** と桁違いに大きく、
「統計的最適化」プリセットの並びは統計的に有意でない係数が支配している。多重共線性
（`z_op_margin` +0.48 と `z_cf_ratio` −0.47 が符号反転で対になる）の典型的な signature で、
重みの縮小推定・有意性フィルタの要否は別 Issue で扱う。

**追記（2026-08-10・Issue #469）: 比較する口だけ先に入れた。既定は変えていない。**
`fama_macbeth_regression(..., estimator="ridge")` / CLI `--estimator ridge` で第1段階だけを
`plugins/utils.py::ridge_regression`（RidgeCV・L2）へ差し替えられる。第2段階の HAC 平均
（`average_premia`）は ols と共有し、補正ロジックを二重化しない。ADR-0021 が
`scripts/candidate_bakeoff.py` で同じ現象（λ̄ 最大 5.37・OOF rank-IC −0.0131）と Ridge での
回復（0.027 / 0.1653）を既に実測しており、本 ADR の推定だけがその知見の外にあった。

- **既定は `ols` のまま**＝永続化される重みは従来と完全に同一。
- **`--estimator ridge` は `--persist` と併用できない**（DB へ接続する前に `SystemExit`）。
  `get_latest_factor_premia` は最新 run_id を読む仕様なので、ridge を書いた瞬間に昇格ゲート
  未通過の重みが「統計的最適化」プリセットへ入ってしまう。
- 既定を入れ替えるには **ADR-0028 の昇格ゲート**（増減どちらの向きも補正後 α を通る実測）が
  要る。ridge 係数は期内標準化後＝「1sd あたり」で ols（生スケール）と単位が違うため、
  比べるのは順位と相対的な大きさ。
- 実データでの比較（λ̄・条件数・`/api/backtest` による as-of 再現）は **Supabase の Egress
  超過が解消する 2026-08-18 以降**に行う（#478）。診断用に期別の設計行列条件数を
  `FactorPremiaResult.condition_numbers` へ記録し、CLI が median / max を出す。

**追記（2026-08-21・Issue #469 の実測で決着し #509 へ引き継ぎ）: 診断は当たっていたが、
効いている対処は Ridge の L2 ではなく winsorize だった。** ローカル正本（61期・中央値
3,253社）で実走した結果:

- **共線性は実在する**。`z_op_margin` × `z_cf_ratio` の相関は **r=+0.9993（中央値）／最大
  +1.0000** で、61期すべてでこのペアが最大相関。上の「符号反転ペアは signature」は正しい。
- **真因は共通分母 `pl_revenue` の外れ値**。`op_margin` も `cf_ratio` も分母が `pl_revenue` で、
  それがゼロ近傍の会社が両列で同じ極端値を取る（実測 `op=-61.6109` / `cf=-61.6108`）。
  その1行を落とすだけで r は +0.999999 → +0.9785、winsorize を通すと **+0.9993 → +0.708** へ
  落ちる。0.708 が本来の経済的な相関で、残りは外れ値が作っていた。
- **L2 を入れなくても直る**。winsorize+標準化した素の OLS は Ridge とほぼ同じ答えを出す
  （`z_eps` −3.3367 → +0.0175、`z_op_margin` +0.4830 → −0.0003、`z_cf_ratio` −0.4757 → +0.0007）。
  ADR-0021 の Ridge 化が効いた理由も `make_fama_macbeth_fit_predict` が同じ前処理を持つためで、
  **L2 の寄与と前処理の寄与が分離されていなかった**可能性がある。
- **条件数の読み方に注意**。`_cross_section_condition_number` は `fit_feature_columns` を通した
  **後**を測るので median 3.4 と小さく出るが、OLS が実際に解くのは生スケールの行列で
  **median 53.1 / max 1,880**。ログの値だけを見ると「共線性は無い」と誤読する（実際に一度誤読した）。
- **上の「全指標が年度内 Zスコア（sd≈1）」という前提が誤り**。sd≈1 は外れ値が作った見かけで、
  実効的な散らばり（IQR）は列間で最大 80倍違う（`z_op_margin` 0.0142 ／ `z_equity_ratio` 1.1179）。
  よって |b|=3.28 は「1sd あたり −328%」ではなく「1 IQR あたり約 105%」。係数が大きい理由は
  共線性だけでなく **単位が揃っていないこと**にもある。

是正（消費側での期内 winsorize→標準化）と ADR-0028 の昇格ゲートは **#509** へ引き継いだ。
`recommend.execute` が VIEW 列をそのまま線形結合しているため、同じ是正で「GUI の重み 1.0 の
意味が列ごとに最大 73倍違う」問題も同時に解ける。既定は本追記時点でも変更していない。

**追記（2026-08-21・Issue #509 で是正を実装）: 消費側で期内 winsorize→標準化するようにした。**
VIEW（`sql/financial_metrics_view.sql`）は触っていない——年度窓の Z スコアは他の消費者
（表示・`serializers`）も読んでおり、`recommend` の断面（月末・最新年度）と窓が一致しないため、
**揃えるべき場所は消費側**という判断。是正したのは3箇所:

| 箇所 | 変更 |
|---|---|
| `plugins/recommend.py::execute` | `fit_view_metric_stats(records, weights)` で断面の (mean, sd) を作り、加重前に `standardize_metric` を通す |
| `backtest.py::score_record` / `run` | 同じ stats を `run` が作って渡す（1レコードでは断面統計を持てないので `momentum_z` と同じ受け渡し） |
| `recommend_factor_premia.fama_macbeth_regression`（`estimator="ols"`） | 生スケールの設計行列をやめ `fit_feature_columns`（winsorize→zscore・切片列付き）へ |

決めたこと3点:

- **標準化の対象は「VIEW 列かどうか」ではなく「同じスコア合成へ入るかどうか」**。`gap_ratio`
  （単位は％で Z ですらない）も対象に含める。逆に `z_momentum` / `mu` は `compute_momentum_z` /
  `compute_mu_z` が既に期内標準化しているので**除外**（二重標準化しない）。境界は
  `recommend.VIEW_METRICS`（`METRICS` − `RUNTIME_METRICS`）が一元的に持つ。
- **mean/sd は winsorize 後から求めるが、変換する値は生値**（`plugins/utils.py::fit_zscore_stats`）。
  クリップ済みの値を Z にすると両端が同点になって順位が潰れる。外れ値は大きい Z を保ったまま
  （最終的な上限は `normalize_transform` の ±5）、散らばりの尺度だけがその1社に支配されなくなる
  ——これが是正の核。
- **`results[].detail` は生値のまま**。画面は指標の実額を見せる場所で、スコアの合成
  単位とは役割が違う（`sell_ranking` の既存挙動に合わせた）。

副次的に、`compute_momentum_z` / `compute_mu_z` / `sell_ranking.execute` が個別に持っていた
「winsorize → mean/sd」の4行を `plugins/utils.py::fit_zscore_stats` へ集約した。数値は完全に
同一（`sum()/len()` の逐次加算を維持）。

**帰結: `mean_b` の単位が「1sd あたり」へ変わる**ので、`resolve_weights` が返す「統計的最適化」
プリセットの重み値は従来と別物になる。消費側（`recommend.execute`）も標準化後の Z を使うため
単位は整合するが、**ユーザーが見る推奨順は変わる**。よって ADR-0028 の昇格ゲート（増減どちらの
向きも補正後 α を通る実測）が要る。

**追記（2026-08-21・#509 の昇格ゲート実測）: 是正の効果は「重みがどこから来たか」で符号が逆になる。**

まず**測り方を訂正した**。#509 が当初置いていた「`/api/backtest` で是正前後のランキングを比較」は
**昇格ゲートの道具にならない**——3/6/12/18/24ヶ月の5点は**すべて終端が今日**で重複しており独立で
ないため、補正後 α を通す検定力が無い。ADR-0018 / ADR-0021 と同じ枠組み（`build_period_panel` の
61期パネル上でスコア合成だけを前後で切り替え、共通期でペアリングして定常ブートストラップ）へ
寄せ直した。Bonferroni α=0.0083（6検定）:

| プリセット | rank-IC 是正前 | 是正後 | 判定 |
|---|---|---|---|
| **統計的最適化**（重みも再推定・in-sample） | **−0.0807** | **+0.0960** | 符号が反転 |
| 成長重視 | +0.1264 | +0.0605 | **有意に悪化**（diff −0.0658・CI[−0.0850,−0.0484]・p=0.001） |
| バランス型 | +0.0316 | +0.0210 | ns（p=0.478） |
| 割安重視 | −0.0183 | −0.0107 | ns（p=0.156） |
| 高収益重視 | −0.0134 | −0.0107 | ns（p=0.585） |

- **データ駆動の重みでは狙いどおり効いた**。統計的最適化は是正前が **rank-IC 負**（−0.081）＝
  そもそも壊れていた。`z_eps` の係数 −3.3367 が支配していたためで、是正後は +0.0175 へ落ち着き
  rank-IC が +0.096 になる。上の追記（#469）の診断がそのまま裏付けられた。
- **人手の重みでは逆に効く**。成長重視の悪化には機構がある——是正前の成長重視は事実上
  **「`z_revenue` 単独モデル」に化けていた**（他3指標が潰れて効かなかった）。そして `z_revenue` は
  唯一有意な因子（t=+4.87）。**外れ値による偶然の重み付けが、たまたま最良の単一因子を強調して
  いた**。揃えた分だけ薄まっている。
- **判断: 是正は維持**。統計的最適化の符号反転は偶然では説明できない大きさで、本 ADR が
  「未決」に置いた問題そのものが解けている。対して成長重視の優位は再現性の保証が無い偶然
  （データ世代が変われば潰れ方も変わる）に依存する。手動プリセットの重みが是正後の世界に
  合っていない件は **#513** へ切り出した。

実測の限界を2つ明記しておく: (1) パネルは `gap_ratio` を持たない（本 ADR の Decision 1 で除外）
ため**割安重視の `gap_ratio` 2.0 は効いていない**＝実際の `recommend.execute` の並びとは一致しない。
(2) 統計的最適化の新単位は **in-sample**（同じ61期で推定して評価）なので水準は楽観側へ寄る
（旧単位も同条件なので比較としては fair）。

分布そのものも取り直した。`z_op_margin` は |z|<0.2 が 98.8% → 39.0%・尖度 2402 → 14.0 へ改善したが、
**#509 が置いた目標（16% 前後・尖度一桁）には届いていない**。winsorize は sd を頑健にするだけで
**変換する値は生値のまま**（クリップ済みの値を Z にすると両端が同点になり順位が潰れる）だからで、
目標値の置き方のほうが誤っていた。重み 1.0 の実効影響力（IQR比）のばらつきは **100倍 → 7.5倍**。
`--estimator ridge` と `--persist` の併用禁止はそのまま維持している。

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
