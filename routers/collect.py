"""収集ジョブ管理 API ルーター。

/api/collect/* および /api/scheduler/* エンドポイントを担当。
共有状態（jobs, limiter, RATELIMIT_* 等）は import api 経由で参照し、
テストの monkeypatch.setattr(api, ...) と互換性を保つ。
"""
import logging
import httpx
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

import api
from database import (
    SessionLocal, Company, FinancialRecord, CollectionLog,
    StockPriceWeekly, StockPriceDaily,
)
from collector import (
    run_full_collection, refresh_company, update_market_data,
    collect_stock_price_history, collect_stock_price_history_jquants,
    update_industry_from_jpx, collect_macro_data, reparse_from_raw,
)

router = APIRouter()
log = logging.getLogger(__name__)

SMART_CHUNK_SIZE     = 200   # スマート収集: 1チャンクあたりの企業数増分
SMART_FULL_THRESHOLD = 3500  # この企業数以上は「全社収集完了」と判定し差分収集に切り替え


# ── Pydantic リクエストモデル ──────────────────────────────────────────────

class CollectRequest(BaseModel):
    years_back: int = Field(default=1, ge=1, le=5)
    max_companies: Optional[int] = None
    skip_existing: bool = False

class HistoryCollectRequest(BaseModel):
    years_back:    int           = Field(default=3, ge=1, le=5)
    max_companies: Optional[int] = None
    skip_existing: bool          = True
    backfill:      bool          = False
    force:         bool          = False

class SmartCollectRequest(BaseModel):
    years_back: int = Field(default=3, ge=1, le=5)
    force:      bool = False

class JQuantsCollectRequest(BaseModel):
    days_back: int  = Field(default=14, ge=1, le=730)
    force:     bool = False

class MacroCollectRequest(BaseModel):
    years_back: int  = Field(default=5, ge=1, le=20)
    force:      bool = False

class MarketDataRequest(BaseModel):
    max_companies: Optional[int] = None
    force: bool = False

class ReparseRequest(BaseModel):
    year:        Optional[int] = None
    edinet_code: Optional[str] = None


# ── 内部ヘルパー ──────────────────────────────────────────────────────────

def _reset_stuck_jobs(db: Session, message: str = "ユーザーによる強制リセット") -> int:
    """in-memory フラグと DB の running ジョブを強制 error 扱いに戻す。リセット件数を返す。"""
    st = api.jobs.state(api._COLLECTION)
    st.running = False
    st.cancel_requested = False
    stuck = db.query(CollectionLog).filter(CollectionLog.status == "running").all()
    for job in stuck:
        job.status      = "error"
        job.message     = message
        job.finished_at = datetime.now(timezone.utc)
    if stuck:
        db.commit()
    return len(stuck)


def _start_collection_job(db: Session, job_type: str) -> CollectionLog:
    """running でないと確定した後の定型処理を一手に担う。

    `CollectionLog` の作成・commit・refresh と in-memory フラグの
    `reset_for_run()` をまとめる。running 判定（smart-start の force 分岐含む）と
    BG タスク登録は呼び出し側に残し、エンドポイントごとの差分を吸収する。
    """
    log_obj = CollectionLog(job_type=job_type, status="running")
    db.add(log_obj); db.commit(); db.refresh(log_obj)
    api.jobs.state(api._COLLECTION).reset_for_run()
    return log_obj


# ── XBRL 収集エンドポイント ────────────────────────────────────────────────

@router.post("/api/collect/start")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_collection(
    request: Request, req: CollectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(api.get_db),
):
    if api.RENDER_LIGHT_MODE and not req.skip_existing:
        raise HTTPException(403, "全件収集はローカル環境から実行してください（Render Free プラン制限）")
    if api.jobs.is_running(api._COLLECTION):
        raise HTTPException(400, "収集ジョブが既に実行中です")
    job_type = "incremental" if req.skip_existing else "full"
    log_obj = _start_collection_job(db, job_type)
    background_tasks.add_task(api._run_collection_bg, req.years_back, req.max_companies, log_obj.id, req.skip_existing)
    return {"message": "収集ジョブを開始しました", "log_id": log_obj.id}


@router.post("/api/collect/smart-start")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_smart_collection(
    request: Request, req: SmartCollectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(api.get_db),
):
    if api.jobs.is_running(api._COLLECTION) and not req.force:
        raise HTTPException(400, "収集ジョブが既に実行中です")
    if req.force:
        _reset_stuck_jobs(db, message="ユーザーによる強制再開で上書き")
    log_obj = _start_collection_job(db, "smart")
    background_tasks.add_task(api._run_smart_collection_bg, log_obj.id, req.years_back)
    return {"message": "スマート収集ジョブを開始しました", "log_id": log_obj.id}


@router.post("/api/collect/reset-stuck")
async def reset_stuck_collection(db: Session = Depends(api.get_db)):
    """スタックした収集ジョブを強制リセット"""
    count = _reset_stuck_jobs(db)
    return {"reset_jobs": count, "message": f"{count}件のジョブをリセットしました"}


@router.post("/api/scheduler/run-now")
async def scheduler_run_now(
    background_tasks: BackgroundTasks,
    db: Session = Depends(api.get_db),
):
    """手動差分収集: 過去1年・収集済みスキップ＋成長率/Zスコア＋市場・マクロ更新"""
    if api.jobs.is_running(api._COLLECTION):
        raise HTTPException(400, "収集ジョブが既に実行中です")
    log_obj = _start_collection_job(db, "incremental")
    background_tasks.add_task(api._run_collection_bg, 1, None, log_obj.id, True)
    return {"message": "差分収集を開始しました（過去1年・収集済みスキップ）", "log_id": log_obj.id}


@router.get("/api/collect/status")
async def collection_status(db: Session = Depends(api.get_db)):
    logs = db.query(CollectionLog).order_by(CollectionLog.id.desc()).limit(5).all()
    return {
        "running": api.jobs.is_running(api._COLLECTION),
        "recent_jobs": [
            {"id": l.id, "status": l.status, "started": api._utc_to_jst_str(l.started_at),
             "companies": l.companies_processed, "records": l.records_saved}
            for l in logs
        ]
    }


@router.post("/api/collect/stop")
async def stop_collection():
    if not api.jobs.is_running(api._COLLECTION):
        raise HTTPException(400, "実行中の収集ジョブがありません")
    api.jobs.request_cancel(api._COLLECTION)
    return {"message": "停止リクエストを送信しました。現在処理中の書類完了後に停止します。"}


@router.get("/api/collect/edinet-coverage")
async def edinet_coverage(db: Session = Depends(api.get_db)):
    """DB内企業マスタ vs 財務レコード保有状況の差分サマリー"""
    total_companies = db.query(Company).count()
    with_records    = db.query(FinancialRecord.edinet_code).distinct().count()
    year_stats = (
        db.query(FinancialRecord.year, func.count(func.distinct(FinancialRecord.edinet_code)))
        .filter(FinancialRecord.year >= 2019)
        .group_by(FinancialRecord.year)
        .order_by(FinancialRecord.year.desc())
        .all()
    )
    return {
        "total_companies": total_companies,
        "with_records":    with_records,
        "without_records": total_companies - with_records,
        "coverage_pct":    round(with_records / total_companies * 100, 1) if total_companies else 0,
        "year_coverage":   [{"year": y, "count": c} for y, c in year_stats],
    }


@router.get("/api/collect/market-coverage")
async def market_coverage(db: Session = Depends(api.get_db)):
    """証券コード保有企業のうち株価データ（market_cap）が埋まっている社数"""
    total_with_sec = (
        db.query(Company)
        .filter(Company.sec_code.isnot(None), Company.sec_code != "")
        .count()
    )
    with_market = (
        db.query(FinancialRecord.edinet_code)
        .filter(FinancialRecord.market_cap.isnot(None))
        .distinct()
        .count()
    )
    latest_update = (
        db.query(func.max(FinancialRecord.updated_at))
        .filter(FinancialRecord.stock_price.isnot(None))
        .scalar()
    )
    return {
        "total_with_sec":    total_with_sec,
        "with_market_data":  with_market,
        "without_market_data": max(total_with_sec - with_market, 0),
        "coverage_pct":      round(with_market / total_with_sec * 100, 1) if total_with_sec else 0,
        "latest_update":     api._utc_to_jst_str(latest_update),
    }


@router.get("/api/collect/data-quality")
async def data_quality(db: Session = Depends(api.get_db)):
    """収集データの品質チェックサマリー"""
    from data_quality import run_data_quality_check
    try:
        result = run_data_quality_check(db)
        return result
    except Exception as e:
        log.error("データ品質チェック失敗: %s", e)
        raise HTTPException(500, "データ品質チェックに失敗しました")


@router.post("/api/collect/refresh/{edinet_code}")
@api.limiter.limit(api.RATELIMIT_REFRESH)
async def refresh_single(request: Request, edinet_code: str, background_tasks: BackgroundTasks):
    if not api._EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    background_tasks.add_task(refresh_company, edinet_code)
    return {"message": f"{edinet_code} の再取得を開始しました"}


@router.post("/api/collect/market-data")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_market_data_update(
    request: Request, req: MarketDataRequest,
    background_tasks: BackgroundTasks,
):
    async def body(on_progress, cancel_check):
        db = SessionLocal()
        try:
            await update_market_data(db, req.max_companies, on_progress=on_progress, cancel_check=cancel_check)
        finally:
            db.close()
    api.jobs.start("market", background_tasks, body,
                   busy_message="市場データ更新ジョブが既に実行中です", force=req.force,
                   error_message="[エラー] 市場データ更新中に問題が発生しました（詳細はサーバーログを確認）")
    return {"message": "市場データ更新を開始しました"}


@router.post("/api/collect/market-stop")
async def stop_market_data():
    if not api.jobs.is_running("market"):
        raise HTTPException(400, "実行中の市場データ更新ジョブがありません")
    api.jobs.request_cancel("market")
    return {"message": "市場データ更新の停止リクエストを送信しました"}


@router.get("/api/collect/market-data/status")
async def market_data_status():
    return api.jobs.snapshot("market")


# ── 株価履歴収集 ──────────────────────────────────────────────────────────

@router.post("/api/collect/history/start")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_history_collection(
    request: Request, req: HistoryCollectRequest,
    background_tasks: BackgroundTasks,
):
    if api.RENDER_LIGHT_MODE:
        raise HTTPException(403, "株価履歴収集はローカル環境から実行してください（Render Free プラン制限）")

    async def body(on_progress, cancel_check):
        db = SessionLocal()
        try:
            await collect_stock_price_history(
                db, req.years_back, req.max_companies,
                on_progress=on_progress, cancel_check=cancel_check,
                skip_existing=req.skip_existing, backfill=req.backfill,
            )
        finally:
            db.close()

    api.jobs.start("history", background_tasks, body,
                   busy_message="株価履歴収集ジョブが既に実行中です", force=req.force,
                   error_message="[エラー] 株価履歴収集中に問題が発生しました（詳細はサーバーログを確認）")
    return {"message": "株価履歴収集を開始しました"}


@router.post("/api/collect/history/stop")
async def stop_history_collection():
    if not api.jobs.is_running("history"):
        raise HTTPException(400, "実行中の株価履歴収集ジョブがありません")
    api.jobs.request_cancel("history")
    return {"message": "株価履歴収集の停止リクエストを送信しました"}


@router.get("/api/collect/history/status")
async def history_collection_status():
    return api.jobs.snapshot("history")


@router.get("/api/collect/history/coverage")
async def history_coverage(db: Session = Depends(api.get_db)):
    """収集済み株価（全履歴の weekly 基準）の社数・レコード数・最古/最新日付を返す"""
    total_companies = db.query(StockPriceWeekly.edinet_code).distinct().count()
    total_records   = db.query(func.count(StockPriceWeekly.edinet_code)).scalar() or 0
    oldest_date     = db.query(func.min(StockPriceWeekly.trade_date)).scalar()
    newest_date     = db.query(func.max(StockPriceWeekly.trade_date)).scalar()
    return {
        "companies":   total_companies,
        "records":     total_records,
        "oldest_date": oldest_date,
        "newest_date": newest_date,
    }


# ── SSE ストリーム（ジョブ進捗配信）─────────────────────────────────────────

for _sse_path, _sse_key in {
    "/api/collect/stream":         "collection",
    "/api/collect/market-stream":  "market",
    "/api/collect/history/stream": "history",
    "/api/collect/reparse/stream": "reparse",
    "/api/collect/jquants/stream": "jquants",
    "/api/collect/macro/stream":   "macro",
}.items():
    def _make_sse(k=_sse_key):
        async def _sse_handler():
            return api.jobs.stream(k)
        return _sse_handler
    router.get(_sse_path)(_make_sse())


# ── 再解析（EDINET通信なし）────────────────────────────────────────────────

@router.post("/api/collect/reparse/start")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_reparse(
    request: Request, req: ReparseRequest,
    background_tasks: BackgroundTasks,
):
    async def body(on_progress, cancel_check):
        await reparse_from_raw(
            year=req.year,
            edinet_code=req.edinet_code,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )
    api.jobs.start("reparse", background_tasks, body,
                   busy_message="再解析ジョブが既に実行中です",
                   error_message="[エラー] 再解析中に問題が発生しました")
    return {"message": "再解析ジョブを開始しました"}


@router.post("/api/collect/reparse/cancel")
async def cancel_reparse():
    api.jobs.request_cancel("reparse")
    return {"message": "停止リクエストを送信しました"}


@router.post("/api/collect/industry")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def collect_industry(request: Request, db: Session = Depends(api.get_db)):
    async with httpx.AsyncClient(timeout=60) as client:
        updated_co, updated_fr = await update_industry_from_jpx(client, db)
    return {"updated_companies": updated_co, "updated_records": updated_fr}


# ── J-Quants 収集 ────────────────────────────────────────────────────────

@router.post("/api/collect/jquants/start")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_jquants_collection(
    request: Request, req: JQuantsCollectRequest,
    background_tasks: BackgroundTasks,
):
    if api.RENDER_LIGHT_MODE:
        raise HTTPException(403, "J-Quants収集はローカル環境から実行してください（Render Free プラン制限）")

    async def body(on_progress, cancel_check):
        db = SessionLocal()
        try:
            await collect_stock_price_history_jquants(
                db, req.days_back, on_progress=on_progress, cancel_check=cancel_check,
            )
        except ValueError as e:
            api.jobs.state("jquants").append_log(f"[設定エラー] {e}")
        finally:
            db.close()

    api.jobs.start("jquants", background_tasks, body,
                   busy_message="J-Quants収集ジョブが既に実行中です", force=req.force,
                   error_message="[エラー] 収集中に問題が発生しました")
    return {"message": "J-Quants収集を開始しました"}


@router.post("/api/collect/jquants/stop")
async def stop_jquants_collection():
    if not api.jobs.is_running("jquants"):
        raise HTTPException(400, "実行中のJ-Quants収集ジョブがありません")
    api.jobs.request_cancel("jquants")
    return {"message": "J-Quants収集の停止リクエストを送信しました"}


@router.get("/api/collect/jquants/status")
async def jquants_collection_status():
    return api.jobs.snapshot("jquants")


# ── マクロデータ収集 ──────────────────────────────────────────────────────

@router.post("/api/collect/macro/start")
@api.limiter.limit(api.RATELIMIT_COLLECT)
async def start_macro_collection(
    request: Request, req: MacroCollectRequest,
    background_tasks: BackgroundTasks,
):
    async def body(on_progress, cancel_check):
        db = SessionLocal()
        try:
            await collect_macro_data(db, req.years_back, on_progress=on_progress, cancel_check=cancel_check)
        finally:
            db.close()

    api.jobs.start("macro", background_tasks, body,
                   busy_message="マクロ収集ジョブが既に実行中です", force=req.force,
                   error_message="[エラー] マクロ収集中に問題が発生しました（詳細はサーバーログを確認）")
    return {"message": "マクロデータ収集を開始しました"}


@router.post("/api/collect/macro/stop")
async def stop_macro_collection():
    if not api.jobs.is_running("macro"):
        raise HTTPException(400, "実行中のマクロ収集ジョブがありません")
    api.jobs.request_cancel("macro")
    return {"message": "マクロ収集の停止リクエストを送信しました"}


@router.get("/api/collect/macro/status")
async def macro_collection_status():
    return api.jobs.snapshot("macro")
