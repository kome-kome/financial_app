"""イベントスタディの計測 CLI（scripts/measure_index_event.py・#1031）のテスト。

CLI は共通の `db` fixture（in-memory SQLite）に合成の週次株価・通期行・data_j の断面を入れ、台帳だけを
差し替えて、読み込みから JSON の書き出しまでを通す（本物の DB・JPX の一覧には触れない）。合成の週次株価は
2021 年からなので、事前期間（2019-07-29〜2021-02-21）のうち 2021 年の 6 週だけが使え、placebo は下限の 15 個に
届かず「測定不能」になる。
"""
from __future__ import annotations

import contextlib
import json
import math
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
    @pytest.mark.parametrize("key", ["measure:index-event-a", "measure:index-event-m", "measure:index-event-b"])
    def test_frozen_value_matches_the_prereg_appendix(self, key):
        argv = rd.JOBS[key].argv
        doc = ROOT / argv[argv.index("--prereg") + 1]
        sha = argv[argv.index("--prereg-sha") + 1]
        text = doc.read_text(encoding="utf-8")
        # 凍結し直したら（本文と追記節の凍結値を変えたら）日中キューの引数も直す
        assert sha == prereg.recorded_sha(text) == prereg.body_sha256(text)

    @pytest.mark.parametrize("key,stage", [("measure:index-event-a", "stage-a"),
                                           ("measure:index-event-m", "m-family"),
                                           ("measure:index-event-b", "stage-b")])
    def test_runs_alone(self, key, stage):
        job = rd.JOBS[key]
        assert job.argv[1:5] == ("-m", "scripts.measure_index_event", stage, "--prereg")
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
        assert data["power"]["n_weeks"] == 6 and data["power"]["mde"] is not None
        assert data["placebo"]["fpr"] is None and data["placebo"]["n_usable"] == 1
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


# ── M 族（m-family・#1009）────────────────────────────────────────────────────────

OTHERS = [f"E8{i:03d}" for i in range(40)]


def _m_inputs(tmp_path, *, shift: float = 1.0, pairs_sha: str = FROZEN, runs=None):
    """M-2 raw / M-6 size_neutral の OOF の CSV と stage A のマッチ対を tmp に書く。処置の予測だけ上げる。"""
    from scripts.momentum_gate import SIZE_OOF_CSV_COLUMNS, write_oof_csv

    rng = np.random.default_rng(0)
    pairs = {t: [c for c in CONTROLS if c.startswith(t[:4])] for t in TREATED}
    firms = TREATED + CONTROLS + OTHERS
    cal = S.pressure_calendar(IE.STEPS_2022)
    yms = [f"{y}-{m:02d}" for y in range(2019, 2026) for m in range(1, 13)][:82]
    rows = {}
    for run in runs or ("raw|xgb_m2", "size_neutral|elasticnet"):
        out = []
        for ym in yms:
            e, _p = S.label_window_exposure(ym, cal)
            for ec in firms:
                y = float(rng.normal())
                out.append((ym, ec, y + float(rng.normal()) + (shift * e if ec in pairs else 0.0), y,
                            10.0, "size_d0"))
        rows[run] = out
    csv_path = tmp_path / "oof.csv"
    write_oof_csv(csv_path, rows, columns=SIZE_OOF_CSV_COLUMNS)
    pairs_path = tmp_path / "pairs.json"
    pairs_path.write_text(json.dumps({"prereg_sha256": pairs_sha, "generated_at": "2026-10-22T00:00:00+00:00",
                                      "data": {"snapshot_as_of": "2026-09-30"},
                                      "pairs": {"C1@2022-03-31": pairs}}), encoding="utf-8")
    return csv_path, pairs_path


@pytest.fixture
def m_wired(db, monkeypatch):
    import collector_utils
    import database
    import pandas as pd

    monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)
    monkeypatch.setattr(database, "SessionLocal", lambda: contextlib.nullcontext(db))
    monkeypatch.setattr(IE, "load_ledger", lambda db, root=None: _ledger())
    firms = TREATED + CONTROLS + OTHERS + [NO_FIN]
    variables = pd.DataFrame({"logcap": [10.0 + (i % 5) for i in range(len(firms))],
                              "log_turnover_ratio": [-3.0 + (i % 3) for i in range(len(firms))],
                              "log_pbr": 0.0, "ret_52w": 0.0, "sector17": "情報通信・サービスその他"},
                             index=firms)
    monkeypatch.setattr(X, "load_strata_variables", lambda db: variables)
    return db


def _run_m(tmp_path, csv_path, pairs_path, sha=FROZEN):
    out = tmp_path / "m.json"
    code = X.main(["m-family", "--prereg", str(PREREG), "--prereg-sha", sha, "--oof-csv", str(csv_path),
                   "--pairs", str(pairs_path), "--out", str(out)])
    return code, out


class TestMFamily:
    def test_end_to_end(self, m_wired, tmp_path, capsys):
        csv_path, pairs_path = _m_inputs(tmp_path, shift=1.5)
        code, out = _run_m(tmp_path, csv_path, pairs_path)
        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["stage"] == "M" and data["prereg_sha256"] == FROZEN
        assert set(data["verdict"]) == {"xgb_m2", "elasticnet"}
        assert data["verdict"]["xgb_m2"]["b"]["coef"] > 0
        assert data["inputs"]["n_treated_itt"] == 5 and data["inputs"]["n_controls_used"] == 12
        # 擬似群は処置（ITT）と使った対照以外から引く
        assert data["inputs"]["n_eligible"] == len(OTHERS)
        assert data["models"]["xgb_m2"]["permutation"]["n"] == S.M_PERMUTATIONS
        assert not re.search(r"E[89]\d{3}", out.read_text(encoding="utf-8"))
        assert "[m-family]" in capsys.readouterr().out

    def test_pairs_from_another_freeze_stop_with_2(self, m_wired, tmp_path):
        csv_path, pairs_path = _m_inputs(tmp_path, pairs_sha="0" * 64)
        code, out = _run_m(tmp_path, csv_path, pairs_path)
        assert code == X.EXIT_FAILED and not out.exists()

    def test_missing_inputs_stop_with_2(self, m_wired, tmp_path):
        csv_path, pairs_path = _m_inputs(tmp_path, runs=("raw|xgb_m2",))     # M-6 の行が無い
        assert _run_m(tmp_path, csv_path, pairs_path)[0] == X.EXIT_FAILED
        assert _run_m(tmp_path, tmp_path / "none.csv", pairs_path)[0] == X.EXIT_FAILED
        assert _run_m(tmp_path, csv_path, tmp_path / "none.json")[0] == X.EXIT_FAILED

    def test_prereg_mismatch_stops_with_2(self, m_wired, tmp_path):
        csv_path, pairs_path = _m_inputs(tmp_path)
        with pytest.raises(SystemExit) as e:
            _run_m(tmp_path, csv_path, pairs_path, sha="0" * 64)
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_reads_only_the_production_runs(self, tmp_path):
        from scripts.momentum_gate import SIZE_OOF_CSV_COLUMNS, write_oof_csv

        path = tmp_path / "x.csv"
        write_oof_csv(path, {"raw|xgb_m2": [("2024-01", "E1", 0.1, 0.2, 1.0, "size_d0")],
                             "size_neutral|xgb_m2": [("2024-01", "E1", 9.0, 0.2, 1.0, "size_d0")],
                             "raw|elasticnet": [("2024-01", "E1", 8.0, 0.2, 1.0, "size_d0")],
                             "size_neutral|elasticnet": [("2024-01", "E1", 0.3, 0.2, 1.0, "size_d0")]},
                      columns=SIZE_OOF_CSV_COLUMNS)
        assert X.read_oof_csv(path, S.M_MODELS) == {"xgb_m2": {("2024-01", "E1"): (0.1, 0.2)},
                                                    "elasticnet": {("2024-01", "E1"): (0.3, 0.2)}}


# ── stage B（開封・#1032）──────────────────────────────────────────────────────────

def _recorded_prereg(tmp_path) -> Path:
    """本物の文書の追記節「stage A（盲検）の結果」だけ書き足した写し（本文は同じ＝凍結値も同じ）。"""
    text = PREREG.read_text(encoding="utf-8")
    head, rest = text.split(X.STAGE_A_HEADING, 1)
    path = tmp_path / "prereg.md"
    path.write_bytes((head + X.STAGE_A_HEADING + "\n\nバランス: 全変数 |SMD| < 0.1（記録の例）\n").encode("utf-8"))
    assert prereg.body_sha256(path.read_text(encoding="utf-8")) == FROZEN
    return path


def _pairs_file(tmp_path, sha: str = FROZEN) -> Path:
    path = tmp_path / "pairs.json"
    pairs = {t: [c for c in CONTROLS if c.startswith(t[:4])] for t in TREATED}
    path.write_text(json.dumps({"prereg_sha256": sha, "pairs": {"C1@2022-03-31": pairs}}), encoding="utf-8")
    return path


def _run_b(tmp_path, doc, pairs, sha=FROZEN):
    out = tmp_path / "b.json"
    code = X.main(["stage-b", "--prereg", str(doc), "--prereg-sha", sha, "--pairs", str(pairs), "--out", str(out)])
    return code, out


class TestStageBCli:
    def test_refuses_to_open_before_stage_a_is_recorded(self, wired, tmp_path, capsys):
        # 本物の文書は追記節が「未記入」のまま＝開封しない
        code, out = _run_b(tmp_path, PREREG, _pairs_file(tmp_path))
        assert code == X.EXIT_FAILED and not out.exists()
        assert "未記入" in capsys.readouterr().err

    def test_end_to_end_on_sqlite(self, wired, tmp_path, monkeypatch):
        _seed(wired)
        monkeypatch.setattr(X, "load_gaps", lambda db, prices, yms: {})     # 業種別回帰は記述だけ
        code, out = _run_b(tmp_path, _recorded_prereg(tmp_path), _pairs_file(tmp_path))
        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["stage"] == "B" and data["prereg_sha256"] == FROZEN
        assert data["quantities"]["I"] is not None
        assert data["quantities"]["car_post"] is None              # 合成の週次は 2025-02 まで
        assert data["e_family"]["tests"] == ["E1", "E2", "E4"]
        assert data["inputs"]["n_treated_matched"] == 4
        assert not re.search(r"E9\d{3}", out.read_text(encoding="utf-8"))

    def test_pairs_from_another_freeze_stop_with_2(self, wired, tmp_path):
        code, out = _run_b(tmp_path, _recorded_prereg(tmp_path), _pairs_file(tmp_path, sha="0" * 64))
        assert code == X.EXIT_FAILED and not out.exists()


def test_month_end_labels_follow_the_production_definition():
    from collections import namedtuple

    from plugins.macro_snapshots import HORIZON_WEEKS

    Bar = namedtuple("Bar", "trade_date close_last")
    days = [date(2022, 1, 7) + W * k for k in range(70)]
    rows = [Bar(d.isoformat(), 100.0 * (1.01 ** k)) for k, d in enumerate(days)]
    got = X.month_end_labels({"E1": rows}, ["E1"], {"2022-01"})
    # 2022-01 の月末の足は 1/28（4 本目）。その 52 本先の足との log 比
    assert got == {("2022-01", "E1"): pytest.approx(HORIZON_WEEKS * math.log(1.01))}


def test_load_strata_variables_on_sqlite(db):
    """層の変数は stage A と同じ読み込みとマッチ日の計算を全社へ当てたもの（通期行の無い社は欠ける）。"""
    _seed(db)
    v = X.load_strata_variables(db)
    assert set(TREATED + CONTROLS) <= set(v.index) and NO_FIN not in v.index
    assert v.loc[TREATED, ["logcap", "log_turnover_ratio"]].notna().all().all()
    assert set(v["sector17"]) == {"情報通信・サービスその他"}
    assert len(S.strata(v)) == len(TREATED + CONTROLS)


def test_jsonable_drops_nan():
    assert X.jsonable({"a": float("nan"), "b": [1.0, float("inf")], "c": "x"}) == \
        {"a": None, "b": [1.0, None], "c": "x"}
