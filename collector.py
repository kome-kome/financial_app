"""EDINET 全上場企業 財務データ収集・正規化エンジン（オーケストレータ）。

実体は責務別モジュールへ分割済み。後方互換のため本モジュールから全シンボルを
再エクスポートする（`from collector import X` / `collector.X` は従来どおり利用可能）:

  - collector_utils.py      : 共通設定定数・ロガー
  - collector_master.py     : 企業/業種マスタ収集（EDINET コードリスト / JPX 業種）
  - collector_financials.py : XBRL 財務収集・パース・CF / PL-BS 補完・再解析
  - collector_prices.py     : 株価（stooq / J-Quants / Yahoo）・マクロ指標収集
  - collector_disclosures.py: 会社予想開示（J-Quants /fins/summary・Issue #322）
  - collector_interim.py    : 半期(H1)財務収集（EDINET 半期/旧四半期Q2・Issue #219②）

CLI エントリポイント（python collector.py ...）は本モジュールに残す。
"""
import asyncio

from collector_utils import *            # 設定定数・log
from collector_master import *           # 企業/業種マスタ
from collector_financials import *       # 財務収集・パース・補完
from collector_prices import *           # 株価・マクロ
from collector_disclosures import *      # 会社予想開示
from collector_interim import run_interim_collection  # 半期(H1)財務収集

# テスト等が `from collector import _name` で参照する非公開名は明示的に再エクスポートする
# （`from module import *` は先頭 _ の名前を取り込まないため）。
from collector_master import _read_jpx_excel
from collector_financials import _match_capex_by_label, _phase_process_docs, _detect_xbrl_columns
from collector_prices import _nearest_price, _jquants_fetch_date
from collector_prices import _esri_candidate_urls, _parse_esri_gdp_csv, _esri_apply_lag
from collector_prices import _parse_imf_weo_sheet
from database import SessionLocal


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EDINET全上場企業収集")
    parser.add_argument("--years",       type=int, default=5)
    parser.add_argument("--max",         type=int, default=None)
    parser.add_argument("--company",     type=str, default=None)
    parser.add_argument("--market",      action="store_true", help="市場データのみ更新")
    parser.add_argument("--macro",       action="store_true", help="マクロデータのみ収集")
    parser.add_argument("--disclosures", action="store_true", help="会社予想開示（決算短信サマリー）のみ収集")
    parser.add_argument("--interim",     action="store_true", help="半期(H1)財務のみ収集（EDINET 半期報告書・旧四半期Q2・Issue #219②）")
    parser.add_argument("--incremental", action="store_true", help="収集済みをスキップ（差分収集）")
    parser.add_argument("--reparse",     action="store_true", help="xbrl_raw_documents から financial_records を再構築")
    parser.add_argument("--year",        type=int, default=None, help="再解析対象年度（--reparse と組み合わせ）")
    parser.add_argument("--refill-pl-bs",    action="store_true", help="pl_pretax 等 NULL の PL/BS 列を EDINET 再取得で補完（タグ修正後の既存データ是正）")
    parser.add_argument("--refill-machinery", action="store_true", help="bs_machinery NULL（かつ bs_ppe_total あり）を EDINET 再取得で補完（MachineryAndVehiclesNet タグ追加後の是正）")
    parser.add_argument("--sleep",           type=float, default=RATE_SLEEP, help="EDINET リクエスト間隔（秒・--refill-* 用）")
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
    elif args.refill_machinery:
        async def _refill_machinery():
            db = SessionLocal()
            try:
                r = await refill_machinery_from_xbrl(
                    db, limit=args.max, sleep_sec=args.sleep,
                    on_progress=lambda c, t, m: print(m),
                )
                print(r)
            finally:
                db.close()
        asyncio.run(_refill_machinery())
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
    elif args.disclosures:
        async def _disclosures():
            db = SessionLocal()
            try:
                r = await collect_statement_disclosures(db, on_progress=lambda c, t, m: print(m))
                print(r)
            finally:
                db.close()
        asyncio.run(_disclosures())
    elif args.interim:
        async def _interim():
            db = SessionLocal()
            try:
                # 半期収集は常に差分（収集済み doc_id を再取得しない＝再DL無駄回避・冪等）。
                # 遡及年数は --years で指定（既定は run_interim_collection 側の 6 年）。
                r = await run_interim_collection(
                    db, years_back=args.years,
                    skip_existing=True,
                    on_progress=lambda c, t, m: print(m))
                print(r)
            finally:
                db.close()
        asyncio.run(_interim())
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
