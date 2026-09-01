"""hyperparameter_search.py — M-1/M-2/M-3 ハイパーパラメータ自動探索バッチ（Issue #264）。

ローカル専用 CLI（`macro_beta_inference.py` と同じ argparse 様式）。各モデルの
`tuning_search_space()` が定義する探索空間を `plugins.tuning.search()` で評価し、
walk-forward OOF（rank-IC 等）を最大化する best params を選ぶ。

実行:
    python hyperparameter_search.py --model macro_gbdt --strategy random --n-iter 200 \\
        --objective rank_ic --seed 0 --persist --persist-scores

品質ゲート（Issue #291 → #590 で作り直し・ADR-0047）: 「人手レビュー無しの月次自動実行で
本番値を悪化させない」という目的は同じだが、**手段が保存値との比較から候補プールへの
champion 投入に変わった**。永続化済みの objective_value は「そのとき存在したパネルでの値」で
あり、パネルは毎晩伸びるので月をまたいだ単純比較は成立しない（実測: macro_gbdt の 0.5068 は
10 fold・macro_dlm の 0.0221 は 55 fold＝fold が少ない候補ほど高く出ていた）。単純比較は
一度たまたま高い値が入ると永久に閉じる。

いまは本番稼働中の params を今回の探索へ投入し（plugins.tuning.search の champion_params）、
**同一パネル上で** best を選ぶ。best >= champion が構造的に成立するので persist は常に行い、
終了コードは 0 のまま。「水準が落ちた」ことは WARNING ログと plugin_tuned_params の
prev_objective_value / champion_objective_value / n_periods / n_oof_samples に残す。

新規 pip 依存は不要（scikit-learn/xgboost は本番 requirements.txt に既存）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging

logger = logging.getLogger("hyperparameter_search")

# CLI で探索できるモデル（`tuning_search_space()` を実装しているもの）。
# GitHub Actions（tune-hyperparameters.yml）の matrix は M-1/M-2/M-3 の3本のままで、
# macro_enet（M-6・#372）は手動 CLI 専用。M-6 の探索軸は use_momentum / momentum_window だけ
# （α・l1_ratio は学習 fold 内 CV が自動決定するため探索対象にしない）。
MODELS = ("macro_risk_return", "macro_gbdt", "macro_dlm", "macro_enet")
OBJECTIVES = ("rank_ic", "ic_ir", "long_short")


def _data_fingerprint(db) -> str:
    """探索に使ったデータの簡易フィンガープリント（鮮度警告用。厳密なハッシュではなく
    「最終週＋行数」の変化を検知できれば十分という設計・Issue #264）。

    週次株価の高水位は `weekly_price_cache.fingerprint()` から取る（Issue #497）。
    **`max(trade_date)` を自前で書かない**——`trade_date` は週内の最終営業日で PK に含まれず
    nullable、しかも `_recompute_weeks_from_daily` の再集約で同じ週でも書き換わりうる。
    高水位は `week_start`（PK 第2列）であり、ADR-0036 でそう決めた。規則が2つ同居すると
    次に触る人が「前例がある」と言って古い方をコピーする。

    ここでは世代印（`generation`）も含めた3点を使う。値の訂正（#465 の分割段差修復のような
    過去週の書き換え）は max/count では原理的に見えないので、印が進めば指紋も変わる。

    macro 側は `max(trade_date)` のまま。`macro_data` に `week_start` 相当が無く、
    こちらは trade_date が素直に高水位である。
    """
    from sqlalchemy import func
    from database import MacroData
    from weekly_price_cache import fingerprint as weekly_fingerprint

    px = weekly_fingerprint(db)
    max_macro = db.query(func.max(MacroData.trade_date)).scalar()
    raw = f"{px.max_week_start}|{px.n_rows}|{px.generation}|{max_macro}"
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

    persist=True のときは既存行の params を champion として探索へ投入する（#590）。
    persist=False（試し撃ち）では投入しない——本番値を巻き込まずに空間だけを見たい用途で、
    champion を混ぜると探索1件ぶんの時間を余分に使う。
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

    prev = get_tuned_params(db, model) if persist else None

    result = await search(
        plugin, base_params, dims, db,
        objective=objective, strategy=strategy, n_iter=n_iter, seed=seed,
        champion_params=prev["params"] if prev else None,
    )
    result["persisted"] = False

    if persist and result["best_params"] is not None:
        # 前回値との比較は「同じ目的関数で測ったもの同士」でのみ意味を持つ
        # （rank_ic と long_short は次元が違う）。
        prev_score = None
        if prev is not None and prev["objective_name"] == objective:
            prev_score = prev["objective_value"]

        best_oof = result.get("best_oof") or {}
        if prev_score is not None and result["best_score"] < prev_score:
            # persist は止めない（ADR-0047）。champion は候補プールに居るので
            # best >= champion が成立しており、本番より悪い params を選ぶことはない。
            # ここで下がっているのは**パネルが変わったこと**による水準の移動なので、
            # バッチを失敗させずに履歴として残す。
            logger.warning(
                "前回の保存値%.4f（%s・%s fold）を下回りました: 今回=%.4f（%s fold）"
                "・champion 再測定=%s。パネル世代が変わった可能性があります"
                "（ADR-0047・比較は同一パネル上の champion で行っています）",
                prev_score, prev["tuned_at"], prev.get("n_periods"),
                result["best_score"], best_oof.get("n_periods"),
                result.get("champion_score"),
            )

        fp = _data_fingerprint(db)
        upsert_tuned_params(
            db, model, result["best_params"], objective,
            result["best_score"], result["leaderboard"][:20],
            result["config"]["n_combos"], fp,
            prev_objective_value=prev["objective_value"] if prev else None,
            champion_objective_value=result.get("champion_score"),
            n_periods=best_oof.get("n_periods"),
            n_oof_samples=best_oof.get("n_oof_samples"),
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

        if result["champion_injected"]:
            logger.info("champion 再測定スコア=%s（同一パネル上の比較・ADR-0047）",
                        result["champion_score"])
        if result["persisted"]:
            logger.info("plugin_tuned_params へ永続化しました（plugin_name=%s）", args.model)
            if args.persist_scores:
                logger.info("best params で最終 execute を実行し、producer スコアを永続化しました")
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
