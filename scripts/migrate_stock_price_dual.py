"""旧 stock_price_history（日次OHLCV全履歴）→ 2本立て（daily + weekly）への一回限り移行。

容量恒久対策（Supabase Free 500MB）。Supabase 側のピーク使用量を現状（≈448MB）から
上げないため、「ローカルで計算 → 旧テーブル DROP（即解放）→ コンパクトな新テーブルを投入」
の順で行う。素朴に新旧を併存させると 500MB を超え read-only に墜落するため厳禁。

ステージ（各ステージは冪等。途中失敗時は再実行で続きから）:
  1. dump      : 旧 stock_price_history を gzip CSV にローカル退避（旧が存在する場合のみ）
  2. drop      : 旧 stock_price_history を DROP（359MB 即解放）
  3. create    : 新テーブル（stock_price_daily / stock_price_weekly）を作成
  4. load      : dump から weekly 集約 + 直近 DAILY_WINDOW_DAYS の daily を bulk upsert
  5. verify    : 件数・カバレッジを照合して表示

使い方（ローカル・venv）:
  python migrate_stock_price_dual.py            # 全ステージ
  python migrate_stock_price_dual.py --verify   # 照合のみ
  退避ファイル stock_price_history_dump.csv.gz は verify 完了まで消さないこと。
"""
import argparse
import csv
import gzip
import os
import sys
from datetime import date, timedelta

from sqlalchemy import text

from database import (
    engine, Base, StockPriceDaily, StockPriceWeekly,
    aggregate_weeks, iso_week_start, _daily_cutoff, DAILY_WINDOW_DAYS,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

DUMP_PATH = "stock_price_history_dump.csv.gz"
OLD_TABLE = "stock_price_history"
BATCH     = 5000


def _table_exists(conn, name: str) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:n"
    ), {"n": name}).first() is not None


def stage_dump():
    """旧テーブルを gzip CSV へ退避（存在する場合のみ・ストリーミングで低メモリ）。"""
    if os.path.exists(DUMP_PATH):
        print(f"[dump] {DUMP_PATH} は既存 → スキップ")
        return
    with engine.connect() as conn:
        if not _table_exists(conn, OLD_TABLE):
            print(f"[dump] {OLD_TABLE} が存在しない → スキップ（移行済みか）")
            return
        n = conn.execute(text(f"SELECT count(*) FROM {OLD_TABLE}")).scalar()
        print(f"[dump] {OLD_TABLE} {n:,} 行を {DUMP_PATH} へ退避中…")
        result = conn.execution_options(stream_results=True).execute(text(
            f"SELECT edinet_code, trade_date, close, volume FROM {OLD_TABLE}"
        ))
        written = 0
        with gzip.open(DUMP_PATH, "wt", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["edinet_code", "trade_date", "close", "volume"])
            for chunk in iter(lambda: result.fetchmany(BATCH), []):
                w.writerows(chunk)
                written += len(chunk)
                if written % 200000 == 0:
                    print(f"[dump]   {written:,} 行")
        print(f"[dump] 完了: {written:,} 行")


def stage_drop():
    """旧テーブルを DROP（容量を即時解放）。dump 済みであることが前提。"""
    with engine.connect() as conn:
        if not _table_exists(conn, OLD_TABLE):
            print(f"[drop] {OLD_TABLE} は既に無い → スキップ")
            return
        if not os.path.exists(DUMP_PATH):
            sys.exit(f"[drop] 中止: {DUMP_PATH} が無い。先に dump を成功させること")
        conn.execute(text(f"DROP TABLE {OLD_TABLE}"))
        conn.commit()
        print(f"[drop] {OLD_TABLE} を DROP（容量解放）")


def stage_create():
    """新テーブルを作成（旧 DROP 後に実行＝併存ピークを避ける）。"""
    Base.metadata.create_all(
        bind=engine,
        tables=[StockPriceDaily.__table__, StockPriceWeekly.__table__],
    )
    print("[create] stock_price_daily / stock_price_weekly を作成")


def _read_dump():
    rows = []
    with gzip.open(DUMP_PATH, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            close = r["close"]
            if close in ("", None):
                continue
            vol = r["volume"]
            rows.append((
                r["edinet_code"], r["trade_date"][:10],
                float(close), float(vol) if vol not in ("", None) else None,
            ))
    return rows


def _bulk_upsert(conn, model, constraint, vals, set_cols):
    for i in range(0, len(vals), BATCH):
        batch = vals[i:i + BATCH]
        ins = pg_insert(model).values(batch)
        conn.execute(ins.on_conflict_do_update(
            constraint=constraint,
            set_={c: ins.excluded[c] for c in set_cols},
        ))
    conn.commit()


def stage_load():
    """dump から weekly 集約 + 直近 daily を計算して bulk upsert。"""
    if not os.path.exists(DUMP_PATH):
        sys.exit(f"[load] 中止: {DUMP_PATH} が無い")
    print(f"[load] dump を読み込み中…（{DUMP_PATH}）")
    rows = _read_dump()
    print(f"[load] {len(rows):,} 行を読み込み")

    # weekly: 全履歴を集約
    weekly = aggregate_weeks(rows)
    print(f"[load] weekly 集約 {len(weekly):,} 週")

    # daily: 直近 DAILY_WINDOW_DAYS のみ（最新窓のみ保持）
    cutoff = _daily_cutoff()
    daily = [{
        "edinet_code": ec, "trade_date": td, "close": cl, "volume": vol,
    } for ec, td, cl, vol in rows if td >= cutoff]
    print(f"[load] daily（{cutoff} 以降）{len(daily):,} 行")

    with engine.connect() as conn:
        _bulk_upsert(conn, StockPriceWeekly, "pk_stock_price_weekly", weekly,
                     ["trade_date", "close_last", "volume_sum", "turnover_sum", "n_days"])
        print("[load] weekly upsert 完了")
        _bulk_upsert(conn, StockPriceDaily, "pk_stock_price_daily", daily,
                     ["close", "volume"])
        print("[load] daily upsert 完了")


def stage_verify():
    with engine.connect() as conn:
        wk = conn.execute(text("SELECT count(*), count(distinct edinet_code), "
                               "min(trade_date), max(trade_date) FROM stock_price_weekly")).first()
        dl = conn.execute(text("SELECT count(*), count(distinct edinet_code), "
                               "min(trade_date), max(trade_date) FROM stock_price_daily")).first()
        print("[verify] weekly:", dict(zip(("rows", "codes", "oldest", "newest"), wk)))
        print("[verify] daily :", dict(zip(("rows", "codes", "oldest", "newest"), dl)))
        # weekly が daily の全 (edinet_code, week) を包含しているか（漏れ検査）
        miss = conn.execute(text(
            "SELECT count(*) FROM ("
            "  SELECT DISTINCT d.edinet_code, "
            "    to_char(date_trunc('week', d.trade_date::date), 'YYYY-MM-DD') AS ws "
            "  FROM stock_price_daily d) x "
            "LEFT JOIN stock_price_weekly w "
            "  ON w.edinet_code = x.edinet_code AND w.week_start = x.ws "
            "WHERE w.edinet_code IS NULL"
        )).scalar()
        print(f"[verify] weekly に欠けている daily の週: {miss}（0 が正常）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="照合のみ実行")
    args = ap.parse_args()

    if args.verify:
        stage_verify()
        return

    stage_dump()
    stage_drop()
    stage_create()
    stage_load()
    stage_verify()
    print("\n移行完了。照合に問題なければ "
          f"{DUMP_PATH} を削除して良い（それまで保持）。")


if __name__ == "__main__":
    main()
