"""会社予想開示（statement_disclosure）からの特徴量計算層（Issue #322 改善案3）。

参考実装: J-Quants ファンダメンタルズ分析チャレンジ2位 UKI さんの予測モデル
https://github.com/UKI000/JQuants-Forum/blob/452a4f4bc086ef0a8b087efc707c51abad5ed50e/jquants01_fund_uki_predictor.py

UKI モデルの `r_*`（実績単四半期化）/ `f_*`（予想の年度内固定ベースライン）/
`m_*`（予想対比サプライズ）/ `d_f_*`（ガイダンス修正の前回開示比差分）を、
本アプリの statement_disclosure スキーマ（J-Quants /fins/summary）へ移植する。

スコープ外（意図的）: roe/roa/turn/equity_ratio/cf_ratio 系。
/fins/summary には四半期の貸借対照表・キャッシュフローが含まれず、本アプリで
四半期粒度の総資産・純資産・CFを持つのは無料プランの範囲外（Issue #219 で
J-Quants 有料化とセットで判断待ち）。年次 FinancialRecord の値を四半期に
流用すると参照期間がずれるため、ここでは組み込まない。

point-in-time原則: 各行は disc_date 時点で確定していた値のみで完結する
（ルックアヘッドなし）。モデルへの組み込み（別Issue #323 等）は本モジュールの
スコープ外。
"""
import numpy as np
import pandas as pd

# 除外する開示種別（実績本体を伴わない修正発表。単四半期化の基準系列には使わない）
_REVISION_DOC_TYPES = {"EarnForecastRevision", "DividendForecastRevision", "NumericalCorrection"}

# 単四半期化の対象（実績・予想とも同名の列を積み上げる）
_LEVEL_COLS = ["sales", "op", "odp", "np"]

_RAW_COLS = [
    "edinet_code", "disc_date", "disc_time", "disc_no", "doc_type", "cur_per_type",
    "cur_per_en", "cur_fy_en", "sales", "op", "odp", "np",
    "f_sales", "f_op", "f_odp", "f_np",
]


def dedupe_disclosures(rows: list[dict]) -> list[dict]:
    """1社分の開示行から特徴量計算の基準系列に使う行だけを残す。

    - ForecastRevision 等（実績を伴わない修正発表）を除外
    - 同一 (cur_per_type, cur_fy_en) の重複（連結/非連結が両方存在する場合等）は
      disc_date→disc_time→disc_no 順で最後の1件を残す（UKI 移植ノートブックの
      keep-last 方式）
    """
    filtered = [r for r in rows if r.get("doc_type") not in _REVISION_DOC_TYPES]
    filtered.sort(key=lambda r: (r["disc_date"], r.get("disc_time") or "", r["disc_no"]))
    keep: dict[tuple, dict] = {}
    for r in filtered:
        key = (r.get("cur_per_type"), r.get("cur_fy_en"))
        keep[key] = r  # 後勝ち = keep-last
    return sorted(keep.values(), key=lambda r: (r["disc_date"], r.get("disc_time") or "", r["disc_no"]))


def _quarterize(df: pd.DataFrame, raw_col: str, period_col: str, out_col: str) -> pd.Series:
    """累積値 raw_col を period_col の変化点で単四半期化する（1Qは累積=単四半期）。

    period_col が前行と同じ（同一期間の訂正発表）場合は再計算せず前値を引き継ぐ（ffill）。
    """
    prev_period = df[period_col].shift(1)
    period_changed = df[period_col] != prev_period
    is_q1 = df["cur_per_type"] == "1Q"

    out = pd.Series(np.nan, index=df.index)
    out[period_changed & ~is_q1] = df[raw_col].diff(1)[period_changed & ~is_q1]
    out[period_changed & is_q1] = df[raw_col][period_changed & is_q1]
    return out.ffill().rename(out_col)


def build_disclosure_features(rows: list[dict]) -> pd.DataFrame:
    """1社分の開示履歴（disc_no昇順である必要はない）から特徴量フレームを作る。

    引数の rows は statement_disclosure の列名を持つ dict のリスト（1社分）。
    戻り値は disc_date 昇順の DataFrame。r_*/f_* は単四半期化系列、m_* はサプライズ、
    d_f_* はガイダンス修正差分、pm/cost はP&Lのみで完結する複合比率。
    """
    clean = dedupe_disclosures(rows)
    if not clean:
        return pd.DataFrame(columns=_RAW_COLS)

    df = pd.DataFrame(clean)[_RAW_COLS].reset_index(drop=True)
    numeric_cols = ["sales", "op", "odp", "np", "f_sales", "f_op", "f_odp", "f_np"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")

    # 複合比率は「その開示時点で公表されていた生の値」を使う（UKI原典と同じ。
    # 単四半期化・年度内据え置きの前に退避しておく）
    f_raw = {col: df["f_" + col].copy() for col in _LEVEL_COLS}

    # 実績・予想の単四半期化（予想は cur_fy_en＝年度単位でのみ更新、年度内は据え置き）
    for col in _LEVEL_COLS:
        df["r_" + col] = _quarterize(df, col, "cur_per_en", "r_" + col)
    for col in _LEVEL_COLS:
        df["f_" + col] = _quarterize(df, "f_" + col, "cur_fy_en", "f_" + col)

    # 費用3分解（単四半期化後の値から）
    df["r_expense1"] = df["r_sales"] - df["r_op"]
    df["r_expense2"] = df["r_op"] - df["r_odp"]
    df["r_expense3"] = df["r_odp"] - df["r_np"]
    df["f_expense1"] = df["f_sales"] - df["f_op"]
    df["f_expense2"] = df["f_op"] - df["f_odp"]
    df["f_expense3"] = df["f_odp"] - df["f_np"]

    # 複合比率（P&Lのみで完結。累積の生値ベース＝UKI原典と同じ）
    df["r_pm1"] = df["np"] / df["sales"]
    df["r_pm2"] = df["odp"] / df["sales"]
    df["r_pm3"] = df["op"] / df["sales"]
    df["f_pm1"] = f_raw["np"] / f_raw["sales"]
    df["f_pm2"] = f_raw["odp"] / f_raw["sales"]
    df["f_pm3"] = f_raw["op"] / f_raw["sales"]

    df["r_cost1"] = (df["sales"] - df["op"]) / df["sales"]
    df["r_cost2"] = (df["op"] - df["odp"]) / df["sales"]
    df["r_cost3"] = (df["odp"] - df["np"]) / df["sales"]
    df["f_cost1"] = (f_raw["sales"] - f_raw["op"]) / f_raw["sales"]
    df["f_cost2"] = (f_raw["op"] - f_raw["odp"]) / f_raw["sales"]
    df["f_cost3"] = (f_raw["odp"] - f_raw["np"]) / f_raw["sales"]

    df.replace([np.inf, -np.inf], np.nan, inplace=True)

    level_feats = ["r_sales", "r_op", "r_odp", "r_np", "f_sales", "f_op", "f_odp", "f_np",
                   "r_expense1", "r_expense2", "r_expense3", "f_expense1", "f_expense2", "f_expense3"]
    ratio_feats = ["r_pm1", "r_pm2", "r_pm3", "f_pm1", "f_pm2", "f_pm3",
                   "r_cost1", "r_cost2", "r_cost3", "f_cost1", "f_cost2", "f_cost3"]

    # ガイダンス修正の前回開示比差分（f_* は年度内据え置きなので実質YoY）
    for col in [c for c in level_feats + ratio_feats if c.startswith("f_")]:
        df["d_" + col] = df[col].diff(1)

    # 予想対比サプライズ = 実績単四半期 − 前回開示時点の予想
    df["m_sales"] = df["r_sales"] - df["f_sales"].shift(1)
    df["m_op"] = df["r_op"] - df["f_op"].shift(1)
    df["m_odp"] = df["r_odp"] - df["f_odp"].shift(1)
    df["m_np"] = df["r_np"] - df["f_np"].shift(1)
    df["m_expense1"] = df["r_expense1"] - df["f_expense1"].shift(1)
    df["m_expense2"] = df["r_expense2"] - df["f_expense2"].shift(1)
    df["m_expense3"] = df["r_expense3"] - df["f_expense3"].shift(1)
    df["m_pm1"] = df["r_pm1"] - df["f_pm1"].shift(1)
    df["m_pm2"] = df["r_pm2"] - df["f_pm2"].shift(1)
    df["m_pm3"] = df["r_pm3"] - df["f_pm3"].shift(1)
    df["m_cost1"] = df["r_cost1"] - df["f_cost1"].shift(1)
    df["m_cost2"] = df["r_cost2"] - df["f_cost2"].shift(1)
    df["m_cost3"] = df["r_cost3"] - df["f_cost3"].shift(1)

    return df


def load_disclosure_features(db, edinet_code: str) -> pd.DataFrame:
    """statement_disclosure から1社分を取得し build_disclosure_features へ渡す。"""
    from database import StatementDisclosure

    q = (
        db.query(StatementDisclosure)
        .filter(StatementDisclosure.edinet_code == edinet_code)
        .all()
    )
    rows = [
        {c.name: getattr(r, c.name) for c in StatementDisclosure.__table__.columns}
        for r in q
    ]
    return build_disclosure_features(rows)
