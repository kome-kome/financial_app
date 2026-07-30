# 売り側 OOF 指標の新設と既定 μ 出所の M-6 化

## Status

accepted（2026-07-30）。Issue #402 の設計決定。ADR-0004（OOF バックテスト＋`mu_source` トグル本体）
および ADR-0021（M-6 昇格）・ADR-0015（M-4 の base-on-common 判定作法）の続編。

## Context

#396 で M-6（ElasticNet）を producer 化した際、既定 `mu_source` は M-2 のまま据え置き、切替判断を
「`/api/backtest` の `sell` source で事後検証してから」と保留していた。#397 の本番実測で M-6 の
rank-IC 優位（0.1713 vs M-2 0.1419・p=0.002）が確定したため、既定切替の検証に着手した。

着手時点で **Issue #402 の改善案の前提が誤っていることが判明した**。

1. **`/api/backtest` の `source="sell"` は `mu_source` を振れない**。`backtest.score_record` の
   `sell` は **recommend プリセット加重和の符号反転**で、sell_ranking の μ / −R_macro 観点を一切
   使わない（`/api/backtest` のクエリにも `mu_source` は無い）。
2. 仮に配線しても **as-of バックテストは成立しない**。producer スコアテーブル
   （`macro_gbdt_scores` / `macro_enet_scores`）は「現在時点」のスナップショットのみ保持する
   （全置換方式）ため、過去日付のスコアリングへ持ち込めば look-ahead になる。

一方、μ̂ の売り性能を honest に測れる基盤は既にある（purge/embargo 付き walk-forward・ADR-0014）。
しかし `oof_backtest` の既存指標では売り判定の優劣を判定できない：`long_short_spread`（top−bottom）
は **top 分位の強さに引っ張られる**ため、「買い候補としては強いが売り候補の見分けは弱い」モデルでも
大きく出る。下流の sell_ranking は μ̂ の**下位**を売るので、評価軸が目的と食い違っていた
（NNLS が rank-IC 評価と目的不一致だった #397 の罠と同型）。

## Decision

1. **`oof_backtest` に売り側指標を追加する**（`plugins/macro_snapshots.py`）:
   - `short_side_spread` = 期毎の「**期内全体平均 − 最低 μ̂ 分位平均**」の期間平均。大きいほど
     売り候補が市場平均を下回った＝売りシグナルとして有効。ベンチマークは分位平均の単純平均では
     なく**全サンプル平均**を使う（端数 `m % n_quantiles` で分位サイズが不均一になるため）。
   - `short_side_hit_rate` = 上記が正だった期の割合。
   - `short_side_spread_by_period` = per-fold 系列。`model_stats.paired_ic_significance`
     （共通 test 期ペアリング＋定常ブートストラップ・ADR-0018）へそのまま渡して候補間の差を検定する。

   追加学習・追加 Egress ゼロの純後処理。`oof_backtest` を呼ぶ**全モデルへ自動波及**し
   （M-4 の `base_oof_backtest` にも載る）、モデル比較 UI にも表示する。既存キーは不変。

2. **既定 `mu_source` を M-2（`macro_gbdt`）→ M-6（`macro_enet`）へ切替**（ユーザー承認済み）。
   根拠は下記実測。`sell_ranking.params_schema()` の default、`templates/analysis.html` の
   `selected`、`static/js/analysis.js` のフォールバック、MODELS.md §10.2/§11.7.2/§16.4、
   CONTEXT.md「μ出所トグル」「売り側spread」、models.html / guide.html を同時更新した。

3. **検証手段は OOF 側に置く**（`scripts/sell_mu_source_bakeoff.py`）。`/api/backtest` の `sell`
   source を sell_ranking 相当へ拡張することは**しない**——producer スナップショットが as-of 復元を
   持たない構造上、リークなしには実装できないため。売り既定の再評価は今後も OOF 側で行う。

## 実測（2026-07-30・ローカル pickle キャッシュ・本番 Egress ゼロ）

`scripts/sell_mu_source_bakeoff.py`。M-4（3基底）を1回実行し `base_oof_backtest`（ADR-0015 の
base-on-common）で母集団差の交絡なく4通りを比較。共通域 3,979社・9 fold・OOF 13,539ペア・
honest（embargo=12）。

| モデル | 売り側 spread | 売り勝率 | 最低分位リターン | rank-IC（参考） | LS spread（参考） |
|---|---|---|---|---|---|
| **M-6（ElasticNet・新既定）** | **+0.0656** | 88.9% | **+0.0556** | +0.1713 | +0.1359 |
| M-4（3基底統合） | +0.0645 | 88.9% | +0.0567 | +0.1720 | +0.1291 |
| M-1（OLS） | +0.0581 | 100% | +0.0631 | +0.1142 | +0.0978 |
| M-2（XGBoost・旧既定） | +0.0511 | 88.9% | +0.0701 | +0.1419 | +0.1117 |

売り側 spread の差（定常ブートストラップ・共通 test 期ペアリング）:

- **M-6 − M-2: +0.0145・95%CI[+0.0072,+0.0219]・p=0.001 → 有意**（既定切替の根拠）
- M-4 − M-6: −0.0011・p=0.655 → 互角（統合は単体を超えない＝ADR-0015 の「単体で十分」に該当。
  rank-IC でも p=0.810 で同結論・#402 の起票元 #397 と一致）
- M-6 − M-1: +0.0075・p=0.483 → 非有意

**確定知見**:

1. **買い側 rank-IC の順位と売り側 spread の順位は一致しない**。M-2 は rank-IC で M-1 を上回る
   （0.1419 vs 0.1142）のに売り側では下回る（0.0511 vs 0.0581）。売りの既定選定に買い側指標を
   流用してはいけない（＝本 ADR で専用指標を新設した理由そのもの）。
2. **M-4 は売り側でも M-6 単体を超えない**。基底追加でも統合が単体を超えないという #397 の結論は
   評価軸を売り側へ変えても保たれる（#402 の残タスクは M-4 側にはない）。
3. R3 足切りゲート（Issue の第2観点）: honest split-conformal の **marginal 半幅は M-6/M-2 =
   1.011 倍**（本番 per-stock 分布も `r1_prime` 中央値 0.480 vs 0.452・p90 0.679 vs 0.691）で
   ほぼ同水準。既定 `r3_gate=0.0` は無効なので**切替による挙動差はなく**、閾値を設ける場合も
   同じ閾値がほぼ同じ厳しさで働く。
4. 本番カバレッジは低下しない（`macro_enet_scores` 1,699社 ≥ `macro_gbdt_scores` 1,687社・
   ともに snapshot 2026-07-13）。M-6 未実行環境では従来同様 graceful-degrade（μ 成分除外）。

## Consequences

- **保有銘柄の SELL/REDUCE ラベルが全面的に変わる**（μ 出所が変わるため）。ユーザー承認済み。
  旧挙動は UI の「μ の出所」で M-2 を選べば再現できる。
- 売り側指標は全モデルの `oof_backtest` に載るため、以降の兄弟モデル追加時は
  「rank-IC（買い側）」と「売り側 spread」を**両方**見て下流既定を判断できる（比較ファミリー内で
  片方の指標だけを見る事故を防ぐ）。
- `/api/backtest` の `sell` source は recommend 加重和の符号反転のまま据え置く。sell_ranking の
  as-of 再現は producer スナップショット構造上できないという判断を本 ADR に固定した。

## 参考文献

- **Politis, D. N. & Romano, J. P. (1994)**. "The Stationary Bootstrap." *Journal of the American
  Statistical Association* 89(428), 1303–1313. → https://doi.org/10.1080/01621459.1994.10476870
- **Grinold, R. C. & Kahn, R. N. (1999)**. *Active Portfolio Management*, 2nd ed. McGraw-Hill.
  （回転調整・実装ショートフォールの標準的枠組み。売り側スプレッドの解釈＝回避価値の根拠）
