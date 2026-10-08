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
採用は `exchangeName ∈ {SAP, FKA}` かつ `currency == JPY` かつ**出来高>0 のバーが1本以上**の
AND のみ。

バー数の下限は設けない（本数で足切りすると、この Issue が救おうとしている低流動銘柄を
まさに落とす）。**ただし約定の証拠は要る**（#769）。1734（北弘電社）は 2024-04-11 に
上場廃止済みなのに、Yahoo は `SAP`/`JPY`/実名とともに**出来高0のバーを1本**返し、これを
採用した結果、2026-07-17 付けの幽霊株価が FY2023 の財務行へ押し込まれた。取引所名・通貨・
実名は廃止済みの記号にも残るので、生きている上場の証拠にならない。

## `--reprobe`（解決済みの測り直し）

対象は「株価ゼロの社」に加えて**株価を持つ解決済みの社**（#769）。解決できた社には翌晩から
株価が入るので、株価ゼロに限ると解決済みの社は永久に測り直せない＝夜間の「解決済みなのに空」
が案内する手順が、警告の対象に届かなかった。**株価を持つ未解決の社（東証の銘柄）は入れない**
——東証と福証の重複上場が `.F` に切り替わる。棄却された解決済みの社は接尾辞を外すが、
そのプローブで 429・5xx・404 以外の 4xx・分類不能の失敗を踏んだ社は**判定不能**として触らない
（一時失敗で健全な社の株価収集を止めない）。

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
    python -m scripts.resolve_price_suffix --reprobe                  # 解決済みも測り直す（棄却なら外す）
    python -m scripts.resolve_price_suffix --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import text

import database as D
from collector_prices import fetch_yahoo_chart, yahoo_http_stats
from collector_utils import (
    YAHOO_LOCAL_EXCHANGES, YAHOO_EXPECT_CURRENCY, YAHOO_STOCK_RATE_SLEEP,
    PRICE_COMMIT_BATCH, YAHOO_BACKFILL_PROGRESS_BATCH,
    force_utf8_stdout, yahoo_ticker,
)

# プローブする順序。`.S` が採用できたら `.F` は叩かない（早期打ち切り）。
PROBE_SUFFIXES = (".S", ".F")

DEFAULT_PROBE_DAYS = 365   # 1リクエストのコストは窓幅に依らないので、薄い銘柄に当たる確率を上げる

# 「株価を1件も持たない社」。collector_prices.py のインライン判定（latest_daily /
# latest_weekly を dict 化して last is None を見る）と等価なものを SQL 側で1文にした。
PRICELESS_COND = (
    "NOT EXISTS (SELECT 1 FROM stock_price_daily  d WHERE d.edinet_code = c.edinet_code)"
    " AND NOT EXISTS (SELECT 1 FROM stock_price_weekly w WHERE w.edinet_code = c.edinet_code)"
)

# 既定は「株価ゼロ・未解決」。`--reprobe` は株価ゼロに加えて**株価を持つ解決済み**も含む
# （#769・モジュール冒頭の `--reprobe` 節）。株価を持つ未解決の社は含めない。
TARGETS_SQL = """
SELECT c.edinet_code, c.sec_code, c.name, c.is_active, c.yahoo_suffix
FROM companies c
WHERE c.sec_code IS NOT NULL
  AND c.sec_code <> ''
  AND ({scope})
  {bucket_filter}
ORDER BY c.sec_code
"""
SCOPE_DEFAULT = f"{PRICELESS_COND} AND c.yahoo_suffix IS NULL"
SCOPE_REPROBE = f"({PRICELESS_COND}) OR c.yahoo_suffix IS NOT NULL"


# Yahoo が「知らない記号」に対して 200 とともに返すプレースホルダの取引所名（2026-08-27 実測）。
# 名証 `.NG` で観測されたのと同じ記号で、`currency` も `longName` も欠ける。
# **実在する上場ではない**ので、バーが0本でも再プローブの価値は無い。
YAHOO_PLACEHOLDER_EXCHANGE = "YHD"

REJECT_BUCKET_NOTE = {
    "mismatch": "別の取引所/通貨を掴んだ（採用すると別会社の株価が入る）",
    "empty":    "期待した取引所の meta は返るが、約定のあるバーが1本も無い（再プローブする価値がある）",
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
    | `231A.F` Cross E Holdings | `FKA` / `JPY` / 実名 | 福証に実在するが Yahoo が価格を持たない＝**再プローブの価値がある** |
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
        if not any((r.get("volume") or 0) > 0 for r in rows):
            # 取引所名・通貨・実名は廃止済みの記号にも残る（#769: 1734.S は 2024 年廃止なのに
            # SAP/JPY/実名＋出来高0の1本）。約定の無い応答は `empty` と同じ棚へ畳む。
            reasons.append(f"{suffix}:empty:{got_ex}:no_trades")
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

    `bucket` を渡すと `yahoo_probe_bucket` で絞る（#560）。#560 当時の月次バッチは `empty`
    （取引所は判明・バー0本）の数社だけを回していた（全数 454社の約8分が窓の余裕5分に
    入らなかった）。#841 で窓に余裕ができ、月次は絞らずに全バケットを回す。

    `reprobe` は株価を持つ解決済みの社も含む（#769・`SCOPE_REPROBE`）。
    """
    sql = TARGETS_SQL.format(
        scope=SCOPE_REPROBE if reprobe else SCOPE_DEFAULT,
        bucket_filter="AND c.yahoo_probe_bucket = :bucket" if bucket else "")
    rows = db.execute(text(sql), {"bucket": bucket} if bucket else {}).fetchall()
    if only:
        want = {s.strip() for s in only}
        rows = [r for r in rows if r[1] in want]
    if limit:
        rows = rows[:limit]
    return rows


def transient_http_failures(stats: dict) -> int:
    """`yahoo_http_stats` の集計のうち「答えが出ていない」失敗の数（#769）。

    404 は「その記号は無い」という**答え**なので数えない。429・5xx・404 以外の 4xx（拒否）・
    分類不能（タイムアウト等）は答えではない＝解決済みの社の接尾辞を外す根拠にしない。
    """
    return (stats.get("429", 0) + stats.get("5xx", 0) + stats.get("other", 0)
            + stats.get("4xx", 0) - stats.get("404", 0))


async def _resolve(db, targets: list, d_from: str, d_to: str, sleep: float,
                   apply: bool) -> dict:
    adopted, rejected = [], []
    unresolved, undecided = [], []   # `--reprobe` で棄却された解決済みの社（#769）
    async with httpx.AsyncClient(timeout=60) as http:
        for i, (ec, sec, name, is_active, cur) in enumerate(targets, 1):
            with yahoo_http_stats() as http_errors:
                suffix, reason, bars = await probe_company(http, sec, d_from, d_to, sleep)
            rec = {"edinet_code": ec, "sec_code": sec, "name": name,
                   "is_active": is_active, "reason": reason, "bars": bars}
            if not suffix and cur and transient_http_failures(http_errors):
                # 一時失敗は答えではない。外すと健全な社の株価収集が止まり、株価を持つ
                # 未解決の社は `--reprobe` の対象にも戻らない＝元に戻す経路が無い。
                rec["suffix"] = cur
                rec["http_errors"] = dict(http_errors)
                undecided.append(rec)
            elif suffix:
                rec["suffix"] = suffix
                adopted.append(rec)
                if apply:
                    # `updated_at` は mirror の増分キー（scripts/mirror_common.py）なので
                    # 必ず進める。**`now()` は使わない**——Postgres 専用で、テストの
                    # in-memory SQLite が `no such function: now` で落ちる。
                    #
                    # 採用できたら棄却理由は消す（#560）。**残すと「解決済みなのに
                    # not_found」という読めない状態になる**し、`--bucket` で絞ったときに
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
                if cur:
                    # 解決済みだった社の棄却（#769）。接尾辞を残すと毎晩「解決済みなのに空」を
                    # 叩き続ける（廃止社の見送りを素通りする＝警告が永久に続く）。
                    rec["previous_suffix"] = cur
                    unresolved.append(rec)
                if apply:
                    # **棄却理由を永続化する（#560）。** 分類する `reject_bucket` は #555 から
                    # あったが printf されて消えており、「取引所は分かっているのに絞り込めない」
                    # 状態だった。ここで残すことで、月次が `empty` の5社だけを叩ける。
                    # 解決済みだった社は同じ文で接尾辞も外す（NULL＝`.T` で試す・#555）。
                    db.execute(
                        text("UPDATE companies SET yahoo_probe_bucket = :b, "
                             + ("yahoo_suffix = NULL, " if cur else "")
                             + "updated_at = :ts WHERE edinet_code = :ec"),
                        {"b": rec["bucket"], "ec": ec,
                         "ts": datetime.now(timezone.utc)})
            if apply and i % PRICE_COMMIT_BATCH == 0:
                db.commit()   # 途中で落ちても、ここまでは残る
            if i % YAHOO_BACKFILL_PROGRESS_BATCH == 0:
                print(f"  [{i}/{len(targets)}] 採用 {len(adopted)} / 棄却 {len(rejected)}",
                      flush=True)
    if apply:
        db.commit()
    return {"adopted": adopted, "rejected": rejected,
            "unresolved": unresolved, "undecided": undecided}


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

    # `--reprobe` で動いた解決済みの社は全件名指しで出す（#769）。接尾辞を外すと翌晩から
    # その社の取り方が変わるので、何が外れたかを数だけで済ませない。
    unresolved, undecided = res.get("unresolved") or [], res.get("undecided") or []
    if unresolved:
        print(f"\n[解除] 解決済みだったが棄却 {len(unresolved)}社 — 接尾辞を外す"
              "（NULL＝.T で試す。夜間の「解決済みなのに空」はこれで止まる）")
        for r in unresolved:
            print(f"  {r['sec_code']:>5}  {r['previous_suffix']} → なし  {r['bars']:>4}バー"
                  f"  {r['reason']}  {r['name']}")
    if undecided:
        print(f"\n[判定不能] 解決済みで HTTP の一時失敗 {len(undecided)}社 — 接尾辞は残した"
              "（時間を置いて --reprobe し直す）")
        for r in undecided:
            print(f"  {r['sec_code']:>5}  {r['suffix']}  {r['http_errors']}  {r['name']}")

    if not applied:
        print("\nドライラン（何も変更していない）。実行するには --apply を付けてください。")


def main() -> int:
    force_utf8_stdout()
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
                    help="株価を持つ解決済みの社も測り直す。棄却されたら接尾辞を外す"
                         "（夜間の「解決済みなのに空」のとき・#769）")
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
