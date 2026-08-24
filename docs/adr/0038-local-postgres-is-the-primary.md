# 正本はローカル PostgreSQL。Supabase は閲覧用の断面とバックアップの置き場へ降格する

## Status

accepted（2026-08-20）。Issue #503。[ADR-0035](0035-mirror-endpoints-are-parameterized.md) が
用意したローカルレプリカの**役割を反転**させる決定で、#500（NANO のメモリ）を構造的に解く。

## Context

Supabase 無料枠の障害でサービスが止まったのは 2026-07 と 2026-08 の2回、通算2週間を超える。
2回目の真因は Egress ではなかった:

- DB 409.8MB に対し **NANO の実効メモリは 408MB**。載り切らず swap 442.54MB（108.36%）。
- `Disk IO Budget 100%` は症状であって原因ではない（IOPS 3.0%・帯域 9.9% とどちらも余っていた）。
- 2026-08-19 16:19:52Z に **OOM クラッシュ**（"not properly shut down" ＋ WAL の `unexpected pageaddr`）。
- VACUUM FULL で 409.8 → 約380MB まで戻したが、**余裕は 28MB しかない**。

ここが決定的で、**週次株価は毎週増える**（`stock_price_weekly` は追記専用で trim しない）。
つまり余裕は自然には回復せず、時間とともに必ず食い潰される。2026-08-20 の実測でも既に
395MB まで戻っていた（余裕 13MB）。Supabase を正本に置く限り、同じ停止が周期的に再発する。

一方でローカル側はこの1か月で揃っていた（#481 B-0〜B-4）: 器・接続先スイッチ
（`FINAPP_DB_TARGET`）・ミラー3本・初回 pull。**足りなかったのは「どちらが正本か」の決定だけ**
だった。ローカルには容量制約もメモリ制約も Egress もない。

## Decision

### 1. 正本をローカル PostgreSQL へ移す

収集・分析・閲覧をローカルで完結させる。2026-08-20 の最終フル pull（17表・152.8MB・checksum
16表一致）を**引き渡し点**とし、以降 Supabase の内容は更新しない。

`resolve_database_url()` の既定を `prod` → **`local`** へ反転した。正本が移った以上、
「環境変数を触らなければ Supabase」は危険側の既定になる——収集も分析も pytest も既定で
正本の外を叩き、内容が黙って分岐する。反転でガードの向きが正しくなり、**明示的に `prod` と
書いた人だけが Supabase へ触れる**。実質 `render.yaml` の1箇所だけである。

### 1b. Render だけは既定を反転させない（`RENDER` 検知）

`render.yaml` の `envVars` に `FINAPP_DB_TARGET: prod` を書いたが、**既存サービスが Blueprint
管理下にない場合その値は反映されない**（ダッシュボード側の設定が正）。反映漏れのまま既定の
local へ落ちると、Render は localhost を見て「接続失敗」ではなく**空の DB に繋がって 0 件**に
なる——#481 B-0 で一度踏んだのと同型の、起動はするが中身が無い壊れ方である。

`RENDER` は Render が必ず `true` で設定する（[Default Environment Variables](https://render.com/docs/environment-variables)）
ので、`FINAPP_DB_TARGET` 未設定時のフォールバックに使う。優先順は **明示 > `RENDER` 検知 > 既定（local）**。
明示指定を最優先に保つのは、Render 上でローカルを見たい検証を塞がないため。開発機・CI には
`RENDER` が無いので既定の local のまま影響しない。

本番の `/health` は 200 / `db:ok` を返すが、`/api/system/info` は認証必須で**どちらの DB を
掴んでいるかは外から確認できない**。確認できない以上、設定漏れでも安全側へ倒れる作りにしておく。

### 2. `stock_price_daily` をミラー範囲へ入れる

「183日ローリングで再構成できるから 300MB 枠を割く価値がない」（ADR-0035）は
**Supabase が正本だったときの理屈**。正本が移れば daily はローカルにしか無い正本データになる。
しかも `_recompute_weeks_from_daily` の入力かつ gap-fill の基準なので、空のまま収集を始めると
全社が183日窓を Yahoo から引き直すことになる。

副次的に、ミラー範囲が FK 閉包になり「範囲外の表が TRUNCATE に巻き込まれる」経路が消えた。

### 3. 駆動主体を GHA から Windows タスクスケジューラへ移す

GHA はクラウドで走るのでローカル DB へは書けない。cron 3本（tune-hyperparameters /
macro-beta-inference / recommend-factor-premia）を停止し、`daily-incremental` は #477 の
一時停止から恒久停止へ切り替えた。`nightly-scores` と `macro-health` は daily の
`workflow_run` チェーンなので連動して止まる。

残すのは `ci.yml`（pytest）・`vacuum-maintenance`（Supabase の保全）・`egress-health`
（Render 経由の消費監視）・`notify-failure`（残す4本の失敗検知）。

> **2026-08-25 に `vacuum-maintenance` も止めた**（下の追補・決定3）。定時で生きているのは
> `egress-health` / `ci` / `notify-failure` の3本。

ローカル側の入口は `scripts/run_nightly.py`（実体）＋ `run_nightly.ps1`（起動口）。
収集は **`_pipeline_incremental.py`** を呼ぶ——`collector.py --incremental` が回すのは
`run_full_collection` だけで**株価を1バイトも更新しない**（2026-08-20 に実測）。

### 4. Supabase は「閲覧用の断面」と「Storage のバックアップ置き場」

- **Postgres**: 2026-08-07 の断面で凍結。Render の閲覧用に残すだけで、書き戻さない。
- **Storage**: `pg_dump`（`--compress=9`）を表ごとに置き、世代管理する。

**Postgres へ書き戻す経路は作らない。** これにより `mirror_common.guard_dest_local()` の
「dest はローカル限定」（ADR-0035）を**そのまま維持できる**——ローカルから本番 DB へ書く
コードは反転後も存在しない。Storage はオブジェクトストアなので、DB のサイズにも swap にも
触らず、#500 の再発要因を増やさない。

## Consequences

### 得たもの

- **容量・メモリ・Egress の制約から外れる。** 395MB/408MB の綱渡りが終わる。
- **#501（`mirror_sync` 経由の float8 15桁丸め・原因未特定）が構造的に無効化される。**
  Supabase から引く経路自体が不要になり、最終取り込みは `pg_dump` 経路（丸めを受けない）で
  済ませた。
- **検証の反復が無料になる。** 過去2回の超過の主因はローカル検証の本番フルロード反復だった。

### 失ったもの・引き受けたリスク

| 失うもの | 引き受け方 |
|---|---|
| **PC が起動していないと回らない** | タスクスケジューラの `StartWhenAvailable` で見逃した回を次回起動時に追いつかせる。収集は元々 gap-fill で欠測を埋める設計（#474） |
| **GHA の notify-failure が使えない** | `run_nightly.py` が失敗時に `gh issue create` で起票。ただし**通知の失敗はバッチを落とさない** |
| **「走らなかった」が静かに起きる** | `app_settings` へ `nightly_last_run` / `nightly_last_success` を残す。鮮度そのものは `/api/morning` の as-of ブロック（#416/#417）が見る |
| **Render の表示が古くなる** | 承知のうえ。閲覧専用の窓と位置づけ、`render.yaml` に `FINAPP_DB_TARGET=prod` を明示（無いと localhost を見にいき「空の DB に繋がって0件」に化ける） |
| **単一障害点がこの PC になる** | Storage への世代バックアップ（Phase 3）と、四半期ごとの復元予行がこれに対応する |

### 「登録があること ≠ 動いていること」への手当て

ADR-0031 と同じ穴が、今度は逆向きに開く——**cron を止めても failure は出ない**ので
notify-failure でも macro-health でも拾えない。`tests/test_workflow_schedule_pauses.py` を
新設し、停止中の schedule に「理由・復旧条件・代替経路」を書くことを CI で強制した。
検出ゼロで全件パスする事故を防ぐため、停止中が1本も見つからない場合も落ちる。

`nightly_scores.HEAVY_AUTOMATION` は現在も GHA のワークフロー名を値に取る。ローカル駆動を
語彙として表現できていないので、**月次バッチをローカルへ移す時点で（#503 Phase 2 の残り）
レジストリ側も直す**必要がある。それまでは「登録はあるが cron は止まっている」状態である
ことをここに明記しておく。

## 追補: 断面をどこまで保守するか（2026-08-25・#505 / #290）

反転から数日ぶんの実測が出たので、保留していた2つの判断をここで確定させる。

### 実測: 断面はほとんど読まれていない

反転1日後（2026-08-21）の請求サイクル累計は **409.9MB / 5.00GB（8.0%）**。ローカル台帳
（`.egress/ledger.jsonl`）のジョブ別内訳では `mirror-final-pull` 152.9MB ＋ `mirror-pull`
138.5MB ＋ `mirror-sync` 118.2MB ＝ **409.6MB がミラー移行ぶん**で、累計との差は **0.3MB**。
つまり **Render の閲覧を含めても、移行以外で Supabase を読んでいるものはほぼ無い**。
反転前が 7.312GB / 5GB（146%・課金制限）だったことと比べると、**Egress は判断材料として
成立しなくなった**。

### 決定1（#505）: Supabase Postgres は軽くしない

余裕 13MB（実質 430MB > NANO 実効メモリ 408MB）は解消しないまま残すが、**読まれないので
実害が出ない**。検討した3案のうち:

- **A. 何もしない（採用）** … コストゼロ。物理サイズの頭打ちは必要なときに手動で打つ（決定3）
- B. `stock_price_daily`（49MB）を落とす … Render の日次ズームを失うのに、430MB > 408MB を
  解消できる保証が無い。「読まれていないデータを削って、読まれていない DB を速くする」作業
- C. Render を止める … #423 の選択肢と同じ判断になるので、そちらで決める話

**再検討トリガー**: #423 の結論が「Render を最新に保つ」へ倒れたとき。そのときは
「Supabase へ書き戻す経路を作らない」（上の決定4）の是非からやり直しになる。

### 決定2（#290）: パーティション化はしない

#290 は「`stock_price_daily` の DELETE ベース trim が btree を bloat させ、500MB 枠を圧迫する」
問題で、再オープントリガーを **「DB ≥ 430MB でパーティション化へ」** と定めていた。
**この 430MB は Supabase のストレージ枠に対する値**であって、反転後はどちらの側にも当てはまらない:

- **正本（ローカル）には 500MB の崖が無い**。超えた瞬間 read-only になる、という前提ごと消えた
- **断面（Supabase）は 2026-08-07 で凍結**＝書き込みが無いので bloat も増えない

よって**トリガーを正本側の数字へ読み替えて延命しない**（ローカルは現在 807MB で、うち
`legacy_stock_price_history_2026_02` が 478MB。これは別の話として扱う）。bloat 対策は
per-table の `autovacuum_vacuum_scale_factor = 0.02` ＋ 月次 `VACUUM FULL` で足りる。

### 決定3: VACUUM の担い手はローカル月次1本にする

`vacuum-maintenance.yml`（週次・UTC 土 23:30）の schedule を停止し、`workflow_dispatch` だけ残す。
凍結した断面へ毎週 `VACUUM FULL` を打っても、初回以降は何も回収しない実行を繰り返すだけになる。
正本側は `scripts/run_monthly.py` の先頭ステップ `vacuum`（#504・ADR-0040）が同じ
`_pipeline_vacuum.py` を回す。

**初回のローカル実走（2026-08-25・この ADR を書くにあたって測った）**:

| | before | after | 所要 |
|---|---|---|---|
| `stock_price_daily` | 59MB | 49MB | 4.0秒 |
| `stock_price_weekly` | 188MB | 165MB | 9.4秒 |
| DB 全体 | 840MB | 807MB | 計 13.4秒 |

**ここで分かったことが1つある**: ローカル正本の2表は `reloptions = null`＝**per-table の
autovacuum 較正が入っていなかった**。#290 で Supabase へ 2026-08-19 に適用した `ALTER TABLE`
は**ミラー移行で運ばれず**、クラスタ既定 0.2（130万行の weekly で発火閾値 ≈ 259,558 行）の
まま5日間動いていた。今回の実走で 0.02 が入った——**「本番へ入れた対処」は正本が移った先へ
自動的には付いてこない**。

なお月次の予算 `BUDGET_MIN["vacuum"] = 45分` に対し実測は 0.25分だが、**値はここでは動かさない**。
予算配分は 9/1 の実走で他ステップとまとめて較正する（#532）。

## Alternatives considered

- **Supabase 正本のまま容量を削る。** `stock_price_daily`（49MB）を削るなどで一時的に余裕は
  作れるが、週次株価の増加は止まらないので**時間稼ぎにしかならない**。#290 で同じ判断を
  一度しており、そのときのトリガー（DB ≥ 430MB でパーティション化へ）にも近づいている。
- **有料プランへ上げる。** 制約は消えるが、このプロジェクトはローカル実行が主で Render は
  ほとんど使っていない（2026-08-04 時点の実態）。払う先が実際の利用と合っていない。
- **バックアップも Supabase Postgres へ restore する。** 「いつでも切り戻せる生きた DB」は
  魅力だが、ADR-0035 のガードを反転する改修が要り、395MB/408MB の綱渡りも続く。
  Storage なら容量・メモリの両方から外れる。

## References

- Issue #503（親）／ #500（NANO のメモリ）／ #501（float8 の丸め）／ #477（Egress 超過）
- Issue #505（断面を軽くするかの判断＝追補の決定1）／ #290（`stock_price_daily` の bloat＝決定2・3）／ #532（月次ステップ予算の実測）
- [ADR-0040](0040-batch-window-is-split-into-step-budgets.md)（バッチ窓のステップ予算・`vacuum` はその先頭）
- [ADR-0034](0034-client-side-egress-ledger-and-circuit-breaker.md)（Egress 台帳）
- [ADR-0035](0035-mirror-endpoints-are-parameterized.md)（ミラーのエンドポイント引数化・**ガードは維持**）
- [ADR-0036](0036-weekly-prices-incremental-load.md)（週次株価の差分ロード・ローカルでも有効）
- [ADR-0037](0037-egress-cycle-budget-is-a-second-axis.md)（Egress の2軸・Render 経由ぶんに縮小）
- [ADR-0031](0031-heavy-plugins-require-registered-automation.md)（登録 ≠ 実行）
