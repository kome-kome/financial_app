# DEPLOYMENT.md から移した完了済みの経緯

2026-10-08（#844 の1回目）に [DEPLOYMENT.md](../DEPLOYMENT.md) から移した。**現行の手順の参照には使わない**——現行は DEPLOYMENT.md が正。各節に移す前の場所を書いた。文は移したときのまま（相対リンクだけこの階層に合わせて直した）。

## ローカル PostgreSQL の初回セットアップの結果（2026-08-15）

移す前の場所: DEPLOYMENT.md「ローカル PostgreSQL」→「セットアップ（`scripts/setup_local_db.py`）」の後半。

2026-08-15 の実行結果: 18テーブル ＋ VIEW 2本を生成、`financial_metrics` / `financial_metrics_interim` とも `SELECT` 可、`security_invoker=true` が**非 superuser でも適用できた**、2回目の実行は差分なし。**`sql/financial_metrics_view.sql` の `STDDEV_SAMP` / `::numeric` / 名前付き `WINDOW` 句が PG18 で通ることの実証になっている。**

同日、この空スキーマに対して **Web アプリが起動することも実証済み**（`DATABASE_URL` をローカルへ向けて `uvicorn api:app`）:

| 確認 | 結果 |
|---|---|
| `GET /health` | `200 {"status":"ok","db":"ok"}` |
| `GET /api/stats` | `200`（全件0・`freshness: "empty"`＝データが無いだけで経路は生きている） |
| `GET /` | `200`（ダッシュボード HTML 17,873 bytes） |

`APP_SECRET_KEY` 未設定の警告が出るが、これは開発用既定鍵で継続する正常な挙動（本番相当環境＝`RENDER`/`RENDER_LIGHT_MODE` でのみ起動を停止する）。**つまり Supabase が restricted でもローカルだけでアプリは動く。あとはデータを入れるだけ**（8/18 以降の mirror pull）。

## ミラー: 正本の反転前の定常手順と予行演習（2026-08-16）

移す前の場所: DEPLOYMENT.md「ミラー3本」。正本がローカルへ移った（#503）後、pull / sync は定常運転では使わない。

```powershell
# 通常運転（8/18 以降）
python -m scripts.mirror_verify --level schema      # pull の前に列差分を確認
python -m scripts.mirror_pull                       # ドライラン（見積りと操作予定）
python -m scripts.mirror_pull --apply --allow-full-pull
python -m scripts.mirror_sync --apply               # 以後は増分
python -m scripts.mirror_verify                     # 0=一致 / 1=乖離 / 2=接続不可

# 予行演習（Supabase 不要・実 financial_db に触れない）
python -m scripts.mirror_rehearse --apply
python -m scripts.mirror_rehearse --drop
```

**予行演習には CREATEDB 権限が要る**（`edinet` は既定で持たない・実測 `rolcreatedb=f`）。案は2つある。

**案A（postgres のパスワードが分かる場合）**——superuser で1回だけ付与する。`financial_db` の中身には触れない:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -c "ALTER ROLE edinet CREATEDB;"
```

**案B（パスワードが不明でも可・2026-08-16 の実走はこちら）**——別ポートに使い捨てクラスタを立てる。`initdb` で作るクラスタは **OS ユーザーが bootstrap superuser** になるため、既存クラスタの認証情報も管理者権限も要らず、**既存クラスタ・Windows サービス・`financial_db` に一切触れない**。

```powershell
$PGD = "<使い捨てディレクトリ>"; $B = "C:\Program Files\PostgreSQL\18\bin"
"edinet" | Out-File -FilePath "<pwfile>" -Encoding ascii -NoNewline   # パスワードは argv に載せない
& "$B\initdb.exe" -D $PGD -U edinet -A scram-sha-256 --pwfile="<pwfile>" -E UTF8 --locale=C
Remove-Item "<pwfile>" -Force
& "$B\pg_ctl.exe" -D $PGD -l "<logfile>" -o "-p 5433" -w start

$env:FINAPP_DB_TARGET   = "local"
$env:DATABASE_URL_LOCAL = "postgresql://edinet:edinet@localhost:5433/postgres"
python -m scripts.mirror_rehearse --apply         # コード変更は不要

& "$B\pg_ctl.exe" -D $PGD -m fast stop            # 後片付け（ディレクトリごと削除）
```

`--locale=C` にすると照合順が既存 `financial_db`（`Japanese_Japan.932`）と違うが、突合のチェックサムは**順序非依存**に組んであるので影響しない（ADR-0035）。

## ミラー: 「未了」だった作業（2026-08-19 に完了）

移す前の場所: DEPLOYMENT.md「ミラー3本」の直後の「未了（8/18 の Egress リセット後）」。

実際の pull（コア約300MB）と `mirror_sync` の本番実行。詳細は Issue #481 と復旧当日ランブック（#493）。

**`EGRESS_COST_TABLE` の較正取り直しは完了**（2026-08-19・commit 191481a）＝`mirror_verify --level counts --bytes --warn-only` が表ごとに1行（`count(*)` と `sum(octet_length(x::text))`）返すので、16表を一度に測れた。合計 131.1MB / 1,569,144 行。**この 131.1MB は「全列を転送したらこうなる」という見積りであって実際の転送ではない**（サーバ側集約なので返るのは16行）。

**restricted は 2026-08-19 に解除済み**。判定は #493 の3基準（バナー／Usage を `All projects` で見る／大きい表のスキャン時間）で行うこと——`financial_records`(68MB) の `count(*)` が **0.373秒**（8/10 は 25.9秒、解除前日の 8/19 未明は2分超 timeout）。**MCP `get_project` の `ACTIVE_HEALTHY` は組織のクォータ制限を反映しないので判定に使えない。**

**未検証の前提**: `pg_dump` が Supabase の session pooler（`...pooler.supabase.com:5432`）越しに通るか（transaction pooler :6543 は不可）。通らなければ `--source-url` で direct 接続へ差し替える＝コード変更は不要。

## Supabase が正本だった時代のバックアップ運用

移す前の場所: DEPLOYMENT.md「Supabase（無料プラン）」→「バックアップ運用ポリシー」。**今は使わない**（正本はローカル。Supabase の Postgres へ書き戻す経路は作らない・ADR-0038）。

### 自動バックアップ（Supabase 標準機能）

Supabase 無料プランは **毎日1回の自動バックアップを7日間保持** する（Point-in-Time Recovery は有料プランのみ）。

| 項目 | Free プラン |
|---|---|
| 自動バックアップ頻度 | 1日1回 |
| 保持期間 | **7日間** |
| PITR（任意時点復元） | 非対応（Pro プラン以上） |
| 確認場所 | Supabase ダッシュボード → Project Settings → Database → Backups |

### 手動バックアップ（スキーマ変更・大規模更新前に実施）

重大な DB 変更（`ALTER TABLE`・データ移行・全件再収集）の前は手動バックアップを取得する。

```
# Supabase ダッシュボードから
Project Settings → Database → Backups → "Create Backup"（Pro）
 ↑ Free プランでは不可。代わりに pg_dump を使う：

pg_dump "$DATABASE_URL" \
  --no-acl --no-owner \
  --format=custom \
  --file="backup_$(date +%Y%m%d).dump"
```

`DATABASE_URL` は Render・ローカルの `.env` に設定されている接続文字列（`postgresql://...?sslmode=require`）を使う。

### 復旧手順

**Supabase ダッシュボードから復元する場合（7日以内）**:
1. Supabase ダッシュボード → Project Settings → Database → Backups
2. 復元したい日時を選んで "Restore" をクリック
3. 復元中は DB が停止（数分〜十数分）→ Render の Web サービスも一時的に 503 になる
4. 完了後、`/health` で DB 疎通を確認

**pg_dump バックアップから復元する場合**:
```
# 既存 DB を全消去してから復元（⚠️ 不可逆操作）
pg_restore --clean --no-acl --no-owner \
  -d "$DATABASE_URL" \
  backup_YYYYMMDD.dump
```

## `vacuum-maintenance.yml` の設計記録（Supabase が正本だった時代）

移す前の場所: DEPLOYMENT.md「GitHub Actions workflow 早見表」→「アクティブ」の表の `vacuum-maintenance.yml` の行。定時実行は 2026-08-25 に停止し、正本側の担い手はローカル月次バッチの `vacuum` ステップ（#504）。

`stock_price_daily` の DELETE ベース trim による index bloat 対策（Issue #290）。`_pipeline_vacuum.py` が AUTOCOMMIT 接続で **`TARGET_TABLES`（`stock_price_daily` / `stock_price_weekly`）を1表ずつ** `VACUUM FULL` し、前後の容量をログ出力。**2026-08-19 に対象を2表へ拡大し、前段で per-table の autovacuum チューニング（冪等な `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = 0.02)`）を行うようにした**——`stock_price_weekly`（195MB）に dead tuple が 200,498 行溜まり autovacuum の最終実行が 2026-07-31 で止まっていたが、これは故障ではなく **per-table 設定が無く（`reloptions = null`）クラスタ既定 0.2 が効いて発火閾値 `50 + 0.2 × 1,284,465 = 256,943` 行に一度も届いていなかった**だけ（当時 200,498 行＝その 78%）。**128万行の表に既定のスケール係数 20% が粗すぎる。** 0.02 で閾値は 25,739 行。チューニングだけでは既存の dead は物理サイズを返さず（通常 VACUUM は死領域をテーブル内で再利用するだけ）、VACUUM FULL だけでは翌週また溜まるので**両方要る**。per-table 設定を `init_db()` / `_ensure_tables()` へ入れてはいけない（lifespan が無条件実行するためローカル API 起動だけで本番へ不可逆反映される）。毎週 UTC 23:30・土（JST 08:30・日）自動（#446 で 22:00 から後ろ倒し。**cron の名目時刻ではなくキュー遅延込みの実起動時刻で設計する**——`daily-incremental` は cron UTC 18:00 に対し実起動 19:45Z 前後で、旧設定では夜間チェーン終端と日曜だけ約24分重なっていた）。**#476 で `daily-incremental` が 08:17Z へ前倒しされ、この 22:37Z 前提は解消した**（間隔が大きく開いたので時刻は据え置き＝動かす必然が無いものを動かさない）。手動即時実行は `workflow_dispatch`（ローカル・GitHub Actions 双方で Supabase pooler 経由の正常動作を確認済み・2026-07-12）。**時間帯は #427 で JST 04:00 → 07:00 へ移動**——差分収集（JST 03:00 開始・実測 2h05m〜2h38m）の最中に `VACUUM FULL`（ACCESS EXCLUSIVE ロック）が走っており、ずらす設計意図が成立していなかった。現行チェーンは 03:00 収集 → 最長 05:40 → nightly-scores（`sector_ols` 16分 + M-6・総所要は #443 の初回実走で実測）。**M-6 追加でチェーン後端が伸びるため、日曜だけは VACUUM FULL（07:00）と重なりうる**——ただし `VACUUM FULL` が排他ロックを取るのは `stock_price_daily` のみで、夜間バッチが読むのは `stock_price_weekly`／`financial_metrics` ゆえロック競合はしない（I/O は共有）。実測で 07:00 に食い込むようなら時間帯を再調整する
