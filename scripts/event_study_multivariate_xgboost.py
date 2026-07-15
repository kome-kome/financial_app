"""決算開示イベント駆動モデル フェーズ1（Issue #323）＋ target相対化ablation（Issue #337）
＋ 断面/自己履歴特徴量×target軸マトリクス検証（Issue #336）。

フェーズ0（scripts/event_study_disclosure_surprise.py）の単一特徴量(m_pm1)による
弱い有意シグナル(rank-IC 0.031)を受け、多変量XGBoostで強化できるかを検証する。
新規データ収集なし（既存の週次株価＋feature_disclosure.pyのみ）。

Issue #337 拡張: Optiver 1位解移植②「インデックス相対target＋zero-sum後処理」の
ablation比較を同一サンプル・同一実行内で行う。結果は「弱い肯定」（Issueコメント参照・
クローズ済み）。target軸は #336 マトリクスへ統合。

Issue #336 拡張: Optiver 1位解移植①「断面相対化・自己履歴相対化特徴量」。
特徴量セット軸（F0-F3）×target軸（A0/A1）のマトリクスで検証し、
事前固定ゲート（Issue #336 コメントで実行前宣言）の自動判定を表4に出力する。

セル定義（--cells で選択、既定 F0A0,F1A0,F2A0,F3A0,F0A1,F3A1）:
  ── #336 マトリクス（特徴量セット×学習target・zero-sumなし）──
  F0A0〜F3A0  特徴量F0〜F3 × 学習target=絶対リターン（F0A0=baseline）
  F0A1〜F3A1  特徴量F0〜F3 × 学習target=マーケット相対
  ── #337 レガシーalias（F0固定・再現用）──
  A0  = F0A0 / A1 = F0A1
  A2  学習target=絶対・予測を基準週断面でdemean（zero-sum後処理単独）
  A3  A1+A2 併用
  A1a 学習target=開示月イベント断面mean比（Issue字義案・demean基準の感度検証）

特徴量セット（#336）:
  F0  既存31特徴量（#323フェーズ1）
  F1  F0＋断面相対化10個（全ユニバース週次percentile rank/断面median偏差・
      業種内rank・開示イベント基準週断面rank）
  F2  F0＋自己履歴相対化4個（52週first値比・52週平均比・expanding mean比・
      自己ボラレジーム比）
  F3  F1＋F2 全部入り

評価は2空間（各セル共通）: y_true=絶対リターン（主指標）／マーケット相対（市場中立・補助）。
スコアは常にraw予測値（相対予測へのR_mkt足し戻しは将来市場リターンのルックアヘッド
混入となるため禁止）。R_mkt が計算できないサンプルは全セルで一律除外し母集団を揃える。

統計的注意: oof_backtest の全指標は期内スコア順位のみに依存するため、予測値の
「月次断面」一律demeanでは指標は動かない。月次評価断面には複数の開示基準週が
混在するため、zero-sum後処理は基準週単位で行う（これなら月内順位が変わる）。

特徴量（F0=既存31個）:
- ファンダ（feature_disclosure.pyのスケールフリー特徴量 24個: pm/cost の r_*/f_*/m_*/d_f_*）
- サプライズの相対値2個（m_sales, d_f_salesをf_sales.shift(1)の絶対値でスケール）
- テクニカル（週次近似・#317と同型の制約下）: 開示週の直前週までのトレーリング
  リターン(4/12/26週)・ボラティリティ(12/26週)。開示自体の価格反応をリークしないよう
  「基準週の1つ前」までのデータのみ使用。

追加特徴量のリーク防止規約: 断面系（cs_*/ind_*）は「基準週の1つ前の週」の
同時点断面（全ユニバースのカレンダー整列wideフレーム）から取得＝当該週までの
情報のみ。自己履歴系（h_*）は既存テクニカルと同じ ref_idx（基準週の1つ前）まで。
開示イベント断面rank（ev_*）は同一基準週に開示した銘柄群内の順位＝同時点情報のみ。
新特徴量は欠損時NaN（XGBoost native対応）とし、サンプル母集団はF0と完全同一に保つ。

ラベル: フェーズ0と同じ基準週からのNw週後フォワードリターン（既定4週）。

評価: plugins/utils.py::walk_forward_cv_monthly（月次ウォークフォワード・
fit_predictにXGBoost注入）→ plugins/macro_snapshots.py::oof_backtest
（rank-IC・quantile spread・hit_rate）。M-1/M-2/M-3と同一の評価関数を再利用。

実行: python -m scripts.event_study_multivariate_xgboost
        [--forward-weeks 4] [--limit 0] [--cells F0A0,F1A0,F2A0,F3A0,F0A1,F3A1]
        [--trim 0.01] [--min-names 100] [--min-week-group 1] [--min-cs-group 5]
"""
import argparse
import math
import statistics
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

# Windows既定コンソール(cp932)は一部記号(✓等)を符号化できず即クラッシュするため、
# 標準出力をUTF-8へ強制（未知記号はreplaceでフォールバックし実行を継続させる）
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from database import Company, SessionLocal, StatementDisclosure, StockPriceWeekly  # noqa: E402
from feature_disclosure import build_disclosure_features  # noqa: E402
from plugins.utils import walk_forward_cv_monthly  # noqa: E402
from plugins.macro_gbdt import _make_xgb_fit_predict  # noqa: E402
from plugins.macro_snapshots import oof_backtest, _spearman  # noqa: E402

FEATURE_NAMES = [
    "r_pm1", "r_pm2", "r_pm3", "f_pm1", "f_pm2", "f_pm3",
    "r_cost1", "r_cost2", "r_cost3", "f_cost1", "f_cost2", "f_cost3",
    "m_pm1", "m_pm2", "m_pm3", "m_cost1", "m_cost2", "m_cost3",
    "d_f_pm1", "d_f_pm2", "d_f_pm3", "d_f_cost1", "d_f_cost2", "d_f_cost3",
    "m_sales_rel", "d_f_sales_rel",
    "ret_4w", "ret_12w", "ret_26w", "vol_12w", "vol_26w",
]

# 断面相対化特徴量（F1・#336改善案1/2）。cs_*/ind_* は基準週の1つ前週の
# 全ユニバース断面（開示銘柄に限らない）、ev_* は同一基準週の開示イベント断面。
CS_FEATURE_NAMES = [
    "cs_rank_ret_4w", "cs_rank_ret_12w", "cs_rank_ret_26w",  # 全ユニバースpercentile rank
    "cs_dev_ret_12w",                                         # ret_12w − 断面median（mean比はリターン≈0で発散するため偏差）
    "cs_rank_vol_26w", "cs_ratio_vol_12w",                    # ボラのrank／断面median比（volは正値なので比が安定）
    "ind_rank_ret_12w", "ind_rank_ret_26w",                   # 業種（companies.industry・EDINET分類）内rank
    "ev_rank_m_pm1", "ev_rank_m_sales_rel",                   # 開示イベント基準週断面のサプライズrank
]
# 自己履歴相対化特徴量（F2・#336改善案3）。Optiver解のstock/day内first値比・
# expanding mean比の週次アナロジー。全て「基準週の1つ前」まで（リーク防止）。
HIST_FEATURE_NAMES = [
    "h_ret_52w",          # 52週前終値比 −1（52週窓first値比）
    "h_px_vs_52w_mean",   # 終値 / 直近52週平均 −1
    "h_px_vs_expmean",    # 終値 / 全履歴expanding mean −1
    "h_vol_ratio_12_52",  # vol12w / vol52w（自己ボラレジーム比）
]
ALL_FEATURE_NAMES = FEATURE_NAMES + CS_FEATURE_NAMES + HIST_FEATURE_NAMES

FEATURE_SETS = {
    "F0": list(FEATURE_NAMES),
    "F1": FEATURE_NAMES + CS_FEATURE_NAMES,
    "F2": FEATURE_NAMES + HIST_FEATURE_NAMES,
    "F3": list(ALL_FEATURE_NAMES),
}
FEATURE_IDX = {fs: [ALL_FEATURE_NAMES.index(n) for n in names] for fs, names in FEATURE_SETS.items()}

# セル定義: cell -> (特徴量セット, 学習target列, zero-sum後処理の有無)
CELLS = {
    # ── #336 マトリクス ──
    "F0A0": ("F0", "fwd_abs", False),
    "F1A0": ("F1", "fwd_abs", False),
    "F2A0": ("F2", "fwd_abs", False),
    "F3A0": ("F3", "fwd_abs", False),
    "F0A1": ("F0", "fwd_rel_mkt", False),
    "F1A1": ("F1", "fwd_rel_mkt", False),
    "F2A1": ("F2", "fwd_rel_mkt", False),
    "F3A1": ("F3", "fwd_rel_mkt", False),
    # ── #337 レガシーalias（F0固定・過去コマンド再現用）──
    "A0":  ("F0", "fwd_abs", False),
    "A1":  ("F0", "fwd_rel_mkt", False),
    "A2":  ("F0", "fwd_abs", True),
    "A3":  ("F0", "fwd_rel_mkt", True),
    "A1a": ("F0", "fwd_rel_month", False),
}
TARGET_LABELS = {"fwd_abs": "絶対", "fwd_rel_mkt": "相対(mkt)", "fwd_rel_month": "相対(開示月)"}
TARGET_ORDER = list(TARGET_LABELS)
EVAL_SPACES = ("fwd_abs", "fwd_rel_mkt")

# 事前固定ゲート（Issue #336 コメント 2026-07-15 で実行前宣言・事後変更禁止）
GATE_MIN_MEAN_DIFF = 0.005   # 条件1: paired rank-IC mean_diff 下限
GATE_MIN_WIN_RATE = 0.60     # 条件1: paired win_rate 下限
# 条件2: spread悪化なし かつ hit_rate低下 ≤ 1フォールド分（1/n_folds・#337の粒度問題対応）
# 条件3: 年別Δが過半数の年で非負・最終年悪化なし
# 条件4: 特徴量効果がA0/A1両target軸で同符号


def _load_disclosures(db) -> dict[str, list[dict]]:
    cols = [c.name for c in StatementDisclosure.__table__.columns]
    rows = db.query(StatementDisclosure).all()
    by_company = defaultdict(list)
    for r in rows:
        by_company[r.edinet_code].append({c: getattr(r, c) for c in cols})
    return by_company


def _load_weekly_prices(db) -> dict[str, pd.DataFrame]:
    rows = db.query(
        StockPriceWeekly.edinet_code, StockPriceWeekly.week_start, StockPriceWeekly.close_last
    ).all()
    by_company = defaultdict(list)
    for edinet_code, week_start, close_last in rows:
        by_company[edinet_code].append((week_start, close_last))
    out = {}
    for edinet_code, pairs in by_company.items():
        df = pd.DataFrame(pairs, columns=["week_start", "close_last"]).sort_values("week_start")
        out[edinet_code] = df.reset_index(drop=True)
    return out


def _technical_features(prices: pd.DataFrame, disc_date: str) -> tuple[dict, str] | None:
    """開示基準週の直前週までの情報のみでトレーリング＋自己履歴特徴量を計算（リーク防止）。

    Returns: (features, ref_week) or None。
    母集団ガードは従来どおり ref_idx < 26 のみ（#336の追加特徴量 h_* は履歴不足時 NaN と
    し、サンプル除外はしない＝全セルで母集団同一）。ref_week は断面特徴量ルックアップ用の
    「基準週の1つ前」の week_start 実値。
    """
    idx = prices["week_start"].searchsorted(disc_date, side="left")
    ref_idx = idx - 1
    if ref_idx < 26:
        return None
    closes = prices["close_last"]
    ref_close = closes.iloc[ref_idx]
    if not ref_close or ref_close <= 0:
        return None

    def _ret(n_weeks):
        if ref_idx - n_weeks < 0:
            return np.nan
        base = closes.iloc[ref_idx - n_weeks]
        if not base or base <= 0:
            return np.nan
        return ref_close / base - 1.0

    def _vol(n_weeks):
        if ref_idx - n_weeks < 0:
            return np.nan
        window = closes.iloc[ref_idx - n_weeks:ref_idx + 1]
        rets = window.pct_change().dropna()
        return rets.std() if len(rets) > 2 else np.nan

    # 自己履歴相対化（F2）。52週窓が取れない場合は NaN（母集団は変えない）
    def _px_vs_mean(n_weeks):
        if ref_idx - n_weeks + 1 < 0:
            return np.nan
        window = closes.iloc[ref_idx - n_weeks + 1:ref_idx + 1]
        m = window.mean()
        return ref_close / m - 1.0 if m and m > 0 else np.nan

    exp_mean = closes.iloc[:ref_idx + 1].mean()
    vol12, vol52 = _vol(12), _vol(52)
    hist = {
        "h_ret_52w": _ret(52),
        "h_px_vs_52w_mean": _px_vs_mean(52),
        "h_px_vs_expmean": ref_close / exp_mean - 1.0 if exp_mean and exp_mean > 0 else np.nan,
        "h_vol_ratio_12_52": (vol12 / vol52) if (vol12 and vol52 and not math.isnan(vol12)
                                                 and not math.isnan(vol52) and vol52 > 0) else np.nan,
    }
    feats = {
        "ret_4w": _ret(4), "ret_12w": _ret(12), "ret_26w": _ret(26),
        "vol_12w": vol12, "vol_26w": _vol(26),
        **hist,
    }
    return feats, prices["week_start"].iloc[ref_idx]


def _forward_return(prices: pd.DataFrame, disc_date: str, forward_weeks: int):
    """(fwd, base_week, end_week) を返す。

    base/end_week は searchsorted で実際に採用された行の week_start 値。
    開示日は大半が月曜以外＝カレンダー導出(iso_week_start)ではキーがずれるため、
    必ずこの実値を R_mkt ルックアップに使う。idx+forward_weeks は銘柄series内の
    位置オフセットであり、欠損週を持つ銘柄では実カレンダー窓が forward_weeks 週を
    超える（だからこそ end_week 実値でインデックス比を取れば窓が完全一致する）。
    """
    idx = prices["week_start"].searchsorted(disc_date, side="left")
    if idx >= len(prices) or idx + forward_weeks >= len(prices):
        return np.nan, None, None
    base = prices["close_last"].iloc[idx]
    fwd = prices["close_last"].iloc[idx + forward_weeks]
    if not base or not fwd or base <= 0:
        return np.nan, None, None
    return (
        fwd / base - 1.0,
        prices["week_start"].iloc[idx],
        prices["week_start"].iloc[idx + forward_weeks],
    )


def _trimmed_mean(values: np.ndarray, trim: float) -> float:
    vals = np.sort(values)
    k = int(len(vals) * trim)
    if k > 0:
        vals = vals[k:len(vals) - k]
    return float(vals.mean())


def _build_wide(prices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """全ユニバースのカレンダー整列 wide フレーム（index=week_start, columns=edinet_code）。

    マーケットインデックス（#337）と断面特徴量フレーム（#336）が共有する。
    """
    series_map = {code: df.set_index("week_start")["close_last"] for code, df in prices.items()}
    return pd.DataFrame(series_map).sort_index()


def _build_market_index(wide: pd.DataFrame, trim: float, min_names: int):
    """全ユニバース等加重チェーン式マーケットインデックスレベル I(w) を構築。

    週次リターン断面を両端trim平均（仕手株的な週次±数十%の実急変による汚染を抑制・
    Issue #324 で実在確認済み）で集約し累積。有効銘柄数 < min_names の週は NaN
    （その週を base/end に持つサンプルは除外される）。NaN週を跨ぐ区間は市場変動0として
    ブリッジし警告印字（期待値=0週。>0なら要調査）。
    Returns: (index_level: pd.Series, diag: dict)
    """
    rets = wide.pct_change(fill_method=None)

    trimmed_list, raw_list, median_list, counts = [], [], [], []
    extreme_events = []  # (week, edinet_code, pct_change) for |r|>10（データティック異常検知用）
    for w, row in rets.iterrows():
        vals = row.to_numpy(dtype=float)
        finite_mask = ~np.isnan(vals)
        vals = vals[finite_mask]
        counts.append(len(vals))
        outliers = row[row.abs() > 10].dropna()
        for code, r in outliers.items():
            extreme_events.append((w, code, float(r)))
        if len(vals) < min_names:
            trimmed_list.append(np.nan)
            raw_list.append(np.nan)
            median_list.append(np.nan)
        else:
            trimmed_list.append(_trimmed_mean(vals, trim))
            raw_list.append(float(vals.mean()))
            median_list.append(float(np.median(vals)))
    weekly = pd.Series(trimmed_list, index=rets.index)
    raw_weekly = pd.Series(raw_list, index=rets.index)
    median_weekly = pd.Series(median_list, index=rets.index)

    idx_weeks = list(weekly.index)
    level = {}
    cur = None
    interior_nan_weeks = []
    for i, w in enumerate(idx_weeks):
        r = weekly.iloc[i]
        if cur is None:
            if not math.isnan(r):
                if i > 0:
                    level[idx_weeks[i - 1]] = 1.0  # 最初の有効リターンの前週をアンカー
                cur = 1.0 + r
                level[w] = cur
        else:
            if math.isnan(r):
                interior_nan_weeks.append(w)
                level[w] = np.nan  # この週自体を base/end に持つサンプルは参照不可
            else:
                cur *= 1.0 + r
                level[w] = cur
    index_level = pd.Series(level).reindex(idx_weeks)

    # trim1pct vs raw の比較は Pearson相関だとティック異常1件（例: 1978万%）がraw側を
    # 支配し相関が意味を失う（外れ値スケールの暴力）。ロバスト同士(trim vs median)の
    # 最大乖離で「trimがまだ汚染されていないか」を診断する方が適切。
    both_tm = pd.DataFrame({"t": weekly, "m": median_weekly}).dropna()
    trim_vs_median_maxdiff = round(float((both_tm["t"] - both_tm["m"]).abs().max()), 6) if len(both_tm) else None
    raw_maxdiff = round(float((weekly - raw_weekly).dropna().abs().max()), 2) if weekly.notna().any() else None
    diag = {
        "n_weeks": len(idx_weeks),
        "first_week": idx_weeks[0] if idx_weeks else None,
        "last_week": idx_weeks[-1] if idx_weeks else None,
        "n_valid_weeks": int(weekly.notna().sum()),
        "interior_nan_weeks": interior_nan_weeks,
        "names_median": int(np.median(counts)) if counts else 0,
        "names_min_valid": int(min((c for c in counts if c >= min_names), default=0)),
        "trim_vs_median_maxdiff": trim_vs_median_maxdiff,
        "raw_maxdiff": raw_maxdiff,  # rawがtrimからどれだけ暴れうるか（外れ値混入の証拠・参考値）
        "n_extreme_events": len(extreme_events),
        "extreme_events_sample": sorted(extreme_events, key=lambda e: -abs(e[2]))[:5],
    }
    return index_level, diag


def _market_return(index_level: pd.Series, base_week: str, end_week: str) -> float:
    base = index_level.get(base_week, np.nan)
    end = index_level.get(end_week, np.nan)
    if base is None or end is None or math.isnan(base) or math.isnan(end) or base <= 0:
        return np.nan
    return end / base - 1.0


def _mask_thin_weeks(df: pd.DataFrame, min_names: int) -> pd.DataFrame:
    """有効銘柄数 < min_names の週の断面統計を NaN 化（薄い断面のrank/中央値は信頼不可）。"""
    thin = df.notna().sum(axis=1) < min_names
    if thin.any():
        df = df.copy()
        df.loc[thin] = np.nan
    return df


def _build_universe_cs_frames(wide: pd.DataFrame, industry_map: dict[str, str],
                              min_names: int, min_ind_names: int = 5):
    """全ユニバース断面特徴量フレーム（F1のcs_*/ind_*・#336改善案1/2）。

    各フレームは index=week_start, columns=edinet_code。値は「その週までの情報のみ」で
    計算されるため、サンプル側は ref_week（基準週の1つ前）の行を引けばリーク無し。
    業種内rankは同週×同業種の銘柄数 < min_ind_names のとき NaN。
    Returns: (frames: dict[name -> DataFrame], diag: dict)
    """
    rets = wide.pct_change(fill_method=None)
    ret4 = wide / wide.shift(4) - 1
    ret12 = wide / wide.shift(12) - 1
    ret26 = wide / wide.shift(26) - 1
    vol12 = rets.rolling(12).std()
    vol26 = rets.rolling(26).std()

    frames = {
        "cs_rank_ret_4w": ret4.rank(axis=1, pct=True),
        "cs_rank_ret_12w": ret12.rank(axis=1, pct=True),
        "cs_rank_ret_26w": ret26.rank(axis=1, pct=True),
        "cs_dev_ret_12w": ret12.sub(ret12.median(axis=1), axis=0),
        "cs_rank_vol_26w": vol26.rank(axis=1, pct=True),
        "cs_ratio_vol_12w": vol12.div(vol12.median(axis=1), axis=0).replace([np.inf, -np.inf], np.nan),
    }
    frames = {k: _mask_thin_weeks(v, min_names) for k, v in frames.items()}

    # 業種内rank（companies.industry・小グループはNaNフォールバック）
    ind_cols = defaultdict(list)
    for code in wide.columns:
        ind = industry_map.get(code)
        if ind:
            ind_cols[ind].append(code)
    ind_rank12 = pd.DataFrame(np.nan, index=wide.index, columns=wide.columns)
    ind_rank26 = pd.DataFrame(np.nan, index=wide.index, columns=wide.columns)
    n_ind_used = 0
    for _, cols in ind_cols.items():
        if len(cols) < min_ind_names:
            continue
        n_ind_used += 1
        r12 = ret12[cols].rank(axis=1, pct=True)
        r26 = ret26[cols].rank(axis=1, pct=True)
        # 同週×同業種の有効銘柄数 < min_ind_names の行は NaN
        thin12 = r12.notna().sum(axis=1) < min_ind_names
        thin26 = r26.notna().sum(axis=1) < min_ind_names
        r12.loc[thin12] = np.nan
        r26.loc[thin26] = np.nan
        ind_rank12[cols] = r12
        ind_rank26[cols] = r26
    frames["ind_rank_ret_12w"] = ind_rank12
    frames["ind_rank_ret_26w"] = ind_rank26

    diag = {
        "n_industries_total": len(ind_cols),
        "n_industries_used": n_ind_used,
        "n_codes_no_industry": sum(1 for c in wide.columns if not industry_map.get(c)),
    }
    return frames, diag


def _cs_lookup(frames: dict, ref_week: str, code: str) -> dict:
    """断面フレームから (ref_week, code) の値を引く。欠損は NaN。"""
    out = {}
    for name, df in frames.items():
        try:
            v = df.at[ref_week, code]
        except KeyError:
            v = np.nan
        out[name] = float(v) if v is not None and not pd.isna(v) else np.nan
    return out


def _fill_event_cs_ranks(samples_by_ym: dict, meta_by_ym: dict, min_cs_group: int):
    """開示イベント基準週断面のサプライズrank（ev_*）を第2パスで充填。

    断面 = 同一基準週（base_week）に開示した銘柄群（ym跨ぎも同一断面として扱う）。
    グループ有効数 < min_cs_group は NaN（薄い断面の順位は情報にならない）。
    """
    src_idx = {
        "ev_rank_m_pm1": ALL_FEATURE_NAMES.index("m_pm1"),
        "ev_rank_m_sales_rel": ALL_FEATURE_NAMES.index("m_sales_rel"),
    }
    dst_idx = {name: ALL_FEATURE_NAMES.index(name) for name in src_idx}

    groups = defaultdict(list)  # base_week -> [(ym, i), ...]
    for ym, metas in meta_by_ym.items():
        for i, m in enumerate(metas):
            groups[m["base_week"]].append((ym, i))

    for members in groups.values():
        for ev_name, s_idx in src_idx.items():
            vals = pd.Series([samples_by_ym[ym][i][0][s_idx] for ym, i in members], dtype=float)
            ranks = vals.rank(pct=True) if vals.notna().sum() >= min_cs_group else None
            d_idx = dst_idx[ev_name]
            for j, (ym, i) in enumerate(members):
                if ranks is not None and not pd.isna(ranks.iloc[j]):
                    samples_by_ym[ym][i][0][d_idx] = float(ranks.iloc[j])


def build_samples(forward_weeks: int, limit: int, trim: float, min_names: int,
                  min_cs_group: int):
    db = SessionLocal()
    print("開示データ取得中...")
    disclosures = _load_disclosures(db)
    print(f"  {len(disclosures)} 社")

    print("週次株価取得中...")
    prices = _load_weekly_prices(db)
    print(f"  {len(prices)} 社")

    industry_map = {code: ind for code, ind in db.query(Company.edinet_code, Company.industry)}

    wide = _build_wide(prices)
    print("マーケットインデックス構築中（全ユニバース・--limit非適用）...")
    index_level, idx_diag = _build_market_index(wide, trim, min_names)
    print(f"  週数: {idx_diag['n_weeks']}（有効 {idx_diag['n_valid_weeks']}）"
          f" {idx_diag['first_week']}〜{idx_diag['last_week']}")
    print(f"  週あたり有効銘柄数: median={idx_diag['names_median']}")
    print(f"  トリム平均のロバスト性: trim_vs_median max|diff|={idx_diag['trim_vs_median_maxdiff']}"
          f"（rawはティック異常混入でmax|diff|={idx_diag['raw_maxdiff']}まで暴れうる・trimで除去済み）")
    if idx_diag["n_extreme_events"]:
        print(f"  参考: |pct_change|>10 の個別銘柄イベント {idx_diag['n_extreme_events']}件"
              f"（trimで自動除去・データ修正対象ではない）上位: {idx_diag['extreme_events_sample']}")
    if idx_diag["interior_nan_weeks"]:
        print(f"  [WARN] 内部NaN週 {len(idx_diag['interior_nan_weeks'])} 件"
              f"（市場変動0ブリッジ・要調査）: {idx_diag['interior_nan_weeks'][:10]}")

    print("断面特徴量フレーム構築中（全ユニバース・--limit非適用）...")
    cs_frames, cs_diag = _build_universe_cs_frames(wide, industry_map, min_names)
    print(f"  業種数: {cs_diag['n_industries_used']}/{cs_diag['n_industries_total']}"
          f"（>=5社のみ使用）業種不明銘柄: {cs_diag['n_codes_no_industry']}")

    samples_by_ym: dict[str, list[tuple]] = defaultdict(list)
    meta_by_ym: dict[str, list[dict]] = defaultdict(list)
    n_events = 0
    n_companies = 0
    n_excluded_rmkt = 0
    for edinet_code, rows in disclosures.items():
        if limit and n_companies >= limit:
            break
        if edinet_code not in prices:
            continue
        feats = build_disclosure_features(rows)
        if feats.empty:
            continue
        n_companies += 1
        price_df = prices[edinet_code]

        for _, row in feats.iterrows():
            fwd, base_week, end_week = _forward_return(price_df, row["disc_date"], forward_weeks)
            if pd.isna(fwd):
                continue
            tech_res = _technical_features(price_df, row["disc_date"])
            if tech_res is None:
                continue
            tech, ref_week = tech_res

            f_sales_prev_abs = abs(row["f_sales"]) if pd.notna(row["f_sales"]) and row["f_sales"] != 0 else np.nan
            m_sales_rel = row["m_sales"] / f_sales_prev_abs if pd.notna(f_sales_prev_abs) else np.nan
            d_f_sales_rel = row["d_f_sales"] / f_sales_prev_abs if pd.notna(f_sales_prev_abs) else np.nan

            feat_vals = [row.get(c, np.nan) for c in FEATURE_NAMES[:24]]
            feat_vals += [m_sales_rel, d_f_sales_rel]
            feat_vals += [tech["ret_4w"], tech["ret_12w"], tech["ret_26w"], tech["vol_12w"], tech["vol_26w"]]
            feat_vals = [float(v) if v is not None and not (isinstance(v, float) and math.isnan(v)) else np.nan
                         for v in feat_vals]
            # 母集団ガードは F0（既存31特徴量）のみで判定＝#337 と母集団を同一に保つ
            if all(math.isnan(v) for v in feat_vals):
                continue

            # 断面特徴量（cs_*/ind_*）: ref_week の全ユニバース断面から取得。ev_* は
            # 第2パス（_fill_event_cs_ranks）で充填するためここでは NaN プレースホルダ
            cs = _cs_lookup(cs_frames, ref_week, edinet_code)
            for name in CS_FEATURE_NAMES:
                feat_vals.append(cs.get(name, np.nan))
            for name in HIST_FEATURE_NAMES:
                v = tech.get(name, np.nan)
                feat_vals.append(float(v) if v is not None and not (isinstance(v, float) and math.isnan(v))
                                 else np.nan)

            # R_mkt 欠損サンプルは全セル一律除外（セル間で母集団を構造的に同一化）
            r_mkt = _market_return(index_level, base_week, end_week)
            if math.isnan(r_mkt):
                n_excluded_rmkt += 1
                continue

            ym = row["disc_date"][:7]
            samples_by_ym[ym].append((feat_vals, fwd))
            meta_by_ym[ym].append({
                "edinet_code": edinet_code,
                "disc_date": row["disc_date"],
                "base_week": base_week,
                "end_week": end_week,
                "fwd_abs": fwd,
                "r_mkt": r_mkt,
                "fwd_rel_mkt": fwd - r_mkt,
            })
            n_events += 1

    # 第2パス: 開示月イベント断面mean比（A1a・Issue字義案）。月断面が揃ってから計算
    for ym, metas in meta_by_ym.items():
        month_mean = sum(m["fwd_abs"] for m in metas) / len(metas)
        for m in metas:
            m["fwd_rel_month"] = m["fwd_abs"] - month_mean

    # 第2パス: 開示イベント基準週断面のサプライズrank（ev_*・#336）
    _fill_event_cs_ranks(samples_by_ym, meta_by_ym, min_cs_group)

    print(f"\n対象企業数: {n_companies}")
    print(f"有効サンプル数: {n_events}（R_mkt欠損除外: {n_excluded_rmkt}）")
    print(f"対象月数: {len(samples_by_ym)}")

    # 追加特徴量カバレッジ（NaN率が異常に高い特徴量は実装ミスの兆候）
    if n_events:
        print("追加特徴量カバレッジ（非NaN率）:")
        new_names = CS_FEATURE_NAMES + HIST_FEATURE_NAMES
        name_idx = [(name, ALL_FEATURE_NAMES.index(name)) for name in new_names]
        counts = {name: 0 for name in new_names}
        for samples in samples_by_ym.values():
            for feat, _ in samples:
                for name, j in name_idx:
                    v = feat[j]
                    if not (isinstance(v, float) and math.isnan(v)):
                        counts[name] += 1
        for name in new_names:
            print(f"  {name:<22} {counts[name] / n_events:.3f}")
    return samples_by_ym, meta_by_ym, idx_diag


def _derive_cell_samples(samples_by_ym: dict, meta_by_ym: dict, target_key: str,
                         feat_idx: list[int]) -> dict:
    """特徴量スライス＋学習target差し替えの samples dict を派生（順序・キー完全保存）。"""
    out = {}
    for ym, samples in samples_by_ym.items():
        metas = meta_by_ym[ym]
        out[ym] = [([feat[j] for j in feat_idx], metas[i][target_key])
                   for i, (feat, _) in enumerate(samples)]
    return out


def _wrap_counting(inner, error_log: list):
    """fit_predict の例外を記録して re-raise（utils側のsilent skipを可視化）。"""
    def wrapped(train_samples, test_samples):
        try:
            return inner(train_samples, test_samples)
        except Exception as e:
            error_log.append(repr(e))
            raise
    return wrapped


def _postprocess_zero_sum(yhats: list, base_weeks: list, min_group: int) -> list:
    """基準週別demean（zero-sum後処理）。min_group未満のグループは無調整。

    n=1グループのスコア0化は「同週の対抗銘柄なし=ニュートラル」として意味論的に正当
    （既定 min_group=1 で全グループdemean）。
    """
    groups = defaultdict(list)
    for i, w in enumerate(base_weeks):
        groups[w].append(i)
    out = list(yhats)
    for idxs in groups.values():
        if len(idxs) < min_group:
            continue
        gmean = sum(yhats[i] for i in idxs) / len(idxs)
        for i in idxs:
            out[i] = yhats[i] - gmean
    return out


def _build_eval_pairs(residuals_by_ym: dict, meta_by_ym: dict, train_key: str,
                      zero_sum: bool, y_space: str, min_group: int) -> dict:
    """oof_backtest 用 {ym: [(score, y_true), ...]} を構築（同順突合の一元化）。

    score は常に raw yhat（zero_sum時は基準週demean後）。相対予測への R_mkt 足し戻しは
    将来市場リターンのルックアヘッド混入となるため行わない。y_true は meta 側から取得
    （学習空間の y と評価空間の混同事故防止・冒頭数件を突合assert）。
    """
    pairs_by_ym = {}
    for ym, residuals in residuals_by_ym.items():
        metas = meta_by_ym[ym]
        if len(residuals) != len(metas):
            raise RuntimeError(f"residuals/meta 長さ不一致 ym={ym}: {len(residuals)} vs {len(metas)}")
        for (_, y_t), meta in zip(residuals[:3], metas[:3]):
            if abs(y_t - meta[train_key]) > 1e-9:
                raise RuntimeError(f"同順突合検証失敗 ym={ym}: y_true={y_t} meta[{train_key}]={meta[train_key]}")
        yhats = [p[0] for p in residuals]
        if zero_sum:
            yhats = _postprocess_zero_sum(yhats, [m["base_week"] for m in metas], min_group)
        pairs_by_ym[ym] = [(yhats[i], metas[i][y_space]) for i in range(len(yhats))]
    return pairs_by_ym


def _per_period_ics(pairs_by_ym: dict) -> dict:
    """{ym: rank-IC or None}（paired比較・年別集計用）。"""
    out = {}
    for ym, pairs in sorted(pairs_by_ym.items()):
        out[ym] = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
    return out


def _paired_stats(ics_a: dict, ics_b: dict):
    """月次 paired IC差分（A−B）の平均・勝率・Wilcoxon p。"""
    common = [ym for ym in sorted(ics_a) if ym in ics_b
              and ics_a[ym] is not None and ics_b[ym] is not None]
    diffs = [ics_a[ym] - ics_b[ym] for ym in common]
    if not diffs:
        return {"n": 0, "mean_diff": None, "win_rate": None, "wilcoxon_p": None}
    p_value = None
    try:
        from scipy.stats import wilcoxon
        if any(d != 0 for d in diffs):
            _, p_value = wilcoxon(diffs)
            p_value = round(float(p_value), 4)
    except Exception:
        p_value = None
    return {
        "n": len(diffs),
        "mean_diff": round(statistics.mean(diffs), 4),
        "win_rate": round(sum(1 for d in diffs if d > 0) / len(diffs), 3),
        "wilcoxon_p": p_value,
    }


def _base_week_group_stats(meta_by_ym: dict) -> dict:
    """(ym, base_week) グループサイズ分布（zero-sum設計判断の実証データ）。"""
    sizes = []
    for metas in meta_by_ym.values():
        counter = defaultdict(int)
        for m in metas:
            counter[m["base_week"]] += 1
        sizes.extend(counter.values())
    if not sizes:
        return {}
    sizes_np = np.array(sizes)
    total_samples = int(sizes_np.sum())
    return {
        "n_groups": len(sizes),
        "min": int(sizes_np.min()), "p25": int(np.percentile(sizes_np, 25)),
        "median": int(np.median(sizes_np)), "p75": int(np.percentile(sizes_np, 75)),
        "max": int(sizes_np.max()),
        "share_groups_n1": round(float((sizes_np == 1).mean()), 3),
        "share_samples_in_ge3": round(float(sizes_np[sizes_np >= 3].sum() / total_samples), 3),
    }


def _yearly_mean_ics(ics: dict) -> dict:
    """{year: 年平均rank-IC}（Noneは除外）。"""
    by_year = defaultdict(list)
    for ym, ic in ics.items():
        if ic is not None:
            by_year[ym[:4]].append(ic)
    return {y: statistics.mean(v) for y, v in by_year.items() if v}


def _find_cell(cells: list[str], fs: str, target_key: str) -> str | None:
    """実行セル一覧から (特徴量セット, target, zero-sumなし) に該当するセル名を返す。"""
    for c in cells:
        f, t, z = CELLS[c]
        if f == fs and t == target_key and not z:
            return c
    return None


def _gate_report(cells: list[str], results: dict, ics_abs: dict,
                 paired_cache: dict, base_cell: str):
    """事前固定ゲート（Issue #336 コメント 2026-07-15 宣言・4条件）の自動判定。

    条件（すべて base_cell=F0A0 比・評価y=絶対リターン空間）:
      ① paired rank-IC mean_diff ≥ +0.005 かつ win_rate ≥ 60%
      ② spread悪化なし かつ hit_rate低下 ≤ 1フォールド分（1/n_folds・#337粒度問題対応）
      ③ 年別Δが過半数の年で非負 かつ 最終年悪化なし
      ④ 特徴量効果がA0/A1両target軸で同符号（該当セルが無い場合 n/a）
    総合: 4条件すべて✓=PASS（昇格ゲート通過）／①のみ✓=弱い肯定／それ以外=改善なし
    """
    r_base = results[(base_cell, "fwd_abs")]
    n_folds = r_base["rank_ic"]["n"]
    hit_tol = 1.0 / n_folds + 1e-9
    print(f"\n=== 表4: 事前固定ゲート判定（vs {base_cell}・評価y=絶対・"
          f"hit許容低下=1フォールド分={round(hit_tol, 4)}）===")

    def _mark(v):
        # Windows既定コンソール(cp932)は✓/✗を符号化不可のためASCII表記固定
        return "OK" if v else ("n/a" if v is None else "NG")

    for cell in cells:
        if cell == base_cell:
            continue
        s = paired_cache.get((cell, base_cell)) or _paired_stats(ics_abs[cell], ics_abs[base_cell])
        cond1 = (s["mean_diff"] is not None and s["mean_diff"] >= GATE_MIN_MEAN_DIFF
                 and s["win_rate"] is not None and s["win_rate"] >= GATE_MIN_WIN_RATE)

        r_c = results[(cell, "fwd_abs")]
        spread_ok = r_c["long_short_spread"] >= r_base["long_short_spread"]
        hit_drop = r_base["hit_rate"] - r_c["hit_rate"]
        cond2 = spread_ok and hit_drop <= hit_tol

        y_c = _yearly_mean_ics(ics_abs[cell])
        y_b = _yearly_mean_ics(ics_abs[base_cell])
        common_years = sorted(set(y_c) & set(y_b))
        diffs = {y: y_c[y] - y_b[y] for y in common_years}
        cond3 = bool(common_years) and \
            sum(1 for d in diffs.values() if d >= 0) * 2 >= len(diffs) and \
            diffs[common_years[-1]] >= 0

        fs = CELLS[cell][0]
        cond4 = None
        if fs != "F0":
            c_a0 = _find_cell(cells, fs, "fwd_abs")
            c_a1 = _find_cell(cells, fs, "fwd_rel_mkt")
            b_a0 = _find_cell(cells, "F0", "fwd_abs")
            b_a1 = _find_cell(cells, "F0", "fwd_rel_mkt")
            if all([c_a0, c_a1, b_a0, b_a1]):
                eff_a0 = results[(c_a0, "fwd_abs")]["rank_ic"]["mean"] - results[(b_a0, "fwd_abs")]["rank_ic"]["mean"]
                eff_a1 = results[(c_a1, "fwd_abs")]["rank_ic"]["mean"] - results[(b_a1, "fwd_abs")]["rank_ic"]["mean"]
                cond4 = (eff_a0 > 0) == (eff_a1 > 0)

        if cond1 and cond2 and cond3 and cond4:
            verdict = "PASS（昇格ゲート通過）"
        elif cond1:
            verdict = "弱い肯定（昇格見送り）"
        else:
            verdict = "改善なし"
        print(f"{cell:<6} (1){_mark(cond1):<3}(diff={s['mean_diff']}/win={s['win_rate']}) "
              f"(2){_mark(cond2):<3}(spread {r_base['long_short_spread']}->{r_c['long_short_spread']}"
              f"/hit低下={round(hit_drop, 4)}) "
              f"(3){_mark(cond3):<3}({ {y: round(d, 4) for y, d in diffs.items()} }) "
              f"(4){_mark(cond4):<3} -> {verdict}")


def run(forward_weeks: int, limit: int, cells: list[str], trim: float,
        min_names: int, min_week_group: int, min_cs_group: int):
    unknown = [c for c in cells if c not in CELLS]
    if unknown:
        print(f"未知のセル: {unknown}（有効: {list(CELLS)}）")
        sys.exit(2)

    samples_by_ym, meta_by_ym, _ = build_samples(forward_weeks, limit, trim, min_names,
                                                 min_cs_group)
    if sum(len(v) for v in samples_by_ym.values()) < 100:
        print("サンプル不足のため評価をスキップ")
        return

    xgb_params = {
        "max_depth": 3,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "reg_lambda": 1.0,
        "reg_alpha": 0.0,
        "n_estimators": 300,
        "early_stopping_rounds": 30,
        "tree_method": "hist",
        "objective": "reg:squarederror",
        "random_state": 42,
    }

    # 学習は (特徴量セット, target空間) ごとに1回（A0/A2 は同一モデル・A1/A3 も同一。
    # 差は後処理のみ）
    train_units = sorted({(CELLS[c][0], CELLS[c][1]) for c in cells},
                         key=lambda u: (u[0], TARGET_ORDER.index(u[1])))
    cv_runs = {}
    for fs, key in train_units:
        print(f"\nウォークフォワードCV実行中（特徴量={fs}・学習target={TARGET_LABELS[key]}）...")
        best_iterations: list[int] = []
        errors: list[str] = []
        fit_predict = _wrap_counting(_make_xgb_fit_predict(xgb_params, best_iterations), errors)
        cell_samples = _derive_cell_samples(samples_by_ym, meta_by_ym, key, FEATURE_IDX[fs])
        fold_results, residuals_by_ym = walk_forward_cv_monthly(
            cell_samples, FEATURE_SETS[fs],
            min_train_months=8, step_months=1,
            return_residuals=True, fit_predict=fit_predict,
        )
        print(f"  フォールド数: {len(fold_results)} 例外: {len(errors)}"
              f" best_iteration中央値: {int(np.median(best_iterations)) if best_iterations else '-'}")
        cv_runs[(fs, key)] = {"fold_results": fold_results, "residuals": residuals_by_ym, "errors": errors}

    # ガードレール: セル間比較の公平性（フォールド・サンプル完全一致／例外ゼロ）が
    # 崩れていたら比較表を出さずエラー終了
    ref_unit = train_units[0]
    ref_folds = [(f["test_ym"], f["n_train"], f["n_test"]) for f in cv_runs[ref_unit]["fold_results"]]
    if not ref_folds:
        print("フォールドが構築できず評価不可（学習月数不足の可能性）")
        return
    for unit in train_units:
        run_ = cv_runs[unit]
        label = f"{unit[0]}x{TARGET_LABELS[unit[1]]}"
        if run_["errors"]:
            print(f"[NG] fit_predict例外 {len(run_['errors'])}件（{label}）: "
                  f"{run_['errors'][:3]} -> 判定無効")
            sys.exit(1)
        folds = [(f["test_ym"], f["n_train"], f["n_test"]) for f in run_["fold_results"]]
        if folds != ref_folds:
            print(f"[NG] フォールド不一致（{label} vs {ref_unit[0]}x{TARGET_LABELS[ref_unit[1]]}）-> 判定無効")
            sys.exit(1)

    # セル×評価空間の評価
    results = {}
    ics_abs = {}
    for cell in cells:
        fs, train_key, zero_sum = CELLS[cell]
        residuals = cv_runs[(fs, train_key)]["residuals"]
        for y_space in EVAL_SPACES:
            pairs = _build_eval_pairs(residuals, meta_by_ym, train_key, zero_sum, y_space, min_week_group)
            results[(cell, y_space)] = oof_backtest(pairs, n_quantiles=5, cost_bps=0.0)
            if y_space == "fwd_abs":
                ics_abs[cell] = _per_period_ics(pairs)

    n_oof_ref = results[(cells[0], "fwd_abs")]["n_oof_samples"]
    if any(results[(c, s)]["n_oof_samples"] != n_oof_ref for c in cells for s in EVAL_SPACES):
        print("[NG] セル間で n_oof_samples 不一致 -> 判定無効")
        sys.exit(1)

    print(f"\n{'=' * 76}")
    print("=== 表1: セル×評価空間 OOF評価 ===")
    print(f"（n_oof_samples={n_oof_ref}・n_quantiles=5・スコアは常にraw yhat）")
    header = f"{'セル':<6} {'特徴量':<4} {'学習target':<10} {'zero-sum':<8} {'評価y':<10} " \
             f"{'rank-IC':>8} {'(std)':>8} {'n':>3} {'spread':>8} {'hit':>6}"
    print(header)
    for cell in cells:
        fs, train_key, zero_sum = CELLS[cell]
        for y_space in EVAL_SPACES:
            r = results[(cell, y_space)]
            print(f"{cell:<6} {fs:<4} {TARGET_LABELS[train_key]:<10} {('基準週' if zero_sum else 'なし'):<8} "
                  f"{TARGET_LABELS[y_space]:<10} "
                  f"{r['rank_ic']['mean']:>8} {r['rank_ic']['std']:>8} {r['rank_ic']['n']:>3} "
                  f"{r['long_short_spread']:>8} {r['hit_rate']:>6}")

    print("\n=== 表2: 月次 paired IC差分（評価y=絶対）===")
    base_cell = "F0A0" if "F0A0" in cells else ("A0" if "A0" in cells else cells[0])
    comparisons = [(c, base_cell) for c in cells if c != base_cell]
    # target軸・特徴量軸の直交ペア（存在するもののみ）＋#337レガシーペア
    for extra in [("F3A1", "F3A0"), ("F3A1", "F0A1"), ("F1A1", "F1A0"), ("F2A1", "F2A0"),
                  ("A1", "A0"), ("A2", "A0"), ("A3", "A2"), ("A3", "A0"), ("A1a", "A0")]:
        if extra[0] in cells and extra[1] in cells and extra not in comparisons:
            comparisons.append(extra)
    paired_cache = {}
    for a, b in comparisons:
        s = _paired_stats(ics_abs[a], ics_abs[b])
        paired_cache[(a, b)] = s
        print(f"{a}−{b}: mean_diff={s['mean_diff']} win_rate={s['win_rate']} "
              f"wilcoxon_p={s['wilcoxon_p']} n={s['n']}")

    print("\n=== 表3: 年別 rank-IC（評価y=絶対）===")
    years = sorted({ym[:4] for ym in ics_abs[cells[0]]})
    print(f"{'セル':<6} " + " ".join(f"{y:>14}" for y in years))
    for cell in cells:
        row = []
        for y in years:
            vals = [ic for ym, ic in ics_abs[cell].items() if ym[:4] == y and ic is not None]
            row.append(f"{round(statistics.mean(vals), 4):>8}({len(vals):>2}月)" if vals else f"{'-':>14}")
        print(f"{cell:<6} " + " ".join(row))

    gs = _base_week_group_stats(meta_by_ym)
    print("\n=== 補助診断 ===")
    print(f"(ym,基準週)グループサイズ: n={gs.get('n_groups')} min={gs.get('min')} "
          f"p25={gs.get('p25')} median={gs.get('median')} p75={gs.get('p75')} max={gs.get('max')}")
    print(f"  n=1グループ比率: {gs.get('share_groups_n1')} / グループ>=3に属すサンプル比率: {gs.get('share_samples_in_ge3')}")
    ym_mean_fwd = {ym: statistics.mean(m["fwd_abs"] for m in metas) for ym, metas in meta_by_ym.items()}
    ym_mean_rmkt = {ym: statistics.mean(m["r_mkt"] for m in metas) for ym, metas in meta_by_ym.items()}
    corr = _spearman([ym_mean_fwd[ym] for ym in sorted(ym_mean_fwd)],
                     [ym_mean_rmkt[ym] for ym in sorted(ym_mean_fwd)])
    print(f"月平均fwd_abs vs 月平均R_mkt の順位相関: {round(corr, 4) if corr is not None else '-'}"
          f"（高い=インデックスが市場成分を捕捉）")
    print("\n参考・歴史的baseline（#323フェーズ1 commit 7d05f6b）: rank-IC=0.0209 / spread=0.59% / hit=71%")
    print("※ DB追加収集＋R_mkt欠損除外により母集団が異なるため直接比較不可。比較は同一実行内A0と行う")

    _gate_report(cells, results, ics_abs, paired_cache, base_cell)

    for fs, key in train_units:
        print(f"\n=== フォールド別 R2/RMSE（特徴量={fs}・学習target={TARGET_LABELS[key]}空間・セル間比較不可）===")
        for f in cv_runs[(fs, key)]["fold_results"]:
            print(f"  {f['test_ym']}: n_train={f['n_train']:>6} n_test={f['n_test']:>4} "
                  f"r2={f['r2']:>7} rmse={f['rmse']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-weeks", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="対象企業数の上限（0=無制限）")
    parser.add_argument("--cells", type=str, default="F0A0,F1A0,F2A0,F3A0,F0A1,F3A1",
                        help=f"実行セル（カンマ区切り・有効: {','.join(CELLS)}）")
    parser.add_argument("--trim", type=float, default=0.01, help="インデックス断面トリム比率（両端）")
    parser.add_argument("--min-names", type=int, default=100, help="インデックス週あたり最小有効銘柄数")
    parser.add_argument("--min-week-group", type=int, default=1,
                        help="zero-sum demean を適用する最小グループサイズ（1=全グループ）")
    parser.add_argument("--min-cs-group", type=int, default=5,
                        help="開示イベント断面rank(ev_*)を計算する最小グループサイズ")
    args = parser.parse_args()
    run(args.forward_weeks, args.limit, [c.strip() for c in args.cells.split(",") if c.strip()],
        args.trim, args.min_names, args.min_week_group, args.min_cs_group)
