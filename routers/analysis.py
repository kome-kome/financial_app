"""分析プラグイン・バックテスト API ルーター。

/api/plugins/*, /api/gap-analysis, /api/recommend, /api/backtest を担当。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import api
import backtest
import hyperparameter_search
import plugins as plugin_registry
from database import SessionLocal

router = APIRouter()
log = logging.getLogger(__name__)


# サイドバーIA用の「特例エントリ」。AnalysisPlugin ではない分析（スクリーニング・バックテスト）を
# プラグインと同じメタ形(name/label/category/ui_order)で /api/plugins に並べ、フロントの統一サイドバーへ載せる。
# 完全プラグイン化はしない（backtest は GET・params_schema 非使用・マルチピリオドで契約に馴染まないため）。
# href を持つエントリはタブを持たず、サイドバーで別ページへのリンクとして描画される。
SPECIAL_ANALYSES = [
    {
        "name": "screen",
        "label": "スクリーニング",
        "description": "ROE・PER・自己資本比率などの財務条件で銘柄を絞り込みます",
        "depends_on": [],
        "heavy": False,
        "category": "① 銘柄を探す",
        "ui_order": 130,
        "params_schema": {},
        "href": "/collection",  # 既存UIは収集ページ。分析ハブへの統合は後続PRで対応
    },
    {
        "name": "backtest",
        "label": "バックテスト",
        "description": "過去時点でのスコアリング（おすすめ／バリュエーション／ネットキャッシュ）の期待リターン（その後の株価変化）を検証します",
        "depends_on": [],
        "heavy": False,
        "category": "④ 戦略を検証",
        "ui_order": 410,
        "params_schema": {},  # 専用UI（既存タブ）を使用するため空
    },
]


@router.get("/api/plugins")
async def list_plugins():
    """分析メタ一覧。プラグイン + 特例エントリ(screen/backtest)を ui_order 昇順で返す。"""
    metas = [p.to_meta() for p in plugin_registry.list_plugins()]
    metas.extend(SPECIAL_ANALYSES)
    metas.sort(key=lambda m: m.get("ui_order", 999))
    return {"plugins": metas}


@router.get("/api/model/status")
async def model_status(db: Session = Depends(api.get_db)):
    """業種別OLSモデルの鮮度情報。鮮度バーUI用。"""
    import datetime
    from sqlalchemy import func
    from database import FinancialRecord, RegressionResult

    rq = db.query(RegressionResult).filter(RegressionResult.gap_ratio.isnot(None))
    computed_at = rq.with_entities(func.max(RegressionResult.computed_at)).scalar()
    n_results = rq.count()
    data_updated_at = db.query(func.max(FinancialRecord.updated_at)).scalar()

    staleness_days = None
    is_stale = False
    if computed_at:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        staleness_days = (now - computed_at).days
        if data_updated_at:
            is_stale = computed_at < data_updated_at

    return {
        "computed_at": computed_at.isoformat() if computed_at else None,
        "staleness_days": staleness_days,
        "n_results": n_results,
        "is_stale": is_stale,
    }


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


@router.get("/api/plugins/{plugin_name}/tuned")
async def get_plugin_tuned(plugin_name: str, db: Session = Depends(api.get_db)):
    """自動調整済みハイパーパラメータ（Issue #264・hyperparameter_search.py --persist が
    書き込む）を読む。読取専用・軽量（重い計算は起こさない）。未調整なら404。"""
    from database import get_tuned_params

    tuned = get_tuned_params(db, plugin_name)
    if tuned is None:
        raise HTTPException(404, f"'{plugin_name}' は自動調整されていません")
    return tuned


# ── ハイパーパラメータ探索の GUI 統合（Issue #278）───────────────────────────
# hyperparameter_search.py（CLI専用・Issue #264）を collection_jobs.py の
# ジョブ registry（routers/collect.py の history/reparse 等と同型）でバックグラウンド
# ジョブ化し、SSE で進捗配信する。plugin_tuned_params への永続化は常時行う
# （GUIから起動する時点で best params を残す意図がある）。producer スコアの上書きは
# 影響が大きいため persist_scores のみ明示的な opt-in。

class TuneStartRequest(BaseModel):
    strategy:       str            = "random"
    n_iter:         int             = Field(default=50, ge=5, le=500)
    objective:      str            = "rank_ic"
    seed:           int             = 0
    persist_scores: bool           = False


def _tuning_job_name(plugin_name: str) -> str:
    return f"tuning_{plugin_name}"


@router.post("/api/plugins/{plugin_name}/tune-start")
async def start_plugin_tuning(
    plugin_name: str, req: TuneStartRequest, background_tasks: BackgroundTasks,
):
    if plugin_name not in hyperparameter_search.MODELS:
        raise HTTPException(404, f"'{plugin_name}' はハイパーパラメータ探索に対応していません")
    if api.RENDER_LIGHT_MODE:
        raise HTTPException(403, "ハイパーパラメータ探索はローカル環境で実行してください"
                                 "（Render Free プラン制限。結果は共有DBに保存され本番に反映されます）")
    if req.strategy not in ("grid", "random"):
        raise HTTPException(400, "strategy は grid または random を指定してください")
    if req.objective not in hyperparameter_search.OBJECTIVES:
        raise HTTPException(400, f"objective は {', '.join(hyperparameter_search.OBJECTIVES)} のいずれか")

    async def body(on_progress, cancel_check):
        # 探索候補の評価（XGBoost fit 等）は同期・CPU-bound で await を挟まないため、
        # await するだけでは asyncio イベントループを丸ごとブロックしてしまい、
        # SSE進捗配信・tune-status が探索完了までまったく応答しなくなる
        # （Issue #278 が解決したい「押した後、完了まで無反応」を再現してしまう）。
        # 別スレッドへオフロードしてイベントループを解放する。
        def _run_blocking() -> None:
            db = SessionLocal()
            try:
                asyncio.run(hyperparameter_search.run_search(
                    plugin_name, req.strategy, req.n_iter, req.objective, req.seed, db,
                    persist=True, persist_scores=req.persist_scores,
                    on_progress=on_progress, cancel_check=cancel_check,
                ))
            finally:
                db.close()

        await asyncio.to_thread(_run_blocking)

    api.jobs.start(_tuning_job_name(plugin_name), background_tasks, body,
                   busy_message=f"'{plugin_name}' のハイパーパラメータ探索は既に実行中です",
                   error_message="[エラー] 探索中に問題が発生しました（詳細はサーバーログを確認）")
    return {"message": "ハイパーパラメータ探索を開始しました"}


@router.post("/api/plugins/{plugin_name}/tune-stop")
async def stop_plugin_tuning(plugin_name: str):
    if not api.jobs.is_running(_tuning_job_name(plugin_name)):
        raise HTTPException(400, f"'{plugin_name}' の実行中の探索ジョブがありません")
    api.jobs.request_cancel(_tuning_job_name(plugin_name))
    return {"message": "探索の停止リクエストを送信しました"}


@router.get("/api/plugins/{plugin_name}/tune-status")
async def plugin_tuning_status(plugin_name: str):
    return api.jobs.snapshot(_tuning_job_name(plugin_name))


@router.get("/api/plugins/{plugin_name}/tune-stream")
async def plugin_tuning_stream(plugin_name: str):
    return api.jobs.stream(_tuning_job_name(plugin_name))


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
    source: str = "recommend",
    db: Session = Depends(api.get_db),
):
    if not (1 <= months_ago <= 60):
        raise HTTPException(400, "months_ago は 1〜60 の範囲で指定してください")
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    if source not in backtest.SCORING_SOURCES:
        raise HTTPException(400, f"source は {', '.join(backtest.SCORING_SOURCES)} のいずれか")
    try:
        return backtest.run(db, preset, months_ago, top_n, industry, min_market_cap, source)
    except Exception as e:
        log.error("Backtest error: %s", e, exc_info=True)
        raise HTTPException(500, "バックテスト実行エラーが発生しました。")


@router.get("/api/backtest/multi")
async def backtest_multi(
    preset: str = "バランス型",
    top_n: int = 20,
    industry: Optional[str] = None,
    min_market_cap: Optional[float] = None,
    source: str = "recommend",
    db: Session = Depends(api.get_db),
):
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    if source not in backtest.SCORING_SOURCES:
        raise HTTPException(400, f"source は {', '.join(backtest.SCORING_SOURCES)} のいずれか")
    periods = []
    for m in backtest.MULTI_PERIODS:
        try:
            periods.append(backtest.run(db, preset, m, top_n, industry, min_market_cap, source))
        except Exception as e:
            log.error("Backtest multi error (months=%d): %s", m, e, exc_info=True)
            periods.append({"holding_months": m, "summary": None, "results": [],
                            "total_candidates": 0, "error": "計算エラー"})
    return {"periods": periods, "preset": preset, "top_n": top_n, "source": source}
