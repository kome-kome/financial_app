"""Issue #219② フェーズB de-risk (第3パス): 旧四半期報告書(〜2024廃止)のうち
Q2(中間=H1累計)報告書を特定し、既存パースでH1累計が取れるかを検証する（読取専用）。

確認:
  (A) 廃止前(2022-11)の上場四半期報告書の formCode/docType 分布
  (B) TypeOfCurrentPeriodDEI=Q2 の報告書を1件深掘り: DEI(period_end/種別) と
      parse_xbrl_csv による H1累計(revenue/assets/ocf)抽出
"""
import asyncio
from collections import Counter
from datetime import date

import httpx

from dotenv import load_dotenv
load_dotenv()

from collector_utils import EDINET_BASE, API_KEY  # noqa: E402
from collector_financials import fetch_xbrl_csv, parse_xbrl_csv, _detect_xbrl_columns  # noqa: E402

# 3月決算のQ2(4-9月)四半期報告書は2022年11月中旬提出。
TALLY_DATES = [date(2022, 11, d) for d in (11, 14)]


async def get_docs(client, target: date) -> list:
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": target.isoformat(), "type": 2, "Subscription-Key": API_KEY}
    r = await client.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results") or []


def dei_of(df, elem_col, val_col, needle):
    for _, row in df.iterrows():
        raw = str(row[elem_col])
        elem = raw.split(":")[-1] if ":" in raw else raw
        if elem == needle:
            return str(row[val_col])
    return None


async def main():
    async with httpx.AsyncClient() as client:
        form_type = Counter()
        candidates = []
        for target in TALLY_DATES:
            try:
                docs = await get_docs(client, target)
            except Exception as e:
                print(f"{target}: 取得失敗 {e}")
                continue
            listed = [d for d in docs if d.get("secCode")]
            for d in listed:
                form_type[(d.get("formCode"), d.get("docTypeCode"))] += 1
            # 四半期らしき docType を候補に（docType 140=四半期報告書想定）
            for d in listed:
                if d.get("docTypeCode") in ("140", "160") and d.get("secCode"):
                    candidates.append(d)
            print(f"{target}: 上場 {len(listed)}件")
            await asyncio.sleep(0.8)

        print("\n=== (A) formCode×docType 分布（2022-11・secCode有り）===")
        for (fc, dt), n in form_type.most_common(15):
            print(f"    form={fc} docType={dt}: {n}件")

        # (B) 候補から secCode の小さい順に走査し、TypeOfCurrentPeriodDEI=Q2 を1件深掘り
        candidates.sort(key=lambda d: d.get("secCode") or "99999")
        print(f"\n候補(docType140/160)={len(candidates)}件。Q2を探索して深掘り...")
        for cand in candidates[:12]:
            df = await fetch_xbrl_csv(client, cand["docID"])
            await asyncio.sleep(0.6)
            if df is None or df.empty:
                continue
            df.columns = [c.strip() for c in df.columns]
            col_map = _detect_xbrl_columns(df)
            elem_col, val_col = col_map.get("element"), col_map.get("value")
            if not elem_col:
                continue
            ptype = dei_of(df, elem_col, val_col, "TypeOfCurrentPeriodDEI")
            pend = dei_of(df, elem_col, val_col, "CurrentPeriodEndDateDEI")
            fyend = dei_of(df, elem_col, val_col, "CurrentFiscalYearEndDateDEI")
            if ptype != "Q2":
                print(f"    skip {cand.get('secCode')} {cand.get('filerName')} type={ptype}")
                continue

            print(f"\n=== (B) 深掘り: {cand.get('filerName')} sec={cand.get('secCode')} "
                  f"form={cand.get('formCode')} docType={cand.get('docTypeCode')} ===")
            print(f"    DEI: TypeOfCurrentPeriod={ptype} PeriodEndDate={pend} FiscalYearEnd={fyend}"
                  f" metaPeriodEnd={cand.get('periodEnd')}")
            # 主要要素のcontext（メンバー無し）サンプル
            ctx_col = col_map.get("context")
            watch = {"NetSales", "Revenue", "OperatingIncome", "Assets",
                     "NetCashProvidedByUsedInOperatingActivities"}
            shown = 0
            for _, row in df.iterrows():
                raw = str(row[elem_col]); e = raw.split(":")[-1] if ":" in raw else raw
                if e in watch and "Member" not in str(row[ctx_col]):
                    print(f"    {e:48s} ctx={row[ctx_col]} val={row[val_col]}")
                    shown += 1
                    if shown >= 12:
                        break
            parsed = parse_xbrl_csv(df, cand.get("edinetCode"), pend or "")
            print(f"    parse: revenue={parsed['pl'].get('revenue')} "
                  f"assets={parsed['bs'].get('total_assets')} ocf={parsed['cf'].get('operating_cf')} "
                  f"| bs非null={sum(1 for v in parsed['bs'].values() if v is not None)} "
                  f"pl非null={sum(1 for v in parsed['pl'].values() if v is not None)} "
                  f"cf非null={sum(1 for v in parsed['cf'].values() if v is not None)}")
            break


if __name__ == "__main__":
    asyncio.run(main())
