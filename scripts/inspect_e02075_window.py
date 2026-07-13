"""E02075(6731)の2023-12-27分割前後を広範囲(2023-09〜2024-03)で精査する（読取専用）。"""
import asyncio

import httpx

from dotenv import load_dotenv
load_dotenv()

from collector_prices import fetch_yahoo_history  # noqa: E402


async def main():
    async with httpx.AsyncClient() as s:
        rows = await fetch_yahoo_history(s, "6731.T", "20230901", "20240301")
        rows = sorted(rows, key=lambda r: r["trade_date"])
        for r in rows:
            print(r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"])


if __name__ == "__main__":
    asyncio.run(main())
