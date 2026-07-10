"""hyperparameter_search.py — M-1/M-2/M-3 ハイパーパラメータ自動探索バッチ（Issue #264）。

ローカル専用 CLI（`macro_beta_inference.py` と同じ argparse 様式）。各モデルの
`tuning_search_space()` が定義する探索空間を `plugins.tuning.search()` で評価し、
walk-forward OOF（rank-IC 等）を最大化する best params を選ぶ。

実行:
    python hyperparameter_search.py --model macro_gbdt --strategy random --n-iter 200 \\
        --objective rank_ic --seed 0 --persist --persist-scores

品質ゲート（Issue #291）: --persist 時、plugin_tuned_params に既存行があれば
その objective_value と今回の best_score を比較し、劣化していれば persist
（persist_scores 込み）をスキップして非ゼロ終了する。GitHub Actions での
月次自動実行を人手レビュー無しで運用しても本番値が悪化しないようにするため。

新規 pip 依存は不要（scikit-learn/xgboost は本番 requirements.txt に既存）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging

logger = logging.getLogger("hyperparameter_search")

MODELS = ("macro_risk_return", "macro_gbdt", "macro_dlm")
OBJECTIVES = ("rank_ic", "ic_ir", "long_short")


def _data_fingerprint(db) -> str:
    """探索に使ったデータの簡易フィンガープリント（鮮度警告用。厳密なハッシュではなく
    「最終trade_date＋行数」の変化を検知できれば十分という設計・Issue #264）。"""
    from sqlalchemy import func
    from database import MacroData, StockPriceWeekly

    max_px = db.query(func.max(StockPriceWeekly.trade_date)).scalar()
    n_px = db.query(func.count()).select_from(StockPriceWeekly).scalar()
    max_macro = db.query(func.max(MacroData.trade_date)).scalar()
    raw = f"{max_px}|{n_px}|{max_macro}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def run_search(
    model: str,
    strategy: str,
    n_iter: int,
    objective: str,
    seed: int,
    db,
    *,
    persist: bool = False,
    persist_scores: bool = False,
) -> dict:
    """1モデル分の探索を実行する共有ロジック（CLI・GitHub Actionsから呼ぶ・Issue #264）。

    persist=True で plugin_tuned_params へ永続化し、persist_scores=True なら
    best params での最終 execute も行い producer スコアを永続化する
    （persist=False のとき persist_scores は無視される）。
    """
    from database import get_tuned_params, upsert_tuned_params
    from plugins import execute_plugin, get_plugin
    from plugins.tuning import search

    plugin = get_plugin(model)
    if plugin is None:
        raise ValueError(f"プラグイン '{model}' が見つかりません")
    space_fn = getattr(plugin, "tuning_search_space", None)
    if space_fn is None:
        raise ValueError(f"プラグイン '{model}' は tuning_search_space() 未実装です")
    base_params, dims = space_fn()

    result = await search(
        plugin, base_params, dims, db,
        objective=objective, strategy=strategy, n_iter=n_iter, seed=seed,
    )
    result["persisted"] = False
    result["skipped_reason"] = None

    if persist and result["best_params"] is not None:
        prev = get_tuned_params(db, model)
        prev_score = prev["objective_value"] if prev is not None else None

        if prev_score is not None and result["best_score"] < prev_score:
            # 品質ゲート（Issue #291）: 探索結果が既存の永続値より劣化している場合は
            # 人手レビュー抜きで本番 plugin_tuned_params / producer スコアを
            # 上書きしない（GitHub Actions 月次自動実行を見据えた劣化防止）。
            reason = (
                f"前回スコア{prev_score:.4f}を下回ったため persist をスキップしました"
                f"（今回={result['best_score']:.4f}）"
            )
            logger.warning(reason)
            result["skipped_reason"] = reason
            return result

        fp = _data_fingerprint(db)
        upsert_tuned_params(
            db, model, result["best_params"], objective,
            result["best_score"], result["leaderboard"][:20],
            result["config"]["n_combos"], fp,
        )
        result["persisted"] = True

        if persist_scores:
            await execute_plugin(plugin, result["best_params"], db)

    return result


async def _run(args: argparse.Namespace) -> None:
    from database import SessionLocal

    db = SessionLocal()
    try:
        try:
            result = await run_search(
                args.model, args.strategy, args.n_iter, args.objective, args.seed, db,
                persist=args.persist, persist_scores=args.persist_scores,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        logger.info("探索完了: best_score=%.4f（objective=%s）", result["best_score"], args.objective)
        logger.info("best_params=%s", json.dumps(result["best_params"], ensure_ascii=False))
        logger.info("config=%s", result["config"])
        top5 = result["leaderboard"][:5]
        logger.info("リーダーボード上位%d件:\n%s", len(top5),
                   json.dumps(top5, ensure_ascii=False, indent=2, default=str))

        if result["persisted"]:
            logger.info("plugin_tuned_params へ永続化しました（plugin_name=%s）", args.model)
            if args.persist_scores:
                logger.info("best params で最終 execute を実行し、producer スコアを永続化しました")
        elif result["skipped_reason"]:
            raise SystemExit(f"品質ゲートによりスキップされました: {result['skipped_reason']}")
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="M-1/M-2/M-3 ハイパーパラメータ自動探索バッチ（Issue #264）"
    )
    ap.add_argument("--model", required=True, choices=MODELS)
    ap.add_argument("--strategy", default="random", choices=("grid", "random"))
    ap.add_argument("--n-iter", type=int, default=50, dest="n_iter",
                    help="strategy=random のときのサンプリング数（grid では無視）")
    ap.add_argument("--objective", default="rank_ic", choices=OBJECTIVES)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--persist", action="store_true",
                    help="best params を plugin_tuned_params へ永続化する")
    ap.add_argument("--persist-scores", action="store_true", dest="persist_scores",
                    help="--persist と併用。best params で最終 execute を1回実行し"
                         "producer スコア（macro_gbdt_scores 等）を永続化する")
    args = ap.parse_args()

    if args.persist_scores and not args.persist:
        ap.error("--persist-scores は --persist と併用してください")

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
