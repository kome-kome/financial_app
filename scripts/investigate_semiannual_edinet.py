"""Issue #219② フェーズB de-risk: EDINET 半期報告書(formCode=050000)の実物1件で、
既存パース機構(parse_xbrl_csv)がそのまま H1 財務値を抽出できるかを検証する（読取専用）。

確認項目:
  1) formCode=050000 の書類が実在し、docTypeCode/periodEnd/ordinanceCode を確認
  2) documents.json の periodEnd が H1 末日（例 09-30）を返すか
  3) その XBRL を parse_xbrl_csv に通し、pl_revenue/bs_total_assets/cf_operating_cf が
     非null で取れるか（context 命名の変種で取りこぼさないか）
  4) 主要要素の実 context 名を数件ダンプ（CurrentYTDDuration 等の実際の綴りを確認）
"""
import asyncio
from datetime import date

import httpx

from dotenv import load_dotenv
load_dotenv()

from collector_utils import EDINET_BASE, API_KEY  # noqa: E402
from collector_financials import fetch_xbrl_csv, parse_xbrl_csv, _detect_xbrl_columns  # noqa: E402
from database import build_xbrl_map  # noqa: E402

XBRL_MAP = build_xbrl_map()

# 3月決算企業のH1(4-9月)半期報告書は11月中旬提出。候補日を数日走査する。
CANDIDATE_DATES = [date(2024, 11, d) for d in (13, 14, 15, 18, 19, 20, 21, 22, 25, 26, 27, 28, 29)]


async def list_semiannual(client, target: date) -> list:
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": target.isoformat(), "type": 2, "Subscription-Key": API_KEY}
    r = await client.get(url, params=params, timeout=30)
    r.raise_for_status()
    results = r.json().get("results") or []
    return [d for d in results if d.get("formCode") == "050000" and d.get("secCode")]


async def main():
    async with httpx.AsyncClient() as client:
        sample = None
        for target in CANDIDATE_DATES:
            try:
                docs = await list_semiannual(client, target)
            except Exception as e:
                print(f"{target}: 取得失敗 {e}")
                continue
            print(f"{target}: formCode=050000 かつ secCode有り = {len(docs)}件")
            if docs and sample is None:
                # 代表1件を控える（本体の財務書類=XBRLがあるものを優先）
                sample = docs[0]
                for d in docs[:5]:
                    print(f"    docID={d.get('docID')} secCode={d.get('secCode')} "
                          f"ordinance={d.get('ordinanceCode')} form={d.get('formCode')} "
                          f"docType={d.get('docTypeCode')} periodStart={d.get('periodStart')} "
                          f"periodEnd={d.get('periodEnd')} name={d.get('filerName')}")
            await asyncio.sleep(1.0)

        if sample is None:
            print("\n半期報告書が見つからなかった（候補日を広げる必要あり）")
            return

        doc_id = sample["docID"]
        period_end = sample.get("periodEnd") or ""
        edinet_code = sample.get("edinetCode")
        print(f"\n=== 深掘り: docID={doc_id} {sample.get('filerName')} periodEnd={period_end} ===")

        df = await fetch_xbrl_csv(client, doc_id)
        if df is None or df.empty:
            print("  XBRL CSV が空 / 取得不可")
            return

        df.columns = [c.strip() for c in df.columns]
        col_map = _detect_xbrl_columns(df)
        print(f"  検出列マップ: {col_map}")

        # 主要要素の実 context 名をダンプ（命名変種の確認）
        elem_col = col_map.get("element")
        ctx_col = col_map.get("context")
        val_col = col_map.get("value")
        if elem_col and ctx_col:
            watch = {"NetSales", "Revenue", "Assets", "OperatingIncome",
                     "NetCashProvidedByUsedInOperatingActivities", "NetIncomeLoss", "ProfitLoss"}
            print("  --- 主要要素の context 実サンプル ---")
            seen = 0
            for _, row in df.iterrows():
                raw = str(row[elem_col])
                elem = raw.split(":")[-1] if ":" in raw else raw
                if elem in watch:
                    print(f"    {elem:50s} ctx={row[ctx_col]} val={row[val_col]}")
                    seen += 1
                    if seen >= 25:
                        break

        parsed = parse_xbrl_csv(df, edinet_code, period_end)
        print("\n  --- parse_xbrl_csv 抽出結果（主要項目）---")
        print(f"    pl.revenue        = {parsed['pl'].get('revenue')}")
        print(f"    pl.operating_profit = {parsed['pl'].get('operating_profit')}")
        print(f"    pl.net_income     = {parsed['pl'].get('net_income')}")
        print(f"    bs.total_assets   = {parsed['bs'].get('total_assets')}")
        print(f"    bs.total_equity   = {parsed['bs'].get('total_equity')}")
        print(f"    cf.operating_cf   = {parsed['cf'].get('operating_cf')}")
        print(f"    bs非null={sum(1 for v in parsed['bs'].values() if v is not None)}件 "
              f"pl非null={sum(1 for v in parsed['pl'].values() if v is not None)}件 "
              f"cf非null={sum(1 for v in parsed['cf'].values() if v is not None)}件")


if __name__ == "__main__":
    asyncio.run(main())
