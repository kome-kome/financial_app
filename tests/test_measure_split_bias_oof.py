"""`scripts/measure_split_bias_oof.py` の再集計（純関数だけ・DB に触れない）。

前後の平均の差だけでは「数期の大きな揺れ」と「全期で一様に下がった」を区別できないので、
同じ期どうしの差を数える部分を縛る（ADR-0055 決定7 の 2026-09-17 追記）。
"""
from __future__ import annotations

import pytest

from scripts import measure_split_bias_oof as M


def _res(name: str, by_period: dict[str, float] | None, available: bool = True) -> dict:
    return {"models": [{
        "name": name,
        "available": available,
        "oof_backtest": {"rank_ic": {"mean": 0.1},
                         "rank_ic_by_period": by_period or {}},
    }]}


class TestSignTest:
    def test_matches_the_exact_binomial(self):
        # 17期中15期で低下＝2 × (C(17,0)+C(17,1)+C(17,2)) / 2^17
        assert M.sign_test_p(15, 2) == pytest.approx(2 * (1 + 17 + 136) / 2 ** 17)

    def test_is_symmetric(self):
        assert M.sign_test_p(3, 9) == M.sign_test_p(9, 3)

    def test_balanced_is_capped_at_one(self):
        assert M.sign_test_p(2, 2) == 1.0

    def test_no_informative_pairs_is_none(self):
        assert M.sign_test_p(0, 0) is None


class TestPairedByPeriod:
    def test_counts_only_common_periods(self):
        r = M.paired_by_period({"2021-06": 0.1, "2021-09": 0.2, "2022-03": 0.5},
                               {"2021-06": 0.05, "2021-09": 0.25, "2021-12": 9.9})
        assert r["n"] == 2
        assert (r["n_down"], r["n_up"]) == (1, 1)
        assert r["mean_diff"] == pytest.approx(0.0)
        assert set(r["diff_by_period"]) == {"2021-06", "2021-09"}

    def test_ties_are_neither_up_nor_down(self):
        r = M.paired_by_period({"a": 0.1, "b": 0.2}, {"a": 0.1, "b": 0.1})
        assert (r["n_down"], r["n_up"]) == (1, 0)
        assert r["sign_p"] == 1.0

    def test_empty_is_not_an_error(self):
        r = M.paired_by_period({}, {})
        assert r["n"] == 0 and r["mean_diff"] is None and r["sign_p"] is None


class TestByPeriodExtraction:
    def test_unavailable_model_is_kept_empty(self):
        assert M._rank_ics_by_period(_res("m", {"x": 0.1}, available=False)) == {"m": {}}

    def test_none_values_are_dropped(self):
        assert M._rank_ics_by_period(_res("m", {"x": 0.1, "y": None})) == {"m": {"x": 0.1}}


class TestReport:
    def test_report_pairs_before_and_after(self, capsys):
        before = _res("macro_gbdt", {"p1": 0.3, "p2": 0.2, "p3": 0.1})
        after = _res("macro_gbdt", {"p1": 0.2, "p2": 0.1, "p3": 0.2})
        paired = M.report({"before": before, "after": after})
        assert paired["macro_gbdt"]["n_down"] == 2
        out = capsys.readouterr().out
        assert "macro_gbdt" in out
        out.encode("cp932")     # タスクスケジューラ経由のログへ書ける文字だけ

    def test_summarize_does_not_touch_the_db(self, tmp_path, monkeypatch, capsys):
        import json

        import collector_utils

        def _boom(*a, **k):
            raise AssertionError("--summarize が係数表かモデル比較を呼んだ")

        monkeypatch.setattr(M, "_phase", _boom)
        # stdout を差し替えるので、そのままだと capsys が何も拾えない
        monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"before": _res("m", {"a": 0.2}),
                                 "after": _res("m", {"a": 0.1})}), encoding="utf-8")
        assert M.main(["--summarize", str(p)]) == 0
        assert "m" in capsys.readouterr().out
