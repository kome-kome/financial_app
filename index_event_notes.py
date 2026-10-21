"""指数イベントの画面注記 — 推奨・売り・企業詳細に TOPIX 移行措置・新規採用の事実を出す（#1013・台帳 #1003）。

移行措置（段階的ウエイト低減）と新規採用の社は、指数連動資金の売買で業績と無関係に株価が動きうる。
株価由来の指標（PER・PBR・割安度・モメンタム・μ̂）を読む人が、その銘柄が対象かを画面で分かるようにする。

## 何を出すか

- 行の印（`DISPLAY_KINDS`）と注記（画面では title）。移行措置・新規採用の社で、状態が公表済み・適用中・
  後続の見直し待ち（`DISPLAY_STATUSES`）のものだけ。表示期限は最後の係数変更 + 52 週で、計算で外す
- 表の上の 1 行（表示した社のうちの件数と、母集団での比率）と、台帳が読めないときのバナー
- 企業詳細の日程（日付が一次資料で確定した回だけ日付を出し、他は「公式資料で確認中」）

## 文言の規則（#1013）

- 出典と公表日を付ける。条件付きの未来は条件付きで書く（「除外予定」は使わない）。未確認は「公式資料で確認中」
- 影響は `IMPACT_NOTE` に固定する。売買の方向を示す語は使わない（`FORBIDDEN_WORDS`・テストが照合する）
- **文言はここで作り、画面（JS）は表示だけにする**＝禁止語の検査を 1 か所でかけられる

## 台帳

本番（Render）は一覧（`.index_lists/`）を持たない（ADR-0068 決定 2）ので `ledger_ok=False` とバナーになる——
注記が無いことは対象外を意味しない。台帳はプロセス内で `LEDGER_TTL_S` 秒持つ（財務 API のたびに一覧と
企業表を読まない）。

**順位・スコア・判定は変えない**（注記は並べ終えた後に付けるだけ）。database・scripts を import しない
（台帳の入口 `index_events.load_ledger` が自分で読む）。
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Optional

import index_events as IE

log = logging.getLogger("collector")

LEDGER_TTL_S = 600
DISPLAY_KINDS = {"transition": "TOPIX移行", "new_add": "TOPIX新規"}
KIND_NAMES = {"transition": "移行措置", "new_add": "新規採用"}
DISPLAY_STATUSES = ("announced", "in_effect", "pending")
DISPLAY_TAIL_DAYS = 364                  # 最後の係数変更 + 52 週まで出す
UNCONFIRMED_NOTE = "公式資料で確認中"
IMPACT_NOTE = ("指数連動資金の売買による価格変化を含む可能性があります。株価由来の指標"
               "（PER・PBR・割安度・モメンタム・μ̂）の解釈に注意してください（本ツールでは未計測）。")
BANNER_NO_LEDGER = ("TOPIX の移行措置・新規採用の台帳を読めないため、銘柄ごとの注記を出していません。"
                    "注記が無いことは対象外であることを意味しません。")
# 注記の文言に使わない語（売買の方向を示す語と、条件付きの未来を言い切る語）。画面の他の語には当てない。
FORBIDDEN_WORDS = ("除外予定", "売り", "買い", "売られ", "買われ", "下落", "上昇", "値下がり", "値上がり")

_CACHE: dict = {}


# ── 台帳 ───────────────────────────────────────────────────────────────────────

def cached_ledger(db, *, clock=time.monotonic) -> IE.IndexLedger:
    """台帳を `LEDGER_TTL_S` 秒持つ。壊れていれば `ok=False` と理由（画面は止めずにバナーを出す）。"""
    hit = _CACHE.get("ledger")
    if hit is not None and clock() - hit[0] < LEDGER_TTL_S:
        return hit[1]
    try:
        ledger = IE.load_ledger(db)
    except ValueError as e:              # IndexLedgerError（sha256・件数・突合率の不一致）
        log.warning("指数イベント台帳が使えない: %s", e)
        ledger = IE.IndexLedger(ok=False, reason=str(e))
    _CACHE["ledger"] = (clock(), ledger)
    return ledger


def clear_cache() -> None:
    _CACHE.clear()


def jst_today() -> date:
    from collector_utils import JST

    return datetime.now(JST).date()


# ── 文言 ───────────────────────────────────────────────────────────────────────

def source_text(review: IE.Review) -> str:
    """出典と公表日（登録表の `basis` の題名の部分）。"""
    return f"出典: JPX「{review.basis.split('（')[0]}」{review.announced.isoformat()} 公表"


def _schedule(st: IE.IndexStatus) -> IE.Schedule:
    return IE.SCHEDULES[(st.review, st.kind)]


def _review_month(ledger: IE.IndexLedger, key: Optional[str]) -> str:
    """見直しの公表の年月（登録済みなら公表日、未登録なら公表見込み）。内部のキーは画面に出さない。"""
    if key in ledger.reviews:
        return ledger.reviews[key].announced.strftime("%Y-%m")
    if key in IE.EXPECTED_REVIEWS:
        return IE.EXPECTED_REVIEWS[key].announce_expected.strftime("%Y-%m")
    return "次回"


def note_text(ledger: IE.IndexLedger, st: IE.IndexStatus, known_at: date) -> str:
    sched = _schedule(st)
    origin = ledger.reviews[sched.origin]
    parts = []
    if st.kind == "transition":
        parts.append(f"TOPIX 定期入替で移行措置の対象（{source_text(origin)}）。{st.effective.isoformat()} から"
                     f"指数のウエイトを四半期ごとに段階的に下げる日程（{len(sched.steps)} 回）。")
    else:
        coef = float(sched.steps[0].coefficient) if sched.steps else None
        parts.append(f"TOPIX 定期入替で新規採用（{source_text(origin)}）。{st.effective.isoformat()} に調整係数 "
                     f"{coef:g} で組み入れ、後の定期入替で継続基準を満たせば 1.0 になる場合があります。")
    resolver = IE.PENDING_REVIEWS.get((st.review, st.kind))
    when = _review_month(ledger, resolver)
    if st.status == "pending":
        parts.append(f"以降の日程は {when} の見直しの結果待ちです（{UNCONFIRMED_NOTE}）。")
    elif resolver is not None and (resolver not in ledger.reviews
                                   or IE.visible_from(ledger.reviews[resolver]) > known_at):
        # 見直しの結果がまだ見えていない＝以降の日程は条件付きの未来（言い切らない）
        parts.append(f"{when} の見直しの結果によって、以降の日程が変わる場合があります。")
    if not st.schedule_confirmed:
        parts.append(f"日程の一部は規則からの推定です（{UNCONFIRMED_NOTE}）。")
    parts.append(IMPACT_NOTE)
    return "".join(parts)


def schedule_rows(st: IE.IndexStatus) -> list:
    """企業詳細の日程。日付が一次資料で確定した回だけ日付を出す。"""
    return [{"m": s.m, "coefficient": float(s.coefficient), "confirmed": s.date_basis == "primary",
             "date_text": s.effective_on.isoformat() if s.date_basis == "primary" else UNCONFIRMED_NOTE}
            for s in _schedule(st).steps]


# ── 行と表 ─────────────────────────────────────────────────────────────────────

def row_note(ledger: IE.IndexLedger, ec: str, on: date, known_at: date, *,
             with_schedule: bool = False) -> Optional[dict]:
    """1 社の注記。出さない社（対象外・期限切れ・未公表・除外済み等）は None。"""
    st = IE.status_at(ledger, ec, on, known_at)
    if st.kind not in DISPLAY_KINDS or st.status not in DISPLAY_STATUSES:
        return None
    steps = _schedule(st).steps
    last = steps[-1].effective_on if steps else st.effective
    if on > last + timedelta(days=DISPLAY_TAIL_DAYS):
        return None
    out = {"kind": st.kind, "review": st.review, "announced": st.announced.isoformat(),
           "effective": st.effective.isoformat(), "status": st.status,
           "schedule_confirmed": st.schedule_confirmed, "label": DISPLAY_KINDS[st.kind],
           "note": note_text(ledger, st, known_at),
           "source": source_text(ledger.reviews[_schedule(st).origin])}
    if with_schedule:
        out["schedule"] = schedule_rows(st)
    return out


def summary_text(what: str, n_shown: int, counts: Mapping[str, int],
                 base_rate: Mapping[str, Optional[float]]) -> str:
    def part(kind: str) -> str:
        rate = base_rate.get(kind)
        tail = f"（母集団では {rate * 100:.1f}%）" if rate is not None else ""
        return f"{KIND_NAMES[kind]} {counts.get(kind, 0)} 社{tail}"
    return f"{what} {n_shown} 社中、TOPIX " + "・".join(part(k) for k in DISPLAY_KINDS)


def no_ledger_meta(ledger: IE.IndexLedger) -> dict:
    return {"ledger_ok": False, "reason": ledger.reason, "banner": BANNER_NO_LEDGER,
            "asof": None, "counts_in_results": None, "base_rate": None, "summary": None}


def annotate(db, shown: Iterable[str], population: Iterable[str], *, what: str,
             on: Optional[date] = None) -> tuple:
    """(行の注記 {社: dict|None}, 表の注記)。`shown` は表に出す社、`population` は比率の分母の社。"""
    ledger = cached_ledger(db)
    shown = [ec for ec in shown if ec]
    if not ledger.ok:
        return {ec: None for ec in shown}, no_ledger_meta(ledger)
    today = on or jst_today()
    notes = {ec: row_note(ledger, ec, today, today) for ec in shown}
    counts = Counter(n["kind"] for n in notes.values() if n)
    labels = IE.group_labels(ledger, [ec for ec in population if ec], on=today, known_at=today)
    n_pop = len(labels)
    base_rate = {k: (sum(1 for g in labels.values() if g == k) / n_pop if n_pop else None)
                 for k in DISPLAY_KINDS}
    counts_out = {k: counts.get(k, 0) for k in DISPLAY_KINDS}
    return notes, {"ledger_ok": True, "reason": "", "banner": None, "asof": today.isoformat(),
                   "counts_in_results": counts_out, "base_rate": base_rate,
                   "summary": summary_text(what, len(shown), counts_out, base_rate)}


def company_note(db, ec: str, *, on: Optional[date] = None) -> tuple:
    """企業詳細の (注記 dict|None・日程付き, 表の注記)。"""
    ledger = cached_ledger(db)
    if not ledger.ok:
        return None, no_ledger_meta(ledger)
    today = on or jst_today()
    return (row_note(ledger, ec, today, today, with_schedule=True),
            {"ledger_ok": True, "reason": "", "banner": None, "asof": today.isoformat(),
             "counts_in_results": None, "base_rate": None, "summary": None})
