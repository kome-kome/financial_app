"""
M-3 ベイズ状態空間モデル（銘柄別 時変マクロβ DLM）推奨プラグイン

M-1（macro_risk_return, OLS 線形）・M-2（macro_gbdt, XGBoost 非線形）の第3の兄弟。
両者が「ある時点でクロスセクションに係数を1つ推定する（静的係数）」のに対し、本モデルは
**係数そのものが時間とともに変動する**ベイズ動的線形モデル（DLM）を、銘柄ごとに独立な
カルマン型逐次ベイズ更新で推定する。

モデル（銘柄 i・週 t。添字 i 省略）:
    観測:   y_t = F_t' θ_t + ν_t,   ν_t ~ N(0, V_t)
            F_t = [1, Δm_{1,t}, …, Δm_{k,t}]'        （定数項=α 用の 1 + マクロ週次変化）
            θ_t = [α_t, β_{1,t}, …, β_{k,t}]'         （状態 = 時変アルファ + 時変マクロβ）
    システム: θ_t = θ_{t-1} + ω_t,  ω_t ~ N(0, W_t)    （ローカルレベル＝ランダムウォーク）

割引係数 δ により W_t = C_{t-1}(1−δ)/δ を与えるため、銘柄ごとの数値最適化が不要
（1銘柄1フォワードパス・高速）。観測分散 V_t は分散割引（Normal-Gamma 共役・β_v）で
オンライン学習し、予測分布は Student-t になる → α・β の信用区間が解析的に得られる。

出力:
    µ̂ = 年率化した最新フィルタ α_T（= α_T × 52）。M-1/M-2 と同じ µ̂ ランキングに載せられる。
    β 経路 = 各週の状態の β 成分と信用区間（時変マクロ感応度の可視化）。
    検証 = DLM 内蔵の1期先予測診断（標準化予測誤差・予測RMSE・95%信用区間カバレッジ）。

設計決定:
  - 銘柄別 TVP 時系列（per-stock）。週次リターン × マクロ週次変化（同時点ファクタ応答 / APT 型）。
  - マクロ + 市場ファクター（既定 USDJPY/US10Y/NIKKEI225/WTI）。指数/FX/商品は対数リターン、
    金利は週次差分（bp スケール）。
  - 自前 割引 DLM（West & Harrison 型・numpy）。全適格銘柄・heavy=True。
  - 初版は API/UI のみ（producer 化・sell_ranking 統合はフォロー Issue）。

参考文献:
  West, M. & Harrison, J. (1997) Bayesian Forecasting and Dynamic Models, 2nd ed., Springer.
  Kalman, R. E. (1960) https://doi.org/10.1115/1.3662552
  Ross, S. A. (1976) https://doi.org/10.1016/0022-0531(76)90046-6
"""
import bisect
import math
from collections import namedtuple
from typing import Any

import numpy as np
from scipy import stats

from .base import AnalysisPlugin
from .utils import macro_risk_exposure

# ── マクロ・ファクター定義 ───────────────────────────────────────────────────
# feature_name → (series_code, kind, label)。
#   kind="logret": 指数/FX/商品 → 週次対数リターン  log(level_t / level_{t-1})
#   kind="diff"  : 金利         → 週次差分          level_t − level_{t-1}（％ポイント）
_DLM_MACRO_MAP: dict[str, tuple[str, str, str]] = {
    "dlm_usdjpy":    ("USDJPY",     "logret", "USD/JPY 週次変化"),
    "dlm_eurjpy":    ("EURJPY",     "logret", "EUR/JPY 週次変化"),
    "dlm_dxy":       ("DXY",        "logret", "ドル指数（DXY）週次変化"),
    "dlm_sp500":     ("SP500",      "logret", "S&P500 週次変化"),
    "dlm_nikkei225": ("NIKKEI225",  "logret", "日経225 週次変化（市場ファクター）"),
    "dlm_topix":     ("TOPIX",      "logret", "TOPIX 週次変化（広範市場ファクター）"),
    "dlm_vix":       ("VIX",        "logret", "VIX 週次変化"),
    "dlm_wti":       ("WTI",        "logret", "WTI原油 週次変化"),
    "dlm_gold":      ("GOLD",       "logret", "金（ゴールド）週次変化"),
    "dlm_us5y":      ("US5Y",       "diff",   "米5年金利 週次差分"),
    "dlm_us10y":     ("US10Y",      "diff",   "米10年金利 週次差分"),
    "dlm_us30y":     ("US30Y",      "diff",   "米30年金利 週次差分"),
    # 日次の日本10年金利は Yahoo ^JGB が廃止（404）で取得不能。FRED は月次のみのため
    # 週次差分は多くの週でゼロ＝情報量に限界あり（日次ソース確保は将来課題）。
    "dlm_jp10y":     ("JP10Y_FRED", "diff",   "日10年金利（FRED・月次）週次差分"),
    "dlm_t10y2y":    ("T10Y2Y",     "diff",   "米10y−2yスプレッド 週次差分"),
}
MACRO_FEATURE_OPTIONS = [{"value": k, "label": v[2]} for k, v in _DLM_MACRO_MAP.items()]
DEFAULT_MACRO_FEATURES = ["dlm_usdjpy", "dlm_us10y", "dlm_nikkei225", "dlm_wti"]

WEEKS_PER_YEAR = 52
# 週次日付グリッドで forward-fill 値がこの割合未満しか無い factor はモデルから自動除外する。
# 未収集・歴史の浅い系列を含めると全週で欠損→全週スキップ→企業が一斉脱落するため、factor を
# 落として企業母集団を factor 選択から切り離す（除外は diagnostics に表示）。
_MIN_FACTOR_COVERAGE = 0.5
_MAX_PATH_POINTS = 120          # チャート用に経路をこの点数まで間引く（payload 抑制）
_AUTO_SAMPLE_N = 50             # 自動ハイパーパラメータ選択で使用する銘柄サンプル数
# δ / β_v の探索グリッド（粗→細の2段階候補）
_AUTO_DELTA_GRID = [0.90, 0.93, 0.95, 0.97, 0.98, 0.99, 0.995]
_AUTO_BV_GRID    = [0.90, 0.93, 0.95, 0.97, 0.99, 1.00]
# 弱情報事前 C_0（拡散）。α は週次リターン規模（~0.03）、β は無次元規模に合わせる。
_PRIOR_VAR_ALPHA = 0.04         # α の事前分散（sd≒0.2/週・実質拡散）
_PRIOR_VAR_BETA = 4.0           # β の事前分散（sd≒2）


# ── データロード（prices + macro のみ。財務は使わない）───────────────────────

def load_prices(db) -> tuple[dict, dict]:
    """StockPriceWeekly（週次株価）と Company を一括ロードして
    (prices_by_co, companies) を返す。financial_metrics は使わない（本モデルは
    財務を参照しないため load_data の重い VIEW クエリを回避する）。"""
    from database import Company, StockPriceWeekly
    _PX = namedtuple("_PX", "trade_date close_last")
    raw = (
        db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                 StockPriceWeekly.close_last)
        .order_by(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date)
        .all()
    )
    prices_by_co: dict[str, list] = {}
    for ec, td, cl in raw:
        prices_by_co.setdefault(ec, []).append(_PX(td, cl))
    companies = {c.edinet_code: c for c in db.query(Company).all()}
    return prices_by_co, companies


def load_macro_levels(db, series_codes: list[str], min_date: str | None) -> dict:
    """MacroData から指定系列の (trade_date, close) を昇順で取得し
    {series_code: (dates_sorted, vals)} を返す（forward-fill 用に分離保持）。"""
    from database import MacroData
    if not series_codes:
        return {}
    q = db.query(MacroData.series_code, MacroData.trade_date, MacroData.close).filter(
        MacroData.close.isnot(None),
        MacroData.series_code.in_(series_codes),
    )
    if min_date:
        q = q.filter(MacroData.trade_date >= min_date)
    rows = q.order_by(MacroData.series_code, MacroData.trade_date).all()
    by_series: dict[str, tuple[list, list]] = {}
    for sc, td, cl in rows:
        d, v = by_series.setdefault(sc, ([], []))
        d.append(td)
        v.append(float(cl))
    return by_series


# ── DLM フィルタ（純 numpy・West & Harrison 割引 DLM＋分散学習）───────────────

def dlm_filter(y: list[float], X: list[list[float]], delta: float, beta_v: float,
               phi: float = 1.0) -> dict:
    """割引係数 DLM の1パス・フォワードフィルタ（観測分散は Normal-Gamma 共役で学習）。

    y: 長さ T の観測（週次対数リターン）。X: T×p の設計行（先頭列=1 の定数項）。
    phi: α（先頭状態）の AR(1) 係数。1.0 のときランダムウォーク（デフォルト）。
         phi < 1.0 のとき α は φ·α_{t-1} へ平均回帰（β 成分はランダムウォーク維持）。
    戻り値: m_path/sd_path（各週の状態事後平均・標準偏差）、fe/q（1期先予測誤差と予測分散）、
            std_errs（標準化予測誤差 e/√Q）、最終の m/C/S/n。
    """
    T = len(y)
    p = len(X[0])
    m = np.zeros(p)
    C = np.diag([_PRIOR_VAR_ALPHA] + [_PRIOR_VAR_BETA] * (p - 1))
    # 観測分散の初期点推定 S_0 = 観測の標本分散（最低でも小さな正値）。n_0=1。
    S = float(np.var(np.asarray(y, dtype=float))) if T > 1 else 1e-4
    S = max(S, 1e-8)
    n = 1.0

    # 対角遷移行列 G: α 成分のみ phi（AR(1)）、β 成分は 1.0（ランダムウォーク維持）
    G = np.ones(p)
    G[0] = phi

    m_path = np.empty((T, p))
    sd_path = np.empty((T, p))
    fe = np.empty(T)        # 1期先予測誤差（リターン単位）
    qv = np.empty(T)        # 予測分散 Q_t
    std_errs = np.empty(T)  # 標準化予測誤差 e_t/√Q_t

    for t in range(T):
        F = np.asarray(X[t], dtype=float)
        a = G * m                           # 事前平均 a_t = G ⊙ m_{t-1}
        GCG = (G[:, None] * C) * G[None, :]  # G C G'（G が対角なので要素積で計算）
        R = GCG / delta                     # 事前共分散 R_t = G C G' / δ
        f = float(F @ a)                    # 1期先予測 f_t（観測前）
        Q = float(F @ R @ F) + S            # 予測分散 Q_t
        e = float(y[t] - f)                 # 予測誤差 e_t

        A = (R @ F) / Q             # カルマンゲイン A_t
        n_new = beta_v * n + 1.0    # 分散割引による自由度更新
        S_new = S * (beta_v * n + (e * e) / Q) / n_new   # 観測分散点推定の更新
        m = a + A * e               # 事後平均 m_t
        C = (S_new / S) * (R - np.outer(A, A) * Q)        # 事後共分散 C_t
        S, n = S_new, n_new

        m_path[t] = m
        sd_path[t] = np.sqrt(np.clip(np.diag(C), 0.0, None))
        fe[t] = e
        qv[t] = Q
        std_errs[t] = e / math.sqrt(Q)

    return {
        "m_path": m_path, "sd_path": sd_path,
        "fe": fe, "qv": qv, "std_errs": std_errs,
        "m": m, "C": C, "S": S, "n": n,
    }


# ── 経路の間引き ─────────────────────────────────────────────────────────────

def _downsample_idx(n: int, k: int) -> list[int]:
    """0..n-1 から末尾を必ず含む等間隔 k 点のインデックスを返す。"""
    if n <= k:
        return list(range(n))
    return sorted(set(round(i * (n - 1) / (k - 1)) for i in range(k)))


def _auto_select_hyperparams(
    sample_data: list[tuple[list, list]],
    burn_in: int,
    phi: float = 1.0,
) -> tuple[float, float]:
    """銘柄横断の正規近似周辺対数尤度を最大化して (delta, beta_v) を選ぶ。

    sample_data: [(y, X), ...] のリスト（事前に min_weeks フィルタ済み）。
    burn_in: バーンイン週数（対数尤度計算から除外）。
    phi: dlm_filter に渡す AR(1) 係数。
    Returns: (best_delta, best_beta_v).
    """
    best_ll = -math.inf
    best: tuple[float, float] = (0.98, 0.98)

    for delta in _AUTO_DELTA_GRID:
        for bv in _AUTO_BV_GRID:
            ll = 0.0
            for y, X in sample_data:
                res = dlm_filter(y, X, delta, bv, phi=phi)
                fe = res["fe"][burn_in:]
                qv = res["qv"][burn_in:]
                if fe.size == 0:
                    continue
                qv_safe = np.maximum(qv, 1e-20)
                # 正規近似: log p(y_t | y_{1:t-1}) ≈ -½ [log(2π Q_t) + e_t²/Q_t]
                ll += float(np.sum(
                    -0.5 * (np.log(2 * math.pi * qv_safe) + fe ** 2 / qv_safe)
                ))
            if ll > best_ll:
                best_ll = ll
                best = (float(delta), float(bv))

    return best


def _r(x: float, nd: int = 6) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), nd)


# ── プラグイン本体 ───────────────────────────────────────────────────────────

class MacroDlmPlugin(AnalysisPlugin):
    name = "macro_dlm"
    label = "M-3: ベイズ状態空間（時変マクロβ）"
    description = (
        "銘柄ごとに週次リターンを主要マクロの週次変化へ回帰し、係数（α/β）が時間変動する"
        "動的線形モデル（DLM）をカルマン型逐次ベイズ更新で推定します。最新の潜在アルファ α を"
        "期待リターン µ̂ とし、マクロ感応度 β の時系列と信用区間を可視化。M-1（線形）・M-2（非線形）"
        "が静的係数なのに対し、本モデルは係数の時間変化そのものを捉えます。"
        "【注意】株価週次履歴とマクロデータの蓄積が必要です。"
    )
    depends_on: list[str] = []
    heavy: bool = True
    category = "③ 将来リターンを予測"
    ui_order = 360                       # macro_gbdt(340) の次

    def params_schema(self) -> dict:
        return {
            "macro_features": {
                "type": "multiselect",
                "label": "マクロ・ファクター（週次変化）",
                "options": MACRO_FEATURE_OPTIONS,
                "default": DEFAULT_MACRO_FEATURES,
                "description": "観測式の説明変数。指数/FX/商品は週次対数リターン、金利は週次差分。"
                               "市場ファクター（日経225）を含めると α は市場・マクロ調整後の固有アルファになる。",
            },
            "state_discount": {
                "type": "slider",
                "dtype": "float",
                "label": "状態割引係数 δ",
                "description": "係数 α/β がどれだけ速く時間変動するか。1 に近いほど安定（変化が緩やか）。",
                "default": 0.98, "min": 0.90, "max": 0.999, "step": 0.005,
            },
            "var_discount": {
                "type": "slider",
                "dtype": "float",
                "label": "分散割引係数 β_v",
                "description": "観測分散の学習の忘却率（ボラの時間変化への追従度）。",
                "default": 0.98, "min": 0.90, "max": 1.0, "step": 0.005,
            },
            "min_weeks": {
                "type": "number",
                "dtype": "int",
                "label": "最低週数",
                "description": "この週数未満の銘柄は推定対象外（既定 104 週＝約2年）。",
                "default": 104, "min": 30, "max": 1040,
            },
            "burn_in_weeks": {
                "type": "number",
                "dtype": "int",
                "label": "バーンイン週数",
                "description": "拡散事前の初期不安定区間。診断・経路はこの週数を除外して集計。",
                "default": 26, "min": 0, "max": 260,
            },
            "top_n": {
                "type": "number",
                "dtype": "int",
                "label": "上位表示数",
                "description": "µ̂（年率化アルファ）上位 N 銘柄をランキング・経路チャートで返す。",
                "default": 50, "min": 1, "max": 500,
            },
            "alpha_ar1": {
                "type": "checkbox",
                "label": "AR(1) アルファ（平均回帰）",
                "description": (
                    "α をランダムウォークではなく AR(1) として推定します。"
                    "µ̂ = φ·α_T × 52 に縮小され、より保守的な期待リターン推定になります。"
                    "φ は下の「α の AR(1) 係数」スライダーで指定。"
                ),
                "default": False,
            },
            "alpha_phi": {
                "type": "slider",
                "dtype": "float",
                "label": "α の AR(1) 係数 φ（AR(1) 有効時のみ）",
                "description": (
                    "1 に近いほどランダムウォークに近い（変化が緩やか）。"
                    "0.95 → 半減期 ≈ 13.5 週、0.99 → 半減期 ≈ 69 週。"
                    "「AR(1) アルファ」を有効にした場合のみ使用されます。"
                ),
                "default": 0.95, "min": 0.50, "max": 0.999, "step": 0.005,
            },
            "auto_hyperparams": {
                "type": "checkbox",
                "label": "δ / β_v を自動選択（周辺尤度最大化）",
                "description": (
                    "銘柄横断でグリッドサーチし、正規近似周辺対数尤度を最大化する δ・β_v を自動選択します。"
                    "有効にすると上の δ・β_v スライダー指定値より優先されます（計算コストあり）。"
                ),
                "default": False,
            },
            "lambda_risk": {
                "type": "slider",
                "dtype": "float",
                "label": "リスク回避度 λ",
                "description": (
                    "U = µ̂ − λ × R_macro。λ=0 でリターン最大化、λ大でマクロリスク重視。"
                    "スライダー操作は再計算なしで即時反映（クライアント後処理）。"
                ),
                "default": 1.0, "min": 0.0, "max": 5.0, "step": 0.1,
            },
        }

    # ── マクロ週次変化の整列（forward-fill＋週次変化）────────────────────────
    def _macro_change_builder(self, macro_levels: dict, factors: list[str]):
        """date → マクロ水準ベクトル（forward-fill）を返すクロージャを作る（日付メモ化）。"""
        series = []
        for f in factors:
            scode, kind, _ = _DLM_MACRO_MAP[f]
            d, v = macro_levels.get(scode, ([], []))
            series.append((d, v, kind))
        memo: dict[str, list] = {}

        def level_at(date: str):
            cached = memo.get(date)
            if cached is not None:
                return cached
            out = []
            for d, v, _kind in series:
                idx = bisect.bisect_right(d, date)
                out.append(v[idx - 1] if idx > 0 else None)
            memo[date] = out
            return out

        kinds = [s[2] for s in series]
        return level_at, kinds

    def _build_series(self, px_rows: list, level_at, kinds: list[str]):
        """1銘柄の (y, X, used_dates) を構築。先頭列=1、欠損週はスキップ。"""
        dates = [r.trade_date for r in px_rows]
        closes = [r.close_last for r in px_rows]
        y: list[float] = []
        X: list[list[float]] = []
        used_dates: list[str] = []
        prev_lv = level_at(dates[0]) if dates else None
        for t in range(1, len(dates)):
            c0, c1 = closes[t - 1], closes[t]
            cur_lv = level_at(dates[t])
            if not (c0 and c1 and c0 > 0 and c1 > 0):
                prev_lv = cur_lv
                continue
            row = [1.0]
            ok = True
            for j, kind in enumerate(kinds):
                a, b = prev_lv[j], cur_lv[j]
                if a is None or b is None:
                    ok = False
                    break
                if kind == "logret":
                    if a <= 0 or b <= 0:
                        ok = False
                        break
                    row.append(math.log(b / a))
                else:  # diff
                    row.append(b - a)
            prev_lv = cur_lv
            if not ok:
                continue
            y.append(math.log(c1 / c0))
            X.append(row)
            used_dates.append(dates[t])
        return y, X, used_dates

    def produced_output(self, db: Any) -> bool:
        """M-3 producer μ̂（macro_dlm_scores）を共有DBに持つか（sell_ranking の graceful 判定用）。"""
        try:
            from database import get_macro_dlm_scores
            return bool(get_macro_dlm_scores(db))
        except Exception:
            return False

    def read_producer_scores(self, db: Any, macro_snapshot: dict | None = None) -> dict:
        """M-1 と同一形 {edinet_code: {mu, r_macro, r1_prime}} を返す（sell_ranking 共用）。

        mu は永続化済み macro_dlm_scores、r_macro は共有 macro_beta producer から
        マージ、r1_prime は常に None（DLM は OLS 予測SE を持たない）。"""
        from database import get_macro_dlm_scores
        from .macro_snapshots import get_producer_scores
        mus = get_macro_dlm_scores(db)
        if not mus:
            return {}
        r_macro_src = get_producer_scores(db, macro_snapshot)
        out: dict = {}
        for ec, mu in mus.items():
            prod = r_macro_src.get(ec) or {}
            out[ec] = {
                "mu":       float(mu),
                "r_macro":  prod.get("r_macro"),
                "r1_prime": None,
            }
        return out

    async def execute(self, params: dict, db: Any) -> dict:
        factors: list[str] = params["macro_features"]
        delta: float = params["state_discount"]
        beta_v: float = params["var_discount"]
        min_weeks: int = params["min_weeks"]
        burn_in: int = params["burn_in_weeks"]
        top_n: int = params["top_n"]
        use_ar1: bool = bool(params.get("alpha_ar1", False))
        phi: float = float(params.get("alpha_phi", 0.95)) if use_ar1 else 1.0
        auto_hp: bool = bool(params.get("auto_hyperparams", False))

        if not factors:
            raise ValueError("マクロ・ファクターを1つ以上選択してください")
        if not (0.0 < delta < 1.0):
            raise ValueError("状態割引係数 δ は (0, 1) の範囲で指定してください")

        prices_by_co, companies = load_prices(db)
        if not prices_by_co:
            raise ValueError("株価週次履歴がありません。先に株価を収集してください。")

        all_dates = [r.trade_date for rows in prices_by_co.values() for r in rows]
        min_date = min(all_dates) if all_dates else None
        series_codes = sorted({_DLM_MACRO_MAP[f][0] for f in factors})
        macro_levels = load_macro_levels(db, series_codes, min_date)
        if not macro_levels:
            raise ValueError("マクロデータがありません。先にマクロ指標を収集してください。")

        # ── 薄い factor の自動除外（企業母集団を factor 選択から切り離す）──────────
        # 週次日付グリッドで forward-fill 値が _MIN_FACTOR_COVERAGE 未満しか無い factor
        # （未収集・歴史が浅い系列）は多くの週で null になり、含めるとその週がスキップされ
        # 企業が一斉脱落する。これをモデルから外して企業を残し、除外を diagnostics に表示する。
        grid = sorted(set(all_dates))
        n_grid = len(grid)

        def _factor_coverage(feat: str) -> float:
            scode = _DLM_MACRO_MAP[feat][0]
            d, _v = macro_levels.get(scode, ([], []))
            if not d or not n_grid:
                return 0.0
            pos = bisect.bisect_left(grid, d[0])   # forward-fill 可能日 = grid 日付 ≥ 初出日
            return (n_grid - pos) / n_grid

        factor_coverage = {f: _factor_coverage(f) for f in factors}
        dropped_factors = [
            {"feature": f, "label": _DLM_MACRO_MAP[f][2], "coverage": _r(factor_coverage[f], 4)}
            for f in factors if factor_coverage[f] < _MIN_FACTOR_COVERAGE
        ]
        factors = [f for f in factors if factor_coverage[f] >= _MIN_FACTOR_COVERAGE]
        if not factors:
            _names = "、".join(d["label"] for d in dropped_factors)
            raise ValueError(
                f"選択したマクロ・ファクターはいずれもデータ蓄積が不足しています（{_names}）。"
                f"カバレッジ {_MIN_FACTOR_COVERAGE:.0%} 以上の系列を選ぶか、マクロ収集を実行してください。"
            )

        level_at, kinds = self._macro_change_builder(macro_levels, factors)

        # 自動ハイパーパラメータ選択: 代表サンプルで周辺尤度を最大化
        auto_hp_used = False
        if auto_hp:
            sample_ecs = sorted(prices_by_co.keys())[:_AUTO_SAMPLE_N]
            sample_data: list[tuple] = []
            for ec in sample_ecs:
                sy, sX, _ = self._build_series(prices_by_co[ec], level_at, kinds)
                if len(sy) >= min_weeks:
                    sample_data.append((sy, sX))
            if sample_data:
                delta, beta_v = _auto_select_hyperparams(sample_data, burn_in, phi=phi)
                auto_hp_used = True

        tq = stats.t.ppf(0.975, df=10_000)   # 信用区間の係数（自由度大の Student-t≒正規）
        k = len(factors)

        rows: list[dict] = []
        agg_se2: list[float] = []     # 標準化予測誤差²（校正＝平均が1なら整合）
        agg_rmse: list[float] = []
        agg_cov: list[float] = []
        oof_residuals: dict[str, list] = {}  # {YYYY-MM: [(yhat, y_true), ...]}

        for ec, px_rows in prices_by_co.items():
            if len(px_rows) < min_weeks + 1:
                continue
            y, X, used_dates = self._build_series(px_rows, level_at, kinds)
            if len(y) < min_weeks:
                continue

            res = dlm_filter(y, X, delta, beta_v, phi=phi)
            T = len(y)
            b0 = min(burn_in, max(T - 5, 0))     # バーンイン（最低5点は残す）

            m_path, sd_path = res["m_path"], res["sd_path"]
            alpha_w = float(m_path[-1, 0])
            alpha_sd = float(sd_path[-1, 0])
            # AR(1) 時は µ̂ = φ·α_T（1期先予測値を 0 へ縮小した保守的推定）
            mu = phi * alpha_w * WEEKS_PER_YEAR
            mu_lo = phi * (alpha_w - tq * alpha_sd) * WEEKS_PER_YEAR
            mu_hi = phi * (alpha_w + tq * alpha_sd) * WEEKS_PER_YEAR

            beta_latest: dict[str, dict] = {}
            beta_T = []
            for j, f in enumerate(factors):
                bm = float(m_path[-1, j + 1])
                bs = float(sd_path[-1, j + 1])
                beta_T.append(bm)
                beta_latest[f] = {"mean": _r(bm), "lo": _r(bm - tq * bs), "hi": _r(bm + tq * bs)}

            # OOF 残差収集: φ·α_{t-1|t-1}（バーンイン後）vs y_t（1期先・無リーク）
            # AR(1) 時は φ·α_T が 1期先予測値なので yhat にも phi を適用
            for t in range(b0 + 1, T):
                yhat = phi * float(m_path[t - 1, 0]) * WEEKS_PER_YEAR
                ym = used_dates[t][:7]
                oof_residuals.setdefault(ym, []).append((yhat, float(y[t])))

            # 1期先診断（バーンイン除外）
            se = res["std_errs"][b0:]
            fe = res["fe"][b0:]
            pred_rmse = float(np.sqrt(np.mean(fe ** 2))) if fe.size else None
            coverage95 = float(np.mean(np.abs(se) <= tq)) if se.size else None
            if se.size:
                agg_se2.append(float(np.mean(se ** 2)))
                agg_cov.append(coverage95)
            if pred_rmse is not None:
                agg_rmse.append(pred_rmse)

            # 系統的マクロリスク R_macro（副産物・表示の:= sqrt(βᵀ cov(Δm) β)）
            r_macro = None
            try:
                Xm = np.asarray([r[1:] for r in X], dtype=float)
                if Xm.shape[0] > k:
                    cov = np.cov(Xm, rowvar=False)
                    cov = np.atleast_2d(cov)
                    r_macro = macro_risk_exposure(beta_T, cov) * math.sqrt(WEEKS_PER_YEAR)
            except Exception:
                r_macro = None

            comp = companies.get(ec)
            rows.append({
                "edinet_code": ec,
                "sec_code": (comp.sec_code if comp else "") or "",
                "company_name": (comp.name if comp else ec) or ec,
                "industry": (comp.industry if comp else "不明") or "不明",
                "mu": _r(mu), "mu_ci": [_r(mu_lo), _r(mu_hi)],
                "alpha_weekly": _r(alpha_w),
                "n_weeks": T,
                "pred_rmse": _r(pred_rmse), "coverage95": _r(coverage95, 4),
                "r_macro": _r(r_macro),
                "beta_latest": beta_latest,
                # 経路は top_n のみ後段で付与（payload 抑制）
                "_path_src": (m_path, sd_path, used_dates, b0),
            })

        if not rows:
            raise ValueError(
                f"推定可能な銘柄がありません（最低 {min_weeks} 週・マクロ整列後）。"
                "株価週次・マクロデータの蓄積を確認してください。"
            )

        rows.sort(key=lambda r: (r["mu"] is None, -(r["mu"] or 0.0)))
        n_companies = len(rows)

        # snapshot_date = 全銘柄の used_dates[-1] の最大値（_path_src を pop する前に収集）
        _snap_dates = [r["_path_src"][2][-1] for r in rows if r.get("_path_src") and r["_path_src"][2]]
        _snap_str: str | None = max(_snap_dates) if _snap_dates else None

        # top_n のみ α/β 経路（信用区間バンド）を構築して付与
        for r in rows[:top_n]:
            m_path, sd_path, used_dates, b0 = r.pop("_path_src")
            idx = _downsample_idx(len(used_dates) - b0, _MAX_PATH_POINTS)
            sel = [b0 + i for i in idx]
            path = {
                "dates": [used_dates[i] for i in sel],
                "alpha": {
                    "mean": [_r(m_path[i, 0]) for i in sel],
                    "lo": [_r(m_path[i, 0] - tq * sd_path[i, 0]) for i in sel],
                    "hi": [_r(m_path[i, 0] + tq * sd_path[i, 0]) for i in sel],
                },
                "beta": {
                    f: {
                        "mean": [_r(m_path[i, j + 1]) for i in sel],
                        "lo": [_r(m_path[i, j + 1] - tq * sd_path[i, j + 1]) for i in sel],
                        "hi": [_r(m_path[i, j + 1] + tq * sd_path[i, j + 1]) for i in sel],
                    } for j, f in enumerate(factors)
                },
            }
            r["path"] = path
        # 経路を付けなかった残りは _path_src を破棄
        for r in rows[top_n:]:
            r.pop("_path_src", None)

        diagnostics = {
            "calibration": _r(float(np.mean(agg_se2)), 4) if agg_se2 else None,
            "pred_rmse": _r(float(np.mean(agg_rmse))) if agg_rmse else None,
            "coverage95": _r(float(np.mean(agg_cov)), 4) if agg_cov else None,
            "n_companies_scored": n_companies,
            # 実際に使用されたハイパーパラメータ（自動選択 or ユーザー指定）
            "selected_delta": _r(delta, 4),
            "selected_bv": _r(beta_v, 4),
            "phi": _r(phi, 4),
            "auto_hyperparams_used": auto_hp_used,
            # データ蓄積不足で除外した factor と、選択 factor のカバレッジ（透明性）
            "dropped_factors": dropped_factors,
            "factor_coverage": {f: _r(c, 4) for f, c in factor_coverage.items()},
        }

        # ── アウトオブサンプル検証（OOF）: α_{t-1} が翌週リターンを順序付けるか（ADR-0004）─
        from .macro_snapshots import oof_backtest as _oof_backtest
        oof_bt = _oof_backtest(oof_residuals, n_quantiles=5) if oof_residuals else {
            "n_quantiles": 5, "n_periods": 0, "n_periods_quantile": 0,
            "n_oof_samples": 0, "quantile_returns": [],
            "rank_ic": {"mean": None, "std": None, "n": 0},
            "long_short_spread": None, "hit_rate": None,
        }

        # ── producer μ̂ を永続化（sell_ranking が mu_source=macro_dlm で読む・ADR-0004）─
        from database import replace_macro_dlm_scores
        try:
            replace_macro_dlm_scores(
                db,
                [{"edinet_code": r["edinet_code"], "mu": r["mu"]}
                 for r in rows if r.get("mu") is not None],
                _snap_str,
            )
        except Exception:
            pass   # 永続化失敗（読取専用DB等）は分析表示を妨げない

        return {
            "model_type": "bayesian_dlm",
            "macro_features": factors,
            "factor_labels": {f: _DLM_MACRO_MAP[f][2] for f in factors},
            "lambda_risk": params.get("lambda_risk", 1.0),
            "params": {
                "state_discount": delta, "var_discount": beta_v,
                "min_weeks": min_weeks, "burn_in_weeks": burn_in, "top_n": top_n,
            },
            "n_companies": n_companies,
            "diagnostics": diagnostics,
            "oof_backtest": oof_bt,
            "results": rows[:top_n],
        }


plugin = MacroDlmPlugin()
