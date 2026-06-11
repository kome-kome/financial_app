"""分析プラグイン・バックテスト API ルーター。

/api/plugins/*, /api/gap-analysis, /api/recommend, /api/backtest を担当。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import api
import backtest
import plugins as plugin_registry

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/api/plugins")
async def list_plugins():
    return {"plugins": [p.to_meta() for p in plugin_registry.list_plugins()]}


@router.post("/api/plugins/{plugin_name}/run", response_model=None)
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def run_plugin(
    request: Request, plugin_name: str, params: dict,
    db: Session = Depends(api.get_db),
):
    p = plugin_registry.get_plugin(plugin_name)
    if p is None:
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    if api.RENDER_LIGHT_MODE and getattr(p, "heavy", False):
        raise HTTPException(403, f"「{p.label}」は計算が重いためローカル環境で実行してください"
                                 "（Render Free プラン制限。結果は共有DBに保存され本番に反映されます）")
    try:
        return await plugin_registry.execute_plugin(p, params, db)
    except plugin_registry.DependencyError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Plugin '%s' error: %s", plugin_name, e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


@router.get("/api/gap-analysis")
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def gap_analysis(
    request: Request,
    year: Optional[int] = None,
    sort: str = "asc",
    db: Session = Depends(api.get_db),
):
    p = plugin_registry.get_plugin("gap_analysis")
    try:
        return await plugin_registry.execute_plugin(p, {"year": year, "sort": sort}, db)
    except plugin_registry.DependencyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        log.error("Gap-analysis error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


@router.get("/api/recommend/presets")
async def get_recommend_presets():
    from plugins.recommend import PRESETS, METRICS
    return {"presets": PRESETS, "metrics": METRICS}


@router.post("/api/recommend")
async def recommend_stocks(req: dict, db: Session = Depends(api.get_db)):
    p = plugin_registry.get_plugin("recommend")
    try:
        return await plugin_registry.execute_plugin(p, req, db)
    except plugin_registry.DependencyError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Recommend error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


@router.get("/api/backtest")
async def run_backtest(
    preset: str = "バランス型",
    months_ago: int = 6,
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    db: Session = Depends(api.get_db),
):
    if not (1 <= months_ago <= 60):
        raise HTTPException(400, "months_ago は 1〜60 の範囲で指定してください")
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    try:
        return backtest.run(db, preset, months_ago, top_n, industry, min_market_cap)
    except Exception as e:
        log.error("Backtest error: %s", e, exc_info=True)
        raise HTTPException(500, "バックテスト実行エラーが発生しました。")


@router.get("/api/backtest/multi")
async def backtest_multi(
    preset: str = "バランス型",
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    db: Session = Depends(api.get_db),
):
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    periods = []
    for m in backtest.MULTI_PERIODS:
        try:
            periods.append(backtest.run(db, preset, m, top_n, industry, min_market_cap))
        except Exception as e:
            log.error("Backtest multi error (months=%d): %s", m, e, exc_info=True)
            periods.append({"holding_months": m, "summary": None, "results": [],
                            "total_candidates": 0, "error": "計算エラー"})
    return {"periods": periods, "preset": preset, "top_n": top_n}
