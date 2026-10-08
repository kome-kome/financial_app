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
