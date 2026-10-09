"""Yahoo が株式併合を split として返した比率倍の株価を戻す一回性の修復（#765）。

## 何が起きたか

Yahoo は TOB 後のスクイーズアウトの株式併合（例: 4,400,000株→1株）を split（1:4,400,000）
として登録し、比率で調整した終値を返す。毎晩の gap-fill がそれを検査なしで書き、6社・日次15行・
週次10週が 40万〜2000万倍の値になった。上場廃止で系列が止まるので、異常値が「最新株価」として
固定される。書く側は `collector_prices.fill_recent_stock_price_gap_yahoo` のガード（#765）が
止めたので、ここは**既に書かれた行だけ**を戻す。

## なぜ台帳なのか

直す行は Issue #765 の表 D を1行ずつ書き写した `FIXES` に限る。値の形（比率倍かどうか）は
計算で分かるが、**その日に取引があったか**（戻すのか消すのか）は上場廃止日という外部の事実で
決まり、値の形からは決められない。観測した異常値は、実際の終値と Yahoo の比率から
`float32(実値 ÷ round(1/比率, 10))`（遡及調整の形）か `float32(実値 × 比率)`（split 当日の形）で
ビット単位まで再現できる（`tests/test_repair_consolidation_prices.py` が22件すべてを縛る）。

## 判定は「値の一致」ではなく「単位」

マージ後の夜間が、同じ単位のまま少し違う値で書き直すことがある（当日の形→遡及の形）。
完全一致で判定すると初日から拒否に倒れるので、戻す行は「実際の終値から100倍以上離れていれば
未処理」、消す行は「比率倍のまま残っていれば未処理」で読む。split 当日より後の幽霊バー
（E02305 の 6/12・7,580・出来高0）だけは単位が正常なので、観測値と出来高0の一致で読む。

## ドライランと適用は同じ手順を通る

ひとつのトランザクションの中で直し、**確定の前に株価表の全体を走査し直す**。段差が1つでも
残れば（台帳に無い比率倍の行が増えていた等）巻き戻して拒否する。ドライランは最後に必ず
巻き戻す＝DB に何も残らない。確定したあとで週次キャッシュの世代印を進め（保持窓より古い
E03530 の週を書き換えるため）、6社に限って `financial_records` の市場データを期末近傍の
週次株価で書き直す（全社に回すと #765 と無関係な行まで書き換わる）。

**Yahoo で取り直す修復コマンド（`repair_scale_mixture --apply`・`--repair-price-breaks
--persist`）は使わない。** Yahoo は今も比率倍の値を返すので、汚染を保持窓全体へ広げる。

実行

    python -m scripts.repair_consolidation_prices            # ドライラン（直して走査→巻き戻す）
    python -m scripts.repair_consolidation_prices --apply    # 確定
    python -m scripts.repair_consolidation_prices --json
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import bindparam, text as sqla_text   # noqa: E402

import database as D   # noqa: E402
import weekly_price_cache   # noqa: E402
from database import guard_local_target   # noqa: E402  唯一の定義は database（#872）
from collector_prices import (   # noqa: E402
    scale_rejection_example, scan_price_scale_steps, update_market_data_from_history,
)
from collector_utils import force_utf8_stdout, is_scale_break   # noqa: E402

RESTORE, DELETE = "restore", "delete"
RETRO, FORWARD, GHOST = "retro", "forward", "ghost"
PENDING, DONE, MISMATCH = "pending", "done", "mismatch"
STATUS_LABEL = {PENDING: "未処理", DONE: "処理済み", MISMATCH: "不一致"}

# financial_records で「まだ比率倍の株価を持っている」とみなす下限（円）。上場株で100万円を
# 超える終値は無い（最高値の銘柄でも数十万円台）ので、表示用の目安として十分。
FIN_SUSPECT_PRICE = 1_000_000


@dataclass(frozen=True)
class Fix:
    """直す1行。`observed` は 2026-09-30 に観測した値（根拠）で、判定には GHOST のときだけ使う。"""
    table: str                 # "daily"（date=trade_date）/ "weekly"（date=week_start）
    ec: str
    date: str
    action: str                # RESTORE（real へ戻す）/ DELETE（取引の無い日の幽霊バー）
    real: float                # その日の実際の終値（DELETE では段差の判定の基準）
    observed: float
    ratio: Optional[float]     # Yahoo の split 比率（GHOST は None）
    form: str                  # RETRO / FORWARD / GHOST


def _d(ec, d, action, real, observed, ratio, form) -> Fix:
    return Fix("daily", ec, d, action, real, observed, ratio, form)


def _w(ec, d, action, real, observed, ratio, form) -> Fix:
    return Fix("weekly", ec, d, action, real, observed, ratio, form)


# Issue #765 の表 D。比率は Yahoo chart API（events=split）の 2026-09-29〜30 の応答。
FIXES: tuple = (
    # E25282 1909 日本ドライケミカル: TOB → 株式併合 4,400,000株→1株 → 9/14 上場廃止
    _d("E25282", "2026-09-09", RESTORE, 3700.0, 16278046720.0, 4_400_000, RETRO),
    _d("E25282", "2026-09-10", RESTORE, 3700.0, 16278046720.0, 4_400_000, RETRO),
    _d("E25282", "2026-09-11", RESTORE, 3700.0, 16278046720.0, 4_400_000, RETRO),
    _d("E25282", "2026-09-14", DELETE, 3700.0, 16280000512.0, 4_400_000, FORWARD),   # 廃止日・取引なし
    # E21381 2180 サニーサイドアップG: TOB → 株式併合 → 9/16 上場廃止
    _d("E21381", "2026-09-14", RESTORE, 1309.0, 1886167168.0, 1_440_960, RETRO),
    _d("E21381", "2026-09-15", RESTORE, 1309.0, 1886167168.0, 1_440_960, RETRO),
    # E02798 7426 山大: 整理銘柄 → 株式併合（効力 9/30）→ 上場廃止
    _d("E02798", "2026-09-24", RESTORE, 592.0, 240699328.0, 406_580, RETRO),
    _d("E02798", "2026-09-25", RESTORE, 591.0, 240292736.0, 406_580, RETRO),
    _d("E02798", "2026-09-28", DELETE, 591.0, 240288784.0, 406_580, FORWARD),        # 廃止日・取引なし
    # 2026-09-30 の Yahoo が返していた廃止後の幽霊バー（DB にはまだ無い。無ければ処理済みとして
    # 読むので害は無く、ガードの入る前の夜間が書いたら消す＝Issue の「9/30 以降に足された分」）
    _d("E02798", "2026-09-29", DELETE, 591.0, 240288784.0, 406_580, FORWARD),
    # E35289 7082 ジモティー: TOB → 株式併合 → 9/29 上場廃止（9/28 が売買最終日）
    _d("E35289", "2026-09-28", RESTORE, 1407.0, 1688467584.0, 1_200_000, RETRO),
    _d("E35289", "2026-09-29", DELETE, 1407.0, 1688400000.0, 1_200_000, FORWARD),    # 廃止日の幽霊（同上）
    # E02305 7999 MUTOH HD: TOB → 株式併合 573,512株→1株（効力 6/12）→ 6/10 上場廃止
    _d("E02305", "2026-05-18", RESTORE, 7580.0, 4347327488.0, 573_512, RETRO),
    _d("E02305", "2026-05-19", RESTORE, 7580.0, 4347327488.0, 573_512, RETRO),
    _d("E02305", "2026-05-20", RESTORE, 7580.0, 4347327488.0, 573_512, RETRO),
    _d("E02305", "2026-05-21", RESTORE, 7580.0, 4347327488.0, 573_512, RETRO),
    _d("E02305", "2026-06-10", DELETE, 7580.0, 4347220992.0, 573_512, FORWARD),      # 廃止日・取引なし
    _d("E02305", "2026-06-12", DELETE, 7580.0, 7580.0, None, GHOST),                # 併合の効力日・出来高0
    # E03530 8303 SBI新生銀行: split 1:20,000,000（2023-09-28＝前回の上場廃止）。週次だけ
    _w("E03530", "2023-09-25", RESTORE, 2766.0, 55319998464.0, 20_000_000, FORWARD),
    _w("E03530", "2025-11-17", DELETE, 2766.0, 55319998464.0, 20_000_000, FORWARD),  # 再上場前の幽霊
    _w("E03530", "2025-11-24", DELETE, 2766.0, 55319998464.0, 20_000_000, FORWARD),
    _w("E03530", "2025-12-01", DELETE, 2766.0, 55319998464.0, 20_000_000, FORWARD),
)


# ── 純関数（DB にもネットワークにも触らない・ここがテスト対象）─────────────────

def float32(x: float) -> float:
    """Yahoo の JSON が持つ単精度へ丸める（異常値の出どころの指紋・#765）。"""
    return struct.unpack("f", struct.pack("f", x))[0]


def reproduce(fix: Fix) -> Optional[float]:
    """実際の終値と比率から、Yahoo が返した値を再現する（GHOST は None）。"""
    if fix.form == RETRO:
        return float32(fix.real / round(1 / fix.ratio, 10))
    if fix.form == FORWARD:
        return float32(fix.real * fix.ratio)
    return None


def status_of(fix: Fix, row: Optional[tuple]) -> str:
    """いまの行 `(close, volume)`（無ければ None）から、その項目の状態を返す。"""
    if fix.action == RESTORE:
        if row is None:
            return MISMATCH           # 戻す相手が消えている＝台帳の前提が崩れた
        return PENDING if is_scale_break(fix.real, row[0]) else DONE
    if row is None:
        return DONE
    if fix.form == GHOST:
        return PENDING if row[0] == fix.observed and not row[1] else MISMATCH
    return PENDING if is_scale_break(fix.real, row[0]) else MISMATCH


# ── DB ──────────────────────────────────────────────────────────────────────────

_COLS = {"daily": ("stock_price_daily", "trade_date", "close", "volume"),
         "weekly": ("stock_price_weekly", "week_start", "close_last", "volume_sum")}


def read_row(db, fix: Fix) -> Optional[tuple]:
    table, key, col, vol = _COLS[fix.table]
    r = db.execute(sqla_text(
        f"SELECT {col}, {vol} FROM {table} WHERE edinet_code = :ec AND {key} = :d"),
        {"ec": fix.ec, "d": fix.date}).first()
    return (float(r[0]), r[1]) if r is not None else None


def plan(db, fixes) -> list:
    """`[(fix, いまの行, 状態)]`。"""
    out = []
    for f in fixes:
        row = read_row(db, f)
        out.append((f, row, status_of(f, row)))
    return out


def execute(db, planned: list) -> dict:
    """未処理の項目を直す（**確定しない**）。日次を直したら触れた週を日次から作り直し、
    日次が1日も残らなくなった週は週次の行も消す（`_recompute_weeks_from_daily` は消さない）。"""
    done = {"restored": 0, "deleted": 0, "weeks_recomputed": 0, "weeks_deleted": []}
    touched = []
    for f, _, st in planned:
        if st != PENDING:
            continue
        table, key, col, _ = _COLS[f.table]
        where = f"WHERE edinet_code = :ec AND {key} = :d"
        params = {"ec": f.ec, "d": f.date}
        if f.action == RESTORE:
            # 出来高は NULL にする。0 だと週次の出来高・売買代金に「取引ゼロの日」として入る
            # （Yahoo が返した 0 は「出来高 ÷ 比率」が丸まったもので、取引が無かった印ではない）。
            extra = ", volume = NULL" if f.table == "daily" else ""
            db.execute(sqla_text(f"UPDATE {table} SET {col} = :v{extra} {where}"),
                       dict(params, v=f.real))
            done["restored"] += 1
        else:
            db.execute(sqla_text(f"DELETE FROM {table} {where}"), params)
            done["deleted"] += 1
        if f.table == "daily":
            touched.append({"edinet_code": f.ec, "trade_date": f.date})

    if touched:
        D._recompute_weeks_from_daily(db, touched)
        weeks = sorted({(t["edinet_code"], D.iso_week_start(t["trade_date"])) for t in touched})
        done["weeks_recomputed"] = len(weeks)
        # 保持窓より古い週は日次が消えているのが正常＝「日次が無い」を空と読まない
        oldest_live_week = D.iso_week_start(D._daily_cutoff())
        for ec, ws in weeks:
            if ws < oldest_live_week:
                continue
            we = (date.fromisoformat(ws) + timedelta(days=6)).isoformat()
            n = db.execute(sqla_text(
                "SELECT COUNT(*) FROM stock_price_daily "
                "WHERE edinet_code = :ec AND trade_date >= :ws AND trade_date <= :we"),
                {"ec": ec, "ws": ws, "we": we}).scalar()
            if not n:
                db.execute(sqla_text(
                    "DELETE FROM stock_price_weekly WHERE edinet_code = :ec AND week_start = :ws"),
                    {"ec": ec, "ws": ws})
                done["weeks_deleted"].append(f"{ec} {ws}")
    return done


def _scan_summary(res: dict) -> dict:
    return {"companies": res["companies"], "daily_steps": res["daily_steps"],
            "weekly_steps": res["weekly_steps"],
            "examples": [f"{h['table']} " + scale_rejection_example(h) for h in res["hits"][:10]]}


def fin_suspects(db, ecs: list) -> list:
    rows = db.execute(sqla_text(
        "SELECT edinet_code, year, period_type, period_end, stock_price FROM financial_records "
        "WHERE stock_price >= :p AND edinet_code IN :ecs ORDER BY edinet_code, year"
    ).bindparams(bindparam("ecs", expanding=True)),
        {"p": FIN_SUSPECT_PRICE, "ecs": list(ecs)}).all()
    return [{"edinet_code": r[0], "year": r[1], "period_type": r[2],
             "period_end": str(r[3]), "stock_price": float(r[4])} for r in rows]


def _market_view(db, ecs: list) -> dict:
    latest = D.latest_prices(db, ecs)
    return {"latest": {ec: {"price": v["price"], "date": str(v["date"])}
                       for ec, v in sorted(latest.items())},
            "fin_suspects": fin_suspects(db, ecs)}


def run(db, fixes=FIXES, *, apply: bool = False) -> dict:
    """台帳を読み、直して走査し、ドライランなら巻き戻す。戻り値の `refused` が非 None なら拒否。"""
    guard_local_target()
    ecs = sorted({f.ec for f in fixes})
    planned = plan(db, fixes)
    rep: dict = {
        "apply": apply, "applied": False, "refused": None,
        "plan": [{"table": f.table, "edinet_code": f.ec, "date": f.date, "action": f.action,
                  "real": f.real, "current": None if row is None else row[0],
                  "volume": None if row is None else row[1], "status": st}
                 for f, row, st in planned],
        "before": _scan_summary(scan_price_scale_steps(db)),
        "market_before": _market_view(db, ecs),
    }
    if any(st == MISMATCH for _, _, st in planned):
        rep["refused"] = "台帳の前提と DB の行が合わない（不一致の項目を確かめる）"
        db.rollback()
        return rep
    if not any(st == PENDING for _, _, st in planned):
        rep["already_applied"] = True
        db.rollback()
        return rep

    try:
        rep["executed"] = execute(db, planned)
        after = scan_price_scale_steps(db)
        rep["after"] = _scan_summary(after)
        if after["hits"]:
            db.rollback()
            rep["refused"] = ("直した後も株価表に100倍以上の段差が残る"
                              "（台帳に無い比率倍の行がある。増えた行を台帳へ足す）")
            return rep
        if not apply:
            db.rollback()
            return rep
        db.commit()
    except Exception:
        db.rollback()
        raise
    rep["applied"] = True

    # 世代印は**確定の後**（ADR-0036）。E03530 の 2023 年・2025 年の週は保持窓より古く、
    # 差分ロードの指紋（max(week_start)・行数）では値の書き換えを検出できない。
    rep["generation"] = weekly_price_cache.bump_generation_safely(
        db, f"repair-consolidation-prices (#765): {len(ecs)} companies")
    # 6社だけ、期末近傍の週次株価で書き直す（E03530 は 2023-09-30 期末の H1 行が
    # 汚染週の30日以内にあり、最新行だけの更新では直らない）。
    rep["market_updated"] = update_market_data_from_history(db, point_in_time=True, only=ecs)
    rep["market_after"] = _market_view(db, ecs)
    return rep


# ── 表示 ────────────────────────────────────────────────────────────────────────

def _fmt(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    return f"{v:,.0f}" if v.is_integer() else f"{v:,.2f}"


def print_report(rep: dict) -> None:
    print(f"台帳 {len(rep['plan'])}件（Issue #765 の表 D）")
    for p in rep["plan"]:
        verb = f"{_fmt(p['real'])} へ戻す" if p["action"] == RESTORE else "削除"
        print(f"  [{STATUS_LABEL[p['status']]}] {p['table']:6s} {p['edinet_code']} {p['date']}"
              f"  いま {_fmt(p['current'])}（出来高 {_fmt(p['volume'])}）→ {verb}")
    b = rep["before"]
    print(f"\n直す前の株価表: 100倍以上の段差 {len(b['companies'])}社"
          f"（日次 {b['daily_steps']}件・週次 {b['weekly_steps']}件）")
    for ex in b["examples"]:
        print(f"  {ex}")
    mb = rep["market_before"]
    print("\n直す前の最新株価（latest_prices）: "
          + " / ".join(f"{ec}={_fmt(v['price'])}（{v['date']}）" for ec, v in mb["latest"].items()))
    print(f"直す前の financial_records で株価 {FIN_SUSPECT_PRICE:,}円以上: {len(mb['fin_suspects'])}行")

    if rep.get("already_applied"):
        print("\n台帳はすべて処理済み（何もしない）")
        return
    if rep.get("executed"):
        e = rep["executed"]
        print(f"\n修正: 戻す {e['restored']}行・消す {e['deleted']}行・週の作り直し {e['weeks_recomputed']}週"
              f"・日次が空になり消した週 {len(e['weeks_deleted'])}（{', '.join(e['weeks_deleted']) or 'なし'}）")
    if rep.get("after"):
        a = rep["after"]
        print(f"直した後の株価表: 100倍以上の段差 {len(a['companies'])}社"
              f"（日次 {a['daily_steps']}件・週次 {a['weekly_steps']}件）")
        for ex in a["examples"]:
            print(f"  {ex}")
    if rep.get("refused"):
        print(f"\n[拒否] {rep['refused']}。何も書いていません（巻き戻し済み）")
        return
    if not rep["applied"]:
        print("\nドライラン: 上の修正はトランザクションの中で行い、巻き戻しました（DB は元のまま）。"
              "--apply で確定します")
        return

    ma = rep["market_after"]
    print(f"\n確定しました。週次キャッシュの世代印: {rep.get('generation') or '進められなかった'}")
    if not rep.get("generation"):
        print("  手で進める: weekly_price_cache.bump_generation(db, 'repair #765') を1回呼ぶ"
              "（進めないと週次キャッシュが 2023 年・2025 年の古い値を返し続ける）")
    print(f"financial_records（6社・point-in-time）: {rep['market_updated']}レコードを更新")
    print("直した後の最新株価（latest_prices）: "
          + " / ".join(f"{ec}={_fmt(v['price'])}（{v['date']}）" for ec, v in ma["latest"].items()))
    print(f"直した後の financial_records で株価 {FIN_SUSPECT_PRICE:,}円以上: {len(ma['fin_suspects'])}行")
    for r in ma["fin_suspects"]:
        print(f"  {r['edinet_code']} {r['year']} {r['period_type']} {r['period_end']} {_fmt(r['stock_price'])}")
    print("\n残りの手順（手で行う）:")
    print("  1. scripts/.cache/weekly_prices_*.pkl を scripts/.cache/_stale_pre765/ へ退避する"
          "（世代印を持たない手元キャッシュ・GOTCHAS の #620 と同じ作法）")
    print("  2. python -m scripts.run_daytime --queue で日中枠の仕事を確かめる")


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Yahoo が株式併合を split として返した比率倍の株価を戻す（#765）")
    ap.add_argument("--apply", action="store_true", help="確定する（既定はドライラン＝必ず巻き戻す）")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args()

    db = D.SessionLocal()
    try:
        rep = run(db, apply=args.apply)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(rep)
    return 2 if rep.get("refused") else 0


if __name__ == "__main__":
    raise SystemExit(main())
