# 半期(H1)決算を分析へ反映する方式

## Status

**proposed（2026-08-11）**。Issue #424 子2 の設計。**決定は保留**する——各案は H1 が揃った企業サブセットでの OOF 実測を通してから選ぶ、というのが #424 本文の要求であり、その実測は Supabase の Egress 復旧（2026-08-18・#478）後にしか回せない。本 ADR は**比較軸と実測プロトコルを固定する**ところまでを担い、結果が出た時点で accepted へ更新する。

## Context

### 1. 実態: H1 は分析のどこからも見えない

`sql/financial_metrics_view.sql:58` が `WHERE fr.period_type = 'annual'`。半期報告書を収集していても `financial_metrics` VIEW に出ないため、M-1〜M-6・`recommend`・`sell_ranking` のいずれからも参照されない。**12月期企業は最大11ヶ月古い通期のみで評価される**（#424 実測・2026-08-02 時点で H1 の `period_end` MAX は 2025-09-30）。

### 2. `WHERE` を外すと VIEW が壊れる（同ファイル :55-57 に既出）

| 列 | 壊れ方 |
|---|---|
| `z_revenue` / `z_op_margin` / `z_roe` / `z_equity_ratio` / `z_cf_ratio` / `z_eps` / `z_de_ratio` / `z_nc_ratio` | `WINDOW yw AS (PARTITION BY year)`。同一 year に annual と H1 が混在すると**期間の異なる値で Zスコアを取る**（半期の売上と通期の売上が同じ母集団に並ぶ） |
| `z_roe_sec` / `z_op_margin_sec` | `yws AS (PARTITION BY year, industry)`。同上 |
| `rev_growth` / `op_growth` / `eps_growth` / `delta_roe` / `delta_op_margin` | `cw AS (PARTITION BY edinet_code ORDER BY year, period_end)`。**通期→半期の LAG が「成長率」として計算される**（無意味） |
| `predicted_market_cap` / `gap_ratio` | `LEFT JOIN regression_results ON (edinet_code, year, period_end)`。通期 OLS の予測なので H1 行には対応行が無く NULL になる |

### 3. 器（`financial_metrics_interim`）は既にあるが**孤児**

`sql/financial_metrics_interim_view.sql`（#219② フェーズC）が非通期行の VIEW として既に存在する。

- **持つ**: 行単位比率（`op_margin` / `net_margin` / `roe` / `roa` / `equity_ratio` / `de_ratio` / `cf_ratio` / `rd_intensity` / `da_intensity` / `asset_turnover` / `net_cash` / `nc_ratio`）、**同一 `period_type` の前年同期比**（`LAG OVER (PARTITION BY edinet_code, period_type ...)`＝H1 vs 前年 H1）、point-in-time 用の `period_type` / `filing_date`
- **持たない**: `z_*`（年度内クロスセクション）、`predicted_market_cap` / `gap_ratio`
- **消費者ゼロ**: Python からの参照は `database.py` の定義と `tests/test_financial_metrics_interim.py` だけ。唯一の予定消費者だった #323（決算開示イベント駆動モデル）は **2026-07-16 に wontfix クローズ**済み（シグナルが弱く、前提の日足 OHLC 収集も #290 の容量制約で見通しが立たないため）。

つまり案B の骨格は**作られたが誰も使わないまま残っている**。これは新規実装コストが低いことを意味すると同時に、「器があること」自体は有効性の証拠ではない。

### 4. モデル側が課す制約（ここが実現可能性を決める）

- 既定の `fin_features` は `DEFAULT_FIN_FEATURES = ["per", "pbr", "roe", "equity_ratio", "roa", "eps_growth"]`（`plugins/macro_snapshots.py`）。**`per` / `pbr` は市場データ依存**。
- `_build_snapshots_impl` は `fin_features` が**1つでも None ならその社のスナップショットを捨てる**。H1 側で `per` / `pbr` が埋まらなければ、H1 を露出しても既定構成では1件も残らない。
- 市場データの**現在株価による上書きは annual 行のみ**（#421・`collector_prices.py:836-838`）。ただし `period_end` 近傍の履歴株価を入れる処理（`dated_records`）は H1 行にも適用される。
- **H1 行の `per`/`pbr`/`market_cap` が実際どれだけ埋まっているかは未実測**。`financial_metrics_interim` の冒頭コメントは「H1 行は市場データ未収集のため `nc_ratio` 等は NULL」と書いているが、これは #421 の前後で実態が変わりうる箇所であり、**確認せずに前提にしない**（Issue の記述を鵜呑みにしない）。

### 5. 会社予想（`statement_disclosure`）の天井

`JQUANTS_DISCLOSURE_DELAY_DAYS = 84`（`collector_utils.py:39`）。J-Quants 無料プランは 12 週固定で配信を遅らせるため、**開示由来のデータは常に `today − 84日` が上限**である。案C はこの制約を直接受ける。`feature_disclosure.py` の特徴量化層は実装済みだが、**消費者は `scripts/event_study_*.py` の2本だけ**で、本番コードパス（プラグイン・API）からは呼ばれていない（その2本も #323 の検証用）。

## Considered Options

| | 案A: TTM 合成列 | 案B: 並列 VIEW（`financial_metrics_interim`） | 案C: サプライズ特徴量 | 案D: 通期のみ（現状維持） |
|---|---|---|---|---|
| 何を足すか | `annual(FY-1) − H1(FY-1) + H1(FY)` で PL/CF の直近12ヶ月を合成し、BS は最新 H1 | 既存 interim VIEW にモデルを繋ぎ、`period_type` をモデル側で選択 | 会社予想と実績の乖離を特徴量化（`feature_disclosure.py`） | 何も足さない |
| 年度粒度 | 保つ（1社1年1行のまま） | 崩さない（別 VIEW） | 崩さない | — |
| `z_*` | TTM 値で再計算すれば整合する | **無い**。H1 側で新規に定義が要る | 不要 | 現状どおり |
| `gap_ratio` | 通期 OLS の予測と粒度がずれる（要再学習） | **無い** | 不要 | 現状どおり |
| `per`/`pbr` | 最新株価 ÷ TTM EPS で作れる | H1 行の市場データ充足率次第（未実測） | 不要 | 現状どおり |
| 実装コスト | **高**（VIEW の全面改修＋会計基準・決算期変更の例外処理） | 中（器はある。波及はモデル・UI の分岐） | 低（VIEW を触らない） | ゼロ |
| 主なリスク | 会計基準変更・決算期変更・連結範囲変更で合成が静かに壊れる | 「どちらを見るか」の分岐がモデルと UI 全体へ波及する | **「最新業績で評価する」という当初要求を満たさない**。かつ 84日の天井 | 鮮度の問題が残り続ける |
| 鮮度の改善 | 最大（半期ごとに前進） | 最大（同上） | 限定的（`today−84日` が天井） | なし |

## Decision

**選択は保留（proposed）。本 ADR で確定させるのは「どう決めるか」である。**

### D1. 実測の前に潰す未確認点（8/18 以降・いずれも読取クエリ1本ずつで済む）

1. **H1 の充足率**: `period_type='H1'` の社数・年数・`period_end` 分布、および `per` / `pbr` / `market_cap` / `bs_total_assets` の非 NULL 率。**ここで `per`/`pbr` がほぼ空なら、案A/案B は「既定 `fin_features` を変える」ことまで含む変更になる**（H1 だけ別の特徴量構成を使うことになり、通期との比較可能性が落ちる）。
2. **決算期変更の頻度**: 同一 `edinet_code` で `period_end` の月が変わった件数。案A の合成が壊れる母数。
3. **(edinet_code, year) の衝突**: annual と H1 が同一 year を持つ件数（VIEW を統合する案では主キー設計に効く）。

### D2. 比較の設計

- **対象サブセット**: H1 が**2期以上**ある社に限定する。H1 を持たない社を含めると差が母集団の希釈で消え、「効かなかった」と「そもそも入っていない」を区別できない。
- **モデル**: M-2（`macro_gbdt`）と M-6（`macro_enet`）。既定 config を `coerce_params({})` で取り、`fin_features` 以外は動かさない。
- **指標**: 買い側 `rank_ic` と売り側 `short_side_spread`（ADR-0018 / ADR-0022 と同じ2軸）。
- **fold**: `walk_forward_cv_monthly(embargo_months=12)`（ADR-0014）。H1 を入れるとラベル窓の重なり方は変わらないが、**`filing_date` を持つのは interim VIEW だけ**なので、案B では point-in-time を `filing_date` で切れる。案A は `period_end + FINANCIAL_LAG_DAYS` の現行規則を引き継ぐ。
- **ゲート**: ADR-0023 / ADR-0028 の昇格ゲート＝`model_stats.paired_ic_significance` の定常ブートストラップで対 base の差を検定し、Bonferroni 補正（2モデル × 2指標 = 4検定なら α=0.0125）。**増減どちらの向きも補正後 α を通ること**を要求する（ADR-0028）。
- **帰無仮説は案D（現状維持）**。どの案も α を通らなければ**案D を accepted にし、「H1 は入れない」という結論と理由を明文化する**。#424 本文が案D を正当な結論として挙げているとおり、これは失敗ではない。

### D3. 実装に進むときの順序

1. 実測は**読取のみ**で行い、VIEW は触らない（案A/案B とも、比較は `build_snapshots` へ渡すパネルを Python 側で組んで代替できる）。
2. VIEW を変更する段階になったら、`init_db()` が**起動のたびに無条件で DROP→再作成する**（`_ensure_one_view`）ことを踏まえ、ローカル API の起動だけで本番へ反映される点に注意する。`--persist` のような安全ゲートは無い。
3. 案C を採る場合は `feature_disclosure.py` の消費者を本番コードパスへ足す。その際 **84日の天井を UI と docs に明記する**（#424 子4）。

## Consequences

- proposed の間、**実装は行わない**。VIEW 変更は本番へ即反映されるため、restricted 中（〜2026-08-18）は特に踏み込まない。
- `financial_metrics_interim` は孤児のまま残る。案A/案C/案D を採る場合は「消費者ゼロの VIEW を残すか削るか」を本 ADR の accepted 時に併せて決める（#219② の資産だが、使われない VIEW は `init_db` の DDL 実行コストと読み手の誤解を生む）。
- 実測が終わるまで「最新業績が分析に反映されていない」という #424 の事実は変わらない。**その間、画面と docs には現状（通期のみ・最大11ヶ月の齢）を書いておく**——鮮度の低さを「壊れている」と誤診させないため（#424 子4 と同じ動機）。
- 本 ADR は `docs/MODELS.md` の分析モデル記述を**まだ変えない**（決定していないため）。accepted 時に MODELS.md と `templates/models.html` をセットで更新する。

## 関連

- Issue #424（親）・#424 子2（本 ADR）・子4（開示の 84日上限の明記）
- #219②（`period_type` / `filing_date` と interim VIEW の導入）・#323（唯一の予定消費者・wontfix クローズ）
- #421（市場データの上書きを annual に絞った経緯）・#322（`statement_disclosure` 収集）
- ADR-0014（purge/embargo）・ADR-0018（OOF 指標）・ADR-0023（昇格ゲート）・ADR-0028（増減どちらも補正後 α）
