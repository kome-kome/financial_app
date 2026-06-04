"""
FastAPI バックエンド
- 収集ジョブ管理（非同期バックグラウンド実行）
- 財務データ検索・スクリーニング API
- 回帰分析・予測株価 API
- フロントエンド（Phase1-2/3-4 HTML）への CORS 対応 REST API
"""

import asyncio, json, logging, re
import hmac, hashlib, base64, secrets, time as _time, os
import httpx

log = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from datetime import date, datetime, timedelta

def _utc_to_jst_str(dt: Optional[datetime]) -> Optional[str]:
    """DB に UTC 保存された naive datetime を 'YYYY-MM-DD HH:MM:SS JST' に整形"""
    if dt is None:
        return None
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S") + " JST"
from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
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
RENDER_LIGHT_MODE = os.environ.get("RENDER_LIGHT_MODE", "").lower() in ("1", "true", "yes")

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

# ── Cookie / CSRF 認証（HttpOnly Cookie + Double-Submit CSRF）──────────────────
_AUTH_COOKIE   = "auth_token"
_CSRF_COOKIE   = "csrf_token"
# 本番（HTTPS）では Render 環境変数で COOKIE_SECURE=true を設定し Secure 属性を付与する。
# ローカル HTTP 開発では false（既定）。Secure=true だと HTTP では Cookie が送られないため。
_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
_CSRF_UNSAFE_METHODS = ("POST", "PUT", "DELETE", "PATCH")

def _create_csrf() -> str:
    return secrets.token_urlsafe(32)

def _set_auth_cookies(response, token: str, csrf: str) -> None:
    """HttpOnly 認証 Cookie（JS から読めない＝XSS 盗難不可）と、JS から読める CSRF Cookie を付与する。"""
    response.set_cookie(_AUTH_COOKIE, token, max_age=_TOKEN_TTL, httponly=True,
                        secure=_COOKIE_SECURE, samesite="lax", path="/")
    response.set_cookie(_CSRF_COOKIE, csrf, max_age=_TOKEN_TTL, httponly=False,
                        secure=_COOKIE_SECURE, samesite="lax", path="/")

def _clear_auth_cookies(response) -> None:
    response.delete_cookie(_AUTH_COOKIE, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")

from database import (
    SessionLocal, init_db,
    Company, FinancialRecord, FinancialMetric, RegressionResult,
    CollectionLog, StockPriceDaily, StockPriceWeekly, MacroData,
    prices_on_or_after, latest_prices,
)
from collector import run_full_collection, refresh_company, update_market_data, collect_stock_price_history, collect_stock_price_history_jquants, update_industry_from_jpx, collect_macro_data, MACRO_SERIES, reparse_from_raw
import plugins as plugin_registry


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
    yield

# ── レート制限（slowapi） ─────────────────────────────────────────────
# APP_RATELIMIT_ENABLED=false でテスト時等に無効化可能（デフォルト有効）。
# 注: 環境変数名は slowapi 内部の予約キー RATELIMIT_* と衝突しないよう APP_ 接頭辞を付ける
# （素の RATELIMIT_ENABLED だと slowapi が文字列値を取り込み enabled を誤上書きする）。
RATELIMIT_ENABLED = os.getenv("APP_RATELIMIT_ENABLED", "true").lower() == "true"
RATELIMIT_COLLECT  = "3/minute"   # 収集ジョブ起動（重い I/O・_job_status と二重防御）
RATELIMIT_ANALYSIS = "20/minute"  # スクリーニング・分析プラグイン・乖離分析
RATELIMIT_REFRESH  = "10/minute"  # 単一企業更新
RATELIMIT_AUTH     = "10/minute"  # ログイン（ブルートフォース対策）
RATELIMIT_RESET    = "3/minute"   # パスワードリセット（アカウント乗っ取り対策）
limiter = Limiter(key_func=get_remote_address, enabled=RATELIMIT_ENABLED)
limiter.enabled = RATELIMIT_ENABLED  # bool を明示（slowapi の env 取り込みによる型ブレを防ぐ）

app = FastAPI(title="EDINET Financial API", version="2.0", lifespan=lifespan)
# slowapi: Limiter を state に登録し、429 ハンドラを設定
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 外部化した JS（static/js/*.js）の配信。/api/* 認証ミドルウェアの対象外（公開取得可）。
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "http://localhost:8000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,  # Cookie 認証のため必須。allow_origins に "*" は使えない（明示オリジンのみ）
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """セキュリティ関連レスポンスヘッダーを全レスポンスに付与する"""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            # インラインJS/イベントハンドラは全て static/js へ外部化＋addEventListener化済みのため 'unsafe-inline' は不要
            "script-src 'self' https://cdn.jsdelivr.net; "
            # style-src の 'unsafe-inline' は据え置き（インライン <style>/style= 属性が残るため。script-src と違い XSS リスクは低い）
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-src 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), camera=(), microphone=(), payment=(), usb=()"
        )
        # HSTS は HTTPS 応答時のみ付与（Render は X-Forwarded-Proto: https を付ける）。
        # ローカル HTTP 開発では付けない（誤って HTTPS 固定化しないため）。
        if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

app.add_middleware(_SecurityHeadersMiddleware)

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """APP_PASSWORD 設定時、/api/* を HttpOnly Cookie で保護し、非冪等メソッドには
    CSRF Double-Submit（X-CSRF-Token ヘッダ == csrf_token Cookie）を要求する。"""
    if not APP_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/login" or path.startswith("/api/auth/"):
        return await call_next(request)
    if path.startswith("/api/"):
        if not _verify_token(request.cookies.get(_AUTH_COOKIE, "")):
            return JSONResponse({"detail": "認証が必要です"}, status_code=401)
        # CSRF: 非冪等メソッドは X-CSRF-Token ヘッダ == csrf_token Cookie を要求（Double-Submit）
        if request.method in _CSRF_UNSAFE_METHODS:
            header_tok = request.headers.get("X-CSRF-Token", "")
            cookie_tok = request.cookies.get(_CSRF_COOKIE, "")
            if not header_tok or not cookie_tok or not hmac.compare_digest(header_tok, cookie_tok):
                return JSONResponse({"detail": "CSRF トークンが無効です"}, status_code=403)
    return await call_next(request)

# DB セッション依存性
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── ヘルスチェック ────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """死活監視用エンドポイント。DB 接続を含む基本疎通を確認する。

    認証ミドルウェアは `/api/*` のみ保護するため、本エンドポイントは認証不要。
    """
    db_ok = False
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
        finally:
            db.close()
    except Exception as e:
        log.error("ヘルスチェック DB エラー: %s", e, exc_info=True)
    status_code = 200 if db_ok else 503
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"},
        status_code=status_code,
    )

@app.get("/api/system/info")
async def system_info():
    """実行環境情報を返す。認証不要（UI が起動時に参照する）。"""
    return {"render_light_mode": RENDER_LIGHT_MODE}

# ── 収集ジョブ管理 ────────────────────────────────────────────────────────

SMART_CHUNK_SIZE     = 200   # スマート収集: 1チャンクあたりの企業数増分
SMART_FULL_THRESHOLD = 3500  # この企業数以上は「全社収集完了」と判定し差分収集に切り替え

# 注意: 以下のステータス辞書はシングル Worker 運用前提（VISION.md 参照）。
# asyncio 単一イベントループ内では mutations が await を跨がないため、
# 同一プロセス内の競合は発生しない。複数 Worker 環境では各プロセスが独立した
# 状態を持つため Redis 等の外部ストアへの移行が必要。
_LOG_MAX = 500

_job_status: dict = {"running": False, "log": [], "log_seq": 0, "progress": 0, "total": 0, "job_type": "", "cancel_requested": False}
_market_status: dict = {"running": False, "progress": 0, "total": 0, "log": [], "log_seq": 0, "cancel_requested": False}
_history_status: dict  = {"running": False, "progress": 0, "total": 0, "log": [], "log_seq": 0, "cancel_requested": False}
_jquants_status: dict  = {"running": False, "progress": 0, "total": 0, "log": [], "log_seq": 0, "cancel_requested": False}
_macro_status:   dict  = {"running": False, "progress": 0, "total": 0, "log": [], "log_seq": 0, "cancel_requested": False}
_reparse_status: dict  = {"running": False, "progress": 0, "total": 0, "log": [], "log_seq": 0, "cancel_requested": False}

def _append_log(status: dict, msg: str) -> None:
    """ステータス辞書にログを追加。log_seq は累積カウンタとして単調増加し、
    SSE 消費者が切り捨て後も正しい差分を計算できるようにする。"""
    status["log"].append(msg)
    status["log_seq"] = status.get("log_seq", 0) + 1
    if len(status["log"]) > _LOG_MAX:
        status["log"] = status["log"][-_LOG_MAX:]

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
    force:      bool = False   # True=実行中フラグを無視して強制再起動

class JQuantsCollectRequest(BaseModel):
    days_back: int  = Field(default=14, ge=1, le=730)
    force:     bool = False

@app.post("/api/collect/start")
@limiter.limit(RATELIMIT_COLLECT)
async def start_collection(request: Request, req: CollectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if RENDER_LIGHT_MODE and not req.skip_existing:
        raise HTTPException(403, "全件収集はローカル環境から実行してください（Render Free プラン制限）")
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
    baseline_records = db.query(FinancialRecord).count()

    def on_progress(current, total, msg):
        _job_status["progress"] = current
        _job_status["total"]    = total
        _append_log(_job_status, msg)
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
        # 成長率・Zスコアは financial_metrics VIEW が都度算出するため収集後の事前計算は不要。
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status = "done"
            log_obj.finished_at = datetime.utcnow()
            log_obj.records_saved = max(0, db.query(FinancialRecord).count() - baseline_records)
            if cancelled:
                log_obj.message = "ユーザーにより停止"
            db.commit()
    except Exception as e:
        log.error("収集ジョブエラー (log_id=%s): %s", log_id, e, exc_info=True)
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status = "error"
            log_obj.message = "収集処理でエラーが発生しました（詳細はサーバーログを確認）"
            log_obj.finished_at = datetime.utcnow()
            log_obj.records_saved = max(0, db.query(FinancialRecord).count() - baseline_records)
            db.commit()
    finally:
        _job_status["running"] = False
        _job_status["cancel_requested"] = False
        db.close()

async def _run_smart_collection_bg(log_id: int, years: int):
    _job_status["cancel_requested"] = False
    _prog_ticks = [0]
    db = SessionLocal()
    baseline_records = db.query(FinancialRecord).count()

    def on_progress(current, total, msg):
        _job_status["progress"] = current
        _job_status["total"]    = total
        _append_log(_job_status, msg)
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
            _append_log(_job_status,
                f"[スマート判定] DB企業数={company_count}社 → 差分収集モード（過去{years}年）"
            )
            cancelled = await run_full_collection(
                years, None, on_progress=on_progress,
                skip_existing=True, cancel_check=cancel_check
            )
        elif company_count == 0:
            _append_log(_job_status,
                f"[スマート判定] DB企業数=0社 → 初回チャンク収集（先着{SMART_CHUNK_SIZE}社）"
            )
            cancelled = await run_full_collection(
                years, SMART_CHUNK_SIZE, on_progress=on_progress,
                skip_existing=False, cancel_check=cancel_check
            )
        else:
            chunk_no = (company_count // SMART_CHUNK_SIZE) + 1
            target   = chunk_no * SMART_CHUNK_SIZE
            _append_log(_job_status,
                f"[スマート判定] DB企業数={company_count}社 → "
                f"チャンク{chunk_no}（先着{target}社のうち未収集を処理）"
            )
            cancelled = await run_full_collection(
                years, target, on_progress=on_progress,
                skip_existing=True, cancel_check=cancel_check
            )

        # 成長率・Zスコアは financial_metrics VIEW が都度算出するため事前計算は不要。

        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status      = "done"
            log_obj.finished_at = datetime.utcnow()
            log_obj.records_saved = max(0, db.query(FinancialRecord).count() - baseline_records)
            if cancelled:
                log_obj.message = "ユーザーにより停止"
            db.commit()

    except Exception as e:
        log.error("スマート収集エラー: %s", e, exc_info=True)
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status      = "error"
            log_obj.message     = "スマート収集でエラーが発生しました（詳細はサーバーログを確認）"
            log_obj.finished_at = datetime.utcnow()
            log_obj.records_saved = max(0, db.query(FinancialRecord).count() - baseline_records)
            db.commit()
    finally:
        _job_status["running"]          = False
        _job_status["cancel_requested"] = False
        db.close()

@app.post("/api/collect/smart-start")
@limiter.limit(RATELIMIT_COLLECT)
async def start_smart_collection(
    request: Request,
    req: SmartCollectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if _job_status["running"] and not req.force:
        raise HTTPException(400, "収集ジョブが既に実行中です")
    if req.force:
        _reset_stuck_jobs(db, message="ユーザーによる強制再開で上書き")
    log_obj = CollectionLog(job_type="smart", status="running")
    db.add(log_obj); db.commit(); db.refresh(log_obj)
    _job_status.update({"running": True, "log": [], "progress": 0, "log_id": log_obj.id, "job_type": "smart"})
    background_tasks.add_task(_run_smart_collection_bg, log_obj.id, req.years_back)
    return {"message": "スマート収集ジョブを開始しました", "log_id": log_obj.id}

def _reset_stuck_jobs(db: Session, message: str = "ユーザーによる強制リセット") -> int:
    """in-memory フラグと DB の running ジョブを強制 error 扱いに戻す。リセット件数を返す。"""
    _job_status["running"]          = False
    _job_status["cancel_requested"] = False
    stuck = db.query(CollectionLog).filter(CollectionLog.status == "running").all()
    for job in stuck:
        job.status      = "error"
        job.message     = message
        job.finished_at = datetime.utcnow()
    if stuck:
        db.commit()
    return len(stuck)

@app.post("/api/collect/reset-stuck")
async def reset_stuck_collection(db: Session = Depends(get_db)):
    """スタックした収集ジョブを強制リセット（実行中フラグと DB 上の running ステータスを解除）"""
    count = _reset_stuck_jobs(db)
    return {"reset_jobs": count, "message": f"{count}件のジョブをリセットしました"}

@app.post("/api/scheduler/run-now")
async def scheduler_run_now(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """手動差分収集: 過去1年・収集済みスキップ＋成長率/Zスコア＋市場・マクロ更新"""
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
            {"id": l.id, "status": l.status, "started": _utc_to_jst_str(l.started_at),
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
        "latest_update":     _utc_to_jst_str(latest_update),
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

_EDINET_CODE_RE = re.compile(r"^E\d{5,6}$")

@app.post("/api/collect/refresh/{edinet_code}")
@limiter.limit(RATELIMIT_REFRESH)
async def refresh_single(request: Request, edinet_code: str, background_tasks: BackgroundTasks):
    if not _EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    background_tasks.add_task(refresh_company, edinet_code)
    return {"message": f"{edinet_code} の再取得を開始しました"}


class MarketDataRequest(BaseModel):
    max_companies: Optional[int] = None
    force: bool = False  # True=実行中フラグを無視して強制再起動

@app.post("/api/collect/market-data")
@limiter.limit(RATELIMIT_COLLECT)
async def start_market_data_update(request: Request, req: MarketDataRequest, background_tasks: BackgroundTasks):
    if _market_status["running"] and not req.force:
        raise HTTPException(400, "市場データ更新ジョブが既に実行中です")
    _market_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _market_status["progress"] = current
        _market_status["total"]    = total
        _append_log(_market_status, msg)

    def cancel_check():
        return _market_status.get("cancel_requested", False)

    async def _run():
        try:
            await update_market_data(req.max_companies, on_progress=on_progress, cancel_check=cancel_check)
        except Exception as e:
            log.error("市場データ更新エラー: %s", e, exc_info=True)
            _append_log(_market_status, "[エラー] 市場データ更新中に問題が発生しました（詳細はサーバーログを確認）")
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
@limiter.limit(RATELIMIT_COLLECT)
async def start_history_collection(request: Request, req: HistoryCollectRequest, background_tasks: BackgroundTasks):
    if RENDER_LIGHT_MODE:
        raise HTTPException(403, "株価履歴収集はローカル環境から実行してください（Render Free プラン制限）")
    if _history_status["running"] and not req.force:
        raise HTTPException(400, "株価履歴収集ジョブが既に実行中です")
    _history_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _history_status["progress"] = current
        _history_status["total"]    = total
        _append_log(_history_status, msg)

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
            log.error("株価履歴収集エラー: %s", e, exc_info=True)
            _append_log(_history_status, "[エラー] 株価履歴収集中に問題が発生しました（詳細はサーバーログを確認）")
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

@app.get("/api/stock/history/{edinet_code}")
async def get_stock_history(edinet_code: str, days: int = 365,
                            resolution: str = "daily", db: Session = Depends(get_db)):
    """指定企業の終値時系列を返す（close-only）。
    resolution=daily : 直近 DAILY_WINDOW_DAYS 日の日次終値（チャートの日次ズーム用）
    resolution=weekly: 全履歴の週次終値（close_last。チャートの全期間表示・長期用）
    返却は [{trade_date, close}] の昇順。
    """
    if not _EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days は 1〜3650 の範囲で指定してください")
    if resolution not in ("daily", "weekly"):
        raise HTTPException(400, "resolution は daily / weekly のいずれか")

    if resolution == "weekly":
        rows = (
            db.query(StockPriceWeekly.trade_date, StockPriceWeekly.close_last)
            .filter(StockPriceWeekly.edinet_code == edinet_code)
            .order_by(StockPriceWeekly.trade_date.desc())
            .limit(days)
            .all()
        )
    else:
        rows = (
            db.query(StockPriceDaily.trade_date, StockPriceDaily.close)
            .filter(StockPriceDaily.edinet_code == edinet_code)
            .order_by(StockPriceDaily.trade_date.desc())
            .limit(days)
            .all()
        )
    return [{"trade_date": r[0], "close": r[1]} for r in reversed(rows)]


async def _sse_stream(status: dict):
    """SSE共通ジェネレータ。status辞書を監視してリアルタイム配信する。

    log_seq（累積カウンタ）を使って差分計算するため、内部リストが切り捨てされても
    ログの取りこぼしを最小化できる（切り捨て分は復元不可だが、以降は連続配信可能）。
    """
    last_seq = 0
    while True:
        log_list = status["log"]
        cur_seq = status.get("log_seq", 0)
        # log_list[-1] の seq == cur_seq、log_list[0] の seq == cur_seq - len(log_list) + 1
        oldest_seq = cur_seq - len(log_list) + 1
        start = max(0, last_seq - oldest_seq + 1) if log_list else 0
        new_logs = log_list[start:]
        last_seq = cur_seq
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

class ReparseRequest(BaseModel):
    year:        Optional[int] = None
    edinet_code: Optional[str] = None

@app.post("/api/collect/reparse/start")
@limiter.limit(RATELIMIT_COLLECT)
async def start_reparse(request: Request, req: ReparseRequest, background_tasks: BackgroundTasks):
    """xbrl_raw_documents から financial_records を再構築する（EDINET 通信なし）。
    RENDER_LIGHT_MODE でも許可（軽量処理のため）。"""
    if _reparse_status["running"]:
        raise HTTPException(400, "再解析ジョブが既に実行中です")
    _reparse_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _reparse_status["progress"] = current
        _reparse_status["total"]    = total
        _append_log(_reparse_status, msg)

    def cancel_check():
        return _reparse_status.get("cancel_requested", False)

    async def _run():
        try:
            await reparse_from_raw(
                year=req.year,
                edinet_code=req.edinet_code,
                on_progress=on_progress,
                cancel_check=cancel_check,
            )
        except Exception as e:
            log.error("再解析エラー: %s", e, exc_info=True)
            _append_log(_reparse_status, "[エラー] 再解析中に問題が発生しました")
        finally:
            _reparse_status["running"] = False
            _reparse_status["cancel_requested"] = False

    background_tasks.add_task(_run)
    return {"message": "再解析ジョブを開始しました"}

@app.post("/api/collect/reparse/cancel")
async def cancel_reparse():
    _reparse_status["cancel_requested"] = True
    return {"message": "停止リクエストを送信しました"}

@app.get("/api/collect/reparse/stream")
async def reparse_progress_stream():
    return StreamingResponse(_sse_stream(_reparse_status), media_type="text/event-stream", headers=_SSE_HEADERS)

@app.post("/api/collect/industry")
async def collect_industry(db: Session = Depends(get_db)):
    async with httpx.AsyncClient() as client:
        updated_co, updated_fr = await update_industry_from_jpx(client, db)
    return {"updated_companies": updated_co, "updated_records": updated_fr}


@app.post("/api/collect/jquants/start")
@limiter.limit(RATELIMIT_COLLECT)
async def start_jquants_collection(request: Request, req: JQuantsCollectRequest, background_tasks: BackgroundTasks):
    if RENDER_LIGHT_MODE:
        raise HTTPException(403, "J-Quants収集はローカル環境から実行してください（Render Free プラン制限）")
    if _jquants_status["running"] and not req.force:
        raise HTTPException(400, "J-Quants収集ジョブが既に実行中です")
    _jquants_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _jquants_status["progress"] = current
        _jquants_status["total"]    = total
        _append_log(_jquants_status, msg)

    def cancel_check():
        return _jquants_status.get("cancel_requested", False)

    async def _run():
        db = SessionLocal()
        try:
            await collect_stock_price_history_jquants(
                db, req.days_back, on_progress=on_progress, cancel_check=cancel_check,
            )
        except ValueError as e:
            _append_log(_jquants_status, f"[設定エラー] {e}")
        except Exception as e:
            log.error("J-Quants収集エラー: %s", e, exc_info=True)
            _append_log(_jquants_status, "[エラー] 収集中に問題が発生しました")
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
@limiter.limit(RATELIMIT_COLLECT)
async def start_macro_collection(request: Request, req: MacroCollectRequest, background_tasks: BackgroundTasks):
    if _macro_status["running"] and not req.force:
        raise HTTPException(400, "マクロ収集ジョブが既に実行中です")
    _macro_status.update({"running": True, "progress": 0, "total": 0, "log": [], "cancel_requested": False})

    def on_progress(current, total, msg):
        _macro_status["progress"] = current
        _macro_status["total"]    = total
        _append_log(_macro_status, msg)

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
            _append_log(_macro_status, "[エラー] マクロ収集中に問題が発生しました（詳細はサーバーログを確認）")
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
    n_stock_price = db.query(func.count(StockPriceWeekly.edinet_code)).scalar() or 0
    # 業種別OLS実行済み判定用: regression_results に書き込まれた予測値の件数。
    # 乖離分析は gap_ratio が必須のため、0 件なら未実行（UIで乖離分析タブをロック）。
    n_predicted = (
        db.query(func.count(RegressionResult.edinet_code))
        .filter(RegressionResult.gap_ratio.isnot(None))
        .scalar()
    ) or 0

    # 最新の財務レコード（year → period_end の降順で1件）
    latest_fr = (
        db.query(FinancialRecord.year, FinancialRecord.period_end, FinancialRecord.updated_at)
        .order_by(FinancialRecord.year.desc(), FinancialRecord.period_end.desc())
        .first()
    )
    # DB全体の最新更新時刻（株価・市場データ更新を含む）
    last_db_update = db.query(func.max(FinancialRecord.updated_at)).scalar()

    # 「今日から見て期待できる最新の決算年度」推定
    # 日本企業の多くは3月決算で、有価証券報告書は決算後3ヶ月以内（6月末）に提出される。
    # 提出から EDINET に反映されるラグも考慮し、7月以降を「当年3月期が揃っている」と判定。
    today = date.today()
    if today.month >= 7:
        expected_year = today.year
    else:
        expected_year = today.year - 1

    # データ鮮度判定（最終DB更新からの経過日数）
    days_since: Optional[int] = None
    if last_db_update:
        days_since = (datetime.utcnow() - last_db_update).days
    if days_since is None:
        freshness = "empty"
    elif days_since <= 2:
        freshness = "fresh"
    elif days_since <= 14:
        freshness = "ok"
    elif days_since <= 60:
        freshness = "stale"
    else:
        freshness = "outdated"

    return {
        "companies":            n_companies,
        "records":              n_records,
        "stock_price_records":  n_stock_price,
        "records_with_prediction": n_predicted,
        "latest_year":          latest_fr.year       if latest_fr else None,
        "latest_period_end":    latest_fr.period_end if latest_fr else None,
        "last_db_update":       _utc_to_jst_str(last_db_update),
        "days_since_update":    days_since,
        "expected_latest_year": expected_year,
        "freshness":            freshness,
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
        # 派生指標・予測値を含むため financial_metrics VIEW から最新行を取得する。
        latest_recs = (
            db.query(FinancialMetric)
            .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                        (FinancialMetric.year == subq.c.max_year))
            .all()
        )
        latest_map = {r.edinet_code: _record_to_dict(r) for r in latest_recs}
        for item in items:
            item["latest"] = latest_map.get(item["edinet_code"])
    return {"total": total, "items": items}


# ── 財務データ取得 ────────────────────────────────────────────────────────

@app.get("/api/financials/{edinet_code}")
async def get_financials(edinet_code: str, db: Session = Depends(get_db)):
    if not _EDINET_CODE_RE.match(edinet_code):
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    # 派生指標・Zスコア・成長率・予測値は financial_metrics VIEW で都度算出される。
    records = (db.query(FinancialMetric)
               .filter_by(edinet_code=edinet_code)
               .order_by(FinancialMetric.year)
               .all())
    if not records:
        raise HTTPException(404, "データが見つかりません")
    return {"edinet_code": edinet_code, "records": [_record_to_dict(r) for r in records]}


def _record_to_dict(r) -> dict:
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
            "cost_of_sales": r.pl_cost_of_sales,
            "gross_profit": r.pl_gross_profit,
            "sga": r.pl_sga,
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
            "dps": r.dps,
            "de_ratio": r.de_ratio,
            "roe": r.roe, "roa": r.roa,
        },
        "nc": {
            "net_cash": r.net_cash,
            "nc_ratio": r.nc_ratio,
        },
        "zscore": {
            "z_revenue": r.z_revenue,
            "z_op_margin": r.z_op_margin,
            "z_roe": r.z_roe,
            "z_equity_ratio": r.z_equity_ratio,
            "z_cf_ratio": r.z_cf_ratio,
            "z_eps": r.z_eps,
            "z_de_ratio": r.z_de_ratio,
            "z_nc_ratio": r.z_nc_ratio,
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
@limiter.limit(RATELIMIT_ANALYSIS)
async def screening(request: Request, req: ScreenRequest, db: Session = Depends(get_db)):
    # 最新年度のレコードのみ対象。派生指標フィルタは financial_metrics VIEW を対象にする
    # （op_margin / roe / rev_growth 等は VIEW が都度算出。本体には保存しない）。
    subq = (db.query(FinancialRecord.edinet_code,
                     func.max(FinancialRecord.year).label("max_year"))
              .group_by(FinancialRecord.edinet_code)
              .subquery())
    query = (db.query(FinancialMetric)
               .join(subq, (FinancialMetric.edinet_code == subq.c.edinet_code) &
                           (FinancialMetric.year == subq.c.max_year)))

    if req.year:
        query = query.filter(FinancialMetric.year == req.year)
    if req.industry:
        query = query.filter(FinancialMetric.industry == req.industry)
    if req.market:
        query = query.filter(FinancialMetric.market == req.market)
    if req.min_rev_growth is not None:
        query = query.filter(FinancialMetric.rev_growth >= req.min_rev_growth)
    if req.min_op_margin is not None:
        query = query.filter(FinancialMetric.op_margin >= req.min_op_margin)
    if req.min_net_margin is not None:
        query = query.filter(FinancialMetric.net_margin >= req.min_net_margin)
    if req.min_roe is not None:
        query = query.filter(FinancialMetric.roe >= req.min_roe)
    if req.min_roa is not None:
        query = query.filter(FinancialMetric.roa >= req.min_roa)
    if req.min_equity_ratio is not None:
        query = query.filter(FinancialMetric.equity_ratio >= req.min_equity_ratio)
    if req.max_de_ratio is not None:
        query = query.filter(FinancialMetric.de_ratio <= req.max_de_ratio)
    if req.max_per is not None:
        query = query.filter(FinancialMetric.per <= req.max_per)
    if req.max_pbr is not None:
        query = query.filter(FinancialMetric.pbr <= req.max_pbr)
    if req.min_div_yield is not None:
        query = query.filter(FinancialMetric.div_yield >= req.min_div_yield)
    if req.min_cf_ratio is not None:
        query = query.filter(FinancialMetric.cf_ratio >= req.min_cf_ratio)

    rows = query.limit(req.limit).all()
    return {"count": len(rows), "results": [_record_to_dict(r) for r in rows]}


# ── プラグイン API ──────────────────────────────────────────────────────

@app.get("/api/plugins")
async def list_plugins():
    """利用可能な分析プラグイン一覧とパラメータスキーマを返す"""
    return {"plugins": [p.to_meta() for p in plugin_registry.list_plugins()]}

@app.post("/api/plugins/{plugin_name}/run", response_model=None)
@limiter.limit(RATELIMIT_ANALYSIS)
async def run_plugin(request: Request, plugin_name: str, params: dict, db: Session = Depends(get_db)):
    """指定プラグインを実行する"""
    p = plugin_registry.get_plugin(plugin_name)
    if p is None:
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    # 重い回帰計算は Render Free では OOM するためローカル実行に限定。
    # 結果は regression_results（共有DB）に保存され、Render は読み取りのみで反映される。
    if RENDER_LIGHT_MODE and getattr(p, "heavy", False):
        raise HTTPException(403, f"「{p.label}」は計算が重いためローカル環境で実行してください"
                                 "（Render Free プラン制限。結果は共有DBに保存され本番に反映されます）")
    try:
        return await p.execute(params, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Plugin '%s' error: %s", plugin_name, e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")

@app.get("/api/gap-analysis")
@limiter.limit(RATELIMIT_ANALYSIS)
async def gap_analysis(request: Request, year: Optional[int] = None, sort: str = "asc", db: Session = Depends(get_db)):
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
    """pパーセンタイル値（0〜100）。numpy.percentile（線形補間）を使用。"""
    n = len(sorted_arr)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_arr[0])
    import numpy as np
    return float(np.percentile(sorted_arr, p, method="linear"))


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
    from database import FinancialRecord

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

    # エントリー=start_date 以降の最初の終値（daily窓内なら日次・古ければ週次へ自動切替）。
    # イグジット="now"=最新終値（daily優先）。価格取得は database のヘルパに集約。
    sp_all = prices_on_or_after(db, bench_codes, start_date_str)
    ep_all = latest_prices(db, bench_codes)

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
        import numpy as np
        n = len(valid)
        arr = np.asarray(valid, dtype=float)
        avg = float(arr.mean())
        srt = sorted(valid)
        std = float(arr.std(ddof=0))
        b_avg = float(np.mean(bench_returns)) if bench_returns else None
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
    "stock_price_daily":   StockPriceDaily,
    "stock_price_weekly":  StockPriceWeekly,
    "macro_data":          MacroData,
    "collection_logs":     CollectionLog,
}

_DB_VIEWER_RELATIONS = [
    # (from_table, from_column, to_table, to_column, label)
    ("financial_records",  "edinet_code", "companies", "edinet_code", "1社:N年度"),
    ("stock_price_daily",  "edinet_code", "companies", "edinet_code", "1社:N日次(直近)"),
    ("stock_price_weekly", "edinet_code", "companies", "edinet_code", "1社:N週次(全期間)"),
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
            "last_updated": _utc_to_jst_str(last_updated),
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
        raise HTTPException(400, "edinet_code の形式が不正です（例: E02167）")
    company = db.query(Company).filter_by(edinet_code=edinet_code).first()
    if not company:
        raise HTTPException(404, "企業が見つかりません")

    fr_rows = (
        db.query(FinancialRecord)
        .filter_by(edinet_code=edinet_code)
        .order_by(FinancialRecord.year.desc())
        .all()
    )
    # カバレッジは全履歴の weekly 基準、直近プレビューは日次（daily）。
    sph_count = db.query(func.count(StockPriceWeekly.edinet_code)).filter_by(edinet_code=edinet_code).scalar() or 0
    sph_oldest = db.query(func.min(StockPriceWeekly.trade_date)).filter_by(edinet_code=edinet_code).scalar()
    sph_newest = db.query(func.max(StockPriceWeekly.trade_date)).filter_by(edinet_code=edinet_code).scalar()
    sph_recent = (
        db.query(StockPriceDaily)
        .filter_by(edinet_code=edinet_code)
        .order_by(StockPriceDaily.trade_date.desc())
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
    # 派生指標・予測値を含むため financial_metrics VIEW から出力する。
    query = db.query(FinancialMetric)
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

@app.get("/company")
async def serve_company_search():
    return FileResponse(BASE_DIR / "templates" / "company.html", headers=_NO_CACHE)

@app.get("/company/{edinet_code}")
async def serve_company(edinet_code: str):
    # ページ自体は静的 HTML。edinet_code は URL 用で、実データ取得は
    # フロントの /api/financials/{edinet_code} 側でバリデーション・404 処理する。
    return FileResponse(BASE_DIR / "templates" / "company.html", headers=_NO_CACHE)

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
@limiter.limit(RATELIMIT_AUTH)
async def auth_login(request: Request, req: LoginRequest, response: Response):
    if not APP_PASSWORD:
        return {"ok": True, "dev_mode": True}
    if not hmac.compare_digest(req.password.encode(), APP_PASSWORD.encode()):
        raise HTTPException(401, "パスワードが違います")
    _set_auth_cookies(response, _create_token(), _create_csrf())
    return {"ok": True}

@app.post("/api/auth/reset-password")
@limiter.limit(RATELIMIT_RESET)
async def reset_password(request: Request, req: ResetPasswordRequest):
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

@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    """認証 Cookie を削除する。/api/auth/ 配下のため CSRF/認証チェックは免除。"""
    _clear_auth_cookies(response)
    return {"ok": True}
