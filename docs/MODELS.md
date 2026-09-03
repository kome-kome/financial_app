# 分析モデル解説

> アプリ内の全分析モデルについて、数式・実装・仮定・限界・参考文献をまとめたドキュメントです。
> 参照元論文のURLは「参考文献」欄に記載しています。
> **初心者向けの噛み砕いた解説は `/guide`（`templates/guide.html`）** にあります。本ドキュメント（および `models.html`）は数式・論文中心の技術版です。

---

## 目次

1. [総合リターン予測 → バリュエーション分析へ統合（§3参照）](#1-総合リターン予測--バリュエーション分析へ統合3参照)
2. [業種別OLS回帰](#2-業種別ols回帰)
3. [バリュエーション分析（割安度＋平均回帰＋期待総リターン）](#3-バリュエーション分析割安度平均回帰期待総リターン)
4. [株価リターン予測 → 削除（M-1 §9 へ集約）](#4-株価リターン予測--削除m-1-9-へ集約)
5. [横断的Zスコア正規化](#5-横断的zスコア正規化)
6. [Zスコア重み付けスコアリング（おすすめ銘柄）](#6-zスコア重み付けスコアリングおすすめ銘柄)
7. [バックテスト](#7-バックテスト)
8. [ネットキャッシュ分析（清原達郎式）](#8-ネットキャッシュ分析清原達郎式)
9. [マクロ×リスク-リターン推奨](#9-マクロリスク-リターン推奨)
10. [売り候補ランキング（保有銘柄の売り時）](#10-売り候補ランキング保有銘柄の売り時)
11. [M-2 マクロ×財務 勾配ブースティング（macro_gbdt）](#11-m-2-マクロ財務-勾配ブースティングmacro_gbdt)
12. [M-3 ベイズ状態空間モデル（時変マクロβ DLM・macro_dlm）](#12-m-3-ベイズ状態空間モデル時変マクロβ-dlmmacro_dlm)
13. [M-4 兄弟μ̂スタッキング・アンサンブル（macro_ensemble）](#13-m-4-兄弟μ̂スタッキングアンサンブルmacro_ensemble) **（退役・ADR-0044）**
14. [M-5 マクロ×財務 ランク学習（learning-to-rank・macro_gbdt_rank）](#14-m-5-マクロ財務-ランク学習learning-to-rankmacro_gbdt_rank) **（退役・ADR-0044）**
15. [兄弟モデル候補メニュー（探索枠・model_candidates）](#15-兄弟モデル候補メニュー探索枠model_candidates)
16. [M-6 マクロ×財務 正則化線形（ElasticNet・macro_enet）](#16-m-6-マクロ財務-正則化線形elasticnetmacro_enet)

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
- **説明変数の自動ドロップ**: 説明変数は「選択列が1つでも NULL の銘柄を AND 除外」する仕様のため、欠損列を重ねると全銘柄が除外され 0 業種に潰れる。これを2段階で防ぐ:
  1. **全業種一括**（`_select_features`）: 母集団での欠損率が `MAX_FEATURE_MISSING_RATE`（50%）超の列を全業種から除外。結果の `dropped_features` で返し、画面に警告表示する
  2. **業種単位**（`_sector_feature_sets`・パラメータ `sector_missing_rate`・既定 30%）: 業種内欠損率が閾値超の列を**その業種の回帰からのみ**除外する。なお `min_samples` に届かない業種は、欠損の多い列から1つずつ貪欲に除外して届くか試す。結果は業種ごとの `sector_stats[].features` / `sector_stats[].dropped_features` と要約 `sector_dropped_features` で返す
- **無配の 0 埋め**（`ZERO_FILL_FEATURES` = `{dps}`・パラメータ `zero_fill_no_dividend`・既定 ON）: XBRL は配当が無い場合そもそもタグを出さないため、**無配（真の0）と未収集が同じ NULL に潰れる**。`dps` の NULL は 0 [円/株] として回帰へ入れる

> **なぜ業種単位の除外が要るか（Issue #434・ADR-0027）**: 銀行業は「売上高 / 売上総利益 / 営業利益」が業種内 100% 欠損（会計上そもそも概念が無い）だが、母集団全体では欠損率 3.8% / 7.7% / 1.4% に過ぎず、全業種一括の 50% 閾値では救えない。結果として AND フィルタが銀行・証券・保険・陸運・電気ガス等を**業種ごと**分析対象外にしていた。業種別に別の β を推定する本モデルでは、採用列も業種別で構造的に整合する（業種横断で同じ列である必要はない）。
>
> 同様に `dps` の NULL は**無配企業＝再投資型／赤字企業に偏る系統的バイアス**だった。本番実測（2026-08-03・`dps` NULL 662社）を同一年度の FY 開示 `statement_disclosure.div_ann` と突合したところ「実績配当 > 0 なのに `dps` が NULL」は 1社（0.2%）のみで、残りは真の無配。2案の併用でカバレッジは 2,882 → 3,619社（+25.6%・母集団 3,874社に対し 74.4% → 93.4%）。実測手順は `scripts/measure_sector_coverage.py`

### 仮定・限界

- 業種分類はJPX上場会社一覧（TSE 33業種）による。分類の粒度が粗いため、同業種内でもビジネスモデルの差異が大きい場合がある
- 株数推計は IFRS/JGAAP 定義差、期中増資、優先株・転換社債存在時に誤差が生じる（FUTURE_TASKS の J-Quants `IssuedShares` 取得で根本解決予定）
- 業種内ドロップにより**業種ごとに説明変数の集合が異なりうる**。同じ `gap_ratio` でも業種によって「何を織り込んだ理論株価との乖離か」が変わるため、業種横断のランキングでは採用列の差を意識する（画面の業種別 R² サマリーに除外列を表示）
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

## 4. 株価リターン予測 → 削除（M-1 §9 へ集約）

**旧実装ファイル**: `plugins/price_predictor.py`（削除）

旧「株価リターン予測」は価格テクニカル特徴量（MA乖離・ボラティリティ・RSI・ATR）＋財務比率を OLS で N 日先（5/20/60日）リターンへ回帰する最古の予測器だった。中核（線形 OLS によるファンダ由来のリターン予測）は **M-1 マクロ×リスク-リターン推奨（§9）** が上位互換で吸収しており、固有のテクニカル特徴量・短期ホライズンはプロジェクトの目的（ファンダメンタル＋マクロ）から外れた「おまけ」であったため、役割が重複する劣化版として削除した（ADR-0005）。リターン予測そのものは比較ファミリー M-1（線形）／M-2（非線形）／M-3（時変）が 52 週ホライズンで担う。

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

### スコア計算式（スケール整合）

VIEW の `z_*`（年度窓の Z）と `gap_ratio`（単位は％で Z ですらない）が混在し、**そのまま線形
結合すると「重み 1.0」の実効的な影響力が列間で最大 73倍違う**（#469 の実測）。CLAUDE.md
「次元整合性」に従い、**その断面の候補集団全体で winsorize（p1–p99）→ z 標準化**してから
合成する（#509・`fit_view_metric_stats` → `standardize_metric`。§10 の売り候補ランキングと
同じ方式）。

```
score_i = Σⱼ∈present (weight_j × z_metric_j_i) / Σⱼ∈present |weight_j|

z_metric_j_i : 企業 i の指標 j の候補集団標準化値（±5 クリップ）
weight_j      : 指標 j の重み（ユーザー設定）
present       : 企業 i において値が NULL でない指標の集合
```

**weighted mean** で計算するため、指標カバレッジが異なる銘柄を公平に比較できる
（旧実装の単純和では値が揃った銘柄が有利だった）。

**標準化の対象は「VIEW 列かどうか」ではなく「同じスコア合成へ入るかどうか」**。`gap_ratio` も
対象に含める一方、`z_momentum` / `mu` は算出側（`compute_momentum_z` / `compute_mu_z`）が
既に期内標準化しているので**通さない**（二重標準化しない）。境界は `VIEW_METRICS`
（`METRICS` − `RUNTIME_METRICS`）が一元的に持つ。**画面に出る指標値（`results[].detail`）は
生値のまま**で、変わったのはスコアの合成単位だけ。

母集団は `latest_year_subq` が返す各社の最新年度で、**会計年度が混ざる**（実測 2025年度 33.5% /
2026年度 66.5%）。これは決算期の違い（12月期・9月期 vs 3月期）であって鮮度の差ではなく、
**年度別に標準化し直さない**（#520 で実測して決着・[ADR-0008](adr/0008-recommend-factor-premia-fama-macbeth.md) の 2026-08-25 追記）。

### カバレッジフィルタ

```
coverage_i = Σⱼ∈present |weight_j| / Σⱼ |weight_j|
```

`min_coverage`（デフォルト 0.5）未満の企業はランキングから除外する。
`min_coverage = 1.0` を指定すれば全指標が揃った企業のみが対象になる。

### デフォルトプリセット

| プリセット | z_roe | z_op_margin | z_revenue | z_cf_ratio | z_equity_ratio | z_eps | gap_ratio | z_de_ratio | z_momentum |
|---|---|---|---|---|---|---|---|---|---|
| バランス型 | 1.0 | 1.0 | 0.8 | 0.8 | 0.5 | — | 0.5 | — | 0.5 |
| 成長重視 | 1.0 | 0.5 | 2.0 | 0.5 | — | — | 0.3 | — | — |
| 割安重視 | 1.0 | 1.0 | — | — | 0.5 | — | 2.0 | — | — |
| 高収益重視 | 2.0 | 2.0 | — | 1.0 | 0.5 | — | — | — | — |

`gap_ratio` は業種別OLS乖離率であり、「割安度」の指標として機能する（正値 = 割安）。

プリセットは**どれも `mu`（μ̂）重みを持たない**（下記「μ̂ の opt-in 結線」）。

この4プリセットは**性能最適ではない**——位置づけと rank-IC の実測値は本節末尾の
「仮定・限界」（本節末尾）を参照（#513）。

`z_momentum` は他指標と異なり `financial_metrics` VIEW の列ではなく、`plugins/recommend.py::
compute_momentum_z()` が実行時に計算する（sell_ranking の `_resolve_metric` と同じ方式）。
12-1モメンタム（直近1ヶ月を除く12ヶ月の log リターン、`plugins/utils.py::
get_momentum_return()`）を候補集団横断で winsorize → Zスコア化する。価格データは週次更新
（`StockPriceWeekly`）で他指標の年度カデンスと異なるため、VIEW の年度別 WINDOW 関数には
乗せず都度計算する。[バックテスト](#7-バックテスト) の as-of 検証では `start_date` 以前の
価格のみを参照するためリークしない。

### μ̂（期待リターン）の opt-in 結線（Issue #423 子4・ADR-0030）

`mu` は M-1〜M-6 が推定する期待リターン μ̂ を買い推奨へ入れるための指標で、`z_momentum` と同じく
VIEW 外の実行時計算（`plugins/recommend.py::compute_mu_z()`）。読み出しは sell_ranking と同じ
producer 契約（ADR-0004 の `read_producer_scores`）を流用する。

- **既定は OFF**。`mu_source` の既定は `None`、上表の4プリセットは `mu` 重みを持たない
  ＝既定経路の結果は従来と同一で、producer への問い合わせも起きない。
- **`mu` に重みを付けるときは `mu_source`（M-1/M-2/M-3/M-4/M-6）を必ず指定する**。未指定なら
  400 で reject する（黙って欠測扱いにすると「重みを付けたのに効いていない」ことが画面から
  分からないため）。producer 未実行のときは μ̂ を外して継続し、`mu_available=false` で明示する。
- μ̂ は週次リターン[小数]で `z_*` と2桁スケールが違うため、**候補集団内で winsorize→Zスコア化**
  してから加重する（母集団は `z_momentum` と同じ「フィルタ適用後の候補集団」）。
- **[バックテスト](#7-バックテスト) は `mu` を含む重みを reject する**。producer スコアは最新
  スナップショット1断面しか持たず `months_ago` 時点の μ̂ を復元できないため。μ̂ 自体の時系列
  評価は [M-1〜M-6 の OOF バックテスト](#9-マクロリスク-リターン推奨) が担う。

買い側 rank-IC は M-6 0.1713 > M-2 0.1419 > M-1 0.1142（ADR-0022）だが、これは **μ̂ 単体の
順位相関**であって合成スコアにおける最適重みではない。既定へ入れるには [ADR-0028](adr/0028-freshness-limits-from-measured-release-lag.md)
の昇格ゲート（増減どちらの向きも補正後 α を通る実測）が要る（**既定 OFF にした決定そのものは
[ADR-0030](adr/0030-buy-side-mu-wiring-default-off.md)**。ゲートの規則が 0028・現在の状態が
0030 で、この文は両方に依存する）。

### 統計的最適化プリセット（Fama-MacBeth 断面回帰）

4プリセットは直感的なヒューリスティックだが、「統計的最適化」プリセットのみ
Fama & MacBeth (1973) の断面回帰でデータ駆動に推定した重みを使う（`recommend_factor_
premia.py`・ローカル専用CLI・Issue #271）。手順:

1. 月末スナップショットごとに横断面 OLS を実行する:
   `return_i,t+52w = Σ_k b_k,t・z_k,i,t + e_i,t`
   （目的変数・母集団・fold は [M-1/M-2/M-3](#9-マクロリスク-リターン推奨) と共有。
   `plugins/macro_snapshots.py::build_snapshots()` を無改修で再利用）。
2. 各因子の時系列平均 `b_k = mean(b_k,t)` をプリセット重みとする。
3. 52週先リターンを毎月ずらして観測するオーバーラップに起因する自己相関を
   Newey-West（HAC）標準誤差で補正し、t統計量とあわせて記録する。

`plugins/utils.py::walk_forward_cv_monthly`（M-1が使う pooled panel OLS＝複数月をプールして
単一の OLS を学習）とは異なり、期間ごとに別々の断面 OLS を行う点が Fama-MacBeth の本質
（設計判断の詳細は ADR-0008）。永続化は `macro_beta_inference.py` と同じ producer/consumer
分離（バッチ→`recommend_factor_premia`テーブル→`plugins.recommend.resolve_weights()`が読む）。
バッチは **ローカル月次バッチ `scripts/run_monthly.py` の `factor_premia` ステップ**
（毎月1日 JST 01:00・タスクスケジューラ・#504）で自動更新する。GHA の cron は #503 の正本反転で
停止した。Fama-MacBeth の推定は月末スナップショットの積み上げなので、1ヶ月で増える新情報は
「期間が1つ増える」ことだけ＝月次で過不足ない。**未算出（テーブル空）ならバランス型へ
graceful degrade** する。

**単位の陳腐化は構造的に止まる（#517・ADR-0039）**。永続化行は推定に使った断面前処理の世代
`preprocess_version` を持ち、`get_dynamic_preset` は `plugins/utils.py::PREPROCESS_VERSION` と
一致しない行を**採らずバランス型へ倒す**。是正前は世代印が無く、#509 のコミット（2026-08-21
15:21 JST）から本日までの間、**旧・生スケールの重み × 新・標準化済みの特徴量**という昇格ゲートが
一度も測っていない組み合わせが本番に出ていた（61期の実測で rank-IC **−0.0881**。再走後は
**+0.0959**、フォールバック先のバランス型でも **+0.0211** で、どちらも壊れた状態より有意に良い）。

**止まるのは「単位」だけで「鮮度」ではない**。世代が同じまま何ヶ月も古いランは、これまでどおり
静かに使われる。データ鮮度は月次バッチが担保し、走らなかったことの検知は `app_settings` の足跡
（`monthly_last_run` / `monthly_last_success`）が担う。2026-08-08 の実測（**#509 の是正前**）で有効期間 61・
有意な因子は `z_revenue`（t=+4.87）のみ、`z_eps` は b=−3.28（t=−1.07）と有意でない係数が最大の
重みを持っていた。この係数爆発は多重共線性そのものではなく**前処理（winsorize）の欠落**が
主因で、是正後の重みは下記のとおり単位が変わる（経緯は ADR-0008 の 2026-08-21 追記）。

**是正前のこのプリセットは rank-IC が負だった**（61期・−0.0807）。`z_eps` の −3.3367 が支配して
いたためで、#509 の是正後は同じ推定手続きで +0.0175 へ落ち着き、rank-IC は **+0.0960** へ反転する
（in-sample・共通期ペアリングの定常ブートストラップ・詳細は ADR-0008）。**4プリセット中で
rank-IC が正なのは是正後のこのプリセットと成長重視だけ**で、割安重視・高収益重視は是正の前後を
問わず負。手動プリセットの重みが是正後のスケールに合っていない件は #513 で扱う。

**これらの値は `python -m scripts.preset_ic_gate` で再現できる**（#529・ADR-0041）。#509 / #517 の
実測はどちらもアドホックなスクリプトで行われていたが、実体を置いて全記録値を差 ≤0.0003 で再現
済み。読むときの注意が2つある: **並ぶ p=0.001 はブートストラップの下限**（`2/(n_boot+1)`）で
それ以上小さい p を意味しない、**割安重視の rank-IC は重みの 44.4%（`gap_ratio` 2.0）を測って
いない**——パネルが `gap_ratio` を持たない（下記 Decision 1）ため。実体側はこの比率を表に出し、
既定では 20% 超の行を判定から外す。

**第1段階は期内 winsorize→標準化してから解く（Issue #509）**。`plugins/utils.py::
fit_feature_columns`（p1-p99 クリップ＋zscore・切片列付き）を通すので、**係数の単位は
「1sd あたり」**。是正前は VIEW の `z_*` を生スケールのまま解いており、共通分母 `pl_revenue`
がゼロ近傍の1社に支配された列が巨大な係数を生んでいた（`z_eps` = −3.34・p=0.29 が唯一有意な
`z_revenue` の 239倍）。#469 の実測では **winsorize+標準化した素の OLS が Ridge とほぼ同じ答え**
を出す（`z_eps` −3.3367 → +0.0175）＝効いていたのは L2 ではなく前処理だった。

**縮小推定との比較手段（Issue #469）**: `python recommend_factor_premia.py --estimator ridge`
で第1段階だけを RidgeCV（L2）へ差し替えた推定を表示できる（第2段階の Newey-West 平均は
共有）。前処理は両者共通なので**係数はそのまま並べて比較できる**（差は L2 の有無だけ）。
ridge は `--persist` と併用できず（DB 接続前に終了する）、既定を入れ替えるには ADR-0028 の
昇格ゲートが要る。CLI は共線性診断として期別の設計行列条件数（median / max）も出す。

**`gap_ratio` は回帰対象から除外**する。`gap_ratio`（sector_ols依存）の非NULL率は本番DBで
2020〜2024年度=0%・2025年度以降=67%超と極端に偏っており、含めると有効期間が直近2ヶ月分
しか残らず統計的に無意味になるため（実データ検証で判明・ADR-0008）。「統計的最適化」
プリセットは残り7指標＋z_momentumの重みのみを持つ。**`mu` も回帰対象から除外**する
（断面回帰の説明変数は財務・株価由来 factor のみで μ̂ の premium は存在しない。混ざると
`mu_source` 未指定の実行が reject され、プリセットを選んだだけで 400 になる・ADR-0030）。

### 仮定・限界

- **4プリセットは「意味の分かる配分」であって性能最適ではない**（#513）。ウェイト設定に
  数学的・経済学的な根拠はなく直感的で、rank-IC を目的に推定した値ではない
  （「統計的最適化」プリセットのみ Fama-MacBeth 断面回帰によるデータ駆動）。
  61期パネルの実測（#509・[ADR-0008](adr/0008-recommend-factor-premia-fama-macbeth.md)）では
  **rank-IC が正なのは成長重視（+0.0605）とバランス型（+0.0210）だけ**で、割安重視
  （−0.0108）・高収益重視（−0.0108）は負だった。**予測力を求めるなら「統計的最適化」**
  （同じパネルで **+0.0960**）を選ぶこと。手動プリセットが担うのは「なぜこの順位になるか」を
  重みから説明できることで、そこに価値がある。
  ただし**割安重視の rank-IC は重みの 44.4% を測っていない**（`gap_ratio` 2.0 が
  Fama-MacBeth のパネルに無い・Decision 1）ので、この数値だけで「割安重視は効かない」と
  結論しないこと。**検証に `/api/backtest` は使えない**（3/6/12/18/24ヶ月は終端が今日で共通
  ＝独立でなく検定力が無い・ADR-0028）。測るなら `python -m scripts.preset_ic_gate`
  （期別 rank-IC ＋ Bonferroni 補正・[ADR-0041](adr/0041-preset-weight-gate-has-an-implementation.md)）。
  重みの再設計そのものは #513 で扱う。
- 各 Zスコアは年度内の相対評価であり、絶対的な財務水準は反映しない（z_momentum のみ
  価格取得日時点の候補集団内相対評価）
- **VIEW 由来の `z_*` は winsorize を通していない**ため、値そのものは大半が 0 近傍へ潰れている
  （2026-08-21・#469 実測）。`financial_metrics` VIEW は `(x - AVG) / STDDEV_SAMP` を年度窓で
  計算するが、`op_margin` と `cf_ratio` は分母が共に `pl_revenue` で、それがゼロ近傍の会社が
  両列で同じ極端値を取り（実測 `op=-61.6109` / `cf=-61.6108`）`STDDEV_SAMP` を支配する。
  61期・中央値 3,253社のパネルで `z_op_margin` は **99.4%** の会社が \|z\|<0.2
  （正規分布なら 15.9%）・尖度 1562。
  **#509 で消費側を是正済み**——`recommend.execute` / `backtest.score_record` は
  `fit_view_metric_stats` が断面ごとに作り直した (mean, sd) で標準化してから加重する。
  **画面に出る指標値（`results[].detail`）は生値のまま**で、変わったのはスコアの合成単位だけ。
  `z_momentum` / `mu` は算出側で期内標準化済みなので対象外。
- **断面統計を共有させた副作用として、非有限値は入口で落とす**（#516・2026-08-23）。
  `fit_zscore_stats` に NaN / ±inf が1件混ざると `np.percentile` が nan を返し、
  `np.clip(arr, nan, nan)` で全要素 nan → mean も nan → `sd = var ** 0.5 or 1.0` は
  **nan が truthy** なのでフォールバックが働かない。結果 `standardize_metric` が全レコードで
  nan を返し、**1社の破損が断面の全社スコアへ無言で伝播**する（例外もログも出ないまま順位が
  でたらめになる）。是正前は1社の値が壊れてもその1社が nan になるだけだった＝**#509 の是正が
  同時に新しい失敗モードを作っていた**。除外は共有ヘルパ1箇所で行い（呼び出し側5箇所へ散らすと
  次に足した経路が漏れる）、除外件数はログへ出す。
- **momentum を推定側で二重に標準化しない**（#519・2026-08-23）。`build_period_panel` は
  momentum 列だけを先に winsorize→Z 化していたが、#509 で ols 経路も `fit_feature_columns` を
  通すようになったため p1-p99 が二重に掛かり、**この1因子だけ推定時と適用時
  （`compute_momentum_z`）で単位が食い違って**いた。panel 側の個別標準化を外し、8因子とも
  期内前処理へ一様に委ねる。
- **是正の効果は「揃った」であって「正規分布になった」ではない**（2026-08-21 実測）。
  `z_op_margin` は |z|<0.2 が 98.8% → **39.0%**・尖度 2402 → **14.0** で、重み 1.0 の実効影響力
  （IQR比）のばらつきは **100倍 → 7.5倍**へ縮んだが、正規分布の参照値（15.9% / 3.0）には遠い。
  winsorize は **sd を頑健にするだけで変換する値は生値のまま**（クリップ済みの値を Z にすると
  両端が同点になり順位が潰れる）ため、財務比率の重い裾は p1-p99 では落ちない。
  **重みを比較可能にすることが目的で、分布を正規化することが目的ではない。**
- モデルの有効性は [バックテスト](#7-バックテスト) で検証すること

### 参考文献

- ファクター投資の学術的基礎:
  **Fama, E.F. & French, K.R. (1993)**. "Common risk factors in the returns on stocks and bonds." *Journal of Financial Economics*, 33(1), 3–56.
  → https://doi.org/10.1016/0304-405X(93)90023-5
- スマートベータ・ファクタースコアリングの実務:
  **Asness, C., Frazzini, A., Israel, R., & Moskowitz, T. (2015)**. "Fact, Fiction, and Value Investing." *Journal of Portfolio Management*, 42(1), 34–52.
  → https://doi.org/10.3905/jpm.2015.42.1.034
- 12-1モメンタムファクターの学術的基礎:
  **Jegadeesh, N., & Titman, S. (1993)**. "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency." *Journal of Finance*, 48(1), 65–91.
  → https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- 「統計的最適化」プリセットの断面回帰手法:
  **Fama, E.F., & MacBeth, J.D. (1973)**. "Risk, Return, and Equilibrium: Empirical Tests." *Journal of Political Economy*, 81(3), 607–636.
  → https://doi.org/10.1086/260061
- Newey-West（HAC）標準誤差:
  **Newey, W.K., & West, K.D. (1987)**. "A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix." *Econometrica*, 55(3), 703–708.
  → https://doi.org/10.2307/1913610

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

ML 系（macro）は WF-CV を内蔵するため対象外（→ §9）。`preset` は `recommend` / `sell` のときのみ意味を持つ。**`mu`（μ̂）を含む重みは 400 で reject する**（producer スコアは最新スナップショット1断面のみで as-of 再現できず、黙って欠測にすると「μ̂ 込みで検証した」と誤読されるため・ADR-0030）。`sell` はメタ層×双対層（売り判断の有効性検証）にあたり、上位 N 社＝最も売り向きの銘柄なので、その後リターンがベンチマークを**下回る**ほど売りシグナルが有効と読む。

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
| 財務（質・トレンド・業種内相対・**#373**） | **accruals**（(純利益−営業CF)/総資産・Sloan 質因子）, **delta_roe**, **delta_op_margin**（前年差 [%pt]）, **z_roe_sec**, **z_op_margin_sec**（業種内Zスコア） | （既定外・任意採用） | 無次元（`financial_metrics` VIEW 追加列・追加収集ゼロ。成長/バリューと直交） |
| モメンタム | 12-1ヶ月ログリターン | （use_momentum 時・**既定 OFF**＝ON/OFF の OOF 実測で棄却・ADR-0045） | log(P_short / P_long) |
| マクロ | 市場系: USDJPY/EURJPY/**DXY**/SP500/NIKKEI225/**TOPIX** = YoY、米5/10/30年金利・VIX・FREDクレジット/インフレ = Zスコア。**コモディティ（ADR-0013・#358）: WTI・金・ブルームバーグ商品指数(BCOM)・銅・天然ガス・銀・小麦・トウモロコシ・大豆・プラチナ = YoY**（商品価格は常に正の水準系。変換の正本はコード `_MACRO_MAP`）。**日本実体経済（#250）: 実質GDP = YoY、失業率・貿易収支 = Zスコア**（鉱工業生産/在庫 = YoY は #451 で既定から棄却＝選択肢のみ。e-Stat 側が2026年3月分で配信停止し、昇格ゲート4検定すべて非有意）。**ESRI GDP 需要項目（#373・追加収集ゼロ）: 民間消費・住宅投資・設備投資・公共投資 = YoY（総需要の内訳分解で業種別露出を捉える）**。**日銀/e-Stat（#251）: コアCPI = YoY、短観製造業大企業DI = Zスコア、M2 = YoY**。**OECD先行指標（#283・ADR-0009）: CLI（景気先行指数）= Zスコア**。**IMF WEO見通し（#284・ADR-0011）: 実質GDP成長率・インフレ率の翌年予測 = Zスコア（唯一のforward-lookingチャネル）** | 全選択肢（use_macro 時・#358 でコモディティ含む全系列を既定 ON。BIC が過剰選択を抑制） | YoY = Δ/前年 / Z = (現在−5年平均)/5年SD |
| 交差項 | 選択財務 × 選択マクロ | （use_macro 時） | 積（無次元×無次元） |

被説明変数は **1年先（52週先）週次ログリターン（年率・無次元）**。全特徴量は学習前に `winsorize(p1–p99)`→z-score 標準化を適用。

> **PER/PBR は「循環参照」ではない（重要）**: 目的変数は株価水準ではなく**将来リターン**であるため、現在の PER/PBR で将来リターンを予測するのは正統な**バリュー・ファクター**（Fama-French HML ≒ book-to-market = 1/PBR）。`per×eps=price` の恒等式が問題になるのは「現在株価水準」を当てる場合だけで、本モデルには当てはまらない（**他のプラグイン sector_ols（業種別OLS）の per-share→株価 Ohlson 型（§2）とは目的変数が異なる**）。ただし PER/PBR は分子に同じ株価 P_t を共有し「割安」と「価格の平均回帰」を分離しきれないため、価格を含まないファンダ（roa/eps_growth 等）を既定に併置して補強する。**収益性の質を分解するデュポン因子（net_margin × asset_turnover ≈ roa）・成長（rev_growth）・財務健全性（nc_ratio）も価格フリーの選択肢として提供**する（既定外・任意採用）。div_yield は配当という株価由来のバリュー因子で per/pbr と同枠（循環ではない）。

> **特徴量・マクロの選択 UI**: 財務特徴量（`fin_features` multiselect）とマクロ特徴量（`macro_features` multiselect）は `/analysis` の M-1 タブで選べる。`use_macro`（マスタ ON/OFF）が OFF のときはマクロ・交差項を生成しない。**モメンタムは `use_macro` から独立した `use_momentum`（既定 OFF）で制御する**（§9.4・§9.8：マクロを使いつつモメンタムの過去履歴要件を外して walk-forward CV を成立させるため）。選択肢は **FX・株式・米金利/期間・コモディティ・ボラの5チャネル / 11系列**（#218 フェーズ1）：USD/JPY・EUR/JPY・ドル指数(DXY)・S&P500・米5/10/30年金利・日経225・VIX・WTI・金。既定選択は **#358（ADR-0013）で全選択肢に変更**（従来は USD/JPY・S&P500・米10年金利の3本のみ）。コモディティ含む全マクロ系列を既定 ON にし M-2/M-3 と揃えた。多重共線（VIX↔SP500・米金利↔DXY 等）や無関係系列は pooled BIC（LassoLarsIC）が自動的に剪定するため、既定を広げても最終モデルは絞り込まれる。VIX/DXY/US5Y/US30Y は `collect-macro.yml` の Actions 実行で macro_data への蓄積（各1255〜1257件/5年）を実証してから公開した。**TOPIX は指数 ^TPX 配信停止のため収集を ETF 1306.T（NEXT FUNDS TOPIX・指数とほぼ同追従）へ切替え、YoY 特徴量として公開（#250）。日次の日本10年金利は ^JGB 上場廃止で取得不能のため引き続き未公開**。さらに **#250 で日本の実体経済指標を FRED 経由で追加**：実質GDP(JPNRGDPEXP)=YoY、失業率(LRUNTTTTJPM156S)・貿易収支(XTNTVA01JPQ664S)=Zスコア（米国偏重の是正）。鉱工業生産(JPNPROINDMISMEI)は 2024-04-30 で FRED 凍結が確認されたため **#253 で除外中**（e-Stat コネクタ実装後に再公開予定）。これらは公表ラグが大きいため、収集時に `lag_days`（四半期135日・月次60日）分 `trade_date` を後ろへシフトして先読みバイアス（look-ahead）を防ぐ（[GOTCHAS.md](GOTCHAS.md) 参照）。米クレジット/インフレ系（HY_OAS・IG_OAS・BREAKEVEN10Y・T10Y2Y・JP10Y_FRED）は #221 で FRED から追加済み。**#381 で非ICE代替の信用スプレッド BAA_SPREAD（BAA10Y=Moody's Baa−10Y・日次・非truncated）を追加し、既定の信用ファクターをこちらへ移行（HY_OAS/IG_OAS は FRED の ICE BofA 3年窓制限で 2023-06 以降しか取得できず strict の学習窓を律速するため既定から除外・選択肢としては残置。ADR-0016）**。**#404 で政策不確実性チャネル（EPU）を追加**：`macro_us_epu_zscore`（`USEPUINDXD`＝Baker-Bloom-Davis の米 Economic Policy Uncertainty Index 日次版）・`macro_us_equity_epu_zscore`（`WLEMUINDXD`＝株式市場関連の経済不確実性）。VIX が「市場が織り込む変動」を測るのに対し EPU は「政策・制度側の不確実性」を新聞記事から測る別チャネルで、いずれも常に正の水準指数のため既存の指数系（VIX/CLI/短観DI）と同じ **Zスコア**規約。日次・1985 年開始のため低頻度系列の変換窓（#379/#382）にも strict 律速（#381）にも触れない。日本版（`JPNEPUINDXM`）は FRED 側で 2016-04 に凍結済のため採らない（#253 の `JP_IP` と同型）。**既定採用は実測ゲート通過後**（`scripts/macro_feature_bakeoff.py --preset epu`・3,979社・43ヶ月・57,955サンプル・9 fold）：M-6 の売り側 spread が +0.0652→+0.0684（diff +0.0032・p=0.001・Bonferroni α=0.0125 通過）で有意に改善したため `DEFAULT_MACRO_FEATURES` へ昇格した。**寄与は買い側ではなく売り側に出る**（rank-IC は M-6 +0.0003・M-2 −0.0037 でいずれも補正後非有意）。ADR-0023。**コモディティ・チャネルは #358（ADR-0013）で WTI・金の2本から10本へ拡張**：ブルームバーグ商品指数(BCOM)・銅(HG=F)・天然ガス(NG=F)・銀(SI=F)・小麦(ZW=F)・トウモロコシ(ZC=F)・大豆(ZS=F)・プラチナ(PL=F) を Yahoo v8 から追加（全て YoY）。**BCOM の収集元は指数 `^BCOM` → 連動 ETN `DJP` へ差し替え済み**（#438・2026-07-17 で指数の配信停止。TOPIX の 1306.T 代理と同型で `series_code`／特徴量名は不変）。日本株の業種別コモディティ感応度（銅=非鉄/電線/機械・天然ガス=電力ガス/化学・貴金属=商社/触媒・穀物=食品/飼料）を捕捉する狙い。**同 #358 で M-1 の既定選択も従来の米国寄り3本から全選択肢へ変更**し、コモディティを含む全マクロ系列を既定 ON にした（M-2/M-3 と統一。過剰選択は pooled BIC が剪定）。 **#406（ADR-0024）でニューストーン／関心度チャネルを追加**：`macro_jp_news_tone_zscore`／`macro_jp_news_econ_tone_zscore`（GDELT DOC 2.0 の日本ニュース平均トーン・全体／株式市場テーマ）・`macro_jp_news_econ_vol_zscore`（同テーマの報道量＝全記事比%）・`macro_jp_wiki_market_attn_zscore`／`macro_jp_wiki_macro_attn_zscore`（ja.wikipedia 記事バスケットの日次閲覧数合算）。EPU が記事の**量**で政策不確実性を測るのに対し、トーンは記事の**極性**、報道量・閲覧数は**注目度**を測る別軸。トーンは正負を跨ぐため yoy が使えず、報道量・閲覧数も「平時比でどれだけ注目されているか」がレジーム情報のため、5系列とも **Zスコア**規約。**銘柄別ではなくマクロ集約**（銘柄別日次は 4,000社×250営業日 ≈ 370MB/年で Supabase 無料枠に入らない）。**昇格ゲートは不通過**（`python -m scripts.macro_feature_bakeoff --preset attention`・3,979社・43ヶ月・57,955サンプル・9 fold）：4検定すべて非有意（M-2 rank-IC −0.0060 p=0.140／M-2 売り側 −0.0021 p=0.495／M-6 rank-IC +0.0010 p=0.214／M-6 売り側 −0.0002 p=0.623）。よって**選択肢のみで既定には入れない**（`_GATE_REJECTED_FEATURES`＝実測済み・棄却。未実測の `_PENDING_EVAL_FEATURES` とは別枠）。確定知見＝**未構造のニューストーン／関心度は既存マクロ71本の上に情報を足さない**（構造化された EPU が売り側で効いたのと対照的）。ADR-0024。

### 9.3 特徴量選択（LASSO-LARS / BIC）

`sklearn.linear_model.LassoLarsIC(criterion="bic")` で **LARS パスを1パス計算し、BIC 最小点**を選ぶ（全候補を winsorize→zscore 標準化してから fit）。L1 正則化が共線性をネイティブに処理するため、旧実装の VIF 門番（`check_collinearity` を各候補×各ステップで呼ぶ貪欲前進選択）は不要となり廃止した。BIC 最小解が `max_features` を超える場合は **|係数| 降順の上位 `max_features`** に切り詰める（パラメータ「BIC 最大採用特徴量数」に忠実）。**選択は LASSO だが最終係数は選択済み特徴量で OLS 再フィット**して不偏化する（LASSO は選択専用）。

> **設計判断（2026-06-19）**: 旧「貪欲前進BIC＋VIF」は 36,000 行規模で OLS を約1.2万回呼び、`use_macro=true` 既定で分単位を要した。LassoLarsIC への置換で特徴量選択は秒未満に短縮（実測：選択フェーズ 0.7s）。非劣位チェック（`use_macro=false` 構成）で旧/新の walk-forward CV mean R² は同値（0.0122）を確認済み。詳細は §9.10。

### 9.4 Walk-forward CV

既存の `walk_forward_cv_monthly`（`plugins/utils.py`）で月次ロールウィンドウ CV を実施。時系列順を厳守（通常の k-fold はルックアヘッドバイアスが生じるため不可）。各フォールドの RMSE・MAE・R² を記録。**学習サンプルが要求する履歴長は特徴量構成で決まる**：52週先リターン（未来）は常に必要だが、**12ヶ月モメンタムは `use_momentum=ON` のときだけ過去履歴を要求する**。`use_momentum=OFF`（既定）なら `use_macro=ON` のままでも過去履歴要件が外れ、週次株価が浅くてもフォールドが確保できる（§9.8）。**現在は週次が7年分あるので ON でも fold は立つ**（実測 15 fold）。既定が OFF のままなのは履歴不足ではなく、ON/OFF の OOF 実測で効果が無かったため（§9.8-4・ADR-0045）。

> **purge/embargo（#363・ADR-0014）**: 目的変数が 52週先リターンのため、テスト月のラベル窓 `[t, t+52週]` は直近約12ヶ月分の学習サンプルのラベル窓と時間重複しリークする（López de Prado 2018 Ch.7 の purge 未実装）。`walk_forward_cv_monthly(embargo_months=LABEL_HORIZON_MONTHS=12)` で直近12ヶ月を学習集合から除外し（学習開始を `min_train_months + embargo_months` へ後ろ倒し）、この重複を遮断する。**M-1 と M-2 に対称適用**（比較の公平性・#272 非対称防止）。M-3（§11・週次 1週先ラベル）はラベル窓が 1週で構造的に重複が生じないため月次 purge は非適用。embargo 導入後の OOF rank-IC は過去値（M-2≈0.33 / M-1≈0.23）と非連続の honest 値になる。

### 9.5 リスク指標

| 指標 | 定義 | 役割 | 解像度 |
|---|---|---|---|
| **R2** 実現ボラティリティ | 直前52週の週次ログリターン SD × √52 | 価格変動リスク（Sharpe 分母）。**効用軸（既定）** | 個社 |
| **R_macro** マクロ起因リスク | $\sqrt{\beta^\top \Sigma_{\text{macro}} \beta}$（$\beta$=per-stock 事後ローディング、$\Sigma_{\text{macro}}$=選択因子の共分散） | マクロ要因が銘柄に与えるリターン単位のリスク。**効用軸（R2 と選択制）** | 個社（macro_beta 推論要） |
| **R1** 予測不確実性 | OLS 予測分散 $s^2(1 + x^\top (X^\top X)^{-1} x)$ の平方根（`se_obs`） | イン・サンプルのレバレッジ。縮小駆動に降格（効用軸からは除外） | 個社 |
| **R3** モデル信頼性 | セクター×サイズ三分位バケットごとの walk-forward CV 残差 RMSE | アウト・オブ・サンプルのグループ誤差。**表示/足切りゲート**に降格（低信頼銘柄を上位表示から除外） | バケット |

**効用軸（`risk_axis`）** は R2（実現ボラ）と R_macro（マクロ起因リスク）の選択制。両者ともリターン単位のため λ の次元整合 $U = \mu - \lambda R$ が保たれる。R1 は縮小駆動専用・R3 は足切りゲート（`r3_gate` スライダー）に降格し、効用軸の選択肢から除外されている。λ は 0〜5（既定 1.0）。

**R3 の算出**: 9.4 の walk-forward CV のテスト残差を、各サンプルの (セクター, サイズ三分位) で層別し、バケットごとに $\text{RMSE}=\sqrt{\overline{e^2}}$ を計算する。サイズ代理は**総資産**（`bs_total_assets`。本番で確実に充足するコア BS 項目。`issued_shares` は C2 新列で本番 NULL のため不可）で、分位点は単調変換に不変なので生値の三分位を用いる。閾値は残差を持つサンプルの母集団から決め、現企業へも同閾値を適用。バケットの残差数が下限（5件）未満なら **セクター → 全体** の順にフォールバックする。

**R_macro の算出**: `plugins/utils.py::macro_risk_exposure(beta, cov)` が担う（`√(βᵀΣβ)`）。$\beta$ は `macro_beta` テーブルに蓄積された per-stock 事後ローディング（#214 推論バッチ）、$\Sigma_{\text{macro}}$ はメタに記録された選択因子の共分散行列。macro_beta 未蓄積なら None を返し、クライアントは `r_macro` 軸選択時に null 銘柄をフィルタして graceful degrade する。**M-1/M-2 は `execute` レスポンスに `r_macro_available`（全社 r_macro が None かどうか）を明示的に返し**、UI は risk_axis セレクトの「R_macro」選択肢を無効化＋理由メッセージ表示で対応する（#273：全社 null 時にグラフ・表が理由不明のまま空表示になっていた問題への対応）。M-3（`macro_dlm`）は per-stock β の共分散から自前で r_macro を計算するため macro_beta バッチに依存しないが、共分散推定が失敗した場合は同様に `r_macro_available=False` を返しリスク-リターン散布図に理由メッセージを表示する。

### 9.6 James-Stein 縮小

予測リターン μ_raw をセクター平均 μ_sector へ縮小（Black-Litterman 型）:

$$\mu_{\text{shrunk}} = (1 - w) \cdot \mu_{\text{raw}} + w \cdot \mu_{\text{sector}}, \quad w = R1 / R1_{\max}$$

R1 が大きい（信頼度が低い）ほど強くセクター平均に引き寄せる。

**低シグナル時の縮退（重要）**: R1 = √(s²(1+leverage)) の leverage は全社ほぼ同値（centroid 近傍）のため、現状の本番データでは R1 がほぼ定数となり **w = R1/R1_max ≈ 1（全社）** になる。結果、μ_shrunk は事実上**全社がセクター平均へ潰れ**、銘柄差が消える。これは縮小式の欠陥ではなく、**モデルの説明力が低い（CV R² ≈ 0.01〜・§9.8 の被覆制約に起因）ことの正直な反映**である。仮に縮小式を正規の Black-Litterman（$w = se^2/(se^2+\tau^2)$）へ直しても、予測誤差 se が銘柄間シグナル分散 τ を桁違いに上回るため w≈1 のままで、縮小では分散を取り戻せない。**根本回復には週次株価バックフィル（§9.8・FUTURE_TASKS DF-3）が必要**。このためバブルチャート／効用 U の期待リターン基準には μ_shrunk ではなく **μ_raw を用いる**（§9.7）。μ_shrunk はランキング表の参考列に残す。

### 9.7 Pareto フロンティア と 効用関数（クライアント側後処理）

$$U = \mu_{\text{raw}} - \lambda \cdot R_{\text{axis}}$$

λ はリスク回避度（スライダー、0〜5、既定 1.0）。$R_{\text{axis}}$ は `risk_axis` で選んだリスク（R2 既定 / R_macro）。期待リターンは **μ_raw**（OLS の生予測値。セクター収縮は低シグナル時に銘柄差を消すため廃止）。

**R3 足切りゲート（`r3_gate`）**: CV-RMSE がスライダー値を超える銘柄を上位表示から除外（0=ゲートなし）。低信頼銘柄（モデルがその企業タイプを苦手とするバケット）を推奨集合から取り除くための信頼度 machinery。#217 SELL ランキングにも R3 ゲートを action-label 段で実装（低信頼保有を SELL から除外）。売り推奨側のゲートが読む確実性軸 `r1_prime` は M-1=OLS 予測SE／**M-2=コンフォーマル区間半幅（§11.7.1・#365）**で、M-2 選択時もゲートが機能する。

**効用 U・Pareto 判定・並べ替え・`top_n` 抽出・R3 ゲートは λ／リスク軸にのみ依存する後処理であり、モデル再学習に一切関与しない**。そのためサーバー（`_score_companies`）は**全社の raw 値**（`mu_raw / r1 / r2 / r3 / r_macro`）を返し、これら後処理は**クライアント側（`static/js/analysis.js`）で算出**する。結果として λ 調整・リスク軸切替・表示件数変更・R3 ゲート変更は**再計算なし（再API なし）で即時反映**される（重い計算が走るのは特徴量・マクロ・`max_features` 等モデル本体のパラメータを変えた時のみ）。Pareto 判定軸は表示中の `risk_axis` に追従する。

**選択特徴量の係数可視化（解釈性）**: `execute` は最終 OLS の**標準化係数**を `feature_coefs`（`selected_name → β`）で返す（X・y とも z-score 正規化済のため特徴量間で大小比較可能）。UI（CV パネル）は係数を **|β| 降順の横バー**で表示し、**財務／マクロ／交差項／テクニカルを色分け**、ゼロ中心で符号（正＝株高方向 / 負＝株安方向）を示す。これにより「どの因子・交差項がどの向きに効いたか」を提示する。**CV R² は低い（§9.6・§9.8）ため、係数は符号と相対的大小の目安であり、寄与度の過大解釈は避ける**旨を UI に明記。

**可視化マッピング**（バブルチャート）: **散布図は全社を描画し、効用上位 `top_n` 社を大きく濃く強調**する（残りは小さく淡く）。**y=μ_raw / x=選択リスク軸（R2/R_macro・クライアント即時切替） / 色の濃淡=効用 U / 枠線強調＋フロンティア線=Pareto 優位**。Pareto は（R3 ゲート後の）全社で算出する。**「効用で絞った上位 N だけを描くとリスク方向に潰れて効率的フロンティアが見えない**（λ>0 では低リスク銘柄ばかり選ばれるため）ので、母集団を描く設計とする。**両軸は描画対象の [p1, p99] に固定**し、データ過少銘柄の過大ボラ（例: R2≈19）・過大μ の外れ値で軸が引き伸ばされ全点が隅へ潰れるのを防ぐ（範囲外の <2% は非描画）。

### 9.8 制約・前提

1. 週次株価履歴（`stock_price_weekly.close_last`）が少なくとも1年分（≥52週）必要
2. マクロデータ（`macro_data`）の YoY 用に約400日、Z スコア用に5年分の蓄積が必要（未蓄積は None でスキップ）
3. 学習サンプル数（企業数 × 月数）が 20 件未満の場合はプラグインが空結果を返す
3-a. **M-1 は strict（`macro_nan_ok=False`）＝選択中のマクロ特徴が1つでも None のスナップショット行を落とす**。これは M-2（NaN 許容・§11.3.1）との構造的な違いで、**「全マクロ既定 ON」（#358）と組み合わさると1系列の欠損が全学習データを消す**。実際 #379 では低頻度3系列（`JP_REAL_GDP` yoy / IMF WEO 2系列 zscore）の変換が全期間 None になり、**M-1 の既定実行が学習0件（UI 空表示・例外なし）** になっていた（fix は [GOTCHAS.md](GOTCHAS.md)「マクロ指標」節）。派生として、strict の学習可能期間は**最も収集開始が新しいマクロ系列**に律速される。2026-07-23 時点で `HY_OAS`/`IG_OAS` の 2023-06 開始により 24ヶ月＝`min_train_months=6`+embargo=12 で fold 2 期しか立たなかったが、これは **FRED が 2026-04 以降 ICE BofA 系列をローリング3年窓に制限し 2023 以前を配信しない**ため（再収集でも遡れない）。#381 で**既定の信用ファクターを非ICE代替 `BAA_SPREAD`（`BAA10Y`=Baa−10Y・日次・非truncated）へ移し、`HY_OAS`/`IG_OAS` を既定から除外（選択肢としては残置）**。これで fold は実用水準へ回復した（[ADR-0016](adr/0016-ice-bofa-truncation-baa-credit-proxy.md)・[GOTCHAS.md](GOTCHAS.md)「マクロ指標」節）。**その後 2026-08-01 の実測（`python -m scripts.measure_strict_binding`・Issue #411）で、strict はもはや学習窓を律速していないと確定した**：除外後の既定マクロ46本は全て 2021-01（週次株価の開始月）から非 None で、strict / nan_ok / マクロ無しの3条件が同一母集団（47ヶ月・2021-08〜2025-06・111,210サンプル）になる。現在の窓を決めているのは `stock_price_weekly`（2021-01-04〜・52週先ラベルで上限53ヶ月）と `financial_records`（先頭6ヶ月を削る）というデータ履歴長で、**マクロ既定の増減では伸びない**。
4. **被覆制約はモメンタム由来（`use_momentum=true` のとき）**: 「52週先リターン（未来必要）」かつ「12ヶ月モメンタム（過去必要）」を同時に要求すると、週次株価が約2年分しかない環境では両条件を満たす月が**約1ヶ月の薄い帯**に収縮し、walk-forward CV が 0 フォルド（`mean_r2=None`）になる（**2026-06-20 当時＝週次が 2024-05〜 だった頃の制約。現在は解消済み・下記参照**）。**本改修でモメンタムを `use_macro` から切り離し `use_momentum`（既定 OFF）化**したため、**既定構成（`use_macro=ON` / `use_momentum=OFF`）ではマクロ・交差項を使ったまま CV が複数フォルドで成立する**（モメンタムの過去履歴要件が外れるため）。**この薄帯制約は #198 のバックフィルで解消済み**（2026-08-31 実測: `stock_price_weekly` は 2019-07-29〜2026-08-24・1,306,610 行・4,024 社で、104週以上の履歴を持つ社が 3,686＝92%。`use_momentum=ON` でも 62ヶ月・91,740 サンプル・**15 fold** 立つ）。**したがって現在の既定 OFF はデータ制約ではなく実測の結果である**——ON/OFF の honest OOF 比較（`python -m scripts.momentum_gate`）で4検定すべてが Bonferroni 補正後 α=0.0125 を通らず、点推定の符号も全て負だった（M-6 rank-IC −0.0104 p=0.100 / M-2 −0.0056 p=0.528 / 売り側も両モデル負）。**raw の母集団のままでは ON が全指標で改善して見える**（M-6 rank-IC 0.1866→0.2068）が、それはモメンタムが計算できない行＝履歴の浅い当てにくい銘柄が落ちた効果で、共通 (ym,ec) 域へ揃えると消える（[ADR-0045](adr/0045-momentum-gains-vanish-when-the-population-is-aligned.md)）。**M-1 自身は ADR-0045 では測っていなかった**（strict 母集団のため M-2/M-6 の結果が転用できない）が、2026-09-04 に窓 [3,6,12,18,24] 込みで実測して同じ結論になった——共通域 32,438件・11 fold で10検定すべて非有意、標準の12-1モメンタムは **−0.0792（p=0.093）と負**、探索が選ぶ窓18 は raw の +0.0435 が共通域では **+0.0039（p=0.757）＝改善の 91% が母集団効果**（[ADR-0050](adr/0050-hyperparameter-search-must-align-populations.md)・#583）。バックフィルは `backfill_weekly_history_yahoo`（`collector_prices.py`）を実装済み＝`python _pipeline_gh.py --backfill-weekly --backfill-weekly-years 5`、または GitHub Actions の「[一回性] 週次株価バックフィル」ワークフローで本番実行する（要本番収集権限）。週次の最古日が `today-years` より新しい社だけを Yahoo から過去方向に取得し、`record_prices_batch` 経由で daily→weekly 再集約する（1社ごとに daily を trim するため Supabase 500MB を超えない）。実行後の検証: `SELECT min(trade_date) FROM stock_price_weekly` が `today-years` 近傍まで遡り、`use_momentum=true` で `cv_metrics.n_folds >= 2`。

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

### 9.11 アウトオブサンプル検証（OOF）（#272・ADR-0004）

M-1 の `execute()` も M-2/M-3 と同じ共有ヘルパ `plugins/macro_snapshots.py::oof_backtest` を
walk-forward CV の無リーク残差（`cv_residuals_by_ym`）に適用し、`oof_backtest`
（分位リターン・rank-IC・ロングショート spread・hit-rate）を返す（§11.7 と同一指標・同一定義
＝3モデルを横並びで比較可能）。**既存「バックテスト」（§7）とは別概念**で「予測 μ̂ が将来
リターンを順序付けるか」を測る。M-1 は producer スコアテーブルを持たない（`macro_beta` は
別バッチ `macro_beta_inference.py` が担う）ため、M-2 の §11.7 後半にある producer 永続化は
該当しない。**この無リーク残差は §9.4 の purge/embargo（`embargo_months=12`・#363・ADR-0014）を
経た honest な OOF であり、M-2 と対称**（M-3 は週次で構造的に非該当）。

> **purge の実測影響（#375・2026-07-23・本番データ・`scripts/measure_embargo_impact.py`）**: embargo=0→12 で **M-2 の OOF rank-IC は 0.348→0.141（−59%）**、M-1（財務のみ OLS）は 0.237→0.195（−18%）。embargo=0 が過去記録値（M-2≈0.33）を再現した上での低下＝**旧 M-2≈0.33 は大半が 52週先ラベルの前方リーク由来で honest 値は約0.14**。柔軟な XGBoost ほどリークを吸っていた。3兄弟比較や `mu_source` 選択は honest 値（embargo 済み）で判断する。

### 9.12 モデル比較の統計的厳密化（#369・全モデル横断）

`oof_backtest` の mean 値を目視で並べるだけでは「あるモデルが他より**本当に**有効か」は言えない。
walk-forward の各 fold は学習窓が重複するため per-fold の rank-IC 系列は iid ではなく、素朴な
paired-t は系列相関を無視して分散を過小評価し、有意差を過大に主張する。#369 では**純後処理**
（追加学習・価格取得・Egress ゼロ・stdlib のみ）で2点を補強する。実装は `model_stats.py`
（`oof_backtest` と `model_comparison.run_comparison` が共用）。

- **rank-IC 差の有意性マトリクス**: 各モデルの per-fold IC 系列（`oof_backtest.rank_ic_by_period`
  ＝`{test_ym: ic}`）を**共通 test 期でペアリング**し、差 IC_A−IC_B の平均を **定常ブートストラップ**
  （Politis & Romano 1994・リサンプル単位を幾何長ブロックにして fold 間相関を標本内に保存）で検定する。
  95% CI が 0 を跨がなければ有意。`run_comparison` は全モデルペアの上三角を `significance_matrix`
  として返し、`/analysis` の比較ビューが ▲（行モデル優位）/▼（劣位）/n.s.（有意差なし）で表示する。
- **分位単調性**: top−bottom spread へ畳むと中間分位の U 字/非単調（過学習・不安定シグナル）が隠れる。
  `oof_backtest.monotonicity` は期毎の Spearman(分位idx, 分位平均リターン) の mean/std、隣接分位の
  正順率（`adjacent_increasing_rate`）、および「単調増が偶然でない」片側ブートストラップ p 値を返す。

> **なぜ Nadeau-Bengio ではなく定常ブートストラップか**: Nadeau & Bengio (2003) の補正 t は
> train/test の重複比を要するが、`oof_backtest` は per-fold の train/test サイズを保持しない。
> 定常ブートストラップは分布仮定もサイズ情報も不要で、系列相関を保存したまま平均の分布を得られる。
> ADR-0004 が確立した OOF-first 哲学を「差の有意性」まで押し進める補強であり非矛盾。
> 参考: Politis & Romano (1994) DOI:10.1080/01621459.1994.10476870 / Nadeau & Bengio (2003) DOI:10.1023/A:1024068626366。

### 9.13 OOF の現実性強化：業種中立rank-IC＋ネットターンオーバーコスト（#368・ADR-0018）

生 rank-IC と 100%回転固定のネット spread だけでは、**業種ベットと真の銘柄選択力**／**低回転な安定モデルと高回転モデル**を区別できない。#368 は `oof_backtest` に `meta_by_ym`（残差と同順の `{ym:[(stock_id, industry)]}`・`build_oof_meta` が build_snapshots 出力から組む）を渡したときのみ算出する2指標を**純後処理・追加学習/Egress ゼロ・numpy 不要**で足す（無印キーは不変＝後方互換）。

- **業種中立 rank-IC**（`rank_ic_industry_neutral`）: 各期・業種内で yhat/y_true をそれぞれ平均順位化→業種平均順位を引く（順位デミーン）→全業種プールして Spearman → サンプル数加重平均。「素材>ハイテクを WTI で一括に並べる」ようなセクター傾斜で稼いだ IC を除去し、**業種内の真の銘柄選択力**だけを測る（`industry=None` の行・単独業種は除外）。
- **実効ターンオーバー＋ブレークイーブンbps**（`effective_turnover` / `breakeven_cost_bps`）: 隣接期の top/bottom 分位メンバー（stock_id）の Jaccard 非重複（入替割合 0..1）を平均＝実効ターンオーバー。ブレークイーブンbps＝ロングショート spread が消える片道コスト水準 `gross·50/turnover`（既存 `cost_bps`（#316）と同一規約：net = gross − (cost_bps/100)·2·turnover=0 の解）。gross・turnover はともにリバランス頻度に比例するため **breakeven は比で頻度不変＝モデル横断で直接比較できる単一スカラー**。低回転な安定モデルほど大きい（コスト耐性が強い）。`long_short_spread_net_turnover` は実効回転で控除したネット、`annual_turnover`（＝回転×`rebalance_per_year`）は参考値。

`model_comparison` へ透過し `/analysis` の比較ビューが「生IC ／ 業種中立IC」「ブレークイーブンbps ／ 実効ターンオーバー」を並置する。M-3（週次残差を月へ束ねるため1銘柄が同月複数行）は分位メンバーを stock_id 集合で dedup するため Jaccard は近似的だが頑健。参考: Grinold & Kahn "Active Portfolio Management"（turnover-adjusted・業種中立）。

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

プリセット（`マクロ予測型`（**既定**）/ `バランス型` / `割高警戒型` / `業績悪化重視`）は $w_i$ の既定値、UI でスライダー上書き可（スライダーは既定で折りたたみ表示）。**マクロ予測型**は期待リターン μ（`mu`）とマクロリスク −Rᴹ（`neg_r_macro`）の2軸のみを用いる（スコアは $\sum w_i(-z_i)/\sum w_i$ で正規化されるため両者の**比率のみ有意**・既定 1.0 : 0.5）。μ の出所（`mu_source`）は M-1（`macro_risk_return`）／M-2（`macro_gbdt`）／M-3（`macro_dlm`）／M-4（`macro_ensemble`）／M-6（`macro_enet`・**既定**・#396）から選ぶ。**R3 足切りゲートが効くのは `r1_prime` を持つ M-1・M-2・M-6 のみ**（M-3/M-4 は不在＝無効）。

> **既定 μ 出所の選定根拠（#402・ADR-0022）**: 既定は M-2 → **M-6** へ切替済み。判定は `oof_backtest` の**売り側指標**（`short_side_spread`＝期内全体平均−最低 μ̂ 分位平均・§11.7.2）で行う（ロングショート spread は top 分位の強さに引っ張られ売り判定の質を測れないため）。同一共通域（3,979社・9 fold・OOF 13,539ペア・honest/embargo=12）の実測は **M-6 +0.0656 > M-4 +0.0645 > M-1 +0.0581 > M-2 +0.0511**、M-6−M-2 は +0.0145・95%CI[+0.0072,+0.0219]・p=0.001 で**有意**、M-4−M-6 は p=0.655 で互角（統合は単体を超えない＝ADR-0015 と同じ判定）。**買い側 rank-IC の順位とは一致しない**（M-2 は rank-IC 0.1419 > M-1 0.1142 だが売り側では逆転）＝売り既定の選定に買い側指標を使ってはいけないことの実証。再現は `python -m scripts.sell_mu_source_bakeoff`。

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
4. **マクロ予測型プリセット（既定）は μ・−Rᴹ の両方が選択 μ モデル（既定 M-6 `macro_enet`・要ローカル実行）の実行結果に依存する**（`neg_r_macro` も `mu_scores` の `r_macro` 由来）。モデル未実行なら両シグナルが欠損し、全保有が score=None＝「データ不足」になる（UI は `mu_available:false` 注記で理由を表示）。他観点も併用したい場合はスライダーで手動追加するか別プリセットを選ぶ
5. 投資助言ではなく、保有整理の参考スコアにすぎない

### 10.6 参考文献

- **Jegadeesh, N. & Titman, S. (1993)**. "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency." *Journal of Finance*, 48(1), 65–91. → https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- **Ohlson, J.A. (1995)**. "Earnings, Book Values, and Dividends in Equity Valuation." *Contemporary Accounting Research*, 11(2), 661–687. → https://doi.org/10.1111/j.1911-3846.1995.tb00461.x

---

## 11. M-2 マクロ×財務 勾配ブースティング（macro_gbdt）

`plugins/macro_gbdt.py` / `MacroGbdtPlugin`  
**カテゴリ**: ③ 将来リターンを予測（`ui_order=340`、`heavy=True`）

### 11.1 概要

M-1（§9）の**非線形兄弟**。同一の目的変数（52週先対数リターン）・同一スナップショット母集団・同一のリスク-リターン幾何（μ／R2／R3／[[系統的マクロリスク曝露]]／効率的フロンティア）を共有しつつ、勾配ブースティング決定木（XGBoost）が `fin×macro` の交互作用を**自動学習**する。M-1 が手動生成した交差項は持たない。

**M-2 の価値は「M-1（OLS 線形）と M-2（XGBoost 非線形）を同一データで並置・比較し、どちらが有効かをユーザー自身が判断・改善し続けられること」**にある（VISION.md の核心）。

### 11.2 設計決定（ADR-0003）

| 決定 | 内容 |
|---|---|
| 実行アーキ | 同期 in-execute・`heavy=True`（Render では 403・ローカル限定）。モデル非永続・毎回学習 |
| データ構築 | `macro_snapshots.py` の共有ビルダー（`build_interactions=False`・`macro_nan_ok=True`）。M-1 と**同一母集団を構造保証**しつつ、マクロ欠損は NaN として保持（§11.3.1） |
| walk-forward CV | `walk_forward_cv_monthly` に `fit_predict` コールバックを注入。同一 fold・同一 r2/rmse 式を共有 |
| 内蔵比較 | 同一特徴量・同一 fold の**素 OLS ベースライン**（交差項/BIC なし）を side-by-side で出力 |
| SHAP | グローバル mean\|SHAP\|（`feature_coefs`）＋**signed SHAP**（`feature_coefs_signed`＝重要度×学習方向）＋**学習方向 corr**（`feature_shap_dir`）＋**交互作用**（`feature_interactions`＝特徴量ペア強度）＋per-stock SHAP 全社添付（#371・§11.5） |
| R1 | 出さない（OLS 固有の予測 SE。効用軸でないため欠落しても幾何は壊れない） |
| R_macro | 既存 `macro_beta` producer から流用（M-1 と軸パリティを維持） |
| 正則化 | 強正則化デフォルト（`max_depth=4`・`min_child_weight=5`・`subsample=colsample_bytree=0.8`・`reg_lambda=1.0`・`lr=0.05`）。日本株 52週リターンは低 S/N のため過学習を抑制 |

### 11.3 特徴量

M-1 から**交差項（`fin×macro`）を除いた**同一セット。BIC/LASSO の事前選択なし（木の暗黙選択に委ねる）。

- 財務ベース（`fin_features` multiselect）: M-1 と同一選択肢
- マクロ（`macro_features` multiselect）: M-1 と同一選択肢（`use_macro=True/False` で制御）
- モメンタム（`use_momentum`、既定 OFF）: M-1 と同一。**M-2/M-6 で ON/OFF を honest OOF 実測した結果、共通 (ym,ec) 域では4検定すべて補正後 α を通らず符号も負のため既定 OFF を維持**（`python -m scripts.momentum_gate`・ADR-0045）。**窓（`momentum_window`）も 2026-09-04 に [3,6,12,18,24] を実測して棄却**——M-2 は共通域で全窓が基準以下、窓24 は **−0.0264（p=0.001）で有意に悪化**した（[ADR-0050](adr/0050-hyperparameter-search-must-align-populations.md)）
- **価格行動系（`price_features` multiselect、既定 OFF・Issue #364）**: M-3（§13）と**共有**する銘柄固有の遅行特徴量（週次実現ボラ `px_rvol`・出来高z-score `px_volz`・52週高値乖離 `px_high52dev`・4週リバーサル `px_rev4w`）。定義の正本は `macro_snapshots.build_price_features`（M-2/M-3 共有）。実現ボラ・出来高z・リバーサルは**非線形/閾値効果が強く GBDT の得意領域**。各スナップショット `snap_idx` 時点の既知値（未来を覗かない・`px_high52dev` の52週 warmup 分は nan → XGBoost がネイティブ処理）を momentum の直後に追加する。**追加収集ゼロ・追加 Egress ゼロ**（`volume_sum` は `StockPriceWeekly` 由来で取得済）。px_* は全て無次元→木で単調不変・次元整合 OK。`use_momentum` と同様に**M-2 側でゲート**し M-1 の OLS 特徴を汚さない。**既定 OFF は保守設定**で、OOF 前後比較（rank-IC / long-short spread / hit-rate）と個別 ablation で有効性を確認してから DEFAULT_PRICE_FEATURES を既定化する（「検証→全選択化」パターン）。参考: Jegadeesh & Titman 1993。

#### 11.3.1 マクロ欠損の扱い（NaN 許容・M-2 専用）

マクロ特徴量は**日付単位**（全企業共通）だが、各企業の現在スナップショット日付（最終週次株価日）はバラバラ。カバレッジの薄いマクロ系列（最近追加・凍結・年次のみ等）を選ぶと、その系列が欠損する日付の企業が一斉に脱落する。M-1（OLS）は欠損で当該スナップショットを破棄するしかないが、**M-2 は XGBoost が NaN をネイティブ処理できる**ため、`build_snapshots(macro_nan_ok=True)` で**マクロ欠損を `float('nan')` として保持し企業を残す**。

- **表示企業母集団は財務特徴量＋株価履歴で決まる**（マクロ選択に依存しない）。薄いマクロ系列を足しても企業数が激減しない。
- 表示可否は `min_coverage`（特徴量充足率下限・既定 0.5）が graceful に制御。マクロが欠損だらけ（>50%）の企業のみ除外。
- 財務特徴量の欠損は従来どおり厳格除外（企業固有・コア指標）。NaN 許容は**マクロのみ**。
- 内蔵 OLS ベースライン（§11.2）は NaN を扱えないため、`fit_feature_columns`/`transform_feature_row`（`plugins/utils.py`）が**学習フォールドの列平均で補完**（学習統計のみ使用＝リークなし・正規化後ほぼ 0 の中立値）。XGBoost 経路は補完せず NaN のまま学習・予測・SHAP する。

### 11.4 walk-forward CV と early_stopping

各フォールド（train 月 < test 月・`min_train_months=6`・`step_months=3`）で:

1. y を p1-p99 winsorize（X は winsorize しない。木は単調不変）
2. train の時系列末尾 20% を `eval_set`（リーク安全・実運用設定を模す）
3. `early_stopping_rounds`（既定 40）で過学習を検知して学習停止
4. `best_iteration` を記録

**最終モデル**: CV フォールドの `best_iteration` の中央値を `n_estimators` として全データで refit。直近月を捨てず予測に活用。

#### 11.4.1 経済符号の単調性制約（monotone_constraints・#366）

BIC/LASSO の事前選択を持たない M-2 は全特徴量を木に丸投げするため、低 S/N な日本株 52週先リターンでは**符号が経済理論と逆の過学習分岐**が生じうる。`use_monotone_constraints`（checkbox・**既定 OFF**）を ON にすると、符号が経済理論から明確な財務比率のみ XGBoost の [`monotone_constraints`](https://xgboost.readthedocs.io/en/stable/tutorials/monotonic.html) で「特徴量↑→予測 μ̂ ↑（+1）/↓（−1）」を木の分岐に強制する（Chen & Guestrin 2016 KDD）。

- **符号表**（`plugins/macro_gbdt.py::_MONOTONE_SIGN`・唯一の源）: `pbr`/`per`/`de_ratio`→**−1**（割高・高レバレッジ→将来リターン低）、`roe`/`roa`/`op_margin`/`div_yield`→**+1**（クオリティ・インカム→将来リターン高）。
- **収載しない = 0（制約なし）**: マクロ系（符号がレジーム依存）・業種内Zスコア（`z_*`）・曖昧な成長/流動性指標（`equity_ratio`/`sales_growth`/`profit_growth`/`current_ratio`）・モメンタム・`px_*`・交差項。`_build_monotone_constraints(all_feat_names)` が列位置に整合したタプルを組み（numpy 入力は列名を持たないため位置整合が必須）、`xgb_params` 経由で **CV の `fit_predict` と最終モデル双方**へ自動注入する。
- 本制約は符号の**事前知識**の唯一の注入点。signed SHAP（§11.5・`feature_shap_dir`・#371）は学習後に木が実際に付けた方向の**事後**診断であり、本制約の事前符号とのクロスチェックに使える（符号表の妥当性検証が M-2 内で完結）。符号がモデル構造として保証され解釈性も上がる。
- **検証方針**: ON/OFF を OOF rank-IC（§11.7）で直接比較し、特に **fold 間 std の低下（頑健化）** を確認してから既定化を判断する（`use_momentum`/`px_*` と同じ保守ゲート）。native XGBoost 機能（新パッケージ不要・次元不変）で ADR-0002（M-1 の交差項却下）とは別軸・非抵触。M-5（§14・XGBRanker）も `execute` を継承し、ランカーも `monotone_constraints` を受理するため同一符号表がそのまま効く。詳細は ADR-0019。

#### 11.4.2 セクター/サイズのカテゴリ特徴量（#370）

XGBoost は業種別の分岐や**セクター×マクロ交互作用**（素材×WTI・ハイテク×DXY 等）を木で暗黙学習できるが、業種情報がモデル入力に無ければ学習しようがない。M-1 は交差項を ADR-0002 で却下しているため、これは**木ならではの拡張**（3兄弟の差別化に寄与）。`use_sector_features`（checkbox・**既定 OFF**）を ON にすると、`build_snapshots` が既に算出済の `(industry, size=bs_total_assets)` メタから 2 列を **M-2 `execute` 内で後付け連結**する（`build_snapshots` は無改変＝M-1 の OLS 特徴を汚さない）。

- **`log_size`**: `log(bs_total_assets)`。木は単調不変ゆえ log/raw で分岐は不変だが解釈性で log を採る。欠損（None/非正）は `float('nan')`（XGBoost がネイティブ処理・「不明」相当・fold 非依存でリークなし）。
- **`sector_te`**（業種の**リークフリー target encoding**）: 業種を「その業種の平均リターン」で数値化する。**リークが最大リスク**（#375 で旧 IC 0.33 が 52週先ラベルの前方リーク由来と判明した反省）のため、**必ず学習 fold 内の業種平均のみ**で fit する — CV は `_wrap_sector_target_encoding` が各 fold で学習集合だけから業種→平均リターン写像を作り test にも同一写像を適用（test 自身のラベルは使わない）、最終モデル/現在断面のスコアリングは全学習データで fit（current にラベルは無く無リーク）。未知/欠損業種（「不明」含む）は学習集合の全体平均へフォールバック。
- **列構成**: `model_feat_names = all_feat_names + [log_size, sector_te]`（末尾 2 列）。`monotone_constraints`（§11.4.1）は両列を 0（符号が業種・レジーム依存）とする。SHAP・per-stock SHAP・`selected_features` も `model_feat_names` で揃う。**OLS ベースライン CV は sector-free の base 特徴のまま**回す（XGB と OLS の差＝非線形性の効果を切り分ける・M-1 の交差項却下と同型）。
- **方式選択**: (A) native categorical（`enable_categorical`＋pandas category）は `np.array(float)` パイプラインを DataFrame へ変える改修が要る（effort M）ため本 Issue では見送り、追加収集ゼロ・VIEW 列追加ゼロで実装できる (B) リークフリー target encoding を採用。`tuning_search_space` の `use_sector_features` 軸で OFF/ON を自動比較できる。
- **検証方針**: OOF rank-IC（§11.7）の ON/OFF 前後比較＋ SHAP でセクター/サイズ特徴の寄与を確認してから既定化を判断する（`use_momentum`/`px_*`/`monotone` と同じ保守ゲート）。M-5（§14・XGBRanker）も `execute` を継承し、ラッパーは 3 引数呼び出し（`pass_train_groups`）を素通しするため同一の sector 特徴がランク学習にもそのまま効く。参考: XGBoost 公式 Categorical Data docs; ADR-0003（M-2 GBDT）。

### 11.5 SHAP による解釈（signed SHAP＋交互作用・#371）

SHAP（SHapley Additive exPlanations）は個々の特徴量がモデルの予測値にどれだけ貢献するかを分解する手法。M-2 は以下 5 系統を返す:

- **グローバル重要度 mean|SHAP|**（`feature_coefs`）: 全銘柄にわたる絶対値平均（非負・後方互換）
- **signed SHAP**（`feature_coefs_signed`・#371）: `mean|SHAP| × 学習方向符号`。棒長で重要度、左右で方向を同時に読める（M-1 の標準化係数バーと同じ見え方に整合）。UI の M-2 グローバルバーはこれを表示
- **学習方向 corr**（`feature_shap_dir`・#371）: 各特徴量の値とその SHAP 寄与の相関 corr∈[-1,1]。木が実際に付けた単調方向の**事後**診断で、`monotone_constraints`（§11.4.1）の事前符号とのクロスチェックに使える（例: `roe` の事前 +1 に対し corr が負なら要調査）
- **SHAP 交互作用**（`feature_interactions`・#371）: `shap_interaction_values`（TreeSHAP・(n,F,F)）の off-diagonal 断面平均で特徴量ペアの交互作用強度を測り、上位ペアを返す。**M-2 が交差項なしで自動学習する `fin×macro` 非線形構造の中身**を可視化する。計算コスト O(n·F²) のため断面を最大 `_INTERACT_MAX_ROWS`（既定 800 社）へ等間隔サブサンプル。`shap_interactions` パラメータ（既定 ON）で OFF 可
- **per-stock SHAP**: 各銘柄の特徴量寄与内訳。`results[i].shap` に丸め値を添付。UI の行クリックで展開

### 11.6 出力契約

M-1（§9）と同一形式で散布図・効用・フロンティアの client 機械を共用できる:

```json
{
  "cv_metrics": {
    "xgb":          {"mean_r2": ..., "mean_rmse": ..., "n_folds": ..., "folds": [...]},
    "ols_baseline": {"mean_r2": ..., "mean_rmse": ..., "n_folds": ..., "folds": [...]}
  },
  "selected_features": ["per", "pbr", ...],
  "feature_coefs":        {"per": 0.042, ...},   // mean|SHAP|（非負・後方互換）
  "feature_coefs_signed": {"per": -0.042, ...},  // 署名付き重要度（#371）
  "feature_shap_dir":     {"per": -0.71, ...},   // 学習方向 corr∈[-1,1]（#371）
  "feature_interactions": [                       // 上位交互作用ペア（#371）
    {"a": "per", "b": "macro_ust10y", "strength": 0.0031}, ...
  ],
  "shap_interactions_available": true,
  "results": [
    {"edinet_code": "...", "mu_raw": ..., "r1": null, "r2": ..., "r3": ..., "r_macro": ...,
     "shap": {"per": 0.012, "pbr": -0.008, ...}, ...}
  ],
  "model_type": "xgboost",
  "best_iteration": 120,
  "oof_backtest": {                       // アウトオブサンプル検証（OOF・ADR-0004）
    "n_quantiles": 5, "n_periods": 24, "n_periods_quantile": 22, "n_oof_samples": 4123,
    "quantile_returns": [...],            // 期内横断分位→期間平均（左=最低μ̂→右=最高μ̂）
    "rank_ic": {"mean": 0.03, "std": 0.11, "n": 24},   // Spearman(μ̂, y) を fold 毎
    "rank_ic_industry_neutral": {"mean": 0.02, "n": 22},  // 業種内順位デミーン後（#368・§9.13）
    "long_short_spread": 0.018, "hit_rate": 0.58,
    "short_side_spread": 0.012, "short_side_hit_rate": 0.73,  // 売り側識別力（#402・§11.7.2）
    "short_side_spread_by_period": {"2024-01": 0.014, ...},   // per-fold 系列（差の有意性検定用）
    "effective_turnover": 0.31, "breakeven_cost_bps": 290.0,  // 実効回転・ブレークイーブン片道bp（#368）
    "long_short_spread_net_turnover": 0.018, "annual_turnover": 1.24   // cost_bps=0 なら net=gross
  }
}
```

### 11.7 アウトオブサンプル検証（OOF）と売り推奨連携（ADR-0004）

M-2 の `execute()` は walk-forward CV の**無リーク OOF 予測**（`{test_ym:[(yhat,y_true),…]}`）から、**再学習・追加価格取得なしで** `oof_backtest` を返す。指標は ① **分位リターン**（各期で μ̂ を横断ランク→分位→分位平均実現リターン→期間平均＝per-period cross-sectional・μ̂ 水準の時系列ドリフトに頑健）、② **rank-IC**（Spearman(μ̂, y) を fold 毎→平均±std）、③ **ロングショート spread**（top−bottom 分位）、④ **hit-rate**（top>bottom だった期の割合）、⑤ **区間被覆率**（`interval_coverage`＝コンフォーマル区間の honest walk-forward 実測被覆率・§11.7.1）、⑥ **売り側 spread**（`short_side_spread`＝期内全体平均−最低 μ̂ 分位平均・§11.7.2）。**既存「バックテスト」（§7・preset/as-of のポートフォリオ模擬）とは別概念**で「予測 μ̂ が将来リターンを順序付けるか」を測る（用語は CONTEXT.md「[[アウトオブサンプル検証]]」）。共有ヘルパ `plugins/macro_snapshots.py::oof_backtest`。

あわせて per-stock μ̂ と `r1_prime` を `macro_gbdt_scores` テーブルへ**全置換で永続化**し（`sector_ols`→`regression_results` と同型・producer.execute 直書き）、**売り候補ランキング（§10）が `mu_source` トグル**（既定 `macro_risk_return`＝M-1／`macro_gbdt`＝M-2）で読む。M-2 の `read_producer_scores` は M-1 と同一形 `{mu, r_macro, r1_prime}` を返す（`r_macro` は共有 `macro_beta`）。**`r1_prime` は §11.7.1 のコンフォーマル区間半幅**で埋め、**R3 足切りゲートは M-2 選択時も機能する**（#365・ADR-0020。列未 migration / 旧スナップショットの `r1_prime=None` はゲート素通り）。

#### 11.7.1 コンフォーマル予測区間（確実性軸 r1_prime・#365・ADR-0020）

XGBoost は OLS のような閉形式の予測 SE を持たない。代わりに**無リーク OOF 残差 |resid| の τ 分位（既定 τ=0.9）を区間半幅**とする**分割コンフォーマル**（Lei et al. 2018）で確実性軸 `r1_prime` を与える。marginal 版（全銘柄一定半幅）は sell_ranking の R3 足切りゲートを全通過/全遮断の二択に退化させるため、**既存 R3 バケット（業種×サイズ三分位）条件付き**で per-stock 化する（`_compute_r3_buckets`／`_r3_for` と同一の bucket→sector→global フォールバック規約・標本数 <`CONFORMAL_MIN_BUCKET`=20 は下位粒度へ）。R3（=√平均二乗残差＝リスク軸）と同一残差から出るが役割は別（`r1_prime`=|resid| の τ分位＝**確実性軸**）。共有ヘルパ `conformal_bucket_halfwidths` / `conformal_halfwidth_for`（`macro_snapshots.py`・M-1/M-2 family-wide）。

被覆診断は `oof_backtest` に `interval_coverage`（honest walk-forward: 各 test 期をそれより過去の全 |resid| で較正した半幅で被覆判定→標本加重平均）を追加し、`model_comparison`（`/api/backtest/model-comparison`）へ**全モデル横並びで表示**（理想は ≈`interval_tau`）。追加学習・Egress ゼロの純後処理。

#### 11.7.2 売り側（ショート側）識別力（#402・ADR-0022・全モデル横断）

下流の売り候補ランキング（§10）は μ̂ の**下位**を売る。ところが `long_short_spread`（top−bottom）は **top 分位の強さに引っ張られる**ため、「買い候補としては強いが売り候補の見分けは弱い」モデルでも大きく出る。μ 出所（`mu_source`）を売り判定基準で選ぶには売り側専用の指標が必要になる。

$$
\text{売り側 spread} = \frac{1}{T}\sum_{t=1}^{T}\Big(\overline{y}_t - \overline{y}_t^{\,(q_1)}\Big)
$$

$\overline{y}_t$ は期 $t$ の**全サンプル平均**実現リターン（分位サイズが端数で不均一になるため分位平均の単純平均は使わない）、$\overline{y}_t^{(q_1)}$ は最低 μ̂ 分位の平均。ロングオンリーの保有者にとっての売りの価値＝「市場平均を下回る銘柄をどれだけ回避できたか」を測る（**大きいほど有効**）。あわせて `short_side_hit_rate`（成立期の割合）と per-fold 系列 `short_side_spread_by_period`（`model_stats.paired_ic_significance` で候補間の差を検定する入力）を返す。追加学習・Egress ゼロの純後処理で、`oof_backtest` を呼ぶ**全モデルへ自動的に波及**する（モデル比較 UI にも表示）。

実測（同一共通域・3,979社・9 fold・OOF 13,539ペア・honest/embargo=12）:

| モデル | 売り側 spread | 売り勝率 | 最低分位リターン | rank-IC（参考） |
|---|---|---|---|---|
| **M-6（ElasticNet・新既定）** | **+0.0656** | 88.9% | **+0.0556** | +0.1713 |
| M-4（3基底統合） | +0.0645 | 88.9% | +0.0567 | +0.1720 |
| M-1（OLS） | +0.0581 | 100% | +0.0631 | +0.1142 |
| M-2（XGBoost・旧既定） | +0.0511 | 88.9% | +0.0701 | +0.1419 |

**M-2 は rank-IC で M-1 を上回るのに売り側では下回る**（順位が逆転する）＝売り既定の選定に買い側指標を流用してはいけないことの実証。再現は `python -m scripts.sell_mu_source_bakeoff`（ローカル pickle キャッシュ利用・本番 Egress ゼロ）。

### 11.8 将来エンハンス

- ~~同 OOF 検証を M-1 にも結線~~ → #272 で対応済み（§9.11）。線形（M-1）vs 非線形（M-2）の
  予測力を同一指標（rank-IC 等）で直接対比可能に。
- inner-CV グリッド / optuna によるハイパラ自動探索 → #264〜#267 でwalk-forward OOF
  rank-IC を目的関数とする共有探索基盤（`plugins/tuning.py` + `hyperparameter_search.py`）
  を実装（M-1/M-2/M-3 共通）。**劣化防止は保存値との比較ではなく候補プールへの champion 投入で
  担う**（#590・ADR-0047）＝永続化済みの `objective_value` は「そのとき存在したパネルでの値」で
  あり、パネルは毎晩伸びるので月をまたいだ比較が成立しない（実測: 0.5068=10 fold /
  0.2614=11 fold / 0.0221=55 fold＝**fold が少ない候補ほど高く出る**・ADR-0045 と同型）。
  本番 params を投入すれば `best >= champion` が構造的に成立するので persist は常に成功し
  （exit 0）、水準の移動は WARNING と `plugin_tuned_params` の `prev_objective_value` /
  `champion_objective_value` / `n_periods` / `n_oof_samples` に残る。M-2 の探索空間は XGBoost 7軸（木構造・正則化）＋
  モメンタム2軸（`use_momentum`/`momentum_window`・候補窓 [3,6,12,18,24] は M-1 と同一・
  `momentum_window` は `use_momentum=True` のときのみ展開）＋符号事前知識1軸
  <br>**このモメンタム2軸は母集団を動かす軸で、目的関数の中では母集団効果と交絡している**
  （[ADR-0050](adr/0050-hyperparameter-search-must-align-populations.md)）。窓を伸ばすほど
  warmup で行が落ち、当てにくい銘柄が消えてスコアが上がるため、探索は毎月 `momentum_window=18`
  を選び続ける。共通 (ym,ec) 域で測り直すと **M-1 +0.0039（p=0.757）・M-2 −0.0072（符号反転）**
  で、20検定のうち基準を上回って補正後 α を通ったものは 0件（唯一通ったのは M-2 窓24 の
  **悪化** −0.0264・p=0.001）。測定入口は
  `python -m scripts.momentum_gate --models risk_return,xgb_m2 --windows 3,6,12,18,24`
  （`use_monotone_constraints`・#366・§11.4.1）の10軸
- ~~quantile regression（`reg:quantileerror`）による予測区間（R1' 代替）~~ → #365 で**分割コンフォーマル区間**として対応済み（§11.7.1・ADR-0020）。quantile regression（再学習要）ではなく OOF 残差ベースのコンフォーマル（再学習不要・family-wide・被覆保証）を採用
- ~~SHAP interaction values（特徴量ペアの交互作用可視化）~~ → #371 で対応済み（§11.5・`feature_interactions`）。あわせて signed SHAP（`feature_coefs_signed`）＋学習方向 corr（`feature_shap_dir`）も追加
- M-2 初心者向けガイド（`M2_MACRO_GBDT_GUIDE.md`）

### 11.9 参考文献

- **Chen, T. & Guestrin, C. (2016)**. "XGBoost: A Scalable Tree Boosting System." *Proceedings of the 22nd ACM SIGKDD*, pp. 785–794. → https://doi.org/10.1145/2939672.2939785
- **Lundberg, S.M. & Lee, S.-I. (2017)**. "A Unified Approach to Interpreting Model Predictions." *Advances in Neural Information Processing Systems*, 30. → https://arxiv.org/abs/1705.07874
- **Lei, J., G'Sell, M., Rinaldo, A., Tibshirani, R.J. & Wasserman, L. (2018)**. "Distribution-Free Predictive Inference for Regression." *Journal of the American Statistical Association*, 113(523), 1094–1111. → https://doi.org/10.1080/01621459.2017.1307116（§11.7.1 分割コンフォーマル区間）
- **Koenker, R. & Bassett, G. (1978)**. "Regression Quantiles." *Econometrica*, 46(1), 33–50. → https://doi.org/10.2307/1913643（§11.7.1 分位のノンパラ近似）

---

## 12. M-3 ベイズ状態空間モデル（時変マクロβ DLM・macro_dlm）

`plugins/macro_dlm.py` / `MacroDlmPlugin`  
**カテゴリ**: ③ 将来リターンを予測（`ui_order=360`、`heavy=True`）

### 12.1 概要

M-1（§9・OLS 線形）・M-2（§11・XGBoost 非線形）の**第3の兄弟**。両者が「ある時点でクロスセクションに係数を1つ推定する（**静的係数**）」のに対し、M-3 は**係数そのものが時間とともに変動する**ベイズ動的線形モデル（DLM）を、**銘柄ごとに独立**なカルマン型逐次ベイズ更新で推定する。係数の時間変化そのものを捉え、マクロ感応度 β の推移と期待リターン α の現在水準を、**信用区間つき**で提示する。

M-1/M-2 と同じ「リターン予測ファミリ」に属し、最新の潜在アルファ α_T を年率化したものを期待リターン µ̂ としてランキングに用いる。

### 12.2 設計決定

| 決定 | 内容 |
|---|---|
| 構造 | **銘柄別 TVP 時系列**（per-stock）。各銘柄を独立に推定し、銘柄固有の時変マクロ感応度を得る |
| 観測 | **週次リターン × マクロ週次変化**（同時点ファクタ応答 / APT 型）。指数/FX/商品/閲覧数は対数リターン、金利・ニューストーンは週次差分（正負を跨ぐ系列は対数比が定義できないため） |
| ファクター | **マクロ + 市場ファクター**（既定は全選択肢）。市場（日経225）を含めると α は市場・マクロ調整後の**固有アルファ**。#250 で **TOPIX（広範市場・1306.T 連動・`dlm_topix`）を選択肢に追加**。**#358（ADR-0013）でコモディティ8系列（`dlm_bcom`/`dlm_copper`/`dlm_natgas`/`dlm_silver`/`dlm_wheat`/`dlm_corn`/`dlm_soybean`/`dlm_platinum`・全て週次対数リターン）を追加**し、銘柄別の時変βで「銘柄×特定コモディティ」の感応度を直接推定する（状態次元 +8）。日次先物のため ADR-0012 の週次高頻度要件に適合。日次の日本10年金利は **財務省「国債金利情報」CSV（`JP10Y_MOF`・日次・1986年〜・PDL1.0）へ差し替え済み**（#458・ADR-0029）。長らく Yahoo `^JGB` 廃止を理由に月次 FRED を据え置いており、**週次差分の 76.89% がゼロ**だった（#456 実測・日次の `dlm_us10y` は 0.40%）が、日次化でゼロ率は **0.91%** になり ADR-0012 の唯一の例外が消えた。**ただし `dlm_jp10y` は既定から外した**——日次化した状態の leave-out で 2 検定とも負方向、売り側 spread が補正後 α を通ったため（rank-IC −0.0005・p=0.076 ns ／売り側 −0.0001・**p=0.023 < α=0.025**・3,982社67期964,466 OOF）。月次版のとき（#456）は 2 検定とも完全に非有意（p=0.695 / 0.206）だったのは、ほぼ何もしない列だったからで、**日次化して初めて実質的に効くようになった結果その効きが負だった**。選択肢としては残る。**#409 でニューストーン／関心度5系列（`dlm_news_tone`/`dlm_news_econ_tone`＝週次差分、`dlm_news_econ_vol`/`dlm_wiki_market_attn`/`dlm_wiki_macro_attn`＝週次対数リターン）を選択肢に追加したが、昇格ゲート実測が2検定とも非有意のため既定には入れない**（`_GATE_REJECTED_FEATURES`・ADR-0024 の追記節。**#456 の 67 期パネルでも棄却維持**＝rank-IC diff +0.0004・p=0.165／売り側 −0.0000・p=0.599） |
| 推定エンジン | **自前 割引係数 DLM（West & Harrison 型・numpy）**。割引 δ で状態ノイズを与え銘柄ごとの数値最適化が不要（1パス高速）。観測分散は Normal-Gamma 共役で学習し予測分布は Student-t |
| α のダイナミクス | **ランダムウォーク（ローカルレベル）** α_t = α_{t-1} + ω |
| ユニバース | 全適格銘柄（最低週数を満たすもの）・`heavy=True`（Render では 403・ローカル限定）。モデル非永続・毎回学習 |
| 出力統合 | 初版は API/UI のみ（µ̂ ランキング・β 経路・診断）。producer 化（`macro_dlm_scores`）と売り推奨連携（§10）は将来 Issue |
| 価格行動系特徴量（既定全選択・Issue #317） | `price_features` で選択する銘柄固有の**遅行特徴量**（週次実現ボラ `px_rvol`・出来高z-score `px_volz`・52週高値乖離 `px_high52dev`・4週リバーサル `px_rev4w`）。マクロ列（同時点変化＝ファクター・エクスポージャー）とは意味が異なり、week (t-1) までの情報のみで計算した既知値を y_t の説明変数に追加する。本番データのOOF比較（rank_ic mean +35%・std半減・long_short_spread負→正・hit_rate 0.45→0.59）で一貫した改善を確認しユーザー承認の上で既定全選択化。r_macro（マクロリスク）の共分散計算からはこの列を除外する。**定義は #364 で `macro_snapshots.build_price_features` へ抽出し M-2（§11.3）と共有化**（M-3 は `PRICE_FEATURE_OPTIONS`/`build_price_features` を re-export・従来シンボル互換維持） |

### 12.3 モデル定式化

各銘柄 i を独立に、週 t で（添字 i 省略）:

**観測方程式**: y_t = F_t' θ_t + ν_t,　ν_t ～ N(0, V_t)  
　F_t = [1, Δm_{1,t}, …, Δm_{k,t}]'　（定数項=α 用の 1 ＋ マクロ週次変化）  
　θ_t = [α_t, β_{1,t}, …, β_{k,t}]'　（状態 = 時変アルファ + 時変マクロβ）

**システム方程式**（ローカルレベル＝ランダムウォーク）: θ_t = θ_{t-1} + ω_t,　ω_t ～ N(0, W_t)

- y_t = 週次対数リターン `log(close_last_t / close_last_{t-1})`
- Δm_{j,t} = マクロ因子 j の週次変化（指数/FX/商品＝対数リターン、金利＝差分）
- 価格行動系特徴量を選択した場合、F_t = [1, Δm_{1,t}, …, Δm_{k,t}, p_{1,t-1}, …, p_{m,t-1}]' へ拡張される
  （p_{l,t-1} = 特徴量 l の week (t-1) 時点の値。マクロ列と異なり同時点でなく1期ラグの既知値）。
  θ の対応成分も同様に増える。r_macro は macro 部分（先頭 k 列）のみで計算し、価格行動系の
  係数は `beta_latest` とは別枠の `price_beta_latest`/`path.price_beta` に報告する。

### 12.4 推定（割引フィルタ＋分散学習）

割引係数 δ により W_t を陽に与えず **R_t = C_{t-1}/δ**（事前共分散の割引膨張）として状態ノイズを表現する（West & Harrison）。観測分散 V_t は分散割引 β_v による Normal-Gamma 共役更新でオンライン学習する。1銘柄1フォワードパス:

```
事前:   a_t = m_{t-1},            R_t = C_{t-1}/δ
予測:   f_t = F_t' a_t,           Q_t = F_t' R_t F_t + S_{t-1}   ← 1期先予測（観測前）
更新:   e_t = y_t − f_t,          A_t = R_t F_t / Q_t
        n_t = β_v n_{t-1} + 1,    S_t = S_{t-1}(β_v n_{t-1} + e_t²/Q_t)/n_t
        m_t = a_t + A_t e_t,       C_t = (S_t/S_{t-1})(R_t − A_t A_t' Q_t)
```

予測分布は自由度 n_{t-1} の Student-t（位置 f_t・尺度 Q_t）。状態 θ_t の事後 (m_t, C_t) から α・β の信用区間が解析的に得られる。

### 12.5 出力契約

```json
{
  "model_type": "bayesian_dlm",
  "macro_features": ["dlm_usdjpy", "dlm_us10y", "dlm_nikkei225", "dlm_wti"],
  "factor_labels": {"dlm_usdjpy": "USD/JPY 週次変化", ...},
  "price_features": ["px_rev4w"],
  "price_feature_labels": {"px_rev4w": "4週リバーサル（過去4週リターン）"},
  "params": {"state_discount": 0.98, "var_discount": 0.98, "min_weeks": 104, "burn_in_weeks": 26, "top_n": 50},
  "n_companies": 1234,
  "diagnostics": {"calibration": 1.05, "pred_rmse": 0.021, "coverage95": 0.94, "n_companies_scored": 1234,
                  "dropped_factors": [{"feature": "dlm_jp10y", "label": "日10年金利（財務省・日次）週次差分", "coverage": 0.31}],
                  "factor_coverage": {"dlm_usdjpy": 1.0, "dlm_us10y": 1.0, ...}},
  "results": [
    {"edinet_code": "...", "sec_code": "...", "company_name": "...", "industry": "...",
     "mu": 0.18, "mu_ci": [0.02, 0.34], "alpha_weekly": 0.0035, "n_weeks": 210,
     "pred_rmse": 0.020, "coverage95": 0.95, "r_macro": 0.11,
     "beta_latest": {"dlm_usdjpy": {"mean": 0.42, "lo": 0.10, "hi": 0.74}, ...},
     "price_beta_latest": {"px_rev4w": {"mean": 0.05, "lo": -0.02, "hi": 0.12}},
     "path": {"dates": [...], "alpha": {"mean": [...], "lo": [...], "hi": [...]},
              "beta": {"dlm_usdjpy": {"mean": [...], "lo": [...], "hi": [...]}, ...},
              "price_beta": {"px_rev4w": {"mean": [...], "lo": [...], "hi": [...]}}}}
  ]
}
```

µ̂ = 年率化した最新フィルタ α_T（= α_T × 52）。経路（`path`）は payload 抑制のため µ̂ 上位 `top_n` 銘柄のみ・最大 120 点に間引いて添付する。

### 12.6 検証（1期先予測診断）

DLM のフォワードパスは各週 y_t を**観測前**に予測する。標準化予測誤差 e_t/√Q_t（バーンイン除外）から、① **校正**（標準化誤差²の平均・1 が理想＝予測分散が妥当）、② **予測 RMSE**（週次リターン単位）、③ **95% 信用区間カバレッジ**を全銘柄平均で集計する。M-1/M-2 の walk-forward CV とは別枠の、状態空間モデルに固有の自己診断。

### 12.7 制約・限界

- **同時点回帰**: β はマクロ「ショックへの感応度」であり予測子ではない。µ̂ は潜在 α（マクロで説明されない持続的ドリフト）に由来する。将来のマクロ変化は未知のため µ̂ ≈ α_T として扱う。
- **週次・最低履歴**: 既定 104 週（約2年）未満の銘柄は対象外。状態次元 = 1+k に対し週次データ点数が要る。
- **重なりなしの逐次観測**: 欠損週（マクロ整列不能・株価 0）はスキップし、残りを連続観測として扱う（暦上の小さなギャップは無視）。
- **薄い factor の自動除外**: factor を増やすと「全 factor が揃う週」の積集合が狭まり、歴史の浅い／未収集系列を選ぶと多くの週が欠損→企業が一斉脱落する。DLM は観測ベクトルが完全である必要があり M-2 のような NaN 許容は使えないため、**週次日付グリッドでのカバレッジが `_MIN_FACTOR_COVERAGE`（既定 0.5）未満の factor をモデルから自動除外**し企業母集団を factor 選択から切り離す。除外内容は `diagnostics.dropped_factors`、全選択 factor のカバレッジは `diagnostics.factor_coverage` に出力（UI の診断ボックスに警告表示）。全 factor が閾値未満なら factor 名入りの明確なエラー。
- **割引係数は実行内で固定**: δ・β_v はスライダー指定（既定値は OOF rank-IC チューニングの適用値。`hyperparameter_search.py --persist` → `/api/plugins/macro_dlm/tuned` で UI へ注入）。周辺尤度最大化による in-UI 自動選択（旧 `auto_hyperparams`）は実装後、①既定値が既に OOF 最適で上書きが劣化リスク ②目的関数不一致（in-sample 周辺尤度 ≠ OOF rank-IC）を理由に撤去（ADR-0007 改訂）。
- 既存マクロ特徴量（M-1/M-2 の水準 YoY/Zスコア）とは別物の**週次変化**を使う（`_DLM_MACRO_MAP`）。
- **週次高頻度ファクター専用（by-design・ADR-0012・Issue #310）**: M-1/M-2 が持つ月次以下のマクロ系列（JP 実体経済・物価・マネー・サーベイ・OECD CLI・IMF WEO 等）は M-3 に組み込まない。観測が「週次変化」のため、forward-fill で月内定数になる低頻度系列は週次変化が大半ゼロ＝情報量が乏しく、`_MIN_FACTOR_COVERAGE` の自動除外も効かない（ffill 後カバレッジ ≈ 1.0）。月次以下のマクロは M-1/M-2 のスナップショット方式が担当する。**例外はもう無い**——唯一の例外だった `dlm_jp10y`（月次 FRED）は財務省の日次 CSV へ差し替え、既定 21 ファクターは全て日次ソースになった（#458・ADR-0029 が ADR-0012 Decision 2 を supersede）。
- **価格行動系特徴量は既定全選択（Issue #317・2026-07-12 本番データで検証済み）**: `px_high52dev`（52週窓）等は窓の立ち上がり分だけ銘柄あたりの使用可能週数が減る（対象企業数は実測 3,641→3,546社・約2.6%減）というコストと引き換えに、OOF全指標（rank_ic mean/std・long_short_spread・hit_rate）が一貫して改善することを確認した上での採用。今後 individual ablation（4特徴量のどれが寄与しているか）は未検証で、悪化が疑われる場合は `price_features` を個別に絞って再検証すること。

### 12.8 将来エンハンス

- walk-forward CV による M-1/M-2 との µ̂ ランキング横並び比較（OOF・§11.7 と同枠）
- 系統的マクロリスク R_macro,i(t) を効用 U = µ − λR の軸へ正式組込み（現状は表示のみ）
- M-3 初心者向けガイド（`M3_STATE_SPACE_GUIDE.md`）

### 12.9 参考文献

- **West, M. & Harrison, J. (1997)**. *Bayesian Forecasting and Dynamic Models*, 2nd ed. Springer. → https://doi.org/10.1007/b98971
- **Kalman, R. E. (1960)**. "A New Approach to Linear Filtering and Prediction Problems." *Journal of Basic Engineering*, 82(1), 35–45. → https://doi.org/10.1115/1.3662552
- **Ross, S. A. (1976)**. "The Arbitrage Theory of Capital Asset Pricing." *Journal of Economic Theory*, 13(3), 341–360. → https://doi.org/10.1016/0022-0531(76)90046-6

---

## 13. M-4 兄弟μ̂スタッキング・アンサンブル（macro_ensemble）

カテゴリ: ③ 将来リターンを予測（`ui_order=370`・`heavy=True`・ローカル実行専用）。Issue #367・ADR-0015。

> **退役（2026-08-30・#570・[ADR-0044](adr/0044-retire-underperforming-models-by-hiding.md)）**:
> `hidden=True` によりサイドバーと `mu_source` の選択肢から外した。統合が **M-6 単体を
> 上回らない**（rank-IC +0.0006・p=0.810／売り側 spread p=0.655）ことが本節 §13.4 の
> 「上回らなければ単体で十分」判定にそのまま該当する一方、実行コストは基底
> M-1+M-2+M-6 の合算のままだったため。**プラグイン本体・テスト・`model_comparison` の
> 比較行（M-4）は残してある**ので、`POST /api/plugins/macro_ensemble/run` と
> `python -m scripts.model_comparison_run --models macro_ensemble,macro_enet` で
> いつでも測り直せる。基底構成を変えたら再評価すること（ADR-0015 追記）。

### 13.1 概要

M-1（線形 OLS+BIC）・M-2（非線形 XGBoost）・M-6（正則化線形 ElasticNet・#397 で追加）の **OOF 予測 μ̂ を統合するメタモデル**（Wolpert 1992 / Breiman 1996 のスタッキング）。予測誤差が低相関な基底を、制約付き（非負・和1）の加重で相殺でき単体を超えうる。重みは NNLS で学習するため**効かない基底は自動的に重み ~0 へ落ちる**＝基底追加の下振れリスクが構造的に小さい。**M-3 は除外**（週次専用・ADR-0012 で目的頻度と母集団が異なる＝論証された非適用）、**M-5 も除外**（順位スコアで水準を持たない・ADR-0017）。

前提（#375・ADR-0014）: purge/embargo 後の honest 評価で M-2 rank-IC は 0.33→0.14 となり M-1 と拮抗。統合判定の基準も **honest 値で max(M-1, M-2) を上回るか**。

### 13.2 アルゴリズム（二段ウォークフォワード）

1. **基底 OOF の自前再現**: 各基底の `execute()` は per-(ym,銘柄) OOF を露出しないため、M-4 が各モデルの既定 config で `build_snapshots(return_stock_ids=True)` → M-1 は BIC+OLS・M-2 は `_make_xgb_fit_predict`・M-6 は `make_elasticnet_fit_predict` を注入して `walk_forward_cv_monthly(return_residuals=True, embargo_months=12)` を回す。`stock_ids_by_ym[ym][k] ↔ residuals_by_ym[ym][k]` の順序保証で (ym, edinet_code, yhat, y) を突合。レグの組み立ては `BASE_MODELS` 駆動で、**M-2/M-6 は build 契約が同じ**（交差項なし・macro_nan_ok）ため config 同値ならスナップショットを共有する（`_same_build_config`・既定では常に同値＝二重構築なし）。
2. **母集団**: M-1（strict）∩ M-2（nan_ok）∩ M-6 の **(ym, edinet_code) intersection**（全基底が予測できる銘柄）。
3. **二段目（無リーク）**: 月 t の統合重みは t より前の月の共通 OOF ペアだけで学習（expanding・`_stack_walk_forward`）。学習前は等重み。基底 μ̂ が embargo=12 済み OOF のため二段目もリークしない。
4. **重み**: 既定 `rank_ic_grid`（期内平均 Spearman 最大化・**n 次元シンプレックス格子** `_simplex_grid` を走査。格子点数 C(steps+n−1, n−1) が上限を超えると刻みを自動で粗くする）。代替 `nnls`（`scipy.optimize.nnls`→和1正規化・和0は等重み）/ `equal`。**既定が rank-IC 最大化なのは二段目の学習目的を評価指標へ揃えるため**——#397 の本番実測で、MSE 最小化の NNLS は縮小推定で予測分散の小さい M-6 を重み 0 で捨て、OOF の改善が現在μ̂（producer）へ反映されない非整合が出た（ADR-0007「周辺尤度 ≠ OOF rank-IC」と同型。OOF 性能自体は両者誤差レベル・p=0.326）。
5. **評価**: 統合残差 `{t:[(ŷ_stack, y)]}` → 共有 `oof_backtest`（§9.11/§11.7 と同一指標・同一 honest 前提）→ `model_comparison` に M-4 として並ぶ。
6. **現在μ̂（producer）**: M-1 `_fit_final`+`_score_companies`・M-2 全データ最終 XGB・M-6 `_fit_final_and_score` の現在μ̂を intersection し、全共通 OOF で学習した最終重み `w_final` を適用（`w_final` は現在μ̂専用・OOF に使い回さない）。結果行には基底ごとの内訳 `mu_m1`/`mu_m2`/`mu_m6` を併記する。`macro_ensemble_scores` へ全置換永続化し、売り候補ランキング（§10）の `mu_source="macro_ensemble"` で利用可能。

### 13.3 パラメータ

`weight_method`（rank_ic_grid/nnls/equal・**既定 rank_ic_grid**・#397）・`min_meta_months`（重み学習開始の最小過去OOF月数・既定2）・`grid_step`（rank_ic_grid の刻み・既定0.05）・`n_quantiles`（既定5）・`top_n`。統合対象 `BASE_MODELS` は定数（UI 非露出）。`tuning_search_space` は持たない（サブモデルは既定固定）。

### 13.4 仮定・限界

- 実行コスト ≈ M-1+M-2+M-6 の合算（snapshot キャッシュキーが `return_stock_ids` で分岐し CV は再計算。M-2/M-6 は build を共有するため増分は ElasticNet の CV のみ）。
- intersection でレグ母集団の狭い側に律速される。`n_common_pairs` を出力し監視。**2026-08-01 実測（`python -m scripts.measure_strict_binding`・Issue #411 / ADR-0025）では狭い側は M-1（strict）ではなく M-2 契約**: 履歴延伸後で M-1 build 71ヶ月/173,836 サンプルに対し M-2 build は 67ヶ月/91,482（価格特徴 `px_high52dev` の52週 warmup 等。延伸前は 47ヶ月/111,210 対 43ヶ月/57,955 で同じ非対称）。「M-1 strict が共通域を狭める」は誤り。基底を増やすと共通域はさらに狭まりうるため、**優劣判定は必ず `base_oof_backtest`（同一共通域に制限した各基底の OOF）と比較する**（ADR-0015 の base-on-common）。
- 統合が単体最良を上回るかは実データ次第（上回らなければ「単体で十分」が確定知見・ADR-0015 に実測を記録）。基底の増減は `BASE_MODELS` 定数の変更だけで済み、`scripts/ensemble_base_bakeoff.py` が同一 honest 前提で構成間の OOF を横並び実測する（#397）。

### 13.5 参考文献

- **Wolpert, D. H. (1992)**. "Stacked Generalization." *Neural Networks*, 5(2), 241–259. → https://doi.org/10.1016/S0893-6080(05)80023-1
- **Breiman, L. (1996)**. "Stacked Regressions." *Machine Learning*, 24, 49–64. → https://doi.org/10.1007/BF00117832

---

## 14. M-5 マクロ×財務 ランク学習（learning-to-rank・macro_gbdt_rank）

カテゴリ: ③ 将来リターンを予測（`ui_order=380`・`heavy=True`・ローカル実行専用）。Issue #362・ADR-0017。

> **退役（2026-08-30・#570・[ADR-0044](adr/0044-retire-underperforming-models-by-hiding.md)）**:
> ADR-0017 が約束したまま未実施だった実測をようやく取り、**M-2(MSE) に有意に劣後**した
> （rank-IC **0.0808 vs 0.1578**・差 −0.0771・95%CI [−0.0995, −0.0558]・p=0.001／17期・
> OOF 25,738ペア）。ADR-0017 の「上回らなければ MSE で十分を確定」に該当するため
> `hidden=True` でサイドバーから外した。producer を持たないため下流の切断は不要。
> **プラグイン・テスト・`model_comparison` の比較行（M-5）は残す**——再挑戦する価値が
> あるとすれば、まず early_stopping の非対称（§14.4）を潰してから測り直すこと。

### 14.1 概要

M-2（macro_gbdt）の **rank-IC 整合版**。M-2 が **MSE 最小化**（`reg:squarederror`）で学習する一方、評価・ハイパラ探索・VISION 比較はすべて **期内クロスセクション Spearman rank-IC**。この「学習目的 ≠ 評価指標」不一致（ADR-0007 が `auto_hyperparams` を撤去した理由「周辺尤度 ≠ OOF rank-IC」と同型）を、M-2 自身が学習側に抱えていた。MSE 最小化は期内クロスセクション順位の最適化を保証せず、外れリターンに MSE が引きずられて順位が歪む。M-5 は XGBoost の **learning-to-rank 目的**（`rank:pairwise` 既定）で **各 test 月を1クエリグループ**として期内順位を直接最適化する（Burges 2010）。

M-2 を無改変のベースラインとして残すため **新兄弟モデル（M-5）** として追加した。`execute()` 本体は `MacroGbdtPlugin` から丸ごと継承し、4フック（`_objective` / `_make_cv_callback` / `_fit_final_model` / `_persist_producer`）＋ `_model_type` / `params_schema` のみ override する（DRY・同一 fold／同一特徴量で M-2 と純比較）。

### 14.2 アルゴリズム

1. **月クエリグループの復元**: `walk_forward_cv_monthly` は学習月を月横断で flat 化し月境界を落とす。`pass_train_groups=True`（`utils.py`・#362 の後方互換な最小拡張）で各学習月のサンプル数配列を fit_predict へ渡し、`XGBRanker.fit(group=...)` の **各月=1クエリグループ**を復元する（既定 False では従来の2引数呼び出しで M-1/M-2/M-3 は不変）。
2. **ラベル**: `rank:pairwise` は順序のみ使うため生の 52 週先対数リターンをそのまま渡す（負値可）。`rank:ndcg` は非負の段階的関連度を要求し 2^rel ゲインが発散するため、各クエリグループ内で `_NDCG_GRADES`（=16）段の分位グレード（0..K-1）へ変換する（順序は保存）。
3. **学習**: early_stopping は使わず固定 `n_estimators` で学習する（ランカーの eval_set は group 付き検証が必要で walk-forward の1テスト月では成立しにくいため・初版は単純化）。
4. **評価**: 予測は**順位スコア**（リターン単位でない）。共有 `oof_backtest`（§9.11／§11.7 と同一指標・同一 honest 前提）に投入し、Spearman rank-IC・分位リターン・ロングショート spread・hit-rate を算出。`model_comparison` に M-5 として並び、M-2(MSE) と同一 fold・同一特徴量で純比較する。
5. **producer なし**: 順位スコアはリターン単位でないため producer を持たない（`produced_output=False` / `read_producer_scores={}` / `_persist_producer` は no-op）。初版は **OOF 比較専用**とし、下流 `sell_ranking`（`mu_source`）統合は「順位→分位期待リターン写像」を別途定義するまで見送る。

### 14.3 パラメータ

M-2 のスキーマを継承（リスク-リターン幾何・財務/マクロ/モメンタム特徴量・XGB ハイパラ）＋ `objective`（select・`rank:pairwise` 既定 / `rank:ndcg`）。`coerce_params` の membership 検証で `reg:squarederror` 等は reject。`tuning_search_space` は M-2 から継承（XGB 7軸＋モメンタム2軸）。

### 14.4 仮定・限界

- 実行コスト ≈ M-2 と同等（同一 CV 骨格・SHAP は XGBRanker でも算出可）。
- 予測は順位のみで**水準を持たない**ため、期待リターン水準が要る用途（分位期待値・効用計算）には未対応（producer 見送りの理由）。
- **実測の結論（2026-08-30・ADR-0017 の実測節）**: rank-IC は **0.0808** で M-2(MSE) の
  **0.1578** を有意に下回った（差 −0.0771・95%CI [−0.0995, −0.0558]・p=0.001・17期／
  OOF 25,738ペア）。**「MSE で十分」が確定知見**。効かなかった箇所は分位の下側に最も強く出て
  おり、最下位分位リターンが 0.0145（M-2）→ 0.0520（M-5）と持ち上がる＝負ける銘柄を下に
  置けていない（売り側 spread も 0.0676→0.0302 と半減）。`sell_ranking` へ統合しなかった
  判断は結果的に正しかったことになる。
- **比較に残る非対称（再挑戦するなら最初に潰すべき点）**: M-5 は early_stopping を使わず固定
  `n_estimators` で学習する（ランカーの eval_set が group 付き検証を要するため・§14.2 の 3）。
  M-2 は early_stopping で実効木数が絞られるので、「同一 fold・同一特徴量」ではあっても
  **「同一の正則化」ではない**。上の差がこの非対称だけで説明できる可能性は排除できていない
  ＝「learning-to-rank が日本株で無効」ではなく「**この実装では**下回った」と読む。

### 14.5 参考文献

- **Burges, C. J. C. (2010)**. "From RankNet to LambdaRank to LambdaMART: An Overview." *Microsoft Research Technical Report* MSR-TR-2010-82. → https://www.microsoft.com/en-us/research/publication/from-ranknet-to-lambdarank-to-lambdamart-an-overview/
- **XGBoost Learning-to-Rank docs** → https://xgboost.readthedocs.io/en/stable/tutorials/learning_to_rank.html

---

## 15. 兄弟モデル候補メニュー（探索枠・model_candidates）

カテゴリ: 分析モデルではなく**モデル選択のための実験装置**（API・UI 非公開・ローカル実行専用）。
Issue #372・ADR-0021。

### 15.1 概要

`walk_forward_cv_monthly(fit_predict=…)` の注入点は**同一 fold・同一特徴量・同一指標**で任意の学習器を
評価できる共有ハーネスである。ここへ差し込むだけの「候補」を `plugins/model_candidates.py` に集約し、
正式兄弟（M-1〜M-5）へ昇格させる前に OOF rank-IC で実証比較する。候補はプラグインではないため
（`plugin` 属性なし＝レジストリ非登録）、API・UI・`model_comparison`・本番 requirements に一切現れない。

実行は `python -m scripts.candidate_bakeoff`。M-2 既定 config（`params_schema` を `coerce_params({})`）で
スナップショットを**1回だけ**構築し、全候補が同じ `samples_by_ym` を共有する。fold は M-2 と完全一致
（`min_train_months=6` / `step_months=3` / `embargo_months=12`＝ADR-0014 の purge 済み honest 前提）。

### 15.2 候補一覧

| 候補 | 位置づけ | 要点 |
|---|---|---|
| `elasticnet` | 正則化線形（L1+L2） | M-1(OLS) と M-2(非線形) の中間。金利カーブ・信用スプレッドのグループ共線性に L2 で頑健な符号付き係数。α・l1_ratio は学習 fold 内 `TimeSeriesSplit` で選択 |
| `extratrees` | バギング非線形＋予測区間 | 葉モーメントの全分散分解 Var ≈ E_t[Var_leaf]+Var_t[mean_leaf]（QRF 相当の軽量版）と木予測の経験分位の**2種**を出力し被覆率を実測 |
| `fama_macbeth` / `fama_macbeth_ridge` | 期別断面回帰の予測ヘッド | 各月の断面回帰 λ_t → NW 補正付き時系列平均 λ̄ で ŷ=Σβ·λ̄（ADR-0008 の資産を予測側へ転用）。**マクロ列は断面定数（＝切片と共線）のため自動除外**され、実質は characteristics モデルになる |
| `regime_linear` | 離散状態×符号解釈 | VIX（無ければ信用スプレッド）の学習 fold 内中央値でストレス/平穏に分割し regime 別 Ridge。標本不足・NaN は pooled へフォールバック |
| `lightgbm` / `catboost` | 代替 GBDT | M-2 と同一ハイパラへ揃え**実装差だけ**を比較（leaf-wise / ordered boosting）。**任意依存**（`requirements-optional.txt`・未導入なら自動スキップ） |
| `xgb_m2` / `ols` | 基準線 | M-2 既定と同一の XGBoost、および素 OLS（正則化なし・BIC 選択なし） |
| `wrap_macro_pca` | 合成可能ラッパー | マクロ列だけを **fold 内 PCA** で直交少数因子へ畳む（`--pca K`）。非マクロ列は無変換で温存。任意の候補に被せられる |

### 15.3 リーク防止の契約

前処理パラメータ（NaN 補完平均・winsorize 境界・正規化統計・PCA 主成分・レジーム閾値・λ̄・α）は
**すべて学習 fold 内で fit** し、テストは同一パラメータで transform するだけ。テストラベルは評価に
しか使わない。`tests/test_model_candidates.py` は「**テストラベルだけ差し替えて予測がビット一致するか**」で
これを直接検証する（受領証ではなく独立検証で確かめる方針）。

例外は Fama-MacBeth のテスト期断面標準化のみ。自期の断面統計（説明変数だけ）を使うが、同時点の
他銘柄の特徴量は運用時にも既知であり、ラベルは一切参照しないため look-ahead ではない。

### 15.4 昇格ゲート

点推定の rank-IC が M-2 を上回るだけでは昇格させない。`model_stats.paired_ic_significance`
（ADR-0018・定常ブートストラップ）で **差が有意**（95% CI が 0 を跨がない）であり、かつ**同時に検定した
候補数で多重比較補正**（Bonferroni・α/候補数）を通ることを条件とする。walk-forward の per-fold IC は
学習窓が重なり系列相関を持ち、fold 数も 10 前後しかないため、点推定の大小はノイズで容易に入れ替わる。

**既定を減らす向きにも同じ基準を課す（#454・ADR-0028）**。昇格ゲートは「候補を足すか否か」の
検定で帰無仮説は「base のまま」に置かれる。既定入りの特徴量へ同じ検定を当てると帰無が反転し
「残す根拠」を要求する形になるため、`scripts/macro_feature_bakeoff.py` が出す
`keep as option only` を既定からの除外と読み替えてはならない。**除外にも補正後 α を通る実測を
要する**（#358 の「全選択肢を既定 ON・過剰選択は BIC/正則化が抑える」方針を、検出力の低い
非有意結果で崩さないため）。同様に、**棄却の理由は「符号が負」ではなく「補正後 α を通らない
こと」に置く**——データ世代が変われば符号構成は入れ替わる（#451 の鉱工業指数が実例）。

**世代をまたぐ絶対値比較の注意**: #447 の `lag_days` 是正（月次7系列の先読み除去）で、既定
マクロを固定したまま **M-2 の rank-IC が +0.1332→+0.1285（−0.0047）・売り側 spread が
+0.0522→+0.0481（−0.0041）** へ動いた（M-6 は rank-IC +0.1725→+0.1742 とほぼ不変）。
**先読みは非線形の M-2 の成績を約 0.005 ぶん良く見せていた**。是正前に測った rank-IC の絶対値を
是正後の数値と横並びで比較しないこと（下の §15.5 の表を含む）。

### 15.5 実測サマリ（2026-07-26・詳細は ADR-0021）

> **注**: 本表は `lag_days` 是正前（#447 以前）・かつ学習窓延伸前（#411 以前）の 43ヶ月パネルでの
> 実測。モデル間の**相対順位**の記録として読み、絶対値を是正後の数値と直接比較しない（§15.4 末尾）。

本番パネル（43ヶ月 / 57,955 サンプル / 71 特徴量 / 9 fold）の honest OOF rank-IC:

| 候補 | rank-IC | vs M-2 の差（95%CI・p） | 判定 |
|---|---|---|---|
| **elasticnet** | **0.1713** | +0.0294 [+0.0116, +0.0469]・p=0.002 | **M-6 へ昇格** |
| fama_macbeth_ridge | 0.1653 | +0.0232・p=0.042 | 多重比較補正で不通過 |
| extratrees | 0.1649 | +0.0230・p=0.080 | 非有意 |
| regime_linear | 0.1627 | +0.0208・p=0.058 | 非有意 |
| ols（基準線） | 0.1554 | +0.0134・p=0.104 | 非有意 |
| catboost | 0.1523 | +0.0103・p=0.032 | 補正で不通過（効果量も僅少） |
| lightgbm | 0.1474 | +0.0055・p=0.043 | 同上 |
| xgb_m2（M-2 基準線） | 0.1419 | — | — |
| fama_macbeth（断面OLS） | −0.0131 | −0.1550・p=0.001 | **有意に劣位** |

確定した知見（ADR-0021 に詳述）:

1. **木の非線形性より、グループ共線性への縮小推定のほうが効く**。GBDT 族（XGBoost/LightGBM/
   CatBoost）はいずれも線形族（ElasticNet/素 OLS）を上回らなかった。
2. **代替 GBDT は XGBoost をわずかに上回るが実務的には誤差**（+0.006〜+0.010）。XGBoost 採用を
   変える理由はない＝Issue #372 の「代替GBDT を試す」は決着。任意依存のまま据え置く。
3. **Fama-MacBeth は第1段階の正則化が必須**。素の断面 OLS は相関の強い財務特徴群で係数が発散し
   rank-IC が負（−0.013・分位が逆順）になる。Ridge に替えるだけで 0.165 へ跳ね上がる。
4. **マクロ fold 内 PCA は効果なし**（5 主成分で分散 90.9% を説明しても IC 不変・木では悪化）。
5. **regime-switch 閾値線形は pooled 線形を超えない**（標本半減の不利を上回る改善が出ない）。
6. **木予測の経験分布による区間は予測区間として使えない**（名目 80% に対し実測被覆 40.6%）。
   分割コンフォーマル（ADR-0020）が τ=0.9 に対し 87〜89% で一貫しており、R1' はそちらが正解。

---

## 16. M-6 マクロ×財務 正則化線形（ElasticNet・macro_enet）

カテゴリ: ③ 将来リターンを予測（`ui_order=390`・`heavy=True`・ローカル実行専用）。Issue #372・ADR-0021。

### 16.1 概要

M-2（macro_gbdt・XGBoost）の**線形兄弟**。§15 の候補メニューで実測した結果、M-2 を **honest OOF
rank-IC で有意に上回った**ため正式兄弟へ昇格した（8 候補中で唯一、多重比較補正 α/8 を通過）。

| 指標 | M-6 ElasticNet | M-2 XGBoost |
|---|---|---|
| rank-IC（mean / std） | **0.1713** / 0.1171 | 0.1419 / 0.1117 |
| 業種中立 rank-IC | 0.1398 | 0.1208 |
| ロングショート spread | 0.1359 | 0.1117 |
| 実効ターンオーバー | 0.296 | 0.520 |
| ブレークイーブンコスト | 22.9bp | 10.7bp |
| 差の検定（vs M-2） | **+0.0294・95%CI [+0.0116, +0.0469]・p=0.002** | — |

（本番パネル 57,955 サンプル／43ヶ月／71特徴量／9 fold・embargo=12。詳細は ADR-0021）

**知見**: 低 S/N な日本株リターン予測では、木の非線形性より**グループ共線性に対する縮小推定**
（金利カーブ us5y/10y/30y・信用スプレッド・株価指数・コモディティが各々ブロックを成す）のほうが効く。
素の OLS（正則化も特徴選択もなし）は 0.1554 で、M-6 との差 0.016 が正則化の寄与にあたる。

### 16.2 アルゴリズム

1. **スナップショット**: M-2 と同一（`build_snapshots(build_interactions=False, macro_nan_ok=True)`）。
2. **前処理**（学習 fold 内でのみ fit）: 列平均で NaN 補完 → p1-p99 winsorize → zscore
   （`fit_feature_columns`）。y も winsorize→zscore し、予測は元スケールへ逆変換する。
3. **学習**: `ElasticNetCV`。α と l1_ratio は**学習 fold 内の `TimeSeriesSplit`**（過去→直近の向き）で
   選択する。ランダム K-fold は期間をシャッフルして楽観バイアスを生むため使わない。
   探索設定（l1_ratios 0.1/0.5/0.9・α パス 20 点・CV 3 分割・`max_iter=50000`）は
   `model_candidates._EN_*` が**単一ソース**で、M-6 の CV・最終学習と M-4 が同じ値を共有する。
   `max_iter` は #452 で 5000 → 50000 へ較正した。5000 では α パス末端が収束せず
   `ConvergenceWarning` が出る（本番パネル 91,482 サンプル・67ヶ月・78特徴量の実測で
   walk-forward 17 fold 中 10 件 + 最終学習 1 件）。50000 では**警告 0 件のまま指標も所要も不変**
   ——rank-IC 0.1663 / `short_side_spread` 0.070221 / ターンオーバー 0.340073 が完全一致
   （per-fold rank-IC の最大差 9.7e-5）、最終学習の μ̂・係数はビット一致、所要は
   CV 288.5→286.7 秒・最終学習 36.2→36.9 秒。未収束が起きるのは **CV が選ばない極小 α** だけで、
   そこは係数がほぼ飽和しているため追加反復が安い。α パス下限（`eps`）を切り上げれば警告は
   同様に消えるが、選択される α・l1_ratio 自体が変わりモデルが別物になるため採らない。
4. **CV**: `walk_forward_cv_monthly(min_train_months=6, step_months=3, embargo_months=12)`＝M-2 と同値。
   注入する fit_predict は候補実装（`model_candidates.make_elasticnet_fit_predict`）**そのもの**で、
   ADR-0021 の実測値と本プラグインの OOF が同一コードパスであることを保証する。
5. **解釈**: 最終モデルの符号付き係数を `feature_coefs`（M-2 の mean|SHAP| と同じスロット）へ載せる。
   L1 でゼロになった特徴量は係数 0＝「使われなかった」がそのまま読める。

### 16.3 パラメータ

財務/マクロ/モメンタム/px_* 特徴量・`min_coverage`・`top_n` は M-2 と同一契約。加えて:

- `l1_ratio`（select・既定 `auto`）: 探索範囲を 0.1/0.5/0.9 の自動選択（既定）／Ridge 寄り固定／
  均等固定／Lasso 寄り固定から選ぶ。α は常に学習 fold 内 CV が決めるため露出しない。

**マクロ fold 内 PCA（§15 の改善案④）は載せていない**。同じ bake-off で実測したが、5 主成分で
マクロ分散の 90.9% を説明しても rank-IC は 0.1713→0.1714 と不変、ターンオーバー（0.296→0.289）・
ブレークイーブン（22.9→23.2bp）も横ばいで、木モデルではむしろ悪化した（LightGBM 0.1474→0.1379）。
昇格ゲートを通らなかったため本番モデルには入れず、ラッパー自体は探索枠に残している。

### 16.4 仮定・限界

- **producer あり**（#396 で追加）。予測は M-1/M-2 と同じ 52 週先対数リターン単位のため、
  `macro_enet_scores`（`macro_gbdt_scores` と同型・`r1_prime` 付き）へ全置換永続化し、
  `sell_ranking` の `mu_source="macro_enet"` へ供給する。**#402（ADR-0022）で既定 `mu_source`
  は M-2 → M-6 へ切替済み**（売り側 OOF 指標 `short_side_spread` で +0.0145・p=0.001 の有意優位・
  §10.2 の選定根拠を参照）。順位スコアで水準を持たない M-5 と異なり、M-6 は水準を持つため統合できる。
- **μ̂ は夜間バッチが日次更新する**（#443）。M-6 は `tune-hyperparameters.yml` の matrix
  （M-1/M-2/M-3）に入っておらず `--persist-scores` の副作用も受けないため、#443 以前は
  ローカル手動実行だけが `macro_enet_scores` の更新経路だった。現在は `nightly-scores.yml`
  → `nightly_scores.py` の `NIGHTLY_MODELS` に載っており、`daily-incremental` 成功後に
  **params_schema の既定構成のまま**（本節の実測と同一）自動で回る。tuned params は持たない。
- 線形モデルのため、M-2 が捉える fin×macro の高次交互作用は表現できない。両者は**補完関係**とみて
  M-4（兄弟μ̂スタッキング）へ M-6 を加えたが（#397）、統合は M-6 単体を rank-IC（p=0.810）でも
  売り側 spread（p=0.655）でも上回らなかった（ADR-0015 の「単体で十分」判定に該当）。
- `results` は上位 `top_n` 件のみ返す（汎用レンダラが全社数千行の DOM を吐かないようにするため）。

### 16.5 参考文献

- **Zou, H. & Hastie, T. (2005)**. "Regularization and variable selection via the elastic net."
  *JRSS-B* 67(2), 301-320. DOI:10.1111/j.1467-9868.2005.00503.x
- **Politis, D. N. & Romano, J. P. (1994)**. "The Stationary Bootstrap." *JASA* 89(428), 1303-1313.
  DOI:10.1080/01621459.1994.10476870（昇格判定に使った有意差検定）

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
| 2026-06-25 | **M-2（マクロ×財務 勾配ブースティング）を §10 として追加**（#234・ADR-0003）。M-1 と同一スナップショット母集団（build_interactions=False）・同一リスク-リターン幾何を共有する非線形兄弟。共有ビルダーを macro_snapshots.py に集約し M-2→M-1 結合をゼロ化。walk_forward_cv_monthly に fit_predict コールバックを注入して同一 fold で XGBoost/OLS を比較。SHAP でグローバル mean\|SHAP\|（feature_coefs スロット）＋per-stock 寄与を全社返却。R1 なし（ADR-0003 §5）。xgboost-3.3.0 / shap-0.52.0 を requirements.txt に完全 pin |
| 2026-06-26 | **M-3（ベイズ状態空間モデル・時変マクロβ DLM）を §12 として追加**。M-1/M-2 の静的係数に対し、係数が時間変動する銘柄別 DLM をカルマン型逐次ベイズ更新で推定。観測=週次リターン×マクロ週次変化（既定 USDJPY/US10Y/NIKKEI225/WTI）。自前 割引係数 DLM（West & Harrison 型・numpy）＋ Normal-Gamma 共役で観測分散を学習し α/β の信用区間を解析的に出力。最新フィルタ α_T を年率化して µ̂ ランキング、β 経路と1期先予測診断（校正/RMSE/カバレッジ）を UI 可視化。初版は API/UI のみ（producer 化・sell_ranking 連携は将来）。新規依存なし（numpy/scipy のみ） |
| 2026-07-01 | **M-3: 薄い factor の自動除外（§12.7）**。factor を増やすと使用可能週の積集合が狭まり、歴史の浅い／未収集 factor で企業が一斉脱落する問題の対処。DLM は観測ベクトル完全性が必須で NaN 許容不可のため、カバレッジ `_MIN_FACTOR_COVERAGE`（既定0.5）未満の factor をモデルから自動除外し企業母集団を factor 選択から切り離す。`diagnostics.dropped_factors`/`factor_coverage` を出力（UI 診断ボックスに警告）。全除外時は factor 名入りエラー |
| 2026-06-30 | **M-2: マクロ欠損 NaN 許容（§11.3.1）**。カバレッジの薄いマクロ系列を選ぶと現在スナップショット日付の企業が一斉脱落し表示企業数が激減する問題の根本対策。`build_snapshots(macro_nan_ok=True)` でマクロ欠損を NaN として保持（XGBoost がネイティブ処理）→ 表示母集団を財務＋株価で決定（マクロ選択に非依存）。表示可否は `min_coverage` が制御。内蔵 OLS ベースライン用に `fit_feature_columns`/`transform_feature_row` を学習フォールド列平均で NaN 補完（リークなし）。財務特徴量の厳格除外・M-1（OLS）の挙動は不変 |
| 2026-07-05 | **M-1〜M-3: R_macro 未蓄積時のサイレント空表示を解消（#273）**。macro_beta 推論バッチ未実行（M-1/M-2）や共分散推定失敗（M-3）で r_macro が全社 None のとき、従来はグラフ・表が理由不明のまま空表示になっていた。`execute` レスポンスに `r_macro_available` フラグを追加し、①M-1/M-2 は risk_axis セレクトの「R_macro」選択肢を無効化（自動的に R2 へフォールバック）、②3モデル共通でランキング表・散布図に「macro_beta 推論バッチ未実行の可能性」を明示するメッセージを表示。散布図は canvas を破棄せずメッセージと表示/非表示を切替えるため、データ復活後も再描画できる |
| 2026-07-11 | **売り候補ランキング（§10）にマクロ予測型プリセットを追加・既定化**。期待リターン μ とマクロリスク −Rᴹ の2軸のみで判定する `マクロ予測型` プリセットを新設し既定に（スコアは正規化されるため 1.0 : 0.5 の比率が有意）。μ 出所（`mu_source`）の既定を M-1→**M-2（macro_gbdt）**へ変更。観点ウェイトのスライダーグリッドは既定で `<details>` 折りたたみ表示に。両シグナルとも選択 μ モデルの実行に依存するため、未実行時は全保有が「データ不足」になる（§10.5・UI 注記で明示） |
| 2026-07-11 | **モデル比較（OOF）ビューを追加**（サイドバー「④ 戦略を検証」・`POST /api/backtest/model-comparison`・`model_comparison.py`）。将来リターン予測 M-1/M-2/M-3 の予測力（rank-IC・ロングショート spread・hit-rate）を無リーク walk-forward OOF で横並び比較する。各モデルの `execute()` が既に返す `oof_backtest` を集約するだけ（追加学習なし）。`tuning_objective_only()`＋`tuning_dry_run()` で重い全社スコアリングを省き producer 永続化も抑止（副作用なし）。3モデルとも heavy＝ローカル実行専用（Render では各モデル `heavy_render` でスキップ）。`/api/backtest`（as-of 上位N の実現リターン）とは別手法 |
| 2026-07-23 | **M-4（兄弟μ̂スタッキング・アンサンブル）を §13 として追加**（#367・ADR-0015）。M-1+M-2 の OOF μ̂ を (ym,銘柄) intersection で整列し、二段ウォークフォワード（月 t の重みは t 未満の共通 OOF だけで学習・NNLS 非負和1）で統合。honest（embargo=12・ADR-0014）前提の `oof_backtest` を返し `model_comparison` に M-4 として並ぶ。現在μ̂は `macro_ensemble_scores` へ producer 永続化し売り候補ランキングの `mu_source` に追加。初版は M-3 除外（週次専用・ADR-0012） |
| 2026-07-24 | **M-5（マクロ×財務 ランク学習・learning-to-rank）を §14 として追加**（#362・ADR-0017）。M-2 の rank-IC 整合版。学習目的を MSE（reg:squarederror）→ XGBoost の learning-to-rank（rank:pairwise 既定）へ差し替え、各 test 月を1クエリグループとして期内順位を直接最適化する。M-2 を無改変ベースラインとして残すため新兄弟モデル化し、execute() 本体を継承して4フック（_objective/_make_cv_callback/_fit_final_model/_persist_producer）のみ override。walk_forward_cv_monthly に `pass_train_groups`（後方互換）を足して月クエリグループ境界を fit(group=…) へ受け渡す。予測は順位スコア（リターン単位でない）ため producer なし・OOF 比較専用（sell_ranking 統合は見送り）。model_comparison に M-5 として並び M-2(MSE) と純比較。xgboost 3.3.0 同梱で新パッケージ不要 |
| 2026-07-12 | **週次株価フルロードのタイムアウトを解消（Issue #311）**。M-1/M-2/M-3 の `stock_price_weekly` 全件ロード（~95万行）が本番 pooler の `statement_timeout=2min` を超過し（`QueryCanceled`／`lost synchronization`）、モデル比較 E2E が全モデル失敗していた。週次ロードを `macro_snapshots.load_weekly_prices_chunked`（`edinet_code` を 500 社ずつ IN 句で分割 fetch・PK インデックス使用）へ集約し、全件でも実測 ~30秒で安定完走。M-1/M-2（`_load_data_impl`）・M-3（`_load_prices_impl`）が共用。3モデル比較 E2E で全モデル OK（M-1 IC=0.23 / M-2 IC=0.33 / M-3 IC≈0.01）を確認。詳細は GOTCHAS.md「DB・運用上の注意」 |
| 2026-07-26 | **兄弟モデル候補メニュー（§15・探索枠）と M-6 正則化線形（§16）を追加**（#372・ADR-0021）。`walk_forward_cv_monthly(fit_predict=…)` 注入点へ差し込む候補（ElasticNet／ExtraTrees・QRF／Fama-MacBeth 予測ヘッド／マクロfold内PCA／regime-switch閾値線形／LightGBM・CatBoost）を `plugins/model_candidates.py` に集約し、`scripts/candidate_bakeoff.py` で同一fold・同一特徴量・同一指標の OOF 横並び実測を行う枠組みを新設。本番パネル（43ヶ月/57,955サンプル/71特徴量/9fold）の実測で **ElasticNet が M-2(XGBoost) を有意に上回った**（rank-IC 0.1713 vs 0.1419・差 +0.0294・95%CI [+0.0116,+0.0469]・p=0.002・多重比較補正 α/8 も通過）ため **M-6（macro_enet）として昇格**。他候補は据え置き。確定知見: 木の非線形性より縮小推定が効く／代替GBDTは誤差レベル／Fama-MacBeth は第1段階の正則化が必須（素の断面OLSは rank-IC 負）／マクロPCA圧縮は効果なし／木予測分布の区間は名目80%に対し実測被覆40.6%で使えず分割コンフォーマル(ADR-0020)が正解 |
| 2026-07-29 | **M-6 を producer 化し売り候補ランキング（§10）の `mu_source` へ統合**（#396・ADR-0021 の残タスク）。`macro_enet_scores`（`macro_gbdt_scores` と同型・`r1_prime` 付き）を追加し、`execute` 末尾で現在μ̂と確実性軸（コンフォーマル区間半幅・ADR-0020）を全置換永続化。`produced_output`/`read_producer_scores` は M-2 と同一形＝`mu_source="macro_enet"` で選択でき、**R3 足切りゲートも M-1・M-2 と同様に機能**する（M-3/M-4 は `r1_prime` 不在で無効のまま）。未実行時は graceful-degrade（ADR-0004）。探索中は `tuning_dry_run` で永続化 no-op（#264）。**既定 `mu_source` は M-2 のまま**＝切替は `/api/backtest` の `sell` source で事後検証してから別途判断する |
| 2026-07-30 | **M-4（§13）の基底に M-6 を追加し、重み既定を rank-IC 最大化へ変更**（#397・ADR-0015 追記）。レグを `BASE_MODELS` 定数駆動へ一般化（build/CV/現在μ̂ を名前でディスパッチ・`_fit_weights`/`_stack_walk_forward` は n 基底対応・`rank_ic_grid` は n 次元シンプレックス格子 `_simplex_grid`）。M-2/M-6 は build 契約が同じため config 同値ならスナップショット共有（`_same_build_config`）＝増分コストは ElasticNet の CV のみ。本番実測（3,979社/9fold/OOF 13,539ペア・honest）で **M-4 は 0.1468→0.1720（+0.0252・p=0.002）と有意に改善**し、M-2（+0.0301・p=0.001）・M-1（+0.0578・p=0.040）も有意に上回った。共通域は基底追加でも 13,539 で不変（M-6 は M-2 と build を共有するため。なお狭いレグは M-1 strict ではなく M-2 契約側＝2026-08-01 実測・#411）。**重み既定を nnls→rank_ic_grid へ変更**: NNLS は MSE 最小化で縮小推定の M-6 を重み 0 で捨て（最終重み M-6=0.0）、OOF の改善が現在μ̂＝producer へ届かない非整合が出たため（OOF 性能自体は誤差レベル・p=0.326／rank_ic_grid の最終重みは M-1 0.30/M-2 0.10/M-6 0.60）。確定知見: **M-4 は M-6 単体を上回らない**（+0.0006・p=0.810＝互角）→ 売り候補の既定 μ 出所を M-6 へ切り替えるかは Issue #402 で `source=sell` バックテスト検証 |
| 2026-08-30 | **M-4・M-5 を退役（GUI 非表示）し、M-5 の未実施だった実測を取った**（#570・ADR-0044）。`AnalysisPlugin.hidden` を新設して `/api/plugins` から除外＝サイドバー「③ 将来リターンを予測」は 6本→4本（M-1/M-2/M-3/M-6）。**削除ではない**のでレジストリ・`POST /api/plugins/{name}/run`・`model_comparison` の比較行・テストは残り、`python -m scripts.model_comparison_run --models a,b`（新設 CLI・`run_comparison(only_models=...)` を薄く包むだけ）で測り直せる。M-4 は統合が M-6 単体を上回らない（+0.0006・p=0.810・ADR-0015 の「単体で十分」判定に該当）ため退役し、`mu_source` 選択肢からも除去。M-5 は **ADR-0017 が約束したまま1ヶ月未実施だった実測**をようやく取り、rank-IC **0.0808 vs M-2 0.1578**（差 −0.0771・95%CI [−0.0995, −0.0558]・p=0.001・17期/OOF 25,738ペア）で**有意に劣後**＝「MSE で十分」を確定して退役。効かなかったのは分位の下側（最下位分位 0.0145→0.0520・売り側 spread 0.0676→0.0302）。ただし M-5 は early_stopping 不使用の固定木数で**正則化が M-2 と非対称**なため、「learning-to-rank が無効」ではなく「この実装では下回った」と読む（§14.4） |
| 2026-08-31 | **モメンタム（`use_momentum`）の既定 ON/OFF を実測し、OFF 維持を確定**（ADR-0045・測定入口 `python -m scripts.momentum_gate`）。導入時（2026-06-20）の OFF 理由「週次株価が約2年で薄帯・0 fold」は #198 のバックフィルで解消済み（実測 2019-07-29〜・4,024社・`use_momentum=ON` でも 15 fold）だったが、ADR-0019 の保守ゲート（ON/OFF の OOF 実測）が未消化のまま既定値＝本番 producer 設定として残っていた。**共通 (ym,ec) 域**（同一 fold・22,907件・15期）では4検定すべて補正後 α=0.0125 を通らず**符号も全て負**（M-6 rank-IC −0.0104 p=0.100・M-2 −0.0056 p=0.528）。ターンオーバー増・breakeven 低下（M-6 28.9→22.6bp）も同じ向き。**raw の母集団のままなら ON が全指標で改善して見え、M-2 は fold 間 std まで 0.1049→0.0809 と下がる**（＝ADR-0019 が既定化の条件に挙げた「頑健化」を満たしているように見える）が、これはモメンタム不可の行＝履歴の浅い当てにくい銘柄が落ちた効果。母集団が動く軸を測るときは共通域制限が必須（§9.8-4） |
| 2026-09-04 | **モメンタムの「窓」も実測して棄却し、M-1 も測って ADR-0045 の穴を埋めた**（[ADR-0050](adr/0050-hyperparameter-search-must-align-populations.md)・#592/#583）。ADR-0045 は窓を12に固定し「通過後に別途 tuning_search_space で探索する」と丸投げしていたが、**その丸投げ先に共通域制限が無い**。`hyperparameter_search` は各候補を**その候補自身の母集団**で評価するため、warmup で行を削る窓軸は「窓を伸ばす→履歴の浅い銘柄と古い月が落ちる→当てにくい対象が消える→スコアが上がる」経路と交絡したまま最大化される（M-1 の leaderboard は `use_macro=true` の候補で完全に単調・`Spearman(score,n_periods)` と `Spearman(score,n_oof_samples)` が完全一致＝分離不能）。窓 [3,6,12,18,24] を共通域で測ると、探索が両モデルで選ぶ**窓18 は M-1 +0.0039（p=0.757）／M-2 −0.0072（符号反転）**、**20検定のうち基準を上回って補正後 α=0.00500 を通ったものは 0件**、唯一通ったのは M-2 窓24 の**悪化**（−0.0264・p=0.001）。raw では M-2 の fold 間 std まで 0.1049→0.0717 と下がり ADR-0019 の「頑健化」を満たして見えるが共通域では 0.98倍。M-1 は strict のためパネルを M-2/M-6 と共有できず、ADR-0045 の対象外だった＝**根拠が失効した既定値が1つ残っていた**（#583）。測定側の落とし穴も確定——**`--smoke` の共通域は読めない**（`_thin` が並び順で間引くため、各条件が 97% 重なっていても6条件の交差が 483件＝7% に落ちる。stride=1 では 32,438件＝97.4%） |
