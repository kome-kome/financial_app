"""公式 `AdjFactor` のイベントを契約窓の過去2年ぶん取り込む（#661・ADR-0055 決定4-5）。

## なぜ要るのか

分割補正の第2経路（1株指標だけが先に動き、株数は翌年に動く社）は、翌年の決算がまだ無い
最新年の倍率を公式 `AdjFactor` から取る。その値は毎晩の J-Quants catchup が
`jquants_adj_factor_events` へ残すが、catchup が見るのは `today-90〜today-80` の日付だけなので、
**#661 より前に契約窓を通り過ぎたイベントは表に無い**。この CLI はそこを一回きりで埋める。

係数表の作り直し（`rebuild_split_adjustment_factors`）はこの表を読むだけで J-Quants を叩かない。
取り込みを回さなくても補正は誤らず、倍率待ちのイベントが採られないまま残るだけである。

## 何社ぶん取るか

全銘柄を日付単位で2年ぶん取ると約163分かかるので、**使う社だけを社単位で取る**（1社1リクエスト
＋ 20秒）。対象は検出器（既定）の出力から選ぶ（`choose_targets`）:

1. 倍率待ち（`no_lagged_row`）の第2経路ペアを持つ社 ＝ 取り込めば補正が入りうる社
2. 翌年の株数から倍率を決めた第2経路イベントのうち、イベント窓が契約窓と重なる社
   ＝ 公式と翌年株数の食い違いを測る（交差検証）ための社

候補にならない社の過去のイベントは、将来その社が倍率待ちになる時点（＝決算の後）には夜間 catchup が
既に残している。取りこぼした晩があれば、この CLI を回し直せば埋まる（upsert なので冪等）。

## 実行

    python -m scripts.backfill_adj_factor_events --dry-run     # 対象社だけ出す（契約窓の学習に1リクエスト）
    python -m scripts.backfill_adj_factor_events               # 取り込む（約100社で約35分）
    python -m scripts.backfill_adj_factor_events --only E03137,E01234

**夜間バッチ（JST 17:20〜・約70分）と重ねない**。catchup と J-Quants のレート制限（約5回/分）を
取り合い、429 で両方の取得が欠ける。書き込み先はローカル正本だけ（ADR-0038）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector_utils import JQUANTS_RATE_SLEEP, force_utf8_stdout  # noqa: E402
from scripts import measure_split_valuation_bias as M  # noqa: E402


# ── 純関数（ネットワークにも DB にも触らない・ここがテスト対象）─────────────────

def choose_targets(events: Sequence[M.ShareEvent], stats: Mapping,
                   coverage: tuple[str, str]) -> dict[str, list[str]]:
    """取り込む社と、その理由（`awaiting` / `crosscheck`）。社は edinet_code 昇順。

    `awaiting` は倍率待ちのペアを持つ社（契約窓で絞らない——窓の外なら取得しても空が返るだけで、
    判定を CLI 側に書き写すより安い）。`crosscheck` は翌年の株数で倍率を決めた第2経路イベントの
    うち、イベント窓が契約窓と**重なる**社（`in_coverage(mode="partial")`・突合と同じ規則）。
    """
    reasons: dict[str, set[str]] = {}
    for a in (stats.get("bps_path") or {}).get("awaiting_magnitude") or ():
        reasons.setdefault(a["edinet_code"], set()).add("awaiting")
    for e in events:
        if (e.source == "bps" and e.lagged_sh_ratio is not None
                and M.in_coverage(e, coverage, mode="partial")):
            reasons.setdefault(e.edinet_code, set()).add("crosscheck")
    return {ec: sorted(r) for ec, r in sorted(reasons.items())}


# ── CLI ─────────────────────────────────────────────────────────────────────

async def _run(args) -> dict:
    import database as D

    if D.DB_TARGET != "local" or not D._is_local:
        raise SystemExit(f"接続先が local ではありません（{D.DB_TARGET!r}）。"
                         "このスクリプトはローカル正本専用です（ADR-0038）。")
    if not os.environ.get("JQUANTS_API_KEY"):
        raise SystemExit("環境変数 JQUANTS_API_KEY が未設定です")

    # 表を用意する。`init_db()` は VIEW の作り直しまで伴うので呼ばない（この表は VIEW に依存しない）。
    D.JQuantsAdjFactorEvent.__table__.create(bind=D.engine, checkfirst=True)

    cover = await M.learn_coverage()
    print(f"契約窓: {cover[0]} 〜 {cover[1]}", flush=True)

    db = D.SessionLocal()
    try:
        if args.only:
            targets = {ec.strip(): ["only"] for ec in args.only.split(",") if ec.strip()}
            n_awaiting = None
        else:
            rows = M.load_annual_rows(db)
            events, stats = M.detect_events(rows)
            targets = choose_targets(events, stats, cover)
            n_awaiting = len(stats["bps_path"]["awaiting_magnitude"])
        meta = M.load_company_meta(db, list(targets))
        no_sec = sorted(ec for ec in targets if not (meta.get(ec) or ("", ""))[0])
        fetchable = [(ec, meta[ec][0]) for ec in targets if ec not in no_sec]
        print(f"対象 {len(targets)}社（倍率待ちのペア {n_awaiting}件・"
              f"証券コード無しで取れない {len(no_sec)}社）"
              f"・見込み {len(fetchable) * JQUANTS_RATE_SLEEP / 60:.0f}分", flush=True)

        report = {"coverage": list(cover), "n_targets": len(targets),
                  "n_awaiting_pairs": n_awaiting, "no_sec_code": no_sec,
                  "targets": targets, "stored": {}, "dry_run": bool(args.dry_run)}
        if args.dry_run:
            return report

        n_rows = 0
        for i, (ec, sec) in enumerate(fetchable, 1):
            if i > 1:
                await asyncio.sleep(JQUANTS_RATE_SLEEP)
            # 1社ずつ取って**その場で書く**。途中で 403 / 5xx が出ても、取れた社は残る。
            got = (await M.fetch_official_events([(ec, sec)], cover)).get(ec) or []
            if got:
                n_rows += D.upsert_jquants_adj_factor_events(db, [
                    {"edinet_code": ec, "event_date": d, "adj_factor": f, "jq_code": f"{sec}0"}
                    for d, f in got])
                db.commit()
                report["stored"][ec] = got
            print(f"[{i}/{len(fetchable)}] {ec} {sec} イベント {len(got)}件", flush=True)
        report["n_rows"] = n_rows
        return report
    finally:
        db.close()


def print_report(rep: dict) -> None:
    print()
    print(f"契約窓 {rep['coverage'][0]} 〜 {rep['coverage'][1]} / 対象 {rep['n_targets']}社")
    if rep["no_sec_code"]:
        print(f"証券コード無しで取れない社: {', '.join(rep['no_sec_code'])}")
    if rep["dry_run"]:
        for ec, why in rep["targets"].items():
            print(f"  {ec}  {','.join(why)}")
        print("（ドライラン。取得も書き込みもしていない）")
        return
    print(f"公式イベントを持っていた社 {len(rep['stored'])} / 書いた行 {rep.get('n_rows', 0)}")
    print("※ 係数表へは今夜の夜間バッチ（rebuild_split_adjustment_factors）が反映する。")


def main(argv: Optional[Sequence[str]] = None) -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="公式 AdjFactor のイベントを契約窓ぶん取り込む（#661）")
    ap.add_argument("--only", help="edinet_code をカンマ区切りで指定（対象選びを省く）")
    ap.add_argument("--dry-run", action="store_true", help="対象社を出すだけで取得しない")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args(argv)

    rep = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
