"""企業マスタ・業種マスタ収集（EDINET コードリスト / JPX 業種マスタ）。"""
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
)

from collector_utils import *


async def fetch_edinet_code_list(client: httpx.AsyncClient) -> pd.DataFrame:
    """書類一覧APIをスキャンして上場企業リストを構築する（直近400日分・週末スキップ）。
    60日だと3月決算企業（Q3=2月提出）を取り逃がすため400日に拡張。
    """
    log.info("書類一覧APIから上場企業リストを構築中（直近400日）...")
    companies: dict = {}
    today = date.today()
    for i in range(400):
        target = today - timedelta(days=i + 1)
        if target.weekday() >= 5:  # 土日はEDINET提出なし
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


def _read_jpx_excel(content: bytes) -> dict:
    """JPX 上場会社一覧 Excel（バイト列）を `{sec_code(4桁ゼロ埋め): 業種名}` に変換する純粋関数。

    xlrd（.xls 専用）を優先し、JPX が .xlsx に移行した場合は openpyxl へフォールバックする。
    業種列（6列目）・コード列（2列目）を欠く行や、業種が空（'-'/''/None）の行はスキップする。
    DB 反映は呼び出し側（update_industry_from_jpx）が担う。
    """
    import xlrd
    import openpyxl
    import io as _io

    try:
        wb = xlrd.open_workbook(file_contents=content, encoding_override='cp932')
        ws = wb.sheet_by_index(0)
        def _cell(row, col):
            return ws.cell_value(row, col)
        nrows = ws.nrows
    except xlrd.XLRDError:
        log.info("xlrd で読み込み失敗。openpyxl（xlsx）でリトライします")
        wb_xlsx = openpyxl.load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
        ws_xlsx = wb_xlsx.active
        _rows = list(ws_xlsx.iter_rows(values_only=True))
        def _cell(row, col):
            return _rows[row][col]
        nrows = len(_rows)

    industry_map: dict = {}
    for row_idx in range(1, nrows):
        try:
            code_val = _cell(row_idx, 1)
            ind_val  = _cell(row_idx, 5)
        except IndexError:
            continue   # 必須列を欠く行はスキップ
        if ind_val in ('-', '', None):
            continue
        if isinstance(code_val, float):
            sec = str(int(code_val)).zfill(4)
        elif isinstance(code_val, str) and code_val.strip():
            sec = code_val.strip()
        else:
            continue
        industry_map[sec] = str(ind_val)
    return industry_map


async def update_industry_from_jpx(client: httpx.AsyncClient, db,
                                   on_progress: Optional[Callable] = None):
    """JPX上場会社一覧Excelから TSE 33業種コードを取得し、Company/FinancialRecordを更新する"""
    try:
        log.info("JPX上場会社一覧Excelをダウンロード中...")
        if on_progress:
            on_progress(0, 1, "[業種更新] JPX上場会社一覧をダウンロード中...")
        r = await client.get(JPX_EXCEL_URL, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        industry_map = _read_jpx_excel(r.content)
        log.info(f"JPX業種マップ: {len(industry_map)}件")

        def resolve_sec(raw_sec: str) -> str:
            s = (raw_sec or '').strip()
            return s if s in industry_map else s.zfill(4)

        # 全件ロードを避けるため、業種ごとにバルク UPDATE する（クエリ数 = 業種数 ≈ 33）。
        # industry_map のキーは4桁ゼロ埋め。DB に非ゼロ埋め形式で格納されている場合に対応するため、
        # ゼロ埋め前後の両方を WHERE IN に含める。
        from sqlalchemy import update as sa_update

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
