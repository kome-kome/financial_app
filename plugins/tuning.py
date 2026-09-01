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


def _combo_key(combo: dict) -> tuple:
    """combo の同一性キー（`_random_combos` の重複排除と champion 投入で共有する）。"""
    return tuple(sorted(combo.items()))


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
        key = _combo_key(combo)
        if key in seen:
            continue
        seen.add(key)
        out.append(combo)
    return out


def _project_champion(plugin: Any, champion_params: dict, dims: list) -> dict | None:
    """本番稼働中の params を、今回の `dims` が張る combo の形へ投影する（Issue #590）。

    combo は軸名だけを持つ辞書（`search()` が `{**base_params, **combo}` で組み立てる）なので、
    champion の全 params から軸名分を抜き出す。

    **まず `coerce_params` を通す**——保存された params には「その時点の探索空間」しか入って
    おらず、後から足された軸のキーは無い（実測: `macro_gbdt` の 2026-07-19 の行には
    `use_monotone_constraints` / `use_sector_features` が無い）。本番の
    `GET /api/plugins/{name}/tuned` → `execute_plugin` も同じ経路で default を補うので、
    **補完後の姿が「いま本番で動いている設定」**である。ここを素通りさせると、軸を1本足した
    だけで champion 再測定が黙って止まる（#590 が直したのと同じ「失敗として現れない」形）。

    **値域外なら None を返す**（`dims` を意図的に狭めた後＝ADR-0045 のモメンタム既定・#583）。
    投入すると退役させたはずの設定が毎月「前回勝ったから」で復活し続ける。**軸の追加は
    default で補い、値域の縮小では投入しない**——前者は本番の姿の再現、後者は退役の尊重。
    """
    from plugins.utils import coerce_params

    try:
        champion_params = coerce_params(plugin.params_schema(), champion_params)
    except ValueError as e:
        log.warning("champion が現在のパラメータ契約を満たさないため再測定しません: %s", e)
        return None

    combo: dict = {}
    for d in dims:
        # 条件を満たさない軸は values[0] へ縮退させる（`_grid_combos` と同じ規則）。揃えないと
        # 無効な軸の値違いだけで combos と一致せず、同じ結果を出す候補を1件余分に評価する。
        if d.only_if is not None and not d.only_if(combo):
            combo[d.name] = d.values[0]
            continue
        if d.name not in champion_params:
            # schema にすら無い軸＝探索空間と契約が食い違っている（default で補えない）。
            log.warning("champion に軸 '%s' が無く default も引けないため再測定しません", d.name)
            return None
        v = champion_params[d.name]
        if v not in d.values:
            log.warning("champion の %s=%r が現在の値域 %r の外なので再測定しません", d.name, v, d.values)
            return None
        combo[d.name] = v
    return combo


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
    champion_params: dict | None = None,
) -> dict:
    """探索空間を評価し {best_params, best_score, objective, leaderboard, config, ...} を返す。

    各候補は execute_plugin（内部で coerce_params による契約検証→ensure_dependencies→
    execute の順に実行・plugins/__init__.py の単一入口）をフル実行し、その oof_backtest
    から objective のスコアを抽出する。M-2/M-3 の producer 永続化
    （replace_macro_gbdt_scores/replace_macro_dlm_scores）は database.tuning_dry_run() で
    抑止する（候補ごとに本番テーブルを上書きしないため。最終選定後の本採用実行は
    このコンテキスト外で呼ぶこと）。1候補の失敗（契約違反の ValueError・実行時例外等）は
    その候補をスコアなしとして leaderboard に記録し、探索全体は継続する。

    呼び出し元は CLI（hyperparameter_search.py）・GitHub Actions のみ（Issue #293で
    GUIからの手動トリガーは廃止・#292の月次自動実行へ一本化）。

    探索ループ全体を macro_snapshots.shared_snapshot_cache() で包む（Issue #298）。
    M-1/M-2 の execute() が呼ぶ load_data/preload_macro/build_snapshots は探索軸に
    依存しない重い処理（DB全件ロード・特徴量スナップショット構築）のため、構造パラメータ
    （fin_features/macro_features/use_momentum/min_coverage 等）が同一の候補間では
    結果を使い回す。このコンテキストは search() を抜けると解除され、通常の API 実行
    （/api/plugins/{name}/run）には影響しない。

    同じコンテキストで、M-3（macro_dlm）の load_prices/load_macro_levels（DB全件ロード）と
    M-1（macro_risk_return）の BIC選択結果（selected_names）に紐づく Walk-Forward CV 結果も
    キャッシュされる（Issue #304）。両者とも `macro_snapshots.shared_cache_get_or_compute()`
    経由で既存の `shared_snapshot_cache()` の名前空間を再利用するため、search() 側の
    変更はこの docstring 更新のみで完結する（with 文自体は #298 のまま）。

    同時に database.tuning_objective_only() でも包む（Issue #299）。ここで読むのは
    oof_backtest のみのため、各プラグインの execute() は oof_backtest 算出後の
    全社スコアリング（M-1: _fit_final/_score_companies、M-2: raw_items構築+SHAP計算、
    M-3: 全社分のβ経路整形）を省略できる。best params での本採用実行
    （hyperparameter_search.py::run_search の persist_scores=True 時の execute_plugin 呼び出し）
    はこの with ブロックの外側で呼ばれるため、このコンテキストは無効＝フルスコアリングされる。

    `champion_params`（Issue #590）: 本番稼働中の params。渡すと候補プールの先頭へ投入し、
    **今回のパネル上で測り直した値**を `champion_score` として返す。これが要るのは、
    永続化済みの `objective_value` が「そのとき存在したパネルでの値」であり、パネルは毎晩
    伸びるので月をまたいだ単純比較が成立しないため（実測: `macro_gbdt` の 0.5068 は
    10 fold・`macro_dlm` の 0.0221 は 55 fold で、fold が少ない候補ほど高く出ていた＝
    ADR-0045 の「母集団が縮む側は必ず有利に見える」と同型）。投入できない場合
    （軸が無い・値域外）は `_project_champion` が None を返し、`champion_score` も None になる。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective は {OBJECTIVES} のいずれかを指定してください: {objective!r}")

    from database import tuning_dry_run, tuning_objective_only
    from plugins import execute_plugin
    from plugins.macro_snapshots import shared_snapshot_cache

    rng = random.Random(seed)
    combos = _grid_combos(dims) if strategy == "grid" else _random_combos(dims, n_iter, rng)
    if not combos:
        raise ValueError("探索空間が空です（dims または only_if 条件を確認してください）")

    # champion を候補プールへ入れる。既に含まれていれば追加しない（grid ではほぼ常にこちら＝
    # 追加コストゼロ）。プールに居ることで best >= champion が構造的に成立し、劣化した値で
    # 本番を上書きすることが「比較」ではなく「探索の性質」として防がれる。
    champion_combo = _project_champion(plugin, champion_params, dims) if champion_params else None
    if champion_combo is not None and _combo_key(champion_combo) not in {_combo_key(c) for c in combos}:
        combos = [champion_combo, *combos]
        log.info("champion を候補へ投入しました: %s", champion_combo)

    leaderboard: list[dict] = []
    with shared_snapshot_cache(), tuning_objective_only():
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

    champion_score = None
    if champion_combo is not None:
        key = _combo_key(champion_combo)
        # 失敗候補は scored から落ちるので leaderboard 全体から探す（None のまま返る＝
        # 「投入したが測れなかった」と「投入しなかった」を score では区別しない。区別が要る
        # ときは champion_injected を見る）。
        champion_score = next(
            (e["score"] for e in leaderboard if _combo_key(e["params"]) == key), None
        )

    return {
        "best_params": {**base_params, **best["params"]},
        "best_score":  best["score"],
        "best_oof":    best.get("oof") or {},
        "objective":   objective,
        "leaderboard": scored,
        "config": config,
        "champion_injected": champion_combo is not None,
        "champion_score": champion_score,
    }
