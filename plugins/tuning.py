"""plugins/tuning.py — M-1/M-2/M-3 共有ハイパーパラメータ探索エンジン（Issue #264）。

探索空間（SearchDim のリスト）から候補パラメータをサンプリングし、各候補を
plugins.execute_plugin() でフル実行して walk-forward OOF（oof_backtest）から
目的関数スコアを抽出する。3モデルとも execute() が同じ形の oof_backtest を返す
（M-1 は #272 で対応済み）ため、モデル別の特殊処理は不要。

ローカル専用（hyperparameter_search.py CLI から使う想定・Render 非搭載・重い計算）。
"""
import logging
import random
import statistics
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

OBJECTIVES = ("rank_ic", "ic_ir", "long_short")

# 上位の候補を種を変えて確かめ直す段（`search(confirm_top=, confirm_seeds=)`・#984）に取り置く時間（分）。
# 探索の本体はこのぶん早く畳み、残りの時間で確かめ直す。**見積り**（2026-10-18）: 上位 5 × 追加の種 4 個＝20 回は
# 今朝の実測で 26 分、`subsample` 1.0（#991・1 回あたり約 1.25 倍）と月次の環境の遅さを乗せて 33〜50 分。本番の候補が
# 上位 5 に居なければ 4 回足す。足りなければ段は巡の途中で止まり、そこまでの種で比べる（失敗にはしない）。
# 月次の予算（`run_monthly.BUDGET_MIN["tune:macro_gbdt"]`）はこのぶん上げてある（240 → 300 分）。
CONFIRM_RESERVE_MIN = 60.0

# 確かめ直す段の間だけ有効な種の上書き（#984）。種で結果が動くプラグイン（`tuning_seed_base` を持つもの）は
# execute の中で `seeded(既定の種)` を使う。画面の項目にはしない＝本番の実行と最終モデルは常に既定の種。
_SEED_OVERRIDE: ContextVar[int | None] = ContextVar("tuning_seed_override", default=None)


@contextmanager
def seed_override(seed: int):
    """このブロックの中だけ、`seeded()` が返す種を `seed` にする（#984）。

    `plugins.execute_plugin` は `asyncio.to_thread` で execute を回すが、to_thread は呼び出し時の contextvars を
    写すので、ブロックの中で await した execute には届く（`database.tuning_dry_run` と同じ仕組み）。
    """
    token = _SEED_OVERRIDE.set(int(seed))
    try:
        yield
    finally:
        _SEED_OVERRIDE.reset(token)


def seeded(default: int) -> int:
    """種で結果が動くプラグインが execute の中で使う種。`seed_override` の外では `default`（#984）。"""
    v = _SEED_OVERRIDE.get()
    return default if v is None else v


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


def _assemble(
    base_params: dict,
    leaderboard: list[dict],
    champion_combo: dict | None,
    *,
    objective: str,
    strategy: str,
    n_iter: int,
    seed: int,
    n_planned: int,
    truncated: bool,
) -> dict | None:
    """ここまでの leaderboard から `search()` の戻り値の形を組み立てる（Issue #638）。

    有効なスコアが1件も無ければ None（＝まだ何も言えない）。**途中経過の通知と最終戻り値で
    同じ関数を使う**——別々に組むと、途中で書いた行と完走で書いた行の意味が静かにずれる。

    `config` の数え方は2本立てで、**`n_combos` は実際に評価した件数・`n_combos_planned` は
    計画した件数**。畳んだかどうかは `truncated` を見なくても `n_combos < n_combos_planned`
    から導ける（完走した回は両者が一致するので、既存行の `n_combos` の解釈は変わらない）。
    """
    scored = sorted((e for e in leaderboard if e["score"] is not None),
                    key=lambda e: e["score"], reverse=True)
    if not scored:
        return None
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
        "config": {
            "strategy": strategy, "n_iter": n_iter, "seed": seed,
            "n_combos": len(leaderboard),
            "n_combos_planned": n_planned,
            "n_failed": len(leaderboard) - len(scored),
            "truncated": truncated,
        },
        "champion_injected": champion_combo is not None,
        "champion_score": champion_score,
    }


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
    deadline: datetime | None = None,
    on_progress: Callable[[dict], None] | None = None,
    confirm_top: int = 0,
    confirm_seeds: int = 0,
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

    `deadline`（Issue #638）: **この時刻までに抜ける**。残り候補が入らないと見たら探索を畳み、
    `config["truncated"]=True` を立てて返る。None なら従来どおり全候補を回す。呼び出し元は
    永続化と最終 execute のぶんを**引いた**時刻を渡すこと（`hyperparameter_search.FINAL_RESERVE_MIN`）。
    見積りは**観測した所要の最大値**を使う——候補ごとの所要にはばらつきがあり、平均で見ると
    最後の1件で超える。**1件目だけは締切を見ずに必ず評価する**（見ると、締切を既に過ぎて
    入ってきた回で有効なスコアが0件になり `ValueError` へ落ちる。1件目は champion なので
    最低限「現状維持」の行は書ける）。

    `on_progress`（Issue #638）: 候補を1件評価するたびに、その時点の戻り値と同じ形の辞書を
    渡す。呼び出し元はこれを使って暫定ベストを逐次永続化できる（外から強制終了されても
    パラメータだけは残る）。有効なスコアが1件も無い間は呼ばれない。

    `confirm_top` / `confirm_seeds`（#984）: 探索の後で、既定の種の score の上位 `confirm_top` 候補（＋本番の
    候補が居なければ足す）を種 `confirm_seeds` 個（既定の種を含む）で回し直し、**種の平均**で選び直す
    （`_confirm_by_seeds`）。M-2 は種だけで OOF rank-IC が 0.009〜0.019 動き、1 回の種で選ぶと上位どうしの差
    （2026-10-01 は 1 位と 2 位で 0.0003）が種の揺れに埋もれる。種で結果が動くと宣言したプラグイン
    （`tuning_seed_base`）だけが使える。有効にすると探索の本体は `deadline` より `CONFIRM_RESERVE_MIN` 早く畳む。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective は {OBJECTIVES} のいずれかを指定してください: {objective!r}")
    confirm = bool(confirm_top or confirm_seeds)
    if confirm:
        if confirm_top < 1 or confirm_seeds < 2:
            raise ValueError("確かめ直す段は confirm_top >= 1 と confirm_seeds >= 2 を両方指定する"
                             f"（受け取った値: top={confirm_top} seeds={confirm_seeds}）")
        if not isinstance(getattr(plugin, "tuning_seed_base", None), int):
            raise ValueError(f"プラグイン '{getattr(plugin, 'name', plugin)}' は種で結果が動くと宣言していない"
                             "（tuning_seed_base が無い）ので、種を変えて確かめ直す意味が無い（#984）")
    # 探索の本体の締切。確かめ直す段のぶんを取り置く（段そのものは `deadline` まで使える）
    loop_deadline = (deadline - timedelta(minutes=CONFIRM_RESERVE_MIN)
                     if (confirm and deadline is not None) else deadline)

    from database import tuning_dry_run, tuning_objective_only
    from plugins import execute_plugin
    from plugins.macro_snapshots import shared_snapshot_cache

    rng = random.Random(seed)
    combos = _grid_combos(dims) if strategy == "grid" else _random_combos(dims, n_iter, rng)
    if not combos:
        raise ValueError("探索空間が空です（dims または only_if 条件を確認してください）")

    # champion を候補プールの**先頭へ置く**（既に含まれていれば移動・Issue #638）。プールに
    # 居ることで best >= champion が構造的に成立し、劣化した値で本番を上書きすることが
    # 「比較」ではなく「探索の性質」として防がれる——**先頭でなければ途中で止めたときに
    # その性質が成立しない**。grid では champion が候補列の中間に埋まるので、そこで畳むと
    # 「まだ champion を測っていない」区間ができ、本番より悪い params を書きうる。
    champion_combo = _project_champion(plugin, champion_params, dims) if champion_params else None
    if champion_combo is not None:
        ckey = _combo_key(champion_combo)
        combos = [champion_combo, *(c for c in combos if _combo_key(c) != ckey)]
        log.info("champion を候補の先頭へ置きました: %s", champion_combo)

    n_planned = len(combos)
    leaderboard: list[dict] = []
    worst_sec = 0.0          # 観測した1候補あたり所要の最大値（次の1件の見積り）
    truncated = False

    def _snapshot() -> dict | None:
        return _assemble(base_params, leaderboard, champion_combo,
                         objective=objective, strategy=strategy, n_iter=n_iter, seed=seed,
                         n_planned=n_planned, truncated=truncated)

    with shared_snapshot_cache(), tuning_objective_only():
        for i, combo in enumerate(combos):
            if loop_deadline is not None and i > 0:
                est = timedelta(seconds=worst_sec)
                if datetime.now(timezone.utc) + est > loop_deadline:
                    truncated = True
                    log.warning(
                        "予算の手前で探索を畳みます（#638）: %d/%d 件まで評価済み・"
                        "締切=%s・次の1件の見積り=%.1f分（観測した最大値）",
                        i, n_planned, loop_deadline.isoformat(), worst_sec / 60.0,
                    )
                    break
            t0 = datetime.now(timezone.utc)
            raw = {**base_params, **combo}
            try:
                with tuning_dry_run():
                    result = await execute_plugin(plugin, raw, db)
            except Exception as e:
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                leaderboard.append({"params": combo, "score": None, "error": str(e),
                                    "elapsed_sec": round(elapsed, 1)})
                log.info("[%d/%d] 失敗（契約違反 or 実行時例外）: %s params=%s",
                          i + 1, n_planned, e, combo)
            else:
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                oof = result.get("oof_backtest") or {}
                score = _score(oof, objective)
                # 所要は確かめ直す段（#984）が候補ごとの見積りに使う
                leaderboard.append({"params": combo, "score": score, "oof": oof,
                                    "elapsed_sec": round(elapsed, 1)})
                log.info("[%d/%d] score=%s params=%s", i + 1, n_planned, score, combo)
            # **失敗した候補の所要も数える**。契約違反で即返る候補ばかり見ていると見積りが
            # 0 に張り付き、重い候補が1件でも残っていれば締切を踏み越える。
            worst_sec = max(worst_sec, (datetime.now(timezone.utc) - t0).total_seconds())
            if on_progress is not None:
                partial = _snapshot()
                if partial is not None:
                    on_progress(partial)

        final = _snapshot()
        if final is None:
            raise ValueError("有効なスコアが1件も得られませんでした（全候補が失敗/契約違反/スコア算出不能）")
        if confirm:
            # 探索と同じキャッシュ・objective_only の内側で回す（断面を作り直さない）
            await _confirm_by_seeds(
                final, leaderboard, champion_combo, plugin=plugin, base_params=base_params, db=db,
                objective=objective, top=confirm_top, n_seeds=confirm_seeds, deadline=deadline,
                execute_plugin=execute_plugin, tuning_dry_run=tuning_dry_run,
            )
    return final


async def _confirm_by_seeds(
    final: dict,
    leaderboard: list[dict],
    champion_combo: dict | None,
    *,
    plugin: Any,
    base_params: dict,
    db: Any,
    objective: str,
    top: int,
    n_seeds: int,
    deadline: datetime | None,
    execute_plugin: Callable,
    tuning_dry_run: Callable,
) -> None:
    """上位の候補を種を変えて回し直し、種の平均で選び直す（#984）。`final` をその場で書き換える。

    - 対象: 既定の種の score の上位 `top` 候補（同じ params は 1 つ）＋本番の候補（champion）が居なければ足す。
      champion を必ず入れるのは、ADR-0047 の「本番より悪い候補は選ばない」（探索のプールに champion を置くと
      best >= champion が成り立つ）を、種の平均で選び直した後も保つため
    - 回し方: **種の順**（全候補を種 base+1、次に全候補を種 base+2 …）。各巡の前に、その巡が `deadline` に入るかを
      候補ごとの所要（探索での所要と、それまでの巡の最大）の合計で見積もり、入らなければ止める——どの候補も同じ数の
      種で比べるため。1 巡も回れなければ既定の種だけで比べる＝探索の選択と同じになる
    - 選び方: 回った種の平均が最も高い候補。ある種で失敗した候補（score が None）は比べる対象から外す
    - 書き換えるもの: `best_params` / `best_score`（**種の平均**）/ `best_oof`（既定の種の OOF）/ `champion_score`
      （champion の種の平均）。候補の leaderboard の行に `seed_scores`（{種: score}）と `seed_mean` を足し、
      `config["confirm"]` に段の要約を置く
    """
    base_seed = int(plugin.tuning_seed_base)
    scored = sorted((e for e in leaderboard if e["score"] is not None),
                    key=lambda e: e["score"], reverse=True)
    picked: list[dict] = []
    seen: set = set()
    for e in scored:
        k = _combo_key(e["params"])
        if k in seen:
            continue
        picked.append(e)
        seen.add(k)
        if len(picked) == top:
            break
    champion_added = False
    if champion_combo is not None:
        ck = _combo_key(champion_combo)
        if ck not in seen:
            ce = next((e for e in scored if _combo_key(e["params"]) == ck), None)
            if ce is not None:
                picked.append(ce)
                seen.add(ck)
                champion_added = True

    for e in picked:
        e["seed_scores"] = {str(base_seed): e["score"]}
    est = [max(float(e.get("elapsed_sec") or 0.0), 1.0) for e in picked]
    rounds = 0
    truncated = False
    for k in range(1, n_seeds):
        s = base_seed + k
        need = sum(est)
        if deadline is not None and datetime.now(timezone.utc) + timedelta(seconds=need) > deadline:
            truncated = True
            log.warning("確かめ直す段を種 %d の手前で止めます（#984）: %d/%d 個の種まで・締切=%s・"
                        "次の巡の見積り=%.1f分", s, 1 + rounds, n_seeds, deadline.isoformat(), need / 60.0)
            break
        for j, e in enumerate(picked):
            t0 = datetime.now(timezone.utc)
            try:
                with seed_override(s), tuning_dry_run():
                    res = await execute_plugin(plugin, {**base_params, **e["params"]}, db)
                sc = _score(res.get("oof_backtest") or {}, objective)
            except Exception as ex:          # 1 回の失敗で段全体は止めない（その候補だけ比べる対象から外す）
                sc = None
                log.info("[confirm seed=%d] 失敗: %s params=%s", s, ex, e["params"])
            est[j] = max(est[j], (datetime.now(timezone.utc) - t0).total_seconds())
            e["seed_scores"][str(s)] = sc
            log.info("[confirm seed=%d %d/%d] score=%s params=%s", s, j + 1, len(picked), sc, e["params"])
        rounds += 1

    for e in picked:
        vals = list(e["seed_scores"].values())
        e["seed_mean"] = (statistics.mean(vals) if vals and all(v is not None for v in vals) else None)
    eligible = [e for e in picked if e["seed_mean"] is not None]
    chosen = max(eligible, key=lambda e: e["seed_mean"]) if eligible else picked[0]
    seed_base_best = scored[0]

    final["best_params"] = {**base_params, **chosen["params"]}
    final["best_score"] = chosen["seed_mean"] if chosen.get("seed_mean") is not None else chosen["score"]
    final["best_oof"] = chosen.get("oof") or {}
    if champion_combo is not None:
        ck = _combo_key(champion_combo)
        ce = next((e for e in picked if _combo_key(e["params"]) == ck), None)
        if ce is not None and ce.get("seed_mean") is not None:
            final["champion_score"] = ce["seed_mean"]
    final["config"]["confirm"] = {
        "top": top,
        "seeds_planned": n_seeds,
        "seeds_completed": 1 + rounds,
        "seeds": [base_seed + k for k in range(1 + rounds)],
        "truncated": truncated,
        "n_candidates": len(picked),
        "champion_added": champion_added,
        "changed": _combo_key(chosen["params"]) != _combo_key(seed_base_best["params"]),
        "seed_base_best_params": seed_base_best["params"],
        "seed_base_best_score": seed_base_best["score"],
        "selected_seed_base_score": chosen["score"],
    }
    log.info("確かめ直す段（#984）: %d 候補 × 種 %d 個で選び直しました。種の平均で 1 位: %s（平均 %s・"
             "種 %d では %s）。種 %d だけで選んだときの 1 位: %s（%s）。入れ替わり=%s",
             len(picked), 1 + rounds, chosen["params"], chosen.get("seed_mean"), base_seed, chosen["score"],
             base_seed, seed_base_best["params"], seed_base_best["score"],
             final["config"]["confirm"]["changed"])
    for e in picked:
        log.info("  [confirm] mean=%s seeds=%s params=%s", e.get("seed_mean"), e["seed_scores"], e["params"])
