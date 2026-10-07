"""企業マスタ・業種マスタ収集（EDINET コードリスト / JPX 業種マスタ）。"""
import bisect
import calendar
import csv
import io
import zipfile
import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Callable

import httpx
import pandas as pd
from sqlalchemy import func as sqla_func, or_
from sqlalchemy.exc import SQLAlchemyError

from database import (
    SessionLocal, Company, FinancialRecord, MacroData,
    XbrlRawDocument, upsert_company, upsert_financial,
    upsert_xbrl_raw, pack_elements, unpack_elements,
    build_xbrl_map,
    StockPriceDaily, StockPriceWeekly,
    record_prices_batch, trim_daily, latest_prices,
    KEY_JPX_INDUSTRY_LAST_SUCCESS, KEY_EDINET_CODELIST_LAST_SUCCESS, upsert_setting,
)

# 設定定数・log。スター import だと ruff が未定義名（F821）を検出できないので名前を明示する（#824）。
from collector_utils import (
    API_KEY, EDINET_BASE, EDINET_CODELIST_COLUMNS, EDINET_CODELIST_LISTED,
    EDINET_CODELIST_URL, EDINET_INDUSTRY_ALIASES, EDINET_MAX_CONSECUTIVE_FAILURES,
    EdinetAccessError, EdinetCodelistError, JPX_CODE_DROP_LIMIT, JPX_EXCEL_LINK_RE,
    JPX_EXCEL_URL, JPX_LISTING_URL, JpxIndustryError, RATE_SLEEP, log, redact_secrets,
)


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
                # `!=` だと NULL の行を更新しない（SQL の三値論理・#784）
                .where(Company.industry.is_distinct_from(ind))
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
                .where(FinancialRecord.industry.is_distinct_from(ind))
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


def _read_edinet_codelist(content: bytes) -> dict:
    """EDINET コードリスト（ZIP のバイト列）を `{edinet_code: (提出者業種, 上場か)}` に変換する純粋関数。

    **上場区分によらず全行を返す**（#797・ADR-0065）。#784 は上場の行だけを返していたが、残った
    非上場・空欄の社（大半は廃止社）の業種が空のままだと、過去年度の分析で「後に廃止した社」という
    未来情報の疑似業種になる。業種名は生のまま返す——別名表での正規化・許可名との照合・上場区分に
    よる警告の出し分けは `_plan_industry_fill` が担う。

    見出しは列の位置ではなく名前で引く（1行目は「ダウンロード実行日,…」のメタ行）。見出しが
    見つからない・上場の行が1件も無いときは `EdinetCodelistError` を送出する＝配布形式が変わった
    疑いがあるので、件数が減ったまま通さない（`_read_jpx_excel` の #632 と同じ考え方）。上場の行は
    約3,800あるのが正常で、0件は上場区分の書き方が変わったときにしか起きない。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise EdinetCodelistError(f"ZIP に CSV が無い（{zf.namelist()}）")
            text = zf.read(csv_names[0]).decode("cp932")
    except (zipfile.BadZipFile, UnicodeDecodeError) as e:
        raise EdinetCodelistError(f"コードリストを読めない: {type(e).__name__}: {e}") from e

    rows = list(csv.reader(io.StringIO(text, newline="")))
    col_code, col_listed, col_industry = EDINET_CODELIST_COLUMNS
    hdr_idx = next((i for i, r in enumerate(rows[:5]) if col_code in r), None)
    missing = (list(EDINET_CODELIST_COLUMNS) if hdr_idx is None
               else [c for c in EDINET_CODELIST_COLUMNS if c not in rows[hdr_idx]])
    if missing:
        raise EdinetCodelistError(f"コードリストの見出しに {missing} が無い（配布形式が変わった疑い）")
    hdr = rows[hdr_idx]
    i_code, i_listed, i_industry = (hdr.index(c) for c in EDINET_CODELIST_COLUMNS)
    width = max(i_code, i_listed, i_industry)

    out: dict = {}
    n_listed = 0
    for r in rows[hdr_idx + 1:]:
        if len(r) <= width:
            continue
        code, industry = r[i_code].strip(), r[i_industry].strip()
        if not (code and industry):
            continue
        listed = r[i_listed].strip() == EDINET_CODELIST_LISTED
        n_listed += listed
        out[code] = (industry, listed)
    if not n_listed:
        raise EdinetCodelistError("上場区分が「上場」の行が1件も無い（配布形式が変わった疑い）")
    return out


def _propagate_company_industry(db) -> int:
    """`companies.industry` を、業種が空（NULL・空文字）の `financial_records` へ写す（#784）。

    **毎晩回す必要がある**——収集は財務行の業種を空文字で書き直すため（XBRL から業種は取れない）。
    差分収集（Phase 4）は新しい行を空で作り、`refresh_company`・半期（H1）収集は既存行を空で
    上書きする。JPX の更新は証券コードで財務行も直すが、JPX に載らない社（地方単独上場・TOB 等で
    外れた社）の行は誰も直さず、業種別回帰から静かに漏れる。

    埋まっている行は変えない（会社の業種が後から変わっても、ここでは追随させない＝JPX が担う）。
    SQLite（テスト）と PostgreSQL の両方で動くよう、UPDATE … FROM ではなく相関サブクエリで書く。
    """
    from sqlalchemy import exists, select, update as sa_update

    co_industry = (select(Company.industry)
                   .where(Company.edinet_code == FinancialRecord.edinet_code)
                   .where(Company.industry.isnot(None), Company.industry != "")
                   .limit(1)
                   .scalar_subquery())
    has_co_industry = exists().where(Company.edinet_code == FinancialRecord.edinet_code,
                                     Company.industry.isnot(None), Company.industry != "")
    res = db.execute(
        sa_update(FinancialRecord)
        .where(or_(FinancialRecord.industry.is_(None), FinancialRecord.industry == ""))
        .where(has_co_industry)
        .values(industry=co_industry)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return res.rowcount


def _plan_industry_fill(db, codelist: dict) -> tuple[dict, Counter, Counter]:
    """業種が空の社を、どの業種で埋めるかを決める（書かない・#797）。

    戻り値は `(by_industry, rejected_listed, out_of_scope)`:

    - `by_industry`: `{業種名: [edinet_code, ...]}`。**上場区分によらず**、提出者業種（別名表で
      正規化）が DB に既にある業種名（＝JPX が書いた名前）に一致する社
    - `rejected_listed`: 上場社なのに名前が一致しなかった提出者業種の件数。表記差か分類体系の変更の
      疑いなので呼び出し側が WARNING で出す（書くと業種別回帰に1社だけの業種ができる）
    - `out_of_scope`: 非上場・区分空欄の社の、33業種外の名前の件数（`内国法人・組合（有価証券報告書等
      の提出義務者以外）`・`外国法人・組合`）。業種ではなく提出者の種別なので想定内＝INFO で数える

    計測（`scripts.measure_industry_fill_impact`）が本番と同じ選び方を使えるよう、書く処理から
    切り出してある。照合先の業種名が DB に1つも無いときは `EdinetCodelistError`。
    """
    allowed = {ind for (ind,) in db.query(Company.industry)
               .filter(Company.industry.isnot(None), Company.industry != "")
               .distinct()}
    if not allowed:
        raise EdinetCodelistError("照合先の業種名（JPX が書いた名前）が DB に1つも無い。"
                                  "JPX の業種更新が先に成功している必要がある")

    empty_codes = [c for (c,) in db.query(Company.edinet_code)
                   .filter(or_(Company.industry.is_(None), Company.industry == ""))]
    by_industry: dict = defaultdict(list)
    rejected_listed: Counter = Counter()
    out_of_scope: Counter = Counter()
    for code in empty_codes:
        entry = codelist.get(code)
        if entry is None:
            continue        # コードリストに無い社・提出者業種が空の社は埋めない
        raw, listed = entry
        industry = EDINET_INDUSTRY_ALIASES.get(raw, raw)
        if industry not in allowed:
            (rejected_listed if listed else out_of_scope)[raw] += 1
            continue
        by_industry[industry].append(code)
    return dict(by_industry), rejected_listed, out_of_scope


def _apply_industry_fill(db, by_industry: dict) -> int:
    """`_plan_industry_fill` の結果を会社へ書く。**commit しない**（呼び出し側が持つ）。

    業種が空の社だけを書く条件を UPDATE にも持たせる（計画と書き込みの間に JPX が埋めた社を
    上書きしない）。戻り値は書いた会社数。
    """
    from sqlalchemy import update as sa_update

    filled = 0
    for industry, codes in by_industry.items():
        res = db.execute(
            sa_update(Company)
            .where(Company.edinet_code.in_(codes))
            .where(or_(Company.industry.is_(None), Company.industry == ""))
            .values(industry=industry)
            .execution_options(synchronize_session=False)
        )
        filled += res.rowcount
    return filled


async def fill_industry_from_edinet_codelist(client: httpx.AsyncClient, db,
                                             on_progress: Optional[Callable] = None):
    """業種が空の社を、EDINET コードリストの提出者業種で埋める（#784・#797）。

    札証・福証の単独上場は JPX の一覧（東証のみ）に構造的に載らず、業種が空のまま業種別回帰
    （`sector_ols._eligible_base`）から外れて `gap_ratio` が付かなかった。例外も警告も出ない。

    - **上場区分によらない**（#797・ADR-0065）: 業種が空の社は「今日の JPX 一覧に載っていない＝
      その後に廃止した社」とほぼ同じ集合で、空文字のまま残すと過去の断面で未来情報の疑似業種になる
      （業種内Z・M-1 系の「不明」カテゴリ・時点再現の gap パネル）。#784 は上場の社だけを埋めていた
    - **JPX を上書きしない**: 会社の業種が空のときだけ埋める。JPX で業種が付いている社の 4.6% で
      EDINET の分類が食い違う（2026-10-02 実測）ので、正本は JPX のまま
    - **許す業種名は DB に既にある名前（＝JPX が書いた名前）だけ**。33業種名をコードへ写さない
      （写しは陳腐化する。旧 `TSE_INDUSTRY` は「証券、」を「証券・」と書いていた）。表記差は
      `EDINET_INDUSTRY_ALIASES` で吸収し、それでも一致しない上場社の名前は書かずに WARNING で数える。
      非上場社の33業種外の名前（提出者の種別）は想定内なので INFO で数える
    - 最後に会社の業種を空の財務行へ写す（`_propagate_company_industry`）

    取れなかった／解釈できなかったときは `EdinetCodelistError` を送出し、足跡を書かない（#632 と
    同じ作法。握って `(0, 0)` を返すと「埋める社が無かった」と区別できない）。戻り値は
    `(埋めた会社数, 写した財務行数)`。
    """
    try:
        log.info("EDINET コードリストをダウンロード中...")
        if on_progress:
            on_progress(0, 1, "[業種補完] EDINET コードリストをダウンロード中...")
        r = await client.get(EDINET_CODELIST_URL, timeout=60)
        r.raise_for_status()
        codelist = _read_edinet_codelist(r.content)

        by_industry, rejected_listed, out_of_scope = _plan_industry_fill(db, codelist)
        if rejected_listed:
            # 上場社の提出者業種が JPX の名前と食い違う＝表記差か分類体系の変更。書くと業種別回帰に
            # 1社だけの業種ができるので書かない。別名表に足すかは名前を見て決める。
            log.warning(f"EDINET 提出者業種が JPX の業種名に無いので書かなかった（上場社）: "
                        f"{dict(rejected_listed)}")
        if out_of_scope:
            log.info(f"33業種外の提出者業種（非上場・提出者の種別）は書かなかった: {dict(out_of_scope)}")

        filled_co = _apply_industry_fill(db, by_industry)
        db.commit()

        filled_fr = _propagate_company_industry(db)

        # 足跡は**補完0件でも書く**（0件は「埋める社が無かった」であって失敗ではない）。
        upsert_setting(db, KEY_EDINET_CODELIST_LAST_SUCCESS,
                       datetime.now(timezone.utc).isoformat(timespec="seconds"))

        n_listed = sum(1 for _ind, listed in codelist.values() if listed)
        log.info(f"業種補完完了（EDINET コードリスト {len(codelist)}社・うち上場 {n_listed}社）: "
                 f"Company {filled_co}件, FinancialRecord {filled_fr}件")
        if on_progress:
            on_progress(1, 1, f"[業種補完完了] Company {filled_co}件, FR {filled_fr}件")
        return filled_co, filled_fr
    except EdinetCodelistError:
        raise
    except Exception as e:
        detail = redact_secrets(f"{type(e).__name__}: {e}")
        log.warning(f"EDINET コードリストでの業種補完失敗: {detail}")
        raise EdinetCodelistError(f"EDINET コードリストを取得できない: {detail}") from e
