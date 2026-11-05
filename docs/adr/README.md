# ADR 索引（Architecture Decision Record）

このディレクトリは設計上の意思決定記録（ADR）の正本。**新しい ADR を追加したら、この索引に1行足す**（`docs/ARCHITECTURE.md` 側は本ファイルへのポインタのみを持つ＝一覧の二重管理をしない・Issue #407）。

「決定」の列は **ADR の題名（決定を言い切る一文）だけ**を書き、経緯・実測値・追記は各 ADR 本文に置く。索引に書き写すと1行が段落に育ち、本文と二重になる（2026-11 に1行最大 2,906 字まで育っていたのを題名へ戻した）。

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
| [0029](0029-m3-jp10y-daily-mof-source.md) | **M-3 の `dlm_jp10y` を財務省の日次 JGB 金利へ差し替え、実測に従って既定から外す** | #458 | accepted（2026-08-07） |

| [0030](0030-buy-side-mu-wiring-default-off.md) | **買い推奨へ μ̂（期待リターン）を opt-in で結線し、既定は OFF に据える** | #423 | accepted（2026-08-08） |
| [0031](0031-heavy-plugins-require-registered-automation.md) | **重い計算は GitHub Actions が回し Render は読むだけ（heavy は自動実行の登録を必須にする）** | #423, #504 | accepted（2026-08-09・2026-08-21 改訂） |
| [0032](0032-statement-timeout-raised-per-statement.md) | **Supabase の statement_timeout は「重い1文の実行中だけ」引き上げる（lock_timeout とセットで）** | #470, #471 | accepted（2026-08-09） |
| [0033](0033-h1-interim-financials-into-analysis.md) | 半期(H1)決算を分析へ反映する方式の比較設計（案A〜D・比較軸と実測プロトコル）。案A（TTM 合成）を ADR-0051 が採った | #424 | proposed（2026-08-11）→ **0051 で superseded**（2026-11-03 追記） |
| [0034](0034-client-side-egress-ledger-and-circuit-breaker.md) | **Egress はクライアント側で常時計測し、プロセス単位のサーキットブレーカで止める** | #478, #477 | accepted（2026-08-13） |
| [0035](0035-mirror-endpoints-are-parameterized.md) | **ミラーはエンドポイントを引数で受け、書き込み先はローカルに限る** | #481 | accepted（2026-08-16） |
| [0036](0036-weekly-prices-incremental-load.md) | **週次株価は指紋＋27週オーバーラップの差分ロードにし、無効化は DB 側の世代印で行う** | #480, #478 | accepted（2026-08-17） |

| [0037](0037-egress-cycle-budget-is-a-second-axis.md) | **Egress の歯止めは2軸。プロセス予算は暴走を止め、月枠を守るのは請求サイクル累計だけ** | #478, #483 | accepted（2026-08-19） |
| [0038](0038-local-postgres-is-the-primary.md) | **正本はローカル PostgreSQL。Supabase は閲覧用の断面とバックアップの置き場へ降格する** | #503, #500, #501 | accepted（2026-08-20） |
| [0039](0039-persisted-weights-carry-their-preprocess-generation.md) | **永続化した重みは、推定に使った前処理の世代を自分で持つ** | #517, #509 | accepted（2026-08-23） |
| [0040](0040-batch-window-is-split-into-step-budgets.md) | **バッチの窓はステップ予算へ分割し、超過は起票される失敗にする** | #530, #512, #504 | accepted（2026-08-24） |
| [0041](0041-preset-weight-gate-has-an-implementation.md) | **プリセット/重みの昇格ゲートは実体を通して測る** | #529, #509, #517 | accepted（2026-08-24） |
| [0042](0042-batch-footprints-need-a-reader.md) | **不実行の検知はローカル watchdog が足跡の鮮度で行い、閾値は cadence＋窓から導出する** | #515, #530, #503, #504 | accepted（2026-08-26 / 2026-09-07 追記） |
| [0043](0043-session-settings-travel-with-the-connection.md) | **セッション設定は接続に付いてくる。接続先の既定値に依存した仮定は例外を出さずに壊れる** | #565, #503, #508 | accepted（2026-08-29） |

| [0044](0044-retire-underperforming-models-by-hiding.md) | **劣後したモデルは削除せず hidden で退役する（評価の結論を UI に反映し、比較の土俵は残す）** | #570, #362, #397 | accepted（2026-08-30） |
| [0045](0045-momentum-gains-vanish-when-the-population-is-aligned.md) | **モメンタムの見かけの改善は母集団を揃えると消える（`use_momentum` は既定 OFF を維持）** | #198, #372, #402 | accepted（2026-08-31） |
| [0046](0046-steps-that-cannot-finish-get-their-own-task.md) | **予算内に完走しないステップは専用タスクへ出す** | #584, #532, #579, #512 | accepted（2026-09-01） |
| [0047](0047-tuning-gate-compares-on-one-panel.md) | **探索の品質ゲートは同一パネル上でしか比較しない** | #590, #291, #532, #592, #711 | accepted（2026-09-01） |
| [0048](0048-schema-migrations-run-only-when-the-fingerprint-changes.md) | **スキーマ移行は指紋が変わったときだけ打つ** | #597, #411, #344, #584 | accepted（2026-09-02） |
| [0049](0049-search-axes-must-be-shown-to-move-the-score.md) | **探索軸は「スコアを動かす」ことを実測してから置く** | #596, #584, #264, #791 | accepted（2026-09-02） |
| [0050](0050-hyperparameter-search-must-align-populations.md) | **母集団を動かす軸は、共通域で測ってから探索させる** | #592, #583, #604 | accepted（2026-09-04） |
| [0051](0051-latest-results-enter-analysis-as-a-ttm-composite.md) | **最新業績は TTM 合成として分析へ入れる（半期を通期の列へ混ぜない）** | #424, #219, #503, #323 | accepted（2026-09-18・実装済み。昇格ゲート（子3）は 2026-09-20 に M-2/M-6 の4検定すべてで有意悪化＝**昇格せず既定は通期のまま**） |
| [0052](0052-m2-stays-for-interpretation-not-supply.md) | **M-2 は解釈と基準線のために残し、μ̂ の供給者からは降ろす（退役とは別の第3の状態）** | #572, #570, #396 | accepted（2026-09-07） |
| [0053](0053-one-scale-per-price-column.md) | **1つの価格列には1つのスケールしか書かない（食い違う公式値は採らない）** | #620, #568, #466, #314, #765 | accepted（2026-09-08） |
| [0054](0054-searches-fold-before-the-budget-instead-of-losing-everything.md) | **探索は予算の手前で自分から畳む（打ち切りに全損させない）** | #638, #633, #639, #587 | accepted（2026-09-11） |
| [0055](0055-valuation-basis-mismatch-is-corrected-in-the-view.md) | **バリュエーション基準の不一致は VIEW で補正する（生値は書き換えない）** | #655, #653, #654, #568, #464 | accepted（2026-09-12） |
| [0056](0056-the-daytime-queue-follows-a-calendar.md) | **日中枠は暦を持つ（日付で決まる仕事を自分で積み、月次と重なる日は重い計算を出さない）** | #681, #424, #618 | accepted（2026-09-16） |
| [0057](0057-past-gap-ratio-is-reconstructed-as-of-each-month.md) | **過去の gap_ratio は月末ごとの時点再現で作り、regression_results へは書かない** | #626, #546, #697, #655 | accepted（2026-09-19） |
| [0058](0058-ridge-alpha-is-chosen-by-loo.md) | **ridge の α は一個抜き交差検証（LOO）で選び、行の並び順に依存させない** | #697, #626 | accepted（2026-09-19） |
| [0059](0059-preset-weights-are-re-estimated-walk-forward-keeping-their-order.md) | **静的プリセットの重みは、大小順を保ったまま rank-IC を直接高めて walk-forward で推定する** | #625, #546 | accepted（2026-09-19） |
| [0060](0060-the-daytime-window-takes-as-many-jobs-as-it-fits.md) | **日中枠は窓に収まるだけキューを取り出す（1日1件をやめる）** | #707 | accepted（2026-09-20） |
| [0061](0061-nightly-keeps-the-diagnostics-it-already-computes.md) | **夜間バッチは計算済みの診断値を捨てずに積み、測る実験は足さない** | #726 | accepted（2026-09-23） |
| [0062](0062-corporate-action-knowledge-lives-in-one-ledger.md) | **企業イベントの知識は台帳 1 つに置く（本番は測定用スクリプトに依存しない）** | #746, #739, #740 | accepted（2026-09-26） |
| [0063](0063-significance-is-p-below-corrected-alpha.md) | **比較の有意判定は p 値と補正後 α で行い、CI は同じ α の区間を添える** | #741, #369 | accepted（2026-09-26） |
| [0064](0064-ridge-alpha-candidates-are-a-fine-log-grid.md) | **ridge の α の候補は同じ範囲を1桁10点に刻んだ対数格子にする** | #761, #758, #728 | accepted（2026-10-03） |
| [0065](0065-empty-industry-is-filled-regardless-of-listing.md) | **業種の空欄は上場区分によらず埋める（空の業種は「後に廃止した社」の印＝未来情報）** | #797, #784 | accepted（2026-10-03） |
| [0066](0066-missing-consolidated-items-are-excluded-not-imputed.md) | **連結の値が無い列は補完せずに外し、回帰に入らなかった社の古い乖離率は消す** | #905, #896, #900 | accepted（2026-10-11） |
| [0067](0067-interest-bearing-debt-is-summed-from-component-columns.md) | **D/E の分子は有利子負債にし、構成要素を列で持って VIEW で合計する（銀行・保険は D/E を作らない）** | #920, #925, #918, #926 | accepted（2026-10-11） |
| [0068](0068-index-events-live-apart-from-corporate-actions.md) | **指数イベントは企業イベント台帳と分け、公式リストの登録表から公表日 as-of で時点再現する** | #1003, #1001, #1002 | accepted（2026-10-21） |
| [0069](0069-evaluation-metrics-have-roles.md) | **評価指標に役割を持たせる——rank-IC を主審に残し、上位K社の指標は拒否権、画面の並びは別に採点する** | #1040, #1041〜#1045, #1073 | accepted（2026-10-28） |
| [0070](0070-search-selects-in-two-stages-for-top-k-buying.md) | **探索は運用（上位10社を買う）に合わせて2段で選び、固定した候補を日中の空きで測り切る** | #1112, #1113〜#1116 | accepted（2026-11-02） |
| [0071](0071-sell-ranking-keeps-only-the-measured-best.md) | **売り候補ランキングは実測で最良の 1 手法（M-6 の μ̂ の下位 1/5）だけを残す** | #1043, #1073 | accepted（2026-11-05） |

## 運用

- ファイル名は `NNNN-kebab-case-title.md`（連番は追記のみ・欠番/再利用なし）。
- 各 ADR は `# タイトル` / `## Status` / `## Context` / `## Decision` / `## Consequences`（＋必要なら `## Considered Options`）で構成する。
- 決定を覆す場合は既存 ADR を書き換えず、新 ADR を起こして旧 ADR の Status に superseded を追記する（例: 0007 → 0010）。
- 分析モデルの ADR は `docs/MODELS.md` の該当セクション・`templates/models.html` とセットで更新する。
