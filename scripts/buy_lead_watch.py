"""買いの主役（M-6）と M-2 の差の点検（#1225・読むだけ）。

規則は `docs/prereg/buy-lead-watch.md`（本文は main へのマージで凍結）。`measure_delisting_redesign` の結果の JSON の
`series`（答えのある行だけの、期ごとの rank-IC と上位10社超過）を読み、全期と直近 8 期の 2 つの窓で M-6 − M-2 を出して、
事前登録 §4 の「当たり」と連続の回数を機械的に当てはめる。

- **DB を触らない・新しい計測をしない。** 入力は計測が書いた JSON と、事前登録の文書だけ
- 連続は、文書の追記節の「点検の記録」の表（これまでの点検）と今回の読みから数える。表へ足す 1 行を出力する
  （足すのは PR で行う＝公開の記録になる。この道具は文書を書き換えない）
- 数えるのは、最後の test 月が `BASELINE_LAST` より後の点検だけ。凍結の時点の 19 期は規則を作るきっかけになった数字
  なので「基準の回」として記録するだけ（着手の条件は新しい証拠だけで数える）
- 条件に当たっても本番は切り替えない。`act` が真なら、買いの主役を見直す Issue を起票する合図

終了コード: 0 = 読めた（当たりかどうかに関わらず）/ 2 = 事前登録の不一致 / 3 = 入力が判定に使えない。

実行:
    python -m scripts.buy_lead_watch --prereg docs/prereg/buy-lead-watch.md --prereg-sha <凍結値>
    python -m scripts.buy_lead_watch --prereg ... --prereg-sha ... --json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_stats import paired_ic_significance  # noqa: E402
from scripts import prereg  # noqa: E402

PREREG = "docs/prereg/buy-lead-watch.md"
SOURCE = ROOT / "scripts" / ".cache" / "delisting_redesign.json"
EXIT_INPUT = 3

# 事前登録 §2〜§4 の値（本文と同じ値。変えるなら文書を凍結し直す）。
LEAD, RIVAL = "macro_enet", "macro_gbdt"   # 差は M-6 − M-2（M-2 は M-4 の基底＝既定の設定）
N_WINDOWS = 2                              # 全期と直近 8 期
ALPHA = 0.05 / N_WINDOWS                   # Bonferroni（1 検定あたり 0.025）
RECENT_N = 8                               # 直近の窓の期数
BASELINE_LAST = "2025-08"                  # 凍結の時点の最後の test 月。これより後の点検だけを数える
STREAK_TO_ACT = 2                          # 数える点検でこの回数続けて当たったら Issue を起票する

LEDGER_HEADING = "### 点検の記録"
LEDGER_COLUMNS = ("最後の test 月", "期数", "直近8期 rank-IC の差", "p", "直近8期 上位10社超過の差",
                  "全期 rank-IC の差", "全期 p", "当たり", "数える", "連続", "記録日")
YES, NO, NOT_COUNTED = "はい", "いいえ", "—"
_YM_RE = re.compile(r"\d{4}-\d{2}")


class WatchInputError(ValueError):
    """入力が判定に使えない（間引いた出力・`series` が無い・期がそろわない・期数が足りない）。"""


# ── 読み ────────────────────────────────────────────────────────────────────────

def common_months(series: dict) -> list[str]:
    """M-6 と M-2 の rank-IC と上位10社超過がそろった test 月（昇順）。そろわなければ WatchInputError。

    片方にしか無い期を黙って落とすと、窓の中身が点検ごとに変わる。そろわないときは判定しない（事前登録 §4）。
    """
    keys = []
    for name in (LEAD, RIVAL):
        s = series.get(name) or {}
        for field in ("rank_ic", "top_k_excess"):
            values = s.get(field) or {}
            if any(v is None for v in values.values()):
                raise WatchInputError(f"{name} の {field} に値の無い期がある")
            keys.append((name, field, set(values)))
    base = keys[0][2]
    for name, field, ks in keys[1:]:
        if ks != base:
            diff = sorted(ks ^ base)
            raise WatchInputError(f"{name} の {field} の期が {keys[0][0]} の {keys[0][1]} とそろわない（例: {diff[:3]}）")
    return sorted(base)


def window_stats(series: dict, months: Sequence[str]) -> dict:
    """窓 `months` での M-6 − M-2（事前登録 §3 の (a)・(b)）。(a) は対の検定、(b) は平均だけ。"""
    lead, rival = series[LEAD], series[RIVAL]
    sig = paired_ic_significance({m: lead["rank_ic"][m] for m in months},
                                 {m: rival["rank_ic"][m] for m in months}, alpha=ALPHA)
    return {
        "n": len(months), "first": months[0], "last": months[-1],
        "rank_ic_diff": sig["mean"] if sig else None,
        "rank_ic_p": sig["p_value"] if sig else None,
        "rank_ic_ci": [sig["ci_lo"], sig["ci_hi"]] if sig else None,
        "top_k_excess_diff": statistics.mean(lead["top_k_excess"][m] - rival["top_k_excess"][m] for m in months),
    }


def is_hit(recent: dict) -> bool:
    """事前登録 §4 の「当たり」: 直近の窓で (a) が負で p < α、かつ (b) が負。検定が出なければ当たりにしない。"""
    diff, p = recent["rank_ic_diff"], recent["rank_ic_p"]
    return diff is not None and p is not None and diff < 0 and p < ALPHA and recent["top_k_excess_diff"] < 0


def read(payload: dict) -> dict:
    """計測の JSON から今回の読みを作る。判定に使えない入力は WatchInputError。"""
    if payload.get("smoke"):
        raise WatchInputError("間引いた動作確認（--smoke）の出力は判定に使わない")
    series = payload.get("series")
    if not isinstance(series, dict) or LEAD not in series or RIVAL not in series:
        raise WatchInputError("結果に series が無い（#1225 より前の出力。measure:delisting-redesign を回し直す）")
    months = common_months(series)
    if len(months) < RECENT_N:
        raise WatchInputError(f"期数が {len(months)} で、直近の窓（{RECENT_N} 期）に満たない")
    recent = window_stats(series, months[-RECENT_N:])
    return {
        "last_test_month": months[-1], "n_periods": len(months),
        "all": window_stats(series, months), "recent": recent,
        "hit": is_hit(recent), "counted": months[-1] > BASELINE_LAST,
    }


# ── 連続の回数（点検の記録の表）───────────────────────────────────────────────────

def ledger_rows(text: str) -> list[dict]:
    """文書の追記節の「点検の記録」の表の行（上から＝古い順）。表が無い・行が無いなら空。"""
    rows = []
    for line in prereg.appendix_section(text, LEDGER_HEADING).split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != len(LEDGER_COLUMNS) or not _YM_RE.fullmatch(cells[0]):
            continue
        rows.append({"last_test_month": cells[0], "hit": cells[7] == YES, "counted": cells[8] == YES})
    return rows


def streak(history: Sequence[dict], reading: dict) -> int:
    """今回の読みまでで、「数える点検」が続けて当たった回数（事前登録 §4）。

    基準の回（数えない行）は飛ばす。最後の test 月が同じ行は何度読んでも 1 回（今回と同じ月の古い行も飛ばす）。
    当たりでない数える点検が挟まれば、そこで切れる。
    """
    if not reading["counted"] or not reading["hit"]:
        return 0
    n, seen = 1, {reading["last_test_month"]}
    for row in reversed(list(history)):
        if not row["counted"] or row["last_test_month"] in seen:
            continue
        if not row["hit"]:
            break
        seen.add(row["last_test_month"])
        n += 1
    return n


# ── 出力 ────────────────────────────────────────────────────────────────────────

def _f(v, digits: int = 4) -> str:
    return "-" if v is None else f"{v:+.{digits}f}"


def _p(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def ledger_line(reading: dict, n_streak: int, today: str) -> str:
    """追記節の表へ足す 1 行（`ledger_rows` が読み戻せる形）。"""
    r, a = reading["recent"], reading["all"]
    cells = [reading["last_test_month"], str(reading["n_periods"]),
             _f(r["rank_ic_diff"]), _p(r["rank_ic_p"]), _f(r["top_k_excess_diff"]),
             _f(a["rank_ic_diff"]), _p(a["rank_ic_p"]),
             YES if reading["hit"] else NO, YES if reading["counted"] else NO,
             str(n_streak) if reading["counted"] else NOT_COUNTED, today]
    return "| " + " | ".join(cells) + " |"


def _print(report: dict) -> None:
    print(f"\n=== 買いの主役の点検（#1225）M-6 − M-2・test 月 {report['n_periods']}・"
          f"最後 {report['last_test_month']} ===", flush=True)
    print(f"{'窓':<10}{'期':>4}{'範囲':>20}{'rank-IC の差':>14}{'p':>8}{'上位10社超過の差':>18}", flush=True)
    for label, key in (("全期", "all"), (f"直近{RECENT_N}期", "recent")):
        w = report[key]
        print(f"{label:<10}{w['n']:>4}{w['first'] + '..' + w['last']:>20}{_f(w['rank_ic_diff']):>14}"
              f"{_p(w['rank_ic_p']):>8}{_f(w['top_k_excess_diff']):>18}", flush=True)
    kind = "数える点検" if report["counted"] else f"基準の回（最後の test 月が {BASELINE_LAST} 以前・数えない）"
    print(f"\n当たり（直近{RECENT_N}期で rank-IC の差が負で p < {ALPHA}、かつ上位10社超過の差が負）: "
          f"{YES if report['hit'] else NO} / {kind} / 連続 {report['streak']} 回", flush=True)
    if report["act"]:
        print(f"**{STREAK_TO_ACT} 回続けて当たった。買いの主役を見直す Issue を起票する**（事前登録 §4）", flush=True)
    print("\n点検の記録へ足す行:", flush=True)
    print(report["ledger_line"], flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.buy_lead_watch",
                                 description="買いの主役（M-6）と M-2 の差の点検（#1225・読むだけ）")
    prereg.add_arguments(ap)
    ap.add_argument("--source", default=str(SOURCE),
                    help="measure_delisting_redesign の結果の JSON（既定は scripts/.cache/delisting_redesign.json）")
    ap.add_argument("--json", action="store_true", help="表の代わりに JSON を出す")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)          # 不一致は読む前に終了コード 2
    if Path(args.prereg).as_posix() != PREREG:
        print(f"[buy_lead_watch] NG: 事前登録は {PREREG}（受け取った値: {args.prereg}）", file=sys.stderr)
        return prereg.EXIT_MISMATCH
    try:
        payload = json.loads(Path(args.source).read_text(encoding="utf-8"))
        reading = read(payload)
    except (OSError, ValueError) as e:            # 読めない・JSON でない・判定に使えない
        print(f"[buy_lead_watch] NG: {e}", file=sys.stderr)
        return EXIT_INPUT
    n_streak = streak(ledger_rows(prereg.read_document(args.prereg)), reading)
    report = {
        **stamp,
        "source": Path(args.source).name,
        "source_computed_at": payload.get("computed_at"),
        "rule": {"pair": [LEAD, RIVAL], "alpha": ALPHA, "recent_n": RECENT_N,
                 "baseline_last": BASELINE_LAST, "streak_to_act": STREAK_TO_ACT},
        **reading,
        "streak": n_streak,
        "act": n_streak >= STREAK_TO_ACT,
        "ledger_line": ledger_line(reading, n_streak, date.today().isoformat()),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
