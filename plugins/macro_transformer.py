"""
M-7 時系列 Transformer（比較用のモデル・#1316 / #1317・ADR-0079）

各社の**直近 52 週の値動きと出来高の並び**を Transformer（注意機構）で読み、M-2/M-6 と同じ財務の列と
合わせて 52 週先の対数リターンを当てる。M-2/M-6 は「1 社×1 月末」の断面を 1 行として見るだけで、
過去の値動きの並びは読んでいない（銘柄ごとの時系列は M-3 だけ）。

**比較用のモデル**。朝の買い候補（`routers.analysis.PRODUCTION_MODELS`・morning.js の `CAND_MODELS`）にも
売りの判定にも使わない。昇格は事前登録（`docs/prereg/m7-transformer.md`）の判定の後に別 Issue で決める。
見立ては「差なし」が最有力（株価由来の特徴は共通域で効かず＝ADR-0045、木の M-2 も線形の M-6 を超えない）。

設計:
  - **同じパネル・同じ fold**: `build_snapshots` の引数は M-6 のスキーマ既定から読む（`_m6_panel_args`・
    書き写さない）。母集団と `shared_snapshot_cache` の構築物が M-6 と同じになり、`model_comparison` で
    同じ行・同じ期で並ぶ。モデルに渡すのは先頭の財務列だけ（マクロは月内で全社同じ値＝順位を動かさない）。
    walk-forward は M-2/M-6 と同値（最低学習 6 か月・3 か月刻み・embargo 12 か月）
  - **系列**: `plugins/weekly_sequences.py`。出来高は `load_weekly_prices_chunked(with_volume=True)` で別に読み、
    パネル用の読み込みと日付列が一致することを確かめる。上場 52 週未満の社も行を落とさない
  - **学習**: `plugins/seq_transformer.py`（JAX）。fold の例外は CV の後で送出する（CV は握りつぶす）
  - **種**: `tuning.seeded(_RANDOM_STATE)`。探索空間は持たない（`tuning_search_space` 無し）
  - **jax**: データを読む前に読めるかを確かめる（Smart App Control の遮断なら、重い読み込みの前に止める）。
    モジュールの import 時には読まない（Render の自動検出）
  - **規模の中立化はしない**（M-6 の `_SIZE_NEUTRAL` は M-6 で測って採ったもの。M-7 は未測定のまま足さない）
  - **producer**: `macro_transformer_scores` へ全置換。読むのは分析画面の保存済み一覧だけ
    （`prediction_snapshots` には写さない・暦が無く古い μ̂ が毎月写されるため）
"""
import logging
import os
import statistics
from typing import Any

import numpy as np

from . import progress
from .base import AnalysisPlugin
from .macro_snapshots import (
    FIN_BASE_OPTIONS,
    LABEL_HORIZON_MONTHS,
    M2_M6_DEFAULT_FIN_FEATURES,
    _realized_vol,
    apply_fin_fill,
    build_oof_meta,
    build_snapshots,
    conformal_bucket_halfwidths,
    conformal_halfwidth_for,
    get_producer_scores,
    load_data,
    load_weekly_prices_chunked,
    oof_backtest,
    preload_macro,
    to_date_str,
    tradable_results,
    tradable_snapshot_asof,
)
from .macro_risk_return import MacroRiskReturnPlugin as _M1
from .utils import walk_forward_cv_monthly

logger = logging.getLogger(__name__)

_RANDOM_STATE = 42
_MIN_TRAIN_MONTHS = 6
_STEP_MONTHS = 3
_MIN_SAMPLES = 20


def _m6_panel_args() -> dict:
    """パネルの引数を M-6 のスキーマ既定から読む（同じ母集団・同じ構築物を共有する。書き写さない）。"""
    from .macro_enet import plugin as m6

    s = m6.params_schema()
    use_macro = bool(s["use_macro"]["default"])
    return {
        "macro_names":     list(s["macro_features"]["default"]) if use_macro else [],
        "use_momentum":    bool(s["use_momentum"]["default"]),
        "momentum_window": int(s["momentum_window"]["default"]),
        "min_coverage":    float(s["min_coverage"]["default"]),
        "price_features":  list(s["price_features"]["default"]),
    }


def _expected_folds(samples_by_ym: dict) -> int:
    """`walk_forward_cv_monthly` が回すはずの fold 数（落ちた fold を数で検出する）。"""
    yms = sorted(samples_by_ym)
    n = 0
    for i in range(_MIN_TRAIN_MONTHS + LABEL_HORIZON_MONTHS, len(yms), _STEP_MONTHS):
        n_train = sum(len(samples_by_ym[ym]) for ym in yms[:max(0, i - LABEL_HORIZON_MONTHS)])
        if n_train >= 5 and samples_by_ym.get(yms[i]):
            n += 1
    return n


def _jax_env() -> dict:
    """計算環境の印（別プロセスで予測が一致しないときの切り分け用・bench_macro_beta と同型）。"""
    from .seq_transformer import require_jax

    jax, _ = require_jax()
    try:
        devices = [str(d) for d in jax.devices()]
    except Exception as e:   # noqa: BLE001 — 印なので、取れなければ理由を残す
        devices = [f"error: {type(e).__name__}"]
    return {"jax": getattr(jax, "__version__", None), "devices": devices,
            "xla_flags": os.environ.get("XLA_FLAGS")}


class MacroTransformerPlugin(AnalysisPlugin):
    name = "macro_transformer"
    label = "時系列 Transformer（M-7）"
    description = (
        "各社の直近52週の値動きと出来高の並びを Transformer（注意機構）で読み、M-2/M-6 と同じ財務の列と"
        "合わせて約1年先のリターンを予想します。M-2/M-6 と同じデータ・同じ期で答え合わせ（OOF）します。"
        "比較用のモデルで、朝の買い候補・売りの判定には使いません。"
        "【注意】学習に jax を使い、所要は数十分〜数時間です（見積り・未実測）。"
        "M-6 に勝つかを事前登録した物差しで測るまでは、画面から回さないでください。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    writes = ("macro_transformer_scores",)
    writes_impact = ("分析画面の M-7 タブの保存済み μ̂（期待リターン）だけが置き換わります。"
                     "朝の推奨・売り候補・おすすめ銘柄では使っていません。次に回すまでこの値のままです")
    category = "比較用のモデル"
    ui_order = 550                       # M-4・M-5（退役）の 530・540 の後

    def params_schema(self) -> dict:
        return {
            "fin_features": {
                "type": "multiselect",
                "label": "財務ベース特徴量",
                "description": "M-2/M-6 と同じ既定。系列の枝とは別の枝から入ります。",
                "options": FIN_BASE_OPTIONS,
                "default": list(M2_M6_DEFAULT_FIN_FEATURES),
            },
            "use_sequence": {
                "type": "checkbox",
                "label": "値動きの系列を使う",
                "description": ("OFF にすると系列の枝を外し、財務の列だけで同じ手順で学習します。"
                                "時系列でどれだけ上乗せできたかを測る対照です。"),
                "default": True,
            },
            "d_model": {
                "type": "slider",
                "dtype": "int",
                "label": "埋め込みの次元",
                "description": "4 の倍数（head 数 4 で割り切れること）。",
                "default": 32,
                "min": 16,
                "max": 64,
                "step": 16,
            },
            "n_layers": {
                "type": "slider",
                "dtype": "int",
                "label": "encoder の層の数",
                "default": 2,
                "min": 1,
                "max": 4,
                "step": 1,
            },
            "dropout": {
                "type": "slider",
                "dtype": "float",
                "label": "dropout",
                "default": 0.1,
                "min": 0.0,
                "max": 0.5,
                "step": 0.05,
            },
            "learning_rate": {
                "type": "number",
                "dtype": "float",
                "label": "学習率（AdamW）",
                "default": 0.001,
                "min": 0.0001,
                "max": 0.01,
            },
            "max_epochs": {
                "type": "slider",
                "dtype": "int",
                "label": "学習回数の上限（epoch）",
                "description": "検証損失が下がらなくなったら手前で止めます（early stopping）。",
                "default": 30,
                "min": 5,
                "max": 100,
                "step": 5,
            },
            "patience": {
                "type": "slider",
                "dtype": "int",
                "label": "early stopping の待ち回数",
                "default": 5,
                "min": 2,
                "max": 20,
                "step": 1,
            },
            # ── ここから下は表示だけのパラメータ（サーバーは受け取って返すだけ・M-6 と同じ）──
            "lambda_risk": {
                "type": "slider",
                "dtype": "float",
                "label": "リスク回避度 λ",
                "description": "U = μ − λ × R で表を並べ替えます。既定 0＝μ̂ の順（OOF で検証する並び）。",
                "default": 0.0,
                "min": 0.0,
                "max": 5.0,
                "step": 0.1,
            },
            "risk_axis": {
                "type": "select",
                "label": "横軸リスク",
                "description": "R2=実現ボラ（既定）/ R_macro=マクロ起因リスク（macro_beta の蓄積が必要）。",
                "options": [
                    {"value": "r2",      "label": "R2 実現ボラティリティ（既定）"},
                    {"value": "r_macro", "label": "R_macro マクロ起因リスク（β推論要）"},
                ],
                "default": "r2",
            },
            "r3_gate": {
                "type": "slider",
                "dtype": "float",
                "label": "R3 信頼度ゲート（足切り）",
                "description": "CV-RMSE がこの値を超える銘柄を表と図から除外（0=ゲートなし）。",
                "default": 0.0,
                "min": 0.0,
                "max": 0.5,
                "step": 0.01,
            },
            "top_n": {
                "type": "number",
                "dtype": "int",
                "label": "上位表示件数",
                "default": 30,
                "min": 5,
                "max": 200,
            },
        }

    # ── producer（M-6 と同一契約）────────────────────────────────────────
    def produced_output(self, db: Any) -> bool:
        try:
            from database import get_macro_transformer_scores
            return bool(get_macro_transformer_scores(db))
        except Exception:
            return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        """M-1/M-2/M-6 と同一形 {edinet_code: {mu, r_macro, r1_prime}}。"""
        from database import get_macro_transformer_producer
        prods = get_macro_transformer_producer(db)
        if not prods:
            return {}
        r_macro_src = get_producer_scores(db, macro_snapshot)
        out: dict = {}
        for ec, rec in prods.items():
            prod = r_macro_src.get(ec) or {}
            out[ec] = {"mu": float(rec["mu"]), "r_macro": prod.get("r_macro"),
                       "r1_prime": rec.get("r1_prime")}
        return out

    @staticmethod
    def _config(params: dict, n_static: int):
        from .seq_transformer import N_HEADS, TFConfig

        if params["d_model"] % N_HEADS:
            raise ValueError(f"埋め込みの次元 {params['d_model']} は head 数 {N_HEADS} の倍数にしてください。")
        return TFConfig(
            n_static=n_static, use_sequence=bool(params["use_sequence"]),
            d_model=int(params["d_model"]), n_layers=int(params["n_layers"]),
            dropout=float(params["dropout"]), learning_rate=float(params["learning_rate"]),
            max_epochs=int(params["max_epochs"]), patience=int(params["patience"]),
        )

    def execute(self, params: dict, db: Any) -> dict:
        from . import seq_transformer as st
        from . import weekly_sequences as ws
        from .tuning import seeded

        fin_features = list(params["fin_features"])
        if not fin_features:
            raise ValueError("財務特徴量を1つ以上選択してください。")
        n_fin = len(fin_features)
        cfg = self._config(params, n_fin)
        top_n = params["top_n"]

        # jax を読めなければ、重い読み込みの前に止める（Smart App Control の遮断を含む）
        env = _jax_env()

        # ── データロード・スナップショット構築（M-6 と同じ引数＝同じ母集団・同じ構築物）────
        panel = _m6_panel_args()
        prices_by_co, fin_by_co, companies = load_data(
            db, with_volume="px_volz" in panel["price_features"])
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に収集を実行してください。")
        fin_by_co, fin_filled = apply_fin_fill(db, fin_by_co)
        macro_names = panel["macro_names"]
        macro_cache = preload_macro(db, prices_by_co, macro_names) if macro_names else {}
        samples_by_ym, sample_meta_by_ym, current_snaps, all_feat_names, stock_ids_by_ym = build_snapshots(
            prices_by_co, fin_by_co, companies, macro_cache,
            fin_features, macro_names, panel["use_momentum"], panel["momentum_window"],
            panel["min_coverage"],
            build_interactions=False,
            macro_nan_ok=True,
            price_features=panel["price_features"],
            return_stock_ids=True,
        )
        if list(all_feat_names[:n_fin]) != fin_features:
            raise RuntimeError(f"パネルの先頭の列が財務列と違う: {all_feat_names[:n_fin]} != {fin_features}")
        total_samples = sum(len(v) for v in samples_by_ym.values())
        if total_samples < _MIN_SAMPLES:
            raise ValueError(
                f"学習サンプルが不足（{total_samples}件）。データを収集してから再実行してください。")

        # ── 系列（行の並び＝ym の昇順・各月は build_snapshots の並び）────────────────
        yms = sorted(samples_by_ym)
        codes_cur = list(current_snaps.keys())
        flat_codes = [ec for ym in yms for ec in stock_ids_by_ym[ym]]
        ym_codes = np.fromiter((st.ym_code(ym) for ym in yms for _ in samples_by_ym[ym]),
                               dtype=np.int64, count=total_samples)
        seq_info: dict = {"used": cfg.use_sequence}
        patches = pmask = cur_patches = cur_pmask = None
        if cfg.use_sequence:
            progress.emit("値動きの系列を組み立て")
            seq_rows = load_weekly_prices_chunked(db, with_volume=True)
            ws.check_same_dates(prices_by_co, seq_rows, set(flat_codes) | set(codes_cur))
            store = ws.build_weekly_store(seq_rows)
            snaps = ws.snap_dates_by_ym(prices_by_co, stock_ids_by_ym)
            flat_dates = [d for ym in yms for d in snaps[ym]]
            patches, pmask = ws.to_patches(ws.gather_windows(store, flat_codes, flat_dates))
            cur_dates = [current_snaps[c][1]["snap_date"] for c in codes_cur]
            cur_patches, cur_pmask = ws.to_patches(ws.gather_windows(store, codes_cur, cur_dates))
            seq_info.update({
                "n_weeks": store.n_weeks,
                "n_masked_weeks": store.n_masked_weeks,
                "rows_without_observation": int((~pmask.any(axis=1)).sum()),
                "mean_observed_patches": round(float(pmask.sum(axis=1).mean()), 3),
            })
        bank = st.SampleBank(ym_codes=ym_codes, patches=patches, pmask=pmask)
        cv_samples: dict = {}
        k = 0
        for ym in yms:
            rows = []
            for feat_row, y in samples_by_ym[ym]:
                rows.append((feat_row[:n_fin], y, k))
                k += 1
            cv_samples[ym] = rows

        # ── walk-forward CV（M-2/M-6 と同じ fold）─────────────────────────────
        seed = seeded(_RANDOM_STATE)
        errors: list = []
        diag: dict = {}
        fit_predict = st.make_fit_predict(cfg, bank, seed, errors, diag)
        cv_folds, cv_residuals = walk_forward_cv_monthly(
            cv_samples, fin_features,
            min_train_months=_MIN_TRAIN_MONTHS, step_months=_STEP_MONTHS,
            return_residuals=True,
            fit_predict=fit_predict,
            embargo_months=LABEL_HORIZON_MONTHS,   # 52週先ラベルの窓重複を purge（ADR-0014）
            pass_train_groups=True,
        )
        st.raise_fold_errors(errors)
        expected = _expected_folds(cv_samples)
        if len(cv_folds) != expected:
            raise RuntimeError(f"交差検証の fold が {len(cv_folds)} 個（{expected} 個のはず）。"
                               "黙って縮んだ OOF では比べない")

        oof_meta = build_oof_meta(stock_ids_by_ym, sample_meta_by_ym, cv_residuals.keys())
        oof_bt = oof_backtest(cv_residuals, n_quantiles=5, meta_by_ym=oof_meta, rebalance_per_year=4)
        epochs = list(diag.get("epochs", []))
        cv_diag = {
            "n_folds":       len(cv_folds),
            "epochs":        epochs,
            "median_epochs": st.median_epochs(epochs),
            "n_es_used":     int(sum(1 for v in diag.get("es_used", []) if v)),
            "seed":          seed,
        }

        def _cv_summary(folds):
            if not folds:
                return {"folds": [], "mean_r2": None, "mean_rmse": None, "n_folds": 0}
            return {
                "folds":     folds,
                "mean_r2":   round(statistics.mean(f["r2"] for f in folds), 4),
                "mean_rmse": round(statistics.mean(f["rmse"] for f in folds), 4),
                "n_folds":   len(folds),
            }

        from database import is_tuning_objective_only
        if is_tuning_objective_only():
            return {
                "cv_metrics":        {"transformer": _cv_summary(cv_folds)},
                "selected_features": fin_features,
                "cv_diagnostics":    cv_diag,
                "sequence":          seq_info,
                "env":               env,
                "n_train_samples":   total_samples,
                "n_companies":       0,
                "lambda_risk":       params["lambda_risk"],
                "risk_axis":         params["risk_axis"],
                "r3_gate":           params["r3_gate"],
                "top_n":             top_n,
                "results":           [],
                "model_type":        "transformer",
                "oof_backtest":      oof_bt,
                "r_macro_available": False,
            }

        # ── 最終モデル（全学習行・学習回数は fold の中央値）→ 現断面を採点 ──────────
        progress.emit("最終モデルを学習")
        all_rows = [s for ym in yms for s in cv_samples[ym]]
        groups = [len(cv_samples[ym]) for ym in yms]
        final = st.fit_model(cfg, [s[0] for s in all_rows], [s[1] for s in all_rows], groups,
                             bank.patches, bank.pmask, st.fold_seed(seed, 0),
                             epochs=cv_diag["median_epochs"])
        current_rows = [current_snaps[c][0][:n_fin] for c in codes_cur]
        mu_preds = (final.predict(current_rows, cur_patches, cur_pmask).tolist()
                    if codes_cur else [])

        # ── リスク軸（M-6 と同一ヘルパーを共有）────────────────────────────
        m1_inst = _M1()
        r3_data = m1_inst._compute_r3_buckets(cv_residuals, sample_meta_by_ym)
        conformal_data = conformal_bucket_halfwidths(cv_residuals, sample_meta_by_ym)
        try:
            macro_beta_producer = get_producer_scores(db)
        except Exception:
            macro_beta_producer = {}

        raw_items: list[dict] = []
        for j, edinet_code in enumerate(codes_cur):
            _, info = current_snaps[edinet_code]
            r2 = _realized_vol(prices_by_co.get(edinet_code, []), info["snap_date"], weeks=52)
            r3 = m1_inst._r3_for(info.get("industry"), info.get("size"), r3_data)
            r1p = conformal_halfwidth_for(info.get("industry"), info.get("size"), conformal_data)
            prod = macro_beta_producer.get(edinet_code)
            raw_items.append({
                "edinet_code":  edinet_code,
                "sec_code":     info["sec_code"],
                "company_name": info["company_name"],
                "industry":     info["industry"],
                "snap_date":    to_date_str(info.get("snap_date")),
                "mu_raw":       round(float(mu_preds[j]), 6),
                "r1":           round(r1p, 6) if r1p is not None else None,
                "r2":           round(r2, 6) if r2 is not None else None,
                "r3":           round(r3, 6) if r3 is not None else None,
                "r_macro":      (round(float(prod["r_macro"]), 6)
                                 if (prod and prod.get("r_macro") is not None) else None),
            })
        raw_items.sort(key=lambda x: x.get("mu_raw") or -1e18, reverse=True)
        r_macro_available = any(it["r_macro"] is not None for it in raw_items)

        _asof = tradable_snapshot_asof(
            db, ((c, current_snaps[c][1].get("snap_date")) for c in codes_cur))
        with progress.persisting():     # 画面の取消はここまで（#849）
            self._persist_producer(db, raw_items, _asof)

        shown, untradable = tradable_results(db, raw_items)
        # 相対 μ̂（M-6 と同じ）: 今買える社の μ̂ 中央値との差。水準は検証の外で、順位だけが OOF で検証される
        mu_rel_center = (round(float(statistics.median(it["mu_raw"] for it in shown)), 6)
                         if shown else None)
        for it in shown:
            it["mu_rel"] = round(it["mu_raw"] - mu_rel_center, 6)

        return {
            "asof":              _asof,
            "cv_metrics":        {"transformer": _cv_summary(cv_folds)},
            "selected_features": fin_features,
            "cv_diagnostics":    cv_diag,
            "final_model":       {"epochs": final.epochs, "d_model": cfg.d_model,
                                  "n_layers": cfg.n_layers, "n_heads": cfg.n_heads,
                                  "use_sequence": cfg.use_sequence},
            "sequence":          seq_info,
            "env":               env,
            "n_train_samples":   total_samples,
            "fin_filled_rows":   fin_filled,
            "n_companies":       len(raw_items),
            "lambda_risk":       params["lambda_risk"],
            "risk_axis":         params["risk_axis"],
            "r3_gate":           params["r3_gate"],
            "top_n":             top_n,
            "results":           shown,
            "untradable_excluded": untradable,
            "mu_rel_center":     mu_rel_center,
            "model_type":        "transformer",
            "oof_backtest":      oof_bt,
            "r_macro_available": r_macro_available,
        }

    @staticmethod
    def _persist_producer(db: Any, raw_items: list, asof: dict) -> None:
        """producer μ̂ を macro_transformer_scores へ全置換（M-6 の _persist_producer と同型）。

        失敗（読取専用 DB 等）は分析表示を妨げない＝握りつぶす（次に回したときに作り直される）。
        探索・比較の中は replace 側が `tuning_dry_run()` で no-op。"""
        from database import replace_macro_transformer_scores
        try:
            replace_macro_transformer_scores(
                db,
                [{"edinet_code": it["edinet_code"], "mu": it["mu_raw"],
                  "r1_prime": it.get("r1")} for it in raw_items],
                asof.get("snapshot_date"),
                snapshot_date_min=asof.get("snapshot_date_min"),
                n_stale=asof.get("n_stale"),
            )
        except Exception:
            logger.exception("M-7 の μ̂ を保存できなかった")


plugin = MacroTransformerPlugin()
