"""本番予測の月次の写しを、写した後に出た答えで採点する読むだけの計測 CLI（#1157）。

規則は `docs/prereg/live-oos.md`（本文は main へのマージで凍結）。判定は道具が `prereg_decision` に
機械的に出す。読み手は手で判定し直さない。

## 何を測るか

OOF の比較は、設定を選んだ期と同じ過去の期で採点している（探索した M-2 には選択の楽観が残る・#1155）。
`prediction_snapshots`（#1005）は本番の画面が出した予測をその時点の値のまま月に 1 回写しているので、写した**後**に
出た答えで採点すれば、選んだ期と採点の期が重ならない。M-6 と M-2（写した時点の本番の設定）の上位10社超過を、
同じ写し・同じ社・同じ答えで対にして比べる。

## 手続き

- **予測**: `prediction_snapshots` の全行。値は `prediction_snapshots.value_column`（μ̂ の源は `mu`・gap は
  `gap_ratio`）。ある写しのある源の値が全部空なら、その源のその写しだけを外す（#1005 の初回の M-1 は
  μ̂ 無しで書かれたことがある）
- **答え**: 社ごとの週次足で、**写した日以前の最後の足**を起点に 52 本先（13 本先）の終値との対数リターン
  （値上がりだけ・ADR-0073）。足の本数で数えるのは学習の答え（`closes[snap_idx + HORIZON_WEEKS]`）と同じ。
  起点の足が写した日より `ENDED_MARGIN_DAYS` 以上前の社は答えを作らない
- **成熟**: 写した日 + 週数が、計測した日と全社の最新の足の両方以前の写しだけを採点する
- **主審**: 52 週・起点は写した日・上場廃止した社は外す。M-6 と M-2 の共通域で `oof_backtest` の上位10社超過の
  写し別系列を取り、両方に値がある写しを日付順に並べた**最初の `N_JUDGE` 本だけ**で
  `paired_ic_significance`（平均ブロック長 `AVG_BLOCK`）。足りなければ判定しない（`accumulating`）
- **参考**: 各源自身の母集団の指標、上位層の売り側spread（`in_layer_short_side`）、上場廃止した社を最後の終値で
  足した版、起点を `data_asof` にした版、13 週の版

**DB へは書かない**（書く関数を呼ばない）。週次株価はローカルのキャッシュ（`.weekly_cache/`）を通して読む。

日中キュー `measure:live-oos`（並走に敏感・数分）で単独に回す。結果は `scripts/.cache/live_oos.json`。

終了コード: 0 = 完了（成熟した写しが 0 本でも 0）/ 2 = 事前登録の不一致 / 3 = 写しか週次株価が空。

実行:
    python -m scripts.measure_live_oos --prereg docs/prereg/live-oos.md --prereg-sha <凍結値>
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import prediction_snapshots as PS  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    HORIZON_WEEKS, LABEL_HORIZON_MONTHS, OPERATIONAL_TOP_K, _add_days, in_layer_short_side, oof_backtest,
)
from scripts import prereg  # noqa: E402
from scripts.index_exposure import MODEL_SOURCES  # noqa: E402
from scripts.measure_label_audit import ENDED_MARGIN_DAYS  # noqa: E402

PREREG = "docs/prereg/live-oos.md"
ISSUE = 1157
_OUT = ROOT / "scripts" / ".cache" / "live_oos.json"
EXIT_INPUT = 3

# 事前登録 §2・§3 の対象と量（本文と同じ値。変えるなら文書を凍結し直す）。
M6 = "macro_enet"
M2 = "macro_gbdt"
PAIR = (M6, M2)                       # 主審の対 M-6 − M-2（写した時点の本番の設定）
SHORT = {source: name for name, source in MODEL_SOURCES.items()}
ALPHA = 0.05                          # 主審 1 本（補正なし・ADR-0063）
TOP_K = OPERATIONAL_TOP_K             # 上位10社（ADR-0070）
MAIN_WEEKS = HORIZON_WEEKS            # 52 週＝学習の答えと同じ
CHECK_WEEKS = 13                      # 中間点検（判定に使わない）
N_JUDGE = 36                          # 判定に使う共通の写しの本数（日付順の最初の 36 本だけ）
AVG_BLOCK = LABEL_HORIZON_MONTHS      # 月次の写しの 52 週の答えは隣の 11 か月と重なる→平均ブロック長 12

START_SNAPSHOT = "snapshot"           # 起点＝写した日（主審）
START_ASOF = "data_asof"              # 起点＝予測が使ったデータの時点（参考・μ̂ の源だけ）
MAIN = "main"
# (名前, 週数, 起点, 上場廃止した社を最後の終値で足すか)
VARIANTS = (
    (MAIN, MAIN_WEEKS, START_SNAPSHOT, False),
    ("delisted_included", MAIN_WEEKS, START_SNAPSHOT, True),
    ("data_asof", MAIN_WEEKS, START_ASOF, False),
    ("check_13w", CHECK_WEEKS, START_SNAPSHOT, False),
)

# 答えの内訳
OK, ENDED, GAPS, RECENT, NO_START = "ok", "ended", "gaps", "recent", "no_start"


class InputMissing(RuntimeError):
    """写しか週次株価が空（終了コード 3）。"""


def _finite(v) -> bool:
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


def _short(source: str) -> str:
    return SHORT.get(source, source)


# ── 予測（写し）─────────────────────────────────────────────────────────────────

def collect_predictions(rows: Iterable[tuple]) -> tuple[dict, list]:
    """写しの行 `(snapshot_date, source, edinet_code, mu, gap_ratio, data_asof)` を
    `{源: {写した日: {社: (値, data_asof)}}}` にまとめる。

    値は `prediction_snapshots.value_column` の列。値が有限でない行は捨てる。ある写しのある源に行はあるのに
    値が 1 つも無いとき（#1005 の初回の M-1 は μ̂ 無しで書かれたことがある）は、その源のその写しを丸ごと外し、
    `missing`（[源, 写した日] の並び）に記録する（事前登録 §2）。
    """
    preds: dict = defaultdict(lambda: defaultdict(dict))
    seen: set = set()
    for sd, source, ec, mu, gap, asof in rows:
        seen.add((source, sd))
        v = gap if PS.value_column(source) == "gap_ratio" else mu
        if _finite(v):
            preds[source][sd][ec] = (float(v), asof)
    missing = sorted([source, sd] for source, sd in seen if sd not in preds.get(source, {}))
    return {s: dict(by_sd) for s, by_sd in preds.items()}, missing


# ── 答え ────────────────────────────────────────────────────────────────────────

def index_prices(prices: dict) -> dict:
    """`{社: [_WeeklyPX,...]}`（trade_date 昇順）→ `{社: (日付の並び, 終値の並び)}`。"""
    out = {}
    for ec, rows in prices.items():
        if rows:
            out[ec] = ([str(r.trade_date)[:10] for r in rows], [r.close_last for r in rows])
    return out


def global_last_date(px: dict) -> Optional[str]:
    return max((dates[-1] for dates, _ in px.values() if dates), default=None)


def answer(px_one: Optional[tuple], start: Optional[str], weeks: int, global_last: str,
           include_ended: bool = False) -> tuple[str, Optional[float]]:
    """社 1 つの答え `(内訳, 対数リターン)`（純関数）。

    起点の足 = `start` 以前の最後の足（`start` より `ENDED_MARGIN_DAYS` 以上前なら `no_start`）。
    `weeks` 本先の足があり両方の終値が正なら `ok`。無ければ `measure_label_audit.classify_no_future` と同じ
    分け方: 起点 + 週数が全社の最新の足から `ENDED_MARGIN_DAYS` 以内なら `recent`（収集の遅れとみなす）、
    系列の最後の足がそれより前なら `ended`（上場廃止・売買停止）、そうでなければ `gaps`（週の欠け）。
    `ended` は `include_ended` のときだけ、最後の正の終値で手仕舞った値を返す（#1195 の案 1 と同じ形）。
    """
    if px_one is None or not start:
        return NO_START, None
    dates, closes = px_one
    i = bisect.bisect_right(dates, start) - 1
    if i < 0 or dates[i] < _add_days(start, -ENDED_MARGIN_DAYS):
        return NO_START, None
    c0 = closes[i]
    if not (_finite(c0) and c0 > 0):
        return NO_START, None
    j = i + weeks
    if j < len(closes):
        c1 = closes[j]
        if _finite(c1) and c1 > 0:
            return OK, math.log(c1 / c0)
        return GAPS, None
    horizon_end = _add_days(dates[i], weeks * 7)
    if horizon_end > _add_days(global_last, -ENDED_MARGIN_DAYS):
        return RECENT, None
    if dates[-1] >= horizon_end:
        return GAPS, None
    if not include_ended:
        return ENDED, None
    last = next((c for c in reversed(closes[i:]) if _finite(c) and c > 0), None)
    return ENDED, (math.log(last / c0) if last else None)


def matured(snapshot_date: str, weeks: int, horizon_end: str) -> bool:
    """写した日 + 週数が `horizon_end`（計測した日と全社の最新の足の早い方）以前なら答えが出た写し。"""
    return _add_days(snapshot_date, weeks * 7) <= horizon_end


def answers_for(preds: dict, px: dict, sds: Sequence[str], weeks: int, start_mode: str,
                include_ended: bool, global_last: str) -> tuple[dict, dict]:
    """`y[源][(写した日, 社)]` と内訳 `counts[源][内訳]`。

    起点が写した日なら、同じ写しの同じ社はどの源でも同じ答えになる。起点を `data_asof` にする版は μ̂ の源だけ
    （gap の `data_asof` は決算期末で、写した日より最大 1 年ほど古い＝写す前の値動きまで数えてしまう）。
    `data_asof` が空の行は `no_start`（M-1 の写しは as-of を持たない・2026-11 の実測）。
    """
    y: dict = {}
    counts: dict = {}
    for source, by_sd in preds.items():
        if start_mode == START_ASOF and PS.value_column(source) == "gap_ratio":
            continue
        ys: dict = {}
        cnt: Counter = Counter()
        for sd in sds:
            for ec, (_v, asof) in (by_sd.get(sd) or {}).items():
                start = sd if start_mode == START_SNAPSHOT else (str(asof)[:10] if asof else None)
                kind, val = answer(px.get(ec), start, weeks, global_last, include_ended)
                cnt[kind] += 1
                if val is not None:
                    ys[(sd, ec)] = val
        y[source] = ys
        counts[source] = dict(cnt)
    return y, counts


# ── 採点 ────────────────────────────────────────────────────────────────────────

def metrics_row(o: dict) -> dict:
    k = (o.get("top_k") or {}).get(str(TOP_K)) or {}
    return {"top_k_excess_return": k.get("excess_return"), "mean_pct_rank": k.get("mean_pct_rank"),
            "precision": k.get("precision"), "lift": k.get("lift"),
            "rank_ic": (o.get("rank_ic") or {}).get("mean"), "n_periods": o.get("n_periods"),
            "top_k_n_periods": k.get("n_periods")}


def _residuals(pred_by_sd: dict, y: dict, sds: Sequence[str], ecs_by_sd: Optional[dict] = None) -> dict:
    out = {}
    for sd in sds:
        pred = pred_by_sd.get(sd) or {}
        ecs = sorted(ecs_by_sd[sd]) if ecs_by_sd is not None else sorted(pred)
        rows = [(pred[ec][0], y[(sd, ec)]) for ec in ecs if ec in pred and (sd, ec) in y]
        if rows:
            out[sd] = rows
    return out


def common_domain(preds: dict, y: dict, sds: Sequence[str]) -> dict:
    """写しごとに、対の両方の源に値があり、両方の答えがそろう社（超過の基準が両者で同じになる）。"""
    a, b = PAIR
    out = {}
    for sd in sds:
        pa = (preds.get(a) or {}).get(sd) or {}
        pb = (preds.get(b) or {}).get(sd) or {}
        ya, yb = y.get(a) or {}, y.get(b) or {}
        ecs = {ec for ec in pa.keys() & pb.keys() if (sd, ec) in ya and (sd, ec) in yb}
        if ecs:
            out[sd] = ecs
    return out


def sell_reference(preds: dict, y: dict, sds: Sequence[str]) -> dict:
    """上位層の売り側spread（参考）。上位層＝1 つ前の写しで M-6 の μ̂ が上位 1/5 だった社（ADR-0069）。

    源ごとに単独で測る（その源の値と答えがそろう社が母集団）。源が欠けた月があっても他の源を巻き込まない。
    対（M-6・M-2）は両方がそろう社で並べて測る。gap は売りの並びに使っていないので測らない。
    """
    buy_src = (preds.get(M6) or {})
    buy = {(sd, ec): v for sd in sds for ec, (v, _a) in (buy_src.get(sd) or {}).items()}

    def _keys(source: str) -> dict:
        by_sd = preds.get(source) or {}
        return {(sd, ec): v for sd in sds for ec, (v, _a) in (by_sd.get(sd) or {}).items()}

    def _summary(res: dict, name: str) -> dict:
        c = res["by_condition"][name]
        return {k: c[k] for k in ("mean", "hit_rate", "n_periods", "precision", "mis_sell_loss",
                                   "n_mis_sell_periods", "by_period")}

    by_source = {}
    for source in sorted(preds):
        if PS.value_column(source) == "gap_ratio" or source not in y:
            continue
        res = in_layer_short_side(buy, {source: _keys(source)}, y[source])
        by_source[source] = _summary(res, source)
    pair = None
    if all(s in preds and s in y for s in PAIR):
        ya, yb = y[PAIR[0]], y[PAIR[1]]
        y_common = {k: v for k, v in ya.items() if k in yb}
        res = in_layer_short_side(buy, {s: _keys(s) for s in PAIR}, y_common)
        pair = {s: _summary(res, s) for s in PAIR}
    return {"by_source": by_source, "pair": pair}


def score_variant(preds: dict, px: dict, snapshot_dates: Sequence[str], horizon_end: str, global_last: str,
                  weeks: int, start_mode: str, include_ended: bool) -> dict:
    sds = [sd for sd in snapshot_dates if matured(sd, weeks, horizon_end)]
    y, counts = answers_for(preds, px, sds, weeks, start_mode, include_ended, global_last)
    by_source = {}
    for source in sorted(y):
        resid = _residuals(preds[source], y[source], sds)
        by_source[source] = metrics_row(oof_backtest(resid)) if resid else None
    pair: dict = {"n_common_snapshots": 0, "metrics": {}, "top_k_excess_by_snapshot": {}}
    if all(s in y for s in PAIR):
        dom = common_domain(preds, y, sds)
        pair["n_common_snapshots"] = len(dom)
        pair["n_rows_by_snapshot"] = {sd: len(ecs) for sd, ecs in sorted(dom.items())}
        for s in PAIR:
            resid = _residuals(preds[s], y[s], sorted(dom), dom)
            o = oof_backtest(resid) if resid else {}
            pair["metrics"][s] = metrics_row(o) if o else None
            pair["top_k_excess_by_snapshot"][s] = (
                (o.get("top_k_excess_return_by_period") or {}).get(str(TOP_K)) or {})
    return {"weeks": weeks, "start": start_mode, "delisted_included": include_ended,
            "matured_snapshots": sds, "answers": counts, "by_source": by_source, "pair": pair,
            "sell": sell_reference(preds, y, sds) if start_mode == START_SNAPSHOT else None}


# ── 判定 ────────────────────────────────────────────────────────────────────────

def _verdict(sig: Optional[dict]) -> str:
    if not sig or not sig.get("significant"):
        return "0"
    return "+" if sig["mean"] > 0 else "-"


def decide(series_a: dict, series_b: dict) -> dict:
    """事前登録 §4 の判定。両方に値がある写しを日付順に並べた最初の `N_JUDGE` 本だけで、M-6 − M-2 の
    上位10社超過を対にして検定する（両側・α 0.05・平均ブロック長 12）。足りなければ検定しない。

    最初の `N_JUDGE` 本に固定するので、そろった後はいつ回しても同じ判定になる（有意になった回だけを拾えない）。
    """
    common = sorted(set(series_a) & set(series_b))
    rule = {"alpha": ALPHA, "top_k": TOP_K, "n_judge": N_JUDGE, "avg_block": AVG_BLOCK,
            "pair": [_short(s) for s in PAIR], "n_common": len(common)}
    if len(common) < N_JUDGE:
        return {**rule, "judged_on": [], "sig": None, "verdict": None, "decision": "accumulating"}
    used = common[:N_JUDGE]
    sig = paired_ic_significance({p: series_a[p] for p in used}, {p: series_b[p] for p in used},
                                 alpha=ALPHA, avg_block=AVG_BLOCK)
    verdict = _verdict(sig)
    decision = {"+": "m6_better", "-": "m2_better"}.get(verdict, "no_difference")
    return {**rule, "judged_on": used, "sig": sig, "verdict": verdict, "decision": decision}


# ── まとめ ──────────────────────────────────────────────────────────────────────

def snapshot_inventory(preds: dict) -> dict:
    """`{源: {写した日: {"n", "data_asof_min", "data_asof_max"}}}`（値のある行だけ）。"""
    out: dict = {}
    for source, by_sd in sorted(preds.items()):
        out[source] = {}
        for sd, rows in sorted(by_sd.items()):
            asofs = sorted(str(a)[:10] for _v, a in rows.values() if a)
            out[source][sd] = {"n": len(rows), "data_asof_min": asofs[0] if asofs else None,
                               "data_asof_max": asofs[-1] if asofs else None}
    return out


def run(rows: Iterable[tuple], prices: dict, today: str) -> dict:
    """純関数の本体（DB に触らない）。`rows` は写しの行、`prices` は `load_weekly_prices_chunked` の戻り値。"""
    preds, missing = collect_predictions(rows)
    px = index_prices(prices)
    global_last = global_last_date(px)
    if not preds or global_last is None:
        raise InputMissing("写しか週次株価が空")
    horizon_end = min(today, global_last)
    snapshot_dates = sorted({sd for by_sd in preds.values() for sd in by_sd})
    variants = {name: score_variant(preds, px, snapshot_dates, horizon_end, global_last, weeks, start, ended)
                for name, weeks, start, ended in VARIANTS}
    main_pair = variants[MAIN]["pair"]["top_k_excess_by_snapshot"]
    decision = decide(main_pair.get(M6) or {}, main_pair.get(M2) or {})
    return {"today": today, "global_last": global_last, "horizon_end": horizon_end,
            "snapshot_dates": snapshot_dates, "missing_source_snapshots": missing,
            "snapshots": snapshot_inventory(preds), "variants": variants, "prereg_decision": decision}


# ── DB から読む（テストでは差し替える）────────────────────────────────────────

def load_inputs(db) -> tuple[list, dict]:
    """写しの全行と週次株価（読むだけ）。"""
    from database import PredictionSnapshot as S
    from plugins.macro_snapshots import load_weekly_prices_chunked

    rows = db.query(S.snapshot_date, S.source, S.edinet_code, S.mu, S.gap_ratio, S.data_asof).all()
    prices = load_weekly_prices_chunked(db, with_volume=False)
    return [tuple(r) for r in rows], prices


def _print(rep: dict) -> None:
    print(f"計測した日 {rep['today']} / 全社の最新の足 {rep['global_last']}", flush=True)
    for source, by_sd in rep["snapshots"].items():
        cells = ", ".join(f"{sd}:{v['n']}" for sd, v in by_sd.items())
        print(f"  写し {_short(source):<5} {cells}", flush=True)
    for source, sd in rep["missing_source_snapshots"]:
        print(f"  欠け {_short(source)} {sd}（値が全部空・この写しだけ外す）", flush=True)
    for name, v in rep["variants"].items():
        print(f"--- {name}（{v['weeks']} 週・起点 {v['start']}）成熟した写し {len(v['matured_snapshots'])} 本 / "
              f"共通の写し {v['pair']['n_common_snapshots']} 本", flush=True)
        for source, m in v["by_source"].items():
            if m:
                print(f"  {_short(source):<5} 上位{TOP_K}社超過 {m['top_k_excess_return']} "
                      f"rank-IC {m['rank_ic']} 期数 {m['n_periods']}", flush=True)
    d = rep["prereg_decision"]
    print(f"判定（事前登録）: {d['decision']}（共通の成熟した写し {d['n_common']} / {N_JUDGE} 本）", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.measure_live_oos",
                                 description="本番予測の月次の写しを写した後の答えで採点する（#1157）")
    prereg.add_arguments(ap)
    ap.add_argument("--today", default=None,
                    help="計測した日（YYYY-MM-DD・テスト用）。既定は JST の今日")
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)          # 不一致は DB を読む前に終了コード 2
    if Path(args.prereg).as_posix() != PREREG or not str(stamp.get("prereg_issue", "")).endswith(
            f"/issues/{ISSUE}"):
        print(f"[live_oos] NG: 事前登録は {PREREG}（Issue #{ISSUE}）。受け取った値: {args.prereg}",
              file=sys.stderr)
        return prereg.EXIT_MISMATCH
    today = args.today or PS.jst_today().isoformat()
    date.fromisoformat(today)                     # 形の確認（YYYY-MM-DD 以外は ValueError）

    from database import SessionLocal

    with SessionLocal() as db:
        rows, prices = load_inputs(db)
    try:
        rep = run(rows, prices, today)
    except InputMissing as e:
        print(f"[live_oos] NG: {e}", file=sys.stderr)
        return EXIT_INPUT
    _print(rep)

    payload = {
        **stamp,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "caveats": [
            "M-2 は写した時点の本番の設定（探索の周回が反映した設定）。写した後の答えで採点するので選択の楽観は入らない",
            "答えの起点は写した日（同じ写しの同じ社はどの源でも同じ答え）。data_asof 起点の版は参考",
            "判定は共通の成熟した写しの日付順の最初の 36 本だけで出す。それより前の実走は accumulating",
        ],
        "rule": {"pair": [_short(s) for s in PAIR], "alpha": ALPHA, "top_k": TOP_K, "n_judge": N_JUDGE,
                 "avg_block": AVG_BLOCK, "main_weeks": MAIN_WEEKS, "check_weeks": CHECK_WEEKS,
                 "ended_margin_days": ENDED_MARGIN_DAYS,
                 "variants": [list(v) for v in VARIANTS]},
        **rep,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
