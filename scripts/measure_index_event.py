"""指数イベントのイベントスタディ（2022 前例・段階的ウエイト低減 493 社）と 2026 群の前向き監視の計測 CLI
（#1031・親 #1008・#1061・親 #1011）。

事前登録 `docs/prereg/index-event-2026.md`（#1007）の順序どおりに段を分ける:

- `stage-a`（盲検）: 暦・マッチング・バランス・first stage・σ / SE(δ̂) / MDE・placebo の誤検出率・R0。
  **処置群の事象窓のリターンを読まない**（部品 `index_event_study.stage_a` が入口で公表週以降の終値を
  落とす）。結果は事前登録の追記節「stage A（盲検）の結果」へ書き足す。
- `stage-b`（開封・#1032）: 事象窓の CAR・I・R・κ・δ̂・CAR_post・ρ、E族の確証（E1・E2・E4・Holm）、
  I・δ̂・κ・ρ の 90% CI（残差の定常ブートストラップ）、生存バイアスの Lee bounds、記述の量（公表・回間ドリフト・
  除外後の他の窓・2023-10 に解除された社・ラベル水準の DiD・gap_ratio）。**事前登録の追記節「stage A（盲検）の
  結果」が未記入なら終了コード 2**（開封の順序）。マッチ対の凍結値が違っても 2。
- `m-family`（M 族・§6.2・#1009）: 本番の OOF（`momentum_gate --size-neutral` が書く CSV の M-2 raw と
  M-6 size_neutral）で、処置群と対照の placement bias の差 d_t を E_t・P_t へ回帰し、層別に引いて全期で
  固定した擬似処置群の順列（1,000 回）で p を出す。マッチ対は stage A が書いたファイルを読み、その凍結値が
  今回の `--prereg-sha` と違えば終了コード 2。**確証の検定なので、事前登録の方針（#1033）が決まる前に
  回さない**。
- `monitor`（2026 群の前向き監視・§6.3・#1061）: `--cohort topix-2026-10`（移行措置 683 社）は、低減ごとの
  圧力窓 CAR を O'Brien-Fleming 型の逐次検定にかける。w_m + 2 の週の終値がそろっていない回は「未到来」で
  検定に入れない。`--cohort topix-2026-10-add`（新規採用 35 社）は記述だけ。マッチングは stage A と同じ部品を
  2026 のマッチ日へ当て、**初回の実行で対を凍結して 2 回目以降は読み直す**（凍結値か群の違う対なら終了
  コード 2）。市場区分別の CAR と露出 τ（`scripts/index_exposure.py`）を記述として併記する。R0 とは独立
  （§6.3）なので追記節の記入を待たない。棄却しても終了コードは 0（結果であって失敗ではない）。
  記述として予測帯（stage B の 2022 の I の 90% CI × s・`--stage-b`）、帯の外が 2 回続いたときの #1001 向け
  起票文（自動では起票しない）、売り圧力（時価総額 × 刻み ÷ 売買代金）の三分位別 CAR を出す（#1062）。
  stage B が無ければ帯は unavailable で、監視は止めない。三分位はマッチ対と同時に凍結する。

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
- 日中キューで単独に回す（`measure:index-event-a` / `-m` / `-b` / `-monitor` / `-monitor-add`・並走に敏感）。

終了コード: 0 = 完了（R0 が pass か pending）/ 2 = 事前登録の不一致・台帳や断面が使えない /
3 = R0 が不成立（JSON は書いてから止まる。first stage の t < 3 は暦の誤り＝事前登録 §5.3）。
`monitor` は 0 = 完了（棄却・非棄却・未到来のどれでも）/ 2 = 事前登録や凍結したマッチ対の不一致・台帳や断面が使えない。

実行:
    python -m scripts.measure_index_event stage-a \\
        --prereg docs/prereg/index-event-2026.md --prereg-sha <凍結値>
    python -m scripts.measure_index_event monitor --cohort topix-2026-10 \\
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
DEFAULT_B_OUT = ROOT / "scripts" / ".cache" / "index_event_stage_b.json"
# 前向き監視（群ごと）。対は公開しない置き場、結果は銘柄コードを含めない
MONITOR_PAIRS_DIR = ROOT / ".index_lists" / "derived"
MONITOR_OUT_DIR = ROOT / "scripts" / ".cache"
# 開封の前に結果が書かれていなければならない追記節の見出し（事前登録の冒頭「開封の順序」）
STAGE_A_HEADING = "### stage A（盲検）の結果"
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


def b_report(summary: dict) -> list[str]:
    """stage B の標準出力の行（ASCII 記号のみ）。"""
    q, ci, e = summary["quantities"], summary["ci"], summary["e_family"]
    lines = [f"[stage-b] I={_fmt(q['I'], '+.4f')} R={_fmt(q['R'], '+.4f')} kappa={_fmt(q['kappa'], '+.3f')}"
             f" delta_hat={_fmt(q['delta_hat'], '+.4f')} CAR_post={_fmt(q['car_post'], '+.4f')}"
             f" rho={_fmt(q['rho'], '+.3f')}",
             f"  I 90% CI={ci['I']} delta_hat 90% CI={ci['delta_hat']} kappa CI={ci['kappa']} rho CI={ci['rho']}",
             f"  E family (Holm, alpha={e['alpha']}): "
             + " ".join(f"{k}: p={_fmt(e['p'][k], '.4f')} sig={e['significant'][k]}" for k in e["tests"]),
             f"  switch_to_empirical_p={e['switch_to_empirical_p']}"]
    for name, row in summary["survival_bias"].items():
        lines.append(f"  survival {name}: retention T={_fmt(row['retention_treated'], '.3f')}"
                     f" C={_fmt(row['retention_control'], '.3f')} lee={row['lee_bounds']}")
    return lines


def monitor_report(cohort: str, summary: dict, pairs_note: str) -> list[str]:
    """前向き監視の標準出力の行（ASCII 記号のみ）。"""
    lines = [f"[monitor] cohort={cohort} matched={summary['n_treated_matched']}"
             f" with_ar={summary['n_treated_with_ar']} weekly_last={summary['weekly_last']} pairs={pairs_note}"]
    seq = summary["sequential"]
    if seq is not None:
        for r in seq["looks"]:
            lines.append(f"  k{r['k']} w={r['pressure_week']} test_after={r['test_week']} t={r['t']:.3f}"
                         f" z_bound=-{r['z_bound']:.3f} status={r['status']} car={_fmt(r['car'], '+.4f')}"
                         f" cum={_fmt(r['cum_car'], '+.4f')} se={_fmt(r['se'], '.4f')} z={_fmt(r['z'], '+.3f')}"
                         f" rejected={r['rejected']}")
        lines.append(f"  first_rejection={seq['first_rejection']} (one-sided, alpha={seq['alpha']})")
    d = summary["descriptive"]
    for name, b in (d.get("bands") or {}).items():
        lines.append(f"  band {name}: s={_fmt(b['s'], '.3f')} [{_fmt(b['lo'], '+.4f')}, {_fmt(b['hi'], '+.4f')}]")
    if seq is not None:
        outs = " ".join(f"k{r['k']}={r['outside']}" for r in seq["looks"] if r.get("outside") is not None)
        lines.append(f"  band step outside: {outs or '(none tested with a band)'}"
                     f" alert={d.get('band_alert')}")
    pr = d.get("pressure")
    if pr:
        lines.append(f"  pressure: median_2026={_fmt(pr['median_2026'], '.4f')}"
                     f" median_2022={_fmt(pr['median_2022'], '.4f')} n_scored={pr['n_scored']}")
    for grp, row in (d.get("pressure_tercile_car") or {}).items():
        cars = " ".join(f"m{m}={_fmt(v, '+.4f')}" for m, v in row["pressure_car"].items())
        lines.append(f"  pressure {grp}: n={row['n_treated']} {cars or '(no look due yet)'}")
    if d.get("issue_draft"):
        lines.append("  issue_draft: written to the JSON (descriptive.issue_draft). Paste it into #1001 by hand")
    for seg, row in d["by_segment"].items():
        cars = " ".join(f"m{m}={_fmt(v, '+.4f')}" for m, v in row["pressure_car"].items())
        lines.append(f"  segment {seg}: n={row['n_treated']} {cars or '(no look due yet)'}")
    if "anticipation" in d:
        a = d["anticipation"]
        lines.append(f"  anticipation {a['window'][0]}..{a['window'][1]}: car={_fmt(a['car'], '+.4f')}")
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


def load_firms(db, prices: dict, codes: set, match_dates: Optional[dict] = None) -> dict:
    """{役割: {社: Firm}}。マッチ日ごとに、その月末に見えていた通期行と月末終値 × F（ADR-0057）。

    `match_dates` の既定は 2022 前例（§3.3）。前向き監視は 2026 のマッチ日を渡す。
    """
    import index_event_study as S
    import sector_gap_asof as SGA
    from plugins.utils import shares_outstanding

    fin_by_co, factors = SGA.load_inputs(db, [])
    fin_by_co = {ec: fin_by_co.get(ec, []) for ec in codes}
    closes_by_co = {ec: SGA.month_end_closes(prices[ec]) for ec in codes if prices.get(ec)}
    industries = load_industries(db, codes)
    out = {}
    for role, d in (match_dates or S.MATCH_DATES_2022).items():
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


def month_end_labels(prices: dict, codes: Iterable[str], yms: set) -> dict:
    """{(ym, 社): 本番の学習ラベル}＝月末の足から HORIZON_WEEKS 本先の足との log 比（ラベル水準の DiD・記述）。"""
    from plugins.macro_snapshots import HORIZON_WEEKS, month_end_indices

    out = {}
    for ec in codes:
        rows = prices.get(ec) or []
        dates = [r.trade_date for r in rows]
        for i in month_end_indices(dates):
            ym = dates[i][:7]
            if ym in yms and i + HORIZON_WEEKS < len(rows):
                c0, c1 = rows[i].close_last, rows[i + HORIZON_WEEKS].close_last
                if c0 and c1 and c0 > 0 and c1 > 0:
                    out[(ym, ec)] = math.log(c1 / c0)
    return out


def load_gaps(db, prices: dict, yms: list) -> dict:
    """月末ごとの時点再現の gap_ratio {(社, ym): 値}（ADR-0057・`sector_gap_asof`）。記述にだけ使う。"""
    import sector_gap_asof as SGA

    gaps, _stats = SGA.build_asof_gaps(db, prices, yms)
    return {(ym, ec): g for (ec, ym), g in gaps.items()}


def load_pairs(path: Path, stamp: dict) -> tuple:
    """stage A のマッチ対（主の仕様）。戻り値は (ファイルの中身, 対, 失敗の理由 or None)。"""
    import index_event_study as S

    if not path.is_file():
        return None, None, f"マッチ対が無い: {path}（先に stage-a を回す）"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("prereg_sha256") != stamp["prereg_sha256"]:
        return doc, None, f"マッチ対は別の凍結値で作られた（{doc.get('prereg_sha256')}）。stage-a を回し直す"
    pairs = doc.get("pairs", {}).get(S.spec_key(*S.MAIN_SPEC))
    if not pairs:
        return doc, None, f"マッチ対に主の仕様 {S.spec_key(*S.MAIN_SPEC)} が無い"
    return doc, pairs, None


def monitor_paths(cohort: str) -> tuple:
    """(凍結するマッチ対, 結果) の既定のパス。"""
    return (MONITOR_PAIRS_DIR / f"index_event_{cohort}_pairs.json",
            MONITOR_OUT_DIR / f"index_event_monitor_{cohort}.json")


def read_stage_b_ci(path: Path, stamp: dict) -> tuple:
    """予測帯の材料（stage B の 2022 の I の 90% CI・#1062）。戻り値は (CI or None, 状態の 1 語)。

    無い・凍結値が違う・CI が無いときは None を返し、帯は unavailable になる（監視そのものは止めない）。
    """
    if not path.is_file():
        return None, "missing"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("prereg_sha256") != stamp["prereg_sha256"]:
        return None, "prereg_mismatch"
    ci = (doc.get("ci") or {}).get("I")
    if not ci or len(ci) != 2 or any(v is None for v in ci):
        return None, "no_ci"
    return [float(ci[0]), float(ci[1])], "ok"


def cohort_pressure(db, prices: dict, treated: Iterable[str], base_day: date, step) -> dict:
    """{処置社: 売り圧力}（#1062）。主のマッチ日の時価総額と、その週より前の売買代金で作る。"""
    import index_event_study as S

    codes = set(treated)
    firms = load_firms(db, prices, codes, {"main": base_day})["main"]
    week = S.iso_monday(base_day)
    turnover = S.weekly_frame(load_turnover(db, codes, week - S.WEEK * (S.FIRST_STAGE_BASE_WEEKS + 1), week))
    return S.pressure_scores(firms, turnover, base_day, float(step))


def _median(values) -> Optional[float]:
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def read_frozen_pairs(path: Path, stamp: dict, cohort: str) -> tuple:
    """凍結したマッチ対。戻り値は (ファイルの中身 or None＝まだ無い, 失敗の理由 or None)。"""
    if not path.is_file():
        return None, None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("prereg_sha256") != stamp["prereg_sha256"]:
        return None, (f"凍結したマッチ対は別の凍結値で作られた（{doc.get('prereg_sha256')}）: {path}。"
                      "黙って作り直さない（消して回し直すかは人が決める）")
    if doc.get("cohort") != cohort:
        return None, f"凍結したマッチ対は別の群（{doc.get('cohort')}）のもの: {path}"
    return doc, None


def match_cohort(db, ledger, snapshot: dict, prices: dict, treated: set, cohort) -> dict:
    """2026 のマッチ日（§3.3）で、stage A と同じ部品（対照・マッチ変数・4 仕様のマッチング）を当てる。"""
    import index_event_study as S
    import index_events as IE

    groups = set().union(*(IE.members(ledger, cohort.review, k) for k in ("transition", "new_add")))
    controls = {"C1": S.c1_controls(snapshot.values(), ledger.mentions),
                "C2": S.c2_controls(IE.members(ledger, cohort.review, "continuing"), groups)}
    codes = treated | controls["C1"] | controls["C2"]
    sub = {ec: prices[ec] for ec in codes if prices.get(ec)}
    closes = S.weekly_frame({
        ec: {S.iso_monday(date.fromisoformat(p.trade_date[:10])): p.close_last for p in rows}
        for ec, rows in sub.items()})
    dates = S.MATCH_DATES_2026
    since = S.iso_monday(min(dates.values())) - S.WEEK * TURNOVER_LEAD_WEEKS
    turnover = S.weekly_frame(load_turnover(db, codes, since, S.iso_monday(max(dates.values()))))
    firms = load_firms(db, sub, codes, dates)
    variables = {role: S.match_variables(f, closes, turnover) for role, f in firms.items()}
    specs, pairs, _matchings = S.match_specs(variables, treated, controls, {}, dates)
    return {"matching": specs, "pairs": pairs}


def load_exposure(db, ledger, asof: date) -> dict:
    """露出 τ（#1006・記述）: 5 モデルの現行の予測の上位・下位に、移行措置・新規採用の社がどれだけ並ぶか。

    `scripts/index_exposure.py` の読み込みと表をそのまま使う（推奨プリセットの採点は重いので入れない）。
    """
    import index_events as IE
    from scripts import index_exposure as IX

    tables = IX.model_scores(db)
    return {"review": IX.REVIEW, "asof": asof.isoformat(),
            "tables": {name: IX.exposure_table(s, IE.group_labels(ledger, s.keys(), on=asof, known_at=asof))
                       for name, s in tables.items()}}


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
    pairs_doc, pairs, err = load_pairs(pairs_path, stamp)
    if err:
        return _fail(err)
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


def run_stage_b(args) -> int:
    import index_event_study as S
    import index_events as IE
    from database import SessionLocal
    from plugins.macro_snapshots import load_weekly_prices_chunked

    stamp = prereg.stamp_from_args(args)
    if not prereg.section_recorded(prereg.read_document(args.prereg), STAGE_A_HEADING):
        return _fail(f"事前登録の追記節「{STAGE_A_HEADING.lstrip('# ')}」が未記入。開封（stage B）は stage A の結果を"
                     "書き足した後（事前登録の冒頭「開封の順序」）")
    pairs_path = Path(args.pairs)
    pairs_doc, pairs, err = load_pairs(pairs_path, stamp)
    if err:
        return _fail(err)
    known_at = datetime.now(timezone.utc)
    review = IE.REVIEWS[S.REVIEW_2022]
    with SessionLocal() as db:
        ledger = IE.load_ledger(db, root=Path(args.lists_dir) if args.lists_dir else None)
        if not ledger.ok:
            return _fail(f"指数イベント台帳が使えない（{ledger.reason}）")
        released = set(IE.members(ledger, "topix-2023-10-review", "released"))
        prices = {ec: rows for ec, rows in load_weekly_prices_chunked(db, with_volume=False).items() if rows}
        codes = set(pairs) | {c for cs in pairs.values() for c in cs}
        closes = S.weekly_frame({
            ec: {S.iso_monday(date.fromisoformat(p.trade_date[:10])): p.close_last for p in prices[ec]}
            for ec in codes if ec in prices})
        labels = month_end_labels(prices, codes, set(S.ym_range(*S.E3_BASE)) | set(S.ym_range(*S.E3_EVENT)))
        gap_yms = S.ym_range(*S.GAP_PERIOD)
        gaps = load_gaps(db, prices, gap_yms)
    summary = S.stage_b(closes=closes, pairs=pairs, calendar=S.pressure_calendar(IE.STEPS_2022),
                        announce_week=S.announcement_week(review.announced, review.after_close),
                        released=released, labels=labels, gaps=gaps, gap_yms=gap_yms)
    weeks = list(closes.index)
    write_json(Path(args.out), {
        "stage": "B", **stamp, "generated_at": known_at.isoformat(timespec="seconds"),
        "inputs": {"pairs": str(pairs_path), "pairs_generated_at": pairs_doc.get("generated_at"),
                   "pairs_snapshot_as_of": (pairs_doc.get("data") or {}).get("snapshot_as_of"),
                   "n_treated_matched": len(pairs), "n_controls": len(codes) - len(pairs),
                   "weekly_first": weeks[0].isoformat() if weeks else None,
                   "weekly_last": weeks[-1].isoformat() if weeks else None},
        **summary})
    for line in b_report(summary):
        print(line)
    print(f"wrote {args.out}")
    return 0


def run_monitor(args) -> int:
    import index_event_study as S
    import index_events as IE
    import prediction_snapshots as PS
    from database import SessionLocal, jpx_listing_snapshot_at
    from plugins.macro_snapshots import load_weekly_prices_chunked

    stamp = prereg.stamp_from_args(args)
    cohort = S.MONITOR_COHORTS[args.cohort]
    default_pairs, default_out = monitor_paths(args.cohort)
    pairs_path = Path(args.pairs) if args.pairs else default_pairs
    out_path = Path(args.out) if args.out else default_out
    frozen, err = read_frozen_pairs(pairs_path, stamp, args.cohort)
    if err:
        return _fail(err)
    main_key = S.spec_key(*S.MAIN_SPEC, S.MATCH_DATES_2026)
    known_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        ledger = IE.load_ledger(db, root=Path(args.lists_dir) if args.lists_dir else None)
        if not ledger.ok:
            return _fail(f"指数イベント台帳が使えない（{ledger.reason}）")
        treated = set(IE.members(ledger, cohort.review, cohort.kind))
        if not treated:
            return _fail(f"台帳の {cohort.review} の {cohort.kind} が 0 社")
        as_of, snapshot = jpx_listing_snapshot_at(db, known_at)
        if as_of is None:
            return _fail("data_j の断面（jpx_listing_snapshots）が無い")
        prices = {ec: rows for ec, rows in load_weekly_prices_chunked(db, with_volume=False).items() if rows}
        froze_now = frozen is None
        if froze_now:
            frozen = {"stage": "monitor-pairs", "cohort": args.cohort, **stamp,
                      "generated_at": known_at.isoformat(timespec="seconds"),
                      "data": {"snapshot_as_of": as_of.isoformat()},
                      **match_cohort(db, ledger, snapshot, prices, treated, cohort)}
            write_json(pairs_path, frozen)
        pairs = (frozen.get("pairs") or {}).get(main_key)
        if not pairs:
            return _fail(f"マッチ対に主の仕様 {main_key} が無い（マッチした処置社が 0 社）: {pairs_path}")
        segments = {r.edinet_code: r.market_segment for r in snapshot.values() if r.edinet_code in pairs}
        exposure = load_exposure(db, ledger, PS.jst_today())
        # 売り圧力（#1062・確証の群だけ）。2026 の三分位はマッチ対と同時に凍結し、2022 の中央値は毎回作る
        terciles, medians, pressure_note = None, {}, "not_confirmatory"
        if cohort.confirmatory:
            frozen_p = frozen.get("pressure")
            if frozen_p is None:
                scores = cohort_pressure(db, prices, pairs, S.MATCH_DATES_2026["main"], IE.STEP_2026)
                frozen_p = {"base_day": S.MATCH_DATES_2026["main"].isoformat(), "median": _median(scores.values()),
                            "terciles": S.pressure_terciles(scores) if scores else {}}
                if froze_now:
                    frozen["pressure"] = frozen_p
                    write_json(pairs_path, frozen)
                pressure_note = "frozen_now" if froze_now else "not_frozen"
            else:
                pressure_note = "reused"
            terciles, medians["2026"] = frozen_p["terciles"], frozen_p["median"]
            _doc22, pairs22, err22 = load_pairs(Path(args.pairs_2022), stamp)
            if pairs22:
                medians["2022"] = _median(cohort_pressure(
                    db, prices, pairs22, S.MATCH_DATES_2022["main"], IE.STEP_2022).values())
            else:
                print(f"[monitor] 2022 の売り圧力を作れない（{err22}）。圧力比の帯は unavailable")
    i_ci, stage_b_note = read_stage_b_ci(Path(args.stage_b), stamp)
    codes = set(pairs) | {c for cs in pairs.values() for c in cs}
    closes = S.weekly_frame({
        ec: {S.iso_monday(date.fromisoformat(p.trade_date[:10])): p.close_last for p in prices[ec]}
        for ec in codes if ec in prices})
    review = IE.REVIEWS[cohort.review]
    summary = S.monitor(closes=closes, pairs=pairs,
                        calendar=S.pressure_calendar(IE.SCHEDULES[(cohort.review, cohort.kind)].steps),
                        announce_week=S.announcement_week(review.announced, review.after_close),
                        segments=segments, confirmatory=cohort.confirmatory,
                        anticipation_from=cohort.anticipation_from, i_ci=i_ci, terciles=terciles,
                        pressure_medians=medians, cohort=args.cohort)
    summary["descriptive"]["exposure_tau"] = exposure
    write_json(out_path, {
        "stage": "monitor", "cohort": args.cohort, **stamp, "generated_at": known_at.isoformat(timespec="seconds"),
        "inputs": {"pairs": str(pairs_path), "pairs_frozen_now": froze_now,
                   "pairs_generated_at": frozen.get("generated_at"),
                   "pairs_snapshot_as_of": (frozen.get("data") or {}).get("snapshot_as_of"),
                   "snapshot_as_of": as_of.isoformat(), "n_treated_itt": len(treated),
                   "n_controls": len(codes) - len(pairs),
                   "stage_b": str(args.stage_b), "stage_b_ci": stage_b_note, "pressure_terciles": pressure_note},
        "matching": frozen.get("matching"), **summary})
    for line in monitor_report(args.cohort, summary, "frozen_now" if froze_now else "reused"):
        print(line)
    if froze_now:
        print(f"wrote {pairs_path}")
    print(f"wrote {out_path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import index_event_study as S

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
    b = sub.add_parser("stage-b", help="開封（事象窓の量と E族の確証）。追記節の stage A が未記入なら止まる")
    prereg.add_arguments(b)
    b.add_argument("--pairs", default=str(DEFAULT_PAIRS_OUT), help="stage-a が書いたマッチ対の JSON")
    b.add_argument("--out", default=str(DEFAULT_B_OUT), help="結果の JSON（銘柄コードを含めない）")
    b.add_argument("--lists-dir", help="指数の一覧の置き場（既定はリポジトリの .index_lists）")
    mo = sub.add_parser("monitor", help="2026 群の前向き監視（低減ごとの圧力窓 CAR の O'Brien-Fleming 逐次検定）")
    prereg.add_arguments(mo)
    mo.add_argument("--cohort", required=True, choices=sorted(S.MONITOR_COHORTS),
                    help="topix-2026-10＝移行措置（確証）/ topix-2026-10-add＝新規採用（記述だけ）")
    mo.add_argument("--pairs", help="凍結するマッチ対の JSON（既定は .index_lists/derived/index_event_<群>_pairs.json"
                                    "・銘柄コードを含む・公開しない置き場）")
    mo.add_argument("--out", help="結果の JSON（既定は scripts/.cache/index_event_monitor_<群>.json・銘柄コードを含めない）")
    mo.add_argument("--lists-dir", help="指数の一覧の置き場（既定はリポジトリの .index_lists）")
    mo.add_argument("--stage-b", default=str(DEFAULT_B_OUT),
                    help="予測帯の材料（stage B の JSON の ci.I）。無ければ帯は unavailable（#1062）")
    mo.add_argument("--pairs-2022", default=str(DEFAULT_PAIRS_OUT),
                    help="圧力比の分母に使う 2022 前例のマッチ対（stage-a が書いたもの）")
    args = ap.parse_args(argv)

    from collector_utils import force_utf8_stdout
    force_utf8_stdout()
    return {"stage-a": run_stage_a, "m-family": run_m_family, "stage-b": run_stage_b,
            "monitor": run_monitor}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
