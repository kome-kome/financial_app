# 分析モデル解説

> アプリ内の全分析モデルについて、数式・実装・仮定・限界・参考文献をまとめたドキュメントです。
> 参照元論文のURLは「参考文献」欄に記載しています。

---

## 目次

1. [総合リターン予測（Ohlsonモデル型OLS）](#1-総合リターン予測ohlsonモデル型ols)
2. [業種別OLS回帰](#2-業種別ols回帰)
3. [乖離分析（OU過程ヒューリスティック）](#3-乖離分析ou過程ヒューリスティック)
4. [株価リターン予測（月次WF-CV）](#4-株価リターン予測月次wf-cv)
5. [横断的Zスコア正規化](#5-横断的zスコア正規化)
6. [Zスコア重み付けスコアリング（おすすめ銘柄）](#6-zスコア重み付けスコアリングおすすめ銘柄)
7. [バックテスト](#7-バックテスト)

---

## 共通事項

### 外れ値処理（全モデル必須）

```
winsorize(X, lo=p1, hi=p99)
  → X を [第1百分位, 第99百分位] の範囲にクリップ
```

日本株データは BPS・EPS に p99 の数百倍の外れ値が存在し、無処理では OLS の行列反転が数値的に破綻する（R² が −10³² になる等）。

実装: `plugins/utils.py` の `winsorize()`

### 次元整合性の原則

OLS 係数が経済的に解釈できるためには、説明変数と被説明変数の次元が一致していなければならない。

| ✅ 正しい例 | ❌ 誤った例 |
|---|---|
| EPS/BPS [円/株] → 株価 [円/株] | ROE [%] → 時価総額 [百万円] |
| ログリターン [無次元] → ログリターン [無次元] | 自己資本比率 [%] → 株価 [円] |

詳細: `CLAUDE.md` の「分析モデルの次元整合性（必須）」参照

### OLS実装

正規方程式による解法:

```
β = (X'X)⁻¹ X'y
```

実装: `plugins/utils.py` の `ols()`。`numpy.linalg.lstsq`（SVD ベース）による
最小二乗解を採用しており、Gauss-Jordan 消去法より条件数の悪い行列で数値的に
安定。返り値に `rank` と `condition_number` を含む。

#### 詳細診断版（`ols_with_diagnostics()`）

`statsmodels.OLS` を利用した詳細統計診断付き OLS。標準の `ols()` に加えて:

- **Durbin-Watson 統計量**: 残差自己相関の検定（1.5〜2.5 で問題なし）
- **Jarque-Bera 検定**: 残差正規性（歪度・尖度を含む）
- **F 統計量・p 値**: モデル全体の有意性検定
- **HC3 ロバスト標準誤差**: 不均一分散に対応した SE（`cov_type="HC3"`）

業種別 OLS の結果に `diagnostics` フィールドとして含まれる。

#### 係数の有意性（標準誤差・t統計量・p値）

各係数 β_i に対し以下を返す:

```
σ²       = SSE / (n - p)                ← 残差分散
Var(β_i) = σ² × [(X'X)⁻¹]_{ii}          ← 係数の分散
SE(β_i)  = √Var(β_i)                    ← 標準誤差
t_i      = β_i / SE(β_i)                ← t統計量
p_i      = 2 × (1 − Φ(|t_i|))           ← 両側p値（df ≥ 30）
```

ここで df = n − p。p 値の計算には `scipy.stats.t.sf` を使用しており、
df < 30 の小サンプル領域でも正確（旧 Pure Python 実装の正規近似 + Cornish-Fisher
補正からの置換済み）。

慣例的に p < 0.05 を「有意」とみなす。業種別OLSの結果には
`n_significant_features`（p < 0.05 の説明変数数）が併記される。

#### 多重共線性チェック（VIF・Pearson相関）

実装: `plugins/utils.py` の `check_collinearity()`

```
VIF_i = 1 / (1 - R²_i)
  ここで R²_i は特徴量 i を残りの特徴量で回帰したときの決定係数
```

慣例的に **VIF > 10** または **|Pearson r| > 0.9** で多重共線性ありと判断
（Kutner et al. 2005, *Applied Linear Regression Models*）。

業種別OLS結果に `collinearity_warnings.high_corr_pairs` /
`high_vif` が含まれる。閾値超過があれば変数の削減・正則化（Ridge 等）を検討する。

#### Ridge 回帰（L2 正則化）

実装: `plugins/utils.py` の `ridge_regression()`（`sklearn.linear_model.RidgeCV` 経由）

```
β_ridge = arg min ‖Xβ - y‖² + α‖β‖²
```

最適 α は CV（クロスバリデーション）で `[1e-3, 1e-2, 0.1, 1, 10, 100, 1000]` から選択。
業種別 OLS の `regularization="ridge"` パラメータで切替可能。多重共線性が顕著な業種
（VIF > 10 や |相関| > 0.9）では予測安定性が向上する反面、係数の統計推論
（SE / t / p 値）は伝統的に定義されないため NaN を返す。

---

## 1. 総合リターン予測（Ohlsonモデル型OLS）

**実装ファイル**: `plugins/total_return.py`

### 概要

Ohlson (1995) が提唱した残余利益モデルの OLS 近似。1株当たりの財務金額から理論株価を推定し、現在株価との乖離（上昇余地）と配当利回りを合算して総合リターンを予測する。

### モデル定式化

**目的変数**: 株価 y [円/株]

**説明変数**（全て [円/株] — 次元整合）:

| 変数 | 内容 | 欠損補完 |
|---|---|---|
| `pl_eps` | EPS（1株当たり純利益） | 除外 |
| `bs_bps` | BPS（1株当たり純資産） | 除外 |
| `cf_ops_ps` | 1株当たり営業CF = 営業CF ÷ 発行株式数 | 0補完 |
| `dps` | 1株当たり配当 | 0補完 |

ただし発行株式数の推計:

```
発行株式数 ≈ bs_total_equity / bs_bps
```

**前処理パイプライン**:

```
X_raw → winsorize(p1–p99) → z-score正規化 → X_norm
y_raw → log変換 → z-score正規化 → y_norm

OLS: ŷ_norm = β₀ + β₁·EPS_norm + β₂·BPS_norm + β₃·CF_norm + β₄·DPS_norm
```

**予測値の逆変換**:

```
ŷ_raw = exp( min(ŷ_norm · σ_y + μ_y, 15.0) )  [円/株]
```

上限 `exp(15) ≈ 3.3百万円/株` は発散防止のキャップ。

**総合リターンの計算**:

```
上昇余地 = (ŷ_raw − 実際株価) / 実際株価 × 100  [%]
期待総合リターン = 上昇余地 + 配当利回り  [%]
```

### OLS係数の経済的解釈

| 係数 | 解釈 | 単位 |
|---|---|---|
| β₁ (EPS) | implied P/E 倍率 | 無次元 |
| β₂ (BPS) | implied P/B 倍率 | 無次元 |
| β₃ (CF) | implied Price/CF 倍率 | 無次元 |
| β₄ (DPS) | implied 配当割引倍率 | 無次元 |

係数が経済的に意味を持つのは、説明変数と被説明変数の次元が一致しているため（全て [円/株]）。

### モデル評価

横断的 k-fold CV（デフォルト 5-fold）で評価。

```
fold_size = n // n_folds
各fold: テストセットで R²・RMSE% を計算
```

注: 横断的 R² は全業種一括回帰のため、業種間の P/E・P/B 構造差により構造的に低くなる（−0.1 〜 0.4 程度）。R² < 0 はモデルが無価値であることを意味しない。

### 主要パラメータ

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `n_folds` | 5 | k-fold 分割数 |
| `top_n` | 20 | 上位表示件数 |
| `use_cf` | True | 営業CF特徴量の使用 |
| `LOG_PRED_CAP` | 15.0 | 予測値の log-space 上限 |

### 仮定・限界

- 発行株式数を `total_equity / bps` で推計（IFRS/JGAAP 混在時に精度低下）
- `market_cap` のみ百万円単位（他は円単位）— 直接比較禁止
- 業種固定効果なし（構造的に R² が低い）
- 年次財務データの決算ラグ（公表まで 45 日程度）は未考慮

### 参考文献

- **Ohlson, J.A. (1995)**. "Earnings, Book Values, and Dividends in Equity Valuation." *Contemporary Accounting Research*, 11(2), 661–687.
  → https://doi.org/10.1111/j.1911-3846.1995.tb00461.x
- **Feltham, G.A. & Ohlson, J.A. (1995)**. "Valuation and Clean Surplus Accounting for Operating and Financial Activities." *Contemporary Accounting Research*, 11(2), 689–731.
  → https://doi.org/10.1111/j.1911-3846.1995.tb00462.x

---

## 2. 業種別OLS回帰

**実装ファイル**: `plugins/sector_ols.py`

### 概要

業種ごとに独立して OLS 回帰を実行し、理論的な時価総額（または株価）を推定する。全業種一括ではなく業種内でモデルを構築することで、業種間の P/E・P/B 構造差の影響を排除する。

### モデル定式化

業種 s 内で独立に実行:

```
ŷₛ_norm = β₀ + Σⱼ βⱼ · Xₛⱼ_norm
```

各変数は業種内で個別に winsorize & z-score 正規化:

```
Xₛⱼ_norm = (Xₛⱼ_winsorized − μₛⱼ) / σₛⱼ
```

**乖離率の定義**:

```
gap_ratio = (ŷ_raw − y_actual) / y_actual × 100  [%]

gap_ratio > 0: 予測 > 実際 → 割安（市場が過小評価）
gap_ratio < 0: 予測 < 実際 → 割高（市場が過大評価）
```

### ターゲット別の変数選択

**ターゲット = `market_cap`（百万円）** — デフォルト

| 説明変数 | 次元 |
|---|---|
| `pl_revenue` | 円（絶対額） |
| `pl_operating_profit` | 円（絶対額） |
| `pl_net_income` | 円（絶対額） |
| `bs_total_equity` | 円（絶対額） |
| `cf_operating_cf` | 円（絶対額） |

説明変数が絶対額、被説明変数も百万円（絶対額）— 次元整合。

**ターゲット = `stock_price`（円/株）**

| 説明変数 | 次元 |
|---|---|
| `pl_eps` | 円/株 |
| `bs_bps` | 円/株 |
| `dps` | 円/株 |

### 実行条件

- 業種内のサンプル数 ≥ `min_samples`（デフォルト: 10社）でなければスキップ

### 仮定・限界

- 業種分類はJPX上場会社一覧（TSE 33業種）による。分類の粒度が粗いため、同業種内でもビジネスモデルの差異が大きい場合がある
- `gap_ratio` の収束予測には統計的根拠がない（→ [乖離分析](#3-乖離分析ou過程ヒューリスティック) を参照）
- 乖離分析（gap_analysis）は本プラグインの実行後でなければ利用不可

### 参考文献

- **Fama, E.F. & French, K.R. (1992)**. "The Cross-Section of Expected Stock Returns." *Journal of Finance*, 47(2), 427–465.
  → https://doi.org/10.1111/j.1540-6261.1992.tb04398.x
- **Greene, W.H. (2018)**. *Econometric Analysis* (8th ed.). Pearson Education.

---

## 3. 乖離分析（OU過程ヒューリスティック）

**実装ファイル**: `plugins/gap_analysis.py`

### 概要

業種別OLS が推定した理論値と実際の時価総額の乖離率を表示し、**OU（Ornstein-Uhlenbeck）過程を単純化した**収束予測を付加する。

**注意**: 収束予測部分は統計的根拠のないヒューリスティックであり、UI 上は「参考値」として表示する。

### 乖離率（業種別OLSで計算済み）

```
gap = gap_ratio  [%]  （sector_ols.py が FinancialRecord.gap_ratio に保存）
```

### 収束予測のヒューリスティック

OU過程の平均回帰特性を参考に、乖離率が半減するまでの期間（half-life）を **統計推定なしに** 乖離幅から決定:

```
half_life = max(6, min(24, |gap| / 2))  [ヶ月]
```

n ヶ月後の期待乖離率（指数減衰）:

```
gap_t = gap₀ × exp(−ln(2) / half_life × t)
```

収束スコア（参考値）:

```
conv_score₁₂ₘ = max(5, min(95, 50 + gap₀ × 0.8))  [0–100スケール]
```

### OU過程との対応（参考）

本来の OU 過程:

```
dX_t = κ(θ − X_t) dt + σ dW_t

θ  : 長期均衡値（= 0 と仮定）
κ  : 平均回帰速度  →  half_life = ln(2) / κ
σ  : ボラティリティ
```

本実装では κ を `ln(2) / half_life` で代替しており、κ の統計的推定は行っていない。正確な推定には過去の gap_ratio 時系列への最尤推定が必要。

### 改善余地

- `gap_ratio` の時系列が十分に蓄積されれば、κ・σ の ML 推定に置き換え可能
- 現状は参考値であり、投資判断の主要根拠として使用すべきでない

### 参考文献

- **Ornstein, L.S. & Uhlenbeck, G.E. (1930)**. "On the Theory of the Brownian Motion." *Physical Review*, 36(5), 823–841.
  → https://doi.org/10.1103/PhysRev.36.823
- **Elliott, R.J., van der Hoek, J., & Malcolm, W.P. (2005)**. "Pairs trading." *Quantitative Finance*, 5(3), 271–276.
  → https://doi.org/10.1080/14697680500149370
- **Vasicek, O. (1977)**. "An equilibrium characterization of the term structure." *Journal of Financial Economics*, 5(2), 177–188.
  → https://doi.org/10.1016/0304-405X(77)90016-2

---

## 4. 株価リターン予測（月次WF-CV）

**実装ファイル**: `plugins/price_predictor.py`

### 概要

日次株価履歴（OHLCV）と年次財務指標を組み合わせ、N 日先の株価対数リターンを OLS で予測する。**月次ウォークフォワード CV（ルックアヘッドバイアスなし）** で評価する。

### 目的変数

```
y = log(C_{t+N} / C_t)  [無次元対数リターン]

N ∈ {5, 20, 60}日  （ユーザー選択）
```

### 特徴量（全て無次元 — 次元整合）

**価格系特徴量**（`StockPriceHistory` から計算）:

| 変数 | 定義 | 範囲 |
|---|---|---|
| `ma20_dev` | (C − MA20) / MA20 | (−1, +∞) |
| `vol60` | 過去60日のログリターンの標準偏差 | [0, +∞) |
| `rsi14` | RSI(14) = 100 − 100/(1+RS)、RS = 平均上昇 / 平均下落 | [0, 100] |
| `atr_ratio` | ATR(14) / C（True Range の 14日平均を現在値で除したもの） | [0, +∞) |

**財務系特徴量**（`FinancialRecord` から結合）:

| 変数 | 内容 |
|---|---|
| `per` | 株価収益率 |
| `pbr` | 株価純資産倍率 |
| `roe` | 自己資本利益率 [%] |
| `equity_ratio` | 自己資本比率 [%] |
| `z_op_margin` | 営業利益率 Zスコア（年度別正規化済み） |
| `z_roe` | ROE Zスコア |
| `z_cf_ratio` | 営業CF/売上比 Zスコア |
| `gap_ratio` | 業種別OLS乖離率 [%] |

**決算公表ラグ**: 財務データは period_end から 45 日後に利用可能とみなして結合する（前倒し利用によるルックアヘッドバイアスを防止）。

### RSI の計算

```
changes = [C_i − C_{i-1}  for i in (t-14, t)]
avg_gain = mean([max(c, 0)  for c in changes])
avg_loss = mean([abs(min(c, 0))  for c in changes])
RS = avg_gain / avg_loss  （avg_loss = 0 の場合: RSI = 100 or 50）
RSI = 100 − 100 / (1 + RS)
```

### ATR の計算

```
TR_i = max(H_i − L_i,  |H_i − C_{i-1}|,  |L_i − C_{i-1}|)
ATR(14) = mean(TR_i  for i in (t-14, t))
atr_ratio = ATR(14) / C_t
```

### 月次ウォークフォワードCV

```
全月度 = ["YYYY-MM", "YYYY-MM", ...]  （昇順）

For i = min_train_months to len(全月度)−1, step = step_months:
  test_month  = 全月度[i]
  train_months = 全月度[:i]  （i未満の全月）

  学習: train_months の全企業 × 全スナップショット
  テスト: test_month のスナップショットのみ
  評価: テストセットで R²・RMSE を計算
```

**ルックアヘッドバイアス防止**:
- テスト月のデータは学習に一切使用しない
- 正規化パラメータも学習データのみから計算

| パラメータ | 値 |
|---|---|
| `min_train_months` | 18 ヶ月（データ不足時は 6 ヶ月に緩和） |
| `step_months` | 3 |

### 仮定・限界

- 線形 OLS であるため、特徴量と目的変数の非線形関係を捉えられない（RSI の U 字型効果等）
- 財務データは年 1 回更新のため、月次スナップショットでは同じ財務値が繰り返し使用される
- gap_ratio が NULL の場合は財務特徴量が欠損になる（業種別OLS未実行時）

### 参考文献

- **Wilder, J.W. (1978)**. *New Concepts in Technical Trading Systems*. Trend Research.
  （RSI・ATR の原典）
- **Bergmeir, C. & Benítez, J.M. (2012)**. "On the use of cross-validation for time series predictor evaluation." *Information Sciences*, 191, 192–213.
  → https://doi.org/10.1016/j.ins.2011.12.028
- **Hyndman, R.J. & Athanasopoulos, G. (2021)**. *Forecasting: Principles and Practice* (3rd ed.). OTexts.
  → https://otexts.com/fpp3/

---

## 5. 横断的Zスコア正規化

**実装ファイル**: `database.py` の `calc_zscore_normalization()` / `_calc_zscore_for_year()`

### 概要

財務指標を年度別に横断的な Zスコアに変換する。これにより異なる企業間で指標の相対的な優劣を比較可能にする。

**注意**: ここでの「Zスコア」は標準化スコア（standard score）であり、倒産予測のための **Altman の Z スコア（1968）とは無関係**。

### 数式

年度 y の企業 i に対して:

```
z_field_i = (field_i − μ_y) / σ_y

μ_y = mean({ field_j : field_j ≠ NULL, year(j) = y })
σ_y = stdev({ field_j : field_j ≠ NULL, year(j) = y })
```

### 正規化対象フィールド

| 元フィールド | Zスコアフィールド | 内容 |
|---|---|---|
| `pl_revenue` | `z_revenue` | 売上高 |
| `op_margin` | `z_op_margin` | 営業利益率 |
| `roe` | `z_roe` | ROE |
| `equity_ratio` | `z_equity_ratio` | 自己資本比率 |
| `cf_ratio` | `z_cf_ratio` | 営業CF/売上比 |
| `pl_eps` | `z_eps` | EPS |
| `de_ratio` | `z_de_ratio` | D/Eレシオ |

### 年度別計算の理由

異なるマクロ環境（金融緩和期・引き締め期等）の年度を混在させると、比較が無意味になる。例えば低金利期は全体的に PER が高いため、同じ企業でも年度をまたいで比較するとバイアスが生じる。

### 仮定・限界

- 全上場銘柄を同一母集団として正規化する（業種固定効果なし）。業種間で指標の分布が大きく異なる場合、業種内での相対評価が歪む可能性がある
- 正規化後のZスコアは異なる年度間でも比較可能だが、年度ごとに計算しているため分布の形状は年度によって異なる

### 参考文献

（標準スコアは統計学の基礎知識であり、特定の論文を参照するものではない）

- **Altman, E.I. (1968)** の Zスコアとは別概念であることに注意:
  Altman, E.I. (1968). "Financial Ratios, Discriminant Analysis and the Prediction of Corporate Bankruptcy." *Journal of Finance*, 23(4), 589–609.
  → https://doi.org/10.1111/j.1540-6261.1968.tb00843.x

---

## 6. Zスコア重み付けスコアリング（おすすめ銘柄）

**実装ファイル**: `plugins/recommend.py`

### 概要

各指標の Zスコアを重み付け線形結合してスコアを計算し、企業をランキングする。事前定義された 4 プリセットまたはカスタムウェイトを使用できる。

### スコア計算式

```
score_i = Σⱼ∈present (weight_j × z_metric_j_i) / Σⱼ∈present |weight_j|

z_metric_j_i : 企業 i の指標 j の Zスコア（年度別正規化済み）
weight_j      : 指標 j の重み（ユーザー設定）
present       : 企業 i において値が NULL でない指標の集合
```

**weighted mean** で計算するため、指標カバレッジが異なる銘柄を公平に比較できる
（旧実装の単純和では値が揃った銘柄が有利だった）。

### カバレッジフィルタ

```
coverage_i = Σⱼ∈present |weight_j| / Σⱼ |weight_j|
```

`min_coverage`（デフォルト 0.5）未満の企業はランキングから除外する。
`min_coverage = 1.0` を指定すれば全指標が揃った企業のみが対象になる。

### デフォルトプリセット

| プリセット | z_roe | z_op_margin | z_revenue | z_cf_ratio | z_equity_ratio | z_eps | gap_ratio | z_de_ratio |
|---|---|---|---|---|---|---|---|---|
| バランス型 | 1.0 | 1.0 | 0.8 | 0.8 | 0.5 | — | 0.5 | — |
| 成長重視 | 1.0 | 0.5 | 2.0 | 0.5 | — | — | 0.3 | — |
| 割安重視 | 1.0 | 1.0 | — | — | 0.5 | — | 2.0 | — |
| 高収益重視 | 2.0 | 2.0 | — | 1.0 | 0.5 | — | — | — |

`gap_ratio` は業種別OLS乖離率であり、「割安度」の指標として機能する（正値 = 割安）。

### 仮定・限界

- ウェイト設定に数学的・経済学的な根拠はなく、直感的なヒューリスティック
- 各 Zスコアは年度内の相対評価であり、絶対的な財務水準は反映しない
- モデルの有効性は [バックテスト](#7-バックテスト) で検証すること

### 参考文献

- ファクター投資の学術的基礎:
  **Fama, E.F. & French, K.R. (1993)**. "Common risk factors in the returns on stocks and bonds." *Journal of Financial Economics*, 33(1), 3–56.
  → https://doi.org/10.1016/0304-405X(93)90023-5
- スマートベータ・ファクタースコアリングの実務:
  **Asness, C., Frazzini, A., Israel, R., & Moskowitz, T. (2015)**. "Fact, Fiction, and Value Investing." *Journal of Portfolio Management*, 42(1), 34–52.
  → https://doi.org/10.3905/jpm.2015.42.1.034

---

## 7. バックテスト

**実装ファイル**: `api.py` の `/api/backtest`・`/api/backtest/multi` エンドポイント

### 概要

過去 N ヶ月前の時点で確定していた財務データを使いスコアリングし、その後の実際の株価リターンを計算する。モデルの有効性を事後的に検証するために使用する。マルチピリオド比較（3/6/12/18/24 ヶ月）により保有期間と有効性の関係も分析できる。

### 計算ロジック

```
start_date = today − months_ago × 30日

1. start_date 以前に period_end が確定しているレコードで
   各社の最新年度のデータを取得

2. recommend.py と同じ重み付きスコアで全社をランキング

3. 上位 top_n 社について:
   始値 = start_date 以降の最初の終値
   終値 = 最新の終値

4. 実績リターン ri = (終値i − 始値i) / 始値i × 100  [%]
```

**ベンチマーク**: スコアリング対象企業全体（最大 500 社）の平均リターン

```
超過収益 = 上位N社平均リターン − ベンチマーク平均リターン
```

### サマリー統計

| 統計量 | 計算式 |
|---|---|
| 平均リターン | μ = Σri / n |
| 標準偏差 | σ = √(Σ(ri−μ)² / n) |
| 中央値 | p50（線形補間） |
| 勝率 | #{ri > 0} / n × 100% |
| パーセンタイル | p5, p25, p75, p95（線形補間） |

パーセンタイルは現状 Pure Python 実装（`_bt_percentile(sorted_arr, p)`）。numpy/scipy への置換は VISION.md の採用基準を満たすため許可されている（将来の高速化候補）。

### 注意事項

- 厳密な時点整合性の制約: `period_end <= start_date` を条件とするが、実際の有価証券報告書の公開日（決算期末から 45–60 日後）は考慮していない。`months_ago` を短く設定すると前の決算データしか使えないことに注意
- 生存バイアス: 現在 DB に存在する企業のみを対象とする（過去に上場廃止した企業は含まれない）
- ベンチマークは全上場銘柄ではなく、DBに収録されスコアリングが可能な企業の部分集合

### 参考文献

- **López de Prado, M. (2018)**. *Advances in Financial Machine Learning*. Wiley.
  → https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086
  （第 11 章: バックテストの統計的有意性、生存バイアス・ルックアヘッドバイアスの解説）
- **Bailey, D.H. & López de Prado, M. (2012)**. "The Sharpe Ratio Efficient Frontier." *Journal of Risk*, 15(2), 3–44.
  → https://doi.org/10.21314/JOR.2012.255

---

## 改訂履歴

| 日付 | 内容 |
|---|---|
| 2026-05-14 | 初版作成（モデル 1–7 を記述）|
