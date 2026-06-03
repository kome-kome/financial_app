# 分析モデル解説

> アプリ内の全分析モデルについて、数式・実装・仮定・限界・参考文献をまとめたドキュメントです。
> 参照元論文のURLは「参考文献」欄に記載しています。

---

## 目次

1. [総合リターン予測（Ohlsonモデル型OLS）](#1-総合リターン予測ohlsonモデル型ols)
2. [業種別OLS回帰](#2-業種別ols回帰)
3. [乖離分析（AR(1) MLE + フォールバックヒューリスティック）](#3-乖離分析ar1-mle--フォールバックヒューリスティック)
4. [株価リターン予測（月次WF-CV）](#4-株価リターン予測月次wf-cv)
5. [横断的Zスコア正規化](#5-横断的zスコア正規化)
6. [Zスコア重み付けスコアリング（おすすめ銘柄）](#6-zスコア重み付けスコアリングおすすめ銘柄)
7. [バックテスト](#7-バックテスト)
8. [ネットキャッシュ分析（清原達郎式）](#8-ネットキャッシュ分析清原達郎式)

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

業種別 OLS の結果に `diagnostics` フィールドとして含まれる（→ モデル2「診断出力」節参照）。

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
| β_sector_i | 業種 i の基準業種に対する log 価格水準差（業種定数項） | log 単位 |

係数が経済的に意味を持つのは次元整合性が保証されているため（→ 共通事項「次元整合性の原則」参照）。

### 業種固定効果（オプション、デフォルト ON）

業種ごとの P/E・P/B 水準差を One-hot ダミー変数として捉える:

```
ŷ = β₀ + β₁·EPS + β₂·BPS + β₃·CF + β₄·DPS + Σⱼ β_sector_j · 1{industry = j}
                                                             (基準業種は省略)
```

- サンプル数 ≥ 5 の業種のみダミー化（過学習防止）
- 最初の業種を基準としてドロップ（多重共線性回避）
- シミュレーション検証で R² が 0.83 → 0.97 に改善（業種差が真に存在する場合）

### モデル評価

横断的 k-fold CV（デフォルト 5-fold）で評価。

```
fold_size = n // n_folds
各fold: テストセットで R²・RMSE% を計算
```

注: 業種固定効果を有効化すると業種間構造差を吸収するため、横断的 R² は大幅改善する。

**横断的 R² の解釈**: `cv_metrics.mean_r2` は業種間の構造差により構造的に低くなる（-0.1〜0.4 程度）。R² < 0 はモデルが無価値なのではなく、「業種固定効果なしの一括回帰の限界」を反映している。業種固定効果（`use_sector_fe=True`）で改善する。

### 主要パラメータ

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `use_cf` | True | CF因子（1株営業CF）を使用するか |
| `use_sector_fe` | True | 業種固定効果（ダミー変数）を使用するか |
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

業種ごとに独立して OLS 回帰を実行し、Ohlson モデル拡張型で**株価（円/株）を推定**する。全業種一括ではなく業種内でモデルを構築することで、業種間の P/E・P/B 構造差の影響を排除する。

**次元整合性の構造的強制（CLAUDE.md 制約）**: 目的変数は `stock_price [円/株]` 単一に固定、説明変数は per-share [円/株] のみ。UI/API レベルで他の組み合わせを選べないようにすることで、係数 β を「implied 倍率」として経済的に解釈可能な状態に保つ。

### モデル定式化

業種 s 内で独立に実行（Ohlson 拡張型）:

```
ŷₛ_norm = β₀ + Σⱼ βⱼ · Xₛⱼ_norm        y = stock_price [円/株]
                                         Xⱼ = per-share 財務金額 [円/株]
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

### 説明変数（全項目 [円/株] per-share）

説明変数のキーは 2 系統:

- **DB永続 per-share**（公式開示値・株数除算不要）: `pl_eps`, `bs_bps`, `dps`
- **派生 per-share（`ps_*` プレフィックス）**: 絶対額カラム ÷ 発行株数 を実行時計算

**発行株数の推計式**:

```
shares = bs_total_equity / bs_bps     （plugins/utils.py の shares_outstanding）
ps_<feat> = <絶対額カラム> / shares     [円/株]
```

`bs_bps` または `bs_total_equity` が NULL/0 の銘柄は株数推計不能のため、業種別 OLS の集計対象から自動的に除外される。

**デフォルト10項目**（PL/BS/CF を網羅）:

| カテゴリ | キー | 元カラム / 説明 |
|---|---|---|
| PL（公式） | `pl_eps` | EPS 公式値 |
| BS（公式） | `bs_bps` | BPS 公式値 — Ohlson モデル中核 |
| 還元（公式） | `dps` | 1株配当 公式値 |
| PL（派生） | `ps_revenue` | `pl_revenue / shares` 売上トップライン |
| PL（派生） | `ps_gross_profit` | `pl_gross_profit / shares` 粗利 |
| PL（派生） | `ps_operating_profit` | `pl_operating_profit / shares` 本業収益 |
| BS（派生） | `ps_total_assets` | `bs_total_assets / shares` 企業規模 |
| BS（派生） | `ps_total_liabilities` | `bs_total_liabilities / shares` 負債規模 |
| CF（派生） | `ps_operating_cf` | `cf_operating_cf / shares` 実キャッシュ創出力 |
| CF（派生） | `ps_free_cf` | `cf_free_cf / shares` 株主還元原資 |

**派生 per-share の全選択肢**（PL/BS/CF の絶対額カラムを網羅、`ps_*` プレフィックス）:

- PL: `ps_revenue`, `ps_cost_of_sales`, `ps_gross_profit`, `ps_sga`, `ps_operating_profit`, `ps_nonoperating_income`, `ps_ordinary_profit`, `ps_net_income`
- BS資産: `ps_total_assets`, `ps_current_assets`, `ps_receivables`, `ps_inventory`, `ps_cash`, `ps_noncurrent_assets`, `ps_buildings`, `ps_machinery`, `ps_intangible_assets`, `ps_investment_securities`
- BS負債: `ps_total_liabilities`, `ps_current_liabilities`, `ps_payables`, `ps_noncurrent_liabilities`, `ps_short_term_debt`, `ps_long_term_debt`, `ps_bonds_payable`
- BS純資産: `ps_total_equity`, `ps_paid_in_capital`, `ps_retained_earnings`
- CF: `ps_operating_cf`, `ps_investing_cf`, `ps_financing_cf`, `ps_free_cf`, `ps_net_change_cash`, `ps_capex`

### 予測値の DB 書き込み

OLS で予測した株価 `ŷ_pred [円/株]` を、互換性のため `predicted_market_cap [百万円]` へ換算保存:

```
predicted_market_cap = ŷ_pred / stock_price × market_cap     [百万円]
```

`stock_price` または `market_cap` が欠損している銘柄は `predicted_market_cap` を上書きしない（DB に NULL のまま、または旧値保持）。`gap_ratio` は `ŷ_pred` と実 `stock_price` の比較で常に算出される。

### 実行条件

- 業種内のサンプル数 ≥ `min_samples`（デフォルト: 10社）でなければスキップ
- 各銘柄について `bs_bps > 0` かつ `bs_total_equity > 0` が必須（株数推計のため）

### 仮定・限界

- 業種分類はJPX上場会社一覧（TSE 33業種）による。分類の粒度が粗いため、同業種内でもビジネスモデルの差異が大きい場合がある
- 株数推計は IFRS/JGAAP 定義差、期中増資、優先株・転換社債存在時に誤差が生じる（FUTURE_TASKS の J-Quants `IssuedShares` 取得で根本解決予定）
- per-share 10項目以上選択時は PL同士・BS同士の比例関係から VIF>10 が頻発する。`check_collinearity` の警告が出た業種では Ridge への切替を強く推奨
- `gap_ratio` の収束予測には統計的根拠がない（→ [乖離分析](#3-乖離分析ou過程ヒューリスティック) を参照）
- 乖離分析（gap_analysis）は本プラグインの実行後でなければ利用不可

### 参考文献

- **Fama, E.F. & French, K.R. (1992)**. "The Cross-Section of Expected Stock Returns." *Journal of Finance*, 47(2), 427–465.
  → https://doi.org/10.1111/j.1540-6261.1992.tb04398.x
- **Greene, W.H. (2018)**. *Econometric Analysis* (8th ed.). Pearson Education.

---

## 3. 乖離分析（AR(1) MLE + フォールバックヒューリスティック）

**実装ファイル**: `plugins/gap_analysis.py`

### 概要

業種別OLS が推定した理論値と実際の時価総額の乖離率を表示し、OU（Ornstein-Uhlenbeck）
過程の離散時間版である **AR(1) を MLE 推定**して半減期を計算する。
履歴が不足する銘柄はヒューリスティックにフォールバックする。

### 乖離率（業種別OLSで計算済み）

```
gap = gap_ratio  [%]  （sector_ols.py が regression_results.gap_ratio に保存。読取は financial_metrics VIEW 経由）
```

### 半減期推定: AR(1) MLE（推奨パス）

各銘柄の年次 `gap_ratio` 履歴（≥ 8 観測）に対し statsmodels の ARIMA(1, 0, 0) を fit:

```
x_t = c + φ x_{t-1} + ε_t,   ε_t ~ N(0, σ²)
平均回帰条件: 0 < φ < 1
half_life = -ln(2) / ln(φ)  [年]
```

実装は `_estimate_ar1_half_life_years()`。推定値の妥当性チェック:
- `0 < φ < 1`（平均回帰条件）
- `0.25 年 ≤ half_life ≤ 20 年`（極端な推定値を除外）

満たさない場合は **None を返し、ヒューリスティックにフォールバック**。

### フォールバック: ヒューリスティック（履歴不足時）

履歴 < 8 観測または AR(1) 推定が失敗した銘柄に対する旧式の計算:

```
half_life = max(6, min(24, |gap| / 2))  [ヶ月]
```

n ヶ月後の期待乖離率（両ケース共通、指数減衰）:

```
gap_t = gap₀ × exp(−ln(2) / half_life × t)
```

収束スコア（参考値）:

```
conv_score₁₂ₘ = max(5, min(95, 50 + gap₀ × 0.8))  [0–100スケール]
```

### 出力フィールド

各レコードに `method`（"ar1" / "heuristic"）、`ar1_phi`、`n_history`、`half_life_months` を併記。
レスポンス全体に `n_ar1_estimated` / `n_heuristic_fallback` のサマリを返す。

### OU過程との対応

連続時間 OU 過程:

```
dX_t = κ(θ − X_t) dt + σ dW_t,   half_life = ln(2) / κ
```

離散時間 AR(1) との対応: `φ = exp(-κ Δt)` で、Δt = 1 年とすると `κ = -ln(φ)`。

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
| `nc_ratio` | `z_nc_ratio` | ネットキャッシュ比率（清原式、モデル 8 参照）|

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

## 8. ネットキャッシュ分析（清原達郎式）

**実装ファイル**: `plugins/net_cash_analysis.py`

### 概要

清原達郎『わが投資術』（2024）で提唱された **ネットキャッシュ** および **ネットキャッシュ比率** で割安株をスクリーニングする。OLS・回帰モデルを介さず会計値からの直接計算であるため、推定誤差が混入せず堅牢である点が特徴。あわせて、より保守的な **グレアムの NCAV（純流動資産価値）** と **NCAV比率** を併設し、2 系統で割安銘柄を相互検証できる。

### 数式

```
ネットキャッシュ NC  = 流動資産 + 投資有価証券 × 0.7 − 総負債      [円]   ← 清原式
ネットキャッシュ比率 = NC / 時価総額                              [無次元]

NCAV               = 流動資産 − 総負債                            [円]   ← Graham 1934（0.7補正なし）
NCAV比率           = NCAV / 時価総額                              [無次元]
```

実装では `market_cap` が百万円単位のため、`比率 = 金額 / (market_cap × 1_000_000)` で単位を整える。投資有価証券は常に NC ≥ NCAV となる（NC − NCAV = 投資有価証券 × 0.7 ≥ 0）。

### 投資有価証券に 0.7 を乗じる理由

清原氏の経験則。投資有価証券は

1. 時価評価のブレで簿価との乖離が大きい
2. 売却時に含み益課税（法人実効税率 ≈ 30%）が発生する

ため、保守的に簿価の 70% でカウントする。`INVESTMENT_DISCOUNT = 0.7` は
`plugins/net_cash_analysis.py` の定数として外出ししている。

### 銘柄選別基準（清原氏）

| 比率 | 意味 |
|---|---|
| `nc_ratio ≥ 1.0` | 時価総額より多くのネットキャッシュを保有。理屈上は **現金で会社を買える**水準 |
| `nc_ratio ≥ 0.5` | 半額バーゲン。時価総額の半分以上をネットキャッシュで保有 |
| `nc_ratio ≤ 0` | ネットキャッシュがマイナス（実質負債超過）|
| `ncav_ratio ≥ 1.5` | **グレアムのネットネット**。時価総額 < NCAV × 2/3（Graham の 2/3 ルール。`NCAV_BARGAIN_RATIO = 1.5`）|

### フィルタ設計（データ品質ガード・バリュートラップ除外）

割安株スクリーニングは「分母（時価総額）の崩れ」と「万年割安の罠」という 2 つのノイズに弱い。本モデルはこれらを次の方針で扱う。

**1. データ品質ガード（NC比率の上限・既定 ON）**

`market_cap` は実測値ではなく推計株数（`総資産÷BPS` ≒ `bs_total_equity / bs_bps`）ベースの概算であり、推計が壊れた銘柄では時価総額がほぼ 0 になり `nc_ratio` が異常値（数十〜数万倍）に発散する。比率降順ランキングの上位がこの推計崩れデータで埋まるのを防ぐため、`nc_ratio > SANITY_MAX_NC_RATIO`（既定 `5.0`、空/0 で無効）の行を除外する。

これは **割安基準ではなく純粋なデータ品質ガード**である。かつて使っていた一律の時価総額下限（例: 50 億円）は、データ誤差わずか数社を除くために正当な小型バーゲン数百社まで巻き込んで除外する「鈍器」だった（清原氏の主戦場はむしろ小型株）。サニティ上限はこの副作用なく異常値だけをピンポイントで除ける。最低時価総額は **任意フィルタ**に降格し、既定では無効。

**2. バリュートラップ除外（任意・既定 OFF）**

割安でも現金を毀損し続ける企業は「万年割安」の罠になりやすい。`require_positive_ocf`（営業CF>0）・`require_positive_ni`（当期純利益>0）を任意で要求できる。データ欠損（`NULL`）は判定不能として通す（除外しない）。

### 計算フロー

```
1. collector.py の calc_derived() で
     net_cash = current_assets + investment_securities × 0.7 − total_liabilities
   を計算し、`FinancialRecord.net_cash` カラムに書き込む（BS データのみ依存）

2. update_market_data_only() で stock_price 取得後に
     nc_ratio = net_cash / (market_cap × 1_000_000)
   を計算し、`FinancialRecord.nc_ratio` カラムに書き込む

3. database.py の _calc_zscore_for_year() で
     z_nc_ratio = (nc_ratio − μ_year) / σ_year
   を年度内 Zスコアとして算出（モデル 5 と統合）
```

### 投資有価証券の取得対応

| 会計基準 | XBRL 要素（`XBRL_MAP` に登録） |
|---|---|
| JGAAP | `InvestmentSecurities` / `InvestmentsInSecurities` / `ShortTermInvestmentSecurities` |
| IFRS | `OtherFinancialAssetsNonCurrentIFRS`（近似値）|

IFRS には完全に対応する科目がないため、「非流動その他金融資産」で近似する。
収集前の古いレコードは `bs_investment_securities = NULL` となり、内部計算では **0 として扱う** ことで簡易 NCAV (Net Current Asset Value, Graham 1934) 相当の値を返す。

### 仮定・限界

- **投資有価証券の評価**: 簿価 × 0.7 は単純化された経験則。個別銘柄の含み益・含み損や、政策保有株のように売却制約のある銘柄は実態を反映しない。
- **特別損失リスク**: 流動資産に含まれる売掛金・棚卸資産は将来貸倒れ・評価減の可能性がある。NC が正でも実際に資産が現金化できる保証はない。
- **IFRS 採用企業**: 「投資有価証券」に厳密に対応する科目がないため、`OtherFinancialAssetsNonCurrentIFRS` で近似する。区分が異なる場合は値が過大・過少になる。
- **古いレコード**: 2026 年 5 月の本機能リリース前のデータは投資有価証券が未収集（NULL）。プラグインは内部的に 0 として扱い、NCAV 相当の値を返すため過小評価方向のバイアスがかかる。再収集することで清原式精度に到達する。
- **会計のクセ**: 商社・金融・REIT 等は BS 構造が特殊で、本指標がうまく機能しない業種がある。業種フィルタで除外することを推奨。
- **株価のタイミング**: `market_cap` は最新の `stock_price` × 推計発行株式数。決算日と現在株価の時点ズレがある。
- **時価総額の推計誤差（重要）**: 発行株式数は `bs_total_equity / bs_bps` で近似するため、IFRS/JGAAP 混在や端株で推計が崩れると `market_cap` が過小（極端な場合ほぼ 0）になり、`nc_ratio` を上振れさせる。上振れは系統的に起きるため、本モデルはサニティ上限で異常値を除外する一方、上限以下でも比率はやや楽観方向のバイアスを含むと解釈すべき。NCAV比率も同じ分母を使うため同様。

### 参考文献

- **清原達郎 (2024)**. 『わが投資術 — 市場は誰に微笑むか』. 講談社.
  → https://bookclub.kodansha.co.jp/product?item=0000392773
  （ネットキャッシュ比率と投資有価証券 0.7 倍ルールの一次出典）
- **Graham, B. (1934)**. *Security Analysis*. McGraw-Hill.
  → https://www.mheducation.com/highered/product/security-analysis-graham-dodd/M9780071592536.html
  （Net Current Asset Value (NCAV) = 流動資産 − 総負債 の原典。投資有価証券補正のない基本形）
- **Oppenheimer, H.R. (1986)**. "Ben Graham's Net Current Asset Values: A Performance Update." *Financial Analysts Journal*, 42(6), 40–47.
  → https://doi.org/10.2469/faj.v42.n6.40
  （NCAV 戦略の超過収益の学術的検証）

---

## 改訂履歴

| 日付 | 内容 |
|---|---|
| 2026-05-14 | 初版作成（モデル 1–7 を記述）|
| 2026-05-21 | モデル 8（ネットキャッシュ分析・清原達郎式）を追加 |
| 2026-06-01 | モデル 8 に Graham NCAV 指標・NCAV比率（2/3ルール）を併設。一律の時価総額下限を廃しデータ品質ガード（NC比率サニティ上限）に置換。営業CF>0/純利益>0 のバリュートラップ除外を追加 |
