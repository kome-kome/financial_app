"""Issue #529: プリセット/重みの昇格ゲート（期別 rank-IC）を測るローカル専用CLI。

ADR-0028 の昇格ゲート（期別 rank-IC 系列 ＋ `paired_ic_significance` の定常ブートストラップ
＋ Bonferroni 補正）は #509（ADR-0008 に記録）と #517（ADR-0039 に記録）で2回測られたが、
**どちらも使い捨てのスクリプトで、リポジトリに実体が残っていなかった**。3回目（#513 の
プリセット重み再設計、#520 の gap_ratio 年度混在）が確実にあるため、実体をここへ置く。

`scripts/candidate_bakeoff.py` は同じ `paired_ic_significance` をモデル候補の比較に使って
おり、**プリセット/重みの比較にだけ実体が無い**という非対称を解消する位置づけ。

設計の要点
----------
1. **標準化は消費側の関数をそのまま呼ぶ**。`plugins.recommend.fit_view_metric_stats` →
   `standardize_metric`（`fit_zscore_stats` で頑健な mean/sd を作り、**生値**を
   `normalize_transform` で変換して ±5 クリップ）であって、学習系の `fit_feature_columns`
   （値を p1-p99 でクリップしてから zscore）ではない。ここを取り違えると測っているものが
   本番と別物になる（#529 の指摘そのもの）。パネルは numpy 配列なので `_panel_rows` で
   属性アクセスできる形へ写す薄いアダプタだけを挟む。
2. **パネルは `recommend_factor_premia.build_period_panel`**。#509/#517 の実測がこの61期
   パネル上で行われたため、再現には同じものが要る。gap_ratio を持たない（ADR-0008
   Decision 1）ので、**測れなかった重みの比率を必ず出力**し、閾値超の行は判定を n/a へ落とす。
   黙って落とすと「割安重視は rank-IC 負」という嘘の結論が独り歩きする。
3. **momentum の単位を本番へ揃える**。パネルの `z_momentum` は生の log return（#519 で
   build_period_panel は標準化しない）。本番は `compute_momentum_z` が期内 winsorize→
   標準化した値を渡すため、ここでも同じ2関数で断面標準化してから合成する（`--raw-momentum`
   で無効化＝アドホック測定との差分を切り分ける用）。
4. **キャッシュは既定オフ**。bakeoff キャッシュは値の是正でも期間拡張でも黙って旧世代を返す
   前例（#454/#456）があるため `--cache-panel` で明示オプトインにする。`--refresh-cache`
   は週次株価40MB級を巻き込むので持たせない。
5. 判定文言は**符号ではなく補正後 α**で書く（ADR-0028 規則2）。パネルの世代（期数・起点 ym・
   社数）を必ず併記する（同規則3）。

DB へは書かない（読み取りのみ）。

実行
----
    python -m scripts.preset_ic_gate --panel-info-only
    python -m scripts.preset_ic_gate --json                            # 静的4プリセット
    python -m scripts.preset_ic_gate --no-cross-section-standardize    # #509 是正前の再現
    python -m scripts.preset_ic_gate --compare-standardization         # #509 型の対比較
    python -m scripts.preset_ic_gate --preset バランス型 --premia-run-id rfp_20260820T183244Z

パネルは株価の蓄積で毎晩伸びる。過去の実測（ADR-0008 は61期・2020-07..2025-07）と突き合わせる
ときは `--until 2025-07` で期を揃えること——揃えないと 0.002 程度の差が出て「手続きが違う」の
ように見える。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, get_latest_factor_premia   # noqa: E402
from model_stats import paired_ic_significance, significance_matrix   # noqa: E402
from plugins.macro_snapshots import _spearman   # noqa: E402
from plugins.recommend import (   # noqa: E402
    METRICS, PRESETS, STATISTICAL_PRESET_NAME,
    fit_view_metric_stats, standardize_metric,
)
from plugins.utils import PREPROCESS_VERSION, fit_zscore_stats   # noqa: E402
from recommend_factor_premia import build_period_panel   # noqa: E402
from scripts._cache import cached   # noqa: E402

_OUT_DIR = Path(__file__).resolve().parent / ".cache"
DEFAULT_MAX_MISSING_WEIGHT = 0.20
DEFAULT_MIN_COMPANIES = 30
DEFAULT_N_BOOT = 2000
DEFAULT_BASELINE = "バランス型"


# ── 表示ヘルパ（日本語ラベルが混ざるので表示幅で揃える）──────────────────────

def _w(s) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, width: int) -> str:
    return str(s) + " " * max(0, width - _w(s))


# ── パネル ────────────────────────────────────────────────────────────────

def load_panel(db, min_companies: int, use_cache: bool) -> tuple:
    """(period_panel, factor_names) を得る。既定はフルビルド（キャッシュは opt-in）。"""
    if use_cache:
        key = f"preset_ic_panel_v1_min{min_companies}"
        return cached(key, lambda: build_period_panel(db, min_companies))
    return build_period_panel(db, min_companies)


def panel_info(panel: dict, factor_names: list) -> dict:
    """パネルの世代を記述する（ADR-0028 規則3: 期数・起点・社数を判定と併記する）。"""
    counts = sorted(len(y) for _X, y in panel.values())
    yms = sorted(panel)
    return {
        "n_periods":        len(panel),
        "first_ym":         yms[0] if yms else None,
        "last_ym":          yms[-1] if yms else None,
        "companies_median": int(statistics.median(counts)) if counts else 0,
        "companies_min":    counts[0] if counts else 0,
        "companies_max":    counts[-1] if counts else 0,
        "factor_names":     list(factor_names),
    }


def print_panel_info(info: dict) -> None:
    print(f"panel: periods={info['n_periods']} span={info['first_ym']}..{info['last_ym']} "
          f"companies(median={info['companies_median']} "
          f"min={info['companies_min']} max={info['companies_max']})", flush=True)
    print(f"panel factors ({len(info['factor_names'])}): "
          f"{', '.join(info['factor_names'])}", flush=True)


# ── スコア合成（消費側と同じ関数・同じ分母）────────────────────────────────

def _panel_rows(X, factor_names: list) -> list:
    """パネル1期分を `fit_view_metric_stats` が読める形へ写す（属性アクセスのみ）。"""
    return [SimpleNamespace(**{name: float(v) for name, v in zip(factor_names, row)})
            for row in X]


def build_view_stats(records: list, weights: dict, factor_names: list, *,
                     standardize: bool = True, raw_momentum: bool = False) -> dict:
    """断面の標準化パラメータを作る。

    VIEW 由来指標は `fit_view_metric_stats`（消費側と同一）。`standardize=False` は
    #509 是正前＝VIEW の年度窓 Z をそのまま線形結合していた挙動の再現。

    `z_momentum` は `fit_view_metric_stats` が RUNTIME_METRICS として除外する（本番では
    `compute_momentum_z` が既に期内標準化して渡すため）。ところが**パネル側の momentum は
    生の log return**（#519 で build_period_panel が意図的に標準化しない）。そのままだと
    この1列だけ本番と単位が違うので、`compute_momentum_z` と同じ2関数でここで揃える。
    是正前の再現でも momentum は Z だったので、`standardize=False` でも揃える。
    """
    stats = fit_view_metric_stats(records, weights) if standardize else {}
    if not raw_momentum and "z_momentum" in weights and "z_momentum" in factor_names:
        vals = [r.z_momentum for r in records if getattr(r, "z_momentum", None) is not None]
        s = fit_zscore_stats(vals)
        if s is not None:
            stats["z_momentum"] = s
    return stats


def score_period(records: list, weights: dict, view_stats: dict) -> list:
    """1期分のスコア列を返す（重みの乗る値が1つも無い行は None）。

    分母は `weight_present`（存在指標の |w| 和）＝ `RecommendPlugin.execute` と同一。
    `total_weight` で割ると欠損の多い銘柄が不当に 0 へ寄る。
    """
    scores = []
    for r in records:
        weighted_sum = 0.0
        weight_present = 0.0
        for metric, weight in weights.items():
            val = getattr(r, metric, None)
            if val is None:
                continue
            weighted_sum += weight * standardize_metric(val, metric, view_stats)
            weight_present += abs(weight)
        scores.append(weighted_sum / weight_present if weight_present > 0 else None)
    return scores


def ic_series(panel: dict, factor_names: list, weights: dict, *,
              standardize: bool = True, raw_momentum: bool = False) -> dict:
    """{ym: rank_IC} を返す。IC は spearman(score, 52週先 log return)。"""
    out: dict = {}
    for ym in sorted(panel):
        X, y = panel[ym]
        records = _panel_rows(X, factor_names)
        view_stats = build_view_stats(records, weights, factor_names,
                                      standardize=standardize, raw_momentum=raw_momentum)
        scores = score_period(records, weights, view_stats)
        xs, ys = [], []
        for s, target in zip(scores, y):
            if s is not None:
                xs.append(s)
                ys.append(float(target))
        ic = _spearman(xs, ys)
        if ic is not None:
            out[ym] = ic
    return out


def missing_weight_ratio(weights: dict, factor_names: list) -> float:
    """パネルに無い列へ置かれた重みの比率（割安重視の gap_ratio 2.0/4.5 = 0.444 等）。"""
    total = sum(abs(w) for w in weights.values())
    if total <= 0:
        return 0.0
    missing = sum(abs(w) for m, w in weights.items() if m not in factor_names)
    return missing / total


# ── 重みの収集（出所は問わない）──────────────────────────────────────────

def premia_weights(db, run_id) -> tuple:
    """recommend_factor_premia の1ランを重み dict へ。戻りは (weights, preprocess_version)。

    `get_dynamic_preset` と違い**世代が古くても採る**（診断のために旧単位の重みを測るのが
    本スクリプトの用途そのもの・ADR-0039 の A 状態）。世代はラベルへ出して区別する。
    """
    premia = get_latest_factor_premia(db, run_id)
    if not premia:
        return {}, None
    versions = sorted({str(v.get("preprocess_version")) for v in premia.values()})
    weights = {f: vals["mean_b"] for f, vals in premia.items()
               if f in METRICS and f != "mu"}
    return weights, ",".join(versions)


def collect_weights(db, args) -> dict:
    """{label: weights} を組み立てる。ラベルは表・JSON・検定名にそのまま使う。"""
    out: dict = {}

    for name in (args.preset or []):
        if name in PRESETS:
            out[name] = dict(PRESETS[name])
        elif name == STATISTICAL_PRESET_NAME:
            w, ver = premia_weights(db, None)
            if not w:
                raise SystemExit(f"{name}: recommend_factor_premia が未蓄積です")
            out[f"{name}(pv={ver})"] = w
        else:
            raise SystemExit(f"未知のプリセット: {name!r}（既知: {', '.join(PRESETS)}）")

    for run_id in (args.premia_run_id or []):
        w, ver = premia_weights(db, run_id)
        if not w:
            raise SystemExit(f"run_id={run_id!r} の行が recommend_factor_premia にありません")
        out[f"premia:{run_id}(pv={ver})"] = w

    if args.weights_json:
        raw = json.loads(Path(args.weights_json).read_text(encoding="utf-8"))
        for label, w in raw.items():
            out[str(label)] = {str(k): float(v) for k, v in w.items()}

    if not out:
        out = {name: dict(w) for name, w in PRESETS.items()}

    for label, w in out.items():
        unknown = [k for k in w if k not in METRICS]
        if unknown:
            raise SystemExit(f"{label}: METRICS 外のキー {unknown}（許可: {', '.join(METRICS)}）")
        if w.get("mu"):
            raise SystemExit(
                f"{label}: mu を含む重みは測れません。パネルは producer 由来の μ̂ を持たず、"
                "as-of 再現もできない（ADR-0030・backtest.run と同じ理由）")
        if sum(abs(v) for v in w.values()) <= 0:
            raise SystemExit(f"{label}: 重みの絶対値和が 0 です")
    return out


# ── 出力 ──────────────────────────────────────────────────────────────────

def fmt_sig(sig, alpha: float, p_floor: float) -> str:
    if not sig:
        return "n/a (common periods < 2)"
    p = sig.get("p_value")
    star = "SIG" if (p is not None and p < alpha) else "ns"
    # p 値は bootstrap_mean_ci が 4 桁へ丸めて返すので、下限も同じ桁で比べる。
    floor_mark = " [p at floor]" if (p is not None and p <= round(p_floor, 4)) else ""
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={p:.4f} n={sig['n_common']} {star}(alpha={alpha:.4f}){floor_mark}")


def report_rows(rows: list, max_missing: float) -> None:
    label_w = max([_w(r["label"]) for r in rows] + [_w("label")])
    print("")
    header = (f"{_pad('label', label_w)}  {'periods':>7}  {'rank_IC':>8}  "
              f"{'IC_std':>7}  {'unmeasured_w':>12}  status")
    print(header, flush=True)
    print("-" * (_w(header) + 2), flush=True)
    for r in rows:
        status = "ok"
        if r["missing_weight"] > max_missing:
            status = f"n/a (measured on {100 * (1 - r['missing_weight']):.0f}% of weight)"
        elif r["missing_weight"] > 0:
            status = "partial"
        print(f"{_pad(r['label'], label_w)}  {r['n_periods']:>7}  {r['rank_ic']:>+8.4f}  "
              f"{r['ic_std']:>7.4f}  {100 * r['missing_weight']:>11.1f}%  {status}", flush=True)


def main() -> None:
    # Windows cp932 では非ASCII記号でクラッシュするため UTF-8 に固定
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Issue #529: プリセット/重みの昇格ゲート（期別 rank-IC）を測る")
    ap.add_argument("--preset", action="append",
                    help="測るプリセット名（複数可）。既定は静的4プリセット全部")
    ap.add_argument("--premia-run-id", action="append",
                    help="recommend_factor_premia の run_id（複数可・世代が古くても採る）")
    ap.add_argument("--weights-json",
                    help="ラベルをキーに重み dict を並べた JSON ファイル")
    ap.add_argument("--baseline", default=None,
                    help=f"差の基準ラベル（既定: {DEFAULT_BASELINE}、無ければ先頭）")
    ap.add_argument("--all-pairs", action="store_true",
                    help="baseline 比較でなく全ペアの有意差マトリクスを出す")
    ap.add_argument("--no-cross-section-standardize", action="store_true",
                    help="断面標準化を通さない＝#509 是正前の挙動を再現する")
    ap.add_argument("--compare-standardization", action="store_true",
                    help="同じ重みを 是正後(post)/是正前(pre)の両方で測り、共通期でペア検定する"
                         "（#509 型の測定）")
    ap.add_argument("--raw-momentum", action="store_true",
                    help="z_momentum を生 log return のまま合成する（本番と単位が違う・診断用）")
    ap.add_argument("--max-missing-weight", type=float, default=DEFAULT_MAX_MISSING_WEIGHT,
                    help="測れない重み比率がこれを超える行は判定を n/a にする")
    ap.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT,
                    help="定常ブートストラップの反復数（p 値の下限は 2/(n_boot+1)）")
    ap.add_argument("--min-companies", type=int, default=DEFAULT_MIN_COMPANIES,
                    help="1期あたりの最小社数（これ未満の期は破棄）")
    ap.add_argument("--until", default=None,
                    help="この ym までの期だけ使う（例 2025-07）。パネルは株価の蓄積で伸びる"
                         "ので、過去の実測と期数を揃えて突き合わせるときに使う")
    ap.add_argument("--cache-panel", action="store_true",
                    help="パネルを scripts/.cache へ保存/再利用する（既定はフルビルド）")
    ap.add_argument("--panel-info-only", action="store_true",
                    help="パネルの世代だけ出して終わる")
    ap.add_argument("--json", nargs="?", const=str(_OUT_DIR / "preset_ic_gate.json"),
                    default=None, help="判定を JSON で書き出す")
    args = ap.parse_args()
    if args.compare_standardization and args.no_cross_section_standardize:
        raise SystemExit("--compare-standardization は両方の経路を測るので "
                         "--no-cross-section-standardize と同時には使えません")

    db = SessionLocal()
    try:
        panel, factor_names = load_panel(db, args.min_companies, args.cache_panel)
        if args.until:
            panel = {ym: v for ym, v in panel.items() if ym <= args.until}
            if not panel:
                raise SystemExit(f"--until {args.until} で残る期がありません")
        info = panel_info(panel, factor_names)
        print_panel_info(info)
        if args.panel_info_only:
            db.commit()
            return
        weights_by_label = collect_weights(db, args)
        db.commit()
    finally:
        db.close()

    standardize = not args.no_cross_section_standardize
    print(f"\nstandardize=cross-section:{standardize} raw_momentum={args.raw_momentum} "
          f"preprocess_version={PREPROCESS_VERSION}", flush=True)

    # #509 型の測定は「同じ重みを、断面標準化の有無だけ変えて」ペアで比べる。1回の実行で
    # 両系列を作らないと共通期のペアリングができず、別々に走らせた表を人手で突き合わせる
    # ことになる（それが ADR-0008 の表が差の CI を1行しか持たない理由）。
    variants = ([(" (post)", True), (" (pre)", False)] if args.compare_standardization
                else [("", standardize)])

    ic_by_label: dict = {}
    rows = []
    for label, weights in weights_by_label.items():
        for suffix, std in variants:
            ics = ic_series(panel, factor_names, weights,
                            standardize=std, raw_momentum=args.raw_momentum)
            ic_by_label[label + suffix] = ics
            vals = list(ics.values())
            rows.append({
                "label":          label + suffix,
                "weights":        weights,
                "n_periods":      len(vals),
                "rank_ic":        statistics.fmean(vals) if vals else float("nan"),
                "ic_std":         statistics.stdev(vals) if len(vals) > 1 else 0.0,
                "missing_weight": missing_weight_ratio(weights, factor_names),
            })
    report_rows(rows, args.max_missing_weight)

    # ── 有意差判定（Bonferroni は検定数から導く）──────────────────────────
    p_floor = 2.0 / (args.n_boot + 1)
    sigs: dict = {}
    passed: list = []
    labels = list(ic_by_label)
    baseline = None
    if args.compare_standardization:
        n_tests = max(1, len(weights_by_label))
    elif args.all_pairs:
        n_tests = max(1, len(labels) * (len(labels) - 1) // 2)
    else:
        baseline = args.baseline or (DEFAULT_BASELINE if DEFAULT_BASELINE in labels
                                     else labels[0])
        if baseline not in labels:
            raise SystemExit(f"baseline={baseline!r} が対象に含まれていません")
        n_tests = max(1, len(labels) - 1)
    alpha = 0.05 / n_tests

    if args.compare_standardization:
        print(f"\n=== post - pre cross-section standardization "
              f"(Bonferroni alpha={alpha:.4f}, {n_tests} tests) ===", flush=True)
        for label in weights_by_label:
            sig = paired_ic_significance(ic_by_label[f"{label} (post)"],
                                         ic_by_label[f"{label} (pre)"],
                                         n_boot=args.n_boot)
            key = f"{label} (post)|{label} (pre)"
            sigs[key] = sig
            if sig and sig.get("p_value") is not None and sig["p_value"] < alpha:
                passed.append(key)
            print(f"  {_pad(label, 46)} {fmt_sig(sig, alpha, p_floor)}", flush=True)
    elif args.all_pairs:
        print(f"\n=== all pairs (Bonferroni alpha={alpha:.4f}, {n_tests} tests) ===",
              flush=True)
        matrix = significance_matrix(ic_by_label, alpha=alpha, n_boot=args.n_boot)
        for key, res in matrix["pairs"].items():
            sig = None if res.get("mean_diff") is None else {
                "mean": res["mean_diff"], "ci_lo": res["ci_lo"], "ci_hi": res["ci_hi"],
                "p_value": res["p_value"], "n_common": res["n_common"],
            }
            sigs[key] = sig
            if sig and sig["p_value"] is not None and sig["p_value"] < alpha:
                passed.append(key)
            print(f"  {_pad(key, 46)} {fmt_sig(sig, alpha, p_floor)}", flush=True)
    else:
        print(f"\n=== diff vs baseline {baseline!r} "
              f"(Bonferroni alpha={alpha:.4f}, {n_tests} tests) ===", flush=True)
        for label in labels:
            if label == baseline:
                continue
            sig = paired_ic_significance(ic_by_label[label], ic_by_label[baseline],
                                         n_boot=args.n_boot)
            key = f"{label}|{baseline}"
            sigs[key] = sig
            if sig and sig.get("p_value") is not None and sig["p_value"] < alpha:
                passed.append(key)
            print(f"  {_pad(label, 46)} {fmt_sig(sig, alpha, p_floor)}", flush=True)

    # 判定は符号ではなく補正後 α で書く（ADR-0028 規則2）。測れない重みが閾値を超えた行は
    # そもそも結論に載せない（rank-IC は出すが判定には使わない）。
    measurable = {r["label"] for r in rows if r["missing_weight"] <= args.max_missing_weight}
    conclusive = [k for k in passed if all(part in measurable for part in k.split("|"))]
    if conclusive:
        verdict = f"significant after correction: {', '.join(conclusive)}"
    else:
        verdict = "no pair significant after correction"
    excluded = [r["label"] for r in rows if r["label"] not in measurable]
    if excluded:
        verdict += f" | excluded (unmeasured weight): {', '.join(excluded)}"
    print(f"\n=== VERDICT: {verdict} ===", flush=True)

    if args.json:
        payload = {
            "panel": info,
            "settings": {
                "cross_section_standardize": standardize,
                "raw_momentum":              args.raw_momentum,
                "preprocess_version":        PREPROCESS_VERSION,
                "n_boot":                    args.n_boot,
                "p_value_floor":             p_floor,
                "alpha":                     alpha,
                "n_tests":                   n_tests,
                "baseline":                  baseline,
                "until":                     args.until,
                "max_missing_weight":        args.max_missing_weight,
            },
            "rows":         rows,
            "ic_by_period": ic_by_label,
            "significance": sigs,
            "verdict":      verdict,
        }
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
