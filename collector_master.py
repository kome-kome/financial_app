"""
collector_master.py — EDINETコードリスト・JPX業種マスタ更新
"""

import asyncio
import logging
import os
from typing import Optional, Callable
from datetime import date, timedelta

import httpx
import pandas as pd
from sqlalchemy import update as sa_update

from database import Company, FinancialRecord, upsert_company

log = logging.getLogger(__name__)

EDINET_BASE   = "https://disclosure.edinet-fsa.go.jp/api/v2"
JPX_EXCEL_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
API_KEY       = os.environ.get("EDINET_API_KEY", "")
RATE_SLEEP    = 0.6


async def fetch_edinet_code_list(client: httpx.AsyncClient) -> pd.DataFrame:
    """書類一覧APIをスキャンして上場企業リストを構築する（直近400日分・週末スキップ）。"""
    log.info("書類一覧APIから上場企業リストを構築中（直近400日）...")
    companies: dict = {}
    today = date.today()
    for i in range(400):
        target = today - timedelta(days=i + 1)
        if target.weekday() >= 5:
            continue
        url = f"{EDINET_BASE}/documents.json"
        params = {"date": target.isoformat(), "type": 2, "Subscription-Key": API_KEY}
        try:
            r = await client.get(url, params=params, timeout=30)
            r.raise_for_status()
            for d in r.json().get("results") or []:
                code = d.get("edinetCode")
                sec  = (d.get("secCode") or "")[:4]
                name = d.get("filerName") or ""
                if code and sec:
                    companies[code] = {
                        "edinet_code":  code,
                        "sec_code":     sec,
                        "company_name": name,
                        "industry":     "",
                    }
            await asyncio.sleep(RATE_SLEEP)
        except Exception as e:
            log.warning(f"書類一覧取得失敗 {target}: {e}")

    df = pd.DataFrame(list(companies.values()))
    log.info(f"上場企業候補: {len(df)}社")
    return df


async def update_industry_from_jpx(client: httpx.AsyncClient, db,
                                   on_progress: Optional[Callable] = None):
    """JPX上場会社一覧Excelから TSE 33業種コードを取得し、Company/FinancialRecordを更新する"""
    import xlrd
    import openpyxl
    import io as _io
    try:
        log.info("JPX上場会社一覧Excelをダウンロード中...")
        if on_progress:
            on_progress(0, 1, "[業種更新] JPX上場会社一覧をダウンロード中...")
        r = await client.get(JPX_EXCEL_URL, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        try:
            wb = xlrd.open_workbook(file_contents=r.content, encoding_override='cp932')
            ws = wb.sheet_by_index(0)
            def _cell(row, col):
                return ws.cell_value(row, col)
            nrows = ws.nrows
        except xlrd.XLRDError:
            log.info("xlrd で読み込み失敗。openpyxl（xlsx）でリトライします")
            wb_xlsx = openpyxl.load_workbook(_io.BytesIO(r.content), read_only=True, data_only=True)
            ws_xlsx = wb_xlsx.active
            _rows = list(ws_xlsx.iter_rows(values_only=True))
            def _cell(row, col):
                return _rows[row][col]
            nrows = len(_rows)

        industry_map: dict = {}
        for row_idx in range(1, nrows):
            code_val = _cell(row_idx, 1)
            ind_val  = _cell(row_idx, 5)
            if ind_val in ('-', '', None):
                continue
            if isinstance(code_val, float):
                sec = str(int(code_val)).zfill(4)
            elif isinstance(code_val, str) and code_val.strip():
                sec = code_val.strip()
            else:
                continue
            industry_map[sec] = str(ind_val)

        log.info(f"JPX業種マップ: {len(industry_map)}件")

        from collections import defaultdict
        by_industry: dict = defaultdict(list)
        for sec, ind in industry_map.items():
            by_industry[ind].append(sec)
            stripped = sec.lstrip('0') or '0'
            if stripped != sec:
                by_industry[ind].append(stripped)

        updated_co = 0
        for ind, codes in by_industry.items():
            r = db.execute(
                sa_update(Company)
                .where(Company.sec_code.in_(codes))
                .where(Company.industry != ind)
                .values(industry=ind)
                .execution_options(synchronize_session=False)
            )
            updated_co += r.rowcount
        db.commit()

        updated_fr = 0
        for ind, codes in by_industry.items():
            r = db.execute(
                sa_update(FinancialRecord)
                .where(FinancialRecord.sec_code.in_(codes))
                .where(FinancialRecord.industry != ind)
                .values(industry=ind)
                .execution_options(synchronize_session=False)
            )
            updated_fr += r.rowcount
        db.commit()

        log.info(f"業種更新完了: Company {updated_co}件, FinancialRecord {updated_fr}件")
        if on_progress:
            on_progress(1, 1, f"[業種更新完了] Company {updated_co}件, FR {updated_fr}件")
        return updated_co, updated_fr
    except Exception as e:
        log.warning(f"JPX業種更新失敗: {e}")
        return 0, 0
