"""52 週以内に上場廃止した行を採点に足すと、M-6 と M-2 の比較の判定が変わるかを、事前登録した規則で測る計測 CLI
（#1197・親 #1195・ADR-0074）。

規則は `docs/prereg/delisting-redesign.md`（本文は main へのマージで凍結）。判定は道具が `prereg_decision` に
機械的に出す。読み手は手で判定し直さない。

## 手続き

- **学習は変えない。** `measure_label_audit`（#1154）と同じく M-4 を 1 回回して共通域の社ごとの OOF を取る
  （`_run_config(keep_common=True)`・M-4 の基底は既定の設定）。答えのある行の予想は今の計測と同じもの
- **消えた行**: M-2 / M-6 のレグの構築（`MacroEnsemblePlugin._m2_build`）にだけ
  `build_snapshots(return_no_future=True, return_no_future_rows=True)` を足し、特徴量の条件をすべて通ったのに答えを
  作れなかった行の特徴量を取る（条件を書き写さない）。そのうち `measure_label_audit.classify_no_future` が `ended`
  （系列が途中で終わった）と分けた行だけを使う
- **消えた行の予想**: M-2（`macro_ensemble._make_xgb_fit_predict`）と M-6（`model_candidates.make_elasticnet_fit_predict`）の
  fit_predict を包み（`ExtraPredictor`）、各 fold の test 月の消えた行を test の後ろに連結して同じ学習済みモデルで
  予測させ、戻り値を答えのある行と消えた行に分ける。学習データには入れない。どちらの学習関数も、予想する行を学習側の
  統計だけで変換して 1 行ずつ予測するので、答えのある行の予想は連結しても変わらない（テストで照合する）。
  `walk_forward_cv_monthly` は fit_predict の例外を握って fold を捨てるので、包む側で失敗と件数の食い違いを記録し、
  走り終えてから止める
- **M-6 の規模の中立化**: 包む側で捕まえた中立化前の予想を、M-4 と同じ `neutralize_size` で補正し直して共通域の M-6 の
  予想と一致することを確かめ（再現できなければ止める）、同じ月の傾き b と x̄ を消えた行に掛ける
- **答え**: 消えた行は最後の終値で手仕舞い `log(最後の終値 / スナップ時の終値)`。参考に、倒産の疑いの行を log(0.01)
  （−99%）にした版も出す
- **照合**: 答えのある行の答えはパネルと同じ（`label_prices`）、その採点は M-4 の `base_oof` と同じ
  （`check_against_base_oof`）でなければ止める
- **限界**: M-1 は対象外（fit_predict の差し替え口が無く、strict な構築で消えた行の集合も違う）。消えた行の予想は
  fold の月（3 か月おき）だけ。2023 年以前に廃止した社はパネルに居ない（#1188）ので、その月は足す行が無い

日中キュー `measure:delisting-redesign`（並走に敏感）で単独に回す。結果は `scripts/.cache/delisting_redesign.json`。

終了コード: 0 = 完了 / 2 = 事前登録の不一致 / 3 = 入力がそろわない・照合が合わない。

実行:
    python -m scripts.measure_delisting_redesign --allow-full-pull --refresh-cache \\
        --prereg docs/prereg/delisting-redesign.md --prereg-sha <凍結値>
    python -m scripts.measure_delisting_redesign --smoke --allow-full-pull --prereg ... --prereg-sha ...  # 動作確認
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_stats import paired_ic_significance  # noqa: E402
from plugins.macro_snapshots import log_market_cap_by_ym, size_slope  # noqa: E402
from scripts import prereg  # noqa: E402
from scripts.measure_label_audit import (  # noqa: E402
    ALPHA, BASES, PRIMARY_SERIES, SHORT, TOP_K, InputMismatch, _top_k_diff, _verdict, check_against_base_oof,
    classify_no_future, label_prices, metrics_row, score_all,
)

PREREG = "docs/prereg/delisting-redesign.md"
_OUT = ROOT / "scripts" / ".cache" / "delisting_redesign.json"
EXIT_INPUT = 3

# 事前登録 §2・§3 の対象と量（本文と同じ値。変えるなら文書を凍結し直す）。
PAIR = ("macro_enet", "macro_gbdt")       # 判定の対 M-6 − M-2（既定の設定）
BANKRUPT_LOG = math.log(0.01)             # 参考: 倒産の疑いの行の答え（−99%・log(0) を避ける）
COVERED_FROM = "2024-01"                  # 参考: 廃止社の株価がそろう test 月（ADR-0074）
REASONS = ("tob_suspect", "bankruptcy_suspect", "unknown")
NEUTRALIZE_TOL = 1e-9                     # M-6 の中立化を再現できたとみなす差


# ── 消えた行の選び方 ────────────────────────────────────────────────────────────

def global_last_week(prices_by_co: dict) -> str:
    """全体の最新週（`measure_label_audit.delisting_report` と同じ）。"""
    return max((rows[-1].trade_date for rows in prices_by_co.values() if rows), default="")


def select_ended(rows_by_ym: dict, prices_by_co: dict, global_last: str) -> tuple[dict, dict, dict]:
    """消えた行のうち、途中で終わった（ended）行だけを `({ym: [(社, 特徴量)]}, {(ym, 社): 分類}, 件数)` で返す。

    rows_by_ym は `build_snapshots(return_no_future_rows=True)` の `{ym: [(社, 特徴量, 業種, 規模)]}`。
    最後の終値で手仕舞った答えを出せない行（`r_last` が無い）は足さず、件数（`no_exit`）だけ数える。
    """
    keep: dict[str, list] = {}
    info: dict[tuple, dict] = {}
    counts: Counter = Counter()
    for ym in sorted(rows_by_ym):
        for ec, feat, _industry, _size in rows_by_ym[ym]:
            c = classify_no_future((ym, ec), prices_by_co, global_last)
            counts[c["kind"]] += 1
            if c["kind"] != "ended":
                continue
            if c["r_last"] is None or c["r_last"] <= -1.0:
                counts["no_exit"] += 1
                continue
            keep.setdefault(ym, []).append((ec, feat))
            info[(ym, ec)] = c
    return keep, info, dict(counts)


# ── 消えた行にも予想を出す ──────────────────────────────────────────────────────

class ExtraPredictor:
    """fit_predict を包み、各 fold の test 月の消えた行も同じ学習済みモデルで予測させる（#1197）。

    test 月は `walk_forward_cv_monthly` が渡す test の list の実体（`samples_by_ym[ym]`）で引く——M-4 の
    `_filter_yms` は list を作り直さないので、構築の時点の実体と同じものが届く。答えのある行の戻り値は
    そのまま返す（M-4 の採点は変わらない）。消えた行の予想は `extra[基底][(ym, 社)]`、答えのある行の予想は
    `raw[基底][ym]`（M-6 の中立化の前の値・test の並び）に積む。失敗は `errors` に積み、走り終えてから止める
    （`walk_forward_cv_monthly` は例外を握って fold を捨てるので、ここで止めても黙って消える）。
    """

    def __init__(self) -> None:
        self.ym_of_list: dict[int, str] = {}
        self.rows: dict[str, list] = {}
        self.raw: dict[str, dict[str, list]] = defaultdict(dict)
        self.extra: dict[str, dict[tuple, float]] = defaultdict(dict)
        self.errors: list[str] = []
        self.unmapped: Counter = Counter()

    def set_build(self, samples_by_ym: dict, rows_by_ym: dict) -> None:
        """構築の答えのある行（test の list の実体）と、足す行 `{ym: [(社, 特徴量)]}` を覚える。"""
        self.ym_of_list = {id(lst): ym for ym, lst in samples_by_ym.items()}
        self.rows = {ym: list(rows) for ym, rows in rows_by_ym.items() if rows}

    def wrap(self, name: str, factory: Callable) -> Callable:
        """学習関数のファクトリ `factory` を包んだファクトリを返す（引数はそのまま渡す）。"""
        def wrapped_factory(*a, **kw):
            inner = factory(*a, **kw)

            def fit_predict(train_samples, test_samples, *rest):
                ym = self.ym_of_list.get(id(test_samples))
                if ym is None:
                    self.unmapped[name] += 1
                    return inner(train_samples, test_samples, *rest)
                extra = self.rows.get(ym, [])
                n = len(test_samples)
                aug = list(test_samples) + [(feat, 0.0) for _ec, feat in extra]
                try:
                    res = inner(train_samples, aug, *rest)
                except Exception as e:  # noqa: BLE001 — 握られる前に記録する
                    self.errors.append(f"{SHORT[name]} {ym}: {type(e).__name__}: {e}")
                    raise
                if res is None:
                    return None
                yhat, ytrue = res
                if len(yhat) != len(aug) or len(ytrue) != len(aug):
                    msg = f"{SHORT[name]} {ym}: 予想の件数 {len(yhat)} が渡した行数 {len(aug)} と合わない"
                    self.errors.append(msg)
                    raise InputMismatch(msg)
                if [float(v) for v in ytrue[:n]] != [float(s[1]) for s in test_samples]:
                    msg = f"{SHORT[name]} {ym}: 答えのある行の並びが戻り値と合わない"
                    self.errors.append(msg)
                    raise InputMismatch(msg)
                self.raw[name][ym] = [float(v) for v in yhat[:n]]
                for (ec, _feat), yh in zip(extra, yhat[n:]):
                    self.extra[name][(ym, ec)] = float(yh)
                return list(yhat[:n]), list(ytrue[:n])

            return fit_predict

        return wrapped_factory


def neutralize_extras(raw_by_ym: dict, extra: dict, ids_by_ym: dict, logcap_lab: dict, logcap_ext: dict,
                      want: dict) -> tuple[dict, dict]:
    """M-6 の消えた行の予想へ、同じ月の答えのある行と同じ規模の中立化を掛ける（事前登録 §2）。

    raw_by_ym = `{ym: [答えのある行の中立化前の予想]}`（test の並び＝`ids_by_ym[ym]` の並び）、extra = `{(ym, 社): 予想}`、
    logcap_* = `log_market_cap_by_ym` の形、want = 共通域の M-6 の予想（M-4 が中立化した後の値）。答えのある行を
    `neutralize_size` と同じ式で補正し直して want と一致しなければ InputMismatch（中立化を再現できていない）。
    戻り値は `(補正後の消えた行の予想, {ym: {"slope", "mean_x", "n_checked"}})`。x の無い行は補正しない。
    """
    out: dict[tuple, float] = {}
    meta: dict[str, dict] = {}
    bad: list = []
    for ym, raw in sorted(raw_by_ym.items()):
        ids = ids_by_ym.get(ym) or []
        if len(ids) != len(raw):
            raise InputMismatch(f"M-6 {ym}: 中立化前の予想 {len(raw)} 行と社の並び {len(ids)} 行が合わない")
        xs = [logcap_lab.get((ym, ec)) for ec in ids]
        idx = [j for j, x in enumerate(xs) if x is not None]
        if idx:
            slope, mean_x, _my = size_slope([raw[j] for j in idx], [xs[j] for j in idx])
        else:
            slope, mean_x = 0.0, None
        n_checked = 0
        for ec, yh, x in zip(ids, raw, xs):
            adj = yh - slope * (x - mean_x) if (x is not None and mean_x is not None) else yh
            ref = want.get((ym, ec))
            if ref is not None:
                n_checked += 1
                if abs(ref - adj) > NEUTRALIZE_TOL:
                    bad.append((ym, ec))
        meta[ym] = {"slope": slope, "mean_x": mean_x, "n_checked": n_checked}
    if bad:
        raise InputMismatch(f"M-6 の規模の中立化を再現できない行が {len(bad)} 件（例: {bad[:3]}）")
    for (ym, ec), yh in extra.items():
        m = meta.get(ym)
        x = logcap_ext.get((ym, ec))
        if m is None or x is None or m["mean_x"] is None:
            out[(ym, ec)] = yh
        else:
            out[(ym, ec)] = yh - m["slope"] * (x - m["mean_x"])
    return out, meta


# ── 判定 ────────────────────────────────────────────────────────────────────────

def include_decision(base: dict, with_ended: dict) -> dict:
    """事前登録 §4 の判定。主審 2 本の判定の入れ替わりか、上位10社超過の差の符号の入れ替わりで propose_include。"""
    a, b = PAIR
    tests: dict[str, dict] = {}
    flip_i = False
    for series in PRIMARY_SERIES:
        s0 = paired_ic_significance(base[a].get(series) or {}, base[b].get(series) or {}, alpha=ALPHA)
        s1 = paired_ic_significance(with_ended[a].get(series) or {}, with_ended[b].get(series) or {}, alpha=ALPHA)
        v0, v1 = _verdict(s0), _verdict(s1)
        tests[series] = {"base": s0, "with_ended": s1, "verdict_base": v0, "verdict_with_ended": v1,
                         "flipped": v0 != v1}
        flip_i = flip_i or v0 != v1
    d0, d1 = _top_k_diff(base[a], base[b]), _top_k_diff(with_ended[a], with_ended[b])
    flip_ii = d0 is not None and d1 is not None and (d0 > 0) != (d1 > 0)
    return {
        "pair": f"{SHORT[a]} - {SHORT[b]}", "alpha": ALPHA, "tests": tests,
        "top_k_excess_diff": {"base": d0, "with_ended": d1, "flipped": flip_ii},
        "flip_primary": flip_i, "flip_top_k": flip_ii,
        "decision": "propose_include" if (flip_i or flip_ii) else "keep",
    }


# ── 実行 ────────────────────────────────────────────────────────────────────────

def run_m4_with_extras(panel: tuple, db) -> tuple[dict, ExtraPredictor, dict]:
    """M-4 を 1 回回し、共通域・消えた行の予想・M-6 のレグの入力を返す。"""
    from plugins import macro_ensemble, model_candidates
    from scripts.ensemble_base_bakeoff import _run_config

    prices_by_co = panel[0]
    global_last = global_last_week(prices_by_co)
    ex = ExtraPredictor()
    captured: dict = {"n_builds": 0}
    orig_build = macro_ensemble.build_snapshots
    orig_m6_cv = macro_ensemble.plugin._m6_cv

    def _spy_build(*a, **kw):
        # M-2 / M-6 のレグ（`_m2_build`・交差項なし）にだけ消えた行の特徴量を足す。M-1 のレグはそのまま
        if kw.get("build_interactions") is False:
            out = orig_build(*a, **{**kw, "return_no_future": True, "return_no_future_rows": True})
            captured["n_builds"] += 1
            keep, info, counts = select_ended(out[-1], a[0], global_last)
            ex.set_build(out[0], keep)
            captured.update(ended_info=info, kind_counts=counts)
            return out[:-2]
        return orig_build(*a, **kw)

    def _spy_m6_cv(b, m6p, yms, fit_predict=None):
        captured["m6_build"] = b
        return orig_m6_cv(b, m6p, yms, fit_predict=fit_predict)

    with patch.object(macro_ensemble, "build_snapshots", _spy_build), \
         patch.object(macro_ensemble.plugin, "_m6_cv", _spy_m6_cv), \
         patch.object(macro_ensemble, "_make_xgb_fit_predict",
                      ex.wrap("macro_gbdt", macro_ensemble._make_xgb_fit_predict)), \
         patch.object(model_candidates, "make_elasticnet_fit_predict",
                      ex.wrap("macro_enet", model_candidates.make_elasticnet_fit_predict)):
        run = _run_config("M-4(3基底・rank_ic_grid)", BASES, panel, db,
                          weight_method="rank_ic_grid", keep_common=True)
    if captured["n_builds"] != 1:
        raise InputMismatch(f"M-2 / M-6 のレグの構築が {captured['n_builds']} 回（共有の 1 回を想定）。"
                            "M-2 と M-6 の構成が分かれている")
    if "m6_build" not in captured:
        raise InputMismatch("M-6 のレグの入力を捕まえられなかった（_m6_cv が呼ばれていない）")
    if list(run["bases"]) != BASES:
        raise InputMismatch(f"M-4 の基底の並びが想定と違う: {run['bases']}")
    return run, ex, captured


def _metrics(scores: dict) -> dict:
    return {name: metrics_row(o, None) for name, o in scores.items()}


def build_report(run: dict, ex: ExtraPredictor, captured: dict, prices_by_co: dict) -> dict:
    """計測の本体（DB を触らない）。照合が合わなければ InputMismatch。"""
    from plugins.macro_enet import _SIZE_NEUTRAL
    from scripts.sell_mu_source_bakeoff import common_to_dicts

    if ex.errors:
        raise InputMismatch(f"消えた行の予想で失敗が {len(ex.errors)} 件（例: {ex.errors[:3]}）")
    if ex.unmapped:
        raise InputMismatch(f"test 月を特定できなかった fold がある: {dict(ex.unmapped)}")

    preds, y = common_to_dicts(run["common"], BASES)
    order = {ym: [ec for ec, _yh, _y in rows] for ym, rows in run["common"].items()}
    label_prices(prices_by_co, y)
    base_all = score_all({name: preds[name] for name in BASES}, order, y)
    check_against_base_oof(base_all, run["base_oof"])
    for name in PAIR:
        missing = sorted(set(order) - set(ex.raw[name]))
        if missing:
            raise InputMismatch(f"{SHORT[name]} の答えのある行の予想を捕まえていない月がある: {missing[:5]}")

    b = captured["m6_build"]
    m2_extra = dict(ex.extra["macro_gbdt"])
    m6_raw_extra = dict(ex.extra["macro_enet"])
    if set(m2_extra) != set(m6_raw_extra):
        raise InputMismatch("M-2 と M-6 で消えた行の予想が出た (月, 社) が違う")
    if _SIZE_NEUTRAL:
        ext_ids: dict[str, list] = defaultdict(list)
        for ym, ec in m6_raw_extra:
            ext_ids[ym].append(ec)
        lab_ids = {ym: b["ids"].get(ym, []) for ym in ex.raw["macro_enet"]}
        m6_extra, neutral_meta = neutralize_extras(
            ex.raw["macro_enet"], m6_raw_extra, lab_ids,
            log_market_cap_by_ym(lab_ids, b["fin_by_co"], b["market_caps"]),
            log_market_cap_by_ym(dict(ext_ids), b["fin_by_co"], b["market_caps"]),
            preds["macro_enet"])
    else:
        m6_extra, neutral_meta = m6_raw_extra, {}

    info = captured["ended_info"]
    add = sorted(k for k in m6_extra if k[0] in order)
    if any(k in y for k in add):
        raise InputMismatch("消えた行と答えのある行が同じ (月, 社) に重なった")
    y_exit = {k: math.log(1.0 + info[k]["r_last"]) for k in add}
    y_bank = {k: (BANKRUPT_LOG if info[k]["reason"] == "bankruptcy_suspect" else y_exit[k]) for k in add}
    order_with = {ym: ecs + sorted(ec for m, ec in add if m == ym) for ym, ecs in order.items()}
    pair_base = {name: preds[name] for name in PAIR}
    pair_with = {"macro_enet": {**preds["macro_enet"], **{k: m6_extra[k] for k in add}},
                 "macro_gbdt": {**preds["macro_gbdt"], **{k: m2_extra[k] for k in add}}}

    base = {name: base_all[name] for name in PAIR}
    with_exit = score_all(pair_with, order_with, {**y, **y_exit})
    with_bank = score_all(pair_with, order_with, {**y, **y_bank})
    cov = {ym: ecs for ym, ecs in order.items() if ym >= COVERED_FROM}
    cov_with = {ym: ecs for ym, ecs in order_with.items() if ym >= COVERED_FROM}
    base_cov = score_all(pair_base, cov, y)
    with_cov = score_all(pair_with, cov_with, {**y, **y_exit})

    by_month = Counter(k[0] for k in add)
    by_reason = Counter(info[k]["reason"] for k in add)
    return {
        "n_common_rows": len(y),
        "test_months": sorted(order),
        "added": {
            "n": len(add),
            "by_month": dict(sorted(by_month.items())),
            "by_year": dict(sorted(Counter(k[0][:4] for k in add).items())),
            "by_reason": {r: by_reason.get(r, 0) for r in REASONS},
            "n_ended_predicted_outside_test_months": len(m6_extra) - len(add),
            "kind_counts_all_months": captured.get("kind_counts", {}),
            "exit_log_return_mean": (sum(y_exit.values()) / len(y_exit)) if y_exit else None,
        },
        "m6_neutralization": {ym: m for ym, m in sorted(neutral_meta.items())},
        "scores": {
            "base": _metrics(base),
            "with_ended": _metrics(with_exit),
            "with_ended_bankrupt_minus_99": _metrics(with_bank),
            "covered_base": _metrics(base_cov),
            "covered_with_ended": _metrics(with_cov),
        },
        "prereg_decision": include_decision(base, with_exit),
        "reference": {
            "bankrupt_minus_99": include_decision(base, with_bank),
            "covered_only": include_decision(base_cov, with_cov),
        },
    }


# ── 出力 ────────────────────────────────────────────────────────────────────────

def _f(v, digits: int = 4) -> str:
    return "-" if v is None else f"{v:+.{digits}f}"


def _print(report: dict) -> None:
    add = report["added"]
    print(f"\n=== 上場廃止した行を採点に足す（#1197）共通域 {report['n_common_rows']} 行・"
          f"test 月 {len(report['test_months'])}・足した行 {add['n']} ===", flush=True)
    print(f"足した行（年別）: {add['by_year']} / 理由: {add['by_reason']}", flush=True)
    print(f"\n{'variant':<30}{'model':<6}{'rank-IC':>9}{'spread':>9}{'top10':>9}{'prec':>8}{'pctrank':>9}",
          flush=True)
    for variant, rows in report["scores"].items():
        for name, r in rows.items():
            print(f"{variant:<30}{SHORT.get(name, name):<6}{_f(r['rank_ic']):>9}{_f(r['short_side_spread']):>9}"
                  f"{_f(r['top_k_excess_return']):>9}{_f(r['precision'], 3):>8}{_f(r['mean_pct_rank'], 3):>9}",
                  flush=True)
    dec = report["prereg_decision"]
    print(f"\n判定（事前登録）: {dec['decision']}（主審の入れ替わり={dec['flip_primary']}・"
          f"上位10社={dec['flip_top_k']}）", flush=True)
    ref = report["reference"]
    print(f"参考: 倒産の疑いを −99% にした版={ref['bankrupt_minus_99']['decision']} / "
          f"2024 年以降の test 月だけ={ref['covered_only']['decision']}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.measure_delisting_redesign",
                                 description="52 週以内に上場廃止した行を採点に足したときの比較の判定（#1197）")
    prereg.add_arguments(ap)
    ap.add_argument("--smoke", action="store_true", help="企業を絞った軽量パネルで動作確認（判定には使わない）")
    ap.add_argument("--smoke-companies", type=int, default=120)
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--out", default=str(_OUT), help="結果の JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    stamp = prereg.stamp_from_args(args)          # 不一致は重い計算の前に終了コード 2
    if Path(args.prereg).as_posix() != PREREG:
        print(f"[delisting_redesign] NG: 事前登録は {PREREG}（受け取った値: {args.prereg}）", file=sys.stderr)
        return prereg.EXIT_MISMATCH

    from database import SessionLocal
    from scripts._cache import set_refresh
    from scripts.ensemble_base_bakeoff import _load_panel

    set_refresh(args.refresh_cache)
    panel = _load_panel(args)
    db = SessionLocal()
    try:
        try:
            run, ex, captured = run_m4_with_extras(panel, db)
            report = build_report(run, ex, captured, panel[0])
        except InputMismatch as e:
            print(f"[delisting_redesign] NG: {e}", file=sys.stderr)
            return EXIT_INPUT
    finally:
        db.close()
    _print(report)

    from scripts.sell_mu_source_bakeoff import M4_BASES_CAVEAT
    payload = {
        **stamp,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "smoke": args.smoke,
        "caveats": [M4_BASES_CAVEAT,
                    "学習は変えない。消えた行は各 fold の学習済みモデルで予測しただけで、学習データには入れていない",
                    "M-1 は対象外（fit_predict の差し替え口が無く、strict な構築で消えた行の集合も違う）",
                    "2023 年以前に廃止した社はパネルに居ない（#1188）ので、その月は足す行が無い"],
        "rule": {"pair": list(PAIR), "primary_series": list(PRIMARY_SERIES), "alpha": ALPHA, "top_k": TOP_K,
                 "bankrupt_log": BANKRUPT_LOG, "covered_from": COVERED_FROM},
        **report,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
