"""
GitHub Actions 用・差分収集パイプライン（毎日自動実行向け）。

対象: 過去1年・収集済みスキップ（XBRL）＋成長率/Zスコア再計算
     ＋市場データ（株価）更新 ＋マクロデータ（為替・金利等）更新

全件収集は _pipeline_gh.py で workflow_dispatch 手動実行。
"""
import asyncio, sys, time
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.exc import InternalError, OperationalError

from collector import (
    run_full_collection, collect_macro_data,
    collect_stock_price_history_jquants, update_market_data_from_history,
    fill_recent_stock_price_gap_yahoo,
)
from database import SessionLocal, init_db

LOG_FILE = "pipeline_incremental.log"

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
    """Supabase が一時的に read-only に切り替わる事象に対する指数バックオフリトライ。"""
    for attempt in range(max_retry + 1):
        try:
            return await coro_factory()
        except (InternalError, OperationalError) as e:
            if not _is_readonly_error(e):
                raise
            if attempt >= max_retry:
                raise
            wait = wait_sec * (2 ** attempt)
            log(f"[{label}] ReadOnly エラー検出 (attempt {attempt+1}/{max_retry+1}) — {wait}秒待機して再試行: {e.__class__.__name__}")
            await asyncio.sleep(wait)


async def main():
    t0 = time.time()
    log("=" * 60)
    log("差分収集パイプライン 開始")
    log("=" * 60)

    log("[init] init_db() でスキーマ冪等マイグレーションを実行")
    init_db()

    # ─── Phase 1: XBRL 差分収集（過去1年・収集済みスキップ）───────────────
    log("[1/4] XBRL 差分収集 開始（過去1年・skip_existing=True）")
    cancelled = await _run_with_retry(
        lambda: run_full_collection(
            years_back=1,
            skip_existing=True,
            on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
        ),
        label="XBRL差分収集",
    )
    if cancelled:
        log("[1/4] 収集が停止されました")
        return
    log(f"[1/4] XBRL 差分収集 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 2: 成長率・Zスコアは financial_metrics VIEW が都度算出するため事前計算は不要 ───
    log("[2/4] 成長率・Zスコアは financial_metrics VIEW で都度算出（事前計算スキップ）")

    # ─── Phase 3: マクロデータ収集 ───────────────────────────────────────────
    log("[3/4] マクロデータ収集 開始")
    db = SessionLocal()
    try:
        n = await _run_with_retry(
            lambda: collect_macro_data(
                db, years_back=5,
                on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
            ),
            label="マクロデータ収集",
        )
        log(f"  マクロデータ {n} 件更新")
    finally:
        db.close()
    log(f"[3/4] マクロデータ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 4: 市場データ更新（J-Quants 優先 → Yahoo Finance ギャップ補完）────
    # J-Quants: days_back=14 で直近2週分を取得。
    # J-Quants のデータが7日以上古い場合（APIラグ等）は Yahoo Finance で補完。
    log("[4/4] 市場データ更新 開始（J-Quants 優先 → Yahoo Finance フォールバック）")
    db4 = SessionLocal()
    try:
        result = await collect_stock_price_history_jquants(
            db4, days_back=14,
            on_progress=lambda c, t, m: log(m) if c % 3 == 0 or "完了" in m else None,
        )
        log(f"  J-Quants: stock_price_history {result.get('upserted', 0)}件 upsert")

        # J-Quants catchup: 12週境界を過ぎた直後（today-90〜today-80日）を再取得し、
        # Yahoo 暫定値を J-Quants 公式値で自動上書きする（毎日走ることで徐々に置換）
        _catchup_to   = date.today() - timedelta(days=80)
        _catchup_from = date.today() - timedelta(days=90)
        catchup_result = await collect_stock_price_history_jquants(
            db4, date_from=_catchup_from, date_to=_catchup_to,
            on_progress=lambda c, t, m: log(m) if c % 3 == 0 or "完了" in m else None,
        )
        log(f"  J-Quants catchup ({_catchup_from}〜{_catchup_to}): {catchup_result.get('upserted', 0)}件 upsert")

        # gap_days=0: steady-state でも毎日 Yahoo が直近を補完する
        gap_result = await fill_recent_stock_price_gap_yahoo(
            db4, gap_days=0,
            on_progress=lambda c, t, m: log(m) if c % 500 == 0 or "完了" in m else None,
        )
        if not gap_result.get("skipped"):
            log(f"  Yahoo Finance gap-fill: {gap_result.get('upserted', 0)}件 追加"
                f"（{gap_result.get('from')} 〜 {gap_result.get('to')}）")

        n_updated = update_market_data_from_history(db4)
        log(f"  financial_records.stock_price: {n_updated}社 更新")
    finally:
        db4.close()
    log(f"[4/4] 市場データ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    log("=" * 60)
    log(f"差分収集パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
    log("=" * 60)

if __name__ == "__main__":
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"差分収集パイプライン開始: {datetime.now()}\n")
    asyncio.run(main())
