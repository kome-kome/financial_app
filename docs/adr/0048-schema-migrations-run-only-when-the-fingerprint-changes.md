# スキーマ移行は指紋が変わったときだけ打つ

## Status

accepted（2026-09-02）

## Context

`init_db()` は**呼ばれるたび無条件に DDL を打っていた**。呼び出し元は4つあり、
どれも「起動の副作用」としてこれを走らせる:

| 呼び出し元 | 頻度 |
|---|---|
| `api.py::lifespan` | **画面を開くたび** |
| `_pipeline_incremental.py` | **毎晩 17:20**（日次バッチの pipeline ステップ） |
| `_pipeline_gh.py` | GHA の全件収集 |
| `scripts/setup_local_db.py` | 初期構築 |

打っていた DDL は冪等だが、**冪等であることとロックを取らないことは別**である。

- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` は**列が既に存在しても ACCESS EXCLUSIVE を取る**
  （Postgres はロックを取ってから存在を確認する）
- `ALTER TABLE financial_records DROP COLUMN IF EXISTS ...` も同様
- `_ensure_one_view()` は「定義変更時のみ DROP+再作成する（毎起動 DROP を避ける）」と
  docstring に書いていたが、判定は `pg_get_viewdef()`（Postgres が正規化して返す文字列）と
  **手書きの SQL 定数**の比較で、両者はまず一致しない。実測すると
  **両 VIEW とも毎回 DROP+CREATE していた**＝設計意図はコメントにしか存在しなかった

### 実測（2026-09-02）

M-1 探索（`scripts/run_monthly_m1.py`）の実行中に `pg_locks` を覗いた:

```
pid 11576  state = idle in transaction   トランザクション経過 2:56:33
  financial_records     table  AccessShareLock
  companies             table  AccessShareLock
  plugin_tuned_params   table  AccessShareLock
  financial_metrics     VIEW   AccessShareLock
```

**探索は1本のトランザクションを実行中ずっと開いたままにしている。** この状態で
`init_db()` を呼ぶと、最初の `ALTER TABLE` か `DROP VIEW` が ACCESS EXCLUSIVE を待って
**返らない**。さらに Postgres は待機中の ACCESS EXCLUSIVE の**後ろに後続のロック要求を並べる**
ので、同じ表に触る新しいクエリがすべてその後ろで詰まる。

### 無人で起きる経路がある

M-1 専用タスクは毎月2日 01:00 開始・予算900分＝**最長 16:07** まで走る。日次は **17:20** に
`init_db()` を打つ。**余裕は73分しかない。**

2026-09-02 の初実走は無人で 09:41 に終わって助かったが、**対話セッションを開いたまま**の
ペース（3.28分/件）では 16:24 終了の見込みだった＝余裕56分。パネルは毎晩伸びるので所要は伸びる。
重なれば日次の `pipeline` は無言で待ち、予算超過で `exit=124`＝**鮮度の担い手が止まる**。
ログには `[init] init_db() で...` の1行しか残らない。

### 前例（#411）

`scripts/measure_embargo_impact.py` のコメント:

> SELECT だけでもトランザクションは開く。pooler 経由では close() 後もセッションが
> "idle in transaction" で残り、**init_db の ALTER TABLE を殺す（#411 で実害）**

同じ形が既に一度実害を出しており、そのときは**呼び出し側**（`db.commit()` を足す）で回した。
`init_db()` 側は直っていない＝呼び出し元が1つ増えるたびに同じ穴が開く。

## Decision

**`init_db()` は、スキーマ指紋が一致し実体も揃っているときは DDL を1本も発行しない。**

### 1. 指紋ゲート（ロックを取らない）

`_schema_fingerprint()` は、移行を決めている**入力そのもの**から計算する:

1. `inspect.getsource()` で取った `_ensure_tables` / `_ensure_one_view` / `_ensure_view` の**ソース**
2. 関数の外にある DDL 由来の定数（`_NEW_COLS` / `_LEGACY_COMPUTED_COLS` / `_DEBUG_ONLY_COLS`）
3. VIEW 定義 SQL 2本
4. `Base.metadata` の全 (テーブル, 列, 型)

**DDL を書き換えれば指紋は自動的に変わる**ので、「移行を足したがゲートの更新を忘れる」が
構造的に起きない（列挙を二重に持たない）。**DDL 文をリストへ移し替える設計は採らなかった**
——110行の機械的な移動はタイプミスのリスクを持ち込むうえ、`Base.metadata` 経由の
列追加（CLAUDE.md が定める「再分類項目の追加は `FinancialRecord` の列に足すだけ」）を
拾えないため。4 がその経路を拾う。

副作用として**コメントだけの編集でも指紋が変わる**（移行が1回余計に走る）。
安全側なので許容する。`getsource` が失敗した場合も「一致しない」に倒す。

指紋は `app_settings.schema_fingerprint` に置き、次の4点がすべて成り立つときだけスキップする
（**すべて読み取り＝AccessShare しか取らない**）:

1. 保存された指紋 == 計算した指紋
2. `Base.metadata.tables` のテーブルがすべて実在する
3. VIEW 2本が実在する（`to_regclass`）
4. VIEW 2本に `security_invoker=true` が乗っている

3 が要るのは、`_ensure_tables()` の `period_end` 移行パスが条件付きで
`DROP VIEW financial_metrics` を打つため——**指紋が一致していても VIEW が無いことがありうる**。
4 が要るのは、`security_invoker` が RLS の前提（#344）で、`ALTER VIEW` は
`_ensure_one_view()` の中でしか打たれないため。

### 2. `lock_timeout`（ハングを失敗へ変える）

移行が実際に必要なときの DDL は、既存の `db_timeouts(conn, lock=...)`（#470/#471・
Postgres 以外では no-op）で囲む。**新しいタイムアウト機構を書かない。**
取れなければ即座に明示的な失敗になり、「無言で待ち続ける」が消える。

### 3. `_ensure_one_view()` の嘘を消す

`pg_get_viewdef()` との比較は削除し、**呼ばれたら必ず再作成する**に単純化する。
「変更時だけ」の責務はゲート（1）が持つので、二重に持たない。

## Consequences

- **定常状態では DDL が1本も出ない**＝画面を開いてもバッチと競合しない。#597 の
  「バッチ実行中は API を起動しない」という運用制約が要らなくなる
- 日次と M-1 が万一重なっても `init_db()` はロックを取らずに素通りする
- **手で DB をいじった場合の自己修復は失われる**（従来は毎回打ち直していたので黙って直っていた）。
  実在確認（2〜4）でテーブル・VIEW・`security_invoker` の欠落は拾うが、
  列の型を手で変えたようなケースは拾わない。**そこは指紋を消して（`app_settings` の行を削除）
  再実行する**運用とする
- 実際にスキーマが変わる回（リリース直後の初回起動など）は従来どおり DDL を打つ。
  そのときバッチが走っていれば `lock_timeout` で**速やかに失敗する**——
  ハングよりは良いが、**移行を含むリリース後の初回起動はバッチの窓を避ける**必要がある
- 指紋は DB ごとに `app_settings` へ持つので、ローカル / Supabase / 復元先で独立に判定される

## Alternatives considered

- **`lock_timeout` だけ足す**: 差分は小さいが、バッチ中に画面を開くと API が起動失敗する
  ままで「使えない」は変わらない。ハングが失敗に変わるだけ
- **指紋ゲートだけ入れる**: 定常状態は守れるが、移行を含む回にバッチと重なると
  従来どおりハングする。**一番診断しにくい回が残る**
- **`init_db()` を起動から外し明示コマンドにする**: 最も筋が良いが、呼び出し元4つと
  Render のデプロイ手順に影響し、**移行の打ち忘れ**という別の「失敗として現れない」形を作る。
  今回は起動時実行を保ったまま副作用を消す方を採る
- **運用文書だけ書く**（バッチ中は API を起動しない）: 無人で重なったときは何も守らない
