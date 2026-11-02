"""tuning_cycle.py — 探索の周回（#1114・ADR-0070 決定 4・5）の状態と段の移り変わり。純関数。

日中枠の空き（キューの仕事が終わった残り時間）で、**固定の候補集合**（M-2 は random 150 件・種 0＝月次の探索と
同じ集合）を数日に分けて回し切る。

- **勝ち抜き戦**: 1 日ぶんは「決勝候補（前日までの上位 `top` 件の params）＋次の束」を**その日のデータで**測る。
  評価値そのものは翌日へ持ち越さない——パネルは毎晩伸びるので、世代の違う値を並べると比較が成立しない
  （ADR-0047）。ADR-0054 の「日をまたいで続きから再開」は却下のままで、続くのは**どこまで測ったか**だけ
- 束を回し切ったら、決勝候補（＋本番の設定）を種を変えて確かめ直し（2 段の選び方・ADR-0070 決定 1）、
  **周回の終わりに 1 回だけ**本番（`plugin_tuned_params` と producer の μ̂）へ反映する。途中は記録だけ
- 次の周回は「前の周回が済んでいて、暦の月（JST）が変わった後の最初の取り出せる日」に始まる。OOF の期は月単位で
  増えるので、暦の月の切り替えが「データの世代が実質的に変わった」とほぼ重なる（判定に DB の重い読み込みが要らない）

状態は `app_settings` の `state_key(model)` に JSON で持つ。読み書きと探索の実行は
`hyperparameter_search.run_cycle`、日中枠への組み込みは `scripts/run_daytime.py`、見張りは `batch_freshness.py`。
**ここは database も plugins も import しない**——日中枠の起動判定と watchdog が軽く読めるように。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

# 暦は JST で数える（日中枠のトリガが JST の 8:00 なので・`run_daytime.JST` と同じ）。
JST = timezone(timedelta(hours=9))

STATE_KEY_PREFIX = "tuning_cycle:"
STATE_VERSION = 1

PHASE_SWEEP = "sweep"        # 固定の候補集合を束ごとに測っている
PHASE_CONFIRM = "confirm"    # 回し切った。決勝候補を種を変えて確かめ直し、本番へ反映する
PHASE_DONE = "done"          # 反映した（または試し撃ちで終えた）。次の周回を待つ

# 残りがこれ未満なら今日は測らない（分）。決勝候補 5 件を測り直すだけで約 15 分かかる（1 件約 3.1 分・
# 2026-11-01 の月次の実測 225 分 / 73 件）ので、その倍。新しい候補を 1 件も測れない日に決勝候補だけを測り直しても
# 進まない。
MIN_RUN_MIN = 30.0

# 始めてからこの日数たっても終わらない周回は捨てて始め直す。1 か月をまたいだ束どうしは、勝ち抜き戦で
# 同じ日に並ばないとはいえ、決勝候補が古いパネルで選ばれたまま残る。
STALE_DAYS = 31

# 見張り（`batch_freshness.PRODUCERS`）の約束: 周回は毎月始まり（`CADENCE_DAYS`）、始めてから `GRACE_DAYS` 日以内に
# 終わる。猶予は「月初の月次の重なり 3 日＋祝日・年末年始＋週末＋実走 2〜3 日」の上に余裕を取った値で、
# 実測から逆算したものではない（ADR-0042）。
CADENCE_DAYS = 31
GRACE_DAYS = 14

# 仕組みを入れた日。まだ 1 度も周回が終わっていない間は、見張りの時計をここから数える（行が無いことを
# 「止まった」と同じ顔にしない＝初回の周回が終わる前に起票しない）。
INTRODUCED_AT = datetime(2026, 11, 2, tzinfo=timezone.utc)

# 周回ごとの要約（戻す規則の材料・ADR-0070 決定 3）を何周回ぶん残すか。
HISTORY_KEEP = 24


def state_key(model: str) -> str:
    """`app_settings` のキー（`AppSetting.key` は 64 文字まで）。"""
    return f"{STATE_KEY_PREFIX}{model}"


def combo_key(combo: dict) -> tuple:
    """候補の同一性キー（`plugins.tuning._combo_key` はこれを使う＝規則を 1 か所に置く）。"""
    return tuple(sorted(combo.items()))


def fingerprint(combos: Sequence[dict]) -> str:
    """候補集合の指紋。探索空間（軸・値域）や件数・種を変えたら周回を始め直す。"""
    raw = json.dumps([sorted(c.items()) for c in combos], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load(raw: Optional[str]) -> Optional[dict]:
    """`app_settings` の値から状態を読む。無い・壊れている・版が違うときは None（＝始め直す）。"""
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return None
    return state


def dump(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, sort_keys=True)


def _month_after(d: date) -> str:
    return f"{d.year + 1}-01" if d.month == 12 else f"{d.year}-{d.month + 1:02d}"


def decide(state: Optional[dict], today: date, fp: Optional[str] = None) -> tuple[str, str]:
    """今日の動き方（`new` / `continue` / `idle`）と理由（ログ行）。

    `fp` を渡さないときは候補集合の照合を飛ばす——日中枠の起動判定（`scripts/run_daytime.py`）は探索空間を
    組み立てずに「今日周回のステップを足すか」だけを決めたい。指紋が変わったときのやり直しは子（探索の本体）が判定する。
    """
    if state is None:
        return "new", "周回の記録が無い（初回）"
    if fp is not None and state.get("fingerprint") != fp:
        return "new", "候補集合の定義（軸・値域・件数・種）が変わったので始め直す"
    started = date.fromisoformat(state["cycle_id"])
    if state.get("phase") == PHASE_DONE:
        if (today.year, today.month) != (started.year, started.month):
            return "new", f"暦の月が変わった（前の周回は {started:%Y-%m} に開始）"
        return "idle", (f"今月の周回 {state['cycle_id']} は済み（{state.get('finished_at')}）。"
                        f"次は {_month_after(started)} の最初の取り出せる日")
    if (today - started).days > STALE_DAYS:
        return "new", f"{STALE_DAYS} 日たっても終わらない周回（{started} に開始）を捨てて始め直す"
    return "continue", f"周回 {state['cycle_id']} の続き（段={state.get('phase')}）"


def pending(state: Optional[dict], today: date) -> bool:
    """今日、周回で測る仕事があるか（日中枠がステップを足すかの判定）。"""
    return decide(state, today)[0] != "idle"


def new_state(*, model: str, today: date, fp: str, n_total: int, prev: Optional[dict] = None) -> dict:
    """新しい周回の状態。前の周回の履歴と最後に反映した時刻は引き継ぐ。"""
    prev = prev or {}
    return {
        "version": STATE_VERSION,
        "model": model,
        "cycle_id": today.isoformat(),
        "fingerprint": fp,
        "n_total": n_total,
        "next_index": 0,
        "finalists": [],
        "phase": PHASE_SWEEP,
        "days": [],
        "worst_min": None,
        "finished_at": None,
        "result": None,
        "last_finished_at": prev.get("last_finished_at"),
        "history": list(prev.get("history") or [])[-HISTORY_KEEP:],
    }


def todays_candidates(state: dict, combos: Sequence[dict]) -> list[dict]:
    """今日測る並び: 決勝候補 → 次の束（残り全部。締切が来たら探索が自分で畳む）。同じ候補は 1 回だけ。"""
    out: list[dict] = []
    seen: set = set()
    for c in [*state["finalists"], *combos[state["next_index"]:]]:
        k = combo_key(c)
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(c))
    return out


def advance(state: dict, combos: Sequence[dict], evaluated: Sequence[dict], scored: Sequence[dict], *,
            today: date, top: int, minutes: float, truncated: bool) -> dict:
    """その日の束を測った後の状態。

    - `evaluated`: その日に評価した全候補の params（失敗した候補を含む・評価した順）
    - `scored`: その日の有効なスコアの行（`search()` の `leaderboard`）

    どこまで測ったかは、固定の候補集合を `next_index` から順に見て「評価済み」が続くところまで。束は締切で畳むので
    評価済みの束は必ず先頭からの連続になる（決勝候補と同じ候補は決勝候補として測られているので評価済みに数える）。
    失敗した候補も評価済みに数える——数えないと、契約違反で落ちる候補が 1 件あるだけで周回が永久にそこで止まる。

    新しい決勝候補は**その日の**スコアの上位 `top` 件で、評価値は持ち越さない（params だけ）。
    """
    measured = {combo_key(p) for p in evaluated}
    start = state["next_index"]
    nxt = start
    while nxt < len(combos) and combo_key(combos[nxt]) in measured:
        nxt += 1
    ranked = sorted((e for e in scored if e.get("score") is not None), key=lambda e: e["score"], reverse=True)
    finalists = [dict(e["params"]) for e in ranked[:top]] or list(state["finalists"])
    elapsed = [float(e.get("elapsed_sec") or 0.0) / 60.0 for e in ranked]
    worst = max([*elapsed, state.get("worst_min") or 0.0])
    day = {
        "date": today.isoformat(),
        "n_evaluated": len(evaluated),
        "from": start,
        "to": nxt,
        "best_score": ranked[0]["score"] if ranked else None,
        "truncated": bool(truncated),
        "minutes": round(minutes, 1),
    }
    return {
        **state,
        "next_index": nxt,
        "finalists": finalists,
        "worst_min": round(worst, 3) if worst > 0 else None,
        "days": [*state["days"], day],
        "phase": PHASE_CONFIRM if nxt >= len(combos) else PHASE_SWEEP,
    }


def confirm_fits(state: dict, remaining_min: Optional[float], n_seeds: int) -> bool:
    """確かめ直す段（決勝候補＋本番の設定を種 `n_seeds` 個で）が残り時間に入るか。

    見積りは観測した 1 件あたりの所要の最大値（`search()` が畳む判断と同じ考え方・ADR-0054）。締切が無い（手動の CLI）か
    見積りの材料が無ければ入るとみなす——段は締切に来たら自分で止まる（`plugins.tuning._confirm_by_seeds`）。
    """
    worst = state.get("worst_min")
    if remaining_min is None or worst is None:
        return True
    return worst * (len(state["finalists"]) + 1) * n_seeds <= remaining_min


def finish(state: dict, result: dict, *, now: datetime) -> dict:
    """確かめ直した後の状態。本番へ反映できた周回だけが `last_finished_at`（見張りが読む時刻）を進める。"""
    confirm = (result.get("config") or {}).get("confirm") or {}
    persisted = bool(result.get("persisted"))
    summary = {
        "cycle_id": state["cycle_id"],
        "finished_at": now.isoformat(),
        "n_days": len(state["days"]),
        "n_total": state["n_total"],
        "best_score": result.get("best_score"),
        "champion_score": result.get("champion_score"),
        "persisted": persisted,
        # 戻す規則（ADR-0070 決定 3）の材料: 2 段目が rank-IC の 1 位と違う候補を選んだか、選んだ − 1 位の上位K社超過の
        # 前半/後半の差。#1112 の「3 周回続いたら 2 段目をやめる」をここから数える
        "selection": confirm.get("selection"),
        "changed_by_stage2": confirm.get("changed_by_stage2"),
        "split_half": confirm.get("split_half"),
        "seeds_completed": confirm.get("seeds_completed"),
    }
    return {
        **state,
        "phase": PHASE_DONE,
        "finished_at": summary["finished_at"],
        "result": summary,
        "last_finished_at": summary["finished_at"] if persisted else state.get("last_finished_at"),
        "history": [*(state.get("history") or []), summary][-HISTORY_KEEP:],
    }


def last_finished_at(state: Optional[dict]) -> datetime:
    """見張りが読む時刻: 最後に周回を終えて本番へ反映した時刻。まだ 1 度も無ければ `INTRODUCED_AT`。"""
    raw = (state or {}).get("last_finished_at")
    if raw:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return INTRODUCED_AT


def describe(state: dict) -> str:
    """ログ・`--queue` に出す 1 行。"""
    return (f"周回 {state['cycle_id']}（段={state['phase']}・{state['next_index']}/{state['n_total']} 件・"
            f"決勝候補 {len(state['finalists'])} 件・{len(state['days'])} 日目まで済み）")
