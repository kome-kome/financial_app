"""naive DateTime 列に混入した JST 値を UTC へ引き直す（#565・ADR-0043）。

## なぜ必要か

DateTime 列は `timestamp without time zone`（naive）だが、Python 側は
`datetime.now(timezone.utc)`（aware UTC）を渡している。psycopg2 は aware のまま送るので、
PG は naive 列へキャストする際に**セッション TZ でローカル時刻へ変換して tz を落とす**。

- Supabase（`TimeZone=UTC`）→ UTC naive で保存される＝`api._utc_to_jst_str` の仮定が成立
- ローカル PG（実測 `TimeZone=Asia/Tokyo`）→ **JST naive** で保存され、表示が更に +9h される

#503 で正本をローカル PG へ移した瞬間に後者へ切り替わったが、**例外は出ない**（#508 と同型）。
結果として同じ列に UTC と JST が混在している。再発は `database.SESSION_FIXES`
（接続時に `SET TimeZone = 'UTC'`）が止める。**このスクリプトは既存行の引き直しだけを担う。**

## 実行順を間違えると再汚染する

`database.SESSION_FIXES` を入れる**前**に引き直すと、次の夜間バッチがまた JST で書く。
必ず「コード修正 → デプロイ/取り込み → 引き直し」の順で走らせること。

## 境界の決め方（実測 2026-08-29）

正本反転（2026-08-20）の前後で、naive timestamp を持つ全列を並べると:

- pre-flip 最終行  `companies.updated_at = 2026-08-18 21:10:23`（UTC 値）
- post-flip 初行   `macro_data.created_at = 2026-08-20 19:43:32`（JST 値）
- その間は**全表ゼロ**＝約 46 時間のきれいな空白

したがって cutoff をこの空白の中へ置けば「UTC 値を誤って 9 時間ずらす」ことは起きない。
**この空白は仮定ではなく毎回の検査にしてある**（`--apply` 前に guard band を数え、
1行でも居たら書かずに終了する）。ミラー pull で入った Supabase 由来の行は反転前の
UTC 値しか持たないので、この空白が空である限り対象から外れる。

## 実行

    python -m scripts.fix_naive_jst_timestamps                    # ドライラン（既定・1バイトも書かない）
    python -m scripts.fix_naive_jst_timestamps --json             # 機械可読
    python -m scripts.fix_naive_jst_timestamps --apply
    python -m scripts.fix_naive_jst_timestamps --cutoff 2026-08-20T19:00:00
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import DateTime, text

import database as D

# 正本反転（#503）の直後・ローカル初回書き込み（2026-08-20 19:43:32）の直前。
# 左側の最も近い行は 2026-08-18 21:10:23 なので 24時間の guard band は空になる。
DEFAULT_CUTOFF = "2026-08-20T19:00:00"

# guard band の幅。cutoff の直前にこの幅だけ行が居ないことを確認してから書く。
GUARD_BAND_HOURS = 24

# JST は 1951 年以降 DST が無いので `- interval '9 hours'` と同値だが、
# **式としては `AT TIME ZONE` を使う**（セッション TZ に依存せず、意図がそのまま読める）。
SHIFT_EXPR = "({col} AT TIME ZONE 'Asia/Tokyo') AT TIME ZONE 'UTC'"

# 冪等スタンプ。2度掛けると 18 時間ずれる＝取り返しがつかないので必ず見る。
STAMP_KEY = "tz_jst_backfill_applied"


# ── 対象列の導出 ────────────────────────────────────────────────────────────

def naive_datetime_columns() -> list[tuple[str, str]]:
    """`Base.metadata` から (table, column) を導出する。

    **一覧を書き写さない**のが要点。表や列を足しても引き直し対象へ自動的に載る
    （書き写すと「足したのに直っていない」が静かに起きる＝ADR-0031 と同型）。
    `timezone=True`（`app_settings.updated_at` だけ）は仕様どおり正しいので除く。
    """
    out: list[tuple[str, str]] = []
    for table in D.Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, DateTime) and not col.type.timezone:
                out.append((table.name, col.name))
    return sorted(out)


def db_naive_datetime_columns(db) -> list[tuple[str, str]]:
    """DB 実体から (table, column) を引く（metadata に無い表を炙り出すため）。"""
    rows = db.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone' "
        "ORDER BY table_name, column_name"
    )).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── 計測 ────────────────────────────────────────────────────────────────────

def probe_column(db, table: str, col: str, cutoff: datetime) -> dict:
    """1列ぶんの対象行数・前後の min/max・guard band・日付跨ぎ件数を測る。"""
    shift = SHIFT_EXPR.format(col=f'"{col}"')
    row = db.execute(text(
        f'SELECT count(*) FILTER (WHERE "{col}" >= :cut) AS n_target, '
        f'       count(*) FILTER (WHERE "{col}" >= :band AND "{col}" < :cut) AS n_guard, '
        f'       min("{col}") FILTER (WHERE "{col}" >= :cut) AS min_before, '
        f'       max("{col}") FILTER (WHERE "{col}" >= :cut) AS max_before, '
        f'       count(*) FILTER (WHERE "{col}" >= :cut '
        f'                          AND ({shift})::date <> "{col}"::date) AS n_date_shift '
        f'FROM public."{table}"'
    ), {"cut": cutoff, "band": cutoff - timedelta(hours=GUARD_BAND_HOURS)}).one()
    n_target, n_guard, min_before, max_before, n_date_shift = row
    return {
        "table": table,
        "column": col,
        "n_target": int(n_target or 0),
        "n_guard": int(n_guard or 0),
        "n_date_shift": int(n_date_shift or 0),
        "min_before": min_before,
        "max_before": max_before,
        "min_after": min_before - timedelta(hours=9) if min_before else None,
        "max_after": max_before - timedelta(hours=9) if max_before else None,
    }


def shift_column(db, table: str, col: str, cutoff: datetime) -> int:
    """1列を引き直す。**生 SQL で更新する**（ORM 経由だと `financial_records.updated_at`
    の `onupdate` が発火して現在時刻で潰れる）。"""
    shift = SHIFT_EXPR.format(col=f'"{col}"')
    res = db.execute(text(
        f'UPDATE public."{table}" SET "{col}" = {shift} WHERE "{col}" >= :cut'
    ), {"cut": cutoff})
    return int(res.rowcount or 0)


# ── ガード ──────────────────────────────────────────────────────────────────

def guard_local_target() -> None:
    """ローカル正本以外へは書かない（ADR-0038: Supabase の Postgres へ書き戻す経路は作らない）。"""
    if D.DB_TARGET != "local" or not D._is_local:
        raise SystemExit(
            f"接続先が local ではありません（FINAPP_DB_TARGET={D.DB_TARGET!r} / "
            f"is_local={D._is_local}）。このスクリプトはローカル正本専用です。"
        )


def guard_not_applied(db, force: bool) -> Optional[str]:
    """冪等スタンプ。2度掛けると 18 時間ずれる＝取り返しがつかない。"""
    stamp = D.get_setting(db, STAMP_KEY)
    if stamp and not force:
        raise SystemExit(
            f"既に適用済みです（app_settings.{STAMP_KEY}）:\n  {stamp}\n"
            "  2度掛けると 18 時間ずれます。本当にやり直すなら --force。"
        )
    return stamp


# ── 出力 ────────────────────────────────────────────────────────────────────

def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def print_report(probes: list[dict], orphans: list[tuple[str, str]],
                 cutoff: datetime, applied: bool) -> None:
    band_from = cutoff - timedelta(hours=GUARD_BAND_HOURS)
    print(f"cutoff      : {_fmt(cutoff)}（これ以降の行を JST→UTC で引き直す）")
    print(f"guard band  : {_fmt(band_from)} 〜 {_fmt(cutoff)} が空であること")
    print()

    hit = [p for p in probes if p["n_target"]]
    print(f"{'表.列':<44} {'対象':>7} {'帯':>4} {'日付跨ぎ':>8}  変換前 max        → 変換後 max")
    print("-" * 118)
    for p in hit:
        name = f'{p["table"]}.{p["column"]}'
        print(f'{name:<44} {p["n_target"]:>7} {p["n_guard"]:>4} {p["n_date_shift"]:>8}  '
              f'{_fmt(p["max_before"])} → {_fmt(p["max_after"])}')
    print("-" * 118)
    print(f"{'合計':<44} {sum(p['n_target'] for p in hit):>7}"
          f" {sum(p['n_guard'] for p in hit):>4} {sum(p['n_date_shift'] for p in hit):>8}")

    clean = [f'{p["table"]}.{p["column"]}' for p in probes if not p["n_target"]]
    if clean:
        print(f"\n対象ゼロ（触らない）: {len(clean)}列")
        print("  " + ", ".join(clean))

    if orphans:
        print("\n⚠ metadata に無いが naive timestamp を持つ表（このスクリプトは触らない）:")
        for t, c in orphans:
            print(f"  {t}.{c}")

    n_date = sum(p["n_date_shift"] for p in hit)
    if n_date:
        print(f"\n⚠ 日付が跨ぐ行が {n_date} 件あります。"
              "`macro_data.created_at` は実配信ラグの実測に**日付**で使われている"
              "（GOTCHAS.md / #447）ため、跨ぐぶんは lag_days の実測が 1 日動きます。")

    n_guard = sum(p["n_guard"] for p in probes)
    if n_guard:
        print(f"\n✗ guard band に {n_guard} 行あります＝cutoff の左に UTC 値が接近しており、"
              "誤って 9 時間ずらす危険があります。cutoff を調べ直してください。")
    elif not applied:
        print("\n✓ guard band は空。--apply で引き直せます。")


# ── CLI ─────────────────────────────────────────────────────────────────────

def _force_utf8_stdout() -> None:
    """cp932 コンソールへリダイレクトすると非 ASCII は UnicodeEncodeError で
    **出力済みの内容ごとクラッシュ**する。`main()` からだけ呼ぶ（import 時に
    差し替えると pytest のキャプチャが壊れる）。"""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")


def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="naive DateTime 列の JST 値を UTC へ引き直す（#565）")
    ap.add_argument("--apply", action="store_true",
                    help="実際に UPDATE する（既定はドライラン）")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                    help=f"この時刻以降の行を対象にする（ISO・既定 {DEFAULT_CUTOFF}）")
    ap.add_argument("--force", action="store_true",
                    help="適用済みスタンプを無視する（2度掛けは 18 時間ずれる）")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args()

    guard_local_target()
    cutoff = datetime.fromisoformat(args.cutoff)

    db = D.SessionLocal()
    try:
        stamp = guard_not_applied(db, args.force)
        targets = naive_datetime_columns()
        in_db = set(db_naive_datetime_columns(db))
        orphans = sorted(in_db - set(targets))
        targets = [tc for tc in targets if tc in in_db]

        probes = [probe_column(db, t, c, cutoff) for t, c in targets]
        n_guard = sum(p["n_guard"] for p in probes)
        n_target = sum(p["n_target"] for p in probes)

        if not args.json:
            print(f"接続先: {D.db_target_info()['db_label']}")
            if stamp:
                print(f"⚠ 適用済みスタンプを --force で無視します: {stamp}")
            print()

        applied: dict[str, int] = {}
        if args.apply:
            if n_guard:
                if not args.json:
                    print_report(probes, orphans, cutoff, applied=False)
                print("\n中止しました（1バイトも書いていません）。")
                return 2
            for t, c in targets:
                n = shift_column(db, t, c, cutoff)
                if n:
                    applied[f"{t}.{c}"] = n
            D.upsert_setting(db, STAMP_KEY, json.dumps({
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "cutoff": cutoff.isoformat(),
                "issue": 565,
                "rows": applied,
            }, ensure_ascii=False))
            db.commit()

        if args.json:
            print(json.dumps({
                "cutoff": cutoff.isoformat(),
                "applied": bool(args.apply),
                "n_target": n_target,
                "n_guard": n_guard,
                "orphans": [f"{t}.{c}" for t, c in orphans],
                "columns": [{k: (v.isoformat() if isinstance(v, datetime) else v)
                             for k, v in p.items()} for p in probes],
                "updated": applied,
            }, ensure_ascii=False, indent=2))
        else:
            print_report(probes, orphans, cutoff, applied=args.apply)
            if args.apply:
                print(f"\n✓ 適用しました: {sum(applied.values())}行 / {len(applied)}列")
                print(f"  app_settings.{STAMP_KEY} へ記録済み（2度掛け防止）")
            else:
                print("\n（ドライラン。1バイトも書いていません）")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
