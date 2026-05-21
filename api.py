"""
FastAPI バックエンド
- 収集ジョブ管理（非同期バックグラウンド実行）
- 財務データ検索・スクリーニング API
- 回帰分析・予測株価 API
- フロントエンド（Phase1-2/3-4 HTML）への CORS 対応 REST API
"""

import asyncio, json, logging, re
import hmac, hashlib, base64, time as _time, os
import httpx

log = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from datetime import date, datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session
import io, csv

BASE_DIR = Path(__file__).parent

# ── 認証設定 ──────────────────────────────────────────────────────────────
# APP_PASSWORD が空の場合は認証なし（開発モード）
APP_PASSWORD     = os.getenv("APP_PASSWORD", "")
APP_RECOVERY_KEY = os.getenv("APP_RECOVERY_KEY", "")
APP_SECRET_KEY   = os.getenv("APP_SECRET_KEY", "")
if not APP_SECRET_KEY:
    import warnings as _warnings
    APP_SECRET_KEY = "dev-secret-key-DO-NOT-USE-IN-PRODUCTION"
    _warnings.warn(
        "APP_SECRET_KEY が未設定です。本番デプロイ前に必ず .env に設定してください。",
        stacklevel=1,
    )
_TOKEN_TTL      = 30 * 24 * 3600  # トークン有効期限: 30日

def _create_token() -> str:
    ts  = str(int(_time.time()))
    sig = hmac.new(APP_SECRET_KEY.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{ts}:{sig}".encode()).decode()

def _verify_token(token: str) -> bool:
    if not APP_PASSWORD:
        return True
    try:
        raw     = base64.urlsafe_b64decode(token.encode()).decode()
        ts, sig = raw.rsplit(":", 1)
        expected = hmac.new(APP_SECRET_KEY.encode(), ts.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and _time.time() - int(ts) < _TOKEN_TTL
    except Exception:
        return False

from database import (
    SessionLocal, init_db,
    Company, FinancialRecord, CollectionLog, StockPriceHistory, MacroData,
    calc_growth_rates, calc_zscore_normalization,
)
from collector import run_full_collection, refresh_company, update_market_data, collect_stock_price_history, collect_stock_price_history_jquants, update_industry_from_jpx, collect_macro_data, MACRO_SERIES
import plugins as plugin_registry


SCHEDULER_RUN_HOUR = 3  # 毎日この時刻（サーバーローカル時刻）に自動実行

_scheduler_status: dict = {
    "enabled":     True,
    "next_run":    None,
    "last_run":    None,
    "last_status": None,
}

async def _daily_scheduler():
    """毎日指定時刻に差分収集＋株価更新を自動実行するバックグラウンドタスク"""
    while True:
        now      = datetime.now()
        next_run = now.replace(hour=SCHEDULER_RUN_HOUR, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        _scheduler_status["next_run"] = next_run.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep((next_run - datetime.now()).total_seconds())

        if not _scheduler_status["enabled"]:
            continue

        if _job_status["running"]:
            _scheduler_status["last_status"] = "スキップ（手動収集が実行中）"
            continue

        _scheduler_status["last_run"]    = datetime.now().strftime("%Y-%m-%d %H:%M")
        _scheduler_status["last_status"] = "実行中"
        _job_status.update({"running": True, "log": [], "progress": 0, "job_type": "incremental"})
        try:
            await run_full_collection(years_back=1, skip_existing=True)
            db = SessionLocal()
            calc_growth_rates(db)
            calc_zscore_normalization(db)
            db.close()
            await update_market_data()
            # マクロデータ（為替・金利・指数・コモディティ）も毎日更新
            db2 = SessionLocal()
            try:
                await collect_macro_data(db2, years_back=5)
            finally:
                db2.close()
            _scheduler_status["last_status"] = "成功"
        except Exception as e:
            log.error("スケジューラーエラー: %s", e, exc_info=True)
            _scheduler_status["last_status"] = "エラー（詳細はサーバーログを確認）"
        finally:
            _job_status["running"] = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # サーバー再起動時: 前回クラッシュで残った "running" ジョブを "error" にリセット
    db = SessionLocal()
    try:
        stuck = db.query(CollectionLog).filter(CollectionLog.status == "running").all()
        for job in stuck:
            job.status = "error"
            job.message = "サーバー再起動により中断"
            job.finished_at = datetime.utcnow()
        if stuck:
            db.commit()
            log.warning("起動時に %d 件のスタックジョブを error にリセットしました", len(stuck))
    finally:
        db.close()
    scheduler_task = asyncio.create_task(_daily_scheduler())
    yield
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="EDINET Financial API", version="2.0", lifespan=lifespan)

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "http://localhost:8000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """セキュリティ関連レスポンスヘッダーを全レスポンスに付与する"""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

app.add_middleware(_SecurityHeadersMiddleware)

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """APP_PASSWORD が設定されている場合、/api/* を Bearer トークンで保護する"""
    if not APP_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/login" or path.startswith("/api/auth/"):
        return await call_next(request)
    if path.startswith("/api/"):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "認証が必要です"}, status_code=401)
        if not _verify_token(auth[7:]):
            return JSONResponse({"detail": "トークンが無効または期限切れです"}, status_code=401)
    return await call_next(request)

# DB セッション依存性
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── 収集ジョブ管理 ────────────────────────────────────────────────────────

SMART_CHUNK_SIZE     = 200   # スマート収集: 1チャンクあたりの企業数増分
SMART_FULL_THRESHOLD = 3500  # この企業数以上は「全社収集完了」と判定し差分収集に切り替え

_job_status: dict = {"running": False, "log": [], "progress": 0, "total": 0, "job_type": "", "cancel_requested": False}
_market_status: dict = {"running": False, "progress": 0, "total": 0, "log": [], "cancel_requested": False}
_history_status: dict  = {"running": False, "progress": 0, "total": 0, "log": [], "cancel_requested": False}
_jquants_status: dict  = {"running": False, "progress": 0, "total": 0, "log": [], "cancel_requested": False}
_macro_status:   dict  = {"running": False, "progress": 0, "total": 0, "log": [], "cancel_requested": False}

class CollectRequest(BaseModel):
    years_back: int = Field(default=1, ge=1, le=5)
    max_companies: Optional[int] = None   # None=全社
    skip_existing: bool = False           # True=差分収集（収集済みdoc_idをスキップ）

class HistoryCollectRequest(BaseModel):
    years_back:    int           = Field(default=3, ge=1, le=5)
    max_companies: Optional[int] = None
    skip_existing: bool          = True   # True=差分収集（収集済み企業をスキップ）
    backfill:      bool          = False  # True=前方差分に加え後方欠損（years_back起点→最古前日）も補完
    force:         bool          = False  # True=実行中フラグを無視して強制再起動

class SmartCollectRequest(BaseModel):
    years_back: int = Field(default=3, ge=1, le=5)

class JQuantsCollectRequest(BaseModel):
    days_back: int  = Field(default=14, ge=1, le=730)
    force:     bool = False

@app.post("/api/collect/start")
async def start_collection(req: CollectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if _job_status["running"]:
        raise HTTPException(400, "収集ジョブが既に実行中です")
    job_type = "incremental" if req.skip_existing else "full"
    log_obj = CollectionLog(job_type=job_type, status="running")
    db.add(log_obj); db.commit(); db.refresh(log_obj)
    _job_status.update({"running": True, "log": [], "progress": 0, "log_id": log_obj.id})
    background_tasks.add_task(_run_collection_bg, req.years_back, req.max_companies, log_obj.id, req.skip_existing)
    return {"message": "収集ジョブを開始しました", "log_id": log_obj.id}

async def _run_collection_bg(years: int, max_co: Optional[int], log_id: int, skip_existing: bool = False):
    _job_status["cancel_requested"] = False
    _prog_ticks = [0]

    db = SessionLocal()

    def on_progress(current, total, msg):
        _job_status["progress"] = current
        _job_status["total"]    = total
        _job_status["log"].append(msg)
        if len(_job_status["log"]) > 500:
            _job_status["log"] = _job_status["log"][-500:]
        _prog_ticks[0] += 1
        if _prog_ticks[0] % 10 == 0:
            try:
                obj = db.get(CollectionLog, log_id)
                if obj:
                    obj.companies_processed = current
                    db.commit()
            except Exception:
                db.rollback()

    def cancel_check():
        return _job_status.get("cancel_requested", False)

    try:
        cancelled = await run_full_collection(years, max_co, on_progress=on_progress,
                                              skip_existing=skip_existing, cancel_check=cancel_check)
        if not cancelled:
            calc_growth_rates(db)
            calc_zscore_normalization(db)
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status = "done"
            log_obj.finished_at = datetime.utcnow()
            if cancelled:
                log_obj.message = "ユーザーにより停止"
            db.commit()
    except Exception as e:
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status = "error"
            log_obj.message = str(e)
            log_obj.finished_at = datetime.utcnow()
            db.commit()
    finally:
        _job_status["running"] = False
        _job_status["cancel_requested"] = False
        db.close()

async def _run_smart_collection_bg(log_id: int, years: int):
    _job_status["cancel_requested"] = False
    _prog_ticks = [0]
    db = SessionLocal()

    def on_progress(current, total, msg):
        _job_status["progress"] = current
        _job_status["total"]    = total
        _job_status["log"].append(msg)
        if len(_job_status["log"]) > 500:
            _job_status["log"] = _job_status["log"][-500:]
        _prog_ticks[0] += 1
        if _prog_ticks[0] % 10 == 0:
            try:
                obj = db.get(CollectionLog, log_id)
                if obj:
                    obj.companies_processed = current
                    db.commit()
            except Exception:
                db.rollback()

    def cancel_check():
        return _job_status.get("cancel_requested", False)

    try:
        company_count = db.query(Company).count()

        if company_count >= SMART_FULL_THRESHOLD:
            _job_status["log"].append(
                f"[スマート判定] DB企業数={company_count}社 → 差分収集モード（過去{years}年）"
            )
            cancelled = await run_full_collection(
                years, None, on_progress=on_progress,
                skip_existing=True, cancel_check=cancel_check
            )
        elif company_count == 0:
            _job_status["log"].append(
                f"[スマート判定] DB企業数=0社 → 初回チャンク収集（先着{SMART_CHUNK_SIZE}社）"
            )
            cancelled = await run_full_collection(
                years, SMART_CHUNK_SIZE, on_progress=on_progress,
                skip_existing=False, cancel_check=cancel_check
            )
        else:
            chunk_no = (company_count // SMART_CHUNK_SIZE) + 1
            target   = chunk_no * SMART_CHUNK_SIZE
            _job_status["log"].append(
                f"[スマート判定] DB企業数={company_count}社 → "
                f"チャンク{chunk_no}（先着{target}社のうち未収集を処理）"
            )
            cancelled = await run_full_collection(
                years, target, on_progress=on_progress,
                skip_existing=True, cancel_check=cancel_check
            )

        if not cancelled:
            calc_growth_rates(db)
            calc_zscore_normalization(db)

        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status      = "done"
            log_obj.finished_at = datetime.utcnow()
            if cancelled:
                log_obj.message = "ユーザーにより停止"
            db.commit()

    except Exception as e:
        log.error("スマート収集エラー: %s", e, exc_info=True)
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status      = "error"
            log_obj.message     = str(e)
            log_obj.finished_at = datetime.utcnow()
            db.commit()
    finally:
        _job_status["running"]          = False
        _job_status["cancel_requested"] = False
        db.close()

@app.post("/api/collect/smart-start")
async def start_smart_collection(
    req: SmartCollectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if _job_status["running"]:
        raise HTTPException(400, "収集ジョブが既に実行中です")
    log_obj = CollectionLog(job_type="smart", status="running")
    db.add(log_obj); db.commit(); db.refresh(log_obj)
    _job_status.update({"running": True, "log": [], "progress": 0, "log_id": log_obj.id, "job_type": "smart"})
    background_tasks.add_task(_run_smart_collection_bg, log_obj.id, req.years_back)
    return {"message": "スマート収集ジョブを開始しました", "log_id": log_obj.id}

@app.get("/api/scheduler/status")
async def get_scheduler_status():
    return _scheduler_status

@app.post("/api/scheduler/toggle")
async def toggle_scheduler():
    _scheduler_status["enabled"] = not _scheduler_status["enabled"]
    return {"enabled": _scheduler_status["enabled"]}

@app.post("/api/scheduler/run-now")
async def scheduler_run_now(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """スケジューラーと同じ差分収集を即時実行"""
    if _job_status["running"]:
        raise HTTPException(400, "収集ジョブが既に実行中です")
    log_obj = CollectionLog(job_type="incremental", status="running")
    db.add(log_obj); db.commit(); db.refresh(log_obj)
    _job_status.update({"running": True, "log": [], "progress": 0, "log_id": log_obj.id})
    background_tasks.add_task(_run_collection_bg, 1, None, log_obj.id, True)
    return {"message": "差分収集を開始しました（過去1年・収集済みスキップ）", "log_id": log_obj.id}

@app.get("/api/collect/status")
async def collection_status(db: Session = Depends(get_db)):
    logs = db.query(CollectionLog).order_by(CollectionLog.id.desc()).limit(5).all()
    return {
        "running": _job_status["running"],
        "recent_jobs": [
            {"id": l.id, "status": l.status, "started": str(l.started_at),
             "companies": l.companies_processed, "records": l.records_saved}
            for l in logs
        ]
    }

@app.post("/api/collect/stop")
async def stop_collection():
    if not _job_status["running"]:
        raise HTTPException(400, "実行中の収集ジョブがありません")
    _job_status["cancel_requested"] = True
    return {"message": "停止リクエストを送信しました。現在処理中の書類完了後に停止します。"}

@app.get("/api/collect/edinet-coverage")
async def edinet_coverage(db: Session = Depends(get_db)):
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

@app.get("/api/collect/market-coverage")
async def market_coverage(db: Session = Depends(get_db)):
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
        "latest_update":     str(latest_update)[:19] if latest_update else None,
    }

@app.get("/api/collect/data-quality")
async def data_quality(db: Session = Depends(get_db)):
    """収集データの品質チェックサマリー"""
    from checker import run_data_quality_check
    try:
        result = run_data_quality_check(db)
        return result
    except Exception as e:
        log.error(f"データ品質チェック失敗: {e}")
        raise HTTPException(500, "データ品質チェックに失敗しました")

_EDINET_CODE_RE = re.compile(r"^E\d{6}$")

@app.post("/api/collect/refresh/{edinet_code}")
async def refresh_single(edinet_code: str, background_tasks: BackgroundTasks):
    if not _EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E123456）")
    background_tasks.add_task(refresh_company, edinet_code)
    return {"message": f"{edinet_code} の再取得を開始しました"}


class MarketDataRequest(BaseModel):
    max_companies: Optional[int] = None
    force: bool = False  # True=実行中フラグを無視して強制再起動

@app.post("/api/collect/market-data")
async def start_market_data_update(req: MarketDataRequest, background_tasks: BackgroundTasks):
    if _market_status["running"] and not req.force:
        raise HTTPException(400, "市場データ更新ジョブが既に実行中です")
    _market_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _market_status["progress"] = current
        _market_status["total"]    = total
        _market_status["log"].append(msg)

    def cancel_check():
        return _market_status.get("cancel_requested", False)

    async def _run():
        try:
            await update_market_data(req.max_companies, on_progress=on_progress, cancel_check=cancel_check)
        finally:
            _market_status["running"] = False
            _market_status["cancel_requested"] = False

    background_tasks.add_task(_run)
    return {"message": "市場データ更新を開始しました"}

@app.post("/api/collect/market-stop")
async def stop_market_data():
    if not _market_status["running"]:
        raise HTTPException(400, "実行中の市場データ更新ジョブがありません")
    _market_status["cancel_requested"] = True
    return {"message": "市場データ更新の停止リクエストを送信しました"}

@app.get("/api/collect/market-data/status")
async def market_data_status():
    return {
        "running":  _market_status["running"],
        "progress": _market_status["progress"],
        "total":    _market_status["total"],
        "recent_logs": _market_status["log"][-20:],
    }


# ── 株価履歴収集 ──────────────────────────────────────────────────────────

@app.post("/api/collect/history/start")
async def start_history_collection(req: HistoryCollectRequest, background_tasks: BackgroundTasks):
    if _history_status["running"] and not req.force:
        raise HTTPException(400, "株価履歴収集ジョブが既に実行中です")
    _history_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _history_status["progress"] = current
        _history_status["total"]    = total
        _history_status["log"].append(msg)

    def cancel_check():
        return _history_status.get("cancel_requested", False)

    async def _run():
        db = SessionLocal()
        try:
            await collect_stock_price_history(
                db, req.years_back, req.max_companies,
                on_progress=on_progress, cancel_check=cancel_check,
                skip_existing=req.skip_existing, backfill=req.backfill,
            )
        except Exception as e:
            log.error(f"株価履歴収集エラー: {e}")
            _history_status["log"].append(f"[エラー] {e}")
        finally:
            _history_status["running"] = False
            _history_status["cancel_requested"] = False
            db.close()

    background_tasks.add_task(_run)
    return {"message": "株価履歴収集を開始しました"}

@app.post("/api/collect/history/stop")
async def stop_history_collection():
    if not _history_status["running"]:
        raise HTTPException(400, "実行中の株価履歴収集ジョブがありません")
    _history_status["cancel_requested"] = True
    return {"message": "株価履歴収集の停止リクエストを送信しました"}

@app.get("/api/collect/history/status")
async def history_collection_status():
    return {
        "running":     _history_status["running"],
        "progress":    _history_status["progress"],
        "total":       _history_status["total"],
        "recent_logs": _history_status["log"][-20:],
    }

@app.get("/api/collect/history/coverage")
async def history_coverage(db: Session = Depends(get_db)):
    """収集済み株価履歴の社数・レコード数・最古日付を返す"""
    total_companies = db.query(StockPriceHistory.edinet_code).distinct().count()
    total_records   = db.query(func.count(StockPriceHistory.id)).scalar() or 0
    oldest_date     = db.query(func.min(StockPriceHistory.trade_date)).scalar()
    newest_date     = db.query(func.max(StockPriceHistory.trade_date)).scalar()
    return {
        "companies":   total_companies,
        "records":     total_records,
        "oldest_date": oldest_date,
        "newest_date": newest_date,
    }

@app.get("/api/stock/history/{edinet_code}")
async def get_stock_history(edinet_code: str, days: int = 365, db: Session = Depends(get_db)):
    """指定企業の日次 OHLCV を最新 days 日分返す"""
    rows = (
        db.query(StockPriceHistory)
        .filter(StockPriceHistory.edinet_code == edinet_code)
        .order_by(StockPriceHistory.trade_date.desc())
        .limit(days)
        .all()
    )
    return [
        {
            "trade_date": r.trade_date,
            "open":       r.open,
            "high":       r.high,
            "low":        r.low,
            "close":      r.close,
            "volume":     r.volume,
        }
        for r in reversed(rows)
    ]


async def _sse_stream(status: dict):
    """SSE共通ジェネレータ。status辞書を監視してリアルタイム配信する"""
    sent = 0
    while True:
        new_logs = status["log"][sent:]
        sent += len(new_logs)
        data = {
            "running":  status["running"],
            "progress": status["progress"],
            "total":    status["total"],
            "new_logs": new_logs,
        }
        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        if not status["running"]:
            break
        await asyncio.sleep(1)

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

@app.get("/api/collect/stream")
async def progress_stream():
    return StreamingResponse(_sse_stream(_job_status), media_type="text/event-stream", headers=_SSE_HEADERS)

@app.get("/api/collect/market-stream")
async def market_progress_stream():
    return StreamingResponse(_sse_stream(_market_status), media_type="text/event-stream", headers=_SSE_HEADERS)

@app.get("/api/collect/history/stream")
async def history_progress_stream():
    return StreamingResponse(_sse_stream(_history_status), media_type="text/event-stream", headers=_SSE_HEADERS)

@app.post("/api/collect/industry")
async def collect_industry(db: Session = Depends(get_db)):
    async with httpx.AsyncClient() as client:
        updated_co, updated_fr = await update_industry_from_jpx(client, db)
    return {"updated_companies": updated_co, "updated_records": updated_fr}


@app.post("/api/collect/jquants/start")
async def start_jquants_collection(req: JQuantsCollectRequest, background_tasks: BackgroundTasks):
    if _jquants_status["running"] and not req.force:
        raise HTTPException(400, "J-Quants収集ジョブが既に実行中です")
    _jquants_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _jquants_status["progress"] = current
        _jquants_status["total"]    = total
        _jquants_status["log"].append(msg)

    def cancel_check():
        return _jquants_status.get("cancel_requested", False)

    async def _run():
        db = SessionLocal()
        try:
            await collect_stock_price_history_jquants(
                db, req.days_back, on_progress=on_progress, cancel_check=cancel_check,
            )
        except ValueError as e:
            _jquants_status["log"].append(f"[設定エラー] {e}")
        except Exception as e:
            log.error(f"J-Quants収集エラー: {e}")
            _jquants_status["log"].append(f"[エラー] 収集中に問題が発生しました")
        finally:
            _jquants_status["running"] = False
            _jquants_status["cancel_requested"] = False
            db.close()

    background_tasks.add_task(_run)
    return {"message": "J-Quants収集を開始しました"}

@app.post("/api/collect/jquants/stop")
async def stop_jquants_collection():
    if not _jquants_status["running"]:
        raise HTTPException(400, "実行中のJ-Quants収集ジョブがありません")
    _jquants_status["cancel_requested"] = True
    return {"message": "J-Quants収集の停止リクエストを送信しました"}

@app.get("/api/collect/jquants/status")
async def jquants_collection_status():
    return {
        "running":     _jquants_status["running"],
        "progress":    _jquants_status["progress"],
        "total":       _jquants_status["total"],
        "recent_logs": _jquants_status["log"][-20:],
    }

@app.get("/api/collect/jquants/stream")
async def jquants_progress_stream():
    return StreamingResponse(_sse_stream(_jquants_status), media_type="text/event-stream", headers=_SSE_HEADERS)


# ── マクロデータ収集（為替・金利・指数・コモディティ）─────────────────

class MacroCollectRequest(BaseModel):
    years_back: int  = Field(default=5, ge=1, le=20)
    force:      bool = False

@app.post("/api/collect/macro/start")
async def start_macro_collection(req: MacroCollectRequest, background_tasks: BackgroundTasks):
    if _macro_status["running"] and not req.force:
        raise HTTPException(400, "マクロ収集ジョブが既に実行中です")
    _macro_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _macro_status["progress"] = current
        _macro_status["total"]    = total
        _macro_status["log"].append(msg)

    def cancel_check():
        return _macro_status.get("cancel_requested", False)

    async def _run():
        db = SessionLocal()
        try:
            await collect_macro_data(
                db, req.years_back, on_progress=on_progress, cancel_check=cancel_check,
            )
        except Exception as e:
            log.error(f"マクロ収集エラー: {e}")
            _macro_status["log"].append(f"[エラー] {e}")
        finally:
            _macro_status["running"] = False
            _macro_status["cancel_requested"] = False
            db.close()

    background_tasks.add_task(_run)
    return {"message": "マクロデータ収集を開始しました"}

@app.post("/api/collect/macro/stop")
async def stop_macro_collection():
    if not _macro_status["running"]:
        raise HTTPException(400, "実行中のマクロ収集ジョブがありません")
    _macro_status["cancel_requested"] = True
    return {"message": "マクロ収集の停止リクエストを送信しました"}

@app.get("/api/collect/macro/status")
async def macro_collection_status():
    return {
        "running":     _macro_status["running"],
        "progress":    _macro_status["progress"],
        "total":       _macro_status["total"],
        "recent_logs": _macro_status["log"][-20:],
    }

@app.get("/api/collect/macro/stream")
async def macro_progress_stream():
    return StreamingResponse(_sse_stream(_macro_status), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/api/macro/series")
async def list_macro_series(db: Session = Depends(get_db)):
    """マクロ系列のカバレッジ一覧（系列ごとの件数・最新日・最古日）"""
    rows = (
        db.query(
            MacroData.series_code,
            MacroData.series_name,
            MacroData.category,
            func.count(MacroData.id).label("rows"),
            func.min(MacroData.trade_date).label("oldest"),
            func.max(MacroData.trade_date).label("newest"),
        )
        .group_by(MacroData.series_code, MacroData.series_name, MacroData.category)
        .all()
    )
    by_code = {r.series_code: r for r in rows}
    items = []
    for s in MACRO_SERIES:
        r = by_code.get(s["code"])
        items.append({
            "code":     s["code"],
            "name":     s["name"],
            "category": s["category"],
            "ticker":   s["ticker"],
            "rows":     int(r.rows) if r else 0,
            "oldest":   r.oldest if r else None,
            "newest":   r.newest if r else None,
        })
    return {"series": items}


@app.get("/api/macro/data/{series_code}")
async def get_macro_data(series_code: str, days: int = 365, db: Session = Depends(get_db)):
    """指定系列の日次データを最新 days 日分返す"""
    # ホワイトリスト検証（MACRO_SERIES に定義されたコードのみ受理）
    if series_code not in {s["code"] for s in MACRO_SERIES}:
        raise HTTPException(404, "未知の系列コードです")
    if not (1 <= days <= 10000):
        raise HTTPException(400, "days は 1〜10000 の範囲で指定してください")
    rows = (
        db.query(MacroData)
        .filter(MacroData.series_code == series_code)
        .order_by(MacroData.trade_date.desc())
        .limit(days)
        .all()
    )
    return {
        "series_code": series_code,
        "rows": [
            {"trade_date": r.trade_date, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in reversed(rows)
        ],
    }


# ── 統計サマリー ─────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    n_companies   = db.query(Company).count()
    n_records     = db.query(FinancialRecord).count()
    n_stock_price = db.query(func.count(StockPriceHistory.id)).scalar() or 0
    latest_year   = db.query(FinancialRecord.year).order_by(FinancialRecord.year.desc()).first()
    return {
        "companies":           n_companies,
        "records":             n_records,
        "stock_price_records": n_stock_price,
        "latest_year":         latest_year[0] if latest_year else None,
    }


# ── 企業検索 ─────────────────────────────────────────────────────────────

@app.get("/api/companies")
async def list_companies(
    q: Optional[str] = None,
    industry: Optional[str] = None,
    market: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    include_latest: bool = False,
    db: Session = Depends(get_db)
):
    query = db.query(Company)
    if q:
        query = query.filter(Company.name.ilike(f"%{q}%") | Company.sec_code.ilike(f"%{q}%"))
    if industry:
        query = query.filter(Company.industry == industry)
    if market:
        query = query.filter(Company.market == market)
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [{"edinet_code": c.edinet_code, "sec_code": c.sec_code,
              "name": c.name, "industry": c.industry, "market": c.market}
             for c in rows]
    if include_latest and rows:
        codes = [c.edinet_code for c in rows]
        subq = (
            db.query(FinancialRecord.edinet_code, func.max(FinancialRecord.year).label("max_year"))
            .filter(FinancialRecord.edinet_code.in_(codes))
            .group_by(FinancialRecord.edinet_code)
            .subquery()
        )
        latest_recs = (
            db.query(FinancialRecord)
            .join(subq, (FinancialRecord.edinet_code == subq.c.edinet_code) &
                        (FinancialRecord.year == subq.c.max_year))
            .all()
        )
        latest_map = {r.edinet_code: _record_to_dict(r) for r in latest_recs}
        for item in items:
            item["latest"] = latest_map.get(item["edinet_code"])
    return {"total": total, "items": items}


# ── 財務データ取得 ────────────────────────────────────────────────────────

@app.get("/api/financials/{edinet_code}")
async def get_financials(edinet_code: str, db: Session = Depends(get_db)):
    records = (db.query(FinancialRecord)
               .filter_by(edinet_code=edinet_code)
               .order_by(FinancialRecord.year)
               .all())
    if not records:
        raise HTTPException(404, "データが見つかりません")
    return {"edinet_code": edinet_code, "records": [_record_to_dict(r) for r in records]}


def _record_to_dict(r: FinancialRecord) -> dict:
    return {
        "edinet_code":  r.edinet_code,
        "sec_code":     r.sec_code,
        "company_name": r.company_name,
        "industry":     r.industry,
        "year": r.year, "period_end": r.period_end,
        "bs": {
            "total_assets": r.bs_total_assets,
            "current_assets": r.bs_current_assets,
            "noncurrent_assets": r.bs_noncurrent_assets,
            "cash": r.bs_cash,
            "total_liabilities": r.bs_total_liabilities,
            "total_equity": r.bs_total_equity,
            "equity_parent": r.bs_equity_parent,
            "short_term_debt": r.bs_short_term_debt,
            "long_term_debt": r.bs_long_term_debt,
            "bps": r.bs_bps,
            "equity_ratio": r.equity_ratio,
        },
        "pl": {
            "revenue": r.pl_revenue,
            "gross_profit": r.pl_gross_profit,
            "operating_profit": r.pl_operating_profit,
            "ordinary_profit": r.pl_ordinary_profit,
            "net_income": r.pl_net_income,
            "eps": r.pl_eps,
            "ebitda": r.pl_ebitda,
            "op_margin": r.op_margin,
            "net_margin": r.net_margin,
            "rev_growth": r.rev_growth,
            "eps_growth": r.eps_growth,
        },
        "cf": {
            "operating_cf": r.cf_operating_cf,
            "investing_cf": r.cf_investing_cf,
            "financing_cf": r.cf_financing_cf,
            "free_cf": r.cf_free_cf,
            "capex": r.cf_capex,
            "cf_ratio": r.cf_ratio,
        },
        "val": {
            "market_cap": r.market_cap,
            "stock_price": r.stock_price,
            "per": r.per, "pbr": r.pbr,
            "div_yield": r.div_yield,
            "roe": r.roe, "roa": r.roa,
        },
        "zscore": {
            "z_revenue": r.z_revenue,
            "z_op_margin": r.z_op_margin,
            "z_roe": r.z_roe,
            "z_equity_ratio": r.z_equity_ratio,
            "z_cf_ratio": r.z_cf_ratio,
            "z_eps": r.z_eps,
        },
        "predicted_market_cap": r.predicted_market_cap,
        "gap_ratio": r.gap_ratio,
    }


# ── スクリーニング API ──────────────────────────────────────────────────

class ScreenRequest(BaseModel):
    year: Optional[int] = None
    industry: Optional[str] = None
    market: Optional[str] = None
    # PL
    min_rev_growth: Optional[float] = None
    min_op_margin: Optional[float] = None
    min_net_margin: Optional[float] = None
    # BS
    min_roe: Optional[float] = None
    min_roa: Optional[float] = None
    min_equity_ratio: Optional[float] = None
    max_de_ratio: Optional[float] = None
    # Valuation
    max_per: Optional[float] = None
    max_pbr: Optional[float] = None
    min_div_yield: Optional[float] = None
    # CF
    min_cf_ratio: Optional[float] = None
    limit: int = 200

@app.post("/api/screen")
async def screening(req: ScreenRequest, db: Session = Depends(get_db)):
    # 最新年度のレコードのみ対象
    subq = (db.query(FinancialRecord.edinet_code,
                     func.max(FinancialRecord.year).label("max_year"))
              .group_by(FinancialRecord.edinet_code)
              .subquery())
    query = (db.query(FinancialRecord)
               .join(subq, (FinancialRecord.edinet_code == subq.c.edinet_code) &
                           (FinancialRecord.year == subq.c.max_year)))

    if req.year:
        query = query.filter(FinancialRecord.year == req.year)
    if req.industry:
        query = query.filter(FinancialRecord.industry == req.industry)
    if req.market:
        query = query.filter(FinancialRecord.market == req.market)
    if req.min_rev_growth is not None:
        query = query.filter(FinancialRecord.rev_growth >= req.min_rev_growth)
    if req.min_op_margin is not None:
        query = query.filter(FinancialRecord.op_margin >= req.min_op_margin)
    if req.min_net_margin is not None:
        query = query.filter(FinancialRecord.net_margin >= req.min_net_margin)
    if req.min_roe is not None:
        query = query.filter(FinancialRecord.roe >= req.min_roe)
    if req.min_roa is not None:
        query = query.filter(FinancialRecord.roa >= req.min_roa)
    if req.min_equity_ratio is not None:
        query = query.filter(FinancialRecord.equity_ratio >= req.min_equity_ratio)
    if req.max_de_ratio is not None:
        query = query.filter(FinancialRecord.de_ratio <= req.max_de_ratio)
    if req.max_per is not None:
        query = query.filter(FinancialRecord.per <= req.max_per)
    if req.max_pbr is not None:
        query = query.filter(FinancialRecord.pbr <= req.max_pbr)
    if req.min_div_yield is not None:
        query = query.filter(FinancialRecord.div_yield >= req.min_div_yield)
    if req.min_cf_ratio is not None:
        query = query.filter(FinancialRecord.cf_ratio >= req.min_cf_ratio)

    rows = query.limit(req.limit).all()
    return {"count": len(rows), "results": [_record_to_dict(r) for r in rows]}


# ── プラグイン API ──────────────────────────────────────────────────────

@app.get("/api/plugins")
async def list_plugins():
    """利用可能な分析プラグイン一覧とパラメータスキーマを返す"""
    return {"plugins": [p.to_meta() for p in plugin_registry.list_plugins()]}

@app.post("/api/plugins/{plugin_name}/run", response_model=None)
async def run_plugin(plugin_name: str, params: dict, db: Session = Depends(get_db)):
    """指定プラグインを実行する"""
    p = plugin_registry.get_plugin(plugin_name)
    if p is None:
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    try:
        return await p.execute(params, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Plugin '%s' error: %s", plugin_name, e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")

@app.get("/api/gap-analysis")
async def gap_analysis(year: Optional[int] = None, sort: str = "asc", db: Session = Depends(get_db)):
    p = plugin_registry.get_plugin("gap_analysis")
    try:
        return await p.execute({"year": year, "sort": sort}, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        log.error("Gap-analysis error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")

@app.get("/api/recommend/presets")
async def get_recommend_presets():
    from plugins.recommend import PRESETS, METRICS
    return {"presets": PRESETS, "metrics": METRICS}

@app.post("/api/recommend")
async def recommend_stocks(req: dict, db: Session = Depends(get_db)):
    p = plugin_registry.get_plugin("recommend")
    try:
        return await p.execute(req, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Recommend error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


# ── バックテスト共通ロジック ────────────────────────────────────────────

def _bt_percentile(sorted_arr: list, p: float) -> float:
    """pパーセンタイル値（0〜100）を線形補間で返す"""
    n = len(sorted_arr)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_arr[0])
    idx = (n - 1) * p / 100
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return float(sorted_arr[lo] + (sorted_arr[hi] - sorted_arr[lo]) * (idx - lo))


def _backtest_single(
    db: Session,
    preset_name: str,
    months_ago: int,
    top_n: int,
    industry: Optional[str],
    min_market_cap: Optional[float],
) -> dict:
    """バックテストを1期間分実行してdictを返す（例外はそのまま伝播）"""
    from plugins.recommend import PRESETS
    from database import FinancialRecord, StockPriceHistory

    weights = PRESETS.get(preset_name, PRESETS["バランス型"])
    today = date.today()
    start_date = today - timedelta(days=months_ago * 30)
    start_date_str = start_date.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    subq = (
        db.query(FinancialRecord.edinet_code,
                 func.max(FinancialRecord.year).label("max_year"))
        .filter(FinancialRecord.period_end <= start_date_str)
        .group_by(FinancialRecord.edinet_code)
        .subquery()
    )
    query = (
        db.query(FinancialRecord)
        .join(subq, (FinancialRecord.edinet_code == subq.c.edinet_code) &
                    (FinancialRecord.year == subq.c.max_year))
        .filter(FinancialRecord.period_end <= start_date_str)
    )
    if industry:
        query = query.filter(FinancialRecord.industry == industry)
    if min_market_cap is not None:
        query = query.filter(FinancialRecord.market_cap >= float(min_market_cap))
    records = query.all()

    best: dict = {}
    for r in records:
        score, has_any = 0.0, False
        for metric, weight in weights.items():
            val = getattr(r, metric, None)
            if val is not None:
                score += weight * val
                has_any = True
        if not has_any:
            continue
        if r.edinet_code not in best or r.period_end > best[r.edinet_code][1].period_end:
            best[r.edinet_code] = (score, r)

    scored = sorted(best.values(), key=lambda x: x[0], reverse=True)
    if not scored:
        return {
            "start_date": start_date_str, "end_date": today_str,
            "holding_months": months_ago, "top_n": top_n, "preset": preset_name,
            "summary": None, "results": [], "total_candidates": 0,
            "message": f"{start_date_str} 時点の財務データが見つかりませんでした",
        }

    top = scored[:top_n]
    bench_limit = min(500, len(scored))
    bench_codes = [r.edinet_code for _, r in scored[:bench_limit]]

    def _first_prices(codes: list, after: str) -> dict:
        if not codes:
            return {}
        sq = (
            db.query(StockPriceHistory.edinet_code,
                     func.min(StockPriceHistory.trade_date).label("min_date"))
            .filter(StockPriceHistory.edinet_code.in_(codes))
            .filter(StockPriceHistory.trade_date >= after)
            .group_by(StockPriceHistory.edinet_code)
            .subquery()
        )
        rows = (
            db.query(StockPriceHistory.edinet_code,
                     StockPriceHistory.close,
                     StockPriceHistory.trade_date)
            .join(sq, (StockPriceHistory.edinet_code == sq.c.edinet_code) &
                      (StockPriceHistory.trade_date == sq.c.min_date))
            .all()
        )
        return {row.edinet_code: {"price": row.close, "date": row.trade_date}
                for row in rows}

    def _last_prices(codes: list) -> dict:
        if not codes:
            return {}
        sq = (
            db.query(StockPriceHistory.edinet_code,
                     func.max(StockPriceHistory.trade_date).label("max_date"))
            .filter(StockPriceHistory.edinet_code.in_(codes))
            .group_by(StockPriceHistory.edinet_code)
            .subquery()
        )
        rows = (
            db.query(StockPriceHistory.edinet_code,
                     StockPriceHistory.close,
                     StockPriceHistory.trade_date)
            .join(sq, (StockPriceHistory.edinet_code == sq.c.edinet_code) &
                      (StockPriceHistory.trade_date == sq.c.max_date))
            .all()
        )
        return {row.edinet_code: {"price": row.close, "date": row.trade_date}
                for row in rows}

    sp_all = _first_prices(bench_codes, start_date_str)
    ep_all = _last_prices(bench_codes)

    results = []
    for rank, (score, r) in enumerate(top, 1):
        c = r.edinet_code
        sp = sp_all.get(c)
        ep = ep_all.get(c)
        if (sp and ep and sp["price"] and ep["price"]
                and sp["date"] < ep["date"]):
            ret_pct = round((ep["price"] - sp["price"]) / sp["price"] * 100, 2)
        else:
            ret_pct = None
        results.append({
            "rank":           rank,
            "edinet_code":    c,
            "sec_code":       r.sec_code or "",
            "company_name":   r.company_name or "",
            "industry":       r.industry or "",
            "score":          round(score, 3),
            "year":           r.year,
            "period_end":     r.period_end,
            "start_price":    sp["price"] if sp else None,
            "start_date":     sp["date"]  if sp else None,
            "end_price":      ep["price"] if ep else None,
            "end_date":       ep["date"]  if ep else None,
            "return_pct":     ret_pct,
            "has_price_data": ret_pct is not None,
        })

    bench_returns = [
        (ep_all[c]["price"] - sp_all[c]["price"]) / sp_all[c]["price"] * 100
        for c in bench_codes
        if (c in sp_all and c in ep_all
            and sp_all[c]["price"] and ep_all[c]["price"]
            and sp_all[c]["date"] < ep_all[c]["date"])
    ]

    valid = [r["return_pct"] for r in results if r["return_pct"] is not None]
    if valid:
        n = len(valid)
        avg = sum(valid) / n
        srt = sorted(valid)
        std = (sum((x - avg) ** 2 for x in valid) / n) ** 0.5
        b_avg = sum(bench_returns) / len(bench_returns) if bench_returns else None
        summary = {
            "avg_return_pct":    round(avg, 2),
            "median_return_pct": round(_bt_percentile(srt, 50), 2),
            "std_dev_pct":       round(std, 2),
            "p5_pct":            round(_bt_percentile(srt,  5), 2),
            "p25_pct":           round(_bt_percentile(srt, 25), 2),
            "p75_pct":           round(_bt_percentile(srt, 75), 2),
            "p95_pct":           round(_bt_percentile(srt, 95), 2),
            "win_rate_pct":      round(sum(1 for x in valid if x > 0) / n * 100, 1),
            "n_with_data":       n,
            "benchmark_avg_pct": round(b_avg, 2) if b_avg is not None else None,
            "excess_return_pct": round(avg - b_avg, 2) if b_avg is not None else None,
            "n_benchmark":       len(bench_returns),
        }
    else:
        summary = None

    return {
        "start_date":       start_date_str,
        "end_date":         today_str,
        "holding_months":   months_ago,
        "top_n":            top_n,
        "preset":           preset_name,
        "total_candidates": len(scored),
        "summary":          summary,
        "results":          results,
    }


_BT_MULTI_PERIODS = [3, 6, 12, 18, 24]


@app.get("/api/backtest")
async def backtest(
    preset: str = "バランス型",
    months_ago: int = 6,
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    db: Session = Depends(get_db),
):
    """Nヶ月前のスコアリング上位N社の実績リターンを計算するバックテスト"""
    if not (1 <= months_ago <= 60):
        raise HTTPException(400, "months_ago は 1〜60 の範囲で指定してください")
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    try:
        return _backtest_single(db, preset, months_ago, top_n, industry, min_market_cap)
    except Exception as e:
        log.error("Backtest error: %s", e, exc_info=True)
        raise HTTPException(500, "バックテスト実行エラーが発生しました。")


@app.get("/api/backtest/multi")
async def backtest_multi(
    preset: str = "バランス型",
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    db: Session = Depends(get_db),
):
    """複数保有期間（3/6/12/18/24ヶ月）のバックテストを一括実行"""
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    periods = []
    for m in _BT_MULTI_PERIODS:
        try:
            periods.append(_backtest_single(db, preset, m, top_n, industry, min_market_cap))
        except Exception as e:
            log.error("Backtest multi error (months=%d): %s", m, e, exc_info=True)
            periods.append({"holding_months": m, "summary": None, "results": [],
                            "total_candidates": 0, "error": "計算エラー"})
    return {"periods": periods, "preset": preset, "top_n": top_n}


# ── DB ビューア API ─────────────────────────────────────────────────────
# DB内部を画面から確認するためのメタデータ・プレビュー・統計・リレーション API。
# テーブル名・カラム名はホワイトリストで厳格に検証する（SQL インジェクション対策）。

_DB_VIEWER_TABLES = {
    "companies":           Company,
    "financial_records":   FinancialRecord,
    "stock_price_history": StockPriceHistory,
    "macro_data":          MacroData,
    "collection_logs":     CollectionLog,
}

_DB_VIEWER_RELATIONS = [
    # (from_table, from_column, to_table, to_column, label)
    ("financial_records",   "edinet_code", "companies", "edinet_code", "1社:N年度"),
    ("stock_price_history", "edinet_code", "companies", "edinet_code", "1社:N日次"),
]


def _column_meta(col):
    """SQLAlchemy カラム→ {name, type, nullable, pk, fk, numeric} の辞書"""
    py_type = getattr(col.type, "python_type", None)
    try:
        py_name = py_type.__name__ if py_type else str(col.type)
    except NotImplementedError:
        py_name = str(col.type)
    is_numeric = py_name in ("int", "float")
    fks = [f"{fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys]
    return {
        "name":     col.name,
        "type":     py_name,
        "nullable": bool(col.nullable),
        "pk":       bool(col.primary_key),
        "fk":       fks[0] if fks else None,
        "numeric":  is_numeric,
    }


@app.get("/api/db/tables")
async def db_tables(db: Session = Depends(get_db)):
    """全テーブルの行数・カラム数・最終更新時刻を返す"""
    items = []
    for name, model in _DB_VIEWER_TABLES.items():
        row_count = db.query(func.count()).select_from(model).scalar() or 0
        cols = list(model.__table__.columns)
        last_updated = None
        if hasattr(model, "updated_at"):
            last_updated = db.query(func.max(model.updated_at)).scalar()
        elif hasattr(model, "created_at"):
            last_updated = db.query(func.max(model.created_at)).scalar()
        items.append({
            "name":         name,
            "row_count":    row_count,
            "column_count": len(cols),
            "last_updated": str(last_updated)[:19] if last_updated else None,
        })
    return {"tables": items}


@app.get("/api/db/schema/{table}")
async def db_schema(table: str, db: Session = Depends(get_db)):
    """指定テーブルのカラム定義 + NULL率を返す"""
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    model = _DB_VIEWER_TABLES[table]
    row_count = db.query(func.count()).select_from(model).scalar() or 0
    cols = []
    for col in model.__table__.columns:
        meta = _column_meta(col)
        if row_count > 0:
            null_count = db.query(func.count()).select_from(model).filter(col.is_(None)).scalar() or 0
            meta["null_rate"] = round(null_count / row_count * 100, 1)
            meta["null_count"] = null_count
        else:
            meta["null_rate"] = None
            meta["null_count"] = 0
        cols.append(meta)
    return {"table": table, "row_count": row_count, "columns": cols}


def _normalize_row(row) -> dict:
    """SQLAlchemy 行 → JSON 可能な dict（datetime/dict は文字列化）"""
    out = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name)
        if isinstance(v, datetime):
            v = v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)[:200]  # raw_xbrl_json は長いので切る
        out[col.name] = v
    return out


@app.get("/api/db/preview/{table}")
async def db_preview(
    table: str,
    limit:  int = 50,
    offset: int = 0,
    sort:   Optional[str] = None,
    order:  str = "desc",
    filter_col: Optional[str] = None,
    filter_val: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """テーブルプレビュー（ページネーション・ソート・簡易フィルタ）"""
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    if not (1 <= limit <= 500):
        raise HTTPException(400, "limit は 1〜500 の範囲で指定してください")
    if offset < 0:
        raise HTTPException(400, "offset は 0 以上で指定してください")
    if order not in ("asc", "desc"):
        raise HTTPException(400, "order は asc / desc のいずれか")

    model    = _DB_VIEWER_TABLES[table]
    col_map  = {c.name: c for c in model.__table__.columns}

    query = db.query(model)
    if filter_col and filter_val:
        if filter_col not in col_map:
            raise HTTPException(400, "filter_col が不正です")
        query = query.filter(col_map[filter_col] == filter_val)
    total = query.count()

    if sort:
        if sort not in col_map:
            raise HTTPException(400, "sort カラムが不正です")
        sort_col = col_map[sort]
        query = query.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    else:
        pk_cols = [c for c in model.__table__.columns if c.primary_key]
        if pk_cols:
            query = query.order_by(pk_cols[0].desc())

    rows = query.offset(offset).limit(limit).all()
    return {
        "table":   table,
        "total":   total,
        "limit":   limit,
        "offset":  offset,
        "columns": [c.name for c in model.__table__.columns],
        "rows":    [_normalize_row(r) for r in rows],
    }


@app.get("/api/db/stats/{table}")
async def db_stats(table: str, db: Session = Depends(get_db)):
    """テーブルの統計サマリー（数値カラムは min/max/avg/p50/p99、文字列カラムはユニーク数）"""
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    model = _DB_VIEWER_TABLES[table]
    row_count = db.query(func.count()).select_from(model).scalar() or 0

    stats = []
    for col in model.__table__.columns:
        meta = _column_meta(col)
        s = {
            "name":    col.name,
            "type":    meta["type"],
            "numeric": meta["numeric"],
        }
        if row_count == 0:
            stats.append(s)
            continue

        if meta["numeric"]:
            agg = db.query(
                func.min(col), func.max(col), func.avg(col), func.count(col)
            ).first()
            mn, mx, avg, cnt = agg
            s["min"]   = float(mn) if mn is not None else None
            s["max"]   = float(mx) if mx is not None else None
            s["avg"]   = round(float(avg), 4) if avg is not None else None
            s["count"] = int(cnt or 0)
            # PostgreSQL の percentile_cont を生 SQL で利用（パーセンタイル分布）
            try:
                sql = text(
                    f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {col.name}) AS p50, "
                    f"       percentile_cont(0.99) WITHIN GROUP (ORDER BY {col.name}) AS p99 "
                    f"FROM {table} WHERE {col.name} IS NOT NULL"
                )
                p = db.execute(sql).first()
                s["p50"] = round(float(p.p50), 4) if p and p.p50 is not None else None
                s["p99"] = round(float(p.p99), 4) if p and p.p99 is not None else None
            except Exception:
                s["p50"] = None
                s["p99"] = None
        else:
            # 文字列・列挙系: ユニーク数のみ（カーディナリティ目安）
            try:
                distinct_cnt = db.query(func.count(func.distinct(col))).scalar() or 0
                s["distinct"] = int(distinct_cnt)
            except Exception:
                s["distinct"] = None
        stats.append(s)
    return {"table": table, "row_count": row_count, "stats": stats}


@app.get("/api/db/relations")
async def db_relations():
    """テーブル間のリレーション一覧（Mermaid 描画用）"""
    return {
        "tables": [
            {
                "name":        name,
                "columns":     [c.name for c in model.__table__.columns],
                "pk":          [c.name for c in model.__table__.columns if c.primary_key],
            }
            for name, model in _DB_VIEWER_TABLES.items()
        ],
        "relations": [
            {
                "from_table":  ft, "from_column": fc,
                "to_table":    tt, "to_column":   tc,
                "label":       lbl,
            }
            for (ft, fc, tt, tc, lbl) in _DB_VIEWER_RELATIONS
        ],
    }


@app.get("/api/db/company/{edinet_code}")
async def db_company_drilldown(edinet_code: str, db: Session = Depends(get_db)):
    """企業別ドリルダウン: 1企業に紐づく全テーブルのレコードを横断取得"""
    if not _EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E123456）")
    company = db.query(Company).filter_by(edinet_code=edinet_code).first()
    if not company:
        raise HTTPException(404, "企業が見つかりません")

    fr_rows = (
        db.query(FinancialRecord)
        .filter_by(edinet_code=edinet_code)
        .order_by(FinancialRecord.year.desc())
        .all()
    )
    sph_count = db.query(func.count(StockPriceHistory.id)).filter_by(edinet_code=edinet_code).scalar() or 0
    sph_oldest = db.query(func.min(StockPriceHistory.trade_date)).filter_by(edinet_code=edinet_code).scalar()
    sph_newest = db.query(func.max(StockPriceHistory.trade_date)).filter_by(edinet_code=edinet_code).scalar()
    sph_recent = (
        db.query(StockPriceHistory)
        .filter_by(edinet_code=edinet_code)
        .order_by(StockPriceHistory.trade_date.desc())
        .limit(30).all()
    )

    return {
        "company":           _normalize_row(company),
        "financial_records": [_normalize_row(r) for r in fr_rows],
        "stock_price_history": {
            "total":       sph_count,
            "oldest_date": sph_oldest,
            "newest_date": sph_newest,
            "recent":      [_normalize_row(r) for r in sph_recent],
        },
    }


@app.get("/api/db/export/{table}")
async def db_export_table(
    table: str,
    limit:      int = 10000,
    filter_col: Optional[str] = None,
    filter_val: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """指定テーブルを CSV でダウンロード（最大 limit 行）"""
    if table not in _DB_VIEWER_TABLES:
        raise HTTPException(404, "テーブルが見つかりません")
    if not (1 <= limit <= 100000):
        raise HTTPException(400, "limit は 1〜100000 の範囲で指定してください")

    model   = _DB_VIEWER_TABLES[table]
    col_map = {c.name: c for c in model.__table__.columns}
    cols    = list(model.__table__.columns)

    query = db.query(model)
    if filter_col and filter_val:
        if filter_col not in col_map:
            raise HTTPException(400, "filter_col が不正です")
        query = query.filter(col_map[filter_col] == filter_val)
    rows = query.limit(limit).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([c.name for c in cols])
    for r in rows:
        row_vals = []
        for c in cols:
            v = getattr(r, c.name)
            if isinstance(v, datetime):
                v = v.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            row_vals.append(v)
        writer.writerow(row_vals)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table}.csv"},
    )


# ── CSV エクスポート ────────────────────────────────────────────────────

@app.get("/api/export/csv")
async def export_csv(year: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(FinancialRecord)
    if year:
        query = query.filter_by(year=year)
    records = query.limit(10000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "証券コード", "企業名", "業種", "期", "決算期末",
        "売上高", "営業利益", "純利益", "総資産", "純資産",
        "営業CF", "時価総額", "PER", "PBR", "ROE", "自己資本比率",
        "営業利益率", "純利益率", "D/Eレシオ",
        "予測時価総額", "乖離率%"
    ])
    for r in records:
        writer.writerow([
            r.sec_code, r.company_name, r.industry, r.year, r.period_end,
            r.pl_revenue, r.pl_operating_profit, r.pl_net_income,
            r.bs_total_assets, r.bs_total_equity,
            r.cf_operating_cf, r.market_cap, r.per, r.pbr, r.roe, r.equity_ratio,
            r.op_margin, r.net_margin, r.de_ratio,
            r.predicted_market_cap, r.gap_ratio
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=financial_db.csv"}
    )

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

@app.get("/")
async def serve_dashboard():
    return FileResponse(BASE_DIR / "templates" / "dashboard.html", headers=_NO_CACHE)

@app.get("/collection")
async def serve_collection():
    return FileResponse(BASE_DIR / "templates" / "collection.html", headers=_NO_CACHE)

@app.get("/analysis")
async def serve_analysis():
    return FileResponse(BASE_DIR / "templates" / "analysis.html", headers=_NO_CACHE)

@app.get("/models")
async def serve_models():
    return FileResponse(BASE_DIR / "templates" / "models.html", headers=_NO_CACHE)

@app.get("/db")
async def serve_db_viewer():
    return FileResponse(BASE_DIR / "templates" / "db.html", headers=_NO_CACHE)

@app.get("/login")
async def serve_login():
    return FileResponse(BASE_DIR / "templates" / "login.html", headers=_NO_CACHE)

def _update_env_file(key: str, value: str):
    env_path = BASE_DIR / ".env"
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

class LoginRequest(BaseModel):
    password: str

class ResetPasswordRequest(BaseModel):
    recovery_key: str
    new_password: str

@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    if not APP_PASSWORD:
        return {"token": "dev-mode"}
    if not hmac.compare_digest(req.password.encode(), APP_PASSWORD.encode()):
        raise HTTPException(401, "パスワードが違います")
    return {"token": _create_token()}

@app.post("/api/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    global APP_PASSWORD
    if not APP_RECOVERY_KEY:
        raise HTTPException(503, "回復キーが設定されていません（APP_RECOVERY_KEY を .env に設定してください）")
    if not hmac.compare_digest(req.recovery_key.encode(), APP_RECOVERY_KEY.encode()):
        raise HTTPException(401, "回復キーが違います")
    new_pw = req.new_password.strip()
    if not new_pw:
        raise HTTPException(400, "新しいパスワードを入力してください")
    if len(new_pw) < 8:
        raise HTTPException(400, "パスワードは8文字以上で設定してください")
    APP_PASSWORD = new_pw
    _update_env_file("APP_PASSWORD", APP_PASSWORD)
    return {"message": "パスワードを更新しました"}

@app.get("/api/auth/status")
async def auth_status():
    return {"auth_required": bool(APP_PASSWORD), "recovery_available": bool(APP_RECOVERY_KEY)}
