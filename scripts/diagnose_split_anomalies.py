"""Issue #324: 21社の株式分割前後データを個別診断する（読取専用・書込みなし）。

Yahoo Finance日次終値を分割日±60日で取得し、以下を機械的に判定する:
  - 分割日近辺で「単発のクリーンな水準シフト」になっているか
  - それとも複数日にわたって前後を往復する異常値（E02075/6731で確認済みパターン）が
    混入しているか（前日比の符号が短期間で頻繁に反転＝smoothでない）

本スクリプトは診断のみ。修正（record_prices_batch書込み）は診断結果を見てから
別途・個別に判断する。
"""
import asyncio
from datetime import date, timedelta

import httpx

from dotenv import load_dotenv
load_dotenv()

from collector_prices import fetch_yahoo_history  # noqa: E402

# edinet_code, sec_code, [(split_date, ratio_str), ...]
TARGETS = [
    ("E01028", "4107", [("2025-12-29", "10:1")]),
    ("E01107", "5189", [("2023-09-28", "2:1")]),
    ("E02075", "6731", [("2023-12-27", "1:100"), ("2026-03-30", "1:10")]),
    ("E02794", "7422", [("2024-12-19", "10:1")]),
    ("E03137", "8227", [("2024-02-19", "2:1"), ("2026-02-19", "3:1")]),
    ("E05280", "8798", [("2021-03-30", "2:1")]),
    ("E05356", "2375", [("2021-03-30", "3:1")]),
    ("E05669", "3841", [("2022-03-30", "2:1")]),
    ("E32225", "3936", [("2021-09-15", "5:1"), ("2021-11-01", "3:1"), ("2021-12-02", "2:1")]),
    ("E32815", "3968", [("2024-02-28", "3:1")]),
    ("E33392", "7809", [("2023-06-29", "3:1")]),
    ("E33610", "9388", [("2025-01-30", "10:1")]),
    ("E33868", "6573", [("2023-10-04", "3:1")]),
    ("E34379", "7042", [("2025-03-28", "2:1")]),
    ("E34748", "7063", [("2022-12-29", "2:1")]),
    ("E35932", "7692", [("2022-10-28", "3:1"), ("2023-03-15", "4:1"), ("2023-04-27", "3:1")]),
    ("E36133", "4935", [("2024-06-27", "2:1"), ("2025-12-29", "5:1")]),
    ("E36860", "6522", [("2021-11-25", "4:1")]),
    ("E36897", "4371", [("2022-03-30", "2:1"), ("2022-09-29", "2:1")]),
    ("E37457", "7138", [("2025-08-28", "5:1")]),
    ("E39023", "5537", [("2025-10-09", "4:1")]),
]

WINDOW_DAYS = 60
# 前日比が±閾値を超える日を「ジャンプ」候補としてマーク
JUMP_THRESHOLD = 0.15


ECHO_MOVE_THRESHOLD = 0.15   # この比率以上動いたら「離れた」とみなす
ECHO_RETURN_TOLERANCE = 0.05  # 離れた後この比率以内に戻れば「往復（エコー）」とみなす
ECHO_LOOKAHEAD_DAYS = 5       # 往復が成立するまでの最大営業日数


def _find_echoes(closes: list[tuple]) -> list[tuple]:
    """A→(乖離)→A′ のような往復（E02075/6731で確認済みの実データ異常パターン）を検出する。
    通常のボラティリティ（一方向の大幅値動き→戻らない）とは区別する。
    """
    echoes = []
    n = len(closes)
    for i in range(n):
        base_d, base_c = closes[i]
        if not base_c or base_c <= 0:
            continue
        moved_away = False
        moved_idx = None
        for k in range(i + 1, min(i + 1 + ECHO_LOOKAHEAD_DAYS, n)):
            d, c = closes[k]
            if not c or c <= 0:
                continue
            dev = c / base_c - 1.0
            if not moved_away and abs(dev) > ECHO_MOVE_THRESHOLD:
                moved_away = True
                moved_idx = k
                continue
            if moved_away and abs(dev) < ECHO_RETURN_TOLERANCE:
                echoes.append((base_d, closes[moved_idx][0], d, base_c, closes[moved_idx][1], c))
                break
    return echoes


def _cluster_dates(dates: list[str], max_gap_days: int = 10) -> list[list]:
    """日付リストを、連続性（土日・祝日を許容するmax_gap_days以内の間隔）でクラスタ化する。"""
    ds = sorted(date.fromisoformat(d) for d in dates)
    clusters = [[ds[0]]]
    for d in ds[1:]:
        if (d - clusters[-1][-1]).days <= max_gap_days:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    return clusters


def _find_duplicate_values(closes: list[tuple]) -> list[tuple]:
    """同一終値が非連続の複数クラスタで再出現する（=一度離れた値に戻る）パターンを検出する
    （E02075/6731で確認済み: 2000.0⇄1000.0が入れ替わりながら再出現し続けた実データ異常）。

    薄商いで同値が何週間も連続するだけ（1クラスタ）は正常（分割前後の板が薄い銘柄では普通）
    なので対象外とし、「一度別の値に切り替わった後にまた戻る」（2クラスタ以上）のみを異常とする。
    """
    by_value: dict[float, list[str]] = {}
    for d, c in closes:
        if c:
            by_value.setdefault(round(c, 2), []).append(d)

    flags = []
    for value, dates in by_value.items():
        if len(dates) < 4:
            continue
        clusters = _cluster_dates(dates)
        if len(clusters) >= 2:
            flags.append((value, dates, len(clusters)))
    return flags


def _classify(rows: list, split_date: str) -> dict:
    """分割日±WINDOW_DAYS の終値系列から、単発シフトか異常（往復エコー）混在かを判定する。"""
    rows = sorted(rows, key=lambda r: r["trade_date"])
    closes = [(r["trade_date"], r["close"]) for r in rows if r["close"]]
    if len(closes) < 5:
        return {"status": "insufficient_data", "n": len(closes)}

    jumps = []
    for i in range(1, len(closes)):
        prev_d, prev_c = closes[i - 1]
        cur_d, cur_c = closes[i]
        if prev_c and cur_c and prev_c > 0:
            pct = cur_c / prev_c - 1.0
            if abs(pct) > JUMP_THRESHOLD:
                jumps.append((cur_d, prev_c, cur_c, round(pct, 4)))

    echoes = _find_echoes(closes)
    dup_values = _find_duplicate_values(closes)

    if dup_values:
        status = "duplicate_value_anomaly"
    elif echoes:
        status = "echo_suspected_needs_review"
    elif len(jumps) == 0:
        status = "no_jump_detected"
    elif len(jumps) == 1 and jumps[0][0] == split_date:
        status = "clean_single_shift"
    else:
        status = "organic_volatility_no_action_needed"

    return {
        "status": status,
        "n": len(closes),
        "jumps": jumps,
        "echoes": echoes,
        "dup_values": dup_values,
        "first": closes[0],
        "last": closes[-1],
    }


async def diagnose_one(session, edinet_code, sec_code, split_date):
    d = date.fromisoformat(split_date)
    d_from = (d - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    d_to = (d + timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    rows = await fetch_yahoo_history(session, f"{sec_code}.T", d_from, d_to)
    result = _classify(rows, split_date)
    return edinet_code, sec_code, split_date, result


async def main():
    async with httpx.AsyncClient() as session:
        for edinet_code, sec_code, splits in TARGETS:
            for split_date, ratio in splits:
                _, _, _, result = await diagnose_one(session, edinet_code, sec_code, split_date)
                status = result["status"]
                print(f"{edinet_code} {sec_code} {split_date}({ratio}): {status} "
                      f"(n={result.get('n')})")
                if result.get("dup_values"):
                    for v, dates, n_clusters in result["dup_values"]:
                        print(f"    dup_value={v} ({n_clusters}クラスタ): {dates}")
                await asyncio.sleep(0.3)  # Yahoo APIへの配慮


if __name__ == "__main__":
    asyncio.run(main())
