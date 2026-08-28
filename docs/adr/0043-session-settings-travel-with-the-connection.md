# セッション設定は接続に付いてくる。接続先の既定値に依存した仮定は例外を出さずに壊れる

## Status

accepted（2026-08-29）。Issue [#565](https://github.com/kome-kome/financial_app/issues/565)。
[ADR-0038](0038-local-postgres-is-the-primary.md)（正本のローカル化）が生んだ3件目の
「接続先を移したら黙って壊れた」であり、[ADR-0035](0035-mirror-endpoints-are-parameterized.md)
がミラー側だけで解いていた問題をアプリ経路へ広げる決定。

## Context

ダッシュボードの「データ鮮度」が `最終更新: 2026-08-29 03:02:56 JST` と表示していた。
夜間バッチは 2026-08-28 17:49 に起動して 19:10 に完走しており、**03:02 には何も走っていない**
（`app_settings` の足跡・タスクスケジューラの `LastRunTime`・`.logs/` のいずれにも記録が無い）。
バッチ直後に開けば「翌日 04:10」という**未来の時刻**が出る状態だった。

### 機構

| 層 | 実際 |
|---|---|
| 列型 | `timestamp without time zone`（naive） |
| Python 側の値 | `datetime.now(timezone.utc)` = **aware UTC** |
| PG が naive 列へキャストするとき | **セッション TZ でローカル時刻へ変換して tz を落とす** |
| ローカル PostgreSQL の TimeZone | **`Asia/Tokyo`**（`pg_settings` のサーバ設定＝全クライアントが継承） |
| 保存結果 | **JST naive** |
| 表示 | `api._utc_to_jst_str` が「UTC 保存の naive」とみなして **+9h** |

Supabase は既定 `TimeZone=UTC` なので、正本が向こうにあった間は仮定が成立していた。
**#503 で正本をローカル PG へ移した瞬間に前提が崩れたが、例外は一切出ない**——
どちらの世界でも「接続でき、書き込みも成功する」。ADR-0038 が `render.yaml` の
prod 明示漏れ（#508）で踏んだのと同型で、**接続先の食い違いは沈黙する**。

結果として同じ列に UTC と JST が混在した（境界は 2026-08-20 の反転）。

### 表示だけの問題ではない

- `routers/market.py` の `days_since_update`（鮮度バッジ・バナー）
- `routers/morning.py` の `_gap_ratio_block` の `age_days`（`GAP_WARN_DAYS=2` / `GAP_ALERT_DAYS=7`）

いずれも**実際より 9 時間新しく見える＝鮮度の判定が甘くなる**。さらに
`nightly_scores._make_score_table_verifier` の `max(created_at) < started_at` 検査は、
9 時間進んだ値のせいで**常に通過する空検査**へ化けていた（書けていなくても気づけない）。

### 正しい実装は既にリポジトリの中にあった

`scripts/mirror_common.py` は 2026-08-16 の時点で `_SESSION_FIXES`（`TimeZone` /
`DateStyle` / `extra_float_digits`）を connect フックで自動適用しており、
**「ローカルは実測で `TimeZone=Asia/Tokyo`」というコメントまで書かれていた**。
同ファイルの docstring は、`table_stats()` の中でしか設定を流していなかったせいで
`mirror_sync` の float8 だけが 15 桁へ丸められていた事故を挙げて、こう結論している——
**「セッション設定は『読む人が思い出す』ものではなく『接続に付いてくる』ものにする」**。

その結論が `database.engine` へ適用されていなかった。**経路ごとに正しさが違う状態**が、
別の値（float8 → 時刻）で再発した。

## Decision

**セッション設定は `database.SESSION_FIXES` を唯一の源とし、`engine` の `connect` フックで
自動適用する。ミラーはそれを再利用する。**

### 1. アプリ経路も接続時に TZ を倒す

```python
SESSION_FIXES = (
    "SET TimeZone = 'UTC'",
    "SET DateStyle = 'ISO, YMD'",
)
```

`event.listens_for(engine, "connect")` で新しい DBAPI 接続ごとに流す。プールの再接続でも
効き、**呼び忘れる余地が無い**。`connect_args` の `options` でも同じ効果は得られるが、
フックにすればミラーと同じ機構に揃い、「張られていること」を `inspect.getsource` で
CI から縛れる。

**ロール既定（`ALTER ROLE ... SET timezone`）や `postgresql.conf` は変えない。**
リポジトリを別マシンへ持っていっても再現する必要があり、DB 側の設定は追随しない。
サーバ既定が `Asia/Tokyo` のままでもアプリは UTC で動く、が正しい形。

### 2. ミラーは共有部を import し、固有部だけ足す

```python
_SESSION_FIXES = database.SESSION_FIXES + ("SET extra_float_digits = 1",)
```

`extra_float_digits` は**ミラー固有**（float8 の完全往復に 17 桁が要る＝チェックサム照合の
前提）。アプリ経路で float の text 表現を変えるのは #565 の範囲外なので共有部には入れない。

### 3. 既存の汚染行は一度きりのスクリプトで引き直す

`scripts/fix_naive_jst_timestamps.py`（既定ドライラン・`--apply` で書く）。

- **対象列は `Base.metadata` から導出する**（`DateTime` かつ `timezone=False`）。一覧を
  書き写すと「表を足したのに直っていない」が静かに起きる（ADR-0031 と同型）。
  `information_schema` も引いて **metadata に無い naive 列を警告として列挙**する。
- **境界は仮定でなく検査にする**。cutoff の直前 24 時間に 1 行でも居たら書かずに終了する
  （`--apply` でも `exit 2`）。実測では pre-flip 最終行 `2026-08-18 21:10:23`（UTC 値）と
  post-flip 初行 `2026-08-20 19:43:32`（JST 値）の間が**全表ゼロ**＝約 46 時間の空白がある。
- **更新は生 SQL**。ORM 経由だと `financial_records.updated_at` の `onupdate` が発火して
  全対象行が現在時刻で潰れる＝直したつもりで壊す。
- **変換式は `(col AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'UTC'`**。JST は DST が無いので
  `- interval '9 hours'` と同値だが、セッション TZ に依存せず意図がそのまま読める。
- **冪等スタンプ**を `app_settings.tz_jst_backfill_applied` へ置く。2 度掛けると 18 時間
  ずれる＝取り返しがつかない。

### 4. 順序を間違えると再汚染する

**コード修正 → 引き直し**の順で走らせる。逆にすると次の夜間バッチがまた JST で書く。

## Consequences

### 得るもの

- 接続先を移しても naive DateTime 列の意味が変わらない。**Supabase では no-op**（元から UTC）
  だが、仮定がコードに書かれること自体が価値（次に接続先を動かす人がここを引ける）。
- `nightly_scores` の `max(created_at) < started_at` が**本来の検査に戻る**。
- 設定の源が 1 つになり、「片方の経路だけ正しい」状態へ戻れなくなる。

### 代償と限界

- **列型は `timestamp without time zone` のまま**。最も正しいのは `timestamptz` への移行だが、
  既存 naive 値を日付で UTC/JST に解釈分岐しながら変換する必要があり、ミラーのチェックサム・
  `pg_dump` バックアップ・復元予行の全経路を再検証しないと安全でない。接続で倒せば再発は
  止まるので、必要になったら別途。**つまりこの ADR は「naive 列に aware 値を書く」構造自体は
  温存している**——安全なのはセッション TZ が UTC に固定されている限りにおいてである。
- 引き直しで**日付を跨ぐ行が 3,469 件**ある（早朝に走った回）。うち `macro_data.created_at`
  の 34 行は、実配信ラグの実測（#447・`docs/GOTCHAS.md`）が **日付**を使うため lag_days の
  実測が 1 日動く。`macro_*_scores` の 3,409 件は as-of 表示にしか使われないので影響しない。
- **CI ではこの再発を捕まえられない**。壊れ方が PG 固有（セッション TZ による naive 列への
  キャスト）で SQLite には再現しようがないため、往復の実測は `FINAPP_TEST_PG_URL` を要求する
  `tests/test_tz_postgres.py` にしか置けず、`ci.yml` では skip される。CI が縛れるのは
  「設定が書いてある・フックが張ってある・ミラーが共有部を包含している」までで、
  **効いていることの確認はローカルでしか取れない**（ADR-0034 が SQLite の `rowcount = -1` で
  同じ限界に当たったのと同型）。

### 監視

`tests/test_db_session_fixes.py`（DB 不要・CI で走る）が守るもの:

1. `SESSION_FIXES` が `TimeZone = 'UTC'` を含む
2. `database` に `listens_for` + `"connect"` + `SESSION_FIXES` のフックがある
3. `mirror_common._SESSION_FIXES ⊇ database.SESSION_FIXES`（二重定義への逆戻りを禁じる）

`tests/test_tz_postgres.py`（`FINAPP_TEST_PG_URL` 設定時のみ）が、aware UTC を naive 列へ
書いて読み戻したとき UTC の壁時計が返ることを実測する。**フック無しの engine では実測で
+9.0h ずれる**ことを確認済み＝このテストは空振りしない。
