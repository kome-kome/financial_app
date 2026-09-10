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

**完走してからしか永続化しない設計はやめた**（Issue #638・ADR-0054）。予算を超えると
`batch_common.kill_tree()` が `taskkill /F /T` でツリーごと落とすため子に猶予は無く、
2026-09-01 には 250分ぶんの計算が2回とも成果ゼロで消えた（うち片方は M-3 の μ̂ が
59.5日固着する直接の原因）。いまは①締切の手前で自分から探索を畳んで永続化まで終える
②候補ごとに暫定ベストを保全する、の2本立てで途中経過を残す。畳んだかどうかは
`plugin_tuned_params` の `n_combos < n_combos_planned` で判別できる。

新規 pip 依存は不要（scikit-learn/xgboost は本番 requirements.txt に既存）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("hyperparameter_search")

# 親バッチ（`scripts/batch_common.Runner`）が渡すこのステップの締切（ISO8601・UTC）。
# **予算の数字を argv へ書き写さない**——唯一の源は `Step.budget_min` で、親がそこから
# 導いた時刻だけを渡す。書き写すと予算を動かしたときに片方だけ残る。
# 名前は `scripts/batch_common.ENV_DEADLINE` と同じ文字列でなければならないが、root 側の
# モジュールから `scripts/` を import する向きは作らない（前例が無い）。片方だけ変えても
# **エラーは出ず、締切が黙って渡らなくなるだけ**なので、`tests/test_hyperparameter_search.py`
# が2つの定数を照合する（`launch.py` の DB target 既定を `tests/test_db_target.py` が
# 照合しているのと同じ形）。
ENV_DEADLINE = "FINAPP_STEP_DEADLINE_UTC"

# `search()` を抜けた後に残る仕事（`upsert_tuned_params` ＋ `--persist-scores` の最終 execute）の
# ための取り置き（分）。**実測**: `tune:macro_dlm` は最終候補から END 行まで3分未満
# （2026-09-10・全体303.8分）、`tune:macro_gbdt` は4.4分未満（2026-09-08・全体179.4分）。
# 約3倍の余裕を採った。日中枠の予算445分に対して3.4%で、しかもこの取り置きを実際に使うのは
# 畳んだ回だけ（完走した回は締切より手前で終わっている）。
FINAL_RESERVE_MIN = 15.0

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


def resolve_deadline(budget_min: float | None = None) -> datetime | None:
    """このプロセスに許された終了時刻（Issue #638）。無ければ None＝無期限。

    `--budget-min` の**明示指定が環境変数より優先**する（手動 CLI で確かめるときに、
    バッチから継承した締切に邪魔されないため）。読めない値は警告して締切なしに倒す——
    ここで落とすと、環境変数が1文字壊れただけで探索が起動しなくなる。
    """
    if budget_min is not None:
        return datetime.now(timezone.utc) + timedelta(minutes=budget_min)
    raw = os.environ.get(ENV_DEADLINE)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("%s を解釈できないので締切なしで走ります: %r", ENV_DEADLINE, raw)
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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
    deadline: datetime | None = None,
) -> dict:
    """1モデル分の探索を実行する共有ロジック（CLI・GitHub Actionsから呼ぶ・Issue #264）。

    persist=True で plugin_tuned_params へ永続化し、persist_scores=True なら
    best params での最終 execute も行い producer スコアを永続化する
    （persist=False のとき persist_scores は無視される）。

    persist=True のときは既存行の params を champion として探索へ投入する（#590）。
    persist=False（試し撃ち）では投入しない——本番値を巻き込まずに空間だけを見たい用途で、
    champion を混ぜると探索1件ぶんの時間を余分に使う。

    `deadline`（Issue #638）: このプロセスの締切。`search()` へは `FINAL_RESERVE_MIN` を
    引いた時刻を渡す＝**探索を畳んだあとに永続化と最終 execute を終える時間を残す**。
    加えて persist=True のときは候補ごとの暫定ベストを逐次永続化する（外から強制終了
    されてもパラメータだけは残る・#639 で 285分が消えた形への保険）。
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

    # 指紋は**探索の開始時点のデータ**を指す。候補ごとに測り直すと `count(*)` を何百回も
    # 打つことになるうえ、探索中にパネルは動かない（動く夜間バッチとは時間帯で分けてある）。
    fp_cache: dict = {}

    def _fingerprint() -> str:
        if "v" not in fp_cache:
            fp_cache["v"] = _data_fingerprint(db)
        return fp_cache["v"]

    def _write(res: dict) -> None:
        best_oof = res.get("best_oof") or {}
        cfg = res.get("config") or {}
        upsert_tuned_params(
            db, model, res["best_params"], objective,
            res["best_score"], res["leaderboard"][:20],
            cfg.get("n_combos"), _fingerprint(),
            prev_objective_value=prev["objective_value"] if prev else None,
            champion_objective_value=res.get("champion_score"),
            n_periods=best_oof.get("n_periods"),
            n_oof_samples=best_oof.get("n_oof_samples"),
            n_combos_planned=cfg.get("n_combos_planned"),
        )

    # 逐次永続化は**暫定ベストが改善したときだけ**書く（Issue #638）。毎回書いても1行の
    # upsert はミリ秒だが、書く理由が無い回まで書くとログと `tuned_at` が意味を失う。
    last_written: list = [None]

    def _on_progress(partial: dict) -> None:
        score = partial.get("best_score")
        if score is None:
            return
        if last_written[0] is not None and score <= last_written[0]:
            return
        _write(partial)
        last_written[0] = score
        logger.info("暫定ベストを保全しました（%d/%s 件時点・score=%.4f）",
                    (partial.get("config") or {}).get("n_combos") or 0,
                    (partial.get("config") or {}).get("n_combos_planned"), score)

    result = await search(
        plugin, base_params, dims, db,
        objective=objective, strategy=strategy, n_iter=n_iter, seed=seed,
        champion_params=prev["params"] if prev else None,
        deadline=None if deadline is None else deadline - timedelta(minutes=FINAL_RESERVE_MIN),
        on_progress=_on_progress if persist else None,
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

        _write(result)
        result["persisted"] = True

        if persist_scores:
            await execute_plugin(plugin, result["best_params"], db)

    return result


async def _run(args: argparse.Namespace) -> None:
    from database import SessionLocal

    db = SessionLocal()
    deadline = resolve_deadline(args.budget_min)
    if deadline is not None:
        logger.info("締切=%s（取り置き %.0f分を引いた時刻まで探索する・#638）",
                    deadline.isoformat(), FINAL_RESERVE_MIN)
    try:
        try:
            result = await run_search(
                args.model, args.strategy, args.n_iter, args.objective, args.seed, db,
                persist=args.persist, persist_scores=args.persist_scores,
                deadline=deadline,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        cfg = result["config"]
        if cfg.get("truncated"):
            # **「畳んだ」を成功のログに埋もれさせない**。exit は 0 のままだが、
            # 見た候補が計画の一部であることは次回の比較条件そのもの。
            logger.warning("予算の手前で畳みました: %s/%s 件を評価（#638）",
                           cfg.get("n_combos"), cfg.get("n_combos_planned"))
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
    ap.add_argument("--budget-min", type=float, default=None, dest="budget_min",
                    help=f"このプロセスに許す分数（Issue #638）。残り候補が入らないと見たら"
                         f"探索を畳み、そこまでの best を永続化する。省略時は環境変数 "
                         f"{ENV_DEADLINE}（親バッチが渡す）を見る。どちらも無ければ無期限")
    args = ap.parse_args()

    if args.persist_scores and not args.persist:
        ap.error("--persist-scores は --persist と併用してください")

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
