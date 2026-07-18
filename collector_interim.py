"""Issue #219② フェーズB: 半期(H1)財務の収集。

EDINET の半期報告書(新式 formCode=043A00 / docType=160)および旧四半期報告書(formCode=043000 /
docType=140)のうち Q2(中間=H1累計)を収集し、`financial_records` に `period_type='H1'` として
保存する。既存の通期(annual)収集パス(`collector_financials._phase_process_docs`)とは独立に動作し、
低レベルヘルパ(`fetch_xbrl_csv`/`parse_xbrl_csv`/`calc_derived`/`upsert_financial`)を再利用する。

de-risk 実証で確認した前提(検証スクリプトは Issue #219② 完了後に削除・git 履歴参照):
  - 新旧いずれも P/L・CF は context `CurrentYTDDuration`(=H1累計)、BS は `CurrentQuarterInstant` で、
    既存 `parse_xbrl_csv` が無改修で H1 累計を抽出できる(前期比較 `Prior1*` は既存フィルタが skip)。
  - 真の H1 期末は DEI 要素 `CurrentPeriodEndDateDEI`。metadata の periodEnd は新式では
    会計年度末を返し不正確なため使わない。
  - H1 判定は DEI `TypeOfCurrentPeriodDEI == 'Q2'`(新式の半期報告書も自己申告は Q2)。
    旧四半期の Q1/Q3 はここで除外する。
"""
import asyncio
from collections import Counter
from datetime import date, timedelta
from typing import Callable, Optional

import httpx

from collector_utils import (
    API_KEY, BATCH_PAUSE, COLLECT_COMMIT_BATCH, COLLECT_SLEEP_BATCH,
    EDINET_BASE, RATE_SLEEP, log,
)
from collector_financials import (
    _col_as_str_list, _detect_xbrl_columns, calc_derived, fetch_xbrl_csv, parse_xbrl_csv,
)
from database import Company, FinancialRecord, upsert_company, upsert_financial

# 半期(H1)を含む書類種別。160=半期報告書(新式043A00/旧式050000)、140=旧四半期報告書。
INTERIM_DOC_TYPES = {"140", "160"}
# H1(中間)と判定する DEI の当期種別。新式半期報告書も自己申告は Q2。
H1_PERIOD_TYPE_DEI = "Q2"
# financial_records.period_type に格納する半期ラベル。
INTERIM_PERIOD_TYPE = "H1"

# 抽出する DEI 要素(接頭辞除去後の要素名)。
_DEI_TYPE  = "TypeOfCurrentPeriodDEI"
_DEI_PEND  = "CurrentPeriodEndDateDEI"
_DEI_FYEND = "CurrentFiscalYearEndDateDEI"


async def fetch_interim_doc_list(client: httpx.AsyncClient, target_date: date) -> list:
    """documents.json から docType∈{140,160}・secCode 有りの書類を返す(半期候補)。

    通期用 `fetch_doc_list`(formCode=030000 固定)とは別に、四半期/半期の書類種別で絞る。
    Q1/Q3 の除外は DEI を見るまで確定しないため、ここでは docType でのみ粗く絞る。
    """
    url = f"{EDINET_BASE}/documents.json"
    params = {"date": target_date.isoformat(), "type": 2, "Subscription-Key": API_KEY}
    try:
        r = await client.get(url, params=params, timeout=30)
        r.raise_for_status()
        results = r.json().get("results") or []
        return [d for d in results
                if d.get("ordinanceCode") == "010"
                and d.get("docTypeCode") in INTERIM_DOC_TYPES
                and d.get("secCode")]
    except Exception as e:
        log.warning(f"半期書類一覧取得失敗 {target_date}: {e}")
        return []


def _extract_dei(df) -> dict:
    """XBRL df から DEI 期間メタ(種別・当期末日・会計年度末日)を1パスで抽出する。"""
    col_map = _detect_xbrl_columns(df)
    if not {"element", "value"}.issubset(col_map):
        return {}
    elements = _col_as_str_list(df, col_map["element"])
    values   = _col_as_str_list(df, col_map["value"])
    want = {_DEI_TYPE, _DEI_PEND, _DEI_FYEND}
    out: dict = {}
    for raw_elem, val in zip(elements, values):
        elem = raw_elem.split(":")[-1] if ":" in raw_elem else raw_elem
        if elem in want and elem not in out:
            out[elem] = val.strip()
            if len(out) == len(want):
                break
    return out


def build_fy_end_month_map(db) -> dict:
    """既存の通期行から企業ごとの会計年度末「月」を推定する {edinet_code: month}。

    旧四半期(docType140)の候補を metadata の periodEnd 月で事前選別(Q2のみに絞る)する
    ためのヒント。各企業の最頻の annual period_end 月を採用する。年度末が不明な企業は
    map に載らず、事前選別をスキップして DEI 判定に委ねる(取りこぼし防止)。
    """
    rows = (db.query(FinancialRecord.edinet_code, FinancialRecord.period_end)
              .filter(FinancialRecord.period_type == "annual",
                      FinancialRecord.period_end.isnot(None))
              .all())
    by_code: dict = {}
    for ec, pe in rows:
        if pe is None:
            continue
        by_code.setdefault(ec, Counter())[pe.month] += 1
    return {ec: cnt.most_common(1)[0][0] for ec, cnt in by_code.items()}


def _h1_month(fy_end_month: int) -> int:
    """会計年度末の月から H1(中間)期末の月を求める(年度末の6か月前)。3月末→9月。"""
    return ((fy_end_month - 6 - 1) % 12) + 1


def prefilter_interim_docs(docs: list, fy_end_month_map: dict) -> list:
    """半期候補を metadata だけで事前選別する(ダウンロード数の削減)。

    - docType=160(半期報告書): 年1回=常に H1 → そのまま候補。
    - docType=140(旧四半期): 年3回(Q1/Q2/Q3)。metadata の periodEnd は中間期末を指すため、
      その月が企業の H1 期末月と一致するものだけ Q2 候補として残す。年度末不明企業は
      選別できないので候補に残し DEI 判定に委ねる。
    """
    out = []
    for d in docs:
        if d.get("docTypeCode") == "160":
            out.append(d)
            continue
        ec = d.get("edinetCode")
        fym = fy_end_month_map.get(ec)
        if fym is None:
            out.append(d)   # 年度末不明 → DEI 判定に委ねる
            continue
        pe = d.get("periodEnd") or ""
        try:
            pe_month = int(pe[5:7])
        except (ValueError, IndexError):
            out.append(d)
            continue
        if pe_month == _h1_month(fym):
            out.append(d)
    return out


async def collect_interim_docs_for_period(
    client, start: date, end: date, fy_end_month_map: dict,
    on_progress: Optional[Callable] = None,
) -> list:
    """指定期間を日次スキャンし、半期候補書類(事前選別済み)を返す。"""
    docs = []
    seen_codes: set = set()
    total_days = (end - start).days + 1
    cur = start
    day_idx = 0
    while cur <= end:
        day_idx += 1
        if on_progress:
            on_progress(day_idx, total_days,
                        f"[半期書類スキャン {day_idx}/{total_days}日] {cur} 累計{len(seen_codes)}社")
        daily = await fetch_interim_doc_list(client, cur)
        kept = prefilter_interim_docs(daily, fy_end_month_map)
        for d in kept:
            seen_codes.add(d.get("edinetCode"))
        docs.extend(kept)
        if kept:
            log.info(f"{cur} -> 半期候補{len(kept)}件(累計{len(seen_codes)}社)")
        await asyncio.sleep(RATE_SLEEP)
        cur += timedelta(days=1)
    return docs


def _safe_rollback(db) -> None:
    try:
        db.rollback()
    except Exception as e:
        log.error(f"rollback失敗: {e}")


async def process_interim_docs(db, client, docs: list,
                               on_progress: Optional[Callable] = None,
                               cancel_check: Optional[Callable] = None,
                               skip_existing_doc_ids: Optional[set] = None,
                               known_edinet: Optional[set] = None) -> dict:
    """半期候補を1件ずつ処理: XBRL取得→DEI判定(Q2のみ)→parse→upsert(period_type='H1')。

    通期の `_phase_process_docs` と同様のフェイルソフト方針(個社失敗でも継続)。
    戻り値は集計 {saved, skipped_notq2, skipped_existing, failed}。
    known_edinet に無い企業は FK(companies)を満たすため最小情報で upsert_company する。
    """
    total = len(docs)
    stat = Counter()
    skip_ids = skip_existing_doc_ids or set()
    known = known_edinet if known_edinet is not None else set()
    for i, doc in enumerate(docs):
        if cancel_check and cancel_check():
            db.commit()
            log.info(f"半期収集をキャンセル({i}/{total}件処理済み)")
            break

        doc_id      = doc["docID"]
        edinet_code = doc["edinetCode"]
        sec_code    = (doc.get("secCode") or "")[:4]
        filer_name  = doc.get("filerName") or ""
        filing_date = (doc.get("submitDateTime") or "")[:10]

        if doc_id in skip_ids:
            stat["skipped_existing"] += 1
            continue

        if on_progress:
            on_progress(i + 1, total, f"[半期 {i+1}/{total}] {filer_name}({sec_code})")

        try:
            xbrl_df = await fetch_xbrl_csv(client, doc_id)
            await asyncio.sleep(RATE_SLEEP)
            if xbrl_df is None or xbrl_df.empty:
                stat["failed"] += 1
                continue

            dei = _extract_dei(xbrl_df)
            # H1(中間=Q2)以外は除外(旧四半期の Q1/Q3)。
            if dei.get(_DEI_TYPE) != H1_PERIOD_TYPE_DEI:
                stat["skipped_notq2"] += 1
                continue

            period_end = (dei.get(_DEI_PEND) or "")[:10]        # 真の H1 期末(DEI)
            fy_end     = (dei.get(_DEI_FYEND) or "")[:10]
            if not period_end or not fy_end:
                stat["failed"] += 1
                continue
            year = int(fy_end[:4])   # 同一会計年度の通期行と同じ year でグルーピング

            raw = parse_xbrl_csv(xbrl_df, edinet_code, period_end)
            if not any(raw.get(cat) for cat in ("bs", "pl", "cf")):
                stat["failed"] += 1
                continue
            rec = calc_derived(raw)

            # FK(companies)を満たすため未知企業を最小情報で登録(通期パスと同じ扱い)。
            if edinet_code not in known:
                upsert_company(db, {
                    "edinet_code": edinet_code, "sec_code": sec_code,
                    "name": filer_name, "industry": "",
                })
                db.flush()
                known.add(edinet_code)

            xbrl_industry = rec.get("meta", {}).get("industry_name", "")
            rec.update({
                "edinet_code":  edinet_code,
                "sec_code":     sec_code,
                "company_name": filer_name,
                "industry":     xbrl_industry,
                "year":         year,
                "period_end":   period_end,
                "period_type":  INTERIM_PERIOD_TYPE,
                "filing_date":  filing_date,
                "doc_id":       doc_id,
                "source":       "EDINET_XBRL_H1",
            })
            upsert_financial(db, rec)
            stat["saved"] += 1

            if stat["saved"] % COLLECT_COMMIT_BATCH == 0:
                db.commit()
            if (i + 1) % COLLECT_SLEEP_BATCH == 0:
                await asyncio.sleep(BATCH_PAUSE)
        except httpx.HTTPError as e:
            log.error(f"[半期取得失敗] {edinet_code}/{doc_id} {filer_name}: {e.__class__.__name__}: {e}")
            _safe_rollback(db)
            stat["failed"] += 1
        except Exception as e:
            log.error(f"[半期処理失敗] {edinet_code}/{doc_id} {filer_name}: {e.__class__.__name__}: {e}",
                      exc_info=True)
            _safe_rollback(db)
            stat["failed"] += 1

    db.commit()
    return dict(stat)


async def run_interim_collection(db,
                                 years_back: int = 6,
                                 skip_existing: bool = True,
                                 on_progress: Optional[Callable] = None,
                                 cancel_check: Optional[Callable] = None) -> dict:
    """半期(H1)収集オーケストレータ。既存通期窓に揃えて過去 years_back 年を対象にする。

    skip_existing=True のとき、収集済み doc_id を再取得しない(差分収集)。
    """
    async with httpx.AsyncClient(timeout=60) as client:
        fy_end_month_map = build_fy_end_month_map(db)
        log.info(f"会計年度末マップ: {len(fy_end_month_map)}社")
        known_edinet = {r[0] for r in db.query(Company.edinet_code).all()}

        existing_ids: set = set()
        if skip_existing:
            rows = (db.query(FinancialRecord.doc_id)
                      .filter(FinancialRecord.period_type == INTERIM_PERIOD_TYPE,
                              FinancialRecord.doc_id.isnot(None))
                      .all())
            existing_ids = {r[0] for r in rows}
            log.info(f"半期差分モード: 収集済み {len(existing_ids)} 件をスキップ")

        today = date.today()
        start = date(today.year - years_back, 1, 1)
        end   = today - timedelta(days=1)
        log.info(f"半期書類スキャン: {start} ~ {end}")

        docs = await collect_interim_docs_for_period(
            client, start, end, fy_end_month_map, on_progress=on_progress)
        log.info(f"半期候補書類: {len(docs)}件")

        stat = await process_interim_docs(
            db, client, docs, on_progress=on_progress, cancel_check=cancel_check,
            skip_existing_doc_ids=existing_ids, known_edinet=known_edinet)
        log.info(f"半期収集完了: {stat}")
        return stat
