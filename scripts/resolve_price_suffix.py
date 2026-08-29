"""株価ゼロの社を `.S`/`.F` でプローブし、解決できたサフィックスを永続化する（#555）。

## なぜ必要か

Yahoo のティッカーは長らく `f"{sec_code}.T"` 固定で、**東証以外の単独上場銘柄は原理的に
取得できなかった**。株価を1件も持たない454社を全数プローブすると 38社が現役上場で
（札証 SAP=16 / 福証 FKA=22）、財務レコードは持つのに株価が0件のまま
`/api/recommend`・M-1/M-2/M-3・`build_snapshots` から**例外を出さずに落ちている**。

## なぜ「毎晩 .S/.F も叩く」にしないか

取れない416社 × 2サフィックス ≒ **5〜6分/晩の新しい無駄**になり、#475 のバックオフで
削った 4.2分/晩 を上回る。そこで **一度きりの解決結果を `companies.yahoo_suffix` へ
永続化**し、毎晩はそれを引くだけにする。未解決の社は #475 の既存7日バックオフの回でのみ
再プローブされる（新規上場・地方上場への昇格も拾えるまま、毎晩のコストは増えない）。

## 採用ガード（件数だけ見てはいけない）

`.F` は Frankfurt と名前空間が衝突する。`377A.F` と `6461.F` は **HTTP200 で61バー返すが
`exchangeName=FRA`**＝同記号の欧州銘柄で、454社中2社（0.44%）が誤爆した。
**「取れた」ように見えるので、件数だけ見ていると別会社の株価を書き込む。**
採用は `exchangeName ∈ {SAP, FKA}` かつ `currency == JPY` の AND のみ。

バー数の下限は設けない。1734（北弘電社・札証）は61営業日中**1日しか約定していない**——
本数で足切りすると、この Issue が救おうとしている低流動銘柄をまさに落とす。

## 解決しただけでは px_* は復活しない

毎晩の gap-fill の起点は `today - DAILY_WINDOW_DAYS`（183日）なので、サフィックスを
解決しても付くのは **daily 183日 → weekly 約26週**だけで、`z_momentum`（52週）にも
`build_snapshots` の52週先ラベルにも届かない。5年遡及を担う
`backfill_weekly_history_yahoo` は `_pipeline_gh.py` からしか呼ばれておらず、#503 で
GHA がローカル正本を見られなくなった今、**運用経路から事実上外れている**。
そのため `--backfill-weekly` で解決できた社だけを 5年ぶん取り直す。

実行:
    python -m scripts.resolve_price_suffix                            # ドライラン（書かない）
    python -m scripts.resolve_price_suffix --apply
    python -m scripts.resolve_price_suffix --apply --backfill-weekly  # ＋5年 weekly
    python -m scripts.resolve_price_suffix --limit 20                 # スモーク
    python -m scripts.resolve_price_suffix --only 8398,1734
    python -m scripts.resolve_price_suffix --reprobe                  # 解決済みも測り直す
    python -m scripts.resolve_price_suffix --json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import text

import database as D
from collector_prices import fetch_yahoo_chart
from collector_utils import (
    YAHOO_LOCAL_EXCHANGES, YAHOO_EXPECT_CURRENCY, YAHOO_STOCK_RATE_SLEEP,
    PRICE_COMMIT_BATCH, YAHOO_BACKFILL_PROGRESS_BATCH, yahoo_ticker,
)

# プローブする順序。`.S` が採用できたら `.F` は叩かない（早期打ち切り）。
PROBE_SUFFIXES = (".S", ".F")

DEFAULT_PROBE_DAYS = 365   # 1リクエストのコストは窓幅に依らないので、薄い銘柄に当たる確率を上げる

# 「株価を1件も持たない社」。collector_prices.py のインライン判定（latest_daily /
# latest_weekly を dict 化して last is None を見る）と等価なものを SQL 側で1文にした。
PRICELESS_SQL = """
SELECT c.edinet_code, c.sec_code, c.name, c.is_active, c.yahoo_suffix
FROM companies c
WHERE c.sec_code IS NOT NULL
  AND c.sec_code <> ''
  AND NOT EXISTS (SELECT 1 FROM stock_price_daily  d WHERE d.edinet_code = c.edinet_code)
  AND NOT EXISTS (SELECT 1 FROM stock_price_weekly w WHERE w.edinet_code = c.edinet_code)
  {resolved_filter}
  {bucket_filter}
ORDER BY c.sec_code
"""


# Yahoo が「知らない記号」に対して 200 とともに返すプレースホルダの取引所名（2026-08-27 実測）。
# 名証 `.NG` で観測されたのと同じ記号で、`currency` も `longName` も欠ける。
# **実在する上場ではない**ので、バーが0本でも再プローブの価値は無い。
YAHOO_PLACEHOLDER_EXCHANGE = "YHD"

REJECT_BUCKET_NOTE = {
    "mismatch": "別の取引所/通貨を掴んだ（採用すると別会社の株価が入る）",
    "empty":    "期待した取引所に実在するが Yahoo にバーが1本も無い（再プローブする価値がある）",
    "placeholder": f"Yahoo が {YAHOO_PLACEHOLDER_EXCHANGE} の空箱を返しただけ（実在する上場ではない）",
    "not_found": "Yahoo がその記号を知らない（現時点で取得手段が無い）",
}

# 強い信号を優先する順。1社は複数サフィックスを試すので理由が混ざるため、
# 「どの文字列を含むか」ではなく**この順で最初に当たったもの**を採る。
REJECT_BUCKET_ORDER = ("mismatch", "empty", "placeholder", "not_found")


def reject_bucket(reason: str) -> str:
    """棄却理由を4分類へ畳む。

    分けている理由は、**「バーが0本」の中に性質のまったく違う2群がある**から（2026-08-27 実測）:

    | 例 | meta | 意味 |
    |---|---|---|
    | `1734.S` 北弘電社 | `SAP` / `JPY` / `KITA KOUDENSHA Corporation` | 札証に実在するが Yahoo が価格を持たない＝**再プローブの価値がある** |
    | `9062.F` 日本通運ほか27社 | `YHD` / currency なし / 名前なし | Yahoo の空箱。実在する上場ではない |

    これを1つの `empty` に畳むと、東証を廃止された大型株（日本通運・NTTドコモ・ベネッセ等）が
    「地方取引所に実在する」かのようにレポートへ並ぶ。**外部識別子は名前空間が衝突する**という
    この Issue の教訓（`.F`＝Frankfurt）を、棄却側でもう一度踏むことになる。
    """
    for head in REJECT_BUCKET_ORDER:
        if head in reason:
            return head
    return "not_found"


def decide_suffix(probes: list) -> tuple:
    """プローブ結果から採用サフィックスを決める → (suffix|None, reason)。

    `probes` は [(suffix, rows, meta), ...]。**純関数**（HTTP も DB も触らない）。

    棄却理由まで返すのは、件数だけ見て「38社取れた」と言わないため。
    `repair_price_scale_breaks` が failed/remaining/introduced を握り潰さずに返すのと同じ作法。
    """
    reasons = []
    for suffix, rows, meta in probes:
        want = YAHOO_LOCAL_EXCHANGES.get(suffix)
        got_ex = (meta or {}).get("exchangeName")
        got_cur = (meta or {}).get("currency")
        if not rows:
            # バーが0本でも「期待した取引所に実在する」と「Yahoo の空箱」は別物。
            # 前者だけが再プローブの候補になる（reject_bucket の docstring 参照）。
            if not meta:
                reasons.append(f"{suffix}:not_found")
            elif got_ex == want:
                reasons.append(f"{suffix}:empty:{got_ex}")
            else:
                reasons.append(f"{suffix}:placeholder:{got_ex or '?'}")
            continue
        if got_ex != want:
            # 例: 377A.F / 6461.F が返す FRA（Frankfurt の同記号銘柄）
            reasons.append(f"{suffix}:exchange_mismatch:{got_ex or '?'}")
            continue
        if got_cur != YAHOO_EXPECT_CURRENCY:
            reasons.append(f"{suffix}:currency_mismatch:{got_cur or '?'}")
            continue
        return suffix, "adopted"
    return None, ",".join(reasons) if reasons else "not_found"


async def probe_company(http, sec_code: str, d_from: str, d_to: str,
                        sleep: float = YAHOO_STOCK_RATE_SLEEP) -> tuple:
    """1社を PROBE_SUFFIXES の順に叩く → (suffix|None, reason, bars)。"""
    probes = []
    for suffix in PROBE_SUFFIXES:
        rows, meta = await fetch_yahoo_chart(
            http, yahoo_ticker(sec_code, suffix), d_from, d_to)
        probes.append((suffix, rows, meta))
        await asyncio.sleep(sleep)
        # 採用できたらそれ以上は叩かない
        got, _ = decide_suffix(probes)
        if got:
            return got, "adopted", len(rows)
    got, reason = decide_suffix(probes)
    bars = max((len(r) for _, r, _ in probes), default=0)
    return got, reason, bars


def _targets(db, reprobe: bool, only: Optional[list], limit: Optional[int],
             bucket: Optional[str] = None) -> list:
    """プローブ対象。`yahoo_suffix IS NULL` 条件だけで再開可能性が成立する
    （途中で落ちても、書けたぶんは次回の対象から自動的に外れる＝状態ファイル不要）。

    `bucket` を渡すと `yahoo_probe_bucket` で絞る（#560）。**月次バッチが使うのはこれ**——
    全数 454社は約8分かかり月次の窓に入らない（Σ予算 925 + マージン 30 に対し窓 960＝
    余裕5分）。`empty`（取引所は判明・バー0本）の5社だけなら約5秒で収まる。
    """
    sql = PRICELESS_SQL.format(
        resolved_filter="" if reprobe else "AND c.yahoo_suffix IS NULL",
        bucket_filter="AND c.yahoo_probe_bucket = :bucket" if bucket else "")
    rows = db.execute(text(sql), {"bucket": bucket} if bucket else {}).fetchall()
    if only:
        want = {s.strip() for s in only}
        rows = [r for r in rows if r[1] in want]
    if limit:
        rows = rows[:limit]
    return rows


async def _resolve(db, targets: list, d_from: str, d_to: str, sleep: float,
                   apply: bool) -> dict:
    adopted, rejected = [], []
    async with httpx.AsyncClient(timeout=60) as http:
        for i, (ec, sec, name, is_active, _cur) in enumerate(targets, 1):
            suffix, reason, bars = await probe_company(http, sec, d_from, d_to, sleep)
            rec = {"edinet_code": ec, "sec_code": sec, "name": name,
                   "is_active": is_active, "reason": reason, "bars": bars}
            if suffix:
                rec["suffix"] = suffix
                adopted.append(rec)
                if apply:
                    # `updated_at` は mirror の増分キー（scripts/mirror_common.py）なので
                    # 必ず進める。**`now()` は使わない**——Postgres 専用で、テストの
                    # in-memory SQLite が `no such function: now` で落ちる。
                    #
                    # 採用できたら棄却理由は消す（#560）。**残すと「解決済みなのに
                    # not_found」という読めない状態になる**し、月次の `--bucket empty` が
                    # 解決済みの社を拾い続ける。
                    db.execute(
                        text("UPDATE companies SET yahoo_suffix = :s, "
                             "yahoo_probe_bucket = NULL, "
                             "updated_at = :ts WHERE edinet_code = :ec"),
                        {"s": suffix, "ec": ec,
                         "ts": datetime.now(timezone.utc)})
            else:
                rec["bucket"] = reject_bucket(reason)
                rejected.append(rec)
                if apply:
                    # **棄却理由を永続化する（#560）。** 分類する `reject_bucket` は #555 から
                    # あったが printf されて消えており、「取引所は分かっているのに絞り込めない」
                    # 状態だった。ここで残すことで、月次が `empty` の5社だけを叩ける。
                    db.execute(
                        text("UPDATE companies SET yahoo_probe_bucket = :b, "
                             "updated_at = :ts WHERE edinet_code = :ec"),
                        {"b": rec["bucket"], "ec": ec,
                         "ts": datetime.now(timezone.utc)})
            if apply and i % PRICE_COMMIT_BATCH == 0:
                db.commit()   # 途中で落ちても、ここまでは残る
            if i % YAHOO_BACKFILL_PROGRESS_BATCH == 0:
                print(f"  [{i}/{len(targets)}] 採用 {len(adopted)} / 棄却 {len(rejected)}",
                      flush=True)
    if apply:
        db.commit()
    return {"adopted": adopted, "rejected": rejected}


def _print_report(res: dict, targets: list, applied: bool) -> None:
    adopted, rejected = res["adopted"], res["rejected"]
    print(f"\n=== 結果: 対象 {len(targets)}社 / 採用 {len(adopted)} / 棄却 {len(rejected)} ===")

    by_suffix: dict = {}
    for r in adopted:
        by_suffix.setdefault(r["suffix"], []).append(r)
    for suffix in sorted(by_suffix):
        ex = YAHOO_LOCAL_EXCHANGES[suffix]
        print(f"\n[採用] {suffix} ({ex}) {len(by_suffix[suffix])}社")
        for r in by_suffix[suffix]:
            print(f"  {r['sec_code']:>5}  {r['bars']:>4}バー  {r['name']}")

    for head in REJECT_BUCKET_ORDER:
        rs = [r for r in rejected if reject_bucket(r["reason"]) == head]
        if not rs:
            continue
        print(f"\n[棄却/{head}] {len(rs)}社  — {REJECT_BUCKET_NOTE[head]}")
        if head in ("mismatch", "empty"):
            # 誤爆と「銘柄はあるがバーが無い」は全件名指しで出す。
            # 前者は別会社を掴んだ証拠、後者は再プローブする価値がある社。
            for r in rs:
                print(f"  {r['sec_code']:>5}  {r['bars']:>4}バー  {r['reason']}  {r['name']}")
        else:
            print(f"  例: {', '.join(r['sec_code'] for r in rs[:10])}"
                  + (f" ... 他 {len(rs) - 10}社" if len(rs) > 10 else ""))

    if not applied:
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")


def _force_utf8_stdout() -> None:
    """cp932 の Windows コンソールへリダイレクトすると非 ASCII は UnicodeEncodeError で
    **出力済みの内容ごとクラッシュ**する（既知の罠）。社名を出すので UTF-8 へ倒す。

    **import 時ではなく `main()` からだけ呼ぶ。** モジュールレベルで `sys.stdout` を
    差し替えると pytest のキャプチャが `I/O operation on closed file` で壊れる
    （テストがこのモジュールを import して純関数を直接叩くため）。
    """
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")


def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="株価ゼロの社を .S/.F でプローブし、解決したサフィックスを永続化する（#555）")
    ap.add_argument("--apply", action="store_true",
                    help="companies.yahoo_suffix を実際に更新する（既定はドライラン）")
    ap.add_argument("--backfill-weekly", action="store_true",
                    help="採用した社だけ 5年ぶんの weekly を取り直す（--apply と併用）")
    ap.add_argument("--years-back", type=int, default=5,
                    help="--backfill-weekly の遡及年数（既定5）")
    ap.add_argument("--days", type=int, default=DEFAULT_PROBE_DAYS,
                    help=f"プローブ窓の日数（既定{DEFAULT_PROBE_DAYS}）")
    ap.add_argument("--limit", type=int, help="先頭N社だけ（スモーク用）")
    ap.add_argument("--only", help="証券コードをカンマ区切りで指定")
    ap.add_argument("--reprobe", action="store_true",
                    help="解決済みの社も測り直す（Yahoo が記号を張り替えた疑いがあるとき）")
    ap.add_argument("--bucket", choices=REJECT_BUCKET_ORDER,
                    help="前回の棄却理由で対象を絞る（月次は empty＝取引所判明・バー0本の5社）")
    ap.add_argument("--sleep", type=float, default=YAHOO_STOCK_RATE_SLEEP,
                    help=f"リクエスト間隔（秒・既定{YAHOO_STOCK_RATE_SLEEP}）")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args()

    today = date.today()
    d_to = today.strftime("%Y%m%d")
    d_from = (today - timedelta(days=args.days)).strftime("%Y%m%d")

    db = D.SessionLocal()
    try:
        targets = _targets(db, args.reprobe,
                           args.only.split(",") if args.only else None, args.limit,
                           bucket=args.bucket)
        n_req = len(targets) * len(PROBE_SUFFIXES)
        print(f"接続先: {'ローカル' if D._is_local else 'リモート'}")
        print(f"対象: {len(targets)}社（プローブ窓 {d_from}〜{d_to}）")
        print(f"最大リクエスト数: {n_req}（早期打ち切りで実際は減る）"
              f" / 見積り {n_req * args.sleep / 60:.1f}分〜")
        if not targets:
            print("対象なし。")
            return 0

        res = asyncio.run(_resolve(db, targets, d_from, d_to, args.sleep, args.apply))

        if args.json:
            print(json.dumps(res, ensure_ascii=False, default=str, indent=2))
        else:
            _print_report(res, targets, args.apply)

        if args.apply and args.backfill_weekly and res["adopted"]:
            ecs = [r["edinet_code"] for r in res["adopted"]]
            print(f"\n=== 5年 weekly backfill: {len(ecs)}社 ===")
            print("（解決しただけでは daily 保持窓183日＝約26週しか付かず z_momentum の"
                  "52週に届かないため・#555）")
            from collector_prices import backfill_weekly_history_yahoo
            r = asyncio.run(backfill_weekly_history_yahoo(
                db, years_back=args.years_back, only=ecs,
                on_progress=lambda i, t, m: print(f"  {m}", flush=True)))
            print(f"  結果: {r}")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
