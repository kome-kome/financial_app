"""
M-4 兄弟μ̂スタッキング・アンサンブル推奨プラグイン（ADR-0015 / Issue #367）

M-1（macro_risk_return, 線形 OLS+BIC）・M-2（macro_gbdt, 非線形 XGBoost）・
M-6（macro_enet, 正則化線形 ElasticNet・#397 で追加）のアウト・オブ・サンプル（OOF）
予測 μ̂ を、リーク回避の二段ウォークフォワードで統合するメタモデル。誤差が低相関な
基底を制約付き加重で相殺でき単体を超えうる（Wolpert 1992 Stacked Generalization /
Breiman 1996 Stacked Regressions）。重みは非負・和1の NNLS で学習するため、効かない
基底は自動的に重み ~0 へ落ちる＝基底追加の下振れリスクが構造的に小さい。

背景（#375）: purge/embargo 導入後の honest 評価で M-2 rank-IC は 0.33→0.14（リーク由来の
上方バイアスを是正）、M-1 と肩を並べた。単体独走の前提が崩れ、多様性統合の価値が上がった。

設計決定（ADR-0015 / #397）:
  - **重みは既定 `rank_ic_grid`（rank-IC 最大化）**。二段目の学習目的を評価指標へ揃える（#397 の
    本番実測で、MSE 最小化の NNLS は縮小推定で予測分散の小さい M-6 を重み 0 で捨て、OOF の
    改善が現在μ̂＝producer へ反映されないという非整合が出た。OOF 性能自体は両者誤差レベル）。
  - **基底は M-1+M-2+M-6**。M-3（macro_dlm）は週次専用（ADR-0012）で目的頻度（52週 vs 週次）・
    母集団が異なり (ym,銘柄) 整列が非自明のため除外（論証された非対称・放置ではない）。
    M-5（macro_gbdt_rank）は順位スコアで水準を持たないため除外（ADR-0017）。
  - M-6 は M-2 と同一の build 契約（交差項なし・macro_nan_ok）ゆえスナップショットを共有し、
    CV だけ ElasticNet の fit_predict へ差し替える（`_same_build_config` が同値性を判定）。
  - 基底が増えると共通 (ym,ec) 域は狭まりうるため、優劣判定は必ず `base_oof_backtest`
    （同一共通域に制限した各基底の OOF）と比較する（ADR-0015 の base-on-common）。
  - M-1/M-2 の execute() は集約 oof_backtest と現在μ̂しか返さず per-(ym,銘柄) OOF を露出しない
    ため、M-4 は build_snapshots(return_stock_ids=True)+walk_forward_cv_monthly(return_residuals=True)
    を両モデル自前で回す（既存シンボルを再利用・新規学習ロジックを書かない）。
  - honest 評価: base μ̂ は embargo=LABEL_HORIZON_MONTHS(=12) の purged OOF。二段目の重みは
    月 t の予測に t 未満の共通 OOF だけを使い（expanding）リークしない。
  - execute は同期 def（#357）。heavy=True（M-1+M-2 を回すためローカル専用・Render skip）。
"""
import math
import statistics
from typing import Any

import numpy as np
from scipy.optimize import nnls

from .base import AnalysisPlugin
from .utils import coerce_params, winsorize, walk_forward_cv_monthly
from .macro_snapshots import (
    LABEL_HORIZON_MONTHS,
    load_data,
    preload_macro,
    build_snapshots,
    oof_backtest,
    get_producer_scores,
    _macro_from_cache,
    _spearman,
)
from .macro_gbdt import _make_xgb_fit_predict

# 統合対象の基底モデル（順序 = 重みベクトルの列順）。M-3 は除外（上記設計決定）、
# M-5 は順位スコアで水準を持たないため除外（ADR-0017）。M-6 は #397 で追加。
BASE_MODELS = ["macro_risk_return", "macro_gbdt", "macro_enet"]
_MIN_META_PAIRS = 5          # 二段目の重み学習に要する最小ペア数（未満は等重み）
_MAX_GRID_POINTS = 50_000    # rank_ic_grid の格子点上限（基底数×刻みの組合せ爆発ガード）


def _equal_w(n: int) -> tuple:
    """等重み（1/n × n 個）。基底数に依存しない既定・フォールバック値。"""
    return tuple(1.0 / n for _ in range(n))


def _simplex_grid_size(n: int, step: float) -> int:
    """`_simplex_grid(n, step)` が返す格子点数 C(steps+n−1, n−1)（実体化せずに数える）。"""
    steps = max(1, int(round(1.0 / step)))
    return math.comb(steps + n - 1, n - 1)


def _simplex_grid(n: int, step: float):
    """和1・非負の重みベクトルを step 刻みで列挙する（n 次元シンプレックス格子）。

    n=2 なら (1.0,0.0),(0.95,0.05),… と旧実装（w1 を 0→1 で走査）と同じ並びになる。
    格子点数は C(steps+n-1, n-1)（n=3・step=0.05 で 231 点）。
    """
    steps = max(1, int(round(1.0 / step)))

    def rec(k: int, remaining: int):
        if k == n - 1:
            yield (remaining / steps,)
            return
        for i in range(remaining, -1, -1):       # 先頭要素の降順＝旧実装と同じ走査順
            for rest in rec(k + 1, remaining - i):
                yield (i / steps,) + rest

    return rec(0, steps)

# サブモデル config の上書き（テスト/オフライン検証用・通常運用は空のまま）。
# coerce 前の raw dict に適用する。例: {"macro_risk_return": {"use_macro": False}}。
# 本番既定（空）では各サブモデルの params_schema 既定＝model_comparison と同一 config になる。
SUB_PARAM_OVERRIDES: dict[str, dict] = {}


# ── OOF 整列・重み最適化ヘルパ ─────────────────────────────────────────────

def _align(resid_by_ym: dict, ids_by_ym: dict) -> dict:
    """{ym:[(yhat,y)]} と {ym:[edinet_code]} を突合し {(ym,ec):(yhat,y)} を返す。

    build_snapshots(return_stock_ids=True) と walk_forward_cv_monthly(return_residuals=True) は
    同一 samples_by_ym[ym] のサンプル順を保存する（macro_snapshots.py の連続 append・
    utils.py の list(zip(...))）ため、index で 1:1 突合できる。resid の ym は fold 月のみ＝
    ids の部分集合なので安全。
    """
    out: dict = {}
    for ym, pairs in resid_by_ym.items():
        ids = ids_by_ym.get(ym, [])
        for k, (yh, y) in enumerate(pairs):
            if k < len(ids) and yh == yh and y == y:  # NaN 除外（yh==yh）
                out[(ym, ids[k])] = (yh, y)
    return out


def _fit_weights(train_by_ym: dict, method: str, grid_step: float,
                 n_base: int | None = None) -> tuple:
    """月グループ保持の共通 OOF から統合重み (w_1..w_n) を学習（非負・和1正規化）。

    train_by_ym: {ym: [(ec, (yh_1..yh_n), y_true), ...]}（学習に使う過去月のみ）。
    method: "nnls"（非負最小二乗）/ "rank_ic_grid"（期内平均 Spearman 最大化）/ "equal"。
    n_base: 基底数。省略時は最初のペアの μ̂ タプル長（空データのフォールバック用に明示可）。
    """
    pairs = [p for v in train_by_ym.values() for p in v]
    n = n_base if n_base is not None else (len(pairs[0][1]) if pairs else len(BASE_MODELS))
    if len(pairs) < _MIN_META_PAIRS or method == "equal":
        return _equal_w(n)

    if method == "nnls":
        A = np.array([p[1] for p in pairs], dtype=float)   # 列 = 基底の μ̂（BASE_MODELS 順）
        b = np.array([p[2] for p in pairs], dtype=float)   # y_true
        try:
            w, _ = nnls(A, b)                              # w >= 0
        except Exception:
            return _equal_w(n)
        s = float(w.sum())
        return tuple(float(wi / s) for wi in w) if s > 0 else _equal_w(n)

    if method == "rank_ic_grid":
        # 格子点数 C(steps+n-1, n-1) が上限超なら刻みを粗くする（基底追加時の爆発ガード）。
        # 点数は math.comb で数える（数えるために格子を実体化するとガード自体が飽和する）。
        step = grid_step
        while _simplex_grid_size(n, step) > _MAX_GRID_POINTS and step < 0.5:
            step = min(0.5, step * 2)
        best_w, best_ic = _equal_w(n), -2.0
        months = [v for v in train_by_ym.values() if len(v) >= 3]
        for w in _simplex_grid(n, step):
            ics = []
            for v in months:
                ic = _spearman([sum(wi * yh for wi, yh in zip(w, p[1])) for p in v],
                               [p[2] for p in v])
                if ic is not None:
                    ics.append(ic)
            if ics:
                m = sum(ics) / len(ics)
                if m > best_ic:
                    best_ic, best_w = m, w
        return best_w

    return _equal_w(n)


# build_snapshots の出力を左右するパラメータ（これらが一致すればスナップショットは同一）。
_BUILD_KEYS = ("fin_features", "use_macro", "macro_features", "use_momentum",
               "momentum_window", "min_coverage", "price_features")


def _same_build_config(pa: dict, pb: dict) -> bool:
    """2つのサブモデル config が同一スナップショットを生むか（build 共有の可否判定）。

    M-2 と M-6 は build 契約（交差項なし・macro_nan_ok）が同じで、既定 config も一致する。
    一致する限り build を1回に減らせる（SUB_PARAM_OVERRIDES で片方だけ変えた場合は別構築）。
    """
    return all(pa.get(k) == pb.get(k) for k in _BUILD_KEYS)


def _drop_dead_macro_features(mc: dict, macro_names: list, prices_by_co: dict) -> tuple[list, list]:
    """全プローブ日で変換後 None になるマクロ特徴を除外する（M-1 strict 全滅の防止）。

    本番実測（2026-07-23）で `macro_jp_real_gdp_yoy` / `macro_jp_weo_gdp_fcast_zscore` /
    `macro_jp_weo_cpi_fcast_zscore` の3特徴が変換窓不足で**全期間 None** になり、strict
    （macro_nan_ok=False）の M-1 は1特徴でも None なら行を落とすため全 snapshot が死ぬ
    （#358 の全マクロ既定 ON 以降、M-1 既定実行が0件になる本番バグ・別 Issue）。M-4 の
    M-1 レグはこのガードで生存させ、除外リストを結果 `dropped_macro_features_m1` に明示する
    （M-3 の `diagnostics.dropped_factors` と同思想）。価格履歴の端点＋中間の5点で評価し、
    **全点 None の特徴のみ**除外する（部分カバレッジの特徴は strict 本来の意味に委ねる）。
    """
    if not macro_names:
        return macro_names, []
    from datetime import date as _date, timedelta as _td
    last_dates = [rows[-1].trade_date for rows in prices_by_co.values() if rows]
    if not last_dates:
        return macro_names, []
    ref = _date.fromisoformat(str(max(last_dates))[:10])
    # プローブは labeled 領域（52週先ラベルが計算できる ＝ 価格末尾 − HORIZON_WEEKS 以前）に
    # 置く。末尾近傍だけ生きる部分カバレッジ特徴（実測: macro_jp_real_gdp_yoy が直近数ヶ月のみ
    # 非None）を「生存」と誤判定すると、labeled 領域の strict 行が全滅して OOF が空になるため。
    label_end = ref - _td(weeks=52)
    probes = [(label_end - _td(days=d)).isoformat() for d in (0, 200, 420, 780, 1100)]
    # カバレッジ閾値（M-3 の _MIN_FACTOR_COVERAGE と同思想）: 5点中3点以上（≥60%）非None の
    # 特徴のみ keep。「1点でも生存なら keep」だと直近スライスのみ生きる特徴（実測:
    # macro_jp_real_gdp_yoy が lag シフトで ~2025-07 以降のみ非None）が残り、strict が
    # labeled 領域の大半を殺して fold が形成できなくなる。
    alive_count: dict = {f: 0 for f in macro_names}
    for d in probes:
        for f, v in _macro_from_cache(mc, d, macro_names).items():
            if v is not None:
                alive_count[f] += 1
    need = max(3, (len(probes) * 3 + 4) // 5)   # 5点→3点以上
    kept = [f for f in macro_names if alive_count[f] >= need]
    dropped = [f for f in macro_names if alive_count[f] < need]
    return kept, dropped


def _stack_walk_forward(common: dict, weight_method: str, grid_step: float,
                        min_meta_months: int, n_base: int | None = None) -> tuple[dict, dict]:
    """二段ウォークフォワード: 月 t の統合重みを t 未満の共通 OOF だけで学習し適用する。

    common: {ym: [(ec, (yh_1..yh_n), y_true), ...]}（全基底に共通の base OOF）。
    Returns: (stacked, weights_by_ym)
      stacked = {t: [(yhat_stack, y_true), ...]}（oof_backtest 入力形）
      weights_by_ym = {t: (w_1..w_n)}（各月に適用した重み・診断用）
    リーク回避: base μ̂ 自体が embargo 済み OOF であり、月 t の重みは yms[:i]
    （t より厳密に前の月）だけで学習するため t の実現 y_true を一切見ない。
    """
    yms = sorted(common)
    n = n_base if n_base is not None else (
        len(common[yms[0]][0][1]) if yms and common[yms[0]] else len(BASE_MODELS))
    stacked: dict[str, list] = {}
    weights_by_ym: dict[str, tuple] = {}
    for i, t in enumerate(yms):
        train_by_ym = {ym: common[ym] for ym in yms[:i]}
        if i < min_meta_months or sum(len(v) for v in train_by_ym.values()) < _MIN_META_PAIRS:
            w = _equal_w(n)
        else:
            w = _fit_weights(train_by_ym, weight_method, grid_step, n_base=n)
        weights_by_ym[t] = w
        stacked[t] = [(sum(wi * yh for wi, yh in zip(w, yhats)), y)
                      for (_ec, yhats, y) in common[t]]
    return stacked, weights_by_ym


class MacroEnsemblePlugin(AnalysisPlugin):
    name = "macro_ensemble"
    label = "M-4: 兄弟μ̂スタッキング・アンサンブル"
    description = ("M-1(OLS)・M-2(XGBoost)・M-6(ElasticNet) の OOF μ̂ を二段ウォークフォワードで"
                   "統合するメタモデル。実行は内部で3モデルすべてを回すため数分かかります"
                   "（ボタンが「実行中...」の間は計算継続中）。")
    depends_on: list[str] = []          # 自前で全計算（r_macro は graceful マージ）
    heavy = True
    category = "③ 将来リターンを予測"
    ui_order = 370                       # M-3=360 の後（M-1→M-2→M-3→M-4 順・#378）

    def params_schema(self) -> dict:
        return {
            "weight_method": {
                "type": "select",
                "label": "重み最適化",
                "options": [
                    {"value": "rank_ic_grid", "label": "rank-IC 最大化グリッド（既定）"},
                    {"value": "nnls",         "label": "NNLS（非負最小二乗）"},
                    {"value": "equal",        "label": "等重み（単純平均）"},
                ],
                "default": "rank_ic_grid",
                "description": (
                    "M-1/M-2/M-6 の μ̂ を統合する重みの決め方。全て非負・和1に正規化。"
                    "既定が rank-IC 最大化なのは評価指標と目的を一致させるため（#397 実測: "
                    "MSE 最小化の NNLS は縮小推定の M-6 を重み0で捨て、OOF の改善が現在μ̂へ反映されない）。"
                ),
            },
            "min_meta_months": {
                "type": "number", "dtype": "int",
                "label": "重み学習を開始する最小過去OOF月数",
                "default": 2, "min": 1, "max": 12,
            },
            "grid_step": {
                "type": "slider", "dtype": "float",
                "label": "グリッド刻み（rank_ic_grid 専用）",
                "default": 0.05, "min": 0.01, "max": 0.5, "step": 0.01,
            },
            "n_quantiles": {
                "type": "number", "dtype": "int",
                "label": "分位数（OOF ロングショート評価）",
                "default": 5, "min": 3, "max": 10,
            },
            "top_n": {
                "type": "number", "dtype": "int",
                "label": "表示件数（クライアント初期シード）",
                "default": 30, "min": 5, "max": 200,
            },
        }

    # ── producer/consumer（sell_ranking 共用契約・M-2 と同型）───────────────
    def produced_output(self, db: Any) -> bool:
        """M-4 統合 μ̂（macro_ensemble_scores）を共有DBに持つか（graceful 判定用）。"""
        try:
            from database import get_macro_ensemble_scores
            return bool(get_macro_ensemble_scores(db))
        except Exception:
            return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        """{edinet_code: {mu, r_macro, r1_prime}} を返す（M-1/M-2 と同一形・sell_ranking 共用）。

        mu=永続化済み統合μ̂、r_macro=共有 macro_beta producer からマージ、r1_prime=None。"""
        from database import get_macro_ensemble_scores
        mus = get_macro_ensemble_scores(db)
        if not mus:
            return {}
        r_macro_src = get_producer_scores(db, macro_snapshot)
        return {
            ec: {"mu": float(mu),
                 "r_macro": (r_macro_src.get(ec) or {}).get("r_macro"),
                 "r1_prime": None}
            for ec, mu in mus.items()
        }

    # ── 基底モデルの per-(ym,ec) OOF 再現 ─────────────────────────────────
    # 【共通月グリッドが必須】walk_forward_cv_monthly の fold 月は各モデル自身の
    # all_yms の index 基準（range(min_train+embargo, len, step)）で決まるため、
    # 母集団差（M-1 strict がマクロ未整備の初期月を落とす等）で月集合が 1 ヶ月ずれる
    # だけで fold 月が全て位相シフトし (ym,ec) 交差が空になる（#367 オフライン実測で
    # 検出した実バグ）。よって build と CV を分離し、両モデルの共通月グリッドへ
    # 揃えてから各 CV を回す（fold 月が同一化し交差が成立する）。

    def _m1_build(self, db, prices_by_co, fin_by_co, companies, m1p) -> dict:
        """M-1 構成（交差項あり・strict）のスナップショットを構築する（CV はまだ）。

        strict は1マクロ特徴でも None なら行を落とすため、全期間 None の死に特徴を
        事前除外する（_drop_dead_macro_features・除外リストは結果へ明示）。"""
        from plugins import get_plugin
        m1 = get_plugin("macro_risk_return")
        macro = list(m1p["macro_features"]) if m1p["use_macro"] else []
        mc = preload_macro(db, prices_by_co, macro) if macro else {}
        macro, dropped = (_drop_dead_macro_features(mc, macro, prices_by_co)
                          if macro else (macro, []))
        s, meta, cur, feats, ids = build_snapshots(
            prices_by_co, fin_by_co, companies, mc,
            m1p["fin_features"], macro, m1p["use_momentum"], m1p["momentum_window"],
            m1p["min_coverage"], build_interactions=True, return_stock_ids=True)  # strict
        return {"inst": m1, "s": s, "meta": meta, "cur": cur, "feats": feats, "ids": ids,
                "dropped_macro": dropped}

    def _m2_build(self, db, prices_by_co, fin_by_co, companies, m2p) -> dict:
        """M-2 構成（交差項なし・macro_nan_ok）のスナップショットを構築する（CV はまだ）。"""
        macro = list(m2p["macro_features"]) if m2p["use_macro"] else []
        mc = preload_macro(db, prices_by_co, macro) if macro else {}
        s, meta, cur, feats, ids = build_snapshots(
            prices_by_co, fin_by_co, companies, mc,
            m2p["fin_features"], macro, m2p["use_momentum"], m2p["momentum_window"],
            m2p["min_coverage"], build_interactions=False, macro_nan_ok=True,
            return_stock_ids=True,
            price_features=m2p.get("price_features") or [],  # M-2 と同構成を維持（#364）
        )
        return {"s": s, "meta": meta, "cur": cur, "feats": feats, "ids": ids}

    @staticmethod
    def _filter_yms(b: dict, yms: list) -> tuple[dict, dict]:
        """samples/ids を共通月グリッドへ制限する（ym 内のサンプル順は不変＝整列保存）。"""
        s = {ym: b["s"][ym] for ym in yms if ym in b["s"]}
        ids = {ym: b["ids"][ym] for ym in yms if ym in b["ids"]}
        return s, ids

    def _m1_cv(self, b: dict, m1p: dict, yms: list):
        """共通月グリッド上で M-1 の OOF を {(ym,ec):(yhat,y)} で返す（+ 現在μ̂用の中間物）。"""
        s, ids = self._filter_yms(b, yms)
        m1, feats = b["inst"], b["feats"]
        sel_names = m1._select_macro_features(s, feats, max_features=m1p["max_features"])
        if not sel_names:
            raise ValueError("M-1 の BIC 選択が空で兄弟μ̂を構成できません。")
        sel_idx = [feats.index(n) for n in sel_names]
        s_sel = {ym: [([row[i] for i in sel_idx], tgt) for row, tgt in pairs]
                 for ym, pairs in s.items()}
        _, resid = walk_forward_cv_monthly(
            s_sel, sel_names, min_train_months=6, step_months=3,
            return_residuals=True, embargo_months=LABEL_HORIZON_MONTHS)  # 既定 OLS
        return _align(resid, ids), {
            "inst": m1, "s_sel": s_sel, "sel_idx": sel_idx, "n_sel": len(sel_names),
            "sel_names": sel_names, "cur": b["cur"], "resid": resid, "meta": b["meta"],
        }

    def _m2_cv(self, b: dict, m2p: dict, yms: list):
        """共通月グリッド上で M-2 の OOF を {(ym,ec):(yhat,y)} で返す（+ 現在μ̂用の中間物）。"""
        s, ids = self._filter_yms(b, yms)
        xgb_params = {
            "max_depth": m2p["max_depth"], "learning_rate": m2p["learning_rate"],
            "subsample": m2p["subsample"], "colsample_bytree": m2p["colsample_bytree"],
            "min_child_weight": m2p["min_child_weight"], "reg_lambda": m2p["reg_lambda"],
            "reg_alpha": m2p["reg_alpha"], "n_estimators": m2p["n_estimators_max"],
            "early_stopping_rounds": m2p["early_stopping_rounds"],
            "tree_method": "hist", "objective": "reg:squarederror", "random_state": 42,
        }
        best_iters: list[int] = []
        cb = _make_xgb_fit_predict(xgb_params, best_iters)
        _, resid = walk_forward_cv_monthly(
            s, b["feats"], min_train_months=6, step_months=3,
            return_residuals=True, fit_predict=cb, embargo_months=LABEL_HORIZON_MONTHS)
        return _align(resid, ids), {
            "s": s, "cur": b["cur"], "xgb_params": xgb_params, "best_iters": best_iters,
            "n_est_max": m2p["n_estimators_max"],
        }

    def _m6_cv(self, b: dict, m6p: dict, yms: list):
        """共通月グリッド上で M-6 の OOF を {(ym,ec):(yhat,y)} で返す（+ 現在μ̂用の中間物）。

        M-6 のスナップショット構成は M-2 と同値（交差項なし・macro_nan_ok）なので build は
        共有し、CV だけ ElasticNet の fit_predict へ差し替える。探索設定は macro_enet の
        モジュール定数をそのまま参照する（M-6 単体実行と同一コードパス＝数値の二重管理を避ける）。"""
        from .macro_enet import _CV_SPLITS, _MAX_ITER, _N_ALPHAS, MacroEnetPlugin
        from .model_candidates import make_diag, make_elasticnet_fit_predict

        s, ids = self._filter_yms(b, yms)
        l1_ratios = MacroEnetPlugin._l1_ratios(m6p["l1_ratio"])
        fp = make_elasticnet_fit_predict(
            l1_ratios=l1_ratios, n_alphas=_N_ALPHAS, cv_splits=_CV_SPLITS,
            max_iter=_MAX_ITER, diag=make_diag(),
        )
        _, resid = walk_forward_cv_monthly(
            s, b["feats"], min_train_months=6, step_months=3,
            return_residuals=True, fit_predict=fp, embargo_months=LABEL_HORIZON_MONTHS)
        return _align(resid, ids), {"s": s, "cur": b["cur"], "l1_ratios": l1_ratios}

    def _current_mu_m6(self, ctx) -> dict:
        """M-6 の最終 ElasticNet を全データで学習し現在μ̂ {ec: mu} を返す。

        M-6 単体の `_fit_final_and_score`（CV と同一前処理）をそのまま呼ぶ。"""
        from .macro_enet import MacroEnetPlugin
        s, cur = ctx["s"], ctx["cur"]
        all_s = [row for v in s.values() for row in v]
        codes = list(cur.keys())
        _coefs, mu, _meta = MacroEnetPlugin._fit_final_and_score(
            all_s, [cur[c][0] for c in codes], ctx["l1_ratios"])
        return dict(zip(codes, mu))

    def _current_mu_m1(self, ctx, prices_by_co) -> dict:
        """M-1 の最終 OLS を全データで学習し現在μ̂ {ec: mu_raw} を返す（DBテーブル無いため自前）。"""
        m1 = ctx["inst"]
        r3_data = m1._compute_r3_buckets(ctx["resid"], ctx["meta"])  # r3 は結果に載るが μ̂ に非依存
        fit = m1._fit_final(ctx["s_sel"], ctx["n_sel"])             # (beta, win, norm, y_mu, y_sd, XtXinv, sigma2)
        items = m1._score_companies(ctx["cur"], ctx["sel_idx"], ctx["n_sel"], *fit,
                                    prices_by_co, r3_data)
        return {it["edinet_code"]: it["mu_raw"] for it in items}

    def _current_mu_m2(self, ctx) -> dict:
        """M-2 の最終 XGB を全データで学習し現在μ̂ {ec: mu} を返す（macro_gbdt.py:481-501 と同型）。"""
        import xgboost as xgb
        s, cur, xgb_params = ctx["s"], ctx["cur"], ctx["xgb_params"]
        n_est = (int(statistics.median(ctx["best_iters"])) if ctx["best_iters"]
                 else ctx["n_est_max"] // 2)
        all_s = [row for v in s.values() for row in v]
        X_all = np.array([row[0] for row in all_s], dtype=float)
        y_w, _, _ = winsorize([row[1] for row in all_s])
        final_params = {k: v for k, v in xgb_params.items()
                        if k not in ("n_estimators", "early_stopping_rounds")}
        model = xgb.XGBRegressor(**final_params, n_estimators=n_est)
        model.fit(X_all, np.array(y_w, dtype=float), verbose=False)
        codes = list(cur.keys())
        preds = model.predict(np.array([cur[c][0] for c in codes], dtype=float)).tolist()
        return dict(zip(codes, preds))

    def execute(self, params: dict, db: Any) -> dict:
        try:
            import xgboost  # noqa: F401  — M-2 OOF / 最終 fit に必須
        except Exception:
            raise ValueError("XGBoost が未インストールのため M-4 を実行できません。")

        weight_method = params["weight_method"]
        min_meta_months = params["min_meta_months"]
        grid_step = params["grid_step"]
        n_quantiles = params["n_quantiles"]
        top_n = params["top_n"]

        # サブモデル config は各既定を coerce して流用（model_comparison と apples-to-apples）。
        from plugins import get_plugin
        subp = {name: coerce_params(get_plugin(name).params_schema(),
                                    SUB_PARAM_OVERRIDES.get(name, {}))
                for name in BASE_MODELS}

        prices_by_co, fin_by_co, companies = load_data(db)
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に収集を実行してください。")

        # ── 1) 各基底のスナップショット構築 → 共通月グリッドで OOF ──────
        # fold 月は all_yms の index 基準のため、月集合を揃えてから CV する
        # （揃えないと 1 ヶ月のずれで fold 月が位相シフトし交差が空になる）。
        # レグは BASE_MODELS で駆動する（基底の増減＝定数の変更だけで済む・#397）。
        builds: dict[str, dict] = {}
        for name in BASE_MODELS:
            if name == "macro_risk_return":
                builds[name] = self._m1_build(db, prices_by_co, fin_by_co, companies, subp[name])
                continue
            # M-2/M-6 は同じ build 契約（交差項なし・macro_nan_ok）。config が同値な
            # 先行レグがあればスナップショットを共有する（既定では M-2↔M-6 が常に同値）。
            shared = next((b for n, b in builds.items()
                           if n != "macro_risk_return" and _same_build_config(subp[n], subp[name])),
                          None)
            builds[name] = shared if shared is not None else self._m2_build(
                db, prices_by_co, fin_by_co, companies, subp[name])

        common_yms = sorted(set.intersection(*(set(b["s"]) for b in builds.values())))
        if not common_yms:
            raise ValueError("基底モデルのスナップショット月が重ならず統合できません。")

        _CV = {"macro_risk_return": self._m1_cv, "macro_gbdt": self._m2_cv,
               "macro_enet": self._m6_cv}
        oofs: dict[str, dict] = {}
        ctxs: dict[str, dict] = {}
        for name in BASE_MODELS:
            oofs[name], ctxs[name] = _CV[name](builds[name], subp[name], common_yms)

        # ── 2) (ym, edinet_code) intersection（全基底に揃う行だけ）────────
        # 先頭レグの挿入順（= ym 内のサンプル index 順）で積む＝ oof_meta と行対応を保つ。
        head, rest = BASE_MODELS[0], BASE_MODELS[1:]
        common: dict[str, list] = {}   # ym -> [(ec, (yh_1..yh_n), y_true)]
        for key, (yh0, y0) in oofs[head].items():
            hits = [oofs[n].get(key) for n in rest]
            if any(h is None for h in hits):
                continue
            common.setdefault(key[0], []).append(
                (key[1], (yh0, *(h[0] for h in hits)), y0))   # y_true は全基底で同一定義
        n_common = sum(len(v) for v in common.values())
        n_base = len(BASE_MODELS)

        # ── 3) 二段ウォークフォワード重み → 統合残差 → oof_backtest ────────
        stacked, _weights_by_ym = _stack_walk_forward(
            common, weight_method, grid_step, min_meta_months, n_base=n_base)

        # Issue #368: 業種中立rank-IC / 実効ターンオーバー用メタ。stacked[ym] も base_oof も
        # common[ym] のサンプル順（ec 先頭）を保存するため同一メタで突合できる。四半期リバランス
        # （M-1/M-2 と同じ step_months=3 → rebalance_per_year=4）。
        def _ind(ec: str) -> str:
            comp = companies.get(ec)
            return (comp.industry if comp else "不明") or "不明"
        oof_meta = {ym: [(ec, _ind(ec)) for (ec, *_rest) in v] for ym, v in common.items()}

        oof_bt = oof_backtest(stacked, n_quantiles=n_quantiles,
                              meta_by_ym=oof_meta, rebalance_per_year=4)

        # 基底モデルを「同一の共通 (ym,ec) 行」に制限した OOF（apples-to-apples 比較用）。
        # 各モデル単体の oof_backtest は自分の母集団/グリッドで算出されるため、M-4 との
        # 優劣判定にはこの共通域の値を使う（母集団差の交絡を除去・ADR-0015）。
        base_oof = {
            name: oof_backtest(
                {ym: [(yhats[j], y) for (_ec, yhats, y) in v] for ym, v in common.items()},
                n_quantiles=n_quantiles, meta_by_ym=oof_meta, rebalance_per_year=4)
            for j, name in enumerate(BASE_MODELS)
        }

        # ── ハイパラ探索/比較中は oof 算出後に早期return（現在μ̂/永続化を省く・#299）─
        from database import is_tuning_objective_only
        w_final = (_fit_weights(common, weight_method, grid_step, n_base=n_base)
                   if n_common >= _MIN_META_PAIRS else _equal_w(n_base))
        base_result = {
            "oof_backtest":       oof_bt,
            "base_oof_backtest":  base_oof,   # 共通域に制限した各基底の OOF（優劣判定用）
            "weights":            {name: round(w_final[j], 4)
                                   for j, name in enumerate(BASE_MODELS)},
            "weight_method":      weight_method,
            "n_common_pairs":     n_common,
            "base_models":        list(BASE_MODELS),
            "selected_features_m1": ctxs.get("macro_risk_return", {}).get("sel_names", []),
            "dropped_macro_features_m1":
                builds.get("macro_risk_return", {}).get("dropped_macro", []),
            "top_n":              top_n,
        }
        if is_tuning_objective_only():
            base_result.update(n_companies=0, results=[], r_macro_available=False)
            return base_result

        # ── 4) 現在μ̂の統合（M-1 _fit_final/_score_companies・M-2 最終 XGB・M-6 最終 ENet）─
        cur_mu = {
            "macro_risk_return": lambda c: self._current_mu_m1(c, prices_by_co),
            "macro_gbdt":        self._current_mu_m2,
            "macro_enet":        self._current_mu_m6,
        }
        mus_by_model = {name: cur_mu[name](ctxs[name]) for name in BASE_MODELS}
        # 表示キーは M-x 表記（既存クライアント互換: mu_m1 / mu_m2 / mu_m6）。
        mu_keys = {"macro_risk_return": "mu_m1", "macro_gbdt": "mu_m2", "macro_enet": "mu_m6"}
        curs = [ctxs[n]["cur"] for n in reversed(BASE_MODELS)]   # info は後発レグ優先で引く
        results: list[dict] = []
        for ec in set.intersection(*(set(m) for m in mus_by_model.values())):
            info = next((c[ec][1] for c in curs if ec in c), None)
            if info is None:
                continue
            mus = tuple(float(mus_by_model[n][ec]) for n in BASE_MODELS)   # BASE_MODELS 順
            item = {
                "edinet_code":  ec,
                "sec_code":     info.get("sec_code"),
                "company_name": info.get("company_name"),
                "industry":     info.get("industry"),
                "mu_raw":       round(sum(w * m for w, m in zip(w_final, mus)), 6),
                "r1": None, "r2": None, "r3": None,
            }
            item.update({mu_keys[n]: round(mus[j], 6) for j, n in enumerate(BASE_MODELS)})
            results.append(item)
        results.sort(key=lambda x: x.get("mu_raw") or -1e18, reverse=True)

        # ── 5) R_macro マージ（macro_beta producer・graceful）──────────────
        try:
            rmacro = get_producer_scores(db)
        except Exception:
            rmacro = {}
        for it in results:
            prod = rmacro.get(it["edinet_code"])
            it["r_macro"] = (round(float(prod["r_macro"]), 6)
                             if (prod and prod.get("r_macro") is not None) else None)
        r_macro_available = any(it["r_macro"] is not None for it in results)

        # ── 6) producer 永続化（sell_ranking が mu_source=macro_ensemble で読む）─
        snaps = [info.get("snap_date") for _, info in curs[0].values()]
        snaps = [d for d in snaps if d]
        rep = max(snaps) if snaps else None
        rep_str = (rep.isoformat() if hasattr(rep, "isoformat") else (str(rep)[:10] if rep else None))
        try:
            from database import replace_macro_ensemble_scores
            replace_macro_ensemble_scores(
                db, [{"edinet_code": it["edinet_code"], "mu": it["mu_raw"]} for it in results],
                rep_str)
        except Exception:
            pass   # 読取専用DB等でも表示を妨げない（次回実行で再生成）

        base_result.update(n_companies=len(results), results=results,
                           r_macro_available=r_macro_available)
        return base_result


plugin = MacroEnsemblePlugin()
