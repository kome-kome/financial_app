"""将来リターン予測モデル（M-1/M-2/M-3）の OOF バックテスト横並び比較。

`/api/backtest`（as-of 上位 N 社の実現リターン）とは**別手法**。各モデルの execute() が
既に返す `oof_backtest`（無リーク walk-forward・rank-IC / 分位リターン / ロングショート
spread / hit-rate）を 3 モデル分まとめて集約するだけで、追加の学習・価格取得はしない。

効率化と副作用抑止（plugins/tuning.py と同じ仕組みを流用）:
  - `tuning_objective_only()`: execute() を oof_backtest 算出後に早期 return させ、
    重い全社スコアリング（M-1 _score_companies / M-2 SHAP / M-3 全社 β 経路）を省く。
  - `tuning_dry_run()`: producer スコア（macro_gbdt_scores / macro_dlm_scores）の永続化を
    no-op にする。既定パラメータの中間予測で本番テーブルを上書きしないため。

Render 軽量モードでは 3 モデルとも heavy=True のため全てスキップ（ローカル実行専用）。
その旨を各モデルの `reason="heavy_render"` で返し、UI が案内する。interface は
`(db, render_light_mode) -> dict` で FastAPI に依存せず直接テストできる（tests/test_model_comparison.py）。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

# 比較対象（表示順）。name=プラグイン名 / short=UI 短縮ラベル。
COMPARISON_MODELS = [
    ("macro_risk_return", "M-1"),
    ("macro_gbdt",        "M-2"),
    ("macro_dlm",         "M-3"),
]


def _safe_rollback(db) -> None:
    """session を失敗状態から復帰させる。切断済みで rollback 自体が失敗しても握りつぶす
    （SQLAlchemy は次回利用時にプールから新しい接続を張り直す）。"""
    try:
        db.rollback()
    except Exception:
        pass


async def run_comparison(db: Session, render_light_mode: bool = False) -> dict:
    """M-1/M-2/M-3 を既定パラメータで実行し oof_backtest を横並びに集約して返す。

    各モデルは per-model で graceful-degrade する（1 モデルの失敗が全体を落とさない）:
      - 未登録            → available=False, reason="not_registered"
      - Render×heavy      → available=False, reason="heavy_render"
      - 依存未充足/契約違反 → available=False, reason="dependency"/"value_error", error=詳細
      - その他例外         → available=False, reason="error", error=詳細
    """
    from plugins import get_plugin, execute_plugin, DependencyError
    from database import tuning_objective_only, tuning_dry_run
    from plugins.macro_snapshots import tuning_snapshot_cache

    models: list[dict] = []
    # tuning_snapshot_cache: 探索軸に依存しない重い共有ロード（M-1/M-2 の load_data、
    # M-3 の load_prices/load_macro_levels）を同一 db セッション内で1回に集約する（Issue
    # #298/#304）。比較ビューは 3モデルを連続実行するため、これが無いと 95万行の
    # stock_price_weekly フルロードがモデルごとに走り本番の statement_timeout に当たる。
    with tuning_snapshot_cache():
        for name, short in COMPARISON_MODELS:
            entry: dict = {"name": name, "short": short}
            p = get_plugin(name)
            if p is None:
                entry.update(available=False, reason="not_registered")
                models.append(entry)
                continue
            entry["label"] = p.label
            if render_light_mode and getattr(p, "heavy", False):
                entry.update(available=False, reason="heavy_render")
                models.append(entry)
                continue
            try:
                # 既定パラメータ（{} → coerce_params がスキーマ既定で補完）で oof のみ取得。
                with tuning_objective_only(), tuning_dry_run():
                    res = await execute_plugin(p, {}, db)
                entry.update(
                    available=True,
                    oof_backtest=res.get("oof_backtest") or {},
                    macro_features=res.get("macro_features"),
                )
            except Exception as e:  # noqa: BLE001 — per-model で握って比較全体は継続
                # 1モデルが DB エラー（接続切断・トランザクション失敗）で落ちると session が
                # 失敗状態のまま残り、後続モデルが "invalid transaction" で連鎖失敗する。
                # 失敗したモデルだけ rollback して session を洗い、後続を独立に評価する。
                # **成功時は rollback しない**: tuning_snapshot_cache がキャッシュした load_data の
                # ORM オブジェクトを expire させず、次モデルが再利用できるようにするため（rollback
                # すると expire_on_rollback で N+1 再クエリ/DetachedInstance を招く）。失敗モデルの
                # ロードは get_or_compute が例外時にキャッシュしないので、後続は安全に再ロードする。
                reason = ("dependency" if isinstance(e, DependencyError)
                          else "value_error" if isinstance(e, ValueError) else "error")
                entry.update(available=False, reason=reason, error=str(e))
                _safe_rollback(db)
            models.append(entry)

    return {
        "models": models,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
