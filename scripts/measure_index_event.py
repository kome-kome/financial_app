"""指数イベントのイベントスタディ（2022 前例・段階的ウエイト低減 493 社）の計測 CLI（#1031・親 #1008）。

事前登録 `docs/prereg/index-event-2026.md`（#1007）の順序どおりに段を分ける:

- `stage-a`（盲検）: 暦・マッチング・バランス・first stage・σ / SE(δ̂) / MDE・placebo の誤検出率・R0。
  **処置群の事象窓のリターンを読まない**（部品 `index_event_study.stage_a` が入口で公表週以降の終値を
  落とす）。結果は事前登録の追記節「stage A（盲検）の結果」へ書き足す。
- `stage-b`（開封）: #1032 で足す。
- `m-family`（M 族・§6.2・#1009）: 本番の OOF（`momentum_gate --size-neutral` が書く CSV の M-2 raw と
  M-6 size_neutral）で、処置群と対照の placement bias の差 d_t を E_t・P_t へ回帰し、層別に引いて全期で
  固定した擬似処置群の順列（1,000 回）で p を出す。マッチ対は stage A が書いたファイルを読み、その凍結値が
  今回の `--prereg-sha` と違えば終了コード 2。**確証の検定なので、事前登録の方針（#1033）が決まる前に
  回さない**。

## 守ること

- **`--prereg` と `--prereg-sha` を必須にし、本文の sha256 が凍結値と一致しなければ終了コード 2**
  （`scripts/prereg.py`）。出力 JSON に本文の sha256・HEAD SHA・事前登録の Issue の URL を入れる。
- **要約の JSON に銘柄コードを入れない。** 追記節（リポジトリは PUBLIC）や Issue へ貼る元になる。
  マッチ対（どの処置社にどの対照が付いたか）は、公開されない `.index_lists/derived/` へ別に書く
  （JPX の一覧と同じ扱い・ADR-0068 決定 2）。stage B と #1009 はこの対を読む——data_j の断面は毎月
  増えて C1 が変わるので、計算し直すと同じ対にならない。
- 週次の終値は学習ラベルと同じローダ（`plugins.macro_snapshots.load_weekly_prices_chunked`）で読み、
  別のローダを書かない（事前登録 §0）。売買代金（`turnover_sum`）だけは既存のローダが読まないので、
  対象の社と週に絞って読む（週次ローダの SELECT へ列を足すと差分ロードのキャッシュの形が変わる）。
- 日中キューで単独に回す（`measure:index-event-a`・並走に敏感）。

終了コード: 0 = 完了（R0 が pass か pending）/ 2 = 事前登録の不一致・台帳や断面が使えない /
3 = R0 が不成立（JSON は書いてから止まる。first stage の t < 3 は暦の誤り＝事前登録 §5.3）。

実行:
    python -m scripts.measure_index_event stage-a \\
        --prereg docs/prereg/index-event-2026.md --prereg-sha <凍結値>

出力は ASCII 記号のみ（Windows cp932 リダイレクト対策）。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import prereg  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / ".cache" / "index_event_stage_a.json"
DEFAULT_PAIRS_OUT = ROOT / ".index_lists" / "derived" / "index_event_2022_pairs.json"
DEFAULT_M_OOF = ROOT / "scripts" / ".cache" / "momentum_gate_size_neutral_oof.csv"
DEFAULT_M_OUT = ROOT / "scripts" / ".cache" / "index_event_m_family.json"
EXIT_FAILED = 2
EXIT_R0_FAILED = 3
CHUNK = 500
# 売買代金を読む週の範囲の余白。マッチの 52 週窓（最も早いマッチ日から）と first stage の
# 直前 26 週の基準（最初の圧力週から）を覆う。
TURNOVER_LEAD_WEEKS = 60


# ── 純関数（DB に触れない）──────────────────────────────────────────────────────

def jsonable(v):
    """NaN / inf を None へ（JSON に NaN を書かない）。"""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def _fmt(v, spec: str = "+.3f") -> str:
    return "n/a" if v is None else format(v, spec)


def report(summary: dict) -> list[str]:
    """標準出力へ出す行（ASCII 記号のみ）。"""
    lines = [f"[stage-a] R0: {summary['r0']['status']}"]
    for key, s in summary["matching"].items():
        smd = " ".join(f"{k}={_fmt(v)}" for k, v in s["smd"].items())
        tag = " (main)" if s["main"] else ""
        lines.append(f"  matching {key}{tag}: matched {s['n_matched']}/{s['n_treated']}"
                     f" controls_used {s['n_controls_used']} unmatched {s['unmatched']['by_reason']}")
        lines.append(f"    SMD {smd} balance_ok={s['balance_ok']}")
    fs = summary["first_stage"]
    lines.append("  first stage: " + " ".join(f"m{r['m']}:t={_fmt(r['t'], '.2f')}(n={r['n']})"
                                             for r in fs["rows"]) + f" pass={fs['pass']}")
    p = summary["power"]
    if p["mde"] is None:
        lines.append(f"  MDE: n/a -- {p.get('reason', '')}")
    else:
        lines.append(f"  sigma(weekly)={p['sigma_weekly']:.4f} SE(delta_hat)={p['se_delta_hat']:.4f}"
                     f" MDE={p['mde']:.4f} (n_weeks={p['n_weeks']})")
    pl = summary["placebo"]
    if pl["fpr"] is None:
        lines.append(f"  placebo: n/a -- {pl.get('reason', '')}")
    else:
        lines.append(f"  placebo: FPR={pl['fpr']:.4f} (usable {pl['n_usable']}/{pl['n_candidates']})"
                     f" switch_to_empirical_p={pl['switch_to_empirical_p']}")
    for name, item in summary["r0"]["items"].items():
        lines.append(f"  R0 {name}: pass={item['pass']}")
    return lines


def m_report(verdict: dict, results: dict) -> list[str]:
    """M 族の標準出力の行（ASCII 記号のみ）。"""
    import index_event_study as S

    lines = [f"[m-family] Bonferroni alpha={S.M_ALPHA:.4f} (two-sided, permutation p)"]
    for model, coefs in verdict.items():
        r = results[model]
        fit = r["fit"]
        lines.append(f"  {model}: periods={fit['n_periods']} ({fit['first']}..{fit['last']})"
                     f" matched_treated={r['n_matched_treated']}"
                     f" pseudo_size={r['permutation']['pseudo_size']}"
                     f" shortfall={sum(r['permutation']['shortfall'].values())}")
        for k, v in coefs.items():
            lines.append(f"    {k}={_fmt(v['coef'], '+.4f')} p_perm={_fmt(v['p'], '.4f')}"
                         f" significant={v['significant']}")
    return lines


# ── I/O ─────────────────────────────────────────────────────────────────────────

def _chunks(codes: Sequence[str]) -> Iterable[list]:
    for i in range(0, len(codes), CHUNK):
        yield list(codes[i:i + CHUNK])


def load_turnover(db, codes: Iterable[str], since: date, until: date) -> dict:
    """{社: {週の月曜: 売買代金}}（`turnover_sum` が NULL の週は入れない）。"""
    from database import StockPriceWeekly as W

    out: dict = defaultdict(dict)
    for part in _chunks(sorted(codes)):
        rows = (db.query(W.edinet_code, W.week_start, W.turnover_sum)
                  .filter(W.edinet_code.in_(part),
                          W.week_start >= since.isoformat(), W.week_start <= until.isoformat())
                  .all())
        for ec, ws, to in rows:
            if to is not None:
                out[ec][date.fromisoformat(ws)] = float(to)
    return dict(out)


def load_industries(db, codes: Iterable[str]) -> dict:
    """企業表の 33 業種（財務行に業種が無いときの代わり。`build_snapshots` と同じ順）。"""
    from database import Company

    out = {}
    for part in _chunks(sorted(codes)):
        for ec, ind in db.query(Company.edinet_code, Company.industry).filter(
                Company.edinet_code.in_(part)).all():
            out[ec] = ind
    return out


def load_firms(db, prices: dict, codes: set) -> dict:
    """{役割: {社: Firm}}。マッチ日ごとに、その月末に見えていた通期行と月末終値 × F（ADR-0057）。"""
    import index_event_study as S
    import sector_gap_asof as SGA
    from plugins.utils import shares_outstanding

    fin_by_co, factors = SGA.load_inputs(db, [])
    fin_by_co = {ec: fin_by_co.get(ec, []) for ec in codes}
    closes_by_co = {ec: SGA.month_end_closes(prices[ec]) for ec in codes if prices.get(ec)}
    industries = load_industries(db, codes)
    out = {}
    for role, d in S.MATCH_DATES_2022.items():
        ym = d.strftime("%Y-%m")
        firms = {}
        for rec in SGA.asof_records(fin_by_co, closes_by_co, factors, ym):
            bar = date.fromisoformat(closes_by_co[rec.edinet_code][ym][0][:10])
            firms[rec.edinet_code] = S.Firm(
                bar=bar, price=float(rec.stock_price), shares=shares_outstanding(rec),
                bps=rec.bs_bps, industry33=rec.industry or industries.get(rec.edinet_code))
        out[role] = firms
    return out


def load_stage_a_inputs(db, ledger, *, known_at: datetime) -> dict:
    """台帳・断面・週次の終値と売買代金・マッチ日の財務を読み、`index_event_study.stage_a` の引数にする。"""
    import index_event_study as S
    import index_events as IE
    from database import jpx_listing_snapshot_at
    from plugins.macro_snapshots import load_weekly_prices_chunked

    treated = set(IE.members(ledger, S.REVIEW_2022, "transition"))
    if not treated:
        raise ValueError(f"台帳の {S.REVIEW_2022} の処置群が 0 社")
    review_2023 = "topix-2023-10-review"
    subgroups = {
        "released": set(IE.members(ledger, review_2023, "released")) & treated,
        "continuing": set(IE.members(ledger, review_2023, "transition")) & treated,
        "delisted_or_succeeded": set(ledger.delisted) & treated,
    }
    as_of, snapshot = jpx_listing_snapshot_at(db, known_at)
    if as_of is None:
        raise ValueError("data_j の断面（jpx_listing_snapshots）が無い")
    groups_2022 = set().union(*(v for (review, _kind), v in ledger.groups.items()
                                if review in (S.REVIEW_2022, review_2023)))
    controls = {
        "C1": S.c1_controls(snapshot.values(), ledger.mentions),
        "C2": S.c2_controls(IE.members(ledger, "topix-2026-10", "continuing"), groups_2022),
    }
    codes = treated | controls["C1"] | controls["C2"]
    prices = load_weekly_prices_chunked(db, with_volume=False)
    prices = {ec: prices[ec] for ec in codes if prices.get(ec)}
    closes = S.weekly_frame({
        ec: {S.iso_monday(date.fromisoformat(p.trade_date[:10])): p.close_last for p in rows}
        for ec, rows in prices.items()})
    calendar = S.pressure_calendar(IE.STEPS_2022)
    since = S.iso_monday(min(S.MATCH_DATES_2022.values())) - S.WEEK * TURNOVER_LEAD_WEEKS
    until = max(pw.week for pw in calendar)
    turnover = S.weekly_frame(load_turnover(db, codes, since, until))
    d2 = IE.DELISTED_DURING["d2"]
    review = IE.REVIEWS
    r0_inputs = {
        "counts": {"transition": review[S.REVIEW_2022].counts["transition"],
                   "released": review[review_2023].counts["released"],
                   "continuing": review[review_2023].counts["transition"]},
        "d1": len(IE.DELISTED_DURING["d1"]),
        "d2": None if d2 is None else len(d2),
        "resolution": {k: r["rate"] for k, r in ledger.stats.get("resolution", {}).items()},
        "min_resolution": IE.MIN_RESOLUTION,
    }
    weeks = list(closes.index)
    return {
        "stage_a": dict(closes=closes, turnover=turnover, firms_by_role=load_firms(db, prices, codes),
                        treated=treated, subgroups=subgroups, controls=controls, calendar=calendar,
                        cutoff=S.blind_from(review[S.REVIEW_2022].announced), r0_inputs=r0_inputs),
        "data": {"snapshot_as_of": as_of.isoformat(),
                 "weekly_first": weeks[0].isoformat() if weeks else None,
                 "weekly_last": weeks[-1].isoformat() if weeks else None,
                 "n_codes": len(codes), "n_with_prices": len(prices)},
    }


def read_oof_csv(path: Path, runs: dict) -> dict:
    """規模の中立化モードの OOF の CSV → {モデル: {(ym, 社): (予測, 実現)}}。`runs` は {モデル: 条件}。"""
    want = {(cond, model): model for model, cond in runs.items()}
    out: dict = {model: {} for model in runs}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            model = want.get((row["cond"], row["model"]))
            if model is not None:
                out[model][(row["ym"], row["edinet_code"])] = (float(row["yhat"]), float(row["y"]))
    return out


def load_strata_variables(db):
    """全社のマッチ日（主）の変数（logcap・流動性・17 業種）。stage A と同じ読み込みと計算を全社へ当てる。"""
    import index_event_study as S
    from plugins.macro_snapshots import load_weekly_prices_chunked

    prices = {ec: rows for ec, rows in load_weekly_prices_chunked(db, with_volume=False).items() if rows}
    codes = set(prices)
    closes = S.weekly_frame({
        ec: {S.iso_monday(date.fromisoformat(p.trade_date[:10])): p.close_last for p in rows}
        for ec, rows in prices.items()})
    match_week = S.iso_monday(S.MATCH_DATES_2022["main"])
    turnover = S.weekly_frame(
        load_turnover(db, codes, match_week - S.WEEK * TURNOVER_LEAD_WEEKS, match_week))
    firms = load_firms(db, prices, codes)["main"]
    return S.match_variables(firms, closes, turnover)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def run_stage_a(args) -> int:
    import index_event_study as S
    import index_events as IE
    from database import SessionLocal

    stamp = prereg.stamp_from_args(args)
    known_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        ledger = IE.load_ledger(db, root=Path(args.lists_dir) if args.lists_dir else None)
        if not ledger.ok:
            print(f"[measure_index_event] NG: 指数イベント台帳が使えない（{ledger.reason}）", file=sys.stderr)
            return EXIT_FAILED
        try:
            inputs = load_stage_a_inputs(db, ledger, known_at=known_at)
        except ValueError as e:
            print(f"[measure_index_event] NG: {e}", file=sys.stderr)
            return EXIT_FAILED
    summary, pairs = S.stage_a(**inputs["stage_a"])
    header = {"stage": "A", **stamp, "generated_at": known_at.isoformat(timespec="seconds"),
              "data": inputs["data"]}
    write_json(Path(args.out), {**header, **summary})
    write_json(Path(args.pairs_out), {**header, "pairs": pairs})
    for line in report(summary):
        print(line)
    print(f"wrote {args.out}")
    print(f"wrote {args.pairs_out}")
    return EXIT_R0_FAILED if summary["r0"]["status"] == "fail" else 0


def _fail(msg: str) -> int:
    print(f"[measure_index_event] NG: {msg}", file=sys.stderr)
    return EXIT_FAILED


def run_m_family(args) -> int:
    import index_event_study as S
    import index_events as IE
    from database import SessionLocal

    stamp = prereg.stamp_from_args(args)
    pairs_path, oof_path = Path(args.pairs), Path(args.oof_csv)
    if not pairs_path.is_file():
        return _fail(f"マッチ対が無い: {pairs_path}（先に stage-a を回す）")
    pairs_doc = json.loads(pairs_path.read_text(encoding="utf-8"))
    if pairs_doc.get("prereg_sha256") != stamp["prereg_sha256"]:
        return _fail(f"マッチ対は別の凍結値で作られた（{pairs_doc.get('prereg_sha256')}）。stage-a を回し直す")
    pairs = pairs_doc.get("pairs", {}).get(S.spec_key(*S.MAIN_SPEC))
    if not pairs:
        return _fail(f"マッチ対に主の仕様 {S.spec_key(*S.MAIN_SPEC)} が無い")
    if not oof_path.is_file():
        return _fail(f"OOF の CSV が無い: {oof_path}（先に momentum_gate --size-neutral を回す）")
    oof = read_oof_csv(oof_path, S.M_MODELS)
    empty = [f"{cond}|{model}" for model, cond in S.M_MODELS.items() if not oof[model]]
    if empty:
        return _fail(f"OOF の CSV に {empty} の行が無い")
    known_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        ledger = IE.load_ledger(db, root=Path(args.lists_dir) if args.lists_dir else None)
        if not ledger.ok:
            return _fail(f"指数イベント台帳が使えない（{ledger.reason}）")
        treated = set(IE.members(ledger, S.REVIEW_2022, "transition"))
        variables = load_strata_variables(db)
    strata_by_ec = S.strata(variables)
    used_controls = {c for cs in pairs.values() for c in cs}
    eligible = set(strata_by_ec) - treated - used_controls
    calendar = S.pressure_calendar(IE.STEPS_2022)
    results = {model: S.m_family_test(oof[model], pairs, calendar, strata_by_ec, eligible)
               for model in S.M_MODELS}
    verdict = S.m_family_verdict(results)
    write_json(Path(args.out), {
        "stage": "M", **stamp, "generated_at": known_at.isoformat(timespec="seconds"),
        "inputs": {"oof_csv": str(oof_path), "runs": S.M_MODELS, "pairs": str(pairs_path),
                   "pairs_generated_at": pairs_doc.get("generated_at"),
                   "pairs_snapshot_as_of": (pairs_doc.get("data") or {}).get("snapshot_as_of"),
                   "n_strata_firms": len(strata_by_ec), "n_eligible": len(eligible),
                   "n_treated_itt": len(treated), "n_controls_used": len(used_controls)},
        "alpha": S.M_ALPHA, "verdict": verdict, "models": results, "definitions": S.M_DEFINITIONS})
    for line in m_report(verdict, results):
        print(line)
    print(f"wrote {args.out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.measure_index_event",
        description="指数イベントのイベントスタディ（2022 前例）を事前登録どおりに測る")
    sub = ap.add_subparsers(dest="stage", required=True)
    a = sub.add_parser("stage-a", help="盲検の下調べ（処置群の事象窓のリターンを読まない）")
    prereg.add_arguments(a)
    a.add_argument("--out", default=str(DEFAULT_OUT), help="要約の JSON（銘柄コードを含めない）")
    a.add_argument("--pairs-out", default=str(DEFAULT_PAIRS_OUT),
                   help="マッチ対の JSON（銘柄コードを含む・公開しない置き場）")
    a.add_argument("--lists-dir", help="指数の一覧の置き場（既定はリポジトリの .index_lists）")
    m = sub.add_parser("m-family", help="M 族（本番の OOF が処置群をどこへ置いていたか・順列検定）")
    prereg.add_arguments(m)
    m.add_argument("--oof-csv", default=str(DEFAULT_M_OOF),
                   help="momentum_gate --size-neutral が書く行ごとの OOF の CSV")
    m.add_argument("--pairs", default=str(DEFAULT_PAIRS_OUT), help="stage-a が書いたマッチ対の JSON")
    m.add_argument("--out", default=str(DEFAULT_M_OUT), help="結果の JSON（銘柄コードを含めない）")
    m.add_argument("--lists-dir", help="指数の一覧の置き場（既定はリポジトリの .index_lists）")
    args = ap.parse_args(argv)

    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    return run_m_family(args) if args.stage == "m-family" else run_stage_a(args)


if __name__ == "__main__":
    sys.exit(main())
