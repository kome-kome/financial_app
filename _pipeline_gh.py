"""
GitHub Actions 用パイプライン。
・xbrl_raw_documents に保存済みの書類はスキップ（再ダウンロード不要）
・全書類の raw 保存完了後、reparse_from_raw で financial_records を更新
・その後、マクロ・市場データ・Zスコア・成長率を更新
"""
import asyncio, sys, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from collector import run_full_collection, update_market_data, collect_macro_data, reparse_from_raw, MACRO_SERIES
from database import SessionLocal, calc_growth_rates, calc_zscore_normalization

LOG_FILE = "pipeline_gh.log"

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

async def main():
    t0 = time.time()
    log("=" * 60)
    log("GitHub Actions パイプライン 開始")
    log("=" * 60)

    # ─── Phase 1: XBRL raw 収集（xbrl_raw_documents に未保存分のみ）────
    log("[1/5] XBRL raw 収集 開始（保存済みdocはスキップ）")
    cancelled = await run_full_collection(
        years_back=5,
        skip_existing=False,
        skip_if_raw_exists=True,  # xbrl_raw_documents に既存の doc はスキップ
        on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
    )
    if cancelled:
        log("[1/5] 収集が停止されました")
        return
    log(f"[1/5] XBRL raw 収集 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 2: raw から financial_records を全件再解析 ───────────────
    log("[2/5] reparse_from_raw 開始（全 xbrl_raw_documents → financial_records）")
    await reparse_from_raw(
        on_progress=lambda c, t, m: log(m) if c % 200 == 0 or "完了" in m else None,
    )
    log(f"[2/5] reparse_from_raw 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 3: 成長率・Zスコア再計算 ─────────────────────────────────
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

    # ─── Phase 4: マクロデータ収集 ───────────────────────────────────────
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

    # ─── Phase 5: 市場データ更新 ─────────────────────────────────────────
    log("[5/5] 市場データ更新 開始（stooq）")
    await update_market_data(
        on_progress=lambda c, t, m: log(m) if c % 200 == 0 or "完了" in m else None,
    )
    log(f"[5/5] 市場データ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    log("=" * 60)
    log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
    log("=" * 60)

if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"パイプライン開始: {datetime.now()}\n")
    asyncio.run(main())
