# 学習窓をデータ履歴の延伸で広げる（週次株価7年＋財務2018〜＋過去株価紐付け）

## Status

accepted（2026-08-01）。Issue #411 の設計決定。ADR-0016 追試の帰結。

## Context

M-1/M-2/M-4/M-6 の honest OOF 評価は `walk_forward_cv_monthly(min_train_months=6, step_months=3)`
＋ embargo=12（ADR-0014）で行う。fold 数は `(母集団月数 − 6 − 12) / 3` で決まるため、**学習窓の
長さが検定力を直接決める**（ADR-0018 の対比較検定・ADR-0021 の昇格ゲートはいずれも fold 数 n の
定常ブートストラップに依存する）。

2026-08-01 の実測（ADR-0016 追試・`scripts/measure_strict_binding.py`）で、窓を縛っていたのは
モデル側（M-1 strict の「全マクロ同時非None」制約）ではなく**データ履歴長**だと確定した:

- `stock_price_weekly` は 2021-01-04 開始（52週先ラベルを引くと上限 53ヶ月）
- `financial_records` は period_end 2020-09-30 開始で、先頭6ヶ月をさらに削る
- 結果 M-1 strict の母集団は **47ヶ月・111,210 サンプル**、fold 10 期

「窓を広げるにはデータを過去へ延ばすしかない」が、Supabase Free 500MB が制約になる。

### 延伸可能性の実測（着手前に上限を確認）

推測で計画を立てると ADR-0016 の「改善案1（FRED 再収集で backfill）が原理的に不可能だった」と
同じ失敗を繰り返すため、両ソースの到達点を先に叩いて確かめた。

- **EDINET**: 過去日の `documents.json` に有価証券報告書が返る（2018-06-27 で 416件・
  2019-06-26 で 445件）。「保存期間5年」ではなく**2018年まで遡って再収集できる**。
- **Yahoo v8**: `interval=1wk` で 2010-01-01 からの週次を返す（代表3銘柄で 867 点）。

## Decision

**years=7（今日から7年前＝2019-08）まで株価・財務の両方を延伸する。**

1. **週次株価**: `_pipeline_gh.py --backfill-weekly --backfill-weekly-years 7`。起動口が
   GitHub Actions に無かったため `.github/workflows/backfill-weekly.yml` を新設。
2. **財務**: `full-pipeline.yml` に `year_steps=[7]`（＝`--years-back 7 --collect-only`）。
   `skip_existing=True` で既存 doc_id はスキップされ冪等。
3. **過去株価の紐付け**: `_pipeline_gh.py --backfill-yahoo`。同じく起動口が無かったため
   `.github/workflows/backfill-price-link.yml` を新設。**これが必須**である理由は下記。
4. **延伸幅は years=7 まで**とし、years=8 は採らない。事前見積もりで DB 426MB・VACUUM 前ピーク
   482MB となり、Issue #290 の再オープン閾値 430MB と 500MB 上限の双方に接近するため。

### 財務だけ収集しても窓は伸びない（実測で判明した連鎖）

財務バックフィル直後の実測で、追加した行は **`stock_price` が 2018/2019 とも 0 件**だった。
`financial_metrics` VIEW の `per`/`pbr` は `financial_records.stock_price` を分子に持ち、M-1 の既定
`fin_features`（per/pbr を含む）が1つでも None なら `build_snapshots` はその行を落とす。結果、
株価を 2019-08 まで延ばし財務を 2018 まで収集してもなお窓は **55ヶ月**（price-only cap 71ヶ月に対し
16ヶ月の取りこぼし）に留まった。EDINET は財務諸表しか返さないため、**過去年度の収集には
「株価の紐付け」までが1セット**になる。

## Consequences

### 学習窓（`python -m scripts.measure_strict_binding`）

| 条件 | 着手前 | 株価+財務のみ | 株価紐付け後（最終） |
|---|---|---|---|
| M-1 strict | 47ヶ月 / 111,210 | 55ヶ月 / 141,285 | **71ヶ月 / 173,836**（2019-08〜2025-06） |
| M-2 実契約 | 43ヶ月 / 57,955 | 55ヶ月 / 74,398 | **67ヶ月 / 91,482** |
| price-only cap | 53ヶ月 | 71ヶ月 | 71ヶ月 |
| 律速している側 | 財務 | 財務 | **週次株価（cap = actual）＝延伸分を取り切った** |

サンプル +56%。マクロ既定46本の同時非None開始も 2021-01 → 2019-07 へ。

### honest OOF（`python -m scripts.measure_embargo_impact`・embargo=12）

| 指標 | 着手前（47ヶ月） | 現在（71ヶ月） |
|---|---|---|
| M-1 fold / OOF | 10 / 29,751 | **18 / 51,374** |
| M-1 rank-IC | 0.1982 | **0.1134** |
| M-4 fold / OOF | 9 / 13,539 | **17 / 25,330** |
| M-4 rank-IC | 0.1569 | **0.1675** |
| 共通域 M-1 | 0.1142 | **0.0484** |
| 共通域 M-2 | 0.1419 | **0.1317** |
| 共通域 M-6 | 0.1713 | **0.1693** |

**確定知見**:

1. **fold が 9〜10 期から 17〜18 期へ倍増**した。ADR-0018 の対比較検定・ADR-0021/0023 の昇格ゲートが
   依拠する n が倍になり、以後の判定の検出力が上がる（本 Issue の主目的）。
2. **M-1 の rank-IC は 0.1982 → 0.1134、共通域では 0.1142 → 0.0484 へ大きく下がった**。これは
   モデルの劣化ではなく、**旧値が 10 期の点推定で、コロナ期（2020年の急落〜急回復）を含む長い窓では
   保てなかった**ということ。M-1（Ohlson 型 OLS ＋ マクロ交差項）はレジーム転換局面に弱い。
   短い窓で測った優位性を長期の実力と読み替えてはいけない、という ADR-0016 の教訓
   （fold 2 期の点推定を信じない）の延長線上にある。
3. **M-6（ElasticNet）は 0.1713 → 0.1693 とほぼ不変**で、窓を倍にしても崩れなかった。M-2 も
   0.1419 → 0.1317 と小幅。縮小推定・GBDT はレジームを跨いでも安定する。既定 `mu_source=macro_enet`
   （ADR-0022）の選択は本実測でも支持される。
4. **M-4 は 0.1569 → 0.1675 と改善**し、共通域の全基底（M-1 0.0484 / M-2 0.1317 / M-6 0.1693）に対し
   M-6 とほぼ互角・他2つを明確に上回る。ADR-0015 の「M-4 は M-6 単体を超えない」は据え置き。

### 容量（Supabase Free 500MB）

| | 着手前 | 最終 |
|---|---|---|
| `stock_price_weekly` | 157MB（967,004行） | 180MB（1,271,282行） |
| `stock_price_daily` | 48MB | 43MB |
| `financial_records` | 59MB（42,872行） | 62MB（50,477行） |
| **DB 合計** | **332MB** | **356MB** |

見積もり（393MB）に対し **+24MB で着地**。週次の行あたりサイズが 162→141 bytes に改善したため。
残余裕 144MB。**backfill 中は daily が DELETE ベース trim で 43→92MB に bloat する**ので、
`vacuum-maintenance.yml`（VACUUM FULL・約9秒）を延伸直後に必ず1回流すこと。

### 運用上の注意（今回踏んだもの）

- **読取専用スクリプトでも `commit()` するまでトランザクションは開いている**。pooler 経由で
  `idle in transaction` が3時間15分滞留し、`init_db()` の `ALTER TABLE companies` を
  AccessExclusiveLock 待ち → `statement_timeout=2min` で殺し、ワークフローが起動直後に落ち続けた。
  Issue #269 と同型の再発。`scripts/candidate_bakeoff.py` / `scripts/measure_embargo_impact.py` の
  価格ローダに `commit()` を追加済み（詳細と診断クエリは GOTCHAS.md）。
- **`full-pipeline.yml` の finalize は J-Quants 403 で必ず落ちる**（`JQUANTS_BACKFILL_DAYS=730` の
  境界日が無料プランの実効カバレッジ外）。財務収集自体は完了しているため本 ADR の作業には影響
  しないが、別途 Issue #412 で対処する。

## Alternatives considered

- **years=8（2018-08 まで）**: 窓は約75ヶ月・fold 19 期と最大だが、DB 426MB・ピーク 482MB で
  #290 の再オープン閾値と 500MB 上限の双方に接近する。効果の差（fold 17→19）に対しリスクが
  見合わないため却下（ユーザー承認も years=7）。
- **years=6（2020-08 まで）**: DB 360MB と最も安全だが窓 55ヶ月・fold 12 期止まり。コロナ期を
  部分的にしか含まず、レジーム多様性の獲得という副次的効果も小さい。却下。
- **古い期間を月末週だけ保持して容量を 1/4 にする**: M-1/M-2 は月末スナップショットしか使わないが、
  `build_snapshots` の 52週先ラベルは**週インデックス基準**（`snap_idx + HORIZON_WEEKS`）なので
  月次行では壊れる。M-3（週次DLM）も古い期間を使えなくなる。実装改修の規模に対し容量の
  余裕（144MB）が十分あるため不要。却下。
- **モデル側（strict の緩和・マクロ既定の変更）で窓を広げる**: ADR-0016 追試で strict は1行も
  落としていないと実測済み。効果ゼロ。

## 参考

- 関連 ADR: 0014（purge/embargo）, 0015（M-4 スタッキングと共通域）, 0016（ICE truncate・strict 追試）,
  0018（対比較検定）, 0021（昇格ゲート）, 0022（既定 mu_source）。
- 関連 Issue: #411（本 ADR）, #412（finalize の J-Quants 403）, #269（idle in transaction の罠）,
  #290（`stock_price_daily` の bloat 恒久対策）, #198（週次バックフィルの実装）。
- 再現手順: `python -m scripts.measure_strict_binding` → `python -m scripts.measure_embargo_impact`。
