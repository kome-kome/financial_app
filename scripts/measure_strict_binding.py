"""M-1 strict の学習窓を「何が」律速しているかを実測する診断スクリプト（ADR-0016 フォローアップ）。

背景: M-1（`macro_risk_return`）は `macro_nan_ok=False`＝strict で、「選択中の全マクロ特徴が
同時に非 None」の行しか使わない（同一母集団の構造保証・ADR-0003）。このため既定マクロに
カバレッジの短い系列が1本混じるだけで学習窓が丸ごと縮む。#381（ADR-0016）はこれを
HY_OAS/IG_OAS の ICE ライセンス truncate で踏み、既定を非ICE代替へ差し替えて解消した。

その後「次の律速はどれか」がコード・ADR に推測（当時のコモディティ 2020-07 開始）のまま残り、
実データが変わっても更新されない状態になっていた。本スクリプトは**推測ではなく実測**で

  1. 既定マクロ各特徴が最初に非 None になる月（＝マクロ側のカバレッジ律速候補）
  2. strict / nan_ok / マクロ無し の3条件で母集団（月数・サンプル数）が変わるか
     ＝ strict 制約そのものが今なお母集団を削っているか
  3. 削っていない場合、母集団を決めているのは株価履歴か財務履歴か

を出し、`VERDICT` として1行にまとめる。マクロ系列を既定へ足す前後（`macro_feature_bakeoff.py`
の昇格ゲート）と、学習窓が短いと感じたときに回す。

判定の読み方:
  - `strict is NOT binding` … strict と nan_ok の母集団が一致。マクロ既定を弄っても学習窓は
    伸びない。窓を伸ばしたければ株価/財務の履歴そのものを延ばすしかない（容量トレードオフ）。
  - `strict IS binding` … 差分あり。上の (1) の `latest-start` 特徴が律速候補なので、
    ADR-0016 と同じ手順（既定から外す＋非truncate代替を入れる）を検討する。

データは `scripts/_cache.py` 経由のローカル pickle を使う（Issue #355・本番 Egress ゼロ）。
週次株価キャッシュ（`weekly_prices_close`）が無い場合は既定で 97 万行 pull を拒否する
（`--allow-full-pull` で明示解除）。本番書込なし・読取専用。

実行: `python -m scripts.measure_strict_binding`（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）
      `python -m scripts.measure_strict_binding --features macro_hy_oas_zscore,macro_vix_zscore`
出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402
from database import SessionLocal  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_snapshots import (  # noqa: E402
    HORIZON_WEEKS,
    MACRO_FEATURE_NAMES,
    _macro_from_cache,
    build_snapshots,
    preload_macro,
)
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import _load_financials, _load_prices  # noqa: E402

_OUT_DIR = Path(__file__).resolve().parent / ".cache"

# 母集団比較の条件。最初の3つは M-1 の build 契約（交差項あり・価格特徴なし）で揃え
# `macro_nan_ok` だけを振る（build 契約まで変えると「strict の影響」と「交差項の影響」が
# 混ざる）。4つ目だけは M-2 の実契約（交差項なし・価格特徴あり・nan_ok）で、M-4 の共通
# (ym,ec) 域を狭めているのが M-1 側か M-2 側かを見るための対照（ADR-0015 の base-on-common）。
_CONDS = (
    # (label, use_macro, macro_nan_ok, contract) contract: "m1" | "m2"
    ("strict(M-1 default)", True,  False, "m1"),
    ("nan_ok(M-2 policy)",  True,  True,  "m1"),
    ("no-macro",            False, False, "m1"),
    ("M-2 actual contract", True,  True,  "m2"),
)


def _macro_cache(db, prices_by_co, macro_names: list) -> dict:
    """candidate_bakeoff と同じキー規約でマクロをキャッシュ共有する。"""
    key = hashlib.md5(",".join(sorted(macro_names)).encode()).hexdigest()[:10]
    return cached(f"bakeoff_macro_{key}", lambda: preload_macro(db, prices_by_co, macro_names))


def _price_only_months(prices_by_co: dict) -> tuple[list[str], list[str]]:
    """財務・マクロを一切要求しないときに「ラベル付きスナップショットが立つ月」を返す。

    build_snapshots の月末判定（`dates[i][:7] != dates[i+1][:7]`）・`snap_idx >= 4`・
    52週先ラベル（`snap_idx + HORIZON_WEEKS < n`）と同じ条件を、財務レコードの有無だけ
    外して再現する。母集団の上限（＝株価履歴だけで決まる窓）を測るための対照。
    """
    yms: set[str] = set()
    all_dates: set[str] = set()
    for rows in prices_by_co.values():
        n = len(rows)
        dates = [r.trade_date for r in rows]
        all_dates.update(dates)
        month_ends = [i for i in range(n - 1) if dates[i][:7] != dates[i + 1][:7]] + [n - 1]
        for i in month_ends:
            if i >= 4 and i + HORIZON_WEEKS < n:
                yms.add(dates[i][:7])
    return sorted(yms), sorted(all_dates)


def _macro_first_seen(macro_cache: dict, month_last: dict, feats: list[str]) -> tuple[dict, list]:
    """各マクロ特徴が最初に非 None になる月と、全特徴が同時に非 None の月一覧を返す。"""
    first: dict[str, str] = {}
    all_ok: list[str] = []
    for ym in sorted(month_last):
        vals = _macro_from_cache(macro_cache, month_last[ym], feats)
        for f, v in vals.items():
            if v is not None and f not in first:
                first[f] = ym
        if all(v is not None for v in vals.values()):
            all_ok.append(ym)
    return first, all_ok


def main() -> None:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="M-1 strict が学習窓を律速しているかの実測診断（ADR-0016 フォローアップ）")
    ap.add_argument("--features", help="診断するマクロ特徴をカンマ区切りで指定（既定=M-1 の既定集合）")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--json", help="結果 JSON の保存先（既定 scripts/.cache/measure_strict_binding.json）")
    args = ap.parse_args()
    if args.refresh_cache:
        set_refresh(True)

    m1p = coerce_params(get_plugin("macro_risk_return").params_schema(), {})
    feats = ([f.strip() for f in args.features.split(",") if f.strip()]
             if args.features else list(m1p["macro_features"]))
    unknown = [f for f in feats if f not in MACRO_FEATURE_NAMES]
    if unknown:
        raise SystemExit(f"未知のマクロ特徴: {unknown}")

    print(f"is_local={database._is_local}", flush=True)
    prices_by_co = _load_prices(args.allow_full_pull)
    px_yms, px_dates = _price_only_months(prices_by_co)
    print(f"weekly prices: companies={len(prices_by_co)} weeks={len(px_dates)} "
          f"range={px_dates[0]}..{px_dates[-1]}", flush=True)
    print(f"price-only labelled months (no fin/macro required): n={len(px_yms)} "
          f"range={px_yms[0]}..{px_yms[-1]}", flush=True)

    db = SessionLocal()
    try:
        fin_by_co, companies = _load_financials(db)
        pes = sorted(str(r.period_end)[:10] for rows in fin_by_co.values() for r in rows
                     if r.period_end)
        print(f"financials: companies={len(fin_by_co)} records={len(pes)} "
              f"period_end={pes[0]}..{pes[-1]}", flush=True)

        macro_cache = _macro_cache(db, prices_by_co, feats)
        db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない
        month_last = {d[:7]: d for d in px_dates}   # 各月の最終週（昇順走査で上書き）
        first_seen, all_ok = _macro_first_seen(macro_cache, month_last, feats)

        print(f"\n=== macro coverage ({len(feats)} default features) ===", flush=True)
        for f in feats:
            print(f"  {first_seen.get(f, 'NEVER'):>8}  {f}")
        latest = max(first_seen.values()) if first_seen else None
        binding_feats = sorted(f for f, m in first_seen.items() if m == latest)
        never = [f for f in feats if f not in first_seen]
        print(f"  latest-start month = {latest} (features starting then: {len(binding_feats)})")
        if never:
            print(f"  NEVER available   = {never}  <- strict kills the whole panel")
        print(f"  months where ALL selected macro are non-None: n={len(all_ok)} "
              f"range={all_ok[0] if all_ok else None}..{all_ok[-1] if all_ok else None}")

        m2p = coerce_params(get_plugin("macro_gbdt").params_schema(), {})
        print("\n=== snapshot population ===", flush=True)
        pops: dict[str, dict] = {}
        for label, use_macro, nan_ok, contract in _CONDS:
            names = feats if use_macro else []
            p = m1p if contract == "m1" else m2p
            s, _meta, _cur, feat_names = build_snapshots(
                prices_by_co, fin_by_co, companies,
                macro_cache if use_macro else {},
                p["fin_features"], names,
                p["use_momentum"], p["momentum_window"], p["min_coverage"],
                build_interactions=(contract == "m1"), macro_nan_ok=nan_ok,
                price_features=(list(m2p.get("price_features") or [])
                                if contract == "m2" else None),
            )
            yms = sorted(s)
            pops[label] = {"months": len(yms), "samples": sum(len(v) for v in s.values()),
                           "first_ym": yms[0] if yms else None,
                           "last_ym": yms[-1] if yms else None,
                           "n_features": len(feat_names)}
            p = pops[label]
            print(f"  {label:22} months={p['months']:>3} range={p['first_ym']}..{p['last_ym']} "
                  f"samples={p['samples']:>7} features={p['n_features']}", flush=True)
    finally:
        db.close()

    strict, nan_ok_pop = pops["strict(M-1 default)"], pops["nan_ok(M-2 policy)"]
    no_macro = pops["no-macro"]
    lost_m = nan_ok_pop["months"] - strict["months"]
    lost_s = nan_ok_pop["samples"] - strict["samples"]
    is_binding = lost_m > 0 or lost_s > 0

    print("\n=== VERDICT ===", flush=True)
    if is_binding:
        print(f"strict IS binding: -{lost_m} months / -{lost_s} samples vs nan_ok.", flush=True)
        print(f"  binding candidates (latest coverage start {latest}): {binding_feats}", flush=True)
        print("  -> follow ADR-0016: drop them from DEFAULT_MACRO_FEATURES and add a "
              "non-truncated substitute.", flush=True)
    else:
        print("strict is NOT binding: same months/samples as nan_ok "
              "(no row is lost to the all-macro-non-None rule).", flush=True)
        # マクロを外しても母集団が変わらない = 窓は株価/財務だけで決まっている。
        px_cap = len(px_yms)
        if no_macro["months"] == strict["months"]:
            src = ("financial records (fin coverage)" if strict["months"] < px_cap
                   else "weekly price history (52w-ahead label horizon)")
            print(f"  window is set by data history, not by the model: price-only cap="
                  f"{px_cap} months vs actual={strict['months']} months -> limiting side = {src}",
                  flush=True)
        print("  -> widening the window requires longer stock_price_weekly / financial_records "
              "history (DB capacity trade-off), not macro/default changes.", flush=True)

    # M-4 の共通 (ym,ec) 域はレグ母集団の積。どちらのレグが狭いかを実測で示す（ADR-0015）。
    m2c = pops["M-2 actual contract"]
    if m2c["months"] < strict["months"]:
        narrow = "M-2 contract (price feature warmup / no interactions)"
    elif m2c["months"] > strict["months"]:
        narrow = "M-1 contract (strict + interactions)"
    else:
        narrow = "neither (same month count)"
    print(f"  M-4 common-domain check: M-1 build={strict['months']} months / "
          f"M-2 build={m2c['months']} months -> narrower leg = {narrow}", flush=True)

    out = {
        "features": feats,
        "price": {"companies": len(prices_by_co), "weeks": len(px_dates),
                  "first": px_dates[0], "last": px_dates[-1],
                  "labelled_months_cap": len(px_yms)},
        "financials": {"companies": len(fin_by_co), "records": len(pes),
                       "period_end_first": pes[0], "period_end_last": pes[-1]},
        "macro_first_seen": first_seen,
        "macro_never": never,
        "macro_all_non_null_months": len(all_ok),
        "populations": pops,
        "strict_binding": is_binding,
    }
    path = Path(args.json) if args.json else _OUT_DIR / "measure_strict_binding.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {path}", flush=True)


if __name__ == "__main__":
    main()
