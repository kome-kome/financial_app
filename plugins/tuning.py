"""plugins/tuning.py — M-1/M-2/M-3 共有ハイパーパラメータ探索エンジン（Issue #264）。

探索空間（SearchDim のリスト）から候補パラメータをサンプリングし、各候補を
plugins.execute_plugin() でフル実行して walk-forward OOF（oof_backtest）から
目的関数スコアを抽出する。3モデルとも execute() が同じ形の oof_backtest を返す
（M-1 は #272 で対応済み）ため、モデル別の特殊処理は不要。

ローカル専用（hyperparameter_search.py CLI から使う想定・Render 非搭載・重い計算）。
"""
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

OBJECTIVES = ("rank_ic", "ic_ir", "long_short")


@dataclass
class SearchDim:
    """探索軸1本。values は離散候補値のリスト（grid/random 共通の表現）。

    only_if: combo（{name: value, ...}）を受け取り、この軸が有効な条件を返す
    （例: M-3 の alpha_phi は alpha_ar1=True のときのみ意味を持つ）。None なら常に有効。
    """
    name: str
    values: list
    only_if: Callable[[dict], bool] | None = None


def _grid_combos(dims: list) -> list[dict]:
    """全組合せグリッドを構築する。

    only_if を持つ軸は、条件を満たさない部分 combo では values[0]（先頭値）に固定し、
    その軸のバリエーションを展開しない（除外ではなく縮退＝無効な組合せで探索予算を
    無駄にしない）。only_if は自身より前の dims の値だけを参照できる（dims の並び順に
    依存＝条件を決める軸を、条件付き軸より先に置くこと）。
    """
    combos: list[dict] = [{}]
    for d in dims:
        next_combos: list[dict] = []
        for c in combos:
            if d.only_if is not None and not d.only_if(c):
                next_combos.append({**c, d.name: d.values[0]})
            else:
                for v in d.values:
                    next_combos.append({**c, d.name: v})
        combos = next_combos
    return combos


def _random_combos(dims: list, n_iter: int, rng: random.Random) -> list[dict]:
    """重複なしランダムサンプリング。only_if の扱いは `_grid_combos` と同じ（縮退）。"""
    seen: set = set()
    out: list[dict] = []
    max_attempts = max(n_iter * 20, 200)
    attempts = 0
    while len(out) < n_iter and attempts < max_attempts:
        attempts += 1
        combo: dict = {}
        for d in dims:
            if d.only_if is not None and not d.only_if(combo):
                combo[d.name] = d.values[0]
            else:
                combo[d.name] = rng.choice(d.values)
        key = tuple(sorted(combo.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(combo)
    return out


def _score(oof: dict, objective: str) -> float | None:
    """oof_backtest 辞書から目的関数スコアを抽出する。算出不能なら None（探索から除外）。"""
    if objective == "rank_ic":
        return (oof.get("rank_ic") or {}).get("mean")
    if objective == "ic_ir":
        ric = oof.get("rank_ic") or {}
        mean, std = ric.get("mean"), ric.get("std")
        if mean is None or not std:
            return None
        return mean / std
    if objective == "long_short":
        return oof.get("long_short_spread")
    raise ValueError(f"未知の objective: {objective!r}（{OBJECTIVES} のいずれかを指定してください）")


async def search(
    plugin: Any,
    base_params: dict,
    dims: list,
    db: Any,
    objective: str = "rank_ic",
    strategy: str = "random",
    n_iter: int = 50,
    seed: int = 0,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """探索空間を評価し {best_params, best_score, objective, leaderboard, config} を返す。

    各候補は execute_plugin（内部で coerce_params による契約検証→ensure_dependencies→
    execute の順に実行・plugins/__init__.py の単一入口）をフル実行し、その oof_backtest
    から objective のスコアを抽出する。M-2/M-3 の producer 永続化
    （replace_macro_gbdt_scores/replace_macro_dlm_scores）は database.tuning_dry_run() で
    抑止する（候補ごとに本番テーブルを上書きしないため。最終選定後の本採用実行は
    このコンテキスト外で呼ぶこと）。1候補の失敗（契約違反の ValueError・実行時例外等）は
    その候補をスコアなしとして leaderboard に記録し、探索全体は継続する。

    on_progress/cancel_check は GUI からのバックグラウンドジョブ化（Issue #278）向けの
    フック。collector_prices.py の各収集関数と同じ形（progress(current, total, msg) /
    cancel() -> bool）で、どちらも省略時（None）は CLI 実行時と完全に同じ挙動になる。
    cancel_check が True を返した時点で残り候補の評価を打ち切る（ユーザーの意図的な
    停止はエラーではないため、score 済み候補が無くても ValueError にはしない）。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective は {OBJECTIVES} のいずれかを指定してください: {objective!r}")

    from database import tuning_dry_run
    from plugins import execute_plugin

    rng = random.Random(seed)
    combos = _grid_combos(dims) if strategy == "grid" else _random_combos(dims, n_iter, rng)
    if not combos:
        raise ValueError("探索空間が空です（dims または only_if 条件を確認してください）")

    leaderboard: list[dict] = []
    cancelled = False
    for i, combo in enumerate(combos):
        if cancel_check is not None and cancel_check():
            cancelled = True
            msg = f"[停止] ユーザーによる停止（{len(leaderboard)}/{len(combos)}候補評価済み）"
            log.info(msg)
            if on_progress is not None:
                on_progress(len(leaderboard), len(combos), msg)
            break
        raw = {**base_params, **combo}
        try:
            with tuning_dry_run():
                result = await execute_plugin(plugin, raw, db)
        except Exception as e:
            leaderboard.append({"params": combo, "score": None, "error": str(e)})
            msg = f"[{i + 1}/{len(combos)}] 失敗（契約違反 or 実行時例外）: {e} params={combo}"
            log.info(msg)
            if on_progress is not None:
                on_progress(i + 1, len(combos), msg)
            continue
        oof = result.get("oof_backtest") or {}
        score = _score(oof, objective)
        leaderboard.append({"params": combo, "score": score, "oof": oof})
        msg = f"[{i + 1}/{len(combos)}] score={score} params={combo}"
        log.info(msg)
        if on_progress is not None:
            on_progress(i + 1, len(combos), msg)

    scored = [e for e in leaderboard if e["score"] is not None]
    config = {
        "strategy": strategy, "n_iter": n_iter, "seed": seed,
        "n_combos": len(combos), "n_failed": len(leaderboard) - len(scored),
        "cancelled": cancelled,
    }
    if not scored:
        if cancelled:
            return {
                "best_params": None, "best_score": None, "objective": objective,
                "leaderboard": [], "config": config,
            }
        raise ValueError("有効なスコアが1件も得られませんでした（全候補が失敗/契約違反/スコア算出不能）")
    scored.sort(key=lambda e: e["score"], reverse=True)
    best = scored[0]

    return {
        "best_params": {**base_params, **best["params"]},
        "best_score":  best["score"],
        "objective":   objective,
        "leaderboard": scored,
        "config": config,
    }
