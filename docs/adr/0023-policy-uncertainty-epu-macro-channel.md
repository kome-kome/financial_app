# 政策不確実性（EPU）チャネルの追加と既定昇格

## Status

accepted（2026-07-31）。Issue #404 の設計決定。ADR-0016（マクロ系列のカバレッジが strict の学習窓を
律速する問題）・ADR-0021（昇格ゲートの作法）・ADR-0022（買い側／売り側の両指標で下流既定を決める）
の続編。

## Context

M-1/M-2/M-6 のマクロ特徴量は #358 以降「全選択肢を既定 ON」だが、含まれるのは市場価格系（FX・金利・
株価指数・コモディティ・ボラ）と実体経済系（GDP・CPI・失業率・短観 DI・CLI 等）に限られる。**政策・
制度側の不確実性**を直接測る系列は無かった。VIX は市場が織り込む変動の指標であり、政策決定の予見
可能性そのものは測らない。

Issue #404 は当初6つの外部データ候補を含んでいたが、トリアージで無料枠の構造的制約により5つが脱落し
（Google Trends は公式 API が申請ゲート、Alpha Vantage は 25req/日、Finnhub は social sentiment 有料、
GDELT / Wikimedia は銘柄別だと 370MB/年で Supabase 500MB に収まらず #406 へ再スコープ）、**既存 FRED
コネクタで即取得できる EPU 系列のみ**が残った。

## Decision

1. **FRED から EPU 2系列を収集する**（`collector_prices.py` の `FRED_SERIES`・category=`uncertainty`）。

   | series_code | fred_id | 内容 | 頻度 |
   |---|---|---|---|
   | `US_EPU` | `USEPUINDXD` | 米 Economic Policy Uncertainty Index（日次版） | 日次 |
   | `US_EQUITY_EPU` | `WLEMUINDXD` | Equity Market-related Economic Uncertainty Index | 日次 |

   いずれも新規コネクタ・新規テーブル・新規インフラなしの**定数追加のみ**。日次のため `lag_days` /
   `freq` を持たず、#379/#382 の低頻度系列の変換窓にも #381 の strict 律速にも触れない。

2. **変換規約は zscore**（`_MACRO_MAP`）。EPU は常に正の水準指数であり、「平時と比べて高いか低いか」
   という水準そのものがレジーム情報になる。既存の指数系（VIX / CLI / 短観 DI）と同じ扱い。

3. **日本版 EPU（`JPNEPUINDXM`）は採らない**。FRED 側で 2016-04 に配信が凍結しており（#253 の
   `JP_IP` と同型）、取得するには policyuncertainty.com の配布ファイルを直接読む別コネクタが必要。

4. **昇格は実測ゲートを通してから行う**（`plugins/macro_snapshots.py` の `_PENDING_EVAL_FEATURES`）。
   ADR-0016 の順序制約と同じく、本番 `macro_data` へ蓄積する前に既定へ入れると strict
   （`macro_nan_ok=False`）の学習母集団が消える。実装時点では保留枠へ入れて選択肢のみとし、実測で
   ゲートを通過したので既定へ移した（保留枠は空になったが、次の系列追加のために構造として残す）。

## 実測（2026-07-31・`scripts/epu_feature_bakeoff.py`・ローカル pickle キャッシュ）

同一モデル・同一 fold・同一スナップショット設定のまま `build_snapshots` へ渡す `macro_names` だけを
差し替えた2条件（base = 現行既定 / with_epu = base + EPU 2本）を比較。3,979社・43ヶ月・57,955サンプル・
9 fold・honest（embargo=12・ADR-0014）。M-2（非線形）と M-6（正則化線形）の両方を見るのは、EPU が
「木の分岐として効く」のか「縮小推定下の線形項として効く」のかで結論が変わりうるため。

| 条件 | M-2 rank-IC | M-6 rank-IC | M-2 売り側 spread | M-6 売り側 spread |
|---|---|---|---|---|
| base | +0.1468 | +0.1710 | +0.0515 | +0.0652 |
| with_epu | +0.1432 | +0.1713 | +0.0549 | **+0.0684** |

差の検定（定常ブートストラップ・共通 test 期ペアリング・ADR-0018）。昇格ゲートは 2モデル × 2指標＝
4検定の Bonferroni 補正 **α = 0.05/4 = 0.0125**:

| モデル | 指標 | diff | 95%CI | p | 判定 |
|---|---|---|---|---|---|
| M-6 | 売り側 spread | **+0.0032** | [+0.0008,+0.0067] | **0.001** | **通過** |
| M-2 | 売り側 spread | +0.0034 | [−0.0010,+0.0078] | 0.144 | 非有意 |
| M-6 | rank-IC | +0.0003 | [−0.0031,+0.0041] | 0.737 | 非有意 |
| M-2 | rank-IC | −0.0037 | [−0.0074,−0.0001] | 0.041 | 非有意（補正後） |

strict 母集団は **43ヶ月・57,955サンプルで不変**（EPU は 2016-07-30 開始＝既存の律速であるコモディ
ティ8系列 2020-07 より古いため、学習窓を縮めない）。

**判定＝昇格**（ユーザー承認済み）。事前に決めたゲート（補正後有意＋改善方向＋strict 不変）を満たす。
現在の売り既定 `mu_source` は M-6（ADR-0022）なので、M-6 の売り側改善は下流の SELL/REDUCE 判定へ
直接効く。

**確定知見**:

1. **EPU の寄与は買い側ではなく売り側に出た**。4指標のうち補正後有意なのは M-6 の売り側 spread のみで、
   rank-IC は両モデルとも動かない（M-6 +0.0003・M-2 −0.0037）。政策不確実性は「上がる銘柄の選別」より
   「下げに晒される銘柄の回避」に効く、という非対称性。ADR-0022 の「買い側と売り側は順位が一致しない」
   がモデル比較だけでなく**特徴量選択にも当てはまる**ことが確認された。
2. **唯一の悪化は M-2 の買い側 rank-IC（−0.0037・補正前 p=0.041）**。補正後は非有意＝偶然と区別でき
   ないが、符号が一貫して負であることは記録しておく（木モデルでは無情報な特徴が分岐を希釈しうる）。
   後続で M-2 の買い側を詰める際は、この2系列を最初の除外候補として見ること。
3. 縮小推定（M-6）は EPU を「効くだけ使う」ため下振れしないが、GBDT（M-2）は僅かに下振れした。
   #372 の確定知見（グループ共線性への縮小推定が効く）と整合する。

## Consequences

- `DEFAULT_MACRO_FEATURES` が 2 本増える（既定 ON のマクロ特徴量が増える）。M-1 は pooled BIC
  （LassoLarsIC）、M-6 は ElasticNet が不要なら自動的に落とすため、過剰選択のリスクは従来どおり
  モデル側が吸収する。
- 本番 `macro_data` は +7,302 行（2系列 × 3,651 行）。テーブル 22→23MB・DB 326→328MB / 500MB。
  日次カレンダー（土日含む）で配信されるため他の日次系列より行数が多い。
- 既定が変わるので M-1/M-2/M-6 の再実行後は μ̂ が変化する。旧挙動は UI のマクロ特徴量選択から EPU 2本を
  外せば再現できる。
- 新規マクロ系列の追加手順が「収集 → 保留枠 → 実測 → 昇格」として定式化された
  （`_PENDING_EVAL_FEATURES` ＋ `scripts/epu_feature_bakeoff.py`）。以降の系列追加はこの手順に従う。

## 参考文献

- **Baker, S. R., Bloom, N. & Davis, S. J. (2016)**. "Measuring Economic Policy Uncertainty."
  *Quarterly Journal of Economics*, 131(4), 1593–1636. → https://doi.org/10.1093/qje/qjw024
- **Politis, D. N. & Romano, J. P. (1994)**. "The Stationary Bootstrap." *Journal of the American
  Statistical Association*, 89(428), 1303–1313. → https://doi.org/10.1080/01621459.1994.10476870
