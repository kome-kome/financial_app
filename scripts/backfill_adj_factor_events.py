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
3. 第1経路のイベントのうち、イベント窓が契約窓に完全に収まる社（`absence`・#668）
   ＝ 公式に分割が無いと確かめられれば、偽陽性として係数表から外れうる社

取った社には**イベントに加えて「バーを受け取った区間」を `jquants_adj_factor_coverage` へ残す**（#668）。
区間は要求した期間ではなく返ってきたバーで決める——J-Quants が扱わない社は 0 本で返り、「イベントが
無い」と同じ形になるので、0 本の社は区間を書かず報告に並べる（公式で確かめられない社）。

候補にならない社の過去のイベントは、将来その社が倍率待ちになる時点（＝決算の後）には夜間 catchup が
既に残している。取りこぼした晩があれば、この CLI を回し直せば埋まる（upsert なので冪等）。

## 実行

    python -m scripts.backfill_adj_factor_events --dry-run     # 対象社だけ出す（契約窓の学習に1リクエスト）
    python -m scripts.backfill_adj_factor_events               # 取り込む（約100社で約35分）
    python -m scripts.backfill_adj_factor_events --only E03137,E01234
    python -m scripts.backfill_adj_factor_events --reasons absence   # 第1経路の不在確認だけ（約93社・約31分）

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
import corporate_actions as C  # noqa: E402
from scripts import measure_split_valuation_bias as M  # noqa: E402


# ── 純関数（ネットワークにも DB にも触らない・ここがテスト対象）─────────────────

#: 対象社を選ぶ理由。`--reasons` の既定は全部。
REASONS = ("awaiting", "crosscheck", "absence")


def choose_targets(events: Sequence[C.ShareEvent], stats: Mapping,
                   coverage: tuple[str, str], *,
                   reasons: Sequence[str] = REASONS) -> dict[str, list[str]]:
    """取り込む社と、その理由（`awaiting` / `crosscheck` / `absence`）。社は edinet_code 昇順。

    `awaiting` は倍率待ちのペアを持つ社（契約窓で絞らない——窓の外なら取得しても空が返るだけで、
    判定を CLI 側に書き写すより安い）。`crosscheck` は翌年の株数で倍率を決めた第2経路イベントの
    うち、イベント窓が契約窓と**重なる**社（`in_coverage(mode="partial")`・突合と同じ規則）。
    `absence` は第1経路のイベントのうち、イベント窓が契約窓に**完全に収まる**社
    （`in_coverage(mode="full")`・#668）。重なるだけでは、窓の外側で起きた分割を不在と区別できない。

    `events` には**公式の不在で外す前**の検出結果を渡す（外した後の結果からは、外すべき社が選べない）。
    `reasons` に無い理由では選ばない（既定は全部）。
    """
    unknown = set(reasons) - set(REASONS)
    if unknown:
        raise ValueError(f"未知の理由: {sorted(unknown)}（{', '.join(REASONS)} から選ぶ）")
    picked: dict[str, set[str]] = {}
    if "awaiting" in reasons:
        for a in (stats.get("bps_path") or {}).get("awaiting_magnitude") or ():
            picked.setdefault(a["edinet_code"], set()).add("awaiting")
    for e in events:
        if ("crosscheck" in reasons and e.source == "bps" and e.lagged_sh_ratio is not None
                and C.in_coverage(e, coverage, mode="partial")):
            picked.setdefault(e.edinet_code, set()).add("crosscheck")
        if ("absence" in reasons and e.source == "shares"
                and C.in_coverage(e, coverage, mode="full")):
            picked.setdefault(e.edinet_code, set()).add("absence")
    return {ec: sorted(r) for ec, r in sorted(picked.items())}


def coverage_rows(ec: str, jq_code: str, spans: Sequence[tuple[str, str, int]]) -> list[dict]:
    """`bars_spans` の出力を `upsert_jquants_adj_factor_coverage` の行へ直す。0 本なら `[]`。"""
    return [{"edinet_code": ec, "first_bar_date": d0, "last_bar_date": d1, "n_bars": n,
             "jq_code": jq_code} for d0, d1, n in spans]


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
    D.JQuantsAdjFactorCoverage.__table__.create(bind=D.engine, checkfirst=True)

    cover = await M.learn_coverage()
    print(f"契約窓: {cover[0]} 〜 {cover[1]}", flush=True)

    db = D.SessionLocal()
    try:
        if args.only:
            targets = {ec.strip(): ["only"] for ec in args.only.split(",") if ec.strip()}
            n_awaiting = None
        else:
            rows = M.load_annual_rows(db)
            # 公式も取得記録も渡さない＝不在で外す前の検出結果（`choose_targets` の約束）。
            events, stats = C.detect_events(rows)
            targets = choose_targets(events, stats, cover, reasons=args.reasons)
            n_awaiting = len(stats["bps_path"]["awaiting_magnitude"])
        meta = M.load_company_meta(db, list(targets))
        no_sec = sorted(ec for ec in targets if not (meta.get(ec) or ("", ""))[0])
        fetchable = [(ec, meta[ec][0]) for ec in targets if ec not in no_sec]
        print(f"対象 {len(targets)}社（倍率待ちのペア {n_awaiting}件・"
              f"証券コード無しで取れない {len(no_sec)}社）"
              f"・見込み {len(fetchable) * JQUANTS_RATE_SLEEP / 60:.0f}分", flush=True)

        report = {"coverage": list(cover), "n_targets": len(targets),
                  "n_awaiting_pairs": n_awaiting, "no_sec_code": no_sec,
                  "targets": targets, "stored": {}, "spans": {}, "no_bars": [],
                  "reasons": list(args.reasons), "dry_run": bool(args.dry_run)}
        if args.dry_run:
            return report

        n_rows = 0
        for i, (ec, sec) in enumerate(fetchable, 1):
            if i > 1:
                await asyncio.sleep(JQUANTS_RATE_SLEEP)
            # 1社ずつ取って**その場で書く**。途中で 403 / 5xx が出ても、取れた社は残る。
            got, spans = (await M.fetch_official([(ec, sec)], cover)).get(ec) or ([], [])
            if got:
                n_rows += D.upsert_jquants_adj_factor_events(db, [
                    {"edinet_code": ec, "event_date": d, "adj_factor": f, "jq_code": f"{sec}0"}
                    for d, f in got])
                report["stored"][ec] = got
            # **イベントと区間は同じ commit で書く**（#668）。区間だけ残ってイベントが欠けると、
            # 検出器は「バーを受け取ったのにイベントが無い」と読み、本物の分割を外す。
            D.upsert_jquants_adj_factor_coverage(db, coverage_rows(ec, f"{sec}0", spans))
            db.commit()
            if spans:
                report["spans"][ec] = [list(sp) for sp in spans]
            else:
                report["no_bars"].append(ec)
            print(f"[{i}/{len(fetchable)}] {ec} {sec} イベント {len(got)}件 / バー "
                  f"{sum(n for _, _, n in spans)}本 区間 {len(spans)}", flush=True)
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
    print(f"バーを受け取った区間を残した社 {len(rep['spans'])}")
    if rep["no_bars"]:
        # 0 本の社は区間を書いていない＝公式の不在で外れることは無い（今日までどおり採られる）。
        print(f"公式のバーが0本で確かめられない社 {len(rep['no_bars'])}: {', '.join(rep['no_bars'])}")
    print("※ 係数表へは今夜の夜間バッチ（rebuild_split_adjustment_factors）が反映する。")


def _parse_reasons(v: str) -> tuple[str, ...]:
    picked = tuple(r.strip() for r in v.split(",") if r.strip())
    unknown = set(picked) - set(REASONS)
    if not picked or unknown:
        raise argparse.ArgumentTypeError(
            f"理由は {', '.join(REASONS)} から選ぶ（指定: {v!r}）")
    return picked


def main(argv: Optional[Sequence[str]] = None) -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="公式 AdjFactor のイベントと取得区間を契約窓ぶん取り込む（#661・#668）")
    ap.add_argument("--only", help="edinet_code をカンマ区切りで指定（対象選びを省く）")
    ap.add_argument("--reasons", type=_parse_reasons, default=REASONS,
                    help=f"対象社を選ぶ理由をカンマ区切りで（既定: {','.join(REASONS)}）")
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
