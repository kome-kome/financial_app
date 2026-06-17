# 今後の課題・改善案

未実装の改善項目を記録する。完了済み項目は `docs/archive/IMPROVEMENTS.md` に集約済み（git 履歴で詳細参照可能）。

> **凡例**: 各項目は「該当（`ファイル:行`）／問題／改善案／検証」で issue 化可能な粒度。優先度は 【高 / 中 / 低】、種別は「運用」（本番環境の操作・コード変更なし）か「コード」（新規実装）で示す。
> **直近完了（2026-06）**: Tier 1 リファクタ全件（T1-1〜T1-9）／ 発行済株式数の正規取得（G）／ `period_end` DATE 型移行（H）／ 財務項目網羅性 C1・C2 / **M-1 マクロ×リスク-リターン推奨モデル（Phase A–D 全件）**。詳細は `docs/archive/IMPROVEMENTS.md`「Phase 4」。

---

## Tier 1 — 本番データの鮮度・完全性【運用・最優先】

> 既に実装・マージ済みの機能を本番で実データとして機能させるための運用作業。コード変更は基本不要だが、本番リソース（EDINET API キー・Supabase・GitHub Actions 実行権限）へのアクセスが要る。Claude のセッションからは実行不可で、ユーザー環境での操作が必要。

### DF-1. 株価 daily 差分収集（cron）の再有効化  【✅ 完了・2026-06-12〜】
- 2026-06-12 以降、毎日 `schedule` トリガーで success 継続中。
- J-Quants catchup（embargo 明け分 upsert）・Yahoo gap-fill（当日株価補完）・financial_records.stock_price 更新（約3,774社）が正常動作確認済み（最終確認: 2026-06-16）。
- **該当**: `.github/workflows/daily-incremental.yml` / `_pipeline_incremental.py`

### DF-2. C2 新項目の本番フル再収集  【中・運用】
- **問題**: C2 の新8列（`pl_depreciation` / `bs_ppe_total` / `bs_investments_other_assets` / `pl_extraordinary_income`・`loss` / `pl_rd_expenses` / `employees` / `issued_shares`）はコード結線済みだが、**本番 DB の既存レコードは再収集まで NULL** のまま。company 画面の内訳チャート・分析特徴量（R&D/D&A 集約度）に新項目が表示されない。
- **改善案**: `python collector.py --years 5`（**方式(あ)=既存 upsert・最小変更で確定**）。新項目の追加コストはほぼゼロ（同じ XBRL ZIP を再パースするだけ）・列追加の容量増は約1.6MB で、DB 容量 165MB/500MB・ヘッドルーム約335MB（2026-06-06 計測）に余裕で収まる。
- **注意**: 本番は `SKIP_XBRL_RAW=true`（Supabase 容量対策）で raw 未保存のため、`/api/collect/reparse`（再解析）は使えず **EDINET からの全件再取得が必要**。`raw_xbrl_json` 削除＋`VACUUM FULL` の事前領域確保は容量目的では不要（やる場合は独立 PR）。
- **該当**: `collector.py`（`run_full_collection`）／要 `EDINET_API_KEY`・数時間

---

## Tier 2 — 分析モデルの拡張【コード】

### M-1. マクロ・リスク-リターン推奨モデル（新プラグイン）  【中・コード】

> 📖 **初心者向けの噛み砕いた解説**: 本節の設計思想をゼロから順を追って説明した副読本 → [`M1_MACRO_MODEL_GUIDE.md`](M1_MACRO_MODEL_GUIDE.md)

> **位置づけ**: 単なる「既存モデルへのマクロ特徴量追加」を超え、**マクロ環境ごとに企業/セクター/財務構造の感応度が違う**ことを交差項で捉え、各銘柄を **リスク-リターン平面**に配置して推奨集合を選ぶ新プラグイン `plugins/macro_risk_return.py` を起こす。最小フェーズ（Phase A）は既存 `price_predictor` へのマクロ特徴量追加なので、段階的に価値が出る。

**着想（ユーザー要件）**: マクロ要因に反応しやすいセクター・財務指標（PL/BS/CF 要素）・個別企業があり、「マクロ×ベース説明変数」の相関で**予測可能性の大小**が決まる。予測可能性を**予測値の分散（リスク）**として定義し、**リスクとリターンの期待値**から推奨企業を選定したい。縦軸リターン・横軸リスクの散布図で効率的な企業群を可視化する。

#### 学術的裏付け（設計の妥当性検証済み）

| 理論 | 我々の設計への含意 |
|---|---|
| **Markowitz (1952) 効率的フロンティア / Sharpe** | リスク-リターン平面は標準。**生リターンでなく Sharpe 的な リターン/リスク でランク**する |
| **Fama-French 3/5因子** | 財務比率（value=B/M・profitability・investment）は実証済みファクター。符号事前（B/M+・利益率+・投資−）で係数をサニティチェック。ただし因子間冗長性に注意 |
| **APT / Chen-Roll-Ross (1986)** | 金利ターム・スプレッド・為替は理論的に価格付けされるリスク。**「同じマクロに企業ごと異なるβ」= 交差項設計そのもの**。ただしマクロは*水準でなくサプライズ/変化*を使う |
| **Black-Litterman (1992) / James-Stein** | 生の予測リターンは誤差最大化を招く。**μ をセクター平均へ収縮し、収縮ウェイトを予測信頼度（R1）にする** |
| **低ボラ・アノマリー / Betting-Against-Beta (Frazzini-Pedersen 2014)** | 高ボラ罰則はリターン押上げにもなる。**フロンティアは非単調**で低ボラ・クラスタが左上を支配する前提で描画 |
| **Michaud (1989) / Chopra-Ziemba (1993) / DeMiguel (2009)** | 危険なのはリターン軸。期待リターン誤差は分散誤差の約11倍有害。推奨ルールは凝りすぎない（1/N に勝つのは難しい） |

#### 説明変数（全て無次元 — 注意事項1の次元整合性を満たす）

被説明変数 μ（1年先リターン・年率）が無次元なので、説明変数も全て無次元に揃える。

- **財務比率**: `per` / `pbr` / `roe` / `equity_ratio` / `rd_intensity` / `da_intensity` ＋ 年度別 Z スコア
- **モメンタム**: 12-1ヶ月リターン（週次株価 `stock_price_weekly` から算出・新規）。Jegadeesh-Titman の単独最強アノマリーで value と負相関
- **マクロ（サプライズ/変化）**: USDJPY YoY 変化率・US10Y/JP10Y の5年 Z スコア（ターム-スプレッド系）。*水準でなく innovation*（CRR）
- **交差項**: 財務×マクロ ＋ セクターダミー×マクロ（条件付きβ = APT の異質感応度）

| 変換 | series | 式 | 次元 |
|---|---|---|---|
| YoY 変化率 | USDJPY, SP500 | (ref30日平均 − 1y前30日平均)/1y前30日平均 | 無次元率 |
| 5年 Z スコア | US10Y, JP10Y | (ref30日平均 − 5y平均)/5y標準偏差 | 無次元 Z |

#### モデル選択（次元爆発の制御）

交差項は財務×マクロ + セクター×マクロで 40〜60 項に膨らむため：

- **前進選択 BIC**: 空モデルから BIC を最も下げる項を1つずつ採用、改善停止で打切り（O(p²)・Render 内）
- **VIF 監視**: 各ステップで既存 `check_collinearity`（`plugins/utils.py`）を噛ませ、共線項の不安定スワップを防ぐ
- **walk-forward CV**: 既存 `walk_forward_cv_monthly` で時系列順を守った汎化検証（plain k-fold はルックアヘッド）

#### リスク3指標と役割分化（3D を「対等3軸」でなく役割で分ける）

| 軸 | 量 | 定義 | 役割 | 解像度 |
|---|---|---|---|---|
| **Y=リターン** | μ_shrunk | 1年先リターン年率を**セクター平均へ収縮**（重み=R1） | 期待リターン | 個社・厳密 |
| **X=リスク（既定）** | **R2** 実現ボラ | 予測基準日直前1年の週次リターン標準偏差 ×√52（過去のみ=リークなし） | 価格変動リスク・Sharpe 分母 | 個社・厳密 |
| **符号化=信頼** | **R1** 予測不確実性 | OLS 予測分散 s²(1+xᵀ(XᵀX)⁻¹x) の `se_obs` | この点をどれだけ信じるか・μ収縮ウェイト兼用 | 個社・厳密 |
| **補助** | **R3** モデル信頼性 | セクター×サイズ・バケットの CV 残差 RMSE | この企業「タイプ」をモデルがどれだけ説明できたか | バケット |

R1（イン・サンプルのレバレッジ）と R3（アウト・オブ・サンプルのグループ誤差）は「モデル信頼性」の二面、R2 は「価格リスク」。

#### 推奨ロジック

- **効用ランキング** `U = μ_shrunk − λ·R2`（λ=リスク回避度・スライダーで可変）。λ=0 でリターン最大化、λ大でリスク最小化
- **パレートフロンティア強調**: 「同リスクでより高リターン」な非劣解を散布図上で色分け
- 低ボラ・アノマリーより、フロンティアは右肩上がりにならず低ボラ・クラスタが左上を支配する想定 → そのまま提示

#### 可視化（Chart.js のみ・Plotly 不採用）

本プロジェクトは **Chart.js 4.4.1 のみ**（Plotly 未使用）。真の3D散布図は描けないが、リスク-リターンは本来2D（Markowitz 平面）なので：

- **Chart.js バブル**: Y軸=μ_shrunk（固定）・X軸=リスク（R2 既定、セレクタで R1/R3 切替）・**バブルのサイズ/色 = R1 信頼度**（大/濃いほど高信頼）
- パレート効率銘柄を強調色・ホバーで企業名/U/μ/各リスク値を表示
- Plotly 導入（真の3D）は別途セキュリティ評価＋承認が必要なため見送り

#### 実装フェーズ

| Phase | 内容 | 該当ファイル | 検証 |
|---|---|---|---|
| **A. マクロ特徴量基盤** ✅ | `get_macro_features` / `get_momentum_return` / `_MACRO_FEATURE_MAP` を `plugins/utils.py` に追加 | `plugins/utils.py` | `tests/test_macro_features.py`（17テスト合格） |
| **B. 交差項モデル** ✅ | 新プラグイン `macro_risk_return.py`。財務×マクロ + セクター×マクロ交差項・前進BIC・VIF監視・walk-forward CV で μ を算出 | `plugins/macro_risk_return.py` | `tests/test_macro_risk_return.py`（18テスト合格） |
| **C. リスク-リターン** ✅ (R3 は未実装) | R1/R2 算出・μ のセクター収縮（James-Stein）・U=μ−λR²・パレート抽出 は Phase B に含めて実装済み。R3（セクター×サイズバケット CV-RMSE）は未実装で `risk_axis` オプションから除外中 | 同上 + `plugins/utils.py` | テスト済み（`r1`, `r2`, `mu_shrunk`, `is_pareto`, `utility` を検証） |
| **D. UI** ✅ | analysis.html に新タブ・Chart.js バブル（Y=μ_shrunk / X=R2 or R1 / バブルサイズ=R1信頼度逆数）・パレート強調・Walk-forward CV 指標・ランキング表 | `templates/analysis.html` / `static/js/analysis.js` | 手動確認済み（18テスト + CI green） |

#### params_schema（パラメータ契約・CLAUDE.md 準拠）

- `lambda_risk`（slider, dtype=float, 0〜3, default=1.0）: リスク回避度
- `risk_axis`（select, options=[R2,R1,R3], default=R2）: X軸に置くリスク
- `features`（multiselect）: 財務+マクロ特徴量の選択
- `sector_grouping`（select, options=[大分類,中分類]）: セクターダミーの粒度
- `momentum_window`（number, dtype=int, default=12）: モメンタム算出月数
- `min_coverage`（slider, dtype=float）: 特徴量充足率の下限

#### 前提条件

1. **マクロ5年蓄積**（`/api/collect/macro/start`）。確認: `SELECT series_code, MIN(trade_date), MAX(trade_date), COUNT(*) FROM macro_data GROUP BY series_code;`（≈1250行/系列）
2. **週次株価の履歴**（モメンタム・R2 用）— `stock_price_weekly` は全履歴保持済みで充足
3. **サンプル充足**: 交差項 40〜60 に対し (企業×年) サンプルが十分か本番 DB で要確認（前進BICは最終 15〜25 項に収束する想定）

#### 設計留意・ドキュメント更新

- `winsorize(p1-p99)` は OLS 学習前に全特徴量へ一括適用（注意事項2）
- Z スコアは年度別に算出（注意事項3）
- 完了時に `docs/MODELS.md`（新モデル解説・参考文献は原著 DOI: Markowitz 1952 / Fama-French 1993,2015 / Chen-Roll-Ross 1986 / Black-Litterman 1992 / Frazzini-Pedersen 2014）と `templates/models.html`・`docs/ARCHITECTURE.md`（プラグイン/画面/エンドポイント）を更新

---

## Tier 3 — 本番運用の堅牢化【運用・インフラ】

### O-1. DB バックアップ運用ポリシーの策定  【中・運用】
- Supabase の自動バックアップ機能を利用しつつ、復旧手順を `docs/DEPLOYMENT.md` に文書化する。

### O-2. 外形監視の追加  【低・運用】
- Render ダッシュボード + UptimeRobot 等の外形監視を追加検討。死活監視用の `/health` エンドポイントは既設（DB 疎通で 200/503）。

### O-3. GitHub Actions workflow の整理・命名改善  【✅ 完了・2026-06-17】
- ① 全 yml の `name:` に `[CI]` / `[定常]` / `[全件]` / `[一回性]` / `[補完]` プレフィックスを付与。
- ② `docs/DEPLOYMENT.md` に「GitHub Actions workflow 早見表」テーブルを追加。
- **該当**: `.github/workflows/*.yml` / `docs/DEPLOYMENT.md`

---

## 注意事項（設計制約）

変更・実装時は以下の制約を必ず守ること（`CLAUDE.md` より）：

1. **次元整合性**: 無次元比率で絶対額を予測しない（Ohlsonモデル型で per-share 設計）
2. **外れ値処理**: OLS学習前に `winsorize(p1-p99)` を適用
3. **Zスコアは年度別に計算**（年度を跨いで計算しない）
4. **科学計算ライブラリ採用基準**: numpy/scipy/statsmodels/scikit-learn は採用可（`docs/VISION.md` の採用基準参照）。新規ライブラリ追加時は同基準と CLAUDE.md「パッケージ管理方針」に従う
5. **`docs/ARCHITECTURE.md` を同じ作業内で更新**
6. **Render デプロイ前提**: メモリ 512MB・スピンダウン 15 分・SSH 不可・永続ディスクなし。スキーマ変更は `init_db()` の冪等マイグレーションで対応。詳細は `docs/DEPLOYMENT.md`
