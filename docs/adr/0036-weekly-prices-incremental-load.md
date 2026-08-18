# 週次株価は指紋＋27週オーバーラップの差分ロードにし、無効化は DB 側の世代印で行う

## Status

accepted（2026-08-17）。Issue #480 の設計決定。
[ADR-0034](0034-client-side-egress-ledger-and-circuit-breaker.md) が「限界」として別 Issue に残した宿題への回答。
[ADR-0035](0035-mirror-endpoints-are-parameterized.md) §5（27週オーバーラップの導出）と定数を共有する。

## Context

夜間バッチ（`nightly-scores.yml`）は毎晩 `stock_price_weekly` を **1,282,436 行 / 39.3MB** 引き直して
いた。1日の増分は約 4,400 行で、**転送の 99.7% が不変データの再送**である。1回 67.7MB のうち週次が
39.3MB を占め、月30回で 1.98GB＝無料枠 5GB の 40% になる。

Egress は 2026-07 と 2026-08 の2回、超過で組織全体を restricted に落としている（2回目は8日間停止）。
ADR-0034 は計測とブレーカを入れたが、そこに明記したとおり **測ることと減らすことは別の仕事**で、
この 1.98GB は手つかずのまま残っていた。

削減の障害は2つある。

**1. 下流が全履歴を要求する。** `_build_snapshots_impl` は全期間の月末で学習サンプルを作り
（`min_rows = HORIZON_WEEKS + 4`）、52週先のラベルを引く。「直近N週だけ引く」は成立しない。
既存の `shared_snapshot_cache()` は `contextvars` によるプロセス内キャッシュで、GHA の run は
毎回新プロセス・新ランナーだから run を跨いだ再利用が無い。**プロセス外の永続キャッシュが要る。**

**2. 週次バーは追記だけでなく上書きで変わる。** `_recompute_weeks_from_daily` は
`record_prices_batch` が触れた週を daily の保持窓ぶん（最大 `DAILY_WINDOW_DAYS`=183 日）遡って
再集約 upsert する。さらに `repair_price_scale_breaks`（#465）は該当社の**全期間**（5〜10年）を
Yahoo で取り直して書き換える。後者では **行数も `max(week_start)` も変わらず、値だけが変わる**。

## Decision

### 1. 高水位は `week_start`。指紋は `max(week_start)` + `count(*)` のサーバ側集約

`trade_date` は週内最終営業日で PK に含まれず nullable ＝範囲スキャンの索引条件に入らない。
PK は `(edinet_code, week_start)` なので、差分条件は**既存の 500 社チャンクの中に**足す
（`edinet_code IN (...) AND week_start >= :since`）。`week_start >= :since` 単独は PK の先頭列に
ならず seq scan になり、チャンク分割の意味が消える。

指紋は 1 行しか返さない集約2本で、Egress はほぼゼロ。
`hyperparameter_search._data_fingerprint` に同じ形の前例があるが、あちらは `max(trade_date)` を
使っている（鮮度警告用なので実害は無い）。**この形をコピーしないこと**を明記しておく。

### 2. オーバーラップ 27週は `DAILY_WINDOW_DAYS` から導出し、定義は `database.py` に置く

`WEEKLY_OVERLAP_WEEKS = ceil(183/7) = 27` / `WEEKLY_OVERLAP_DAYS = 189`。
根拠（`_recompute_weeks_from_daily` の遡及上書き）が `database.py` にあるので**定義もそこへ置き**、
`scripts/mirror_common.py` は import に変えた。ADR-0035 §5 で導入したときは `scripts/` 側にあり、
根拠と定義の場所がねじれていた。導出は1箇所・消費が2箇所（ミラー同期／週次キャッシュ）で、
テストは値の一致ではなく**同一オブジェクトであること**を見る——別々のリテラルが偶然一致しても
通ってしまい、片方だけスラックを足す変更で黙って乖離するため。

`since` が必ず月曜であることから、ISO 週の不変条件 `week_start <= trade_date <= week_start+6` より

    week_start >= since  ⟺  trade_date >= since

が成り立つ。**DB 側は `week_start` で切り、キャッシュ側は行が既に持っている `trade_date` で切る**
のが厳密に一致する。これにより SELECT の列を増やさずに済む（決定6）。

### 3. 指紋で見えない訂正は「書き手が印を進める」側で解く

`app_settings.weekly_prices_generation` を世代印とし、値が変わったら次のロードを強制フルロードにする。

**印を DB に置くのは、修復 CLI が開発者のマシンで走り、キャッシュは GHA ランナーに載るため。**
ディスク上の印では相手に届かない。印は書き手と読み手の両方から見える場所にしか置けない。

印を進めるトリガは**構造的な条件を主、明示フックを従**にする。

- 主: `_recompute_weeks_from_daily` が「保持窓より古い週を実際に書き換えた」ときに進める。
  定常経路（Yahoo gap-fill / J-Quants catchup）は取得開始日を `today - DAILY_WINDOW_DAYS` で
  クリップするのでここへは来ない。`repair_price_scale_breaks` と `backfill_weekly_history_yahoo`
  だけが必ず該当する。**将来あらたな深い書き込み経路が足されても自動で拾える。**
- 従: `repair_price_scale_breaks` と `_pipeline_gh --backfill-weekly` からも明示的に呼ぶ
  （1社ずつの rollback で主トリガが落ちる場合の保険）。

明示フックだけにしないのは、列挙は必ず漏れるから（ADR-0031「登録≠実行」と同型の失敗）。

### 4. 行数照合はハードゲート。「不一致だが続行」を作らない

差分ロードでは DB は毎晩正当に変わるので `count(*)` を等値ゲートにはできない。**マージ結果の
自己検証**に使う: `Σ len(rows) == count(*) - offset`。`offset` は「ローダーが引かない孤立行」等の
構造的な差で、コールドロード時に学習する（学習しないと孤立行が1行あるだけで毎晩フルロードへ
退化し、Issue の目的が消える）。

不一致は警告ではなく**必ずフルロード**。backfill・新規上場社の過去バックフィル・DELETE・
キャッシュの取りこぼしはすべてここで倒れる。

### 5. 静かな劣化に4層の歯止めを置く

stale なパネルで μ̂ を生成しても failure は出ない（ADR-0031/0034 が繰り返し警戒している形）。

1. 行数照合はハードゲート（決定4）
2. **鮮度アサートは raise**。マージ結果の `max(trade_date)` が DB の `max(week_start)` に届かなければ
   例外。GHA では failure ＝ `notify-failure.yml` が Issue を自動起票する
3. **週1回の強制コールドロード**（`FINAPP_WEEKLY_CACHE_MAX_AGE_DAYS`、既定7）。未検知の乖離が
   生き延びる期間を設計で7日に上限する。コスト 39.3MB×4回/月 = 157MB に対し削減は約 1.06GB/月
4. **コールド時のドリフト監査**（追加 Egress ゼロ）。旧キャッシュと新フルロードが同時にメモリに
   ある瞬間に、差分経路が原理的に触らない `since` 以前の区間だけを突合する。不一致は例外。
   これは「差分とフルがビット一致すること」を本番データで週1回タダで検証しているに等しく、
   世代印フックが漏れた場合の最終防衛線になる。
   **監査は `periodic-refresh` のときだけ**行う——他の理由でのコールドは「過去が変わった」と既に
   分かっている状態であり、掛ければ必ず誤検出になる

### 6. SELECT の列は増やさない。ワイヤ形式は素のタプル

`week_start` を戻り値に載せない理由:

- `db_egress.EGRESS_COST_TABLE` の `("stock_price_weekly", 4)` は **volume 込みの較正値**（42.0 B/行）で、
  `week_start` を足した4列が誤ってこれに当たる。新エントリには実測が要るが 8/18 まで測れない
- DB をモックする3テストが週次行を4要素固定タプルで返している
- 1,282,436 行 × 1 フィールドぶんのメモリ

キャッシュのワイヤ形式は namedtuple ではなく**素のタプル**にする。`_VOLUME_NOT_LOADED` は
`object()` の番兵で、**pickle round-trip で同一性が壊れる**（`is` 判定が False になる）。すると
`px_volz` は ValueError を投げずに全 nan を返す——#438/#446 が番兵を導入して潰した「静かな故障」が
キャッシュ経由で復活する。番兵の再付与は呼び出し側（`load_weekly_prices_chunked`）が持ち、
キャッシュ層は行の型を一切知らない。

### 7. `actions/cache` は `nightly-scores.yml` だけに入れる

週次を引く4経路のうち、毎晩走るのは nightly-scores だけ（月 1.98GB のうち 1.98GB）。
月次3本（`tune-hyperparameters` / `recommend-factor-premia` / `macro-beta-inference`）は月1回で
合計約 400MB であり、`tune-hyperparameters` は matrix 3並列＝同一キーへの同時 save が競合する。
**意図的なスコープ限定**であって入れ忘れではない（CI がそれを固定する）。

キーは `weekly-prices-v1-${{ github.run_id }}` で毎回ユニークにし、復元は `restore-keys` の前方一致で
直近世代を拾う。固定キーにすると初日以降 save が起きず、基準が古いまま凍って 27週窓では届かなく
なり、毎晩フルロードへ退化する。

### 8. `scripts/_cache.py` は再利用しない

`scripts/_cache.py`（#355）は docstring で「検証専用」と宣言し、`tests/test_column_scoping.py` は
`scripts/` を走査対象から外す根拠として「別の制御下にある」と説明している。加えて意味論が逆で、
あちらは **TTL 無し・明示 `--refresh-cache` のみ**（再現性優先）、こちらに要るのは「データ世代が
変われば自動で捨てる」である。`plugins/` → `scripts/` の import も現状ゼロで、依存の向きを
作りたくない。ディレクトリ（`.weekly_cache/` と `scripts/.cache/`）とログ接頭辞（`[wpcache]` と
`[cache]`）を分け、二重管理であることを可視にしておく。

## Considered Options

**毎晩チェックサムを取る（不採用）。** `scripts/mirror_common.py` の順序非依存 md5 集約なら値だけの
訂正も自動検出できる。ADR-0035 がバケット指紋案を不採用にしたのと同じ理由で見送った——1.28M 行の
サーバ側フルスキャンが `statement_timeout=2min`（ADR-0032）に当たるリスクが**未実測**である。
8/18 に `mirror_verify --level checksum` の所要が測れた時点で再検討する（#494 と同じ扱い）。

**`record_prices_batch` で無条件に世代印を進める（不採用）。** 毎晩発火してキャッシュが一度も
再利用されなくなり、Issue の目的が消える。印は「深い書き換え」の粒度に留めるしかない。

**学習期間そのものを短くする（不採用）。** 転送は減るがモデルの挙動が変わる。ADR-0025 が
履歴の延伸で学習窓を広げた判断と正面から衝突する。

## Consequences

- 夜間バッチの週次は **39.3MB → 約3.7MB**（27/約289週 ≒ 9.3%）。1回 67.7MB → 約32MB、
  月 1.98GB → 約1.06GB＋週1コールド 157MB ＝ **実効 約1.11GB（枠の22%）**。
  比率は1社あたり週数の分布で変わるため、コールド時に `delta_preview` を出して実測へ置き換える
- **初回は必ずフルロードになる**。GHA キャッシュが載る翌晩から効く。8/18 の復帰判断は
  従来値（67.7MB × 残日数）で行い、この Issue の成否に依存させない
- キャッシュは「あれば速い」だけの位置づけで、**正しさは指紋・世代印・行数照合の3つが持つ**。
  ファイルが無い・壊れている・古い・保存に失敗した、はすべてフルロードへ倒れる。
  `FINAPP_WEEKLY_CACHE=0` で従来動作へ戻せる（そのときは指紋クエリすら発行しない）
- **残存リスク**: 主トリガを通らない書き込み（手書き SQL の UPDATE・`pg_restore`・
  ダッシュボードからの編集・将来 `StockPriceWeekly` へ直接 upsert する新コード）は指紋にも印にも
  現れない。露出は週1コールド（決定5-3）で最大7日に上限し、ドリフト監査（決定5-4）が例外へ変換する。
  **これは設計上受け入れた弱点**である
- `_recompute_weeks_from_daily` の戻り値が `None` から `Optional[str]`（深く書き換えた最古週）に
  変わった。世代印の bump は `record_prices_batch` の `commit()` **後**に置く——`upsert_setting` は
  自前で commit するため、手順②の途中で呼ぶと trim 前の状態が確定して原子性が変わる
