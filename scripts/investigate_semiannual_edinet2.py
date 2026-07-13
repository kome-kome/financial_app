"""Issue #219② フェーズB de-risk (第2パス): 新制度・上場企業の半期報告書の実体を特定する。

第1パスの残課題を解消する:
  (A) 上場企業のH1半期報告書が実際に使う formCode/docTypeCode と提出volume/timing
  (B) 連結(大企業)での Interim context の綴りと、連結値が正しく優先されるか
  (C) 真の interim period_end の取得源（DEI要素 jpdei_cor:*）
"""
import asyncio
from collections import Counter
from datetime import date

import httpx

from dotenv import load_dotenv
load_dotenv()

from collector_utils import EDINET_BASE, API_KEY  # noqa: E402
from collector_financials import fetch_xbrl_csv, parse_xbrl_csv, _detect_xbrl_columns  # noqa: E402

# 3月決算のH1(4-9月末)半期報告書は11月中旬〜下旬提出。数日をtallyする。
TALLY_DATES = [date(2024, 11, d) for d in (14, 15, 20, 22, 26, 27, 28, 29)]


async def get_docs(client, target: date) -> list:
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": target.isoformat(), "type": 2, "Subscription-Key": API_KEY}
    r = await client.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results") or []


async def main():
    async with httpx.AsyncClient() as client:
        # ── (A) formCode×docTypeCode 分布（secCode有り＝上場のみ）──
        form_type = Counter()
        big_sample = None   # secCodeの数値が小さい=歴史ある大型銘柄を1件深掘り用に確保
        for target in TALLY_DATES:
            try:
                docs = await get_docs(client, target)
            except Exception as e:
                print(f"{target}: 取得失敗 {e}")
                continue
            listed = [d for d in docs if d.get("secCode")]
            for d in listed:
                form_type[(d.get("formCode"), d.get("docTypeCode"))] += 1
            # 半期らしき docType(160)を大型銘柄で1件確保
            for d in listed:
                if d.get("docTypeCode") == "160":
                    sc = d.get("secCode") or "99999"
                    if big_sample is None or sc < (big_sample.get("secCode") or "99999"):
                        big_sample = d
            print(f"{target}: 上場書類 {len(listed)}件")
            await asyncio.sleep(0.8)

        print("\n=== (A) formCode×docTypeCode 分布（secCode有り・降順）===")
        for (fc, dt), n in form_type.most_common(20):
            print(f"    form={fc} docType={dt}: {n}件")

        if big_sample is None:
            print("\ndocType=160 の上場書類が見つからず。半期報告書の識別方法を再検討要。")
            return

        # ── (B)(C) 代表1件（できるだけ大型）を深掘り ──
        doc_id = big_sample["docID"]
        print(f"\n=== (B)(C) 深掘り: docID={doc_id} sec={big_sample.get('secCode')} "
              f"form={big_sample.get('formCode')} docType={big_sample.get('docTypeCode')} "
              f"name={big_sample.get('filerName')} metaPeriodEnd={big_sample.get('periodEnd')} ===")

        df = await fetch_xbrl_csv(client, doc_id)
        if df is None or df.empty:
            print("  XBRL空")
            return
        df.columns = [c.strip() for c in df.columns]
        col_map = _detect_xbrl_columns(df)
        elem_col, ctx_col, val_col = col_map.get("element"), col_map.get("context"), col_map.get("value")

        # (C) DEI要素（期間種別・当期末日）をダンプ
        print("  --- (C) DEI/期間メタ要素 ---")
        for _, row in df.iterrows():
            raw = str(row[elem_col])
            elem = raw.split(":")[-1] if ":" in raw else raw
            if any(k in elem for k in ("TypeOfCurrentPeriod", "CurrentPeriodEndDate",
                                       "CurrentFiscalYearEndDate", "CurrentFiscalYearStartDate",
                                       "PeriodOfReport", "AccountingPeriod")):
                print(f"    {elem:55s} = {row[val_col]}")

        # (B) 連結Interim context の綴りサンプル
        print("  --- (B) 主要要素の context 実サンプル（連結の綴り確認）---")
        watch = {"NetSales", "Revenue", "OperatingIncome", "Assets",
                 "NetCashProvidedByUsedInOperatingActivities", "ProfitLoss"}
        seen = 0
        for _, row in df.iterrows():
            raw = str(row[elem_col])
            elem = raw.split(":")[-1] if ":" in raw else raw
            if elem in watch and "Member" not in str(row[ctx_col]):
                print(f"    {elem:50s} ctx={row[ctx_col]} val={row[val_col]}")
                seen += 1
                if seen >= 20:
                    break

        parsed = parse_xbrl_csv(df, big_sample.get("edinetCode"), big_sample.get("periodEnd") or "")
        print("\n  --- parse抽出（連結H1が取れるか）---")
        print(f"    pl.revenue={parsed['pl'].get('revenue')} op={parsed['pl'].get('operating_profit')} "
              f"net={parsed['pl'].get('net_income')}")
        print(f"    bs.total_assets={parsed['bs'].get('total_assets')} equity={parsed['bs'].get('total_equity')}")
        print(f"    cf.operating_cf={parsed['cf'].get('operating_cf')}")
        print(f"    bs非null={sum(1 for v in parsed['bs'].values() if v is not None)} "
              f"pl非null={sum(1 for v in parsed['pl'].values() if v is not None)} "
              f"cf非null={sum(1 for v in parsed['cf'].values() if v is not None)}")


if __name__ == "__main__":
    asyncio.run(main())
