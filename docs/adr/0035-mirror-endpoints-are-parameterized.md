# ミラーはエンドポイントを引数で受け、書き込み先はローカルに限る

## Status

accepted（2026-08-16）。Issue #481 B-2〜B-4 の設計決定。
B-0（器の作成・PR #491）と B-1（接続先スイッチ・PR #492）の続き。
[ADR-0034](0034-client-side-egress-ledger-and-circuit-breaker.md)（Egress 台帳とブレーカ）と
[ADR-0032](0032-statement-timeout-raised-per-statement.md)（上書きは局所・既定は変えない）を前提とする。

## Context

Supabase が Egress 超過で restricted になるとアプリも分析も一切動かせない。2026-07 と 2026-08 に
2回発生し、2回目は 8/10 から 8/18 まで**8日間まるごと停止**した。#481 はその恒久対策として
ローカル PostgreSQL に読取レプリカを置く。残るのは 3 本のスクリプトである。

| | 役割 | Supabase への依存 |
|---|---|---|
| B-2 `mirror_pull` | 初回一括（`pg_dump` -> `pg_restore`） | 接続 URL 1つ |
| B-3 `mirror_sync` | 増分同期 | 接続 URL 1つ |
| B-4 `mirror_verify` | 突合 | 接続 URL 1つ |

ここで問題になるのは、**書いた時点では Supabase に繋げない**（復旧は 8/18）という制約である。
素直に「source は本番、dest はローカル」と決め打ちで実装すると、
**8/18 に初めて実行して初めて動作が分かる**。そこで落ちれば、復旧の翌日から
また調査が始まり、次の課金サイクルまで待つ事態にもなりうる。

しかも B-2 には確実に踏みそうな地雷が事前に分かっていた。`edinet` は superuser でないため
`pg_restore --disable-triggers` が使えず、**FK はテーブル順序でしか満たせない**。

## Decision

### 1. 3本とも source / dest を引数で受ける。両方ローカルを指せば予行演習になる

```
本番（8/18）: --source prod --dest local
予行（今日） : --source-url .../financial_db_rehearsal_src --dest-url .../financial_db_rehearsal_dst
```

**Supabase へ触れる箇所が「接続文字列1つ」に閉じる**ので、それ以外は今日すべて実証できる。

`--source-url` / `--dest-url` は運用上も正当な引数（別のミラーへ流す・復旧時に接続先を差し替える）
であり、**テスト専用の分岐を本番コードへ持ち込まない**。同一 DB 内の別スキーマを source にする案は
採らなかった——`pg_restore` にスキーマ書き換え機能が無く、テキストを sed で書き換える経路を
検証することになって「本番と違う経路を検証する」ため意味が消える。

予行用の DB は**専用の2本**（`financial_db_rehearsal_src` / `_dst`）を作り、`--drop` で捨てる。
「TRUNCATE で掃除する」ではなく器ごと捨てるので、合成データが実ミラーへ残る経路が構造的に無い。

### 2. 書き込み先はローカル限定（`guard_dest_local`）

ミラーは Supabase を正本とする読取レプリカであり、**逆向きに書く用途は存在しない**。
`--dest` が localhost / 127.0.0.1 でなければ `SystemExit` で止める。
「ローカルのつもりで本番へ流し込む」を構造的に不可能にする。
加えて `source == dest` も拒否する（予行でも別 DB を使う設計なので例外フラグは要らない）。

エンドポイント解決は `database.resolve_database_url()`（B-1 で純関数化済み）へ委譲する。
ガードを二重に実装すると、片方だけ直したときに「ローカルのつもりで本番」が復活する。

### 3. `pg_dump` はブレーカで止められない。歯止めは事前見積り

ADR-0034 のサーキットブレーカは engine の `after_cursor_execute` に張ってあるので、
**SQLAlchemy を通らない `pg_dump` の転送は途中で止められない**。したがって:

- 事前にサーバ側 `sum(octet_length(...))` で見積もる（表ごとに1行＝Egress ほぼ 0・#446 の測り方）
- 見積りが閾値を超えたら `--allow-full-pull` を要求する
- `LEDGER.record_external()` は**事後の記帳**であり歯止めではない
- 正当な pull が自分のブレーカに引っかからないよう、`egress_budget()` で局所的に予算を上げる
  （ADR-0032 の `db_timeouts` と同じ「上書きは局所・既定は変えない」）

**`--compress=0` で dump する。** custom 形式は既定でローカル側 zlib 圧縮するが、
ワイヤ上を流れるのは非圧縮の COPY ストリームである（libpq の通信圧縮は既定 OFF）。
圧縮したままだとファイルサイズが実 Egress を大幅に過小申告し、台帳に嘘の数字が載る。

### 4. restore は1表ずつ、FK 依存順に流す

**ダンプの TOC は `--table` の指定順ではなくアルファベット順である**（2026-08-16 実測）。

```
--table=stock_price_weekly --table=financial_records --table=companies --table=statement_disclosure
  -> TOC: companies / financial_records / statement_disclosure / stock_price_weekly
```

つまり 1 回の `pg_restore` に 16 表をまとめて渡した場合、FK を満たせているのは
**`companies` の頭文字が c で子表より先に来るという偶然**にすぎない。表名が変われば黙って壊れる。
順序を `Base.metadata.sorted_tables`（FK から算出される位相順）で明示し、1表ずつ流す。

`--jobs`（並列）は順序を崩すので使わない。`--disable-triggers` は非 superuser では使えない。

**TRUNCATE も `CASCADE` を使わず明示列挙する。** `companies` を空にするには、それを参照する
`stock_price_daily`（ミラー範囲外）も同時に TRUNCATE される必要がある。`CASCADE` で済ませると
「ミラー範囲外の表が黙って消える」ことがコードから読めなくなるので、メタデータから機械的に
洗い出して名前で並べ、ドライランで件数付きに予告する。

### 5. 週次のオーバーラップは `DAILY_WINDOW_DAYS` から導出する（27週）

`_recompute_weeks_from_daily`（database.py）は `record_prices_batch` が触れた週を
**daily の保持窓ぶん遡って再集約 upsert** する。つまり週次バーは最大 `DAILY_WINDOW_DAYS=183` 日前まで
黙って書き換わる。Issue #481 / #480 の当初案「末尾8週」では 56 日しか覆えず**取り落とす**。

`WEEKLY_OVERLAP_WEEKS = ceil(DAILY_WINDOW_DAYS / 7)` = 27 週（189日 >= 183日）と**導出**する。
定数を直書きすると、保持窓を広げたときに黙って不足する。

同じ理由で `macro_data` / `statement_disclosure` も日付列の高水位＋90日のオーバーラップにする。
この2表は `created_at` を持つが、**upsert の `set_` に含まれていないため値の訂正で進まない**。

### 6. 突合は3段（`--level schema` / `counts` / `checksum`）

| level | 見るもの | 検出できるもの |
|---|---|---|
| `schema` | `information_schema.columns` | 列集合と型の乖離。**pull の事前確認** |
| `counts`（既定） | `count(*)` / `max(キー)` | 件数ずれ・同期の遅れ |
| `checksum` | 全行の md5 集約 | **過去行の値だけの訂正** |

`schema` を pull の前に置く理由: `pg_dump --data-only` は source の列リストで `COPY t (a,b,c)` を吐く。
source にしか無い列は restore が落ちて気づけるが、**dest にしか無い列は黙って NULL のまま残る**。

チェックサムは **順序に依存しない形**にする:

```sql
SET TimeZone = 'UTC'; SET DateStyle = 'ISO, YMD'; SET extra_float_digits = 1;
SELECT coalesce(sum(('x' || substr(md5(x::text), 1, 8))::bit(32)::bigint), 0) FROM t x;
```

- `string_agg(... ORDER BY pk)` は 1.28M 行でソートと巨大な文字列連結が要るうえ、
  **照合順の違い**（ローカルは `Japanese_Japan.932`・Supabase は通常 `C`）で両端がずれる
- **セッション設定の固定が生命線**。ローカルは実測 `TimeZone=Asia/Tokyo` で、Supabase はほぼ UTC。
  固定しないと `app_settings.updated_at`（唯一の timestamptz）だけで恒常的に不一致になり、
  **「指紋が常に赤い＝誰も見なくなる」**という最悪の劣化になる

## Consequences

### 得たもの

- **B-2〜B-4 の動作が 8/18 を待たずに実証できる。** 予行演習（`scripts/mirror_rehearse.py`）が
  pull -> 突合一致 -> 遡及訂正を注入 -> 乖離検出 -> 同期 -> 再び一致 までを回す。
  実 pull だけが 8/18 に残る。
- **FK 順序が効いていることを「逆順で流すと FK 違反になる」で示せる**（`tests/test_mirror_postgres.py`）。
  正順で通ったことだけでは「順序が実は無関係だった」ケースと区別できない。
- ミラーが本番へ書く経路をコードとして持たない。

### 払うもの

- 予行演習に **CREATEDB 権限が要る**（`edinet` は既定で持たない）。superuser で1回
  `ALTER ROLE edinet CREATEDB;`。無い場合はスクリプトがコマンドを表示して止まる。
- restore が表ごとのプロセスになるため、**16 表を跨いだ原子性が無い**。
  そこは `mirror_pull` が最後に突合し、その結果を自分の終了コードにすることで埋める。
- `checksum` は両端でフルスキャンになるので、Supabase では `statement_timeout=2min`（ADR-0032）に
  当たりうる。既定を `counts` にし、`checksum` は明示指定にした。

### 8/18 まで確定しないこと

- `pg_dump` が Supabase の session pooler（`...pooler.supabase.com:5432`）越しに通るか。
  transaction pooler（:6543）は session を要求する pg_dump では使えない。
  **`--source-url` で差し替えられるので、通らなければコード変更なしで direct 接続へ倒せる。**
- 実ダンプサイズと `--compress=0` 前提（ワイヤ非圧縮）の妥当性。
  ダンプサイズと Supabase の Usage 差分を突き合わせれば検証できる。
- Supabase 実スキーマとの列差分（`--level schema` の実出力）。
- `EGRESS_COST_TABLE` の較正取り直し（#478）。

## Considered Options

### 週次を「バケット指紋の不一致だけ再取得」にする案（不採用・記録として残す）

月単位のバケットで `(count, hash)` を突き合わせ、**不一致バケットだけ**取り直す案。
定常日は「今月」だけが不一致になるので転送はさらに小さくなり、183 日より古い訂正
（#465 の分割段差修復がこれ）も拾える。固定窓より原理的に強い。

採らなかったのは、**source 側で毎回フルスキャンの集約が走る**ためである。
`stock_price_weekly` は 1.28M 行あり、Supabase の `statement_timeout=2min` に当たる可能性が
実測できていない。27週の固定窓なら 1 回あたり約 12 万行（約 4MB）で、月30回でも枠の 2.4%
に収まる。**8/18 に `--level checksum` の所要が実測できた時点で再検討する**（Issue 化済み）。
