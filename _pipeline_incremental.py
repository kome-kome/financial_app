"""
GitHub Actions 用・差分収集パイプライン（毎日自動実行向け）。

対象: 過去1年・収集済みスキップ（XBRL）＋成長率/Zスコア再計算
     ＋市場データ（株価）更新 ＋マクロデータ（為替・金利等）更新

全件収集は _pipeline_gh.py で workflow_dispatch 手動実行。
"""
import asyncio, sys, time
from datetime import datetime, date, timedelta
from functools import partial
from dotenv import load_dotenv
load_dotenv()

from collector import (
    run_full_collection, collect_macro_data,
    collect_stock_price_history_jquants, update_market_data_from_history,
    fill_recent_stock_price_gap_yahoo,
)
from database import SessionLocal, init_db
import _pipeline_utils
from macro_health import check_macro_freshness, format_report

LOG_FILE = "logs/pipeline_incremental.log"

log = _pipeline_utils.make_logger(LOG_FILE)
_is_readonly_error = _pipeline_utils._is_readonly_error
# 差分パイプラインは指数バックオフ（backoff_base=2）で従来挙動を維持する。
_run_with_retry = partial(_pipeline_utils._run_with_retry, log_fn=log, backoff_base=2)


async def main():
    t0 = time.time()
    log("=" * 60)
    log("差分収集パイプライン 開始")
    log("=" * 60)

    log("[init] init_db() でスキーマ冪等マイグレーションを実行")
    init_db()

    # ─── Phase 1: XBRL 差分収集（過去1年・収集済みスキップ）───────────────
    log("[1/4] XBRL 差分収集 開始（過去1年・skip_existing=True）")
    db1 = SessionLocal()
    try:
        cancelled = await _run_with_retry(
            lambda: run_full_collection(
                db1,
                years_back=1,
                skip_existing=True,
                on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
            ),
            label="XBRL差分収集",
        )
    finally:
        db1.close()
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

        # 健全性レポート（#420）: collect_macro_data は 1 系列が取れなくても continue する
        # ため、部分失敗は exit 0 で通り #414 の失敗通知にも載らない。その時点の鮮度を
        # run ログへ残しておく（後から「いつ欠け始めたか」を遡れるようにする）。
        # **ここでは非ゼロ終了しない**。マクロの不調でこのジョブを failure にすると、
        # マクロを一切使わない sector_ols の夜間更新（nightly-scores の workflow_run
        # チェーンは conclusion=success 条件・#432）まで巻き添えで止まるため。
        # 終了コードによる通知は独立した macro-health.yml が担う。
        for line in format_report(check_macro_freshness(db)):
            log(line)
    finally:
        db.close()
    log(f"[3/4] マクロデータ 完了 ({(time.time()-t0)/60:.1f}分経過)")

    # ─── Phase 4: 市場データ更新（Yahoo で鮮度確保 → J-Quants で公式値へ置換）────
    # 直近の鮮度は Yahoo ギャップ補完が担う。J-Quants 無料は直近84日（12週）を配信しないため、
    # かつては days_back=14 でも取得していたが、この窓はエンバーゴ内で**構造的に常に0件**であり
    # （毎日 JQUANTS_RATE_SLEEP=20s × 14日 ≒ 4.7分の空振り）、しかも全日403となって
    # 中断ガードを誤発火させ Yahoo 補完まで巻き添えで止めていた（#419 / #425）。
    log("[4/4] 市場データ更新 開始（Yahoo で鮮度確保 → J-Quants catchup で公式値へ置換）")
    db4 = SessionLocal()
    try:
        # 鮮度を先に確保する（gap_days=0: steady-state でも毎日 Yahoo が直近を補完）。
        # J-Quants より先に置くのは、片方の収集元の失敗がもう片方を巻き添えにしないため（#425）。
        gap_result = await fill_recent_stock_price_gap_yahoo(
            db4, gap_days=0,
            on_progress=lambda c, t, m: log(m) if c % 500 == 0 or "完了" in m else None,
        )
        if not gap_result.get("skipped"):
            log(f"  Yahoo Finance gap-fill: {gap_result.get('upserted', 0)}件 追加"
                f"（{gap_result.get('from')} 〜 {gap_result.get('to')}・{gap_result.get('companies')}社）")

        # J-Quants catchup: 12週境界を過ぎた直後（today-90〜today-80日）を再取得し、
        # Yahoo 暫定値を J-Quants 公式値で自動上書きする（毎日走ることで徐々に置換）。
        # J-Quants 側の障害で鮮度更新（上の Yahoo）と PER/PBR 反映（下の market_data）を
        # 落とさないよう、この呼び出しだけは失敗を握って継続する。
        _catchup_to   = date.today() - timedelta(days=80)
        _catchup_from = date.today() - timedelta(days=90)
        try:
            catchup_result = await collect_stock_price_history_jquants(
                db4, date_from=_catchup_from, date_to=_catchup_to,
                on_progress=lambda c, t, m: log(m) if c % 3 == 0 or "完了" in m else None,
            )
            log(f"  J-Quants catchup ({_catchup_from}〜{_catchup_to}): "
                f"{catchup_result.get('upserted', 0)}件 upsert"
                + (f"・契約窓外 {catchup_result['out_of_coverage']}日"
                   if catchup_result.get("out_of_coverage") else "")
                # 403 は契約失効／プラン対象外／URL 不在。**カバレッジ境界ではない**（#462）
                + ("（全日403＝要確認）" if catchup_result.get("all_forbidden") else ""))
        except Exception as e:
            log(f"  J-Quants catchup 失敗（継続します）: {type(e).__name__}: {e}")

        # 開始も残す（#470）。完了ログしか無かったため、2026-08-08 の失敗は
        # 「catchup 完了の 2分16秒後に落ちた」から**推定**するしかなかった。
        log("  financial_records へ株価・バリュエーションを反映 開始")
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
