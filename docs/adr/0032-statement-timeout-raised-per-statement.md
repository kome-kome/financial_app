# Supabase の statement_timeout は「重い1文の実行中だけ」引き上げる（lock_timeout とセットで）

## Status

accepted（2026-08-09）。Issue #470 / #471 の設計決定。[ADR-0025](0025-training-window-history-backfill.md) が付随記録として残していた「`AccessExclusiveLock` 待ち → `statement_timeout=2min` で殺され、ワークフローが起動直後に落ち続けた」事象を、**恒久的な契約**として一般化する。

## Context

本番（Supabase Free）の `postgres` ロールの既定は 2026-08-09 の実測で次のとおり:

| 設定 | 値 | 意味 |
|---|---|---|
| `statement_timeout` | **2min** | 1文がこれを超えると `QueryCanceled` |
| `lock_timeout` | **0** | ロックは取れるまで無制限に待つ |
| `idle_in_transaction_session_timeout` | **0** | 放置トランザクションも切られない |

2026-08-08 の夜、この 2min が**同じ日に2本の定常ワークフローを落とした**。

| Issue | ワークフロー | 落ち方 |
|---|---|---|
| #470 | `daily-incremental.yml` | Yahoo gap-fill（3,677件）と J-Quants catchup は成功していたのに、最後の `update_market_data_from_history` が **2分16秒**で `QueryCanceled` → 非ゼロ終了 |
| #471 | `vacuum-maintenance.yml` | `VACUUM FULL stock_price_daily` が **きっかり 2分01秒**で `QueryCanceled` |

この2件は単独の失敗より重い意味を持つ。

- **#470 は失敗の波及が広い**。`nightly-scores.yml` / `macro-health.yml` は `workflow_run` の `conclusion=success` で連鎖する（#432 / #420）ため、**Phase 4 の最後の1文が落ちるだけで夜間チェーン全体が起動しない**。収集された株価は正しく入っているのに、朝のランキングだけが更新されない。
- **#471 は容量に直結する**。`stock_price_daily` は DELETE ベースの trim で btree が bloat し続ける（#290）。VACUUM が通らない週は物理サイズが縮まず、実際 DB は 426MB / 500MB（余裕 74MB）まで詰まっていた。

同 run では pooler 枯渇（`FATAL: (ECHECKOUTTIMEOUT) unable to check out connection from the pool after 15000ms in Session mode`）も2回発生し、いずれも1バッチぶんの株価を warning 1行だけ残して捨てていた（収集自体は成功扱い＝#438 と同型の静かな劣化）。

**なぜ今まで通っていたのか**も測った。`VACUUM FULL` の実測は 43〜92MB で **7.6〜10.4秒**（2026-07-18〜08-01 の4回）だったが、2026-08-09 に 79MB を手動実行すると **37.4秒**——同規模で4倍に伸びていた。「前は速かった」は上限に対する余裕の証明にならない（#446 の「所要は据え置かず伸びる」と同じ話）。

## Decision

1. **タイムアウトの引き上げは、その文の実行中だけ行う。** `db_timeouts`（database.py）という単一のコンテキストマネージャに集約し、`with` を抜けるときに必ず `RESET` する。接続はプールへ返って他の処理に再利用されるため、差し替えたまま返すと引き上げが無関係な経路へ漏れる。
2. **ロール既定（`ALTER ROLE`）もプロセス全体の上書きも採らない。** 通常経路（API・分析）の 2min は暴走クエリの安全網として残す。
3. **`lock_timeout` を明示できるようにし、ロックを取る処理では必ず有限値を置く。** 既定の 0（無制限待ち）だと「ロック待ち超過」と「文が重い」が**どちらも同じ `QueryCanceled` として現れ、事後に区別できない**。有限の `lock_timeout` はロック待ちだけを先に諦めさせ、原因を確定させる。
4. **VACUUM の `statement_timeout` は `'0'`（無制限）にする。** 時間による歯止めはワークフローの `timeout-minutes: 30` が持つ。所要が上限へ寄っていく前提では、文側の固定上限は「いつか必ず踏む地雷」になる。ロック待ちで落ちたら `pg_locks` × `pg_stat_activity` で保持者を run ログへ残し、間を空けて再試行する（最大3回）。
5. **保存の失敗は再試行してから諦める。** `_price_collection_driver` は timeout も pooler 枯渇もどちらも一過性として扱い、`PRICE_BATCH_MAX_ATTEMPTS=3`・待ち時間倍化で粘る。使い切ったときだけ従来どおり warning を残して次バッチへ進む（1バッチの取りこぼしで収集全体は落とさない）。**`except` では必ず `rollback()` する**——aborted transaction を持ち越すと1社の失敗が残り全社を道連れにする。
6. **Postgres 以外では no-op。** テストは SQLite で走るため、`SET` を無条件で投げると全件落ちる。値は `'0' / '500ms' / '90s' / '10min'` の書式を正規表現で検証してから連結する（`SET` はパラメータをバインドできない）。

## Consequences

- **良い点**: 引き上げの適用範囲が `with` ブロックとして読める。どの文が 2min を超えうると判断したかがコードに残り、後から棚卸しできる。ロック待ちは原因がログで確定する。
- **代償**: 引き上げた区間では、本当に病的なクエリも 10分（VACUUM は無制限）まで走り続ける。歯止めはワークフローの `timeout-minutes` に一元化されるため、**新しいワークフローを足すときは `timeout-minutes` を必ず現実的な値で置く**必要がある。
- **限界**: これは「2min では足りない」への対処であって、**遅さそのものを直してはいない**。gap-fill が 4,437社 × `YAHOO_STOCK_RATE_SLEEP=0.5秒` で2時間超かかる構造（`gap_days=0` で土日は全社が対象になる）は別途 Issue として残す。所要が伸び続ければいずれ 10min も足りなくなるので、`HEAVY_STATEMENT_TIMEOUT` を上げる前に**なぜ遅いか**を測ること。
- **検証**: `tests/test_db_timeouts.py`（SET/RESET の順序・SQLite no-op・書式検証）、`tests/test_pipeline_vacuum.py`（ロック待ち再試行・非ロックエラーは即送出）、`tests/test_collector_prices.py::TestPriceBatchRetry`（再試行と rollback）。いずれも DB へ繋がない。#471 は 2026-08-09 に本番実走で確認済み（37.4秒・79MB→49MB・DB 426MB→395MB）。

## Considered Options

- **`ALTER ROLE postgres SET statement_timeout`（サーバ側で恒久変更）**: 1行で済むが、Render のアプリ接続を含む**全接続**に効くため、UI から踏まれた重いクエリが 30秒のリクエスト上限を超えても DB 側で止まらなくなる。コードに痕跡が残らず、次に読む人が「なぜ 2min を超える文が通るのか」を追えない点も却下理由。
- **バッチプロセス起動時に engine の `connect` イベントで一律 SET**: 書き漏れが起きない利点はあるが、パイプライン内の**あらゆる**クエリが引き上げ対象になる。#411 のような放置トランザクションが 2min で自壊せず3時間滞留する種類の事故を、むしろ助長する。
- **チャンクをもっと小さくして1文を軽くする**: `BULK_UPDATE_CHUNK` を下げれば1文は軽くなるが、往復回数が増えて GHA↔Supabase のレイテンシに比例悪化する（#464 でまさに 143.1分 → 56秒へ改善した方向を逆行させる）。VACUUM FULL には分割という選択肢自体が無い。
- **失敗したら次の定時に任せる（再試行しない）**: `daily-incremental` は毎日走るので一見成立するが、失敗した日は夜間チェーンが起動せず**朝のランキングが前日のまま**になる。#470 の実害はそこにあるため却下。
