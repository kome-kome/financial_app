"""Issue #402: sell_ranking の既定 μ 出所（mu_source）を M-2 / M-6 / M-4 のどれにするかの実測。

**Issue #402 の前提の訂正**:
  Issue の改善案は「`/api/backtest`（`source=sell`）で `mu_source` を振る」だったが、
  `backtest.py` の `source="sell"` は **recommend プリセット加重和の符号反転**
  （`score_record`）であり、sell_ranking の μ / −R_macro 観点を一切使わない
  （`mu_source` パラメータも持たない・`/api/backtest` のクエリにも無い）。
  加えて producer スコアテーブル（`macro_gbdt_scores` 等）は「現在時点」のスナップショット
  しか持たないため、過去日付の as-of バックテストへ持ち込むと look-ahead になる。
  よって μ 出所の売り性能を honest に測れるのは OOF 側（purge/embargo 付き walk-forward・
  ADR-0014）だけであり、本スクリプトは `oof_backtest` の **売り側指標**
  （`short_side_spread`・Issue #402 で追加）で比較する。

手法:
  M-4（3基底＝M-1+M-2+M-6）を1回実行すると `base_oof_backtest` に**同一共通 (ym,ec) 域**へ
  制限した各基底の OOF が揃う（ADR-0015 の base-on-common）。母集団差の交絡なしに
  M-1 / M-2 / M-6 / M-4 の4通りを横並び比較できるため、実行は1回で足りる。

  読み方（売り判定として）:
    - short_side_spread = 期内全体平均 − bottom 分位平均。**大きいほど**「売り候補（μ̂ 下位）が
      市場平均を下回った」＝売りシグナルとして有効。
    - bottom 分位リターン（quantile_returns[0]）は**小さいほど**良い（売って正解だった）。
    - long_short_spread / rank-IC は買い側（top 分位）の強さに引っ張られるため、売り判定の
      優劣判定には使わない（参考値として併記する）。
  有意差は per-fold 系列を共通 test 期でペアリングした定常ブートストラップ
  （`model_stats.paired_ic_significance`・ADR-0018）で見る。

  μ と −R_macro の合成について: sell_ranking の「マクロ予測型」は #1073 まで
  mu:1.0 + neg_r_macro:0.5 の加重和だった（今は μ だけ）。`r_macro` は共有 macro_beta 由来で **mu_source
  非依存**（sell_ranking.execute のコメント参照）。したがって mu_source 間の差分は μ 成分に
  帰着し、μ 単独ランキングでの比較で優劣の向きは保たれる。

  R3 ゲート（Issue の第2項）: OOF の `interval_halfwidth` は honest split-conformal の
  **marginal**（全銘柄一定）半幅なので、M-2 と M-6 の水準比を見れば「同じ `r3_gate` 閾値を
  使ったときにどちらが厳しく足切りされるか」の目安になる。per-stock バケット半幅
  （producer 側 `conformal_bucket_halfwidths`）の分布までは OOF では測れない。

`--in-layer`（#1043・ADR-0069 の決定 4）: 上の売り側spread は**全社の**下位を測るが、実運用の売りは
**保有の中の下位**から選ぶ。保有はもともと買いの並びの上位から買った社なので、全社の最下位にはほとんど
入らない。そこで同じ M-4 の共通域で「上位層の売り側spread」も出す（参考で判定には使わない）:
  - 上位層: 1 つ前の test 期 t−1 に、買いの並びで上位 20% だった社のうち t にも予測がある社。買いの並びは
    各基底の画面の既定（M-1 / M-2 は U = μ̂ − λR・生の R2、λ=0 の M-6 は μ̂）＝画面の並びの採点（#1042）と同じ
  - 値: 層全体の y 平均 − 層の中で売りの並び（μ̂ の昇順）の下位 20% の y 平均（大きいほど売りが当たった）
  - 売りの並び: M-6・M-2・M-1 の μ̂ と、#1073 まで既定だったプリセットの式（μ + 0.5×−R_macro）の上限版。
    R_macro は今の推定値しかなく過去の時点で作り直せないので**未来情報込みの上限**
  - 比べる条件は全て同じ共通域・同じ層で測る（`plugins.macro_snapshots.in_layer_short_side`）
  - M-4 の基底は #1058 で本番の M-2 / M-6 の設定にそろえた（`M4_BASES_CAVEAT`）。上の全社の売り側spread も
    同じ基底を測っている
同じ実行で**全社の売り側spread**（#1073）も同じ売りの条件で出し、`whole_universe` に書く。上の全社の比較と
同じ `oof_backtest` の `short_side_spread` を、M-4 の共通域の行の順のまま売りのキーで測る（定義を書き写さない）。
再計算した M-6 の値が M-4 実行の `base_oof_backtest` と一致しなければ、同じ域を測れていないので止める。
既定プリセットの −R_macro を残すかの判定の規則は #1073 のコメントで計測の前に固定した。
結果は `scripts/.cache/sell_in_layer.json`。日中枠 `measure:in-layer-sell`（並走に敏感）で単独に回す。

`--sell-candidates`（#1152）: 売り専用に学習した候補（S1＝その月の下位 1/5 に入る確率・L1+L2 のロジスティック回帰・
`plugins.model_candidates.make_logistic_bottom_fit_predict`）を、本番の売りの μ̂（M-6）と比べる。事前登録
`docs/prereg/sell-dedicated-model.md` の規則で判定し、`--prereg` と `--prereg-sha` を必須にする（凍結値が合わなければ
重い計算の前に exit 2）。
  - 候補は M-4 の中の M-6 のレグ（`MacroEnsemblePlugin._m6_cv`）が受け取ったパネル・月をそのまま使い、学習関数だけを
    差し替えて回す＝特徴量・fold・embargo・内側 CV の区切り方・規模の中立化は本番の M-6 と同じで、違うのは当てる答えだけ
  - 予測は M-4 の共通域へそろえる。共通域の行が候補に無い・実現リターンが食い違うときは止める（域の取り違え）
  - 主審: (a) M-6 の買いの並びの上位層の売り側spread（ユーザー判断・2026-11-09）、(b) 全社の売り側spread。差は
    候補 − M-6 を共通の期で対にした定常ブートストラップ（両側 p）。α は 0.05 /（候補数 × 2）
  - 判定は `sell_dedicated_decision` が `prereg_decision` に機械的に出す。結果は `scripts/.cache/sell_dedicated.json`。
    日中枠 `measure:sell-dedicated`（並走に敏感）で単独に回す

データは `scripts/_cache.py` 経由でローカル pickle を読む（Issue #355・本番 Egress ゼロ）。
producer 永続化は `tuning_dry_run()` で no-op、現在μ̂スコアリングは `tuning_objective_only()`
で省略する（本番 `macro_ensemble_scores` を実測で汚さない）。

実行:
    python -m scripts.sell_mu_source_bakeoff                 # 実測（キャッシュ利用）
    python -m scripts.sell_mu_source_bakeoff --smoke         # 間引きパネルで動作確認
    python -m scripts.sell_mu_source_bakeoff --refresh-cache # 本番から取り直す
    python -m scripts.sell_mu_source_bakeoff --in-layer      # 上位層の売り側spread も出す（#1043）
    python -m scripts.sell_mu_source_bakeoff --sell-candidates \
        --prereg docs/prereg/sell-dedicated-model.md --prereg-sha <凍結値>   # 売り専用の候補（#1152）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins import macro_ensemble  # noqa: E402
from plugins.model_candidates import make_diag, make_logistic_bottom_fit_predict, summarize_diag  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    display_risk_map, display_scores, get_producer_scores, in_layer_short_side, oof_backtest,
)
from plugins.utils import fit_zscore_stats, normalize_transform  # noqa: E402
from scripts.ensemble_base_bakeoff import _load_panel, _run_config  # noqa: E402
from scripts._cache import set_refresh  # noqa: E402

_OUT = Path(__file__).resolve().parent / ".cache" / "sell_mu_source_bakeoff.json"
_OUT_IN_LAYER = Path(__file__).resolve().parent / ".cache" / "sell_in_layer.json"
_OUT_SELL = Path(__file__).resolve().parent / ".cache" / "sell_dedicated.json"

# M-4 の基底＝比較対象の μ 出所（sell_ranking.params_schema の mu_source 候補のうち
# per-(ym,銘柄) OOF を出せるもの）。M-3 は水準を持つが M-4 の基底ではないため対象外
# （M-5 は順位スコアのみ・ADR-0017）。
BASES = ["macro_risk_return", "macro_gbdt", "macro_enet"]
LABELS = {
    "macro_risk_return": "M-1",
    "macro_gbdt":        "M-2（ADR-0022 まで既定）",
    "macro_enet":        "M-6",
}
# ADR-0022 で比較した「当時の既定」と「切替候補」（結論の自動判定に使う）。**この対は
# 決定そのものの記録なので現在の既定へ追随させない**（両方 macro_enet になれば検定は
# 自明に無意味になる）。M-2 は #572/ADR-0052 で mu_source から外れたが、BASES は
# M-4 の基底＝比較の土俵であって画面の品揃えではないため据え置く。
CURRENT = "macro_gbdt"
CANDIDATE = "macro_enet"

# ── 上位層の売り側spread（--in-layer・#1043）────────────────────────────────────
# 売りの条件のキー。μ̂ の出所は BASES（基底名をそのままキーにする）、上限版はこの名前。
UPPER = "upper_bound_with_r_macro"
# 上限版の重み。#1073 までの既定プリセット「マクロ予測型」の式で、#1073 の規則 B で既定から −R_macro を外した
# （2026-10-31）。既定へ追随させると上限版が μ̂ だけと同じものになり比較が消えるので、ここに残す。
# 採点の式（`sell_score`）は sell_ranking が μ̂ だけの並びになった（ADR-0071）ので、唯一の使い手のここへ移した。
UPPER_WEIGHTS = {"mu": 1.0, "neg_r_macro": 0.5}
# 被覆率の下限。ADR-0071 まで sell_ranking の `min_coverage` の既定だった値（上限版の測り方を変えない）。
UPPER_MIN_COVERAGE = 0.4


def sell_score(raws: dict, weights: dict, stats: dict,
               min_coverage: float) -> tuple[float | None, float]:
    """1社の売りスコアと被覆率 `(score, coverage)`。score が大きいほど売る理由が大きい。

    raws = {指標: 生の値 or None}（「高い＝売る理由が小さい」向き）。stats = {指標: (mean, sd)}
    （`fit_zscore_stats`）。値がそろった指標を z へ変換し、Σw(−z) をそろった重みの和で割る。
    被覆率（そろった重み / 全重み）が min_coverage 未満なら score は None。ADR-0071 まで
    sell_ranking の本番の式だったもの（上限版の比較を同じ式で続けるために残す）。
    """
    total_weight = sum(weights.values())
    weighted_sum = 0.0
    weight_present = 0.0
    for m, w in weights.items():
        raw = raws.get(m)
        if raw is not None and m in stats:
            mean_, sd = stats[m]
            weighted_sum += w * (-normalize_transform(float(raw), mean_, sd, "zscore"))
            weight_present += w
    coverage = weight_present / total_weight if total_weight else 0.0
    score = weighted_sum / weight_present if weight_present > 0 and coverage >= min_coverage else None
    return score, coverage
# ── 売り専用の候補（--sell-candidates・#1152）──────────────────────────────────
# 事前登録（`docs/prereg/sell-dedicated-model.md`）で固定した候補。S2（分位回帰）は日中枠に収まらず外した（#1152）。
SELL_S1 = "s1_logistic_bottom"
SELL_CANDIDATES = (SELL_S1,)
SELL_PREREG = "docs/prereg/sell-dedicated-model.md"
# 分母＝本番の売りの μ̂ の出どころ（`sell_ranking.MU_SOURCE`・ADR-0071）。
SELL_BASE = "macro_enet"
# 主審 (a) の上位層を作る買いの並び（ユーザー判断・2026-11-09）。他の基底の層は参考で出す。
SELL_BUY_LAYER = "macro_enet"

SHORT = {"macro_risk_return": "M-1", "macro_gbdt": "M-2", "macro_enet": "M-6",
         UPPER: "M-6+R_macro(上限)", SELL_S1: "S1(下位確率)"}
# 検定する対（a − b）。μ̂ の出所どうしは主判定と同じ対、上限版は「R_macro を足して層の中の売りが
# 良くなるか」の未来情報込みの上限。
IN_LAYER_PAIRS = [(CANDIDATE, CURRENT), (CANDIDATE, "macro_risk_return"), (UPPER, CANDIDATE)]
# M-4 の基底のレグは #1058 で本番の M-2 / M-6 の設定にそろえた（plugins/macro_ensemble.py）。何をそろえたかを
# 出力と JSON に残す（そろえる前の結果と読み比べるときの目印）。
M4_BASES_CAVEAT = ("M-4 の基底は本番の設定にそろえてある（#1058）: M-2 と M-6 に財務の空欄埋め（fin_fill）、M-6 に"
                   "本番と同じ内側の交差検証（months_purged）と OOF への規模の中立化。M-4 の現断面の μ̂ は対象外")


def _row(o: dict) -> dict:
    """oof_backtest から売り判定の評価に使う指標だけ抜く。"""
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":            o["rank_ic"]["mean"],
        "rank_ic_std":        o["rank_ic"].get("std"),
        "short_side_spread":  o.get("short_side_spread"),
        "short_side_hit":     o.get("short_side_hit_rate"),
        "bottom_q_return":    q[0] if q else None,
        "top_q_return":       q[-1] if q else None,
        "long_short_spread":  o.get("long_short_spread"),
        "interval_halfwidth": o.get("interval_halfwidth"),
        "n_periods":          o.get("n_periods"),
    }


def _fmt_sig(sig: dict | None) -> str:
    if not sig:
        return "n/a（共通 test 期 < 2）"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={sig.get('p_value', float('nan')):.3f} n={sig['n_common']} "
            f"{'有意' if sig['significant'] else '非有意'}")


def _print_table(rows: dict) -> None:
    hdr = (f"{'モデル':<16}{'売り側spread':>13}{'売り勝率':>10}{'bottom分位':>12}"
           f"{'rank-IC':>10}{'LS spread':>11}{'区間半幅':>10}")
    print(hdr, flush=True)
    print("-" * 82, flush=True)
    for label, r in rows.items():
        def _f(v, w, p=4):
            return f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else f"{'n/a':>{w}}"
        print(f"{label:<16}{_f(r['short_side_spread'], 13)}{_f(r['short_side_hit'], 10)}"
              f"{_f(r['bottom_q_return'], 12)}{_f(r['rank_ic'], 10)}"
              f"{_f(r['long_short_spread'], 11)}{_f(r['interval_halfwidth'], 10)}", flush=True)


def common_to_dicts(common: dict, bases: list) -> tuple[dict[str, dict], dict]:
    """M-4 の共通域 `{ym: [(ec, (yhat…), y)]}`（yhat は bases の順）を
    `({基底: {(ym, ec): yhat}}, {(ym, ec): y})` へ変える。"""
    preds: dict[str, dict] = {name: {} for name in bases}
    y: dict[tuple, float] = {}
    for ym, rows in common.items():
        for ec, yhats, yt in rows:
            for name, yh in zip(bases, yhats):
                preds[name][(ym, ec)] = float(yh)
            y[(ym, ec)] = float(yt)
    return preds, y


def buy_scores(name: str, pred: dict, y: dict, prices_by_co: dict, db) -> tuple[dict, dict]:
    """基底 name の買いの並び `{(ym, ec): U}` と並びの情報。

    画面の既定の λ・横軸（`display_risk_map`）で U = μ̂ − λR（生の R・`display_scores`）にする。λ=0 の
    モデル（M-6）は μ̂ のまま。R が取れない社は落とし、その数を n_dropped_rows に返す。
    """
    resid: dict[str, list] = {}
    meta: dict[str, list] = {}
    for (ym, ec), yh in sorted(pred.items()):
        resid.setdefault(ym, []).append((yh, y[(ym, ec)]))
        meta.setdefault(ym, []).append((ec, None))
    lam, axis, rmap = display_risk_map(get_plugin(name).params_schema(), meta, prices_by_co, db)
    if not lam > 0:
        return dict(pred), {"lambda": lam, "risk_axis": None, "time_reproducible": True,
                            "n_dropped_rows": 0}
    u_res, u_meta, dropped = display_scores(resid, meta, rmap, lam)
    buy = {(ym, m[0]): u for ym in u_res for (u, _y), m in zip(u_res[ym], u_meta[ym])}
    return buy, {"lambda": lam, "risk_axis": axis, "time_reproducible": axis == "r2",
                 "n_dropped_rows": dropped}


def upper_bound_keys(mu: dict, producer: dict) -> dict:
    """上限版の売りのキー `{(ym, ec): −売りスコア}`（低いほど先に売る）。

    `UPPER_WEIGHTS`（#1073 まで既定だった μ + 0.5×−R_macro）を `sell_score` で採点する。
    μ に M-6 の OOF μ̂、−R_macro に今の producer の R_macro を入れる。**R_macro は時点再現できない**（単一 run
    のスナップショット・#709）ので未来情報込みの上限。z の平均・標準偏差は期ごとに共通域の全社から取る（本番は
    今の投資可能な全社）。score が None の社（被覆率が `UPPER_MIN_COVERAGE` 未満）は入れない。
    """
    weights = dict(UPPER_WEIGHTS)
    raws_by_ym: dict[str, dict] = defaultdict(dict)
    for (ym, ec), v in mu.items():
        rm = (producer.get(ec) or {}).get("r_macro")
        raws_by_ym[ym][ec] = {"mu": v, "neg_r_macro": -float(rm) if rm is not None else None}
    out: dict[tuple, float] = {}
    for ym, raws in raws_by_ym.items():
        stats = {}
        for m in weights:
            s = fit_zscore_stats([r[m] for r in raws.values() if r[m] is not None])
            if s is not None:
                stats[m] = s
        for ec, r in raws.items():
            score, _coverage = sell_score(r, weights, stats, UPPER_MIN_COVERAGE)
            if score is not None:
                out[(ym, ec)] = -score
    return out


def _print_in_layer(buy_name: str, res: dict) -> None:
    o = res["ordering"]
    sizes = [p["n_layer"] for p in res["periods"].values()]
    axis = f"U = mu - {o['lambda']:g}*{o['risk_axis']}" if o["risk_axis"] else "mu"
    print(f"\n--- 買いの並び {SHORT[buy_name]}（{axis}・R の欠けで落とした行 {o['n_dropped_rows']}"
          f"{'' if o['time_reproducible'] else '・R は未来情報込み'}）---", flush=True)
    print(f"  共通域 {res['n_common_rows']} 行 / 層のある期 {len(sizes)} / 層の社数 "
          f"{min(sizes) if sizes else 0}〜{max(sizes) if sizes else 0}", flush=True)
    print(f"  {'売りの並び':<20}{'上位層spread':>13}{'当たり率':>10}{'期数':>6}", flush=True)
    for name, c in res["by_condition"].items():
        mean = f"{c['mean']:>13.4f}" if c["mean"] is not None else f"{'n/a':>13}"
        hit = f"{c['hit_rate']:>10.4f}" if c["hit_rate"] is not None else f"{'n/a':>10}"
        print(f"  {SHORT[name]:<20}{mean}{hit}{c['n_periods']:>6}", flush=True)
    for key, sig in res["sig"].items():
        a, b = key.split("|")
        print(f"  {SHORT[a]} − {SHORT[b]:<18} {_fmt_sig(sig)}", flush=True)


def whole_universe_short_side(common: dict, sells: dict, y: dict, base_oof: dict) -> dict:
    """全社の売り側spread（#1073）を売りの条件ごとに出す。

    sells = {条件名: {(ym, 社): 売りのキー}}（低いほど先に売る）。各期は M-4 の共通域 `common` の行の順のまま、
    全ての条件と y がそろう社だけで `oof_backtest` に `(キー, y)` を渡す＝ADR-0022 の全社の売り側spread と同じ定義・
    同じ母集団。照合: μ̂ の出所（基底名の条件）の期ごとの値は `base_oof[基底]["short_side_spread_by_period"]`
    （M-4 が同じ域で出した値）と一致するはずで、外れたら RuntimeError で止める（域の取り違えは例外を出さずに
    もっともらしい値を返すため）。
    """
    by_ym: dict[str, list] = {}
    for ym, rows in common.items():
        by_ym[ym] = [ec for ec, _yhats, _y in rows
                     if (ym, ec) in y and all((ym, ec) in s for s in sells.values())]
    by_condition: dict[str, dict] = {}
    for name, sell in sells.items():
        o = oof_backtest({ym: [(sell[(ym, ec)], y[(ym, ec)]) for ec in ecs] for ym, ecs in by_ym.items()})
        by_condition[name] = {
            "mean":      o.get("short_side_spread"),
            "hit_rate":  o.get("short_side_hit_rate"),
            "n_periods": len(o.get("short_side_spread_by_period") or {}),
            "by_period": o.get("short_side_spread_by_period") or {},
        }
    for name in base_oof:
        if name not in by_condition:
            continue
        ref = base_oof[name].get("short_side_spread_by_period") or {}
        got = by_condition[name]["by_period"]
        bad = sorted(ym for ym in set(ref) | set(got)
                     if ym not in ref or ym not in got or abs(ref[ym] - got[ym]) > 1e-9)
        if bad:
            raise RuntimeError(f"全社の売り側spread の再計算が M-4 の base_oof と一致しない（{name}・期 {bad[:5]}）。"
                               "同じ共通域を測れていないので判定に使わない")
    return {"by_condition": by_condition,
            "n_common_rows": sum(len(ecs) for ecs in by_ym.values())}


def _print_whole(res: dict) -> None:
    print(f"\n--- 全社（#1073・M-4 の共通域 {res['n_common_rows']} 行・M-4 の base_oof と照合済み）---", flush=True)
    print(f"  {'売りの並び':<20}{'全社spread':>13}{'当たり率':>10}{'期数':>6}", flush=True)
    for name, c in res["by_condition"].items():
        mean = f"{c['mean']:>13.4f}" if c["mean"] is not None else f"{'n/a':>13}"
        hit = f"{c['hit_rate']:>10.4f}" if c["hit_rate"] is not None else f"{'n/a':>10}"
        print(f"  {SHORT[name]:<20}{mean}{hit}{c['n_periods']:>6}", flush=True)
    for key, sig in res["sig"].items():
        a, b = key.split("|")
        print(f"  {SHORT[a]} − {SHORT[b]:<18} {_fmt_sig(sig)}", flush=True)


def in_layer_report(run: dict, prices_by_co: dict, db) -> dict:
    """上位層の売り側spread（#1043）を基底ごとの買いの並びで出し、JSON に書く中身を返す。"""
    bases = run["bases"]
    preds, y = common_to_dicts(run["common"], bases)
    sells = {name: preds[name] for name in bases}          # μ̂ の昇順に売る
    sells[UPPER] = upper_bound_keys(preds[CANDIDATE], get_producer_scores(db))
    print("\n=== 上位層の売り側spread（#1043・参考で判定には使わない）===", flush=True)
    print("t-1 に買いの並びで上位 20% だった社の中で、売りの並びの下位 20% が層の平均を下回った幅"
          "（大きいほど良い）", flush=True)
    print(f"注意: {M4_BASES_CAVEAT}", flush=True)
    results: dict[str, dict] = {}
    for buy_name in bases:
        buy, ordering = buy_scores(buy_name, preds[buy_name], y, prices_by_co, db)
        res = in_layer_short_side(buy, sells, y)
        res["ordering"] = ordering
        res["sig"] = {
            f"{a}|{b}": paired_ic_significance(res["by_condition"][a]["by_period"],
                                               res["by_condition"][b]["by_period"])
            for a, b in IN_LAYER_PAIRS
        }
        _print_in_layer(buy_name, res)
        results[buy_name] = res
    whole = whole_universe_short_side(run["common"], sells, y, run["base_oof"])
    whole["sig"] = {
        f"{a}|{b}": paired_ic_significance(whole["by_condition"][a]["by_period"],
                                           whole["by_condition"][b]["by_period"])
        for a, b in IN_LAYER_PAIRS
    }
    _print_whole(whole)
    return {"caveat": M4_BASES_CAVEAT, "upper_bound": UPPER, "results": results,
            "whole_universe": whole}


def sell_alpha(n_candidates: int = len(SELL_CANDIDATES)) -> float:
    """Bonferroni の有意水準（候補数 × 主審 2 本）。事前登録 §3。"""
    return 0.05 / (n_candidates * 2)


def sell_candidate_fit_predict(name: str, m6p: dict, diag: dict):
    """候補 name の fit_predict。l1_ratio・内側 CV の区切り方・分割数は本番の M-6 から取る（書き写さない）。"""
    from plugins.macro_enet import _CV_SPLITS, _INNER_CV, MacroEnetPlugin

    if name != SELL_S1:
        raise ValueError(f"未知の売りの候補: {name}（{', '.join(SELL_CANDIDATES)} のみ）")
    return make_logistic_bottom_fit_predict(
        l1_ratios=MacroEnetPlugin._l1_ratios(m6p["l1_ratio"]), cv_splits=_CV_SPLITS,
        inner_cv=_INNER_CV, diag=diag)


def candidate_on_common(aligned: dict, y: dict, name: str) -> dict:
    """候補の OOF `{(ym, ec): (yhat, y)}` を M-4 の共通域 `y` の行へそろえて `{(ym, ec): yhat}` にする。

    共通域の行が候補に無い、または実現リターンが食い違うなら、同じ域・同じラベルを測れていないので止める。
    """
    missing = [k for k in y if k not in aligned]
    if missing:
        raise RuntimeError(f"{name}: M-4 の共通域の {len(missing)} 行に予測が無い（例 {missing[:3]}）。"
                           "M-6 と同じパネル・fold で回せていないので判定に使わない")
    bad = [k for k in y if abs(float(aligned[k][1]) - y[k]) > 1e-12]
    if bad:
        raise RuntimeError(f"{name}: 実現リターンが M-4 の共通域と {len(bad)} 行で食い違う（例 {bad[:3]}）")
    return {k: float(aligned[k][0]) for k in y}


def sell_dedicated_decision(sig_layer: dict | None, sig_whole: dict | None, alpha: float) -> dict:
    """事前登録 §4 の判定（1 候補ぶん）。

    採用は「(a) 上位層の売り側spread の差が正で p < alpha」かつ「(b) 全社の売り側spread の差が負で p < alpha ではない」
    ときだけ。検定が出ない（共通の期が 2 未満）指標は有意でないものとして扱う。**規則を変えるなら文書を凍結し直す**。
    """
    def _hit(s):
        p = s.get("p_value") if s else None
        return bool(s and p is not None and p < alpha)

    layer_better = _hit(sig_layer) and sig_layer["mean"] > 0
    whole_worse = _hit(sig_whole) and sig_whole["mean"] < 0
    return {"decision": "adopt" if (layer_better and not whole_worse) else "keep",
            "in_layer_better": layer_better, "whole_universe_worse": whole_worse, "alpha": alpha}


def overall_sell_decision(decisions: dict, layer_sigs: dict) -> dict:
    """候補ごとの判定から全体の判定を出す（§4）。採用が複数なら (a) の差の平均が大きい方。無ければ据え置き。"""
    adopted = [c for c, d in decisions.items() if d["decision"] == "adopt"]
    if not adopted:
        return {"decision": "keep", "chosen": None, "by_candidate": decisions}
    chosen = max(adopted, key=lambda c: layer_sigs[c]["mean"])
    return {"decision": "adopt", "chosen": chosen, "by_candidate": decisions}


def _buy_rank_ic(sells: dict, y: dict, by_ym: dict) -> dict:
    """参考: 各売りのキーを買いの並びとして読んだ rank-IC（共通域・全社）。判定には使わない。"""
    out = {}
    for name, sell in sells.items():
        o = oof_backtest({ym: [(sell[(ym, ec)], y[(ym, ec)]) for ec in ecs] for ym, ecs in by_ym.items()})
        out[name] = {"mean": o["rank_ic"]["mean"], "n_periods": o.get("n_periods")}
    return out


def sell_dedicated_report(run: dict, m6_args: tuple, prices_by_co: dict, db, stamp: dict) -> dict:
    """売り専用の候補を M-6 のレグと同じ入力で回し、主審 2 本の検定と判定を返す（#1152）。"""
    import time

    b, m6p, yms = m6_args
    bases = run["bases"]
    preds, y = common_to_dicts(run["common"], bases)
    sells = {SELL_BASE: preds[SELL_BASE]}
    diags: dict[str, dict] = {}
    for name in SELL_CANDIDATES:
        diag = make_diag()
        t0 = time.time()
        aligned, _ctx = macro_ensemble.plugin._m6_cv(
            b, m6p, yms, fit_predict=sell_candidate_fit_predict(name, m6p, diag))
        sells[name] = candidate_on_common(aligned, y, name)
        diags[name] = {**summarize_diag(diag), "n_folds": len(diag.get("C") or []),
                       "seconds": round(time.time() - t0, 1)}
        print(f"[{name}] folds={diags[name]['n_folds']} C={diags[name].get('C')} "
              f"l1_ratio={diags[name].get('l1_ratio')} n_nonzero={diags[name].get('n_nonzero')} "
              f"convergence_warnings={diags[name].get('n_convergence_warnings')} ({diags[name]['seconds']}s)",
              flush=True)

    alpha = sell_alpha()
    print(f"\n=== 売り専用の候補（#1152・事前登録 {stamp['prereg_path']}・Bonferroni alpha={alpha:g}）===",
          flush=True)
    print(f"注意: {M4_BASES_CAVEAT}", flush=True)

    def _sigs(by_condition):
        return {c: paired_ic_significance(by_condition[c]["by_period"], by_condition[SELL_BASE]["by_period"],
                                          alpha=alpha)
                for c in SELL_CANDIDATES}

    layers: dict[str, dict] = {}
    for buy_name in bases:
        buy, ordering = buy_scores(buy_name, preds[buy_name], y, prices_by_co, db)
        res = in_layer_short_side(buy, sells, y)
        res["ordering"] = ordering
        sig = _sigs(res["by_condition"])
        res["sig"] = {f"{c}|{SELL_BASE}": v for c, v in sig.items()}
        res["primary"] = buy_name == SELL_BUY_LAYER
        _print_in_layer(buy_name, res)
        layers[buy_name] = res
    whole = whole_universe_short_side(run["common"], sells, y, run["base_oof"])
    whole_sig = _sigs(whole["by_condition"])
    whole["sig"] = {f"{c}|{SELL_BASE}": v for c, v in whole_sig.items()}
    _print_whole(whole)

    layer_sig = {c: layers[SELL_BUY_LAYER]["sig"][f"{c}|{SELL_BASE}"] for c in SELL_CANDIDATES}
    decisions = {c: sell_dedicated_decision(layer_sig[c], whole_sig[c], alpha) for c in SELL_CANDIDATES}
    decision = overall_sell_decision(decisions, layer_sig)
    by_ym: dict[str, list] = {}
    for ym, rows in run["common"].items():
        by_ym[ym] = [ec for ec, _yh, _y in rows if (ym, ec) in y and all((ym, ec) in v for v in sells.values())]
    buy_ic = _buy_rank_ic(sells, y, by_ym)
    chosen = f"（{SHORT[decision['chosen']]}）" if decision["chosen"] else ""
    print(f"\n=== 判定（prereg_decision）: {decision['decision']}{chosen} ===", flush=True)
    for c, d in decisions.items():
        print(f"  {SHORT[c]}: {d['decision']}（(a) 上位層 {SHORT[SELL_BUY_LAYER]} が有意に良い={d['in_layer_better']}・"
              f"(b) 全社が有意に悪い={d['whole_universe_worse']}）", flush=True)
    print("  参考（判定に使わない）: 買いの rank-IC "
          + " / ".join(f"{SHORT[n]} {v['mean']:+.4f}" for n, v in buy_ic.items()), flush=True)
    return {**stamp, "caveat": M4_BASES_CAVEAT, "alpha": alpha, "base": SELL_BASE,
            "buy_layer": SELL_BUY_LAYER, "candidates": list(SELL_CANDIDATES), "diagnostics": diags,
            "in_layer": layers, "whole_universe": whole, "buy_rank_ic_reference": buy_ic,
            "n_common": run["n_common"], "prereg_decision": decision}


def check_prereg_args(args) -> dict | None:
    """`--sell-candidates` だけ事前登録を必須にして照合する（不一致は重い計算の前に exit 2）。"""
    from scripts import prereg

    if not args.sell_candidates:
        if args.prereg or args.prereg_sha:
            raise SystemExit("--prereg / --prereg-sha は --sell-candidates と一緒にしか使えません")
        return None
    if not (args.prereg and args.prereg_sha):
        raise SystemExit("--sell-candidates には --prereg と --prereg-sha が要る（事前登録 #1152）")
    if Path(args.prereg).as_posix() != SELL_PREREG:
        raise SystemExit(f"--sell-candidates の事前登録は {SELL_PREREG}（受け取った値: {args.prereg}）")
    return prereg.stamp_from_args(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.sell_mu_source_bakeoff",
                                 description="Issue #402 売り側 μ 出所の OOF 実測")
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--in-layer", action="store_true",
                    help="上位層の売り側spread（#1043）も出す。結果は scripts/.cache/sell_in_layer.json")
    ap.add_argument("--sell-candidates", action="store_true",
                    help="売り専用の候補を M-6 の μ̂ と比べる（#1152）。結果は scripts/.cache/sell_dedicated.json")
    ap.add_argument("--prereg", help=f"事前登録文書（--sell-candidates で必須・{SELL_PREREG}）")
    ap.add_argument("--prereg-sha", help="凍結値（本文の sha256・追記節に記録したもの）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> None:
    # Windows の既定 cp932 では μ̂（結合文字 U+0302）等が出力できずクラッシュするため、
    # リダイレクト時も含めて UTF-8 に固定する（ensemble_base_bakeoff.py と同流儀）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    stamp = check_prereg_args(args)
    set_refresh(args.refresh_cache)

    panel = _load_panel(args)
    db = SessionLocal()
    try:
        m6_args: dict = {}
        orig_m6_cv = macro_ensemble.plugin._m6_cv

        def _spy_m6_cv(b, m6p, yms, fit_predict=None):
            # M-6 のレグが受け取った入力を捕まえる（候補を同じパネル・月で回すため・#1152）
            m6_args["args"] = (b, m6p, yms)
            return orig_m6_cv(b, m6p, yms, fit_predict=fit_predict)

        with patch.object(macro_ensemble.plugin, "_m6_cv", _spy_m6_cv):
            run = _run_config("M-4(3基底・rank_ic_grid)", BASES, panel, db, weight_method="rank_ic_grid",
                              keep_common=args.in_layer or args.sell_candidates)
        _report(run)
        if args.sell_candidates:
            if "args" not in m6_args:
                raise RuntimeError("M-6 のレグの入力を捕まえられなかった（_m6_cv が呼ばれていない）")
            payload = sell_dedicated_report(run, m6_args["args"], panel[0], db, stamp)
            _OUT_SELL.parent.mkdir(parents=True, exist_ok=True)
            _OUT_SELL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nwrote {_OUT_SELL}", flush=True)
        if args.in_layer:
            payload = in_layer_report(run, panel[0], db)
            _OUT_IN_LAYER.parent.mkdir(parents=True, exist_ok=True)
            _OUT_IN_LAYER.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nwrote {_OUT_IN_LAYER}", flush=True)
    finally:
        db.close()


def _report(run: dict) -> None:
    """全社の売り側spread の比較（#402・ADR-0022）を表示し、JSON に書く。"""

    rows = {LABELS[name]: _row(bo) for name, bo in run["base_oof"].items()}
    rows["M-4（3基底統合）"] = _row(run["oof"])

    print(f"\n=== 売り側 OOF 指標（共通域 {run['n_common']} ペア・"
          f"weights={run['weights']}）===", flush=True)
    print("売り側spread・売り勝率は大きいほど良い / bottom分位は小さいほど良い", flush=True)
    _print_table(rows)

    # per-fold 系列（売り側 spread）でペア検定。M-4 は base_oof と同一共通域・同一 test 期。
    ss_by_period = {name: (bo.get("short_side_spread_by_period") or {})
                    for name, bo in run["base_oof"].items()}
    ss_by_period["macro_ensemble"] = run["oof"].get("short_side_spread_by_period") or {}
    ic_by_period = {name: (bo.get("rank_ic_by_period") or {})
                    for name, bo in run["base_oof"].items()}
    ic_by_period["macro_ensemble"] = run["oof"].get("rank_ic_by_period") or {}

    pairs = [
        (CANDIDATE, CURRENT),            # 主判定: M-6 − M-2（既定切替の根拠）
        ("macro_ensemble", CANDIDATE),   # M-4 − M-6（統合が単体を超えるか・ADR-0015 の作法）
        (CANDIDATE, "macro_risk_return"),
    ]
    disp = {**LABELS, "macro_ensemble": "M-4"}
    print("\n=== 売り側 spread の差の有意性（定常ブートストラップ・共通 test 期ペアリング）===",
          flush=True)
    sigs: dict[str, dict | None] = {}
    for a, b in pairs:
        sig = paired_ic_significance(ss_by_period[a], ss_by_period[b])
        sigs[f"{a}|{b}"] = sig
        print(f"  {disp[a]} − {disp[b]:<12} {_fmt_sig(sig)}", flush=True)

    print("\n=== 参考: rank-IC 差（買い側込みの総合予測力・#397 の再掲）===", flush=True)
    ic_sigs: dict[str, dict | None] = {}
    for a, b in pairs:
        sig = paired_ic_significance(ic_by_period[a], ic_by_period[b])
        ic_sigs[f"{a}|{b}"] = sig
        print(f"  {disp[a]} − {disp[b]:<12} {_fmt_sig(sig)}", flush=True)

    # ── 結論（既定切替の可否）───────────────────────────────────────────────
    main_sig = sigs[f"{CANDIDATE}|{CURRENT}"]
    cand = _row(run["base_oof"][CANDIDATE])
    curr = _row(run["base_oof"][CURRENT])
    better = (cand["short_side_spread"] or 0) > (curr["short_side_spread"] or 0)
    signif = bool(main_sig and main_sig["significant"])
    print("\n=== 結論 ===", flush=True)
    print(f"  M-6 売り側spread={cand['short_side_spread']:+.4f} vs "
          f"M-2={curr['short_side_spread']:+.4f} / bottom分位 "
          f"{cand['bottom_q_return']:+.4f} vs {curr['bottom_q_return']:+.4f}", flush=True)
    if better and signif:
        verdict = "切替推奨（M-6 が売り側でも有意に優位）"
    elif better:
        verdict = "切替は任意（M-6 が優位だが売り側では有意差なし）"
    else:
        verdict = "切替非推奨（売り側では M-6 が優位でない）"
    print(f"  → {verdict}", flush=True)
    hw_ratio = ((cand["interval_halfwidth"] / curr["interval_halfwidth"])
                if (cand["interval_halfwidth"] and curr["interval_halfwidth"]) else None)
    if hw_ratio:
        print(f"  R3 ゲート: marginal 区間半幅は M-6/M-2 = {hw_ratio:.3f} 倍"
              f"（既定 r3_gate=0.0 は無効のため挙動差なし。閾値を設ける場合の目安）", flush=True)

    payload = {
        "n_common":       run["n_common"],
        "weights":        run["weights"],
        "rows":           rows,
        "sig_short_side": sigs,
        "sig_rank_ic":    ic_sigs,
        "verdict":        verdict,
        "halfwidth_ratio_cand_over_current": hw_ratio,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUT.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {_OUT}", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
