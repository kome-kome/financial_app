"""指数イベントの画面注記（index_event_notes.py・#1013）のテスト。

台帳は架空の小さな一覧と `build_ledger(reviews=...)` で作る（日程と見直しの登録は本物を使う・
test_index_events.py と同じ作り方）。JPX の一覧・本物の DB には触れない。文言はすべて禁止語と照合し、
推奨・売りは台帳あり・なしで順位とスコアが変わらないことを確かめる。
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_event_notes as N  # noqa: E402
import index_events as IE  # noqa: E402

# 2022 の指定 2 社（2023 の再評価で 1001 は解除・1002 は継続）。2026 は新規追加 2001・移行措置 2002・継続 3001。
LISTS = {
    "topix-2022-10": [("1001", "transition"), ("1002", "transition")],
    "topix-2023-10-review": [("1001", "released"), ("1002", "transition")],
    "topix-2026-10": [("2001", "new_add"), ("2002", "transition"),
                      ("2001", "constituent"), ("2002", "constituent"), ("3001", "constituent")],
}
COUNTS = {
    "topix-2022-10": {"transition": 2},
    "topix-2023-10-review": {"released": 1, "transition": 1},
    "topix-2026-10": {"new_add": 1, "transition": 1, "constituent": 3},
}
REVIEWS = {k: r._replace(counts=COUNTS[k], market_counts={}) for k, r in IE.REVIEWS.items()}
SEC_TO_EC = {s: f"E{s}" for s in ("1001", "1002", "2001", "2002", "3001")}
TODAY = date(2026, 10, 22)


def _ledger(sec_to_ec=None):
    return IE.build_ledger(LISTS, sec_to_ec or SEC_TO_EC, reviews=REVIEWS,
                           delisted_during={"d1": (), "d2": None}, successions={"d1": ()})


@pytest.fixture(autouse=True)
def _fresh_cache():
    # 台帳のキャッシュはプロセス内で共有される。差し替えた台帳を他のテストへ漏らさない
    N.clear_cache()
    yield
    N.clear_cache()


@pytest.fixture
def ledger():
    return _ledger()


@pytest.fixture
def wired(monkeypatch):
    lg = _ledger()
    monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: lg)
    monkeypatch.setattr(N, "jst_today", lambda: TODAY)
    return lg


# ── 行の注記 ─────────────────────────────────────────────────────────────────────

class TestRowNote:
    def test_transition_before_the_first_reduction(self, ledger):
        n = N.row_note(ledger, "E2002", TODAY, TODAY)
        assert (n["label"], n["kind"], n["status"], n["schedule_confirmed"]) == \
            ("TOPIX移行", "transition", "announced", True)
        assert "出典: JPX「TOPIX初回定期入替における選定結果及び構成銘柄一覧」2026-10-07 公表" in n["note"]
        assert "2026-10-30 から" in n["note"] and "（8 回）" in n["note"]
        assert "2027-10 の見直しの結果によって、以降の日程が変わる場合があります。" in n["note"]
        assert n["note"].endswith(N.IMPACT_NOTE)
        assert n["source"].startswith("出典: JPX")

    def test_new_addition(self, ledger):
        n = N.row_note(ledger, "E2001", TODAY, TODAY)
        assert n["label"] == "TOPIX新規"
        assert "調整係数 0.75 で組み入れ" in n["note"]
        assert "2028-10 の見直しの結果によって" in n["note"]

    @pytest.mark.parametrize("ec", ["E3001", "E1001", "E1002", "E9999"])
    def test_continuing_released_excluded_and_unknown_get_no_note(self, ledger, ec):
        assert N.row_note(ledger, ec, TODAY, TODAY) is None

    def test_pending_after_the_unregistered_review(self, ledger):
        on = date(2027, 11, 1)                      # 2027-10 の再評価（未登録）の反映日より後
        n = N.row_note(ledger, "E2002", on, on)
        assert n["status"] == "pending"
        assert "以降の日程は 2027-10 の見直しの結果待ちです（公式資料で確認中）。" in n["note"]

    def test_unconfirmed_schedule_says_so(self, ledger):
        # 2022 前例は 10 回のうち 8 回が規則からの推定。2023-03 の時点では再評価の結果はまだ見えない
        on = date(2023, 3, 1)
        n = N.row_note(ledger, "E1002", on, on, with_schedule=True)
        assert n["schedule_confirmed"] is False
        assert "日程の一部は規則からの推定です（公式資料で確認中）。" in n["note"]
        assert "2023-10 の見直しの結果によって" in n["note"]
        texts = {row["m"]: row["date_text"] for row in n["schedule"]}
        assert texts[5] == "2023-10-31" and texts[10] == "2025-01-31"     # 一次資料の回だけ日付
        assert texts[1] == N.UNCONFIRMED_NOTE

    def test_note_disappears_52_weeks_after_the_last_change(self, ledger):
        known = date(2023, 3, 1)                    # 2022 の一覧だけが見えている＝見直し待ちのまま
        assert N.row_note(ledger, "E1002", date(2026, 1, 30), known) is not None
        assert N.row_note(ledger, "E1002", date(2026, 1, 31), known) is None

    def test_company_schedule_for_2026(self, ledger):
        n = N.row_note(ledger, "E2002", TODAY, TODAY, with_schedule=True)
        assert [r["date_text"] for r in n["schedule"]][:2] == ["2026-10-30", "2027-01-29"]
        assert n["schedule"][-1]["coefficient"] == 0.0


# ── 文言の禁止語 ─────────────────────────────────────────────────────────────────

def _all_texts(ledger) -> list:
    texts = [N.BANNER_NO_LEDGER, N.IMPACT_NOTE, N.UNCONFIRMED_NOTE, *N.DISPLAY_KINDS.values()]
    cases = [("E2002", TODAY, TODAY), ("E2001", TODAY, TODAY), ("E2002", date(2027, 11, 1), date(2027, 11, 1)),
             ("E1002", date(2023, 3, 1), date(2023, 3, 1)), ("E1001", date(2023, 3, 1), date(2023, 3, 1)),
             ("E1002", date(2025, 6, 1), date(2023, 3, 1))]
    for ec, on, known in cases:
        n = N.row_note(ledger, ec, on, known, with_schedule=True)
        assert n is not None, (ec, on)
        texts += [n["note"], n["label"], n["source"], *(r["date_text"] for r in n["schedule"])]
    texts.append(N.summary_text("上位", 30, {"transition": 5, "new_add": 1},
                                {"transition": 0.197, "new_add": 0.009}))
    texts.append(N.summary_text("保有", 3, {"transition": 0}, {"transition": None, "new_add": None}))
    return texts


def test_no_forbidden_words_in_any_text(ledger):
    for text in _all_texts(ledger):
        for word in N.FORBIDDEN_WORDS:
            assert word not in text, (word, text)


def test_forbidden_words_cover_the_issue_rules():
    assert "除外予定" in N.FORBIDDEN_WORDS
    assert {"売り", "買い", "下落", "上昇"} <= set(N.FORBIDDEN_WORDS)


# ── 表の注記・台帳のキャッシュ ─────────────────────────────────────────────────────

class TestAnnotate:
    def test_counts_base_rate_and_summary(self, wired):
        notes, meta = N.annotate(None, ["E2002", "E2001", "E3001"],
                                 ["E2002", "E2001", "E3001", "E9999"], what="上位")
        assert notes["E3001"] is None and notes["E2002"]["label"] == "TOPIX移行"
        assert meta["ledger_ok"] is True and meta["banner"] is None and meta["asof"] == "2026-10-22"
        assert meta["counts_in_results"] == {"transition": 1, "new_add": 1}
        assert meta["base_rate"] == {"transition": 0.25, "new_add": 0.25}
        assert meta["summary"] == ("上位 3 社中、TOPIX 移行措置 1 社（母集団では 25.0%）"
                                   "・新規採用 1 社（母集団では 25.0%）")

    def test_without_the_ledger_shows_the_banner(self, monkeypatch):
        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: IE.IndexLedger(ok=False, reason="一覧が無い"))
        notes, meta = N.annotate(None, ["E2002"], ["E2002"], what="上位")
        assert notes == {"E2002": None}
        assert meta["ledger_ok"] is False and meta["banner"] == N.BANNER_NO_LEDGER
        assert meta["summary"] is None and meta["reason"] == "一覧が無い"

    def test_broken_ledger_does_not_break_the_page(self, monkeypatch):
        def broken(db=None, **kw):
            raise IE.IndexLedgerError("sha256 が登録表と違う")
        monkeypatch.setattr(IE, "load_ledger", broken)
        assert N.cached_ledger(None).ok is False

    def test_ledger_is_kept_for_the_ttl(self, monkeypatch):
        calls = []
        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: calls.append(1) or _ledger())
        now = [0.0]
        N.cached_ledger(None, clock=lambda: now[0])
        now[0] = N.LEDGER_TTL_S - 1
        N.cached_ledger(None, clock=lambda: now[0])
        assert len(calls) == 1
        now[0] = N.LEDGER_TTL_S + 1
        N.cached_ledger(None, clock=lambda: now[0])
        assert len(calls) == 2

    def test_company_note(self, wired):
        note, meta = N.company_note(None, "E2002")
        assert note["schedule"] and meta["ledger_ok"] is True
        assert N.company_note(None, "E3001")[0] is None


# ── 推奨・売り・財務 API（順位は変えない）─────────────────────────────────────────

def _recommend(db):
    from plugins import execute_plugin
    from plugins.recommend import plugin
    return asyncio.run(execute_plugin(plugin, {"weights": {"z_roe": 1.0}, "min_coverage": 0.0}, db))


def _seed(db, make_metric):
    db.add_all([make_metric(edinet_code=f"E{s}", sec_code=s, company_name=f"会社{s}", year=2025,
                            z_roe=float(i), roe=float(i))
                for i, s in enumerate(("1001", "1002", "2001", "2002", "3001"))])
    db.commit()


class TestPlugins:
    def test_recommend_ranking_is_the_same_with_and_without_the_ledger(self, db, make_metric, monkeypatch):
        _seed(db, make_metric)
        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: IE.IndexLedger(ok=False, reason="x"))
        without = _recommend(db)
        N.clear_cache()
        lg = _ledger()
        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: lg)
        monkeypatch.setattr(N, "jst_today", lambda: TODAY)
        with_ = _recommend(db)
        key = [(r["rank"], r["edinet_code"], r["score"]) for r in without["results"]]
        assert key == [(r["rank"], r["edinet_code"], r["score"]) for r in with_["results"]]
        assert without["index_events"]["ledger_ok"] is False
        assert all(r["index_event"] is None for r in without["results"])
        tags = {r["edinet_code"]: (r["index_event"] or {}).get("label") for r in with_["results"]}
        assert tags == {"E1001": None, "E1002": None, "E2001": "TOPIX新規", "E2002": "TOPIX移行", "E3001": None}
        assert with_["index_events"]["counts_in_results"] == {"transition": 1, "new_add": 1}
        assert with_["index_events"]["summary"].startswith("上位 5 社中、TOPIX 移行措置 1 社")

    def test_sell_ranking_is_the_same_with_and_without_the_ledger(self, db, make_metric, monkeypatch):
        from plugins.sell_ranking import plugin
        from plugins.utils import coerce_params

        _seed(db, make_metric)
        raw = {"holdings": "2002\n3001", "weights": {"roe": 1.0}, "min_coverage": 0.0}

        def run():
            return plugin.execute(coerce_params(plugin.params_schema(), raw), db)

        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: IE.IndexLedger(ok=False, reason="x"))
        without = run()
        N.clear_cache()
        lg = _ledger()
        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: lg)
        monkeypatch.setattr(N, "jst_today", lambda: TODAY)
        with_ = run()
        key = [(r["rank"], r["sec_code"], r["score"], r["action"]) for r in without["results"]]
        assert key == [(r["rank"], r["sec_code"], r["score"], r["action"]) for r in with_["results"]]
        assert {r["sec_code"]: (r["index_event"] or {}).get("label") for r in with_["results"]} == \
            {"2002": "TOPIX移行", "3001": None}
        assert with_["index_events"]["summary"].startswith("保有 2 社中、TOPIX 移行措置 1 社（母集団では 20.0%）")
        assert without["index_events"]["banner"] == N.BANNER_NO_LEDGER

    def test_financials_endpoint_carries_the_note(self, db, make_metric, monkeypatch):
        from fastapi.testclient import TestClient

        import api

        # API は edinet_code を「E + 数字 5 桁」で検証するので、この台帳だけ 5 桁のコードで作る
        db.add(make_metric(edinet_code="E02002", year=2025, period_end="2025-03-31"))
        db.commit()
        lg = _ledger({s: f"E0{s}" for s in SEC_TO_EC})
        monkeypatch.setattr(IE, "load_ledger", lambda db=None, **kw: lg)
        monkeypatch.setattr(N, "jst_today", lambda: TODAY)
        api.app.dependency_overrides[api.get_db] = lambda: db
        try:
            r = TestClient(api.app).get("/api/financials/E02002")
        finally:
            api.app.dependency_overrides.clear()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["index_event"]["label"] == "TOPIX移行"
        assert body["index_event"]["schedule"][0]["date_text"] == "2026-10-30"
        assert body["index_events"]["ledger_ok"] is True


def test_js_only_renders_the_server_text():
    """文言はサーバで作る（禁止語の検査を 1 か所でかける）。JS は esc() を通して並べるだけ。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    common = open(os.path.join(root, "static", "js", "common.js"), encoding="utf-8").read()
    assert "esc(ev.note)" in common and "esc(ev.label)" in common and "esc(text)" in common
    for word in N.FORBIDDEN_WORDS:
        block = common[common.index("function indexEventBadge"):common.index("function indexEventsNote") + 400]
        assert word not in block
