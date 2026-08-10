# ADR 索引（Architecture Decision Record）

このディレクトリは設計上の意思決定記録（ADR）の正本。**新しい ADR を追加したら、この索引に1行足す**（`docs/ARCHITECTURE.md` 側は本ファイルへのポインタのみを持つ＝一覧の二重管理をしない・Issue #407）。

| # | 決定 | 関連 Issue | 状態 |
|---|---|---|---|
| [0001](0001-valuation-consolidation-and-backtest-generalization.md) | バリュエーション集約・OLSエンジンを `sector_ols` 1本化・バックテスト一般化（旧 `total_return` → `gap_analysis` 吸収） | — | accepted（2026-06-21） |
| [0002](0002-m1-per-stock-hierarchical-macro-beta.md) | M-1 を per-stock 階層マクロβ（PyMC 二層フルベイズ）へ再設計 | #260 | accepted（2026-06-21）・実装完了 |
| [0003](0003-m2-macro-financial-gbdt.md) | M-2（マクロ×財務 GBDT）を M-1 の非線形兄弟として同期 in-execute 実装 | #234 | accepted（2026-06-25） |
| [0004](0004-m2-downstream-sell-and-oof-backtest.md) | M-2 を売り推奨（`mu_source` トグル）とアウトオブサンプル検証（OOF）へ連動 | #234 系列 | accepted（2026-06-26） |
| [0005](0005-remove-price-predictor-consolidate-return-prediction.md) | `price_predictor` を削除し、③リターン予測を比較ファミリー（M-1〜M-3）へ集約 | — | accepted（2026-06-27） |
| [0006](0006-japan-macro-estat-boj-connectors.md) | 日本マクロ統計（CPI/M2/短観DI）の e-Stat API・日銀 REST コネクタ設計 | #251 | accepted（2026-06-27） |
| [0007](0007-hyperparameter-tuning-shared-engine.md) | M-1/M-2/M-3 共有ハイパーパラメータ探索エンジン（目的関数＝walk-forward OOF rank-IC） | #264-#267 | accepted（2026-07-05）・**0010 で superseded**（実行手段のみ） |
| [0008](0008-recommend-factor-premia-fama-macbeth.md) | recommend の Fama-MacBeth ファクタープレミアム推定（統計的最適化プリセット） | #271 | accepted（2026-07-05） |
| [0009](0009-oecd-cli-leading-indicator.md) | OECD Composite Leading Indicator（`JP_CLI`）を先行指標チャネルへ追加 | #283 | accepted（2026-07-09） |
| [0010](0010-hyperparameter-tuning-github-actions-automation.md) | ハイパーパラメータ探索を GUI 手動トリガーから GitHub Actions 月次自動実行へ一本化 | #291・#292 | accepted（2026-07-10） |
| [0011](0011-imf-weo-forward-looking-forecast.md) | IMF WEO 見通し（GDP成長率・インフレ率）を forward-looking チャネルへ追加 | #284 | accepted（2026-07-11） |
| [0012](0012-m3-dlm-weekly-only-factors.md) | M-3（時変マクロβ DLM）は週次高頻度ファクター専用（月次以下のマクロ系列は組み込まない） | #310 | accepted（2026-07-12） |
| [0013](0013-commodity-channel-expansion.md) | コモディティ価格チャネルを日次8系列へ拡張（Yahoo Finance v8 chart API 流用） | #358 | accepted（2026-07-20） |
| [0014](0014-purge-embargo-walk-forward.md) | walk-forward CV に purge/embargo を導入し 52週先ラベルの前方リークを遮断 | #363 | accepted（2026-07-22） |
| [0015](0015-m4-ensemble-stacking.md) | M-4 兄弟μ̂スタッキング・アンサンブル（基底 OOF μ̂ の二段ウォークフォワード統合） | #367 | accepted（2026-07-23） |
| [0016](0016-ice-bofa-truncation-baa-credit-proxy.md) | FRED ICE BofA 系列の履歴制限（ローリング3年）に対し非ICE代替 `BAA_SPREAD` へ既定差替（追試 2026-08-01: strict はもう律速していない） | #381, #411 | accepted（2026-07-24） |
| [0017](0017-m5-learning-to-rank.md) | M-5 マクロ×財務 ランク学習（`XGBRanker`・M-2 の rank-IC 整合版・producer なし） | #362 | accepted（2026-07-24） |
| [0018](0018-oof-turnover-and-industry-neutral-ic.md) | OOF バックテストの現実性強化（業種中立 rank-IC＋ネットターンオーバーコスト） | #368 | accepted（2026-07-25） |
| [0019](0019-m2-monotone-constraints-economic-sign-priors.md) | M-2 に `monotone_constraints` で経済符号の事前知識を注入（既定 OFF トグル） | #366 | accepted（2026-07-25） |
| [0020](0020-m2-conformal-prediction-intervals-r3-gate.md) | M-2 に分割コンフォーマル予測区間を付与し `r1_prime`／R3 足切りゲートを再有効化 | #365 | accepted（2026-07-25） |
| [0021](0021-sibling-model-candidate-menu.md) | 兄弟モデル候補メニュー（`fit_predict` 注入の探索枠）→ ElasticNet を **M-6** へ昇格 | #372 | accepted（2026-07-26） |
| [0022](0022-short-side-oof-metric-and-default-mu-source.md) | 売り側 OOF 指標（`short_side_spread` 等）の新設と既定 `mu_source` の M-6 化 | #402 | accepted（2026-07-30） |
| [0023](0023-policy-uncertainty-epu-macro-channel.md) | 政策不確実性（EPU）2系列を FRED から収集しマクロチャネルへ追加・既定昇格 | #404 | accepted（2026-07-31） |
| [0024](0024-news-tone-attention-macro-channel.md) | ニューストーン／関心度（GDELT・Wikimedia）を**マクロ集約**5系列で追加（銘柄別は容量不可）。昇格ゲートは不通過＝選択肢のみ（月次 M-2/M-6・週次 M-3 とも非有意＝#409 の追記節） | #406, #409 | accepted（2026-07-31） |
| [0025](0025-training-window-history-backfill.md) | 学習窓をデータ履歴の延伸で広げる（週次株価7年＋財務2018〜＋過去株価紐付け）。47→71ヶ月・fold 10→18 期。M-1 の rank-IC はコロナ期を含めると 0.198→0.113 へ低下＝旧値は短窓の点推定だった | #411 | accepted（2026-08-01） |
| [0026](0026-representative-asof-and-price-freshness.md) | 代表 as-of は中央値（max 禁止・最古と古い銘柄数を併記）／株価鮮度は p50 判定で 5・10 営業日の黄赤・実行はブロックしない | #417, #416 | accepted（2026-08-03） |
| [0027](0027-structural-nulls-in-sector-ols-population.md) | sector_ols の構造的 NULL（業種に概念が無い列・無配の `dps`）を欠測と分けて扱う。業種単位の列ドロップ＋無配 0 埋めでカバレッジ 74.4→93.4% | #434 | accepted（2026-08-03） |
| [0028](0028-freshness-limits-from-measured-release-lag.md) | 鮮度ゲートの許容遅延は実配信ラグの実測で与え `lag_days` から導出しない。`freq` 既定の較正は**停止していない系列のみ**で行う（`JP_IIP` 停止中の 96 日で `monthly=130` を較正していた） | #451, #447 | accepted（2026-08-04） |
| [0029](0029-m3-jp10y-daily-mof-source.md) | `dlm_jp10y` を財務省の日次 JGB 金利（`JP10Y_MOF`）へ差し替え ADR-0012 Decision 2 を supersede。週次差分ゼロ率 76.89%→0.91%。ただし**日次化して初めて実質的に効くようになった結果その効きが負**（売り側 p=0.023）だったため既定からは外す。日次系列の `lag_days` 上限は実配信ラグ（超えると未来日 CRITICAL） | #458 | accepted（2026-08-07） |

| [0030](0030-buy-side-mu-wiring-default-off.md) | 買い推奨へ μ̂ を opt-in 結線（`METRICS` へ `mu`＋`mu_source`）。**既定は OFF**＝プリセットは `mu` 重みを持たず既定経路は不変。重みだけ付けて出所未指定は reject、backtest は as-of 再現不能につき `mu` を reject | #423 | accepted（2026-08-08） |
| [0031](0031-heavy-plugins-require-registered-automation.md) | 重い計算は GitHub Actions が回し Render は読むだけ。**`heavy=True` は `HEAVY_AUTOMATION` への登録を必須**にし（ワークフロー名 or `exempt: 理由`）、未登録・空理由・実在しないワークフローを CI で落とす。「heavy を足したが自動実行が無い」は失敗しないため通知で拾えない | #423 | accepted（2026-08-09） |
| [0032](0032-statement-timeout-raised-per-statement.md) | Supabase 既定の `statement_timeout=2min` / `lock_timeout=0` は重い1文に足りない（同日に2本のワークフローが落ちた）。引き上げは `db_timeouts` で**その文の実行中だけ**・ロール既定は変えない。ロックを取る処理は `lock_timeout` を有限にして原因を確定させ、VACUUM の上限はワークフローの `timeout-minutes` が持つ | #470, #471 | accepted（2026-08-09） |
| [0033](0033-h1-interim-financials-into-analysis.md) | 半期(H1)決算を分析へ反映する方式。`financial_metrics` は `period_type='annual'` 限定で H1 が一切見えない一方、`WHERE` を外すと年度内Zスコアと LAG 成長率が期間混在で壊れる。案A(TTM合成)/案B(並列VIEW)/案C(サプライズ特徴量)/案D(通期のみ)を**比較軸と実測プロトコルまで確定して保留**——決定は H1 サブセットでの OOF 実測（Egress 復旧後）を待つ | #424 | **proposed（2026-08-11）** |

## 運用

- ファイル名は `NNNN-kebab-case-title.md`（連番は追記のみ・欠番/再利用なし）。
- 各 ADR は `# タイトル` / `## Status` / `## Context` / `## Decision` / `## Consequences`（＋必要なら `## Considered Options`）で構成する。
- 決定を覆す場合は既存 ADR を書き換えず、新 ADR を起こして旧 ADR の Status に superseded を追記する（例: 0007 → 0010）。
- 分析モデルの ADR は `docs/MODELS.md` の該当セクション・`templates/models.html` とセットで更新する。
