"""認証・ヘルスチェック API ルーター。

/api/auth/* および /health を担当。
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

import api

router = APIRouter()
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent


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


@router.get("/health")
async def health_check():
    """死活監視用エンドポイント。DB 接続を含む基本疎通を確認する。"""
    db_ok = False
    try:
        db = api.SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
        finally:
            db.close()
    except Exception as e:
        log.error("ヘルスチェック DB エラー: %s", e, exc_info=True)
    from fastapi.responses import JSONResponse
    status_code = 200 if db_ok else 503
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"},
        status_code=status_code,
    )


@router.post("/api/auth/login")
@api.limiter.limit(api.RATELIMIT_AUTH)
async def auth_login(request: Request, req: LoginRequest, response: Response):
    if not api.APP_PASSWORD:
        return {"ok": True, "dev_mode": True}
    import hmac
    if not hmac.compare_digest(req.password.encode(), api.APP_PASSWORD.encode()):
        raise HTTPException(401, "パスワードが違います")
    api._set_auth_cookies(response, api._create_token(), api._create_csrf())
    return {"ok": True}


@router.post("/api/auth/reset-password")
@api.limiter.limit(api.RATELIMIT_RESET)
async def reset_password(request: Request, req: ResetPasswordRequest):
    import hmac
    if not api.APP_RECOVERY_KEY:
        raise HTTPException(503, "回復キーが設定されていません（APP_RECOVERY_KEY を .env に設定してください）")
    if not hmac.compare_digest(req.recovery_key.encode(), api.APP_RECOVERY_KEY.encode()):
        raise HTTPException(401, "回復キーが違います")
    new_pw = req.new_password.strip()
    if not new_pw:
        raise HTTPException(400, "新しいパスワードを入力してください")
    if len(new_pw) < 8:
        raise HTTPException(400, "パスワードは8文字以上で設定してください")
    api.APP_PASSWORD = new_pw
    _update_env_file("APP_PASSWORD", api.APP_PASSWORD)
    return {"message": "パスワードを更新しました"}


@router.get("/api/auth/status")
async def auth_status():
    return {"auth_required": bool(api.APP_PASSWORD), "recovery_available": bool(api.APP_RECOVERY_KEY)}


@router.post("/api/auth/logout")
async def auth_logout(response: Response):
    """認証 Cookie を削除する。/api/auth/ 配下のため CSRF/認証チェックは免除。"""
    api._clear_auth_cookies(response)
    return {"ok": True}
