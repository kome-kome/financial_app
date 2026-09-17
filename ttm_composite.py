"""TTM（直近12か月）合成 — 半期決算を通期の列の形へ直して分析へ入れる（#424 子2・ADR-0051）。

`financial_metrics` VIEW は `period_type='annual'` の行しか出さないので、半期（H1）は収集
済みでもモデルからは見えない。半期の行をそのまま通期の列へ混ぜると、フロー（PL・CF）の
期間が銘柄ごとにばらばらになり、Zスコア（`PARTITION BY year`）も成長率（`LAG`）も壊れる。
そこで**前期の通期 − 前期の半期 + 今期の半期**で 12 か月ぶんのフローを作り、ストック（BS）は
今期の半期の期末値を使う「TTM 行」を、専用の表へ毎晩全置換する（分割補正係数と同じ形）。

## この合成の敵は「例外を出さずに値だけ狂う」壊れ方である

- **1 株指標の基準**: 3 つの材料の間に分割・併合があると、`pl_eps` の基準が混ざる。足し引きの
  結果はもっともらしい数字になり、例外は出ない。**基準が揃わない社・年度は合成しない**
  （ADR-0051 の「株数の比で揃える」は実装時にこの方針へ倒した。生の株数比で揃えると、
  自己株式の消却や増資まで分割として扱ってしまう＝EPS は期中平均株数ベースなので、
  消却・増資では基準を直してはいけない）。
- **表示の省略**: 半期の BS は科目をまとめて出すことがあり、通期にある列が空で入る。VIEW は
  `net_cash` / `de_ratio` の計算で欠けを 0 として扱うので、そのままでは値が黙って偏る。
  引き継ぐ列（`CARRY_FORWARD_BS_COLUMNS`）と通期値で埋める列（`ANNUAL_FALLBACK_FLOW_COLUMNS`）は
  実測で決めた集合だけに限る。
- **先読み**: 学習パネルは「期末＋45日」で行を使い始める（`macro_snapshots._find_applicable_fin`）。
  提出がそれより遅い H1 から作ると、まだ公表されていない実績で過去を評価することになる。

**弾いた件数は理由ごとに毎晩数えて出す**（#605 と同じ考え方＝「作れなかった」と「作らなかった」を
混ぜない）。ゼロ件が続くなら判定が効いていない合図である。

## 分割の判定に使う 3 つの信号

危ない窓は **(前期 H1 の提出日, 今期 H1 の提出日]** である。分割は遡って修正されるので、
その窓より前のイベントは 3 つの材料すべてに反映済み、窓より後のイベントはどれにも入っていない。

1. 株数の比（`issued_shares` と「純利益 ÷ EPS」の逆算）が `SPLIT_GATE_RATIO`（検出器の
   `DEFAULT_MIN_RATIO`）以上動いた。**逆算を併せて見るのは、期末の後・提出の前に分割が
   あると EPS だけが遡って直り、`issued_shares` は期末の株数のまま残るためである。**
2. 係数表の検出器（`scripts/measure_split_valuation_bias.detect_events`）のイベント窓が
   危ない窓と重なる。第2経路の「倍率待ち」も同じ扱いにする（倍率が決まっていないだけで、
   分割そのものは起きている）。
3. 公式 `AdjFactor`（`jquants_adj_factor_events`）の日付が危ない窓の中にある。

**1.4 倍未満の小さな分割（1:1.1 の無償割当など）は、公式の契約窓（2024-06 以降）の外では
拾えない。** 既存の通期の補正（F）と同じ制約で、ADR-0051 に明記した。

## 分割補正係数 F

TTM 行の F は「今期 H1 の**提出日**より後に起きたイベントの積」である（通期の行は「その行の
年より後」）。窓が提出日をまたぐイベントは上の判定で弾かれるので、ここでは窓が丸ごと後にある
ものだけを掛ける。**最新の TTM 行では、提出日より後の分割を検出器が見つけられない**
（次の通期がまだ無い）＝通期の最新行と同じ制約である。

理論は docs/adr/0051-latest-results-enter-analysis-as-a-ttm-composite.md、用語は CONTEXT.md
（「TTM 行」「行の基準」）が正本。
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Callable, NamedTuple, Optional, Sequence

log = logging.getLogger("collector")

# ── 定数 ────────────────────────────────────────────────────────────────────
# H1 の提出がこれより遅い行からは作らない。学習パネルが「期末＋45日」で行を使い始めるため
# （`plugins/macro_snapshots._find_applicable_fin`）。半期報告書の提出期限そのものでもある。
H1_FILING_MAX_DAYS = 45

# 「純利益 ÷ EPS」で株数を逆算するときの EPS の下限（円）。EPS は小数第2位までの提出値なので、
# 小さいと丸め誤差が株数比を大きく動かす。
MIN_EPS_FOR_IMPLIED_SHARES = 1.0

# 期末日の間隔（日）。月末で揺れるので幅を持たせる。外れたら決算期の変更として作らない。
PERIOD_GAP_ANNUAL_TO_H1 = (150, 205)     # A(Y−1) 期末 → H(Y) 期末（約 6 か月）
PERIOD_GAP_H1_TO_H1 = (350, 380)         # H(Y−1) 期末 → H(Y) 期末（約 12 か月）
PERIOD_GAP_H1_TO_ANNUAL = (150, 205)     # H(Y) 期末 → A(Y) 期末（A(Y) がある年だけ）

# 桁の外れ。TTM 売上 ÷ A(Y−1) 売上、H(Y) 総資産 ÷ A(Y−1) 総資産がこの範囲の外なら作らない。
# 会計基準の変更・連結範囲の変更・単位の取り違えは、**例外を出さずにここへ現れる**。
#
# 2026-09-18 の実測（構造の判定を通った 17,268 行）:
#   売上比   p0.1%=0.438 / p50=1.029 / p99.9%=2.094 / 最大 8.97 / 最小 0.063
#   総資産比 p0.1%=0.590 / p50=1.014 / p99.9%=2.616 / 最大 16.59 / 最小 0.144
# 5 倍で切ると、外れるのは総資産比が毎年 7〜9 倍で一定の社（E00317）のような**構造的に
# 基準の違う行**に寄る。**分布の分位点で切らない**（p99.9 で切ると、実際に事業が倍増した
# 社まで毎年一定数を落とす）。桁が変わる側だけを落とす閾値として 5 倍・1/5 を置く。
REVENUE_RATIO_BOUNDS = (0.2, 5.0)
ASSETS_RATIO_BOUNDS = (0.2, 5.0)

# 市場データ列（株価から計算する列）。合成せず、株価を当ててから作り直す。
MARKET_COLUMNS = ("stock_price", "per", "pbr", "market_cap", "div_yield")
# 合成の規則が列名で決まっているもの。
SHARES_COLUMN = "issued_shares"          # 今期 H1 の値
ANNUAL_ONLY_COLUMNS = ("dps", "employees")   # 前期の通期の値（配当は TTM にしない・決定5）
BPS_COLUMN = "bs_bps"                    # 合成で作る（H1 はほぼ全行が空）
EQUITY_COLUMNS = ("bs_total_equity", "bs_equity_parent")   # BPS を伸縮させる純資産

# H1 に無く前期の通期にある BS 列のうち、**表示の省略と確かめられた列だけ**を引き継ぐ。
# 空のままだと VIEW が 0 として扱い、`net_cash`（→ `nc_ratio`）と `de_ratio` が黙って偏る。
#
# 「表示の省略」と言える根拠は、**同じ社の翌年の通期にはその列がある**ことである（半期の BS は
# 科目をまとめて出す）。2026-09-18 の実測（候補 19,134 組）では、H1 で欠けた列の 85〜92% が
# 翌年の通期に現れた: 受取手形 33.7%（翌年あり 89.3%）・建物 32.9%（90.2%）・機械装置 32.3%
# （92.3%）・投資有価証券 27.6%（90.2%）・短期借入金 8.2%（69.1%）など。
# **フロー（PL・CF）はここへ入れない**——期間の違う値を混ぜることになる。
CARRY_FORWARD_BS_COLUMNS: frozenset = frozenset({
    "bs_investment_securities", "bs_short_term_debt", "bs_long_term_debt",
    "bs_bonds_payable", "bs_payables", "bs_receivables", "bs_inventory",
    "bs_buildings", "bs_machinery", "bs_ppe_total", "bs_intangible_assets",
    "bs_investments_other_assets", "bs_paid_in_capital", "bs_retained_earnings",
    "bs_noncurrent_assets", "bs_current_liabilities", "bs_noncurrent_liabilities",
})

# 注記にしか出ない（半期では開示されないことが多い）フロー列。H1 側が欠けたら前期の通期の
# 12 か月ぶんの値をそのまま使う——TTM も 12 か月ぶんなので**期間の長さは合う**（半年古いだけ）。
# **主要な列（売上・利益・CF・EPS）はここへ入れない**——古い実績を新しい実績に見せかけることになる。
#
# 研究開発費だけを入れたのは実測から: H1 で欠けるのが 43.3%（うち 91.0% は翌年の通期にある＝
# 半期では注記に出ないだけ）で、これを空のままにすると `rd_intensity` を選んでいる断面から
# **4 割の社が丸ごと落ちる**（特徴量が1つでも空のサンプルは捨てられる）。
ANNUAL_FALLBACK_FLOW_COLUMNS: frozenset = frozenset({
    "pl_rd_expenses",
})

# `doc_type` の形: FYFinancialStatements_Consolidated_JP / 2QFinancialStatements_NonConsolidated_IFRS
_DOC_TYPE_RE = re.compile(
    r"^(?P<period>FY|1Q|2Q|3Q|4Q|X|Other)FinancialStatements"
    r"_(?P<consolidation>Consolidated|NonConsolidated)"
    r"_(?P<standard>JP|IFRS|US|JMIS|Foreign)$")


# ── 純関数 ──────────────────────────────────────────────────────────────────
class SourceRow(NamedTuple):
    """合成の材料になる `financial_records` の 1 行（通期または H1）。"""
    edinet_code: str
    year: int
    period_type: str
    period_end: Optional[date]
    filing_date: Optional[date]
    doc_id: Optional[str]
    sec_code: Optional[str]
    company_name: Optional[str]
    industry: Optional[str]
    market: Optional[str]
    values: dict            # 財務列（列名 → 値）


class SplitWindow(NamedTuple):
    """分割イベントの窓。検出器の `ShareEvent` と公式 `AdjFactor` の両方をこの形へ寄せる。

    `start` は「この日より後」、`end` は「この日まで」を表す半開区間 (start, end]。
    公式イベントは日付が 1 点なので `start == end` になる。
    """
    start: Optional[date]
    end: Optional[date]
    canonical: Optional[float]
    source: str             # detected | awaiting | official


class Basis(NamedTuple):
    """開示（`statement_disclosure.doc_type`）から読んだ会計基準と連結・単体の別。"""
    consolidation: str
    standard: str


def parse_doc_type(doc_type: Optional[str]) -> Optional[Basis]:
    """`doc_type` から会計基準と連結・単体を読む。予想修正など決算以外の開示は None。"""
    if not doc_type:
        return None
    m = _DOC_TYPE_RE.match(doc_type.strip())
    if not m:
        return None
    return Basis(m.group("consolidation"), m.group("standard"))


def implied_shares(values: dict) -> Optional[float]:
    """「純利益 ÷ EPS」から株数を逆算する。判定できなければ None。

    `issued_shares` は期末時点の株数だが、EPS は**提出時点の基準**へ遡って直される。
    期末の後・提出の前に分割があると両者がずれ、そのずれだけが分割の痕跡になる。
    """
    eps = values.get("pl_eps")
    ni = values.get("pl_net_income_attr")
    if ni in (None, 0):
        ni = values.get("pl_net_income")
    if eps is None or ni in (None, 0):
        return None
    if abs(eps) < MIN_EPS_FOR_IMPLIED_SHARES:
        return None             # 丸めの効く領域では比が信用できない
    shares = ni / eps
    return shares if shares > 0 else None    # 符号が食い違う（attr と総額の取り違え等）なら使わない


def ratio_moved(a: Optional[float], b: Optional[float], *, min_ratio: float) -> bool:
    """`b / a` が `min_ratio` 倍以上（対数対称に）動いたか。どちらかが無ければ False。"""
    if not a or not b or a <= 0 or b <= 0:
        return False
    return abs(math.log(b / a)) >= math.log(min_ratio) - 1e-12


def windows_overlap(win: SplitWindow, start: date, end: date) -> bool:
    """イベントの窓 (win.start, win.end] が危ない窓 (start, end] と重なるか。

    窓の端が欠けているイベントは**重なる側へ倒す**（分からないものを安全と読まない）。
    """
    if win.start is None or win.end is None:
        return True
    return win.start < end and win.end > start


def ttm_split_factor(windows: Sequence[SplitWindow], filing_date: date) -> float:
    """TTM 行の分割補正係数 F ＝ 提出日より後にある窓の定番比の積。

    窓が提出日をまたぐイベントは呼び出し側（`reject_reason`）が弾いているので、ここでは
    「丸ごと後」だけを掛ければ足りる。倍率の決まっていない窓（`canonical` が None）は
    掛けない＝通期側の `cumulative_factors(use_canonical=True)` と同じ扱いである。
    """
    f = 1.0
    for w in windows:
        if w.canonical and w.start is not None and w.start >= filing_date:
            f *= w.canonical
    return f


def _days_between(a: Optional[date], b: Optional[date]) -> Optional[int]:
    if a is None or b is None:
        return None
    return (b - a).days


def _in_range(days: Optional[int], bounds: tuple[int, int]) -> bool:
    return days is not None and bounds[0] <= days <= bounds[1]


def reject_reason(annual_prev: SourceRow, h1_prev: SourceRow, h1_cur: SourceRow, *,
                  annual_cur: Optional[SourceRow],
                  windows: Sequence[SplitWindow],
                  basis_prev: Optional[Basis], basis_cur: Optional[Basis],
                  split_gate_ratio: float) -> Optional[str]:
    """合成しない理由。作ってよければ None。

    **順序に意味がある**: 構造（期間・提出日）→ 基準（会計基準・分割）→ 値（桁）。先に構造で
    弾いておかないと、期ずれの行どうしの比を「桁が外れた」と数えてしまい、件数の意味が濁る。
    """
    # ── 構造 ──
    if not _in_range(_days_between(annual_prev.period_end, h1_cur.period_end),
                     PERIOD_GAP_ANNUAL_TO_H1):
        return "period_shift"
    if not _in_range(_days_between(h1_prev.period_end, h1_cur.period_end),
                     PERIOD_GAP_H1_TO_H1):
        return "period_shift"
    if annual_cur is not None and not _in_range(
            _days_between(h1_cur.period_end, annual_cur.period_end), PERIOD_GAP_H1_TO_ANNUAL):
        return "period_shift"
    if h1_cur.filing_date is None or h1_prev.filing_date is None:
        return "no_filing_date"
    if _days_between(h1_cur.period_end, h1_cur.filing_date) > H1_FILING_MAX_DAYS:
        return "late_filing"

    # ── 基準（会計基準・連結範囲）──
    if basis_prev and basis_cur:
        if basis_prev.standard != basis_cur.standard:
            return "standard_change"
        if basis_prev.consolidation != basis_cur.consolidation:
            return "consolidation_change"

    # ── 基準（分割・併合）。危ない窓は「前期 H1 の提出日 〜 今期 H1 の提出日」──
    danger_start, danger_end = h1_prev.filing_date, h1_cur.filing_date
    for w in windows:
        if windows_overlap(w, danger_start, danger_end):
            return {"official": "split_official", "awaiting": "split_awaiting"}.get(
                w.source, "split_detected")
    pairs = ((annual_prev, h1_cur), (h1_prev, annual_prev), (h1_prev, h1_cur))
    for lhs, rhs in pairs:
        if ratio_moved(lhs.values.get(SHARES_COLUMN), rhs.values.get(SHARES_COLUMN),
                       min_ratio=split_gate_ratio):
            return "split_shares"
        if ratio_moved(implied_shares(lhs.values), implied_shares(rhs.values),
                       min_ratio=split_gate_ratio):
            return "split_implied_shares"
    return None


def _flow(annual_prev: SourceRow, h1_prev: SourceRow, h1_cur: SourceRow,
          col: str) -> Optional[float]:
    """フロー列の TTM ＝ A(Y−1) − H(Y−1) + H(Y)。材料が 1 つでも空なら空。"""
    a, p, c = (annual_prev.values.get(col), h1_prev.values.get(col), h1_cur.values.get(col))
    if a is None or p is None or c is None:
        return None
    return a - p + c


def compose_values(annual_prev: SourceRow, h1_prev: SourceRow, h1_cur: SourceRow, *,
                   columns: Sequence[str], carried: Optional[Counter] = None) -> dict:
    """TTM 行の財務列。市場データ列（`MARKET_COLUMNS`）は作らない（株価を当ててから計算する）。

    `carried` を渡すと、引き継ぎ・通期値での代用を列ごとに数える。**数えないと「引き継ぎが
    一度も起きていない」と「引き継ぎが常に起きている」を区別できない。**
    """
    out: dict = {}
    for col in columns:
        if col in MARKET_COLUMNS:
            continue
        if col == SHARES_COLUMN:
            out[col] = h1_cur.values.get(col)
        elif col in ANNUAL_ONLY_COLUMNS:
            out[col] = annual_prev.values.get(col)
        elif col == BPS_COLUMN:
            out[col] = _compose_bps(annual_prev, h1_cur)
        elif col.startswith(("pl_", "cf_")):
            v = _flow(annual_prev, h1_prev, h1_cur, col)
            if v is None and col in ANNUAL_FALLBACK_FLOW_COLUMNS:
                v = annual_prev.values.get(col)
                if v is not None and carried is not None:
                    carried[f"flow:{col}"] += 1
            out[col] = v
        elif col.startswith("bs_"):
            v = h1_cur.values.get(col)
            if v is None and col in CARRY_FORWARD_BS_COLUMNS:
                v = annual_prev.values.get(col)
                if v is not None and carried is not None:
                    carried[f"bs:{col}"] += 1
            out[col] = v
        else:
            # val / nonfin の未知の列。**黙って落とさない**（列が増えたら扱いを決める）。
            raise ValueError(f"TTM 合成の規則が決まっていない列: {col}")
    return out


def _compose_bps(annual_prev: SourceRow, h1_cur: SourceRow) -> Optional[float]:
    """1 株純資産 ＝ 前期通期の BPS × (今期 H1 の純資産 ÷ 前期通期の純資産)。

    H1 行の `bs_bps` はほぼ全行が空なので、通期の提出値を純資産の伸縮で引き伸ばす。
    純資産の総額どうしの比を使うので、自己株式の扱い（提出値の BPS は自己株式を除いた
    株数で割る）が前期の値のまま引き継がれる。**総額 ÷ 株数で作り直すと、自己株式を
    持つ社で系統的に小さく出る。**
    """
    for col in EQUITY_COLUMNS:
        base, cur = annual_prev.values.get(col), h1_cur.values.get(col)
        bps = annual_prev.values.get(BPS_COLUMN)
        if bps and base and cur and base > 0:
            return bps * (cur / base)
    return None


def magnitude_reason(values: dict, annual_prev: SourceRow) -> Optional[str]:
    """桁の外れ。合成した値が前期の通期からかけ離れていれば作らない。"""
    rev_ttm, rev_prev = values.get("pl_revenue"), annual_prev.values.get("pl_revenue")
    if rev_ttm is not None and rev_prev:
        if rev_ttm <= 0 < rev_prev:
            return "revenue_nonpositive"
        r = rev_ttm / rev_prev
        if r > 0 and not (REVENUE_RATIO_BOUNDS[0] <= r <= REVENUE_RATIO_BOUNDS[1]):
            return "magnitude_revenue"
    assets, assets_prev = values.get("bs_total_assets"), annual_prev.values.get("bs_total_assets")
    if assets and assets_prev and assets_prev > 0:
        r = assets / assets_prev
        if not (ASSETS_RATIO_BOUNDS[0] <= r <= ASSETS_RATIO_BOUNDS[1]):
            return "magnitude_assets"
    return None


def second_half_reason(annual_prev: SourceRow, h1_prev: SourceRow) -> Optional[str]:
    """下期の売上が 0 以下なら作らない。A(Y−1) と H(Y−1) の基準が食い違う合図である。"""
    a, p = annual_prev.values.get("pl_revenue"), h1_prev.values.get("pl_revenue")
    if a is not None and p is not None and a - p <= 0:
        return "second_half_nonpositive"
    return None


def build_ttm_rows(rows: Sequence[SourceRow], *,
                   windows_by_ec: dict,
                   basis_by_key: dict,
                   columns: Sequence[str],
                   split_gate_ratio: float,
                   embargo_days: int,
                   today: date,
                   price_for: Callable[[str, date, bool], Optional[float]],
                   market_values: Callable[..., dict]) -> tuple[list, dict]:
    """全社ぶんの TTM 行と、作らなかった理由の集計を返す。

    `price_for(edinet_code, period_end, is_latest)` は株価を返す。社の最新の行（それより後の
    通期も TTM も無い行）は現在株価、それ以外は期末に近い週次終値——**通期の行と同じ規約**で、
    ここで規約を割ると per / pbr が行の基準ごとに別の意味になる。

    `market_values(price, pl_eps, bs_bps, issued_shares, bs_total_equity, dps)` は
    `collector_prices._compute_market_values` をそのまま渡す（計算を書き写さない）。
    """
    by_ec: dict = defaultdict(lambda: {"annual": {}, "H1": {}, "dup": set()})
    for r in rows:
        if r.period_type not in ("annual", "H1") or r.period_end is None:
            continue
        slot = by_ec[r.edinet_code][r.period_type]
        if r.year in slot:
            by_ec[r.edinet_code]["dup"].add(r.year)
        slot[r.year] = r

    reasons: Counter = Counter()
    notes: Counter = Counter()
    carried: Counter = Counter()
    by_year: Counter = Counter()
    out: list = []

    for ec, slots in by_ec.items():
        annuals, h1s, dup = slots["annual"], slots["H1"], slots["dup"]
        windows = windows_by_ec.get(ec, ())
        # その社の最新の期末日（通期と H1 の両方を見る）。TTM 行が最新かどうかの判定に使う。
        latest_end = max((r.period_end for r in list(annuals.values()) + list(h1s.values())
                          if r.period_end), default=None)
        made: list = []
        for year in sorted(h1s):
            h1_cur = h1s[year]
            annual_prev, h1_prev = annuals.get(year - 1), h1s.get(year - 1)
            if year in dup or (year - 1) in dup:
                reasons["dup_rows"] += 1
                continue
            if annual_prev is None:
                reasons["no_prev_annual"] += 1
                continue
            if h1_prev is None:
                reasons["no_prev_h1"] += 1
                continue
            basis_prev = basis_by_key.get((ec, "FY", annual_prev.period_end))
            basis_cur = basis_by_key.get((ec, "2Q", h1_cur.period_end))
            if not (basis_prev and basis_cur):
                # 開示は 84 日遅れで届く（J-Quants 無料プラン）。**まだ届いていない**のと
                # **もともと無い**のを分けて数える＝前者は日が経てば判定できる。
                fd = h1_cur.filing_date
                notes["standard_embargo" if fd and (today - fd).days <= embargo_days
                      else "standard_unknown"] += 1
            reason = reject_reason(
                annual_prev, h1_prev, h1_cur, annual_cur=annuals.get(year),
                windows=windows, basis_prev=basis_prev, basis_cur=basis_cur,
                split_gate_ratio=split_gate_ratio)
            if reason:
                reasons[reason] += 1
                continue
            reason = second_half_reason(annual_prev, h1_prev)
            if reason:
                reasons[reason] += 1
                continue
            values = compose_values(annual_prev, h1_prev, h1_cur,
                                    columns=columns, carried=carried)
            reason = magnitude_reason(values, annual_prev)
            if reason:
                reasons[reason] += 1
                continue
            values["split_factor"] = ttm_split_factor(windows, h1_cur.filing_date)
            made.append((year, h1_cur, annual_prev, h1_prev, values))

        for year, h1_cur, annual_prev, h1_prev, values in made:
            is_latest = latest_end is not None and h1_cur.period_end >= latest_end
            price = price_for(ec, h1_cur.period_end, is_latest)
            if price:
                values.update(market_values(
                    price, values.get("pl_eps"), values.get(BPS_COLUMN),
                    values.get(SHARES_COLUMN), values.get("bs_total_equity"),
                    values.get("dps")))
            else:
                notes["no_price"] += 1
            by_year[year] += 1
            out.append(dict(
                values,
                edinet_code=ec, year=year, period_end=h1_cur.period_end,
                filing_date=h1_cur.filing_date, doc_id=h1_cur.doc_id,
                sec_code=h1_cur.sec_code or annual_prev.sec_code,
                company_name=h1_cur.company_name or annual_prev.company_name,
                industry=h1_cur.industry or annual_prev.industry,
                market=annual_prev.market,
                prev_annual_period_end=annual_prev.period_end,
                prev_h1_period_end=h1_prev.period_end,
            ))

    stats = {
        "n_rows": len(out),
        "n_companies": len({r["edinet_code"] for r in out}),
        "by_year": dict(sorted(by_year.items())),
        "rejected": dict(reasons.most_common()),
        "notes": dict(notes.most_common()),
        "carried": dict(carried.most_common()),
    }
    return out, stats


def split_windows(events: Sequence, awaiting: Sequence[dict],
                  official: Sequence[tuple]) -> list:
    """検出イベント・倍率待ち・公式 AdjFactor を 1 社ぶんの `SplitWindow` 列へ寄せる。

    3 つは形が違うだけで、言っているのは同じ「この窓の中で株数の基準が変わった」である。
    寄せておかないと、判定側が 3 通りの形を知ることになる（そのうち 1 つを足し忘れても
    もっともらしい結果が返る）。
    """
    out: list = []
    for e in events:
        out.append(SplitWindow(_as_date(e.prev_period_end), _as_date(e.period_end),
                               e.canonical, "detected"))
    for a in awaiting:
        out.append(SplitWindow(_as_date(a.get("prev_period_end")), _as_date(a.get("period_end")),
                               None, "awaiting"))
    for d, factor in official:
        day = _as_date(d)
        if day is None or not factor or factor == 1.0:
            continue
        # 公式は「過去株価に掛ける係数」なので 1:2 分割は 0.5。株数比へ直すため逆数を取る。
        out.append(SplitWindow(day - timedelta(days=1), day, 1.0 / factor, "official"))
    return out


def _as_date(v) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# ── I/O ─────────────────────────────────────────────────────────────────────
def rebuild_ttm_financial_records(db) -> int:
    """`ttm_financial_records` を全置換する。戻り値は書いた行数。

    **毎晩作り直すのは、TTM 行が材料と F の両方に依存するからである。** 新しい半期が入れば
    行が増え、新しい分割が 1 件起きれば F が変わる。焼き付けた値は必ず陳腐化する
    （分割補正係数と同じ理由・ADR-0055）。

    **「入力が無い」と「入力はあるのに作れない」を分ける。** H1 の行が 0 件ならスキップして
    0 を返す（初回ブートストラップ前・テストのスタブ DB）。行はあるのに 1 件も作れないのは
    合成か判定が壊れた側なので `RuntimeError` を上げる。**どちらの場合も既存の表に触らない**
    ——消してから失敗すると、TTM が静かに全部消えた VIEW が残る（通期の行は出るのでエラーは
    出ず、画面もモデルも「元どおり」に見える）。
    """
    from collector_prices import (_compute_market_values, _nearest_price, MAX_GAP_DAYS,
                                 compute_split_adjustments)
    from collector_utils import JQUANTS_DISCLOSURE_DELAY_DAYS
    from database import (FinancialRecord, StatementDisclosure, StockPriceWeekly,
                          latest_prices, load_jquants_adj_factor_events,
                          replace_ttm_financial_records, ttm_financial_columns)
    from scripts.measure_split_valuation_bias import DEFAULT_MIN_RATIO

    columns = ttm_financial_columns()
    rows = _load_source_rows(db, FinancialRecord, columns)
    if not [r for r in rows if r.period_type == "H1"]:
        log.warning("TTM 合成: H1 行が0件のためスキップした（TTM 表は温存）")
        return 0

    computed = compute_split_adjustments(db)
    events_by_ec: dict = defaultdict(list)
    awaiting_by_ec: dict = defaultdict(list)
    if computed is not None:
        _, events, stats, _ = computed
        for e in events:
            events_by_ec[e.edinet_code].append(e)
        for a in ((stats.get("bps_path") or {}).get("awaiting_magnitude") or ()):
            awaiting_by_ec[a["edinet_code"]].append(a)
    official = load_jquants_adj_factor_events(db)
    windows_by_ec = {
        ec: split_windows(events_by_ec.get(ec, ()), awaiting_by_ec.get(ec, ()),
                          official.get(ec, ()))
        for ec in set(events_by_ec) | set(awaiting_by_ec) | set(official)
    }
    basis_by_key = _load_basis(db, StatementDisclosure)
    price_for = _price_lookup(db, rows, StockPriceWeekly, latest_prices,
                              _nearest_price, MAX_GAP_DAYS)

    out, stats = build_ttm_rows(
        rows, windows_by_ec=windows_by_ec, basis_by_key=basis_by_key, columns=columns,
        split_gate_ratio=DEFAULT_MIN_RATIO, embargo_days=JQUANTS_DISCLOSURE_DELAY_DAYS,
        today=date.today(), price_for=price_for, market_values=_compute_market_values)

    if not out:
        raise RuntimeError(
            "TTM 行が1件も作れなかった（材料 %d 行 / 弾いた理由 %s）。既存の TTM 表は温存する"
            % (len(rows), stats.get("rejected")))

    n = replace_ttm_financial_records(db, out)
    db.commit()
    log.info("TTM 行: %d 行 / %d 社 を全置換（年度別 %s）",
             n, stats["n_companies"], stats["by_year"])
    log.info("TTM 行: 作らなかった理由 %s", stats["rejected"])
    log.info("TTM 行: 注記 %s・引き継いだ列 %s", stats["notes"], stats["carried"])
    return n


def _load_source_rows(db, FinancialRecord, columns: Sequence[str]) -> list:
    """材料になる行を必要な列だけ読む（#446 の「消費列だけ引く」方針）。"""
    meta = ("edinet_code", "year", "period_type", "period_end", "filing_date", "doc_id",
            "sec_code", "company_name", "industry", "market")
    cols = [getattr(FinancialRecord, c) for c in meta + tuple(columns)]
    out = []
    for row in (db.query(*cols)
                .filter(FinancialRecord.period_type.in_(("annual", "H1")))
                .order_by(FinancialRecord.edinet_code, FinancialRecord.period_end).all()):
        vals = dict(zip(columns, row[len(meta):]))
        out.append(SourceRow(*row[:len(meta)], values=vals))
    return out


def _load_basis(db, StatementDisclosure) -> dict:
    """(edinet_code, 期種, 期末日) → 会計基準。決算以外の開示は `parse_doc_type` が落とす。"""
    out: dict = {}
    rows = (db.query(StatementDisclosure.edinet_code, StatementDisclosure.cur_per_type,
                     StatementDisclosure.cur_per_en, StatementDisclosure.cur_fy_en,
                     StatementDisclosure.doc_type, StatementDisclosure.disc_date)
            .filter(StatementDisclosure.cur_per_type.in_(("FY", "2Q")))
            .order_by(StatementDisclosure.disc_date).all())
    for ec, per_type, per_en, fy_en, doc_type, _disc in rows:
        basis = parse_doc_type(doc_type)
        if not basis or not ec:
            continue
        # FY は事業年度末、2Q は当期の期末で財務の行と突き合わせる（訂正の再開示は後勝ち）。
        end = _as_date(fy_en if per_type == "FY" else per_en)
        if end is not None:
            out[(ec, per_type, end)] = basis
    return out


def _price_lookup(db, rows: Sequence[SourceRow], StockPriceWeekly, latest_prices,
                  nearest_price, max_gap_days: int):
    """TTM 行へ当てる株価を返す関数を作る。

    過去の行は期末に近い週次終値、社の最新の行は現在株価——`_update_market_data_point_in_time`
    と同じ規約である（#421 の「最新行だけ現在株価」を TTM 側でも守る）。
    """
    ends = [r.period_end for r in rows if r.period_type == "H1" and r.period_end]
    history: dict = {}
    if ends:
        lo = (min(ends) - timedelta(days=max_gap_days)).isoformat()
        hi = (max(ends) + timedelta(days=max_gap_days)).isoformat()
        weekly = (db.query(StockPriceWeekly.edinet_code, StockPriceWeekly.trade_date,
                           StockPriceWeekly.close_last)
                  .filter(StockPriceWeekly.trade_date >= lo,
                          StockPriceWeekly.trade_date <= hi,
                          StockPriceWeekly.close_last > 0).all())
        acc: dict = defaultdict(list)
        for ec, td, close in weekly:
            acc[ec].append((td, close))
        history = {ec: ([d for d, _ in sorted(ps)], dict(ps)) for ec, ps in acc.items()}
    latest = latest_prices(db, sorted({r.edinet_code for r in rows}))

    def price_for(ec: str, period_end: date, is_latest: bool) -> Optional[float]:
        if is_latest:
            info = latest.get(ec) or {}
            price = info.get("price")
            if price and price > 0:
                return price
        idx = history.get(ec)
        if not idx:
            return None
        dates, prices = idx
        return nearest_price(dates, prices, period_end.isoformat(), max_gap_days)

    return price_for
