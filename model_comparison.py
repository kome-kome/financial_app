"""将来リターン予測モデル（M-1〜M-6）の OOF バックテスト横並び比較。

`/api/backtest`（as-of 上位 N 社の実現リターン）とは**別手法**。各モデルの execute() が
既に返す `oof_backtest`（無リーク walk-forward・rank-IC / 分位リターン / ロングショート
spread / hit-rate）をまとめて集約するだけで、追加の学習・価格取得はしない。

**退役した（`hidden=True`・ADR-0044）モデルもここには残す**。比較の基準線としての役割は
UI から降ろした後も続くため（M-2 が「無改変のベースライン」として価値を持つのと同じ理由）。
`COMPARISON_MODELS` は評価の土俵であって、サイドバーの品揃えではない。

効率化と副作用抑止（plugins/tuning.py と同じ仕組みを流用）:
  - `tuning_objective_only()`: execute() を oof_backtest 算出後に早期 return させ、
    重い全社スコアリング（M-1 _score_companies / M-2 SHAP / M-3 全社 β 経路）を省く。
  - `tuning_dry_run()`: producer スコア（macro_gbdt_scores / macro_dlm_scores）の永続化を
    no-op にする。既定パラメータの中間予測で本番テーブルを上書きしないため。

Render 軽量モードでは全モデルが heavy=True のため全てスキップ（ローカル実行専用）。
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
    ("macro_ensemble",    "M-4"),   # 兄弟μ̂スタッキング（#367・ADR-0015）
    ("macro_gbdt_rank",   "M-5"),   # M-2 の rank-IC 整合版・learning-to-rank（#362・ADR-0017）
    ("macro_enet",        "M-6"),   # 正則化線形（ElasticNet）・候補メニューから昇格（#372・ADR-0021）
]


def _safe_rollback(db) -> None:
    """session を失敗状態から復帰させる。切断済みで rollback 自体が失敗しても握りつぶす
    （SQLAlchemy は次回利用時にプールから新しい接続を張り直す）。"""
    try:
        db.rollback()
    except Exception:
        pass


async def run_comparison(db: Session, render_light_mode: bool = False,
                         only_models: list[str] | tuple[str, ...] | None = None) -> dict:
    """COMPARISON_MODELS を既定パラメータで実行し oof_backtest を横並びに集約して返す。

    各モデルは per-model で graceful-degrade する（1 モデルの失敗が全体を落とさない）:
      - 未登録            → available=False, reason="not_registered"
      - Render×heavy      → available=False, reason="heavy_render"
      - 依存未充足/契約違反 → available=False, reason="dependency"/"value_error", error=詳細
      - その他例外         → available=False, reason="error", error=詳細

    `only_models`（プラグイン名の列）を渡すと**その部分集合だけ**を走らせる（既定 None=全件）。
    UI からは常に None＝全件で、部分集合は `scripts/model_comparison_run.py`（CLI）用。
    2モデルだけ測りたいとき（例: M-2 vs M-5 の rank-IC 差）に6モデル分の計算を払わずに済み、
    **fold・特徴量・embargo・significance_matrix の手続きは全件時と同一**のまま比較できる。
    アドホックな測定スクリプトを書き起こすと手続きが本番と別物になる（ADR-0041）ため、
    測る側の入口はここに集約する。順序・short ラベルは COMPARISON_MODELS 側が持つ。
    """
    if only_models is None:
        targets = list(COMPARISON_MODELS)
    else:
        unknown = set(only_models) - {n for n, _ in COMPARISON_MODELS}
        if unknown:   # 黙って空の比較を返さない（typo が「全モデル失敗」に化けるのを防ぐ）
            raise ValueError(f"COMPARISON_MODELS に無いモデル名: {sorted(unknown)}")
        targets = [(n, s) for n, s in COMPARISON_MODELS if n in set(only_models)]
    from plugins import get_plugin, execute_plugin, DependencyError, progress
    from database import tuning_objective_only, tuning_dry_run
    from plugins.macro_snapshots import shared_snapshot_cache

    models: list[dict] = []
    # shared_snapshot_cache: 探索軸に依存しない重い共有ロード（M-1/M-2 の load_data、
    # M-3 の load_prices/load_macro_levels）を同一 db セッション内で1回に集約する（Issue
    # #298/#304）。比較ビューは全モデルを連続実行するため、これが無いと 130万行の
    # stock_price_weekly フルロードがモデルごとに走り本番の statement_timeout に当たる。
    with shared_snapshot_cache():
        for i, (name, short) in enumerate(targets, 1):
            # どのモデルを回しているかを画面へ出す（#593）。共通骨格（macro_snapshots）の
            # 進捗はそのまま流れるが、**それだけでは3本のどれを回しているかが分からない**。
            # sink 未設定（CLI・バッチ経路）では emit は完全な no-op。
            progress.emit(f"{short}（{name}）を実行", i - 1, len(targets))
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
                # **成功時は rollback しない**: shared_snapshot_cache がキャッシュした load_data の
                # ORM オブジェクトを expire させず、次モデルが再利用できるようにするため（rollback
                # すると expire_on_rollback で N+1 再クエリ/DetachedInstance を招く）。失敗モデルの
                # ロードは get_or_compute が例外時にキャッシュしないので、後続は安全に再ロードする。
                reason = ("dependency" if isinstance(e, DependencyError)
                          else "value_error" if isinstance(e, ValueError) else "error")
                entry.update(available=False, reason=reason, error=str(e))
                _safe_rollback(db)
            models.append(entry)

    # 終端を必ず流す（#593）。`emit` は間引きでも最後（current==total）を落とさない約束で、
    # ここを出さないと画面は「2/3 のまま完了」に見え、止まったのか終わったのか区別できない。
    progress.emit("モデル間の有意性を集計", len(targets), len(targets))

    # モデル間 rank-IC 差の有意性マトリクス（Issue #369）。各モデルの per-fold IC 系列
    # （oof_backtest.rank_ic_by_period）を共通 test 期でペアリングし、系列相関を保存する
    # 定常ブートストラップで差の平均を検定する（純後処理・追加学習/Egress ゼロ）。
    from model_stats import significance_matrix
    ic_by_model: dict[str, dict] = {}
    for m in models:
        if not m.get("available"):
            continue
        ic_series = (m.get("oof_backtest") or {}).get("rank_ic_by_period")
        if ic_series:   # 非空 dict のモデルのみペアリング対象
            ic_by_model[m["short"]] = ic_series
    sig_matrix = significance_matrix(ic_by_model) if len(ic_by_model) >= 2 else None

    return {
        "models": models,
        "significance_matrix": sig_matrix,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
