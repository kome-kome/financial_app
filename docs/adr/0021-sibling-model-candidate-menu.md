# 兄弟モデル候補メニュー（fit_predict 注入による低コスト実証比較・探索枠）

## Status

accepted（2026-07-26）。Issue #372 の設計決定。実測の結果 **ElasticNet を M-6 として昇格**し、
他の候補は探索枠に据え置いた（末尾「実測」節）。

## Context

`plugins/utils.py::walk_forward_cv_monthly(fit_predict=...)` の注入点（ADR-0003 §3 で M-2 のために
導入し、ADR-0017 で `pass_train_groups` を足した）は、**同一 fold・同一特徴量・同一指標**で任意の
学習器を評価できる共有ハーネスになっている。ここへ差し込むだけで、新しい学習器を「プラグイン化・
UI 追加・producer 永続化・ドキュメント」なしで OOF rank-IC まで測れる。

VISION の核心は「並置してどちらが有効かを実データで決める」ことだが、正式兄弟（M-1〜M-5）を1本
増やすコストは高い（プラグイン＋スキーマ＋テスト＋ADR＋`model_comparison` 追加＋実行時間）。結果として
「試す価値はあるが確信がない」学習器は検討されないまま滞留する。Issue #372 はこれを**探索枠**として
切り出し、6つの候補（ElasticNet / ExtraTrees・QRF / Fama-MacBeth / マクロPCA圧縮 / regime-switch 閾値
線形 / 代替GBDT）を安価に実証し、**有効なものだけ正式兄弟へ昇格**する枠組みを求めた。

## Decision

1. **候補は `plugins/model_candidates.py` に `fit_predict` ファクトリとして集約する**（プラグイン化しない）。
   `plugin` 属性を持たないためレジストリに登録されず、API・UI・`model_comparison` には現れない。
   実行経路は `scripts/candidate_bakeoff.py` のみ＝本番の挙動・Render のビルドに一切影響しない。
2. **昇格ゲート**: OOF rank-IC が M-2 を上回るだけでなく、`model_stats.paired_ic_significance`
   （ADR-0018・定常ブートストラップ）で **差が有意**（95% CI が 0 を跨がない）であることを昇格条件とする。
   walk-forward の per-fold IC は学習窓が重なり系列相関を持つため、点推定の大小比較や素朴な paired-t で
   昇格を決めると、ノイズを実力と読み違える。
3. **評価は M-2 既定 config で固定**する。`MacroGbdtPlugin.params_schema()` を `coerce_params({})` して
   得た設定でスナップショットを**1回だけ**構築し、全候補が同じ `samples_by_ym` を共有する
   （`model_comparison` が M-2 を走らせるときと同一設定＝ここで出る基準線は本番 M-2 の OOF と直接比較可能）。
   fold は `min_train_months=6 / step_months=3 / embargo_months=12`（ADR-0014）で M-2 と完全一致させる。
4. **リーク防止の共通契約**: 前処理パラメータ（NaN 補完平均・winsorize 境界・正規化統計・PCA 主成分・
   レジーム閾値・λ̄・ElasticNet の α）は**すべて学習 fold 内で fit** する。テスト側は同一パラメータで
   transform するだけ。テストラベルは評価にしか使わない。テストは「テストラベルだけ差し替えて予測が
   ビット一致するか」で**直接検証**する（`tests/test_model_candidates.py`）。
5. **候補固有の設計**:
   - **ElasticNet**: α・l1_ratio は学習 fold 内の `TimeSeriesSplit`（過去→直近の向き）で選ぶ。ランダム
     K-fold は期間をシャッフルして楽観バイアスを生むため使わない。
   - **ExtraTrees**: 予測区間を2通り出す。`leaf`＝全分散の法則 Var[y|x] ≈ E_t[Var_leaf] + Var_t[mean_leaf]
     を葉の1次・2次モーメント（`np.bincount`）から集計する QRF 相当の軽量版（真の QRF（Meinshausen 2006）は
     葉ごとの学習ラベル集合をテスト行ごとに再集約するため O(n_test·T·leaf) で本規模に不適）。`tree`＝木予測
     の経験分位（Issue #372 の記述どおり）。後者は**アンサンブル平均の epistemic なばらつき**であり残差分布
     ではないため、被覆率は名目を大きく下回るのが理論的な期待値であり、実測でもそうなった（下記）。
   - **Fama-MacBeth**: `pass_train_groups=True` で月境界を受け取り期別断面回帰を復元する。**マクロ列は
     同一月の全銘柄で同値＝断面回帰では切片と完全共線**のため、断面標準偏差が実質 0 の列を自動除外する
     （結果として本候補は「マクロ非条件付きの characteristics モデル」になる）。第2段階（HAC 平均）は
     ADR-0008 の `recommend_factor_premia` を再利用する（`fama_macbeth_regression` をそのまま呼ぶ OLS 版と、
     第1段階だけ Ridge に替えて `average_premia` を共有する Ridge 版）。
   - **マクロPCA**: 任意の候補に被せられる**合成可能ラッパー**（`wrap_macro_pca`）。マクロ列だけを fold 内
     PCA で直交少数因子へ畳み、非マクロ列は無変換で温存する。3引数候補（`pass_train_groups`）にも
     `*rest` 素通しで対応。autoencoder は tensorflow/torch が必要で VISION 採用基準・heavy 制約に
     合わないため不採用（Issue #372 の判断を踏襲）。
   - **regime-switch 閾値線形**: レジーム変数（VIX/信用スプレッド）は**既に特徴行のマクロ列にある**ため、
     Issue が案として挙げた `sample_meta` への regime 列追加は行わない（`build_snapshots` 無改修で
     同じ状態変数が得られる）。閾値は学習 fold 内の中央値（リークなし・両レジームの標本を確保）。
     標本不足のレジームと NaN 行は pooled モデルへフォールバックする。
   - **代替GBDT（LightGBM / CatBoost）**: M-2 の既定ハイパラ（depth=4 / lr=0.05 / subsample=0.8 /
     colsample=0.8 / L2=1.0 / n_estimators≤500 / early_stopping=40・時系列末尾20%を検証）へ意図的に揃え、
     **実装差だけ**を比較する。
6. **代替GBDT の依存は `requirements-optional.txt`（本番 `requirements.txt` には入れない）**。候補は
   ローカル探索専用で Render では一切使わないため、無料プランのビルド footprint（CatBoost の wheel は
   約 100MB）を増やす理由がない。未導入環境では `candidate_available()` が False を返し bakeoff が
   自動スキップする。**正式兄弟へ昇格した時点で `requirements.txt` へ移す**。
   VISION 採用基準の審査（2026-07-26・ユーザー明示承認済み）: lightgbm 4.7.0 = MIT / 18.4k star /
   月間 16.6M DL / CVE-2024-43598（RCE・heap overflow）は 4.6.0 で修正済み → pin 版は patched。
   catboost 1.2.10 = Apache-2.0 / 9.0k star / 月間 2.9M DL / ライブラリ固有の既知 CVE なし。
7. **Egress 対策**: 価格は既存 `weekly_prices_close` キャッシュ（Issue #355）を再利用し、
   financial_metrics / companies / macro も軽量 namedtuple で `scripts/.cache` へ保存する。**1回のロードで
   全候補を評価する**設計のため、候補を増やしても Egress は増えない（2回目以降はゼロ）。

## Considered Options

- **各候補を最初から正式プラグイン（M-6, M-7, …）にする**: `model_comparison` が 10 モデルになり
  実行時間が跳ね上がる。効かないと分かった候補の撤去コストも高い（UI・ドキュメント・ADR）。
  → 却下（昇格ゲートを通ったものだけプラグイン化する二段構えにした）。
- **候補を M-2 の `params_schema` に `model_backend` として足す**: `model_comparison` は既定パラメータで
  走るため候補が比較行に現れず、ADR-0017 で却下したのと同じ理由で受け入れ基準を満たさない。→ 却下。
- **点推定の rank-IC 大小だけで昇格判定する**: fold 数が 10 前後で IC の fold 間 std が点推定と同程度
  あるため、ノイズで容易に順位が入れ替わる。→ 却下（ADR-0018 の有意性検定をゲートに使う）。
- **真の QRF（葉の経験分位）を実装する**: 本規模（学習 7 万行 × 300 木）ではテスト行ごとの葉集合再集約が
  重すぎる。→ 葉モーメント＋正規近似の軽量版を採用（区間の目的は R1' 相当の相対比較であり分位の厳密性
  ではない）。
- **regime を `sample_meta` へ足す**: `build_snapshots` の改修は M-1/M-2/M-3/M-5 全てに波及する。
  同じ情報が特徴行から得られる以上、変更範囲を増やす理由がない。→ 却下。

## Consequences

- 新しい学習器の実証コストが「ファクタ関数1本＋レジストリ1行」まで下がる。今後の学習器提案は
  まずここで測ってから昇格を議論する（VISION「並置して有効性を決める」の低コスト運用）。
- 候補は本番コードパスに一切乗らない（プラグイン非登録・API 非公開・requirements 非依存）。逆に言えば
  **候補のまま放置すると誰も実行しない**ため、実測値は本 ADR に残して知見として固定する。
- `recommend_factor_premia.py` に `average_premia()` を切り出した（`fama_macbeth_regression` は
  それを呼ぶ薄い層になる）。既存の挙動・呼び出し側は不変（`tests/test_recommend_factor_premia.py` 全通過）。
- `plugins/model_candidates.py` は `plugins/` 配下にあるためレジストリのスキャン対象になるが、
  `plugin` 属性を持たないため登録されない。モジュール import は numpy と `.utils` のみで軽い
  （sklearn / statsmodels / lightgbm / catboost は候補構築時に遅延 import）。
- **昇格時は producer を切り出した（#372）→ 2026-07-29 に #396 で解消**。M-6 の予測は M-1/M-2 と
  同じ 52 週先対数リターン単位のため、`macro_enet_scores`（`macro_gbdt_scores` と同型・`r1_prime`
  付き）を追加し `sell_ranking` の `mu_source="macro_enet"` へ供給する。R3 足切りゲートも
  ADR-0020 のコンフォーマル区間半幅をそのまま流用して機能する。**既定 `mu_source` は M-2 のまま**
  ——rank-IC で上回っていても売り判定の出力が全面的に変わるため、`/api/backtest` の `sell` source
  で事後検証してから別途判断する（既定変更は本 ADR の対象外）。
  → **2026-07-30 に #402 / ADR-0022 で既定を M-6 へ切替済み**。検証は `/api/backtest` ではなく
  OOF 側の新指標 `short_side_spread` で行った（`sell` source は recommend 加重和の符号反転で
  `mu_source` を持たず、producer スナップショットも as-of 復元を持たないため・ADR-0022 §Context）。

## 実測（本番データ・honest OOF・embargo=12・2026-07-26）

`python -m scripts.candidate_bakeoff --with-ols`。パネル: 43ヶ月 / 57,955 サンプル / 71 特徴量 /
9 fold / OOF 13,539 件。M-2 既定 config（全マクロ・モメンタム OFF・px_* OFF・min_coverage=0.5）。

| 候補 | rank-IC | IC std | 業種中立IC | LS spread | hit | breakeven bp | turnover | 秒 |
|---|---|---|---|---|---|---|---|---|
| **elasticnet** | **+0.1713** | 0.1171 | +0.1398 | +0.1359 | 0.89 | 22.9 | 0.296 | 68 |
| fama_macbeth_ridge | +0.1653 | 0.1053 | +0.1367 | +0.1301 | 0.89 | 23.7 | 0.274 | 24 |
| extratrees | +0.1649 | 0.1330 | +0.1492 | +0.1306 | 0.89 | 18.9 | 0.345 | 131 |
| regime_linear | +0.1627 | 0.0983 | +0.1284 | +0.1324 | 0.89 | 17.6 | 0.377 | 553 |
| ols（基準線） | +0.1554 | 0.0988 | +0.1255 | +0.1250 | 0.89 | 18.8 | 0.334 | 24 |
| catboost | +0.1523 | 0.1160 | +0.1264 | +0.1149 | 0.89 | 12.3 | 0.466 | 19 |
| lightgbm | +0.1474 | 0.1079 | +0.1286 | +0.1087 | 0.89 | 10.0 | 0.545 | 8 |
| **xgb_m2（M-2 基準線）** | **+0.1419** | 0.1117 | +0.1208 | +0.1117 | 0.89 | 10.7 | 0.520 | 30 |
| fama_macbeth（断面OLS） | −0.0131 | 0.0782 | +0.0059 | −0.0172 | 0.22 | n/a | 0.532 | 13 |

M-2 基準線との rank-IC 差（`paired_ic_significance`・定常ブートストラップ・共通 9 期）:

| 候補 | 差 | 95%CI | p | 有意 | 多重比較補正 α/8=0.00625 |
|---|---|---|---|---|---|
| **elasticnet** | **+0.0294** | [+0.0116, +0.0469] | **0.002** | Yes | **通過** |
| fama_macbeth_ridge | +0.0232 | [+0.0015, +0.0467] | 0.042 | Yes | 不通過 |
| extratrees | +0.0230 | [−0.0024, +0.0516] | 0.080 | No | — |
| regime_linear | +0.0208 | [−0.0005, +0.0391] | 0.058 | No | — |
| ols | +0.0134 | [−0.0026, +0.0311] | 0.104 | No | — |
| catboost | +0.0103 | [+0.0010, +0.0192] | 0.032 | Yes | 不通過 |
| lightgbm | +0.0055 | [+0.0002, +0.0111] | 0.043 | Yes | 不通過 |
| fama_macbeth | −0.1550 | [−0.2144, −0.0877] | 0.001 | Yes（劣位） | 通過（劣位） |

**昇格判定**: 8 候補を同時に検定しているため、素の α=0.05 では偽陽性が混じる（family-wise
error rate ≈ 1−0.95⁸ ≈ 34%）。Bonferroni 補正 α/8=0.00625 を要求すると **elasticnet のみが通過**する。
よって **ElasticNet を M-6（`plugins/macro_enet.py`）へ昇格**し、他は候補のまま据え置く。
昇格後の M-6 を本番データで実行して OOF が一致することを確認済み（rank-IC 0.1713 / n_folds=9 /
α=0.062 / l1_ratio=0.32 / 非ゼロ係数 28.8 本＝候補実装と同一コードパスであることの実証）。

### 確定した知見

1. **低 S/N な日本株リターン予測では、木の非線形性よりグループ共線性への縮小推定が効く**。
   ElasticNet 0.1713 > ExtraTrees 0.1649 > CatBoost 0.1523 > LightGBM 0.1474 > XGBoost(M-2) 0.1419。
   素の OLS ですら 0.1554 で M-2 を上回った（有意ではないが、GBDT 族が線形族に勝てていない）。
   「M-2 の非線形性が効いている」という暗黙の前提は、この panel では支持されない。
2. **代替 GBDT（LightGBM / CatBoost）は XGBoost をわずかに上回るが実務的には誤差**（+0.006 / +0.010）。
   同一ハイパラで実装差だけを見た結果であり、**XGBoost 採用を変える理由にはならない**（多重比較
   補正も通らない）。この結論により Issue #372 の「代替GBDT を試す」は決着した。
   `requirements.txt` へ移す必要はなく、`requirements-optional.txt` に据え置く。
3. **Fama-MacBeth 予測ヘッドは第1段階の正則化が必須**。素の断面 OLS はマクロ列除外後も
   roe/roa/op_margin/net_margin・per/pbr のような相関群で係数が発散し（λ̄ の最大絶対値 5.37、
   予測残差の 0.9 分位が対数リターンで 5.6）、rank-IC は **負**（−0.0131・分位単調性 −0.17＝
   逆順）になった。第1段階を Ridge に替えるだけで λ̄ 最大絶対値は 0.027 へ収まり rank-IC 0.1653 へ
   跳ね上がる。**「Fama-MacBeth が効かない」のではなく「素の断面 OLS が壊れていた」**。
4. **マクロ fold 内 PCA は効果なし**。5 主成分でマクロ分散の 90.9% を説明しても rank-IC は
   elasticnet 0.1713→0.1714、xgb_m2 0.1419→0.1409 とほぼ不変で、木モデルではむしろ悪化
   （lightgbm 0.1474→0.1379・extratrees 0.1649→0.1599）。ターンオーバー・ブレークイーブンも横ばい。
   → **昇格せず**（ラッパーは探索枠に残す）。「40 本超のマクロは冗長だから圧縮が効くはず」という
   仮説は否定された（L1 正則化が既に同じ役割を果たしているためと解釈できる）。
5. **regime-switch 閾値線形は pooled 線形を超えない**（0.1627 vs elasticnet 0.1713）。VIX 中央値で
   分割してレジーム別に係数を持たせても、標本が半減する不利を上回る改善は得られなかった。
   実行時間も 553 秒と突出して重い（レジーム別 RidgeCV）。→ 昇格せず。
6. **ExtraTrees の「木予測の経験分布」区間は予測区間として使えない**。名目 80% に対し実測被覆は
   **40.6%**（半幅 0.139）。これはアンサンブル平均の epistemic なばらつきであって残差分布ではない、
   という理論的予想どおりの結果。葉モーメント版（QRF 相当）は被覆 71.4%（半幅 0.292）で名目に
   近づくがまだ不足する。一方 **family-wide の分割コンフォーマル（ADR-0020）は τ=0.9 に対し実測
   87.2〜88.5%** と全モデルで一貫して名目付近にある。→ **R1' の実装としては #365 のコンフォーマル
   区間が正解**であり、木固有の区間へ差し替える理由はない。
7. **turnover は線形族のほうが構造的に低い**（elasticnet 0.296 / fama_macbeth_ridge 0.274 vs
   xgb_m2 0.520 / lightgbm 0.545）。ブレークイーブンコストで見ると 22.9bp 対 10.7bp と 2 倍以上の差が
   あり、rank-IC の差以上にコスト後の実力差は大きい。

参考:
- Zou, H. & Hastie, T. (2005). "Regularization and variable selection via the elastic net."
  *JRSS-B* 67(2), 301-320. DOI:10.1111/j.1467-9868.2005.00503.x
- Geurts, P., Ernst, D. & Wehenkel, L. (2006). "Extremely randomized trees."
  *Machine Learning* 63, 3-42. DOI:10.1007/s10994-006-6226-1
- Meinshausen, N. (2006). "Quantile Regression Forests." *JMLR* 7, 983-999.
  → https://www.jmlr.org/papers/v7/meinshausen06a.html
- Fama, E. F. & MacBeth, J. D. (1973). "Risk, Return, and Equilibrium: Empirical Tests."
  *Journal of Political Economy* 81(3), 607-636. DOI:10.1086/260061
- Hamilton, J. D. (1989). "A New Approach to the Economic Analysis of Nonstationary Time Series
  and the Business Cycle." *Econometrica* 57(2), 357-384. DOI:10.2307/1912559
- Ke, G. et al. (2017). "LightGBM: A Highly Efficient Gradient Boosting Decision Tree." *NIPS 30*.
  → https://papers.nips.cc/paper_files/paper/2017/hash/6449f44a102fde848669bdd9eb6b76fa-Abstract.html
- Prokhorenkova, L. et al. (2018). "CatBoost: unbiased boosting with categorical features." *NeurIPS 31*.
  → https://papers.nips.cc/paper_files/paper/2018/hash/14491b756b3a51daac41c24863285549-Abstract.html

関連 ADR: 0003（M-1/M-2 公平性・fit_predict 注入）, 0004（OOF 定義）, 0008（Fama-MacBeth 因子プレミア）,
0014（purge/embargo）, 0017（pass_train_groups）, 0018（rank-IC 差の有意性検定）, 0020（コンフォーマル区間）。
