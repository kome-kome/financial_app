"""売買の記録と運用成績の読む口（#1222）。

`GET /api/portfolio/ledger`（記録の一覧）と `GET /api/portfolio/summary`（保有・損益・期間リターン）。
どちらも読むだけで、計算は `portfolio_ledger.py`。**書く口は `routers/collect.py` に置く**（閲覧専用の
環境で 403 にするルーター依存を通すため・#733）。

Render（Supabase の断面）には記録が無いので、そこでは空の結果が返る。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import api
import portfolio_ledger

router = APIRouter()


@router.get("/api/portfolio/ledger")
def list_trade_ledger(db: Session = Depends(api.get_db)):
    """記録を新しい順に返す。取り消された行は `voided=True`（消さずに見せる）。"""
    rows = portfolio_ledger.load_rows(db)
    voided = {r["voids_id"] for r in rows if r["side"] == portfolio_ledger.SIDE_VOID}
    return {"entries": [dict(r, voided=r["id"] in voided) for r in reversed(rows)]}


@router.get("/api/portfolio/summary")
def portfolio_summary(db: Session = Depends(api.get_db)):
    return portfolio_ledger.summary(db)
