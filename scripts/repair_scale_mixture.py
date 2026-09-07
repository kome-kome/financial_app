"""1つの価格列に2つのスケールが混ざった帯を、Yahoo で取り直して均す（#620）。

## 何が起きていたのか

`stock_price_daily.close` には毎晩2人が書いていた。Yahoo gap-fill が直近セッションを、
J-Quants catchup が `today-90 〜 today-80` を。どちらも「調整済み終値」だが、Yahoo は
1:1.2 のような無償割当を splits として持たない（#466）ため、その社では2つの調整が
恒常的に食い違う。結果、catchup が上書きした区間の**両端に企業イベントではない段差**が
できる。11営業日で元へ戻る企業イベントは存在しない＝分割でも併合でもない。

**この壊れ方はエラーを出さない。** どちらの値も妥当な株価で、upsert は成功し、行数も
鮮度も正常に見える。現れるのは週次リターンを入力に持つ M-1 / M-2 / M-6 の中だけ。

## なぜ「Yahoo で取り直す」のか

仕組み側（`collector_prices._jquants_batch_gen`）で **`AdjC != C` の行は書かない**ように
した（#620）。以後 `stock_price_daily` の書き手は実質 Yahoo だけになるので、既に入って
しまった公式スケールの帯を Yahoo スケールへ戻せば系列が一貫する。

**順序を逆にしない。** 仕組みを直す前にデータだけ直すと、次の晩の catchup が同じ場所へ
書き戻す（#568 の「Yahoo で取り直せ」という案内が当たらなかったのと同じ理由）。

## 確認してから直す

**形だけでは本物を選べない。** 全社を走査すると往復の帯を持つ社は 234社あり（2026-09-08
実測・保持窓183日）、そのうち混在は2社だった。残りは実際の値動きで往復しただけである
（#620 の E02293 は `C == AdjC` ＝調整差が無く、6/16 の 808→1108 は本物の値動き）。
帯の長さでも、帯の内側が一定比かでも分離できない——E32779 は帯の中で株価が 13% 動いている。

したがって確定は公式値との突合で行う。`AdjC != C`（その社は Yahoo と調整の中身が違う）
かつ **帯の日の DB 値が `AdjC` と一致する**（帯の正体が公式値そのもの）を両方確かめる。

突合は1社1リクエスト＝`JQUANTS_RATE_SLEEP`（20秒）かかるので、**候補が `--max-verify` を
超えたら突合せずに候補一覧だけ出して止まる**。日常の入口は毎晩のバッチログで、そちらは
「今夜 `AdjC != C` を報告した社」と交差済みの短い一覧を出す。それを `--only` へ渡す。

## 実行

    python -m scripts.repair_scale_mixture                  # ドライラン（既定）
    python -m scripts.repair_scale_mixture --only E32779    # 1社だけ
    python -m scripts.repair_scale_mixture --only E32779,E05716 --apply
    python -m scripts.repair_scale_mixture --skip-verify --only E32779   # 突合を省く
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import text as sqla_text

import database as D
from collector_prices import (
    _jquants_fetch_code, _learn_jquants_coverage,
    detect_roundtrip_scale_bands, fetch_yahoo_history,
)
from collector_utils import (
    JQUANTS_RATE_SLEEP, YAHOO_STOCK_RATE_SLEEP,
    force_utf8_stdout, same_price_scale, yahoo_guard_kwargs, yahoo_ticker,
)


# ── 純関数（ネットワークにも DB にも触らない・ここがテスト対象）─────────────────

def band_dates(bands: list) -> list:
    """帯の一覧から「その社で疑わしい日付の範囲」を `[(start, end)]` で返す。"""
    return [(b["start"], b["end"]) for b in bands]


def confirm_official_scale(official_rows: list, db_closes: dict, bands: list) -> tuple:
    """帯の中身が**公式スケールで書かれている**かを判定する。戻り値 `(ok, reason)`。

    `official_rows`: `_jquants_fetch_code` の戻り（`Date` / `C` / `AdjC` を持つ）。
    `db_closes`: `{trade_date: close}`（DB の日次終値）。

    条件は2つ。両方そろって初めて「この帯は公式値で上書きされた」と言える。

    1. 帯の中に `AdjC != C` の日がある＝**その社は Yahoo と調整の中身が食い違う**
       （食い違わない社なら、公式値で上書きされていても段差は出ない）
    2. その日の DB 値が `AdjC` と一致する＝**帯の正体が公式値そのもの**である

    2 を要求するのが要点で、1 だけでは「調整差のある社が、たまたま実際の値動きで
    往復した」場合と区別がつかない。
    """
    if not official_rows:
        return False, "公式値を1行も取得できなかった（契約窓の外側か銘柄コード不一致）"
    ranges = band_dates(bands)
    hit_adj, hit_match = 0, 0
    for r in official_rows:
        d = str(r.get("Date") or "")[:10]
        c, adjc = r.get("C"), r.get("AdjC")
        if not d or c is None or adjc is None:
            continue
        if not any(lo <= d <= hi for lo, hi in ranges):
            continue
        if same_price_scale(c, adjc):
            continue          # 調整差なし＝この日は誰が書いても同じ値になる
        hit_adj += 1
        dbv = db_closes.get(d)
        if dbv is not None and same_price_scale(dbv, adjc):
            hit_match += 1
    if not hit_adj:
        return False, "帯の期間に AdjC≠C の日が無い＝調整差が無く、往復は実際の値動きの疑い"
    if not hit_match:
        return False, (f"帯に AdjC≠C の日が {hit_adj}件あるが、DB 値が AdjC と一致しない"
                       "＝帯の正体は公式値ではない")
    return True, f"帯の {hit_match}/{hit_adj}日で DB 値が公式 AdjC と一致"


# ── DB / ネットワーク ────────────────────────────────────────────────────────

def load_daily_closes(db, ec: str) -> dict:
    """{trade_date: close}（その社の日次終値・保持窓ぶん）。"""
    return {
        row[0]: float(row[1])
        for row in db.execute(sqla_text(
            "SELECT trade_date, close FROM stock_price_daily "
            "WHERE edinet_code = :ec ORDER BY trade_date"
        ), {"ec": ec}).fetchall()
    }


def load_tickers(db, ecs: list) -> dict:
    """{edinet_code: (sec_code, yahoo_suffix)}。"""
    return {
        c.edinet_code: (c.sec_code, c.yahoo_suffix)
        for c in db.query(D.Company.edinet_code, D.Company.sec_code, D.Company.yahoo_suffix)
        .filter(D.Company.edinet_code.in_(ecs)).all()
    }


async def verify_targets(db, targets: dict, *, on_progress=None) -> dict:
    """候補社を公式値と突合する。{edinet_code: (ok, reason)}。1社1リクエスト。"""
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise RuntimeError("環境変数 JQUANTS_API_KEY が未設定です（--skip-verify で省けます）")
    tickers = load_tickers(db, list(targets))
    out: dict = {}
    async with httpx.AsyncClient(timeout=60) as session:
        cover_from, cover_to = await _learn_jquants_coverage(session, api_key)
        if not cover_from:
            raise RuntimeError("J-Quants のカバレッジ窓を取得できなかった（契約状態を確認）")
        for i, (ec, bands) in enumerate(sorted(targets.items()), 1):
            sec = (tickers.get(ec) or (None, None))[0]
            if not sec:
                out[ec] = (False, "sec_code なし")
                continue
            if i > 1:
                await asyncio.sleep(JQUANTS_RATE_SLEEP)
            rows = await _jquants_fetch_code(session, api_key, f"{sec}0",
                                             cover_from, cover_to)
            out[ec] = confirm_official_scale(rows, load_daily_closes(db, ec), bands)
            if on_progress:
                on_progress(i, len(targets), f"[突合 {i}/{len(targets)}] {ec} {out[ec][1]}")
    return out


async def refetch_from_yahoo(db, ecs: list, *, days: int) -> dict:
    """該当社の直近 `days` 日を Yahoo で取り直して upsert する。{edinet_code: 件数}。

    帯の右端は契約窓（無料プランは直近12週エンバーゴ）に貼り付いて**毎晩伸びる**ので、
    日付を手で固定せず保持窓ぶんをまとめて取り直す。`record_prices_batch` が daily の
    upsert → 触れた週の再集約 → trim までを担う。
    """
    tickers = load_tickers(db, ecs)
    d_to = date.today().strftime("%Y%m%d")
    d_from = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    out: dict = {}
    async with httpx.AsyncClient(timeout=60) as session:
        for i, ec in enumerate(ecs, 1):
            sec, suffix = tickers.get(ec) or (None, None)
            if not sec:
                out[ec] = 0
                continue
            if i > 1 and YAHOO_STOCK_RATE_SLEEP > 0:
                await asyncio.sleep(YAHOO_STOCK_RATE_SLEEP)
            rows = await fetch_yahoo_history(session, yahoo_ticker(sec, suffix),
                                             d_from, d_to, **yahoo_guard_kwargs(suffix))
            recs = [{"edinet_code": ec, "trade_date": r["trade_date"],
                     "close": r["close"], "volume": r.get("volume")}
                    for r in (rows or []) if r.get("close")]
            if not recs:
                out[ec] = 0
                continue
            try:
                D.record_prices_batch(db, recs, trim=True)
                out[ec] = len(recs)
            except Exception as e:
                db.rollback()
                out[ec] = 0
                print(f"  [失敗] {ec}: {type(e).__name__}: {str(e)[:150]}")
    return out


async def _run(args) -> dict:
    db = D.SessionLocal()
    try:
        found = detect_roundtrip_scale_bands(db)
        targets = {c["edinet_code"]: c["bands"] for c in found["companies"]}
        if args.only:
            want = {s.strip() for s in args.only.split(",") if s.strip()}
            # 検知に出ない社を明示指定したら、帯が無いまま取り直す（人が理由を持っている）
            targets = {ec: targets.get(ec, []) for ec in want}
        rep = {
            "scanned_steps": found["steps"],
            "candidates": [
                {"edinet_code": ec, "bands": bands} for ec, bands in sorted(targets.items())
            ],
            "verified": {}, "repaired": {}, "applied": False, "days": args.days,
            "aborted": False,
        }
        if not targets:
            return rep

        if args.skip_verify:
            if not args.only:
                # 突合を省くなら対象は人が名指しする。候補のまま全部取り直すと、
                # 実際の値動きで往復しただけの社まで巻き込む（実測 234社）。
                rep["aborted"] = True
                return rep
            confirmed = sorted(targets)
        elif len(targets) > args.max_verify:
            # 1社1リクエスト × JQUANTS_RATE_SLEEP。多すぎる＝入口を間違えている合図なので、
            # 黙って80分待たせずに候補だけ出して止まる。
            rep["aborted"] = True
            return rep
        else:
            verdicts = await verify_targets(
                db, targets,
                on_progress=lambda i, n, m: print(f"  {m}"))
            rep["verified"] = {ec: {"ok": ok, "reason": why}
                               for ec, (ok, why) in verdicts.items()}
            confirmed = sorted(ec for ec, (ok, _) in verdicts.items() if ok)
        rep["confirmed"] = confirmed

        if not args.apply or not confirmed:
            return rep

        rep["repaired"] = await refetch_from_yahoo(db, confirmed, days=args.days)
        db.commit()
        rep["applied"] = True

        # 週次価格キャッシュの世代印を進める（#480・ADR-0036）。**行数も max(week_start) も
        # 変わらないのに過去の値だけが変わる**ため、差分ロードの指紋では検出できない。
        if any(rep["repaired"].values()):
            import weekly_price_cache
            weekly_price_cache.bump_generation_safely(
                db, f"repair-scale-mixture: {len(confirmed)} companies")
        return rep
    finally:
        db.close()


def print_report(rep: dict, applied: bool) -> None:
    print(f"往復段差の候補: {len(rep['candidates'])}社"
          f"（段差 {rep['scanned_steps']}件を検査・{rep['days']}日を取り直す設定）")
    for c in rep["candidates"][:50]:
        v = (rep.get("verified") or {}).get(c["edinet_code"])
        mark = "" if v is None else ("[確定] " if v["ok"] else "[棄却] ")
        print(f"  {mark}{c['edinet_code']}")
        for b in c["bands"]:
            print(f"      帯 {b['start']} 〜 {b['end']}（{b['days']}日）"
                  f"・比 {b['ratio_out']:.4f} → {b['ratio_back']:.4f}"
                  f"・往復後 {b['ratio_out'] * b['ratio_back']:.4f}")
        if v is not None:
            print(f"      {v['reason']}")
    if len(rep["candidates"]) > 50:
        print(f"  … ほか {len(rep['candidates']) - 50}社")
    if rep.get("aborted"):
        print("公式値との突合をしていません＝どれが本物かまだ分かりません。"
              "毎晩のバッチログが出す社を --only へ渡すか、--max-verify を上げてください"
              "（1社あたり20秒かかります）")
        return
    if not applied:
        print("ドライラン（書き込みなし）。--apply で Yahoo から取り直します")
        return
    ok = sum(1 for n in rep["repaired"].values() if n)
    print(f"取り直し: {ok}/{len(rep['repaired'])}社"
          f"（{sum(rep['repaired'].values())}行 upsert）")


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="1つの価格列に2つのスケールが混ざった帯を Yahoo で取り直して均す（#620）")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む（既定はドライラン）")
    ap.add_argument("--only", help="edinet_code をカンマ区切りで指定")
    ap.add_argument("--skip-verify", action="store_true",
                    help="公式値との突合を省く（--only と併用必須）")
    ap.add_argument("--max-verify", type=int, default=30,
                    help="突合する社数の上限（既定30・1社20秒）。超えたら候補一覧だけ出す")
    ap.add_argument("--days", type=int, default=D.DAILY_WINDOW_DAYS,
                    help=f"取り直す期間（日・既定 {D.DAILY_WINDOW_DAYS}＝daily の保持窓）")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args()

    rep = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(rep, rep.get("applied", False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
