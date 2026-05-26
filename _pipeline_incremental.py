"""
GitHub Actions 用・差分収集パイプライン（毎日自動実行向け）。

対象: 過去1年・収集済みスキップ（XBRL）＋成長率/Zスコア再計算
     ＋市場データ（株価）更新 ＋マクロデータ（為替・金利等）更新

全件収集は _pipeline_gh.py で workflow_dispatch 手動実行。
"""
import asyncio, sys, time
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from collector import run_full_collection, update_market_data, collect_macro_data
from database import SessionLocal, init_db, calc_growth_rates, calc_zscore_normalization

LOG_FILE = "pipeline_incremental.log"

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main():
    t0 = time.time()
    log("=" * 60)
    log("差分収集パイプライン 開始")
    log("=" * 60)

    log("[init] init_db() でスキーマ冪等マイグレーションを実行")
    init_db()

    # ─── Phase 1: XBRL 差分収集（過去1年・収集済みスキップ）───────────────
    log("[1/4] XBRL 差分収集 開始（過去1年・skip_existing=True）")
    cancelled = await run_full_collection(
        years_back=1,
        skip_existing=True,
        on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
    )
    if cancelled:
        log("[1/4] 収集が停止されました")
        return
    log(f"[1/4] XBRL 差分収集 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 2: 成長率・Zスコア再計算 ─────────────────────────────────────
    log("[2/4] 成長率・Zスコア再計算 開始")
    db = SessionLocal()
    try:
        calc_growth_rates(db)
        log("  成長率 計算完了")
        calc_zscore_normalization(db)
        log("  Zスコア 計算完了")
    finally:
        db.close()
    log(f"[2/4] 成長率・Zスコア 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 3: マクロデータ収集 ───────────────────────────────────────────
    log("[3/4] マクロデータ収集 開始")
    db = SessionLocal()
    try:
        n = await collect_macro_data(
            db, years_back=5,
            on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
        )
        log(f"  マクロデータ {n} 件更新")
    finally:
        db.close()
    log(f"[3/4] マクロデータ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 4: 市場データ更新（株価・J-Quants）──────────────────────────
    log("[4/4] 市場データ更新 開始")
    await update_market_data(
        on_progress=lambda c, t, m: log(m) if c % 200 == 0 or "完了" in m else None,
    )
    log(f"[4/4] 市場データ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    log("=" * 60)
    log(f"差分収集パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
    log("=" * 60)

if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"差分収集パイプライン開始: {datetime.now()}\n")
    asyncio.run(main())
