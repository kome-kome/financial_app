# 分析モデル解説

> アプリ内の全分析モデルについて、数式・実装・仮定・限界・参考文献をまとめたドキュメントです。
> 参照元論文のURLは「参考文献」欄に記載しています。
> **初心者向けの噛み砕いた解説は `/guide`（`templates/guide.html`）** にあります。本ドキュメント（および `models.html`）は数式・論文中心の技術版です。

---

## 目次

1. [総合リターン予測 → バリュエーション分析へ統合（§3参照）](#1-総合リターン予測--バリュエーション分析へ統合3参照)
2. [業種別OLS回帰](#2-業種別ols回帰)
3. [バリュエーション分析（割安度＋平均回帰＋期待総リターン）](#3-バリュエーション分析割安度平均回帰期待総リターン)
4. [株価リターン予測（月次WF-CV）](#4-株価リターン予測月次wf-cv)
5. [横断的Zスコア正規化](#5-横断的zスコア正規化)
6. [Zスコア重み付けスコアリング（おすすめ銘柄）](#6-zスコア重み付けスコアリングおすすめ銘柄)
7. [バックテスト](#7-バックテスト)
8. [ネットキャッシュ分析（清原達郎式）](#8-ネットキャッシュ分析清原達郎式)
9. [マクロ×リスク-リターン推奨](#9-マクロリスク-リターン推奨)
10. [売り候補ランキング（保有銘柄の売り時）](#10-売り候補ランキング保有銘柄の売り時)

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

## 1. 総合リターン予測 → バリュエーション分析へ統合（§3参照）

**旧実装ファイル**: `plugins/total_return.py`（廃止）

旧「総合リターン予測」は独自のプール OLS（全市場一括＋業種ダミー）で理論株価を推定し、
上昇余地＋配当利回りを総合リターンとしてランキングしていた。この OLS エンジンは
**業種別OLS（§2）**と二重化していたため統合し、本モデルは廃止した（ADR-0001）。

- 理論株価の推定は **業種別OLS（§2）1本**に集約（業種内回帰でプール回帰より精緻）。
- 「期待総リターン＝乖離率＋配当利回り」というランキングは **バリュエーション分析（§3）**へ吸収し、
  `gap_ratio` seam ＋ 配当利回りから算出する（独自 OLS 不要）。
- implied P/E・P/B は **予測株価 ÷ EPS・BPS** で銘柄ごとに復元する（プール回帰の β 係数解釈は失うが、業種別係数で代替）。

理論的背景（Ohlson 残余利益モデル）は §2・§3 の per-share 回帰に引き継がれている。

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

- PL: `ps_revenue`, `ps_cost_of_sales`, `ps_gross_profit`, `ps_sga`, `ps_rd_expenses`†, `ps_operating_profit`, `ps_depreciation`†, `ps_nonoperating_income`, `ps_ordinary_profit`, `ps_extraordinary_income`†, `ps_extraordinary_loss`†, `ps_pretax_profit`, `ps_net_income`
- BS資産: `ps_total_assets`, `ps_current_assets`, `ps_receivables`, `ps_inventory`, `ps_cash`, `ps_noncurrent_assets`, `ps_buildings`, `ps_machinery`, `ps_ppe_total`†, `ps_intangible_assets`, `ps_investments_other_assets`†, `ps_investment_securities`
- BS負債: `ps_total_liabilities`, `ps_current_liabilities`, `ps_payables`, `ps_noncurrent_liabilities`, `ps_short_term_debt`, `ps_long_term_debt`, `ps_bonds_payable`
- BS純資産: `ps_total_equity`, `ps_paid_in_capital`, `ps_retained_earnings`
- CF: `ps_operating_cf`, `ps_investing_cf`, `ps_financing_cf`, `ps_free_cf`, `ps_net_change_cash`, `ps_capex`

**† C2 収集列の結線**（研究開発費 `pl_rd_expenses` / 減価償却費 `pl_depreciation` / 有形固定資産合計 `bs_ppe_total` / 投資その他の資産合計 `bs_investments_other_assets` / 特別損益 `pl_extraordinary_income`・`pl_extraordinary_loss`）を per-share 派生として選択可能にした。**デフォルト10項目には含めない**（選択肢としてのみ提供）。理由は次の2点:
- **欠損による標本縮小**: sector_ols は選択した全特徴量が non-null の銘柄のみを集計するため、欠損が広い列（特別損益は JGAAP 専用で IFRS/US-GAAP 連結は概ね null、研究開発費は非研究開発企業で null）をデフォルトに入れると業種ごとの標本が激減する。
- **多重共線性**: per-share 11 項目以上で VIF>10 が頻発するため、C2 列は「研究開発集約度・資本集約度を見たい業種で明示選択し、必要に応じ Ridge 併用」という運用を推奨。

### 予測値の DB 書き込み

OLS で予測した株価 `ŷ_pred [円/株]` を、互換性のため `predicted_market_cap [百万円]` へ換算保存:

```
predicted_market_cap = ŷ_pred / stock_price × market_cap     [百万円]
```

`stock_price` または `market_cap` が欠損している銘柄は `predicted_market_cap` を上書きしない（DB に NULL のまま、または旧値保持）。`gap_ratio` は `ŷ_pred` と実 `stock_price` の比較で常に算出される。

### 実行条件

- 業種内のサンプル数 ≥ `min_samples`（デフォルト: 5社）でなければスキップ
- 各銘柄に発行株数が必要。`issued_shares`（XBRL 直接値・fill率100%）を優先し、欠損時のみ `bs_total_equity ÷ bs_bps` で推計する（`plugins/utils.shares_outstanding`）。どちらでも株数を求められない銘柄のみ対象外（`bs_bps` 欠損だけでは除外されない）
- **説明変数の自動ドロップ**（`_select_features`）: 説明変数は「選択列が1つでも NULL の銘柄を AND 除外」する仕様のため、欠損列を重ねると全銘柄が除外され 0 業種に潰れる。これを防ぐため (1) 母集団での欠損率が `MAX_FEATURE_MISSING_RATE`（50%）超の列を一括除外し、(2) なおどの業種も `min_samples` に届かない場合は欠損の多い列から1つずつ除外して、いずれかの業種が `min_samples` に届くまで繰り返す。除外列は結果の `dropped_features` で返し、画面に警告表示する

### 仮定・限界

- 業種分類はJPX上場会社一覧（TSE 33業種）による。分類の粒度が粗いため、同業種内でもビジネスモデルの差異が大きい場合がある
- 株数推計は IFRS/JGAAP 定義差、期中増資、優先株・転換社債存在時に誤差が生じる（FUTURE_TASKS の J-Quants `IssuedShares` 取得で根本解決予定）
- per-share 10項目以上選択時は PL同士・BS同士の比例関係から VIF>10 が頻発する。`check_collinearity` の警告が出た業種では Ridge への切替を強く推奨
- `gap_ratio` の収束予測には統計的根拠がない（→ [乖離分析](#3-乖離分析ar1-mle--フォールバックヒューリスティック) を参照）
- 乖離分析（gap_analysis）は本プラグインの実行後でなければ利用不可

### 参考文献

- **Fama, E.F. & French, K.R. (1992)**. "The Cross-Section of Expected Stock Returns." *Journal of Finance*, 47(2), 427–465.
  → https://doi.org/10.1111/j.1540-6261.1992.tb04398.x
- **Greene, W.H. (2018)**. *Econometric Analysis* (8th ed.). Pearson Education.

---

## 3. バリュエーション分析（割安度＋平均回帰＋期待総リターン）

**実装ファイル**: `plugins/gap_analysis.py`（内部 slug・`/api/gap-analysis` は後方互換で維持。表示ラベルは「バリュエーション分析」）

### 概要

業種別OLS（§2）が推定した理論値と実際値の乖離率（割安度）を起点に、バリュエーション系の
出力を一括で出すハブ。3 つの出力を持つ:

1. **割安度**: `gap_ratio`（業種内 OLS 理論株価との乖離率 [%]）
2. **平均回帰タイミング**: OU 過程の離散時間版 **AR(1) を MLE 推定**して半減期を計算（履歴不足はヒューリスティックにフォールバック）
3. **期待総リターン**: `gap_ratio + 配当利回り`（旧「総合リターン予測」§1 を吸収）

旧 total_return の独自 OLS は廃止し、理論株価は業種別OLS の seam から得る（OLSエンジン1本化・ADR-0001）。

### 乖離率（業種別OLSで計算済み）

```
gap = gap_ratio  [%]  （sector_ols.py が regression_results.gap_ratio に保存。読取は financial_metrics VIEW 経由）
```

### 期待総リターンと implied 倍率（旧 total_return 吸収）

`gap_ratio`[%] と配当利回り[%] は同次元なので加算できる。理論株価は `gap_ratio` から復元する:

```
予測株価   = 実株価 × (1 + gap_ratio / 100)            [円/株]
期待総リターン = gap_ratio + 配当利回り                  [%]   （sort=total_return でこの順に表示）
implied PER = 予測株価 ÷ EPS,   implied PBR = 予測株価 ÷ BPS
```

- 配当利回りは VIEW 由来。異常値ガードとして 30% 超は 0 とみなす。
- `min_div_yield`（%）で最低配当利回りフィルタ（0=フィルタなし）。
- プール回帰の β 係数（市場全体の implied 倍率）は失うが、業種別係数（§2）で代替する。

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
| `rd_intensity` | 研究開発集約度 = `pl_rd_expenses / pl_revenue` [%]（C2列の結線・VIEW算出） |
| `da_intensity` | 減価償却集約度 = `pl_depreciation / pl_revenue` [%]（C2列の結線・VIEW算出） |
| `z_op_margin` | 営業利益率 Zスコア（年度別正規化済み） |
| `z_roe` | ROE Zスコア |
| `z_cf_ratio` | 営業CF/売上比 Zスコア |
| `gap_ratio` | 業種別OLS乖離率 [%] |

> **C2 結線（無次元 intensity）**: 研究開発費・減価償却費（per-share 絶対額では対数リターンと次元不整合）を**売上で正規化した集約度 [%]** として投入。`financial_metrics` VIEW が `op_margin` と同じ流儀で算出し、分子は非 COALESCE で null 伝播（R&D/D&A 未開示企業は intensity も null → サンプルから自動除外）。デフォルト財務特徴量（`per`/`pbr`/`roe`）には含めず選択肢として提供。

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

**実装ファイル**: `backtest.py`（ロジック）＋ `routers/analysis.py` の `/api/backtest`・`/api/backtest/multi` エンドポイント

### 概要

過去 N ヶ月前の時点で確定していた財務データを使いスコアリングし、その後の実際の株価リターンを計算する。モデルの有効性を事後的に検証するために使用する。マルチピリオド比較（3/6/12/18/24 ヶ月）により保有期間と有効性の関係も分析できる。

**メタ層の一般化（scoring source）**: 検証対象のスコアリング手法を `source` パラメータで切り替える（`SCORING_SOURCES`）。ランキングを出す一次分析なら同一土俵（as-of スコア→上位N社→実現リターン→ベンチマーク超過）で比較できる。

| source | スコア（高いほど上位 N 社へ） | 有効性の判定 | 前提 |
|---|---|---|---|
| `recommend`（既定） | recommend プリセットの加重和（z_roe 等） | 超過収益 > 0 | — |
| `valuation` | 期待総リターン ＝ `gap_ratio` ＋ 配当利回り [%] | 超過収益 > 0 | sector_ols 実行済み年度のみ（gap_ratio 必須） |
| `net_cash` | 清原式ネットキャッシュ比率 ＝ (流動資産＋投資有価証券×0.7−総負債) / 時価総額 | 超過収益 > 0 | — |
| `sell` | 売り候補 ＝ recommend 加重和の符号反転（買い系の逆観点） | **超過収益 < 0**（上位＝売り候補が下回るほど有効） | — |

ML 系（price_predictor / macro）は WF-CV を内蔵するため対象外（→ §4・§9）。`preset` は `recommend` / `sell` のときのみ意味を持つ。`sell` はメタ層×双対層（売り判断の有効性検証）にあたり、上位 N 社＝最も売り向きの銘柄なので、その後リターンがベンチマークを**下回る**ほど売りシグナルが有効と読む。

### 計算ロジック

```
start_date = today − months_ago × 30日

1. start_date 以前に period_end が確定しているレコードで
   各社の最新年度のデータを取得

2. source のスコア関数（score_record）で全社をランキング
   （recommend=加重和 / valuation=gap+配当 / net_cash=NC比率）

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

## 9. マクロ×リスク-リターン推奨

**プラグイン**: `plugins/macro_risk_return.py` / `MacroRiskReturnPlugin`  
**カテゴリ**: ③ 将来リターンを予測（`ui_order=330`、`heavy=True`）  
**副読本**: 予備知識ゼロ向けの噛み砕いた解説は [`M1_MACRO_MODEL_GUIDE.md`](M1_MACRO_MODEL_GUIDE.md)（本節はその正式・技術版）

### 9.1 概要

マクロ要因（為替・金利・株式市況）と企業固有の財務指標の**交差項 OLS** で1年先リターン μ を推定し、各銘柄をリスク-リターン平面に配置して**効率的フロンティア**（Pareto 最優解）を提示するモデル。

### 9.2 説明変数

| 種別 | 特徴量（選択肢） | 既定 | 変換 |
|---|---|---|---|
| 財務（価格由来＝バリュー） | per, pbr, **div_yield** | per, pbr | 無次元（FinancialMetric VIEW） |
| 財務（価格フリー） | roe, **roa**, **op_margin**, **net_margin**, **asset_turnover**, equity_ratio, **de_ratio**, **nc_ratio**, **cf_ratio**, **eps_growth**, **op_growth**, **rev_growth**, rd_intensity, da_intensity, z_op_margin, z_roe, z_cf_ratio | roe, **roa**, equity_ratio, **eps_growth** | 無次元（FinancialMetric VIEW 既存列。**asset_turnover は本改修で VIEW 追加**） |
| モメンタム | 12-1ヶ月ログリターン | （use_momentum 時・**既定 OFF**） | log(P_short / P_long) |
| マクロ | USDJPY/SP500/NIKKEI225 = YoY 変化率、US10Y = 5年 Z スコア | USDJPY, SP500, US10Y（use_macro 時） | YoY = Δ/前年 / Z = (現在−5年平均)/5年SD |
| 交差項 | 選択財務 × 選択マクロ | （use_macro 時） | 積（無次元×無次元） |

被説明変数は **1年先（52週先）週次ログリターン（年率・無次元）**。全特徴量は学習前に `winsorize(p1–p99)`→z-score 標準化を適用。

> **PER/PBR は「循環参照」ではない（重要）**: 目的変数は株価水準ではなく**将来リターン**であるため、現在の PER/PBR で将来リターンを予測するのは正統な**バリュー・ファクター**（Fama-French HML ≒ book-to-market = 1/PBR）。`per×eps=price` の恒等式が問題になるのは「現在株価水準」を当てる場合だけで、本モデルには当てはまらない（**他のプラグイン sector_ols / price_predictor の per-share→株価 Ohlson 型（§本書 該当節）とは目的変数が異なる**）。ただし PER/PBR は分子に同じ株価 P_t を共有し「割安」と「価格の平均回帰」を分離しきれないため、価格を含まないファンダ（roa/eps_growth 等）を既定に併置して補強する。**収益性の質を分解するデュポン因子（net_margin × asset_turnover ≈ roa）・成長（rev_growth）・財務健全性（nc_ratio）も価格フリーの選択肢として提供**する（既定外・任意採用）。div_yield は配当という株価由来のバリュー因子で per/pbr と同枠（循環ではない）。

> **特徴量・マクロの選択 UI**: 財務特徴量（`fin_features` multiselect）とマクロ特徴量（`macro_features` multiselect）は `/analysis` の M-1 タブで選べる。`use_macro`（マスタ ON/OFF）が OFF のときはマクロ・交差項を生成しない。**モメンタムは `use_macro` から独立した `use_momentum`（既定 OFF）で制御する**（§9.4・§9.8：マクロを使いつつモメンタムの過去履歴要件を外して walk-forward CV を成立させるため）。選択肢は **FX・株式・米金利/期間・コモディティ・ボラの5チャネル / 11系列**（#218 フェーズ1）：USD/JPY・EUR/JPY・ドル指数(DXY)・S&P500・米5/10/30年金利・日経225・VIX・WTI・金。既定選択は USD/JPY・S&P500・米10年金利の3本のみで、その他は多重共線（VIX↔SP500・米金利↔DXY 等）や任意性のため既定では未選択（任意。pooled BIC が過剰選択を抑える）。VIX/DXY/US5Y/US30Y は `collect-macro.yml` の Actions 実行で macro_data への蓄積（各1255〜1257件/5年）を実証してから公開した。**TOPIX・JP10Y は本番 macro_data に蓄積がない（収集失敗：JP10Y=^JGB 上場廃止 / TOPIX=^tpx・^TPX 取得不可）ため選択肢から除外**（選ぶと全サンプルが None スキップで学習不能になる。収集が直り次第 `_MACRO_MAP` へ追加すれば自動で選択肢に出る）。

### 9.3 特徴量選択（LASSO-LARS / BIC）

`sklearn.linear_model.LassoLarsIC(criterion="bic")` で **LARS パスを1パス計算し、BIC 最小点**を選ぶ（全候補を winsorize→zscore 標準化してから fit）。L1 正則化が共線性をネイティブに処理するため、旧実装の VIF 門番（`check_collinearity` を各候補×各ステップで呼ぶ貪欲前進選択）は不要となり廃止した。BIC 最小解が `max_features` を超える場合は **|係数| 降順の上位 `max_features`** に切り詰める（パラメータ「BIC 最大採用特徴量数」に忠実）。**選択は LASSO だが最終係数は選択済み特徴量で OLS 再フィット**して不偏化する（LASSO は選択専用）。

> **設計判断（2026-06-19）**: 旧「貪欲前進BIC＋VIF」は 36,000 行規模で OLS を約1.2万回呼び、`use_macro=true` 既定で分単位を要した。LassoLarsIC への置換で特徴量選択は秒未満に短縮（実測：選択フェーズ 0.7s）。非劣位チェック（`use_macro=false` 構成）で旧/新の walk-forward CV mean R² は同値（0.0122）を確認済み。詳細は §9.10。

### 9.4 Walk-forward CV

既存の `walk_forward_cv_monthly`（`plugins/utils.py`）で月次ロールウィンドウ CV を実施。時系列順を厳守（通常の k-fold はルックアヘッドバイアスが生じるため不可）。各フォールドの RMSE・MAE・R² を記録。**学習サンプルが要求する履歴長は特徴量構成で決まる**：52週先リターン（未来）は常に必要だが、**12ヶ月モメンタムは `use_momentum=ON` のときだけ過去履歴を要求する**。`use_momentum=OFF`（既定）なら `use_macro=ON` のままでも過去履歴要件が外れ、週次株価が浅くてもフォールドが確保できる（§9.8）。

### 9.5 リスク指標

| 指標 | 定義 | 役割 | 解像度 |
|---|---|---|---|
| **R2** 実現ボラティリティ | 直前52週の週次ログリターン SD × √52 | 価格変動リスク（Sharpe 分母）。**効用軸（既定）** | 個社 |
| **R_macro** マクロ起因リスク | $\sqrt{\beta^\top \Sigma_{\text{macro}} \beta}$（$\beta$=per-stock 事後ローディング、$\Sigma_{\text{macro}}$=選択因子の共分散） | マクロ要因が銘柄に与えるリターン単位のリスク。**効用軸（R2 と選択制）** | 個社（macro_beta 推論要） |
| **R1** 予測不確実性 | OLS 予測分散 $s^2(1 + x^\top (X^\top X)^{-1} x)$ の平方根（`se_obs`） | イン・サンプルのレバレッジ。縮小駆動に降格（効用軸からは除外） | 個社 |
| **R3** モデル信頼性 | セクター×サイズ三分位バケットごとの walk-forward CV 残差 RMSE | アウト・オブ・サンプルのグループ誤差。**表示/足切りゲート**に降格（低信頼銘柄を上位表示から除外） | バケット |

**効用軸（`risk_axis`）** は R2（実現ボラ）と R_macro（マクロ起因リスク）の選択制。両者ともリターン単位のため λ の次元整合 $U = \mu - \lambda R$ が保たれる。R1 は縮小駆動専用・R3 は足切りゲート（`r3_gate` スライダー）に降格し、効用軸の選択肢から除外されている。λ は 0〜5（既定 1.0）。

**R3 の算出**: 9.4 の walk-forward CV のテスト残差を、各サンプルの (セクター, サイズ三分位) で層別し、バケットごとに $\text{RMSE}=\sqrt{\overline{e^2}}$ を計算する。サイズ代理は**総資産**（`bs_total_assets`。本番で確実に充足するコア BS 項目。`issued_shares` は C2 新列で本番 NULL のため不可）で、分位点は単調変換に不変なので生値の三分位を用いる。閾値は残差を持つサンプルの母集団から決め、現企業へも同閾値を適用。バケットの残差数が下限（5件）未満なら **セクター → 全体** の順にフォールバックする。

**R_macro の算出**: `plugins/utils.py::macro_risk_exposure(beta, cov)` が担う（`√(βᵀΣβ)`）。$\beta$ は `macro_beta` テーブルに蓄積された per-stock 事後ローディング（#214 推論バッチ）、$\Sigma_{\text{macro}}$ はメタに記録された選択因子の共分散行列。macro_beta 未蓄積なら None を返し、クライアントは `r_macro` 軸選択時に null 銘柄をフィルタして graceful degrade する。

### 9.6 James-Stein 縮小

予測リターン μ_raw をセクター平均 μ_sector へ縮小（Black-Litterman 型）:

$$\mu_{\text{shrunk}} = (1 - w) \cdot \mu_{\text{raw}} + w \cdot \mu_{\text{sector}}, \quad w = R1 / R1_{\max}$$

R1 が大きい（信頼度が低い）ほど強くセクター平均に引き寄せる。

**低シグナル時の縮退（重要）**: R1 = √(s²(1+leverage)) の leverage は全社ほぼ同値（centroid 近傍）のため、現状の本番データでは R1 がほぼ定数となり **w = R1/R1_max ≈ 1（全社）** になる。結果、μ_shrunk は事実上**全社がセクター平均へ潰れ**、銘柄差が消える。これは縮小式の欠陥ではなく、**モデルの説明力が低い（CV R² ≈ 0.01〜・§9.8 の被覆制約に起因）ことの正直な反映**である。仮に縮小式を正規の Black-Litterman（$w = se^2/(se^2+\tau^2)$）へ直しても、予測誤差 se が銘柄間シグナル分散 τ を桁違いに上回るため w≈1 のままで、縮小では分散を取り戻せない。**根本回復には週次株価バックフィル（§9.8・FUTURE_TASKS DF-3）が必要**。このためバブルチャート／効用 U の期待リターン基準には μ_shrunk ではなく **μ_raw を用いる**（§9.7）。μ_shrunk はランキング表の参考列に残す。

### 9.7 Pareto フロンティア と 効用関数（クライアント側後処理）

$$U = \mu_{\text{raw}} - \lambda \cdot R_{\text{axis}}$$

λ はリスク回避度（スライダー、0〜5、既定 1.0）。$R_{\text{axis}}$ は `risk_axis` で選んだリスク（R2 既定 / R_macro）。期待リターンは **μ_raw**（OLS の生予測値。セクター収縮は低シグナル時に銘柄差を消すため廃止）。

**R3 足切りゲート（`r3_gate`）**: CV-RMSE がスライダー値を超える銘柄を上位表示から除外（0=ゲートなし）。低信頼銘柄（モデルがその企業タイプを苦手とするバケット）を推奨集合から取り除くための信頼度 machinery。#217 SELL ランキングにも R3 ゲートを action-label 段で実装（低信頼保有を SELL から除外）。

**効用 U・Pareto 判定・並べ替え・`top_n` 抽出・R3 ゲートは λ／リスク軸にのみ依存する後処理であり、モデル再学習に一切関与しない**。そのためサーバー（`_score_companies`）は**全社の raw 値**（`mu_raw / r1 / r2 / r3 / r_macro`）を返し、これら後処理は**クライアント側（`static/js/analysis.js`）で算出**する。結果として λ 調整・リスク軸切替・表示件数変更・R3 ゲート変更は**再計算なし（再API なし）で即時反映**される（重い計算が走るのは特徴量・マクロ・`max_features` 等モデル本体のパラメータを変えた時のみ）。Pareto 判定軸は表示中の `risk_axis` に追従する。

**選択特徴量の係数可視化（解釈性）**: `execute` は最終 OLS の**標準化係数**を `feature_coefs`（`selected_name → β`）で返す（X・y とも z-score 正規化済のため特徴量間で大小比較可能）。UI（CV パネル）は係数を **|β| 降順の横バー**で表示し、**財務／マクロ／交差項／テクニカルを色分け**、ゼロ中心で符号（正＝株高方向 / 負＝株安方向）を示す。これにより「どの因子・交差項がどの向きに効いたか」を提示する。**CV R² は低い（§9.6・§9.8）ため、係数は符号と相対的大小の目安であり、寄与度の過大解釈は避ける**旨を UI に明記。

**可視化マッピング**（バブルチャート）: **散布図は全社を描画し、効用上位 `top_n` 社を大きく濃く強調**する（残りは小さく淡く）。**y=μ_raw / x=選択リスク軸（R2/R_macro・クライアント即時切替） / 色の濃淡=効用 U / 枠線強調＋フロンティア線=Pareto 優位**。Pareto は（R3 ゲート後の）全社で算出する。**「効用で絞った上位 N だけを描くとリスク方向に潰れて効率的フロンティアが見えない**（λ>0 では低リスク銘柄ばかり選ばれるため）ので、母集団を描く設計とする。**両軸は描画対象の [p1, p99] に固定**し、データ過少銘柄の過大ボラ（例: R2≈19）・過大μ の外れ値で軸が引き伸ばされ全点が隅へ潰れるのを防ぐ（範囲外の <2% は非描画）。

### 9.8 制約・前提

1. 週次株価履歴（`stock_price_weekly.close_last`）が少なくとも1年分（≥52週）必要
2. マクロデータ（`macro_data`）の YoY 用に約400日、Z スコア用に5年分の蓄積が必要（未蓄積は None でスキップ）
3. 学習サンプル数（企業数 × 月数）が 20 件未満の場合はプラグインが空結果を返す
4. **被覆制約はモメンタム由来（`use_momentum=true` のとき）**: 「52週先リターン（未来必要）」かつ「12ヶ月モメンタム（過去必要）」を同時に要求すると、週次株価が約2年分（本番現状 2024-05〜）しかない環境では両条件を満たす月が**約1ヶ月の薄い帯**に収縮し、walk-forward CV が 0 フォルド（`mean_r2=None`）になる。**本改修でモメンタムを `use_macro` から切り離し `use_momentum`（既定 OFF）化**したため、**既定構成（`use_macro=ON` / `use_momentum=OFF`）ではマクロ・交差項を使ったまま CV が複数フォルドで成立する**（モメンタムの過去履歴要件が外れるため）。`use_momentum=ON` で12ヶ月モメンタムを使う場合は引き続き上記の薄帯制約が生じ、CV 品質の回復には**週次株価のバックフィル**で履歴を延ばす必要がある（#198）。バックフィルは `backfill_weekly_history_yahoo`（`collector_prices.py`）を実装済み＝`python _pipeline_gh.py --backfill-weekly --backfill-weekly-years 5`、または GitHub Actions の「[一回性] 週次株価バックフィル」ワークフローで本番実行する（要本番収集権限）。週次の最古日が `today-years` より新しい社だけを Yahoo から過去方向に取得し、`record_prices_batch` 経由で daily→weekly 再集約する（1社ごとに daily を trim するため Supabase 500MB を超えない）。実行後の検証: `SELECT min(trade_date) FROM stock_price_weekly` が `today-years` 近傍まで遡り、`use_momentum=true` で `cv_metrics.n_folds >= 2`。

### 9.9 参考文献

- **Markowitz, H. (1952)**. "Portfolio Selection." *Journal of Finance*, 7(1), 77–91. → https://doi.org/10.2307/2975974
- **Efron, B., Hastie, T., Johnstone, I., & Tibshirani, R. (2004)**. "Least Angle Regression." *Annals of Statistics*, 32(2), 407–499. → https://doi.org/10.1214/009053604000000067
- **Zou, H., Hastie, T., & Tibshirani, R. (2007)**. "On the 'degrees of freedom' of the lasso." *Annals of Statistics*, 35(5), 2173–2192. → https://doi.org/10.1214/009053607000000127
- **Fama, E.F. & French, K.R. (1993)**. "Common risk factors in the returns on stocks and bonds." *Journal of Financial Economics*, 33(1), 3–56. → https://doi.org/10.1016/0304-405X(93)90023-5
- **Chen, N., Roll, R., & Ross, S.A. (1986)**. "Economic Forces and the Stock Market." *Journal of Business*, 59(3), 383–403. → https://doi.org/10.1086/296344
- **Black, F. & Litterman, R. (1992)**. "Global Portfolio Optimization." *Financial Analysts Journal*, 48(5), 28–43. → https://doi.org/10.2469/faj.v48.n5.28
- **Frazzini, A. & Pedersen, L.H. (2014)**. "Betting Against Beta." *Journal of Financial Economics*, 111(1), 1–23. → https://doi.org/10.1016/j.jfineco.2013.10.005
- **Jegadeesh, N. & Titman, S. (1993)**. "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency." *Journal of Finance*, 48(1), 65–91. → https://doi.org/10.1111/j.1540-6261.1993.tb04702.x

### 9.10 性能（2026-06-19 改修）

| 構成 | 旧 | 新 | 備考 |
|---|---|---|---|
| `use_macro=true`（既定・fin4・max20） | 約219s | **約29s** | 主因は特徴量選択ではなく `_build_snapshots` のマクロ計算（企業×月の全日付再走査）。マクロ特徴量は snap_date のみに依存するため**日付メモ化**で 207.9s→12.7s。 |
| 特徴量選択フェーズ単体 | 約1.2万回 OLS | **0.7s** | LassoLarsIC の1パス LARS（§9.3）。 |
| `use_macro=false`（非劣位検証） | mean R²=0.0122 | mean R²=0.0122 | 旧/新で同値。選択法置換による CV 品質劣化なし。 |

---

## 10. 売り候補ランキング（保有銘柄の売り時）

`plugins/sell_ranking.py`

### 10.1 概要

買い系モデル（§1 総合リターン・§2/§3 割安スクリーニング・§6 おすすめ銘柄）が**全銘柄ユニバースから「買い」を探す**のに対し、本モデルは**ユーザーが入力した保有銘柄リスト**の中から「売るべき銘柄と売り時」をランキングする。観点は買い系の「逆」：

- **① 割高度**: 回帰乖離 `gap_ratio`（§2 で算出。正＝割安・負＝割高）が負＝割高なほど売り
- **② 業績悪化**: ROE・営業利益率・CF余力・売上成長率・財務安全性が低いほど売り
- **③ ネットキャッシュ余力の毀損**: 清原式ネットキャッシュ比率 `nc_ratio`（§8）が低い＝安全マージン消失なほど売り（買い系 §8 の逆観点）。VIEW 列ではなく実行時計算。
- **④ 価格モメンタム（タイミング）**: 週次株価の下落トレンドを「売り時」シグナルとして別軸で評価

保有銘柄はサーバに保存しない（都度入力＋ブラウザ localStorage 記憶）。購入単価は損益（PnL）表示のみで、スコアには使わない。

### 10.2 スコア定式化（スケール整合）

各シグナルはいずれも「高いほど良い（売る理由が小さい）」指標である。％指標（`gap_ratio` / `rev_growth`）と無次元の比率（`roe` / `op_margin` / `nc_ratio` 等）が混在するため、**CLAUDE.md「次元整合性」に従い、最新年度ユニバース全体で各指標を `winsorize`（p1–p99）→ z 標準化**してからスコアを合成する（§5 の横断的 Z 化と同型。買い系 §6 が VIEW の `z_*` 列を使うのに対し、本モデルは生の比率列を保有判定用にその場で標準化する）。`nc_ratio` は VIEW 列に無いため `_resolve_metric` が清原式（流動資産＋投資有価証券×0.7−総負債）÷時価総額で実行時計算する。

$$
\text{売りスコア} = \frac{\sum_i w_i \cdot (-z_i)}{\sum_i w_i}, \qquad w_i \ge 0
$$

ここで $z_i$ は指標 $i$ のユニバース標準化値（±5 にクリップ）、$w_i$ は「その観点を売り判断でどれだけ重視するか」を表す**非負ウェイト**。符号反転 $(-z_i)$ により、ユニバース平均より劣る（割高・低収益・低成長）銘柄ほどスコアが正に大きくなる。ユニバース平均並みの銘柄は ≈0。値が揃う重み付き指標の比率（カバレッジ）が下限を下回る銘柄は「データ不足」とする。

プリセット（`バランス型` / `割高警戒型` / `業績悪化重視`）は $w_i$ の既定値、UI でスライダー上書き可。

### 10.3 価格モメンタム（タイミング軸）

`stock_price_weekly.close_last` から各保有銘柄について算出：

- **13週リターン** $= P_t / P_{t-13} - 1$（週次データ < 8 週なら算出せず `不明`）
- **52週高値からの下落** $= P_t / \max(P_{t-51..t}) - 1$
- **トレンド分類**: 13週リターン ≤ −10% → `下落`、≥ +10% → `上昇`、その間 → `横ばい`

### 10.4 アクションラベル

売りスコアに絶対閾値を適用し、トレンドで補正する：

1. スコア ≥ `sell_threshold`（既定 0.8）→ **SELL**、≥ `reduce_threshold`（既定 0.3）→ **REDUCE**、未満 → **HOLD**
2. タイミング補正（既定 ON）: `下落` トレンドは 1 段引き上げ（HOLD→REDUCE→SELL）、`上昇` トレンドは SELL を REDUCE へ緩和（上昇中の即売り回避）

相対ランキング（売りスコア降順）と絶対ラベルを併用するため、優良な保有のみのポートフォリオでは全銘柄が HOLD になり「売るべきものは無い」を表現できる。

### 10.5 制約・前提

1. 割高度（`gap_ratio`）には §2 業種別OLS（`regression_results`）の事前実行が必要（`depends_on=["sector_ols"]`）。未実行なら runner が 400 を返す
2. 保有コードは証券コード4桁。DB 未収録（ETF・外国株・未上場）は `not_found` に集約し判定対象外
3. 価格モメンタムは週次株価履歴の蓄積に依存（不足時は `不明` で補正なし）
4. 投資助言ではなく、保有整理の参考スコアにすぎない

### 10.6 参考文献

- **Jegadeesh, N. & Titman, S. (1993)**. "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency." *Journal of Finance*, 48(1), 65–91. → https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- **Ohlson, J.A. (1995)**. "Earnings, Book Values, and Dividends in Equity Valuation." *Contemporary Accounting Research*, 11(2), 661–687. → https://doi.org/10.1111/j.1911-3846.1995.tb00461.x

---

## 改訂履歴

| 日付 | 内容 |
|---|---|
| 2026-05-14 | 初版作成（モデル 1–7 を記述）|
| 2026-05-21 | モデル 8（ネットキャッシュ分析・清原達郎式）を追加 |
| 2026-06-01 | モデル 8 に Graham NCAV 指標・NCAV比率（2/3ルール）を併設。一律の時価総額下限を廃しデータ品質ガード（NC比率サニティ上限）に置換。営業CF>0/純利益>0 のバリュートラップ除外を追加 |
| 2026-06-17 | モデル 9（マクロ×リスク-リターン推奨）を追加。交差項OLS + 前進BIC + Walk-forward CV + James-Stein縮小 + Paretoフロンティア |
| 2026-06-18 | モデル 9 に R3 リスク指標（セクター×サイズ別バケットの walk-forward CV 残差 RMSE）を追加。横軸リスクで R1/R2/R3 を切替し、散布図・効用・Pareto 判定を選択軸に整合 |
| 2026-06-19 | モデル 9 を性能・可視化リファクタ。①特徴量選択を貪欲前進BIC＋VIF→`LassoLarsIC(bic)` に置換（VIF廃止・最終OLS再フィット）。②`_build_snapshots` のマクロ計算を日付メモ化（既定構成 219s→29s）。③効用U・Pareto・並べ替え・top_n をクライアント側後処理へ移譲（サーバーは全社rawを返却、λ・軸切替が即時）。④可視化を 色=効用U / 径=R1 / 枠線＋線=Pareto の単一バブルチャートへ再マッピング。JP10Y 記載を実装（未使用）に合わせ削除 |
| 2026-06-20 | バブルチャート目視で μ_shrunk が全社セクター平均へ潰れる（w=R1/R1_max≈1）と判明。期待リターン基準を **μ_shrunk→μ_raw** へ変更（効用U・Pareto・チャートY軸・ランキング主列）。μ_shrunk は表の参考列に降格。根因の説明（低シグナル＝CV R²≈0.01）を §9.6 に追記（根本回復は DF-3 週次株価バックフィル） |
| 2026-06-20 | バブルチャートが依然 X 軸で潰れる件を是正。原因は「効用 U 上位 N のみ描画」で λ>0 だと低リスク銘柄ばかり集まる構造。散布図を **全社描画＋効用上位 N 強調**へ変更し、**両軸を [p1,p99] に固定**（外れ値で軸が伸び全点が隅へ潰れるのを防止）。p99 クランプの少数点バグ（floor(n·0.99)=max）も汎用パーセンタイル関数へ置換して解消 |
| 2026-06-20 | **X 軸潰れの真因を特定・根治**: frontier の line データセットにより Chart.js が x 軸を既定で **category スケール**化し、数値 min/max・クランプを無視していた（y は既定 linear で正常だったため「Y は効くのに X だけ潰れる」非対称が発生）。**x/y に `type:'linear'` を明示**して数値軸を強制。あわせて雲を可視化（径は固定＝R1 がほぼ一定で径エンコードが退化していたため廃止・R1 はツールチップへ）。静的アセットのブラウザキャッシュで JS 更新が反映されない事故も是正（`api.py` 静的配信に `Cache-Control: no-cache`・テンプレ script に版クエリ） |
| 2026-06-20 | **特徴量の正当性強化＋マクロ可視化＋係数表示**（ゴール=予測力ではなく解釈性）。①目的変数は将来リターンのため PER/PBR は循環でなくバリュー因子と整理（§9.2 注記）。価格を含まないファンダ（roa/cf_ratio/de_ratio/eps_growth/op_growth）を選択肢に追加し、既定に roa・eps_growth を注入（全て FinancialMetric VIEW 既存列＝DB 移行ゼロ）。②マクロを `macro_features` multiselect 化（USDJPY/SP500/US10Y＋NIKKEI225、既定3。TOPIX は本番データなし＝収集失敗のため JP10Y 同様に除外）。③`execute` が標準化係数 `feature_coefs` を返し、UI が種別色分けの係数バーで表示（§9.7） |
| 2026-06-20 | **モメンタム独立化（CV 制約の緩和）＋価格フリー特徴量の拡充**。①モメンタムを `use_macro` 連動から切り離し独立パラメータ `use_momentum`（既定 OFF）化。既定構成（`use_macro=ON`/`use_momentum=OFF`）で過去履歴要件が外れ walk-forward CV が複数フォルドで成立（§9.4・§9.8。従来は use_macro=true で 0 フォルド）。②財務特徴量に div_yield（バリュー）・op_margin/net_margin/asset_turnover（デュポン分解）・rev_growth（成長）・nc_ratio（健全性）を追加。asset_turnover のみ `financial_metrics` VIEW に新規列追加、他は既存列 |
| 2026-06-20 | モデル 10（売り候補ランキング・保有銘柄の売り時）を追加。買い系の逆観点（割高度 gap_ratio 反転・業績悪化・価格モメンタム）をユニバース標準化で合成し、相対ランキング＋SELL/REDUCE/HOLD 絶対ラベル（タイミング補正付き）を付与。保有はサーバ非保存（都度入力＋localStorage）、購入単価は損益表示のみ |
| 2026-06-22 | M-1 tidy (#220)。①特徴量選択関数を `_forward_bic`→`_select_macro_features` へ改名（実体は LassoLarsIC ベース、貪欲前進 BIC の名残を一掃）。②未使用引数 `vif_threshold` を削除。③セクターダミー×マクロ交差項を廃止（fin×macro のみに簡素化）。④μ_shrunk（セクター平均収縮）を廃止し μ_raw を唯一の期待リターン指標に統一。各ドキュメント・JS・テストを整合 |
| 2026-06-23 | リスク軸再編（#215）。①`risk_axis` を r2/r_macro に再編（R1/R3 を効用軸から除外）。②R_macro（√(βᵀΣ_macroβ)・リターン単位）を全社 raw 値に追加（macro_beta 未蓄積なら None・graceful degrade）。③R3 を表示/足切りゲート（`r3_gate` スライダー・0=ゲートなし）に降格。④λ レンジを 0〜5 に拡張（次元整合の確保）。クライアント側後処理・ランキング表列・tooltip も整合 |
