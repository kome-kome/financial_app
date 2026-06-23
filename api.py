"""
FastAPI バックエンド（エントリポイント）

エンドポイントは routers/ 配下に分割：
  routers/collect.py  ─ /api/collect/*, /api/scheduler/*
  routers/market.py   ─ /api/stock/*, /api/macro/*, /api/screen, /api/db/*, /api/export/*
  routers/analysis.py ─ /api/plugins/*, /api/gap-analysis, /api/recommend, /api/backtest
  routers/auth.py     ─ /api/auth/*, /health

このファイルは: 共有状態・認証ヘルパー・ミドルウェア・HTML配信・バックグラウンドジョブ関数を担う。
"""

import json, logging, re
import hmac, hashlib, base64, secrets, time as _time, os
import httpx

# .env を「APP_PASSWORD 等の認証設定を読む前」に読み込む。
# 注意: これらの os.getenv(...) は database の import より前に実行されるため、
#       ここで明示的に load_dotenv() しないと .env のみ設定の環境（ローカル等）で
#       APP_PASSWORD="" → 認証無効・APP_SECRET_KEY が dev 既定値、という事故になる。
#       override=False（既定）なので本番の実環境変数（Render dashboard）が優先される。
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from datetime import date, datetime, timedelta, timezone

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
_TOKEN_TTL      = 30 * 24 * 3600
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

_AUTH_COOKIE   = "auth_token"
_CSRF_COOKIE   = "csrf_token"
_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
_CSRF_UNSAFE_METHODS = ("POST", "PUT", "DELETE", "PATCH")

def _create_csrf() -> str:
    return secrets.token_urlsafe(32)

def _set_auth_cookies(response, token: str, csrf: str) -> None:
    response.set_cookie(_AUTH_COOKIE, token, max_age=_TOKEN_TTL, httponly=True,
                        secure=_COOKIE_SECURE, samesite="lax", path="/")
    response.set_cookie(_CSRF_COOKIE, csrf, max_age=_TOKEN_TTL, httponly=False,
                        secure=_COOKIE_SECURE, samesite="lax", path="/")

def _clear_auth_cookies(response) -> None:
    response.delete_cookie(_AUTH_COOKIE, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")

# ── DB / コレクター インポート ──────────────────────────────────────────────
from database import (
    SessionLocal, init_db,
    Company, FinancialRecord, FinancialMetric, RegressionResult,
    CollectionLog, StockPriceDaily, StockPriceWeekly, MacroData,
    prices_on_or_after, latest_prices, latest_year_subq,
)
from collector import run_full_collection, refresh_company, update_market_data, collect_stock_price_history, collect_stock_price_history_jquants, update_industry_from_jpx, collect_macro_data, MACRO_SERIES, reparse_from_raw
from collection_jobs import jobs
import backtest
import serializers
import plugins as plugin_registry

# ── edinet_code 検証正規表現 ────────────────────────────────────────────
_EDINET_CODE_RE = re.compile(r"^E\d{5,6}$")

# ── レート制限定数 ────────────────────────────────────────────────────────
RATELIMIT_ENABLED  = os.getenv("APP_RATELIMIT_ENABLED", "true").lower() == "true"
RATELIMIT_COLLECT  = "3/minute"
RATELIMIT_ANALYSIS = "20/minute"
RATELIMIT_REFRESH  = "10/minute"
RATELIMIT_AUTH     = "10/minute"
RATELIMIT_RESET    = "3/minute"
limiter = Limiter(key_func=get_remote_address, enabled=RATELIMIT_ENABLED)
limiter.enabled = RATELIMIT_ENABLED

# ── 収集ジョブ定数 ────────────────────────────────────────────────────────
_COLLECTION = "collection"   # full / smart / incremental の共有スロット名


# ── アプリ初期化 ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        stuck = db.query(CollectionLog).filter(CollectionLog.status == "running").all()
        for job in stuck:
            job.status = "error"
            job.message = "サーバー再起動により中断"
            job.finished_at = datetime.now(timezone.utc)
        if stuck:
            db.commit()
            log.warning("起動時に %d 件のスタックジョブを error にリセットしました", len(stuck))
    finally:
        db.close()
    yield

app = FastAPI(title="EDINET Financial API", version="2.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

class _RevalidateStaticFiles(StaticFiles):
    """静的アセットに Cache-Control: no-cache を付与し、ブラウザに毎回 ETag 再検証を強制する。
    （変更なしは 304 で軽量・変更時は確実に最新取得。JS/CSS 更新がキャッシュで反映されない事故を防ぐ）"""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidateStaticFiles(directory=str(BASE_DIR / "static")), name="static")

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "http://localhost:8000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
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
        if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

app.add_middleware(_SecurityHeadersMiddleware)

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if not APP_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/login" or path.startswith("/api/auth/"):
        return await call_next(request)
    if path.startswith("/api/"):
        if not _verify_token(request.cookies.get(_AUTH_COOKIE, "")):
            return JSONResponse({"detail": "認証が必要です"}, status_code=401)
        if request.method in _CSRF_UNSAFE_METHODS:
            header_tok = request.headers.get("X-CSRF-Token", "")
            cookie_tok = request.cookies.get(_CSRF_COOKIE, "")
            if not header_tok or not cookie_tok or not hmac.compare_digest(header_tok, cookie_tok):
                return JSONResponse({"detail": "CSRF トークンが無効です"}, status_code=403)
    return await call_next(request)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── バックグラウンドジョブ関数（テスト互換性のため api モジュールに保持）────────

async def _run_bg_job(coro_factory, log_id: int, error_msg: str = "収集処理でエラーが発生しました（詳細はサーバーログを確認）") -> None:
    """full / smart 収集ジョブの共通ライフサイクルラッパー。"""
    st = jobs.state(_COLLECTION)
    _prog_ticks = [0]
    db = SessionLocal()
    baseline_records = db.query(FinancialRecord).count()

    def on_progress(current: int, total: int, msg: str) -> None:
        st.progress = current
        st.total    = total
        st.append_log(msg)
        _prog_ticks[0] += 1
        if _prog_ticks[0] % 10 == 0:
            try:
                obj = db.get(CollectionLog, log_id)
                if obj:
                    obj.companies_processed = current
                    db.commit()
            except Exception:
                db.rollback()

    def cancel_check() -> bool:
        return st.cancel_requested

    try:
        cancelled = await coro_factory(on_progress, cancel_check, db)
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status        = "done"
            log_obj.finished_at   = datetime.now(timezone.utc)
            log_obj.records_saved = max(0, db.query(FinancialRecord).count() - baseline_records)
            if cancelled:
                log_obj.message = "ユーザーにより停止"
            db.commit()
    except Exception as e:
        log.error("収集ジョブエラー (log_id=%s): %s", log_id, e, exc_info=True)
        log_obj = db.get(CollectionLog, log_id)
        if log_obj:
            log_obj.status        = "error"
            log_obj.message       = error_msg
            log_obj.finished_at   = datetime.now(timezone.utc)
            log_obj.records_saved = max(0, db.query(FinancialRecord).count() - baseline_records)
            db.commit()
    finally:
        st.running          = False
        st.cancel_requested = False
        db.close()


async def _run_collection_bg(years: int, max_co: Optional[int], log_id: int, skip_existing: bool = False):
    async def coro(on_progress, cancel_check, _db):
        return await run_full_collection(
            _db, years, max_co, on_progress=on_progress,
            skip_existing=skip_existing, cancel_check=cancel_check,
        )
    await _run_bg_job(coro, log_id)


async def _run_smart_collection_bg(log_id: int, years: int):
    # モジュールトップでは循環インポートになるため関数内ローカルインポートで単一定義を参照
    from routers.collect import SMART_CHUNK_SIZE, SMART_FULL_THRESHOLD

    async def coro(on_progress, cancel_check, db):
        company_count = db.query(Company).count()
        st = jobs.state(_COLLECTION)
        if company_count >= SMART_FULL_THRESHOLD:
            st.append_log(f"[スマート判定] DB企業数={company_count}社 → 差分収集モード（過去{years}年）")
            return await run_full_collection(
                db, years, None, on_progress=on_progress,
                skip_existing=True, cancel_check=cancel_check,
            )
        elif company_count == 0:
            st.append_log(f"[スマート判定] DB企業数=0社 → 初回チャンク収集（先着{SMART_CHUNK_SIZE}社）")
            return await run_full_collection(
                db, years, SMART_CHUNK_SIZE, on_progress=on_progress,
                skip_existing=False, cancel_check=cancel_check,
            )
        else:
            chunk_no = (company_count // SMART_CHUNK_SIZE) + 1
            target   = chunk_no * SMART_CHUNK_SIZE
            st.append_log(
                f"[スマート判定] DB企業数={company_count}社 → "
                f"チャンク{chunk_no}（先着{target}社のうち未収集を処理）"
            )
            return await run_full_collection(
                db, years, target, on_progress=on_progress,
                skip_existing=True, cancel_check=cancel_check,
            )
    await _run_bg_job(coro, log_id, error_msg="スマート収集でエラーが発生しました（詳細はサーバーログを確認）")


# ── ルーターのインクルード ────────────────────────────────────────────────
# 注: routers/* は import api で共有状態を参照するため、
#     すべての定数・limiter・app・get_db の定義後にインポートする。

from routers import collect as _r_collect
from routers import market  as _r_market
from routers import analysis as _r_analysis
from routers import auth     as _r_auth

app.include_router(_r_collect.router)
app.include_router(_r_market.router)
app.include_router(_r_analysis.router)
app.include_router(_r_auth.router)


# ── システム情報・HTML ページ配信 ──────────────────────────────────────────

@app.get("/api/system/info")
async def system_info():
    return {"render_light_mode": RENDER_LIGHT_MODE}

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

@app.get("/guide")
async def serve_guide():
    return FileResponse(BASE_DIR / "templates" / "guide.html", headers=_NO_CACHE)

@app.get("/db")
async def serve_db_viewer():
    return FileResponse(BASE_DIR / "templates" / "db.html", headers=_NO_CACHE)

@app.get("/company")
async def serve_company_search():
    return FileResponse(BASE_DIR / "templates" / "company.html", headers=_NO_CACHE)

@app.get("/company/{edinet_code}")
async def serve_company(edinet_code: str):
    return FileResponse(BASE_DIR / "templates" / "company.html", headers=_NO_CACHE)

@app.get("/login")
async def serve_login():
    return FileResponse(BASE_DIR / "templates" / "login.html", headers=_NO_CACHE)
