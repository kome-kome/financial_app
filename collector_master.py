"""企業マスタ・業種マスタ収集（EDINET コードリスト / JPX 業種マスタ）。"""
import bisect
import calendar
import io
import zipfile
import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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
    KEY_JPX_INDUSTRY_LAST_SUCCESS, upsert_setting,
)

from collector_utils import *


async def fetch_edinet_code_list(client: httpx.AsyncClient) -> pd.DataFrame:
    """書類一覧APIをスキャンして上場企業リストを構築する（直近400日分・週末スキップ）。
    60日だと3月決算企業（Q3=2月提出）を取り逃がすため400日に拡張。
    """
    log.info("書類一覧APIから上場企業リストを構築中（直近400日）...")
    companies: dict = {}
    today = date.today()
    consecutive_failures = 0
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
            consecutive_failures = 0
            await asyncio.sleep(RATE_SLEEP)
        except Exception as e:
            # 単発は握って続行・**連続**は構造的なので送出する（#577。理屈は
            # `collect_doc_ids_for_period` と同じ）。例外文字列はクエリ付き URL＝
            # `Subscription-Key` を含むので必ず伏せる。
            consecutive_failures += 1
            detail = redact_secrets(f"{type(e).__name__}: {e}")
            log.warning(f"書類一覧取得失敗 {target}: {detail}")
            if consecutive_failures >= EDINET_MAX_CONSECUTIVE_FAILURES:
                raise EdinetAccessError(
                    f"企業マスタを構築できない（{target} まで走査・最後の理由: {detail}）",
                    consecutive_failures) from e

    df = pd.DataFrame(list(companies.values()))
    log.info(f"上場企業候補: {len(df)}社")
    return df


def _read_jpx_excel(content: bytes) -> dict:
    """JPX 上場会社一覧 Excel（バイト列）を `{sec_code(4桁ゼロ埋め): 業種名}` に変換する純粋関数。

    xlrd（.xls 専用）を優先し、JPX が .xlsx に移行した場合は openpyxl へフォールバックする。
    業種列（6列目）・コード列（2列目）を欠く行や、業種が空（'-'/''/None）の行はスキップする。
    DB 反映は呼び出し側（update_industry_from_jpx）が担う。

    **コード列の型は読み手によって違う**（#632）: xlrd は数値を `float` で返すが、
    openpyxl は `int` で返す。`float`/`str` しか受けていなかった旧実装は、xlsx へ移行した
    2026-09-03 以降**業種を持つ 3,899行のうち 3,606行を黙って捨てて 293件を返していた**
    （拾えたのは `130A` のような英字混じり＝文字列で返る新形式のコードだけ）。件数が減るだけで
    例外は出ないので、URL を直しても「正常終了しているのに 93% 欠落」に化ける。

    そこで**捨てた行を数え**、`JPX_CODE_DROP_LIMIT` を超えたら `JpxIndustryError` を送出する。
    正常なファイルではこの数は 0 なので、閾値をどこに置いても誤検知しない。
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
    candidates = 0      # 業種が埋まっている行＝本来は採用されるはずの行
    dropped    = 0      # そのうちコード列を解釈できなかった行
    for row_idx in range(1, nrows):
        try:
            code_val = _cell(row_idx, 1)
            ind_val  = _cell(row_idx, 5)
        except IndexError:
            continue   # 必須列を欠く行はスキップ
        if ind_val in ('-', '', None):
            continue
        candidates += 1
        # bool は int の派生。True/False がコードに化けないよう先に除く。
        if isinstance(code_val, (int, float)) and not isinstance(code_val, bool):
            sec = str(int(code_val)).zfill(4)
        elif isinstance(code_val, str) and code_val.strip():
            sec = code_val.strip()
        else:
            dropped += 1
            continue
        industry_map[sec] = str(ind_val)

    if candidates and dropped / candidates > JPX_CODE_DROP_LIMIT:
        raise JpxIndustryError(
            f"業種はあるがコード列を解釈できない行が多すぎる（{dropped}/{candidates}行）。"
            "JPX の列構成かセルの型が変わった疑いがあるので、件数が減ったまま通さない")
    return industry_map


async def resolve_jpx_excel_url(client: httpx.AsyncClient) -> str:
    """JPX 上場会社一覧ページから Excel の現行 URL を解決する（#632）。

    JPX はファイル名（拡張子）を予告なく変える。定数で持つと変わった晩から 404 になり、
    しかもそれは WARNING 止まりで `exit=0` のまま通る。**一覧ページを唯一の源にする**ことで、
    次に `.xls` へ戻されても、ハッシュ部分が変わっても追随できる。

    ページを読めない・リンクが無いときは `JPX_EXCEL_URL` を返す＝**旧挙動へ倒す**
    （解決の失敗をここで致命傷にしない。取得そのものの失敗は呼び出し側が現す）。
    """
    try:
        r = await client.get(JPX_LISTING_URL, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        m = JPX_EXCEL_LINK_RE.search(r.text)
        if not m:
            log.warning("JPX 一覧ページに data_j へのリンクが無い。既定 URL を使う")
            return JPX_EXCEL_URL
        url = str(httpx.URL(JPX_LISTING_URL).join(m.group(1)))
        if url != JPX_EXCEL_URL:
            log.info(f"JPX Excel の URL が既定と違う（解決値を使う）: {url}")
        return url
    except Exception as e:      # noqa: BLE001 — 解決の失敗は既定 URL で続行する
        log.warning(f"JPX 一覧ページを読めない（既定 URL を使う）: "
                    f"{redact_secrets(f'{type(e).__name__}: {e}')}")
        return JPX_EXCEL_URL


async def update_industry_from_jpx(client: httpx.AsyncClient, db,
                                   on_progress: Optional[Callable] = None):
    """JPX上場会社一覧Excelから TSE 33業種コードを取得し、Company/FinancialRecordを更新する。

    取れなかった／解釈できなかったときは `JpxIndustryError` を送出する（#632）。**握って
    `(0, 0)` を返さない**——それだと「業種に変化が無かった」と区別できず、6晩連続の 404 が
    `exit=0` のまま通る。成功したときは `app_settings` に足跡を書き、
    `batch_freshness.PRODUCERS` が翌日以降その鮮度を見る。
    """
    try:
        log.info("JPX上場会社一覧Excelをダウンロード中...")
        if on_progress:
            on_progress(0, 1, "[業種更新] JPX上場会社一覧をダウンロード中...")
        excel_url = await resolve_jpx_excel_url(client)
        r = await client.get(excel_url, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        industry_map = _read_jpx_excel(r.content)
        log.info(f"JPX業種マップ: {len(industry_map)}件")
        if not industry_map:
            raise JpxIndustryError(f"業種マップが空（{excel_url}）。"
                                   "取得はできたが1社も解釈できていない")

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

        # 足跡は**更新0件でも書く**（0件は「変化が無かった」であって失敗ではない）。
        # 書式は `scripts/batch_common.utc_now_iso()` と揃える（`batch_freshness._parse` が読む）。
        upsert_setting(db, KEY_JPX_INDUSTRY_LAST_SUCCESS,
                       datetime.now(timezone.utc).isoformat(timespec="seconds"))

        log.info(f"業種更新完了: Company {updated_co}件, FinancialRecord {updated_fr}件")
        if on_progress:
            on_progress(1, 1, f"[業種更新完了] Company {updated_co}件, FR {updated_fr}件")
        return updated_co, updated_fr
    except JpxIndustryError:
        raise
    except Exception as e:
        detail = redact_secrets(f"{type(e).__name__}: {e}")
        log.warning(f"JPX業種更新失敗: {detail}")
        raise JpxIndustryError(f"JPX 業種マスタを取得できない: {detail}") from e
