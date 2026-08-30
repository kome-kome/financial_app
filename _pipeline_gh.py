"""
GitHub Actions 用パイプライン。

SKIP_XBRL_RAW=true（デフォルト）の運用前提:
- xbrl_raw_documents には書き込まない（Supabase Free 500MB 制約）
- skip_existing=True で financial_records.doc_id を参照してスキップ
- reparse_from_raw は使用しない

実行モード:
  --years-back N      収集年数（デフォルト 5）
  --collect-only      Phase 1-2 のみ（XBRL収集）
  --finalize-only     Phase 3-5 のみ（Phase 3=成長率/Zスコアは financial_metrics VIEW が
                      都度算出のため事前計算なし／Phase 4=マクロ／
                      Phase 5=Yahoo 鮮度補完→J-Quants株価→financial_records 反映）
  --backfill-yahoo    Phase 6: Yahoo Finance で過去株価をバックフィル
                      （J-Quants カバー外の FY2021〜FY2023 等を補完）
  --refill-cf         Phase 7: CF NULL 補完（capex/net_change_cash を XBRL 再取得で補完）
  --refill-cf-limit N CF 補完の上限件数（CLI デフォルト 6000）
  --refill-pl-bs      Phase 8: bs_inventory NULL 補完（旧コホートの PL/BS 列を XBRL 再取得で是正）
  --refill-pl-bs-limit N  PL/BS 補完の上限件数（CLI デフォルト None＝全件）
  --refill-c2         C2 NULL 補完（pl_depreciation/bs_ppe_total 等を XBRL 再取得で是正）
  --refill-c2-limit N C2 補完の上限件数（CLI デフォルト None＝全件）
  --refill-machinery  bs_machinery NULL（かつ bs_ppe_total あり）を XBRL 再取得で補完
  --refill-machinery-limit N  機械装置補完の上限件数（デフォルト None＝全件）
"""
import argparse, asyncio, sys, time
from datetime import datetime
from functools import partial
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

from collector import (
    run_full_collection, collect_macro_data, reparse_from_raw,
    collect_stock_price_history_jquants, update_market_data_from_history,
    backfill_historical_stock_prices_yahoo, fill_recent_stock_price_gap_yahoo,
    backfill_weekly_history_yahoo,
    refill_cf_from_xbrl, refill_pl_bs_from_xbrl, refill_c2_from_xbrl,
    refill_machinery_from_xbrl, diagnose_cf_labels,
    SKIP_XBRL_RAW, JQUANTS_BACKFILL_DAYS,
)
from database import SessionLocal, init_db
import _pipeline_utils
from macro_health import check_macro_freshness, format_report

LOG_FILE = "logs/pipeline_gh.log"

log = _pipeline_utils.make_logger(LOG_FILE)
_is_readonly_error = _pipeline_utils._is_readonly_error
# gh パイプラインは定数待機（backoff_base=1）で従来挙動を維持する。
_run_with_retry = partial(_pipeline_utils._run_with_retry, log_fn=log, backoff_base=1)


async def main(years_back: int, collect_only: bool = False,
               finalize_only: bool = False, backfill_yahoo: bool = False,
               backfill_weekly: bool = False, backfill_weekly_years: int = 5,
               refill_cf: bool = False, refill_cf_limit: int = 3000,
               refill_cf_sleep: float = 0.5, refill_capex_only: bool = False,
               refill_missing_cf: bool = False,
               refill_pl_bs: bool = False, refill_pl_bs_limit: Optional[int] = None,
               refill_pl_bs_sleep: float = 0.6,
               refill_c2: bool = False, refill_c2_limit: Optional[int] = None,
               refill_c2_sleep: float = 0.6,
               refill_machinery: bool = False, refill_machinery_limit: Optional[int] = None,
               refill_machinery_sleep: float = 0.6,
               diagnose_cf: bool = False, diagnose_cf_limit: int = 20,
               jquants_days: int = JQUANTS_BACKFILL_DAYS):
    t0 = time.time()
    log("=" * 60)
    if diagnose_cf:
        mode = "diagnose-cf"
    elif refill_cf:
        mode = ("refill-cf-missing" if refill_missing_cf
                else "refill-capex-only" if refill_capex_only else "refill-cf")
    elif refill_pl_bs:
        mode = "refill-pl-bs"
    elif refill_c2:
        mode = "refill-c2"
    elif refill_machinery:
        mode = "refill-machinery"
    elif backfill_yahoo:
        mode = "backfill-yahoo"
    elif backfill_weekly:
        mode = "backfill-weekly"
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

    # ─── 診断モード: CF ラベルのサンプル出力（単独モード）──────────────────────
    if diagnose_cf:
        log(f"[diagnose] CF ラベル診断 開始（サンプル {diagnose_cf_limit}件）")
        dbd = SessionLocal()
        try:
            found = await diagnose_cf_labels(dbd, limit=diagnose_cf_limit)
            log(f"[diagnose] 完了: ユニーク要素 {len(found)}種（詳細は上記ログ参照）")
        finally:
            dbd.close()
        log("=" * 60)
        log(f"診断完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    # ─── Phase 7: CF NULL 補完（単独モード）──────────────────────────────────
    if refill_cf:
        if refill_missing_cf:
            target_desc = "cf_operating_cf IS NULL（CF全NULL＝IFRS決算大企業等の営業/投資/財務CFを補完）"
        elif refill_capex_only:
            target_desc = "cf_capex IS NULL かつ cf_net_change_cash IS NOT NULL（capex のみ補完）"
        else:
            target_desc = "cf_net_change_cash IS NULL（投資CF/現金増減/capex を補完）"
        log(f"[7/7] CF NULL 補完 開始（limit={refill_cf_limit}, capex_only={refill_capex_only}, "
            f"missing_cf={refill_missing_cf}）")
        log(f"  対象: {target_desc}")
        db7 = SessionLocal()
        try:
            result = await refill_cf_from_xbrl(
                db7,
                limit=refill_cf_limit,
                capex_only=refill_capex_only,
                missing_cf=refill_missing_cf,
                sleep_sec=refill_cf_sleep,
                on_progress=lambda c, t, m: log(m) if c % 100 == 0 or t - c < 10 else None,
            )
            log(f"  CF 補完完了: updated={result['updated']}, skipped={result['skipped']}, "
                f"failed={result['failed']}, remaining={result.get('remaining', '?')}")
        finally:
            db7.close()
        log(f"[7/7] CF NULL 補完 完了 ({(time.time()-t0)/60:.1f}分経過)")
        log("=" * 60)
        log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    # ─── Phase 8: bs_inventory NULL 補完（単独モード）────────────────────────────
    # パーサ修正（_inventory_fallback）前に収集した旧コホートが backfill 未実施で
    # bs_inventory IS NULL のまま残存。古い順（order="asc"）に XBRL を再取得し是正する。
    # 金融・サービス等の正当 NULL は何度実行しても埋まらず少数残件として残る（無害）。
    if refill_pl_bs:
        log(f"[8/8] bs_inventory NULL 補完 開始（limit={refill_pl_bs_limit}, 古い順）")
        log("  対象: bs_inventory IS NULL かつ doc_id IS NOT NULL（NULL の PL/BS 列を補完）")
        db8 = SessionLocal()
        try:
            result = await refill_pl_bs_from_xbrl(
                db8,
                limit=refill_pl_bs_limit,
                sleep_sec=refill_pl_bs_sleep,
                on_progress=lambda c, t, m: log(m) if c % 100 == 0 or t - c < 10 else None,
            )
            log(f"  PL/BS 補完完了: updated={result['updated']}, skipped={result['skipped']}, "
                f"failed={result['failed']}, remaining={result.get('remaining', '?')}")
        finally:
            db8.close()
        log(f"[8/8] bs_inventory NULL 補完 完了 ({(time.time()-t0)/60:.1f}分経過)")
        log("=" * 60)
        log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    # ─── 機械装置 NULL 補完（単独モード）──────────────────────────────────────────
    # MachineryAndVehiclesNet タグ追加後の既存データ是正。bs_ppe_total があるのに
    # bs_machinery が NULL のレコードを古い順に再取得して補完する。
    if refill_machinery:
        log(f"[machinery] bs_machinery NULL 補完 開始（limit={refill_machinery_limit}, 古い順）")
        log("  対象: bs_machinery IS NULL かつ bs_ppe_total IS NOT NULL かつ doc_id IS NOT NULL")
        db_m = SessionLocal()
        try:
            result = await refill_machinery_from_xbrl(
                db_m,
                limit=refill_machinery_limit,
                sleep_sec=refill_machinery_sleep,
                on_progress=lambda c, t, m: log(m) if c % 100 == 0 or t - c < 10 else None,
            )
            log(f"  機械装置補完完了: updated={result['updated']}, skipped={result['skipped']}, "
                f"failed={result['failed']}, remaining={result.get('remaining', '?')}")
        finally:
            db_m.close()
        log(f"[machinery] bs_machinery NULL 補完 完了 ({(time.time()-t0)/60:.1f}分経過)")
        log("=" * 60)
        log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    # ─── C2 NULL 補完（単独モード）──────────────────────────────────────────────
    # C2 追加列（pl_depreciation / bs_ppe_total 等）は skip_existing=True の通常収集では
    # 既存 doc_id がスキップされるため NULL のまま残存。pl_depreciation を駆動マーカーとして
    # 古い順に再取得し、PL/BS/nonfin の NULL 列を一括補完する。
    if refill_c2:
        log(f"[C2] C2 NULL 補完 開始（limit={refill_c2_limit}, 古い順）")
        log("  対象: pl_depreciation IS NULL かつ doc_id IS NOT NULL（C2 列を補完）")
        db_c2 = SessionLocal()
        try:
            result = await refill_c2_from_xbrl(
                db_c2,
                limit=refill_c2_limit,
                sleep_sec=refill_c2_sleep,
                on_progress=lambda c, t, m: log(m) if c % 100 == 0 or t - c < 10 else None,
            )
            log(f"  C2 補完完了: updated={result['updated']}, skipped={result['skipped']}, "
                f"failed={result['failed']}, remaining={result.get('remaining', '?')}")
        finally:
            db_c2.close()
        log(f"[C2] C2 NULL 補完 完了 ({(time.time()-t0)/60:.1f}分経過)")
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

    # ─── 週次株価バックフィル（単独モード・#198）──────────────────────────────────
    # stock_price_weekly を過去 backfill_weekly_years 年まで延伸し、use_momentum=ON 時の
    # walk-forward CV（52週先リターン＋12ヶ月モメンタム）が成立する被覆を確保する。
    if backfill_weekly:
        log(f"[週次backfill] 開始（years_back={backfill_weekly_years}, Yahoo Finance）")
        log("  対象: stock_price_weekly の最古日が today-years_back より新しい（過去不足）社")
        dbw = SessionLocal()
        try:
            r = await backfill_weekly_history_yahoo(
                dbw, years_back=backfill_weekly_years,
                on_progress=lambda c, t, m: log(m),
            )
            log(f"  週次backfill 完了: {r}")
            # 週次を過去方向へ延伸した＝差分ロードの27週窓の外に行が増えた（#480・ADR-0036）。
            # 行数が増えるので指紋の照合でも拾えるが、印を進めておけば次回1回で確実に直る。
            import weekly_price_cache
            weekly_price_cache.bump_generation_safely(
                dbw, f"backfill-weekly years_back={backfill_weekly_years}")
        finally:
            dbw.close()
        log(f"[週次backfill] 完了 ({(time.time()-t0)/60:.1f}分経過)")
        log("=" * 60)
        log(f"パイプライン完了  総所要時間: {(time.time()-t0)/60:.1f}分")
        log("=" * 60)
        return

    if not finalize_only:
        # ─── Phase 1: XBRL 収集（financial_records.doc_id でスキップ）──────────
        # **ここは `_pipeline_incremental` と違い EdinetAccessError を握らない**（#580）。
        # 意図的な非対称であって直し忘れではない: あちらは毎晩走る鮮度の担い手で、EDINET が
        # 死んでも株価・マクロは取り続けさせる価値がある（#425）。こちらは手動の全件収集で、
        # XBRL が取れないなら走らせる意味そのものが無いので、早く落ちるほうが正しい。
        log(f"[1/5] XBRL 収集 開始（skip_existing=True, years_back={years_back}）")
        db1 = SessionLocal()
        try:
            cancelled = await _run_with_retry(
                lambda: run_full_collection(
                    db1,
                    years_back=years_back,
                    skip_existing=True,
                    skip_if_raw_exists=False,
                    on_progress=lambda c, t, m: log(m) if c % 50 == 0 or "[完了]" in m or "[企業マスタ" in m else None,
                ),
                label="1/5",
            )
        finally:
            db1.close()
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
        # ─── Phase 3: 成長率・Zスコアは financial_metrics VIEW が都度算出するため事前計算は不要 ───
        log("[3/5] 成長率・Zスコアは financial_metrics VIEW で都度算出（事前計算スキップ）")

        # ─── Phase 4: マクロデータ収集 ───────────────────────────────────────────
        log("[4/5] マクロデータ収集 開始")
        db = SessionLocal()
        try:
            n = await collect_macro_data(
                db, years_back=5,
                on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
            )
            log(f"  マクロデータ {n} 件更新")
            # 健全性レポート（#420）。collect_macro_data は 1 系列が取れなくても continue する
            # ため、部分失敗は exit 0 で通り #414 の失敗通知に載らない。その時点の鮮度を
            # run ログへ残す。非ゼロ終了は独立した macro-health.yml が担う（ここで落とすと
            # マクロと無関係な Phase 5＝株価鮮度を巻き添えにする・#425）。
            for line in format_report(check_macro_freshness(db)):
                log(line)
        finally:
            db.close()
        log(f"[4/5] マクロデータ 完了 ({(time.time()-t0)/60:.1f}分経過)")

        # ─── Phase 5: 市場データ更新（Yahoo で鮮度確保 → J-Quants で公式値へ置換 → 反映）
        # J-Quants 無料プランは直近 84日（12週）を配信しないため、finalize を J-Quants
        # だけで組むと**全件収集を何度回しても直近12週の株価は1日も前進しない**（#426）。
        # しかも全件収集中は daily-incremental を止める運用（DEPLOYMENT.md）なので、
        # その間 Yahoo ギャップ補完＝鮮度の実質唯一の担い手が完全に不在になる。
        # 差分パイプライン（_pipeline_incremental.py Phase 4）と同じ順序に揃える:
        #   鮮度担当の Yahoo を先に置き、J-Quants の失敗は握って継続する（#425）。
        # J-Quants は最大 JQUANTS_BACKFILL_DAYS 日分（約 490 営業日 × 20秒 ≈ 163分）、
        # Yahoo 補完は約4,000社 ×（YAHOO_STOCK_RATE_SLEEP=0.5秒）で +35〜60分。
        # point_in_time=True で全財務レコードの period_end 近傍株価を設定する。
        log(f"[5/5] 市場データ更新 開始（Yahoo 鮮度補完 → J-Quants days_back={JQUANTS_BACKFILL_DAYS} → point_in_time=True 反映）")
        db5 = SessionLocal()
        try:
            # 鮮度を先に確保する（gap_days=0・起点は銘柄別の最終 trade_date＝#415）。
            gap_result = await fill_recent_stock_price_gap_yahoo(
                db5, gap_days=0,
                on_progress=lambda c, t, m: log(m) if c % 500 == 0 or "完了" in m else None,
            )
            if gap_result.get("skipped"):
                log(f"  Yahoo Finance gap-fill: スキップ（{gap_result.get('reason')}"
                    f"・基準セッション {gap_result.get('session')}）")
            else:
                # upserted は**投入行数**（ON CONFLICT DO UPDATE）。正味の増分は new_rows（#474）。
                log(f"  Yahoo Finance gap-fill: {gap_result.get('upserted', 0)}件 投入"
                    f"（うち新規日付 {gap_result.get('new_rows', 0)}件・"
                    f"{gap_result.get('from')} 〜 {gap_result.get('to')}・"
                    f"{gap_result.get('companies')}社・基準セッション {gap_result.get('session')}）")

            # J-Quants 側の障害（境界403・レート制限等）で鮮度確保と PER/PBR 反映を
            # 落とさないよう、この呼び出しだけは失敗を握って継続する（#425）。
            try:
                result = await collect_stock_price_history_jquants(
                    db5, days_back=jquants_days,
                    on_progress=lambda c, t, m: log(m) if c % 10 == 0 or "完了" in m else None,
                )
                log(f"  stock_price_history: {result.get('upserted', 0)}件 upsert")
                if result.get("out_of_coverage"):
                    # 400＝契約窓の外側。平常運転（無料プランのエンバーゴ・遡及上限）・#462
                    log(f"  J-Quants 契約窓外でスキップ: {result['out_of_coverage']}日")
                if result.get("forbidden"):
                    # 403＝契約失効／プラン対象外／URL 不在。**境界ではない**（境界は 400）・#462
                    log(f"  J-Quants 403 でスキップ: {result['forbidden']}日"
                        + (f"（うち契約失効 {result['no_subscription']}日）"
                           if result.get("no_subscription") else "")
                        + ("（全日403）" if result.get("all_forbidden") else ""))
            except Exception as e:
                log(f"  J-Quants 株価取得 失敗（継続します）: {type(e).__name__}: {e}")

            n_updated = update_market_data_from_history(db5, point_in_time=True)
            log(f"  financial_records.stock_price: {n_updated}レコード 更新")
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
    parser.add_argument("--jquants-days", type=int, default=JQUANTS_BACKFILL_DAYS,
                        help=(f"Phase 5 の J-Quants 遡及日数（デフォルト {JQUANTS_BACKFILL_DAYS}）。"
                              "1日あたり JQUANTS_RATE_SLEEP=20秒 かかるため finalize の所要を"
                              "直接決める。timeout に収めたいときだけ小さくする（#426/#462）"))
    parser.add_argument("--backfill-yahoo", action="store_true",
                        help="Phase 6: Yahoo Finance で過去株価をバックフィル（J-Quants カバー外の旧年度を補完）")
    parser.add_argument("--backfill-weekly", action="store_true",
                        help="週次株価バックフィル（#198）: stock_price_weekly を過去方向へ延伸し use_momentum=ON の CV 被覆を確保")
    parser.add_argument("--backfill-weekly-years", type=int, default=5,
                        help="週次バックフィルの延伸年数（デフォルト 5）")
    parser.add_argument("--refill-cf", action="store_true",
                        help="Phase 7: CF NULL 補完（投資CF/現金増減/capex を XBRL 再取得で補完）")
    parser.add_argument("--refill-cf-limit", type=int, default=6000,
                        help="CF 補完の上限件数（デフォルト 6000・約3〜4時間）")
    parser.add_argument("--refill-cf-sleep", type=float, default=0.5,
                        help="CF 補完の1件あたりスリープ秒（デフォルト 0.5）")
    parser.add_argument("--refill-capex-only", action="store_true",
                        help="capex のみ補完（net_change_cash 補完済みレコードの設備投資を一度だけ補完。手動ワンショット専用）")
    parser.add_argument("--refill-cf-missing", action="store_true",
                        help="CF全NULL（cf_operating_cf IS NULL）レコードを補完（IFRS決算大企業等の営業/投資/財務CF）")
    parser.add_argument("--refill-pl-bs", action="store_true",
                        help="Phase 8: bs_inventory NULL 補完（旧コホートの PL/BS 列を XBRL 再取得で是正・古い順）")
    parser.add_argument("--refill-pl-bs-limit", type=int, default=None,
                        help="PL/BS 補完の上限件数（デフォルト None＝全件・約4〜5時間）")
    parser.add_argument("--refill-pl-bs-sleep", type=float, default=0.6,
                        help="PL/BS 補完の1件あたりスリープ秒（デフォルト 0.6）")
    parser.add_argument("--refill-c2", action="store_true",
                        help="C2 NULL 補完（pl_depreciation/bs_ppe_total 等 C2 追加列を XBRL 再取得で是正・古い順）")
    parser.add_argument("--refill-c2-limit", type=int, default=None,
                        help="C2 補完の上限件数（デフォルト None＝全件）")
    parser.add_argument("--refill-c2-sleep", type=float, default=0.6,
                        help="C2 補完の1件あたりスリープ秒（デフォルト 0.6）")
    parser.add_argument("--refill-machinery", action="store_true",
                        help="bs_machinery NULL 補完（MachineryAndVehiclesNet タグ追加後の是正・古い順）")
    parser.add_argument("--refill-machinery-limit", type=int, default=None,
                        help="機械装置補完の上限件数（デフォルト None＝全件）")
    parser.add_argument("--refill-machinery-sleep", type=float, default=0.6,
                        help="機械装置補完の1件あたりスリープ秒（デフォルト 0.6）")
    parser.add_argument("--diagnose-cf", action="store_true",
                        help="診断モード: サンプル書類の CF ラベル/要素IDを出力（capex ラベル照合の検証用）")
    parser.add_argument("--diagnose-cf-limit", type=int, default=20,
                        help="診断モードのサンプル件数（デフォルト 20）")
    args = parser.parse_args()

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(
            f"パイプライン開始: {datetime.now()}"
            f"  years_back={args.years_back}"
            f"  collect_only={args.collect_only}"
            f"  finalize_only={args.finalize_only}"
            f"  backfill_yahoo={args.backfill_yahoo}"
            f"  backfill_weekly={args.backfill_weekly}"
            f"  refill_cf={args.refill_cf}"
            f"  refill_cf_limit={args.refill_cf_limit}"
            f"  refill_capex_only={args.refill_capex_only}"
            f"  refill_cf_missing={args.refill_cf_missing}"
            f"  refill_pl_bs={args.refill_pl_bs}"
            f"  refill_pl_bs_limit={args.refill_pl_bs_limit}"
            f"  refill_c2={args.refill_c2}"
            f"  refill_c2_limit={args.refill_c2_limit}"
            f"  refill_machinery={args.refill_machinery}"
            f"  refill_machinery_limit={args.refill_machinery_limit}"
            f"  diagnose_cf={args.diagnose_cf}"
            f"  jquants_days={args.jquants_days}\n"
        )
    asyncio.run(main(
        args.years_back,
        collect_only=args.collect_only,
        finalize_only=args.finalize_only,
        backfill_yahoo=args.backfill_yahoo,
        backfill_weekly=args.backfill_weekly,
        backfill_weekly_years=args.backfill_weekly_years,
        refill_cf=args.refill_cf,
        refill_cf_limit=args.refill_cf_limit,
        refill_cf_sleep=args.refill_cf_sleep,
        refill_capex_only=args.refill_capex_only,
        refill_missing_cf=args.refill_cf_missing,
        refill_pl_bs=args.refill_pl_bs,
        refill_pl_bs_limit=args.refill_pl_bs_limit,
        refill_pl_bs_sleep=args.refill_pl_bs_sleep,
        refill_c2=args.refill_c2,
        refill_c2_limit=args.refill_c2_limit,
        refill_c2_sleep=args.refill_c2_sleep,
        refill_machinery=args.refill_machinery,
        refill_machinery_limit=args.refill_machinery_limit,
        refill_machinery_sleep=args.refill_machinery_sleep,
        diagnose_cf=args.diagnose_cf,
        diagnose_cf_limit=args.diagnose_cf_limit,
        jquants_days=args.jquants_days,
    ))
