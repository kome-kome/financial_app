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
- [ADR-0034](0034-client-side-egress-ledger-and-circuit-breaker.md)（Egress 台帳）
- [ADR-0035](0035-mirror-endpoints-are-parameterized.md)（ミラーのエンドポイント引数化・**ガードは維持**）
- [ADR-0036](0036-weekly-prices-incremental-load.md)（週次株価の差分ロード・ローカルでも有効）
- [ADR-0037](0037-egress-cycle-budget-is-a-second-axis.md)（Egress の2軸・Render 経由ぶんに縮小）
- [ADR-0031](0031-heavy-plugins-require-registered-automation.md)（登録 ≠ 実行）
