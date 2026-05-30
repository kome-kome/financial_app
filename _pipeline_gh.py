"""
GitHub Actions 用パイプライン。

SKIP_XBRL_RAW=true（デフォルト）の運用前提:
- xbrl_raw_documents には書き込まない（Supabase Free 500MB 制約）
- skip_existing=True で financial_records.doc_id を参照してスキップ
- reparse_from_raw は使用しない

実行モード:
  --years-back N      収集年数（デフォルト 5）
  --collect-only      Phase 1-2 のみ（XBRL収集）
  --finalize-only     Phase 3-5 のみ（成長率/Zスコア/マクロ/J-Quants株価）
  --backfill-yahoo    Phase 6: Yahoo Finance で過去株価をバックフィル
                      （J-Quants カバー外の FY2021〜FY2023 等を補完）
  --refill-cf         Phase 7: CF NULL 補完（capex/net_change_cash を XBRL 再取得で補完）
  --refill-cf-limit N CF 補完の上限件数（デフォルト 3000・約90分）
"""
import argparse, asyncio, sys, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.exc import InternalError, OperationalError

from collector import (
    run_full_collection, update_market_data, collect_macro_data, reparse_from_raw,
    collect_stock_price_history_jquants, update_market_data_from_history,
    backfill_historical_stock_prices_yahoo, fill_recent_stock_price_gap_yahoo,
    refill_cf_from_xbrl,
    SKIP_XBRL_RAW, JQUANTS_BACKFILL_DAYS,
)
from database import SessionLocal, init_db, calc_growth_rates, calc_zscore_normalization

LOG_FILE = "pipeline_gh.log"

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _is_readonly_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "read-only" in msg or "readonlysqltransaction" in msg


async def _run_with_retry(coro_factory, label: str,
                         max_retry: int = 2, wait_sec: int = 90):
    """Supabase が一時的に read-only に切り替わる事象に対する単純なリトライ。"""
    for attempt in range(max_retry + 1):
        try:
            return await coro_factory()
        except (InternalError, OperationalError) as e:
            if not _is_readonly_error(e):
                raise
            if attempt >= max_retry:
                raise
            log(f"[{label}] ReadOnly エラー検出 (attempt {attempt+1}/{max_retry+1}) — {wait_sec}秒待機して再試行: {e.__class__.__name__}")
            await asyncio.sleep(wait_sec)


async def main(years_back: int, collect_only: bool = False,
               finalize_only: bool = False, backfill_yahoo: bool = False,
               refill_cf: bool = False, refill_cf_limit: int = 3000):
    t0 = time.time()
    log("=" * 60)
    if refill_cf:
        mode = "refill-cf"
    elif backfill_yahoo:
        mode = "backfill-yahoo"
    elif collect_only:
        mode = "collect-only"
    elif finalize_only:
        mode = "finalize-only"
    else:
        mode = "full"
    log(f"GitHub Actions パイプライン 開始 (years_back={years_back}, mode={mode})")
    log("=" * 60)

    log("[init] init_db() でスキーマ冪等マイグレーションを実行")
    init_db()

    # ─── Phase 7: CF NULL 補完（単独モード）──────────────────────────────────
    if refill_cf:
        log(f"[7/7] CF NULL 補完 開始（limit={refill_cf_limit}）")
        log(f"  対象: cf_capex IS NULL かつ cf_operating_cf IS NOT NULL")
        db7 = SessionLocal()
        try:
            result = await refill_cf_from_xbrl(
                db7,
                limit=refill_cf_limit,
                on_progress=lambda c, t, m: log(m) if c % 100 == 0 or t - c < 10 else None,
            )
            log(f"  CF 補完完了: updated={result['updated']}, skipped={result['skipped']}, failed={result['failed']}")
        finally:
            db7.close()
        log(f"[7/7] CF NULL 補完 完了 ({(time.time()-t0)/60:.1f}分経過)")
        log("=" * 60)
        log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    # ─── Phase 6: Yahoo Finance 過去株価バックフィル（単独モード）────────────────
    if backfill_yahoo:
        log(f"[6/6] Yahoo Finance バックフィル 開始")
        log(f"  対象: stock_price が NULL かつ period_end が {JQUANTS_BACKFILL_DAYS}日以前のレコード")
        db6 = SessionLocal()
        try:
            n = await backfill_historical_stock_prices_yahoo(
                db6,
                on_progress=lambda c, t, m: log(m),
            )
            log(f"  Yahoo バックフィル完了: {n}件の financial_records を更新")
        finally:
            db6.close()
        log(f"[6/6] Yahoo バックフィル 完了 ({(time.time()-t0)/60:.1f}分経過)")
        log("=" * 60)
        log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    if not finalize_only:
        # ─── Phase 1: XBRL 収集（financial_records.doc_id でスキップ）──────────
        log(f"[1/5] XBRL 収集 開始（skip_existing=True, years_back={years_back}）")
        cancelled = await _run_with_retry(
            lambda: run_full_collection(
                years_back=years_back,
                skip_existing=True,
                skip_if_raw_exists=False,
                on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
            ),
            label="1/5",
        )
        if cancelled:
            log("[1/5] 収集が停止されました")
            return
        log(f"[1/5] XBRL 収集 完了 ({(time.time()-t0)/60:.1f}分経過)")

        # ─── Phase 2: raw 再解析（SKIP_XBRL_RAW=true の場合は省略）─────────────
        if SKIP_XBRL_RAW:
            log("[2/5] reparse_from_raw 省略（SKIP_XBRL_RAW=true）")
        else:
            log("[2/5] reparse_from_raw 開始（全 xbrl_raw_documents → financial_records）")
            await _run_with_retry(
                lambda: reparse_from_raw(
                    on_progress=lambda c, t, m: log(m) if c % 200 == 0 or "完了" in m else None,
                ),
                label="2/5",
            )
            log(f"[2/5] reparse_from_raw 完了 ({(time.time()-t0)/60:.1f}分経過)")

    if not collect_only:
        # ─── Phase 3: 成長率・Zスコア再計算 ─────────────────────────────────────
        log("[3/5] 成長率・Zスコア再計算 開始")
        db = SessionLocal()
        try:
            calc_growth_rates(db)
            log("  成長率 計算完了")
            calc_zscore_normalization(db)
            log("  Zスコア 計算完了")
        finally:
            db.close()
        log(f"[3/5] 成長率・Zスコア 完了 ({(time.time()-t0)/60:.1f}分経過)")

        # ─── Phase 4: マクロデータ収集 ───────────────────────────────────────────
        log("[4/5] マクロデータ収集 開始")
        db = SessionLocal()
        try:
            n = await collect_macro_data(
                db, years_back=5,
                on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
            )
            log(f"  マクロデータ {n} 件更新")
        finally:
            db.close()
        log(f"[4/5] マクロデータ 完了 ({(time.time()-t0)/60:.1f}分経過)")

        # ─── Phase 5: 市場データ更新（J-Quants 2年分 → stock_price_history → financial_records）
        # J-Quants 無料プランの最大 JQUANTS_BACKFILL_DAYS 日分を取得。
        # point_in_time=True で全財務レコードの period_end 近傍株価を設定。
        # 約 490 営業日 × 20秒 ≈ 163分。timeout-minutes は 240 に設定済み。
        log(f"[5/5] 市場データ更新 開始（J-Quants days_back={JQUANTS_BACKFILL_DAYS}, point_in_time=True）")
        db5 = SessionLocal()
        try:
            result = await collect_stock_price_history_jquants(
                db5, days_back=JQUANTS_BACKFILL_DAYS,
                on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
            )
            log(f"  stock_price_history: {result.get('upserted', 0)}件 upsert")
            n_updated = update_market_data_from_history(db5, point_in_time=True)
            log(f"  financial_records.stock_price: {n_updated}レコード 更新（J-Quants 由来）")
        finally:
            db5.close()
        log(f"[5/5] 市場データ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    log("=" * 60)
    log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
    log("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--years-back", type=int, default=5,
                        help="収集年数（デフォルト5）")
    parser.add_argument("--collect-only", action="store_true",
                        help="Phase 1-2 のみ実行（XBRL収集）")
    parser.add_argument("--finalize-only", action="store_true",
                        help="Phase 3-5 のみ実行（成長率/Zスコア/マクロ/J-Quants株価）")
    parser.add_argument("--backfill-yahoo", action="store_true",
                        help="Phase 6: Yahoo Finance で過去株価をバックフィル（J-Quants カバー外の旧年度を補完）")
    parser.add_argument("--refill-cf", action="store_true",
                        help="Phase 7: CF NULL 補完（capex/net_change_cash を XBRL 再取得で補完）")
    parser.add_argument("--refill-cf-limit", type=int, default=3000,
                        help="CF 補完の上限件数（デフォルト 3000・約90分）")
    args = parser.parse_args()

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(
            f"パイプライン開始: {datetime.now()}"
            f"  years_back={args.years_back}"
            f"  collect_only={args.collect_only}"
            f"  finalize_only={args.finalize_only}"
            f"  backfill_yahoo={args.backfill_yahoo}"
            f"  refill_cf={args.refill_cf}"
            f"  refill_cf_limit={args.refill_cf_limit}\n"
        )
    asyncio.run(main(
        args.years_back,
        collect_only=args.collect_only,
        finalize_only=args.finalize_only,
        backfill_yahoo=args.backfill_yahoo,
        refill_cf=args.refill_cf,
        refill_cf_limit=args.refill_cf_limit,
    ))
