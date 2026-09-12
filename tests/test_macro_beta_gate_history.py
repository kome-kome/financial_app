"""tests/test_macro_beta_gate_history.py — 収束ゲートの余裕を run 横断で読む（Issue #612）

`summarize_runs` / `render` は純関数なので DB も PyMC も要らない（`fetch_runs` だけが DB を
触り、そこはテストしない）。

**検体は本番 DB の `macro_beta_meta.hyperparams.diagnostics` から写した生値**を使う。推測で
書いた検体は「本物を読めないこと」を検出できないため、丸めずに貼る。
"""
from datetime import datetime

import pytest

import macro_beta_inference as mbi
from scripts.macro_beta_gate_history import render, summarize_runs

# ── 実測の検体（DB から写した生値） ─────────────────────────────────────────────

# mb_20260906T055243Z: 2026-09-06 の run。`--force` 付きの使い捨てタスクで書かれたので
# status=live だが**ゲートを通った証拠ではない**（#612 のコメント参照）。
BY_PARAM_20260906 = {
    "beta":        {"n": 46_044, "r_hat_p99": 1.0192854311419859, "r_hat_max": 1.073546832466667},
    "alpha":       {"n": 3_837,  "r_hat_p99": 1.0462810889549843, "r_hat_max": 1.1242346687861646},
    "mu_universe": {"n": 12,     "r_hat_p99": 1.037251967716491,  "r_hat_max": 1.0381113646117754},
}

# mb_20260907T015257Z: 重いテストを並走させた回。発散344回でサンプリングそのものが壊れ、
# 3変数すべてが閾値を超えて隔離された。**規模は 9/06 と同一**（n_stock=3837）。
BY_PARAM_20260907 = {
    "beta":        {"n": 46_044, "r_hat_p99": 1.0790, "r_hat_max": 1.0790},
    "alpha":       {"n": 3_837,  "r_hat_p99": 1.1843, "r_hat_max": 1.1843},
    "mu_universe": {"n": 12,     "r_hat_p99": 1.0837, "r_hat_max": 1.0837},
}

# mb_20260911T051941Z: 日中枠（#618）で並走なしに回した回。**現行コードで `--force` 無しに
# live へ到達した初めての run**（#612 前半の答え）。規模はやはり n_stock=3837。
BY_PARAM_20260911 = {
    "beta":        {"n": 46_044, "r_hat_p99": 1.014176215790639,  "r_hat_max": 1.0675122362666312},
    "alpha":       {"n": 3_837,  "r_hat_p99": 1.0317391544527446, "r_hat_max": 1.0647366065724613},
    "mu_universe": {"n": 12,     "r_hat_p99": 1.0352686321877826, "r_hat_max": 1.0354104156245563},
}


def _row(run_id, status, created, diagnostics):
    return {"run_id": run_id, "status": status, "created_at": created,
            "hyperparams": {"draws": 800, "tune": 800, "chains": 2,
                            "diagnostics": diagnostics}}


def _rows():
    """本番 DB と同じ5 run（古い順）。前2件は `by_param` を持たない旧 run。"""
    return [
        # #608 以前の run。r_hat_max は #356 の丸め時代の値（1.00/1.01/1.02 の3値解像度）。
        _row("mb_20260704T164023Z", None, datetime(2026, 7, 4, 16, 40, 23),
             {"r_hat_max": 1.02, "n_divergences": 0}),
        _row("mb_20260801T135935Z", None, datetime(2026, 8, 1, 13, 59, 35),
             {"r_hat_max": 1.03, "n_divergences": 0}),
        _row("mb_20260906T055243Z", "live", datetime(2026, 9, 6, 5, 52, 44),
             {"r_hat_max": 1.1242346687861646, "n_divergences": 0,
              "by_param": BY_PARAM_20260906}),
        _row("mb_20260907T015257Z", "quarantined", datetime(2026, 9, 7, 1, 52, 57),
             {"r_hat_max": 1.1843, "n_divergences": 344, "by_param": BY_PARAM_20260907}),
        _row("mb_20260911T051941Z", "live", datetime(2026, 9, 11, 5, 19, 41),
             {"r_hat_max": 1.0675122362666312, "n_divergences": 0,
              "by_param": BY_PARAM_20260911}),
    ]


def _by_id(items):
    return {i["run_id"]: i for i in items}


class TestSummarizeRuns:
    """run 横断の要約。**判定は本番（gate_verdict / persist_allowed）と一致させる**。"""

    def test_healthy_live_run_passes_with_its_margin(self):
        """9/11 の run: PASS。`alpha` の余裕は 0.0183 で `PERSIST_MARGIN_WARN` を切っていない。

        #612 が「余裕が尽きたときの基準」を今回も決められなかった根拠そのもの。
        """
        got = _by_id(summarize_runs(_rows()))["mb_20260911T051941Z"]
        assert got["verdict"] == "PASS"
        assert got["status"] == "live"
        assert got["healthy"] is True
        assert got["legacy"] is False
        assert got["gate"]["alpha"]["margin"] == pytest.approx(0.0182608455, abs=1e-9)
        assert got["gate"]["alpha"]["thin"] is False

    def test_deciding_variable_of_the_healthy_run_is_mu_universe(self):
        """9/11 で閾値に最も近いのは `alpha` ではなく `mu_universe`（1.0353 > 1.0317）。

        `alpha` だけを別扱いする案（#609 改善案1）が、健全な run では**そもそも効かない**
        ことを示す実測。値ではなく位置を残すのが要点（#600 と同じ理由）。
        """
        got = _by_id(summarize_runs(_rows()))["mb_20260911T051941Z"]
        assert got["worst"].startswith("mu_universe ")
        tightest = min(got["gate"], key=lambda n: got["gate"][n]["margin"])
        assert tightest == "mu_universe"

    def test_diverged_run_fails_and_is_flagged(self):
        """9/07 の run: FAIL かつ `healthy=False`。推移へ混ぜてはいけない回。"""
        got = _by_id(summarize_runs(_rows()))["mb_20260907T015257Z"]
        assert got["verdict"] == "FAIL"
        assert got["status"] == "quarantined"
        assert got["n_divergences"] == 344
        assert got["healthy"] is False
        # 3変数すべてが閾値を超えている＝余裕は負。
        assert all(g["margin"] < 0 for g in got["gate"].values())

    def test_thin_margin_of_the_forced_run_is_reported(self):
        """9/06 の run: 余裕 0.0037 で THIN。`log_gate_report` の警告条件と同じ判定。"""
        got = _by_id(summarize_runs(_rows()))["mb_20260906T055243Z"]
        assert got["verdict"] == "PASS"
        assert got["gate"]["alpha"]["margin"] == pytest.approx(0.0037189110, abs=1e-9)
        assert got["gate"]["alpha"]["thin"] is True

    def test_legacy_runs_fall_back_to_r_hat_max_and_are_flagged(self):
        """`by_param` の無い旧 run は変数名が `r_hat_max` になり `legacy=True` が立つ。

        生値（4桁）と丸め値（1.02）を並べて「縮んでいる」と読まないための印（#356）。
        """
        got = _by_id(summarize_runs(_rows()))["mb_20260704T164023Z"]
        assert set(got["gate"]) == {"r_hat_max"}
        assert got["legacy"] is True
        assert got["n_stock"] is None

    def test_n_stock_comes_from_the_alpha_group(self):
        """銘柄数は `alpha` の個数（銘柄ごとの切片）。規模依存を読むための軸。"""
        items = _by_id(summarize_runs(_rows()))
        assert items["mb_20260911T051941Z"]["n_stock"] == 3837
        assert items["mb_20260906T055243Z"]["n_stock"] == 3837

    def test_verdict_agrees_with_production_persist_allowed(self):
        """表の合否は `persist_allowed` と一致する（本番と別基準の表を見ても意味が無い）。"""
        for row, item in zip(_rows(), summarize_runs(_rows())):
            diag = row["hyperparams"]["diagnostics"]
            ok = mbi.persist_allowed(diag, mbi.MONTHLY_RHAT_THRESHOLD, force=False)
            assert (item["verdict"] == "PASS") is ok, item["run_id"]

    def test_threshold_is_the_monthly_one_by_default(self):
        items = summarize_runs(_rows())
        assert {i["threshold"] for i in items} == {mbi.MONTHLY_RHAT_THRESHOLD}

    def test_strict_threshold_narrows_every_margin(self):
        """`--threshold` は判定ごと切り替わる（strict 1.01 では 9/11 も落ちる）。"""
        got = _by_id(summarize_runs(_rows(), threshold=1.01))["mb_20260911T051941Z"]
        assert got["verdict"] == "FAIL"
        assert got["gate"]["alpha"]["margin"] < 0

    def test_missing_divergence_count_is_unknown_not_healthy(self):
        """発散数が診断に無い run は `healthy=None`＝「不明」を True と混ぜない。"""
        rows = [_row("mb_x", "live", datetime(2026, 1, 1), {"by_param": BY_PARAM_20260911})]
        assert summarize_runs(rows)[0]["healthy"] is None

    def test_json_text_hyperparams_are_accepted(self):
        """JSON 列がテキストで渡ってきても読める（他経路の写し・手で貼った検体）。"""
        import json

        row = _row("mb_x", "live", datetime(2026, 1, 1), {})
        row["hyperparams"] = json.dumps(
            {"diagnostics": {"n_divergences": 0, "by_param": BY_PARAM_20260911}})
        assert summarize_runs([row])[0]["verdict"] == "PASS"


class TestRender:
    """表の整形。**cp932 で書き出せること**を含めて固定する。"""

    def test_table_marks_the_runs_that_must_not_be_compared(self):
        out = render(summarize_runs(_rows()))
        assert "div=344" in out
        assert "old-gate" in out
        # 比べられるのは健全かつ by_param のある2件だけ。
        assert "2件 / 5件" in out

    def test_unmeasured_scale_dependence_is_stated(self):
        """比べられる run の `n_stock` が1種類しか無いことを表自身が言う（#612 の結論）。"""
        out = render(summarize_runs(_rows()))
        assert "規模依存は未実測" in out

    def test_thin_margin_is_marked(self):
        out = render(summarize_runs(_rows()))
        assert "THIN" in out

    def test_output_is_encodable_on_windows_console(self):
        """cp932 へ書き出せない記号を混ぜない。

        リダイレクト時に `print()` が UnicodeEncodeError で落ちるのは、この表を
        `.logs/` へ残す運用と相性が悪い（過去に同型の失敗がある）。
        """
        render(summarize_runs(_rows())).encode("cp932")

    def test_empty_history_does_not_crash(self):
        assert "run がありません" in render([])
