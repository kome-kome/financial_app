"""指数イベント台帳 — TOPIX の定期入替・移行措置・新規採用を、公式リストから公表日 as-of で時点再現する（#1003・ADR-0068）。

「どの社が、いつ、どの指数イベント（段階的ウエイト低減＝移行措置・新規採用・継続・前例の解除）の
対象で、その日の移行係数はいくつで、その事実はいつから分かっていたか」を1か所で答える。読み手は
指数効果の計測（#1006・#1008〜#1011・事前登録 `docs/prereg/index-event-2026.md`）、画面の事実タグ
（#1013）、年次運用の watchdog（#1014・`next_review_expected`）。

## 企業イベント台帳とは分ける

`corporate_actions.py` の台帳は分割補正係数 F（`financial_metrics` VIEW の per / pbr / market_cap）と
TTM へ流れる。指数イベントは1株の基準を変えないので、混ぜると F・TTM・`tradable_filters` へ
波及するだけになる。**`corporate_actions` / `ttm_composite` / `database` はこの module を import しない**
（テストで固定）。

## 一覧はリポジトリに入れない

JPX の一覧は無断転載不可で、リポジトリは PUBLIC。一覧は `.index_lists/`（`.gitignore`）に置き、
`scripts/import_index_list.py` が `sec_code,kind` の正規化 CSV にする。コミットするのは登録表
（公表日・引け後か・反映日・件数・元資料と正規化 CSV の sha256・出典・根拠）だけ。

## 使い方

- 本番: `load_ledger(db)` ——正規化 CSV と企業表を自分で読む。一覧のファイルが無ければ
  `ok=False` と理由を返す（黙って空にしない）。sha256 の不一致・件数の不一致・突合率の不足は例外。
- テスト: `build_ledger(lists, sec_to_ec)` ——DB にもファイルにも触らない。
- 問い合わせ: `status_at(ledger, ec, on, known_at)` / `group_labels` / `panel_index_status` / `members`。

## 先読みしない・無い≠無し

- 公表日より前の `known_at` には、その一覧の事実が見えない。**引け後の公表は翌日から見える**
  （公表日の終値で計算する処理に先読みを入れない）。
- どの一覧にも無い社は `unknown`。「継続」と決めつけない。
- 結果がまだ台帳に無い見直し（再評価・定期入替）より後の日付は、係数を `None`・状態を `pending` にする。

## 構成

登録表 → 台帳（純関数）→ I/O の順に並べる。**I/O より上は database / httpx / scripts をトップレベルで
import しない**（テストで固定）。祝日表（`scripts.run_daytime.HOLIDAYS`）も使わない——指数修正日は
資料と事前登録文書の表から手で入れる（ADR-0062 決定4。東証の休業日とも違う）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence

log = logging.getLogger("collector")

DEFAULT_LISTS_DIR = Path(__file__).resolve().parent / ".index_lists"

# 証券コード → edinet_code の突合率の下限（事前登録 §2 R0）。一覧ごとの重複なしのコード集合で測る——
# 節ごと（新規追加 35 社）で測ると、上場直後の 1 社が企業表に無いだけで 97.1% になる。
MIN_RESOLUTION = 0.98


class IndexLedgerError(ValueError):
    """一覧が壊れている・登録表と合わない・突合率が足りない。"""


# ── 登録表 ──────────────────────────────────────────────────────────────────

class Review(NamedTuple):
    """公式一覧 1 本（1 回の公表）。"""
    announced: date            # 公表日
    after_close: bool          # 引け後の公表か（True なら翌日から見える）
    effective: date            # 反映日（その回の最初の指数修正日）
    source: str                # 元資料の `.index_lists/` からの相対パス
    source_sha256: str
    source_url: Optional[str]
    sections: dict             # {見出し番号: 種別}。見出しの無い一覧は {"0": 種別}
    counts: dict               # {種別: 件数}（事前登録 §2 の照合値）
    market_counts: dict        # {種別: {市場区分: 件数}}（照合値があるものだけ）
    normalized_sha256: str     # 正規化 CSV（`normalized_bytes`）の sha256
    confirmed: bool
    basis: str


# 元資料は #1002 で 2026-10-20 に保存したもの（ファイル名の末尾は DL 日）。sha256 は #1002 のコメントの値。
REVIEWS: dict = {
    "topix-2022-10": Review(
        announced=date(2022, 10, 7), after_close=True, effective=date(2022, 10, 31),
        source="raw/20221007/topix_j_20261020.pdf",
        source_sha256="b3005c8d85a3e68b1da6f15be53c2b2e31194672e2b7aa94fb1626a8d0c6d9c2",
        source_url="https://www.jpx.co.jp/news/6030/nlsgeu000006mh6p-att/topix_j.pdf",
        sections={"0": "transition"},
        counts={"transition": 493},
        market_counts={},
        normalized_sha256="047a19a114960e6ac73dd25c9ce2e483269d476608b4767128b33af7e2a40e13",
        confirmed=True,
        basis="TOPIXにおける段階的ウエイト低減銘柄一覧（2022-10-07 公表・ニュース 20221007-02）"),
    "topix-2023-10-review": Review(
        announced=date(2023, 10, 6), after_close=True, effective=date(2023, 10, 31),
        source="raw/20231006/TOPIX_re-evaluation_J_20261020.pdf",
        source_sha256="cce5a96558e0dfb984e75af5dbb3de46d66d3c7f0f0074be2bf45eaf75019bfb",
        source_url="https://www.jpx.co.jp/news/6030/jr4eth0000003hbq-att/TOPIX_re-evaluation_J.pdf",
        # (1) 段階的ウエイト低減銘柄から除外（＝解除。0.1 幅で 1 まで戻す）/ (2) 低減を継続（2025-01 に除外）
        sections={"1": "released", "2": "transition"},
        counts={"released": 43, "transition": 439},
        market_counts={},
        normalized_sha256="289ab86633e88d56bfd64b432b958f2118a48b75ca15a947513569522cce7d18",
        confirmed=True,
        basis="TOPIXにおける段階的ウエイト低減銘柄一覧（再評価後の取扱い）（2023-10-06 公表・ニュース 20231006-02）"),
    "topix-2026-10": Review(
        announced=date(2026, 10, 7), after_close=True, effective=date(2026, 10, 30),
        source="raw/topix_j_20261020.pdf",
        source_sha256="7c8f87a35cbc25625dd3f63735eb9bb47d3df6153ec9b7445b67be03b092d13d",
        source_url=None,
        # (1) 新規追加 / (2) 移行措置 / (3) 2026-10-30 時点の構成銘柄（(1)(2) を含む全銘柄）
        sections={"1": "new_add", "2": "transition", "3": "constituent"},
        counts={"new_add": 35, "transition": 683, "constituent": 1669},
        market_counts={"new_add": {"プライム": 0, "スタンダード": 23, "グロース": 12}},
        normalized_sha256="2274bb402cc69a95e07fbc3fc16dfc744c0a86adeaa50282df11c9da8726db7b",
        confirmed=True,
        basis="TOPIX初回定期入替における選定結果及び構成銘柄一覧（2026-10-07 公表）。"
              "#1002 でユーザーが DL・取得元 URL は未記録"),
}


class Step(NamedTuple):
    """m 回目の係数変更。`coefficient` は指数修正日 `effective_on` から効く移行係数。"""
    m: int
    effective_on: date         # 指数修正日 D_m
    coefficient: Fraction
    date_basis: str            # "primary"（資料に日付まである）/ "rule"（月は資料・日は最終営業日の規則から計算）


class Schedule(NamedTuple):
    origin: str                # 一連のイベントを始めた公表（`REVIEWS` のキー）
    steps: tuple


# 係数は丸めた小数を書き写さず `1 - m * step` の式で持つ。日付と根拠の区別は事前登録文書 §3 の表と
# 1 行ずつ一致する（`tests/test_index_events.py` が照合する）。
STEP_2022 = Fraction(1, 10)    # 2022 前例: 10 回 × 10%
STEP_2026 = Fraction(1, 8)     # 2026 移行措置: 8 回（算出要領 TOPIX 編 2026-10-01 版 3.(1)c）

STEPS_2022: tuple = tuple(Step(m, d, 1 - m * STEP_2022, b) for m, d, b in (
    (1, date(2022, 10, 31), "rule"),
    (2, date(2023, 1, 31), "rule"),
    (3, date(2023, 4, 28), "rule"),
    (4, date(2023, 7, 31), "rule"),
    (5, date(2023, 10, 31), "primary"),   # 2023-10-06 公表の取扱い
    (6, date(2024, 1, 31), "rule"),
    (7, date(2024, 4, 30), "rule"),
    (8, date(2024, 7, 31), "rule"),
    (9, date(2024, 10, 31), "rule"),
    (10, date(2025, 1, 31), "primary"),   # 2025-01 最終営業日に TOPIX から除外（2023-10-06 公表）
))

# 2023-10 の再評価で解除された社は「2023 年 10 月最終営業日以降、四半期ごとに移行係数を 1 となるまで
# 0.1 幅で逓増」（2023-10-06 公表の一覧）。4 回目（2023-07）の値から戻り始める。
RELEASE_FROM_M = 4
STEPS_2023_RELEASE: tuple = tuple(
    Step(RELEASE_FROM_M + k, d, 1 - RELEASE_FROM_M * STEP_2022 + k * STEP_2022, b)
    for k, d, b in (
        (1, date(2023, 10, 31), "primary"),
        (2, date(2024, 1, 31), "rule"),
        (3, date(2024, 4, 30), "rule"),
        (4, date(2024, 7, 31), "rule"),
    ))

# 2026 は 8 回とも算出要領 3.(1)c の「その月の最終営業日」で、曜日と祝日から計算した一次資料の確定。
STEPS_2026: tuple = tuple(Step(m, d, 1 - m * STEP_2026, "primary") for m, d in (
    (1, date(2026, 10, 30)),
    (2, date(2027, 1, 29)),
    (3, date(2027, 4, 30)),
    (4, date(2027, 7, 30)),
    (5, date(2027, 10, 29)),
    (6, date(2028, 1, 31)),
    (7, date(2028, 4, 28)),
    (8, date(2028, 7, 31)),
))

# 新規採用は浮動株比率 × 調整係数 0.75 で組み入れ、後の定期入替で継続基準を満たせば解除（1.0 へ）。
NEW_ADD_COEFFICIENT = Fraction(3, 4)

# {(公表, 種別): 係数の道筋}。種別 `continuing` は 2026 の構成銘柄のうち新規追加でも移行措置でもない社。
SCHEDULES: dict = {
    ("topix-2022-10", "transition"): Schedule("topix-2022-10", STEPS_2022),
    ("topix-2023-10-review", "transition"): Schedule("topix-2022-10", STEPS_2022),
    ("topix-2023-10-review", "released"):
        Schedule("topix-2022-10", STEPS_2022[:RELEASE_FROM_M] + STEPS_2023_RELEASE),
    ("topix-2026-10", "transition"): Schedule("topix-2026-10", STEPS_2026),
    ("topix-2026-10", "new_add"):
        Schedule("topix-2026-10", (Step(1, STEPS_2026[0].effective_on, NEW_ADD_COEFFICIENT, "primary"),)),
    ("topix-2026-10", "continuing"): Schedule("topix-2026-10", ()),
}


class ExpectedReview(NamedTuple):
    """結果がまだ台帳に無い見直し。登録されたら `REVIEWS` へ移し、ここから消す。"""
    announce_expected: date
    effective: date
    basis: str


EXPECTED_REVIEWS: dict = {
    "topix-2027-10-review": ExpectedReview(
        announce_expected=date(2027, 10, 7), effective=date(2027, 10, 29),
        basis="移行措置銘柄の再評価（基準日 2027-08 最終営業日）。継続基準を満たせば 4 回目の 0.500 を"
              "継続適用し 5〜8 回目の低減を受けない（算出要領 3.(1)c）。公表は 10 月第 5 営業日の見込み"
              "（事前登録 §3.2）・反映は 5 回目の指数修正日"),
    "topix-2028-10": ExpectedReview(
        announce_expected=date(2028, 10, 6), effective=date(2028, 10, 31),
        basis="第 2 回定期入替（以後毎年・別紙 p.5・p.7）。新規採用の調整係数の解除はここが最初。"
              "公表は 10 月第 5 営業日・反映は 10 月最終営業日の規則から計算した推定"),
}

# {(公表, 種別): その結果を決める後続の見直し}。見直しの反映日より後は、見直しの一覧がその社を
# 載せていない限り係数が分からない（`pending`）。
PENDING_REVIEWS: dict = {
    ("topix-2022-10", "transition"): "topix-2023-10-review",
    ("topix-2023-10-review", "released"): "topix-2026-10",
    ("topix-2026-10", "transition"): "topix-2027-10-review",
    ("topix-2026-10", "new_add"): "topix-2028-10",
    ("topix-2026-10", "continuing"): "topix-2028-10",
}


class Delisted(NamedTuple):
    sec_code: str
    name: str


# 期間中の上場廃止等（事前登録 §2: 社ごとに明示登録し、暗黙の残差にしない）。
# {窓: (前の公表, 後の公表)}。d2 の後ろの端は一覧ではなく 2025-01-31 の除外。
DELISTED_WINDOWS: dict = {
    "d1": ("topix-2022-10", "topix-2023-10-review"),
    "d2": ("topix-2023-10-review", None),
}

# `None` は未登録（空のタプル＝登録済みで 0 社とは別）。
# d1: 2022 の一覧にあり 2023 の一覧に無く、コードの承継（`CODE_SUCCESSIONS`）でもない 11 社。
#     2026-10-21 の照会で企業表は 10 社が is_active=False、5128 は行が無い。
# d2: 継続 439 社のうち 2023-10〜2025-01 に上場廃止等になった社。企業表の `delisted_date` は同期が
#     廃止に気づいた日（2026-08-08 の一括同期）で真の廃止日ではないので、手元では決められない（#1022）。
DELISTED_DURING: dict = {
    "d1": (
        Delisted("4550", "日水製薬"),
        Delisted("4653", "ダイオーズ"),
        Delisted("5128", "ＷＯＷ ＷＯＲＬＤ ＧＲＯＵＰ"),
        Delisted("5807", "東京特殊電線"),
        Delisted("6175", "ネットマーケティング"),
        Delisted("6205", "ニデックオーケーケー"),
        Delisted("6355", "住友精密工業"),
        Delisted("6461", "日本ピストンリング"),
        Delisted("6826", "本多通信工業"),
        Delisted("7961", "兼松サステック"),
        Delisted("8521", "長野銀行"),
    ),
    "d2": None,
}


class Succession(NamedTuple):
    old: str
    new: str
    confirmed: bool
    basis: str


# 窓の中でコードが変わった社。旧コードは一覧から消え、新コードが後の一覧に現れる。
CODE_SUCCESSIONS: dict = {
    "d1": (
        Succession("3464", "5535", False,
                   "プロパティエージェント → ミガロホールディングス（持株会社化と見る）。2022 と 2023 の一覧の"
                   "片側にだけ現れる組で、社名の対応と企業表の上場状態（旧 is_active=False・新 True）から推定。"
                   "適時開示は未照合"),
        Succession("6032", "7374", False,
                   "インターワークス → コンフィデンス・インターワークス（合併と見る）。推定の根拠は上と同じ。"
                   "適時開示は未照合"),
    ),
}


def visible_from(review: Review) -> date:
    """この一覧の事実が見えるようになる日（`known_at` がこの日以上なら見える）。"""
    return review.announced + timedelta(days=1) if review.after_close else review.announced


def pending_from(key: str, *, reviews: Optional[Mapping] = None) -> date:
    """見直し `key` の反映日。登録済みなら `REVIEWS`、未登録なら `EXPECTED_REVIEWS` から取る。"""
    reviews = REVIEWS if reviews is None else reviews
    if key in reviews:
        return reviews[key].effective
    return EXPECTED_REVIEWS[key].effective


def next_review_expected(*, reviews: Optional[Mapping] = None) -> Optional[tuple]:
    """次に来る未登録の見直し `(キー, ExpectedReview)`。公表見込み日を過ぎても未登録なら返し続ける。"""
    reviews = REVIEWS if reviews is None else reviews
    pending = sorted(((k, e) for k, e in EXPECTED_REVIEWS.items() if k not in reviews),
                     key=lambda kv: kv[1].announce_expected)
    return pending[0] if pending else None


# ── 台帳（純関数）────────────────────────────────────────────────────────────

class IndexStatus(NamedTuple):
    """`status_at` の答え。`announced` / `effective` は一連のイベントを始めた公表の日付。"""
    review: Optional[str]      # その社を載せた最新の（見えている）公表
    kind: str                  # transition / new_add / continuing / released / unknown
    status: str                # not_yet_announced / announced / in_effect / pending / excluded / delisted / unknown
    group: str                 # transition / new_add / other / unknown（#1006）
    coefficient: Optional[float]
    announced: Optional[date]
    effective: Optional[date]
    schedule_confirmed: bool   # 日程がすべて一次資料の日付か
    source: Optional[str]


UNKNOWN = IndexStatus(None, "unknown", "unknown", "unknown", None, None, None, False, None)


@dataclass(frozen=True)
class IndexLedger:
    ok: bool
    reason: str = ""
    # {ec: ((公表, 種別), ...)}。公表日の古い順
    mentions: Mapping = field(default_factory=dict)
    # {(公表, 種別): frozenset(ec)}。ITT（最初の指定どおり）の群
    groups: Mapping = field(default_factory=dict)
    # {ec: (窓, "delisted" | "succeeded")}
    delisted: Mapping = field(default_factory=dict)
    stats: Mapping = field(default_factory=dict)
    reviews: Mapping = field(default_factory=lambda: REVIEWS)


def normalized_bytes(rows: Iterable[tuple]) -> bytes:
    """正規化 CSV（見出し `sec_code,kind`・(種別, コード) 順・LF・BOM なし UTF-8）のバイト列。"""
    lines = ["sec_code,kind"] + [f"{s},{k}" for s, k in sorted(set(rows), key=lambda r: (r[1], r[0]))]
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_normalized(data: bytes) -> list:
    """`normalized_bytes` の逆。形が崩れていれば `IndexLedgerError`。"""
    reader = csv.reader(io.StringIO(data.decode("utf-8")))
    header = next(reader, None)
    if header != ["sec_code", "kind"]:
        raise IndexLedgerError(f"正規化 CSV の見出しが違う: {header}")
    rows = []
    for rec in reader:
        if len(rec) != 2 or not rec[0] or not rec[1]:
            raise IndexLedgerError(f"正規化 CSV の行が崩れている: {rec}")
        rows.append((rec[0], rec[1]))
    return rows


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _kinds_by_code(key: str, rows: Sequence[tuple], review: Review) -> dict:
    """一覧 1 本を照合し、{sec_code: 種別} にする。`constituent` だけの社は `continuing`。"""
    got = Counter(kind for _, kind in rows)
    if dict(got) != dict(review.counts):
        raise IndexLedgerError(f"{key}: 件数が登録表と違う（一覧 {dict(got)} / 登録表 {review.counts}）")
    if len(set(rows)) != len(rows):
        raise IndexLedgerError(f"{key}: 同じ (コード, 種別) が重複している")
    by_kind = defaultdict(set)
    for sec, kind in rows:
        by_kind[kind].add(sec)
    special = [k for k in by_kind if k != "constituent"]
    seen: dict = {}
    for kind in special:
        for sec in by_kind[kind]:
            if sec in seen:
                raise IndexLedgerError(f"{key}: {sec} が {seen[sec]} と {kind} の両方にある")
            seen[sec] = kind
    if "constituent" in by_kind:
        outside = sorted(set(seen) - by_kind["constituent"])
        if outside:
            raise IndexLedgerError(f"{key}: 構成銘柄に無い新規追加・移行措置がある: {outside[:10]}")
        for sec in by_kind["constituent"] - set(seen):
            seen[sec] = "continuing"
    return seen


def _reconcile(window: str, before: set, after: set, delisted: Sequence, successions: Sequence) -> None:
    """前後の一覧の差が、登録した上場廃止等とコードの承継で過不足なく説明できるか。"""
    gone, came = before - after, after - before
    want_gone = {d.sec_code for d in delisted} | {s.old for s in successions}
    want_came = {s.new for s in successions}
    if gone != want_gone or came != want_came:
        raise IndexLedgerError(
            f"{window}: 一覧の差が登録と合わない（消えた {len(gone)} / 登録 {len(want_gone)}・"
            f"未登録で消えた {sorted(gone - want_gone)}・登録したのに残った {sorted(want_gone - gone)}・"
            f"現れた {sorted(came)} / 承継先 {sorted(want_came)}）")


def build_ledger(lists: Mapping, sec_to_ec: Mapping, *,
                 reviews: Optional[Mapping] = None,
                 delisted_during: Optional[Mapping] = None,
                 successions: Optional[Mapping] = None,
                 min_resolution: float = MIN_RESOLUTION) -> IndexLedger:
    """正規化済みの一覧 `{公表: [(sec_code, kind), ...]}` と `{sec_code: edinet_code}` から台帳を作る。

    DB にもファイルにも触らない。登録表と合わなければ `IndexLedgerError`（`ok=False` にはしない——
    それは「一覧が無い」ときだけ）。
    """
    reviews = REVIEWS if reviews is None else reviews
    delisted_during = DELISTED_DURING if delisted_during is None else delisted_during
    successions = CODE_SUCCESSIONS if successions is None else successions

    missing = sorted(set(reviews) - set(lists))
    if missing:
        raise IndexLedgerError(f"一覧が渡されていない: {missing}")
    kinds = {key: _kinds_by_code(key, lists[key], reviews[key]) for key in reviews}

    for window, (before, after) in DELISTED_WINDOWS.items():
        if after is None or before not in kinds or after not in kinds:
            continue
        registered = delisted_during.get(window)
        if registered is None:
            raise IndexLedgerError(f"{window}: 前後とも一覧があるのに上場廃止等が未登録")
        _reconcile(window, set(kinds[before]), set(kinds[after]), registered, successions.get(window, ()))

    gone_codes = {d.sec_code: (w, "delisted") for w, ds in delisted_during.items() for d in (ds or ())}
    gone_codes.update({s.old: (w, "succeeded") for w, ss in successions.items() for s in ss})

    resolution: dict = {}
    mentions = defaultdict(list)
    groups: dict = {}
    for key in sorted(reviews, key=lambda k: reviews[k].announced):
        codes = kinds[key]
        resolved = {sec: sec_to_ec[sec] for sec in codes if sec_to_ec.get(sec)}
        rate = len(resolved) / len(codes) if codes else 1.0
        unresolved = defaultdict(list)
        for sec in sorted(set(codes) - set(resolved)):
            unresolved["delisted" if sec in gone_codes else "no_company"].append(sec)
        resolution[key] = {"n_codes": len(codes), "n_resolved": len(resolved), "rate": round(rate, 4),
                           "unresolved": dict(unresolved),
                           "unresolved_by_kind": dict(Counter(codes[s] for s in codes if s not in resolved))}
        if rate < min_resolution:
            raise IndexLedgerError(
                f"{key}: edinet_code への突合率 {rate:.4f} < {min_resolution}（未突合 {dict(unresolved)}）")
        by_group = defaultdict(set)
        for sec, ec in resolved.items():
            mentions[ec].append((key, codes[sec]))
            by_group[(key, codes[sec])].add(ec)
        groups.update({g: frozenset(ecs) for g, ecs in by_group.items()})

    delisted = {sec_to_ec[sec]: v for sec, v in gone_codes.items() if sec_to_ec.get(sec)}

    d1, d2 = delisted_during.get("d1"), delisted_during.get("d2")
    continued = reviews.get("topix-2023-10-review").counts.get("transition") \
        if "topix-2023-10-review" in reviews else None
    stats = {
        "resolution": resolution,
        "precedent_2022": {
            "d1": None if d1 is None else len(d1),
            "successions_d1": len(successions.get("d1", ())),
            "d2": None if d2 is None else len(d2),
            # 2025-01 の除外数 = 継続 − d2（事前登録 §2）。d2 が未登録なら出さない
            "excluded_2025_01": None if d2 is None or continued is None else continued - len(d2),
        },
    }
    return IndexLedger(ok=True, mentions={ec: tuple(v) for ec, v in mentions.items()},
                       groups=groups, delisted=delisted, stats=stats, reviews=reviews)


def _require_ok(ledger: IndexLedger) -> None:
    if not ledger.ok:
        raise IndexLedgerError(f"台帳が使えない: {ledger.reason}")


def _group(kind: str, status: str) -> str:
    if kind == "unknown" or status in ("unknown", "not_yet_announced"):
        return "unknown"
    if status in ("excluded", "delisted"):
        return "other"
    return kind if kind in ("transition", "new_add") else "other"


def status_at(ledger: IndexLedger, ec: str, on: date, known_at: date) -> IndexStatus:
    """`known_at` の時点で分かっていた事実から、`on` の日のその社の指数イベントの状態を返す。"""
    _require_ok(ledger)
    reviews = ledger.reviews
    seen = [(key, kind) for key, kind in ledger.mentions.get(ec, ())
            if visible_from(reviews[key]) <= known_at]
    if not seen:
        return UNKNOWN
    key, kind = seen[-1]
    sched = SCHEDULES[(key, kind)]
    origin = reviews[sched.origin]
    effective = sched.steps[0].effective_on if sched.steps else origin.effective
    confirmed = all(s.date_basis == "primary" for s in sched.steps)

    def answer(status: str, coefficient) -> IndexStatus:
        coef = None if coefficient is None else float(coefficient)
        return IndexStatus(key, kind, status, _group(kind, status), coef, origin.announced,
                           effective, confirmed, reviews[key].source)

    gone = ledger.delisted.get(ec)
    if gone is not None:
        end = DELISTED_WINDOWS[gone[0]][1]
        if end is not None and end in reviews and visible_from(reviews[end]) <= known_at \
                and on >= reviews[end].announced:
            return answer("delisted", None)
    if on < origin.announced:
        return answer("not_yet_announced", 1)
    resolver = PENDING_REVIEWS.get((key, kind))
    if resolver is not None and on >= pending_from(resolver, reviews=reviews):
        return answer("pending", None)
    coefficient = Fraction(1)
    for step in sched.steps:
        if step.effective_on <= on:
            coefficient = step.coefficient
    if coefficient == 0:
        return answer("excluded", coefficient)
    if on < effective:
        return answer("announced", coefficient)
    return answer("in_effect", coefficient)


def group_labels(ledger: IndexLedger, ecs: Iterable[str], *, on: date, known_at: date) -> dict:
    """{ec: transition / new_add / other / unknown}（#1006 の群）。"""
    return {ec: status_at(ledger, ec, on, known_at).group for ec in ecs}


def members(ledger: IndexLedger, review: str, kind: str) -> frozenset:
    """ITT（最初の指定どおり）の群。例: `members(L, "topix-2022-10", "transition")` は 2022 前例の指定。"""
    _require_ok(ledger)
    if review not in ledger.reviews:
        raise IndexLedgerError(f"未登録の公表: {review}")
    return ledger.groups.get((review, kind), frozenset())


def panel_index_status(ledger: IndexLedger, asof_by_ym: Mapping, codes: Iterable[str]):
    """月×社の表（pandas DataFrame）。各月の as-of 日を `on` と `known_at` の両方に使う＝先読みしない。

    列は ym / edinet_code / review / kind / status / group / coefficient。
    """
    import pandas as pd

    _require_ok(ledger)
    codes = list(codes)
    rows = []
    for ym, asof in asof_by_ym.items():
        for ec in codes:
            st = status_at(ledger, ec, asof, asof)
            rows.append((ym, ec, st.review, st.kind, st.status, st.group, st.coefficient))
    return pd.DataFrame(rows, columns=["ym", "edinet_code", "review", "kind", "status", "group",
                                       "coefficient"])


# ── I/O ─────────────────────────────────────────────────────────────────────

def read_lists(root: Optional[Path] = None, *, reviews: Optional[Mapping] = None) -> tuple:
    """正規化 CSV（`<root>/<公表>.csv`）を読む。戻り値は `({公表: 行}, [無かった公表])`。

    sha256 が登録表と違う・形が崩れているときは `IndexLedgerError`。
    """
    root = DEFAULT_LISTS_DIR if root is None else Path(root)
    reviews = REVIEWS if reviews is None else reviews
    lists, missing = {}, []
    for key, review in reviews.items():
        path = root / f"{key}.csv"
        if not path.is_file():
            missing.append(key)
            continue
        data = path.read_bytes()
        actual = sha256_hex(data)
        if actual != review.normalized_sha256:
            raise IndexLedgerError(
                f"{key}: 正規化 CSV の sha256 が登録表と違う（{actual} / 登録表 {review.normalized_sha256 or '空'}）。"
                f"`python -I scripts/import_index_list.py {key}` で取り込み直す")
        lists[key] = parse_normalized(data)
    return lists, missing


def load_sec_to_ec(db) -> dict:
    """企業表から {sec_code: edinet_code}。同じ証券コードが複数社にあれば、その証券コードは引かない。"""
    from database import Company

    pairs = db.query(Company.sec_code, Company.edinet_code).filter(Company.sec_code.isnot(None)).all()
    by_sec = defaultdict(set)
    for sec, ec in pairs:
        if sec and sec.strip():
            by_sec[sec.strip()].add(ec)
    ambiguous = sorted(s for s, ecs in by_sec.items() if len(ecs) > 1)
    if ambiguous:
        log.warning("指数イベント台帳: 複数社に同じ証券コードがあるので引かない: %s", ambiguous[:10])
    return {s: next(iter(ecs)) for s, ecs in by_sec.items() if len(ecs) == 1}


def load_ledger(db=None, *, root: Optional[Path] = None,
                sec_to_ec: Optional[Mapping] = None) -> IndexLedger:
    """本番の入口。一覧のファイルが無ければ `ok=False` と理由を返し、壊れていれば例外を出す。"""
    lists, missing = read_lists(root)
    if missing:
        reason = f"一覧が無い: {missing}（`python -I scripts/import_index_list.py --all` で取り込む）"
        log.warning("指数イベント台帳: %s", reason)
        return IndexLedger(ok=False, reason=reason)
    if sec_to_ec is None:
        if db is None:
            from database import SessionLocal
            with SessionLocal() as session:
                sec_to_ec = load_sec_to_ec(session)
        else:
            sec_to_ec = load_sec_to_ec(db)
    ledger = build_ledger(lists, sec_to_ec)
    for key, r in ledger.stats["resolution"].items():
        log.info("指数イベント台帳: %s 突合 %d/%d（%.4f）未突合 %s", key, r["n_resolved"], r["n_codes"],
                 r["rate"], {k: len(v) for k, v in r["unresolved"].items()})
    return ledger
