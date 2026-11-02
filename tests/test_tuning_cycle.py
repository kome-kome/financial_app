"""探索の周回（#1114・ADR-0070 決定 4・5）——`tuning_cycle` の規則と `hyperparameter_search.run_cycle`。

日中枠の空きで M-2 の固定の候補集合を数日に分けて回し切り、周回の終わりに 1 回だけ本番へ反映する。守るのは:

1. **束の分け方が決定的で、1 周回の中で各候補が 1 回ずつ新しく測られる**（失敗した候補で止まらない）
2. **前日までの勝者はその日のデータで測り直す**（評価値を翌日へ持ち越さない＝世代の違う値を混ぜない）
3. **周回の途中では `plugin_tuned_params` を書かず、終わりに 1 回だけ書く**
4. 残り時間が足りない日は測らない・確かめ直す段が入らない日は次の日に回す
5. 今月の周回が済んだら次の月まで何もしない・候補集合の定義が変わったら始め直す
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

import hyperparameter_search as hs
import tuning_cycle as tc


# ── 偽物のプラグイン ─────────────────────────────────────────────────────────────

class _CyclePlugin:
    """x=7 が最適の 1 軸（12 候補）。x=4 は契約違反で必ず落ちる。種では結果が動かない。"""
    name = "cycle_model"
    depends_on: list = []
    tuning_seed_base = 0
    calls: list = []
    values = list(range(12))

    def params_schema(self) -> dict:
        return {"x": {"type": "slider", "dtype": "int", "default": 0, "min": 0, "max": 20}}

    def tuning_search_space(self):
        from plugins.tuning import SearchDim
        return {}, [SearchDim("x", list(type(self).values))]

    def execute(self, params: dict, db) -> dict:
        type(self).calls.append(params["x"])
        if params["x"] == 4:
            raise ValueError("契約違反の候補")
        return {"oof_backtest": {
            "rank_ic": {"mean": float(-(params["x"] - 7) ** 2), "std": 1.0, "n": 3},
            "n_periods": 3, "n_oof_samples": 300,
        }}


@pytest.fixture(autouse=True)
def _plugin(monkeypatch):
    import plugins
    _CyclePlugin.calls = []
    _CyclePlugin.values = list(range(12))
    monkeypatch.setattr(plugins, "get_plugin", lambda name: _CyclePlugin())


@pytest.fixture
def per_day(monkeypatch):
    """探索 1 回で測れる候補を先頭 k 件に絞る（締切で畳む日の代わり。`k=None` なら絞らない）。"""
    import plugins.tuning as tuning

    real = tuning.search
    box = {"k": None}

    async def _search(*args, candidates=None, **kwargs):
        if candidates is not None and box["k"] is not None:
            candidates = candidates[:box["k"]]
        return await real(*args, candidates=candidates, **kwargs)

    monkeypatch.setattr(tuning, "search", _search)
    return box


@pytest.fixture
def writes(monkeypatch):
    """`plugin_tuned_params` への書き込みを数える（`run_search` は呼ぶたびに database から import する）。"""
    import database

    real = database.upsert_tuned_params
    seen: list = []

    def _upsert(db, name, params, *args, **kwargs):
        seen.append(dict(params))
        return real(db, name, params, *args, **kwargs)

    monkeypatch.setattr(database, "upsert_tuned_params", _upsert)
    return seen


def _cycle(db, today, **kw):
    kw.setdefault("confirm_top", 2)
    kw.setdefault("confirm_seeds", 2)
    return asyncio.run(hs.run_cycle("cycle_model", "grid", 0, "rank_ic", 0, db, today=today, **kw))


def _state(db):
    from database import get_setting
    return tc.load(get_setting(db, tc.state_key("cycle_model")))


# ── 周回を数日に分けて回す ───────────────────────────────────────────────────────

class TestACycleOverSeveralDays:

    def test_every_candidate_is_measured_once_and_production_is_written_once(self, db, per_day, writes):
        per_day["k"] = 5
        days = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
        for d in days[:-1]:
            out = _cycle(db, d, persist=True)
            assert out["action"] == "sweep"
            assert writes == [], f"{d}: 周回の途中で本番の表を書いた"
        out = _cycle(db, days[-1], persist=True)
        assert out["action"] == "sweep+confirm"

        state = _state(db)
        spans = [(d["from"], d["to"]) for d in state["days"]]
        assert spans == [(0, 5), (5, 8), (8, 11), (11, 12)], "束が先頭から連続していない（測り漏れか二重計上）"
        assert state["phase"] == tc.PHASE_DONE
        assert len(writes) == 1, "本番への反映が周回の終わりの 1 回になっていない"
        assert writes[0]["x"] == 7

    def test_a_failing_candidate_does_not_stall_the_cycle(self, db, per_day):
        """x=4 は毎回落ちる。評価済みに数えないと周回が永久にそこで止まる。"""
        per_day["k"] = 5
        _cycle(db, date(2026, 11, 4))
        assert _state(db)["next_index"] == 5, "落ちた候補（x=4）の手前で止まった"

    def test_the_winners_are_remeasured_on_the_next_day(self, db, per_day):
        per_day["k"] = 5
        _cycle(db, date(2026, 11, 4))
        finalists = [f["x"] for f in _state(db)["finalists"]]
        assert finalists == [3, 2]
        assert all(set(f) == {"x"} for f in _state(db)["finalists"]), "評価値を翌日へ持ち越している"

        _CyclePlugin.calls = []
        _cycle(db, date(2026, 11, 5))
        assert _CyclePlugin.calls[:2] == [3, 2], "前日の勝者をその日のデータで測り直していない"
        assert _CyclePlugin.calls[2:] == [5, 6, 7]

    def test_the_production_setting_leads_the_first_day(self, db, per_day):
        from database import upsert_tuned_params
        upsert_tuned_params(db, "cycle_model", {"x": 9}, "rank_ic", -4.0, [], 1, "fp")
        per_day["k"] = 3
        _cycle(db, date(2026, 11, 4), persist=True)
        assert _CyclePlugin.calls[0] == 9, "初日の先頭に本番の設定（champion）が居ない"

    def test_confirm_waits_for_the_next_day_when_it_does_not_fit(self, db, per_day, writes, monkeypatch):
        monkeypatch.setattr(tc, "confirm_fits", lambda *a, **k: False)
        out = _cycle(db, date(2026, 11, 4), persist=True)
        assert out["action"] == "sweep"
        assert _state(db)["phase"] == tc.PHASE_CONFIRM
        assert writes == []

        out = _cycle(db, date(2026, 11, 5), persist=True)
        assert out["action"] == "confirm"
        assert _state(db)["phase"] == tc.PHASE_DONE
        assert len(writes) == 1


class TestWhenTheCycleRuns:

    def test_short_remaining_time_measures_nothing(self, db):
        deadline = datetime.now(timezone.utc) + timedelta(minutes=hs.FINAL_RESERVE_MIN + 10)
        out = _cycle(db, date(2026, 11, 4), deadline=deadline)
        assert out["action"] == "skip"
        assert _CyclePlugin.calls == []
        assert _state(db) is None, "測っていない日に新しい周回を書いた"

    def test_a_finished_month_is_idle_and_the_next_month_starts_over(self, db, writes):
        _cycle(db, date(2026, 11, 4), persist=True)
        assert _state(db)["phase"] == tc.PHASE_DONE

        _CyclePlugin.calls = []
        out = _cycle(db, date(2026, 11, 20), persist=True)
        assert out["action"] == "idle"
        assert _CyclePlugin.calls == [], "済んだ月にまた測った"

        out = _cycle(db, date(2026, 12, 2), persist=True)
        state = _state(db)
        assert state["cycle_id"] == "2026-12-02"
        assert [h["cycle_id"] for h in state["history"]] == ["2026-11-04", "2026-12-02"]
        assert len(writes) == 2

    def test_changing_the_candidate_set_restarts_the_cycle(self, db, per_day):
        per_day["k"] = 5
        _cycle(db, date(2026, 11, 4))
        _CyclePlugin.values = list(range(10))
        _cycle(db, date(2026, 11, 5))
        state = _state(db)
        assert state["cycle_id"] == "2026-11-05"
        assert [(d["from"], d["to"]) for d in state["days"]] == [(0, 5)]

    def test_a_dry_cycle_does_not_move_the_watchdog_clock(self, db):
        """試し撃ち（persist なし）で終えた周回は、本番へ反映した時刻を進めない。"""
        _cycle(db, date(2026, 11, 4))
        state = _state(db)
        assert state["phase"] == tc.PHASE_DONE
        assert state["last_finished_at"] is None
        assert tc.last_finished_at(state) == tc.INTRODUCED_AT


class TestCli:

    def test_cycle_requires_the_confirm_settings(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["hyperparameter_search.py", "--model", "macro_gbdt", "--cycle"])
        with pytest.raises(SystemExit) as e:
            hs.main()
        assert e.value.code == 2


# ── 純関数 ───────────────────────────────────────────────────────────────────────

COMBOS = [{"x": i} for i in range(6)]


def _fresh(**over):
    st = tc.new_state(model="m", today=date(2026, 11, 4), fp=tc.fingerprint(COMBOS), n_total=len(COMBOS))
    st.update(over)
    return st


class TestPureRules:

    def test_combo_key_is_shared_with_the_search_engine(self):
        from plugins.tuning import _combo_key
        c = {"b": 1, "a": 2.0, "c": True}
        assert _combo_key(c) == tc.combo_key(c)

    def test_todays_candidates_put_finalists_first_without_duplicates(self):
        st = _fresh(finalists=[{"x": 4}, {"x": 1}], next_index=3)
        assert tc.todays_candidates(st, COMBOS) == [{"x": 4}, {"x": 1}, {"x": 3}, {"x": 5}]

    def test_advance_counts_the_contiguous_prefix(self):
        st = _fresh(next_index=2)
        evaluated = [{"x": 2}, {"x": 3}, {"x": 5}]          # x=4 は測っていない（畳んだ）
        scored = [{"params": {"x": 3}, "score": 0.2, "elapsed_sec": 120.0},
                  {"params": {"x": 2}, "score": 0.1, "elapsed_sec": 60.0}]
        new = tc.advance(st, COMBOS, evaluated, scored, today=date(2026, 11, 4), top=1,
                         minutes=3.0, truncated=True)
        assert new["next_index"] == 4
        assert new["finalists"] == [{"x": 3}]
        assert new["worst_min"] == 2.0
        assert new["phase"] == tc.PHASE_SWEEP

    def test_advance_moves_to_confirm_once_everything_is_measured(self):
        st = _fresh(next_index=4, finalists=[{"x": 1}])
        evaluated = [{"x": 1}, {"x": 4}, {"x": 5}]
        scored = [{"params": {"x": 5}, "score": 0.3}, {"params": {"x": 1}, "score": 0.1}]
        new = tc.advance(st, COMBOS, evaluated, scored, today=date(2026, 11, 5), top=2,
                         minutes=1.0, truncated=False)
        assert new["phase"] == tc.PHASE_CONFIRM
        assert new["finalists"] == [{"x": 5}, {"x": 1}]

    def test_finalists_are_kept_when_nothing_scored(self):
        st = _fresh(finalists=[{"x": 1}])
        new = tc.advance(st, COMBOS, [{"x": 0}], [], today=date(2026, 11, 4), top=2,
                         minutes=1.0, truncated=False)
        assert new["finalists"] == [{"x": 1}]

    @pytest.mark.parametrize("today,state_over,expected", [
        (date(2026, 11, 5), {}, "continue"),
        (date(2026, 11, 20), {"phase": tc.PHASE_DONE}, "idle"),
        (date(2026, 12, 1), {"phase": tc.PHASE_DONE}, "new"),
        (date(2026, 12, 6), {}, "new"),                     # 31 日を超えても終わらない周回
        (date(2026, 12, 5), {}, "continue"),                # ちょうど 31 日目までは続ける
    ])
    def test_decide(self, today, state_over, expected):
        assert tc.decide(_fresh(**state_over), today, tc.fingerprint(COMBOS))[0] == expected

    def test_decide_without_a_state_or_with_a_new_candidate_set(self):
        assert tc.decide(None, date(2026, 11, 4))[0] == "new"
        assert tc.decide(_fresh(), date(2026, 11, 5), "other-fp")[0] == "new"
        assert tc.decide(_fresh(), date(2026, 11, 5))[0] == "continue", "fp を渡さないなら照合しない"

    def test_confirm_fits_uses_the_worst_candidate(self):
        st = _fresh(finalists=[{"x": 1}, {"x": 2}], worst_min=4.0)
        assert tc.confirm_fits(st, 4.0 * 3 * 5, 5)
        assert not tc.confirm_fits(st, 4.0 * 3 * 5 - 1, 5)
        assert tc.confirm_fits(st, None, 5)
        assert tc.confirm_fits(_fresh(), 1.0, 5), "見積りの材料が無いなら入るとみなす"

    def test_finish_moves_the_clock_only_when_persisted(self):
        now = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
        done = tc.finish(_fresh(), {"persisted": True, "best_score": 0.2}, now=now)
        assert tc.last_finished_at(done) == now
        dry = tc.finish(_fresh(), {"persisted": False}, now=now)
        assert dry["last_finished_at"] is None

    def test_history_is_capped(self):
        st = _fresh(history=[{"cycle_id": str(i)} for i in range(tc.HISTORY_KEEP)])
        done = tc.finish(st, {"persisted": True}, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
        assert len(done["history"]) == tc.HISTORY_KEEP
        assert done["history"][-1]["cycle_id"] == "2026-11-04"

    def test_new_state_carries_history_and_the_clock(self):
        prev = _fresh(history=[{"cycle_id": "x"}], last_finished_at="2026-11-06T00:00:00+00:00")
        st = tc.new_state(model="m", today=date(2026, 12, 2), fp="f", n_total=6, prev=prev)
        assert st["history"] == [{"cycle_id": "x"}]
        assert st["last_finished_at"] == "2026-11-06T00:00:00+00:00"
        assert st["next_index"] == 0 and st["finalists"] == []

    @pytest.mark.parametrize("raw", [None, "", "{ not json", "[1, 2]", '{"version": 999}'])
    def test_unreadable_state_reads_as_none(self, raw):
        assert tc.load(raw) is None

    def test_state_round_trips_through_json(self):
        st = _fresh(finalists=[{"x": 1}])
        assert tc.load(tc.dump(st)) == st

    def test_the_state_key_fits_the_settings_column(self):
        assert len(tc.state_key("macro_risk_return")) <= 64


class TestSearchTakesACandidateList:
    """`plugins.tuning.search(candidates=...)`——周回が固定の候補集合の一部だけを測る口。"""

    def _search(self, db, candidates, champion=None):
        from plugins.tuning import SearchDim, search
        return asyncio.run(search(_CyclePlugin(), {}, [SearchDim("x", list(range(12)))], db,
                                  strategy="grid", candidates=candidates, champion_params=champion))

    def test_only_the_given_candidates_are_measured_once(self, db):
        out = self._search(db, [{"x": 5}, {"x": 3}, {"x": 5}])
        assert _CyclePlugin.calls == [5, 3]
        assert out["evaluated"] == [{"x": 5}, {"x": 3}]

    def test_the_champion_still_leads(self, db):
        out = self._search(db, [{"x": 5}], champion={"x": 7})
        assert out["evaluated"] == [{"x": 7}, {"x": 5}]

    def test_failed_candidates_are_in_evaluated_but_not_in_the_leaderboard(self, db):
        out = self._search(db, [{"x": 4}, {"x": 6}])
        assert out["evaluated"] == [{"x": 4}, {"x": 6}]
        assert [e["params"] for e in out["leaderboard"]] == [{"x": 6}]
