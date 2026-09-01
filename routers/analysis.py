"""分析プラグイン・バックテスト API ルーター。

/api/plugins/*, /api/gap-analysis, /api/recommend, /api/backtest を担当。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import api
import backtest
import model_comparison
import plugins as plugin_registry
from collection_jobs import jobs
from plugins import progress

router = APIRouter()
log = logging.getLogger(__name__)


def _progress_job(plugin_name: str) -> str:
    """進捗 JobState のスロット名。収集ジョブと同じ registry へ相乗りする（#545）。

    プラグイン名でスロットを分けるのは「種別ごとに独立スロット」という registry の
    設計に合わせるため（別モデルを続けて回しても互いのログを踏まない）。
    """
    return f"plugin:{plugin_name}"


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
    {
        "name": "model_comparison",
        "label": "モデル比較（OOF）",
        "description": "将来リターン予測モデル M-1〜M-6 の予測力（rank-IC・ロングショート spread・hit-rate）を無リーク OOF で横並び比較します（/api/backtest の as-of 上位N とは別手法）。退役した M-4/M-5 もここには並びます",
        "depends_on": [],
        "heavy": True,   # 全モデル（heavy）を実行するため。Render では各モデルがスキップされる
        "category": "④ 戦略を検証",
        "ui_order": 420,
        "params_schema": {},  # 専用UI（静的タブ）を使用するため空
    },
]


@router.get("/api/plugins")
async def list_plugins():
    """分析メタ一覧。プラグイン + 特例エントリ(screen/backtest)を ui_order 昇順で返す。

    `hidden=True` のプラグインは除外する（ADR-0044 の退役＝サイドバーに出さない）。除外は
    ここだけで、レジストリ・`/api/plugins/{name}/run`・`model_comparison` には残る。
    """
    metas = [p.to_meta() for p in plugin_registry.list_plugins() if not p.hidden]
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
        if getattr(p, "heavy", False):
            return await _execute_with_progress(p, plugin_name, params, db)
        return await plugin_registry.execute_plugin(p, params, db)
    except (plugin_registry.DependencyError, ValueError) as e:
        # パラメータ契約違反・依存不足のドメイン検証メッセージ（内部実装は露出しない）。
        # 可観測性のためログにも残す（#346）。
        log.info("プラグイン '%s' の実行を検証で拒否: %s", plugin_name, e)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("Plugin '%s' error: %s", plugin_name, e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


async def _execute_with_progress(p, plugin_name: str, params: dict, db):
    """heavy プラグインを進捗 sink で包んで実行する（Issue #545）。

    包むのは **heavy だけ**。軽いプラグインまで JobState を回すと、画面が開いていない
    実行（`/api/recommend` 等）でも状態が残り続けて意味が無い。sink は ContextVar 経由で
    execute の内側（`asyncio.to_thread` の先）まで伝播する。

    例外は握らず送出する（呼び出し側の except が HTTP ステータスへマップする契約を保つ）
    が、**閉じる前に最後の1行として画面へ残す**——ここで黙って落ちると、画面は
    ストリームが切れただけになり「終わった」と区別できない。
    """
    st = jobs.state(_progress_job(plugin_name))
    st.reset_for_run()
    st.append_log(f"「{p.label}」を開始しました")

    def sink(step: str, current: int, total: int) -> None:
        st.progress, st.total = current, total
        st.append_log(f"{step} {current}/{total}" if total else step)

    try:
        with progress.progress_sink(sink):
            return await plugin_registry.execute_plugin(p, params, db)
    except Exception as e:
        st.append_log(f"[エラー] {e}")
        raise
    finally:
        # running=False が SSE の終端。append_log より後（最後の1件に載せるため）。
        st.running = False


@router.get("/api/plugins/{plugin_name}/progress")
async def stream_plugin_progress(plugin_name: str):
    """heavy プラグイン実行の進捗を SSE で流す（Issue #545）。

    画面は POST の応答を待たずにこれを開く（POST は完了まで返らないため）。開始前に
    開かれても `stream_awaiting_start` が数秒待ち合わせる。
    """
    p = plugin_registry.get_plugin(plugin_name)
    if p is None:
        raise HTTPException(404, f"プラグイン '{plugin_name}' が見つかりません")
    return jobs.stream_awaiting_start(_progress_job(plugin_name))


@router.get("/api/plugins/{plugin_name}/tuned")
async def get_plugin_tuned(plugin_name: str, db: Session = Depends(api.get_db)):
    """自動調整済みハイパーパラメータ（Issue #264・hyperparameter_search.py --persist が
    書き込む）を読む。読取専用・軽量（重い計算は起こさない）。未調整なら404。"""
    from database import get_tuned_params

    tuned = get_tuned_params(db, plugin_name)
    if tuned is None:
        raise HTTPException(404, f"'{plugin_name}' は自動調整されていません")
    return tuned


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
    except (plugin_registry.DependencyError, ValueError) as e:
        # sector_ols 未実行等の依存/検証エラー（内部実装は露出しない）。可観測性のためログにも残す（#346）。
        log.info("gap-analysis を依存/検証で拒否: %s", e)
        raise HTTPException(404, str(e))
    except Exception as e:
        log.error("Gap-analysis error: %s", e, exc_info=True)
        raise HTTPException(500, "分析エラーが発生しました。")


@router.get("/api/recommend/presets")
async def get_recommend_presets(db: Session = Depends(api.get_db)):
    from plugins.recommend import METRICS, get_all_presets
    return {"presets": get_all_presets(db), "metrics": METRICS}


@router.post("/api/recommend")
async def recommend_stocks(req: dict, db: Session = Depends(api.get_db)):
    p = plugin_registry.get_plugin("recommend")
    try:
        return await plugin_registry.execute_plugin(p, req, db)
    except (plugin_registry.DependencyError, ValueError) as e:
        # パラメータ契約違反・依存不足のドメイン検証メッセージ（内部実装は露出しない）。可観測性のためログにも残す（#346）。
        log.info("recommend を依存/検証で拒否: %s", e)
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
    cost_bps: float = 0.0,
    db: Session = Depends(api.get_db),
):
    if not (1 <= months_ago <= 60):
        raise HTTPException(400, "months_ago は 1〜60 の範囲で指定してください")
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    if source not in backtest.SCORING_SOURCES:
        raise HTTPException(400, f"source は {', '.join(backtest.SCORING_SOURCES)} のいずれか")
    if not (0 <= cost_bps <= 100):
        raise HTTPException(400, "cost_bps は 0〜100 の範囲で指定してください")
    try:
        return backtest.run(db, preset, months_ago, top_n, industry, min_market_cap, source, cost_bps)
    except ValueError as e:
        # 非対応の重み（mu＝μ̂・Issue #423 子4）等のドメイン検証。500 にすると
        # 「壊れている」と読めてしまうため 400 で理由を返す。
        log.info("backtest を検証で拒否: %s", e)
        raise HTTPException(400, str(e))
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
    cost_bps: float = 0.0,
    db: Session = Depends(api.get_db),
):
    if not (5 <= top_n <= 100):
        raise HTTPException(400, "top_n は 5〜100 の範囲で指定してください")
    if source not in backtest.SCORING_SOURCES:
        raise HTTPException(400, f"source は {', '.join(backtest.SCORING_SOURCES)} のいずれか")
    if not (0 <= cost_bps <= 100):
        raise HTTPException(400, "cost_bps は 0〜100 の範囲で指定してください")
    periods = []
    for m in backtest.MULTI_PERIODS:
        try:
            periods.append(backtest.run(db, preset, m, top_n, industry, min_market_cap, source, cost_bps))
        except ValueError as e:
            # 重み由来の非対応（mu）はどの期間でも同じ結論なので、期間ごとに「計算エラー」を
            # 並べず即 400 で理由を返す（Issue #423 子4）。
            log.info("backtest multi を検証で拒否: %s", e)
            raise HTTPException(400, str(e))
        except Exception as e:
            log.error("Backtest multi error (months=%d): %s", m, e, exc_info=True)
            periods.append({"holding_months": m, "summary": None, "results": [],
                            "total_candidates": 0, "error": "計算エラー"})
    return {"periods": periods, "preset": preset, "top_n": top_n, "source": source, "cost_bps": cost_bps}


@router.post("/api/backtest/model-comparison", response_model=None)
@api.limiter.limit(api.RATELIMIT_ANALYSIS)
async def backtest_model_comparison(request: Request, db: Session = Depends(api.get_db)):
    """将来リターン予測モデル M-1/M-2/M-3 の OOF バックテストを横並び比較する。

    3モデルとも heavy=True のため、Render 軽量モードでは各モデルが reason="heavy_render" で
    スキップされる（ローカル実行専用）。個々のモデル失敗は per-model で握り、比較全体は継続する。
    """
    try:
        return await model_comparison.run_comparison(db, render_light_mode=api.RENDER_LIGHT_MODE)
    except Exception as e:
        log.error("Model comparison error: %s", e, exc_info=True)
        raise HTTPException(500, "モデル比較の実行エラーが発生しました。")
