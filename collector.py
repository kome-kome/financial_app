"""
collector.py — オーケストレータ（後方互換 re-export 層）

実装は各ドメインモジュールに分離:
  collector_utils.py      — 純粋関数・低レベルヘルパー
  collector_master.py     — EDINETコードリスト・JPX業種マスタ
  collector_prices.py     — 株価収集・市場データ更新
  collector_financials.py — XBRL財務収集・CF補完・reparse
"""

import asyncio
import os
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from collector_utils import (
    MAX_GAP_DAYS,
    TSE_INDUSTRY,
    XBRL_MAP,
    CONSOLIDATED_KEYS,
    _INVENTORY_GROUPS,
    _INVENTORY_SUB_ELEMS,
    CAPEX_LABEL_INCLUDE,
    CAPEX_LABEL_REQUIRE,
    CAPEX_LABEL_EXCLUDE,
    _bisect_left,
    _match_capex_by_label,
    _detect_xbrl_columns,
    df_to_raw_rows,
    _apply_row,
    _inventory_fallback,
    parse_raw_rows,
    parse_xbrl_csv,
    calc_derived,
    _apply_price_to_record,
    _fetch_latest_fin_by_ec,
)

from collector_master import (
    EDINET_BASE,
    JPX_EXCEL_URL,
    API_KEY,
    RATE_SLEEP,
    fetch_edinet_code_list,
    update_industry_from_jpx,
)

from collector_prices import (
    STOOQ_CONCURRENCY,
    STOOQ_HIST_CONCURRENCY,
    JQUANTS_ENDPOINT,
    JQUANTS_RATE_SLEEP,
    JQUANTS_BACKFILL_DAYS,
    YAHOO_STOCK_RATE_SLEEP,
    MACRO_SERIES,
    fetch_stock_price_stooq,
    fetch_stock_history_stooq,
    _price_collection_driver,
    collect_stock_price_history,
    _jquants_fetch_date,
    collect_stock_price_history_jquants,
    _update_market_data_latest,
    _update_market_data_point_in_time,
    update_market_data_from_history,
    backfill_historical_stock_prices_yahoo,
    fill_recent_stock_price_gap_yahoo,
    update_market_data,
    fetch_yahoo_history,
    fetch_stooq_history,
    collect_macro_data,
)

from collector_financials import (
    BATCH_PAUSE,
    SKIP_XBRL_RAW,
    fetch_doc_list,
    collect_doc_ids_for_period,
    fetch_xbrl_csv,
    refill_cf_from_xbrl,
    refill_pl_bs_from_xbrl,
    diagnose_cf_labels,
    reparse_from_raw,
    _phase_upsert_master,
    _phase_build_skip_ids,
    _phase_process_docs,
    run_full_collection,
    refresh_company,
)

from database import SessionLocal, latest_prices, record_prices_batch, trim_daily


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
    parser.add_argument("--refill-pl-bs", action="store_true", help="pl_pretax 等 NULL の PL/BS 列を EDINET 再取得で補完")
    parser.add_argument("--sleep",       type=float, default=RATE_SLEEP, help="EDINET リクエスト間隔（秒・--refill-pl-bs 用）")
    args = parser.parse_args()

    if args.reparse:
        asyncio.run(reparse_from_raw(
            year=args.year,
            edinet_code=args.company,
            on_progress=lambda c, t, m: print(m),
        ))
    elif args.refill_pl_bs:
        async def _refill_pl_bs():
            db = SessionLocal()
            try:
                r = await refill_pl_bs_from_xbrl(
                    db, limit=args.max, sleep_sec=args.sleep,
                    on_progress=lambda c, t, m: print(m),
                )
                print(r)
            finally:
                db.close()
        asyncio.run(_refill_pl_bs())
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
