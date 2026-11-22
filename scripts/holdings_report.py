"""保有証券一覧 CSV を口座の種類ごとに分析し、1 枚の HTML レポートを書く。

証券会社の「保有証券一覧」の書き出し（cp932・区分ごとに「合計」と「明細」が並ぶ）を読み、
特定口座・新NISA（成長投資枠・つみたて投資枠）・旧NISA・口座横断の 4 つの見方で数字を並べる。
**売買の助言は出さない**——出すのは数字と、利用者の方針（NISA に優待株を入れない）に合っているかの印だけ。

任意で 2 つを足せる（#1307）。特定口座の譲渡損益の明細（`--realized`・cp932）を渡すと、今年すでに確定した
損益と含み損を合わせた損益通算の数字を出す。旧NISA の買付年の表（`--old-nisa-years`・既定は
`.portfolio/old_nisa_years.csv` があれば読む）があると、銘柄ごとに非課税期間の終わる日と残りの日数を出す
（保有一覧にも取引履歴の書き出しにも、旧NISA の買付年が入らないため。取引履歴は直近 2 年分しか出ない）。

## 何を照合するか（黙って捨てない）

1. 区分の見出しが既知であること（知らない口座・商品の区分は raise）
2. 列名の行が既知の並びであること、明細の列の数がそれと同じであること
3. 明細ごとに 評価額 − 取得金額 = 評価損益 であること
4. 区分ごとに 明細の評価額・評価損益の和 = その区分の「合計」行 であること
5. 譲渡損益の明細: 期間が基準日の年の 1 月 1 日からであること・全件の書き出しであること・知らない取引と
   税の区分が無いこと・受渡日がその年であること・明細の件数と 4 つの合計（損益・譲渡益税・配当・配当所得税）が
   明細の和と合うこと
6. 買付年の表: 買付年が 2014〜2023 年であること・同じ銘柄が 2 行無いこと（保有に無い行は捨てずに出す）

## 置き場（リポジトリが PUBLIC）

保有の CSV・明細・優待一覧・買付年の表・レポートは `.portfolio/`（`.gitignore`）に置き、コミットしない。
テストの値はすべて作り物。DB へは**書かない**。読むのは資産の種類（JPX の市場区分）・業種・配当利回りの補足だけで、
`--no-db` で飛ばせる（そのときは「未取得」と出す）。

実行（リポジトリの直下で）:
    python -I scripts/holdings_report.py --csv .portfolio/raw/<書き出し>.csv
        [--realized .portfolio/raw/<明細>.csv] [--old-nisa-years .portfolio/old_nisa_years.csv]
        [--yutai .portfolio/yutai.csv] [--as-of YYYY-MM-DD] [--no-db] [--out <path>]

`-I`（カレントフォルダから Python のモジュールを読まない）でも動くよう、先頭で自分のリポジトリを
`sys.path` に足す。DL した資料のフォルダ（`.portfolio/` と CSV のあるフォルダ）の中からは実行させない。
標準出力は ASCII の要約だけ（銘柄名は HTML にだけ書く）。
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio_ledger import normalize_sec_code  # noqa: E402

# ── 置き場と終了コード ────────────────────────────────────────────────────────
DEFAULT_DIR = ROOT / ".portfolio"
EXIT_NG = 2       # 入力が読めない・照合に合わない・資料のフォルダの中から実行した
EXIT_DB = 1       # DB から補足を読めない（`--no-db` で飛ばせる）

# ── 税と制度（2026-11-22 に公式ページで確認） ────────────────────────────────
# 上場株式等の配当・譲渡益の税率（所得税 15% + 復興特別所得税 0.315% + 住民税 5%）。
#   配当: https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1331.htm （20.315%）
#   譲渡: https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1463.htm （20% + 復興特別所得税）
TAX_RATE = 0.20315
# 2024 年からの NISA の非課税保有限度額（簿価で計算し、売れば翌年以降に枠が戻る）。
#   https://www.fsa.go.jp/policy/nisa2/know/
NISA_LIFETIME_LIMIT = 18_000_000
NISA_GROWTH_LIFETIME_LIMIT = 12_000_000
# NISA 口座の損失は「ないものとみなされる」（他の口座と損益通算・繰越控除できない）。
#   https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1535.htm
# 2023 年までの一般NISA: 非課税期間 5 年・買付は 2014〜2023 年・新しい NISA へは移せない。
#   https://www.fsa.go.jp/policy/nisa2/till2023/
# 期間が終わって課税口座へ移ったときの取得価額は、上の公式ページでは確かめられなかった。利用中の証券会社の
# 案内（2026-11-22 に画面で確認）は「翌年の初めに課税口座へ払い出し、期間の最後の年の最終営業日の時価で受け入れる」。
# レポートは「今の株価で期間が終わった場合」として近似で出し、そう書く。
# 同じ銘柄を 2 つの年に買って一部を売ったときは古い年の分から減る扱い（複数の証券会社の FAQ。法令の条文は
# 確かめていない）。買付年の表は 1 銘柄 1 行なので、2 つの年に分かれる銘柄は書けず、候補の表示に残る。
OLD_NISA_YEARS = 5
OLD_NISA_FIRST_BUY_YEAR = 2014
OLD_NISA_LAST_BUY_YEAR = 2023
SOURCES = (
    ("国税庁 No.1331 上場株式等の配当等に係る申告分離課税制度",
     "https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1331.htm"),
    ("国税庁 No.1463 株式等を譲渡したときの課税（申告分離課税）",
     "https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1463.htm"),
    ("国税庁 No.1535 NISA（非課税口座）の概要",
     "https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1535.htm"),
    ("金融庁 NISA を知る", "https://www.fsa.go.jp/policy/nisa2/know/"),
    ("金融庁 2023 年までの NISA", "https://www.fsa.go.jp/policy/nisa2/till2023/"),
)

# 配当利回り（%）がこれを超える値は異常値として使わない（plugins/gap_analysis.py の `_DIV_YIELD_CAP` と同じ考え）。
DIV_YIELD_CAP = 30.0

# ── 口座と区分 ────────────────────────────────────────────────────────────────
ACCOUNT_TAXABLE = "taxable"
ACCOUNT_GENERAL = "general"
ACCOUNT_NISA_GROWTH = "nisa_growth"
ACCOUNT_NISA_TSUMITATE = "nisa_tsumitate"
ACCOUNT_OLD_NISA = "old_nisa"
ACCOUNT_ORDER = (ACCOUNT_TAXABLE, ACCOUNT_GENERAL, ACCOUNT_NISA_GROWTH, ACCOUNT_NISA_TSUMITATE,
                 ACCOUNT_OLD_NISA)
ACCOUNT_NAMES = {
    ACCOUNT_TAXABLE: "特定口座",
    ACCOUNT_GENERAL: "一般口座",
    ACCOUNT_NISA_GROWTH: "新NISA 成長投資枠",
    ACCOUNT_NISA_TSUMITATE: "新NISA つみたて投資枠",
    ACCOUNT_OLD_NISA: "旧NISA",
}
TAXABLE_ACCOUNTS = (ACCOUNT_TAXABLE, ACCOUNT_GENERAL)
NISA_ACCOUNTS = (ACCOUNT_NISA_GROWTH, ACCOUNT_NISA_TSUMITATE)
# CSV の区分見出しの括弧の中（投信は先頭の `金額/` `口数/` を外したもの）→ 口座
ACCOUNT_OF_LABEL = {
    "特定預り": ACCOUNT_TAXABLE,
    "一般預り": ACCOUNT_GENERAL,
    "NISA預り（成長投資枠）": ACCOUNT_NISA_GROWTH,
    "NISA預り（つみたて投資枠）": ACCOUNT_NISA_TSUMITATE,
    "旧NISA預り": ACCOUNT_OLD_NISA,
}
FUND_UNITS = ("金額", "口数")

KIND_STOCK, KIND_FUND = "stock", "fund"
KIND_OF_HEAD = {"株式": KIND_STOCK, "投資信託": KIND_FUND}

TITLE = "保有証券一覧"
STOCK_COLUMNS = ("銘柄コード", "銘柄名称", "保有株数", "売却注文中", "取得単価", "現在値", "取得金額",
                 "評価額", "評価損益")
FUND_COLUMNS = ("ファンド名", "保有口数", "売却注文中", "取得単価", "基準価額", "取得金額", "評価額",
                "評価損益", "分配金受取方法")
TOTAL_COLUMNS = ("評価額合計", "評価損益合計")
# 区分の見出し: `株式（特定預り）` / `投資信託（金額/NISA預り（成長投資枠））合計` など（括弧は全角）
_HEAD = re.compile(r"^(株式|投資信託)（(.+)）(合計)?$")

# ── 資産の種類 ────────────────────────────────────────────────────────────────
ASSET_STOCK, ASSET_REIT, ASSET_ETF = "stock", "reit", "etf"
ASSET_FUND, ASSET_OTHER, ASSET_UNKNOWN = "fund", "other", "unknown"
ASSET_ORDER = (ASSET_STOCK, ASSET_REIT, ASSET_ETF, ASSET_FUND, ASSET_OTHER, ASSET_UNKNOWN)
ASSET_NAMES = {
    ASSET_STOCK: "個別株",
    ASSET_REIT: "REIT 等",
    ASSET_ETF: "ETF・ETN",
    ASSET_FUND: "投資信託",
    ASSET_OTHER: "その他",
    ASSET_UNKNOWN: "種類不明（DB 未取得）",
}
# JPX data_j の「市場・商品区分」（`collector_utils.JPX_NON_MARKET_SEGMENTS` と同じ値。テストが照合する）
SEGMENT_ETF = "ETF・ETN"
SEGMENT_REIT_PREFIX = "REIT"
SEGMENT_OTHER = "出資証券"

# ── 優待一覧（`.portfolio/yutai.csv`・UTF-8） ─────────────────────────────────
YUTAI_YES, YUTAI_NO, YUTAI_UNKNOWN = "あり", "なし", "不明"
YUTAI_STATUSES = (YUTAI_YES, YUTAI_NO, YUTAI_UNKNOWN)
YUTAI_REQUIRED = ("code", "has_yutai")
YUTAI_COLUMNS = ("code", "name", "has_yutai", "min_shares", "record_months", "content", "est_value_yen",
                 "source_url", "checked_on", "confirmed")

# ── 特定口座の譲渡損益の明細（cp932・2026-11-22 の書き出しから写した） ─────────
# 見出し・期間・4 つの合計・注記・列名の行・明細の順に並ぶ。明細は売り・配当の行（地方税の列が無く 12 列）と、
# 税の行（1 列目が税の区分で 13 列）が混ざる。
REALIZED_TITLE = "特定口座損益明細"
REALIZED_META_COLUMNS = ("受渡開始年月日", "受渡終了年月日", "明細数", "明細指定開始", "明細指定終了")
REALIZED_TOTALS = {               # 合計の見出し → 鍵
    "譲渡益税徴収額合計": "sale_tax",
    "損益金額合計": "sale_pnl",
    "配当所得税徴収額合計": "div_tax",
    "配当金額等合計 (税引き前)": "dividends",
}
REALIZED_NOTE_PREFIX = "（注）"
REALIZED_COLUMNS = ("銘柄コード", "銘柄", "譲渡益取消区分", "約定日", "数量", "取引", "受渡日", "売却/決済金額",
                    "費用", "取得/新規年月日", "取得/新規金額", "損益金額/徴収額", "地方税")
TRADE_SELL, TRADE_DIVIDEND = "現物売", "株式配当金"
TRADE_TOTAL = {TRADE_SELL: "sale_pnl", TRADE_DIVIDEND: "dividends"}
# 税の行の 1 列目 → (合計の鍵, 符号)。配当所得税還付金は 2026-11-22 の書き出しに無く、譲渡益税と対の名前で置いた
TAX_ROWS = {
    "譲渡益税徴収額": ("sale_tax", 1),
    "譲渡益税還付金": ("sale_tax", -1),
    "配当所得税徴収額": ("div_tax", 1),
    "配当所得税還付金": ("div_tax", -1),
}
REALIZED_NONE = "--"              # 値の無い欄（損益が 0 の売りの損益など）
_JP_DATE = re.compile(r"^(\d{4})年(\d{2})月(\d{2})日$")

# ── 旧NISA の買付年の表（`.portfolio/old_nisa_years.csv`・UTF-8・手で書く） ──────
OLD_NISA_YEARS_COLUMNS = ("code", "buy_year")


class HoldingsCsvError(ValueError):
    """CSV・優待一覧が読めない、または照合に合わない。"""


# ── 純関数ブロック（database を import しない） ──────────────────────────────

@dataclass(frozen=True)
class Holding:
    account: str
    kind: str                 # KIND_STOCK（株式の欄）/ KIND_FUND（投資信託の欄）
    code: Optional[str]       # 証券コード（投信は None）
    name: str                 # NFKC で全角英数をそろえた名前
    quantity: float           # 株数・口数
    avg_cost: float           # 取得単価（投信は 1 万口あたり）
    price: float              # 現在値・基準価額
    cost: int                 # 取得金額
    value: int                # 評価額
    pnl: int                  # 評価損益

    @property
    def key(self) -> str:
        """口座をまたいで同じ銘柄・同じ投信をまとめる鍵。"""
        return self.code if self.code else f"fund:{self.name}"


@dataclass(frozen=True)
class ParsedHoldings:
    holdings: tuple
    totals: Mapping           # {(kind, account): (評価額合計, 評価損益合計)}


@dataclass(frozen=True)
class Yutai:
    code: str
    name: str
    status: str               # あり / なし / 不明
    min_shares: Optional[float]
    record_months: str
    content: str
    est_value_yen: Optional[int]
    source_url: str
    checked_on: str
    confirmed: bool           # 利用者が確かめた（confirmed 列が空でない）


@dataclass(frozen=True)
class RealizedRow:
    trade: str                # TRADE_SELL / TRADE_DIVIDEND
    code: str
    trade_date: date
    settle_date: date
    quantity: float
    amount: int               # 売りは損益金額、配当は配当金額（税引き前）


@dataclass(frozen=True)
class Realized:
    start: date
    end: date
    rows: tuple
    sale_pnl: int             # 確定した損益の合計
    sale_tax: int             # 源泉徴収された譲渡益税（徴収 − 還付）
    dividends: int            # 配当金額の合計（税引き前）
    div_tax: int              # 源泉徴収された配当所得税（徴収 − 還付）

    @property
    def sells(self) -> int:
        return sum(1 for r in self.rows if r.trade == TRADE_SELL)


@dataclass(frozen=True)
class SecurityInfo:
    segment: Optional[str] = None     # JPX の市場・商品区分（生値）
    industry: Optional[str] = None    # 東証 33 業種
    div_yield: Optional[float] = None  # 直近期の配当利回り（%・期末株価基準）


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip()


def _decimal(raw: str, what: str) -> Decimal:
    s = raw.strip().replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise HoldingsCsvError(f"{what} が数値でない: {raw!r}") from None


def _int(raw: str, what: str) -> int:
    v = _decimal(raw, what)
    if v != v.to_integral_value():
        raise HoldingsCsvError(f"{what} が整数でない: {raw!r}")
    return int(v)


def _float(raw: str, what: str) -> float:
    return float(_decimal(raw, what))


def _section(m: re.Match, line_no: int) -> tuple:
    kind = KIND_OF_HEAD[m.group(1)]
    label = m.group(2)
    if kind == KIND_FUND:
        unit, sep, rest = label.partition("/")
        if not sep or unit not in FUND_UNITS:
            raise HoldingsCsvError(f"{line_no} 行目: 投資信託の区分 {m.group(0)!r} の形を知らない")
        label = rest
    account = ACCOUNT_OF_LABEL.get(label)
    if account is None:
        raise HoldingsCsvError(f"{line_no} 行目: 知らない口座の区分 {m.group(0)!r}（{sorted(ACCOUNT_OF_LABEL)}）")
    return kind, account, bool(m.group(3))


def _is_heading(row: list) -> bool:
    return len(row) == 1 and (row[0] == TITLE or bool(_HEAD.match(row[0])))


def _expect_columns(rows: list, i: int, columns: tuple, where: str) -> None:
    got = tuple(rows[i]) if i < len(rows) else ()
    if got != columns:
        raise HoldingsCsvError(f"{where}: 列名の行が {got!r}（期待は {columns!r}）")


def _holding(row: list, kind: str, account: str, where: str) -> Holding:
    columns = STOCK_COLUMNS if kind == KIND_STOCK else FUND_COLUMNS
    if len(row) != len(columns):
        raise HoldingsCsvError(f"{where}: 明細の列の数が {len(row)}（列名は {len(columns)}）: {row[:2]!r}")
    c = dict(zip(columns, row))
    if kind == KIND_STOCK:
        try:
            code = normalize_sec_code(c["銘柄コード"])
        except ValueError as e:
            raise HoldingsCsvError(f"{where}: {e}") from None
        name, qty_raw, price_raw = c["銘柄名称"], c["保有株数"], c["現在値"]
    else:
        code, name, price_raw = None, c["ファンド名"], c["基準価額"]
        qty_raw = c["保有口数"].removesuffix("口")
    h = Holding(
        account=account, kind=kind, code=code, name=_nfkc(name),
        quantity=_float(qty_raw, f"{where} の数量"),
        avg_cost=_float(c["取得単価"], f"{where} の取得単価"),
        price=_float(price_raw, f"{where} の現在値"),
        cost=_int(c["取得金額"], f"{where} の取得金額"),
        value=_int(c["評価額"], f"{where} の評価額"),
        pnl=_int(c["評価損益"], f"{where} の評価損益"),
    )
    if h.value - h.cost != h.pnl:
        raise HoldingsCsvError(f"{where}: {h.name} の 評価額 {h.value} − 取得金額 {h.cost} ≠ 評価損益 {h.pnl}")
    return h


def parse_holdings_csv(data: bytes) -> ParsedHoldings:
    """保有証券一覧の書き出し（cp932）を読む。形が違う・合計が合わないときは HoldingsCsvError。"""
    try:
        text = data.decode("cp932")
    except UnicodeDecodeError as e:
        raise HoldingsCsvError(f"cp932 として読めない（{e}）") from None
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    rows = [r for r in rows if any(r)]          # 空行は区切りでしかない
    holdings: list = []
    totals: dict = {}
    details: set = set()
    i = 0
    while i < len(rows):
        row = rows[i]
        line_no = i + 1
        if len(row) == 1 and row[0] == TITLE:
            i += 1
            continue
        m = _HEAD.match(row[0]) if len(row) == 1 else None
        if m is None:
            raise HoldingsCsvError(f"{line_no} 行目（空行を除く）: 区分の見出しのはずの行が {row[:3]!r}")
        kind, account, is_total = _section(m, line_no)
        key = (kind, account)
        where = m.group(0)
        i += 1
        if is_total:
            _expect_columns(rows, i, TOTAL_COLUMNS, where)
            i += 1
            vals = rows[i] if i < len(rows) else []
            if len(vals) != len(TOTAL_COLUMNS) or key in totals:
                raise HoldingsCsvError(f"{where}: 合計の行が無い・重複している（{vals!r}）")
            totals[key] = (_int(vals[0], f"{where} の評価額合計"), _int(vals[1], f"{where} の評価損益合計"))
            i += 1
            continue
        _expect_columns(rows, i, STOCK_COLUMNS if kind == KIND_STOCK else FUND_COLUMNS, where)
        i += 1
        if key in details:
            raise HoldingsCsvError(f"{where}: 明細の区分が 2 回出てきた")
        details.add(key)
        while i < len(rows) and not _is_heading(rows[i]):
            holdings.append(_holding(rows[i], kind, account, where))
            i += 1
    for key in sorted(details | set(totals)):
        name = f"{'株式' if key[0] == KIND_STOCK else '投資信託'}（{ACCOUNT_NAMES[key[1]]}）"
        if key not in totals or key not in details:
            raise HoldingsCsvError(f"{name}: 合計と明細の片方しか無い")
        got = (sum(h.value for h in holdings if (h.kind, h.account) == key),
               sum(h.pnl for h in holdings if (h.kind, h.account) == key))
        if got != totals[key]:
            raise HoldingsCsvError(f"{name}: 明細の和（評価額 {got[0]}・評価損益 {got[1]}）が合計の行 {totals[key]} と違う")
    if not holdings:
        raise HoldingsCsvError("保有の明細が 1 行も無い")
    return ParsedHoldings(holdings=tuple(holdings), totals=totals)


def _opt_float(raw: Optional[str], what: str) -> Optional[float]:
    s = (raw or "").strip()
    return _float(s, what) if s else None


def _opt_int(raw: Optional[str], what: str) -> Optional[int]:
    s = (raw or "").strip()
    return _int(s, what) if s else None


def parse_yutai_csv(text: str) -> dict:
    """優待一覧（UTF-8・BOM 可）を `{証券コード: Yutai}` にする。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in YUTAI_REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise HoldingsCsvError(f"優待一覧に列 {missing} が無い（列: {YUTAI_COLUMNS}）")
    out: dict = {}
    for n, r in enumerate(reader, start=2):
        where = f"優待一覧の {n} 行目"
        try:
            code = normalize_sec_code(r["code"])
        except ValueError as e:
            raise HoldingsCsvError(f"{where}: {e}") from None
        status = (r["has_yutai"] or "").strip()
        if status not in YUTAI_STATUSES:
            raise HoldingsCsvError(f"{where}: has_yutai は {YUTAI_STATUSES} のどれか（{status!r}）")
        if code in out:
            raise HoldingsCsvError(f"{where}: {code} が 2 回出てきた")
        out[code] = Yutai(
            code=code, name=(r.get("name") or "").strip(), status=status,
            min_shares=_opt_float(r.get("min_shares"), f"{where} の min_shares"),
            record_months=(r.get("record_months") or "").strip(),
            content=(r.get("content") or "").strip(),
            est_value_yen=_opt_int(r.get("est_value_yen"), f"{where} の est_value_yen"),
            source_url=(r.get("source_url") or "").strip(),
            checked_on=(r.get("checked_on") or "").strip(),
            confirmed=bool((r.get("confirmed") or "").strip()),
        )
    return out


def _jp_date(raw: str, what: str) -> date:
    m = _JP_DATE.match(raw)
    if m is None:
        raise HoldingsCsvError(f"{what} が YYYY年MM月DD日 の形でない: {raw!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _slash_date(raw: str, what: str) -> date:
    try:
        return datetime.strptime(raw, "%Y/%m/%d").date()
    except ValueError:
        raise HoldingsCsvError(f"{what} が YYYY/MM/DD の形でない: {raw!r}") from None


def _realized_header(rows: list, year: int) -> tuple:
    """見出し・期間・合計を読み、(開始日, 終了日, 明細数, 合計, 列名の行の位置) を返す。"""
    if len(rows) < 3 or rows[0] != [REALIZED_TITLE] or tuple(rows[1]) != REALIZED_META_COLUMNS:
        raise HoldingsCsvError(f"明細: 先頭が {REALIZED_TITLE!r} と期間の列名の行でない（{rows[:2]!r}）")
    meta = rows[2]
    if len(meta) != len(REALIZED_META_COLUMNS):
        raise HoldingsCsvError(f"明細: 期間の行の列の数が {len(meta)}（期待は {len(REALIZED_META_COLUMNS)}）")
    start = _jp_date(meta[0], "明細の受渡開始年月日")
    end = _jp_date(meta[1], "明細の受渡終了年月日")
    count, first, last = (_int(v, f"明細の{n}") for v, n in zip(meta[2:], REALIZED_META_COLUMNS[2:]))
    if start != date(year, 1, 1) or end.year != year:
        raise HoldingsCsvError(f"明細: 期間 {start}〜{end} が {year} 年の 1 月 1 日からでない"
                               "（損益通算は暦年で区切られるので、その年の確定損益にならない）")
    if (first, last) != (1, count):
        raise HoldingsCsvError(f"明細: 全 {count} 件のうち {first}〜{last} 件目だけの書き出し（全件を書き出す）")
    totals: dict = {}
    i = 3
    while i < len(rows) and tuple(rows[i]) != REALIZED_COLUMNS:
        row = rows[i]
        if len(row) == 1 and row[0] in REALIZED_TOTALS and i + 1 < len(rows) and len(rows[i + 1]) == 1:
            key = REALIZED_TOTALS[row[0]]
            if key in totals:
                raise HoldingsCsvError(f"明細: {row[0]} が 2 回出てきた")
            totals[key] = _int(rows[i + 1][0], f"明細の{row[0]}")
            i += 2
        elif len(row) == 1 and row[0].startswith(REALIZED_NOTE_PREFIX):
            i += 1
        else:
            raise HoldingsCsvError(f"明細の {i + 1} 行目（空行を除く）: 合計か列名の行のはずが {row[:3]!r}")
    if i == len(rows):
        raise HoldingsCsvError(f"明細: 列名の行 {REALIZED_COLUMNS!r} が無い")
    missing = [k for k, v in REALIZED_TOTALS.items() if v not in totals]
    if missing:
        raise HoldingsCsvError(f"明細: 合計 {missing} が無い")
    return start, end, count, totals, i


def parse_realized_csv(data: bytes, year: int) -> Realized:
    """特定口座の譲渡損益の明細（cp932）を読む。`year` の 1 月 1 日からの全件で、明細の和が 4 つの合計と合うこと。"""
    try:
        text = data.decode("cp932")
    except UnicodeDecodeError as e:
        raise HoldingsCsvError(f"明細: cp932 として読めない（{e}）") from None
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    rows = [r for r in rows if any(r)]          # 空行は区切りでしかない
    start, end, count, totals, head = _realized_header(rows, year)
    sums = dict.fromkeys(REALIZED_TOTALS.values(), 0)
    details: list = []
    for n, row in enumerate(rows[head + 1:], start=head + 2):
        where = f"明細の {n} 行目（空行を除く）"
        tax = TAX_ROWS.get(row[0])
        width = len(REALIZED_COLUMNS) if tax else len(REALIZED_COLUMNS) - 1
        if len(row) != width:
            raise HoldingsCsvError(f"{where}: 列の数が {len(row)}（期待は {width}）: {row[:1]!r}")
        c = dict(zip(REALIZED_COLUMNS, row))
        settle = _slash_date(c["受渡日"], f"{where} の受渡日")
        if settle.year != year:
            raise HoldingsCsvError(f"{where}: 受渡日 {settle} が {year} 年でない")
        if tax:
            key, sign = tax
            sums[key] += sign * _int(c["損益金額/徴収額"], f"{where} の{row[0]}")
            continue
        trade = c["取引"]
        if trade not in TRADE_TOTAL:
            raise HoldingsCsvError(f"{where}: 知らない取引の区分 {trade!r}（{sorted(TRADE_TOTAL)}）・"
                                   f"または知らない税の区分 {row[0]!r}（{sorted(TAX_ROWS)}）")
        if c["譲渡益取消区分"]:
            raise HoldingsCsvError(f"{where}: 譲渡益取消区分が空でない行（{c['譲渡益取消区分']!r}）は読み方を知らない")
        try:
            code = normalize_sec_code(c["銘柄コード"])
        except ValueError as e:
            raise HoldingsCsvError(f"{where}: {e}") from None
        raw = c["損益金額/徴収額"]
        amount = 0 if raw == REALIZED_NONE else _int(raw, f"{where} の損益金額")
        details.append(RealizedRow(
            trade=trade, code=code, trade_date=_slash_date(c["約定日"], f"{where} の約定日"), settle_date=settle,
            quantity=_float(c["数量"].removesuffix("株"), f"{where} の数量"), amount=amount))
        sums[TRADE_TOTAL[trade]] += amount
    if len(details) != count:
        raise HoldingsCsvError(f"明細: 売り・配当の行が {len(details)} 件（明細数は {count}）")
    for label, key in REALIZED_TOTALS.items():
        if sums[key] != totals[key]:
            raise HoldingsCsvError(f"明細: 明細の和 {sums[key]} が {label} {totals[key]} と違う")
    return Realized(start=start, end=end, rows=tuple(details), sale_pnl=totals["sale_pnl"],
                    sale_tax=totals["sale_tax"], dividends=totals["dividends"], div_tax=totals["div_tax"])


def parse_old_nisa_years_csv(text: str) -> dict:
    """旧NISA の買付年の表（UTF-8・BOM 可・列 code,buy_year）を `{証券コード: 買付年}` にする。"""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in OLD_NISA_YEARS_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise HoldingsCsvError(f"旧NISA の買付年の表に列 {missing} が無い（列: {OLD_NISA_YEARS_COLUMNS}）")
    out: dict = {}
    for n, r in enumerate(reader, start=2):
        where = f"旧NISA の買付年の表の {n} 行目"
        try:
            code = normalize_sec_code(r["code"])
        except ValueError as e:
            raise HoldingsCsvError(f"{where}: {e}") from None
        year = _int(r["buy_year"] or "", f"{where} の buy_year")
        if not OLD_NISA_FIRST_BUY_YEAR <= year <= OLD_NISA_LAST_BUY_YEAR:
            raise HoldingsCsvError(f"{where}: buy_year は {OLD_NISA_FIRST_BUY_YEAR}〜{OLD_NISA_LAST_BUY_YEAR}（{year}）")
        if code in out:
            raise HoldingsCsvError(f"{where}: {code} が 2 回出てきた（2 つの年に分かれる銘柄はこの表に書けない）")
        out[code] = year
    return out


def asset_type_of(holding: Holding, segment: Optional[str]) -> str:
    """資産の種類。投信の欄は投資信託、株式の欄は JPX の市場区分で分ける（区分が無ければ不明）。"""
    if holding.kind == KIND_FUND:
        return ASSET_FUND
    if segment is None:
        return ASSET_UNKNOWN
    if segment == SEGMENT_ETF:
        return ASSET_ETF
    if segment.startswith(SEGMENT_REIT_PREFIX):
        return ASSET_REIT
    if segment == SEGMENT_OTHER:
        return ASSET_OTHER
    return ASSET_STOCK


def merge_security_info(codes: Sequence[str], segments: Mapping, industries: Mapping,
                        yields: Mapping) -> dict:
    """DB から読んだ 3 つの対応表を `{証券コード: SecurityInfo}` にまとめる。配当利回りの異常値は捨てる。"""
    out = {}
    for code in codes:
        y = yields.get(code)
        if y is not None and not (0 <= y <= DIV_YIELD_CAP):
            y = None
        ind = industries.get(code)
        out[code] = SecurityInfo(segment=segments.get(code), industry=ind if ind and ind != "-" else None,
                                 div_yield=y)
    return out


def _tax(amount: float) -> int:
    """税の見込み（マイナスは 0・円未満は切り捨て）。"""
    return int(max(amount, 0) * TAX_RATE)


def _pct(n: float, d: float) -> Optional[float]:
    return n / d * 100 if d else None


def possible_old_nisa_expiries(as_of: date) -> list:
    """`as_of` の時点でまだ終わっていない一般NISA の非課税期間の終わりの日（買付年が分からないときの候補）。"""
    out = []
    for year in range(OLD_NISA_FIRST_BUY_YEAR, OLD_NISA_LAST_BUY_YEAR + 1):
        end = old_nisa_expiry(year)
        if end >= as_of:
            out.append(end)
    return out


def old_nisa_expiry(buy_year: int) -> date:
    """一般NISA の買付年 → 非課税期間の終わりの日（その年を含む 5 年目の 12 月 31 日）。"""
    if not OLD_NISA_FIRST_BUY_YEAR <= buy_year <= OLD_NISA_LAST_BUY_YEAR:
        raise ValueError(f"一般NISA の買付年は {OLD_NISA_FIRST_BUY_YEAR}〜{OLD_NISA_LAST_BUY_YEAR}（{buy_year}）")
    return date(buy_year + OLD_NISA_YEARS - 1, 12, 31)


def _assets(holdings: Sequence[Holding], info: Mapping) -> dict:
    return {h: asset_type_of(h, info[h.code].segment if h.code in info else None) for h in holdings}


def _qty_by_key(holdings: Sequence[Holding]) -> dict:
    out: dict = {}
    for h in holdings:
        out[h.key] = out.get(h.key, 0.0) + h.quantity
    return out


def _dividends(hs: Sequence[Holding], info: Mapping) -> tuple:
    """配当の年額の見込み（評価額 × 直近期の配当利回り）と、利回りが無い保有。"""
    total, missing = 0.0, []
    for h in hs:
        y = info[h.code].div_yield if h.code in info else None
        if y is None:
            missing.append(h)
        else:
            total += h.value * y / 100
    return int(total), missing


def account_summary(holdings: Sequence[Holding], assets: Mapping) -> dict:
    """口座 × 資産の種類ごとの件数・取得額・評価額・含み損益。"""
    cells: dict = {}
    for h in holdings:
        c = cells.setdefault((h.account, assets[h]), {"count": 0, "cost": 0, "value": 0, "pnl": 0})
        c["count"] += 1
        c["cost"] += h.cost
        c["value"] += h.value
        c["pnl"] += h.pnl
    rows, subtotals = [], []
    for account in ACCOUNT_ORDER:
        acc = [dict(account=account, asset=a, **cells[(account, a)]) for a in ASSET_ORDER if (account, a) in cells]
        if not acc:
            continue
        rows.extend(acc)
        subtotals.append({"account": account, **{k: sum(r[k] for r in acc) for k in ("count", "cost", "value", "pnl")}})
    total = {k: sum(s[k] for s in subtotals) for k in ("count", "cost", "value", "pnl")}
    for r in (*rows, *subtotals, total):
        r["pnl_pct"] = _pct(r["pnl"], r["cost"])
    return {"rows": rows, "subtotals": subtotals, "total": total}


def realized_netting(realized: Realized, holdings: Sequence[Holding]) -> dict:
    """今年の確定損益 R と特定口座の含み損 L の損益通算。源泉徴収された税のうち、L を確定すると戻る見込み。

    戻る見込み = 徴収された譲渡益税 − (R + L) にかかる税の見込み（0 未満は 0）。一般口座の含み損は特定口座の
    源泉徴収と自動では相殺されないので L に入れない。通算しきれない損と配当の相殺は数えない。
    """
    loss = sum(h.pnl for h in holdings if h.account == ACCOUNT_TAXABLE and h.pnl < 0)
    netted = realized.sale_pnl + loss
    tax_after = _tax(netted)
    return {
        "pnl": realized.sale_pnl,
        "sells": realized.sells,
        "start": realized.start,
        "end": realized.end,
        "tax_withheld": realized.sale_tax,
        "loss": loss,
        "netted": netted,
        "tax_after": tax_after,
        "refund": max(realized.sale_tax - tax_after, 0),
        # 確定損益が損のとき、その分までは含み益を確定しても税がかからない
        "room": -realized.sale_pnl if realized.sale_pnl < 0 else 0,
        "dividends": realized.dividends,
        "div_tax": realized.div_tax,
    }


def taxable_view(holdings: Sequence[Holding], info: Mapping, yutai: Mapping,
                 realized: Optional[Realized] = None) -> dict:
    """特定口座・一般口座: 含み益にかかる税の見込み・含み損・配当にかかる税・優待の株数・確定損益との通算。"""
    hs = [h for h in holdings if h.account in TAXABLE_ACCOUNTS]
    qty_all = _qty_by_key(holdings)
    gains = sum(h.pnl for h in hs if h.pnl > 0)
    losses = sum(h.pnl for h in hs if h.pnl < 0)
    div_total, div_missing = _dividends([h for h in hs if h.kind == KIND_STOCK], info)
    yutai_rows = []
    for h in hs:
        y = yutai.get(h.code) if h.code else None
        if y is not None and y.status == YUTAI_YES:
            yutai_rows.append({"holding": h, "yutai": y, "qty_here": h.quantity, "qty_all": qty_all[h.key]})
    return {
        "holdings": hs,
        "gains": gains,
        "losses": losses,
        "tax_gains_only": _tax(gains),
        "tax_netted": _tax(gains + losses),
        "loss_rows": sorted((h for h in hs if h.pnl < 0), key=lambda h: h.pnl),
        "div_total": div_total,
        "div_tax": _tax(div_total),
        "div_missing": div_missing,
        "yutai_rows": yutai_rows,
        "realized": realized_netting(realized, hs) if realized is not None else None,
    }


def nisa_view(holdings: Sequence[Holding], info: Mapping, yutai: Mapping, assets: Mapping) -> dict:
    """新NISA: 優待の印（一律）・含み損・非課税で浮く税の見込み・生涯枠の使用。"""
    hs = [h for h in holdings if h.account in NISA_ACCOUNTS]
    qty_all = _qty_by_key(holdings)
    # ETF と投信は優待の確認から外す（株式の欄で種類が分からないものは確認に残す）
    checkable = [h for h in hs if h.kind == KIND_STOCK and assets[h] != ASSET_ETF]
    flagged, unconfirmed, without = [], [], []
    for h in checkable:
        y = yutai.get(h.code)
        if y is None or y.status == YUTAI_UNKNOWN:
            unconfirmed.append(h)
        elif y.status == YUTAI_NO:
            without.append((h, y))
        else:
            price_at_min = (y.min_shares or 0) * h.price
            div = info[h.code].div_yield if h.code in info else None
            flagged.append({
                "holding": h, "yutai": y, "qty_all": qty_all[h.key],
                "reaches_min": None if y.min_shares is None else qty_all[h.key] >= y.min_shares,
                "yutai_yield": _pct(y.est_value_yen, price_at_min) if y.est_value_yen and price_at_min else None,
                "div_yield": div,
            })
    gains = sum(h.pnl for h in hs if h.pnl > 0)
    div_total, div_missing = _dividends([h for h in hs if h.kind == KIND_STOCK], info)
    used_total = sum(h.cost for h in hs)
    used_growth = sum(h.cost for h in hs if h.account == ACCOUNT_NISA_GROWTH)
    return {
        "holdings": hs,
        "flagged": flagged,
        "unconfirmed": unconfirmed,
        "without": without,
        "without_confirmed": sum(1 for _, y in without if y.confirmed),
        "loss_rows": sorted((h for h in hs if h.pnl < 0), key=lambda h: h.pnl),
        "loss_total": sum(h.pnl for h in hs if h.pnl < 0),
        "gains": gains,
        "tax_free_gains": _tax(gains),
        "div_total": div_total,
        "tax_free_div": _tax(div_total),
        "div_missing": div_missing,
        "used_total": used_total,
        "used_growth": used_growth,
        "left_total": NISA_LIFETIME_LIMIT - used_total,
        "left_growth": NISA_GROWTH_LIFETIME_LIMIT - used_growth,
    }


def old_nisa_view(holdings: Sequence[Holding], yutai: Mapping, as_of: date,
                  buy_years: Optional[Mapping] = None) -> dict:
    """旧NISA: 銘柄ごとの非課税期間の終わる日（買付年の表から）と、今の株価で終わった場合の税の影響。"""
    buy_years = buy_years or {}
    hs = [h for h in holdings if h.account == ACCOUNT_OLD_NISA]
    rows = []
    for h in hs:
        y = yutai.get(h.code) if h.code else None
        year = buy_years.get(h.code) if h.code else None
        end = old_nisa_expiry(year) if year is not None else None
        rows.append({
            "holding": h,
            "buy_year": year,
            "expiry": end,
            "days_left": (end - as_of).days if end is not None else None,
            "gain": h.pnl >= 0,
            # 益: 非課税のまま確定する益にかからずに済む税 / 損: 元の取得単価まで戻ったときにかかる税
            "tax_effect": _tax(abs(h.pnl)),
            "yutai": y.status if y else None,
        })
    rows.sort(key=lambda r: (r["expiry"] is None, r["expiry"] or date.max, r["holding"].key))
    unknown = sum(1 for r in rows if r["expiry"] is None)
    held = {h.code for h in hs if h.code}
    return {
        "rows": rows,
        "unknown": unknown,
        # 候補の日付は、買付年が分からない銘柄があるときだけ出す
        "expiries": possible_old_nisa_expiries(as_of) if unknown else [],
        "years_given": len(buy_years),
        "not_held": sorted(c for c in buy_years if c not in held),
        "gain_total": sum(r["holding"].pnl for r in rows if r["gain"]),
        "loss_total": sum(r["holding"].pnl for r in rows if not r["gain"]),
    }


def cross_view(holdings: Sequence[Holding], info: Mapping, assets: Mapping) -> dict:
    """口座横断: 同じ銘柄の合算・資産の種類と業種の比率・上位の集中度。"""
    total = sum(h.value for h in holdings)
    groups: dict = {}
    for h in holdings:
        g = groups.setdefault(h.key, {"key": h.key, "code": h.code, "name": h.name, "asset": assets[h],
                                      "value": 0, "quantity": 0.0, "pnl": 0, "accounts": {}})
        g["value"] += h.value
        g["quantity"] += h.quantity
        g["pnl"] += h.pnl
        g["accounts"][h.account] = g["accounts"].get(h.account, 0) + h.value
    ranked = sorted(groups.values(), key=lambda g: (-g["value"], g["key"]))
    by_asset: dict = {}
    by_industry: dict = {}
    for g in ranked:
        by_asset[g["asset"]] = by_asset.get(g["asset"], 0) + g["value"]
        if g["asset"] == ASSET_STOCK:
            label = (info[g["code"]].industry if g["code"] in info else None) or "業種不明"
        else:
            label = ASSET_NAMES[g["asset"]]
        by_industry[label] = by_industry.get(label, 0) + g["value"]
    return {
        "total": total,
        "multi": [g for g in ranked if len(g["accounts"]) >= 2],
        "by_asset": [(a, v, _pct(v, total)) for a, v in sorted(by_asset.items(), key=lambda kv: -kv[1])],
        "by_industry": [(k, v, _pct(v, total)) for k, v in sorted(by_industry.items(), key=lambda kv: (-kv[1], kv[0]))],
        "top": ranked[:10],
        "top5_pct": _pct(sum(g["value"] for g in ranked[:5]), total),
        "top10_pct": _pct(sum(g["value"] for g in ranked[:10]), total),
        "groups": len(ranked),
    }


def build_report(parsed: ParsedHoldings, info: Mapping, yutai: Mapping, as_of: date, *,
                 db_used: bool, source: str, realized: Optional[Realized] = None,
                 realized_source: Optional[str] = None, buy_years: Optional[Mapping] = None) -> dict:
    holdings = list(parsed.holdings)
    assets = _assets(holdings, info)
    return {
        "as_of": as_of,
        "source": source,
        "realized_source": realized_source,
        "db_used": db_used,
        "yutai_count": len(yutai),
        "yutai_confirmed": sum(1 for y in yutai.values() if y.confirmed),
        "holdings": holdings,
        "summary": account_summary(holdings, assets),
        "taxable": taxable_view(holdings, info, yutai, realized),
        "nisa": nisa_view(holdings, info, yutai, assets),
        "old_nisa": old_nisa_view(holdings, yutai, as_of, buy_years),
        "cross": cross_view(holdings, info, assets),
    }


# ── 書き出し ─────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def _yen(v: Optional[float]) -> str:
    return "—" if v is None else f"{int(v):,}"


def _signed(v: Optional[float]) -> str:
    if v is None:
        return "—"
    cls = "pos" if v > 0 else "neg" if v < 0 else ""
    return f'<span class="{cls}">{int(v):+,}</span>'


def _pc(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _qty(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def _price(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], num_from: int = 1) -> str:
    """表。`num_from` 列目以降を右寄せの数値列にする（中身は呼び出し側でエスケープ済み）。"""
    if not rows:
        return '<p class="muted">該当なし</p>'
    head = "".join(f'<th class="{"num" if i >= num_from else ""}">{_e(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i >= num_from else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _label(h: Holding) -> str:
    return f"{_e(h.code)} {_e(h.name)}" if h.code else _e(h.name)


def _section_summary(s: dict) -> str:
    rows = [[_e(ACCOUNT_NAMES[r["account"]]), _e(ASSET_NAMES[r["asset"]]), str(r["count"]), _yen(r["cost"]),
             _yen(r["value"]), _signed(r["pnl"]), _pc(r["pnl_pct"], True)] for r in s["rows"]]
    rows += [[f'<b>{_e(ACCOUNT_NAMES[r["account"]])} 計</b>', "", str(r["count"]), _yen(r["cost"]),
              _yen(r["value"]), _signed(r["pnl"]), _pc(r["pnl_pct"], True)] for r in s["subtotals"]]
    t = s["total"]
    rows.append(["<b>合計</b>", "", str(t["count"]), _yen(t["cost"]), _yen(t["value"]), _signed(t["pnl"]),
                 _pc(t["pnl_pct"], True)])
    return ("<h2>1. 全体</h2>"
            + _table(["口座", "資産の種類", "件数", "取得額", "評価額", "含み損益", "損益率"], rows, num_from=2))


def _realized_block(r: Optional[dict]) -> str:
    if r is None:
        return ("<p>今年すでに確定した損益は入っていません（特定口座の譲渡損益の明細を <code>--realized</code> で"
                "渡すと、ここに損益通算の数字が出ます）。</p>")
    room = (f'<p>今年の確定損益は損なので、<b>含み益をあと {_yen(r["room"])} 円確定するまでは税がかかりません</b>'
            "（同じ年の損と相殺されるため）。</p>" if r["room"] else "")
    return (
        f'<h3>今年の確定損益との損益通算（受渡日 {_e(r["start"].isoformat())}〜{_e(r["end"].isoformat())}・'
        f'売り {r["sells"]} 件）</h3>'
        '<div class="cards">'
        f'<div class="card"><div class="k">今年の確定損益（R）</div><div class="v">{_signed(r["pnl"])}</div></div>'
        f'<div class="card"><div class="k">源泉徴収された譲渡益税（明細の値）</div><div class="v">{_yen(r["tax_withheld"])}</div></div>'
        f'<div class="card"><div class="k">特定口座の含み損の合計（L）</div><div class="v">{_signed(r["loss"])}</div></div>'
        f'<div class="card"><div class="k">含み損を全部確定した場合の損益（R + L）</div><div class="v">{_signed(r["netted"])}</div></div>'
        f'<div class="card"><div class="k">そのときの税の見込み</div><div class="v">{_yen(r["tax_after"])}</div></div>'
        f'<div class="card"><div class="k">戻る見込み（徴収された税 − 税の見込み）</div><div class="v">{_yen(r["refund"])}</div></div>'
        "</div>"
        + room
        + '<p class="muted">一般口座の含み損は特定口座の源泉徴収と自動では相殺されないので L に入れていない。'
        f'R + L が損になった分と、特定口座で受け取った配当（税引き前 {_yen(r["dividends"])} 円・徴収 '
        f'{_yen(r["div_tax"])} 円）の相殺は数えていない。税の見込みは 20.315% をかけた値で、実際の徴収額とは'
        "取引ごとの端数の分だけずれる。</p>"
    )


def _section_taxable(t: dict) -> str:
    if not t["holdings"]:
        return ("<h2>2. 特定口座・一般口座</h2><p class=\"muted\">保有なし</p>"
                + (_realized_block(t["realized"]) if t["realized"] else ""))
    loss = [[_label(h), _qty(h.quantity), _yen(h.cost), _yen(h.value), _signed(h.pnl)] for h in t["loss_rows"]]
    yt = [[_label(r["holding"]), _qty(r["qty_here"]), _qty(r["qty_all"]), _qty(r["yutai"].min_shares),
           _e(r["yutai"].record_months), _e(r["yutai"].content), "済" if r["yutai"].confirmed else "未"]
          for r in t["yutai_rows"]]
    return (
        "<h2>2. 特定口座・一般口座（課税）</h2>"
        "<p>売ると、利益に税（20.315%）がかかります。同じ年のうちなら、損の出た銘柄の損と相殺できます"
        "（損益通算）。下のカードは今の保有だけの数字です。</p>"
        '<div class="cards">'
        f'<div class="card"><div class="k">含み益の合計</div><div class="v">{_signed(t["gains"])}</div></div>'
        f'<div class="card"><div class="k">含み損の合計</div><div class="v">{_signed(t["losses"])}</div></div>'
        f'<div class="card"><div class="k">税の見込み（利益の銘柄だけ売った場合）</div><div class="v">{_yen(t["tax_gains_only"])}</div></div>'
        f'<div class="card"><div class="k">税の見込み（全部売って損益を相殺した場合）</div><div class="v">{_yen(t["tax_netted"])}</div></div>'
        f'<div class="card"><div class="k">配当の年額（概算）とその税</div><div class="v">{_yen(t["div_total"])} / {_yen(t["div_tax"])}</div></div>'
        "</div>"
        f'<p class="muted">配当は直近期の配当利回り（期末株価基準）× 評価額の概算。利回りが無い保有 {len(t["div_missing"])} 件は含まない。</p>'
        + _realized_block(t["realized"])
        + "<h3>含み損の銘柄</h3>"
        + _table(["銘柄", "株数", "取得額", "評価額", "含み損益"], loss)
        + "<h3>優待のある銘柄と株数</h3>"
        "<p class=\"muted\">同じ証券会社の口座は株主として合算されることが多いので、この口座の株数と全口座の合計を並べる"
        "（足りているかの判定はしない）。継続保有の条件は内容の欄を見る。</p>"
        + _table(["銘柄", "この口座", "全口座", "必要株数", "権利月", "内容", "確認"], yt)
    )


def _section_nisa(n: dict) -> str:
    if not n["holdings"]:
        return "<h2>3. 新NISA</h2><p class=\"muted\">保有なし</p>"
    flagged = [[_label(r["holding"]), _e(ACCOUNT_NAMES[r["holding"].account]), _qty(r["qty_all"]),
                _qty(r["yutai"].min_shares),
                "—" if r["reaches_min"] is None else ("届いている" if r["reaches_min"] else "届いていない"),
                _pc(r["yutai_yield"]), _pc(r["div_yield"]), _e(r["yutai"].content),
                "済" if r["yutai"].confirmed else "未"] for r in n["flagged"]]
    unconf = [[_label(h), _e(ACCOUNT_NAMES[h.account]), _yen(h.value)] for h in n["unconfirmed"]]
    loss = [[_label(h), _e(ACCOUNT_NAMES[h.account]), _yen(h.cost), _yen(h.value), _signed(h.pnl)]
            for h in n["loss_rows"]]
    return (
        "<h2>3. 新NISA（成長投資枠・つみたて投資枠）</h2>"
        "<h3>方針チェック: 優待のある銘柄（一律に印）</h3>"
        "<p>NISA で非課税になるのは配当（株式数比例配分方式で受け取るもの）と譲渡益です（国税庁 No.1535）。"
        "優待はこの範囲に書かれていないので、優待の比重が大きい銘柄ほど NISA に置く意味が薄くなります。"
        "印は「方針と合わない可能性」で、問題ないかどうかの判断はあなたがします。</p>"
        + _table(["銘柄", "枠", "全口座の株数", "必要株数", "必要株数に", "優待利回り（概算）", "配当利回り（概算）",
                  "内容", "確認"], flagged, num_from=2)
        + "<h3>優待が未確認の銘柄</h3>"
        "<p class=\"muted\">優待一覧に無い、または「不明」の銘柄。ETF と投信は確認から外している。</p>"
        + _table(["銘柄", "枠", "評価額"], unconf, num_from=2)
        + f'<p class="muted">優待「なし」と見ている銘柄: {len(n["without"])} 件（うち確認済み {n["without_confirmed"]} 件）。</p>'
        "<h3>含み損の銘柄</h3>"
        "<p>NISA 口座の損は「ないものとみなされる」ため、他の口座の利益と相殺できません（国税庁 No.1535）。</p>"
        + _table(["銘柄", "枠", "取得額", "評価額", "含み損益"], loss, num_from=2)
        + f'<p>含み損の合計: {_signed(n["loss_total"])}</p>'
        "<h3>非課税で浮く税の見込み</h3>"
        '<div class="cards">'
        f'<div class="card"><div class="k">含み益（利益の銘柄の合計）にかからない税</div><div class="v">{_yen(n["tax_free_gains"])}</div></div>'
        f'<div class="card"><div class="k">配当の年額（概算）にかからない税</div><div class="v">{_yen(n["tax_free_div"])}</div></div>'
        "</div>"
        f'<p class="muted">配当は個別株の直近期の配当利回りから出した概算。REIT・ETF の分配金と利回りが無い保有 '
        f'{len(n["div_missing"])} 件は含まない。</p>'
        "<h3>生涯枠の使用（簿価）</h3>"
        + _table(["枠", "使用（取得額の合計）", "上限", "残り"], [
            ["非課税保有限度額（両枠の合計）", _yen(n["used_total"]), _yen(NISA_LIFETIME_LIMIT), _yen(n["left_total"])],
            ["うち成長投資枠", _yen(n["used_growth"]), _yen(NISA_GROWTH_LIFETIME_LIMIT), _yen(n["left_growth"])],
        ])
        + "<p class=\"muted\">CSV の取得金額を簿価とみなした値。年間の枠（つみたて 120 万円・成長 240 万円）は買付日が要るので出していない。</p>"
    )


def _days_left(d: Optional[int]) -> str:
    if d is None:
        return "—"
    return f"{d:,} 日" if d >= 0 else "終了済み（買付年を確かめる）"


def _section_old_nisa(o: dict) -> str:
    if not o["rows"]:
        return "<h2>4. 旧NISA</h2><p class=\"muted\">保有なし</p>"
    rows = [[_label(r["holding"]), _qty(r["holding"].quantity), _e(r["buy_year"] or "不明"),
             _e(r["expiry"].isoformat() if r["expiry"] else "—"), _days_left(r["days_left"]),
             _price(r["holding"].avg_cost), _price(r["holding"].price), _signed(r["holding"].pnl),
             ("かからずに済む税 " if r["gain"] else "戻ったときにかかる税 ") + _yen(r["tax_effect"]),
             _e(r["yutai"] or "—")] for r in o["rows"]]
    if o["unknown"]:
        ends = "・".join(d.isoformat() for d in o["expiries"]) or "（候補なし）"
        unknown = (f'<p>買付年が分からない {o["unknown"]} 銘柄の、終わる日の候補: <b>{_e(ends)}</b>。買付年は'
                   " <code>.portfolio/old_nisa_years.csv</code> に <code>code,buy_year</code> の形で書くと出ます"
                   "（保有一覧にも取引履歴にも旧NISA の買付年が入らないため）。</p>")
    else:
        unknown = ""
    not_held = (f'<p class="muted">買付年の表にあって旧NISA の保有に無い証券コード: {_e("・".join(o["not_held"]))}'
                "（売った・期間が終わって移った銘柄なら、表から消してよい）。</p>" if o["not_held"] else "")
    return (
        "<h2>4. 旧NISA（2023 年までの一般NISA）</h2>"
        "<p>非課税期間は買った年を含めて 5 年で、2022 年に買った分は 2026-12-31、2023 年に買った分は 2027-12-31 に"
        "終わります。新しい NISA へは移せず、終わると課税口座へ移ります（金融庁）。表は終わる日の近い順です。</p>"
        + unknown
        + "<p>移ったあとの取得単価は、利用中の証券会社の案内（2026-11-22 に確認）では、期間の最後の年の最終営業日の"
        "時価になり、翌年の初めに課税口座へ払い出されます（国の公式ページでは確かめていません）。下の表は"
        "<b>今の株価で期間が終わった場合</b>の近似です。含み益の銘柄は益が非課税のまま確定し、含み損の銘柄は元の"
        "取得単価まで戻った分にも税がかかります。</p>"
        + _table(["銘柄", "株数", "買付年", "終わる日", "残り", "取得単価", "現在値", "含み損益",
                  "期間が終わった場合の税の影響", "優待"], rows)
        + f'<p>含み益の合計 {_signed(o["gain_total"])} ／ 含み損の合計 {_signed(o["loss_total"])}</p>'
        + not_held
        + '<p class="muted">同じ銘柄を 2 つの年に買って一部を売った場合は、古い年の分から減る扱いが一般的です'
        "（証券会社の FAQ）。買付年の表は 1 銘柄 1 行なので、2 つの年に分かれる銘柄は書かず、候補の表示のままにします。</p>"
    )


def _section_cross(c: dict) -> str:
    multi = [[(_e(g["code"]) + " " if g["code"] else "") + _e(g["name"]),
              _e("・".join(f"{ACCOUNT_NAMES[a]} {v:,}" for a, v in g["accounts"].items())),
              _qty(g["quantity"]), _yen(g["value"]), _signed(g["pnl"])] for g in c["multi"]]
    assets = [[_e(ASSET_NAMES[a]), _yen(v), _pc(p)] for a, v, p in c["by_asset"]]
    inds = [[_e(k), _yen(v), _pc(p)] for k, v, p in c["by_industry"]]
    top = [[(_e(g["code"]) + " " if g["code"] else "") + _e(g["name"]), _e(ASSET_NAMES[g["asset"]]),
            _yen(g["value"]), _pc(_pct(g["value"], c["total"]))] for g in c["top"]]
    return (
        "<h2>5. 口座横断</h2>"
        "<h3>複数の口座で持っている銘柄</h3>"
        + _table(["銘柄", "口座ごとの評価額", "数量の合計", "評価額の合計", "含み損益の合計"], multi, num_from=2)
        + "<h3>資産の種類</h3>" + _table(["種類", "評価額", "比率"], assets)
        + "<h3>業種（個別株は東証 33 業種、それ以外は資産の種類）</h3>" + _table(["業種", "評価額", "比率"], inds)
        + f'<h3>上位 10 銘柄（全 {c["groups"]} 銘柄・同じ銘柄は口座をまたいで合算）</h3>'
        f'<p>上位 5 銘柄で {_pc(c["top5_pct"])}、上位 10 銘柄で {_pc(c["top10_pct"])}。</p>'
        + _table(["銘柄", "種類", "評価額", "比率"], top, num_from=2)
    )


_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#ffffff;--pos:#1d6b3a;--neg:#a3271f;--head:#f1f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#a3a29c;--line:#34332f;--card:#1f1f1d;--pos:#6fcf8f;--neg:#ff8a80;--head:#262624}}
body{background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Yu Gothic UI",sans-serif;margin:0;padding:24px 16px;}
main{max-width:1080px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px} h2{font-size:1.2rem;margin:32px 0 8px;padding-top:8px;border-top:1px solid var(--line)} h3{font-size:1rem;margin:20px 0 6px}
.muted{color:var(--muted);font-size:.9em} .pos{color:var(--pos)} .neg{color:var(--neg)}
.note{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0}
.scroll{overflow-x:auto} table{border-collapse:collapse;width:100%;margin:6px 0;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top} th{background:var(--head);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;white-space:nowrap}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0} .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:200px;flex:1}
.card .k{color:var(--muted);font-size:.85em} .card .v{font-size:1.15rem;font-variant-numeric:tabular-nums}
"""


def render_html(report: dict) -> str:
    srcs = "".join(f'<li>{_e(t)}: <a href="{_e(u)}">{_e(u)}</a></li>' for t, u in SOURCES)
    yutai = (f'{report["yutai_count"]} 銘柄（うち確認済み {report["yutai_confirmed"]}）'
             if report["yutai_count"] else "読み込んでいない")
    realized = report["realized_source"] or "読み込んでいない"
    years = report["old_nisa"]["years_given"]
    years = f"{years} 銘柄" if years else "読み込んでいない"
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>保有の口座別レポート {_e(report["as_of"].isoformat())}</title><style>{_CSS}</style></head><body><main>'
        "<h1>保有の口座別レポート</h1>"
        f'<p class="muted">基準日 {_e(report["as_of"].isoformat())} ／ 入力 {_e(report["source"])} ／ '
        f'DB の補足（資産の種類・業種・配当利回り）: {"あり" if report["db_used"] else "なし（未取得）"} ／ 優待一覧: {_e(yutai)} ／ '
        f'譲渡損益の明細: {_e(realized)} ／ 旧NISA の買付年の表: {_e(years)}</p>'
        '<div class="note"><b>これは売買の助言ではありません。</b>数字と、方針（NISA に優待株を入れない）に合っているかの'
        "印だけを並べています。税は 20.315% をかけた見込み（円未満切り捨て）で、配当利回りは直近期の期末株価を基準にした"
        "概算です。優待の情報は Web の下書きを含むので、「確認」が「未」の行は確かめてから使ってください。</div>"
        + _section_summary(report["summary"])
        + _section_taxable(report["taxable"])
        + _section_nisa(report["nisa"])
        + _section_old_nisa(report["old_nisa"])
        + _section_cross(report["cross"])
        + f"<h2>出典・前提</h2><ul>{srcs}</ul>"
        "</main></body></html>"
    )


def summary_lines(report: dict) -> list:
    """標準出力の要約（ASCII だけ・銘柄名は出さない）。"""
    s = report["summary"]
    lines = [f'[holdings_report] as_of={report["as_of"].isoformat()} holdings={len(report["holdings"])} '
             f'db={"yes" if report["db_used"] else "no"} yutai={report["yutai_count"]}'
             f'(confirmed {report["yutai_confirmed"]})']
    for r in s["subtotals"]:
        lines.append(f'  {r["account"]:<15} n={r["count"]:<3} value={r["value"]:>14,} pnl={r["pnl"]:>+13,}')
    t = s["total"]
    lines.append(f'  {"TOTAL":<15} n={t["count"]:<3} value={t["value"]:>14,} pnl={t["pnl"]:>+13,}')
    n = report["nisa"]
    lines.append(f'  nisa: yutai_flagged={len(n["flagged"])} unconfirmed={len(n["unconfirmed"])} '
                 f'losses={len(n["loss_rows"])}')
    o = report["old_nisa"]
    nearest = next((r["expiry"].isoformat() for r in o["rows"] if r["expiry"]), "-")
    lines.append(f'  old_nisa: holdings={len(o["rows"])} known={len(o["rows"]) - o["unknown"]} '
                 f'unknown={o["unknown"]} nearest_expiry={nearest} not_held={len(o["not_held"])} '
                 f'expiry_candidates={",".join(d.isoformat() for d in o["expiries"]) or "-"}')
    r = report["taxable"]["realized"]
    if r is None:
        lines.append("  realized: none")
    else:
        lines.append(f'  realized: sells={r["sells"]} pnl={r["pnl"]:+,} tax_withheld={r["tax_withheld"]:,} '
                     f'netted={r["netted"]:+,} refund={r["refund"]:,}')
    return lines


# ── DB ブロック（読むだけ） ───────────────────────────────────────────────────

def load_security_info(db, codes: Sequence[str], known_at: datetime) -> dict:
    """資産の種類（JPX の市場区分）・業種・直近期の配当利回りを読む。書かない。"""
    from sqlalchemy import func

    from database import Company, FinancialMetric, jpx_listing_snapshot_at

    codes = sorted(set(codes))
    if not codes:
        return {}
    _, snap = jpx_listing_snapshot_at(db, known_at)
    segments = {c: snap[c].market_segment for c in codes if c in snap}
    industries: dict = {}
    for code, ind, active in (db.query(Company.sec_code, Company.industry, Company.is_active)
                              .filter(Company.sec_code.in_(codes)).all()):
        if ind and (active or code not in industries):
            industries[code] = ind
    for c in codes:
        if c not in industries and c in snap:
            industries[c] = snap[c].industry33
    # 最新年度は保有の社だけで取る（`latest_year_subq` と同じ形を保有の証券コードで絞ったもの）
    FM = FinancialMetric
    subq = (db.query(FM.edinet_code, func.max(FM.year).label("max_year"))
            .filter(FM.sec_code.in_(codes)).group_by(FM.edinet_code).subquery())
    rows = (db.query(FM.sec_code, FM.div_yield, FM.period_end)
            .join(subq, (FM.edinet_code == subq.c.edinet_code) & (FM.year == subq.c.max_year))
            .filter(FM.div_yield.isnot(None)).all())
    yields: dict = {}
    for code, y, _ in sorted(rows, key=lambda r: (r.period_end or date.min)):
        yields[code] = y            # 同じ年度に 2 行ある社（決算期変更）は期末の新しい方
    return merge_security_info(codes, segments, industries, yields)


def _load_info_from_db(codes: Sequence[str]) -> dict:
    from database import SessionLocal

    db = SessionLocal()
    try:
        return load_security_info(db, codes, datetime.now(timezone.utc))
    finally:
        db.close()


# ── CLI ─────────────────────────────────────────────────────────────────────

def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="保有証券一覧 CSV を口座の種類ごとに分析し、HTML のレポートを書く")
    ap.add_argument("--csv", required=True, help="保有証券一覧の書き出し（cp932）")
    ap.add_argument("--realized", help="特定口座の譲渡損益の明細（cp932・基準日の年の 1 月 1 日からの全件）")
    ap.add_argument("--old-nisa-years",
                    help="旧NISA の買付年の表（UTF-8・列 code,buy_year）。既定: .portfolio/old_nisa_years.csv があれば読む")
    ap.add_argument("--yutai", help="優待一覧（UTF-8）。既定: .portfolio/yutai.csv があれば読む")
    ap.add_argument("--as-of", type=date.fromisoformat, help="基準日 YYYY-MM-DD（既定: 今日）")
    ap.add_argument("--no-db", action="store_true", help="DB を読まない（資産の種類・業種・配当利回りは未取得）")
    ap.add_argument("--out", help="レポートの書き先（既定: .portfolio/reports/holdings_<基準日>.html）")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    csv_path = Path(args.csv)
    realized_path = Path(args.realized) if args.realized else None
    cwd = Path.cwd()
    folders = [DEFAULT_DIR, csv_path.resolve().parent] + ([realized_path.resolve().parent] if realized_path else [])
    for folder in folders:
        if _inside(cwd, folder):
            print(f"[holdings_report] NG: 資料のフォルダの中から実行しない（cwd={cwd}）", file=sys.stderr)
            return EXIT_NG
    as_of = args.as_of or date.today()
    yutai_path = Path(args.yutai) if args.yutai else DEFAULT_DIR / "yutai.csv"
    years_path = Path(args.old_nisa_years) if args.old_nisa_years else DEFAULT_DIR / "old_nisa_years.csv"
    try:
        parsed = parse_holdings_csv(csv_path.read_bytes())
        yutai = (parse_yutai_csv(yutai_path.read_text(encoding="utf-8-sig"))
                 if args.yutai or yutai_path.is_file() else {})
        realized = parse_realized_csv(realized_path.read_bytes(), as_of.year) if realized_path else None
        buy_years = (parse_old_nisa_years_csv(years_path.read_text(encoding="utf-8-sig"))
                     if args.old_nisa_years or years_path.is_file() else {})
    except (HoldingsCsvError, OSError) as e:
        print(f"[holdings_report] NG: {e}", file=sys.stderr)
        return EXIT_NG
    info: dict = {}
    if not args.no_db:
        codes = [h.code for h in parsed.holdings if h.code]
        try:
            info = _load_info_from_db(codes)
        except Exception as e:  # noqa: BLE001 — 接続できないことを黙って「未取得」にしない
            # 文面は 1 行目だけ（SQLAlchemy の例外は SQL と引数を後ろに付ける）
            first = (str(e).splitlines() or [""])[0][:200]
            print(f"[holdings_report] NG: DB から補足を読めない（--no-db で飛ばせる）: {type(e).__name__}: {first}",
                  file=sys.stderr)
            return EXIT_DB
    report = build_report(parsed, info, yutai, as_of, db_used=not args.no_db, source=csv_path.name,
                          realized=realized, realized_source=realized_path.name if realized_path else None,
                          buy_years=buy_years)
    out = Path(args.out) if args.out else DEFAULT_DIR / "reports" / f"holdings_{as_of:%Y%m%d}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(report), encoding="utf-8")
    for line in summary_lines(report):
        print(line)
    print(f"  report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
