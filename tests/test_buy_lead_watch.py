"""scripts/buy_lead_watch.py（#1225・買いの主役の点検）のテスト。

作り物の期別系列だけを使う（DB も計測も回さない）。事前登録 `docs/prereg/buy-lead-watch.md` の値（窓・有意の線・
連続の回数・基準の回）が道具の定数と食い違っていないことも照合する。
"""
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import buy_lead_watch as W  # noqa: E402
from scripts import measure_delisting_redesign as X  # noqa: E402
from scripts import prereg  # noqa: E402

# 直近 8 期だけ M-2 が上（rank-IC も上位10社超過も）＝事前登録 §4 の「当たり」になる値
REVERSED = (0.19, 0.23, 0.03, 0.10)
NORMAL = (0.20, 0.15, 0.06, 0.04)


def _months(n: int) -> list[str]:
    """2021-02 から 3 か月おきに n 期（19 期で 2025-08・20 期で 2025-11）。"""
    y, m, out = 2021, 2, []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m += 3
        if m > 12:
            y, m = y + 1, m - 12
    return out


def _series(n: int = 19, *, base: tuple = NORMAL, recent: tuple | None = None, seed: int = 5) -> dict:
    """M-6 と M-2 の期別系列。値は (M-6 の IC, M-2 の IC, M-6 の上位10社超過, M-2 の上位10社超過)。

    `recent` を渡すと最後の 8 期だけその値にする。期ごとに小さなぶれを足す。
    """
    rng = random.Random(seed)
    out = {W.LEAD: {"rank_ic": {}, "top_k_excess": {}}, W.RIVAL: {"rank_ic": {}, "top_k_excess": {}}}
    for i, ym in enumerate(_months(n)):
        li, ri, lt, rt = recent if (recent and i >= n - W.RECENT_N) else base
        out[W.LEAD]["rank_ic"][ym] = li + rng.gauss(0, 0.004)
        out[W.RIVAL]["rank_ic"][ym] = ri + rng.gauss(0, 0.004)
        out[W.LEAD]["top_k_excess"][ym] = lt + rng.gauss(0, 0.004)
        out[W.RIVAL]["top_k_excess"][ym] = rt + rng.gauss(0, 0.004)
    return out


def _row(ym: str, hit: bool, counted: bool = True) -> dict:
    return {"last_test_month": ym, "hit": hit, "counted": counted}


# ── 事前登録の値と道具の定数 ────────────────────────────────────────────────────

class TestRuleMatchesThePrereg:
    def test_constants(self):
        assert (W.LEAD, W.RIVAL) == X.PAIR            # 計測と同じ対（M-6 − 既定の設定の M-2）
        assert W.SOURCE == X._OUT                     # 読むのは計測が書く JSON
        assert W.ALPHA == 0.025 and W.RECENT_N == 8 and W.STREAK_TO_ACT == 2
        assert W.BASELINE_LAST == "2025-08"

    def test_document_states_the_same_values(self):
        text = prereg.read_document(W.PREREG)
        body, _ = prereg.split_document(text)
        assert prereg.issue_url(text).endswith("/issues/1225")
        for phrase in ("α = 0.05 / 2 = 0.025", "直近 8 期", "**2 回続けて**", "2025-08 より後"):
            assert phrase in body, phrase

    def test_ledger_header_in_the_document_matches_the_columns(self):
        section = prereg.appendix_section(prereg.read_document(W.PREREG), W.LEDGER_HEADING)
        assert "| " + " | ".join(W.LEDGER_COLUMNS) + " |" in section
        for row in W.ledger_rows(prereg.read_document(W.PREREG)):     # 行が足された後も読めること
            assert W._YM_RE.fullmatch(row["last_test_month"])


# ── 読み（窓・当たり・数える）──────────────────────────────────────────────────

class TestRead:
    def test_two_windows_on_the_baseline(self):
        r = W.read({"series": _series(19)})
        assert (r["n_periods"], r["last_test_month"]) == (19, "2025-08")
        assert (r["all"]["n"], r["all"]["first"]) == (19, "2021-02")
        assert (r["recent"]["n"], r["recent"]["first"], r["recent"]["last"]) == (8, "2023-11", "2025-08")
        assert r["all"]["rank_ic_diff"] == pytest.approx(0.05, abs=0.01)
        assert r["recent"]["top_k_excess_diff"] == pytest.approx(0.02, abs=0.01)
        assert r["hit"] is False and r["counted"] is False        # 凍結の時点の 19 期は基準の回

    def test_hit_needs_both_a_significant_rank_ic_gap_and_a_negative_top_k_gap(self):
        r = W.read({"series": _series(20, recent=REVERSED)})
        assert r["last_test_month"] == "2025-11" and r["counted"] is True
        assert r["recent"]["rank_ic_diff"] < 0 and r["recent"]["rank_ic_p"] < W.ALPHA
        assert r["recent"]["top_k_excess_diff"] < 0
        assert r["hit"] is True
        assert r["all"]["rank_ic_diff"] > 0                       # 全期は並べるだけ（判定に使わない）

    def test_rank_ic_alone_is_not_a_hit(self):
        """rank-IC の差が有意に負でも、上位10社超過の差が負でなければ当たりにしない（ユーザー判断 2026-11-15）。"""
        r = W.read({"series": _series(20, recent=(0.19, 0.23, 0.10, 0.03))})
        assert r["recent"]["rank_ic_diff"] < 0 and r["recent"]["rank_ic_p"] < W.ALPHA
        assert r["recent"]["top_k_excess_diff"] > 0 and r["hit"] is False

    def test_insignificant_negative_gap_is_not_a_hit(self):
        s = _series(20)
        for i, ym in enumerate(_months(20)[-W.RECENT_N:]):          # 符号が期ごとに入れ替わる差（平均はわずかに負）
            s[W.LEAD]["rank_ic"][ym] = s[W.RIVAL]["rank_ic"][ym] + (-0.06 if i % 2 == 0 else 0.05)
            s[W.LEAD]["top_k_excess"][ym] = s[W.RIVAL]["top_k_excess"][ym] - 0.05
        r = W.read({"series": s})
        assert r["recent"]["rank_ic_diff"] < 0 and r["recent"]["rank_ic_p"] >= W.ALPHA
        assert r["hit"] is False

    def test_baseline_reading_is_not_counted_even_when_it_hits(self):
        r = W.read({"series": _series(19, recent=REVERSED)})
        assert r["hit"] is True and r["counted"] is False
        assert W.streak([], r) == 0

    @pytest.mark.parametrize("payload, match", [
        ({"smoke": True, "series": _series(19)}, "smoke"),
        ({}, "series が無い"),
        ({"series": {W.LEAD: _series(19)[W.LEAD]}}, "series が無い"),
        ({"series": _series(7)}, "満たない"),
    ])
    def test_unusable_inputs_stop(self, payload, match):
        with pytest.raises(W.WatchInputError, match=match):
            W.read(payload)

    def test_misaligned_periods_stop(self):
        s = _series(19)
        del s[W.RIVAL]["top_k_excess"]["2024-05"]
        with pytest.raises(W.WatchInputError, match="そろわない"):
            W.read({"series": s})

    def test_missing_value_stops(self):
        s = _series(19)
        s[W.LEAD]["rank_ic"]["2024-05"] = None
        with pytest.raises(W.WatchInputError, match="値の無い期"):
            W.read({"series": s})


# ── 連続の回数 ──────────────────────────────────────────────────────────────────

class TestStreak:
    HIT = {"last_test_month": "2026-02", "hit": True, "counted": True}

    def test_first_counted_hit_is_one(self):
        assert W.streak([], dict(self.HIT, last_test_month="2025-11")) == 1

    def test_baseline_row_is_skipped(self):
        assert W.streak([_row("2025-08", True, counted=False)], dict(self.HIT, last_test_month="2025-11")) == 1

    def test_two_in_a_row(self):
        n = W.streak([_row("2025-08", True, counted=False), _row("2025-11", True)], self.HIT)
        assert n == 2 and n >= W.STREAK_TO_ACT

    def test_a_miss_in_between_resets(self):
        history = [_row("2025-11", True), _row("2026-02", False)]
        assert W.streak(history, dict(self.HIT, last_test_month="2026-05")) == 1

    def test_same_month_read_twice_counts_once(self):
        assert W.streak([_row("2025-11", True), _row("2026-02", True)], self.HIT) == 2
        assert W.streak([_row("2025-11", True)], dict(self.HIT, last_test_month="2025-11")) == 1

    def test_a_miss_now_is_zero(self):
        assert W.streak([_row("2025-11", True)], dict(self.HIT, hit=False)) == 0


# ── 点検の記録の行 ──────────────────────────────────────────────────────────────

class TestLedger:
    def _doc(self, *lines: str) -> str:
        header = "| " + " | ".join(W.LEDGER_COLUMNS) + " |"
        sep = "|" + "---|" * len(W.LEDGER_COLUMNS)
        table = "\n".join([header, sep, *lines])
        return (f"# t\n\n本文\n\n{prereg.APPEND_MARKER}\n\n## 追記節\n\n{W.LEDGER_HEADING}\n\n説明の文\n\n"
                f"{table}\n\n### 次\n\n| 2099-01 | 行ではない |\n")

    def test_empty_table_has_no_rows(self):
        assert W.ledger_rows(self._doc()) == []
        assert W.ledger_rows(self._doc("", "未記入")) == []

    def test_line_round_trips(self):
        baseline = W.read({"series": _series(19, recent=REVERSED)})
        counted = W.read({"series": _series(20, recent=REVERSED)})
        lines = [W.ledger_line(baseline, 0, "2026-11-17"), W.ledger_line(counted, 1, "2026-12-05")]
        assert lines[0].startswith("| 2025-08 | 19 |") and f"| {W.NOT_COUNTED} | 2026-11-17 |" in lines[0]
        assert lines[1].startswith("| 2025-11 | 20 |") and lines[1].endswith(f"| {W.YES} | {W.YES} | 1 | 2026-12-05 |")
        assert W.ledger_rows(self._doc(*lines)) == [
            {"last_test_month": "2025-08", "hit": True, "counted": False},
            {"last_test_month": "2025-11", "hit": True, "counted": True}]

    def test_rows_after_the_next_heading_are_ignored(self):
        assert W.ledger_rows(self._doc()) == []       # 「### 次」の下の行は別の節


# ── CLI ─────────────────────────────────────────────────────────────────────────

class TestCli:
    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch):
        import collector_utils
        monkeypatch.setattr(prereg, "_head_sha", lambda: "a" * 40)
        # 標準出力の差し替えは pytest のキャプチャを壊す（force_utf8_stdout の docstring）
        monkeypatch.setattr(collector_utils, "force_utf8_stdout", lambda: None)

    @staticmethod
    def _sha() -> str:
        return prereg.body_sha256(prereg.read_document(W.PREREG))

    def _source(self, tmp_path, payload: dict) -> str:
        p = tmp_path / "delisting_redesign.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_json_output_on_the_baseline(self, tmp_path, capsys):
        src = self._source(tmp_path, {"smoke": False, "computed_at": "2026-11-12T23:11:41+00:00",
                                       "series": _series(19, recent=REVERSED)})
        assert W.main(["--prereg", W.PREREG, "--prereg-sha", self._sha(), "--source", src, "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["prereg_sha256"] == self._sha() and out["prereg_issue"].endswith("/issues/1225")
        assert out["last_test_month"] == "2025-08" and out["hit"] is True and out["counted"] is False
        assert out["streak"] == 0 and out["act"] is False          # 基準の回は当たっても動かない
        assert out["ledger_line"].startswith("| 2025-08 | 19 |")
        assert out["rule"] == {"pair": [W.LEAD, W.RIVAL], "alpha": 0.025, "recent_n": 8,
                               "baseline_last": "2025-08", "streak_to_act": 2}

    def test_table_output_prints_the_ledger_line(self, tmp_path, capsys):
        src = self._source(tmp_path, {"series": _series(20, recent=REVERSED)})
        assert W.main(["--prereg", W.PREREG, "--prereg-sha", self._sha(), "--source", src]) == 0
        out = capsys.readouterr().out
        assert "| 2025-11 | 20 |" in out and "連続 1 回" in out
        assert "Issue を起票する" not in out                       # 1 回では動かない

    def test_wrong_sha_exits_2(self, tmp_path):
        src = self._source(tmp_path, {"series": _series(19)})
        with pytest.raises(SystemExit) as e:
            W.main(["--prereg", W.PREREG, "--prereg-sha", "f" * 64, "--source", src])
        assert e.value.code == prereg.EXIT_MISMATCH

    def test_other_document_exits_2(self, tmp_path):
        other = "docs/prereg/delisting-redesign.md"
        sha = prereg.body_sha256(prereg.read_document(other))
        src = self._source(tmp_path, {"series": _series(19)})
        assert W.main(["--prereg", other, "--prereg-sha", sha, "--source", src]) == prereg.EXIT_MISMATCH

    @pytest.mark.parametrize("payload", [{"smoke": True, "series": _series(19)}, {"scores": {}}])
    def test_unusable_source_exits_3(self, tmp_path, payload):
        src = self._source(tmp_path, payload)
        assert W.main(["--prereg", W.PREREG, "--prereg-sha", self._sha(), "--source", src]) == W.EXIT_INPUT

    def test_missing_source_exits_3(self, tmp_path):
        assert W.main(["--prereg", W.PREREG, "--prereg-sha", self._sha(),
                       "--source", str(tmp_path / "nope.json")]) == W.EXIT_INPUT
