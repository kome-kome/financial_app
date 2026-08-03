"""sector_ols のカバレッジと説明力を設定別に実測する（Issue #434 の昇格ゲート）。

背景:
  `_classify_by_sector` は AND フィルタ（選択列が1つでも NULL の企業を丸ごと除外）のため、
  「無配で dps が NULL」「銀行業に売上総利益が存在しない」といった *構造的な欠損* が
  企業や業種を丸ごと分析対象外にしていた（実測 76.0%）。対策は2つ:
    案1 zero_fill_no_dividend : dps の NULL を無配（0円/株）とみなす
    案2 sector_missing_rate   : 業種内欠損率が閾値超の列を、その業種の回帰からのみ外す

本スクリプトは両案の ON/OFF を総当たりし、カバレッジ（対象社数・業種数）と説明力
（業種別 R² の中央値・社数加重平均）を並べて出す。カバレッジが上がっても説明力が落ちる
なら採らない、という判断を実測でやるための道具（無検証で既定を変えない・CLAUDE.md）。

実行:
  python -m scripts.measure_sector_coverage            # 既定の総当たり
  python -m scripts.measure_sector_coverage --ridge    # Ridge で比較

注意:
  DATABASE_URL は本番 Supabase を指す。**書き込みは行わない**（`_persist_and_rank` を
  no-op へ差し替えて regression_results への upsert を止める）。財務レコードの読み込みは
  1回だけ行い、全設定で使い回す（Egress 節約・[[feedback_verification_fullloads_exhaust_egress]]）。
"""
import argparse
import io
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from database import SessionLocal                       # noqa: E402
from plugins import execute_plugin                      # noqa: E402
from plugins.sector_ols import DEFAULT_FEATURES_PRICE, plugin  # noqa: E402

# (ラベル, sector_missing_rate, zero_fill_no_dividend)
CONFIGS = [
    ("現行相当（率ドロップ無効・0埋めなし）", 1.0, False),
    ("案1のみ（dps 0埋め）",                 1.0, True),
    ("案2のみ（業種内 30%）",                0.3, False),
    ("案1+案2（業種内 30%）★新既定",         0.3, True),
    ("案1+案2（業種内 20%）",                0.2, True),
    ("案1+案2（業種内 50%）",                0.5, True),
]


def _run(db, rate: float, zero_fill: bool, regularization: str) -> tuple[dict, dict]:
    """execute を1回走らせ、(結果, {edinet_code: (sector, 実績株価, 予測株価)}) を返す。"""
    import asyncio
    preds: dict = {}

    def _capture(_db, sector, samples, all_yhat, _reg):
        for (_row, actual, r), yhat in zip(samples, all_yhat):
            preds[r.edinet_code] = (sector, actual, yhat)
        return []

    plugin._persist_and_rank = _capture      # 本番 regression_results への書き込みを止める
    res = asyncio.run(execute_plugin(plugin, {
        "features": DEFAULT_FEATURES_PRICE,
        "min_samples": 5,
        "regularization": regularization,
        "sector_missing_rate": rate,
        "zero_fill_no_dividend": zero_fill,
    }, db))
    return res, preds


def _r2_on(preds: dict, codes: set) -> float:
    """指定企業集合に限定した業種内 R²（社数加重平均）。母集団が違う設定同士を
    同じ分母で比べるための指標。全体 R² は母集団が変わると比較不能になる。"""
    by_sector: dict = {}
    for ec in codes:
        sector, actual, yhat = preds[ec]
        by_sector.setdefault(sector, []).append((actual, yhat))
    num = den = 0.0
    for sector, pairs in by_sector.items():
        if len(pairs) < 3:
            continue
        mean = statistics.fmean(a for a, _ in pairs)
        sst = sum((a - mean) ** 2 for a, _ in pairs)
        sse = sum((a - p) ** 2 for a, p in pairs)
        if sst <= 0:
            continue
        num += (1 - sse / sst) * len(pairs)
        den += len(pairs)
    return num / den if den else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ridge", action="store_true", help="Ridge で比較（既定は OLS）")
    ap.add_argument("--year", type=int, default=None, help="対象年度（既定=最新年度）")
    args = ap.parse_args()
    regularization = "ridge" if args.ridge else "none"

    db = SessionLocal()
    try:
        # 財務レコードは1回だけ読む（全設定で使い回す）
        records = plugin._load_records(db, args.year)
        print(f"読み込み: {len(records)} レコード / regularization={regularization}")
        plugin._load_records = lambda _db, _year: records          # 以降の再読込を抑止

        rows = []
        for label, rate, zero_fill in CONFIGS:
            res, preds = _run(db, rate, zero_fill, regularization)
            stats = res["sector_stats"]
            rows.append({
                "label":     label,
                "n_total":   res["n_total"],
                "n_sectors": res["n_sectors"],
                "r2_median": statistics.median(s["r2"] for s in stats),
                "adj_median": statistics.median(s["adj_r2"] for s in stats),
                # n < 2p は過学習ゾーン（説明変数+切片の2倍のサンプルも無い業種）
                "n_overfit": sum(1 for s in stats if s["n"] < 2 * (len(s["features"]) + 1)),
                "sectors":   {s["industry"]: (s["n"], s["r2"], s["adj_r2"]) for s in stats},
                "preds":     preds,
                "dropped":   res["sector_dropped_features"],
            })

        base = rows[0]
        new  = next(r for r in rows if "★新既定" in r["label"])
        common_codes = set(base["preds"]) & set(new["preds"])

        print()
        print(f"{'設定':<34} {'社数':>7} {'業種':>5} {'R²中央':>8} {'調整R²中央':>10} "
              f"{'n<2p業種':>8} {'共通社R²':>9}")
        print("-" * 88)
        for r in rows:
            d = r["n_total"] - base["n_total"]
            print(f"{r['label']:<34} {r['n_total']:>7} {r['n_sectors']:>5} "
                  f"{r['r2_median']:>8.4f} {r['adj_median']:>10.4f} {r['n_overfit']:>8} "
                  f"{_r2_on(r['preds'], common_codes & set(r['preds'])):>9.4f}"
                  f"{'' if d == 0 else f'   ({d:+d}社)'}")
        print(f"（共通社R² = 現行相当と新既定の両方でスコアが付く {len(common_codes)} 社に"
              "限定した業種内 R² の社数加重平均。母集団が違う設定同士を同じ分母で比べる指標）")

        # 共通業種での R² 変化（母集団が増えた業種ほど下がる＝選択バイアスの解消かを見る）
        print()
        print("共通業種（現行相当と新既定の両方に出る業種）での R² 差:")
        common = sorted(set(base["sectors"]) & set(new["sectors"]))
        diffs = [new["sectors"][s][1] - base["sectors"][s][1] for s in common]
        worse = [(s, base["sectors"][s], new["sectors"][s]) for s in common
                 if new["sectors"][s][1] < base["sectors"][s][1] - 0.05]
        print(f"  共通 {len(common)} 業種 / ΔR² 中央値 {statistics.median(diffs):+.4f} / "
              f"平均 {statistics.fmean(diffs):+.4f} / 悪化 {sum(1 for d in diffs if d < 0)}業種")
        for s, b, n in sorted(worse, key=lambda x: x[2][1] - x[1][1])[:10]:
            print(f"    悪化 {s:<16} n {b[0]:>4}→{n[0]:<4} R² {b[1]:.4f}→{n[1]:.4f} "
                  f"/ 調整R² {b[2]:.4f}→{n[2]:.4f}")
        added = sorted(set(new["sectors"]) - set(base["sectors"]))
        if added:
            print(f"  新たに分析可能になった業種 ({len(added)}): "
                  + "、".join(f"{s}(n={new['sectors'][s][0]}, R²={new['sectors'][s][1]:.3f})"
                              for s in added))
        print()
        print("新既定で業種内ドロップが起きた業種:")
        for d in new["dropped"]:
            print(f"  {d}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
