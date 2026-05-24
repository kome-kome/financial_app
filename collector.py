"""
EDINET全上場企業 財務データ収集・正規化エンジン
- EDINETコードリストCSVから全上場企業を取得（フォールバック: 書類一覧APIスキャン）
- 有価証券報告書XBRL(CSV形式)から BS/PL/CF を再分類・正規化
- stooqから株価・バリュエーション指標を取得
- PostgreSQL DBへ保存
"""

import os, io, zipfile, logging, asyncio
from datetime import date, timedelta
from typing import Optional, Callable
from dotenv import load_dotenv
import httpx
import pandas as pd
from database import (
    SessionLocal, Company, FinancialRecord, MacroData,
    XbrlRawDocument, upsert_company, upsert_financial,
    upsert_xbrl_raw, pack_elements, unpack_elements,
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
                               # 実測：データ日は約8s、非営業日は約3s で応答。
                               # 無料プランの上限が約5リクエスト/60秒のため20s を確保して安全マージンを持たせる。
                               # データ日はダウンロードに~8秒かかるため追加待機ほぼゼロ。
                               # 祝日（即時400）の後は残り~7秒を補完スリープ。

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

XBRL_MAP = {
    # ── PL 売上・収益（JGAAP）──────────────────────────────────────────────
    "NetSales":                                        ("pl", "revenue"),
    "Revenues":                                        ("pl", "revenue"),
    "NetRevenues":                                     ("pl", "revenue"),
    "OperatingRevenues":                               ("pl", "revenue"),
    "Revenue":                                         ("pl", "revenue"),
    # ── PL 売上・収益（IFRS）──────────────────────────────────────────────
    "RevenueIFRS":                                     ("pl", "revenue"),
    "RevenueIFRSSummaryOfBusinessResults":             ("pl", "revenue"),
    # ── PL 売上原価・費用（JGAAP）───────────────────────────────────────
    "CostOfSales":                                     ("pl", "cost_of_sales"),
    "SellingGeneralAndAdministrativeExpenses":         ("pl", "sga"),
    # ── PL 売上原価・費用（IFRS）────────────────────────────────────────
    "CostOfSalesIFRS":                                 ("pl", "cost_of_sales"),
    # ── PL 利益系（JGAAP）────────────────────────────────────────────────
    "GrossProfit":                                     ("pl", "gross_profit"),
    "OperatingIncome":                                 ("pl", "operating_profit"),
    "OperatingProfit":                                 ("pl", "operating_profit"),
    "ProfitFromOperatingActivities":                   ("pl", "operating_profit"),
    "OrdinaryIncome":                                  ("pl", "ordinary_profit"),
    "ProfitLossBeforeIncomeTaxes":                     ("pl", "pretax_profit"),
    "NetIncomeLoss":                                   ("pl", "net_income"),
    "ProfitLoss":                                      ("pl", "net_income"),
    "ProfitLossAttributableToOwnersOfParent":          ("pl", "net_income_attr"),
    "EarningsPerShare":                                ("pl", "eps"),
    "BasicEarningsLossPerShare":                       ("pl", "eps"),
    "BasicEarningsLossPerShareSummaryOfBusinessResults": ("pl", "eps"),   # 経営指標等セクション
    # ── PL 利益系（IFRS）────────────────────────────────────────────────
    "GrossProfitIFRS":                                 ("pl", "gross_profit"),
    "OperatingProfitLossIFRS":                         ("pl", "operating_profit"),
    "ProfitFromOperatingActivitiesIFRS":               ("pl", "operating_profit"),
    "ProfitLossBeforeIncomeTaxesIFRS":                 ("pl", "pretax_profit"),
    "ProfitLossIFRS":                                  ("pl", "net_income"),
    "ProfitLossAttributableToOwnersOfParentIFRS":      ("pl", "net_income_attr"),
    "BasicEarningsLossPerShareIFRS":                   ("pl", "eps"),
    "EarningsPerShareIFRS":                            ("pl", "eps"),
    # ── BS（JGAAP）───────────────────────────────────────────────────────
    "Assets":                                          ("bs", "total_assets"),
    "Liabilities":                                     ("bs", "total_liabilities"),
    "Equity":                                          ("bs", "total_equity"),
    "NetAssets":                                       ("bs", "total_equity"),
    "EquityAttributableToOwnersOfParent":              ("bs", "equity_parent"),
    "CurrentAssets":                                   ("bs", "current_assets"),
    "NoncurrentAssets":                                ("bs", "noncurrent_assets"),
    "CurrentLiabilities":                              ("bs", "current_liabilities"),
    "NoncurrentLiabilities":                           ("bs", "noncurrent_liabilities"),
    "CashAndCashEquivalents":                          ("bs", "cash"),
    # BS 投資有価証券（清原式ネットキャッシュ用・JGAAP）
    "InvestmentSecurities":                            ("bs", "investment_securities"),
    "InvestmentsInSecurities":                         ("bs", "investment_securities"),
    "ShortTermInvestmentSecurities":                   ("bs", "investment_securities"),
    # BS 流動資産 詳細（JGAAP）
    "NotesAndAccountsReceivableTrade":                 ("bs", "receivables"),
    "AccountsReceivableTrade":                         ("bs", "receivables"),
    "Inventories":                                     ("bs", "inventory"),
    # BS 固定資産 詳細（JGAAP）
    "IntangibleAssets":                                ("bs", "intangible_assets"),
    "BuildingsAndStructuresNet":                       ("bs", "buildings"),
    "BuildingsAndStructures":                          ("bs", "buildings"),
    "MachineryAndEquipmentNet":                        ("bs", "machinery"),
    "MachineryAndEquipment":                           ("bs", "machinery"),
    # BS 負債 詳細（JGAAP）
    "NotesAndAccountsPayableTrade":                    ("bs", "payables"),
    "AccountsPayableTrade":                            ("bs", "payables"),
    "BondsPayable":                                    ("bs", "bonds_payable"),
    # BS 純資産 詳細（JGAAP）
    "CapitalStock":                                    ("bs", "paid_in_capital"),
    "RetainedEarnings":                                ("bs", "retained_earnings"),
    "ShortTermLoansPayable":                           ("bs", "short_term_debt"),
    "LongTermLoansPayable":                            ("bs", "long_term_debt"),
    "BookValuePerShare":                               ("bs", "bps"),
    "NetAssetsPerShareSummaryOfBusinessResults":       ("bs", "bps"),     # 経営指標等セクション
    # ── BS（IFRS）────────────────────────────────────────────────────────
    "AssetsIFRS":                                      ("bs", "total_assets"),
    "LiabilitiesIFRS":                                 ("bs", "total_liabilities"),
    "EquityIFRS":                                      ("bs", "total_equity"),
    "EquityAttributableToOwnersOfParentIFRS":          ("bs", "equity_parent"),
    "CurrentAssetsIFRS":                               ("bs", "current_assets"),
    "NoncurrentAssetsIFRS":                            ("bs", "noncurrent_assets"),
    "CurrentLiabilitiesIFRS":                          ("bs", "current_liabilities"),
    "NoncurrentLiabilitiesIFRS":                       ("bs", "noncurrent_liabilities"),
    "CashAndCashEquivalentsIFRS":                      ("bs", "cash"),
    # BS 投資有価証券（IFRS — 非流動その他金融資産で近似。流動性の高い金融資産は別科目のため除外）
    "OtherFinancialAssetsNonCurrentIFRS":              ("bs", "investment_securities"),
    # BS 詳細（IFRS）
    "TradeAndOtherReceivablesCurrentIFRS":             ("bs", "receivables"),
    "InventoriesIFRS":                                 ("bs", "inventory"),
    "IntangibleAssetsIFRS":                            ("bs", "intangible_assets"),
    "GoodwillAndIntangibleAssetsIFRS":                 ("bs", "intangible_assets"),
    "TradeAndOtherPayablesCurrentIFRS":                ("bs", "payables"),
    "IssuedCapitalIFRS":                               ("bs", "paid_in_capital"),
    "RetainedEarningsIFRS":                            ("bs", "retained_earnings"),
    # ── CF（JGAAP/共通）──────────────────────────────────────────────────
    "NetCashProvidedByUsedInOperatingActivities":      ("cf", "operating_cf"),
    "NetCashProvidedByUsedInInvestingActivities":      ("cf", "investing_cf"),
    "NetCashProvidedByUsedInFinancingActivities":      ("cf", "financing_cf"),
    "CashAndCashEquivalentsPeriodIncreaseDecrease":    ("cf", "net_change_cash"),
    "CapitalExpendituresForTangibleAssets":            ("cf", "capex"),
    # ── CF（IFRS）────────────────────────────────────────────────────────
    "CashFlowsFromUsedInOperatingActivitiesIFRS":      ("cf", "operating_cf"),
    "CashFlowsFromUsedInInvestingActivitiesIFRS":      ("cf", "investing_cf"),
    "CashFlowsFromUsedInFinancingActivitiesIFRS":      ("cf", "financing_cf"),
    # ── バリュエーション・配当 ────────────────────────────────────────────
    "DividendPaidPerShare":                            ("val", "dps"),
    "DividendPaidPerShareSummaryOfBusinessResults":    ("val", "dps"),    # 経営指標等セクション
}

# 連結データ優先判定キー。"Prior1Year" は含めない（前期連結データが当期データを上書きするバグを防ぐ）
CONSOLIDATED_KEYS = ["Consolidated"]


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
    try:
        log.info("JPX上場会社一覧Excelをダウンロード中...")
        if on_progress:
            on_progress(0, 1, "[業種更新] JPX上場会社一覧をダウンロード中...")
        r = await client.get(JPX_EXCEL_URL, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        wb = xlrd.open_workbook(file_contents=r.content, encoding_override='cp932')
        ws = wb.sheet_by_index(0)

        industry_map: dict = {}
        for row_idx in range(1, ws.nrows):
            code_val = ws.cell_value(row_idx, 1)
            ind_val  = ws.cell_value(row_idx, 5)
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

        companies = db.query(Company).all()
        updated_co = 0
        for co in companies:
            sec = resolve_sec(co.sec_code or '')
            ind = industry_map.get(sec, '')
            if ind and co.industry != ind:
                co.industry = ind
                updated_co += 1
        db.commit()

        records = db.query(FinancialRecord).all()
        updated_fr = 0
        for fr in records:
            sec = resolve_sec(fr.sec_code or '')
            ind = industry_map.get(sec, '')
            if ind and fr.industry != ind:
                fr.industry = ind
                updated_fr += 1
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
            biggest = max(csv_files, key=lambda n: z.getinfo(n).file_size)
            with z.open(biggest) as f:
                raw = f.read()
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding="utf-8", low_memory=False)
        except UnicodeDecodeError:
            # EDINET の XBRL CSV は UTF-16 LE (BOM付き) + タブ区切りの場合がある
            content = raw.decode("utf-16", errors="replace")
            df = pd.read_csv(io.StringIO(content), sep="\t", low_memory=False)
        return df
    except zipfile.BadZipFile:
        log.warning(f"ZIPエラー: {doc_id}")
        return None
    except Exception as e:
        log.warning(f"XBRL取得失敗 {doc_id}: {e}")
        return None


def _detect_xbrl_columns(df) -> dict:
    """XBRL CSV の列名マップ {element, context, value} を検出して返す"""
    col_map = {}
    for c in df.columns:
        lc = c.lower()
        if "要素" in c or "element" in lc:          col_map["element"] = c
        elif "コンテキスト" in c or "context" in lc: col_map["context"] = c
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


def parse_raw_rows(rows: list) -> dict:
    """[{element, context, value}, ...] から {bs, pl, cf, meta} を抽出（再解析用）"""
    result = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "meta": {}}
    _priority: dict = {}
    for row in rows:
        elem = row.get("element", "")
        category_field = XBRL_MAP.get(elem)
        if not category_field:
            continue
        cat, field = category_field
        ctx = row.get("context", "")
        if "Prior" in ctx and "CurrentYear" not in ctx:
            continue
        is_consol  = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
        has_member = "_Member" in ctx or "NonConsolidated" in ctx
        priority   = 2 if is_consol else (1 if not has_member else 0)
        try:
            val = float(str(row.get("value", "")).replace(",", ""))
        except (ValueError, TypeError):
            continue
        if cat == "meta":
            code_str = str(int(val)).zfill(4)
            result["meta"]["tse_industry_code"] = code_str
            result["meta"]["industry_name"] = TSE_INDUSTRY.get(code_str, "")
        else:
            key = f"{cat}_{field}"
            if priority > _priority.get(key, -1):
                result[cat][field] = val
                _priority[key] = priority
    return result


def parse_xbrl_csv(df, edinet_code: str, period_end: str) -> dict:
    result = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "meta": {}}
    if df is None or df.empty:
        return result
    df.columns = [c.strip() for c in df.columns]
    col_map = _detect_xbrl_columns(df)
    if not {"element", "value"}.issubset(col_map):
        return result

    # 優先度管理: 連結(2) > 非メンバー非連結(1) > メンバー付き非連結(0)
    # これにより CSV 内の行順序に依存せず正しい値を採用できる
    _priority: dict = {}

    for _, row in df.iterrows():
        raw_elem = str(row[col_map["element"]])
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        category_field = XBRL_MAP.get(elem)
        if not category_field:
            continue
        cat, field = category_field
        ctx = str(row.get(col_map.get("context", ""), ""))

        # 前期比較データ（Prior1Year等）はスキップ。当期データのみ処理する
        if "Prior" in ctx and "CurrentYear" not in ctx:
            continue

        is_consol  = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
        has_member = "_Member" in ctx or "NonConsolidated" in ctx
        priority   = 2 if is_consol else (1 if not has_member else 0)

        val_raw = row[col_map["value"]]
        try:
            val = float(str(val_raw).replace(",", ""))
        except (ValueError, TypeError):
            continue

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
    from database import StockPriceHistory
    from sqlalchemy import func as sqlfunc
    from sqlalchemy.dialects.postgresql import insert as pg_insert

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
        if backfill:
            minmax_dates = {
                row.edinet_code: (row.min_date, row.max_date)
                for row in db.query(
                    StockPriceHistory.edinet_code,
                    sqlfunc.min(StockPriceHistory.trade_date).label("min_date"),
                    sqlfunc.max(StockPriceHistory.trade_date).label("max_date"),
                ).group_by(StockPriceHistory.edinet_code).all()
            }
        else:
            latest_dates = dict(
                db.query(StockPriceHistory.edinet_code, sqlfunc.max(StockPriceHistory.trade_date))
                .group_by(StockPriceHistory.edinet_code)
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
                    stmt = pg_insert(StockPriceHistory).values([
                        {"edinet_code": edinet_code, "sec_code": sec_code, **r}
                        for r in rows
                    ]).on_conflict_do_nothing(constraint="uq_sph_edinet_date")
                    result = db.execute(stmt)
                    db.commit()
                    inserted_total += result.rowcount
            except Exception as e:
                log.warning(f"株価履歴保存失敗 {sec_code}: {e}")

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
    on_progress: Optional[Callable] = None,
    cancel_check: Optional[Callable] = None,
) -> dict:
    """J-Quants API から日次 OHLCV を日付単位で取得し ON CONFLICT UPDATE で保存する。
    J-Quants は JPX 公式データのため、stooq 由来レコードより優先して上書きする。
    1回のリクエストで全銘柄のデータが取得できるため stooq より大幅に高速。
    """
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise ValueError("環境変数 JQUANTS_API_KEY が未設定です")

    from database import StockPriceHistory, Company as _Company
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today     = date.today()
    date_from = today - timedelta(days=days_back)
    # 土日は J-Quants も空を返すのでスキップして API コール数を削減
    dates = [
        (date_from + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days_back + 1)
        if (date_from + timedelta(days=i)).weekday() < 5
        and (date_from + timedelta(days=i)) <= today
    ]

    # sec_code (4桁) → edinet_code のルックアップ（全社一括で1回のみ）
    sec_to_edinet: dict = {
        row.sec_code: row.edinet_code
        for row in db.query(_Company.sec_code, _Company.edinet_code)
        .filter(_Company.sec_code.isnot(None))
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
                ins = pg_insert(StockPriceHistory).values(records)
                stmt = ins.on_conflict_do_update(
                    constraint="uq_sph_edinet_date",
                    set_={
                        "open":   ins.excluded.open,
                        "high":   ins.excluded.high,
                        "low":    ins.excluded.low,
                        "close":  ins.excluded.close,
                        "volume": ins.excluded.volume,
                    },
                )
                result = db.execute(stmt)
                db.commit()
                upserted_total += result.rowcount

            if on_progress:
                on_progress(completed, total,
                            f"[{completed}/{total}] {date_str} {len(records)}件")

    if on_progress:
        on_progress(total, total, f"[完了] {total}日処理・{upserted_total}件追加/更新")
    return {"cancelled": False, "upserted": upserted_total, "days": total}


async def update_market_data(max_companies: Optional[int] = None,
                             on_progress: Optional[Callable] = None,
                             cancel_check: Optional[Callable] = None):
    """
    全企業の最新財務レコードに株価・バリュエーション指標を書き込む。
    株価: stooq API
    time市価総額: stock_price × (bs_total_equity / bs_bps)
    PER: stock_price / pl_eps
    PBR: stock_price / bs_bps
    """
    db = SessionLocal()
    try:
        companies = (db.query(Company)
                     .filter(Company.sec_code.isnot(None))
                     .filter(Company.sec_code != "")
                     .all())
        if max_companies:
            companies = companies[:max_companies]

        total = len(companies)
        updated = 0
        log.info(f"市場データ更新開始: {total}社")

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

                latest = (db.query(FinancialRecord)
                          .filter_by(edinet_code=company.edinet_code)
                          .order_by(FinancialRecord.year.desc())
                          .first())
                if not latest:
                    continue

                latest.stock_price = price

                if latest.pl_eps and latest.pl_eps > 0:
                    latest.per = round(price / latest.pl_eps, 2)
                if latest.bs_bps and latest.bs_bps > 0:
                    latest.pbr = round(price / latest.bs_bps, 2)
                if (latest.bs_bps and latest.bs_bps > 0
                        and latest.bs_total_equity and latest.bs_total_equity > 0):
                    shares = latest.bs_total_equity / latest.bs_bps
                    latest.market_cap = round(price * shares / 1_000_000, 2)
                if latest.dps and latest.dps > 0 and price > 0:
                    latest.div_yield = round(latest.dps / price * 100, 2)
                # ネットキャッシュ比率 = net_cash[円] / (market_cap[百万円] × 1e6)
                # 清原氏の銘柄選別基準: nc_ratio > 1.0 で「時価総額を上回るネットキャッシュ」
                if (latest.net_cash is not None
                        and latest.market_cap and latest.market_cap > 0):
                    latest.nc_ratio = round(
                        latest.net_cash / (latest.market_cap * 1_000_000), 4
                    )

                updated += 1
                if updated % 50 == 0:
                    db.commit()
                    log.info(f"市場データ更新中: {updated}社完了")

        db.commit()
        log.info(f"市場データ更新完了: {updated}/{total}社")
    finally:
        db.close()
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

async def run_full_collection(years_back: int = 5,
                              max_companies: Optional[int] = None,
                              on_progress: Optional[Callable] = None,
                              skip_existing: bool = False,
                              skip_if_raw_exists: bool = False,
                              cancel_check: Optional[Callable] = None):
    db = SessionLocal()
    try:
        async with httpx.AsyncClient() as client:
            companies_df = await fetch_edinet_code_list(client)

            # 企業マスタは全件DBに保存（max_companiesは収集書類数の上限であり企業マスタは絞らない）
            df_master = companies_df

            master_total = len(df_master)
            log.info(f"企業マスタをDBに保存中... ({master_total}社)")
            if on_progress:
                on_progress(0, master_total, f"[企業マスタ保存] {master_total}社をDBに登録中...")
            for i, (_, row) in enumerate(df_master.iterrows()):
                upsert_company(db, {
                    "edinet_code":  row["edinet_code"],
                    "sec_code":     row.get("sec_code", ""),
                    "name":         row["company_name"],
                    "industry":     row.get("industry", ""),
                    "fiscal_month": int(row["fiscal_month"]) if str(row.get("fiscal_month", "")).isdigit() else None,
                })
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
                    if xbrl_df is not None and not xbrl_df.empty:
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
                    log.error(f"書類処理エラー（スキップ）: {doc_id} {filer_name} — {e}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

            db.commit()
            log.info(f"全収集完了（スキップ: {skipped}件 / 取得: {total - skipped}件）")

            # XBRL から業種が取れないため、JPX上場会社一覧から業種を補完する
            await update_industry_from_jpx(client, db, on_progress=on_progress)
    finally:
        db.close()
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
    {"code": "USDJPY",    "name": "USD/JPY",      "category": "fx",        "ticker": "usdjpy"},
    {"code": "EURJPY",    "name": "EUR/JPY",      "category": "fx",        "ticker": "eurjpy"},
    {"code": "US10Y",     "name": "米10年金利",   "category": "rate",      "ticker": "10usy.b"},
    {"code": "JP10Y",     "name": "日10年金利",   "category": "rate",      "ticker": "10jpy.b"},
    {"code": "NIKKEI225", "name": "日経225",      "category": "equity",    "ticker": "^nkx"},
    {"code": "TOPIX",     "name": "TOPIX",        "category": "equity",    "ticker": "^tpx"},
    {"code": "SP500",     "name": "S&P500",       "category": "equity",    "ticker": "^spx"},
    {"code": "WTI",       "name": "WTI原油",      "category": "commodity", "ticker": "cl.f"},
    {"code": "GOLD",      "name": "金",           "category": "commodity", "ticker": "gc.f"},
]


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
    """MACRO_SERIES の全系列について stooq から日次データを取得し macro_data に upsert する。
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

            if on_progress:
                on_progress(i-1, total, f"[マクロ {i}/{total}] {series['name']} ({series['ticker']}) 取得中")

            rows = await fetch_stooq_history(session, series["ticker"], d1, d2)
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
        asyncio.run(update_market_data(args.max))
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
        asyncio.run(run_full_collection(args.years, args.max, skip_existing=args.incremental))
