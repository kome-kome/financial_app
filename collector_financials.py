"""XBRL 財務データの収集・パース・正規化、および CF / PL-BS 補完・再解析。"""
import bisect
import calendar
import io
import traceback
import zipfile
import asyncio
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional, Callable

import httpx
import pandas as pd
from sqlalchemy import func as sqla_func, extract
from sqlalchemy.exc import SQLAlchemyError

from database import (
    SessionLocal, Company, FinancialRecord, MacroData,
    XbrlRawDocument, upsert_company, upsert_financial,
    upsert_xbrl_raw, pack_elements, unpack_elements,
    build_xbrl_map, _parse_period_end,
    StockPriceDaily, StockPriceWeekly,
    record_prices_batch, trim_daily, latest_prices,
)

from collector_utils import *
from collector_master import fetch_edinet_code_list, update_industry_from_jpx


# ── XBRL パース用ドメイン定数 ────────────────────────────────────────────
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

# 棚卸資産サブ項目 — aggregate Inventories 要素を出さない会社（JGAAP の ~92%）向けフォールバック。
# 各タプルは precedence 順（先頭が取れたら後続をスキップして二重計上を防ぐ）。
_INVENTORY_GROUPS: tuple = (
    ("MerchandiseAndFinishedGoods", "Merchandise", "FinishedGoods"),  # 商品及び製品
    ("WorkInProcess",),                                                # 仕掛品
    ("RawMaterialsAndSupplies", "RawMaterials", "Supplies"),          # 原材料及び貯蔵品
    ("OtherInventories",),                                             # その他の棚卸資産
)
_INVENTORY_SUB_ELEMS: frozenset = frozenset(e for g in _INVENTORY_GROUPS for e in g)

# ── capex（設備投資）のラベル照合 ─────────────────────────────────────────
# 設備投資の CF 明細行は企業独自の拡張要素IDでタグ付けされることが多く、要素ID照合では
# 捕捉できない（実証: 標準要素 PurchaseOfPropertyPlantAndEquipment は0件）。
# EDINET CSV の「項目名」列（日本語標準ラベル）で照合することで拡張要素も捕捉する。
# INCLUDE 条件（いずれかを含む）かつ EXCLUDE 条件（いずれも含まない）で判定する。
CAPEX_LABEL_INCLUDE = ["取得による支出", "購入による支出"]   # 取得＝支出（アウトフロー）
CAPEX_LABEL_REQUIRE = ["有形固定資産"]                         # 有形固定資産を必ず含む（無形のみ除外、有形及び無形は可）
CAPEX_LABEL_EXCLUDE = ["売却", "収入", "減少"]                 # 売却収入・回収は capex ではない


def _match_capex_by_label(label) -> bool:
    """項目名（日本語ラベル）が設備投資（有形固定資産の取得による支出）に該当するか判定。

    例: "有形固定資産の取得による支出" → True
        "有形固定資産及び無形固定資産の取得による支出" → True
        "有形固定資産の売却による収入" → False（売却収入）
        "無形固定資産の取得による支出" → False（有形を含まない）
    """
    if not isinstance(label, str) or not label:  # NaN(float) は str でないため早期リターン
        return False
    if not any(kw in label for kw in CAPEX_LABEL_REQUIRE):
        return False
    if not any(kw in label for kw in CAPEX_LABEL_INCLUDE):
        return False
    if any(kw in label for kw in CAPEX_LABEL_EXCLUDE):
        return False
    return True


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


def _detect_xbrl_columns(df) -> dict:
    """XBRL CSV の列名マップ {element, context, value, label} を検出して返す"""
    col_map = {}
    for c in df.columns:
        if not isinstance(c, str):  # pd.concat 後に NaN 列名が混入する場合に備えたガード
            continue
        lc = c.lower()
        if "要素" in c or "element" in lc:          col_map["element"] = c
        elif "コンテキスト" in c or "context" in lc: col_map["context"] = c
        elif "項目名" in c or "label" in lc:          col_map["label"] = c   # 日本語標準ラベル（capex 照合用）
        elif "値" in c and "id" not in lc:            col_map["value"] = c
    return col_map


def _col_as_str_list(df, col) -> list:
    """列を文字列リスト化する。NaN は空文字に正規化する。

    pandas 3.0 から `Series.astype(str)` は NaN を文字列 "nan" へ変換せず float の
    まま残すため、`.astype(str).tolist()` の結果に float NaN が混入する。これが
    後段の `"Prior" in ctx` / `kw in label` / `elem.split(":")` を TypeError で
    落とす（C2 補完で全件失敗の原因）。fillna("") で NaN を先に潰してから変換する。
    """
    return df[col].fillna("").astype(str).tolist()


def df_to_raw_rows(df) -> list:
    """XBRL CSV DataFrame を [{element, context, value}, ...] に変換（raw 保存用）"""
    col_map = _detect_xbrl_columns(df)
    if not {"element", "value"}.issubset(col_map):
        return []
    # iterrows() は1行ごとに Series を生成して遅い。列を一括で list 化し zip で回す
    # （1書類あたり数千〜数万行になりうる収集ホットパス）。
    ctx_col  = col_map.get("context")
    elements = _col_as_str_list(df, col_map["element"])
    values   = _col_as_str_list(df, col_map["value"])
    contexts = (_col_as_str_list(df, ctx_col)
                if ctx_col and ctx_col in df.columns else [""] * len(df))
    rows = []
    for raw_elem, ctx, val in zip(elements, contexts, values):
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        rows.append({"element": elem, "context": ctx, "value": val})
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


def _inventory_fallback(inv_parts: dict, result: dict) -> None:
    """aggregate Inventories 未取得時にサブ項目の合計を bs["inventory"] へ設定する。"""
    if result["bs"].get("inventory") is not None or not inv_parts:
        return
    total = 0.0
    found = False
    for group in _INVENTORY_GROUPS:
        for elem in group:
            if elem in inv_parts:
                total += inv_parts[elem]
                found = True
                break
    if found:
        result["bs"]["inventory"] = total


def _collect_inventory_row(elem: str, ctx: str, raw_value, inv_parts: dict, inv_prio: dict) -> None:
    """棚卸資産サブ項目を連結優先度付きで inv_parts へ集約する（in-place）。

    parse_raw_rows / parse_xbrl_csv 共通。Prior 期（CurrentYear を含まない）は除外し、
    連結 > 単体(メンバー無し) > メンバー有り の優先度が高い値で上書きする。
    パース不能な値は無視する。
    """
    if "Prior" in ctx and "CurrentYear" not in ctx:
        return
    is_consol = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
    has_member = "Member" in ctx or "NonConsolidated" in ctx
    prio = 2 if is_consol else (1 if not has_member else 0)
    try:
        val = float(str(raw_value).replace(",", ""))
    except (ValueError, TypeError):
        return
    if prio > inv_prio.get(elem, -1):
        inv_parts[elem] = val
        inv_prio[elem] = prio


def parse_raw_rows(rows: list) -> dict:
    """[{element, context, value}, ...] から {bs, pl, cf, val, nonfin, meta} を抽出（再解析用）"""
    result = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "nonfin": {}, "meta": {}}
    _priority: dict = {}
    _inv_parts: dict = {}
    _inv_prio: dict = {}
    for row in rows:
        elem = row.get("element", "")
        category_field = XBRL_MAP.get(elem)
        if not category_field:
            if elem in _INVENTORY_SUB_ELEMS:
                _collect_inventory_row(
                    elem, row.get("context", ""), row.get("value", ""),
                    _inv_parts, _inv_prio,
                )
            continue
        cat, field = category_field
        ctx = row.get("context", "")
        _apply_row(elem, ctx, row.get("value", ""), cat, field, result, _priority)
    _inventory_fallback(_inv_parts, result)
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
    _inv_parts: dict = {}
    _inv_prio: dict = {}
    label_col = col_map.get("label")

    # iterrows() は1行ごとに Series を生成して遅い。列を一括で list 化し zip で回す
    # （1書類あたり数千〜数万行になりうる収集ホットパス）。value は生のまま渡し、
    # 後段の _apply_row / _collect_inventory_row が float 変換する。
    ctx_col  = col_map.get("context")
    elements = _col_as_str_list(df, col_map["element"])
    values   = df[col_map["value"]].tolist()
    contexts = (_col_as_str_list(df, ctx_col)
                if ctx_col and ctx_col in df.columns else [""] * len(df))
    labels   = (_col_as_str_list(df, label_col)
                if label_col is not None and label_col in df.columns else [""] * len(df))

    for raw_elem, ctx, val, label in zip(elements, contexts, values, labels):
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        category_field = XBRL_MAP.get(elem)

        # 要素ID照合で外れた行は capex のラベル照合、次に棚卸資産サブ項目照合を試みる。
        if not category_field:
            if label_col is not None and _match_capex_by_label(label):
                category_field = ("cf", "capex")
            elif elem in _INVENTORY_SUB_ELEMS:
                _collect_inventory_row(elem, ctx, val, _inv_parts, _inv_prio)
                continue
            else:
                continue

        cat, field = category_field
        _apply_row(elem, ctx, val, cat, field, result, _priority, apply_capex_sign=True)

    _inventory_fallback(_inv_parts, result)
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


async def _refill_records_from_xbrl(db, target_q, field_updater, *, label: str,
                                    limit: Optional[int] = None, defer_raw: bool = False,
                                    sleep_sec: float = RATE_SLEEP, order: str = "desc",
                                    on_progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """EDINET XBRL 再取得で NULL 列を補完する共通骨格（CF / PL・BS の refill が共用）。

    `target_q()` は対象レコードのフィルタ済みクエリを返す callable（targets 取得と
    remaining 集計の両方で呼ぶ）。列ごとの補完ロジックは `field_updater(rec, parsed) -> bool`
    に注入し、何か書き込めば True（=更新扱い）を返す。骨格（httpx セッション・enumerate
    ループ・on_progress・fetch→parse・100件ごと commit・例外処理・sleep）は共通。

    `order` は period_end の処理順（"desc"=新しい順 / "asc"=古い順）。limit 付き／途中
    中断（タイムアウト等）でも本命コホートを先に処理したい補完では "asc" を指定する。
    """
    period_order = FinancialRecord.period_end.asc() if order == "asc" else FinancialRecord.period_end.desc()
    q = target_q().order_by(period_order)
    if defer_raw:
        from sqlalchemy.orm import defer
        # raw_xbrl_json は使わないので defer（全件ロード時の転送/メモリ削減）
        q = q.options(defer(FinancialRecord.raw_xbrl_json))
    if limit:
        q = q.limit(limit)
    targets = q.all()

    total = len(targets)
    log.info(f"  対象レコード: {total}件")
    if total == 0:
        return {"updated": 0, "skipped": 0, "failed": 0, "remaining": 0}

    updated = skipped = failed = 0

    async with httpx.AsyncClient(timeout=60) as client:
        for i, rec in enumerate(targets, 1):
            msg = f"[{label} {i}/{total}] {rec.company_name} {rec.period_end}"
            if on_progress:
                on_progress(i, total, msg)
            if i % PROGRESS_LOG_BATCH == 0:
                log.info(msg)

            try:
                df = await fetch_xbrl_csv(client, rec.doc_id)
                if df is None or df.empty:
                    skipped += 1
                    continue

                pe = rec.period_end
                period_end_str = pe.isoformat() if hasattr(pe, "isoformat") else str(pe) if pe else ""
                parsed = parse_xbrl_csv(df, rec.edinet_code, period_end_str)
                if field_updater(rec, parsed):
                    db.add(rec)
                    updated += 1
                else:
                    skipped += 1

                # 100件ごとに commit（長時間トランザクション対策）
                if updated % REPARSE_COMMIT_BATCH == 0 and updated > 0:
                    db.commit()

            except Exception as e:
                log.warning(f"  {label}失敗 {rec.edinet_code} {rec.doc_id}: {e}\n{traceback.format_exc()}")
                failed += 1

            await asyncio.sleep(sleep_sec)

    db.commit()

    # 残件数を集計（スケジュール実行の終了判定に使う）
    remaining = target_q().count()
    result = {"updated": updated, "skipped": skipped, "failed": failed, "remaining": remaining}
    log.info(f"{label} 完了: {result}")
    return result


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

    def _cf_updater(rec, parsed) -> bool:
        cf = parsed.get("cf", {})
        if not cf:
            return False
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
        return changed

    return await _refill_records_from_xbrl(
        db, _target_q, _cf_updater, label="CF補完",
        limit=limit, sleep_sec=sleep_sec, on_progress=on_progress,
    )


async def refill_pl_bs_from_xbrl(
    db,
    limit: Optional[int] = None,
    sleep_sec: float = RATE_SLEEP,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """`bs_inventory` が NULL のレコードを EDINET XBRL から再取得し、
    NULL の PL/BS 列を一括補完する（既存値は上書きしない）。

    駆動マーカー = `bs_inventory IS NULL` かつ `doc_id IS NOT NULL`。棚卸資産が埋まると
    対象から外れるため、繰り返し実行で自然終了する（refill_cf_from_xbrl と同じ思想）。
    注意: 棚卸資産を持たない企業（金融・サービス等）は何度実行しても埋まらないため
    永続的な少数残件として残る（無害）。

    処理順は `order="asc"`（period_end 古い順）。NULL は主にパーサ修正前に収集した
    古いコホート（旧年度）に集中するため、古い順に処理することで limit 付き／タイムアウト
    時も本命の旧年度に着実に前進・再開できる（新しい順だと直近の正当 NULL=金融等で
    limit を浪費し旧年度に届かない）。

    タグ修正・パースロジック変更後の既存データ是正用。`raw_xbrl_json`
    は生タグを保存しないため `reparse_from_raw` では復元できず再フェッチが必須。値は
    parse_xbrl_csv の連結優先ロジックで選ばれるため通常収集と同等の信頼性。CF は
    refill_cf_from_xbrl が担当するため対象外。
    """
    log.info(f"refill_pl_bs_from_xbrl 開始 (limit={limit}, sleep={sleep_sec})")

    def _target_q():
        return db.query(FinancialRecord).filter(
            FinancialRecord.bs_inventory.is_(None),
            FinancialRecord.doc_id.isnot(None),
        )

    def _pl_bs_updater(rec, parsed) -> bool:
        changed = False
        # NULL の PL/BS 列のみ書き込む（既存値を保護＝上書き禁止）
        for cat in ("pl", "bs"):
            for field, val in (parsed.get(cat) or {}).items():
                col = f"{cat}_{field}"
                if val is not None and hasattr(rec, col) and getattr(rec, col) is None:
                    setattr(rec, col, val)
                    changed = True
        return changed

    return await _refill_records_from_xbrl(
        db, _target_q, _pl_bs_updater, label="PL/BS補完",
        limit=limit, defer_raw=True, sleep_sec=sleep_sec, order="asc", on_progress=on_progress,
    )


async def refill_c2_from_xbrl(
    db,
    limit: Optional[int] = None,
    sleep_sec: float = RATE_SLEEP,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """`pl_depreciation` が NULL のレコードを EDINET XBRL から再取得し、
    C2 追加列（PL/BS/nonfin）を一括補完する（既存値は上書きしない）。

    駆動マーカー = `pl_depreciation IS NULL` かつ `doc_id IS NOT NULL`。
    C2 対象列: pl_depreciation / pl_rd_expenses / pl_extraordinary_income / pl_extraordinary_loss
               bs_ppe_total / bs_investments_other_assets / employees / issued_shares

    nonfin 列（employees / issued_shares）はプレフィックスなしで直接列にマップする。
    金融・サービス等で正当に pl_depreciation が取れない少数残件は永続的に残る（無害）。
    """
    log.info(f"refill_c2_from_xbrl 開始 (limit={limit}, sleep={sleep_sec})")

    def _target_q():
        return db.query(FinancialRecord).filter(
            FinancialRecord.pl_depreciation.is_(None),
            FinancialRecord.doc_id.isnot(None),
        )

    def _c2_updater(rec, parsed) -> bool:
        changed = False
        for cat in ("pl", "bs"):
            for field, val in (parsed.get(cat) or {}).items():
                col = f"{cat}_{field}"
                if val is not None and hasattr(rec, col) and getattr(rec, col) is None:
                    setattr(rec, col, val)
                    changed = True
        for field, val in (parsed.get("nonfin") or {}).items():
            if val is not None and hasattr(rec, field) and getattr(rec, field) is None:
                setattr(rec, field, val)
                changed = True
        return changed

    return await _refill_records_from_xbrl(
        db, _target_q, _c2_updater, label="C2補完",
        limit=limit, defer_raw=True, sleep_sec=sleep_sec, order="asc", on_progress=on_progress,
    )


async def refill_machinery_from_xbrl(
    db,
    limit: Optional[int] = None,
    sleep_sec: float = RATE_SLEEP,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """`bs_machinery` が NULL かつ `bs_ppe_total` が取得済みのレコードを EDINET XBRL
    から再取得し、bs_machinery を補完する（既存値は上書きしない）。

    駆動マーカー = `bs_machinery IS NULL AND bs_ppe_total IS NOT NULL AND doc_id IS NOT NULL`。
    MachineryAndVehiclesNet タグ追加後の既存データ是正用。金融・サービス等で機械装置を
    持たない企業は永続的に残るが無害（bs_ppe_total も NULL のため対象外）。
    """
    log.info(f"refill_machinery_from_xbrl 開始 (limit={limit}, sleep={sleep_sec})")

    def _target_q():
        return db.query(FinancialRecord).filter(
            FinancialRecord.bs_machinery.is_(None),
            FinancialRecord.bs_ppe_total.isnot(None),
            FinancialRecord.doc_id.isnot(None),
        )

    def _machinery_updater(rec, parsed) -> bool:
        changed = False
        for field, val in (parsed.get("bs") or {}).items():
            col = f"bs_{field}"
            if val is not None and hasattr(rec, col) and getattr(rec, col) is None:
                setattr(rec, col, val)
                changed = True
        return changed

    return await _refill_records_from_xbrl(
        db, _target_q, _machinery_updater, label="機械装置補完",
        limit=limit, defer_raw=True, sleep_sec=sleep_sec, order="asc", on_progress=on_progress,
    )


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
            q = q.filter(extract('year', XbrlRawDocument.period_end) == year)
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
                "year":         doc.period_end.year if doc.period_end else 0,
                "period_end":   doc.period_end.isoformat() if doc.period_end else "",
                "doc_id":       doc.doc_id,
                "source":       "EDINET_XBRL",
            })
            upsert_financial(db, rec)

            if on_progress:
                on_progress(i, total, f"[再解析 {i}/{total}] {doc.edinet_code} {doc.period_end}")
            if i % REPARSE_COMMIT_BATCH == 0:
                db.commit()
                log.info(f"再解析 commit ({i}/{total})")

        db.commit()
        log.info(f"再解析完了: {total} 書類")
    finally:
        db.close()
    return False


async def _phase_upsert_master(db, client, on_progress, max_companies) -> tuple:
    """Phase 1: EDINET から企業マスタを取得し DB に一括 Upsert する。
    (company_info, known_edinet, edinet_set) を返す。
    """
    companies_df = await fetch_edinet_code_list(client)
    master_total = len(companies_df)
    log.info(f"企業マスタをDBに保存中... ({master_total}社)")
    if on_progress:
        on_progress(0, master_total, f"[企業マスタ保存] {master_total}社をDBに登録中...")
    # iterrows() の二重走査を避け、to_dict('records') で1パスに統一
    # （upsert と company_info 構築を同一ループで行う）。
    records = companies_df.to_dict("records")
    company_info = {}
    # 3978 社の dirty を 1 トランザクションに溜めると Supabase が read-only
    # に切り替わるため、MASTER_COMMIT_BATCH 件ごとに commit + expire_all してトランザクションを短く保つ
    for i, row in enumerate(records):
        upsert_company(db, {
            "edinet_code":  row["edinet_code"],
            "sec_code":     row.get("sec_code", ""),
            "name":         row["company_name"],
            "industry":     row.get("industry", ""),
            "fiscal_month": int(row["fiscal_month"]) if str(row.get("fiscal_month", "")).isdigit() else None,
        })
        company_info[row["edinet_code"]] = {
            "company_name": row["company_name"],
            "industry":     row.get("industry", ""),
        }
        if (i + 1) % MASTER_COMMIT_BATCH == 0:
            db.commit()
            db.expire_all()
        if on_progress and (i + 1) % PROGRESS_REPORT_BATCH == 0:
            on_progress(i + 1, master_total, f"[企業マスタ保存] {i+1}/{master_total}社完了")
    db.commit()
    if on_progress:
        on_progress(master_total, master_total, f"[企業マスタ保存完了] {master_total}社")
    # 全件収集時は formCode=030000+secCode フィルタ（fetch_doc_list内）に委任し
    # 不完全なマスタリストで絞り込まない。max_companies 指定時のみ絞り込む。
    edinet_set = set(companies_df["edinet_code"].tolist()) if max_companies and not companies_df.empty else None
    known_edinet = set(company_info.keys())
    return company_info, known_edinet, edinet_set


def _phase_build_skip_ids(db, skip_existing: bool, skip_if_raw_exists: bool) -> set:
    """Phase 2: 差分スキップ対象の doc_id セットを DB から一括取得する。"""
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


def _safe_rollback(db) -> None:
    """rollback 失敗（二次例外）でも収集ループを止めないための安全 rollback。"""
    try:
        db.rollback()
    except Exception as rollback_err:
        log.error(f"rollback失敗: {rollback_err}")


async def _phase_process_docs(db, client, all_docs: list,
                               company_info: dict, known_edinet: set,
                               existing_doc_ids: set, skip_existing: bool,
                               on_progress, cancel_check) -> tuple:
    """Phase 4: XBRL 取得→パース→DB 保存のメインループ。(skipped, cancelled) を返す。"""
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

        # 失敗種別の切り分け用に現在のフェーズを追跡する（fetch / parse / db）。
        phase = "master"
        try:
            if edinet_code not in known_edinet:
                upsert_company(db, {
                    "edinet_code": edinet_code, "sec_code": sec_code,
                    "name": filer_name, "industry": "",
                })
                db.flush()
                known_edinet.add(edinet_code)

            phase = "fetch"
            xbrl_df = await fetch_xbrl_csv(client, doc_id)
            await asyncio.sleep(RATE_SLEEP)

            # XBRL 全行を raw テーブルに保存（新指標追加時の再解析用）
            # xbrl_raw_documents への書き込みは即コミットしてロックを解放する
            # （financial_records との間でデッドロックが起きるのを防ぐため）
            phase = "db"
            if not SKIP_XBRL_RAW and xbrl_df is not None and not xbrl_df.empty:
                raw_rows = df_to_raw_rows(xbrl_df)
                if raw_rows:
                    upsert_xbrl_raw(db, doc_id, edinet_code, period_end, raw_rows)
                    db.commit()

            phase = "parse"
            raw = parse_xbrl_csv(xbrl_df, edinet_code, period_end)
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
            phase = "db"
            upsert_financial(db, rec)

            if (i + 1) % COLLECT_COMMIT_BATCH == 0:
                db.commit()
                log.info(f"DB commit ({COLLECT_COMMIT_BATCH}件)")
            if (i + 1) % COLLECT_SLEEP_BATCH == 0:
                await asyncio.sleep(BATCH_PAUSE)

        # 1社の失敗で収集全体を止めないフェイルソフト方針（個社スキップ＋種別別ログ）。
        except httpx.HTTPError as e:
            log.error(f"[取得失敗 phase={phase}] {edinet_code}/{doc_id} {filer_name}: "
                      f"{e.__class__.__name__}: {e}")
            _safe_rollback(db)
        except SQLAlchemyError as e:
            log.error(f"[DB保存失敗 phase={phase}] {edinet_code}/{doc_id} {filer_name}: "
                      f"{e.__class__.__name__}: {e}", exc_info=True)
            _safe_rollback(db)
        except Exception as e:
            log.error(f"[パース/処理失敗 phase={phase}] {edinet_code}/{doc_id} {filer_name}: "
                      f"{e.__class__.__name__}: {e}", exc_info=True)
            _safe_rollback(db)

    return skipped, False


async def run_full_collection(db,
                              years_back: int = 5,
                              max_companies: Optional[int] = None,
                              on_progress: Optional[Callable] = None,
                              skip_existing: bool = False,
                              skip_if_raw_exists: bool = False,
                              cancel_check: Optional[Callable] = None):
    async with httpx.AsyncClient() as client:
        # Phase 1: 企業マスタ Upsert
        company_info, known_edinet, edinet_set = await _phase_upsert_master(
            db, client, on_progress, max_companies
        )

        # Phase 2: 差分スキップ集合の構築
        existing_doc_ids = _phase_build_skip_ids(db, skip_existing, skip_if_raw_exists)

        # Phase 3: 書類一覧スキャン
        today = date.today()
        start = date(today.year - years_back, 1, 1)
        end   = today - timedelta(days=1)
        log.info(f"書類一覧収集: {start} ~ {end}")
        if on_progress:
            on_progress(0, 1, f"[書類スキャン開始] {start} ～ {end}（{(end - start).days + 1}日分）")
        all_docs = await collect_doc_ids_for_period(
            client, start, end, edinet_set, max_companies=max_companies, on_progress=on_progress
        )
        log.info(f"対象書類数: {len(all_docs)}")

        # Phase 4: XBRL 取得 / パース / DB 保存
        skipped, cancelled = await _phase_process_docs(
            db, client, all_docs, company_info, known_edinet,
            existing_doc_ids, skip_existing, on_progress, cancel_check
        )
        if cancelled:
            return True
        db.commit()
        log.info(f"全収集完了（スキップ: {skipped}件 / 取得: {len(all_docs) - skipped}件）")

        # Phase 5: 業種補完（JPX 上場会社一覧から XBRL で取れない業種を補完）
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
