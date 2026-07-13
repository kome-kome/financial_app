"""Issue #324: 「要目視精査」9社の深掘り診断（読取専用）。

diagnose_split_anomalies.py の echo_suspected_needs_review 判定を、出来高(volume)を
追加シグナルに使って再判定する。真のデータ不良（Yahoo側の価格取り違え等）であれば、
異常な値動きの日に出来高が0または欠損である可能性が高い（実売買を伴わない誤登録）。
一方、出来高が正常にあれば実際の売買で動いた価格＝通常のボラティリティと判断できる。
"""
import asyncio
from datetime import date, timedelta

import httpx

from dotenv import load_dotenv
load_dotenv()

from collector_prices import fetch_yahoo_history  # noqa: E402
from scripts.diagnose_split_anomalies import _find_echoes, _find_duplicate_values, WINDOW_DAYS  # noqa: E402

TARGETS = [
    ("E02794", "7422", "2024-12-19"),
    ("E32225", "3936", "2021-09-15"),
    ("E32225", "3936", "2021-11-01"),
    ("E32225", "3936", "2021-12-02"),
    ("E33868", "6573", "2023-10-04"),
    ("E35932", "7692", "2023-03-15"),
    ("E35932", "7692", "2023-04-27"),
    ("E36133", "4935", "2024-06-27"),
    ("E36860", "6522", "2021-11-25"),
    ("E36897", "4371", "2022-03-30"),
    ("E37457", "7138", "2025-08-28"),
    # 「明確な異常」候補（duplicate_value_anomaly）も出来高で裏付け確認
    ("E02075", "6731", "2023-12-27"),
    ("E02075", "6731", "2026-03-30"),
    ("E05669", "3841", "2022-03-30"),
    ("E34379", "7042", "2025-03-28"),
    ("E35932", "7692", "2022-10-28"),
]


async def diagnose_one(session, edinet_code, sec_code, split_date):
    d = date.fromisoformat(split_date)
    d_from = (d - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    d_to = (d + timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    rows = await fetch_yahoo_history(session, f"{sec_code}.T", d_from, d_to)
    rows_by_date = {r["trade_date"]: r for r in rows}
    closes = [(r["trade_date"], r["close"]) for r in sorted(rows, key=lambda r: r["trade_date"]) if r["close"]]
    echoes = _find_echoes(closes)
    dup_values = _find_duplicate_values(closes)

    print(f"\n=== {edinet_code} {sec_code} {split_date} ===")

    target_dates = set()
    for e in echoes[:8]:
        target_dates.update([e[0], e[1], e[2]])
    for value, dates, n_clusters in dup_values:
        target_dates.update(dates)

    if not target_dates:
        print("  echo/重複値パターンなし")
        return

    zero_volume_hits = 0
    total_hits = 0
    for d_str in sorted(target_dates):
        r = rows_by_date.get(d_str)
        if r is None:
            continue
        total_hits += 1
        vol = r.get("volume")
        h, l = r.get("high"), r.get("low")
        flat_range = (h is not None and l is not None and abs(h - l) < 0.01)
        flag = ""
        if not vol:
            flag += " <-- 出来高0/欠損"
            zero_volume_hits += 1
        if flat_range:
            flag += " <-- H=L(値幅ゼロ)"
        print(f"  {d_str}: O={r['open']} H={h} L={l} C={r['close']} V={vol}{flag}")
    print(f"  対象日数中 出来高0/欠損: {zero_volume_hits}/{total_hits}")


async def main():
    async with httpx.AsyncClient() as session:
        for edinet_code, sec_code, split_date in TARGETS:
            await diagnose_one(session, edinet_code, sec_code, split_date)
            await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
