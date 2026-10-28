"""OOF 運用再現（#1045・親 #1040・ADR-0069 の「参考」）。

実運用（画面の上位 K 社を買い、保有の中の下位 k 社を売って入れ替える）を、無リークの OOF 予測だけで
時系列に繰り返したことにして、年率・最大下落・入替率・コスト控除後・全社平均に対する超過を出す。

**過去の経路は 1 本しかなく、運とモデルの良さを区別できない。参考で、判定・ゲートには使わない**
（`DISCLAIMER` を出力と JSON に残す）。名前は「OOF 運用再現」で、`/api/backtest`（as-of の模擬売買・
終端が今日で共通の 1 断面＝検定力が無い・ADR-0041）とは別物（CONTEXT.md）。

## 手続き

- 予測は `sell_mu_source_bakeoff --in-layer`（#1043）と同じく M-4 を 1 回回して共通域の社ごとの OOF を取る
  （`_run_config(keep_common=True)`）。M-4 の基底は #1058 で本番の設定にそろえた（`M4_BASES_CAVEAT`）
- 買いの並びは各モデルの画面の既定（M-1 / M-2 は U = μ̂ − λR・生の R2、λ=0 の M-6 は μ̂＝`buy_scores`）。
  売りの並びは `--sell-source` の μ̂ の低い順（既定は sell_ranking の既定の μ の出所＝M-6）。−R_macro は
  過去の時点で作り直せないので使わない
- test 期（3 か月おき）ごとに: 予測が無くなった保有を外す → 残りの中で売りの並びの下位 k 社を売る →
  買いの並びの上位から K 社になるまで補充する（その期に売った社は買い戻さない）。最初の期は上位 K 社を買う
- 期間のリターンは **y（52 週先）を繋げない**（四半期で繋ぐとラベルが重複する）。各社の月末の週の終値
  （`month_end_indices`＝学習ラベルの基準週と同じ）の比を test 期の間で取る。後の月に株価が無い社は、その
  月末以前の最後の終値で手仕舞ったとみなす（`DELIST_NOTE`・件数を出す）
- 保有は毎期等金額にそろえる前提。コストは片道 `cost_bps` ×（売った社数＋買った社数）÷ K を入れ替えの
  たびに次の区間のリターンから引く（値動きの比率のずれを直す売買のコストは数えない）
- 全社平均＝各区間の始めの期に予測がある全社を等金額で持った場合（コスト無し）

日中枠 `measure:oof-replay`（並走に敏感）で単独に回す。結果は `scripts/.cache/oof_portfolio_replay.json`。

実行:
    python -m scripts.oof_portfolio_replay                      # 既定 K=10・k=2・10bp
    python -m scripts.oof_portfolio_replay --top-k 30 --sell-k 6 --cost-bps 20
    python -m scripts.oof_portfolio_replay --smoke --allow-full-pull   # 間引きパネルで動作確認
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from plugins.macro_snapshots import _month_end_bound, month_end_indices  # noqa: E402
from scripts._cache import set_refresh  # noqa: E402
from scripts.ensemble_base_bakeoff import _load_panel, _run_config  # noqa: E402
from scripts.sell_mu_source_bakeoff import (  # noqa: E402
    BASES, CANDIDATE, M4_BASES_CAVEAT, SHORT, buy_scores, common_to_dicts,
)

_OUT = Path(__file__).resolve().parent / ".cache" / "oof_portfolio_replay.json"

# 既定値（引数で変えられる）。K=10 は ADR-0069 の参考の K の 1 つ、k は #1043 と同じく保有の 20%、
# 10bp は日本株の片道コストの目安として置いた値（実測ではない）。
DEFAULT_TOP_K = 10
DEFAULT_SELL_K = 2
DEFAULT_COST_BPS = 10.0

DISCLAIMER = ("過去の経路は 1 本しかなく、運とモデルの良さを区別できない。参考で、判定・ゲートには使わない"
              "（ADR-0069）")
DELIST_NOTE = ("株価が途切れた保有は、その月末以前の最後の終値で手仕舞ったとみなす（最後の取引より後の損失は"
               "捉えない＝成績は楽観側にずれる）")


class PriceBook:
    """社ごとの月末の週の終値と、ある月末以前の最後の終値を引く。

    月末の週は `month_end_indices`（学習ラベルの基準週と同じ定義）で決める。その月に足が無い社
    （上場廃止・売買停止）は、`_month_end_bound` 以前の最後の正の終値へ倒し、「途切れた」と印を付ける。
    """

    def __init__(self, prices_by_co: dict):
        self._month_end: dict[str, dict[str, float]] = {}
        self._dates: dict[str, list[str]] = {}
        self._closes: dict[str, list[float]] = {}
        for ec, rows in prices_by_co.items():
            dates = [r.trade_date for r in rows]
            self._month_end[ec] = {dates[i][:7]: rows[i].close_last for i in month_end_indices(dates)}
            valid = [(r.trade_date, r.close_last) for r in rows if r.close_last and r.close_last > 0]
            self._dates[ec] = [d for d, _c in valid]
            self._closes[ec] = [c for _d, c in valid]

    def close_at(self, ec: str, ym: str) -> tuple[float | None, bool]:
        """`(終値, 途切れたか)`。月末の週の終値が正ならそれ、無ければ月末以前の最後の正の終値（途切れた）。"""
        c = (self._month_end.get(ec) or {}).get(ym)
        if c and c > 0:
            return float(c), False
        dates = self._dates.get(ec) or []
        hi = bisect.bisect_right(dates, _month_end_bound(ym))
        if hi == 0:
            return None, False
        return float(self._closes[ec][hi - 1]), True

    def period_return(self, ec: str, ym0: str, ym1: str) -> tuple[float | None, bool]:
        """ym0 の月末から ym1 の月末までのリターンと、ym1 で株価が途切れていたか。

        ym0 の月末の終値が無い社（その期に予測があれば起きない）は None。
        """
        c0, stale0 = self.close_at(ec, ym0)
        if c0 is None or stale0:
            return None, False
        c1, stale1 = self.close_at(ec, ym1)
        if c1 is None:
            return None, False
        return c1 / c0 - 1.0, stale1


def months_between(ym0: str, ym1: str) -> int:
    y0, m0 = map(int, ym0.split("-"))
    y1, m1 = map(int, ym1.split("-"))
    return (y1 - y0) * 12 + (m1 - m0)


def replay(periods: list[str], buy: dict, sell: dict,
           ret: Callable[[str, str, str], tuple[float | None, bool]],
           top_k: int, sell_k: int, cost_bps: float) -> list[dict]:
    """入れ替えを periods の順に繰り返し、区間ごとの明細を返す。

    buy = {(ym, 社): 買いのスコア}（高いほど買う）、sell = {(ym, 社): 売りのキー}（低いほど先に売る）。
    その期に予測がある社＝両方がそろう社。ret(社, ym0, ym1) は `(リターン, 途切れたか)`。
    区間 i は periods[i] で組んだ保有を periods[i+1] まで持った結果。最後の期では入れ替えない。
    同点は社の順で決める（入力の順に依らない）。
    """
    by_ym: dict[str, set] = {}
    for ym, ec in buy.keys() & sell.keys():
        by_ym.setdefault(ym, set()).add(ec)
    cost_rate = cost_bps / 10_000.0
    holdings: list[str] = []
    intervals: list[dict] = []
    for i, ym in enumerate(periods[:-1]):
        domain = by_ym.get(ym, set())
        forced = sorted(ec for ec in holdings if ec not in domain)
        keep = [ec for ec in holdings if ec in domain]
        sold = sorted(keep, key=lambda ec: (sell[(ym, ec)], ec))[:min(sell_k, len(keep))] if i else []
        kept = set(keep) - set(sold)
        candidates = sorted((ec for ec in domain if ec not in kept and ec not in sold),
                            key=lambda ec: (-buy[(ym, ec)], ec))
        bought = candidates[:max(0, top_k - len(kept))]
        holdings = sorted(kept | set(bought))
        cost = cost_rate * (len(sold) + len(forced) + len(bought)) / top_k

        nxt = periods[i + 1]
        rets, stopped, missing = [], 0, 0
        for ec in holdings:
            r, stale = ret(ec, ym, nxt)
            if r is None:
                missing += 1
                continue
            rets.append(r)
            stopped += stale
        bench = [r for r, _s in (ret(ec, ym, nxt) for ec in sorted(domain)) if r is not None]
        gross = statistics.mean(rets) if rets else 0.0
        intervals.append({
            "from": ym, "to": nxt, "months": months_between(ym, nxt),
            "holdings": holdings, "sold": sold, "forced": forced, "bought": bought,
            "gross": gross, "cost": cost, "net": gross - cost,
            "bench": statistics.mean(bench) if bench else 0.0,
            "n_domain": len(domain), "n_stopped": stopped, "n_missing": missing,
        })
    return intervals


def _annualized(rets: list[float], years: float) -> float | None:
    wealth = math.prod(1.0 + r for r in rets)
    if years <= 0 or wealth <= 0:
        return None
    return wealth ** (1.0 / years) - 1.0


def max_drawdown(rets: list[float]) -> float:
    """区間ごとの資産の推移（始め 1）で、それまでの高値からの最大の下落率。"""
    wealth = peak = 1.0
    worst = 0.0
    for r in rets:
        wealth *= 1.0 + r
        peak = max(peak, wealth)
        worst = max(worst, 1.0 - wealth / peak)
    return worst


def summarize(intervals: list[dict], top_k: int) -> dict:
    """年率（コスト前・後・全社平均）・超過・最大下落・入替率。年率は実際の経過月数で割る幾何平均。

    入替率は、2 回目以降の入れ替えで手放した社数（予測が無くなった社を含む）÷ K の平均。
    超過は、年率（コスト後）− 全社平均の年率。
    """
    if not intervals:
        return {"n_intervals": 0}
    years = sum(iv["months"] for iv in intervals) / 12.0
    ann_net = _annualized([iv["net"] for iv in intervals], years)
    ann_bench = _annualized([iv["bench"] for iv in intervals], years)
    turns = [(len(iv["sold"]) + len(iv["forced"])) / top_k for iv in intervals[1:]]
    return {
        "n_intervals":       len(intervals),
        "years":             round(years, 4),
        "annual_gross":      _annualized([iv["gross"] for iv in intervals], years),
        "annual_net":        ann_net,
        "annual_bench":      ann_bench,
        "annual_excess":     (ann_net - ann_bench) if ann_net is not None and ann_bench is not None else None,
        "max_drawdown":      max_drawdown([iv["net"] for iv in intervals]),
        "turnover":          statistics.mean(turns) if turns else None,
        "n_stopped":         sum(iv["n_stopped"] for iv in intervals),
        "n_missing":         sum(iv["n_missing"] for iv in intervals),
    }


def run_replays(common: dict, bases: list, prices_by_co: dict, db, models: list[str],
                sell_source: str, top_k: int, sell_k: int, cost_bps: float) -> dict:
    """買いの並び（models）ごとに OOF 運用再現を回す。"""
    preds, y = common_to_dicts(common, bases)
    book = PriceBook(prices_by_co)
    sell = preds[sell_source]
    out: dict[str, dict] = {}
    for name in models:
        buy, ordering = buy_scores(name, preds[name], y, prices_by_co, db)
        periods = sorted({ym for ym, _ec in buy.keys() & sell.keys()})
        intervals = replay(periods, buy, sell, book.period_return, top_k, sell_k, cost_bps)
        out[name] = {"ordering": ordering, "summary": summarize(intervals, top_k),
                     "intervals": intervals}
    return out


def _pct(v) -> str:
    return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "n/a"


def _print(results: dict, sell_source: str, top_k: int, sell_k: int, cost_bps: float) -> None:
    print(f"\n=== OOF 運用再現（#1045・参考）K={top_k} k={sell_k} 片道 {cost_bps:g}bp・"
          f"売りの並び={SHORT[sell_source]} の mu ===", flush=True)
    print(f"注意: {DISCLAIMER}", flush=True)
    print(f"注意: {M4_BASES_CAVEAT}", flush=True)
    print(f"注意: {DELIST_NOTE}", flush=True)
    print(f"  {'買いの並び':<10}{'年率(前)':>10}{'年率(後)':>10}{'全社平均':>10}{'超過':>10}"
          f"{'最大下落':>10}{'入替率':>8}{'区間':>6}{'途切れ':>7}", flush=True)
    for name, res in results.items():
        s = res["summary"]
        turn = f"{s['turnover']:>8.3f}" if s.get("turnover") is not None else f"{'n/a':>8}"
        dd = f"{s['max_drawdown'] * 100:.2f}%" if s.get("max_drawdown") is not None else "n/a"
        print(f"  {SHORT[name]:<10}{_pct(s.get('annual_gross')):>10}{_pct(s.get('annual_net')):>10}"
              f"{_pct(s.get('annual_bench')):>10}{_pct(s.get('annual_excess')):>10}"
              f"{dd:>10}{turn}{s.get('n_intervals', 0):>6}"
              f"{s.get('n_stopped', 0):>7}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.oof_portfolio_replay",
                                 description="OOF 運用再現（#1045・参考で判定には使わない）")
    ap.add_argument("--models", nargs="+", choices=BASES, default=list(BASES),
                    help="買いの並びに使うモデル（各モデルの画面の既定の並び）")
    ap.add_argument("--sell-source", choices=BASES, default=CANDIDATE,
                    help="売りの並びに使う μ̂ の出所（既定は sell_ranking の既定＝M-6）")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="保有する社数 K")
    ap.add_argument("--sell-k", type=int, default=DEFAULT_SELL_K, help="毎期売る保有の下位の社数 k")
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS, help="片道の売買コスト（bp）")
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows の既定 cp932 では μ̂ 等が出力できずクラッシュするため UTF-8 に固定する（他の bakeoff と同流儀）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    if not 0 <= args.sell_k <= args.top_k or args.top_k < 1:
        print(f"[oof_replay] NG: 0 <= --sell-k <= --top-k かつ --top-k >= 1 にする"
              f"（top_k={args.top_k} sell_k={args.sell_k}）", file=sys.stderr)
        return 2
    set_refresh(args.refresh_cache)

    panel = _load_panel(args)
    db = SessionLocal()
    try:
        run = _run_config("M-4(3基底・rank_ic_grid)", BASES, panel, db,
                          weight_method="rank_ic_grid", keep_common=True)
        results = run_replays(run["common"], run["bases"], panel[0], db, args.models,
                              args.sell_source, args.top_k, args.sell_k, args.cost_bps)
    finally:
        db.close()
    _print(results, args.sell_source, args.top_k, args.sell_k, args.cost_bps)

    payload = {
        "disclaimer": DISCLAIMER,
        "caveats":    [M4_BASES_CAVEAT, DELIST_NOTE],
        "params":     {"top_k": args.top_k, "sell_k": args.sell_k, "cost_bps": args.cost_bps,
                       "sell_source": args.sell_source, "models": args.models, "smoke": args.smoke},
        "results":    results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
