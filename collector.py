"""
EDINET全上場企業 財務データ収集・正規化エンジン
- EDINETコードリストCSVから全上場企業を取得（フォールバック: 書類一覧APIスキャン）
- 有価証券報告書XBRL(CSV形式)から BS/PL/CF を再分類・正規化
- stooqから株価・バリュエーション指標を取得
- PostgreSQL DBへ保存
"""

import bisect
import os, io, zipfile, logging, asyncio
from datetime import date, timedelta
from typing import Optional, Callable
from dotenv import load_dotenv
import httpx
import pandas as pd
from sqlalchemy import func as sqla_func
from database import (
    SessionLocal, Company, FinancialRecord, MacroData,
    XbrlRawDocument, upsert_company, upsert_financial,
    upsert_xbrl_raw, pack_elements, unpack_elements,
    build_xbrl_map,
    StockPriceDaily, StockPriceWeekly,
    record_prices_batch, trim_daily, latest_prices,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EDINET_BASE   = "https://disclosure.edinet-fsa.go.jp/api/v2"
JPX_EXCEL_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
API_KEY       = os.environ.get("EDINET_API_KEY", "")
RATE_SLEEP             = 0.6   # EDINET API のリクエスト間隔（秒）
BATCH_PAUSE            = 3.0   # 100件ごとの追加ポーズ（秒）
STOOQ_CONCURRENCY      = 30    # stooq 現在株価の同時接続数
STOOQ_HIST_CONCURRENCY = 20    # stooq 履歴の同時接続数（1リクエストが重いため控えめ）
JQUANTS_ENDPOINT       = "https://api.jquants.com/v2/equities/bars/daily"
JQUANTS_RATE_SLEEP     = 20.0  # リクエスト開始間隔の最低値（秒）。
JQUANTS_BACKFILL_DAYS  = 730   # J-Quants 無料プランの最大取得可能期間（2年分）
YAHOO_STOCK_RATE_SLEEP = 0.5   # Yahoo Finance 銘柄別取得のリクエスト間隔（秒）
                               # 銘柄ごとに1リクエスト。3800社×0.5s ≈ 32分
MAX_GAP_DAYS           = 30    # period_end から±30日以内の株価のみ採用（point_in_time マッチ）
                               # 実測：データ日は約8s、非営業日は約3s で応答。
                               # 無料プランの上限が約5リクエスト/60秒のため20s を確保して安全マージンを持たせる。
                               # データ日はダウンロードに~8秒かかるため追加待機ほぼゼロ。
                               # 祝日（即時400）の後は残り~7秒を補完スリープ。

# Supabase Free プランの DB 容量制約(500MB)で xbrl_raw_documents (TOAST 880MB)
# を持てないため、デフォルトで保存をスキップ。再解析が必要な場合のみ
# SKIP_XBRL_RAW=false にすると保存される
SKIP_XBRL_RAW = os.environ.get("SKIP_XBRL_RAW", "true").lower() == "true"

# TSE 33業種コード → 業種名
TSE_INDUSTRY = {
    "0050": "水産・農林業", "1050": "鉱業",       "2050": "建設業",
    "3050": "食料品",       "3100": "繊維製品",    "3150": "パルプ・紙",
    "3200": "化学",         "3250": "医薬品",       "3300": "石油・石炭製品",
    "3350": "ゴム製品",     "3400": "ガラス・土石製品", "3450": "鉄鋼",
    "3500": "非鉄金属",     "3550": "金属製品",    "3600": "機械",
    "3650": "電気機器",     "3700": "輸送用機器",  "3750": "精密機器",
    "3800": "その他製品",   "4050": "電気・ガス業","5050": "陸運業",
    "5100": "海運業",       "5150": "空運業",       "5200": "倉庫・運輸関連業",
    "5250": "情報・通信業", "6050": "卸売業",       "6100": "小売業",
    "7050": "銀行業",       "7100": "証券・商品先物取引業", "7150": "保険業",
    "7200": "その他金融業", "8050": "不動産業",    "9050": "サービス業",
}

# XBRL 生タグ → (section, field) のマップ。手書きせず FinancialRecord の各列 info["xbrl"]
# から逆引き生成する（再分類項目の唯一の源は列定義。database.build_xbrl_map 参照）。
XBRL_MAP = build_xbrl_map()

# 連結データ優先判定キー。"Prior1Year" は含めない（前期連結データが当期データを上書きするバグを防ぐ）
CONSOLIDATED_KEYS = ["Consolidated"]

# ── capex（設備投資）のラベル照合 ─────────────────────────────────────────
# 設備投資の CF 明細行は企業独自の拡張要素IDでタグ付けされることが多く、要素ID照合では
# 捕捉できない（実証: 標準要素 PurchaseOfPropertyPlantAndEquipment は0件）。
# EDINET CSV の「項目名」列（日本語標準ラベル）で照合することで拡張要素も捕捉する。
# INCLUDE 条件（いずれかを含む）かつ EXCLUDE 条件（いずれも含まない）で判定する。
CAPEX_LABEL_INCLUDE = ["取得による支出", "購入による支出"]   # 取得＝支出（アウトフロー）
CAPEX_LABEL_REQUIRE = ["有形固定資産"]                         # 有形固定資産を必ず含む（無形のみ除外、有形及び無形は可）
CAPEX_LABEL_EXCLUDE = ["売却", "収入", "減少"]                 # 売却収入・回収は capex ではない


def _match_capex_by_label(label: str) -> bool:
    """項目名（日本語ラベル）が設備投資（有形固定資産の取得による支出）に該当するか判定。

    例: "有形固定資産の取得による支出" → True
        "有形固定資産及び無形固定資産の取得による支出" → True
        "有形固定資産の売却による収入" → False（売却収入）
        "無形固定資産の取得による支出" → False（有形を含まない）
    """
    if not label:
        return False
    if not any(kw in label for kw in CAPEX_LABEL_REQUIRE):
        return False
    if not any(kw in label for kw in CAPEX_LABEL_INCLUDE):
        return False
    if any(kw in label for kw in CAPEX_LABEL_EXCLUDE):
        return False
    return True


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
        # xlrd は .xls 専用。JPX が .xlsx に移行した場合は openpyxl にフォールバック。
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

        def resolve_sec(raw_sec: str) -> str:
            s = (raw_sec or '').strip()
            return s if s in industry_map else s.zfill(4)

        # 全件ロードを避けるため、業種ごとにバルク UPDATE する（クエリ数 = 業種数 ≈ 33）。
        # industry_map のキーは4桁ゼロ埋め。DB に非ゼロ埋め形式で格納されている場合に対応するため、
        # ゼロ埋め前後の両方を WHERE IN に含める。
        from collections import defaultdict
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
    seen_order: dict = {}  # edinet_code -> 発見順(0始まり)
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

    # max_companies 指定時: 最初に発見した N 社の書類だけ返す（全期間スキャン済みなので両年分が含まれる）
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
        _ZIP_MAX_BYTES = 200 * 1024 * 1024  # 展開後200MB上限（ZIP爆弾対策）
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_files = [n for n in z.namelist() if n.endswith(".csv")]
            if not csv_files:
                return None
            total_uncompressed = sum(z.getinfo(n).file_size for n in z.namelist())
            if total_uncompressed > _ZIP_MAX_BYTES:
                log.warning(f"ZIPサイズ超過（{total_uncompressed // 1024 // 1024}MB）: {doc_id}")
                return None
            # EDINET XBRL ZIP には概要ファイルと詳細（CF 明細等）の複数 CSV が存在する。
            # 最大ファイルだけでは CF 明細（capex 等）を取り逃がすため全 CSV を結合する。
            all_dfs = []
            for fname in csv_files:
                with z.open(fname) as f:
                    raw = f.read()
                try:
                    df_part = pd.read_csv(io.BytesIO(raw), encoding="utf-8", low_memory=False)
                except UnicodeDecodeError:
                    # EDINET の XBRL CSV は UTF-16 LE (BOM付き) + タブ区切りの場合がある
                    content = raw.decode("utf-16", errors="replace")
                    df_part = pd.read_csv(io.StringIO(content), sep="\t", low_memory=False)
                except Exception:
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


def _detect_xbrl_columns(df) -> dict:
    """XBRL CSV の列名マップ {element, context, value, label} を検出して返す"""
    col_map = {}
    for c in df.columns:
        lc = c.lower()
        if "要素" in c or "element" in lc:          col_map["element"] = c
        elif "コンテキスト" in c or "context" in lc: col_map["context"] = c
        elif "項目名" in c or "label" in lc:          col_map["label"] = c   # 日本語標準ラベル（capex 照合用）
        elif "値" in c and "id" not in lc:            col_map["value"] = c
    return col_map


def df_to_raw_rows(df) -> list:
    """XBRL CSV DataFrame を [{element, context, value}, ...] に変換（raw 保存用）"""
    col_map = _detect_xbrl_columns(df)
    if not {"element", "value"}.issubset(col_map):
        return []
    rows = []
    for _, row in df.iterrows():
        raw_elem = str(row[col_map["element"]])
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        rows.append({
            "element": elem,
            "context": str(row.get(col_map.get("context", ""), "")),
            "value":   str(row[col_map["value"]]),
        })
    return rows


def _apply_row(
    elem: str, ctx: str, val_raw, cat: str, field: str,
    result: dict, _priority: dict, apply_capex_sign: bool = False,
) -> None:
    """共通フィルタ・優先度計算・結果反映。parse_raw_rows / parse_xbrl_csv の中核共通ロジック。

    Prior コンテキストスキップ・OperatingRevenue1 非連結フィルタ・
    is_consol/has_member/priority 計算・float 変換・meta/priority 更新を担う。
    apply_capex_sign=True のとき capex を負値（支出＝アウトフロー）に統一する。
    """
    # 前期比較データ（Prior1Year等）はスキップ。当期データのみ処理する
    if "Prior" in ctx and "CurrentYear" not in ctx:
        return
    # OperatingRevenue1 系（営業収益）は連結のみ採用。金融持株会社は連結営業収益を持たず
    # 提出会社単体（NonConsolidatedMember）の営業収益しか無いため、非連結値を売上に誤採用しない。
    if field == "revenue" and elem.startswith("OperatingRevenue1") and \
            ("NonConsolidated" in ctx or "_Member" in ctx):
        return
    is_consol  = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
    # 次元メンバー（セグメント別・株式種類別等）は全て "...Member" で終わる breakdown。
    # "_Member" 限定だと "ReportableSegmentMember"（直前にアンダースコア無し）を取りこぼし、
    # 連結総額（メンバー無し context）と同優先度で並んで CSV 順次第で上書きされる（従業員数で顕在化）。
    # 広く "Member" を breakdown とみなすことで連結総額を確実に優先する。
    has_member = "Member" in ctx or "NonConsolidated" in ctx
    priority   = 2 if is_consol else (1 if not has_member else 0)
    try:
        val = float(str(val_raw).replace(",", ""))
    except (ValueError, TypeError):
        return
    if apply_capex_sign and field == "capex":
        # capex は支出＝負（アウトフロー）で統一する（UI の符号ロバスト実装と整合）
        val = -abs(val)
    if cat == "meta":
        # 業種コード: 数値 → 4桁ゼロ埋め文字列 → 業種名に変換
        code_str = str(int(val)).zfill(4)
        result["meta"]["tse_industry_code"] = code_str
        result["meta"]["industry_name"] = TSE_INDUSTRY.get(code_str, "")
    else:
        key = f"{cat}_{field}"
        if priority > _priority.get(key, -1):
            result[cat][field] = val
            _priority[key] = priority


def parse_raw_rows(rows: list) -> dict:
    """[{element, context, value}, ...] から {bs, pl, cf, val, nonfin, meta} を抽出（再解析用）"""
    result = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "nonfin": {}, "meta": {}}
    _priority: dict = {}
    for row in rows:
        elem = row.get("element", "")
        category_field = XBRL_MAP.get(elem)
        if not category_field:
            continue
        cat, field = category_field
        ctx = row.get("context", "")
        _apply_row(elem, ctx, row.get("value", ""), cat, field, result, _priority)
    return result


def parse_xbrl_csv(df, edinet_code: str, period_end: str) -> dict:
    result = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "nonfin": {}, "meta": {}}
    if df is None or df.empty:
        return result
    df.columns = [c.strip() for c in df.columns]
    col_map = _detect_xbrl_columns(df)
    if not {"element", "value"}.issubset(col_map):
        return result

    _priority: dict = {}
    label_col = col_map.get("label")

    for _, row in df.iterrows():
        raw_elem = str(row[col_map["element"]])
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        category_field = XBRL_MAP.get(elem)

        # 要素ID照合で外れた行は capex のラベル照合を試みる（拡張要素ID対策）。
        # 設備投資は企業独自要素でタグ付けされるため項目名（日本語ラベル）で捕捉する。
        if not category_field:
            if label_col is not None and _match_capex_by_label(str(row.get(label_col, ""))):
                category_field = ("cf", "capex")
            else:
                continue

        cat, field = category_field
        ctx = str(row.get(col_map.get("context", ""), ""))
        _apply_row(elem, ctx, row[col_map["value"]], cat, field, result, _priority, apply_capex_sign=True)
    return result


def calc_derived(rec: dict) -> dict:
    bs, pl, cf = rec.get("bs", {}), rec.get("pl", {}), rec.get("cf", {})
    rev   = pl.get("revenue", 0) or 0
    op    = pl.get("operating_profit", 0) or 0
    ord_p = pl.get("ordinary_profit", 0) or 0
    net   = pl.get("net_income", 0) or pl.get("net_income_attr", 0) or 0
    asset = bs.get("total_assets", 0) or 0
    eq    = bs.get("total_equity", 0) or bs.get("equity_parent", 0) or 0
    debt  = (bs.get("short_term_debt", 0) or 0) + (bs.get("long_term_debt", 0) or 0)
    ocf   = cf.get("operating_cf", 0) or 0
    # free_cf は cf セクションに置くことで upsert_financial が cf_free_cf 列に正しくマップする
    cf["free_cf"] = ocf + (cf.get("investing_cf", 0) or 0)
    # 営業外損益（純額）= 経常利益 - 営業利益。pl セクションに置くことで pl_ プレフィックスが付く
    if op and ord_p:
        pl["nonoperating_income"] = round(ord_p - op, 0)
    # EBITDA = 営業利益 + 減価償却費及び償却費（C2 で depreciation を収集。両方揃う時のみ算出）
    dep = pl.get("depreciation")
    if op and dep:
        pl["ebitda"] = round(op + dep, 0)
    # 清原達郎式ネットキャッシュ = 流動資産 + 投資有価証券×0.7 − 総負債
    # 投資有価証券が未取得の古いレコードは 0 として扱う（簡易NCAV式相当）。
    # nc_ratio は market_cap 確定後に update_market_data_only で計算する。
    ca   = bs.get("current_assets", 0) or 0
    inv  = bs.get("investment_securities", 0) or 0
    tl   = bs.get("total_liabilities", 0) or 0
    net_cash = ca + inv * 0.7 - tl if (ca or tl) else None
    rec["derived"] = {
        "op_margin":    round(op / rev * 100, 2) if rev else None,
        "net_margin":   round(net / rev * 100, 2) if rev else None,
        "roe":          round(net / eq * 100, 2) if eq else None,
        "roa":          round(net / asset * 100, 2) if asset else None,
        "equity_ratio": round(eq / asset * 100, 2) if asset else None,
        "de_ratio":     round(debt / eq, 4) if eq else None,
        "cf_ratio":     round(ocf / rev * 100, 2) if rev else None,
        "net_cash":     round(net_cash, 0) if net_cash is not None else None,
    }
    return rec


# ── 市場データ取得（stooq） ────────────────────────────────────────────────

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


async def fetch_stock_history_stooq(
    session: httpx.AsyncClient,
    sec_code: str,
    date_from: str,   # "YYYYMMDD"
    date_to: str,     # "YYYYMMDD"
) -> list:
    """stooq 日次 OHLCV を取得して [{trade_date, open, high, low, close, volume}] で返す"""
    url = f"https://stooq.com/q/d/l/?s={sec_code}.jp&d1={date_from}&d2={date_to}&i=d"
    try:
        r = await session.get(url, timeout=30)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        log.debug(f"stooq履歴取得失敗 {sec_code}: {e}")
        return []

    rows = []
    for line in text.strip().splitlines()[1:]:   # ヘッダー行をスキップ
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            rows.append({
                "trade_date": parts[0],          # "YYYY-MM-DD"
                "open":       float(parts[1]),
                "high":       float(parts[2]),
                "low":        float(parts[3]),
                "close":      float(parts[4]),
                "volume":     float(parts[5]) if len(parts) > 5 else None,
            })
        except ValueError:
            continue
    return rows


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
    """
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
    inserted_total = 0
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

    sem = asyncio.Semaphore(STOOQ_HIST_CONCURRENCY)

    async def _fetch_hist(session, edinet_code, sec_code, name, d1_co, d2_co):
        async with sem:
            rows = await fetch_stock_history_stooq(session, sec_code, d1_co, d2_co)
        return edinet_code, sec_code, name, rows

    async with httpx.AsyncClient() as session:
        tasks = [asyncio.ensure_future(
            _fetch_hist(session, ec, sc, nm, d1c, d2c)
        ) for ec, sc, nm, d1c, d2c in to_fetch]

        completed = 0
        for coro in asyncio.as_completed(tasks):
            if cancel_check and cancel_check():
                for t in tasks:
                    t.cancel()
                if on_progress:
                    on_progress(completed, progress_total,
                                f"[停止] ユーザーによる停止（{completed}/{fetch_total}件処理済み）")
                db.commit()
                return {"cancelled": True, "inserted": inserted_total, "skipped": skipped_total}

            edinet_code, sec_code, name, rows = await coro
            completed += 1
            if on_progress:
                prog = completed if backfill else skipped_total + completed
                on_progress(prog, progress_total,
                            f"[{prog}/{progress_total}] {name}({sec_code}) {len(rows)}件")

            try:
                if rows:
                    # close-only 2本立て（daily+weekly）へ集約保存。trim はループ末で一括。
                    inserted_total += record_prices_batch(db, [
                        {"edinet_code": edinet_code, "trade_date": r["trade_date"],
                         "close": r.get("close"), "volume": r.get("volume")}
                        for r in rows
                    ], trim=False)
            except Exception as e:
                log.warning(f"株価履歴保存失敗 {sec_code}: {e}")

    trim_daily(db)
    if on_progress:
        on_progress(progress_total, progress_total, f"[完了] {total}社処理（スキップ:{skipped_total}社）、{inserted_total}件追加")
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

    total          = len(dates)
    upserted_total = 0

    async with httpx.AsyncClient() as session:
        completed = 0
        last_req_time: float = 0.0
        for date_str in dates:
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(completed, total, f"[停止] ユーザーによる停止（{completed}/{total}日処理済み）")
                db.commit()
                return {"cancelled": True, "upserted": upserted_total}

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
                continue

            records = []
            for q in quote_rows:
                code      = str(q.get("Code", ""))
                sec_code  = code[:4]   # J-Quants は "13010"（5桁）→ 先頭4桁が証券コード
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
                key = rec["edinet_code"]
                if key not in seen:
                    seen.add(key)
                    deduped.append(rec)
            records = deduped

            if records:
                # close-only 2本立て（daily+weekly）へ集約保存。trim はループ末で一括。
                upserted_total += record_prices_batch(db, records, trim=False)

            if on_progress:
                on_progress(completed, total,
                            f"[{completed}/{total}] {date_str} {len(records)}件")

    trim_daily(db)
    if on_progress:
        on_progress(total, total, f"[完了] {total}日処理・{upserted_total}件追加/更新")
    return {"cancelled": False, "upserted": upserted_total, "days": total}


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
    from sqlalchemy import func as sqlfunc

    if not point_in_time:
        # ── 最新レコードのみ・最新株価（daily＝直近窓を優先・無ければ weekly）─────────
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

    # ── point_in_time=True: 全レコードを period_end 近傍の株価で更新 ─────────
    # period_end 近傍の価格は全履歴を持つ weekly（close_last）から引く
    all_rows = db.query(
        StockPriceWeekly.edinet_code,
        StockPriceWeekly.trade_date,
        StockPriceWeekly.close_last,
    ).all()

    if not all_rows:
        log.info("update_market_data_from_history(point_in_time): stock_price_weekly が空のためスキップ")
        return 0

    # {edinet_code: sorted list of (trade_date_str, close)}
    from collections import defaultdict
    history: dict = defaultdict(list)
    for ec, td, cl in all_rows:
        if cl and cl > 0:
            history[ec].append((td, cl))
    for ec in history:
        history[ec].sort()  # trade_date の昇順

    all_records = db.query(FinancialRecord).all()

    # 最新レコード（year最大）を社別にメモリ上でインデックス（最後の上書きステップで使用）
    latest_by_ec: dict = {}
    for rec in all_records:
        ec = rec.edinet_code
        if ec not in latest_by_ec or (rec.year or 0) > (latest_by_ec[ec].year or 0):
            latest_by_ec[ec] = rec

    updated = 0
    for rec in all_records:
        prices = history.get(rec.edinet_code)
        if not prices:
            continue
        if not rec.period_end:
            continue

        # period_end を date に変換（"YYYY-MM-DD" 形式を前提）
        try:
            target = date.fromisoformat(rec.period_end[:10])
        except (ValueError, TypeError):
            continue

        # 最近傍の trade_date を二分探索
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
    from collections import defaultdict

    cutoff = (date.today() - timedelta(days=JQUANTS_BACKFILL_DAYS)).isoformat()

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
            period_ends = sorted(r.period_end[:10] for r in recs)
            d_from = (date.fromisoformat(period_ends[0])  - timedelta(days=MAX_GAP_DAYS)).strftime("%Y%m%d")
            d_to   = (date.fromisoformat(period_ends[-1]) + timedelta(days=MAX_GAP_DAYS)).strftime("%Y%m%d")

            # Yahoo Finance ティッカー（東証: {sec_code}.T）
            ticker = f"{sec_code}.T"
            rows = await fetch_yahoo_history(session, ticker, d_from, d_to)

            if rows:
                # {trade_date_str: close} の辞書
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
    """
    from sqlalchemy import func as sqlfunc

    # 最新日は直近窓の daily を基準にする（無ければ weekly にフォールバック）
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

    # sec_code → edinet_code マッピング
    companies = [
        (row.sec_code, row.edinet_code)
        for row in db.query(Company.sec_code, Company.edinet_code)
        .filter(Company.sec_code.isnot(None))
        .all()
    ]
    total      = len(companies)
    upserted   = 0

    async with httpx.AsyncClient() as session:
        for i, (sec_code, edinet_code) in enumerate(companies, 1):
            ticker = f"{sec_code}.T"
            rows = await fetch_yahoo_history(session, ticker, d_from, d_to)
            if rows:
                records = [
                    {
                        "edinet_code": edinet_code,
                        "trade_date":  r["trade_date"],
                        "close":  r["close"], "volume": r.get("volume"),
                    }
                    for r in rows if r["close"]
                ]
                if records:
                    # ギャップ補完は最新日より後の新規日付が対象のため衝突は稀。
                    # close-only 2本立てへ集約保存（trim はループ末で一括）。
                    upserted += record_prices_batch(db, records, trim=False)

            if on_progress and i % 500 == 0:
                on_progress(i, total, f"[Yahoo gap-fill {i}/{total}] {upserted}件追加")

            await asyncio.sleep(YAHOO_STOCK_RATE_SLEEP)

    trim_daily(db)
    log.info(f"fill_recent_stock_price_gap_yahoo: {upserted}件を株価テーブルへ集約保存")
    return {"skipped": False, "upserted": upserted, "from": d_from, "to": d_to}


async def refill_cf_from_xbrl(
    db,
    limit: int = 3000,
    capex_only: bool = False,
    missing_cf: bool = False,
    sleep_sec: float = 0.5,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """CF フィールドが NULL の既存レコードを EDINET XBRL から再取得して補完する。

    通常モード（capex_only=False, missing_cf=False）:
      対象 = cf_net_change_cash IS NULL かつ cf_operating_cf IS NOT NULL かつ doc_id IS NOT NULL
      net_change_cash は標準要素で充足率100%のため「未補完」マーカーとして使う。
      補完されると net_change_cash が埋まり対象から外れるため、繰り返し実行で自然終了する
      （capex を対象マーカーにすると、設備投資が存在しない企業を永久に再取得してしまう）。

    capex_only モード:
      対象 = cf_capex IS NULL かつ cf_net_change_cash IS NOT NULL かつ cf_operating_cf IS NOT NULL
      通常モードで net_change_cash 等は補完済みだが capex だけ未取得のレコード（ラベル照合追加前の
      旧バッチ分）を一度だけ補完する。手動ワンショット専用（スケジュールには載せない）。

    missing_cf モード:
      対象 = cf_operating_cf IS NULL かつ doc_id IS NOT NULL（＝CF が全 NULL）
      営業CFすら取得できていないレコード。IFRS決算の大企業（トヨタ等）が該当し、
      上記2モードは cf_operating_cf IS NOT NULL を前提とするため拾えなかった。
      XBRL_MAP への IFRS CF 要素追加（NetCash...IFRS / ...IFRSSummaryOfBusinessResults）と
      併用して埋める。営業CFが埋まれば対象から外れるため繰り返し実行で自然終了する。

    fetch_xbrl_csv の全 CSV concat + capex ラベル照合により capex/net_change_cash を補完する。
    """

    mode = "missing" if missing_cf else ("capex_only" if capex_only else "normal")
    log.info(f"refill_cf_from_xbrl 開始 (mode={mode}, limit={limit}, sleep={sleep_sec})")

    def _target_q():
        """モード別の対象レコードクエリ（targets 取得と remaining 集計で共用）。"""
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

    # 対象レコードを取得
    targets = _target_q().order_by(FinancialRecord.period_end.desc()).limit(limit).all()

    total = len(targets)
    log.info(f"  対象レコード: {total}件")
    if total == 0:
        return {"updated": 0, "skipped": 0, "failed": 0, "remaining": 0}

    updated = skipped = failed = 0
    SLEEP_SEC = sleep_sec  # EDINET API レート制限対策

    async with httpx.AsyncClient(timeout=60) as client:
        for i, rec in enumerate(targets, 1):
            msg = f"[CF補完 {i}/{total}] {rec.company_name} {rec.period_end}"
            if on_progress:
                on_progress(i, total, msg)
            if i % 100 == 0:
                log.info(msg)

            try:
                df = await fetch_xbrl_csv(client, rec.doc_id)
                if df is None or df.empty:
                    skipped += 1
                    continue

                parsed = parse_xbrl_csv(df, rec.edinet_code, str(rec.period_end))
                cf = parsed.get("cf", {})
                if not cf:
                    skipped += 1
                    continue

                changed = False
                # CF フィールドが NULL の場合のみ書き込む（既存値を保護）
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
                    # free_cf = 営業CF + 投資CF
                    if rec.cf_operating_cf is not None and rec.cf_investing_cf is not None:
                        rec.cf_free_cf = rec.cf_operating_cf + rec.cf_investing_cf
                    db.add(rec)
                    updated += 1
                else:
                    skipped += 1

                # 100件ごとに commit（長時間トランザクション対策）
                if updated % 100 == 0 and updated > 0:
                    db.commit()

            except Exception as e:
                log.warning(f"  CF補完失敗 {rec.edinet_code} {rec.doc_id}: {e}")
                failed += 1

            await asyncio.sleep(SLEEP_SEC)

    db.commit()

    # 残件数を集計（スケジュール実行の終了判定に使う）
    remaining = _target_q().count()

    result = {"updated": updated, "skipped": skipped, "failed": failed, "remaining": remaining}
    log.info(f"refill_cf_from_xbrl 完了: {result}")
    return result


async def diagnose_cf_labels(db, limit: int = 20) -> dict:
    """診断モード: サンプル書類の CF 関連ファクト（要素ID・項目名・値）をログに出力する。

    capex のラベル照合パターンを実データで検証するために使う。
    cf_operating_cf IS NOT NULL のレコードから先着 limit 件をサンプリングし、
    項目名に固定資産/設備/取得/支出 を含む、または要素IDに Purchase/Property/Capital/
    Acquisition/Tangible を含むファクトを全て出力する。
    """

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

    found: dict = {}
    async with httpx.AsyncClient(timeout=60) as client:
        for rec in targets:
            df = await fetch_xbrl_csv(client, rec.doc_id)
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
    # nc_ratio（= net_cash / 時価総額）は計算結果のため financial_metrics VIEW で都度算出する
    # （財務本体には永続化しない）。


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
            if updated % 50 == 0:
                db.commit()
                log.info(f"市場データ更新中: {updated}社完了")

    db.commit()
    log.info(f"市場データ更新完了: {updated}/{total}社")
    return False


# ── XBRL 再解析 ───────────────────────────────────────────────────────────

async def reparse_from_raw(year: Optional[int] = None,
                            edinet_code: Optional[str] = None,
                            on_progress: Optional[Callable] = None,
                            cancel_check: Optional[Callable] = None):
    """xbrl_raw_documents の生データから financial_records を再構築する。
    EDINET への通信は行わないため Render Free プランでも実行可能。
    """
    db = SessionLocal()
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
            parsed = parse_raw_rows(rows)
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


# ── フル収集 ──────────────────────────────────────────────────────────────

async def run_full_collection(db,
                              years_back: int = 5,
                              max_companies: Optional[int] = None,
                              on_progress: Optional[Callable] = None,
                              skip_existing: bool = False,
                              skip_if_raw_exists: bool = False,
                              cancel_check: Optional[Callable] = None):
    async with httpx.AsyncClient() as client:
        companies_df = await fetch_edinet_code_list(client)

        # 企業マスタは全件DBに保存（max_companiesは収集書類数の上限であり企業マスタは絞らない）
        df_master = companies_df

        master_total = len(df_master)
        log.info(f"企業マスタをDBに保存中... ({master_total}社)")
        if on_progress:
            on_progress(0, master_total, f"[企業マスタ保存] {master_total}社をDBに登録中...")
        # 3978 社の dirty を 1 トランザクションに溜めると Supabase が read-only
        # に切り替わるため、BATCH 件ごとに commit + expire_all してトランザクションを短く保つ
        MASTER_BATCH = 200
        for i, (_, row) in enumerate(df_master.iterrows()):
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
                on_progress(i + 1, master_total,
                            f"[企業マスタ保存] {i+1}/{master_total}社完了")
        db.commit()
        if on_progress:
            on_progress(master_total, master_total,
                        f"[企業マスタ保存完了] {master_total}社")

        # 差分収集モード: 収集済みのdoc_idをDBから一括取得してセットに保持
        existing_doc_ids: set = set()
        if skip_existing:
            rows = db.query(FinancialRecord.doc_id).filter(FinancialRecord.doc_id.isnot(None)).all()
            existing_doc_ids = {r[0] for r in rows}
            log.info(f"差分収集モード: {len(existing_doc_ids)}件の収集済みdoc_idをスキップ対象として読み込み")
        elif skip_if_raw_exists:
            rows = db.query(XbrlRawDocument.doc_id).all()
            existing_doc_ids = {r[0] for r in rows}
            log.info(f"raw_skip モード: xbrl_raw_documents に保存済みの {len(existing_doc_ids)} 件をスキップ")

        # company_name と industry を参照できるよう辞書化（全CSVデータを保持）
        company_info = {
            row["edinet_code"]: {
                "company_name": row["company_name"],
                "industry":     row.get("industry", ""),
            }
            for _, row in companies_df.iterrows()
        }
        # 全件収集時は formCode=030000+secCode フィルタ（fetch_doc_list内）に委任し
        # 不完全なマスタリストで絞り込まない。max_companies 指定時のみ絞り込む。
        edinet_set = None
        if max_companies and not df_master.empty:
            edinet_set = set(df_master["edinet_code"].tolist())
        known_edinet = set(company_info.keys())

        today = date.today()
        start = date(today.year - years_back, 1, 1)
        end   = today - timedelta(days=1)   # 昨日まで（最新の報告書を含む）

        log.info(f"書類一覧収集: {start} ~ {end}")
        if on_progress:
            on_progress(0, 1, f"[書類スキャン開始] {start} ～ {end}（{(end - start).days + 1}日分）")
        all_docs = await collect_doc_ids_for_period(
            client, start, end, edinet_set, max_companies=max_companies,
            on_progress=on_progress
        )
        total = len(all_docs)
        log.info(f"対象書類数: {total}")

        skipped = 0
        for i, doc in enumerate(all_docs):
            if cancel_check and cancel_check():
                if on_progress:
                    on_progress(i, total, f"[停止] ユーザーによる停止（{i}/{total}件処理済み）")
                db.commit()
                log.info(f"収集をキャンセルしました（{i}/{total}件処理済み）")
                return True

            doc_id      = doc["docID"]
            edinet_code = doc["edinetCode"]
            sec_code    = (doc.get("secCode") or "")[:4]
            period_end  = doc.get("periodEnd") or ""
            filer_name  = doc.get("filerName") or company_info.get(edinet_code, {}).get("company_name", "")
            year        = int(period_end[:4]) if period_end else 0

            # 差分収集: 収集済みの書類はスキップ
            if skip_existing and doc_id in existing_doc_ids:
                skipped += 1
                if on_progress:
                    on_progress(i + 1, total,
                                f"[{i+1}/{total}] スキップ（収集済み）: {filer_name} {period_end}")
                continue

            if on_progress:
                on_progress(i + 1, total,
                            f"[{i+1}/{total}] {filer_name}({sec_code}) {period_end}")

            log.info(f"[{i+1}/{total}] {filer_name}({sec_code}) {period_end}")

            try:
                # 書類から発見した未登録企業を都度upsert（FK制約エラー防止）
                if edinet_code not in known_edinet:
                    upsert_company(db, {
                        "edinet_code":  edinet_code,
                        "sec_code":     sec_code,
                        "name":         filer_name,
                        "industry":     "",
                    })
                    db.flush()
                    known_edinet.add(edinet_code)

                xbrl_df = await fetch_xbrl_csv(client, doc_id)
                await asyncio.sleep(RATE_SLEEP)

                # XBRL 全行を raw テーブルに保存（新指標追加時の再解析用）
                # xbrl_raw_documents への書き込みは即コミットしてロックを解放する
                # （financial_records との間でデッドロックが起きるのを防ぐため）
                if not SKIP_XBRL_RAW and xbrl_df is not None and not xbrl_df.empty:
                    raw_rows = df_to_raw_rows(xbrl_df)
                    if raw_rows:
                        upsert_xbrl_raw(db, doc_id, edinet_code, period_end, raw_rows)
                        db.commit()

                raw = parse_xbrl_csv(xbrl_df, edinet_code, period_end)
                if not any(raw.get(cat) for cat in ("bs", "pl", "cf")):
                    log.warning(f"財務データなし（スキップ）: {filer_name} {doc_id}")
                    continue
                rec = calc_derived(raw)

                # XBRLから業種が取れた場合は優先、なければマスタの業種
                xbrl_industry = rec.get("meta", {}).get("industry_name", "")
                master_industry = company_info.get(edinet_code, {}).get("industry", "")
                industry = xbrl_industry or master_industry

                rec.update({
                    "edinet_code":  edinet_code,
                    "sec_code":     sec_code,
                    "company_name": filer_name,
                    "industry":     industry,
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
                    await asyncio.sleep(BATCH_PAUSE)

            except Exception as e:
                log.error(f"書類処理エラー（スキップ）: {doc_id} {filer_name} — {e}", exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass

        db.commit()
        log.info(f"全収集完了（スキップ: {skipped}件 / 取得: {total - skipped}件）")

        # XBRL から業種が取れないため、JPX上場会社一覧から業種を補完する
        await update_industry_from_jpx(client, db, on_progress=on_progress)
    return False


async def refresh_company(edinet_code: str, years_back: int = 5,
                          on_progress: Optional[Callable] = None):
    db = SessionLocal()
    try:
        async with httpx.AsyncClient() as client:
            today = date.today()
            start = date(today.year - years_back, 1, 1)
            docs  = await collect_doc_ids_for_period(client, start, today, {edinet_code})
            total = len(docs)
            for i, doc in enumerate(docs):
                if on_progress:
                    on_progress(i + 1, total, f"[{i+1}/{total}] リフレッシュ中...")
                xbrl_df = await fetch_xbrl_csv(client, doc["docID"])
                await asyncio.sleep(RATE_SLEEP)
                raw = parse_xbrl_csv(xbrl_df, edinet_code, doc.get("periodEnd", ""))
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


# ── マクロデータ（為替・金利・指数・コモディティ）─────────────────────────

# stooq ティッカー定義。category は 'fx' / 'rate' / 'equity' / 'commodity'。
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


async def fetch_yahoo_history(
    session: httpx.AsyncClient,
    yf_ticker: str,
    date_from: str,   # "YYYYMMDD"
    date_to:   str,   # "YYYYMMDD"
) -> list:
    """Yahoo Finance v8 API から日次 OHLCV を取得する。
    GitHub Actions（Azure IP）からも動作する。stooq の代替として使用。"""
    import calendar
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
    """stooq 日次 OHLCV（汎用ティッカー）を取得して [{trade_date, open, high, low, close, volume}] で返す"""
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
    # ヘッダー（"Date,Open,High,Low,Close,Volume"）以外を解析
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
    """MACRO_SERIES の全系列について日次データを取得し macro_data に upsert する。
    Yahoo Finance を優先して使用し（GitHub Actions Azure IP でも動作）、
    取得失敗時は stooq にフォールバックする。
    既存レコードがあれば close 等を上書き（最新値で更新）。"""
    today    = date.today()
    start    = today - timedelta(days=int(years_back * 365.25))
    d1       = start.strftime("%Y%m%d")
    d2       = today.strftime("%Y%m%d")
    total    = len(MACRO_SERIES)
    saved    = 0

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

            # 既存行のキー集合を一度に取得し、無いものだけ INSERT、有るものは UPDATE
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


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EDINET全上場企業収集")
    parser.add_argument("--years",       type=int, default=5)
    parser.add_argument("--max",         type=int, default=None)
    parser.add_argument("--company",     type=str, default=None)
    parser.add_argument("--market",      action="store_true", help="市場データのみ更新")
    parser.add_argument("--macro",       action="store_true", help="マクロデータのみ収集")
    parser.add_argument("--incremental", action="store_true", help="収集済みをスキップ（差分収集）")
    parser.add_argument("--reparse",     action="store_true", help="xbrl_raw_documents から financial_records を再構築")
    parser.add_argument("--year",        type=int, default=None, help="再解析対象年度（--reparse と組み合わせ）")
    args = parser.parse_args()

    if args.reparse:
        asyncio.run(reparse_from_raw(
            year=args.year,
            edinet_code=args.company,
            on_progress=lambda c, t, m: print(m),
        ))
    elif args.market:
        async def _market():
            db = SessionLocal()
            try:
                await update_market_data(db, args.max)
            finally:
                db.close()
        asyncio.run(_market())
    elif args.macro:
        async def _run():
            db = SessionLocal()
            try:
                n = await collect_macro_data(db, args.years,
                    on_progress=lambda c, t, m: print(m))
                print(f"完了: {n} 件更新")
            finally:
                db.close()
        asyncio.run(_run())
    elif args.company:
        asyncio.run(refresh_company(args.company, args.years))
    else:
        async def _full():
            db = SessionLocal()
            try:
                await run_full_collection(db, args.years, args.max, skip_existing=args.incremental)
            finally:
                db.close()
        asyncio.run(_full())
