"""
collector_financials.py — XBRL財務収集・CF補完・reparse

内部でサブモジュール間の関数呼び出しは `sys.modules['collector']` 経由で行う。
これにより `patch("collector.xxx")` で正しくモックが効く。
"""

import asyncio
import io
import logging
import os
import sys
import zipfile
from datetime import date, timedelta
from typing import Optional, Callable

import httpx
import pandas as pd

from database import (
    SessionLocal, Company, FinancialRecord, XbrlRawDocument,
    upsert_company, upsert_financial, upsert_xbrl_raw,
    pack_elements, unpack_elements,
)
from collector_utils import (
    parse_xbrl_csv, parse_raw_rows, calc_derived,
    df_to_raw_rows, _detect_xbrl_columns, _match_capex_by_label,
)
from collector_master import (
    fetch_edinet_code_list, update_industry_from_jpx,
    EDINET_BASE, API_KEY, RATE_SLEEP,
)

log = logging.getLogger(__name__)

BATCH_PAUSE   = 3.0
SKIP_XBRL_RAW = os.environ.get("SKIP_XBRL_RAW", "true").lower() == "true"


def _c():
    """collector モジュールを遅延インポートして返す（循環 import 回避）"""
    return sys.modules.get('collector')


async def fetch_doc_list(client: httpx.AsyncClient, target_date: date) -> list:
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": target_date.isoformat(), "type": 2, "Subscription-Key": API_KEY}
    try:
        r = await client.get(url, params=params, timeout=30)
        r.raise_for_status()
        results = r.json().get("results") or []
        return [d for d in results
                if d.get("ordinanceCode") == "010"
                and d.get("formCode") == "030000"
                and d.get("secCode")]
    except Exception as e:
        log.warning(f"書類一覧取得失敗 {target_date}: {e}")
        return []


async def collect_doc_ids_for_period(client, start: date, end: date,
                                     edinet_codes: Optional[set] = None,
                                     max_companies: Optional[int] = None,
                                     on_progress: Optional[Callable] = None) -> list:
    docs = []
    seen_order: dict = {}
    total_days = (end - start).days + 1
    cur = start
    day_idx = 0
    while cur <= end:
        day_idx += 1
        if on_progress:
            on_progress(day_idx, total_days,
                        f"[書類スキャン {day_idx}/{total_days}日] {cur}  累計 {len(seen_order)}社")
        daily = await fetch_doc_list(client, cur)
        matched = daily if edinet_codes is None else [d for d in daily if d.get("edinetCode") in edinet_codes]
        for d in matched:
            ec = d.get("edinetCode")
            if ec not in seen_order:
                seen_order[ec] = len(seen_order)
        docs.extend(matched)
        if matched:
            log.info(f"{cur} -> {len(matched)}件（累計 {len(seen_order)} 社）")
        await asyncio.sleep(RATE_SLEEP)
        cur += timedelta(days=1)

    if max_companies and len(seen_order) > max_companies:
        top = {ec for ec, rank in seen_order.items() if rank < max_companies}
        docs = [d for d in docs if d.get("edinetCode") in top]
        log.info(f"max_companies={max_companies}: 先着{max_companies}社({len(docs)}件)に絞り込み")

    return docs


async def fetch_xbrl_csv(client: httpx.AsyncClient, doc_id: str):
    url = f"{EDINET_BASE}/documents/{doc_id}"
    params = {"type": 5, "Subscription-Key": API_KEY}
    try:
        r = await client.get(url, params=params, timeout=60)
        r.raise_for_status()
        _ZIP_MAX_BYTES = 200 * 1024 * 1024
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_files = [n for n in z.namelist() if n.endswith(".csv")]
            if not csv_files:
                return None
            total_uncompressed = sum(z.getinfo(n).file_size for n in z.namelist())
            if total_uncompressed > _ZIP_MAX_BYTES:
                log.warning(f"ZIPサイズ超過（{total_uncompressed // 1024 // 1024}MB）: {doc_id}")
                return None
            all_dfs = []
            for fname in csv_files:
                with z.open(fname) as f:
                    raw = f.read()
                try:
                    df_part = pd.read_csv(io.BytesIO(raw), encoding="utf-8", low_memory=False)
                except UnicodeDecodeError:
                    content = raw.decode("utf-16", errors="replace")
                    df_part = pd.read_csv(io.StringIO(content), sep="\t", low_memory=False)
                except Exception as e:
                    log.warning(f"XBRL CSVスキップ: {fname} / {e}")
                    continue
                if df_part is not None and not df_part.empty:
                    all_dfs.append(df_part)
        if not all_dfs:
            return None
        if len(all_dfs) == 1:
            return all_dfs[0]
        return pd.concat(all_dfs, ignore_index=True, sort=False)
    except zipfile.BadZipFile:
        log.warning(f"ZIPエラー: {doc_id}")
        return None
    except Exception as e:
        log.warning(f"XBRL取得失敗 {doc_id}: {e}")
        return None


async def refill_cf_from_xbrl(
    db,
    limit: int = 3000,
    capex_only: bool = False,
    missing_cf: bool = False,
    sleep_sec: float = 0.5,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    mode = "missing" if missing_cf else ("capex_only" if capex_only else "normal")
    log.info(f"refill_cf_from_xbrl 開始 (mode={mode}, limit={limit}, sleep={sleep_sec})")

    def _target_q():
        if missing_cf:
            return db.query(FinancialRecord).filter(
                FinancialRecord.cf_operating_cf.is_(None),
                FinancialRecord.doc_id.isnot(None),
            )
        q = db.query(FinancialRecord).filter(
            FinancialRecord.cf_operating_cf.isnot(None),
            FinancialRecord.doc_id.isnot(None),
        )
        if capex_only:
            return q.filter(
                FinancialRecord.cf_capex.is_(None),
                FinancialRecord.cf_net_change_cash.isnot(None),
            )
        return q.filter(FinancialRecord.cf_net_change_cash.is_(None))

    targets = _target_q().order_by(FinancialRecord.period_end.desc()).limit(limit).all()

    total = len(targets)
    log.info(f"  対象レコード: {total}件")
    if total == 0:
        return {"updated": 0, "skipped": 0, "failed": 0, "remaining": 0}

    updated = skipped = failed = 0
    SLEEP_SEC = sleep_sec

    # collector 名前空間経由で参照することで patch("collector.xxx") が効く
    _col = _c()
    _fetch_xbrl_csv = getattr(_col, 'fetch_xbrl_csv', fetch_xbrl_csv)
    _parse_xbrl_csv = getattr(_col, 'parse_xbrl_csv', parse_xbrl_csv)

    async with httpx.AsyncClient(timeout=60) as client:
        for i, rec in enumerate(targets, 1):
            msg = f"[CF補完 {i}/{total}] {rec.company_name} {rec.period_end}"
            if on_progress:
                on_progress(i, total, msg)
            if i % 100 == 0:
                log.info(msg)

            try:
                df = await _fetch_xbrl_csv(client, rec.doc_id)
                if df is None or df.empty:
                    skipped += 1
                    continue

                parsed = _parse_xbrl_csv(df, rec.edinet_code, str(rec.period_end))
                cf = parsed.get("cf", {})
                if not cf:
                    skipped += 1
                    continue

                changed = False
                for field, col in [
                    ("capex",           "cf_capex"),
                    ("net_change_cash", "cf_net_change_cash"),
                    ("investing_cf",    "cf_investing_cf"),
                    ("operating_cf",    "cf_operating_cf"),
                    ("financing_cf",    "cf_financing_cf"),
                ]:
                    val = cf.get(field)
                    if val is not None and getattr(rec, col) is None:
                        setattr(rec, col, val)
                        changed = True

                if changed:
                    if rec.cf_operating_cf is not None and rec.cf_investing_cf is not None:
                        rec.cf_free_cf = rec.cf_operating_cf + rec.cf_investing_cf
                    db.add(rec)
                    updated += 1
                else:
                    skipped += 1

                if updated % 100 == 0 and updated > 0:
                    db.commit()

            except Exception as e:
                log.warning(f"  CF補完失敗 {rec.edinet_code} {rec.doc_id}: {e}")
                failed += 1

            await asyncio.sleep(SLEEP_SEC)

    db.commit()

    remaining = _target_q().count()

    result = {"updated": updated, "skipped": skipped, "failed": failed, "remaining": remaining}
    log.info(f"refill_cf_from_xbrl 完了: {result}")
    return result


async def refill_pl_bs_from_xbrl(
    db,
    limit: Optional[int] = None,
    sleep_sec: float = RATE_SLEEP,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    from sqlalchemy.orm import defer

    log.info(f"refill_pl_bs_from_xbrl 開始 (limit={limit}, sleep={sleep_sec})")

    def _target_q():
        return db.query(FinancialRecord).filter(
            FinancialRecord.bs_inventory.is_(None),
            FinancialRecord.doc_id.isnot(None),
        )

    q = (_target_q()
         .options(defer(FinancialRecord.raw_xbrl_json))
         .order_by(FinancialRecord.period_end.desc()))
    if limit:
        q = q.limit(limit)
    targets = q.all()

    total = len(targets)
    log.info(f"  対象レコード: {total}件")
    if total == 0:
        return {"updated": 0, "skipped": 0, "failed": 0, "remaining": 0}

    updated = skipped = failed = 0

    _col = _c()
    _fetch_xbrl_csv = getattr(_col, 'fetch_xbrl_csv', fetch_xbrl_csv)
    _parse_xbrl_csv = getattr(_col, 'parse_xbrl_csv', parse_xbrl_csv)

    async with httpx.AsyncClient(timeout=60) as client:
        for i, rec in enumerate(targets, 1):
            msg = f"[PL/BS補完 {i}/{total}] {rec.company_name} {rec.period_end}"
            if on_progress:
                on_progress(i, total, msg)
            if i % 100 == 0:
                log.info(msg)

            try:
                df = await _fetch_xbrl_csv(client, rec.doc_id)
                if df is None or df.empty:
                    skipped += 1
                    continue

                parsed = _parse_xbrl_csv(df, rec.edinet_code, str(rec.period_end))
                changed = False
                for cat in ("pl", "bs"):
                    for field, val in (parsed.get(cat) or {}).items():
                        col = f"{cat}_{field}"
                        if val is not None and hasattr(rec, col) and getattr(rec, col) is None:
                            setattr(rec, col, val)
                            changed = True

                if changed:
                    db.add(rec)
                    updated += 1
                else:
                    skipped += 1

                if updated % 100 == 0 and updated > 0:
                    db.commit()

            except Exception as e:
                log.warning(f"  PL/BS補完失敗 {rec.edinet_code} {rec.doc_id}: {e}")
                failed += 1

            await asyncio.sleep(sleep_sec)

    db.commit()

    remaining = _target_q().count()
    result = {"updated": updated, "skipped": skipped, "failed": failed, "remaining": remaining}
    log.info(f"refill_pl_bs_from_xbrl 完了: {result}")
    return result


async def diagnose_cf_labels(db, limit: int = 20) -> dict:
    KW_LABEL = ["固定資産", "設備", "取得", "支出", "購入"]
    KW_ELEM  = ["Purchase", "Property", "Capital", "Acquisition", "Tangible", "PaymentsFor"]

    targets = (
        db.query(FinancialRecord)
        .filter(
            FinancialRecord.cf_operating_cf.isnot(None),
            FinancialRecord.doc_id.isnot(None),
        )
        .order_by(FinancialRecord.period_end.desc())
        .limit(limit)
        .all()
    )
    log.info(f"diagnose_cf_labels: {len(targets)}件をサンプリング")

    _col = _c()
    _fetch_xbrl_csv = getattr(_col, 'fetch_xbrl_csv', fetch_xbrl_csv)

    found: dict = {}
    async with httpx.AsyncClient(timeout=60) as client:
        for rec in targets:
            df = await _fetch_xbrl_csv(client, rec.doc_id)
            if df is None or df.empty:
                continue
            df.columns = [c.strip() for c in df.columns]
            col_map = _detect_xbrl_columns(df)
            ecol = col_map.get("element"); lcol = col_map.get("label"); vcol = col_map.get("value")
            if not ecol:
                continue
            log.info(f"--- {rec.company_name} ({rec.edinet_code}) {rec.period_end} doc={rec.doc_id} ---")
            log.info(f"    検出列: element={ecol}, label={lcol}, value={vcol}")
            for _, row in df.iterrows():
                raw_elem = str(row[ecol])
                elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
                label = str(row.get(lcol, "")) if lcol else ""
                if any(k in label for k in KW_LABEL) or any(k in elem for k in KW_ELEM):
                    val = row.get(vcol, "") if vcol else ""
                    matched = "★capex一致" if _match_capex_by_label(label) else ""
                    log.info(f"    [{elem}] 「{label}」 = {val} {matched}")
                    found[elem] = label
            await asyncio.sleep(0.5)

    log.info(f"diagnose_cf_labels 完了: ユニーク要素 {len(found)}種")
    return found


async def reparse_from_raw(year: Optional[int] = None,
                            edinet_code: Optional[str] = None,
                            on_progress: Optional[Callable] = None,
                            cancel_check: Optional[Callable] = None):
    _col = _c()
    _SessionLocal = getattr(_col, 'SessionLocal', SessionLocal)
    _parse_raw_rows = getattr(_col, 'parse_raw_rows', parse_raw_rows)

    db = _SessionLocal()
    try:
        q = db.query(XbrlRawDocument)
        if year:
            q = q.filter(XbrlRawDocument.period_end.like(f"{year}%"))
        if edinet_code:
            q = q.filter(XbrlRawDocument.edinet_code == edinet_code)
        docs = q.order_by(XbrlRawDocument.period_end.desc()).all()
        total = len(docs)
        log.info(f"XBRL 再解析開始: {total} 書類")

        for i, doc in enumerate(docs, 1):
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(i, total, f"[停止] ユーザーによる停止（{i}/{total}件処理済み）")
                db.commit()
                return True

            rows   = unpack_elements(doc.elements_gz)
            parsed = _parse_raw_rows(rows)
            if not any(parsed.get(cat) for cat in ("bs", "pl", "cf")):
                continue

            rec = calc_derived(parsed)
            co  = db.query(Company).filter_by(edinet_code=doc.edinet_code).first()
            xbrl_industry = rec.get("meta", {}).get("industry_name", "")
            rec.update({
                "edinet_code":  doc.edinet_code,
                "sec_code":     co.sec_code if co else "",
                "company_name": co.name if co else "",
                "industry":     xbrl_industry or (co.industry if co else ""),
                "year":         int(doc.period_end[:4]) if doc.period_end else 0,
                "period_end":   doc.period_end,
                "doc_id":       doc.doc_id,
                "source":       "EDINET_XBRL",
            })
            upsert_financial(db, rec)

            if on_progress:
                on_progress(i, total, f"[再解析 {i}/{total}] {doc.edinet_code} {doc.period_end}")
            if i % 100 == 0:
                db.commit()
                log.info(f"再解析 commit ({i}/{total})")

        db.commit()
        log.info(f"再解析完了: {total} 書類")
    finally:
        db.close()
    return False


async def _phase_upsert_master(db, client, on_progress, max_companies) -> tuple:
    _col = _c()
    _fetch_edinet_code_list = getattr(_col, 'fetch_edinet_code_list', fetch_edinet_code_list)

    companies_df = await _fetch_edinet_code_list(client)
    master_total = len(companies_df)
    log.info(f"企業マスタをDBに保存中... ({master_total}社)")
    if on_progress:
        on_progress(0, master_total, f"[企業マスタ保存] {master_total}社をDBに登録中...")
    MASTER_BATCH = 200
    for i, (_, row) in enumerate(companies_df.iterrows()):
        upsert_company(db, {
            "edinet_code":  row["edinet_code"],
            "sec_code":     row.get("sec_code", ""),
            "name":         row["company_name"],
            "industry":     row.get("industry", ""),
            "fiscal_month": int(row["fiscal_month"]) if str(row.get("fiscal_month", "")).isdigit() else None,
        })
        if (i + 1) % MASTER_BATCH == 0:
            db.commit()
            db.expire_all()
        if on_progress and (i + 1) % 500 == 0:
            on_progress(i + 1, master_total, f"[企業マスタ保存] {i+1}/{master_total}社完了")
    db.commit()
    if on_progress:
        on_progress(master_total, master_total, f"[企業マスタ保存完了] {master_total}社")

    company_info = {
        row["edinet_code"]: {
            "company_name": row["company_name"],
            "industry":     row.get("industry", ""),
        }
        for _, row in companies_df.iterrows()
    }
    edinet_set = set(companies_df["edinet_code"].tolist()) if max_companies and not companies_df.empty else None
    known_edinet = set(company_info.keys())
    return company_info, known_edinet, edinet_set


def _phase_build_skip_ids(db, skip_existing: bool, skip_if_raw_exists: bool) -> set:
    if skip_existing:
        rows = db.query(FinancialRecord.doc_id).filter(FinancialRecord.doc_id.isnot(None)).all()
        ids = {r[0] for r in rows}
        log.info(f"差分収集モード: {len(ids)}件の収集済みdoc_idをスキップ対象として読み込み")
        return ids
    if skip_if_raw_exists:
        rows = db.query(XbrlRawDocument.doc_id).all()
        ids = {r[0] for r in rows}
        log.info(f"raw_skip モード: xbrl_raw_documents に保存済みの {len(ids)} 件をスキップ")
        return ids
    return set()


async def _phase_process_docs(db, client, all_docs: list,
                               company_info: dict, known_edinet: set,
                               existing_doc_ids: set, skip_existing: bool,
                               on_progress, cancel_check) -> tuple:
    _col = _c()
    _fetch_xbrl_csv = getattr(_col, 'fetch_xbrl_csv', fetch_xbrl_csv)
    _parse_xbrl_csv = getattr(_col, 'parse_xbrl_csv', parse_xbrl_csv)
    _asyncio_sleep = getattr(getattr(_col, 'asyncio', None), 'sleep', asyncio.sleep) if _col else asyncio.sleep

    total   = len(all_docs)
    skipped = 0
    for i, doc in enumerate(all_docs):
        if cancel_check and cancel_check():
            if on_progress:
                on_progress(i, total, f"[停止] ユーザーによる停止（{i}/{total}件処理済み）")
            db.commit()
            log.info(f"収集をキャンセルしました（{i}/{total}件処理済み）")
            return skipped, True

        doc_id      = doc["docID"]
        edinet_code = doc["edinetCode"]
        sec_code    = (doc.get("secCode") or "")[:4]
        period_end  = doc.get("periodEnd") or ""
        filer_name  = doc.get("filerName") or company_info.get(edinet_code, {}).get("company_name", "")
        year        = int(period_end[:4]) if period_end else 0

        if skip_existing and doc_id in existing_doc_ids:
            skipped += 1
            if on_progress:
                on_progress(i + 1, total,
                            f"[{i+1}/{total}] スキップ（収集済み）: {filer_name} {period_end}")
            continue

        if on_progress:
            on_progress(i + 1, total, f"[{i+1}/{total}] {filer_name}({sec_code}) {period_end}")
        log.info(f"[{i+1}/{total}] {filer_name}({sec_code}) {period_end}")

        try:
            if edinet_code not in known_edinet:
                upsert_company(db, {
                    "edinet_code": edinet_code, "sec_code": sec_code,
                    "name": filer_name, "industry": "",
                })
                db.flush()
                known_edinet.add(edinet_code)

            xbrl_df = await _fetch_xbrl_csv(client, doc_id)
            await _asyncio_sleep(RATE_SLEEP)

            if not SKIP_XBRL_RAW and xbrl_df is not None and not xbrl_df.empty:
                raw_rows = df_to_raw_rows(xbrl_df)
                if raw_rows:
                    upsert_xbrl_raw(db, doc_id, edinet_code, period_end, raw_rows)
                    db.commit()

            raw = _parse_xbrl_csv(xbrl_df, edinet_code, period_end)
            if not any(raw.get(cat) for cat in ("bs", "pl", "cf")):
                log.warning(f"財務データなし（スキップ）: {filer_name} {doc_id}")
                continue
            rec = calc_derived(raw)

            xbrl_industry   = rec.get("meta", {}).get("industry_name", "")
            master_industry = company_info.get(edinet_code, {}).get("industry", "")
            rec.update({
                "edinet_code":  edinet_code,
                "sec_code":     sec_code,
                "company_name": filer_name,
                "industry":     xbrl_industry or master_industry,
                "year":         year,
                "period_end":   period_end,
                "doc_id":       doc_id,
                "source":       "EDINET_XBRL",
            })
            upsert_financial(db, rec)

            if (i + 1) % 50 == 0:
                db.commit()
                log.info("DB commit (50件)")
            if (i + 1) % 100 == 0:
                await _asyncio_sleep(BATCH_PAUSE)

        except Exception as e:
            log.error(f"書類処理エラー（スキップ）: {doc_id} {filer_name} — {e}", exc_info=True)
            try:
                db.rollback()
            except Exception as rollback_err:
                log.error(f"rollback失敗: {rollback_err}")

    return skipped, False


async def run_full_collection(db,
                              years_back: int = 5,
                              max_companies: Optional[int] = None,
                              on_progress: Optional[Callable] = None,
                              skip_existing: bool = False,
                              skip_if_raw_exists: bool = False,
                              cancel_check: Optional[Callable] = None):
    _col = _c()
    _collect_doc_ids_for_period = getattr(_col, 'collect_doc_ids_for_period', collect_doc_ids_for_period)
    _update_industry_from_jpx = getattr(_col, 'update_industry_from_jpx', update_industry_from_jpx)

    async with httpx.AsyncClient() as client:
        company_info, known_edinet, edinet_set = await _phase_upsert_master(
            db, client, on_progress, max_companies
        )

        existing_doc_ids = _phase_build_skip_ids(db, skip_existing, skip_if_raw_exists)

        today = date.today()
        start = date(today.year - years_back, 1, 1)
        end   = today - timedelta(days=1)
        log.info(f"書類一覧収集: {start} ~ {end}")
        if on_progress:
            on_progress(0, 1, f"[書類スキャン開始] {start} ～ {end}（{(end - start).days + 1}日分）")
        all_docs = await _collect_doc_ids_for_period(
            client, start, end, edinet_set, max_companies=max_companies, on_progress=on_progress
        )
        log.info(f"対象書類数: {len(all_docs)}")

        skipped, cancelled = await _phase_process_docs(
            db, client, all_docs, company_info, known_edinet,
            existing_doc_ids, skip_existing, on_progress, cancel_check
        )
        if cancelled:
            return True
        db.commit()
        log.info(f"全収集完了（スキップ: {skipped}件 / 取得: {len(all_docs) - skipped}件）")

        await _update_industry_from_jpx(client, db, on_progress=on_progress)
    return False


async def refresh_company(edinet_code: str, years_back: int = 5,
                          on_progress: Optional[Callable] = None):
    _col = _c()
    _fetch_xbrl_csv = getattr(_col, 'fetch_xbrl_csv', fetch_xbrl_csv)
    _parse_xbrl_csv = getattr(_col, 'parse_xbrl_csv', parse_xbrl_csv)
    _collect_doc_ids_for_period = getattr(_col, 'collect_doc_ids_for_period', collect_doc_ids_for_period)

    db = SessionLocal()
    try:
        async with httpx.AsyncClient() as client:
            today = date.today()
            start = date(today.year - years_back, 1, 1)
            docs  = await _collect_doc_ids_for_period(client, start, today, {edinet_code})
            total = len(docs)
            for i, doc in enumerate(docs):
                if on_progress:
                    on_progress(i + 1, total, f"[{i+1}/{total}] リフレッシュ中...")
                xbrl_df = await _fetch_xbrl_csv(client, doc["docID"])
                await asyncio.sleep(RATE_SLEEP)
                raw = _parse_xbrl_csv(xbrl_df, edinet_code, doc.get("periodEnd", ""))
                rec = calc_derived(raw)
                xbrl_industry = rec.get("meta", {}).get("industry_name", "")
                rec.update({
                    "edinet_code":  edinet_code,
                    "sec_code":     (doc.get("secCode") or "")[:4],
                    "company_name": doc.get("filerName", ""),
                    "industry":     xbrl_industry,
                    "year":         int(doc.get("periodEnd", "0000")[:4]),
                    "period_end":   doc.get("periodEnd", ""),
                    "doc_id":       doc["docID"],
                    "source":       "EDINET_XBRL",
                })
                upsert_financial(db, rec)
            db.commit()
            log.info(f"{edinet_code}: {len(docs)}件更新完了")
    finally:
        db.close()
