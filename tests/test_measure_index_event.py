"""イベントスタディの計測 CLI（scripts/measure_index_event.py・#1031）のテスト。

CLI は共通の `db` fixture（in-memory SQLite）に合成の週次株価・通期行・data_j の断面を入れ、台帳だけを
差し替えて、読み込みから JSON の書き出しまでを通す（本物の DB・JPX の一覧には触れない）。週次株価は
2021 年からなので、本物と同じく事前期間（2014〜2018）が無く、MDE と placebo は「測定不能」になる。
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_event_study as S  # noqa: E402
import index_events as IE  # noqa: E402
from scripts import measure_index_event as X  # noqa: E402
from scripts import prereg  # noqa: E402
from scripts import run_daytime as rd  # noqa: E402

PREREG = ROOT / "docs" / "prereg" / "index-event-2026.md"
FROZEN = prereg.recorded_sha(PREREG.read_text(encoding="utf-8"))
W = timedelta(days=7)

TREATED = ["E9000", "E9100", "E9200", "E9300"]
CONTROLS = [f"E9{i}0{j}" for i in range(4) for j in range(1, 4)]     # 処置ごとに値の近い 3 社
NO_FIN = "E9400"                                                     # 処置だが通期行が無い


@pytest.fixture(autouse=True)
def _fixed_head_sha(monkeypatch):
    monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)


class TestDaytimeJob:
    def test_frozen_value_matches_the_prereg_appendix(self):
        argv = rd.JOBS["measure:index-event-a"].argv
        doc = ROOT / argv[argv.index("--prereg") + 1]
        sha = argv[argv.index("--prereg-sha") + 1]
        text = doc.read_text(encoding="utf-8")
        # 凍結し直したら（本文と追記節の凍結値を変えたら）日中キューの引数も直す
        assert sha == prereg.recorded_sha(text) == prereg.body_sha256(text)

    def test_runs_stage_a_alone(self):
        job = rd.JOBS["measure:index-event-a"]
        assert job.argv[1:5] == ("-m", "scripts.measure_index_event", "stage-a", "--prereg")
        assert job.parallel_sensitive is True


def _ledger(treated=TREATED + [NO_FIN]):
    return IE.IndexLedger(
        ok=True,
        mentions={ec: (("topix-2022-10", "transition"),) for ec in treated},
        groups={("topix-2022-10", "transition"): frozenset(treated),
                ("topix-2023-10-review", "released"): frozenset(treated[:1]),
                ("topix-2023-10-review", "transition"): frozenset(treated[1:])},
        stats={"resolution": {"topix-2022-10": {"rate": 0.998}}})


def _seed(db, *, spike: float = 4.0, seed: int = 0) -> None:
    """2021-01〜2025-02 の週次・通期行（FY2021・FY2022）・data_j の断面（2026-09-30）を入れる。"""
    from database import Company, FinancialRecord, JpxListingSnapshot, StockPriceWeekly

    rng = np.random.default_rng(seed)
    weeks = [date(2021, 1, 4) + W * k for k in range(((date(2025, 2, 24) - date(2021, 1, 4)).days // 7) + 1)]
    pressure = {pw.week for pw in S.pressure_calendar(IE.STEPS_2022)}
    seen = datetime(2026, 10, 1)
    for i, t in enumerate(TREATED + [NO_FIN]):
        path = 500 * (1 + i) * np.exp(np.cumsum(rng.normal(0, 0.02, size=len(weeks))))
        group = [t] + ([c for c in CONTROLS if c.startswith(t[:4])] if t != NO_FIN else [])
        for j, ec in enumerate(group):
            db.add(Company(edinet_code=ec, sec_code=f"{90 + i}{j}0", name=ec, industry="情報・通信業"))
            p = path * np.exp(np.cumsum(rng.normal(0, 0.001, size=len(weeks))))
            for w, close in zip(weeks, p.tolist()):
                to = 1e8 * (1 + i) * float(np.exp(rng.normal(0, 0.05)))
                if ec == t and w in pressure:
                    to *= spike
                db.add(StockPriceWeekly(edinet_code=ec, week_start=w.isoformat(),
                                        trade_date=(w + timedelta(days=4)).isoformat(), close_last=close,
                                        volume_sum=to / close, turnover_sum=to, n_days=5))
            if ec != NO_FIN:
                for year in (2021, 2022):
                    db.add(FinancialRecord(
                        edinet_code=ec, sec_code=f"{90 + i}{j}0", company_name=ec, industry="情報・通信業",
                        year=year, period_end=date(year, 3, 31), period_type="annual",
                        stock_price=1.0, market_cap=1.0, issued_shares=1e6 * (1 + i) * (1 + 0.0005 * j),
                        bs_total_equity=1e8, bs_bps=200.0 * (1 + i)))
            db.add(JpxListingSnapshot(
                source="data_j", as_of_date=date(2026, 9, 30), sec_code=f"{90 + i}{j}0", edinet_code=ec,
                market_segment="スタンダード（内国株式）", industry33="情報・通信業", size_code="-",
                size_class="-", first_seen_at=seen, last_seen_at=seen))
    db.commit()


@pytest.fixture
def wired(db, monkeypatch):
    import collector_utils
    import database

    # 標準出力の差し替えは pytest のキャプチャを壊す（force_utf8_stdout の docstring）
    monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)
    monkeypatch.setattr(database, "SessionLocal", lambda: contextlib.nullcontext(db))
    monkeypatch.setattr(IE, "load_ledger", lambda db, root=None: _ledger())
    return db


def _run(tmp_path, sha=FROZEN):
    out, pairs = tmp_path / "a.json", tmp_path / "pairs.json"
    code = X.main(["stage-a", "--prereg", str(PREREG), "--prereg-sha", sha,
                   "--out", str(out), "--pairs-out", str(pairs)])
    return code, out, pairs


class TestStageA:
    def test_end_to_end_on_sqlite(self, wired, tmp_path, capsys):
        _seed(wired)
        code, out, pairs_path = _run(tmp_path)
        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["stage"] == "A" and data["prereg_sha256"] == FROZEN
        assert data["prereg_issue"].endswith("/issues/1007")
        assert data["data"]["snapshot_as_of"] == "2026-09-30"
        main = data["matching"]["C1@2022-03-31"]
        assert main["n_treated"] == 5 and main["n_matched"] == 4
        assert main["unmatched"]["by_reason"] == {"no_data": 1}
        assert main["balance_ok"] is True
        assert data["first_stage"]["pass"] is True
        assert data["power"]["mde"] is None and data["power"]["n_weeks"] == 0
        assert data["placebo"]["fpr"] is None and data["placebo"]["n_usable"] == 0
        assert data["r0"]["status"] == "pending"
        # 要約には銘柄コードを入れない。マッチ対は別のファイルに書く
        assert not re.search(r"E9\d{3}", out.read_text(encoding="utf-8"))
        pairs = json.loads(pairs_path.read_text(encoding="utf-8"))["pairs"]["C1@2022-03-31"]
        assert set(pairs) == set(TREATED)
        for t, cs in pairs.items():
            assert cs and all(c.startswith(t[:4]) for c in cs)
        assert "R0: pending" in capsys.readouterr().out

    def test_first_stage_failure_exits_3_after_writing(self, wired, tmp_path):
        _seed(wired, spike=1.0)
        code, out, _ = _run(tmp_path)
        assert code == X.EXIT_R0_FAILED
        assert json.loads(out.read_text(encoding="utf-8"))["r0"]["status"] == "fail"

    def test_prereg_mismatch_stops_with_2(self, wired, tmp_path):
        with pytest.raises(SystemExit) as e:
            _run(tmp_path, sha="0" * 64)
        assert e.value.code == prereg.EXIT_MISMATCH
        assert not (tmp_path / "a.json").exists()

    def test_unusable_ledger_stops_with_2(self, wired, tmp_path, monkeypatch):
        monkeypatch.setattr(IE, "load_ledger", lambda db, root=None: IE.IndexLedger(ok=False, reason="x"))
        code, out, _ = _run(tmp_path)
        assert code == X.EXIT_FAILED and not out.exists()

    def test_missing_snapshot_stops_with_2(self, wired, tmp_path):
        code, out, _ = _run(tmp_path)        # 断面を入れていない
        assert code == X.EXIT_FAILED and not out.exists()


def test_load_turnover_reads_only_the_window(db, make_weekly):
    db.add_all([make_weekly(edinet_code="E1", trade_date="2022-01-07", turnover_sum=5.0),
                make_weekly(edinet_code="E1", trade_date="2022-01-14", turnover_sum=None),
                make_weekly(edinet_code="E1", trade_date="2023-01-06", turnover_sum=7.0),
                make_weekly(edinet_code="E2", trade_date="2022-01-07", turnover_sum=9.0)])
    db.commit()
    got = X.load_turnover(db, {"E1"}, date(2022, 1, 1), date(2022, 12, 31))
    assert got == {"E1": {date(2022, 1, 3): 5.0}}


def test_jsonable_drops_nan():
    assert X.jsonable({"a": float("nan"), "b": [1.0, float("inf")], "c": "x"}) == \
        {"a": None, "b": [1.0, None], "c": "x"}
