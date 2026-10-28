"""指数イベント台帳（index_events.py・#1003・ADR-0068）のテスト。

DB・ネット・環境変数に触れない。JPX の一覧は CI に無いので、台帳は架空の小さな一覧と
`build_ledger(reviews=..., delisted_during=..., successions=...)` で作る（登録表の件数だけ差し替え、
日程と見直しの登録は本物を使う）。登録表そのものは事前登録文書の表と照合する。
"""
from __future__ import annotations

import re
import sys
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_events as IE  # noqa: E402

PREREG = ROOT / "docs" / "prereg" / "index-event-2026.md"


# ── 架空の一覧 ────────────────────────────────────────────────────────────────
# 2022 の指定 5 社 → 2023 の再評価で解除 1・継続 3（1006 は 1005 の承継先）・1004 は上場廃止等。
# 2026 は新規追加 2001・移行措置 1001・継続 3001。

LISTS = {
    "topix-2022-10": [("1001", "transition"), ("1002", "transition"), ("1003", "transition"),
                      ("1004", "transition"), ("1005", "transition")],
    "topix-2023-10-review": [("1001", "released"), ("1002", "transition"), ("1003", "transition"),
                             ("1006", "transition")],
    "topix-2026-10": [("2001", "new_add"), ("1001", "transition"),
                      ("2001", "constituent"), ("1001", "constituent"), ("3001", "constituent")],
}
COUNTS = {
    "topix-2022-10": {"transition": 5},
    "topix-2023-10-review": {"released": 1, "transition": 3},
    "topix-2026-10": {"new_add": 1, "transition": 1, "constituent": 3},
}
REVIEWS = {k: r._replace(counts=COUNTS[k], market_counts={}) for k, r in IE.REVIEWS.items()}
DELISTED = {"d1": (IE.Delisted("1004", "架空A"),), "d2": None}
SUCCESSIONS = {"d1": (IE.Succession("1005", "1006", False, "架空"),)}
SEC_TO_EC = {s: f"E{s}" for s in ("1001", "1002", "1003", "1004", "1005", "1006", "2001", "3001")}


def build(lists=None, sec_to_ec=None, **kw):
    kw.setdefault("reviews", REVIEWS)
    kw.setdefault("delisted_during", DELISTED)
    kw.setdefault("successions", SUCCESSIONS)
    return IE.build_ledger(LISTS if lists is None else lists,
                           SEC_TO_EC if sec_to_ec is None else sec_to_ec, **kw)


@pytest.fixture(scope="module")
def ledger():
    return build()


# ── 登録表 ↔ 事前登録文書 ────────────────────────────────────────────────────────

_CAL_ROW = re.compile(
    r"^\| (\d+) \| (\d{4}-\d{2}-\d{2})（.） \|[^|]*\|[^|]*\| ([0-9.]+)(?:（除外）)? \|(?: ([^|]+) \|)?$")


def _prereg_section(start: str, end: str) -> list:
    text = PREREG.read_text(encoding="utf-8")
    return text.split(start, 1)[1].split(end, 1)[0].splitlines()


def _calendar(start: str, end: str) -> list:
    rows = []
    for line in _prereg_section(start, end):
        m = _CAL_ROW.match(line.strip())
        if m:
            rows.append((int(m.group(1)), date.fromisoformat(m.group(2)), Fraction(m.group(3)),
                         (m.group(4) or "").strip()))
    return rows


class TestRegistryMatchesPrereg:
    """日程と照合値を書き写したずれを拾う（事前登録文書は凍結済みで、台帳が合わせる）。"""

    def test_2022_calendar(self):
        rows = _calendar("### 3.1 2022 前例", "### 3.2")
        assert len(rows) == 10
        for (m, d, coef, basis), step in zip(rows, IE.STEPS_2022):
            assert (step.m, step.effective_on, step.coefficient) == (m, d, coef)
            assert step.date_basis == ("rule" if basis == "規則からの推定" else "primary"), m

    def test_2026_calendar(self):
        rows = _calendar("### 3.2 2026 群", "### 3.3")
        assert len(rows) == 8
        for (m, d, coef, _), step in zip(rows, IE.STEPS_2026):
            assert (step.m, step.effective_on, step.coefficient, step.date_basis) == (m, d, coef, "primary")

    def test_r0_counts(self):
        lines = _prereg_section("## 2. 台帳の照合値", "## 3.")
        cells = [line.split("|")[2].strip() for line in lines if line.startswith("| 20")]
        got = [int(re.match(r"[\d,]+", c).group(0).replace(",", "")) for c in cells]
        r = IE.REVIEWS
        assert got == [r["topix-2022-10"].counts["transition"],
                       r["topix-2023-10-review"].counts["released"],
                       r["topix-2023-10-review"].counts["transition"],
                       r["topix-2026-10"].counts["new_add"],
                       r["topix-2026-10"].counts["transition"],
                       r["topix-2026-10"].counts["constituent"]]
        m = re.search(r"スタンダード (\d+)・グロース (\d+)・プライム (\d+)", "".join(cells))
        assert r["topix-2026-10"].market_counts["new_add"] == {
            "スタンダード": int(m.group(1)), "グロース": int(m.group(2)), "プライム": int(m.group(3))}


class TestRegistry:
    def test_precedent_closes_with_d1(self):
        """493 = 43 + 439 + d1（承継は 1 対 1 で入れ替わるので件数に効かない）。"""
        r = IE.REVIEWS
        assert r["topix-2022-10"].counts["transition"] == (
            r["topix-2023-10-review"].counts["released"] + r["topix-2023-10-review"].counts["transition"]
            + len(IE.DELISTED_DURING["d1"]))
        assert len(IE.DELISTED_DURING["d1"]) == 11
        assert len(IE.CODE_SUCCESSIONS["d1"]) == 2

    def test_d2_is_registered_per_company(self):
        """#1022: 継続 439 社のうち 2025-01 の除外より前に廃止した 15 社。2025-01 の除外数は 439 − 15。"""
        d2 = IE.DELISTED_DURING["d2"]
        assert len(d2) == 15
        codes = [d.sec_code for d in d2]
        assert len(set(codes)) == len(codes)
        assert not set(codes) & {d.sec_code for d in IE.DELISTED_DURING["d1"]}
        assert not set(codes) & {s.old for ss in IE.CODE_SUCCESSIONS.values() for s in ss}
        assert IE.REVIEWS["topix-2023-10-review"].counts["transition"] - len(d2) == 424

    def test_coefficients_are_formulas(self):
        assert [s.coefficient for s in IE.STEPS_2026] == [1 - Fraction(m, 8) for m in range(1, 9)]
        assert IE.STEPS_2022[-1].coefficient == 0 and IE.STEPS_2026[-1].coefficient == 0
        released = IE.SCHEDULES[("topix-2023-10-review", "released")].steps
        assert [float(s.coefficient) for s in released] == [0.9, 0.8, 0.7, 0.6, 0.7, 0.8, 0.9, 1.0]
        assert IE.SCHEDULES[("topix-2026-10", "new_add")].steps[0].coefficient == Fraction(3, 4)

    def test_review_effective_is_first_step(self):
        assert IE.REVIEWS["topix-2022-10"].effective == IE.STEPS_2022[0].effective_on
        assert IE.REVIEWS["topix-2023-10-review"].effective == IE.STEPS_2022[4].effective_on
        assert IE.REVIEWS["topix-2026-10"].effective == IE.STEPS_2026[0].effective_on

    def test_pending_reviews_resolve_to_known_dates(self):
        for (review, kind), resolver in IE.PENDING_REVIEWS.items():
            assert (review, kind) in IE.SCHEDULES
            assert resolver in IE.REVIEWS or resolver in IE.EXPECTED_REVIEWS
        assert IE.EXPECTED_REVIEWS["topix-2027-10-review"].effective == IE.STEPS_2026[4].effective_on

    def test_every_date_is_a_weekday(self):
        days = [s.effective_on for s in IE.STEPS_2022 + IE.STEPS_2026 + IE.STEPS_2023_RELEASE]
        days += [r.announced for r in IE.REVIEWS.values()]
        days += [d for e in IE.EXPECTED_REVIEWS.values() for d in (e.announce_expected, e.effective)]
        assert all(d.weekday() < 5 for d in days)

    def test_next_review_expected(self):
        key, exp = IE.next_review_expected()
        assert key == "topix-2027-10-review" and exp.announce_expected == date(2027, 10, 7)
        registered = {**IE.REVIEWS, "topix-2027-10-review": IE.REVIEWS["topix-2026-10"]}
        assert IE.next_review_expected(reviews=registered)[0] == "topix-2028-10"


# ── 台帳を作る ──────────────────────────────────────────────────────────────

class TestBuildLedger:
    def test_groups_and_continuing(self, ledger):
        assert ledger.ok
        assert ledger.groups[("topix-2026-10", "continuing")] == frozenset({"E3001"})
        assert ledger.groups[("topix-2026-10", "transition")] == frozenset({"E1001"})
        assert ledger.delisted == {"E1004": ("d1", "delisted"), "E1005": ("d1", "succeeded")}
        assert ledger.stats["precedent_2022"] == {
            "d1": 1, "successions_d1": 1, "d2": None, "excluded_2025_01": None}

    def test_excluded_count_needs_d2(self):
        led = build(delisted_during={"d1": DELISTED["d1"], "d2": (IE.Delisted("1003", "架空B"),)})
        assert led.stats["precedent_2022"]["excluded_2025_01"] == 2

    def test_count_mismatch_raises(self):
        lists = {**LISTS, "topix-2022-10": LISTS["topix-2022-10"][:-1]}
        with pytest.raises(IE.IndexLedgerError, match="件数"):
            build(lists)

    def test_duplicate_raises(self):
        reviews = {**REVIEWS, "topix-2022-10": REVIEWS["topix-2022-10"]._replace(counts={"transition": 6})}
        lists = {**LISTS, "topix-2022-10": LISTS["topix-2022-10"] + [("1001", "transition")]}
        with pytest.raises(IE.IndexLedgerError, match="重複"):
            build(lists, reviews=reviews)

    def test_new_add_must_be_a_constituent(self):
        lists = {**LISTS, "topix-2026-10": [("2001", "new_add"), ("1001", "transition"),
                                            ("1001", "constituent"), ("3001", "constituent"),
                                            ("3002", "constituent")]}
        with pytest.raises(IE.IndexLedgerError, match="構成銘柄に無い"):
            build(lists)

    def test_new_add_and_transition_are_disjoint(self):
        lists = {**LISTS, "topix-2026-10": [("1001", "new_add"), ("1001", "transition"),
                                            ("2001", "constituent"), ("1001", "constituent"),
                                            ("3001", "constituent")]}
        with pytest.raises(IE.IndexLedgerError, match="両方"):
            build(lists)

    def test_unexplained_departure_raises(self):
        with pytest.raises(IE.IndexLedgerError, match="未登録で消えた"):
            build(delisted_during={"d1": (), "d2": None})

    def test_unregistered_d1_raises_when_both_lists_exist(self):
        with pytest.raises(IE.IndexLedgerError, match="未登録"):
            build(delisted_during={"d1": None, "d2": None})

    def test_missing_successor_raises(self):
        with pytest.raises(IE.IndexLedgerError, match="一覧の差"):
            build(successions={"d1": ()})

    def test_low_resolution_raises(self):
        """突合率は一覧ごとの重複なしのコード集合で測る。"""
        sec_to_ec = {k: v for k, v in SEC_TO_EC.items() if k != "3001"}
        with pytest.raises(IE.IndexLedgerError, match="突合率"):
            build(sec_to_ec=sec_to_ec)

    def test_unresolved_are_counted_by_reason(self):
        sec_to_ec = {k: v for k, v in SEC_TO_EC.items() if k not in ("1004", "3001")}
        led = build(sec_to_ec=sec_to_ec, min_resolution=0.5)
        assert led.stats["resolution"]["topix-2022-10"]["unresolved"] == {"delisted": ["1004"]}
        r26 = led.stats["resolution"]["topix-2026-10"]
        assert r26["unresolved"] == {"no_company": ["3001"]}
        assert r26["unresolved_by_kind"] == {"continuing": 1}

    def test_missing_list_raises(self):
        lists = {k: v for k, v in LISTS.items() if k != "topix-2026-10"}
        with pytest.raises(IE.IndexLedgerError, match="渡されていない"):
            build(lists)


# ── 問い合わせ ──────────────────────────────────────────────────────────────

class TestStatusAt:
    def test_after_close_announcement_is_seen_from_next_day(self, ledger):
        assert IE.status_at(ledger, "E1001", date(2026, 10, 6), date(2026, 10, 6)).review == "topix-2023-10-review"
        assert IE.status_at(ledger, "E2001", date(2026, 10, 6), date(2026, 10, 6)) == IE.UNKNOWN
        assert IE.status_at(ledger, "E2001", date(2026, 10, 7), date(2026, 10, 7)) == IE.UNKNOWN
        st = IE.status_at(ledger, "E2001", date(2026, 10, 8), date(2026, 10, 8))
        assert (st.kind, st.status, st.group, st.coefficient) == ("new_add", "announced", "new_add", 1.0)

    def test_2026_transition_schedule(self, ledger):
        st = IE.status_at(ledger, "E1001", date(2026, 10, 30), date(2026, 10, 30))
        assert (st.review, st.kind, st.status, st.coefficient) == ("topix-2026-10", "transition", "in_effect", 0.875)
        assert (st.announced, st.effective, st.schedule_confirmed) == (date(2026, 10, 7), date(2026, 10, 30), True)
        assert IE.status_at(ledger, "E1001", date(2027, 10, 28), date(2027, 10, 28)).coefficient == 0.5

    def test_after_unregistered_reevaluation_is_pending(self, ledger):
        st = IE.status_at(ledger, "E1001", date(2027, 10, 29), date(2027, 10, 29))
        assert (st.status, st.group, st.coefficient) == ("pending", "transition", None)

    def test_new_add_and_continuing(self, ledger):
        st = IE.status_at(ledger, "E2001", date(2026, 10, 30), date(2026, 10, 30))
        assert (st.status, st.coefficient) == ("in_effect", 0.75)
        assert IE.status_at(ledger, "E2001", date(2028, 10, 31), date(2028, 10, 31)).status == "pending"
        st = IE.status_at(ledger, "E3001", date(2026, 11, 2), date(2026, 11, 2))
        assert (st.kind, st.group, st.coefficient) == ("continuing", "other", 1.0)
        assert IE.status_at(ledger, "E3001", date(2028, 10, 31), date(2028, 10, 31)).coefficient is None

    def test_not_in_any_list_is_unknown(self, ledger):
        assert IE.status_at(ledger, "E9999", date(2026, 11, 2), date(2026, 11, 2)) == IE.UNKNOWN

    def test_precedent_before_and_after_reevaluation(self, ledger):
        # 再評価の公表前に見えていたのは 2022 の指定だけ。5 回目（2023-10-31）以降は分からない
        st = IE.status_at(ledger, "E1001", date(2023, 10, 31), date(2023, 10, 6))
        assert (st.review, st.status, st.coefficient) == ("topix-2022-10", "pending", None)
        st = IE.status_at(ledger, "E1001", date(2023, 7, 31), date(2023, 7, 31))
        assert (st.status, st.coefficient) == ("in_effect", 0.6)
        # 解除: 0.7 → 1.0（2024-07-31）
        st = IE.status_at(ledger, "E1001", date(2023, 10, 31), date(2023, 10, 31))
        assert (st.review, st.kind, st.group, st.coefficient) == ("topix-2023-10-review", "released", "other", 0.7)
        assert IE.status_at(ledger, "E1001", date(2024, 7, 31), date(2024, 7, 31)).coefficient == 1.0
        # 継続: 2025-01-31 に除外
        st = IE.status_at(ledger, "E1002", date(2025, 1, 31), date(2025, 1, 31))
        assert (st.status, st.group, st.coefficient) == ("excluded", "other", 0.0)
        assert IE.status_at(ledger, "E1002", date(2025, 1, 30), date(2025, 1, 30)).coefficient == 0.1

    def test_delisted_and_succeeded(self, ledger):
        for ec in ("E1004", "E1005"):
            assert IE.status_at(ledger, ec, date(2023, 10, 6), date(2023, 10, 7)).status == "delisted"
            # 廃止日は分からないので、窓の中の日付は指定どおりに答える
            assert IE.status_at(ledger, ec, date(2023, 1, 5), date(2023, 10, 7)).coefficient == 0.9
        st = IE.status_at(ledger, "E1006", date(2024, 2, 1), date(2024, 2, 1))
        assert (st.review, st.kind, st.coefficient) == ("topix-2023-10-review", "transition", 0.4)

    def test_hindsight_before_announcement(self, ledger):
        st = IE.status_at(ledger, "E1002", date(2022, 9, 30), date(2024, 1, 1))
        assert (st.status, st.group, st.coefficient) == ("not_yet_announced", "unknown", 1.0)

    def test_ledger_not_ok_raises(self):
        bad = IE.IndexLedger(ok=False, reason="一覧が無い")
        with pytest.raises(IE.IndexLedgerError, match="一覧が無い"):
            IE.status_at(bad, "E1001", date(2026, 11, 2), date(2026, 11, 2))
        with pytest.raises(IE.IndexLedgerError):
            IE.members(bad, "topix-2026-10", "transition")


def test_group_labels(ledger):
    got = IE.group_labels(ledger, ["E1001", "E2001", "E3001", "E9999"], on=date(2026, 11, 2),
                          known_at=date(2026, 11, 2))
    assert got == {"E1001": "transition", "E2001": "new_add", "E3001": "other", "E9999": "unknown"}


def test_members_is_itt(ledger):
    assert IE.members(ledger, "topix-2022-10", "transition") == frozenset(
        {"E1001", "E1002", "E1003", "E1004", "E1005"})
    with pytest.raises(IE.IndexLedgerError, match="未登録"):
        IE.members(ledger, "topix-2099-10", "transition")


def test_panel_index_status_does_not_look_ahead(ledger):
    df = IE.panel_index_status(ledger, {"2026-09": date(2026, 9, 30), "2026-10": date(2026, 10, 30)},
                               ["E2001"])
    assert list(df.columns) == ["ym", "edinet_code", "review", "kind", "status", "group", "coefficient"]
    assert df.set_index("ym")["group"].to_dict() == {"2026-09": "unknown", "2026-10": "new_add"}
    assert df.set_index("ym").loc["2026-10", "coefficient"] == 0.75


# ── I/O ─────────────────────────────────────────────────────────────────────

class TestReadLists:
    def test_normalized_roundtrip(self):
        rows = [("2001", "new_add"), ("1001", "transition"), ("1001", "constituent")]
        data = IE.normalized_bytes(rows)
        assert data == b"sec_code,kind\n1001,constituent\n2001,new_add\n1001,transition\n"
        assert sorted(IE.parse_normalized(data)) == sorted(rows)

    def test_bad_header_raises(self):
        with pytest.raises(IE.IndexLedgerError, match="見出し"):
            IE.parse_normalized(b"code,kind\n1001,transition\n")

    def test_missing_files_give_not_ok(self, tmp_path):
        led = IE.load_ledger(root=tmp_path, sec_to_ec={})
        assert not led.ok and "一覧が無い" in led.reason

    def test_sha_mismatch_raises(self, tmp_path):
        (tmp_path / "topix-2022-10.csv").write_bytes(IE.normalized_bytes([("1001", "transition")]))
        with pytest.raises(IE.IndexLedgerError, match="sha256"):
            IE.read_lists(tmp_path)

    def test_reads_when_sha_matches(self, tmp_path):
        data = IE.normalized_bytes(LISTS["topix-2022-10"])
        reviews = {"topix-2022-10": REVIEWS["topix-2022-10"]._replace(normalized_sha256=IE.sha256_hex(data))}
        (tmp_path / "topix-2022-10.csv").write_bytes(data)
        lists, missing = IE.read_lists(tmp_path, reviews=reviews)
        assert missing == [] and sorted(lists["topix-2022-10"]) == sorted(LISTS["topix-2022-10"])


# ── import ガード ────────────────────────────────────────────────────────────

def test_ledgers_feeding_f_and_ttm_do_not_import_index_events():
    """指数イベントを F・TTM・`tradable_filters`（database.py）へ流さない（ADR-0068）。"""
    for name in ("corporate_actions.py", "ttm_composite.py", "database.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "import index_events" not in src and "from index_events" not in src, name


def test_pure_blocks_do_not_import_heavy_modules_at_top_level():
    src = (ROOT / "index_events.py").read_text(encoding="utf-8")
    head = src.split("# ── I/O ──")[0]
    for bad in ("\nimport database", "\nfrom database import", "\nimport httpx", "\nimport pandas"):
        assert bad not in head
    assert "from scripts" not in src and "import scripts" not in src


# ── TOPIX 以外の指数の一覧（交絡の記述用・#1065）────────────────────────────────

class TestConfounderLists:
    KEY = "growth250-2026-10"

    def _write(self, tmp_path, monkeypatch, rows):
        data = IE.normalized_bytes(rows)
        (tmp_path / f"{self.KEY}.csv").write_bytes(data)
        review = IE.CONFOUNDER_LISTS[self.KEY]
        counts = {}
        for _sec, kind in rows:
            counts[kind] = counts.get(kind, 0) + 1
        monkeypatch.setitem(IE.CONFOUNDER_LISTS, self.KEY,
                            review._replace(normalized_sha256=IE.sha256_hex(data), counts=counts))

    def test_keys_never_overlap_the_topix_ledger(self):
        assert not set(IE.CONFOUNDER_LISTS) & set(IE.REVIEWS)
        kinds = {k for r in IE.CONFOUNDER_LISTS.values() for k in r.counts}
        assert not kinds & {k for r in IE.REVIEWS.values() for k in r.counts}

    def test_registered_counts_match_the_announcement(self):
        assert IE.CONFOUNDER_LISTS[self.KEY].counts == {"g250_add": 31, "g250_remove": 52}

    def test_members_by_kind_and_unresolved_codes_are_counted(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [("1111", "g250_add"), ("2222", "g250_remove"), ("3333", "g250_remove")])
        got = IE.confounder_members(self.KEY, {"1111": "E1", "2222": "E2"}, root=tmp_path)
        assert got == {"by_kind": {"g250_add": frozenset({"E1"}), "g250_remove": frozenset({"E2"})},
                       "n_codes": 3, "n_resolved": 2}

    def test_missing_list_is_none(self, tmp_path):
        assert IE.confounder_members(self.KEY, {}, root=tmp_path) is None

    def test_sha_mismatch_raises(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [("1111", "g250_add")])
        (tmp_path / f"{self.KEY}.csv").write_bytes(IE.normalized_bytes([("9999", "g250_add")]))
        with pytest.raises(IE.IndexLedgerError):
            IE.confounder_members(self.KEY, {}, root=tmp_path)

    def test_the_topix_ledger_does_not_read_it(self, tmp_path):
        """`read_lists` の既定は `REVIEWS` だけ＝台帳（`status_at`・C1 対照・画面のタグ）に流れない。"""
        (tmp_path / f"{self.KEY}.csv").write_bytes(IE.normalized_bytes([("1111", "g250_add")]))
        _lists, missing = IE.read_lists(tmp_path)
        assert self.KEY not in missing and set(missing) == set(IE.REVIEWS)
