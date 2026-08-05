"""M-3（週次 DLM）へ新規マクロ・ファクターを昇格すべきかの OOF 実測（昇格ゲート・#409）。

`scripts/macro_feature_bakeoff.py` の M-3 版。あちらは M-2/M-6 の**月次スナップショット**
（月末の値を1点だけ使う）で判定するため、日次系列のスパイクはほぼ落ちる。本スクリプトは
同じ判定作法（ADR-0023）を **週次** の M-3（`plugins/macro_dlm.py`・ADR-0012）へ適用する。

    base      … 現行 DEFAULT_MACRO_FEATURES から候補を除いたもの
    with_cand … base + 候補（状態次元が候補数だけ増える）

判定指標は買い側 rank-IC と売り側 `short_side_spread` の**両方**（ADR-0022 の確定知見＝
両者の順位は一致しない）。有意差は共通 test 期でペアリングした定常ブートストラップ
（`model_stats.paired_ic_significance`・ADR-0018）。

昇格ゲート:
  検定数 = 1モデル（M-3）× 2指標 = 2 → Bonferroni 補正 α = 0.05 / 2 = 0.025。
  どちらか1つでも補正後 α を下回る改善があれば既定採用、なければ選択肢のみに留める。
  M-2/M-6 版が 4 検定なのは2モデル走らせるため。M-3 は週次 DLM 単独なので 2 検定。

M-3 に strict 母集団（`macro_nan_ok=False`）の概念は無い（週ごとに欠損週をスキップする
設計）。代わりに **OOF サンプル数と実行時間**を base/with_cand で比較する（状態次元 +N の
推定コストと、非正水準ガードによる週落ちを可視化する＝#409 検証項目3）。

**低 Egress 設計**（[[feedback_verification_fullloads_exhaust_egress]]）:
週次株価（約128万行・volume 込み）と companies / macro_data は `scripts/_cache.py` の
ローカル pickle を使い、2回目以降は本番 Supabase を一切叩かない。プラグインの
`load_prices` / `load_macro_levels` をキャッシュ読みへ差し替える（execute 本体のロジックは
一切変えないため、出る数字は本番 M-3 の oof_backtest と直接比較できる）。

**測る前にパネルの世代を確かめる**（#456 の実例）: `_cache.py` のキーはテーブル・列の形だけで
決まりデータ世代を含まない（[[feedback_bakeoff_cache_generation_after_data_fix]]）。#456 では
`weekly_prices_full_v1.pkl` が #411（ADR-0025）の履歴バックフィル**前**（2021-01-08 起点・
967,004 行）のまま残り、M-3 の昇格ゲートだけが 49 期の短いパネルで判定されていた。退避して
取り直したら 67 期・964,466 OOF ペア（期 +37%／サンプル +48%）になり、#454 が「負方向・
p=0.084」と読んだ `dlm_jp10y` の点差は p=0.695 まで戻った。実行時に出る
`panel src: price_cos=...` は社数しか見せないので、**キャッシュの起点日と行数を直接見ること**。
`--refresh-cache` は全 `cached()` を無効化して 40MB 級の再 pull を巻き込むため、疑わしい
pkl だけを `_stale_pre<N>/` へ mv して `--allow-full-pull` で取り直す方が安い。

実行例（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）:
    python -m scripts.macro_dlm_feature_bakeoff --preset attention --smoke   # 5社に1社
    python -m scripts.macro_dlm_feature_bakeoff --preset attention           # フル実測
    python -m scripts.macro_dlm_feature_bakeoff --features dlm_news_tone
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins.macro_dlm as dlm  # noqa: E402
from database import SessionLocal, tuning_dry_run, tuning_objective_only  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins.macro_snapshots import load_weekly_prices_chunked, _WEEKLY_PX  # noqa: E402
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402

_OUT_DIR = Path(__file__).resolve().parent / ".cache"

# 昇格判定を通す候補セット。新しいチャネルを足したらここへ1行足す。
PRESETS: dict[str, list[str]] = {
    # #409（ADR-0024 の「日次シグナルは週次 M-3 側が筋」という未検証仮説の消化）。
    "attention": [
        "dlm_news_tone",
        "dlm_news_econ_tone",
        "dlm_news_econ_vol",
        "dlm_wiki_market_attn",
        "dlm_wiki_macro_attn",
    ],
}
# 昇格ゲート: 1モデル（M-3）× 2指標（rank-IC / short_side_spread）の 2 検定を Bonferroni 補正。
N_TESTS = 2
ALPHA = 0.05 / N_TESTS

# 週次株価キャッシュ（close だけの `weekly_prices_close` とは別物）。M-3 の既定価格行動系
# 特徴量 px_volz が volume_sum を要求するため、3列すべてを持つキーを別に張る。
_PX_CACHE_KEY = "weekly_prices_full_v1"
_CO_CACHE_KEY = "bakeoff_companies_v2"   # candidate_bakeoff と共有（素 tuple で保存）


def _load_prices(allow_full_pull: bool) -> dict:
    """{edinet_code: [_WEEKLY_PX(trade_date, close_last, volume_sum), ...]} を返す。"""
    def _pull():
        if not allow_full_pull:
            raise SystemExit(
                f"{_PX_CACHE_KEY} キャッシュがありません。97万行の pull は Egress を食うため"
                "既定で拒否します。--allow-full-pull を付けて1回だけ作成してください。")
        db = SessionLocal()
        try:
            # pickle には素の tuple だけを載せる（namedtuple クラスは載せない）。
            return {ec: [(r.trade_date, r.close_last, r.volume_sum) for r in rows]
                    for ec, rows in load_weekly_prices_chunked(db).items()}
        finally:
            db.close()

    raw = cached(_PX_CACHE_KEY, _pull)
    return {ec: [_WEEKLY_PX(*t) for t in rows] for ec, rows in raw.items()}


def _load_companies(db) -> dict:
    """{edinet_code: _CompanyRow}（industry/sec_code/name のみ）をキャッシュして返す。"""
    from scripts.candidate_bakeoff import _CompanyRow

    def _pull():
        from database import Company
        return {c.edinet_code: (c.edinet_code, c.sec_code, c.name, c.industry)
                for c in db.query(Company).all()}

    return {ec: _CompanyRow(*vals) for ec, vals in cached(_CO_CACHE_KEY, _pull).items()}


def _macro_loader(db, series_codes: list[str], min_date: str | None) -> dict:
    """load_macro_levels のキャッシュ版（系列集合＋開始日でキー分け）。"""
    key = hashlib.md5((",".join(sorted(series_codes)) + "|" + str(min_date)).encode()).hexdigest()[:10]
    return cached(f"dlm_macro_levels_{key}",
                  lambda: dlm._load_macro_levels_impl(db, series_codes, min_date))


def _run(db, factors: list[str], overrides: dict) -> dict:
    """M-3 既定 config のまま macro_features だけ差し替えて oof_backtest を得る。"""
    params = coerce_params(dlm.plugin.params_schema(), {"macro_features": factors, **overrides})
    t0 = time.time()
    # tuning_objective_only: 全社スコアリング（β経路・r_macro）を省き oof_backtest 直後に
    # 早期 return（Issue #299）。tuning_dry_run: producer 永続化（macro_dlm_scores）を no-op。
    with tuning_objective_only(), tuning_dry_run():
        out = dlm.plugin.execute(params, db)
    return {"oof": out["oof_backtest"], "elapsed_sec": round(time.time() - t0, 1),
            "n_factors": len(out["macro_features"]),
            "dropped_factors": [d["feature"] for d in (out.get("diagnostics") or {})
                                .get("dropped_factors", [])] if out.get("diagnostics") else []}


def _row(o: dict) -> dict:
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":           o["rank_ic"]["mean"],
        "rank_ic_std":       o["rank_ic"].get("std"),
        "short_side_spread": o.get("short_side_spread"),
        "short_side_hit":    o.get("short_side_hit_rate"),
        "bottom_q_return":   q[0] if q else None,
        "long_short_spread": o.get("long_short_spread"),
        "n_periods":         o.get("n_periods"),
        "n_oof_samples":     o.get("n_oof_samples"),
    }


def _fmt_sig(sig: dict | None) -> str:
    if not sig:
        return "n/a (common test periods < 2)"
    star = "SIG" if (sig.get("p_value") is not None and sig["p_value"] < ALPHA) else "ns"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={sig.get('p_value', float('nan')):.3f} n={sig['n_common']} "
            f"{star}(alpha={ALPHA:.4f})")


def main() -> None:
    # Windows cp932 では非ASCII記号でクラッシュするため UTF-8 に固定
    # （[[feedback_windows_cp932_stdout_symbols]]）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="M-3 新規マクロ・ファクターの昇格ゲート実測（#409）")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="attention",
                    help=f"判定する候補セット（既定 attention）: {', '.join(sorted(PRESETS))}")
    ap.add_argument("--features", help="候補をカンマ区切りで直接指定（--preset より優先）")
    ap.add_argument("--smoke", action="store_true", help="銘柄を間引いた短時間確認")
    ap.add_argument("--stride", type=int, default=1, help="銘柄の間引き幅（N社に1社）")
    ap.add_argument("--min-weeks", type=int, default=None,
                    help="M-3 の最低週数を上書き（既定はプラグイン既定＝104）")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に本番フル pull を許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    args = ap.parse_args()
    if args.smoke and args.stride <= 1:
        args.stride = 5
    set_refresh(args.refresh_cache)

    cand = ([f.strip() for f in args.features.split(",") if f.strip()]
            if args.features else PRESETS[args.preset])
    unknown = [f for f in cand if f not in dlm._DLM_MACRO_MAP]
    if unknown:
        raise SystemExit(f"未登録のファクターです（_DLM_MACRO_MAP に無い）: {', '.join(unknown)}")
    label = args.preset if not args.features else "custom"
    out_path = _OUT_DIR / f"macro_dlm_feature_bakeoff_{label}.json"
    print(f"candidate ({label}): {', '.join(cand)}", flush=True)

    # DEFAULT_MACRO_FEATURES は昇格済み系列を含みうるので、候補は必ず引いてから base を作る
    # （保留枠 `_PENDING_EVAL_FEATURES` のファクターは元から既定に入っていない）。
    base = [f for f in dlm.DEFAULT_MACRO_FEATURES if f not in cand]
    conds = {"base": base, "with_cand": base + cand}
    overrides = {"min_weeks": args.min_weeks} if args.min_weeks else {}

    db = SessionLocal()
    results: dict[str, dict] = {}
    try:
        prices_by_co = _load_prices(args.allow_full_pull)
        companies = _load_companies(db)
        if args.stride > 1:
            keys = sorted(prices_by_co)[::args.stride]
            prices_by_co = {ec: prices_by_co[ec] for ec in keys}
        print(f"panel src: price_cos={len(prices_by_co)} companies={len(companies)} "
              f"stride={args.stride}", flush=True)

        # プラグインの重いロード2本をキャッシュ読みへ差し替える（execute 本体は無改変）。
        dlm.load_prices = lambda _db: (prices_by_co, companies)
        dlm.load_macro_levels = _macro_loader

        for cond, factors in conds.items():
            r = _run(db, factors, overrides)
            results[cond] = r
            o = r["oof"]
            print(f"[{cond}] factors={r['n_factors']} rank-IC={o['rank_ic']['mean']:+.4f} "
                  f"short_side={o.get('short_side_spread', float('nan')):+.4f} "
                  f"periods={o.get('n_periods')} oof_samples={o.get('n_oof_samples')} "
                  f"({r['elapsed_sec']}s)", flush=True)
            if r["dropped_factors"]:
                print(f"  dropped (coverage<{dlm._MIN_FACTOR_COVERAGE}): "
                      f"{', '.join(r['dropped_factors'])}", flush=True)
        db.commit()
    finally:
        db.close()

    # ── 昇格ゲート判定 ───────────────────────────────────────────────────────
    print(f"\n=== with_cand - base (Bonferroni alpha={ALPHA:.4f}, {N_TESTS} tests) ===",
          flush=True)
    a, b = results["with_cand"]["oof"], results["base"]["oof"]
    sigs: dict[str, dict | None] = {}
    passed: list[str] = []
    for metric, key in (("rank_ic", "rank_ic_by_period"),
                        ("short_side_spread", "short_side_spread_by_period")):
        sig = paired_ic_significance(a.get(key) or {}, b.get(key) or {})
        sigs[metric] = sig
        ok = bool(sig and sig.get("p_value") is not None
                  and sig["p_value"] < ALPHA and sig["mean"] > 0)
        if ok:
            passed.append(metric)
        print(f"  M-3(DLM) {metric:<18} {_fmt_sig(sig)}", flush=True)

    # 状態次元 +N の推定コストと、非正水準ガードによる週落ち（#409 検証項目3）
    d_sec = results["with_cand"]["elapsed_sec"] - results["base"]["elapsed_sec"]
    d_oof = ((a.get("n_oof_samples") or 0) - (b.get("n_oof_samples") or 0))
    print(f"\ncost: elapsed {results['base']['elapsed_sec']}s -> "
          f"{results['with_cand']['elapsed_sec']}s ({d_sec:+.1f}s) / "
          f"oof samples {b.get('n_oof_samples')} -> {a.get('n_oof_samples')} ({d_oof:+d})",
          flush=True)

    if passed:
        verdict = f"promote to DEFAULT_MACRO_FEATURES (passed: {', '.join(passed)})"
    else:
        verdict = "keep as option only (no significant improvement after correction)"
    print(f"\n=== VERDICT: {verdict} ===", flush=True)

    payload = {
        "model": "macro_dlm",
        "candidate": {"label": label, "features": cand},
        "alpha": ALPHA,
        "n_tests": N_TESTS,
        "stride": args.stride,
        "rows": {k: _row(v["oof"]) for k, v in results.items()},
        "cost": {k: {"elapsed_sec": v["elapsed_sec"], "n_factors": v["n_factors"]}
                 for k, v in results.items()},
        "significance": sigs,
        "verdict": verdict,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
