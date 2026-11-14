"""売買の記録と運用成績の読む口（#1222）。

`GET /api/portfolio/ledger`（記録の一覧）・`GET /api/portfolio/summary`（保有・損益・期間リターン）・
`GET /api/portfolio/sell-candidates`（記録の保有の売り判定・朝の画面用・#1228）。
どれも読むだけで、計算は `portfolio_ledger.py` と `plugins/sell_ranking.py`。**書く口は `routers/collect.py` に置く**（閲覧専用の
環境で 403 にするルーター依存を通すため・#733）。

Render（Supabase の断面）には記録が無いので、そこでは空の結果が返る。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import api
import plugins as plugin_registry
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


@router.get("/api/portfolio/sell-candidates")
async def portfolio_sell_candidates(db: Session = Depends(api.get_db)):
    """売買の記録の保有を、売り候補ランキングにかけた結果を返す（朝の画面用・#1228・読むだけ）。

    判定の規則は `plugins/sell_ranking.py` のまま（ここで作らない）。保有の出どころを入力欄から記録へ
    替えただけで、渡す書式も分析画面の「売買の記録の保有を入れる」と同じ（`holdings_text`）。

    - 記録に保有が無い: `n_holdings=0`・`ranking=None`
    - 判定できない（M-6 の μ̂ が未保存など）: `ranking=None` と `reason`（朝の画面を 4xx で壊さない）
    """
    holdings = portfolio_ledger.current_holdings(db)
    out = {"n_holdings": len(holdings), "ranking": None, "reason": None}
    if not holdings:
        return out
    plugin = plugin_registry.get_plugin("sell_ranking")
    try:
        out["ranking"] = await plugin_registry.execute_plugin(
            plugin, {"holdings": portfolio_ledger.holdings_text(holdings)}, db)
    except (plugin_registry.DependencyError, ValueError) as e:
        out["reason"] = str(e)
    return out
