"""Yahoo が遡及反映しない分割で残る週次段差を、JPX 公式の裏付けを取ってから直す（#466）。

## なぜ Yahoo 経路（`--repair-price-breaks`）では直らないのか

**修復ソースである Yahoo 自身が公式と食い違う。** Yahoo は `0.909091`（1:1.1）のような
**無償割当を splits イベントとして持たず**、価格も遡及調整しない。何度取り直しても同じ値に
なるので、`repair_price_scale_breaks`（Yahoo 全履歴取り直し）はこの銘柄群に無効である。
J-Quants（JPX 公式）の `AdjC` が正しい側。

## なぜ「公式値で置き換える」だけでは駄目なのか

**公式値は週次履歴の 32% にしか存在しない。** 実測（2026-08-29）:

    stock_price_weekly  2019-07-29 〜 2026-08-24
    J-Quants 契約窓     2024-05-29 〜 2026-05-29
    対象銘柄の週次      各 370週。うち 253週（68%）が窓より前

乖離は分割日より前の全期間に及ぶので、窓内 117週だけを公式値へ置き換えると
**2024-05-29 の位置に新しい段差ができる**＝現状より悪化する。したがって
「窓内で測った補正比を、窓外へも延長して掛ける」形にする。

## 何を源にし、何を検算に使うのか

| 役割 | 使うもの |
|---|---|
| **補正比の値** | 窓内で実測した `AdjC / close_last`（**推測しない**） |
| **補正してよいかの判定** | 公式 `AdjFactor`（`!= 1.0` の日が JPX 公式の企業イベント日） |

**比の段差は、すべて公式イベント日で説明できなければならない。** 説明できない段差を
直すのは、分割の無い銘柄にニセの分割を作る行為であり、週次リターンを入力に持つ
M-1 / M-2 / M-6 へ誤った企業イベントを伝播させる。#466 の調査で、公式イベントが
無いのに 5/6 ずれている銘柄（E32779）と、時期も比率も公式と食い違う銘柄（E02086）が
実在すると分かっている。

**逆向き（公式イベントがあるのに段差が無い）は正常。** Yahoo が既に知っている分割は
DB 側で調整済みなので比が動かない（E02978 の 2024-07-30 が該当）。したがって
「全イベントに段差が対応すること」は要求してはいけない——要求すると、
正しく調整済みの銘柄を弾く。

## 「直せない」には2種類ある（2026-08-29 の実測で判明・#466 本文には無い区別）

検出17社のうち9社を補正、8社を棄却したが、**棄却8社のうち6社は #466 とは別の現象**だった。

    E03178 9900 の実測: J-Quants は窓全体で AdjC = C/2 なのに AdjFactor は全日 1.0
    DB の週次:  05-22=1717 → 05-29=836 → 06-05=764.5 → 06-12=1620 … 08-21=2169 → 08-28=1135

公式が窓内の `AdjC` を遡及調整済みで返すのにイベント行が無いのは、**分割が J-Quants
無料プランのエンバーゴ（直近12週）の中で起きている**ため。普通の分割なら Yahoo も遡及
調整するので、これは「Yahoo が無償割当を splits として持たない」という #466 の現象ではなく、
**既存の Yahoo 経路（`--repair-price-breaks --persist`）の担当**である。`post_window_adjustment`
がこれを `AdjC / C != 1.0` で見分け、`embargoed` として別枠に出す。

**「直せなかった」を1つの箱に入れると打ち手を取り違える**——エンバーゴ群は12週待つか
Yahoo 経路で直り、真に説明できない群（E32779・E02086）は第3のソースが要る。

## 触らないもの

`volume_sum` / `turnover_sum` は**補正しない**（#466 のスコープ外）。分割では株数が変わる
ので出来高も逆比で動くが、この2列は `px_volz` 経由で macro 系プラグイン5本の特徴量に
入っており、動かすと昇格ゲート（rank-IC）の再測定が要る。`turnover_sum` の内部整合は
現状も崩れている（分割未調整）ので、触らないことで悪化はしない。

## 窓外へ延長する仮定（レポートに必ず出す）

窓の最古測定日より前の週には、その最古測定日の比をそのまま掛ける。これは
**「履歴の開始から窓の最古測定日までの間に、Yahoo が取り落とした企業イベントが無い」**
という仮定である。窓外のイベントは `AdjFactor` でも Yahoo splits でも取得できないため
検証手段が無い（EDINET 等の第3のソースが要る＝#466 のスコープ外）。

## 実行

    python -m scripts.repair_splits_from_jquants                    # ドライラン（既定）
    python -m scripts.repair_splits_from_jquants --only E03137      # 1社だけ
    python -m scripts.repair_splits_from_jquants --apply
    python -m scripts.repair_splits_from_jquants --json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import text as sqla_text

import database as D
from collector_prices import (
    _jquants_fetch_code, _learn_jquants_coverage, detect_price_scale_breaks,
)
from collector_utils import JQUANTS_RATE_SLEEP

# 丸め由来の許容相対誤差の下限。株価が高いほどこちらが効く。
REL_TOL_FLOOR = 2.0e-3
# 丸め幅（円）。低位株では `ROUND_UNIT / 株価` がそのまま許容誤差になる。
# #466 の実測では E01300（株価 21円）の観測幅が 3.5e-3 に対し、この式だと 4.8e-2。
ROUND_UNIT = 1.0
# `AdjFactor` がイベントとみなされる下限（浮動小数の 1.0 ゆらぎを拾わない）。
EVENT_EPS = 1.0e-6

STAMP_KEY = "splits_repaired_from_jquants"


# ── 純関数（ネットワークにも DB にも触らない・ここがテスト対象）─────────────────

def rounding_tolerance(a: float, b: float) -> float:
    """2つの終値を比べるときの許容相対誤差。低位株ほど大きく取る。"""
    base = min(abs(a), abs(b))
    if base <= 0:
        return REL_TOL_FLOOR
    return max(REL_TOL_FLOOR, ROUND_UNIT / base)


def extract_events(rows: list) -> list:
    """J-Quants の日次バーから公式の企業イベント [(date, factor)] を取り出す。"""
    out = []
    for r in rows:
        f = r.get("AdjFactor")
        d = r.get("Date")
        if f is None or not d:
            continue
        f = float(f)
        if abs(f - 1.0) > EVENT_EPS:
            out.append((str(d)[:10], f))
    return sorted(out)


def measured_ratios(rows: list, weekly: dict) -> list:
    """weekly の trade_date と一致する日で `AdjC / close_last` を測る。

    `weekly`: {trade_date: close_last}。戻り値は日付昇順の [(date, ratio, db, official)]。
    **比は推測ではなく実測**である（この関数が #466 の「株価比は検算に使う」の実体）。
    """
    out = []
    for r in rows:
        d = str(r.get("Date") or "")[:10]
        adjc = r.get("AdjC")
        dbv = weekly.get(d)
        if not d or adjc is None or dbv is None:
            continue
        dbv, adjc = float(dbv), float(adjc)
        if dbv <= 0 or adjc <= 0:
            continue
        out.append((d, adjc / dbv, dbv, adjc))
    return sorted(out)


def find_steps(ratios: list) -> list:
    """比の段差 [(前日, 翌日, 前比, 後比)] を返す。丸め許容を超えた変化だけ拾う。"""
    steps = []
    for (d0, r0, db0, of0), (d1, r1, db1, of1) in zip(ratios, ratios[1:]):
        tol = max(rounding_tolerance(db0, of0), rounding_tolerance(db1, of1))
        if abs(r1 - r0) > tol * max(r0, r1):
            steps.append((d0, d1, r0, r1))
    return steps


def post_window_adjustment(rows: list) -> Optional[float]:
    """窓の最終日で `AdjC / C` を返す。1.0 から外れていれば**窓より後の企業イベント**の証拠。

    J-Quants 無料プランは直近12週をエンバーゴするので、**その期間に起きた分割は
    `AdjFactor` の行そのものが取得できない**。しかし公式は窓内の `AdjC` を遡及調整済みで
    返すため、`AdjC / C != 1.0` が「窓の外で調整が起きた」ことを公式に示す。

    この群は **#466 が対象とする現象（Yahoo が無償割当を splits として持たない）とは別物**で、
    普通の分割なら Yahoo も遡及調整するので既存の Yahoo 経路
    （`collector.py --repair-price-breaks --persist`）で直る。ここでは触らず、理由を分けて出す。
    """
    if not rows:
        return None
    last = rows[-1]
    c, adjc = last.get("C"), last.get("AdjC")
    if c is None or adjc is None:
        return None
    c, adjc = float(c), float(adjc)
    if c <= 0 or adjc <= 0:
        return None
    r = adjc / c
    return r if abs(r - 1.0) > rounding_tolerance(c, adjc) else None


def validate(ratios: list, events: list) -> tuple:
    """補正してよいかを判定する。戻り値 (ok, reason)。

    条件は2つだけ:

    1. **最新の測定比が 1.0 であること。** 直近で公式と食い違うのは「過去の分割が
       未反映」ではなく別の原因（E32779: 公式イベント none なのに 5/6 ずれ）。
    2. **比の段差がすべて公式イベント日で説明できること。** 説明できない段差を直すのは
       ニセの分割を作る行為（E02086: 段差は 2024-10 頃なのに公式イベントは 2026-03-30）。

    **「全イベントに段差が対応すること」は要求しない。** Yahoo が既に知っている分割は
    DB 側で調整済みなので段差が出ないのが正しい（E02978 の 2024-07-30）。
    """
    if not ratios:
        return False, "窓内に比較できる日が1つも無い"

    d_last, r_last, db_last, of_last = ratios[-1]
    tol_last = rounding_tolerance(db_last, of_last)
    if abs(r_last - 1.0) > tol_last:
        return False, (f"最新 {d_last} の比が {r_last:.6f}（許容 {tol_last:.2e}）＝"
                       "直近で公式と一致しない。過去の分割の未反映では説明できない")

    ev_dates = [d for d, _ in events]
    for d0, d1, r0, r1 in find_steps(ratios):
        hit = [d for d in ev_dates if d0 < d <= d1]
        if not hit:
            return False, (f"{d0}→{d1} で比が {r0:.6f}→{r1:.6f} と動くが、"
                           "この区間に公式イベントが無い")
    return True, ""


def plan_corrections(ratios: list, weekly_dates: list) -> dict:
    """{trade_date: 掛ける係数} を返す。

    窓内は実測比をそのまま使い、測定の無い日は**次に測定のある日**の比を使う
    （比は事象日でのみ変わる階段関数なので、事象を挟まない限り等しい）。
    最古の測定日より前は、その最古の比を延長する（docstring の仮定）。
    """
    if not ratios:
        return {}
    meas = [(d, r) for d, r, _, _ in ratios]
    out: dict = {}
    i = 0
    for d in sorted(weekly_dates):
        while i < len(meas) and meas[i][0] < d:
            i += 1
        if i < len(meas) and meas[i][0] == d:
            factor = meas[i][1]
        elif i < len(meas):
            factor = meas[i][1]          # 次に測定のある日の比
        else:
            factor = meas[-1][1]         # 最新測定より後（＝1.0 のはず）
        out[d] = factor
    return out


def extension_span(ratios: list, weekly_dates: list) -> int:
    """窓の最古測定日より前にある週の数（＝仮定に依存する行数）。"""
    if not ratios:
        return 0
    oldest = ratios[0][0]
    return sum(1 for d in weekly_dates if d < oldest)


# ── DB / ネットワーク ───────────────────────────────────────────────────────

def weekly_rows(db, ec: str) -> list:
    """(trade_date, week_start, close_last) を日付昇順で返す。"""
    return [
        (str(r[0])[:10], str(r[1])[:10], float(r[2]))
        for r in db.execute(sqla_text(
            "SELECT trade_date, week_start, close_last FROM stock_price_weekly "
            "WHERE edinet_code = :ec AND close_last IS NOT NULL ORDER BY week_start"
        ), {"ec": ec}).fetchall()
    ]


def apply_corrections(db, ec: str, rows: list, factors: dict) -> int:
    """weekly.close_last へ係数を掛ける。**生 SQL で week_start 単位に UPDATE する。**

    `record_prices_batch` は通さない（未 trim の daily が積み上がる・#465 の容量理由）。
    """
    n = 0
    for trade_date, week_start, close in rows:
        f = factors.get(trade_date)
        if f is None or abs(f - 1.0) <= REL_TOL_FLOOR:
            continue
        db.execute(sqla_text(
            "UPDATE stock_price_weekly SET close_last = :v "
            "WHERE edinet_code = :ec AND week_start = :ws"
        ), {"v": close * f, "ec": ec, "ws": week_start})
        n += 1
    return n


async def collect_official(targets: list, cover: tuple, *, on_progress=None) -> dict:
    """{edinet_code: J-Quants 日次バー} を銘柄単位で取る（1社1リクエスト）。"""
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        raise RuntimeError("環境変数 JQUANTS_API_KEY が未設定です")
    d_from, d_to = cover
    out: dict = {}
    async with httpx.AsyncClient(timeout=60) as session:
        for i, (ec, sec) in enumerate(targets, 1):
            if i > 1:
                await asyncio.sleep(JQUANTS_RATE_SLEEP)
            rows = await _jquants_fetch_code(session, api_key, f"{sec}0", d_from, d_to)
            out[ec] = rows
            if on_progress:
                on_progress(i, len(targets), f"[取得 {i}/{len(targets)}] {ec} {sec} {len(rows)}行")
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────

def _force_utf8_stdout() -> None:
    """cp932 コンソールへリダイレクトすると非 ASCII は出力済みの内容ごとクラッシュする。
    `main()` からだけ呼ぶ（import 時に差し替えると pytest のキャプチャが壊れる）。"""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


async def _run(args) -> dict:
    db = D.SessionLocal()
    try:
        if D.DB_TARGET != "local" or not D._is_local:
            raise SystemExit(f"接続先が local ではありません（{D.DB_TARGET!r}）。"
                             "このスクリプトはローカル正本専用です（ADR-0038）。")
        stamp = D.get_setting(db, STAMP_KEY)
        if stamp and args.apply and not args.force:
            raise SystemExit(f"既に適用済みです（app_settings.{STAMP_KEY}）:\n  {stamp}\n"
                             "  2度掛けると係数が二乗されます。やり直すなら --force。")

        api_key = os.environ.get("JQUANTS_API_KEY", "")
        if not api_key:
            raise SystemExit("環境変数 JQUANTS_API_KEY が未設定です")
        async with httpx.AsyncClient(timeout=60) as s:
            cover = await _learn_jquants_coverage(s, api_key)
        if not cover[0]:
            raise SystemExit("J-Quants のカバレッジ窓を取得できませんでした")
        print(f"契約窓: {cover[0]} 〜 {cover[1]}")

        # 対象の決定。--only が無ければ既存の検出器（#465）に任せる＝13社を書き写さない。
        sec_of = dict(db.execute(sqla_text(
            "SELECT edinet_code, sec_code FROM companies "
            "WHERE sec_code IS NOT NULL AND sec_code <> ''")).fetchall())
        if args.only:
            ecs = [e.strip() for e in args.only.split(",") if e.strip()]
        else:
            print(f"段差の検出中（既存 detect_price_scale_breaks・"
                  f"約{25 * JQUANTS_RATE_SLEEP / 60:.0f}分）...")
            found = await detect_price_scale_breaks(
                db, api_key, on_progress=lambda i, t, m: print(f"  {m}", flush=True))
            ecs = [b["edinet_code"] for b in found["breaks"]]
            print(f"段差 {len(ecs)}社を検出（突合 {found['compared']}件）")
        targets = [(ec, sec_of[ec]) for ec in ecs if sec_of.get(ec)]
        if not targets:
            print("対象なし。")
            return {"targets": 0}

        print(f"公式イベントを取得（{len(targets)}社 × 1リクエスト・"
              f"約{len(targets) * JQUANTS_RATE_SLEEP / 60:.1f}分）...")
        official = await collect_official(
            targets, cover, on_progress=lambda i, t, m: print(f"  {m}", flush=True))

        report = {"cover": list(cover), "fixed": [], "skipped": [],
                  "embargoed": [], "clean": []}
        for ec, sec in targets:
            rows = weekly_rows(db, ec)
            wk = {td: cl for td, _, cl in rows}
            jq = official.get(ec) or []
            events = extract_events(jq)
            ratios = measured_ratios(jq, wk)
            ok, reason = validate(ratios, events)
            entry = {"edinet_code": ec, "sec_code": sec, "weeks": len(rows),
                     "events": [{"date": d, "factor": f} for d, f in events],
                     "measured": len(ratios),
                     "steps": [{"from": a, "to": b, "before": r0, "after": r1}
                               for a, b, r0, r1 in find_steps(ratios)]}
            if not ok:
                # 棄却の理由を2群に分ける。**「直せない」の中身が違うと打ち手も違う。**
                post = post_window_adjustment(jq)
                if post is not None:
                    entry["post_window_factor"] = post
                    report["embargoed"].append({**entry, "reason": reason})
                else:
                    report["skipped"].append({**entry, "reason": reason})
                continue
            factors = plan_corrections(ratios, [td for td, _, _ in rows])
            n_change = sum(1 for f in factors.values() if abs(f - 1.0) > REL_TOL_FLOOR)
            entry["would_change"] = n_change
            entry["extended"] = extension_span(ratios, [td for td, _, _ in rows])
            if not n_change:
                report["clean"].append(entry)
                continue
            if args.apply:
                entry["updated"] = apply_corrections(db, ec, rows, factors)
            report["fixed"].append(entry)

        if args.apply and report["fixed"]:
            # **過去週を書き換えたので世代印を進める**（#480・ADR-0036）。進めないと
            # 差分ロードのキャッシュが旧値を返し続ける——指紋（max(week_start)＋count(*)）は
            # 「値だけの訂正」を原理的に見られない。
            import weekly_price_cache
            weekly_price_cache.bump_generation(db, f"#466 split repair ({len(report['fixed'])}社)")
            D.upsert_setting(db, STAMP_KEY, json.dumps({
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "issue": 466, "cover": list(cover),
                "fixed": {e["edinet_code"]: e.get("updated", 0) for e in report["fixed"]},
            }, ensure_ascii=False))
            db.commit()
        return report
    finally:
        db.close()


def print_report(rep: dict, applied: bool) -> None:
    verb = "更新" if applied else "更新予定"
    print(f"\n=== 補正対象 {len(rep['fixed'])}社 ===")
    for e in rep["fixed"]:
        ev = " / ".join(f"{d} x{f:g}" for d, f in
                        [(x["date"], x["factor"]) for x in e["events"]]) or "（窓内イベント無し）"
        print(f"  {e['edinet_code']} {e['sec_code']}: {verb} {e.get('updated', e['would_change'])}週"
              f" / 全{e['weeks']}週・実測 {e['measured']}日")
        print(f"      公式イベント: {ev}")
        print(f"      段差 {len(e['steps'])}件・窓外へ延長した週 {e['extended']}"
              f"（仮定: 履歴開始〜窓最古の間に取り落としイベントが無い）")

    if rep["clean"]:
        print(f"\n=== 乖離なし {len(rep['clean'])}社（触らない）===")
        for e in rep["clean"]:
            print(f"  {e['edinet_code']} {e['sec_code']}")

    if rep.get("embargoed"):
        print(f"\n=== この Issue の対象外 {len(rep['embargoed'])}社（別の現象）===")
        print("  公式は窓内の AdjC を遡及調整済みで返すのに AdjFactor の行が無い")
        print("  ＝**分割が J-Quants 無料プランのエンバーゴ（直近12週）の中で起きている**。")
        print("  普通の分割なら Yahoo も遡及調整するので、これは #466 が対象とする")
        print("  「Yahoo が無償割当を splits として持たない」現象ではない。")
        print("  → 既存の Yahoo 経路 `collector.py --repair-price-breaks --persist` の担当。")
        for e in rep["embargoed"]:
            print(f"  {e['edinet_code']} {e['sec_code']}: 窓外調整 x{e['post_window_factor']:.6g}"
                  f" / {e['reason']}")

    if rep["skipped"]:
        print(f"\n=== 修正しないと決めた {len(rep['skipped'])}社 ===")
        print("  （「直せなかった」ではない。公式イベントで説明できない乖離を直すのは")
        print("    分割の無い銘柄にニセの分割を作る行為なので、意図的に残している）")
        for e in rep["skipped"]:
            print(f"  {e['edinet_code']} {e['sec_code']}: {e['reason']}")

    print("\n※ この後の検出（--repair-price-breaks の dry-run）は"
          "上記「修正しないと決めた」ぶんが残るためゼロにはならない。")
    if applied:
        print("※ 週次キャッシュの世代印を進めた。scripts/.cache/weekly_prices_*.pkl の退避と")
        print("   update_market_data_from_history(point_in_time=True) をセットで回すこと（#465）。")
    else:
        print("（ドライラン。1バイトも書いていない）")


def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Yahoo が遡及反映しない分割で残る週次段差を公式の裏付け付きで直す（#466）")
    ap.add_argument("--apply", action="store_true", help="実際に UPDATE する（既定はドライラン）")
    ap.add_argument("--only", help="edinet_code をカンマ区切りで指定（検出を省く）")
    ap.add_argument("--force", action="store_true", help="適用済みスタンプを無視する")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args()

    rep = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(rep, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
