"""採点の物差しの点検（#1154）: `build_snapshots(return_no_future=True)` と `scripts.measure_label_audit`。

DB と M-4 は回さず、合成の株価・財務で確かめる。issue の「検証」の 4 項目に対応する:
52 週先の株価が無い社が理由ごとに数えられる / 配当 0 の社では配当込みの答えが値上がりだけの答えと一致する /
予測はビット一致のまま答えだけを差し替えて採点できる / 本番の学習・答え（既定の build_snapshots）は変わらない。
"""
import math
from collections import namedtuple
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

import plugins.macro_snapshots as ms
import scripts.measure_label_audit as X
import scripts.run_daytime as rd
from scripts import prereg

SHA = prereg.body_sha256(prereg.read_document(X.PREREG))

Fin = namedtuple("Fin", "period_end industry sec_code company_name bs_total_assets per")
START = date(2020, 1, 6)   # 月曜


def _weeks(n: int, start: date = START, skip: set | None = None) -> list[str]:
    """週次の日付列（start から 1 週おき・skip の週番号を抜く）。"""
    skip = skip or set()
    return [(start + timedelta(weeks=i)).isoformat() for i in range(n) if i not in skip]


def _series(dates: list[str], closes: list[float]) -> list:
    return [ms._WEEKLY_PX(d, float(c), None) for d, c in zip(dates, closes)]


def _rising(n: int, base: float = 100.0, step: float = 0.5) -> list[float]:
    return [base + step * i for i in range(n)]


def _fin(per: float = 10.0) -> list:
    return [Fin(date(2019, 12, 31), "製造業", "1000", "合成", 1e9, per)]


def _build(prices: dict, **kw):
    fin = {ec: _fin() for ec in prices}
    return ms._build_snapshots_impl(
        prices, fin, {}, {}, ["per"], [], False, 12, 0.0,
        build_interactions=False, macro_nan_ok=False, return_stock_ids=True, price_features=[], **kw)


def _panel():
    """A: 全期間（150 週）続く。B: 90 週で終わる（上場廃止）。"""
    return {
        "E_A": _series(_weeks(150), _rising(150)),
        "E_B": _series(_weeks(90), _rising(90, base=50.0)),
    }


# ── build_snapshots の return_no_future ─────────────────────────────────────────

class TestReturnNoFuture:
    def test_default_shape_and_rows_are_unchanged(self):
        """既定（無効）の戻り値の形と中身は従来どおり。有効にしても学習用の行はビット一致する。"""
        prices = _panel()
        base = _build(prices)
        with_nf = _build(prices, return_no_future=True)
        assert len(base) == 5 and len(with_nf) == 6
        assert with_nf[:5] == base
        four = ms._build_snapshots_impl(prices, {ec: _fin() for ec in prices}, {}, {}, ["per"], [], False, 12,
                                        0.0, False, False, False, [])
        assert len(four) == 4 and four[0] == base[0]

    def test_rows_without_a_52_week_future_are_listed(self):
        prices = _panel()
        _s, _m, _c, _f, ids, no_future = _build(prices, return_no_future=True)
        dates_b = [r.trade_date for r in prices["E_B"]]
        want_b = {dates_b[i][:7] for i in ms.month_end_indices(dates_b)
                  if i >= 4 and i + ms.HORIZON_WEEKS >= len(dates_b) and dates_b[i] >= "2020-02-14"}
        got_b = {ym for ym, ecs in no_future.items() if "E_B" in ecs}
        assert got_b == want_b and want_b
        # 答えのある行と答えの無い行は重ならない（同じ (ym, 社) が両方に入らない）
        for ym, ecs in no_future.items():
            assert not set(ecs) & set(ids.get(ym, []))

    def test_rows_failing_feature_checks_are_not_listed(self):
        """特徴量の条件で落ちた行（ここでは財務が空欄）は数えない＝条件を書き写さない。"""
        prices = {"E_A": _series(_weeks(80), _rising(80))}
        fin = {"E_A": [Fin(date(2019, 12, 31), "製造業", "1000", "合成", 1e9, None)]}
        out = ms._build_snapshots_impl(prices, fin, {}, {}, ["per"], [], False, 12, 0.0,
                                       build_interactions=False, macro_nan_ok=False, return_stock_ids=False,
                                       price_features=[], return_no_future=True)
        assert out[0] == {} and out[-1] == {}

    def test_cache_key_distinguishes_the_flag(self, monkeypatch):
        mock_impl = MagicMock(return_value=({}, {}, {}, []))
        monkeypatch.setattr(ms, "_build_snapshots_impl", mock_impl)
        prices, fin, companies, macro = {}, {}, {}, {}
        with ms.shared_snapshot_cache():
            ms.build_snapshots(prices, fin, companies, macro, ["per"], [], False, 12, 1.0,
                               build_interactions=False)
            ms.build_snapshots(prices, fin, companies, macro, ["per"], [], False, 12, 1.0,
                               build_interactions=False, return_no_future=True)
        assert mock_impl.call_count == 2
        assert mock_impl.call_args.kwargs["return_no_future"] is True


# ── 答えの作り直しと配当込みの答え ─────────────────────────────────────────────

def _labelled_y(prices: dict) -> dict:
    s, _m, _c, _f, ids = _build(prices)
    return {(ym, ec): yt for ym in s for (_x, yt), ec in zip(s[ym], ids[ym])}


class TestLabels:
    def test_rebuilt_labels_match_the_panel(self):
        prices = _panel()
        y = _labelled_y(prices)
        got = X.label_prices(prices, y)
        assert set(got) == set(y)
        for key, (c_snap, c_fut, _d) in got.items():
            assert math.log(c_fut / c_snap) == y[key]

    def test_mismatch_stops(self):
        prices = _panel()
        y = _labelled_y(prices)
        key = next(iter(y))
        y[key] += 1e-6
        with pytest.raises(X.InputMismatch, match="合わない"):
            X.label_prices(prices, y)

    def test_zero_dividend_is_bit_identical(self):
        prices = _panel()
        y = _labelled_y(prices)
        y_total, div, counts = X.total_return_labels(X.label_prices(prices, y), {})
        assert all(div[k] == 0.0 for k in y)
        assert all(y_total[k] == y[k] for k in y)       # ビット一致（== で比べる）
        assert counts["none"] == len(y)

    def test_dividend_raises_the_label(self):
        prices = _panel()
        y = _labelled_y(prices)
        divs = {ec: [("2019-12-31", 10.0, 1.0, 1.0), ("2020-12-31", 10.0, 1.0, 1.0),
                     ("2021-12-31", 10.0, 1.0, 1.0), ("2022-12-31", 10.0, 1.0, 1.0)] for ec in prices}
        lab = X.label_prices(prices, y)
        y_total, div, _counts = X.total_return_labels(lab, divs)
        for key, (c_snap, c_fut, _d) in lab.items():
            assert div[key] == 10.0
            assert y_total[key] == math.log((c_fut + 10.0) / c_snap) > y[key]


class TestDividendInWindow:
    def test_uses_the_next_fiscal_year_scaled_to_todays_shares(self):
        recs = [("2020-03-31", 100.0, 5.0, 1.0), ("2021-03-31", 30.0, 1.0, 1.0)]
        assert X.dividend_in_window(recs, "2020-01-31") == (20.0, "next_fy", False)   # 1:5 分割の前の年
        assert X.dividend_in_window(recs, "2020-06-30") == (30.0, "next_fy", False)

    def test_dps_lag_is_applied(self):
        assert X.dividend_in_window([("2020-03-31", 100.0, 1.0, 2.0)], "2020-01-31")[0] == 50.0

    def test_falls_back_to_the_latest_known_year(self):
        recs = [("2020-03-31", 40.0, 1.0, 1.0)]
        assert X.dividend_in_window(recs, "2020-06-30") == (40.0, "fallback_asof", False)
        far = [("2020-03-31", 50.0, 1.0, 1.0), ("2023-03-31", 60.0, 1.0, 1.0)]
        assert X.dividend_in_window(far, "2021-01-31") == (50.0, "fallback_asof", False)

    def test_blank_dps_is_zero_and_flagged(self):
        assert X.dividend_in_window([("2020-03-31", None, 1.0, 1.0)], "2020-01-31") == (0.0, "next_fy", True)
        assert X.dividend_in_window([], "2020-01-31") == (0.0, "none", False)

    def test_yield_bands(self):
        assert X.yield_band(0.0) == "0"
        assert X.yield_band(0.005) == "(0%,1%]"
        assert X.yield_band(0.01) == "(0%,1%]"
        assert X.yield_band(0.035) == "(3%,4%]"
        assert X.yield_band(0.09) == ">4%"


# ── 消えた行の分け方 ────────────────────────────────────────────────────────────

def _snap_ym(prices: dict, ec: str, week: int) -> str:
    return prices[ec][week].trade_date[:7]


class TestClassify:
    @pytest.fixture
    def prices(self):
        n_end = 90
        collapse = _rising(80) + [100.0 * 0.5 ** (i + 1) for i in range(n_end - 80)]
        pinned = _rising(80) + [200.0, 201.0, 201.5, 200.5, 201.0, 200.8, 201.2, 200.9, 201.0, 201.1]
        unknown = [100.0 + (10.0 if i % 2 else -10.0) for i in range(n_end)]
        return {
            "LIVE": _series(_weeks(200), _rising(200)),        # 全体の最新週を決める
            "COLLAPSE": _series(_weeks(n_end), collapse),
            "PINNED": _series(_weeks(n_end), pinned),
            "UNKNOWN": _series(_weeks(n_end), unknown),
            # 160〜195 週が欠ける。130 週あたりの月末は 52 行先が無いが、系列は 52 週より後まで続く
            "GAPS": _series(_weeks(200, skip=set(range(160, 196))), _rising(164)),
        }

    def _classify(self, prices, ec, ym):
        global_last = max(rows[-1].trade_date for rows in prices.values())
        return X.classify_no_future((ym, ec), prices, global_last)

    def _month_end_ym(self, prices, ec, idx_from_end):
        dates = [r.trade_date for r in prices[ec]]
        return dates[ms.month_end_indices(dates)[-idx_from_end]][:7]

    def test_ended_reasons(self, prices):
        ym = self._month_end_ym(prices, "COLLAPSE", 6)
        assert self._classify(prices, "COLLAPSE", ym)["reason"] == "bankruptcy_suspect"
        assert self._classify(prices, "PINNED", ym)["reason"] == "tob_suspect"
        c = self._classify(prices, "UNKNOWN", ym)
        assert c["kind"] == "ended" and c["reason"] == "unknown"

    def test_recent_rows_are_not_counted(self, prices):
        ym = self._month_end_ym(prices, "LIVE", 2)
        assert self._classify(prices, "LIVE", ym)["kind"] == "recent"

    def test_gaps(self, prices):
        dates = [r.trade_date for r in prices["GAPS"]]
        week130 = (START + timedelta(weeks=130)).isoformat()
        i = next(i for i in ms.month_end_indices(dates) if dates[i] >= week130)
        assert i + ms.HORIZON_WEEKS >= len(dates)                       # 行で数えると 52 行先が無い
        assert dates[-1] >= ms._add_days(dates[i], X.HORIZON_DAYS)      # 暦では 52 週より後まで続く
        c = self._classify(prices, "GAPS", dates[i][:7])
        assert c == {"kind": "gaps", "reason": None, "r_last": None}

    def test_exit_at_last_close_return(self, prices):
        ym = self._month_end_ym(prices, "PINNED", 6)
        c = self._classify(prices, "PINNED", ym)
        dates = [r.trade_date for r in prices["PINNED"]]
        idx = next(i for i in ms.month_end_indices(dates) if dates[i][:7] == ym)
        assert c["r_last"] == pytest.approx(201.1 / prices["PINNED"][idx].close_last - 1.0)


class TestDelistingReport:
    def test_share_counts_ended_rows_only(self):
        prices = _panel()
        prices["E_LIVE"] = _series(_weeks(150), _rising(150))
        _s, _m, _c, _f, ids, no_future = _build(prices, return_no_future=True)
        yms = sorted(set(ids) | set(no_future))
        rep = X.delisting_report(no_future, ids, prices, yms, {})
        t = rep["test_months"]
        assert t["n_ended"] > 0 and t["n_gaps"] == 0 and t["n_recent_excluded"] > 0
        assert t["ended_share"] == pytest.approx(t["n_ended"] / (t["n_labelled"] + t["n_ended"]), abs=1e-6)
        assert sum(t["ended_by_reason"].values()) == t["n_ended"]
        # 系列が終わったのは E_B だけ
        ended_ecs = {ec for ym, ecs in no_future.items() for ec in ecs
                     if X.classify_no_future((ym, ec), prices, rep["global_last_week"])["kind"] == "ended"}
        assert ended_ecs == {"E_B"}


# ── 採点と判定 ──────────────────────────────────────────────────────────────────

class TestScoring:
    def test_predictions_stay_identical_and_only_labels_change(self, monkeypatch):
        seen = []
        monkeypatch.setattr(X, "oof_backtest", lambda resid: seen.append(resid) or {})
        order = {"2021-03": ["a", "b", "c"]}
        preds = {"m": {("2021-03", "a"): 0.3, ("2021-03", "b"): 0.1, ("2021-03", "c"): 0.2}}
        y = {("2021-03", "a"): 0.05, ("2021-03", "b"): -0.02, ("2021-03", "c"): 0.01}
        y_total = {k: v + 0.03 for k, v in y.items()}
        X.score_all(preds, order, y)
        X.score_all(preds, order, y_total)
        yh0 = [yh for yh, _ in seen[0]["2021-03"]]
        yh1 = [yh for yh, _ in seen[1]["2021-03"]]
        assert yh0 == yh1 == [0.3, 0.1, 0.2]
        assert [v for _, v in seen[1]["2021-03"]] == [y_total[("2021-03", e)] for e in "abc"]

    def test_mismatch_with_m4_stops(self):
        series = {"rank_ic_by_period": {"2021-03": 0.1}, "short_side_spread_by_period": {"2021-03": 0.02}}
        scores = {name: dict(series) for name in X.BASES}
        X.check_against_base_oof(scores, {name: dict(series) for name in X.BASES})
        bad = {name: dict(series) for name in X.BASES}
        bad["macro_enet"] = {"rank_ic_by_period": {"2021-03": 0.2}, "short_side_spread_by_period": {"2021-03": 0.02}}
        with pytest.raises(X.InputMismatch, match="base_oof"):
            X.check_against_base_oof(scores, bad)


def _oof(ic: dict, ss: dict, top: dict) -> dict:
    return {"rank_ic_by_period": ic, "short_side_spread_by_period": ss,
            "top_k_excess_return_by_period": {str(X.TOP_K): top}}


YMS = [f"{2018 + i // 4}-{(i % 4) * 3 + 3:02d}" for i in range(16)]


def _series_of(value: float, wiggle: float = 0.0) -> dict:
    return {ym: value + (wiggle if i % 2 else -wiggle) for i, ym in enumerate(YMS)}


class TestDecision:
    def test_flip_in_primary_proposes_a_switch(self):
        a, b = X.PAIR
        m6 = _oof(_series_of(0.20, 0.01), _series_of(0.03), _series_of(0.1))
        m2 = _oof(_series_of(0.10, 0.01), _series_of(0.03), _series_of(0.0))
        price_only = {a: m6, b: m2}
        total = {a: m6, b: _oof(_series_of(0.20, 0.01), _series_of(0.03), _series_of(0.0))}
        d = X.dividend_decision(price_only, total)
        assert d["tests"]["rank_ic_by_period"]["verdict_price_only"] == "+"
        assert d["tests"]["rank_ic_by_period"]["verdict_total"] == "0"
        assert d["flip_primary"] is True and d["decision"] == "propose_switch"

    def test_no_flip_keeps(self):
        a, b = X.PAIR
        m6 = _oof(_series_of(0.20, 0.01), _series_of(0.03), _series_of(0.1))
        m2 = _oof(_series_of(0.10, 0.01), _series_of(0.03), _series_of(0.0))
        d = X.dividend_decision({a: m6, b: m2}, {a: m6, b: m2})
        assert d["flip_primary"] is False and d["flip_top_k"] is False and d["decision"] == "keep"

    def test_top_k_sign_flip_proposes_a_switch(self):
        a, b = X.PAIR
        m6 = _oof(_series_of(0.10), _series_of(0.03), _series_of(0.1))
        m2 = _oof(_series_of(0.10), _series_of(0.03), _series_of(0.0))
        m2_total = _oof(_series_of(0.10), _series_of(0.03), _series_of(0.2))
        d = X.dividend_decision({a: m6, b: m2}, {a: m6, b: m2_total})
        assert d["flip_primary"] is False and d["flip_top_k"] is True and d["decision"] == "propose_switch"

    def test_missing_series_counts_as_not_significant(self):
        a, b = X.PAIR
        empty = _oof({}, {}, {})
        d = X.dividend_decision({a: empty, b: empty}, {a: empty, b: empty})
        assert d["tests"]["rank_ic_by_period"]["verdict_price_only"] == "0"
        assert d["decision"] == "keep"

    def test_delisting_threshold(self):
        assert X.DELIST_SHARE_THRESHOLD == 0.01
        assert X.delisting_decision(0.0101)["decision"] == "redesign"
        assert X.delisting_decision(0.01)["decision"] == "keep"
        assert X.delisting_decision(None)["decision"] == "undetermined"

    def test_alpha_is_bonferroni_over_two(self):
        assert X.ALPHA == pytest.approx(0.025)


# ── 事前登録と日中キュー ────────────────────────────────────────────────────────

class TestPrereg:
    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        import collector_utils
        monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)
        # 標準出力の差し替えは pytest のキャプチャを壊す（force_utf8_stdout の docstring）
        monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)

    def test_document_records_its_frozen_value(self):
        assert prereg.recorded_sha(prereg.read_document(X.PREREG)) == SHA

    def test_sha_mismatch_exits_2_before_loading(self):
        with pytest.raises(SystemExit) as e:
            X.main(["--prereg", X.PREREG, "--prereg-sha", "f" * 64])
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_other_document_exits_2(self):
        other = "docs/prereg/sell-dedicated-model.md"
        sha = prereg.body_sha256(prereg.read_document(other))
        assert X.main(["--prereg", other, "--prereg-sha", sha]) == prereg.EXIT_MISMATCH

    def test_issue_is_1154(self):
        assert prereg.issue_url(prereg.read_document(X.PREREG)).endswith("/issues/1154")


class TestDaytimeJob:
    def test_job_passes_the_frozen_document(self):
        job = rd.JOBS["measure:label-audit"]
        argv = list(job.argv)
        assert job.name == "label_audit" and job.parallel_sensitive is True
        assert argv[argv.index("--prereg") + 1] == X.PREREG
        assert argv[argv.index("--prereg-sha") + 1] == SHA
        assert "--smoke" not in argv

    def test_result_section_records_the_decision(self):
        # 2026-11-11 の実走で配当 propose_switch（#1187 を起票）・上場廃止 keep（#1188 で見直す）。
        # 書き写した結果を消さない（#1154）
        section = prereg.appendix_section(prereg.read_document(X.PREREG), "### 結果")
        assert not section.startswith(prereg.PLACEHOLDER)
        assert "判定: 配当 = propose_switch" in section
        assert "判定: 上場廃止 = keep" in section
