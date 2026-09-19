"""時点再現の乖離率 — 学習パネルの各月末に「その時点で計算できた」gap_ratio を作る（#626・ADR-0057）。

本番の `gap_ratio` は、夜間に `sector_ols` が**各社の最新の通期行 × 当日の株価**で業種別回帰を
回した結果である。過去の月末について同じ意味の値を作るには、月末ごとに

1. その月末に見えていた各社の最新の通期行（期末＋45日・`macro_snapshots._find_applicable_fin`）
2. その月末の週次終値に分割補正係数 F を掛けた株価（提出当時の 1 株指標と基準を揃える）

で業種別回帰をやり直すしかない。年度ごとに回して `regression_results` へ保存する案は、実データで
先読みが2つ見つかったので採らなかった（ADR-0057）:

- **株価の先読み**: 過去行の `financial_records.stock_price` は、期末±3% に入るのが 2023年度 70%・
  2024年度 65% だけで、残りは「その年度が最新だった頃に毎晩上書きされた現在株価」＝最大1年先の値
- **係数の先読み**: 同じ年度の回帰に、最大11か月後に決算を迎える他社のデータが入る

## 守ること

- **`regression_results` へ書かない。** 本番の gap（画面・バックテスト・鮮度表示）と混ざらない
  ように、ここで作った値はメモリ上でパネルへ付けるだけにする。計算は `SectorOLSPlugin.predict_gaps`
  （保存しない経路）を通す。
- **株価は `financial_records.stock_price` を読まない。** 上の先読みそのものなので、月末の週次終値
  （`build_snapshots` と同じ `month_end_indices` の足）を使う。
- **F を掛ける。** 週次株価は分割を遡って調整済み、1 株指標は提出当時の基準（ADR-0055）。
  月末終値 × F（その行の年より後のイベントの積）で提出当時の基準へ戻る——月末より後の分割は
  調整済み株価で割られ F で掛け戻され、行と月末の間の分割は F にだけ入るので残る。
- **回帰の設定は `nightly_scores.NIGHTLY_PARAMS` を唯一の源にする。** 写すと本番を ols に戻した
  ときなどに、学習パネルの gap だけが古い設定で作られる。
- **回帰の母集団はその月末に株価がある全社**（パネルの標本＝52週先リターンがある社ではない）。
  本番の回帰も上場している全社で回っており、将来の上場廃止で母集団を削ると生存バイアスになる。

純関数ブロックは database・scripts を import しない（ttm_composite.py と同じ置き方）。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Iterable, Mapping

log = logging.getLogger(__name__)

# 回帰の設定を借りる夜間の producer 名（`nightly_scores.NIGHTLY_PARAMS` のキー）。
SECTOR_MODEL = "sector_ols"


# ── 純関数ブロック ──────────────────────────────────────────────────────────

def month_end_closes(price_rows: list) -> dict[str, tuple[str, float]]:
    """週次足（昇順）→ {ym: (月末の足の日付, 終値)}。月末の定義は `build_snapshots` と共有する。"""
    from plugins.macro_snapshots import month_end_indices

    dates = [r.trade_date for r in price_rows]
    out: dict[str, tuple[str, float]] = {}
    for i in month_end_indices(dates):
        close = price_rows[i].close_last
        if close is not None and close > 0:
            out[dates[i][:7]] = (dates[i], float(close))
    return out


def asof_records(fin_by_co: Mapping[str, list],
                 closes_by_co: Mapping[str, Mapping[str, tuple[str, float]]],
                 factors: Mapping[tuple[str, int], float],
                 ym: str) -> list:
    """ym の月末に見えていた各社の通期行を、目的変数を月末株価 × F へ差し替えて返す。

    `fin_by_co` の値は `period_end` 昇順の `_SectorRec`（namedtuple）。見える行の判定は
    パネルの財務特徴量と同じ `_find_applicable_fin`（期末＋45日）を使う。

    **戻りは edinet_code 順に固定する。** 回帰の結果は行の並びに依存しない（ridge の α は LOO で
    選ぶ・#697・ADR-0058）が、浮動小数の加算順まで揃えるとパネルがビット単位で再現する。
    以前の KFold（シャッフルなし）では、並びが変わるだけで業種ごと gap が動いていた。
    """
    from plugins.macro_snapshots import _find_applicable_fin

    out = []
    for ec in sorted(closes_by_co):
        hit = closes_by_co[ec].get(ym)
        if hit is None:
            continue
        snap_date, close = hit
        rec = _find_applicable_fin(fin_by_co.get(ec, []), snap_date)
        if rec is None:
            continue
        f = factors.get((ec, rec.year), 1.0)
        out.append(rec._replace(stock_price=close * f))
    return out


def asof_gap_ratios(fin_by_co: Mapping[str, list],
                    closes_by_co: Mapping[str, Mapping[str, tuple[str, float]]],
                    factors: Mapping[tuple[str, int], float],
                    yms: Iterable[str],
                    params: dict,
                    predict: Callable[[list, dict], dict]) -> tuple[dict, dict]:
    """月末ごとに業種別回帰をやり直し、`({(edinet_code, ym): gap_ratio}, 月別の件数)` を返す。

    `predict` は `SectorOLSPlugin.predict_gaps`（保存しない経路）。採用列が残らない等で
    回帰が成り立たない月は ValueError を握って件数に「skipped」として残す——その月の gap は
    欠けるだけで、パネル側が欠けた行として扱う（黙って 0 や前月値で埋めない）。
    """
    from plugins import progress

    gaps: dict[tuple[str, str], float] = {}
    stats: dict[str, dict] = {}
    yms = sorted(set(yms))
    for done, ym in enumerate(yms):
        progress.emit("gap_ratio を月末ごとに時点再現", done, len(yms))
        recs = asof_records(fin_by_co, closes_by_co, factors, ym)
        try:
            by_key = predict(recs, params)
        except ValueError as e:
            stats[ym] = {"n_universe": len(recs), "n_gap": 0, "skipped": str(e)}
            continue
        n_gap = 0
        for (ec, _year, _pe), g in by_key.items():
            if g is not None:
                gaps[(ec, ym)] = g
                n_gap += 1
        stats[ym] = {"n_universe": len(recs), "n_gap": n_gap}
    progress.emit("gap_ratio を月末ごとに時点再現", len(yms), len(yms))
    return gaps, stats


def attach_gap_ratio(samples_by_ym: Mapping[str, list],
                     stock_ids_by_ym: Mapping[str, list],
                     gaps: Mapping[tuple[str, str], float],
                     coverage: dict | None = None) -> dict[str, list]:
    """パネルの各行の末尾へ gap_ratio を足す。gap の無い行は落とす（他の財務列と同じ厳格さ）。

    `coverage` を渡すと `{ym: (付ける前の行数, 付けた後の行数)}` を書き込む。載せるかどうかの
    判断（母集団の減少率・ADR-0057）はこの数で行う。
    """
    out: dict[str, list] = {}
    for ym, pairs in samples_by_ym.items():
        ids = stock_ids_by_ym[ym]
        if len(ids) != len(pairs):
            raise ValueError(f"attach_gap_ratio: {ym} の行数と銘柄IDの数が一致しません")
        kept = []
        for (feat_row, y), ec in zip(pairs, ids):
            g = gaps.get((ec, ym))
            if g is None:
                continue
            kept.append((list(feat_row) + [float(g)], y))
        if coverage is not None:
            coverage[ym] = (len(pairs), len(kept))
        if kept:
            out[ym] = kept
    return out


# ── DB 境界 ─────────────────────────────────────────────────────────────────

def nightly_params() -> dict:
    """夜間の本番設定（`NIGHTLY_PARAMS`）を契約どおりに型付けした sector_ols の params。"""
    from nightly_scores import NIGHTLY_PARAMS
    from plugins.sector_ols import plugin
    from plugins.utils import coerce_params

    return coerce_params(plugin.params_schema(), NIGHTLY_PARAMS.get(SECTOR_MODEL, {}))


def load_inputs(db, features: list) -> tuple[dict[str, list], dict[tuple[str, int], float]]:
    """全年度の通期行（社ごとに period_end 昇順）と分割補正係数 F を読む。"""
    from database import SplitAdjustmentFactor
    from plugins.sector_ols import plugin

    fin_by_co: dict[str, list] = defaultdict(list)
    for r in plugin._load_records(db, None, features, all_years=True):
        fin_by_co[r.edinet_code].append(r)
    for recs in fin_by_co.values():
        recs.sort(key=lambda r: str(r.period_end))
    factors = {
        (ec, int(y)): float(f)
        for ec, y, f in db.query(SplitAdjustmentFactor.edinet_code,
                                 SplitAdjustmentFactor.year,
                                 SplitAdjustmentFactor.factor).all()
    }
    return dict(fin_by_co), factors


def build_asof_gaps(db, prices_by_co: Mapping[str, list],
                    yms: Iterable[str]) -> tuple[dict, dict]:
    """学習パネルの月（`yms`）ごとに時点再現の gap_ratio を作る。DB へは書かない。"""
    from plugins.sector_ols import plugin

    params = nightly_params()
    fin_by_co, factors = load_inputs(db, params["features"])
    closes_by_co = {ec: month_end_closes(rows) for ec, rows in prices_by_co.items()}
    gaps, stats = asof_gap_ratios(fin_by_co, closes_by_co, factors, yms, params,
                                  plugin.predict_gaps)
    n_skipped = sum(1 for s in stats.values() if "skipped" in s)
    log.info("時点再現の gap_ratio: %d か月・%d 件（回帰が成り立たなかった月 %d）",
             len(stats), len(gaps), n_skipped)
    return gaps, stats
