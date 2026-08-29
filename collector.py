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
from collector_prices import _nearest_price, _jquants_fetch_date, _jquants_fetch_code
from collector_prices import _esri_candidate_urls, _parse_esri_gdp_csv, _esri_apply_lag
from collector_prices import _parse_imf_weo_sheet
from database import SessionLocal


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EDINET全上場企業収集")
    parser.add_argument("--years",       type=int, default=5)
    parser.add_argument("--max",         type=int, default=None)
    parser.add_argument("--company",     type=str, default=None)
    parser.add_argument("--market",      action="store_true",
                        help="株価テーブル（J-Quants/Yahoo 由来）から financial_records の"
                             "株価・PER/PBR/時価総額を反映する（外部への株価取得はしない）")
    parser.add_argument("--macro",       action="store_true", help="マクロデータのみ収集")
    parser.add_argument("--macro-series", type=str, default=None,
                        help="--macro と併用し、指定 series_code（カンマ区切り）だけを収集する。"
                             "系列定義の是正に伴う再収集で全外部APIを叩き直さないための絞り込み（#444）")
    parser.add_argument("--disclosures", action="store_true", help="会社予想開示（決算短信サマリー）のみ収集")
    parser.add_argument("--interim",     action="store_true", help="半期(H1)財務のみ収集（EDINET 半期報告書・旧四半期Q2・Issue #219②）")
    parser.add_argument("--incremental", action="store_true", help="収集済みをスキップ（差分収集）")
    parser.add_argument("--reparse",     action="store_true", help="xbrl_raw_documents から financial_records を再構築")
    parser.add_argument("--year",        type=int, default=None, help="再解析対象年度（--reparse と組み合わせ）")
    parser.add_argument("--refill-pl-bs",    action="store_true", help="pl_pretax 等 NULL の PL/BS 列を EDINET 再取得で補完（タグ修正後の既存データ是正）")
    parser.add_argument("--refill-machinery", action="store_true", help="bs_machinery NULL（かつ bs_ppe_total あり）を EDINET 再取得で補完（MachineryAndVehiclesNet タグ追加後の是正）")
    parser.add_argument("--sleep",           type=float, default=RATE_SLEEP, help="EDINET リクエスト間隔（秒・--refill-* 用）")
    parser.add_argument("--repair-price-breaks", action="store_true",
                        help="週次株価の段差（株式分割の遡及調整もれ）を J-Quants 公式値と突合して"
                             "検出し、該当銘柄だけ Yahoo で取り直す（#465）。既定は dry-run")
    parser.add_argument("--persist", action="store_true",
                        help="--repair-price-breaks の結果を実際に書き込む（既定は検出のみ）")
    parser.add_argument("--probe-months", type=int, default=PRICE_BREAK_PROBE_MONTHS,
                        help="--repair-price-breaks で突合する月数（月あたり1営業日）")
    parser.add_argument("--break-threshold", type=float, default=PRICE_BREAK_THRESHOLD,
                        help="--repair-price-breaks で段差とみなす相対乖離（既定 0.01＝1%%）")
    parser.add_argument("--max-repair", type=int, default=PRICE_BREAK_MAX_REPAIR,
                        help="--repair-price-breaks で検出がこの件数を超えたら書かずに中止する")
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
    elif args.repair_price_breaks:
        async def _repair_breaks():
            db = SessionLocal()
            try:
                r = await repair_price_scale_breaks(
                    db, persist=args.persist, months=args.probe_months,
                    threshold=args.break_threshold, max_repair=args.max_repair,
                    on_progress=lambda c, t, m: print(m))
                mode = "書き込み済み" if r["persisted"] else "dry-run（書き込みなし）"
                print(f"\n=== 段差検出 {r['detected']}社 / 突合 {r['compared']}件 "
                      f"/ 突合日 {len(r['probe_dates'])}日 ― {mode} ===")
                for b in r["breaks"][:40]:
                    print(f"  {b['edinet_code']} {str(b.get('sec_code') or ''):5s} "
                          f"乖離{b['max_dev'] * 100:8.2f}%  JQ/DB {b['ratio']:.4f}  "
                          f"{b['hits']}日  {b.get('name', '')[:24]}")
                if len(r["breaks"]) > 40:
                    print(f"  ... 他 {len(r['breaks']) - 40}社")
                if r["aborted"]:
                    print(f"\n上限 {args.max_repair} 超過のため中止（書き込みなし）")
                elif r["persisted"]:
                    print(f"\n修復 {r['repaired']}社 / 失敗 {len(r['failed'])}社 "
                          f"/ 残存 {len(r['remaining'])}社 / 新規 {len(r['introduced'])}社")
                    for f in r["failed"]:
                        print(f"  [失敗] {f['edinet_code']}: {f['reason']}")
                    for b in r["remaining"]:
                        print(f"  [残存] {b['edinet_code']} {b.get('name', '')[:24]} "
                              f"乖離{b['max_dev'] * 100:.2f}%（分割以外の要因・要個別確認）")
                    for b in r["introduced"]:
                        print(f"  [新規] {b['edinet_code']} {b.get('name', '')[:24]} "
                              f"乖離{b['max_dev'] * 100:.2f}%")
                    # 週次を書き換えても financial_records の株価・PER/PBR/時価総額は
                    # 自動では追随しない。同じ作業内で必ず反映する（#465）。
                    print("\n次の2つを実行して修復を反映させること:\n"
                          "  1) update_market_data_from_history(db, point_in_time=True)\n"
                          "     ― financial_records の株価・PER/PBR/時価総額を再計算\n"
                          "  2) scripts/.cache/weekly_prices_*.pkl を退避\n"
                          "     ― 検証キャッシュはデータ世代を持たず旧値を黙って返す（#454/#456）\n"
                          "（夜間バッチの週次キャッシュ（#480）は app_settings の世代印を"
                          "自動で進めたので手当て不要）")
                else:
                    print("\n書き込むには --persist を付けて再実行")
            finally:
                db.close()
        asyncio.run(_repair_breaks())
    elif args.market:
        # stock_price_daily/weekly（夜間バッチが J-Quants/Yahoo で蓄積）から反映する。
        # 旧実装は stooq へ全社ぶん逐次リクエストしていたが、stooq はクラウド IP から
        # ブロックされるため実質ローカル専用の別経路になっていた（#428 で一本化）。
        db = SessionLocal()
        try:
            n = update_market_data_from_history(db)
            print(f"financial_records.stock_price: {n}社 更新")
        finally:
            db.close()
    elif args.macro:
        async def _run():
            db = SessionLocal()
            only = ([c.strip() for c in args.macro_series.split(",") if c.strip()]
                    if args.macro_series else None)
            try:
                n = await collect_macro_data(db, args.years,
                    on_progress=lambda c, t, m: print(m), only=only)
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
