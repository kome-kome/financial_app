"""ミラー3本をローカル同士で予行演習する（Issue #481 B-2〜B-4 の実証）。

## なぜ必要か

Supabase は Egress 超過で 2026-08-18 まで restricted。しかし B-2〜B-4 の**本当に危ない部分**は
Supabase 無しで検証できる。エンドポイントを引数化してあるので、**両方をローカルへ向ければ
本番と完全に同一のコードパスが走る**（`mirror_common` の docstring 参照）。

証明したいのは次の2点。どちらも「8/18 に初めて試して落ちる」と復旧がもう1か月先になる:

1. **非 superuser で FK 順序 restore が成立するか。**
   `edinet` は superuser でないため `pg_restore --disable-triggers` が使えず、FK は
   テーブル順序だけで満たすしかない。しかも**ダンプの TOC はアルファベット順**であって
   `--table` の指定順ではない（2026-08-16 実測）ので、1回の pg_restore に任せると
   「`companies` の頭文字が c だから偶然通っている」状態になる。1表ずつ流す実装が
   本当に効いているかを実データで確かめる。
2. **27週オーバーラップが遡及訂正を拾うか。**
   `_recompute_weeks_from_daily` は最大 `DAILY_WINDOW_DAYS=183` 日遡って過去週を上書きする。
   Issue #481 / #480 の当初案「末尾8週」では 56 日ぶんしか覆えない。**20週前のバーを
   書き換えてから同期し、値レベルのチェックサムが一致すること**を確認する。

## 実 `financial_db` には一切触れない

専用の2 DB（`financial_db_rehearsal_src` / `_dst`）を作って回し、`--drop` で消す。
「TRUNCATE で掃除する」ではなく**器ごと捨てる**ので、予行演習の合成データが本番ミラーの
投入先に残る経路が構造的に存在しない。

## 前提: CREATEDB 権限（1回だけ superuser 操作が要る）

`edinet` は既定で CREATEDB を持たない（2026-08-15 実測 `rolcreatedb=f`）。
権限が無ければ本スクリプトは**コマンドを表示して止まる**（勝手に superuser 接続はしない）。

実行:
    python -m scripts.mirror_rehearse            # 手順を表示するだけ
    python -m scripts.mirror_rehearse --apply    # 一連を実行
    python -m scripts.mirror_rehearse --drop     # 後片付け（2 DB を削除）

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text

from database import Base
from scripts import mirror_common as mc

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DB = "financial_db_rehearsal_src"
DST_DB = "financial_db_rehearsal_dst"

# 合成データの基準週（月曜）。実行日に依存させないことで結果を再現可能にする。
BASE_WEEK = date(2026, 8, 10)
N_WEEKS = 30
# 訂正を仕込む週。**8週(56日)より古く、27週(189日)より新しい**位置に置くのが要点。
CORRECTION_WEEKS_BACK = 20

CODES = ("E90001", "E90002", "E90003")


# ── 器 ────────────────────────────────────────────────────────────────────────

def _swap_db(url: str, dbname: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + dbname, p.query, p.fragment))


def base_url() -> str:
    """ローカル既定の接続 URL（`FINAPP_DB_TARGET=local` の解決結果）。"""
    return mc.resolve_endpoint("local").url


def admin_engine():
    """`postgres` メンテナンス DB への接続（CREATE/DROP DATABASE は他 DB からしか打てない）。"""
    return create_engine(_swap_db(base_url(), "postgres"),
                         isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def ensure_createdb(conn) -> None:
    ok = conn.execute(text(
        "SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user")).scalar()
    if ok:
        return
    user = conn.execute(text("SELECT current_user")).scalar()
    raise SystemExit(
        f"中止: ロール {user} に CREATEDB 権限がありません。\n"
        "  予行演習には専用の2 DB が要ります（実 financial_db を汚さないため）。\n"
        "  superuser で1回だけ次を実行してください:\n"
        '    & "C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe" -U postgres -h localhost '
        '-c "ALTER ROLE edinet CREATEDB;"\n'
        "  （financial_db の中身には触れない付与操作です）")


def db_exists(conn, name: str) -> bool:
    return bool(conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                             {"n": name}).scalar())


def create_dbs(conn) -> list[str]:
    made = []
    for name in (SRC_DB, DST_DB):
        if db_exists(conn, name):
            print(f"  既存: {name}")
            continue
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        made.append(name)
        print(f"  作成: {name}")
    return made


def drop_dbs(conn) -> list[str]:
    dropped = []
    for name in (SRC_DB, DST_DB):
        if not db_exists(conn, name):
            continue
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name})
        conn.execute(text(f'DROP DATABASE "{name}"'))
        dropped.append(name)
        print(f"  削除: {name}")
    return dropped


def init_schema(url: str, label: str) -> None:
    """既存の `setup_local_db.py` をそのまま使う（コード変更ゼロでスキーマを作る）。

    `init_db()` は `database.engine`（モジュールグローバル）に固定なので、別 DB へ打つには
    別プロセスで環境変数を差し替えるのが最も素直。`load_dotenv()` は override=False なので
    ここで立てた `DATABASE_URL_LOCAL` が `.env` に勝つ。
    """
    env = {**os.environ, "FINAPP_DB_TARGET": "local", "DATABASE_URL_LOCAL": url,
           "FINAPP_JOB": f"rehearse-init-{label}"}
    proc = subprocess.run([sys.executable, "-m", "scripts.setup_local_db", "--apply"],
                          cwd=BASE_DIR, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"{label} のスキーマ作成に失敗しました:\n{proc.stdout}\n{proc.stderr}")
    print(f"  スキーマ作成: {label}")


# ── 合成シード ────────────────────────────────────────────────────────────────

def _week(offset: int) -> str:
    return (BASE_WEEK + timedelta(weeks=offset)).isoformat()


def seed_rows() -> dict[str, list[dict]]:
    """全16表に最低1行。**0行の表があるとその表の扱いのバグが予行演習を素通りする**。

    仕込んである罠:
      - 日本語・全角記号（COPY の UTF-8 往復）
      - JSON 列（plugin_tuned_params / macro_beta_meta）
      - NULL 混在（volume_sum が NULL の週・finished_at が NULL のログ）
      - Date 型と String(10) の混在（period_end と week_start）
      - **serial の id を 1000 番台から振る**（restore 後に setval を怠ると次の INSERT が衝突する）
      - FK 子のうち1行は E90003 だけを参照（companies を後に流したら確実に落ちる）
    """
    now = datetime(2026, 8, 16, 3, 0, 0)
    rows: dict[str, list[dict]] = {}

    rows["companies"] = [
        {"id": 1001 + i, "edinet_code": code, "sec_code": f"9{i}010",
         "name": f"予行テスト{i + 1}株式会社／〈全角〉", "industry": "情報・通信業",
         "market": "プライム", "fiscal_month": 3, "issued_shares": 1.0e8 + i,
         "is_active": True, "created_at": now, "updated_at": now}
        for i, code in enumerate(CODES)
    ]
    rows["financial_records"] = [
        {"id": 2001 + i, "edinet_code": code, "year": 2025, "period_type": "annual",
         "period_end": date(2025, 3, 31), "doc_id": f"S1000{i}",
         "created_at": now, "updated_at": now}
        for i, code in enumerate(CODES)
    ] + [
        # FK 順序の検証用: companies が先に入っていなければ確実に違反する行
        {"id": 2004, "edinet_code": CODES[2], "year": 2024, "period_type": "annual",
         "period_end": date(2024, 3, 31), "doc_id": "S10099",
         "created_at": now, "updated_at": now},
    ]
    weekly = []
    for code in CODES:
        for w in range(N_WEEKS):
            offset = -(N_WEEKS - 1 - w)
            weekly.append({
                "edinet_code": code, "week_start": _week(offset),
                "trade_date": (BASE_WEEK + timedelta(weeks=offset, days=4)).isoformat(),
                "close_last": 1000.0 + w, "n_days": 5,
                # 一部の週は volume_sum が NULL（aggregate_weeks が実際に出す形）
                "volume_sum": None if w % 7 == 0 else 10000.0 + w,
                "turnover_sum": 1.0e7 + w,
            })
    rows["stock_price_weekly"] = weekly
    rows["statement_disclosure"] = [
        {"disc_no": f"D9000{i}", "edinet_code": code, "sec_code": f"9{i}010",
         "disc_date": _week(-(i * 4)), "created_at": now}
        for i, code in enumerate(CODES)
    ]
    rows["macro_data"] = [
        {"id": 3001 + i, "series_code": "TEST_SERIES", "series_name": "予行用系列",
         "category": "test", "trade_date": _week(-i),
         "close": 100.0 + i, "created_at": now}
        for i in range(20)
    ]
    rows["regression_results"] = [
        {"edinet_code": code, "year": 2025, "period_end": date(2025, 3, 31),
         "predicted_market_cap": 1.0e11, "gap_ratio": 0.1 * i, "model": "sector_ols",
         "sector": "情報・通信業", "computed_at": now}
        for i, code in enumerate(CODES)
    ]
    rows["macro_beta_meta"] = [
        {"id": 4001, "run_id": "rehearse-run-1", "snapshot_date": _week(0),
         "selected_factors": ["f1", "f2"], "factor_cov": [[1.0, 0.0], [0.0, 1.0]],
         "hyperparams": {"tau": 0.1}, "created_at": now},
    ]
    rows["macro_beta_loadings"] = [
        {"id": 5001 + i, "run_id": "rehearse-run-1", "edinet_code": code,
         "factor_name": "f1", "loading_mean": 0.5 + i, "loading_se": 0.1,
         "created_at": now}
        for i, code in enumerate(CODES)
    ]
    rows["recommend_factor_premia"] = [
        {"id": 6001 + i, "run_id": "rehearse-run-1", "factor_name": f"prem{i}",
         "mean_b": 1e-300 if i == 0 else -1.5, "newey_west_se": 0.2,
         "t_stat": 2.0, "p_value": 0.04, "n_periods": 60, "computed_at": now}
        for i in range(2)
    ]
    for tbl in ("macro_dlm_scores", "macro_enet_scores",
                "macro_ensemble_scores", "macro_gbdt_scores"):
        rows[tbl] = [
            {"edinet_code": code, "mu": 0.01 * (i + 1), "snapshot_date": _week(0),
             "snapshot_date_min": _week(-1), "n_stale": 0, "created_at": now}
            for i, code in enumerate(CODES)
        ]
    for tbl in ("macro_enet_scores", "macro_gbdt_scores"):
        for r in rows[tbl]:
            r["r1_prime"] = 0.02
    rows["plugin_tuned_params"] = [
        {"plugin_name": "rehearse_plugin", "params_json": {"alpha": 0.5, "cols": ["a", "b"]},
         "objective_name": "rank_ic", "objective_value": 0.12,
         "leaderboard_json": [{"alpha": 0.5, "score": 0.12}], "n_combos": 8,
         "data_fingerprint": "abc123", "tuned_at": now},
    ]
    rows["app_settings"] = [
        {"key": "REHEARSE_FLAG", "value": "1",
         "updated_at": datetime(2026, 8, 16, 3, 0, 0, tzinfo=timezone.utc)},
    ]
    rows["collection_logs"] = [
        {"id": 7001, "job_type": "rehearse", "status": "success", "started_at": now,
         "finished_at": now + timedelta(minutes=5), "companies_processed": 3,
         "records_saved": 3, "errors_count": 0, "message": "ok"},
        # finished_at が NULL ＝ running。extra_where で毎回取り直す対象
        {"id": 7002, "job_type": "rehearse", "status": "running", "started_at": now,
         "finished_at": None, "companies_processed": 0, "records_saved": 0,
         "errors_count": 0, "message": None},
    ]
    return rows


def seed(url: str) -> int:
    eng = create_engine(url)
    rows = seed_rows()
    missing = set(mc.mirror_tables()) - set(rows)
    if missing:
        raise SystemExit(f"シードが不足しています（0行の表を作らない方針）: {sorted(missing)}")
    total = 0
    try:
        with eng.begin() as conn:
            for t in reversed(mc.mirror_tables()):       # 子から消す
                conn.execute(text(f'DELETE FROM public."{t}"'))
            for t in mc.mirror_tables():                 # 親から入れる
                conn.execute(Base.metadata.tables[t].insert(), rows[t])
                total += len(rows[t])
            mc.resync_sequences(conn, mc.mirror_tables())
    finally:
        eng.dispose()
    return total


def mutate(url: str) -> dict:
    """src だけを変化させる。**遡及訂正を 20週前に置く**のがこの関数の主眼。"""
    eng = create_engine(url)
    corrected_week = _week(-CORRECTION_WEEKS_BACK)
    try:
        with eng.begin() as conn:
            # 1) 新しい週を2本追加（高水位が前進する）
            for code in CODES:
                for offset in (1, 2):
                    conn.execute(text(
                        'INSERT INTO public.stock_price_weekly '
                        '(edinet_code, week_start, trade_date, close_last, n_days, '
                        ' volume_sum, turnover_sum) '
                        'VALUES (:c, :w, :t, :p, 5, 12345, 1e7)'),
                        {"c": code, "w": _week(offset),
                         "t": (BASE_WEEK + timedelta(weeks=offset, days=4)).isoformat(),
                         "p": 2000.0 + offset})
            # 2) 20週前のバーを書き換える（= #465 型の遡及訂正）。
            #    高水位も件数も動かないので、**チェックサムでしか検出できない**。
            conn.execute(text(
                'UPDATE public.stock_price_weekly SET close_last = close_last * 10 '
                'WHERE week_start = :w'), {"w": corrected_week})
            # 3) onupdate 付きの列が進むケース
            conn.execute(text(
                "UPDATE public.companies SET name = name || '（改称）', "
                "updated_at = now() WHERE edinet_code = :c"), {"c": CODES[0]})
            # 4) 追記（macro_data）と全置換表（macro_gbdt_scores）の値変更
            conn.execute(text(
                "INSERT INTO public.macro_data "
                "(series_code, series_name, category, trade_date, close, created_at) "
                "VALUES ('TEST_SERIES', '予行用系列', 'test', :d, 999.0, now())"),
                {"d": _week(1)})
            conn.execute(text(
                "UPDATE public.macro_gbdt_scores SET mu = mu + 0.5"))
    finally:
        eng.dispose()
    return {"corrected_week": corrected_week}


# ── 手順の実行 ────────────────────────────────────────────────────────────────

def run_step(label: str, argv: list[str], *, expect: int) -> bool:
    env = {**os.environ, "FINAPP_JOB": f"rehearse-{label}"}
    env.pop("FINAPP_DB_TARGET", None)
    proc = subprocess.run([sys.executable, "-m", *argv], cwd=BASE_DIR, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = proc.returncode == expect
    print(f"\n=== {label}  (exit={proc.returncode}, 期待={expect}) {'OK' if ok else 'NG'}")
    tail = (proc.stdout or "").strip().splitlines()
    for line in tail[-14:]:
        print("   " + line)
    if not ok and proc.stderr:
        for line in proc.stderr.strip().splitlines()[-8:]:
            print("   ! " + line)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ミラー3本をローカル同士で予行演習する（Supabase へは接続しない）")
    ap.add_argument("--apply", action="store_true", help="実際に一連を実行する")
    ap.add_argument("--drop", action="store_true", help="予行用の2 DB を削除して終了")
    args = ap.parse_args()

    src_url = _swap_db(base_url(), SRC_DB)
    dst_url = _swap_db(base_url(), DST_DB)
    print(f"予行用 source: {mc.mask_url(src_url)}")
    print(f"予行用 dest  : {mc.mask_url(dst_url)}")
    print(f"実 financial_db には一切触れません\n")

    adm = admin_engine()
    try:
        with adm.connect() as conn:
            if args.drop:
                dropped = drop_dbs(conn)
                print(f"\n削除 {len(dropped)} 件")
                return 0
            ensure_createdb(conn)
            exists = [n for n in (SRC_DB, DST_DB) if db_exists(conn, n)]
    finally:
        adm.dispose()

    if not args.apply:
        print("実行する操作:")
        for i, s in enumerate([
            f"CREATE DATABASE {SRC_DB} / {DST_DB}（既存 {len(exists)} 件はそのまま）",
            "両方へ setup_local_db --apply でスキーマを作成",
            f"src へ合成シードを投入（{len(mc.mirror_tables())} 表・週次 {len(CODES)}社x{N_WEEKS}週）",
            "mirror_pull src -> dst --apply  -> mirror_verify --level checksum が 0",
            f"src を変化させる（新規2週 + {CORRECTION_WEEKS_BACK}週前のバーを書換 + 改称）",
            "mirror_verify --level checksum が 1（乖離を検出できること）",
            "mirror_sync --apply -> mirror_verify --level checksum が再び 0",
        ], 1):
            print(f"  {i}. {s}")
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")
        print("後片付けは --drop。")
        return 0

    adm = admin_engine()
    try:
        with adm.connect() as conn:
            print("[器] DB を用意しています...")
            create_dbs(conn)
    finally:
        adm.dispose()

    init_schema(src_url, SRC_DB)
    init_schema(dst_url, DST_DB)

    n = seed(src_url)
    print(f"[シード] src へ {n:,} 行を投入しました")

    ep = ["--source-url", src_url, "--dest-url", dst_url]
    results: list[tuple[str, bool]] = []

    results.append(("1. pull", run_step(
        "pull", ["scripts.mirror_pull", *ep, "--apply", "--allow-full-pull"], expect=0)))
    results.append(("2. pull 直後は一致", run_step(
        "verify-after-pull", ["scripts.mirror_verify", *ep, "--level", "checksum"], expect=0)))

    info = mutate(src_url)
    hi_week = _week(2)
    print(f"\n[変化] src を更新しました。訂正した週 = {info['corrected_week']}"
          f" / 新しい高水位 = {hi_week}")
    weeks_back = CORRECTION_WEEKS_BACK + 2
    print(f"  訂正位置は高水位から {weeks_back} 週前 ＝ "
          f"当初案の 8 週(56日)では覆えず、27 週(189日)なら覆う")

    results.append(("3. 乖離を検出", run_step(
        "verify-drift", ["scripts.mirror_verify", *ep, "--level", "checksum"], expect=1)))
    results.append(("4. sync", run_step(
        "sync", ["scripts.mirror_sync", *ep, "--apply"], expect=0)))
    results.append(("5. sync 後は一致", run_step(
        "verify-after-sync", ["scripts.mirror_verify", *ep, "--level", "checksum"], expect=0)))

    print("\n" + "=" * 60)
    for label, ok in results:
        print(f"  {'OK' if ok else 'NG'}  {label}")
    all_ok = all(ok for _, ok in results)
    print("=" * 60)
    if all_ok:
        print("予行演習すべて成功。FK 依存順 restore と 27週オーバーラップが実データで通りました。")
        print("後片付け: python -m scripts.mirror_rehearse --drop")
        return 0
    print("失敗した手順があります。上記 NG を確認してください。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
