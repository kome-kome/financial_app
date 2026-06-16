"""
collector_prices.py — 株価収集（stooq / JQuants / Yahoo）＋市場データ更新

内部でサブモジュール間の関数呼び出しは `import collector as _c` 経由で行う。
これにより `patch("collector.xxx")` で正しくモックが効く。
"""

import asyncio
import bisect
import logging
import os
import sys
from datetime import date, timedelta
from typing import Optional, Callable

import httpx

from database import (
    Company, FinancialRecord, MacroData,
    StockPriceDaily, StockPriceWeekly,
    record_prices_batch, trim_daily, latest_prices,
)
from collector_utils import (
    MAX_GAP_DAYS,
    _apply_price_to_record,
    _fetch_latest_fin_by_ec,
)

log = logging.getLogger(__name__)

STOOQ_CONCURRENCY      = 30
STOOQ_HIST_CONCURRENCY = 20
JQUANTS_ENDPOINT       = "https://api.jquants.com/v2/equities/bars/daily"
JQUANTS_RATE_SLEEP     = 20.0
JQUANTS_BACKFILL_DAYS  = 730
YAHOO_STOCK_RATE_SLEEP = 0.5

MACRO_SERIES: list[dict] = [
    {"code": "USDJPY",    "name": "USD/JPY",      "category": "fx",        "ticker": "usdjpy",   "yf_ticker": "USDJPY=X"},
    {"code": "EURJPY",    "name": "EUR/JPY",      "category": "fx",        "ticker": "eurjpy",   "yf_ticker": "EURJPY=X"},
    {"code": "US10Y",     "name": "米10年金利",   "category": "rate",      "ticker": "10usy.b",  "yf_ticker": "^TNX"},
    {"code": "JP10Y",     "name": "日10年金利",   "category": "rate",      "ticker": "10jpy.b",  "yf_ticker": "^JGB"},
    {"code": "NIKKEI225", "name": "日経225",      "category": "equity",    "ticker": "^nkx",     "yf_ticker": "^N225"},
    {"code": "TOPIX",     "name": "TOPIX",        "category": "equity",    "ticker": "^tpx",     "yf_ticker": "^TPX"},
    {"code": "SP500",     "name": "S&P500",       "category": "equity",    "ticker": "^spx",     "yf_ticker": "^GSPC"},
    {"code": "WTI",       "name": "WTI原油",      "category": "commodity", "ticker": "cl.f",     "yf_ticker": "CL=F"},
    {"code": "GOLD",      "name": "金",           "category": "commodity", "ticker": "gc.f",     "yf_ticker": "GC=F"},
]


def _c():
    """collector モジュールを遅延インポートして返す（循環 import 回避）"""
    return sys.modules.get('collector')


async def fetch_stock_price_stooq(sec_code: str, client: httpx.AsyncClient) -> Optional[float]:
    if not sec_code or len(sec_code) < 4:
        return None
    ticker = f"{sec_code}.jp"
    url = f"https://stooq.com/q/l/?s={ticker}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        values = lines[1].split(",")
        if len(values) < 7:
            return None
        close = values[6].strip()
        price = float(close)
        return price if price > 0 else None
    except Exception as e:
        log.debug(f"株価取得失敗 {sec_code}: {e}")
        return None


async def fetch_stock_history_stooq(
    session: httpx.AsyncClient,
    sec_code: str,
    date_from: str,
    date_to: str,
) -> list:
    url = f"https://stooq.com/q/d/l/?s={sec_code}.jp&d1={date_from}&d2={date_to}&i=d"
    try:
        r = await session.get(url, timeout=30)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        log.debug(f"stooq履歴取得失敗 {sec_code}: {e}")
        return []

    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            rows.append({
                "trade_date": parts[0],
                "open":       float(parts[1]),
                "high":       float(parts[2]),
                "low":        float(parts[3]),
                "close":      float(parts[4]),
                "volume":     float(parts[5]) if len(parts) > 5 else None,
            })
        except ValueError:
            continue
    return rows


async def _price_collection_driver(db, batch_gen) -> tuple[bool, int]:
    _col = _c()
    _record_prices_batch = getattr(_col, 'record_prices_batch', record_prices_batch)
    _trim_daily = getattr(_col, 'trim_daily', trim_daily)
    total = 0
    async for batch in batch_gen:
        if batch is None:
            db.commit()
            return True, total
        if batch:
            try:
                total += _record_prices_batch(db, batch, trim=False)
            except Exception as e:
                log.warning("株価バッチ保存失敗: %s", e)
    _trim_daily(db)
    return False, total


async def collect_stock_price_history(
    db,
    years_back: int = 3,
    max_companies: Optional[int] = None,
    on_progress: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
    skip_existing: bool = True,
    backfill: bool = False,
) -> dict:
    from sqlalchemy import func as sqlfunc

    today     = date.today()
    date_from = date(today.year - years_back, today.month, today.day)
    d1 = date_from.strftime("%Y%m%d")
    d2 = today.strftime("%Y%m%d")
    date_from_str = date_from.strftime("%Y-%m-%d")
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    companies = (
        db.query(Company.edinet_code, Company.sec_code, Company.name)
        .filter(Company.sec_code.isnot(None), Company.sec_code != "")
        .all()
    )
    if max_companies:
        companies = companies[:max_companies]

    minmax_dates: dict = {}
    latest_dates: dict = {}
    if skip_existing:
        if backfill:
            minmax_dates = {
                row.edinet_code: (row.min_date, row.max_date)
                for row in db.query(
                    StockPriceWeekly.edinet_code,
                    sqlfunc.min(StockPriceWeekly.trade_date).label("min_date"),
                    sqlfunc.max(StockPriceWeekly.trade_date).label("max_date"),
                ).group_by(StockPriceWeekly.edinet_code).all()
            }
        else:
            latest_dates = dict(
                db.query(StockPriceWeekly.edinet_code, sqlfunc.max(StockPriceWeekly.trade_date))
                .group_by(StockPriceWeekly.edinet_code)
                .all()
            )

    total = len(companies)
    skipped_total  = 0

    to_fetch = []
    for edinet_code, sec_code, name in companies:
        if skip_existing and backfill:
            entry = minmax_dates.get(edinet_code)
            if entry is None:
                to_fetch.append((edinet_code, sec_code, name, d1, d2))
            else:
                min_date, max_date = entry
                added = False
                if max_date < yesterday:
                    d1_fwd = (date.fromisoformat(max_date) + timedelta(days=1)).strftime("%Y%m%d")
                    to_fetch.append((edinet_code, sec_code, name, d1_fwd, d2))
                    added = True
                if min_date > date_from_str:
                    min_dt = date.fromisoformat(min_date)
                    if min_dt > date_from:
                        d2_bwd = (min_dt - timedelta(days=1)).strftime("%Y%m%d")
                        to_fetch.append((edinet_code, sec_code, name, d1, d2_bwd))
                        added = True
                if not added:
                    skipped_total += 1
        elif skip_existing:
            latest = latest_dates.get(edinet_code)
            if latest and latest >= yesterday:
                skipped_total += 1
                continue
            d1_company = (date.fromisoformat(latest) + timedelta(days=1)).strftime("%Y%m%d") if latest else d1
            to_fetch.append((edinet_code, sec_code, name, d1_company, d2))
        else:
            to_fetch.append((edinet_code, sec_code, name, d1, d2))

    fetch_total = len(to_fetch)
    progress_total = fetch_total if backfill else total
    if on_progress and skipped_total:
        on_progress(0 if backfill else skipped_total, progress_total,
                    f"[スキップ] {skipped_total}社は補完不要 → {fetch_total}件を取得します" if backfill
                    else f"[スキップ] {skipped_total}社は最新済み → {fetch_total}社を並列取得します")

    async def _stooq_batch_gen(session):
        sem = asyncio.Semaphore(STOOQ_HIST_CONCURRENCY)
        _col = _c()
        _fetch_stock_history_stooq = getattr(_col, 'fetch_stock_history_stooq', fetch_stock_history_stooq)

        async def _fetch_hist(ec, sc, nm, d1c, d2c):
            async with sem:
                rows = await _fetch_stock_history_stooq(session, sc, d1c, d2c)
            return ec, sc, nm, rows

        tasks = [asyncio.ensure_future(_fetch_hist(*item)) for item in to_fetch]
        completed = 0
        for coro in asyncio.as_completed(tasks):
            if cancel_check and cancel_check():
                for t in tasks:
                    t.cancel()
                if on_progress:
                    on_progress(completed, progress_total,
                                f"[停止] ユーザーによる停止（{completed}/{fetch_total}件処理済み）")
                db.commit()
                yield None
                return
            edinet_code, sec_code, name, rows = await coro
            completed += 1
            if on_progress:
                prog = completed if backfill else skipped_total + completed
                on_progress(prog, progress_total,
                            f"[{prog}/{progress_total}] {name}({sec_code}) {len(rows) if rows else 0}件")
            yield [
                {"edinet_code": edinet_code, "trade_date": r["trade_date"],
                 "close": r.get("close"), "volume": r.get("volume")}
                for r in rows
            ] if rows else []

    async with httpx.AsyncClient() as session:
        cancelled, inserted_total = await _price_collection_driver(db, _stooq_batch_gen(session))

    if cancelled:
        return {"cancelled": True, "inserted": inserted_total, "skipped": skipped_total}
    if on_progress:
        on_progress(progress_total, progress_total,
                    f"[完了] {total}社処理（スキップ:{skipped_total}社）、{inserted_total}件追加")
    return {"cancelled": False, "inserted": inserted_total, "skipped": skipped_total, "companies": total}


async def _jquants_fetch_date(session: httpx.AsyncClient, api_key: str, date_str: str) -> list:
    _col = _c()
    _JQUANTS_RATE_SLEEP = getattr(_col, 'JQUANTS_RATE_SLEEP', JQUANTS_RATE_SLEEP)
    headers = {"x-api-key": api_key}
    rows = []
    pagination_key = None
    while True:
        params: dict = {"date": date_str}
        if pagination_key:
            params["pagination_key"] = pagination_key
        r = await session.get(JQUANTS_ENDPOINT, headers=headers, params=params, timeout=30)
        if r.status_code == 429:
            log.warning(f"J-Quants 429: {date_str} → 90秒後に再試行")
            await asyncio.sleep(90)
            r = await session.get(JQUANTS_ENDPOINT, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                log.error(f"J-Quants 429: {date_str} → 再試行も429、スキップ")
                return []
        if r.status_code in (400, 404):
            break
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("data", []))
        pagination_key = data.get("pagination_key")
        if not pagination_key:
            break
        await asyncio.sleep(_JQUANTS_RATE_SLEEP)
    return rows


async def collect_stock_price_history_jquants(
    db,
    days_back: int = 14,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    on_progress: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
) -> dict:
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise ValueError("環境変数 JQUANTS_API_KEY が未設定です")

    today  = date.today()
    _from  = date_from if date_from is not None else (today - timedelta(days=days_back))
    _to    = date_to   if date_to   is not None else today
    span   = (_to - _from).days + 1
    dates = [
        (_from + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(span)
        if (_from + timedelta(days=i)).weekday() < 5
        and (_from + timedelta(days=i)) <= _to
    ]

    sec_to_edinet: dict = {
        row.sec_code: row.edinet_code
        for row in db.query(Company.sec_code, Company.edinet_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    }

    total = len(dates)

    async def _jquants_batch_gen(session):
        _col = _c()
        _jquants_fetch_date_fn = getattr(_col, '_jquants_fetch_date', _jquants_fetch_date)
        _JQUANTS_RATE_SLEEP = getattr(_col, 'JQUANTS_RATE_SLEEP', JQUANTS_RATE_SLEEP)
        completed = 0
        last_req_time: float = 0.0
        for date_str in dates:
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(completed, total, f"[停止] ユーザーによる停止（{completed}/{total}日処理済み）")
                db.commit()
                yield None
                return

            if completed > 0:
                elapsed = asyncio.get_event_loop().time() - last_req_time
                wait = _JQUANTS_RATE_SLEEP - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)

            last_req_time = asyncio.get_event_loop().time()
            quote_rows = await _jquants_fetch_date_fn(session, api_key, date_str)
            completed += 1

            if not quote_rows:
                if on_progress:
                    on_progress(completed, total, f"[{completed}/{total}] {date_str} スキップ（非営業日）")
                yield []
                continue

            records = []
            for q in quote_rows:
                code        = str(q.get("Code", ""))
                sec_code    = code[:4]
                edinet_code = sec_to_edinet.get(sec_code)
                if not edinet_code:
                    continue
                close_val = q.get("C")
                if close_val is None:
                    continue
                try:
                    records.append({
                        "edinet_code": edinet_code,
                        "sec_code":    sec_code,
                        "trade_date":  q["Date"],
                        "open":        float(q["O"])  if q.get("O")  is not None else None,
                        "high":        float(q["H"])  if q.get("H")  is not None else None,
                        "low":         float(q["L"])  if q.get("L")  is not None else None,
                        "close":       float(close_val),
                        "volume":      float(q["Vo"]) if q.get("Vo") is not None else None,
                    })
                except (KeyError, ValueError, TypeError):
                    continue

            seen: set = set()
            deduped = []
            for rec in records:
                if rec["edinet_code"] not in seen:
                    seen.add(rec["edinet_code"])
                    deduped.append(rec)

            if on_progress:
                on_progress(completed, total, f"[{completed}/{total}] {date_str} {len(deduped)}件")
            yield deduped

    async with httpx.AsyncClient() as session:
        cancelled, upserted_total = await _price_collection_driver(db, _jquants_batch_gen(session))

    if cancelled:
        return {"cancelled": True, "upserted": upserted_total}
    if on_progress:
        on_progress(total, total, f"[完了] {total}日処理・{upserted_total}件追加/更新")
    return {"cancelled": False, "upserted": upserted_total, "days": total}


def _update_market_data_latest(db) -> int:
    from sqlalchemy import func as sqlfunc

    subq = (
        db.query(
            StockPriceDaily.edinet_code,
            sqlfunc.max(StockPriceDaily.trade_date).label("max_date"),
        )
        .group_by(StockPriceDaily.edinet_code)
        .subquery()
    )
    latest_price_rows = (
        db.query(StockPriceDaily.edinet_code, StockPriceDaily.close)
        .join(
            subq,
            (StockPriceDaily.edinet_code == subq.c.edinet_code)
            & (StockPriceDaily.trade_date == subq.c.max_date),
        )
        .all()
    )

    valid_ecs = [ec for ec, price in latest_price_rows if price and price > 0]
    latest_fin_by_ec = _fetch_latest_fin_by_ec(db, valid_ecs)

    updated = 0
    for edinet_code, price in latest_price_rows:
        if price is None or price <= 0:
            continue
        latest = latest_fin_by_ec.get(edinet_code)
        if not latest:
            continue
        _apply_price_to_record(latest, price)
        updated += 1
        if updated % 200 == 0:
            db.commit()

    db.commit()
    log.info(f"update_market_data_from_history: {updated}社を更新")
    return updated


def _update_market_data_point_in_time(db) -> int:
    from collections import defaultdict

    all_records = db.query(FinancialRecord).all()

    latest_by_ec: dict = {}
    for rec in all_records:
        ec = rec.edinet_code
        if ec not in latest_by_ec or (rec.year or 0) > (latest_by_ec[ec].year or 0):
            latest_by_ec[ec] = rec

    dated_records = [r for r in all_records if r.period_end]
    if not dated_records:
        log.info("update_market_data_from_history(point_in_time): period_end ありレコードが空のためスキップ")
        for ec, info in latest_prices(db, list(latest_by_ec.keys())).items():
            latest_price = info.get("price")
            if not latest_price or latest_price <= 0:
                continue
            _apply_price_to_record(latest_by_ec[ec], latest_price)
        db.commit()
        return 0

    period_ends = [r.period_end[:10] for r in dated_records]
    date_lo = (date.fromisoformat(min(period_ends)) - timedelta(days=MAX_GAP_DAYS)).isoformat()
    date_hi = (date.fromisoformat(max(period_ends)) + timedelta(days=MAX_GAP_DAYS)).isoformat()
    ec_q = (
        db.query(FinancialRecord.edinet_code)
        .filter(FinancialRecord.period_end.isnot(None))
        .distinct()
    )
    weekly_rows = (
        db.query(
            StockPriceWeekly.edinet_code,
            StockPriceWeekly.trade_date,
            StockPriceWeekly.close_last,
        )
        .filter(
            StockPriceWeekly.edinet_code.in_(ec_q),
            StockPriceWeekly.trade_date >= date_lo,
            StockPriceWeekly.trade_date <= date_hi,
            StockPriceWeekly.close_last > 0,
        )
        .all()
    )

    if not weekly_rows:
        log.info("update_market_data_from_history(point_in_time): stock_price_weekly が空のためスキップ")
        return 0

    history: dict = defaultdict(list)
    for ec, td, cl in weekly_rows:
        history[ec].append((td, cl))
    for ec in history:
        history[ec].sort()

    updated = 0
    for rec in dated_records:
        prices = history.get(rec.edinet_code)
        if not prices:
            continue

        try:
            target = date.fromisoformat(rec.period_end[:10])
        except (ValueError, TypeError):
            continue

        dates = [p[0] for p in prices]
        pos = bisect.bisect_left(dates, target.isoformat())
        best_price = None
        best_gap = MAX_GAP_DAYS + 1

        for idx in (pos - 1, pos):
            if 0 <= idx < len(prices):
                td_str, cl = prices[idx]
                try:
                    td = date.fromisoformat(td_str[:10])
                except ValueError:
                    continue
                gap = abs((td - target).days)
                if gap < best_gap:
                    best_gap = gap
                    best_price = cl

        if best_price is None:
            continue

        _apply_price_to_record(rec, best_price)
        updated += 1
        if updated % 200 == 0:
            db.commit()

    for ec, info in latest_prices(db, list(latest_by_ec.keys())).items():
        latest_price = info.get("price")
        if not latest_price or latest_price <= 0:
            continue
        _apply_price_to_record(latest_by_ec[ec], latest_price)

    db.commit()
    log.info(f"update_market_data_from_history(point_in_time): {updated}レコードを更新")
    return updated


def update_market_data_from_history(db, point_in_time: bool = False) -> int:
    if not point_in_time:
        return _update_market_data_latest(db)
    return _update_market_data_point_in_time(db)


async def backfill_historical_stock_prices_yahoo(
    db,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    from collections import defaultdict

    cutoff = (date.today() - timedelta(days=JQUANTS_BACKFILL_DAYS)).isoformat()

    target_records = (
        db.query(FinancialRecord)
        .filter(
            FinancialRecord.stock_price.is_(None),
            FinancialRecord.period_end.isnot(None),
            FinancialRecord.period_end < cutoff,
        )
        .all()
    )
    if not target_records:
        log.info("backfill_historical_stock_prices_yahoo: 対象レコードなし")
        return 0

    sec_map = {
        c.edinet_code: c.sec_code
        for c in db.query(Company.edinet_code, Company.sec_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    }

    by_company: dict = defaultdict(list)
    for rec in target_records:
        sec_code = sec_map.get(rec.edinet_code)
        if sec_code and sec_code.strip():
            by_company[sec_code].append(rec)

    total   = len(by_company)
    updated = 0

    _col = _c()
    _fetch_yahoo_history = getattr(_col, 'fetch_yahoo_history', fetch_yahoo_history)

    async with httpx.AsyncClient() as session:
        for i, (sec_code, recs) in enumerate(sorted(by_company.items()), 1):
            if cancel_check and cancel_check():
                db.commit()
                if on_progress:
                    on_progress(i - 1, total, f"[Yahoo backfill] 停止（{updated}件更新済み）")
                return updated

            period_ends = sorted(r.period_end[:10] for r in recs)
            d_from = (date.fromisoformat(period_ends[0])  - timedelta(days=MAX_GAP_DAYS)).strftime("%Y%m%d")
            d_to   = (date.fromisoformat(period_ends[-1]) + timedelta(days=MAX_GAP_DAYS)).strftime("%Y%m%d")

            ticker = f"{sec_code}.T"
            rows = await _fetch_yahoo_history(session, ticker, d_from, d_to)

            if rows:
                price_dict = {r["trade_date"]: r["close"] for r in rows if r["close"]}
                price_dates = sorted(price_dict.keys())

                for rec in recs:
                    target_str = rec.period_end[:10]
                    pos = bisect.bisect_left(price_dates, target_str)
                    best_price, best_gap = None, MAX_GAP_DAYS + 1
                    for idx in (pos - 1, pos):
                        if 0 <= idx < len(price_dates):
                            td = price_dates[idx]
                            gap = abs((date.fromisoformat(td) - date.fromisoformat(target_str)).days)
                            if gap < best_gap:
                                best_gap, best_price = gap, price_dict[td]
                    if best_price and best_price > 0:
                        _apply_price_to_record(rec, best_price)
                        updated += 1

                if updated % 200 == 0:
                    db.commit()

            if on_progress and (i % 200 == 0 or i == total):
                on_progress(i, total, f"[Yahoo backfill {i}/{total}] {sec_code}  累計{updated}件更新")

            if YAHOO_STOCK_RATE_SLEEP > 0:
                await asyncio.sleep(YAHOO_STOCK_RATE_SLEEP)

    db.commit()
    log.info(f"backfill_historical_stock_prices_yahoo: {updated}件の financial_records を更新")
    return updated


async def fill_recent_stock_price_gap_yahoo(
    db,
    gap_days: int = 7,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    from sqlalchemy import func as sqlfunc

    latest_row = (db.query(sqlfunc.max(StockPriceDaily.trade_date)).scalar()
                  or db.query(sqlfunc.max(StockPriceWeekly.trade_date)).scalar())
    if not latest_row:
        log.info("fill_recent_stock_price_gap_yahoo: 株価データが空のためスキップ")
        return {"skipped": True, "reason": "empty"}

    latest_date = date.fromisoformat(latest_row)
    gap = (date.today() - latest_date).days
    if gap <= gap_days:
        log.info(f"fill_recent_stock_price_gap_yahoo: 最新日 {latest_date}（{gap}日前）→ ギャップなし")
        return {"skipped": True, "reason": "no_gap", "latest": str(latest_date)}

    log.info(f"fill_recent_stock_price_gap_yahoo: 最新日 {latest_date}（{gap}日前）→ Yahoo で補完")
    d_from = (latest_date + timedelta(days=1)).strftime("%Y%m%d")
    d_to   = date.today().strftime("%Y%m%d")

    companies = [
        (row.sec_code, row.edinet_code)
        for row in db.query(Company.sec_code, Company.edinet_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    ]
    total = len(companies)

    _col = _c()
    _fetch_yahoo_history = getattr(_col, 'fetch_yahoo_history', fetch_yahoo_history)

    async def _yahoo_batch_gen(session):
        for i, (sec_code, edinet_code) in enumerate(companies, 1):
            rows = await _fetch_yahoo_history(session, f"{sec_code}.T", d_from, d_to)
            records = [
                {"edinet_code": edinet_code, "trade_date": r["trade_date"],
                 "close": r["close"], "volume": r.get("volume")}
                for r in rows if r["close"]
            ] if rows else []
            if on_progress and i % 500 == 0:
                on_progress(i, total, f"[Yahoo gap-fill {i}/{total}]")
            yield records
            await asyncio.sleep(YAHOO_STOCK_RATE_SLEEP)

    async with httpx.AsyncClient() as session:
        _, upserted = await _price_collection_driver(db, _yahoo_batch_gen(session))

    log.info(f"fill_recent_stock_price_gap_yahoo: {upserted}件を株価テーブルへ集約保存")
    return {"skipped": False, "upserted": upserted, "from": d_from, "to": d_to}


async def update_market_data(db,
                             max_companies: Optional[int] = None,
                             on_progress: Optional[Callable] = None,
                             cancel_check: Optional[Callable] = None):
    companies = (db.query(Company)
                 .filter(Company.sec_code.isnot(None))
                 .filter(Company.sec_code != "")
                 .all())
    if max_companies:
        companies = companies[:max_companies]

    total = len(companies)
    updated = 0
    log.info(f"市場データ更新開始: {total}社")

    latest_fin_by_ec = _fetch_latest_fin_by_ec(db, [c.edinet_code for c in companies])

    sem = asyncio.Semaphore(STOOQ_CONCURRENCY)

    async def _fetch_price(company, client):
        async with sem:
            price = await fetch_stock_price_stooq(company.sec_code, client)
        return company, price

    async with httpx.AsyncClient() as client:
        tasks = [asyncio.ensure_future(_fetch_price(c, client)) for c in companies]
        completed = 0
        for coro in asyncio.as_completed(tasks):
            if cancel_check and cancel_check():
                for t in tasks:
                    t.cancel()
                if on_progress:
                    on_progress(completed, total,
                                f"[停止] ユーザーによる停止（{completed}/{total}社処理済み）")
                db.commit()
                log.info(f"市場データ更新をキャンセルしました（{completed}/{total}社処理済み）")
                return True

            company, price = await coro
            completed += 1
            if on_progress:
                on_progress(completed, total,
                            f"[{completed}/{total}] 株価取得: {company.sec_code} {company.name}")

            if price is None:
                continue

            latest = latest_fin_by_ec.get(company.edinet_code)
            if not latest:
                continue

            _apply_price_to_record(latest, price)
            updated += 1
            if updated % 50 == 0:
                db.commit()
                log.info(f"市場データ更新中: {updated}社完了")

    db.commit()
    log.info(f"市場データ更新完了: {updated}/{total}社")
    return False


async def fetch_yahoo_history(
    session: httpx.AsyncClient,
    yf_ticker: str,
    date_from: str,
    date_to:   str,
) -> list:
    import calendar
    try:
        y1, m1, d1_ = int(date_from[:4]), int(date_from[4:6]), int(date_from[6:8])
        y2, m2, d2_ = int(date_to[:4]),   int(date_to[4:6]),   int(date_to[6:8])
        period1 = int(calendar.timegm((y1, m1, d1_, 0, 0, 0)))
        period2 = int(calendar.timegm((y2, m2, d2_, 23, 59, 59)))
    except (ValueError, IndexError) as e:
        log.debug(f"Yahoo Finance 日付変換失敗 {yf_ticker}: {e}")
        return []

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}"
           f"?interval=1d&period1={period1}&period2={period2}")
    try:
        r = await session.get(url, timeout=30,
                              headers={"User-Agent": "Mozilla/5.0",
                                       "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug(f"Yahoo Finance 取得失敗 {yf_ticker}: {e}")
        return []

    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as e:
        log.debug(f"Yahoo Finance レスポンス解析失敗 {yf_ticker}: {e}")
        return []

    def _sf(lst, i):
        v = lst[i] if i < len(lst) else None
        return float(v) if v is not None else None

    rows = []
    for i, ts in enumerate(timestamps):
        close = _sf(quote.get("close", []), i)
        if close is None:
            continue
        rows.append({
            "trade_date": date.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "open":   _sf(quote.get("open",   []), i),
            "high":   _sf(quote.get("high",   []), i),
            "low":    _sf(quote.get("low",    []), i),
            "close":  close,
            "volume": _sf(quote.get("volume", []), i),
        })
    return rows


async def fetch_stooq_history(
    session: httpx.AsyncClient,
    ticker:  str,
    date_from: str,
    date_to:   str,
) -> list:
    url = f"https://stooq.com/q/d/l/?s={ticker}&d1={date_from}&d2={date_to}&i=d"
    try:
        r = await session.get(url, timeout=30)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        log.debug(f"stooq マクロ取得失敗 {ticker}: {e}")
        return []

    rows = []
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            close = float(parts[4])
        except ValueError:
            continue
        def _f(s):
            try: return float(s)
            except ValueError: return None
        rows.append({
            "trade_date": parts[0],
            "open":       _f(parts[1]),
            "high":       _f(parts[2]),
            "low":        _f(parts[3]),
            "close":      close,
            "volume":     _f(parts[5]) if len(parts) > 5 else None,
        })
    return rows


async def collect_macro_data(
    db,
    years_back: int = 5,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
):
    today    = date.today()
    start    = today - timedelta(days=int(years_back * 365.25))
    d1       = start.strftime("%Y%m%d")
    d2       = today.strftime("%Y%m%d")
    total    = len(MACRO_SERIES)
    saved    = 0

    _col = _c()
    _fetch_yahoo_history = getattr(_col, 'fetch_yahoo_history', fetch_yahoo_history)
    _fetch_stooq_history = getattr(_col, 'fetch_stooq_history', fetch_stooq_history)

    async with httpx.AsyncClient() as session:
        for i, series in enumerate(MACRO_SERIES, 1):
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(i-1, total, "[マクロ収集] ユーザー停止")
                return saved

            rows = await _fetch_yahoo_history(session, series["yf_ticker"], d1, d2)
            src = "Yahoo Finance"
            if not rows:
                rows = await _fetch_stooq_history(session, series["ticker"], d1, d2)
                src = "stooq"
            if on_progress:
                on_progress(i-1, total, f"[マクロ {i}/{total}] {series['name']} ({src}) 取得中")
            if not rows:
                if on_progress:
                    on_progress(i, total, f"[マクロ {i}/{total}] {series['name']} データ無し")
                continue

            existing_dates = {
                d for (d,) in db.query(MacroData.trade_date)
                                .filter(MacroData.series_code == series["code"]).all()
            }
            ins, upd = 0, 0
            for r in rows:
                if r["trade_date"] in existing_dates:
                    db.query(MacroData).filter_by(
                        series_code=series["code"], trade_date=r["trade_date"]
                    ).update({
                        "open": r["open"], "high": r["high"], "low": r["low"],
                        "close": r["close"], "volume": r["volume"],
                    })
                    upd += 1
                else:
                    db.add(MacroData(
                        series_code = series["code"],
                        series_name = series["name"],
                        category    = series["category"],
                        trade_date  = r["trade_date"],
                        open=r["open"], high=r["high"], low=r["low"],
                        close=r["close"], volume=r["volume"],
                    ))
                    ins += 1
            db.commit()
            saved += ins + upd
            if on_progress:
                on_progress(i, total, f"[マクロ {i}/{total}] {series['name']}: {ins}件新規, {upd}件更新")

    return saved
