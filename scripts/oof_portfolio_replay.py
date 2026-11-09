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

## 本番の設定の M-2（#1123・ADR-0070 決定 6）

上の M-2 は M-4 の基底（既定の設定）で、実際に買っている本番の設定（探索の 1 位）の M-2 ではない。そこで M-2 の行の
直後に `macro_gbdt:production`（表示は `M-2*`）を足す。設定は `plugin_tuned_params` を比較画面と同じ射影
（`model_comparison._production_config`）で写し、**単体の M-2 を本番と同じ手続き**（検証の取り方・単調制約・空欄埋め）
で回す。M-4 の基底に設定を渡す形は採らない（M-4 の基底は単調制約を読まず、早期打ち切りの検証も `rows` で本番の
`months` と違う＝本番の M-2 にならない）。

- **test 月を共通域にそろえる**: 単体の M-2 は M-1 より早く始まることがあり、3 か月おきの test 月の位置が共通域と
  ずれうる（ずれ方はパネルの月の並びで決まる。ずれると 1 期も重ならないことがある）。最初の学習期間だけを延ばして
  （`aligned_min_train`）共通域の最初の test 月から始める。学習の手続きは変わらない
- 共通域の外の予測は落とし、件数を `coverage` に出す。そろった期の数を出し、0 なら行を出さずに理由を出す
- `tuning_dry_run`・`tuning_objective_only` の内側で回す＝本番の μ̂（`macro_gbdt_scores`）を上書きしない
- 本番の設定は採点と同じ期間のデータで選んでいる＝**選択の楽観を含む**（`selection_optimism`・`PRODUCTION_NOTE`）
- 本番の設定が無い・そろえられない・回せないときは行を出さず、理由を表示と JSON の `production` に出す

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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, tuning_dry_run, tuning_objective_only  # noqa: E402
from model_comparison import VARIANT_PRODUCTION, _production_config, _safe_rollback  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_snapshots import _month_end_bound, month_end_indices, oof_backtest  # noqa: E402
from plugins.utils import coerce_params, walk_forward_cv_monthly  # noqa: E402
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

# 本番の設定の行（#1123）。鍵は比較画面・λ の参考と同じ `<name>:production`（model_comparison.VARIANT_PRODUCTION）
PRODUCTION_MODEL = "macro_gbdt"
PRODUCTION_KEY = f"{PRODUCTION_MODEL}:{VARIANT_PRODUCTION}"
PRODUCTION_NOTE = ("M-2* は本番の設定（探索の 1 位）の M-2 を単体の手続きのまま回した予測。最初の学習期間だけを延ばして"
                   "test 月を共通域にそろえた。設定は採点と同じ期間のデータで選んでいる＝選択の楽観を含む（#1123）")


class NotAligned(ValueError):
    """本番の設定の M-2 の test 月を共通域にそろえられない。"""


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


def aligned_min_train(yms: Sequence[str], first_test: str, min_train: int, embargo: int) -> int | None:
    """walk-forward の最初の test 月を `first_test` にする最初の学習期間（月数）。そろえられなければ None。

    test 月は `yms[min_train + embargo]` から step おき（`walk_forward_cv_monthly`）。最初の学習期間を延ばすと、
    学習の手続きはそのままで test 月だけが後ろへずれる。本番の最小より短くはしない（短くなるなら None）。
    """
    yms = sorted(yms)
    if first_test not in yms:
        return None
    shifted = yms.index(first_test) - embargo
    return shifted if shifted >= min_train else None


def production_predictions(panel: tuple, db, test_yms: Sequence[str]) -> tuple[dict | None, dict]:
    """本番の設定の M-2 の OOF 予測 `{(ym, 社): 予測}` と付記。回せなければ予測は None で付記に理由（#1123）。

    設定は `plugin_tuned_params` を比較画面と同じ射影（`_production_config`）で写し、`predictions_with_params` で回す。
    """
    plugin = get_plugin(PRODUCTION_MODEL)
    params, info = _production_config(db, plugin, PRODUCTION_MODEL, {})
    if params is None:
        return None, {"available": False, **info}
    preds, _oof, extra = predictions_with_params(panel, db, test_yms, params)
    if preds is None:
        return None, {"available": False, **info, **extra}
    return preds, {"available": True, **info, **extra}


def predictions_with_params(panel: tuple, db, test_yms: Sequence[str],
                            params: dict) -> tuple[dict | None, dict | None, dict]:
    """M-2 を params で単体の手続きのまま回した OOF 予測 `{(ym, 社): 予測}`、その回の `oof_backtest`（M-2 自身の
    母集団）、付記。そろえられない・回せないときは予測と oof は None で、付記に理由（#1123・#1155）。

    `oof_backtest` へ渡る予測と社の並び（`meta_by_ym`）を横取りする。変えるのは最初の学習期間だけで、test 月を
    `test_yms`（M-4 の共通域）の最初の月から始める（モジュールの docstring）。読み込み済みのパネルを使い回す（DB から
    引き直さない。空欄埋めは M-2 自身が行う）。`tuning_dry_run`・`tuning_objective_only` の内側で回す＝本番の μ̂ と
    探索の結果を上書きしない。種は呼び出し側が `plugins.tuning.seed_override` で変えられる（既定は M-2 の既定の種）。
    """
    plugin = get_plugin(PRODUCTION_MODEL)
    test_yms = sorted(test_yms)
    if not test_yms:
        return None, None, {"reason": "not_aligned", "error": "共通域に test 月が無い"}

    def aligned_wf(samples_by_ym, feature_names, **kwargs):
        m = aligned_min_train(list(samples_by_ym), test_yms[0], kwargs.get("min_train_months", 18),
                              kwargs.get("embargo_months", 0))
        if m is None:
            raise NotAligned(f"test 月を共通域の最初の月（{test_yms[0]}）にそろえられない")
        return walk_forward_cv_monthly(samples_by_ym, feature_names, **{**kwargs, "min_train_months": m})

    prices_by_co, fin_by_co, companies, macro_cache = panel
    try:
        with patch("plugins.macro_gbdt.load_data", return_value=(prices_by_co, fin_by_co, companies)), \
             patch("plugins.macro_gbdt.preload_macro", return_value=macro_cache), \
             patch("plugins.macro_gbdt.walk_forward_cv_monthly", side_effect=aligned_wf), \
             patch("plugins.macro_gbdt.oof_backtest", wraps=oof_backtest) as spy, \
             tuning_dry_run(), tuning_objective_only():
            result = plugin.execute(coerce_params(plugin.params_schema(), params), db)
    except NotAligned as e:
        return None, None, {"reason": "not_aligned", "error": str(e)}
    except Exception as e:  # noqa: BLE001 — 1 つの設定が回せなくても呼び出し側の他の行は出す
        _safe_rollback(db)
        return None, None, {"reason": "error", "error": str(e)}
    if spy.call_args is None:
        return None, None, {"reason": "error", "error": "M-2 の OOF を捕まえられなかった（oof_backtest が呼ばれていない）"}
    resid = spy.call_args.args[0]
    meta = spy.call_args.kwargs.get("meta_by_ym") or {}
    preds = {(ym, sid): float(yh) for ym, rows in resid.items()
             for (yh, _y), (sid, _industry) in zip(rows, meta.get(ym, [])) if sid is not None}
    matched = sorted(set(resid) & set(test_yms))
    if not matched:
        return None, None, {"reason": "not_aligned", "error": "test 月が共通域と 1 期も重ならない"}
    oof = result.get("oof_backtest") if isinstance(result, dict) else None
    return preds, oof, {"n_periods_target": len(test_yms), "n_periods_matched": len(matched)}


def run_replays(common: dict, bases: list, prices_by_co: dict, db, models: list[str],
                sell_source: str, top_k: int, sell_k: int, cost_bps: float,
                production: dict | None = None, extra_m2: dict | None = None) -> dict:
    """買いの並び（models）ごとに OOF 運用再現を回す。

    `production`（本番の設定の M-2 の `{(ym, 社): 予測}`・#1123）があり models に M-2 が入っていれば、M-2 の行の
    直後に `PRODUCTION_KEY` の行を足す。共通域（y がある鍵）の外の予測は落とし、件数を `coverage` に出す。
    `extra_m2`（`{行の名前: M-2 の予測}`・#1155 の時間順に選んだ M-2 など）は、M-2 の並び方（`buy_scores`）で
    末尾に足す（models に M-2 が無くても足す）。
    """
    preds, y = common_to_dicts(common, bases)
    book = PriceBook(prices_by_co)
    sell = preds[sell_source]

    def one(name: str, pred: dict) -> dict:
        buy, ordering = buy_scores(name, pred, y, prices_by_co, db)
        periods = sorted({ym for ym, _ec in buy.keys() & sell.keys()})
        intervals = replay(periods, buy, sell, book.period_return, top_k, sell_k, cost_bps)
        return {"ordering": ordering, "summary": summarize(intervals, top_k), "intervals": intervals}

    def coverage(pred: dict, inside: dict) -> dict:
        return {"n_rows": len(inside), "n_outside_common": len(pred) - len(inside),
                "n_common_without_prediction": len(y.keys() - inside.keys())}

    out: dict[str, dict] = {}
    for name in models:
        out[name] = one(name, preds[name])
        if name == PRODUCTION_MODEL and production is not None:
            inside = {k: v for k, v in production.items() if k in y}
            out[PRODUCTION_KEY] = {
                **one(name, inside), "selection_optimism": True,
                "coverage": {"n_rows": len(inside), "n_outside_common": len(production) - len(inside),
                             "n_common_without_prediction": len(preds[name].keys() - inside.keys())},
            }
    for key, pred in (extra_m2 or {}).items():
        inside = {k: v for k, v in pred.items() if k in y}
        out[key] = {**one(PRODUCTION_MODEL, inside), "coverage": coverage(pred, inside)}
    return out


def _pct(v) -> str:
    return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "n/a"


def _label(name: str) -> str:
    """表示名。本番の設定の行（`<name>:production`）は `M-2*`。"""
    base, _, variant = name.partition(":")
    return SHORT[base] + ("*" if variant else "")


def _print(results: dict, sell_source: str, top_k: int, sell_k: int, cost_bps: float,
           production: dict | None = None) -> None:
    print(f"\n=== OOF 運用再現（#1045・参考）K={top_k} k={sell_k} 片道 {cost_bps:g}bp・"
          f"売りの並び={SHORT[sell_source]} の mu ===", flush=True)
    print(f"注意: {DISCLAIMER}", flush=True)
    print(f"注意: {M4_BASES_CAVEAT}", flush=True)
    print(f"注意: {DELIST_NOTE}", flush=True)
    if production and production.get("available"):
        print(f"注意: {PRODUCTION_NOTE}", flush=True)
        print(f"  M-2* の設定: 探索 {production.get('tuned_at')}・そろった期 {production.get('n_periods_matched')}"
              f"/{production.get('n_periods_target')}・パネルの変化 {production.get('panel_changed')}・"
              f"射影で既定へ戻した設定 {production.get('stale_params') or 'なし'}", flush=True)
    elif production:
        print(f"注意: M-2*（本番の設定）は出さない: {production.get('reason')}: {production.get('error')}",
              flush=True)
    print(f"  {'買いの並び':<10}{'年率(前)':>10}{'年率(後)':>10}{'全社平均':>10}{'超過':>10}"
          f"{'最大下落':>10}{'入替率':>8}{'区間':>6}{'途切れ':>7}", flush=True)
    for name, res in results.items():
        s = res["summary"]
        turn = f"{s['turnover']:>8.3f}" if s.get("turnover") is not None else f"{'n/a':>8}"
        dd = f"{s['max_drawdown'] * 100:.2f}%" if s.get("max_drawdown") is not None else "n/a"
        print(f"  {_label(name):<10}{_pct(s.get('annual_gross')):>10}{_pct(s.get('annual_net')):>10}"
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
        prod_preds, prod_info = None, None
        if PRODUCTION_MODEL in args.models:
            # 本番の設定の M-2（#1123）。test 月は M-4 の共通域にそろえる
            prod_preds, prod_info = production_predictions(panel, db, sorted(run["common"]))
        results = run_replays(run["common"], run["bases"], panel[0], db, args.models,
                              args.sell_source, args.top_k, args.sell_k, args.cost_bps,
                              production=prod_preds)
    finally:
        db.close()
    _print(results, args.sell_source, args.top_k, args.sell_k, args.cost_bps, prod_info)

    payload = {
        "disclaimer": DISCLAIMER,
        "caveats":    [M4_BASES_CAVEAT, DELIST_NOTE] + ([PRODUCTION_NOTE] if prod_preds is not None else []),
        "params":     {"top_k": args.top_k, "sell_k": args.sell_k, "cost_bps": args.cost_bps,
                       "sell_source": args.sell_source, "models": args.models, "smoke": args.smoke},
        # 本番の設定の M-2 の行の付記（設定・探索日・そろった期・回せなかった理由）。M-2 を選ばない回は None
        "production": prod_info,
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
