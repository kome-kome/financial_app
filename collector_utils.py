"""
collector_utils.py — 純粋関数・低レベルヘルパー（database.py のみインポート可）
"""

import bisect as _bisect_mod
import logging

from database import FinancialRecord, build_xbrl_map
from sqlalchemy import func as sqla_func

log = logging.getLogger(__name__)

MAX_GAP_DAYS = 30

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

XBRL_MAP = build_xbrl_map()

CONSOLIDATED_KEYS = ["Consolidated"]

_INVENTORY_GROUPS: tuple = (
    ("MerchandiseAndFinishedGoods", "Merchandise", "FinishedGoods"),
    ("WorkInProcess",),
    ("RawMaterialsAndSupplies", "RawMaterials", "Supplies"),
    ("OtherInventories",),
)
_INVENTORY_SUB_ELEMS: frozenset = frozenset(e for g in _INVENTORY_GROUPS for e in g)

CAPEX_LABEL_INCLUDE = ["取得による支出", "購入による支出"]
CAPEX_LABEL_REQUIRE = ["有形固定資産"]
CAPEX_LABEL_EXCLUDE = ["売却", "収入", "減少"]


def _bisect_left(a, x):
    return _bisect_mod.bisect_left(a, x)


def _match_capex_by_label(label: str) -> bool:
    if not label:
        return False
    if not any(kw in label for kw in CAPEX_LABEL_REQUIRE):
        return False
    if not any(kw in label for kw in CAPEX_LABEL_INCLUDE):
        return False
    if any(kw in label for kw in CAPEX_LABEL_EXCLUDE):
        return False
    return True


def _detect_xbrl_columns(df) -> dict:
    col_map = {}
    for c in df.columns:
        lc = c.lower()
        if "要素" in c or "element" in lc:          col_map["element"] = c
        elif "コンテキスト" in c or "context" in lc: col_map["context"] = c
        elif "項目名" in c or "label" in lc:          col_map["label"] = c
        elif "値" in c and "id" not in lc:            col_map["value"] = c
    return col_map


def df_to_raw_rows(df) -> list:
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
    if "Prior" in ctx and "CurrentYear" not in ctx:
        return
    if field == "revenue" and elem.startswith("OperatingRevenue1") and \
            ("NonConsolidated" in ctx or "_Member" in ctx):
        return
    is_consol  = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
    has_member = "Member" in ctx or "NonConsolidated" in ctx
    priority   = 2 if is_consol else (1 if not has_member else 0)
    try:
        val = float(str(val_raw).replace(",", ""))
    except (ValueError, TypeError):
        return
    if apply_capex_sign and field == "capex":
        val = -abs(val)
    if cat == "meta":
        code_str = str(int(val)).zfill(4)
        result["meta"]["tse_industry_code"] = code_str
        result["meta"]["industry_name"] = TSE_INDUSTRY.get(code_str, "")
    else:
        key = f"{cat}_{field}"
        if priority > _priority.get(key, -1):
            result[cat][field] = val
            _priority[key] = priority


def _inventory_fallback(inv_parts: dict, result: dict) -> None:
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


def parse_raw_rows(rows: list) -> dict:
    result = {"bs": {}, "pl": {}, "cf": {}, "val": {}, "nonfin": {}, "meta": {}}
    _priority: dict = {}
    _inv_parts: dict = {}
    _inv_prio: dict = {}
    for row in rows:
        elem = row.get("element", "")
        category_field = XBRL_MAP.get(elem)
        if not category_field:
            if elem in _INVENTORY_SUB_ELEMS:
                ctx = row.get("context", "")
                if "Prior" in ctx and "CurrentYear" not in ctx:
                    continue
                is_consol = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
                has_member = "Member" in ctx or "NonConsolidated" in ctx
                prio = 2 if is_consol else (1 if not has_member else 0)
                try:
                    val = float(str(row.get("value", "")).replace(",", ""))
                except (ValueError, TypeError):
                    pass
                else:
                    if prio > _inv_prio.get(elem, -1):
                        _inv_parts[elem] = val
                        _inv_prio[elem] = prio
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

    for _, row in df.iterrows():
        raw_elem = str(row[col_map["element"]])
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        category_field = XBRL_MAP.get(elem)

        if not category_field:
            if label_col is not None and _match_capex_by_label(str(row.get(label_col, ""))):
                category_field = ("cf", "capex")
            elif elem in _INVENTORY_SUB_ELEMS:
                ctx = str(row.get(col_map.get("context", ""), ""))
                if "Prior" in ctx and "CurrentYear" not in ctx:
                    continue
                is_consol = any(k in ctx for k in CONSOLIDATED_KEYS) and "NonConsolidated" not in ctx
                has_member = "Member" in ctx or "NonConsolidated" in ctx
                prio = 2 if is_consol else (1 if not has_member else 0)
                try:
                    val = float(str(row[col_map["value"]]).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if prio > _inv_prio.get(elem, -1):
                    _inv_parts[elem] = val
                    _inv_prio[elem] = prio
                continue
            else:
                continue

        cat, field = category_field
        ctx = str(row.get(col_map.get("context", ""), ""))
        _apply_row(elem, ctx, row[col_map["value"]], cat, field, result, _priority, apply_capex_sign=True)

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
    cf["free_cf"] = ocf + (cf.get("investing_cf", 0) or 0)
    if op and ord_p:
        pl["nonoperating_income"] = round(ord_p - op, 0)
    dep = pl.get("depreciation")
    if op and dep:
        pl["ebitda"] = round(op + dep, 0)
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


def _apply_price_to_record(rec, price: float) -> None:
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
