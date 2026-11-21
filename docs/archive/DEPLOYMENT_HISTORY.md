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
## Supabase の Egress・容量設計の実測と経緯（2026-08）

2026-10-09（#844 の2回目）に [DEPLOYMENT.md](../DEPLOYMENT.md)「外部サービス制約」→「Supabase（無料プラン）」から移した。正本がローカルの PostgreSQL へ移り（#503・[ADR-0038](../adr/0038-local-postgres-is-the-primary.md)）、夜間バッチは Supabase の Egress を使わなくなった。今も動く仕組み（`db_egress` の台帳とブレーカ・請求サイクル累計・週次株価の差分ロード・列の絞り込みの約束）の説明は DEPLOYMENT.md に残してある。

### Egress 設計: 常時計測（`db_egress`）が要った理由

移す前の場所: 「Egress 設計」→「常時計測」の箇条の1つ目。今の仕組みは DEPLOYMENT.md と [ADR-0034](../adr/0034-client-side-egress-ledger-and-circuit-breaker.md) が正。

- **なぜ要ったか**: 2026-07（61.2GB）・2026-08（7.312GB）の2回とも、超過後に「誰が食ったか」を答えられなかった。当時の計測は `scripts/_cache.py` の HIT/MISS だけで、**夜間バッチ本体・`routers/`・`collector*.py` は完全に無計測**だった。

### Egress 設計: 夜間スコア更新の実測（2026-08-20・#482）

移す前の場所: 「Egress 設計」の請求サイクル累計の後。夜間バッチはローカルで走り、Supabase の Egress を使わなくなった。

夜間スコア更新（`sector_ols` + M-6）の実測。**2026-08-20 の回は正本＝ローカル PostgreSQL に対する実走**で、
台帳（`.egress/ledger.jsonl`・job=`nightly-local`）のテーブル別内訳がそのまま取れる:

| 引くもの | 行数 | 削減前（2026-08-06） | 実測（2026-08-20・#482） |
|---|---|---|---|
| `financial_metrics` VIEW（97列 → 消費36列・#459） | 30,298 | 22.5 MB | **8.76 MB**（`octet_length` 実測は 6.69MB） |
| `macro_data`（`{series: {date: close}}` にしか使わない） | 87,355 | 8.7 MB | **3.50 MB** |
| `macro_beta_loadings`（7列 → 消費4列・#482） | 49,283 | 4.86 MB | **3.41 MB**（実測 3.18MB） |
| `stock_price_weekly`（3列・差分ロード後の定常・#480） | 103,976 | 51.4 MB | **3.34 MB** |
| `financial_records` 最新 annual（69列 → 消費20列・#482） | 4,430 | 2.8 MB | **0.82 MB** |
| `companies` | 8,876 | 0.5 MB | **0.63 MB**（`calls=2`） |
| `regression_results`（sector_ols の**書き込み**） | 3,611 | — | 0.30 MB（`calls=3,623`＝1社1文） |
| **1回あたり合計** | 287,831 | **86.0 MB** | **20.8 MB** |

> **見積り 52.0MB に対し実測 20.8MB**。差の主因は #480（週次差分ロード）で、`stock_price_weekly` が
> 39.3MB → 3.34MB に落ちた（初回だけフルロード）。#459/#482 の列絞りも効いており、
> `financial_metrics` は 22.5 → 8.76MB、`financial_records` は 2.8 → 0.82MB。
>
> **`companies` の `calls=2` が #482 の副産物の確認になっている**——列指定 Row から `relationship` が
> 消えて `record.company.issued_shares` が黙って None になる罠を SQL 側 COALESCE で潰した結果、
> N+1 が JOIN 1本になった（N+1 が残っていれば 4,430 回級の calls が出る）。
>
> 一方 **`regression_results` は `calls=3,623`＝1社1文の書き込み**が残っている。読み取り列の話ではないので
> #482/#489 の対象外だが、所要には効く（→ 別 Issue）。
>
> なお **この測定はもう Supabase の枠を1バイトも使わない**（#503 で正本がローカルへ移った）。
> 「restricted 中は測定自体が枠を食う」という #478 当時の制約は解けている。

### Egress 設計: 列指定への切り替えの経緯（#459・#482）

移す前の場所: 「Egress 設計」の列の絞り込みの箇条。`FIN_LOAD_FIELDS` だけを引く約束と、列を落としたら黙らずに落とす約束は DEPLOYMENT.md に残した。

- **残る最大項だった `financial_metrics` VIEW の全列 22.5MB は #459 で列指定へ切り替えた**（2026-08-10）。`plugins/macro_snapshots.py::FIN_LOAD_FIELDS`（36列＝`FIN_BASE_OPTIONS` の選択肢＋`recommend.METRICS` の非 RUNTIME 列＋突合/メタ6列）だけを引き、軽量 namedtuple `_FinRow` で返す。**削減後の実測は 2026-08-21 に取得済み**＝36列 220.8 B/行（6.69MB / 30,298行）に対し全列 97 は 685.4 B/行（20.77MB）＝**1/3 へ落ちた**（上表）。
- **残る5経路も列指定へ広げた（#482・2026-08-15）**。#459 と #441 以外は全列 ORM ロードのままで、「静かに枠を食う」形でしか現れないため誰も気づかない状態だった。
  - `plugins/sector_ols.py::_load_records` — `sector_load_fields(features)` が選択 features から列を導出（既定10項目なら **69列 → 20列**）。`shares_outstanding` の第2優先（`record.company.issued_shares`・#462）は列指定 Row にリレーションが無く消えるため、SQL 側の `COALESCE(FinancialRecord.issued_shares, Company.issued_shares)` で優先順位を保つ。副産物として `issued_shares` が NULL の社ごとに `companies` を引いていた N+1 が JOIN 1本になる。
  - `plugins/sell_ranking.py` — `SELL_SELECT_COLS`（**97列 → 18列**＝表示9＋VIEW指標6＋`nc_ratio` の入力3）。週次は `week_start >= today − 400日` の下限＋500社チャンク＋3列で、保有20銘柄あたり 0.43 → 0.037 MB。**ユーザーが押すたびに払う経路**なので効き方が日次ジョブと違う（下記）。
  - `plugins/utils.py::get_macro_features` — `macro_data` 11列 → 3列（`_preload_macro_impl` と同じ。非対称の解消）。
  - `database.py::get_macro_beta` — `macro_beta_loadings` 7列 → 4列。加えて `with_loadings=False` を新設した。**4呼び出しのうち3つは `meta` の `selected_factors` だけを見て loadings を捨てていた**ので、そこは転送自体を止める。

### Egress 設計: 他の消費（GHA の夜間スコア更新が定期で動いていた頃）

移す前の場所: 「Egress 設計」の箇条の最後。`nightly-scores.yml` は今は手動実行だけ。

- **他の消費**: `daily-incremental`（収集は主に ingress だが `update_market_data_from_history` の読みがある）・ローカルの `scripts/` 検証（`scripts/.cache/` の pickle キャッシュで反復 pull を抑える・Issue #355）。1.98GB は**このワークフロー単独の値**なので、他を足した余裕で判断すること。

### 週次株価の差分ロード: 導入の理由と Egress の見積り（#480）

移す前の場所: 「週次株価の差分ロード」の冒頭と箇条。決定の正本は [ADR-0036](../adr/0036-weekly-prices-incremental-load.md)。仕組み・世代印・歯止め・緊急停止は DEPLOYMENT.md に残した。

上表の最大項（`stock_price_weekly` 39.3MB）は**毎晩ほぼ同じ行を送り直していた**。1日の増分は約4,400行＝転送の 99.7% が不変データの再送。列を削る（#446/#459/#482）とは別の軸で、**行を送らない**手当てが要った。

| | 転送 | 月30回 |
|---|---|---|
| 従来（毎晩フルロード） | 39.3 MB | 1.98 GB（枠の40%） |
| 差分ロード（定常） | 約 3.7 MB（27週 ≒ 9.3%） | 約 1.06 GB |
| ＋週1回の強制コールド | 39.3 MB × 4 = 157 MB | **実効 約1.11 GB（枠の22%）** |

- **初回は必ずフルロード**。GHA キャッシュが載る翌晩から効く。復帰判断は従来値で行うこと。

### リクエスト経路の Egress: 削減前後の表（#482）と `macro_beta_loadings` の較正

移す前の場所: 「リクエスト経路の Egress（#482）」の段落の後。

| 引くもの | 削減前 | 削減後（見積り） |
|---|---|---|
| `financial_metrics` ユニバース（97 → 18列・4,430行） | 3.45 MB | 0.64 MB |
| `financial_metrics` 保有分（同上・20行） | 0.02 MB | 0.003 MB |
| `stock_price_weekly`（7列全期間 → 3列400日窓） | 0.43 MB | 0.04 MB |
| `macro_data`（11 → 3列） | 1.34 MB | 0.40 MB |
| `macro_beta_loadings`（`with_loadings=False` で転送ゼロ） | 4.86 MB | 0 MB |
| **1リクエスト合計** | **10.10 MB** | **1.08 MB（−89%）** |

`macro_beta_loadings` の B/行は #493（2026-08-19）で実測へ差し替えた：**121.4 B/行（7列・10.5MB / 90,841 行）**。それまでは較正値が無く 12 B/列/行 = 84.0 B/行 の既定を当てており、**実測はその 1.44 倍＝保守側に置いたつもりの既定が過小だった**（上表の 3.36 MB → 4.86 MB はこの比で引き直した値）。`DEFAULT_BYTES_PER_COLUMN` を 17.5 へ引き上げたのはこの実例が根拠。

### 全表較正（#493・2026-08-19）の当時の要点

移す前の場所: 「全表較正」の箇条。明細の正本は `db_egress.EGRESS_COST_TABLE` のエントリと note。

- **16 表・全列の合計は 131.1 MB**（1,569,144 行）。これが **ミラー初回 pull（#481 手順3）の見積りの母数**であり、枠 5GB の 2.6%。1,284,465 行の `stock_price_weekly` だけで 66.6MB＝**半分がここ**
- `financial_metrics` は VIEW でミラー対象外＝この回では未測。#446 の 779 B/行（97列）が引き続き唯一の実測

### 容量設計: 500MB に対する見通しと一回限りの移行（2026-06 完了）

移す前の場所: 「容量設計」の箇条と段落。凍結した Supabase の容量見通しは今は意味を持たない。単一の書き込み口（`record_prices_batch`）の文は DEPLOYMENT.md に残した。

- 見通し：5年分 weekly ≈ 145MB、総計 ≈ 285MB / 500MB、+約37MB/年（runway 約6年）。書き込みは単一チョークポイント `record_prices_batch`（daily upsert→触れた週を weekly 再集約→trim）。

**移行（一回限り・ローカル実行 `migrate_stock_price_dual.py`・2026-06 完了済みでスクリプトは撤去／以下は手順記録）**：満杯DB（≈448MB）で新旧テーブルを併存させると 500MB 超で read-only に墜落するため、**ローカルで集約計算 → 旧テーブル DROP（即解放）→ コンパクトな新テーブルをアップロード** の順で Supabase 側ピークを上げない（[GOTCHAS.md](../GOTCHAS.md) 参照）。

### 容量設計: 実装済みの後続 PR

移す前の場所: 「容量設計」→「後続PR」。未実装の「予測モデルの平滑化ターゲット化」は DEPLOYMENT.md に残した。

- *`financial_records.raw_xbrl_json` の drop*：**実装済み（Issue #219 ①）**。financial_records 73MBの主因＝第2の容量レバーだった列を冪等DROPマイグレーション（`database.py::_DEBUG_ONLY_COLS`）で削除し、ヘッドルームを確保。
- *過去2〜5年の Yahoo 週次バックフィル*：J-Quants 無料は2年上限のため、5年時系列（財務5年と整合）を Yahoo から `stock_price_weekly` へ補填。**実装済み（#198・`backfill_weekly_history_yahoo`）。本番実行は完了済みで、専用の `backfill-weekly-history.yml` は削除した**。現在の呼び出し元は `_pipeline_gh.py`（全件収集）と `python -m scripts.resolve_price_suffix --backfill-weekly`（解決できた社だけ）。

## バッチ暦: 電源オン窓の実測（#551）

2026-10-09（#844 の3回目）に DEPLOYMENT.md「バッチ暦」の箇条から移した。規則（名目時刻は下限・この環境では 17:20 が最速）は DEPLOYMENT.md に残し、根拠の表と `WakeToRun` が効かない理由をここへ移した（同じ事実は [ADR-0042](../adr/0042-batch-footprints-need-a-reader.md) の #551 追補にもある）。

- **名目時刻は「これより前には走らせない」下限であって、実起動時刻の予測ではない（#551）。** 実起動を決めるのは **PC の電源オン窓**で、実測（2026-08-26〜28 の System ログ 6005/6006）は **~17:40 → 翌 ~08:00**。この1つの変数で3タスクすべての挙動が説明できる:

  | タスク | 名目 | 窓との関係 | 実起動 |
  |---|---|---|---|
  | nightly | 17:20 | **外** | 毎日 `StartWhenAvailable` で追いつき、電源投入の 4〜10分後（実測 17:49〜17:53） |
  | monthly | 1日 01:00 | 内 | 名目どおりの見込み（9/1 が初回・#532 が観測） |
  | watchdog | 20:00 | 内 | **20:00:02**＝ずれない |

  **watchdog がずれず nightly だけずれることが、「スケジューラの設定ではなく電源時刻が原因」の決定的な証拠**（設定が原因なら 20:00 も同じようにずれる）。`WakeToRun` を足しても直らない——ウェイクタイマはスリープ／休止からの復帰にしか働かず、**ユーザーが行ったシャットダウンからは起動できない**（電源オフから起こすには BIOS の RTC アラームか Wake-on-LAN が要る）。名目を電源投入後へ寄せるのも現状より遅くなるだけ。**この環境では 17:20 のままが最速**。

## バッチ暦: 正本へのリンクにした箇条（移す前の文）

2026-10-09（#844 の3回目）に DEPLOYMENT.md「バッチ暦」から外した文。どちらも同じ事実が正本にあるので、DEPLOYMENT.md では要点とリンクだけにした（watchdog の閾値と `*_last_run` を見る理由は [ADR-0042](../adr/0042-batch-footprints-need-a-reader.md) の決定1・2、月次タスクの XML 登録は [SCRIPTS_REFERENCE.md](../SCRIPTS_REFERENCE.md#scriptsinstall_monthly_taskps1)）。

- 「走らなかった」ことは **毎日 20:00 の `financial_app-watchdog`（`scripts/check_batch_freshness.py`）が `app_settings` の `*_last_run` を見て起票する**（#515 手順3）。`/api/morning` の as-of ブロック（#416/#417）は人が開いたときの最後の環として残る。**閾値は `cadence + 窓` の導出**（夜間 24h+6h=30h／月次 31日+16h=760h）で、窓を広げれば閾値も自動で広がる＝乖離が原理的に起きない。副産物として「実行中は鳴らない」が構造的に成立する（窓の項がそのまま「まだ走っていてよい時間」の許容）。**見るのは `*_last_run` であって `*_last_success` ではない**——後者は #512 が解けるまで `monthly_last_success` が設計上ずっと古く、そこで鳴らすと恒久的に open な Issue ができて通知そのものが信用されなくなる（成功側は報告と Issue 本文には必ず載る）。
- **月次タスクの登録は XML 直渡し**（`scripts/install_monthly_task.ps1`）。PowerShell の `New-ScheduledTaskTrigger` に `-Monthly` は無く、CIM の `MSFT_TaskMonthlyTrigger` を組んでも `schtasks` の産物を渡し直しても `Register`/`Set-ScheduledTask` が "The parameter is incorrect" で弾く（2026-08-21 に実測）。**しかも非終了エラーなので `$ErrorActionPreference=Stop` でも止まらず「登録しました」と嘘が出る**ため、登録後に `Export-ScheduledTask` で日・上限・`StartWhenAvailable` を読み直して検証している。

## ワークフロー: 月次3本を削除した理由（#504）

2026-10-09（#844 の3回目）に DEPLOYMENT.md「バッチ暦」の箇条から移した。`HEAVY_AUTOMATION` の箇条は、今の規則（`local:` を指す・yml を指す登録は CI が schedule まで確かめる）を DEPLOYMENT.md に残し、#504 で語彙を足した経緯ごと元の文をここへ移した（正本は [ADR-0031](../adr/0031-heavy-plugins-require-registered-automation.md) の 2026-08-21 改訂）。

- **月次3本は #504 で削除した**（「停止中」のまま残さなかった）。理由は3つ。①`workflow_dispatch` が生きている限り誰でも手動起動でき、その1回で Supabase 側だけが前進して正本と分岐する（ADR-0038 が禁じた向き）。②yml に書いた「代替経路」が #579・#584 の分離で実体とずれており（`macro_beta` は `run_monthly.ps1` ではなく `run_monthly_beta.ps1`、M-1 は `run_monthly_m1.ps1`）、`tests/test_workflow_schedule_pauses.py` は⛔・復旧条件・代替経路という**語の有無しか見ない**ので乖離が失敗として現れなかった。③停止中として残す条件は「復旧条件が書けること」だが、この3本の復旧条件は「正本を Supabase へ戻すとき」＝ADR-0038 がしないと決めた事象だった。
- `nightly_scores.HEAVY_AUTOMATION` は #504 で語彙に `local:<スクリプト>` を足し、全エントリがローカルバッチを指すようになった。**yml を指すエントリは schedule が生きていることまで CI が確かめる**（`tests/test_nightly_scores.py`）＝「登録はあるが cron は止まっている」という嘘を構造的に作れなくした。ただし `local:` には**タスクスケジューラ登録**という CI から見えない一段が残る（ADR-0031 の「登録があること ≠ 動いていること」は健在）。

## ワークフロー早見表: cron 停止前の3行（#503 まで）

2026-10-09（#844 の3回目）に DEPLOYMENT.md「GitHub Actions workflow 早見表」→「アクティブ」の表から移した。#503（2026-08-20）で `daily-incremental` の `schedule:` が止まり、`nightly-scores` / `macro-health` はその `workflow_run` チェーンなので連動して発火しない。DEPLOYMENT.md の3行は今の状態に書き直し、GHA で定時に動いていた頃の行（実測・設計の説明）をここへ移した。表の上にあった注記の1文も合わせて移した。

> **⚠ 下表の「使うタイミング」には cron 停止前の記述が残っている。**

| カテゴリ | workflow 名 | ファイル | 使うタイミング | 所要時間の目安 |
|---|---|---|---|---|
| `[定常]` | 差分収集・毎日自動実行 | `daily-incremental.yml` | **毎日 UTC 08:17（JST 17:17）** に自動（#476 で JST 03:00 から前倒し＝大引け 15:30 と EDINET 受付終了 17:15 の直後。根拠は下記「daily-incremental の動作詳細」）。手動で即時更新したい場合は `workflow_dispatch` | **2h05m〜2h38m**（2026-08-02 実測）。#474 以降、週末・祝日明けは gap-fill をほぼ飛ばすため大幅に短い |
| `[定常]` | 夜間スコア更新（`sector_ols` + M-6） | `nightly-scores.yml` | `nightly_scores.py`（Issue #432/#443・親 #423）を実行し、①`sector_ols` → `regression_results`（`predicted_market_cap` / `gap_ratio`）②`macro_enet`（M-6）→ `macro_enet_scores`（μ̂・`sell_ranking` の**既定** mu_source）を更新する。**起動は `daily-incremental` の `workflow_run` チェーンで `conclusion == 'success'` のときだけ**（株価が前進していない日にスコアだけ更新すると、古い株価由来の値が「今日のランキング」として出るため）。`sector_ols` は `regularization=ridge` 固定（既定 features 10項目は VIF>10 が頻発）、M-6 は params_schema の既定のまま（ADR-0021/0022 の実測と同一構成）。1モデルの失敗は他を巻き込まず、実行後に `max(computed_at)` / `max(created_at)` を直接クエリして永続化を確認する（例外なし＝コミット済みとしない）。モデル間の `load_data`（週次127万行）は `shared_snapshot_cache()` で共有し、Egress がモデル数に比例しないようにしている。手動即時実行は `workflow_dispatch` | **総所要 33.5分**（2026-08-04 本番実走・[run 30954182465](https://github.com/kome-kome/financial_app/actions/runs/30954182465)＝`sector_ols` 30.3分 + M-6 3.2分・job wall 34.5分）／**32.6分**（08-05・[run 31050406971](https://github.com/kome-kome/financial_app/actions/runs/31050406971)＝29.4分 + 3.2分）。`timeout-minutes` は実測 job wall の 2.0倍で **70分**（#446 で 150 から）。重いのは `sector_ols` 側で M-6 は 3.2分。起票時の 16.1分（2026-08-03・run 30808053564・30業種/2,837社）は #434 の構造的NULL対応前の値＝**銘柄数・業種数とともに伸びるので実走ログで追う** |
| `[定常]` | マクロ鮮度ゲート | `macro-health.yml` | `python -m scripts.check_macro_health`（Issue #420）が `macro_data` の系列別 `max(trade_date)` を期待更新頻度（`macro_health.FREQ_STALE_DAYS`）と突き合わせ、**既定モデルが使う系列**（`DEFAULT_MACRO_FEATURES` から逆引き）が古ければ exit 2 → `notify-failure` が Issue 起票。`collect_macro_data` は 1 系列失敗しても `continue` するため部分失敗が exit 0 で通り、#414 の失敗通知では拾えないのを塞ぐ。**収集本体（`daily-incremental` / `full-pipeline`）を落とさず独立ジョブに分離しているのが要点**——あちらを failure にすると `nightly-scores` の `workflow_run` チェーン（`success` 条件）が発火せず、マクロと無関係な `sector_ols` の夜間更新まで巻き添えで止まる（#425 の構造をワークフロー間へ適用）。収集側は同じレポートを run ログに出すだけ。誤検知が続く系列は `macro_health.EXCLUDED_SERIES` へ**理由付きで**登録する（現在: `JP_IP`＝FRED 凍結 #253／`JP_IIP`・`JP_IIP_INVENTORY`＝e-Stat が年単位更新 #451。`JP10Y` は #442 で `MACRO_SERIES` ごと削除したため除外指定も不要になった。`BCOM` は #438 の Yahoo 配信停止で一時除外していたが、収集元を連動 ETN `DJP` へ差し替えて 2026-08-06 に除外解除＝**直った系列は必ず除外から外す**（残すと代替ソース側の停止を検知できなくなる）） | 〜2分（GROUP BY 集約1本・`timeout-minutes: 10`） |

## CF 補完と bs_inventory バックフィルの完了状態（2026-05〜06）

2026-10-09（#844 の4回目）に DEPLOYMENT.md「データ収集の仕組み（自動 vs 手動）」の「CF補完の完了状態」「bs_inventory バックフィル」の2節から移した。どちらも完了済みの一回性の作業で、今の運用の手順は [GOTCHAS.md](../GOTCHAS.md)「CF NULL補完の運用」「bs_inventory バックフィルの運用」にある。`refill-cf.yml` は移した時点で `.github/workflows/` に無い。

### CF補完の完了状態（2026-05-31 完了）

| 指標 | 状態 |
|---|---|
| 通常補完（`cf_net_change_cash IS NULL`）| ✅ **全件完了**（remaining=0） |
| capex 充足率 | **88.8%**（CF文を持つ 19,073件中 16,929件取得済み） |
| 残り 2,144件 | アセットライト企業（持株会社・IT等）で capex 行が元々無いため永続的に NULL。再実行しても変わらない |
| `refill-cf.yml` スケジュール | **cron を撤去し手動（workflow_dispatch）のみ**に確定（Issue #117・案B）。下記の実態計測により定期実行は便益が無いと判断 |

#### cron を持たない理由（Issue #117 / 本番DB 20,548行の実態計測）

| CF 区分 | 充足率 |
|---|---|
| 営業CF `cf_operating_cf` | 100.0% |
| 投資CF `cf_investing_cf` | 99.9% |
| 財務CF `cf_financing_cf` | 99.7% |
| 現金増減 `cf_net_change_cash` | 98.3% |
| 設備投資 `cf_capex` | 88.9% |

- 主要3区分（営業/投資/財務CF）は初回 XBRL 収集で ≧99.7% 充足し、「CF NULL が蓄積し続ける」懸念は実態として発生していない。
- 唯一の有意な欠損は capex（~11%）だが全年度で安定した**構造的欠損**（提出企業ごとのタグ揺れ）であり、同じ XBRL を再パースする日次 cron では改善しない。本質的改善は parse 側のラベル照合拡充（別 Issue）で扱う。
- よって定期 cron の便益はほぼ無く、J-Quants レート制限・Render スリープのコストのみ残るため cron は撤去。欠損補完が必要な場合は `workflow_dispatch`（mode=refill/capex-only/diagnose・件数指定）で随時実行する。

### bs_inventory バックフィル（`.github/workflows/old/refill-pl-bs.yml`）

`bs_inventory` の NULL はタグ漏れではなく**時系列コホート**が原因（パーサ修正前に収集した〜2022年度が backfill 未実施。2026-06-15 実測で旧年度 57〜94% null・新年度は ~3%）。`.github/workflows/old/refill-pl-bs.yml` を（`.github/workflows/` へ戻してから）**workflow_dispatch（limit 省略＝全件・約4〜5時間）** で起動し、古い順に XBRL を再取得して是正する。詳細・残件の見方は GOTCHAS.md「bs_inventory バックフィルの運用」。

| 項目 | 状態 |
|---|---|
| 自動化整備 | ✅ `_pipeline_gh.py --refill-pl-bs` + `.github/workflows/old/refill-pl-bs.yml` を結線 |
| 本番バックフィル実行 | ✅ **完了**（2026-06-24 実測: 全年度 82〜87% カバレッジ。残 NULL はサービス業・金融等の構造的欠損） |
| 完了判定 | 全年度で一様な欠損率（≒13〜18%）になっており旧コホート偏りは解消済み |

## daily-incremental: GHA で定時に動いていた頃の運用と履歴（#503 まで）

2026-10-09（#844 の7回目）に DEPLOYMENT.md「daily-incremental の動作詳細」の注記から移した。収集は #503 以降ローカルの夜間バッチが回しており、GHA の `schedule:` はコメントアウトされている。今も効く規則（停止の有無はファイル名ではなく yml 本文の `schedule:` で判断する）は DEPLOYMENT.md に残した。「#412 で修正済み」は後に誤りと分かった読み（実際の原因は契約の失効・#461。GOTCHAS.md「J-Quants API の応答とエンバーゴ」）だが、当時の記録としてそのまま移した。

> **注（運用パターン）**: 全件収集（`full-pipeline.yml`）を回している間は、Supabase 接続上限での同時実行を避けるため、本ワークフローを一時的に `daily-incremental.yml.disabled` へリネームして停止する（例: コミット `4764d96`「全件収集中の同時実行回避」）。全件収集が終わったら `.yml` に戻して再有効化する。**現在ファイル名が `.disabled` の場合は自動の定時収集が止まっている状態**なので、UI / 手動収集で補う。<br>**ただし 2026-08 以降の恒久停止はこの方式ではない**——ファイル名は `.yml` のままで、`schedule:` をコメントアウトして止めている（#503）。「ファイル名が `.yml` だから動いている」とは読めないので、停止の有無は必ず yml 本文の `schedule:` を見て判断すること。<br>停止中の株価鮮度は `full-pipeline` の finalize（Phase 5）が同じ Yahoo ギャップ補完を持つため、全件収集の完了時点で追いつく（#426）。それでも collect フェーズ中（実測 約4時間）は前進しない点は変わらない。
>
> **cron の履歴（2026-06-22 再開 → 2026-08 停止・現在は停止中）**: dual-table 移行後の `workflow_dispatch` 手動実行で Yahoo ギャップ補完・J-Quants 株価取得が GitHub Actions（Azure IP）から正常動作することを確認 → `on.schedule` を有効化。**2026-08-09 に UTC 18:00（JST 03:00）→ UTC 08:17（JST 17:17）へ移動**（#476・上の起動時刻の根拠を参照）。その後 **#503 で正本がローカル PostgreSQL へ移り `schedule:` はコメントアウト**＝上の時刻は「停止時点の設定値」であって現在の挙動ではない（起動時刻の根拠としてだけ残す）。
>
> **⚠️ 2026-07-14 〜 08-01 は19日連続で failure していた**（真因: Phase 4 冒頭の J-Quants 403 が例外送出しプロセスが exit 1 → 鮮度の担い手である Yahoo ギャップ補完に一度も到達しなかった）。#412 で 403 をカバレッジ境界の欠測として継続扱いに修正済み。**この障害に19日間誰も気づかなかった**のは全ワークフローに失敗通知が無いためで、#414 で対応する。

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「daily-incremental の動作詳細」から移した、GHA 時代の所要とチェーン終端:

4. 所要時間: **2h05m〜2h38m**（2026-08-02 の success run 2本で実測。大半は EDINET 全日スキャンと Yahoo ギャップ補完の逐次リクエスト。`timeout-minutes: 360`）。**#474 以降、週末・祝日明けの run は gap-fill をほぼ丸ごと飛ばすため大きく短くなる**（平日は全社取得のままなので据え置き）

> チェーン終端の見込みは JST 21〜22時台（本体は下の手順4の所要 ＋ `nightly-scores` 実測33〜35分）。週次 VACUUM は 土 23:30Z なので衝突しない。

> **GHA の `daily-incremental.yml` は止まっている**（#503 で `schedule:` をコメントアウト。ファイル名は `.yml` のまま）。停止の有無はファイル名ではなく、yml 本文の `schedule:` を見て判断すること。GHA で定時に動いていた頃の運用パターン（全件収集中の `.disabled` へのリネーム）・cron の履歴・2026-07 の19日連続 failure は [archive/DEPLOYMENT_HISTORY.md](DEPLOYMENT_HISTORY.md) へ移した。

## ワークフロー: 完了して削除したバックフィル2本の所要

2026-11-22（/tidy）に DEPLOYMENT.md「GitHub Actions」→「ジョブ所要時間（設計参考値）」の箇条から移した。どちらも本番実行を終えて yml を削除済みで、現行の設計参考値にならない。

- `backfill-stock-history.yml`（完了につき削除済み）: 対象＝stock_price NULL かつ period_end 730日超前（初回 約3,800社）。`YAHOO_STOCK_RATE_SLEEP=0.5秒`・1社1リクエストで **約60〜90分**（`timeout-minutes: 150`）。
- `backfill-weekly-history.yml`（#198・完了につき削除済み）: 対象＝`stock_price_weekly` の最古日が `today-years` より新しい社。`backfill_weekly_history_yahoo` が Yahoo から過去方向に取得し、**1社ごとに `record_prices_batch(trim=True)`** で daily→weekly 再集約しつつ daily を都度 trim する（5年×全社の daily 同時展開を避け Supabase 500MB を超えない）。`YAHOO_STOCK_RATE_SLEEP=0.5秒`で **約60〜150分**（`timeout-minutes: 150`）。

## ワークフロー: #503 で停止したものと代替

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「GitHub Actions（#503 で停止したもの）」から移した。

| 旧 UTC | ワークフロー | 停止理由 | 代替 |
|---|---|---|---|
| 08:17 | `daily-incremental` → `nightly-scores` ／ マクロ鮮度ゲート（**#876 で削除**） | 正本がローカルへ移り、動かすと Supabase だけが前進して分岐する | ローカル `run_nightly.ps1`（JST 17:20）。鮮度ゲートはその `macro_health` ステップ |
| 00:00 | `macro-beta-inference`（**#504 で削除**） | 同上（`macro_beta_loadings` が分岐する） | `run_monthly_beta.ps1`（#579・毎月2日 JST 01:00） |
| 16:30 | `tune-hyperparameters`（**#504 で削除**） | 同上。M-1/M-2/M-3 の**唯一の自動更新経路**だったので、止めた時点で μ̂ の鮮度も止まる | 月次本体の `tune:macro_dlm`（M-3）、日中枠の探索の周回 `tune_cycle:macro_gbdt`（M-2・#1114・#1118）と `run_monthly_m1.ps1`（M-1・#584） |
| 22:00 | `recommend-factor-premia`（**#504 で削除**） | 同上。#423 子5 で「実行履歴ゼロのまま 37 期の重みで固着」を直した cron なので、**止めれば同じ固着へ戻る** | 月次本体の `factor_premia` ステップ（先頭に置いて打ち切りに強くしてある） |
| 土 23:30 | `vacuum-maintenance` | **2026-08-25 に停止**（#290 / #505）。断面は 2026-08-07 で凍結＝書き込みが無いので bloat も増えず、毎週 `VACUUM FULL` を打っても初回以降は何も回収しない | ローカル月次の `vacuum` ステップ（#504・先頭。実測 daily 59→49MB / weekly 188→165MB / 計13.4秒） |

- `nightly-scores` は `daily-incremental` の `workflow_run` チェーンなので、親を止めれば連動して止まる（yml 側の schedule は元から無い）。同じチェーンにいたマクロ鮮度ゲートは、#503 から起動元が無いまま #876 で見つかり、ワークフローを削除して夜間バッチの `macro_health` ステップへ移した。
- `full-pipeline` / `backfill-*` / `collect-interim` / `collect-disclosures` / `collect-macro` は `workflow_dispatch` 専用。放置で害はないが、**手動起動すると Supabase へ書く**＝正本と分岐するので注意。
- **月次3本は #504 でファイルごと削除した**（「停止中」のまま残さなかった）。削除を選んだ理由は [DEPLOYMENT_HISTORY.md](DEPLOYMENT_HISTORY.md#ワークフロー-月次3本を削除した理由504)。

## ワークフロー: 早見表の「アクティブ／アーカイブ済み」の表（#1291 まで）

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「GitHub Actions workflow 早見表」から移した。

一覧の正本は ARCHITECTURE.md §10 に一本化した。動かすときの注意は DEPLOYMENT.md「GitHub Actions の workflow（手動の口）」に残してある。

#### アクティブ（`.github/workflows/` 直下・Actions 対象）

> #503 で `daily-incremental` の `schedule:` はコメントアウトされ、連動して `nightly-scores`
> の `workflow_run` チェーンも発火しない（下表の2行は今の状態に書き直した。
> GHA で定時に動いていた頃の行は [DEPLOYMENT_HISTORY.md](DEPLOYMENT_HISTORY.md#ワークフロー早見表-cron-停止前の3行503-まで) へ移した・#844）。
> 同じチェーンにいたマクロ鮮度ゲートは **#876 でファイルごと削除し**、夜間バッチの `macro_health`
> ステップへ移したので下表にも無い。
> 月次3本（`macro-beta-inference` / `tune-hyperparameters` /
> `recommend-factor-premia`）は **#504 でファイルごと削除した**ので下表にも無い。
> **現在の駆動は `scripts/run_nightly.py`（日次）・`scripts/run_monthly.py`
> （月次）・`run_monthly_beta.ps1`・`run_monthly_m1.ps1`（月次の別タスク）・
> `run_daytime.ps1`（平日）**（時刻は上の「バッチ暦」）で、停止と代替の対応は上の「GitHub Actions（#503 で停止したもの）」が
> 正本。残っているファイルは `workflow_dispatch` の口が生きているが、**GHA からはローカル正本の DB へは
> 書けない**（書けるのは Supabase 断面だけ＝走らせると正本と分岐する）。定時（cron）で生きているのは
> `egress-health`（毎日）と `dependency-audit`（毎週・#723）の**2本**で、`ci` / `notify-failure` はイベント駆動
> （`vacuum-maintenance` は 2026-08-25 に停止・#290 / #505）。

| カテゴリ | workflow 名 | ファイル | 使うタイミング | 所要時間の目安 |
|---|---|---|---|---|
| `[CI]` | ruff（F 系）＋ pytest 自動テスト | `ci.yml` | PR・main push で自動実行（手動起動不要） | 〜1分 |
| `[停止]` | 差分収集（旧・毎日自動実行） | `daily-incremental.yml` | **⛔ 定時は #503（2026-08-20）で停止**。収集の入口はローカル夜間バッチの `_pipeline_incremental.py`（上の「バッチ暦」の `financial_app-nightly`）。`workflow_dispatch` の口は生きているが、**手動起動すると Supabase 断面へ書く＝正本と分岐する**。動作の中身（gap-fill・J-Quants catchup 等）は下記「daily-incremental の動作詳細」 | —（GHA 時代の実測は [DEPLOYMENT_HISTORY.md](DEPLOYMENT_HISTORY.md#ワークフロー早見表-cron-停止前の3行503-まで)） |
| `[全件]` | XBRL収集・財務データ全件更新 | `full-pipeline.yml` | DB初期構築時・全社バックフィル必要時（`daily-incremental` は #503 で schedule を止めてあるので、同時実行回避の退避は不要） | 200〜240分 |
| `[補完]` | マクロのみ収集 | `collect-macro.yml` | `MACRO_SERIES`（為替・金利・指数・コモディティ・ボラ）を Yahoo から収集。新規系列追加や macro_data の鮮度補完。`workflow_dispatch`（years 既定5）。**新系列のバックフィルは years=6 で起動**（yoy は1年+30日で足りるが、将来 zscore 版追加時に再バックフィル不要な余裕幅。#358 コモディティ8系列追加時の運用）。入力 `series` に series_code（カンマ区切り）を渡すと**その系列だけ**を収集する（#444・定義是正後の再収集で GDELT 累積クエリ制限を消費しないため） | 〜数分 |
| `[停止]` | 夜間スコア更新（`sector_ols` + M-6） | `nightly-scores.yml` | **⛔ `daily-incremental` の `workflow_run` チェーンなので、#503 で親ごと発火しなくなった**（手動起動の口だけ残る）。`nightly_scores.py` 自体はローカル夜間バッチの `scores` ステップで毎晩回る。回すモデル・1モデルの失敗が他を巻き込まないこと・書き込みの直接確認・`shared_snapshot_cache()` の共有は [ARCHITECTURE.md](../ARCHITECTURE.md) の `nightly_scores.py` の行が正本 | —（GHA 時代の実測は [DEPLOYMENT_HISTORY.md](DEPLOYMENT_HISTORY.md#ワークフロー早見表-cron-停止前の3行503-まで)） |
| `[補完]` | 半期(H1)財務収集 | `collect-interim.yml` | EDINET 半期報告書（043A00/docType160）と旧四半期報告書（043000/docType140）の Q2(中間=H1累計)を収集し `financial_records` に `period_type='H1'` で保存（Issue #219② フェーズB）。通期収集とは独立・常に差分（収集済み doc_id をスキップ）。`workflow_dispatch`（years_back 既定6＝既存通期窓に整合）。240分に収まらない場合は years_back を分割 | 数時間（過去6年・事前選別でQ1/Q3を除外し概ね1社1半期1DL） |
| `[定常]` | Supabase 枠消費ゲート | `egress-health.yml` | `python -m scripts.check_egress_health`（Issue #478 / #483・[ADR-0037](../adr/0037-egress-cycle-budget-is-a-second-axis.md)）が **Egress のサイクル累計**（`app_settings.egress_cycle_bytes`）と **Database Size**（`pg_database_size`）を閾値と突き合わせ、超過なら exit 2 → `notify-failure` が Issue 起票。**毎日 UTC 21:00（JST 06:00）自動**。閾値は Egress 80%（`db_egress.CYCLE_WARN_RATIO`）／DB 85%（`check_egress_health.DB_WARN_RATIO`）で、**DB 側を厳しくしてある**——Egress は超えても翌サイクルで戻るが、Database Size 超過は read-only で収集そのものが止まるため。**DB は Usage 画面と同じ割合で判定する**（#1109・2026-11-01）: `pg_database_size` に Usage との差（`DB_USAGE_GAP_BYTES`・11/1 の実測 14.4 MiB を切り上げて 15 MiB）を足した「Usage 換算」を、**10 進の上限 500 MB**（Usage 画面の 0.5 GB）で割る。以前は `pg_database_size` ÷ 2 進の 500 MiB に、差（8/19 の実測 約 35MB: Usage 430MB / Infrastructure 409.8MB / `pg_database_size` 395MB）を織り込んだ 0.85 を当てていたが、上限を約 5% 大きく見ていたので 11/1 は Usage 80% を 73.1% と出していた。**差は固定ではない**ので、割合が Usage 画面と数ポイント以上ずれたら測り直す。**`workflow_run` チェーンにせず cron で回すのが要点**：Egress はワークフローの成否と無関係に積み上がり、開発者のローカル CLI からも積まれる（過去2回の超過はどちらもローカル検証の反復が主因）ので「収集が成功した後に見る」では見落とす経路が残る。Management API の PAT は不要（判定材料は DB の中にある＝#483 のブロッカーを迂回）。手動即時実行は `workflow_dispatch`（`warn_only` で常に exit 0） | 〜2分（`timeout-minutes: 10`） |
| `[定常]` | 依存パッケージの脆弱性検査 | `dependency-audit.yml` | `requirements*.txt`（glob で全本・推移依存込み）を pip-audit で照合し、既知の脆弱性があれば failure → `notify-failure` が起票（Issue #723）。**毎週 UTC 22:23・日（JST 07:23・月）自動**＋ `requirements*.txt` を変える PR / main への push。手動は `workflow_dispatch`。仕組みと検出時の対応は下記「依存パッケージの脆弱性検査」節 | 〜数分（ローカル実測 48秒・107件） |
| `[定常]` | ワークフロー失敗の自動 Issue 起票 | `notify-failure.yml` | 上記ワークフロー（`ci.yml` を除く全本数・列挙しない設計）＋セルフテストが `failure` または `cancelled` で終わると自動起票（`workflow_run`）。手動起動しない。詳細は下記「ワークフロー失敗の通知」節 | 〜1分 |
| `[検証]` | notify-failure セルフテスト | `notify-failure-selftest.yml` | `notify-failure.yml` の変更後に発火を実証するための、意図的に失敗するだけのワークフロー（本番データ不使用）。`workflow_dispatch`（`mode=fail`／`mode=cancel`） | 〜1分（cancel は約1分） |
| `[手動]` | DBメンテナンス（VACUUM FULL・Supabase 断面用） | `vacuum-maintenance.yml` | **⛔ 定時実行は 2026-08-25 に停止（#290 / #505・[ADR-0038](../adr/0038-local-postgres-is-the-primary.md) の追補）。正本側の担い手はローカル月次バッチの `vacuum` ステップ**（#504）で、この yml は断面を手で保守するときの口としてだけ残る。**起動するときは step env へ `FINAPP_DB_TARGET: prod` を一時的に足すこと**（常設すると「誤起動が localhost で落ちる」安全弁を外すことになり `tests/test_db_target.py` が落ちる）。**per-table の autovacuum 設定を `init_db()` / `_ensure_tables()` へ入れない**（lifespan が無条件実行するので、ローカル API の起動だけで本番へ不可逆に反映される）。Supabase が正本だった時代の設計記録は [DEPLOYMENT_HISTORY.md](DEPLOYMENT_HISTORY.md) へ移した（#844） | 数秒〜数分（対象テーブルは実測 ~50MB・42万行） |

#### アーカイブ済み（`.github/workflows/old/` 配下・一回性・Actions 対象外）

一回性バックフィル完了後に `old/` へ退避済み。サブディレクトリの yml は Actions が認識しないので、再実行が必要な場合のみ `.github/workflows/` へ戻してから `workflow_dispatch` で起動する。定期スケジュールは持たない。

> 株価履歴バックフィル / 週次株価バックフィル / C2 NULL バックフィル / CF NULL バックフィル / bs_machinery バックフィルの5本は完了済みにつき削除済み（Issue #259）。

| カテゴリ | workflow 名 | ファイル | 用途 | 所要時間の目安 |
|---|---|---|---|---|
| `[補完]` | PL/BS NULL バックフィル | `.github/workflows/old/refill-pl-bs.yml` | `bs_inventory` 等 旧コホート（〜2022年）が NULL の場合に再取得 | 4〜5時間 |

> **CI（`ci.yml`）**: データ収集系ワークフローとは独立した回帰検知用。`pull_request` と main への `push` で Python 3.13.7 上に `requirements.txt` + `requirements-dev.txt` を入れて `ruff check`（F 系＝未定義名・import 漏れの静的検出・ルールは `ruff.toml`・#724）→ `pytest` の順に実行する。Secrets・外部ネットワーク・本番 DB には一切触れず、`conftest.py` の in-memory SQLite / モックで完結する範囲のみを検証する。

## egress-health: DB の割合を Usage 換算へ直す前（#1109 まで）

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の早見表の `egress-health` の行から移した。

以前は `pg_database_size` ÷ 2 進の 500 MiB に、差（8/19 の実測 約 35MB: Usage 430MB / Infrastructure 409.8MB / `pg_database_size` 395MB）を織り込んだ 0.85 を当てていたが、上限を約 5% 大きく見ていたので 11/1 は Usage 80% を 73.1% と出していた。

## バックアップ: Storage 経路の初回の復元（2026-09-04）

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「バックアップからの復元」から移した。

- **2026-09-04 に Storage 経路を初めて通した**（#503 検証8）: `20260821T013818Z` を落として
  使い捨てクラスタへ復元し、**17表すべて行数一致**。さらに `DATABASE_URL_LOCAL` を復元先へ向けて
  API を起動し、`/health` と全9画面が 200・`/api/stats` が `companies 4,438 / records 50,490 /
  stock_price 1,293,208` を返すことまで確認した（**戻せたことの本体は「サービスが再開できる」こと**）。

## 通知: 手動キャンセルが high で起票された件（#453・2026-08-04）

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「ワークフロー失敗の通知」から移した。

> この振り分けが無かった 2026-08-04 に、人が押したキャンセル1件が `priority:high` の Issue #453 として自動起票された。**「復旧すべき障害」ではないノイズが high として積まれると、本物の失敗の優先度が相対的に薄まる**（Issue 本文自身が「open のまま放置すると次の失敗がコメントに埋もれる」と警告している通り）。

## 依存の脆弱性検査: 検出の記録

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「依存パッケージの脆弱性検査」から移した。

**初回の検査結果（2026-09-24・ローカル実測）**: 107件を照合・スキップ 0。検出は **anyio 4.13.0 の2件**（CVE-2026-63374 / GHSA-82r6-8w77-94w6、CVE-2026-64847 / GHSA-5p39-cfhj-2xmp・修正版 4.14.2）。anyio は starlette / httpx の推移依存。前者は非 ASCII ドメインへの TLS 接続、後者は `anyio.to_process` のプロセスプールが条件で、本アプリはどちらも踏まない見込みだったが、修正版があるので #730（検査の初回実走が起票した Issue）で 4.14.2 へ上げて解消した。最新の 4.15.x は Python 3.13 で `typing_extensions>=4.16.0` を要求し pin（4.15.0）と衝突するので、単独 PR の範囲に収まる 4.14.2 を選んだ。

**2回目の検出（2026-10-26・週次の定期実行）**: **certifi 2026.4.22 の1件**（PYSEC-2026-4320 / GHSA-6q48-6c7w-9gw2・修正版 2026.10.19）。CA バンドルに残っていた emSign ECC Root CA - G3（配下の中間証明書が鍵漏えいで失効）を信頼してしまう問題で、#1067（自動起票）で 2026.10.19 へ上げて解消した。certifi は依存を持たないので他の pin は動かない。**ローカルの `venv` は自動では上がらない**ので、バッチの走っていない時間に `.\venv\Scripts\python.exe -m pip install certifi==2026.10.19` で揃える。

## 探索の運用: 品質ゲートと成果物の固着の経緯

2026-11-22（#1291）に DEPLOYMENT.md「データ収集の仕組み」の「ハイパーパラメータ探索の運用詳細」から移した。

- **品質ゲート（#291 → #590 で作り直し・[ADR-0047](../adr/0047-tuning-gate-compares-on-one-panel.md)）**:
  旧実装は永続化済みの `objective_value` と比較し、劣化していれば persist をスキップして
  非ゼロ終了した。だが保存値は「そのとき存在したパネルでの値」で、パネルは毎晩伸びるため
  月をまたいだ比較が成立しない（実測 0.5068=10 fold / 0.0221=55 fold＝**fold が少ない候補ほど
  高く出る**）。実際 `macro_gbdt` はこれで 2026-07-19 の値のまま閉じ続けた。現在は champion を
  **同一パネル上で**測り直して候補プールへ入れるので `best >= champion` が構造的に成立し、
  persist は常に行い exit は 0。水準の移動は WARNING と `plugin_tuned_params` の
  `prev_objective_value` / `champion_objective_value` / `n_periods` / `n_oof_samples` に残す。

     **ただしその Issue を閉じても穴は残る**——2026-09-01 の打ち切り後、`plugin_tuned_params` は
     M-2 が 50日・M-3 が 59日 古いまま誰も気づかなかった。成果物の固着は
     `batch_freshness.PRODUCERS` を watchdog が見て別に起票する（#504）。
     **`PRODUCERS` は月次の成果物だけではない**——夜間バッチが更新する `JPX 業種マスタ`
     （`app_settings.jpx_industry_last_success`）も同じ表で見る。取得が止まっても既存の業種は
     DB に残り、`nightly_last_run` も画面も健全なままなので、ここ以外に現れる場所が無い
     （#632・2026-09-03 の拡張子変更は6晩連続の 404 を `exit=0` で通した）。JPX に載らない上場社の
     空欄を埋める `EDINET コードリスト（業種の空欄補完）`（`app_settings.edinet_codelist_last_success`・
     #784）も同じ理由で同じ表に並ぶ。
