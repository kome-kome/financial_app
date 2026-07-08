"""株価収集（stooq / J-Quants / Yahoo Finance）とマクロ指標収集。"""
import bisect
import calendar
import io
import zipfile
import asyncio
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional, Callable

import httpx
import pandas as pd
from sqlalchemy import func as sqla_func
from sqlalchemy.exc import SQLAlchemyError

from database import (
    SessionLocal, Company, FinancialRecord, MacroData,
    XbrlRawDocument, upsert_company, upsert_financial,
    upsert_xbrl_raw, pack_elements, unpack_elements,
    build_xbrl_map,
    StockPriceDaily, StockPriceWeekly,
    record_prices_batch, trim_daily, latest_prices,
    upsert_macro_batch,
)

from collector_utils import *


async def fetch_stock_price_stooq(sec_code: str, client: httpx.AsyncClient) -> Optional[float]:
    """stooqから日本株の現在株価（終値）を取得する"""
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
        # 形式: Symbol,Date,Time,Open,High,Low,Close,Volume
        values = lines[1].split(",")
        if len(values) < 7:
            return None
        close = values[6].strip()
        price = float(close)
        return price if price > 0 else None
    except Exception as e:
        log.debug(f"株価取得失敗 {sec_code}: {e}")
        return None


def _stooq_float(s: str) -> float | None:
    """stooq CSV セルを float 化。パース不能なら None（欠損許容経路用）。"""
    try:
        return float(s)
    except ValueError:
        return None


def _parse_stooq_csv(text: str, *, strict: bool) -> list:
    """stooq 日次 OHLCV CSV（"Date,Open,High,Low,Close,Volume"）をパースする。
    close は両経路とも必須（パース不能行はスキップ）。
    strict=True : open/high/low/volume も float 必須で、不能なら行ごとスキップ（個別銘柄経路）。
    strict=False: open/high/low/volume は None 許容（マクロ経路）。
    """
    rows = []
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return []
    for line in lines[1:]:   # ヘッダー行をスキップ
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            close = float(parts[4])
        except ValueError:
            continue
        if strict:
            try:
                row = {
                    "trade_date": parts[0],          # "YYYY-MM-DD"
                    "open":       float(parts[1]),
                    "high":       float(parts[2]),
                    "low":        float(parts[3]),
                    "close":      close,
                    "volume":     float(parts[5]) if len(parts) > 5 else None,
                }
            except ValueError:
                continue
        else:
            row = {
                "trade_date": parts[0],
                "open":       _stooq_float(parts[1]),
                "high":       _stooq_float(parts[2]),
                "low":        _stooq_float(parts[3]),
                "close":      close,
                "volume":     _stooq_float(parts[5]) if len(parts) > 5 else None,
            }
        rows.append(row)
    return rows


async def _fetch_stooq_ohlcv(
    session: httpx.AsyncClient,
    ticker: str,
    date_from: str,   # "YYYYMMDD"
    date_to: str,     # "YYYYMMDD"
    *,
    strict: bool,
    log_label: str,
) -> list:
    """stooq 日次 OHLCV CSV を取得・パースする単一実装。
    ticker 組み立ては呼び出し側が行う（個別銘柄は `.jp` 付与・マクロはそのまま）。"""
    url = f"https://stooq.com/q/d/l/?s={ticker}&d1={date_from}&d2={date_to}&i=d"
    try:
        r = await session.get(url, timeout=30)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        log.debug(f"{log_label}: {e}")
        return []
    return _parse_stooq_csv(text, strict=strict)


async def fetch_stock_history_stooq(
    session: httpx.AsyncClient,
    sec_code: str,
    date_from: str,   # "YYYYMMDD"
    date_to: str,     # "YYYYMMDD"
) -> list:
    """stooq 日次 OHLCV を取得して [{trade_date, open, high, low, close, volume}] で返す（個別銘柄・`.jp` 付与）。"""
    return await _fetch_stooq_ohlcv(
        session, f"{sec_code}.jp", date_from, date_to,
        strict=True, log_label=f"stooq履歴取得失敗 {sec_code}",
    )


async def _price_collection_driver(db, batch_gen) -> tuple[bool, int]:
    """
    Provider-agnostic driver: iterates batch_gen, saves each batch via
    record_prices_batch, calls trim_daily when done.

    batch_gen yields list[dict] of price records, or None as a cancellation
    sentinel (triggers early return with cancelled=True).
    Returns (cancelled, total_inserted).
    """
    total = 0
    async for batch in batch_gen:
        if batch is None:   # cancellation sentinel from generator
            db.commit()
            return True, total
        if batch:
            try:
                total += record_prices_batch(db, batch, trim=False)
            except Exception as e:
                log.warning("株価バッチ保存失敗: %s", e)
                db.rollback()  # aborted transaction をリセット。次バッチ・trim_daily を救済
    trim_daily(db)
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
    """全企業（sec_code 保有）の日次 OHLCV を stooq から取得して DB に保存する。
    skip_existing=True: DB の最新 trade_date から翌日以降のみ取得（差分収集）。
    backfill=True かつ skip_existing=True: 前方差分に加えて後方欠損（years_back 起点→最古レコード前日）も補完。
    プロバイダー固有ロジック（stooq 並行フェッチ）を _stooq_batch_gen に分離し、
    _price_collection_driver の共通フレームで DB 保存・trim を一元管理する。
    """

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

    # 差分収集: 企業ごとの min/max の trade_date を一括取得（ループ外で1回のみ）
    minmax_dates: dict = {}
    latest_dates: dict = {}
    if skip_existing:
        # 差分判定は全履歴を持つ weekly の min/max を基準にする（daily は直近窓のみのため）
        if backfill:
            minmax_dates = {
                row.edinet_code: (row.min_date, row.max_date)
                for row in db.query(
                    StockPriceWeekly.edinet_code,
                    sqla_func.min(StockPriceWeekly.trade_date).label("min_date"),
                    sqla_func.max(StockPriceWeekly.trade_date).label("max_date"),
                ).group_by(StockPriceWeekly.edinet_code).all()
            }
        else:
            latest_dates = dict(
                db.query(StockPriceWeekly.edinet_code, sqla_func.max(StockPriceWeekly.trade_date))
                .group_by(StockPriceWeekly.edinet_code)
                .all()
            )

    total = len(companies)
    skipped_total  = 0

    # 差分収集: スキップ判定を事前に行い、取得対象だけリストアップ
    # to_fetch: (edinet_code, sec_code, name, d1_co, d2_co) のリスト
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

        async def _fetch_hist(ec, sc, nm, d1c, d2c):
            async with sem:
                rows = await fetch_stock_history_stooq(session, sc, d1c, d2c)
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
                yield None   # cancellation sentinel → driver returns early
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
    """date_str (YYYY-MM-DD) の全銘柄 OHLCV を返す（ページネーション対応）。
    V2 API: x-api-key ヘッダーで認証。レスポンスキーは "data"、フィールドは O/H/L/C/Vo。
    400 = 非営業日またはサブスクリプション範囲外 → 空リストで正常終了。
    429 = レート制限 → 90秒待って1回だけ再試行。それでも429 なら skip。
    """
    headers = {"x-api-key": api_key}
    rows = []
    pagination_key = None
    while True:
        params: dict = {"date": date_str}
        if pagination_key:
            params["pagination_key"] = pagination_key
        r = await session.get(JQUANTS_ENDPOINT, headers=headers, params=params, timeout=30)
        if r.status_code == 429:
            # 指数バックオフは429リトライがクォータを浪費するため使わない。
            # 90秒待って1回だけ再試行（60s ウィンドウが確実にリセットされる余裕）。
            log.warning(f"J-Quants 429: {date_str} → 90秒後に再試行")
            await asyncio.sleep(90)
            r = await session.get(JQUANTS_ENDPOINT, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                log.error(f"J-Quants 429: {date_str} → 再試行も429、スキップ")
                return []
        if r.status_code in (400, 404):
            break  # 非営業日またはサブスクリプション範囲外
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("data", []))
        pagination_key = data.get("pagination_key")
        if not pagination_key:
            break
        await asyncio.sleep(JQUANTS_RATE_SLEEP)  # ページ間もレート制限を考慮
    return rows


async def _fetch_jquants_issued_shares(session: httpx.AsyncClient, api_key: str) -> dict:
    """J-Quants /markets/listed/info から全上場銘柄の発行済株式数を取得する。
    戻り値: {sec_code(4桁): issued_shares(float)} — 取得失敗時は空辞書。
    """
    headers = {"x-api-key": api_key}
    try:
        r = await session.get(JQUANTS_LISTED_INFO_ENDPOINT, headers=headers, timeout=30)
        if r.status_code == 429:
            await asyncio.sleep(90)
            r = await session.get(JQUANTS_LISTED_INFO_ENDPOINT, headers=headers, timeout=30)
        if not r.is_success:
            log.warning(f"J-Quants listed/info 取得失敗 status={r.status_code}")
            return {}
        result = {}
        for item in r.json().get("info", []):
            code = str(item.get("Code", ""))
            shares = item.get("IssuedShares")
            if code and shares is not None:
                result[code[:4]] = float(shares)
        return result
    except Exception as e:
        log.warning(f"J-Quants listed/info 例外: {e}")
        return {}


def _update_issued_shares(db, sec_to_edinet: dict, issued_shares_map: dict) -> int:
    """companies.issued_shares を J-Quants 値で更新し、
    financial_records.issued_shares が NULL の最新レコードにも補完する。
    戻り値: 更新した companies 行数。
    """
    updated = 0
    for sec_code, shares in issued_shares_map.items():
        edinet_code = sec_to_edinet.get(sec_code)
        if not edinet_code or shares <= 0:
            continue
        rows = db.query(Company).filter(Company.edinet_code == edinet_code).all()
        for co in rows:
            co.issued_shares = shares
            updated += 1

    if updated:
        db.flush()
        # 最新の financial_record で issued_shares が NULL のものを J-Quants 値で補完
        from sqlalchemy import text as _text
        db.execute(_text("""
            UPDATE financial_records fr
            SET issued_shares = c.issued_shares
            FROM companies c
            WHERE fr.edinet_code = c.edinet_code
              AND c.issued_shares IS NOT NULL
              AND fr.issued_shares IS NULL
              AND fr.year = (
                  SELECT MAX(fr2.year)
                  FROM financial_records fr2
                  WHERE fr2.edinet_code = fr.edinet_code
              )
        """))
        db.commit()
        log.info(f"J-Quants 発行済株式数: {updated}社を companies に更新")
    return updated


async def collect_stock_price_history_jquants(
    db,
    days_back: int = 14,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    on_progress: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
) -> dict:
    """J-Quants API から日次 OHLCV を日付単位で取得し ON CONFLICT UPDATE で保存する。
    J-Quants は JPX 公式データのため、stooq 由来レコードより優先して上書きする。
    1回のリクエストで全銘柄のデータが取得できるため stooq より大幅に高速。
    date_from/date_to を指定した場合はその範囲を使用し、省略時は days_back から計算する。
    プロバイダー固有ロジック（J-Quants 日付単位フェッチ）を _jquants_batch_gen に分離し、
    _price_collection_driver の共通フレームで DB 保存・trim を一元管理する。
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise ValueError("環境変数 JQUANTS_API_KEY が未設定です")

    today  = date.today()
    _from  = date_from if date_from is not None else (today - timedelta(days=days_back))
    _to    = date_to   if date_to   is not None else today
    span   = (_to - _from).days + 1
    # 土日は J-Quants も空を返すのでスキップして API コール数を削減
    dates = [
        (_from + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(span)
        if (_from + timedelta(days=i)).weekday() < 5
        and (_from + timedelta(days=i)) <= _to
    ]

    # sec_code (4桁) → edinet_code のルックアップ（全社一括で1回のみ）
    sec_to_edinet: dict = {
        row.sec_code: row.edinet_code
        for row in db.query(Company.sec_code, Company.edinet_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    }

    total = len(dates)

    async def _jquants_batch_gen(session):
        completed = 0
        last_req_time: float = 0.0
        for date_str in dates:
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(completed, total, f"[停止] ユーザーによる停止（{completed}/{total}日処理済み）")
                db.commit()
                yield None   # cancellation sentinel → driver returns early
                return

            # リクエスト開始間隔を最低 JQUANTS_RATE_SLEEP 秒に保つ（高速な祝日レスポンス後も適用）
            if completed > 0:
                elapsed = asyncio.get_event_loop().time() - last_req_time
                wait = JQUANTS_RATE_SLEEP - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)

            last_req_time = asyncio.get_event_loop().time()
            quote_rows = await _jquants_fetch_date(session, api_key, date_str)
            completed += 1

            if not quote_rows:
                if on_progress:
                    on_progress(completed, total, f"[{completed}/{total}] {date_str} スキップ（非営業日）")
                yield []
                continue

            records = []
            for q in quote_rows:
                code        = str(q.get("Code", ""))
                sec_code    = code[:4]   # J-Quants は "13010"（5桁）→ 先頭4桁が証券コード
                edinet_code = sec_to_edinet.get(sec_code)
                if not edinet_code:
                    continue
                close_val = q.get("C")   # V2: C (unadjusted close)
                if close_val is None:
                    continue   # close は nullable=False のためスキップ
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

            # 同一 edinet_code に複数の J-Quants コードが対応する場合（優先株等）に
            # ON CONFLICT DO UPDATE の CardinalityViolation を防ぐため重複排除
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
        # 価格収集と同じセッションで発行済株式数も取得（API キー共用）
        issued_shares_map = await _fetch_jquants_issued_shares(session, api_key)

    if issued_shares_map:
        _update_issued_shares(db, sec_to_edinet, issued_shares_map)

    if cancelled:
        return {"cancelled": True, "upserted": upserted_total}
    if on_progress:
        on_progress(total, total, f"[完了] {total}日処理・{upserted_total}件追加/更新")
    return {"cancelled": False, "upserted": upserted_total, "days": total}


def _update_market_data_latest(db) -> int:
    """point_in_time=False: 各社の最新レコードのみ、最新株価（daily優先）で更新する。"""

    subq = (
        db.query(
            StockPriceDaily.edinet_code,
            sqla_func.max(StockPriceDaily.trade_date).label("max_date"),
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
        if updated % PRICE_COMMIT_BATCH == 0:
            db.commit()

    db.commit()
    log.info(f"update_market_data_from_history: {updated}社を更新")
    return updated


def _update_market_data_point_in_time(db) -> int:
    """point_in_time=True: 全財務レコードを period_end 近傍の週次株価で更新し、
    最新レコードは現在株価で上書きする。"""

    # ── point_in_time=True: 全レコードを period_end 近傍の株価で更新 ─────────
    # financial_records を先にロードし、対象会社×日付範囲でフィルタした
    # weekly 行だけを取得する（全件メモリ展開を回避）。
    all_records = db.query(FinancialRecord).all()

    # 最新レコード（year最大）を社別にインデックス（最後の上書きステップで使用）
    latest_by_ec: dict = {}
    for rec in all_records:
        ec = rec.edinet_code
        if ec not in latest_by_ec or (rec.year or 0) > (latest_by_ec[ec].year or 0):
            latest_by_ec[ec] = rec

    # period_end を持つレコードのみが近傍探索の対象
    dated_records = [r for r in all_records if r.period_end]
    if not dated_records:
        log.info("update_market_data_from_history(point_in_time): period_end ありレコードが空のためスキップ")
        # 最新レコードへの現在株価上書きだけ実施（期間探索なし）
        for ec, info in latest_prices(db, list(latest_by_ec.keys())).items():
            latest_price = info.get("price")
            if not latest_price or latest_price <= 0:
                continue
            _apply_price_to_record(latest_by_ec[ec], latest_price)
        db.commit()
        return 0

    # 対象会社・日付範囲を算出して weekly を必要範囲だけロード
    # ec_subq は Query をそのまま渡す（SQLAlchemy が SELECT サブクエリへ変換）
    period_ends = [r.period_end for r in dated_records]
    date_lo = (min(period_ends) - timedelta(days=MAX_GAP_DAYS)).isoformat()
    date_hi = (max(period_ends) + timedelta(days=MAX_GAP_DAYS)).isoformat()
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

    # {edinet_code: sorted list of (trade_date_str, close)}
    history: dict = defaultdict(list)
    for ec, td, cl in weekly_rows:
        history[ec].append((td, cl))
    for ec in history:
        history[ec].sort()  # trade_date の昇順

    updated = 0
    for rec in dated_records:
        prices = history.get(rec.edinet_code)
        if not prices:
            continue

        try:
            target = rec.period_end
        except (ValueError, TypeError):
            continue

        dates = [p[0] for p in prices]
        price_dict = dict(prices)
        best_price = _nearest_price(dates, price_dict, target.isoformat(), MAX_GAP_DAYS)

        if best_price is None:
            continue

        _apply_price_to_record(rec, best_price)
        updated += 1
        if updated % PRICE_COMMIT_BATCH == 0:
            db.commit()

    # 最新レコードは現在株価で上書き（スクリーニング用）。daily（直近窓）を優先し
    # 無ければ weekly にフォールバックする latest_prices で最新終値を引く。
    for ec, info in latest_prices(db, list(latest_by_ec.keys())).items():
        latest_price = info.get("price")
        if not latest_price or latest_price <= 0:
            continue
        _apply_price_to_record(latest_by_ec[ec], latest_price)

    db.commit()
    log.info(f"update_market_data_from_history(point_in_time): {updated}レコードを更新")
    return updated


def update_market_data_from_history(db, point_in_time: bool = False) -> int:
    """stock_price_history の終値を financial_records.stock_price に反映する。
    stooq が GitHub Actions IP でブロックされる問題を回避するため、
    J-Quants 由来の stock_price_history を使ってバリュエーション指標を計算する。

    point_in_time=False（デフォルト・日次差分向け）:
        各社の最新レコードのみ、最新株価で更新する。高速。
    point_in_time=True（全件収集 finalize 向け）:
        全財務レコードを period_end 最近傍の株価で更新する。
        J-Quants カバレッジ外（データなし）のレコードはスキップし既存値を保持する。
        最新レコードは常に最新株価で上書きする。

    戻り値: 更新した財務レコード数
    """
    if not point_in_time:
        return _update_market_data_latest(db)
    return _update_market_data_point_in_time(db)


async def backfill_historical_stock_prices_yahoo(
    db,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    """J-Quants カバー範囲外（JQUANTS_BACKFILL_DAYS 日より前）の financial_records で
    stock_price が NULL のレコードに対し、Yahoo Finance から period_end 近傍の
    株価を取得して financial_records.stock_price を直接更新する。

    stock_price_history には書き込まない（Supabase 500MB ストレージ節約のため）。
    J-Quants 由来の既存 stock_price は上書きしない（NULL のみ補完）。
    GitHub Actions（Azure IP）から動作する。
    """
    cutoff = date.today() - timedelta(days=JQUANTS_BACKFILL_DAYS)

    # 対象: stock_price が NULL かつ period_end が J-Quants カバー外
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

    # edinet_code → sec_code マッピング
    sec_map = {
        c.edinet_code: c.sec_code
        for c in db.query(Company.edinet_code, Company.sec_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    }

    # 企業ごとにグループ化（1企業=1 Yahoo リクエストで複数 period_end をカバー）
    by_company: dict = defaultdict(list)
    for rec in target_records:
        sec_code = sec_map.get(rec.edinet_code)
        if sec_code and sec_code.strip():
            by_company[sec_code].append(rec)

    total   = len(by_company)
    updated = 0

    async with httpx.AsyncClient() as session:
        for i, (sec_code, recs) in enumerate(sorted(by_company.items()), 1):
            if cancel_check and cancel_check():
                db.commit()
                if on_progress:
                    on_progress(i - 1, total, f"[Yahoo backfill] 停止（{updated}件更新済み）")
                return updated

            # この企業の全 period_end をカバーする日付範囲（±MAX_GAP_DAYS の余裕を持たせる）
            period_ends = sorted(r.period_end for r in recs)
            d_from = (period_ends[0]  - timedelta(days=MAX_GAP_DAYS)).strftime("%Y%m%d")
            d_to   = (period_ends[-1] + timedelta(days=MAX_GAP_DAYS)).strftime("%Y%m%d")

            # Yahoo Finance ティッカー（東証: {sec_code}.T）
            ticker = f"{sec_code}.T"
            rows = await fetch_yahoo_history(session, ticker, d_from, d_to)

            if rows:
                # {trade_date_str: close} の辞書
                price_dict = {r["trade_date"]: r["close"] for r in rows if r["close"]}
                price_dates = sorted(price_dict.keys())

                for rec in recs:
                    target_str = rec.period_end.isoformat() if rec.period_end else ""
                    best_price = _nearest_price(price_dates, price_dict, target_str, MAX_GAP_DAYS)
                    if best_price and best_price > 0:
                        _apply_price_to_record(rec, best_price)
                        updated += 1

                if updated % PRICE_COMMIT_BATCH == 0:
                    db.commit()

            if on_progress and (i % YAHOO_BACKFILL_PROGRESS_BATCH == 0 or i == total):
                on_progress(i, total, f"[Yahoo backfill {i}/{total}] {sec_code}  累計{updated}件更新")

            # レート制限対策（fetch_yahoo_history 内の処理時間は含まれるため短めのスリープ）
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
    """stock_price_history の最新日が gap_days 日以上古い場合に、
    Yahoo Finance から不足期間を補完して stock_price_history に追記する。
    差分収集（incremental）後のフォールバックとして使用。
    J-Quants データが存在する行は上書きしない（ON CONFLICT DO NOTHING）。
    プロバイダー固有ロジック（Yahoo 逐次フェッチ）を _yahoo_batch_gen に分離し、
    _price_collection_driver の共通フレームで DB 保存・trim を一元管理する。
    """

    # 最新日は直近窓の daily を基準にする（無ければ weekly にフォールバック）
    latest_row = (db.query(sqla_func.max(StockPriceDaily.trade_date)).scalar()
                  or db.query(sqla_func.max(StockPriceWeekly.trade_date)).scalar())
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

    async def _yahoo_batch_gen(session):
        for i, (sec_code, edinet_code) in enumerate(companies, 1):
            rows = await fetch_yahoo_history(session, f"{sec_code}.T", d_from, d_to)
            # ギャップ補完は最新日より後の新規日付が対象のため衝突は稀。
            records = [
                {"edinet_code": edinet_code, "trade_date": r["trade_date"],
                 "close": r["close"], "volume": r.get("volume")}
                for r in rows if r["close"]
            ] if rows else []
            if on_progress and i % PROGRESS_REPORT_BATCH == 0:
                on_progress(i, total, f"[Yahoo gap-fill {i}/{total}]")
            yield records
            await asyncio.sleep(YAHOO_STOCK_RATE_SLEEP)

    async with httpx.AsyncClient() as session:
        _, upserted = await _price_collection_driver(db, _yahoo_batch_gen(session))

    log.info(f"fill_recent_stock_price_gap_yahoo: {upserted}件を株価テーブルへ集約保存")
    return {"skipped": False, "upserted": upserted, "from": d_from, "to": d_to}


async def backfill_weekly_history_yahoo(
    db,
    years_back: int = 5,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """stock_price_weekly を過去方向へ years_back 年まで延伸する（Yahoo Finance / #198）。

    背景: use_momentum=ON（macro_risk_return §9.8）は 52週先リターン＋12ヶ月モメンタムを
    同時に要求するため、週次株価の被覆が短いと walk-forward CV が 0 フォルドになる。
    各社の週次の最古 trade_date が today-years_back より新しい（＝過去が不足）場合に、
    不足期間を Yahoo から取得し record_prices_batch 経由で daily→weekly 再集約して埋める。

    ストレージ安全性: 1社処理ごとに record_prices_batch(trim=True) を呼び、daily を保持窓
    （DAILY_WINDOW_DAYS）以外は都度 trim する。これにより 5年×全社の daily が同時展開して
    Supabase Free 500MB を超えるのを防ぐ。weekly は古い daily から再集約済みのため情報損失なし。
    既存 weekly 行は ON CONFLICT UPDATE で同値上書き（破壊なし）。
    J-Quants カバー外の過去も Yahoo で取得でき、GitHub Actions（Azure IP）から動作する。
    """
    today     = date.today()
    floor_d   = date(today.year - years_back, today.month, today.day)
    floor_str = floor_d.isoformat()
    d_from    = floor_d.strftime("%Y%m%d")

    # 企業ごとの週次最古 trade_date（weekly 未収集の社はキー無し）
    min_week = dict(
        db.query(StockPriceWeekly.edinet_code,
                 sqla_func.min(StockPriceWeekly.trade_date))
        .group_by(StockPriceWeekly.edinet_code)
        .all()
    )

    companies = (
        db.query(Company.sec_code, Company.edinet_code)
        .filter(Company.sec_code.isnot(None), Company.sec_code != "")
        .all()
    )

    # 取得対象: weekly 未収集、または最古日が floor より新しい（過去が不足する）社のみ
    to_fetch = []
    for sec_code, edinet_code in companies:
        oldest = min_week.get(edinet_code)
        if oldest is None:
            d_to = today
        elif oldest > floor_str:
            d_to = date.fromisoformat(oldest) - timedelta(days=1)
        else:
            continue  # 既に years_back 以上カバー済み
        to_fetch.append((sec_code, edinet_code, d_to.strftime("%Y%m%d")))

    total = len(to_fetch)
    if total == 0:
        log.info(f"backfill_weekly_history_yahoo: 全社 {years_back}年以上カバー済み（対象なし）")
        return {"skipped": True, "reason": "already_covered", "companies": 0}

    upserted = 0
    async with httpx.AsyncClient() as session:
        for i, (sec_code, edinet_code, d_to) in enumerate(sorted(to_fetch), 1):
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(i - 1, total, f"[週次backfill] 停止（{upserted}件保存済み）")
                return {"cancelled": True, "upserted": upserted, "companies": i - 1}

            rows = await fetch_yahoo_history(session, f"{sec_code}.T", d_from, d_to)
            records = [
                {"edinet_code": edinet_code, "trade_date": r["trade_date"],
                 "close": r["close"], "volume": r.get("volume")}
                for r in rows if r.get("close")
            ] if rows else []
            if records:
                try:
                    # 1社ごとに trim=True：daily を都度 trim して保持窓外の過去を残さない
                    upserted += record_prices_batch(db, records, trim=True)
                except Exception as e:
                    log.warning("週次backfill バッチ保存失敗 %s: %s", sec_code, e)

            if on_progress and (i % YAHOO_BACKFILL_PROGRESS_BATCH == 0 or i == total):
                on_progress(i, total, f"[週次backfill {i}/{total}] {sec_code} 累計{upserted}件")

            if YAHOO_STOCK_RATE_SLEEP > 0:
                await asyncio.sleep(YAHOO_STOCK_RATE_SLEEP)

    log.info(f"backfill_weekly_history_yahoo: {upserted}件の daily を保存し weekly を再集約（{total}社）")
    return {"skipped": False, "upserted": upserted, "companies": total, "floor": floor_str}


def _nearest_price(sorted_dates: list, price_dict: dict, target_str: str,
                   max_gap: int) -> Optional[float]:
    """昇順の日付文字列リスト `sorted_dates` から `target_str` に最も近い日付の
    価格（`price_dict[日付]`）を返す。最近傍の日付差が `max_gap` 日を超える場合は
    None。bisect で挿入位置を求め、その前後2候補のみを比較する。

    point-in-time マッチと Yahoo backfill の最近傍探索で共用する内部ヘルパー。
    """
    try:
        target = date.fromisoformat(target_str[:10])
    except (ValueError, TypeError):
        return None

    pos = bisect.bisect_left(sorted_dates, target_str)
    best_price = None
    best_gap = max_gap + 1

    for idx in (pos - 1, pos):
        if 0 <= idx < len(sorted_dates):
            td_str = sorted_dates[idx]
            try:
                td = date.fromisoformat(td_str[:10])
            except ValueError:
                continue
            gap = abs((td - target).days)
            if gap < best_gap:
                best_gap = gap
                best_price = price_dict[td_str]

    return best_price


def _apply_price_to_record(rec, price: float) -> None:
    """財務レコードに株価・バリュエーション指標を書き込む（内部ヘルパー）"""
    rec.stock_price = price
    if rec.pl_eps and rec.pl_eps > 0:
        rec.per = round(price / rec.pl_eps, 2)
    if rec.bs_bps and rec.bs_bps > 0:
        rec.pbr = round(price / rec.bs_bps, 2)
    _sh = (float(rec.issued_shares) if (rec.issued_shares and rec.issued_shares > 0)
           else ((rec.bs_total_equity / rec.bs_bps)
                 if (rec.bs_bps and rec.bs_bps > 0
                     and rec.bs_total_equity and rec.bs_total_equity > 0)
                 else None))
    if _sh:
        rec.market_cap = round(price * _sh / 1_000_000, 2)
    if rec.dps and rec.dps > 0 and price > 0:
        rec.div_yield = round(rec.dps / price * 100, 2)


def _fetch_latest_fin_by_ec(db, edinet_codes: list) -> dict:
    """各社の最新 FinancialRecord を1クエリで取得して {edinet_code: record} を返す。

    ROW_NUMBER() OVER (PARTITION BY edinet_code ORDER BY year DESC, period_end DESC)
    で最新行を確定するため、同一 year・複数 period_end が存在しても安全。
    N+1 クエリの代替として update_market_data 系関数で使用。
    """
    if not edinet_codes:
        return {}
    rn = sqla_func.row_number().over(
        partition_by=FinancialRecord.edinet_code,
        order_by=[FinancialRecord.year.desc(), FinancialRecord.period_end.desc()],
    ).label("rn")
    subq = (
        db.query(FinancialRecord.id, rn)
        .filter(FinancialRecord.edinet_code.in_(edinet_codes))
        .subquery()
    )
    return {
        r.edinet_code: r
        for r in db.query(FinancialRecord)
        .join(subq, (FinancialRecord.id == subq.c.id) & (subq.c.rn == 1))
        .all()
    }


async def update_market_data(db,
                             max_companies: Optional[int] = None,
                             on_progress: Optional[Callable] = None,
                             cancel_check: Optional[Callable] = None):
    """
    全企業の最新財務レコードに株価・バリュエーション指標を書き込む。
    株価: stooq API
    time市価総額: stock_price × (bs_total_equity / bs_bps)
    PER: stock_price / pl_eps
    PBR: stock_price / bs_bps
    """
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
            if updated % MARKET_COMMIT_BATCH == 0:
                db.commit()
                log.info(f"市場データ更新中: {updated}社完了")

    db.commit()
    log.info(f"市場データ更新完了: {updated}/{total}社")
    return False


# ── マクロデータ（為替・金利・指数・コモディティ）─────────────────────────

# stooq ティッカー定義。category は 'fx' / 'rate' / 'equity' / 'commodity' / 'volatility'。
# 本番収集は GitHub Actions（Azure IP）上で Yahoo Finance を優先する（stooq は 403 ブロック）。
# VIX/DXY/US5Y/US30Y は #218 フェーズ1 で追加。Yahoo のみで取得するため stooq ticker は
# best-effort（空文字は stooq フォールバック時に「データ無し」で skip され安全）。これらが
# macro_data に実際に蓄積されたことを Actions で実証してから M-1 の特徴量（_MACRO_MAP）へ公開する。
MACRO_SERIES: list[dict] = [
    {"code": "USDJPY",    "name": "USD/JPY",      "category": "fx",         "ticker": "usdjpy",   "yf_ticker": "USDJPY=X"},
    {"code": "EURJPY",    "name": "EUR/JPY",      "category": "fx",         "ticker": "eurjpy",   "yf_ticker": "EURJPY=X"},
    {"code": "DXY",       "name": "ドル指数",     "category": "fx",         "ticker": "",         "yf_ticker": "DX-Y.NYB"},
    {"code": "US5Y",      "name": "米5年金利",    "category": "rate",       "ticker": "",         "yf_ticker": "^FVX"},
    {"code": "US10Y",     "name": "米10年金利",   "category": "rate",       "ticker": "10usy.b",  "yf_ticker": "^TNX"},
    {"code": "US30Y",     "name": "米30年金利",   "category": "rate",       "ticker": "",         "yf_ticker": "^TYX"},
    {"code": "JP10Y",     "name": "日10年金利",   "category": "rate",       "ticker": "10jpy.b",  "yf_ticker": "^JGB"},
    {"code": "NIKKEI225", "name": "日経225",      "category": "equity",     "ticker": "^nkx",     "yf_ticker": "^N225"},
    # TOPIX 指数 ^TPX は Yahoo で配信停止（200 OK だが 0 件）。TOPIX 連動 ETF 1306.T
    # （NEXT FUNDS TOPIX・最長履歴・高流動）を代理に使う＝yoy/logret/zscore は指数と同等に追従。
    {"code": "TOPIX",     "name": "TOPIX",        "category": "equity",     "ticker": "^tpx",     "yf_ticker": "1306.T"},
    {"code": "SP500",     "name": "S&P500",       "category": "equity",     "ticker": "^spx",     "yf_ticker": "^GSPC"},
    {"code": "VIX",       "name": "VIX恐怖指数",  "category": "volatility", "ticker": "",         "yf_ticker": "^VIX"},
    {"code": "WTI",       "name": "WTI原油",      "category": "commodity",  "ticker": "cl.f",     "yf_ticker": "CL=F"},
    {"code": "GOLD",      "name": "金",           "category": "commodity",  "ticker": "gc.f",     "yf_ticker": "GC=F"},
]

# ── FRED マクロ系列（クレジット・インフレ・JP金利・期間構造）──────────────────────────
# FRED_API_KEY が設定されている場合のみ収集。未設定時は collect_macro_data 内でスキップ。
# アカウント登録: https://fred.stlouisfed.org/docs/api/api_key.html （無料・要ユーザー登録）
# GitHub Actions シークレット名: FRED_API_KEY
# レート制限: 120 req/min → FRED_RATE_SLEEP=0.6s でバッファ込み
FRED_API_KEY  = os.getenv("FRED_API_KEY", "")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_RATE_SLEEP = 0.6  # 120 req/min = 2/s → 0.6s でバッファ付き

FRED_SERIES: list[dict] = [
    {"code": "HY_OAS",       "name": "米HYスプレッド（OAS）",     "category": "credit",    "fred_id": "BAMLH0A0HYM2"},
    {"code": "IG_OAS",       "name": "米IGスプレッド（OAS）",     "category": "credit",    "fred_id": "BAMLC0A0CM"},
    {"code": "BREAKEVEN10Y", "name": "米10年BEI（インフレ期待）", "category": "inflation", "fred_id": "T10YIE"},
    {"code": "JP10Y_FRED",   "name": "日10年金利（FRED）",        "category": "rate",      "fred_id": "IRLTLT01JPM156N"},
    {"code": "T10Y2Y",       "name": "米10y−2yスプレッド",       "category": "rate",      "fred_id": "T10Y2Y"},
    # ── 日本 実体経済指標（#250・日本マクロのリバランス）─────────────────────────
    # FRED は観測値を「期の参照開始日」の日付で返す。実体経済指標は公表ラグが大きい
    # （GDP=期末から約1.5〜2か月、月次指標=約1か月）ため、lag_days 分だけ trade_date を
    # 後ろへシフトして「この日には知れた値」へ正規化する＝先読みバイアス（look-ahead）防止。
    # lag_days 未指定の既存5系列は 0=シフト無し（完全後方互換）。
    # 採用前に各 fred_id の最終更新日を確認すること（OECD 旧系列は凍結あり：CPALTT01JPM657N 等）。
    {"code": "JP_REAL_GDP",  "name": "日本 実質GDP",        "category": "real_economy", "fred_id": "JPNRGDPEXP",      "freq": "quarterly", "lag_days": 135},
    {"code": "JP_UNEMP",     "name": "日本 失業率",         "category": "labor",        "fred_id": "LRUNTTTTJPM156S", "freq": "monthly",   "lag_days": 60},
    # JP_IP (JPNPROINDMISMEI) は 2024-04-30 で凍結確認済み (#253)。e-Stat コネクタ実装まで除外。
    {"code": "JP_TRADE_BAL", "name": "日本 貿易収支",       "category": "trade",        "fred_id": "XTNTVA01JPQ664S", "freq": "quarterly", "lag_days": 135},
]
# FRED 低頻度系列の履歴確保（四半期 zscore は ≥20 点必要・[macro_snapshots]_macro_from_cache）。
# 市場系（years_back）より長く遡って観測点を担保する。
FRED_MIN_YEARS_BACK = 10

# ── 日銀 時系列統計 API（stat-search.boj.or.jp/api/v1）─────────────────────
# 認証不要・JSON。ADR-0006 §Decision-2。
# 注: ADR は api.boj.or.jp と記したが実エンドポイントは stat-search.boj.or.jp/api/v1
#   （2026-02 新 API 発表後も stat-search が正式エンドポイント）→ GOTCHAS.md に記載済み。
BOJ_BASE_URL  = "https://www.stat-search.boj.or.jp/api/v1"
BOJ_RATE_SLEEP = 0.5  # 同一 DB への連続リクエストに備えたバッファ

# freq="monthly" → SURVEY_DATES は YYYYMM（e.g. 202501）
# freq="quarterly" → SURVEY_DATES は YYYYQQ（01=Q1/4月, 02=Q2/7月, 03=Q3/10月, 04=Q4/翌1月）
BOJ_SERIES: list[dict] = [
    {
        "code": "JP_M2",
        "name": "日本 M2（マネーストック）",
        "category": "money",
        "db": "MD02",
        "boj_code": "MAM1NAM2M2MO",
        "freq": "monthly",
        "lag_days": 21,
    },
    {
        "code": "JP_TANKAN_MFG_LARGE",
        "name": "短観 製造業大企業 業況DI",
        "category": "survey",
        "db": "CO",
        "boj_code": "TK99F1000601GCQ01000",
        "freq": "quarterly",
        "lag_days": 14,
    },
    {
        "code": "JP_TANKAN_NONMFG_LARGE",
        "name": "短観 非製造業大企業 業況DI",
        "category": "survey",
        "db": "CO",
        "boj_code": "TK99F2000601GCQ01000",
        "freq": "quarterly",
        "lag_days": 14,
    },
    {
        "code": "JP_TANKAN_MFG_SMALL",
        "name": "短観 製造業中小企業 業況DI",
        "category": "survey",
        "db": "CO",
        "boj_code": "TK99F1000601GCQ03000",
        "freq": "quarterly",
        "lag_days": 14,
    },
    {
        "code": "JP_TANKAN_NONMFG_SMALL",
        "name": "短観 非製造業中小企業 業況DI",
        "category": "survey",
        "db": "CO",
        "boj_code": "TK99F2000601GCQ03000",
        "freq": "quarterly",
        "lag_days": 14,
    },
    {
        "code": "JP_CGPI",
        "name": "日本 企業物価指数（国内・総平均）",
        "category": "price",
        "db": "PR01",
        "boj_code": "PRCG20_2200000000",
        "freq": "monthly",
        "lag_days": 30,
    },
    {
        "code": "JP_MONETARY_BASE",
        "name": "日本 マネタリーベース平均残高",
        "category": "money",
        "db": "MD01",
        "boj_code": "MABS1AN11",
        "freq": "monthly",
        "lag_days": 14,
    },
]

# ── e-Stat API（CPI）──────────────────────────────────────────────────────────
# ESTAT_API_KEY が設定されている場合のみ収集（FRED_API_KEY と同挙動）。
# アカウント登録: https://www.e-stat.go.jp/api/ （無料・要ユーザー登録）
# GitHub Actions シークレット名: ESTAT_API_KEY
# statsDataId=0003427113: 2020年基準消費者物価指数（月次〜1970年・年次集計が同一テーブルに混在）
#   cdCat01=0001: 総合, 0161: 生鮮食品を除く総合（非季調）
#   cdArea=00000: 全国, 13A01: 東京都区部（表示名は "13100 東京都区部" だが実際の @code は 13A01。
#     旧来の "13100" を cdArea に指定すると STATUS=1「該当データなし」で0件になる・#262 で実API確認）
#   cdTab=1: 表章項目「指数」＋lvTime=4: 時間軸レベル「月次」（#262）。
#     両方指定しないと表章項目・時間軸レベルが絞られず年次系列のみ返却される。
ESTAT_API_KEY  = os.getenv("ESTAT_API_KEY", "")
ESTAT_BASE_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"

ESTAT_SERIES: list[dict] = [
    {
        "code": "JP_CPI_TOTAL",
        "name": "日本 CPI 全国総合",
        "category": "price",
        "stats_data_id": "0003427113",
        "cd_tab": "1",
        "cd_cat01": "0001",
        "cd_area": "00000",
        "lag_days": 30,
    },
    {
        "code": "JP_CPI_CORE",
        "name": "日本 CPI 全国コア（生鮮除く）",
        "category": "price",
        "stats_data_id": "0003427113",
        "cd_tab": "1",
        "cd_cat01": "0161",
        "cd_area": "00000",
        "lag_days": 30,
    },
    {
        "code": "JP_CPI_TOKYO",
        "name": "日本 CPI 東京都区部総合",
        "category": "price",
        "stats_data_id": "0003427113",
        "cd_tab": "1",
        "cd_cat01": "0001",
        "cd_area": "13A01",
        "lag_days": 30,
    },
]

# ── e-Stat API（鉱工業指数・在庫指数）───────────────────────────────────────
# ESTAT_API_KEY を共用（CPI と同じキー・同じスキップ挙動）。CPI（ESTAT_SERIES）とは
# @time のフォーマットが異なる: CPI は "YYYY0000MM" の自己記述コードで直接パース可能だが、
# 鉱工業指数は "0500100" のような連番コードで年月を直接表現しない。そのため cd_tab/cd_area
# は存在せず（表章項目・地域軸を持たないテーブル）、fetch_estat_index_series が
# metaGetFlg="Y" でメタ情報（time 軸 code→"YYYYMM"）を同一レスポンスに同梱取得して変換する。
# 統計表は「経済産業省 鉱工業指数」2020年基準・業種別・季節調整済指数【月次】（2018年1月～）。
# 鉱工業指数は基準改定（2010→2015→2020年基準）のたびに statsDataId が別テーブルへ切り替わり
# 旧テーブルは更新停止する（FRED 版 JPNPROINDMISMEI が2024-04-30凍結した根本原因と同型・#253）。
# 次回基準改定時（目安10年ごと）は本節の statsDataId を再調査すること。
# cd_cat01="0001000" は業種分類（cat01）の「鉱工業総合」（"0002000"=製造工業も選択可）。
ESTAT_INDEX_SERIES: list[dict] = [
    {
        "code": "JP_IIP",
        "name": "日本 鉱工業生産指数（季調済・鉱工業総合）",
        "category": "real_economy",
        "stats_data_id": "0004052177",
        "cd_cat01": "0001000",
        "lag_days": 60,
    },
    {
        "code": "JP_IIP_INVENTORY",
        "name": "日本 鉱工業在庫指数（季調済・鉱工業総合）",
        "category": "real_economy",
        "stats_data_id": "0004052179",
        "cd_cat01": "0001000",
        "lag_days": 60,
    },
]


async def fetch_fred_series(
    session: httpx.AsyncClient,
    fred_id: str,
    date_from: str,  # "YYYY-MM-DD"
    date_to:   str,  # "YYYY-MM-DD"
    lag_days: int = 0,
) -> list:
    """FRED API から指定系列の観測値を取得する（日次・月次両対応）。
    欠損値（"."）と None はスキップ。月次系列は FRED が1か月1観測を返すので結果も月次になる。
    lag_days > 0 のとき、observation の日付（期の参照開始日）を lag_days 分だけ後ろへ
    シフトして trade_date とする＝公表ラグ補正（実体経済指標の先読みバイアス防止）。"""
    params = {
        "series_id":         fred_id,
        "api_key":           FRED_API_KEY,
        "file_type":         "json",
        "observation_start": date_from,
        "observation_end":   date_to,
    }
    try:
        r = await session.get(FRED_BASE_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        # URL にクエリで api_key を渡しているため e をそのまま出すと鍵が漏洩する
        log.warning("FRED 取得失敗 %s: HTTP %s", fred_id, e.response.status_code)
        return []
    except Exception as e:
        log.warning("FRED 取得失敗 %s: %s", fred_id, type(e).__name__)
        return []

    rows = []
    for obs in data.get("observations", []):
        v = obs.get("value", ".")
        if v == "." or v is None:
            continue
        try:
            obs_date = obs["date"]
            if lag_days:
                obs_date = (date.fromisoformat(obs_date) + timedelta(days=lag_days)).isoformat()
            rows.append({
                "trade_date": obs_date,
                "open":   None,
                "high":   None,
                "low":    None,
                "close":  float(v),
                "volume": None,
            })
        except (ValueError, KeyError):
            continue
    return rows


async def fetch_boj_series(
    session: httpx.AsyncClient,
    db: str,
    boj_code: str,
    date_from: str,  # "YYYYMM"
    date_to:   str,  # "YYYYMM"
    lag_days: int = 0,
    freq: str = "monthly",
) -> list:
    """日銀時系列統計 API（stat-search.boj.or.jp/api/v1/getDataCode）から観測値を取得する。
    monthly: SURVEY_DATES は YYYYMM。quarterly: SURVEY_DATES は YYYYQQ（01-04=Q1-Q4）。
    四半期 Q1=4月公表, Q2=7月公表, Q3=10月公表, Q4=翌年1月公表 として calendar date へ変換後
    lag_days 分だけ後ろへシフトして trade_date とする。
    quarterly 系列の startDate/endDate は YYYYQQ 形式に変換して送信（YYYYMM だと 400）。"""
    _Q_RELEASE_MONTH = {1: 4, 2: 7, 3: 10, 4: 1}

    if freq == "quarterly":
        def _yyyymm_to_boj_quarter(yyyymm: str) -> str:
            year, month = int(yyyymm[:4]), int(yyyymm[4:])
            if month <= 3:   return f"{year - 1}04"  # Jan-Mar → Q4 of prev year
            elif month <= 6: return f"{year}01"       # Apr-Jun → Q1
            elif month <= 9: return f"{year}02"       # Jul-Sep → Q2
            else:            return f"{year}03"        # Oct-Dec → Q3
        date_from = _yyyymm_to_boj_quarter(date_from)
        date_to   = _yyyymm_to_boj_quarter(date_to)

    params = {
        "format":    "json",
        "db":        db,
        "startDate": date_from,
        "endDate":   date_to,
        "code":      boj_code,
    }
    try:
        r = await session.get(f"{BOJ_BASE_URL}/getDataCode", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("BOJ 取得失敗 %s/%s: %s", db, boj_code, type(e).__name__)
        return []

    if data.get("STATUS") != 200:
        log.warning("BOJ 取得失敗 %s/%s: STATUS=%s", db, boj_code, data.get("STATUS"))
        return []

    rows = []
    for series in data.get("RESULTSET", []):
        vdata = series.get("VALUES", {})
        survey_dates = vdata.get("SURVEY_DATES", [])
        values       = vdata.get("VALUES", [])
        for sd, v in zip(survey_dates, values):
            if v is None:
                continue
            sd_str = str(sd)
            if freq == "quarterly":
                year    = int(sd_str[:4])
                quarter = int(sd_str[4:])
                month   = _Q_RELEASE_MONTH[quarter]
                if quarter == 4:
                    year += 1
                obs_date = date(year, month, 1).isoformat()
            else:
                year     = int(sd_str[:4])
                month    = int(sd_str[4:])
                obs_date = date(year, month, 1).isoformat()
            if lag_days:
                obs_date = (date.fromisoformat(obs_date) + timedelta(days=lag_days)).isoformat()
            rows.append({
                "trade_date": obs_date,
                "open": None, "high": None, "low": None,
                "close": float(v), "volume": None,
            })
    return rows


async def fetch_estat_series(
    session: httpx.AsyncClient,
    stats_data_id: str,
    cd_tab: str,
    cd_cat01: str,
    cd_area: str,
    date_from: str,  # "YYYYMM000000"
    date_to:   str,  # "YYYYMM000000"
    lag_days: int = 0,
) -> list:
    """e-Stat API（api.e-stat.go.jp）から月次統計を取得する。ESTAT_API_KEY が必要。
    @time 実測フォーマットは月次 "YYYY" + "00" + "MM" + "MM"（月を2回繰り返す。例 2024年12月＝
    "2024001212"）・年度（会計年度集計）は "YYYY" + "10" + "0000"。年度行の先頭6文字が偶然
    "YYYY10" になり月=10と誤読される事故があった（#256）ため、月は末尾2文字から取り出す。
    cdTab（表章項目=1:指数）と lvTime（時間軸レベル=4:月次）の両方が必須（#262 で実 API 検証済み。
    片方だけでは年次行が混入するか解析失敗になる。過去の lvTime 単体試行が失敗したのは cdTab
    未指定のままだったため）。"""
    params = {
        "appId":        ESTAT_API_KEY,
        "statsDataId":  stats_data_id,
        "cdTab":        cd_tab,
        "cdCat01":      cd_cat01,
        "cdArea":       cd_area,
        "cdTimeFrom":   date_from,
        "cdTimeTo":     date_to,
        "lvTime":       "4",
        "lang":         "J",
        "metaGetFlg":   "N",
    }
    try:
        r = await session.get(ESTAT_BASE_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("e-Stat 取得失敗 %s/%s: %s", stats_data_id, cd_cat01, type(e).__name__)
        return []

    try:
        values = (
            data["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
        )
    except (KeyError, TypeError):
        log.warning("e-Stat レスポンス解析失敗 %s/%s", stats_data_id, cd_cat01)
        return []

    if isinstance(values, dict):
        values = [values]

    rows = []
    for val in values:
        raw_v = val.get("$")
        t     = val.get("@time", "")
        if raw_v is None or t == "":
            continue
        # @cat01/@area が VALUE 要素に存在する場合のみフィルタ（属性なし = API 側で既に絞込済み）。
        # 属性なしのとき None != cd_cat01 → 全行スキップになるため "in val" チェックが必須。
        if cd_cat01 and "@cat01" in val and val["@cat01"] != cd_cat01:
            continue
        if cd_area and "@area" in val and val["@area"] != cd_area:
            continue
        try:
            # "YYYY" + "00" + "MM" + "MM"（月次・月が2回繰り返される）。年は先頭4文字、
            # 月は末尾2文字（[6:8] と同値）から取り出す。[4:6] は月ではなく年次/月次の
            # 区分マーカー（月次="00"・年度="10"）なので月として読んではいけない。
            year   = int(t[:4])
            month  = int(t[8:10])
            obs_date = date(year, month, 1).isoformat()
            if lag_days:
                obs_date = (date.fromisoformat(obs_date) + timedelta(days=lag_days)).isoformat()
            rows.append({
                "trade_date": obs_date,
                "open": None, "high": None, "low": None,
                "close": float(raw_v), "volume": None,
            })
        except (ValueError, IndexError):
            continue
    # 同 trade_date が複数行ある場合（API が同一時点を複数カテゴリで返す等）は最後の値で dedup。
    seen: dict = {}
    for r in rows:
        seen[r["trade_date"]] = r
    return list(seen.values())


async def fetch_estat_index_series(
    session: httpx.AsyncClient,
    stats_data_id: str,
    cd_cat01: str,
    lag_days: int = 0,
) -> list:
    """e-Stat API から「time 軸が連番コード」形式の指数系列（鉱工業指数等）を取得する。
    CPI 系列（fetch_estat_series）は @time が "YYYY0000MM" の自己記述コードで直接パース
    できるが、鉱工業指数は @time が "0500100" のような連番コードで年月を直接表現しない。
    metaGetFlg="Y" を付けて time 軸のメタ情報（code→"YYYYMM"）を同一レスポンスへ同梱させ、
    そのマッピングで変換する（追加の getMetaInfo 呼び出し不要・1リクエストで完結）。
    ウエイト行等（"付加生産ウエイト" 等・6桁の YYYYMM にならない）はスキップする。"""
    params = {
        "appId":       ESTAT_API_KEY,
        "statsDataId": stats_data_id,
        "cdCat01":     cd_cat01,
        "metaGetFlg":  "Y",
        "lang":        "J",
    }
    try:
        r = await session.get(ESTAT_BASE_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("e-Stat(index) 取得失敗 %s/%s: %s", stats_data_id, cd_cat01, type(e).__name__)
        return []

    try:
        stat       = data["GET_STATS_DATA"]["STATISTICAL_DATA"]
        values     = stat["DATA_INF"]["VALUE"]
        class_objs = stat["CLASS_INF"]["CLASS_OBJ"]
    except (KeyError, TypeError):
        log.warning("e-Stat(index) レスポンス解析失敗 %s/%s", stats_data_id, cd_cat01)
        return []

    if isinstance(values, dict):
        values = [values]
    if isinstance(class_objs, dict):
        class_objs = [class_objs]

    time_map: dict = {}
    for obj in class_objs:
        if obj.get("@id") != "time":
            continue
        classes = obj.get("CLASS", [])
        if isinstance(classes, dict):
            classes = [classes]
        for c in classes:
            time_map[c.get("@code")] = c.get("@name")

    rows = []
    for val in values:
        raw_v  = val.get("$")
        code   = val.get("@time", "")
        yyyymm = time_map.get(code, "")
        if raw_v is None or len(yyyymm) != 6 or not yyyymm.isdigit():
            continue  # ウエイト行等（time_map の名前が YYYYMM でない）はスキップ
        try:
            year, month = int(yyyymm[:4]), int(yyyymm[4:])
            obs_date = date(year, month, 1).isoformat()
            if lag_days:
                obs_date = (date.fromisoformat(obs_date) + timedelta(days=lag_days)).isoformat()
            rows.append({
                "trade_date": obs_date,
                "open": None, "high": None, "low": None,
                "close": float(raw_v), "volume": None,
            })
        except (ValueError, IndexError):
            continue
    return rows


async def fetch_yahoo_history(
    session: httpx.AsyncClient,
    yf_ticker: str,
    date_from: str,   # "YYYYMMDD"
    date_to:   str,   # "YYYYMMDD"
) -> list:
    """Yahoo Finance v8 API から日次 OHLCV を取得する。
    GitHub Actions（Azure IP）からも動作する。stooq の代替として使用。"""
    try:
        # date → Unix timestamp（JST 00:00 = UTC 前日15:00、余裕を持って+1日）
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
    date_from: str,   # "YYYYMMDD"
    date_to:   str,   # "YYYYMMDD"
) -> list:
    """stooq 日次 OHLCV（汎用ティッカー・マクロ用）。open/high/low/volume は None 許容。"""
    return await _fetch_stooq_ohlcv(
        session, ticker, date_from, date_to,
        strict=False, log_label=f"stooq マクロ取得失敗 {ticker}",
    )


async def collect_macro_data(
    db,
    years_back: int = 5,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
):
    """MACRO_SERIES（Yahoo/stooq）+ FRED_SERIES + BOJ_SERIES + ESTAT_SERIES を macro_data に upsert。
    Yahoo Finance 優先（GitHub Actions Azure IP 対応）→ stooq フォールバック。
    FRED: FRED_API_KEY 設定時のみ。BOJ: 常時収集（認証不要）。e-Stat: ESTAT_API_KEY 設定時のみ。
    既存レコードは close 等を上書き（最新値で更新）。"""
    today      = date.today()
    start      = today - timedelta(days=int(years_back * 365.25))
    d1         = start.strftime("%Y%m%d")
    d2         = today.strftime("%Y%m%d")
    # FRED は市場系より長く遡る（四半期系列の zscore に ≥20 点を確保）。
    fred_start = today - timedelta(days=int(max(years_back, FRED_MIN_YEARS_BACK) * 365.25))
    d1_iso     = fred_start.strftime("%Y-%m-%d")
    d2_iso     = today.strftime("%Y-%m-%d")
    # BOJ: 短観は四半期なので FRED と同じく長めに遡る（zscore ≥20 点確保）。
    boj_start  = today - timedelta(days=int(max(years_back, FRED_MIN_YEARS_BACK) * 365.25))
    d1_boj     = boj_start.strftime("%Y%m")   # "YYYYMM"
    d2_boj     = today.strftime("%Y%m")
    # e-Stat: @time フォーマット YYYYMM000000。
    d1_estat   = boj_start.strftime("%Y%m") + "000000"
    d2_estat   = today.strftime("%Y%m") + "000000"
    total      = (
        len(MACRO_SERIES)
        + (len(FRED_SERIES)  if FRED_API_KEY  else 0)
        + len(BOJ_SERIES)
        + (len(ESTAT_SERIES) + len(ESTAT_INDEX_SERIES) if ESTAT_API_KEY else 0)
    )
    saved      = 0

    async with httpx.AsyncClient() as session:
        for i, series in enumerate(MACRO_SERIES, 1):
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(i-1, total, "[マクロ収集] ユーザー停止")
                return saved

            # Yahoo Finance 優先（GitHub Actions Azure IP 対応）→ stooq フォールバック
            rows = await fetch_yahoo_history(session, series["yf_ticker"], d1, d2)
            src = "Yahoo Finance"
            if not rows:
                rows = await fetch_stooq_history(session, series["ticker"], d1, d2)
                src = "stooq"
            if on_progress:
                on_progress(i-1, total, f"[マクロ {i}/{total}] {series['name']} ({src}) 取得中")
            if not rows:
                if on_progress:
                    on_progress(i, total, f"[マクロ {i}/{total}] {series['name']} データ無し")
                continue

            # 系列単位のバルク upsert（(series_code, trade_date) 競合で最新値上書き）。
            # 旧実装は行ごとに INSERT/UPDATE を発行していたが N+1 解消のため 1 文に圧縮。
            vals = [{
                "series_code": series["code"],
                "series_name": series["name"],
                "category":    series["category"],
                "trade_date":  r["trade_date"],
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "volume": r["volume"],
            } for r in rows]
            n = upsert_macro_batch(db, vals)
            db.commit()
            saved += n
            if on_progress:
                on_progress(i, total, f"[マクロ {i}/{total}] {series['name']}: {n}件処理")

        # ── FRED 収集（FRED_API_KEY が設定されている場合のみ）──────────────────
        if not FRED_API_KEY:
            if on_progress:
                on_progress(len(MACRO_SERIES), total, "[FRED] FRED_API_KEY 未設定のためスキップ")
        else:
            base_i = len(MACRO_SERIES)
            for j, series in enumerate(FRED_SERIES, 1):
                idx = base_i + j
                if cancel_check and cancel_check():
                    if on_progress:
                        on_progress(idx - 1, total, "[マクロ収集] ユーザー停止")
                    return saved

                if on_progress:
                    on_progress(idx - 1, total, f"[FRED {j}/{len(FRED_SERIES)}] {series['name']} 取得中")
                rows = await fetch_fred_series(
                    session, series["fred_id"], d1_iso, d2_iso, series.get("lag_days", 0)
                )
                await asyncio.sleep(FRED_RATE_SLEEP)

                if not rows:
                    if on_progress:
                        on_progress(idx, total, f"[FRED {j}/{len(FRED_SERIES)}] {series['name']} データ無し")
                    continue

                vals = [{
                    "series_code": series["code"],
                    "series_name": series["name"],
                    "category":    series["category"],
                    "trade_date":  r["trade_date"],
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "volume": r["volume"],
                } for r in rows]
                n = upsert_macro_batch(db, vals)
                db.commit()
                saved += n
                if on_progress:
                    on_progress(idx, total, f"[FRED {j}/{len(FRED_SERIES)}] {series['name']}: {n}件処理")

        # ── 日銀 収集（認証不要・常時）──────────────────────────────────────────
        boj_base_i = len(MACRO_SERIES) + (len(FRED_SERIES) if FRED_API_KEY else 0)
        for k, series in enumerate(BOJ_SERIES, 1):
            idx = boj_base_i + k
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(idx - 1, total, "[マクロ収集] ユーザー停止")
                return saved

            if on_progress:
                on_progress(idx - 1, total, f"[BOJ {k}/{len(BOJ_SERIES)}] {series['name']} 取得中")
            rows = await fetch_boj_series(
                session,
                series["db"],
                series["boj_code"],
                d1_boj,
                d2_boj,
                series.get("lag_days", 0),
                series.get("freq", "monthly"),
            )
            await asyncio.sleep(BOJ_RATE_SLEEP)

            if not rows:
                if on_progress:
                    on_progress(idx, total, f"[BOJ {k}/{len(BOJ_SERIES)}] {series['name']} データ無し")
                continue

            vals = [{
                "series_code": series["code"],
                "series_name": series["name"],
                "category":    series["category"],
                "trade_date":  r["trade_date"],
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "volume": r["volume"],
            } for r in rows]
            n = upsert_macro_batch(db, vals)
            db.commit()
            saved += n
            if on_progress:
                on_progress(idx, total, f"[BOJ {k}/{len(BOJ_SERIES)}] {series['name']}: {n}件処理")

        # ── e-Stat 収集（ESTAT_API_KEY が設定されている場合のみ）────────────────
        if not ESTAT_API_KEY:
            if on_progress:
                on_progress(total, total, "[e-Stat] ESTAT_API_KEY 未設定のためスキップ")
        else:
            estat_base_i = boj_base_i + len(BOJ_SERIES)
            for m, series in enumerate(ESTAT_SERIES, 1):
                idx = estat_base_i + m
                if cancel_check and cancel_check():
                    if on_progress:
                        on_progress(idx - 1, total, "[マクロ収集] ユーザー停止")
                    return saved

                if on_progress:
                    on_progress(idx - 1, total, f"[e-Stat {m}/{len(ESTAT_SERIES)}] {series['name']} 取得中")
                rows = await fetch_estat_series(
                    session,
                    series["stats_data_id"],
                    series["cd_tab"],
                    series["cd_cat01"],
                    series["cd_area"],
                    d1_estat,
                    d2_estat,
                    series.get("lag_days", 0),
                )

                if not rows:
                    if on_progress:
                        on_progress(idx, total, f"[e-Stat {m}/{len(ESTAT_SERIES)}] {series['name']} データ無し")
                    continue

                vals = [{
                    "series_code": series["code"],
                    "series_name": series["name"],
                    "category":    series["category"],
                    "trade_date":  r["trade_date"],
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "volume": r["volume"],
                } for r in rows]
                n = upsert_macro_batch(db, vals)
                db.commit()
                saved += n
                if on_progress:
                    on_progress(idx, total, f"[e-Stat {m}/{len(ESTAT_SERIES)}] {series['name']}: {n}件処理")

            # ── e-Stat 鉱工業指数（time 軸が連番コード・日付範囲パラメータ無し）────
            estat_idx_base_i = estat_base_i + len(ESTAT_SERIES)
            for p, series in enumerate(ESTAT_INDEX_SERIES, 1):
                idx = estat_idx_base_i + p
                if cancel_check and cancel_check():
                    if on_progress:
                        on_progress(idx - 1, total, "[マクロ収集] ユーザー停止")
                    return saved

                if on_progress:
                    on_progress(idx - 1, total, f"[e-Stat-idx {p}/{len(ESTAT_INDEX_SERIES)}] {series['name']} 取得中")
                rows = await fetch_estat_index_series(
                    session,
                    series["stats_data_id"],
                    series["cd_cat01"],
                    series.get("lag_days", 0),
                )

                if not rows:
                    if on_progress:
                        on_progress(idx, total, f"[e-Stat-idx {p}/{len(ESTAT_INDEX_SERIES)}] {series['name']} データ無し")
                    continue

                vals = [{
                    "series_code": series["code"],
                    "series_name": series["name"],
                    "category":    series["category"],
                    "trade_date":  r["trade_date"],
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "volume": r["volume"],
                } for r in rows]
                n = upsert_macro_batch(db, vals)
                db.commit()
                saved += n
                if on_progress:
                    on_progress(idx, total, f"[e-Stat-idx {p}/{len(ESTAT_INDEX_SERIES)}] {series['name']}: {n}件処理")

    return saved
