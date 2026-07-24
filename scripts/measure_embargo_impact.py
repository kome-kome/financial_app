"""#363/ADR-0014 の purge/embargo が OOF rank-IC に与える影響を honest 比較する検証スクリプト。

embargo_months=0（従来＝52週先ラベルの前方リーク込み）と =12（purge 済み・honest）で
M-1/M-2 の execute() を回し、`oof_backtest` の rank-IC / long-short spread / hit-rate を
before/after で並べる。#375 の follow-up。

**低 Egress 設計**: 重い stock_price_weekly（約97万行）は `scripts/.cache/weekly_prices_close.pkl`
（Issue #355・`scripts/event_study_*` が作成）を再利用し pull しない。小さい financial_metrics /
companies / macro_data のみ本番から 1 回 pull する。キャッシュが無ければ既定では 97万行 pull を
拒否する（`--allow-full-pull` で明示解除。ただしローカルからの 97万行 pull はストールしやすい
＝ [[feedback_verification_fullloads_exhaust_egress]]）。

**caveat**:
- 価格キャッシュは `week_start`（ISO 週の月曜）で、本番 loader の `trade_date`（週内最終営業日）
  とは数日ずれる。52週先リターンはインデックス基準のため値は不変、月バケットのみ月境界で稀に
  ずれる（rank-IC はほぼ同値）。embargo=0 の M-2 rank-IC が記録済み本番値（≈0.33）を再現する
  ことでこの近似の妥当性を確認できる。
- **M-1 は本番と同一 config（全マクロ・strict）で測る**（#379 まで `use_macro=False` に落として
  いたが、0 サンプルの原因は week_start プロキシではなく低頻度マクロの変換バグだった。修正後は
  offline でも本番と同じ 24ヶ月/約71.2k サンプルを再現する＝実測で確認済み）。
- ただし M-1 strict のスナップショットは HY_OAS/IG_OAS の収集開始（2023-06）に律速され 24ヶ月しか
  無く、embargo=12 では `min_train_months=6` と合わせて fold が 2 期しか立たない（rank-IC の
  統計的信頼性は低い）。マクロ履歴の backfill が前提条件（別 Issue）。

実行: `python -m scripts.measure_embargo_impact`（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）
出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import database
from database import (
    Company,
    FinancialMetric,
    SessionLocal,
    tuning_dry_run,
    tuning_objective_only,
)
from plugins import get_plugin
from plugins.utils import coerce_params
import plugins.macro_snapshots as ms
import plugins.macro_gbdt as m2mod
import plugins.macro_risk_return as m1mod
import plugins.macro_ensemble as m4mod
from scripts._cache import cached, set_refresh

# 3モデルとも本番既定 config で測る（#379 で M-1 strict が offline でも成立するようになった）。
PARAM_OVERRIDES = {"M-1": {}, "M-2": {}, "M-4": {}}
LABELS = {"M-1": "M-1 (default, full macro strict)",
          "M-2": "M-2 (default, full macro)",
          "M-4": "M-4 (stack of M-1 + M-2, both default)"}
EMBARGOS = (0, 12)
METRICS = ("ic_mean", "ic_std", "ic_n", "ls", "hit", "n_periods", "n_oof")


def _load_prices(allow_full_pull: bool) -> dict:
    """既存 weekly_prices_close キャッシュを M-1/M-2 の _WEEKLY_PX 形式へ変換して返す。"""
    def _pull_full():
        if not allow_full_pull:
            raise SystemExit(
                "weekly_prices_close キャッシュがありません。97万行の pull はストールしやすいため既定で"
                "拒否します。scripts/event_study_multivariate_xgboost.py 等でキャッシュを作るか、"
                "--allow-full-pull を付けて実行してください。"
            )
        from database import StockPriceWeekly
        db = SessionLocal()
        try:
            out: dict[str, list] = defaultdict(list)
            for ec, ws, cl in (db.query(StockPriceWeekly.edinet_code,
                                        StockPriceWeekly.week_start,
                                        StockPriceWeekly.close_last).all()):
                out[ec].append((ws, cl))
            import pandas as pd
            return {ec: pd.DataFrame(rows, columns=["week_start", "close_last"]).sort_values("week_start")
                    for ec, rows in out.items()}
        finally:
            db.close()

    px_df = cached("weekly_prices_close", _pull_full)
    prices_by_co: dict[str, list] = {}
    for ec, df in px_df.items():
        rows = [ms._WEEKLY_PX(str(ws), float(cl), None)
                for ws, cl in zip(df["week_start"].tolist(), df["close_last"].tolist())]
        rows.sort(key=lambda r: r.trade_date)
        prices_by_co[ec] = rows
    return prices_by_co


def _install_offline_loaders(prices_by_co: dict, db) -> None:
    """load_data/preload_macro/get_producer_scores を差し替え、重い再 pull を封じる。"""
    fin_by_co: dict[str, list] = defaultdict(list)
    for r in (db.query(FinancialMetric)
              .order_by(FinancialMetric.edinet_code, FinancialMetric.period_end).all()):
        fin_by_co[r.edinet_code].append(r)
    fin_by_co = dict(fin_by_co)
    companies = {c.edinet_code: c for c in db.query(Company).all()}
    print(f"fin_cos={len(fin_by_co)} companies={len(companies)}", flush=True)

    macro_holder: dict[tuple, dict] = {}
    real_preload = ms.preload_macro

    def fake_load_data(_db):
        return prices_by_co, fin_by_co, companies

    def fake_preload(_db, pbc, macro_names=None):
        key = tuple(sorted(macro_names or []))
        if key not in macro_holder:
            macro_holder[key] = real_preload(db, pbc, list(macro_names) if macro_names else None)
        return macro_holder[key]

    for mod in (m1mod, m2mod, m4mod):
        mod.load_data = fake_load_data
        mod.preload_macro = fake_preload
        if hasattr(mod, "get_producer_scores"):
            mod.get_producer_scores = lambda *a, **k: {}
    # #379 修正後は M-4 内部の M-1 も本番既定（strict 全マクロ）のままで成立する。
    m4mod.SUB_PARAM_OVERRIDES.clear()


def _run(db) -> dict:
    results: dict = {}
    for short, name in (("M-1", "macro_risk_return"), ("M-2", "macro_gbdt"),
                        ("M-4", "macro_ensemble")):
        p = get_plugin(name)
        params = coerce_params(p.params_schema(), PARAM_OVERRIDES[short])
        for emb in EMBARGOS:
            m1mod.LABEL_HORIZON_MONTHS = emb
            m2mod.LABEL_HORIZON_MONTHS = emb
            m4mod.LABEL_HORIZON_MONTHS = emb
            try:
                with tuning_objective_only(), tuning_dry_run():
                    res = p.execute(params, db)
                oof = res.get("oof_backtest") or {}
                ric = oof.get("rank_ic", {})
                results[(short, emb)] = {
                    "ic_mean": ric.get("mean"), "ic_std": ric.get("std"), "ic_n": ric.get("n"),
                    "ls": oof.get("long_short_spread"), "hit": oof.get("hit_rate"),
                    "n_periods": oof.get("n_periods"), "n_oof": oof.get("n_oof_samples"),
                }
                base_oof = res.get("base_oof_backtest") or {}
            except Exception as e:  # noqa: BLE001
                results[(short, emb)] = {"error": f"{type(e).__name__}: {e}"}
                base_oof = {}
            print(f"[{short} embargo={emb:>2}] {results[(short, emb)]}", flush=True)
            # M-4 は共通 (ym,ec) 域に制限した基底の OOF も出す（apples-to-apples 判定・ADR-0015）
            for bname, bbt in base_oof.items():
                bric = bbt.get("rank_ic", {})
                print(f"    base-on-common {bname}: ic_mean={bric.get('mean')} "
                      f"ls={bbt.get('long_short_spread')} n_oof={bbt.get('n_oof_samples')}",
                      flush=True)
    return results


def _fmt(x) -> str:
    return "None" if x is None else (f"{x:+.4f}" if isinstance(x, float) else str(x))


def _report(results: dict) -> None:
    print("\n================ #375 HONEST COMPARISON (embargo 0 -> 12) ================")
    hdr = f"{'model':5} {'metric':10} {'embargo=0':>12} {'embargo=12':>12} {'delta':>12}"
    print(hdr)
    print("-" * len(hdr))
    for short in ("M-1", "M-2", "M-4"):
        print(f"# {LABELS[short]}")
        r0, r12 = results[(short, 0)], results[(short, 12)]
        if "error" in r0 or "error" in r12:
            print(f"{short:5} ERROR e0={r0.get('error')} e12={r12.get('error')}")
            continue
        for m in METRICS:
            v0, v12 = r0.get(m), r12.get(m)
            d = (v12 - v0) if isinstance(v0, (int, float)) and isinstance(v12, (int, float)) else None
            print(f"{short:5} {m:10} {_fmt(v0):>12} {_fmt(v12):>12} {_fmt(d):>12}")


def main() -> None:
    ap = argparse.ArgumentParser(description="#363/ADR-0014 embargo の OOF rank-IC 影響を honest 比較")
    ap.add_argument("--refresh-cache", action="store_true", help="価格キャッシュを再取得（97万行 pull）")
    ap.add_argument("--allow-full-pull", action="store_true", help="価格キャッシュ不在時に 97万行 pull を許可")
    args = ap.parse_args()
    if args.refresh_cache:
        set_refresh(True)

    print(f"is_local={database._is_local}", flush=True)
    prices_by_co = _load_prices(args.allow_full_pull)
    print(f"cached companies (prices)={len(prices_by_co)}", flush=True)

    db = SessionLocal()
    try:
        _install_offline_loaders(prices_by_co, db)
        results = _run(db)
    finally:
        db.close()
    _report(results)


if __name__ == "__main__":
    main()
