"""モメンタム特徴量（`use_momentum`）を既定 ON にすべきかの OOF 実測（昇格ゲート）。

`use_momentum` は 2026-06-20 に既定 OFF で導入された。理由は2つある:

  (a) **データ制約**: 52週先リターン（未来が必要）と 12ヶ月モメンタム（過去が必要）を同時に
      要求すると、週次株価が約2年分しかない環境では両条件を満たす月が約1ヶ月の薄帯へ収縮し、
      walk-forward CV が 0 fold になった（MODELS.md §9.8-4）。
  (b) **保守ゲート**: `px_*` / `monotone` / `sector` と同じく「既定 OFF で入れ、OOF の ON/OFF
      実測で有効性を示してから既定化」する慣行（ADR-0019）。

(a) は #198 の Yahoo バックフィルで解けている（2026-08-31 実測: `stock_price_weekly` は
2019-07-29〜・1,306,610 行・4,024 社。104週以上の履歴を持つ社が 3,686＝92%）。一方 (b) は
**未消化**で、ON/OFF の実測は一度も取られていない——ADR-0021（M-6 昇格）も ADR-0022（既定
mu_source を M-6 へ）も、実測条件はすべて `use_momentum=False` だった。本スクリプトはその
欠けている実測を埋める。

判定対象は「特徴量セットにモメンタムを**足すか否か**」であってモデルの優劣ではない。よって
同一モデル・同一 fold・同一パネル入力のまま `build_snapshots` へ渡す `use_momentum` だけを
差し替えた2条件を比較する（`scripts/macro_feature_bakeoff.py` と同型。あちらが差し替えるのは
`macro_names`）。

    off … use_momentum=False（現行既定・M-2/M-6 の本番構成）
    on  … use_momentum=True, momentum_window=12（12-1 モメンタム・Jegadeesh-Titman 1993）

**母集団が動く点が macro_feature_bakeoff との決定的な違い**（本スクリプト固有の設計）:
マクロ系列の追加は M-2/M-6 が `macro_nan_ok=True` なので母集団を動かさないが、モメンタムは
`macro_snapshots._build_snapshots_impl` の `if mom is None: continue` が**行ごと落とす**ため、
ON では履歴不足の社・月が母集団から消える。さらに `min_coverage` の充足率が `c/n → (c+1)/(n+1)`
へ変わって ON 側がわずかに緩くなるので、**厳密な部分集合ですらない**。母集団差を交絡させたまま
測ると「モメンタムの効果」と「母集団が変わった効果」が分離できない（#454 の列順交絡と同型）。
そこで:

  1. 両条件のパネル規模（月数 / サンプル数 / 社数 / 特徴量数）を必ず出す
  2. **主判定は共通 (ym, ec) 域へ制限した OOF**（ADR-0015 の base-on-common と同じ発想）
  3. 生母集団のままの値も併記する（本番の運用形に対応するため）

測る手続きは書き直さない（ADR-0041 の教訓＝書き直すと本番と別物を測る）。walk-forward →
`oof_backtest` は `scripts/candidate_bakeoff.run_one`、有意差は `model_stats.
paired_ic_significance`（ADR-0018 の定常ブートストラップ）、(ym,ec) 突合は
`plugins.macro_ensemble._align` をそのまま使う。

昇格ゲート（#372 / ADR-0023 と同じ作法）:
  検定数 = 2モデル（M-2 / M-6） × 2指標（買い側 rank-IC / 売り側 short_side_spread）= 4
  → Bonferroni 補正 α = 0.05 / 4 = 0.0125。共通域の判定でどれか1つでも補正後 α を下回る
  **改善**があれば既定 ON、なければ選択肢のまま（棄却を ADR へ記録する）。
  買い側だけで決めない理由は ADR-0022 の確定知見（買い側 rank-IC の順位と売り側 spread の
  順位は一致しない）。

データは `scripts/_cache.py` 経由のローカル pickle を使う。**モメンタムは過去履歴を読む特徴量
なので、週次株価キャッシュが旧世代だと ON 条件だけが不当に不利になり判定そのものが壊れる**
（#456 と同型。mtime は当てにならない）。起点日・行数・社数を実行時に必ず印字する。

実行例（`-m` 必須・[[feedback_scripts_dir_needs_module_invocation]]）:
    python -m scripts.momentum_gate --smoke                    # 5社に1社へ間引いた経路確認
    python -m scripts.momentum_gate                            # フル実測
    python -m scripts.momentum_gate --models elasticnet        # M-6 だけ
    python -m scripts.momentum_gate --json scripts/.cache/momentum_gate.json

## 窓モード（`--windows`・#592）

上の昇格ゲートは **ON/OFF の2条件**で、窓は 12 に固定している（同時に振ると「窓を選んだこと」
自体が過剰適合になるため）。ところが**その窓選びは `tuning_search_space` の探索へ丸投げされ、
そちらには共通域制限が無い**。実際 M-1 の leaderboard は

    mw=18 → 13 fold → 0.3003 ／ mw=12 → 15 fold → 0.2846 ／ mw=6 → 17 fold → 0.2817
    ／ モメンタム無し → 19 fold → 0.2605

と、**窓が長い＝母集団が縮む＝スコアが高い**が完全に単調で、窓の効果と母集団が縮む効果が
分離できていない（`n_oof_samples` と `n_periods` の Spearman が完全一致）。`--windows` は
その分離を**この昇格ゲートと同じ手続き**（共通月 → 共通 (ym,ec) 域）で行うためのモードである:

    python -m scripts.momentum_gate --models risk_return --windows 3,6,12,18,24 --smoke
    python -m scripts.momentum_gate --models risk_return,xgb_m2 --windows 6,12,18

窓モードは既定の判定に一切影響しない（`--windows` 未指定なら条件も alpha も従来どおり）。
alpha は検定数から導出するので、窓を5本振れば 1/5 に締まる。

**`--smoke` の共通域の数値は読んではいけない**（経路確認専用）。`_thin` は各月の先頭から
stride 刻みで**並び順**に選ぶため、母集団が条件ごとに1社ずれるだけで選ばれる銘柄が総入れ替えに
なる。実測（2026-09-04・M-1）では各条件が互いに 97% 重なっているのに 6条件の交差が **483件**
＝7% しか残らなかった（97% を5回掛ければ 86% のはずで桁が合わない）。stride=1 では
**32,438件＝97.4%** が残り、期待どおりになる。**間引きは条件間の突合と両立しない。**

## M-1 を測るときの注意

M-1 は `macro_nan_ok=False`（strict）で**母集団自体が M-2/M-6 と別物**なので、パネルを共有
できない（`MODEL_SPECS` のパネル種別で分ける）。CV の設定（min_train=6 / step=3 / embargo=12）
は M-2 と同値なので `run_one` をそのまま使えるが、**BIC 特徴量選択を挟む点だけが違う**。

出力は ASCII のみ（Windows cp932 リダイレクト対策・[[feedback_windows_cp932_stdout_symbols]]）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402
from model_stats import paired_ic_significance  # noqa: E402
from plugins import get_plugin  # noqa: E402
from plugins.macro_ensemble import _align  # noqa: E402
from plugins.macro_snapshots import build_snapshots, oof_backtest, preload_macro  # noqa: E402
from plugins.utils import coerce_params  # noqa: E402
from scripts._cache import cached, set_refresh  # noqa: E402
from scripts.candidate_bakeoff import (  # noqa: E402
    _load_financials,
    _load_prices,
    _thin,
    run_one,
)

_OUT_DIR = Path(__file__).resolve().parent / ".cache"

# 12-1 モメンタムの標準形（Jegadeesh-Titman 1993）。窓の最適化は**既定のゲートでは**対象外＝
# まず「入れるか否か」を clean に判定する（窓も同時に振ると検定数が5倍になり、
# 「窓を選んだこと」自体が過剰適合になりうる）。`--windows` はその過剰適合を
# **わざと再現して測る**ためのモードで、既定の判定には一切影響しない（#592）。
MOM_WINDOW = 12

CONDS: dict[str, bool] = {"off": False, "on": True}
MODELS = ["xgb_m2", "elasticnet"]
MODEL_LABELS = {"xgb_m2": "M-2(XGBoost)", "elasticnet": "M-6(ElasticNet)",
                "risk_return": "M-1(RiskReturn)"}
METRICS = (("rank_ic", "rank_ic_by_period"),
           ("short_side_spread", "short_side_spread_by_period"))
# 昇格ゲート: 2モデル × 2指標 = 4 検定を Bonferroni 補正。
N_TESTS = 4

# ── モデル → (config を持つプラグイン, run_one へ渡す推定器, パネル種別) ────────────
#
# **パネル種別が同じモデルだけがパネルを共有できる。** M-2/M-6 は `macro_enet.py:20` が
# 「`walk_forward_cv_monthly(min_train_months=6, step_months=3, embargo_months=12)` は M-2 と同値」
# と宣言しているので1枚を共有してよい。M-1 は `macro_nan_ok=False`（strict）で**母集団自体が
# 別物**なうえ `build_interactions=True`・BIC 特徴量選択が入るため、共有すると本番と違うものを
# 測る。推定器が "ols" なのは M-1 の CV が `fit_predict` を渡さない素の OLS だからで、
# `run_one` の MIN_TRAIN_MONTHS=6 / STEP_MONTHS=3 / embargo=LABEL_HORIZON_MONTHS は
# `macro_risk_return.py` の CV 呼び出しと完全に一致する（手続きを書き写していない）。
MODEL_SPECS: dict[str, tuple[str, str, str]] = {
    "xgb_m2":      ("macro_gbdt",        "xgb_m2",     "gbdt"),
    "elasticnet":  ("macro_gbdt",        "elasticnet", "gbdt"),
    "risk_return": ("macro_risk_return", "ols",        "m1"),
}
BASE_COND = "off"    # 比較の分母。窓モードでも「モメンタム無し」が基準


def build_conditions(windows: list[int] | None = None) -> dict[str, tuple[bool, int]]:
    """条件集合 {名前: (use_momentum, momentum_window)} を作る。

    `windows` 未指定なら **ADR-0045 の昇格ゲートと完全に同じ2条件**を返す（既定を変えない）。
    指定するとモメンタム無し＋各窓の多条件モードになる。窓は昇順に並べ、重複は落とす
    （同じ窓を2回測っても検定数だけが増えて alpha が不当に厳しくなる）。
    """
    if not windows:
        return {name: (use_mom, MOM_WINDOW) for name, use_mom in CONDS.items()}
    ws = sorted({int(w) for w in windows})
    if any(w < 1 for w in ws):
        raise ValueError(f"モメンタム窓は1以上の整数で指定してください: {ws}")
    return {BASE_COND: (False, MOM_WINDOW), **{f"mw{w}": (True, w) for w in ws}}


def bonferroni_alpha(n_models: int, n_conds: int) -> float:
    """検定数から補正後 alpha を導出する（**定数を書き写さない**）。

    検定数 = モデル数 × 指標数 × 基準以外の条件数。既定（2モデル・2条件）では
    2×2×1 = 4 となり `ALPHA` と一致する。窓を5本振れば 2×2×5 = 20 検定になり
    alpha は 1/5 に締まる——**窓を同時に振ると「窓を選んだこと」自体が過剰適合になる**
    という docstring 冒頭の懸念を、判定側でも数として現す。
    """
    n_tests = max(n_models, 1) * len(METRICS) * max(n_conds - 1, 1)
    return 0.05 / n_tests
ALPHA = 0.05 / N_TESTS


def _num(v, nd: int = 4) -> str:
    """None 安全な数値整形（欠測は '-'）。cp932 で落ちる記号は使わない。"""
    if v is None:
        return "-"
    return f"{v:+.{nd}f}" if isinstance(v, float) else str(v)


def _restrict_months(panel: tuple, yms: set) -> tuple:
    """パネル（samples / meta / ids / feats）を指定した月集合へ制限する。

    2条件で fold の位相を揃えるために使う（`walk_forward_cv_monthly` の test 月は月リストの
    先頭からの相対位置で決まるため、開始月が違うと 3ヶ月周期の別位相になる）。
    """
    s, m, i, feats = panel
    return ({ym: v for ym, v in s.items() if ym in yms},
            {ym: v for ym, v in m.items() if ym in yms},
            {ym: v for ym, v in i.items() if ym in yms},
            feats)


def _panel_stats(samples_by_ym: dict, ids_by_ym: dict, feats: list) -> dict:
    cos = {ec for ids in ids_by_ym.values() for ec in ids}
    return {
        "months": len(samples_by_ym),
        "samples": sum(len(v) for v in samples_by_ym.values()),
        "companies": len(cos),
        "n_features": len(feats),
        "first_ym": min(samples_by_ym) if samples_by_ym else None,
        "last_ym": max(samples_by_ym) if samples_by_ym else None,
    }


def _restrict(resid_by_ym: dict, oof_meta: dict, ids_by_ym: dict, keys: set) -> tuple:
    """residuals / meta を共通 (ym, ec) 集合へ制限して同順で組み直す。

    `build_snapshots(return_stock_ids=True)` と `walk_forward_cv_monthly(return_residuals=True)`
    は samples_by_ym[ym] のサンプル順を保存する（`_align` / `build_oof_meta` が依拠する既存
    契約）ため index で 1:1 突合できる。keys は `_align` が作った集合で NaN 行を含まないので、
    NaN は自動的に落ちる。
    """
    r2: dict[str, list] = {}
    m2: dict[str, list] = {}
    for ym, pairs in resid_by_ym.items():
        ids = ids_by_ym.get(ym, [])
        metas = oof_meta.get(ym, [])
        rr, mm = [], []
        for j, (yh, y) in enumerate(pairs):
            if j >= len(ids) or (ym, ids[j]) not in keys:
                continue
            rr.append((yh, y))
            mm.append(metas[j] if j < len(metas) else (ids[j], None))
        if rr:
            r2[ym] = rr
            m2[ym] = mm
    return r2, m2


def _row(o: dict) -> dict:
    q = o.get("quantile_returns") or []
    return {
        "rank_ic":           o["rank_ic"]["mean"],
        "rank_ic_std":       o["rank_ic"].get("std"),
        "rank_ic_neutral":   o.get("rank_ic_industry_neutral"),
        "short_side_spread": o.get("short_side_spread"),
        "short_side_hit":    o.get("short_side_hit_rate"),
        "bottom_q_return":   q[0] if q else None,
        "long_short_spread": o.get("long_short_spread"),
        "turnover":          o.get("effective_turnover"),
        "breakeven_bps":     o.get("breakeven_cost_bps"),
        "n_periods":         o.get("n_periods"),
    }


def _fmt_sig(sig: dict | None, alpha: float = ALPHA) -> str:
    """有意差の1行表示。**alpha は呼び出し側から渡す**（窓モードで検定数が変わるため）。"""
    if not sig:
        return "n/a (common test periods < 2)"
    p = sig.get("p_value")
    star = "SIG" if (p is not None and p < alpha) else "ns"
    return (f"diff={sig['mean']:+.4f} 95%CI[{sig['ci_lo']:+.4f},{sig['ci_hi']:+.4f}] "
            f"p={p if p is not None else float('nan'):.3f} n={sig['n_common']} "
            f"{star}(alpha={alpha:.5f})")


def macro_names_for(kind: str) -> list:
    """パネル種別が使うマクロ系列名（各プラグインの既定 config が唯一の源）。"""
    plugin_name = "macro_risk_return" if kind == "m1" else "macro_gbdt"
    params = coerce_params(get_plugin(plugin_name).params_schema(), {})
    return list(params["macro_features"]) if params["use_macro"] else []


def _build(kind: str, args, prices_by_co, fin_by_co, companies, macro_cache,
           use_momentum: bool, mom_window: int) -> tuple:
    """種別の本番 config のまま `use_momentum` / `momentum_window` だけ差し替えて構築する。

    **config は各プラグインの `params_schema()` から取り、ここへ書き写さない**（書き写すと
    本番が変わったときに黙って別物を測る）。種別ごとの差は本番コードの差そのもの:

      gbdt … `macro_nan_ok=True` / 交互作用なし（M-2・M-6 が共有）
      m1   … `macro_nan_ok=False`（strict）/ 交互作用あり / BIC 特徴量選択あり。
             `price_features` は渡さない（M-1 に px_* は無い・#446）

    M-1 の BIC 選択は**間引いた後のパネル**に対して行う。CV も同じパネルで回るので、
    「選んだ特徴量」と「評価に使う特徴量」が一致する（本番の順序と同じ）。
    """
    plugin_name = "macro_risk_return" if kind == "m1" else "macro_gbdt"
    params = coerce_params(get_plugin(plugin_name).params_schema(), {})
    macro_names = macro_names_for(kind)
    extra = {} if kind == "m1" else {
        "price_features": list(params.get("price_features") or [])}
    samples_by_ym, meta_by_ym, _current, feats, ids_by_ym = build_snapshots(
        prices_by_co, fin_by_co, companies, macro_cache,
        params["fin_features"], macro_names,
        use_momentum, mom_window, params["min_coverage"],
        build_interactions=(kind == "m1"),
        macro_nan_ok=(kind != "m1"),
        return_stock_ids=True,
        **extra,
    )
    s, m, i = _thin(samples_by_ym, meta_by_ym, ids_by_ym, args.stride)
    if kind == "m1":
        s, feats = _select_bic(s, feats, params["max_features"])
    return s, m, i, feats


def _select_bic(samples_by_ym: dict, feat_names: list, max_features: int) -> tuple:
    """M-1 の LassoLarsIC(BIC) 選択をかけ、選ばれた列だけのパネルへ絞る。

    選択そのものは `macro_risk_return._select_macro_features` を呼ぶ（`macro_snapshots.
    select_features_bic` への薄いラッパ）。**ここで BIC を書き直さない**——書き直すと
    本番の M-1 とは別のモデルを測ることになる（ADR-0041 の教訓）。

    サンプル順は保存する。`_restrict` / `_align` / `build_oof_meta` が
    `samples_by_ym[ym]` と `ids_by_ym[ym]` の index 1:1 対応に依拠しているため、
    ここで順序を崩すと共通 (ym,ec) 域の突合が静かに壊れる。
    """
    selected = get_plugin("macro_risk_return")._select_macro_features(
        samples_by_ym, feat_names, max_features=max_features)
    if not selected:
        raise SystemExit("BIC 選択で特徴量が1つも選ばれませんでした（パネルを確認）")
    idx = [feat_names.index(n) for n in selected]
    sel = {ym: [([row[i] for i in idx], tgt) for row, tgt in pairs]
           for ym, pairs in samples_by_ym.items()}
    return sel, selected


def main() -> None:
    # Windows cp932 では非ASCII記号でクラッシュするため UTF-8 に固定
    # （[[feedback_windows_cp932_stdout_symbols]]）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="モメンタム特徴量（use_momentum）の既定 ON/OFF 昇格ゲート実測")
    ap.add_argument("--models",
                    help=f"測るモデルをカンマ区切りで指定（既定: {','.join(MODELS)}／"
                         f"選べるのは {','.join(MODEL_SPECS)}）")
    ap.add_argument("--windows",
                    help="モメンタム窓をカンマ区切りで指定すると多条件モードになる"
                         "（例: 3,6,12,18,24）。既定は ON/OFF の2条件のまま")
    ap.add_argument("--smoke", action="store_true", help="サンプルを間引いた短時間確認")
    ap.add_argument("--stride", type=int, default=1, help="各月のサンプル間引き幅")
    ap.add_argument("--allow-full-pull", action="store_true",
                    help="週次株価キャッシュが無い場合に DB からのフルロードを許可する")
    ap.add_argument("--refresh-cache", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--json", dest="json_path", help="結果 JSON の出力先")
    args = ap.parse_args()
    if args.smoke and args.stride <= 1:
        args.stride = 5
    set_refresh(args.refresh_cache)

    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else list(MODELS))
    unknown = [m for m in models if m not in MODEL_SPECS]
    if unknown:
        raise SystemExit(
            f"未知のモデル: {', '.join(unknown)}（{', '.join(MODEL_SPECS)} のみ）")
    windows = ([int(w) for w in args.windows.replace(" ", "").split(",") if w]
               if args.windows else None)
    conds = build_conditions(windows)
    # **alpha は検定数から導出する**（定数 ALPHA を窓モードへ流用すると、条件を増やした
    # ぶんの多重比較が補正されないまま「有意」が出る）。
    alpha = bonferroni_alpha(len(models), len(conds))
    n_tests = len(models) * len(METRICS) * max(len(conds) - 1, 1)
    if n_tests != N_TESTS:
        print(f"[warn] 検定数が {n_tests} です（既定のゲートは {N_TESTS}）。"
              f"alpha は {alpha:.5f} へ導出し直しました。ADR-0045 の昇格判定と"
              f"直接は比較できません。", flush=True)
    out_path = Path(args.json_path) if args.json_path else _OUT_DIR / "momentum_gate.json"

    db = SessionLocal()
    try:
        prices_by_co = _load_prices(args.allow_full_pull)
        # モメンタムは過去履歴を読む。キャッシュが旧世代だと ON 側だけが不当に不利になり
        # 判定が壊れるため（#456 と同型）、起点・終端・規模をここで必ず現す。
        first = min((r.trade_date for rows in prices_by_co.values() for r in rows[:1]),
                    default=None)
        last = max((rows[-1].trade_date for rows in prices_by_co.values() if rows),
                   default=None)
        n_rows = sum(len(r) for r in prices_by_co.values())
        print(f"weekly px cache: cos={len(prices_by_co)} rows={n_rows} "
              f"range={first}..{last}", flush=True)

        fin_by_co, companies = _load_financials(db)
        print(f"panel src: fin_cos={len(fin_by_co)} companies={len(companies)}", flush=True)

        # マクロは**必要な種別の和集合**を1度だけ読む（M-1 の44系列は M-2 の53系列の
        # 部分集合だが、それに寄りかからず和集合を取る＝将来どちらかが増えても壊れない）。
        kinds = sorted({MODEL_SPECS[m][2] for m in models})
        macro_names = sorted({n for k in kinds for n in macro_names_for(k)})
        mkey = hashlib.md5(",".join(macro_names).encode()).hexdigest()[:10]
        macro_cache = (cached(f"bakeoff_macro_{mkey}",
                              lambda: preload_macro(db, prices_by_co, macro_names))
                       if macro_names else {})
        db.commit()   # 以降の CPU 計算中に読取トランザクションを残さない（#411）

        # ── 1. 各条件 × 各パネル種別を構築し、母集団の差をそのまま現す ────────────
        #
        # **パネルは (条件, 種別) で持つ。** M-2/M-6 は同じ種別なので1枚を共有し（従来どおり）、
        # M-1 は strict のため別の1枚になる。ここを共有すると M-1 を M-2 の母集団で測る。
        panels: dict[tuple, tuple] = {}
        stats: dict[str, dict] = {}
        for cond, (use_mom, mw) in conds.items():
            for kind in kinds:
                s, m, i, feats = _build(kind, args, prices_by_co, fin_by_co, companies,
                                        macro_cache, use_mom, mw)
                panels[(cond, kind)] = (s, m, i, feats)
                st = _panel_stats(s, i, feats)
                stats[f"{cond}|{kind}"] = st
                print(f"[{cond}/{kind}] mw={mw if use_mom else '-'} "
                      f"months={st['months']} ({st['first_ym']}..{st['last_ym']}) "
                      f"samples={st['samples']} companies={st['companies']} "
                      f"features={st['n_features']}", flush=True)

        # ── 2. 各条件 × 各モデルを走らせる（残差も受け取る）────────────────────
        results: dict[str, dict] = {}
        parts: dict[str, tuple] = {}
        for cond in conds:
            for model in models:
                estimator, kind = MODEL_SPECS[model][1], MODEL_SPECS[model][2]
                s, m, i, feats = panels[(cond, kind)]
                out = run_one(estimator, s, m, i, feats, pca=0, return_parts=True)
                parts[f"{cond}|{model}"] = out.pop("_parts")
                if out.get("error"):
                    print(f"  {model}: ERROR {out['error']}", flush=True)
                results[f"{cond}|{model}"] = out
                o = out["oof"]
                print(f"  [{cond}] {MODEL_LABELS[model]:<18} "
                      f"rank-IC={_num(o['rank_ic']['mean'])} "
                      f"(std={_num(o['rank_ic'].get('std'))}) "
                      f"short_side={_num(o.get('short_side_spread'))} "
                      f"folds={out['n_folds']} ({out['elapsed_sec']}s)", flush=True)
    finally:
        db.close()

    # ── 3. 共通月で走らせ直し、さらに共通 (ym,ec) 域へ制限する（主判定）────────
    #
    # **月を揃えないと fold の位相がずれて共通域が空になる**（初回実測で実際に踏んだ）。
    # `walk_forward_cv_monthly` は月リストの先頭から min_train_months+embargo_months を空けて
    # step_months=3 刻みで test 月を選ぶため、パネルの開始月が1ヶ月でも違うと test 月が
    # 3ヶ月周期の別位相になり **一度も一致しない**（実測: off=2019-12 起点/69ヶ月 と
    # on=2020-07 起点/62ヶ月 で共通 (ym,ec) が 0 件）。よって共通月へ制限したパネルで
    # 走らせ直す。ここまでが「fold を揃える」段で、そのあと同一 fold の中で銘柄集合を
    # 揃えるのが (ym,ec) 制限の段になる。
    # 共通月は**パネル種別ごと**に取る。M-1（strict）と M-2 の月を交差させると、
    # どちらの比較にも要らない月まで落ちて両方が不当に狭くなる（比較したいのは
    # 「同じモデルの条件間」であって「モデル間」ではない）。
    common_yms = {kind: set.intersection(*[set(panels[(c, kind)][0]) for c in conds])
                  for kind in kinds}
    for kind in kinds:
        ys = common_yms[kind]
        print(f"\n=== common months [{kind}]: {len(ys)} "
              f"({min(ys, default='-')}..{max(ys, default='-')}) ===", flush=True)
    cpanels = {(cond, kind): _restrict_months(panels[(cond, kind)], common_yms[kind])
               for cond in conds for kind in kinds}
    cparts: dict[str, tuple] = {}
    cruns: dict[str, dict] = {}
    for cond in conds:
        for kind in kinds:
            cs, _cm, ci, cfeats = cpanels[(cond, kind)]
            st = _panel_stats(cs, ci, cfeats)
            print(f"[{cond}/{kind}/common-months] months={st['months']} "
                  f"samples={st['samples']} companies={st['companies']} "
                  f"features={st['n_features']}", flush=True)
        for model in models:
            estimator, kind = MODEL_SPECS[model][1], MODEL_SPECS[model][2]
            s, m, i, feats = cpanels[(cond, kind)]
            out = run_one(estimator, s, m, i, feats, pca=0, return_parts=True)
            cparts[f"{cond}|{model}"] = out.pop("_parts")
            cruns[f"{cond}|{model}"] = out
            if out.get("error"):
                print(f"  {model}: ERROR {out['error']}", flush=True)

    print("\n=== common (ym,ec) restriction ===", flush=True)
    common_results: dict[str, dict] = {}
    common_info: dict[str, dict] = {}
    for model in models:
        kind = MODEL_SPECS[model][2]
        aligned = {cond: _align(cparts[f"{cond}|{model}"][0], cpanels[(cond, kind)][2])
                   for cond in conds}
        # **全条件の交差**を取る。2条件のときは従来と同じ off ∩ on。
        keys = set.intersection(*[set(aligned[c]) for c in conds])
        folds = {c: cruns[f"{c}|{model}"]["n_folds"] for c in conds}
        common_info[model] = {
            "n_common": len(keys),
            "n_by_cond": {c: len(aligned[c]) for c in conds},
            "n_folds": folds,
        }
        per_cond = " ".join(f"{c}={len(aligned[c])}" for c in conds)
        print(f"  {MODEL_LABELS[model]:<18} common={len(keys)} "
              f"({per_cond}) folds={folds}", flush=True)
        if len(set(folds.values())) > 1:
            print("    [warn] fold 数が一致していません（位相が揃っていない可能性）", flush=True)
        for cond in conds:
            resid, meta = cparts[f"{cond}|{model}"]
            r2, m2 = _restrict(resid, meta, cpanels[(cond, kind)][2], keys)
            bt = oof_backtest(r2, n_quantiles=5, meta_by_ym=m2, rebalance_per_year=4)
            common_results[f"{cond}|{model}"] = bt
            print(f"    [{cond}] rank-IC={_num(bt['rank_ic']['mean'])} "
                  f"(std={_num(bt['rank_ic'].get('std'))}) "
                  f"short_side={_num(bt.get('short_side_spread'))} "
                  f"periods={bt.get('n_periods')}", flush=True)

    # ── 4. 昇格ゲート判定 ──────────────────────────────────────────────────
    # 検定は common スコープだけで行う。**raw では差を検定できない**——条件ごとに
    # パネルの開始月が違うと `walk_forward_cv_monthly` の test 月が3ヶ月周期の別位相になり、
    # `paired_ic_significance` がペアリングできる共通 test 期が 0 になる（実測で 4検定すべて
    # "common test periods < 2"）。raw は各条件の**水準**としてだけ意味を持つので併記する。
    sigs: dict[str, dict] = {}
    passed: list[str] = []
    regressed: list[str] = []
    test_conds = [c for c in conds if c != BASE_COND]
    print(f"\n=== cond - {BASE_COND} / common [PRIMARY] "
          f"(Bonferroni alpha={alpha:.5f}, {n_tests} tests) ===", flush=True)
    for model in models:
        b = common_results[f"{BASE_COND}|{model}"]
        for cond in test_conds:
            a = common_results[f"{cond}|{model}"]
            for metric, key in METRICS:
                sig = paired_ic_significance(a.get(key) or {}, b.get(key) or {})
                sigs[f"common|{cond}|{model}|{metric}"] = sig
                p = sig.get("p_value") if sig else None
                hit = bool(sig and p is not None and p < alpha)
                label = f"{MODEL_LABELS[model]}/{cond}/{metric}"
                if hit and sig["mean"] > 0:
                    passed.append(label)
                elif hit:
                    regressed.append(label)
                print(f"  {MODEL_LABELS[model]:<18} {cond:<6} {metric:<18} "
                      f"{_fmt_sig(sig, alpha)}", flush=True)

    print("\n=== raw levels (each condition's own population; NOT testable across "
          "conditions: fold phases differ) ===", flush=True)
    print(f"  {'cond':<6} {'model':<18} {'rank-IC':>9} {'IC std':>9} {'short':>9} "
          f"{'LS spread':>10} {'folds':>6} {'samples':>9}", flush=True)
    for cond in conds:
        for model in models:
            r = results[f"{cond}|{model}"]
            o = r["oof"]
            st = stats[f"{cond}|{MODEL_SPECS[model][2]}"]
            print(f"  {cond:<6} {MODEL_LABELS[model]:<18} {_num(o['rank_ic']['mean']):>9} "
                  f"{_num(o['rank_ic'].get('std')):>9} "
                  f"{_num(o.get('short_side_spread')):>9} "
                  f"{_num(o.get('long_short_spread')):>10} {r['n_folds']:>6} "
                  f"{st['samples']:>9}", flush=True)

    if len(conds) > 2:
        # 窓モードは「どの窓を既定にするか」を決める場ではない（それを共通域抜きでやって
        # いるのが #592 の指摘そのもの）。ここで出すのは**母集団を揃えても差が残るか**だけ。
        verdict = (("WINDOW SCAN: effects that survive the common-domain restriction: "
                    + ", ".join(passed)) if passed else
                   "WINDOW SCAN: no window beat the no-momentum baseline on the common "
                   "(ym,ec) domain at the corrected alpha")
        if regressed:
            verdict += " | significantly WORSE: " + ", ".join(regressed)
    elif passed:
        verdict = "PROMOTE (default use_momentum=True): " + ", ".join(passed)
    elif regressed:
        verdict = ("REJECT (keep default use_momentum=False): no improvement passed "
                   "corrected alpha; significantly WORSE on " + ", ".join(regressed))
    else:
        verdict = "REJECT (keep default use_momentum=False): no metric passed corrected alpha"
    print(f"\n=== verdict === {verdict}", flush=True)
    print("判定は common スコープで読む（同一 fold・同一 (ym,ec) 域）。raw は水準のみ。",
          flush=True)

    payload = {
        "momentum_window": MOM_WINDOW,
        "conditions": {c: {"use_momentum": u, "window": w} for c, (u, w) in conds.items()},
        "windows": windows,
        "alpha": alpha,
        "n_tests": n_tests,
        "models": models,
        "stride": args.stride,
        "panel": stats,
        "common_months": {kind: {"n": len(ys),
                                 "first": min(ys, default=None),
                                 "last": max(ys, default=None)}
                          for kind, ys in common_yms.items()},
        "common_info": common_info,
        "raw": {k: _row(v["oof"]) for k, v in results.items()},
        "common": {k: _row(v) for k, v in common_results.items()},
        "n_folds": {k: v["n_folds"] for k, v in results.items()},
        "n_folds_common_months": {k: v["n_folds"] for k, v in cruns.items()},
        "significance": sigs,
        "passed": passed,
        "regressed": regressed,
        "verdict": verdict,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
