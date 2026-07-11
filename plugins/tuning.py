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
) -> dict:
    """探索空間を評価し {best_params, best_score, objective, leaderboard, config} を返す。

    各候補は execute_plugin（内部で coerce_params による契約検証→ensure_dependencies→
    execute の順に実行・plugins/__init__.py の単一入口）をフル実行し、その oof_backtest
    から objective のスコアを抽出する。M-2/M-3 の producer 永続化
    （replace_macro_gbdt_scores/replace_macro_dlm_scores）は database.tuning_dry_run() で
    抑止する（候補ごとに本番テーブルを上書きしないため。最終選定後の本採用実行は
    このコンテキスト外で呼ぶこと）。1候補の失敗（契約違反の ValueError・実行時例外等）は
    その候補をスコアなしとして leaderboard に記録し、探索全体は継続する。

    呼び出し元は CLI（hyperparameter_search.py）・GitHub Actions のみ（Issue #293で
    GUIからの手動トリガーは廃止・#292の月次自動実行へ一本化）。

    探索ループ全体を macro_snapshots.tuning_snapshot_cache() で包む（Issue #298）。
    M-1/M-2 の execute() が呼ぶ load_data/preload_macro/build_snapshots は探索軸に
    依存しない重い処理（DB全件ロード・特徴量スナップショット構築）のため、構造パラメータ
    （fin_features/macro_features/use_momentum/min_coverage 等）が同一の候補間では
    結果を使い回す。このコンテキストは search() を抜けると解除され、通常の API 実行
    （/api/plugins/{name}/run）には影響しない。

    同時に database.tuning_objective_only() でも包む（Issue #299）。ここで読むのは
    oof_backtest のみのため、各プラグインの execute() は oof_backtest 算出後の
    全社スコアリング（M-1: _fit_final/_score_companies、M-2: raw_items構築+SHAP計算、
    M-3: 全社分のβ経路整形）を省略できる。best params での本採用実行
    （hyperparameter_search.py::run_search の persist_scores=True 時の execute_plugin 呼び出し）
    はこの with ブロックの外側で呼ばれるため、このコンテキストは無効＝フルスコアリングされる。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective は {OBJECTIVES} のいずれかを指定してください: {objective!r}")

    from database import tuning_dry_run, tuning_objective_only
    from plugins import execute_plugin
    from plugins.macro_snapshots import tuning_snapshot_cache

    rng = random.Random(seed)
    combos = _grid_combos(dims) if strategy == "grid" else _random_combos(dims, n_iter, rng)
    if not combos:
        raise ValueError("探索空間が空です（dims または only_if 条件を確認してください）")

    leaderboard: list[dict] = []
    with tuning_snapshot_cache(), tuning_objective_only():
        for i, combo in enumerate(combos):
            raw = {**base_params, **combo}
            try:
                with tuning_dry_run():
                    result = await execute_plugin(plugin, raw, db)
            except Exception as e:
                leaderboard.append({"params": combo, "score": None, "error": str(e)})
                log.info("[%d/%d] 失敗（契約違反 or 実行時例外）: %s params=%s",
                          i + 1, len(combos), e, combo)
                continue
            oof = result.get("oof_backtest") or {}
            score = _score(oof, objective)
            leaderboard.append({"params": combo, "score": score, "oof": oof})
            log.info("[%d/%d] score=%s params=%s", i + 1, len(combos), score, combo)

    scored = [e for e in leaderboard if e["score"] is not None]
    config = {
        "strategy": strategy, "n_iter": n_iter, "seed": seed,
        "n_combos": len(combos), "n_failed": len(leaderboard) - len(scored),
    }
    if not scored:
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
