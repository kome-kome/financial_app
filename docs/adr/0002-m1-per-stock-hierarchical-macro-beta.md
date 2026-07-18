# M-1 を per-stock 階層マクロ・ベータへ再設計

## Status

accepted（2026-06-21・**設計決定**）→ **実装・デプロイ完了（2026-07-02・Issue #260）**。

**実装済み**: `macro_beta_inference.py` の `build_panel`（`plugins/macro_snapshots.py` のデータ経路を再利用）・`select_shared_factors`（`select_features_bic` 共有ヘルパーへ集約し `macro_risk_return._select_macro_features` と実体統一）・`build_hierarchical_model` の non-centered パラメータ化（offset×scale・Neal's funnel 対策）・収束診断（r_hat/ESS/発散遷移数を hyperparams へ保存）。GitHub Actions 推論ワークフロー `macro-beta-inference.yml`（`workflow_dispatch` 手動実行のみ・定期スケジュールは持たない）を整備。合成データでのエンドツーエンド動作確認済み（`tests/test_macro_beta_inference.py`：`build_panel`/`select_shared_factors`単体・階層モデル構築+NUTSサンプリング・`run_inference`一気通貫、いずれも pass）。

**未実施（初回本番実行後に本節を更新）**: 本番 DB での初回実行・実データでの収束確認（r_hat<1.01）・WF-CV 検証（単一β比 R² 非劣化・R1' の銘柄間分散>0・Consequences 節の受け入れ基準）。

**改訂（2026-06-21）**: partial pooling の手法を**フルベイズ二層階層モデル（全体→セクター→銘柄、PyMC 5系・NUTS）**に確定。R_macro を **√(βᵀΣ_macroβ)**（リターン単位）に確定。実行アーキを**推論バッチ分離**（PyMC は GitHub Actions 専用・本番 Render 非搭載）に確定。詳細は下記 Decision §1/§5・Considered Options・Consequences に反映済み。

**改訂（2026-07-18・Issue #341）**: `macro-beta-inference.yml` に**月次 cron（毎月1日 UTC 11:00）を追加**（従来の `workflow_dispatch` は残す）。再学習が人力任せで producer の鮮度が担保されなかった（最終成功 2026-07-04 から12日以上放置）ため、tune-hyperparameters.yml と同じ月次自動化に揃えた。あわせて、上記「収束確認（r_hat<1.01）」の **strict ゲートを `--r-hat-threshold` で可変化**した（既定 1.01・cron は 1.05）。理由: 本番実 persist 済みの 2026-07-04 run も含め chains=2 では r_hat_max が構造的に ~1.02 で頭打ち（PyMC も「信頼できる r_hat には4 chain以上推奨」と警告するとおり 2 chain では保守的に出る・当該 run は n_divergences=0 で実質収束）。strict 1.01 のままだと chains=2 の無人 cron は毎回ゲートで落ちて永遠に persist されないため、cron では 1.05 を渡して構造的 ~1.02 を自動 persist しつつ、真に収束していない run（r_hat が 1.05 を大きく超過）は依然 reject する。ゲート判定は純関数 `persist_allowed(r_hat_max, threshold, force)` に切り出し単体テスト（`TestPersistGate`）で担保。

**改訂（2026-07-18・Issue #352）**: 上記 #341 の cron 化後、`build_panel` が `ValueError: 有効なサンプルがありません` で全滅していることが発覚（#341 の r_hat プール検証を走らせて発見）。原因は #284（IMF WEO 見通し・年2回公表）で追加された `JP_WEO_GDP_FCAST`/`JP_WEO_CPI_FCAST`（zscore）が trailing 5年に約10点しか無く `_macro_from_cache` の zscore 最小点数（<20 で None）を満たさず**全 snap_date で None** になり、`macro_nan_ok=False` の ANY-None ゲートで全スナップショットが脱落したこと。対策として `build_panel` に **`_drop_unusable_macro`** を追加し、全観測日で None になる（＝一切値が出ない）マクロ特徴量を build_snapshots へ渡す前に自動除外＋WARNING ログ出力する（将来の疎な系列追加でも producer が全滅しない・`JP_WEO` は M-2 の `macro_nan_ok=True` では引き続き利用可）。あわせて `macro-beta-inference.yml` 等の run step に `set -o pipefail` を追加（`\| tee` が python の非ゼロ終了をマスクし、build_panel 失敗がジョブ緑表示に埋もれていた観測性バグの是正）。

**検証メモ（2026-07-18・#341 のプール仮説を実測反証）**: 「chains=2 の run を複数回まわして生サンプルをプールすれば 2×N チェーン相当の r_hat が strict 1.01 を切れるのでは」という案を検証した（実験基盤 `scripts/experiment_pooled_rhat.py`・PR#351）。縮小規模（実データ500銘柄・K=4・draws/tune=800・numpyro）で **r_hat_max は 2/4/6/8 チェーンを通じて 1.0100 で完全に平坦（delta=+0.0000）**、一方 ESS は 525→1927 と約4倍に増加した。結論: **プールは ESS（サンプル数）を増やすが r_hat は下げない**——r_hat はチェーン内混合の良さ（between/within 分散比）で決まり、同程度に混合の甘いチェーンを増やしても比は不変。r_hat を下げるレバーは「チェーン数↑」ではなく「1本1本の混合改善（reparameterization・target_accept↑・tune↑）」。過去に draws=800/1000 でもサンプル長で動かなかった事実と合わせ、**~1.01–1.02 はこのモデル幾何の安定した床**であり、`--r-hat-threshold` の緩和（cron 1.05）が妥当な対処だったと確認された（プール昇格は見送り）。なお r_hat_max は銘柄数（パラメータ数）が増えるほど上がる max 順序統計の性質があり、500銘柄で 1.01・本番3800銘柄で 1.02 という差もこれで説明できる。

## Context

M-1（マクロ×リスク-リターン推奨）は現状、**ユニバース全体で単一の β**（BIC 選択も OLS 再フィットも全銘柄プール）を学習し、銘柄固有なのは R1（断面レバレッジ）と R2（実現ボラ）だけだった。これに起因する構造的限界が判明した。

- **判別力の欠如と退化**: walk-forward CV R²≈0.01。§9.6 で自認のとおり、R1 レバレッジが全銘柄でほぼ一定 → 縮小重み w≈1 → μ_shrunk が全銘柄をセクター平均へ収束させ、銘柄差が消える。
- **マクロ特徴量が4つに限定**: 設計ではなく収集失敗（JP10Y=`^JGB` 上場廃止 / TOPIX 取得不可）。さらに**横断モデルではマクロの水準は全銘柄同値＝断面分散ゼロ**で、交互作用にしないと寄与しない——これが「マクロ増量」を阻む根本制約だった。
- **リスク軸の次元非整合**: R1（OLS-SE）/ R2（年率ボラ）/ R3（CV-RMSE）は単位がバラバラなのに、単一 λ で軸を切替可能 → `U = μ − λ·R` が軸ごとに別物（λ の意味が破綻）。
- **doc⇔doc⇔code 衝突**: 散布図・効用に μ_raw を使うか μ_shrunk を使うか、MODELS.md / ガイド / コードで不一致。

「個別銘柄ごとのマクロ感応度」「マクロ増量」「R3 の要否」「効率的フロンティアの逆（売り側）」を同時に解くには、推定単位そのものを変える必要があった。

## Decision

1. **推定単位を per-stock 階層モデルへ**。共有マクロ因子集合を pooled データ上で BIC（`LassoLarsIC`）で一括選択し、その因子への**ローディングを銘柄ごとに推定**してセクター/ユニバース事前分布へ partial pooling する（[[個別マクロ感応度]]）。**因子集合が全銘柄共通**なので μ と銘柄固有リスクが比較可能（commensurable）で、リスク-リターン散布図とフロンティアが成立する。**手法は全体→セクター→銘柄の二層フルベイズ階層モデル**（PyMC・NUTS）：共有因子へのローディングを各銘柄の random slope とし、`銘柄 ← セクター事前 ← ユニバース事前`の超事前で部分プーリングする。MCMC 事後分布から **per-stock 事後平均ローディング**（μ 予測の係数）と**事後SE（R1'）**が自然に得られ、小 n（実効サンプル一桁）の不確実性を正しく伝播する。Vasicek 型・Empirical Bayes も検討したが、表現力と不確実性伝播を最優先してフルベイズを採用（比較は Considered Options 参照）。
2. **特徴量の居場所を分離**。マクロは**主効果**（per-stock で時間変動）。年次/半期ファンダは時系列回帰子ではなく、**ローディングの断面事前分布（上位層）**を説明する。真の四半期ファンダは容量（Supabase 500MB）＋ソース（EDINET 四半期報告書が 2024/4 廃止）＋ point-in-time リーク対策がブロッカーのため **DEFER**。
3. **交互作用項（`fin×macro` / `sector×macro`）を撤去**。異質性は per-stock random slope が担う。`sectors[:10]` の隠れバイアスが消滅する。
4. **マクロ増量はチャネル網羅で**。count ではなく、直交する経済チャネル（金利/期間構造・FX・株式β・クレジット・コモディティ・ボラ/リスク選好・インフレ）で 8-12。BIC 選択は pooled（large-n）で行うため次元爆発に耐性。
5. **リスク軸を再編**。効用軸＝R2（総ボラ）＋**R_macro（[[系統的マクロリスク曝露]]、新設）＝√(βᵀΣ_macroβ)**（β＝per-stock 事後ローディング・ベクトル、Σ_macro＝選択マクロ因子の共分散行列）。これは**マクロで説明されるリターンの標準偏差**ゆえ μ・R2 と同じリターン単位で λ が次元整合し、総リスク ≒ √(系統的² ＋ 固有²) として R2 のマクロ起因成分に分解できる。当初の括弧書き候補 ‖macro loadings‖（L2 ノルム）はリターン単位にならず λ の次元整合が崩れるため**棄却**。**R1'（per-stock 事後SE）＝縮小駆動**、**R3（バケット CV-RMSE）＝表示/足切りゲート**に降格（[[結果リスクと信頼度の分離]]）。
6. **売り側ジオメトリを新設**。[[負の効用]] `D = λ·R − μ`（買い側 U の符号反転）と[[非効率的フロンティア]]（反 Pareto 集合＝支配判定の反転）。売り側で反転する誤差の罠（μ 過大評価が悪い保有を売りから守る）は **R3 足切りゲート**で処理し、μ は再定義しない。
7. **売り候補ランキングへは成分結線**。M-1 producer が per-stock μ・R_macro を出し、sell_ranking に**別メトリックとして**注入（μ:高=売り理由減 / −R_macro:高曝露=売り理由増）。λ は売り重みに吸収。`depends_on`＋graceful-degrade、R3 ゲートは action-label 段。フロンティア/D は M-1 画面の可視化に留める。
8. **対数変換は選択適用**。水準/フロー（売上・資産・時価総額・EPS>0）は log/Δlog 成長、バリュエーション倍率（PER/PBR/配当利回り）は log、負値・有界比率（ROE・自己資本比率・各種 YoY 変化・z-score）は非 log。ターゲットは既に log return。

## Considered Options

- **ユニバース単一 β 維持＋交互作用増強**（却下）: 最小改修だが、CV R²≈0.01 と μ_shrunk 退化の主因が「単一モデル仮定」である可能性が高く、交互作用を増やしても断面分散の制約（マクロ水準＝全銘柄同値）に縛られ続ける。
- **独立 per-stock BIC**（却下・当初案の文字通り）: 銘柄ごとに特徴量も係数も独立 BIC 選択。小 n（52週フォワード＋週次で実効サンプル一桁）で過学習し、銘柄間で因子集合がバラバラになって**フロンティアの μ/R が比較不能**になる。共有因子集合＋縮小ローディングが、柔軟性と比較可能性・サンプル効率を両立する。
- **真の四半期ファンダを前提プロジェクト化**（保留）: capacity レバー（`raw_xbrl_json` drop）＋`period_type`/`filing_date` 列＋J-Quants 有料 Light が必要で、モデル進化をゲートする。per-stock マクロ・ベータは年次ファンダのままでも出せる（ファンダは事前分布側）ため、四半期は後日エンハンスへ分離。
- **R3 を破棄 / R3 を別軸として残す**（却下）: R3 は唯一の out-of-sample 指標で破棄は惜しい。一方で「リスク軸」ではなく信頼度なので、軸ではなく machinery（表示/足切り）として残す第三の道を採用。
- **負の効用を売り特化で再設計**（却下）: `D_sell = λ_R·R_macro + λ_c·(R1'+R3) − μ` は表現力が高いが、新 λ 追加・買い側との対称性喪失・降格した R1'/R3 を罰則として再昇格させる。純ミラー D=−U＋R3 ゲートの方が対称的で再利用が効く。
- **per-stock 縮小の手法（partial pooling の実装）**: 3案を比較。(a) **Vasicek 型 信頼性加重縮小**＝numpy 閉形式・軽量・§9.6 James-Stein の自然拡張で Render 本番でもリクエスト時計算可だが、正規・線形の仮定が強く不確実性伝播が点推定どまり。(b) **Empirical Bayes**＝階層分散をモーメント/周辺尤度で点推定する (a)(c) の中間。(c) **フルベイズ二層階層モデル（採用）**＝全体→セクター→銘柄の階層事前を MCMC（NUTS）で同時推定。事後分布から per-stock 事後平均ローディングと事後SE（R1'）が自然に得られ、小 n の不確実性を正しく伝播する。代償は新規重依存（PyMC／PyTensor のビルドが重い）と推論コストだが、下記の**バッチ分離**で本番ランタイムへの影響を遮断する。表現力と不確実性伝播を最優先して (c) を採用。

## Consequences

- **μ_raw vs μ_shrunk 衝突が自動解消**: μ は pooled loading からの単一予測（縮小はローディング段で完結）になり、別途の μ_shrunk 段が不要。散布図・効用にどちらを使うかの不一致が消える。
- **R1 退化が解消**: 銘柄ごとの時系列回帰が自然な per-stock 事後SE（R1'）を与える。
- **doc⇔code 名称負債の清算が必要**: `_forward_bic`（実体は単一 `LassoLarsIC` パス）の改名、未使用 `vif_threshold` 削除、MODELS.md / ガイドの μ 記述統一、`sectors[:10]` 記述の撤去。
- **sell_ranking が M-1 producer の consumer に**（`depends_on` 追加）。未実行時は `gap_available` 流に graceful-degrade。
- **新たな検証責務**: per-stock × 共有因子集合 × partial pooling の WF-CV、R_macro の定義（標準化ローディングの L2 ノルム等）と λ レンジの再較正。
- **収集タスクが派生**: 新マクロチャネルの取得可否調査（無料ソース制約下）。
- **実行アーキ＝推論バッチ分離（Render 制約からの必然）**: 階層 MCMC は `/api/plugins/{name}/run` の**同期リクエスト実行**では時間的に不可能。よって **PyMC は GitHub Actions の推論バッチ専用依存**とし、本番 Render の `requirements.txt` には載せず `requirements-inference.txt` へ分離する（PyMC 5系＝5.28.x・枯れた安定版を pin）。バッチが per-stock 事後ローディング（平均・SE）・選択因子集合・因子共分散 Σ_macro を **DB に永続化**し、M-1 プラグインは **producer** としてそれを読むだけで μ・R_macro・R1' を出す（`produced_output`／`depends_on` 機構に乗せる）。これにより本番ランタイムは軽量を維持し、収集パイプライン側でのみ MCMC を回す。
- **パッケージ評価（CLAUDE.md 準拠）**: PyMC は重大 CVE 未確認・NumFOCUS 後援でメンテ活発・研究標準で査読者多数。総合判定 ⚠️注意（PyTensor のビルドが重い）。本番非搭載＋バッチ分離で懸念を遮断する前提でユーザー承認済み（2026-06-21）。

実装・調査タスクは GitHub Issues（残タスクの正本）で追跡する。
