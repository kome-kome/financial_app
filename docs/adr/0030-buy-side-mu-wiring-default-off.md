# 買い推奨へ μ̂（期待リターン）を opt-in で結線し、既定は OFF に据える

## Status

accepted（2026-08-08）。Issue #423 子4 の設計決定。既定 `mu_source` を定めた [ADR-0022](0022-short-side-oof-metric-and-default-mu-source.md) を **買い側へは適用しない**ことを明文化する（0022 自体は有効・売り側の決定は不変）。

## Context

`/api/recommend`（毎朝の買い推奨ランキング）は Zスコア加重和だけで順位を決めており、**μ̂（M-1〜M-6 が推定する期待リターン）を1つも読んでいなかった**。μ̂ は `sell_ranking` の `mu_source` パラメータ専用で、買い側からは参照経路そのものが存在しない。

一方で買い側 rank-IC は実測されている（ADR-0022 の OOF）:

| モデル | 買い側 rank-IC | 売り側 spread の順位 |
|---|---|---|
| M-6 (`macro_enet`) | 0.1713 | 1位（既定 `mu_source`） |
| M-2 (`macro_gbdt`) | 0.1419 | 2位 |
| M-1 (`macro_risk_return`) | 0.1142 | 3位 |

つまり「最も予測力のあるシグナルが、ユーザーが毎朝必ず踏む画面にだけ入っていない」状態だった。

ただし **買い側 rank-IC の順位と売り側 spread の順位は一致しない**（ADR-0022）。売り側で M-6 を既定に据えた実測は、買い側の合成スコア（z_roe 等との加重和）に μ̂ を混ぜたときの成績を何ひとつ示していない。rank-IC は μ̂ 単体の順位相関であって、合成スコアにおける最適重みではない。

## Decision

1. **`METRICS` へ `mu` を追加し、`mu_source` パラメータで producer を選ばせる。** 読み出しは `sell_ranking` と同じ producer 契約（ADR-0004 の `read_producer_scores` → `{edinet_code: {mu, r_macro, r1_prime}}`）をそのまま流用する。買い側が使うのは `mu` のみ（`r_macro` は売りの −Rᴹ 観点専用、`r1_prime` は売りの R3 足切り専用）。

2. **既定は OFF。** `mu_source` の既定は `None`、`PRESETS`（バランス型／成長重視／割安重視／高収益重視）は**どれも `mu` 重みを持たない**。したがって既定経路の結果はビット単位で従来と同じで、producer への問い合わせも発生しない（`weights.get("mu")` が偽なら読まない）。既定へ入れるには [ADR-0028](0028-freshness-limits-from-measured-release-lag.md) の昇格ゲート（**増減どちらの向きも補正後 α を通る実測**）が要る。`tests/test_recommend.py::test_no_preset_carries_mu` がこの契約を強制する。

3. **`mu` に重みがあるのに `mu_source` 未指定なら `ValueError` で reject する。** 黙って `None` 扱いにすると「重みを付けたのに効いていない」状態が画面から見分けられず、カバレッジだけが静かに下がる。パラメータ契約の fail fast（未知キーを silent-drop しない）と同じ思想。

4. **producer 未実行は graceful-degrade**（μ̂ を外して継続）。ただし応答へ `mu_available` / `mu_asof` を必ず返し、UI は `mu_available === false` のとき「μ̂ 抜きで計算した」と明示する。degrade したこと自体は隠さない（#438 型の静かな劣化を作らない）。

5. **μ̂ は候補集団内で winsorize(p1-p99)→Zスコア化してから加重する。** μ̂ は週次リターン[小数]で `z_*` と2桁スケールが違い、生値のまま Σ(w·z)/Σ|w| に入れると他指標が実質無効化される。標準化母集団は**フィルタ適用後の候補集団**＝同一画面の `z_momentum`（`compute_momentum_z`）と同じ基準に揃える。売り側は保有銘柄がユニバースの一部でしかないため producer 全体を母集団に取っており、そちらとは基準が異なる（意図的）。

6. **`/api/backtest` は `mu` を含む重みを reject する。** producer スコア（`macro_enet_scores` 等）は最新 `snapshot_date` の**1断面**しか保持せず、`months_ago` 時点の μ̂ を復元できない。`getattr(r, "mu", None)` へ黙って落ちると「μ̂ 込みでバックテストした」と誤読される。μ̂ の時系列評価は各 producer の OOF バックテスト（`plugins/macro_snapshots.py::oof_backtest`）が担う。

7. **「統計的最適化」プリセットへ `mu` は入れない。** `recommend_factor_premia`（Fama-MacBeth）は財務・株価由来 factor しか推定しておらず μ̂ の premium は存在しない。仮に混ざると `mu_source` 未指定の実行が Decision 3 で reject され、**プリセットを選んだだけで 400 になる**。`get_dynamic_preset` で構造的に落とす。

## Considered Options

- **バランス型へ `mu` を組み込んで既定を動かす**（却下）: 毎朝必ず踏むパスの既定重みを、買い側の合成スコアでの実測なしに変えることになる。ADR-0028 が「既定の増減はどちらの向きも補正後 α を通る実測を要する」と定めた対象そのもの。rank-IC 0.1713 は μ̂ 単体の順位相関で、既定重み変更の根拠にはならない。
- **新プリセット「μ̂重視」を追加する**（却下・保留）: 既定を動かさないので安全だが、重み配分（`mu` と `z_*` の比）に根拠が無い。プリセットは「押せば妥当な結果が出る」と読まれるため、実測なしの配分を配るのは誤解を招く。カスタムウェイトなら「自分で決めた比率」であることが利用者に明示される。実測後に ADR を起こして追加する。
- **`mu_source` の既定を売り側と同じ `macro_enet` にする**（却下）: `mu` 重みが 0 なら実挙動は同じだが、パラメータ画面上「常に M-6 が効いている」と読める。既定 OFF という決定を UI が裏切る。
- **backtest で過去 `snapshot_date` から μ̂ を復元する**（却下）: `*_scores` は全置換（`replace_macro_*_scores`）で履歴を持たない。持たせるとしても、先読み検証を別途要する実装が新たに必要で、Decision 6 の目的（誤読の防止）は reject で足りる。

## Consequences

- **既定経路は完全に不変**。`/api/recommend`・`/api/morning`（preset 経由でしか重みを渡さない）とも従来と同じ結果を返し、producer への追加クエリも 0 本。Supabase Egress への影響なし。
- **μ̂ を使うには2つの操作が要る**（ウェイトを上げる＋出所を選ぶ）。1操作で済ませなかったのは Decision 3 の reject を成立させるため——「出所の既定値」を置くと、重みを付けた瞬間に無検証のモデルが効き始める。
- **`RUNTIME_METRICS`（`z_momentum` / `mu`）という区分が生まれた**。`financial_metrics` VIEW の列ではなく実行時に埋める指標の集合で、`SELECT_COLS`（転送列・#441）と `recommend_factor_premia.build_period_panel` の `fin_features` の両方がここから除外を導出する。METRICS へ VIEW 外の指標を足すときはこの集合へも入れること（入れ忘れると存在しない列を SELECT しに行く）。
- **`/api/backtest` の `SCORING_SOURCES` は変更しない**（CLAUDE.md のメタ検証網羅性ルールに対する本 ADR の回答）。`recommend` source は従来どおり検証でき、`mu` を含む重みだけが reject される＝**評価手段が欠けた指標を黙って作らない**。μ̂ 側の評価は OOF が担当済み。
- **買い側で μ̂ が効くかは未測定のまま残る**。合成スコアへ入れたときの rank-IC／実現リターンを測る手段は現状 backtest にも OOF にも無い（前者は as-of 再現不能、後者は μ̂ 単体の評価）。既定化を検討するなら「μ̂ を含む合成スコアの OOF」を先に用意する必要がある——本 ADR はそれを作らずに既定を動かさない、という選択である。
