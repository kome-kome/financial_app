"""週次株価の run 間差分ロードキャッシュ（Issue #480・ADR-0036）。

夜間バッチは毎晩 `stock_price_weekly` を 1,282,436 行引き直していたが、1日の増分は約 4,400 行
＝**転送の 99.7% が不変データの再送**だった（月 1.98GB＝無料枠 5GB の 40%）。`shared_snapshot_cache()`
（plugins/macro_snapshots.py）はプロセス内でしか効かず、GHA の run は毎回新プロセスなので
run を跨いだ再利用が無い。ここは**プロセス外の永続キャッシュ**を持ち、DB からは差分だけ引く。

    フルロード           : 1,282,436 行 / 39.3MB
    差分ロード（定常）   : 直近27週ぶん ≒ 12万行 / 約3.7MB

**キャッシュは「あれば速い」だけの位置づけで、正しさは一切これに依存しない。**
正しさを担保するのは次の3つで、いずれも外れたら黙って古い値を返すのではなく**フルロードへ倒す**。

  1. 指紋（`max(week_start)` + `count(*)`・サーバ側集約なので Egress は2行ぶん）
  2. 27週オーバーラップ（`database.WEEKLY_OVERLAP_DAYS`＝`DAILY_WINDOW_DAYS` からの導出）
  3. DB 側の世代印（`app_settings.weekly_prices_generation`）

### なぜ指紋だけでは足りないか（この設計の中心）

`repair_price_scale_breaks`（#465）は該当社の**全期間**を Yahoo で取り直して上書きする。
このとき **行数も `max(week_start)` も変わらず、値だけが変わる**＝指紋では原理的に検出できない。
かといって毎晩フルスキャンのチェックサムを払うと statement_timeout(2min) のリスクがある
（ADR-0035 がバケット指紋案を不採用にしたのと同じ理由）。そこで**書き手が印を進める**側で解く。
印を DB に置くのは、修復 CLI が開発者のローカルで走り、キャッシュは GHA ランナーに載るため
＝ディスク上の印では相手に届かない。「印は書き手と読み手の両方から見える場所に置く」。

### 静かな劣化への歯止め（ADR-0031/0034 が繰り返し警戒している形）

stale なパネルで μ̂ を生成しても failure は出ない。4層で塞ぐ（上ほど構造的）:

  1. 行数照合はハードゲート。不一致は警告ではなく必ずフルロード（「続行」の分岐を作らない）
  2. 鮮度アサートは raise。GHA では failure ＝ notify-failure.yml が Issue を自動起票する
  3. 週1回の強制コールドロード（未検知の乖離が生き延びる期間を7日で打ち切る）
  4. コールド時のドリフト監査（差分経路が触らない過去区間を旧キャッシュと突合・追加 Egress ゼロ）

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import atexit
import os
import pickle
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

# キャッシュ形式のバージョン。**ファイル名に入れる**（ヘッダを読んでから判定する必要がない
# ＝形式を変えたら旧ファイルは触りもせず単なる MISS になる）。
CACHE_VERSION = 1

# GHA の actions/cache が保存するパス。`.github/workflows/nightly-scores.yml` と一致させること
# ＝`tests/test_nightly_scores.py::TestWeeklyCacheStep` が照合する（ADR-0031「登録≠実行」対策）。
CACHE_DIR_NAME = ".weekly_cache"

# 世代印の置き場所（app_settings）。書き手（repair / backfill / 深い再集約）が進める。
GENERATION_KEY = "weekly_prices_generation"

# 未検知の乖離が生き延びる上限。コスト 39.3MB × 4回/月 = 157MB に対し削減は約 1.06GB/月。
DEFAULT_MAX_AGE_DAYS = 7

# ドリフト監査で表示する例の件数（ログを溢れさせない）
_DRIFT_EXAMPLES = 5

_stats = {"hits": 0, "misses": 0, "fetched_rows": 0}


class WeeklyCacheStale(RuntimeError):
    """差分ロード結果が DB の高水位に届いていない。**握らず落とす**（#480 の歯止め2）。"""


class WeeklyCacheDrift(RuntimeError):
    """差分経路が触らない過去区間がキャッシュと実データで食い違った（#480 の歯止め4）。"""


@dataclass(frozen=True)
class WeeklyFingerprint:
    """週次株価テーブルの世代。サーバ側集約だけで作る（Egress は2行ぶん）。"""
    max_week_start: Optional[str]
    n_rows: int
    generation: str


@dataclass(frozen=True)
class _Header:
    version: int
    with_volume: bool
    generation: str
    db_row_count: int       # 書いた時点の count(*)
    cached_row_count: int   # 実際に格納した行数（db との差＝孤立行などの構造的オフセット）
    max_week_start: Optional[str]
    full_loaded_at: str     # 直近フルロードの UTC 日付（週1コールドの起点）


# ── 設定 ──────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """既定 ON。緊急停止は `FINAPP_WEEKLY_CACHE=0`（コード変更なしで従来動作へ戻せる）。"""
    return os.environ.get("FINAPP_WEEKLY_CACHE", "1").strip().lower() not in ("0", "false", "no")


def cache_dir() -> Path:
    return Path(os.environ.get("FINAPP_WEEKLY_CACHE_DIR") or CACHE_DIR_NAME)


def _max_age_days() -> int:
    try:
        return max(1, int(os.environ.get("FINAPP_WEEKLY_CACHE_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS


def cache_path(with_volume: bool) -> Path:
    # with_volume は **別ファイル**にする。混ぜると「False でロードした結果を True の要求へ
    # 流用しない」（macro_snapshots.load_data のキャッシュキー設計・#446）をプロセス外で破る。
    return cache_dir() / f"weekly_v{CACHE_VERSION}_wv{int(with_volume)}.pkl"


def _log(msg: str) -> None:
    # stderr が閉じている環境でも本処理を落とさない（db_egress._log と同じ作法）。
    try:
        print(f"[wpcache] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ── 指紋と世代印 ──────────────────────────────────────────────────────────────

def fingerprint(db) -> WeeklyFingerprint:
    """`max(week_start)` + `count(*)` + 世代印。

    高水位は **`week_start`**（PK 第2列）であって `trade_date` ではない。`trade_date` は週内の
    最終営業日で PK に含まれず nullable ＝範囲スキャンのインデックス条件に入らない。
    （`hyperparameter_search._data_fingerprint` は `max(trade_date)` を使っており、あちらは
    鮮度警告用なので実害は無いが、**この形をコピーしないこと**。）
    """
    from sqlalchemy import func
    from database import StockPriceWeekly, get_setting

    max_ws, n = (db.query(func.max(StockPriceWeekly.week_start), func.count())
                   .select_from(StockPriceWeekly)
                   .one())
    return WeeklyFingerprint(
        max_week_start=max_ws,
        n_rows=int(n or 0),
        generation=get_setting(db, GENERATION_KEY) or "0",
    )


def bump_generation(db, reason: str) -> str:
    """週次キャッシュの世代印を進める＝次回のロードを強制フルロードにする。

    **ローカルファイルに置いてはいけない。** `--repair-price-breaks` は開発者のマシンで走り、
    キャッシュは GHA ランナーに載る。ディスク上の印では相手に届かない。
    """
    from database import upsert_setting
    token = datetime.now(timezone.utc).isoformat(timespec="seconds")
    upsert_setting(db, GENERATION_KEY, token)
    _log(f"GENERATION bumped to {token} ({reason})")
    return token


def bump_generation_safely(db, reason: str) -> Optional[str]:
    """`bump_generation` の失敗でデータ収集そのものを落とさないラッパー。

    印を落とすと次回が stale になるが、それは週1回の強制コールド（歯止め3）とドリフト監査
    （歯止め4）が拾う。逆に「株価の修復は成功したのに印の書き込みで例外」で収集全体を
    落とすほうが害が大きい。**失敗は必ず可視化する**（黙って握らない）。
    """
    try:
        return bump_generation(db, reason)
    except Exception as e:   # noqa: BLE001 — 収集を止めないことが目的
        _log(f"WARN  generation bump failed ({reason}): {type(e).__name__}: {e}")
        return None


# ── ファイル入出力 ────────────────────────────────────────────────────────────

def _read(with_volume: bool):
    """(header, prices) を返す。読めない・壊れている場合は None（例外にしない）。"""
    p = cache_path(with_volume)
    try:
        with p.open("rb") as f:
            blob = pickle.load(f)
        header = _Header(**blob["header"])
        if header.version != CACHE_VERSION or header.with_volume != with_volume:
            return None
        return header, blob["prices"]
    except FileNotFoundError:
        return None
    except Exception as e:   # noqa: BLE001 — 破損は MISS へ縮退させる（落とさない）
        _log(f"WARN  cache unreadable ({p}): {type(e).__name__}: {e}")
        return None


def _write(prices_wire: dict, header: _Header) -> None:
    """tmp -> Path.replace のアトミック置換（scripts/_cache.py の作法を踏襲）。"""
    p = cache_path(header.with_volume)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("wb") as f:
            pickle.dump({"header": header.__dict__, "prices": prices_wire},
                        f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(p)
        mb = p.stat().st_size / (1024 * 1024)
        _log(f"SAVE  {p.name} rows={header.cached_row_count} "
             f"companies={len(prices_wire)} ({mb:.1f}MB)")
    except Exception as e:   # noqa: BLE001 — 保存失敗は次回 MISS になるだけ
        _log(f"WARN  cache save failed ({p}): {type(e).__name__}: {e}")


# ── 境界とマージ ──────────────────────────────────────────────────────────────

def refresh_boundary(max_week_start: str) -> str:
    """無条件に取り直す下限（必ず月曜）。

    `min(max_week_start, 今週の月曜)` から 27週遡る。max_week_start を素で使わないのは、
    未来日のデータ（収集側の事故）が入ったときに窓が未来へずれて過去の訂正を取り落とすため。
    """
    from database import WEEKLY_OVERLAP_DAYS, iso_week_start
    anchor = min(max_week_start, iso_week_start(_today()))
    return (date.fromisoformat(anchor) - timedelta(days=WEEKLY_OVERLAP_DAYS)).isoformat()


def _merge(old: dict, fresh: dict, since: str, trade_date_of: Callable) -> dict:
    """old（全履歴）から since 以降を捨て、fresh（since 以降の全社ぶん）で置き換える。

    `since` は必ず月曜（週境界）で、ISO 週の不変条件 `week_start <= trade_date <= week_start+6`
    から **`week_start >= since` ⟺ `trade_date >= since`** が成り立つ。よって DB 側は
    `week_start >= :since`（PK プレフィックスが効く）、キャッシュ側は行が既に持っている
    `trade_date` で切る、で厳密に一致する＝**行に week_start を載せる必要がない**。

    戻り値は「各社 trade_date 昇順」の契約を保つ。old 側は昇順リストの接頭辞、fresh 側は
    `ORDER BY` 済みでどれも `>= since` なので、連結でソートは不要（1.28M 行の再ソートを払わない）。
    """
    out: dict = {}
    for ec, rows in old.items():
        kept = [r for r in rows if trade_date_of(r) < since]
        if kept:
            out[ec] = kept
    for ec, rows in fresh.items():
        if rows:
            out.setdefault(ec, []).extend(rows)
    return out


def _count_rows(prices: dict) -> int:
    return sum(len(v) for v in prices.values())


# ── 本体 ──────────────────────────────────────────────────────────────────────

def load_incremental(db, *, with_volume: bool, fetch, to_wire, from_wire, trade_date_of) -> dict:
    """週次株価を差分ロードして {edinet_code: [row, ...]}（各社 trade_date 昇順）を返す。

    引数はすべて呼び出し側（plugins/macro_snapshots.py）が渡す。**このモジュールは行の型を
    知らない**——`_VOLUME_NOT_LOADED` 番兵（object()）は pickle round-trip で同一性が壊れ、
    `is` 判定が False になって「未ロードなのに欠測扱い」という #438 型の静かな故障を招く。
    ワイヤ形式を素のタプルに固定し、番兵の再付与を呼び出し側の `from_wire` に任せる。

      fetch(since)   : since 以降（None なら全件）を {ec: [row,...]} で返す。ORDER BY 済み
      to_wire(row)   : 行 -> pickle 可能な素タプル
      from_wire(t)   : 素タプル -> 行
      trade_date_of(row) : 行 -> 'YYYY-MM-DD'
    """
    if not enabled():
        # 指紋クエリすら発行しない＝**無効化したら従来と1文も変わらない**。
        # 「無効にしたはずなのに挙動が違う」を作らないことが緊急停止スイッチの条件。
        _log("MISS  reason=disabled -> full load")
        return fetch(None)

    fp = fingerprint(db)
    cached = _read(with_volume)
    reason = _cold_reason(cached, fp)
    if reason is not None:
        old_prices = cached[1] if cached else None
        return _full_load(db, fp, fetch, to_wire, with_volume, reason=reason,
                          old_prices=old_prices, from_wire=from_wire,
                          trade_date_of=trade_date_of)

    header, wire = cached
    old = {ec: [from_wire(t) for t in rows] for ec, rows in wire.items()}

    since = refresh_boundary(fp.max_week_start)
    t0 = time.monotonic()
    fresh = fetch(since)
    n_fresh = _count_rows(fresh)
    _stats["fetched_rows"] += n_fresh

    merged = _merge(old, fresh, since, trade_date_of)

    # 行数照合（ハードゲート）。差分ロードでは DB は毎晩正当に変わるので count(*) は等値ゲートに
    # できない。**マージ結果の自己検証**に使う。backfill・新規上場社の過去バックフィル・DELETE・
    # キャッシュの取りこぼし/二重計上はすべてここで不一致になり、フルロードへ倒れる。
    offset = header.db_row_count - header.cached_row_count
    got = _count_rows(merged)
    expected = fp.n_rows - offset
    if got != expected:
        _log(f"MISS  reason=row-count-mismatch expected={expected} got={got} "
             f"delta={got - expected} (offset={offset})")
        return _full_load(db, fp, fetch, to_wire, with_volume,
                          reason="row-count-mismatch", old_prices=wire,
                          from_wire=from_wire, trade_date_of=trade_date_of)

    _assert_fresh(merged, fp, trade_date_of)

    _stats["hits"] += 1
    _log(f"HIT   {cache_path(with_volume).name} cached={header.cached_row_count} "
         f"fresh={n_fresh} since={since} total={got} ({time.monotonic() - t0:.1f}s)")

    _write({ec: [to_wire(r) for r in rows] for ec, rows in merged.items()},
           _Header(version=CACHE_VERSION, with_volume=with_volume, generation=fp.generation,
                   db_row_count=fp.n_rows, cached_row_count=got,
                   max_week_start=fp.max_week_start,
                   full_loaded_at=header.full_loaded_at))
    return merged


def _cold_reason(cached, fp: WeeklyFingerprint) -> Optional[str]:
    """フルロードすべき理由。None ならウォーム（差分で進める）。"""
    if cached is None:
        return "no-cache-file"
    header, _ = cached
    if fp.max_week_start is None:
        return "empty-db"
    if header.generation != fp.generation:
        _log(f"generation changed: {header.generation} -> {fp.generation}")
        return "generation-changed"
    if header.max_week_start is None:
        return "no-watermark-in-cache"
    try:
        age = (date.fromisoformat(_today()) - date.fromisoformat(header.full_loaded_at)).days
    except (TypeError, ValueError):
        return "bad-full-loaded-at"
    if age >= _max_age_days():
        return "periodic-refresh"
    return None


def _full_load(db, fp: WeeklyFingerprint, fetch, to_wire, with_volume: bool, *,
               reason: str, old_prices, from_wire, trade_date_of) -> dict:
    """全件を引き直してキャッシュを作り直す。差分が使えないときの唯一の着地点。"""
    t0 = time.monotonic()
    full = fetch(None)
    got = _count_rows(full)
    _stats["misses"] += 1
    _stats["fetched_rows"] += got
    _log(f"MISS  {cache_path(with_volume).name} reason={reason} rows={got} "
         f"({time.monotonic() - t0:.1f}s)")

    # 参考値: 定常なら差分で取るはずだった行数。GHA キャッシュを後から入れる運びなので、
    # この数字だけが「実際どれだけ減るか」の事前見積りになる（机上の 9.3% を実測へ置き換える）。
    if fp.max_week_start and got:
        since = refresh_boundary(fp.max_week_start)
        n_delta = sum(1 for rows in full.values() for r in rows if trade_date_of(r) >= since)
        _log(f"      delta_preview since={since} rows={n_delta} "
             f"({100.0 * n_delta / got:.1f}% of full)")
        # ドリフト監査は **periodic-refresh のときだけ**。他の理由でのコールドは「過去が
        # 変わった」と既に分かっている状態（世代印が進んだ／行数が変わった）であり、
        # 差分と実データの食い違いはそこでは想定内＝監査に掛けると必ず誤検出になる。
        # ここで見たいのは「誰も何も宣言していないのに過去が変わっていた」だけ。
        if old_prices is not None and reason == "periodic-refresh":
            _audit_drift(old_prices, full, since, from_wire, trade_date_of)

    _assert_fresh(full, fp, trade_date_of)
    _write({ec: [to_wire(r) for r in rows] for ec, rows in full.items()},
           _Header(version=CACHE_VERSION, with_volume=with_volume, generation=fp.generation,
                   db_row_count=fp.n_rows, cached_row_count=got,
                   max_week_start=fp.max_week_start, full_loaded_at=_today()))
    return full


def _assert_fresh(prices: dict, fp: WeeklyFingerprint, trade_date_of) -> None:
    """マージ結果が DB の高水位に届いているか。**握らず raise**（歯止め2）。

    ISO 週の不変条件から `max(trade_date) >= max(week_start)` が常に成り立つので、これを
    下回るのは差分ロードが最新週を取れていない証拠。GHA では例外 = failure = 自動起票。
    """
    if fp.max_week_start is None:
        return
    newest = None
    for rows in prices.values():
        if not rows:
            continue
        td = trade_date_of(rows[-1])
        if td is not None and (newest is None or td > newest):
            newest = td
    if newest is None or newest < fp.max_week_start:
        raise WeeklyCacheStale(
            f"週次キャッシュが古い: merged max(trade_date)={newest} < "
            f"DB max(week_start)={fp.max_week_start}（差分ロードが最新週を取れていない・#480）。"
            f"FINAPP_WEEKLY_CACHE=0 で従来のフルロードへ戻せる")


def _audit_drift(old_wire: dict, full: dict, since: str, from_wire, trade_date_of) -> None:
    """強制コールド時に「差分経路が原理的に触らない過去区間」を突合する（歯止め4）。

    旧キャッシュと新フルロードが両方メモリにある瞬間にしか出来ず、**追加 Egress はゼロ**。
    ここが世代印フックの漏れ（手書き UPDATE・pg_restore・将来の新経路）に対する最終防衛線。
    """
    diffs = []
    for ec, rows in full.items():
        old_rows = old_wire.get(ec)
        if old_rows is None:
            continue
        new_old_part = [r for r in rows if trade_date_of(r) < since]
        cached_part = [from_wire(t) for t in old_rows]
        cached_part = [r for r in cached_part if trade_date_of(r) < since]
        if new_old_part != cached_part:
            for a, b in zip(cached_part, new_old_part):
                if a != b:
                    diffs.append(f"{ec}@{trade_date_of(a)} {a} -> {b}")
                    break
            else:
                diffs.append(f"{ec} row count {len(cached_part)} -> {len(new_old_part)}")
    if diffs:
        raise WeeklyCacheDrift(
            f"週次キャッシュのドリフト検出（since={since} より前の {len(diffs)} 社が不一致）: "
            + " | ".join(diffs[:_DRIFT_EXAMPLES])
            + f"{' ...' if len(diffs) > _DRIFT_EXAMPLES else ''}"
            + "。世代印を進めずに過去週を書き換えた経路がある（#480 の残存リスク）")


def _emit_summary() -> None:
    if not (_stats["hits"] or _stats["misses"]):
        return   # 一度も使っていなければ黙る
    _log(f"summary hits={_stats['hits']} misses={_stats['misses']} "
         f"fetched_rows={_stats['fetched_rows']} dir={cache_dir()}")


atexit.register(_emit_summary)
