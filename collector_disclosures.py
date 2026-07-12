"""会社予想（ガイダンス）開示データ収集（J-Quants /fins/summary・Issue #322）。

決算短信サマリーの生開示を statement_disclosure へそのまま蓄積する。予想対比サプライズ
等の特徴量化（f_*/m_*/d_f_*）は別タスク（Issue #322 改善案③）で行う。
無料プランの制約は株価エンドポイントと同一設計（実測・調査コメント参照）:
  - 遡及期間: JQUANTS_BACKFILL_DAYS（730日）
  - 配信遅延: JQUANTS_DISCLOSURE_DELAY_DAYS（84日＝12週固定）
"""
import asyncio
import os
from datetime import date, timedelta
from typing import Callable, Optional

import httpx
from sqlalchemy import func as sqla_func

from collector_utils import (
    log, JQUANTS_SUMMARY_ENDPOINT, JQUANTS_RATE_SLEEP,
    JQUANTS_BACKFILL_DAYS, JQUANTS_DISCLOSURE_DELAY_DAYS,
)
from database import Company, StatementDisclosure, upsert_statement_disclosures


def _num(v) -> Optional[float]:
    """J-Quants の数値フィールドは文字列（"" は欠損）で返る。float に変換する。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


async def _jquants_fetch_summary_date(session: httpx.AsyncClient, api_key: str, date_str: str) -> list:
    """date_str (YYYY-MM-DD) の全銘柄・決算短信サマリーを返す（ページネーション対応）。
    400 = 非営業日または配信遅延・遡及期間外 → 空リストで正常終了。
    429 = レート制限 → 90秒待って1回だけ再試行。それでも429 なら skip。
    """
    headers = {"x-api-key": api_key}
    rows = []
    pagination_key = None
    while True:
        params: dict = {"date": date_str}
        if pagination_key:
            params["pagination_key"] = pagination_key
        r = await session.get(JQUANTS_SUMMARY_ENDPOINT, headers=headers, params=params, timeout=30)
        if r.status_code == 429:
            log.warning(f"J-Quants 429: {date_str} → 90秒後に再試行")
            await asyncio.sleep(90)
            r = await session.get(JQUANTS_SUMMARY_ENDPOINT, headers=headers, params=params, timeout=30)
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
        await asyncio.sleep(JQUANTS_RATE_SLEEP)
    return rows


def _row_to_record(row: dict, sec_to_edinet: dict) -> Optional[dict]:
    code = str(row.get("Code", ""))
    sec_code = code[:4]
    edinet_code = sec_to_edinet.get(sec_code)
    disc_no = row.get("DiscNo")
    if not edinet_code or not disc_no or not row.get("DiscDate"):
        return None
    return {
        "disc_no": disc_no,
        "edinet_code": edinet_code,
        "sec_code": sec_code,
        "disc_date": row["DiscDate"],
        "disc_time": row.get("DiscTime"),
        "doc_type": row.get("DocType"),
        "cur_per_type": row.get("CurPerType"),
        "cur_per_st": row.get("CurPerSt"),
        "cur_per_en": row.get("CurPerEn"),
        "cur_fy_st": row.get("CurFYSt"),
        "cur_fy_en": row.get("CurFYEn"),
        "nxt_fy_st": row.get("NxtFYSt"),
        "nxt_fy_en": row.get("NxtFYEn"),
        "sales": _num(row.get("Sales")),
        "op": _num(row.get("OP")),
        "odp": _num(row.get("OdP")),
        "np": _num(row.get("NP")),
        "eps": _num(row.get("EPS")),
        "deps": _num(row.get("DEPS")),
        "div_ann": _num(row.get("DivAnn")),
        "f_sales": _num(row.get("FSales")),
        "f_op": _num(row.get("FOP")),
        "f_odp": _num(row.get("FOdP")),
        "f_np": _num(row.get("FNP")),
        "f_eps": _num(row.get("FEPS")),
        "f_div_ann": _num(row.get("FDivAnn")),
        "nxf_sales": _num(row.get("NxFSales")),
        "nxf_op": _num(row.get("NxFOP")),
        "nxf_odp": _num(row.get("NxFOdP")),
        "nxf_np": _num(row.get("NxFNp")),   # J-Quants 側の実フィールド名の大文字小文字表記（"NxFNp"）に合わせる
        "nxf_eps": _num(row.get("NxFEPS")),
    }


async def collect_statement_disclosures(
    db,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    on_progress: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
) -> dict:
    """J-Quants /fins/summary から決算短信サマリーを日付単位で取得し statement_disclosure へ保存する。

    date_from 省略時: 差分収集（DB内の最新 disc_date から）。DB が空なら JQUANTS_BACKFILL_DAYS 分さかのぼる。
    date_to 省略時: 配信遅延境界（today - JQUANTS_DISCLOSURE_DELAY_DAYS）。それより新しい日付は指定しても
    切り詰める（無料プランでは常に空レスポンスのため無駄なリクエストを避ける）。
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise ValueError("環境変数 JQUANTS_API_KEY が未設定です")

    today = date.today()
    delay_cutoff = today - timedelta(days=JQUANTS_DISCLOSURE_DELAY_DAYS)
    _to = min(date_to, delay_cutoff) if date_to is not None else delay_cutoff

    if date_from is not None:
        _from = date_from
    else:
        latest = db.query(sqla_func.max(StatementDisclosure.disc_date)).scalar()
        _from = date.fromisoformat(latest) if latest else (today - timedelta(days=JQUANTS_BACKFILL_DAYS))

    if _from > _to:
        if on_progress:
            on_progress(0, 0, "[完了] 収集対象日なし（配信遅延境界内 or 範囲指定が逆転）")
        return {"cancelled": False, "upserted": 0, "days": 0}

    span = (_to - _from).days + 1
    dates = [
        (_from + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(span)
        if (_from + timedelta(days=i)).weekday() < 5
    ]
    total = len(dates)

    sec_to_edinet: dict = {
        row.sec_code: row.edinet_code
        for row in db.query(Company.sec_code, Company.edinet_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    }

    upserted_total = 0
    completed = 0
    last_req_time = 0.0
    async with httpx.AsyncClient() as session:
        for date_str in dates:
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(completed, total, f"[停止] ユーザーによる停止（{completed}/{total}日処理済み）")
                db.commit()
                return {"cancelled": True, "upserted": upserted_total}

            if completed > 0:
                elapsed = asyncio.get_event_loop().time() - last_req_time
                wait = JQUANTS_RATE_SLEEP - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
            last_req_time = asyncio.get_event_loop().time()

            raw_rows = await _jquants_fetch_summary_date(session, api_key, date_str)
            completed += 1

            if not raw_rows:
                if on_progress:
                    on_progress(completed, total, f"[{completed}/{total}] {date_str} 開示なし")
                continue

            records = [r for r in (_row_to_record(row, sec_to_edinet) for row in raw_rows) if r is not None]
            if records:
                upserted_total += upsert_statement_disclosures(db, records)
                db.commit()

            if on_progress:
                on_progress(completed, total, f"[{completed}/{total}] {date_str} {len(records)}件")

    if on_progress:
        on_progress(total, total, f"[完了] {total}日処理・{upserted_total}件追加/更新")
    return {"cancelled": False, "upserted": upserted_total, "days": total}
