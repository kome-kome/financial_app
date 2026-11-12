"""本番予測の月次の写しを写した後の答えで採点する計測（#1157）: `scripts.measure_live_oos`。

本物の DB と週次株価は使わず、合成の写しと週次足で確かめる。issue の「検証」の 3 項目に対応する:
答えが出ていない写し（写した日 + ホライズン > 今日）は採点に入らない / 写しの源が欠けた月はその源だけ外して
数える（他の源は数える）/ 事前登録の sha が不一致なら非 0 で止まる。あわせて、起点が写した日以前の最後の足に
なること、判定が最初の 36 本だけで出ること、DB へ書かないことを確かめる。
"""
import contextlib
import json
import math
import random
from datetime import date, timedelta

import pytest

import database
import plugins.macro_snapshots as ms
import scripts.measure_live_oos as X
import scripts.run_daytime as rd
from database import PredictionSnapshot
from scripts import prereg

SHA = prereg.body_sha256(prereg.read_document(X.PREREG))
FIRST_BAR = "2026-01-02"            # 金曜。以後 7 日おき
N_BARS = 120                        # 最後の足は 2028-04-14
N_CO = 60
SD1, SD2 = "2026-10-21", "2026-11-02"
DELISTED = "E00000"                 # 2026-12-25 で足が途切れる社（最も成長率が低い）


def _bar_dates(n: int = N_BARS) -> list[str]:
    d0 = date.fromisoformat(FIRST_BAR)
    return [(d0 + timedelta(weeks=k)).isoformat() for k in range(n)]


def _growth(k: int) -> float:
    return (k - N_CO // 2) * 0.002


def _code(k: int) -> str:
    return f"E{k:05d}"


def _prices() -> dict:
    """社 k の終値 = 100·exp(g_k·i + 0.05·sin(i/3))。sin は全社共通の相場の波で、起点がずれると答えも変わる。"""
    dates = _bar_dates()
    out = {}
    for k in range(N_CO):
        rows = [ms._WEEKLY_PX(t, 100.0 * math.exp(_growth(k) * i + 0.05 * math.sin(i / 3)), None)
                for i, t in enumerate(dates)]
        if _code(k) == DELISTED:
            rows = rows[:52]                 # 最後の足 2026-12-25
        out[_code(k)] = rows
    return out


def _rows(sds=(SD1, SD2), m2_codes=None, m1_empty=(), asof_m6="2026-10-16", asof_m2="2026-09-25") -> list:
    """M-6 は成長率どおり（完全な予測）、M-2 は逆向き。M-1 は `m1_empty` の写しで μ̂ が全部空。"""
    out = []
    for sd in sds:
        for k in range(N_CO):
            ec, g = _code(k), _growth(k)
            out.append((sd, "macro_enet", ec, g, None, asof_m6))
            if m2_codes is None or ec in m2_codes:
                out.append((sd, "macro_gbdt", ec, -g, None, asof_m2))
            out.append((sd, "macro_risk_return", ec, None if sd in m1_empty else g * 0.5, None, asof_m6))
            out.append((sd, "sector_ols", ec, None, g * 100.0, "2026-03-31"))
    return out


# ── 答え ────────────────────────────────────────────────────────────────────────

class TestAnswer:
    def _px(self, n=N_BARS):
        dates = _bar_dates(n)
        return dates, [100.0 * (1.01 ** i) for i in range(n)]

    def test_start_is_the_last_bar_on_or_before_the_snapshot_date(self):
        dates, closes = self._px()
        i = dates.index("2026-10-16")        # 写した日 2026-10-21（水）の直前の金曜
        kind, val = X.answer((dates, closes), SD1, 52, dates[-1])
        assert kind == X.OK
        assert val == pytest.approx(math.log(closes[i + 52] / closes[i]))

    def test_13_weeks_counts_13_bars(self):
        dates, closes = self._px()
        i = dates.index("2026-10-16")
        _kind, val = X.answer((dates, closes), SD1, 13, dates[-1])
        assert val == pytest.approx(math.log(closes[i + 13] / closes[i]))

    def test_no_start_when_the_last_bar_is_long_before_the_snapshot(self):
        dates, closes = self._px(30)          # 2026-07-24 で途切れる
        assert X.answer((dates, closes), SD1, 52, "2028-04-14") == (X.NO_START, None)

    def test_no_start_without_prices(self):
        assert X.answer(None, SD1, 52, "2028-04-14") == (X.NO_START, None)

    def test_delisted_is_left_out_unless_included(self):
        dates, closes = self._px(52)          # 2026-12-25 で途切れる
        i = dates.index("2026-10-16")
        assert X.answer((dates, closes), SD1, 52, "2028-04-14") == (X.ENDED, None)
        kind, val = X.answer((dates, closes), SD1, 52, "2028-04-14", include_ended=True)
        assert kind == X.ENDED
        assert val == pytest.approx(math.log(closes[-1] / closes[i]))

    def test_recent_when_the_horizon_is_near_the_latest_bar(self):
        dates, closes = self._px(52)
        # 起点の足 2026-10-16 + 52 週 = 2027-10-15。全社の最新の足から 28 日以内なら収集の遅れとみなす
        assert X.answer((dates, closes), SD1, 52, "2027-11-01")[0] == X.RECENT


# ── 成熟（検証 1）───────────────────────────────────────────────────────────────

class TestMaturity:
    def test_unmatured_snapshots_are_not_scored(self):
        rep = X.run(_rows(), _prices(), "2027-10-25")       # SD1 + 52 週 = 2027-10-20、SD2 は 2027-11-01
        main = rep["variants"][X.MAIN]
        assert main["matured_snapshots"] == [SD1]
        assert set(main["pair"]["top_k_excess_by_snapshot"][X.M6]) == {SD1}
        assert rep["variants"]["check_13w"]["matured_snapshots"] == [SD1, SD2]

    def test_nothing_is_scored_before_the_first_answer(self):
        rep = X.run(_rows(), _prices(), "2027-10-19")
        assert rep["variants"][X.MAIN]["matured_snapshots"] == []
        assert rep["variants"][X.MAIN]["by_source"][X.M6] is None
        assert rep["prereg_decision"]["decision"] == "accumulating"

    def test_maturity_is_bounded_by_the_latest_bar_too(self):
        prices = {ec: rows[:93] for ec, rows in _prices().items()}    # 最後の足 2027-10-08
        rep = X.run(_rows(), prices, "2028-01-01")
        assert rep["horizon_end"] == "2027-10-08"
        assert rep["variants"][X.MAIN]["matured_snapshots"] == []


# ── 欠けた源（検証 2）───────────────────────────────────────────────────────────

class TestMissingSource:
    def test_only_the_missing_source_is_left_out(self):
        rep = X.run(_rows(m1_empty=(SD1,)), _prices(), "2028-01-01")
        assert rep["missing_source_snapshots"] == [["macro_risk_return", SD1]]
        by_source = rep["variants"][X.MAIN]["by_source"]
        assert by_source["macro_risk_return"]["n_periods"] == 1
        assert by_source[X.M6]["n_periods"] == 2
        assert by_source["sector_ols"]["n_periods"] == 2


# ── 答えと母集団 ────────────────────────────────────────────────────────────────

class TestAnswersAndDomain:
    def test_every_source_gets_the_same_answer_when_the_start_is_the_snapshot_date(self):
        preds, _ = X.collect_predictions(_rows())
        px = X.index_prices(_prices())
        y, _ = X.answers_for(preds, px, [SD1], 52, X.START_SNAPSHOT, False, "2028-04-14")
        shared = y[X.M6].keys() & y[X.M2].keys()
        assert shared and all(y[X.M6][k] == y[X.M2][k] for k in shared)
        y_asof, _ = X.answers_for(preds, px, [SD1], 52, X.START_ASOF, False, "2028-04-14")
        assert any(y_asof[X.M6][k] != y_asof[X.M2][k] for k in shared)    # data_asof が違えば答えも違う
        assert "sector_ols" not in y_asof                                   # gap の data_asof は決算期末

    def test_rows_without_data_asof_have_no_start_in_the_asof_variant(self):
        """M-1 の写しは as-of を持たない（2026-11 の実測で全行 NULL）。起点が写した日なら答えは作れる。"""
        rows = [(sd, s, ec, mu, gap, None if s == "macro_risk_return" else asof)
                for sd, s, ec, mu, gap, asof in _rows()]
        rep = X.run(rows, _prices(), "2028-01-01")
        asof_answers = rep["variants"]["data_asof"]["answers"]["macro_risk_return"]
        assert asof_answers == {X.NO_START: 2 * N_CO}
        assert rep["variants"]["data_asof"]["by_source"]["macro_risk_return"] is None
        assert rep["variants"][X.MAIN]["by_source"]["macro_risk_return"]["n_periods"] == 2

    def test_pair_is_scored_on_the_common_domain(self):
        m2_codes = {_code(k) for k in range(10, N_CO)}
        rep = X.run(_rows(m2_codes=m2_codes), _prices(), "2028-01-01")
        pair = rep["variants"][X.MAIN]["pair"]
        assert pair["n_rows_by_snapshot"] == {SD1: N_CO - 10, SD2: N_CO - 10}
        assert pair["metrics"][X.M6]["top_k_excess_return"] > 0
        assert pair["metrics"][X.M2]["top_k_excess_return"] < 0

    def test_delisted_company_is_counted_only_in_the_included_variant(self):
        rep = X.run(_rows(), _prices(), "2028-01-01")
        assert rep["variants"][X.MAIN]["answers"][X.M6][X.ENDED] == 2
        main = rep["variants"][X.MAIN]["pair"]["n_rows_by_snapshot"]
        incl = rep["variants"]["delisted_included"]["pair"]["n_rows_by_snapshot"]
        assert main == {SD1: N_CO - 1, SD2: N_CO - 1}
        assert incl == {SD1: N_CO, SD2: N_CO}

    def test_sell_reference_uses_the_previous_snapshot_layer(self):
        rep = X.run(_rows(), _prices(), "2028-01-01")
        sell = rep["variants"][X.MAIN]["sell"]
        m6 = sell["by_source"][X.M6]
        assert m6["n_periods"] == 1 and set(m6["by_period"]) == {SD2}
        assert m6["mean"] > 0                         # 完全な予測で売れば層の平均より低い社を売る
        assert sell["pair"][X.M2]["mean"] < 0          # 逆向きの予測は層の上側を売る
        assert "sector_ols" not in sell["by_source"]
        assert rep["variants"]["data_asof"]["sell"] is None


# ── 判定 ────────────────────────────────────────────────────────────────────────

def _months(n: int) -> list[str]:
    return [f"{2026 + (9 + k) // 12}-{(9 + k) % 12 + 1:02d}-01" for k in range(n)]


def _series(level: float, n: int, seed: int, wiggle: float = 0.02) -> dict:
    rng = random.Random(seed)
    return {p: level + rng.uniform(-wiggle, wiggle) for p in _months(n)}


class TestDecide:
    def test_accumulating_until_36_common_snapshots(self):
        d = X.decide(_series(0.10, 35, 1), _series(0.0, 35, 2))
        assert d["decision"] == "accumulating" and d["sig"] is None and d["judged_on"] == []
        assert d["n_common"] == 35

    def test_m6_better_and_m2_better(self):
        assert X.decide(_series(0.10, 36, 1), _series(0.0, 36, 2))["decision"] == "m6_better"
        assert X.decide(_series(0.0, 36, 1), _series(0.10, 36, 2))["decision"] == "m2_better"

    def test_no_difference(self):
        assert X.decide(_series(0.0, 36, 1, 0.2), _series(0.0, 36, 2, 0.2))["decision"] == "no_difference"

    def test_only_the_first_36_snapshots_are_judged(self):
        a, b = _series(0.10, 48, 1), _series(0.0, 48, 2)
        for p in _months(48)[36:]:                     # 37 本目以降を大きく逆転させても判定は動かない
            a[p], b[p] = -5.0, 5.0
        d48 = X.decide(a, b)
        d36 = X.decide({p: a[p] for p in _months(36)}, {p: b[p] for p in _months(36)})
        assert d48["judged_on"] == _months(36)
        assert d48["decision"] == d36["decision"] == "m6_better"
        assert d48["sig"]["mean"] == d36["sig"]["mean"]

    def test_uses_the_overlap_block_length(self):
        d = X.decide(_series(0.10, 36, 1), _series(0.0, 36, 2))
        assert d["avg_block"] == 12 and d["alpha"] == 0.05 and d["n_judge"] == 36


# ── 入口（検証 3・DB へ書かない）─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import collector_utils
    monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)


def _boom(*_a, **_k):
    raise AssertionError("DB に触った")


class TestMain:
    def _seed(self, db):
        for sd, source, ec, mu, gap, asof in _rows():
            db.add(PredictionSnapshot(snapshot_date=sd, source=source, edinet_code=ec, mu=mu, gap_ratio=gap,
                                      data_asof=asof, code_version="x", preprocess_version="v"))
        db.commit()

    def test_sha_mismatch_exits_2_before_touching_the_db(self, monkeypatch):
        monkeypatch.setattr(database, "SessionLocal", _boom)
        with pytest.raises(SystemExit) as e:
            X.main(["--prereg", X.PREREG, "--prereg-sha", "f" * 64])
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_other_document_exits_2(self, monkeypatch):
        monkeypatch.setattr(database, "SessionLocal", _boom)
        other = "docs/prereg/label-audit.md"
        sha = prereg.body_sha256(prereg.read_document(other))
        assert X.main(["--prereg", other, "--prereg-sha", sha]) == prereg.EXIT_MISMATCH

    def test_reads_the_table_and_writes_nothing(self, db, monkeypatch, tmp_path):
        self._seed(db)
        before = db.query(PredictionSnapshot).count()
        monkeypatch.setattr(database, "SessionLocal", lambda: contextlib.nullcontext(db))
        monkeypatch.setattr(ms, "load_weekly_prices_chunked", lambda _db, **_k: _prices())
        out = tmp_path / "live_oos.json"
        code = X.main(["--prereg", X.PREREG, "--prereg-sha", SHA, "--today", "2028-01-01", "--out", str(out)])
        assert code == 0
        assert db.query(PredictionSnapshot).count() == before
        assert not db.new and not db.dirty and not db.deleted
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["prereg_sha256"] == SHA
        assert payload["variants"][X.MAIN]["matured_snapshots"] == [SD1, SD2]
        assert payload["prereg_decision"]["decision"] == "accumulating"

    def test_empty_snapshots_exit_3(self, db, monkeypatch, tmp_path):
        monkeypatch.setattr(database, "SessionLocal", lambda: contextlib.nullcontext(db))
        monkeypatch.setattr(ms, "load_weekly_prices_chunked", lambda _db, **_k: _prices())
        code = X.main(["--prereg", X.PREREG, "--prereg-sha", SHA, "--out", str(tmp_path / "o.json")])
        assert code == X.EXIT_INPUT


class TestPrereg:
    def test_document_records_its_frozen_value(self):
        assert prereg.recorded_sha(prereg.read_document(X.PREREG)) == SHA

    def test_issue_is_1157(self):
        assert prereg.issue_url(prereg.read_document(X.PREREG)).endswith("/issues/1157")


class TestDaytimeJob:
    def test_job_passes_the_frozen_document(self):
        job = rd.JOBS["measure:live-oos"]
        argv = list(job.argv)
        assert job.name == "live_oos" and job.parallel_sensitive is True
        assert argv[argv.index("--prereg") + 1] == X.PREREG
        assert argv[argv.index("--prereg-sha") + 1] == SHA
        assert job.measured_min * rd.JOB_HEADROOM <= rd.JOB_BUDGET_MIN

    def test_job_argv_is_accepted_by_the_script(self):
        argv = rd.JOBS["measure:live-oos"].argv
        assert argv[1:3] == ("-m", "scripts.measure_live_oos")
        args = X.build_parser().parse_args(list(argv[3:]))
        assert args.prereg == X.PREREG and args.today is None
